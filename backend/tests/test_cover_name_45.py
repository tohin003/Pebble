"""Phase 45 (1.9.13, F193 from audit round R6): a click refused because something else covers the point names what is in
front (in R6 a macOS alert covered it and the model blamed an earlier step)."""
from pebble import native_apps
from pebble import screen_session as ss


def test_the_cover_is_named_by_its_app(monkeypatch):
    monkeypatch.setattr(native_apps, 'pid_path', lambda pid: '/System/Library/CoreServices/UserNotificationCenter.app/'
                                                             'Contents/MacOS/UserNotificationCenter')
    monkeypatch.setattr(native_apps, '_bundle_of', lambda path: '/System/Library/CoreServices/UserNotificationCenter.app')
    assert ss.cover_name(812) == 'In front of it now: UserNotificationCenter.'
    message = str(ss.ScreenRefused('covered', ss.cover_name(812)))
    assert message.startswith('Something other than the granted window is at that point now') \
        and message.endswith('In front of it now: UserNotificationCenter.')


def test_an_unknown_cover_adds_nothing(monkeypatch):
    monkeypatch.setattr(native_apps, 'pid_path', lambda pid: None)
    assert ss.cover_name(812) is None and ss.cover_name(None) is None and ss.cover_name(-1) is None
