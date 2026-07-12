from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "chart"


def _render(*values: str) -> list[dict]:
    command = ["helm", "template", "palaceoftruth", str(CHART)]
    for value in values:
        command.extend(["--set", value])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _resource(documents: list[dict], kind: str) -> dict:
    return next(document for document in documents if document.get("kind") == kind)


def test_postgres_backup_resources_are_disabled_by_default() -> None:
    documents = _render()

    assert not any(document.get("kind") == "ObjectStore" for document in documents)
    assert not any(document.get("kind") == "ScheduledBackup" for document in documents)
    assert "plugins" not in _resource(documents, "Cluster")["spec"]


def test_postgres_backup_renders_plugin_object_store_and_schedule() -> None:
    documents = _render(
        "postgres.backup.enabled=true",
        "postgres.backup.objectStore.configuration.destinationPath=s3://backups/palace",
        "postgres.backup.objectStore.configuration.endpointURL=https://objects.example.test",
        "postgres.backup.objectStore.configuration.s3Credentials.accessKeyId.name=backup-creds",
        "postgres.backup.objectStore.configuration.s3Credentials.accessKeyId.key=ACCESS_KEY_ID",
        "postgres.backup.objectStore.configuration.s3Credentials.secretAccessKey.name=backup-creds",
        "postgres.backup.objectStore.configuration.s3Credentials.secretAccessKey.key=SECRET_ACCESS_KEY",
        "postgres.backup.objectStore.retentionPolicy=30d",
        "postgres.backup.scheduledBackup.immediate=true",
    )

    cluster = _resource(documents, "Cluster")
    object_store = _resource(documents, "ObjectStore")
    scheduled_backup = _resource(documents, "ScheduledBackup")

    assert cluster["spec"]["plugins"] == [
        {
            "name": "barman-cloud.cloudnative-pg.io",
            "isWALArchiver": True,
            "parameters": {"barmanObjectName": "palaceoftruth-postgres-backup"},
        }
    ]
    assert object_store["metadata"]["name"] == "palaceoftruth-postgres-backup"
    assert object_store["spec"]["configuration"]["destinationPath"] == "s3://backups/palace"
    assert object_store["spec"]["configuration"]["s3Credentials"]["accessKeyId"] == {
        "name": "backup-creds",
        "key": "ACCESS_KEY_ID",
    }
    assert object_store["spec"]["retentionPolicy"] == "30d"
    assert scheduled_backup["spec"] == {
        "schedule": "0 0 0 * * *",
        "immediate": True,
        "backupOwnerReference": "self",
        "cluster": {"name": "palaceoftruth-postgres"},
        "method": "plugin",
        "pluginConfiguration": {"name": "barman-cloud.cloudnative-pg.io"},
    }


def test_postgres_backup_requires_destination_path() -> None:
    command = [
        "helm",
        "template",
        "palaceoftruth",
        str(CHART),
        "--set",
        "postgres.backup.enabled=true",
    ]

    with pytest.raises(subprocess.CalledProcessError) as error:
        subprocess.run(command, check=True, capture_output=True, text=True)

    assert "configuration.destinationPath is required" in error.value.stderr
