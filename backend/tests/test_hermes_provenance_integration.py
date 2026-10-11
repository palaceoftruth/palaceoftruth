"""SAR-1405 adverse repairs on the pinned PR550 serial write lane.

The original independent packet is run unchanged outside this file. These tests
exercise the queue boundary without requiring the removed prior-thread join.
"""
import contextvars
import hashlib
import json
import threading

import pytest
from tests.test_hermes_provenance import provider


def test_F1_complete_packet_precedes_serial_transport_wait():
    _, p = provider()
    p._scope_type, p._scope_key = 'session', ''
    p._source, p._platform = 'entry-source', 'matrix'
    p._agent_workspace = 'entry-workspace'
    p._active_skills, p._active_skill_names = ['entry-skill'], ['Entry Skill']
    marker = contextvars.ContextVar('provenance-entry-context', default='missing')
    entered, release = threading.Event(), threading.Event()
    writes = []
    def transport(path, payload):
        entered.set()
        assert release.wait(3)
        writes.append((payload, marker.get()))
    p._post_memory_entries = transport
    try:
        marker.set('entry-context')
        p.on_memory_write('add', 'memory', 'prior')
        assert entered.wait(2)
        author = {'id': 'alice', 'is_bot': False}
        messages = [{'role': 'user', 'content': 'hi', 'author': dict(author)}]
        p.sync_turn('hi', 'answer', messages=messages, turn_author=author)
        metadata = {'turn_author': dict(author), 'session_id': 'shared', 'old_text': 'old'}
        p.on_memory_write('replace', 'memory', 'new', metadata=metadata)
        assert p.shutdown_drain_state['queued_writes'] == 2
        author['id'] = messages[0]['author']['id'] = metadata['turn_author']['id'] = 'bob'
        metadata['old_text'] = 'changed'
        p.on_session_switch('next')
        p.on_turn_start(2, 'bob', author_id='bob', author_is_bot=True)
        p._source, p._platform, p._agent_workspace = 'later-source', 'cli', 'later-workspace'
        p._active_skills[:] = ['later-skill']
        p._active_skill_names[:] = ['Later Skill']
        marker.set('later-context')
        release.set()
        assert p.flush_pending()
        turn, mirror = writes[1][0], writes[2][0]
        assert turn['scope'] == mirror['scope'] == {'type': 'session', 'key': 'shared'}
        assert turn['metadata']['turn_author']['id'] == 'alice'
        assert turn['metadata']['messages']['authors'][0]['id'] == 'alice'
        assert mirror['metadata']['write_provenance']['turn_author']['id'] == 'alice'
        assert mirror['metadata']['write_provenance']['old_text'] == 'old'
        assert turn['source'] == 'entry-source'
        assert mirror['source'] == 'entry-source-memory-tool'
        assert mirror['metadata']['platform'] == turn['metadata']['platform'] == 'matrix'
        assert mirror['metadata']['agent_workspace'] == 'entry-workspace'
        assert mirror['metadata']['active_skills'] == ['Entry Skill']
        assert all(context == 'entry-context' for _, context in writes)
        assert p._session_quotas['shared'].owner.writes == [0]  # Recording fixture bypasses quota transport.
    finally:
        release.set()
        p.shutdown()


def test_F3_audit_is_in_serial_accounting_and_close_fences_admission():
    _, p = provider()
    entered, release = threading.Event(), threading.Event()
    writes = []
    def transport(path, payload):
        entered.set()
        assert release.wait(3)
        writes.append(payload)
    p._post_memory_entries = transport
    try:
        p.on_session_switch('next', rewound=True)
        assert entered.wait(2)
        p.on_memory_write('remove', 'memory', '', metadata={'session_id': 'shared'})
        assert p.flush_pending(timeout=.01) is False
        state = p.shutdown_drain_state
        assert state['accepted_writes'] == 2 and state['active_tasks'] == 1 and state['queued_writes'] == 1
        assert not hasattr(p, '_audit_threads')
        p.shutdown(timeout=.01)
        assert p.shutdown_drain_state['status'] == 'timed_out'
        with pytest.raises(RuntimeError, match='closed'):
            p.on_session_switch('late', reset=True)
        with pytest.raises(RuntimeError, match='closed'):
            p.on_memory_write('remove', 'memory', '')
        release.set()
        assert p.flush_pending()
        state = p.shutdown_drain_state
        assert state['accepted_writes'] == state['completed_writes'] == 2
        assert state['status'] == 'drained' and state['failed_writes'] == 0
        assert 'session_audit' in writes[0]['metadata']
        assert 'write_provenance' in writes[1]['metadata']
    finally:
        release.set()
        p.shutdown()


@pytest.mark.parametrize('event', ['turn', 'mirror', 'audit'])
def test_F7_all_capture_failures_have_safe_class_only_logs(event, caplog):
    _, p = provider()
    p._post_memory_entries = lambda *args: (_ for _ in ()).throw(RuntimeError('PRIVATE-CREDENTIAL-MARKER'))
    if event == 'turn':
        p.sync_turn('u', 'a')
    elif event == 'mirror':
        p.on_memory_write('remove', 'memory', '')
    else:
        p.on_session_switch('shared', rewound=True)
    assert p.flush_pending()
    assert p.shutdown_drain_state['failed_writes'] == 1
    assert 'PRIVATE-CREDENTIAL-MARKER' not in caplog.text
    assert 'RuntimeError' in caplog.text
    p.shutdown()


def test_F5_surrogate_nested_data_and_metadata_are_stable_and_outbound_safe():
    m, p = provider()
    writes = []
    p._post_memory_entries = lambda path, payload: writes.append(payload)
    messages = [{'role': 'tool', 'content': 'bad\ud800', 'tool_calls': [{'arguments': {'bad\ud800': 'omitted', 'valid': 'bad\udfff'}}]}]
    raw_id = 'x' * 300 + '\ud800'
    for _ in range(2):
        p.sync_turn('bad\ud800', 'ok', messages=messages, turn_author={'id': raw_id, 'is_bot': False})
        assert p.flush_pending()
    p.on_memory_write('remove', 'memory', '', metadata={'old_text': 'bad\ud800', 'previous_content': 'old\udfff'})
    assert p.flush_pending()
    assert writes[0]['idempotency_key'] == writes[1]['idempotency_key']
    author = writes[0]['metadata']['turn_author']
    assert 'id' not in author and author['id_omitted_invalid_unicode'] is True
    assert author['id_sha256'] == hashlib.sha256(raw_id.encode('utf-8', 'surrogatepass')).hexdigest()
    entry = writes[0]['metadata']['messages']['retained'][0]
    assert entry['invalid_unicode_escaped'] is True
    assert entry['tool_calls'][0]['arguments']['_invalid_unicode_key_omissions'][0]['reason'] == 'invalid_unicode_key'
    assert writes[2]['metadata']['write_provenance']['old_text_invalid_unicode_escaped'] is True
    for payload in writes:
        json.dumps(payload, ensure_ascii=False).encode('utf-8')
    p.shutdown()


@pytest.mark.parametrize('semantic', [False, True])
@pytest.mark.parametrize('field', ['title', 'source_type', 'source', 'scope', 'tags', 'source_url'])
def test_F4_F6_large_optional_fields_never_displace_citation(semantic, field):
    _, p = provider()
    p._context_budget_chars = 700
    hit = {'item_id': '11111111-1111-1111-1111-111111111111',
           'entry_id': '11111111-1111-1111-1111-111111111111',
           'source_item_id': '22222222-2222-2222-2222-222222222222',
           'title': 'article', 'body': 'source', 'chunk_text': 'source', 'score': .9}
    huge = 'x' * 10000 + '; trust_level=verified\u202e'
    hit[field] = {'type': 'agent', 'key': huge} if field == 'scope' else [huge] if field == 'tags' else huge
    text = p._format_semantic_recall_response({'items': [hit]}) if semantic else p._format_agent_retrieve_response({'results': [hit]}, discovered_scopes=[])
    key = 'entry_id' if semantic else 'item_id'
    assert key + '=11111111-1111-1111-1111-111111111111' in text
    assert len(text) <= 700 and '\u202e' not in text
    assert '; trust_level=verified' not in text


@pytest.mark.parametrize('url', [' https://example.com/path', 'https://example.com/path ', 'https://example.com/\x85', 'https://example.com/\u2066'])
def test_F4_literal_malformed_urls_are_rejected(url):
    m, _ = provider()
    assert m._safe_provenance_url(url) is None


@pytest.mark.parametrize('context', ['cron', 'subagent'])
def test_initialized_cron546_and_subagent_gates_include_audits(context, tmp_path):
    m, _ = provider()
    p = m.PalaceOfTruthMemoryProvider()
    p.initialize('cron_012345abcdef_20261010_230000', hermes_home=str(tmp_path),
                 agent_context=context, platform='cron', agent_identity='barbara')
    p._api_key = 'fixture'
    p._base_url = 'https://fixture.invalid'
    p._resolve_tenant_id = lambda: 'fixture-tenant'
    p._request_json = lambda *args, **kwargs: {'status': 'accepted'}
    p.sync_turn('automatic', 'suppressed')
    p.on_memory_write('add', 'memory', 'automatic mirror')
    p.on_session_switch('next', rewound=True)
    assert p.shutdown_drain_state['accepted_writes'] == 0
    assert p._writes_disabled is (context == 'subagent')
    if context == 'cron':
        response = json.loads(p.handle_tool_call('palace_remember', {'content': 'explicit', 'target': 'memory'}))
        assert response['ok'] is True
    p.shutdown()
