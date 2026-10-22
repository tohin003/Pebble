"""Actual budgeted provider requests, compaction/retry and restart; no paid model."""

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
    issued, abandoned, replayed = [], [], []
    goal = 'Inspect the fixture files, then ask me for scope. Preserve 漢字. Never publish anything.'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            assert all(not any(k.startswith('_pebble_') for k in m) for m in body['messages'])
            assert any(goal == m.get('content') for m in body['messages'])
            compacted = any(m.get('name') == 'pebble_evidence_spine' for m in body['messages'])
            fragment = False
            if compacted and not abandoned:
                abandoned.append(len(requests))
                fragment, delta = True, {'content': 'abandoned-compaction-text'}
            elif compacted:
                replayed.append(len(requests))
                fn = {'name': 'AskUserQuestion', 'arguments': json.dumps({'required': True, 'questions': json.dumps([
                    {'id': 'scope', 'title': 'Which scope should I use?', 'options': []}])})}
                delta = {'tool_calls': [{'index': 0, 'id': 'question', 'type': 'function', 'function': fn}]}
            else:
                if len(issued) >= 8:
                    raise AssertionError('Context never retired within eight bounded file reads')
                name = f'file-{len(issued)}.txt'
                issued.append(name)
                delta = {'tool_calls': [{'index': 0, 'id': 'read', 'type': 'function', 'function': {
                    'name': 'Read', 'arguments': json.dumps({'file_path': name, 'limit': 40})}}]}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
            if not fragment:
                self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-compaction-smoke-') as directory:
            home, root = Path(directory) / 'data', Path(directory) / 'project'
            root.mkdir()
            for number in range(8):
                lines = [f'old-source-{number}-{i}: ' + hashlib.sha256(f'{number}-{i}'.encode()).hexdigest()[:32] for i in range(40)]
                (root / f'file-{number}.txt').write_text('\n'.join(lines))
            service = Service(command, home)
            try:
                service.rpc('setting', key='memory_enabled', value=False)
                service.rpc('model_save', name='Compaction fixture', url=f'http://127.0.0.1:{server.server_port}', model='fixture', context=4096)
                chat = service.rpc('new_chat', workspace=str(root))
                started = service.rpc('send', id=chat['id'], text=goal)
                deadline = time.monotonic() + 20
                while service.rpc('state')['active'] and time.monotonic() < deadline:
                    time.sleep(.05)
                run = service.rpc('run_get', id=chat['id'], run_id=started['run_id'])
                assert run['state'] == 'waiting_input', run
                assert len(run['compactions']) >= 1 and len(abandoned) == len(replayed) == 1
                checks.append('real 4K provider preflight archives older exchanges before dispatch')
                assert run['context']['manifest']['input_tokens']['after'] <= run['context']['manifest']['limits']['input']
                checks.append('post-compaction counted input remains within declared budget')
                assert len([t for t in run['tools'] if t['name'] == 'Read']) == len(issued)
                assert 'abandoned-compaction-text' not in json.dumps(service.rpc('get_chat', id=chat['id']))
                checks.append('failed stream is discarded and committed reads are never repeated')
                archive = service.rpc('run_context_archive', id=chat['id'], run_id=started['run_id'], generation=run['compactions'][0]['generation'])
                assert any('old-source-0' in str(i['message'].get('content')) for i in archive['archive'])
                assert 'old-source' not in json.dumps(archive['manifest']) and goal not in json.dumps(archive['manifest'], ensure_ascii=False)
                checks.append('raw retired source is recoverable only through scoped archive; manifest is content-free')
                events = service.rpc('run_events', id=chat['id'], run_id=started['run_id'], limit=200)
                generations = [e['payload']['generation'] for e in events if e['kind'] == 'context_compacted']
                assert len(generations) == len(set(generations))
                checks.append('transport retry creates no duplicate compaction generation')
                other = service.rpc('new_chat')
                try:
                    service.rpc('run_context_archive', id=other['id'], run_id=started['run_id'], generation=1)
                except RuntimeError as exc:
                    assert 'conversation' in str(exc)
                else:
                    raise AssertionError('Foreign conversation accessed archive')
                checks.append('archive inspection is bound to the owning conversation')
            finally:
                service.close()
            restarted = Service(command, home)
            try:
                assert restarted.rpc('run_context_archive', id=chat['id'], run_id=started['run_id'], generation=1) == archive
                assert restarted.rpc('run_get', id=chat['id'], run_id=started['run_id'])['state'] == 'waiting_input'
                checks.append('archive and suspended run survive restart with personalization disabled')
                restarted.rpc('delete_chat', id=chat['id'])
                assert not restarted.rpc('state')['runs']
                checks.append('deletion removes run context archives')
            finally:
                restarted.close()
    finally:
        server.shutdown()
        server.server_close()
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source', 'passed': True,
        'checks': checks, 'synthetic_provider_attempts': len(requests), 'file_reads': len(issued),
        'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
        'limits': 'Synthetic RPC/SSE/budget/restart evidence; no live model, semantic summarizer, installed desktop or frontier benchmark.'}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
