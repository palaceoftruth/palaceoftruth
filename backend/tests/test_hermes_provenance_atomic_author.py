"""R1: real line-traced submission races, without replacing provider methods."""
import inspect
import sys
import threading

import pytest
from tests.test_hermes_provenance import provider


@pytest.mark.parametrize('requested,later,origin,execution,explicit,expected', [
    ('other', 'other', 'assistant_tool', 'foreground', None, {}),
    ('shared', 'other', 'assistant_tool', 'foreground', None, {'id': 'alice', 'is_bot': False}),
    ('shared', 'shared', 'assistant_tool', 'foreground', None, {'id': 'alice', 'is_bot': False}),
    ('other', 'other', 'background_review', 'background_review', None, {}),
    ('shared', 'shared', 'background_review', 'foreground', None, {}),
    ('shared', 'shared', 'assistant_tool', 'background_review', None, {}),
    ('other', 'other', 'background_review', 'background_review', {'id': 'event', 'is_bot': True}, {'id': 'event', 'is_bot': True}),
    ('shared', 'shared', 'assistant_tool', 'foreground', {}, {}),
])
def test_atomic_author_eligibility_across_session_and_participant_switch(requested, later, origin, execution, explicit, expected):
    _, p = provider()
    p._scope_type, p._scope_key = 'session', ''
    p.on_turn_start(1, 'Alice', author_id='alice', author_is_bot=False)
    writes, errors = [], []
    p._post_memory_entries = lambda path, payload: writes.append(payload)
    ready, resume = threading.Event(), threading.Event()
    function = p.on_memory_write.__func__
    source, start = inspect.getsourcelines(function)
    pause_line = start + next(i for i, line in enumerate(source) if 'foreground_session = snapshot[' in line)
    metadata = {'session_id': requested, 'write_origin': origin, 'execution_context': execution}
    if explicit is not None:
        metadata['turn_author'] = explicit
    def trace(frame, event, arg):
        if event == 'line' and frame.f_code is function.__code__ and frame.f_lineno == pause_line:
            ready.set()
            assert resume.wait(4)
        return trace
    def submit():
        try:
            sys.settrace(trace)
            p.on_memory_write('remove', 'memory', '', metadata=metadata)
        except BaseException as exc:
            errors.append(exc)
        finally:
            sys.settrace(None)
    caller = threading.Thread(target=submit)
    try:
        caller.start()
        assert ready.wait(2)
        if later != 'shared':
            p.on_session_switch(later)
        p.on_turn_start(2, 'Bob', author_id='bob', author_is_bot=True)
        if explicit:
            explicit['id'] = 'mutated-caller'
        resume.set()
        caller.join(2)
        assert not caller.is_alive() and not errors
        assert p.flush_pending(timeout=3)
        assert writes[0]['scope'] == {'type': 'session', 'key': requested}
        assert writes[0]['metadata']['write_provenance']['turn_author'] == expected
        assert p.shutdown_drain_state['accepted_writes'] == p.shutdown_drain_state['completed_writes'] == 1
    finally:
        resume.set()
        caller.join(3)
        p.shutdown()
