"""Phase 46 (1.9.27): the memory family's false completions (golden held-out: Opus 6 of 15, sol 3 of 15), reproduced
offline on the golden DEV prompts' shapes (d-me-01's three phrasings; never the sealed split).

- The host saved a cross-session wish only when it began "Remember (that|this) ...": d-me-01's third phrasing ("Two
  things to keep in mind from now on, across sessions: I like dates written as DD.MM.YYYY, ...") saved nothing, and
  the model's MemoryPropose can only quote a preference-grammar sentence or wait for a native confirmation that no
  new session gives. The next session then answered as if it knew (a false completion).
- A profile record the learning layer promoted from the person's own sentence (host-verified quote) reached a new
  session only inside the untrusted recall pack ("not instructions"); it is their words and now comes with the SAVED
  PREFERENCES message like a "Remember that ..." does.
"""

import json

import pytest

from pebble.app import App
from test_routing_24 import LARGE, drive

PHRASING_3 = ("Two things to keep in mind from now on, across sessions: I like dates written as DD.MM.YYYY, and the "
              "settings for my staging server are in deploy/staging.ini. When you're finished, end with `FINAL: done`, "
              "or `FINAL: blocked` and why if you get stuck.")
LATER = 'When does the next maintenance window on my staging server start? Give the date on the last line as `FINAL: <date>`.'


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield instance
    instance.close()


@pytest.mark.parametrize('text, statement', [
    (PHRASING_3, 'I like dates written as DD.MM.YYYY, and the settings for my staging server are in deploy/staging.ini.'),
    ('For future sessions, note that my staging settings are in deploy/staging.ini. End with `FINAL: done`.',
     'my staging settings are in deploy/staging.ini.'),
    ('Going forward, please keep in mind: I write dates as DD.MM.YYYY.', 'I write dates as DD.MM.YYYY.'),
    ('Keep in mind for later sessions that I prefer tabs.', 'I prefer tabs.'),
    ('In future sessions, remember my name is Alex.', 'my name is Alex.'),
    ('Note for next time: the deploy script lives in ops/deploy.sh.', 'the deploy script lives in ops/deploy.sh.'),
    ('Please remember this for future sessions: I always want dates written as DD.MM.YYYY.',
     'for future sessions: I always want dates written as DD.MM.YYYY.'),  # as before (V1-033)
])
def test_a_cross_session_wish_in_other_words_than_remember_is_saved_by_the_host(app, text, statement):
    saved = app.memory.capture_user(text, app.new_chat()['id'])
    assert saved and saved['saved'] and saved['statement'] == statement
    assert saved['continue_run'] == ('FINAL' in text)  # the rest of the message still gets its answer
    assert statement in [item['content'] for item in app.memory.standing(app.new_chat()['id'])] or saved['kind'] != 'profile'


@pytest.mark.parametrize('text', [
    'Note: the build is slow today.', 'Keep in mind that the tests take a while.',
    'Please note the following and fix it: the parser fails on empty input.', 'From now on, use British spelling in replies.',
    'Remember when we fixed the parser?', 'Keep in mind how the cache works.',
])
def test_ordinary_notes_reminders_and_recollections_are_still_not_captured(app, text):
    assert app.memory.capture_user(text, app.new_chat()['id']) is None


def test_a_standing_instruction_for_later_sessions_is_saved_whole(app):
    # integrated with the checkout's capture: "remember to <verb>" with a later-session marker is an instruction for
    # every later session, saved in the person's words (a reminder for now, "Remember to call the plumber", is not)
    saved = app.memory.capture_user('From now on, remember to run the tests.', app.new_chat()['id'])
    assert saved and saved['statement'] == 'From now on, remember to run the tests.'
    assert app.memory.capture_user('Remember to call the plumber.', app.new_chat()['id']) is None


def test_the_third_phrasing_reaches_the_next_session_as_a_saved_preference(tmp_path):
    folder = tmp_path / 'same'
    first = drive(folder, PHRASING_3, budget=LARGE)
    assert first.run['state'] == 'completed' and first.answer.startswith('Saved ')
    later = drive(folder, LATER, budget=LARGE)  # the same data folder: a new conversation, the same memory
    request = later.requests[0][0]
    saved = [m for m in request if m.get('name') == 'pebble_saved_preferences']
    assert len(saved) == 1
    assert 'I like dates written as DD.MM.YYYY' in json.loads(saved[0]['content'].split('\n', 1)[1])[0]['content']
    assert not any('DD.MM.YYYY' in str(m.get('content', '')) for m in request if str(m.get('content', '')).startswith('RETRIEVED MEMORY'))


def drive_in(folder, workspace, text):
    """drive() in a project conversation (the memory cases run on the project surface: a learned preference's scope
    is the workspace, shared by every later session there)."""
    from pebble import runner
    folder.mkdir(parents=True, exist_ok=True)
    app = App(folder / 'data', folder / 'resources', lambda *event: None)
    requests = []
    try:
        app.store.set_setting('model_path', '')
        app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                                  'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                                  'context': LARGE, 'budget': LARGE, 'tools': True}])
        app.model.select('route')

        def complete(messages, tools, cancel, token, deliberate=False):
            requests.append([dict(m) for m in messages])
            token('Noted.\n\nFINAL: done')
            return {'role': 'assistant', 'content': 'Noted.\n\nFINAL: done'}
        app.model.complete = complete
        chat = app.new_chat(str(workspace))
        runner.run(app, app.chat(chat['id']), text, False, [])
        memories = [dict(m) for m in app.store.query('SELECT id,kind,content,origin,scope FROM memories')]
        return requests, memories
    finally:
        app.active = app.active_run = None
        app.close()
        from test_high_fd_waits_21 import close_guard_databases
        close_guard_databases(folder)


def test_a_learned_profile_quote_of_the_persons_own_sentence_is_a_standing_preference(tmp_path):
    folder, project = tmp_path / 'learned', tmp_path / 'project'
    project.mkdir()
    (project / 'README.md').write_text('# Project\n')
    _, memories = drive_in(folder, project, 'From now on, please use British spelling in replies.')
    learned = [m for m in memories if m['origin'] == 'auto-learned' and m['kind'] == 'profile']
    assert learned and learned[0]['content'] == 'From now on, please use British spelling in replies.'
    assert learned[0]['scope'] == str(project)
    requests, _ = drive_in(folder, project, 'Summarise README.md in two lines.')
    saved = [m for m in requests[0] if m.get('name') == 'pebble_saved_preferences']
    assert len(saved) == 1 and 'British spelling' in saved[0]['content']
    assert json.loads(saved[0]['content'].split('\n', 1)[1])[0]['id'] == learned[0]['id']
    untrusted = [m for m in requests[0] if str(m.get('content', '')).startswith('RETRIEVED MEMORY')]
    # not also in the untrusted pack (the first run's episode may mention the request's words; the record is moved)
    assert not any(learned[0]['id'] in str(m.get('content', '')) for m in untrusted)


def test_learned_records_without_a_verified_human_quote_stay_out_of_standing(app):
    chat = app.new_chat()['id']
    app.memory.put('profile', 'Prefers short replies (learned).', 'global', chat, origin='learned')
    app.memory.put('profile', 'Likes tabs (auto, no evidence).', 'global', chat, origin='auto-learned', ident='learned-x')
    assert app.memory.standing(app.new_chat()['id']) == []
    assert app.memory._verified_quote(json.dumps({'evidence_class': 'human_preference'})) is True
    assert app.memory._verified_quote(json.dumps({'evidence_class': 'attributed_file_quote'})) is False
    assert app.memory._verified_quote(None) is False


def test_the_cued_phrasings_offer_the_memory_tool_above_8k(tmp_path):
    from pebble.runner import MEMORY_WORDS
    for text in [PHRASING_3, 'Keep this in mind going forward: I use tabs.', 'For later sessions: my name is Sam.']:
        assert MEMORY_WORDS.search(text), text
    assert not MEMORY_WORDS.search('Fix the bug in app/parse.py and run the tests.')
