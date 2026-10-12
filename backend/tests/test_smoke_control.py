"""Offline acceptance for conservative one-host smoke control. No live transports."""
import copy, hashlib, json, os, sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest
from scripts import smoke_control as control

NOW=datetime(2026,10,12,12,2,tzinfo=timezone.utc)
JOB='11111111-1111-4111-8111-111111111111'
ITEM='22222222-2222-4222-8222-222222222222'
ENTRY='33333333-3333-4333-8333-333333333333'

def doc_for(root):
    binding=control.initialize_store(root,host_id='fixture-host',store_id='fixture-store')
    baseline={'captured_at':'2026-10-12T12:00:00Z','workers':[{'pod':'worker-a','uid':'worker-uid','container':'worker','container_id':'containerd://fixed','restart_count':108,'ready':True,'phase':'Running','last_terminated':{'reason':'OOMKilled','exitCode':137,'finishedAt':'2026-10-12T01:37:20Z'}}]}
    payload={'tenant_id':'default','body':'Non-sensitive bounded smoke fixture.','title':'Approved smoke','summary':None,'source':'palace-rollout-smoke','created_at':'2026-10-12T12:00:00Z','tags':[],'scope':{'type':'workspace','key':'rollout-smoke'},'metadata':{'campaign':'fixture'},'enable_ai_enrichment':False,'relationship_policy':'deferred'}
    doc={'schema_version':1,'approval_id':'parent-fixture','campaign_id':'fixture-campaign','phase':'forward','api_base_url':'https://fixture.invalid/api/v1','cluster_uid':'cluster-uid','namespace':'fixture','namespace_uid':'namespace-uid','release_name':'fixture-release','release_uid':'release-uid','target_digest':'sha256:'+'1'*64,'chart_digest':'sha256:'+'2'*64,'tenant_id':'default','principal':{'mode':'mcp_oauth','id':JOB,'client_key':'fixture-client'},'scope':payload['scope'],'starts_at':'2026-10-12T12:01:00Z','expires_at':'2026-10-12T12:30:00Z','baseline':baseline,'baseline_sha256':control.sha256_json(baseline),'store_binding':binding,'max_POST_invocations':1,'max_new_records':1,'transport_retries':0,'payload':payload}
    key=control.slot_key(doc);doc['payload']['idempotency_key']=key;doc['payload']['tags']=['smoke-slot-'+key]
    return doc

def approval(doc,now=NOW):return control.Approval(doc,control.sha256_json(doc),now=now)
def gates():return {'alerts':[],'checks':[{'name':n,'status':'passed'} for n in control.REQUIRED_CHECKS]}
def observed(doc):return copy.deepcopy(doc['baseline']['workers'])
def logs(doc):return {('worker-uid','worker'):{'readable':True,'covers_since':'2026-10-12T11:02:00Z','observed_at':NOW.isoformat(),'text':'Starting worker; heartbeat healthy'}}
class Transport:
    base_url='https://fixture.invalid/api/v1'
    def __init__(self,doc):self.doc=doc;self.posts=[];self.record=False;self.corrupt=None
    def request(self,method,path,**kwargs):
        from scripts.check_memory_rollout_smoke import HttpResult
        if method=='POST':
            assert path=='/memory/entries';self.posts.append(copy.deepcopy(kwargs['body']));self.record=True
            return HttpResult(202,{'job_id':JOB,'contract_status':'accepted'})
        if path=='/memory/entries':return HttpResult(200,{'total':int(self.record),'next_cursor':None,'entries':[{'entry_id':ENTRY,'source_item_id':ITEM,'job_id':JOB,'scope':self.doc['scope']}] if self.record else []})
        r={'durable':True,'tenant_id':'default','scope':self.doc['scope'],'job_id':JOB,'source_item_id':ITEM,'idempotency_key':self.doc['payload']['idempotency_key'],'body_sha256':hashlib.sha256(self.doc['payload']['body'].encode()).hexdigest()}
        if self.corrupt:r[self.corrupt]='wrong'
        return HttpResult(200,r)

def test_unchanged_lifetime_counter_preserves_old_oom(tmp_path):
    d=doc_for(tmp_path/'store');r=control.temporal_gate(approval(d),observed(d),logs(d),now=NOW)
    assert r['workers'][0]['restart_delta']==0 and r['historical_oom'][0]['restart_count']==108

@pytest.mark.parametrize('bad',['missing_uid','container_replace','uid_replace','decrease','delta_three','notready','waiting','recent_oom','undated_oom','missing_logs','stale_logs','new_error'])
def test_temporal_fail_closed(tmp_path,bad):
    d=doc_for(tmp_path/'store');w=observed(d);l=logs(d)
    if bad=='missing_uid':w[0].pop('uid')
    if bad=='container_replace':w[0]['container_id']='containerd://other'
    if bad=='uid_replace':w[0]['uid']='other'
    if bad=='decrease':w[0]['restart_count']=107
    if bad=='delta_three':w[0]['restart_count']=111
    if bad=='notready':w[0]['ready']=False
    if bad=='waiting':w[0]['waiting']={'reason':'CrashLoopBackOff'}
    if bad=='recent_oom':w[0]['last_terminated']['finishedAt']='2026-10-12T12:01:50Z'
    if bad=='undated_oom':w[0]['last_terminated'].pop('finishedAt')
    if bad=='missing_logs':l={}
    if bad=='stale_logs':l[('worker-uid','worker')]['observed_at']='2026-10-12T11:00:00Z'
    if bad=='new_error':l[('worker-uid','worker')]['text']='No master found after startup'
    with pytest.raises(control.ControlError):control.temporal_gate(approval(d),w,l,now=NOW)

@pytest.mark.parametrize('change',['missing','stale','future','tamper','expired','raised_budget','reties','created_at','body','key','tenant','unsafe_phase'])
def test_manifest_invalid_before_writes(tmp_path,change):
    d=doc_for(tmp_path/'store');digest=control.sha256_json(d)
    if change=='missing':d.pop('baseline')
    elif change=='stale':d['baseline']['captured_at']='2026-10-12T10:00:00Z';d['baseline_sha256']=control.sha256_json(d['baseline']);digest=control.sha256_json(d)
    elif change=='future':d['baseline']['captured_at']='2026-10-12T12:02:20Z';d['baseline_sha256']=control.sha256_json(d['baseline']);digest=control.sha256_json(d)
    elif change=='tamper':d['baseline']['workers'][0]['restart_count']=0
    elif change=='expired':d['expires_at']='2026-10-12T12:01:01Z';digest=control.sha256_json(d)
    elif change=='raised_budget':d['max_POST_invocations']=2;digest=control.sha256_json(d)
    elif change=='reties':d['transport_retries']=1;digest=control.sha256_json(d)
    elif change=='created_at':d['payload'].pop('created_at');digest=control.sha256_json(d)
    elif change=='body':d['payload']['body']=''
    elif change=='key':d['payload']['idempotency_key']='timestamp';digest=control.sha256_json(d)
    elif change=='tenant':d['payload']['tenant_id']='other';digest=control.sha256_json(d)
    else:d['phase']='generation-184';digest=control.sha256_json(d)
    with pytest.raises(control.ControlError):control.Approval(d,digest,now=NOW)

@pytest.mark.parametrize('failure',list(control.REQUIRED_CHECKS)+['alert','exception'])
def test_every_gate_fails_before_reservation_or_post(tmp_path,failure):
    d=doc_for(tmp_path/'store');a=approval(d);j=control.Journal(tmp_path/'store',a,host_id='fixture-host');c=Transport(d)
    def pre():
        if failure=='exception':raise RuntimeError('fixture')
        r=gates()
        if failure=='alert':r['alerts']=[{'code':'failure'}]
        else:next(x for x in r['checks'] if x['name']==failure)['status']='failed'
        return r
    with pytest.raises(control.ControlError):control.run_reserved(a,j,c,pre,lambda:gates(),now=NOW)
    assert c.posts==[] and j.inspect(a.slot) is None

def test_duplicate_concurrent_runners_at_most_one_post(tmp_path):
    d=doc_for(tmp_path/'store');a=approval(d);c=Transport(d)
    def run(_):
        j=control.Journal(tmp_path/'store',a,host_id='fixture-host')
        try:return control.run_reserved(a,j,c,gates,gates,now=NOW)
        except control.ControlError:return None
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(run,range(16)))
    assert len(c.posts)==1 and any(results)
    assert c.posts[0]==d['payload']

@pytest.mark.parametrize('point',['after_fence','after_reservation','after_post','lost_response'])
def test_crash_is_consumed_and_only_read_recovery(tmp_path,point):
    d=doc_for(tmp_path/'store');a=approval(d);j=control.Journal(tmp_path/'store',a,host_id='fixture-host');c=Transport(d)
    def crash(at):
        if at==point:raise control.SimulatedCrash(at)
    if point=='lost_response':
        old=c.request
        def request(method,path,**kw):
            response=old(method,path,**kw)
            if method=='POST':raise TimeoutError('ambiguous')
            return response
        c.request=request
    with pytest.raises((control.SimulatedCrash,control.ControlError)):control.run_reserved(a,j,c,gates,gates,now=NOW,crash=crash)
    for _ in range(3):
        try:control.run_reserved(a,j,c,gates,gates,now=NOW)
        except control.ControlError:pass
    assert len(c.posts)==(1 if point in ['after_post','lost_response'] else 0)

@pytest.mark.parametrize('corruption',['missing_db','corrupt_db','missing_anchor','bad_fence','deleted_row','symlink','wrong_host','wrong_store'])
def test_journal_identity_and_corruption_never_replenish(tmp_path,corruption):
    root=tmp_path/'store';d=doc_for(root);a=approval(d);j=control.Journal(root,a,host_id='fixture-host');j.reserve(a)
    if corruption=='missing_db':(root/'journal.sqlite3').unlink()
    elif corruption=='corrupt_db':(root/'journal.sqlite3').write_bytes(b'not sqlite')
    elif corruption=='missing_anchor':(root/'anchor.json').unlink()
    elif corruption=='bad_fence':next((root/'reservations').iterdir()).write_text('{}')
    elif corruption=='deleted_row':
        with sqlite3.connect(root/'journal.sqlite3') as db:db.execute('DELETE FROM slots')
    elif corruption=='symlink':
        (root/'journal.sqlite3').rename(root/'other');(root/'journal.sqlite3').symlink_to(root/'other')
    host='other-host' if corruption=='wrong_host' else 'fixture-host'
    if corruption=='wrong_store':d['store_binding']['store_id']='other';a=approval(d)
    c=Transport(d)
    with pytest.raises(control.ControlError):
        jj=control.Journal(root,a,host_id=host);control.run_reserved(a,jj,c,gates,gates,now=NOW)
    assert not c.posts

@pytest.mark.parametrize('field',['tenant_id','scope','source_item_id','idempotency_key','body_sha256','durable','job_id'])
def test_exact_remote_receipt_mismatch_keeps_consumed_permit(tmp_path,field):
    d=doc_for(tmp_path/'store');a=approval(d);j=control.Journal(tmp_path/'store',a,host_id='fixture-host');c=Transport(d);c.corrupt=field
    with pytest.raises(control.ControlError):control.run_reserved(a,j,c,gates,gates,now=NOW)
    assert len(c.posts)==1
    with pytest.raises(control.ControlError):control.run_reserved(a,j,c,gates,gates,now=NOW)
    assert len(c.posts)==1

def test_postflight_oom_does_not_erase_consumed_permit(tmp_path):
    d=doc_for(tmp_path/'store');a=approval(d);j=control.Journal(tmp_path/'store',a,host_id='fixture-host');c=Transport(d)
    def post():r=gates();r['alerts']=[{'code':'recent_oom'}];return r
    with pytest.raises(control.ControlError):control.run_reserved(a,j,c,gates,post,now=NOW)
    assert len(c.posts)==1 and j.inspect(a.slot)['state']=='reserved'

def test_runtime_name_and_installed_order():
    image='ghcr.io/palaceoftruth/palaceoftruth/backend@sha256:5fbe3566a8d18a77c21df0205036ac0583320704f0e55a3ee63c5ce9556e1f7b'
    assert control.migration_name('palace-sarvent','0.1.608+dfc70b91a28c',image)=='palace-sarvent-migrate-36da397153'
    assert control.migration_name('palace-sarvent','0.1.608',image)=='palace-sarvent-migrate-a8d07a0efb'
    calls=[]
    with pytest.raises(control.ControlError):control.helm319_hooks([(10,'tenant-rls'),(10,'memory-smoke')],lambda n:calls.append(n) or n!='memory-smoke')
    assert calls==['memory-smoke']
