"""Real JSON-RPC/SSE clarification, plan and restart fixture; no paid provider."""

import argparse
import hashlib
import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT, Service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT / 'backend/main.py')]
    requests, checks = [], []

    def tool(name, ident, arguments, index=0):
        return {'index': index, 'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            assert not any(k.startswith('_pebble_') for m in requests[-1]['messages'] for k in m)
            step = {'id': 'draw', 'title': 'Draw the scoped diagram', 'status': 'pending'}
            if len(requests) == 1:
                delta = {'tool_calls': [tool('UpdatePlan', 'plan', {'steps': json.dumps([step])}),
                    tool('AskUserQuestion', 'question', {'required': True, 'questions': json.dumps([
                        {'id': 'scope', 'title': 'Which scope should I use?', 'options': [{'id': 'project', 'label': 'This project'}]}])}, 1)]}
            elif len(requests) == 2:
                delta = {'tool_calls': [tool('UIRender', 'diagram', {'title': 'Scoped fixture', 'kind': 'diagram', 'data': json.dumps({'nodes': [{'id': 'a', 'label': 'This project'}], 'edges': []})})]}
            elif len(requests) == 3:
                delta = {'tool_calls': [tool('UpdatePlan', 'complete-plan', {'steps': json.dumps([{**step, 'status': 'completed', 'evidence_ids': ['2:diagram']}])})]}
            else:
                delta = {'content': 'Finished the scoped fixture with committed evidence.'}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
            self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-interaction-smoke-') as directory:
            service = Service(command, Path(directory))
            try:
                service.rpc('model_save', name='Interactive fixture', url=f'http://127.0.0.1:{server.server_port}', model='fixture')
                chat = service.rpc('new_chat')
                result = service.rpc('send', id=chat['id'], text='Draw a diagram after choosing its scope')
                deadline = time.monotonic() + 5
                while service.rpc('state')['active'] and time.monotonic() < deadline:
                    time.sleep(.05)
                run = service.rpc('run_get', id=chat['id'], run_id=result['run_id'])
                assert run['state'] == 'waiting_input' and len(requests) == 1, run
                assert run['context']['step'] == 1 and run['context']['manifest']['dispatchable']
                assert 'Which scope should I use?' not in json.dumps(run['context'])
                checks.append('pre-dispatch context manifest persists without prompt text')
                question = run['questions'][0]
                assert run['plan']['revision'] == 1 and not service.rpc('get_chat', id=chat['id'])['artifacts']
                checks.append('required form suspends model work and retains plan')
            finally:
                service.close()
            restarted = Service(command, Path(directory))
            try:
                restored = restarted.rpc('get_chat', id=chat['id'])
                assert restored['questions'][0]['id'] == question['id'] and restored['plan']['revision'] == 1
                assert restarted.rpc('run_get', id=chat['id'], run_id=result['run_id'])['context']['step'] == 1
                checks.append('pending question and plan restore after process restart')
                try:
                    restarted.rpc('question_answer', id=chat['id'], question_id=question['id'], revision=2, answers={'scope': {'option_id': 'project'}})
                except RuntimeError as exc:
                    assert 'stale' in str(exc)
                else:
                    raise AssertionError('Stale question revision was accepted')
                checks.append('stale answer rejected without inference')
                answer = {'id': chat['id'], 'question_id': question['id'], 'revision': 1, 'answers': {'scope': {'option_id': 'project'}}}
                restarted.rpc('question_answer', **answer)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    run = restarted.rpc('run_get', id=chat['id'], run_id=result['run_id'])
                    if run['state'] == 'completed':
                        break
                    time.sleep(.05)
                assert run['state'] == 'completed' and len(requests) == 4, run
                assert len(restarted.rpc('get_chat', id=chat['id'])['artifacts']) == 1
                assert run['plan']['revision'] == 2 and run['plan']['steps'][0]['evidence_ids'] == ['2:diagram']
                manifest = run['context']['manifest']
                assert run['context']['step'] == 4
                assert {'policy', 'goal', 'inbox', 'plan', 'tool_exchange'} <= {u['category'] for u in manifest['units'] if u['selected']}
                assert manifest['input_tokens']['after'] <= manifest['limits']['input']
                assert manifest['estimation']['window_method'] == 'o200k_local_estimate'
                checks.append('final request retains human inbox, plan and whole exchanges within budget')
                assert any('human-origin' in str(m.get('content')) and 'question_answer' in str(m.get('content')) for m in requests[1]['messages'])
                checks += ['answer resumes same run automatically', 'human origin retained in model context', 'one artifact and evidence-linked completed plan']
                restarted.rpc('question_answer', **answer)
                assert len(requests) == 4
                checks.append('answer replay creates no extra model request')
                restarted.rpc('delete_chat', id=chat['id'])
                assert not restarted.rpc('state')['runs']
                checks.append('deletion removes interactive run state')
            finally:
                restarted.close()
    finally:
        server.shutdown()
        server.server_close()
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source', 'passed': True,
        'checks': checks, 'synthetic_provider_attempts': len(requests),
        'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
        'limits': 'Synthetic service/SSE verification; not installed desktop, live model, Windows confinement, signed deployment or frontier evaluation.'}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
