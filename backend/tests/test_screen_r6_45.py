"""Phase 45 (1.9.11, audit round R6 on 1.9.10, E-C3): the Mac's Chess as it really is (each square named twice), the
capture that waits on macOS's own alert, and app steps in words."""
from pebble import screen_session as ss
from pebble.app import App
from pebble.run_activity import SCREENSHOT_MISSING, TOOLS_SQL


def test_a_chess_square_named_twice_is_still_a_board_square():
    # read from Chess on the person's Mac at 11:41 (ux.attrs_of): AXTitle and AXDescription both set
    for label in ('white pawn, e4', 'e2', 'black knight, g8'):
        element = {'role': 'AXButton', 'title': label, 'description': label, 'help': None, 'placeholder': None}
        assert ss._words(element) == label
        assert ss.classify('press', element, kind='app') == ('input', ['keypad_key']), label
    calculator = {'role': 'AXButton', 'title': None, 'description': '7', 'help': None}
    assert ss.classify('press', calculator, kind='app')[0] == 'input'
    different = {'role': 'AXButton', 'title': 'Delete', 'description': 'Delete the game', 'help': None}
    assert ss._words(different) == 'Delete Delete the game'
    assert ss.classify('press', different, kind='app')[0] == 'commit'


def test_a_stalled_capture_says_macos_may_be_asking():
    for code in ('capture_timeout', 'capture_failed'):
        why = ss.NO_SCREENSHOT_WHY[code]
        assert 'bypass the system private window picker' in why and 'choose Allow' in why and 'never sound' in why
        assert SCREENSHOT_MISSING[code].startswith('No screenshot: macOS may be asking')
    assert "json_type(result,'$.screenshot') END AS screenshot" in TOOLS_SQL


def test_app_steps_are_said_in_words():
    for name in ('ScreenRequest', 'ScreenObserve', 'ScreenAct', 'BrowserRequest', 'OpenURL', 'AppAction', 'Shell',
                 'ReadFile', 'WriteFile', 'EditFile', 'Screenshot', 'FindTool'):
        label = App.tool_label(name, {'file_path': '', 'name': '', 'purpose': ''})
        assert label != name and name not in label, (name, label)
