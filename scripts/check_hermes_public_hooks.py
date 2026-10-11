"""Exercise public plugin registration and real hook fail-open semantics."""
import argparse
import inspect
import json
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--host', type=Path, required=True)
parser.add_argument('--receipt', type=Path, required=True)
args = parser.parse_args()
host = args.host.resolve()
sys.path.insert(0, str(host))
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest, VALID_HOOKS
from hermes_cli.middleware import VALID_MIDDLEWARE
assert Path(inspect.getfile(PluginContext)).resolve() == host / 'hermes_cli/plugins.py'
manager = PluginManager()
ctx = PluginContext(PluginManifest(name='offline-admission-feasibility', source='user'), manager)
seen = []
def barrier_failure(**kwargs):
    seen.append('barrier-failed')
    raise RuntimeError('offline drain deadline exceeded')
def later_observer(**kwargs):
    seen.append('later-observer-ran')
ctx.register_hook('on_session_finalize', barrier_failure)
ctx.register_hook('on_session_finalize', later_observer)
result = manager.invoke_hook('on_session_finalize', session_id='offline-session', platform='cli')
assert seen == ['barrier-failed','later-observer-ran'], seen
receipt = {
    'public_registration': 'passed',
    'host_monkeypatch': False,
    'failure_propagated': False,
    'subsequent_callback_ran': True,
    'results': result,
    'middleware_kinds': sorted(VALID_MIDDLEWARE),
    'documented_lifecycle_hooks_present': sorted(VALID_HOOKS.intersection({'pre_llm_call','post_llm_call','on_session_start','on_session_end','on_session_finalize','on_session_reset','pre_command','pre_gateway_dispatch'})),
    'barrier_verdict': 'observer is not fail-closed completion authority',
}
args.receipt.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
