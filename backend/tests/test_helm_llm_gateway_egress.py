from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


CHART_DIR = Path(__file__).resolve().parents[2] / "chart"

ENABLED = (
    "llmGatewayEgress.enabled=true",
    "llmGatewayEgress.tailnetFqdn=lux.example.ts.net",
    "llmGatewayEgress.proxyGroup=example-egress",
)


def _render_chart(*set_args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", "palaceoftruth", str(CHART_DIR)]
    for arg in set_args:
        command.extend(["--set", arg])
    return subprocess.run(command, capture_output=True, text=True)


def _egress_service(*set_args: str) -> dict[str, Any] | None:
    result = _render_chart(*set_args)
    assert result.returncode == 0, result.stderr
    services = [
        doc
        for doc in yaml.safe_load_all(result.stdout)
        if isinstance(doc, dict)
        and doc.get("kind") == "Service"
        and doc["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "tailnet-egress"
    ]
    if not services:
        return None
    assert len(services) == 1
    return services[0]


def test_egress_service_is_absent_by_default() -> None:
    assert _egress_service() is None


def test_egress_service_sets_externalname_placeholder() -> None:
    service = _egress_service(*ENABLED)
    assert service is not None

    spec = service["spec"]
    assert spec["type"] == "ExternalName"
    # The API server rejects an ExternalName Service without this field, so the
    # chart must supply an inert value even though the Tailscale operator
    # immediately rewrites it to its own proxy Service.
    assert spec["externalName"] == "placeholder"
    # Pointing at the tailnet FQDN would resolve nowhere: cluster DNS does not
    # serve *.ts.net.
    assert "ts.net" not in spec["externalName"]

    annotations = service["metadata"]["annotations"]
    assert annotations["tailscale.com/tailnet-fqdn"] == "lux.example.ts.net"
    assert annotations["tailscale.com/proxy-group"] == "example-egress"


def test_egress_service_defaults_to_gateway_port() -> None:
    service = _egress_service(*ENABLED)
    assert service is not None
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 8080, "targetPort": 8080, "protocol": "TCP"}
    ]


@pytest.mark.parametrize("omitted", ["llmGatewayEgress.tailnetFqdn", "llmGatewayEgress.proxyGroup"])
def test_egress_service_requires_tailnet_settings(omitted: str) -> None:
    set_args = [arg for arg in ENABLED if not arg.startswith(f"{omitted}=")]
    result = _render_chart(*set_args)
    assert result.returncode != 0
    assert omitted in result.stderr
