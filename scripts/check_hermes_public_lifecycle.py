"""Real official-host probe. Never replaces or patches Hermes modules.

Exit 2 means compatibility passed but strict host-global drain failed.
Only plugin transport/auth methods are fixture substitutes; no live memory writes.
"""
from __future__ import annotations

import argparse
import contextvars
import importlib.util
import inspect
import json
import os
import pathlib
import sys
import threading
from types import SimpleNamespace
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=pathlib.Path, required=True)
    parser.add_argument("--receipt", type=pathlib.Path, required=True)
    args = parser.parse_args()
    host = args.host.resolve()
    sys.path.insert(0, str(host))
    from agent.memory_provider import MemoryProvider, spawn_context_thread
    from agent.memory_manager import MemoryManager
    from hermes_cli.cli_commands_mixin import _sync_agent_to_session

    def deny_network(event, args):
        if event.startswith("socket."):
            raise AssertionError("Offline contract forbids network access")
    sys.addaudithook(deny_network)

    assert pathlib.Path(inspect.getfile(MemoryManager)).resolve() == host / "agent/memory_manager.py"
    assert pathlib.Path(inspect.getfile(_sync_agent_to_session)).resolve() == host / "hermes_cli/cli_commands_mixin.py"
    os.environ["PALACEOFTRUTH_DEFAULT_SCOPE_TYPE"] = "session"
    os.environ["PALACEOFTRUTH_DEFAULT_SCOPE_KEY"] = ""
    plugin = pathlib.Path(__file__).resolve().parents[1] / "third_party_plugins/hermes/memory/palaceoftruth/__init__.py"
    spec = importlib.util.spec_from_file_location("offline_palace_contract", plugin)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.spawn_context_thread is spawn_context_thread
    assert issubclass(module.PalaceOfTruthMemoryProvider, MemoryProvider)
    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("offline-session", hermes_home=str(args.receipt.parent / "offline-home"),
                        agent_identity="offline-agent", platform="cli")
    provider._has_api_auth = lambda: True
    provider._resolve_tenant_id = lambda: "offline-tenant"
    entered, release = threading.Event(), threading.Event()
    marker = contextvars.ContextVar("official-profile", default="incorrect")
    seen = []

    def transport(path, payload):
        seen.append(marker.get())
        entered.set()
        assert release.wait(5), "fixture release deadline"
        return {"status": "queued", "job_id": "offline-job"}

    provider._post_memory_entries = transport
    manager = MemoryManager()
    manager.add_provider(provider)
    token = marker.set("submitted-profile")
    try:
        manager.sync_all("offline user", "offline assistant", session_id="offline-session")
        assert entered.wait(3), "Official manager did not reach provider"
        host_flush = manager.flush_pending(timeout=0.1)
        provider_flush = provider.flush_pending(timeout=0.02)
        assert seen == ["submitted-profile"], seen
        receipt = {
            "official_abc_and_helper": "passed",
            "official_manager_context": "passed",
            "host_flush_returned": host_flush,
            "provider_flush_returned_while_transport_blocked": provider_flush,
            "host_global_drain": "unsupported" if host_flush and not provider_flush else "passed",
            "remote_durability": "not tested; offline transport only",
            "core_changes": False,
        }
    finally:
        release.set()
        assert provider.flush_pending(timeout=3)
        manager.shutdown_all()
        marker.reset(token)
    receipt["provider_shutdown"] = provider.shutdown_drain_state
    # Block the official executor using a provider hook, not private host APIs.
    # Use the unchanged official /resume-/branch boundary. Agent/CLI objects are
    # offline application fixtures; do not mutate private scope configuration.
    delayed: Any = module.PalaceOfTruthMemoryProvider()
    delayed.initialize("old-session", hermes_home=str(args.receipt.parent / "offline-home"),
                       agent_identity="offline-agent", platform="cli")
    delayed._has_api_auth = lambda: True
    delayed._resolve_tenant_id = lambda: "offline-tenant"
    entered, release = threading.Event(), threading.Event()
    payloads = []
    def blocked_prefetch(*args, **kwargs):
        entered.set()
        assert release.wait(5)
    delayed.queue_prefetch = blocked_prefetch
    # Retain the real quota/accounting path; substitute only HTTP transport.
    delayed._request_json = lambda method, path, payload: payloads.append(payload) or {"ok": True}
    manager = MemoryManager()
    manager.add_provider(delayed)
    cli = SimpleNamespace(agent=SimpleNamespace(session_id="old-session", _memory_manager=manager,
                                                reset_session_state=lambda: None), conversation_history=[])
    try:
        manager.queue_prefetch_all("offline blocker", session_id="old-session")
        assert entered.wait(3)
        manager.sync_all("offline delayed", "answer", session_id="old-session")
        _sync_agent_to_session(cli, "new-session", parent_session_id="", reason="resume")
        quota_before_dispatch = delayed._session_write_count
        release.set()
        assert manager.flush_pending(timeout=3)
        assert delayed.flush_pending(timeout=3)
        assert len(payloads) == 1
        receipt["host_submission_snapshot"] = (
            "passed" if payloads[0]["scope"] == {"type": "session", "key": "old-session"}
            and payloads[0]["metadata"]["session_id"] == "old-session"
            else "unsupported"
        )
        receipt["delayed_scope_observed"] = payloads[0]["scope"]
        receipt["supported_boundary"] = "hermes_cli.cli_commands_mixin._sync_agent_to_session"
        receipt["private_scope_mutation"] = False
        receipt["new_session_quota_before_old_dispatch"] = quota_before_dispatch
        receipt["new_session_quota_after_old_dispatch"] = delayed._session_write_count
        receipt["stale_host_submission_quota_isolation"] = (
            "passed" if delayed._session_write_count == quota_before_dispatch else "unsupported"
        )
    finally:
        release.set()
        manager.shutdown_all()
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    return 2 if "unsupported" in (receipt["host_global_drain"], receipt["host_submission_snapshot"],
                                   receipt["stale_host_submission_quota_isolation"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
