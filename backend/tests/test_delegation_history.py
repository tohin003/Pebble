"""Host-recorded delegation history: content-free, salted per project, bounded and advisory only.

Store-level tests use the real SQLite journal; no App, model or network.
"""

import copy
import hashlib
import inspect
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import delegation_history as dh
from pebble import delegation_policy
from pebble.delegation_history import DelegationHistory, HISTORY_DDL, summarize
from pebble.permissions import DEFAULT
from pebble.runtime import RunJournal
from pebble.store import Store

MARKER = 'history-private-phrase-91c2'


@pytest.fixture
def env(tmp_path):
    home = tmp_path / 'data'
    store = Store(home)
    runs = RunJournal(store, home)
    yield SimpleNamespace(store=store, runs=runs, history=DelegationHistory(store), tmp=tmp_path)
    runs.close()
    store.close()


def run_in(env, project_name='project', text='Inspect source'):
    project = env.tmp / project_name
    project.mkdir(exist_ok=True)
    cid = env.store.new_chat(str(project))
    config = {'workspace': str(project), 'workspace_identity': [1, 2], 'permissions': copy.deepcopy(DEFAULT),
              'route': {'context': 4096}, 'limits': {'max_steps': 12, 'max_seconds': 300}}
    rid = env.runs.create(cid, text, config)
    env.runs.transition(rid, 'stopped')  # one live root at a time; history only reads the run config
    return cid, rid


def rows(env):
    return env.store.query('SELECT * FROM delegation_history ORDER BY rowid')


def test_advice_starts_empty_then_follows_recorded_outcomes(env):
    _, rid = run_in(env)
    assert env.history.advice(rid, 'write')['recommendation'] == 'no_history'
    # Nothing is written, not even the salt, until the host records an outcome.
    assert not env.store.query('SELECT 1 FROM delegation_salt') and not rows(env)
    env.history.record(rid, 'write', 'admitted', 'a:1')
    env.history.record(rid, 'write', 'accepted', 'a:1', 'completed', 3, 900)
    paid = env.history.advice(rid, 'write')
    assert paid['recommendation'] == 'delegation_paid' and paid['accepted'] == 1 and paid['admitted'] == 1
    assert (paid['median_child_steps'], paid['median_child_tokens']) == (3, 900)
    assert paid['authority'] == 'none' and paid['source'] == 'host_recorded'
    # Kinds are separate: read-only history is still empty.
    assert env.history.advice(rid, 'read_only')['recommendation'] == 'no_history'
    for ident in ('b:1', 'c:1', 'd:1'):
        env.history.record(rid, 'write', 'dismissed', ident, 'budget_exhausted', 8, None)
    lost = env.history.advice(rid, 'write')
    assert lost['recommendation'] == 'solo' and (lost['accepted'], lost['dismissed']) == (1, 3)
    assert 'Solo work is recommended' in lost['note'] and 'never permission' in lost['note']


def test_repeated_refusals_without_an_accepted_delegation_recommend_solo(env):
    _, rid = run_in(env)
    env.history.record(rid, 'read_only', 'refused', 'call-1', 'shared_steps')
    assert env.history.advice(rid, 'read_only')['recommendation'] == 'insufficient_history'
    env.history.record(rid, 'read_only', 'refused', 'call-2', 'file_set')
    assert env.history.advice(rid, 'read_only')['recommendation'] == 'solo'


@pytest.mark.parametrize('rows_in,expected', [
    ([], 'no_history'),
    ([('admitted', None, None)], 'insufficient_history'),
    ([('accepted', 2, 10), ('dismissed', 9, None)], 'delegation_paid'),
    ([('accepted', 2, 10), ('dismissed', 9, None), ('dismissed', 9, None)], 'solo'),
    ([('refused', None, None), ('refused', None, None)], 'solo'),
    ([('refused', None, None), ('refused', None, None), ('accepted', 3, None)], 'delegation_paid'),
])
def test_summary_rules_are_pure_and_never_authoritative(rows_in, expected):
    advice = summarize('write', rows_in)
    assert advice['recommendation'] == expected and advice['authority'] == 'none'
    assert advice['note'].endswith(dh.NOTE_TAIL)


def test_rows_are_content_free_salted_and_hold_no_ids(env):
    cid, rid = run_in(env, 'secret-' + MARKER, 'Summarise ' + MARKER)
    agent = 'f' * 32
    env.history.record(rid, 'write', 'admitted', agent + ':1')
    env.history.record(rid, 'write', 'refused', 'spawn-call-' + MARKER, 'file_set')
    env.history.record(rid, 'write', 'accepted', agent + ':1', 'completed', 4, 1200)
    dumped = json.dumps(rows(env)) + json.dumps(env.store.query('SELECT * FROM delegation_salt'))
    workspace = str(env.tmp / ('secret-' + MARKER))
    for forbidden in (MARKER, workspace, rid, cid, agent, 'spawn-call'):
        assert forbidden not in dumped
    stored = rows(env)
    assert {row['kind'] for row in stored} == {'write'}
    assert {row['outcome'] for row in stored} == {'admitted', 'refused', 'accepted'}
    assert {row['detail'] for row in stored} <= dh.DETAILS | {None}
    # The scope is salted: never the plain digest of the project path.
    plain = hashlib.sha256(workspace.encode()).hexdigest()
    assert all(row['scope'] != plain and len(row['scope']) == 64 for row in stored)
    assert len({row['scope'] for row in stored}) == 1


def test_only_fixed_labels_and_bounded_counts_are_accepted(env):
    _, rid = run_in(env)
    for kwargs in ({'kind': 'research'}, {'outcome': 'succeeded'}, {'detail': MARKER},
                   {'child_steps': -1}, {'child_tokens': 2 ** 40}, {'child_steps': True}, {'event': ''},
                   {'event': 'x' * 401}):
        args = {'kind': 'write', 'outcome': 'accepted', 'event': 'a:1', **kwargs}
        with pytest.raises(ValueError):
            env.history.record(rid, args.pop('kind'), args.pop('outcome'), args.pop('event'), **args)
    assert not rows(env)
    with pytest.raises(ValueError):
        env.history.advice(rid, 'swarm')
    with pytest.raises(ValueError, match='Root run not found'):
        env.history.record('missing-run', 'write', 'admitted', 'a:1')
    # The table itself refuses free-text kinds and outcomes written by any other path.
    with env.store.lock, pytest.raises(sqlite3.IntegrityError):
        env.store.db.execute("INSERT INTO delegation_history VALUES('s','e','r',?,'admitted',NULL,NULL,NULL,0)", (MARKER,))
    env.store.db.rollback()


def test_each_event_is_recorded_once(env):
    _, rid = run_in(env)
    assert env.history.record(rid, 'read_only', 'admitted', 'a:1') is True
    assert env.history.record(rid, 'read_only', 'admitted', 'a:1') is False
    assert env.history.record(rid, 'read_only', 'accepted', 'a:1', 'completed', 2, None) is True
    assert len(rows(env)) == 2


def test_history_is_bounded_per_scope_and_across_scopes(env, monkeypatch):
    monkeypatch.setattr(dh, 'MAX_EVENTS', 5)
    monkeypatch.setattr(dh, 'MAX_SCOPES', 3)
    _, rid = run_in(env, 'busy')
    for index in range(9):
        env.history.record(rid, 'write', 'admitted', 'busy:' + str(index))
    assert len(rows(env)) == 5
    others = [run_in(env, 'project-' + str(index))[1] for index in range(4)]
    for index, other in enumerate(others):
        env.history.record(other, 'read_only', 'refused', 'call-' + str(index), 'shared_steps')
    scopes = {row['scope'] for row in rows(env)}
    assert len(scopes) == 3
    # The least recently recorded scopes went first; the newest three remain.
    assert env.history.advice(others[-1], 'read_only')['refused'] == 1
    assert env.history.advice(rid, 'write')['recommendation'] == 'no_history'


def test_projects_are_separate_scopes_and_chats_in_one_project_share_one(env):
    _, first = run_in(env, 'alpha')
    _, second = run_in(env, 'alpha')
    _, other = run_in(env, 'beta')
    env.history.record(first, 'write', 'accepted', 'a:1', 'completed', 3, None)
    assert env.history.advice(second, 'write')['accepted'] == 1
    assert env.history.advice(other, 'write')['recommendation'] == 'no_history'


def test_forgetting_a_chat_drops_only_its_runs(env):
    cid, rid = run_in(env, 'alpha')
    _, kept = run_in(env, 'alpha')
    env.history.record(rid, 'write', 'accepted', 'a:1', 'completed', 3, None)
    env.history.record(kept, 'write', 'dismissed', 'b:1', 'failed', 2, None)
    with env.store.lock, env.store.db as db:
        env.history.forget(db, cid)
    advice = env.history.advice(kept, 'write')
    assert (advice['accepted'], advice['dismissed']) == (0, 1)


def test_history_never_reaches_the_admission_gate():
    # The gate's inputs are host budget numbers, the task kind and permission-derived approvals.
    assert list(inspect.signature(delegation_policy.admission).parameters) == ['ledger', 'kind', 'approvals', 'own']
    backend = Path(delegation_policy.__file__).parent
    for module in ('delegation_policy.py', 'agent_tree.py'):
        assert 'delegation_history' not in (backend / module).read_text()


def test_ddl_is_idempotent_and_matches_the_created_schema(env):
    with env.store.lock, env.store.db as db:
        db.executescript(HISTORY_DDL)
    names = {r['name'] for r in env.store.query("SELECT name FROM sqlite_master WHERE name LIKE 'delegation_%'")}
    assert names == {'delegation_history', 'delegation_history_scope', 'delegation_salt'}
