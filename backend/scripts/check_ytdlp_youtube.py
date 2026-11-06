"""Prove that the installed yt-dlp can still extract YouTube audio.

A yt-dlp upgrade is not safe to judge by version number. A release can retire a
player client the worker pins, and the extractor then returns no media at all
while every unit test still passes, because the tests never reach YouTube. This
check runs the download options the media worker really uses, against a stable
fixture video, and fails loudly when the pinned client comes back empty.

Run it before a yt-dlp bump and after the image changes:

    python -m scripts.check_ytdlp_youtube

Exit codes: 0 the pinned client returns audio, 1 the check failed, 2 the
environment cannot run it (for example no JavaScript runtime for the challenge
solver, which is a configuration fault, not a yt-dlp verdict).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
from collections.abc import Mapping
from typing import Any

# Big Buck Bunny, Blender Foundation: public, long-lived, and not a live stream.
DEFAULT_PROBE_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_ENVIRONMENT_FAULT = 2

# The media worker options come from app.config, which refuses to load without
# these three values. This check never uses them: it extracts metadata only.
# Placeholders keep the probe runnable from a bare shell and in CI.
_CONFIG_PLACEHOLDERS = {
    "DATABASE_URL": "postgresql+asyncpg://check:check@localhost:5432/check",
    "OPENAI_API_KEY": "check-only",
    "API_KEY": "check-only",
}


def _environment_problem() -> str | None:
    """Return why this host cannot judge yt-dlp, or None when it can.

    A bare `yt-dlp` install has no EJS solver distribution, and a host with no
    JavaScript runtime cannot solve YouTube challenges. Either one makes the
    web-family players return storyboards only, which looks exactly like a
    provider regression. Refusing to judge is the honest answer.
    """

    if importlib.util.find_spec("yt_dlp_ejs") is None:
        return "the yt_dlp_ejs solver distribution is missing; install yt-dlp[default]"
    if not any(shutil.which(binary) for binary in ("deno", "bun", "node")):
        return "no supported JavaScript runtime on PATH (the worker image ships deno)"
    return None


def _audio_format_ids(info: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Return (audio-only, muxed) format ids from an extracted info dict."""

    formats = info.get("formats")
    entries: list[Any] = list(formats) if isinstance(formats, list) else [info]

    audio_only: list[str] = []
    muxed: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        format_id = str(entry.get("format_id") or "?")
        acodec = entry.get("acodec")
        vcodec = entry.get("vcodec")
        if acodec in (None, "none"):
            continue
        if vcodec in (None, "none"):
            audio_only.append(format_id)
        else:
            muxed.append(format_id)
    return audio_only, muxed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check yt-dlp YouTube audio extraction.")
    parser.add_argument("--url", default=DEFAULT_PROBE_URL)
    parser.add_argument(
        "--client",
        default=None,
        help="Override the yt-dlp YouTube player client to probe.",
    )
    args = parser.parse_args(argv)

    for name, value in _CONFIG_PLACEHOLDERS.items():
        os.environ.setdefault(name, value)

    import yt_dlp
    from yt_dlp.version import __version__ as yt_dlp_version

    from app.pipelines.youtube import build_media_download_options

    options: Any = build_media_download_options(outtmpl="/dev/null")
    options.pop("progress_hooks", None)
    options["skip_download"] = True
    if args.client:
        options["extractor_args"] = {"youtube": {"player_client": [args.client]}}

    clients = options["extractor_args"]["youtube"]["player_client"]
    print(f"yt-dlp: {yt_dlp_version}")
    print(f"player_client: {', '.join(clients)}")
    print(f"probe url: {args.url}")

    environment_problem = _environment_problem()
    if environment_problem is not None:
        print(f"ENVIRONMENT FAULT: {environment_problem}", file=sys.stderr)
        print("This host cannot judge yt-dlp; install the extras and a runtime, then retry.", file=sys.stderr)
        return EXIT_ENVIRONMENT_FAULT

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            # download=True with skip_download still runs format selection, which
            # is where a retired player client fails ("Requested format is not
            # available"). download=False skips selection entirely and would pass
            # on a broken client, so it is not a valid probe.
            info = ydl.extract_info(args.url, download=True)
    except Exception as exc:  # yt_dlp raises DownloadError, plus provider errors
        message = str(exc)
        lowered = message.lower()
        if "javascript runtime" in lowered or "challenge" in lowered or "ejs" in lowered:
            print(f"ENVIRONMENT FAULT: {message}", file=sys.stderr)
            print(
                "Install a supported JavaScript runtime (the image ships deno) and retry.",
                file=sys.stderr,
            )
            return EXIT_ENVIRONMENT_FAULT
        print(f"FAILED: yt-dlp could not extract this video: {message}", file=sys.stderr)
        return EXIT_CHECK_FAILED

    if not isinstance(info, Mapping):
        print("FAILED: extraction returned no video info", file=sys.stderr)
        return EXIT_CHECK_FAILED

    audio_only, muxed = _audio_format_ids(info)
    if not audio_only and not muxed:
        print(
            "FAILED: the pinned player client returned no audio format. "
            "A yt-dlp upgrade retired it, or YouTube changed the player. "
            "Probe another client with --client before changing the pin.",
            file=sys.stderr,
        )
        return EXIT_CHECK_FAILED

    print(
        f"OK: {info.get('title')} | duration={info.get('duration')}s | "
        f"audio_only={len(audio_only)} muxed={len(muxed)}"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())