"""Phase 45 (1.9.16, F236 from audit round R10): Calculator showed 100 × 123 × 4,566 after a wrong burst; Pebble's reply
said "I'll clear the calculator and enter 123 × 456 again." and the run ended there, with "Done." A reply whose last
sentence is its own next step is held once while a window is still granted, and a run that still ends on one is not
shown as done (runner.UNFINISHED_NOTE) nor kept as finished."""
import pytest

from pebble import runner
from test_runner_honesty_24 import LARGE, env  # noqa: F401

SAID = ("The calculator had a previous value, so the first attempt used it too. I'll clear the calculator and enter "
        "123 × 456 again.")


def test_a_closing_promise_is_a_next_step_not_taken():
    assert runner.will_do(SAID) and runner.not_done(SAID)
    for promise in ("Let me press Equals now.", "Next, I'll draw the rectangle.", "Okay, I will move the pawn to d4.",
                    "Now I'm going to retry with the window in front."):
        assert runner.will_do('The board is ready. ' + promise), promise
    for finished in ('Done: 123 × 456 = 56,088 on the display.', 'Moved the pawn to e4. Let me know if you want more.',
                     "I'll leave the calculator showing 56,088.", 'If you want, I can play the next move too.',
                     'I will keep the window open for you.', "I'll clear it now. The display shows 56,088."):
        assert not runner.will_do(finished), finished
    assert 'take that step now' in runner.WILL_DO_CHECK and 'nothing runs after a reply' in runner.WILL_DO_CHECK


@pytest.fixture
def held(monkeypatch):
    monkeypatch.setattr(runner, 'screen_held', lambda app, run_id: True)


def test_the_promise_is_held_once_and_the_next_reply_stands(env, held):
    e = env(LARGE)
    replies = iter([SAID, 'Done: I cleared it and entered 123 × 456; the display shows 56,088.'])
    e.script = lambda n, messages, tools: next(replies)
    result = e.work('Use Calculator to compute 123 times 456.')
    assert len(e.requests) == 2 and result.run['state'] == 'completed' and result.run['error'] is None
    assert result.answer == 'Done: I cleared it and entered 123 × 456; the display shows 56,088.'
    assert e.requests[1][0][-1]['content'].startswith(runner.WILL_DO_CHECK)


def test_a_run_that_still_ends_on_a_promise_is_not_shown_as_done(env, held):
    e = env(LARGE)
    e.script = lambda n, messages, tools: SAID
    result = e.work('Use Calculator to compute 123 times 456.')
    assert len(e.requests) == 2, 'held once, never twice'
    assert result.run['state'] == 'completed' and result.run['error'] == runner.UNFINISHED_NOTE


def test_without_a_granted_window_a_promise_ends_the_run_as_before(env):
    e = env(LARGE)
    e.script = lambda n, messages, tools: SAID
    result = e.work('Use Calculator to compute 123 times 456.')
    assert len(e.requests) == 1 and result.run['error'] is None


def test_the_run_panel_knows_the_note():
    from pathlib import Path
    panel = (Path(__file__).resolve().parents[2] / 'src' / 'components' / 'RunPanel.vue').read_text(encoding='utf-8')
    assert f'const UNFINISHED = "{runner.UNFINISHED_NOTE}";' in panel and "'Ended without finishing.'" in panel


def test_a_run_that_ends_with_its_window_paused_is_kept_as_not_completed(env, monkeypatch):
    """F242: "Please continue and finish drawing the red rectangle." was answered only with "press Done" while Preview's
    window stayed paused, and Past experiences kept it as finished."""
    e = env(LARGE)
    kept = []
    monkeypatch.setattr(e.app.memory, 'episode', lambda *args, **kwargs: kept.append(args[4]))
    monkeypatch.setattr(runner, 'paused_window', lambda app, run_id: {'paused': True, 'app': 'Preview'})
    e.script = lambda n, messages, tools: "Preview's window is paused. Press Done in the Needs a check block."
    result = e.work('Please continue and finish drawing the red rectangle.')
    assert result.run['state'] == 'completed' and kept == ['not_completed']
    monkeypatch.setattr(runner, 'paused_window', lambda app, run_id: None)
    e.script = lambda n, messages, tools: 'Done: the rectangle is red and drawn.'
    e.work('Please continue and finish drawing the red rectangle.')
    assert kept == ['not_completed', 'completed']
