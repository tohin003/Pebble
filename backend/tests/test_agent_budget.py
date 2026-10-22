"""Shared policy receipt caps stop work before a new marker or provider request."""

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from pebble.agent_manager import Agents, AgentBudgetExhausted
from pebble.agent_tree import ToolBudgetExhausted
from pebble.app import App
from pebble.policy import Workspace


PROFILE = {'id': 'budget-fixture', 'kind': 'openai', 'name': 'Budget fixture',
           'url': 'https://unused.invalid', 'model': 'fixture', 'context': 8192,
           'budget': 4096, 'tools': True}


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Exercise the checked-in App/Tree without dispatching unscripted child
    # providers. This tests admission, not the separately tested scheduler.
    monkeypatch.setattr(Agents, 'start', lambda self: None)
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'source.py').write_text('value = 1\n')
    monkeypatch.setattr(value.model, 'current', lambda: copy.deepcopy(PROFILE))
    monkeypatch.setattr(value.guard, 'handle', lambda *args, **kwargs: {'action': 'allow'})
    value.budget_project = project
    yield value
    value.active = value.active_run = None
    value.close()


def root(app, steps=16):
    chat = app.new_chat(str(app.budget_project))
    config = app.runtime_config(chat, False, {'max_steps': steps, 'max_seconds': 180})
    rid = app.runs.create(chat['id'], 'Inspect the source receipt budget.', config)
    app.runs.transition(rid, 'running')
    app.active, app.active_run = chat['id'], rid
    app.task_policy = copy.deepcopy(config['permissions'])
    return chat, rid


def child(app, cid, rid):
    node = app.agents.tree.spawn(cid, rid, None, 'Read source.py', 'reader', ['Read'],
                                  {'max_steps': 1, 'max_seconds': 10, 'model_tokens': 8192})
    app.agents.tree.claim(cid, rid, node['id'], 1, 'budget-worker')
    return node


def child_reads(app, cid, rid, node, count, start=0, generation=1):
    for index in range(start, start + count):
        call_id = 'read-' + str(index)
        app.agents.tree.begin_tool(cid, rid, node['id'], generation, 'budget-worker',
                                    call_id, 'Read', {'file_path': 'source.py'})
        app.agents.tree.end_tool(cid, rid, node['id'], generation, 'budget-worker',
                                  call_id, {'path': 'source.py', 'status': 'read', 'content': 'value = 1'})


def planned_reads(app, rid, count):
    calls = [{'id': 'planned-' + str(i), 'type': 'function',
              'function': {'name': 'Read', 'arguments': json.dumps({'file_path': 'source.py'})}}
             for i in range(count)]
    app.runs.response(rid, {'role': 'assistant', 'content': None, 'tool_calls': calls},
                      {'messages': []}, steps=1, elapsed=0)


def test_child_cap_denies_ninth_read_before_creating_marker(app):
    chat, rid = root(app)
    node = child(app, chat['id'], rid)
    child_reads(app, chat['id'], rid, node, 8)
    with pytest.raises(ToolBudgetExhausted, match='receipt budget'):
        app.agents.tree.begin_tool(chat['id'], rid, node['id'], 1, 'budget-worker',
                                    'read-8', 'Read', {'file_path': 'source.py'})
    receipts = app.agents.tree.receipts(chat['id'], rid, node['id'], 1)
    assert len(receipts) == 8 and all(r['state'] == 'completed' for r in receipts)
    assert not app.store.query('SELECT 1 FROM agent_receipts WHERE call_id=?', ('read-8',))


def test_child_completed_replay_remains_allowed_at_cap_without_increment(app):
    chat, rid = root(app)
    node = child(app, chat['id'], rid)
    child_reads(app, chat['id'], rid, node, 8)
    replay = app.agents.tree.begin_tool(chat['id'], rid, node['id'], 1, 'budget-worker',
                                        'read-0', 'Read', {'file_path': 'source.py'})
    assert replay['content'] == 'value = 1'
    assert len(app.agents.tree.receipts(chat['id'], rid, node['id'], 1)) == 8


def test_child_tool_cap_survives_explicit_generation_retry(app):
    chat, rid = root(app)
    node = child(app, chat['id'], rid)
    child_reads(app, chat['id'], rid, node, 8)
    app.agents.tree.release(chat['id'], rid, node['id'], 1, 'budget-worker')
    new = app.agents.tree.retry(chat['id'], rid, rid, node['id'])
    app.agents.tree.claim(chat['id'], rid, new['id'], 2, 'budget-worker')
    with pytest.raises(ToolBudgetExhausted):
        app.agents.tree.begin_tool(chat['id'], rid, new['id'], 2, 'budget-worker',
                                    'new-generation-read', 'Read', {'file_path': 'source.py'})
    assert not app.agents.tree.receipts(chat['id'], rid, new['id'], 2)
    assert app.store.query('SELECT count(*) AS n FROM agent_receipts')[0]['n'] == 8


def test_shared_root_and_child_counts_deny_fresh_and_planned_root_under_lock(app):
    chat, rid = root(app, steps=3)
    node = child(app, chat['id'], rid)
    child_reads(app, chat['id'], rid, node, 4)
    ws = Workspace(app.budget_project)
    for index in range(20):
        app.execute('Read', {'file_path': 'source.py'}, ws, chat['id'], call_id='root-' + str(index))
    planned_reads(app, rid, 20)
    with app.store.lock:
        for call_id in ('fresh-root', '1:planned-0'):
            with pytest.raises(AgentBudgetExhausted, match='shared tool receipt budget'):
                app.agents.tool_budget(rid, call_id)
            with pytest.raises(AgentBudgetExhausted):
                app.execute('Read', {'file_path': 'source.py'}, ws, chat['id'], call_id=call_id)
    assert not app.store.query('SELECT 1 FROM run_tools WHERE call_id=?', ('fresh-root',))
    assert app.store.query('SELECT state FROM run_tools WHERE call_id=?', ('1:planned-0',))[0]['state'] == 'planned'
    assert app.store.query("SELECT count(*) AS n FROM run_tools WHERE run_id=? AND state!='planned'", (rid,))[0]['n'] == 20
    assert len(app.agents.tree.receipts(chat['id'], rid, node['id'], 1)) == 4


def test_many_planned_proposals_do_not_consume_admitted_tool_budget(app):
    chat, rid = root(app, steps=3)
    node = child(app, chat['id'], rid)
    child_reads(app, chat['id'], rid, node, 4)
    planned_reads(app, rid, 30)
    ws = Workspace(app.budget_project)
    with app.store.lock:
        for index in range(4):
            call_id = '1:planned-' + str(index)
            app.agents.tool_budget(rid, call_id)
            assert app.execute('Read', {'file_path': 'source.py'}, ws, chat['id'], call_id=call_id)['receipt_id'] == call_id
    assert app.store.query("SELECT count(*) AS n FROM run_tools WHERE state='planned'")[0]['n'] == 26
    assert app.store.query("SELECT count(*) AS n FROM run_tools WHERE state='completed'")[0]['n'] == 4


def test_root_completed_replay_is_cached_at_shared_cap(app, monkeypatch):
    chat, rid = root(app, steps=3)
    node = child(app, chat['id'], rid)
    child_reads(app, chat['id'], rid, node, 4)
    ws = Workspace(app.budget_project)
    originals = [app.execute('Read', {'file_path': 'source.py'}, ws, chat['id'], call_id='root-' + str(i))
                 for i in range(20)]
    def forbidden(*args, **kwargs):
        pytest.fail('A completed receipt replay must not execute its tool again.')
    monkeypatch.setattr(app, '_execute', forbidden)
    with app.store.lock:
        assert app.execute('Read', {'file_path': 'source.py'}, ws, chat['id'], call_id='root-0') == originals[0]
    assert app.store.query("SELECT count(*) AS n FROM run_tools WHERE state!='planned'")[0]['n'] == 20


def test_shared_root_receipts_also_limit_new_child_markers(app):
    chat, rid = root(app, steps=3)
    node = child(app, chat['id'], rid)
    ws = Workspace(app.budget_project)
    for index in range(8):
        app.execute('Read', {'file_path': 'source.py'}, ws, chat['id'], call_id='root-' + str(index))
    with pytest.raises(ToolBudgetExhausted):
        app.agents.tree.begin_tool(chat['id'], rid, node['id'], 1, 'budget-worker',
                                    'first-child-read', 'Read', {'file_path': 'source.py'})
    assert not app.agents.tree.receipts(chat['id'], rid, node['id'], 1)


def test_root_model_budget_denial_settles_budget_exhausted_without_provider_call(app, monkeypatch):
    chat, rid = root(app, steps=1)
    app.agents.tree.budget(chat['id'], rid)
    for index in range(3):
        dispatch = 'failed-' + str(index)
        assert app.agents.tree.reserve_model(chat['id'], rid, rid, 0, 1024, 0, dispatch)['admitted']
        app.agents.tree.finish_model(chat['id'], rid, rid, 0, dispatch, failed=True)
    requests = []
    def complete(*args, **kwargs):
        requests.append(True)
        pytest.fail('The exhausted root must not dispatch another provider request.')
    monkeypatch.setattr(app.model, 'complete', complete)
    app.work(chat, 'Inspect the source receipt budget.', False, [], run_id=rid)
    run = app.runs.get(rid, chat['id'])
    assert run['state'] == 'budget_exhausted'
    assert 'shared agent tree exhausted its model budget' in run['error']
    assert requests == []


def test_root_tool_budget_denial_does_not_enter_invalid_tool_recovery(app, monkeypatch):
    chat, rid = root(app, steps=3)
    node = child(app, chat['id'], rid)
    child_reads(app, chat['id'], rid, node, 8)
    ws = Workspace(app.budget_project)
    for index in range(16):
        app.execute('Read', {'file_path': 'source.py'}, ws, chat['id'], call_id='prior-root-' + str(index))
    calls = []
    def complete(*args, **kwargs):
        calls.append(True)
        return {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'over-budget', 'type': 'function', 'function': {
                'name': 'Read', 'arguments': json.dumps({'file_path': 'source.py'})}}]}
    monkeypatch.setattr(app.model, 'complete', complete)
    app.work(chat, 'Inspect the source receipt budget.', False, [], run_id=rid)
    run = app.runs.get(rid, chat['id'])
    assert run['state'] == 'budget_exhausted' and len(calls) == 1
    assert app.store.query('SELECT state FROM run_tools WHERE call_id=?', ('1:over-budget',))[0]['state'] == 'planned'
    assert app.store.query("SELECT count(*) AS n FROM run_tools WHERE state!='planned'")[0]['n'] == 16


def test_competing_root_and_child_admissions_cannot_both_take_last_shared_slot(app):
    chat, rid = root(app, steps=3)
    node = child(app, chat['id'], rid)
    child_reads(app, chat['id'], rid, node, 7)
    for index in range(16):
        app.execute('Read', {'file_path': 'source.py'}, Workspace(app.budget_project), chat['id'], call_id='prior-root-' + str(index))
    barrier, winners, denials = threading.Barrier(3), [], []
    def take_root():
        barrier.wait()
        try:
            app.execute('Read', {'file_path': 'source.py'}, Workspace(app.budget_project), chat['id'], call_id='last-root')
            winners.append('root')
        except AgentBudgetExhausted:
            denials.append('root')
    def take_child():
        barrier.wait()
        try:
            child_reads(app, chat['id'], rid, node, 1, start=7)
            winners.append('child')
        except ToolBudgetExhausted:
            denials.append('child')
    threads = [threading.Thread(target=take_root), threading.Thread(target=take_child)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert len(winners) == len(denials) == 1
    shared_count = app.store.query('SELECT count(*) AS n FROM agent_receipts')[0]['n']
    shared_count += app.store.query("SELECT count(*) AS n FROM run_tools WHERE state!='planned'")[0]['n']
    assert shared_count == 24
    assert winners == ['root'] and denials == ['child']


def test_real_child_executor_stops_before_ninth_call_in_one_model_response(app):
    chat, rid = root(app)
    node = child(app, chat['id'], rid)
    requests, closed = [], []
    calls = [{'id': 'batch-' + str(i), 'type': 'function', 'function': {
        'name': 'Read', 'arguments': json.dumps({'file_path': 'source.py'})}} for i in range(9)]
    def complete(*args):
        requests.append(True)
        return {'role': 'assistant', 'content': None, 'tool_calls': calls}
    provider = SimpleNamespace(complete=complete, cancel=lambda: None,
                               close=lambda: closed.append(True), last_usage={})
    app.agents.provider_factory = lambda *args, **kwargs: provider
    worker = {'cid': chat['id'], 'rid': rid, 'id': node['id'], 'generation': 1,
              'owner': 'budget-worker', 'cancel': threading.Event(), 'suspend': False}
    app.agents.drive(worker)
    record = app.agents.tree.get(chat['id'], rid, node['id'])
    assert record['status'] == 'budget_exhausted'
    assert 'tool receipt budget' in record['result']['error']
    assert requests == closed == [True]
    receipts = app.agents.tree.receipts(chat['id'], rid, node['id'], 1)
    assert len(receipts) == 8 and all(r['state'] == 'completed' for r in receipts)
    assert not app.store.query('SELECT 1 FROM agent_receipts WHERE call_id=?', ('1:batch-8',))
