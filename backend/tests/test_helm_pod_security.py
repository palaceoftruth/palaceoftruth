"""Chart rendering tests for pod hardening.

These cover the security-audit fixes H-06 (no securityContext anywhere),
H-07 (ServiceAccount tokens auto-mounted into every pod) and M-18 (no Pod
Security Admission labels on the namespace). The assertions are written
against the Kubernetes `restricted` profile rather than against the exact
values, so a future workload that forgets to opt in fails here instead of at
admission time in the cluster.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parents[2] / "chart"

# Every workload the chart can render, including the ones behind feature flags.
ALL_WORKLOADS_ARGS = (
    "highAvailability.enabled=true",
    "localEmbeddingService.enabled=true",
    "memoryRolloutSmoke.enabled=true",
    "valkey.metrics.enabled=true",
)

# The one pod that legitimately talks to the Kubernetes API. It reads pods and
# pod logs through a minimal Role to verify a rollout.
TOKEN_MOUNTING_WORKLOAD = "memory-smoke"


def _render_chart(*set_args: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", "palaceoftruth", str(CHART_DIR)]
    for arg in set_args:
        command.extend(["--set", arg])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _pod_specs(docs: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (workload name, PodSpec) for every workload kind the chart renders."""
    for doc in docs:
        kind = doc.get("kind")
        name = doc.get("metadata", {}).get("name", "<unnamed>")
        if kind in {"Deployment", "StatefulSet", "Job", "DaemonSet"}:
            yield name, doc["spec"]["template"]["spec"]
        elif kind == "CronJob":
            yield name, doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]


def _containers(spec: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from spec.get("initContainers") or []
    yield from spec.get("containers") or []


@pytest.fixture(scope="module")
def all_workload_docs() -> list[dict[str, Any]]:
    return _render_chart(*ALL_WORKLOADS_ARGS)


def test_every_workload_renders(all_workload_docs: list[dict[str, Any]]) -> None:
    """Guard the other tests: they pass vacuously if nothing renders."""
    names = {name for name, _ in _pod_specs(all_workload_docs)}
    assert len(names) >= 12, names


def test_pod_security_contexts_are_restricted(
    all_workload_docs: list[dict[str, Any]],
) -> None:
    for name, spec in _pod_specs(all_workload_docs):
        security = spec.get("securityContext") or {}
        assert security.get("runAsNonRoot") is True, name
        # A non-zero uid, not merely a present one.
        assert security.get("runAsUser"), name
        assert security["runAsUser"] != 0, name
        assert security.get("fsGroup"), name
        assert security.get("seccompProfile") == {"type": "RuntimeDefault"}, name


def test_container_security_contexts_are_restricted(
    all_workload_docs: list[dict[str, Any]],
) -> None:
    for name, spec in _pod_specs(all_workload_docs):
        for container in _containers(spec):
            where = f"{name}/{container['name']}"
            security = container.get("securityContext") or {}
            assert security.get("allowPrivilegeEscalation") is False, where
            assert security.get("readOnlyRootFilesystem") is True, where
            assert security.get("capabilities", {}).get("drop") == ["ALL"], where
            assert security.get("privileged") is not True, where


def test_every_volume_mount_has_a_volume(
    all_workload_docs: list[dict[str, Any]],
) -> None:
    """A mount naming a volume the PodSpec never declares fails at admission."""
    for doc in all_workload_docs:
        for name, spec in _pod_specs([doc]):
            volumes = {volume["name"] for volume in spec.get("volumes") or []}
            # A StatefulSet's claim templates are volumes too.
            volumes |= {
                claim["metadata"]["name"]
                for claim in doc["spec"].get("volumeClaimTemplates") or []
            }
            for container in _containers(spec):
                for mount in container.get("volumeMounts") or []:
                    assert mount["name"] in volumes, (
                        f"{name}/{container['name']}: {mount['name']}"
                    )


def test_app_image_containers_get_writable_scratch(
    all_workload_docs: list[dict[str, Any]],
) -> None:
    """A read-only root filesystem is only workable with somewhere to write.

    The application writes temp files under /tmp and an MCP credential cache
    under $HOME, so both must be mounted and HOME must point at the mount.
    Third-party images (Valkey, the metrics exporter) and the nginx frontend
    keep their own writable paths and are excluded.
    """
    checked = 0
    for name, spec in _pod_specs(all_workload_docs):
        for container in _containers(spec):
            if "palaceoftruth" not in container["image"]:
                continue
            if "frontend" in container["image"]:
                continue
            where = f"{name}/{container['name']}"
            mount_paths = {
                mount["mountPath"] for mount in container.get("volumeMounts") or []
            }
            env = {
                item["name"]: item.get("value")
                for item in container.get("env") or []
            }
            assert "/tmp" in mount_paths, where
            assert env.get("HOME"), where
            assert env["HOME"] in mount_paths, f"{where}: HOME is not writable"
            checked += 1
    assert checked >= 8, checked


def test_service_account_tokens_are_not_auto_mounted(
    all_workload_docs: list[dict[str, Any]],
) -> None:
    mounted = set()
    for name, spec in _pod_specs(all_workload_docs):
        assert "automountServiceAccountToken" in spec, name
        if spec["automountServiceAccountToken"]:
            mounted.add(name)
    # Exactly one exception, and it is the rollout smoke Job.
    assert len(mounted) == 1, mounted
    assert TOKEN_MOUNTING_WORKLOAD in next(iter(mounted))


def test_token_mounting_workload_keeps_a_minimal_role(
    all_workload_docs: list[dict[str, Any]],
) -> None:
    roles = [doc for doc in all_workload_docs if doc.get("kind") == "Role"]
    assert roles, "the rollout smoke Job must be bound to a namespaced Role"
    for role in roles:
        for rule in role["spec"] if "spec" in role else role["rules"]:
            # Read-only, and never a wildcard verb or resource.
            assert set(rule["verbs"]) <= {"get", "list", "watch"}, rule
            assert "*" not in rule["resources"], rule


def test_namespace_carries_pod_security_admission_labels() -> None:
    docs = _render_chart("namespace.create=true")
    namespaces = [doc for doc in docs if doc.get("kind") == "Namespace"]
    assert len(namespaces) == 1
    labels = namespaces[0]["metadata"]["labels"]
    for mode in ("enforce", "audit", "warn"):
        assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted"
        # Without a pinned version label the profile silently changes meaning
        # when the cluster upgrades.
        assert labels[f"pod-security.kubernetes.io/{mode}-version"]


def test_pod_security_admission_labels_can_be_relaxed_per_mode() -> None:
    docs = _render_chart(
        "namespace.create=true",
        "podSecurity.admission.enforce=baseline",
        "podSecurity.admission.warn=",
    )
    labels = next(
        doc for doc in docs if doc.get("kind") == "Namespace"
    )["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "baseline"
    assert labels["pod-security.kubernetes.io/audit"] == "restricted"
    assert "pod-security.kubernetes.io/warn" not in labels


def test_overrides_reach_a_single_workload() -> None:
    docs = _render_chart(
        "localEmbeddingService.enabled=true",
        "podSecurity.overrides.localEmbedding.container.readOnlyRootFilesystem=false",
    )
    by_name = dict(_pod_specs(docs))
    relaxed = next(
        spec for name, spec in by_name.items() if "local-embedding" in name
    )
    assert relaxed["containers"][0]["securityContext"][
        "readOnlyRootFilesystem"
    ] is False
    # No other workload is affected.
    for name, spec in by_name.items():
        if "local-embedding" in name:
            continue
        for container in _containers(spec):
            assert (
                container["securityContext"]["readOnlyRootFilesystem"] is True
            ), f"{name}/{container['name']}"


def test_postgres_cluster_leaves_besteffort_qos() -> None:
    """M-20: an empty resources block makes the database first to be evicted."""
    docs = _render_chart()
    cluster = next(doc for doc in docs if doc.get("kind") == "Cluster")
    resources = cluster["spec"]["resources"]
    assert resources["requests"]["memory"], resources
    assert resources["requests"]["cpu"], resources
    # Memory request must equal its limit; a memory limit above the request is
    # an eviction risk the kubelet cannot reclaim from.
    assert resources["limits"]["memory"] == resources["requests"]["memory"]


def test_backup_retention_has_a_default() -> None:
    """L-21: enabling backups must not create an unbounded bucket."""
    docs = _render_chart(
        "postgres.backup.enabled=true",
        "postgres.backup.objectStore.configuration.destinationPath=s3://bucket/palace",
    )
    store = next(doc for doc in docs if doc.get("kind") == "ObjectStore")
    assert store["spec"]["retentionPolicy"]
    schedule = next(doc for doc in docs if doc.get("kind") == "ScheduledBackup")
    assert schedule["spec"]["cluster"]["name"]


def test_require_backup_fails_the_render_when_backups_are_off() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    result = subprocess.run(
        [
            "helm",
            "template",
            "palaceoftruth",
            str(CHART_DIR),
            "--set",
            "postgres.backup.requireBackup=true",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "requireBackup" in result.stderr


def test_frontend_port_stays_consistent_across_resources() -> None:
    """The unprivileged nginx image cannot bind 80, but the Service still does."""
    docs = _render_chart()
    by_kind_name = {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in docs
        if doc.get("kind") and doc.get("metadata", {}).get("name")
    }
    deployment = by_kind_name[("Deployment", "palaceoftruth-frontend")]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    port = container["ports"][0]["containerPort"]
    assert port >= 1024, "a non-root container cannot bind a privileged port"

    service = by_kind_name[("Service", "palaceoftruth-frontend")]
    assert service["spec"]["ports"][0]["port"] == 80
    assert service["spec"]["ports"][0]["targetPort"] == port

    config = by_kind_name[("ConfigMap", "palaceoftruth-frontend-nginx")]
    assert f"listen {port};" in config["data"]["default.conf"]

    policy = by_kind_name[
        ("NetworkPolicy", "palaceoftruth-frontend-ingress")
    ]
    policy_ports = {
        entry["port"]
        for rule in policy["spec"]["ingress"]
        for entry in rule.get("ports", [])
    }
    assert policy_ports in ({port}, set()), policy_ports


def test_hardening_can_be_disabled_for_debugging() -> None:
    docs = _render_chart("podSecurity.enabled=false")
    for name, spec in _pod_specs(docs):
        assert "securityContext" not in spec, name
        for container in _containers(spec):
            assert "securityContext" not in container, f"{name}/{container['name']}"
