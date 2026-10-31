"""Phase 46 (1.9.28, F3): an updated preference wins over the one it replaces.

Measured first (offline, three sessions through the real runner; the 1.9.28 memory research's U2, W2 and U3): after
"Remember that I want dates as DD.MM.YYYY." and then "Remember that dates should be YYYY-MM-DD from now on." (or
"From now on, write dates as YYYY-MM-DD.", or a moved settings file), the third session's SAVED PREFERENCES listed the
old value first, undated, with no rule for a disagreement. The order came from the runner (recall's records first, then
memory.standing()'s), not from standing(), which orders newest first. Now the message lists every item newest first
with when it was saved, and says that the newer one is the person's current wish; an update that names the value it
replaces ("..., not DD/MM/YYYY", "... instead of DD.MM.YYYY") supersedes the older record holding it. Above 8K only,
as before (4K and 8K carry no SAVED PREFERENCES message: test_saved_preferences_46).
"""

import re

import pytest

from pebble import runner
from pebble.app import App
from test_memory_capture_1928 import FINAL, run_session, saved_items

WHEN = ('When does the next maintenance window on my staging server start? Give the date in my preferred format on '
        'the last line as `FINAL: <date>`.')


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield instance
    instance.close()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    (root / 'deploy').mkdir(parents=True)
    (root / 'deploy' / 'staging.ini').write_text('[maintenance]\nnext_window_start = 2026-11-21T02:00\n')
    return root


@pytest.mark.parametrize('update, kept', [
    ('Remember that dates should be YYYY-MM-DD from now on.', 'dates should be YYYY-MM-DD from now on.'),
    ('Change of plan for future sessions: I now want dates written as YYYY-MM-DD.',
     'I now want dates written as YYYY-MM-DD.'),
])
def test_an_update_of_the_same_preference_replaces_the_older_one_with_a_notice(tmp_path, project, update, kept):
    """Follow-up 2 (D183, the person's decision): the update takes effect as the newest statement and replaces the
    older one, with the notice and Forget (memory_graph.supersede_slot); 1.9.28 listed both, newest first, and the first
    update, saved as a project fact, could not replace the person's own global record."""
    home = tmp_path / 'home'
    run_session(home, 'Remember that I want dates as DD.MM.YYYY.' + FINAL, workspace=project)
    second = run_session(home, update + FINAL, workspace=project)
    assert 'It replaces “I want dates as DD.MM.YYYY.”' in second.answer
    [card] = [data for name, data in second.events if name == 'memory_forget']
    assert [m['content'] for m in second.memories if m['id'] in card['memory_ids']] == [kept]
    third = run_session(home, WHEN, workspace=project)
    assert [item['content'] for item in saved_items(third.requests[0][0])] == [kept]


@pytest.mark.parametrize('update', [
    'From now on, write dates as YYYY-MM-DD.',  # the learning layer's record of the person's sentence (this project)
])
def test_the_third_session_lists_the_update_first_with_its_date_and_the_rule(tmp_path, project, update):
    home = tmp_path / 'home'
    run_session(home, 'Remember that I want dates as DD.MM.YYYY.' + FINAL, workspace=project)
    run_session(home, update + FINAL, workspace=project)
    third = run_session(home, WHEN, workspace=project)
    [message] = [m for m in third.requests[0][0] if m.get('name') == 'pebble_saved_preferences']
    header = message['content'].split('\n', 1)[0]
    assert "the person's standing instructions" in header and 'the newer one is the person\'s current wish' in header
    items = saved_items(third.requests[0][0])
    assert 'YYYY-MM-DD' in items[0]['content'] and 'DD.MM.YYYY' in items[-1]['content']
    assert all(re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', item['saved']) for item in items)
    assert items[0]['saved'] >= items[-1]['saved']


def test_an_update_that_names_the_replaced_value_supersedes_it(tmp_path, project):
    home = tmp_path / 'home'
    first = run_session(home, 'Remember for future sessions: dates should be DD/MM/YYYY.' + FINAL, workspace=project)
    [old] = [m for m in first.memories if m['origin'] == 'direct-user']
    second = run_session(home, 'Correction to what I asked you to remember: dates should be DD.MM.YYYY, not DD/MM/YYYY.'
                         + FINAL, workspace=project)
    [new] = [m for m in second.memories if m['origin'] == 'direct-user' and m['id'] != old['id']]
    assert second.evidence[old['id']][0] == 'superseded'
    third = run_session(home, WHEN, workspace=project)
    assert [item['content'] for item in saved_items(third.requests[0][0])] == [new['content']]


def test_a_value_named_about_another_subject_supersedes_nothing(app, project):
    chat = app.new_chat(str(project))['id']
    staging = app.memory.capture_user('Remember that my staging settings are in deploy/staging.ini.', chat, str(project))
    app.memory.capture_user('Remember: the production config is deploy/production.ini, not deploy/staging.ini.', chat,
                            str(project))
    standing = {item['id'] for item in app.memory.standing(app.new_chat(str(project))['id'], str(project))}
    assert staging['id'] in standing and len(standing) == 2
    # "tabs, not spaces": an ordinary word is no value to replace
    spaces = app.memory.capture_user('Remember that YAML files use spaces.', chat, str(project))
    app.memory.capture_user('Remember that I prefer tabs, not spaces.', chat, str(project))
    assert spaces['id'] in {item['id'] for item in app.memory.standing(app.new_chat(str(project))['id'], str(project))}


def test_saying_a_replaced_statement_again_makes_it_current_again(app, project):
    chat = app.new_chat(str(project))['id']
    old = app.memory.capture_user('Remember that dates should be DD/MM/YYYY.', chat, str(project))
    app.memory.capture_user('Remember that dates should be DD.MM.YYYY, not DD/MM/YYYY.', chat, str(project))
    assert old['id'] not in {item['id'] for item in app.memory.standing(app.new_chat(str(project))['id'], str(project))}
    again = app.memory.capture_user('Remember that dates should be DD/MM/YYYY.', chat, str(project))
    assert again['id'] == old['id']
    assert old['id'] in {item['id'] for item in app.memory.standing(app.new_chat(str(project))['id'], str(project))}


def test_a_persons_fact_that_recall_chose_comes_once_as_theirs_not_also_as_untrusted_data(tmp_path, project):
    home = tmp_path / 'home'
    run_session(home, 'Remember that I want dates as DD.MM.YYYY.' + FINAL, workspace=project)
    second = run_session(home, 'Remember that dates should be YYYY-MM-DD from now on.' + FINAL, workspace=project)
    request = second.requests[0][0]
    assert 'dates should be YYYY-MM-DD from now on.' in [item['content'] for item in saved_items(request)]
    untrusted = [m['content'] for m in request if str(m.get('content', '')).startswith('RETRIEVED MEMORY')]
    assert not any('"dates should be YYYY-MM-DD from now on."' in text for text in untrusted)  # 1.9.27: in both


def test_saved_when_is_local_time_and_nothing_without_one():
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', runner.saved_when({'updated': 1793000000.5})['saved'])
    assert runner.saved_when({}) == {} and runner.saved_when({'updated': True}) == {}
