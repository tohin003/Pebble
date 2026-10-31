"""Phase 45 (1.9.13, F199 from audit round R7): Calculator with old input on its display; Pebble pressed C and 1, then
stopped and answered from its own arithmetic. A keypad window says how to clear it, and an "I didn't complete" reply is
held once while the window is still granted."""
from types import SimpleNamespace

from pebble import runner, screen_session as ss


def keys(*labels):
    return [{'role': 'AXButton', 'label': label} for label in labels]


def test_a_full_keypad_gets_its_note():
    calculator = keys(*[str(d) for d in range(10)], 'All Clear', 'multiply', 'equals')
    assert ss.keypad_window(calculator) and 'AC or esc clears everything' in ss.KEYPAD_NOTE
    assert not ss.keypad_window(keys('1', '2', '3', 'Send')), 'a few digits are not a keypad'
    assert not ss.keypad_window(keys(*[str(d) for d in range(10)])), 'no equals key'


def test_an_unfinished_reply_is_held_only_while_a_window_is_granted():
    said = "I opened Calculator and pressed the 1 button, but I didn't complete the button sequence. The result is 56,088."
    assert runner.UNFINISHED_APP.search(said)
    assert runner.UNFINISHED_APP.search("I wasn't able to complete the button-entry")
    assert not runner.UNFINISHED_APP.search('Done: 123 × 456 = 56,088, entered with the buttons.')
    held = SimpleNamespace(screens=SimpleNamespace(session_for_run=lambda run: {'id': 's1'}))
    none = SimpleNamespace(screens=SimpleNamespace(session_for_run=lambda run: None))
    assert runner.screen_held(held, 'r1') and not runner.screen_held(none, 'r1')
    assert not runner.screen_held(SimpleNamespace(), 'r1')
    assert 'Do not give your own computed answer' in runner.UNFINISHED_CHECK
