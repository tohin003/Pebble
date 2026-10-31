"""Phase 46 (1.9.27): two message items of one Responses API output are two paragraphs, never one fused line.

A reply that holds a `FINAL:` line in one message item and more text in a second was joined without a separator, so
the person (and a line-based grader) read "FINAL: doneAll tests pass" as a malformed last line. The non-streaming
rebuild (_message_text) and the streamed text (a newline sent at the second message item) agree.
"""

from pebble.responses import _message_text


def message(*texts):
    return {'type': 'message', 'content': [{'type': 'output_text', 'text': text} for text in texts]}


def test_message_items_are_separated_and_parts_of_one_item_are_not():
    assert _message_text([message('Fixed.\n\nFINAL: done'), message('All tests pass.')]) == 'Fixed.\n\nFINAL: done\nAll tests pass.'
    assert _message_text([message('A.\n'), message('B.')]) == 'A.\nB.'  # no second newline after one
    assert _message_text([message('A', 'B')]) == 'AB'  # parts of one item, as before
    assert _message_text([message(''), message('B.')]) == 'B.'
    assert _message_text([{'type': 'function_call', 'name': 'Read', 'arguments': '{}', 'call_id': 'c1'},
                          message('Only.')]) == 'Only.'
    assert _message_text([message('A'), {'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'No.'}]}]) == 'A\nNo.'
