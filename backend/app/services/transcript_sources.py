from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import ValidationError

from app.schemas.memory import MemoryEntryRequest, MemoryScope, RelationshipExtractionPolicy


TranscriptAdapter = Literal["codex", "claude", "gemini"]

_UTC = timezone.utc
_SENSITIVE_RE = re.compile(
    r"(api[_-]?key|token|secret|password|private key|bearer\s+[a-z0-9._-]+)",
    re.IGNORECASE,
)
_GEMINI_TEXT_RE = re.compile(
    r"^(?:\[(?P<bracket_ts>[^\]]+)\]\s*)?"
    r"(?:(?P<iso_ts>\d{4}-\d{2}-\d{2}[T ][^ ]+)\s+)?"
    r"(?P<role>user|assistant|model|gemini|system|tool)\s*[:>-]\s*(?P<body>.+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TranscriptMemoryRecord:
    adapter: TranscriptAdapter
    source_id: str
    source_file: str
    line_number: int
    role: str
    privacy_classification: str
    entry: MemoryEntryRequest


@dataclass(frozen=True)
class TranscriptWarning:
    source_file: str
    line_number: int
    code: str
    detail: str


@dataclass(frozen=True)
class TranscriptNormalizeResult:
    records: list[TranscriptMemoryRecord] = field(default_factory=list)
    warnings: list[TranscriptWarning] = field(default_factory=list)


def normalize_transcript_files(
    paths: Iterable[Path | str],
    *,
    adapter: TranscriptAdapter,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str] = (),
    relationship_policy: RelationshipExtractionPolicy = "deferred",
    max_body_chars: int = 20_000,
) -> TranscriptNormalizeResult:
    records: list[TranscriptMemoryRecord] = []
    warnings: list[TranscriptWarning] = []
    for raw_path in paths:
        path = Path(raw_path)
        file_result = normalize_transcript_file(
            path,
            adapter=adapter,
            tenant_id=tenant_id,
            scope=scope,
            tags=tags,
            relationship_policy=relationship_policy,
            max_body_chars=max_body_chars,
        )
        records.extend(file_result.records)
        warnings.extend(file_result.warnings)
    return TranscriptNormalizeResult(records=records, warnings=warnings)


def normalize_transcript_file(
    path: Path,
    *,
    adapter: TranscriptAdapter,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str] = (),
    relationship_policy: RelationshipExtractionPolicy = "deferred",
    max_body_chars: int = 20_000,
) -> TranscriptNormalizeResult:
    records: list[TranscriptMemoryRecord] = []
    warnings: list[TranscriptWarning] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                parsed = _parse_line(adapter, line)
                if parsed is None:
                    if line.strip():
                        warnings.append(
                            TranscriptWarning(
                                source_file=str(path),
                                line_number=line_number,
                                code="malformed_record",
                                detail="Line could not be parsed as a supported transcript event.",
                            )
                        )
                    continue
                body = parsed["body"].strip()
                if not body:
                    continue
                if len(body) > max_body_chars:
                    warnings.append(
                        TranscriptWarning(
                            source_file=str(path),
                            line_number=line_number,
                            code="body_too_large",
                            detail=f"Record body has {len(body)} characters; max_body_chars is {max_body_chars}.",
                        )
                    )
                    continue
                try:
                    records.append(
                        _build_record(
                            adapter=adapter,
                            path=path,
                            line_number=line_number,
                            parsed=parsed,
                            tenant_id=tenant_id,
                            scope=scope,
                            tags=tags,
                            relationship_policy=relationship_policy,
                        )
                    )
                except ValidationError as exc:
                    warnings.append(
                        TranscriptWarning(
                            source_file=str(path),
                            line_number=line_number,
                            code="invalid_memory_entry",
                            detail=str(exc),
                        )
                    )
    except OSError as exc:
        warnings.append(
            TranscriptWarning(
                source_file=str(path),
                line_number=0,
                code="file_error",
                detail=str(exc),
            )
        )
    return TranscriptNormalizeResult(records=records, warnings=warnings)


def transcript_result_to_dry_run_json(
    result: TranscriptNormalizeResult,
    *,
    include_body: bool = False,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for record in result.records:
        payload = record.entry.model_dump(mode="json")
        body = payload.get("body")
        if not include_body and isinstance(body, str):
            payload["body"] = f"<redacted:{len(body)} chars>"
        entries.append(
            {
                "adapter": record.adapter,
                "source_id": record.source_id,
                "source_file": record.source_file,
                "line_number": record.line_number,
                "role": record.role,
                "privacy_classification": record.privacy_classification,
                "memory_entry": payload,
            }
        )
    return {
        "dry_run": True,
        "would_write": False,
        "record_count": len(result.records),
        "warning_count": len(result.warnings),
        "records": entries,
        "warnings": [warning.__dict__ for warning in result.warnings],
    }


def _parse_line(adapter: TranscriptAdapter, line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped:
        return None
    parsed_json = _loads_json(stripped)
    if isinstance(parsed_json, dict):
        return _parse_json_event(adapter, parsed_json)
    if adapter == "gemini":
        return _parse_gemini_text_line(stripped)
    return None


def _loads_json(value: str) -> Any | None:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _parse_json_event(adapter: TranscriptAdapter, event: dict[str, Any]) -> dict[str, Any] | None:
    role = _first_text(
        event.get("role"),
        _nested(event, "message", "role"),
        _nested(event, "item", "role"),
        event.get("type") if event.get("type") in {"user", "assistant", "system", "tool"} else None,
    )
    body = _extract_content(
        event.get("content"),
        _nested(event, "message", "content"),
        _nested(event, "item", "content"),
        event.get("text"),
        event.get("message") if isinstance(event.get("message"), str) else None,
    )
    if role is None or body is None:
        return None
    return {
        "role": _normalize_role(adapter, role),
        "body": body,
        "created_at": _parse_datetime(
            _first_text(
                event.get("created_at"),
                event.get("timestamp"),
                event.get("time"),
                _nested(event, "message", "created_at"),
            )
        ),
        "event_type": _first_text(event.get("type"), event.get("event_type")),
        "session_id": _first_text(event.get("session_id"), event.get("sessionId"), event.get("conversation_id")),
        "message_id": _first_text(event.get("id"), event.get("uuid"), event.get("message_id")),
        "cwd": _first_text(event.get("cwd"), event.get("project_dir")),
        "model": _first_text(event.get("model"), _nested(event, "message", "model")),
    }


def _parse_gemini_text_line(line: str) -> dict[str, Any] | None:
    match = _GEMINI_TEXT_RE.match(line)
    if not match:
        return None
    created_at = _parse_datetime(match.group("bracket_ts") or match.group("iso_ts"))
    return {
        "role": _normalize_role("gemini", match.group("role")),
        "body": match.group("body"),
        "created_at": created_at,
        "event_type": "text_log",
        "session_id": None,
        "message_id": None,
        "cwd": None,
        "model": "gemini" if match.group("role").lower() in {"gemini", "model"} else None,
    }


def _build_record(
    *,
    adapter: TranscriptAdapter,
    path: Path,
    line_number: int,
    parsed: dict[str, Any],
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str],
    relationship_policy: RelationshipExtractionPolicy,
) -> TranscriptMemoryRecord:
    role = parsed["role"]
    body = parsed["body"]
    created_at = parsed.get("created_at") or datetime.now(_UTC)
    source_id = _source_id(adapter, path, line_number, parsed)
    privacy_classification = _privacy_classification(body)
    source_file = str(path)
    adapter_tags = [
        "agent-transcript",
        f"transcript-{adapter}",
        f"role-{role}",
        f"privacy-{privacy_classification}",
    ]
    metadata = {
        "transcript_source": {
            "schema_version": 1,
            "adapter": adapter,
            "source_id": source_id,
            "source_file": source_file,
            "line_number": line_number,
            "role": role,
            "privacy_classification": privacy_classification,
            "event_type": parsed.get("event_type"),
            "session_id": parsed.get("session_id"),
            "message_id": parsed.get("message_id"),
            "cwd": parsed.get("cwd"),
            "model": parsed.get("model"),
            "transformation": "verbatim_transcript_record",
        }
    }
    entry = MemoryEntryRequest.model_validate(
        {
            "tenant_id": tenant_id,
            "title": f"{adapter.title()} transcript {role} message",
            "body": body,
            "summary": None,
            "source": f"{adapter}_transcript",
            "created_at": created_at,
            "tags": [*tags, *adapter_tags],
            "scope": scope.model_dump(mode="json"),
            "source_url": _source_url(path, line_number),
            "created_by_role": role,
            "metadata": metadata,
            "idempotency_key": f"transcript:{source_id}",
            "enable_ai_enrichment": False,
            "relationship_policy": relationship_policy,
        }
    )
    return TranscriptMemoryRecord(
        adapter=adapter,
        source_id=source_id,
        source_file=source_file,
        line_number=line_number,
        role=role,
        privacy_classification=privacy_classification,
        entry=entry,
    )


def _source_id(adapter: TranscriptAdapter, path: Path, line_number: int, parsed: dict[str, Any]) -> str:
    stable_event_id = parsed.get("message_id") or f"line:{line_number}"
    identity = {
        "adapter": adapter,
        "source_file": str(path),
        "event_id": stable_event_id,
        "role": parsed.get("role"),
        "created_at": (
            parsed["created_at"].astimezone(_UTC).isoformat()
            if isinstance(parsed.get("created_at"), datetime)
            else None
        ),
        "body_sha256": hashlib.sha256(parsed["body"].encode()).hexdigest(),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"{adapter}-{digest[:32]}"


def _source_url(path: Path, line_number: int) -> str:
    return f"file://{path}#L{line_number}"


def _privacy_classification(body: str) -> str:
    return "sensitive" if _SENSITIVE_RE.search(body) else "internal"


def _normalize_role(adapter: TranscriptAdapter, role: str) -> str:
    normalized = role.strip().lower()
    if adapter == "gemini" and normalized in {"model", "gemini"}:
        return "assistant"
    if normalized in {"human"}:
        return "user"
    return normalized or "unknown"


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _extract_content(*values: Any) -> str | None:
    parts: list[str] = []
    for value in values:
        _collect_text(value, parts)
        if parts:
            break
    if not parts:
        return None
    return "\n".join(part for part in parts if part.strip()).strip() or None


def _collect_text(value: Any, parts: list[str]) -> None:
    if isinstance(value, str):
        if value.strip():
            parts.append(value.strip())
        return
    if isinstance(value, list):
        for item in value:
            _collect_text(item, parts)
        return
    if isinstance(value, dict):
        for key in ("text", "content", "value", "message"):
            if key in value:
                _collect_text(value[key], parts)
                return
