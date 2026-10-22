"""Per-layer consent stays narrowed for the whole owning run."""
import pytest

from pebble.app import App
from pebble.tool_catalogue import TOOLS


@pytest.fixture
def app(tmp_path):
    value = App(tmp_path/'home',tmp_path/'resources',lambda *args:None)
    try:
        yield value
    finally:
        value.close()


def test_episode_layer_enabled_after_run_start_does_not_backfill(app):
    app.store.set_setting('memory_layers', {'episodic':False})
    chat = app.new_chat()
    rid = app.runs.create(chat['id'],'Synthetic task.', app.runtime_config(chat,False))
    app.learning.begin_run(rid)
    assert app.learning.consent(rid,'episodic') is False
    app.store.set_setting('memory_layers', {'episodic':True})
    app.memory.episode(chat['id'],'Synthetic task.',None,[],outcome='failed',run_id=rid)
    assert not app.store.query("SELECT id FROM memories WHERE kind='episodic' AND chat_id=?",(chat['id'],))


def test_direct_outcome_observation_checks_owning_run_layer_consent(app):
    app.store.set_setting('memory_layers', {'episodic':False})
    chat = app.new_chat()
    rid = app.runs.create(chat['id'],'Synthetic task.',app.runtime_config(chat,False))
    app.learning.begin_run(rid)
    app.runs.begin_tool(rid,'refusal','Read',{'file_path':'missing.txt'})
    app.runs.end_tool(rid,'refusal',{'error':'Synthetic host refusal.'})
    app.store.set_setting('memory_layers', {'episodic':True})
    app.memory.observe_outcomes(chat['id'],None,rid,[],'failed')
    assert not app.store.query("SELECT id FROM memories WHERE kind='episodic' AND chat_id=?",(chat['id'],))


def test_native_recall_inspection_is_scoped_and_not_an_agent_tool(app):
    first, other = app.new_chat(), app.new_chat()
    marker = 'NativeRecall7421'
    ident = app.memory.put('semantic', marker+' is a synthetic port.',scope='chat:'+first['id'],chat_id=first['id'])
    own = app.dispatch('memory_recall',{'id':first['id'],'text':marker,'budget':2000})
    assert ident in {r.get('id') for r in own['items']}
    assert marker not in str(own['diagnostics'])
    foreign = app.dispatch('memory_recall',{'id':other['id'],'text':marker,'budget':2000})
    assert ident not in {r.get('id') for r in foreign['items']}
    assert not any(t['function']['name']=='memory_recall' for t in TOOLS)
