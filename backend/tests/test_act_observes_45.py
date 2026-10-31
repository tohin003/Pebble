"""Phase 45 (1.9.14, D140 from audit round R7): an element act returns the window after it as the next observation, so
the model acts again without a ScreenObserve turn (R7: 10 model steps for one Chess move, about half of them looks)."""
import json

from pebble import runner
from pebble import screen_session as ss


def test_the_tool_descriptions_name_the_observation_an_act_returns():
    assert len(json.dumps(ss.SCHEMAS)) < 2600  # F262 (1.9.18): drag_at's two coordinates and words added 155
    act = next(t for t in ss.SCHEMAS if t['function']['name'] == 'ScreenAct')['function']['description']
    look = next(t for t in ss.SCHEMAS if t['function']['name'] == 'ScreenObserve')['function']['description']
    assert "the result's observation is the window after" in act and 'observe again before acting' not in look
    assert ss.AFTER_ACT.strip().startswith('The observation field is the window after this act')
    assert 'first of two presses' in ss.UNCHANGED_ROWS and 'Observe again' not in ss.UNCHANGED_ROWS


def act_message(oid, rows):
    return {'role': 'tool', 'tool_call_id': 'call-' + oid, 'content': json.dumps(
        {'status': 'done', 'verdict': 'occurred', 'target': 'e4', 'changes': [{'before': 'a', 'after': 'b'}],
         'observation': {'observation_id': oid, 'elements': rows}})}


def test_an_acts_observation_is_superseded_but_its_outcome_stays():
    observed, rows = {}, [{'role': 'AXButton', 'label': str(n)} for n in range(40)]
    look = {'role': 'tool', 'tool_call_id': 'call-o1', 'content': json.dumps({'observation_id': 'o1', 'elements': rows})}
    runner.supersede_observation(observed, 's', look, {'observation_id': 'o1'})
    first = act_message('o2', rows)
    runner.supersede_observation(observed, 's', first, json.loads(first['content'])['observation'], nested=True)
    assert json.loads(look['content'])['superseded'] is True, 'the look before the act became a stub'
    second = act_message('o3', rows)
    runner.supersede_observation(observed, 's', second, json.loads(second['content'])['observation'], nested=True)
    kept = json.loads(first['content'])
    assert kept['verdict'] == 'occurred' and kept['target'] == 'e4' and kept['changes'], "the act's outcome stays"
    assert kept['observation'] == {'superseded': True, 'observation_id': 'o2', 'note': runner.SUPERSEDED}
    assert 'elements' in json.loads(second['content'])['observation'], 'the newest keeps its rows'
    look2 = {'role': 'tool', 'tool_call_id': 'call-o4', 'content': json.dumps({'observation_id': 'o4', 'elements': rows})}
    runner.supersede_observation(observed, 's', look2, {'observation_id': 'o4'})
    assert json.loads(second['content'])['observation']['superseded'] is True and \
        json.loads(second['content'])['verdict'] == 'occurred'


# ---- the session: the act's observation is acted on directly, and is withheld where acting must wait -----------

from test_screen_session import (ALL, AXError, ScreenRefused, env, grant, ref, refused)  # noqa: E402,F401


def test_the_next_act_uses_the_observation_the_act_returned(env):
    sid = grant(env)
    view = env.sessions.observe(sid, env.run)
    typed = env.sessions.act(sid, env.run, view['observation_id'], ref(env, sid, view, 'w/to'), 'type', 'alice@example.com')
    seen = typed['observation']
    assert seen['observation_id'] != view['observation_id'] and seen['state'] == 'active' and seen['elements']
    assert seen['untrusted'] and typed['message'].startswith('The observation field is the window after this act')
    toggled = env.sessions.act(sid, env.run, seen['observation_id'], ref(env, sid, seen, 'w/bold'), 'toggle')
    assert toggled['verdict'] == 'occurred' and toggled['observation']['observation_id'] != seen['observation_id']
    # the observation an act returned goes stale like any other
    env.clock.mono += env.sessions.max_age + 1
    late = toggled['observation']
    refused('stale_ui', env.sessions.act, sid, env.run, late['observation_id'], ref(env, sid, late, 'w/to'), 'focus')


def test_no_observation_comes_back_after_an_uncertain_act(env):
    sid = grant(env)

    def lost(app, window, text):
        raise AXError('uncertain', 'no answer')
    env.window.hooks[('w/bold', 'toggle')] = lost
    view = env.sessions.observe(sid, env.run)
    unsure = env.sessions.act(sid, env.run, view['observation_id'], ref(env, sid, view, 'w/bold'), 'toggle')
    assert unsure['verdict'] == 'unknown' and 'observation' not in unsure and unsure['next'] == 'handoff'
    assert unsure['fresh_observation_required'] is True
