from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-push.yml"
TRUSTED_RUNNER = "palace-trusted-amd64"
HOSTED_RUNNER = "ubuntu-24.04"


def _load_workflow() -> dict:
    # BaseLoader keeps the top-level `on` key as a string instead of applying
    # YAML 1.1 boolean coercion.
    with WORKFLOW_PATH.open(encoding="utf-8") as workflow_file:
        return yaml.load(workflow_file, Loader=yaml.BaseLoader)


def _normalize_expression(value: str) -> str:
    return " ".join(value.split())


def _expected_validation_runner(event_name: str, head_repository: str | None) -> str:
    if event_name == "pull_request" and head_repository != "palaceoftruth/palaceoftruth":
        return HOSTED_RUNNER
    return TRUSTED_RUNNER


def test_validation_preserves_one_job_name_and_routes_by_pr_trust() -> None:
    workflow = _load_workflow()
    validate = workflow["jobs"]["validate"]

    assert _normalize_expression(validate["runs-on"]) == (
        "${{ github.event_name == 'pull_request' && "
        "github.event.pull_request.head.repo.full_name != github.repository && "
        "'ubuntu-24.04' || 'palace-trusted-amd64' }}"
    )
    assert validate["if"] == (
        "github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main'"
    )
    assert _expected_validation_runner("pull_request", "palaceoftruth/palaceoftruth") == TRUSTED_RUNNER
    assert _expected_validation_runner("pull_request", "contributor/palaceoftruth") == HOSTED_RUNNER
    assert _expected_validation_runner("push", None) == TRUSTED_RUNNER
    assert _expected_validation_runner("workflow_dispatch", None) == TRUSTED_RUNNER


def test_publishing_uses_trusted_runner_with_main_ref_guards() -> None:
    jobs = _load_workflow()["jobs"]
    main_push_or_dispatch = (
        "github.event_name == 'push' || "
        "(github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main')"
    )

    assert jobs["build-push"]["runs-on"] == TRUSTED_RUNNER
    assert jobs["build-push"]["if"] == main_push_or_dispatch
    assert jobs["publish-chart"]["runs-on"] == TRUSTED_RUNNER
    assert jobs["publish-chart"]["if"] == main_push_or_dispatch
    assert jobs["publish-agent-plugin"]["runs-on"] == TRUSTED_RUNNER
    assert jobs["publish-agent-plugin"]["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )


def test_workflow_does_not_use_privileged_pr_target_event() -> None:
    triggers = _load_workflow()["on"]

    assert "pull_request" in triggers
    assert "pull_request_target" not in triggers
