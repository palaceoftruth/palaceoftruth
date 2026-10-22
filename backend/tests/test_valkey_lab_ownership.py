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


class _FakeDocker:
    """In-memory Docker stand-in: absent inspect, named create, ID removal."""

    def __init__(self, owner="mine"):
        self.owner = owner
        self.ids = {}
        self.calls = []
        self.removed_ids = []
        self._seq = 0

    def create(self, kind, name, **kwargs):
        self._seq += 1
        resource_id = f"{kind}-id-{self._seq}"
        self.ids[(kind, name)] = resource_id
        return subprocess.CompletedProcess((kind, "create", name), 0, resource_id + "\n", "")

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] in ("network", "container"):
            verb, name = args[1], args[-1]
            kind = args[0]
            if verb == "create":
                return self.create(kind, name)
        elif args[0] in ("inspect", "run"):
            verb, kind = args[0], "container"
            name = args[args.index("--name") + 1] if verb == "run" else args[1]
        else:
            verb, kind, name = "rm", "container", args[-1]
        if verb == "inspect":
            resource_id = self.ids.get((kind, name))
            if resource_id is None:
                return subprocess.CompletedProcess(args, 1, "", f"Error: No such {kind}")
            return subprocess.CompletedProcess(
                args, 0,
                json.dumps([{"Id": resource_id, "Labels": {lab.OWNER_LABEL: self.owner},
                             "Config": {"Labels": {lab.OWNER_LABEL: self.owner}}}]), "")
        if verb == "run":
            return self.create(kind, name)
        if verb == "rm":
            target = args[-1]
            self.removed_ids.append(target)
            for key, resource_id in list(self.ids.items()):
                if resource_id == target:
                    del self.ids[key]
            return subprocess.CompletedProcess(args, 0, target + "\n", "")
        raise AssertionError(f"unexpected docker call: {args}")


def test_absent_before_create_then_new_id_rearms_removal(monkeypatch, tmp_path):
    """Pre-create remember leaves a tombstone; the created ID must clear it."""
    fake = _FakeDocker()
    monkeypatch.setattr(lab, "_docker", fake)
    ledger = lab._Ownership("mine", tmp_path / "ledger.json")

    ledger.remember("container", "fresh")          # registered BEFORE it exists
    ledger.remove("container", "fresh")            # absent: nothing to remove
    assert ("container", "fresh") in ledger.removed
    assert fake.removed_ids == []

    cid = lab._docker("run", "-d", "--name", "fresh", check=True).stdout.strip()
    ledger.remember("container", "fresh", cid)     # the ID now exists
    assert ("container", "fresh") not in ledger.removed

    ledger.remove("container", "fresh")
    assert fake.removed_ids == [cid]               # removal really ran, by inspected ID
    assert ("container", "fresh") in ledger.removed


def test_recreate_after_remove_is_removable_again(monkeypatch, tmp_path):
    """A second create after a real removal must not inherit the old tombstone."""
    fake = _FakeDocker()
    monkeypatch.setattr(lab, "_docker", fake)
    ledger = lab._Ownership("mine", tmp_path / "ledger.json")

    first = lab._docker("run", "-d", "--name", "cycled", check=True).stdout.strip()
    ledger.remember("container", "cycled", first)
    ledger.remove("container", "cycled")
    assert fake.removed_ids == [first]

    second = lab._docker("run", "-d", "--name", "cycled", check=True).stdout.strip()
    assert second != first
    ledger.remember("container", "cycled", second)
    ledger.remove("container", "cycled")
    assert fake.removed_ids == [first, second]
    assert ("container", "cycled") in ledger.removed
