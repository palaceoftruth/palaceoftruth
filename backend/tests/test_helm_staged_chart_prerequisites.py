"""Chart prerequisites for a staged SAR1484 production upgrade.

Two renderer-only capabilities are pinned here:

1. The Sentinel-mode data StatefulSets expose a bounded ``updateStrategy``
   choice. ``RollingUpdate`` stays the default so existing upgrades keep their
   behavior; ``OnDelete`` holds both data pods in place for a separately
   approved staged replacement. Anything else must fail rendering.
2. The pre-upgrade migration hook carries explicit generation-model settings
   derived from the desired chart config, so it does not validate the *old*
   ConfigMap's GPT/nemotron policy while it runs before the replacement
   ConfigMap exists. Explicit env overrides ``envFrom`` without removing the
   database, RLS, auth, or secret wiring.

These tests exercise real ``helm template`` output and the actual
``app.config.Settings`` validators with fake credentials. They do not touch a
cluster, a provider, or a production values file.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.config import Settings

CHART_DIR = Path(__file__).resolve().parents[2] / "chart"
DATA_STATEFULSETS = ("palaceoftruth-valkey-primary", "palaceoftruth-valkey-replica")

# The generation settings Settings validates at import time. Keep this list
# aligned with validate_minimax_generation_policy in backend/app/config.py.
POLICY_ENV = (
    "LLM_PROVIDER",
    "LLM_DEFAULT_MODEL",
    "LLM_FALLBACK_MODELS",
    "OPENROUTER_DEFAULT_MODEL",
    "OPENROUTER_FALLBACK_MODELS",
    "RELATIONSHIP_CLASSIFICATION_MODEL",
    "CLIENT_LLM_ALLOWED_MODELS",
    "OPENROUTER_VISION_MODEL",
    "OPENROUTER_VISION_FALLBACK_MODELS",
)

DESIRED_POLICY = {
    "LLM_PROVIDER": "refuseless",
    "LLM_DEFAULT_MODEL": "deepseek-v4.1-flash-fast",
    "LLM_FALLBACK_MODELS": "minimax-m3.1-flash",
    "OPENROUTER_DEFAULT_MODEL": "minimax/minimax-m3",
    "OPENROUTER_FALLBACK_MODELS": "minimax/minimax-m2.7",
    "RELATIONSHIP_CLASSIFICATION_MODEL": "deepseek-v4.1-flash-fast",
    "CLIENT_LLM_ALLOWED_MODELS": "deepseek-v4.1-flash-fast,minimax-m3.1-flash",
    "OPENROUTER_VISION_MODEL": "minimax/minimax-m3",
    "OPENROUTER_VISION_FALLBACK_MODELS": "",
}

# What the previous ConfigMap/envFrom can still inject during a pre-upgrade
# hook. Every one of these values is independently rejected by the current
# Settings validator.
LEGACY_POLICY = {
    "LLM_PROVIDER": "openrouter",
    "OPENROUTER_DEFAULT_MODEL": "openai/gpt-4.1",
    "OPENROUTER_FALLBACK_MODELS": "nvidia/nemotron-3-super-120b-a12b",
    "RELATIONSHIP_CLASSIFICATION_MODEL": "openai/gpt-4.1",
    "CLIENT_LLM_ALLOWED_MODELS": "openai/gpt-4.1",
    "OPENROUTER_VISION_MODEL": "minimax/minimax-m3",
    "OPENROUTER_VISION_FALLBACK_MODELS": "openai/gpt-4o-mini,openai/gpt-4.1-mini",
    "VISION_PROVIDER": "gateway",
}

FAKE_CREDENTIALS = {
    "database_url": "postgresql+asyncpg://palace:fake-db-pass@db.example.test/palace",
    "openai_api_key": "fake-openai-key",
    "openrouter_api_key": "fake-openrouter-key",
    "llm_api_key": "fake-refuseless-key",
    "api_key": "fake-api-key",
}

DESIRED_SET_ARGS = (
    f"config.llmProvider={DESIRED_POLICY['LLM_PROVIDER']}",
    f"config.llmDefaultModel={DESIRED_POLICY['LLM_DEFAULT_MODEL']}",
    f"config.llmFallbackModels={DESIRED_POLICY['LLM_FALLBACK_MODELS']}",
    f"config.openrouterDefaultModel={DESIRED_POLICY['OPENROUTER_DEFAULT_MODEL']}",
    f"config.openrouterFallbackModels={DESIRED_POLICY['OPENROUTER_FALLBACK_MODELS']}",
    "config.relationshipClassificationModel="
    f"{DESIRED_POLICY['RELATIONSHIP_CLASSIFICATION_MODEL']}",
    "config.openrouterVisionModel="
    f"{DESIRED_POLICY['OPENROUTER_VISION_MODEL']}",
    # Commas separate --set entries, so the list value is escaped.
    "config.clientLlmAllowedModels="
    + DESIRED_POLICY["CLIENT_LLM_ALLOWED_MODELS"].replace(",", "\\,"),
)


def _render_chart(*set_args: str, is_upgrade: bool = True) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", "palaceoftruth", str(CHART_DIR)]
    if is_upgrade:
        command.append("--is-upgrade")
    for arg in set_args:
        command.extend(["--set-string", arg])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _render_raw(*set_args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", "palaceoftruth", str(CHART_DIR), "--is-upgrade"]
    for arg in set_args:
        command.extend(["--set-string", arg])
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _data_statefulsets(manifests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found = {
        manifest["metadata"]["name"]: manifest
        for manifest in manifests
        if manifest.get("kind") == "StatefulSet"
        and manifest["metadata"]["name"] in DATA_STATEFULSETS
    }
    assert set(found) == set(DATA_STATEFULSETS), "Sentinel data StatefulSets were not rendered"
    return found


def _migration_container(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    jobs = [
        manifest
        for manifest in manifests
        if manifest.get("kind") == "Job"
        and manifest.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "migration"
    ]
    assert len(jobs) == 1
    containers = jobs[0]["spec"]["template"]["spec"]["containers"]
    return next(container for container in containers if container["name"] == "migrate")


def _container_env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in container.get("env", [])}


def _policy_values(container: dict[str, Any]) -> dict[str, str]:
    env = _container_env(container)
    assert set(POLICY_ENV) <= set(env)
    return {name: env[name]["value"] for name in POLICY_ENV}


def _settings_from_environment(values: dict[str, str]) -> Settings:
    kwargs = {name.lower(): value for name, value in values.items()}
    kwargs.update(FAKE_CREDENTIALS)
    return Settings(**kwargs)


# ---------------------------------------------------------------------------
# StatefulSet updateStrategy
# ---------------------------------------------------------------------------


def test_data_statefulsets_default_to_rolling_update() -> None:
    manifests = _render_chart("valkey.sentinel.enabled=true")
    statefulsets = _data_statefulsets(manifests)

    for name, manifest in statefulsets.items():
        # Kubernetes defaults an omitted updateStrategy to RollingUpdate. The
        # chart now renders that type explicitly so the value is reviewable.
        assert manifest["spec"]["updateStrategy"] == {"type": "RollingUpdate"}, name
        # Preserve the existing selectors and PVC claim templates exactly.
        assert manifest["spec"]["selector"]["matchLabels"] == {"app": name}
        claim = manifest["spec"]["volumeClaimTemplates"][0]
        assert claim["metadata"]["name"] == "data"
        assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
        assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi"

    # No Sentinel workload or storage change is part of this prerequisite.
    sentinel = next(
        manifest
        for manifest in manifests
        if manifest.get("kind") == "Deployment"
        and manifest["metadata"]["name"] == "palaceoftruth-valkey-sentinel"
    )
    assert "updateStrategy" not in sentinel["spec"]


def test_on_delete_applies_to_both_data_statefulsets() -> None:
    manifests = _render_chart(
        "valkey.sentinel.enabled=true",
        "valkey.dataUpdateStrategy=OnDelete",
    )

    for name, manifest in _data_statefulsets(manifests).items():
        assert manifest["spec"]["updateStrategy"] == {"type": "OnDelete"}, name


@pytest.mark.parametrize("strategy", ["Recreate", "", "false", "0", "null"])
def test_invalid_update_strategy_is_refused(strategy: str) -> None:
    result = _render_raw(
        "valkey.sentinel.enabled=true",
        f"valkey.dataUpdateStrategy={strategy}",
    )

    assert result.returncode != 0
    assert "valkey.dataUpdateStrategy must be RollingUpdate or OnDelete" in result.stderr


@pytest.mark.parametrize("strategy", [False, 0, None, [], {}])
def test_non_string_update_strategy_is_refused(strategy: Any, tmp_path: Path) -> None:
    values = tmp_path / "invalid-strategy.yaml"
    values.write_text(yaml.safe_dump({"valkey": {"dataUpdateStrategy": strategy,
                                               "sentinel": {"enabled": True}}}))
    result = subprocess.run(
        ["helm", "template", "palaceoftruth", str(CHART_DIR), "--is-upgrade",
         "--values", str(values)], capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "valkey.dataUpdateStrategy must be RollingUpdate or OnDelete" in result.stderr


# ---------------------------------------------------------------------------
# Pre-upgrade migration generation policy
# ---------------------------------------------------------------------------


def test_migration_renders_desired_generation_policy_explicitly() -> None:
    manifests = _render_chart(*DESIRED_SET_ARGS, "valkey.sentinel.enabled=true")
    container = _migration_container(manifests)

    assert _policy_values(container) == DESIRED_POLICY
    # Explicit policy env is a literal override, never a secret reference.
    env = _container_env(container)
    assert all("valueFrom" not in env[name] for name in POLICY_ENV)

    # The desired ConfigMap carries the same reviewed values, so the pre-upgrade
    # hook and the post-upgrade workload cannot disagree.
    configmap = next(
        manifest
        for manifest in manifests
        if manifest.get("kind") == "ConfigMap"
        and manifest["metadata"]["name"] == "palaceoftruth-config"
    )
    assert {name: configmap["data"][name] for name in POLICY_ENV} == DESIRED_POLICY
    assert "openai/" not in yaml.safe_dump(configmap["data"])
    assert "nemotron" not in yaml.safe_dump(configmap["data"])


def test_migration_policy_env_satisfies_actual_settings_over_legacy_configmap() -> None:
    # Control: the legacy envFrom policy alone is exactly what the current
    # validator refuses during a pre-upgrade hook.
    with pytest.raises(ValidationError):
        _settings_from_environment(LEGACY_POLICY)

    manifests = _render_chart(*DESIRED_SET_ARGS)
    explicit = _policy_values(_migration_container(manifests))

    # Kubernetes gives explicit container env precedence over envFrom. Model
    # that merge, then run the real Settings validators.
    effective = {**LEGACY_POLICY, **explicit}
    settings = _settings_from_environment(effective)

    assert settings.llm_provider == "refuseless"
    assert settings.llm_default_model == "deepseek-v4.1-flash-fast"
    assert settings.llm_fallback_models == "minimax-m3.1-flash"
    assert settings.openrouter_default_model == "minimax/minimax-m3"
    assert settings.openrouter_fallback_models == "minimax/minimax-m2.7"
    assert settings.relationship_classification_model == "deepseek-v4.1-flash-fast"
    assert settings.client_llm_allowed_models == DESIRED_POLICY["CLIENT_LLM_ALLOWED_MODELS"]
    assert settings.openrouter_vision_model == "minimax/minimax-m3"
    assert settings.openrouter_vision_fallback_models == ""
    # Gateway vision routing is untouched by this change.
    assert settings.vision_provider == "gateway"


def test_migration_preserves_database_auth_rls_and_hooks() -> None:
    manifests = _render_chart(*DESIRED_SET_ARGS)
    job = next(
        manifest
        for manifest in manifests
        if manifest.get("kind") == "Job"
        and manifest.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "migration"
    )
    container = _migration_container(manifests)
    env = _container_env(container)

    assert job["metadata"]["annotations"] == {
        "argocd.argoproj.io/sync-wave": "1",
        "helm.sh/hook": "post-install,pre-upgrade",
        "helm.sh/hook-weight": "-10",
        "helm.sh/hook-delete-policy": "before-hook-creation,hook-succeeded",
    }
    assert container["command"] == ["alembic", "upgrade", "head"]
    assert container["envFrom"][0] == {"configMapRef": {"name": "palaceoftruth-config"}}
    assert [entry["secretRef"]["name"] for entry in container["envFrom"][1:]] == [
        "palaceoftruth-app-secrets"
    ]

    assert env["DATABASE_URL"]["value"].startswith("postgresql+asyncpg://")
    assert env["DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "palaceoftruth-postgres-app",
        "key": "password",
    }
    assert env["REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == (
        "palaceoftruth-valkey-auth"
    )
    assert env["DEFER_TENANT_RLS_ENFORCEMENT"]["value"] == "true"

    # No credential value is added next to the policy overrides.
    for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "API_KEY", "DB_PASSWORD"):
        assert name not in POLICY_ENV
    assert "valueFrom" in env["DB_PASSWORD"]
    assert "valueFrom" in env["REDIS_PASSWORD"]


def test_migration_keeps_additional_env_from_secrets() -> None:
    manifests = _render_chart(*DESIRED_SET_ARGS, "additionalEnvFromSecrets[0]=ops-extra")
    container = _migration_container(manifests)

    assert [entry["secretRef"]["name"] for entry in container["envFrom"][1:]] == [
        "palaceoftruth-app-secrets",
        "ops-extra",
    ]
