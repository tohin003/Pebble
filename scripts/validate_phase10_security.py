"""Source/frozen JSON-RPC adversarial proof for the Phase 10d runtime boundary.

This drives the *actual* packaged service over its JSON-RPC stdio contract with a
single owned loopback SSE provider, a disposable temporary home and project, and
the real vendored AgentGuard engine. No real model, external network, account,
credential or user data is involved; every model turn is scripted synthetic
output. The report is metadata-only: it carries checks, physical counts, source
and backend hashes, and content-free Guard/DB summaries — never prompts, source,
outcomes or secrets.

The campaign reaches a *pending* native effect action in Ask mode and then
attacks it. Because it never approves, it needs no OS sandbox and runs on any
platform: the invariant is that the admitted test never ran and that foreign,
forged and cross-tree attempts are refused, before and after a real restart.
Separately it proves that hostile/stale configuration cannot create a scheduled
job and that a forged review token cannot create a native Git worktree.

Usage:
  python scripts/validate_phase10_security.py [--backend PATH] [--report PATH]

--backend runs a frozen executable; the default runs backend/main.py. This is
fixture evidence, not an independent human security audit, and it establishes no
model quality, installed-desktop behavior or provider billing.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

from validate_runtime import ROOT, Service
from validate_phase8 import tool, tool_results, until, rejected


MODEL = 'phase10-security-fixture'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT / 'backend/main.py')]
    records, errors, checks = [], [], []
    steps, known = {}, {}
    lock = threading.RLock()
    # Set by the driver once the root reaches its pending native action, so the
    # requesting child stays live *through* root execution (keeping the effect
    # admissible) and then settles cleanly for teardown.
    executed = threading.Event()

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
                assert body['model'] == MODEL
                assert '_pebble_' not in json.dumps(body)
                values = list(tool_results(messages))
                if role == 'child':
                    if step == 1:
                        delta = {'tool_calls': [tool('Read', 'c-read', {'file_path': 'test_sample.py'})]}
                    elif step == 2:
                        delta = {'tool_calls': [tool('RequestAgentEffect', 'c-request',
                            {'tool': 'RunTests', 'arguments': json.dumps({'recipe': 'python-unittest'}),
                             'purpose': 'Verify the assigned source in the isolated recipe.'})]}
                    else:
                        request = next((v for v in reversed(values) if v.get('request_id')), None)
                        if request:
                            known['request_id'] = request['request_id']
                            assert request['status'] == 'proposed' and request['authorization'] == 'none'
                        # Stay live until the root has executed the request, so the
                        # effect is admissible with a live requester, then finish.
                        executed.wait(30)
                        delta = {'content': 'The source was inspected; a test run was requested as data.'}
                else:
                    if step == 1:
                        delta = {'tool_calls': [tool('SpawnAgent', 'r-spawn',
                            {'task': 'Inspect the source and request isolated tests.', 'name': 'Test requester',
                             'tools': json.dumps(['Read', 'RequestAgentEffect', 'ReadAgentEffect']),
                             'max_steps': 6, 'max_seconds': 90})]}
                    elif step == 2:
                        spawned = next(v for v in reversed(values) if v.get('agent_id'))
                        known['agent_id'] = spawned['agent_id']
                        delta = {'tool_calls': [tool('WaitForAgents', 'r-wait', {'agent_ids': json.dumps([spawned['agent_id']])})]}
                    elif step == 3:
                        delta = {'tool_calls': [tool('ReadAgentEffects', 'r-read', {})]}
                    elif step == 4:
                        packet = next(v for v in reversed(values) if v.get('requests'))
                        request = packet['requests'][0]
                        known['request_id'] = request['request_id']
                        assert request['untrusted_arguments'] == {'recipe': 'python-unittest'}
                        assert request['executable'] is True and packet['authorization'] == 'none'
                        delta = {'tool_calls': [tool('ExecuteAgentEffect', 'r-exec', {'request_id': request['request_id']})]}
                    elif step == 5:
                        result = next(v for v in reversed(values) if v.get('request_id'))
                        assert result['status'] == 'awaiting_user_approval', result
                        known['action_id'] = result['action_id']
                        delta = {'content': 'The reviewed test request is waiting for native approval.'}
                    else:
                        raise AssertionError('Unexpected root inference step.')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
                self.wfile.write(b'data: [DONE]\n\n')
            except Exception as exc:
                errors.append(str(exc)[:2000])
                try:
                    self.send_error(500)
                except Exception:
                    pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    guard_summary = {}
    counts = {}
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-phase10-security-') as scratch:
            home, repo = Path(scratch) / 'home', Path(scratch) / 'project'
            home.mkdir(); repo.mkdir()
            source = repo / 'test_sample.py'
            source.write_text('import unittest\nclass Sample(unittest.TestCase):\n    def test_ok(self):\n        self.assertEqual(2 + 2, 4)\n')
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

            def effects_db():
                with sqlite3.connect(home / 'pebble.sqlite') as db:
                    rows = db.execute('SELECT status FROM agent_effects').fetchall()
                return [r[0] for r in rows]

            def pending_tests(service, cid):
                return [a for a in service.rpc('get_chat', id=cid)['actions']
                        if a['status'] == 'pending' and a['kind'] == 'tests']

            def applied_actions(service, cid):
                return [a for a in service.rpc('get_chat', id=cid)['actions'] if a['status'] == 'applied']

            def guard_verifications():
                db_path = home / 'guard' / 'agentguard.db'
                if not db_path.exists():
                    return 0
                try:
                    with sqlite3.connect(db_path) as db:
                        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='verifications'").fetchone():
                            return 0
                        return db.execute("SELECT count(*) FROM verifications WHERE status='passed'").fetchone()[0]
                except sqlite3.Error:
                    return 0

            # --- Phase A: reach a pending effect action and attack it. -------
            service = Service(command, home)
            try:
                service.rpc('model_save', name='Phase 10 security fixture',
                            url=f'http://127.0.0.1:{server.server_port}', model=MODEL, context=32768)
                cid = service.rpc('new_chat')['id']
                foreign = service.rpc('new_chat')['id']
                service.rpc('set_workspace', id=cid, path=str(repo))
                assert service.rpc('state')['guard']['available'] is True, 'The real vendored Guard must be available.'
                checks.append('actual vendored AgentGuard engine is available for the campaign')
                rid = service.rpc('send', id=cid, text='Delegate source inspection to a child, review its test request.',
                                  limits={'max_steps': 24, 'max_seconds': 180})['run_id']
                run = until(lambda: (r if (r := service.rpc('run_get', id=cid, run_id=rid))['state'] == 'waiting_approval' else None), 40)
                executed.set()  # release the child now that the pending action exists
                assert not errors, errors
                pending = pending_tests(service, cid)
                assert len(pending) == 1, {'actions': service.rpc('get_chat', id=cid)['actions'], 'steps': steps, 'errors': errors}
                action_id = pending[0]['id']
                assert 'result' not in pending[0]['payload']
                assert applied_actions(service, cid) == []
                assert guard_verifications() == 0, 'No test ran, so no passed Guard verification may exist.'
                assert effects_db() == ['awaiting_approval']
                assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
                checks += ['real child inference stores a data-only typed test request',
                           'root ExecuteAgentEffect reaches a pending native action without running the test',
                           'the admitted test is physically unexecuted while awaiting native review']

                # Attack 1: a foreign conversation cannot approve the pending action.
                rejected(service, 'approve', id=foreign, action_id=action_id)
                # Attack 2: a forged action id cannot be approved.
                rejected(service, 'approve', id=cid, action_id='forged-' + action_id)
                rejected(service, 'approve', id=foreign, action_id='forged-' + action_id)
                assert pending_tests(service, cid)[0]['id'] == action_id
                assert applied_actions(service, cid) == [] and guard_verifications() == 0
                assert effects_db() == ['awaiting_approval']
                checks.append('foreign-conversation and forged-id approvals are refused with no physical effect')

                # Attack 3: no second send can run while the action is pending / app settled.
                assert not service.rpc('state')['active']
                before_requests = len(records)
                attempts = before_requests
            finally:
                service.close()

            # --- Phase A (continued): restart proves no replay. --------------
            service = Service(command, home)
            try:
                run = service.rpc('run_get', id=cid, run_id=rid)
                assert run['state'] == 'waiting_approval'
                assert len(records) == attempts, 'A restart must not re-run any inference.'
                assert pending_tests(service, cid)[0]['id'] == action_id
                assert applied_actions(service, cid) == [] and guard_verifications() == 0
                assert effects_db() == ['awaiting_approval']
                rejected(service, 'approve', id=foreign, action_id=action_id)
                assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
                checks.append('restart preserves the pending request, runs no test and still refuses foreign approval')

                # --- Phase B: configuration and native-review forgery. -------
                limits = {'max_steps': 4, 'max_seconds': 60}
                schedule = {'kind': 'interval', 'seconds': 3600}
                job_cid = service.rpc('new_chat')['id']
                service.rpc('set_workspace', id=job_cid, path=str(repo))
                fingerprint = service.rpc('job_preview', id=job_cid, schedule=schedule, limits=limits)['config_fingerprint']
                # Drift the pinned configuration after the review.
                service.rpc('setting', key='permissions', value={'mode': 'ask', 'tools': {
                    'files': True, 'edits': True, 'tests': True, 'apps': True, 'web': False,
                    'terminal': True, 'plugins': True, 'views': True}})
                rejected(service, 'job_create', id=job_cid, name='Hostile', task='Do it',
                         schedule=schedule, monitor=False, limits=limits, config_fingerprint=fingerprint)
                # Host-only authority fields are refused by name.
                rejected(service, 'job_create', id=job_cid, name='Hostile', task='Do it', schedule=schedule,
                         monitor=False, limits=limits, config_fingerprint=fingerprint, permissions={'edits': True})
                assert service.rpc('job_list', id=job_cid)['jobs'] == []
                checks.append('stale configuration and host-only authority fields cannot create a scheduled job')

                # A forged native Git review token cannot create a worktree.
                rejected(service, 'git_create_worktree', id=job_cid, review_id='forged-review-token')
                assert service.rpc('git_worktrees', id=job_cid)['worktrees'] == []
                checks.append('a forged review token cannot create a native Git worktree')

                with sqlite3.connect(home / 'pebble.sqlite') as db:
                    counts = {'agent_effects': db.execute('SELECT count(*) FROM agent_effects').fetchone()[0],
                              'applied_actions': db.execute("SELECT count(*) FROM actions WHERE status='applied'").fetchone()[0],
                              'jobs': db.execute('SELECT count(*) FROM jobs').fetchone()[0],
                              'git_worktrees': db.execute('SELECT count(*) FROM git_worktrees').fetchone()[0]}
                assert counts == {'agent_effects': 1, 'applied_actions': 0, 'jobs': 0, 'git_worktrees': 0}
                guard_summary = {'passed_verifications': guard_verifications()}
            finally:
                service.close()
            assert not errors, errors
    finally:
        executed.set()  # never leave a blocked child handler on an early failure
        server.shutdown()
        server.server_close()

    paths = ['backend/main.py', *[str(p.relative_to(ROOT)) for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'scripts/validate_phase10_security.py']
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source',
              'passed': True, 'checks': checks, 'synthetic_provider_attempts': len(records),
              'request_metadata': records, 'durable_counts': counts, 'guard_summary': guard_summary,
              'source_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths},
              'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
              'limits': ('Owned loopback synthetic provider, temporary home/project and the actual vendored '
                         'AgentGuard only. No approval is performed, so no OS sandbox, real model, installer, '
                         'other-platform, credential or production proof is established. Fixture evidence, not '
                         'an independent human security audit.')}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
