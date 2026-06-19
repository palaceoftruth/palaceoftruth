from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
from pydantic import ValidationError

from app.schemas.memory import MemoryEntryRequest, MemoryScope, RelationshipExtractionPolicy
from app.services.pid_lock import pid_file_lock


DEFAULT_CODEX_MEMORY_GLOBS = ("*.json", "*.jsonl", "*.md")
DEFAULT_API_TIMEOUT_SECONDS = 30.0
DEFAULT_CODEX_MEMORY_SOURCE = "codex_memory"
_UTC = timezone.utc
_THREAD_HEADING_RE = re.compile(r"^## Thread `(?P<thread_id>[^`]+)`\s*$", re.MULTILINE)
_SECTION_METADATA_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")


@dataclass(frozen=True)
class CodexMemoryIngestionConfig:
    api_base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS

    @classmethod
    def from_env(
        cls,
        *,
        api_base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS,
    ) -> "CodexMemoryIngestionConfig":
        return cls(
            api_base_url=api_base_url
            or os.getenv("PALACEOFTRUTH_API_BASE_URL")
            or os.getenv("PALACEOFTRUTH_BASE_URL")
            or os.getenv("SECONDBRAIN_API_BASE_URL")
            or os.getenv("SECONDBRAIN_BASE_URL"),
            api_key=api_key
            or os.getenv("PALACEOFTRUTH_API_KEY")
            or os.getenv("SECONDBRAIN_API_KEY")
            or os.getenv("API_KEY"),
            timeout_seconds=timeout_seconds,
        )

    def validate_for_write(self) -> None:
        if not self.api_base_url:
            raise ValueError("PALACEOFTRUTH_API_BASE_URL is required when write=True")
        if not self.api_key:
            raise ValueError("PALACEOFTRUTH_API_KEY is required when write=True")


@dataclass(frozen=True)
class CodexMemoryRecord:
    source_id: str
    source_file: str
    line_number: int
    source_format: str
    entry: MemoryEntryRequest


@dataclass(frozen=True)
class CodexMemoryWarning:
    source_file: str
    line_number: int
    code: str
    detail: str


@dataclass(frozen=True)
class CodexMemoryNormalizeResult:
    records: list[CodexMemoryRecord] = field(default_factory=list)
    warnings: list[CodexMemoryWarning] = field(default_factory=list)


@dataclass(frozen=True)
class CodexMemoryWriteResult:
    source_id: str
    status_code: int
    status: str | None
    job_id: str | None
    accepted_as: str | None


@dataclass(frozen=True)
class CodexMemoryIngestionReport:
    dry_run: bool
    path_count: int
    record_count: int
    warning_count: int
    write_count: int = 0
    write_error_count: int = 0
    records: list[CodexMemoryRecord] = field(default_factory=list)
    warnings: list[CodexMemoryWarning] = field(default_factory=list)
    writes: list[CodexMemoryWriteResult] = field(default_factory=list)
    write_errors: list[dict[str, str]] = field(default_factory=list)

    def to_json(
        self,
        *,
        include_records: bool = True,
        include_bodies: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dry_run": self.dry_run,
            "would_write": not self.dry_run,
            "path_count": self.path_count,
            "record_count": self.record_count,
            "warning_count": self.warning_count,
            "write_count": self.write_count,
            "write_error_count": self.write_error_count,
            "warnings": [warning.__dict__ for warning in self.warnings],
            "writes": [write.__dict__ for write in self.writes],
            "write_errors": self.write_errors,
        }
        if include_records:
            payload["records"] = [
                {
                    "source_id": record.source_id,
                    "source_file": record.source_file,
                    "line_number": record.line_number,
                    "source_format": record.source_format,
                    "memory_entry": _entry_payload(record.entry, include_body=include_bodies),
                }
                for record in self.records
            ]
        return payload


def expand_codex_memory_paths(
    roots: Iterable[Path | str],
    *,
    glob_patterns: Iterable[str] | str = DEFAULT_CODEX_MEMORY_GLOBS,
    glob_pattern: Iterable[str] | str | None = None,
) -> list[Path]:
    if glob_pattern is not None:
        glob_patterns = glob_pattern
    patterns = (glob_patterns,) if isinstance(glob_patterns, str) else tuple(glob_patterns)
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        candidates: list[Path] = []
        if root.is_dir():
            for default_name in ("MEMORY.md", "memory_summary.md", "raw_memories.md"):
                default_path = root / default_name
                if default_path.exists():
                    candidates.append(default_path)
            for pattern in patterns:
                candidates.extend(sorted(root.rglob(pattern)))
        else:
            candidates = [root]
        for candidate in candidates:
            try:
                key = candidate.resolve()
            except OSError:
                key = candidate
            if key in seen:
                continue
            seen.add(key)
            paths.append(candidate)
    return paths


def codex_memory_pid_lock(lock_path: Path | None):
    return pid_file_lock(lock_path, name="Codex memory ingestion")


def normalize_codex_memory_files(
    paths: Iterable[Path | str],
    *,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str] = (),
    relationship_policy: RelationshipExtractionPolicy = "deferred",
    max_body_chars: int = 50_000,
) -> CodexMemoryNormalizeResult:
    records: list[CodexMemoryRecord] = []
    warnings: list[CodexMemoryWarning] = []
    for raw_path in paths:
        result = normalize_codex_memory_file(
            Path(raw_path),
            tenant_id=tenant_id,
            scope=scope,
            tags=tags,
            relationship_policy=relationship_policy,
            max_body_chars=max_body_chars,
        )
        records.extend(result.records)
        warnings.extend(result.warnings)
    return CodexMemoryNormalizeResult(records=records, warnings=warnings)


def normalize_codex_memory_file(
    path: Path,
    *,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str] = (),
    relationship_policy: RelationshipExtractionPolicy = "deferred",
    max_body_chars: int = 50_000,
) -> CodexMemoryNormalizeResult:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return CodexMemoryNormalizeResult(
            warnings=[
                CodexMemoryWarning(
                    source_file=str(path),
                    line_number=0,
                    code="file_error",
                    detail=str(exc),
                )
            ]
        )

    stripped = text.strip()
    if not stripped:
        return CodexMemoryNormalizeResult()
    if path.suffix.lower() in {".json", ".jsonl"}:
        return _normalize_json_memory_file(
            path,
            stripped,
            tenant_id=tenant_id,
            scope=scope,
            tags=tags,
            relationship_policy=relationship_policy,
            max_body_chars=max_body_chars,
        )
    return _normalize_markdown_memory_file(
        path,
        text,
        tenant_id=tenant_id,
        scope=scope,
        tags=tags,
        relationship_policy=relationship_policy,
        max_body_chars=max_body_chars,
    )


def codex_memory_result_to_dry_run_json(
    result: CodexMemoryNormalizeResult,
    *,
    include_bodies: bool = False,
) -> dict[str, Any]:
    report = CodexMemoryIngestionReport(
        dry_run=True,
        path_count=0,
        record_count=len(result.records),
        warning_count=len(result.warnings),
        records=result.records,
        warnings=result.warnings,
    )
    return report.to_json(include_records=True, include_bodies=include_bodies)


def write_records(
    records: Iterable[CodexMemoryRecord],
    *,
    config: CodexMemoryIngestionConfig,
    client: httpx.Client | None = None,
) -> tuple[list[CodexMemoryWriteResult], list[dict[str, str]]]:
    config.validate_for_write()
    assert config.api_base_url is not None
    assert config.api_key is not None

    close_client = client is None
    http_client = client or httpx.Client(timeout=config.timeout_seconds)
    writes: list[CodexMemoryWriteResult] = []
    errors: list[dict[str, str]] = []
    endpoint = f"{config.api_base_url.rstrip('/')}/api/v1/memory/entries"
    headers = {"X-API-Key": config.api_key}

    try:
        for record in records:
            try:
                response = http_client.post(
                    endpoint,
                    headers=headers,
                    json=record.entry.model_dump(mode="json"),
                )
                response.raise_for_status()
                payload = response.json()
                writes.append(
                    CodexMemoryWriteResult(
                        source_id=record.source_id,
                        status_code=response.status_code,
                        status=payload.get("status") if isinstance(payload, dict) else None,
                        job_id=str(payload.get("job_id")) if isinstance(payload, dict) and payload.get("job_id") else None,
                        accepted_as=payload.get("accepted_as") if isinstance(payload, dict) else None,
                    )
                )
            except (httpx.HTTPError, ValueError) as exc:
                errors.append({"source_id": record.source_id, "error": str(exc)})
    finally:
        if close_client:
            http_client.close()

    return writes, errors


def sweep_codex_memory_records(
    roots: Iterable[Path | str],
    *,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str] = (),
    relationship_policy: RelationshipExtractionPolicy = "deferred",
    max_body_chars: int = 50_000,
    glob_patterns: Iterable[str] | str = DEFAULT_CODEX_MEMORY_GLOBS,
    write: bool = False,
    config: CodexMemoryIngestionConfig | None = None,
    lock_path: Path | None = None,
    client: httpx.Client | None = None,
) -> CodexMemoryIngestionReport:
    paths = expand_codex_memory_paths(roots, glob_patterns=glob_patterns)
    with codex_memory_pid_lock(lock_path):
        normalize_result = normalize_codex_memory_files(
            paths,
            tenant_id=tenant_id,
            scope=scope,
            tags=tags,
            relationship_policy=relationship_policy,
            max_body_chars=max_body_chars,
        )
        writes: list[CodexMemoryWriteResult] = []
        write_errors: list[dict[str, str]] = []
        if write and normalize_result.records:
            writes, write_errors = write_records(
                normalize_result.records,
                config=config or CodexMemoryIngestionConfig.from_env(),
                client=client,
            )
        return CodexMemoryIngestionReport(
            dry_run=not write,
            path_count=len(paths),
            record_count=len(normalize_result.records),
            warning_count=len(normalize_result.warnings),
            write_count=len(writes),
            write_error_count=len(write_errors),
            records=normalize_result.records,
            warnings=normalize_result.warnings,
            writes=writes,
            write_errors=write_errors,
        )


def sweep_codex_memory(
    roots: Iterable[Path | str],
    *,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str] = (),
    relationship_policy: RelationshipExtractionPolicy = "deferred",
    max_body_chars: int = 50_000,
    glob_patterns: Iterable[str] | str = DEFAULT_CODEX_MEMORY_GLOBS,
    glob_pattern: Iterable[str] | str | None = None,
    write: bool = False,
    config: CodexMemoryIngestionConfig | None = None,
    lock_path: Path | None = None,
    client: httpx.Client | None = None,
) -> CodexMemoryIngestionReport:
    return sweep_codex_memory_records(
        roots,
        tenant_id=tenant_id,
        scope=scope,
        tags=tags,
        relationship_policy=relationship_policy,
        max_body_chars=max_body_chars,
        glob_patterns=glob_pattern if glob_pattern is not None else glob_patterns,
        write=write,
        config=config,
        lock_path=lock_path,
        client=client,
    )
def _normalize_json_memory_file(
    path: Path,
    text: str,
    *,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str],
    relationship_policy: RelationshipExtractionPolicy,
    max_body_chars: int,
) -> CodexMemoryNormalizeResult:
    records: list[CodexMemoryRecord] = []
    warnings: list[CodexMemoryWarning] = []
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return CodexMemoryNormalizeResult(
                warnings=[_warning(path, 1, "malformed_record", f"JSON could not be parsed: {exc}")]
            )
        events = [(index, event) for index, event in enumerate(parsed if isinstance(parsed, list) else [parsed], start=1)]
    else:
        events = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                events.append((line_number, json.loads(line)))
            except json.JSONDecodeError as exc:
                warnings.append(_warning(path, line_number, "malformed_record", f"JSON line could not be parsed: {exc}"))

    for line_number, event in events:
        if not isinstance(event, dict):
            warnings.append(_warning(path, line_number, "unsupported_record", "JSON memory record must be an object."))
            continue
        try:
            record = _record_from_json_event(
                path,
                line_number,
                event,
                tenant_id=tenant_id,
                scope=scope,
                tags=tags,
                relationship_policy=relationship_policy,
                max_body_chars=max_body_chars,
            )
        except ValueError as exc:
            warnings.append(_warning(path, line_number, "invalid_memory_entry", str(exc)))
            continue
        records.append(record)
    return CodexMemoryNormalizeResult(records=records, warnings=warnings)


def _record_from_json_event(
    path: Path,
    line_number: int,
    event: dict[str, Any],
    *,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str],
    relationship_policy: RelationshipExtractionPolicy,
    max_body_chars: int,
) -> CodexMemoryRecord:
    payload = event.get("memory_entry") or event.get("entry") or event
    if not isinstance(payload, dict):
        raise ValueError("memory_entry must be an object when present.")

    source_id = _first_text(event.get("source_id"), payload.get("source_id"))
    if _looks_like_memory_entry(payload):
        entry_payload = dict(payload)
        entry_payload.setdefault("tenant_id", tenant_id)
        entry_payload.setdefault("scope", scope.model_dump(mode="json"))
        entry_payload.setdefault("relationship_policy", relationship_policy)
        entry_payload["tags"] = [*tags, *list(entry_payload.get("tags") or []), "codex-memory-import"]
        try:
            entry = MemoryEntryRequest.model_validate(entry_payload)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        source_id = source_id or _source_id(path, line_number, entry.body, payload)
        return CodexMemoryRecord(
            source_id=source_id,
            source_file=str(path),
            line_number=line_number,
            source_format="json_memory_entry",
            entry=entry,
        )

    body = _body_from_json_memory(event)
    if body is None:
        raise ValueError("Codex memory JSON record must include body, raw_text, normalized_text, or text.")
    if len(body) > max_body_chars:
        raise ValueError(f"Record body has {len(body)} characters; max_body_chars is {max_body_chars}.")
    created_at = _parse_datetime(_first_text(event.get("created_at"), event.get("updated_at"), event.get("timestamp")))
    title = _first_text(event.get("title"), event.get("description"), event.get("task")) or "Codex memory record"
    source_id = source_id or _source_id(path, line_number, body, event)
    metadata = _metadata(path, line_number, source_id, "json_codex_memory", event)
    entry = _build_entry(
        tenant_id=tenant_id,
        title=title,
        body=body,
        summary=_first_text(event.get("summary"), event.get("description")),
        created_at=created_at,
        scope=scope,
        source_url=_source_url(path, line_number),
        tags=[*tags, "codex-memory", "codex-memory-json"],
        metadata=metadata,
        idempotency_key=f"codex-memory:{source_id}",
        relationship_policy=relationship_policy,
    )
    return CodexMemoryRecord(
        source_id=source_id,
        source_file=str(path),
        line_number=line_number,
        source_format="json_codex_memory",
        entry=entry,
    )


def _normalize_markdown_memory_file(
    path: Path,
    text: str,
    *,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str],
    relationship_policy: RelationshipExtractionPolicy,
    max_body_chars: int,
) -> CodexMemoryNormalizeResult:
    sections = _markdown_thread_sections(text)
    if not sections:
        sections = [(1, None, text.strip())]

    records: list[CodexMemoryRecord] = []
    warnings: list[CodexMemoryWarning] = []
    for line_number, thread_id, section in sections:
        if not section:
            continue
        if len(section) > max_body_chars:
            warnings.append(
                _warning(
                    path,
                    line_number,
                    "body_too_large",
                    f"Record body has {len(section)} characters; max_body_chars is {max_body_chars}.",
                )
            )
            continue
        frontmatter, body = _extract_markdown_metadata(section)
        body = body.strip() or section.strip()
        title = frontmatter.get("description") or frontmatter.get("task") or "Codex memory record"
        created_at = _parse_datetime(frontmatter.get("updated_at"))
        source_id = _source_id(path, line_number, body, {"thread_id": thread_id, **frontmatter})
        metadata = _metadata(
            path,
            line_number,
            source_id,
            "markdown_codex_memory",
            {"thread_id": thread_id, "frontmatter": frontmatter},
        )
        entry = _build_entry(
            tenant_id=tenant_id,
            title=title,
            body=body,
            summary=frontmatter.get("description"),
            created_at=created_at,
            scope=scope,
            source_url=_source_url(path, line_number),
            tags=[*tags, "codex-memory", "codex-memory-markdown"],
            metadata=metadata,
            idempotency_key=f"codex-memory:{source_id}",
            relationship_policy=relationship_policy,
        )
        records.append(
            CodexMemoryRecord(
                source_id=source_id,
                source_file=str(path),
                line_number=line_number,
                source_format="markdown_codex_memory",
                entry=entry,
            )
        )
    return CodexMemoryNormalizeResult(records=records, warnings=warnings)


def _markdown_thread_sections(text: str) -> list[tuple[int, str, str]]:
    matches = list(_THREAD_HEADING_RE.finditer(text))
    sections: list[tuple[int, str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        line_number = text.count("\n", 0, start) + 1
        sections.append((line_number, match.group("thread_id"), text[start:end].strip()))
    return sections


def _extract_markdown_metadata(section: str) -> tuple[dict[str, str], str]:
    metadata: dict[str, str] = {}
    lines = section.splitlines()
    body_start = 0
    for index, line in enumerate(lines):
        if line.strip() == "---":
            body_start = index + 1
            break
        match = _SECTION_METADATA_RE.match(line.strip())
        if match:
            metadata[match.group(1)] = match.group(2).strip()
    if body_start:
        for index in range(body_start, len(lines)):
            if lines[index].strip() == "---":
                body_start = index + 1
                break
            match = _SECTION_METADATA_RE.match(lines[index].strip())
            if match:
                metadata[match.group(1)] = match.group(2).strip()
    return metadata, "\n".join(lines[body_start:])


def _build_entry(
    *,
    tenant_id: str,
    title: str,
    body: str,
    summary: str | None,
    created_at: datetime,
    scope: MemoryScope,
    source_url: str,
    tags: Iterable[str],
    metadata: dict[str, Any],
    idempotency_key: str,
    relationship_policy: RelationshipExtractionPolicy,
) -> MemoryEntryRequest:
    return MemoryEntryRequest.model_validate(
        {
            "tenant_id": tenant_id,
            "title": _truncate(title.strip(), 240) or "Codex memory record",
            "body": body,
            "summary": summary,
            "source": DEFAULT_CODEX_MEMORY_SOURCE,
            "created_at": created_at,
            "tags": list(tags),
            "scope": scope.model_dump(mode="json"),
            "source_url": source_url,
            "created_by_role": "codex",
            "metadata": metadata,
            "idempotency_key": idempotency_key,
            "enable_ai_enrichment": False,
            "relationship_policy": relationship_policy,
        }
    )


def _entry_payload(entry: MemoryEntryRequest, *, include_body: bool) -> dict[str, Any]:
    payload = entry.model_dump(mode="json")
    body = payload.get("body")
    if not include_body and isinstance(body, str):
        payload["body"] = f"<redacted:{len(body)} chars>"
    return payload


def _looks_like_memory_entry(payload: dict[str, Any]) -> bool:
    return {"title", "body", "source", "created_at"}.issubset(payload.keys())


def _body_from_json_memory(event: dict[str, Any]) -> str | None:
    for key in ("body", "normalized_text", "raw_text", "text", "content"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    fields = event.get("fields")
    if isinstance(fields, dict):
        lines = [f"{key}: {value}" for key, value in fields.items() if isinstance(value, str) and value.strip()]
        if lines:
            return "\n".join(lines)
    return None


def _metadata(
    path: Path,
    line_number: int,
    source_id: str,
    source_format: str,
    source_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "codex_memory_source": {
            "schema_version": 1,
            "source_id": source_id,
            "source_file": str(path),
            "line_number": line_number,
            "source_format": source_format,
            "source_payload": _redact_payload_for_metadata(source_payload),
            "transformation": "codex_memory_record_import",
        }
    }


def _redact_payload_for_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"body", "raw_text", "normalized_text", "text", "content"} and isinstance(item, str):
                redacted[key] = f"<redacted:{len(item)} chars>"
            else:
                redacted[key] = _redact_payload_for_metadata(item)
        return redacted
    if isinstance(value, list):
        return [_redact_payload_for_metadata(item) for item in value]
    return value


def _source_id(path: Path, line_number: int, body: str, identity: dict[str, Any]) -> str:
    stable_identity = {
        "source_file": str(path),
        "line_number": line_number,
        "identity": identity,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    digest = hashlib.sha256(json.dumps(stable_identity, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
    return f"codex-memory-{digest[:32]}"


def _source_url(path: Path, line_number: int) -> str:
    return f"file://{path}#L{line_number}"


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(_UTC)
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(_UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip() + "..."


def _warning(path: Path, line_number: int, code: str, detail: str) -> CodexMemoryWarning:
    return CodexMemoryWarning(source_file=str(path), line_number=line_number, code=code, detail=detail)
