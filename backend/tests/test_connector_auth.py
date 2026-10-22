"""Private credential adapters with real bounded HTTP/PKCE and MCP processes."""
import copy
import json
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlsplit,urlencode

import pytest
from pebble.app import App
from pebble.connector_auth import Records,ConnectorVault,scopes
from pebble.mcp_transport import python_command
from pebble.policy import PolicyError

ACCESS='fixture-access-private-8181'
REFRESH='fixture-refresh-private-7272'


class FakeVault:
    def __init__(self):self.data={};self.fail_set=False;self.fail_delete=False
    def get(self,key):return self.data.get(key,'')
    def set(self,key,value):
        if self.fail_set:raise OSError('Sensitive storage exception '+ACCESS)
        self.data[key]=value
    def delete(self,key):
        if self.fail_delete:raise OSError('Sensitive cleanup exception '+REFRESH)
        self.data.pop(key,None)


@pytest.fixture
def server():
    state={'requests':[],'mode':'normal','token':ACCESS,'refresh':REFRESH,'refresh_calls':0,'tool_calls':0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def write(self,body,status=200):
            self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers()
            try:self.wfile.write(json.dumps(body).encode())
            except (BrokenPipeError,ConnectionResetError):pass
        def do_DELETE(self):self.send_response(204);self.end_headers()
        def do_GET(self):
            base=f'http://127.0.0.1:{self.server.server_port}'
            if self.path.startswith('/.well-known/oauth-protected-resource'):
                body={'resource':base+'/mcp','authorization_servers':[base],'scopes_supported':['read','write']}
                if state['mode']=='wrong_resource':body['resource']=base+'/other'
                if state['mode']=='bad_issuer':body['authorization_servers']=['http://127.0.0.1:1']
            elif self.path.startswith('/.well-known/'):
                body={'issuer':base,'authorization_endpoint':base+'/authorize','token_endpoint':base+'/token','revocation_endpoint':base+'/revoke',
                    'code_challenge_methods_supported':['S256'],'token_endpoint_auth_methods_supported':['none'],'authorization_response_iss_parameter_supported':True}
                if state['mode']=='issuer_mismatch':body['issuer']=base+'/different'
                if state['mode']=='no_pkce':body['code_challenge_methods_supported']=[]
                if state['mode']=='alternate_token':body['token_endpoint']='https://other.example/token'
                if state['mode']=='secret_client':body['token_endpoint_auth_methods_supported']=['client_secret_basic']
            else:body={}
            self.write(body)
        def do_POST(self):
            raw=self.rfile.read(int(self.headers['Content-Length']))
            if self.path in {'/token','/revoke'}:
                form={k:v[0] for k,v in parse_qs(raw.decode()).items()};state['requests'].append((self.path,form))
                if self.path=='/revoke':
                    if state['mode']=='empty_revocation':self.send_response(200);self.end_headers()
                    else:self.write({})
                    return
                if form.get('grant_type')=='refresh_token':
                    state['refresh_calls']+=1
                    if state['mode']=='lost_refresh':self.connection.shutdown(2);self.connection.close();return
                    state['token']='fixture-access-rotated-5151';state['refresh']='fixture-refresh-rotated-4141'
                body={'access_token':state['token'],'refresh_token':state['refresh'],'expires_in':3600,'token_type':'Bearer','scope':'read'}
                if state['mode']=='widen':body['scope']='read write'
                if state['mode']=='bad_token':body['access_token']='bad\ncredential'
                self.write(body);return
            body=json.loads(raw);state['requests'].append(('/mcp',body))
            if self.headers.get('Authorization')!='Bearer '+state['token'] or state['mode']=='unauthorized':self.write({},401);return
            if 'id' not in body:self.send_response(202);self.end_headers();return
            if body['method']=='initialize':result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
            elif body['method']=='tools/list':
                result={'tools':[{'name':'lookup','description':state['token'] if state['mode']=='echo_schema' else 'Read a fixture','inputSchema':{'type':'object'}}]}
            else:
                state['tool_calls']+=1
                result={'content':[{'type':'text','text':'Echo '+state['token']+' '+state['refresh']}]}
            self.write({'jsonrpc':'2.0','id':body['id'],'result':result})
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler);http.daemon_threads=True
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{http.server_port}',state
    http.shutdown();http.server_close();thread.join()


@pytest.fixture
def app(tmp_path):
    app=App(tmp_path/'data',tmp_path/'resources',lambda *args:None)
    app.connector_auth.records=Records(FakeVault())
    yield app
    app.close()


def install(app,server):
    return app.extension_add(kind='plugin',name='Authenticated fixture',content='{"tools":["lookup"]}',url=server[0]+'/mcp')


def sign_in(app,server,id):
    review=app.connector_operation('preview',id=id,issuer=server[0],client_id='fixture-public',requested_scopes=['read'])
    started=app.connector_operation('start',id=id,preview_id=review['preview_id'])
    q=parse_qs(urlsplit(started['url']).query)
    p=app.connector_auth.pending
    callback={'state':q['state'][0],'code':'fixture-code','iss':server[0]}
    with urllib.request.urlopen(p['redirect']+'?'+urlencode(callback),timeout=10) as response:assert response.status==200
    assert app.connector_auth.metadata(id)['state']=='connected'
    return started,review


@pytest.mark.parametrize('value',[[],['read','read'],['read write'],['bad"scope'],[True],'read'])
def test_explicit_scope_validation(value):
    with pytest.raises(PolicyError):scopes(value)


def test_records_publish_last_and_rollback_without_leaking():
    vault=FakeVault();records=Records(vault);records.put('owned',{'token':ACCESS})
    old=copy.deepcopy(vault.data);vault.fail_set=True
    with pytest.raises(PolicyError) as error:records.put('owned',{'token':REFRESH})
    assert ACCESS not in str(error.value) and REFRESH not in str(error.value) and vault.data==old
    vault.fail_set=False;records.put('owned',{'token':REFRESH})
    assert records.get('owned')=={'token':REFRESH} and len(vault.data)==2
    records.delete('owned');assert not vault.data


@pytest.mark.parametrize('manifest',[{}, {'generation':'a'*32,'count':999,'sha256':'x'}, {'generation':'../x','count':1,'sha256':'x'}])
def test_corrupt_vault_record_is_bounded(manifest):
    vault=FakeVault();vault.data['k:manifest']=json.dumps(manifest)
    with pytest.raises(PolicyError):Records(vault).get('k')


def test_namespace_is_separate():
    from pebble.vault import Vault
    assert ConnectorVault.service!=Vault.service


@pytest.mark.parametrize('mode',['wrong_resource','bad_issuer','issuer_mismatch','no_pkce','alternate_token','secret_client'])
def test_metadata_never_authorizes_an_alternate_destination(app,server,mode):
    id=install(app,server);server[1]['mode']=mode
    with pytest.raises((PolicyError,ValueError)):app.connector_operation('preview',id=id,issuer=server[0],client_id='public',requested_scopes=['read'])
    assert not app.connector_auth.records.vault.data and not any(path=='/token' for path,body in server[1]['requests'])


def test_manual_bearer_only_reaches_approved_endpoint_and_is_redacted(app,server,tmp_path):
    id=install(app,server)
    meta=app.connector_operation('save_bearer',id=id,token=ACCESS,requested_scopes=['read'])
    assert meta['kind']=='bearer' and not app.extensions.get(id)['enabled']
    app.extension_toggle(id,True)
    payload=app.extensions.plugin_payload(id,'lookup','{}')
    result=app.extensions.plugin_call(payload)
    assert ACCESS not in json.dumps(result) and '[connector credential redacted]' in result['untrusted_plugin_output']
    assert ACCESS not in (app.home/'pebble.sqlite').read_bytes().decode(errors='ignore')
    assert ACCESS not in json.dumps(app.extensions.list()) and ACCESS not in json.dumps(meta)
    config=copy.deepcopy(app.extensions.get(id)['config']);config['url']=server[0]+'/other'
    with pytest.raises(PolicyError):app.connector_auth.credential(id,config)
    app.connector_operation('disconnect',id=id)
    assert not app.connector_auth.records.vault.data and not app.extensions.get(id)['enabled']


def test_authenticated_schema_metadata_is_redacted_before_sqlite(app,server):
    id=install(app,server);server[1]['mode']='echo_schema'
    app.connector_operation('save_bearer',id=id,token=ACCESS,requested_scopes=['read']);app.extension_toggle(id,True)
    assert ACCESS not in json.dumps(app.extensions.get(id))
    assert '[connector credential redacted]' in app.extensions.get(id)['config']['schemas'][0]['description']


def test_real_pkce_callback_resource_binding_and_replay(app,server):
    id=install(app,server);started,review=sign_in(app,server,id)
    q=parse_qs(urlsplit(started['url']).query)
    assert q['resource']==[server[0]+'/mcp'] and q['code_challenge_method']==['S256'] and len(q['code_challenge'][0])>=43
    exchanged=next(body for path,body in server[1]['requests'] if path=='/token')
    assert exchanged['client_id']=='fixture-public' and exchanged['resource']==server[0]+'/mcp'
    assert app.connector_auth.callback({'state':q['state'],'code':['fixture-code'],'iss':[server[0]]})[0]==400
    assert len([p for p,b in server[1]['requests'] if p=='/token'])==1
    app.extension_toggle(id,True)
    result=app.extensions.plugin_call(app.extensions.plugin_payload(id,'lookup','{}'))
    assert ACCESS not in json.dumps(result) and REFRESH not in json.dumps(result)
    assert server[1]['tool_calls']==1


@pytest.mark.parametrize('change',['state','issuer','duplicate','unknown','missing_code'])
def test_invalid_callback_cannot_consume_or_publish_grant(app,server,change):
    id=install(app,server);review=app.connector_operation('preview',id=id,issuer=server[0],client_id='public',requested_scopes=['read'])
    app.connector_operation('start',id=id,preview_id=review['preview_id']);p=app.connector_auth.pending
    q={'state':[p['state']],'code':['fixture-code'],'iss':[server[0]]}
    if change=='state':q['state']=['wrong']
    elif change=='issuer':q['iss']=[server[0]+'/wrong']
    elif change=='duplicate':q['code']+=['duplicate']
    elif change=='unknown':q['client_secret']=[ACCESS]
    else:q.pop('code')
    assert app.connector_auth.callback(q)[0]==400
    assert app.connector_auth.status()['state']=='waiting' and not app.connector_auth.records.vault.data and not any(path=='/token' for path,body in server[1]['requests'])


@pytest.mark.parametrize('mode',['widen','bad_token'])
def test_invalid_issued_tokens_never_publish(app,server,mode):
    id=install(app,server);review=app.connector_operation('preview',id=id,issuer=server[0],client_id='public',requested_scopes=['read'])
    app.connector_operation('start',id=id,preview_id=review['preview_id']);p=app.connector_auth.pending;server[1]['mode']=mode
    assert app.connector_auth.callback({'state':[p['state']],'code':['fixture'],'iss':[server[0]]})[0]==400
    assert not app.connector_auth.records.vault.data and app.connector_auth.metadata(id)['state']=='disconnected'


def expire(app,id):
    meta=app.connector_auth.metadata(id);record=app.connector_auth.records.get(meta['grant_id']);record['values']['expires_at']=time.time()-1
    app.connector_auth.records.put(meta['grant_id'],record)


def test_serialized_refresh_preserves_public_run_digest(app,server):
    id=install(app,server);sign_in(app,server,id);app.extension_toggle(id,True);expire(app,id)
    config=copy.deepcopy(app.extensions.get(id)['config'])
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:app.connector_auth.credential(id,config),range(4)))
    assert server[1]['refresh_calls']==1 and all('rotated' in result['headers']['Authorization'] for result in results)
    assert app.extensions.get(id)['config']==config
    body=next(body for path,body in server[1]['requests'] if path=='/token' and body['grant_type']=='refresh_token')
    assert body['resource']==server[0]+'/mcp'


@pytest.mark.parametrize('mode',['lost_refresh','widen','vault_failure'])
def test_ambiguous_or_invalid_refresh_requires_reconnect_without_replay(app,server,mode):
    id=install(app,server);sign_in(app,server,id);app.extension_toggle(id,True);expire(app,id)
    config=app.extensions.get(id)['config'];server[1]['mode']=mode
    if mode=='vault_failure':app.connector_auth.records.vault.fail_set=True
    with pytest.raises(PolicyError):app.connector_auth.credential(id,config)
    assert app.connector_auth.metadata(id)['state']=='needs_reconnect'
    with pytest.raises(PolicyError):app.connector_auth.credential(id,config)
    assert server[1]['refresh_calls']==1 and server[1]['tool_calls']==0


def test_revocation_is_local_first_and_failed_cleanup_stays_blocked(app,server):
    id=install(app,server);sign_in(app,server,id);app.extension_toggle(id,True)
    app.connector_auth.records.vault.fail_delete=True
    result=app.connector_operation('disconnect',id=id)
    assert result['storage_cleanup_pending'] and not app.extensions.get(id)['enabled']
    assert app.connector_auth.metadata(id)['state']=='revoked'
    with pytest.raises(PolicyError):app.connector_auth.credential(id,app.extensions.get(id)['config'])
    with pytest.raises(PolicyError):app.extension_remove(id)
    app.connector_auth.records.vault.fail_delete=False;app.extension_remove(id)
    assert not app.connector_auth.records.vault.data and not app.store.query('SELECT * FROM connector_grants')


def test_server_auth_failure_does_not_expand_or_retry(app,server):
    id=install(app,server);app.connector_operation('save_bearer',id=id,token=ACCESS,requested_scopes=['read']);server[1]['mode']='unauthorized'
    with pytest.raises(PolicyError):app.extension_toggle(id,True)
    assert app.connector_auth.metadata(id)['state']=='needs_reconnect' and server[1]['tool_calls']==0


def test_native_grant_changes_require_idle_policy_and_transaction_boundary(app,server):
    id=install(app,server);app.active='other'
    with pytest.raises(PolicyError):app.connector_operation('save_bearer',id=id,token=ACCESS,requested_scopes=['read'])
    app.active=None
    with app.store.lock:
        app.store.db.execute('BEGIN')
        try:
            with pytest.raises(PolicyError):app.connector_operation('save_bearer',id=id,token=ACCESS,requested_scopes=['read'])
            assert app.store.db.in_transaction
        finally:app.store.db.rollback()
    assert not app.connector_auth.records.vault.data


def test_mcp_private_packets_use_pipes(app,server,monkeypatch):
    id=install(app,server);app.connector_operation('save_bearer',id=id,token=ACCESS,requested_scopes=['read'])
    monkeypatch.setattr('tempfile.TemporaryFile',lambda *a,**k:(_ for _ in ()).throw(AssertionError('No credential packet files')))
    app.extension_toggle(id,True)


def test_grant_publication_rollback_preserves_previous_access(app,server):
    id=install(app,server);app.connector_operation('save_bearer',id=id,token=ACCESS,requested_scopes=['read'])
    old=copy.deepcopy(app.connector_auth.records.vault.data);config=copy.deepcopy(app.extensions.get(id)['config'])
    app.store.query("CREATE TRIGGER deny_grant BEFORE UPDATE ON extensions BEGIN SELECT RAISE(ABORT,'fixture publication failure'); END")
    with pytest.raises(PolicyError):app.connector_operation('save_bearer',id=id,token=REFRESH,requested_scopes=['read'])
    assert app.connector_auth.records.vault.data==old and app.extensions.get(id)['config']==config
    app.store.query('DROP TRIGGER deny_grant')


def test_cancelled_callback_cannot_publish_late_response(app,server,monkeypatch):
    id=install(app,server);review=app.connector_operation('preview',id=id,issuer=server[0],client_id='public',requested_scopes=['read'])
    app.connector_operation('start',id=id,preview_id=review['preview_id']);p=app.connector_auth.pending
    entered=threading.Event();release=threading.Event()
    def delayed(*a,**k):
        entered.set();release.wait(3)
        return {'token_type':'Bearer','access_token':ACCESS,'refresh_token':REFRESH,'expires_in':3600}
    monkeypatch.setattr(app.connector_auth,'request',delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(app.connector_auth.callback,{'state':[p['state']],'code':['fixture'],'iss':[server[0]]})
        assert entered.wait(1);app.connector_auth.cancel();release.set();assert future.result()[0]==400
    assert not app.connector_auth.records.vault.data and app.connector_auth.status()['state']=='cancelled'


def test_task_starting_during_browser_signin_prevents_grant_mutation(app,server):
    id=install(app,server);review=app.connector_operation('preview',id=id,issuer=server[0],client_id='public',requested_scopes=['read'])
    app.connector_operation('start',id=id,preview_id=review['preview_id']);p=app.connector_auth.pending;app.active='another-task'
    assert app.connector_auth.callback({'state':[p['state']],'code':['fixture'],'iss':[server[0]]})[0]==400
    assert not app.connector_auth.records.vault.data
    app.active=None


def test_scope_expiry_and_stale_review_are_rejected(app,server):
    id=install(app,server);review=app.connector_operation('preview',id=id,issuer=server[0],client_id='public',requested_scopes=['read'])
    app.connector_auth.previews[review['preview_id']]['deadline']=0
    with pytest.raises(PolicyError):app.connector_operation('start',id=id,preview_id=review['preview_id'])
    review=app.connector_operation('preview',id=id,issuer=server[0],client_id='public',requested_scopes=['read'])
    config=app.extensions.get(id)['config'];config['url']=server[0]+'/changed'
    app.store.query('UPDATE extensions SET config=? WHERE id=?',(json.dumps(config),id))
    with pytest.raises(PolicyError):app.connector_operation('start',id=id,preview_id=review['preview_id'])


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS confined environment grant')
def test_stdio_gets_one_explicit_environment_credential_and_echo_is_redacted(app,server,tmp_path,monkeypatch):
    source=tmp_path/'stdio.py';source.write_text('''import json,sys,os
for line in sys.stdin:
 b=json.loads(line)
 if 'id' not in b:continue
 if b['method']=='initialize':r={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
 elif b['method']=='tools/list':r={'tools':[{'name':'lookup','inputSchema':{'type':'object'}}]}
 else:r={'content':[{'type':'text','text':os.getenv('API_KEY','missing')+':'+str('OTHER_KEY_SECRET' in os.environ)}]}
 print(json.dumps({'jsonrpc':'2.0','id':b['id'],'result':r}),flush=True)
''')
    monkeypatch.setenv('OTHER_KEY_SECRET','foreign')
    config={'transport':'stdio','command':python_command('--unused',framework=True)[0],'args':['-s','-B',str(source)],'read_files':[str(source)],'tools':['lookup']}
    id=app.extension_add(kind='plugin',name='Explicit local grant',content=json.dumps(config))
    app.connector_operation('save_environment',id=id,name='API_KEY',token=ACCESS);app.extension_toggle(id,True)
    result=app.extensions.plugin_call(app.extensions.plugin_payload(id,'lookup','{}'))
    assert result['untrusted_plugin_output']=='[connector credential redacted]:False'
    assert ACCESS not in json.dumps(app.extensions.list())


@pytest.mark.parametrize('name',['PATH','HOME','PYTHONPATH','LD_PRELOAD','NODE_OPTIONS','lower_token'])
def test_environment_grants_cannot_control_runtime(app,server,name):
    id=install(app,server)
    with pytest.raises(PolicyError):app.connector_operation('save_environment',id=id,name=name,token=ACCESS)


def test_discovery_respects_plugin_permission(app,server):
    id=install(app,server);policy=app.store.setting('permissions');policy['tools']['plugins']=False;app.store.set_setting('permissions',policy)
    with pytest.raises(PolicyError):app.dispatch('connector_discover',{'id':id})
    assert not server[1]['requests']


def test_rotation_cleanup_references_survive_restart_and_remove_all_generations():
    vault=FakeVault();records=Records(vault);records.put('owned',{'token':ACCESS})
    vault.fail_delete=True;records.put('owned',{'token':REFRESH})
    records=Records(vault)
    assert records.get('owned')=={'token':REFRESH}
    assert len(json.loads(vault.data['owned:manifest'])['cleanup'])==1
    with pytest.raises(PolicyError):records.delete('owned')
    vault.fail_delete=False;records.delete('owned');assert not vault.data


def test_interrupted_staging_keeps_old_value_and_retryable_chunk_references():
    vault=FakeVault();records=Records(vault);records.put('owned',{'token':ACCESS})
    original=vault.set
    def interrupted(key,value):
        original(key,value)
        if not key.endswith(':manifest'):raise KeyboardInterrupt('Fixture interruption')
    vault.set=interrupted
    with pytest.raises(KeyboardInterrupt):records.put('owned',{'token':REFRESH})
    vault.set=original;records=Records(vault)
    assert records.get('owned')=={'token':ACCESS}
    assert json.loads(vault.data['owned:manifest'])['cleanup']
    records.delete('owned');assert not vault.data


def test_replaced_grants_retain_public_cleanup_refs_and_disconnect_retries(app,server):
    id=install(app,server);app.connector_operation('save_bearer',id=id,token=ACCESS,requested_scopes=['read'])
    first=app.connector_auth.metadata(id)['grant_id'];vault=app.connector_auth.records.vault;vault.fail_delete=True
    result=app.connector_operation('save_bearer',id=id,token=REFRESH,requested_scopes=['read'])
    assert result['storage_cleanup_pending'] and app.connector_auth.metadata(id)['storage_cleanup_pending']
    assert app.store.query('SELECT grant_id FROM connector_cleanup')[0]['grant_id']==first
    with pytest.raises(PolicyError):app.connector_operation('save_bearer',id=id,token=ACCESS,requested_scopes=['read'])
    assert app.connector_operation('disconnect',id=id)['storage_cleanup_pending']
    vault.fail_delete=False
    assert not app.connector_operation('disconnect',id=id)['storage_cleanup_pending']
    assert not vault.data and not app.store.query('SELECT * FROM connector_cleanup')


def test_failed_publication_and_cleanup_preserve_previous_grant_and_retry_refs(app,server):
    id=install(app,server);app.connector_operation('save_bearer',id=id,token=ACCESS,requested_scopes=['read'])
    config=copy.deepcopy(app.extensions.get(id)['config']);vault=app.connector_auth.records.vault;vault.fail_delete=True
    app.store.query("CREATE TRIGGER deny_grant BEFORE UPDATE ON extensions BEGIN SELECT RAISE(ABORT,'fixture publication failure'); END")
    with pytest.raises(PolicyError):app.connector_operation('save_bearer',id=id,token=REFRESH,requested_scopes=['read'])
    assert app.extensions.get(id)['config']==config and app.connector_auth.metadata(id)['storage_cleanup_pending']
    assert app.connector_auth.credential(id,config)['headers']['Authorization']=='Bearer '+ACCESS
    app.store.query('DROP TRIGGER deny_grant');vault.fail_delete=False
    assert not app.connector_operation('disconnect',id=id)['storage_cleanup_pending']
    assert not vault.data and not app.store.query('SELECT * FROM connector_cleanup')


def test_rfc7009_empty_successful_revocation_response_is_confirmed(app,server):
    id=install(app,server);sign_in(app,server,id);server[1]['mode']='empty_revocation'
    assert app.connector_operation('disconnect',id=id)['remote_revocation_confirmed']
