"""Small contexts page exact source instead of overflowing the latest exchange."""

import hashlib

import pytest

from pebble.app import App
from pebble.policy import Workspace, digest
from pebble.context import focused_excerpt


def test_token_prefix_never_breaks_unicode_or_changes_content():
    text = ('漢字🙂é\r\n' * 1000)
    excerpt = focused_excerpt(text, 96000, token_limit=320)
    assert text.startswith(excerpt) and '\ufffd' not in excerpt
    assert len(excerpt) < len(text) and excerpt.encode().decode() == excerpt


def test_read_pages_reconstruct_exact_unicode_crlf_selected_span(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    raw = ''.join(f'漢字-{i}: {hashlib.sha256(str(i).encode()).hexdigest()}\r\n' for i in range(40))
    (root / 'a.txt').write_bytes(raw.encode())
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        chat = app.new_chat(str(root))
        ws, offset, pages = Workspace(root), 0, []
        for _ in range(100):
            result = app.execute('Read', {'file_path': 'a.txt', 'offset': 1, 'limit': 40, 'char_offset': offset,
                **({'expected_hash': digest(raw.encode())} if offset else {})}, ws, chat['id'])
            pages.append(result['untrusted_source'])
            assert result['file_hash'] == digest(raw.encode())
            if result['next_char_offset'] is None:
                assert result['excerpt_complete']
                break
            assert result['next_char_offset'] > offset and not result['excerpt_complete']
            offset = result['next_char_offset']
        else:
            pytest.fail('Character paging made no bounded progress')
        assert len(pages) > 1 and ''.join(pages) == raw
    finally:
        app.close()


def test_character_continuation_is_pinned_to_the_first_view(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'a.txt').write_text('漢字' * 3000)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        chat, ws = app.new_chat(str(root)), Workspace(root)
        first = app.execute('Read', {'file_path': 'a.txt'}, ws, chat['id'])
        args = {'file_path': 'a.txt', 'char_offset': first['next_char_offset']}
        with pytest.raises(ValueError, match='expected_hash'):
            app.execute('Read', args, ws, chat['id'])
        (root / 'a.txt').write_text('different source')
        with pytest.raises(ValueError, match='changed between pages'):
            app.execute('Read', {**args, 'expected_hash': first['file_hash']}, ws, chat['id'])
    finally:
        app.close()


@pytest.mark.parametrize('args', [{'offset': 0}, {'char_offset': True}, {'limit': 0}])
def test_invalid_read_does_not_create_fresh_witness(tmp_path, args):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'a.txt').write_text('old')
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        chat = app.new_chat(str(root))
        rid = app.runs.create(chat['id'], 'Read source', app.runtime_config(chat, False))
        execution, token = app.controller.enter(app.runs.get(rid))
        app.active, app.active_run = chat['id'], rid
        try:
            with pytest.raises(ValueError):
                app.execute('Read', {'file_path': 'a.txt', **args}, Workspace(root), chat['id'], call_id='invalid')
            assert not app.provenance.records(rid)
        finally:
            app.active = app.active_run = None
            app.controller.leave(execution, token)
    finally:
        app.close()
