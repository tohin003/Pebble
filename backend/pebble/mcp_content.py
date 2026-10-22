"""Reviewed MCP data routes. Server prompts never become conversation roles."""
import hashlib
import json
import re
import time
import uuid
from urllib.parse import urlsplit

from .policy import PolicyError
from .mcp_transport import MCP, MCPUncertain


def packed(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))


def sha(value):return hashlib.sha256(packed(value).encode()).hexdigest()


def identifier(kind,value):
    if not isinstance(value,str) or not 1<=len(value)<=1000 or any(ord(c)<=32 for c in value):raise PolicyError('Invalid MCP data identifier.')
    if kind=='prompt':
        if not re.fullmatch(r'[\w.-]{1,100}',value):raise PolicyError('Invalid MCP prompt name.')
    elif kind=='resource':
        p=urlsplit(value)
        if not p.scheme or p.scheme.lower() in {'javascript','data','vbscript'} or p.username or p.password:raise PolicyError('Use a resource URI without executable schemes or credentials.')
    else:raise PolicyError('Choose resource or prompt.')
    return value


def bounded(value,depth=0,count=None):
    count=count if count is not None else [0];count[0]+=1
    if depth>20 or count[0]>2000:raise PolicyError('MCP content complexity exceeded its limit.')
    if isinstance(value,dict):
        for k,v in value.items():
            if not isinstance(k,str):raise PolicyError('Invalid MCP object key.')
            bounded(v,depth+1,count)
    elif isinstance(value,list):
        for v in value:bounded(v,depth+1,count)
    elif value is not None and not isinstance(value,(str,int,float,bool)):raise PolicyError('Invalid MCP data value.')


def normalize(value,kind='tool',selected=None,modern=False):
    try:
        bounded(value)
        if not isinstance(value,dict):raise PolicyError('Invalid MCP result.')
    except PolicyError:raise MCPUncertain('MCP response exceeds the validation boundary.') from None
    if len(packed(value).encode())>96000:raise MCPUncertain('MCP data response exceeds the archive limit; reconcile its outcome.')
    blocks=[];unsupported=0
    raw=value.get('contents') if kind=='resource' else value.get('messages') if kind=='prompt' else value.get('content')
    if not isinstance(raw,list) or len(raw)>100:raise MCPUncertain('Invalid MCP data response.')
    for block in raw:
        if not isinstance(block,dict):raise MCPUncertain('Invalid MCP data block.')
        role=None
        if kind=='prompt':
            role=block.get('role');block=block.get('content')
            if role not in {'user','assistant'} or not isinstance(block,dict):raise MCPUncertain('Invalid MCP prompt message.')
        if kind=='resource':
            if block.get('uri')!=selected:raise MCPUncertain('MCP resource response changed its reviewed URI.')
            item={'type':'resource','uri':selected,'mime_type':block.get('mimeType','text/plain')}
            if isinstance(block.get('text'),str):item['text']=block['text']
            elif isinstance(block.get('blob'),str):
                import base64
                try:data=base64.b64decode(block['blob'],validate=True)
                except (ValueError,TypeError):raise MCPUncertain('Invalid MCP resource encoding.') from None
                item.update(binary_sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),binary_unavailable=True)
            else:raise MCPUncertain('MCP resource has no valid text or binary body.')
        elif block.get('type')=='text' and isinstance(block.get('text'),str):item={'type':'text','text':block['text']}
        elif block.get('type')=='resource' and isinstance(block.get('resource'),dict):
            resource=block['resource'];identifier('resource',resource.get('uri'))
            item={'type':'embedded_resource','uri':resource['uri'],'text':resource.get('text')}
            if not isinstance(item['text'],str):unsupported+=1;continue
        elif block.get('type') in {'image','audio'} and isinstance(block.get('data'),str):
            import base64
            try:data=base64.b64decode(block['data'],validate=True)
            except (ValueError,TypeError):raise MCPUncertain('Invalid MCP media encoding.') from None
            item={'type':block['type'],'mime_type':block.get('mimeType'),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'binary_unavailable':True}
        elif block.get('type')=='resource_link':
            identifier('resource',block.get('uri'));item={'type':'resource_link','uri':block['uri'],'name':block.get('name'),'fetched':False}
        else:unsupported+=1;continue
        if role:item['untrusted_server_role']=role
        blocks.append(item)
    structured=value.get('structuredContent')
    if not modern and structured is not None and not isinstance(structured,dict):raise MCPUncertain('Legacy structured MCP output must be an object.')
    return {'untrusted_blocks':blocks,'untrusted_structured':structured,'has_structured':'structuredContent' in value,'unsupported_blocks':unsupported,'authorization':'none'}


class Content:
    def __init__(self,extensions):
        self.ext,self.store=extensions,extensions.store
        self.store.query('CREATE TABLE IF NOT EXISTS extension_data_routes(run_id TEXT,plugin_id TEXT,kind TEXT,identifier TEXT,digest TEXT,snapshot TEXT,PRIMARY KEY(run_id,plugin_id,kind,identifier))')
        self.store.query('CREATE TABLE IF NOT EXISTS extension_data_snapshots(id TEXT PRIMARY KEY,run_id TEXT,plugin_id TEXT,kind TEXT,identifier TEXT,digest TEXT,body TEXT,body_hash TEXT,created REAL)')
    def capture(self,response,kind,name,payload,run_id,cancel,modern=False):
        result=normalize(response,kind,name,modern=modern)
        media=getattr(self.ext,'media',None)
        if not media:return result
        raw=response.get('contents' if kind=='resource' else 'messages' if kind=='prompt' else 'content',[])
        snapshots=[]
        for block in raw:
            candidate=block.get('content',{}) if kind=='prompt' else block
            embedded=candidate.get('resource',{}) if candidate.get('type')=='resource' else candidate
            if candidate.get('type') not in {'image','audio'} and not isinstance(embedded.get('blob'),str):continue
            try:
                origin=self.ext.media_origin(payload,run_id,kind,name)
                metadata=media.ingest(candidate,origin,cancel=cancel)
            except Exception:
                raise MCPUncertain('MCP media was received but could not be safely archived; reconcile this effect before retrying.') from None
            # Complete provenance lives in the immutable media archive. Keep
            # the required approval inbox small enough for bounded models.
            snapshots.append({k:metadata[k] for k in ('id','mime_type','rendered','source_bytes','sha256')})
            self.ext.publish_media(metadata)
        if snapshots:result['media_snapshots']=snapshots
        return result
    def digest(self,config):
        route={k:config.get(k) for k in ('transport','url','command','args','read_files','launch_files','protocol','auth_binding')}
        if 'protocol_era' in config:route['protocol_era']=config['protocol_era']
        return sha({'route':route,
            'resources':config.get('resources',[]),'prompts':config.get('prompts',[]),'catalogue':config.get('content_catalogue')})
    def review(self,config,client):
        result={'resource':[],'prompt':[]}
        for kind,key,field in [('resource','resources','uri'),('prompt','prompts','name')]:
            allowed=config.get(key,[])
            if not allowed:continue
            specs=client.catalogue(key);names=[s.get(field) for s in specs]
            if len(set(names))!=len(names):raise PolicyError('Duplicate MCP data identifiers.')
            for name in allowed:
                identifier(kind,name)
                if name not in names:raise PolicyError('An allowlisted MCP data route is missing.')
                spec=specs[names.index(name)];bounded(spec)
                if len(packed(spec).encode())>12000 or not isinstance(spec.get('name'),str) or not isinstance(spec.get('description',''),str):raise PolicyError('MCP data metadata exceeded its review budget.')
                if kind=='prompt':
                    args=spec.get('arguments',[])
                    if not isinstance(args,list) or len(args)>8:raise PolicyError('A prompt supports at most eight arguments.')
                    seen=set()
                    for arg in args:
                        if not isinstance(arg,dict) or not isinstance(arg.get('name'),str) or not re.fullmatch(r'[\w.-]{1,80}',arg['name']) or arg['name'] in seen or type(arg.get('required',False)) is not bool:raise PolicyError('Invalid or duplicate prompt argument.')
                        seen.add(arg['name'])
                result[kind].append(spec)
        return result
    def checked(self,id,digest=None):
        item=self.ext.checked_plugin(id)
        if not item['config'].get('content_digest') or self.digest(item['config'])!=item['config']['content_digest'] or digest is not None and digest!=item['config']['content_digest']:raise PolicyError('MCP data route needs fresh native review.')
        return item
    def search(self,query,limit=4):
        if not isinstance(query,str) or not 1<=len(query)<=200 or type(limit) is not int or not 1<=limit<=8:raise PolicyError('Use a bounded MCP data search.')
        words=set(re.findall(r'\w+',query.casefold()));matches=[]
        for item in self.ext.list():
            if item['kind']!='plugin' or not item['enabled']:continue
            for kind,specs in item['config'].get('content_catalogue',{}).items():
                for spec in specs:
                    score=len(words&set(re.findall(r'\w+',packed(spec).casefold())))
                    if score:matches.append((score,{'plugin_id':item['id'],'kind':kind,'identifier':spec.get('uri') if kind=='resource' else spec['name'],'description':spec.get('description','')[:240],'digest':item['config']['content_digest']}))
        matches.sort(key=lambda v:(-v[0],v[1]['plugin_id'],v[1]['identifier']))
        return {'untrusted_matches':[v[1] for v in matches[:limit]],'omitted':max(0,len(matches)-limit),'authorization':'none'}
    def load(self,id,kind,name,digest,run_id):
        self.ext.boundary();identifier(kind,name)
        if not run_id:raise PolicyError('MCP data loading requires an owning run.')
        item=self.checked(id,digest)
        spec=next((s for s in item['config']['content_catalogue'][kind] if s.get('uri' if kind=='resource' else 'name')==name),None)
        if not spec:raise PolicyError('This MCP data identifier is not allowlisted.')
        rows=self.store.query('SELECT * FROM extension_data_routes WHERE run_id=?',(run_id,))
        if len(rows)>=4 and not any(r['plugin_id']==id and r['kind']==kind and r['identifier']==name for r in rows):raise PolicyError('A run loads at most four MCP data routes.')
        snap={'plugin_id':id,'kind':kind,'identifier':name,'digest':digest,'untrusted_metadata':spec,'authorization':'none'}
        self.store.query('INSERT OR REPLACE INTO extension_data_routes VALUES(?,?,?,?,?,?)',(run_id,id,kind,name,digest,packed(snap)))
        return snap
    def loaded(self,run_id):
        result=[]
        for row in self.store.query('SELECT * FROM extension_data_routes WHERE run_id=? ORDER BY plugin_id,kind,identifier',(run_id,)):
            item=self.checked(row['plugin_id'],row['digest']);snap=json.loads(row['snapshot'])
            spec=next((s for s in item['config']['content_catalogue'][row['kind']] if s.get('uri' if row['kind']=='resource' else 'name')==row['identifier']),None)
            expected={'plugin_id':row['plugin_id'],'kind':row['kind'],'identifier':row['identifier'],'digest':row['digest'],'untrusted_metadata':spec,'authorization':'none'}
            if not spec or snap!=expected:raise PolicyError('Loaded MCP data metadata changed.')
            result.append(snap)
        return result
    def payload(self,id,kind,name,digest,arguments,run_id):
        item=self.checked(id,digest)
        route=next((r for r in self.loaded(run_id) if r['plugin_id']==id and r['kind']==kind and r['identifier']==name),None)
        if not route:raise PolicyError('Load this MCP data route in the current run first.')
        if not isinstance(arguments,str) or len(arguments.encode())>12000:raise PolicyError('Use bounded JSON prompt arguments.')
        try:args=json.loads(arguments)
        except ValueError:raise PolicyError('Invalid MCP data arguments.') from None
        specs=route['untrusted_metadata'].get('arguments',[]) if kind=='prompt' else []
        if not isinstance(args,dict) or set(args)-{a['name'] for a in specs} or {a['name'] for a in specs if a.get('required')} - set(args) or any(not isinstance(v,str) or len(v)>1000 for v in args.values()):raise PolicyError('MCP data arguments do not match the reviewed template.')
        return {'plugin_id':id,'name':item['name'],'tool':kind+':'+name,'data_kind':kind,'identifier':name,'arguments':args,'digest':digest,
            'url':item['config'].get('url'),'transport':item['config'].get('transport','http'),'run_id':run_id,'requires_review':True}
    def call(self,payload,run_id,cancel=None):
        if payload.get('run_id')!=run_id:raise PolicyError('This MCP data route belongs to another run.')
        checked=self.payload(payload['plugin_id'],payload['data_kind'],payload['identifier'],payload['digest'],packed(payload['arguments']),run_id)
        if any(checked.get(k)!=payload.get(k) for k in checked):raise PolicyError('MCP data route changed after approval.')
        item=self.checked(payload['plugin_id'],payload['digest']);client=MCP(config=item['config'],cancel=cancel,auth=getattr(self.ext,'auth',None),plugin_id=item['id'])
        try:
            if self.store.query('SELECT count(*) AS n FROM extension_data_snapshots WHERE run_id=?',(run_id,))[0]['n']>=40:raise PolicyError('This run reached its MCP data snapshot budget.')
            fresh={**item['config'],'content_catalogue':self.review(item['config'],client),'protocol':client.protocol}
            if self.digest(fresh)!=payload['digest']:raise PolicyError('MCP resource/prompt metadata changed before dispatch.')
            kind=payload['data_kind'];params={'uri':payload['identifier']} if kind=='resource' else {'name':payload['identifier'],'arguments':payload['arguments']}
            response=client.request('resources/read' if kind=='resource' else 'prompts/get',params)
            try:result=self.capture(response,kind,payload['identifier'],payload,run_id,cancel,client.modern)
            except (PolicyError,ValueError,TypeError,RecursionError):raise MCPUncertain('MCP returned invalid source data; reconcile its outcome.') from None
            body=packed(result);sid=uuid.uuid4().hex
            self.store.query('INSERT INTO extension_data_snapshots VALUES(?,?,?,?,?,?,?,?,?)',(sid,run_id,item['id'],kind,payload['identifier'],payload['digest'],body,sha(result),time.time()))
            return {'snapshot_id':sid,'plugin_id':item['id'],'tool':payload['tool'],'digest':payload['digest'],'untrusted_plugin_output':body[:500],
                'characters':len(body),'next_offset':500 if len(body)>500 else None,'origin':'reviewed_mcp_data','authorization':'none'}
        finally:
            try:client.close()
            except Exception:pass
    def read(self,run_id,snapshot_id,offset=0,limit=1000):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=4000:raise PolicyError('Use bounded MCP archive paging.')
        rows=self.store.query('SELECT * FROM extension_data_snapshots WHERE run_id=? AND id=?',(run_id,snapshot_id))
        if not rows:raise PolicyError('MCP data snapshot not found in this run.')
        row=rows[0]
        if sha(json.loads(row['body']))!=row['body_hash']:raise PolicyError('MCP data snapshot integrity failed.')
        end=min(len(row['body']),offset+limit)
        return {'untrusted_source':row['body'][offset:end],'origin':{'plugin_id':row['plugin_id'],'kind':row['kind'],'identifier':row['identifier'],'digest':row['digest'],'body_hash':row['body_hash'],'retrieved_at':row['created']},
            'offset':offset,'next_offset':end if end<len(row['body']) else None,'characters':len(row['body']),'freshness':'historical_snapshot','authorization':'none'}
