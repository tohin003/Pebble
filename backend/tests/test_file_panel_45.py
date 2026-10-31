"""Phase 45 (1.9.11, F187): a window that looks like an Open or Save panel is flagged on the card, and ScreenRequest
tells the model never to ask for one."""
from pebble import screen_session as ss


def test_open_and_save_panels_are_flagged():
    for title in ('Open', 'Save', 'Save As…', 'Export…', 'Choose a File:', 'open'):
        assert ss.file_panel({'title': title}), title
    for title in ('Untitled.png', 'Game 1 | Tohin - Computer (White to Move)', 'Calculator', 'Open Source Licenses',
                  'Saved Searches', ''):
        assert not ss.file_panel({'title': title}), title
    assert ss.file_panel({'title': 'x', 'ax_identifier': 'open-panel'})


def test_screenrequest_says_never_to_ask_for_a_file_panel():
    text = ss.REQUEST_SCHEMA['function']['description']
    # 1.9.13 (F200): the way that works comes first; the panel is refused at the tool level too
    assert 'use OpenApp with its path: never ask for an Open or Save panel' in text and 'most presses still wait' in text
