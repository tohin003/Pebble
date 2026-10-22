"""Host root completion reserve, fitted child limits and content-free delegation advice.

Tree tests use the real SQLite ledger. The manager test drives the real child
executor synchronously with a scripted transport. No model HTTP or billing.
"""

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from pebble.agent_manager import Agents, AgentBudgetExhausted
from pebble.agent_tree import DENIALS, ToolBudgetExhausted, Tree
from pebble.app import App
from pebble.delegation_policy import (UnfitLimits, delegation_advice, reserved_steps, root_reserve,
                                      schema_note)
from pebble.interaction import RunInteractions
from pebble.permissions import DEFAULT
from pebble.runtime import RunJournal
from pebble.store import Store


WINDOW = 4096
LIMIT_KEYS = ('max_steps', 'max_seconds', 'model_tokens')


@pytest.fixture
def env(tmp_path):
    home = tmp_path / 'data'
    store = Store(home)
    runs = RunJournal(store, home)
    app = SimpleNamespace(store=store, runs=runs, interactions=RunInteractions(store, runs))
    app.tree = Tree(app)
    yield SimpleNamespace(app=app, tree=app.tree, tmp=tmp_path)
    runs.close()
    store.close()


def start(env, steps=12, seconds=300):
    cid = env.app.store.new_chat(str(env.tmp))
    config = {'workspace': str(env.tmp), 'permissions': copy.deepcopy(DEFAULT), 'route': {'context': WINDOW},
              'limits': {'max_steps': steps, 'max_seconds': seconds}}
    rid = env.app.runs.create(cid, 'Inspect source', config)
    env.app.runs.transition(rid, 'running')
    return cid, rid


def owner(ident):
    return 'owner-' + ident


def spawn(env, cid, rid, limits=None, parent=None, tools=('Read',), name='worker'):
    lease = {'parent_generation': 1, 'parent_owner': owner(parent)} if parent else {}
    return env.tree.spawn(cid, rid, parent, 'Read source', name, list(tools),
                          {} if limits is None else dict(limits), **lease)


def claim(env, cid, rid, node, generation=1):
    return env.tree.claim(cid, rid, node['id'], generation, owner(node['id']))


def turn(env, cid, rid, actor, ident, generation=None, finish=True):
    """One model dispatch reserved at the pinned window."""
    generation = 0 if actor == rid else (generation or 1)
    admission = env.tree.reserve_model(cid, rid, actor, generation, WINDOW, 0, ident)
    if admission['admitted'] and finish:
        env.tree.finish_model(cid, rid, actor, generation, ident)
    return admission


def root_turns(env, cid, rid, names):
    return [turn(env, cid, rid, rid, name)['admitted'] for name in names]


REVIEW = ('root-review', 'root-verify', 'root-final')


# Pure policy

@pytest.mark.parametrize('steps,reserved', [(1, 1), (2, 2), (3, 2), (4, 2), (7, 2), (8, 2), (11, 2), (12, 3),
                                            (64, 3), (256, 3)])
def test_root_reserve_is_bounded_per_step_share_of_existing_caps(steps, reserved):
    reserve = root_reserve({'max_steps': steps, 'max_seconds': 300., 'window': WINDOW})
    assert reserved_steps(steps) == reserve['steps'] == reserved
    assert reserve['dispatches'] == 3 * reserved and reserve['model_tokens'] == 3 * reserved * WINDOW
    assert reserve['tool_receipts'] == 8 * reserved
    assert reserve['seconds'] == pytest.approx(300. * reserved / steps)
    # Review and final answer whenever the cap allows, never beyond the cap or an extra allowance.
    assert reserve['steps'] <= steps and reserve['seconds'] <= 300. + 1e-9
    assert reserve['steps'] >= min(2, steps)


def ledger(**changes):
    base = {'max_steps': 12, 'max_seconds': 300., 'max_tokens': 12 * 3 * WINDOW, 'window': WINDOW,
            'used_steps': 0, 'dispatches': 0, 'charged_tokens': 0, 'used_seconds': 0., 'tool_receipts': 0,
            'allocated_steps': 0, 'allocated_seconds': 0., 'allocated_tokens': 0, 'nodes_left': 8}
    return {**base, **changes}


@pytest.mark.parametrize('used,recommendation', [(0, 'delegation_fits'), (3, 'small_child'), (6, 'small_child'),
                                                 (7, 'solo'), (9, 'solo')])
def test_advice_follows_budget_numbers_only(used, recommendation):
    advice = delegation_advice(ledger(used_steps=used, dispatches=used, charged_tokens=used * WINDOW))
    assert advice['recommendation'] == recommendation and advice['reason'] == 'budget'
    assert advice['child_steps_left'] == max(0, 9 - used) and advice['root_reserved_steps'] == 3
    # largest_child leaves the spawner's own next wait step.
    assert advice['largest_child']['max_steps'] == max(0, 9 - used - 1)
    assert delegation_advice(ledger(nodes_left=0))['reason'] == 'tree_limit'


def test_schema_note_is_static_per_tree():
    assert schema_note(12) == ' Budget: root keeps 3 of 12 shared steps; omitted limits are host-fitted, unfit explicit ones fail.'
    assert schema_note(4) == ' Budget: root keeps 2 of 4 shared steps; omitted limits are host-fitted, unfit explicit ones fail.'
    assert schema_note(2) == ' Budget: root keeps all 2 shared steps; no child fits.'


# Host-enforced reserve on the durable ledger

def test_tight_tree_children_stop_at_reserve_and_root_still_reviews_and_finishes(env):
    cid, rid = start(env, steps=12)
    assert turn(env, cid, rid, rid, 'root-spawn-turn')['admitted']
    child = spawn(env, cid, rid, {'max_steps': 8, 'max_seconds': 60, 'model_tokens': 8 * WINDOW})
    assert child['limits'] == {'max_steps': 8, 'max_seconds': 60.0, 'model_tokens': 8 * WINDOW}
    assert child['limit_sources'] == dict.fromkeys(LIMIT_KEYS, 'explicit')
    claim(env, cid, rid, child)
    assert turn(env, cid, rid, rid, 'root-wait-turn')['admitted']
    outcomes = [turn(env, cid, rid, child['id'], 'child-' + str(index)) for index in range(8)]
    # 12 shared steps - 3 reserved - 2 root turns leave 7 for the child, below its own limit of 8.
    assert [o['admitted'] for o in outcomes] == [True] * 7 + [False]
    assert outcomes[-1]['status'] == 'budget_exhausted' and outcomes[-1]['reason'] == 'root_reserve'
    record = env.tree.get(cid, rid, child['id'])
    assert record['status'] == 'budget_exhausted'
    assert record['result'] == {'text': DENIALS['root_reserve'], 'reason': 'root_reserve', 'reported_by': 'host'}
    budget = env.tree.budget(cid, rid)
    assert budget['used']['steps'] == 9 and budget['child_capacity']['steps'] == 0
    assert budget['root_reserve']['steps'] == 3 and budget['delegation']['recommendation'] == 'solo'
    # The root keeps exactly its reserve: review, one verification and the final answer.
    assert env.tree.dismiss(cid, rid, rid, child['id'], 1)['review'] == 'dismissed'
    assert root_turns(env, cid, rid, REVIEW) == [True] * 3
    over = turn(env, cid, rid, rid, 'root-over')
    assert not over['admitted'] and over['reason'] == 'shared_budget'
    after = env.tree.budget(cid, rid)
    assert after['used']['steps'] == 12 == after['root_limits']['max_steps']
    assert after['root_limits'] == budget['root_limits']


def test_concurrent_child_admissions_cannot_take_reserved_steps(env):
    cid, rid = start(env, steps=12)
    assert turn(env, cid, rid, rid, 'root-spawn-turn')['admitted']
    children = [spawn(env, cid, rid, {'max_steps': 4, 'max_seconds': 30, 'model_tokens': 4 * WINDOW}, name='w' + str(i))
                for i in range(3)]
    for child in children:
        claim(env, cid, rid, child)
    barrier, results, lock = threading.Barrier(len(children)), [], threading.Lock()

    def worker(child):
        barrier.wait()
        for index in range(4):
            try:
                # In-flight dispatches stay running, as with concurrent provider requests.
                admission = turn(env, cid, rid, child['id'], child['id'] + '-' + str(index), finish=False)
            except ValueError:
                return  # A denial closed this child; it cannot dispatch again.
            with lock:
                results.append(admission['admitted'])
            if not admission['admitted']:
                return

    threads = [threading.Thread(target=worker, args=(child,)) for child in children]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    # Three children could ask for 12; only the 8 unreserved shared steps were admitted.
    assert results.count(True) == 8
    assert env.app.store.query("SELECT count(*) AS n FROM agent_dispatches WHERE run_id=? AND status='running'", (rid,))[0]['n'] == 8
    assert root_turns(env, cid, rid, REVIEW) == [True] * 3
    assert not turn(env, cid, rid, rid, 'root-over')['admitted']


def test_grandchildren_and_parents_share_one_reserved_pool(env):
    cid, rid = start(env, steps=12)
    assert turn(env, cid, rid, rid, 'root-spawn-turn')['admitted']
    parent = spawn(env, cid, rid, {'max_steps': 6}, tools=('Read', 'SpawnAgent'), name='parent')
    claim(env, cid, rid, parent)
    grandchild = spawn(env, cid, rid, {'max_steps': 6}, parent=parent['id'], name='grandchild')
    assert grandchild['depth'] == 2 and grandchild['limit_sources']['max_steps'] == 'explicit'
    # Omitted limits are fitted to unallocated budget outside the reserve, never above the parent.
    assert grandchild['limit_sources']['model_tokens'] == 'default_fitted'
    assert grandchild['limits']['model_tokens'] == 12 * 3 * WINDOW - 6 * 3 * WINDOW - 3 * 3 * WINDOW
    claim(env, cid, rid, grandchild)
    for index in range(4):
        assert turn(env, cid, rid, parent['id'], 'parent-' + str(index))['admitted']
    outcomes = [turn(env, cid, rid, grandchild['id'], 'grandchild-' + str(index)) for index in range(5)]
    assert [o['admitted'] for o in outcomes] == [True] * 4 + [False]
    assert outcomes[-1]['reason'] == 'root_reserve'
    assert turn(env, cid, rid, parent['id'], 'parent-late')['reason'] == 'root_reserve'
    assert root_turns(env, cid, rid, REVIEW) == [True] * 3
    assert env.tree.budget(cid, rid)['used']['steps'] == 12


def test_restart_and_retry_never_replenish_shared_or_reserved_budget(env):
    cid, rid = start(env, steps=12)
    assert turn(env, cid, rid, rid, 'root-spawn-turn')['admitted']
    child = spawn(env, cid, rid, {'max_steps': 8})
    claim(env, cid, rid, child)
    for index in range(4):
        assert turn(env, cid, rid, child['id'], 'first-' + str(index))['admitted']
    assert turn(env, cid, rid, child['id'], 'in-flight', finish=False)['admitted']
    before = env.tree.budget(cid, rid)
    restarted = Tree(env.app)
    assert restarted.get(cid, rid, child['id'])['status'] == 'interrupted'
    after = restarted.budget(cid, rid)
    # The interrupted dispatch keeps its charges; only its step slot is free again.
    assert after['used']['dispatches'] == before['used']['dispatches'] == 6
    assert after['used']['model_tokens'] == before['used']['model_tokens'] == 6 * WINDOW
    assert after['allocated'] == before['allocated'] and after['root_limits'] == before['root_limits']
    assert turn(env, cid, rid, rid, 'root-after-restart')['admitted']
    retried = restarted.retry(cid, rid, rid, child['id'])
    assert retried['generation'] == 2 and retried['limits'] == child['limits']
    claim(env, cid, rid, retried, generation=2)
    outcomes = [turn(env, cid, rid, child['id'], 'second-' + str(index), generation=2) for index in range(4)]
    # 12 - 3 reserved - 2 root - 4 completed child steps; the new generation gets no fresh pool.
    assert [o['admitted'] for o in outcomes] == [True, True, True, False]
    assert outcomes[-1]['reason'] == 'root_reserve'
    assert env.tree.get(cid, rid, child['id'])['dispatches'] == 8
    Tree(env.app)
    assert root_turns(env, cid, rid, REVIEW) == [True] * 3
    assert not turn(env, cid, rid, rid, 'root-over')['admitted']


# Fitted defaults and explicit requests

def test_omitted_limits_are_host_fitted_and_explicit_limits_are_exact_or_refused(env):
    cid, rid = start(env, steps=12)
    assert turn(env, cid, rid, rid, 'root-spawn-turn')['admitted']
    allocated = env.tree.budget(cid, rid)['allocated']
    for limits, message in [({'max_steps': 9}, 'Explicit max_steps 9 does not fit: children can use 8 steps'),
                            ({'max_seconds': 290}, 'Explicit max_seconds 290 does not fit: children can use 225 seconds'),
                            ({'model_tokens': 27 * WINDOW}, 'Explicit model_tokens ' + str(27 * WINDOW) + ' does not fit')]:
        with pytest.raises(UnfitLimits, match=message) as caught:
            spawn(env, cid, rid, limits)
        assert 'the root keeps' in str(caught.value) and 'Nothing was spawned' in str(caught.value)
        assert caught.value.advice['recommendation'] == 'delegation_fits'
    assert not env.tree.list(cid, rid) and env.tree.budget(cid, rid)['allocated'] == allocated
    fitted = spawn(env, cid, rid)
    # Default 8 steps clamped to 12 - 3 reserved - 1 used - 1 for the spawner's next wait.
    assert fitted['limits'] == {'max_steps': 7, 'max_seconds': 120.0, 'model_tokens': 7 * 3 * WINDOW}
    assert fitted['limit_sources'] == {'max_steps': 'default_fitted', 'max_seconds': 'default', 'model_tokens': 'default'}
    exact = spawn(env, cid, rid, {'max_steps': 5, 'max_seconds': 12.5, 'model_tokens': 5000}, name='exact')
    assert exact['limits'] == {'max_steps': 5, 'max_seconds': 12.5, 'model_tokens': 5000}
    assert exact['limit_sources'] == dict.fromkeys(LIMIT_KEYS, 'explicit')
    with pytest.raises(UnfitLimits, match='Child allocations cannot multiply the root budget'):
        spawn(env, cid, rid, name='third')
    assert len(env.tree.list(cid, rid)) == 2


def test_no_capacity_refuses_defaults_and_explicit_limits_and_advises_solo(env):
    cid, rid = start(env, steps=12)
    assert root_turns(env, cid, rid, ['root-' + str(index) for index in range(9)]) == [True] * 9
    advice = env.tree.advice(cid, rid)
    assert advice['recommendation'] == 'solo' and advice['child_steps_left'] == 0
    assert advice['largest_child']['max_steps'] == 0
    with pytest.raises(UnfitLimits, match='No default child fits beside the root reserve'):
        spawn(env, cid, rid)
    with pytest.raises(UnfitLimits, match='Explicit max_steps 1 does not fit: .* continue solo'):
        spawn(env, cid, rid, {'max_steps': 1})
    with pytest.raises(ValueError, match='positive bounded'):
        spawn(env, cid, rid, {'max_steps': 0})
    assert not env.tree.list(cid, rid)
    # The root itself may still spend its reserve.
    assert root_turns(env, cid, rid, REVIEW) == [True] * 3


def test_children_cannot_take_the_roots_reserved_tool_receipts(env):
    cid, rid = start(env, steps=4)
    limits = {'max_steps': 2, 'max_seconds': 10, 'model_tokens': 2 * WINDOW}
    first = spawn(env, cid, rid, limits, name='first')
    second = spawn(env, cid, rid, limits, name='second')
    for node in (first, second):
        claim(env, cid, rid, node)

    def read(node, index):
        call = node['name'] + '-read-' + str(index)
        env.tree.begin_tool(cid, rid, node['id'], 1, owner(node['id']), call, 'Read', {'file_path': 'x.py'})
        env.tree.end_tool(cid, rid, node['id'], 1, owner(node['id']), call, {'path': 'x.py', 'status': 'read'})

    # 32 shared receipts; the root keeps 16. Neither child reaches its own cap of 16.
    for index in range(10):
        read(first, index)
    for index in range(6):
        read(second, index)
    with pytest.raises(ToolBudgetExhausted, match='root keeps 16 receipts'):
        read(second, 6)
    budget = env.tree.budget(cid, rid)
    assert budget['child_capacity']['tool_receipts'] == 0 and budget['root_reserve']['tool_receipts'] == 16
    assert budget['root_limits']['max_tool_receipts'] == 32
    assert env.app.store.query('SELECT count(*) AS n FROM agent_receipts WHERE run_id=?', (rid,))[0]['n'] == 16


@pytest.mark.parametrize('steps,reserved', [(1, 1), (2, 2), (3, 2), (4, 2)])
def test_small_roots_keep_review_and_final_capacity_and_admit_only_a_real_remainder(env, steps, reserved):
    cid, rid = start(env, steps=steps)
    budget = env.tree.budget(cid, rid)
    assert budget['root_limits']['max_steps'] == steps and budget['root_reserve']['steps'] == reserved
    assert budget['child_capacity']['steps'] == steps - reserved
    assert budget['child_capacity']['tool_receipts'] == 8 * (steps - reserved)
    assert turn(env, cid, rid, rid, 'root-spawn-turn')['admitted']
    room = max(0, steps - reserved - 1)
    advice = env.tree.advice(cid, rid)
    assert advice['child_steps_left'] == room and advice['recommendation'] == 'solo'
    # A host-fitted default also leaves the spawner's wait step, so none fits these trees.
    with pytest.raises(UnfitLimits, match='No default child fits beside the root reserve'):
        spawn(env, cid, rid)
    limits = {'max_steps': 1, 'max_seconds': 10, 'model_tokens': WINDOW}
    if not room:
        # No remainder: an explicit request fails clearly and nothing is admitted.
        with pytest.raises(UnfitLimits, match='Explicit max_steps 1 does not fit: .* continue solo'):
            spawn(env, cid, rid, limits)
        assert not env.tree.list(cid, rid)
    else:
        first, second = spawn(env, cid, rid, limits, name='first'), spawn(env, cid, rid, limits, name='second')
        for node in (first, second):
            claim(env, cid, rid, node)
        assert turn(env, cid, rid, first['id'], 'first-turn')['admitted']
        denied = turn(env, cid, rid, second['id'], 'second-turn')
        assert not denied['admitted'] and denied['reason'] == 'root_reserve'
    # The root still owns every remaining step, then the fixed cap holds.
    remaining = steps - 1 - room
    assert remaining >= min(reserved, steps - 1)
    assert root_turns(env, cid, rid, ['root-' + str(index) for index in range(remaining)]) == [True] * remaining
    over = turn(env, cid, rid, rid, 'root-over')
    assert not over['admitted'] and over['reason'] == 'shared_budget'
    assert env.tree.budget(cid, rid)['used']['steps'] == steps


def test_elapsed_reserve_blocks_child_admission_but_not_the_root(env):
    cid, rid = start(env, steps=12, seconds=120)
    child = spawn(env, cid, rid, {'max_steps': 2, 'max_seconds': 10, 'model_tokens': 2 * WINDOW})
    claim(env, cid, rid, child)
    # Persisted root time leaves less than the root's 30-second reserved share.
    env.app.store.query('UPDATE runs SET elapsed=? WHERE id=?', (95.0, rid))
    denied = turn(env, cid, rid, child['id'], 'child-late')
    assert not denied['admitted'] and denied['reason'] == 'root_reserve'
    assert turn(env, cid, rid, rid, 'root-final')['admitted']


MARKER = 'ledger-private-phrase-7f3a'


def test_spawn_and_child_advice_is_content_free_and_never_requires_delegation(env):
    cid, rid = start(env, steps=12)
    child = env.tree.spawn(cid, rid, None, 'Summarise ' + MARKER, MARKER, ['Read', 'SpawnAgent'], {'max_steps': 4},
                           context={'hint': MARKER})
    advice = child['delegation']
    assert set(advice) == {'recommendation', 'reason', 'root_reserved_steps', 'child_steps_left', 'largest_child', 'note'}
    assert MARKER not in json.dumps(advice) and MARKER not in json.dumps(env.tree.budget(cid, rid)['delegation'])
    assert advice['recommendation'] == 'delegation_fits' and 'optional' in advice['note']
    assert not any(word in advice['note'].lower() for word in ('must', 'required', 'should spawn'))
    claim(env, cid, rid, child)
    own = env.tree.advice(cid, rid, child['id'])
    assert own['own_steps_left'] == 4 and own['largest_child']['max_steps'] <= 4
    assert MARKER not in json.dumps(own)
    leaf = spawn(env, cid, rid, {'max_steps': 2}, parent=child['id'], name='leaf')
    assert leaf['delegation']['recommendation'] in {'solo', 'small_child', 'delegation_fits'}
    claim(env, cid, rid, leaf)
    advice = env.tree.advice(cid, rid, leaf['id'])
    assert (advice['recommendation'], advice['reason'], advice['largest_child']['max_steps']) == ('solo', 'tree_limit', 0)


# The real manager path: SpawnAgent, child executor and root attempts

PROFILE = {'id': 'delegation-fixture', 'kind': 'openai', 'name': 'Delegation fixture',
           'url': 'https://unused.invalid', 'model': 'fixture', 'context': 8192, 'budget': 4096, 'tools': True}


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(Agents, 'start', lambda self: None)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'source.py').write_text('value = 1\n')
    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(PROFILE))
    monkeypatch.setattr(app.guard, 'handle', lambda *args, **kwargs: {'action': 'allow'})
    yield SimpleNamespace(app=app, project=project)
    app.active = app.active_run = None
    app.close()


def host_root(app, project, steps):
    chat = app.new_chat(str(project))
    config = app.runtime_config(chat, False, {'max_steps': steps, 'max_seconds': 180})
    rid = app.runs.create(chat['id'], 'Inspect the source with a delegated reader.', config)
    app.runs.transition(rid, 'running')
    app.active, app.active_run = chat['id'], rid
    app.task_policy = copy.deepcopy(config['permissions'])
    return chat['id'], rid


def root_turn(app, cid, rid, usage=None):
    with app.agents.root_attempt(cid, rid, [{'role': 'user', 'content': 'turn'}], [], threading.Event()):
        if usage is not None:
            app.model.last_usage = dict(usage)  # What the root provider binds after a completed request.


def test_real_manager_child_stops_at_reserve_while_root_reviews_and_finishes(host):
    app = host.app
    cid, rid = host_root(app, host.project, 12)
    # The root's spawning turn committed before the tree existed; it is backfilled conservatively.
    app.runs.response(rid, {'role': 'assistant', 'content': 'Delegating one read.'}, {'messages': []}, steps=1, elapsed=0)
    with pytest.raises(UnfitLimits, match='Explicit max_steps 9 does not fit'):
        app.agents.tool(cid, rid, rid, 'SpawnAgent', {'task': 'Read source.py.', 'tools': '["Read"]', 'max_steps': 9}, 'spawn-unfit')
    child = app.agents.tool(cid, rid, rid, 'SpawnAgent', {'task': 'Read source.py and report.', 'name': 'reader',
                                                          'tools': '["Read", "SpawnAgent"]'}, 'spawn')
    assert child['limits']['max_steps'] == 7 and child['limit_sources']['max_steps'] == 'default_fitted'
    assert child['delegation']['root_reserved_steps'] == 3
    assert len(app.agents.tree.list(cid, rid)) == 1
    app.agents.tree.claim(cid, rid, child['id'], 1, 'reserve-worker')
    root_turn(app, cid, rid, {'input_tokens': 900, 'output_tokens': 12})  # The WaitForAgents turn.

    class Reader:
        """Reads every step and reports usage; its third request overlaps a root turn."""

        def __init__(self):
            self.requests, self.last_usage, self.tools = 0, {}, []

        def complete(self, messages, tools, cancel, token, effort=False):
            self.requests += 1
            self.tools.append(copy.deepcopy(tools))
            if self.requests == 3:
                root_turn(app, cid, rid, {'input_tokens': 950, 'output_tokens': 15})
            self.last_usage = {'input_tokens': 100 + self.requests, 'output_tokens': 4}
            return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'read', 'type': 'function', 'function': {
                'name': 'Read', 'arguments': json.dumps({'file_path': 'source.py'})}}]}

        def cancel(self):
            pass

        def close(self):
            pass

    reader = Reader()
    app.agents.provider_factory = lambda *args, **kwargs: reader
    app.agents.drive({'cid': cid, 'rid': rid, 'id': child['id'], 'generation': 1, 'owner': 'reserve-worker',
                      'cancel': threading.Event(), 'suspend': False})
    # 12 - 3 reserved - 3 root turns leaves 6 child steps, below the child's own 7.
    assert reader.requests == 6
    record = app.agents.tree.get(cid, rid, child['id'])
    assert record['status'] == 'budget_exhausted' and record['result']['reason'] == 'root_reserve'
    assert app.agents.tree.dismiss(cid, rid, rid, child['id'], 1)['review'] == 'dismissed'
    for index in range(3):
        root_turn(app, cid, rid, {'input_tokens': 1000 + index, 'output_tokens': 20})
    with pytest.raises(AgentBudgetExhausted):
        root_turn(app, cid, rid)
    budget = app.agents.tree.budget(cid, rid)
    assert budget['used']['steps'] == 12 == budget['root_limits']['max_steps']
    # Reservations (3 pre-tree estimates + 11 admissions) stay the only charges.
    assert budget['used']['model_tokens'] == 14 * PROFILE['context']
    rows = {(row['actor'], row['generation']): row for row in budget['usage']['by_actor']}
    assert rows[child['id'], 1]['states'] == {'reported': 6}
    assert rows[child['id'], 1]['totals'] == {'input_tokens': sum(100 + i for i in range(1, 7)), 'output_tokens': 24,
                                               'cached_tokens': None}
    assert rows[rid, 0]['states'] == {'reported': 5}
    assert rows[rid, 0]['known']['input_tokens'] == 900 + 950 + 1000 + 1001 + 1002
    # The pre-tree root turn was never measured by this ledger.
    assert budget['usage']['pre_tree_root_steps'] == 1 and budget['usage']['complete'] is False
    # The child SpawnAgent schema carries only the static per-tree note.
    spawn_schemas = [t for request in reader.tools for t in request if t['function']['name'] == 'SpawnAgent']
    assert len(spawn_schemas) == 6
    assert all(t['function']['description'].endswith(schema_note(12)) for t in spawn_schemas)
    assert len({t['function']['description'] for t in spawn_schemas}) == 1
