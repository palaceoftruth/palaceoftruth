"""Regression tests for application log visibility.

A production 500 was undiagnosable because application logs never reached the
container log. Two defects caused it, and both are easy to reintroduce, so each
one is pinned here.
"""

import logging
import sys
from logging.config import fileConfig
from pathlib import Path

import pytest

from app.logging_config import configure_logging, is_configured

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
ENV_PY = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """Undo whatever a test did to process-wide logging.

    fileConfig() disables every pre-existing logger, and that flag survives on
    the singleton logger objects, so restoring the root handlers alone would
    leak a disabled app.main into the next test.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_disabled = {
        name: item.disabled
        for name, item in logging.Logger.manager.loggerDict.items()
        if isinstance(item, logging.Logger)
    }
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, disabled in saved_disabled.items():
        item = logging.Logger.manager.loggerDict.get(name)
        if isinstance(item, logging.Logger):
            item.disabled = disabled


def test_configure_logging_emits_info_records_to_stdout(capsys) -> None:
    configure_logging(force=True)

    logging.getLogger("app.main").info("Database migrations complete")

    assert "Database migrations complete" in capsys.readouterr().out


def test_configure_logging_does_not_duplicate_records(capsys) -> None:
    configure_logging(force=True)
    configure_logging(force=True)

    logging.getLogger("app.main").info("ARQ pool ready")

    assert capsys.readouterr().out.count("ARQ pool ready") == 1


def test_sqlalchemy_stays_quiet_when_the_app_logs_at_info(capsys) -> None:
    configure_logging(force=True)

    logging.getLogger("sqlalchemy.engine").info("SELECT 1")

    assert "SELECT 1" not in capsys.readouterr().out


def test_alembic_ini_would_silence_application_logging(capsys) -> None:
    """Pins *why* the env.py guard exists.

    alembic.ini sets the root logger to WARN and fileConfig() disables existing
    loggers by default. Running it in the API process is what silenced every
    app log — and `uvicorn.error`, which is how Uvicorn reports lifespan
    tracebacks. If this ever stops being true the guard can be reconsidered.
    """
    configure_logging(force=True)
    # Both loggers must exist beforehand: fileConfig only disables loggers that
    # are already present, which is exactly the situation at API startup.
    logging.getLogger("app.main")
    logging.getLogger("uvicorn.error")

    fileConfig(str(ALEMBIC_INI))

    logging.getLogger("app.main").info("Database migrations complete")
    assert "Database migrations complete" not in capsys.readouterr().out
    assert logging.getLogger("uvicorn.error").disabled is True


def test_alembic_env_guards_fileconfig_behind_is_configured() -> None:
    """env.py must not reconfigure logging when the app already owns it."""
    source = ENV_PY.read_text()

    assert "logging_is_configured" in source, "env.py must import the guard"
    assert "not logging_is_configured()" in source, (
        "fileConfig() must be guarded, or in-process migrations will silence "
        "every application log for the rest of the process"
    )


def test_is_configured_reports_configuration_state() -> None:
    configure_logging(force=True)

    assert is_configured() is True


def test_unknown_log_level_falls_back_to_info(monkeypatch, capsys) -> None:
    monkeypatch.setenv("LOG_LEVEL", "NOT_A_LEVEL")

    configure_logging(force=True)

    logging.getLogger("app.main").info("still visible")
    assert "still visible" in capsys.readouterr().out


def test_handler_targets_stdout() -> None:
    configure_logging(force=True)

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert handlers[0].stream is sys.stdout
