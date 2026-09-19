"""Execute the real classifier; enforce the read-only host-canary boundary."""
import json
import os
import sys
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/hermes-host-compatibility.yml"


def load(path):
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize("changed", [
    "third_party_plugins/hermes/memory/palaceoftruth/provider.py",
    "third_party_plugins/hermes/memory/palaceoftruth/plugin.yaml",
])
def test_plugin_only_pr_selects_backend_fast(tmp_path, changed):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid", "commit", "--allow-empty", "-qm", "base")
    base = git("rev-parse", "HEAD")
    target = tmp_path / changed
    target.parent.mkdir(parents=True)
    target.write_text("regression fixture\n")
    git("add", ".")
    git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid", "commit", "-qm", "plugin only")
    output = tmp_path / "outputs"
    script = next(s["run"] for s in load(ROOT / ".github/workflows/build-push.yml")["jobs"]["classify"]["steps"] if s.get("id") == "changes")
    subprocess.run(["bash", "-c", script], cwd=tmp_path, check=True, timeout=10, env={
        **os.environ, "EVENT_NAME": "pull_request", "PR_BASE_SHA": base,
        "PR_HEAD_SHA": git("rev-parse", "HEAD"), "BEFORE_SHA": "", "AFTER_SHA": "",
        "GITHUB_OUTPUT": str(output),
    })
    selected = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert selected["backend_fast"] == "true"
    assert all(selected[lane] == "false" for lane in ("backend_database", "frontend", "browser", "extension", "chart_release_only"))


@pytest.mark.parametrize("existing,fail_list", [(0, False), (2, False), (20000, False), (0, True)])
def test_alert_shell_deduplicates_and_fails_closed(tmp_path, existing, fail_list):
    """Run production shell without credentials or any real GitHub client."""
    client = tmp_path / "gh"
    calls = tmp_path / "calls.jsonl"
    client.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
if "--paginate" in args:
    if os.environ["FAIL_LIST"] == "1":
        sys.exit(23)
    for number in range(int(os.environ["EXISTING"]), 0, -1):
        print(number)
elif "POST" in args:
    print(42)
elif "PATCH" not in args:
    print("https://github.invalid/issues/42")
''')
    client.chmod(0o700)
    script = load(WORKFLOW)["jobs"]["alert"]["steps"][0]["run"]
    result = subprocess.run(["/bin/bash", "-c", script], text=True, capture_output=True,
        timeout=10, env={"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path),
        "CALLS": str(calls), "EXISTING": str(existing), "FAIL_LIST": str(int(fail_list)),
        "GH_REPO": "offline/fixture", "RUN_URL": "https://github.invalid/run/123"})
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    mutations = [args for args in recorded if "--method" in args]
    if fail_list:
        assert result.returncode != 0
        assert mutations == []
        return
    assert result.returncode == 0, result.stderr
    assert len(mutations) == 1
    mutation = mutations[0]
    assert mutation[mutation.index("--method") + 1] == ("PATCH" if existing else "POST")
    target = "repos/offline/fixture/issues" + ("/1" if existing else "")
    assert target in mutation
    expected = "repos/offline/fixture/issues/" + ("1" if existing else "42")
    assert expected in recorded[-1] and "--method" not in recorded[-1]
    assert any("https://github.invalid/run/123" in arg for arg in mutation)


def test_host_workflow_policy():
    assert WORKFLOW.exists(), "Missing real-host compatibility lane"
    policy_runs = "\n".join(s.get("run", "") for s in load(ROOT / ".github/workflows/build-push.yml")["jobs"]["helm-policy"]["steps"])
    assert "tests/test_hermes_host_workflow.py" in policy_runs
    workflow = load(WORKFLOW)
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch", "pull_request"}
    assert len(workflow["on"]["schedule"]) == 1
    paths = workflow["on"]["pull_request"]["paths"]
    for path in ("third_party_plugins/hermes/memory/palaceoftruth/**", "scripts/check_hermes_host_compatibility.py", "backend/tests/test_hermes_host*.py", ".github/workflows/hermes-host-compatibility.yml", ".github/workflows/build-push.yml"):
        assert path in paths
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["compatibility"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert int(job["timeout-minutes"]) <= 20
    assert job["strategy"]["fail-fast"] == "false"
    assert job["strategy"]["matrix"]["include"] == [
        {"canary": "supported", "ref": "e83b1d51f13a08b424636f0b519db86a35aa7bd7"},
        {"canary": "upstream-main", "ref": "main"},
    ]
    steps = job["steps"]
    checkouts = [s for s in steps if s.get("uses", "").startswith("actions/checkout@")]
    assert len(checkouts) == 2
    assert all(s["with"]["persist-credentials"] == "false" for s in checkouts)
    assert checkouts[1]["with"]["repository"] == "NousResearch/hermes-agent"
    runs = "\n".join(s.get("run", "") for s in steps)
    assert 'git -C "$HOST_ROOT" rev-parse HEAD' in runs
    assert 'uv venv --python 3.12 "$HOST_VENV"' in runs
    assert 'uv pip install --python "$HOST_VENV/bin/python"' in runs
    assert '"$HOST_VENV/bin/python" scripts/check_hermes_host_compatibility.py' in runs
    assert '--hermes-root "$HOST_ROOT"' in runs
    assert '--require-gate checkpoint' not in runs, "v1 limitation must not demand SAR-1406 v2"
    assert '--context-helper-smoke' not in runs
    assert 'timeout 180s env -i' in runs
    assert 'host-sha.txt' in runs
    assert 'HERMES_COMPAT_TEST_ROOT="$HOST_ROOT"' in runs
    assert '-m pytest backend/tests/test_hermes_host_compatibility.py' in runs
    uploads = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 1 and uploads[0]["if"] == "always()"
    assert uploads[0]["with"]["retention-days"] == "14"
    assert "secrets." not in str(job)
    alert = workflow["jobs"]["alert"]
    assert alert["needs"] == "compatibility"
    assert alert["permissions"] == {"issues": "write"}
    assert alert["runs-on"] == "ubuntu-24.04"
    condition = alert["if"]
    for guard in ("always()", "needs.compatibility.result == 'failure'", "github.event_name == 'schedule'", "github.event_name == 'workflow_dispatch'", "github.event.repository.default_branch"):
        assert guard in condition
    assert not any("uses" in s for s in alert["steps"]), "Privileged alert must never check out or execute PR code"
    alert_script = alert["steps"][0]["run"]
    assert "--paginate" in alert_script
    assert "hermes-host-compatibility-alert" in alert_script
    assert "gh api" in alert_script and "PATCH" in alert_script and "POST" in alert_script
    for job in workflow["jobs"].values():
        assert int(job["timeout-minutes"]) <= 20
        for step in job["steps"]:
            if "uses" in step:
                name, sha = step["uses"].split("@")
                assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
    text = WORKFLOW.read_text()
    for forbidden in ("pull_request_target", "contents: write", "pull-requests: write", "hermes update", "gh pr merge", "--system"):
        assert forbidden not in text
