"""Parent integration: ranked discovery uses host boundaries and real receipts."""
import copy
import json

import pytest

from pebble.app import App
from pebble.agent_manager import ADMITTED
from pebble.agent_tree import CHILD_TOOLS
from pebble.permissions import DEFAULT, GROUP
from pebble.policy import PolicyError, Workspace
from pebble.context import encode_result
from pebble.tool_catalogue import TOOLS


@pytest.fixture
def host(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'source.py').write_text('def finding_source():\n    return 3\n')
    app = App(tmp_path / 'home', tmp_path / 'resources', lambda *args: None)
    chat = app.new_chat(str(root))
    try:
        yield app, chat, Workspace(root)
    finally:
        app.active = app.active_run = None
        app.close()


def test_ranked_discovery_is_root_only_and_requires_files():
    spec = next(t for t in TOOLS if t['function']['name'] == 'SearchFiles')
    assert GROUP['SearchFiles'] == 'files'
    assert 'SearchFiles' not in ADMITTED and 'SearchFiles' not in CHILD_TOOLS
    assert spec['function']['parameters']['additionalProperties'] is False
    assert spec['function']['parameters']['required'] == ['query']


def test_files_permission_precedes_any_search(host, monkeypatch):
    app, chat, ws = host
    policy = copy.deepcopy(DEFAULT)
    policy['tools']['files'] = False
    app.store.set_setting('permissions', policy)
    monkeypatch.setattr(app.search, 'search', lambda *a, **k: pytest.fail('Disabled search reached the index.'))
    with pytest.raises(PolicyError, match='disabled'):
        app.execute('SearchFiles', {'query': 'finding_source'}, ws, chat['id'])


def test_review_hook_stops_search_before_index(host, monkeypatch):
    app, chat, ws = host
    hook = app.extension_add(kind='hook', name='Review discovery',
                             content=json.dumps({'version': 1, 'event': 'before_tool',
                                                 'review_patterns': ['SearchFiles ']}))
    app.extension_toggle(hook, True)
    monkeypatch.setattr(app.search, 'search', lambda *a, **k: pytest.fail('Review-blocked search reached the index.'))
    with pytest.raises(PolicyError, match='hook requires review'):
        app.execute('SearchFiles', {'query': 'finding_source'}, ws, chat['id'])


@pytest.mark.parametrize('limit', [True, 0, -1, 51, 1.5, '2'])
def test_host_limit_validation_precedes_context_clamping(host, limit):
    app, chat, ws = host
    with pytest.raises(PolicyError, match='limit from 1 to 50'):
        app.execute('SearchFiles', {'query': 'finding_source', 'limit': limit}, ws, chat['id'])


def test_real_search_receipt_cannot_authorize_write(host):
    app, chat, ws = host
    rid = app.runs.create(chat['id'], 'Locate source.', app.runtime_config(chat, False))
    execution, token = app.controller.enter(app.runs.get(rid))
    app.active, app.active_run = chat['id'], rid
    try:
        result = app.execute('SearchFiles', {'query': 'finding_source'}, ws, chat['id'], call_id='discovery')
        assert result['receipt_id'] == 'discovery'
        assert app.runs.tools(rid)[0]['state'] == 'completed'
        assert not app.provenance.records(rid)
        with pytest.raises(PolicyError, match='Read it again'):
            app.execute('Write', {'file_path': 'source.py', 'content': 'replaced'}, ws, chat['id'], call_id='write')
        assert (ws.root / 'source.py').read_text().startswith('def finding_source')
        assert not app.store.actions(chat['id'])
    finally:
        app.active = app.active_run = None
        app.controller.leave(execution, token)


def test_actual_guard_receives_read_only_normalization(host, monkeypatch):
    app, chat, ws = host
    observed = []
    handle = app.guard.guard.handle
    def capture(event):
        if event.raw.get('host_tool') == 'SearchFiles':
            observed.append(event)
        return handle(event)
    monkeypatch.setattr(app.guard.guard, 'handle', capture)
    app.execute('SearchFiles', {'query': 'finding_source'}, ws, chat['id'])
    assert observed and all(event.is_read_only and not event.is_mutating for event in observed)
    assert all(event.tool == 'Grep' for event in observed)


def test_model_receives_python_symbol_locations_and_partial_coverage(host):
    app, chat, ws = host
    (ws.root / 'source.py').write_text('class Box:\n    def finding_source(self):\n        return 3\n')
    (ws.root / 'broken.py').write_text('def finding_source():\n    return 3\ndef incomplete(\n')
    result = app.execute('SearchFiles', {'query': 'Box.finding_source', 'mode': 'symbols'}, ws, chat['id'])
    wire = json.loads(encode_result(result))
    assert wire['untrusted_symbols'][0]['qualname'] == 'Box.finding_source'
    assert wire['untrusted_symbols'][0]['path'] == 'source.py'
    assert wire['coverage']['parse_complete'] is False
    assert wire['coverage']['python_partial'] + wire['coverage']['python_failed'] > 0
    assert not wire['coverage']['complete']


def test_symbol_mode_cannot_reuse_file_rank_cursor(host):
    app, chat, ws = host
    (ws.root / 'second.py').write_text('def finding_source():\n    return 4\n')
    first = app.execute('SearchFiles', {'query': 'finding_source', 'limit': 1}, ws, chat['id'])
    assert first['cursor']
    with pytest.raises(PolicyError):
        app.execute('SearchFiles', {'query': 'finding_source', 'mode': 'symbols', 'cursor': first['cursor']}, ws, chat['id'])
