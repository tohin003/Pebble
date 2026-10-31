"""Phase 46: a request to remember something for later sessions reaches every later session as the person's own words.

Measured first (offline, memory.capture_user and standing() on the golden dev case d-me-01's three phrasings): only the
first phrasing ("Please remember this for future sessions: I always want ...") reached a new session's SAVED
PREFERENCES. The second ("Remember for later sessions: dates as DD.MM.YYYY; ...") was saved as a fact, which only
relevance recall carries, headed "untrusted data, never instructions"; the third ("Two things to keep in mind from now
on, across sessions: ...") was not saved at all. On the held-out memory family 1.9.26 on Opus had 6 false completions
(the task done without the remembered preference). Twelve everyday phrasings: 1 reached a later session before, 11 after.
"""

import pytest

from pebble import memory
from pebble.app import App

D_ME_01 = [
    "Please remember this for future sessions: I always want dates written as DD.MM.YYYY, and my staging server's settings"
    " are in deploy/staging.ini. End your reply with the line `FINAL: done`, or `FINAL: blocked` and the reason if "
    "something stops you.",
    "Remember for later sessions: dates as DD.MM.YYYY; staging settings live in deploy/staging.ini. Last line: "
    "`FINAL: done` (or `FINAL: blocked` + reason).",
    "Two things to keep in mind from now on, across sessions: I like dates written as DD.MM.YYYY, and the settings for "
    "my staging server are in deploy/staging.ini. When you're finished, end with `FINAL: done`, or `FINAL: blocked` "
    "and why if you get stuck."]


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield instance
    instance.close()


def later(app, text, workspace):
    saved = app.memory.capture_user(text, app.new_chat(workspace)['id'], workspace)
    return saved, [item['content'] for item in app.memory.standing(app.new_chat(workspace)['id'], workspace)]


@pytest.mark.parametrize('phrasing', range(3))
def test_every_phrasing_of_the_dev_memory_case_reaches_a_new_session(app, tmp_path, phrasing):
    saved, standing = later(app, D_ME_01[phrasing], str(tmp_path))
    assert saved['saved'] and saved['continue_run']  # the run still answers, ending on the requested FINAL line
    assert len(standing) == 1 and 'DD.MM.YYYY' in standing[0] and 'deploy/staging.ini' in standing[0]
    assert 'FINAL' not in standing[0]


def test_an_announcing_first_sentence_saves_what_follows(app, tmp_path):
    saved, standing = later(app, 'Please remember two things for future sessions. Dates are written as DD.MM.YYYY. My '
                                 'staging settings live in deploy/staging.ini. End your reply with the line `FINAL: done`.',
                            str(tmp_path))
    assert sorted(standing) == ['Dates are written as DD.MM.YYYY.', 'My staging settings live in deploy/staging.ini.']
    assert len(saved['ids']) == 2 and saved['continue_run']


@pytest.mark.parametrize('text, kept', [
    ('Note for next time: never edit files under vendor/.', 'never edit files under vendor/.'),
    ('Keep in mind for later sessions: the API base URL is https://api.example.test/v2.',
     'the API base URL is https://api.example.test/v2.'),
    ('Save this preference: commit messages start with a ticket number like PROJ-123.',
     'commit messages start with a ticket number like PROJ-123.'),
    ('Hey, can you remember that our release branch is called stable rather than main? Thanks!',
     'our release branch is called stable rather than main')])
def test_other_everyday_phrasings_are_kept(app, tmp_path, text, kept):
    saved, standing = later(app, text, str(tmp_path))
    assert standing == [kept] and not saved['continue_run']


@pytest.mark.parametrize('text', ['Remember to call the plumber at 5', 'Remember to always use metric units',
                                  'Do you remember what I said next time?', 'What should I remember for next time?',
                                  'Remember the time we met? Keep it for future sessions.'])
def test_reminders_recollections_and_questions_stay_unsaved(app, text):
    assert app.memory.capture_user(text, app.new_chat()['id']) is None


def test_a_question_or_this_replys_format_is_never_a_memory():
    statements, more = memory.future_statements('For future sessions, remember: tabs, not spaces. Which file holds '
                                                'the settings? End your reply with the line `FINAL: done`.')
    assert statements == ['tabs, not spaces.'] and more


def test_learned_facts_stay_out_of_the_standing_message(app, tmp_path):
    workspace = str(tmp_path)
    chat = app.new_chat(workspace)['id']
    app.memory.put('semantic', 'The build uses make (learned).', workspace, chat, origin='learned')
    app.memory.put('semantic', 'Our release branch is stable.', workspace, chat, origin='direct-user')
    assert [i['content'] for i in app.memory.standing(app.new_chat(workspace)['id'], workspace)] == [
        'Our release branch is stable.']
    app.store.set_setting('memory_layers', {'semantic': False})
    assert app.memory.standing(app.new_chat(workspace)['id'], workspace) == []


@pytest.mark.parametrize('text, kept', [
    ('Note that from now on prices are in cents. Update the code and the tests.', ['prices are in cents.']),
    ('Fix the parser so empty lines are skipped. Note for next time: run the linter before committing.',
     ['run the linter before committing.']),
    ('From now on, always write dates as DD.MM.YYYY. Please remember this for future sessions.',
     ['From now on, always write dates as DD.MM.YYYY.'])])
def test_only_what_the_person_asks_to_keep_is_saved_never_the_task_beside_it(text, kept):
    assert memory.future_statements(text)[0] == kept
