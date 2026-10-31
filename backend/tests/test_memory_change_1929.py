"""Phase 46 (1.9.29, the lead): the person asking to change a saved preference ("Please change my date preference to
YYYY-MM-DD.") is their update of it. D212's per-case counts of the golden held-out memory family point at a preference
changed in a later session; private d-me-244 reproduced it on Opus, 0 of 3, all false completions: the host read the
request as a task and kept nothing, the model's MemoryPropose of those words waited for a confirmation no later session
gives, and the next session applied the old preference (the host's date repair even wrote it back) and said done.

Now Memory.update takes "change, switch, update or set MY <subject> to <value>" in the person's own words when it speaks
of a record of theirs, saves it as their newest wish and lets the existing same-subject rule replace the older record,
which stays stored and listed in Settings > Memory (D176). Never a question, a file's change, a wish for now, someone
else's words, quoted material, a credential, or a request with no saved record behind it.
"""

import pytest

from pebble import memory_check
from pebble.app import App
from test_memory_capture_1928 import FINAL, run_session, saved_items

FIRST = 'Please remember for future sessions: I always want dates written as DD.MM.YYYY.'
LATER = 'When is the next release? Give the date on the last line as `FINAL: <date>`.'


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield instance
    instance.close()


def saved_first(app):
    chat = app.new_chat()['id']
    assert app.memory.capture_user(FIRST, chat)['saved']
    return chat


@pytest.mark.parametrize('text', [
    'Please change my date preference to YYYY-MM-DD.', 'Change my date format to YYYY-MM-DD.',
    'Switch my date preference to YYYY-MM-DD.', 'Update my date preference to YYYY-MM-DD.',
    'Set my date format to YYYY-MM-DD.', 'Could you change my date format to YYYY-MM-DD, please?'.rstrip('?') + '.',
])
def test_a_change_request_replaces_the_saved_preference(app, text):
    chat = saved_first(app)
    result = app.memory.capture_user(text, app.new_chat()['id'])
    assert result and result['saved'] and result['relation'] == 'updated' and result['replaced'], text
    standing = [record['content'] for record in app.memory.standing(chat)]
    assert len(standing) == 1 and 'YYYY-MM-DD' in standing[0] and 'DD.MM.YYYY' not in standing[0]
    # the older record stays stored for Settings > Memory and the person's Forget (D176)
    assert any('DD.MM.YYYY' in row['content'] for row in app.store.query('SELECT content FROM memories'))


@pytest.mark.parametrize('text', [
    'Can you change my date preference to YYYY-MM-DD?',                    # a question
    'Change my report.md dates to YYYY-MM-DD.',                            # a file's change
    'Change the date format in notes/today.md to YYYY-MM-DD.',             # not mine, a file's
    'Change my date preference to YYYY-MM-DD for this answer.',            # a wish for now
    '> Please change my date preference to YYYY-MM-DD.',                    # quoted material
    'My manager said: change my date preference to YYYY-MM-DD.',          # someone else's words
    'Change my mind about it: dates to YYYY-MM-DD.',                       # not a saved preference's change
    'Change my password to hunter22.',                                     # never a credential
])
def test_anything_else_is_no_change_request(app, text):
    saved_first(app)
    result = app.memory.capture_user(text, app.new_chat()['id'])
    assert not (result and result.get('relation') == 'updated'), (text, result)


def test_a_change_request_with_nothing_saved_behind_it_saves_nothing(app):
    assert app.memory.capture_user('Please change my date preference to YYYY-MM-DD.', app.new_chat()['id']) is None


def test_the_reply_check_holds_the_changed_value(app):
    records = [{'content': 'Please change my date preference to YYYY-MM-DD.', 'saved': '2026-10-30 21:40:00'}]
    assert memory_check.preferred(records) == {'date': 'YYYY-MM-DD'}


def test_the_next_session_is_given_only_the_changed_preference(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    home = tmp_path / 'home'
    run_session(home, FIRST + FINAL, workspace=project)
    change = run_session(home, 'Please change my date preference to YYYY-MM-DD.' + FINAL, workspace=project)
    assert 'YYYY-MM-DD' in (change.answer or '')  # the person sees what was saved, with Forget
    later = run_session(home, LATER, workspace=project)
    assert [item['content'] for item in saved_items(later.requests[0][0])] == [
        'Change my date preference to YYYY-MM-DD.']  # the person's words without the request's courtesy


@pytest.mark.parametrize('newer', ['To my preferences: dates should now be written as YYYY-MM-DD.',
                                   'I have switched to ISO dates, YYYY-MM-DD, for everything now.'])
def test_a_newer_record_that_names_a_slot_never_lets_an_older_value_be_held_against_the_reply(newer):
    # before: the older DD.MM.YYYY decided the date, so a reply in the person's new format was asked to be rewritten
    old = {'content': 'for future sessions: I always want dates written as DD.MM.YYYY.', 'saved': '2026-10-30 21:00:00'}
    records = [{'content': newer, 'saved': '2026-10-30 21:05:00'}, old]
    answer = 'Ten days after 20 November 2026 is 2026-11-30.\n\nFINAL: 2026-11-30'
    request = 'What is the date ten days after 20 November 2026?'
    assert 'date' not in memory_check.preferred(records)
    assert memory_check.check(answer, records, request) == [] and memory_check.repair(answer, records, request)[1] == []
    # a newer record that decides the slot still decides it, and an older one alone still does
    assert memory_check.preferred([{'content': 'From now on, write dates as MM/DD/YYYY instead.',
                                    'saved': '2026-10-30 21:05:00'}, old]) == {'date': 'MM/DD/YYYY'}
    assert memory_check.preferred([old]) == {'date': 'DD.MM.YYYY'}
