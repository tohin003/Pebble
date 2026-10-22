"""Run-owned source witnesses and handoff through real RPC/SSE and restart."""

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
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            assert not any(k.startswith('_pebble_') for m in body['messages'] for k in m)
            if len(requests) == 1:
                delta = {'tool_calls': [tool('Read', 'read', {'file_path': 'fixture.txt'})]}
            else:
                assert len(requests) == 2
                assert any(m.get('name') == 'pebble_source_status' and 'current' in m['content'] for m in body['messages'])
                delta = {'tool_calls': [
                    tool('Write', 'write', {'file_path': 'fixture.txt', 'content': 'staged draft 漢字'}),
                    tool('AskUserQuestion', 'question', {'required': True, 'questions': json.dumps([
                        {'id': 'scope', 'title': 'Which scope should I use?', 'options': [{'id': 'project', 'label': 'This project'}]}])}, 1)]}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
            self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-handoff-smoke-') as directory:
            home, project = Path(directory) / 'data', Path(directory) / 'project'
            project.mkdir()
            target = project / 'fixture.txt'
            target.write_text('baseline 漢字')
            goal = 'Read fixture.txt and draft an update; clarify its scope. Preserve 漢字.'
            service = Service(command, home)
            try:
                service.rpc('setting', key='memory_enabled', value=False)
                service.rpc('model_save', name='Handoff fixture', url=f'http://127.0.0.1:{server.server_port}', model='fixture')
                chat = service.rpc('new_chat', workspace=str(project))
                result = service.rpc('send', id=chat['id'], text=goal)
                deadline = time.monotonic() + 10
                while service.rpc('state')['active'] and time.monotonic() < deadline:
                    time.sleep(.05)
                run = service.rpc('run_get', id=chat['id'], run_id=result['run_id'])
                assert run['state'] == 'waiting_input' and len(requests) == 2, run
                assert run['handoff'] and run['handoff']['manifest']['status'] == 'complete', run
                assert {s['basis'] for s in run['sources']} == {'draft', 'disk'}
                checks.append('automatic settled handoff records current disk and unapplied draft views')
                packet = service.rpc('run_handoff', id=chat['id'], run_id=result['run_id'], refresh=False)
                assert packet['data']['goal']['text'] == goal
                assert packet['data']['authority']['authorization'] == 'none'
                assert len(packet['data']['unresolved']['effects']) == 1
                assert len(packet['data']['unresolved']['waiting_questions']) == 1
                assert 'staged draft' not in json.dumps(packet)
                assert goal not in json.dumps(packet['manifest'], ensure_ascii=False)
                checks.append('exact human goal and unresolved records survive without raw file/tool bodies')
                assert target.read_text() == 'baseline 漢字'
                checks.append('handoff inspection neither applies nor replays pending edits')
                question = run['questions'][0]
            finally:
                service.close()
            restarted = Service(command, home)
            try:
                assert restarted.rpc('run_handoff', id=chat['id'], run_id=result['run_id'], refresh=False) == packet
                assert len(requests) == 2
                checks.append('packet and provenance survive service restart with personalized memory disabled')
                target.write_text('external replacement')
                refreshed = restarted.rpc('run_handoff', id=chat['id'], run_id=result['run_id'])
                assert refreshed['manifest']['counts']['sources']['stale'] == 2
                assert all(s['status'] == 'changed' for s in refreshed['data']['sources'])
                checks.append('refresh invalidates both old disk witness and its staged baseline')
                other = restarted.rpc('new_chat')
                try:
                    restarted.rpc('run_handoff', id=other['id'], run_id=result['run_id'])
                except RuntimeError as exc:
                    assert 'conversation' in str(exc)
                else:
                    raise AssertionError('Foreign chat inspected a run packet')
                checks.append('native handoff inspection rejects a foreign conversation')
                for text in ['/context ' + result['run_id'], '/handoff ' + result['run_id']]:
                    restarted.rpc('send', id=chat['id'], text=text)
                assert len(requests) == 2 and target.read_text() == 'external replacement'
                checks.append('host context and handoff commands require no inference or external effect')
                restarted.rpc('question_cancel', id=chat['id'], question_id=question['id'], revision=1)
                cancelled = restarted.rpc('run_handoff', id=chat['id'], run_id=result['run_id'])
                human = [i for i in cancelled['data']['inputs'] if i['origin'] == 'human']
                assert human[0]['payload'] == {'question_id': question['id'], 'required': True}
                assert human[0]['kind'] == 'question_cancelled'
                checks.append('required cancellation retains exact human origin without implicit approval')
                restarted.rpc('delete_chat', id=chat['id'])
                try:
                    restarted.rpc('run_handoff', id=chat['id'], run_id=result['run_id'], refresh=False)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('Deleted run packet remained accessible')
                checks.append('chat deletion removes packet and source state')
            finally:
                restarted.close()
    finally:
        server.shutdown()
        server.server_close()
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source', 'passed': True,
        'checks': checks, 'synthetic_provider_attempts': len(requests),
        'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
        'limits': 'Synthetic RPC/SSE/restart checks; not semantic compaction, paid models, installed desktop, full memory, Pebble children or frontier evaluation.'}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
