"""Path/line/rule only. Synthetic fixtures, no real credentials, no values logged.

The new guard contract is: only path, line and rule id
are emitted. The guard does not inspect, hash, sample,
length, normalise or print any assigned value. Tests below prove both
the true-positive rule emission AND the explicit no-value-derivative
output contract.
"""
from pathlib import Path
import importlib.util, json, subprocess
import pytest
spec=importlib.util.spec_from_file_location('repair_guard',Path(__file__).parent.parent/'scripts/safe_hygiene_guard.py')
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
VALUE='SYNTHETIC_NON_SECRET_REVIEW_MARKER'

FORBIDDEN_KEYS={'value','length','sample','shape_fingerprint','hash','fingerprint','prefix','suffix','first2','last2','redact','start','end','truncate','head','tail'}
ALLOWED_KEYS={'path','line','rule'}


def _emits_only_paths_rules(finding):
    for k in finding:
        assert k not in FORBIDDEN_KEYS, f"forbidden key {k!r} in finding {finding!r}"
    assert set(finding)==ALLOWED_KEYS, f"unexpected keys {set(finding)}"


def _run_guard_main():
    return subprocess.run(['python3',str(Path(__file__).parent.parent/'scripts/safe_hygiene_guard.py'),'HEAD'],capture_output=True,text=True,check=True)


def test_plain_credential_assignment_true_positive():
    rows=g.scan_blob(Path('backend/config.py'),'api_key = "'+VALUE+'"\n')
    assert len(rows)==1
    assert rows[0]['rule']=='rule:credential-shaped-api-key-assignment'
    _emits_only_paths_rules(rows[0])


@pytest.mark.parametrize('line,expected',[
 ('manifest_sha256 = "'+'a'*64+'"\n',[]),
 ('body_sha256 = "'+'a'*64+'"\n',[]),
 ('key = "literal-app-name"\n',[]),
 ('credentials_count = 5\n',[]),
 ('credentials_dir = "fixture-only"\n',[]),
 ('scope_type = "workspace"\n',[]),
 ('public_id = "literal-app-name"\n',[]),
])
def test_app_defined_field_names_are_excluded_negatives(line,expected):
    assert g.scan_blob(Path('backend/config.py'),line)==expected


@pytest.mark.parametrize('path,expected_category',[
 ('backend/app/auth.py','category:secret-handling'),
 ('backend/secrets/api.yaml','category:secret-handling'),
 ('backend/app/credentials.py','category:secret-handling'),
 ('backend/tests/test_a.py','category:excluded'),
 ('docs/INTEGRATIONS.md','category:excluded'),
 ('.github/workflows/build-push.yml','category:excluded'),
 ('receipts/all-green.xml','category:publication-surface'),
 ('receipts/INDEX.md','category:publishable-source'),
 ('private-retained-receipts/all-green.xml','category:publication-surface'),
 ('.env.example','category:publication-surface'),
 ('.env','category:publication-surface'),
 ('junit/results.xml','category:publication-surface'),
 ('build/output.json','category:publication-surface'),
 ('backend/app/router.py','category:publishable-source'),
])
def test_path_classification(path,expected_category):
    assert g.classify_path(Path(path))==expected_category


def test_credential_assignment_in_actual_publication_surface():
    rows=g.scan_blob(Path('receipts/test.xml'),'api_key = "'+VALUE+'"\n')
    assert len(rows)==1
    assert rows[0]['rule']=='rule:credential-shaped-api-key-assignment'
    _emits_only_paths_rules(rows[0])


def test_credential_assignment_in_actual_secret_handling_surface():
    rows=g.scan_blob(Path('backend/app/auth.py'),'api_key = "'+VALUE+'"\n')
    assert len(rows)==1
    assert rows[0]['rule']=='rule:credential-shaped-api-key-assignment'
    _emits_only_paths_rules(rows[0])


@pytest.mark.parametrize('line,expected_rule',[
 ('TYPESAFE_API_KEY = "'+VALUE+'"\n','rule:credential-shaped-api-key-assignment'),
 ('PRIVATE_KEY = "'+VALUE+'"\n','rule:credential-shaped-private-key-assignment'),
 ('CLIENT_SECRET = "'+VALUE+'"\n','rule:credential-shaped-client-secret-assignment'),
 ('ADMIN_SECRET = "'+VALUE+'"\n','rule:credential-shaped-admin-secret-assignment'),
 ('REFRESH_TOKEN = "'+VALUE+'"\n','rule:credential-shaped-refresh-token-assignment'),
 ('SIGNING_KEY = "'+VALUE+'"\n','rule:credential-shaped-signing-key-assignment'),
 ('ENCRYPTION_KEY = "'+VALUE+'"\n','rule:credential-shaped-encryption-key-assignment'),
 ('HMAC = "'+VALUE+'"\n','rule:credential-shaped-hmac-assignment'),
 ('JWT = "'+VALUE+'"\n','rule:credential-shaped-jwt-assignment'),
 ('OAUTH = "'+VALUE+'"\n','rule:credential-shaped-oauth-assignment'),
 ('BEARER = "'+VALUE+'"\n','rule:credential-shaped-bearer-assignment'),
 ('AUTHORIZATION = "'+VALUE+'"\n','rule:credential-shaped-authorization-assignment'),
 ('PASSWORD = "'+VALUE+'"\n','rule:credential-shaped-password-assignment'),
 ('CREDENTIAL = "'+VALUE+'"\n','rule:credential-shaped-credential-assignment'),
 ('TOKEN = "'+VALUE+'"\n','rule:credential-shaped-token-assignment'),
 ('SECRET = "'+VALUE+'"\n','rule:credential-shaped-secret-assignment'),
 ('API_KEY = "'+VALUE+'"\n','rule:credential-shaped-api-key-assignment'),
 ('MY_BEARER_TOKEN = "'+VALUE+'"\n','rule:credential-shaped-bearer-assignment'),
 ('AUTHORIZATION_HEADER = "'+VALUE+'"\n','rule:credential-shaped-authorization-assignment'),
 ('AUTH_TOKEN = "'+VALUE+'"\n','rule:credential-shaped-token-assignment'),
])
def test_rule_for_field(line,expected_rule):
    rows=g.scan_blob(Path('backend/config.py'),line)
    assert len(rows)==1, rows
    assert rows[0]['rule']==expected_rule, rows
    _emits_only_paths_rules(rows[0])


def test_hex_credential_value_is_NOT_excluded_from_finding_emit():
    rows=g.scan_blob(Path('backend/config.py'),'api_key = "'+'a'*64+'"\n')
    assert len(rows)==1
    assert rows[0]['rule']=='rule:credential-shaped-api-key-assignment'
    _emits_only_paths_rules(rows[0])


def test_output_never_contains_value_substring_of_reviewer_fixture():
    rows=g.scan_blob(Path('backend/config.py'),'api_key = "'+VALUE+'"\n')
    flat=json.dumps(rows)
    assert VALUE not in flat
    assert VALUE[:4] not in flat
    assert VALUE[-4:] not in flat
    assert str(len(VALUE)) not in flat


def test_main_output_only_contains_path_line_field_rule_category():
    proc=_run_guard_main()
    assert proc.returncode==0
    payload=json.loads(proc.stdout)
    assert set(payload) >= {'scanned','policy','rule_ids','findings','finding_count'}
    assert payload['policy']=='review-aid-only; no value, length, sample, hash, or fingerprint is ever emitted'
    for f in payload['findings']:
        _emits_only_paths_rules(f)
    for r in payload['rule_ids']:
        assert r.startswith('rule:credential-shaped-') or r.startswith('category:') or r=='rule:credential-shaped-assignment', r


def test_main_output_has_no_value_substring_across_excluded_publication_files(tmp_path):
    rows=g.scan_blob(Path('receipts/all-green.xml'),'api_key = "'+VALUE+'"\n')
    assert len(rows)==1
    for f in rows:
        flat=json.dumps(f)
        assert VALUE not in flat
        assert VALUE[:4] not in flat
