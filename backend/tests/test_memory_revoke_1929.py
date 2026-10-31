"""Phase 46 (1.9.29, the lead, 2026-10-30): the person's stop or forget of a saved preference (D176: saved as their newest
statement, listed first and followed from then on; the older record stays listed until they choose Forget).

D212's per-case counts of the golden held-out memory family: a two-clause preference with one clause revoked by a newer
statement, both listed, failed on Opus 3 of 3 while sol followed it. The older item in SAVED PREFERENCES now says that
the newer statement stops what it names there and that only what it leaves applies (runner.revocation_notes): one
wording for a whole or a partial stop, since splitting a record into clauses misread whole stops as partial ones.

Private d-me-245 (Opus, 2 of 3 false completions): "Please stop starting your replies with Ahoy." kept nothing, the model
said it had no tool to forget, and the next session began "Ahoy!" again. Stopping a habit a preference sets ("stop
starting/adding/signing ...", "don't add ... anymore") is now a revocation too, when it shares a distinctive word with a
record of theirs (memory.REVOKE_HABIT, Memory.revoked_records).
"""

import pytest

from pebble import memory
from pebble.app import App
from test_memory_capture_1928 import FINAL, run_session, saved_items

AHOY = 'Please remember for future sessions: I want every reply you give me to start with the word Ahoy.'
TWO_PART = ('Please remember for future sessions: start every reply to me with the word Hello, and end the body of every '
            'reply with a line that says Cheers.')


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield instance
    instance.close()


@pytest.mark.parametrize('text', [
    'Please stop starting your replies with Ahoy.', 'Stop beginning your answers with Ahoy.',
    "Don't start your replies with Ahoy anymore.", 'Never open a reply with Ahoy again.',
    'Quit starting replies with Ahoy, please.',
])
def test_stopping_a_habit_revokes_the_record_it_names(app, text):
    assert app.memory.capture_user(AHOY + FINAL, app.new_chat()['id'])['saved']
    result = app.memory.capture_user(text + FINAL, app.new_chat()['id'])
    assert result and result['saved'] and result['relation'] == 'revoked' and result['revoked'], text
    standing = [record['content'] for record in app.memory.standing(app.new_chat()['id'])]
    assert len(standing) == 2 and memory.revocation(standing[0]), standing  # newest first, the older one still listed
    # D176: nothing is erased without the person's click
    assert any('Ahoy' in row['content'] for row in app.store.query("SELECT content FROM memories WHERE origin='direct-user'"))


@pytest.mark.parametrize('text', [
    'Stop adding print statements, just fix the bug.',       # no record of theirs speaks of print statements
    'Stop adding the tag for now.',                           # a wish for now
    'Can you stop starting replies with Ahoy?',               # a question
    'Stop the server.',                                       # no habit
    'My manager said: stop starting replies with Ahoy.',      # someone else's words
])
def test_a_stop_without_its_record_or_for_now_keeps_nothing(app, text):
    assert app.memory.capture_user(AHOY + FINAL, app.new_chat()['id'])['saved']
    result = app.memory.capture_user(text, app.new_chat()['id'])
    assert not (result and result.get('relation') == 'revoked'), text
    assert len(app.memory.standing(app.new_chat()['id'])) == 1


def test_a_habit_stop_needs_a_distinctive_word_of_the_record(app):
    assert app.memory.capture_user('Please remember for future sessions: I want my code reviewed for typos.' + FINAL,
                                   app.new_chat()['id'])['saved']
    result = app.memory.capture_user('Stop adding print statements to every reply.', app.new_chat()['id'])
    assert not (result and result.get('relation') == 'revoked')


def test_the_narrow_forms_are_unchanged(app):
    assert app.memory.capture_user('Please remember for future sessions: I always want dates written as DD.MM.YYYY.',
                                   app.new_chat()['id'])['saved']
    result = app.memory.capture_user('Stop using DD.MM.YYYY.', app.new_chat()['id'])
    assert result['relation'] == 'revoked'


def test_saved_preferences_say_which_part_a_newer_stop_ends(tmp_path):
    run_session(tmp_path, TWO_PART + FINAL)
    run_session(tmp_path, 'Forget the Cheers line.' + FINAL)
    later = run_session(tmp_path, 'What is 6 multiplied by 7? Put the number on the last line as `FINAL: <number>`.',
                        script=['Hello! 6 x 7 = 42.\n\nFINAL: 42'])
    items = saved_items(later.requests[0][0])
    assert len(items) == 2 and memory.revocation(items[0]['content']) and 'note' not in items[0], items
    assert 'Cheers' in items[1]['content'] and 'stops what it names here' in items[1]['note'], items
    assert items[1]['note'].endswith('apply only what it leaves, if anything.')


def test_saved_preferences_say_a_whole_record_is_stopped(tmp_path):
    run_session(tmp_path, AHOY + FINAL)
    run_session(tmp_path, 'Please stop starting your replies with Ahoy.' + FINAL)
    later = run_session(tmp_path, 'What is 17 multiplied by 23? Put the number on the last line as `FINAL: <number>`.',
                        script=['17 x 23 = 391.\n\nFINAL: 391'])
    items = saved_items(later.requests[0][0])
    assert len(items) == 2 and 'note' not in items[0]
    assert 'Ahoy' in items[1]['content'] and items[1]['note'].endswith('apply only what it leaves, if anything.'), items


def test_saved_preferences_without_a_stop_are_listed_as_before(tmp_path):
    run_session(tmp_path, TWO_PART + FINAL)
    later = run_session(tmp_path, 'What is 6 multiplied by 7? Put the number on the last line as `FINAL: <number>`.',
                        script=['Hello! 6 x 7 = 42.\nCheers\n\nFINAL: 42'])
    items = saved_items(later.requests[0][0])
    assert len(items) == 1 and set(items[0]) == {'id', 'kind', 'content', 'saved'}, items
