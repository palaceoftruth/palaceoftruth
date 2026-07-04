from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlparse

import boto3
from fastapi import HTTPException
from botocore.config import Config as BotoConfig
from sqlalchemy import and_, delete, func, select, text as sa_text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.embedding import Embedding
from app.models.item import Item
from app.models.job import Job, JobProgressEvent
from app.models.palace import (
    PalaceDirtyItem,
    PalaceRoomEvent,
    PalaceRun,
    PalaceTenantState,
    RetrievalHintArtifact,
    Room,
    RoomClosetArtifact,
    RoomMembership,
    RoomSnapshot,
    RoomTunnel,
    SyncRun,
    SyncSource,
    SyncSourceFile,
    Wing,
)
from app.schemas.palace import (
    PalaceControlTower,
    PalaceConsolidationCandidate,
    PalaceConsolidationSummary,
    PalaceArtifactSectionHealth,
    PalaceDiaryRollupSummary,
    PalaceFactRegistrySummary,
    PalaceMemoryHealthSummary,
    PalaceMemoryJobScope,
    PalaceMemoryJobSummary,
    PalaceMcpActivityEvent,
    PalaceMcpActivitySummary,
    PalaceMembershipDetail,
    PalaceOverview,
    PalacePinRequest,
    PalaceRepresentativeItem,
    PalaceRetrieveRequest,
    PalaceRetrieveResponse,
    PalaceRankingTrace,
    PalaceRankingTraceResult,
    PalaceRetrieveTrace,
    PalaceRoomArtifactBlocker,
    PalaceRoomArtifactHealthSummary,
    PalaceRoomDetail,
    PalaceRoomSummary,
    PalaceRoomUpdate,
    PalaceRunSummary,
    PalaceSectionFreshness,
    PalaceSourceTrustHealthSummary,
    PalaceStateBanner,
    PalaceTraceStep,
    PalaceTunnelActivationTrace,
    PalaceTunnelSummary,
    PalaceWebhookHealthSummary,
    PalaceWebhookJobSummary,
    PalaceWakeupBriefSummary,
    PalaceWingSummary,
    SyncRunSummary,
    SyncSourceCreate,
    SyncSourceSummary,
    SyncSourceUpdate,
)
from app.schemas.job import JobProgressEventResponse
from app.services.queue_telemetry import build_worker_backpressure
from app.schemas.search import SearchResult
from app.services.diary_rollups import build_diary_rollup_summary
from app.services.fact_registry import build_fact_registry_summary
from app.services.item_processing import process_prebuilt_item
from app.services.retrieval_hints import (
    rebuild_room_retrieval_hints,
    report_retrieval_hint_candidates,
    retrieve_retrieval_hint_rescue_results,
)
from app.services.search import SearchService
from app.services.source_trust_summary import build_source_trust_health_summary
from app.services.wakeup_briefs import build_wakeup_brief_summary
from app.utils.crypto import decrypt_secret, encrypt_secret
from app.utils.hash import compute_content_hash

logger = logging.getLogger(__name__)

MEMORY_JOB_TYPE = "memory_artifact"
_MEMORY_JOB_PUBLIC_STATUS_MAP = {
    "queued": "queued",
    "processing": "processing",
    "completed": "complete",
    "duplicate": "duplicate",
    "failed": "failed",
    "cancelled": "cancelled",
}
_MEMORY_SCOPE_TYPES = {"session", "agent", "workspace", "tenant_shared"}
_WEBHOOK_TERMINAL_STATUSES = {"completed", "duplicate", "failed", "cancelled"}
CONSOLIDATION_CANDIDATE_EVENT = "consolidation-candidate"
CONSOLIDATION_CANDIDATE_LIMIT = 8
CONSOLIDATION_CANDIDATE_SCORE_THRESHOLD = 0.62

SYNC_ACTIVE_STATUSES = {"queued", "running"}
PALACE_ACTIVE_STATUSES = {"queued", "routing", "snapshotting", "tunneling"}
SUPPORTED_SYNC_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".go",
    ".rs",
    ".java",
    ".html",
    ".css",
    ".sql",
    ".csv",
}
_LOW_SIGNAL_CONVERSATION_PATTERNS = (
    "don't have any stored knowledge",
    "do not have any stored knowledge",
    "don't know",
    "do not know",
    "still no.",
    "no memory of",
    "doesn't appear",
    "does not appear",
    "not in memory",
    "not in palace of truth",
    "not in palaceoftruth",
    "palace of truth recall",
    "palaceoftruth recall",
    "conversation turns",
    "fact card",
    "media layer",
    "tenant_shared",
    "stored as media",
    "query the media layer",
    "doesn't contain a separate knowledge entry",
    "does not contain a separate knowledge entry",
)
DENIED_PATH_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".ssh",
    ".aws",
    ".kube",
    ".terraform",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
}
DENIED_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "id_rsa",
    "id_dsa",
    "credentials",
    "known_hosts",
}
TOKEN_RE = re.compile(r"[a-z0-9]+")
LOW_CONFIDENCE_ROUTE_SCORE = 0.5
ABSTAIN_ROUTE_SCORE = 0.12
RANKING_TRACE_ROUTING_KEYS = {
    "scope_type",
    "scope_key",
    "requested_scope_type",
    "requested_scope_key",
    "display_limit",
    "candidate_limit",
    "room_count",
    "route_score",
    "route_confidence",
    "route_abstain_reason",
    "route_low_confidence",
    "explicit_tag_filter",
    "fallback_used",
    "room_candidate_count",
    "global_candidate_count",
    "global_merge_rescued_results",
    "activated_tunnel_count",
}
RANKING_TRACE_RERANKER_KEYS = {
    "enabled",
    "provider",
    "model",
    "status",
    "candidate_limit",
    "candidate_count",
    "timeout_ms",
    "latency_ms",
    "changed_top_k",
    "error_class",
}
RANKING_TRACE_LENS_PROFILE_KEYS = {
    "name",
    "description",
    "graph_expansion_enabled",
    "graph_signal_weight",
    "trace_label",
}
OPERATIONAL_TAGS = {
    "benchmark",
    "benchmark-cleanup-ok",
    "corpus-nist-sp800",
    "nist",
    "nist-corpus",
    "nist-sp800",
    "scope-agent",
    "scope-session",
    "scope-tenant_shared",
    "scope-workspace",
}
OPERATIONAL_TAG_PREFIXES = (
    "benchmark-run-",
    "brief-scope-",
    "wake-up-day-",
)
SEED_WING_RULES = [
    ("product / growth", {"product", "growth", "pricing", "launch", "marketing", "cta"}),
    ("customers / calls", {"customer", "customers", "sales", "call", "calls", "interview"}),
    ("infra / code / agents", {"infra", "code", "engineering", "agent", "agents", "repo"}),
    ("founder notes", {"founder", "journal", "note", "notes"}),
    ("research / market", {"research", "market", "analysis", "competitor"}),
    (
        "security / compliance",
        {"security", "privacy", "risk", "controls", "control", "compliance", "nist", "cybersecurity"},
    ),
]


# Palace control plane flow:
# sync source -> sync run -> dirty item generations -> palace run
#   routing      -> room memberships
#   snapshotting -> room summaries / representative drawers
#   tunneling    -> cross-room links + retrieval expansion


@dataclass
class RoutedRoom:
    room: Room | None
    redirected_from_room_id: uuid.UUID | None


@dataclass(frozen=True)
class SyncCandidate:
    relative_path: str
    source_url: str
    source_fingerprint: str | None
    file_size: int
    modified_ns: int | None
    load_text: Callable[[], str]


@dataclass(frozen=True)
class PalaceArtifactRepairPlan:
    snapshot_room_ids: tuple[uuid.UUID, ...]
    tunnel_room_ids: tuple[uuid.UUID, ...]
    blocked_room_ids: tuple[uuid.UUID, ...]
    closet_room_ids: tuple[uuid.UUID, ...] = ()
    retrieval_hint_room_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class PalaceIndexIntegrityPlan:
    missing_embedding_item_ids: tuple[uuid.UUID, ...]
    missing_membership_item_ids: tuple[uuid.UUID, ...]
    artifact_repair_plan: PalaceArtifactRepairPlan


@dataclass(frozen=True)
class PalaceTunnelRecomputeResult:
    room_ids: tuple[uuid.UUID, ...]
    target_generation: int


@dataclass(frozen=True)
class _RoomConsolidationProfile:
    room_id: uuid.UUID
    room_name: str
    room_stable_key: str
    room_slug: str
    wing_id: uuid.UUID
    wing_name: str
    item_ids: frozenset[uuid.UUID]
    tag_counts: dict[str, int]


@dataclass(frozen=True)
class _RoomRouteCandidate:
    room: Room
    wing_name: str
    summary: str | None
    score: float


def slugify(value: str) -> str:
    parts = TOKEN_RE.findall(value.lower())
    return "-".join(parts[:8]) or "room"


def _titleize_slug(value: str) -> str:
    return " ".join(part.capitalize() for part in value.split("-"))


def _allowed_sync_roots() -> list[Path]:
    if settings.palace_sync_allowed_roots:
        roots = [
            Path(part).expanduser().resolve()
            for part in settings.palace_sync_allowed_roots.split(",")
            if part.strip()
        ]
        return roots
    return [Path.cwd().resolve()]


def _normalize_sync_prefix(prefix: str | None) -> str:
    if not prefix:
        return ""
    return prefix.strip().strip("/")


def _sync_source_locator(
    *,
    source_kind: str,
    root_path: str | None = None,
    bucket: str | None = None,
    prefix: str | None = None,
) -> str:
    if source_kind == "s3":
        if not bucket:
            raise HTTPException(status_code=422, detail="S3 sync source requires a bucket")
        normalized_prefix = _normalize_sync_prefix(prefix)
        if normalized_prefix:
            return f"s3://{bucket}/{normalized_prefix}"
        return f"s3://{bucket}"
    if root_path is None:
        raise HTTPException(status_code=422, detail="Sync source path is required")
    return root_path


def _source_kind_label(source_kind: str) -> str:
    if source_kind == "s3":
        return "S3"
    return source_kind.capitalize()


def _sync_source_summary(source: SyncSource) -> SyncSourceSummary:
    return SyncSourceSummary(
        id=source.id,
        name=source.name,
        root_path=source.root_path,
        source_kind=source.source_kind,
        credential_type=source.credential_type or "none",
        has_stored_credential=bool(source.credential_ciphertext),
        status="active" if source.status == "active" else "disabled",
        disabled_at=source.disabled_at,
        disabled_reason=source.disabled_reason,
        scan_interval_seconds=source.scan_interval_seconds,
        allowed_extensions=source.allowed_extensions or [],
        bucket=source.bucket,
        prefix=source.prefix,
        endpoint_url=source.endpoint_url,
        region=source.region,
        force_path_style=bool(source.force_path_style),
        last_synced_at=source.last_synced_at,
        last_error=source.last_error,
    )


def _is_remote_github_repo(root_path: str) -> bool:
    if root_path.startswith("git@github.com:"):
        return "/" in root_path.split(":", 1)[1]

    parsed = urlparse(root_path)
    if parsed.scheme not in {"https", "ssh"}:
        return False
    if parsed.hostname != "github.com":
        return False
    return parsed.path.strip("/").count("/") >= 1


def _github_repo_parts(root_path: str) -> tuple[str, str]:
    if root_path.startswith("git@github.com:"):
        repo_path = root_path.split(":", 1)[1]
    else:
        parsed = urlparse(root_path)
        if parsed.hostname != "github.com":
            raise ValueError("Only github.com repo URLs are supported for remote repo sync")
        repo_path = parsed.path.lstrip("/")

    parts = [part for part in repo_path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("GitHub repo URL must include owner and repo")

    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def _github_https_repo_url(root_path: str) -> str:
    owner, repo = _github_repo_parts(root_path)
    return f"https://github.com/{owner}/{repo}.git"


def _github_ssh_repo_url(root_path: str) -> str:
    owner, repo = _github_repo_parts(root_path)
    return f"git@github.com:{owner}/{repo}.git"


def _github_blob_url(root_path: str, branch: str, relative_path: str) -> str:
    owner, repo = _github_repo_parts(root_path)
    quoted_branch = quote(branch, safe="")
    quoted_path = quote(relative_path, safe="/")
    return f"https://github.com/{owner}/{repo}/blob/{quoted_branch}/{quoted_path}"


def _sync_source_secret_key() -> str:
    if not settings.palaceoftruth_sync_source_credential_key:
        raise HTTPException(
            status_code=503,
            detail="Repo credentials are not enabled because PALACEOFTRUTH_SYNC_SOURCE_CREDENTIAL_KEY is not configured",
        )
    return settings.palaceoftruth_sync_source_credential_key


def _encrypt_repo_credential(value: str) -> str:
    return encrypt_secret(value, key=_sync_source_secret_key())


def _decrypt_repo_credential(value: str) -> str:
    return decrypt_secret(value, key=_sync_source_secret_key())


def _is_remote_repo_source(source: SyncSource) -> bool:
    return source.source_kind == "repo" and _is_remote_github_repo(source.root_path)


def _repo_checkout_exists(source: SyncSource) -> bool:
    return _repo_checkout_dir(source).exists()


def _remove_repo_checkout(source: SyncSource) -> None:
    if not _is_remote_repo_source(source) and not _repo_checkout_exists(source):
        return
    shutil.rmtree(_repo_checkout_dir(source), ignore_errors=True)


def _resolve_repo_credential_ciphertext(
    *,
    credential_type: str,
    github_pat: str | None,
    ssh_private_key: str | None,
    existing_source: SyncSource | None = None,
    clear_stored_credential: bool = False,
) -> str | None:
    if credential_type == "none":
        return None
    if credential_type == "deployment_github_pat":
        if not settings.github_pat:
            raise HTTPException(
                status_code=503,
                detail="Repo credential mode deployment_github_pat requires GITHUB_PAT to be configured",
            )
        return None
    if credential_type == "github_pat":
        if github_pat is not None:
            try:
                return _encrypt_repo_credential(github_pat)
            except ValueError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        if clear_stored_credential:
            raise HTTPException(status_code=422, detail="github_pat must be provided when credential_type is github_pat")
        if existing_source and existing_source.credential_type == "github_pat" and existing_source.credential_ciphertext:
            return existing_source.credential_ciphertext
        raise HTTPException(status_code=422, detail="github_pat must be provided when credential_type is github_pat")
    if credential_type == "ssh_key":
        if ssh_private_key is not None:
            try:
                return _encrypt_repo_credential(ssh_private_key)
            except ValueError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        if clear_stored_credential:
            raise HTTPException(status_code=422, detail="ssh_private_key must be provided when credential_type is ssh_key")
        if existing_source and existing_source.credential_type == "ssh_key" and existing_source.credential_ciphertext:
            return existing_source.credential_ciphertext
        raise HTTPException(status_code=422, detail="ssh_private_key must be provided when credential_type is ssh_key")
    raise HTTPException(status_code=422, detail=f"Unsupported repo credential type: {credential_type}")


def _resolve_sync_source_values(
    *,
    source_kind: str,
    root_path: str | None,
    credential_type: str,
    github_pat: str | None,
    ssh_private_key: str | None,
    scan_interval_seconds: int,
    allowed_extensions: list[str] | None,
    bucket: str | None,
    prefix: str | None,
    endpoint_url: str | None,
    region: str | None,
    force_path_style: bool,
    existing_source: SyncSource | None = None,
    clear_stored_credential: bool = False,
) -> dict[str, object]:
    normalized_extensions = _normalized_allowed_extensions(allowed_extensions)
    resolved_credential_type = "none"
    credential_ciphertext = None

    if source_kind == "s3":
        if not bucket:
            raise HTTPException(status_code=422, detail="S3 sync source requires a bucket")
        if endpoint_url and not endpoint_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="S3 endpoint_url must start with http:// or https://")
        normalized_prefix = _normalize_sync_prefix(prefix) or None
        resolved_root_path = _sync_source_locator(
            source_kind=source_kind,
            bucket=bucket,
            prefix=normalized_prefix,
        )
        return {
            "root_path": resolved_root_path,
            "source_kind": "s3",
            "credential_type": "none",
            "credential_ciphertext": None,
            "bucket": bucket,
            "prefix": normalized_prefix,
            "endpoint_url": endpoint_url,
            "region": region,
            "force_path_style": force_path_style,
            "scan_interval_seconds": scan_interval_seconds,
            "allowed_extensions": normalized_extensions,
        }

    if root_path is None:
        raise HTTPException(status_code=422, detail="Sync source path is required")

    if source_kind == "repo" and _is_remote_github_repo(root_path):
        resolved_credential_type = credential_type
        credential_ciphertext = _resolve_repo_credential_ciphertext(
            credential_type=resolved_credential_type,
            github_pat=github_pat,
            ssh_private_key=ssh_private_key,
            existing_source=existing_source,
            clear_stored_credential=clear_stored_credential,
        )
        return {
            "root_path": root_path,
            "source_kind": "repo",
            "credential_type": resolved_credential_type,
            "credential_ciphertext": credential_ciphertext,
            "bucket": None,
            "prefix": None,
            "endpoint_url": None,
            "region": None,
            "force_path_style": False,
            "scan_interval_seconds": scan_interval_seconds,
            "allowed_extensions": normalized_extensions,
        }

    resolved_root_path = str(validate_sync_root(root_path))
    if source_kind == "repo" and credential_type != "none":
        raise HTTPException(
            status_code=422,
            detail="Repo credentials are only valid for remote github.com repo URLs",
        )
    return {
        "root_path": resolved_root_path,
        "source_kind": source_kind,
        "credential_type": "none",
        "credential_ciphertext": None,
        "bucket": None,
        "prefix": None,
        "endpoint_url": None,
        "region": None,
        "force_path_style": False,
        "scan_interval_seconds": scan_interval_seconds,
        "allowed_extensions": normalized_extensions,
    }


async def _ensure_sync_source_mutable(db: AsyncSession, *, tenant_id: str, source_id: uuid.UUID) -> None:
    active = await db.scalar(
        select(SyncRun.id)
        .where(SyncRun.tenant_id == tenant_id)
        .where(SyncRun.sync_source_id == source_id)
        .where(SyncRun.status.in_(SYNC_ACTIVE_STATUSES))
        .limit(1)
    )
    if active is not None:
        raise HTTPException(status_code=409, detail="Sync source has an active sync run")


def _ensure_sync_source_active(source: SyncSource) -> None:
    if source.status != "active":
        raise HTTPException(status_code=409, detail="Sync source is disabled")


def validate_sync_root(root_path: str) -> Path:
    candidate = Path(root_path).expanduser()
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="Sync source path does not exist")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise HTTPException(status_code=422, detail="Sync source path must be a directory")

    for part in resolved.parts:
        if part in DENIED_PATH_PARTS or part in DENIED_FILE_NAMES:
            raise HTTPException(status_code=422, detail="Sync source path is denied by policy")

    allowed_roots = _allowed_sync_roots()
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=422, detail="Sync source path is outside allowed roots")

    return resolved


def _path_is_denied(path: Path) -> bool:
    if any(part in DENIED_PATH_PARTS for part in path.parts):
        return True
    if path.name in DENIED_FILE_NAMES:
        return True
    if path.name.startswith(".env"):
        return True
    if path.suffix in {".pem", ".key", ".crt"}:
        return True
    return False


def _is_supported_sync_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SYNC_EXTENSIONS


def _normalized_allowed_extensions(extensions: list[str] | None) -> list[str] | None:
    if not extensions:
        return None

    normalized: list[str] = []
    for extension in extensions:
        value = extension.strip().lower()
        if not value:
            continue
        if not value.startswith("."):
            value = f".{value}"
        if value not in SUPPORTED_SYNC_EXTENSIONS:
            raise HTTPException(status_code=422, detail=f"Unsupported sync extension: {value}")
        normalized.append(value)

    if not normalized:
        return None
    return list(dict.fromkeys(normalized))


def _file_source_type(path: Path) -> str:
    if path.suffix.lower() in {".md", ".markdown", ".txt"}:
        return "note"
    return "doc"


async def _load_sync_item(
    db: AsyncSession,
    *,
    tenant_id: str,
    row: SyncSourceFile | None,
    source_url: str,
) -> Item | None:
    if row and row.item_id:
        item = await db.get(Item, row.item_id)
        if item is not None:
            return item

    return await db.scalar(
        select(Item)
        .where(Item.tenant_id == tenant_id)
        .where(Item.source_url == source_url)
        .limit(1)
    )


def _sync_file_title(relative_path: Path) -> str:
    if relative_path.suffix:
        return relative_path.stem
    return relative_path.name


def _is_blank_sync_text(raw_text: str) -> bool:
    return raw_text.strip() == ""


def _tokenize(value: str | None) -> set[str]:
    if not value:
        return set()
    return set(TOKEN_RE.findall(value.lower()))


def _memory_entry_metadata(item: Item) -> dict[str, Any]:
    memory_entry = (item.metadata_ or {}).get("memory_entry")
    if not isinstance(memory_entry, dict):
        return {}
    metadata = memory_entry.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _memory_entry_scope(item: Item) -> dict[str, Any]:
    memory_entry = (item.metadata_ or {}).get("memory_entry")
    if not isinstance(memory_entry, dict):
        return {}
    scope = memory_entry.get("scope")
    return scope if isinstance(scope, dict) else {}


def _explicit_palace_placement(item: Item) -> dict[str, Any]:
    metadata = _memory_entry_metadata(item)
    palace = metadata.get("palace") or metadata.get("placement")
    return palace if isinstance(palace, dict) else {}


def _tag_is_operational(tag: str) -> bool:
    lowered = tag.lower()
    if lowered in OPERATIONAL_TAGS:
        return True
    nist_tag_suffix = lowered.removeprefix("nist-")
    if lowered.startswith("nist-") and nist_tag_suffix[:1].isdigit():
        return True
    return lowered.startswith(OPERATIONAL_TAG_PREFIXES)


def _semantic_tags(item: Item) -> list[str]:
    scope = _memory_entry_scope(item)
    scope_type = str(scope.get("type") or "").strip().lower()
    scope_key = str(scope.get("key") or "").strip().lower()
    scope_tags = {f"scope-{scope_type}"} if scope_type else set()
    if scope_type and scope_key:
        scope_tags.add(f"{scope_type}-{scope_key}")

    return [
        tag
        for tag in (item.tags or [])
        if not _tag_is_operational(tag) and tag.lower() not in scope_tags
    ]


def _scope_fallback_room(item: Item) -> str | None:
    scope = _memory_entry_scope(item)
    scope_type = str(scope.get("type") or "")
    scope_key = str(scope.get("key") or "").strip()
    if scope_type in {"agent", "workspace", "session"} and scope_key:
        return _titleize_slug(slugify(scope_key))
    return None


def _nist_publication_room(item: Item) -> str | None:
    metadata = _memory_entry_metadata(item)
    nist = metadata.get("nist")
    if not isinstance(nist, dict):
        return None
    title = str(nist.get("title") or "").strip()
    publication_id = str(nist.get("publication_id") or "").strip()
    if title:
        return _titleize_slug(slugify(title))
    if publication_id:
        return f"NIST SP {publication_id}"
    return None


def _infer_wing_name(item: Item) -> str:
    placement = _explicit_palace_placement(item)
    wing = str(placement.get("wing") or "").strip()
    if wing:
        return wing

    if _nist_publication_room(item):
        return "Security / Compliance"

    path_parts = [
        part.lower()
        for part in Path(str(item.metadata_.get("sync_relative_path", ""))).parts
        if part not in {".", ""}
    ]
    tokens = set(path_parts)
    for tag in _semantic_tags(item):
        tokens.update(_tokenize(tag))
    tokens.update(category.lower() for category in (item.categories or []))
    tokens.update(_tokenize(item.title))
    scope_type = str(_memory_entry_scope(item).get("type") or "")
    if scope_type:
        tokens.add(scope_type)

    for label, keywords in SEED_WING_RULES:
        if tokens & keywords:
            return " / ".join(part.capitalize() for part in label.split(" / "))

    if path_parts:
        return _titleize_slug(slugify(path_parts[0]))
    if item.categories:
        return _titleize_slug(slugify(item.categories[0]))
    semantic_tags = _semantic_tags(item)
    if semantic_tags:
        return _titleize_slug(slugify(semantic_tags[0]))
    scope_room = _scope_fallback_room(item)
    if scope_room:
        return "Infra / Code / Agents"
    return "General"


def _infer_room_name(item: Item) -> str:
    placement = _explicit_palace_placement(item)
    room = str(placement.get("room") or "").strip()
    if room:
        return room

    nist_room = _nist_publication_room(item)
    if nist_room:
        return nist_room

    path_parts = [
        part
        for part in Path(str(item.metadata_.get("sync_relative_path", ""))).parts
        if part not in {".", ""}
    ]
    if len(path_parts) >= 2:
        return _titleize_slug(slugify(path_parts[1]))
    semantic_tags = _semantic_tags(item)
    if semantic_tags:
        return _titleize_slug(slugify(semantic_tags[0]))
    if item.categories:
        return _titleize_slug(slugify(item.categories[0]))
    scope_room = _scope_fallback_room(item)
    if scope_room:
        return scope_room
    title_tokens = [part.capitalize() for part in item.title.split()[:3]]
    return " ".join(title_tokens) or "General"


def _infer_room_stable_key(item: Item, wing_name: str, room_name: str) -> str:
    return f"{slugify(wing_name)}:{slugify(room_name)}"


def _room_status(generation: int, target_generation: int, active_generation: int | None, *, room_state: str = "active") -> PalaceSectionFreshness:
    if room_state == "redirected":
        return PalaceSectionFreshness(
            status="redirected",
            generation=generation,
            target_generation=target_generation,
            message="This room now redirects to a newer room.",
        )
    if active_generation is not None and generation < active_generation <= target_generation:
        return PalaceSectionFreshness(
            status="indexing",
            generation=generation,
            target_generation=target_generation,
            message="Refresh in progress for this section.",
        )
    if generation < target_generation:
        return PalaceSectionFreshness(
            status="stale",
            generation=generation,
            target_generation=target_generation,
            message="Showing last confirmed state while newer changes wait in backlog.",
        )
    return PalaceSectionFreshness(
        status="fresh",
        generation=generation,
        target_generation=target_generation,
        message="Current with the latest relevant Palace generation.",
    )


def _room_artifact_statuses(
    room: Room,
    state: PalaceTenantState,
) -> tuple[PalaceSectionFreshness, PalaceSectionFreshness, PalaceSectionFreshness]:
    room_target_generation = room.membership_generation
    return (
        _room_status(
            room.membership_generation,
            room_target_generation,
            state.active_generation,
            room_state=room.state,
        ),
        _room_status(
            room.snapshot_generation,
            room_target_generation,
            state.active_generation,
            room_state=room.state,
        ),
        _room_status(
            room.tunnel_generation,
            room_target_generation,
            state.active_generation,
            room_state=room.state,
        ),
    )


async def ensure_tenant_state(db: AsyncSession, tenant_id: str) -> PalaceTenantState:
    state = await db.get(PalaceTenantState, tenant_id)
    if state:
        return state
    state = PalaceTenantState(tenant_id=tenant_id)
    db.add(state)
    await db.flush()
    return state


async def mark_items_dirty(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_ids: Iterable[uuid.UUID],
    reason: str,
    sync_source_id: uuid.UUID | None = None,
) -> int:
    unique_item_ids = tuple(dict.fromkeys(item_ids))
    state = await ensure_tenant_state(db, tenant_id)
    if not unique_item_ids:
        return state.dirty_generation

    state.dirty_generation += 1

    existing_rows = (
        await db.execute(
            select(PalaceDirtyItem)
            .where(PalaceDirtyItem.tenant_id == tenant_id)
            .where(PalaceDirtyItem.item_id.in_(unique_item_ids))
        )
    ).scalars().all()
    existing_by_item_id = {dirty.item_id: dirty for dirty in existing_rows}

    for dirty in existing_by_item_id.values():
        dirty.generation = state.dirty_generation
        dirty.reason = reason
        dirty.sync_source_id = sync_source_id

    for item_id in unique_item_ids:
        if item_id in existing_by_item_id:
            continue
        db.add(
            PalaceDirtyItem(
                tenant_id=tenant_id,
                item_id=item_id,
                generation=state.dirty_generation,
                reason=reason,
                sync_source_id=sync_source_id,
            )
        )
    await db.flush()
    return state.dirty_generation


async def mark_item_dirty(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_id: uuid.UUID,
    reason: str,
    sync_source_id: uuid.UUID | None = None,
) -> int:
    return await mark_items_dirty(
        db,
        tenant_id=tenant_id,
        item_ids=(item_id,),
        reason=reason,
        sync_source_id=sync_source_id,
    )


async def create_sync_source(db: AsyncSession, *, tenant_id: str, body: SyncSourceCreate) -> SyncSource:
    resolved = _resolve_sync_source_values(
        source_kind=body.source_kind,
        root_path=body.root_path,
        credential_type=body.credential_type,
        github_pat=body.github_pat,
        ssh_private_key=body.ssh_private_key,
        scan_interval_seconds=body.scan_interval_seconds,
        allowed_extensions=body.allowed_extensions,
        bucket=body.bucket,
        prefix=body.prefix,
        endpoint_url=body.endpoint_url,
        region=body.region,
        force_path_style=body.force_path_style,
    )

    source = SyncSource(
        tenant_id=tenant_id,
        name=body.name,
        root_path=resolved["root_path"],
        source_kind=resolved["source_kind"],
        credential_type=resolved["credential_type"],
        credential_ciphertext=resolved["credential_ciphertext"],
        bucket=resolved["bucket"],
        prefix=resolved["prefix"],
        endpoint_url=resolved["endpoint_url"],
        region=resolved["region"],
        force_path_style=resolved["force_path_style"],
        scan_interval_seconds=resolved["scan_interval_seconds"],
        allowed_extensions=resolved["allowed_extensions"],
        status="active",
    )
    db.add(source)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Sync source already exists")
    await db.refresh(source)
    return source


async def update_sync_source(
    db: AsyncSession,
    *,
    tenant_id: str,
    source: SyncSource,
    body: SyncSourceUpdate,
) -> SyncSource:
    _ensure_sync_source_active(source)
    await _ensure_sync_source_mutable(db, tenant_id=tenant_id, source_id=source.id)

    fields = body.model_fields_set
    target_source_kind = body.source_kind if "source_kind" in fields and body.source_kind is not None else source.source_kind
    target_name = body.name if "name" in fields and body.name is not None else source.name
    target_scan_interval = (
        body.scan_interval_seconds
        if "scan_interval_seconds" in fields and body.scan_interval_seconds is not None
        else source.scan_interval_seconds
    )
    target_allowed_extensions = (
        body.allowed_extensions
        if "allowed_extensions" in fields and body.allowed_extensions is not None
        else source.allowed_extensions
    )

    if target_source_kind == "s3":
        target_root_path = None
        target_bucket = body.bucket if "bucket" in fields else source.bucket
        target_prefix = body.prefix if "prefix" in fields else source.prefix
        target_endpoint_url = body.endpoint_url if "endpoint_url" in fields else source.endpoint_url
        target_region = body.region if "region" in fields else source.region
        target_force_path_style = (
            body.force_path_style if "force_path_style" in fields and body.force_path_style is not None else bool(source.force_path_style)
        )
        target_credential_type = "none"
        target_github_pat = None
        target_ssh_private_key = None
    else:
        target_root_path = body.root_path if "root_path" in fields else source.root_path
        target_bucket = None
        target_prefix = None
        target_endpoint_url = None
        target_region = None
        target_force_path_style = False
        if target_source_kind == "repo":
            target_credential_type = (
                body.credential_type
                if "credential_type" in fields and body.credential_type is not None
                else (source.credential_type or "none")
            )
            target_github_pat = body.github_pat if "github_pat" in fields else None
            target_ssh_private_key = body.ssh_private_key if "ssh_private_key" in fields else None
        else:
            target_credential_type = "none"
            target_github_pat = None
            target_ssh_private_key = None

    remove_checkout_after_commit = _is_remote_repo_source(source) and not (
        target_source_kind == "repo" and target_root_path and _is_remote_github_repo(target_root_path)
    )
    resolved = _resolve_sync_source_values(
        source_kind=target_source_kind,
        root_path=target_root_path,
        credential_type=target_credential_type,
        github_pat=target_github_pat,
        ssh_private_key=target_ssh_private_key,
        scan_interval_seconds=target_scan_interval,
        allowed_extensions=target_allowed_extensions,
        bucket=target_bucket,
        prefix=target_prefix,
        endpoint_url=target_endpoint_url,
        region=target_region,
        force_path_style=target_force_path_style,
        existing_source=source,
        clear_stored_credential=body.clear_stored_credential,
    )

    source.name = target_name
    source.root_path = resolved["root_path"]
    source.source_kind = resolved["source_kind"]
    source.credential_type = resolved["credential_type"]
    source.credential_ciphertext = resolved["credential_ciphertext"]
    source.bucket = resolved["bucket"]
    source.prefix = resolved["prefix"]
    source.endpoint_url = resolved["endpoint_url"]
    source.region = resolved["region"]
    source.force_path_style = resolved["force_path_style"]
    source.scan_interval_seconds = resolved["scan_interval_seconds"]
    source.allowed_extensions = resolved["allowed_extensions"]
    source.last_error = None
    await db.commit()
    await db.refresh(source)
    if remove_checkout_after_commit:
        _remove_repo_checkout(source)
    return source


async def delete_sync_source(
    db: AsyncSession,
    *,
    tenant_id: str,
    source: SyncSource,
    actor_type: str = "system",
    actor_id: str | None = None,
) -> int:
    _ensure_sync_source_active(source)
    await _ensure_sync_source_mutable(db, tenant_id=tenant_id, source_id=source.id)

    disabled_at = datetime.now(timezone.utc)
    owned_items = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(sa_text("metadata ->> 'sync_source_id' = :source_id").bindparams(source_id=str(source.id)))
        )
    ).scalars().all()
    deactivated = 0
    for item in owned_items:
        metadata = dict(item.metadata_ or {})
        metadata["sync_active"] = False
        metadata["sync_deleted"] = True
        metadata["sync_source_deleted"] = True
        item.metadata_ = metadata
        item.status = "failed"
        item.updated_at = datetime.now(timezone.utc)
        await mark_item_dirty(
            db,
            tenant_id=tenant_id,
            item_id=item.id,
            reason="sync-source-delete",
            sync_source_id=source.id,
        )
        deactivated += 1

    source.status = "disabled"
    source.disabled_at = disabled_at
    source.disabled_by = actor_id
    source.disabled_reason = "sync-source-removal"
    source.last_error = None
    db.add(
        PalaceRoomEvent(
            tenant_id=tenant_id,
            room_id=None,
            event_type="sync-source-disabled",
            payload={
                "sync_source_id": str(source.id),
                "sync_source_name": source.name,
                "root_path": source.root_path,
                "source_kind": source.source_kind,
                "items_deactivated": deactivated,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "disabled_at": disabled_at.isoformat(),
            },
        )
    )
    await db.commit()
    return deactivated


async def restore_sync_source(
    db: AsyncSession,
    *,
    tenant_id: str,
    source: SyncSource,
    actor_type: str = "system",
    actor_id: str | None = None,
) -> SyncSource:
    if source.status == "active":
        return source
    await _ensure_sync_source_mutable(db, tenant_id=tenant_id, source_id=source.id)
    restored_at = datetime.now(timezone.utc)
    source.status = "active"
    source.disabled_at = None
    source.disabled_by = None
    source.disabled_reason = None
    source.last_error = None
    db.add(
        PalaceRoomEvent(
            tenant_id=tenant_id,
            room_id=None,
            event_type="sync-source-restored",
            payload={
                "sync_source_id": str(source.id),
                "sync_source_name": source.name,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "restored_at": restored_at.isoformat(),
            },
        )
    )
    await db.commit()
    await db.refresh(source)
    return source


async def create_or_get_sync_run(
    db: AsyncSession,
    *,
    tenant_id: str,
    source: SyncSource,
    triggered_by: str,
) -> tuple[SyncRun, bool]:
    _ensure_sync_source_active(source)
    active = await db.scalar(
        select(SyncRun)
        .where(SyncRun.tenant_id == tenant_id)
        .where(SyncRun.sync_source_id == source.id)
        .where(SyncRun.status.in_(SYNC_ACTIVE_STATUSES))
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    )
    if active:
        return active, False

    run = SyncRun(
        sync_source_id=source.id,
        tenant_id=tenant_id,
        status="queued",
        triggered_by=triggered_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run, True


async def sync_source_has_local_file_changes(db: AsyncSession, source: SyncSource) -> bool:
    """Return whether a local filesystem-backed sync source changed since the last run."""
    if source.source_kind == "s3" or (source.source_kind == "repo" and _is_remote_github_repo(source.root_path)):
        return False

    try:
        root = validate_sync_root(source.root_path)
        candidates = _iter_sync_files(root, allowed_extensions=source.allowed_extensions)
    except Exception as exc:
        logger.warning(
            "skipping local sync watcher for source_id=%s tenant=%s error=%s",
            source.id,
            source.tenant_id,
            exc,
        )
        return False

    rows = (
        await db.execute(
            select(SyncSourceFile)
            .where(SyncSourceFile.tenant_id == source.tenant_id)
            .where(SyncSourceFile.sync_source_id == source.id)
        )
    ).scalars().all()
    rows_by_path = {row.relative_path: row for row in rows}
    candidate_paths = {candidate.relative_path for candidate in candidates}

    for candidate in candidates:
        row = rows_by_path.get(candidate.relative_path)
        if row is None:
            return True
        if (
            row.source_fingerprint != candidate.source_fingerprint
            or row.file_size != candidate.file_size
            or row.modified_ns != candidate.modified_ns
        ):
            return True

    return any(row.status in {"active", "skipped"} and row.relative_path not in candidate_paths for row in rows)


async def create_or_get_palace_run(
    db: AsyncSession,
    *,
    tenant_id: str,
    triggered_by: str,
    source_sync_run_id: uuid.UUID | None = None,
) -> tuple[PalaceRun, bool]:
    active = await db.scalar(
        select(PalaceRun)
        .where(PalaceRun.tenant_id == tenant_id)
        .where(PalaceRun.status.in_(PALACE_ACTIVE_STATUSES))
        .order_by(PalaceRun.started_at.desc())
        .limit(1)
    )
    if active:
        logger.info(
            "palace run submission coalesced tenant=%s trigger=%s active_run_id=%s active_status=%s requested_generation=%s",
            tenant_id,
            triggered_by,
            active.id,
            active.status,
            active.requested_generation,
        )
        return active, False

    state = await ensure_tenant_state(db, tenant_id)
    stale_run_id = state.active_palace_run_id
    if stale_run_id:
        stale_run = await db.get(PalaceRun, stale_run_id)
        stale_status = getattr(stale_run, "status", None)
        if stale_run is None or stale_status not in PALACE_ACTIVE_STATUSES:
            logger.warning(
                "palace run lease recovered tenant=%s trigger=%s stale_run_id=%s stale_status=%s dirty_generation=%s indexed_generation=%s active_generation=%s",
                tenant_id,
                triggered_by,
                stale_run_id,
                stale_status or "missing",
                state.dirty_generation,
                state.indexed_generation,
                state.active_generation,
            )
            state.active_palace_run_id = None
            state.active_generation = None

    run = PalaceRun(
        tenant_id=tenant_id,
        status="queued",
        triggered_by=triggered_by,
        requested_generation=state.dirty_generation,
        source_sync_run_id=source_sync_run_id,
    )
    db.add(run)
    await db.flush()
    state.active_palace_run_id = run.id
    state.active_generation = run.requested_generation
    await db.commit()
    await db.refresh(run)
    logger.info(
        "palace run lease created tenant=%s trigger=%s run_id=%s requested_generation=%s source_sync_run_id=%s",
        tenant_id,
        triggered_by,
        run.id,
        run.requested_generation,
        source_sync_run_id,
    )
    return run, True


async def list_sync_sources(
    db: AsyncSession,
    tenant_id: str,
    *,
    include_disabled: bool = False,
) -> list[SyncSourceSummary]:
    query = select(SyncSource).where(SyncSource.tenant_id == tenant_id)
    if not include_disabled:
        query = query.where(SyncSource.status == "active")
    rows = (
        await db.execute(
            query.order_by(SyncSource.created_at.desc())
        )
    ).scalars().all()
    return [_sync_source_summary(row) for row in rows]


async def list_sync_runs(db: AsyncSession, tenant_id: str, *, limit: int = 20) -> list[SyncRunSummary]:
    rows = (
        await db.execute(
            select(SyncRun, SyncSource.name)
            .join(SyncSource, SyncSource.id == SyncRun.sync_source_id)
            .where(SyncRun.tenant_id == tenant_id)
            .order_by(SyncRun.started_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        SyncRunSummary(
            id=run.id,
            sync_source_id=run.sync_source_id,
            sync_source_name=name,
            status="running" if run.status == "running" else run.status,
            triggered_by=run.triggered_by,
            files_seen=run.files_seen,
            files_changed=run.files_changed,
            files_skipped=run.files_skipped,
            items_created=run.items_created,
            items_updated=run.items_updated,
            items_failed=run.items_failed,
            generation=run.generation,
            error_message=run.error_message,
            started_at=run.started_at,
            completed_at=run.completed_at,
        )
        for run, name in rows
    ]


async def list_palace_runs(db: AsyncSession, tenant_id: str, *, limit: int = 20) -> list[PalaceRunSummary]:
    rows = (
        await db.execute(
            select(PalaceRun)
            .where(PalaceRun.tenant_id == tenant_id)
            .order_by(PalaceRun.started_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        PalaceRunSummary(
            id=row.id,
            status=row.status,
            triggered_by=row.triggered_by,
            requested_generation=row.requested_generation,
            applied_generation=row.applied_generation,
            attempt=row.attempt,
            error_message=row.error_message,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]


async def _build_memory_health(db: AsyncSession, tenant_id: str) -> PalaceMemoryHealthSummary:
    counts_rows = (
        await db.execute(
            select(Job.status, func.count(Job.id))
            .where(Job.tenant_id == tenant_id)
            .where(Job.job_type == MEMORY_JOB_TYPE)
            .group_by(Job.status)
        )
    ).all()
    counts = {status: count for status, count in counts_rows}

    recent_rows = (
        await db.execute(
            select(Job, Item.title, Item.metadata_)
            .outerjoin(Item, Job.item_id == Item.id)
            .where(Job.tenant_id == tenant_id)
            .where(Job.job_type == MEMORY_JOB_TYPE)
            .order_by(Job.created_at.desc())
            .limit(8)
        )
    ).all()
    recent_job_ids = [job.id for job, _item_title, _metadata in recent_rows]
    event_rows = (
        await db.execute(
            select(JobProgressEvent)
            .where(JobProgressEvent.tenant_id == tenant_id)
            .where(JobProgressEvent.job_id.in_(recent_job_ids))
            .order_by(JobProgressEvent.job_id.asc(), JobProgressEvent.created_at.desc())
        )
    ).scalars().all() if recent_job_ids else []
    events_by_job: dict[uuid.UUID, list[JobProgressEventResponse]] = {job_id: [] for job_id in recent_job_ids}
    for event in event_rows:
        bucket = events_by_job.setdefault(event.job_id, [])
        if len(bucket) < 3:
            bucket.append(JobProgressEventResponse.model_validate(event))

    recent_jobs: list[PalaceMemoryJobSummary] = []
    for job, item_title, metadata in recent_rows:
        payload = job.payload or {}
        memory_entry = metadata.get("memory_entry", {}) if isinstance(metadata, dict) else {}
        raw_scope_type = payload.get("scope_type")
        scope_type = raw_scope_type if raw_scope_type in _MEMORY_SCOPE_TYPES else "tenant_shared"
        scope_key = payload.get("scope_key") if scope_type != "tenant_shared" else None
        accepted_as = payload.get("accepted_as")
        recent_jobs.append(
            PalaceMemoryJobSummary(
                job_id=job.id,
                title=str(item_title or "Untitled memory"),
                status=_MEMORY_JOB_PUBLIC_STATUS_MAP.get(job.status, job.status),
                scope=PalaceMemoryJobScope(type=scope_type, key=scope_key),
                accepted_as=accepted_as if accepted_as in {"canonical", "legacy_artifact"} else None,
                retriable=job.status in {"failed", "cancelled"},
                source=memory_entry.get("source"),
                error_message=job.error_message,
                created_at=job.created_at,
                completed_at=job.completed_at,
                recent_progress_events=events_by_job.get(job.id, []),
            )
        )

    return PalaceMemoryHealthSummary(
        queued=counts.get("queued", 0),
        processing=counts.get("processing", 0),
        failed=counts.get("failed", 0),
        retryable=counts.get("failed", 0) + counts.get("cancelled", 0),
        recent_jobs=recent_jobs,
    )


async def _build_webhook_health(db: AsyncSession, tenant_id: str) -> PalaceWebhookHealthSummary:
    webhook_filter = and_(Job.tenant_id == tenant_id, Job.webhook_url.isnot(None))
    counts_rows = (
        await db.execute(
            select(Job.status, func.count(Job.id))
            .where(webhook_filter)
            .group_by(Job.status)
        )
    ).all()
    counts = {status: count for status, count in counts_rows}

    recent_rows = (
        await db.execute(
            select(Job, Item.title)
            .outerjoin(Item, Job.item_id == Item.id)
            .where(webhook_filter)
            .order_by(Job.created_at.desc())
            .limit(8)
        )
    ).all()

    recent_jobs = [
        PalaceWebhookJobSummary(
            job_id=job.id,
            title=str(item_title or f"{job.job_type} job"),
            job_type=job.job_type,
            status=_MEMORY_JOB_PUBLIC_STATUS_MAP.get(job.status, job.status),
            terminal=job.status in _WEBHOOK_TERMINAL_STATUSES,
            error_message=job.error_message,
            created_at=job.created_at,
            completed_at=job.completed_at,
        )
        for job, item_title in recent_rows
    ]

    pending = counts.get("queued", 0) + counts.get("processing", 0)
    failed_jobs = counts.get("failed", 0)
    retryable_jobs = failed_jobs + counts.get("cancelled", 0)
    terminal = sum(counts.get(status, 0) for status in _WEBHOOK_TERMINAL_STATUSES)

    return PalaceWebhookHealthSummary(
        configured=sum(counts.values()),
        pending=pending,
        terminal=terminal,
        failed_jobs=failed_jobs,
        retryable_jobs=retryable_jobs,
        recent_jobs=recent_jobs,
    )


async def _build_mcp_activity(db: AsyncSession, tenant_id: str) -> PalaceMcpActivitySummary:
    client_count = await db.scalar(
        sa_text("SELECT COUNT(*) FROM mcp_clients WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    count_rows = (
        await db.execute(
            sa_text(
                "SELECT status, COUNT(*) AS count "
                "FROM mcp_request_audit_events "
                "WHERE tenant_id = :tenant_id "
                "GROUP BY status"
            ),
            {"tenant_id": tenant_id},
        )
    ).mappings().all()
    counts = {row["status"]: int(row["count"]) for row in count_rows}
    event_rows = (
        await db.execute(
            sa_text(
                "SELECT id, client_name, client_key, operation, required_scope, params_summary, "
                "status, latency_ms, error_class, created_at "
                "FROM mcp_request_audit_events "
                "WHERE tenant_id = :tenant_id "
                "ORDER BY created_at DESC "
                "LIMIT 10"
            ),
            {"tenant_id": tenant_id},
        )
    ).mappings().all()
    return PalaceMcpActivitySummary(
        registered_clients=int(client_count or 0),
        recent_success=counts.get("success", 0),
        recent_error=counts.get("error", 0),
        recent_denied=counts.get("denied", 0),
        recent_events=[
            PalaceMcpActivityEvent(
                id=row["id"],
                client_name=row["client_name"],
                client_key=row["client_key"],
                operation=row["operation"],
                required_scope=row["required_scope"],
                status=row["status"],
                latency_ms=row["latency_ms"],
                error_class=row["error_class"],
                params_summary=row["params_summary"] or {},
                created_at=row["created_at"],
            )
            for row in event_rows
        ],
    )


async def build_control_tower(db: AsyncSession, tenant_id: str, arq_pool=None) -> PalaceControlTower:
    state = await ensure_tenant_state(db, tenant_id)
    active_run = None
    if state.active_palace_run_id:
        row = await db.get(PalaceRun, state.active_palace_run_id)
        if row:
            active_run = PalaceRunSummary(
                id=row.id,
                status=row.status,
                triggered_by=row.triggered_by,
                requested_generation=row.requested_generation,
                applied_generation=row.applied_generation,
                attempt=row.attempt,
                error_message=row.error_message,
                started_at=row.started_at,
                completed_at=row.completed_at,
            )

    return PalaceControlTower(
        tenant_id=tenant_id,
        dirty_generation=state.dirty_generation,
        indexed_generation=state.indexed_generation,
        backlog_generation=max(state.dirty_generation - state.indexed_generation, 0),
        active_palace_run=active_run,
        room_artifacts=await build_room_artifact_health(db, tenant_id=tenant_id, state=state),
        consolidation=await find_consolidation_candidates(db, tenant_id=tenant_id),
        worker_backpressure=await build_worker_backpressure(arq_pool, db=db),
        mcp_activity=await _build_mcp_activity(db, tenant_id),
        memory_health=await _build_memory_health(db, tenant_id),
        webhook_health=await _build_webhook_health(db, tenant_id),
        fact_registry=PalaceFactRegistrySummary.model_validate(
            await build_fact_registry_summary(db, tenant_id=tenant_id)
        ),
        diary_rollups=PalaceDiaryRollupSummary.model_validate(
            await build_diary_rollup_summary(db, tenant_id=tenant_id)
        ),
        wakeup_briefs=PalaceWakeupBriefSummary.model_validate(
            await build_wakeup_brief_summary(
                db,
                tenant_id=tenant_id,
                indexed_generation=state.indexed_generation,
            )
        ),
        source_trust_health=await _build_control_tower_source_trust_health(db, tenant_id),
        sync_sources=await list_sync_sources(db, tenant_id),
        sync_runs=await list_sync_runs(db, tenant_id, limit=8),
        palace_runs=await list_palace_runs(db, tenant_id, limit=8),
    )


async def _build_control_tower_source_trust_health(
    db: AsyncSession,
    tenant_id: str,
) -> PalaceSourceTrustHealthSummary:
    try:
        summary = await build_source_trust_health_summary(db, tenant_id=tenant_id)
        return PalaceSourceTrustHealthSummary(
            status=summary.status,
            total_contexts=summary.total_contexts,
            source_backed=summary.source_backed,
            generated_unpromoted=summary.generated_unpromoted,
            stale_missing=summary.stale_missing,
            policy_limited=summary.policy_limited,
            unknown=summary.unknown,
            recent_warnings=[warning.__dict__ for warning in summary.recent_warnings or []],
        )
    except Exception:
        logger.exception("control tower source trust health summary failed")
        return PalaceSourceTrustHealthSummary(
            status="error",
            error_message="Source trust counts failed; MCP wakeup remains usable.",
        )


async def build_room_artifact_health(
    db: AsyncSession,
    *,
    tenant_id: str,
    state: PalaceTenantState | None = None,
) -> PalaceRoomArtifactHealthSummary:
    state = state or await ensure_tenant_state(db, tenant_id)
    target_generation = state.indexed_generation
    active_room_ids = (
        await db.execute(
            select(Room.id)
            .where(Room.tenant_id == tenant_id)
            .where(Room.state == "active")
            .order_by(Room.updated_at.asc(), Room.id.asc())
        )
    ).scalars().all()
    active_rooms = len(active_room_ids)
    if target_generation <= 0 or active_rooms == 0:
        return PalaceRoomArtifactHealthSummary(
            target_generation=target_generation,
            active_rooms=active_rooms,
        )

    repair_plan = await _repairable_room_artifacts(
        db,
        tenant_id=tenant_id,
        target_generation=target_generation,
    )
    blocked_rooms = len(repair_plan.blocked_room_ids)
    blocked_room_samples: list[PalaceRoomArtifactBlocker] = []
    if repair_plan.blocked_room_ids:
        rows = (
            await db.execute(
                select(
                    Room.id,
                    Room.name,
                    Room.stable_key,
                    Room.membership_generation,
                    Room.closet_generation,
                    Room.snapshot_generation,
                    Room.tunnel_generation,
                    Wing.name,
                )
                .join(Wing, Wing.id == Room.wing_id)
                .where(Room.id.in_(repair_plan.blocked_room_ids))
                .order_by(Room.updated_at.asc(), Room.id.asc())
                .limit(5)
            )
        ).all()
        blocked_room_samples = [
            PalaceRoomArtifactBlocker(
                room_id=room_id,
                room_name=room_name,
                room_stable_key=stable_key,
                wing_name=wing_name,
                membership_generation=membership_generation,
                closet_generation=closet_generation,
                snapshot_generation=snapshot_generation,
                tunnel_generation=tunnel_generation,
            )
            for (
                room_id,
                room_name,
                stable_key,
                membership_generation,
                closet_generation,
                snapshot_generation,
                tunnel_generation,
                wing_name,
            ) in rows
        ]

    def section(stale_count: int) -> PalaceArtifactSectionHealth:
        return PalaceArtifactSectionHealth(
            fresh=max(active_rooms - blocked_rooms - stale_count, 0),
            stale=stale_count,
        )

    return PalaceRoomArtifactHealthSummary(
        target_generation=target_generation,
        active_rooms=active_rooms,
        blocked_rooms=blocked_rooms,
        blocked_room_samples=blocked_room_samples,
        closets=section(len(repair_plan.closet_room_ids)),
        snapshots=section(len(repair_plan.snapshot_room_ids)),
        tunnels=section(len(repair_plan.tunnel_room_ids)),
    )


async def _room_counts(db: AsyncSession, tenant_id: str) -> dict[uuid.UUID, int]:
    rows = (
        await db.execute(
            select(RoomMembership.room_id, func.count(RoomMembership.id))
            .where(RoomMembership.tenant_id == tenant_id)
            .group_by(RoomMembership.room_id)
        )
    ).all()
    return {room_id: count for room_id, count in rows}


async def _latest_snapshots(db: AsyncSession, tenant_id: str) -> dict[uuid.UUID, RoomSnapshot]:
    rows = (
        await db.execute(
            select(RoomSnapshot)
            .where(RoomSnapshot.tenant_id == tenant_id)
            .order_by(RoomSnapshot.room_id, RoomSnapshot.generation.desc())
        )
    ).scalars().all()
    latest: dict[uuid.UUID, RoomSnapshot] = {}
    for row in rows:
        latest.setdefault(row.room_id, row)
    return latest


async def _latest_room_closets(db: AsyncSession, tenant_id: str) -> dict[uuid.UUID, RoomClosetArtifact]:
    rows = (
        await db.execute(
            select(RoomClosetArtifact)
            .where(RoomClosetArtifact.tenant_id == tenant_id)
            .order_by(RoomClosetArtifact.room_id, RoomClosetArtifact.generation.desc())
        )
    ).scalars().all()
    latest: dict[uuid.UUID, RoomClosetArtifact] = {}
    for row in rows:
        latest.setdefault(row.room_id, row)
    return latest


def _drawer_ref_item_ids(drawer_refs: list[dict[str, object]] | None) -> frozenset[uuid.UUID]:
    item_ids: set[uuid.UUID] = set()
    for ref in drawer_refs or []:
        raw_id = ref.get("item_id")
        if raw_id is None:
            continue
        try:
            item_ids.add(uuid.UUID(str(raw_id)))
        except ValueError:
            continue
    return frozenset(item_ids)


def _normalized_tag_counts(tag_profile: dict[str, int] | None) -> dict[str, int]:
    normalized: Counter[str] = Counter()
    for tag, count in (tag_profile or {}).items():
        token = str(tag).strip().lower()
        if not token:
            continue
        try:
            weight = int(count)
        except (TypeError, ValueError):
            weight = 1
        normalized[token] += max(weight, 1)
    return dict(normalized)


def _jaccard(left: set[str] | frozenset[uuid.UUID], right: set[str] | frozenset[uuid.UUID]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _weighted_tag_overlap(left: dict[str, int], right: dict[str, int]) -> float:
    if not left or not right:
        return 0.0
    shared = set(left) & set(right)
    all_tags = set(left) | set(right)
    shared_weight = sum(min(left[tag], right[tag]) for tag in shared)
    total_weight = sum(max(left.get(tag, 0), right.get(tag, 0)) for tag in all_tags)
    return shared_weight / total_weight if total_weight else 0.0


def _consolidation_candidate_signature(candidate: PalaceConsolidationCandidate) -> str:
    room_ids = sorted([str(candidate.room_id), str(candidate.candidate_room_id)])
    return f"{candidate.wing_id}:{room_ids[0]}:{room_ids[1]}"


def _score_consolidation_pair(
    left: _RoomConsolidationProfile,
    right: _RoomConsolidationProfile,
) -> PalaceConsolidationCandidate | None:
    name_similarity = SequenceMatcher(None, left.room_slug, right.room_slug).ratio()
    token_overlap = _jaccard(_tokenize(left.room_name), _tokenize(right.room_name))
    tag_overlap = _weighted_tag_overlap(left.tag_counts, right.tag_counts)
    drawer_overlap = _jaccard(left.item_ids, right.item_ids)
    score = round(
        max(
            name_similarity * 0.72 + tag_overlap * 0.18 + drawer_overlap * 0.10,
            drawer_overlap * 0.70 + tag_overlap * 0.20 + name_similarity * 0.10,
            tag_overlap * 0.58 + token_overlap * 0.27 + drawer_overlap * 0.15,
        ),
        3,
    )

    reasons: list[str] = []
    if name_similarity >= 0.82:
        reasons.append("very similar room names")
    elif token_overlap >= 0.5:
        reasons.append("overlapping room-name tokens")
    if drawer_overlap >= 0.2:
        reasons.append("shared drawer references")
    if tag_overlap >= 0.45:
        reasons.append("overlapping closet tag profiles")

    if score < CONSOLIDATION_CANDIDATE_SCORE_THRESHOLD or not reasons:
        return None

    shared_tags = sorted((set(left.tag_counts) & set(right.tag_counts)))[:8]
    shared_drawer_item_ids = sorted(left.item_ids & right.item_ids, key=str)[:8]
    return PalaceConsolidationCandidate(
        room_id=left.room_id,
        room_name=left.room_name,
        room_stable_key=left.room_stable_key,
        candidate_room_id=right.room_id,
        candidate_room_name=right.room_name,
        candidate_stable_key=right.room_stable_key,
        wing_id=left.wing_id,
        wing_name=left.wing_name,
        score=score,
        reasons=reasons,
        shared_tags=shared_tags,
        shared_drawer_item_ids=shared_drawer_item_ids,
    )


async def find_consolidation_candidates(
    db: AsyncSession,
    *,
    tenant_id: str,
    limit: int = CONSOLIDATION_CANDIDATE_LIMIT,
) -> PalaceConsolidationSummary:
    rooms = (
        await db.execute(
            select(Room, Wing.name)
            .join(Wing, Wing.id == Room.wing_id)
            .where(Room.tenant_id == tenant_id)
            .where(Room.state == "active")
            .order_by(Wing.name.asc(), Room.name.asc(), Room.id.asc())
        )
    ).all()
    if len(rooms) < 2:
        return PalaceConsolidationSummary()

    closets = await _latest_room_closets(db, tenant_id)
    membership_rows = (
        await db.execute(
            select(RoomMembership.room_id, RoomMembership.item_id)
            .where(RoomMembership.tenant_id == tenant_id)
            .order_by(RoomMembership.room_id.asc(), RoomMembership.item_id.asc())
        )
    ).all()
    membership_item_ids: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for room_id, item_id in membership_rows:
        membership_item_ids[room_id].add(item_id)

    profiles_by_wing: dict[uuid.UUID, list[_RoomConsolidationProfile]] = defaultdict(list)
    for room, wing_name in rooms:
        closet = closets.get(room.id)
        drawer_item_ids = _drawer_ref_item_ids(closet.drawer_refs if closet else None)
        item_ids = drawer_item_ids or frozenset(membership_item_ids.get(room.id, set()))
        profile = _RoomConsolidationProfile(
            room_id=room.id,
            room_name=room.name,
            room_stable_key=room.stable_key,
            room_slug=room.slug or slugify(room.name),
            wing_id=room.wing_id,
            wing_name=wing_name,
            item_ids=item_ids,
            tag_counts=_normalized_tag_counts(closet.tag_profile if closet else None),
        )
        profiles_by_wing[room.wing_id].append(profile)

    candidates: list[PalaceConsolidationCandidate] = []
    for profiles in profiles_by_wing.values():
        for index, left in enumerate(profiles):
            for right in profiles[index + 1 :]:
                candidate = _score_consolidation_pair(left, right)
                if candidate is not None:
                    candidates.append(candidate)

    candidates.sort(
        key=lambda candidate: (
            -candidate.score,
            candidate.wing_name.lower(),
            candidate.room_name.lower(),
            candidate.candidate_room_name.lower(),
            str(candidate.room_id),
        )
    )
    return PalaceConsolidationSummary(candidate_count=len(candidates), candidates=candidates[:limit])


async def record_consolidation_candidate_events(
    db: AsyncSession,
    *,
    tenant_id: str,
    limit: int = CONSOLIDATION_CANDIDATE_LIMIT,
) -> PalaceConsolidationSummary:
    summary = await find_consolidation_candidates(db, tenant_id=tenant_id, limit=limit)
    if not summary.candidates:
        return summary

    existing_events = (
        await db.execute(
            select(PalaceRoomEvent)
            .where(PalaceRoomEvent.tenant_id == tenant_id)
            .where(PalaceRoomEvent.event_type == CONSOLIDATION_CANDIDATE_EVENT)
        )
    ).scalars().all()
    existing_signatures = {
        str((event.payload or {}).get("candidate_signature", ""))
        for event in existing_events
        if isinstance(event.payload, dict)
    }

    created = False
    for candidate in summary.candidates:
        signature = _consolidation_candidate_signature(candidate)
        if signature in existing_signatures:
            continue
        db.add(
            PalaceRoomEvent(
                tenant_id=tenant_id,
                room_id=candidate.room_id,
                event_type=CONSOLIDATION_CANDIDATE_EVENT,
                payload={
                    "candidate_signature": signature,
                    "candidate_room_id": str(candidate.candidate_room_id),
                    "score": candidate.score,
                    "reasons": candidate.reasons,
                    "shared_tags": candidate.shared_tags,
                    "shared_drawer_item_ids": [str(item_id) for item_id in candidate.shared_drawer_item_ids],
                    "non_destructive": True,
                },
            )
        )
        existing_signatures.add(signature)
        created = True

    if created:
        await db.commit()
    return summary


async def _repairable_room_artifacts(
    db: AsyncSession,
    *,
    tenant_id: str,
    target_generation: int,
) -> PalaceArtifactRepairPlan:
    rooms = (
        await db.execute(
            select(
                Room.id,
                Room.membership_generation,
                Room.closet_generation,
                Room.snapshot_generation,
                Room.tunnel_generation,
                Room.retrieval_hint_generation,
                RoomClosetArtifact.id,
                RoomSnapshot.id,
                RetrievalHintArtifact.id,
            )
            .outerjoin(
                RoomClosetArtifact,
                and_(
                    RoomClosetArtifact.room_id == Room.id,
                    RoomClosetArtifact.tenant_id == tenant_id,
                    RoomClosetArtifact.generation == Room.closet_generation,
                ),
            )
            .outerjoin(
                RoomSnapshot,
                and_(
                    RoomSnapshot.room_id == Room.id,
                    RoomSnapshot.tenant_id == tenant_id,
                    RoomSnapshot.generation == Room.snapshot_generation,
                ),
            )
            .outerjoin(
                RetrievalHintArtifact,
                and_(
                    RetrievalHintArtifact.room_id == Room.id,
                    RetrievalHintArtifact.tenant_id == tenant_id,
                    RetrievalHintArtifact.generation == Room.retrieval_hint_generation,
                ),
            )
            .where(Room.tenant_id == tenant_id)
            .where(Room.state == "active")
            .where(
                (Room.membership_generation > target_generation)
                | (Room.closet_generation < Room.membership_generation)
                | (Room.snapshot_generation < Room.membership_generation)
                | (Room.tunnel_generation < Room.membership_generation)
                | (
                    (Room.closet_generation > 0)
                    & RoomClosetArtifact.id.is_(None)
                )
                | (
                    (Room.snapshot_generation > 0)
                    & RoomSnapshot.id.is_(None)
                )
                | (
                    (Room.retrieval_hint_generation > 0)
                    & RetrievalHintArtifact.id.is_(None)
                )
            )
            .order_by(Room.updated_at.asc(), Room.id.asc())
        )
    ).all()

    snapshot_room_ids: list[uuid.UUID] = []
    tunnel_room_ids: list[uuid.UUID] = []
    blocked_room_ids: list[uuid.UUID] = []
    closet_room_ids: list[uuid.UUID] = []
    retrieval_hint_room_ids: list[uuid.UUID] = []

    for (
        room_id,
        membership_generation,
        closet_generation,
        snapshot_generation,
        tunnel_generation,
        retrieval_hint_generation,
        closet_id,
        snapshot_id,
        retrieval_hint_id,
    ) in rooms:
        if membership_generation > target_generation:
            blocked_room_ids.append(room_id)
            continue
        if closet_generation < membership_generation or (closet_generation > 0 and closet_id is None):
            closet_room_ids.append(room_id)
        if snapshot_generation < membership_generation or (snapshot_generation > 0 and snapshot_id is None):
            snapshot_room_ids.append(room_id)
        if tunnel_generation < membership_generation:
            tunnel_room_ids.append(room_id)
        if retrieval_hint_generation < membership_generation or (
            retrieval_hint_generation > 0 and retrieval_hint_id is None
        ):
            retrieval_hint_room_ids.append(room_id)

    return PalaceArtifactRepairPlan(
        snapshot_room_ids=tuple(snapshot_room_ids),
        tunnel_room_ids=tuple(tunnel_room_ids),
        blocked_room_ids=tuple(blocked_room_ids),
        closet_room_ids=tuple(closet_room_ids),
        retrieval_hint_room_ids=tuple(retrieval_hint_room_ids),
    )


async def repair_stale_room_artifacts(
    db: AsyncSession,
    *,
    tenant_id: str,
    target_generation: int | None = None,
) -> PalaceArtifactRepairPlan:
    state = await ensure_tenant_state(db, tenant_id)
    generation = target_generation if target_generation is not None else state.indexed_generation
    if generation <= 0:
        return PalaceArtifactRepairPlan(snapshot_room_ids=(), tunnel_room_ids=(), blocked_room_ids=())

    repair_plan = await _repairable_room_artifacts(
        db,
        tenant_id=tenant_id,
        target_generation=generation,
    )

    for room_id in repair_plan.closet_room_ids:
        await _rebuild_room_closet_artifact(
            db,
            tenant_id=tenant_id,
            room_id=room_id,
            generation=generation,
        )

    for room_id in repair_plan.retrieval_hint_room_ids:
        await rebuild_room_retrieval_hints(
            db,
            tenant_id=tenant_id,
            room_id=room_id,
            generation=generation,
        )

    for room_id in repair_plan.snapshot_room_ids:
        await _rebuild_room_snapshot(
            db,
            tenant_id=tenant_id,
            room_id=room_id,
            generation=generation,
        )

    tunnel_room_ids = set(repair_plan.tunnel_room_ids)
    if tunnel_room_ids:
        await _rebuild_tunnels(
            db,
            tenant_id=tenant_id,
            room_ids=tunnel_room_ids,
            generation=generation,
        )

    if (
        repair_plan.closet_room_ids
        or repair_plan.retrieval_hint_room_ids
        or repair_plan.snapshot_room_ids
        or repair_plan.tunnel_room_ids
    ):
        await db.commit()

    return repair_plan


async def inspect_palace_index_integrity(
    db: AsyncSession,
    *,
    tenant_id: str,
    target_generation: int | None = None,
) -> PalaceIndexIntegrityPlan:
    state = await ensure_tenant_state(db, tenant_id)
    generation = target_generation if target_generation is not None else state.indexed_generation

    missing_embedding_rows = (
        await db.execute(
            select(Item.id)
            .outerjoin(Embedding, Embedding.item_id == Item.id)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
            .where(Item.raw_content.is_not(None))
            .group_by(Item.id)
            .having(func.count(Embedding.item_id) == 0)
            .order_by(Item.updated_at.asc(), Item.id.asc())
        )
    ).all()
    missing_embedding_item_ids = tuple(row[0] for row in missing_embedding_rows)

    missing_membership_rows = (
        await db.execute(
            select(Item.id)
            .outerjoin(
                RoomMembership,
                and_(
                    RoomMembership.item_id == Item.id,
                    RoomMembership.tenant_id == tenant_id,
                ),
            )
            .outerjoin(
                PalaceDirtyItem,
                and_(
                    PalaceDirtyItem.item_id == Item.id,
                    PalaceDirtyItem.tenant_id == tenant_id,
                ),
            )
            .where(Item.tenant_id == tenant_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
            .group_by(Item.id)
            .having(func.count(RoomMembership.id) == 0)
            .having(func.count(PalaceDirtyItem.id) == 0)
            .order_by(Item.updated_at.asc(), Item.id.asc())
        )
    ).all()
    missing_membership_item_ids = tuple(row[0] for row in missing_membership_rows)

    artifact_repair_plan = PalaceArtifactRepairPlan(
        snapshot_room_ids=(),
        tunnel_room_ids=(),
        blocked_room_ids=(),
    )
    if generation > 0:
        artifact_repair_plan = await _repairable_room_artifacts(
            db,
            tenant_id=tenant_id,
            target_generation=generation,
        )

    return PalaceIndexIntegrityPlan(
        missing_embedding_item_ids=missing_embedding_item_ids,
        missing_membership_item_ids=missing_membership_item_ids,
        artifact_repair_plan=artifact_repair_plan,
    )


async def build_overview(db: AsyncSession, tenant_id: str) -> PalaceOverview:
    state = await ensure_tenant_state(db, tenant_id)
    wings = (
        await db.execute(
            select(Wing)
            .where(Wing.tenant_id == tenant_id)
            .order_by(Wing.name.asc())
        )
    ).scalars().all()
    rooms = (
        await db.execute(
            select(Room)
            .where(Room.tenant_id == tenant_id)
            .order_by(Room.name.asc())
        )
    ).scalars().all()
    room_counts = await _room_counts(db, tenant_id)
    snapshots = await _latest_snapshots(db, tenant_id)
    rooms_by_wing: dict[uuid.UUID, list[PalaceRoomSummary]] = defaultdict(list)

    for room in rooms:
        snapshot = snapshots.get(room.id)
        item_count = room_counts.get(room.id, snapshot.item_count if snapshot else 0)
        membership_status, snapshot_status, tunnel_status = _room_artifact_statuses(room, state)
        rooms_by_wing[room.wing_id].append(
            PalaceRoomSummary(
                id=room.id,
                wing_id=room.wing_id,
                name=room.name,
                stable_key=room.stable_key,
                state=room.state,
                item_count=item_count,
                summary=snapshot.summary if snapshot else None,
                membership_status=membership_status,
                snapshot_status=snapshot_status,
                tunnel_status=tunnel_status,
                redirect_room_id=room.redirect_room_id,
            )
        )

    active_run = None
    if state.active_palace_run_id:
        row = await db.get(PalaceRun, state.active_palace_run_id)
        if row:
            active_run = PalaceRunSummary(
                id=row.id,
                status=row.status,
                triggered_by=row.triggered_by,
                requested_generation=row.requested_generation,
                applied_generation=row.applied_generation,
                attempt=row.attempt,
                error_message=row.error_message,
                started_at=row.started_at,
                completed_at=row.completed_at,
            )

    latest_sync_runs = await list_sync_runs(db, tenant_id, limit=3)
    state_banner = None
    if latest_sync_runs and latest_sync_runs[0].status == "failed":
        state_banner = PalaceStateBanner(
            kind="stale",
            message="Last sync run failed. Palace is showing the last confirmed structure.",
            detail=latest_sync_runs[0].error_message,
        )
    elif state.dirty_generation > state.indexed_generation:
        state_banner = PalaceStateBanner(
            kind="indexing",
            message="New changes are waiting to be indexed into Palace.",
            detail=f"Backlog generation {state.indexed_generation + 1} to {state.dirty_generation}.",
        )

    wing_summaries: list[PalaceWingSummary] = []
    for wing in wings:
        wing_rooms = rooms_by_wing.get(wing.id, [])
        wing_summaries.append(
            PalaceWingSummary(
                id=wing.id,
                slug=wing.slug,
                name=wing.name,
                room_count=len(wing_rooms),
                item_count=sum(room.item_count for room in wing_rooms),
                rooms=wing_rooms,
            )
        )

    return PalaceOverview(
        tenant_id=tenant_id,
        dirty_generation=state.dirty_generation,
        indexed_generation=state.indexed_generation,
        backlog_generation=max(state.dirty_generation - state.indexed_generation, 0),
        active_palace_run=active_run,
        latest_sync_runs=latest_sync_runs,
        state_banner=state_banner,
        wings=wing_summaries,
    )


async def resolve_room(db: AsyncSession, tenant_id: str, room_id: uuid.UUID) -> RoutedRoom:
    room = await db.get(Room, room_id)
    if room is None or room.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Room not found")
    redirected_from = None
    seen = {room.id}
    while room.redirect_room_id:
        redirected_from = redirected_from or room.id
        next_room = await db.get(Room, room.redirect_room_id)
        if next_room is None or next_room.tenant_id != tenant_id or next_room.id in seen:
            break
        seen.add(next_room.id)
        room = next_room
    return RoutedRoom(room=room, redirected_from_room_id=redirected_from)


async def get_room_detail(db: AsyncSession, tenant_id: str, room_id: uuid.UUID) -> PalaceRoomDetail:
    state = await ensure_tenant_state(db, tenant_id)
    resolved = await resolve_room(db, tenant_id, room_id)
    room = resolved.room
    assert room is not None
    wing = await db.get(Wing, room.wing_id)
    snapshots = await _latest_snapshots(db, tenant_id)
    snapshot = snapshots.get(room.id)
    item_count = (
        await db.execute(
            select(func.count(RoomMembership.id))
            .where(RoomMembership.tenant_id == tenant_id)
            .where(RoomMembership.room_id == room.id)
        )
    ).scalar_one()

    memberships = (
        await db.execute(
            select(RoomMembership, Item)
            .join(Item, Item.id == RoomMembership.item_id)
            .where(RoomMembership.tenant_id == tenant_id)
            .where(RoomMembership.room_id == room.id)
            .order_by(RoomMembership.source.desc(), Item.created_at.desc())
        )
    ).all()
    representative_items: list[PalaceRepresentativeItem] = []
    membership_items: list[PalaceMembershipDetail] = []
    seen_items: set[uuid.UUID] = set()
    for membership, item in memberships:
        detail = PalaceMembershipDetail(
            item_id=item.id,
            title=item.title,
            source_type=item.source_type,
            summary=item.summary,
            membership_source="pinned" if membership.source == "pinned" else "auto",
            membership_kind=membership.membership_kind,
            pinned=membership.source == "pinned",
        )
        membership_items.append(detail)
        if item.id not in seen_items and len(representative_items) < 6:
            seen_items.add(item.id)
            representative_items.append(
                PalaceRepresentativeItem(
                    item_id=item.id,
                    title=item.title,
                    source_type=item.source_type,
                    summary=item.summary,
                    membership_source=detail.membership_source,
                    pinned=detail.pinned,
                )
            )

    tunnel_rows = (
        await db.execute(
            select(RoomTunnel, Room)
            .join(Room, Room.id == RoomTunnel.target_room_id)
            .where(RoomTunnel.tenant_id == tenant_id)
            .where(RoomTunnel.source_room_id == room.id)
            .order_by(RoomTunnel.strength.desc())
            .limit(6)
        )
    ).all()
    tunnels = [
        PalaceTunnelSummary(
            room_id=target_room.id,
            room_name=target_room.name,
            strength=tunnel.strength,
            tunnel_type=tunnel.tunnel_type,
            activation_count=tunnel.activation_count,
            stability=tunnel.stability,
            last_activated_at=tunnel.last_activated_at,
        )
        for tunnel, target_room in tunnel_rows
    ]

    banner = None
    redirect_target = None
    if resolved.redirected_from_room_id:
        original = await db.get(Room, resolved.redirected_from_room_id)
        if original:
            membership_status, snapshot_status, tunnel_status = _room_artifact_statuses(room, state)
            banner = PalaceStateBanner(
                kind="redirected",
                message=f"{original.name} now redirects to {room.name}.",
                detail="Using the newest room lineage target.",
            )
            redirect_target = PalaceRoomSummary(
                id=room.id,
                wing_id=room.wing_id,
                name=room.name,
                stable_key=room.stable_key,
                state=room.state,
                item_count=item_count,
                summary=snapshot.summary if snapshot else None,
                membership_status=membership_status,
                snapshot_status=snapshot_status,
                tunnel_status=tunnel_status,
                redirect_room_id=room.redirect_room_id,
            )

    membership_status, snapshot_status, tunnel_status = _room_artifact_statuses(room, state)
    room_summary = PalaceRoomSummary(
        id=room.id,
        wing_id=room.wing_id,
        name=room.name,
        stable_key=room.stable_key,
        state=room.state,
        item_count=item_count,
        summary=snapshot.summary if snapshot else None,
        membership_status=membership_status,
        snapshot_status=snapshot_status,
        tunnel_status=tunnel_status,
        redirect_room_id=room.redirect_room_id,
    )

    return PalaceRoomDetail(
        room=room_summary,
        wing_name=wing.name if wing else "General",
        banner=banner,
        representative_items=representative_items,
        tunnels=tunnels,
        memberships=membership_items[:50],
        redirect_target=redirect_target,
    )


async def update_room(
    db: AsyncSession,
    *,
    tenant_id: str,
    room_id: uuid.UUID,
    body: PalaceRoomUpdate,
) -> PalaceRoomDetail:
    room = await db.get(Room, room_id)
    if room is None or room.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.state == "redirected":
        raise HTTPException(status_code=409, detail="Redirected rooms cannot be renamed")

    old_name = room.name
    if old_name != body.name:
        room.name = body.name
        room.slug = slugify(body.name)
        db.add(
            PalaceRoomEvent(
                tenant_id=tenant_id,
                room_id=room.id,
                event_type="rename",
                payload={
                    "old_name": old_name,
                    "new_name": body.name,
                    "stable_key": room.stable_key,
                },
            )
        )
        await db.commit()
    return await get_room_detail(db, tenant_id, room.id)


async def pin_room_membership(
    db: AsyncSession,
    *,
    tenant_id: str,
    room_id: uuid.UUID,
    body: PalacePinRequest,
) -> None:
    resolved = await resolve_room(db, tenant_id, room_id)
    room = resolved.room
    assert room is not None
    item = await db.get(Item, body.item_id)
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Item not found")

    existing = (
        await db.execute(
            select(RoomMembership)
            .where(RoomMembership.tenant_id == tenant_id)
            .where(RoomMembership.item_id == item.id)
            .where(RoomMembership.source == "pinned")
        )
    ).scalars().all()
    for membership in existing:
        membership.room_id = room.id
        membership.membership_kind = "primary"
    if not existing:
        db.add(
            RoomMembership(
                tenant_id=tenant_id,
                room_id=room.id,
                item_id=item.id,
                source="pinned",
                membership_kind="primary",
                confidence=1.0,
            )
        )
    db.add(
        PalaceRoomEvent(
            tenant_id=tenant_id,
            room_id=room.id,
            event_type="pin",
            payload={"item_id": str(item.id)},
        )
    )
    await mark_item_dirty(db, tenant_id=tenant_id, item_id=item.id, reason="curation")
    await db.commit()


async def unpin_room_membership(
    db: AsyncSession,
    *,
    tenant_id: str,
    room_id: uuid.UUID,
    item_id: uuid.UUID,
) -> None:
    resolved = await resolve_room(db, tenant_id, room_id)
    room = resolved.room
    assert room is not None
    membership = await db.scalar(
        select(RoomMembership)
        .where(RoomMembership.tenant_id == tenant_id)
        .where(RoomMembership.room_id == room.id)
        .where(RoomMembership.item_id == item_id)
        .where(RoomMembership.source == "pinned")
        .limit(1)
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Pinned membership not found")
    await db.delete(membership)
    db.add(
        PalaceRoomEvent(
            tenant_id=tenant_id,
            room_id=room.id,
            event_type="unpin",
            payload={"item_id": str(item_id)},
        )
    )
    await mark_item_dirty(db, tenant_id=tenant_id, item_id=item_id, reason="curation")
    await db.commit()


def _route_room_score(
    query: str,
    room_name: str,
    wing_name: str,
    summary: str | None,
    routing_terms: Iterable[str] = (),
    stable_key: str | None = None,
) -> float:
    query_tokens = _tokenize(query)
    for term in routing_terms:
        query_tokens.update(_tokenize(term))
    if not query_tokens:
        return 0.0
    room_tokens = _tokenize(room_name) | _tokenize(wing_name) | _tokenize(stable_key)
    corpus_tokens = room_tokens | _tokenize(summary)
    if not corpus_tokens:
        return 0.0
    corpus_overlap = len(query_tokens & corpus_tokens) / max(len(query_tokens), 1)
    room_overlap = len(query_tokens & room_tokens) / max(len(room_tokens), 1) if room_tokens else 0.0
    return max(corpus_overlap, room_overlap)


def _room_scope_match_rank(
    *,
    room: Room,
    wing_name: str,
    summary: str | None,
    scope_type: str,
    scope_key: str | None,
    routing_terms: Iterable[str] = (),
) -> int:
    if scope_type == "tenant_shared" or not scope_key:
        return 0
    expected_tokens = _tokenize(scope_key)
    if not expected_tokens:
        return 0

    room_name_tokens = _tokenize(getattr(room, "name", None))
    stable_key_tokens = _tokenize(getattr(room, "stable_key", None))
    slug_tokens = _tokenize(getattr(room, "slug", None))
    wing_tokens = _tokenize(wing_name)
    term_tokens: set[str] = set()
    for term in routing_terms:
        term_tokens.update(_tokenize(term))
    summary_tokens = _tokenize(summary)

    room_identity_tokens = room_name_tokens | stable_key_tokens | slug_tokens
    if expected_tokens <= room_identity_tokens:
        return 4
    if expected_tokens <= (room_identity_tokens | wing_tokens | term_tokens):
        return 3
    if expected_tokens & room_identity_tokens:
        return 2
    if expected_tokens & summary_tokens:
        return 1
    return 0


def _route_confidence(score: float | None) -> str:
    if score is None or score <= 0:
        return "none"
    if score < LOW_CONFIDENCE_ROUTE_SCORE:
        return "low"
    return "high"


def _routing_terms(*, tags: list[str] | None, scope_type: str, scope_key: str | None) -> list[str]:
    terms: list[str] = []
    terms.extend(tags or [])
    if scope_type != "tenant_shared" and scope_key:
        terms.append(scope_key)
    return terms


def _global_merge_rescued(
    *,
    room_results: list[SearchResult],
    global_results: list[SearchResult],
    merged_results: list[SearchResult],
) -> bool:
    if not global_results:
        return False
    room_ids = {result.item_id for result in room_results}
    if any(result.item_id not in room_ids for result in global_results):
        return True
    return bool(
        room_results
        and merged_results
        and merged_results[0].item_id != room_results[0].item_id
        and any(result.item_id == merged_results[0].item_id for result in global_results)
    )


def _should_merge_tenant_shared_results(
    *,
    scope_type: str,
    scope_key: str | None,
    results: list[SearchResult],
) -> bool:
    if scope_type == "tenant_shared":
        return False
    if not results:
        return True
    if scope_type in {"workspace", "session"} and scope_key:
        return False
    return all(r.source_type == "note" for r in results)


def _merge_search_results(
    primary: list[SearchResult],
    secondary: list[SearchResult],
    *,
    limit: int,
) -> list[SearchResult]:
    by_item_id: dict[uuid.UUID, SearchResult] = {}
    for result in [*primary, *secondary]:
        existing = by_item_id.get(result.item_id)
        if existing is None or result.score > existing.score:
            by_item_id[result.item_id] = result
    return sorted(by_item_id.values(), key=lambda result: result.score, reverse=True)[:limit]


def _merge_scoped_rescue_results(
    rescue_results: list[SearchResult],
    routed_results: list[SearchResult],
    *,
    limit: int,
) -> list[SearchResult]:
    rescued_item_ids = {result.item_id for result in rescue_results}
    by_item_id: dict[uuid.UUID, SearchResult] = {}
    for result in [*rescue_results, *routed_results]:
        existing = by_item_id.get(result.item_id)
        if existing is None or result.score > existing.score:
            by_item_id[result.item_id] = result
    return sorted(
        by_item_id.values(),
        key=lambda result: (
            result.item_id not in rescued_item_ids,
            -result.score,
        ),
    )[:limit]


def _append_rescue_results(
    results: list[SearchResult],
    rescue_results: list[SearchResult],
    *,
    limit: int,
) -> list[SearchResult]:
    if not rescue_results:
        return results
    by_item_id = {result.item_id for result in results}
    additions = [result for result in rescue_results if result.item_id not in by_item_id]
    if not additions:
        return results
    return [*results, *additions[: max(limit, 0)]]


def _looks_like_conversation_turn(result: SearchResult) -> bool:
    return result.source_type == "note" and (
        "# Conversation Turn" in result.chunk_text or result.title.startswith("default: [")
    )


def _looks_like_low_signal_conversation_note(result: SearchResult) -> bool:
    if not _looks_like_conversation_turn(result):
        return False
    haystack = " ".join(part for part in (result.title, result.summary, result.chunk_text) if part).lower()
    return any(pattern in haystack for pattern in _LOW_SIGNAL_CONVERSATION_PATTERNS)


def _suppress_low_signal_conversation_notes(
    results: list[SearchResult],
) -> tuple[list[SearchResult], int]:
    if not any(result.source_type != "note" for result in results):
        return results, 0
    filtered = [result for result in results if not _looks_like_low_signal_conversation_note(result)]
    if not filtered:
        return results, 0
    return filtered, len(results) - len(filtered)


def _coerce_trace_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    return None


def _coerce_trace_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _sanitize_retrieval_lens_profile(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        str(name): field_value
        for name, field_value in value.items()
        if name in RANKING_TRACE_LENS_PROFILE_KEYS
        and isinstance(field_value, (str, int, float, bool))
    }


def _sanitize_trace_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for name, raw_count in value.items():
        if isinstance(name, str) and isinstance(raw_count, int) and not isinstance(raw_count, bool):
            count = max(raw_count, 0)
            if count > 0:
                counts[name] = count
    return counts


def _sanitize_trace_reuse_metrics(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(name): field_value
        for name, field_value in value.items()
        if isinstance(name, str) and isinstance(field_value, (str, int, float, bool))
    }


def _append_search_ranking_trace(
    trace: PalaceRetrieveTrace,
    service: SearchService,
    *,
    route: str,
    limit: int,
    routing: dict[str, Any],
) -> None:
    raw_trace = getattr(service, "last_ranking_trace", None)
    if not isinstance(raw_trace, dict):
        return

    max_rows = min(max(limit, 1), 25)
    rows: list[PalaceRankingTraceResult] = []
    for rank, row in enumerate(raw_trace.get("results") or [], start=1):
        if rank > max_rows or not isinstance(row, dict):
            break
        raw_adjustments = row.get("adjustments")
        adjustments = {
            str(name): score
            for name, value in (raw_adjustments.items() if isinstance(raw_adjustments, dict) else [])
            if (score := _coerce_trace_float(value)) is not None
        }
        item_id = row.get("item_id")
        try:
            parsed_item_id = uuid.UUID(str(item_id)) if item_id else None
        except ValueError:
            parsed_item_id = None
        raw_derived_artifact_keys = row.get("derived_artifact_keys")
        rows.append(
            PalaceRankingTraceResult(
                rank=rank,
                item_id=parsed_item_id,
                source_type=str(row["source_type"]) if row.get("source_type") is not None else None,
                artifact_provenance_type=(
                    str(row["artifact_provenance_type"])
                    if row.get("artifact_provenance_type") is not None
                    else None
                ),
                artifact_provenance_label=(
                    str(row["artifact_provenance_label"])
                    if row.get("artifact_provenance_label") is not None
                    else None
                ),
                derived_artifact_keys=[
                    str(key)
                    for key in raw_derived_artifact_keys
                    if isinstance(key, str)
                ] if isinstance(raw_derived_artifact_keys, list) else [],
                retrieved_scope_type=(
                    str(row["retrieved_scope_type"])
                    if row.get("retrieved_scope_type") is not None
                    else None
                ),
                retrieved_scope_key=(
                    str(row["retrieved_scope_key"])
                    if row.get("retrieved_scope_key") is not None
                    else None
                ),
                retrieved_scope_label=(
                    str(row["retrieved_scope_label"])
                    if row.get("retrieved_scope_label") is not None
                    else None
                ),
                trust_class=str(row["trust_class"]) if row.get("trust_class") is not None else None,
                source_support_state=(
                    str(row["source_support_state"])
                    if row.get("source_support_state") is not None
                    else None
                ),
                freshness=str(row["freshness"]) if row.get("freshness") is not None else None,
                derived_raw_classification=(
                    str(row["derived_raw_classification"])
                    if row.get("derived_raw_classification") is not None
                    else None
                ),
                source_publication_id=(
                    str(row["source_publication_id"])
                    if row.get("source_publication_id") is not None
                    else None
                ),
                source_role=str(row["source_role"]) if row.get("source_role") is not None else None,
                query_source_role=(
                    str(row["query_source_role"]) if row.get("query_source_role") is not None else None
                ),
                reranker_score=_coerce_trace_float(row.get("reranker_score")),
                reranker_bonus=_coerce_trace_float(row.get("reranker_bonus")),
                reranker_provider=(
                    str(row["reranker_provider"]) if row.get("reranker_provider") is not None else None
                ),
                reranker_reason=(
                    str(row["reranker_reason"]) if row.get("reranker_reason") is not None else None
                ),
                retrieval_hint_score=_coerce_trace_float(row.get("retrieval_hint_score")),
                relationship_graph_score=_coerce_trace_float(row.get("relationship_graph_score")),
                base_score=_coerce_trace_float(row.get("base_score")),
                adjusted_score=_coerce_trace_float(row.get("adjusted_score")),
                adjustments=adjustments,
            )
        )

    trace.ranking_traces.append(
        PalaceRankingTrace(
            route=route,
            retrieval_lens=(
                str(raw_trace["retrieval_lens"])
                if isinstance(raw_trace.get("retrieval_lens"), str)
                else None
            ),
            retrieval_lens_profile=(
                _sanitize_retrieval_lens_profile(raw_trace.get("retrieval_lens_profile"))
            ),
            ranking_features_version=_coerce_trace_int(raw_trace.get("ranking_features_version")),
            query_intent=(
                str(raw_trace["query_intent"])
                if isinstance(raw_trace.get("query_intent"), str)
                else None
            ),
            source_ranking_enabled=(
                bool(raw_trace["source_ranking_enabled"])
                if isinstance(raw_trace.get("source_ranking_enabled"), bool)
                else None
            ),
            second_stage_reranker={
                str(name): value
                for name, value in (
                    raw_trace.get("second_stage_reranker", {}).items()
                    if isinstance(raw_trace.get("second_stage_reranker"), dict)
                    else []
                )
                if name in RANKING_TRACE_RERANKER_KEYS
                and (isinstance(value, (str, int, float, bool)) or value is None)
            },
            ranking_feature_flags={
                str(name): bool(value)
                for name, value in (
                    raw_trace.get("ranking_feature_flags", {}).items()
                    if isinstance(raw_trace.get("ranking_feature_flags"), dict)
                    else []
                )
                if isinstance(value, bool)
            },
            candidate_limit=_coerce_trace_int(raw_trace.get("candidate_limit")),
            display_limit=_coerce_trace_int(raw_trace.get("display_limit")),
            candidate_count=_coerce_trace_int(raw_trace.get("candidate_count")),
            trust_class_counts=_sanitize_trace_counts(raw_trace.get("trust_class_counts")),
            source_support_counts=_sanitize_trace_counts(raw_trace.get("source_support_counts")),
            freshness_counts=_sanitize_trace_counts(raw_trace.get("freshness_counts")),
            derived_raw_counts=_sanitize_trace_counts(raw_trace.get("derived_raw_counts")),
            reuse_metrics=_sanitize_trace_reuse_metrics(raw_trace.get("reuse_metrics")),
            result_count=len(rows),
            routing={
                key: value
                for key, value in routing.items()
                if key in RANKING_TRACE_ROUTING_KEYS
                and (isinstance(value, (str, int, float, bool)) or value is None)
            },
            results=rows,
        )
    )


def _activated_tunnel_stability(previous_stability: float | None) -> float:
    prior = previous_stability if previous_stability is not None else 1.0
    return round(min(1.0, max(0.0, prior + 0.02)), 6)


async def _activate_room_tunnels(
    db: AsyncSession,
    *,
    tenant_id: str,
    tunnel_ids: Iterable[uuid.UUID],
) -> list[PalaceTunnelActivationTrace]:
    unique_ids = tuple(dict.fromkeys(tunnel_ids))
    if not unique_ids:
        return []

    activated_at = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(RoomTunnel)
            .where(RoomTunnel.tenant_id == tenant_id)
            .where(RoomTunnel.id.in_(list(unique_ids)))
        )
    ).scalars().all()

    traces: list[PalaceTunnelActivationTrace] = []
    for tunnel in rows:
        tunnel.activation_count = int(tunnel.activation_count or 0) + 1
        tunnel.stability = _activated_tunnel_stability(tunnel.stability)
        tunnel.last_activated_at = activated_at
        tunnel.updated_at = activated_at
        traces.append(
            PalaceTunnelActivationTrace(
                source_room_id=tunnel.source_room_id,
                target_room_id=tunnel.target_room_id,
                tunnel_type=tunnel.tunnel_type,
                strength=float(tunnel.strength or 0.0),
                activation_count=tunnel.activation_count,
                stability=float(tunnel.stability or 0.0),
                last_activated_at=tunnel.last_activated_at,
            )
        )

    if not traces:
        return []

    try:
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        logger.warning("Failed to persist Palace tunnel activation trace", exc_info=True)
        return []
    return traces


async def _used_expanded_tunnel_ids(
    db: AsyncSession,
    *,
    tenant_id: str,
    expanded_tunnel_target_room_ids: dict[uuid.UUID, uuid.UUID],
    results: list[SearchResult],
) -> list[uuid.UUID]:
    if not expanded_tunnel_target_room_ids or not results:
        return []

    result_item_ids = [result.item_id for result in results]
    target_room_ids = list(set(expanded_tunnel_target_room_ids.values()))
    rows = (
        await db.execute(
            select(RoomMembership.room_id)
            .where(RoomMembership.tenant_id == tenant_id)
            .where(RoomMembership.room_id.in_(target_room_ids))
            .where(RoomMembership.item_id.in_(result_item_ids))
        )
    ).scalars().all()
    used_room_ids = set(rows)
    return [
        tunnel_id
        for tunnel_id, target_room_id in expanded_tunnel_target_room_ids.items()
        if target_room_id in used_room_ids
    ]


async def retrieve_palace(
    db: AsyncSession,
    *,
    tenant_id: str,
    embedder,
    body: PalaceRetrieveRequest,
    query_vector: list[float] | None = None,
) -> PalaceRetrieveResponse:
    # Canonical read pipeline:
    # query + explicit scope
    #   -> optional Palace room routing
    #   -> one scoped search attempt
    #   -> explicit fallback only if routing/search comes up empty
    state = await ensure_tenant_state(db, tenant_id)
    rooms = (
        await db.execute(
            select(Room, Wing.name, RoomSnapshot.summary)
            .join(Wing, Wing.id == Room.wing_id)
            .outerjoin(
                RoomSnapshot,
                (RoomSnapshot.room_id == Room.id)
                & (RoomSnapshot.generation == Room.snapshot_generation),
            )
            .where(Room.tenant_id == tenant_id)
            .where(Room.state == "active")
        )
    ).all()
    trace = PalaceRetrieveTrace(
        requested_scope_type=body.scope_type,
        requested_scope_key=body.scope_key,
    )
    routed_room_id = None
    redirected_from = None

    candidate_room_ids: list[uuid.UUID] = []
    selected_wing = None
    route_low_confidence = False
    route_score: float | None = None
    route_abstain_reason: str | None = None
    room_candidate_count: int | None = None
    global_candidate_count: int | None = None
    expanded_tunnel_target_room_ids: dict[uuid.UUID, uuid.UUID] = {}

    if body.room_id:
        resolved = await resolve_room(db, tenant_id, body.room_id)
        if resolved.room is not None:
            routed_room_id = resolved.room.id
            redirected_from = resolved.redirected_from_room_id
            candidate_room_ids = [resolved.room.id]
            trace.route_candidate_count = 1
            selected_wing = next((wing_name for room, wing_name, _ in rooms if room.id == resolved.room.id), None)
            if redirected_from:
                trace.status_banner = PalaceStateBanner(
                    kind="redirected",
                    message="Requested room redirected to the latest room lineage target.",
                )
    else:
        routing_terms = _routing_terms(tags=body.tags, scope_type=body.scope_type, scope_key=body.scope_key)
        scored_rooms = [
            _RoomRouteCandidate(room=room, wing_name=wing_name, summary=summary, score=score)
            for room, wing_name, summary in rooms
            if (
                score := _route_room_score(
                    body.query,
                    room.name,
                    wing_name,
                    summary,
                    routing_terms,
                    stable_key=getattr(room, "stable_key", None),
                )
            )
            > 0
        ]
        scored_rooms.sort(
            key=lambda entry: (
                entry.score,
                _room_scope_match_rank(
                    room=entry.room,
                    wing_name=entry.wing_name,
                    summary=entry.summary,
                    scope_type=body.scope_type,
                    scope_key=body.scope_key,
                    routing_terms=routing_terms,
                ),
            ),
            reverse=True,
        )
        trace.route_candidate_count = len(scored_rooms)
        if scored_rooms:
            route_score = round(scored_rooms[0].score, 6)
            if scored_rooms[0].score < ABSTAIN_ROUTE_SCORE:
                route_abstain_reason = "low_confidence"
            else:
                candidate_room_ids = [candidate.room.id for candidate in scored_rooms[:3]]
                trace.candidate_rooms = [candidate.room.name for candidate in scored_rooms[:3]]
                routed_room_id = scored_rooms[0].room.id
                selected_wing = scored_rooms[0].wing_name
            if candidate_room_ids and scored_rooms[0].score < LOW_CONFIDENCE_ROUTE_SCORE:
                route_low_confidence = True
                tunnel_rows = (
                    await db.execute(
                        select(RoomTunnel, Room)
                        .join(Room, Room.id == RoomTunnel.target_room_id)
                        .where(RoomTunnel.tenant_id == tenant_id)
                        .where(RoomTunnel.source_room_id.in_(candidate_room_ids))
                        .order_by(RoomTunnel.strength.desc())
                        .limit(4)
                    )
                ).all()
                expanded = []
                for tunnel, room in tunnel_rows:
                    if room.id not in candidate_room_ids:
                        candidate_room_ids.append(room.id)
                        expanded.append(room.name)
                        expanded_tunnel_target_room_ids[tunnel.id] = room.id
                trace.expanded_rooms = expanded
                trace.fallback_used = False
                if not body.tags:
                    trace.completeness_warning = "Expanded to neighboring rooms because route confidence was low."
                    trace.status_banner = PalaceStateBanner(
                        kind="fallback",
                        message="Expanded search to neighboring rooms before falling back globally.",
                    )
        else:
            route_abstain_reason = "no_matching_room"

    trace.selected_wing = selected_wing
    trace.route_score = route_score
    trace.route_confidence = _route_confidence(route_score)
    trace.route_abstain_reason = route_abstain_reason
    service = SearchService(db, embedder, tenant_id=tenant_id)
    query_vector = query_vector or await embedder.embed_single(body.query)
    results = []
    global_results_merged = False
    shared_results_merged = False
    suppressed_low_signal_notes = 0
    has_explicit_tag_filter = bool(body.tags)
    retrieve_candidate_limit = getattr(body, "candidate_limit", None)
    include_neighbor_chunks = getattr(body, "include_neighbor_chunks", False)
    neighbor_chunk_window = getattr(body, "neighbor_chunk_window", 1)
    context_budget_chars = getattr(body, "context_budget_chars", None)
    include_derived_artifacts = getattr(body, "include_derived_artifacts", False)
    retrieval_lens = getattr(body, "retrieval_lens", None)
    if candidate_room_ids:
        results = await service.vector_search(
            query=body.query,
            limit=body.limit,
            retrieval_lens=retrieval_lens,
            candidate_limit=retrieve_candidate_limit,
            include_neighbor_chunks=include_neighbor_chunks,
            neighbor_chunk_window=neighbor_chunk_window,
            context_budget_chars=context_budget_chars,
            include_derived_artifacts=include_derived_artifacts,
            room_ids=candidate_room_ids,
            scope_type=body.scope_type,
            scope_key=body.scope_key,
            tags=body.tags,
            tags_mode=body.tags_mode,
            date_from=body.date_from,
            date_to=body.date_to,
            min_score=body.min_score,
            query_vector=query_vector,
        )
        activated_tunnel_ids = await _used_expanded_tunnel_ids(
            db,
            tenant_id=tenant_id,
            expanded_tunnel_target_room_ids=expanded_tunnel_target_room_ids,
            results=results,
        )
        if activated_tunnel_ids:
            trace.activated_tunnels = await _activate_room_tunnels(
                db,
                tenant_id=tenant_id,
                tunnel_ids=activated_tunnel_ids,
            )
        room_candidate_count = len(results)
        _append_search_ranking_trace(
            trace,
            service,
            route="room_scoped",
            limit=body.limit,
            routing={
                "scope_type": body.scope_type,
                "scope_key": body.scope_key,
                "display_limit": body.limit,
                "candidate_limit": retrieve_candidate_limit,
                "room_count": len(candidate_room_ids),
                "route_score": route_score,
                "route_confidence": trace.route_confidence,
                "route_low_confidence": route_low_confidence,
                "explicit_tag_filter": has_explicit_tag_filter,
                "fallback_used": False,
                "room_candidate_count": room_candidate_count,
                "activated_tunnel_count": len(trace.activated_tunnels),
            },
        )

    should_merge_global_results = (
        results and not body.room_id and (route_low_confidence or has_explicit_tag_filter)
    )
    should_rescue_explicit_scope = (
        results
        and candidate_room_ids
        and not body.room_id
        and body.scope_type in {"agent", "workspace", "session"}
        and bool(body.scope_key)
        and not has_explicit_tag_filter
        and not route_low_confidence
    )
    if should_rescue_explicit_scope:
        scoped_rescue_results = await service.vector_search(
            query=body.query,
            limit=body.limit,
            retrieval_lens=retrieval_lens,
            candidate_limit=retrieve_candidate_limit,
            include_neighbor_chunks=include_neighbor_chunks,
            neighbor_chunk_window=neighbor_chunk_window,
            context_budget_chars=context_budget_chars,
            include_derived_artifacts=include_derived_artifacts,
            scope_type=body.scope_type,
            scope_key=body.scope_key,
            tags=body.tags,
            tags_mode=body.tags_mode,
            date_from=body.date_from,
            date_to=body.date_to,
            min_score=body.min_score,
            query_vector=query_vector,
        )
        global_candidate_count = len(scoped_rescue_results)
        _append_search_ranking_trace(
            trace,
            service,
            route="scoped_rescue",
            limit=body.limit,
            routing={
                "scope_type": body.scope_type,
                "scope_key": body.scope_key,
                "display_limit": body.limit,
                "candidate_limit": retrieve_candidate_limit,
                "route_score": route_score,
                "route_confidence": trace.route_confidence,
                "fallback_used": False,
                "room_candidate_count": room_candidate_count,
                "global_candidate_count": global_candidate_count,
            },
        )
        if scoped_rescue_results:
            results = _merge_scoped_rescue_results(
                scoped_rescue_results,
                results,
                limit=body.limit,
            )

    if should_merge_global_results:
        global_results = await service.vector_search(
            query=body.query,
            limit=body.limit,
            retrieval_lens=retrieval_lens,
            candidate_limit=retrieve_candidate_limit,
            include_neighbor_chunks=include_neighbor_chunks,
            neighbor_chunk_window=neighbor_chunk_window,
            context_budget_chars=context_budget_chars,
            include_derived_artifacts=include_derived_artifacts,
            scope_type=body.scope_type,
            scope_key=body.scope_key,
            tags=body.tags,
            tags_mode=body.tags_mode,
            date_from=body.date_from,
            date_to=body.date_to,
            min_score=body.min_score,
            query_vector=query_vector,
        )
        global_candidate_count = len(global_results)
        _append_search_ranking_trace(
            trace,
            service,
            route="global_merge",
            limit=body.limit,
            routing={
                "scope_type": body.scope_type,
                "scope_key": body.scope_key,
                "display_limit": body.limit,
                "candidate_limit": retrieve_candidate_limit,
                "route_score": route_score,
                "route_confidence": trace.route_confidence,
                "route_low_confidence": route_low_confidence,
                "explicit_tag_filter": has_explicit_tag_filter,
                "fallback_used": False,
                "room_candidate_count": room_candidate_count,
                "global_candidate_count": global_candidate_count,
            },
        )
        if global_results:
            room_results = results
            results = _merge_search_results(results, global_results, limit=body.limit)
            trace.global_merge_rescued_results = _global_merge_rescued(
                room_results=room_results,
                global_results=global_results,
                merged_results=results,
            )
            if trace.ranking_traces and trace.ranking_traces[-1].route == "global_merge":
                trace.ranking_traces[-1].routing["global_merge_rescued_results"] = (
                    trace.global_merge_rescued_results
                )
            global_results_merged = True
            if not has_explicit_tag_filter:
                trace.completeness_warning = "Room routing confidence was low, so results include global semantic matches."
                trace.status_banner = PalaceStateBanner(
                    kind="fallback",
                    message="Room routing confidence was low, so results include global semantic matches.",
                )

    if not results:
        if not has_explicit_tag_filter:
            trace.fallback_used = True
            trace.status_banner = PalaceStateBanner(
                kind="fallback",
                message="Room routing was too weak, so search fell back to the whole library.",
            )
            trace.completeness_warning = trace.completeness_warning or "Global fallback used because room-scoped retrieval had low confidence."
        results = await service.vector_search(
            query=body.query,
            limit=body.limit,
            retrieval_lens=retrieval_lens,
            candidate_limit=retrieve_candidate_limit,
            include_neighbor_chunks=include_neighbor_chunks,
            neighbor_chunk_window=neighbor_chunk_window,
            context_budget_chars=context_budget_chars,
            include_derived_artifacts=include_derived_artifacts,
            scope_type=body.scope_type,
            scope_key=body.scope_key,
            tags=body.tags,
            tags_mode=body.tags_mode,
            date_from=body.date_from,
            date_to=body.date_to,
            min_score=body.min_score,
            query_vector=query_vector,
        )
        global_candidate_count = len(results)
        _append_search_ranking_trace(
            trace,
            service,
            route="global_fallback",
            limit=body.limit,
            routing={
                "scope_type": body.scope_type,
                "scope_key": body.scope_key,
                "display_limit": body.limit,
                "candidate_limit": retrieve_candidate_limit,
                "room_count": len(candidate_room_ids),
                "route_score": route_score,
                "route_confidence": trace.route_confidence,
                "route_abstain_reason": route_abstain_reason,
                "route_low_confidence": route_low_confidence,
                "explicit_tag_filter": has_explicit_tag_filter,
                "fallback_used": trace.fallback_used,
                "global_candidate_count": global_candidate_count,
            },
        )

    if _should_merge_tenant_shared_results(
        scope_type=body.scope_type,
        scope_key=body.scope_key,
        results=results,
    ):
        shared_results = await service.vector_search(
            query=body.query,
            limit=body.limit,
            retrieval_lens=retrieval_lens,
            candidate_limit=retrieve_candidate_limit,
            include_neighbor_chunks=include_neighbor_chunks,
            neighbor_chunk_window=neighbor_chunk_window,
            context_budget_chars=context_budget_chars,
            include_derived_artifacts=include_derived_artifacts,
            scope_type="tenant_shared",
            scope_key=None,
            tags=body.tags,
            tags_mode=body.tags_mode,
            date_from=body.date_from,
            date_to=body.date_to,
            min_score=body.min_score,
            query_vector=query_vector,
        )
        _append_search_ranking_trace(
            trace,
            service,
            route="tenant_shared_merge",
            limit=body.limit,
            routing={
                "scope_type": "tenant_shared",
                "scope_key": None,
                "requested_scope_type": body.scope_type,
                "requested_scope_key": body.scope_key,
                "display_limit": body.limit,
                "candidate_limit": retrieve_candidate_limit,
                "fallback_used": trace.fallback_used,
            },
        )
        if shared_results:
            results = _merge_search_results(results, shared_results, limit=body.limit)
            shared_results_merged = True
            results, suppressed_low_signal_notes = _suppress_low_signal_conversation_notes(results)

    if settings.retrieval_hint_report_enabled:
        trace.hint_report = await report_retrieval_hint_candidates(
            db,
            tenant_id=tenant_id,
            query=body.query,
            current_results=results,
            room_ids=candidate_room_ids or None,
            limit=max(settings.retrieval_hint_report_limit, 1),
        )
    if settings.retrieval_hint_rescue_enabled:
        rescue_results = await retrieve_retrieval_hint_rescue_results(
            db,
            tenant_id=tenant_id,
            query=body.query,
            current_results=results,
            room_ids=candidate_room_ids or None,
            scope_type=body.scope_type,
            scope_key=body.scope_key,
            tags=body.tags,
            tags_mode=body.tags_mode,
            date_from=body.date_from,
            date_to=body.date_to,
            min_score=settings.retrieval_hint_rescue_min_score,
            limit=max(settings.retrieval_hint_rescue_limit, 1),
        )
        results = _append_rescue_results(results, rescue_results, limit=settings.retrieval_hint_rescue_limit)
        if trace.hint_report is None:
            trace.hint_report = {"report_enabled": False}
        trace.hint_report["applied"] = bool(rescue_results)
        trace.hint_report["applied_count"] = len(rescue_results)
        trace.hint_report["min_score"] = settings.retrieval_hint_rescue_min_score
        trace.hint_report["rescue_limit"] = settings.retrieval_hint_rescue_limit
    trace.search_ranking_trace = getattr(service, "last_ranking_trace", None)
    if isinstance(trace.search_ranking_trace, dict):
        raw_lens = trace.search_ranking_trace.get("retrieval_lens")
        raw_lens_profile = trace.search_ranking_trace.get("retrieval_lens_profile")
        trace.retrieval_lens = raw_lens if isinstance(raw_lens, str) else None
        trace.retrieval_lens_profile = _sanitize_retrieval_lens_profile(raw_lens_profile)
    if include_neighbor_chunks:
        trace.context_budget_chars = context_budget_chars
        trace.context_budget_truncated = bool(
            trace.search_ranking_trace
            and trace.search_ranking_trace.get("context_budget_truncated")
        )
    trace.route_room_candidate_count = room_candidate_count
    trace.route_global_candidate_count = global_candidate_count

    if routed_room_id:
        room = await db.get(Room, routed_room_id)
        if room:
            membership_generation = getattr(room, "membership_generation", room.snapshot_generation)
            if room.snapshot_generation < membership_generation:
                trace.completeness_warning = "Room content is still indexing. Results may be incomplete."
                trace.status_banner = PalaceStateBanner(
                    kind="indexing",
                    message="Showing results while this room is still indexing.",
                )

    steps = []
    if selected_wing:
        steps.append(
            PalaceTraceStep(
                title="Palace routing",
                detail=f"Query mapped into scope {body.scope_type} and wing {selected_wing}, with rooms {', '.join(trace.candidate_rooms) or 'none'}.",
            )
        )
    if trace.expanded_rooms:
        steps.append(
            PalaceTraceStep(
                title="Low-confidence expansion",
                detail=f"Expanded into neighboring rooms: {', '.join(trace.expanded_rooms)}.",
            )
        )
    if trace.activated_tunnels:
        steps.append(
            PalaceTraceStep(
                title="Tunnel activation",
                detail=f"Recorded usage for {len(trace.activated_tunnels)} retrieval tunnel(s).",
            )
        )
    if global_results_merged:
        if has_explicit_tag_filter and route_low_confidence:
            detail = "Merged explicitly tag-filtered semantic results because room routing confidence was low."
        elif has_explicit_tag_filter:
            detail = "Merged explicitly tag-filtered semantic results to keep routed corpus retrieval complete."
        else:
            detail = "Merged global semantic results because room routing confidence was low."
        steps.append(
            PalaceTraceStep(
                title="Tag-constrained global merge" if has_explicit_tag_filter else "Low-confidence global merge",
                detail=detail,
            )
        )
    if shared_results_merged:
        steps.append(
            PalaceTraceStep(
                title="Shared memory merge",
                detail="Merged tenant_shared results because scoped retrieval only produced note memories or came back empty.",
            )
        )
    if suppressed_low_signal_notes:
        steps.append(
            PalaceTraceStep(
                title="Conversation hygiene",
                detail=f"Suppressed {suppressed_low_signal_notes} low-signal conversation-turn notes because higher-value shared knowledge was available.",
            )
        )
    steps.append(
        PalaceTraceStep(
            title="Scoped retrieval" if not trace.fallback_used else "Global fallback",
            detail="Retrieved drawers from the selected scope and returned raw excerpts plus Palace trace context.",
        )
    )
    trace.steps = steps

    return PalaceRetrieveResponse(
        routed_room_id=routed_room_id,
        redirected_from_room_id=redirected_from,
        trace=trace,
        results=results,
        total=len(results),
    )


def _decode_sync_text(raw: bytes) -> str:
    if len(raw) > settings.palace_sync_max_file_bytes:
        raise ValueError("File exceeds Palace sync size limit")
    if b"\x00" in raw:
        raise ValueError("Binary file content is not supported for Palace sync")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="ignore")


def _iter_sync_files(root: Path, *, allowed_extensions: list[str] | None = None) -> list[SyncCandidate]:
    files: list[SyncCandidate] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _path_is_denied(rel) or _path_is_denied(path):
            continue
        if not _is_supported_sync_file(path):
            continue
        if allowed_extensions and path.suffix.lower() not in allowed_extensions:
            continue
        stat = path.stat()
        files.append(
            SyncCandidate(
                relative_path=str(rel),
                source_url=path.as_uri(),
                source_fingerprint=f"{stat.st_mtime_ns}:{stat.st_size}",
                file_size=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                load_text=lambda path=path: _read_sync_text(path),
            )
        )
    return files


def _read_sync_text(path: Path) -> str:
    return _decode_sync_text(path.read_bytes())


def _run_git_command(args: list[str], *, env: dict[str, str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValueError("git is not installed in the backend runtime") from exc

    if result.returncode == 0:
        return result.stdout.strip()

    stderr = (result.stderr or result.stdout or "").strip()
    detail = stderr.splitlines()[-1] if stderr else "git command failed"
    raise ValueError(f"Git sync failed: {detail}")


def _repo_checkout_dir(source: SyncSource) -> Path:
    tenant_segment = quote(source.tenant_id, safe="")
    return Path(settings.palace_repo_checkout_root).expanduser().resolve() / tenant_segment / str(source.id)


def _repo_credential_value(source: SyncSource) -> str:
    credential_type = source.credential_type or "none"
    if credential_type == "deployment_github_pat":
        if not settings.github_pat:
            raise ValueError("Repo sync requires GITHUB_PAT for deployment_github_pat sources")
        return settings.github_pat
    if not source.credential_ciphertext:
        raise ValueError("Repo sync source is missing its stored credential")
    return _decrypt_repo_credential(source.credential_ciphertext)


@contextmanager
def _repo_git_environment(source: SyncSource):
    repo_root = _repo_checkout_dir(source).parent
    repo_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    clone_url = _github_https_repo_url(source.root_path)

    with tempfile.TemporaryDirectory(dir=str(repo_root)) as temp_dir:
        temp_path = Path(temp_dir)
        credential_type = source.credential_type or "none"

        if credential_type in {"github_pat", "deployment_github_pat"}:
            askpass = temp_path / "git-askpass.sh"
            askpass.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
                "  *Password*) printf '%s\\n' \"$PALACEOFTRUTH_GITHUB_PAT\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            os.chmod(askpass, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            env["GIT_ASKPASS"] = str(askpass)
            env["PALACEOFTRUTH_GITHUB_PAT"] = _repo_credential_value(source)
        elif credential_type == "ssh_key":
            private_key = _repo_credential_value(source)
            key_path = temp_path / "id_ed25519"
            if not private_key.endswith("\n"):
                private_key = f"{private_key}\n"
            key_path.write_text(private_key, encoding="utf-8")
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
            known_hosts = temp_path / "known_hosts"
            known_hosts.write_text("", encoding="utf-8")
            os.chmod(known_hosts, stat.S_IRUSR | stat.S_IWUSR)
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key_path} -o IdentitiesOnly=yes "
                f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={known_hosts}"
            )
            clone_url = _github_ssh_repo_url(source.root_path)

        yield env, clone_url


def _prepare_repo_checkout(source: SyncSource) -> tuple[Path, str]:
    checkout_dir = _repo_checkout_dir(source)
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    if checkout_dir.exists() and not (checkout_dir / ".git").exists():
        shutil.rmtree(checkout_dir)

    with _repo_git_environment(source) as (env, clone_url):
        if not checkout_dir.exists():
            _run_git_command(["git", "clone", "--depth", "1", clone_url, str(checkout_dir)], env=env)
        else:
            _run_git_command(["git", "remote", "set-url", "origin", clone_url], env=env, cwd=checkout_dir)
            _run_git_command(["git", "fetch", "--depth", "1", "origin"], env=env, cwd=checkout_dir)

        branch = _run_git_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], env=env, cwd=checkout_dir)
        try:
            _run_git_command(["git", "remote", "set-head", "origin", "--auto"], env=env, cwd=checkout_dir)
            remote_head = _run_git_command(
                ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
                env=env,
                cwd=checkout_dir,
            )
            if remote_head.startswith("origin/"):
                branch = remote_head.removeprefix("origin/")
        except ValueError:
            pass

        _run_git_command(["git", "checkout", "-B", branch, f"origin/{branch}"], env=env, cwd=checkout_dir)
        _run_git_command(["git", "clean", "-fdx"], env=env, cwd=checkout_dir)
        return checkout_dir, branch


def _iter_repo_sync_files(
    root: Path,
    *,
    source: SyncSource,
    branch: str,
    allowed_extensions: list[str] | None = None,
) -> list[SyncCandidate]:
    files: list[SyncCandidate] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _path_is_denied(rel) or _path_is_denied(path):
            continue
        if not _is_supported_sync_file(path):
            continue
        if allowed_extensions and path.suffix.lower() not in allowed_extensions:
            continue
        stat_result = path.stat()
        relative_path = rel.as_posix()
        files.append(
            SyncCandidate(
                relative_path=relative_path,
                source_url=_github_blob_url(source.root_path, branch, relative_path),
                source_fingerprint=f"{stat_result.st_mtime_ns}:{stat_result.st_size}",
                file_size=stat_result.st_size,
                modified_ns=stat_result.st_mtime_ns,
                load_text=lambda path=path: _read_sync_text(path),
            )
        )
    return files


def _make_s3_client(source: SyncSource):
    client_kwargs: dict[str, object] = {}
    if source.endpoint_url:
        client_kwargs["endpoint_url"] = source.endpoint_url
    if source.region:
        client_kwargs["region_name"] = source.region

    client_kwargs["config"] = BotoConfig(
        signature_version="s3v4",
        s3={"addressing_style": "path" if source.force_path_style else "auto"},
    )
    return boto3.client("s3", **client_kwargs)


def _read_s3_text(client, *, bucket: str, key: str, expected_size: int) -> str:
    if expected_size > settings.palace_sync_max_file_bytes:
        raise ValueError("File exceeds Palace sync size limit")
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        raw = body.read()
    finally:
        body.close()
    return _decode_sync_text(raw)


def _iter_s3_sync_files(source: SyncSource) -> list[SyncCandidate]:
    if not source.bucket:
        raise ValueError("S3 sync source is missing bucket configuration")

    client = _make_s3_client(source)
    normalized_prefix = _normalize_sync_prefix(source.prefix)
    object_prefix = f"{normalized_prefix}/" if normalized_prefix else ""
    paginator = client.get_paginator("list_objects_v2")
    files: list[SyncCandidate] = []

    for page in paginator.paginate(Bucket=source.bucket, Prefix=object_prefix):
        for entry in page.get("Contents", []):
            key = entry.get("Key")
            if not key or key.endswith("/"):
                continue
            relative_path = key[len(object_prefix):] if object_prefix else key
            relative = Path(relative_path)
            if _path_is_denied(relative):
                continue
            if not _is_supported_sync_file(relative):
                continue
            if source.allowed_extensions and relative.suffix.lower() not in source.allowed_extensions:
                continue

            last_modified = entry.get("LastModified")
            modified_ns = int(last_modified.timestamp() * 1_000_000_000) if last_modified else None
            source_fingerprint = (entry.get("ETag") or "").strip("\"") or None
            file_size = int(entry.get("Size") or 0)
            quoted_key = quote(key, safe="/")
            files.append(
                SyncCandidate(
                    relative_path=relative_path,
                    source_url=f"s3://{source.bucket}/{quoted_key}",
                    source_fingerprint=source_fingerprint or f"{modified_ns}:{file_size}",
                    file_size=file_size,
                    modified_ns=modified_ns,
                    load_text=lambda client=client, bucket=source.bucket, key=key, file_size=file_size: _read_s3_text(
                        client,
                        bucket=bucket,
                        key=key,
                        expected_size=file_size,
                    ),
                )
            )
    return files


async def run_sync_run(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    embedder,
    llm,
) -> tuple[str, str | None]:
    run = await db.get(SyncRun, run_id)
    if run is None:
        raise ValueError(f"Sync run {run_id} not found")
    source = await db.get(SyncSource, run.sync_source_id)
    if source is None or source.tenant_id != run.tenant_id:
        raise ValueError(f"Sync source missing for run {run_id}")
    source_id = source.id
    if source.status != "active":
        error = f"Sync source {source.id} is disabled"
        run.status = "failed"
        run.error_message = error
        run.completed_at = datetime.now(timezone.utc)
        source.last_error = error
        await db.commit()
        return "failed", error

    run.status = "running"
    run.error_message = None
    source.last_error = None
    await db.commit()

    file_rows = (
        await db.execute(
            select(SyncSourceFile)
            .where(SyncSourceFile.tenant_id == run.tenant_id)
            .where(SyncSourceFile.sync_source_id == source.id)
        )
    ).scalars().all()
    existing_by_path = {row.relative_path: row for row in file_rows}
    seen_paths: set[str] = set()
    synced_item_ids: list[uuid.UUID] = []
    deleted_item_ids: list[uuid.UUID] = []

    repo_branch: str | None = None
    try:
        if source.source_kind == "s3":
            files = _iter_s3_sync_files(source)
        elif source.source_kind == "repo" and _is_remote_github_repo(source.root_path):
            root, repo_branch = _prepare_repo_checkout(source)
            files = _iter_repo_sync_files(
                root,
                source=source,
                branch=repo_branch,
                allowed_extensions=source.allowed_extensions,
            )
        else:
            root = validate_sync_root(source.root_path)
            files = _iter_sync_files(root, allowed_extensions=source.allowed_extensions)

        run.files_seen = len(files)
        for candidate in files:
            relative_path = candidate.relative_path
            seen_paths.add(relative_path)
            row = existing_by_path.get(relative_path)
            try:
                raw_text = candidate.load_text()
            except ValueError as exc:
                run.files_skipped += 1
                if row is None:
                    row = SyncSourceFile(
                        sync_source_id=source.id,
                        tenant_id=run.tenant_id,
                        relative_path=relative_path,
                        status="skipped",
                    )
                    db.add(row)
                row.status = "skipped"
                row.source_fingerprint = candidate.source_fingerprint
                row.file_size = candidate.file_size
                row.modified_ns = candidate.modified_ns
                row.last_error = str(exc)
                row.last_seen_run_id = run.id
                continue

            if _is_blank_sync_text(raw_text):
                run.files_skipped += 1
                if row is None:
                    row = SyncSourceFile(
                        sync_source_id=source.id,
                        tenant_id=run.tenant_id,
                        relative_path=relative_path,
                        status="skipped",
                    )
                    db.add(row)
                row.status = "skipped"
                row.source_fingerprint = candidate.source_fingerprint
                row.file_size = candidate.file_size
                row.modified_ns = candidate.modified_ns
                row.last_error = "Skipped blank file"
                row.last_seen_run_id = run.id
                continue

            content_hash = compute_content_hash(raw_text)
            unchanged = (
                row is not None
                and row.source_fingerprint == candidate.source_fingerprint
                and row.file_size == candidate.file_size
                and row.modified_ns == candidate.modified_ns
                and row.status == "active"
            )
            if unchanged:
                row.last_seen_run_id = run.id
                row.last_error = None
                continue

            if row is not None and row.content_hash == content_hash and row.status == "active":
                row.source_fingerprint = candidate.source_fingerprint
                row.file_size = candidate.file_size
                row.modified_ns = candidate.modified_ns
                row.last_seen_run_id = run.id
                row.last_error = None
                continue

            run.files_changed += 1
            item = await _load_sync_item(
                db,
                tenant_id=run.tenant_id,
                row=row,
                source_url=candidate.source_url,
            )
            is_new_item = item is None
            if item is None:
                item = Item(
                    source_type=_file_source_type(Path(relative_path)),
                    source_url=candidate.source_url,
                    title=_sync_file_title(Path(relative_path)),
                    raw_content=raw_text,
                    summary=None,
                    tags=[],
                    categories=[],
                    tenant_id=run.tenant_id,
                    status="processing",
                    metadata_={},
                )
                db.add(item)
                await db.flush()
            else:
                await db.execute(delete(Embedding).where(Embedding.item_id == item.id))
                item.source_type = _file_source_type(Path(relative_path))
                item.source_url = candidate.source_url
                item.title = _sync_file_title(Path(relative_path))
                item.raw_content = raw_text
                item.summary = None
                item.content_chunks = None
                item.status = "processing"

            metadata = dict(item.metadata_ or {})
            metadata.update(
                {
                    "sync_source_id": str(source.id),
                    "sync_relative_path": relative_path,
                    "sync_root_path": source.root_path,
                    "sync_active": True,
                    "sync_source_kind": source.source_kind,
                }
            )
            if source.bucket:
                metadata["sync_bucket"] = source.bucket
            if source.prefix:
                metadata["sync_prefix"] = source.prefix
            if source.endpoint_url:
                metadata["sync_endpoint_url"] = source.endpoint_url
            if repo_branch:
                metadata["sync_repo_branch"] = repo_branch
            item.metadata_ = metadata
            item.updated_at = datetime.now(timezone.utc)

            result = await process_prebuilt_item(
                db,
                item=item,
                embedder=embedder,
                llm=llm,
                tenant_id=run.tenant_id,
                enable_ai_enrichment=False,
            )
            if result.status in {"completed", "duplicate"}:
                synced_item_ids.append(item.id)

            if row is None:
                row = SyncSourceFile(
                    sync_source_id=source.id,
                    tenant_id=run.tenant_id,
                    relative_path=relative_path,
                )
                db.add(row)
            row.content_hash = content_hash
            row.source_fingerprint = candidate.source_fingerprint
            row.file_size = candidate.file_size
            row.modified_ns = candidate.modified_ns
            row.item_id = item.id
            row.status = "active"
            row.last_seen_run_id = run.id
            row.last_error = None

            if is_new_item:
                run.items_created += 1
            else:
                run.items_updated += 1

        for row in file_rows:
            if row.relative_path in seen_paths or row.status == "deleted":
                continue
            row.status = "deleted"
            row.last_seen_run_id = run.id
            if row.item_id:
                item = await db.get(Item, row.item_id)
                if item:
                    metadata = dict(item.metadata_ or {})
                    if str(metadata.get("sync_source_id", "")) == str(source.id):
                        item.status = "failed"
                        metadata["sync_active"] = False
                        metadata["sync_deleted"] = True
                        item.metadata_ = metadata
                        item.updated_at = datetime.now(timezone.utc)
                        deleted_item_ids.append(item.id)

        if synced_item_ids:
            await mark_items_dirty(
                db,
                tenant_id=run.tenant_id,
                item_ids=synced_item_ids,
                reason="sync",
                sync_source_id=source.id,
            )
        if deleted_item_ids:
            await mark_items_dirty(
                db,
                tenant_id=run.tenant_id,
                item_ids=deleted_item_ids,
                reason="sync-delete",
                sync_source_id=source.id,
            )
        state = await ensure_tenant_state(db, run.tenant_id)
        run.generation = state.dirty_generation
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        source.last_synced_at = run.completed_at
        source.last_error = None
        await db.commit()
        return "completed", None
    except Exception as exc:
        await db.rollback()
        logger.exception("sync run %s failed: %s", run_id, exc)
        run = await db.get(SyncRun, run_id)
        source = await db.get(SyncSource, source_id)
        if run:
            run.status = "failed"
            run.error_message = str(exc)[:500]
            run.completed_at = datetime.now(timezone.utc)
        if source:
            source.last_error = str(exc)[:500]
        await db.commit()
        return "failed", str(exc)


async def _ensure_room(db: AsyncSession, *, tenant_id: str, wing_name: str, room_name: str) -> Room:
    wing_slug = slugify(wing_name)
    wing = await db.scalar(
        select(Wing)
        .where(Wing.tenant_id == tenant_id)
        .where(Wing.slug == wing_slug)
        .limit(1)
    )
    if wing is None:
        wing = Wing(tenant_id=tenant_id, slug=wing_slug, name=wing_name, source="derived")
        db.add(wing)
        await db.flush()

    stable_key = f"{wing_slug}:{slugify(room_name)}"
    room = await db.scalar(
        select(Room)
        .where(Room.tenant_id == tenant_id)
        .where(Room.stable_key == stable_key)
        .limit(1)
    )
    if room is None:
        room = Room(
            tenant_id=tenant_id,
            wing_id=wing.id,
            slug=slugify(room_name),
            stable_key=stable_key,
            name=room_name,
        )
        db.add(room)
        await db.flush()
    return room


async def _apply_room_routing(
    db: AsyncSession,
    *,
    tenant_id: str,
    item: Item,
    generation: int,
) -> set[uuid.UUID]:
    affected_rooms: set[uuid.UUID] = set()
    existing_auto = (
        await db.execute(
            select(RoomMembership)
            .where(RoomMembership.tenant_id == tenant_id)
            .where(RoomMembership.item_id == item.id)
            .where(RoomMembership.source == "auto")
        )
    ).scalars().all()
    for membership in existing_auto:
        affected_rooms.add(membership.room_id)
        await db.delete(membership)
    if existing_auto:
        # Persist removals before adding the replacement row with the same unique key.
        await db.flush()

    pinned_primary = await db.scalar(
        select(RoomMembership)
        .where(RoomMembership.tenant_id == tenant_id)
        .where(RoomMembership.item_id == item.id)
        .where(RoomMembership.source == "pinned")
        .where(RoomMembership.membership_kind == "primary")
        .limit(1)
    )

    if item.status != "ready" or not item.metadata_.get("sync_active", True):
        pinned = (
            await db.execute(
                select(RoomMembership)
                .where(RoomMembership.tenant_id == tenant_id)
                .where(RoomMembership.item_id == item.id)
                .where(RoomMembership.source == "pinned")
            )
        ).scalars().all()
        for membership in pinned:
            affected_rooms.add(membership.room_id)
            await db.delete(membership)
        return affected_rooms

    wing_name = _infer_wing_name(item)
    room_name = _infer_room_name(item)
    room = await _ensure_room(db, tenant_id=tenant_id, wing_name=wing_name, room_name=room_name)
    affected_rooms.add(room.id)

    auto_kind = "secondary" if pinned_primary and pinned_primary.room_id != room.id else "primary"
    db.add(
        RoomMembership(
            tenant_id=tenant_id,
            room_id=room.id,
            item_id=item.id,
            source="auto",
            membership_kind=auto_kind,
            confidence=0.85,
        )
    )
    room.membership_generation = generation

    if pinned_primary and pinned_primary.room_id != room.id:
        db.add(
            PalaceRoomEvent(
                tenant_id=tenant_id,
                room_id=pinned_primary.room_id,
                event_type="pin-conflict",
                payload={"item_id": str(item.id), "auto_room_id": str(room.id)},
            )
        )
        affected_rooms.add(pinned_primary.room_id)

    return affected_rooms


async def _rebuild_room_snapshot(db: AsyncSession, *, tenant_id: str, room_id: uuid.UUID, generation: int) -> None:
    room = await db.get(Room, room_id)
    if room is None:
        return

    closet = await _ensure_room_closet_artifact(
        db,
        tenant_id=tenant_id,
        room_id=room_id,
        generation=generation,
        room=room,
    )
    drawer_refs = closet.drawer_refs or []
    titles = [
        str(ref.get("title", "")).strip()
        for ref in drawer_refs[:3]
        if str(ref.get("title", "")).strip()
    ]
    hot_tags = [
        tag
        for tag, _count in sorted(
            (closet.tag_profile or {}).items(),
            key=lambda entry: (-entry[1], entry[0]),
        )[:3]
    ]
    summary_parts = []
    if titles:
        summary_parts.append(f"{room.name} groups {'; '.join(titles[:3])}.")
    if hot_tags:
        summary_parts.append(f"Common threads: {', '.join(hot_tags)}.")
    if not summary_parts:
        summary_parts.append(f"{room.name} is waiting for its first active drawers.")

    await db.execute(
        delete(RoomSnapshot)
        .where(RoomSnapshot.tenant_id == tenant_id)
        .where(RoomSnapshot.room_id == room_id)
        .where(RoomSnapshot.generation == generation)
    )
    snapshot = RoomSnapshot(
        room_id=room_id,
        tenant_id=tenant_id,
        generation=generation,
        item_count=closet.item_count,
        summary=" ".join(summary_parts),
        representative_item_ids=[str(ref["item_id"]) for ref in drawer_refs[:6] if ref.get("item_id")],
    )
    db.add(snapshot)
    room.snapshot_generation = generation


async def _ensure_room_closet_artifact(
    db: AsyncSession,
    *,
    tenant_id: str,
    room_id: uuid.UUID,
    generation: int,
    room: Room | None = None,
) -> RoomClosetArtifact:
    existing = (
        await db.execute(
            select(RoomClosetArtifact)
            .where(RoomClosetArtifact.tenant_id == tenant_id)
            .where(RoomClosetArtifact.room_id == room_id)
            .where(RoomClosetArtifact.generation == generation)
        )
    ).scalars().first()
    if existing is not None:
        if room is not None:
            room.closet_generation = generation
        return existing

    return await _rebuild_room_closet_artifact(
        db,
        tenant_id=tenant_id,
        room_id=room_id,
        generation=generation,
        room=room,
    )


async def _rebuild_room_closet_artifact(
    db: AsyncSession,
    *,
    tenant_id: str,
    room_id: uuid.UUID,
    generation: int,
    room: Room | None = None,
) -> RoomClosetArtifact:
    room = room or await db.get(Room, room_id)
    if room is None:
        raise ValueError(f"Room {room_id} not found")

    rows = (
        await db.execute(
            select(RoomMembership, Item)
            .join(Item, Item.id == RoomMembership.item_id)
            .where(RoomMembership.tenant_id == tenant_id)
            .where(RoomMembership.room_id == room_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
            .order_by(RoomMembership.source.desc(), Item.created_at.desc())
        )
    ).all()
    items = [item for _, item in rows]
    tags = Counter(tag for item in items for tag in (item.tags or []))

    await db.execute(
        delete(RoomClosetArtifact)
        .where(RoomClosetArtifact.tenant_id == tenant_id)
        .where(RoomClosetArtifact.room_id == room_id)
        .where(RoomClosetArtifact.generation == generation)
    )
    closet = RoomClosetArtifact(
        room_id=room_id,
        tenant_id=tenant_id,
        generation=generation,
        item_count=len(items),
        drawer_refs=[
            {
                "item_id": str(item.id),
                "title": item.title,
                "source_type": item.source_type,
                "tags": item.tags or [],
            }
            for item in items[:50]
        ],
        tag_profile=dict(tags),
    )
    db.add(closet)
    room.closet_generation = generation
    return closet


async def _room_tag_profile(db: AsyncSession, tenant_id: str, room_id: uuid.UUID) -> set[str]:
    rows = (
        await db.execute(
            select(Item.tags)
            .join(RoomMembership, RoomMembership.item_id == Item.id)
            .where(RoomMembership.tenant_id == tenant_id)
            .where(RoomMembership.room_id == room_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
        )
    ).all()
    tags: set[str] = set()
    for (item_tags,) in rows:
        tags.update(tag.lower() for tag in (item_tags or []))
    return tags


async def _rebuild_tunnels(db: AsyncSession, *, tenant_id: str, room_ids: set[uuid.UUID], generation: int) -> None:
    if not room_ids:
        return
    existing_rows = (
        await db.execute(
            select(RoomTunnel)
            .where(RoomTunnel.tenant_id == tenant_id)
            .where((RoomTunnel.source_room_id.in_(list(room_ids))) | (RoomTunnel.target_room_id.in_(list(room_ids))))
        )
    ).scalars().all()
    existing_edges = {
        (row.source_room_id, row.target_room_id, row.tunnel_type): row
        for row in existing_rows
    }
    await db.execute(
        delete(RoomTunnel)
        .where(RoomTunnel.tenant_id == tenant_id)
        .where((RoomTunnel.source_room_id.in_(list(room_ids))) | (RoomTunnel.target_room_id.in_(list(room_ids))))
    )

    all_rooms = (
        await db.execute(
            select(Room)
            .where(Room.tenant_id == tenant_id)
            .where(Room.state == "active")
        )
    ).scalars().all()
    tag_profiles = {room.id: await _room_tag_profile(db, tenant_id, room.id) for room in all_rooms}

    for room in all_rooms:
        if room.id not in room_ids:
            continue
        source_tags = tag_profiles.get(room.id, set())
        best_edges: list[tuple[uuid.UUID, float]] = []
        for other in all_rooms:
            if other.id == room.id:
                continue
            shared = source_tags & tag_profiles.get(other.id, set())
            if not shared:
                continue
            strength = len(shared) / max(len(source_tags | tag_profiles.get(other.id, set())), 1)
            best_edges.append((other.id, strength))

        best_edges.sort(key=lambda entry: entry[1], reverse=True)
        for target_room_id, strength in best_edges[:4]:
            previous = existing_edges.get((room.id, target_room_id, "shared-tag"))
            tunnel_kwargs = {
                "tenant_id": tenant_id,
                "source_room_id": room.id,
                "target_room_id": target_room_id,
                "tunnel_type": "shared-tag",
                "strength": strength,
                "activation_count": previous.activation_count if previous else 0,
                "stability": _updated_tunnel_stability(
                    previous_strength=previous.strength if previous else None,
                    new_strength=strength,
                    previous_stability=previous.stability if previous else None,
                ),
                "last_activated_at": previous.last_activated_at if previous else None,
            }
            if previous is not None:
                tunnel_kwargs["created_at"] = previous.created_at
            db.add(
                RoomTunnel(
                    **tunnel_kwargs,
                )
            )
        room.tunnel_generation = generation


def _updated_tunnel_stability(
    *,
    previous_strength: float | None,
    new_strength: float,
    previous_stability: float | None,
) -> float:
    if previous_strength is None:
        return 1.0
    prior = previous_stability if previous_stability is not None else 1.0
    drift = min(abs(float(previous_strength) - float(new_strength)), 1.0)
    reinforcement = 0.04 if drift < 0.05 else 0.0
    return round(min(1.0, max(0.0, prior - (drift * 0.5) + reinforcement)), 6)


async def _stale_tunnel_room_ids(
    db: AsyncSession,
    *,
    tenant_id: str,
    limit: int,
) -> tuple[uuid.UUID, ...]:
    rows = (
        await db.execute(
            select(Room.id)
            .where(Room.tenant_id == tenant_id)
            .where(Room.state == "active")
            .where(Room.membership_generation > Room.tunnel_generation)
            .order_by(Room.updated_at.asc(), Room.id.asc())
            .limit(limit)
        )
    ).scalars().all()
    return tuple(rows)


async def recompute_stale_room_tunnels(
    db: AsyncSession,
    *,
    tenant_id: str,
    target_generation: int | None = None,
    limit: int = 50,
) -> PalaceTunnelRecomputeResult:
    state = await ensure_tenant_state(db, tenant_id)
    generation = target_generation if target_generation is not None else state.indexed_generation
    if generation <= 0 or limit < 1:
        return PalaceTunnelRecomputeResult(room_ids=(), target_generation=generation)

    room_ids = await _stale_tunnel_room_ids(db, tenant_id=tenant_id, limit=limit)
    if not room_ids:
        return PalaceTunnelRecomputeResult(room_ids=(), target_generation=generation)

    await _rebuild_tunnels(
        db,
        tenant_id=tenant_id,
        room_ids=set(room_ids),
        generation=generation,
    )
    await db.commit()
    return PalaceTunnelRecomputeResult(room_ids=room_ids, target_generation=generation)


async def run_palace_run(db: AsyncSession, *, run_id: uuid.UUID) -> tuple[str, str | None]:
    run = await db.get(PalaceRun, run_id)
    if run is None:
        raise ValueError(f"Palace run {run_id} not found")
    state = await ensure_tenant_state(db, run.tenant_id)
    logger.info(
        "palace run build started tenant=%s run_id=%s trigger=%s requested_generation=%s indexed_generation=%s dirty_generation=%s",
        run.tenant_id,
        run.id,
        run.triggered_by,
        run.requested_generation,
        state.indexed_generation,
        state.dirty_generation,
    )
    run.status = "routing"
    state.active_palace_run_id = run.id
    state.active_generation = run.requested_generation
    await db.commit()

    try:
        dirty_items = (
            await db.execute(
                select(PalaceDirtyItem, Item)
                .join(Item, Item.id == PalaceDirtyItem.item_id)
                .where(PalaceDirtyItem.tenant_id == run.tenant_id)
                .where(PalaceDirtyItem.generation <= run.requested_generation)
            )
        ).all()
        if not dirty_items and state.indexed_generation == 0:
            all_items = (
                await db.execute(
                    select(Item)
                    .where(Item.tenant_id == run.tenant_id)
                    .where(Item.status == "ready")
                    .where(Item.deleted_at.is_(None))
                )
            ).scalars().all()
            dirty_items = [(None, item) for item in all_items]

        affected_rooms: set[uuid.UUID] = set()
        processed_dirty_ids: list[uuid.UUID] = []
        routed_item_ids: set[uuid.UUID] = set()
        for dirty, item in dirty_items:
            if dirty is not None:
                processed_dirty_ids.append(dirty.id)
            if item.id in routed_item_ids:
                continue
            routed_item_ids.add(item.id)
            affected_rooms |= await _apply_room_routing(
                db,
                tenant_id=run.tenant_id,
                item=item,
                generation=run.requested_generation,
            )

        run.status = "snapshotting"
        await db.commit()
        for room_id in affected_rooms:
            await _rebuild_room_snapshot(
                db,
                tenant_id=run.tenant_id,
                room_id=room_id,
                generation=run.requested_generation,
            )
            await rebuild_room_retrieval_hints(
                db,
                tenant_id=run.tenant_id,
                room_id=room_id,
                generation=run.requested_generation,
            )

        run.status = "tunneling"
        await db.commit()
        await _rebuild_tunnels(
            db,
            tenant_id=run.tenant_id,
            room_ids=affected_rooms,
            generation=run.requested_generation,
        )

        if processed_dirty_ids:
            await db.execute(
                delete(PalaceDirtyItem).where(PalaceDirtyItem.id.in_(processed_dirty_ids))
            )
        state.indexed_generation = run.requested_generation
        state.active_generation = None
        state.active_palace_run_id = None
        run.status = "completed"
        run.applied_generation = run.requested_generation
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info(
            "palace run build completed tenant=%s run_id=%s applied_generation=%s routed_items=%d affected_rooms=%d",
            run.tenant_id,
            run.id,
            run.applied_generation,
            len(routed_item_ids),
            len(affected_rooms),
        )
        return "completed", None
    except Exception as exc:
        await db.rollback()
        logger.exception("palace run %s failed: %s", run_id, exc)
        run = await db.get(PalaceRun, run_id)
        state = await ensure_tenant_state(db, run.tenant_id if run else "default")
        if run:
            run.status = "failed"
            run.error_message = str(exc)[:500]
            run.completed_at = datetime.now(timezone.utc)
        state.active_generation = None
        state.active_palace_run_id = None
        await db.commit()
        logger.info(
            "palace run build marked failed tenant=%s run_id=%s error=%s",
            run.tenant_id if run else "default",
            run_id,
            str(exc)[:500],
        )
        return "failed", str(exc)
