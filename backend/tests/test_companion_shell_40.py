"""Phase 40 (1.6.0, D90-D92): companion_shell, real runs under sandbox-exec on a throwaway home folder.

No test reaches the internet: the broker refuses a host that is not approved before connecting anywhere.
"""
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from pebble import companion_shell as cs
from pebble.policy import PolicyError

pytestmark = pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
                                reason='macOS sandbox only')


@pytest.fixture
def home(tmp_path):
    root = Path(os.path.realpath(tmp_path / 'home'))
    (root / '.ssh').mkdir(parents=True)
    (root / '.ssh/id_ed25519').write_text('PRIVATE KEY')
    (root / 'Documents').mkdir()
    (root / 'Documents/a.txt').write_text('hello\n')
    (root / 'Documents/.env').write_text('TOKEN=secret\n')
    (root / 'Library/Messages').mkdir(parents=True)
    (root / 'Library/Messages/chat.db').write_text('messages')
    return root


def run(line, home, **kw):
    return cs.run(line, home=home, **kw)


def test_pipes_and_the_workbench(home):
    r = run('echo one two three | wc -w && pwd && echo x > made.txt && ls', home)
    assert r['exit_code'] == 0 and r['outcome'] == 'exited'
    assert [line.strip() for line in r['output'].splitlines()] == ['3', str(home / 'Pebble Workbench'), 'made.txt']
    assert (home / 'Pebble Workbench/made.txt').read_text() == 'x\n'
    assert r['untrusted_output'] is True and len(r['output_sha256']) == 64


def test_ordinary_home_files_are_readable(home):
    assert run('cat ~/Documents/a.txt', home)['output'] == 'hello\n'


@pytest.mark.parametrize('target', ['~/.ssh/id_ed25519', '~/Documents/.env', '~/Library/Messages/chat.db'])
def test_protected_places_are_refused_by_the_sandbox_itself(target, home):
    r = run(f'cat {target}', home)
    assert r['exit_code'] != 0 and 'Operation not permitted' in r['output']
    assert 'secret' not in r['output'] and 'PRIVATE' not in r['output'] and 'messages' not in r['output']


def test_writes_outside_the_workbench_fail_until_opened(home):
    r = run('echo b > ~/Documents/b.txt', home)
    assert r['exit_code'] != 0 and not (home / 'Documents/b.txt').exists()
    assert 'writable=' in r.get('hint', '')
    r = run('echo b > ~/Documents/b.txt', home, writable=[str(home / 'Documents')])
    assert r['exit_code'] == 0 and (home / 'Documents/b.txt').read_text() == 'b\n'


def test_an_opened_folder_still_keeps_its_secrets(home):
    r = run('cat ~/Documents/.env; echo k > ~/Documents/x.pem', home, writable=[str(home / 'Documents')])
    assert 'secret' not in r['output'] and not (home / 'Documents/x.pem').exists()


def test_no_network_without_approved_hosts(home):
    r = run('curl -sS -m 5 -o /dev/null https://example.com; echo rc=$?', home)
    assert 'rc=0' not in r['output']


def test_the_broker_refuses_a_host_that_was_not_approved(home):
    r = run('curl -sS -m 10 -o /dev/null https://not-approved.example.org; echo rc=$?', home, hosts=['example.com'])
    assert 'rc=0' not in r['output']
    assert any(rec['host'] == 'not-approved.example.org' and not rec['allowed'] for rec in r['network_refused'])


def test_the_environment_has_no_secrets(home, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test-should-not-leak')
    monkeypatch.setenv('PEBBLE_HOME', '/somewhere')
    r = run('env', home)
    assert 'sk-test-should-not-leak' not in r['output'] and 'PEBBLE_HOME' not in r['output']
    assert 'HOME=' + str(home) in r['output']


def test_a_timeout_stops_the_whole_process_group(home):
    r = run('sleep 30 & sleep 30; echo never', home, timeout=1)
    assert r['outcome'] == 'timed_out' and r['exit_code'] is None and 'never' not in r['output']


def test_cancel_stops_it(home):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    r = run('sleep 20', home, cancel=cancel)
    assert r['outcome'] == 'cancelled'


def test_a_snapshot_undoes_a_change_outside_the_workbench(home, tmp_path):
    store = tmp_path / 'snapshots'
    store.mkdir()
    docs = str(home / 'Documents')
    r = run('rm ~/Documents/a.txt; echo n > ~/Documents/new.txt', home, writable=[docs], snapshot_store=store,
            snapshot_paths=[docs])
    assert r['snapshot']['undoable'] and not (home / 'Documents/a.txt').exists()
    record = json.loads((store / r['snapshot']['id'] / 'snapshot.json').read_text())
    assert cs.restore(record)['failed'] == []
    assert (home / 'Documents/a.txt').read_text() == 'hello\n' and not (home / 'Documents/new.txt').exists()


def test_a_path_that_did_not_exist_is_removed_on_undo(home, tmp_path):
    store = tmp_path / 's'
    store.mkdir()
    target = str(home / 'Documents/fresh.txt')
    r = run('echo f > ~/Documents/fresh.txt', home, writable=[str(home / 'Documents')], snapshot_store=store,
            snapshot_paths=[target])
    record = json.loads((store / r['snapshot']['id'] / 'snapshot.json').read_text())
    cs.restore(record)
    assert not Path(target).exists()


def test_bad_inputs_are_refused(home):
    with pytest.raises(PolicyError):
        run('ls', home, timeout=0)
    with pytest.raises(PolicyError):
        run('ls', home, cwd=str(home / '.ssh'))
    with pytest.raises(PolicyError):
        run('ls', home, writable=[str(home)])
