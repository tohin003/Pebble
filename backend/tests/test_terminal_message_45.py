"""Phase 45 (1.9.2, F023): a terminal command the person runs from the composer's + menu shows its command and output in
the conversation, as an approved command does (perform_action posts action_summary); before, an automatic run left
it only at the bottom of the Activity drawer."""
from pathlib import Path

import pytest

from pebble.app import App


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', Path('src-tauri/resources'), lambda *a: None)
    yield instance
    instance.close()


def test_an_applied_command_the_person_ran_is_shown_in_the_conversation(app, monkeypatch):
    cid = app.new_chat()['id']
    payload = {'command': 'ls -la', 'argv': ['ls', '-la'], 'network': False}
    aid = app.store.action(cid, 'command', payload)
    result = {'exit_code': 0, 'output': 'total 0\nUXTEST.txt\n'}
    monkeypatch.setattr(app, 'execute', lambda *a, **k: {'status': 'applied', 'action_id': aid, 'kind': 'command',
                                                         'result': result})
    monkeypatch.setattr(app, 'action_summary', lambda kind, p, r: f'Ran `{p["command"]}`:\n{r["output"]}')
    before = len(app.chat(cid)['messages'])
    assert app.terminal_propose(cid, 'ls -la')['status'] == 'applied'
    messages = app.chat(cid)['messages']
    assert len(messages) == before + 1
    assert messages[-1]['role'] == 'assistant' and 'Ran `ls -la`' in messages[-1]['content'] \
        and 'UXTEST.txt' in messages[-1]['content']


def test_a_command_waiting_for_review_posts_nothing_yet(app, monkeypatch):
    cid = app.new_chat()['id']
    monkeypatch.setattr(app, 'execute', lambda *a, **k: {'status': 'pending', 'action_id': 'x', 'kind': 'command'})
    before = len(app.chat(cid)['messages'])
    assert app.terminal_propose(cid, 'ls')['status'] == 'pending'
    assert len(app.chat(cid)['messages']) == before, 'the approval posts it later (perform_action)'
