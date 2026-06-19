from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from app.schemas.memory import MemoryEntryRequest, MemoryScope, RelationshipExtractionPolicy
from app.services.codex_memory_privacy import detect_secret_warnings, scan_codex_memory_privacy


CodexMemorySourceKind = Literal["memory-md", "memory-summary", "raw-memories", "rollout-summary"]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_DEFAULT_MAX_BODY_CHARS = 40_000
_LOCAL_MEMORY_SOURCE = "codex-local-memory"
_BASE_TAGS = ("codex-local-memory", "codex-memory-import")
_SOURCE_TAGS: dict[CodexMemorySourceKind, str] = {
    "memory-md": "source-memory-md",
    "memory-summary": "source-memory-summary",
    "raw-memories": "source-raw-memories",
    "rollout-summary": "source-rollout-summary",
}
_LOW_SIGNAL_WARNING_CODE = "low_signal_memory_skipped"
_MIN_SIGNAL_BODY_CHARS = 80
_UUID_ONLY_RE = re.compile(r"^`?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`?$", re.IGNORECASE)
_FOOTNOTE_TITLE_RE = re.compile(r"^\[\d+\](?:\s|$)")
_REFERENCE_FRAGMENT_RE = re.compile(r"^(references:?|true`\s+with\s+`archived_reason:.*|\[\d+\]\s+.+)$", re.IGNORECASE | re.DOTALL)
_COMMAND_FRAGMENT_RE = re.compile(
    r"\b("
    r"python3|uv\s+run|pytest|npm\s+run|pnpm|yarn|gh\s+pr|git\s+status|"
    r"kubectl|curl|docker\s+compose|memory_ops\.py|project_manager_report\.py|"
    r"task_pool_ops\.py|codex_state_snapshot\.py"
    r")\b",
    re.IGNORECASE,
)
_CONCRETE_IDENTIFIER_RE = re.compile(
    r"\b("
    r"PR\s*#?\d+|#\d{2,}|SAR-\d+|task\s+#?\d+|[0-9a-f]{7,40}|"
    r"(?:/Users|/home)/[^/\s]+/|https?://|[A-Za-z0-9_.-]+\.(?:py|ts|tsx|md|yaml|yml|toml|json)"
    r")\b",
    re.IGNORECASE,
)
_DURABLE_SIGNAL_RE = re.compile(
    r"\b("
    r"symptom:|cause:|fix:|future runs should|when the user said|when the user asked|"
    r"when this regression|source of truth|prefer\b|default to\b|do not\b|must\b|"
    r"should stay|should preserve|key failure|root cause|learned|learnings:"
    r")",
    re.IGNORECASE,
)
_METADATA_ONLY_TITLES = {
    "cwd",
    "thread_id",
    "thread id",
    "run thread id",
    "rollout_path",
    "updated_at",
    "session_id",
    "automation_id",
    "task_id",
    "preference signals",
    "source",
    "desc",
    "description",
    "learnings",
    "references",
    "appended memory file",
    "new task written",
}
_GENERIC_LOW_SIGNAL_TITLES = {
    "notes",
    "todo",
    "misc",
    "older memory topics",
    "general tips",
    "user profile",
    "codex memory",
    "codex memory file",
}


@dataclass(frozen=True)
class CodexMemoryImportWarning:
    code: str
    message: str
    source_file: str
    start_line: int | None = None
    end_line: int | None = None
    details: dict[str, int | str] = field(default_factory=dict)

    @property
    def line_number(self) -> int:
        return self.start_line or 0

    @property
    def detail(self) -> str:
        if not self.details:
            return self.message
        return f"{self.message} {self.details}"


@dataclass(frozen=True)
class CodexMemorySourceRecord:
    source_file: Path
    source_kind: CodexMemorySourceKind
    title: str
    body: str
    start_line: int
    end_line: int
    tags: list[str]
    idempotency_key: str
    body_sha256: str


@dataclass(frozen=True)
class CodexMemoryImportResult:
    entries: list[MemoryEntryRequest]
    records: list[CodexMemorySourceRecord]
    warnings: list[CodexMemoryImportWarning]


@dataclass(frozen=True)
class CodexMemoryNormalizedRecord:
    source_id: str
    source_file: str
    line_number: int
    source_format: str
    entry: MemoryEntryRequest


@dataclass(frozen=True)
class CodexMemorySkippedRecord:
    source_file: str
    line_number: int
    source_format: str
    title: str
    reason: str
    body_chars: int
    body_words: int


@dataclass(frozen=True)
class CodexMemoryNormalizeResult:
    records: list[CodexMemoryNormalizedRecord]
    warnings: list[CodexMemoryImportWarning]
    skipped_records: list[CodexMemorySkippedRecord] = field(default_factory=list)

    @property
    def low_signal_count(self) -> int:
        return len(self.skipped_records)

    @property
    def low_signal_ratio(self) -> float:
        total = len(self.records) + len(self.skipped_records)
        if total == 0:
            return 0.0
        return len(self.skipped_records) / total


def build_codex_memory_entries(
    *,
    tenant_id: str,
    memory_md_path: Path | str | None = None,
    memory_summary_path: Path | str | None = None,
    rollout_summary_paths: list[Path | str] | None = None,
    scope: MemoryScope | None = None,
    created_at: datetime | None = None,
    max_body_chars: int = _DEFAULT_MAX_BODY_CHARS,
    relationship_policy: RelationshipExtractionPolicy = "deferred",
) -> CodexMemoryImportResult:
    """Normalize curated local Codex memory files into canonical memory requests."""
    effective_scope = scope or MemoryScope(type="tenant_shared")
    effective_created_at = created_at or datetime.now(timezone.utc)
    records: list[CodexMemorySourceRecord] = []
    warnings: list[CodexMemoryImportWarning] = []

    if memory_md_path is not None:
        parsed = parse_markdown_memory_file(
            memory_md_path,
            source_kind="memory-md",
            max_body_chars=max_body_chars,
        )
        records.extend(parsed.records)
        warnings.extend(parsed.warnings)

    if memory_summary_path is not None:
        parsed = parse_markdown_memory_file(
            memory_summary_path,
            source_kind="memory-summary",
            max_body_chars=max_body_chars,
        )
        records.extend(parsed.records)
        warnings.extend(parsed.warnings)

    for rollout_path in rollout_summary_paths or []:
        parsed = parse_rollout_summary_file(
            rollout_path,
            max_body_chars=max_body_chars,
        )
        records.extend(parsed.records)
        warnings.extend(parsed.warnings)

    entries = [
        record_to_memory_entry_request(
            record,
            tenant_id=tenant_id,
            scope=effective_scope,
            created_at=effective_created_at,
            relationship_policy=relationship_policy,
        )
        for record in records
    ]
    return CodexMemoryImportResult(entries=entries, records=records, warnings=warnings)


def normalize_codex_memory_files(
    paths: Iterable[Path | str],
    *,
    tenant_id: str,
    scope: MemoryScope | None = None,
    tags: Iterable[str] = (),
    relationship_policy: RelationshipExtractionPolicy = "deferred",
    max_body_chars: int = _DEFAULT_MAX_BODY_CHARS,
    include_rollout_summaries: bool = False,
    rollout_glob: str = "rollout_summaries/*.md",
) -> CodexMemoryNormalizeResult:
    """Normalize curated local Codex memory markdown into Palace entry records."""
    effective_scope = scope or MemoryScope(type="agent", key="codex")
    records: list[CodexMemoryNormalizedRecord] = []
    warnings: list[CodexMemoryImportWarning] = []
    skipped_records: list[CodexMemorySkippedRecord] = []
    for raw_path in _expand_curated_memory_paths(
        paths,
        include_rollout_summaries=include_rollout_summaries,
        rollout_glob=rollout_glob,
    ):
        path = Path(raw_path).expanduser()
        parsed = _normalize_curated_markdown_file(
            path,
            tenant_id=tenant_id,
            scope=effective_scope,
            tags=tags,
            relationship_policy=relationship_policy,
            max_body_chars=max_body_chars,
        )
        records.extend(parsed.records)
        warnings.extend(parsed.warnings)
        skipped_records.extend(parsed.skipped_records)
    return CodexMemoryNormalizeResult(records=records, warnings=warnings, skipped_records=skipped_records)


def _expand_curated_memory_paths(
    paths: Iterable[Path | str],
    *,
    include_rollout_summaries: bool = False,
    rollout_glob: str = "rollout_summaries/*.md",
) -> list[Path]:
    expanded: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            key = path.expanduser().resolve(strict=False)
        except OSError:
            key = path.expanduser()
        if key in seen:
            return
        seen.add(key)
        expanded.append(path.expanduser())

    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path.is_dir():
            for candidate in (
                path / "MEMORY.md",
                path / "memory_summary.md",
                path / "raw_memories.md",
            ):
                if candidate.exists():
                    add(candidate)
            if include_rollout_summaries:
                pattern = "*.md" if path.name == "rollout_summaries" else rollout_glob
                for rollout_path in sorted(path.glob(pattern)):
                    add(rollout_path)
            continue
        add(path)
    return expanded


def codex_memory_result_to_dry_run_json(
    result: CodexMemoryNormalizeResult,
    *,
    include_body: bool = False,
    include_bodies: bool | None = None,
) -> dict[str, Any]:
    show_body = include_body if include_bodies is None else include_bodies
    return {
        "dry_run": True,
        "would_write": False,
        "path_count": len(
            {record.source_file for record in result.records}
            | {record.source_file for record in result.skipped_records}
        ),
        "record_count": len(result.records),
        "low_signal_count": result.low_signal_count,
        "low_signal_ratio": round(result.low_signal_ratio, 4),
        "signal_quality": _signal_quality_json(result),
        "warning_count": len(result.warnings),
        "warnings": [_warning_json(warning) for warning in result.warnings],
        "skipped_records": [_skipped_record_json(record) for record in result.skipped_records],
        "records": [
            {
                "source_id": record.source_id,
                "source_file": record.source_file,
                "line_number": record.line_number,
                "source_format": record.source_format,
                "memory_entry": _entry_json(record.entry, include_body=show_body),
            }
            for record in result.records
        ],
    }


def parse_markdown_memory_file(
    path: Path | str,
    *,
    source_kind: Literal["memory-md", "memory-summary", "raw-memories"],
    max_body_chars: int = _DEFAULT_MAX_BODY_CHARS,
) -> CodexMemoryImportResult:
    source_path = Path(path)
    file_text = _read_text_file(source_path)
    if file_text is None:
        return CodexMemoryImportResult(
            entries=[],
            records=[],
            warnings=[_warning("missing_file", "Memory source file is missing.", source_path)],
        )
    if not file_text.strip():
        return CodexMemoryImportResult(
            entries=[],
            records=[],
            warnings=[_warning("empty_file", "Memory source file is empty.", source_path)],
        )

    lines = file_text.splitlines()
    headings = [(index + 1, match.group(2).strip()) for index, line in enumerate(lines) if (match := _HEADING_RE.match(line))]
    if not headings:
        return CodexMemoryImportResult(
            entries=[],
            records=[],
            warnings=[_warning("no_heading_blocks", "Memory markdown file has no heading blocks.", source_path)],
        )

    records: list[CodexMemorySourceRecord] = []
    warnings: list[CodexMemoryImportWarning] = []
    for index, (start_line, title) in enumerate(headings):
        next_start = headings[index + 1][0] if index + 1 < len(headings) else len(lines) + 1
        end_line = next_start - 1
        body = "\n".join(lines[start_line - 1 : end_line]).strip()
        if not body:
            warnings.append(_warning("empty_heading_block", "Memory heading block is empty.", source_path, start_line, end_line))
            continue
        records.append(
            _build_record(
                source_file=source_path,
                source_kind=source_kind,
                title=title,
                body=body,
                start_line=start_line,
                end_line=end_line,
            )
        )
        warnings.extend(_oversized_warnings(source_path, start_line, end_line, body, max_body_chars))

    return CodexMemoryImportResult(entries=[], records=records, warnings=warnings)


def parse_rollout_summary_file(
    path: Path | str,
    *,
    max_body_chars: int = _DEFAULT_MAX_BODY_CHARS,
) -> CodexMemoryImportResult:
    source_path = Path(path)
    file_text = _read_text_file(source_path)
    if file_text is None:
        return CodexMemoryImportResult(
            entries=[],
            records=[],
            warnings=[_warning("missing_file", "Rollout summary file is missing.", source_path)],
        )
    body = file_text.strip()
    if not body:
        return CodexMemoryImportResult(
            entries=[],
            records=[],
            warnings=[_warning("empty_file", "Rollout summary file is empty.", source_path)],
        )

    line_count = len(file_text.splitlines())
    record = _build_record(
        source_file=source_path,
        source_kind="rollout-summary",
        title=_rollout_title(source_path),
        body=body,
        start_line=1,
        end_line=max(line_count, 1),
    )
    return CodexMemoryImportResult(
        entries=[],
        records=[record],
        warnings=_oversized_warnings(source_path, 1, max(line_count, 1), body, max_body_chars),
    )


def record_to_memory_entry_request(
    record: CodexMemorySourceRecord,
    *,
    tenant_id: str,
    scope: MemoryScope | None = None,
    created_at: datetime | None = None,
    relationship_policy: RelationshipExtractionPolicy = "deferred",
) -> MemoryEntryRequest:
    effective_scope = scope or MemoryScope(type="tenant_shared")
    effective_created_at = created_at or datetime.now(timezone.utc)
    scan = scan_codex_memory_privacy(record.body)
    tags = list(record.tags)
    privacy_metadata: dict[str, Any] = {}
    if scan.has_findings:
        tags.extend(["privacy-sensitive", f"privacy-severity-{scan.severity}"])
        privacy_metadata = {
            "severity": scan.severity,
            "finding_count": len(scan.findings),
            "kinds": sorted({finding.kind for finding in scan.findings}),
            "patterns": sorted({finding.pattern for finding in scan.findings}),
        }
    return MemoryEntryRequest(
        tenant_id=tenant_id,
        title=record.title,
        body=record.body,
        summary=None,
        source=_LOCAL_MEMORY_SOURCE,
        created_at=effective_created_at,
        tags=_dedupe_tags(tags),
        scope=effective_scope,
        source_url=f"file://{_stable_path(record.source_file)}#L{record.start_line}",
        created_by_role="system",
        metadata={
            "codex_memory_import": {
                "source_file": str(record.source_file),
                "source_kind": record.source_kind,
                "start_line": record.start_line,
                "end_line": record.end_line,
                "body_sha256": record.body_sha256,
                "privacy": privacy_metadata,
            }
        },
        idempotency_key=record.idempotency_key,
        webhook_url=None,
        enable_ai_enrichment=False,
        relationship_policy=relationship_policy,
    )


def _build_record(
    *,
    source_file: Path,
    source_kind: CodexMemorySourceKind,
    title: str,
    body: str,
    start_line: int,
    end_line: int,
) -> CodexMemorySourceRecord:
    body_sha256 = hashlib.sha256(body.encode()).hexdigest()
    idempotency_key = _idempotency_key(
        source_file=source_file,
        source_kind=source_kind,
        start_line=start_line,
        end_line=end_line,
        body_sha256=body_sha256,
    )
    return CodexMemorySourceRecord(
        source_file=source_file,
        source_kind=source_kind,
        title=_clean_title(title),
        body=body,
        start_line=start_line,
        end_line=end_line,
        tags=[*_BASE_TAGS, _SOURCE_TAGS[source_kind]],
        idempotency_key=idempotency_key,
        body_sha256=body_sha256,
    )


def _idempotency_key(
    *,
    source_file: Path,
    source_kind: CodexMemorySourceKind,
    start_line: int,
    end_line: int,
    body_sha256: str,
) -> str:
    source_identity = _stable_path(source_file)
    if source_kind == "rollout-summary":
        identity = f"{source_kind}:{source_identity}:sha256:{body_sha256}"
    else:
        identity = f"{source_kind}:{source_identity}:lines:{start_line}-{end_line}"
    return f"codexmem:{hashlib.sha256(identity.encode()).hexdigest()[:55]}"


def _normalize_curated_markdown_file(
    path: Path,
    *,
    tenant_id: str,
    scope: MemoryScope,
    tags: Iterable[str],
    relationship_policy: RelationshipExtractionPolicy,
    max_body_chars: int,
) -> CodexMemoryNormalizeResult:
    file_text = _read_text_file(path)
    if file_text is None:
        return CodexMemoryNormalizeResult(records=[], warnings=[_warning("missing_file", "Memory source file is missing.", path)])
    if not file_text.strip():
        return CodexMemoryNormalizeResult(records=[], warnings=[])

    records: list[CodexMemoryNormalizedRecord] = []
    warnings: list[CodexMemoryImportWarning] = []
    skipped_records: list[CodexMemorySkippedRecord] = []
    for item in _iter_markdown_bullet_entries(file_text):
        section, title, body, line_number = item
        normalized_body = _normalize_codex_memory_body(body)
        signal_reason = _low_signal_reason(title=title, body=normalized_body)
        if signal_reason is not None:
            skipped = CodexMemorySkippedRecord(
                source_file=_stable_path(path),
                line_number=line_number,
                source_format="codex_memory_markdown_entry",
                title=title,
                reason=signal_reason,
                body_chars=len(normalized_body),
                body_words=len(normalized_body.split()),
            )
            skipped_records.append(skipped)
            warnings.append(
                _warning(
                    _LOW_SIGNAL_WARNING_CODE,
                    f"Skipped low-signal Codex memory entry: {signal_reason}.",
                    path,
                    line_number,
                    line_number,
                    details={
                        "reason": signal_reason,
                        "signal_quality": "low",
                        "skipped_count": 1,
                        "title": title[:120],
                    },
                )
            )
            continue
        if len(normalized_body) > max_body_chars:
            warnings.append(
                _warning(
                    "oversized_body",
                    "Memory source body exceeds the configured size threshold.",
                    path,
                    line_number,
                    line_number,
                    details={"body_chars": len(normalized_body), "max_body_chars": max_body_chars},
                )
            )
        privacy_warnings = detect_secret_warnings(normalized_body, source_file=_stable_path(path), line_number=line_number)
        warnings.extend(
            _warning(
                warning.code,
                warning.detail,
                path,
                warning.line_number,
                warning.line_number,
                details={"privacy": "redacted"},
            )
            for warning in privacy_warnings
        )
        source_id = _curated_source_id(path, section, title, normalized_body)
        base_tags = [
            "codex-memory",
            "agent-memory" if scope.type == "agent" else f"{scope.type}-memory",
            "codex-memory-import",
            "codex-local-memory",
            f"scope-{scope.type}",
        ]
        if scope.key:
            base_tags.append(f"{scope.type}-{scope.key}")
        entry_tags = _dedupe_tags([*base_tags, *tags])
        signal_quality = _signal_quality(title=title, body=normalized_body)
        scan = scan_codex_memory_privacy(normalized_body)
        privacy_metadata: dict[str, Any] = {}
        if scan.has_findings:
            entry_tags = _dedupe_tags([*entry_tags, "privacy-sensitive", f"privacy-severity-{scan.severity}"])
            privacy_metadata = {
                "severity": scan.severity,
                "finding_count": len(scan.findings),
                "kinds": sorted({finding.kind for finding in scan.findings}),
                "patterns": sorted({finding.pattern for finding in scan.findings}),
            }
        entry = MemoryEntryRequest(
            tenant_id=tenant_id,
            title=title,
            body=normalized_body,
            summary=None,
            source="codex_memory",
            created_at=datetime.now(timezone.utc),
            tags=entry_tags,
            scope=scope,
            source_url=f"file://{_stable_path(path)}#L{line_number}",
            created_by_role="codex",
            metadata={
                "codex_memory": {
                    "schema_version": 1,
                    "source_id": source_id,
                    "source_file": _stable_path(path),
                    "line_number": line_number,
                    "section": section,
                    "signal_quality": signal_quality,
                    "transformation": "codex_memory_markdown_entry",
                    "body_sha256": hashlib.sha256(normalized_body.encode()).hexdigest(),
                    "privacy": privacy_metadata,
                }
            },
            idempotency_key=f"codex-memory:{hashlib.sha256(source_id.encode()).hexdigest()[:51]}",
            webhook_url=None,
            enable_ai_enrichment=False,
            relationship_policy=relationship_policy,
        )
        records.append(
            CodexMemoryNormalizedRecord(
                source_id=source_id,
                source_file=str(path),
                line_number=line_number,
                source_format="codex_memory_markdown_entry",
                entry=entry,
            )
        )
    if not records and not skipped_records:
        warnings.append(_warning("no_parseable_entries", "Memory markdown file has no parseable Codex memory entries.", path))
    return CodexMemoryNormalizeResult(records=records, warnings=warnings, skipped_records=skipped_records)


def _iter_markdown_bullet_entries(text: str) -> list[tuple[str, str, str, int]]:
    lines = text.splitlines()
    entries: list[tuple[str, str, str, int]] = []
    current_section = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        if match := _HEADING_RE.match(line):
            current_section = match.group(2).strip()
            index += 1
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent == 0 and stripped.startswith("- "):
            start_index = index
            body_lines = [stripped[2:].strip()]
            index += 1
            while index < len(lines):
                next_line = lines[index]
                next_stripped = next_line.lstrip()
                next_indent = len(next_line) - len(next_stripped)
                if next_indent == 0 and (next_stripped.startswith("- ") or _HEADING_RE.match(next_line)):
                    break
                if next_stripped:
                    body_lines.append(next_stripped)
                index += 1
            first = body_lines[0]
            title, body_first = _split_bullet_title(first)
            body = "\n".join([body_first, *body_lines[1:]]).strip()
            entries.append((current_section, title, body or first, start_index + 1))
            continue
        index += 1
    return entries


def _split_bullet_title(text: str) -> tuple[str, str]:
    if ":" not in text:
        return (_clean_title(text[:120]), text)
    raw_title, rest = text.split(":", 1)
    return (_clean_title(raw_title), rest.strip())


def _normalize_codex_memory_body(body: str) -> str:
    lines = []
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^\s*[-*]\s+", "", stripped)
        lines.append(stripped)
    return "\n".join(lines).strip()


def _low_signal_reason(*, title: str, body: str) -> str | None:
    normalized_title = _clean_title(title).strip()
    title_key = normalized_title.lower().strip("`\"' ")
    stripped_body = body.strip()
    signal_text = f"{normalized_title}\n{stripped_body}"
    lower_body = stripped_body.lower()
    if not stripped_body:
        return "empty_body"
    if _UUID_ONLY_RE.fullmatch(stripped_body):
        return "uuid_only"
    if _FOOTNOTE_TITLE_RE.match(normalized_title) or _REFERENCE_FRAGMENT_RE.fullmatch(stripped_body):
        return "reference_fragment"
    if title_key in _METADATA_ONLY_TITLES:
        return "metadata_only"
    if _DURABLE_SIGNAL_RE.search(signal_text):
        return None
    if title_key in _GENERIC_LOW_SIGNAL_TITLES and len(stripped_body) < _MIN_SIGNAL_BODY_CHARS:
        return "generic_label"
    if len(stripped_body) < 40 and not _CONCRETE_IDENTIFIER_RE.search(signal_text):
        return "too_short"
    if len(stripped_body) < _MIN_SIGNAL_BODY_CHARS and not (
        _CONCRETE_IDENTIFIER_RE.search(signal_text) or _COMMAND_FRAGMENT_RE.search(signal_text)
    ):
        return "short_without_identifier"
    if (
        _COMMAND_FRAGMENT_RE.search(signal_text)
        and len(stripped_body) < 220
        and not _CONCRETE_IDENTIFIER_RE.search(signal_text)
        and not _DURABLE_SIGNAL_RE.search(signal_text)
    ):
        return "command_fragment"
    if lower_body in {"notes", "todo", "misc", "older memory topics", "general tips", "user profile"}:
        return "generic_label"
    return None


def _signal_quality(*, title: str, body: str) -> str:
    signal_text = f"{title}\n{body}"
    if _DURABLE_SIGNAL_RE.search(signal_text):
        return "durable_learning"
    if _CONCRETE_IDENTIFIER_RE.search(signal_text):
        return "concrete_context"
    if _COMMAND_FRAGMENT_RE.search(signal_text):
        return "operational_context"
    return "context"


def _signal_quality_json(result: CodexMemoryNormalizeResult) -> dict[str, Any]:
    by_quality: dict[str, int] = {}
    for record in result.records:
        metadata = record.entry.metadata.get("codex_memory", {})
        quality = metadata.get("signal_quality") if isinstance(metadata, dict) else None
        key = quality if isinstance(quality, str) else "context"
        by_quality[key] = by_quality.get(key, 0) + 1
    if result.skipped_records:
        by_quality["low"] = result.low_signal_count
    total = len(result.records) + result.low_signal_count
    return {
        "total_bullets": total,
        "retained_count": len(result.records),
        "skipped_low_signal_count": result.low_signal_count,
        "low_signal_ratio": round(result.low_signal_ratio, 4),
        "by_quality": by_quality,
    }


def _curated_source_id(path: Path, section: str, title: str, body: str) -> str:
    identity = {
        "source_file": _stable_path(path),
        "section": _clean_title(section),
        "title": _clean_title(title),
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"codex-memory-{digest[:32]}"


def _entry_json(entry: MemoryEntryRequest, *, include_body: bool) -> dict[str, Any]:
    payload = entry.model_dump(mode="json")
    body = payload.get("body")
    if not include_body and isinstance(body, str):
        payload["body"] = f"<redacted:{len(body)} chars>"
    return payload


def _warning_json(warning: CodexMemoryImportWarning) -> dict[str, Any]:
    return {
        "code": warning.code,
        "message": warning.message,
        "detail": warning.detail,
        "source_file": warning.source_file,
        "line_number": warning.line_number,
        "start_line": warning.start_line,
        "end_line": warning.end_line,
        "details": warning.details,
    }


def _skipped_record_json(record: CodexMemorySkippedRecord) -> dict[str, Any]:
    return {
        "source_file": record.source_file,
        "line_number": record.line_number,
        "source_format": record.source_format,
        "title": record.title,
        "reason": record.reason,
        "body_chars": record.body_chars,
        "body_words": record.body_words,
    }


def _dedupe_tags(tags: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    for tag in tags:
        if tag and tag not in deduped:
            deduped.append(tag)
    return deduped


def _stable_path(path: Path) -> str:
    try:
        return str(path.expanduser().resolve(strict=False))
    except OSError:
        return str(path.expanduser())


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _warning(
    code: str,
    message: str,
    source_file: Path,
    start_line: int | None = None,
    end_line: int | None = None,
    *,
    details: dict[str, int | str] | None = None,
) -> CodexMemoryImportWarning:
    return CodexMemoryImportWarning(
        code=code,
        message=message,
        source_file=_stable_path(source_file),
        start_line=start_line,
        end_line=end_line,
        details=details or {},
    )


def _oversized_warnings(
    source_file: Path,
    start_line: int,
    end_line: int,
    body: str,
    max_body_chars: int,
) -> list[CodexMemoryImportWarning]:
    if max_body_chars <= 0 or len(body) <= max_body_chars:
        return []
    return [
        _warning(
            "oversized_body",
            "Memory source body exceeds the configured size threshold.",
            source_file,
            start_line,
            end_line,
            details={"body_chars": len(body), "max_body_chars": max_body_chars},
        )
    ]


def _clean_title(title: str) -> str:
    cleaned = " ".join(title.strip().split())
    return cleaned or "Untitled Codex memory"


def _rollout_title(path: Path) -> str:
    stem = path.stem.strip()
    if not stem:
        return "Codex rollout summary"
    return f"Codex rollout summary: {stem}"
