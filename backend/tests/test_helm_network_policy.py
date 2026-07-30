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


def _network_policy(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    policies = [manifest for manifest in manifests if manifest.get("kind") == "NetworkPolicy"]
    assert len(policies) == 1
    return policies[0]


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
