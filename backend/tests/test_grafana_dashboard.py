import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_PATH = REPO_ROOT / "docs/monitoring/grafana/palace-operations.json"


def test_operations_dashboard_covers_retrieval_freshness_and_remote_write() -> None:
    dashboard = json.loads(DASHBOARD_PATH.read_text())
    panels = dashboard["panels"]

    panel_ids = [panel["id"] for panel in panels]
    assert len(panel_ids) == len(set(panel_ids))

    expressions = "\n".join(
        target["expr"]
        for panel in panels
        for target in panel.get("targets", [])
        if "expr" in target
    )
    required_metrics = {
        "palace_retrieval_stage_duration_seconds_bucket",
        "palace_retrieval_requests_total",
        "palace_retrieval_classifications_total",
        "palace_retrieval_results_total",
        "palace_embedding_requests_total",
        "palace_jobs_oldest_age_seconds",
        "palace_source_refresh_due",
        "palace_source_refresh_oldest_due_age_seconds",
        "palace_source_refreshes_total",
        "prometheus_remote_storage_samples_failed_total",
        "prometheus_remote_storage_samples_pending",
    }

    for metric in required_metrics:
        assert metric in expressions
    for panel in panels:
        if panel["id"] < 14:
            continue
        for target in panel.get("targets", []):
            expression = target.get("expr", "")
            assert 'cluster="$cluster"' in expression
            if "prometheus_remote_storage_" not in expression:
                assert 'namespace="$namespace"' in expression
                assert 'job="$job"' in expression

    assert "max by (kind, status) (palace_source_refresh_due" in expressions
    assert "max by (outcome, validator, change) (increase(palace_source_refreshes_total" in expressions


def test_operations_dashboard_has_expected_environment_variables() -> None:
    dashboard = json.loads(DASHBOARD_PATH.read_text())
    variable_names = {variable["name"] for variable in dashboard["templating"]["list"]}

    assert {"datasource", "cluster", "namespace", "job"} <= variable_names
