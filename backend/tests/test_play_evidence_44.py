"""Phase 44 (1.9.1, D119): a request to play is done only when macOS shows sound or video that started during the
task. Read from `pmset -g assertions` (process names, assertion names and ages only): browsers hold "Playing audio"
and "Video Wake Lock" while media plays (measured in Comet and Chrome, 2026-10-25), coreaudiod one per output device in
use; voice processing is not playback; something that was already playing before the task never counts."""
import time
from types import SimpleNamespace

from pebble import native_apps
from pebble.controller import PLAY_ISSUE, RunController

ASSERTIONS = '''Assertion status system-wide:
   PreventUserIdleSystemSleep     1
Listed by owning process:
   pid 99737(Comet): [0x000bc03400018aa8] 00:18:11 NoIdleSleepAssertion named: "Playing audio"
   pid 99737(Comet): [0x000bc41e00059481] 00:00:09 NoDisplaySleepAssertion named: "Video Wake Lock"
   pid 679(coreaudiod): [0x000bc41e0001a06e] 00:00:09 PreventUserIdleSystemSleep named: "com.apple.audio.BuiltInSpeakerDevice.context.preventuseridlesleep"
   pid 679(coreaudiod): [0x000bc3260001a03c] 00:00:02 PreventUserIdleSystemSleep named: "com.apple.audio.AVVCAggregateDevice-964-16807.context.preventuseridlesleep"
   pid 90056(caffeinate): [0x000bc39c00019478] 00:00:01 PreventUserIdleSystemSleep named: "caffeinate command-line tool"
   pid 666(sharingd): [0x000bc2fc00019458] 00:06:19 PreventUserIdleSystemSleep named: "Handoff"
'''


def test_only_playback_assertions_are_read_with_their_ages():
    rows = native_apps.playing(capture=lambda argv: ASSERTIONS)
    assert [(r['app'], r['age']) for r in rows] == [('Comet', 1091), ('Comet', 9), ('coreaudiod', 9)]
    assert native_apps.playing(capture=lambda argv: None) is None


def test_something_counts_only_when_it_started_within_the_window():
    capture = lambda argv: ASSERTIONS
    assert native_apps.playing_since(30, capture=capture) is True       # the video started 9 s ago
    quiet = ASSERTIONS.replace('00:00:09', '00:40:00')
    assert native_apps.playing_since(30, capture=lambda argv: quiet) is False, 'playing before the task began'
    assert native_apps.playing_since(30, capture=lambda argv: 'nothing\n') is False
    assert native_apps.playing_since(30, capture=lambda argv: None) is None


def host(goal, tools, created):
    runs = SimpleNamespace(tools=lambda rid: tools, get=lambda rid: {'id': rid, 'chat_id': 'c', 'goal': goal,
                                                                      'created': created},
                           note=lambda *a, **k: None)
    store = SimpleNamespace(actions=lambda cid: [], query=lambda *a, **k: [])
    agents = SimpleNamespace(completion=lambda cid, rid, answer=None: {'state': 'completed', 'issues': []})
    return SimpleNamespace(runs=runs, store=store, agents=agents,
                           hook_event=lambda *a, **k: {'decision': 'allow', 'require_tests': False})


def issues(monkeypatch, goal, tools, playing, created=None):
    monkeypatch.setattr(native_apps, 'playing_since', lambda seconds: playing)
    app = host(goal, tools, time.time() - 20 if created is None else created)
    controller = RunController.__new__(RunController)  # the completion check, without a store behind it
    controller.app, controller.interactions = app, SimpleNamespace(plan=lambda rid: None)
    return controller.completion('r' * 32, None, 'It is playing now.')['issues']


def test_a_play_request_needs_something_playing_since_the_task_began(monkeypatch):
    pressed = [{'name': 'BrowserRequest', 'state': 'completed', 'result': {}},
               {'name': 'ScreenAct', 'state': 'completed', 'result': {}}]
    assert PLAY_ISSUE in issues(monkeypatch, 'On YouTube, play the navior song', pressed, False)
    assert PLAY_ISSUE not in issues(monkeypatch, 'On YouTube, play the navior song', pressed, True)
    assert PLAY_ISSUE not in issues(monkeypatch, 'On YouTube, play the navior song', pressed, None), \
        'an unreadable list says nothing either way'


def test_other_requests_and_runs_without_a_media_tool_are_not_checked(monkeypatch):
    pressed = [{'name': 'ScreenAct', 'state': 'completed', 'result': {}}]
    assert PLAY_ISSUE not in issues(monkeypatch, 'Fill in the form on the shop page', pressed, False)
    assert PLAY_ISSUE not in issues(monkeypatch, 'Play a game of chess with me', [], False)
    assert PLAY_ISSUE not in issues(monkeypatch, 'Play it', [{'name': 'ScreenAct', 'state': 'failed', 'result': {}}],
                                    False)
