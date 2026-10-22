"""Actual source/frozen RPC/SSE skill disclosure and confined hook lifecycle."""
import argparse
import hashlib
import json
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT, Service


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--backend',type=Path);parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    command=[str(args.backend.resolve())] if args.backend else [sys.executable,str(ROOT/'backend/main.py')]
    attempts, requests, checks=Counter(),[],[]
    expected='---\nname: checker\ndescription: Python lifecycle validation\nrequired-references: [references/check.md]\n---\n'+'Whole guidance. '*400+'FINAL_INSTRUCTION_SENTINEL'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*unused):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(body)
            model=body['model'];attempts[model]+=1;count=attempts[model];call=None
            if model=='skill-explicit':
                skill=next(m for m in body['messages'] if m.get('name','').startswith('pebble_skill_'))
                assert expected in json.loads(skill['content'].split('\n',1)[1])['untrusted_skill']
                assert 'Required source instructions.' in skill['content']
            elif model=='skill-discover':
                if count==1:
                    metadata=next(m['content'] for m in body['messages'] if str(m.get('content','')).startswith('Enabled skill metadata'))
                    menu=json.loads(metadata.split('\n',1)[1]);assert 'FINAL_INSTRUCTION_SENTINEL' not in metadata
                    calls=[('load','LoadSkill',{'id':menu[0]['id'],'digest':menu[0]['digest']}),('unseen','AskUserQuestion',{'questions':json.dumps([{'id':'skip','title':'Should not be created?'}]),'required':True})]
                    call=calls
                elif count==2:
                    outputs=[json.loads(m['content']) for m in body['messages'] if m['role']=='tool']
                    assert outputs[-1]['status']=='not_executed'
                    assert any('FINAL_INSTRUCTION_SENTINEL' in str(m.get('content','')) for m in body['messages'])
                    loaded=next(o for o in outputs if o.get('load_kind')=='instructions')
                    call=[('ref','LoadSkill',{'id':loaded['id'],'digest':loaded['digest'],'reference':'references/optional.md','offset':0})]
                else:
                    page=json.loads(next(m['content'] for m in reversed(body['messages']) if m['role']=='tool'))
                    assert page.get('load_kind')=='reference' and page.get('next_offset')==3000, page
            elif model=='hook-tool' and count%2:
                call=[('blocked','AskUserQuestion',{'questions':json.dumps([{'id':'input','title':'Unadmitted question?'}]),'required':False})]
            elif model=='hook-tool':
                result=json.loads(next(m['content'] for m in reversed(body['messages']) if m['role']=='tool'));assert 'Blocked by hook' in result['error']
            elif model=='skill-small':
                if count==1:
                    selected=json.loads(next(m['content'] for m in body['messages'] if m.get('name','').startswith('pebble_skill_')).split('\n',1)[1])
                    call=[('small-ref','LoadSkill',{'id':selected['id'],'digest':selected['digest'],'reference':'references/brief.md','offset':0,'limit':40})]
                else:
                    page=json.loads(next(m['content'] for m in reversed(body['messages']) if m['role']=='tool'))
                    assert len(page['untrusted_source'])==40 and page['next_offset']==40
            if call:
                delta={'tool_calls':[{'index':index,'id':ident,'type':'function','function':{'name':name,'arguments':json.dumps(data)}} for index,(ident,name,data) in enumerate(call)]}
            else:delta={'content':'The host evidence is recorded; no unverified action is claimed.'}
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            self.wfile.write(b'data: '+json.dumps({'choices':[{'delta':delta}]}).encode()+b'\n\ndata: [DONE]\n\n')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    def task(service,cid,text,state='completed'):
        result=service.rpc('send',id=cid,text=text)
        deadline=time.monotonic()+45
        while service.rpc('state')['active'] and time.monotonic()<deadline:time.sleep(.05)
        run=service.rpc('run_get',id=cid,run_id=result['run_id'])
        assert run['state']==state,{'state':run['state'],'error':run['error']}
        return run
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-extension-smoke-') as folder:
            folder=Path(folder).resolve();home=folder/'data';bundle=folder/'bundle';bundle.mkdir();(bundle/'references').mkdir()
            (bundle/'SKILL.md').write_text(expected);(bundle/'references/check.md').write_text('Required source instructions.')
            (bundle/'references/optional.md').write_text('Optional source detail. '*200)
            service=Service(command,home)
            try:
                ident=service.rpc('extension_import',path=str(bundle/'SKILL.md'))
                assert not service.rpc('extensions')[0]['enabled']
                service.rpc('extension_toggle',id=ident,enabled=True)
                digest=service.rpc('extensions')[0]['config']['bundle_digest']
                chat=service.rpc('new_chat');cid=chat['id']
                service.rpc('send',id=cid,text='/skills');assert not service.rpc('run_list',id=cid)
                checks.append('disabled installation and host-only metadata discovery preserve zero inference')
                inspected=service.rpc('skill_inspect',id=ident,digest=digest)
                assert inspected['untrusted_skill']==expected and inspected['authorization']=='none'
                checks.append('complete instructions and declared required references survive snapshot import')
                service.rpc('model_save',name='Extension fixture',url=f'http://127.0.0.1:{server.server_port}',model='skill-explicit',context=8192)
                profile=service.rpc('model_profiles')[0]
                task(service,cid,'/skill checker Explain the pinned instructions.')
                checks.append('explicit skill invocation sends complete protected guidance in the actual provider request')
                service.rpc('model_save',profile_id=profile['id'],name='Extension fixture',url=f'http://127.0.0.1:{server.server_port}',model='skill-discover',context=8192)
                task(service,cid,'Use the Python lifecycle validation skill and inspect its optional reference.')
                assert not service.rpc('get_chat',id=cid)['questions']
                checks.append('metadata-first LoadSkill re-plans siblings and loads hash-pinned optional reference pages')
                try:service.rpc('skill_inspect',id=ident,digest='0'*64)
                except RuntimeError:pass
                else:raise AssertionError('Stale digest admitted')
                checks.append('stale skill digests fail before disclosure')
                small=folder/'small';small.mkdir();(small/'references').mkdir()
                (small/'SKILL.md').write_text('---\nname: slim\ndescription: Small reference review\n---\nInspect the optional reference.')
                (small/'references/brief.md').write_text('Unicode Δ detail. '*100)
                small_id=service.rpc('extension_import',path=str(small/'SKILL.md'));service.rpc('extension_toggle',id=small_id,enabled=True)
                service.rpc('model_save',profile_id=profile['id'],name='Extension fixture',url=f'http://127.0.0.1:{server.server_port}',model='skill-small',context=4096)
                small_run=task(service,cid,'/skill slim Inspect its optional reference.')
                assert small_run['context']['manifest']['input_tokens']['after'] <= small_run['context']['manifest']['limits']['input']
                checks.append('a counted 4K request retains complete short guidance and a narrowed Unicode reference page')
                def add(event,source,timeout=2000):
                    hid=service.rpc('extension_add',kind='hook',name='Fixture decision',content=json.dumps({'version':1,'event':event,'worker':{'language':'python','source':source,'timeout_ms':timeout}}))
                    service.rpc('extension_toggle',id=hid,enabled=True);return hid
                sentinel=folder/'private.txt';sentinel.write_text('Disposable private sentinel')
                worker_source=f'''def handle(event):
    import os, socket
    assert event['origin']=='host' and event['authorization']=='none'
    assert set(event['data']) <= {{'tool','arguments_hash'}}
    for action in [lambda: open({str(sentinel)!r}).read(), lambda: open({str(folder/'outside.txt')!r}, 'w'), lambda: socket.create_connection(('127.0.0.1',{server.server_port})), os.fork]:
        try:
            action()
            raise AssertionError('Isolation escaped')
        except OSError:
            pass
    return {{'decision':'deny'}}
'''
                denial=add('before_tool',worker_source)
                service.rpc('model_save',profile_id=profile['id'],name='Extension fixture',url=f'http://127.0.0.1:{server.server_port}',model='hook-tool',context=4096)
                denied=task(service,cid,'Ask a follow-up question through the native question tool.')
                audit=service.rpc('hook_events',id=cid,run_id=denied['id'])
                assert audit[0]['decision']=='deny' and audit[0]['status']=='evaluated'
                assert not service.rpc('get_chat',id=cid)['questions']
                assert not (folder/'outside.txt').exists()
                checks.append('real worker denies outside reads/writes, live loopback network and child fork before native input admission')
                service.rpc('extension_toggle',id=denial,enabled=False)
                timeout=add('before_tool','def handle(event):\n    while True: pass',100)
                timed=task(service,cid,'Ask a follow-up question through the native question tool.')
                assert service.rpc('hook_events',id=cid,run_id=timed['id'])[0]['status']=='worker_failed_closed'
                checks.append('worker timeout fails closed before tool effects')
                service.rpc('extension_toggle',id=timeout,enabled=False)
                start=add('run_start',"def handle(event):\n    return {'decision':'deny'}")
                before=len(requests);task(service,cid,'What is the weather today in Paris?',state='failed');assert len(requests)==before
                checks.append('run-start policy blocks deterministic and model routes before external calls')
                service.rpc('extension_toggle',id=start,enabled=False)
                end=add('task_end',"def handle(event):\n    return {'decision':'deny'}")
                service.rpc('model_save',profile_id=profile['id'],name='Extension fixture',url=f'http://127.0.0.1:{server.server_port}',model='completion',context=4096)
                task(service,cid,'Explain the current task state.',state='verification_required')
                checks.append('completion hook cannot be bypassed by a model success claim')
                service.rpc('extension_toggle',id=end,enabled=False)
                (bundle/'SKILL.md').unlink();service.close();service=Service(command,home)
                assert service.rpc('skill_inspect',id=ident,digest=digest)['untrusted_skill']==expected
                assert service.rpc('hook_events',id=cid,run_id=timed['id'])[0]['status']=='worker_failed_closed'
                checks.append('pinned instructions and hook audit survive restart independently of original files')
            finally:service.close()
    finally:server.shutdown()
    report={'date':datetime.now(timezone.utc).isoformat(),'kind':'frozen' if args.backend else 'source','passed':True,'checks':checks,
        'synthetic_provider_attempts':sum(attempts.values()),'backend_sha256':hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
        'limits':'Actual local RPC/SSE and macOS isolated Python hook processes. No paid-model quality, installed UI, other-platform isolation or production deployment.'}
    if args.report:args.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
