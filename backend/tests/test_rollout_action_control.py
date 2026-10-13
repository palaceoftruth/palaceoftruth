"""Tests for the host-side single Helm action fence.

No production call: adapters are mock objects, the chart/values artifacts are
synthetic, and the helm invoker is a fake local process. No cluster, no real
Helm, no environment read.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve().parent.parent / "scripts/rollout_action_control.py"
spec = importlib.util.spec_from_file_location("rollout_action_control", SCRIPT)
g = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(g)

NOW = datetime(2026, 10, 13, 5, 0, 0, tzinfo=timezone.utc)
REV = "main@sha1:" + "a" * 40
HOST = g.control_host_id()
IMAGE_DIGEST = "sha256:" + "1" * 64
VALUES_REL = "apps/k3s-lab/palace-sarvent/release.yaml"


def _chart_tar(version="0.1.610"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = f"apiVersion: v2\nname: palaceoftruth\nversion: {version}\n".encode()
        info = tarfile.TarInfo("palaceoftruth/Chart.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeChartRunner:
    """Fake skopeo/helm: writes the synthetic chart tar and renders one image."""

    def __init__(self, tar_bytes, version="0.1.610", image=IMAGE_DIGEST):
        self.tar_bytes = tar_bytes
        self.version = version
        self.image = image

    def __call__(self, argv, *, timeout=120):
        if argv[0] == "skopeo":
            outdir = Path(argv[-1].split(":", 1)[1])
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "chart.tar").write_bytes(self.tar_bytes)
            return ""
        if argv[0] == "helm" and argv[1] == "template":
            return (
                "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: palaceoftruth-backend\n"
                "spec:\n  template:\n    spec:\n      containers:\n"
                f"      - name: backend\n        image: ghcr.io/palaceoftruth/palaceoftruth/backend@{self.image}\n"
            )
        raise AssertionError(f"unexpected runner argv: {argv}")


class MockReader:
    def __init__(self, **over):
        base = dict(repo_revision=REV, working_revision=REV, suspend=True, revision=474,
                    active=False, cluster_uid="cluster-uid-1", status="pending-upgrade")
        base.update(over)
        self.__dict__.update(base)
        self.calls = []

    def canonical_repo_revision(self, url, reference):
        self.calls.append(("canonical", url, reference))
        return self.repo_revision

    def working_tree_revision(self, repo_dir):
        self.calls.append(("working", repo_dir))
        return self.working_revision

    def cluster_identity(self, context, api_origin):
        self.calls.append(("cluster", context, api_origin))
        return self.cluster_uid

    def flux_suspend(self, context, namespace, release):
        self.calls.append(("suspend",))
        return self.suspend

    def helm_history(self, kube_context, namespace, release):
        self.calls.append(("history",))
        return self.revision, f"helm/{release}/revision/{self.revision}"

    def active_job(self, context, namespace, release):
        self.calls.append(("job",))
        return self.active

    def release_status(self, kube_context, namespace, release):
        self.calls.append(("status",))
        return self.status


def _make_env(tmp_path, **over):
    values_path = tmp_path / VALUES_REL
    values_path.parent.mkdir(parents=True, exist_ok=True)
    values_text = "spec:\n  values:\n    image:\n      backendDigest: " + IMAGE_DIGEST + "\n"
    values_path.write_text(values_text)
    tar = _chart_tar()
    doc = {
        "schema_version": 1,
        "action_id": "sar1402-forward-01",
        "phase": "forward",
        "owner": {"host_id": HOST, "uid": os.getuid(), "owner_id": "barbara"},
        "repo": {"url": g.CANONICAL_REPO_URL, "canonical_revision": REV},
        "kube": {"context": "k3s-lab", "cluster_uid": "cluster-uid-1", "api_origin": "https://10.13.37.50"},
        "namespace": "palace-sarvent",
        "release_name": "palace-sarvent",
        "expected_current_revision": 474,
        "chart": {"oci": g.ALLOWED_CHART_OCI, "version": "0.1.610",
                  "manifest_digest": "sha256:" + "9" * 64,
                  "layer_digest": "sha256:" + hashlib.sha256(tar).hexdigest()},
        "image_digest": IMAGE_DIGEST,
        "values": {"path": VALUES_REL,
                   "blob_sha256": hashlib.sha256(values_text.encode()).hexdigest(),
                   "digest": "sha256:" + hashlib.sha256(values_text.encode()).hexdigest()},
        "config_digest": "sha256:" + "3" * 64,
        "flux_suspend": True,
        "starts_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        "deadline_seconds": 30,
    }
    doc.update(over)
    return doc, g.sha256_json(doc), FakeChartRunner(tar)


def _manifest(tmp_path, **over):
    doc, digest, chart = _make_env(tmp_path, **over)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(doc))
    return g.ActionManifest.load(str(path), digest, now=NOW), chart


def _ok_invoker(argv, *, deadline_seconds):
    assert argv[0] == "helm"
    return {"returncode": 0}


@pytest.fixture
def repo_dir(tmp_path):
    d = tmp_path / "flux-control"
    d.mkdir()
    return str(d)


def _chdir_canonical(monkeypatch, tmp_path):
    """read_gitops_values uses a repo-relative canonical path; run inside tmp."""
    monkeypatch.chdir(tmp_path)


# --- refusal surface -------------------------------------------------------

@pytest.mark.parametrize("forbidden", [
    {"unsuspend": True}, {"manual_helm": "helm upgrade x"},
    {"extra_args": ["--set", "a=b"]}, {"values_files": ["v.yaml"]},
    {"proxy": "http://p:8080"}, {"print_secrets": True}, {"debug": True},
    {"insecure_skip_tls_verify": True},
])
def test_unsafe_caller_surfaces_are_refused(tmp_path, forbidden):
    doc, digest, _ = _make_env(tmp_path, **forbidden)
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(g.ControlError):
        g.ActionManifest.load(str(path), digest, now=NOW)


def test_flux_must_stay_suspended(tmp_path):
    doc, digest, _ = _make_env(tmp_path, flux_suspend=False)
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(g.ControlError):
        g.ActionManifest.load(str(path), digest, now=NOW)


def test_digest_mismatch_refused(tmp_path):
    doc, _, _ = _make_env(tmp_path)
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(g.ControlError):
        g.ActionManifest.load(str(path), "b" * 64, now=NOW)


def test_wrong_phase_refused(tmp_path):
    doc, digest, _ = _make_env(tmp_path, phase="rollback")
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(g.ControlError):
        g.ActionManifest.load(str(path), digest, now=NOW)


def test_credential_field_refused(tmp_path):
    doc, _, _ = _make_env(tmp_path)
    doc["owner"]["token"] = "x"
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(g.ControlError):
        g.ActionManifest.load(str(path), g.sha256_json(doc), now=NOW)


# --- INVERSE safety: each review defect must now be ABSENT ------------------

def test_inverse_D1_values_are_pinned(tmp_path):
    m, _ = _manifest(tmp_path)
    argv = m.helm_argv("/chart", "/values.yaml")
    assert "--reset-values" in argv
    assert "--values" in argv and "/values.yaml" in argv


def test_inverse_D2_immutable_digests_enforced():
    src = SCRIPT.read_text()
    assert "skopeo" in src and "verify_oci_chart" in src
    assert "manifest_digest" in src and "layer_digest" in src


def test_inverse_D3_non_canonical_repo_refused(tmp_path):
    doc, _, _ = _make_env(tmp_path)
    doc["repo"] = {"url": "https://attacker.example/evil.git", "canonical_revision": REV}
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(g.ControlError):
        g.ActionManifest.load(str(path), g.sha256_json(doc), now=NOW)


def test_inverse_D4_cluster_context_is_bound(tmp_path):
    m, _ = _manifest(tmp_path)
    argv = m.helm_argv("/chart", "/values.yaml")
    assert "--kube-context" in argv and "k3s-lab" in argv


def test_inverse_D5_timeout_reports_server_status(tmp_path, repo_dir, monkeypatch):
    _chdir_canonical(monkeypatch, tmp_path)
    m, chart = _manifest(tmp_path)
    store = g.SlotStore(tmp_path / "store")
    script = tmp_path / "hang.sh"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o700)
    reader = MockReader(status="pending-upgrade")

    def hanging(argv, *, deadline_seconds):
        return g.default_invoker([str(script)], deadline_seconds=1)

    with pytest.raises(g.ControlError) as ei:
        g.run_action(m, reader, store=store, invoker=hanging, now=NOW,
                     workdir=str(tmp_path / "wd"), repo_dir=repo_dir, chart_runner=chart)
    assert "unknown/pending" in str(ei.value)
    assert ("status",) in reader.calls
    assert "cancel" not in str(ei.value).lower()
    assert store.consumed(m.slot)


def test_inverse_D6_at_most_once_wording():
    src = SCRIPT.read_text()
    assert "AT-MOST-ONCE" in src.upper()
    assert "Exactly one action per frozen slot" not in src


def test_inverse_D7_output_is_private_and_not_scanned():
    src = SCRIPT.read_text()
    assert "_scan_output" not in src
    assert "os.devnull" in src
    assert "O_NOFOLLOW" in src and "0o600" in src


def test_inverse_D8_same_uid_limit_is_documented():
    src = SCRIPT.read_text().lower()
    assert "same uid" in src


def test_inverse_D9_deadline_is_floored(tmp_path):
    m, _ = _manifest(tmp_path, deadline_seconds=30)
    argv = m.helm_argv("/chart", "/values.yaml")
    # The floor keeps --timeout at the minimum (never the invalid "0s").
    assert argv[argv.index("--timeout") + 1] == "30s"
    m2, _ = _manifest(tmp_path, deadline_seconds=300)
    argv2 = m2.helm_argv("/chart", "/values.yaml")
    assert argv2[argv2.index("--timeout") + 1] == "299s"
    doc, digest, _ = _make_env(tmp_path, deadline_seconds=1,
                               expires_at=(NOW + timedelta(seconds=2)).isoformat())
    path = tmp_path / "short.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(g.ControlError):
        g.ActionManifest.load(str(path), digest, now=NOW)


# --- live binding ------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"repo_revision": "main@sha1:" + "b" * 40},
    {"working_revision": "main@sha1:" + "c" * 40},
    {"suspend": False}, {"revision": 475}, {"active": True}, {"cluster_uid": "other"},
])
def test_live_binding_mismatch_refuses_before_consume(tmp_path, repo_dir, monkeypatch, bad):
    _chdir_canonical(monkeypatch, tmp_path)
    m, chart = _manifest(tmp_path)
    store = g.SlotStore(tmp_path / "store")
    with pytest.raises(g.ControlError):
        g.run_action(m, MockReader(**bad), store=store, invoker=_ok_invoker, now=NOW,
                     workdir=str(tmp_path / "wd"), repo_dir=repo_dir, chart_runner=chart)
    assert not store.consumed(m.slot)


def test_happy_path_consumes_once(tmp_path, repo_dir, monkeypatch):
    _chdir_canonical(monkeypatch, tmp_path)
    m, chart = _manifest(tmp_path)
    store = g.SlotStore(tmp_path / "store")
    result = g.run_action(m, MockReader(), store=store, invoker=_ok_invoker, now=NOW,
                          workdir=str(tmp_path / "wd"), repo_dir=repo_dir, chart_runner=chart)
    assert result["slot"] == m.slot and store.consumed(m.slot)


# --- at-most-once, crash, timeout -------------------------------------------

def test_second_run_refused(tmp_path, repo_dir, monkeypatch):
    _chdir_canonical(monkeypatch, tmp_path)
    m, chart = _manifest(tmp_path)
    store = g.SlotStore(tmp_path / "store")
    g.run_action(m, MockReader(), store=store, invoker=_ok_invoker, now=NOW,
                 workdir=str(tmp_path / "wd"), repo_dir=repo_dir, chart_runner=chart)
    with pytest.raises(g.ControlError):
        g.run_action(m, MockReader(), store=store, invoker=_ok_invoker, now=NOW,
                     workdir=str(tmp_path / "wd"), repo_dir=repo_dir, chart_runner=chart)


def test_crash_after_consumption_no_second_invocation(tmp_path, repo_dir, monkeypatch):
    _chdir_canonical(monkeypatch, tmp_path)
    m, chart = _manifest(tmp_path)
    store = g.SlotStore(tmp_path / "store")

    def crashing(argv, *, deadline_seconds):
        raise g.ControlError("crash before helm ran")

    with pytest.raises(g.ControlError):
        g.run_action(m, MockReader(), store=store, invoker=crashing, now=NOW,
                     workdir=str(tmp_path / "wd"), repo_dir=repo_dir, chart_runner=chart)
    # At-most-once: the crash may or may not have started the action, but the
    # slot is consumed and no SECOND invocation is ever allowed.
    assert store.consumed(m.slot)
    with pytest.raises(g.ControlError):
        g.run_action(m, MockReader(), store=store, invoker=_ok_invoker, now=NOW,
                     workdir=str(tmp_path / "wd"), repo_dir=repo_dir, chart_runner=chart)


def test_expired_manifest_refused(tmp_path, repo_dir, monkeypatch):
    _chdir_canonical(monkeypatch, tmp_path)
    m, chart = _manifest(tmp_path)
    with pytest.raises(g.ControlError):
        g.run_action(m, MockReader(), store=g.SlotStore(tmp_path / "s"), invoker=_ok_invoker,
                     now=NOW + timedelta(hours=2), workdir=str(tmp_path / "wd"),
                     repo_dir=repo_dir, chart_runner=chart)


# --- OCI tamper ---------------------------------------------------------------

def test_oci_layer_digest_tamper_refused(tmp_path):
    doc, digest, chart = _make_env(tmp_path)
    chart.tar_bytes = _chart_tar(version="9.9.9")
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc))
    manifest = g.ActionManifest.load(str(path), digest, now=NOW)
    with pytest.raises(g.ControlError):
        g.verify_oci_chart(manifest.doc["chart"], str(tmp_path / "wd"), runner=chart)


def test_oci_version_mismatch_refused(tmp_path):
    doc, digest, chart = _make_env(tmp_path)
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc))
    manifest = g.ActionManifest.load(str(path), digest, now=NOW)
    chart.tar_bytes = _chart_tar(version="0.1.610")
    with pytest.raises(g.ControlError):
        g.verify_oci_chart(dict(manifest.doc["chart"], version="0.1.611"),
                           str(tmp_path / "wd"), runner=chart)


# --- store hardening + concurrency ------------------------------------------

def test_store_rejects_world_readable(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    os.chmod(root, 0o755)
    with pytest.raises(g.ControlError):
        g.SlotStore(root).consume("a" * 64, {"x": 1})


def test_concurrent_processes_consume_exactly_once(tmp_path):
    store_dir = tmp_path / "store"
    slot = "s" * 64
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(f'''
        import importlib.util
        spec = importlib.util.spec_from_file_location("rac", {str(SCRIPT)!r})
        g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        try:
            g.SlotStore({str(store_dir)!r}).consume({slot!r}, {{"probe": 1}})
            print("WON")
        except g.ControlError:
            print("LOST")
    '''))
    procs = [subprocess.Popen([sys.executable, str(worker)], stdout=subprocess.PIPE, text=True)
             for _ in range(8)]
    outs = [p.communicate()[0].strip() for p in procs]
    assert outs.count("WON") == 1, outs


# --- CLI ---------------------------------------------------------------------

def _cli_env(tmp_path):
    # Use the real current time so the CLI window is active.
    real = datetime.now(timezone.utc)
    doc, digest, chart = _make_env(
        tmp_path,
        starts_at=(real - timedelta(minutes=1)).isoformat(),
        expires_at=(real + timedelta(minutes=30)).isoformat())
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(doc))
    return doc, digest, chart, str(path)


def test_cli_help():
    r = subprocess.run([sys.executable, str(SCRIPT), "help"], capture_output=True, text=True)
    assert r.returncode == 0 and "subcommands" in r.stdout


def test_cli_dry_run(tmp_path):
    doc, digest, chart, path = _cli_env(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPT), "dry-run",
                        "--manifest", path, "--manifest-sha256", digest],
                       capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["slot"]


def test_cli_run_requires_args():
    r = subprocess.run([sys.executable, str(SCRIPT), "run"], capture_output=True, text=True)
    assert r.returncode == 3 and "required" in r.stderr


def test_host_entrypoint_runs():
    entry = ROOT / "scripts/rollout_fence.py"
    r = subprocess.run([sys.executable, str(entry), "help"], capture_output=True, text=True)
    assert r.returncode == 0 and "rollout-fence" in r.stdout


# --- secret-shaped output ----------------------------------------------------

def test_child_output_never_returned(tmp_path):
    script = tmp_path / "leaky.sh"
    script.write_text("#!/bin/sh\necho 'api_key=abc123' >&2\nexit 0\n")
    script.chmod(0o700)
    out = tmp_path / "private.log"
    result = g.default_invoker([str(script)], deadline_seconds=5, output_path=str(out))
    assert result == {"returncode": 0}
    assert out.stat().st_mode & 0o777 == 0o600
