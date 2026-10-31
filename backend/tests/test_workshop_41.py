"""Phase 41 (1.7.0, Pebble's Workshop part 2): workshop.py and its Code Mode wiring (agent-made tools, D67-D70, D90)."""
import json
import sys
from pathlib import Path

import pytest

from pebble import code_mode, workshop as ws
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='the validation worker needs the macOS sandbox')

ADD = "def run(args):\n    return args['a'] + args['b']\n"
INPUTS = {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}}, 'required': ['a', 'b']}
TESTS = [{'args': {'a': 1, 'b': 2}, 'expect': 3}, {'args': {'a': -1, 'b': 1}, 'expect': 0},
         {'args': {'a': 0, 'b': 0}, 'expect': 0}]


class Store:
    def __init__(self):
        self.values = {}

    def setting(self, key, default=None):
        return json.loads(json.dumps(self.values[key])) if key in self.values else default

    def set_setting(self, key, value):
        self.values[key] = json.loads(json.dumps(value))


class Runs:
    def __init__(self):
        self.notes, self.receipts = [], []

    def note(self, rid, kind, data):
        self.notes.append((rid, kind, data))

    def tools(self, rid):
        return [{'name': name} for name in self.receipts]


class Model:
    def current(self):
        return {'model': 'test-model'}


class FakeApp:
    def __init__(self):
        self.store, self.runs, self.model = Store(), Runs(), Model()
        self.events = []
        self.task_policy = json.loads(json.dumps(DEFAULT))
        self.workshop = None

    def emit(self, kind, data):
        self.events.append((kind, data))


def make(app, rid='r1', **over):
    args = {'name': 'add_pair', 'purpose': 'Add two integers', 'code': ADD, 'tests': TESTS, 'inputs': INPUTS, **over}
    return ws.make(app, rid, args, code_mode.validate_tool, app.runs.tools(rid) and [t['name'] for t in app.runs.tools(rid)])


def blind_ok(app, messages):
    return {'content': json.dumps([{'args': {'a': n, 'b': n}, 'expect': 2 * n} for n in range(5)])}


def blind_wrong(app, messages):
    return {'content': json.dumps([{'args': {'a': n, 'b': n}, 'expect': 2 * n + 1} for n in range(5)])}


# -- the static check, schema and tests -----------------------------------------------------------------------------

@pytest.mark.parametrize('code, needle', [
    ('import os\ndef run(args):\n    return 1\n', 'import os'),
    ('import socket\ndef run(args):\n    return 1\n', 'import socket'),
    ('def run(args):\n    return eval("1")\n', 'eval'),
    ('def run(args):\n    return open("/etc/hosts").read()\n', 'open'),
    ('def run(args):\n    return ().__class__.__bases__\n', 'dunder'),
    ('import pebble\ndef run(args):\n    return 1\n', 'pebble'),
    ('def go(args):\n    return 1\n', 'run(args)'),
    ('print("x")\ndef run(args):\n    return 1\n', 'top level'),
    ('X = 1\ndef run(args):\n    global X\n    return X\n', 'global'),
])
def test_the_static_check_refuses_what_reaches_outside_a_calculation(code, needle):
    found = ws.static_findings('a_tool', 'does a thing', code)
    assert any(needle in f for f in found), found


def test_a_pure_tool_passes_the_static_check():
    assert ws.static_findings('add_pair', 'Add two integers', ADD) == []
    assert ws.static_findings('Bad-Name', 'x', ADD) and ws.static_findings('run', 'x', ADD)


def test_schema_and_tests_are_checked():
    assert ws.schema_findings(INPUTS) == []
    assert ws.schema_findings({'type': 'object', 'properties': {}})
    assert ws.schema_findings({'type': 'object', 'properties': {'a': {'type': 'blob'}}})
    assert ws.test_findings(TESTS) == []
    assert ws.test_findings(TESTS[:2])
    assert ws.test_findings(TESTS + [{'args': {}, 'expect': 1, 'raises': 'ValueError'}])


def test_generated_inputs_are_the_same_every_time_and_cover_the_edges():
    first, second = ws.generate(INPUTS), ws.generate(INPUTS)
    assert first == second and 10 <= len(first) <= ws.CASES
    assert {'a': 0, 'b': 10 ** 12} in first or any(c.get('b') == 10 ** 12 for c in first)


# -- make: validated independently in the sandboxed worker ---------------------------------------------------------

def test_a_correct_tool_is_made_and_callable_at_once():
    app = FakeApp()
    result, mine = make(app)
    assert result['made'] and mine == {'add_pair': ADD} and result['generated_inputs'] >= 10
    assert ('r1', 'workshop_made', {'name': 'add_pair', 'digest': ws._made(app, 'r1')['add_pair']['digest'],
                                    'validated': True, 'findings': []}) in app.runs.notes


def test_a_tool_that_fails_its_own_tests_is_not_made():
    app = FakeApp()
    result, mine = make(app, tests=[{**t, 'expect': 99} for t in TESTS])
    assert not result['made'] and mine is None and any('Test 1 failed' in f for f in result['findings'])


@pytest.mark.parametrize('code, outcome', [
    ("def run(args):\n    return 1 / args['a']\n", 'raised'),                       # ZeroDivisionError at a=0
    ("def run(args):\n    return {1, 2}\n", 'not_json'),
    ("import uuid\ndef run(args):\n    return str(uuid.uuid4())\n", 'nondeterministic'),
    ("def run(args):\n    while True:\n        pass\n", 'timeout'),
])
def test_generated_inputs_catch_what_the_makers_tests_missed(code, outcome):
    app = FakeApp()
    tests = [{'args': {'a': 1, 'b': 1}, 'expect': 1}] * 3 if outcome == 'raised' else TESTS
    result, mine = make(app, code=code, tests=tests)
    assert not result['made'] and mine is None, result
    if outcome != 'timeout':
        assert any(outcome in f or 'failed' in f for f in result['findings']), result['findings']


def test_a_name_may_be_remade_at_most_three_times():
    app = FakeApp()
    for _ in range(3):
        make(app, tests=[{**t, 'expect': 99} for t in TESTS])
    result, _ = make(app)
    assert not result['made'] and any('3 times' in f for f in result['findings'])


# -- keep: D69 ------------------------------------------------------------------------------------------------------

def used(app, rid='r1', uses=2, errors=0):
    ws.record_uses(app, rid, {'add_pair': [uses, errors]})


def test_pebble_keeps_a_validated_used_untainted_blind_checked_tool_and_always_tells_the_person():
    app = FakeApp()
    make(app)
    used(app)
    result = ws.keep(app, 'r1', 'c1', {'name': 'add_pair', 'reason': 'sums pairs'}, code_mode.validate_tool, blind_ok)
    assert result['kept'] and result['by'] == 'pebble' and 'Tell the person' in result['notice']
    assert app.events[-1][0] == 'toolbox' and app.events[-1][1]['kind'] == 'kept'
    tool = ws.toolbox(app)['tools'][0]
    assert tool['kept_by'] == 'pebble' and ws.verified(tool) and tool['blind'] == {'run': 5, 'passed': 5}


@pytest.mark.parametrize('setup, reason', [
    (lambda app: None, 'not been used successfully'),
    (lambda app: used(app, uses=2, errors=2), 'not been used successfully'),
])
def test_an_unused_tool_is_asked_about(setup, reason):
    app = FakeApp()
    make(app)
    setup(app)
    result = ws.keep(app, 'r1', 'c1', {'name': 'add_pair'}, code_mode.validate_tool, blind_ok)
    assert result['asked'] and any(reason in w for w in result['why'])
    assert app.events[-1][1]['kind'] == 'asked' and ws.toolbox(app)['pending']


def test_a_tool_made_after_untrusted_content_is_asked_about():
    app = FakeApp()
    app.runs.receipts = ['ReadURL']
    make(app)
    used(app)
    result = ws.keep(app, 'r1', 'c1', {'name': 'add_pair'}, code_mode.validate_tool, blind_ok)
    assert result['asked'] and any('untrusted' in w for w in result['why'])


def test_a_blind_check_that_disagrees_or_cannot_run_makes_pebble_ask():
    for complete, words in ((blind_wrong, 'disagreed'), (lambda app, m: {'content': 'no idea'}, 'could not run')):
        app = FakeApp()
        make(app)
        used(app)
        result = ws.keep(app, 'r1', 'c1', {'name': 'add_pair'}, code_mode.validate_tool, complete)
        assert result['asked'] and any(words in w for w in result['why']), result


def test_the_person_decides_a_pending_keep_and_a_same_name_keep_asks_to_replace():
    app = FakeApp()
    make(app)
    used(app)
    ws.keep(app, 'r1', 'c1', {'name': 'add_pair'}, code_mode.validate_tool, blind_ok)
    # Phase 45 (F203, audit round R7): the identical tool kept again is already kept, with no second card
    make(app, rid='r2')
    used(app, rid='r2')
    again = ws.keep(app, 'r2', 'c1', {'name': 'add_pair'}, code_mode.validate_tool, blind_ok)
    assert again['kept'] is True and again.get('already') is True and not ws.toolbox(app)['pending']
    # a changed tool of the same name still asks to replace it
    make(app, rid='r3', code=ADD.replace("args['a'] + args['b']", "args['b'] + args['a']"))
    used(app, rid='r3')
    asked = ws.keep(app, 'r3', 'c1', {'name': 'add_pair'}, code_mode.validate_tool, blind_ok)
    assert asked['asked'] and any('replaces it' in w for w in asked['why'])
    pending = ws.toolbox(app)['pending'][0]['id']
    listing = ws.decide(app, pending, True)
    assert [t['name'] for t in listing['tools']] == ['add_pair'] and listing['tools'][0]['kept_by'] == 'person'
    with pytest.raises(PolicyError):
        ws.decide(app, pending, True)


def test_remove_and_discard():
    app = FakeApp()
    make(app)
    used(app)
    ws.keep(app, 'r1', 'c1', {'name': 'add_pair'}, code_mode.validate_tool, blind_ok)
    assert ws.remove(app, 'add_pair')['tools'] == []
    with pytest.raises(PolicyError):
        ws.remove(app, 'add_pair')


# -- the Toolbox as a supply chain -----------------------------------------------------------------------------------

def kept_app():
    app = FakeApp()
    make(app)
    used(app)
    ws.keep(app, 'r1', 'c1', {'name': 'add_pair'}, code_mode.validate_tool, blind_ok)
    app.workshop = None  # a new app session: nothing re-validated yet
    return app


def test_a_kept_tool_is_offered_only_to_scripts_that_name_it_after_re_validation():
    app = kept_app()
    assert ws.mine_for_run(app, 'r9', 'print(1)', code_mode.validate_tool) == {}
    assert ws.mine_for_run(app, 'r9', 'print(pebble.mine.add_pair(a=1, b=2))', code_mode.validate_tool) == {'add_pair': ADD}
    assert ws.state(app).revalidated


def test_a_changed_kept_tool_is_never_run_and_needs_attention():
    app = kept_app()
    box = ws.toolbox(app)
    box['tools'][0]['spec']['code'] = "def run(args):\n    return 0\n"   # tampered after it was kept
    app.store.set_setting('toolbox', box)
    assert ws.mine_for_run(app, 'r9', 'pebble.mine.add_pair(a=1, b=2)', code_mode.validate_tool) == {}
    assert ws.listing(app)['tools'][0]['state'] == 'needs_attention'


def test_find_tool_finds_kept_tools_by_their_purpose():
    app = kept_app()
    assert ws.search(app, 'add two integers')[0]['name'] == 'pebble.mine.add_pair'
    assert ws.search(app, 'translate french') == []


# -- Code Mode wiring ------------------------------------------------------------------------------------------------

def test_the_switch_restores_the_1_6_0_description(monkeypatch):
    import importlib
    monkeypatch.setenv('PEBBLE_TOOL_MAKING', '0')
    reloaded = importlib.reload(code_mode)
    try:
        assert 'make_tool' not in reloaded.DESCRIPTION
    finally:
        monkeypatch.delenv('PEBBLE_TOOL_MAKING')
        importlib.reload(code_mode)
    assert 'pebble.make_tool' in code_mode.DESCRIPTION


def test_the_worker_makes_and_uses_a_tool_in_one_script(tmp_path):
    """The real worker under its sandbox: make_tool's answer adds the tool to pebble.mine at once."""
    import subprocess
    exe, command, runtime, main = code_mode._worker_command()
    script = ("r = pebble.make_tool(name='add_pair', purpose='Add', code=CODE, tests=[], inputs={})\n"
              "print(pebble.mine.add_pair(a=2, b=3))\n").replace('CODE', repr(ADD))
    process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', code_mode.profile(exe, runtime, tmp_path, main), *command],
                               cwd=tmp_path, env={'HOME': str(tmp_path), 'TMPDIR': str(tmp_path) + '/', 'PATH': '/usr/bin:/bin'},
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    process.stdin.write((json.dumps({'code': script, 'tools': {}, 'mine': {}, 'workshop': True}) + '\n').encode())
    process.stdin.flush()
    request = json.loads(process.stdout.readline())
    assert request['op'] == 'call' and request['tool'] == 'make_tool'
    process.stdin.write((json.dumps({'id': request['id'], 'result': {'made': True}, 'mine': {'add_pair': ADD}}) + '\n').encode())
    process.stdin.flush()
    done = json.loads(process.stdout.readline())
    process.wait(timeout=10)
    assert done['op'] == 'done' and done['printed'].strip() == '5' and done['mine'] == {'add_pair': [1, 0]}


def test_a_tool_that_was_not_made_says_why_when_the_script_uses_it(tmp_path):
    """Phase 46 (golden dev d-wr-01 on gpt-6.1-sol, 3 of 3 runs): a script made a tool, make_tool answered made False, and
    the script's next line failed with a bare AttributeError. The use now names make_tool's findings."""
    import subprocess
    exe, command, runtime, main = code_mode._worker_command()
    script = ("r = pebble.make_tool(name='iban_check', purpose='Check', code='def iban_check(text): return 1', "
              "tests=[], inputs={})\n"
              "print(pebble.mine.iban_check(text='x'))\n")
    process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', code_mode.profile(exe, runtime, tmp_path, main), *command],
                               cwd=tmp_path, env={'HOME': str(tmp_path), 'TMPDIR': str(tmp_path) + '/', 'PATH': '/usr/bin:/bin'},
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    process.stdin.write((json.dumps({'code': script, 'tools': {}, 'mine': {}, 'workshop': True}) + '\n').encode())
    process.stdin.flush()
    request = json.loads(process.stdout.readline())
    finding = 'Define exactly one top-level function run(args) taking one argument, the input object.'
    process.stdin.write((json.dumps({'id': request['id'], 'result': {'made': False, 'findings': [finding]}}) + '\n').encode())
    process.stdin.flush()
    done = json.loads(process.stdout.readline())
    process.wait(timeout=10)
    assert done['op'] == 'done' and 'pebble.mine.iban_check does not exist: make_tool did not make it' in done['error']
    assert finding in done['error']  # the static check's contract (run(args)) reaches the model at the failing line


def test_a_tool_made_in_a_conversation_stays_usable_in_its_later_turns():
    """Phase 46 (dev d-wr-01 on gpt-6.1-sol): a made tool belonged to one run, so every follow-up turn (a new run) wrote
    and validated the same tool again, about 20 s a turn. A validated tool stays with its conversation."""
    app = FakeApp()
    chats = {'r1': 'c1', 'r2': 'c1', 'r3': 'c2'}
    app.runs.get = lambda rid: {'id': rid, 'chat_id': chats[rid]}
    app.runs.list = lambda cid: [{'id': rid} for rid in ('r3', 'r2', 'r1') if chats[rid] == cid]  # newest first
    result, _ = make(app, 'r1')
    assert result['made']
    assert set(ws.conversation_tools(app, 'r2')) == {'add_pair'} and ws.conversation_tools(app, 'r3') == {}
    assert ws.mine_for_run(app, 'r2', 'print(1)', code_mode.validate_tool) == {'add_pair': ADD}
    assert ws.mine_for_run(app, 'r3', 'print(1)', code_mode.validate_tool) == {}  # another conversation: nothing
    # shared into the later run, so its uses count there and it can be kept from the later turn
    ws.record_uses(app, 'r2', {'add_pair': [2, 0]})
    assert ws._made(app, 'r2')['add_pair'] is ws._made(app, 'r1')['add_pair']
    assert ws._made(app, 'r2')['add_pair']['uses'] == 2
    # a tool that did not pass validation is never shared
    bad, _ = make(app, 'r1', name='bad_pair', code="def run(args):\n    return args['a'] - args['b']\n")
    assert not bad['made'] and 'bad_pair' not in ws.conversation_tools(app, 'r2')


# -- end to end: the follow-up turn is told which tools its conversation already has --------------------------------

from test_approval_handoff_22 import env, final, settled  # noqa: E402,F401 (fixture)


def test_a_follow_up_turn_is_told_about_its_conversations_tools(env):
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    env.script = [final('Lines 1 and 3 are valid.\nFINAL: 1,3'), final('Line 2 is valid.\nFINAL: 2')]
    app.work(chat, 'Which lines of ibans/a.txt hold valid IBANs? End with `FINAL: <lines>`.', False, [])
    first = app.runs.list(chat['id'])[0]['id']
    settled(env, app, first, 'completed', 1)
    with ws.state(app).lock:  # as RunCode's make_tool leaves a validated tool
        ws.state(app).made[first] = {'iban_check': {'validated': True, 'spec': {
            'name': 'iban_check', 'purpose': 'Check IBANs', 'code': ADD, 'inputs': {'type': 'object', 'properties': {
                'text': {'type': 'string'}}}}, 'uses': 1, 'errors': 0, 'makes': 1, 'made_after': []}}
    app.work(app.chat(chat['id']), 'Same question for ibans/b.txt.', False, [])
    second = app.runs.list(chat['id'])[0]['id']
    settled(env, app, second, 'completed', 2)
    notes = [m for m in env.requests[1] if m.get('name') == 'pebble_workshop']
    assert len(notes) == 1 and 'pebble.mine.<name>(**args)' in notes[0]['content']
    assert json.loads(notes[0]['content'].split(': ', 2)[2]) == [{'name': 'iban_check', 'purpose': 'Check IBANs',
                                                                    'inputs': ['text']}]
    assert not [m for m in env.requests[0] if m.get('name') == 'pebble_workshop']  # the first turn had none
