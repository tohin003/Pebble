import copy
import json
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
from urllib.parse import urlsplit, parse_qs, urlencode
import hashlib
import base64
import pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from pebble.app import App, TOOLS
from pebble.artifacts import Artifacts, validate
from pebble.capabilities import capabilities, choose
from pebble.providers import Providers, safe_error
from pebble.responses import input_items
from pebble.chatgpt import ChatGPT, SecretRecords, ISSUER
from pebble.permissions import DEFAULT
from pebble.policy import Workspace, PolicyError
from pebble.preview import Previews
from pebble.views import Views
from pebble.store import Store


class FakeVault:
    def __init__(self): self.data = {}
    def get(self,key): return self.data.get(key,'')
    def set(self,key,value): assert len(value)<=2400; self.data[key]=value
    def delete(self,key): self.data.pop(key,None)


def profile(model='gpt-6-luna', kind='openai', url='https://api.openai.com/v1'):
    return {'id':'fixture','name':'Fixture','kind':kind,'url':url,'model':model,'context':1050000,'budget':32768,'tools':True}


@pytest.mark.parametrize('p,expected',[
    (profile(),['none','low','medium','high','xhigh','max']),
    (profile('gpt-6.1-sol'),['low','medium','high','xhigh','max']),
    (profile('gpt-6-astra'),['low','medium','high','xhigh','max']),
    (profile('ordinary-chat'),[]),
    (profile('gemini-3.8-flash','openai','https://generativelanguage.googleapis.com/v1beta/openai'),['low','medium','high']),
    (profile('gemini-3.1-pro-preview','openai','https://generativelanguage.googleapis.com/v1beta/openai'),['low','medium','high']),
    (profile('gemini-3.5-flash','openai','https://generativelanguage.googleapis.com/v1beta/openai'),['minimal','low','medium','high']),
    (profile('claude-opus-4-6','anthropic','https://api.anthropic.com/v1'),['low','medium','high','max']),
    (profile('claude-sonnet-5-5','anthropic','https://api.anthropic.com/v1'),['low','medium','high','xhigh','max']),
    (profile('claude-3-haiku','anthropic','https://api.anthropic.com/v1'),[]),
    ({'kind':'gguf','name':'nonthinking-model'},[]),
])
def test_effort_contract(p, expected):
    assert capabilities(p)['efforts']==['default']+expected
    with pytest.raises(ValueError): choose(p,'unsupported')
    if not expected:
        assert choose(p,True)==choose(p,False)=='default'


def test_context_migration_and_edit_preserve_key(tmp_path):
    s=Store(tmp_path);p=profile();p['context']=131072;p['has_key']=True
    s.set_setting('model_profiles',[p]);s.set_setting('active_profile','fixture')
    provider=Providers(tmp_path,Path('missing'),s,lambda *a:None);provider.vault=FakeVault();provider.vault.set('fixture','fixture-key')
    assert provider.status()['context']==1050000
    provider.save('Renamed',p['url'],p['model'],context=2000000,budget=1000000,profile_id='fixture')
    assert provider.vault.get('fixture')=='fixture-key'
    assert provider.status()['budget']==1000000
    with pytest.raises(ValueError): provider.save('Bad',p['url'],p['model'],context=4096,budget=10000)
    s.close()


def test_exact_token_count_keeps_long_text_and_protected_groups():
    messages=[{'role':'system','content':'s'}, {'role':'user','content':'old '*20000}, {'role':'user','content':'latest','_pebble_current':True}]
    result=Providers.bounded(messages,5000,counter=lambda ms:1000,reserve=64)
    assert len(result)==3  # Full text is kept when the model's tokenizer says it fits.
    call={'id':'c','type':'function','function':{'name':'Read','arguments':'{}'}}
    result=Providers.bounded([messages[0], {'role':'assistant','tool_calls':[call],'content':None},{'role':'tool','tool_call_id':'c','content':'data'},messages[-1]],4096)
    assert result[1]['tool_calls'][0]['id']=='c'


@pytest.fixture
def responses_server():
    records=[]; mode={'value':'call'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));records.append(body)
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            if mode['value']=='cut': events=[{'type':'response.output_text.delta','delta':'Partial'}]
            elif mode['value']=='failed': events=[{'type':'response.failed','response':{'error':{'message':'usage limit','code':'subscription_sharing_usage_limit_exceeded'}}}]
            else:
                output=[{'type':'reasoning','id':'r','summary':[],'encrypted_content':'opaque'}, {'type':'function_call','id':'fc','call_id':'call_1','name':'Read','arguments':'{"file_path":"a.py"}'}]
                events=[{'type':'response.function_call_arguments.delta','delta':'{}'}, {'type':'response.completed','response':{'status':'completed','output':output,'usage':{'input_tokens':40,'output_tokens':8,'input_tokens_details':{'cached_tokens':12}}}}]
            for event in events:self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}/v1',records,mode
    server.shutdown();server.server_close()


def test_responses_replays_reasoning_calls_and_counts(tmp_path,responses_server):
    url,records,mode=responses_server;s=Store(tmp_path);p=profile(kind='responses',url=url);p['reasoning_capabilities']={'parameter':'reasoning','levels':['low','high'],'default':'low'}
    s.set_setting('model_profiles',[p]);s.set_setting('active_profile',p['id']);provider=Providers(tmp_path,Path('missing'),s,lambda *a:None)
    result=provider.complete([{'role':'user','content':'Read a.py','_pebble_current':True}],TOOLS[:1],threading.Event(),lambda x:None,'high')
    assert records[-1]['store'] is False and records[-1]['reasoning']=={'effort':'high'}
    assert records[-1]['tools'][0]['strict'] is False
    assert provider.last_usage['cached_tokens']==12
    items=input_items([result,{'role':'tool','tool_call_id':'call_1','content':'{"ok":true}'}])
    assert items[0]['encrypted_content']=='opaque' and items[-1]['call_id']=='call_1'
    assert len([i for i in items if i['type']=='function_call'])==1
    for value in ('cut','failed'):
        mode['value']=value
        with pytest.raises(ValueError):provider.complete([{'role':'user','content':'test'}],[],threading.Event(),lambda x:None)
    s.close()


def test_errors_redact_keys_tokens_and_urls():
    s=safe_error({'error':{'message':'bad sk-secret123 https://user:pw@host/path eyJ'+('a'*50),'param':'reasoning_effort'}},['sk-secret123'])
    assert 'secret123' not in s and 'user:pw' not in s and 'eyJ' not in s
    assert 'reasoning_effort' in s


def test_secret_records_rotate_without_exposing_large_credentials():
    vault=FakeVault();r=SecretRecords(vault)
    r.set('oauth',{'token':'a'*8000});assert r.get('oauth')['token']=='a'*8000
    r.set('oauth',{'token':'b'*8000});assert r.get('oauth')['token']=='b'*8000
    assert not any('a'*100 in v for v in vault.data.values())
    r.delete('oauth');assert not vault.data


def test_oidc_identity_rejects_nonce_audience_expiry_and_unsigned(tmp_path):
    s=Store(tmp_path);p=Providers(tmp_path,Path('missing'),s,lambda *a:None);p.vault=FakeVault();auth=ChatGPT(p)
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()));jwk['kid']='fixture'
    auth.http_json=lambda *a:{'keys':[jwk]}
    claims={'iss':ISSUER,'aud':'oaiapp_fixture','exp':int(time.time()+600),'iat':int(time.time()),'sub':'account','nonce':'nonce'}
    signed=lambda c:jwt.encode(c,private,algorithm='RS256',headers={'kid':'fixture'})
    assert auth.validate_identity(signed(claims),'oaiapp_fixture','nonce',{'jwks_uri':'https://auth.openai.com/.well-known/jwks.json'})['sub']=='account'
    for changed in ({'nonce':'bad'},{'aud':'wrong'},{'exp':int(time.time()-100)}):
        with pytest.raises((ValueError,jwt.PyJWTError)):auth.validate_identity(signed({**claims,**changed}),'oaiapp_fixture','nonce',{'jwks_uri':'url'})
    with pytest.raises(ValueError):auth.validate_tokens({'access_token':'token','token_type':'Bearer','expires_in':3600,'scope':'openid'})
    s.close()


def test_callback_csrf_and_client_substitution_fail_closed(tmp_path):
    s=Store(tmp_path);p=Providers(tmp_path,Path('missing'),s,lambda *a:None);p.vault=FakeVault();a=ChatGPT(p)
    a.pending={'state':'correct','deadline':time.time()+100,'client_id':'oaiapp_old','old':{'id':'p'},'config':{}};a.result={'state':'waiting'}
    assert a.callback({'state':['wrong'],'code':['code']})[0]==400
    assert a.result['state']=='waiting'
    assert a.callback({'state':['correct'],'code':['code'],'client_id':['oaiapp_different']})[0]==400
    assert not p.vault.data
    s.close()


def test_pkce_loopback_exchange_eligible_model_and_session_rotation(tmp_path):
    s=Store(tmp_path);p=Providers(tmp_path,Path('missing'),s,lambda *a:None);p.vault=FakeVault();a=ChatGPT(p)
    a.can_activate=lambda:False
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()));jwk['kid']='fixture'
    config={'issuer':ISSUER,'authorization_endpoint':ISSUER+'/authorize','token_endpoint':ISSUER+'/token','jwks_uri':ISSUER+'/jwks'}
    records=[]
    def auth_http(url, form=None):
        if url.endswith('openid-configuration'):return config
        if url.endswith('/jwks'):return {'keys':[jwk]}
        records.append(form)
        if form['grant_type']=='refresh_token':
            return {'access_token':'rotated-access','refresh_token':'rotated-refresh','token_type':'Bearer','expires_in':3600,'scope':'resource.invoke chatgpt.tokens.use.direct'}
        claims={'iss':ISSUER,'aud':'oaiapp_fixture','exp':int(time.time()+600),'iat':int(time.time()),'sub':'account','nonce':a.pending['nonce'],'email':'fixture@example.test'}
        return {'access_token':'owned-access','refresh_token':'owned-refresh','token_type':'Bearer','expires_in':3600,'scope':'resource.invoke chatgpt.tokens.use.direct','id_token':jwt.encode(claims,private,algorithm='RS256',headers={'kid':'fixture'})}
    a.http_json=auth_http
    p.discover=lambda url,kind,api_key:[{'id':'gpt-6-sol','context':1050000}]
    q=parse_qs(urlsplit(a.start()['url']).query);pending=a.pending.copy()
    assert q['client_id']==['dynamic_agent_client'] and q['code_challenge_method']==['S256']
    challenge=base64.urlsafe_b64encode(hashlib.sha256(pending['verifier'].encode()).digest()).decode().rstrip('=')
    assert q['code_challenge']==[challenge] and urlsplit(pending['redirect']).hostname=='127.0.0.1'
    with urllib.request.urlopen(pending['redirect']+'?'+urlencode({'state':pending['state'],'client_id':'oaiapp_fixture','code':'one-use-code'})) as response:
        assert response.status==200
    pid=a.status()['profile_id']; saved=next(x for x in p.profiles() if x['id']==pid)
    assert saved['model']=='gpt-6-sol' and saved['subject']=='account'
    assert s.setting('active_profile') is None  # Do not interrupt an active task.
    assert records[0]['code_verifier']==pending['verifier'] and records[0]['redirect_uri']==pending['redirect']
    assert 'owned-access' not in str(s.setting('model_profiles')) and 'one-use-code' not in str(s.setting('model_profiles'))
    record=a.records.get('oauth:'+pid);record['expires_at']=time.time()-1;a.records.set('oauth:'+pid,record)
    assert a.access(pid)=='rotated-access'
    assert a.records.get('oauth:'+pid)['refresh_token']=='rotated-refresh'
    a.cancel();s.close()


@contextmanager
def wire_fixture(events, records):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            records.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            for event in events:
                self.wfile.write(('data: '+(event if isinstance(event,str) else json.dumps(event))+'\n\n').encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    try:yield f'http://127.0.0.1:{server.server_port}/v1'
    finally:server.shutdown();server.server_close()


def test_gemini_effort_wire_limits_and_interrupted_calls(tmp_path):
    records=[];events=[{'choices':[{'delta':{'content':'OK'},'finish_reason':'stop'}]},'[DONE]']
    with wire_fixture(events,records) as url:
        s=Store(tmp_path);p=Providers(tmp_path,Path('missing'),s,lambda *a:None)
        fixture=profile('gemini-fixture','openai',url);fixture.update(context=4096,budget=4096,reasoning_capabilities={'parameter':'reasoning_effort','levels':['low','high'],'default':'low'})
        s.set_setting('model_profiles',[fixture]);s.set_setting('active_profile','fixture')
        p.complete([{'role':'user','content':'Reply OK'}],[],threading.Event(),lambda x:None,'high')
        assert records[-1]['reasoning_effort']=='high' and records[-1]['max_tokens']==2048
        events[:]=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':'sig-call','function':{'name':'Read','arguments':'{"file_path":"a.py"}'},'extra_content':{'google':{'thought_signature':'opaque-signed'}}}]},'finish_reason':'tool_calls'}]},'[DONE]']
        result=p.complete([{'role':'user','content':'Read'}],TOOLS[:1],threading.Event(),lambda x:None,'low')
        p.complete([{'role':'user','content':'Read'},result,{'role':'tool','tool_call_id':'sig-call','content':'data'}],TOOLS[:1],threading.Event(),lambda x:None,'low')
        assert records[-1]['messages'][1]['tool_calls'][0]['extra_content']=={'google':{'thought_signature':'opaque-signed'}}
        events[:]=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':'c','function':{'name':'Write','arguments':'{"file_path":"a'}}]},'finish_reason':'length'}]}]
        with pytest.raises(ValueError,match='No tool calls'):p.complete([{'role':'user','content':'Build'}],TOOLS[:1],threading.Event(),lambda x:None,'low')
        s.close()


def test_claude_effort_and_signed_thinking_roundtrip(tmp_path):
    records=[];events=[
        {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':''}},
        {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'opaque thought'}},
        {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'signed'}},
        {'type':'content_block_start','index':1,'content_block':{'type':'tool_use','id':'call','name':'Read','input':{}}},
        {'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':'{"file_path":"a.py"}'}},
        {'type':'message_stop'}]
    with wire_fixture(events,records) as url:
        s=Store(tmp_path);p=Providers(tmp_path,Path('missing'),s,lambda *a:None);fixture=profile('claude-sonnet-5-5','anthropic',url)
        s.set_setting('model_profiles',[fixture]);s.set_setting('active_profile','fixture')
        result=p.complete([{'role':'user','content':'Read a.py'}],TOOLS[:1],threading.Event(),lambda x:None,'xhigh')
        assert records[-1]['thinking']=={'type':'adaptive'} and records[-1]['output_config']=={'effort':'xhigh'}
        p.complete([{'role':'user','content':'Read a.py'},result,{'role':'tool','tool_call_id':'call','content':'data'}],TOOLS[:1],threading.Event(),lambda x:None,'low')
        replay=records[-1]['messages'][1]['content']
        assert replay[0]=={'type':'thinking','thinking':'opaque thought','signature':'signed'}
        assert replay[1]['input']=={'file_path':'a.py'}
        assert records[-1]['messages'][2]['content'][0]['tool_use_id']=='call'
        s.close()


def test_quiz_diagram_validation_and_chat_deletion(tmp_path):
    app=App(tmp_path,Path('missing'),lambda *a:None);cid=app.new_chat()['id']
    quiz={'duration_minutes':30,'questions':[{'question':'2+2?','choices':['3','4'],'answer':1,'explanation':'Arithmetic.'}]*10}
    app.execute('UIRender',{'title':'Practice','kind':'quiz','data':json.dumps(quiz)},None,cid)
    assert len(app.chat(cid)['artifacts'][0]['data']['questions'])==10
    policy=copy.deepcopy(DEFAULT);policy['tools']['views']=False;app.store.set_setting('permissions',policy)
    with pytest.raises(PolicyError):app.execute('UIRender',{'title':'Practice','kind':'quiz','data':json.dumps(quiz)},None,cid)
    with pytest.raises(ValueError):validate('quiz',{**quiz,'duration_minutes':0})
    with pytest.raises(ValueError):validate('diagram',{'nodes':[{'id':'a','label':'A'}],'edges':[{'from':'a','to':'bad','label':''}]})
    app.delete_chat(cid);assert not app.artifacts.list(cid);app.close()


def test_static_preview_serves_filtered_snapshot_and_blocks_secrets(tmp_path):
    (tmp_path/'index.html').write_text('<h1>Portfolio</h1>');(tmp_path/'.env').write_text('secret');(tmp_path/'package.json').write_text('{"private":true}')
    preview=Previews();result=preview.start(Workspace(tmp_path))
    try:
        with urllib.request.urlopen(result['url']) as res:assert b'Portfolio' in res.read()
        for name in ('.env','package.json','../.env'):
            with pytest.raises(urllib.error.HTTPError):urllib.request.urlopen(result['url']+name)
        assert result['url'].startswith('http://127.0.0.1:')
    finally:preview.close()


def test_generated_view_has_opaque_route_and_strict_headers():
    views=Views();r=views.stage('<button>Hi</button>',script='document.body.dataset.result="ready"')
    try:
        with urllib.request.urlopen(r['url']) as res:
            policy=res.headers['Content-Security-Policy'];body=res.read().decode()
            assert "connect-src 'none'" in policy and "sandbox allow-scripts" in policy
            assert 'allow-same-origin' not in policy and 'native commands' not in body
        views.stop(r['url'])
        with pytest.raises(urllib.error.HTTPError):urllib.request.urlopen(r['url'])
    finally:views.close()


def test_auto_append_uses_applied_baseline(tmp_path):
    project=tmp_path/'project';project.mkdir();app=App(tmp_path/'data',Path('missing'),lambda *a:None)
    app.store.set_setting('permissions',{'mode':'auto','tools':DEFAULT['tools']});cid=app.new_chat(str(project))['id'];ws=Workspace(project)
    app.execute('Write',{'file_path':'index.html','content':'<main>'},ws,cid)
    app.execute('Append',{'file_path':'index.html','content':'Hello</main>'},ws,cid)
    assert (project/'index.html').read_text()=='<main>Hello</main>'
    assert all(a['status']=='applied' for a in app.store.actions(cid));app.close()


def test_website_phase_constrains_path_without_constraining_content(tmp_path):
    from pebble.workflows import website_phase
    tools,hint=website_phase('Build a portfolio website and run locally',Workspace(tmp_path),[],TOOLS)
    assert len(tools)==1 and tools[0]['function']['name']=='Write'
    spec=tools[0]['function']['parameters']['properties']
    assert spec['file_path']['enum']==['index.html'] and 'enum' not in spec['content']
    App.validate('Write',{'file_path':'index.html','content':'<main>Portfolio</main>'},tools)
    original=next(t for t in TOOLS if t['function']['name']=='Write')
    assert 'enum' not in original['function']['parameters']['properties']['file_path']
