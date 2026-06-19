import asyncio
import glob
import logging
import math
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import yt_dlp
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.config import settings
from app.models.job import Job
from app.pipelines.base import BasePipeline, PendingAvailabilityError

_URL_RE = re.compile(r'https?://[^\s\)\]>\"\']+')


def _extract_description_links(description: str) -> list[str]:
    """Return deduplicated list of URLs found in a video description."""
    return list(dict.fromkeys(_URL_RE.findall(description)))


logger = logging.getLogger(__name__)

_TEMP_DIR = "/tmp/palaceoftruth"
_MIN_CHUNK_SECONDS = 30


@dataclass(frozen=True)
class TranscriptionChunk:
    path: str
    start_seconds: float
    duration_seconds: float
    size_bytes: int | None = None
    cleanup: bool = False


class MediaTranscriptionLimitError(ValueError):
    """Raised when a media source exceeds the configured transcription window."""


class MediaPendingAvailabilityError(PendingAvailabilityError):
    provider = "youtube"
    status_code = "pending_availability"
    fallback_retry_after_seconds = 3600


class TranscriptionProviderUnavailable(RuntimeError):
    """Raised when an optional transcription provider is not configured."""


_PREMIERE_UNAVAILABLE_RE = re.compile(
    r"\b(?:premieres?|premiere|scheduled|live)\b.*?\bin\s+"
    r"(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>second|seconds|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs|day|days)\b",
    re.IGNORECASE,
)


def _parse_availability_delay_seconds(message: str) -> int | None:
    match = _PREMIERE_UNAVAILABLE_RE.search(message)
    if match is None:
        return None
    amount = float(match.group("amount"))
    unit = match.group("unit").lower()
    multiplier = 1
    if unit.startswith("min"):
        multiplier = 60
    elif unit.startswith(("hour", "hr")):
        multiplier = 60 * 60
    elif unit.startswith("day"):
        multiplier = 24 * 60 * 60
    return max(60, math.ceil(amount * multiplier))


def _pending_youtube_availability_error(exc: BaseException) -> MediaPendingAvailabilityError | None:
    message = str(exc)
    if "youtube" not in message.lower():
        return None
    retry_after_seconds = _parse_availability_delay_seconds(message)
    if retry_after_seconds is None:
        return None
    return MediaPendingAvailabilityError(
        message[:500],
        retry_after_seconds=retry_after_seconds,
        user_message=(
            "YouTube says this video is scheduled but not available yet; "
            "Palace will retry after the premiere window."
        ),
    )


def _build_media_metadata(info: dict[str, Any]) -> dict[str, Any]:
    description = info.get("description") or ""
    channel = info.get("uploader") or info.get("channel")
    duration = info.get("duration")
    return {
        "title": info.get("title", ""),
        # Legacy fields kept for backward compatibility
        "duration": duration,
        "channel": channel,
        "upload_date": info.get("upload_date"),
        # Enriched native metadata (R14)
        "duration_seconds": duration,
        "channel_name": channel,
        "author": channel,
        "video_id": info.get("id"),
        "published_at": info.get("upload_date"),  # YYYYMMDD string
        "view_count": info.get("view_count"),
        "description": description,
        "description_links": _extract_description_links(description),
    }


def _format_duration_seconds(raw_duration: Any) -> str:
    if isinstance(raw_duration, int):
        return f"{raw_duration}s"
    if isinstance(raw_duration, float):
        return f"{raw_duration:.1f}s"
    return str(raw_duration)


def _format_timestamp(seconds: float) -> str:
    whole_seconds = max(0, int(seconds))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _supports_diarization() -> bool:
    return settings.whisper_model == "gpt-4o-transcribe-diarize"


def _response_to_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return {"text": str(response)}


def _milliseconds_to_seconds(value: Any) -> float | None:
    try:
        return float(value) / 1000
    except (TypeError, ValueError):
        return None


def _normalize_speaker_label(raw_label: Any) -> str:
    if isinstance(raw_label, str) and raw_label.strip():
        label = raw_label.strip().replace("_", " ")
        return label[:1].upper() + label[1:]
    if isinstance(raw_label, int):
        return f"Speaker {raw_label}"
    return "Speaker unknown"


def _extract_openai_speaker_segments(response_dict: dict[str, Any], *, offset_seconds: float) -> list[dict[str, Any]]:
    raw_segments = response_dict.get("segments")
    if not isinstance(raw_segments, list):
        return []

    segments: list[dict[str, Any]] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            continue
        text = raw_segment.get("text") or raw_segment.get("transcript")
        if not isinstance(text, str) or not text.strip():
            continue
        start = raw_segment.get("start")
        end = raw_segment.get("end")
        try:
            start_seconds = float(start) + offset_seconds if start is not None else offset_seconds
        except (TypeError, ValueError):
            start_seconds = offset_seconds
        try:
            end_seconds = float(end) + offset_seconds if end is not None else start_seconds
        except (TypeError, ValueError):
            end_seconds = start_seconds
        speaker = _normalize_speaker_label(
            raw_segment.get("speaker")
            or raw_segment.get("speaker_label")
            or raw_segment.get("speaker_id")
            or raw_segment.get("speaker_name")
        )
        segments.append(
            {
                "speaker": speaker,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "text": text.strip(),
            }
        )
    return segments


def _extract_assemblyai_utterance_segments(
    response_dict: dict[str, Any],
    *,
    offset_seconds: float,
) -> list[dict[str, Any]]:
    raw_utterances = response_dict.get("utterances")
    if not isinstance(raw_utterances, list):
        return []

    segments: list[dict[str, Any]] = []
    for raw_utterance in raw_utterances:
        if not isinstance(raw_utterance, dict):
            continue
        text = raw_utterance.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        start_seconds = _milliseconds_to_seconds(raw_utterance.get("start"))
        end_seconds = _milliseconds_to_seconds(raw_utterance.get("end"))
        if start_seconds is None:
            start_seconds = 0.0
        if end_seconds is None:
            end_seconds = start_seconds
        segments.append(
            {
                "speaker": _normalize_speaker_label(raw_utterance.get("speaker")),
                "start_seconds": start_seconds + offset_seconds,
                "end_seconds": end_seconds + offset_seconds,
                "text": text.strip(),
            }
        )
    return segments


def _extract_assemblyai_word_segments(
    response_dict: dict[str, Any],
    *,
    offset_seconds: float,
) -> list[dict[str, Any]]:
    raw_words = response_dict.get("words")
    if not isinstance(raw_words, list):
        return []

    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_words: list[str] = []
    for raw_word in raw_words:
        if not isinstance(raw_word, dict):
            continue
        text = raw_word.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        speaker = _normalize_speaker_label(raw_word.get("speaker"))
        start_seconds = _milliseconds_to_seconds(raw_word.get("start"))
        end_seconds = _milliseconds_to_seconds(raw_word.get("end"))
        if start_seconds is None:
            start_seconds = 0.0
        if end_seconds is None:
            end_seconds = start_seconds

        if current is None or current["speaker"] != speaker:
            if current is not None and current_words:
                current["text"] = " ".join(current_words).strip()
                segments.append(current)
            current = {
                "speaker": speaker,
                "start_seconds": start_seconds + offset_seconds,
                "end_seconds": end_seconds + offset_seconds,
                "text": "",
            }
            current_words = []
        current["end_seconds"] = end_seconds + offset_seconds
        current_words.append(text.strip())

    if current is not None and current_words:
        current["text"] = " ".join(current_words).strip()
        segments.append(current)
    return segments


def _extract_speaker_segments(response_dict: dict[str, Any], *, offset_seconds: float) -> list[dict[str, Any]]:
    provider = str(response_dict.get("provider") or "").lower()
    if provider == "assemblyai":
        utterance_segments = _extract_assemblyai_utterance_segments(response_dict, offset_seconds=offset_seconds)
        if utterance_segments:
            return utterance_segments
        return _extract_assemblyai_word_segments(response_dict, offset_seconds=offset_seconds)
    return _extract_openai_speaker_segments(response_dict, offset_seconds=offset_seconds)


def _render_transcript(response_dict: dict[str, Any], *, offset_seconds: float) -> tuple[str, list[dict[str, Any]]]:
    diarized_segments = _extract_speaker_segments(response_dict, offset_seconds=offset_seconds)
    if diarized_segments:
        lines = [
            f"[{_format_timestamp(segment['start_seconds'])}] {segment['speaker']}: {segment['text']}"
            for segment in diarized_segments
        ]
        return "\n".join(lines), diarized_segments

    text = response_dict.get("text")
    if isinstance(text, str):
        return text.strip(), []
    return str(text).strip(), []


def _estimate_chunk_seconds(*, duration_seconds: float, file_size_bytes: int) -> int:
    if file_size_bytes <= 0 or file_size_bytes <= settings.transcription_max_upload_bytes:
        return max(_MIN_CHUNK_SECONDS, math.ceil(duration_seconds))

    size_ratio = settings.transcription_max_upload_bytes / file_size_bytes
    estimated = math.floor(duration_seconds * size_ratio * 0.9)
    return max(_MIN_CHUNK_SECONDS, estimated)


def _configured_max_chunk_seconds() -> int:
    return max(_MIN_CHUNK_SECONDS, int(settings.transcription_max_chunk_seconds))


def _split_audio_with_ffmpeg(*, input_path: str, output_path: str, start_seconds: float, duration_seconds: float) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-i",
        input_path,
        "-vn",
        "-c:a",
        "aac",
        output_path,
    ]
    try:
        subprocess.run(
            command,
            check=True,
            timeout=settings.media_ffmpeg_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"ffmpeg timed out after {settings.media_ffmpeg_timeout_seconds}s while preparing media transcription"
        ) from exc


def _probe_audio_duration_seconds(audio_path: str) -> float | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=settings.media_ffmpeg_timeout_seconds,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not probe extracted media duration for %s: %s", audio_path, exc)
        return None

    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def _is_transient_transcription_error(exc: BaseException) -> bool:
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return True
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError, httpx.TimeoutException)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _cleanup_media_temp_files(*, job_id: str, audio_path: str | None, chunks: list[TranscriptionChunk]) -> None:
    paths = {chunk.path for chunk in chunks if chunk.cleanup}
    if audio_path:
        paths.add(audio_path)
    for candidate in glob.glob(f"{_TEMP_DIR}/{job_id}.*"):
        paths.add(candidate)
    for candidate in glob.glob(f"{_TEMP_DIR}/{job_id}-chunk-*"):
        paths.add(candidate)

    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("Could not delete temp media file: %s", path)


class OpenAITranscriptionProvider:
    name = "openai"
    display_name = "OpenAI"

    async def transcribe(self, audio_path: str) -> dict[str, Any]:
        client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.transcription_request_timeout_seconds)
        with open(audio_path, "rb") as f:
            transcript = await asyncio.wait_for(
                client.audio.transcriptions.create(
                    model=settings.whisper_model,
                    file=f,
                    response_format="diarized_json" if _supports_diarization() else "text",
                    chunking_strategy="auto",
                ),
                timeout=settings.transcription_request_timeout_seconds,
            )
        response_dict = _response_to_dict(transcript)
        response_dict.setdefault("provider", self.name)
        return response_dict


class AssemblyAITranscriptionProvider:
    name = "assemblyai"
    display_name = "AssemblyAI"

    async def transcribe(self, audio_path: str) -> dict[str, Any]:
        if not settings.assemblyai_api_key:
            raise TranscriptionProviderUnavailable("ASSEMBLYAI_API_KEY is required for AssemblyAI transcription")

        timeout = httpx.Timeout(settings.transcription_request_timeout_seconds)
        headers = {"authorization": settings.assemblyai_api_key}
        async with httpx.AsyncClient(
            base_url=settings.assemblyai_base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        ) as client:
            with open(audio_path, "rb") as f:
                upload_response = await client.post("/v2/upload", content=f.read())
            upload_response.raise_for_status()
            upload_url = upload_response.json().get("upload_url")
            if not isinstance(upload_url, str) or not upload_url:
                raise RuntimeError("AssemblyAI upload response did not include upload_url")

            transcript_response = await client.post(
                "/v2/transcript",
                json={
                    "audio_url": upload_url,
                    "speaker_labels": True,
                    "speech_model": settings.assemblyai_speech_model,
                },
            )
            transcript_response.raise_for_status()
            transcript_id = transcript_response.json().get("id")
            if not isinstance(transcript_id, str) or not transcript_id:
                raise RuntimeError("AssemblyAI transcript response did not include id")

            while True:
                poll_response = await client.get(f"/v2/transcript/{transcript_id}")
                poll_response.raise_for_status()
                payload = poll_response.json()
                status = payload.get("status")
                if status == "completed":
                    payload["provider"] = self.name
                    return payload
                if status == "error":
                    error = payload.get("error") or "unknown AssemblyAI transcription error"
                    raise RuntimeError(f"AssemblyAI transcription failed: {error}")
                await asyncio.sleep(max(0.1, settings.assemblyai_poll_interval_seconds))


def _configured_transcription_provider() -> OpenAITranscriptionProvider | AssemblyAITranscriptionProvider:
    provider = settings.transcription_provider.strip().lower()
    if provider in {"", "openai"}:
        return OpenAITranscriptionProvider()
    if provider == "assemblyai":
        return AssemblyAITranscriptionProvider()
    raise ValueError(f"Unsupported transcription provider: {settings.transcription_provider}")


def _configured_parallel_transcription_chunks() -> int:
    return max(1, int(settings.transcription_max_parallel_chunks))


class MediaPipeline(BasePipeline):
    """Download audio/video via yt-dlp, transcribe via OpenAI Whisper.

    Accepts any URL supported by yt-dlp: YouTube, podcasts, Vimeo, etc.
    """

    async def extract(self, url: str, job_id: str = "unknown") -> tuple[str, dict[str, Any]]:
        loop = asyncio.get_event_loop()
        audio_path: str | None = None
        transcription_inputs: list[TranscriptionChunk] = []

        try:
            await self._set_extract_progress(job_id, 12, "download", message="Downloading media")
            audio_path, meta = await asyncio.wait_for(
                loop.run_in_executor(None, self._download_audio, url, str(job_id)),
                timeout=settings.media_download_timeout_seconds,
            )
            audio_size_bytes = self._file_size_bytes(audio_path)
            await self._set_extract_progress(
                job_id,
                14,
                "download",
                event_status="completed",
                message="Media download complete",
                metadata={
                    "duration_seconds": meta.get("duration_seconds"),
                    "audio_size_bytes": audio_size_bytes,
                    "media_kind": "audio",
                },
            )
            await self._set_extract_progress(job_id, 15, "chunk", message="Preparing transcription chunks")
            transcription_inputs = await loop.run_in_executor(
                None,
                self._prepare_transcription_inputs,
                audio_path,
                meta,
                str(job_id),
            )
            total_transcription_inputs = len(transcription_inputs)
            await self._set_extract_progress(
                job_id,
                16,
                "chunk",
                event_status="completed",
                message=f"Prepared {total_transcription_inputs} transcription chunk(s)",
                metadata={
                    "chunk_count": total_transcription_inputs,
                    "chunks": [
                        {
                            "chunk_index": index,
                            "chunk_count": total_transcription_inputs,
                            "start_seconds": chunk.start_seconds,
                            "duration_seconds": chunk.duration_seconds,
                            "size_bytes": (
                                chunk.size_bytes if chunk.size_bytes is not None else self._file_size_bytes(chunk.path)
                            ),
                        }
                        for index, chunk in enumerate(transcription_inputs, start=1)
                    ],
                },
            )

            transcribed_chunks = await self._transcribe_chunks(transcription_inputs, job_id=job_id)
            transcript_parts = [part for part, _segments in transcribed_chunks if part]
            diarized_segments = [
                segment for _part, chunk_segments in transcribed_chunks for segment in chunk_segments
            ]

            transcript = "\n".join(part for part in transcript_parts if part).strip()
            if not transcript:
                raise ValueError(f"Whisper returned empty transcript for {url}")

            description = meta.get("description", "")
            if description:
                transcript = f"{transcript}\n\n---\n\nVideo Description:\n{description}"

            if diarized_segments:
                meta["speaker_segments"] = diarized_segments
            if len(transcription_inputs) > 1:
                meta["transcription_chunk_count"] = len(transcription_inputs)
                meta["transcription_chunks"] = [
                    {
                        "start_seconds": transcription_input.start_seconds,
                        "duration_seconds": transcription_input.duration_seconds,
                    }
                    for transcription_input in transcription_inputs
                ]

            return transcript, meta
        finally:
            _cleanup_media_temp_files(job_id=str(job_id), audio_path=audio_path, chunks=transcription_inputs)

    async def _set_extract_progress(
        self,
        job_id: str,
        progress: int,
        stage: str,
        *,
        event_status: str = "processing",
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            try:
                job_key = uuid.UUID(job_id)
            except ValueError:
                job_key = job_id
            job = await self.db.get(Job, job_key)
            if job and job.status == "processing":
                await self._update_job(
                    job,
                    phase=stage,
                    progress=progress,
                    event_status=event_status,
                    event_message=message,
                    event_metadata=metadata,
                )
                logger.info(
                    "media extract job %s stage=%s status=%s progress=%d metadata=%s",
                    job_id,
                    stage,
                    event_status,
                    progress,
                    self._log_safe_metadata(metadata),
                )
        except Exception as exc:
            logger.warning("Could not update media extraction progress for job %s: %s", job_id, exc)

    async def _transcribe_chunks(
        self,
        transcription_inputs: list[TranscriptionChunk],
        *,
        job_id: str,
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        chunk_count = len(transcription_inputs)
        if chunk_count == 0:
            return []

        parallel_limit = min(chunk_count, _configured_parallel_transcription_chunks())
        semaphore = asyncio.Semaphore(parallel_limit)
        results: list[tuple[str, list[dict[str, Any]]] | None] = [None] * chunk_count

        async def transcribe_one(chunk_index: int, transcription_input: TranscriptionChunk) -> None:
            async with semaphore:
                await self._set_extract_progress(
                    job_id,
                    self._transcribe_progress(chunk_index, chunk_count),
                    "transcribe",
                    message=f"Transcribing chunk {chunk_index}/{chunk_count}",
                    metadata={
                        **self._chunk_progress_metadata(
                            transcription_input,
                            chunk_index=chunk_index,
                            chunk_count=chunk_count,
                        ),
                        "parallel_chunk_limit": parallel_limit,
                    },
                )
                results[chunk_index - 1] = await self._transcribe(
                    transcription_input.path,
                    offset_seconds=transcription_input.start_seconds,
                    chunk_index=chunk_index,
                    chunk_count=chunk_count,
                    duration_seconds=transcription_input.duration_seconds,
                    job_id=job_id,
                )

        tasks = [
            asyncio.create_task(transcribe_one(chunk_index, transcription_input))
            for chunk_index, transcription_input in enumerate(transcription_inputs, start=1)
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            failed = next((task for task in done if task.exception() is not None), None)
            if failed is not None:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                exc = failed.exception()
                if exc is not None:
                    raise exc
            if pending:
                await asyncio.gather(*pending)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        if any(result is None for result in results):
            raise RuntimeError("Media transcription finished without all chunk results")
        return [result for result in results if result is not None]

    @staticmethod
    def _file_size_bytes(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    @staticmethod
    def _transcribe_progress(chunk_index: int, chunk_count: int) -> int:
        if chunk_count <= 1:
            return 16
        return min(35, 16 + math.floor(((chunk_index - 1) / chunk_count) * 18))

    @classmethod
    def _chunk_progress_metadata(
        cls,
        chunk: TranscriptionChunk,
        *,
        chunk_index: int,
        chunk_count: int,
        attempt: int | None = None,
        retry_backoff_seconds: float | None = None,
        error_class: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "duration_seconds": chunk.duration_seconds,
            "size_bytes": chunk.size_bytes if chunk.size_bytes is not None else cls._file_size_bytes(chunk.path),
        }
        if attempt is not None:
            metadata["attempt"] = attempt
        if retry_backoff_seconds is not None:
            metadata["retry_backoff_seconds"] = retry_backoff_seconds
        if error_class is not None:
            metadata["error_class"] = error_class
        return metadata

    @staticmethod
    def _log_safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        if not metadata:
            return None
        return {key: value for key, value in metadata.items() if key not in {"chunks"}}

    @staticmethod
    def _prepare_transcription_inputs(audio_path: str, meta: dict[str, Any], job_id: str) -> list[TranscriptionChunk]:
        os.makedirs(_TEMP_DIR, exist_ok=True)
        duration = meta.get("duration_seconds")
        if not isinstance(duration, (int, float)) or duration <= 0:
            return [
                TranscriptionChunk(
                    path=audio_path,
                    start_seconds=0.0,
                    duration_seconds=0.0,
                    size_bytes=MediaPipeline._file_size_bytes(audio_path),
                )
            ]

        try:
            file_size_bytes = os.path.getsize(audio_path)
        except OSError:
            file_size_bytes = 0

        max_chunk_seconds = min(
            settings.transcription_max_duration_seconds,
            _configured_max_chunk_seconds(),
            _estimate_chunk_seconds(duration_seconds=float(duration), file_size_bytes=file_size_bytes),
        )
        if duration <= max_chunk_seconds and file_size_bytes <= settings.transcription_max_upload_bytes:
            return [
                TranscriptionChunk(
                    path=audio_path,
                    start_seconds=0.0,
                    duration_seconds=float(duration),
                    size_bytes=file_size_bytes,
                )
            ]

        chunks: list[TranscriptionChunk] = []
        chunk_start = 0.0
        chunk_index = 0
        total_duration = float(duration)
        while chunk_start < total_duration:
            current_duration = min(max_chunk_seconds, total_duration - chunk_start)
            if current_duration <= 0:
                break

            chunk_path = f"{_TEMP_DIR}/{job_id}-chunk-{chunk_index}.m4a"
            _split_audio_with_ffmpeg(
                input_path=audio_path,
                output_path=chunk_path,
                start_seconds=chunk_start,
                duration_seconds=current_duration,
            )
            try:
                chunk_size_bytes = os.path.getsize(chunk_path)
            except OSError as exc:
                raise MediaTranscriptionLimitError(f"Failed to read chunked audio for ingest job {job_id}") from exc

            if chunk_size_bytes > settings.transcription_max_upload_bytes:
                scaled_duration = math.floor(
                    current_duration * (settings.transcription_max_upload_bytes / chunk_size_bytes) * 0.9
                )
                if scaled_duration < _MIN_CHUNK_SECONDS:
                    raise MediaTranscriptionLimitError(
                        "Audio chunking could not reduce the upload below the current API size limit "
                        f"for ingest job {job_id}."
                    )
                os.unlink(chunk_path)
                max_chunk_seconds = scaled_duration
                continue

            chunks.append(
                TranscriptionChunk(
                    path=chunk_path,
                    start_seconds=chunk_start,
                    duration_seconds=current_duration,
                    size_bytes=chunk_size_bytes,
                    cleanup=True,
                )
            )
            chunk_start += current_duration
            chunk_index += 1

        return chunks or [
            TranscriptionChunk(
                path=audio_path,
                start_seconds=0.0,
                duration_seconds=total_duration,
                size_bytes=file_size_bytes,
            )
        ]

    @staticmethod
    def _download_audio(url: str, job_id: str) -> tuple[str, dict[str, Any]]:
        os.makedirs(_TEMP_DIR, exist_ok=True)
        output_template = f"{_TEMP_DIR}/{job_id}.%(ext)s"

        ydl_opts: dict[str, Any] = {
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": settings.media_download_timeout_seconds,
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
        }

        meta: dict[str, Any] = {}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    meta = _build_media_metadata(info)
                ydl.download([url])
        except Exception as exc:
            pending_error = _pending_youtube_availability_error(exc)
            if pending_error is not None:
                raise pending_error from exc
            raise

        audio_path = f"{_TEMP_DIR}/{job_id}.m4a"
        if not os.path.exists(audio_path):
            for ext in ("m4a", "mp3", "webm", "ogg", "opus"):
                candidate = f"{_TEMP_DIR}/{job_id}.{ext}"
                if os.path.exists(candidate):
                    audio_path = candidate
                    break

        duration = meta.get("duration_seconds")
        if not isinstance(duration, (int, float)) or duration <= 0:
            probed_duration = _probe_audio_duration_seconds(audio_path)
            if probed_duration is not None:
                meta["duration_seconds"] = probed_duration
                meta["duration"] = probed_duration
                meta["duration_source"] = "ffprobe"

        return audio_path, meta

    async def _transcribe(
        self,
        audio_path: str,
        *,
        offset_seconds: float = 0.0,
        chunk_index: int = 1,
        chunk_count: int = 1,
        duration_seconds: float = 0.0,
        job_id: str | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        primary_provider = _configured_transcription_provider()
        fallback_provider = OpenAITranscriptionProvider() if primary_provider.name != "openai" else None
        response_dict: dict[str, Any] | None = None
        for attempt in range(settings.transcription_transient_retries + 1):
            attempt_number = attempt + 1
            if job_id is not None:
                await self._set_extract_progress(
                    job_id,
                    self._transcribe_progress(chunk_index, chunk_count),
                    "transcribe",
                    message=(
                        f"{primary_provider.display_name} transcription attempt {attempt_number} "
                        f"for chunk {chunk_index}/{chunk_count}"
                    ),
                    metadata={
                        "transcription_provider": primary_provider.name,
                        "chunk_index": chunk_index,
                        "chunk_count": chunk_count,
                        "duration_seconds": duration_seconds,
                        "size_bytes": self._file_size_bytes(audio_path),
                        "attempt": attempt_number,
                    },
                )
            try:
                response_dict = await primary_provider.transcribe(audio_path)
                break
            except asyncio.TimeoutError as exc:
                if job_id is not None:
                    await self._set_extract_progress(
                        job_id,
                        self._transcribe_progress(chunk_index, chunk_count),
                        "transcribe",
                        event_status="failed",
                        message=f"{primary_provider.display_name} transcription timed out for chunk {chunk_index}/{chunk_count}",
                        metadata={
                            "transcription_provider": primary_provider.name,
                            "chunk_index": chunk_index,
                            "chunk_count": chunk_count,
                            "duration_seconds": duration_seconds,
                            "size_bytes": self._file_size_bytes(audio_path),
                            "attempt": attempt_number,
                            "error_class": exc.__class__.__name__,
                        },
                    )
                raise TimeoutError(
                    f"{primary_provider.display_name} transcription timed out after "
                    f"{settings.transcription_request_timeout_seconds}s for chunk {chunk_index}/{chunk_count} "
                    f"({duration_seconds:.1f}s media) at {audio_path}"
                ) from exc
            except asyncio.CancelledError:
                raise
            except TranscriptionProviderUnavailable as exc:
                if fallback_provider is None:
                    raise
                logger.warning(
                    "%s unavailable for chunk %d/%d; falling back to OpenAI: %s",
                    primary_provider.display_name,
                    chunk_index,
                    chunk_count,
                    exc,
                )
                if job_id is not None:
                    await self._set_extract_progress(
                        job_id,
                        self._transcribe_progress(chunk_index, chunk_count),
                        "transcribe",
                        message=(
                            f"{primary_provider.display_name} unavailable; falling back to OpenAI "
                            f"for chunk {chunk_index}/{chunk_count}"
                        ),
                        metadata={
                            "transcription_provider": primary_provider.name,
                            "fallback_transcription_provider": fallback_provider.name,
                            "chunk_index": chunk_index,
                            "chunk_count": chunk_count,
                            "duration_seconds": duration_seconds,
                            "size_bytes": self._file_size_bytes(audio_path),
                            "attempt": attempt_number,
                            "error_class": exc.__class__.__name__,
                        },
                    )
                response_dict = await fallback_provider.transcribe(audio_path)
                break
            except Exception as exc:
                if not _is_transient_transcription_error(exc) or attempt >= settings.transcription_transient_retries:
                    if job_id is not None:
                        await self._set_extract_progress(
                            job_id,
                            self._transcribe_progress(chunk_index, chunk_count),
                            "transcribe",
                            event_status="failed",
                            message=f"{primary_provider.display_name} transcription failed for chunk {chunk_index}/{chunk_count}",
                            metadata={
                                "transcription_provider": primary_provider.name,
                                "chunk_index": chunk_index,
                                "chunk_count": chunk_count,
                                "duration_seconds": duration_seconds,
                                "size_bytes": self._file_size_bytes(audio_path),
                                "attempt": attempt_number,
                                "error_class": exc.__class__.__name__,
                            },
                        )
                    raise
                delay = settings.transcription_retry_backoff_seconds * (2**attempt)
                if job_id is not None:
                    await self._set_extract_progress(
                        job_id,
                        self._transcribe_progress(chunk_index, chunk_count),
                        "transcribe",
                        message=(
                            f"Retrying chunk {chunk_index}/{chunk_count} after transient "
                            f"{primary_provider.display_name} failure"
                        ),
                        metadata={
                            "transcription_provider": primary_provider.name,
                            "chunk_index": chunk_index,
                            "chunk_count": chunk_count,
                            "duration_seconds": duration_seconds,
                            "size_bytes": self._file_size_bytes(audio_path),
                            "attempt": attempt_number,
                            "retry_backoff_seconds": delay,
                            "error_class": exc.__class__.__name__,
                        },
                    )
                logger.warning(
                    "%s transcription transient failure for chunk %d/%d; retrying in %.1fs (attempt %d/%d): %s",
                    primary_provider.display_name,
                    chunk_index,
                    chunk_count,
                    delay,
                    attempt_number,
                    settings.transcription_transient_retries,
                    exc,
                )
                await asyncio.sleep(delay)

        if response_dict is None:
            raise RuntimeError(f"{primary_provider.display_name} transcription did not return a response for {audio_path}")
        return _render_transcript(response_dict, offset_seconds=offset_seconds)
