"""Ownership controls, without starting Docker or enabling the runtime lab."""
import json
import subprocess

import pytest
from tests import test_valkey_sentinel_authority_runtime_lab as lab


@pytest.mark.parametrize("kind", ["container", "network"])
@pytest.mark.parametrize("labels", [{}, {"io.palace.valkey-lab.owner": "foreign"}])
def test_refuses_foreign_resources(monkeypatch, kind, labels):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps([{"Id": "foreign-id", "Config": {"Labels": labels}, "Labels": labels}]), "")

    monkeypatch.setattr(lab, "_docker", docker)
    ledger = lab._Ownership("mine")
    ledger.remember(kind, "fixture")
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        ledger.remove(kind, "fixture")
    assert len(calls) == 1
    assert "rm" not in calls[0]


@pytest.mark.parametrize("kind", ["container", "network"])
def test_removes_only_owned_immutable_id(monkeypatch, kind):
    calls = []
    labels = {"io.palace.valkey-lab.owner": "mine"}

    def docker(*args, **kwargs):
        calls.append(args)
        if "inspect" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps([{"Id": "owned-id", "Config": {"Labels": labels}, "Labels": labels}]), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(lab, "_docker", docker)
    ledger = lab._Ownership("mine")
    ledger.remember(kind, "fixture")
    ledger.remove(kind, "fixture")
    assert calls[-1] == (("rm", "-f", "owned-id") if kind == "container" else ("network", "rm", "owned-id"))
    with pytest.raises(RuntimeError, match="not in ownership ledger"):
        ledger.remove(kind, "untracked")
    assert len(calls) == 2


def test_inspect_errors_fail_closed(monkeypatch):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "daemon unavailable")

    monkeypatch.setattr(lab, "_docker", docker)
    ledger = lab._Ownership("mine")
    ledger.remember("container", "fixture")
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        ledger.cleanup()
    assert len(calls) == 1


def test_cleanup_continues_after_refusal(monkeypatch):
    ledger = lab._Ownership("mine")
    ledger.remember("network", "net")
    ledger.remember("container", "foreign")
    ledger.remember("container", "own")
    removed = []

    def remove(kind, name):
        removed.append(name)
        if name == "foreign":
            raise RuntimeError("ownership mismatch")

    monkeypatch.setattr(ledger, "remove", remove)
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        ledger.cleanup()
    assert removed == ["own", "foreign", "net"]
