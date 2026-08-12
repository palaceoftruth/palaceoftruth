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
import math
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Bounds the decompressed *output*, which the 50 MB input cap does not: office
# formats are zip containers with an unbounded amplification ratio in practice.
MAX_EXTRACTED_CHARS = 5_000_000
# Address space and CPU ceilings for the child. Generous enough for a legitimate
# large PDF, small enough that a bomb dies instead of taking the pod with it.
EXTRACTION_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
EXTRACTION_CPU_LIMIT_SECONDS = 90
_MIN_CHILD_MEMORY_BYTES = 128 * 1024 * 1024
_POD_MEMORY_HEADROOM_BYTES = 256 * 1024 * 1024


class DocumentExtractionError(RuntimeError):
    """Raised when extraction failed for a reason attributable to the input."""


class DocumentValidationError(DocumentExtractionError):
    """Raised when the uploaded bytes do not match the declared document type."""


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
            logger.warning("could not apply %s in extraction child", limit_name)


def _read_positive_int(path: str) -> int | None:
    try:
        raw = Path(path).read_text(encoding="ascii").strip()
        value = int(raw)
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def cgroup_memory_limit_bytes() -> int | None:
    """Return the active container memory ceiling for cgroup v2 or v1."""

    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        value = _read_positive_int(path)
        if value is not None and value < (1 << 60):
            return value
    return None


def cgroup_cpu_quota() -> float | None:
    """Return CPU cores available to this cgroup when a finite quota exists."""

    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="ascii").split()
        if quota != "max":
            return max(float(quota) / float(period), 0.01)
    except (OSError, ValueError, ZeroDivisionError):
        pass
    quota = _read_positive_int("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_positive_int("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota is not None and period is not None:
        return max(quota / period, 0.01)
    return None


def default_child_memory_limit_bytes(*, concurrency: int | None = None) -> int:
    workers = concurrency or default_max_concurrent_extractions()
    pod_limit = cgroup_memory_limit_bytes()
    if pod_limit is None:
        return EXTRACTION_MEMORY_LIMIT_BYTES
    headroom = min(_POD_MEMORY_HEADROOM_BYTES, max(pod_limit // 4, 64 * 1024 * 1024))
    available = max(pod_limit - headroom, _MIN_CHILD_MEMORY_BYTES)
    return max(
        _MIN_CHILD_MEMORY_BYTES,
        min(EXTRACTION_MEMORY_LIMIT_BYTES, available // max(workers, 1)),
    )


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
        from app.utils.file_type import FileTypeError, verify_file_type

        verify_file_type(path, Path(filename).suffix.lower())
        text, metadata = extract_document(path, filename, max_chars=max_chars)
        connection.send(("ok", text, metadata))
    except FileTypeError as exc:
        connection.send(("invalid", str(exc), None))
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
    memory_limit_bytes: int | None = None,
    cpu_limit_seconds: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Extract document text in a killable, resource-limited child process."""

    resolved_memory_limit = (
        default_child_memory_limit_bytes()
        if memory_limit_bytes is None
        else memory_limit_bytes
    )
    resolved_cpu_limit = (
        min(EXTRACTION_CPU_LIMIT_SECONDS, max(1, math.floor(timeout_seconds) - 1))
        if cpu_limit_seconds is None
        else cpu_limit_seconds
    )
    context = _multiprocessing_context()
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_extract_in_child,
        args=(child_connection, path, filename, max_chars, resolved_memory_limit, resolved_cpu_limit),
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
    if status == "invalid":
        raise DocumentValidationError(first)
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

    cpu_budget = cgroup_cpu_quota() or float(os.cpu_count() or 2)
    return max(1, min(4, math.floor(cpu_budget)))
