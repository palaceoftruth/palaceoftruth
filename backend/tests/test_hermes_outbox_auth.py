"""R1/R2 failure-first tests. Auth functions/routes are real; persistence is fixture-only."""
import asyncio
import importlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Request
from app import auth
from app.api import memory
from tests.test_hermes_outbox import setup, admit, drain, Backend, TENANT

KEY_ID='44444444-4444-4444-8444-444444444444'
CLIENT_ID='55555555-5555-4555-8555-555555555555'
CLIENT_KEY='hermes-fixture'


def identity(mode='api_key', *, principal=KEY_ID, tenant=TENANT, scopes=('read','write','write:agent')):
    request=Request({'type':'http','headers':[(b'x-palace-agent-scope-key',b'caller-not-principal')]})
    auth._attach_auth_context(request,auth._context_from_scopes(tenant_id=tenant,auth_mode=mode,
        subject_id=principal,client_id=principal if mode=='mcp_oauth' else None,
        client_key=CLIENT_KEY if mode=='mcp_oauth' else None,
        agent_scope_key='barbara' if mode=='mcp_oauth' else None,
        scopes=list(scopes),token_hash_reference='fixture-reference'))
    return asyncio.run(memory.whoami(request)).model_dump(mode='json')


def test_real_whoami_api_key_principal_is_server_owned_not_scope_header():
    payload=identity()
    assert payload['agent_scope_key'] is None
    assert payload['credential_id']==KEY_ID
    assert payload['auth_mode']=='api_key'


def test_api_key_recovery_accepts_real_whoami_null_agent_identity(tmp_path):
    m,p,binding=setup(tmp_path);store=admit(p)
    class ActualWhoami(Backend):
        def call(self,method,path,payload=None,*,deadline):
            if path.endswith('/whoami'):return identity()
            return super().call(method,path,payload,deadline=deadline)
    assert m.RecoveryWorker(store,ActualWhoami(binding)).run(max_jobs=1,timeout=1)['remote_durable']==1


def test_oauth_mode_supported_without_api_key_conversion(tmp_path):
    m,p,binding=setup(tmp_path)
    binding['credential']={'mode':'mcp_oauth','id':CLIENT_ID,'client_key':CLIENT_KEY}
    p._api_key='';p._oauth_client_secret='fixture-client-secret';p._oauth_client_key=CLIENT_KEY
    p._config['outbox_binding']=binding
    store=admit(p)
    credentials=importlib.import_module(m.__package__+'.credentials')
    provider=credentials.OAuthCredentials(binding,client_secret='fixture-client-secret')
    transport=m.RestTransport(binding,credentials=provider)
    assert transport.binding['credential']['mode']=='mcp_oauth'
    assert b'fixture-client-secret' not in store.path.read_bytes()


@pytest.mark.parametrize('mutation',['origin_tenant','credential_mode'])
def test_submission_nonsecret_binding_freezes_before_worker(tmp_path,mutation):
    import threading
    m,p,binding=setup(tmp_path);entered,release=threading.Event(),threading.Event()
    with p._lifecycle_lock:p._queue_write(lambda:(entered.set(),release.wait(2)),p._quota_for_session(p._session_id))
    assert entered.wait(1)
    p.sync_turn('Alice old target','answer',turn_author={'id':'alice'})
    changed={**binding,'origin':'https://changed.invalid','tenant_id':CLIENT_ID}
    if mutation=='credential_mode':
        changed={**binding,'credential':{'mode':'mcp_oauth','id':CLIENT_ID,'client_key':CLIENT_KEY}}
        p._api_key='';p._oauth_client_secret='fixture-late-mode';p._oauth_client_key=CLIENT_KEY
    p._config['outbox_binding']=changed;p._base_url=changed['origin']
    try:
        release.set();assert drain(p)
        assert p._outbox_store.binding==binding
        assert p._outbox_store.records()[0]['payload']['tenant_id']==TENANT
    finally:release.set();p.shutdown()


def test_oauth_never_falls_back_to_broad_api_key(tmp_path,monkeypatch):
    m,p,b=setup(tmp_path);b['credential']={'mode':'mcp_oauth','id':CLIENT_ID,'client_key':CLIENT_KEY}
    c=importlib.import_module(m.__package__+'.credentials')
    monkeypatch.setenv('PALACE_OUTBOX_API_KEY','fixture-broad-key')
    monkeypatch.delenv('PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET',raising=False)
    monkeypatch.delenv('SECONDBRAIN_MCP_OAUTH_CLIENT_SECRET',raising=False)
    with pytest.raises(c.CredentialError):c.from_environment(b)
