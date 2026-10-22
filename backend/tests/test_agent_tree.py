"""Durable child authority, leases, budget admissions and wake-up races."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble.agent_tree import Tree, ordinary
from pebble.interaction import RunInteractions
from pebble.permissions import DEFAULT
from pebble.runtime import RunJournal
from pebble.store import Store


@pytest.fixture
def env(tmp_path):
    home = tmp_path / 'data'
    store = Store(home)
    runs = RunJournal(store, home)
    interactions = RunInteractions(store, runs)
    app = SimpleNamespace(store=store, runs=runs, interactions=interactions)
    app.tree = Tree(app)
    cid = store.new_chat(str(tmp_path))
    config = {'workspace': str(tmp_path), 'permissions': DEFAULT, 'route': {'context': 4096},
              'limits': {'max_steps': 64, 'max_seconds': 900}}
    rid = runs.create(cid, 'Inspect source', config)
    runs.transition(rid, 'running')
    yield app, cid, rid
    runs.close()
    store.close()


def spawn(env, *, parent=None, tools=None, limits=None, paths=(), deps=(), task='Read source'):
    app, cid, rid = env
    return app.tree.spawn(cid, rid, parent, task, 'worker', tools or ['Read', 'SendAgentMessage'],
                          limits or {'max_steps': 4, 'max_seconds': 30, 'model_tokens': 8192},
                          paths, deps, parent_generation=1 if parent else None, parent_owner='worker-1' if parent else None)


def claim(env, node, owner='worker-1'):
    app, cid, rid = env
    return app.tree.claim(cid, rid, node['id'], node['generation'], owner)


def evidence(env, node, owner='worker-1', call='read'):
    app, cid, rid = env
    app.tree.begin_tool(cid, rid, node['id'], node['generation'], owner, call, 'Read', {'file_path': 'x.py'})
    app.tree.end_tool(cid, rid, node['id'], node['generation'], owner, call,
                      {'status': 'read', 'content': 'x = 1', 'origin': 'source', 'authorization': 'none'})
    return call


def settle(env, node, owner='worker-1', ids=()):
    app, cid, rid = env
    return app.tree.settle(cid, rid, node['id'], node['generation'], 'completed',
                           {'summary': 'Read source'}, ids, owner)


def test_children_are_scoped_separate_records_and_narrow_parent_capabilities(env):
    app, cid, rid = env
    child = spawn(env, tools=['Read', 'SpawnAgent'])
    assert child['origin'] == 'agent' and child['authorization'] == 'none'
    assert len(app.runs.list()) == 1 and child['generation'] == 1
    assert app.tree.get(cid, rid, child['id'])['tools'] == ['Read', 'SpawnAgent']
    with pytest.raises(ValueError, match='conversation'):
        app.tree.get('other-chat', rid, child['id'])
    claim(env, child)
    grandchild = spawn(env, parent=child['id'], tools=['Read', 'SpawnAgent'])
    assert grandchild['depth'] == 2
    claim(env, grandchild)
    with pytest.raises(ValueError, match='depth'):
        spawn(env, parent=grandchild['id'], tools=['Read'])
    with pytest.raises(ValueError, match='capabilities'):
        spawn(env, parent=child['id'], tools=['WebSearch'])
    with pytest.raises(ValueError, match='capabilities'):
        spawn(env, tools=['RunCommand'])


@pytest.mark.parametrize('value', [
    {'origin': 'human'}, {'permissions': {'files': True}}, {'outer': {'authority': 'host'}},
    {'api-key': 'fake'}, {'text': 'sk-' + 'a' * 32}, {'secret': 'fake'}, {'x': float('nan')},
    {'password': 'ordinary-looking-credential'}, {'nested': {'api key': 'fake'}},
    {'x': [[[[[[['too deep']]]]]]]},
])
def test_ordinary_payload_cannot_poison_authority_or_store_credentials(value):
    with pytest.raises(ValueError):
        ordinary(value)


def test_permission_snapshot_and_parent_tools_both_bound_child(env):
    app, cid, rid = env
    config = app.runs.get(rid)['config']
    config['permissions']['tools']['web'] = False
    app.store.query('UPDATE runs SET config=? WHERE id=?', (json.dumps(config), rid))
    with pytest.raises(ValueError, match='capabilities'):
        spawn(env, tools=['WebSearch'])
    child = spawn(env)
    config['permissions']['tools']['web'] = True
    app.store.query('UPDATE runs SET config=? WHERE id=?', (json.dumps(config), rid))
    with pytest.raises(ValueError, match='configuration'):
        spawn(env)
    assert app.tree.get(cid, rid, child['id'])['status'] == 'queued'


def test_node_depth_and_count_are_durable_limits(env):
    app, cid, rid = env
    for _ in range(8):
        spawn(env)
    with pytest.raises(ValueError, match='eight'):
        spawn(env)
    assert len(app.tree.list(cid, rid)) == 8


def test_parallel_claims_have_one_winner_and_wrong_generation_cannot_settle(env):
    app, cid, rid = env
    node = spawn(env)
    winners, errors = [], []
    barrier = threading.Barrier(3)
    def worker(owner):
        barrier.wait()
        try:
            winners.append(app.tree.claim(cid, rid, node['id'], 1, owner))
        except ValueError as exc:
            errors.append(str(exc))
    threads = [threading.Thread(target=worker, args=(owner,)) for owner in ('one', 'two')]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(winners) == len(errors) == 1
    with pytest.raises(ValueError, match='lease'):
        app.tree.heartbeat(cid, rid, node['id'], 1, 'wrong')
    ignored = app.tree.settle(cid, rid, node['id'], 2, 'completed', {}, owner=winners[0]['owner'])
    assert ignored['ignored'] and ignored['status'] == 'running'
    app.tree.cancel(cid, rid, rid, node['id'])
    assert app.tree.settle(cid, rid, node['id'], 1, 'completed', {}, owner=winners[0]['owner'])['ignored']


def test_budget_allocations_and_failed_dispatch_tokens_do_not_multiply(env):
    app, cid, rid = env
    one = spawn(env, limits={'max_steps': 32, 'max_seconds': 400, 'model_tokens': 10000})
    spawn(env, limits={'max_steps': 32, 'max_seconds': 400, 'model_tokens': 10000})
    with pytest.raises(ValueError, match='multiply'):
        spawn(env)
    claim(env, one)
    admission = app.tree.reserve_model(cid, rid, one['id'], 1, 6000, 2000, 'attempt-1')
    assert admission['admitted'] and admission['charged_tokens'] == 8000
    app.tree.finish_model(cid, rid, one['id'], 1, 'attempt-1', failed=True)
    assert app.tree.budget(cid, rid)['used']['model_tokens'] == 8000
    with pytest.raises(ValueError, match='already admitted'):
        app.tree.reserve_model(cid, rid, one['id'], 1, 6000, 2000, 'attempt-1')
    denied = app.tree.reserve_model(cid, rid, one['id'], 1, 2000, 1, 'attempt-2')
    assert not denied['admitted'] and app.tree.get(cid, rid, one['id'])['status'] == 'budget_exhausted'
    assert app.tree.budget(cid, rid)['used']['model_tokens'] == 8000


def test_root_and_child_share_global_dispatch_admissions_even_on_failures(env):
    app, cid, rid = env
    config = app.runs.get(rid)['config']
    config['limits']['max_steps'] = 3
    app.store.query('UPDATE runs SET config=? WHERE id=?', (json.dumps(config), rid))
    child = spawn(env, limits={'max_steps': 1, 'max_seconds': 30, 'model_tokens': 10000})
    claim(env, child)
    for i, actor in enumerate((rid, child['id'], rid)):
        generation = 0 if actor == rid else 1
        assert app.tree.reserve_model(cid, rid, actor, generation, 100, 10, 'dispatch-' + str(i))['admitted']
        app.tree.finish_model(cid, rid, actor, generation, 'dispatch-' + str(i), failed=True)
    late = app.tree.reserve_model(cid, rid, child['id'], 1, 100, 10, 'child-late')
    assert not late['admitted'] and late['reason'] == 'root_reserve'
    for i in range(3, 9):
        assert app.tree.reserve_model(cid, rid, rid, 0, 100, 10, 'dispatch-' + str(i))['admitted']
        app.tree.finish_model(cid, rid, rid, 0, 'dispatch-' + str(i), failed=True)
    assert not app.tree.reserve_model(cid, rid, rid, 0, 1, 1, 'overflow')['admitted']
    assert app.tree.budget(cid, rid)['used']['dispatches'] == 9


def test_parent_wait_keeps_children_dispatchable_and_blocks_root_dispatch(env):
    app, cid, rid = env
    child = spawn(env)
    claim(env, child)
    app.runs.transition(rid, 'waiting_input')
    with pytest.raises(ValueError, match='running root'):
        app.tree.reserve_model(cid, rid, rid, 0, 1, 1, 'root-dispatch')
    assert app.tree.reserve_model(cid, rid, child['id'], 1, 1, 1, 'child-dispatch')['admitted']
    with pytest.raises(ValueError, match='running root'):
        spawn(env)


def test_exact_path_leases_prevent_overlap_and_root_integration_requires_release(env):
    app, cid, rid = env
    child = spawn(env, paths=['src/x.py'])
    claim(env, child)
    app.tree.assert_path(cid, rid, child['id'], 'src/x.py', 1, 'a' * 64)
    with pytest.raises(ValueError, match='baseline'):
        app.tree.assert_path(cid, rid, child['id'], 'src/x.py', 1, 'b' * 64)
    with pytest.raises(ValueError, match='lease'):
        app.tree.assert_path(cid, rid, child['id'], 'src/y.py', 1)
    with pytest.raises(ValueError, match='already leased'):
        spawn(env, paths=['src'])
    with pytest.raises(ValueError, match='release'):
        app.tree.assert_path(cid, rid, rid, 'src/x.py')
    assert app.tree.assert_path(cid, rid, rid, 'unleased.py')['generation'] == 0
    call = evidence(env, child)
    settle(env, child, ids=[call])
    app.tree.review(cid, rid, rid, child['id'], 1, [call])
    app.tree.release_paths(cid, rid, rid, child['id'], 1)
    assert app.tree.assert_path(cid, rid, rid, 'src/x.py')['path'] == 'src/x.py'


@pytest.mark.parametrize('path', ['../x.py', '/tmp/x.py', 'src//x.py', 'src/./x.py', 'src\\x.py', '.git/config', '.env', 'C:x.py', '.env.local', '.pebble/config', 'secrets/token', 'x.pem', 'src/x\ny.py'])
def test_paths_reject_escape_private_metadata_and_ambiguous_normalization(env, path):
    with pytest.raises(ValueError):
        spawn(env, paths=[path])


def test_dependencies_claim_atomic_reassignment_and_cycle_detection(env):
    app, cid, rid = env
    a, b = spawn(env), spawn(env)
    app.tree.delegate(cid, rid, rid, b['id'], dependencies=[a['id']])
    with pytest.raises(ValueError, match='dependencies'):
        claim(env, b)
    with pytest.raises(ValueError, match='cycle'):
        app.tree.delegate(cid, rid, rid, a['id'], dependencies=[b['id']])
    claim(env, a)
    with pytest.raises(ValueError, match='reassigned'):
        app.tree.delegate(cid, rid, rid, a['id'], task='Changed task')
    settle(env, a)
    assert claim(env, b)['generation'] == 1


def test_messages_use_host_actor_identity_and_at_least_once_ack_cursor(env):
    app, cid, rid = env
    a, b = spawn(env), spawn(env)
    claim(env, a)
    first = app.tree.send(cid, rid, a['id'], b['id'], {'text': 'ordinary task'}, idempotency_key='same', generation=1, owner='worker-1')
    again = app.tree.send(cid, rid, a['id'], b['id'], {'text': 'ordinary task'}, idempotency_key='same', generation=1, owner='worker-1')
    assert first['id'] == again['id'] and first['sender'] == a['id'] and first['origin'] == 'agent'
    with pytest.raises(ValueError, match='conflicts'):
        app.tree.send(cid, rid, a['id'], b['id'], 'changed', idempotency_key='same', generation=1, owner='worker-1')
    delivered = app.tree.deliver(cid, rid, b['id'])
    assert app.tree.deliver(cid, rid, b['id']) == delivered
    assert not app.tree.deliver(cid, rid, b['id'], delivered['cursor'])['messages']
    with pytest.raises(ValueError, match='recipient'):
        app.tree.ack(cid, rid, a['id'], [first['id']])
    app.tree.ack(cid, rid, b['id'], [first['id']])
    assert not app.tree.deliver(cid, rid, b['id'])['messages']
    with pytest.raises(ValueError, match='authority'):
        app.tree.send(cid, rid, a['id'], b['id'], {'origin': 'human'}, generation=1, owner='worker-1')


def test_message_delivery_boundary_and_lifetime_message_quota(env):
    app, cid, rid = env
    child = spawn(env)
    for i in range(128):
        app.tree.send(cid, rid, rid, child['id'], str(i), idempotency_key=str(i))
    assert len(app.tree.deliver(cid, rid, child['id'])['messages']) == 20
    with pytest.raises(ValueError, match='128'):
        app.tree.send(cid, rid, rid, child['id'], 'overflow')


def test_settlement_refreshes_wait_receipt_and_notifies_exactly_once(env):
    app, cid, rid = env
    child = spawn(env)
    claim(env, child)
    app.runs.begin_tool(rid, '1:wait', 'WaitForAgents', {'ids': [child['id']]})
    result = app.tree.wait(cid, rid, rid, '1:wait', [child['id']])
    assert result['status'] == 'awaiting_agent_results'
    with app.store.lock:
        assert Tree.has_pending_wait(app.store.db, rid)
    app.runs.end_tool(rid, '1:wait', result)
    app.runs.transition(rid, 'waiting_input')
    settle(env, child)
    assert app.runs.tools(rid)[0]['result']['status'] == 'ready'
    assert app.interactions.continuation(rid)['state'] == 'queued'
    inbox = app.store.query("SELECT * FROM run_inbox WHERE run_id=? AND kind='agent_wait_ready'", (rid,))
    assert len(inbox) == 1 and inbox[0]['origin'] == 'agent'
    settle(env, child)
    assert len(app.store.query('SELECT * FROM run_inbox WHERE run_id=?', (rid,))) == 1


def test_wait_receipt_helper_closes_settle_before_end_tool_race(env):
    app, cid, rid = env
    child = spawn(env)
    claim(env, child)
    app.runs.begin_tool(rid, '1:wait', 'WaitForAgents', {})
    original = app.tree.wait(cid, rid, rid, '1:wait', [child['id']])
    settle(env, child)
    with app.store.lock:
        updated = Tree.resolve_wait_receipt(app.store.db, rid, '1:wait', original)
        assert updated['status'] == 'ready'
        assert Tree.resolve_wait_receipt(app.store.db, rid, 'other', original) == original
    app.runs.end_tool(rid, '1:wait', updated)
    assert app.runs.tools(rid)[0]['result']['status'] == 'ready'


def test_child_question_wakes_parent_without_human_origin_or_native_form(env):
    app, cid, rid = env
    child = spawn(env, tools=['Read', 'AskParentQuestion'])
    claim(env, child)
    app.tree.wait(cid, rid, rid, '1:wait', [child['id']])
    question = app.tree.ask_parent(cid, rid, child['id'], 1, 'worker-1', 'ask',
                                   [{'id': 'scope', 'title': 'Which source should I read?'}])
    assert app.tree.get(cid, rid, child['id'])['status'] == 'waiting_parent'
    assert not app.interactions.questions(cid)
    pending = app.tree.parent_questions(cid, rid, rid)
    assert pending[0]['id'] == question['question_id']
    result = app.tree.answer_parent(cid, rid, rid, question['question_id'], {'scope': {'text': 'src'}})
    assert result['origin'] == 'agent' and result['authorization'] == 'none'
    assert app.tree.get(cid, rid, child['id'])['status'] == 'running'


def test_parent_review_requires_child_generation_receipts_and_host_results_are_accepted(env):
    app, cid, rid = env
    a, b = spawn(env), spawn(env)
    claim(env, a)
    claim(env, b, 'worker-2')
    call = evidence(env, a)
    with pytest.raises(ValueError, match='generation'):
        app.tree.settle(cid, rid, b['id'], 1, 'completed', {}, [call], 'worker-2')
    app.tree.begin_tool(cid, rid, a['id'], 1, 'worker-1', 'mail', 'SendAgentMessage', {'target': b['id'], 'body': 'hello'})
    message = app.tree.send(cid, rid, a['id'], b['id'], 'hello', generation=1, owner='worker-1')
    assert app.tree.end_tool(cid, rid, a['id'], 1, 'worker-1', 'mail', message)['origin'] == 'agent'
    settle(env, a, ids=[call])
    with pytest.raises(ValueError, match='evidence'):
        app.tree.review(cid, rid, rid, a['id'], 1, [])
    assert app.tree.review(cid, rid, rid, a['id'], 1, [call])['review'] == 'accepted'


def test_cancel_cascade_revokes_claims_paths_questions_and_future_messages(env):
    app, cid, rid = env
    a = spawn(env, tools=['Read', 'SpawnAgent', 'SendAgentMessage'], paths=['a.py'])
    claim(env, a)
    b = spawn(env, parent=a['id'], tools=['Read'], paths=['b.py'])
    claim(env, b, 'worker-2')
    app.tree.send(cid, rid, a['id'], b['id'], 'queued message', generation=1, owner='worker-1')
    stopped = app.tree.cancel(cid, rid, rid, a['id'])
    assert set(stopped['agent_ids']) == {a['id'], b['id']}
    assert not app.store.query('SELECT * FROM agent_claims')
    assert {r['status'] for r in app.store.query('SELECT * FROM agent_path_leases')} == {'revoked'}
    assert not app.tree.deliver(cid, rid, b['id'])['messages']
    with pytest.raises(ValueError, match='new work'):
        app.tree.send(cid, rid, a['id'], rid, 'late outcome', generation=1, owner='worker-1')
    assert settle(env, a)['ignored']


def test_recovery_preserves_history_and_budget_without_automatic_execution(env):
    app, cid, rid = env
    child = spawn(env, paths=['x.py'])
    claim(env, child)
    app.tree.reserve_model(cid, rid, child['id'], 1, 100, 20, 'attempt')
    app.tree.begin_tool(cid, rid, child['id'], 1, 'worker-1', 'read', 'Read', {'file_path': 'x.py'})
    app.tree.wait(cid, rid, rid, '1:wait', [child['id']])
    restarted = Tree(app)
    assert restarted.get(cid, rid, child['id'])['status'] == 'interrupted'
    assert not app.store.query('SELECT * FROM agent_claims')
    assert restarted.budget(cid, rid)['used']['model_tokens'] == 120
    assert restarted.receipts(cid, rid, child['id'], 1)[0]['state'] == 'uncertain'
    with pytest.raises(ValueError, match='queued'):
        restarted.claim(cid, rid, child['id'], 1, 'new-worker')
    assert app.store.query('SELECT status FROM agent_waits')[0]['status'] == 'cancelled'
    assert app.store.query('SELECT status FROM agent_path_leases')[0]['status'] == 'revoked'
    retried = restarted.retry(cid, rid, rid, child['id'])
    assert retried['generation'] == 2
    assert app.store.query('SELECT state FROM agent_receipts')[0]['state'] == 'interrupted'
    assert app.store.query('SELECT status FROM agent_path_leases')[0]['status'] == 'held'
    assert restarted.budget(cid, rid)['used']['model_tokens'] == 120


def test_explicit_retry_changes_generation_and_late_outcome_is_ignored(env):
    app, cid, rid = env
    child = spawn(env)
    claim(env, child)
    app.tree.release(cid, rid, child['id'], 1, 'worker-1')
    retry = app.tree.retry(cid, rid, rid, child['id'])
    assert retry['generation'] == 2
    claim(env, retry, 'worker-2')
    assert settle(env, child)['ignored']
    assert app.tree.get(cid, rid, child['id'])['status'] == 'running'


def test_cross_tree_messages_dependencies_and_evidence_are_rejected(env):
    app, cid, rid = env
    a = spawn(env)
    claim(env, a)
    app.runs.transition(rid, 'waiting_input')
    other_cid = app.store.new_chat()
    other_rid = app.runs.create(other_cid, 'Other work', app.runs.get(rid)['config'])
    app.runs.transition(other_rid, 'running')
    b = app.tree.spawn(other_cid, other_rid, None, 'Read', 'other', ['Read'], {'max_steps': 1, 'max_seconds': 10, 'model_tokens': 100})
    with pytest.raises(ValueError, match='tree'):
        app.tree.send(cid, rid, a['id'], b['id'], 'cross tree', generation=1, owner='worker-1')
    queued = app.tree.spawn(other_cid, other_rid, None, 'Read', 'queued', ['Read'], {'max_steps': 1, 'max_seconds': 10, 'model_tokens': 100})
    with pytest.raises(ValueError, match='tree'):
        app.tree.delegate(other_cid, other_rid, other_rid, queued['id'], dependencies=[a['id']])


def test_sqlite_immediate_claim_transaction_serializes_independent_connections(env):
    app, cid, rid = env
    node = spawn(env)
    other_store = Store(Path(app.store.db.execute('PRAGMA database_list').fetchone()[2]).parent)
    # RunJournal already owns the service lock. A second raw connection isolates
    # the database transaction boundary from the shared in-process Store.lock.
    other_tree = Tree.__new__(Tree)
    other_tree.app = app
    other_tree.store = other_store
    barrier, won, lost = threading.Barrier(3), [], []
    def take(tree, owner):
        barrier.wait()
        try:
            won.append(tree.claim(cid, rid, node['id'], 1, owner))
        except ValueError as exc:
            lost.append(str(exc))
    threads = [threading.Thread(target=take, args=(app.tree, 'one')),
               threading.Thread(target=take, args=(other_tree, 'two'))]
    try:
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        assert len(won) == len(lost) == 1
        assert len(app.store.query('SELECT * FROM agent_claims')) == 1
    finally:
        other_store.close()


def test_old_generation_cannot_send_after_explicit_retry(env):
    app, cid, rid = env
    node = spawn(env)
    claim(env, node)
    app.tree.release(cid, rid, node['id'], 1, 'worker-1')
    new = app.tree.retry(cid, rid, rid, node['id'])
    claim(env, new, 'worker-2')
    with pytest.raises(ValueError, match='stale'):
        app.tree.send(cid, rid, node['id'], rid, 'late', generation=1, owner='worker-1')
    assert app.tree.send(cid, rid, node['id'], rid, 'current', generation=2, owner='worker-2')['generation'] == 2


def test_retry_reacquires_paths_only_if_still_exclusive(env):
    app, cid, rid = env
    node = spawn(env, paths=['x.py'])
    claim(env, node)
    app.tree.recover()
    other = spawn(env, paths=['x.py'])
    with pytest.raises(ValueError, match='conflicting path'):
        app.tree.retry(cid, rid, rid, node['id'])
    app.tree.cancel(cid, rid, rid, other['id'])
    new = app.tree.retry(cid, rid, rid, node['id'])
    claim(env, new)
    assert app.tree.assert_path(cid, rid, new['id'], 'x.py', 2)['generation'] == 2


def test_child_wait_blocks_until_peer_settles_and_recomputes_required_flag(env):
    app, cid, rid = env
    a = spawn(env, tools=['Read', 'WaitForAgents'])
    b = spawn(env)
    claim(env, a)
    claim(env, b, 'worker-2')
    wait = app.tree.wait(cid, rid, a['id'], 'wait', [b['id']], generation=1, owner='worker-1')
    assert wait['required'] and app.tree.get(cid, rid, a['id'])['status'] == 'waiting_parent'
    settle(env, b, 'worker-2')
    assert app.tree.get(cid, rid, a['id'])['status'] == 'running'
    again = app.tree.wait(cid, rid, a['id'], 'wait', [b['id']], generation=1, owner='worker-1')
    assert again['status'] == 'ready' and again['required'] is False


def test_terminal_dismissal_does_not_claim_accepted_success(env):
    app, cid, rid = env
    child = spawn(env)
    claim(env, child)
    with pytest.raises(ValueError, match='Settle'):
        app.tree.dismiss(cid, rid, rid, child['id'], 1)
    app.tree.cancel(cid, rid, rid, child['id'])
    assert app.tree.dismiss(cid, rid, rid, child['id'], 1)['review'] == 'dismissed'
    assert app.tree.get(cid, rid, child['id'])['review'] == 'dismissed'


def test_pre_tree_root_work_is_conservatively_backfilled_and_explicit_limits_keep_usage(env):
    app, cid, rid = env
    app.store.query('UPDATE runs SET steps=2 WHERE id=?', (rid,))
    budget = app.tree.budget(cid, rid)
    assert budget['pre_tree_estimate'] == {'dispatches': 6, 'model_tokens': 6 * 4096}
    config = app.runs.get(rid)['config']
    config['limits']['max_steps'] = 80
    app.store.query('UPDATE runs SET config=? WHERE id=?', (json.dumps(config), rid))
    revised = app.tree.budget(cid, rid)
    assert revised['root_limits']['max_dispatches'] == 240
    assert revised['used'] == budget['used']


def test_default_child_tokens_follow_pinned_profile_window(env):
    app, cid, rid = env
    config = app.runs.get(rid)['config']
    config['route']['context'] = 65536
    app.store.query('UPDATE runs SET config=? WHERE id=?', (json.dumps(config), rid))
    child = app.tree.spawn(cid, rid, None, 'Read', 'reader', ['Read'], {'max_steps': 2, 'max_seconds': 30})
    assert child['limits']['model_tokens'] == 2 * 3 * 65536


def test_global_logical_steps_cannot_use_retry_headroom_as_extra_child_steps(env):
    app, cid, rid = env
    config = app.runs.get(rid)['config']
    config['limits']['max_steps'] = 4
    app.store.query('UPDATE runs SET config=? WHERE id=?', (json.dumps(config), rid))
    a = spawn(env, limits={'max_steps': 1, 'max_seconds': 20, 'model_tokens': 1000})
    b = spawn(env, limits={'max_steps': 1, 'max_seconds': 20, 'model_tokens': 1000})
    claim(env, a)
    claim(env, b, 'worker-2')
    for i, node in enumerate((a, b)):
        assert app.tree.reserve_model(cid, rid, node['id'], 1, 1, 1, 'logical-' + str(i))['admitted']
        app.tree.finish_model(cid, rid, node['id'], 1, 'logical-' + str(i))
    assert app.tree.budget(cid, rid)['used']['steps'] == 2
    for i in range(2):
        assert app.tree.reserve_model(cid, rid, rid, 0, 1, 1, 'root-' + str(i))['admitted']
        app.tree.finish_model(cid, rid, rid, 0, 'root-' + str(i))
    assert not app.tree.reserve_model(cid, rid, rid, 0, 1, 1, 'extra-root')['admitted']
    assert app.tree.budget(cid, rid)['used']['dispatches'] == 4


def test_nested_parent_answer_requires_parent_capability_and_current_lease(env):
    app, cid, rid = env
    parent = spawn(env, tools=['Read', 'SpawnAgent', 'AskParentQuestion', 'AnswerAgentQuestion'])
    claim(env, parent)
    child = spawn(env, parent=parent['id'], tools=['Read', 'AskParentQuestion'])
    claim(env, child, 'worker-2')
    q = app.tree.ask_parent(cid, rid, child['id'], 1, 'worker-2', 'ask', [{'id': 'scope', 'title': 'Which source?'}])
    with pytest.raises(ValueError, match='lease|stale'):
        app.tree.answer_parent(cid, rid, parent['id'], q['question_id'], {'scope': {'text': 'src'}})
    answer = app.tree.answer_parent(cid, rid, parent['id'], q['question_id'], {'scope': {'text': 'src'}}, generation=1, owner='worker-1')
    assert answer['origin'] == 'agent'
    assert app.tree.parent_question(cid, rid, child['id'], q['question_id'])['answer']['scope']['text'] == 'src'


def test_patch_receipt_arguments_admit_bounded_source_but_cannot_gain_effects(env):
    app, cid, rid = env
    node = spawn(env, tools=['Read', 'ProposePatch'], paths=['x.py'])
    claim(env, node)
    args = {'file_path': 'x.py', 'content': 'x' * 80000}
    assert app.tree.begin_tool(cid, rid, node['id'], 1, 'worker-1', 'patch', 'ProposePatch', args) is None
    with pytest.raises(ValueError, match='capability'):
        app.tree.begin_tool(cid, rid, node['id'], 1, 'worker-1', 'write', 'Write', {'file_path': 'x.py', 'content': 'x'})


def test_root_retry_backoff_does_not_revoke_independent_child_work(env):
    app, cid, rid = env
    node = spawn(env)
    claim(env, node)
    app.runs.transition(rid, 'retry_wait')
    assert app.tree.heartbeat(cid, rid, node['id'], 1, 'worker-1')['generation'] == 1
    assert app.tree.reserve_model(cid, rid, node['id'], 1, 1, 1, 'child-during-backoff')['admitted']
    with pytest.raises(ValueError, match='running root'):
        app.tree.reserve_model(cid, rid, rid, 0, 1, 1, 'root-before-running')


def test_native_cancel_in_can_cascade_inside_existing_host_transaction(env):
    app, cid, rid = env
    parent = spawn(env, tools=['Read', 'SpawnAgent', 'AskParentQuestion'])
    claim(env, parent)
    child = spawn(env, parent=parent['id'], tools=['Read', 'AskParentQuestion'], paths=['x.py'])
    claim(env, child, 'worker-2')
    q = app.tree.ask_parent(cid, rid, parent['id'], 1, 'worker-1', 'ask', [{'id': 'scope', 'title': 'Which scope?'}])
    with app.tree._transaction() as db:
        db.execute("UPDATE agent_parent_questions SET status='promoted' WHERE id=?", (q['question_id'],))
        result = app.tree.cancel_in(db, cid, rid, rid, parent['id'])
    assert set(result['agent_ids']) == {parent['id'], child['id']}
    assert app.tree.parent_question(cid, rid, rid, q['question_id'])['status'] == 'cancelled'
    assert app.store.query('SELECT status FROM agent_path_leases')[0]['status'] == 'revoked'


def test_human_followup_wakes_exact_registered_root_wait_in_same_transaction(env):
    app, cid, rid = env
    child = spawn(env)
    claim(env, child)
    pending = app.tree.wait(cid, rid, rid, '1:wait', [child['id']])
    with app.tree._transaction() as db:
        app.interactions._input(db, rid, 'steering', 'human', {'text': 'Review the current result first'}, 'followup')
        app.tree._wake(db, rid)
        ready = Tree.resolve_wait_receipt(db, rid, '1:wait', pending)
    assert ready['reason'] == 'human_followup' and ready['required'] is False
    assert ready['input_ids'] == ['followup']
    assert app.tree.get(cid, rid, child['id'])['status'] == 'running'


def test_custom_input_budget_does_not_underreserve_full_provider_window(env):
    app, cid, rid = env
    config = app.runs.get(rid)['config']
    config['route'].update(context=5000000, budget=32000)
    app.store.query('UPDATE runs SET config=? WHERE id=?', (json.dumps(config), rid))
    budget = app.tree.budget(cid, rid)
    assert budget['root_limits']['model_tokens'] == 64 * 3 * 5000000
    child = app.tree.spawn(cid, rid, None, 'Read', 'reader', ['Read'], {'max_steps': 1, 'max_seconds': 30})
    assert child['limits']['model_tokens'] == 3 * 5000000


def test_releasing_mid_tool_requires_explicit_new_generation_without_old_replay(env):
    app, cid, rid = env
    child = spawn(env)
    claim(env, child)
    app.tree.begin_tool(cid, rid, child['id'], 1, 'worker-1', 'read', 'Read', {'file_path': 'x.py'})
    app.tree.release(cid, rid, child['id'], 1, 'worker-1')
    assert app.tree.receipts(cid, rid, child['id'], 1)[0]['state'] == 'uncertain'
    new = app.tree.retry(cid, rid, rid, child['id'])
    assert new['generation'] == 2
    assert app.store.query('SELECT state FROM agent_receipts')[0]['state'] == 'interrupted'
