#!/usr/bin/env python3
"""Preview or ingest local Codex memory files into Palace.

By default this tool does not write. Use the sweep command with --write to
submit normalized memory entries to Palace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
DEFAULT_MEMORY_ROOT = Path(os.getenv("CODEX_MEMORY_ROOT", str(Path.home() / ".codex" / "memories")))
DEFAULT_ROLLOUT_GLOB = "rollout_summaries/*.md"
DEFAULT_MCP_URL = "http://localhost:8765/mcp"

sys.path.insert(0, str(BACKEND_ROOT))

from app.services.pid_lock import pid_file_lock  # noqa: E402


def _scope(args: argparse.Namespace) -> Any:
    payload = {"type": args.scope_type, "key": args.scope_key}
    try:
        from app.schemas.memory import MemoryScope  # type: ignore[attr-defined]  # noqa: E402
    except (ImportError, SystemError):
        return payload
    return MemoryScope.model_validate(payload)


def _die_json(message: str, *, code: str = "error", exit_code: int = 1) -> NoReturn:
    print(json.dumps({"ok": False, "error": {"code": code, "message": message}}, indent=2, sort_keys=True))
    raise SystemExit(exit_code)


def _load_services() -> dict[str, Any]:
    try:
        from app.services import codex_memory_import  # type: ignore[attr-defined]  # noqa: E402
    except ModuleNotFoundError:
        _die_json(
            "Codex memory service module is not available. Expected app.services.codex_memory_import.",
            code="missing_service_module",
            exit_code=2,
        )
    except SystemError as exc:
        _die_json(str(exc), code="service_import_error", exit_code=2)
    normalize = getattr(codex_memory_import, "normalize_codex_memory_files", None)
    dry_run_json = getattr(codex_memory_import, "codex_memory_result_to_dry_run_json", None)
    return {
        "build_codex_memory_entries": codex_memory_import.build_codex_memory_entries,
        "codex_memory_result_to_dry_run_json": dry_run_json,
        "normalize_codex_memory_files": normalize,
    }


def _call_with_supported_kwargs(func: Callable[..., Any], **kwargs: Any) -> Any:
    """Call worker-owned service functions while tolerating final naming drift."""
    try:
        return func(**kwargs)
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
    import inspect

    signature = inspect.signature(func)
    supported = {name: value for name, value in kwargs.items() if name in signature.parameters}
    return func(**supported)


def _report_to_json(report: Any, *, include_records: bool = False, include_body: bool = False) -> dict[str, Any]:
    if hasattr(report, "to_json"):
        try:
            return report.to_json(include_records=include_records, include_body=include_body)
        except TypeError:
            try:
                return report.to_json(include_records=include_records)
            except TypeError:
                return report.to_json()
    if isinstance(report, dict):
        return report
    return {"ok": True, "report": str(report)}


def _dry_run_payload(func: Callable[..., Any], result: Any, *, include_body: bool) -> dict[str, Any]:
    try:
        return func(result, include_body=include_body)
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
    return func(result)


def _paths(args: argparse.Namespace) -> list[Path]:
    return args.paths or [DEFAULT_MEMORY_ROOT]


def _source_paths(
    roots: list[Path],
    *,
    glob_pattern: str,
    include_rollout_summaries: bool,
) -> tuple[list[Path], list[Path], list[Path]]:
    memory_md: list[Path] = []
    memory_summaries: list[Path] = []
    rollout_summaries: list[Path] = []
    seen: set[Path] = set()

    def add(target: list[Path], path: Path) -> None:
        try:
            key = path.expanduser().resolve(strict=False)
        except OSError:
            key = path.expanduser()
        if key in seen:
            return
        seen.add(key)
        target.append(path)

    for raw_root in roots:
        root = raw_root.expanduser()
        if root.is_dir():
            add(memory_md, root / "MEMORY.md")
            add(memory_summaries, root / "memory_summary.md")
            add(memory_summaries, root / "raw_memories.md")
            if include_rollout_summaries:
                pattern = "*.md" if root.name == "rollout_summaries" else glob_pattern
                for rollout_path in sorted(root.glob(pattern)):
                    add(rollout_summaries, rollout_path)
            continue
        name = root.name.lower()
        if name == "memory.md":
            add(memory_md, root)
        elif name == "memory_summary.md":
            add(memory_summaries, root)
        else:
            add(rollout_summaries, root)
    return memory_md, memory_summaries, rollout_summaries


def _combined_result(args: argparse.Namespace, services: dict[str, Any]) -> dict[str, Any]:
    normalize = services.get("normalize_codex_memory_files")
    if normalize is not None:
        result = _call_with_supported_kwargs(
            normalize,
            paths=_paths(args),
            roots=_paths(args),
            tenant_id=args.tenant_id,
            scope=_scope(args),
            tags=args.tag,
            relationship_policy=args.relationship_policy,
            max_body_chars=args.max_body_chars,
            glob_pattern=getattr(args, "glob", DEFAULT_ROLLOUT_GLOB),
            rollout_glob=getattr(args, "rollout_glob", getattr(args, "glob", DEFAULT_ROLLOUT_GLOB)),
            include_rollout_summaries=getattr(args, "include_rollout_summaries", False),
        )
        return {"service_result": result}

    memory_md, memory_summaries, rollout_summaries = _source_paths(
        _paths(args),
        glob_pattern=getattr(args, "rollout_glob", getattr(args, "glob", DEFAULT_ROLLOUT_GLOB)),
        include_rollout_summaries=getattr(args, "include_rollout_summaries", False),
    )
    entries: list[Any] = []
    records: list[Any] = []
    warnings: list[Any] = []
    for path in memory_md:
        result = services["build_codex_memory_entries"](
            tenant_id=args.tenant_id,
            memory_md_path=path,
            scope=_scope(args),
            max_body_chars=args.max_body_chars,
            relationship_policy=args.relationship_policy,
        )
        entries.extend(result.entries)
        records.extend(result.records)
        warnings.extend(result.warnings)
    for path in memory_summaries:
        result = services["build_codex_memory_entries"](
            tenant_id=args.tenant_id,
            memory_summary_path=path,
            scope=_scope(args),
            max_body_chars=args.max_body_chars,
            relationship_policy=args.relationship_policy,
        )
        entries.extend(result.entries)
        records.extend(result.records)
        warnings.extend(result.warnings)
    if rollout_summaries:
        result = services["build_codex_memory_entries"](
            tenant_id=args.tenant_id,
            rollout_summary_paths=rollout_summaries,
            scope=_scope(args),
            max_body_chars=args.max_body_chars,
            relationship_policy=args.relationship_policy,
        )
        entries.extend(result.entries)
        records.extend(result.records)
        warnings.extend(result.warnings)
    if args.tag:
        entries = [_append_tags(entry, args.tag) for entry in entries]
    return {
        "entries": entries,
        "records": records,
        "warnings": warnings,
        "source_paths": {
            "memory_md": [str(path) for path in memory_md],
            "memory_summary": [str(path) for path in memory_summaries],
            "rollout_summary": [str(path) for path in rollout_summaries],
        },
    }


def _append_tags(entry: Any, tags: list[str]) -> Any:
    current_tags = list(getattr(entry, "tags", []))
    merged = [*current_tags, *[tag for tag in tags if tag not in current_tags]]
    if hasattr(entry, "model_copy"):
        return entry.model_copy(update={"tags": merged})
    if isinstance(entry, dict):
        return {**entry, "tags": merged}
    return entry


def _entry_json(entry: Any, *, include_body: bool) -> dict[str, Any]:
    if hasattr(entry, "model_dump"):
        payload = entry.model_dump(mode="json")
    elif isinstance(entry, dict):
        payload = dict(entry)
    else:
        payload = dict(entry.__dict__)
    body = payload.get("body")
    if not include_body and isinstance(body, str):
        payload["body"] = f"<redacted:{len(body)} chars>"
    return payload


def _warning_json(warning: Any) -> dict[str, Any]:
    if hasattr(warning, "model_dump"):
        return warning.model_dump(mode="json")
    return dict(warning.__dict__)


def _manual_payload(result: dict[str, Any], *, include_body: bool, dry_run: bool, write_report: dict[str, Any] | None = None) -> dict[str, Any]:
    entries = result["entries"]
    records = result["records"]
    payload: dict[str, Any] = {
        "dry_run": dry_run,
        "would_write": not dry_run,
        "path_count": sum(len(paths) for paths in result["source_paths"].values()),
        "record_count": len(entries),
        "warning_count": len(result["warnings"]),
        "low_signal_count": 0,
        "low_signal_ratio": 0.0,
        "signal_quality": {
            "total_bullets": len(entries),
            "retained_count": len(entries),
            "skipped_low_signal_count": 0,
            "low_signal_ratio": 0.0,
            "by_quality": {},
        },
        "source_paths": result["source_paths"],
        "records": [],
        "warnings": [_warning_json(warning) for warning in result["warnings"]],
    }
    for index, entry in enumerate(entries):
        record = records[index] if index < len(records) else None
        record_payload = {
            "source_file": str(getattr(record, "source_file", "")),
            "line_number": getattr(record, "start_line", None),
            "source_kind": getattr(record, "source_kind", None),
            "idempotency_key": getattr(entry, "idempotency_key", None),
            "memory_entry": _entry_json(entry, include_body=include_body),
        }
        payload["records"].append(record_payload)
    if write_report:
        payload.update(write_report)
    return payload


def _source_bucket(path: str) -> str:
    normalized = path.replace("\\", "/")
    name = Path(path).name
    if name == "MEMORY.md":
        return "memory_md"
    if name == "memory_summary.md":
        return "memory_summary"
    if name == "raw_memories.md":
        return "raw_memories"
    if "/rollout_summaries/" in normalized:
        return "rollout_summary"
    return "other"


def _attach_import_controls(payload: dict[str, Any], args: argparse.Namespace) -> None:
    payload["include_rollout_summaries"] = bool(getattr(args, "include_rollout_summaries", False))
    payload["rollout_glob"] = getattr(args, "rollout_glob", getattr(args, "glob", DEFAULT_ROLLOUT_GLOB))
    source_counts = {"memory_md": 0, "memory_summary": 0, "raw_memories": 0, "rollout_summary": 0, "other": 0}
    skipped_source_counts = dict(source_counts)
    for record in payload.get("records", []) or []:
        if isinstance(record, dict):
            source_counts[_source_bucket(str(record.get("source_file", "")))] += 1
    for record in payload.get("skipped_records", []) or []:
        if isinstance(record, dict):
            skipped_source_counts[_source_bucket(str(record.get("source_file", "")))] += 1
    payload["source_counts"] = {key: value for key, value in source_counts.items() if value}
    payload["skipped_source_counts"] = {key: value for key, value in skipped_source_counts.items() if value}
    failures: list[str] = []
    if payload.get("record_count", 0) <= 0:
        failures.append("no_records_found")
    if payload.get("low_signal_count", 0):
        failures.append("low_signal_entries_skipped")
    payload["quality_gate"] = {
        "passed": not failures,
        "failures": failures,
        "checks": {
            "records_found": payload.get("record_count", 0) > 0,
            "low_signal_entries_absent": payload.get("low_signal_count", 0) == 0,
            "rollout_summaries_intentionally_selected": bool(getattr(args, "include_rollout_summaries", False))
            or source_counts["rollout_summary"] == 0,
            "body_redaction_default": not bool(getattr(args, "include_body", False)),
        },
    }


def _isoformat_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _source_file_stats(paths: list[Path], payload: dict[str, Any]) -> dict[str, Any]:
    records_by_source: dict[str, int] = {}
    skipped_by_source: dict[str, int] = {}
    for record in payload.get("records", []) or []:
        if isinstance(record, dict):
            source_file = str(record.get("source_file") or "")
            if source_file:
                records_by_source[source_file] = records_by_source.get(source_file, 0) + 1
    for record in payload.get("skipped_records", []) or []:
        if isinstance(record, dict):
            source_file = str(record.get("source_file") or "")
            if source_file:
                skipped_by_source[source_file] = skipped_by_source.get(source_file, 0) + 1

    files: list[dict[str, Any]] = []
    latest_mtime: datetime | None = None
    for path in paths:
        expanded = path.expanduser()
        try:
            resolved = expanded.resolve(strict=False)
        except OSError:
            resolved = expanded
        if not expanded.exists():
            continue
        stat = expanded.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if latest_mtime is None or mtime > latest_mtime:
            latest_mtime = mtime
        files.append(
            {
                "path": str(resolved),
                "exists": True,
                "size_bytes": stat.st_size,
                "mtime": _isoformat_utc(mtime),
                "record_count": records_by_source.get(str(resolved), 0),
                "skipped_count": skipped_by_source.get(str(resolved), 0),
            }
        )
    return {
        "latest_mtime": _isoformat_utc(latest_mtime),
        "files": files,
    }


def _local_freshness_payload(args: argparse.Namespace, services: dict[str, Any]) -> dict[str, Any]:
    result = _combined_result(args, services)
    if "service_result" in result and services["codex_memory_result_to_dry_run_json"] is not None:
        payload = _dry_run_payload(
            services["codex_memory_result_to_dry_run_json"],
            result["service_result"],
            include_body=False,
        )
    elif "service_result" in result:
        payload = _report_to_json(result["service_result"], include_records=True, include_body=False)
    else:
        payload = _manual_payload(result, include_body=False, dry_run=True)
    _attach_import_controls(payload, args)
    memory_md, memory_summaries, rollout_summaries = _source_paths(
        _paths(args),
        glob_pattern=getattr(args, "rollout_glob", getattr(args, "glob", DEFAULT_ROLLOUT_GLOB)),
        include_rollout_summaries=getattr(args, "include_rollout_summaries", False),
    )
    source_files = _source_file_stats([*memory_md, *memory_summaries, *rollout_summaries], payload)
    return {
        "path_count": payload.get("path_count", 0),
        "record_count": payload.get("record_count", 0),
        "warning_count": payload.get("warning_count", 0),
        "low_signal_count": payload.get("low_signal_count", 0),
        "source_counts": payload.get("source_counts", {}),
        "skipped_source_counts": payload.get("skipped_source_counts", {}),
        "quality_gate": payload.get("quality_gate", {}),
        "signal_quality": payload.get("signal_quality", {}),
        "latest_source_mtime": source_files["latest_mtime"],
        "source_files": source_files["files"],
    }


def _palace_freshness_from_listing(listing: dict[str, Any], *, scope_type: str, scope_key: str | None, tags: list[str]) -> dict[str, Any]:
    entries = listing.get("entries") if isinstance(listing, dict) else None
    if not isinstance(entries, list):
        entries = []

    latest_entry: dict[str, Any] | None = None
    latest_at: datetime | None = None
    source_counts: dict[str, int] = {}
    readiness_counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
        readiness = str(entry.get("readiness_state") or "unknown")
        readiness_counts[readiness] = readiness_counts.get(readiness, 0) + 1
        candidate = _parse_datetime(entry.get("updated_at")) or _parse_datetime(entry.get("created_at"))
        if candidate is not None and (latest_at is None or candidate > latest_at):
            latest_at = candidate
            latest_entry = entry

    return {
        "scope": {"type": scope_type, "key": scope_key} if scope_type != "tenant_shared" else {"type": scope_type},
        "tags": tags,
        "total": listing.get("total") if isinstance(listing, dict) else None,
        "returned_count": len(entries),
        "limit": listing.get("limit") if isinstance(listing, dict) else None,
        "latest_import_at": _isoformat_utc(latest_at),
        "latest_entry": {
            "title": latest_entry.get("title"),
            "source": latest_entry.get("source"),
            "source_url": latest_entry.get("source_url"),
            "created_at": latest_entry.get("created_at"),
            "updated_at": latest_entry.get("updated_at"),
            "readiness_state": latest_entry.get("readiness_state"),
            "tags": latest_entry.get("tags"),
        }
        if latest_entry
        else None,
        "source_counts": source_counts,
        "readiness_counts": readiness_counts,
    }


async def _fetch_palace_freshness_async(args: argparse.Namespace) -> dict[str, Any]:
    headers = _parse_headers(args.header)
    async with _StreamableMcpClient(
        url=args.mcp_url,
        headers=headers,
        timeout_seconds=args.timeout_seconds,
        sse_read_timeout_seconds=args.sse_read_timeout_seconds,
    ) as client:
        whoami = await client.call_tool("whoami")
        scope_key = None if args.scope_type == "tenant_shared" else args.scope_key
        listing = await client.call_tool(
            "list_memory_entries",
            {
                "scope_type": args.scope_type,
                "scope_key": scope_key,
                "tags": args.palace_tag,
                "tags_mode": args.palace_tags_mode,
                "limit": args.palace_limit,
            },
        )
    payload = _palace_freshness_from_listing(
        listing if isinstance(listing, dict) else {},
        scope_type=args.scope_type,
        scope_key=None if args.scope_type == "tenant_shared" else args.scope_key,
        tags=args.palace_tag,
    )
    payload["tenant_id"] = whoami.get("tenant_id") if isinstance(whoami, dict) else None
    payload["transport"] = "mcp-http"
    payload["mcp_url"] = args.mcp_url
    return payload


def _freshness_status(local: dict[str, Any], palace: dict[str, Any] | None) -> dict[str, Any]:
    local_latest = _parse_datetime(local.get("latest_source_mtime"))
    palace_latest = _parse_datetime(palace.get("latest_import_at")) if palace else None
    if local_latest is None:
        return {"state": "unknown", "reason": "no_local_source_mtime"}
    if palace is None:
        return {"state": "local_only", "reason": "palace_check_not_requested"}
    if palace.get("error"):
        return {"state": "unknown", "reason": "palace_check_failed"}
    if palace_latest is None:
        return {"state": "stale", "reason": "no_palace_imports_found"}
    lag_seconds = int((local_latest - palace_latest).total_seconds())
    if lag_seconds > 0:
        return {"state": "stale", "reason": "local_memory_newer_than_palace", "lag_seconds": lag_seconds}
    return {"state": "fresh", "reason": "palace_import_is_at_least_as_new_as_local", "lag_seconds": lag_seconds}


def _enforce_quality_gate(payload: dict[str, Any], args: argparse.Namespace) -> None:
    if not getattr(args, "write", False):
        return
    failures = (payload.get("quality_gate") or {}).get("failures") if isinstance(payload.get("quality_gate"), dict) else None
    if getattr(args, "allow_low_signal", False) and failures == ["low_signal_entries_skipped"]:
        return
    if failures:
        raise ValueError(
            "Codex memory import quality gate failed: "
            + ", ".join(str(failure) for failure in failures)
            + ". Review the dry-run output or pass --allow-low-signal to write retained records anyway."
        )


@contextmanager
def _pid_lock(lock_path: Path | None) -> Iterator[None]:
    with pid_file_lock(lock_path, name="Codex memory import"):
        yield


def _write_entries(entries: list[Any], args: argparse.Namespace) -> dict[str, Any]:
    api_base_url = args.api_base_url or os.getenv("PALACEOFTRUTH_API_BASE_URL") or os.getenv("PALACEOFTRUTH_BASE_URL")
    api_key = args.api_key or os.getenv("PALACEOFTRUTH_API_KEY") or os.getenv("API_KEY")
    if not api_base_url:
        raise ValueError("PALACEOFTRUTH_API_BASE_URL is required when --write is set")
    if not api_key:
        raise ValueError("PALACEOFTRUTH_API_KEY is required when --write is set")
    import httpx

    writes: list[dict[str, Any]] = []
    write_errors: list[dict[str, Any]] = []
    endpoint = f"{api_base_url.rstrip('/')}/api/v1/memory/entries"
    with httpx.Client(timeout=args.timeout_seconds) as client:
        for entry in entries:
            payload = _entry_json(entry, include_body=True)
            try:
                response = client.post(
                    endpoint,
                    headers={"X-API-Key": api_key, **_memory_entry_scope_headers(payload)},
                    json=payload,
                )
                response.raise_for_status()
                response_payload = response.json()
                writes.append(
                    {
                        "idempotency_key": payload.get("idempotency_key"),
                        "status_code": response.status_code,
                        "status": response_payload.get("status") if isinstance(response_payload, dict) else None,
                        "job_id": str(response_payload.get("job_id"))
                        if isinstance(response_payload, dict) and response_payload.get("job_id")
                        else None,
                        "accepted_as": response_payload.get("accepted_as") if isinstance(response_payload, dict) else None,
                    }
                )
            except (httpx.HTTPError, ValueError) as exc:
                write_errors.append({"idempotency_key": payload.get("idempotency_key"), "error": str(exc)})
    return {
        "write_count": len(writes),
        "write_error_count": len(write_errors),
        "writes": writes,
        "write_errors": write_errors,
    }


def _memory_entry_scope_headers(payload: dict[str, Any]) -> dict[str, str]:
    scopes = ["write"]
    scope = payload.get("scope")
    if isinstance(scope, dict):
        scope_type = scope.get("type")
        if scope_type in {"agent", "workspace", "session"}:
            scopes.append(f"write:{scope_type}")
    return {"X-MCP-Scope": "write", "X-MCP-Scopes": ",".join(scopes)}


def _parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_value in values:
        if ":" in raw_value:
            name, value = raw_value.split(":", 1)
        elif "=" in raw_value:
            name, value = raw_value.split("=", 1)
        else:
            raise ValueError(f"MCP header must be NAME=VALUE or NAME:VALUE, got {raw_value!r}")
        name = name.strip()
        if not name:
            raise ValueError("MCP header name cannot be empty")
        headers[name] = value.strip()
    return headers


def _mcp_result_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _mcp_result_to_payload(name: str, result: Any) -> Any:
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool {name} failed: {_mcp_result_text(result)}")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    text = _mcp_result_text(result)
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return text


class _StreamableMcpClient:
    def __init__(self, *, url: str, headers: dict[str, str], timeout_seconds: float, sse_read_timeout_seconds: float):
        self.url = url
        self.headers = headers
        self.timeout_seconds = timeout_seconds
        self.sse_read_timeout_seconds = sse_read_timeout_seconds

    async def __aenter__(self) -> "_StreamableMcpClient":
        try:
            from mcp import ClientSession  # type: ignore[import-not-found]
            from mcp.client.streamable_http import streamablehttp_client  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "MCP import requires the Python 'mcp' package. Run from backend with "
                "`uv run python ../scripts/import_codex_memory_to_palace.py mcp-http ...` "
                "or install backend dependencies."
            ) from exc

        self._stack = AsyncExitStack()
        read_stream, write_stream, _ = await self._stack.enter_async_context(
            streamablehttp_client(
                self.url,
                headers=self.headers or None,
                timeout=self.timeout_seconds,
                sse_read_timeout=self.sse_read_timeout_seconds,
            )
        )
        session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        self._session = session
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self._stack.aclose()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = await self._session.call_tool(name, arguments or {})
        return _mcp_result_to_payload(name, result)


def _entry_to_mcp_arguments(entry: Any) -> dict[str, Any]:
    if hasattr(entry, "model_dump"):
        payload = entry.model_dump(mode="json")
    elif isinstance(entry, dict):
        payload = dict(entry)
    else:
        payload = dict(entry.__dict__)
    scope = payload.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("type"), str):
        raise ValueError("memory entry does not include a valid MCP scope")
    arguments = {
        "title": payload["title"],
        "body": payload["body"],
        "source": payload.get("source", "codex_memory"),
        "created_at": payload.get("created_at"),
        "summary": payload.get("summary"),
        "tags": payload.get("tags") or [],
        "scope_type": scope["type"],
        "scope_key": scope.get("key"),
        "source_url": payload.get("source_url"),
        "created_by_role": payload.get("created_by_role"),
        "metadata": payload.get("metadata"),
        "idempotency_key": payload.get("idempotency_key"),
        "webhook_url": payload.get("webhook_url"),
        "enable_ai_enrichment": bool(payload.get("enable_ai_enrichment")),
        "relationship_policy": payload.get("relationship_policy", "deferred"),
    }
    return {key: value for key, value in arguments.items() if value is not None}


async def _write_entries_with_mcp_client(entries: list[Any], client: Any) -> dict[str, Any]:
    writes: list[dict[str, Any]] = []
    write_errors: list[dict[str, str]] = []
    async with client:
        whoami = await client.call_tool("whoami")
        tenant_id = whoami.get("tenant_id") if isinstance(whoami, dict) else None
        for entry in entries:
            arguments = _entry_to_mcp_arguments(entry)
            try:
                accepted = await client.call_tool("create_memory_entry", arguments)
                writes.append(
                    {
                        "idempotency_key": arguments.get("idempotency_key"),
                        "status": accepted.get("status") if isinstance(accepted, dict) else None,
                        "job_id": str(accepted.get("job_id"))
                        if isinstance(accepted, dict) and accepted.get("job_id")
                        else None,
                        "accepted_as": accepted.get("accepted_as") if isinstance(accepted, dict) else None,
                    }
                )
            except (RuntimeError, ValueError) as exc:
                write_errors.append({"idempotency_key": str(arguments.get("idempotency_key")), "error": str(exc)})
    return {
        "transport": "mcp-http",
        "tenant_id": tenant_id,
        "write_count": len(writes),
        "write_error_count": len(write_errors),
        "writes": writes,
        "write_errors": write_errors,
    }


async def _write_entries_mcp_http_async(entries: list[Any], args: argparse.Namespace) -> dict[str, Any]:
    headers = _parse_headers(args.header)
    return await _write_entries_with_mcp_client(
        entries,
        _StreamableMcpClient(
            url=args.mcp_url,
            headers=headers,
            timeout_seconds=args.timeout_seconds,
            sse_read_timeout_seconds=args.sse_read_timeout_seconds,
        ),
    )


def _records_from_combined_result(result: dict[str, Any]) -> list[Any]:
    if "service_result" in result:
        return [record.entry for record in getattr(result["service_result"], "records", [])]
    return result["entries"]


def cmd_dry_run(args: argparse.Namespace) -> int:
    services = _load_services()
    try:
        result = _combined_result(args, services)
        if "service_result" in result and services["codex_memory_result_to_dry_run_json"] is not None:
            payload = _dry_run_payload(
                services["codex_memory_result_to_dry_run_json"],
                result["service_result"],
                include_body=args.include_body,
            )
        elif "service_result" in result:
            payload = _report_to_json(result["service_result"], include_records=True, include_body=args.include_body)
        else:
            payload = _manual_payload(result, include_body=args.include_body, dry_run=True)
        _attach_import_controls(payload, args)
    except (RuntimeError, ValueError) as exc:
        _die_json(str(exc))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("record_count", 0) else 1


def cmd_sweep(args: argparse.Namespace) -> int:
    services = _load_services()
    try:
        with _pid_lock(args.lock_file):
            result = _combined_result(args, services)
            if "service_result" in result:
                if services["codex_memory_result_to_dry_run_json"] is not None:
                    payload = _dry_run_payload(
                        services["codex_memory_result_to_dry_run_json"],
                        result["service_result"],
                        include_body=args.include_body,
                    )
                else:
                    payload = _report_to_json(
                        result["service_result"],
                        include_records=args.include_records,
                        include_body=args.include_body,
                    )
                payload["dry_run"] = not args.write
                payload["would_write"] = args.write
                _attach_import_controls(payload, args)
                _enforce_quality_gate(payload, args)
                write_report = None
                if args.write:
                    entries = [record.entry for record in getattr(result["service_result"], "records", [])]
                    write_report = _write_entries(entries, args) if entries else None
                if write_report:
                    payload.update(write_report)
            else:
                payload = _manual_payload(
                    result,
                    include_body=args.include_body,
                    dry_run=not args.write,
                )
                _attach_import_controls(payload, args)
                _enforce_quality_gate(payload, args)
                write_report = _write_entries(result["entries"], args) if args.write and result["entries"] else None
                if write_report:
                    payload.update(write_report)
    except (RuntimeError, ValueError) as exc:
        _die_json(str(exc))
    print(json.dumps(payload, indent=2, sort_keys=True))
    if payload.get("write_error_count", 0):
        return 2
    return 0 if payload.get("record_count", 0) else 1


def cmd_mcp_http(args: argparse.Namespace) -> int:
    services = _load_services()
    try:
        with _pid_lock(args.lock_file):
            result = _combined_result(args, services)
            if "service_result" in result and services["codex_memory_result_to_dry_run_json"] is not None:
                payload = _dry_run_payload(
                    services["codex_memory_result_to_dry_run_json"],
                    result["service_result"],
                    include_body=args.include_body,
                )
            elif "service_result" in result:
                payload = _report_to_json(
                    result["service_result"],
                    include_records=args.include_records,
                    include_body=args.include_body,
                )
            else:
                payload = _manual_payload(result, include_body=args.include_body, dry_run=True)
            entries = _records_from_combined_result(result)
            payload["dry_run"] = not args.write
            payload["would_write"] = args.write
            payload["transport"] = "mcp-http"
            payload["mcp_url"] = args.mcp_url
            _attach_import_controls(payload, args)
            _enforce_quality_gate(payload, args)
            if args.write and entries:
                payload.update(asyncio.run(_write_entries_mcp_http_async(entries, args)))
    except (RuntimeError, ValueError) as exc:
        _die_json(str(exc))
    print(json.dumps(payload, indent=2, sort_keys=True))
    if payload.get("write_error_count", 0):
        return 2
    return 0 if payload.get("record_count", 0) else 1


def cmd_freshness_report(args: argparse.Namespace) -> int:
    services = _load_services()
    try:
        local = _local_freshness_payload(args, services)
        palace: dict[str, Any] | None = None
        if not args.local_only:
            try:
                palace = asyncio.run(_fetch_palace_freshness_async(args))
            except (RuntimeError, ValueError) as exc:
                palace = {
                    "transport": "mcp-http",
                    "mcp_url": args.mcp_url,
                    "error": {"code": "palace_freshness_check_failed", "message": str(exc)},
                }
        payload = {
            "report": "codex-memory-freshness",
            "dry_run": True,
            "would_write": False,
            "local": local,
            "palace": palace,
            "freshness": _freshness_status(local, palace),
            "operator_decision": {
                "default": "import_when_stale",
                "write_path": "Run `mcp-http --write` only after reviewing this redacted report.",
                "audit_only_alternative": (
                    "If local Codex memory remains audit-only, use palace_remember or capture_checkpoint "
                    "for future durable Palace write-back instead of bulk importing raw transcripts."
                ),
            },
        }
    except (RuntimeError, ValueError) as exc:
        _die_json(str(exc))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["freshness"]["state"] in {"fresh", "stale", "local_only"} else 1


def add_common_args(parser: argparse.ArgumentParser, *, paths_nargs: str) -> None:
    parser.add_argument("paths", nargs=paths_nargs, type=Path, help=f"Memory roots or files. Defaults to {DEFAULT_MEMORY_ROOT}.")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--scope-type", default="agent", choices=("session", "agent", "workspace", "tenant_shared"))
    parser.add_argument("--scope-key", default="codex")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--relationship-policy", choices=("immediate", "deferred", "skip"), default="deferred")
    parser.add_argument("--include-body", action="store_true", help="Include memory bodies in JSON output.")
    parser.add_argument("--max-body-chars", type=int, default=20_000)
    parser.add_argument("--include-rollout-summaries", action="store_true", help="Include rollout_summaries files discovered under memory roots.")
    parser.add_argument("--rollout-glob", default=DEFAULT_ROLLOUT_GLOB, help="Glob used under memory roots when --include-rollout-summaries is set.")
    parser.add_argument(
        "--glob",
        dest="rollout_glob",
        default=argparse.SUPPRESS,
        help="Backward-compatible alias for --rollout-glob.",
    )


def add_write_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--write", action="store_true", help="Submit normalized records to Palace memory entries.")
    parser.add_argument(
        "--allow-low-signal",
        action="store_true",
        help="Allow writes to proceed when the dry-run reports skipped low-signal entries. Skipped entries are still not written.",
    )
    parser.add_argument("--api-base-url", help="Palace API base URL. Defaults to PALACEOFTRUTH_API_BASE_URL.")
    parser.add_argument("--api-key", help="Palace API key. Defaults to PALACEOFTRUTH_API_KEY.")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--lock-file", type=Path, help="PID lock path used to avoid concurrent sweeper writes.")


def add_mcp_http_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--write", action="store_true", help="Submit normalized records through create_memory_entry.")
    parser.add_argument(
        "--allow-low-signal",
        action="store_true",
        help="Allow writes to proceed when the dry-run reports skipped low-signal entries. Skipped entries are still not written.",
    )
    parser.add_argument(
        "--mcp-url",
        default=os.getenv("PALACEOFTRUTH_MCP_URL", os.getenv("SECONDBRAIN_MCP_URL", DEFAULT_MCP_URL)),
        help=f"Streamable HTTP MCP endpoint. Defaults to {DEFAULT_MCP_URL}.",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra HTTP header for the MCP endpoint, as NAME=VALUE or NAME:VALUE. Repeatable.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--sse-read-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--lock-file", type=Path, help="PID lock path used to avoid concurrent MCP writes.")


def add_mcp_read_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mcp-url",
        default=os.getenv("PALACEOFTRUTH_MCP_URL", os.getenv("SECONDBRAIN_MCP_URL", DEFAULT_MCP_URL)),
        help=f"Streamable HTTP MCP endpoint. Defaults to {DEFAULT_MCP_URL}.",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra HTTP header for the MCP endpoint, as NAME=VALUE or NAME:VALUE. Repeatable.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--sse-read-timeout-seconds", type=float, default=300.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    dry_run = sub.add_parser("dry-run", help="Normalize Codex memory files without writing to Palace.")
    add_common_args(dry_run, paths_nargs="*")
    dry_run.set_defaults(func=cmd_dry_run)

    sweep = sub.add_parser("sweep", help="Scan Codex memory files and optionally write Palace memory.")
    add_common_args(sweep, paths_nargs="*")
    add_write_args(sweep)
    sweep.add_argument("--include-records", action="store_true", help="Include normalized record summaries in output.")
    sweep.set_defaults(func=cmd_sweep)

    write = sub.add_parser("write", help="Alias for sweep --write.")
    add_common_args(write, paths_nargs="*")
    add_write_args(write)
    write.add_argument("--include-records", action="store_true", help="Include normalized record summaries in output.")
    write.set_defaults(func=cmd_sweep, write=True)

    mcp_http = sub.add_parser(
        "mcp-http",
        help="Normalize Codex memory files and optionally write through streamable HTTP MCP.",
    )
    add_common_args(mcp_http, paths_nargs="*")
    add_mcp_http_args(mcp_http)
    mcp_http.add_argument("--include-records", action="store_true", help="Include normalized record summaries in output.")
    mcp_http.set_defaults(func=cmd_mcp_http)

    freshness = sub.add_parser(
        "freshness-report",
        help="Read-only report comparing local Codex memory freshness with Palace imports.",
    )
    add_common_args(freshness, paths_nargs="*")
    add_mcp_read_args(freshness)
    freshness.set_defaults(func=cmd_freshness_report, write=False, include_records=False)
    freshness.add_argument(
        "--local-only",
        action="store_true",
        help="Skip MCP listing and report only local file mtimes/counts.",
    )
    freshness.add_argument(
        "--palace-tag",
        action="append",
        default=["codex-local-memory"],
        help="Tag filter for Palace list_memory_entries. Repeatable. Defaults to codex-local-memory.",
    )
    freshness.add_argument("--palace-tags-mode", choices=("any", "all"), default="all")
    freshness.add_argument("--palace-limit", type=int, default=10)

    parser.set_defaults(func=cmd_dry_run, paths=[], include_records=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_args = list(argv if argv is not None else sys.argv[1:])
    known_commands = {"dry-run", "sweep", "write", "mcp-http", "freshness-report", "-h", "--help"}
    if raw_args and raw_args[0] not in known_commands:
        raw_args.insert(0, "dry-run")
    args = parser.parse_args(raw_args)
    if args.scope_type == "tenant_shared" and args.scope_key == "codex":
        args.scope_key = None
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
