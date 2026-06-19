import asyncio
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.embedding_profile import resolve_embedding_profile
from app.models.embedding import Embedding
from app.models.item import Item
from app.models.job import Job, JobProgressEvent
from app.pipelines.base import BasePipeline, stable_merge_tags
from app.pipelines.youtube import MediaPendingAvailabilityError, MediaPipeline, MediaTranscriptionLimitError, TranscriptionChunk
from app.pipelines.youtube import _render_transcript
from app.workers.queues import (
    DEFAULT_WORKER_QUEUE,
    MEDIA_FAIR_DISPATCH_TASK_NAME,
    PALACE_WORKER_QUEUE,
    singleton_job_id,
)
from app.workers.tasks import backfill_missing_taxonomy, process_media, process_youtube


class FakeYoutubeDL:
    def __init__(self, info: dict) -> None:
        self.info = info
        self.download_calls: list[list[str]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url: str, *, download: bool):
        assert url == "https://example.com/watch?v=too-long"
        assert download is False
        return self.info

    def download(self, urls: list[str]) -> None:
        self.download_calls.append(urls)


class FakeYoutubeDLFactory:
    def __init__(self, info: dict) -> None:
        self.instance = FakeYoutubeDL(info)

    def __call__(self, _opts):
        return self.instance


class SessionFactory:
    def __init__(self, session) -> None:
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePipeline:
    def __init__(self, _db, _embedder, _llm) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def process(self, job_id: uuid.UUID, *, url: str, tenant_id: str, model: str | None = None):
        self.calls.append((str(job_id), url, model))
        raise MediaTranscriptionLimitError("source exceeds configured transcription window")


class PendingAvailabilityFakePipeline:
    def __init__(self, _db, _embedder, _llm) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def process(self, job_id: uuid.UUID, *, url: str, tenant_id: str, model: str | None = None):
        self.calls.append((str(job_id), url, model))
        raise MediaPendingAvailabilityError(
            "ERROR: [youtube] ZL0FdHH7wwc: Premieres in 2 hours",
            retry_after_seconds=7200,
            user_message="YouTube says this video is scheduled but not available yet; Palace will retry after the premiere window.",
        )


class SuccessfulFakePipeline:
    def __init__(self, _db, _embedder, _llm) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def process(self, job_id: uuid.UUID, *, url: str, tenant_id: str, model: str | None = None):
        self.calls.append((str(job_id), url, model))
        return uuid.UUID("11111111-1111-4111-8111-111111111111")


class FakeRedis:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


class FakePipelineSession:
    def __init__(self, *, job: Job, item: Item) -> None:
        self.job = job
        self.item = item
        self.commits = 0
        self.rollbacks = 0
        self.progress_events: list[JobProgressEvent] = []
        self.embeddings: list[Embedding] = []
        self.scalar_values: list[object | None] = []
        self.scalar_statements: list[str] = []

    async def get(self, model, key):
        if model is Job:
            return self.job if key == self.job.id else None
        if model is Item:
            return self.item if key == self.item.id else None
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def scalar(self, _statement):
        self.scalar_statements.append(str(_statement))
        if self.scalar_values:
            return self.scalar_values.pop(0)
        return None

    async def execute(self, statement):
        if "SELECT DISTINCT unnest(tags)" in str(statement):
            return FakeTagResult([])
        return FakeProgressEventResult(self.progress_events)

    async def flush(self) -> None:
        return None

    def add(self, value) -> None:
        if isinstance(value, JobProgressEvent):
            self.progress_events.append(value)
            return
        if isinstance(value, Embedding):
            self.embeddings.append(value)
            return
        raise AssertionError(f"Unexpected added model: {type(value)!r}")


class FakeProgressEventResult:
    def __init__(self, events: list[JobProgressEvent]) -> None:
        self.events = events

    def scalars(self):
        return self

    def all(self):
        return [event.id for event in self.events if event.id is not None]


class FakeTagResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class CancellingPipeline(BasePipeline):
    async def extract(self, **_kwargs):
        raise asyncio.CancelledError


class ImageHashPipeline(BasePipeline):
    async def extract(self, **_kwargs):
        return (
            "Diagram of the control plane",
            {
                "filename": "board.png",
                "media_type": "image/png",
                "image_analysis": {
                    "byte_hash": "a" * 64,
                    "caption": "Diagram of the control plane",
                    "visible_text": [],
                    "objects": [],
                    "entities": [],
                },
            },
        )


class TextPipeline(BasePipeline):
    async def extract(self, **_kwargs):
        return (
            "Robotics lab notes about AI autonomy.",
            {"manual_tags": ["Manual", "research"]},
        )


class PendingAvailabilityPipeline(BasePipeline):
    async def extract(self, **_kwargs):
        raise MediaPendingAvailabilityError(
            "ERROR: [youtube] ZL0FdHH7wwc: Premieres in 2 hours",
            retry_after_seconds=7200,
            user_message="YouTube says this video is scheduled but not available yet; Palace will retry after the premiere window.",
        )


class FakeEmbedder:
    profile = resolve_embedding_profile()

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.profile.dimensions for _text in texts]


class FakeLlm:
    async def summarize(self, _text: str, model: str | None = None) -> str:
        return "summary"

    async def generate_tags(self, _text: str, *, existing_tags: list[str], model: str | None = None):
        return ([], [])

    async def extract_entities(self, _text: str, model: str | None = None):
        return None


class TaxonomyFakeLlm(FakeLlm):
    async def generate_tags(self, _text: str, *, existing_tags: list[str], model: str | None = None):
        return (["AI", "research", ""], ["Technology"])


class FakeBackfillResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class FakeBackfillSession:
    def __init__(self, items: list[Item]) -> None:
        self.items = {item.id: item for item in items}
        self.commits = 0
        self.params: list[dict] = []

    async def execute(self, statement, _params=None):
        statement_text = str(statement)
        if _params is not None:
            self.params.append(_params)
        if "SELECT i.id, i.title" in statement_text:
            source_types = set((_params or {}).get("source_types") or [])
            return FakeBackfillResult(
                [
                    SimpleNamespace(
                        id=item.id,
                        title=item.title,
                        source_type=item.source_type,
                        job_type=(item.metadata_ or {}).get("test_job_type", "unknown"),
                    )
                    for item in self.items.values()
                    if not source_types or item.source_type in source_types
                ]
            )
        if "SELECT DISTINCT unnest(tags)" in statement_text:
            return FakeBackfillResult([SimpleNamespace(tag="research")])
        raise AssertionError(f"unexpected statement: {statement_text}")

    async def get(self, model, key):
        if model is Item:
            return self.items.get(key)
        return None

    async def commit(self) -> None:
        self.commits += 1


def test_download_audio_extracts_metadata_and_downloads_even_for_long_media(monkeypatch, tmp_path: Path) -> None:
    factory = FakeYoutubeDLFactory(
        {
            "id": "video-1",
            "title": "Long briefing",
            "duration": 1511.545,
            "uploader": "Hermes",
            "description": "https://example.com/source",
        }
    )
    monkeypatch.setattr("app.pipelines.youtube.yt_dlp.YoutubeDL", factory)
    monkeypatch.setattr("app.pipelines.youtube._TEMP_DIR", str(tmp_path))

    audio_path = tmp_path / "job-123.m4a"

    def fake_download(urls: list[str]) -> None:
        factory.instance.download_calls.append(urls)
        audio_path.write_bytes(b"audio")

    factory.instance.download = fake_download

    downloaded_path, metadata = MediaPipeline._download_audio("https://example.com/watch?v=too-long", "job-123")

    assert downloaded_path == str(audio_path)
    assert metadata["duration_seconds"] == 1511.545
    assert factory.instance.download_calls == [["https://example.com/watch?v=too-long"]]


def test_download_audio_probes_duration_when_provider_metadata_omits_it(monkeypatch, tmp_path: Path) -> None:
    factory = FakeYoutubeDLFactory(
        {
            "id": "video-1",
            "title": "Direct video",
            "duration": None,
            "uploader": "Hermes",
            "description": "",
        }
    )
    monkeypatch.setattr("app.pipelines.youtube.yt_dlp.YoutubeDL", factory)
    monkeypatch.setattr("app.pipelines.youtube._TEMP_DIR", str(tmp_path))
    monkeypatch.setattr("app.pipelines.youtube._probe_audio_duration_seconds", lambda _path: 901.5)

    audio_path = tmp_path / "job-123.m4a"

    def fake_download(urls: list[str]) -> None:
        factory.instance.download_calls.append(urls)
        audio_path.write_bytes(b"audio")

    factory.instance.download = fake_download

    downloaded_path, metadata = MediaPipeline._download_audio("https://example.com/watch?v=too-long", "job-123")

    assert downloaded_path == str(audio_path)
    assert metadata["duration_seconds"] == 901.5
    assert metadata["duration"] == 901.5
    assert metadata["duration_source"] == "ffprobe"
    assert factory.instance.download_calls == [["https://example.com/watch?v=too-long"]]


def test_download_audio_maps_youtube_premiere_error_to_pending_availability(monkeypatch, tmp_path: Path) -> None:
    factory = FakeYoutubeDLFactory({})
    monkeypatch.setattr("app.pipelines.youtube.yt_dlp.YoutubeDL", factory)
    monkeypatch.setattr("app.pipelines.youtube._TEMP_DIR", str(tmp_path))

    def fake_extract_info(_url: str, *, download: bool):
        raise RuntimeError("ERROR: [youtube] ZL0FdHH7wwc: Premieres in 2 hours")

    factory.instance.extract_info = fake_extract_info

    with pytest.raises(MediaPendingAvailabilityError) as exc_info:
        MediaPipeline._download_audio("https://example.com/watch?v=too-long", "job-123")

    assert exc_info.value.retry_after_seconds == 7200
    assert "scheduled but not available yet" in exc_info.value.user_message


def test_download_audio_keeps_non_premiere_ytdlp_errors_hard_failures(monkeypatch, tmp_path: Path) -> None:
    factory = FakeYoutubeDLFactory({})
    monkeypatch.setattr("app.pipelines.youtube.yt_dlp.YoutubeDL", factory)
    monkeypatch.setattr("app.pipelines.youtube._TEMP_DIR", str(tmp_path))

    def fake_extract_info(_url: str, *, download: bool):
        raise RuntimeError("ERROR: [youtube] missing-video: Video unavailable")

    factory.instance.extract_info = fake_extract_info

    with pytest.raises(RuntimeError, match="Video unavailable"):
        MediaPipeline._download_audio("https://example.com/watch?v=too-long", "job-123")


def test_prepare_transcription_inputs_splits_long_or_large_media(monkeypatch, tmp_path: Path) -> None:
    audio_path = tmp_path / "job-123.m4a"
    audio_path.write_bytes(b"x" * (30 * 1024 * 1024))

    created_chunks: list[tuple[str, float, float]] = []

    def fake_split_audio_with_ffmpeg(*, input_path: str, output_path: str, start_seconds: float, duration_seconds: float) -> None:
        assert input_path == str(audio_path)
        Path(output_path).write_bytes(b"x" * (10 * 1024 * 1024))
        created_chunks.append((output_path, start_seconds, duration_seconds))

    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_max_duration_seconds", 1400)
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_max_chunk_seconds", 600)
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_max_upload_bytes", 25 * 1024 * 1024)
    monkeypatch.setattr("app.pipelines.youtube._split_audio_with_ffmpeg", fake_split_audio_with_ffmpeg)

    chunks = MediaPipeline._prepare_transcription_inputs(
        str(audio_path),
        {"duration_seconds": 3600},
        "job-123",
    )

    assert len(chunks) >= 2
    assert all(isinstance(chunk, TranscriptionChunk) for chunk in chunks)
    assert all(chunk.duration_seconds <= 600 for chunk in chunks)
    assert created_chunks[0][1] == 0.0
    assert sum(chunk.duration_seconds for chunk in chunks) == pytest.approx(3600)


@pytest.mark.asyncio
async def test_extract_long_media_stitches_diarized_chunks(monkeypatch, tmp_path: Path) -> None:
    audio_path = tmp_path / "job-123.m4a"
    chunk_a = tmp_path / "job-123-chunk-0.m4a"
    chunk_b = tmp_path / "job-123-chunk-1.m4a"
    for path in (audio_path, chunk_a, chunk_b):
        path.write_bytes(b"audio")

    pipeline = MediaPipeline(None, None, None)  # type: ignore[arg-type]

    async def fake_transcribe(path: str, *, offset_seconds: float = 0.0, **_kwargs):
        if path == str(chunk_a):
            return (
                "[00:00:00] Speaker 1: Opening line",
                [{"speaker": "Speaker 1", "start_seconds": 0.0, "end_seconds": 5.0, "text": "Opening line"}],
            )
        if path == str(chunk_b):
            return (
                "[00:23:20] Speaker 2: Follow-up",
                [{"speaker": "Speaker 2", "start_seconds": offset_seconds, "end_seconds": offset_seconds + 4.0, "text": "Follow-up"}],
            )
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(
        "app.pipelines.youtube.MediaPipeline._download_audio",
        staticmethod(lambda _url, _job_id: (str(audio_path), {"duration_seconds": 1512, "description": "Shownotes"})),
    )
    monkeypatch.setattr(
        "app.pipelines.youtube.MediaPipeline._prepare_transcription_inputs",
        staticmethod(
            lambda _audio_path, _meta, _job_id: [
                TranscriptionChunk(path=str(chunk_a), start_seconds=0.0, duration_seconds=1400, cleanup=True),
                TranscriptionChunk(path=str(chunk_b), start_seconds=1400.0, duration_seconds=112, cleanup=True),
            ]
        ),
    )
    monkeypatch.setattr(pipeline, "_transcribe", fake_transcribe)

    transcript, metadata = await pipeline.extract("https://example.com/watch?v=long", job_id="job-123")

    assert "Speaker 1: Opening line" in transcript
    assert "Speaker 2: Follow-up" in transcript
    assert "Video Description:\nShownotes" in transcript
    assert metadata["transcription_chunk_count"] == 2
    assert metadata["speaker_segments"][1]["start_seconds"] == 1400.0


@pytest.mark.asyncio
async def test_extract_long_media_records_chunk_progress_in_order(monkeypatch, tmp_path: Path) -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Long media",
        source_type="media",
        status="processing",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="media",
        tenant_id="tenant-a",
        status="processing",
        progress=0,
    )
    session = FakePipelineSession(job=job, item=item)
    pipeline = MediaPipeline(session, None, None)  # type: ignore[arg-type]

    audio_path = tmp_path / "job.m4a"
    chunk_a = tmp_path / "chunk-a.m4a"
    chunk_b = tmp_path / "chunk-b.m4a"
    audio_path.write_bytes(b"audio")
    chunk_a.write_bytes(b"a" * 10)
    chunk_b.write_bytes(b"b" * 20)

    async def fake_transcribe(path: str, *, offset_seconds: float = 0.0, **_kwargs):
        if path == str(chunk_a):
            return ("first", [])
        if path == str(chunk_b):
            return ("second", [])
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(
        "app.pipelines.youtube.MediaPipeline._download_audio",
        staticmethod(lambda _url, _job_id: (str(audio_path), {"duration_seconds": 1200})),
    )
    monkeypatch.setattr(
        "app.pipelines.youtube.MediaPipeline._prepare_transcription_inputs",
        staticmethod(
            lambda _audio_path, _meta, _job_id: [
                TranscriptionChunk(path=str(chunk_a), start_seconds=0.0, duration_seconds=600.0, size_bytes=10),
                TranscriptionChunk(path=str(chunk_b), start_seconds=600.0, duration_seconds=600.0, size_bytes=20),
            ]
        ),
    )
    monkeypatch.setattr(pipeline, "_transcribe", fake_transcribe)

    transcript, metadata = await pipeline.extract("https://example.com/watch?v=long", job_id=str(job_id))

    assert transcript == "first\nsecond"
    assert metadata["transcription_chunk_count"] == 2
    chunk_event = next(
        event for event in session.progress_events if event.phase == "chunk" and event.status == "completed"
    )
    assert chunk_event.message == "Prepared 2 transcription chunk(s)"
    assert chunk_event.metadata_["chunks"] == [
        {"chunk_index": 1, "chunk_count": 2, "start_seconds": 0.0, "duration_seconds": 600.0, "size_bytes": 10},
        {"chunk_index": 2, "chunk_count": 2, "start_seconds": 600.0, "duration_seconds": 600.0, "size_bytes": 20},
    ]
    transcribe_events = [
        event
        for event in session.progress_events
        if event.phase == "transcribe" and event.message.startswith("Transcribing chunk")
    ]
    assert [event.message for event in transcribe_events] == [
        "Transcribing chunk 1/2",
        "Transcribing chunk 2/2",
    ]
    assert [event.metadata_["chunk_index"] for event in transcribe_events] == [1, 2]


@pytest.mark.asyncio
async def test_extract_long_media_transcribes_chunks_concurrently_and_stitches_in_order(monkeypatch, tmp_path: Path) -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = FakePipelineSession(
        job=Job(
            id=job_id,
            item_id=item_id,
            job_type="media",
            tenant_id="tenant-a",
            status="processing",
            progress=0,
        ),
        item=Item(
            id=item_id,
            tenant_id="tenant-a",
            title="Parallel media",
            source_type="media",
            status="processing",
        ),
    )
    pipeline = MediaPipeline(session, None, None)  # type: ignore[arg-type]

    audio_path = tmp_path / "job.m4a"
    chunk_a = tmp_path / "chunk-a.m4a"
    chunk_b = tmp_path / "chunk-b.m4a"
    for path in (audio_path, chunk_a, chunk_b):
        path.write_bytes(b"audio")

    first_chunk_can_finish = asyncio.Event()
    second_chunk_started = asyncio.Event()
    calls: list[str] = []

    async def fake_transcribe(path: str, *, offset_seconds: float = 0.0, **_kwargs):
        calls.append(path)
        if path == str(chunk_a):
            await second_chunk_started.wait()
            await first_chunk_can_finish.wait()
            return (
                "first",
                [{"speaker": "Speaker 1", "start_seconds": offset_seconds, "end_seconds": offset_seconds + 1.0}],
            )
        if path == str(chunk_b):
            second_chunk_started.set()
            first_chunk_can_finish.set()
            return (
                "second",
                [{"speaker": "Speaker 2", "start_seconds": offset_seconds, "end_seconds": offset_seconds + 1.0}],
            )
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_max_parallel_chunks", 2)
    monkeypatch.setattr(
        "app.pipelines.youtube.MediaPipeline._download_audio",
        staticmethod(lambda _url, _job_id: (str(audio_path), {"duration_seconds": 1200})),
    )
    monkeypatch.setattr(
        "app.pipelines.youtube.MediaPipeline._prepare_transcription_inputs",
        staticmethod(
            lambda _audio_path, _meta, _job_id: [
                TranscriptionChunk(path=str(chunk_a), start_seconds=0.0, duration_seconds=600.0, size_bytes=10),
                TranscriptionChunk(path=str(chunk_b), start_seconds=600.0, duration_seconds=600.0, size_bytes=20),
            ]
        ),
    )
    monkeypatch.setattr(pipeline, "_transcribe", fake_transcribe)

    transcript, metadata = await pipeline.extract("https://example.com/watch?v=long", job_id=str(job_id))

    assert calls == [str(chunk_a), str(chunk_b)]
    assert transcript == "first\nsecond"
    assert [segment["start_seconds"] for segment in metadata["speaker_segments"]] == [0.0, 600.0]
    transcribe_events = [
        event
        for event in session.progress_events
        if event.phase == "transcribe" and event.message.startswith("Transcribing chunk")
    ]
    assert [event.metadata_["parallel_chunk_limit"] for event in transcribe_events] == [2, 2]


@pytest.mark.asyncio
async def test_extract_long_media_failure_cancels_pending_chunks_and_cleans_temp_files(monkeypatch, tmp_path: Path) -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = FakePipelineSession(
        job=Job(
            id=job_id,
            item_id=item_id,
            job_type="media",
            tenant_id="tenant-a",
            status="processing",
            progress=0,
        ),
        item=Item(
            id=item_id,
            tenant_id="tenant-a",
            title="Failed parallel media",
            source_type="media",
            status="processing",
        ),
    )
    pipeline = MediaPipeline(session, None, None)  # type: ignore[arg-type]

    audio_path = tmp_path / "job.m4a"
    chunk_a = tmp_path / "chunk-a.m4a"
    chunk_b = tmp_path / "chunk-b.m4a"
    for path in (audio_path, chunk_a, chunk_b):
        path.write_bytes(b"audio")

    slow_chunk_started = asyncio.Event()
    slow_chunk_cancelled = asyncio.Event()

    async def fake_transcribe(path: str, **_kwargs):
        if path == str(chunk_a):
            await slow_chunk_started.wait()
            raise RuntimeError("provider failed for chunk 1")
        if path == str(chunk_b):
            slow_chunk_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                slow_chunk_cancelled.set()
                raise
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_max_parallel_chunks", 2)
    monkeypatch.setattr(
        "app.pipelines.youtube.MediaPipeline._download_audio",
        staticmethod(lambda _url, _job_id: (str(audio_path), {"duration_seconds": 1200})),
    )
    monkeypatch.setattr(
        "app.pipelines.youtube.MediaPipeline._prepare_transcription_inputs",
        staticmethod(
            lambda _audio_path, _meta, _job_id: [
                TranscriptionChunk(path=str(chunk_a), start_seconds=0.0, duration_seconds=600.0, cleanup=True),
                TranscriptionChunk(path=str(chunk_b), start_seconds=600.0, duration_seconds=600.0, cleanup=True),
            ]
        ),
    )
    monkeypatch.setattr(pipeline, "_transcribe", fake_transcribe)

    with pytest.raises(RuntimeError, match="provider failed for chunk 1"):
        await pipeline.extract("https://example.com/watch?v=long", job_id=str(job_id))

    assert slow_chunk_cancelled.is_set()
    assert not audio_path.exists()
    assert not chunk_a.exists()
    assert not chunk_b.exists()


@pytest.mark.asyncio
async def test_media_progress_metadata_redacts_sensitive_fields() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Sensitive media",
        source_type="media",
        status="processing",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="media",
        tenant_id="tenant-a",
        status="processing",
        progress=0,
    )
    session = FakePipelineSession(job=job, item=item)
    pipeline = MediaPipeline(session, None, None)  # type: ignore[arg-type]

    await pipeline._set_extract_progress(
        str(job_id),
        16,
        "transcribe",
        message="Transcribing chunk 1/1",
        metadata={
            "chunk_index": 1,
            "description": "private transcript-adjacent description",
            "api_key": "sk-secret",
        },
    )

    assert session.progress_events[0].metadata_ == {
        "chunk_index": 1,
        "description": "[redacted]",
        "api_key": "[redacted]",
    }


@pytest.mark.asyncio
async def test_process_media_swallows_non_retryable_duration_errors(monkeypatch) -> None:
    redis = FakeRedis()
    dispatched_job_ids: list[str] = []

    async def fake_webhook_dispatch(_redis, job_id: str) -> None:
        dispatched_job_ids.append(job_id)

    monkeypatch.setattr("app.workers.tasks.MediaPipeline", FakePipeline)
    monkeypatch.setattr("app.workers.tasks.async_session", SessionFactory(object()))
    monkeypatch.setattr("app.workers.tasks.maybe_dispatch_webhook", fake_webhook_dispatch)

    job_id = str(uuid.uuid4())
    await process_media(
        {"embedder": object(), "llm": object(), "redis": redis},
        job_id=job_id,
        url="https://example.com/watch?v=too-long",
        tenant_id="tenant-a",
    )

    assert redis.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        )
    ]
    assert dispatched_job_ids == [job_id]


@pytest.mark.asyncio
async def test_process_media_schedules_pending_availability_retry_wake(monkeypatch) -> None:
    redis = FakeRedis()
    dispatched_job_ids: list[str] = []

    async def fake_webhook_dispatch(_redis, job_id: str) -> None:
        dispatched_job_ids.append(job_id)

    monkeypatch.setattr("app.workers.tasks.MediaPipeline", PendingAvailabilityFakePipeline)
    monkeypatch.setattr("app.workers.tasks.async_session", SessionFactory(object()))
    monkeypatch.setattr("app.workers.tasks.maybe_dispatch_webhook", fake_webhook_dispatch)

    job_id = str(uuid.uuid4())
    await process_media(
        {"embedder": object(), "llm": object(), "redis": redis},
        job_id=job_id,
        url="https://youtube.com/watch?v=ZL0FdHH7wwc",
        tenant_id="tenant-a",
    )

    assert redis.enqueued[0][0] == MEDIA_FAIR_DISPATCH_TASK_NAME
    assert redis.enqueued[0][1]["_defer_by"] == 7200
    assert redis.enqueued[1] == (
        MEDIA_FAIR_DISPATCH_TASK_NAME,
        {
            "_queue_name": DEFAULT_WORKER_QUEUE,
            "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
        },
    )
    assert dispatched_job_ids == [job_id]


@pytest.mark.asyncio
async def test_process_media_routes_follow_on_relationships_to_default_queue(monkeypatch) -> None:
    redis = FakeRedis()
    dispatched_job_ids: list[str] = []

    async def fake_webhook_dispatch(_redis, job_id: str) -> None:
        dispatched_job_ids.append(job_id)

    monkeypatch.setattr("app.workers.tasks.MediaPipeline", SuccessfulFakePipeline)
    monkeypatch.setattr("app.workers.tasks.async_session", SessionFactory(object()))
    monkeypatch.setattr("app.workers.tasks.maybe_dispatch_webhook", fake_webhook_dispatch)

    job_id = str(uuid.uuid4())
    await process_media(
        {"embedder": object(), "llm": object(), "redis": redis},
        job_id=job_id,
        url="https://example.com/watch?v=media",
        tenant_id="tenant-a",
    )

    assert redis.enqueued == [
        (
            "extract_relationships",
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "item_id": "11111111-1111-4111-8111-111111111111",
                "tenant_id": "tenant-a",
            },
        ),
        (
            "mark_item_dirty_and_schedule",
            {
                "_queue_name": PALACE_WORKER_QUEUE,
                "item_id": "11111111-1111-4111-8111-111111111111",
                "tenant_id": "tenant-a",
                "reason": "ingest",
            },
        ),
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        ),
    ]
    assert dispatched_job_ids == [job_id]


@pytest.mark.asyncio
async def test_process_youtube_routes_follow_on_relationships_to_default_queue(monkeypatch) -> None:
    redis = FakeRedis()

    async def fake_webhook_dispatch(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.workers.tasks.MediaPipeline", SuccessfulFakePipeline)
    monkeypatch.setattr("app.workers.tasks.async_session", SessionFactory(object()))
    monkeypatch.setattr("app.workers.tasks.maybe_dispatch_webhook", fake_webhook_dispatch)

    await process_youtube(
        {"embedder": object(), "llm": object(), "redis": redis},
        job_id=str(uuid.uuid4()),
        url="https://example.com/watch?v=legacy",
        tenant_id="tenant-a",
    )

    assert redis.enqueued[0] == (
        "extract_relationships",
        {
            "_queue_name": DEFAULT_WORKER_QUEUE,
            "item_id": "11111111-1111-4111-8111-111111111111",
            "tenant_id": "tenant-a",
        },
    )


@pytest.mark.asyncio
async def test_base_pipeline_marks_cancelled_job_terminal_and_retryable() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Long media",
        source_type="media",
        status="processing",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="media",
        tenant_id="tenant-a",
        status="queued",
        progress=0,
    )
    session = FakePipelineSession(job=job, item=item)
    pipeline = CancellingPipeline(session, object(), object())  # type: ignore[arg-type]

    with pytest.raises(asyncio.CancelledError):
        await pipeline.process(job_id, tenant_id="tenant-a")

    assert session.rollbacks == 1
    assert job.status == "cancelled"
    assert job.error_message == "Worker cancelled the job before completion"
    assert job.completed_at is not None
    assert item.status == "failed"


@pytest.mark.asyncio
async def test_base_pipeline_preserves_image_byte_hash_for_worker_dedupe() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="board.png",
        source_type="image",
        status="processing",
        content_hash="a" * 64,
        metadata_={
            "upload_artifact": {
                "source": "user_upload",
                "filename": "board.png",
                "media_type": "image/png",
                "extension": ".png",
                "storage_path": "/tmp/board.png",
            }
        },
        tags=[],
        categories=[],
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="image",
        tenant_id="tenant-a",
        status="queued",
        progress=0,
    )
    session = FakePipelineSession(job=job, item=item)
    pipeline = ImageHashPipeline(session, FakeEmbedder(), FakeLlm())  # type: ignore[arg-type]

    result = await pipeline.process(job_id, tenant_id="tenant-a")

    assert result == item_id
    assert item.status == "ready"
    assert item.raw_content == "Diagram of the control plane"
    assert item.content_hash == "a" * 64
    assert any("items.id !=" in statement for statement in session.scalar_statements)
    assert item.metadata_["upload_artifact"]["storage_path"] == "/tmp/board.png"
    assert item.metadata_["image_analysis"]["byte_hash"] == "a" * 64
    assert job.status == "completed"
    assert session.embeddings


def test_stable_merge_tags_preserves_order_and_normalizes_values() -> None:
    assert stable_merge_tags([" Research ", "manual"], ["AI", "research", ""], ["Ops"]) == [
        "research",
        "manual",
        "ai",
        "ops",
    ]


@pytest.mark.asyncio
async def test_base_pipeline_preserves_source_subscription_tags_after_media_completion() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="New upload",
        source_type="media",
        source_url="https://www.youtube.com/watch?v=video-123",
        status="processing",
        tags=["research"],
        categories=[],
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="media",
        tenant_id="tenant-a",
        status="queued",
        progress=0,
    )
    session = FakePipelineSession(job=job, item=item)
    pipeline = TextPipeline(session, FakeEmbedder(), TaxonomyFakeLlm())  # type: ignore[arg-type]

    result = await pipeline.process(job_id, tenant_id="tenant-a")

    assert result == item_id
    assert item.status == "ready"
    assert item.tags == ["research", "manual", "ai"]
    assert item.categories == ["technology"]


@pytest.mark.asyncio
async def test_backfill_missing_taxonomy_dry_run_reports_without_mutation(monkeypatch) -> None:
    item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        title="Untyped media",
        source_type="media",
        status="ready",
        raw_content="Transcript about robotics.",
        tags=[],
        categories=[],
    )
    session = FakeBackfillSession([item])
    monkeypatch.setattr("app.workers.tasks.async_session", SessionFactory(session))
    redis = FakeRedis()

    report = await backfill_missing_taxonomy(
        {"llm": TaxonomyFakeLlm(), "redis": redis},
        tenant_id="tenant-a",
        dry_run=True,
    )

    assert report["candidate_count"] == 1
    assert report["changed_count"] == 1
    assert report["skipped_count"] == 0
    assert report["failure_count"] == 0
    assert item.tags == []
    assert item.categories == []
    assert session.commits == 0
    assert redis.enqueued == []


@pytest.mark.asyncio
async def test_backfill_missing_taxonomy_updates_existing_items_and_schedules_palace(monkeypatch) -> None:
    item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        title="Untyped webpage",
        source_type="webpage",
        status="ready",
        raw_content="Article about robotics.",
        tags=["source"],
        categories=[],
        metadata_={"capture_origin": "browser"},
    )
    session = FakeBackfillSession([item])
    monkeypatch.setattr("app.workers.tasks.async_session", SessionFactory(session))
    redis = FakeRedis()

    report = await backfill_missing_taxonomy(
        {"llm": TaxonomyFakeLlm(), "redis": redis},
        tenant_id="tenant-a",
        dry_run=False,
    )

    assert report["candidate_count"] == 1
    assert report["changed_count"] == 1
    assert report["failure_count"] == 0
    assert item.tags == ["source"]
    assert item.categories == ["technology"]
    assert item.metadata_["taxonomy_backfill"]["source"] == "backfill_missing_taxonomy"
    assert item.metadata_["taxonomy_backfill"]["raw_content_chars_used"] == len("Article about robotics.")
    assert session.commits == 1
    assert redis.enqueued == [
        (
            "mark_items_dirty_and_schedule",
            {
                "_queue_name": PALACE_WORKER_QUEUE,
                "item_ids": [str(item.id)],
                "tenant_id": "tenant-a",
                "reason": "taxonomy-backfill",
            },
        )
    ]


@pytest.mark.asyncio
async def test_backfill_missing_taxonomy_repairs_memory_note_categories_with_safe_report(monkeypatch) -> None:
    item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        title="Scoped Codex memory",
        source_type="note",
        status="ready",
        raw_content="Private memory body about taxonomy routing.",
        tags=["codex-memory"],
        categories=[],
        metadata_={
            "memory_entry": {"scope": {"type": "agent", "key": "codex"}},
            "test_job_type": "memory_artifact",
        },
    )
    session = FakeBackfillSession([item])
    monkeypatch.setattr("app.workers.tasks.async_session", SessionFactory(session))
    redis = FakeRedis()

    report = await backfill_missing_taxonomy(
        {"llm": TaxonomyFakeLlm(), "redis": redis},
        tenant_id="tenant-a",
        dry_run=False,
        source_types=["note"],
    )

    assert report["source_types"] == ["note"]
    assert report["candidate_count"] == 1
    assert report["changed_count"] == 1
    assert report["candidate_breakdown"] == {
        "source_type": {"note": 1},
        "source_type_job_type": {"note": {"memory_artifact": 1}},
    }
    assert report["changed_breakdown"] == {
        "source_type": {"note": 1},
        "source_type_job_type": {"note": {"memory_artifact": 1}},
    }
    assert report["samples"] == [
        {
            "item_id": str(item.id),
            "title": "Scoped Codex memory",
        }
    ]
    assert "Private memory body" not in str(report)
    assert item.tags == ["codex-memory"]
    assert item.categories == ["technology"]
    assert item.raw_content == "Private memory body about taxonomy routing."
    assert item.metadata_["memory_entry"]["scope"] == {"type": "agent", "key": "codex"}
    assert item.metadata_["taxonomy_backfill"]["previous_tag_count"] == 1
    assert item.metadata_["taxonomy_backfill"]["previous_category_count"] == 0
    assert session.commits == 1
    assert redis.enqueued == [
        (
            "mark_items_dirty_and_schedule",
            {
                "_queue_name": PALACE_WORKER_QUEUE,
                "item_ids": [str(item.id)],
                "tenant_id": "tenant-a",
                "reason": "taxonomy-backfill",
            },
        )
    ]


@pytest.mark.asyncio
async def test_backfill_missing_taxonomy_fills_only_missing_tags(monkeypatch) -> None:
    item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        title="Tagged note",
        source_type="note",
        status="ready",
        raw_content="Note with a category but no tags.",
        tags=[],
        categories=["operator-memory"],
        metadata_={"test_job_type": "note"},
    )
    skipped_media = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        title="Skipped media",
        source_type="media",
        status="ready",
        raw_content="Media should be filtered from this note-only run.",
        tags=[],
        categories=[],
    )
    session = FakeBackfillSession([item, skipped_media])
    monkeypatch.setattr("app.workers.tasks.async_session", SessionFactory(session))

    report = await backfill_missing_taxonomy(
        {"llm": TaxonomyFakeLlm(), "redis": FakeRedis()},
        tenant_id="tenant-a",
        dry_run=False,
        source_types=["note"],
    )

    assert session.params[0]["source_types"] == ["note"]
    assert report["candidate_count"] == 1
    assert item.tags == ["ai", "research"]
    assert item.categories == ["operator-memory"]
    assert skipped_media.tags == []
    assert skipped_media.categories == []


@pytest.mark.asyncio
async def test_base_pipeline_marks_media_timeout_job_terminal() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Slow media",
        source_type="media",
        status="processing",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="media",
        tenant_id="tenant-a",
        status="queued",
        progress=0,
    )
    session = FakePipelineSession(job=job, item=item)

    class TimeoutPipeline(BasePipeline):
        async def extract(self, **_kwargs):
            raise TimeoutError("OpenAI transcription timed out after 2s for chunk 1/4")

    pipeline = TimeoutPipeline(session, object(), object())  # type: ignore[arg-type]

    with pytest.raises(TimeoutError):
        await pipeline.process(job_id, tenant_id="tenant-a")

    assert session.rollbacks == 1
    assert job.status == "failed"
    assert job.error_message == "OpenAI transcription timed out after 2s for chunk 1/4"
    assert job.completed_at is not None
    assert item.status == "failed"


@pytest.mark.asyncio
async def test_base_pipeline_records_pending_availability_without_hard_failure() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Premiere media",
        source_type="media",
        status="processing",
        metadata_={},
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="media",
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        payload={"retry_task": {"name": "process_media", "kwargs": {"url": "https://youtube.com/watch?v=ZL0FdHH7wwc"}}},
    )
    session = FakePipelineSession(job=job, item=item)
    pipeline = PendingAvailabilityPipeline(session, object(), object())  # type: ignore[arg-type]

    with pytest.raises(MediaPendingAvailabilityError):
        await pipeline.process(job_id, tenant_id="tenant-a")

    assert session.rollbacks == 1
    assert job.status == "pending_availability"
    assert job.progress == 10
    assert "scheduled but not available yet" in job.error_message
    assert job.completed_at is None
    assert item.status == "processing"
    pending = job.payload["pending_availability"]
    assert pending["provider"] == "youtube"
    assert pending["provider_message"] == "ERROR: [youtube] ZL0FdHH7wwc: Premieres in 2 hours"
    assert pending["retryable"] is True
    assert pending["retry_after_seconds"] == 7200
    assert "retry_after_at" in pending
    assert item.metadata_["pending_availability"] == pending
    assert session.progress_events[-1].phase == "pending_availability"
    assert session.progress_events[-1].status == "queued"
    assert session.progress_events[-1].metadata_["retry_after_seconds"] == 7200


def test_split_audio_with_ffmpeg_raises_clear_timeout(monkeypatch, tmp_path: Path) -> None:
    from app.pipelines.youtube import _split_audio_with_ffmpeg

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=3)

    monkeypatch.setattr("app.pipelines.youtube.subprocess.run", fake_run)
    monkeypatch.setattr("app.pipelines.youtube.settings.media_ffmpeg_timeout_seconds", 3)

    with pytest.raises(TimeoutError, match="ffmpeg timed out after 3s"):
        _split_audio_with_ffmpeg(
            input_path=str(tmp_path / "in.m4a"),
            output_path=str(tmp_path / "out.m4a"),
            start_seconds=0.0,
            duration_seconds=30.0,
        )


def test_assemblyai_utterances_normalize_to_speaker_segments() -> None:
    transcript, segments = _render_transcript(
        {
            "provider": "assemblyai",
            "text": "Opening line Follow-up",
            "utterances": [
                {"speaker": "a", "start": 0, "end": 2500, "text": "Opening line"},
                {"speaker": "b", "start": 3200, "end": 4800, "text": "Follow-up"},
            ],
        },
        offset_seconds=600.0,
    )

    assert transcript == "[00:10:00] A: Opening line\n[00:10:03] B: Follow-up"
    assert segments == [
        {"speaker": "A", "start_seconds": 600.0, "end_seconds": 602.5, "text": "Opening line"},
        {"speaker": "B", "start_seconds": 603.2, "end_seconds": 604.8, "text": "Follow-up"},
    ]


def test_assemblyai_words_normalize_without_utterances() -> None:
    transcript, segments = _render_transcript(
        {
            "provider": "assemblyai",
            "text": "Hello there General Kenobi",
            "words": [
                {"speaker": "speaker_a", "start": 0, "end": 100, "text": "Hello"},
                {"speaker": "speaker_a", "start": 120, "end": 240, "text": "there"},
                {"speaker": "speaker_b", "start": 500, "end": 700, "text": "General"},
                {"speaker": "speaker_b", "start": 720, "end": 900, "text": "Kenobi"},
            ],
        },
        offset_seconds=2.5,
    )

    assert transcript == "[00:00:02] Speaker a: Hello there\n[00:00:03] Speaker b: General Kenobi"
    assert segments == [
        {"speaker": "Speaker a", "start_seconds": 2.5, "end_seconds": 2.74, "text": "Hello there"},
        {"speaker": "Speaker b", "start_seconds": 3.0, "end_seconds": 3.4, "text": "General Kenobi"},
    ]


def test_transcription_normalization_returns_plain_text_when_no_segments() -> None:
    transcript, segments = _render_transcript({"provider": "assemblyai", "text": "Plain transcript"}, offset_seconds=5.0)

    assert transcript == "Plain transcript"
    assert segments == []


@pytest.mark.asyncio
async def test_transcribe_retries_transient_openai_failures(monkeypatch, tmp_path: Path) -> None:
    audio_path = tmp_path / "chunk.m4a"
    audio_path.write_bytes(b"audio")
    sleeps: list[float] = []
    calls = 0

    class FakeTranscriptions:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("HTTP 500 from transcription API")
            return {"text": "Recovered transcript"}

    class FakeAudio:
        def __init__(self) -> None:
            self.transcriptions = FakeTranscriptions()

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            assert kwargs["timeout"] == 4
            self.audio = FakeAudio()

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.pipelines.youtube.AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr("app.pipelines.youtube._is_transient_transcription_error", lambda _exc: True)
    monkeypatch.setattr("app.pipelines.youtube.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_provider", "openai")
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_request_timeout_seconds", 4)
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_transient_retries", 2)
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_retry_backoff_seconds", 1.5)

    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = FakePipelineSession(
        job=Job(
            id=job_id,
            item_id=item_id,
            job_type="media",
            tenant_id="tenant-a",
            status="processing",
            progress=0,
        ),
        item=Item(
            id=item_id,
            tenant_id="tenant-a",
            title="Retry media",
            source_type="media",
            status="processing",
        ),
    )
    pipeline = MediaPipeline(session, None, None)  # type: ignore[arg-type]
    text, segments = await pipeline._transcribe(
        str(audio_path),
        chunk_index=2,
        chunk_count=4,
        duration_seconds=45.0,
        job_id=str(job_id),
    )

    assert text == "Recovered transcript"
    assert segments == []
    assert calls == 2
    assert sleeps == [1.5]
    assert [event.message for event in session.progress_events] == [
        "OpenAI transcription attempt 1 for chunk 2/4",
        "Retrying chunk 2/4 after transient OpenAI failure",
        "OpenAI transcription attempt 2 for chunk 2/4",
    ]
    assert session.progress_events[1].metadata_["attempt"] == 1
    assert session.progress_events[1].metadata_["retry_backoff_seconds"] == 1.5
    assert session.progress_events[1].metadata_["error_class"] == "RuntimeError"


@pytest.mark.asyncio
async def test_transcribe_raises_clear_per_chunk_timeout(monkeypatch, tmp_path: Path) -> None:
    audio_path = tmp_path / "chunk.m4a"
    audio_path.write_bytes(b"audio")
    pending_call_started = asyncio.Event()

    class FakeTranscriptions:
        async def create(self, **_kwargs):
            pending_call_started.set()
            await asyncio.sleep(10)
            return {"text": "too late"}

    class FakeAudio:
        def __init__(self) -> None:
            self.transcriptions = FakeTranscriptions()

    class FakeOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.audio = FakeAudio()

    monkeypatch.setattr("app.pipelines.youtube.AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_provider", "openai")
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_request_timeout_seconds", 0.01)
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_transient_retries", 0)

    pipeline = MediaPipeline(None, None, None)  # type: ignore[arg-type]
    with pytest.raises(TimeoutError, match=r"timed out after 0.01s for chunk 2/5 .*600.0s media"):
        await pipeline._transcribe(
            str(audio_path),
            chunk_index=2,
            chunk_count=5,
            duration_seconds=600.0,
        )

    assert pending_call_started.is_set()


@pytest.mark.asyncio
async def test_transcribe_with_assemblyai_uploads_polls_and_normalizes(monkeypatch, tmp_path: Path) -> None:
    audio_path = tmp_path / "chunk.m4a"
    audio_path.write_bytes(b"audio")
    requests: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v2/upload":
            return httpx.Response(200, json={"upload_url": "https://cdn.assemblyai.test/audio"})
        if request.url.path == "/v2/transcript" and request.method == "POST":
            payload = request.read().decode()
            assert "speaker_labels" in payload
            return httpx.Response(200, json={"id": "transcript-123"})
        if request.url.path == "/v2/transcript/transcript-123":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "text": "Assembly transcript",
                    "utterances": [{"speaker": "a", "start": 1000, "end": 2500, "text": "Assembly transcript"}],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["base_url"] == "https://api.assemblyai.test"
            assert kwargs["headers"]["authorization"] == "assembly-key"
            self._client = real_async_client(transport=httpx.MockTransport(handler), base_url=kwargs["base_url"])

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, exc_type, exc, tb):
            await self._client.aclose()
            return False

    monkeypatch.setattr("app.pipelines.youtube.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_provider", "assemblyai")
    monkeypatch.setattr("app.pipelines.youtube.settings.assemblyai_api_key", "assembly-key")
    monkeypatch.setattr("app.pipelines.youtube.settings.assemblyai_base_url", "https://api.assemblyai.test")
    monkeypatch.setattr("app.pipelines.youtube.settings.assemblyai_speech_model", "universal-2")

    pipeline = MediaPipeline(None, None, None)  # type: ignore[arg-type]
    text, segments = await pipeline._transcribe(str(audio_path), offset_seconds=10.0)

    assert text == "[00:00:11] A: Assembly transcript"
    assert segments == [{"speaker": "A", "start_seconds": 11.0, "end_seconds": 12.5, "text": "Assembly transcript"}]
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/v2/upload"),
        ("POST", "/v2/transcript"),
        ("GET", "/v2/transcript/transcript-123"),
    ]


@pytest.mark.asyncio
async def test_transcribe_falls_back_to_openai_when_assemblyai_key_missing(monkeypatch, tmp_path: Path) -> None:
    audio_path = tmp_path / "chunk.m4a"
    audio_path.write_bytes(b"audio")
    calls = 0

    class FakeTranscriptions:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            return {"text": "Fallback transcript"}

    class FakeAudio:
        def __init__(self) -> None:
            self.transcriptions = FakeTranscriptions()

    class FakeOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.audio = FakeAudio()

    monkeypatch.setattr("app.pipelines.youtube.AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr("app.pipelines.youtube.settings.transcription_provider", "assemblyai")
    monkeypatch.setattr("app.pipelines.youtube.settings.assemblyai_api_key", "")

    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = FakePipelineSession(
        job=Job(
            id=job_id,
            item_id=item_id,
            job_type="media",
            tenant_id="tenant-a",
            status="processing",
            progress=0,
        ),
        item=Item(
            id=item_id,
            tenant_id="tenant-a",
            title="Fallback media",
            source_type="media",
            status="processing",
        ),
    )
    pipeline = MediaPipeline(session, None, None)  # type: ignore[arg-type]
    text, segments = await pipeline._transcribe(str(audio_path), job_id=str(job_id))

    assert text == "Fallback transcript"
    assert segments == []
    assert calls == 1
    assert [event.message for event in session.progress_events] == [
        "AssemblyAI transcription attempt 1 for chunk 1/1",
        "AssemblyAI unavailable; falling back to OpenAI for chunk 1/1",
    ]
    assert session.progress_events[1].metadata_["transcription_provider"] == "assemblyai"
    assert session.progress_events[1].metadata_["fallback_transcription_provider"] == "openai"
