"""Phase 46 (1.9.28): the Companion asks before the everyday writes it did not see, flags every refusal, and a run that
ends on a refused write it never redid gets one repair.

Measured first (offline, a throwaway home under sandbox-exec, no grant): sed -i, perl -i, sips, textutil -convert,
gzip, gunzip, bzip2, xz, truncate and xattr -w ran without asking and the sandbox refused them, with "Operation not
permitted", "You don’t have permission" (textutil) or "Cannot to rename temporary file" (sips, exit 13); only the
exit-0 form set sandbox_refused. `sed -i "" ... 2>/dev/null; echo updated` exited 0 saying "updated" with the file
unchanged. rsync --remove-source-files failed even after the person approved, its sources' folder never asked. A
Companion run has no project, so no completion check looked at any of it (golden held-out companion_desktop: 2 false
completions of 15 on Opus).
"""

import base64
import copy
import gzip
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import companion_shell, companion_tools, shell_policy
from pebble.app import App
from pebble.permissions import DEFAULT
from pebble.providers import Providers

MAC = os.path.exists('/usr/bin/sandbox-exec')
macos = pytest.mark.skipif(not MAC, reason='the Shell is a macOS sandbox surface')
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg==')
# line, the folders (or files) it must ask for, the file whose bytes it changes
WRITERS = [
    ('sed -i "" "s/2025/2026/" ~/Documents/notes.txt', {'Documents'}, 'Documents/notes.txt'),
    ('sed -i.bak "s/2025/2026/" ~/Documents/notes.txt', {'Documents'}, 'Documents/notes.txt'),
    ('cd ~/Documents && sed -i "" s/2025/2026/ notes.txt', {'Documents'}, 'Documents/notes.txt'),
    ("perl -i -pe 's/2025/2026/' ~/Documents/notes.txt", {'Documents'}, 'Documents/notes.txt'),
    ('sips -Z 1 ~/Pictures/a.png', {'Pictures/a.png'}, 'Pictures/a.png'),
    ('sips -s format jpeg ~/Pictures/a.png --out ~/Pictures/a.jpg', {'Pictures'}, 'Pictures/a.jpg'),
    ('textutil -convert txt ~/Documents/a.rtf', {'Documents'}, 'Documents/a.txt'),
    ('textutil -convert html ~/Documents/a.rtf -output ~/Documents/a.html', {'Documents'}, 'Documents/a.html'),
    ('gzip ~/Documents/big.log', {'Documents'}, 'Documents/big.log.gz'),
    ('gunzip ~/Documents/old.log.gz', {'Documents'}, 'Documents/old.log'),
    ('bzip2 ~/Documents/b.log', {'Documents'}, 'Documents/b.log.bz2'),
    ('xz ~/Documents/x.log', {'Documents'}, 'Documents/x.log.xz'),
    ('truncate -s 0 ~/Documents/t.log', {'Documents/t.log'}, 'Documents/t.log'),
    ('rsync -a --remove-source-files ~/Downloads/r.pdf ~/Documents/', {'Documents', 'Downloads/r.pdf'},
     'Documents/r.pdf'),
    ('sed -i "" "s/2025/2026/" ~/Documents/notes.txt 2>/dev/null; echo updated', {'Documents'}, 'Documents/notes.txt')]


@pytest.fixture
def home(tmp_path):
    root = Path(os.path.realpath(tmp_path / 'home'))
    for name in ('Documents', 'Downloads', 'Pictures', 'Pebble Workbench'):
        (root / name).mkdir(parents=True)
    (root / 'Pictures' / 'a.png').write_bytes(PNG)
    (root / 'Documents' / 'a.rtf').write_text('{\\rtf1\\ansi hello}')
    (root / 'Documents' / 'notes.txt').write_text('budget 2025\n')
    for name in ('big.log', 'b.log', 'x.log', 't.log'):
        (root / 'Documents' / name).write_text('x\n' * 100)
    (root / 'Documents' / 'old.log.gz').write_bytes(gzip.compress(b'old\n'))
    (root / 'Downloads' / 'r.pdf').write_bytes(b'%PDF r')
    return root


def decide(line, home):
    return shell_policy.decide(line, cwd=str(home), home=str(home), grants={})


def shown(paths, home):
    return {str(Path(p).relative_to(home)) for p in paths}


@pytest.mark.parametrize('line, folders, changed', WRITERS)
def test_each_everyday_writer_asks_for_what_it_writes(home, line, folders, changed):
    decision = decide(line, home)
    assert decision.action == 'ask' and shown(decision.folders, home) == folders, decision.folders


@macos
@pytest.mark.parametrize('line, folders, changed', WRITERS)
def test_once_allowed_each_writer_works_in_the_sandbox(home, line, folders, changed):
    before = (home / changed).read_bytes() if (home / changed).exists() else None
    result = companion_shell.run(line, home=str(home), cwd=str(home), writable=decide(line, home).folders)
    assert result['exit_code'] == 0 and not result.get('sandbox_refused'), result['output'][-300:]
    assert (home / changed).exists() and (home / changed).read_bytes() != before


@pytest.mark.parametrize('line', [
    'sed -n 1,5p ~/Documents/notes.txt', "perl -ne 'print if /2025/' ~/Documents/notes.txt", 'sips -g all ~/Pictures/a.png',
    'textutil -info ~/Documents/a.rtf', 'gzip -l ~/Documents/old.log.gz', 'gunzip -c ~/Documents/old.log.gz | head',
    'xz -t ~/Documents/x.log', 'xattr -l ~/Downloads/r.pdf', 'git -C ~/Documents status 2>/dev/null',
    'ls ~/Documents 2>/dev/null; echo done'])
def test_reads_with_the_same_tools_still_run_without_asking(home, line):
    assert decide(line, home).action == 'allow'


def test_an_in_place_edit_of_a_file_in_the_home_folder_itself_is_refused_with_its_reason(home):
    # 1.9.27 allowed it and the sandbox refused it (the new file beside it lands in the home folder, never given);
    # the first 1.9.28 patch left it so and its soft repair then asked for a redo (sec-w7 minor)
    (home / 'notes.txt').write_text('x\n')
    decision = decide('sed -i "" s/x/y/ ~/notes.txt', home)
    assert decision.action == 'deny' and decision.folders == [] and 'EditFile' in decision.reasons[0]
    assert decide('chmod 600 ~/notes.txt', home).action == 'ask'  # the file itself can be opened: asked as before


def test_xargs_moves_ask_for_the_folder_find_lists(home):
    bench = home / 'Pebble Workbench'
    decision = shell_policy.decide("find ~/Downloads -name 'r*.pdf' -print0 | xargs -0 -I{} mv {} ~/Documents/",
                                   cwd=str(bench), home=str(home), grants={})  # the Shell's own starting folder
    assert decision.action == 'ask' and shown(decision.folders, home) == {'Documents', 'Downloads'}
    assert any(g.startswith('deleting or moving') and g.endswith('/Downloads outside the workbench') for g in decision.gates)


@macos
@pytest.mark.parametrize('line, code', [
    ('sed -i "" s/2025/2026/ ~/Documents/notes.txt', 1),                # Operation not permitted
    ('textutil -convert txt ~/Documents/a.rtf', 1),                      # You don’t have permission
    ('sips -Z 1 ~/Pictures/a.png', 13),                                  # Cannot to rename temporary file
    ("perl -pi -e 's/2025/2026/' ~/Documents/notes.txt", 0)])            # exit 0, nothing changed
def test_every_wording_of_a_refusal_is_flagged_whatever_the_exit_status(home, line, code):
    result = companion_shell.run(line, home=str(home), cwd=str(home))
    assert result['exit_code'] == code and result['sandbox_refused'] is True and 'writable=' in result['hint']


@macos
def test_a_script_without_its_execute_bit_is_no_sandbox_refusal(home):
    (home / 'Pebble Workbench' / 'x.sh').write_text('echo hi\n')
    result = companion_shell.run('cd ~/"Pebble Workbench" && ./x.sh', home=str(home), cwd=str(home))
    assert result['exit_code'] == 126 and 'permission denied' in result['output'] and 'sandbox_refused' not in result


# -- the completion check -----------------------------------------------------------------------------------------

def shell(line, result, state='completed'):
    return {'name': 'Shell', 'state': state, 'arguments': {'command': line}, 'result': result}


def refused(home, path='Documents/notes.txt'):
    return {'exit_code': 1, 'sandbox_refused': True,
            'output': f'sed: {home}/{path}: Operation not permitted\n'}


def test_a_refused_write_nothing_redid_is_one_issue(home):
    tools = [shell('sed -i "" s/a/b/ ~/Documents/notes.txt', refused(home)),
             shell('ls ~/Documents', {'exit_code': 0, 'output': 'notes.txt\n'})]  # looking is not doing
    found = companion_tools.refusal_issues(tools, home=home)
    assert len(found) == 1 and found[0][0].startswith('The sandbox refused a write of this task ("sed -i \\"\\" s/a/b/ '
                                                      '~/Documents/notes.txt", refused at "~/Documents/notes.txt")')


@pytest.mark.parametrize('later', [
    shell('sed -i "" s/a/b/ ~/Documents/notes.txt', {'exit_code': 0, 'output': ''}),
    {'name': 'Shell', 'state': 'completed', 'arguments': {'command': 'cp /tmp/x ~/Documents/notes.txt'},
     'result': {'status': 'applied', 'action_id': 'a1', 'result': {'exit_code': 0, 'output': ''}}},  # approved later
    {'name': 'WriteFile', 'state': 'completed', 'arguments': {'path': '~/Documents/notes.txt', 'content': 'b'},
     'result': {'path': 'HOME/Documents/notes.txt', 'bytes': 1}}])
def test_a_later_write_of_the_same_path_settles_it(home, later):
    if later['name'] == 'WriteFile':
        later = {**later, 'result': {'path': str(home / 'Documents' / 'notes.txt'), 'bytes': 1}}
    tools = [shell('sed -i "" s/a/b/ ~/Documents/notes.txt', refused(home)), later]
    assert companion_tools.refusal_issues(tools, home=home) == []


def test_a_later_write_elsewhere_does_not(home):
    tools = [shell('sed -i "" s/a/b/ ~/Documents/notes.txt', refused(home)),
             shell('cp ~/Documents/notes.txt ~/Downloads/', {'exit_code': 0, 'output': ''})]
    assert len(companion_tools.refusal_issues(tools, home=home)) == 1


def test_no_refusal_no_issue(home):
    assert companion_tools.refusal_issues([shell('ls ~', {'exit_code': 0, 'output': 'x'})], home=home) == []


# -- the run end to end -------------------------------------------------------------------------------------------

def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


@pytest.fixture
def env(tmp_path, home, monkeypatch):
    monkeypatch.setenv('HOME', str(home))
    value = SimpleNamespace(home=home, requests=[], script=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        response = copy.deepcopy(value.script.pop(0) if value.script else final('FINAL: done'))
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)
    value.app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    value.app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'auto'})
    value.app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                                    'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                                    'context': 32768, 'budget': 32768, 'tools': True}])
    value.app.model.select('route')
    yield value
    value.app.active = value.app.active_run = None
    value.app.close()


def run(env, goal, script):
    env.script = list(script)
    chat = env.app.new_chat()
    env.app.work(chat, goal, False, [])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        found = env.app.runs.list(chat['id'])[0]
        if found['state'] not in {'queued', 'running', 'retry_wait'} and env.app.active is None:
            return env.app.runs.get(found['id'])
        time.sleep(.02)
    pytest.fail('The run did not settle.')


def soft_repairs(env):
    return [m['content'] for request in env.requests for m in request[-1:]
            if 'HOST COMPLETION CHECK' in str(m.get('content'))]


@macos
def test_a_companion_run_that_claims_a_refused_write_gets_one_repair(env):
    # a script file the policy cannot read into: allowed, and its write refused by the sandbox
    (env.home / 'Pebble Workbench' / 'stamp.py').write_text(
        "import os\nopen(os.path.expanduser('~/Documents/stamp.txt'), 'w').write('x')\n")
    goal = 'Run ~/Pebble Workbench/stamp.py to write ~/Documents/stamp.txt. End with `FINAL: done`.'
    done = run(env, goal, [call('Shell', {'command': 'python3 ~/"Pebble Workbench"/stamp.py'}, 's1'),
                           final('Wrote it.\nFINAL: done'), final('The sandbox refused it; nothing was written.\n'
                                                                  'FINAL: blocked - no write access to Documents')])
    repairs = soft_repairs(env)
    assert len(repairs) == 1 and 'The sandbox refused a write of this task' in repairs[0], repairs
    assert '~/Documents/stamp.txt' in repairs[0] and done['state'] == 'completed'
    assert not (env.home / 'Documents' / 'stamp.txt').exists()
