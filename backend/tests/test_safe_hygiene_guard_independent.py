"""Independent cleanup repair probes: synthetic text only, no live env/credentials."""
from pathlib import Path
import importlib.util,json
import pytest
spec=importlib.util.spec_from_file_location('guard_review',Path(__file__).parent.parent/'scripts/safe_hygiene_guard.py');g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
MARK='SYNTHETIC_NON_SECRET_REVIEW_MARKER'

def test_plain_true_positive():assert g.scan_blob(Path('backend/config.py'),'api_key = '+repr(MARK))
def test_placeholder_is_documented_name_only_false_positive():assert g.scan_blob(Path('backend/config.py'),'api_key = "${FIXTURE_ONLY}"')
def test_artifact_hash_field_negative():assert not g.scan_blob(Path('backend/config.py'),'manifest_sha256 = "'+'a'*64+'"')
def test_old_sample_diagnostic_inverse():
 rows=g.scan_blob(Path('backend/config.py'),'api_key = '+repr(MARK));flat=json.dumps(rows)
 assert not {'sample','length','fingerprint','shape_fingerprint','value','prefix','suffix'}.intersection(rows[0]);assert MARK not in flat and MARK[:4] not in flat and MARK[-4:] not in flat

def test_receipt_boundary_inverse():
 assert g.is_allowed_text_file(Path('receipts/all-green.xml'));assert g.scan_blob(Path('receipts/all-green.xml'),'api_key = '+repr(MARK))
@pytest.mark.parametrize('path',['backend/tests/case.py','docs/note.md','.github/workflows/build.yml'])
def test_three_documented_exclusions_remain_honest(path):assert not g.is_allowed_text_file(Path(path))
def test_prefixed_env_field_inverse():assert g.scan_blob(Path('backend/config.py'),'TYPESAFE_API_KEY = '+repr(MARK))
def test_original_json_safety_inverse():assert g.scan_blob(Path('backend/config.json'),json.dumps({'api_key':MARK}))
def test_hex_candidate_inverse():assert g.scan_blob(Path('backend/config.py'),'api_key = "'+'a'*64+'"')

@pytest.mark.parametrize('path',['.env','.env.example','build/report.json','receipts/test_config.xml'])
def test_actual_promised_publication_paths_eligible(path):assert g.is_allowed_text_file(Path(path))
def test_real_xml_assertion_context_detected():
 blob='<testsuite><testcase><failure>E assert Config(api_key="'+MARK+'") == fixture</failure></testcase></testsuite>'
 assert g.scan_blob(Path('receipts/all-green.xml'),blob)
