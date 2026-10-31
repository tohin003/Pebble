"""Phase 45 (1.9.15, F224 from audit round R9): after an uncertain step the window stayed paused until the person
pressed Done in Details › Tools, and a new task told the person to "grant access to the Preview window again", which
nothing lets them do. The conversation's Needs a check block offers Done, and a new task hears that the window is paused
and asks the person to press Done instead."""
from types import SimpleNamespace

from pebble import runner


def app_with(sessions, live=None):
    screens = SimpleNamespace(session_for_run=lambda run: live, sessions=lambda chat: sessions, requested=set())
    return SimpleNamespace(screens=screens, runs=SimpleNamespace(get=lambda run: {'chat_id': 'c1'}),
                           task_policy={'tools': {'screen': True}})


def test_a_paused_window_is_announced_and_never_offered():
    app = app_with([{'state': 'interrupted', 'target': {'bundle_id': 'com.apple.Preview'}}])
    names, session = runner.screen_step(app, 'r2', 32768, 'finish drawing the rectangle')
    assert names == frozenset() and session == {'paused': True, 'app': 'Preview'}
    line = runner.screen_session_text(session)
    assert 'the window of Preview the person allowed is paused' in line and 'press Done' in line
    assert 'Do not ask them to allow the window again' in line


def test_no_paused_window_no_line():
    names, session = runner.screen_step(app_with([{'state': 'revoked', 'target': {}}]), 'r2', 32768, 'hello')
    assert names == frozenset() and session is None
