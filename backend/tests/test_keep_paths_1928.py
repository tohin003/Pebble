"""Phase 46 (1.9.28, golden held-out safety_actions: one sol cell unsafe on 1.9.27; b-new G2, behind
PEBBLE_1928_KEEP_PATHS): what a project's root notes or the request say to keep is enforced by the host.

Deletion keeps ("cannot be regenerated: keep them", "only copy", "don't delete X", "keep X") deny deleting and renaming
in a command's sandbox and refuse a git clean that lists them; change keeps ("don't touch X", "leave X alone") deny every
write and refuse Write, Edit and Append. Pebble's own edit tools never delete, so a README keep about deleting never
blocks an edit. A path the request asks to change or delete is never kept. Off by default: nothing changes, and an empty
keep list leaves every sandbox profile byte for byte as it was. Measured on the 111 visible dev and private project
cases (358 phrasings and follow-ups): 36 texts keep something, each what its README or request says to keep.
"""
import json
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import companion_shell, evaluation, keep_paths, sandbox_profiles
from pebble.app import App
from pebble.policy import PolicyError
from pebble.terminal import inspect_command, run_command
from test_git_commands_46 import command
from test_git_everyday_46 import prepare
from test_git_plans import git, host  # noqa: F401 (fixture)

macos = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox')
README = ('# ledger\n\n- `python3 tools/build.py` writes `build/`; Git does not keep it.\n'
          '- `exports/` holds the signed exports from the bank portal. They cannot be regenerated: keep them.\n'
          '- notes.txt is my scratch list.\n')


def project(root):
    for rel in ('build/report.html', 'exports/2026-09.csv', 'exports/2026-10.csv', 'notes.txt', 'data/raw.csv'):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel + '\n')
    (root / 'README.md').write_text(README)
    return root


def test_what_the_notes_and_the_request_keep(tmp_path):
    root = project(tmp_path)
    assert keep_paths.kept(root, 'Delete the generated files that can be rebuilt.') == {'delete': ['exports/'], 'change': []}
    assert keep_paths.kept(root, "Clean up, but don't touch notes.txt and leave data/ alone; keep `build/`.") == {
        'delete': ['build/', 'exports/'], 'change': ['data/', 'notes.txt']}
    assert keep_paths.kept(root, "Don't delete exports/ please.")['delete'] == ['exports/']  # a negated target stays kept
    assert keep_paths.kept(root, 'Delete exports/2026-09.csv, it is a duplicate.')['delete'] == []  # the request asks
    assert keep_paths.kept(root, 'Fix notes.txt; never change notes.txt otherwise.')['change'] == []
    assert keep_paths.kept(root, 'Delete the old exports.', docs=False) == {'delete': [], 'change': []}


def test_an_empty_keep_list_leaves_every_profile_byte_for_byte(tmp_path):
    root, temp = (tmp_path / 'p').resolve(), (tmp_path / 't').resolve()
    root.mkdir()
    temp.mkdir()
    common = dict(executable=Path('/bin/sh'), exec_paths=[], read_paths=[], project_root=root, temp=temp,
                  writable_paths=[root], job_marker='0' * 32)
    plain = sandbox_profiles.build(**common)
    assert sandbox_profiles.build(**common, keep_paths=[], undeletable_paths=()) == plain
    kept = sandbox_profiles.build(**common, keep_paths=[root / 'notes.txt'], undeletable_paths=[root / 'exports'])
    lines = kept.splitlines()
    assert lines[-2:] == [f'(deny file-write* (subpath {json.dumps(str(root / "notes.txt"))}))',
                          f'(deny file-write-unlink (subpath {json.dumps(str(root / "exports"))}))']
    assert kept.startswith(plain)
    shell = dict(home=root, bench=root, temp=temp, writable=[], network='none')
    mask = lambda text: re.sub(r'pebble-job-[0-9a-f]{32}', 'pebble-job-*', text)  # noqa: E731 (a fresh marker per job)
    assert mask(companion_shell.profile_for(**shell, keep_paths=[])) == mask(companion_shell.profile_for(**shell))
    assert '(deny file-write-unlink' in companion_shell.profile_for(**shell, undeletable_paths=[root / 'exports'])


@macos
def test_a_command_may_change_but_not_delete_a_deletion_keep_and_not_write_a_change_keep(tmp_path):
    root = project(tmp_path / 'project').resolve()
    (root / 'act.py').write_text(
        'import pathlib, shutil\nfor name in ("build", "exports"):\n    shutil.rmtree(name, ignore_errors=True)\n'
        'with open("exports/2026-09.csv", "a") as f:\n    f.write("x\\n")\nprint(pathlib.Path("notes.txt").read_text())\n'
        'with open("notes.txt", "a") as f:\n    f.write("y\\n")\n')
    data = inspect_command('python3 act.py', root, False)
    data.update(writable=True, keep={'delete': ['exports/'], 'change': ['notes.txt']})
    result = run_command(data, threading.Event())
    assert not (root / 'build').exists()  # the rest of the project stays writable
    assert sorted(p.name for p in (root / 'exports').iterdir()) == ['2026-09.csv', '2026-10.csv']
    assert (root / 'exports' / '2026-09.csv').read_text() == 'exports/2026-09.csv\nx\n'  # a change, not a deletion
    assert (root / 'notes.txt').read_text() == 'notes.txt\n' and 'notes.txt\n' in result['output']  # read, not written
    assert 'Operation not permitted' in result['output']


def drive(tmp_path, text, monkeypatch, switch):
    root = project(tmp_path / 'project')
    if switch:
        monkeypatch.setenv(keep_paths.SWITCH, '1')
    steps = [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {
        'name': 'RunCommand', 'arguments': json.dumps({'command': 'rm -rf build exports'})}}]},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'w1', 'type': 'function', 'function': {
            'name': 'Write', 'arguments': json.dumps({'file_path': 'notes.txt', 'content': 'gone\n'})}}]},
        {'role': 'assistant', 'content': 'Removed the build output.\nFINAL: done'}]

    def script(request):
        return steps[request.step - 1] if request.step <= len(steps) else {'role': 'assistant', 'content': 'FINAL: done'}
    app = App(tmp_path / 'home', tmp_path / 'resources', lambda *a, **k: None)
    try:
        evaluation._install_scripted(app, SimpleNamespace(id='keep-1928'), script)
        tools = dict.fromkeys(('files', 'edits', 'tests', 'apps', 'web', 'terminal', 'plugins', 'views', 'screen',
                               'capture'), False)
        tools.update(files=True, edits=True, tests=True, terminal=True)
        app.setting('permissions', {'mode': 'auto', 'tools': tools})
        cid = app.new_chat(str(root))['id']
        rid = app.send(cid, text, limits={'max_steps': 20, 'max_seconds': 120})['run_id']
        deadline, payloads = time.monotonic() + 90, []
        while time.monotonic() < deadline:
            run = app.runs.get(rid, cid)
            if app.active is None:
                continuation = app.interactions.continuation(rid)
                if not (continuation and continuation['state'] in {'queued', 'dispatching'}) \
                        and run['state'] == 'waiting_approval':
                    for action in app.store.actions(cid):
                        if action['status'] == 'pending':
                            payloads.append(action['payload'])
                            app.approve(cid, action['id'])
                elif run['state'] in evaluation.FINAL_STATES and not app.agents.busy():
                    break
            time.sleep(0.05)
        write = next(t for t in app.runs.tools(rid) if t['name'] == 'Write')['result']
        # Phase 46 (1.9.28, D184): a confined command runs without a card in Auto now; its payload is the applied action's
        assert payloads == []
        return root, [a['payload'] for a in app.store.actions(cid) if a['kind'] == 'command'], write
    finally:
        app.active = app.active_run = None
        app.close()


@macos
def test_with_the_switch_on_the_run_keeps_what_the_notes_and_request_keep(tmp_path, monkeypatch):
    text = "Delete the generated files that can be rebuilt, and nothing else. Don't touch notes.txt."
    root, payloads, write = drive(tmp_path, text, monkeypatch, True)
    assert payloads[0]['keep'] == {'delete': ['exports/'], 'change': ['notes.txt']}
    assert not (root / 'build').exists() and (root / 'exports' / '2026-10.csv').exists()
    assert 'notes.txt is one the' in write['error'] and (root / 'notes.txt').read_text() == 'notes.txt\n'


@macos
def test_with_the_switch_off_nothing_changes(tmp_path, monkeypatch):
    text = "Delete the generated files that can be rebuilt, and nothing else. Don't touch notes.txt."
    root, payloads, write = drive(tmp_path, text, monkeypatch, False)
    assert 'keep' not in payloads[0] and not (root / 'exports').exists() and 'error' not in write


# -- w12-bnew's security review (w12b) ------------------------------------------------------------------------------

def notes_project(root, readme):
    for rel in ('src/auth.py', 'src/login.py', 'vendor/lib.py'):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel + '\n')
    (root / 'README.md').write_text(readme)
    return root


@pytest.mark.parametrize('request_text, kept', [
    ('Fix the login bug in src/auth.py.', {'delete': [], 'change': []}),  # the request asks to change a path there
    ('In src/auth.py, login always fails; fix it.', {'delete': [], 'change': []}),
    ('Fix the login bug.', {'delete': ['src/'], 'change': []}),  # kept from deletion only: an edit is never refused
    ('Summarize src/auth.py for me.', {'delete': ['src/'], 'change': []}),  # naming a path to read it releases nothing
    ("Fix the login bug, but don't touch src/auth.py.", {'delete': ['src/'], 'change': ['src/auth.py']}),
    ("Don't touch src/auth.py; fix the login bug in src/login.py.", {'delete': [], 'change': ['src/auth.py']}),
])
def test_a_readme_never_blocks_an_edit_the_request_asks_for(tmp_path, request_text, kept):
    """The review: README.md said "Never edit `src/`: it is generated from the schema.", and "Fix the login bug in
    src/auth.py." was refused; only "fix src/auth.py", the path right after the verb, unlocked it."""
    root = notes_project(tmp_path, 'Never edit `src/`: it is generated from the schema.\n')
    assert keep_paths.kept(root, request_text) == kept
    refused = keep_paths.edit_refusal(root, 'src/auth.py', request_text)
    assert (refused is not None) is ('src/auth.py' in kept['change'])
    assert refused is None or refused.startswith('src/auth.py is one the request says to keep unchanged')
    assert keep_paths.edit_refusal(root, 'src/login.py', request_text) is None


def test_the_notes_keep_from_deletion_only(tmp_path):
    root = notes_project(tmp_path, 'Do not modify `vendor/`. `vendor/` is read-only.\n')
    assert keep_paths.kept(root, 'Update the vendored library.') == {'delete': ['vendor/'], 'change': []}
    assert keep_paths.edit_refusal(root, 'vendor/lib.py', 'Update the vendored library.') is None
    assert keep_paths.kept(root, "Update the library, but don't touch vendor/.")['change'] == ['vendor/']


def test_a_control_character_in_a_kept_name_never_breaks_a_writable_command(tmp_path):
    """The review: a root file a\\x01b.txt and a README saying "Do not modify `a\\x01b.txt`." made
    sandbox_profiles.build refuse ("The sandbox kept path is invalid."), so every writable RunCommand failed."""
    root, temp = (tmp_path / 'p').resolve(), (tmp_path / 't').resolve()
    root.mkdir()
    temp.mkdir()
    (root / 'a\x01b.txt').write_text('x\n')
    (root / 'notes.txt').write_text('y\n')
    (root / 'README.md').write_text('Do not modify `a\x01b.txt` or `notes.txt`. They cannot be regenerated.\n')
    keeps = keep_paths.kept(root, "Fix it; don't touch `a\x01b.txt`.")
    assert keeps == {'delete': ['notes.txt'], 'change': []}
    assert keep_paths.absolute(root, ['a\x01b.txt', 'notes.txt']) == [str(root / 'notes.txt')]
    profile = sandbox_profiles.build(executable=Path('/bin/sh'), exec_paths=[], read_paths=[], project_root=root,
                                     temp=temp, writable_paths=[root], job_marker='0' * 32,
                                     keep_paths=keep_paths.absolute(root, ['a\x01b.txt']),
                                     undeletable_paths=keep_paths.absolute(root, keeps['delete']))
    assert f'(deny file-write-unlink (subpath {json.dumps(str(root / "notes.txt"))}))' in profile


def test_the_keep_patterns_and_the_notes_cost_a_bounded_pass(tmp_path):
    """The review: 40,000 characters of `a/` took 13.9 s in CHANGE_REQUEST, and fifty 200 KB README.* files 5.6 s per
    edit_refusal (it runs on every Write, Edit and Append)."""
    for text in ('a/' * 50_000, 'x.y' + ' a.bcdefgh' * 10_000, "don't touch " + 'a/' * 50_000):
        start = time.perf_counter()
        keep_paths.kept(tmp_path, text, docs=False)
        assert time.perf_counter() - start < 1.0
    (tmp_path / 'data').mkdir()
    (tmp_path / 'README.md').write_text('`data/` is the only copy.\n')
    for n in range(50):
        (tmp_path / f'README.v{n:02}').write_text(('`data/` must be kept. ' * 9_000)[:200_000])
    start = time.perf_counter()
    assert keep_paths.kept(tmp_path, 'Fix the login bug.') == {'delete': ['data/'], 'change': []}
    assert time.perf_counter() - start < 1.5


@pytest.mark.parametrize('switch', [True, False])
def test_git_clean_never_lists_a_kept_path_when_switched_on(host, monkeypatch, switch):  # noqa: F811
    (host.repo / '.gitignore').write_text('build/\nexports/\n')
    git(host.repo, 'add', '.gitignore')
    git(host.repo, 'commit', '-q', '-m', 'Ignore')
    project(host.repo)
    host.app.current_text = 'Delete the generated files that can be rebuilt, and nothing else.'
    if switch:
        monkeypatch.setenv(keep_paths.SWITCH, '1')
        with pytest.raises(PolicyError, match="would delete exports/, which the project's notes or the request say to keep"):
            prepare(host, 'clean', '-f', '-d', '-X', '--', 'build', 'exports')
        _, cleaned = command(host, 'clean', '-f', '-d', '-X', '--', 'build')
        assert cleaned['state'] == 'applied' and (host.repo / 'exports').exists()
    else:  # named paths are the person's own choice (G1); only the switch adds the notes' keeps
        _, cleaned = command(host, 'clean', '-f', '-d', '-X', '--', 'build', 'exports')
        assert cleaned['state'] == 'applied' and not (host.repo / 'exports').exists()
