"""Bounded searches must not present a partial scan as proof of absence."""

import pytest

from pebble.app import App
from pebble.policy import Workspace


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    chat = app.new_chat(str(root))
    yield app, chat, Workspace(root)
    app.close()


def test_grep_now_finds_suffix_past_the_old_scan_limit(project):
    app, chat, ws = project
    for i in range(501):
        (ws.root / f'{i:04d}.txt').write_text('needle' if i == 500 else 'ordinary')
    result = app.execute('Grep', {'pattern': 'needle'}, ws, chat['id'])
    assert result['untrusted_matches'][0]['path'] == '0500.txt'
    assert result['coverage']['searched'] == 501 and result['coverage']['inventoried'] == 501
    assert result['coverage']['complete'] is True


def test_grep_reports_match_and_unreadable_limits(project):
    app, chat, ws = project
    (ws.root / 'a-binary').write_bytes(b'\xff\xfe')
    (ws.root / 'b-text').write_text('needle\n' * 31)
    result = app.execute('Grep', {'pattern': 'needle'}, ws, chat['id'])
    assert len(result['untrusted_matches']) == 2 and result['cursor']
    assert result['coverage']['skipped'] == 1 and result['coverage']['matches_truncated']
    assert result['coverage']['complete'] is False


def test_complete_eligible_search_reports_scope_exclusions(project):
    app, chat, ws = project
    (ws.root / 'source.py').write_text('needle')
    (ws.root / '.env').write_text('needle SECRET')
    result = app.execute('Grep', {'pattern': 'needle'}, ws, chat['id'])
    assert result['coverage']['complete'] and len(result['untrusted_matches']) == 1
    assert 'private paths omitted' in result['coverage']['scope']
    assert result['untrusted_matches'][0]['path'] == 'source.py'


def test_glob_and_inventory_distinguish_result_limit_from_full_scan(project):
    app, chat, ws = project
    for i in range(101):
        (ws.root / f'{i:03d}.txt').write_text('x')
    inventory = ws.inventory(limit=100)
    assert inventory['truncated'] and len(inventory['files']) == 100
    result = app.execute('Glob', {'pattern': '*.txt'}, ws, chat['id'])
    assert result['coverage']['matches_truncated'] and result['coverage']['complete'] is False
    assert not result['coverage']['inventory_truncated'] and len(result['files']) == 10 and result['cursor']
