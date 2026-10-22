"""Real local RPC/SSE learning and isolated tests; no paid API or live web."""

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
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT/'backend/main.py')]
    attempts, requests, checks = Counter(), [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            model = body['model']; attempts[model] += 1; requests.append(body)
            count = attempts[model]
            call = None
            if model == 'memory-source':
                assert 'MemoryPropose' in {t['function']['name'] for t in body['tools']}
                if count == 1:
                    call = ('read', 'Read', {'file_path':'config.txt'})
                elif count == 2:
                    page = json.loads(next(m['content'] for m in reversed(body['messages']) if m['role'] == 'tool'))
                    call = ('learn', 'MemoryPropose', {'kind':'semantic','content':'SQLite is configured here.',
                        'key':'config-note','evidence_ids':json.dumps([page['receipt_id']])})
                else:
                    result = json.loads(next(m['content'] for m in reversed(body['messages']) if m['role'] == 'tool'))
                    assert result['status'] == 'promoted' and result['authorization'] == 'none'
            elif model == 'memory-tests' and count % 2:
                call = ('tests', 'RunTests', {'recipe':'python-unittest'})
            elif model == 'memory-disabled':
                assert 'MemoryPropose' not in {t['function']['name'] for t in body['tools']}
                assert not any(isinstance(m.get('content'),str) and m['content'].startswith('RETRIEVED MEMORY:') for m in body['messages'])
            if call:
                ident, name, data = call
                delta = {'tool_calls':[{'index':0,'id':ident,'type':'function','function':{'name':name,'arguments':json.dumps(data)}}]}
            else:
                delta = {'content':'The recorded evidence is available; future work requires fresh verification.'}
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            self.wfile.write(b'data: '+json.dumps({'choices':[{'delta':delta}]}).encode()+b'\n\n')
            self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()

    def task(service, cid, text):
        result = service.rpc('send',id=cid,text=text)
        deadline = time.monotonic()+45
        while service.rpc('state')['active'] and time.monotonic()<deadline:
            time.sleep(.05)
        run = service.rpc('run_get',id=cid,run_id=result['run_id'])
        assert run['state'] == 'completed', {'state':run['state'],'error':run['error']}
        return run

    try:
        with tempfile.TemporaryDirectory(prefix='pebble-memory-smoke-') as folder:
            home, root = Path(folder)/'data', Path(folder)/'project'; root.mkdir()
            (root/'config.txt').write_text('SQLite is configured here.\n')
            (root/'test_example.py').write_text('import unittest\nclass Example(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(2+2,4)\n')
            service = Service(command,home)
            try:
                service.rpc('model_save',name='Memory fixture',url=f'http://127.0.0.1:{server.server_port}',model='memory-source',context=4096)
                profile = service.rpc('model_profiles')[0]
                chat = service.rpc('new_chat',workspace=str(root)); cid=chat['id']
                run = task(service,cid,'I prefer concise answers. Learn memory from config.txt by reading it.')
                assert len([t for t in run['tools'] if t['name']=='Read']) == 1
                assert len([t for t in run['tools'] if t['name']=='MemoryPropose']) == 1
                semantic = [m for m in service.rpc('memory_list',kind='semantic') if m['origin']=='auto-learned']
                assert len(semantic)==1 and json.loads(semantic[0]['content'])['claim_class']=='source_report'
                checks.append('real 4K model tool proposal promotes an owned attributed file quote without duplicate automatic learning')
                assert service.rpc('memory_list',kind='working') and service.rpc('memory_list',kind='episodic')
                assert any(m['content']=='I prefer concise answers.' and m['scope']==chat['workspace'] for m in service.rpc('memory_list',kind='profile'))
                checks.append('settlement automatically updates working, episodic and scoped human profile layers')
                assert run['context']['manifest']['input_tokens']['after'] <= run['context']['manifest']['limits']['input']
                checks.append('actual learning exchange fits the counted small-model window')
                settings=service.rpc('state')['settings']['permissions']; settings['mode']='auto'
                service.rpc('setting',key='permissions',value=settings)
                service.rpc('model_save',profile_id=profile['id'],name='Memory fixture',url=f'http://127.0.0.1:{server.server_port}',model='memory-tests',context=4096)
                for index in range(2):
                    tested=task(service,cid,'Run the isolated project Python tests and report the recorded result.')
                    receipt=next(t for t in tested['tools'] if t['name']=='RunTests')
                    assert receipt['result']['status']=='applied' and receipt['result']['result']['exit_code']==0
                    if index==0:
                        assert not any(m['origin']=='auto-learned' for m in service.rpc('memory_list',kind='procedural'))
                service.rpc('memory_maintain')
                recipes=[m for m in service.rpc('memory_list',kind='procedural') if m['origin']=='auto-learned']
                assert len(recipes)==1 and json.loads(recipes[0]['content'])['recipe']=='python-unittest'
                checks.append('two distinct runs execute real sandboxed tests before a host recipe is learned')
                service.rpc('memory_delete',id=semantic[0]['id'])
                assert not [m for m in service.rpc('memory_list',kind='semantic') if m['origin']=='auto-learned']
                checks.append('native forgetting removes learned source and its normal inspection record')
                service.rpc('setting',key='memory_enabled',value=False)
                before=service.rpc('memory_candidates',id=cid)
                before_memories={m['id'] for m in service.rpc('memory_list')}
                service.rpc('model_save',profile_id=profile['id'],name='Memory fixture',url=f'http://127.0.0.1:{server.server_port}',model='memory-disabled',context=4096)
                task(service,cid,'I prefer private brief answers. Just acknowledge this request.')
                assert len(service.rpc('memory_candidates',id=cid))==len(before)
                assert {m['id'] for m in service.rpc('memory_list')}==before_memories
                checks.append('an actual memory-disabled model run recalls and collects no personalized memories')
            finally:
                service.close()
            restarted=Service(command,home)
            try:
                assert not restarted.rpc('state')['memory']['enabled']
                assert len(restarted.rpc('memory_candidates',id=cid))==len(before)
                assert not [m for m in restarted.rpc('memory_list',kind='semantic') if m['origin']=='auto-learned']
                checks.append('disabled personalization and forgotten source survive packaged-service restart')
                restarted.rpc('setting',key='memory_enabled',value=True)
                restarted.rpc('memory_maintain')
                assert not [m for m in restarted.rpc('memory_list',kind='semantic') if m['origin']=='auto-learned']
                checks.append('maintenance cannot re-promote a forgotten source after re-enabling memory')
                restarted.rpc('delete_chat',id=cid)
                assert not restarted.rpc('memory_candidates',id=None)
                checks.append('conversation deletion cascades learning candidates and linked records')
            finally:
                restarted.close()
    finally:
        server.shutdown(); server.server_close()
    report={'date':datetime.now(timezone.utc).isoformat(),'kind':'frozen' if args.backend else 'source','passed':True,
        'checks':checks,'synthetic_provider_attempts':len(requests),'real_sandboxed_test_runs':2,
        'backend_sha256':hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
        'limits':'Local synthetic 4K RPC/SSE and real macOS test snapshots. Temporal source learning is covered by typed unit fixtures; no live web, paid-model quality, installed GUI or frontier benchmark.'}
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True); args.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
