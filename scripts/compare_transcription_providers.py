#!/usr/bin/env python3
"""Compare transcription providers on non-sensitive sample media.

The report intentionally prints aggregate quality signals only. It does not
print raw transcript text.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost/unused")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("API_KEY", "unused")

from app.config import settings  # noqa: E402
from app.pipelines.youtube import (  # noqa: E402
    AssemblyAITranscriptionProvider,
    MediaPipeline,
    OpenAITranscriptionProvider,
    TranscriptionChunk,
    _cleanup_media_temp_files,
    _probe_audio_duration_seconds,
    _render_transcript,
)

OPENAI_TRANSCRIPTION_USD_PER_HOUR = float(os.getenv("OPENAI_TRANSCRIPTION_USD_PER_HOUR", "0.36"))
ASSEMBLYAI_UNIVERSAL_2_USD_PER_HOUR = float(os.getenv("ASSEMBLYAI_UNIVERSAL_2_USD_PER_HOUR", "0.15"))
ASSEMBLYAI_SPEAKER_DIARIZATION_USD_PER_HOUR = float(
    os.getenv("ASSEMBLYAI_SPEAKER_DIARIZATION_USD_PER_HOUR", "0.02")
)


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _provider_cost_per_hour(provider: str) -> float:
    if provider == "assemblyai":
        return ASSEMBLYAI_UNIVERSAL_2_USD_PER_HOUR + ASSEMBLYAI_SPEAKER_DIARIZATION_USD_PER_HOUR
    return OPENAI_TRANSCRIPTION_USD_PER_HOUR


async def _run_provider(provider: str, audio_path: str) -> dict[str, Any]:
    adapter = AssemblyAITranscriptionProvider() if provider == "assemblyai" else OpenAITranscriptionProvider()
    started = time.perf_counter()
    try:
        response = await adapter.transcribe(audio_path)
        transcript, segments = _render_transcript(response, offset_seconds=0.0)
        return {
            "provider": provider,
            "status": "ok",
            "runtime_seconds": round(time.perf_counter() - started, 3),
            "empty_transcript": not bool(transcript.strip()),
            "speaker_segment_count": len(segments),
            "speaker_count": len({segment.get("speaker") for segment in segments}),
        }
    except Exception as exc:
        return {
            "provider": provider,
            "status": "failed",
            "runtime_seconds": round(time.perf_counter() - started, 3),
            "error_class": exc.__class__.__name__,
            "error": str(exc)[:240],
        }


def _source_audio(source: str, job_id: str) -> tuple[str, dict[str, Any], bool]:
    if _is_url(source):
        audio_path, metadata = MediaPipeline._download_audio(source, job_id)
        return audio_path, metadata, True

    audio_path = str(Path(source).expanduser())
    duration_seconds = _probe_audio_duration_seconds(audio_path)
    return audio_path, {"duration_seconds": duration_seconds}, False


async def _compare_source(source: str, providers: list[str]) -> dict[str, Any]:
    job_id = f"transcription-compare-{uuid.uuid4().hex[:12]}"
    chunks: list[TranscriptionChunk] = []
    audio_path: str | None = None
    cleanup_audio = False
    try:
        audio_path, metadata, cleanup_audio = _source_audio(source, job_id)
        duration_seconds = metadata.get("duration_seconds")
        duration_hours = float(duration_seconds or 0) / 3600
        results = []
        for provider in providers:
            result = await _run_provider(provider, audio_path)
            result["estimated_cost_usd"] = round(duration_hours * _provider_cost_per_hour(provider), 5)
            results.append(result)
        return {
            "source": source,
            "duration_seconds": duration_seconds,
            "results": results,
        }
    finally:
        if cleanup_audio:
            _cleanup_media_temp_files(job_id=job_id, audio_path=audio_path, chunks=chunks)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", help="Non-sensitive audio files or media URLs to compare")
    parser.add_argument(
        "--provider",
        action="append",
        choices=("openai", "assemblyai"),
        dest="providers",
        help="Provider to run. Repeat to compare multiple providers. Defaults to both.",
    )
    parser.add_argument("--assemblyai-api-key", help="AssemblyAI key override; otherwise ASSEMBLYAI_API_KEY is used")
    parser.add_argument("--openai-api-key", help="OpenAI key override; otherwise OPENAI_API_KEY is used")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    if args.assemblyai_api_key:
        settings.assemblyai_api_key = args.assemblyai_api_key
    if args.openai_api_key:
        settings.openai_api_key = args.openai_api_key

    providers = args.providers or ["openai", "assemblyai"]
    reports = []
    for source in args.sources:
        reports.append(await _compare_source(source, providers))

    for report in reports:
        print(f"source={report['source']} duration_seconds={report['duration_seconds']}")
        for result in report["results"]:
            parts = [
                f"provider={result['provider']}",
                f"status={result['status']}",
                f"runtime_seconds={result['runtime_seconds']}",
                f"estimated_cost_usd={result['estimated_cost_usd']}",
            ]
            if result["status"] == "ok":
                parts.extend(
                    [
                        f"empty_transcript={result['empty_transcript']}",
                        f"speaker_segment_count={result['speaker_segment_count']}",
                        f"speaker_count={result['speaker_count']}",
                    ]
                )
            else:
                parts.extend([f"error_class={result['error_class']}", f"error={result['error']}"])
            print("  " + " ".join(parts))


if __name__ == "__main__":
    asyncio.run(_main())
