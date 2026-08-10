"""Root logging configuration for every Palace of Truth Python entrypoint.

Without this, `logging.getLogger(__name__)` calls throughout the app are silent:
the root logger defaults to WARNING with no handler, so `logger.info(...)` is
discarded before it ever reaches stdout. Uvicorn and ARQ each configure only
their own logger namespaces, never the root logger, so neither one covers the
`app.*` loggers.
"""

import logging
import os
import sys

# Matches the shape of Uvicorn's default access/error lines so a single stream
# stays readable when application and server records interleave.
_LOG_FORMAT = "%(levelname)s:     %(asctime)s %(name)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def _resolve_level(explicit: str | None) -> int:
    raw = (explicit or os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    # getLevelName returns the string back unchanged for unknown names, which
    # would make setLevel raise. Fall back rather than break startup.
    level = logging.getLevelName(raw)
    return level if isinstance(level, int) else logging.INFO


def configure_logging(level: str | None = None, *, force: bool = False) -> None:
    """Attach a stdout handler to the root logger.

    Idempotent — repeat calls are ignored so importing this from several
    entrypoints in one process cannot duplicate every log line.

    Args:
        level: Level name override. Defaults to the LOG_LEVEL env var, then INFO.
        force: Reconfigure even if this function already ran (used by tests).
    """
    global _configured
    if _configured and not force:
        return

    resolved = _resolve_level(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    # `force=True` drops handlers a previous basicConfig or a test harness left
    # behind, so records are emitted exactly once.
    logging.basicConfig(level=resolved, handlers=[handler], force=True)
    root.setLevel(resolved)

    # SQLAlchemy echoes every statement at INFO. Pin it to its own level so
    # raising the application level to INFO does not flood the log with SQL.
    logging.getLogger("sqlalchemy.engine").setLevel(
        _resolve_level(os.environ.get("SQLALCHEMY_LOG_LEVEL") or "WARNING")
    )

    _configured = True
