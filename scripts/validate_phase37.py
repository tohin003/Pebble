#!/usr/bin/env python3
"""Phase 37 service validator: 1.5.0, Pebble's Workshop part 1, Code Mode (and the Stealth full-screen fix).

RunCode runs one model-written Python script in a sandboxed worker (`--code-worker`); every call the script makes
through the `pebble` module comes back to the run's thread and takes the direct-call path, with its own nested receipt;
only what the script prints returns to the model. These checks drive the checked-in App, runner, broker, sandbox and
worker in process (only the root model is scripted), the same in source and frozen modes, and in frozen mode also start
the frozen backend's own `--code-worker`:
  - a script calls tools through pebble, each call has a nested receipt, and only its printed output enters the window;
  - the worker's sandbox denies the network, the project's files, processes and writes outside its own folder;
  - a runaway script is stopped at its time limit and its printed output is bounded;
  - calls from a script take the direct path: an edit in Ask mode waits for approval, a read outside the project is
    refused, a tool the step does not offer is not callable, and a nested receipt never adopts a model-chosen id;
  - RunCode is offered above 8K with a project, from a run's first step only, and PEBBLE_CODE_MODE=0 removes it;
  - the worker runs from the backend executable (main.py in source mode, the frozen backend in frozen mode);
  - Stealth Lockdown turned on in the full-screen workspace leaves into an ordinary centred window;
  - a ChatGPT request that carries the run's cache key also names it in a session_id header (nothing else does);
  - the release version is 1.5.0 and its notes describe this without a saving claim.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import copy
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_runtime import ROOT  # noqa: E402
from validate_phase13 import bounded, owned_backend, sha  # noqa: E402
from validate_phase20 import write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402
import validate_phase34 as v34  # noqa: E402  (its scripted-model harness)

for _path in (ROOT / 'backend', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'a_script_calls_tools_and_only_its_printed_output_returns',
    'the_worker_sandbox_denies_network_project_files_processes_and_outside_writes',
    'scripts_are_bounded_in_time_and_output',
    'calls_from_a_script_take_the_direct_path',
    'runcode_is_offered_above_8k_with_a_project_from_the_first_step',
    'the_worker_runs_from_the_backend_executable',
    'stealth_leaves_full_screen_into_an_ordinary_window',
    'chatgpt_requests_name_the_run_in_session_id',
    'release_version_is_1_5_0_and_its_notes_describe_it',
    'backend_inputs_stable_during_campaign',
)
READS = ('backend/pebble/code_mode.py', 'backend/pebble/code_worker.py', 'backend/pebble/runner.py',
         'backend/pebble/responses.py',
         'backend/pebble/app.py', 'backend/pebble/tool_catalogue.py', 'backend/pebble/permissions.py',
         'backend/pebble/tool_authz.py', 'backend/main.py', 'src-tauri/src/main.rs', 'src-tauri/tauri.conf.json',
         'scripts/validate_phase34.py', 'CHANGELOG.md', 'scripts/validate_phase37.py', 'backend/pebble/__init__.py',
         'package.json', 'package-lock.json', 'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock')
SHOWN = 16_000  # the release's promise, fixed here so a changed constant cannot pass by changing both


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def flat(text):
    return re.sub(r'\s+', ' ', text)


def changelog_entry(version):
    text = read('CHANGELOG.md')
    match = re.search(r'^## \[' + re.escape(version) + r'\][^\n]*\n(.*?)(?=^## \[|\Z)', text, re.M | re.S)
    assert match, 'CHANGELOG.md has no entry for ' + version
    return flat(match.group(1))


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


def function(shell, name):
    start = shell.index('fn ' + name + '(')
    return shell[start:shell.index('\n}\n', start) + 2]


class Scripted(v34.Scripted):
    """validate_phase34's scripted root model, also keeping the tool names each request offered."""

    def __enter__(self):
        super().__enter__()
        self.tools, inner = [], self.providers.complete

        def complete(provider, messages, tools, cancel, token, effort=False):
            self.tools.append([t['function']['name'] for t in tools or ()])
            return inner(provider, messages, tools, cancel, token, effort)
        self.providers.complete = complete
        return self


def task(home, script, text='How many functions does this project define?', context=32768, mode='auto',
         project=True, switch=None):
    folder = v34.project(home / 'project') if project else None
    saved = os.environ.get('PEBBLE_CODE_MODE')
    if switch is not None:
        os.environ['PEBBLE_CODE_MODE'] = switch
    try:
        app = v34.app_for(home, mode, context)
    finally:
        if saved is None:
            os.environ.pop('PEBBLE_CODE_MODE', None)
        else:
            os.environ['PEBBLE_CODE_MODE'] = saved
    try:
        with Scripted() as scripted:
            scripted.script = script + [v34.final('Done.')] * 3
            chat = app.new_chat(str(folder) if folder else None)
            app.work(chat, text, False, [])
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                runs = app.runs.list(chat['id'])
                if runs and app.active is None and runs[0]['state'] not in ('queued', 'running', 'retry_wait'):
                    break
                time.sleep(.02)
            rid = app.runs.list(chat['id'])[0]['id']
            return {'run': app.runs.get(rid), 'requests': list(scripted.requests), 'tools': list(scripted.tools),
                    'receipts': app.runs.tools(rid), 'folder': folder,
                    'actions': app.store.query('SELECT kind,status FROM actions WHERE chat_id=?', (chat['id'],))}
    finally:
        v34.close(app)


def code(source, ident='rc1', purpose='Check the project'):
    return v34.call('RunCode', {'code': source, 'purpose': purpose}, ident)


def outcome(result, ident='rc1'):
    message = next(m for m in result['requests'][-1] if m.get('role') == 'tool' and m.get('tool_call_id') == ident)
    return json.loads(message['content'])


def _line(process, seconds=20):
    """One line from the worker within `seconds`, or an AssertionError (a worker that never answers fails, not hangs)."""
    import selectors
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        assert selector.select(seconds), 'the worker did not answer within %d s' % seconds
        line = process.stdout.readline()
        assert line, 'the worker ended without answering'
        return json.loads(line)
    finally:
        selector.close()


def worker_round_trip(command, temp):
    """Start a worker command directly (outside any sandbox, in a scratch folder), answer its one call, read its done."""
    process = subprocess.Popen(command, cwd=temp, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env={'HOME': str(temp), 'TMPDIR': str(temp) + '/',
                                                            'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'})
    try:
        header = {'code': 'r = pebble.echo(text="hi")\nprint("echo:", r)\n', 'tools': {'echo': 'echo(text)'}}
        process.stdin.write((json.dumps(header) + '\n').encode())
        process.stdin.flush()
        request = _line(process)
        assert request == {'op': 'call', 'id': 1, 'tool': 'echo', 'args': {'text': 'hi'}}, request
        process.stdin.write((json.dumps({'id': 1, 'result': {'echoed': 'hi'}}) + '\n').encode())
        process.stdin.flush()
        done = _line(process)
        assert done['op'] == 'done' and done['error'] is None and done['printed'] == "echo: {'echoed': 'hi'}\n", done
        process.wait(timeout=20)
        return done
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


class Campaign:
    def __init__(self, scratch, backend=None, only=None):
        self.scratch, self.backend, self.results, self.only = scratch, backend, [], only

    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
        if self.only is not None and name not in self.only:
            return
        began = time.monotonic()
        try:
            evidence = fn() or {}
            self.results.append({'name': name, 'passed': True, 'evidence': evidence,
                                 'seconds': round(time.monotonic() - began, 2)})
        except Exception as exc:
            self.results.append({'name': name, 'passed': False, 'error': bounded(exc, 1500),
                                 'trace': traceback.format_exc(limit=3)[-1200:].replace(str(ROOT), '<checkout>')})

    def run(self):
        scratch = self.scratch

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        def through():
            result = task(scratch / 'through', [code('''
hits = pebble.grep(pattern="def ")
text = pebble.read(file_path="stats.py")
print("definitions:", len(hits["untrusted_matches"]), "lines:", text["total_lines"])
''')])
            assert result['run']['state'] == 'completed', result['run'].get('error')
            data = outcome(result)
            assert data['origin'] == 'run_code' and data['error'] is None, data
            assert data['by_tool'] == {'Grep': 1, 'Read': 1} and data['receipts'] == ['1:rc1:c1', '1:rc1:c2'], data
            assert data['untrusted_printed'].startswith('definitions: '), data['untrusted_printed']
            receipts = {r['call_id']: r for r in result['receipts']}
            assert receipts['1:rc1']['name'] == 'RunCode' and receipts['1:rc1']['state'] == 'completed'
            assert [receipts[c]['name'] for c in ('1:rc1:c1', '1:rc1:c2')] == ['Grep', 'Read']
            assert all(receipts[c]['state'] == 'completed' for c in ('1:rc1:c1', '1:rc1:c2'))
            window = [m for m in result['requests'][-1] if m.get('role') == 'tool']
            assert len(window) == 1 and 'def mean' not in window[0]['content'], 'an inner result entered the window'
            return {'nested_receipts': 2, 'tool_messages_in_window': 1, 'seconds': data['seconds']}
        self.check('a_script_calls_tools_and_only_its_printed_output_returns', through)

        def sandbox():
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            project = (scratch / 'sandbox' / 'project').resolve()
            escape = Path(tempfile.gettempdir()).resolve() / f'pebble-p37-escape-{os.getpid()}.txt'
            try:
                result = task(scratch / 'sandbox', [code(f'''
import os, socket, subprocess
def attempt(label, fn):
    try:
        fn()
        print(label, "ALLOWED")
    except BaseException as exc:
        print(label, "blocked", type(exc).__name__)
attempt("project-read", lambda: open({str(project / "stats.py")!r}).read())
attempt("network", lambda: socket.create_connection(("127.0.0.1", {port}), timeout=2))
attempt("process", lambda: subprocess.run(["/bin/echo", "hi"], check=True))
attempt("write-outside", lambda: open({str(escape)!r}, "w").write("x"))
attempt("scratch-write", lambda: open("scratch.txt", "w").write("ok"))
''')])
            finally:
                listener.close()
            printed = outcome(result)['untrusted_printed']
            for label in ('project-read', 'network', 'process', 'write-outside'):
                assert f'{label} blocked PermissionError' in printed, (label, printed)
            assert 'scratch-write ALLOWED' in printed, printed
            assert not escape.exists(), 'a script wrote outside its folder'
            return {'denied': ['project files', 'network', 'processes', 'writes outside'], 'allowed': ['its folder']}
        self.check('the_worker_sandbox_denies_network_project_files_processes_and_outside_writes', sandbox)

        def bounded_scripts():
            from pebble import code_mode
            wall = code_mode.WALL_SECONDS
            code_mode.WALL_SECONDS = 3
            try:
                result = task(scratch / 'bounded', [code('while True:\n    pass\n', 'rc1'),
                                                    code('print("x" * 100000)', 'rc2')])
            finally:
                code_mode.WALL_SECONDS = wall
            assert result['run']['state'] == 'completed', result['run'].get('error')
            first, second = outcome(result, 'rc1'), outcome(result, 'rc2')
            assert first['error'] and 'limit' in first['error'] and first['calls'] == 0, first
            assert first['seconds'] < 15, first['seconds']
            assert len(second['untrusted_printed']) == SHOWN and second['printed_truncated'] is True
            return {'stopped_after_seconds': first['seconds'], 'printed_shown': SHOWN}
        self.check('scripts_are_bounded_in_time_and_output', bounded_scripts)

        def direct():
            write = 'print(pebble.write(file_path="notes.txt", content="from a script\\n").get("status"))'
            asked = task(scratch / 'ask', [code(write)], text='Write notes.txt with one line.', mode='ask')
            assert outcome(asked)['untrusted_printed'].strip() == 'awaiting_user_approval', outcome(asked)
            assert not (asked['folder'] / 'notes.txt').exists(), 'an Ask-mode edit applied without approval'
            assert ('edit', 'pending') in {(a['kind'], a['status']) for a in asked['actions']}, asked['actions']
            fenced = task(scratch / 'fenced', [code('''
print(sorted(pebble.tools()))
print(hasattr(pebble, "run_command"), hasattr(pebble, "web_search"))
try:
    pebble.read(file_path="../outside.txt")
except pebble.Refused as exc:
    print("refused:", exc)
''')])
            printed = outcome(fenced)['untrusted_printed']
            assert 'False False' in printed and 'refused: ' in printed, printed
            first = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'rc1:c1', 'type': 'function',
                 'function': {'name': 'Read', 'arguments': json.dumps({'file_path': 'stats.py'})}},
                {'id': 'rc1', 'type': 'function', 'function': {'name': 'RunCode', 'arguments': json.dumps(
                    {'code': 'try:\n    pebble.read(file_path="stats.py")\nexcept pebble.Refused as exc:\n'
                             '    print(exc)\n', 'purpose': 'Read once'})}}]}
            collided = task(scratch / 'collide', [first])
            assert 'collides' in outcome(collided)['untrusted_printed'], outcome(collided)
            model_read = next(r for r in collided['receipts'] if r['call_id'] == '1:rc1:c1')
            assert model_read['name'] == 'Read', model_read
            return {'ask_mode_edit': 'awaiting_user_approval', 'outside_project': 'refused',
                    'not_offered': ['run_command', 'web_search'], 'collision': 'refused'}
        self.check('calls_from_a_script_take_the_direct_path', direct)

        def offered():
            read_step = [v34.call('Read', {'file_path': 'stats.py'}, 'r1')]
            large = task(scratch / 'large', read_step)
            assert all('RunCode' in names for names in large['tools']), large['tools']
            assert len({tuple(names) for names in large['tools']}) == 1, 'the tool list changed mid-run'
            small = task(scratch / 'small', read_step, context=8192)
            assert not any('RunCode' in names for names in small['tools'])
            bare = task(scratch / 'bare', [], text='What is a closure in Python?', project=False)
            assert not any('RunCode' in names for names in bare['tools'])
            off = task(scratch / 'off', read_step, switch='0')
            assert [list(n) for n in off['tools']] == [[x for x in n if x != 'RunCode'] for n in large['tools']]
            return {'above_8k': True, 'at_8k': False, 'without_project': False, 'switch_off': 'removed only RunCode'}
        self.check('runcode_is_offered_above_8k_with_a_project_from_the_first_step', offered)

        def worker():
            folder = scratch / 'worker'
            folder.mkdir()
            if self.backend:
                command = [str(self.backend), '--code-worker']
                where = 'the frozen backend'
            else:
                command = [sys.executable, '-s', str(ROOT / 'backend/main.py'), '--code-worker']
                where = 'backend/main.py'
            done = worker_round_trip(command, folder)
            return {'worker': where, 'printed': done['printed'].strip()}
        self.check('the_worker_runs_from_the_backend_executable', worker)

        def stealth():
            shell = read('src-tauri/src/main.rs')
            apply = function(shell, 'apply_stealth')
            assert 'leaving=on && appkit_fullscreen(window.ns_window())' in apply
            assert apply.index('leaving=on') < apply.index('set_fullscreen(false)'), 'measured after leaving'
            assert 'if leaving { LEFT_FULL_SCREEN.store(true, Ordering::SeqCst); windowed_after_full_screen(app); }' in apply
            assert apply.index('set_content_protected(on)') < apply.index('windowed_after_full_screen(app)')
            windowed = function(shell, 'windowed_after_full_screen')
            assert 'if !stealth_on() { return; }' in windowed and 'main_fullscreen(&probe)' in windowed
            assert 'width.min(screen.width*0.8)' in windowed and 'height.min(screen.height*0.8)' in windowed
            assert windowed.index('set_size(') < windowed.index('center()') < windowed.index('set_panel(')
            assert apply.index('set_activation_policy(') < apply.index('RETURN_TO_FULL_SCREEN.swap(false')
            conf = json.loads(read('src-tauri/tauri.conf.json'))
            main = next(w for w in conf['app']['windows'] if w['label'] == 'main')
            assert 'const WINDOWED_SIZE: (f64, f64) = (%d.0, %d.0);' % (main['width'], main['height']) in shell
            return {'windowed_size': [main['width'], main['height']], 'within': '80 % of the screen',
                    'back_to_full_screen': 'when stealth turns off'}
        self.check('stealth_leaves_full_screen_into_an_ordinary_window', stealth)

        def session_header():
            import threading
            from pebble.responses import complete_responses

            class Capture(Exception):
                pass

            class Probe:
                last_usage = None

                def __init__(self, cache_key):
                    self.prompt_cache_key, self.sent = cache_key, []

                def key(self, profile):
                    return 'fixture'

                def headers(self, kind, key):
                    return {'Authorization': 'Bearer ' + key}

                def request(self, url, headers, body, timeout=None):
                    self.sent.append((headers, body))
                    raise Capture()

                def project_context(self, messages, tools, budget, counter=None, method=None):
                    return messages
            chatgpt = {'id': 'p37', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
                       'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'tools': True}
            seen = {}
            for label, profile, key in (('chatgpt', chatgpt, 'run-0123abcd'), ('no_key', chatgpt, None),
                                        ('at_4k', {**chatgpt, 'budget': 4096}, 'run-0123abcd'),
                                        ('openai', {**chatgpt, 'kind': 'openai', 'url': 'https://api.openai.invalid/v1'},
                                         'run-0123abcd')):
                probe = Probe(key)
                try:
                    complete_responses(probe, profile, [{'role': 'user', 'content': 'Hello.', '_pebble_current': True}],
                                       [], threading.Event(), [].append, 'medium')
                except Capture:
                    pass
                headers, body = probe.sent[-1]
                seen[label] = (headers.get('session_id'), body.get('prompt_cache_key'))
            assert seen['chatgpt'] == ('run-0123abcd', 'run-0123abcd'), seen
            assert seen['no_key'] == (None, None) and seen['at_4k'] == (None, None), seen
            assert seen['openai'][0] is None, seen
            return {k: {'session_id': v[0], 'prompt_cache_key': v[1]} for k, v in seen.items()}
        self.check('chatgpt_requests_name_the_run_in_session_id', session_header)

        def version():
            current = checkout_version()
            assert current == '1.5.0', current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = changelog_entry('1.5.0')
            for needle in ("Pebble's Workshop", 'RunCode', 'Only what the script prints', 'PEBBLE_CODE_MODE=0',
                           'no saving is claimed here', 'Stealth Lockdown', 'session_id', 'measured in the next check'):
                assert needle in entry, ('the 1.5.0 notes lack', needle)
            assert 'Context Warping' not in entry, 'Context Warping is named before its gate (D55)'
            claims = re.findall(r'\d+(?:\.\d+)?\s?%\s*(?:cheaper|less|lower|faster|saving|saved|off|smaller|fewer)\b'
                                r'|\bcheaper\b|\bsaves?\b', entry, re.I)
            assert not claims, ('the 1.5.0 notes claim a saving', claims)
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_1_5_0_and_its_notes_describe_it', version)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary folder even on success')
    args = parser.parse_args(argv)
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; choose a new path (earlier raw evidence is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    backend_sha = sha(backend.read_bytes()) if backend else None
    before = digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase37-', dir='/private/tmp')).resolve()
    started = time.monotonic()
    campaign = Campaign(scratch, backend)
    fatal = None
    try:
        campaign.run()
    except Exception as exc:
        fatal = bounded(exc, 1500)
    after = digests()
    stable = before == after and (not backend or sha(backend.read_bytes()) == backend_sha)
    campaign.results.append({'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                             'evidence': {'files_compared': len(before), 'executable_compared': bool(backend)}})
    recorded = {r['name'] for r in campaign.results}
    failed = [r['name'] for r in campaign.results if not r['passed']] + [n for n in PLANNED if n not in recorded]
    passed = fatal is None and not failed and recorded == set(PLANNED)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source', 'passed': passed,
        'checks': [r['name'] for r in campaign.results if r['passed']], 'failed': failed, 'planned_checks': list(PLANNED),
        'check_results': campaign.results, 'fatal_error': fatal,
        'duration_seconds': round(time.monotonic() - started, 1),
        'backend_sha256': backend_sha, 'source_sha256': before,
        'source_note': 'Digests of the tested sources this validator reads; the campaign binds them to its snapshot.',
    }
    if passed and not args.keep_scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    else:
        report['scratch_preserved'] = str(scratch)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_new(report_path, json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']), 'failed': failed,
                      'fatal_error': fatal}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
