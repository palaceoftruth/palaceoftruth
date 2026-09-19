#!/usr/bin/env python3
"""Offline real-Hermes contract gate. Trusted checkout only; never starts an agent.

Host/provider code runs in a -I -S subprocess with an allowlisted environment,
scratch HOME/HERMES_HOME/cwd and Linux seccomp network/process-exec denial.
Only the copied Palace provider is explicitly loaded; no plugin auto-discovery.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import functools
import inspect
import json
import os
from pathlib import Path
import shutil
import site
import socket
import subprocess
import sys
import tempfile
from typing import Any

GATES = ("isolation", "discovery", "registration", "lifecycle", "write_completion", "global_drain", "tool_dispatch", "checkpoint", "context_helper")


def probe_write_completion(provider_type, manager_type):
    """Exercise actual accepted writes, with offline transport and real host waits.

    No initialize/config/auth lookup: only fixture instances and in-memory writes.
    The host's five-second drain timeout and provider join are NOT patched.
    """
    import contextvars
    import threading
    from time import perf_counter

    profile = contextvars.ContextVar("completion_profile", default="wrong")

    def fixture():
        provider = provider_type()
        provider._session_id = "old-session"
        provider._scope_type = "session"
        provider._has_api_auth = lambda: True
        provider._resolve_tenant_id = lambda: "offline-tenant"
        entered, release, completed = (threading.Event() for _ in range(3))
        seen = []

        def transport(method, path, payload):
            entered.set()
            if not release.wait(25):
                raise AssertionError("Offline transport was not released")
            seen.append({"profile": profile.get(), "session": payload["metadata"]["session_id"],
                         "scope": payload["scope"], "thread": threading.current_thread().name})
            completed.set()
            return {"ok": True}

        provider._request_json = transport
        manager = manager_type()
        manager.add_provider(provider)
        return provider, manager, entered, release, completed, seen

    # A flush barrier cannot pass a still-running turn write.
    provider, manager, entered, release, completed, seen = fixture()
    try:
        token = profile.set("submission-profile")
        manager.sync_all("Offline accepted turn", "Offline answer", session_id="old-session")
        profile.reset(token)
        assert entered.wait(3), "Authenticated fixture never reached transport"
        flush_blocked = manager.flush_pending(timeout=0.1)
        assert not flush_blocked, "flush_pending reported completion while sync transport was blocked"
        manager.on_session_switch("new-session")
        provider._reserve_write_quota(is_bulk=False)
        release.set()
        assert manager.flush_pending(timeout=3), "Released write did not complete"
        assert completed.is_set() and len(seen) == 1, seen
        assert seen[0]["profile"] == "submission-profile", seen
        assert seen[0]["session"] == "old-session", seen
        assert seen[0]["scope"] == {"type": "session", "key": "old-session"}, seen
        assert provider._session_write_count == provider._turn_write_count == 1
        manager.shutdown_all()
        assert manager.shutdown_drain_state["status"] == "drained"
        released = {"flush_while_blocked": flush_blocked, "writes": seen,
                    "new_session_write_count": provider._session_write_count,
                    "shutdown": manager.shutdown_drain_state}
    finally:
        release.set()
        manager.shutdown_all()

    # The real shutdown bound must expire honestly, not orphan a hidden writer.
    provider, manager, entered, release, completed, seen = fixture()
    try:
        manager.sync_all("Offline blocked shutdown", "Offline answer", session_id="old-session")
        assert entered.wait(3)
        manager.sync_all("Offline queued turn", "Offline answer", session_id="old-session")
        started = perf_counter()
        manager.shutdown_all()
        elapsed = perf_counter() - started
        drain = manager.shutdown_drain_state
        assert drain["status"] == "timed_out", drain
        assert drain["active_tasks"] == 1 and drain["abandoned_writes"] == 1, drain
        assert not completed.is_set(), "Fixture completed before the shutdown bound"
        assert elapsed < 15, "Shutdown exceeded the two real five-second drain budgets"
        # Expose pinned-host shortcomings rather than call them provider fixes.
        post_shutdown_flush = manager.flush_pending(timeout=0.1)
        manager.shutdown_all()
        repeated_shutdown = manager.shutdown_drain_state
        assert not completed.is_set()
        timed_out = {"seconds": round(elapsed, 3), "shutdown": drain,
                     "write_completed_at_shutdown": False}
    finally:
        release.set()
        assert completed.wait(3), "Released detached transport failed to finish"
        provider.shutdown()

    # Host mirrors run inline, outside its tracked executor; the provider still
    # has a legacy asynchronous mirror FIFO. A strict global drain must reject it.
    provider, manager, entered, release, completed, seen = fixture()
    caller = threading.Thread(
        target=lambda: manager.on_memory_write("add", "memory", "Offline mirrored write"),
        daemon=True,
    )
    try:
        caller.start()
        assert entered.wait(3)
        mirror_flush = manager.flush_pending(timeout=0.1)
    finally:
        release.set()
        caller.join(3)
        provider.shutdown()
        manager.shutdown_all()
    assert not caller.is_alive()
    assert completed.is_set() and len(seen) == 1
    limitations = []
    if post_shutdown_flush:
        limitations.append("Host flush returns true after clearing executor while transport remains active")
    if repeated_shutdown["status"] == "drained":
        limitations.append("Repeated host shutdown overwrites timeout while transport remains active")
    if mirror_flush:
        limitations.append("Host flush does not account for provider-owned mirror writes")
    return ({"status": "passed", "scope": "manager-accepted sync attempts; not remote durability",
             "released": released, "blocked_shutdown": timed_out},
            {"status": "supported_limitation" if limitations else "passed",
             "post_timeout_flush": post_shutdown_flush, "repeated_shutdown": repeated_shutdown,
             "mirror_flush_while_blocked": mirror_flush, "limitations": limitations})


def deny_network_and_exec():
    """Kernel-enforced in this child and inherited by its threads; fail closed."""
    if sys.platform != "linux":
        raise RuntimeError("Offline gate requires Linux and libseccomp.so.2")
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError("Cannot allocate seccomp filter")
    try:
        # socketpair remains available for local asyncio wakeups; socket() is
        # entirely denied, including Unix sockets that could proxy credentials.
        for name in ("socket", "connect", "sendto", "sendmsg", "sendmmsg", "execve", "execveat"):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or lib.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise RuntimeError(f"Cannot deny syscall {name}")
        if lib.seccomp_load(ctx) != 0:
            raise RuntimeError("Cannot install seccomp filter")
    finally:
        lib.seccomp_release(ctx)


def child_probe(host: Path, package_paths: list[str], context_smoke: bool) -> dict:
    report: dict[str, Any] = {"gates": {name: {"status": "not_run"} for name in GATES}}
    gates = report["gates"]
    current = "isolation"
    violations = []
    scratch = Path.cwd().resolve()
    try:
        deny_network_and_exec()
        assert Path(os.environ["HOME"]).resolve() == scratch
        assert Path(os.environ["HERMES_HOME"]).resolve() == scratch / ".hermes"
        assert not any(k for k in os.environ if k.endswith(("_KEY", "_TOKEN", "_SECRET")))
        assert os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] == "0"
        # Exercise the kernel filter through libc, not a patched Python socket.
        libc = ctypes.CDLL(None, use_errno=True)
        for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
            assert libc.socket(family, socket.SOCK_STREAM, 0) == -1
            assert ctypes.get_errno() == errno.EPERM

        def audit(event, args):
            if event in {"socket.__new__", "socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "subprocess.Popen", "os.system", "os.exec"}:
                violations.append(event)
                raise PermissionError(f"Host gate forbids {event}")
            if event == "open" and isinstance(args[0], (str, bytes)):
                path = Path(os.fsdecode(args[0])).resolve()
                if path.name in {".env", "auth.json", "credentials.json", "config.yaml"} and not path.is_relative_to(scratch):
                    violations.append("external_configuration")
                    raise PermissionError("Host gate forbids external configuration")

        sys.addaudithook(audit)
        sys.dont_write_bytecode = True
        # -S prevents .pth/sitecustomize/plugin-entrypoint startup. Add only
        # explicit interpreter package paths after installing both guards.
        sys.path[:0] = [str(host), *package_paths]
        gates[current] = {"status": "passed", "network": "Linux seccomp + Python audit", "credentials": "none", "plugin_autoload": False}

        current = "discovery"
        from agent.memory_provider import MemoryProvider
        from agent.memory_manager import MemoryManager
        from plugins.memory import find_provider_dir, load_memory_provider
        copied = scratch / ".hermes/plugins/palaceoftruth"
        assert find_provider_dir("palaceoftruth").resolve() == copied
        provider = load_memory_provider("palaceoftruth", register_skills=False)
        assert isinstance(provider, MemoryProvider), "Real host loader failed to instantiate Palace"
        assert Path(inspect.getfile(MemoryProvider)).resolve().is_relative_to(host)
        assert Path(inspect.getfile(MemoryManager)).resolve().is_relative_to(host)
        assert Path(inspect.getfile(type(provider))).resolve().is_relative_to(copied)
        assert provider.name == "palaceoftruth"
        assert provider.is_available() is False, "Uncredentialed provider unexpectedly available"
        gates[current] = {"status": "passed", "base_class": str(Path(inspect.getfile(MemoryProvider)).relative_to(host))}

        current = "registration"
        provider.initialize("compat-session", hermes_home=str(scratch / ".hermes"), platform="cli", agent_context="primary", agent_identity="compat-test", agent_workspace="compat-workspace")
        manager = MemoryManager()
        manager.add_provider(provider)
        assert manager.get_provider("palaceoftruth") is provider
        schemas = manager.get_all_tool_schemas()
        names = {item["name"] for item in schemas}
        assert names and "palace_search" in names
        assert manager.get_all_tool_names() == names
        assert len(names) == len(schemas), "Duplicate tool schemas"
        assert "Palace of Truth" in manager.build_system_prompt()
        gates[current] = {"status": "passed", "tool_names": sorted(names)}

        current = "lifecycle"
        # Observe real methods (not stub replacements). Manager deliberately
        # swallows provider exceptions; observations make swallowed errors fail.
        observed, completed, errors = [], [], []
        def wrap_observation(name, original, prepared=False):
            @functools.wraps(original)
            def wrapped(*args, **kwargs):
                observed.append(name)
                try:
                    result = original(*args, **kwargs)
                    if prepared and result is not None:
                        if callable(result):
                            result = wrap_observation(name + ".complete", result)
                        else:
                            publish, complete = result
                            result = (wrap_observation(name + ".publish", publish),
                                      wrap_observation(name + ".complete", complete))
                    completed.append(name)
                    return result
                except Exception as exc:
                    errors.append(f"{name}: {type(exc).__name__}: {exc}")
                    raise
            return wrapped

        hooks = ("on_turn_start", "prefetch", "queue_prefetch", "sync_turn", "on_memory_write", "on_delegation", "on_session_switch", "on_session_end", "shutdown")
        for hook in hooks:
            setattr(provider, hook, wrap_observation(hook, getattr(provider, hook)))
        for hook in ("prepare_memory_write", "prepare_sync_turn", "prepare_session_boundary"):
            original = getattr(provider, hook, None)
            if callable(original):
                setattr(provider, hook, wrap_observation(hook, original, prepared=True))
        messages = [{"role": "user", "content": "Offline compatibility fixture"}, {"role": "assistant", "content": "Fixture response"}]
        manager.on_turn_start(1, messages[0]["content"], author_id="fixture", author_name="Fixture", author_is_bot=False)
        assert isinstance(manager.prefetch_all("Offline compatibility fixture", session_id="compat-session"), str)
        manager.sync_all(messages[0]["content"], messages[1]["content"], session_id="compat-session", messages=messages, turn_author={"id": "fixture"})
        manager.queue_prefetch_all("Offline compatibility fixture", session_id="compat-session")
        assert manager.flush_pending(timeout=3), "Background hooks did not drain"
        # Admit an actual mirror using only in-memory transport. A no-auth
        # prepare returning None cannot prove that its completion is tracked.
        provider._has_api_auth = lambda: True
        provider._resolve_tenant_id = lambda: "offline-tenant"
        provider._request_json = lambda *args, **kwargs: {"ok": True}
        manager.on_memory_write("add", "memory", "Offline fixture", metadata={"source": "contract-test"})
        assert manager.flush_pending(timeout=3)
        manager.on_delegation("Offline fixture task", "Offline fixture result", child_session_id="compat-child")
        manager.on_session_switch("compat-resumed", parent_session_id="compat-session", rewound=True)
        assert provider._session_id == "compat-resumed"
        manager.commit_session_boundary_async(messages, new_session_id="compat-new", parent_session_id="compat-resumed")
        assert manager.flush_pending(timeout=3)
        assert not errors, errors
        assert provider._session_id == "compat-new"
        manager.shutdown_all()
        assert not errors, errors
        assert set(hooks) - {"on_memory_write", "sync_turn"} <= set(completed), completed
        assert "sync_turn" in completed or {"prepare_sync_turn.publish", "prepare_sync_turn.complete"} <= set(completed), completed
        assert "on_memory_write" in completed or "prepare_memory_write.complete" in completed, completed
        if "prepare_session_boundary.publish" in completed:
            assert "prepare_session_boundary.complete" in completed, completed
            assert completed.index("on_session_end") < completed.index("prepare_session_boundary.complete") < completed.index("shutdown"), completed
        else:
            assert completed[-3:] == ["on_session_end", "on_session_switch", "shutdown"], completed
        drain = manager.shutdown_drain_state
        assert drain == {"status": "drained", "abandoned_writes": 0, "abandoned_prefetches": 0, "active_tasks": 0}, drain
        gates[current] = {"status": "passed", "observed_hooks": observed, "shutdown_drain": drain, "scope": "real-provider lifecycle with simulated auth and in-memory transport; no credentials or remote durability"}

        current = "write_completion"
        gates[current], gates["global_drain"] = probe_write_completion(type(provider), MemoryManager)

        current = "tool_dispatch"
        result = json.loads(manager.handle_tool_call("palace_search", {}))
        assert result.get("ok") is False and "query" in result.get("error", "")
        assert json.loads(manager.handle_tool_call("unknown_compat_tool", {})).get("error")
        gates[current] = {"status": "passed", "scope": "real schema routing + local validation"}

        current = "checkpoint"
        assert isinstance(manager.on_pre_compress(messages), str)
        version = getattr(provider, "pre_compress_checkpoint_api_version", 1)
        supported = manager.supports_pre_compress_checkpoint()
        if supported:
            # Do not mistake an advertised version/successful no-op for durability.
            raise AssertionError("Checkpoint v2 advertised: add durable evidence/failure acceptance fixtures (SAR-1406) before accepting")
        try:
            manager.on_pre_compress(messages, evidence_messages=messages, require_checkpoint=True)
        except RuntimeError as exc:
            assert "checkpoint" in str(exc).lower(), str(exc)
        else:
            raise AssertionError("Strict checkpoint unexpectedly accepted unsupported provider")
        gates[current] = {"status": "supported_limitation", "provider_api_version": version, "strict_checkpoint_supported": False, "reason": "Legacy v1 best-effort hook is valid; strict durable v2 checkpoint awaits SAR-1406"}

        current = "context_helper"
        if context_smoke:
            from agent.context_compressor import ContextCompressor
            signature = inspect.signature(ContextCompressor)
            kwargs = {"model": "compat-offline", "config_context_length": 8192, "quiet_mode": True}
            signature.bind(**kwargs)
            helper = ContextCompressor(**kwargs)
            assert helper.model == "compat-offline"
            gates[current] = {"status": "passed", "signature": str(signature), "scope": "constructor only; no model calls/compression"}
        else:
            gates[current] = {"status": "not_requested"}
        assert not violations, f"Forbidden operations attempted (even if swallowed): {violations}"
    except Exception as exc:
        gates[current] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    if violations:
        gates["isolation"] = {"status": "failed", "violations": violations}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, required=True)
    parser.add_argument("--plugin-root", type=Path, help="Trusted extracted plugin directory (default: this repository)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-gate", action="append", choices=GATES, default=[])
    parser.add_argument("--context-helper-smoke", action="store_true")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--child-package-paths", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_package_paths is not None:
        report = child_probe(args.hermes_root, json.loads(args.child_package_paths), args.context_helper_smoke)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        return 0

    report = {"schema_version": 1, "host_sha": None, "required_gates": args.require_gate, "success": False, "gates": {}}
    try:
        host = args.hermes_root.resolve(strict=True)
        for required in ("agent/memory_provider.py", "agent/memory_manager.py", "plugins/memory/__init__.py"):
            if not (host / required).is_file():
                raise ValueError(f"Not a real Hermes checkout: missing {required}")
        # No hooks, inherited git config, fsmonitor, or credential helpers.
        git_env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        report["host_sha"] = subprocess.check_output(["git", "-C", str(host), "rev-parse", "HEAD"], text=True, env=git_env, timeout=5).strip()
        package = args.plugin_root or Path(__file__).resolve().parents[1] / "third_party_plugins/hermes/memory/palaceoftruth"
        with tempfile.TemporaryDirectory(prefix="palace-hermes-contract-") as directory:
            scratch = Path(directory)
            shutil.copytree(package, scratch / ".hermes/plugins/palaceoftruth", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (scratch / ".hermes/config.yaml").write_text("memory:\n  provider: palaceoftruth\nplugins:\n  enabled: []\n")
            child_output = scratch / "report.json"
            env = {"HOME": directory, "HERMES_HOME": str(scratch / ".hermes"), "HERMES_ENABLE_PROJECT_PLUGINS": "0", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
            command = [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--hermes-root", str(host), "--output", str(child_output), "--child-package-paths", json.dumps(site.getsitepackages())]
            if args.context_helper_smoke:
                command.append("--context-helper-smoke")
            result = subprocess.run(command, env=env, cwd=scratch, text=True, capture_output=True, timeout=max(1, min(args.timeout, 180)))
            if result.returncode or not child_output.exists():
                raise RuntimeError(f"Probe child failed ({result.returncode}): {result.stderr[-4000:]}")
            report.update(json.loads(child_output.read_text()))
            report["success"] = all(g["status"] in {"passed", "supported_limitation", "not_requested"} for g in report["gates"].values()) and all(report["gates"][name]["status"] == "passed" for name in args.require_gate)
    except Exception as exc:
        report["gates"]["setup"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
