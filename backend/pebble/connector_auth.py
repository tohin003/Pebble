"""Native-only connector grants and public-client OAuth. No model credential API."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .vault import Vault
from .policy import PolicyError
from .mcp_transport import endpoint, isolated_http, MCPNotDispatched, credential_name, route_spec
from .oauth_metadata import (OAuthMetadata, OAuthMetadataError, RegistrationUnknown,
    RegistrationRejected, RequestNotDispatched, validate_client_id_metadata)

# Computed for review, never stored: credential() excludes it from the grant digest.
EXPOSURE = {'environment': 'environment_readable_by_local_commands', 'environment_route': 'reviewed_route_placeholder_only',
    'bearer': 'host_only', 'oauth': 'host_only'}
COMPUTED = {'plugin_id','state','grant_id','revision','storage_cleanup_pending','credential_exposure'}


def packed(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def scopes(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 16 or any(not isinstance(s, str) or not re.fullmatch(r'[\x21\x23-\x5b\x5d-\x7e]{1,160}', s) for s in value) or len(set(value)) != len(value):
        raise PolicyError('Choose 1–16 distinct explicit scopes.')
    return sorted(value)


def secret(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise PolicyError('Enter a bounded printable credential without whitespace.')
    return value


def origin(url):
    p = endpoint(url)
    return (p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80))


class ConnectorVault(Vault):
    service = b'com.pebble.local.connectors'
    # The Windows adapter uses account targets; prefix them independently too.
    def get(self, account): return super().get('connector:'+account)
    def set(self, account, value): return super().set('connector:'+account, value)
    def delete(self, account): return super().delete('connector:'+account)


class Records:
    """Publish-last generation records; credentials never fall back to disk."""
    def __init__(self, vault): self.vault = vault
    def manifest(self, raw):
        if not raw:return None
        info=json.loads(raw)
        if not isinstance(info,dict) or set(info)-{'generation','count','sha256','cleanup'} or not {'generation','count','sha256'}<=set(info):raise ValueError()
        def generation(value):
            return (isinstance(value,dict) and set(value)=={'generation','count'} and isinstance(value['generation'],str)
                and re.fullmatch('[a-f0-9]{32}',value['generation']) and type(value['count']) is int and 1<=value['count']<=40)
        active={'generation':info['generation'],'count':info['count']}
        if not (generation(active) and isinstance(info['sha256'],str) and re.fullmatch('[a-f0-9]{64}',info['sha256'])):
            if active!={'generation':None,'count':0} or type(info['count']) is not int or info['sha256'] is not None:raise ValueError()
        cleanup=info.get('cleanup',[])
        if not isinstance(cleanup,list) or len(cleanup)>17 or any(not generation(v) for v in cleanup):raise ValueError()
        if len({v['generation'] for v in cleanup})!=len(cleanup) or info['generation'] in {v['generation'] for v in cleanup}:raise ValueError()
        return info
    def clean(self, key, info):
        pending=[]
        for value in info.get('cleanup',[]):
            try:
                for i in range(value['count']):self.vault.delete(f"{key}:{value['generation']}:{i}")
            except Exception:pending.append(value)
        updated={**info,'cleanup':pending}
        # A failed journal update retains retryable references, never credentials.
        if updated!=info:self.vault.set(key+':manifest',packed(updated))
        return updated
    def get(self, key):
        try:
            info = self.manifest(self.vault.get(key+':manifest'))
            if not info or not info['generation']:raise ValueError()
            data = ''.join(self.vault.get(f"{key}:{info['generation']}:{i}") for i in range(info['count']))
            if len(data) > 72000 or hashlib.sha256(data.encode()).hexdigest() != info['sha256']:
                raise ValueError()
            return json.loads(data)
        except Exception:
            raise PolicyError('Connector credential storage is unavailable or invalid; reconnect.') from None
    def put(self, key, value):
        data = packed(value)
        if len(data) > 72000:
            raise PolicyError('Connector credential record exceeded its limit.')
        try:old=self.manifest(self.vault.get(key+':manifest'))
        except Exception:raise PolicyError('Connector credential storage is unavailable.') from None
        generation = uuid.uuid4().hex; parts = [data[i:i+1800] for i in range(0,len(data),1800)]
        try:
            old=self.clean(key,old) if old else {'generation':None,'count':0,'sha256':None,'cleanup':[]}
            if len(old.get('cleanup',[]))>=16:raise ValueError()
            # Register staging before writing any credential chunk. A crash or
            # failed final publication leaves the previous value and cleanup refs.
            journal={**old,'cleanup':[*old.get('cleanup',[]),{'generation':generation,'count':len(parts)}]}
            self.vault.set(key+':manifest',packed(journal))
            for i,part in enumerate(parts):
                self.vault.set(f'{key}:{generation}:{i}',part)
            retired=old.get('cleanup',[])+([{'generation':old['generation'],'count':old['count']}] if old['generation'] else [])
            committed={'generation':generation,'count':len(parts),'sha256':hashlib.sha256(data.encode()).hexdigest(),'cleanup':retired}
            self.vault.set(key+':manifest',packed(committed))
        except Exception:
            try:
                current=self.manifest(self.vault.get(key+':manifest'))
                if current:self.clean(key,current)
            except Exception:pass
            raise PolicyError('Connector credential could not be saved securely.') from None
        try:self.clean(key,committed)
        except Exception:pass
    def delete(self, key):
        try:self._delete(key)
        except Exception:raise PolicyError('Connector credential cleanup needs secure storage access.') from None
    def _delete(self, key):
        # Keep the manifest until all chunks are removed so a failed deletion can retry.
        raw=self.vault.get(key+':manifest')
        if not raw:return
        info=self.manifest(raw)
        generations=info.get('cleanup',[])+([{'generation':info['generation'],'count':info['count']}] if info['generation'] else [])
        for value in generations:
            for i in range(value['count']):self.vault.delete(f"{key}:{value['generation']}:{i}")
        self.vault.delete(key+':manifest')


class ConnectorAuth:
    def __init__(self, app, vault=None):
        self.app, self.store = app, app.store
        self.records=Records(vault if vault is not None else ConnectorVault())
        self.lock=threading.RLock();self.locks={};self.previews={};self.pending=None;self.server=None;self.timer=None
        self.result={'state':'idle'};self.redactions={}
        self.registration_pending=None
        self.interop=OAuthMetadata(self.metadata_request)
        self.store.query('CREATE TABLE IF NOT EXISTS connector_grants(plugin_id TEXT PRIMARY KEY,grant_id TEXT,revision INTEGER,state TEXT,metadata TEXT,updated REAL)')
        self.store.query('CREATE TABLE IF NOT EXISTS connector_cleanup(plugin_id TEXT,grant_id TEXT PRIMARY KEY)')
        # Public effect admission is committed before registration's HTTP POST.
        # Interrupted admissions survive restart as unknown; they are never replayed.
        self.store.query('CREATE TABLE IF NOT EXISTS connector_registrations(registration_id TEXT PRIMARY KEY,plugin_id TEXT,issuer TEXT,resource TEXT,base_digest TEXT,plan_digest TEXT,state TEXT,grant_id TEXT,metadata TEXT,updated REAL)')
        self.store.query("UPDATE connector_registrations SET state='unknown' WHERE state='admitted'")
    def boundary(self):
        self.app.extensions.boundary()
    def item(self, id):
        item=self.app.extensions.get(id)
        if item['kind']!='plugin':raise PolicyError('Choose an installed plugin.')
        return item
    def idle(self):
        if self.app.active:raise PolicyError('Stop the current task before changing connector access.')
    def base(self, item):
        value={k:item['config'].get(k) for k in ('transport','url','command','args','read_files','launch_files','tools')}
        for key in ('resources','prompts','protocol_era'):
            if key in item['config']:value[key]=item['config'][key]
        return digest(value)
    def metadata(self, id):
        self.boundary()
        self.item(id)
        rows=self.store.query('SELECT * FROM connector_grants WHERE plugin_id=?',(id,))
        cleanup=bool(self.store.query('SELECT grant_id FROM connector_cleanup WHERE plugin_id=?',(id,)))
        if not rows:return {'state':'disconnected','plugin_id':id,'storage_cleanup_pending':cleanup}
        row=rows[0]
        metadata=json.loads(row['metadata'])
        expected={'kind','resource','issuer','client_id','scopes','environment_names','endpoints','base_digest'}
        if not isinstance(metadata,dict) or set(metadata)!=expected:raise PolicyError('Stored connector grant metadata is invalid.')
        return {**metadata,'plugin_id':id,'state':row['state'],'grant_id':row['grant_id'],'revision':row['revision'],'storage_cleanup_pending':cleanup,
            'credential_exposure':EXPOSURE.get(metadata['kind'])}
    def request(self, url, form=None, cancel=None, revocation=False):
        try:
            reply=isolated_http({'url':url,'body':None,'form':form,'purpose':'oauth-revoke' if revocation else 'oauth-json','method':'POST' if form is not None else 'GET',
                'headers':{'Accept':'application/json',**({'Content-Type':'application/x-www-form-urlencoded'} if form is not None else {})}},cancel or threading.Event(),8)
            if revocation:
                if reply.get('status')!=200:raise ValueError()
                return {}
            if reply.get('http_error') or reply.get('transport_error') or not isinstance(reply.get('document'),dict):raise ValueError()
            return reply['document']
        except Exception:
            raise PolicyError('Connector authentication request failed; no response body or credential is exposed.') from None
    def metadata_request(self, packet, cancel=None):
        """Unauthenticated JSON only. The isolated worker owns checked DNS/TLS."""
        if (set(packet)-{'url','method','headers','body','purpose'} or packet.get('method') not in {'GET','POST'}
                or set(packet.get('headers',{}))-{'Accept','Content-Type'} or packet.get('purpose')!='oauth-json'):
            raise PolicyError('Invalid unauthenticated OAuth metadata packet.')
        try:return isolated_http(packet,cancel if cancel is not None else threading.Event(),8)
        except MCPNotDispatched:raise RequestNotDispatched('Authentication stopped before HTTP admission.') from None
    def discover_interop(self, id, www_authenticate=None):
        self.boundary();self.idle();item=self.item(id);url=item['config'].get('url')
        if item['config'].get('transport','http')!='http':raise PolicyError('OAuth metadata discovery requires an HTTP plugin.')
        endpoint(url)
        if www_authenticate is None:
            try:
                probe=isolated_http({'url':url,'body':None,'method':'GET',
                    'headers':{'Accept':'application/json'},'purpose':'oauth-json'},threading.Event(),8)
            except Exception:probe={}
            if probe.get('auth_challenge_invalid'):raise PolicyError('Invalid or excessive authentication challenge.')
            www_authenticate=probe.get('auth_challenge','')
            if probe.get('session'):
                try:isolated_http({'url':url,'body':None,'method':'DELETE','headers':{'MCP-Session-Id':probe['session'],'MCP-Protocol-Version':'2025-11-25'}},threading.Event(),.5)
                except Exception:pass
        value=self.interop.discover_resource(url,www_authenticate)
        history=self.store.query('SELECT issuer,resource,state,metadata,plan_digest FROM connector_registrations WHERE plugin_id=? ORDER BY updated DESC LIMIT 16',(id,))
        registered=[];unknown=[]
        for row in history:
            if row['state']=='registered' and row['metadata']:
                public=json.loads(row['metadata'])
                registered.append({'issuer':row['issuer'],'resource':row['resource'],'client_id':public['client_id']})
            if row['state'] in {'unknown','admitted'}:
                unknown.append({'issuer':row['issuer'],'resource':row['resource'],'state':'unknown','plan_digest':row['plan_digest']})
        return {**value,'plugin_id':id,'origin':'untrusted_server_metadata','authorization':'none',
            'registration_methods':['pre_registered','client_id_metadata','dynamic_registration'],
            'registration_capabilities_require_issuer_discovery':True,
            'registered_clients':registered[-16:],'unresolved_registrations':unknown[-16:]}
    def callback_server(self, port=0):
        owner=self
        class Callback(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if len(self.path)>4096 or urlsplit(self.path).path!='/connector/callback':self.send_error(404);return
                query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                code,message=owner.callback(query)
                self.send_response(code);self.send_header('Content-Type','text/plain; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'none'");self.end_headers();self.wfile.write(message.encode())
        try:server=ThreadingHTTPServer(('127.0.0.1',port),Callback)
        except Exception:raise PolicyError('The exact native callback could not be reserved; no registration was sent.') from None
        server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
        return server,f'http://127.0.0.1:{server.server_port}/connector/callback'
    def registration_budget(self, id):
        if (self.store.query('SELECT COUNT(*) AS n FROM connector_registrations WHERE plugin_id=?',(id,))[0]['n']>=16
                or self.store.query('SELECT COUNT(*) AS n FROM connector_registrations')[0]['n']>=800):
            raise PolicyError('Registration admission limit reached. Inspect existing client registrations and use an existing client ID; no records or private cleanup references are pruned.')
    def preview_registration(self, id, issuer, requested_scopes, registration_method='dynamic_registration',
                             client_id='',client_metadata=None,client_name='Pebble'):
        self.boundary();self.idle();item=self.item(id);requested=scopes(requested_scopes)
        if registration_method not in {'pre_registered','client_id_metadata','dynamic_registration'}:
            raise PolicyError('Choose a supported public-client registration method.')
        if registration_method=='dynamic_registration':self.registration_budget(id)
        with self.lock:
            if self.pending or self.registration_pending:raise PolicyError('Finish or cancel the existing connector review/sign-in.')
        resource=self.discover_interop(id)
        if issuer not in resource['authorization_servers']:raise PolicyError('Select an advertised authorization server.')
        config=self.interop.discover_issuer(issuer,resource=resource['resource'])
        if registration_method=='dynamic_registration' and self.store.query("SELECT registration_id FROM connector_registrations WHERE plugin_id=? AND issuer=? AND resource=? AND state IN ('unknown','admitted')",(id,issuer,resource['resource'])):
            raise PolicyError('A prior registration has an unknown outcome. Reconcile it with the issuer; use its existing client ID instead of repeating registration.')
        supplied=None;port=0
        if registration_method=='pre_registered':
            if not isinstance(client_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}',client_id):raise PolicyError('Enter the registered public client ID.')
        elif registration_method=='client_id_metadata':
            if not config['client_id_metadata_document_supported']:raise PolicyError('The issuer did not advertise client ID metadata support.')
            validate_client_id_metadata(client_id)
            # A supplied document is validated, never uploaded. Otherwise verify
            # the existing public reference through the same bounded transport.
            supplied=self.interop.read_client_id_metadata(client_id,resource=resource['resource'],document=client_metadata)
            document=supplied['client_metadata']
            compatible=[urlsplit(u) for u in supplied['client_metadata']['redirect_uris']
                if urlsplit(u).scheme=='http' and urlsplit(u).hostname=='127.0.0.1' and urlsplit(u).path=='/connector/callback']
            if not compatible:raise PolicyError('Client metadata needs an exact literal loopback /connector/callback redirect for this native client.')
            port=compatible[0].port
            supplied['hosting_verified']=client_metadata is None
        cancel=threading.Event();server=None
        try:
            with self.lock:
                if self.pending or self.registration_pending:raise PolicyError('A connector review started while metadata was loading.')
                self.idle()
                if self.base(self.item(id))!=self.base(item):raise PolicyError('Plugin changed while registration metadata was loading.')
                server,redirect=self.callback_server(port)
                value={'plugin_id':id,'resource':resource['resource'],'issuer':issuer,'client_id':client_id,'scopes':requested,
                    'endpoints':config['endpoints'],'require_iss':config['require_iss'],'base_digest':self.base(item)}
                helper=OAuthMetadata(lambda packet:self.metadata_request(packet,cancel))
                if registration_method=='dynamic_registration':
                    public=helper.preview_registration(config,resource=resource['resource'],redirect_uri=redirect,
                        client_name=client_name,scopes=requested,binding=value['base_digest'])
                    ident=public['plan_id']
                else:
                    if supplied:validate_client_id_metadata(client_id,document,redirect_uri=redirect)
                    ident=uuid.uuid4().hex
                    public={'plan_id':ident,'nonce':secrets.token_urlsafe(32),'resource':resource['resource'],'issuer':issuer,
                        'client_id':client_id,'endpoints':config['endpoints'],'redirect_uri':redirect,'scopes':requested,
                        'binding':value['base_digest'],'registration_method':registration_method,'client_metadata':supplied,
                        'requires_native_review':True,'expires_in':300}
                    public['digest']=digest(public)
                flow={'id':ident,'public':public,'value':value,'method':registration_method,'state':'review','server':server,
                    'redirect':redirect,'cancel':cancel,'helper':helper,'deadline':time.monotonic()+300}
                self.registration_pending=flow;self.server=server
                self.result={'state':'registration_review','plugin_id':id}
                self.timer=threading.Timer(300,self.cancel,kwargs={'state':'expired','expected_registration':ident});self.timer.daemon=True;self.timer.start()
                return {**public,'preview_id':ident,'plugin_id':id,'registration_method':registration_method,
                    'client_id_metadata_document_supported':config['client_id_metadata_document_supported']}
        except Exception:
            if server:server.shutdown();server.server_close()
            raise
    def register_client(self, id, preview_id, digest):
        self.boundary();self.idle();item=self.item(id)
        with self.lock:
            flow=self.registration_pending
            if (not flow or flow['id']!=preview_id or flow['value']['plugin_id']!=id or flow['state']!='review'
                    or flow['deadline']<=time.monotonic() or flow['value']['base_digest']!=self.base(item)
                    or not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest)
                    or not hmac.compare_digest(flow['public']['digest'],digest)):
                raise PolicyError('Registration review expired, changed or was already consumed.')
            flow['state']='registering';self.result={'state':'registering','plugin_id':id}
        registration_id=None
        try:
            if flow['method']=='dynamic_registration':
                registration_id=uuid.uuid4().hex
                with self.store.lock:
                    self.registration_budget(id)
                    self.store.query('INSERT INTO connector_registrations VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (registration_id,id,flow['value']['issuer'],flow['value']['resource'],flow['value']['base_digest'],digest,'admitted',registration_id,None,time.time()))
                outcome=flow['helper'].register(preview_id,digest,binding=flow['value']['base_digest'])
                with self.app.task_lock,self.lock:
                    if self.registration_pending is not flow or flow['cancel'].is_set() or flow['deadline']<=time.monotonic():raise RegistrationUnknown('Registration completed after cancellation; reconcile provider state.')
                    self.idle()
                    if self.base(self.item(id))!=flow['value']['base_digest']:raise RegistrationUnknown('Plugin changed during registration; reconcile provider state.')
                    # A public client never uses or retains an unsolicited secret.
                    private={key:value for key,value in outcome.private.items() if key in {'registration_access_token','registration_client_uri'}}
                    if private:self.records.put(registration_id,{'kind':'registration','issuer':flow['value']['issuer'],
                        'resource':flow['value']['resource'],'client_id':outcome.public['client_id'],'values':private})
                    self.remember(id,private)
                    if flow['cancel'].is_set():raise RegistrationUnknown('Registration was cancelled during secure persistence.')
                    self.store.query("UPDATE connector_registrations SET state='registered',grant_id=?,metadata=?,updated=? WHERE registration_id=?",
                        (registration_id if private else None,packed(outcome.public),time.time(),registration_id))
                    flow['value']['client_id']=outcome.public['client_id']
            with self.app.task_lock,self.lock:
                if self.registration_pending is not flow or flow['cancel'].is_set() or flow['deadline']<=time.monotonic():raise PolicyError('Connector registration review expired or was cancelled.')
                self.idle()
                if self.base(self.item(id))!=flow['value']['base_digest']:raise PolicyError('Plugin changed during registration.')
                ident=uuid.uuid4().hex
                self.previews[ident]={'value':flow['value'],'deadline':time.monotonic()+300,'server':flow['server'],'redirect':flow['redirect'],'registration_flow':flow}
                flow['state']='ready';flow['deadline']=time.monotonic()+300
                if self.timer:self.timer.cancel()
                self.timer=threading.Timer(300,self.cancel,kwargs={'state':'expired','expected_registration':preview_id});self.timer.daemon=True;self.timer.start()
                self.result={'state':'registration_ready','plugin_id':id}
                return {'preview_id':ident,'plugin_id':id,'client_id':flow['value']['client_id'],'resource':flow['value']['resource'],
                    'issuer':flow['value']['issuer'],'endpoints':flow['value']['endpoints'],'scopes':flow['value']['scopes'],
                    'redirect_uri':flow['redirect'],'registration_method':flow['method'],'requires_native_review':True,'expires_in':300}
        except Exception as exc:
            if registration_id:
                state='rejected' if isinstance(exc,(RegistrationRejected,RequestNotDispatched)) else 'unknown'
                self.store.query('UPDATE connector_registrations SET state=?,updated=? WHERE registration_id=?',(state,time.time(),registration_id))
            self.cancel('registration_failed',expected_registration=preview_id)
            raise PolicyError('Client registration did not produce a securely usable reviewed result. Check the issuer before registering again; private response details are withheld.') from None
    def discover(self, id):
        item=self.item(id);url=item['config'].get('url')
        if item['config'].get('transport','http')!='http':raise PolicyError('OAuth discovery is for HTTP plugins; local workers use explicit environment grants.')
        p=endpoint(url);suffix=p.path.rstrip('/')
        candidates=[urlunsplit((p.scheme,p.netloc,'/.well-known/oauth-protected-resource'+suffix,'',''))]
        try:
            probe=isolated_http({'url':url,'body':None,'method':'GET',
                'headers':{'Accept':'application/json'},'purpose':'oauth-json'},threading.Event(),8)
        except Exception:probe={}
        challenge=probe.get('auth_challenge','')
        advertised=re.findall(r'(?:^|[,\s])resource_metadata="([^"\\]+)"',challenge) if challenge.lower().startswith('bearer ') else []
        if advertised:
            if len(advertised)!=1 or origin(advertised[0])!=origin(url):raise PolicyError('Protected resource discovery cannot leave the reviewed MCP origin.')
            candidates.insert(0,advertised[0])
        if probe.get('session'):
            try:isolated_http({'url':url,'body':None,'method':'DELETE','headers':{'MCP-Session-Id':probe['session'],'MCP-Protocol-Version':'2025-11-25'}},threading.Event(),.5)
            except Exception:pass
        fallback=urlunsplit((p.scheme,p.netloc,'/.well-known/oauth-protected-resource','',''))
        if fallback not in candidates:candidates.append(fallback)
        resource=None
        for candidate in candidates:
            try:resource=self.request(candidate);break
            except PolicyError:pass
        if not resource or resource.get('resource')!=url:
            raise PolicyError('Protected resource metadata must match the exact reviewed MCP endpoint.')
        issuers=resource.get('authorization_servers')
        if not isinstance(issuers,list) or not 1 <= len(issuers) <= 4 or any(not isinstance(u,str) for u in issuers) or len(set(issuers))!=len(issuers):
            raise PolicyError('Protected resource metadata needs 1–4 distinct authorization servers.')
        for issuer in issuers:
            endpoint(issuer)
            if urlsplit(issuer).scheme!='https' and origin(issuer)!=origin(url):raise PolicyError('A local resource cannot redirect authentication to another plaintext destination.')
        suggested=scopes(resource['scopes_supported']) if resource.get('scopes_supported') else []
        return {'plugin_id':id,'resource':url,'authorization_servers':issuers,'suggested_scopes':suggested,'origin':'untrusted_server_metadata','authorization':'none'}
    def preview(self, id, issuer, client_id, requested_scopes):
        self.boundary();self.idle();item=self.item(id);requested=scopes(requested_scopes)
        if not isinstance(client_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}',client_id):raise PolicyError('Enter the public client ID registered for this connector; client secrets are unsupported.')
        resource=self.discover(id)
        if issuer not in resource['authorization_servers']:raise PolicyError('Select an advertised authorization server.')
        p=endpoint(issuer);path=p.path.rstrip('/')
        candidates=[urlunsplit((p.scheme,p.netloc,'/.well-known/oauth-authorization-server'+path,'','')),
                    urlunsplit((p.scheme,p.netloc,'/.well-known/openid-configuration'+path,'','')),
                    issuer.rstrip('/')+'/.well-known/openid-configuration']
        config=None
        for candidate in dict.fromkeys(candidates):
            try:config=self.request(candidate);break
            except PolicyError:pass
        if not config or config.get('issuer')!=issuer or 'S256' not in config.get('code_challenge_methods_supported',[]):raise PolicyError('Issuer metadata must match and advertise S256 PKCE.')
        reviewed={}
        for name in ('authorization_endpoint','token_endpoint','revocation_endpoint'):
            url=config.get(name)
            if not url and name=='revocation_endpoint':continue
            if not isinstance(url,str) or origin(url)!=origin(issuer):raise PolicyError('Authentication endpoints must stay on the reviewed issuer origin.')
            reviewed[name]=url
        if 'token_endpoint_auth_methods_supported' in config and 'none' not in config['token_endpoint_auth_methods_supported']:
            raise PolicyError('This connector requires a public client without client-secret authentication.')
        value={'plugin_id':id,'resource':resource['resource'],'issuer':issuer,'client_id':client_id,'scopes':requested,
            'endpoints':reviewed,'require_iss':config.get('authorization_response_iss_parameter_supported') is True,'base_digest':self.base(item)}
        ident=uuid.uuid4().hex
        with self.lock:
            self.previews={k:v for k,v in self.previews.items() if v['deadline']>time.monotonic()}
            if len(self.previews)>=4:raise PolicyError('At most four pending connector reviews are retained.')
            self.previews[ident]={'value':value,'deadline':time.monotonic()+300}
        return {'preview_id':ident,**{k:v for k,v in value.items() if k not in {'require_iss','base_digest'}},'requires_native_review':True,'expires_in':300}
    def start(self, id, preview_id):
        self.boundary();self.idle()
        with self.lock:
            preview=self.previews.pop(preview_id,None)
            if not preview or preview['deadline']<=time.monotonic() or preview['value']['plugin_id']!=id or preview['value']['base_digest']!=self.base(self.item(id)):
                raise PolicyError('Connector review expired or changed; preview it again.')
            if self.pending:raise PolicyError('Finish or cancel the existing connector sign-in.')
            flow=preview.get('registration_flow')
            if self.registration_pending and (self.registration_pending is not flow or flow['state']!='ready'):
                raise PolicyError('Finish or cancel the existing client registration review.')
            value=preview['value'];state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48)
            if flow:
                if flow['cancel'].is_set() or self.server is not flow['server']:raise PolicyError('The reviewed registration callback is no longer owned.')
                self.server=flow['server'];redirect=flow['redirect'];cancel=flow['cancel'];self.registration_pending=None
            else:self.server,redirect=self.callback_server();cancel=threading.Event()
            self.pending={'value':value,'state':state,'verifier':verifier,'redirect':redirect,'deadline':time.monotonic()+300,'cancel':cancel}
            self.pending['server']=self.server
            self.result={'state':'waiting','plugin_id':id}
            if self.timer:self.timer.cancel()
            self.timer=threading.Timer(300,self.expire,args=(state,));self.timer.daemon=True;self.timer.start()
            query={'response_type':'code','client_id':value['client_id'],'redirect_uri':redirect,'scope':' '.join(value['scopes']),'resource':value['resource'],'state':state,
                'code_challenge_method':'S256','code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
            url=value['endpoints']['authorization_endpoint']+'?'+urlencode(query)
            self.pending['authorization_url']=url
            return {'url':url,'state':'waiting','plugin_id':id}
    def expire(self, state):
        with self.lock:
            if not self.pending or self.pending['state']!=state:return
        self.cancel('expired',expected=state)
    def cancel(self, state='cancelled',expected=None,expected_registration=None):
        with self.lock:
            if expected and (not self.pending or self.pending['state']!=expected):return False
            if expected_registration and (not self.registration_pending or self.registration_pending['id']!=expected_registration):return False
            if self.pending:self.pending['cancel'].set()
            if self.registration_pending:
                flow=self.registration_pending;flow['cancel'].set();flow['helper'].cancel_registration(flow['id'])
                self.previews={key:value for key,value in self.previews.items() if value.get('registration_flow') is not flow}
            self.registration_pending=None
            self.pending=None;self.result={'state':state};server,self.server=self.server,None
            if self.timer:self.timer.cancel();self.timer=None
        if server:
            server.shutdown();server.server_close()
        return True
    def status(self):
        with self.lock:return dict(self.result)
    def tokens(self, response, allowed, previous=None):
        if not isinstance(response.get('token_type'),str) or response['token_type'].lower()!='bearer' or type(response.get('expires_in')) is not int or not 1<=response['expires_in']<=86400:raise PolicyError('Invalid OAuth credential response.')
        if 'scope' in response and not isinstance(response['scope'],str):raise PolicyError('Invalid OAuth scope response.')
        issued=scopes(response['scope'].split()) if 'scope' in response else allowed
        if not set(issued)<=set(allowed):raise PolicyError('OAuth response widened the reviewed scopes.')
        record={'access_token':secret(response.get('access_token')),'expires_at':time.time()+response['expires_in'],'scopes':issued}
        refresh=response.get('refresh_token',previous.get('refresh_token') if previous else None)
        if refresh:record['refresh_token']=secret(refresh)
        return record
    def callback(self, query):
        with self.lock:
            p=self.pending
            if (not p or self.result.get('state')!='waiting' or p['deadline']<=time.monotonic() or set(query)-{'state','code','error','iss'}
                    or any(not isinstance(v,list) or len(v)!=1 or not isinstance(v[0],str) or len(v[0])>2048 for v in query.values())
                    or not hmac.compare_digest(query.get('state',[''])[0],p['state'])):
                return 400,'Invalid or expired connector callback.'
            value=p['value']
            if query.get('error') or not query.get('code') or (value['require_iss'] or 'iss' in query) and query.get('iss',[''])[0]!=value['issuer']:
                return 400,'Connector sign-in was declined or issuer did not match.'
            self.result={'state':'validating','plugin_id':value['plugin_id']}
        try:
            response=self.request(value['endpoints']['token_endpoint'],{'grant_type':'authorization_code','client_id':value['client_id'],'code':query['code'][0],
                'code_verifier':p['verifier'],'redirect_uri':p['redirect'],'resource':value['resource']},p['cancel'])
            credential=self.tokens(response,value['scopes'])
            with self.app.task_lock,self.lock:
                if self.pending is not p or p['cancel'].is_set() or p['deadline']<=time.monotonic():raise PolicyError('Sign-in was cancelled or expired.')
                self.idle();item=self.item(value['plugin_id'])
                if self.base(item)!=value['base_digest']:raise PolicyError('Plugin changed during sign-in.')
                self.bind(item,'oauth',value,credential)
                self.pending=None;self.result={'state':'connected','plugin_id':value['plugin_id']}
            threading.Thread(target=self.stop_callback,args=(p['server'],),daemon=True).start()
            return 200,'Connector connected. Return to Pebble and review/enable its tools.'
        except Exception:
            with self.lock:
                if self.pending is p:self.pending=None;self.result={'state':'failed','plugin_id':value['plugin_id'],'error':'Sign-in could not be committed securely. Stop active work and reconnect; prior access is preserved.'}
            threading.Thread(target=self.stop_callback,args=(p['server'],),daemon=True).start()
            return 400,'Connector sign-in failed. Return to Pebble for status.'
    def stop_callback(self,server):
        with self.lock:
            if self.server is server:
                self.server=None
                if self.timer:self.timer.cancel();self.timer=None
        if server:server.shutdown();server.server_close()
    def bind(self, item, kind, public, values):
        self.boundary();ident=item['id'];old=self.metadata(ident);gid=uuid.uuid4().hex;revision=old.get('revision',0)+1
        if not self.cleanup(ident):raise PolicyError('Retry secure credential cleanup before replacing connector access.')
        metadata={'kind':kind,'resource':public.get('resource'),'issuer':public.get('issuer'),'client_id':public.get('client_id'),
            'scopes':values.get('scopes',public.get('scopes',[])),'environment_names':public.get('environment_names',[]),
            'endpoints':public.get('endpoints',{}),'base_digest':self.base(item)}
        binding={'grant_id':gid,'revision':revision,'digest':digest(metadata)}
        # Public opaque references survive a crash before grant publication.
        self.store.query('INSERT INTO connector_cleanup VALUES(?,?)',(ident,gid))
        self.records.put(gid,{'binding':binding,'metadata':metadata,'values':values})
        config={k:v for k,v in item['config'].items() if k not in {'auth_binding','schemas','protocol','catalogue_digest','content_catalogue','content_digest'}}
        config['auth_binding']=binding
        try:
            with self.store.lock:
                if self.store.db.in_transaction:raise PolicyError('Grant publication requires an outside transaction boundary.')
                with self.store.db as db:
                    db.execute('INSERT OR REPLACE INTO connector_grants VALUES(?,?,?,?,?,?)',(ident,gid,revision,'connected',packed(metadata),time.time()))
                    db.execute('UPDATE extensions SET config=?,enabled=0 WHERE id=?',(packed(config),ident))
                    db.execute('DELETE FROM connector_cleanup WHERE grant_id=?',(gid,))
                    if old.get('grant_id'):db.execute('INSERT OR IGNORE INTO connector_cleanup VALUES(?,?)',(ident,old['grant_id']))
        except Exception:
            try:self.records.delete(gid);self.store.query('DELETE FROM connector_cleanup WHERE grant_id=?',(gid,))
            except Exception:pass
            raise PolicyError('Connector access was not published; previous grant is preserved.') from None
        self.app.extensions.close_plugin(ident)
        self.remember(ident,values)
        clean=self.cleanup(ident)
        return {**self.metadata(ident),'storage_cleanup_pending':not clean}
    def cleanup(self,id):
        pending=False
        for row in self.store.query('SELECT grant_id FROM connector_cleanup WHERE plugin_id=?',(id,)):
            try:
                self.records.delete(row['grant_id'])
                self.store.query('UPDATE connector_registrations SET grant_id=NULL WHERE grant_id=?',(row['grant_id'],))
                self.store.query('DELETE FROM connector_cleanup WHERE grant_id=?',(row['grant_id'],))
            except Exception:pending=True
        return not pending
    def save_bearer(self,id,token,requested_scopes):
        self.boundary();self.idle();item=self.item(id)
        if item['config'].get('transport','http')!='http':raise PolicyError('Bearer tokens require an HTTP destination.')
        return self.bind(item,'bearer',{'resource':item['config']['url'],'scopes':scopes(requested_scopes)}, {'access_token':secret(token)})
    def save_environment(self,id,name,token):
        self.boundary();self.idle();item=self.item(id)
        if item['config'].get('transport')!='stdio' or not isinstance(name,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',name) or not name.endswith(('TOKEN','API_KEY','SECRET','PASSWORD')):
            raise PolicyError('Local grants require one explicit credential environment name ending TOKEN, API_KEY, SECRET or PASSWORD.')
        return self.bind(item,'environment',{'environment_names':[name]}, {'environment':{name:secret(token)}})
    def save_environment_route(self,id,name,token,base_url_env,origin,methods,path_prefix,header='Authorization',scheme='Bearer'):
        """Opt-in F2 route: the server gets a placeholder and a loopback base URL; its supervisor's broker adds the header.

        Every shape is refused before the vault is touched. The stored key set is the
        environment grant's, and the route sits inside the grant digest, so any route
        edit needs a reconnect. Existing environment grants are never converted.
        """
        self.boundary();self.idle();item=self.item(id)
        if item['config'].get('transport')!='stdio' or not credential_name(name):
            raise PolicyError('Local grants require one explicit credential environment name ending TOKEN, API_KEY, SECRET or PASSWORD.')
        route=route_spec({'base_url_env':base_url_env,'origin':origin,'methods':methods,'path_prefix':path_prefix,'header':header,'scheme':scheme},{name:''})
        token=secret(token)
        return self.bind(item,'environment_route',{'resource':route['origin'],'environment_names':[name],'endpoints':{'credential_route':route}},
            {'environment':{name:token}})
    def remember(self,id,values):
        tokens=[values.get('access_token'),values.get('refresh_token'),values.get('registration_access_token'),*values.get('environment',{}).values()]
        with self.lock:self.redactions[id]=list(dict.fromkeys([*self.redactions.get(id,[]),*[v for v in tokens if v]]))[-8:]
    def redact(self,id,value):
        with self.lock:tokens=tuple(self.redactions.get(id,[]))
        def clean(v):
            if isinstance(v,str):
                for token in tokens:v=v.replace(token,'[connector credential redacted]')
                return v
            if isinstance(v,list):return [clean(x) for x in v]
            if isinstance(v,dict):return {clean(k):clean(x) for k,x in v.items()}
            return v
        return clean(value)
    def credential(self,id,config,cancel=None):
        binding=config.get('auth_binding')
        if not binding:return None
        with self.lock:lock=self.locks.setdefault(id,threading.RLock())
        with lock:
            meta=self.metadata(id)
            if meta['state']!='connected' or any(binding.get(k)!=meta.get(k) for k in ('grant_id','revision')) or digest({k:v for k,v in meta.items() if k not in COMPUTED})!=binding.get('digest'):
                raise PolicyError('Connector access changed or needs reconnect.')
            record=self.records.get(binding['grant_id'])
            if record.get('binding')!=binding or digest(record.get('metadata'))!=binding['digest'] or self.base(self.item(id))!=record['metadata']['base_digest']:
                raise PolicyError('Connector credential binding does not match its reviewed destination.')
            values=record['values'];self.remember(id,values)
            for registration in self.store.query("SELECT grant_id FROM connector_registrations WHERE plugin_id=? AND state='registered' AND grant_id IS NOT NULL ORDER BY updated DESC LIMIT 4",(id,)):
                private=self.records.get(registration['grant_id'])
                if private.get('kind')!='registration' or private.get('issuer')!=meta['issuer'] or private.get('resource')!=meta['resource']:
                    continue
                self.remember(id,private.get('values',{}))
            if meta['kind']=='oauth' and values['expires_at']<=time.time()+30:
                try:
                    if not values.get('refresh_token'):raise PolicyError('Connector needs interactive renewal.')
                    response=self.request(meta['endpoints']['token_endpoint'],{'grant_type':'refresh_token','client_id':meta['client_id'],
                        'refresh_token':values['refresh_token'],'resource':meta['resource']},cancel)
                    renewed=self.tokens(response,values['scopes'],values)
                    if renewed['scopes']!=values['scopes']:raise PolicyError('Renewal changed the issued scope set; review access again.')
                    # Private generation rotation preserves the public grant and run digest.
                    self.records.put(binding['grant_id'],{**record,'values':renewed})
                    values=renewed;self.remember(id,values)
                except Exception:
                    self.rejected(id)
                    raise PolicyError('Connector renewal has no verified outcome; reconnect before any tool call.') from None
            if meta['kind']=='environment_route':
                return {'environment':values['environment'],'route':meta['endpoints']['credential_route']}
            if meta['kind']=='environment':return {'environment':values['environment']}
            if meta['resource']!=config.get('url'):raise PolicyError('Connector token destination changed.')
            return {'headers':{'Authorization':'Bearer '+values['access_token']}}
    def rejected(self,id):
        self.store.query("UPDATE connector_grants SET state='needs_reconnect',updated=? WHERE plugin_id=?",(time.time(),id))
    def disconnect(self,id):
        self.boundary();self.idle();item=self.item(id);meta=self.metadata(id);remote=False
        with self.lock:
            owned=bool(self.pending and self.pending['value']['plugin_id']==id or self.registration_pending and self.registration_pending['value']['plugin_id']==id)
        if owned:self.cancel('disconnected')
        self.store.query('UPDATE extensions SET enabled=0 WHERE id=?',(id,));self.app.extensions.close_plugin(id)
        for row in self.store.query('SELECT grant_id FROM connector_registrations WHERE plugin_id=? AND grant_id IS NOT NULL',(id,)):
            self.store.query('INSERT OR IGNORE INTO connector_cleanup VALUES(?,?)',(id,row['grant_id']))
        if meta.get('grant_id'):
            self.store.query("UPDATE connector_grants SET state='revoked',updated=? WHERE plugin_id=?",(time.time(),id))
            self.store.query('UPDATE extensions SET enabled=0 WHERE id=?',(id,));self.app.extensions.close_plugin(id)
            self.store.query('INSERT OR IGNORE INTO connector_cleanup VALUES(?,?)',(id,meta['grant_id']))
            try:
                record=self.records.get(meta['grant_id']);values=record['values'];self.remember(id,values)
                if meta['kind']=='oauth' and meta['endpoints'].get('revocation_endpoint'):
                    self.request(meta['endpoints']['revocation_endpoint'],{'token':values.get('refresh_token',values['access_token']),'client_id':meta['client_id']},revocation=True);remote=True
            except Exception:pass
        if not self.cleanup(id):
            self.store.query("UPDATE connector_grants SET state='revoked',updated=? WHERE plugin_id=?",(time.time(),id))
            return {'state':'revoked','remote_revocation_confirmed':remote,'storage_cleanup_pending':True}
        config={k:v for k,v in item['config'].items() if k not in {'auth_binding','schemas','protocol','catalogue_digest','content_catalogue','content_digest'}}
        self.store.query('UPDATE extensions SET config=?,enabled=0 WHERE id=?',(packed(config),id));self.app.extensions.close_plugin(id)
        self.store.query("UPDATE connector_grants SET state='disconnected',updated=? WHERE plugin_id=?",(time.time(),id))
        return {'state':'disconnected','remote_revocation_confirmed':remote,'storage_cleanup_pending':False}
    def close(self):
        self.cancel('closed')
        with self.lock:self.previews.clear();self.redactions.clear()
