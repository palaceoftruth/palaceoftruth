"""Valkey must never accept unauthenticated connections on its data port.

An open Valkey is an open ARQ queue, and an open ARQ queue is a job-execution
primitive. These tests pin the password wiring end to end: the server requires
it, every client that needs it gets it, and it is never rendered into a
ConfigMap or a process argument.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


CHART_DIR = Path(__file__).resolve().parents[2] / "chart"
SECRET_NAME = "palaceoftruth-valkey-auth"


def _render_chart(*set_args: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", "palaceoftruth", str(CHART_DIR)]
    for arg in set_args:
        command.extend(["--set", arg])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _by_name(manifests: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(
        manifest
        for manifest in manifests
        if manifest.get("kind") == kind and manifest["metadata"]["name"] == name
    )


def _pod_specs(manifests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pod specs keyed by their ``app`` label.

    Jobs carry a content hash in their object name, so the pod label is the only
    stable key — and it is the same key the NetworkPolicies select on.
    """
    specs: dict[str, dict[str, Any]] = {}
    for manifest in manifests:
        if manifest.get("kind") not in {"Deployment", "StatefulSet", "Job"}:
            continue
        template = manifest["spec"]["template"]
        key = template["metadata"].get("labels", {}).get("app", manifest["metadata"]["name"])
        specs[key] = template["spec"]
    return specs


def _env_names(container: dict[str, Any]) -> set[str]:
    return {entry["name"] for entry in container.get("env", [])}


def _secret_ref(container: dict[str, Any], name: str) -> dict[str, Any]:
    entry = next(item for item in container["env"] if item["name"] == name)
    return entry["valueFrom"]["secretKeyRef"]


def test_password_secret_is_generated_by_default() -> None:
    secret = _by_name(_render_chart(), "Secret", SECRET_NAME)

    assert secret["stringData"]["valkey-password"]
    # Losing the Secret on an uninstall would orphan the persisted dataset.
    assert secret["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_an_existing_secret_replaces_the_generated_one() -> None:
    manifests = _render_chart(
        "valkey.auth.existingSecret=ops-valkey",
        "valkey.auth.existingSecretKey=password",
    )

    assert not any(
        manifest.get("kind") == "Secret" and manifest["metadata"]["name"] == SECRET_NAME
        for manifest in manifests
    )
    backend = _pod_specs(manifests)["palaceoftruth-backend"]["containers"][0]
    assert _secret_ref(backend, "REDIS_PASSWORD") == {
        "name": "ops-valkey",
        "key": "password",
    }


def test_valkey_server_requires_a_password() -> None:
    manifests = _render_chart()
    valkey = _pod_specs(manifests)["palaceoftruth-valkey"]["containers"][0]

    rendered = yaml.safe_dump(valkey["args"])
    assert "requirepass" in rendered
    # The password reaches the server through the environment and a private
    # config file, never through argv where any pod could read it from /proc.
    assert "$VALKEY_PASSWORD" in rendered
    assert _secret_ref(valkey, "VALKEY_PASSWORD")["name"] == SECRET_NAME


def test_sentinel_topology_authenticates_data_nodes_and_replication() -> None:
    manifests = _render_chart("highAvailability.enabled=true")
    specs = _pod_specs(manifests)

    for name in ("palaceoftruth-valkey-primary", "palaceoftruth-valkey-replica"):
        rendered = yaml.safe_dump(specs[name])
        # masterauth as well as requirepass: a replica must authenticate to the
        # primary it syncs from.
        assert "requirepass" in rendered
        assert "masterauth" in rendered

    # Sentinel needs the primary's password to run its own health checks, but
    # its own port stays unauthenticated because redis-py cannot pass
    # credentials to sentinel connections.
    sentinel = specs["palaceoftruth-valkey-sentinel"]
    assert "sentinel auth-pass" in yaml.safe_dump(sentinel["initContainers"])
    assert "REDISCLI_AUTH" not in _env_names(sentinel["containers"][0])


def test_every_client_workload_receives_the_password() -> None:
    manifests = _render_chart(
        "highAvailability.enabled=true",
        "memoryRolloutSmoke.enabled=true",
    )
    specs = _pod_specs(manifests)

    for name in (
        "palaceoftruth-backend",
        "palaceoftruth-worker",
        "palaceoftruth-media-worker",
        "palaceoftruth-palace-worker",
        # The MCP server is absent on purpose: it reaches the app over HTTP and
        # holds no queue credentials.
        "palaceoftruth-migration",
        "palaceoftruth-memory-rollout-smoke",
    ):
        containers = specs[name]["containers"] + specs[name].get("initContainers", [])
        assert any(
            "REDIS_PASSWORD" in _env_names(container) for container in containers
        ), f"{name} has no Redis credentials"


def test_the_password_never_lands_in_a_configmap() -> None:
    manifests = _render_chart("valkey.auth.password=s3cret-literal")

    for manifest in manifests:
        if manifest.get("kind") == "ConfigMap":
            assert "s3cret-literal" not in yaml.safe_dump(manifest)


def test_auth_can_be_disabled_for_local_and_test_installs() -> None:
    manifests = _render_chart("valkey.auth.enabled=false")

    assert not any(
        manifest.get("kind") == "Secret" and manifest["metadata"]["name"] == SECRET_NAME
        for manifest in manifests
    )
    backend = _pod_specs(manifests)["palaceoftruth-backend"]["containers"][0]
    assert "REDIS_PASSWORD" not in _env_names(backend)
    assert "requirepass" not in yaml.safe_dump(
        _pod_specs(manifests)["palaceoftruth-valkey"]
    )
