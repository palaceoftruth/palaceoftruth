from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


CHART_DIR = Path(__file__).resolve().parents[2] / "chart"


def _helm(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    return subprocess.run(
        ["helm", "template", "palaceoftruth", str(CHART_DIR), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _render(*set_values: str) -> list[dict[str, Any]]:
    args = [part for value in set_values for part in ("--set", value)]
    return [
        document
        for document in yaml.safe_load_all(_helm(*args).stdout)
        if isinstance(document, dict)
    ]


def test_default_runtime_trust_boundaries_match_ingress_and_probes() -> None:
    manifests = _render()
    config = next(item for item in manifests if item.get("kind") == "ConfigMap")
    trusted_hosts = set(config["data"]["TRUSTED_HOSTS"].split(","))
    assert "api.palaceoftruth.example.com" in trusted_hosts
    assert "palaceoftruth-backend" in trusted_hosts
    assert "palaceoftruth-backend.default.svc.cluster.local" in trusted_hosts

    backend = next(
        item
        for item in manifests
        if item.get("kind") == "Deployment"
        and item["metadata"]["name"] == "palaceoftruth-backend"
    )
    container = backend["spec"]["template"]["spec"]["containers"][0]
    for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
        headers = container[probe_name]["httpGet"]["httpHeaders"]
        assert {"name": "Host", "value": "api.palaceoftruth.example.com"} in headers

    mcp = next(
        item
        for item in manifests
        if item.get("kind") == "Deployment"
        and item["metadata"]["name"] == "palaceoftruth-mcp"
    )
    env = {entry["name"]: entry.get("value") for entry in mcp["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["PALACEOFTRUTH_MCP_ALLOWED_HOSTS"] == (
        "mcp.palaceoftruth.example.com,mcp.palaceoftruth.example.com:*"
    )
    assert env["PALACEOFTRUTH_MCP_ALLOWED_ORIGINS"] == (
        "https://mcp.palaceoftruth.example.com,https://mcp.palaceoftruth.example.com:*"
    )


def test_admin_ingress_fails_closed_without_source_ranges() -> None:
    result = _helm("--set", "ingress.admin.enabled=true", check=False)
    assert result.returncode != 0
    assert "ingress.admin.allowedSourceRanges must be non-empty" in result.stderr


def test_admin_ingress_renders_only_with_explicit_source_range() -> None:
    manifests = _render(
        "ingress.admin.enabled=true",
        "ingress.admin.allowedSourceRanges[0]=10.0.0.0/8",
    )
    ingress = next(
        item
        for item in manifests
        if item.get("kind") == "Ingress"
        and item["metadata"]["name"] == "palaceoftruth-admin"
    )
    assert ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/whitelist-source-range"] == "10.0.0.0/8"
    assert ingress["spec"]["rules"][0]["host"] == "admin.palaceoftruth.example.com"
