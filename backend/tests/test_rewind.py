"""Phase 20 track G20-rewind: run checkpoints and rewind for file edits (D19 §8.2, frontier gap X10).

Every run here is a real App run driven by a scripted provider (App.model.complete), so the edits,
commands and connector calls reach the broker, the run journal and the provenance ledger exactly as
in the product. Commands use a recording fake in place of the confined launcher; the connector is the
loopback MCP mail fake from test_connector_effects. Nothing reaches a model or the network, and the
continuation thread is closed so an approval never resumes a run behind a test.

Until the parent wires the routes (app.py dispatch, tool_authz.NATIVE_ONLY, main.rs), `route` calls
the module's own route table with the same arguments; once wired, the same calls go through
App.dispatch, and the window checks below tighten on their own. Merge (i20): the wiring landed, so
the window checks run on the real main.rs and NATIVE_ONLY.
"""
import copy
import json
import re
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import app as app_module
from pebble import runtime, tool_authz
from pebble.app import App
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError, digest
from pebble.rewind import FILE_TOOLS, KINDS, NOTES, REASONS, ROUTES, Rewinds
from pebble.tool_authz import Actor
from test_connector_effects import ALICE, EFFECTS as MAIL_EFFECTS, TOOLS as MAIL_TOOLS, server, tool_calls
from test_high_fd_waits_21 import close_guard_databases

assert server  # the loopback MCP fixture, imported so it applies here

ROOT = Path(__file__).resolve().parents[2]
MAIN_RS = ROOT / 'src-tauri' / 'src' / 'main.rs'
WIDE = {'id': 'wide', 'kind': 'openai', 'name': 'Wide', 'url': 'https://unused.invalid', 'model': 'fixture',
        'context': 32768, 'budget': 16384, 'tools': True}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded(original):
        def attempt(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ('127.0.0.1', '::1'):
                raise AssertionError('Rewind tests must not open network connections.')
            return original(self, address)
        return attempt
    monkeypatch.setattr(socket.socket, 'connect', guarded(connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded(connect_ex))


@pytest.fixture
def host(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        assert app.controller.close(), 'approvals must never auto-continue a run behind the test'
        monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(WIDE))
        tools = app.store.setting('permissions', DEFAULT)['tools']
        app.setting('permissions', {'mode': 'auto', 'tools': {name: name != 'screen' for name in tools}})
        chat = app.new_chat(str(project))
        yield SimpleNamespace(app=app, root=project, chat=chat, cid=chat['id'],
                              rewinds=getattr(app, 'rewinds', None) or Rewinds(app))
    finally:
        app.active = app.active_run = None
        app.close()
        close_guard_databases(tmp_path)  # resource cleanup only (F21-fd)


def call(ident, name, **args):
    return {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


class Script:
    """A scripted provider: one tool-call response per model step, then a final answer for any later step."""

    def __init__(self, app, *responses):
        self.responses, self.calls = list(responses), 0
        app.model.complete = self

    def __call__(self, messages, tools, cancel, token, effort):
        self.calls += 1
        if self.responses:
            return {'role': 'assistant', 'tool_calls': self.responses.pop(0)}
        return {'role': 'assistant', 'content': 'Done.'}


def no_model(*args, **kwargs):
    raise AssertionError('A rewind never calls the model.')


def route(host, method, **params):
    """A native route through App.dispatch once wired; until then the module's route table, same arguments."""
    try:
        return host.app.dispatch(method, params)
    except ValueError as exc:
        if str(exc) != 'Unknown request.':
            raise
    return host.rewinds.methods()[method](**params)


def preview(host, rid, step, surface='main'):
    return route(host, 'rewind_preview', chat_id=host.cid, run_id=rid, step=step, surface=surface)


def apply(host, rid, step, value, surface='main'):
    return route(host, 'rewind_apply', chat_id=host.cid, run_id=rid, step=step, digest=value, surface=surface)


def text(host, name):
    target = host.root / name
    return target.read_text() if target.exists() else None


def by_path(items):
    return {item['path']: item for item in items}


def rewind_actions(host):
    return [a for a in host.app.store.actions(host.cid) if isinstance(a['payload'].get('rewind'), dict)]


def snapshot(app):
    """Every row of every data table, for a "writes nothing" comparison."""
    with app.store.lock:
        names = [n for (n,) in app.store.db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                    "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'")]
        names = [n for n in names if not n.startswith('memory_search')]
        return {n: sorted(map(repr, app.store.db.execute(f'SELECT * FROM "{n}"').fetchall())) for n in names}


def rust_list(source, pattern):
    return set(re.findall(r'"([a-z_]+)"', re.search(pattern, source, re.S).group(1)))


def two_step_run(host):
    """Step 1 edits a.txt; step 2 edits a.txt and b.txt and creates c.txt. Edits apply (Auto mode)."""
    (host.root / 'a.txt').write_text('zero\n')
    (host.root / 'b.txt').write_text('bee\n')
    Script(host.app,
           [call('r1', 'Read', file_path='a.txt'),
            call('e1', 'Edit', file_path='a.txt', old_string='zero', new_string='one')],
           [call('r2', 'Read', file_path='b.txt'),
            call('e2', 'Edit', file_path='a.txt', old_string='one', new_string='two'),
            call('e3', 'Edit', file_path='b.txt', old_string='bee', new_string='BEE'),
            call('w1', 'Write', file_path='c.txt', content='new\n')])
    host.app.work(host.chat, 'Change a.txt and b.txt, and write c.txt', False, [])
    host.app.model.complete = no_model
    rid = host.app.runs.list(host.cid)[0]['id']
    assert host.app.runs.get(rid)['state'] not in runtime.LIVE
    assert (text(host, 'a.txt'), text(host, 'b.txt'), text(host, 'c.txt')) == ('two\n', 'BEE\n', 'new\n')
    return rid


def edit_action(host, rid, call_id):
    owner = host.app.store.query('SELECT action_id FROM run_action_owners WHERE run_id=? AND call_id=?', (rid, call_id))
    return owner[0]['action_id']


# 1. A two-step run rewound to step 1 -------------------------------------------------------------------

def test_a_two_step_run_rewinds_to_step_one_through_reviewed_broker_writes_and_reads_back(host, monkeypatch):
    rid = two_step_run(host)
    steps = host.app.runs.get(rid)['steps']
    assert steps >= 3
    before = snapshot(host.app)
    plan = preview(host, rid, 1)
    assert snapshot(host.app) == before, 'a preview is a read: it writes nothing'
    files = by_path(plan['files'])
    assert set(files) == {'a.txt', 'b.txt', 'c.txt'}
    assert files['a.txt']['status'] == files['b.txt']['status'] == 'revert'
    assert files['a.txt']['steps'] == files['b.txt']['steps'] == [2]
    assert files['a.txt']['current_hash'] == digest(b'two\n') and files['a.txt']['target_hash'] == digest(b'one\n')
    assert '-two\n' in files['a.txt']['diff'] and '+one\n' in files['a.txt']['diff']
    assert '-BEE\n' in files['b.txt']['diff'] and '+bee\n' in files['b.txt']['diff']
    # A rewind never deletes: the file this task created after step 1 is listed and kept.
    assert files['c.txt']['status'] == 'kept' and files['c.txt']['reason'] == 'created'
    assert files['c.txt']['detail'] == REASONS['created'] and files['c.txt']['diff'] == ''
    assert plan['effects'] == [] and plan['effects_reverted'] == 0 and plan['authorization'] == 'none'
    assert plan['counts'] == {'revert': 2, 'refused': 0, 'kept': 1, 'unchanged': 0, 'effects': 0}
    assert [t['step'] for t in plan['timeline']] == [1, 2] and plan['timeline'][1]['files'] == ['a.txt', 'b.txt', 'c.txt']
    assert plan['previous_step'] == steps and plan['reverses'] is None and plan['rewinds'] == []
    assert not any(key.startswith('_') for item in plan['files'] for key in item), 'no host-only field leaves'

    performed = []
    original = host.app.perform_action

    def spy(action, cid):
        performed.append((action['kind'], action.get('_native_reviewed'), action['payload'].get('requires_review')))
        return original(action, cid)
    monkeypatch.setattr(host.app, 'perform_action', spy)
    result = apply(host, rid, 1, plan['digest'])
    assert (text(host, 'a.txt'), text(host, 'b.txt'), text(host, 'c.txt')) == ('one\n', 'bee\n', 'new\n')
    outcome = by_path(result['files'])
    for name, content in (('a.txt', b'one\n'), ('b.txt', b'bee\n')):
        assert outcome[name]['status'] == 'reverted' and outcome[name]['verified'] is True
        assert outcome[name]['readback_hash'] == outcome[name]['target_hash'] == digest(content)
    assert outcome['c.txt']['status'] == 'kept' and outcome['c.txt']['action_id'] is None
    assert result['counts'] == {'reverted': 2, 'verified': 2, 'refused': 0, 'uncertain': 0, 'effects_not_reverted': 0}
    assert result['effects_reverted'] == 0 and result['replayed'] == 0 and result['journaled'] is True
    # Only ordinary edit actions, each staged for review and applied by the native approval path.
    assert performed == [('edit', True, True), ('edit', True, True)]
    staged = rewind_actions(host)
    assert {a['id'] for a in staged} == {outcome['a.txt']['action_id'], outcome['b.txt']['action_id']}
    for action in staged:
        payload = action['payload']
        assert action['kind'] == 'edit' and action['status'] == 'applied' and payload['requires_review'] is True
        assert payload['rewind'] == {'rewind_id': result['rewind_id'], 'run_id': rid, 'step': 1, 'previous_step': steps}
        assert payload['baseline'] == digest(payload['before'].encode()) and payload['result']['hash'] == digest(payload['after'].encode())
        assert payload['diff'].startswith('--- a/' + payload['path'])
        assert not host.app.store.query('SELECT 1 FROM run_action_owners WHERE action_id=?', (action['id'],))
    events = [json.loads(r['payload']) for r in host.app.store.query(
        "SELECT payload FROM run_events WHERE run_id=? AND kind='rewind_applied'", (rid,))]
    assert len(events) == 1 and events[0]['rewind_id'] == result['rewind_id']
    assert {f['path']: f['status'] for f in events[0]['files']} == {'a.txt': 'reverted', 'b.txt': 'reverted', 'c.txt': 'kept'}
    assert 'BEE' not in json.dumps(events[0]) and 'zero' not in json.dumps(events[0]), 'hashes only, never file text'

    again = preview(host, rid, 1)
    assert {f['path']: f['status'] for f in again['files']} == {'a.txt': 'unchanged', 'b.txt': 'unchanged', 'c.txt': 'kept'}
    assert again['counts']['revert'] == 0 and again['rewinds'][0]['rewind_id'] == result['rewind_id']


# 2. A file the person changed after the run is refused ---------------------------------------------------

def test_a_file_the_person_changed_after_the_run_is_refused_and_never_overwritten(host):
    rid = two_step_run(host)
    (host.root / 'a.txt').write_text('mine\n')
    plan = preview(host, rid, 1)
    files = by_path(plan['files'])
    assert files['a.txt']['status'] == 'refused' and files['a.txt']['reason'] == 'changed_since'
    assert files['a.txt']['current_hash'] == digest(b'mine\n') and files['a.txt']['diff'] == ''
    assert files['a.txt']['last_hash'] == digest(b'two\n')
    assert files['b.txt']['status'] == 'revert'
    result = apply(host, rid, 1, plan['digest'])
    assert text(host, 'a.txt') == 'mine\n' and text(host, 'b.txt') == 'bee\n'
    outcome = by_path(result['files'])
    assert outcome['a.txt']['status'] == 'refused' and outcome['a.txt']['action_id'] is None
    assert outcome['b.txt']['status'] == 'reverted' and outcome['b.txt']['verified'] is True
    assert {a['payload']['path'] for a in rewind_actions(host)} == {'b.txt'}


def test_a_change_after_the_preview_refuses_the_whole_apply_and_writes_nothing(host):
    rid = two_step_run(host)
    plan = preview(host, rid, 1)
    (host.root / 'b.txt').write_text('BEE and more\n')
    before = snapshot(host.app)
    with pytest.raises(PolicyError, match='changed since this preview'):
        apply(host, rid, 1, plan['digest'])
    assert snapshot(host.app) == before
    assert (text(host, 'a.txt'), text(host, 'b.txt')) == ('two\n', 'BEE and more\n')


def test_a_person_edit_between_the_runs_own_edits_is_never_overwritten(host):
    target = host.root / 'a.txt'
    state = {'done': False}

    def emit(event, data):
        if event == 'progress' and data.get('kind') == 'done' and data.get('tool') == 'Edit' and not state['done']:
            state['done'] = True
            target.write_text('mine\n')  # the person edits a.txt right after step 1's edit
    (host.root / 'a.txt').write_text('zero\n')
    (host.root / 'b.txt').write_text('bee\n')
    Script(host.app,
           [call('r1', 'Read', file_path='a.txt'), call('e1', 'Edit', file_path='a.txt', old_string='zero', new_string='one')],
           [call('r2', 'Read', file_path='a.txt'), call('e2', 'Edit', file_path='a.txt', old_string='mine', new_string='two')])
    host.app.emit = emit
    host.app.work(host.chat, 'Change a.txt twice', False, [])
    host.app.model.complete = no_model
    rid = host.app.runs.list(host.cid)[0]['id']
    assert text(host, 'a.txt') == 'two\n'
    for step in (0, 1):
        entry = by_path(preview(host, rid, step)['files'])['a.txt']
        assert entry['status'] == 'refused' and entry['reason'] == 'interleaved', step


def test_a_broker_refusal_mid_apply_is_reported_and_leaves_no_staged_rewind(host, monkeypatch):
    rid = two_step_run(host)
    plan = preview(host, rid, 1)
    original = host.app.approve

    def racing(cid, action_id):
        action = next(a for a in host.app.store.actions(cid) if a['id'] == action_id)
        if action['payload']['path'] == 'a.txt':
            (host.root / 'a.txt').write_text('raced\n')  # someone writes between staging and approval
        return original(cid, action_id)
    monkeypatch.setattr(host.app, 'approve', racing)
    outcome = by_path(apply(host, rid, 1, plan['digest'])['files'])
    assert outcome['a.txt']['status'] == 'refused' and outcome['a.txt']['verified'] is False
    assert 'changed' in outcome['a.txt']['detail'] and outcome['a.txt']['readback_hash'] == digest(b'raced\n')
    assert text(host, 'a.txt') == 'raced\n' and text(host, 'b.txt') == 'bee\n'
    statuses = {a['payload']['path']: a['status'] for a in rewind_actions(host)}
    assert statuses == {'a.txt': 'failed', 'b.txt': 'applied'}

    def refuses(cid, action_id):
        raise PolicyError('Wait for the current task to finish before approving an action.')
    steps = host.app.runs.get(rid)['steps']
    plan = preview(host, rid, steps)  # b.txt forward to BEE; a.txt has nothing of this task's after that step
    assert by_path(plan['files'])['b.txt']['status'] == 'revert' and 'a.txt' not in by_path(plan['files'])
    monkeypatch.setattr(host.app, 'approve', refuses)
    outcome = by_path(apply(host, rid, steps, plan['digest'])['files'])
    assert outcome['b.txt']['status'] == 'refused' and outcome['b.txt']['detail'].startswith('Wait for the current task')
    assert text(host, 'b.txt') == 'bee\n'
    assert not [a for a in rewind_actions(host) if a['status'] == 'pending'], 'a staged rewind never waits for a later approval'


# 3. Commands and connector effects are listed, never reverted, never re-run --------------------------------

def test_commands_and_connector_effects_are_listed_never_reverted_and_never_rerun(host, server, monkeypatch):
    url, mail = server
    app = host.app
    ran = []

    def launcher(payload, cancel):
        ran.append(payload['command'])
        (host.root / 'build.stamp').write_text('built\n')  # the command's own file effect
        return {'exit_code': 0, 'output': 'built\n'}
    monkeypatch.setattr(app_module, 'run_command', launcher)
    plugin = app.extension_add(kind='plugin', name='Mail fixture', url=url,
                               content=json.dumps({'tools': MAIL_TOOLS, 'effects': MAIL_EFFECTS}))
    app.extension_toggle(plugin, True)
    catalogue = app.extensions.get(plugin)['config']['catalogue_digest']
    (host.root / 'notes.txt').write_text('draft notes\n')
    Script(app,
           [call('r1', 'Read', file_path='notes.txt'),
            call('e1', 'Edit', file_path='notes.txt', old_string='draft', new_string='final')],
           # Phase 46 (D154): the connector's draft before the command. A command staged with nothing else awaiting
           # review ends the turn (runner.STAGED_STOP), and this fixture's continuation thread is closed, so the run
           # stages the draft first and both wait for the person together, as before
           [call('l1', 'LoadPluginTool', plugin_id=plugin, tool='save_draft', digest=catalogue)],
           [call('p1', 'PluginCall', plugin_id=plugin, tool='save_draft', digest=catalogue,
                 arguments=json.dumps({'to': [ALICE], 'subject': 'Notes', 'body': 'The notes are final.'}))],
           [call('c1', 'RunCommand', command='touch build.stamp')])
    app.work(host.chat, f'Finalize notes.txt, build it, and save a draft email to {ALICE} about it', False, [])
    app.model.complete = no_model
    rid = app.runs.list(host.cid)[0]['id']
    pending = [a for a in app.store.actions(host.cid) if a['status'] == 'pending']
    assert sorted(a['kind'] for a in pending) == ['command', 'plugin']
    for action in pending:  # the person approves both in the main window
        app.approve(host.cid, action['id'])
    assert len(ran) == 1 and ran[0].endswith('touch build.stamp') and len(tool_calls(mail, 'save_draft')) == 1
    assert text(host, 'notes.txt') == 'final notes\n' and text(host, 'build.stamp') == 'built\n'
    tools = {t['name']: t['call_id'] for t in app.runs.tools(rid)}
    command_step, connector_step = (int(tools[name].split(':')[0]) for name in ('RunCommand', 'PluginCall'))

    plan = preview(host, rid, 0)
    assert [f['path'] for f in plan['files']] == ['notes.txt'] and plan['files'][0]['status'] == 'revert'
    effects = {e['tool']: e for e in plan['effects']}
    assert set(effects) == {'RunCommand', 'PluginCall'} and plan['counts']['effects'] == 2
    command, connector = effects['RunCommand'], effects['PluginCall']
    assert (command['kind'], command['step'], command['label'], command['outcome']) == \
        ('command', command_step, 'touch build.stamp', 'occurred')
    assert (connector['kind'], connector['step'], connector['outcome']) == ('connector', connector_step, 'occurred')
    assert connector['label'] == 'Mail fixture / save_draft'
    assert (connector['connector_class'], connector['connector_state']) == ('draft', 'responded')
    for effect in plan['effects']:
        assert effect['reverted'] is False and effect['note'] == NOTES[effect['kind']] and effect['note'].startswith('Not reverted')
    assert {t['step']: t['effects'] for t in plan['timeline']}[connector_step] == 1

    performed = []
    original = app.perform_action
    monkeypatch.setattr(app, 'perform_action', lambda action, cid: performed.append(action['kind']) or original(action, cid))
    result = apply(host, rid, 0, plan['digest'])
    assert text(host, 'notes.txt') == 'draft notes\n'
    assert text(host, 'build.stamp') == 'built\n', 'a command\'s own file effect is not reverted'
    assert len(ran) == 1 and len(tool_calls(mail, 'save_draft')) == 1, 'nothing ran again'
    assert performed == ['edit'] and result['effects_reverted'] == 0 and result['replayed'] == 0
    assert [e['tool'] for e in result['effects']] == ['PluginCall', 'RunCommand']
    assert all(e['reverted'] is False for e in result['effects']) and result['counts']['effects_not_reverted'] == 2

    # Only effects after the chosen step are in the rewound span.
    assert [e['tool'] for e in preview(host, rid, connector_step)['effects']] == ['RunCommand']
    assert preview(host, rid, command_step)['effects'] == []


def test_every_effect_tool_has_a_ledger_kind_and_a_not_reverted_note():
    assert set(runtime.EFFECTS) - FILE_TOOLS <= set(KINDS), sorted(set(runtime.EFFECTS) - FILE_TOOLS - set(KINDS))
    assert FILE_TOOLS - {'IntegrateAgentPatch', 'IntegrateWorktreeFile'} == {'Write', 'Append', 'Edit'}
    assert set(KINDS.values()) == set(NOTES) and all(note.startswith('Not reverted.') for note in NOTES.values())


# 4. Rewind of a rewind ------------------------------------------------------------------------------------

def test_a_rewind_can_itself_be_rewound_and_each_lands_exactly(host):
    rid = two_step_run(host)
    steps = host.app.runs.get(rid)['steps']
    first = apply(host, rid, 1, preview(host, rid, 1)['digest'])
    assert (text(host, 'a.txt'), text(host, 'b.txt')) == ('one\n', 'bee\n')

    # Undoing the rewind restores the state it started from: the run's final step.
    back = preview(host, rid, steps)
    assert back['reverses'] == first['rewind_id'] and back['previous_step'] == 1
    files = by_path(back['files'])
    assert set(files) == {'a.txt', 'b.txt'}, 'c.txt was never rewound, so nothing to restore'
    assert files['a.txt']['status'] == 'revert' and files['a.txt']['target_hash'] == digest(b'two\n')
    second = apply(host, rid, steps, back['digest'])
    assert (text(host, 'a.txt'), text(host, 'b.txt'), text(host, 'c.txt')) == ('two\n', 'BEE\n', 'new\n')
    assert all(f['verified'] for f in second['files'])
    history = preview(host, rid, steps)['rewinds']
    assert [h['rewind_id'] for h in history] == [first['rewind_id'], second['rewind_id']]
    assert history[1]['reverses'] == first['rewind_id'] and history[1]['applied'] == 2

    # The whole chain is checkpointed: before the task, then forward to step 1 again.
    whole = preview(host, rid, 0)
    assert {f['path']: f['status'] for f in whole['files']} == {'a.txt': 'revert', 'b.txt': 'revert', 'c.txt': 'kept'}
    apply(host, rid, 0, whole['digest'])
    assert (text(host, 'a.txt'), text(host, 'b.txt'), text(host, 'c.txt')) == ('zero\n', 'bee\n', 'new\n')
    forward = preview(host, rid, 1)
    assert by_path(forward['files'])['a.txt']['status'] == 'revert' and by_path(forward['files'])['b.txt']['status'] == 'unchanged'
    apply(host, rid, 1, forward['digest'])
    assert (text(host, 'a.txt'), text(host, 'b.txt')) == ('one\n', 'bee\n')
    assert len({a['payload']['rewind']['rewind_id'] for a in rewind_actions(host)}) == 4


def test_a_per_action_undo_is_part_of_the_checkpoint(host):
    rid = two_step_run(host)
    host.app.undo(host.cid, edit_action(host, rid, '2:e2'))
    assert text(host, 'a.txt') == 'one\n'
    assert by_path(preview(host, rid, 1)['files'])['a.txt']['status'] == 'unchanged'
    whole = preview(host, rid, 0)
    assert by_path(whole['files'])['a.txt']['status'] == 'revert'
    apply(host, rid, 0, whole['digest'])
    assert text(host, 'a.txt') == 'zero\n'


# 5. The pet, generated views and models cannot reach the routes --------------------------------------------

def test_the_pet_cannot_reach_rewind_and_neither_can_a_model_or_a_generated_view(host):
    rid = two_step_run(host)
    value = preview(host, rid, 1)['digest']
    before = snapshot(host.app)
    for surface in (None, 'pet', 'Main', 'main ', 'generated', '', 'view'):
        with pytest.raises(PolicyError, match='main window'):
            preview(host, rid, 1, surface=surface)
        with pytest.raises(PolicyError, match='main window'):
            apply(host, rid, 1, value, surface=surface)  # a real main-window digest does not help
    assert snapshot(host.app) == before and (text(host, 'a.txt'), text(host, 'b.txt')) == ('two\n', 'BEE\n')

    from pebble.tool_catalogue import TOOLS
    assert not {t['function']['name'].casefold() for t in TOOLS} & {r.casefold() for r in ROUTES}, 'no model tool'
    for name in ('Pet.vue', 'components/PetRunSummary.vue'):
        pet_source = (ROOT / 'src' / name).read_text()
        assert 'RewindPanel' not in pet_source and 'rewind_' not in pet_source, name
    # Merge (i20): parent patches P4 (tool_authz.NATIVE_ONLY) and P5 (main.rs) landed, so the real files carry the
    # wiring and every gate below applies to them; the copy-based check of the proposed patch is no longer needed.
    assert set(ROUTES) <= tool_authz.NATIVE_ONLY
    shell_gates(MAIN_RS.read_text(), tool_authz.NATIVE_ONLY)


def shell_gates(source, native_only):
    """Window gates for the rewind routes: never the pet or a generated view, never a model actor."""
    rpc = source[source.index('async fn rpc('):source.index('fn compact(')]
    pet = rust_list(source, r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains')
    assert not pet & set(ROUTES), 'the pet allowlist never names a rewind route'
    # Generated windows are refused before any route list (Phase 19 G1 keeps this order).
    assert rpc.index('if !["main","pet"].contains(&window.label())') < rpc.index('if !ALLOWED.contains(&method.as_str())')
    if not set(ROUTES) <= native_only:
        return  # before the parent's wiring patch: neither window can reach the routes at all
    assert set(ROUTES) <= rust_list(source, r'const ALLOWED: &\[&str\] = &\[(.*?)\];')
    assert not set(ROUTES) & rust_list(source, r'const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];')
    inject = next(line for line in rpc.splitlines() if 'map.insert("surface"' in line)
    assert all(f'method=="{name}"' in inject for name in ROUTES), 'the shell writes surface for both routes'
    assert rpc.index('if !ALLOWED.contains(&method.as_str())') < rpc.index(inject) < rpc.index('backend.call(&method')
    child = Actor.child('a' * 32, 'b' * 32, 1)
    for name in ROUTES:
        for spelled in (name, name.upper()):
            for actor in (Actor.model(), Actor.root('a' * 32), child):
                decision = tool_authz.check(tool=spelled, args={}, actor=actor, run_config={}, catalogue_digest='0' * 64,
                                            policy_version=0, admitted=lambda _, n=spelled: {n}, layers=('actor',),
                                            native_only=native_only)
                assert not decision.allowed and decision.refusal_layer == 'actor', (spelled, actor)


# Refusals and invariants -----------------------------------------------------------------------------------

def test_scope_state_and_argument_refusals(host):
    rid = two_step_run(host)
    steps = host.app.runs.get(rid)['steps']
    for step in (-1, steps + 1, True, '1', 1.0, None):
        with pytest.raises(PolicyError, match='Choose a step'):
            preview(host, rid, step)
    other = host.app.new_chat(str(host.root))['id']
    with pytest.raises(ValueError, match='not found in this conversation'):
        route(host, 'rewind_preview', chat_id=other, run_id=rid, step=1, surface='main')
    for bad in (None, '', 'f' * 63, 'F' * 64, 'x' * 64):
        with pytest.raises(PolicyError, match='Preview the rewind'):
            apply(host, rid, 1, bad)
    value = preview(host, rid, 1)['digest']
    host.app.active = host.cid
    try:
        with pytest.raises(PolicyError, match='current task'):
            apply(host, rid, 1, value)
    finally:
        host.app.active = None
    live = host.app.runs.create(host.cid, 'Another task', host.app.runtime_config(host.chat, False))
    host.app.runs.transition(live, 'running')
    try:
        with pytest.raises(PolicyError, match='Pause or stop'):
            preview(host, live, 0)
    finally:
        host.app.runs.transition(live, 'stopped')
    assert (text(host, 'a.txt'), text(host, 'b.txt')) == ('two\n', 'BEE\n') and not rewind_actions(host)


def test_disabled_edits_refuse_every_file_and_write_nothing(host):
    rid = two_step_run(host)
    tools = host.app.store.setting('permissions', DEFAULT)['tools']
    host.app.setting('permissions', {'mode': 'auto', 'tools': {**tools, 'edits': False}})
    plan = preview(host, rid, 1)
    assert {f['path']: f['reason'] for f in plan['files']} == {'a.txt': 'edits_disabled', 'b.txt': 'edits_disabled', 'c.txt': 'created'}
    result = apply(host, rid, 1, plan['digest'])
    assert result['counts']['reverted'] == 0 and not rewind_actions(host)
    assert (text(host, 'a.txt'), text(host, 'b.txt')) == ('two\n', 'BEE\n')


def test_the_brokers_before_tool_hook_gates_every_rewind_write(host, monkeypatch):
    rid = two_step_run(host)
    plan = preview(host, rid, 1)
    seen, original = [], host.app.hook_event

    def hook_event(event, text='', data=None, run_id=None):
        if event == 'before_tool':
            seen.append(data['tool'])
            if '"path": "a.txt"' in text:
                raise PolicyError('Blocked by hook: fixture')
        return original(event, text, data, run_id)
    monkeypatch.setattr(host.app, 'hook_event', hook_event)
    outcome = by_path(apply(host, rid, 1, plan['digest'])['files'])
    assert seen == ['Write', 'Write'], 'each rewind write is admitted as an ordinary broker Write'
    assert outcome['a.txt']['status'] == 'refused' and outcome['a.txt']['detail'] == 'Blocked by hook: fixture'
    assert outcome['b.txt']['status'] == 'reverted' and (text(host, 'a.txt'), text(host, 'b.txt')) == ('two\n', 'bee\n')


def test_a_pending_edit_to_the_same_file_blocks_only_that_file(host):
    rid = two_step_run(host)
    tools = host.app.store.setting('permissions', DEFAULT)['tools']
    host.app.setting('permissions', {'mode': 'ask', 'tools': tools})
    from pebble.policy import Workspace
    host.app.propose(host.cid, 'edit', Workspace(str(host.root)).draft('a.txt', 'proposed\n'))
    files = by_path(preview(host, rid, 1)['files'])
    assert files['a.txt']['reason'] == 'pending_edit' and files['b.txt']['status'] == 'revert'


def test_construction_creates_no_table_and_the_routes_are_two(tmp_path):
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        tables = snapshot(app)
        rewinds = Rewinds(app)
        assert snapshot(app) == tables and set(rewinds.methods()) == set(ROUTES) == {'rewind_preview', 'rewind_apply'}
    finally:
        app.close()
        close_guard_databases(tmp_path)  # resource cleanup only (F21-fd)
