from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class PidLockHolder:
    pid: int | None
    raw: str
    age_seconds: float | None
    alive: bool

    def describe(self) -> str:
        pid = str(self.pid) if self.pid is not None else self.raw or "unknown"
        age = f", age_seconds={self.age_seconds:.1f}" if self.age_seconds is not None else ""
        return f"pid {pid}{age}"


def _read_holder(path: Path) -> PidLockHolder:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        raw = "unknown"
    try:
        pid = int(raw)
    except ValueError:
        pid = None
    try:
        modified_at = os.path.getmtime(path)
        age_seconds = max(0.0, time.time() - modified_at)
    except OSError:
        age_seconds = None
    return PidLockHolder(pid=pid, raw=raw, age_seconds=age_seconds, alive=_pid_is_alive(pid))


def _pid_is_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def pid_file_lock(lock_path: Path | None, *, name: str) -> Iterator[None]:
    if lock_path is None:
        yield
        return

    path = lock_path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    while True:
        try:
            fd = os.open(path, flags, 0o600)
            break
        except FileExistsError as exc:
            holder = _read_holder(path)
            if holder.alive:
                raise RuntimeError(f"{name} lock is already held at {path} by {holder.describe()}") from exc
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as unlink_exc:
                raise RuntimeError(
                    f"{name} lock at {path} is stale ({holder.describe()}) but could not be removed: {unlink_exc}"
                ) from unlink_exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
