"""Phase 45 (1.9.13, F200, F201 from audit round R7): a ScreenRequest for an Open or Save panel is refused with the
way that works, and a run whose reply says it is not finished is remembered as not completed."""
from types import SimpleNamespace

import pytest

from pebble import screen_session as ss
from test_app_control_42 import TITLE, Window, compose, env  # noqa: F401
from test_app_control_42 import ask


def test_an_open_panel_is_refused_with_openapps_path(env):
    del env.running.windows['win-1']
    env.running.add(Window('win-open', 'Open', compose()))
    with pytest.raises(ss.ScreenRefused) as exc:
        ask(env)
    assert exc.value.code == 'file_panel' and 'OpenApp' in str(exc.value) and 'full path' in str(exc.value)


def test_an_unfinished_reply_is_remembered_as_not_completed():
    from pebble import runner
    said = "I opened Calculator and pressed the 1 button, but I didn't complete the button sequence."
    assert runner.UNFINISHED_APP.search(said)
