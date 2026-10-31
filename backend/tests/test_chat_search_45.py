"""Phase 45 (1.9.5, F031): conversation search finds what was said, not only titles; Recently deleted stays out; the
route is the main window's only (main.rs ALLOWED, never the pet; tool_authz.NATIVE_ONLY; never a model tool)."""
import re
from pathlib import Path

import pytest

from pebble import tool_authz
from pebble.app import App
from pebble.tool_catalogue import TOOLS

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield a
    a.close()


def said(app, words, role='user'):
    chat = app.new_chat()
    app.store.query('INSERT INTO messages VALUES(?,?,?,?,?)', (words[:8] + role, chat['id'], role, words, 1.0))
    return chat['id']


def test_words_find_their_conversation_with_an_excerpt(app):
    hello = said(app, 'Hello Pebble, what can you do for my 100% complete_test plan?')
    other = said(app, 'Something else entirely', 'assistant')
    found = app.chat_search('hello pebble')
    assert [m['id'] for m in found['matches']] == [hello]
    assert 'Hello Pebble' in found['matches'][0]['excerpt']
    assert [m['id'] for m in app.chat_search('ELSE ENTIRE')['matches']] == [other]
    # LIKE's own wildcards are literal words here
    assert [m['id'] for m in app.chat_search('100%')['matches']] == [hello]
    assert [m['id'] for m in app.chat_search('complete_test')['matches']] == [hello]
    assert app.chat_search('100x')['matches'] == [] and app.chat_search('a')['matches'] == []


def test_recently_deleted_conversations_stay_out(app):
    gone = said(app, 'the secret word is pumpernickel')
    assert app.chat_search('pumpernickel')['matches']
    app.delete_chat(gone)  # to Recently deleted
    assert app.chat_search('pumpernickel')['matches'] == []


def test_the_route_is_the_main_windows_only():
    shell = (ROOT / 'src-tauri/src/main.rs').read_text()
    allowed = re.search(r'const ALLOWED: &\[&str\] = &\[(.*?)\];', shell).group(1)
    pet = re.search(r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains', shell).group(1)
    assert '"chat_search"' in allowed and '"chat_search"' not in pet
    assert 'chat_search' in tool_authz.NATIVE_ONLY
    assert 'chat_search' not in {t['function']['name'] for t in TOOLS}
