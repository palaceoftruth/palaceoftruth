from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

from app.schemas.memory import MemoryScope, RelationshipExtractionPolicy
from app.services.pid_lock import pid_file_lock
from app.services.transcript_sources import (
    TranscriptAdapter,
    TranscriptMemoryRecord,
    TranscriptNormalizeResult,
    TranscriptWarning,
    normalize_transcript_files,
)


DEFAULT_TRANSCRIPT_GLOB = "*.jsonl"
DEFAULT_API_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class TranscriptIngestionConfig:
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
    ) -> "TranscriptIngestionConfig":
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
            raise ValueError("PALACEOFTRUTH_API_BASE_URL is required when --write is set")
        if not self.api_key:
            raise ValueError("PALACEOFTRUTH_API_KEY is required when --write is set")


@dataclass(frozen=True)
class TranscriptWriteResult:
    source_id: str
    status_code: int
    status: str | None
    job_id: str | None
    accepted_as: str | None


@dataclass(frozen=True)
class TranscriptIngestionReport:
    dry_run: bool
    path_count: int
    record_count: int
    warning_count: int
    write_count: int = 0
    write_error_count: int = 0
    records: list[TranscriptMemoryRecord] = field(default_factory=list)
    warnings: list[TranscriptWarning] = field(default_factory=list)
    writes: list[TranscriptWriteResult] = field(default_factory=list)
    write_errors: list[dict[str, str]] = field(default_factory=list)

    def to_json(self, *, include_records: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dry_run": self.dry_run,
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
                    "adapter": record.adapter,
                    "source_id": record.source_id,
                    "source_file": record.source_file,
                    "line_number": record.line_number,
                    "role": record.role,
                    "privacy_classification": record.privacy_classification,
                    "idempotency_key": record.entry.idempotency_key,
                }
                for record in self.records
            ]
        return payload


def expand_transcript_paths(
    roots: Iterable[Path | str],
    *,
    glob_pattern: str = DEFAULT_TRANSCRIPT_GLOB,
) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        candidates = sorted(root.rglob(glob_pattern)) if root.is_dir() else [root]
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


def transcript_pid_lock(lock_path: Path | None):
    return pid_file_lock(lock_path, name="Transcript ingestion")


def write_transcript_records(
    records: Iterable[TranscriptMemoryRecord],
    *,
    config: TranscriptIngestionConfig,
    client: httpx.Client | None = None,
) -> tuple[list[TranscriptWriteResult], list[dict[str, str]]]:
    config.validate_for_write()
    assert config.api_base_url is not None
    assert config.api_key is not None

    close_client = client is None
    http_client = client or httpx.Client(timeout=config.timeout_seconds)
    writes: list[TranscriptWriteResult] = []
    errors: list[dict[str, str]] = []
    endpoint = f"{config.api_base_url.rstrip('/')}/api/v1/memory/entries"
    headers = {"X-API-Key": config.api_key}

    try:
        for record in records:
            try:
                request_payload = record.entry.model_dump(mode="json")
                response = http_client.post(
                    endpoint,
                    headers={**headers, **_memory_entry_scope_headers(request_payload)},
                    json=request_payload,
                )
                response.raise_for_status()
                payload = response.json()
                writes.append(
                    TranscriptWriteResult(
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


def _memory_entry_scope_headers(payload: dict[str, Any]) -> dict[str, str]:
    scopes = ["write"]
    scope = payload.get("scope")
    if isinstance(scope, dict):
        scope_type = scope.get("type")
        if scope_type in {"agent", "workspace", "session"}:
            scopes.append(f"write:{scope_type}")
    return {"X-MCP-Scope": "write", "X-MCP-Scopes": ",".join(scopes)}


def sweep_transcripts(
    roots: Iterable[Path | str],
    *,
    adapter: TranscriptAdapter,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str] = (),
    relationship_policy: RelationshipExtractionPolicy = "deferred",
    max_body_chars: int = 20_000,
    glob_pattern: str = DEFAULT_TRANSCRIPT_GLOB,
    write: bool = False,
    config: TranscriptIngestionConfig | None = None,
    lock_path: Path | None = None,
    client: httpx.Client | None = None,
) -> TranscriptIngestionReport:
    paths = expand_transcript_paths(roots, glob_pattern=glob_pattern)
    with transcript_pid_lock(lock_path):
        normalize_result: TranscriptNormalizeResult = normalize_transcript_files(
            paths,
            adapter=adapter,
            tenant_id=tenant_id,
            scope=scope,
            tags=tags,
            relationship_policy=relationship_policy,
            max_body_chars=max_body_chars,
        )
        writes: list[TranscriptWriteResult] = []
        write_errors: list[dict[str, str]] = []
        if write and normalize_result.records:
            writes, write_errors = write_transcript_records(
                normalize_result.records,
                config=config or TranscriptIngestionConfig.from_env(),
                client=client,
            )
        return TranscriptIngestionReport(
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
