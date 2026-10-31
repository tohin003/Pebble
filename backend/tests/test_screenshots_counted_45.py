"""Phase 45 (1.9.15, F213 from audit round R8): Context use showed no screenshot line once a task ended, because the
pictures sent to the model are never kept in the conversation. Each send is noted in the task's journal (the count
only) and Context use reports how many were sent."""
import json
from types import SimpleNamespace

from pebble import context_inspector as ci
from pebble import runner


def test_each_sent_picture_is_counted_never_kept():
    notes = []
    app = SimpleNamespace(runs=SimpleNamespace(note=lambda run, kind, data: notes.append((run, kind, data))))
    runner.note_screenshot(app, 'r1', [{'type': 'text', 'text': 'untrusted'}, {'type': 'image_url', 'image_url': {'url': 'data:x'}}])
    runner.note_screenshot(app, 'r1', [{'type': 'text', 'text': 'no picture'}])
    assert notes == [('r1', 'screenshot_sent', {'images': 1})], 'the count only, and only when a picture went'
    rows = {'r1': [{'payload': json.dumps({'images': images})} for images in (1, 2, 'x')] + [{'payload': 'not json'}]}
    store = SimpleNamespace(query=lambda sql, args: rows.get(args[0], []) if "kind='screenshot_sent'" in sql else [])
    assert ci.screenshots_sent(store, 'r1') == 3 and ci.screenshots_sent(store, 'r2') == 0
