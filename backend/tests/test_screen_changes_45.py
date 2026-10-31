"""Phase 45 (1.9.11, F183): an act's answer lists the rows it changed, before and after, so a press that appended to
old input reads as that."""
from pebble import screen_session as ss


def snap(display, secret='hunter2'):
    return {'elements': [
        {'path': [0, 0], 'role': 'AXStaticText', 'value': display, 'label': display, 'secure': False},
        {'path': [0, 1], 'role': 'AXButton', 'title': '1', 'description': '1', 'label': '1', 'secure': False},
        {'path': [0, 2], 'role': 'AXTextField', 'value': secret, 'label': 'Password', 'secure': True},
    ]}


def test_a_press_that_appended_to_old_input_is_named_as_that():
    changes = ss.row_changes(snap('100×10'), snap('100×101', secret='other'))
    assert changes == [{'role': 'AXStaticText', 'before': '100×10', 'after': '100×101'}]


def test_nothing_changed_and_secure_fields_say_nothing():
    assert ss.row_changes(snap('0'), snap('0', secret='changed')) == []


def test_screenact_says_keypad_presses_are_input_and_to_clear_old_input():
    act = next(t for t in ss.SCHEMAS if t['function']['name'] == 'ScreenAct')['function']['description']
    assert 'keypad keys, board squares. Commit: other presses' in act and 'Clear old input first' in act
    assert 'changes: what it changed' in act
