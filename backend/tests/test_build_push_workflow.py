import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-push.yml"
GH_SETUP_ACTION_PATH = REPO_ROOT / ".github" / "actions" / "setup-gh" / "action.yml"
TRUSTED_RUNNER = "palace-trusted-amd64"
PR_RUNNER = "ubuntu-24.04"
GH_SETUP_ACTION = "./.github/actions/setup-gh"
CHECKOUT_ACTION = "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"
COMMIT_PINNED_ACTION = re.compile(r"^[^./][^@]*@[0-9a-f]{40}$")
OCI_PINNED_ACTION = re.compile(r"^docker://.+@sha256:[0-9a-f]{64}$")


def _load_workflow() -> dict:
    # BaseLoader keeps the top-level `on` key as a string instead of applying
    # YAML 1.1 boolean coercion.
    with WORKFLOW_PATH.open(encoding="utf-8") as workflow_file:
        return yaml.load(workflow_file, Loader=yaml.BaseLoader)


def _normalize_expression(value: str) -> str:
    return " ".join(value.split())


def _expected_validation_runner(event_name: str, head_repository: str | None) -> str:
    if event_name == "pull_request":
        return PR_RUNNER
    return TRUSTED_RUNNER


def test_validation_preserves_one_job_name_and_routes_by_pr_trust() -> None:
    workflow = _load_workflow()
    classify = workflow["jobs"]["classify"]
    validate = workflow["jobs"]["ci-gate"]

    assert _normalize_expression(classify["runs-on"]) == (
        "${{ github.event_name == 'pull_request' && 'ubuntu-24.04' || 'palace-trusted-amd64' }}"
    )
    assert _normalize_expression(validate["runs-on"]) == (
        "${{ github.event_name == 'pull_request' && 'ubuntu-24.04' || 'palace-trusted-amd64' }}"
    )
    assert validate["name"] == "validate"
    assert validate["if"] == "always()"
    assert _expected_validation_runner("pull_request", "palaceoftruth/palaceoftruth") == PR_RUNNER
    assert _expected_validation_runner("pull_request", "contributor/palaceoftruth") == PR_RUNNER
    assert _expected_validation_runner("push", None) == TRUSTED_RUNNER
    assert _expected_validation_runner("workflow_dispatch", None) == TRUSTED_RUNNER


def test_chart_release_classifier_matches_digest_coordinate_commit() -> None:
    jobs = _load_workflow()["jobs"]
    run = jobs["classify"]["steps"][1]["run"]

    assert '"${#CHANGED_FILES[@]}" -eq 2' in run
    assert '"${CHANGED_FILES[0]}" = "chart/Chart.yaml"' in run
    assert '"${CHANGED_FILES[1]}" = "chart/values.yaml"' in run

    release_scope = next(
        step
        for step in jobs["publish-chart"]["steps"]
        if step.get("name") == "Detect chart-only release bump"
    )["run"]
    assert '"${#CHANGED_FILES[@]}" -eq 2' in release_scope
    assert '"${CHANGED_FILES[0]}" = "chart/Chart.yaml"' in release_scope
    assert '"${CHANGED_FILES[1]}" = "chart/values.yaml"' in release_scope


def test_publishing_uses_trusted_runner_with_main_ref_guards() -> None:
    jobs = _load_workflow()["jobs"]
    main_push_or_dispatch = (
        "github.event_name == 'push' || "
        "(github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main')"
    )
    classified_main_push_or_dispatch = (
        "needs.classify.outputs.chart_release_only != 'true' && "
        f"({main_push_or_dispatch})"
    )

    assert jobs["build-backend"]["runs-on"] == TRUSTED_RUNNER
    assert _normalize_expression(jobs["build-backend"]["if"]) == classified_main_push_or_dispatch
    assert jobs["build-frontend"]["runs-on"] == TRUSTED_RUNNER
    assert _normalize_expression(jobs["build-frontend"]["if"]) == classified_main_push_or_dispatch
    assert jobs["publish-chart"]["runs-on"] == TRUSTED_RUNNER
    assert jobs["publish-chart"]["if"] == main_push_or_dispatch
    assert jobs["publish-agent-plugin"]["runs-on"] == TRUSTED_RUNNER
    assert _normalize_expression(jobs["publish-agent-plugin"]["if"]) == (
        "needs.classify.outputs.chart_release_only != 'true' && "
        "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert jobs["build-backend"]["permissions"] == {
        "attestations": "write",
        "contents": "write",
        "id-token": "write",
        "packages": "write",
    }
    assert jobs["build-frontend"]["permissions"] == {
        "attestations": "write",
        "contents": "read",
        "id-token": "write",
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


def test_remote_actions_are_commit_pinned() -> None:
    workflow = _load_workflow()

    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            action = step.get("uses")
            if not action or action.startswith("./"):
                continue
            assert COMMIT_PINNED_ACTION.fullmatch(action) or OCI_PINNED_ACTION.fullmatch(action), (
                f"{job_name} uses mutable remote action reference {action}"
            )


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
            if step.get("uses") == CHECKOUT_ACTION
        ]
        assert len(checkout_indexes) == 1
        assert len(setup_indexes) == 1
        assert "if" not in steps[setup_indexes[0]]
        assert checkout_indexes[0] < setup_indexes[0]
        assert setup_indexes[0] < min(gh_step_indexes)

    assert jobs_using_gh == {"build-backend", "publish-agent-plugin", "publish-chart"}


def test_image_builds_are_parallel_attested_and_digest_bound() -> None:
    jobs = _load_workflow()["jobs"]
    backend = jobs["build-backend"]
    frontend = jobs["build-frontend"]
    publish_chart = jobs["publish-chart"]

    assert backend["needs"] == ["classify", "ci-gate"]
    assert frontend["needs"] == ["classify", "ci-gate"]
    assert publish_chart["needs"] == ["build-backend", "build-frontend"]

    backend_steps = {step.get("id"): step for step in backend["steps"] if step.get("id")}
    assert backend_steps["backend_runtime"]["with"]["target"] == "backend-runtime"
    assert backend_steps["backend_ci"]["with"]["target"] == "backend-ci"
    assert backend_steps["backend"]["with"]["target"] == "app"
    assert backend_steps["worker"]["with"]["target"] == "worker"
    assert backend["outputs"]["backend_digest"] == "${{ steps.backend.outputs.digest }}"
    assert backend["outputs"]["backend_runtime_digest"] == (
        "${{ steps.backend_runtime.outputs.digest }}"
    )
    assert backend["outputs"]["backend_ci_digest"] == "${{ steps.backend_ci.outputs.digest }}"
    assert frontend["outputs"]["frontend_digest"] == "${{ steps.frontend.outputs.digest }}"

    for job in (backend, frontend):
        buildx_step = next(
            step for step in job["steps"] if step.get("name") == "Set up Docker Buildx"
        )
        assert buildx_step["with"]["driver"] == "docker"

        for step in job["steps"]:
            if not step.get("uses", "").startswith("docker/build-push-action@"):
                continue
            assert "cache-to" not in step["with"]
            assert "cache-from" not in step["with"]
            # The ARC DinD docker driver cannot emit BuildKit attestations.
            # Each digest is attested after push by the local attest-image action.
            assert "provenance" not in step["with"]
            assert "sbom" not in step["with"]
            assert ":latest" not in step["with"]["tags"]

        attestation_steps = [
            step
            for step in job["steps"]
            if step.get("uses") == "./.github/actions/attest-image"
        ]
        expected_attestations = 5 if job is backend else 1
        assert len(attestation_steps) == expected_attestations
        for step in attestation_steps:
            assert step["with"]["image"]
            assert step["with"]["digest"]
            assert step["with"]["sbom-name"]

        assert job["permissions"]["attestations"] == "write"
        assert job["permissions"]["id-token"] == "write"

    digest_step = next(
        step for step in publish_chart["steps"] if step.get("name") == "Record published image digests"
    )
    assert digest_step["env"]["BACKEND_DIGEST"] == (
        "${{ needs.build-backend.outputs.backend_digest }}"
    )
    assert digest_step["env"]["FRONTEND_DIGEST"] == (
        "${{ needs.build-frontend.outputs.frontend_digest }}"
    )


def test_validation_fans_out_to_required_lanes_and_aggregates_one_gate() -> None:
    jobs = _load_workflow()["jobs"]
    lane_names = [
        "backend-fast",
        "backend-database",
        "frontend",
        "helm-policy",
        "extension",
        "browser",
    ]
    gate = jobs["ci-gate"]

    assert gate["name"] == "validate"
    assert gate["needs"] == ["classify", *lane_names]
    assert gate["if"] == "always()"
    assert gate["permissions"] == {}
    assert jobs["build-backend"]["needs"] == ["classify", "ci-gate"]
    assert jobs["build-frontend"]["needs"] == ["classify", "ci-gate"]
    assert jobs["publish-agent-plugin"]["needs"] == ["classify", "ci-gate"]

    expected_runner = _normalize_expression(jobs["classify"]["runs-on"])
    for lane_name in lane_names:
        lane = jobs[lane_name]
        assert lane["needs"] == "classify"
        assert _normalize_expression(lane["runs-on"]) == expected_runner

    gate_step = gate["steps"][0]
    assert gate_step["env"]["HELM_POLICY_RESULT"] == "${{ needs.helm-policy.result }}"
    assert 'if [ "$CHART_RELEASE_ONLY" = "true" ]' in gate_step["run"]
    assert "All required validation lanes succeeded." in gate_step["run"]


def test_validation_lanes_cover_full_backend_extension_browser_and_policy_checks() -> None:
    jobs = _load_workflow()["jobs"]

    backend_step = next(
        step for step in jobs["backend-fast"]["steps"] if step.get("name") == "Run backend test suite"
    )
    assert "python -m pytest tests" in backend_step["run"]
    assert "--ignore=tests/test_retrieval_query_plans.py" in backend_step["run"]
    assert "--ignore-glob='tests/test_helm_*.py'" in backend_step["run"]

    database_step = next(
        step
        for step in jobs["backend-database"]["steps"]
        if step.get("name") == "Verify query plans and migration round trip"
    )
    assert "tests/test_retrieval_query_plans.py" in database_step["run"]
    assert "verify_migration_roundtrip.py" in database_step["run"]

    helm_steps = jobs["helm-policy"]["steps"]
    actionlint_step = next(step for step in helm_steps if step.get("name") == "Lint GitHub Actions")
    assert OCI_PINNED_ACTION.fullmatch(actionlint_step["uses"])
    assert "chart_release_only" not in jobs["helm-policy"]["if"]
    assert any("tests/test_helm_*.py" in step.get("run", "") for step in helm_steps)

    assert any("npm test" in step.get("run", "") for step in jobs["extension"]["steps"])
    browser = jobs["browser"]
    assert browser["container"]["image"].startswith(
        "mcr.microsoft.com/playwright:v1.59.1-noble@sha256:"
    )
    assert not any(
        step.get("uses", "").startswith("actions/setup-node@")
        for step in browser["steps"]
    )
    browser_test_step = next(
        step for step in browser["steps"] if step.get("name") == "Run mocked Playwright suite"
    )
    assert "npm run test:e2e -- --workers=1" in browser_test_step["run"]
    assert browser_test_step["env"]["HOME"] == "/root"

    report_step = next(
        step for step in browser["steps"] if step.get("name") == "Upload Playwright report"
    )
    assert report_step["if"] == "${{ !cancelled() }}"
    assert report_step["uses"] == (
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
    )
    assert report_step["with"]["retention-days"] == "14"

    failure_step = next(
        step
        for step in browser["steps"]
        if step.get("name") == "Upload Playwright failure artifacts"
    )
    assert failure_step["if"] == "${{ failure() }}"
    assert failure_step["with"]["retention-days"] == "7"

    playwright_config = (REPO_ROOT / "frontend" / "playwright.config.ts").read_text(
        encoding="utf-8"
    )
    assert "retries: process.env.CI ? 2 : 0" in playwright_config
    assert '["html", { open: "never" }]' in playwright_config
    assert 'outputDir: process.env.CI' in playwright_config
    assert 'screenshot: "only-on-failure"' in playwright_config
    assert 'trace: "on-first-retry"' in playwright_config


def test_backend_dockerfile_uses_pinned_locked_image_targets() -> None:
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim@sha256:" in dockerfile
    assert "FROM ghcr.io/astral-sh/uv:0.11.29@sha256:" in dockerfile
    assert "AS backend-ci" in dockerfile
    assert "AS backend-runtime" in dockerfile
    assert "AS app" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "uv sync --frozen --all-groups --no-install-project" in dockerfile
    assert dockerfile.index("AS backend-ci") < dockerfile.index("apt-get install")
    assert dockerfile.index("AS backend-ci") < dockerfile.index("playwright install")

    frontend_dockerfile = (REPO_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM node:22-alpine@sha256:" in frontend_dockerfile
    # nginx-unprivileged, not the official nginx image: the pod runs as a
    # non-root user with NET_BIND_SERVICE dropped and a read-only root.
    assert "FROM nginxinc/nginx-unprivileged:alpine@sha256:" in frontend_dockerfile
    assert "RUN npm ci" in frontend_dockerfile

    # The chart sets runAsNonRoot with runAsUser 10001, so the image must
    # already own its runtime paths at that uid.
    assert "USER 10001:10001" in dockerfile
    assert "HOME=/home/palace" in dockerfile
    backend_runtime = dockerfile.split("FROM runtime-base AS backend-runtime", 1)[1].split(
        "FROM runtime-base AS worker-runtime", 1
    )[0]
    assert "git openssh-client" in backend_runtime
    assert "ffmpeg" not in backend_runtime
    assert "playwright" not in backend_runtime
    # Chromium must live outside root's 0700 cache to stay executable.
    assert "ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright" in dockerfile
    assert dockerfile.index("playwright install") < dockerfile.index("USER 10001")


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


def test_chart_publisher_reserves_every_published_oci_version_before_bump() -> None:
    publish_chart = _load_workflow()["jobs"]["publish-chart"]
    publish_step = next(
        step
        for step in publish_chart["steps"]
        if step.get("name") == "Publish chart and prepare release-coordinate PR"
    )
    script = publish_step["run"]

    assert publish_step["env"]["OCI_REGISTRY_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert publish_step["env"]["CHART_NAME"] == "${{ steps.chart.outputs.name }}"
    assert 'OCI_REPOSITORY="${REGISTRY_NAMESPACE}/${CHART_NAME}"' in script
    assert "curl --config -" in script
    assert '--user "${GITHUB_ACTOR}:${OCI_REGISTRY_TOKEN}"' not in script
    assert "python3 scripts/list_oci_tags.py" in script
    assert '--repository "$OCI_REPOSITORY"' in script
    assert "--semver-only" in script
    assert 'RESERVED_VERSION_ARGS+=(--reserved-version "$PUBLISHED_CHART_VERSION")' in script
    assert script.index("for attempt in 1 2 3 4 5") < script.index("python3 scripts/list_oci_tags.py")
    assert script.index("python3 scripts/list_oci_tags.py") < script.index(
        "python3 scripts/bump_chart_release.py"
    )
    assert script.index("python3 scripts/bump_chart_release.py") < script.index("helm push")
