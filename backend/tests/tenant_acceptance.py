"""Mandatory opaque-tenant tests; no production credentials or tenant normalization."""
import pytest
from tests.test_hermes_outbox import setup, TENANT, Backend, admit


def test_literal_default_and_uuid_bindings(tmp_path):
    m,p,b=setup(tmp_path)
    for tenant in ['default',TENANT]:
        assert m.validate_binding({**b,'tenant_id':tenant})['tenant_id']==tenant


@pytest.mark.parametrize('tenant',['', ' ', ' default', 'default ', 'de\tfault', 'de\nfault', 'de\x00fault', 'de\x7ffault', 'de\u0085fault', 'de\u202efault', 'x'*257, None, 7])
def test_invalid_tenant_binding_refused(tmp_path,tenant):
    m,p,b=setup(tmp_path)
    with pytest.raises(m.OutboxError):m.OutboxStore(tmp_path/'invalid',{**b,'tenant_id':tenant})
    assert not (tmp_path/'invalid').exists()


@pytest.mark.parametrize('other',['Default','default ',' default','unrelated','', 'x'*257])
def test_server_tenant_mismatch_never_normalizes(tmp_path,other):
    m,p,b=setup(tmp_path);b['tenant_id']='default'
    p._tenant_id='default';p._config['outbox_binding']=b
    store=admit(p)
    backend=Backend(b);original=backend.call
    def call(method,path,*args,**kwargs):
        value=original(method,path,*args,**kwargs)
        if path.endswith('/whoami'):value['tenant_id']=other
        return value
    backend.call=call
    with pytest.raises(m.OutboxError):m.RecoveryWorker(store,backend).run()
    assert all(r['state']=='pending' for r in store.records())
    assert backend.posts==[]


@pytest.mark.parametrize('other',['Default','default ',' default','unrelated','', 'x'*257])
def test_receipt_tenant_mismatch_keeps_pending(tmp_path,other):
    m,p,b=setup(tmp_path);b['tenant_id']='default'
    p._tenant_id='default';p._config['outbox_binding']=b
    store=admit(p);backend=Backend(b);backend.receipt_change={'tenant_id':other}
    result=m.RecoveryWorker(store,backend).run()
    assert result['failed']==1 and result['remote_durable']==0
    assert all(r['state']=='pending' for r in store.records())
    assert len(backend.posts)==1


@pytest.mark.parametrize('value',['default','',7])
def test_credential_uuid_requirement_not_relaxed(tmp_path,value):
    m,p,b=setup(tmp_path);b['tenant_id']='default';b['credential']['id']=value
    with pytest.raises(m.OutboxError):m.validate_binding(b)


def test_live_shaped_oauth_identity_preflight_before_store(tmp_path):
    m,p,b=setup(tmp_path)
    b['tenant_id']='default'
    b['credential']={'mode':'mcp_oauth','id':'2f6b0abc-7473-4d59-94e3-3d3455b5c081','client_key':'hermes-barbara'}
    identity={'tenant_id':'default','auth_mode':'mcp_oauth','credential_id':None,
        'mcp_client_id':b['credential']['id'],'mcp_client_key':'hermes-barbara',
        'agent_scope_key':'barbara','allowed_scopes':['read','write','write:agent']}
    m.RecoveryWorker.validate_identity(b,identity)
    for key,value in [('tenant_id','Default'),('auth_mode','api_key'),('mcp_client_key','other'),
                      ('mcp_client_id',TENANT),('agent_scope_key','other')]:
        with pytest.raises(m.OutboxError):m.RecoveryWorker.validate_identity(b,{**identity,key:value})
    assert not (tmp_path/'outbox').exists()
