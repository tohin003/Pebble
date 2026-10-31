"""Phase 45 (1.9.13, F197 from audit round R7): an empty model reply gets one host nudge; a second ends the task with a
plain message (it ended on "Model returned no text or tool calls" with a raw step line)."""
from pebble import responses, runner, screen_session as ss


def test_the_empty_reply_is_its_own_error_and_the_nudge_asks_for_an_answer():
    assert issubclass(responses.EmptyResponse, ValueError), 'callers that catch ValueError still do'
    assert 'Answer the person now' in runner.EMPTY_REPLY_NUDGE and 'whether the task is done' in runner.EMPTY_REPLY_NUDGE


def test_a_step_reads_in_words():
    review = {'element': {'label': 'd5'}}
    assert ss.describe({'action': 'click_at', 'verdict': 'not_observed', 'basis': 'no_visible_change'}, review) == \
        'Pebble clicked “d5”: nothing changed in the window.'
    assert ss.describe({'action': 'press', 'verdict': 'occurred'}, {'element': {'label': 'e4'}}) == \
        'Pebble pressed “e4”: the window changed as expected.'


def test_the_session_line_prefers_element_refs():
    text = runner.screen_session_text({'id': 's1', 'kind': 'app', 'actions': ['press'], 'expires_at': None})
    assert 'act on its element ref (exact)' in text and 'only for what the rows lack' in text
