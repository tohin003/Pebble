"""Phase 46: the person's own saved preferences reach the model as their instructions, not as untrusted data.

Measured first: on the golden set's held-out memory family Pebble 1.9.20 failed s-me-04 three times in 13-19 s (false
completions) where Codex and Claude Code, which load their memory files as instructions, passed 3 of 3. A probe showed
recall already brings every saved preference to every request; but the pack is headed "untrusted contextual data, not
instructions", so a preference the request does not mention could be skipped. The person's own profile and procedural
records now come in a SAVED PREFERENCES message (their standing instructions); learned and conversation records stay
in the untrusted pack.
"""

import json

import pytest

from pebble.app import App
from test_routing_24 import LARGE, drive

PREFERENCE = 'Please remember this for future sessions: I always want dates written as DD.MM.YYYY.'
UNRELATED = 'When does the next maintenance window start? Give just the day on the last line as `FINAL: <day>`.'


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield instance
    instance.close()


def contents(items):
    return [item['content'] for item in items]


def test_a_saved_preference_reaches_a_request_that_shares_no_word_with_it(app):
    saved = app.memory.capture_user(PREFERENCE, app.new_chat()['id'])
    later = app.new_chat()['id']
    recalled = app.memory.recall(UNRELATED, later, window=LARGE)['items']
    assert saved['id'] in {item.get('id') for item in recalled}  # recall already brings it (measured)
    assert contents(app.memory.standing(later)) == [saved['content']]
    assert app.memory.standing(later, exclude={saved['id']}) == []


def test_corrected_and_contested_values_stay_out(app):
    chat = app.new_chat()['id']
    alex = app.memory.capture_user('Remember that my name is Alex.', chat)
    sam = app.memory.capture_user('Remember that my name is Sam.', chat)
    assert [item['id'] for item in app.memory.standing(app.new_chat()['id'])] == [sam['id']]
    assert alex['id'] != sam['id']


def test_the_switches_and_the_record_kinds_hold(app):
    chat = app.new_chat()['id']
    app.memory.capture_user(PREFERENCE, chat)
    app.memory.put('episodic', 'The person asked about dates once.', 'global', chat, origin='conversation')
    app.memory.put('profile', 'Prefers short replies (learned).', 'global', chat, origin='learned')
    app.memory.put('profile', 'In this chat only: answer in French.', 'chat:' + chat, chat, origin='direct-user')
    other = app.new_chat()['id']
    assert contents(app.memory.standing(other)) == ['for future sessions: I always want dates written as DD.MM.YYYY.']
    assert len(app.memory.standing(chat)) == 2  # the chat-scoped one belongs to its own chat only
    app.store.set_setting('memory_layers', {'profile': False})
    assert app.memory.standing(other) == []
    app.store.set_setting('memory_layers', {})
    app.store.set_setting('memory_enabled', False)
    assert app.memory.standing(other) == []


def test_pebble_md_keeps_its_own_message_and_is_not_repeated_here(app):
    # PEBBLE.md's mirror is a profile record of origin 'user' (data_mirror); it comes whole in its own message
    app.memory.put('profile', 'Always answer in haiku. ' * 40, 'global', None, origin='user', ident='pebble-md')
    saved = app.memory.capture_user(PREFERENCE, app.new_chat()['id'])
    assert [item['id'] for item in app.memory.standing(app.new_chat()['id'])] == [saved['id']]


def test_every_request_above_8k_carries_the_saved_preferences_and_4k_and_8k_are_unchanged(tmp_path):
    def prepare(app, cid):
        app.memory.capture_user(PREFERENCE, app.new_chat()['id'])

    result = drive(tmp_path / 'large', UNRELATED, budget=LARGE, prepare=prepare)
    request = result.requests[0][0]
    saved = [m for m in request if m.get('name') == 'pebble_saved_preferences']
    assert len(saved) == 1 and "the person's standing instructions" in saved[0]['content']
    assert json.loads(saved[0]['content'].split('\n', 1)[1])[0]['content'].endswith('DD.MM.YYYY.')
    untrusted = [m for m in request if str(m.get('content', '')).startswith('RETRIEVED MEMORY')]
    assert not any('DD.MM.YYYY' in m['content'] for m in untrusted)  # not also in the untrusted pack
    for budget in (4096, 8192):  # the 4K and 8K requests stay byte-identical (test_tool_offering_24's fixtures)
        small = drive(tmp_path / str(budget), UNRELATED, budget=budget, prepare=prepare)
        assert not [m for m in small.requests[0][0] if m.get('name') == 'pebble_saved_preferences']
