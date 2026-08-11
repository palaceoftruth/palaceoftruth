"""Resource-bounded, hard-killable document text extraction.

Extraction runs on untrusted uploads inside a shared multi-tenant process, so
it must be stoppable.  ``asyncio.wait_for`` around ``run_in_executor`` cancels
only the await: the executor thread keeps decompressing a bomb long after the
client is gone.  A child process can be killed outright, can carry its own
address-space and CPU limits, and cannot corrupt the parent's memory when it
dies, so extraction runs there instead.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import resource
from typing import Any

logger = logging.getLogger(__name__)

# Bounds the decompressed *output*, which the 50 MB input cap does not: office
# formats are zip containers with an unbounded amplification ratio in practice.
MAX_EXTRACTED_CHARS = 5_000_000
# Address space and CPU ceilings for the child. Generous enough for a legitimate
# large PDF, small enough that a bomb dies instead of taking the pod with it.
EXTRACTION_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
EXTRACTION_CPU_LIMIT_SECONDS = 120


class DocumentExtractionError(RuntimeError):
    """Raised when extraction failed for a reason attributable to the input."""


class DocumentExtractionTimeout(RuntimeError):
    """Raised when extraction exceeded its deadline and the child was killed."""


class DocumentTooComplexError(RuntimeError):
    """Raised when extraction exhausted its memory or CPU budget."""


def _multiprocessing_context() -> multiprocessing.context.BaseContext:
    """Prefer a start method that does not fork the live event-loop process."""

    for method in ("forkserver", "spawn"):
        try:
            return multiprocessing.get_context(method)
        except ValueError:
            continue
    return multiprocessing.get_context()


def _apply_child_limits(memory_limit_bytes: int, cpu_limit_seconds: int) -> None:
    """Make the child fail fast on its own instead of starving the pod."""

    for limit_name, value in (
        ("RLIMIT_AS", memory_limit_bytes),
        ("RLIMIT_CPU", cpu_limit_seconds),
    ):
        limit = getattr(resource, limit_name, None)
        if limit is None:
            continue
        try:
            soft, hard = resource.getrlimit(limit)
            ceiling = value if hard in (resource.RLIM_INFINITY, -1) else min(value, hard)
            resource.setrlimit(limit, (ceiling, hard))
        except (OSError, ValueError):
            # Not every platform honours these; the kill-on-timeout path and the
            # output cap remain in force either way.
            logger.debug("could not apply %s in extraction child", limit_name)


def _extract_in_child(
    connection,
    path: str,
    filename: str,
    max_chars: int,
    memory_limit_bytes: int,
    cpu_limit_seconds: int,
) -> None:
    _apply_child_limits(memory_limit_bytes, cpu_limit_seconds)
    try:
        from app.utils.doc_extract import extract_document

        text, metadata = extract_document(path, filename, max_chars=max_chars)
        connection.send(("ok", text, metadata))
    except MemoryError:
        connection.send(("too_complex", "document extraction exhausted its memory budget", None))
    except Exception as exc:  # noqa: BLE001 - the reason is reported, not swallowed
        connection.send(("error", f"{type(exc).__name__}: {exc}", None))
    finally:
        connection.close()


async def extract_document_bounded(
    path: str,
    filename: str,
    *,
    timeout_seconds: float,
    max_chars: int = MAX_EXTRACTED_CHARS,
    memory_limit_bytes: int = EXTRACTION_MEMORY_LIMIT_BYTES,
    cpu_limit_seconds: int = EXTRACTION_CPU_LIMIT_SECONDS,
) -> tuple[str, dict[str, Any]]:
    """Extract document text in a killable, resource-limited child process."""

    context = _multiprocessing_context()
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_extract_in_child,
        args=(child_connection, path, filename, max_chars, memory_limit_bytes, cpu_limit_seconds),
        daemon=True,
    )
    process.start()
    # The parent never writes, and must close its copy so the pipe reports EOF
    # when a killed child leaves nothing behind.
    child_connection.close()

    try:
        payload = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _receive, parent_connection, process),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        raise DocumentExtractionTimeout(
            f"document extraction exceeded {timeout_seconds:g}s"
        ) from None
    finally:
        # Unconditional: on timeout this is what actually stops the work, and on
        # success it reaps a child that has already finished.
        _terminate(process)
        parent_connection.close()

    if payload is None:
        # No result and a dead child means the kernel stopped it — an RLIMIT_AS
        # or RLIMIT_CPU kill, or the OOM killer.
        raise DocumentTooComplexError(
            "document extraction exhausted its memory or CPU budget"
        )

    status, first, second = payload
    if status == "ok":
        return first, second or {}
    if status == "too_complex":
        raise DocumentTooComplexError(first)
    raise DocumentExtractionError(first)


def _receive(connection, process) -> tuple[str, Any, Any] | None:
    """Block until the child sends a result or dies without sending one."""

    try:
        payload = connection.recv()
    except EOFError:
        return None
    process.join(timeout=5)
    return payload


def _terminate(process) -> None:
    if process.is_alive():
        process.kill()
    process.join(timeout=5)
    if hasattr(process, "close"):
        try:
            process.close()
        except ValueError:
            logger.debug("extraction child %s was not reapable", getattr(process, "pid", "?"))


def concurrency_limiter(max_concurrent: int) -> asyncio.Semaphore:
    """Build the semaphore that bounds simultaneous extraction children."""

    return asyncio.Semaphore(max(1, max_concurrent))


def default_max_concurrent_extractions() -> int:
    """Leave the pod enough CPU to keep serving every other tenant's requests."""

    return max(1, min(4, (os.cpu_count() or 2) // 2 or 1))
