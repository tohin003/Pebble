"""Synthetic service/packaged-service recovery smoke; no paid API or user data.

Run with the project Python environment. --backend accepts a frozen executable;
the default runs backend/main.py. Report paths are explicitly selected by caller.
"""

import argparse
import hashlib
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Service:
    def __init__(self, command, home):
        self.events, self.lines = [], queue.Queue()
        self.stderr = tempfile.TemporaryFile(mode='w+t')
        self.starting = True
        self.process = subprocess.Popen([*command, '--home', str(home), '--resources', str(ROOT / 'src-tauri/resources')],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True)

        def read():
            try:
                for line in self.process.stdout:
                    self.lines.put(json.loads(line))
            except (ValueError, OSError) as exc:
                self.lines.put({'protocol_error': str(exc)})
            finally:
                self.lines.put(None)

        threading.Thread(target=read, daemon=True).start()

    def rpc(self, method, **params):
        ident = uuid.uuid4().hex
        self.process.stdin.write(json.dumps({'id': ident, 'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        # A signed/frozen binary's first cold launch can be slower than later
        # RPCs. Keep a finite startup bound and retain diagnostics on failure.
        deadline = time.monotonic() + (45 if self.starting else 15)
        while True:
            try:
                item = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                self.stderr.seek(0)
                raise RuntimeError(f'Backend timed out during {method}; stderr: {self.stderr.read()[-3000:]}') from None
            if item is None:
                self.stderr.seek(0)
                raise RuntimeError(f'Backend stopped during {method}; stderr: {self.stderr.read()[-3000:]}')
            if item.get('protocol_error'):
                raise RuntimeError(item['protocol_error'])
            if item.get('event'):
                self.events.append(item)
            elif item.get('id') == ident:
                self.starting = False
                if 'error' in item:
                    raise RuntimeError(item['error'])
                return item['result']

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.stderr.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT / 'backend/main.py')]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            if len(requests) == 1:
                data = {'nodes': [{'id': 'a', 'label': 'Verified fixture artifact'}], 'edges': []}
                call = {'index': 0, 'id': 'fixture-call', 'type': 'function', 'function': {
                    'name': 'UIRender', 'arguments': json.dumps({'title': 'Fixture', 'kind': 'diagram', 'data': json.dumps(data)})}}
                delta = {'tool_calls': [call]}
            else:
                delta = {'content': 'Abandoned partial text' if len(requests) == 2 else 'Completed synthetic recovery.'}
            self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
            if len(requests) != 2:
                self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-runtime-smoke-') as directory:
            service = Service(command, Path(directory))
            try:
                catalogue = service.rpc('command_catalogue')
                assert {'pause', 'resume', 'status'} <= {c['name'] for c in catalogue}
                checks.append('service JSON-RPC command contract')
                profile = service.rpc('model_save', name='Synthetic fixture', url=f'http://127.0.0.1:{server.server_port}', model='fixture')
                chat = service.rpc('new_chat')
                result = service.rpc('send', id=chat['id'], text='Draw a diagram', deliberate=False)
                deadline = time.monotonic() + 10
                while service.rpc('state')['active'] and time.monotonic() < deadline:
                    time.sleep(0.1)
                run = service.rpc('run_get', id=chat['id'], run_id=result['run_id'])
                assert run['state'] == 'completed', run
                transcript = service.rpc('get_chat', id=chat['id'])
                assert len(transcript['artifacts']) == 1 and len(requests) == 3
                assert transcript['messages'][-1]['content'] == 'Completed synthetic recovery.'
                assert transcript['messages'][-1]['usage']['usage_incomplete'] is True
                checks += ['one artifact effect across three model attempts', 'failed stream text absent', 'unknown abandoned-attempt usage is explicit']
                service.rpc('send', id=chat['id'], text='/status')
                assert len(requests) == 3
                checks.append('host status command uses no inference')
                events = service.rpc('run_events', id=chat['id'], run_id=result['run_id'])
                assert any(e['kind'] == 'model_retry' for e in events)
                cursor = events[-1]['sequence']
                assert service.rpc('run_events', id=chat['id'], run_id=result['run_id'], after=cursor) == []
                checks.append('durable event replay cursor')
            finally:
                service.close()
            restarted = Service(command, Path(directory))
            try:
                run = restarted.rpc('run_get', id=chat['id'], run_id=result['run_id'])
                assert run['state'] == 'completed' and len(run['tools']) == 1
                assert len(restarted.rpc('get_chat', id=chat['id'])['artifacts']) == 1
                checks.append('service restart retains settled run and tool receipt')
                restarted.rpc('delete_chat', id=chat['id'])
                assert not restarted.rpc('state')['runs']
                checks.append('chat deletion cascades operational journal')
            finally:
                restarted.close()
    finally:
        server.shutdown()
        server.server_close()
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source',
              'passed': True, 'checks': checks, 'synthetic_provider_attempts': len(requests),
              'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
              'limits': 'Synthetic protocol verification only; no live model, installer, Windows sandbox, signed deployment or frontier evaluation.'}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
