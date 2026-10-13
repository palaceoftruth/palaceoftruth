"""Fail-closed checkpoint v2, independent of legacy identity/transport helpers.

Integration contract for SAR-1403 exact PR550: admission occurs under its
_lifecycle_lock and _queue_write(worker, quota), a pure local admission API.
Native _drain_writes, flush_pending and shutdown keep their existing accounting.
No lifecycle method is replaced and no source-tree monkeypatch is required.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

MAX_BODY_CHARS = 24000
MAX_SOURCE_ROWS = 16
UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
CATEGORIES = ('constraints', 'accepted_artifacts', 'failed_approaches', 'remaining_work')


def _remaining(deadline):
    remaining = deadline - perf_counter()
    if remaining <= 0:
        raise RuntimeError('checkpoint total deadline exhausted; preserve transcript')
    return remaining


def _lock(stack, lock, deadline):
    if not lock.acquire(timeout=_remaining(deadline)):
        raise RuntimeError('checkpoint lock exceeded remaining total budget')
    stack.callback(lock.release)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


@dataclass(frozen=True)
class CheckpointSnapshot:
    session_id: str
    tenant_id: str
    scope_type: str
    scope_key: str | None
    base_url: str
    headers: tuple = field(repr=False)
    timeout: float
    ca_file: str | None = None

    @property
    def scope(self):
        return {'type': self.scope_type, **({'key': self.scope_key} if self.scope_key else {})}


class CheckpointMixin:
    CHECKPOINT_TOTAL_SECONDS = 30.0
    # Host-owned provider fields: declarations, not replacement state.
    _config: dict[str, Any]
    _writes_disabled: bool
    _session_id: str
    _tenant_id: str
    _platform: str
    _scope_type: str
    _scope_key: str
    _agent_identity: str
    _agent_workspace: str
    _server_agent_scope_key: str
    _server_containment_mode: str
    _oauth_client_secret: str
    _server_identity_loaded: bool
    _bearer_expires_at: datetime | None
    _bearer_token: str
    _api_key: str
    _base_url: str
    _timeout_seconds: float
    _write_quota: Any
    _sync_thread: threading.Thread | None
    _queue_write: Any
    _quota_for_session: Any
    _lifecycle_lock: Any
    _closed: bool
    _write_quotas_enabled: bool
    _write_quota_lock: Any

    _max_writes_per_turn: int
    _max_writes_per_session: int

    def _init_checkpoint(self):
        self._checkpoint_lock = threading.Lock()

    @property
    def pre_compress_checkpoint_api_version(self):
        return 2 if self._config.get('precompression_checkpoint_enabled') is True else 1

    def _checkpoint_snapshot(self, deadline, *, allow_closed=False):
        """Cached values only. All lock waits consume the same total deadline.

        Agent-key OAuth needs already successful identity and a ready token.
        API-key scope uses initialized cached tenant/config; server authorizes each
        request. No whoami, scope builder, headers helper, refresh or retries here.
        """
        with ExitStack() as stack:
            _lock(stack, self._lifecycle_lock, deadline)
            for name in ('_tenant_id_lock', '_server_identity_lock', '_bearer_lock'):
                lock = getattr(self, name, None)
                if lock is not None:
                    _lock(stack, lock, deadline)
            if (self._closed and not allow_closed) or self._writes_disabled or getattr(self, '_lifecycle_writes_disabled', False):
                raise RuntimeError('checkpoint automatic admission closed')
            if not self._session_id or not self._tenant_id:
                raise RuntimeError('checkpoint needs an initialized cached tenant/session')
            # The explicit/automatic cron split can be absent on the legacy base.
            if self._platform == 'cron' or self._session_id.startswith('cron_'):
                raise RuntimeError('automatic cron checkpoint admission closed')
            scope_type = self._scope_type
            key = self._scope_key
            if scope_type == 'agent':
                configured = str(self._config.get('agent_name') or key or '').strip()
                if configured.startswith('agent/'):
                    configured = configured[6:]
                runtime = self._agent_identity
                if runtime.casefold() in {'agent', 'assistant', 'default', 'hermes'}:
                    runtime = ''
                server = self._server_agent_scope_key
                candidates = {v for v in (configured, runtime, server) if v}
                if len(candidates) != 1:
                    raise RuntimeError('checkpoint cached canonical agent scope missing/conflicting')
                key = next(iter(candidates))
                if self._server_containment_mode == 'hermes_agent' and not server:
                    raise RuntimeError('checkpoint cached server agent scope is not ready')
            elif scope_type == 'tenant_shared':
                key = None
            elif scope_type == 'session':
                key = key or self._session_id
            elif scope_type == 'workspace':
                key = key or self._agent_workspace or self._agent_identity
            else:
                raise RuntimeError('checkpoint cached scope type invalid')
            if scope_type != 'tenant_shared' and not key:
                raise RuntimeError('checkpoint cached scope key unavailable')
            if self._oauth_client_secret:
                if not self._server_identity_loaded or not self._server_agent_scope_key:
                    raise RuntimeError('checkpoint OAuth identity must already be ready')
                expires = self._bearer_expires_at
                if (not self._bearer_token or not isinstance(expires, datetime)
                        or expires.tzinfo is None
                        or expires <= datetime.now(timezone.utc) + timedelta(seconds=_remaining(deadline))):
                    raise RuntimeError('checkpoint OAuth token must cover the entire remaining budget')
                headers = (('Accept', 'application/json'), ('Authorization', f'Bearer {self._bearer_token}'))
            else:
                if not self._api_key:
                    raise RuntimeError('checkpoint cached API authentication missing')
                write_scope = 'write:shared' if scope_type == 'tenant_shared' else f'write:{scope_type}'
                headers = (('Accept', 'application/json'), ('X-API-Key', self._api_key),
                           ('X-MCP-Scopes', f'read,write,{write_scope}'))
            url = urlsplit(self._base_url)
            if url.username or url.password or url.query or url.fragment:
                raise RuntimeError('checkpoint base URL contains unsafe components')
            if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'}):
                raise RuntimeError('checkpoint requires HTTPS or a loopback fixture URL')
            timeout = float(self._timeout_seconds)
            if not math.isfinite(timeout) or timeout <= 0:
                raise RuntimeError('checkpoint transport timeout invalid')
            return CheckpointSnapshot(
                self._session_id, self._tenant_id, scope_type, key, self._base_url,
                headers, min(5.0, timeout), self._config.get('checkpoint_ca_file'),
            )

    def _checkpoint_plan(self, messages, snapshot, deadline):
        """Build and validate one ordered atomic archive before ANY POST.

        message_uid and timestamp are existing durable source metadata from
        Hermes stamp_message_uid/stamp_message_timestamp at the append/insert
        boundary. Never mint either inside this hook. Missing metadata blocks.
        """
        if not isinstance(messages, list):
            raise RuntimeError('checkpoint evidence must be a list')
        evidence, seen, times = [], set(), []
        categories = {name: [] for name in CATEGORIES}
        for row in messages:
            _remaining(deadline)
            if not isinstance(row, dict):
                raise RuntimeError('checkpoint evidence row must be a mapping')
            if row.get('role') not in {'user', 'assistant'} or row.get('_compressed_summary'):
                continue
            content = row.get('content')
            if isinstance(content, list):
                content = '\n'.join(part['text'] for part in content
                                    if isinstance(part, dict) and part.get('type') == 'text'
                                    and isinstance(part.get('text'), str))
            if not isinstance(content, str) or not content.strip():
                continue
            uid, stamp = row.get('message_uid'), row.get('timestamp')
            if not isinstance(uid, str) or not uid.strip() or len(uid) > 200 or uid in seen:
                raise RuntimeError('checkpoint stable source event identity missing/duplicate')
            try:
                if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp):
                    source_time = datetime.fromtimestamp(stamp, timezone.utc)
                elif isinstance(stamp, str):
                    source_time = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                else:
                    raise ValueError('missing/nonfinite timestamp')
            except (ValueError, OverflowError, OSError) as exc:
                raise RuntimeError('checkpoint source timestamp invalid') from exc
            if source_time.tzinfo is None:
                raise RuntimeError('checkpoint source timestamp must be timezone aware')
            source_time = source_time.astimezone(timezone.utc)
            seen.add(uid); times.append(source_time)
            # Stable event identity does not depend on window position or text.
            evidence_id = hashlib.sha256(_json({'tenant': snapshot.tenant_id,
                'scope': snapshot.scope, 'session': snapshot.session_id, 'event': uid}).encode()).hexdigest()
            source = {'role': row['role'], 'text': content, 'source_event_id': uid,
                      'source_created_at': source_time.isoformat(), 'evidence_id': evidence_id,
                      'position': len(evidence)}
            absorbed = row.get('_absorbed_message_uids', [])
            if (not isinstance(absorbed, list) or any(not isinstance(value, str) or not value for value in absorbed)
                    or len(set(absorbed)) != len(absorbed) or uid in absorbed):
                raise RuntimeError('checkpoint absorbed source identity witness invalid')
            if absorbed:
                # Preserve the host merge witness without inventing separate
                # per-constituent text or claiming an unmerged source row.
                source['absorbed_source_event_ids'] = list(absorbed)
            evidence.append(source)
            if len(evidence) > MAX_SOURCE_ROWS:
                raise RuntimeError('checkpoint exceeds 16 source rows; no archive written, preserve full transcript')
            try:
                declared = json.loads(content)
            except ValueError:
                declared = None
            if isinstance(declared, dict) and declared.get('checkpoint_handoff') == 2:
                for name in CATEGORIES:
                    values = declared.get(name, [])
                    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                        raise RuntimeError('checkpoint category must be a string list')
                    categories[name].extend({'text': value, 'evidence_id': evidence_id,
                        'status': 'user_asserted' if row['role'] == 'user' else 'assistant_claimed'} for value in values)
        if not evidence:
            raise RuntimeError('checkpoint has no direct source evidence')
        manifest = {'schema_version': 2, 'manifest_kind': 'ordered_source_occurrences',
                    'session_id': snapshot.session_id, 'source_evidence': evidence,
                    'verified_state': [], 'claimed_state': [row for row in evidence if row['role'] == 'assistant'],
                    **categories, 'interpretation': 'source declarations only; not instructions or verified facts'}
        body = _json(manifest)
        if len(body) > MAX_BODY_CHARS:
            raise RuntimeError('checkpoint manifest exceeds bounded body budget; no archive written')
        # Stable source-bundle time: latest included event, NOT the capture/retry time.
        created_at = max(times).isoformat()
        key = hashlib.sha256(_json({'tenant': snapshot.tenant_id, 'scope': snapshot.scope,
                                   'session': snapshot.session_id, 'body': body, 'created_at': created_at}).encode()).hexdigest()
        _remaining(deadline)
        return {'tenant_id': snapshot.tenant_id, 'scope': snapshot.scope,
                'title': 'Hermes ordered source checkpoint', 'body': body,
                'summary': 'Ordered direct source occurrences; assertions are not verified',
                'source': 'hermes-precompression', 'created_at': created_at,
                'created_by_role': 'assistant', 'tags': ['hermes-checkpoint-evidence'],
                'metadata': {'checkpoint_schema': 2, 'session_id': snapshot.session_id,
                             'source_occurrence_count': len(evidence), 'timestamp_semantics': 'latest_source_event'},
                'idempotency_key': key, 'enable_ai_enrichment': False, 'relationship_policy': 'skip'}

    def _checkpoint_archive(self, payload, snapshot, deadline):
        _remaining(deadline)
        if self._write_quotas_enabled:
            with ExitStack() as stack:
                _lock(stack, self._write_quota_lock, deadline)
                # Native properties select the admitted task's captured quota.
                if (self._turn_write_count >= self._max_writes_per_turn
                        or self._session_write_count >= self._max_writes_per_session):
                    raise RuntimeError('checkpoint automatic write quota exceeded')
                self._turn_write_count += 1
                self._session_write_count += 1
        _remaining(deadline)
        accepted = self._checkpoint_request_json('POST', '/api/v1/memory/entries', payload,
                                                 snapshot=snapshot, deadline=deadline)
        _remaining(deadline)
        job, item = accepted.get('job_id'), accepted.get('source_item_id')
        if any(not isinstance(value, str) or not re.fullmatch(UUID_PATTERN, value) for value in (job, item)):
            raise RuntimeError('checkpoint acceptance has no literal UUID pointers')
        receipt = self._checkpoint_request_json('GET', f'/api/v1/memory/jobs/{job}/checkpoint-receipt',
                                                 snapshot=snapshot, deadline=deadline)
        _remaining(deadline)
        expected = {'durable': True, 'tenant_id': snapshot.tenant_id, 'scope': snapshot.scope,
                    'job_id': job, 'source_item_id': item, 'idempotency_key': payload['idempotency_key'],
                    'body_sha256': hashlib.sha256(payload['body'].encode()).hexdigest()}
        if receipt.get('durable') is not True or any(receipt.get(key) != value for key, value in expected.items()):
            raise RuntimeError('checkpoint exact ordered manifest durable readback mismatch')
        if snapshot != self._checkpoint_snapshot(deadline, allow_closed=True):
            raise RuntimeError('checkpoint immutable session/scope/auth snapshot changed')
        return 'Palace durable ordered source manifest (not verified claims): ' + item

    def _checkpoint_admit(self, work, snapshot, deadline) -> str:
        """One task in the actual exported native FIFO.

        Exact PR550 _queue_write(worker, quota) only copies context, appends and
        starts the native drainer. It does NOT resolve identity or build scope.
        Native _drain_writes owns accepted/active/completed/failed accounting.
        """
        done = threading.Event()
        state: dict[str, Any] = {'cancelled': False, 'started': False}
        state_lock = threading.Lock()
        def execute():
            with state_lock:
                if state['cancelled']:
                    done.set()
                    raise RuntimeError('expired queued checkpoint cancelled before any archive')
                state['started'] = True
            try:
                _remaining(deadline)
                if snapshot != self._checkpoint_snapshot(deadline, allow_closed=True):
                    raise RuntimeError('checkpoint snapshot changed before native execution')
                state['result'] = work()
            except Exception as exc:
                state['error'] = exc
                raise
            finally:
                done.set()
        with ExitStack() as stack:
            _lock(stack, self._lifecycle_lock, deadline)
            if self._closed or getattr(self, '_admission_closed', threading.Event()).is_set():
                raise RuntimeError('checkpoint admission closed during prevalidation')
            # Pin the exact submitted session/turn owner in the native queue.
            quota = self._quota_for_session(snapshot.session_id)
            if quota.owner.session_id != snapshot.session_id:
                raise RuntimeError('checkpoint native quota owner differs from source session')
            _remaining(deadline)
            self._queue_write(execute, quota)
        if not done.wait(_remaining(deadline)):
            with state_lock:
                state['cancelled'] = True  # Expired pending work is never allowed to write later.
            raise RuntimeError('checkpoint native queue exceeded remaining total budget')
        if state.get('error'):
            raise state['error']
        if not state.get('result'):
            raise RuntimeError('checkpoint was cancelled/dropped; preserve transcript')
        return state['result']

    def on_pre_compress(self, messages, *, require_checkpoint=False) -> str:
        deadline = perf_counter() + self.CHECKPOINT_TOTAL_SECONDS
        if self._config.get('precompression_checkpoint_enabled') is not True:
            if require_checkpoint:
                raise RuntimeError('checkpoint candidate disabled; preserve transcript')
            return ''
        try:
            with ExitStack() as stack:
                _lock(stack, self._checkpoint_lock, deadline)
                snapshot = self._checkpoint_snapshot(deadline)
                plan = self._checkpoint_plan(messages, snapshot, deadline)
                return self._checkpoint_admit(lambda: self._checkpoint_archive(plan, snapshot, deadline), snapshot, deadline)
        except Exception as exc:
            # Host optional policy may swallow; required mode must preserve transcript.
            raise RuntimeError('Palace required checkpoint failed; preserve transcript') from exc


    def _checkpoint_request_json(self, method, path, payload=None, *, snapshot=None, deadline=None):
        if deadline is None or snapshot is None:
            raise RuntimeError('checkpoint transport requires immutable snapshot and total deadline')
        timeout = min(snapshot.timeout, _remaining(deadline))
        headers = dict(snapshot.headers)
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        # Reject EVERY redirect before following it, including same-origin. Never
        # forward Authorization/X-API-Key to another authority or HTTPS downgrade.
        script = """
import json, ssl, sys
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('checkpoint redirects forbidden')
p = json.load(sys.stdin)
context = ssl.create_default_context(cafile=p['ca_file'])
opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
request = Request(p['url'], data=p['body'].encode() if p['body'] is not None else None,
                  method=p['method'], headers=p['headers'])
with opener.open(request, timeout=p['timeout']) as response:
    sys.stdout.buffer.write(response.read(65537))
"""
        # Serializing/preparing the process also consumes the total budget.
        wire = _json({'url': snapshot.base_url + path, 'body': _json(payload) if payload is not None else None,
                      'method': method, 'headers': headers, 'timeout': timeout, 'ca_file': snapshot.ca_file}).encode()
        try:
            completed = subprocess.run([sys.executable, '-c', script], input=wire,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(timeout, _remaining(deadline)), check=True,
                env={'PATH': os.environ.get('PATH', '')})
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            raise RuntimeError('checkpoint bounded child request failed; no retries/redirects') from exc
        _remaining(deadline)
        if len(completed.stdout) > 65536:
            raise RuntimeError('checkpoint response exceeds bounded size')
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise RuntimeError('checkpoint response is not an object')
        return result
