"""Host-side external single-Helm-action runner with a durable fence.

This is a HOST TOOL, not part of the backend image. It runs where an operator
has ``git``, ``helm``, ``kubectl`` and ``skopeo``, and it shells out to them
read-only for binding. The backend image does NOT need a Helm binary because
this tool is delivered separately (see ``scripts/rollout_fence.py``).

Honest guarantees — do not overstate:

- AT-MOST-ONCE (zero-or-one) action per frozen slot. The slot is consumed with
  an atomic ``openat(O_CREAT|O_EXCL)`` against a directory file descriptor, so
  two concurrent processes cannot both win and a symlink swap cannot redirect it.
  The marker is never deleted. A crash before the invocation consumes the slot
  and performs ZERO actions: the action is forfeited, never retried.
- The slot store is parent-owned and 0700, but a process running as the SAME uid
  can delete the marker and re-run. This fence is NOT adversarial protection
  against a same-uid actor; that requires a store the action uid cannot write.
- The Flux hold (``spec.suspend: true``) is a procedural, parent-owned control in
  canonical Git, not an immutable cluster fence. A Git admin (or a live resume)
  can change it. This tool never unsuspends and refuses that.
- On deadline the LOCAL client process group is killed; the SERVER-side action
  may still run. The tool then does a read-only status check and reports
  ``unknown/pending`` — it never claims cancellation.

Live read-only binding is mandatory and comes from adapters, never caller
booleans. Tests inject mock adapters and a fake helm executable; no test runs
real Helm or contacts a cluster.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CANONICAL_REPO_URL = "https://github.com/heysarver/flux-control.git"
ALLOWED_CHART_OCI = "ghcr.io/palaceoftruth/palaceoftruth/palaceoftruth"
CANONICAL_VALUES_PATHS = ("apps/k3s-lab/palace-sarvent/release.yaml",)
MIN_TIMEOUT_SECONDS = 30

HEX64 = re.compile(r"[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
GIT_REV = re.compile(r"main@sha1:[0-9a-f]{40}")
GIT_SHA1 = re.compile(r"[0-9a-f]{40}")
CHART_LAYER_MEDIA_TYPES = (
    "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
    "application/vnd.oci.image.layer.v1.tar+gzip",
)


class ControlError(RuntimeError):
    """Any refusal or binding failure. Always fail-closed."""


def canonical(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ControlError("invalid canonical data") from exc


def sha256_json(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def instant(value) -> datetime:
    if not isinstance(value, str):
        raise ControlError("timestamp missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControlError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ControlError("timezone required")
    return parsed.astimezone(timezone.utc)


def control_host_id() -> str:
    value = Path("/etc/machine-id").read_text().strip()
    if not re.fullmatch("[0-9a-f]{32}", value):
        raise ControlError("stable host identity unavailable")
    return value


def _reject_secretish(value) -> None:
    """Refuse credential-shaped FIELD NAMES in the manifest. Names only."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {"apikey", "privatekey", "credentialvalue", "token",
                              "password", "secret"} or any(
                    word in str(key).lower()
                    for word in ("secret", "password", "token", "authorization")):
                raise ControlError("credential field forbidden in action manifest")
            _reject_secretish(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secretish(child)


def slot_key(manifest: dict) -> str:
    return sha256_json({"action_id": manifest["action_id"], "phase": manifest["phase"],
                        "release_name": manifest["release_name"],
                        "namespace": manifest["namespace"]})


# --------------------------------------------------------------------------
# Read-only live adapters. Production shells out; tests inject doubles.
# --------------------------------------------------------------------------

class LiveReader:
    """Production read-only adapter (git/helm/kubectl). Never writes anything."""

    def _run(self, argv: list[str], *, timeout: int = 60) -> str:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise ControlError("read-only probe failed")
        return proc.stdout

    def canonical_repo_revision(self, url: str, reference: str) -> str:
        out = self._run(["git", "ls-remote", url, reference]).strip().splitlines()
        if len(out) != 1 or not GIT_SHA1.fullmatch(out[0].split()[0]):
            raise ControlError("canonical remote revision unreadable")
        return "main@sha1:" + out[0].split()[0]

    def working_tree_revision(self, repo_dir: str) -> str:
        head = self._run(["git", "-C", repo_dir, "rev-parse", "HEAD"]).strip()
        if not GIT_SHA1.fullmatch(head):
            raise ControlError("working tree revision unreadable")
        return "main@sha1:" + head

    def flux_suspend(self, context: str, namespace: str, release: str) -> bool:
        out = self._run(["kubectl", "--context", context, "-n", namespace, "get",
                         "helmrelease", release, "-o", "jsonpath={.spec.suspend}"]).strip()
        if out not in {"true", "false"}:
            raise ControlError("flux suspend state unreadable")
        return out == "true"

    def helm_history(self, kube_context: str, namespace: str, release: str) -> tuple[int, str]:
        out = self._run(["helm", "--kube-context", kube_context, "-n", namespace,
                         "history", release, "-o", "json"])
        try:
            rows = json.loads(out)
        except ValueError as exc:
            raise ControlError("helm history unreadable") from exc
        if not isinstance(rows, list) or not rows:
            raise ControlError("helm history empty")
        # `helm history -o json` is ordered ASCENDING, so rows[0] is the OLDEST
        # revision. Select the maximum revision instead of an index. Validate
        # strictly: reject malformed rows, bool/str/float/None revisions, and a
        # duplicated revision whose statuses disagree (ambiguous). Do not
        # coerce or normalize source values.
        statuses: dict[int, object] = {}
        for row in rows:
            if not isinstance(row, dict) or "revision" not in row:
                raise ControlError("helm history row malformed")
            revision = row["revision"]
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise ControlError("helm history revision malformed")
            status = row.get("status")
            if revision in statuses and statuses[revision] != status:
                raise ControlError("helm history duplicate revision is ambiguous")
            statuses[revision] = status
        latest = max(statuses)
        return latest, f"helm/{release}/revision/{latest}"

    def active_job(self, context: str, namespace: str, release: str) -> bool:
        out = self._run(["kubectl", "--context", context, "-n", namespace, "get", "jobs",
                         "-l", f"app.kubernetes.io/instance={release}",
                         "-o", "jsonpath={.items[*].status.active}"]).strip()
        return any(part.strip() not in {"", "0"} for part in out.split())

    def cluster_identity(self, context: str, api_origin: str) -> str:
        server = self._run(["kubectl", "--context", context, "config", "view",
                            "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"]).strip()
        if server.rstrip("/") != api_origin.rstrip("/"):
            raise ControlError("cluster API origin mismatch")
        uid = self._run(["kubectl", "--context", context, "get", "ns", "kube-system",
                         "-o", "jsonpath={.metadata.uid}"]).strip()
        if not uid:
            raise ControlError("cluster identity unreadable")
        return uid

    def release_status(self, kube_context: str, namespace: str, release: str) -> str:
        """Read-only server-side status. Never mutates; used after a deadline."""
        try:
            out = self._run(["helm", "--kube-context", kube_context, "-n", namespace,
                             "status", release, "-o", "json"])
        except (ControlError, subprocess.SubprocessError, OSError, ValueError):
            return "unknown"
        try:
            return json.loads(out).get("info", {}).get("status", "unknown")
        except ValueError:
            return "unknown"


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

REQUIRED_MANIFEST_FIELDS = {
    "schema_version", "action_id", "phase", "owner", "repo", "kube", "namespace",
    "release_name", "expected_current_revision", "chart", "image_digest", "values",
    "config_digest", "flux_suspend", "starts_at", "expires_at", "deadline_seconds",
}
REFUSED_FIELDS = ("unsuspend", "allow_unsuspend", "manual_helm", "helm_command",
                  "extra_args", "values_files", "set", "proxy", "http_proxy",
                  "print_secrets", "show_secrets", "debug",
                  "insecure_skip_tls_verify")


class ActionManifest:
    """Validated, frozen description of exactly one forward Helm action."""

    def __init__(self, doc: dict, expected_sha256: str, *, now: datetime,
                 host_id: str | None = None, uid: int | None = None):
        _reject_secretish(doc)
        self._bytes = canonical(doc)
        self.digest = hashlib.sha256(self._bytes).hexdigest()
        if not isinstance(expected_sha256, str) or not HEX64.fullmatch(expected_sha256):
            raise ControlError("manifest digest required")
        if self.digest != expected_sha256:
            raise ControlError("manifest digest mismatch")
        d = doc
        if set(d) != REQUIRED_MANIFEST_FIELDS:
            raise ControlError("manifest schema mismatch")
        if d.get("schema_version") != 1:
            raise ControlError("manifest schema version mismatch")
        if d.get("phase") != "forward":
            raise ControlError("only a forward action phase is accepted")
        for forbidden in REFUSED_FIELDS:
            if forbidden in d:
                raise ControlError(f"refused manifest field: {forbidden}")

        owner = d["owner"]
        if not isinstance(owner, dict) or set(owner) != {"host_id", "uid", "owner_id"}:
            raise ControlError("owner identity schema mismatch")
        if owner["host_id"] != (host_id if host_id is not None else control_host_id()):
            raise ControlError("owner host identity mismatch")
        if type(owner["uid"]) is not int or owner["uid"] != (os.getuid() if uid is None else uid):
            raise ControlError("owner uid mismatch")
        if not isinstance(owner["owner_id"], str) or not owner["owner_id"]:
            raise ControlError("owner id required")

        repo = d["repo"]
        if not isinstance(repo, dict) or set(repo) != {"url", "canonical_revision"}:
            raise ControlError("repo binding schema mismatch")
        if repo["url"] != CANONICAL_REPO_URL:
            raise ControlError("non-canonical repo url refused")
        if not GIT_REV.fullmatch(str(repo["canonical_revision"])):
            raise ControlError("canonical revision must be main@sha1:<40 hex>")

        kube = d["kube"]
        if not isinstance(kube, dict) or set(kube) != {"context", "cluster_uid", "api_origin"}:
            raise ControlError("kube binding schema mismatch")
        if not isinstance(kube["context"], str) or not kube["context"]:
            raise ControlError("explicit kube context required")
        if not isinstance(kube["cluster_uid"], str) or not kube["cluster_uid"]:
            raise ControlError("cluster uid required")
        if not isinstance(kube["api_origin"], str) or not kube["api_origin"].startswith("https://"):
            raise ControlError("cluster api origin must be https")

        chart = d["chart"]
        if not isinstance(chart, dict) or set(chart) != {"oci", "version", "manifest_digest", "layer_digest"}:
            raise ControlError("chart binding schema mismatch")
        if chart["oci"] != ALLOWED_CHART_OCI:
            raise ControlError("non-canonical chart oci refused")
        if not isinstance(chart["version"], str) or not chart["version"]:
            raise ControlError("chart version required")
        if not DIGEST.fullmatch(str(chart["manifest_digest"])) or not DIGEST.fullmatch(str(chart["layer_digest"])):
            raise ControlError("chart OCI and layer digests required")

        values = d["values"]
        if not isinstance(values, dict) or set(values) != {"path", "source_sha256", "digest"}:
            raise ControlError("values binding schema mismatch")
        if values["path"] not in CANONICAL_VALUES_PATHS:
            raise ControlError("values path must be an approved canonical path")
        if not HEX64.fullmatch(str(values["source_sha256"])) or not DIGEST.fullmatch(str(values["digest"])):
            raise ControlError("values digest required")
        if values["digest"] != d["config_digest"]:
            raise ControlError("config digest must equal the extracted values digest")

        for key in ("namespace", "release_name", "action_id"):
            value = d[key]
            if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
                raise ControlError(f"invalid binding: {key}")
        if not isinstance(d["expected_current_revision"], int) or d["expected_current_revision"] < 1:
            raise ControlError("expected revision must be a positive integer")
        if not DIGEST.fullmatch(str(d["image_digest"])) or not DIGEST.fullmatch(str(d["config_digest"])):
            raise ControlError("immutable image/config digest required")
        if d["flux_suspend"] is not True:
            raise ControlError("flux hold must stay suspended for this action")
        if type(d["deadline_seconds"]) is not int or not MIN_TIMEOUT_SECONDS <= d["deadline_seconds"] <= 3600:
            raise ControlError("bounded deadline required")
        start, end = instant(d["starts_at"]), instant(d["expires_at"])
        if not start < end <= start + timedelta(seconds=3600):
            raise ControlError("bounded active window required")
        self.slot = slot_key(d)
        self.active(now)

    @property
    def doc(self) -> dict:
        return json.loads(self._bytes)

    def active(self, now: datetime) -> None:
        if not instant(self.doc["starts_at"]) <= now < instant(self.doc["expires_at"]):
            raise ControlError("manifest not active")

    @classmethod
    def load(cls, path, expected_sha256, *, now, host_id=None, uid=None):
        try:
            doc = json.loads(Path(path).read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ControlError("manifest unreadable") from exc
        return cls(doc, expected_sha256, now=now, host_id=host_id, uid=uid)

    def timeout_seconds(self) -> int:
        return max(MIN_TIMEOUT_SECONDS, self.doc["deadline_seconds"] - 1)

    def helm_argv(self, chart_dir: str, values_file: str, *, binary: str = "helm") -> list[str]:
        d = self.doc
        # Local digest-verified chart, explicit context, explicit values with
        # --reset-values so applied values are chart defaults + the approved
        # file only, never a silent reuse of previous release values.
        return [
            binary, "--kube-context", d["kube"]["context"],
            "upgrade", d["release_name"], chart_dir,
            "--namespace", d["namespace"],
            "--reset-values", "--values", values_file,
            "--install=false", "--atomic=false", "--wait",
            "--timeout", f"{self.timeout_seconds()}s",
        ]


# --------------------------------------------------------------------------
# Durable fence store (dirfd-based, TOCTOU-free)
# --------------------------------------------------------------------------

class SlotStore:
    def __init__(self, root):
        self.root = Path(root)

    def _dirfd(self) -> int:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.root.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ControlError("unsafe store path")
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ControlError("store must be 0700 and owner-only")
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def consume(self, slot: str, binding: dict) -> None:
        dirfd = self._dirfd()
        try:
            try:
                fd = os.open(f"{slot}.consumed",
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o600, dir_fd=dirfd)
            except FileExistsError as exc:
                raise ControlError("slot already consumed") from exc
            except OSError as exc:
                raise ControlError("slot reservation failed") from exc
            try:
                os.write(fd, canonical(binding))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(dirfd)
        finally:
            os.close(dirfd)

    def consumed(self, slot: str) -> bool:
        return (self.root / f"{slot}.consumed").exists()


# --------------------------------------------------------------------------
# Artifact + values verification
# --------------------------------------------------------------------------

def _default_runner(argv: list[str], *, timeout: int = 120) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ControlError(f"artifact step failed: {argv[0]}")
    return proc.stdout


def _safe_extract_tar(path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = str(dest.resolve())
    with tarfile.open(path) as tf:
        for member in tf.getmembers():
            target = str((dest / member.name).resolve())
            # Require a real path separator so a sibling dir sharing the prefix
            # (…/extract-evil) can never pass the check.
            if target != root and not target.startswith(root + os.sep):
                raise ControlError("unsafe tar member path")
            if member.issym() or member.islnk() or member.isdev():
                raise ControlError("unsafe tar member type")
        tf.extractall(dest, filter="data")


def _select_chart_layer(outdir: Path, chart: dict) -> bytes:
    """Select the chart layer from the OCI manifest by mediaType descriptor.

    Never by sorted blob filename. Missing or duplicate chart layers fail closed.
    """
    manifest_path = outdir / "manifest.json"
    if not manifest_path.exists():
        raise ControlError("OCI manifest.json missing")
    manifest_bytes = manifest_path.read_bytes()
    if "sha256:" + hashlib.sha256(manifest_bytes).hexdigest() != chart["manifest_digest"]:
        raise ControlError("OCI manifest digest mismatch")
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise ControlError("OCI manifest unreadable") from exc
    layers = manifest.get("layers") or []
    selected = [layer for layer in layers if layer.get("mediaType") in CHART_LAYER_MEDIA_TYPES]
    if len(selected) != 1:
        raise ControlError("chart layer descriptor missing or duplicated")
    descriptor_digest = selected[0].get("digest")
    blobs = {}
    for candidate in outdir.iterdir():
        if candidate.is_file():
            blobs["sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()] = candidate
    if descriptor_digest not in blobs:
        raise ControlError("chart layer blob absent for descriptor digest")
    if descriptor_digest != chart["layer_digest"]:
        raise ControlError("chart layer digest mismatch")
    if descriptor_digest == chart["manifest_digest"]:
        raise ControlError("manifest digest must differ from layer tar digest")
    return blobs[descriptor_digest].read_bytes()


def verify_oci_chart(chart: dict, workdir: str, *, runner=_default_runner) -> str:
    """Pull the chart by OCI digest, verify digests, safely extract, return dir.

    Distinguishes the OCI manifest digest from the chart layer tar digest.
    """
    if shutil.which("skopeo") is None and runner is _default_runner:
        raise ControlError("skopeo required to pull OCI chart by digest")
    ref = f"{chart['oci']}@{chart['manifest_digest']}"
    outdir = Path(workdir) / "chart-oci"
    outdir.mkdir(parents=True, exist_ok=True)
    runner(["skopeo", "copy", f"docker://{ref}", f"dir:{outdir}"])
    layer_bytes = _select_chart_layer(outdir, chart)
    layer = outdir / "chart-layer.tar"
    layer.write_bytes(layer_bytes)
    extract = Path(workdir) / "chart-extracted"
    _safe_extract_tar(layer, extract)
    # Root Chart.yaml only: the chart root, or one directory down. Never a
    # nested dependency Chart.yaml under charts/.
    candidates = [extract / "Chart.yaml"] + [p / "Chart.yaml" for p in extract.iterdir() if p.is_dir()]
    chart_yaml = next((c for c in candidates if c.exists()), None)
    if chart_yaml is None:
        raise ControlError("chart metadata missing after extract")
    try:
        meta = __import__("yaml").safe_load(chart_yaml.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse failure is fail-closed
        raise ControlError("chart metadata unreadable") from exc
    if not isinstance(meta, dict) or str(meta.get("version")) != chart["version"]:
        raise ControlError("extracted chart version does not match manifest")
    return str(chart_yaml.parent)


def extract_gitops_values(path: str) -> tuple[str, str]:
    """Return (original_git_file_sha256, canonical_values_yaml).

    Parses the HelmRelease and extracts ``spec.values``. The raw HelmRelease is
    NEVER used as Helm values; the extracted mapping is re-emitted as
    deterministic canonical YAML.
    """
    text = Path(path).read_text("utf-8")
    original_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        doc = __import__("yaml").safe_load(text)
    except Exception as exc:  # noqa: BLE001
        raise ControlError("HelmRelease unreadable") from exc
    if not isinstance(doc, dict) or doc.get("kind") != "HelmRelease":
        raise ControlError("approved values source must be a HelmRelease")
    spec = doc.get("spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("values"), dict):
        raise ControlError("HelmRelease spec.values missing")
    canonical_values = __import__("yaml").safe_dump(spec["values"], sort_keys=True, default_flow_style=False)
    return original_sha256, canonical_values


def read_gitops_values(path: str, binding: dict) -> str:
    """Verify the source hash and return the extracted canonical values YAML."""
    original_sha256, canonical_values = extract_gitops_values(path)
    if original_sha256 != binding["source_sha256"]:
        raise ControlError("approved GitOps source hash mismatch")
    digest = "sha256:" + hashlib.sha256(canonical_values.encode()).hexdigest()
    if digest != binding["digest"]:
        raise ControlError("extracted values digest mismatch")
    return canonical_values


def render_expected_image_digests(chart_dir: str, values_file: str, doc: dict,
                                  *, runner=_default_runner) -> dict:
    out = runner(["helm", "template", doc["release_name"], chart_dir,
                  "--namespace", doc["namespace"], "--values", values_file])
    import yaml
    digests = {}
    for loaded in yaml.safe_load_all(out):
        if not isinstance(loaded, dict):
            continue
        spec = loaded.get("spec", {}) or {}
        pod = spec.get("template", {}) or {}
        containers = pod.get("spec", {}).get("containers", []) or []
        for container in containers:
            image = container.get("image", "")
            if "@sha256:" in image:
                digests[f"{loaded.get('kind')}/{loaded.get('metadata', {}).get('name')}/{container.get('name')}"] = image.split("@")[-1]
    return digests


# --------------------------------------------------------------------------
# Invoker (bounded, private output)
# --------------------------------------------------------------------------

def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def default_invoker(argv: list[str], *, deadline_seconds: int,
                    output_path: str | None = None) -> dict:
    """Run the action in its own process group with one hard deadline.

    Child stdout/stderr go to /dev/null or a private, 0600 file. They are NEVER
    returned to the caller and NEVER substring-scanned as a security control.
    """
    if output_path:
        fd = os.open(output_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        sink = os.fdopen(fd, "wb")
    else:
        sink = open(os.devnull, "wb")
    try:
        proc = subprocess.Popen(argv, stdout=sink, stderr=sink, start_new_session=True)
        try:
            proc.wait(timeout=deadline_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            raise ControlError("helm action exceeded deadline")
        return {"returncode": proc.returncode}
    finally:
        sink.close()


def run_action(manifest: ActionManifest, reader: LiveReader, *, store: SlotStore,
               invoker=default_invoker, now: datetime, workdir: str, repo_dir: str,
               server_reader: LiveReader | None = None,
               chart_runner=_default_runner) -> dict:
    manifest.active(now)
    d = manifest.doc
    server = server_reader or reader

    # --- Live, read-only, fail-closed binding. Caller booleans are NOT used. ---
    if reader.canonical_repo_revision(d["repo"]["url"], "main") != d["repo"]["canonical_revision"]:
        raise ControlError("canonical remote revision changed")
    if reader.working_tree_revision(repo_dir) != d["repo"]["canonical_revision"]:
        raise ControlError("working tree does not match canonical revision")
    if reader.cluster_identity(d["kube"]["context"], d["kube"]["api_origin"]) != d["kube"]["cluster_uid"]:
        raise ControlError("cluster identity mismatch")
    if reader.flux_suspend(d["kube"]["context"], d["namespace"], d["release_name"]) is not True:
        raise ControlError("flux hold not suspended")
    revision, pointer = reader.helm_history(d["kube"]["context"], d["namespace"], d["release_name"])
    if revision != d["expected_current_revision"]:
        raise ControlError("release revision does not match expected")
    if reader.active_job(d["kube"]["context"], d["namespace"], d["release_name"]):
        raise ControlError("active rollout job present")

    # --- Artifact + values verification BEFORE consumption. ---
    chart_dir = verify_oci_chart(d["chart"], workdir, runner=chart_runner)
    values_text = read_gitops_values(d["values"]["path"], d["values"])
    values_file = Path(workdir) / "values.yaml"
    fd = os.open(values_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(values_text)
    image_digests = render_expected_image_digests(chart_dir, str(values_file), d, runner=chart_runner)
    if d["image_digest"] not in set(image_digests.values()):
        raise ControlError("rendered image digest does not match manifest")

    # --- Consume the slot exactly once, atomically, before running anything. ---
    store.consume(manifest.slot, {"manifest": manifest.digest, "action_id": d["action_id"],
                                  "release": d["release_name"], "namespace": d["namespace"],
                                  "expected_revision": d["expected_current_revision"]})

    # From here the slot is permanently consumed. Failure is terminal: the
    # action is forfeited, never retried (at-most-once).
    try:
        result = invoker(manifest.helm_argv(chart_dir, str(values_file)),
                         deadline_seconds=d["deadline_seconds"])
    except ControlError as exc:
        if "exceeded deadline" in str(exc):
            status = server.release_status(d["kube"]["context"], d["namespace"], d["release_name"])
            raise ControlError(f"deadline exceeded; server status unknown/pending ({status})") from exc
        raise
    if result.get("returncode") != 0:
        raise ControlError("helm action failed")
    return {"action_id": d["action_id"], "slot": manifest.slot, "returncode": 0}


# --------------------------------------------------------------------------
# CLI — the real production entry point
# --------------------------------------------------------------------------

def _cmd_help() -> int:
    print("rollout-fence: host-side single Helm action runner (at-most-once).")
    print("subcommands: help | dry-run | run")
    return 0


def _build_manifest(args) -> ActionManifest:
    if not args.manifest or not args.manifest_sha256:
        raise ControlError("--manifest and --manifest-sha256 are required")
    return ActionManifest.load(args.manifest, args.manifest_sha256,
                               now=datetime.now(timezone.utc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rollout-fence", add_help=False)
    parser.add_argument("subcommand", nargs="?", default="help")
    parser.add_argument("--manifest")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--store")
    parser.add_argument("--workdir")
    parser.add_argument("--repo-dir")
    args = parser.parse_args(argv)

    if args.subcommand in (None, "help"):
        return _cmd_help()
    try:
        if args.subcommand == "dry-run":
            manifest = _build_manifest(args)
            print(json.dumps({"action_id": manifest.doc["action_id"], "slot": manifest.slot,
                              "deadline_seconds": manifest.doc["deadline_seconds"]}))
            return 0
        if args.subcommand == "run":
            if not args.store or not args.workdir or not args.repo_dir:
                raise ControlError("--store, --workdir and --repo-dir are required")
            manifest = _build_manifest(args)
            result = run_action(manifest, LiveReader(), store=SlotStore(args.store),
                                now=datetime.now(timezone.utc), workdir=args.workdir,
                                repo_dir=args.repo_dir)
            print(json.dumps(result))
            return 0
        raise ControlError(f"unknown subcommand: {args.subcommand}")
    except ControlError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3


__all__ = ["ControlError", "ActionManifest", "SlotStore", "LiveReader", "run_action",
           "default_invoker", "slot_key", "control_host_id", "sha256_json", "instant",
           "verify_oci_chart", "extract_gitops_values", "read_gitops_values",
           "render_expected_image_digests", "canonical", "main",
           "CANONICAL_REPO_URL", "ALLOWED_CHART_OCI", "GIT_SHA1"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
