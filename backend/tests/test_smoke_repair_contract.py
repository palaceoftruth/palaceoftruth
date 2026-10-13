"""Real predecessor RED contracts. All transports and live gates are mocked."""
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from scripts import check_memory_rollout_smoke as smoke


def test_default_automatic_hook_never_posts_even_with_failed_api(monkeypatch):
    calls=[]
    def api(client, report):smoke._alert(report,'api_health_failed','fixture')
    async def sentinel(report):smoke._record_check(report,'sentinel_valkey','passed')
    with patch.multiple(smoke,check_api_health=api,check_runtime_dependencies=lambda *a,**k:None,
                        resolve_tenant_identity=lambda *a,**k:'fixture',check_memory_write=lambda *a,**k:calls.append('POST'),
                        check_sentinel=sentinel,check_mcp_reachable=lambda *a,**k:None,check_kubernetes=lambda *a,**k:None):
        report=smoke.build_report(smoke.build_parser().parse_args([]))
    assert report['status']=='failed'
    assert calls==[]


def test_payload_time_is_not_regenerated_for_fixed_run():
    t=datetime(2026,10,12,tzinfo=timezone.utc)
    with patch.object(smoke,'utc_now',return_value=t):
        a=smoke._memory_entry(tenant_id='fixture',target_name='fixture',run_id='20261012T000000Z',scope_key='fixture')
    with patch.object(smoke,'utc_now',return_value=t+timedelta(seconds=1)):
        b=smoke._memory_entry(tenant_id='fixture',target_name='fixture',run_id='20261012T000000Z',scope_key='fixture')
    assert a==b


def test_unmanifested_legacy_write_refuses_before_post():
    from types import SimpleNamespace
    class Client:
        def __init__(self):self.posts=[]
        def request(self, method, path, **kw):
            if method=='POST':self.posts.append(path);return smoke.HttpResult(200,{'job_id':'legacy'})
            if path=='/memory/whoami':return smoke.HttpResult(200,{'tenant_id':'fixture'})
            if path=='/memory/jobs':return smoke.HttpResult(200,{'jobs':[]})
            return smoke.HttpResult(200,{'status':'completed'})
    c=Client();report={'target':'fixture','checks':[],'alerts':[]}
    args=SimpleNamespace(run_id='20261012T000000Z',target_name='fixture',scope_key='fixture',job_timeout_seconds=0.1,job_interval_seconds=0,job_list_limit=2)
    smoke.check_memory_write(c,report,args)
    assert c.posts==[] and report['alerts']
