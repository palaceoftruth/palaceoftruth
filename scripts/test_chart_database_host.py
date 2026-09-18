#!/usr/bin/env python3
"""Render-only regression tests. Requires Helm 3 and PyYAML; runs no cluster commands.

Run: HELM=/path/to/helm python3 scripts/test_chart_database_host.py
"""
import os
from pathlib import Path
import subprocess
import unittest

import yaml

CHART = Path(__file__).resolve().parents[1] / "chart"


def render(*settings):
    command = [os.environ.get("HELM", "helm"), "template", "palace-sarvent", str(CHART),
               "--set", "fullnameOverride=palace-sarvent"]
    for setting in settings:
        command.extend(["--set", setting])
    return [doc for doc in yaml.safe_load_all(subprocess.check_output(command, text=True)) if doc]


class DatabaseHostTest(unittest.TestCase):
    def check_internal(self, documents, host=None, ca="palace-sarvent-postgres-ca"):
        rows = []
        for doc in documents:
            pod = doc.get("spec", {}).get("template", {}).get("spec", {})
            for container in pod.get("initContainers", []) + pod.get("containers", []):
                env_list = container.get("env", [])
                env = {item["name"]: item for item in env_list}
                if "DB_HOST" not in env:
                    continue
                name = doc["metadata"]["name"]
                rows.append((name, container["name"]))
                with self.subTest(resource=name, container=container["name"]):
                    expected = {"name": "DB_HOST", "value": host} if host else {
                        "name": "DB_HOST", "valueFrom": {"secretKeyRef": {
                            "name": "palace-sarvent-postgres-app", "key": "host"}}}
                    self.assertEqual(env["DB_HOST"], expected)
                    for variable, key in [("DB_USER", "username"), ("DB_PASSWORD", "password"),
                                          ("DB_PORT", "port"), ("DB_NAME", "dbname")]:
                        self.assertEqual(env[variable]["valueFrom"]["secretKeyRef"],
                                         {"name": "palace-sarvent-postgres-app", "key": key})
                    self.assertIn("@$(DB_HOST):$(DB_PORT)/$(DB_NAME)?sslmode=verify-full",
                                  env["DATABASE_URL"]["value"])
                    names = [item["name"] for item in env_list]
                    self.assertLess(names.index("DB_HOST"), names.index("DATABASE_URL"))
                    self.assertEqual(env["DATABASE_SSL_ROOT_CERT"]["value"],
                                     "/etc/palaceoftruth/database-tls/ca.crt")
                    volumes = {item["name"]: item for item in pod["volumes"]}
                    self.assertEqual(volumes["database-tls"]["secret"]["secretName"], ca)
                    mounts = {item["name"]: item for item in container["volumeMounts"]}
                    self.assertTrue(mounts["database-tls"]["readOnly"])
                    if doc["kind"] == "Job":
                        self.assertIn("helm.sh/hook", doc["metadata"]["annotations"])
        self.assertEqual(len(rows), 9)
        self.assertEqual({container for _, container in rows}, {
            "wait-for-migrations", "backend", "worker", "media-worker", "palace-worker",
            "wait-for-writable-database", "migrate", "enforce-tenant-rls"})
        clusters = [doc for doc in documents if doc["kind"] == "Cluster"]
        self.assertEqual([doc["metadata"]["name"] for doc in clusters], ["palace-sarvent-postgres"])

    def test_default_preserves_source_routing(self):
        self.check_internal(render())

    def test_override_reaches_all_nine_consumers_and_preserves_cluster(self):
        baseline = render()
        target = render("postgres.applicationHost=palace-sarvent-recovery-rw",
                        "databaseTls.caSecretName=palace-sarvent-recovery-ca")
        self.check_internal(target, host="palace-sarvent-recovery-rw", ca="palace-sarvent-recovery-ca")
        self.assertEqual([doc for doc in baseline if doc["kind"] == "Cluster"],
                         [doc for doc in target if doc["kind"] == "Cluster"])

    def test_external_database_ignores_internal_host_override(self):
        documents = render("postgres.enabled=false", "existingSecret=external-app",
                           "postgres.applicationHost=ignored.example",
                           "databaseTls.caSecretName=external-ca")
        count = 0
        for doc in documents:
            pod = doc.get("spec", {}).get("template", {}).get("spec", {})
            for container in pod.get("initContainers", []) + pod.get("containers", []):
                env = {item["name"]: item for item in container.get("env", [])}
                if "DATABASE_URL" in env:
                    count += 1
                    self.assertNotIn("DB_HOST", env)
                    self.assertEqual(env["DATABASE_URL"]["valueFrom"]["secretKeyRef"],
                                     {"name": "external-app", "key": "DATABASE_URL"})
        self.assertEqual(count, 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
