"""Phase 46 (1.9.28, golden held-out parallel_work: one sol cell ended in Pebble's own `interrupted` state, "The action
result is saved, but its follow-up update failed"; b-new F1a, F1b).

An approved action's receipt enters the run's inbox as a host input, which RunInteractions._input stored under 16,000
bytes or refused. A reviewed command (in 1.9.27's Auto mode every command outside the eleven read-only executables got a
card; since D184 one confined to the project runs without one, so these runs keep the card with PEBBLE_AUTO_RISKY_ONLY=0)
keeps 16,000 bytes of output, so one that printed more than about 15,600 bytes failed after its receipt was committed
and App._approve ended the run `interrupted` (offline: a 190-line output did). A host or agent input now keeps the
receipt whole when it fits and is shortened to fit otherwise (the whole result stays in the tool receipt and in HOST
ACTION UPDATES, which the resumed run reads); a person's own input stays exact. Each follow-up of an approval that
fails is noted and the run continues; only a resolution that cannot be recorded even as the receipt's identity ends it
`interrupted`, as test_agent_effects' test_failed_follow_up_after_native_commit_preserves_the_physical_receipt pins.
Scripted root model through the real App, broker, sandboxed terminal and driver loop (pebble_worker._decide's shape).
"""
import json
import sys
import time
from types import SimpleNamespace

import pytest

from pebble import evaluation
from pebble.app import App
from pebble.interaction import INPUT_BYTES, RunInteractions, fitted

macos = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox')
GEN = "for i in range({lines}):\n    print('service-%04d: debug=off tls=on timeout=30 ' % i + 'x' * 40)\n"


@pytest.fixture(autouse=True)
def card(monkeypatch):
    """Phase 46 (1.9.28, D184): a confined command runs without a card in Auto now; these runs test the approval path
    every step that still asks takes, so they keep the command's card."""
    monkeypatch.setenv('PEBBLE_AUTO_RISKY_ONLY', '0')


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def approved_run(tmp_path, lines, patch=None):
    """A scripted root writes gen.py, runs `python3 gen.py` (a card in Auto mode, approved as the golden driver does)
    and answers. Returns (run, approve outcomes, inbox rows, tool receipts, notes, the last request's messages)."""
    project = tmp_path / 'project'
    project.mkdir()
    steps = [call('Write', {'file_path': 'gen.py', 'content': GEN.format(lines=lines)}, 'w1'),
             call('RunCommand', {'command': 'python3 gen.py'}, 'c1'),
             {'role': 'assistant', 'content': 'Ran gen.py.\nFINAL: done'}]
    seen = []

    def script(request):
        seen.append(request.messages)
        return steps[request.step - 1] if request.step <= len(steps) else {'role': 'assistant', 'content': 'FINAL: done'}
    app = App(tmp_path / 'home', tmp_path / 'resources', lambda *a, **k: None)
    try:
        evaluation._install_scripted(app, SimpleNamespace(id='approval-1928'), script)
        tools = dict.fromkeys(('files', 'edits', 'tests', 'apps', 'web', 'terminal', 'plugins', 'views', 'screen',
                               'capture'), False)
        tools.update(files=True, edits=True, tests=True, terminal=True)
        app.setting('permissions', {'mode': 'auto', 'tools': tools})
        if patch:
            patch(app)
        cid = app.new_chat(str(project))['id']
        rid = app.send(cid, 'Run gen.py and tell me how many lines it printed. End with FINAL: done.',
                       limits={'max_steps': 20, 'max_seconds': 120})['run_id']
        outcomes, deadline = [], time.monotonic() + 90
        while time.monotonic() < deadline:
            run = app.runs.get(rid, cid)
            if app.active is None:
                continuation = app.interactions.continuation(rid)
                if not (continuation and continuation['state'] in {'queued', 'dispatching'}) \
                        and run['state'] == 'waiting_approval':
                    for action in app.store.actions(cid):
                        if action['status'] == 'pending':
                            try:
                                app.approve(cid, action['id'])
                                outcomes.append('approved')
                            except Exception as exc:  # pebble_worker._decide swallows it
                                outcomes.append(type(exc).__name__ + ': ' + str(exc))
                elif run['state'] in evaluation.FINAL_STATES and not app.agents.busy():
                    break
            time.sleep(0.05)
        inbox = app.store.query('SELECT kind,origin,payload FROM run_inbox WHERE run_id=?', (rid,))
        notes = [(e['kind'], e['payload']) for e in app.runs.events(rid, 0, 200)
                 if e['kind'] == 'approval_follow_up_failed']
        return app.runs.get(rid, cid), outcomes, inbox, app.runs.tools(rid), notes, seen[-1]
    finally:
        app.active = app.active_run = None
        app.close()


# -- the inbox copy --------------------------------------------------------------------------------------------------

def test_a_receipt_that_fits_is_kept_byte_for_byte():
    receipt = {'action_id': 'a' * 32, 'kind': 'command', 'status': 'applied', 'receipt_id': '1:c1',
               'result': {'exit_code': 0, 'output': 'Ran 1 test\n\nOK\n'}}
    assert fitted(receipt) is receipt
    assert fitted([1, 2, 3]) == [1, 2, 3]


@pytest.mark.parametrize('output', ['x' * 40_000, 'é€' * 9_000, '\n'.join(f'line {n}' for n in range(5_000))])
def test_a_long_result_keeps_its_start_end_identity_and_exit_code(output):
    receipt = {'action_id': 'b' * 32, 'kind': 'command', 'status': 'applied', 'receipt_id': '1:c1',
               'result': {'exit_code': 3, 'output': output, 'command': 'python3 gen.py'}}
    copy = fitted(receipt)
    assert size(copy) <= INPUT_BYTES and copy['shortened_for_input'] is True
    assert {k: copy[k] for k in ('action_id', 'kind', 'status', 'receipt_id')} == \
        {k: receipt[k] for k in ('action_id', 'kind', 'status', 'receipt_id')}
    assert copy['result']['exit_code'] == 3 and copy['result']['command'] == 'python3 gen.py'
    text = copy['result']['output']
    assert text.startswith(output[:200]) and text.endswith(output[-200:]) and 'HOST ACTION UPDATES' in text


def test_a_long_list_and_a_record_with_nothing_to_cut_still_fit():
    paths = {'action_id': 'c' * 32, 'status': 'applied', 'result': {'paths': [f'build/obj/{n}.o' for n in range(5_000)]}}
    copy = fitted(paths)
    assert size(copy) <= INPUT_BYTES and copy['result']['paths'][0] == 'build/obj/0.o'
    assert copy['result']['paths'][-1] == 'build/obj/4999.o' and any('items left out' in p for p in copy['result']['paths'])
    many = {'action_id': 'd' * 32, 'status': 'applied', **{f'k{n}': {'v': n} for n in range(3_000)}}
    assert fitted(many) == {'action_id': 'd' * 32, 'status': 'applied', 'shortened_for_input': True}


def test_only_host_and_agent_inputs_are_shortened_a_persons_stay_exact(tmp_path):
    app = App(tmp_path / 'home', tmp_path / 'resources', lambda *a, **k: None)
    try:
        with app.store.lock, app.store.db as db:
            ident = RunInteractions._input(db, 'run-1', 'action_resolution', 'host',
                                           {'action_id': 'e' * 32, 'status': 'applied', 'result': {'output': 'y' * 30_000}})
            stored = json.loads(db.execute('SELECT payload FROM run_inbox WHERE id=?', (ident,)).fetchone()[0])
            assert stored['shortened_for_input'] is True and len(stored['result']['output']) < 16_000
            # the same input again is the same stored copy (the identifier check compares the stored text)
            assert RunInteractions._input(db, 'run-1', 'action_resolution', 'host', {
                'action_id': 'e' * 32, 'status': 'applied', 'result': {'output': 'y' * 30_000}}, ident) == ident
        with pytest.raises(ValueError, match='storage limit'):
            with app.store.lock, app.store.db as db:
                RunInteractions._input(db, 'run-1', 'steering', 'human', {'text': '€' * 7_000})
    finally:
        app.close()


# -- the approval path -----------------------------------------------------------------------------------------------

@macos
def test_an_approved_command_that_printed_more_than_the_inbox_holds_completes(tmp_path):
    run, outcomes, inbox, receipts, notes, last = approved_run(tmp_path, 400)
    assert run['state'] == 'completed' and outcomes == ['approved'] and notes == [], run['error']
    command = next(r for r in receipts if r['name'] == 'RunCommand')
    assert len(command['result']['result']['output'].encode()) == 16_000  # the tool receipt stays whole
    [(kind, origin, payload)] = [(r['kind'], r['origin'], r['payload']) for r in inbox]
    assert (kind, origin) == ('action_resolution', 'host') and len(payload.encode()) <= INPUT_BYTES
    assert json.loads(payload)['shortened_for_input'] is True
    # the resumed request reads the whole result in HOST ACTION UPDATES, and the shortened copy as the host input
    updates = next(m['content'] for m in last if str(m.get('content', '')).startswith('HOST ACTION UPDATES'))
    assert 'service-0190' in updates
    host_input = next(m['content'] for m in last if str(m.get('content', '')).startswith('HOST INPUT'))
    assert 'left out of this copy' in host_input and 'service-0001' in host_input


@macos
def test_a_failed_summary_or_checkpoint_after_the_commit_is_noted_and_the_run_continues(tmp_path):
    def patch(app):
        def broken(*args, **kwargs):
            raise RuntimeError('fixture follow-up failed')
        app.action_summary = broken
        app.update_checkpoint = broken
    run, outcomes, _, _, notes, _ = approved_run(tmp_path, 20, patch)
    assert run['state'] == 'completed' and outcomes == ['approved'], run['error']
    assert [p['step'] for _, p in notes] == ['summary', 'checkpoint']
    assert all(p['type'] == 'RuntimeError' for _, p in notes)


@macos
def test_a_failed_resolution_is_recorded_again_as_the_receipts_identity(tmp_path):
    def patch(app):
        original, calls = app.interactions.action_resolution, []

        def once(cid, aid, receipt):
            calls.append(receipt)
            if len(calls) == 1:
                raise RuntimeError('fixture resolution failed')
            return original(cid, aid, receipt)
        app.interactions.action_resolution = once
    run, outcomes, inbox, receipts, notes, _ = approved_run(tmp_path, 20, patch)
    assert run['state'] == 'completed' and outcomes == ['approved'], run['error']
    assert [p['step'] for _, p in notes] == ['resolution']
    payload = json.loads(inbox[0]['payload'])
    assert set(payload) == {'action_id', 'kind', 'status', 'receipt_id'} and payload['status'] == 'applied'
    command = next(r for r in receipts if r['name'] == 'RunCommand')
    assert 'service-0019' in command['result']['result']['output']  # the receipt itself was never touched
