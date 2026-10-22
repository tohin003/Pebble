"""A macro may mirror an action result; the admitted physical tool owns it."""
from pebble.store import Store
from pebble.runtime import RunJournal, RunLimits
from pebble.interaction import RunInteractions


def setup(home):
    store = Store(home)
    runs = RunJournal(store, home)
    interactions = RunInteractions(store, runs)
    cid = store.new_chat()
    rid = runs.create(cid, 'Verify the source.', {'limits': RunLimits().as_dict()})
    runs.transition(rid, 'running')
    runs.begin_tool(rid, 'outer', 'ExecuteAgentEffect', {'request_id': 'request'})
    runs.begin_tool(rid, 'outer:effect', 'RunTests', {'recipe': 'python-unittest'})
    aid = store.action(cid, 'tests', {'recipe': 'python-unittest'})
    runs.bind_action(rid, 'outer:effect', aid, 'tests')
    pending = {'status': 'awaiting_user_approval', 'action_id': aid, 'kind': 'tests'}
    runs.end_tool(rid, 'outer:effect', pending)
    runs.end_tool(rid, 'outer', {**pending, 'request_id': 'request', 'effect_receipt_id': 'outer:effect'})
    return store, runs, interactions, cid, rid, aid


def test_physical_approval_owner_survives_restart_and_macro_result_mirroring(tmp_path):
    store, runs, _, cid, rid, aid = setup(tmp_path / 'home')
    runs.close(); store.close()
    store = Store(tmp_path / 'home')
    runs = RunJournal(store, tmp_path / 'home')
    interactions = RunInteractions(store, runs)
    try:
        owner = runs.begin_approval(cid, aid)
        assert owner['call_id'] == 'outer:effect'
        receipt = runs.commit_action(aid, {'recipe': 'python-unittest'}, {'exit_code': 0}, 'tests', owner)
        interactions.action_resolution(cid, aid, receipt)
        tools = {r['call_id']: r for r in runs.tools(rid)}
        assert tools['outer:effect']['result']['status'] == 'applied'
        assert tools['outer']['result']['request_id'] == 'request'
        assert tools['outer']['result']['receipt_id'] == 'outer'
        assert tools['outer']['result']['status'] == 'applied'
        runs.forget(cid)
        assert store.query('SELECT * FROM run_action_owners') == []
    finally:
        runs.close(); store.close()


def test_pre_ledger_macro_receipt_cannot_take_physical_tool_ownership(tmp_path):
    store, runs, _, cid, _, aid = setup(tmp_path / 'home')
    try:
        store.query('DELETE FROM run_action_owners')
        assert runs.begin_approval(cid, aid)['call_id'] == 'outer:effect'
    finally:
        runs.close(); store.close()


def test_inconsistent_durable_action_binding_fails_closed(tmp_path):
    import pytest
    store, runs, _, cid, _, aid = setup(tmp_path / 'home')
    try:
        store.query("UPDATE run_action_owners SET call_id='missing' WHERE action_id=?", (aid,))
        with pytest.raises(ValueError, match='ownership record is inconsistent'):
            runs.begin_approval(cid, aid)
        assert store.actions(cid)[0]['status'] == 'pending'
    finally:
        runs.close(); store.close()
