"""Default-off plugin-owned outbox. Local commit is NOT remote durability.

No host objects, monkeypatches, credentials or arbitrary ContextVars are persisted.
SQLite FULL commit/readback assumes a functioning local filesystem. busy_timeout
bounds SQLite lock admission, not a frozen kernel/fsync. See the alternative contract.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
MAX_BYTES = 131072
MAX_ROWS = 1000
ALLOWED = {'tenant_id', 'scope', 'title', 'body', 'summary', 'source', 'created_at',
           'created_by_role', 'metadata', 'tags', 'idempotency_key', 'valid_from',
           'valid_until', 'supersedes_entry_id', 'fact_kind', 'enable_ai_enrichment',
           'relationship_policy'}


class OutboxError(RuntimeError):
    """Constant safe errors only; never raw SQL/network/request content."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_tenant(value):
    # Backend tenant columns are Text and authenticated identity is str, not UUID.
    # This finite client representation policy rejects unsafe values; never repairs them.
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or any(c.isspace() or unicodedata.category(c).startswith('C') for c in value)):
        raise OutboxError('outbox tenant identifier invalid')
    return value


def validate_binding(binding):
    if not isinstance(binding, dict) or set(binding) != {'origin', 'tenant_id', 'credential', 'scope'}:
        raise OutboxError('outbox binding fields invalid')
    value = copy.deepcopy(binding)
    url = urlsplit(value['origin'])
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.path or url.query or url.fragment or value['origin'].endswith('/')):
        raise OutboxError('outbox requires exact HTTPS origin')
    validate_tenant(value['tenant_id'])
    credential=value['credential']
    if not isinstance(credential,dict) or credential.get('mode') not in {'api_key','mcp_oauth'}:
        raise OutboxError('outbox explicit credential mode missing')
    fields={'mode','id','client_key'} if credential['mode']=='mcp_oauth' else {'mode','id'}
    if set(credential)!=fields or not re.fullmatch(UUID,str(credential.get('id',''))):
        raise OutboxError('outbox server-owned credential ID required')
    if credential['mode']=='mcp_oauth' and (not isinstance(credential['client_key'],str) or not credential['client_key']):
        raise OutboxError('outbox registered OAuth client key required')
    scope = value['scope']
    if not isinstance(scope, dict) or scope.get('type') not in {'agent', 'workspace', 'session'}:
        raise OutboxError('outbox scope policy invalid')
    expected = {'type'} if scope['type'] == 'session' else {'type', 'key'}
    if set(scope) != expected or ('key' in scope and (not isinstance(scope['key'], str) or not scope['key'])):
        raise OutboxError('outbox requires explicit scope binding')
    return value


def validate_payload(payload, binding):
    if not isinstance(payload, dict) or set(payload) - ALLOWED:
        raise OutboxError('outbox payload fields invalid; credentials forbidden')
    if payload.get('tenant_id') != binding['tenant_id']:
        raise OutboxError('outbox tenant mismatch')
    scope = payload.get('scope')
    metadata = payload.get('metadata')
    if not isinstance(metadata, dict) or not isinstance(metadata.get('session_id'), str) or not metadata['session_id']:
        raise OutboxError('outbox submitted session missing')
    if binding['scope']['type'] == 'session':
        expected = {'type': 'session', 'key': metadata['session_id']}
    else:
        expected = binding['scope']
    if scope != expected:
        raise OutboxError('outbox immutable scope mismatch')
    if not re.fullmatch(r'[a-f0-9]{64}', str(payload.get('idempotency_key', ''))):
        raise OutboxError('outbox literal idempotency digest missing')
    if not isinstance(payload.get('body'), str) or not payload['body']:
        raise OutboxError('outbox raw body missing')
    # Authentication keys are never metadata. Content may still be sensitive:
    # permissions are confidentiality protection, not encryption or redaction.
    def scan(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if str(key).casefold() in {'authorization', 'api_key', 'access_token', 'refresh_token', 'client_secret', 'password', 'headers'}:
                    raise OutboxError('outbox credential-shaped field forbidden')
                scan(item)
        elif isinstance(node, list):
            for item in node: scan(item)
    scan(metadata)
    text = canonical(payload)
    if len(text.encode()) > MAX_BYTES:
        raise OutboxError('outbox normalized payload exceeds cap')
    return json.loads(text)


def secure(path, *, directory=False):
    st = path.lstat()
    valid = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if (not valid or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != (0o700 if directory else 0o600)
            or (not directory and st.st_nlink != 1)):
        raise OutboxError('outbox private ownership/type/mode invalid')


def canonical_receipt(payload, receipt, accepted=None):
    if not isinstance(receipt, dict) or receipt.get('durable') is not True:
        raise OutboxError('outbox canonical durable receipt absent')
    validate_tenant(payload.get('tenant_id'))
    validate_tenant(receipt.get('tenant_id'))
    expected = {'tenant_id': payload['tenant_id'], 'scope': payload['scope'],
                'idempotency_key': payload['idempotency_key'],
                'body_sha256': hashlib.sha256(payload['body'].encode()).hexdigest()}
    for key in ['job_id', 'source_item_id']:
        value = receipt.get(key)
        if not isinstance(value, str) or not re.fullmatch(UUID, value):
            raise OutboxError('outbox literal receipt pointers invalid')
        expected[key] = accepted[key] if accepted is not None else value
    if any(receipt.get(k) != v for k, v in expected.items()):
        raise OutboxError('outbox canonical durable receipt mismatch')
    # Persist only canonical evidence, never arbitrary server data or auth keys.
    return {'durable': True, **expected}


class OutboxStore:
    def __init__(self, directory, binding, *, create=True):
        self._binding_text = canonical(validate_binding(binding))
        self.directory = Path(directory).absolute()
        if self.directory.resolve() != self.directory:
            raise OutboxError('outbox symlink path forbidden')
        if create:
            self.directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        secure(self.directory, directory=True)
        self.path = self.directory / 'outbox.sqlite3'
        if create:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600) if not self.path.exists() else None
            if fd is not None: os.close(fd)
        self._verify_files()
        with self._connect() as con:
            if create:
                con.execute('CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)')
                con.execute('CREATE TABLE IF NOT EXISTS records (seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, sha TEXT NOT NULL, semantic_sha TEXT NOT NULL, session TEXT NOT NULL, state TEXT NOT NULL DEFAULT \'pending\' CHECK(state IN (\'pending\',\'delivered\')), receipt TEXT)')
                con.execute('INSERT OR IGNORE INTO binding VALUES (1,?)', (canonical(self.binding),))
            row = con.execute('SELECT value FROM binding WHERE id=1').fetchone()
            if row is None or row[0] != canonical(self.binding):
                raise OutboxError('outbox persisted binding differs')
        if create:
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(fd)
            finally: os.close(fd)

    @property
    def binding(self):
        return json.loads(self._binding_text)

    def _verify_files(self):
        secure(self.directory, directory=True)
        secure(self.path)
        for name in ['outbox.sqlite3-journal', 'worker.lock']:
            path = self.directory / name
            if path.exists() or path.is_symlink(): secure(path)

    @contextmanager
    def _connect(self):
        self._verify_files()
        con = None
        try:
            con = sqlite3.connect(str(self.path), timeout=.1)
            con.execute('PRAGMA synchronous=FULL')
            con.execute('PRAGMA journal_mode=DELETE')
            with con: yield con
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise OutboxError('outbox local transaction/readback failed') from None
        finally:
            if con is not None: con.close()

    def records(self):
        with self._connect() as con:
            rows = con.execute('SELECT seq,key,payload,sha,semantic_sha,session,state,receipt FROM records ORDER BY seq').fetchall()
        result = []
        for seq, key, text, sha, semantic_sha, session, state, receipt in rows:
            try:
                payload = validate_payload(json.loads(text), self.binding)
                if digest(payload) != sha or payload['idempotency_key'] != key or payload['metadata']['session_id'] != session:
                    raise OutboxError('outbox stored evidence corrupted')
                semantic = {k: v for k, v in payload.items() if k != 'created_at'}
                if digest(semantic) != semantic_sha: raise OutboxError('outbox semantic evidence corrupted')
                parsed_receipt = json.loads(receipt) if receipt else None
                if state == 'delivered':
                    parsed_receipt = canonical_receipt(payload, parsed_receipt)
                elif parsed_receipt is not None:
                    raise OutboxError('outbox pending record has contradictory receipt')
                result.append({'seq': seq, 'key': key, 'payload': payload, 'sha': sha,
                               'state': state, 'receipt': parsed_receipt})
            except (ValueError, TypeError, KeyError):
                raise OutboxError('outbox record encoding invalid') from None
        return result

    def admit(self, payload, *, max_session):
        payload = validate_payload(payload, self.binding)
        if not isinstance(max_session, int) or max_session < 1:
            raise OutboxError('outbox finite session quota required')
        key, session = payload['idempotency_key'], payload['metadata']['session_id']
        semantic_sha = digest({k: v for k, v in payload.items() if k != 'created_at'})
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            prior = con.execute('SELECT semantic_sha FROM records WHERE key=?', (key,)).fetchone()
            if prior:
                if prior[0] != semantic_sha: raise OutboxError('outbox idempotency conflict')
            else:
                if con.execute('SELECT count(*) FROM records').fetchone()[0] >= MAX_ROWS:
                    raise OutboxError('outbox total retained row cap reached')
                if con.execute('SELECT count(*) FROM records WHERE session=?', (session,)).fetchone()[0] >= max_session:
                    raise OutboxError('outbox persistent session quota exceeded')
                con.execute('INSERT INTO records(key,payload,sha,semantic_sha,session) VALUES (?,?,?,?,?)',
                            (key, canonical(payload), digest(payload), semantic_sha, session))
        # Separate connection after COMMIT: not queue acceptance/in-memory evidence.
        row = next((r for r in self.records() if r['key'] == key), None)
        if row is None: raise OutboxError('outbox committed readback absent')
        return {'local_durable': True, 'remote_durable': False, 'boundary': 'local_sqlite_FULL_commit_readback',
                'idempotency_key': key, 'payload_sha256': row['sha'], 'duplicate': prior is not None}

    def is_duplicate(self, payload):
        payload = validate_payload(payload, self.binding)
        semantic = digest({k: v for k, v in payload.items() if k != 'created_at'})
        for row in self.records():
            if row['key'] == payload['idempotency_key']:
                prior = digest({k: v for k, v in row['payload'].items() if k != 'created_at'})
                if prior != semantic: raise OutboxError('outbox idempotency conflict')
                return True
        return False

    @contextmanager
    def worker_lock(self):
        self._verify_files()
        fd = os.open(self.directory / 'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            secure(self.directory / 'worker.lock')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise OutboxError('outbox recovery already active') from None
            yield
        finally:
            os.close(fd)  # OS releases lock after exit/crash; no stale lease guessing.

    def mark_delivered(self, row, receipt):
        receipt = canonical_receipt(row['payload'], receipt)
        with self._connect() as con:
            updated = con.execute('UPDATE records SET state=\'delivered\',receipt=? WHERE key=? AND sha=? AND state=\'pending\'',
                                  (canonical(receipt), row['key'], row['sha']))
            if updated.rowcount != 1: raise OutboxError('outbox delivery transition conflict')
        reread = next(r for r in self.records() if r['key'] == row['key'])
        if reread['state'] != 'delivered' or reread['receipt'] != receipt:
            raise OutboxError('outbox delivery receipt readback failed')


class RecoveryWorker:
    def __init__(self, store, transport):
        self.store, self.transport = store, transport
        if isinstance(transport, RestTransport) and transport.binding != store.binding:
            raise OutboxError('outbox external transport binding differs')

    def run(self, *, max_jobs=10, timeout=10):
        if not isinstance(max_jobs, int) or not 1 <= max_jobs <= MAX_ROWS or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise OutboxError('outbox finite recovery budget invalid')
        deadline = perf_counter() + timeout
        result = {'remote_durable': 0, 'failed': 0, 'host_submission_complete': False}
        with self.store.worker_lock():
            rows = [r for r in self.store.records() if r['state'] == 'pending'][:max_jobs]
            if not rows: return result
            self._verify_identity(self.transport.call('GET', '/api/v1/memory/whoami', deadline=deadline))
            for row in rows:
                try:
                    if perf_counter() >= deadline: raise OutboxError('outbox recovery deadline exceeded')
                    payload = row['payload']
                    accepted = self.transport.call('POST', '/api/v1/memory/entries', payload, deadline=deadline)
                    job, item = accepted.get('job_id'), accepted.get('source_item_id')
                    if not all(isinstance(v, str) and re.fullmatch(UUID, v) for v in (job, item)):
                        raise OutboxError('outbox acceptance pointers invalid')
                    receipt = self.transport.call('GET', f'/api/v1/memory/jobs/{job}/checkpoint-receipt', deadline=deadline)
                    receipt = canonical_receipt(payload, receipt, accepted)
                    if perf_counter() >= deadline: raise OutboxError('outbox recovery deadline exceeded')
                    self.store.mark_delivered(row, receipt)
                    result['remote_durable'] += 1
                except Exception:
                    # First unresolved predecessor stops FIFO. Never save raw error.
                    result['failed'] += 1
                    break
        return result


    def _verify_identity(self,identity):
        self.validate_identity(self.store.binding,identity)

    @staticmethod
    def validate_identity(binding,identity):
        validate_binding(binding)
        validate_tenant(identity.get('tenant_id'))
        credential=binding['credential']
        observed_id=identity.get('credential_id')
        if credential['mode']=='mcp_oauth' and observed_id is None:
            observed_id=identity.get('mcp_client_id')  # Existing server-owned field, not a scope/header fallback.
        if (identity.get('tenant_id')!=binding['tenant_id'] or identity.get('auth_mode')!=credential['mode']
                or observed_id!=credential['id']):
            raise OutboxError('outbox fresh credential identity differs')
        if credential['mode']=='mcp_oauth':
            if identity.get('mcp_client_id')!=credential['id'] or identity.get('mcp_client_key')!=credential['client_key']:
                raise OutboxError('outbox registered OAuth principal differs')
            if binding['scope']['type']=='agent' and identity.get('agent_scope_key')!=binding['scope']['key']:
                raise OutboxError('outbox OAuth agent scope differs')
        scopes=identity.get('allowed_scopes')
        if not isinstance(scopes,list) or not {'read','write','write:'+binding['scope']['type']}.issubset(scopes):
            raise OutboxError('outbox authenticated scope grant insufficient')


class RestTransport:
    """Finite HTTPS subprocess requests reuse the approved plugin transport only."""
    def __init__(self, binding, api_key=None, *, credentials=None, ca_file=None):
        self._binding_text = canonical(validate_binding(binding))
        if __package__:
            from .credentials import ApiKeyCredentials
        else:
            from credentials import ApiKeyCredentials
        self._credentials=credentials if credentials is not None else ApiKeyCredentials(self.binding,api_key)
        if self._credentials.binding!=self.binding:
            raise OutboxError('outbox credential source mode/origin binding differs')
        self._authorized_headers=None
        self.ca_file=ca_file

    @property
    def binding(self):
        return json.loads(self._binding_text)

    def call(self, method, path, payload=None, *, deadline):
        if __package__:
            from .checkpoint import CheckpointMixin, CheckpointSnapshot
        else:
            # Finite external CLI imports only adjacent own-plugin modules.
            from checkpoint import CheckpointMixin, CheckpointSnapshot
        if not (method == 'GET' and (path == '/api/v1/memory/whoami' or re.fullmatch('/api/v1/memory/jobs/' + UUID + '/checkpoint-receipt', path))
                or method == 'POST' and path == '/api/v1/memory/entries'):
            raise OutboxError('outbox REST action invalid')
        scope = self.binding['scope']
        headers=tuple(self._credentials.headers(deadline).items())
        headers+=(('X-MCP-Scopes',','.join(('read','write','write:'+scope['type']))),)
        if scope['type'] == 'agent': headers += (('X-Palace-Agent-Scope-Key', scope['key']),)
        snapshot = CheckpointSnapshot('', self.binding['tenant_id'], scope['type'], scope.get('key'),
                                      self.binding['origin'], headers, 5.0, self.ca_file)
        try:
            transport=CheckpointMixin()
            if path!='/api/v1/memory/whoami' and self._authorized_headers!=headers:
                identity=transport._checkpoint_request_json('GET','/api/v1/memory/whoami',snapshot=snapshot,deadline=deadline)
                RecoveryWorker.validate_identity(self.binding,identity)
                self._authorized_headers=headers
            result=transport._checkpoint_request_json(method,path,payload,snapshot=snapshot,deadline=deadline)
            if path=='/api/v1/memory/whoami':
                RecoveryWorker.validate_identity(self.binding,result)
                self._authorized_headers=headers
            return result
        except Exception:
            raise OutboxError('outbox bounded authenticated request failed') from None
