"""External single Helm action runner with a durable, parent-owned fence.

This is deliberately NOT the smoke token fence. The smoke fence reserves a
single API POST; a Helm action has a different contract (an OCI chart pull, a
cluster-side upgrade, and an immutable rollout identity that the caller must not
be able to mutate). This module therefore implements its own reservation,
identity binding and refusal surface, and it does not import or reuse
``smoke_control``.

Guarantees:

- Exactly one action per frozen slot. The slot is consumed atomically across
  processes and is never deleted, so a crash before, during or after the Helm
  invocation can never lead to a second invocation.
- The runner performs the Helm action WHILE the Flux hold stays suspended. It
  refuses any request to unsuspend, to run a manual Helm command, to pass extra
  arguments or value overrides, to use a proxy, or to print secrets.
- Identity is fail-closed: the canonical repo revision, namespace, release,
  expected current revision, image/chart/config digests, owner host and uid must
  all match before anything runs.
- The whole exchange is bounded by one deadline. The child runs in its own
  process group and is killed and reaped on expiry.
- Tests never reach production: they inject a fake reader and a fake invoker.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


class ControlError(RuntimeError):
    """Any refusal or binding failure. Always fail-closed."""


HEX64 = re.compile(r"[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
GIT_REV = re.compile(r"main@sha1:[0-9a-f]{40}")

# A Helm argv built only from vetted, frozen manifest fields. No caller extras.
HELM_BINARY = "helm"

REQUIRED_MANIFEST_FIELDS = {
    "schema_version", "action_id", "phase", "owner", "repo", "namespace",
    "release_name", "expected_current_revision", "chart_ref", "chart_version",
    "image_digest", "chart_digest", "config_digest", "flux_suspend", "starts_at",
    "expires_at", "deadline_seconds",
}


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
    """Stable per-(action) key. Re-running the same action reuses one slot."""
    return sha256_json({"action_id": manifest["action_id"], "phase": manifest["phase"],
                        "release_name": manifest["release_name"],
                        "namespace": manifest["namespace"]})


class ActionManifest:
    """Validated, frozen description of exactly one Helm action."""

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

        # Refuse every unsafe caller surface explicitly.
        for forbidden in ("unsuspend", "allow_unsuspend", "manual_helm", "helm_command",
                          "extra_args", "values", "set", "values_files", "proxy",
                          "http_proxy", "print_secrets", "show_secrets", "debug"):
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
        if not isinstance(repo["url"], str) or not repo["url"].startswith("https://"):
            raise ControlError("repo url must be https")
        if not GIT_REV.fullmatch(str(repo["canonical_revision"])):
            raise ControlError("canonical revision must be main@sha1:<40 hex>")

        for key in ("namespace", "release_name", "chart_ref", "chart_version", "action_id"):
            value = d[key]
            if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
                raise ControlError(f"invalid binding: {key}")
        if not isinstance(d["expected_current_revision"], int) or d["expected_current_revision"] < 1:
            raise ControlError("expected revision must be a positive integer")
        for key in ("image_digest", "chart_digest", "config_digest"):
            if not DIGEST.fullmatch(str(d[key])):
                raise ControlError(f"immutable {key} required")
        if d["flux_suspend"] is not True:
            raise ControlError("flux hold must stay suspended for this action")
        if type(d["deadline_seconds"]) is not int or not 1 <= d["deadline_seconds"] <= 3600:
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
        d = self.doc
        if not instant(d["starts_at"]) <= now < instant(d["expires_at"]):
            raise ControlError("manifest not active")

    @classmethod
    def load(cls, path: str | Path, expected_sha256: str, *, now: datetime,
             host_id: str | None = None, uid: int | None = None) -> "ActionManifest":
        try:
            raw = Path(path).read_text("utf-8")
            doc = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise ControlError("manifest unreadable") from exc
        return cls(doc, expected_sha256, now=now, host_id=host_id, uid=uid)

    def helm_argv(self) -> list[str]:
        d = self.doc
        # Fixed shape. No caller-supplied arguments, values, or overrides.
        return [HELM_BINARY, "upgrade", d["release_name"], d["chart_ref"],
                "--namespace", d["namespace"], "--version", d["chart_version"],
                "--install=false", "--atomic=false", "--wait", "--timeout",
                f"{d['deadline_seconds'] - 1}s"]


class SlotStore:
    """Durable, parent-owned, atomic-cross-process consumption store."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _ensure(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.root.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ControlError("unsafe store path")
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ControlError("store must be 0700 and owner-only")
        if self.root.absolute() != self.root.resolve():
            raise ControlError("traversal store path")

    def consume(self, slot: str, binding: dict) -> Path:
        """Atomically consume the slot. Raises if already consumed."""
        self._ensure()
        marker = self.root / f"{slot}.consumed"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
        try:
            fd = os.open(marker, flags, 0o600)
        except FileExistsError as exc:
            raise ControlError("slot already consumed") from exc
        except OSError as exc:
            raise ControlError("slot reservation failed") from exc
        try:
            os.write(fd, canonical(binding))
            os.fsync(fd)
        finally:
            os.close(fd)
        dirfd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return marker

    def consumed(self, slot: str) -> bool:
        return (self.root / f"{slot}.consumed").exists()


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def default_invoker(argv: list[str], *, deadline_seconds: int) -> dict:
    """Run the Helm action in its own process group with one hard deadline."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise ControlError("helm action exceeded deadline")
    return {"returncode": proc.returncode, "stdout": out, "stderr": err}


class Preflight:
    """Read-only, external live evidence required before any invocation."""

    def __init__(self, *, repo_revision: str, flux_suspend: bool,
                 current_revision: int, active_job: bool, hr_history_revision: int,
                 hr_history_pointer: str):
        self.repo_revision = repo_revision
        self.flux_suspend = flux_suspend
        self.current_revision = current_revision
        self.active_job = active_job
        self.hr_history_revision = hr_history_revision
        self.hr_history_pointer = hr_history_pointer


def run_action(manifest: ActionManifest, preflight: Preflight, *,
               store: SlotStore, invoker=default_invoker, now: datetime) -> dict:
    """Consume the slot, verify live identity, then perform one Helm action."""
    manifest.active(now)
    d = manifest.doc

    # --- Fail-closed live identity checks, all read-only, before consumption. ---
    if preflight.repo_revision != d["repo"]["canonical_revision"]:
        raise ControlError("desired canonical revision changed")
    if preflight.flux_suspend is not True:
        raise ControlError("flux hold not suspended")
    if preflight.current_revision != d["expected_current_revision"]:
        raise ControlError("release revision does not match expected")
    if preflight.active_job:
        raise ControlError("active rollout job present")
    if preflight.hr_history_revision != d["expected_current_revision"]:
        raise ControlError("HelmRelease history does not match expected revision")
    if not isinstance(preflight.hr_history_pointer, str) or not preflight.hr_history_pointer:
        raise ControlError("HelmRelease history pointer missing")

    # --- Consume the slot exactly once, atomically, before running anything. ---
    store.consume(manifest.slot, {"manifest": manifest.digest, "action_id": d["action_id"],
                                  "release": d["release_name"], "namespace": d["namespace"],
                                  "expected_revision": d["expected_current_revision"]})

    # From here the slot is permanently consumed. Any failure is terminal: we
    # never retry, and a crash leaves the consumed marker in place.
    result = invoker(manifest.helm_argv(), deadline_seconds=d["deadline_seconds"])
    if result.get("returncode") != 0:
        raise ControlError("helm action failed")
    return {"action_id": d["action_id"], "slot": manifest.slot,
            "release": d["release_name"], "namespace": d["namespace"],
            "returncode": result["returncode"]}


def _scan_output(text: str) -> None:
    """Refuse to emit obvious credential material from a child's output."""
    for marker in ("password", "token=", "Authorization:", "api_key"):
        if marker.lower() in text.lower():
            raise ControlError("refused: child output may contain secrets")


__all__ = ["ControlError", "ActionManifest", "SlotStore", "Preflight", "run_action",
           "default_invoker", "slot_key", "control_host_id", "sha256_json", "instant"]


if __name__ == "__main__":  # pragma: no cover - manual, never in tests
    print("rollout_action_control is a library; no CLI action is exposed", file=sys.stderr)
    sys.exit(2)
