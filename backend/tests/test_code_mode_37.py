"""Phase 37 (1.5.0, Pebble's Workshop part 1): Code Mode.

RunCode runs one model-written Python script in a sandboxed worker (`--code-worker`). The script calls this run's own
tools through the `pebble` module; every call comes back to the run's thread and takes the direct-call path (offered
schema, authorize, execute, a nested receipt `<RunCode receipt>:c<n>`), and only what the script prints returns to the
model. RunCode is offered above 8K with a project, from a run's first step; PEBBLE_CODE_MODE=0 keeps 1.4.0's requests.

The App runner, broker, sandbox and worker are the checked-in code; only the root model is scripted.
"""
import json
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'scripts'))

import validate_phase34 as v34  # noqa: E402  (its scripted-model harness)
from pebble import code_mode  # noqa: E402

macos = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox')


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


def task(tmp_path, script, text='How many functions does this project define?', context=32768, mode='auto',
         project=True, switch=None, monkeypatch=None):
    folder = v34.project(tmp_path / 'project') if project else None
    if switch is not None:
        monkeypatch.setenv('PEBBLE_CODE_MODE', switch)
    app = v34.app_for(tmp_path, mode, context)
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
                    'receipts': app.runs.tools(rid), 'folder': folder, 'actions': app.store.query(
                        'SELECT kind,status FROM actions WHERE chat_id=?', (chat['id'],))}
    finally:
        v34.close(app)


def code(source, ident='rc1', purpose='Check the project'):
    return v34.call('RunCode', {'code': source, 'purpose': purpose}, ident)


def outcome(result, ident='rc1'):
    message = next(m for m in result['requests'][-1] if m.get('role') == 'tool' and m.get('tool_call_id') == ident)
    return json.loads(message['content'])


@macos
def test_a_script_calls_tools_through_pebble_and_only_its_output_returns(tmp_path):
    result = task(tmp_path, [code('''
hits = pebble.grep(pattern="def ")
text = pebble.read(file_path="stats.py")
print("definitions:", len(hits["untrusted_matches"]), "lines:", text["total_lines"])
''')])
    assert result['run']['state'] == 'completed', result['run'].get('error')
    data = outcome(result)
    assert data['origin'] == 'run_code' and data['error'] is None and data['by_tool'] == {'Grep': 1, 'Read': 1}
    assert data['untrusted_printed'].startswith('definitions: ') and data['receipts'] == ['1:rc1:c1', '1:rc1:c2']
    receipts = {r['call_id']: r for r in result['receipts']}
    assert receipts['1:rc1']['name'] == 'RunCode' and receipts['1:rc1']['state'] == 'completed'
    assert receipts['1:rc1:c1']['name'] == 'Grep' and receipts['1:rc1:c2']['name'] == 'Read'
    assert all(receipts[c]['state'] == 'completed' for c in ('1:rc1:c1', '1:rc1:c2'))
    # The inner results never entered the window: one tool message, and no file text in it.
    tools = [m for m in result['requests'][-1] if m.get('role') == 'tool']
    assert len(tools) == 1 and 'def mean' not in tools[0]['content']


@macos
def test_the_sandbox_has_no_network_no_project_files_no_processes_and_no_writes_outside(tmp_path):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        project = (tmp_path / 'project').resolve()
        result = task(tmp_path, [code(f'''
import os, socket, subprocess
def attempt(label, fn):
    try:
        fn()
        print(label, "ALLOWED")
    except BaseException as exc:
        print(label, "blocked", type(exc).__name__)
attempt("project-read", lambda: open({str(project / "stats.py")!r}).read())
attempt("home-read", lambda: os.listdir({str(Path.home() / '.pebble')!r}))
attempt("network", lambda: socket.create_connection(("127.0.0.1", {port}), timeout=2))
attempt("process", lambda: subprocess.run(["/bin/echo", "hi"], check=True))
attempt("write-outside", lambda: open("/private/tmp/pebble-code-escape.txt", "w").write("x"))
attempt("scratch-write", lambda: open("scratch.txt", "w").write("ok"))
''')])
    finally:
        listener.close()
    printed = outcome(result)['untrusted_printed']
    for label in ('project-read', 'home-read', 'network', 'process', 'write-outside'):
        assert f'{label} blocked' in printed, printed
    assert 'scratch-write ALLOWED' in printed, printed
    assert not Path('/private/tmp/pebble-code-escape.txt').exists()


@macos
def test_limits_stop_a_runaway_script_and_bound_its_output(tmp_path, monkeypatch):
    monkeypatch.setattr(code_mode, 'WALL_SECONDS', 3)
    result = task(tmp_path, [code('while True:\n    pass\n', 'rc1'), code('print("x" * 100000)', 'rc2')])
    assert result['run']['state'] == 'completed', result['run'].get('error')
    first = outcome(result, 'rc1')
    assert 'limit' in first['error'] and first['calls'] == 0
    second = outcome(result, 'rc2')
    assert len(second['untrusted_printed']) == code_mode.SHOWN and second['printed_truncated'] is True


@macos
def test_an_edit_from_a_script_follows_the_permission_mode(tmp_path):
    source = 'print(pebble.write(file_path="notes.txt", content="from a script\\n").get("status"))'
    asked = task(tmp_path / 'ask', [code(source)], text='Write notes.txt with one line.', mode='ask')
    assert asked['run']['state'] in ('completed', 'waiting_approval'), asked['run'].get('error')
    assert outcome(asked)['untrusted_printed'].strip() == 'awaiting_user_approval'
    assert not (asked['folder'] / 'notes.txt').exists()
    assert ('edit', 'pending') in {(a['kind'], a['status']) for a in asked['actions']}
    auto = task(tmp_path / 'auto', [code(source)], text='Write notes.txt with one line.', mode='auto')
    assert outcome(auto)['untrusted_printed'].strip() == 'applied'
    assert (auto['folder'] / 'notes.txt').read_text() == 'from a script\n'
    write = next(r for r in auto['receipts'] if r['call_id'] == '1:rc1:c1')
    assert write['name'] == 'Write' and write['state'] == 'completed'


@macos
def test_only_offered_tools_are_callable_and_a_refusal_is_the_hosts_message(tmp_path):
    result = task(tmp_path, [code('''
print(sorted(pebble.tools()))
print(hasattr(pebble, "web_search"), hasattr(pebble, "run_command"))
try:
    pebble.read(file_path="../outside.txt")
except pebble.Refused as exc:
    print("refused:", exc)
''')])
    printed = outcome(result)['untrusted_printed']
    assert 'False False' in printed  # the web permission is off; commands are never callable from a script
    assert 'refused: ' in printed


def test_the_host_refuses_a_tool_the_script_was_not_given():
    answer = code_mode._serve(None, {'tool': 'run_command', 'args': {}}, {'read': 'Read(file_path)'}, {}, [],
                              None, 'c', 'r', '1:rc1', __import__('collections').Counter(), [], None)
    assert 'not available' in answer['error']


@macos
def test_a_nested_receipt_never_adopts_a_model_chosen_id(tmp_path):
    first = {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'rc1:c1', 'type': 'function', 'function': {'name': 'Read', 'arguments': json.dumps({'file_path': 'stats.py'})}},
        {'id': 'rc1', 'type': 'function', 'function': {'name': 'RunCode', 'arguments': json.dumps(
            {'code': 'try:\n    pebble.read(file_path="stats.py")\nexcept pebble.Refused as exc:\n    print(exc)\n',
             'purpose': 'Read once'})}}]}
    result = task(tmp_path, [first])
    assert 'collides' in outcome(result)['untrusted_printed']
    assert next(r for r in result['receipts'] if r['call_id'] == '1:rc1:c1')['name'] == 'Read'


@macos
def test_a_script_that_is_not_python_runs_nothing(tmp_path):
    result = task(tmp_path, [code('print(')])
    data = outcome(result)
    assert 'not valid Python' in json.dumps(data) and not any(r['call_id'].startswith('1:rc1:') for r in result['receipts'])


@macos
def test_runcode_is_offered_above_8k_with_a_project_from_the_first_step_only(tmp_path, monkeypatch):
    large = task(tmp_path / 'large', [v34.call('Read', {'file_path': 'stats.py'}, 'r1')])
    assert all('RunCode' in names for names in large['tools']) and len(set(map(tuple, large['tools']))) == 1
    small = task(tmp_path / 'small', [v34.call('Read', {'file_path': 'stats.py'}, 'r1')], context=8192)
    assert not any('RunCode' in names for names in small['tools'])
    bare = task(tmp_path / 'bare', [], text='What is a closure in Python?', project=False)
    # D98 (Phase 41, 1.7.0): Code Mode joins the Companion's tools above 8K, where Pebble's Workshop makes its tools.
    assert all('RunCode' in names for names in bare['tools'])
    bare_off = task(tmp_path / 'bare-off', [], text='What is a closure in Python?', project=False, switch='0',
                    monkeypatch=monkeypatch)
    assert not any('RunCode' in names for names in bare_off['tools'])
    off = task(tmp_path / 'off', [v34.call('Read', {'file_path': 'stats.py'}, 'r1')], switch='0',
               monkeypatch=monkeypatch)
    assert not any('RunCode' in names for names in off['tools'])
    assert [names for names in off['tools']] == [[n for n in names if n != 'RunCode'] for names in large['tools']]


@macos
def test_a_failed_edit_inside_a_script_stops_the_script_and_the_run(tmp_path, monkeypatch):
    from pebble.app import App
    real = App._execute

    def failing(self, name, args, ws, cid):
        if name == 'Write':
            raise RuntimeError('disk vanished')
        return real(self, name, args, ws, cid)
    monkeypatch.setattr(App, '_execute', failing)
    result = task(tmp_path, [code('pebble.write(file_path="notes.txt", content="x\\n")\nprint("after")')],
                  text='Write notes.txt.')
    receipts = {r['call_id']: r['state'] for r in result['receipts']}
    # The nested write is uncertain, as a direct one would be, and an uncertain receipt stops the run.
    assert receipts.get('1:rc1:c1') == 'uncertain' and receipts.get('1:rc1') == 'uncertain'
    assert result['run']['state'] != 'completed'


def test_child_agents_are_never_offered_runcode_and_its_effects_are_its_calls():
    from pebble.tool_authz import ROOT_ONLY
    from pebble.runtime import EFFECTS
    # A script's edits and plugin calls are effects under their own names, with their own receipts.
    assert 'RunCode' in ROOT_ONLY and 'RunCode' not in EFFECTS and {'Write', 'Edit', 'Append', 'PluginCall'} <= EFFECTS
