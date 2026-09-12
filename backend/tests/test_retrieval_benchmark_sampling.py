import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_agent_memory_retrieval.py"
SPEC = importlib.util.spec_from_file_location("benchmark_agent_memory_retrieval_sampling", SCRIPT_PATH)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


class Response:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"results": [], "total": 0, "scopes": [], "trace": {}}


def _requests(count: int = 3) -> list[tuple[dict, str, dict]]:
    return [({"id": f"case-{i}"}, "/retrieve", {"query": "private query"}) for i in range(count)]


def test_sampling_excludes_warmups_and_reports_percentiles() -> None:
    args = SimpleNamespace(warmup=2, repeat=3, concurrency=1)
    samples = [{"case_id": "a", "duration_ms": value, "status": "ok"} for value in (30, 10, 40, 20)]
    warmups = [{"case_id": "a", "duration_ms": 1, "status": "ok"} for _ in range(2)]

    report = benchmark._performance_report(samples, warmups, args)

    assert report["summary"]["sample_count"] == 4
    assert report["summary"]["warmup_count"] == 2
    assert report["summary"]["duration_ms"] == {"count": 4, "p50": 20.0, "p95": 40.0}
    assert "p99" in report["summary"]["notes"][0]
    assert all("query" not in sample for sample in report["samples"])


def test_live_command_records_warmup_separately(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pack = tmp_path / "pack.json"
    output = tmp_path / "report.json"
    pack.write_text(json.dumps({"schema_version": 1, "pack_id": "warm", "cases": [{"id": "one", "query": "q", "expected_item_ids": []}]}))
    monkeypatch.setattr(benchmark, "case_from_live_response", lambda case, **kwargs: case)
    monkeypatch.setattr(benchmark, "evaluate_eval_pack", lambda *args, **kwargs: {"summary": {"passed": True}})

    class Client:
        calls = 0

        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, endpoint: str, *, json: dict) -> Response:
            type(self).calls += 1
            return Response()

    monkeypatch.setattr(benchmark.httpx, "Client", Client)
    args = benchmark.build_parser().parse_args([
        "live-report", "--pack", str(pack), "--base-url", "https://palaceoftruth.test",
        "--api-key", "key", "--warmup", "1", "--repeat", "2", "--output", str(output),
    ])
    assert benchmark.cmd_live_report(args) == 0
    report = json.loads(output.read_text())
    performance = report["performance"]
    assert Client.calls == 3
    assert len(performance["samples"]) == 2
    assert len(performance["warmup_samples"]) == 1
    assert performance["summary"]["sample_count"] == 2
    assert performance["summary"]["warmup_count"] == 1


def test_batch_concurrency_is_bounded() -> None:
    active = 0
    maximum = 0
    lock = threading.Lock()

    class Client:
        def post(self, endpoint: str, *, json: dict) -> Response:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return Response()

    outcomes = benchmark._run_live_batch(Client(), _requests(8), 2)
    assert len(outcomes) == 8
    assert maximum == 2


def test_errors_are_kept_and_timeout_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(benchmark, "case_from_live_response", lambda case, **kwargs: case)
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, endpoint: str, *, json: dict) -> Response:
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("secret response text")
            return Response()

    outcomes = benchmark._run_live_batch(Client(), _requests(2), 1)
    statuses = [outcome["sample"]["status"] for outcome in outcomes]
    assert statuses == ["timeout", "ok"]
    assert all("secret" not in json.dumps(outcome["sample"]) for outcome in outcomes)


def test_default_live_flags_remain_serial_single_sample() -> None:
    args = benchmark.build_parser().parse_args(
        ["live-report", "--base-url", "https://palaceoftruth.test", "--api-key", "key"]
    )
    assert (args.warmup, args.repeat, args.concurrency) == (0, 1, 1)


def test_invalid_bounds_are_rejected() -> None:
    args = benchmark.build_parser().parse_args(
        ["live-report", "--base-url", "https://palaceoftruth.test", "--api-key", "key", "--repeat", "0"]
    )
    with pytest.raises(benchmark.AgentMemoryEvalInputError, match="repeat"):
        benchmark.cmd_live_report(args)
    for option, value, label in (("--warmup", "101", "warmup"), ("--concurrency", "33", "concurrency")):
        bounded_args = benchmark.build_parser().parse_args(
            ["live-report", "--base-url", "https://palaceoftruth.test", "--api-key", "key", option, value]
        )
        with pytest.raises(benchmark.AgentMemoryEvalInputError, match=label):
            benchmark.cmd_live_report(bounded_args)


def test_live_report_returns_nonzero_when_a_measured_request_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pack = tmp_path / "pack.json"
    pack.write_text(json.dumps({"schema_version": 1, "pack_id": "errors", "cases": [{"id": "one", "query": "q", "expected_item_ids": []}]}))

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, endpoint: str, *, json: dict) -> Response:
            raise httpx.ReadTimeout("private failure detail")

    monkeypatch.setattr(benchmark.httpx, "Client", Client)
    args = benchmark.build_parser().parse_args(
        ["live-report", "--pack", str(pack), "--base-url", "https://palaceoftruth.test", "--api-key", "key"]
    )
    assert benchmark.cmd_live_report(args) == 1


def test_single_case_repeats_can_run_concurrently(monkeypatch, tmp_path):
    pack = tmp_path / "pack.json"
    output = tmp_path / "report.json"
    pack.write_text(json.dumps({"schema_version": 1, "pack_id": "parallel", "cases": [
        {"id": "one", "query": "synthetic query", "expected_item_ids": []},
    ]}))
    monkeypatch.setattr(benchmark, "case_from_live_response", lambda case, **kwargs: case)
    monkeypatch.setattr(benchmark, "evaluate_eval_pack", lambda *args, **kwargs: {"summary": {"passed": True}})
    barrier = threading.Barrier(2, timeout=2)

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, endpoint, *, json):
            barrier.wait()
            return Response()

    monkeypatch.setattr(benchmark.httpx, "Client", Client)
    args = benchmark.build_parser().parse_args([
        "live-report", "--pack", str(pack), "--base-url", "http://localhost:8000",
        "--api-key", "synthetic-key", "--repeat", "2", "--concurrency", "2", "--output", str(output),
    ])
    assert benchmark.cmd_live_report(args) == 0
    samples = json.loads(output.read_text())["performance"]["samples"]
    assert [sample["repeat"] for sample in samples] == [1, 2]
    assert all(sample["status"] == "ok" for sample in samples)
