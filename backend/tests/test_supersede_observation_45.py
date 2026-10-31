"""Phase 45 (1.9.13, F191 from audit round R6): an app task resent every earlier observation of its window each turn
(287,654 input tokens for 8 Calculator keys). A new observation turns the one before it into a short stub; only that one
changes, so the conversation before it, and the prompt cache over it, stay as they were."""
import json

from pebble import runner


def observation(oid, rows):
    return {'role': 'tool', 'tool_call_id': 'call-' + oid,
            'content': json.dumps({'observation_id': oid, 'elements': rows})}


def test_each_new_observation_turns_the_previous_one_into_a_stub():
    observed, rows = {}, [{'role': 'AXButton', 'label': str(n)} for n in range(40)]
    first, second, third = observation('o1', rows), observation('o2', rows), observation('o3', rows)
    other = observation('x1', rows)
    runner.supersede_observation(observed, 'session-a', first, {'observation_id': 'o1'})
    runner.supersede_observation(observed, 'session-b', other, {'observation_id': 'x1'})
    runner.supersede_observation(observed, 'session-a', second, {'observation_id': 'o2'})
    assert json.loads(first['content']) == {'superseded': True, 'observation_id': 'o1', 'note': runner.SUPERSEDED}
    assert 'elements' in json.loads(second['content']) and 'elements' in json.loads(other['content']), \
        'the newest, and another window, keep their rows'
    before = first['content']
    runner.supersede_observation(observed, 'session-a', third, {'observation_id': 'o3'})
    assert first['content'] == before, 'an old stub never changes again (the cached prefix holds)'
    assert json.loads(second['content'])['superseded'] is True and 'elements' in json.loads(third['content'])
    assert len(first['content']) < 250


def test_a_result_without_an_observation_changes_nothing():
    observed, first = {}, observation('o1', [])
    runner.supersede_observation(observed, 'session-a', first, {'observation_id': 'o1'})
    runner.supersede_observation(observed, 'session-a', observation('o2', []), {'status': 'needs_bring_forward'})
    assert 'elements' in json.loads(first['content'])


def test_a_screenshot_is_its_own_part_of_the_context():
    # audit round R6: Context use showed no sign that a picture of the window was in the model's context
    from pebble import context_inspector as ci
    message = {'role': 'user', 'name': 'pebble_screenshot', 'content': [{'type': 'text', 'text': 'untrusted'}]}
    assert ci.classify(message, before_request=False) == ('project_host', 'screenshots')
    assert 'screenshots' in ci.PARTS['project_host']
