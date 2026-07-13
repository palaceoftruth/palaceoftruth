import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-push.yml"
GH_SETUP_ACTION_PATH = REPO_ROOT / ".github" / "actions" / "setup-gh" / "action.yml"
TRUSTED_RUNNER = "palace-trusted-amd64"
HOSTED_RUNNER = "ubuntu-24.04"
GH_SETUP_ACTION = "./.github/actions/setup-gh"


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
    assert jobs["build-push"]["permissions"] == {
        "contents": "write",
        "packages": "write",
    }
    assert jobs["publish-agent-plugin"]["permissions"] == {"contents": "write"}
    assert jobs["publish-chart"]["permissions"] == {
        "contents": "write",
        "packages": "write",
        "pull-requests": "write",
    }


def test_workflow_does_not_use_privileged_pr_target_event() -> None:
    triggers = _load_workflow()["on"]

    assert "pull_request" in triggers
    assert "pull_request_target" not in triggers


def test_every_trusted_job_using_gh_provisions_the_pinned_cli_first() -> None:
    jobs = _load_workflow()["jobs"]
    jobs_using_gh: set[str] = set()

    for job_name, job in jobs.items():
        steps = job.get("steps", [])
        gh_step_indexes = [
            index
            for index, step in enumerate(steps)
            if re.search(r"\bgh\s", step.get("run", ""))
        ]
        if not gh_step_indexes:
            continue

        jobs_using_gh.add(job_name)
        assert job["runs-on"] == TRUSTED_RUNNER
        setup_indexes = [
            index
            for index, step in enumerate(steps)
            if step.get("uses") == GH_SETUP_ACTION
        ]
        checkout_indexes = [
            index
            for index, step in enumerate(steps)
            if step.get("uses") == "actions/checkout@v6"
        ]
        assert len(checkout_indexes) == 1
        assert len(setup_indexes) == 1
        assert "if" not in steps[setup_indexes[0]]
        assert checkout_indexes[0] < setup_indexes[0]
        assert setup_indexes[0] < min(gh_step_indexes)

    assert jobs_using_gh == {"build-push", "publish-agent-plugin", "publish-chart"}


def test_github_cli_setup_is_version_and_checksum_pinned() -> None:
    with GH_SETUP_ACTION_PATH.open(encoding="utf-8") as action_file:
        action = yaml.load(action_file, Loader=yaml.BaseLoader)

    assert action["runs"]["using"] == "composite"
    install_step = action["runs"]["steps"][0]
    assert install_step["env"] == {
        "GH_CLI_VERSION": "2.96.0",
        "GH_CLI_SHA256": "83d5c2ccad5498f58bf6368acb1ab32588cf43ab3a4b1c301bf36328b1c8bd60",
    }

    script = install_step["run"]
    assert "releases/download/v${GH_CLI_VERSION}/${ARCHIVE}" in script
    assert "sha256sum --check --strict" in script
    assert '"$INSTALL_DIR/bin/gh" version | awk' in script
    assert 'echo "$INSTALL_DIR/bin" >> "$GITHUB_PATH"' in script
