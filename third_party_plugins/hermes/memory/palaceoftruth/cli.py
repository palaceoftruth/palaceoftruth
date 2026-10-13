"""Finite recovery through the documented active-provider CLI extension.

Uses public profile home/secret scope. Never reads dotenv, changes services,
converts OAuth to API keys, or starts a persistent worker.
"""
import json
from pathlib import Path


def palaceoftruth_command(args):
    from hermes_constants import get_hermes_home
    from agent.secret_scope import get_secret
    from .outbox import OutboxStore,RestTransport,RecoveryWorker
    from .credentials import from_environment
    try:
        home=Path(get_hermes_home()).absolute()
        directory=Path(args.directory).absolute()
        if directory.parent!=home:
            raise RuntimeError('recovery directory outside selected profile')
        binding=json.loads(Path(args.binding_file).read_text())
        store=OutboxStore(directory,binding,create=False)
        credentials=from_environment(store.binding,ca_file=args.ca_file,secret_reader=get_secret)
        result=RecoveryWorker(store,RestTransport(store.binding,credentials=credentials,ca_file=args.ca_file)).run(
            max_jobs=args.max_jobs,timeout=args.timeout)
        print(json.dumps(result,sort_keys=True))
        if result['failed']:raise SystemExit(2)
        return 0
    except Exception:
        print(json.dumps({'error':'recovery_not_proven','remote_durable':0,'host_submission_complete':False}))
        raise SystemExit(2)


def register_cli(subparser):
    commands=subparser.add_subparsers(dest='palaceoftruth_subcommand',required=True)
    recover=commands.add_parser('recover-outbox',help='Finite prototype recovery; no service control')
    recover.add_argument('--directory',required=True)
    recover.add_argument('--binding-file',required=True)
    recover.add_argument('--max-jobs',type=int,default=10)
    recover.add_argument('--timeout',type=float,default=10)
    recover.add_argument('--ca-file',default=None)
    recover.set_defaults(func=palaceoftruth_command)
