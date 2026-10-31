"""Source/frozen proof: managed worktree export -> root broker -> native approval -> tests.

A temporary home and Git repository, an owned loopback SSE provider and the
actual vendored AgentGuard only. Fixture Git commands are author commands with
hooks disabled inside the temporary folder; no installed service, user
repository, live account or external destination is touched.
"""
import argparse
import contextlib
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT, Service
from validate_phase8 import tool, tool_results, until, rejected


GOAL = 'Integrate a managed worktree file and verify it with isolated tests.'
BASE_CALC = 'def answer():\n    return 41\n'
NEW_CALC = 'def answer():\n    return 42\n'
TEST_CALC = ('import unittest\n\nfrom calc import answer\n\n\nclass AnswerTest(unittest.TestCase):\n'
             '    def test_answer(self):\n        self.assertEqual(answer(), 42)\n')
BASE_HUMAN = 'Committed human notes.\n'
DIRTY_HUMAN = 'Uncommitted human work that must stay untouched.\n'
WORKTREE_TOOLS = {'ReadGitWorktrees', 'PreviewWorktreeExport', 'ReadWorktreeExport', 'IntegrateWorktreeFile'}
OFFERED = WORKTREE_TOOLS | {'ReadGitStatus', 'RunTests'}
STEPS = 7  # Phase 46 (D154): the run waits at the staged RunTests (runner.STAGED_STOP); no held turn


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def bounded(exc, limit=400):
    return (type(exc).__name__ + ': ' + str(exc))[:limit]


def readonly(path):
    return contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT / 'backend/main.py')]
    records, errors, checks, guard_metadata, verification_metadata = [], [], [], [], []
    steps, known = {'root': 0}, {}
    edit_approved, tests_approved = threading.Event(), threading.Event()
    lock = threading.RLock()

    def latest(values, key):
        return next((v for v in reversed(values) if key in v), None)

    def script(step, values, names):
        """The synthetic root. Assertions carry metadata only, never prompts or source."""
        if step == 1:
            assert OFFERED <= set(names), {'missing_offered': sorted(OFFERED - set(names))}
            assert not {n for n in names if 'Worktree' in n} - WORKTREE_TOOLS, {'unexpected': sorted(names)}
            return {'tool_calls': [tool('ReadGitStatus', 'root-status', {})]}
        if step == 2:
            status = latest(values, 'head_commit')
            assert status and status['head_commit'] == known['base'] and status['authorization'] == 'none', 'status metadata'
            assert status['clean'] is False and 'human.txt' in {e['path'] for e in status['entries']}, 'dirty human file not reported'
            return {'tool_calls': [tool('PreviewWorktreeExport', 'root-preview',
                                        {'worktree_id': known['worktree_id'], 'file_paths': json.dumps(['calc.py'])})]}
        if step == 3:
            manifest = latest(values, 'proposals')
            assert manifest and manifest['content_included'] is False and manifest['authorization'] == 'none', 'manifest metadata'
            [entry] = manifest['proposals']
            assert entry['path'] == 'calc.py' and entry['operation'] == 'replace' and 'content' not in entry, sorted(entry)
            assert entry['base_hash'] == sha(BASE_CALC) and entry['content_hash'] == sha(NEW_CALC), 'manifest hashes'
            known['export_id'] = manifest['export_id']
            return {'tool_calls': [tool('ReadWorktreeExport', 'root-read-export',
                                        {'export_id': known['export_id'], 'file_path': 'calc.py'})]}
        if step == 4:
            page = latest(values, 'untrusted_content')
            assert page and page['untrusted_content'] == NEW_CALC and page['next_offset'] is None, 'export page'
            assert page['content_hash'] == sha(NEW_CALC) and page['base_hash'] == sha(BASE_CALC), 'page hashes'
            assert page['applied'] is False and page['authorization'] == 'none', 'page authority'
            return {'tool_calls': [tool('IntegrateWorktreeFile', 'root-integrate',
                                        {'export_id': known['export_id'], 'file_path': 'calc.py'})]}
        if step == 5:
            staged = latest(values, 'write_receipt_id')
            last = values[-1] if values else {}
            # Host refusals are bounded policy messages, never source or prompt text.
            assert staged, {'last_result_keys': sorted(last), 'host_error': str(last.get('error', ''))[:300]}
            assert staged['status'] == 'awaiting_user_approval' and staged['authorization'] == 'none', staged.get('status')
            known['integration'] = {k: staged[k] for k in ('receipt_id', 'read_receipt_id', 'write_receipt_id',
                                                            'inspection_receipt_id', 'export_receipt_id', 'action_id')}
            return {'content': 'The managed worktree file is staged and waits for native review.'}
        if step == 6:
            assert edit_approved.wait(25), 'native edit approval never arrived'
            return {'tool_calls': [tool('RunTests', 'root-tests', {'recipe': 'python-unittest'})]}
        if step == 7:
            # Phase 46 (D154): the run waited at the staged RunTests, so this request follows the native test approval
            # (the test proposal's pending state is checked from the run's journal before it, in main)
            assert tests_approved.wait(25), 'native test approval never arrived'
            return {'content': 'Integrated calc.py from the managed worktree; the isolated unittest run passed.'}
        raise AssertionError(f'Unexpected root inference step {step}.')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                messages = body['messages']
                names = sorted(t['function']['name'] for t in body.get('tools', []))
                child = any(isinstance(m.get('content'), str) and m['content'].startswith('HOST ASSIGNMENT') for m in messages)
                with lock:
                    step = steps['root'] = steps['root'] + 1
                    record = {'role': 'child' if child else 'root', 'step': step, 'model': body.get('model'), 'tools': names}
                    records.append(record)
                assert not child, 'The fixture spawns no child agents.'
                delta = script(step, list(tool_results(messages)), names)
                with lock:
                    record['emitted'] = delta['tool_calls'][0]['function']['name'] if 'tool_calls' in delta else 'content'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
                self.wfile.write(b'data: [DONE]\n\n')
            except Exception as exc:
                with lock:
                    errors.append(bounded(exc))
                self.send_error(500)

    def diagnostics(label, run, chat):
        """Root metadata only: no prompts, source bodies or tool results."""
        with lock:
            return {'waiting_for': label, 'state': run and run.get('state'), 'run_error': run and (run.get('error') or '')[:300],
                    'provider_steps': steps['root'], 'provider_errors': errors[-5:],
                    'tools': [[t['call_id'], t['name'], t['state']] for t in (run or {}).get('tools', [])][-20:],
                    'actions': [[a['kind'], a['status']] for a in (chat or {}).get('actions', [])]}

    def settle(service, cid, rid, label, predicate, seconds=30):
        seen = {}

        def probe():
            seen['run'] = run = service.rpc('run_get', id=cid, run_id=rid)
            seen['chat'] = chat = service.rpc('get_chat', id=cid)
            return (run, chat) if predicate(run, chat) else None
        try:
            return until(probe, seconds)
        except AssertionError:
            raise AssertionError(json.dumps(diagnostics(label, seen.get('run'), seen.get('chat')))) from None

    def pending(chat, kind):
        return [a for a in chat['actions'] if a['status'] == 'pending' and a['kind'] == kind]

    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source', 'passed': False}
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-phase9-git-') as scratch:
            scratch = Path(scratch).resolve()
            home, repo = scratch / 'home', scratch / 'project'
            home.mkdir(); repo.mkdir()
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(scratch), 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'GIT_CEILING_DIRECTORIES': str(scratch),
                   'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                   'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}

            def git(*argv):
                return subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'init.defaultBranch=main',
                                       '-c', 'commit.gpgSign=false', '-C', str(repo), *argv],
                                      env=env, check=True, capture_output=True, timeout=30).stdout.decode()

            git('init', '-q')
            calc, human = repo / 'calc.py', repo / 'human.txt'
            calc.write_text(BASE_CALC)
            (repo / 'test_calc.py').write_text(TEST_CALC)
            human.write_text(BASE_HUMAN)
            git('add', '--', 'calc.py', 'test_calc.py', 'human.txt')
            git('commit', '-qm', 'Fixture baseline')
            known['base'] = base = git('rev-parse', 'HEAD').strip()
            human.write_text(DIRTY_HUMAN)
            human_hash = sha(human.read_bytes())

            def source_intact(expected_calc):
                assert calc.read_text() == expected_calc, 'source calc.py changed unexpectedly'
                assert sha(human.read_bytes()) == human_hash, 'dirty human file changed'

            service = Service(command, home)
            try:
                service.rpc('model_save', name='Phase 9 Git fixture', url=f'http://127.0.0.1:{server.server_port}',
                            model='phase9-git-fixture', context=32768)
                assert service.rpc('state')['guard']['available'] is True
                checks.append('loopback model profile saved with a 32768-token context before any task')
                cid = service.rpc('new_chat')['id']
                foreign = service.rpc('new_chat')['id']
                service.rpc('set_workspace', id=cid, path=str(repo))

                review = service.rpc('git_preview_create', id=cid)
                assert review['base'] == base and review['root'] == str(repo) and review['requires_native_review'] is True
                rejected(service, 'git_create_worktree', id=foreign, review_id=review['review_id'])
                rejected(service, 'git_create_worktree', id=cid, review_id=review['review_id'])
                review = service.rpc('git_preview_create', id=cid)
                worktree = service.rpc('git_create_worktree', id=cid, review_id=review['review_id'])
                known['worktree_id'] = worktree.get('worktree_id') or worktree['id']
                tree = Path(worktree['path'])
                assert worktree['base'] == base and worktree.get('state') == 'ready'
                assert (tree / 'calc.py').read_text() == BASE_CALC and (tree / 'human.txt').read_text() == BASE_HUMAN
                source_intact(BASE_CALC)
                checks += ['a wrong-chat create consumes the native review token, which cannot be reused',
                           'a fresh native review creates a detached exact-base worktree and preserves the dirty human file']

                (tree / 'calc.py').write_text(NEW_CALC)
                (tree / 'human.txt').write_text('A worktree edit that conflicts with dirty human work.\n')
                exported = service.rpc('git_export', id=cid, worktree_id=known['worktree_id'])
                assert [p['path'] for p in exported['proposals']] == ['calc.py'] and exported['authorization'] == 'none'
                assert {s['path']: s['reason'] for s in exported['skipped']} == {'human.txt': 'stale_parent_changed'}
                assert exported['root'] == str(repo)
                for paths in (['.git/config'], ['.env']):
                    rejected(service, 'git_export', id=cid, worktree_id=known['worktree_id'], file_paths=paths)
                rejected(service, 'git_export', id=foreign, worktree_id=known['worktree_id'])
                rejected(service, 'git_preview_remove', id=cid, worktree_id=known['worktree_id'])
                (tree / 'human.txt').write_text(BASE_HUMAN)
                [item] = service.rpc('git_worktrees', id=cid)['worktrees']
                assert item['state'] == 'ready' and item['dirty'] is True and item['actions'] == ['export']
                source_intact(BASE_CALC)
                checks += ['native export is data only: the dirty-human conflict is skipped and private or foreign requests are refused',
                           'native dirty worktree removal is refused']

                rid = service.rpc('send', id=cid, text=GOAL, limits={'max_steps': 24, 'max_seconds': 180})['run_id']
                run, chat = settle(service, cid, rid, 'pending worktree edit',
                                   lambda run, chat: run['state'] == 'waiting_approval' and steps['root'] >= 5 and pending(chat, 'edit'))
                assert not errors and steps['root'] == 5, diagnostics('five root steps', run, chat)
                assert len(chat['actions']) == 1, diagnostics('a single pending action', run, chat)
                [edit] = pending(chat, 'edit')
                payload = edit['payload']
                assert payload['path'] == 'calc.py' and payload['root'] == str(repo) and payload['permission_mode'] == 'ask'
                assert payload['baseline'] == sha(BASE_CALC) and payload['after'] == NEW_CALC and 'result' not in payload
                source_intact(BASE_CALC)
                tools = {t['call_id']: t for t in run['tools']}
                [outer] = [t for t in run['tools'] if t['name'] == 'IntegrateWorktreeFile']
                outer_id = outer['call_id']
                read, write = tools[outer_id + ':read'], tools[outer_id + ':write']
                assert read['name'] == 'Read' and read['state'] == 'completed' and read['result']['file_hash'] == sha(BASE_CALC)
                assert write['name'] == 'Write' and write['arguments'] == {'file_path': 'calc.py', 'content': NEW_CALC}
                assert write['result']['action_id'] == edit['id'] == outer['result']['action_id'] == known['integration']['action_id']
                [inspect] = [t for t in run['tools'] if t['name'] == 'ReadWorktreeExport']
                [preview] = [t for t in run['tools'] if t['name'] == 'PreviewWorktreeExport']
                assert known['integration'] == {'receipt_id': outer_id, 'read_receipt_id': read['call_id'],
                                                'write_receipt_id': write['call_id'], 'inspection_receipt_id': inspect['call_id'],
                                                'export_receipt_id': preview['call_id'], 'action_id': edit['id']}
                with readonly(home / 'pebble.sqlite') as db:
                    owners = db.execute('SELECT run_id,call_id,kind FROM run_action_owners WHERE action_id=?', (edit['id'],)).fetchall()
                assert owners == [(rid, write['call_id'], 'edit')], 'nested :write must own the broker edit'
                checks += ['root reads typed Git status and a content-free export manifest with exact hashes',
                           'root reads the exact exported proposal as untrusted paged data',
                           'root integration nests an admitted Read and Write on the exact base without changing the source',
                           'ask mode leaves only the pending edit and the run waits for native approval',
                           'run_action_owners pins the nested :write receipt as the broker owner']

                until(lambda: not service.rpc('state')['active'])
                rejected(service, 'approve', id=foreign, action_id=edit['id'])
                applied = service.rpc('approve', id=cid, action_id=edit['id'])
                assert applied['applied'] == 'calc.py' and applied['hash'] == sha(NEW_CALC)
                source_intact(NEW_CALC)
                edit_approved.set()
                run = service.rpc('run_get', id=cid, run_id=rid)
                tools = {t['call_id']: t for t in run['tools']}
                assert tools[write['call_id']]['result']['status'] == 'applied'
                mirrored = tools[outer_id]['result']
                assert mirrored['status'] == 'applied' and mirrored['action_id'] == edit['id']
                assert mirrored['export_id'] == known['export_id'] and mirrored['write_receipt_id'] == write['call_id']
                checks += ['foreign approval is refused and native approval applies the owned edit',
                           'the outer integration receipt mirrors the applied outcome and keeps its correlation metadata']

                run, chat = settle(service, cid, rid, 'pending isolated tests',
                                   lambda run, chat: run['state'] == 'waiting_approval' and steps['root'] >= 6 and pending(chat, 'tests'))
                assert not errors and steps['root'] == 6, diagnostics('six root steps', run, chat)
                # Phase 46 (D154): the run waits at the staged RunTests (runner.STAGED_STOP), so the test proposal the model
                # would have read on a seventh step is read from the run's journal
                [proposal] = [t for t in run['tools'] if t['name'] == 'RunTests']
                assert proposal['result']['kind'] == 'tests' and proposal['result']['status'] == 'awaiting_user_approval', \
                    'test proposal'
                [tests] = pending(chat, 'tests')
                assert tests['payload']['recipe'] == 'python-unittest' and tests['payload']['drafts'] == []
                assert [a['status'] for a in chat['actions'] if a['kind'] == 'edit'] == ['applied']
                until(lambda: not service.rpc('state')['active'])
                rejected(service, 'approve', id=foreign, action_id=tests['id'])
                outcome = service.rpc('approve', id=cid, action_id=tests['id'])
                assert outcome['exit_code'] == 0 and 'macOS sandbox' in outcome['isolation'], {'exit_code': outcome.get('exit_code')}
                tests_approved.set()
                checks.append('the resumed root requests isolated tests and native approval runs the actual macOS sandbox snapshot with exit 0')

                run, chat = settle(service, cid, rid, 'completed root', lambda run, chat: run['state'] == 'completed')
                assert not errors and steps['root'] == STEPS, diagnostics('seven root steps', run, chat)
                [runtests] = [t for t in run['tools'] if t['name'] == 'RunTests']
                assert runtests['result']['status'] == 'applied' and runtests['result']['result']['exit_code'] == 0
                assert {t['call_id']: t for t in run['tools']}[write['call_id']]['result']['status'] == 'applied'
                with readonly(home / 'pebble.sqlite') as db:
                    owners = db.execute('SELECT run_id,call_id,kind FROM run_action_owners WHERE action_id=?', (tests['id'],)).fetchall()
                assert owners == [(rid, runtests['call_id'], 'tests')]
                source_intact(NEW_CALC)
                with readonly(home / 'guard' / 'agentguard.db') as db:
                    guard_metadata = [{'event': r[0], 'tool': r[1], 'action': r[2], 'level': r[3]} for r in db.execute(
                        'SELECT e.type,e.tool,d.action,d.level FROM events e JOIN decisions d ON d.event_id=e.id '
                        'WHERE e.session_id=? ORDER BY e.ts', (rid,))]
                    verification_metadata = [{'kind': r[0], 'status': r[1]} for r in db.execute(
                        'SELECT kind,status FROM verifications WHERE task_id IN '
                        '(SELECT DISTINCT task_id FROM events WHERE session_id=? AND task_id IS NOT NULL) ORDER BY started_at', (rid,))]
                assert any(v['event'] == 'pre_tool_use' and v['tool'] == 'Write' and v['action'] == 'allow' for v in guard_metadata)
                assert any(v['event'] == 'stop' and v['action'] == 'allow' for v in guard_metadata)
                assert any(v['status'] == 'passed' for v in verification_metadata)
                checks += ['actual AgentGuard allowed the nested Write, recorded passed verification and allowed completion for this run',
                           'completion leaves calc.py returning 42 and the dirty human file unchanged']

                (tree / 'calc.py').write_text(BASE_CALC)
                removal = service.rpc('git_preview_remove', id=cid, worktree_id=known['worktree_id'])
                removed = service.rpc('git_remove_worktree', id=cid, review_id=removal['review_id'])
                assert removed['state'] == 'removed' and not tree.exists()
                source_intact(NEW_CALC)
                checks.append('reviewed clean removal deletes only the managed worktree; source and dirty human work are preserved')
                count = len(records)
            finally:
                edit_approved.set(); tests_approved.set()
                service.close()

            service = Service(command, home)
            try:
                run = service.rpc('run_get', id=cid, run_id=rid)
                assert run['state'] == 'completed'
                rejected(service, 'approve', id=cid, action_id=edit['id'])
                rejected(service, 'approve', id=cid, action_id=tests['id'])
                time.sleep(2)
                assert len(records) == count == STEPS, {'provider_requests': len(records)}
                statuses = {a['id']: a['status'] for a in service.rpc('get_chat', id=cid)['actions']}
                assert statuses == {edit['id']: 'applied', tests['id']: 'applied'}
                assert [w['state'] for w in service.rpc('git_worktrees', id=cid)['worktrees']] == ['removed']
                source_intact(NEW_CALC)
                checks.append('restart keeps the completed run with no provider request, approval replay or worktree change')
            finally:
                service.close()
        report['passed'] = True
    except Exception as exc:
        report['failure'] = bounded(exc, 1500)
        raise
    finally:
        edit_approved.set(); tests_approved.set()
        server.shutdown(); server.server_close()
        paths = ['backend/main.py', *[str(p.relative_to(ROOT)) for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
                 'scripts/validate_phase9_git.py']
        sources = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}
        report.update({
            'checks': checks, 'synthetic_provider_attempts': len(records), 'request_metadata': records,
            'provider_errors': errors, 'actual_guard_metadata': guard_metadata, 'actual_verification_metadata': verification_metadata,
            'source_sha256': sources,
            'integrated_source_sha256': sha(json.dumps(sources, sort_keys=True, separators=(',', ':'))),
            'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
            'limits': 'Owned synthetic loopback provider, a temporary macOS Git repository, actual local sandbox tests and the actual '
                      'vendored AgentGuard only; no installed, live-model, other-platform, signing or production proof.'})
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
