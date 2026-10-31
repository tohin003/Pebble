"""Phase 46 (1.9.27, the lead's review of the integrated memory capture): a task that saves, stores or notes something
"for later" is a task for now, never a memory. Before this fix the host saved "the report to reports/summary.md for
later." as a memory, answered "Saved ... for later sessions" and ended the run with the task never done; a request to
keep something for later sessions in a statement's shape is still saved by the host.
"""

import pytest

from pebble.app import App
from pebble.memory import future_statements


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield instance
    instance.close()


TASKS = [
    'Save the report to reports/summary.md for later.',
    'Store the cleaned CSV in out/clean.csv so I can use it in the future.',
    'Please note the totals in totals.txt for next time.',
    'Can you save these results as notes for later? Then run the tests.',
    'Write the summary to notes.md and save it for later reference.',
    'Save a backup of config.json for later, then change the port to 8080.',
    'Fix the bug in parser.py. Save a note in NOTES.md for future sessions about what you changed.',
    'Going forward, save all reports in reports/.',
    'Remember to save the file for later.',
    'Save this file for future sessions.',
]


@pytest.mark.parametrize('text', TASKS)
def test_a_task_that_saves_something_for_later_is_not_captured_as_a_memory(app, text):
    assert future_statements(text)[0] == []
    chat = app.new_chat()['id']
    before = app.store.query('SELECT id FROM memories')
    assert app.memory.capture_user(text, chat) is None  # the whole message goes to the model
    assert app.store.query('SELECT id FROM memories') == before


@pytest.mark.parametrize('text, statement', [
    ('Keep in mind for later that the build is slow.', 'the build is slow.'),
    ("Don't forget for next time: the API key rotates monthly.", 'the API key rotates monthly.'),
    ('Store this for future sessions: the VPN must be on.', 'the VPN must be on.'),
    ('Remember for next time that I like short replies.', 'for next time that I like short replies.'),  # as V1-033
])
def test_a_wish_to_keep_something_for_later_in_a_statements_shape_is_still_saved(app, text, statement):
    saved = app.memory.capture_user(text, app.new_chat()['id'])
    assert saved and saved['saved'] and saved['statement'] == statement and not saved['continue_run']
