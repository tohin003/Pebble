"""Actual attempt-ledger attribution reaches public evaluation metrics."""
import copy

import pytest

from pebble.app import App
from pebble.agent_manager import Agents
from pebble.evaluation import _child_usage

PROFILE = {'id':'usage-fixture','kind':'openai','name':'Fixture','url':'https://unused.invalid',
           'model':'fixture','context':8192,'budget':4096,'tools':True}


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(Agents, 'start', lambda self: None)
    app = App(tmp_path/'home', tmp_path/'resources', lambda *args: None)
    monkeypatch.setattr(app.model, 'current', lambda:copy.deepcopy(PROFILE))
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], 'Inspect owned evidence.', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    try:
        yield app, chat['id'], rid
    finally:
        app.close()


def child(app, cid, rid):
    node = app.agents.tree.spawn(cid, rid, None, 'Report findings.', 'reader', ['ReadRunEvidence'],
                                {'max_steps':2,'max_seconds':10,'model_tokens':16384})
    app.agents.tree.claim(cid, rid, node['id'], 1, 'fixture-owner')
    return node


def report(app, cid, rid, node, ident, usage):
    assert app.agents.tree.reserve_model(cid, rid, node['id'], 1, 8192, 0, ident)['admitted']
    app.agents.tree.finish_model(cid, rid, node['id'], 1, ident, usage=usage)


def test_known_child_usage_is_exact_and_excludes_root(host):
    app, cid, rid = host
    node = child(app,cid,rid)
    report(app,cid,rid,node,'attempt-1',{'input_tokens':100,'output_tokens':10,'cached_tokens':20})
    assert app.agents.tree.reserve_model(cid,rid,rid,0,8192,0,'root-attempt')['admitted']
    app.agents.tree.finish_model(cid,rid,rid,0,'root-attempt',usage={'input_tokens':999,'output_tokens':80,'cached_tokens':0})
    usage = _child_usage(app,cid,rid,[node])
    assert usage['child_input_tokens'] == 100 and usage['child_output_tokens'] == 10
    assert usage['child_tokens'] == 110 and usage['child_calls'] == 1
    assert usage['child_cached_tokens'] == 20 and usage['child_usage_complete']
    assert usage['child_unknown_attempts'] == dict.fromkeys(('input_tokens','output_tokens','cached_tokens'),0)


def test_missing_child_attempt_makes_exact_total_unknown_and_keeps_known_lower_bound(host):
    app,cid,rid = host
    node = child(app,cid,rid)
    report(app,cid,rid,node,'known',{'input_tokens':100,'output_tokens':10,'cached_tokens':20})
    report(app,cid,rid,node,'unknown',None)
    usage = _child_usage(app,cid,rid,[node])
    assert usage['child_tokens'] is None and usage['child_input_tokens'] is None
    assert usage['child_calls'] == 2 and not usage['child_usage_complete']
    assert usage['child_known_tokens'] == {'input_tokens':100,'output_tokens':10,'cached_tokens':20}
    assert usage['child_unknown_attempts'] == dict.fromkeys(('input_tokens','output_tokens','cached_tokens'),1)


def test_repeated_settlement_does_not_inflate_reported_usage(host):
    app,cid,rid = host
    node = child(app,cid,rid)
    report(app,cid,rid,node,'once',{'input_tokens':100,'output_tokens':10,'cached_tokens':20})
    app.agents.tree.finish_model(cid,rid,node['id'],1,'once',usage={'input_tokens':9000,'output_tokens':9000,'cached_tokens':9000})
    assert _child_usage(app,cid,rid,[node])['child_tokens'] == 110


def test_queued_child_with_no_admission_has_zero_measured_spend(host):
    app,cid,rid = host
    node = child(app,cid,rid)
    usage = _child_usage(app,cid,rid,[node])
    assert usage['child_tokens'] == 0 and usage['child_calls'] == 0 and usage['child_usage_complete']
