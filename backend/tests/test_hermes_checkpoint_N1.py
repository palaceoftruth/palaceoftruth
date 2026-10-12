import threading,time,sys,hashlib
import pytest
from pathlib import Path
from tests.test_hermes_checkpoint_native_export import ready


def test_checkpoint_deadline_preserves_pin_until_accounting_released(monkeypatch):
    from tests.test_hermes_checkpoint_repair import wire,event
    _,p=ready();wire(p);monkeypatch.setattr(type(p),'CHECKPOINT_TOTAL_SECONDS',.04)
    original=p._checkpoint_request_json
    held=threading.Event();release=threading.Event();done=threading.Event();errors=[]
    def holder():
        with p._lifecycle_lock:held.set();release.wait(2)
    h=threading.Thread(target=holder)
    def transport(method,path,payload=None,**kw):
        result=original(method,path,payload,**kw)
        if method=='GET':h.start();assert held.wait(1)
        return result
    p._checkpoint_request_json=transport
    def call():
        try:p.on_pre_compress([event()],require_checkpoint=True)
        except RuntimeError:errors.append(True)
        finally:done.set()
    t=threading.Thread(target=call);t.start();assert held.wait(1)
    try:
        assert done.wait(.15) and errors==[True]
        assert p._active_writes==1 and p._session_quota_jobs[p._session_id]==1
        assert not p.flush_pending(timeout=.02)
    finally:release.set();h.join(1);t.join(1)
    assert p.flush_pending(timeout=1)
    assert p._session_quota_jobs.get(p._session_id,0)==0
    assert p.shutdown_drain_state['failed_writes']==1
    assert p.shutdown_drain_state['active_tasks']==0


def test_normal_loader_bytes_and_native_export():
    _,p=ready(); module=sys.modules[type(p).__module__]
    source=Path(__file__).resolve().parents[2]/'third_party_plugins/hermes/memory/palaceoftruth'
    installed=Path(module.__file__).parent
    for f in ['__init__.py','checkpoint.py']:
        assert (source/f).read_bytes()==(installed/f).read_bytes()
    assert hasattr(p,'_write_queue') and p._admission_closed is not None


def test_multiwaiter_selected_generation_keeps_completion_wakeup():
    _,p=ready(); entered=threading.Event(); release=threading.Event()
    def worker():entered.set(); release.wait(2)
    with p._lifecycle_lock:p._queue_write(worker,p._quota_for_session(p._session_id))
    assert entered.wait(1)
    underlying=p._work_changed; about_to_wait=threading.Event(); resume=threading.Event()
    class GatedEvent:
        def clear(self):underlying.clear()
        def set(self):underlying.set()
        def wait(self,timeout=None):
            if threading.current_thread().name=='first-flush':
                about_to_wait.set(); assert resume.wait(1)
            return underlying.wait(timeout)
    p._work_changed=GatedEvent()
    results=[]
    first=threading.Thread(name='first-flush',target=lambda:results.append(p.flush_pending(timeout=.3)))
    first.start(); assert about_to_wait.wait(1)
    try:
        release.set()
        end=time.perf_counter()+1
        while time.perf_counter()<end:
            with p._lifecycle_lock:
                if not p._active_writes and not p._write_queue:break
            time.sleep(.001)
        assert underlying.is_set()
        assert p.flush_pending(timeout=.2) is True
        assert underlying.is_set()
    finally:
        resume.set(); release.set(); first.join(1)
    assert results==[True], 'Selected completion notification must remain published for every waiter'
    state=p.shutdown_drain_state
    assert state['accepted_writes']==state['completed_writes']==1
    assert state['active_tasks']==state['queued_writes']==0


def test_shutdown_gate_closes_under_held_lock_then_rejects_capture():
    _,p=ready(); entered=threading.Event(); release=threading.Event()
    def holder():
        with p._lifecycle_lock:entered.set();release.wait(1)
    t=threading.Thread(target=holder);t.start();assert entered.wait(1)
    start=time.perf_counter();p.shutdown(timeout=.03)
    assert time.perf_counter()-start<.15 and p._admission_closed.is_set()
    release.set();t.join(1)
    import pytest
    with pytest.raises(RuntimeError):p.sync_turn('x','y',session_id=p._session_id)
    assert p.shutdown_drain_state['accepted_writes']==0


@pytest.mark.parametrize('transition', ['completion', 'admission', 'close'])
@pytest.mark.parametrize('failed', [False, True])
def test_multiple_waiters_generation_completion_admission_close(transition, failed):
    _, p = ready()
    entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    release_second = threading.Event()
    resume = threading.Event()
    selected = [threading.Event() for _ in range(3)]
    finished = [threading.Event() for _ in range(3)]
    results = [None] * 3
    def worker():
        entered.set()
        assert release.wait(2)
        if failed:
            raise RuntimeError('fixture write failure, not dropped accounting')
    def second_worker():
        second_entered.set()
        assert release_second.wait(2)
    with p._lifecycle_lock:
        p._queue_write(worker, p._quota_for_session(p._session_id))
    assert entered.wait(1)
    underlying = p._work_changed
    class SelectedGeneration:
        def clear(self):
            pytest.fail('Selected notifications are never consumable')
        def set(self):
            underlying.set()
        def wait(self, timeout=None):
            name = threading.current_thread().name
            if name.startswith('flush-'):
                index = int(name.rsplit('-', 1)[1])
                selected[index].set()
                assert resume.wait(1)
            return underlying.wait(timeout)
    p._work_changed = SelectedGeneration()  # Scheduling control only.
    def flush(index):
        try:
            results[index] = p.flush_pending(timeout=.8)
        finally:
            finished[index].set()
    threads = [threading.Thread(name=f'flush-{i}', target=flush, args=(i,)) for i in range(3)]
    try:
        for thread in threads:
            thread.start()
        assert all(signal.wait(1) for signal in selected)
        if transition == 'admission':
            with p._lifecycle_lock:
                p._queue_write(second_worker, p._quota_for_session(p._session_id))
            assert underlying.is_set()
            release.set()
            assert second_entered.wait(1)
            resume.set()
            assert not any(signal.wait(.02) for signal in finished)
            with p._lifecycle_lock:
                assert p._active_writes == 1
                assert p._session_quota_jobs[p._session_id] == 1
            release_second.set()
        elif transition == 'close':
            p.shutdown(timeout=.02)
            assert underlying.is_set() and p._admission_closed.is_set()
            with pytest.raises(RuntimeError):
                p.sync_turn('late', 'late', session_id=p._session_id)
            release.set()
            resume.set()
        else:
            release.set()
            assert p.flush_pending(timeout=.3)
            assert underlying.is_set()  # Late callers cannot consume this generation.
            resume.set()
        assert all(signal.wait(1) for signal in finished)
    finally:
        resume.set(); release.set(); release_second.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(1)
        assert p.flush_pending(timeout=1)
    assert results == [True, True, True]
    state = p.shutdown_drain_state
    accepted = 2 if transition == 'admission' else 1
    assert state['accepted_writes'] == accepted
    assert state['failed_writes'] == int(failed)
    assert state['completed_writes'] == accepted - int(failed)
    assert state['active_tasks'] == state['queued_writes'] == 0
    assert not p._session_quota_jobs
