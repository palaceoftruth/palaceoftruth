import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.services.memory_storage_quality import (
    ExistingMemoryAdjudicationSignals,
    MemoryStorageQualityReport,
    StaleMemoryAdjudicationReport,
)


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "report_memory_storage_quality.py"
SPEC = importlib.util.spec_from_file_location("report_memory_storage_quality", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
report_module = importlib.util.module_from_spec(SPEC)
sys.modules["report_memory_storage_quality"] = report_module
SPEC.loader.exec_module(report_module)


class _FakeSessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _warn_report() -> MemoryStorageQualityReport:
    return MemoryStorageQualityReport(
        tenant_id="tenant-a",
        item_count=0,
        warning_count=0,
        warnings_by_code={},
        sample_warnings=(),
        stale_memory_adjudication=StaleMemoryAdjudicationReport(
            tenant_id="tenant-a",
            fixture_case_count=3,
            fixture_findings=(),
            existing_data_signals=ExistingMemoryAdjudicationSignals(
                temporal_facts_by_status={},
                contradiction_edges=1,
                contradiction_edges_with_derived_context=0,
                memory_dream_items=1,
                memory_dreams_with_source_support=1,
                memory_dreams_with_contradiction_metrics=0,
                wakeup_briefs=0,
                stale_wakeup_briefs=0,
                retrieval_hint_artifacts=0,
                retrieval_hint_source_items=0,
            ),
        ),
    )


def test_report_memory_storage_quality_cli_invokes_opt_in_adjudication_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_report(_db: object, **kwargs: object) -> MemoryStorageQualityReport:
        calls.append(kwargs)
        return _warn_report()

    monkeypatch.setattr(report_module, "async_session", lambda: _FakeSessionContext())
    monkeypatch.setattr(report_module, "run_memory_storage_quality_report", fake_report)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report_memory_storage_quality.py",
            "--tenant-id",
            "tenant-a",
            "--include-adjudication-gate",
            "--format",
            "json",
        ],
    )

    exit_code = asyncio.run(report_module.main())
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert calls == [
        {
            "tenant_id": "tenant-a",
            "limit": 500,
            "sample_limit": 25,
            "include_adjudication": True,
        }
    ]
    assert output["stale_memory_adjudication"]["dry_run"] is True
    assert output["stale_memory_adjudication"]["mutating"] is False
    assert output["stale_memory_adjudication"]["no_mutation_contract"]["deletes"] is False
    assert output["stale_memory_adjudication"]["no_mutation_contract"]["prints_raw_memory_bodies"] is False
