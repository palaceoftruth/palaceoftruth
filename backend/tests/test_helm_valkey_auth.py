"""Valkey must never accept unauthenticated connections on its data port.

An open Valkey is an open ARQ queue, and an open ARQ queue is a job-execution
primitive. These tests pin the password wiring end to end: the server requires
it, every client that needs it gets it, and it is never rendered into a
ConfigMap or a process argument.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
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


def test_the_password_is_rendered_in_exactly_one_manifest() -> None:
    # This is what makes the generated default safe. The password template uses
    # randAlphaNum when no existing Secret is found, so a second render site
    # would produce a *different* value in the same render and lock the app out
    # of its own Valkey. Every consumer must reference the Secret by key.
    manifests = _render_chart("valkey.auth.password=s3cret-literal")

    carriers = [
        f"{manifest['kind']}/{manifest['metadata']['name']}"
        for manifest in manifests
        if "s3cret-literal" in yaml.safe_dump(manifest)
    ]

    assert carriers == [f"Secret/{SECRET_NAME}"]


def _hook_phases(manifest: dict[str, Any]) -> set[str]:
    annotations = manifest["metadata"].get("annotations", {})
    return {phase.strip() for phase in annotations.get("helm.sh/hook", "").split(",") if phase.strip()}


def _hook_weight(manifest: dict[str, Any]) -> int:
    annotations = manifest["metadata"].get("annotations", {})
    return int(annotations.get("helm.sh/hook-weight", "0"))


def test_the_secret_is_created_before_every_hook_that_consumes_it() -> None:
    # Helm applies all pre-upgrade hooks before any normal resource, so a hook
    # that mounts REDIS_PASSWORD cannot depend on the Secret being a normal
    # resource. It wedges in CreateContainerConfigError instead -- which is
    # exactly how the first upgrade of this chart failed.
    manifests = _render_chart(
        "highAvailability.enabled=true",
        "memoryRolloutSmoke.enabled=true",
    )
    secret = _by_name(manifests, "Secret", SECRET_NAME)

    assert "pre-upgrade" in _hook_phases(secret)
    assert "pre-install" in _hook_phases(secret)

    consumers = [
        manifest
        for manifest in manifests
        if "REDIS_PASSWORD" in yaml.safe_dump(manifest)
        and _hook_phases(manifest) & {"pre-install", "pre-upgrade"}
    ]
    # A guard against the assertion below passing vacuously if the migration
    # Job ever stops being a pre-upgrade hook.
    assert consumers, "expected at least one pre-phase hook to consume the password"

    for consumer in consumers:
        assert _hook_weight(secret) < _hook_weight(consumer), (
            f"{consumer['kind']}/{consumer['metadata']['name']} runs at weight "
            f"{_hook_weight(consumer)}, at or before the Secret's {_hook_weight(secret)}"
        )


def test_repeat_upgrades_can_recreate_the_hook_secret() -> None:
    # Helm errors rather than adopting a hook resource that already exists, so
    # without this delete policy the *second* upgrade fails.
    secret = _by_name(_render_chart(), "Secret", SECRET_NAME)

    policy = secret["metadata"]["annotations"]["helm.sh/hook-delete-policy"]
    assert "before-hook-creation" in policy
    # hook-succeeded would delete the password the whole release depends on.
    assert "hook-succeeded" not in policy
    assert "hook-failed" not in policy


def _sed_supports_gnu_in_place() -> bool:
    """Whether ``sed -i SCRIPT FILE`` edits in place, as it does on Alpine.

    BSD sed reads the argument after ``-i`` as a backup suffix instead, so on a
    macOS workstation the script's ``sed -i`` steps fail and the tests below
    would be measuring the wrong thing. CI runs on Linux, where they run for
    real.
    """
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "probe"
        target.write_text("a")
        subprocess.run(
            ["/bin/sh", "-c", f'sed -i "s/a/b/" "{target}"'], capture_output=True
        )
        return target.read_text() == "b"


requires_gnu_sed = pytest.mark.skipif(
    not _sed_supports_gnu_in_place(),
    reason="the init script targets Alpine's GNU-style `sed -i`",
)


def _run_init_config_script(role: str, tmp_path: Path, existing_conf: str) -> str:
    """Run the real init-config script against a fixture config file.

    The script is executed verbatim apart from the config path, so these tests
    exercise the shell and awk the cluster actually runs -- not a paraphrase of
    it.
    """
    manifests = _render_chart("highAvailability.enabled=true")
    statefulset = _by_name(manifests, "StatefulSet", f"palaceoftruth-valkey-{role}")
    init = statefulset["spec"]["template"]["spec"]["initContainers"][0]
    assert init["name"] == "init-config"

    conf = tmp_path / "valkey.conf"
    conf.write_text(existing_conf)
    script = init["args"][0].replace("/data/valkey.conf", str(conf))

    subprocess.run(
        ["/bin/sh", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "VALKEY_PASSWORD": "s3cret-literal"},
    )
    return conf.read_text()


@requires_gnu_sed
@pytest.mark.parametrize("role", ["primary", "replica"])
@pytest.mark.parametrize(
    "password_token",
    [
        # What CONFIG REWRITE leaves behind on a data volume that predates auth.
        "nopass",
        # ...and what it leaves behind once a password is in play. A stale hash
        # locks out every client just as effectively as nopass leaves the door
        # open.
        "#c0ffee",
        ">an-old-password",
    ],
)
def test_a_stale_default_user_acl_cannot_defeat_requirepass(
    role: str, password_token: str, tmp_path: Path
) -> None:
    # Valkey applies `user` directives after the rest of the config, so this
    # line wins over requirepass. This is exactly how the first production
    # rollout of this chart ended up with an unauthenticated Valkey.
    result = _run_init_config_script(
        role,
        tmp_path,
        f"appendonly yes\nuser default on {password_token} sanitize-payload ~* &* +@all\n",
    )

    assert "user default on >s3cret-literal sanitize-payload ~* &* +@all" in result
    assert "nopass" not in result
    assert "#c0ffee" not in result
    assert "an-old-password" not in result
    assert "requirepass s3cret-literal" in result
    assert "masterauth s3cret-literal" in result


@requires_gnu_sed
@pytest.mark.parametrize("role", ["primary", "replica"])
def test_a_config_without_an_acl_line_is_left_to_requirepass(
    role: str, tmp_path: Path
) -> None:
    # With no `user default` directive at all, requirepass governs the default
    # user on its own. Synthesising an ACL rule here would be a way to get the
    # two out of step later.
    result = _run_init_config_script(role, tmp_path, "appendonly yes\n")

    assert "user default" not in result
    assert "requirepass s3cret-literal" in result


@requires_gnu_sed
@pytest.mark.parametrize("role", ["primary", "replica"])
def test_rerunning_the_init_script_is_idempotent(role: str, tmp_path: Path) -> None:
    # Every pod restart reruns this against the same persistent volume.
    once = _run_init_config_script(
        role, tmp_path, "appendonly yes\nuser default on nopass ~* +@all\n"
    )
    twice = _run_init_config_script(role, tmp_path, once)

    assert twice == once


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
