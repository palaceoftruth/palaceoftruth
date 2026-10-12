"""Ephemeral recovery credentials using the existing Palace client_credentials protocol.

No credential file/dotenv reading, token persistence, API-key fallback or raw errors.
The form helper is also used by the normal provider's existing minting path.
"""
from __future__ import annotations
import json
import math
import os
import subprocess
import sys
from time import perf_counter
from urllib.parse import urlencode, urlsplit


class CredentialError(RuntimeError):
    pass


def client_credentials_form(client_key, client_secret, scopes, resource):
    return urlencode({'grant_type':'client_credentials','client_id':client_key,
                      'client_secret':client_secret,'scope':' '.join(scopes),'resource':resource}).encode()


def required_scopes(binding):
    return ('read','write','write:'+binding['scope']['type'])


class ApiKeyCredentials:
    def __init__(self,binding,secret):
        if binding['credential']['mode']!='api_key' or not secret or '\n' in secret or '\r' in secret:
            raise CredentialError('recovery API-key credential unavailable or mismatched')
        self._secret=secret
        self._binding_text=json.dumps(binding,sort_keys=True)
    @property
    def binding(self):return json.loads(self._binding_text)
    def headers(self,deadline):
        if perf_counter()>=deadline:raise CredentialError('recovery credential deadline exhausted')
        return {'X-API-Key':self._secret}


class OAuthCredentials:
    def __init__(self,binding,*,client_secret,token_url=None,resource=None,scopes=None,ca_file=None):
        credential=binding['credential']
        if credential['mode']!='mcp_oauth' or not client_secret:
            raise CredentialError('recovery OAuth credential unavailable or mismatched')
        self._client_key=credential['client_key'];self._secret=client_secret
        self._binding_text=json.dumps(binding,sort_keys=True)
        self._url=token_url or binding['origin']+'/api/v1/memory/mcp/oauth/token'
        parsed=urlsplit(self._url)
        if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment
                or self._url!=binding['origin']+'/api/v1/memory/mcp/oauth/token'):
            raise CredentialError('recovery OAuth token endpoint differs from bound origin')
        self._resource=resource or binding['origin']+'/api/v1'
        if self._resource!=binding['origin']+'/api/v1':
            raise CredentialError('recovery OAuth resource differs from bound API')
        required=set(required_scopes(binding))
        if scopes is not None and not required.issubset(set(scopes)):
            raise CredentialError('recovery OAuth requested scopes are insufficient')
        # Never expand a supplied grant into admin/tenant-wide scopes.
        self._scopes=required_scopes(binding)
        self._token=None;self._expires=0.0;self._margin=0.0;self._ca_file=ca_file

    @property
    def binding(self):return json.loads(self._binding_text)

    def headers(self,deadline):
        if not self._token or self._expires-perf_counter()<=self._margin:
            self._mint(deadline)
        if self._token is None:raise CredentialError('recovery OAuth token unavailable')
        if perf_counter()>=deadline:raise CredentialError('recovery credential deadline exhausted')
        return {'Authorization':'Bearer '+self._token}

    def _mint(self,deadline):
        remaining=deadline-perf_counter()
        if remaining<=0:raise CredentialError('recovery OAuth deadline exhausted')
        script='''
import json,ssl,sys
from urllib.request import Request,build_opener,ProxyHandler,HTTPSHandler,HTTPRedirectHandler
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):raise RuntimeError('redirect forbidden')
p=json.load(sys.stdin)
opener=build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context(cafile=p['ca_file'])),NoRedirect())
req=Request(p['url'],data=p['form'].encode(),method='POST',headers={'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'})
with opener.open(req,timeout=p['timeout']) as r:sys.stdout.buffer.write(r.read(65537))
'''
        form=client_credentials_form(self._client_key,self._secret,self._scopes,self._resource)
        wire=json.dumps({'url':self._url,'form':form.decode(),'timeout':min(5,remaining),'ca_file':self._ca_file}).encode()
        try:
            response=subprocess.run([sys.executable,'-c',script],input=wire,stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,timeout=min(5,deadline-perf_counter()),check=True,env={'PATH':os.environ.get('PATH','')})
            if len(response.stdout)>65536:raise ValueError('size')
            value=json.loads(response.stdout)
            token=value.get('access_token');lifetime=value.get('expires_in')
            if (not isinstance(token,str) or not token or '\n' in token or '\r' in token
                    or type(lifetime) is not int or lifetime<=0 or value.get('token_type','Bearer').casefold()!='bearer'):
                raise ValueError('invalid token metadata')
            if perf_counter()>=deadline:raise ValueError('deadline')
            self._token=token;self._expires=perf_counter()+lifetime;self._margin=min(30,lifetime*.1)
        except Exception:
            self._token=None;self._expires=0.0
            raise CredentialError('bounded OAuth acquisition failed') from None


def from_environment(binding,*,ca_file=None,secret_reader=None):
    read=secret_reader if secret_reader is not None else os.environ.get
    mode=binding['credential']['mode']
    if mode=='api_key':
        return ApiKeyCredentials(binding,read('PALACE_OUTBOX_API_KEY') or read('PALACEOFTRUTH_API_KEY') or '')
    # Existing plugin credential names; no dotenv read and no conversion/fallback.
    secret=read('PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET') or read('SECONDBRAIN_MCP_OAUTH_CLIENT_SECRET') or ''
    key=read('PALACEOFTRUTH_MCP_CLIENT_KEY') or read('SECONDBRAIN_MCP_CLIENT_KEY')
    if key and key!=binding['credential']['client_key']:
        raise CredentialError('recovery OAuth configured client differs')
    scopes=read('PALACEOFTRUTH_MCP_CLIENT_SCOPES') or read('SECONDBRAIN_MCP_CLIENT_SCOPES')
    resource=read('PALACEOFTRUTH_MCP_OAUTH_RESOURCE') or read('SECONDBRAIN_MCP_OAUTH_RESOURCE')
    if resource==binding['origin']+'/mcp':resource=binding['origin']+'/api/v1'
    return OAuthCredentials(binding,client_secret=secret,
        token_url=read('PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL') or read('SECONDBRAIN_MCP_OAUTH_TOKEN_URL'),
        resource=resource,
        scopes=scopes.replace(',',' ').split() if scopes else None,ca_file=ca_file)
