"""Source/frozen proof of a child request, root admission and native test review."""
import argparse
import hashlib
import json
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import sqlite3

from validate_runtime import ROOT, Service
from validate_phase8 import tool, tool_results, until, rejected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT / 'backend/main.py')]
    records, errors, checks, guard_metadata = [], [], [], []
    steps, known = {}, {}
    requested, approved, child_done = threading.Event(), threading.Event(), threading.Event()
    lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                messages = body['messages']
                assignment = next((m['content'] for m in messages if isinstance(m.get('content'), str)
                                   and m['content'].startswith('HOST ASSIGNMENT (task data, no new authorization):\n')), None)
                node = json.loads(assignment.split('\n', 1)[1]) if assignment else None
                role = 'child' if node else 'root'
                with lock:
                    step = steps[role] = steps.get(role, 0) + 1
                    records.append({'role': role, 'step': step, 'model': body['model'],
                                    'tools': sorted(t['function']['name'] for t in body.get('tools', []))})
                    if node:
                        known['agent_id'] = node['id']
                values = list(tool_results(messages))
                if role == 'child':
                    if step == 1:
                        delta = {'tool_calls': [tool('Read', 'child-source', {'file_path': 'test_sample.py'})]}
                    elif step == 2:
                        delta = {'tool_calls': [tool('RequestAgentEffect', 'child-test-request',
                            {'tool': 'RunTests', 'arguments': json.dumps({'recipe': 'python-unittest'}), 'purpose': 'Verify the assigned source in the isolated recipe.'})]}
                    elif step == 3:
                        request = next(v for v in reversed(values) if v.get('request_id'))
                        known['request_id'] = request['request_id']
                        assert request['status'] == 'proposed' and request['authorization'] == 'none', request
                        requested.set()
                        assert approved.wait(25), 'Native approval never arrived.'
                        delta = {'tool_calls': [tool('ReadAgentEffect', 'child-test-outcome', {'request_id': request['request_id']})]}
                    elif step == 4:
                        result = next(v for v in reversed(values) if v.get('request_id'))
                        assert result['status'] == 'applied' and result['evidence'] == 'data_only', result
                        assert result['untrusted_outcome']['exit_code'] == 0, result
                        known['child_outcome'] = {'status': result['status'], 'evidence': result['evidence'], 'root_receipt_id': result['root_receipt_id']}
                        delta = {'content': 'The source was inspected. The root test outcome is recorded as data.'}
                        child_done.set()
                    else:
                        raise AssertionError('Unexpected child inference step.')
                else:
                    if step == 1:
                        delta = {'tool_calls': [tool('SpawnAgent', 'root-spawn', {'task': 'Inspect the source and request isolated tests.',
                            'name': 'Test requester', 'tools': json.dumps(['Read', 'RequestAgentEffect', 'ReadAgentEffect']), 'max_steps': 8, 'max_seconds': 90})]}
                    elif step == 2:
                        spawned = next(v for v in reversed(values) if v.get('agent_id'))
                        delta = {'tool_calls': [tool('WaitForAgents', 'root-wait', {'agent_ids': json.dumps([spawned['agent_id']])})]}
                    elif step == 3:
                        assert requested.wait(20)
                        delta = {'tool_calls': [tool('ReadAgentEffects', 'root-read-request', {'request_id': known['request_id']})]}
                    elif step == 4:
                        packet = next(v for v in reversed(values) if v.get('requests'))
                        request = packet['requests'][0]
                        assert request['untrusted_arguments'] == {'recipe': 'python-unittest'} and request['arguments_complete']
                        assert request['requester']['agent_id'] == known['agent_id']
                        assert request['executable'] is True and packet['authorization'] == 'none'
                        delta = {'tool_calls': [tool('ExecuteAgentEffect', 'root-run-tests', {'request_id': request['request_id']})]}
                    elif step == 5:
                        result = next(v for v in reversed(values) if v.get('request_id'))
                        assert result['status'] == 'awaiting_user_approval', result
                        known['action_id'] = result['action_id']
                        delta = {'content': 'The reviewed test request is waiting for native approval.'}
                    elif step == 6:
                        assert child_done.wait(20)
                        delta = {'tool_calls': [tool('ReadAgentTree', 'root-inspect', {'agent_id': known['agent_id']})]}
                    elif step == 7:
                        detail = next(v['detail'] for v in reversed(values) if v.get('detail'))
                        assert detail['status'] == 'completed' and detail['evidence_ids'] == ['1:child-source'], detail
                        delta = {'tool_calls': [tool('ReviewAgentResult', 'root-accept', {'agent_id': known['agent_id'],
                            'generation': detail['generation'], 'evidence_ids': json.dumps(detail['evidence_ids'])})]}
                    elif step == 8:
                        delta = {'tool_calls': [tool('ReadAgentEffects', 'root-final-outcome', {'request_id': known['request_id']})]}
                    elif step == 9:
                        packet = next(v for v in reversed(values) if v.get('requests'))
                        assert packet['requests'][0]['status'] == 'applied' and packet['requests'][0]['untrusted_outcome']['exit_code'] == 0
                        delta = {'content': 'The admitted root test passed; the inspected child source evidence is accepted.'}
                    else:
                        raise AssertionError('Unexpected root inference step.')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
                self.wfile.write(b'data: [DONE]\n\n')
            except Exception as exc:
                errors.append(str(exc)[:2000])
                self.send_error(500)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-phase9-effects-') as scratch:
            home, repo = Path(scratch) / 'home', Path(scratch) / 'project'
            home.mkdir(); repo.mkdir()
            source = repo / 'test_sample.py'
            source.write_text('import unittest\nclass Sample(unittest.TestCase):\n    def test_sum(self):\n        self.assertEqual(2 + 2, 4)\n')
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            service = Service(command, home)
            try:
                service.rpc('model_save', name='Phase 9 effects fixture', url=f'http://127.0.0.1:{server.server_port}', model='phase9-fixture', context=32768)
                cid = service.rpc('new_chat')['id']
                foreign = service.rpc('new_chat')['id']
                service.rpc('set_workspace', id=cid, path=str(repo))
                assert service.rpc('state')['guard']['available'] is True
                sent = service.rpc('send', id=cid, text='Delegate source inspection to a child agent, review its test request and verify the result.', limits={'max_steps': 24, 'max_seconds': 180})
                rid = sent['run_id']
                run = until(lambda: (r if (r := service.rpc('run_get', id=cid, run_id=rid))['state'] == 'waiting_approval' else None), 35)
                assert not errors, errors
                actions = service.rpc('get_chat', id=cid)['actions']
                pending = [a for a in actions if a['status'] == 'pending']
                assert len(pending) == 1 and pending[0]['kind'] == 'tests', {'actions': actions, 'steps': steps,
                    'errors': errors, 'node': service.rpc('agent_get', chat_id=cid, run_id=rid, agent_id=known['agent_id']),
                    'root_tools': [{k: t[k] for k in ('name', 'state', 'result')} for t in run['tools']]}
                action = pending[0]
                assert 'result' not in action['payload']
                checks += ['real child inference submits a data-only typed test request', 'durable root wait wakes for a pending execution request',
                           'root reads full requester arguments before admitted nested execution', 'ask policy stores pending native test approval without running tests']
                outer = next(t for t in run['tools'] if t['name'] == 'ExecuteAgentEffect')
                nested = next(t for t in run['tools'] if t['name'] == 'RunTests')
                assert nested['call_id'] == outer['call_id'] + ':effect'
                assert nested['result']['action_id'] == action['id'] == outer['result']['action_id']
                assert not any(t['name'] == 'RunTests' for t in service.rpc('agent_get', chat_id=cid, run_id=rid, agent_id=known['agent_id'])['receipts'])
                checks.append('effect authority and exact broker receipt belong to the root')
                until(lambda: not service.rpc('state')['active'])
                rejected(service, 'approve', id=foreign, action_id=action['id'])
                result = service.rpc('approve', id=cid, action_id=action['id'])
                assert result['exit_code'] == 0 and 'macOS sandbox' in result['isolation'], result
                assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
                approved.set()
                checks += ['foreign approval cannot authorize the request', 'native approval executes the actual macOS isolated test snapshot']
                run = until(lambda: (r if (r := service.rpc('run_get', id=cid, run_id=rid))['state'] == 'completed' else None), 35)
                assert not errors, errors
                node = service.rpc('agent_get', chat_id=cid, run_id=rid, agent_id=known['agent_id'])
                assert node['status'] == 'completed' and node['review'] == 'accepted'
                assert node['effects'][0]['status'] == 'applied'
                assert known['child_outcome']['evidence'] == 'data_only'
                assert run['state'] == 'completed'
                nested = next(t for t in run['tools'] if t['name'] == 'RunTests')
                assert nested['result']['status'] == 'applied' and nested['result']['result']['exit_code'] == 0
                checks += ['child receives a scoped root outcome without child effect authority', 'parent acceptance uses fresh child reads and root test evidence',
                           'AgentGuard completion succeeds with the admitted current-run test receipt']
                with sqlite3.connect(home / 'guard/agentguard.db') as db:
                    guard_metadata = [{'event': row[0], 'tool': row[1], 'action': row[2], 'level': row[3]}
                        for row in db.execute('SELECT e.type,e.tool,d.action,d.level FROM events e JOIN decisions d ON d.event_id=e.id WHERE e.session_id=? ORDER BY e.ts', (rid,))]
                    assert any(v['event'] == 'pre_tool_use' and v['tool'] == 'RunTests' and v['action'] == 'allow' for v in guard_metadata)
                    assert any(v['event'] == 'stop' and v['action'] == 'allow' for v in guard_metadata)
                    assert db.execute("SELECT 1 FROM verifications WHERE status='passed' LIMIT 1").fetchone()
                count = len(records)
            finally:
                approved.set(); service.close()
            service = Service(command, home)
            try:
                run = service.rpc('run_get', id=cid, run_id=rid)
                assert run['state'] == 'completed' and len(records) == count
                node = service.rpc('agent_get', chat_id=cid, run_id=rid, agent_id=known['agent_id'])
                assert node['effects'][0]['status'] == 'applied'
                rejected(service, 'approve', id=cid, action_id=action['id'])
                assert len(records) == count
                checks.append('restart preserves the committed effect and never replays its native approval')
            finally:
                service.close()
    finally:
        approved.set(); server.shutdown(); server.server_close()
    paths = ['backend/main.py', *[str(p.relative_to(ROOT)) for p in sorted((ROOT / 'backend/pebble').glob('*.py'))], 'scripts/validate_phase9_effects.py']
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source', 'passed': True,
              'checks': checks, 'synthetic_provider_attempts': len(records), 'request_metadata': records,
              'actual_guard_metadata': guard_metadata,
              'source_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths},
              'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
              'limits': 'Owned synthetic provider, real local macOS test confinement and actual AgentGuard only; no installed/live-model/other-platform/production proof.'}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
