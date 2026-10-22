"""Real RPC/SSE paged model search beyond the old inventory limits."""

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
    requests, found, checks = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            assert all(not any(k.startswith('_pebble_') for k in m) for m in body['messages'])
            cursor = None
            if len(requests) > 1:
                page = json.loads(next(m['content'] for m in reversed(body['messages']) if m['role'] == 'tool'))
                assert page['coverage']['scan_complete'] and page['coverage']['inventoried'] == 1605, page
                assert len(page['untrusted_matches']) == 1
                found.append(page['untrusted_matches'][0]['path'])
                cursor = page['cursor']
            if len(requests) == 1 or cursor:
                delta = {'tool_calls': [{'index': 0, 'id': 'grep', 'type': 'function', 'function': {
                    'name': 'Grep', 'arguments': json.dumps({'pattern': 'rare-search-target', 'limit': 1,
                        **({'cursor': cursor} if cursor else {})})}}]}
            else:
                delta = {'content': 'Found all three fixture matches with scoped paged evidence.'}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
            self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-search-smoke-') as directory:
            home, root = Path(directory) / 'data', Path(directory) / 'project'
            root.mkdir()
            for i in range(1605):
                (root / f'{i:04d}.txt').write_text('rare-search-target\n' if i in (1601, 1602, 1603) else 'ordinary\n')
            service = Service(command, home)
            try:
                service.rpc('model_save', name='Search fixture', url=f'http://127.0.0.1:{server.server_port}', model='fixture', context=4096)
                chat = service.rpc('new_chat', workspace=str(root))
                started = service.rpc('send', id=chat['id'], text='Find every rare-search-target occurrence using paged source search.')
                deadline = time.monotonic() + 20
                while service.rpc('state')['active'] and time.monotonic() < deadline:
                    time.sleep(.05)
                run = service.rpc('run_get', id=chat['id'], run_id=started['run_id'])
                assert run['state'] == 'completed', {'state': run['state'], 'error': run['error'], 'requests': len(requests),
                    'context': run.get('context'), 'compactions': run.get('compactions')}
                assert found == ['1601.txt', '1602.txt', '1603.txt']
                checks.append('actual model tool calls find all matches beyond file 1500')
                assert len(requests) == 4 and len([t for t in run['tools'] if t['name'] == 'Grep']) == 3
                checks.append('cursor pages preserve every result exactly once through the model wire')
                assert not run['sources'] and not service.rpc('get_chat', id=chat['id'])['actions']
                checks.append('search matches neither grant edit witnesses nor run effects')
                stats = service.rpc('search_stats', id=chat['id'])
                assert stats['inventoried'] == 1605 and stats['status_counts']['text'] == 1605
                assert 'rare-search-target' not in json.dumps(stats) and '1601.txt' not in json.dumps(stats)
                checks.append('native cache inspector is content-free and declares resource limits')
                assert run['context']['manifest']['input_tokens']['after'] <= run['context']['manifest']['limits']['input']
                checks.append('real 4K final request remains within counted input budget')
            finally:
                service.close()
            restarted = Service(command, home)
            try:
                restored = restarted.rpc('search_stats', id=chat['id'])
                assert restored['generation'] == stats['generation'] and restored['indexed_bytes'] == stats['indexed_bytes']
                checks.append('incremental cache metadata survives backend restart')
            finally:
                restarted.close()
    finally:
        server.shutdown()
        server.server_close()
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source', 'passed': True,
        'checks': checks, 'eligible_files': 1605, 'synthetic_provider_attempts': len(requests),
        'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
        'limits': 'Synthetic RPC/SSE literal search and restart; not paid-model quality, symbol indexing, installed desktop or frontier comparison.'}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
