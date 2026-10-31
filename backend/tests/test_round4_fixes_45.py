"""Phase 45 (1.9.7, audit round R4): the Workshop's functions take their documented arguments in order (F147: a
positional keep_tool raised, nothing was kept, and the reply said it was); a reply that claims a kept tool the run did not
keep gets the host's line; keypad keys enter input instead of asking (F150); observations last long enough for a
reasoning model (F149); an app on this Mac named in the request is said to be one (F152)."""
from types import SimpleNamespace

import pytest

from pebble import code_worker, runner
from pebble import screen_session as ss


def test_workshop_functions_take_their_arguments_in_order():
    calls = []
    keep = code_worker._workshop(lambda name, args: calls.append((name, args)) or 'ok', 'keep_tool')
    make = code_worker._workshop(lambda name, args: calls.append((name, args)) or 'ok', 'make_tool')
    assert keep('uxtest_c_to_f', 'it helped') == 'ok'
    assert make('c_to_f', 'convert', 'def run(args): ...', [], {}, ) == 'ok'
    assert keep(name='a', reason='b') == 'ok'
    assert calls == [('keep_tool', {'name': 'uxtest_c_to_f', 'reason': 'it helped'}),
                     ('make_tool', {'name': 'c_to_f', 'purpose': 'convert', 'code': 'def run(args): ...', 'tests': [],
                                    'inputs': {}}),
                     ('keep_tool', {'name': 'a', 'reason': 'b'})]
    with pytest.raises(TypeError, match='at most 2'):
        keep('a', 'b', 'c')
    with pytest.raises(TypeError, match='twice'):
        keep('a', name='b')


def store_with(kinds):
    rows = [{'kind': k} for k in kinds]
    return SimpleNamespace(store=SimpleNamespace(query=lambda sql, params: rows))


def test_a_claimed_keep_the_run_did_not_make_gets_the_hosts_line():
    claim = 'Kept the tool uxtest_c_to_f, which converts Celsius to Fahrenheit.'
    assert runner.keep_notice(store_with([]), 'run', claim) == runner.KEPT_NOTHING
    assert runner.keep_notice(store_with(['workshop_asked']), 'run', claim) == runner.KEEP_WAITS
    assert runner.keep_notice(store_with(['workshop_kept']), 'run', claim) is None
    assert runner.keep_notice(store_with([]), 'run', 'The answer is 77.') is None
    for claim in ("I've kept the tool c_to_f.", 'Done. Saved c_to_f in your Toolbox.', 'I added it to the toolbox.',
                  "The tool works. I kept it in Pebble's Toolbox."):
        assert runner.keep_notice(store_with([]), 'run', claim) == runner.KEPT_NOTHING, claim
    for mention in ('Your toolbox has c_to_f, which you kept yesterday.', 'The Toolbox lists tools Pebble kept earlier.',
                    'To keep a tool, say "keep it".'):
        assert runner.keep_notice(store_with([]), 'run', mention) is None, mention


@pytest.mark.parametrize('label, klass', [('7', 'input'), ('\N{MULTIPLICATION SIGN}', 'input'), ('=', 'input'),
                                          ('multiply', 'input'), ('All Clear', 'input'), ('Per cent', 'input'),
                                          ('Send', 'commit'), ('OK', 'commit'), ('Delete', 'commit'), ('', 'commit'),
                                          # a lone + or - is a list's Add or Remove button (Remove can delete)
                                          ('+', 'commit'), ('-', 'commit'), ('\N{MINUS SIGN}', 'commit')])
def test_keypad_keys_enter_input_and_everything_else_still_asks(label, klass):
    assert ss.classify('press', {'role': 'AXButton', 'title': label}, kind='app')[0] == klass


def test_a_keypad_key_in_a_browser_page_is_unchanged():
    assert ss.classify('press', {'role': 'AXButton', 'title': '7'}, kind='browser')[0] == 'commit'


def test_observations_outlast_a_reasoning_models_turn():
    assert ss.OBSERVATION_MAX_AGE >= 45  # audit round R10: 15 s still refused 12 of 144 acts (deliberate)
    observe = next(s for s in ss.SCHEMAS if s['function']['name'] == 'ScreenObserve')
    assert 'stale after 45 seconds' in observe['function']['description']


def test_an_installed_app_named_in_the_request_is_said_to_be_one(monkeypatch):
    monkeypatch.setitem(runner._apps_seen, 'at', 10 ** 12)
    monkeypatch.setitem(runner._apps_seen, 'names', ('Calculator', 'Chess', 'TextEdit', 'Weather'))
    assert runner.named_apps('UXTEST: Open Chess and move the white king\'s pawn two squares forward') == ['Chess']
    assert runner.named_apps('Type hello in TextEdit, then use Calculator') == ['Calculator', 'TextEdit']
    assert runner.named_apps("What's the weather today?") == []
    monkeypatch.setitem(runner._apps_seen, 'names', ('Calculator', 'Chess', 'Comet', 'Google Chrome', 'Safari', 'TextEdit'))
    assert runner.named_apps('Open YouTube in Google Chrome and play the navior song') == [], 'a browser is the website route'
    assert runner.named_apps('Use Safari, then open Calculator') == ['Calculator']
    assert runner.named_apps('Search for it in Comet') == []
    sentence = runner.app_sentence(['Chess'])
    assert 'Chess is an app on this Mac' in sentence and 'not a website' in sentence and 'ScreenRequest' in sentence
    assert runner.app_sentence([]) == ''
