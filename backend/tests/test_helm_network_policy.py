from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


CHART_DIR = Path(__file__).resolve().parents[2] / "chart"


def _render_chart(*set_args: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", "palaceoftruth", str(CHART_DIR)]
    for arg in set_args:
        command.extend(["--set", arg])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _policies(manifests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        manifest["metadata"]["name"]: manifest
        for manifest in manifests
        if manifest.get("kind") == "NetworkPolicy"
    }


def _network_policy(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    """The egress policy. Ingress policies are asserted separately below."""
    return _policies(manifests)["palaceoftruth-restricted-egress"]


def test_restricted_egress_is_enabled_by_default() -> None:
    policy = _network_policy(_render_chart())

    assert policy["metadata"]["name"] == "palaceoftruth-restricted-egress"
    assert policy["spec"]["policyTypes"] == ["Egress"]
    selected_apps = policy["spec"]["podSelector"]["matchExpressions"][0]["values"]
    assert selected_apps == [
        "palaceoftruth-backend",
        "palaceoftruth-worker",
        "palaceoftruth-media-worker",
        "palaceoftruth-palace-worker",
        "palaceoftruth-mcp",
        "palaceoftruth-frontend",
    ]

    rules = policy["spec"]["egress"]
    assert any(
        rule.get("to") == [{"podSelector": {"matchLabels": {"app": "palaceoftruth-backend"}}}]
        and rule.get("ports") == [{"protocol": "TCP", "port": 8000}]
        for rule in rules
    )
    assert any(
        rule.get("to") == [{"podSelector": {"matchLabels": {"cnpg.io/cluster": "palaceoftruth-postgres"}}}]
        and rule.get("ports") == [{"protocol": "TCP", "port": 5432}]
        for rule in rules
    )


def test_public_egress_excludes_private_and_local_addresses() -> None:
    policy = _network_policy(_render_chart())
    ip_blocks = [
        peer["ipBlock"]
        for rule in policy["spec"]["egress"]
        for peer in rule.get("to", [])
        if "ipBlock" in peer and "except" in peer["ipBlock"]
    ]
    ipv4 = next(block for block in ip_blocks if block["cidr"] == "0.0.0.0/0")
    ipv6 = next(block for block in ip_blocks if block["cidr"] == "::/0")

    assert {"10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16"} <= set(
        ipv4["except"]
    )
    assert {"::1/128", "fc00::/7", "fe80::/10"} <= set(ipv6["except"])
    assert "::ffff:0:0/96" not in ipv6["except"]


def test_policy_can_be_disabled_explicitly() -> None:
    manifests = _render_chart("networkPolicy.enabled=false")
    assert not any(manifest.get("kind") == "NetworkPolicy" for manifest in manifests)


def test_operator_can_add_a_narrow_private_endpoint_exception() -> None:
    policy = _network_policy(
        _render_chart(
            "networkPolicy.additionalEgress[0].to[0].ipBlock.cidr=10.20.30.40/32",
            "networkPolicy.additionalEgress[0].ports[0].protocol=TCP",
            "networkPolicy.additionalEgress[0].ports[0].port=443",
        )
    )

    assert {
        "to": [{"ipBlock": {"cidr": "10.20.30.40/32"}}],
        "ports": [{"protocol": "TCP", "port": 443}],
    } in policy["spec"]["egress"]


def test_external_database_and_valkey_are_not_broadly_allowed() -> None:
    policy = _network_policy(_render_chart("postgres.enabled=false", "valkey.enabled=false"))
    serialized_rules = yaml.safe_dump(policy["spec"]["egress"])

    assert "cnpg.io/cluster" not in serialized_rules
    assert "palaceoftruth-valkey" not in serialized_rules


def _peer_apps(rule: dict[str, Any]) -> set[str]:
    apps: set[str] = set()
    for peer in rule.get("from", []):
        selector = peer.get("podSelector", {})
        for expression in selector.get("matchExpressions", []):
            if expression.get("key") == "app":
                apps.update(expression.get("values", []))
        app = selector.get("matchLabels", {}).get("app")
        if app:
            apps.add(app)
    return apps


def _ports(rule: dict[str, Any]) -> set[int]:
    return {port["port"] for port in rule.get("ports", [])}


def test_ingress_policies_are_enabled_by_default() -> None:
    policies = _policies(_render_chart())

    assert {
        "palaceoftruth-valkey-ingress",
        "palaceoftruth-postgres-ingress",
        "palaceoftruth-app-ingress",
        "palaceoftruth-frontend-ingress",
    } <= set(policies)
    for name, policy in policies.items():
        if name.endswith("-ingress"):
            assert policy["spec"]["policyTypes"] == ["Ingress"]


def test_valkey_ingress_is_limited_to_application_pods() -> None:
    policy = _policies(_render_chart())["palaceoftruth-valkey-ingress"]

    app_rule = next(
        rule for rule in policy["spec"]["ingress"] if "palaceoftruth-backend" in _peer_apps(rule)
    )
    assert _ports(app_rule) == {6379, 26379}
    # The release-time Jobs also talk to Valkey; excluding them would wedge deploys.
    assert {
        "palaceoftruth-worker",
        "palaceoftruth-media-worker",
        "palaceoftruth-palace-worker",
        "palaceoftruth-migration",
        "palaceoftruth-memory-rollout-smoke",
    } <= _peer_apps(app_rule)
    # Nothing else in the cluster, and no namespace-wide or empty allowance.
    # MCP and the frontend never touch the queue.
    assert not {
        "palaceoftruth-frontend",
        "palaceoftruth-mcp",
        "palaceoftruth-tenant-rls-enforcement",
    } & _peer_apps(app_rule)
    assert all(rule.get("from") for rule in policy["spec"]["ingress"])


def test_postgres_ingress_allows_the_cnpg_operator_and_replication() -> None:
    policy = _policies(_render_chart())["palaceoftruth-postgres-ingress"]
    rules = policy["spec"]["ingress"]

    assert policy["spec"]["podSelector"] == {
        "matchLabels": {"cnpg.io/cluster": "palaceoftruth-postgres"}
    }
    app_rule = next(
        rule for rule in rules if "palaceoftruth-backend" in _peer_apps(rule)
    )
    assert _ports(app_rule) == {5432}
    assert "palaceoftruth-tenant-rls-enforcement" in _peer_apps(app_rule)
    assert any(
        peer.get("podSelector", {}).get("matchLabels", {}).get("cnpg.io/cluster")
        == "palaceoftruth-postgres"
        for rule in rules
        for peer in rule.get("from", [])
    )
    assert any(
        peer.get("namespaceSelector", {}).get("matchLabels", {}).get(
            "kubernetes.io/metadata.name"
        )
        == "cnpg-system"
        for rule in rules
        for peer in rule.get("from", [])
    )


def test_app_ingress_covers_backend_and_mcp_ports() -> None:
    policy = _policies(_render_chart())["palaceoftruth-app-ingress"]

    assert policy["spec"]["podSelector"]["matchExpressions"][0]["values"] == [
        "palaceoftruth-backend",
        "palaceoftruth-mcp",
    ]
    in_cluster = next(
        rule for rule in policy["spec"]["ingress"] if "palaceoftruth-frontend" in _peer_apps(rule)
    )
    assert in_cluster and _ports(in_cluster) == {8000, 8765}
    assert any(
        peer.get("namespaceSelector", {}).get("matchLabels", {}).get(
            "kubernetes.io/metadata.name"
        )
        == "ingress-nginx"
        for rule in policy["spec"]["ingress"]
        for peer in rule.get("from", [])
    )


def test_ingress_policies_can_be_disabled_without_losing_egress() -> None:
    policies = _policies(_render_chart("networkPolicy.ingress.enabled=false"))

    assert set(policies) == {"palaceoftruth-restricted-egress"}


def test_data_tier_ingress_policies_follow_the_bundled_components() -> None:
    policies = _policies(_render_chart("postgres.enabled=false", "valkey.enabled=false"))

    assert "palaceoftruth-valkey-ingress" not in policies
    assert "palaceoftruth-postgres-ingress" not in policies


def test_frontend_stays_reachable_when_no_ingress_namespace_is_configured() -> None:
    policy = _policies(
        _render_chart("networkPolicy.ingress.ingressControllerNamespace=")
    )["palaceoftruth-frontend-ingress"]

    assert policy["spec"]["ingress"] == [{}]


def test_s3_endpoint_allowlist_is_wired_into_runtime_config() -> None:
    manifests = _render_chart(
        "config.palaceSyncS3AllowedEndpointHosts=minio.example.com",
    )
    config_map = next(
        manifest
        for manifest in manifests
        if manifest.get("kind") == "ConfigMap"
        and manifest.get("metadata", {}).get("name") == "palaceoftruth-config"
    )

    assert (
        config_map["data"]["PALACE_SYNC_S3_ALLOWED_ENDPOINT_HOSTS"]
        == "minio.example.com"
    )


def test_source_refresh_private_host_allowlist_is_wired_into_runtime_config() -> None:
    manifests = _render_chart(
        "config.sourceResourceRefreshAllowedHosts=fixture.internal",
        "config.sourceResourceRefreshTrustedPrivateHosts=fixture.internal",
    )
    config_map = next(
        manifest
        for manifest in manifests
        if manifest.get("kind") == "ConfigMap"
        and manifest.get("metadata", {}).get("name") == "palaceoftruth-config"
    )

    assert config_map["data"]["SOURCE_RESOURCE_REFRESH_ALLOWED_HOSTS"] == "fixture.internal"
    assert (
        config_map["data"]["SOURCE_RESOURCE_REFRESH_TRUSTED_PRIVATE_HOSTS"]
        == "fixture.internal"
    )
