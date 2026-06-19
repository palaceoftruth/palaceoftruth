from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import yt_dlp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.item import Item
from app.models.job import Job
from app.models.source_subscription import SourceSubscription, SourceSubscriptionEntry
from app.services.item_dates import apply_effective_date
from app.utils.job_payloads import build_retry_payload
from app.workers.queues import enqueue_worker_job


SUBSCRIPTION_STATUSES = frozenset({"active", "paused", "deleted"})
ENTRY_STATUSES = frozenset({"discovered", "queued", "captured", "skipped", "failed"})
YOUTUBE_CHANNEL_PROVIDER_TYPE = "youtube_channel"
YOUTUBE_DISCOVERY_BACKEND_YTDLP = "yt-dlp"
SOURCE_SUBSCRIPTION_MANUAL_SYNC_CURSOR_KEY = "last_manual_sync_at"
SOURCE_SUBSCRIPTION_BACKFILL_CURSOR_KEY = "backfill"
YOUTUBE_WATCH_DISCOVERY_WINDOW = 50

logger = logging.getLogger(__name__)


class SourceSubscriptionProviderError(RuntimeError):
    """Raised when a source subscription provider cannot complete its work."""


@dataclass(frozen=True)
class ResolvedSource:
    provider_type: str
    external_id: str
    source_url: str
    external_url: str | None = None
    display_name: str | None = None
    cursor: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveredSourceEntry:
    provider_entry_id: str | None
    source_url: str
    title: str | None = None
    published_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    skip_reason: str | None = None


@dataclass(frozen=True)
class SourceSubscriptionDiscoveryResult:
    entries: list[DiscoveredSourceEntry]
    provider_exhausted: bool = False


@dataclass(frozen=True)
class SourceSubscriptionBackfillPolicy:
    enabled: bool = False
    limit: int | None = None
    published_after: datetime | None = None


@dataclass(frozen=True)
class SourceIngestJobSpec:
    task_name: str
    item_source_type: str
    source_url: str
    title: str
    metadata: dict[str, Any] = field(default_factory=dict)
    task_kwargs: dict[str, Any] = field(default_factory=dict)


class SourceSubscriptionProvider(Protocol):
    provider_type: str

    async def resolve_source(
        self,
        source_url: str,
        *,
        tenant_id: str,
        backfill_policy: SourceSubscriptionBackfillPolicy | None = None,
    ) -> ResolvedSource:
        """Resolve an operator-provided source URL into a stable provider identity."""

    async def discover_entries(
        self, subscription: SourceSubscription
    ) -> list[DiscoveredSourceEntry] | SourceSubscriptionDiscoveryResult:
        """Discover new candidate entries without enqueueing capture work inline."""

    async def build_ingest_job(
        self,
        subscription: SourceSubscription,
        entry: SourceSubscriptionEntry,
    ) -> SourceIngestJobSpec:
        """Map a discovered entry to an ingest job spec for the worker queue."""


ProviderFactory = type[SourceSubscriptionProvider] | SourceSubscriptionProvider


class YoutubeChannelDiscoveryBackend(Protocol):
    name: str

    def extract_channel_info(self, url: str, *, playlistend: int | None) -> dict[str, Any]:
        """Return flat YouTube channel upload metadata for a channel URL."""


class SourceSubscriptionProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderFactory] = {}

    def register(self, provider: ProviderFactory) -> None:
        provider_type = getattr(provider, "provider_type", None)
        if not isinstance(provider_type, str) or not provider_type.strip():
            raise ValueError("source subscription providers must define a non-empty provider_type")
        self._providers[provider_type] = provider

    def create(self, provider_type: str) -> SourceSubscriptionProvider:
        provider = self._providers.get(provider_type)
        if provider is None:
            raise KeyError(f"unknown source subscription provider: {provider_type}")
        if isinstance(provider, type):
            return provider()
        return provider

    def available_provider_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


class YtDlpYoutubeChannelDiscoveryBackend:
    name = YOUTUBE_DISCOVERY_BACKEND_YTDLP

    def __init__(self, *, youtube_dl_factory: Any = yt_dlp.YoutubeDL) -> None:
        self._youtube_dl_factory = youtube_dl_factory

    def extract_channel_info(self, url: str, *, playlistend: int | None) -> dict[str, Any]:
        ydl_opts: dict[str, Any] = {
            "extract_flat": True,
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": settings.media_download_timeout_seconds,
            "retries": 3,
            "extractor_retries": 3,
        }
        if playlistend is not None:
            ydl_opts["playlistend"] = playlistend

        try:
            with self._youtube_dl_factory(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # yt-dlp exposes many extractor/network exception classes.
            raise SourceSubscriptionProviderError("YouTube channel discovery failed") from exc

        if not isinstance(info, dict):
            raise SourceSubscriptionProviderError("YouTube channel discovery returned no metadata")
        return info


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_youtube_channel_url(source_url: str) -> str:
    value = source_url.strip()
    if not value:
        raise SourceSubscriptionProviderError("YouTube channel URL is required")
    if value.startswith("@"):
        return f"https://www.youtube.com/{value}"
    if value.startswith("youtube.com/") or value.startswith("www.youtube.com/"):
        return f"https://{value}"
    return value


def _canonical_youtube_video_url(video_id: str, fallback_url: str | None = None) -> str:
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    if fallback_url:
        return fallback_url
    raise SourceSubscriptionProviderError("discovered YouTube entry did not include a usable URL")


def _youtube_video_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/") or None
    if parsed.netloc.endswith("youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        if parsed.path.startswith("/shorts/"):
            return parsed.path.split("/", 2)[2].split("/", 1)[0] or None
        if parsed.path.startswith("/live/"):
            return parsed.path.split("/", 2)[2].split("/", 1)[0] or None
    return None


def _youtube_entry_provider_id(info: dict[str, Any]) -> str | None:
    fallback_url = str(info.get("webpage_url") or info.get("url") or "")
    video_id = str(info.get("id") or _youtube_video_id_from_url(fallback_url) or "").strip()
    return video_id or None


def _parse_youtube_datetime(info: dict[str, Any]) -> datetime | None:
    timestamp = info.get("timestamp") or info.get("release_timestamp")
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(timestamp, timezone.utc)

    upload_date = info.get("upload_date") or info.get("release_date")
    if isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit():
        try:
            return datetime(
                int(upload_date[0:4]),
                int(upload_date[4:6]),
                int(upload_date[6:8]),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
    return None


def _isoformat_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_cursor_datetime(cursor: dict[str, Any], key: str) -> datetime | None:
    value = cursor.get(key)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sanitize_source_subscription_error(exc: BaseException | str) -> str:
    message = str(exc).strip()
    if not message:
        return "source subscription operation failed"
    if len(message) > 300:
        message = message[:297] + "..."
    return message.replace(settings.openai_api_key, "[redacted]") if settings.openai_api_key else message


def _youtube_entry_skip_reason(info: dict[str, Any], source_url: str | None) -> str | None:
    live_status = str(info.get("live_status") or "").lower()
    if live_status in {"is_live", "is_upcoming", "was_live", "post_live"}:
        return "youtube_live_unsupported"

    url_values = [
        str(info.get("url") or ""),
        str(info.get("webpage_url") or ""),
        source_url or "",
    ]
    if any("/shorts/" in value for value in url_values):
        return "youtube_shorts_unsupported"

    ie_key = str(info.get("ie_key") or "").lower()
    if "shorts" in ie_key:
        return "youtube_shorts_unsupported"

    return None


class YoutubeChannelSourceSubscriptionProvider:
    provider_type = YOUTUBE_CHANNEL_PROVIDER_TYPE

    def __init__(
        self,
        *,
        discovery_backend: YoutubeChannelDiscoveryBackend | None = None,
        youtube_dl_factory: Any = yt_dlp.YoutubeDL,
        now: Any = _utc_now,
    ) -> None:
        self._discovery_backend = discovery_backend or YtDlpYoutubeChannelDiscoveryBackend(
            youtube_dl_factory=youtube_dl_factory
        )
        self._now = now

    async def resolve_source(
        self,
        source_url: str,
        *,
        tenant_id: str,
        backfill_policy: SourceSubscriptionBackfillPolicy | None = None,
    ) -> ResolvedSource:
        normalized_url = _normalize_youtube_channel_url(source_url)
        backfill_policy = backfill_policy or SourceSubscriptionBackfillPolicy()
        info = self._extract_channel_info(normalized_url, playlistend=YOUTUBE_WATCH_DISCOVERY_WINDOW)
        channel_id = self._channel_id(info)
        if not channel_id:
            raise SourceSubscriptionProviderError("could not resolve a stable YouTube channel id")

        created_at = self._now()
        initial_entry_ids = [] if backfill_policy.enabled else self._entry_ids(info.get("entries") or [])
        channel_url = self._channel_url(info, channel_id)
        display_name = self._display_name(info)
        backfill_cursor = _source_subscription_backfill_cursor(backfill_policy)

        return ResolvedSource(
            provider_type=self.provider_type,
            external_id=channel_id,
            source_url=normalized_url,
            external_url=channel_url,
            display_name=display_name,
            cursor={
                "created_at": _isoformat_datetime(created_at),
                "no_backfill": not backfill_policy.enabled,
                "seen_provider_entry_ids": initial_entry_ids,
                SOURCE_SUBSCRIPTION_BACKFILL_CURSOR_KEY: backfill_cursor,
            },
            metadata={
                "discovery_backend": self._discovery_backend.name,
                "youtube_channel_id": channel_id,
                "youtube_channel_name": display_name,
                "youtube_channel_handle": info.get("channel_handle") or info.get("uploader_id"),
                "resolved_tenant_id": tenant_id,
                "backfill_enabled": backfill_policy.enabled,
                "backfill_limit": backfill_policy.limit,
                "backfill_published_after": _isoformat_datetime(backfill_policy.published_after),
            },
        )

    async def discover_entries(self, subscription: SourceSubscription) -> SourceSubscriptionDiscoveryResult:
        channel_url = subscription.external_url or subscription.source_url
        cursor = dict(subscription.cursor or {})
        backfill_cursor = _cursor_backfill(cursor)
        playlistend = _youtube_discovery_playlistend(backfill_cursor)
        info = self._extract_channel_info(channel_url, playlistend=playlistend)
        raw_entries = info.get("entries") or []
        if not isinstance(raw_entries, list):
            raw_entries = []
        seen_ids = {str(value) for value in cursor.get("seen_provider_entry_ids", []) if value}
        created_at = _parse_cursor_datetime(cursor, "created_at")
        backfill_published_after = _parse_cursor_datetime(backfill_cursor, "published_after")
        backfill_remaining = _cursor_backfill_remaining(backfill_cursor)
        backfill_boundary_provider_entry_id = str(backfill_cursor.get("boundary_provider_entry_id") or "").strip()
        provider_exhausted = (
            bool(backfill_cursor.get("enabled"))
            and backfill_remaining is not None
            and playlistend is not None
            and len(raw_entries) < playlistend
        )

        discovered: list[DiscoveredSourceEntry] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            if (
                backfill_boundary_provider_entry_id
                and _youtube_entry_provider_id(raw_entry) == backfill_boundary_provider_entry_id
            ):
                break
            if backfill_remaining is not None and backfill_remaining <= 0:
                break
            entry = self._build_entry(
                raw_entry,
                subscription=subscription,
                created_at=created_at,
                seen_ids=seen_ids,
                backfill_enabled=bool(backfill_cursor.get("enabled")),
                backfill_published_after=backfill_published_after,
            )
            if entry is not None:
                discovered.append(entry)
                if entry.skip_reason is None and backfill_remaining is not None:
                    backfill_remaining -= 1
        return SourceSubscriptionDiscoveryResult(entries=discovered, provider_exhausted=provider_exhausted)

    async def build_ingest_job(
        self,
        subscription: SourceSubscription,
        entry: SourceSubscriptionEntry,
    ) -> SourceIngestJobSpec:
        if not entry.source_url:
            raise SourceSubscriptionProviderError("source subscription entry is missing source_url")
        title = entry.title or "YouTube upload"
        return SourceIngestJobSpec(
            task_name="process_media",
            item_source_type="media",
            source_url=entry.source_url,
            title=title,
            metadata=build_source_entry_metadata(
                subscription=subscription,
                entry=entry,
                provider_metadata=entry.metadata_,
            ),
            task_kwargs={"url": entry.source_url},
        )

    def _extract_channel_info(self, url: str, *, playlistend: int | None) -> dict[str, Any]:
        return self._discovery_backend.extract_channel_info(self._uploads_url(url), playlistend=playlistend)

    def _build_entry(
        self,
        info: dict[str, Any],
        *,
        subscription: SourceSubscription,
        created_at: datetime | None,
        seen_ids: set[str],
        backfill_enabled: bool,
        backfill_published_after: datetime | None,
    ) -> DiscoveredSourceEntry | None:
        fallback_url = str(info.get("webpage_url") or info.get("url") or "")
        video_id = _youtube_entry_provider_id(info) or ""
        if not video_id and not fallback_url:
            return None
        source_url = _canonical_youtube_video_url(video_id, fallback_url)
        if not video_id:
            video_id = _youtube_video_id_from_url(source_url) or ""

        if video_id and video_id in seen_ids:
            return None

        published_at = _parse_youtube_datetime(info)
        if backfill_enabled:
            if (
                backfill_published_after is not None
                and published_at is not None
                and published_at < backfill_published_after
            ):
                return None
        elif published_at is not None and created_at is not None and published_at <= created_at:
            return None

        skip_reason = _youtube_entry_skip_reason(info, source_url)
        metadata = {
            "discovery_backend": self._discovery_backend.name,
            "youtube_channel_id": subscription.external_id,
            "youtube_channel_name": subscription.display_name,
            "youtube_video_id": video_id or None,
            "youtube_entry_kind": info.get("_type") or info.get("ie_key"),
        }
        return DiscoveredSourceEntry(
            provider_entry_id=video_id or None,
            source_url=source_url,
            title=info.get("title"),
            published_at=published_at,
            metadata={key: value for key, value in metadata.items() if value is not None},
            skip_reason=skip_reason,
        )

    @staticmethod
    def _entry_ids(entries: list[Any]) -> list[str]:
        ids: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id = str(entry.get("id") or _youtube_video_id_from_url(entry.get("url")) or "").strip()
            if video_id:
                ids.append(video_id)
        return list(dict.fromkeys(ids))

    @staticmethod
    def _channel_id(info: dict[str, Any]) -> str | None:
        for key in ("channel_id", "uploader_id", "id"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _channel_url(info: dict[str, Any], channel_id: str) -> str:
        for key in ("channel_url", "uploader_url", "webpage_url"):
            value = info.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        return f"https://www.youtube.com/channel/{channel_id}"

    @staticmethod
    def _display_name(info: dict[str, Any]) -> str | None:
        for key in ("channel", "uploader", "title"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _uploads_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.netloc.endswith("youtube.com") and parsed.path.rstrip("/").endswith("/videos"):
            return url
        return url.rstrip("/") + "/videos"


def _source_subscription_backfill_cursor(policy: SourceSubscriptionBackfillPolicy) -> dict[str, Any]:
    return {
        "enabled": policy.enabled,
        "limit": policy.limit,
        "remaining": policy.limit,
        "published_after": _isoformat_datetime(policy.published_after),
        "completed": not policy.enabled,
    }


def _cursor_backfill(cursor: dict[str, Any] | None) -> dict[str, Any]:
    value = (cursor or {}).get(SOURCE_SUBSCRIPTION_BACKFILL_CURSOR_KEY)
    if isinstance(value, dict):
        return dict(value)
    return {"enabled": False, "limit": None, "remaining": None, "published_after": None, "completed": True}


def _cursor_backfill_remaining(backfill_cursor: dict[str, Any]) -> int | None:
    if not backfill_cursor.get("enabled") or backfill_cursor.get("completed"):
        return None
    remaining = backfill_cursor.get("remaining")
    if isinstance(remaining, int):
        return max(remaining, 0)
    return None


def _youtube_discovery_playlistend(backfill_cursor: dict[str, Any]) -> int | None:
    if not backfill_cursor.get("enabled") or backfill_cursor.get("completed"):
        return YOUTUBE_WATCH_DISCOVERY_WINDOW
    limit = backfill_cursor.get("remaining") or backfill_cursor.get("limit")
    if isinstance(limit, int) and limit > 0:
        return max(limit, YOUTUBE_WATCH_DISCOVERY_WINDOW)
    return None


def build_source_entry_metadata(
    *,
    subscription: SourceSubscription,
    entry: SourceSubscriptionEntry,
    provider_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        "capture_origin": "source_subscription",
        "subscription_id": str(subscription.id),
        "subscription_entry_id": str(entry.id),
        "source_provider_type": subscription.provider_type,
    }
    if subscription.external_id:
        metadata["source_external_id"] = subscription.external_id
    if entry.provider_entry_id:
        metadata["source_provider_entry_id"] = entry.provider_entry_id
    if provider_metadata:
        metadata.update(provider_metadata)
    return metadata


async def queue_source_subscription_entry(
    db: AsyncSession,
    arq_pool: Any,
    subscription: SourceSubscription,
    entry: SourceSubscriptionEntry,
    *,
    registry: SourceSubscriptionProviderRegistry | None = None,
    queued_at: datetime | None = None,
) -> bool:
    registry = registry or DEFAULT_SOURCE_SUBSCRIPTION_PROVIDER_REGISTRY
    validate_source_subscription_tenant(subscription, entry)
    if entry.status in {"queued", "captured", "skipped"}:
        return False
    if not entry.source_url:
        _mark_entry_failed(entry, "Source subscription entry is missing source_url", failed_at=queued_at)
        await db.flush()
        return False

    provider = registry.create(subscription.provider_type)
    spec = await provider.build_ingest_job(subscription, entry)
    existing_item = await _find_existing_active_item(db, tenant_id=subscription.tenant_id, source_url=spec.source_url)
    if existing_item is not None:
        _mark_entry_skipped(
            entry,
            item_id=existing_item.id,
            skip_reason="duplicate_source_url",
            skipped_at=queued_at,
        )
        await db.flush()
        return False

    item = Item(
        source_type=spec.item_source_type,
        source_url=spec.source_url,
        title=spec.title,
        status="processing",
        metadata_=spec.metadata,
        tags=list(subscription.auto_tags or []),
        tenant_id=subscription.tenant_id,
    )
    apply_effective_date(item, metadata=spec.metadata)
    db.add(item)
    await db.flush()

    job = Job(
        item_id=item.id,
        job_type=spec.item_source_type,
        status="queued",
        progress=0,
        tenant_id=subscription.tenant_id,
        payload=build_retry_payload(task_name=spec.task_name, task_kwargs=spec.task_kwargs),
    )
    db.add(job)
    await db.flush()

    apply_entry_queue_state(entry, item_id=item.id, job_id=job.id, queued_at=queued_at or _utc_now())
    await db.commit()

    try:
        task_kwargs = {**spec.task_kwargs, "tenant_id": subscription.tenant_id}
        await enqueue_worker_job(
            arq_pool,
            spec.task_name,
            job_id=str(job.id),
            **task_kwargs,
        )
    except Exception as exc:
        error_message = sanitize_source_subscription_error(exc)
        logger.exception(
            "failed to enqueue source subscription entry entry_id=%s subscription_id=%s provider=%s",
            entry.id,
            subscription.id,
            subscription.provider_type,
        )
        _mark_entry_failed(entry, f"Failed to enqueue ingest task: {error_message}", failed_at=queued_at)
        item.status = "failed"
        job.status = "failed"
        job.error_message = f"Failed to enqueue ingest task: {error_message}"
        job.completed_at = queued_at or _utc_now()
        await db.commit()
        return False
    return True


async def reflect_source_subscription_entry_for_job(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    completed_at: datetime | None = None,
) -> SourceSubscriptionEntry | None:
    result = await db.execute(
        select(SourceSubscriptionEntry).where(SourceSubscriptionEntry.job_id == job_id)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        return None

    job = await db.get(Job, job_id)
    if job is None:
        _mark_entry_failed(entry, "Media ingest job disappeared before completion", failed_at=completed_at)
        await db.commit()
        return entry

    item = await db.get(Item, job.item_id) if job.item_id else None
    completed_at = completed_at or _utc_now()
    if job.status == "completed" and item is not None and item.status == "ready":
        entry.status = "captured"
        entry.item_id = item.id
        entry.captured_at = completed_at
        entry.error_message = None
    elif job.status == "duplicate":
        _mark_entry_skipped(
            entry,
            item_id=job.duplicate_of or entry.item_id,
            skip_reason="duplicate_content",
            skipped_at=completed_at,
        )
    elif job.status in {"failed", "cancelled"}:
        _mark_entry_failed(
            entry,
            job.error_message or f"Media ingest job {job.status}",
            failed_at=completed_at,
        )
    await db.commit()
    return entry


async def _find_existing_active_item(
    db: AsyncSession,
    *,
    tenant_id: str,
    source_url: str,
) -> Item | None:
    result = await db.execute(
        select(Item)
        .where(Item.tenant_id == tenant_id)
        .where(Item.source_url == source_url)
        .where(Item.status != "failed")
        .where(Item.status != "deleted")
        .where(Item.deleted_at.is_(None))
        .limit(1)
    )
    return result.scalar_one_or_none()


def _mark_entry_failed(
    entry: SourceSubscriptionEntry,
    message: str,
    *,
    failed_at: datetime | None = None,
) -> None:
    entry.status = "failed"
    entry.error_message = message[:500]
    entry.failed_at = failed_at or _utc_now()


def _mark_entry_skipped(
    entry: SourceSubscriptionEntry,
    *,
    item_id: uuid.UUID | None,
    skip_reason: str,
    skipped_at: datetime | None = None,
) -> None:
    entry.status = "skipped"
    entry.item_id = item_id
    entry.skip_reason = skip_reason
    entry.skipped_at = skipped_at or _utc_now()
    entry.error_message = None


DEFAULT_SOURCE_SUBSCRIPTION_PROVIDER_REGISTRY = SourceSubscriptionProviderRegistry()
DEFAULT_SOURCE_SUBSCRIPTION_PROVIDER_REGISTRY.register(YoutubeChannelSourceSubscriptionProvider)


async def create_source_subscription(
    db: AsyncSession,
    *,
    tenant_id: str,
    provider_type: str,
    source_url: str,
    display_name: str | None = None,
    auto_tags: list[str] | None = None,
    poll_interval_seconds: int = 3600,
    backfill_enabled: bool = False,
    backfill_limit: int | None = None,
    backfill_published_after: datetime | None = None,
    registry: SourceSubscriptionProviderRegistry = DEFAULT_SOURCE_SUBSCRIPTION_PROVIDER_REGISTRY,
) -> SourceSubscription:
    poll_interval_seconds = max(poll_interval_seconds, settings.source_subscription_poll_min_interval)
    backfill_policy = SourceSubscriptionBackfillPolicy(
        enabled=backfill_enabled,
        limit=backfill_limit,
        published_after=backfill_published_after,
    )
    provider = registry.create(provider_type)
    resolved = await provider.resolve_source(source_url, tenant_id=tenant_id, backfill_policy=backfill_policy)
    if resolved.provider_type != provider_type:
        raise SourceSubscriptionProviderError(
            f"provider {provider_type!r} resolved unexpected type {resolved.provider_type!r}"
        )

    subscription = SourceSubscription(
        tenant_id=tenant_id,
        provider_type=provider_type,
        source_url=resolved.source_url,
        external_id=resolved.external_id,
        external_url=resolved.external_url,
        display_name=display_name or resolved.display_name,
        auto_tags=auto_tags or [],
        poll_interval_seconds=poll_interval_seconds,
        cursor=resolved.cursor,
        provider_metadata=resolved.metadata,
        status="active",
        consecutive_failures=0,
    )
    db.add(subscription)
    await db.flush()
    return subscription


async def poll_source_subscription(
    db: AsyncSession,
    subscription: SourceSubscription,
    *,
    registry: SourceSubscriptionProviderRegistry = DEFAULT_SOURCE_SUBSCRIPTION_PROVIDER_REGISTRY,
    checked_at: datetime | None = None,
) -> list[SourceSubscriptionEntry]:
    checked_at = checked_at or _utc_now()
    if subscription.status != "active":
        subscription.last_checked_at = checked_at
        await db.flush()
        return []

    provider = registry.create(subscription.provider_type)
    try:
        discovery_result = _source_subscription_discovery_result(await provider.discover_entries(subscription))
    except Exception as exc:
        error_message = sanitize_source_subscription_error(exc)
        subscription.last_checked_at = checked_at
        subscription.last_error = error_message
        subscription.consecutive_failures = int(subscription.consecutive_failures or 0) + 1
        if subscription.consecutive_failures >= settings.source_subscription_max_failures:
            subscription.status = "paused"
            subscription.paused_reason = "max_discovery_failures"
        await db.flush()
        logger.warning(
            "source subscription discovery failed subscription_id=%s provider=%s failure_count=%s auto_paused=%s",
            subscription.id,
            subscription.provider_type,
            subscription.consecutive_failures,
            subscription.status == "paused",
        )
        return []

    created_entries: list[SourceSubscriptionEntry] = []
    discovered_entries = discovery_result.entries
    seen_provider_entry_ids = _cursor_seen_provider_entry_ids(subscription.cursor)
    latest_seen_at = _parse_cursor_datetime(subscription.cursor or {}, "last_seen_published_at")
    backfill_cursor = _cursor_backfill(subscription.cursor)
    backfill_remaining = _cursor_backfill_remaining(backfill_cursor)
    backfill_boundary_provider_entry_id: str | None = None
    complete_backfill_after_poll = bool(backfill_cursor.get("enabled")) and (
        backfill_remaining is None or discovery_result.provider_exhausted
    )
    seen_batch_keys: set[tuple[str, str]] = set()

    for discovered in discovered_entries:
        discovered_keys = _discovered_entry_keys(discovered)
        if discovered_keys & seen_batch_keys:
            _merge_seen_cursor(seen_provider_entry_ids, discovered.provider_entry_id)
            latest_seen_at = _latest_datetime(latest_seen_at, discovered.published_at)
            continue
        existing_entry = await _find_existing_source_entry(db, subscription=subscription, discovered=discovered)
        if existing_entry is not None:
            seen_batch_keys.update(discovered_keys)
            _merge_seen_cursor(seen_provider_entry_ids, discovered.provider_entry_id)
            latest_seen_at = _latest_datetime(latest_seen_at, discovered.published_at)
            continue

        entry = SourceSubscriptionEntry(
            tenant_id=subscription.tenant_id,
            subscription_id=subscription.id,
            provider_entry_id=discovered.provider_entry_id,
            source_url=discovered.source_url,
            title=discovered.title,
            published_at=discovered.published_at,
            status="skipped" if discovered.skip_reason else "discovered",
            skip_reason=discovered.skip_reason,
            skipped_at=checked_at if discovered.skip_reason else None,
            metadata_=discovered.metadata,
        )
        db.add(entry)
        await db.flush()
        created_entries.append(entry)
        seen_batch_keys.update(discovered_keys)
        _merge_seen_cursor(seen_provider_entry_ids, discovered.provider_entry_id)
        latest_seen_at = _latest_datetime(latest_seen_at, discovered.published_at)
        if discovered.skip_reason is None and backfill_remaining is not None:
            previous_backfill_remaining = backfill_remaining
            backfill_remaining = max(backfill_remaining - 1, 0)
            if previous_backfill_remaining > 0 and backfill_remaining <= 0:
                backfill_boundary_provider_entry_id = discovered.provider_entry_id

    subscription.last_checked_at = checked_at
    subscription.last_error = None
    subscription.consecutive_failures = 0
    if created_entries:
        subscription.last_discovered_at = checked_at
    if complete_backfill_after_poll and discovery_result.provider_exhausted:
        logger.info(
            "source subscription backfill completed after provider exhaustion subscription_id=%s provider=%s remaining=%s created_entries=%s",
            subscription.id,
            subscription.provider_type,
            backfill_remaining,
            len(created_entries),
        )
    subscription.cursor = _updated_discovery_cursor(
        subscription.cursor,
        seen_provider_entry_ids=seen_provider_entry_ids,
        latest_seen_at=latest_seen_at,
        backfill_remaining=backfill_remaining,
        backfill_boundary_provider_entry_id=backfill_boundary_provider_entry_id,
        complete_backfill=complete_backfill_after_poll,
    )
    await db.flush()
    return created_entries


def _source_subscription_discovery_result(
    result: list[DiscoveredSourceEntry] | SourceSubscriptionDiscoveryResult,
) -> SourceSubscriptionDiscoveryResult:
    if isinstance(result, SourceSubscriptionDiscoveryResult):
        return result
    return SourceSubscriptionDiscoveryResult(entries=result)


async def _find_existing_source_entry(
    db: AsyncSession,
    *,
    subscription: SourceSubscription,
    discovered: DiscoveredSourceEntry,
) -> SourceSubscriptionEntry | None:
    if discovered.provider_entry_id:
        result = await db.execute(
            select(SourceSubscriptionEntry).where(
                SourceSubscriptionEntry.tenant_id == subscription.tenant_id,
                SourceSubscriptionEntry.subscription_id == subscription.id,
                SourceSubscriptionEntry.provider_entry_id == discovered.provider_entry_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

    if discovered.source_url:
        result = await db.execute(
            select(SourceSubscriptionEntry).where(
                SourceSubscriptionEntry.tenant_id == subscription.tenant_id,
                SourceSubscriptionEntry.subscription_id == subscription.id,
                SourceSubscriptionEntry.source_url == discovered.source_url,
            )
        )
        return result.scalar_one_or_none()
    return None


def _discovered_entry_keys(discovered: DiscoveredSourceEntry) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if discovered.provider_entry_id:
        keys.add(("provider_entry_id", discovered.provider_entry_id))
    if discovered.source_url:
        keys.add(("source_url", discovered.source_url))
    return keys


def _cursor_seen_provider_entry_ids(cursor: dict[str, Any] | None) -> list[str]:
    seen = (cursor or {}).get("seen_provider_entry_ids", [])
    if not isinstance(seen, list):
        return []
    return [str(value) for value in seen if value]


def _merge_seen_cursor(seen_provider_entry_ids: list[str], provider_entry_id: str | None) -> None:
    if provider_entry_id and provider_entry_id not in seen_provider_entry_ids:
        seen_provider_entry_ids.insert(0, provider_entry_id)
        del seen_provider_entry_ids[200:]


def _latest_datetime(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current


def _updated_discovery_cursor(
    cursor: dict[str, Any] | None,
    *,
    seen_provider_entry_ids: list[str],
    latest_seen_at: datetime | None,
    backfill_remaining: int | None = None,
    backfill_boundary_provider_entry_id: str | None = None,
    complete_backfill: bool = False,
) -> dict[str, Any]:
    updated = dict(cursor or {})
    backfill_cursor = _cursor_backfill(updated)
    if backfill_remaining is not None:
        backfill_cursor["remaining"] = backfill_remaining
        if backfill_remaining <= 0:
            backfill_cursor["completed"] = True
            backfill_cursor["enabled"] = False
            if backfill_boundary_provider_entry_id:
                backfill_cursor["boundary_provider_entry_id"] = backfill_boundary_provider_entry_id
    if complete_backfill:
        backfill_cursor["completed"] = True
        backfill_cursor["enabled"] = False
    updated["no_backfill"] = not bool(backfill_cursor.get("enabled"))
    updated[SOURCE_SUBSCRIPTION_BACKFILL_CURSOR_KEY] = backfill_cursor
    updated["seen_provider_entry_ids"] = seen_provider_entry_ids
    if latest_seen_at is not None:
        updated["last_seen_published_at"] = _isoformat_datetime(latest_seen_at)
    return updated


def validate_source_subscription_tenant(subscription: SourceSubscription, entry: SourceSubscriptionEntry) -> None:
    if subscription.tenant_id != entry.tenant_id:
        raise ValueError(
            f"source subscription tenant mismatch: subscription={subscription.tenant_id!r} entry={entry.tenant_id!r}"
        )
    if entry.subscription_id != subscription.id:
        raise ValueError(
            "source subscription entry does not belong to subscription "
            f"{subscription.id} (entry subscription_id={entry.subscription_id})"
        )


def apply_entry_queue_state(
    entry: SourceSubscriptionEntry,
    *,
    item_id: uuid.UUID,
    job_id: uuid.UUID,
    queued_at: datetime,
) -> None:
    entry.item_id = item_id
    entry.job_id = job_id
    entry.status = "queued"
    entry.queued_at = queued_at
    entry.failed_at = None
    entry.skipped_at = None
    entry.skip_reason = None
    entry.error_message = None


def enforce_source_subscription_manual_sync_cooldown(
    subscription: SourceSubscription,
    *,
    now: datetime | None = None,
) -> int | None:
    """Return remaining cooldown seconds when manual sync should be rejected."""
    now = now or _utc_now()
    last_manual_sync_at = _parse_cursor_datetime(subscription.cursor or {}, SOURCE_SUBSCRIPTION_MANUAL_SYNC_CURSOR_KEY)
    if last_manual_sync_at is None:
        return None
    elapsed = (now - last_manual_sync_at).total_seconds()
    remaining = settings.source_subscription_manual_sync_cooldown_seconds - int(elapsed)
    return remaining if remaining > 0 else None


def record_source_subscription_manual_sync(subscription: SourceSubscription, *, synced_at: datetime | None = None) -> None:
    synced_at = synced_at or _utc_now()
    cursor = dict(subscription.cursor or {})
    cursor[SOURCE_SUBSCRIPTION_MANUAL_SYNC_CURSOR_KEY] = _isoformat_datetime(synced_at)
    subscription.cursor = cursor


async def diagnose_stale_queued_source_subscription_entries(
    db: AsyncSession,
    *,
    stale_after: timedelta | None = None,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    """Reconcile or annotate queued source entries that appear stranded."""
    now = now or _utc_now()
    stale_after = stale_after or timedelta(minutes=settings.source_subscription_stale_queued_minutes)
    cutoff = now - stale_after
    result = await db.execute(
        select(SourceSubscriptionEntry)
        .where(SourceSubscriptionEntry.status == "queued")
        .where(SourceSubscriptionEntry.queued_at.is_not(None))
        .where(SourceSubscriptionEntry.queued_at < cutoff)
        .order_by(SourceSubscriptionEntry.queued_at.asc())
        .limit(limit)
    )
    entries = result.scalars().all()

    diagnosed = 0
    for entry in entries:
        if entry.job_id is None:
            _mark_entry_failed(entry, "Queued source subscription entry has no ingest job", failed_at=now)
            diagnosed += 1
            continue
        reflected = await reflect_source_subscription_entry_for_job(db, job_id=entry.job_id, completed_at=now)
        if reflected is None:
            _mark_entry_failed(entry, "Queued source subscription entry references a missing ingest job", failed_at=now)
            diagnosed += 1
        elif reflected.status == "queued":
            reflected.error_message = "Queued source subscription entry is stale; ingest job is still pending or processing"
            diagnosed += 1

    if diagnosed:
        await db.commit()
    return diagnosed
