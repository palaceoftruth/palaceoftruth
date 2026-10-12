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
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'third_party_plugins/hermes/memory/palaceoftruth'))
    from outbox import OutboxStore, RecoveryWorker, RestTransport
    try:
        binding = json.loads(args.binding_file.read_text())
        # Never source a host/profile dotenv; no auth on disk, command args or logs.
        credential = os.environ.get('PALACE_OUTBOX_API_KEY', '')
        store = OutboxStore(args.directory, binding, create=False)
        result = RecoveryWorker(store, RestTransport(binding, credential, ca_file=args.ca_file)).run(
            max_jobs=args.max_jobs, timeout=args.timeout)
        print(json.dumps(result, sort_keys=True))
        return 2 if result['failed'] else 0
    except Exception:
        print(json.dumps({'remote_durable': 0, 'error': 'recovery_not_proven', 'host_submission_complete': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
