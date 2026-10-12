"""Deterministic source package only; never installs or publishes."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ('__init__.py', 'checkpoint.py', 'outbox.py', 'credentials.py', 'cli.py', 'plugin.yaml', 'README.md', 'Dockerfile')


def build(output):
    plugin = ROOT / 'third_party_plugins/hermes/memory/palaceoftruth'
    version = next(line.split(':', 1)[1].strip() for line in (plugin / 'plugin.yaml').read_text().splitlines() if line.startswith('version:'))
    entries = {f'palaceoftruth/{name}': (plugin / name).read_bytes() for name in FILES}
    entries['scripts/recover_hermes_outbox.py'] = (ROOT / 'scripts/recover_hermes_outbox.py').read_bytes()
    inventory = {'version': version, 'contract': 'committed-record-recovery-only', 'released': False,
                 'files': {name: hashlib.sha256(body).hexdigest() for name, body in sorted(entries.items())}}
    entries['inventory.json'] = (json.dumps(inventory, sort_keys=True, indent=2) + '\n').encode()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, 'w', format=tarfile.USTAR_FORMAT) as archive:
        for name, body in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(body))
    return {**inventory, 'archive_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    print(json.dumps(build(parser.parse_args().output), sort_keys=True))
