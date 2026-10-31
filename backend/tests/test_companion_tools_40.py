"""Phase 40 (1.6.0, D90-D93): companion_tools, the Companion's Shell, files, Screenshot and FindTool handlers."""
import base64
import io
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from pebble import companion_tools as ct
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError


class Store:
    def __init__(self):
        self.values = {}

    def setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = json.loads(json.dumps(value))


class Runs:
    def __init__(self):
        self.notes = []

    def note(self, run_id, kind, data):
        self.notes.append((run_id, kind, data))


class FakeApp:
    def __init__(self, data):
        self.store, self.runs, self.home = Store(), Runs(), data
        self.proposed = []
        self.task_policy = json.loads(json.dumps(DEFAULT))
        self.companion = None

    def propose(self, cid, kind, payload):
        self.proposed.append((kind, payload))
        return {'status': 'awaiting_user_approval', 'action_id': 'a1', 'kind': kind}


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = Path(os.path.realpath(tmp_path / 'home'))
    (root / '.ssh').mkdir(parents=True)
    (root / '.ssh/id_ed25519').write_text('key')
    (root / 'Documents').mkdir()
    (root / 'Documents/notes.txt').write_text('\n'.join(f'line {i}' for i in range(1000)))
    (root / '.zshrc').write_text('export X=1\n')
    monkeypatch.setenv('HOME', str(root))
    return root


@pytest.fixture
def app(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    return FakeApp(data)


# -- grants ------------------------------------------------------------------------------------------------------

def test_grants_this_session_and_always(app, home):
    ct.add_grant(app, 'hosts', 'PyPI.org', 'session')
    ct.add_grant(app, 'folders', '~/Documents', 'always')
    merged = ct.grants(app)
    assert merged['hosts'] == {'pypi.org': 'session'}
    assert merged['folders'] == {str(home / 'Documents'): 'always'}
    assert app.store.values['shell_grants']['folders'] == [str(home / 'Documents')]  # only "always" is saved
    ct.remove_grant(app, 'folders', str(home / 'Documents'))
    assert ct.grants(app)['folders'] == {}


@pytest.mark.parametrize('kind, value, scope', [('folders', '~/.zshrc', 'always'), ('folders', '~/.ssh', 'session'),
                                                ('folders', '~', 'always'), ('hosts', '10.0.0.1', 'always'),
                                                ('people', 'x', 'always'), ('hosts', 'a.com', 'forever')])
def test_gated_protected_and_bad_grants_are_refused(app, home, kind, value, scope):
    with pytest.raises(PolicyError):
        ct.add_grant(app, kind, value, scope)


# -- Shell -------------------------------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox only')
def test_an_allowed_line_runs_and_records_its_untrusted_output(app, home):
    result = ct.shell(app, {'command': 'echo visit https://collect.evil-example.com/x'}, 'c1', 'r1', threading.Event())
    assert result['exit_code'] == 0 and app.proposed == []
    assert 'https://collect.evil-example.com/x' in ct.state(app).fragments_for('r1')


def test_an_ask_becomes_a_shell_action_for_the_person(app, home):
    result = ct.shell(app, {'command': 'curl https://pypi.org/simple/', 'hosts': '["pypi.org"]'}, 'c1', 'r1', None)
    assert result['status'] == 'awaiting_user_approval'
    kind, payload = app.proposed[0]
    assert kind == 'shell' and payload['requires_review'] and payload['hosts'] == ['pypi.org']
    assert payload['always_offered'] is True


def test_a_delete_outside_the_workbench_asks_with_its_snapshot_paths(app, home):
    ct.add_grant(app, 'folders', '~/Documents', 'always')
    ct.shell(app, {'command': 'rm ~/Documents/notes.txt'}, 'c1', 'r1', None)
    kind, payload = app.proposed[0]
    assert kind == 'shell' and not payload['always_offered']
    assert payload['snapshot_paths'] == [str(home / 'Documents/notes.txt')]


def test_a_refusal_counts_toward_hand_back(app, home):
    for n in range(3):
        result = ct.shell(app, {'command': 'sudo ls'}, 'c1', 'r1', None)
        assert result['status'] == 'refused'
    assert result['hand_back'] is True and 'Stop and ask the person' in result['next']


def test_a_hook_review_turns_an_allowed_line_into_an_ask(app, home):
    ct.shell(app, {'command': 'ls'}, 'c1', 'r1', None, force_review=True)
    assert app.proposed[0][0] == 'shell'


def test_lists_accept_json_or_commas():
    assert ct._list('["a", "b"]') == ['a', 'b'] and ct._list('a, b') == ['a', 'b'] and ct._list(None) == []
    with pytest.raises(PolicyError):
        ct._list('[1, 2]')


# -- files -------------------------------------------------------------------------------------------------------

def test_read_file_pages_text_and_marks_it_untrusted(app, home):
    first = ct.read_file(app, {'path': '~/Documents/notes.txt', 'limit': 10}, 'r1')
    assert first['untrusted_text'].splitlines() == [f'line {i}' for i in range(10)] and first['next_offset'] == 10
    assert first['total_lines'] == 1000
    second = ct.read_file(app, {'path': str(home / 'Documents/notes.txt'), 'offset': 995}, 'r1')
    assert second['untrusted_text'].splitlines()[-1] == 'line 999' and 'next_offset' not in second


@pytest.mark.parametrize('path', ['~/.ssh/id_ed25519', 'Documents/notes.txt', '~/Documents/missing.txt'])
def test_read_file_refuses_protected_relative_and_missing_paths(app, home, path):
    with pytest.raises(PolicyError):
        ct.read_file(app, {'path': path}, 'r1')


def test_writes_in_the_workbench_apply_with_an_undo_copy(app, home):
    target = home / 'Pebble Workbench/report.md'
    result = ct.write_file(app, {'path': str(target), 'content': '# Report\n'}, 'c1', 'r1')
    assert target.read_text() == '# Report\n' and result['undoable'] and app.proposed == []
    ct.write_file(app, {'path': str(target), 'old_string': 'Report', 'new_string': 'Summary'}, 'c1', 'r1', edit=True)
    assert target.read_text() == '# Summary\n'
    assert ct.undo(app, ct_snapshot_id(app, 2))['failed'] == []
    assert target.read_text() == '# Report\n'


def ct_snapshot_id(app, n):
    return [d for (_, kind, d) in app.runs.notes if kind == 'file_snapshot'][n - 1]['id']


def test_writes_outside_the_workbench_ask_and_gated_paths_never_offer_always(app, home):
    ct.write_file(app, {'path': '~/Documents/new.txt', 'content': 'x'}, 'c1', 'r1')
    ct.write_file(app, {'path': '~/.zshrc', 'content': 'x', 'append': True}, 'c1', 'r1')
    (k1, p1), (k2, p2) = app.proposed
    assert k1 == k2 == 'file' and p1['always_offered'] is True and p2['always_offered'] is False
    assert not (home / 'Documents/new.txt').exists() and (home / '.zshrc').read_text() == 'export X=1\n'


def test_an_edit_needs_one_exact_match(app, home):
    target = home / 'Pebble Workbench/x.txt'
    target.parent.mkdir(exist_ok=True)
    target.write_text('a a')
    with pytest.raises(PolicyError):
        ct.write_file(app, {'path': str(target), 'old_string': 'a', 'new_string': 'b'}, 'c1', 'r1', edit=True)


# -- Screenshot --------------------------------------------------------------------------------------------------

def _png():
    from PIL import Image
    data = io.BytesIO()
    Image.new('RGB', (40, 30), (10, 120, 200)).save(data, format='PNG')
    return data.getvalue()


def test_a_screenshot_reaches_the_model_once_as_labelled_untrusted_pixels(app, home):
    def capture(argv):
        assert argv[:4] == ['/usr/sbin/screencapture', '-x', '-t', 'png'] and argv[4:6] == ['-R', '0,0,40,30']
        Path(argv[-1]).write_bytes(_png())
    result = ct.screenshot(app, {'region': '0,0,40,30'}, 'r1', capture=capture, allowed=True)
    assert result['status'] == 'captured' and result['width'] == 40 and result['preview'].startswith('data:image/jpeg')
    parts = ct.take_image(app, 'r1')
    assert parts[0]['type'] == 'text' and 'UNTRUSTED screenshot' in parts[0]['text']
    assert parts[1]['image_url']['url'].startswith('data:image/png;base64,')
    assert ct.take_image(app, 'r1') is None  # once


def test_without_screen_recording_permission_pebble_says_how_to_grant_it(app, home, monkeypatch):
    monkeypatch.setattr(ct, '_request_capture', lambda: False)
    result = ct.screenshot(app, {}, 'r1', capture=lambda argv: pytest.fail('no capture'), allowed=False)
    assert result['status'] == 'needs_permission' and 'Screen & System Audio Recording' in result['error']


@pytest.mark.parametrize('region', ['1,2,3', 'a,b,c,d', '0,0,0,10', '-1,0,5,5'])
def test_a_bad_region_is_refused(app, home, region):
    with pytest.raises(PolicyError):
        ct.screenshot(app, {'region': region}, 'r1', capture=lambda argv: None, allowed=True)


# -- FindTool ----------------------------------------------------------------------------------------------------

def test_find_tool_offers_only_what_this_run_may_use(app, home):
    found = ct.find_tool(app, {'query': 'git history of a commit'}, 'r1', ws=None)
    assert all(f['name'] not in ('ReadGitHistory', 'ReadGitDiff') for f in found['found'])  # project tools, no project
    found = ct.find_tool(app, {'query': 'weather forecast city'}, 'r1', ws=None)
    assert found['found'][0]['name'] == 'Weather' and 'Weather' in app.found_tools['r1']
    app.task_policy['tools']['web'] = False
    assert all(f['name'] != 'Weather' for f in ct.find_tool(app, {'query': 'weather'}, 'r2', ws=None)['found'])


def test_find_tool_never_hands_over_core_or_native_routes(app, home):
    names = {f['name'] for f in ct.find_tool(app, {'query': 'approve reject setting question plan'}, 'r1', ws=None,
                                             )['found']}
    assert not names & {'approve', 'reject', 'setting', 'AskUserQuestion', 'UpdatePlan'}


# -- undo --------------------------------------------------------------------------------------------------------

def test_undo_refuses_unknown_or_tampered_snapshots(app, home):
    with pytest.raises(PolicyError):
        ct.undo(app, '../../etc')
    with pytest.raises(PolicyError):
        ct.undo(app, '0' * 32)
    folder = Path(app.home) / 'snapshots' / ('a' * 32)
    folder.mkdir(parents=True)
    (folder / 'snapshot.json').write_text(json.dumps({'entries': [{'path': str(home / '.ssh'), 'undoable': True,
                                                                    'existed': True, 'clone': str(folder / '0')}]}))
    with pytest.raises(PolicyError):
        ct.undo(app, 'a' * 32)
