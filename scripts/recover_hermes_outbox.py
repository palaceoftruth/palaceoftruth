"""Finite, explicit source-prototype recovery. Never installs/starts a service."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--binding-file', type=Path, required=True,
                        help='Expected non-secret tenant/principal/origin/scope binding')
    parser.add_argument('--max-jobs', type=int, default=10)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--ca-file', default=None)
    args = parser.parse_args()
    # Own plugin's pure-stdlib modules, not Hermes internals or a provider factory.
    try:
        root = Path(__file__).resolve().parents[1]
        packaged = root / 'palaceoftruth'
        modules = packaged if packaged.exists() else root / 'third_party_plugins/hermes/memory/palaceoftruth'
        # Explicit adjacent layout only. Never search cwd/PYTHONPATH for replacements.
        import importlib.util
        import types
        required = ('outbox', 'credentials', 'checkpoint')
        if any(not (modules / (name + '.py')).is_file() for name in required):
            raise RuntimeError('recovery package incomplete')
        namespace = '_palace_recovery_artifact'
        package = types.ModuleType(namespace)
        package.__path__ = [str(modules)]
        sys.modules[namespace] = package
        loaded = {}
        for name in required:
            spec = importlib.util.spec_from_file_location(namespace + '.' + name, modules / (name + '.py'))
            if spec is None or spec.loader is None:
                raise RuntimeError('recovery package unavailable')
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        OutboxStore, RecoveryWorker, RestTransport = (getattr(loaded['outbox'], name) for name in ('OutboxStore', 'RecoveryWorker', 'RestTransport'))
        from_environment = loaded['credentials'].from_environment
        binding = json.loads(args.binding_file.read_text())
        # Never source a host/profile dotenv; no auth on disk, command args or logs.
        store = OutboxStore(args.directory, binding, create=False)
        credential = from_environment(store.binding, ca_file=args.ca_file)
        result = RecoveryWorker(store, RestTransport(store.binding, credentials=credential, ca_file=args.ca_file)).run(
            max_jobs=args.max_jobs, timeout=args.timeout)
        print(json.dumps(result, sort_keys=True))
        return 2 if result['failed'] else 0
    except Exception:
        print(json.dumps({'remote_durable': 0, 'error': 'recovery_not_proven', 'host_submission_complete': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
