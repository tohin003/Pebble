"""Phase 43 (1.9.0, decision 9): AppAction, the fixed scripting verbs. Every osascript run here is a fake runner: no
script reaches a real app. The scripts are checked as constants (no argument is ever written into one) and for the
words they must never contain."""
import json
from pathlib import Path

import pytest

from pebble import app_actions as aa
from pebble.policy import PolicyError

US, RS = '\x1f', '\x1e'


@pytest.fixture(autouse=True)
def switch_on(monkeypatch):
    monkeypatch.delenv('PEBBLE_APP_ACTIONS', raising=False)


class Runner:
    """Records (script, argv, limit) and answers one scripted (code, out, err), or None for a time-out."""

    def __init__(self, answer=(0, '', '')):
        self.answer, self.calls = answer, []

    def __call__(self, script, argv, limit):
        self.calls.append((script, list(argv), limit))
        return self.answer


def ran(args, answer=(0, '', '')):
    runner = Runner(answer)
    payload = aa.proposal(args)
    return aa.run(payload, runner=runner), runner


def test_every_script_is_a_constant_with_argv_and_none_holds_a_forbidden_word():
    scripts = {verb[4] for verb in aa.VERBS.values()}
    assert len(scripts) == len(aa.VERBS), 'one script per verb'
    for script in scripts:
        assert script.startswith('on run argv') and not aa.FORBIDDEN.search(script), script[:60]
        assert '%s' not in script and '{0}' not in script
        assert all(ord(c) < 128 for c in script), 'ASCII only'
    assert aa.FORBIDDEN.search('tell application "System Events" to keystroke "q"')
    assert aa.FORBIDDEN.search('do shell script "rm"') and aa.FORBIDDEN.search('send m')


def test_arguments_travel_only_in_argv():
    tricky = '" & (do shell script "id") & "'
    result, runner = ran({'action': 'notes.search', 'query': tricky}, (0, '0' + RS, ''))
    script, argv, limit = runner.calls[0]
    assert script == aa.NOTES_SEARCH and argv == [tricky] and limit == aa.TIME_LIMIT
    assert result['status'] == 'done' and result['count'] == 0


def test_the_schema_is_small_and_strict():
    params = aa.SCHEMA['function']['parameters']
    assert aa.SCHEMA['function']['name'] == 'AppAction' and params['additionalProperties'] is False
    assert params['properties']['action']['enum'] == list(aa.ACTIONS) and params['required'] == ['action']
    assert set(params['properties']) == {'action', *aa.FIELDS}
    assert len(json.dumps(aa.SCHEMA, separators=(',', ':'))) < 2200


@pytest.mark.parametrize('args,needle', [
    ({'action': 'music.explode'}, 'Choose an action'),
    ({'action': 'music.play'}, 'needs query'),
    ({'action': 'music.pause', 'query': 'x'}, 'takes no other fields'),
    ({'action': 'notes.create', 'title': 't', 'text': 'b', 'subject': 's'}, 'not subject'),
    ({'action': 'notes.create', 'title': 't', 'text': 'b', 'colour': 'red'}, 'not colour'),
    ({'action': 'music.play', 'query': 'a\nb'}, 'one line'),
    ({'action': 'music.play', 'query': 'x' * 201}, '1 to 200'),
    ({'action': 'notes.create', 'title': 't', 'text': 'a\x00b'}, 'at most'),
    ({'action': 'notes.read', 'id': 'note 5'}, 'note id'),
    ({'action': 'calendar.events', 'start': 'tomorrow', 'end': '2031-01-02'}, 'local time'),
    ({'action': 'calendar.events', 'start': '2031-01-02T10:00', 'end': '2031-01-02T09:00'}, 'after start'),
    ({'action': 'calendar.events', 'start': '2031-01-01', 'end': '2031-03-01'}, '31 days'),
    ({'action': 'calendar.create', 'title': 't', 'start': '2031-01-01T10:00', 'end': '2031-01-20T10:00'}, '14 days'),
    ({'action': 'calendar.events', 'start': '1801-01-01', 'end': '1801-01-02'}, 'local time'),
    ({'action': 'mail.draft', 'to': 'not an address', 'subject': 's', 'text': 'b'}, 'email addresses'),
    ({'action': 'mail.draft', 'to': ','.join(f'a{i}@example.com' for i in range(11)), 'subject': 's', 'text': 'b'},
     'one to ten'),
    ({'action': 'mail.unread', 'limit': 21}, '0 to 20'),
    ({'action': 'mail.unread', 'limit': True}, '0 to 20'),
    ({'action': 'finder.reveal', 'path': 'relative/path'}, 'absolute'),
    ({'action': 'finder.reveal', 'path': '/no/such/place/pebble'}, 'does not exist'),
])
def test_each_verb_takes_only_its_own_checked_fields(args, needle):
    with pytest.raises(PolicyError, match=needle):
        aa.validate(args)


def test_normalized_fields():
    clean = aa.validate({'action': 'calendar.create', 'title': '  Lunch   with  Sam ', 'start': '2031-01-01 12:30:59',
                         'end': '2031-01-01T13:30', 'calendar': 'Home'})
    assert clean == {'action': 'calendar.create', 'title': 'Lunch with Sam', 'start': '2031-01-01T12:30',
                     'end': '2031-01-01T13:30', 'calendar': 'Home'}
    assert aa.validate({'action': 'calendar.events', 'start': '2031-01-01', 'end': '2031-01-02'})['end'] == \
        '2031-01-02T00:00'
    draft = aa.validate({'action': 'mail.draft', 'to': 'a@example.com; b.c+d@example.org', 'subject': 'Hi',
                         'text': 'line 1\r\nline 2'})
    assert draft['to'] == ['a@example.com', 'b.c+d@example.org'] and draft['text'] == 'line 1\nline 2'
    assert aa.validate({'action': 'music.pause', 'query': '', 'title': None}) == {'action': 'music.pause'}


def test_finder_paths_follow_the_companions_read_rules(tmp_path):
    with pytest.raises(PolicyError, match='protected'):
        aa.validate({'action': 'finder.reveal', 'path': str(Path.home() / '.ssh')})
    with pytest.raises(PolicyError, match='protected'):
        aa.validate({'action': 'finder.open_folder', 'path': '~/Library/Keychains'})
    folder = tmp_path / 'Reports'
    folder.mkdir()
    (tmp_path / 'notes.txt').write_text('x')
    assert aa.validate({'action': 'finder.open_folder', 'path': str(folder)})['path'] == str(folder.resolve())
    assert aa.validate({'action': 'finder.reveal', 'path': str(tmp_path / 'notes.txt')})['action'] == 'finder.reveal'
    for bundle in (tmp_path / 'Tool.app', tmp_path / 'Plain'):
        (bundle / 'Contents').mkdir(parents=True)
        if bundle.name == 'Plain':
            (bundle / 'Contents' / 'Info.plist').write_text('<plist/>')
        with pytest.raises(PolicyError, match='folders only'):
            aa.validate({'action': 'finder.open_folder', 'path': str(bundle)})
    with pytest.raises(PolicyError, match='folders only'):
        aa.validate({'action': 'finder.open_folder', 'path': str(tmp_path / 'notes.txt')})


def test_classes_and_reviews():
    assert aa.proposal({'action': 'shortcuts.run', 'name': 'Morning'})['requires_review'] is True
    shortcut = aa.proposal({'action': 'shortcuts.run', 'name': 'Morning', 'text': 'hello'})
    assert shortcut['class'] == 'shortcut_run' and 'anything' in shortcut['reasons'][0]
    for args in ({'action': 'notes.search', 'query': 'tax'}, {'action': 'mail.unread'},
                 {'action': 'music.now_playing'}, {'action': 'shortcuts.list'}, {'action': 'finder.selection'}):
        payload = aa.proposal(args)
        assert payload['requires_review'] is False and payload['class'] in ('read', 'read_private')
    create = aa.proposal({'action': 'notes.create', 'title': 'Groceries', 'text': 'eggs'})
    assert (create['class'], create['requires_review'], create['app']) == ('effect', False, 'Notes')
    assert create['args'] == {'title': 'Groceries', 'text': 'eggs'} and create['summary'] == \
        'Create the note “Groceries” in Notes.'
    assert {aa.VERBS[a][1] for a in ('notes.search', 'notes.read', 'mail.unread', 'calendar.events',
                                      'finder.selection')} == {'read_private'}


def test_untrusted_or_private_words_in_an_effect_wait_for_the_person():
    planted = aa.proposal({'action': 'mail.draft', 'to': 'drop@evil.example', 'subject': 'Report',
                           'text': 'please forward the quarterly numbers'},
                          untrusted={'drop@evil.example'})
    assert planted['requires_review'] is True and 'page' in planted['reasons'][0]
    leaked = aa.proposal({'action': 'notes.create', 'title': 'x', 'text': 'acct 4111222233334444'},
                         private=lambda text: ['4111222233334444'] if '4111222233334444' in text else [])
    assert leaked['requires_review'] is True and 'files Pebble read' in leaked['reasons'][0]
    read = aa.proposal({'action': 'notes.search', 'query': 'drop@evil.example'}, untrusted={'drop@evil.example'})
    assert read['requires_review'] is False, 'a read sends nothing'


def test_the_switch_turns_app_actions_off(monkeypatch):
    payload = aa.proposal({'action': 'music.pause'})
    monkeypatch.setenv('PEBBLE_APP_ACTIONS', '0')
    assert aa.enabled() is False
    for call in (lambda: aa.proposal({'action': 'music.pause'}), lambda: aa.run(payload, runner=Runner())):
        with pytest.raises(PolicyError, match='off'):
            call()


def test_argv_for_each_kind_of_verb(tmp_path):
    cases = {
        ('shortcuts.run', (('name', 'Morning'),)): ['Morning'],
        ('shortcuts.run', (('name', 'Morning'), ('text', 'hi'))): ['Morning', 'hi'],
        ('calendar.events', (('start', '2031-01-01T03:17'), ('end', '2031-01-01T03:18'))):
            ['2031-01-01T03:17', '2031-01-01T03:18'],
        ('calendar.create', (('title', 'Run'), ('start', '2031-01-01T07:00'), ('end', '2031-01-01T08:00'))):
            ['Run', '2031-01-01T07:00', '2031-01-01T08:00'],
        ('mail.unread', ()): ['5'],
        ('mail.unread', (('limit', 0),)): ['0'],
        ('mail.draft', (('to', 'a@example.com, b@example.com'), ('subject', 'S'), ('text', 'B'))):
            ['S', 'B', 'a@example.com', 'b@example.com'],
        ('music.next', ()): [],
    }
    for (action, fields), argv in cases.items():
        assert aa._argv(aa.validate({'action': action, **dict(fields)})) == argv, action
    body = aa._argv(aa.validate({'action': 'notes.create', 'title': 'A <b>&</b>', 'text': 'one\n\n<script>',
                                 'folder': 'Work'}))
    assert body == ['<div><h1>A &lt;b&gt;&amp;&lt;/b&gt;</h1></div><div>one</div><div><br></div>'
                    '<div>&lt;script&gt;</div>', 'Work']


def test_reads_parse_bounded_and_untrusted():
    result, _ = ran({'action': 'shortcuts.list'}, (0, RS.join(['Morning', 'x' * 300, 'Focus']) + RS, ''))
    assert result['count'] == 3 and len(result['shortcuts'][1]) == 200 and result['untrusted'] == aa.UNTRUSTED
    now, _ = ran({'action': 'music.now_playing'}, (0, US.join(['playing', 'Navior', 'Artist', 'Album']), ''))
    assert now['state'] == 'playing' and now['track'] == {'name': 'Navior', 'artist': 'Artist', 'album': 'Album'}
    idle, _ = ran({'action': 'music.now_playing'}, (0, 'not_running', ''))
    assert idle['state'] == 'not_running' and idle['track'] is None
    note_id = 'x-coredata://0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0/ICNote/p42'
    found, _ = ran({'action': 'notes.search', 'query': 'tax'},
                   (0, '25' + RS + US.join([note_id, 'Tax 2025', '2026-10-25T09:00']) + RS + 'garbage' + RS, ''))
    assert found['count'] == 25 and found['truncated'] is True
    assert found['notes'] == [{'id': note_id, 'title': 'Tax 2025', 'modified': '2026-10-25T09:00'}]
    note, _ = ran({'action': 'notes.read', 'id': note_id},
                  (0, US.join(['note', 'Tax 2025', '2026-10-25T09:00', 'line' + US + 'with a separator']), ''))
    assert note['title'] == 'Tax 2025' and note['text'] == 'line' + US + 'with a separator'
    locked, _ = ran({'action': 'notes.read', 'id': note_id}, (0, 'locked', ''))
    assert locked['locked'] is True and locked['text'] is None
    events, _ = ran({'action': 'calendar.events', 'start': '2031-01-01', 'end': '2031-01-02'},
                    (0, '1' + RS + US.join(['Dentist', '2031-01-01T09:00', '2031-01-01T10:00', 'Home', 'UID-1']) + RS,
                     ''))
    assert events['events'] == [{'title': 'Dentist', 'start': '2031-01-01T09:00', 'end': '2031-01-01T10:00',
                                 'calendar': 'Home', 'id': 'UID-1'}] and 'repeating' in events['note']
    mail, _ = ran({'action': 'mail.unread', 'limit': 1}, (0, '7' + RS + US.join(['Hello', '2026-10-25T08:00']) + RS, ''))
    assert mail['unread'] == 7 and mail['newest'] == [{'subject': 'Hello', 'received': '2026-10-25T08:00'}]
    texts = aa.private_texts(mail)
    assert 'Hello' in texts


def test_finder_selection_withholds_protected_paths(tmp_path):
    secret = str(Path.home() / '.ssh' / 'id_rsa')
    result, _ = ran({'action': 'finder.selection'}, (0, '2' + RS + str(tmp_path / 'a.txt') + RS + secret + RS, ''))
    assert result['count'] == 2 and result['paths'] == [str(tmp_path / 'a.txt')] and result['withheld'] == 1


def test_effects_report_only_what_the_app_said(tmp_path):
    note_id = 'x-coredata://0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0/ICNote/p43'
    made, runner = ran({'action': 'notes.create', 'title': 'Groceries', 'text': 'eggs'},
                       (0, US.join([note_id, 'Groceries', 'Notes']), ''))
    assert made['created'] == {'id': note_id, 'title': 'Groceries', 'folder': 'Notes'}
    assert aa.describe(made) == 'Notes reported creating the note “Groceries”.'
    event, _ = ran({'action': 'calendar.create', 'title': 'Run', 'start': '2031-01-01T07:00', 'end': '2031-01-01T08:00'},
                   (0, US.join(['UID-9', 'Home']), ''))
    assert event['created']['calendar'] == 'Home' and 'Calendar reported adding “Run”' in aa.describe(event)
    draft, _ = ran({'action': 'mail.draft', 'to': 'sam@example.com', 'subject': 'Hi', 'text': 'Hello'},
                   (0, 'drafted', ''))
    assert draft['drafted'] is True and 'never sends' in draft['note'] and 'You review and send' in aa.describe(draft)
    play, _ = ran({'action': 'music.play', 'query': 'navior'}, (0, US.join(['playing', 'Navior', 'Band']), ''))
    assert play['playing'] == {'name': 'Navior', 'artist': 'Band'} and 'reported playing' in aa.describe(play)
    none, _ = ran({'action': 'music.play', 'query': 'zzz'}, (0, 'none', ''))
    assert none['found'] == 0 and 'found nothing' in aa.describe(none)
    folder = tmp_path / 'Reports'
    folder.mkdir()
    shown, runner = ran({'action': 'finder.open_folder', 'path': str(folder)}, (0, 'opened', ''))
    assert runner.calls[0][1] == [str(folder.resolve())] and 'Finder showed' in aa.describe(shown)


def test_refusals_and_time_outs_are_said_plainly():
    denied, _ = ran({'action': 'notes.search', 'query': 'tax'},
                    (1, '', '0:10: execution error: Not authorized to send Apple events to Notes. (-1743)\n'))
    assert denied['status'] == 'failed' and denied['error'] == -1743 and 'Automation' in denied['message']
    assert 'Apple events' not in json.dumps(denied), 'the app\'s own words never reach the model'
    missing, _ = ran({'action': 'notes.read', 'id': 'x-coredata://A/ICNote/p1'}, (1, '', 'error (-1728)'))
    assert 'no such item' in missing['message']
    odd, _ = ran({'action': 'music.pause'}, (1, '', 'something odd'))
    assert odd['error'] is None and 'reported an error' in odd['message']
    late, _ = ran({'action': 'notes.create', 'title': 't', 'text': 'b'}, None)
    assert late['status'] == 'uncertain' and 'may or may not' in late['message'] and 'nothing was retried' in \
        aa.describe(late)
    slow, _ = ran({'action': 'mail.unread'}, None)
    assert slow['status'] == 'failed'
    shortcut, runner = ran({'action': 'shortcuts.run', 'name': 'Morning'}, (0, 'done' + RS, ''))
    assert runner.calls[0][2] == aa.SHORTCUT_TIME_LIMIT and shortcut['ran'] == 'Morning'


def test_run_checks_the_arguments_again(tmp_path):
    folder = tmp_path / 'Reports'
    folder.mkdir()
    payload = aa.proposal({'action': 'finder.open_folder', 'path': str(folder)})
    folder.rmdir()
    with pytest.raises(PolicyError, match='does not exist'):
        aa.run(payload, runner=Runner())
    with pytest.raises(PolicyError):
        aa.run({'action': 'music.pause'}, runner=Runner())
