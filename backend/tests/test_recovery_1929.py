"""Phase 46 (1.9.29, w21-recovery): recovery's honest stops (golden held-out: 6 of 15 on both models, the same cells, in
1.9.26, 1.9.27 and 1.9.28; D212: each run made about one command, which failed printing a lock, busy resource or port
with "operation not permitted" or "permission denied", or a traceback about a version or configuration mismatch, and
the run then wrote nothing and stopped; Claude Code and Codex fix the cause and run again).

- terminal._mode (PEBBLE_1929_MODES, on by default): a reviewed chmod also sets the read and write bits of one project
  file or folder (u+w, 755; -R for a folder). Group and world write, setuid, setgid and sticky bits, ACLs, links,
  several paths, the project folder, Git metadata and private files stay refused; D178's exec-bit form is unchanged.
- terminal.cause_note, attached by App to a failed RunCommand's result: which cause of its permission error the host
  can see (the path's own permissions, a path outside the project, Git metadata, a signal, a socket) and whether the
  process a lock or PID file names still runs (checked outside the sandbox, where ps and kill -0 can see it).
- confined.CODE_NOTE: a card-free command refused a write in a code folder is to be run again now, not handed back.
- requirements._kinds (PINS): a configuration or version-pin file is never "the data" or "the inputs" a request keeps.

The model is scripted; commands run in the real macOS sandbox where a test says so.
"""

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import pebble.app as app_module
from pebble import confined, requirements, terminal
from pebble.policy import PolicyError
from pebble.terminal import cause_note, inspect_command, run_command
from test_guard_terminal_outcomes_24 import call, env, final, settled  # noqa: F401 (fixture)
from test_recovery_46 import contents, large

SANDBOX = sys.platform == 'darwin' and os.path.exists('/usr/bin/sandbox-exec')
GONE = 4194303  # above macOS's highest process id: never a running process


@pytest.fixture
def project(tmp_path):
    root = (tmp_path / 'project').resolve()
    for rel, text in (('run/export.lock', '48213 2026-10-28T02:14:09+00:00\n'), ('out/report.csv', 'a,b\n'),
                      ('bin/tool', '#!/usr/bin/env python3\nprint(1)\n'), ('.env', 'K=1\n'), ('.git/HEAD', 'ref: x\n'),
                      ('scripts/export.py', 'print(1)\n')):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / 'link').symlink_to(root / 'out' / 'report.csv')
    yield root
    for path in (root / 'run', root / 'out' / 'report.csv'):  # pytest's cleanup needs them writable again
        os.chmod(path, 0o755 if path.is_dir() else 0o644)


@pytest.fixture
def modes(monkeypatch):
    monkeypatch.delenv('PEBBLE_1929_MODES', raising=False)
    monkeypatch.delenv('PEBBLE_1928_TERMINAL', raising=False)


# -- the reviewed chmod --------------------------------------------------------------------------------------------------

@pytest.mark.usefixtures('modes')
@pytest.mark.parametrize('command', ['chmod u+w run', 'chmod u+w out/report.csv', 'chmod 755 run', 'chmod 0644 out/report.csv',
                                     'chmod -R u+w run', 'chmod +w out/report.csv', 'chmod a-w out/report.csv',
                                     'chmod u=rw,go=r out/report.csv', 'chmod u+rwX run', 'chmod 444 out/report.csv'])
def test_chmod_sets_the_read_and_write_bits_of_one_project_file_or_folder_under_review(project, command):
    checked = inspect_command(command, project)
    assert checked['requires_review'] and checked['risk'] == 'dangerous' and 'env' not in checked
    assert checked['effect'].startswith('Sets the permissions of ') and 'no other account gains write' in checked['effect']
    assert checked['argv'] == command.split()


@pytest.mark.usefixtures('modes')
@pytest.mark.parametrize('command', [
    'chmod 777 run', 'chmod g+w run', 'chmod o+w out/report.csv', 'chmod a+w run', 'chmod ugo+rw run', 'chmod 666 out/report.csv',
    'chmod 775 run', 'chmod 4755 bin/tool', 'chmod u+s bin/tool', 'chmod +t run', 'chmod u+w run out', 'chmod u+w .',
    'chmod u+w ..', 'chmod u+w ../x', 'chmod -R u+w out/report.csv', 'chmod u+w missing', 'chmod u+w link',
    'chmod u+w .env', 'chmod u+w .git', 'chmod -h u+w run', 'chmod u+w /etc/hosts', 'chmod -x bin/tool',
    'chmod -R +x run', 'chmod +x run', 'chmod u+w', 'chmod "+a" run'])
def test_chmod_keeps_refusing_group_and_world_write_special_bits_links_and_everything_outside(project, command):
    with pytest.raises(PolicyError):
        inspect_command(command, project)


@pytest.mark.usefixtures('modes')
def test_the_exec_bit_form_is_d178s_and_the_switch_restores_1928(project, monkeypatch):
    assert inspect_command('chmod +x bin/tool', project)['effect'].startswith('Adds the exec bit to bin/tool')
    with pytest.raises(PolicyError, match='group or world write'):
        inspect_command('chmod g+w run', project)  # the refusal names what works (MODE_HINT)
    monkeypatch.setenv('PEBBLE_1929_MODES', '0')
    with pytest.raises(PolicyError, match='Only chmod \\+x'):
        inspect_command('chmod u+w run', project)
    assert inspect_command('chmod +x bin/tool', project)['effect']


@pytest.mark.skipif(not SANDBOX, reason='actual macOS confinement')
@pytest.mark.usefixtures('modes')
def test_a_reviewed_chmod_gives_a_read_only_folder_back_its_write_bit_in_the_sandbox(project):
    os.chmod(project / 'run', 0o555)
    removal = {**inspect_command('rm run/export.lock', project), 'writable': True}
    assert run_command(removal, threading.Event())['exit_code'] != 0  # the folder's own mode refuses it
    assert run_command({**inspect_command('chmod u+w run', project), 'writable': True}, threading.Event())['exit_code'] == 0
    assert os.stat(project / 'run').st_mode & stat.S_IWUSR and not os.stat(project / 'run').st_mode & 0o022
    assert run_command(removal, threading.Event())['exit_code'] == 0 and not (project / 'run' / 'export.lock').exists()


# -- the sandbox: a project's own Python processes, and Apple's /usr/bin tools ---------------------------------------------

SEMAPHORES = """import multiprocessing, _multiprocessing
from concurrent.futures import ProcessPoolExecutor
if __name__ == '__main__':
    multiprocessing.Lock(); multiprocessing.Queue()
    with multiprocessing.get_context('spawn').Pool(2) as pool:
        print('pool', pool.map(abs, [-1, -2]))
    with ProcessPoolExecutor(2) as executor:
        print('executor', list(executor.map(abs, [-3, -4])))
    try:
        _multiprocessing.SemLock(1, 1, 1, '/zz-w21-other', True)  # a name Python's multiprocessing never makes
        print('other name ALLOWED')
    except PermissionError:
        print('other name refused')
"""


@pytest.mark.skipif(not SANDBOX, reason='actual macOS confinement')
@pytest.mark.parametrize('card_free', [True, False], ids=['card-free', 'approved'])
def test_a_projects_multiprocessing_locks_pools_and_executors_run_in_the_sandbox(project, card_free):
    (project / 'par.py').write_text(SEMAPHORES)
    payload = {**inspect_command('python3 par.py', project), 'writable': True}
    if card_free:
        payload['confined'] = {'copy': None}  # the containment anchor, as Auto runs it without a card
    result = run_command(payload, threading.Event())
    assert result['exit_code'] == 0, result['output']  # 1.9.28: PermissionError [Errno 1] from SemLock, on both paths
    assert 'pool [1, 2]' in result['output'] and 'executor [3, 4]' in result['output']
    assert 'other name refused' in result['output']  # only the /mp- names Python's multiprocessing makes


def test_the_profile_allows_only_pythons_semaphores_and_still_no_network(tmp_path):
    from pebble import sandbox_profiles
    temp, root = (tmp_path / 'temp').resolve(), (tmp_path / 'project').resolve()
    temp.mkdir()
    root.mkdir()
    text = sandbox_profiles.build(executable=Path('/usr/bin/true'), exec_paths=[], read_paths=[], project_root=root,
                                  temp=temp, job_marker='0' * 32)
    assert sandbox_profiles.PYTHON_SEMAPHORES in text and text.count('(allow ipc-posix') == 1
    assert 'allow network' not in text and 'ipc-posix-shm' not in text


@pytest.mark.skipif(not SANDBOX or not Path('/Library/Developer/CommandLineTools').is_dir(), reason='macOS with CLT')
@pytest.mark.parametrize('command', ['make --version', 'cc --version',  # a project script that asks git (raw git is refused)
                                     'python3 -c "import subprocess; subprocess.run([\'/usr/bin/git\', \'--version\'], check=True)"'])
def test_apples_usr_bin_tools_run_whichever_developer_folder_is_selected(project, command):
    # 1.9.28 on this Mac (Xcode selected since 2026-10-29): "xcrun: error: unable to load libxcrun"
    assert terminal.developer_environment() == {'DEVELOPER_DIR': '/Library/Developer/CommandLineTools'}
    result = run_command({**inspect_command(command, project), 'writable': True}, threading.Event())
    assert result['exit_code'] == 0 and 'xcrun' not in result['output'], result['output']


# -- the cause of a failed command ---------------------------------------------------------------------------------------

def test_a_lock_in_a_read_only_folder_names_the_folders_own_permissions_and_the_reviewed_fix(project):
    os.chmod(project / 'run', 0o555)
    output = ('export: run/export.lock is stale (pid %d, started 2026-10-28T02:14:09+00:00) but could not be cleared: '
              "[Errno 13] Permission denied: '%s'\n" % (GONE, project / 'run' / 'export.lock'))
    note = cause_note(output, project)
    assert 'run is read-only by its own permissions (dr-xr-xr-x)' in note and '`chmod u+w run`' in note
    assert 'not Pebble\'s sandbox' in note and 'files in it can be added and removed again' in note
    assert terminal.STALE_PID.format(pid=GONE) in note  # the host saw that the process the lock names is gone


def test_a_read_only_file_names_itself_and_a_writable_one_names_nothing(project):
    os.chmod(project / 'out' / 'report.csv', 0o444)
    note = cause_note("PermissionError: [Errno 13] Permission denied: 'out/report.csv'\n", project)
    assert 'out/report.csv is read-only by its own permissions (-r--r--r--)' in note and 'removed again' not in note
    os.chmod(project / 'out' / 'report.csv', 0o644)
    assert cause_note("PermissionError: [Errno 13] Permission denied: 'out/report.csv'\n", project) is None


HOOK_RAN = ('Traceback (most recent call last):\n  File "scripts/build_site.py", line 35, in main\n'
            "    subprocess.run([os.path.join('hooks', 'post_build')], check=True)\n"
            '  File "/Library/Frameworks/Python.framework/Versions/3.14/lib/python3.14/subprocess.py", line 1990, in '
            '_execute_child\n    raise child_exception_type(errno_num, err_msg, err_filename)\n'
            "PermissionError: [Errno 13] Permission denied: 'hooks/post_build'\n")


@pytest.mark.parametrize('output', [HOOK_RAN, 'sh: hooks/post_build: Permission denied\n',
                                    '/bin/sh: hooks/post_build: Permission denied\n',
                                    'make: execvp: hooks/post_build: Permission denied\n'])
def test_a_file_that_cannot_run_names_its_exec_bit_and_the_reviewed_fix(project, output):
    # the lead, 1.9.29 (Opus dev d-rc-232 phrasing 2 stopped here, reading `chmod +x` on a kept hook as an edit of it)
    hook = project / 'hooks' / 'post_build'
    hook.parent.mkdir(exist_ok=True)
    hook.write_text('#!/bin/sh\nls site > site/manifest.txt\n')
    os.chmod(hook, 0o644)
    note = cause_note(output, project)
    assert note == terminal.EXEC_CAUSE.format(path='hooks/post_build', mode='-rw-r--r--')
    assert '`chmod +x hooks/post_build` (reviewed)' in note and 'not its contents' in note
    os.chmod(hook, 0o755)  # it may run, so the error is something else: nothing is said
    assert cause_note(output, project) is None


def test_a_file_named_without_a_program_run_gets_no_exec_note(project):
    # a bare "Permission denied" naming a readable, writable file (a write the host cannot explain) still says nothing
    hook = project / 'hooks' / 'post_build'
    hook.parent.mkdir(exist_ok=True)
    hook.write_text('#!/bin/sh\n')
    os.chmod(hook, 0o644)
    assert cause_note("PermissionError: [Errno 13] Permission denied: 'hooks/post_build'\n", project) is None
    os.chmod(hook, 0o444)  # read-only by its own permissions: the write note, as before
    assert 'is read-only by its own permissions' in cause_note(HOOK_RAN, project)


@pytest.mark.parametrize('output, expected', [
    ('touch: /tmp/app.lock: Operation not permitted\n', terminal.OUTSIDE_CAUSE.format(path='/tmp/app.lock')),
    ("fatal: Unable to create '{root}/.git/index.lock': Operation not permitted\n",
     terminal.PRIVATE_CAUSE.format(path='.git/index.lock')),
    ('Traceback (most recent call last):\n  File "check.py", line 4, in <module>\n    os.kill(pid, 0)\n'
     'PermissionError: [Errno 1] Operation not permitted\n', terminal.SIGNAL_CAUSE),
    ('Traceback (most recent call last):\n  File "serve.py", line 9, in <module>\n    sock.bind(("127.0.0.1", 8765))\n'
     'PermissionError: [Errno 1] Operation not permitted\n', terminal.SOCKET_CAUSE),
    ('build: run/preview.pid says the preview server holds port 8765, and checking the port failed: [Errno 1] '
     'Operation not permitted.\n', terminal.SOCKET_CAUSE),
    ('error: data/.import.lock exists (pid %d): another import may be running\n' % GONE,
     terminal.STALE_PID.format(pid=GONE)),
])
def test_each_cause_the_host_can_see_is_named_with_what_works(project, output, expected):
    assert cause_note(output.format(root=project), project) == expected


def test_a_running_process_a_lock_names_is_said_to_run_with_its_name(project):
    sleeper = subprocess.Popen(['/bin/sleep', '30'])
    try:
        note = cause_note('error: run/export.lock is held by pid %d\n' % sleeper.pid, project)
        assert note == terminal.LIVE_PID.format(pid=sleeper.pid, name='sleep')
        # a refused kill says only that the process is not the command's own; the host says that it runs, and what
        note = cause_note('kill: %d: Operation not permitted\n' % sleeper.pid, project)
        assert note == terminal.SIGNAL_CAUSE + ' ' + terminal.LIVE_PID.format(pid=sleeper.pid, name='sleep')
    finally:
        sleeper.kill()
        sleeper.wait()


# -- a lock file that names no process (the lead, 1.9.29: D212 counts, 3 held-out cells on each model) --------------------
# Restated after an independent review (2026-10-30 ~21:05): every input it reproduced is pinned below.

SCRIPT = 'pebble_lock_probe_7c1e.py'  # a name no other process on this Mac runs
LOCKED = 'sync: another sync is running (.sync.lock exists since 2026-10-29 03:12:44); remove it if no sync runs\n'


def _holder(project, before=(), after=(), folder='scripts'):
    """A process that runs the probe script as `python <before> <script> <after>`."""
    script = project / folder / SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text('import sys, time\nprint(1, flush=True)\ntime.sleep(30)\n')
    process = subprocess.Popen([sys.executable, *before, str(script), *after], stdout=subprocess.PIPE)
    process.stdout.readline()  # it runs now
    return process


def _stop(process):
    process.kill()
    process.wait()


def test_a_lock_that_names_no_process_and_that_nothing_holds_is_said_to_be_stale(project):
    (project / '.sync.lock').write_text('started 2026-10-29T03:12:44Z by sync\n')
    note = cause_note(LOCKED, project, command='python3 scripts/%s --all' % SCRIPT)
    assert note == terminal.FREE_LOCK.format(path='.sync.lock', program=SCRIPT)
    assert 'removing it is the fix' in note and 'Unless another program takes this lock' in note
    # without the command the host cannot say what would hold it: nothing is said
    assert cause_note(LOCKED, project) is None


def test_a_lock_held_open_is_said_to_be_in_use(project):
    lock = project / '.sync.lock'
    lock.write_text('started 2026-10-29T03:12:44Z by sync\n')
    holder = subprocess.Popen([sys.executable, '-c', 'import sys, time; f = open(sys.argv[1]); print(1, flush=True); '
                               'time.sleep(30)', str(lock)], stdout=subprocess.PIPE)
    try:
        holder.stdout.readline()  # the file is open now
        note = cause_note(LOCKED, project, command='python3 scripts/%s' % SCRIPT)
        assert note.startswith('.sync.lock is in use: ') and '(PID %d) has it open' % holder.pid in note
        assert 'removing it is the fix' not in note and 'not one this task started' not in note  # never claimed
    finally:
        _stop(holder)


@pytest.mark.parametrize('before, after', [
    ((), ()),
    ((), ('-c', 'conf.yaml')),  # review 1: an option after the script was read as inline code
    ((), ('-m', 'fast')),       # review 1: ... or as a module
    (('-W', 'ignore'), ()),     # review 1: an option's value was read as the script
    (('-X', 'dev'), ('--watch',)),
])
def test_a_lock_whose_program_runs_is_never_said_to_be_stale(project, before, after):
    (project / '.sync.lock').write_text('started 2026-10-29T03:12:44Z by sync\n')
    holder = _holder(project, before, after)
    try:
        note = cause_note(LOCKED, project, command='python3 scripts/%s --once' % SCRIPT)
        assert note and '(PID %d) runs %s' % (holder.pid, SCRIPT) in note and 'removing it is the fix' not in note
    finally:
        _stop(holder)


def test_a_script_in_a_folder_with_a_space_still_counts_as_running(project):
    # review 1: ps joins arguments with spaces, so "/…/My Proj/scripts/x.py" split at the space
    (project / '.sync.lock').write_text('started 2026-10-29T03:12:44Z by sync\n')
    holder = _holder(project, folder='My Proj/scripts')
    try:
        note = cause_note(LOCKED, project, command='python3 "My Proj/scripts/%s"' % SCRIPT)
        assert '(PID %d) runs %s' % (holder.pid, SCRIPT) in note
    finally:
        _stop(holder)


def test_a_runner_that_failed_never_makes_a_lock_stale(project):
    # review 1: `make sync` fails while `python3 scripts/x.py --watch` holds the lock; make never holds it itself
    (project / '.sync.lock').write_text('started 2026-10-29T03:12:44Z by sync\n')
    for command in ('make sync', 'npm run sync', 'python3 -c "import sync"', 'bash -ec "python3 scripts/x.py"'):
        assert cause_note(LOCKED, project, command=command) is None, command


def test_a_process_id_inside_the_lock_file_is_checked_like_one_in_the_output(project):
    (project / '.sync.lock').write_text('%d\n' % GONE)
    assert cause_note(LOCKED, project, command='python3 scripts/%s' % SCRIPT) == terminal.STALE_PID.format(pid=GONE)
    sleeper = subprocess.Popen(['/bin/sleep', '30'])
    try:
        (project / '.sync.lock').write_text('{"pid": %d, "host": "mac"}' % sleeper.pid)
        assert cause_note(LOCKED, project, command='python3 scripts/%s' % SCRIPT) == \
            terminal.LIVE_PID.format(pid=sleeper.pid, name='sleep')
    finally:
        _stop(sleeper)


@pytest.mark.parametrize('name', ['package-lock.json', 'yarn.lock', 'Cargo.lock', 'poetry.lock', 'Gemfile.lock',
                                  'uv.lock', 'composer.lock', 'flake.lock', 'pnpm-lock.yaml', 'requirements.lock',
                                  'requirements-dev.lock', 'MODULE.bazel.lock', 'renv.lock', 'paket.lock',
                                  'gradle/dependency-locks/compileClasspath.lockfile'])
def test_a_dependency_lockfile_is_never_a_process_lock(project, name):
    (project / name).parent.mkdir(parents=True, exist_ok=True)
    (project / name).write_text('{}\n')
    output = 'error: %s already exists and is out of date with the manifest\n' % name
    assert cause_note(output, project, command='python3 scripts/%s' % SCRIPT) is None


def test_files_that_are_not_held_locks_get_no_lock_note(project):
    (project / 'tests' / 'fixtures').mkdir(parents=True)
    (project / 'tests' / 'fixtures' / 'minimal.lock').write_text('x\n')
    (project / 'old.pid').write_text('%d\n' % GONE)
    (project / 'deps.lock').write_text('# generated by a package manager\nflask==3.1.0\n')
    (project / 'tools.lock').write_text('[[package]]\nname = "x"\nchecksum = "ab"\n')
    command = 'python3 scripts/%s' % SCRIPT
    # review 3: a file named like a lock, on a line that says nothing of contention, is no lock
    assert cause_note("AssertionError: parse('tests/fixtures/minimal.lock') returned None\n", project,
                      command=command) is None
    assert cause_note('loaded old.pid as a sample fixture\n', project, command=command) is None
    for name in ('deps.lock', 'tools.lock'):  # contention words, but the content is a dependency list
        assert cause_note('error: %s exists and is out of date\n' % name, project, command=command) is None
    # Git metadata and the code folders another note covers are never read
    assert cause_note("fatal: Unable to create '.git/index.lock': File exists. Another git process seems to be "
                      "running.\n", project, command=command) is None
    (project / 'node_modules' / '.cache').mkdir(parents=True)
    (project / 'node_modules' / '.cache' / 'x.lock').write_text('started\n')
    assert cause_note('error: node_modules/.cache/x.lock exists\n', project, skip=('node_modules',),
                      command=command) is None


def test_a_lock_outside_the_project_a_link_or_a_large_file_is_not_read(project, tmp_path):
    outside = tmp_path / 'elsewhere.lock'
    outside.write_text('x\n')
    assert cause_note('sync: %s exists\n' % outside, project, command='python3 %s' % SCRIPT) is None
    (project / 'run' / 'linked.lock').symlink_to(outside)
    assert cause_note('sync: run/linked.lock exists\n', project, command='python3 %s' % SCRIPT) is None
    (project / 'linked').symlink_to(tmp_path, target_is_directory=True)  # a linked folder leading out
    assert cause_note('sync: linked/elsewhere.lock exists\n', project, command='python3 %s' % SCRIPT) is None
    (project / 'run' / 'big.lock').write_text('x' * 5000)
    assert cause_note('sync: run/big.lock exists\n', project, command='python3 %s' % SCRIPT) is None


def test_a_fifo_never_blocks_the_backend(project, monkeypatch):
    # review 6: a FIFO swapped in for the lock between lstat and open blocked open() for good
    fifo = project / 'run' / 'x.lock'
    os.mkfifo(fifo)
    assert terminal._small_text(fifo) is None  # lstat already says FIFO
    regular = os.lstat(project / 'out' / 'report.csv')
    real_lstat = os.lstat
    monkeypatch.setattr(terminal.os, 'lstat', lambda path, *args, **kwargs: regular if str(path) == str(fifo)
                        else real_lstat(path, *args, **kwargs))  # as if a regular file stood there at lstat
    started = time.monotonic()
    assert terminal._small_text(fifo) is None and time.monotonic() - started < 1


def test_many_spellings_of_one_lock_and_long_words_stay_cheap(project):
    # review 6: each spelling of one file ran lsof again; a nested token pattern backtracked on a long word
    (project / '.sync.lock').write_text('started\n')
    spellings = ' '.join('d%d/../.sync.lock' % i for i in range(200))
    started = time.monotonic()
    note = cause_note('sync: lock held by another run: %s\n' % spellings, project, command='python3 -c "x"')
    assert note is None and time.monotonic() - started < 3
    started = time.monotonic()
    assert cause_note('lock held: ' + 'a' * 16000 + '\n', project, command='python3 x.py') is None
    assert time.monotonic() - started < 1


def test_a_host_that_cannot_tell_says_nothing(project, monkeypatch):
    # review 8: lsof's 1 with an error on stderr is not "nobody has it open"
    (project / '.sync.lock').write_text('started\n')
    real = subprocess.run
    def fake(argv, **kwargs):
        if argv[0] == '/usr/sbin/lsof':
            return subprocess.CompletedProcess(argv, 1, '', 'lsof: status error on .sync.lock\n')
        return real(argv, **kwargs)
    monkeypatch.setattr(terminal.subprocess, 'run', fake)
    assert cause_note(LOCKED, project, command='python3 scripts/%s' % SCRIPT) is None


BLIND_PGREP = 'sysmon request failed with error: sysmond service not found\npgrep: Cannot get process list\n'


def test_a_pgrep_the_sandbox_blinds_is_answered_by_the_host_by_program_name(project):
    name = SCRIPT[:-3]
    note = cause_note(BLIND_PGREP, project, command='pgrep -af %s' % name)
    assert note == terminal.LISTED.format(answer='no process on this Mac runs a program or script whose name contains '
                                          '"%s" (Pebble compares program and script names, never other arguments).'
                                          % name)
    holder = _holder(project, after=('--token=hidden-value',))
    try:
        note = cause_note(BLIND_PGREP, project, command='pgrep -f %s' % name)
        assert '%s (PID %d, ' % (SCRIPT, holder.pid) in note and 'hidden-value' not in note  # names, never arguments
        # review 2: a pattern, a path, a PID file or any other option is not answered (a wrong "no process" decides)
        for command in ('pgrep -f scripts/%s' % SCRIPT, "pgrep '%s\\.py'" % name, "pgrep 'python.*%s'" % name,
                        "pgrep '[%s]%s'" % (name[0], name[1:]), 'pgrep -F run/sync.pid', 'pgrep -u me %s' % name,
                        'pgrep'):
            assert cause_note(BLIND_PGREP, project, command=command) is None, command
    finally:
        _stop(holder)


def test_a_pgrep_answer_never_shows_another_process_s_arguments(project):
    # review 4: `pgrep -i review_token` showed "REVIEW_TOKEN=abc123secretvalue" from a running `sh -ec '...'`
    shell = subprocess.Popen(['/bin/sh', '-ec', 'REVIEW_TOKEN=abc123secretvalue; export REVIEW_TOKEN; sleep 30'])
    try:
        time.sleep(0.3)
        for command in ('pgrep -i review_token', 'pgrep -i abc123', 'pgrep -f secretvalue'):
            note = cause_note(BLIND_PGREP, project, command=command)  # only the model's own pattern comes back
            assert note and 'abc123secretvalue' not in note and 'REVIEW_TOKEN' not in note, (command, note)
            assert 'running now' not in note, (command, note)  # arguments are never searched
    finally:
        _stop(shell)


def test_a_process_with_undecodable_arguments_never_breaks_a_note(project):
    # review 5: one process anywhere with non-UTF-8 argument bytes made cause_note raise after the command ran
    odd = subprocess.Popen([sys.executable.encode(), b'-c', b'import time; time.sleep(30)', b'\xff\xfe-odd'])
    try:
        time.sleep(0.3)
        (project / '.sync.lock').write_text('started\n')
        assert cause_note(LOCKED, project, command='python3 scripts/%s' % SCRIPT)  # a note, no exception
        assert cause_note(BLIND_PGREP, project, command='pgrep odd')
    finally:
        _stop(odd)


def test_a_sandbox_exec_refusal_of_a_project_script_still_names_its_exec_bit(project):
    # review 7: only "Operation not permitted" (the sandbox) is skipped; "Permission denied" is the file's own mode
    hook = project / 'hooks' / 'post_build'
    hook.parent.mkdir(exist_ok=True)
    hook.write_text('#!/bin/sh\n')
    os.chmod(hook, 0o644)
    note = cause_note("sandbox-exec: execvp() of '%s' failed: Permission denied\n" % hook, project)
    assert note == terminal.EXEC_CAUSE.format(path='hooks/post_build', mode='-rw-r--r--')
    assert cause_note("sandbox-exec: execvp() of '/bin/ps' failed: Operation not permitted\n", project,
                      command='ps -eo pid,args') == terminal.LIST_HINT
    refused = PolicyError('The confined command could not start; nothing ran.')
    assert terminal.process_list_hint(refused, 'ps -eo pid,lstart,args') == terminal.LIST_HINT
    assert terminal.process_list_hint(refused, 'python3 x.py') is None
    assert terminal.process_list_hint(PolicyError('Pipes are blocked.'), 'ps -ef') is None


def test_the_program_of_a_command_line_is_the_script_it_runs(project):
    program = terminal._program
    assert program('python3 scripts/sync.py --all') == 'sync.py' and program('python3 -m tools.sync') == 'tools.sync'
    assert program('PYTHONPATH=. python3 -u ./run.py') == 'run.py' and program('./scripts/build.sh x', project) == 'build.sh'
    assert program('python3 -W ignore scripts/x.py') == 'x.py' and program('python3 scripts/x.py -c conf.yaml') == 'x.py'
    assert program('bash scripts/deploy') == 'deploy' and program('%s/bin/syncd 40' % project, project) == 'syncd'
    # a runner wherever it lives, inline code, an option's value that may be the script, or an unreadable line names
    # no script, so the host never calls a lock stale for it (the second review: --require, -I, /usr/bin/make)
    for line in ('make build', 'npm run sync', '', '"unclosed', 'zsh -c "python3 x.py"', 'python3 -c "print(1)" x.py',
                 'bash -ec "x.sh"', 'node -e "1"', 'python3', 'node --require dotenv/config scripts/syncn.js',
                 'perl -I lib/perl5 x.pl', 'ruby -I lib/ext x.rb', '/usr/bin/make sync', '/usr/local/bin/syncd'):
        assert program(line, project) is None, line


def test_an_executable_whose_path_holds_a_space_is_found_by_its_name(project):
    # the second review: "/…/My Bin/bin/syncd" was read as "My" from ps's arguments, so its lock looked free
    binary = project / 'My Bin' / 'bin' / 'syncd_7c1e'
    binary.parent.mkdir(parents=True)
    source = project / 'sleeper.c'
    source.write_text('#include <unistd.h>\nint main(void) { sleep(30); return 0; }\n')
    built = subprocess.run(['/usr/bin/cc', '-o', str(binary), str(source)], capture_output=True,
                           env={**os.environ, 'DEVELOPER_DIR': '/Library/Developer/CommandLineTools'})
    if built.returncode != 0:
        pytest.skip('no C compiler here')  # a copied system binary does not run on macOS
    (project / '.sync.lock').write_text('started\n')
    runner = subprocess.Popen([str(binary), '30'])
    try:
        time.sleep(0.3)
        if runner.poll() is not None:
            pytest.skip('the built sleeper did not run')
        note = cause_note(LOCKED, project, command='"%s" 40' % binary)
        assert note and '(PID %d) runs syncd_7c1e' % runner.pid in note and 'removing it is the fix' not in note
        assert '(PID %d)' % runner.pid in cause_note(BLIND_PGREP, project, command='pgrep -x syncd_7c1e')
    finally:
        _stop(runner)


def _synthetic(monkeypatch, *rows):
    monkeypatch.setattr(terminal, '_processes', lambda: [(pid, exe, args) for pid, exe, args in rows])


def test_pgrep_lists_the_closest_names_first_and_says_how_many_more(monkeypatch, project):
    # the second review: three system daemons filled the answer and the project's own process never showed
    _synthetic(monkeypatch, (101, '/usr/libexec/clocksyncd', ['/usr/libexec/clocksyncd']),
               (102, '/usr/libexec/syncdefaultsd', ['/usr/libexec/syncdefaultsd']),
               (103, '/usr/libexec/mapssyncd', ['/usr/libexec/mapssyncd']),
               (104, '/usr/libexec/xsyncd', ['/usr/libexec/xsyncd']),
               (200, '/usr/bin/python3', ['python3', 'scripts/sync.py', '--watch']))
    note = cause_note(BLIND_PGREP, project, command='pgrep -f sync')
    assert note.index('sync.py (PID 200') < note.index('clocksyncd') and note.endswith(' and 2 more.')


def test_pgrep_never_shows_an_address_or_a_later_option_value(monkeypatch, project):
    # the second review: a URL after the script, and "-m" after the script, leaked into the answer
    _synthetic(monkeypatch, (301, '/usr/bin/python3', ['python3', 'notify.py',
                                                       'https://hooks.example.com/services/T0/B0/xoxbSECRET123']),
               (302, '/usr/bin/python3', ['python3', 'tool.py', '-m', 'HUSHWORD42']))
    for command, secret in (('pgrep -i xoxb', 'xoxbSECRET123'), ('pgrep -i hush', 'HUSHWORD42')):
        note = cause_note(BLIND_PGREP, project, command=command)
        assert secret not in note and 'running now' not in note, (command, note)
    assert 'notify.py (PID 301' in cause_note(BLIND_PGREP, project, command='pgrep notify')


def test_a_path_on_the_line_never_supplies_the_contention_word(project):
    # the second review: "tests/fixtures/stale.lock" gave FREE_LOCK through the word "stale" in its own name
    (project / 'tests' / 'fixtures').mkdir(parents=True)
    (project / 'tests' / 'fixtures' / 'stale.lock').write_text('x\n')
    assert cause_note("AssertionError: read_lock('tests/fixtures/stale.lock') returned None\n", project,
                      command='python3 scripts/%s' % SCRIPT) is None


@pytest.mark.parametrize('output', [
    'AssertionError: 3 != 4\n', 'FAILED (failures=1)\n', '',
    "PermissionError: [Errno 1] Operation not permitted: '.cache/catalog/sync.lock'\n",  # the code-folder note's
    'Traceback (most recent call last):\n  File "x.py", line 3\n    lock.acquire()\nPermissionError: [Errno 1] '
    'Operation not permitted\n',  # no cause the host can see: nothing is said
])
def test_nothing_is_said_where_the_host_sees_no_cause(project, output):
    assert cause_note(output, project, skip=confined.CODE_PARTS) is None


def test_a_failed_commands_result_carries_its_cause_to_the_model(env, monkeypatch):
    monkeypatch.setenv('PEBBLE_AUTO_RISKY_ONLY', '0')  # the approval path: every command it covers takes it too
    project = env.project.resolve()
    (project / 'run').mkdir()
    (project / 'run' / 'export.lock').write_text('48213 2026-10-28T02:14:09+00:00\n')
    os.chmod(project / 'run', 0o555)
    env.output = ("export: run/export.lock is stale (pid %d) but could not be cleared: [Errno 13] Permission denied: "
                  "'%s'\n" % (GONE, project / 'run' / 'export.lock'))
    try:
        app = env.app
        large(app)
        chat = app.new_chat(str(project))
        env.script = [call('RunCommand', {'command': 'python3 calc.py'}, 'run'),
                      final('run/ is read-only.\n\nFINAL: blocked')]
        app.work(chat, 'Run calc.py. End with `FINAL: done` or `FINAL: blocked`.', False, [])
        rid = app.runs.list(chat['id'])[0]['id']
        settled(env, rid, 'waiting_approval', 1)
        command = next(a for a in app.store.actions(chat['id']) if a['kind'] == 'command' and a['status'] == 'pending')
        app.approve(chat['id'], command['id'])
        settled(env, rid, 'completed', 2)
        receipt = next(t for t in app.runs.tools(rid) if t['name'] == 'RunCommand')
        assert '`chmod u+w run`' in receipt['result']['result']['cause']
        assert '`chmod u+w run`' in contents(env.requests[1])  # the model's next request reads it
    finally:
        os.chmod(project / 'run', 0o755)


def test_a_command_that_succeeded_gets_no_cause(env, monkeypatch):
    monkeypatch.setenv('PEBBLE_AUTO_RISKY_ONLY', '0')
    project = env.project.resolve()
    (project / 'run').mkdir()
    os.chmod(project / 'run', 0o555)
    monkeypatch.setattr(app_module, 'run_command', lambda payload, cancel: {
        'exit_code': 0, 'output': "warning: [Errno 13] Permission denied: '%s'\n" % (project / 'run' / 'x.lock'),
        'output_projection': 'terminal_text/1', 'controls_removed': 0, 'raw_output_sha256': '0' * 64,
        'command': payload['command'], 'isolation': 'fixture'})
    try:
        app = env.app
        large(app)
        chat = app.new_chat(str(project))
        env.script = [call('RunCommand', {'command': 'python3 calc.py'}, 'run'), final('It warned.\n\nFINAL: blocked')]
        app.work(chat, 'Run calc.py. End with `FINAL: done` or `FINAL: blocked`.', False, [])
        rid = app.runs.list(chat['id'])[0]['id']
        settled(env, rid, 'waiting_approval', 1)
        command = next(a for a in app.store.actions(chat['id']) if a['kind'] == 'command' and a['status'] == 'pending')
        app.approve(chat['id'], command['id'])
        settled(env, rid, 'completed', 2)
        receipt = next(t for t in app.runs.tools(rid) if t['name'] == 'RunCommand')
        assert 'cause' not in receipt['result']['result']  # exit 0: the command's own words stand
    finally:
        os.chmod(project / 'run', 0o755)


# -- the system prompt keeps 1.9.28's sentence on every route ------------------------------------------------------------
# Restated by the lead (2026-10-30 ~16:10): w21's F5 rewrote it on large routes to "the person's Pebble permissions", and
# Opus d-rc-109 phrasing 2 then failed 3 of 3 as false completions where the same tree without F5 passed 3 of 3 (1.9.28
# 4 of 4). The cause notes carry the permission facts instead.

def test_the_large_route_keeps_the_permissions_sentence_unchanged(tmp_path):
    from pebble.tool_catalogue import SYSTEM
    from test_tool_offering_24 import LARGE, first, system_of
    system = system_of(first(tmp_path / 'chat', budget=LARGE, text='Run export.py and fix what stops it.', web=False)[0])
    assert 'Tools cannot change permissions or access credentials.' in system
    assert "the person's Pebble permissions" not in system
    assert 'Tools cannot change permissions or access credentials.' in SYSTEM  # 4K and 8K: the pinned text, unchanged


# -- the card-free path's code folders ------------------------------------------------------------------------------------

def test_the_code_folder_note_says_to_run_the_command_again_now():
    note = confined.Copies(Path('/nonexistent')).refusal(
        'run-1', 'python3 scripts/sync.py', "PermissionError: [Errno 1] Operation not permitted: '.cache/x.lock'")
    assert 'run the same command again now' in note and 'no reason to stop' in note
    assert 'waits for the person' not in note


# -- the ledger: configuration and pins are never the data -----------------------------------------------------------------

@pytest.mark.parametrize('path, has, lacks', [
    ('config/report.json', {'config'}, {'data'}), ('requirements-dev.txt', {'config'}, {'data'}),
    ('settings.json', {'config'}, {'data'}), ('package.json', {'config'}, {'data'}), ('.python-version', {'config'}, {'data'}),
    ('data/settings.json', {'data', 'config'}, set()), ('data/sales.csv', {'data'}, {'config'}),
    ('notes.txt', {'data'}, {'config'}), ('report.toml', {'config'}, {'data'}), ('scripts/run.py', {'code'}, {'data'}),
])
def test_a_configuration_or_pin_file_is_never_data_unless_it_lies_in_data(path, has, lacks):
    kinds = requirements._kinds(path)
    assert has <= kinds and not lacks & kinds


REPORT = ('Run scripts/report.py to produce out/report.csv and tell me the grand total it prints. It stopped working after '
          'reportkit was upgraded to 2.0. The report reads config/report.json and data/sales.csv. Fix the cause, but '
          "don't change the data, and leave reportkit/ as it is.")
NOTES = ('Generate the release notes with scripts/release_notes.py and tell me how many entries they list. It reads '
         'data/changes.csv and checks requirements-dev.txt against the tools before it writes. If it fails, fix the cause, '
         'but leave the data alone.')


@pytest.mark.parametrize('text, kept', [
    (REPORT, ['data/sales.csv', 'reportkit/']),
    ('scripts/report.py -> out/report.csv (reads config/report.json, data/sales.csv) broke with the reportkit 2.0 '
     'upgrade. Fix the cause, not the data; reportkit/ stays as it is.', ['data/sales.csv', 'reportkit/']),
    ('Could you get scripts/report.py producing out/report.csv again and tell me the grand total it prints? It broke '
     'when reportkit was upgraded to 2.0; it reads config/report.json and data/sales.csv. Please fix the cause, but '
     'leave the data and reportkit/ alone.', ['data/sales.csv', 'reportkit/']),  # the output it produces is not data
    (NOTES, ['data/changes.csv']),
    ('scripts/release_notes.py -> out/RELEASE_NOTES.md (reads data/changes.csv; checks requirements-dev.txt against '
     'tools/). Fix the cause if it fails (not the data).', ['data/changes.csv']),
])
def test_the_data_a_request_keeps_leaves_its_configuration_and_pins_free(text, kept):
    found = [p for item in requirements.extract(text) if item['kind'] == 'unchanged' for p in item['paths']]
    assert sorted(found) == sorted(kept)


def test_the_run_is_told_only_the_data_is_kept(tmp_path):
    for rel in ('scripts/report.py', 'config/report.json', 'data/sales.csv', 'reportkit/__init__.py'):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text('x\n')
    found = requirements.extract(REPORT)
    note = requirements.kept_note(REPORT, found, requirements.baseline(tmp_path, found), ())
    assert '"data/sales.csv"' in note and 'config/report.json' not in note


def test_reading_a_lock_or_pid_file_carries_the_hosts_fact(project, tmp_path):
    # the lead (final check d-rc-109 phrasing 3 on 29g): the run read the lock and asked pgrep, learned no import ran,
    # and still kept the stale lock; the decisive "removing it is the fix" came only with a failed run's note
    (project / 'data').mkdir()
    (project / 'data' / '.import.lock').write_text('pid %d\nstarted 2026-10-27T23:10:05Z\n' % GONE)
    assert terminal.lock_read_note(project, 'data/.import.lock') == terminal.STALE_PID.format(pid=GONE)
    sleeper = subprocess.Popen(['/bin/sleep', '30'])
    try:
        (project / 'run' / 'app.pid').write_text('%d\n' % sleeper.pid)
        assert terminal.lock_read_note(project, 'run/app.pid') == terminal.LIVE_PID.format(pid=sleeper.pid, name='sleep')
    finally:
        _stop(sleeper)
    (project / '.catalog.lock').write_text('locked at 2026-10-29T03:12:44Z\n')
    assert terminal.lock_read_note(project, '.catalog.lock') == terminal.READ_FREE.format(path='.catalog.lock')
    holder = subprocess.Popen([sys.executable, '-c', 'import sys, time; f = open(sys.argv[1]); print(1, flush=True); '
                               'time.sleep(30)', str(project / '.catalog.lock')], stdout=subprocess.PIPE)
    try:
        holder.stdout.readline()
        assert '(PID %d) has it open' % holder.pid in terminal.lock_read_note(project, '.catalog.lock')
    finally:
        _stop(holder)
    # anything else gets nothing: a dependency lockfile, an ordinary file, Git metadata, a file outside, a large one
    (project / 'yarn.lock').write_text('# yarn lockfile v1\n')
    (project / '.git' / 'index.lock').write_text('x\n')
    outside = tmp_path / 'other.lock'
    outside.write_text('x\n')
    (project / 'run' / 'big.lock').write_text('x' * 5000)
    for path in ('yarn.lock', 'scripts/export.py', '.git/index.lock', str(outside), 'run/big.lock', 'missing.lock'):
        assert terminal.lock_read_note(project, path) is None, path
