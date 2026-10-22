"""Attributable provider usage: validated per dispatch, recorded once, unknown never zero.

Conservative reservations stay the only budget charges. Fixtures are the real
SQLite tree, a genuine loopback provider stream, and the real child scheduler
with a scripted transport. No live model, billing or credential is used.
"""

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from pebble.agent_provider import InferenceLane, attempt_usage, usage_probe
from pebble.agent_tree import Tree
from pebble.delegation_policy import USAGE_FIELDS, aggregate_usage, validate_usage
from pebble.interaction import RunInteractions
from pebble.permissions import DEFAULT
from pebble.runtime import RunJournal
from pebble.store import Store

from test_agent_manager import PROFILE, FakePinnedProvider, call, env, final, root, settled, spawn  # noqa: F401
from test_agent_provider import RequestJob, cloud_app, pin, stream_server  # noqa: F401


WINDOW = 4096
UNKNOWN = dict.fromkeys(USAGE_FIELDS)
MARKER = 'usage-private-phrase-91c2'


@pytest.fixture
def ledger(tmp_path):
    home = tmp_path / 'data'
    store = Store(home)
    runs = RunJournal(store, home)
    app = SimpleNamespace(store=store, runs=runs, interactions=RunInteractions(store, runs))
    app.tree = Tree(app)
    cid = store.new_chat(str(tmp_path))
    config = {'workspace': str(tmp_path), 'permissions': copy.deepcopy(DEFAULT), 'route': {'context': WINDOW},
              'limits': {'max_steps': 12, 'max_seconds': 300}}
    rid = runs.create(cid, 'Inspect source', config)
    runs.transition(rid, 'running')
    yield SimpleNamespace(app=app, tree=app.tree, cid=cid, rid=rid)
    runs.close()
    store.close()


def child(env, name='worker'):
    node = env.tree.spawn(env.cid, env.rid, None, 'Read source for ' + MARKER, name, ['Read'], {'max_steps': 4})
    env.tree.claim(env.cid, env.rid, node['id'], 1, 'owner-' + node['id'])
    return node


def admit(env, actor, ident, generation=None):
    generation = 0 if actor == env.rid else (generation or 1)
    admission = env.tree.reserve_model(env.cid, env.rid, actor, generation, WINDOW, 0, ident)
    assert admission['admitted']
    return admission


def finish(env, actor, ident, usage=None, failed=False, generation=None):
    generation = 0 if actor == env.rid else (generation or 1)
    return env.tree.finish_model(env.cid, env.rid, actor, generation, ident, failed=failed, usage=usage)


def by_actor(usage):
    return {(row['actor'], row['generation']): row for row in usage['by_actor']}


def rows(env, dispatch_id):
    return env.app.store.query('SELECT state,input_tokens,output_tokens,cached_tokens FROM agent_usage WHERE dispatch_id=?',
                               (dispatch_id,))


# Validation

@pytest.mark.parametrize('report,state,counts', [
    (None, 'unreported', (None, None, None)),
    ({}, 'unreported', (None, None, None)),
    ({'tokens_per_second': 3.5, 'rate_source': 'runtime'}, 'unreported', (None, None, None)),
    ({'input_tokens': 5, 'output_tokens': None}, 'partial', (5, None, None)),
    ({'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0}, 'reported', (0, 0, 0)),
    ({'input_tokens': 9, 'output_tokens': 1, 'cached_tokens': 9}, 'reported', (9, 1, 9)),
])
def test_validated_usage_keeps_true_zero_apart_from_unknown(report, state, counts):
    assert validate_usage(report) == (dict(zip(USAGE_FIELDS, counts)), state)


def test_no_attempts_is_a_true_zero_and_unknown_attempts_never_sum_to_zero():
    assert aggregate_usage([])['totals'] == dict.fromkeys(USAGE_FIELDS, 0)
    assert aggregate_usage([])['complete'] is True
    unknown = aggregate_usage([{'state': 'unrecorded', **UNKNOWN}])
    assert unknown['totals'] == UNKNOWN and unknown['known'] == dict.fromkeys(USAGE_FIELDS, 0)
    assert unknown['unknown_attempts'] == dict.fromkeys(USAGE_FIELDS, 1) and unknown['complete'] is False


MALFORMED = [
    {'input_tokens': -1, 'output_tokens': 3}, {'input_tokens': True, 'output_tokens': 3},
    {'input_tokens': 1.5, 'output_tokens': 3}, {'input_tokens': '12', 'output_tokens': 3},
    {'input_tokens': 10 ** 12, 'output_tokens': 3}, {'input_tokens': 10, 'output_tokens': 2, 'cached_tokens': 11},
    {'input_tokens': 10, 'output_tokens': float('nan')}, ['input_tokens', 10], 'input_tokens=10', 7,
]


@pytest.mark.parametrize('report', MALFORMED)
def test_malformed_usage_is_recorded_unknown_and_never_repaired(ledger, report):
    env = ledger
    admit(env, env.rid, 'root-1')
    steps = env.tree.budget(env.cid, env.rid)['used']['steps']
    done = finish(env, env.rid, 'root-1', copy.deepcopy(report))
    assert done['status'] == 'completed' and done['usage_state'] == 'invalid' and done['usage'] == UNKNOWN
    # The dispatch still settles; only its usage is unknown.
    assert env.tree.budget(env.cid, env.rid)['used']['steps'] == steps + 1
    assert rows(env, 'root-1') == [{'state': 'invalid', **UNKNOWN}]
    root_row = by_actor(env.tree.usage(env.cid, env.rid))[env.rid, 0]
    assert root_row['states'] == {'invalid': 1} and root_row['unknown_attempts'] == dict.fromkeys(USAGE_FIELDS, 1)


# Attribution and exactly-once settlement

def test_root_and_child_usage_is_attributed_per_actor_generation_and_attempt(ledger):
    env = ledger
    worker = child(env, name=MARKER)
    admit(env, env.rid, 'root-1')
    root_done = finish(env, env.rid, 'root-1', {'input_tokens': 300, 'output_tokens': 20, 'cached_tokens': 100,
                                                 'tokens_per_second': 41.5, 'rate_source': 'runtime'})
    assert (root_done['attempt'], root_done['usage_state']) == (1, 'reported')
    assert root_done['usage'] == {'input_tokens': 300, 'output_tokens': 20, 'cached_tokens': 100}
    assert admit(env, worker['id'], 'child-1')['attempt'] == 1
    first = finish(env, worker['id'], 'child-1', {'input_tokens': 50, 'output_tokens': 0}, failed=True)
    assert admit(env, worker['id'], 'child-2')['attempt'] == 2
    second = finish(env, worker['id'], 'child-2')
    assert (first['attempt'], first['status'], first['usage_state'], first['usage']['output_tokens']) == (1, 'failed', 'reported', 0)
    assert (second['attempt'], second['usage_state'], second['usage']) == (2, 'unreported', UNKNOWN)
    usage = env.tree.usage(env.cid, env.rid)
    actors = by_actor(usage)
    assert set(actors) == {(env.rid, 0), (worker['id'], 1)}
    assert actors[env.rid, 0]['role'] == 'root'
    assert actors[env.rid, 0]['totals'] == {'input_tokens': 300, 'output_tokens': 20, 'cached_tokens': 100}
    kid = actors[worker['id'], 1]
    assert (kid['role'], kid['depth'], kid['attempts']) == ('child', 1, 2)
    assert kid['states'] == {'reported': 1, 'unreported': 1}
    # The unreported attempt is unknown, not zero; the reported zero output stays a true zero.
    assert kid['totals'] == UNKNOWN
    assert kid['known'] == {'input_tokens': 50, 'output_tokens': 0, 'cached_tokens': 0}
    assert kid['unknown_attempts'] == {'input_tokens': 1, 'output_tokens': 1, 'cached_tokens': 2}
    assert usage['totals'] == UNKNOWN and usage['known'] == {'input_tokens': 350, 'output_tokens': 20, 'cached_tokens': 100}
    assert usage['complete'] is False and usage['cost_usd'] is None and usage['tracked'] is True
    assert usage['source'] == 'provider_reported_per_dispatch' and usage['pre_tree_root_steps'] == 0
    detail = env.tree.usage(env.cid, env.rid, worker['id'])
    assert [(a['attempt'], a['outcome'], a['state']) for a in detail['attempts_detail']] == [
        (1, 'failed', 'reported'), (2, 'completed', 'unreported')]
    assert set(by_actor(detail)) == {(worker['id'], 1)}
    # Reservations stay the budget charge; measured usage never replaces or refunds them.
    budget = env.tree.budget(env.cid, env.rid)
    assert budget['used']['model_tokens'] == 3 * WINDOW and budget['used']['dispatches'] == 3
    assert budget['usage']['known'] == usage['known']
    # Views carry host IDs, counts and states only; no task text or model-chosen names.
    assert MARKER not in json.dumps(usage) + json.dumps(detail) + json.dumps(budget)


def test_duplicate_late_and_foreign_settlement_never_rewrites_usage_or_caps(ledger):
    env = ledger
    worker, other = child(env, 'one'), child(env, 'two')
    admit(env, env.rid, 'root-1')
    finish(env, env.rid, 'root-1', {'input_tokens': 10, 'output_tokens': 2})
    before = env.tree.budget(env.cid, env.rid)['used']
    again = finish(env, env.rid, 'root-1', {'input_tokens': 999, 'output_tokens': 999})
    assert again['duplicate'] and again['status'] == 'completed'
    assert again['usage'] == {'input_tokens': 10, 'output_tokens': 2, 'cached_tokens': None}
    assert env.tree.budget(env.cid, env.rid)['used'] == before
    admit(env, worker['id'], 'failed-1')
    failed = finish(env, worker['id'], 'failed-1', {'input_tokens': 40, 'output_tokens': 1}, failed=True)
    assert failed['status'] == 'failed' and failed['usage_state'] == 'reported'
    steps = env.tree.budget(env.cid, env.rid)['used']['steps']
    repeat = finish(env, worker['id'], 'failed-1', None, failed=False)
    # A repeated settlement cannot turn a failed attempt into a completed step.
    assert repeat['duplicate'] and repeat['status'] == 'failed' and repeat['usage']['input_tokens'] == 40
    assert env.tree.budget(env.cid, env.rid)['used']['steps'] == steps
    admit(env, other['id'], 'other-1')
    for actor, generation in ((worker['id'], 1), (env.rid, 0)):
        with pytest.raises(ValueError, match='not found'):
            env.tree.finish_model(env.cid, env.rid, actor, generation, 'other-1', usage={'input_tokens': 1, 'output_tokens': 1})
    assert not rows(env, 'other-1')
    env.tree.release(env.cid, env.rid, other['id'], 1, 'owner-' + other['id'])
    late = finish(env, other['id'], 'other-1', {'input_tokens': 5, 'output_tokens': 5})
    assert late['duplicate'] and late['status'] == 'interrupted'
    assert (late['usage_state'], late['usage']) == ('unrecorded', UNKNOWN)
    assert not rows(env, 'other-1')
    assert by_actor(env.tree.usage(env.cid, env.rid))[other['id'], 1]['states'] == {'unrecorded': 1}
    assert env.app.store.query('SELECT count(*) AS n FROM agent_usage')[0]['n'] == 2


def test_restart_leaves_in_flight_usage_unrecorded_and_retry_starts_new_attempts(ledger):
    env = ledger
    worker = child(env)
    admit(env, env.rid, 'root-in-flight')
    admit(env, worker['id'], 'child-in-flight')
    restarted = Tree(env.app)
    # Recovery writes no usage rows; the ended dispatches simply stay unknown.
    assert not env.app.store.query('SELECT 1 FROM agent_usage')
    usage = restarted.usage(env.cid, env.rid)
    assert usage['states'] == {'unrecorded': 2} and usage['totals'] == UNKNOWN
    late = restarted.finish_model(env.cid, env.rid, env.rid, 0, 'root-in-flight', usage={'input_tokens': 7, 'output_tokens': 7})
    assert late['duplicate'] and late['usage_state'] == 'unrecorded'
    assert restarted.budget(env.cid, env.rid)['used']['dispatches'] == 2
    restarted.retry(env.cid, env.rid, env.rid, worker['id'])
    restarted.claim(env.cid, env.rid, worker['id'], 2, 'owner-' + worker['id'])
    assert restarted.reserve_model(env.cid, env.rid, worker['id'], 2, WINDOW, 0, 'child-retry')['attempt'] == 1
    restarted.finish_model(env.cid, env.rid, worker['id'], 2, 'child-retry', usage={'input_tokens': 30, 'output_tokens': 3})
    actors = by_actor(restarted.usage(env.cid, env.rid))
    assert actors[worker['id'], 1]['states'] == {'unrecorded': 1}
    assert actors[worker['id'], 2]['states'] == {'reported': 1} and actors[worker['id'], 2]['totals']['input_tokens'] == 30
    assert actors[env.rid, 0]['states'] == {'unrecorded': 1}
    assert restarted.budget(env.cid, env.rid)['used']['dispatches'] == 3


def test_untracked_root_reports_unknown_rather_than_zero(ledger):
    env = ledger
    usage = env.tree.usage(env.cid, env.rid)
    assert usage['tracked'] is False and usage['attempts'] == 0
    assert usage['totals'] == UNKNOWN and usage['complete'] is False


def test_forgetting_a_chat_removes_its_usage_rows(ledger):
    env = ledger
    admit(env, env.rid, 'root-1')
    finish(env, env.rid, 'root-1', {'input_tokens': 1, 'output_tokens': 1})
    with env.tree._transaction() as db:
        env.tree.forget(db, env.cid)
    assert not env.app.store.query('SELECT 1 FROM agent_usage')


# A genuine loopback provider stream

def test_real_pinned_provider_attributes_usage_to_exactly_one_attempt(tmp_path, stream_server):
    app, profile = cloud_app(tmp_path, stream_server.url)
    provider = pin(app, profile, 'usage-owner', InferenceLane())
    try:
        root_probe = usage_probe(app.model)
        assert root_probe[0] == 'binding' and provider.usage_serial == 0
        stream_server.release('alpha').set()
        probe = usage_probe(provider)
        assert probe == ('serial', 0)
        first = RequestJob(provider, 'alpha').join()
        assert first.error is None and provider.usage_serial == 1
        report = attempt_usage(provider, probe)
        assert report == {'input_tokens': 105, 'output_tokens': 5, 'cached_tokens': None}
        assert validate_usage(report)[1] == 'reported'
        # A failed attempt reports nothing; the earlier report is never reused for it.
        stream_server.release('incomplete').set()
        probe = usage_probe(provider)
        second = RequestJob(provider, 'incomplete').join()
        assert second.error is not None and provider.usage_serial == 2
        assert attempt_usage(provider, probe) is None
        # A stale probe spanning two requests is ambiguous and attributes nothing.
        assert attempt_usage(provider, ('serial', 0)) is None
        # Child streams never bind the root provider's report.
        assert attempt_usage(app.model, root_probe) is None
        assert app.model.last_usage == {'input_tokens': 9999}
    finally:
        provider.close()


# The real child scheduler and root attempts

class ReportingProvider(FakePinnedProvider):
    """The executor fixture transport, binding a fresh usage report per attempt as providers do."""

    def complete(self, messages, tools, cancel, token, effort):
        result = super().complete(messages, tools, cancel, token, effort)
        usage = result.pop('_usage', None)
        if usage is not None:
            self.last_usage = usage
        return result


def test_real_executor_attributes_child_usage_and_isolates_the_root(env):
    env.app.agents.provider_factory = lambda *args, **kwargs: ReportingProvider(env, *args, **kwargs)
    cid, rid = root(env)
    (env.project / 'alpha.py').write_text('alpha = 1\n')
    alpha = spawn(env, cid, rid, 'alpha', [
        {**call('Read', {'file_path': 'alpha.py'}, 'read'), '_usage': {'input_tokens': 120, 'output_tokens': 7, 'cached_tokens': 64}},
        {**final('Alpha result.'), '_usage': {'input_tokens': 140, 'output_tokens': 0}},
    ])
    # beta's transport keeps only its stale constructor report, which is never attributed.
    beta = spawn(env, cid, rid, 'beta', [final('Beta result.')])
    assert alpha['limit_sources'] == dict.fromkeys(('max_steps', 'max_seconds', 'model_tokens'), 'explicit')
    assert alpha['delegation']['recommendation'] in {'solo', 'small_child', 'delegation_fits'}
    a, b = settled(env, cid, rid, alpha), settled(env, cid, rid, beta)
    assert a['result']['usage'] == {'input_tokens': 140, 'output_tokens': 0, 'cached_tokens': None}
    assert b['result']['usage'] == UNKNOWN
    root_report = {'input_tokens': 900, 'output_tokens': 30, 'cached_tokens': 512}
    turn = [{'role': 'user', 'content': 'Review the delegated results.'}]
    with env.app.agents.root_attempt(cid, rid, turn, [], threading.Event()):
        env.app.model.last_usage = dict(root_report)
    with env.app.agents.root_attempt(cid, rid, turn, [], threading.Event()):
        # A child transport reporting during a root attempt is never root usage.
        env.providers['alpha'].last_usage = {'input_tokens': 5, 'output_tokens': 5}
    actors = by_actor(env.app.agents.tree.usage(cid, rid))
    assert actors[alpha['id'], 1]['totals'] == {'input_tokens': 260, 'output_tokens': 7, 'cached_tokens': None}
    assert actors[alpha['id'], 1]['known']['cached_tokens'] == 64
    assert actors[beta['id'], 1]['states'] == {'unreported': 1}
    assert actors[rid, 0]['states'] == {'reported': 1, 'unreported': 1}
    assert actors[rid, 0]['known'] == root_report and actors[rid, 0]['totals']['input_tokens'] is None
    detail = env.app.dispatch('agent_get', {'chat_id': cid, 'run_id': rid, 'agent_id': alpha['id']})['usage']
    assert [(item['attempt'], item['state']) for item in detail['attempts_detail']] == [(1, 'reported'), (2, 'reported')]
    budget = env.app.agents.list(cid, rid)['budget']
    # Reservations: three child and two root attempts at the pinned window, independent of usage.
    assert budget['used']['model_tokens'] == 5 * PROFILE['context']
    assert budget['usage']['cost_usd'] is None and budget['usage']['known']['input_tokens'] == 260 + 900
