"""Phase 45 (1.9.14, F212 from audit round R8): asked to draw on a picture in Preview, Pebble asked for the window
without pressing, could not press Markup, and told the person to "grant access that allows pressing" with no way to do
so; asking again for the same window answered "already allowed" (D136). Drawing words ask for pressing, and a request
for more than the held window allows is a new review that widens it."""
import uuid

from pebble import screen_session as ss
from test_screen_session import ALL, env, grant, target  # noqa: F401


def test_drawing_and_moving_words_ask_for_pressing():
    for text in ('Open UXTEST-canvas.png in Preview and draw a red rectangle on it with Markup.',
                 "Open Chess and move the white king's pawn two squares forward.",
                 'Open Calculator and use its buttons to compute 123 times 456.', 'Crop the photo and rotate it'):
        assert ss.PRESS_INTENT.search(text), text
    for text in ('Read the reviews of this kettle', 'Search YouTube for lofi playlists and tell me the top three'):
        assert not ss.PRESS_INTENT.search(text), text


def test_a_request_for_more_than_the_held_window_allows_is_a_new_review(env):
    looking = [a for a in ALL if a in ss.REQUEST_GROUPS['look'] + ss.REQUEST_GROUPS['fill']]
    sid = grant(env, actions=looking)
    nxt = uuid.uuid4().hex
    assert env.sessions.carry_over(env.chat, nxt, lambda run: run == env.run) == [sid]
    payload = {'bundle_id': target()['bundle_id'], 'team_id': target()['team_id'], 'window': target()['window']}
    assert env.sessions.held_window(nxt, {**payload, 'actions': looking})['session_id'] == sid, 'covered: held'
    pressing = looking + list(ss.REQUEST_GROUPS['press'])
    assert env.sessions.held_window(nxt, {**payload, 'actions': pressing}) is None, 'more than held: a new review'


def test_a_negated_pressing_word_does_not_ask_for_pressing():
    # audit round R9 (F227): "Don't move anything" asked for a pressing grant, so a look-only task got a card
    assert not ss.press_intent("UXTEST: Open Chess and tell me whose turn it is. Don't move anything.")
    assert not ss.press_intent('Read the page without clicking anything') and not ss.press_intent('Do not press Send')
    assert ss.press_intent("Don't save it yet, but click the Options button")
    assert ss.press_intent("Open Chess and move the white king's pawn two squares forward.")
