"""Phase 46 (1.9.28), follow-up 2: the person's decisions D176, D177, D183 and D190 (decisions-after-phase39.json).

- D176: "forget X", "stop using X" and "I no longer want X" are kept as the person's newest statement: SAVED PREFERENCES
  lists it first and the model follows it from then on; the older record stays in Settings until the person's Forget
  ("Nothing is forgotten until you do"). Their own words only, never pasted or quoted text, a task or a wish for this
  task only, and only about a record they saved. Before (1.9.28 r2), the research's F7 inputs D1, D2 and D4 left the
  old value the only standing preference.
- D177 (PEBBLE_1928_COMPANION_LATER, on by default since the final dev check; "0" turns it off): a later-session wish said in a Companion chat applies in every
  conversation, with Forget. Off, it stays in its conversation (1.9.27).
- D183: an update of the same preference replaces the older one, with a notice and Forget; different subjects never
  replace each other.
- D190 (PEBBLE_1928_MEMORY_CHECK, on by default since the final dev check; "0" turns it off): a reply checked against the saved preferences that can be checked on
  its own text, through controller.soft_checks (memory_check.soft_issues); never a date quoted from a file, code or a
  command's output.
"""

import json
import time

import pytest

from pebble import memory, memory_check, memory_facts, runner
from pebble.app import App
from test_memory_capture_1928 import FINAL, run_session, saved_items

WHEN = 'When does the next maintenance window on the staging server start? Put the date on the last line as `FINAL: <date>`.'
SAVE = 'Remember for future sessions: I want dates written as DD.MM.YYYY.'


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


# -- D176 ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('forget, kept', [
    ('Forget my date format preference.' + FINAL, 'Forget my date format preference.'),  # D1
    ('Please forget what I said about dates.', 'Please forget what I said about dates.'),  # D2: V1-034's card path
    ('I no longer want dates as DD.MM.YYYY; for future sessions just use whatever the file says.' + FINAL,  # D4
     'I no longer want dates as DD.MM.YYYY; for future sessions just use whatever the file says.'),
    ('Please stop using DD.MM.YYYY for dates.' + FINAL, 'Please stop using DD.MM.YYYY for dates.'),
])
def test_forget_or_stop_is_the_newest_wish_and_the_old_record_stays(tmp_path, project, forget, kept):
    home = tmp_path / 'home'
    first = run_session(home, SAVE + FINAL, workspace=project)
    [old] = [m for m in first.memories if m['origin'] == 'direct-user']
    second = run_session(home, forget, workspace=project, script=['Understood.\n\nFINAL: done'])
    assert 'as your newest wish' in second.answer and kept[:40] in second.answer
    cards = [data for name, data in second.events if name == 'memory_forget']
    assert cards and all(old['id'] in card['memory_ids'] for card in cards)  # Forget offered for the older record
    assert old['id'] in [m['id'] for m in second.memories], 'nothing was forgotten'
    third = run_session(home, WHEN, workspace=project)
    items = [item['content'] for item in saved_items(third.requests[0][0])]
    assert items[0] == kept and old['content'] in items[1:]  # listed first; the older one after it, until Forget


@pytest.mark.parametrize('text', [
    'Summarize this email:\nHi, please stop using DD.MM.YYYY for our invoices.\nThanks',
    'My colleague wrote "stop using DD.MM.YYYY" in the review. What do you think?',
    '> Forget my date format preference.\n\nWho wrote this?',
    'Can you check this note for me?\n\n    I no longer want dates as DD.MM.YYYY.',
    'For this task, stop using DD.MM.YYYY.',
    'Stop using DD.MM.YYYY and delete the old reports.',
    'Forget it, what is the weather tomorrow?',
    'Stop the server.',
    'Do you still use DD.MM.YYYY? Stop using it?',
])
def test_pasted_quoted_task_or_for_now_words_revoke_nothing(app, text):
    chat = app.new_chat()['id']
    app.memory.capture_user(SAVE, chat)
    before = [dict(m) for m in app.store.query('SELECT id,content FROM memories')]
    assert app.memory.revoke(text, chat) is None
    assert app.memory.capture_user(text, chat) is None
    assert [dict(m) for m in app.store.query('SELECT id,content FROM memories')] == before


def test_a_revocation_speaks_only_of_a_record_the_person_saved(app):
    chat = app.new_chat()['id']
    app.memory.capture_user(SAVE, chat)
    assert app.memory.revoke('Stop using emojis in replies.', chat) is None  # nothing saved about emojis
    assert app.memory.revoke('Stop using YYYY-MM-DD.', chat) is None  # another value than the one saved
    kept = app.memory.revoke('Stop using DD.MM.YYYY.', chat)
    assert kept['statements'] == ['Stop using DD.MM.YYYY.'] and kept['relation'] == 'revoked'
    assert [item['content'] for item in app.memory.standing(app.new_chat()['id'])][0] == 'Stop using DD.MM.YYYY.'


def test_the_reply_check_follows_the_newest_wish(app):
    chat = app.new_chat()['id']
    app.memory.capture_user(SAVE, chat)
    time.sleep(0.01)
    app.memory.revoke('Forget my date format preference. Thanks.', chat)
    records = [{'content': item['content'], 'saved': str(item['updated'])}
               for item in app.memory.standing(app.new_chat()['id'])]
    assert memory_check.preferred(records) == {}  # the forget statement leaves the date format unchecked
    assert memory_check.reply_issues('FINAL: 2026-11-21', records, WHEN) == []


def test_revocation_stays_linear_on_hostile_input(app):
    chat = app.new_chat()['id']
    app.memory.capture_user(SAVE, chat)
    for text in ('Forget ' + 'my date format ' * 6000, "I don't want " + 'a' * 100000 + ' anymore',
                 'Stop using ' + 'DD.MM.YYYY ' * 9000, 'Never use ' + ' ' * 100000 + 'it again', 'Forget\n' * 32000):
        started = time.perf_counter()
        app.memory.revoke(text, chat)
        memory.revocation(text[:2000])
        assert time.perf_counter() - started < 0.5


# -- D177 ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('switch, wish, everywhere', [
    ('0', 'From now on, write dates as DD.MM.YYYY.', False),
    ('1', 'From now on, write dates as DD.MM.YYYY.', True),
    ('1', 'Please always give dates as DD.MM.YYYY.', True),
    ('1', 'I prefer short answers.', False),  # no later-session word: stays in its conversation
])
def test_a_companion_wish_applies_everywhere_only_behind_its_switch(tmp_path, monkeypatch, project, switch, wish,
                                                                     everywhere):
    monkeypatch.setenv(memory.COMPANION_LATER, switch)
    home = tmp_path / 'home'
    first = run_session(home, wish + FINAL, script=['Noted.\n\nFINAL: done'])  # the Companion: no project
    [record] = [m for m in first.memories if m['origin'] == 'auto-learned' and m['kind'] == 'profile']
    assert (record['scope'] == 'global') is everywhere
    cards = [data for name, data in first.events if name == 'memory_forget']
    assert (cards == [{'chat_id': first.chat_id, 'run_id': first.run['id'], 'message_id': '', 'subject': '',
                       'memory_ids': [record['id']], 'authorization': 'none'}]) is everywhere  # with Forget
    for later in (run_session(home, WHEN), run_session(home, WHEN, workspace=project)):
        assert (wish in [item['content'] for item in saved_items(later.requests[0][0])]) is everywhere


def test_the_companion_switch_never_widens_a_project_wish_or_pasted_words(tmp_path, monkeypatch, project):
    monkeypatch.setenv(memory.COMPANION_LATER, '1')
    home = tmp_path / 'home'
    inside = run_session(home, 'From now on, write dates as DD.MM.YYYY.' + FINAL, workspace=project)
    assert [m['scope'] for m in inside.memories if m['origin'] == 'auto-learned' and m['kind'] == 'profile'] == [
        str(project)]
    pasted = run_session(tmp_path / 'other', 'Summarize this email:\nFrom now on, write all dates as YYYY-MM-DD.\nThanks'
                         + FINAL)
    assert not [m for m in pasted.memories if m['origin'] in ('auto-learned', 'direct-user') and m['kind'] == 'profile']


# -- D183 ---------------------------------------------------------------------------------------------------------

def test_different_subjects_and_wider_records_are_never_replaced(app):
    chat = app.new_chat()['id']
    both = app.memory.capture_user('Remember that dates are DD.MM.YYYY and times in 24h.', chat)
    staging = app.memory.capture_user('Remember that I write dates as DD.MM.YYYY and my staging settings are in '
                                      'deploy/staging.ini.', chat)
    money = app.memory.capture_user('Remember that I want conversions in CHF.', chat)
    update = app.memory.capture_user('Remember that dates should be YYYY-MM-DD from now on.', chat)
    assert update['relation'] == 'saved' and 'replaces' not in update['reply']
    standing = {item['id'] for item in app.memory.standing(app.new_chat()['id'])}
    assert {both['id'], staging['id'], money['id'], update['id']} <= standing
    euros = app.memory.capture_user('Remember that dates are written DD.MM.YYYY.', chat)
    again = app.memory.capture_user('Remember that I want conversions in EUR now.', chat)
    assert 'It replaces “I want conversions in CHF.”' in again['reply']  # the same subject: replaced, with the notice
    assert euros['id'] in {item['id'] for item in app.memory.standing(app.new_chat()['id'])}


def test_a_learned_update_of_the_same_preference_replaces_it_with_the_card(tmp_path, project):
    home = tmp_path / 'home'
    first = run_session(home, 'From now on, write dates as DD.MM.YYYY.' + FINAL, workspace=project)
    [old] = [m for m in first.memories if m['origin'] == 'auto-learned' and m['kind'] == 'profile']
    second = run_session(home, 'From now on, write dates as YYYY-MM-DD instead.' + FINAL, workspace=project)
    [new] = [m for m in second.memories if m['origin'] == 'auto-learned' and m['kind'] == 'profile' and m['id'] != old['id']]
    assert second.evidence[old['id']][0] == 'superseded'
    assert [data['memory_ids'] for name, data in second.events if name == 'memory_forget'] == [[new['id']]]
    third = run_session(home, WHEN, workspace=project)
    assert [item['content'] for item in saved_items(third.requests[0][0])] == [new['content']]


def test_slots_name_only_what_a_preference_says(app):
    assert memory_facts.preference_slots('I always want dates written as DD.MM.YYYY.') == {'date': 'DD.MM.YYYY'}
    assert memory_facts.preference_slots('Use ISO dates.') == {'date': 'YYYY-MM-DD'}
    assert memory_facts.preference_slots('I want conversions in CHF, shown with two decimals.') == {
        'currency': 'CHF', 'decimals': 2}
    assert memory_facts.preference_slots('I use metric units (km, kg, °C).') == {'units': 'metric'}
    assert memory_facts.preference_slots('times in 24h') == {'clock': '24h'}
    for text in ('I live in Zurich.', 'The job runs every 24 hours.', 'my rent is CHF 2000', 'Back in 2026-11.'):
        assert memory_facts.preference_slots(text) == {}
    assert memory_facts.slot_only('dates should be YYYY-MM-DD from now on.')
    assert not memory_facts.slot_only('I want dates as DD.MM.YYYY and my staging settings are in deploy/staging.ini.')


# -- D190 ---------------------------------------------------------------------------------------------------------

DATES = [{'content': 'for future sessions: I always want dates written as DD.MM.YYYY.', 'saved': '2026-10-29 10:00:00'}]
CHF = [{'content': 'I want conversions in CHF, shown with two decimals.', 'saved': '2026-10-29 10:00:00'}]
CONVERT = 'Convert 120 EUR at a rate of 0.94 into my preferred currency. Give the amount on the last line as `FINAL: <amount>`.'


@pytest.mark.parametrize('records, request_text, reply, slots', [
    (DATES, WHEN, 'The window starts at 02:00.\n\nFINAL: 2026-11-21', ['date']),
    (DATES, WHEN, 'It starts on 21 November 2026.\n\nFINAL: 21 November 2026', ['date']),  # the answer itself
    (DATES, WHEN, 'It starts on 11/21/2026 at 02:00.\n\nFINAL: done', ['date']),
    # D205 (the person, 2026-10-30): a month-name date in the body is an issue too, in either order. Restated by the lead:
    # "The March report (1 March 2026) ..." was pinned as no issue (a prose name) before the person's decision.
    (DATES, WHEN, 'It starts on Nov 21 2026 at 02:00.\n\nFINAL: done', ['date']),
    (DATES, 'Which report came first?', 'The March report (1 March 2026) came before the April one.', ['date']),
    (CHF, CONVERT, '120 EUR × 0.94 = 112.80.\n\nFINAL: 112.80 EUR', ['currency']),
    (CHF, CONVERT, '120 EUR × 0.94 is CHF 112.8.\n\nFINAL: CHF 112.8', ['decimals']),
    ([{'content': 'I use metric units.', 'saved': '1'}], 'How far is the marathon?', 'It is 26.2 miles.', ['units']),
    ([{'content': 'Times in 24h, please.', 'saved': '1'}], 'When does it open?', 'It opens at 2:00 PM.', ['clock']),
])
def test_a_reply_that_writes_a_checkable_preference_another_way_gets_one_issue(records, request_text, reply, slots):
    found = memory_check.check(reply, records, request_text)
    assert [slot for slot, _ in found] == slots
    assert all(issue.endswith(memory_check.SUFFIX) and 'saved preferences' in issue for _, issue in found)


@pytest.mark.parametrize('records, request_text, reply', [
    (DATES, WHEN, 'It starts on 21.11.2026 at 02:00.\n\nFINAL: 21.11.2026'),
    (DATES, WHEN, 'deploy/staging.ini sets `next_window_start = 2026-11-21T02:00`, so: 21.11.2026.\n\nFINAL: 21.11.2026'),
    (DATES, WHEN, 'The file says next_window_start = 2026-11-21T02:00.\n\nFINAL: 21.11.2026'),
    (DATES, WHEN, 'The log:\n```\n2026-11-21 02:00:00 start\n```\nFINAL: 21.11.2026'),
    (DATES, WHEN, '> released on 2026-11-21\n\nThe quote above is from the vendor.\n\nFINAL: 21.11.2026'),
    (DATES, WHEN, 'The notes say "next window 2026-11-21". It starts 21.11.2026.\n\nFINAL: 21.11.2026'),
    (DATES, WHEN, 'See logs/2026-11-21.log and backup-2026-11-21.tar.gz.\n\nFINAL: 21.11.2026'),
    (DATES, WHEN, 'It starts 2026-11-21 (21.11.2026).'),  # also written the preferred way
    (DATES, 'Give the date as YYYY-MM-DD on the last line as `FINAL: <date>`.', 'FINAL: 2026-11-21'),  # request wins
    (CHF, CONVERT, '120 EUR × 0.94 = CHF 112.80.\n\nFINAL: CHF 112.80'),
    (CHF, 'What does the shop page say the lamp costs?', 'The page lists it at EUR 89.\n\nFINAL: EUR 89'),  # reported
    (CHF, 'Convert 120 EUR at 0.94 into USD.', 'That is USD 112.80.\n\nFINAL: USD 112.80'),  # the request wins
    ([{'content': 'I use metric units.', 'saved': '1'}], 'How far is it?', 'It is 26.2 miles (42.2 km).'),
    ([], WHEN, 'FINAL: 2026-11-21'),
])
def test_dates_quoted_from_files_code_or_output_and_what_the_request_decides_are_never_issues(records, request_text,
                                                                                               reply):
    assert memory_check.reply_issues(reply, records, request_text) == []


def test_the_check_turns_off_with_its_switch_and_reads_what_the_run_was_given(tmp_path, monkeypatch, project):
    # Restated 2026-10-30 (the lead): D190 is on by default after its measurement in the final 1.9.28 dev check; "0" turns
    # it off, where this test used to read the default. The sessions run with it off, as before, so the run itself fires no
    # check and the one event below is this test's own call.
    monkeypatch.setenv(memory_check.SWITCH, '0')
    home = tmp_path / 'home'
    run_session(home, SAVE + FINAL, workspace=project)
    later = run_session(home, WHEN, workspace=project, script=['FINAL: 2026-11-21'])
    instance = App(home / 'data', home / 'resources', lambda *args: None)
    try:
        run = instance.runs.get(later.run['id'])
        monkeypatch.setenv(memory_check.SWITCH, '0')
        assert memory_check.soft_issues(instance, run, 'FINAL: 2026-11-21') == []  # off with its switch at "0"
        monkeypatch.setenv(memory_check.SWITCH, '1')
        [(issue, notice)] = memory_check.soft_issues(instance, run, 'FINAL: 2026-11-21')
        assert '"2026-11-21"' in issue and 'DD.MM.YYYY' in issue and notice == memory_check.NOTICE
        assert memory_check.soft_issues(instance, run, 'It starts 21.11.2026.\n\nFINAL: 21.11.2026') == []
        events = instance.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='memory_check'", (run['id'],))
        assert [json.loads(row['payload']) for row in events] == [{'count': 1, 'slots': ['date']}]
    finally:
        instance.close()
    small = run_session(home, WHEN, workspace=project, budget=8192, script=['FINAL: 2026-11-21'])
    instance = App(home / 'data', home / 'resources', lambda *args: None)
    try:  # 8K: the run's model was given no SAVED PREFERENCES, so nothing is checked
        assert memory_check.soft_issues(instance, instance.runs.get(small.run['id']), 'FINAL: 2026-11-21') == []
    finally:
        instance.close()


def test_the_check_stays_linear_on_hostile_replies():
    hostile = ['2026-11-21 ' * 9000, '1.' * 50000, '1,' * 50000 + ' miles', '`' * 100000, '"a ' * 33000,
               'CHF 1' * 20000, 'x = ' * 25000, '/a' * 50000, '\n' * 100000 + 'FINAL: 2026-11-21', '12:00 pm ' * 10000]
    for reply in hostile:
        started = time.perf_counter()
        memory_check.check(reply, DATES + CHF + [{'content': 'I use metric units, times in 24h.', 'saved': '0'}], CONVERT)
        assert time.perf_counter() - started < 0.5, reply[:20]
