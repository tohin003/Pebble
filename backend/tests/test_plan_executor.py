"""The contained plan executor, stage A (X03, X04): typed plans under shim + anchor.

Pipelines, per-step cwd and env, exact writable subpaths, process_model
'single', executable drift caught right before exec, and destination-scoped
network through a per-job broker whose credentials travel only over a pipe.

Signal safety: every plan written here gets its own outside canary and the
anchor's SIGURG interlock (a test-only plan field), so each sandbox-instance
kill(-1) sweep first proves it cannot reach a process outside the fresh
instance this test created. The `scoped` fixture asserts (reached, alive) ==
(False, True) for every canary. The only processes a test ends directly are
its own canaries and its own HTTPS fixture threads, by handle.
"""

import ast
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import async_terminal, command_plans, containment, job_shim, plan_runner, process_jobs, sandbox_profiles
from pebble.egress import network_profile
from pebble.policy import PolicyError, Workspace
from pebble.process_jobs import ProcessJobs
from pebble.runtime import RunJournal
from pebble.store import Store
from pebble.terminal import CommandOutcomeUncertain
from pebble.time_ledger import TimeLedger

ROOT = Path(__file__).resolve().parents[2]
PY = Path(sys.executable).resolve()
MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
macos = pytest.mark.skipif(not MAC, reason='actual macOS confinement')
LAUNCHER = containment.launcher(False)  # backend/main.py's --job-shim/--job-anchor dispatch
SECRET = 'w8-vault-' + uuid.uuid4().hex + uuid.uuid4().hex
CANARY = '''import signal, sys, time
signal.signal(signal.SIGURG, lambda *a: open(sys.argv[1] + '.urg', 'a').write('x'))
end = time.time() + 120
while time.time() < end:
    open(sys.argv[1] + '.beat', 'w').write(str(time.time())); time.sleep(0.05)
'''


# --- outside canaries and the anchor's SIGURG interlock ------------------------------------

def outside_canary(folder):
    beat = Path(folder) / ('canary-' + uuid.uuid4().hex[:8])
    proc = subprocess.Popen([sys.executable, '-I', '-c', CANARY, str(beat)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(200):
        if Path(str(beat) + '.beat').exists():
            break
        time.sleep(0.02)
    return {'proc': proc, 'beat': Path(str(beat) + '.beat'), 'urg': Path(str(beat) + '.urg')}


def canary_result(canary):
    """(reached, alive): did SIGURG reach it, and was its heartbeat fresh? Then end it by its handle."""
    alive = False
    for _ in range(20):
        try:
            alive = time.time() - float(canary['beat'].read_text()) < 1.5
            break
        except (OSError, ValueError):
            time.sleep(0.05)
    canary['proc'].terminate()
    canary['proc'].wait(timeout=10)
    return canary['urg'].exists(), alive


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    """Every plan written in this process gets its own outside canary and the anchor's interlock."""
    canaries, real = [], job_shim.write_plan

    def write_plan(jobdir, plan):
        canary = outside_canary(tmp_path)
        canaries.append(canary)
        return real(jobdir, {**plan, 'interlock_abort_path': str(canary['urg'])})
    monkeypatch.setattr(job_shim, 'write_plan', write_plan)
    yield canaries
    assert [canary_result(c) for c in canaries] == [(False, True)] * len(canaries), \
        'A job sweep reached or killed a process outside its sandbox.'


@pytest.fixture
def project(tmp_path):
    folder = (tmp_path / 'project').resolve()
    folder.mkdir()
    return folder


def script(folder, name, text):
    path = Path(folder) / name
    path.write_text(text)
    return path


def run(plan, *, launcher=LAUNCHER, jobdir=None, **kwargs):
    """run_plan with collecting callbacks: (result, output bytes, on_started infos, receipt)."""
    chunks, started, receipt = [], [], {}
    result = async_terminal.run_plan(plan, threading.Event(), started.append, chunks.append, launcher=launcher,
                                     jobdir=jobdir, receipt=receipt, **kwargs)
    return result, b''.join(chunks), started, receipt


def kinds(jobdir):
    return [r['record'] for r in job_shim.read_records(jobdir)]


def exited(jobdir):
    return next(r for r in job_shim.read_records(jobdir) if r['record'] == 'exited')


# --- profile builder: slots, pinned executables -------------------------------------------

def test_bind_fills_each_slot_exactly_once_and_refuses_leftovers(tmp_path):
    root, temp = (tmp_path / 'p').resolve(), (tmp_path / 't').resolve()
    root.mkdir(), temp.mkdir()
    base = dict(executable=PY, exec_paths=[], read_paths=[], project_root=root, temp=temp,
                job_marker=uuid.uuid4().hex)
    deferred = sandbox_profiles.build(**base, network='broker', broker_port=sandbox_profiles.DEFERRED,
                                      tty=sandbox_profiles.DEFERRED)
    assert deferred.count(sandbox_profiles.BROKER_SLOT) == 1 and deferred.count(sandbox_profiles.TTY_SLOT) == 1
    assert 'network-outbound' not in deferred and 'ttys' not in deferred  # an unbound slot grants nothing
    bound = sandbox_profiles.bind(deferred, broker_port=40123, tty='/dev/ttys042')
    assert network_profile(40123) in bound.split('\n')  # the network section is exactly network_profile(port)
    assert bound.count('network-outbound') == 1 and 'pebble-slot' not in bound
    tty = '(allow file-ioctl file-read* file-write* (literal "/dev/ttys042") (literal "/dev/tty"))'
    assert tty in bound.split('\n') and bound.count('ttys') == 1 and 'regex' not in tty
    # The same text as building with the values directly.
    assert bound == sandbox_profiles.build(**base, network='broker', broker_port=40123, tty='/dev/ttys042')
    plain = sandbox_profiles.build(**base)
    assert sandbox_profiles.bind(plain) == plain and 'ttys' not in plain and '/dev/tty' not in plain
    for profile, kwargs in ((deferred, {}), (deferred, {'broker_port': 40123}), (deferred, {'tty': '/dev/ttys1'}),
                            (plain, {'broker_port': 40123}), (plain, {'tty': '/dev/ttys1'}),
                            (deferred + '\n' + sandbox_profiles.BROKER_SLOT, {'broker_port': 1, 'tty': '/dev/ttys1'}),
                            (deferred, {'broker_port': 0, 'tty': '/dev/ttys1'}),
                            (deferred, {'broker_port': 40123, 'tty': '/dev/ttys*'}),
                            (deferred, {'broker_port': 40123, 'tty': '/dev/tty'}),
                            (deferred, {'broker_port': 40123, 'tty': '/dev/ttyp1'}),
                            (deferred, {'broker_port': 40123, 'tty': '/dev/ttys1\n(allow default)'}),
                            ('', {}), (None, {})):
        with pytest.raises(PolicyError):
            sandbox_profiles.bind(profile, **kwargs)
    for bad in ('/dev/ttys', '/dev/ttys1/../x', 'ttys1', '/dev/ptmx', '/private/dev/ttys1'):
        with pytest.raises(PolicyError):
            sandbox_profiles.build(**base, tty=bad)
    with pytest.raises(PolicyError):
        sandbox_profiles.build(**base, broker_port=sandbox_profiles.DEFERRED)  # a broker slot needs broker mode


def test_pinned_executables_are_exec_literals_and_still_refused_inside_writable_paths(tmp_path):
    root, temp = (tmp_path / 'p').resolve(), (tmp_path / 't').resolve()
    (root / 'build').mkdir(parents=True), temp.mkdir()
    tool = (tmp_path / 'tc' / 'bin').resolve()
    tool.mkdir(parents=True)
    profile = sandbox_profiles.build(executable=PY, executables=[tool / 'a', Path('/sbin/md5')], exec_paths=[],
                                     read_paths=[], project_root=root, temp=temp, writable_paths=[root / 'build'],
                                     job_marker=uuid.uuid4().hex)
    line = next(l for l in profile.split('\n') if l.startswith('(allow process-exec*'))
    assert f'(literal "{tool / "a"}")' in line and '(literal "/sbin/md5")' in line
    reads = next(l for l in profile.split('\n') if l.startswith('(allow file-read*'))
    assert f'(literal "{tool / "a"}")' in reads and f'(literal "{tool}")' in reads  # file plus dyld ancestor handle
    # Later rules win: a writable path's exec deny follows every allow.
    lines = profile.split('\n')
    assert lines.index(next(l for l in lines if l.startswith('(deny process-exec*'))) > lines.index(line)
    for bad in (['relative'], 'not-a-list', [str(tmp_path / 'link/../x')]):
        with pytest.raises(PolicyError):
            sandbox_profiles.build(executable=PY, executables=bad, exec_paths=[], read_paths=[], project_root=root,
                                   temp=temp, job_marker=uuid.uuid4().hex)


# --- anchor spec and credential frame parsing ---------------------------------------------

def step_spec(**changes):
    return {'argv': ['/bin/echo', 'x'], 'cwd': '/', 'env': {}, 'pin': {'realpath': '/bin/echo', 'sha256': 'a' * 64,
                                                                   'size': 1}, **changes}


def test_plan_specs_are_strict():
    good = {'mode': 'plan', 'steps': [step_spec()], 'single': False, 'pty': False}
    assert plan_runner.valid(good)
    for bad in ({**good, 'steps': []}, {**good, 'steps': [step_spec()] * 5}, {**good, 'single': 1},
                {**good, 'pty': True, 'steps': [step_spec(), step_spec()]}, {**good, 'steps': [step_spec(cwd='rel')]},
                {**good, 'steps': [step_spec(argv=[])]}, {**good, 'steps': [step_spec(argv=['/bin/echo', 'a\x00'])]},
                {**good, 'steps': [step_spec(env={'A': 1})]}, {**good, 'steps': [{**step_spec(), 'extra': 1}]},
                {**good, 'steps': [step_spec(pin={'realpath': '/bin/cat', 'sha256': 'a' * 64, 'size': 1})]},
                {**good, 'steps': [step_spec(pin={'realpath': '/bin/echo', 'sha256': 'a' * 64, 'size': '1'})]},
                {**good, 'pty': None}):
        assert not plan_runner.valid(bad), bad
    assert [plan_runner.exit_code(c) for c in ([0, 0], [3, 0], [0, 4], [2, 5, 0], [0, None], [])] == [0, 3, 4, 5, None,
                                                                                                    None]


def frame(*items, end=True):
    data = bytearray(job_shim.CREDENTIAL_MAGIC)
    for ref, value in items:
        data += len(ref).to_bytes(2, 'big') + ref + len(value).to_bytes(4, 'big') + value
    return data + (b'\x00\x00' if end else b'')


def test_credential_frames_parse_complete_and_refuse_malformed():
    data = frame((b'vault:a', b'one'), (b'vault:b', b'two'))
    found = job_shim._credentials(memoryview(data), len(data))
    assert found == {'vault:a': bytearray(b'one'), 'vault:b': bytearray(b'two')}
    for size in range(len(data)):  # every prefix is incomplete, never a smaller credential set
        assert job_shim._credentials(memoryview(data), size) is None
    for bad in (b'PCRED2\n' + data[7:], frame((b'x' * 129, b'v')), frame((b'a', b'')), frame((b'a', b'1'), (b'a', b'2')),
                frame(*[(b'r%d' % i, b'v') for i in range(9)]), frame((b'\xff', b'v'))):
        with pytest.raises(job_shim._Unstarted):
            job_shim._credentials(memoryview(bad), len(bad))


# --- pipelines, cwd, env, writable subpaths, single -----------------------------------------

GEN = "import sys\nprint('b\\na\\nc')\nprint('gen-err', file=sys.stderr)\n"
UPPER = "import sys\ndata = sys.stdin.read()\nprint('up-err', file=sys.stderr, flush=True)\nsys.stdout.write(data.upper())\n"
FAIL = "import sys\nprint('partial')\nsys.exit(3)\n"


@macos
def test_two_and_three_step_pipelines_connect_stdout_to_stdin_and_merge_stderr(project, scoped, tmp_path):
    for name, text in (('gen.py', GEN), ('upper.py', UPPER), ('fail.py', FAIL)):
        script(project, name, text)
    two = command_plans.from_argv([{'argv': ['python3', 'gen.py']}, {'argv': ['python3', 'upper.py']}], str(project),
                                  pipeline='linear')
    result, out, started, receipt = run(two, jobdir=tmp_path / 'jobs' / 'two')
    assert result == {'state': 'succeeded', 'exit_code': 0, 'isolation': result['isolation'],
                      'outcome_uncertain': False}, (out, kinds(tmp_path / 'jobs' / 'two'))
    assert 'shim+anchor · typed plan' in result['isolation'] and result['isolation'].endswith('network denied')
    assert b'B\nA\nC\n' in out and b'gen-err' in out and b'up-err' in out and b'b\n' not in out
    assert len(started) == 1 and type(started[0]['pid']) is int
    assert receipt['step_codes'] == [0, 0]
    assert kinds(tmp_path / 'jobs' / 'two') == ['spawning', 'spawned', 'started', 'exited', 'teardown']
    assert exited(tmp_path / 'jobs' / 'two')['contained'] is True
    three = command_plans.from_argv([{'argv': ['python3', 'gen.py']}, {'argv': ['python3', 'upper.py']},
                                     {'argv': ['sort']}], str(project), pipeline='linear')
    result, out, _, receipt = run(three)
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('succeeded', 0, False)
    assert b'A\nB\nC\n' in out and receipt['step_codes'] == [0, 0, 0]
    failing = command_plans.from_argv([{'argv': ['python3', 'fail.py']}, {'argv': ['python3', 'upper.py']}],
                                      str(project), pipeline='linear')
    result, out, _, receipt = run(failing)
    # pipefail: a failed step is never hidden by a later successful one.
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('failed', 3, False)
    assert b'PARTIAL' in out and receipt['step_codes'] == [3, 0]
    assert len(scoped) == 3


SHOW = ("import json, os, sys\n"
        "seen = json.loads(sys.stdin.read() or '[]')\n"
        "seen.append({'cwd': os.getcwd(), 'env': dict(os.environ), 'argv': sys.argv})\n"
        "print(json.dumps(seen))\n")


@macos
def test_per_step_cwd_and_env_are_exactly_the_reviewed_ones(project, scoped):
    (project / 'sub').mkdir()
    script(project / 'sub', 'show.py', SHOW)
    script(project, 'show.py', SHOW)
    plan = command_plans.from_argv([{'argv': ['python3', 'show.py'], 'cwd': 'sub', 'env': {'NO_COLOR': '1'}},
                                    {'argv': ['python3', 'show.py', 'two'], 'env': {'TZ': 'UTC', 'LANG': 'C'}}],
                                   str(project), pipeline='linear')
    result, out, _, _ = run(plan)
    assert result['state'] == 'succeeded', out
    first, second = json.loads(out)
    assert Path(first['cwd']).resolve() == project / 'sub' and Path(second['cwd']).resolve() == project
    assert first['env']['NO_COLOR'] == '1' and 'TZ' not in first['env']
    assert second['env']['TZ'] == 'UTC' and second['env']['LANG'] == 'C' and 'NO_COLOR' not in second['env']
    assert second['argv'][1:] == ['two']
    host = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'PYTHONNOUSERSITE', 'GIT_CONFIG_NOSYSTEM', 'GIT_TERMINAL_PROMPT'}
    for seen, extra in ((first, {'NO_COLOR'}), (second, {'TZ'})):
        # Only host-owned keys and the reviewed literals (CLT shims and Python may add their own).
        assert host | extra <= set(seen['env']) and not any(k.startswith('PEBBLE_') for k in seen['env'])
        assert not Path(seen['env']['HOME']).is_relative_to(project)
    assert len(scoped) == 1


WRITER = ("import errno, os\n"
          "for path in ('build/out.txt', 'build/deep/x.txt', 'src/x.txt', 'top.txt'):\n"
          "    try:\n"
          "        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)\n"
          "        open(path, 'w').write('w8')\n"
          "        print(path, 'ok')\n"
          "    except OSError as exc:\n"
          "        print(path, errno.errorcode.get(exc.errno, exc.errno))\n")


@macos
def test_subpath_write_is_allowed_while_a_sibling_and_the_root_are_refused(project, scoped, tmp_path):
    (project / 'build').mkdir(), (project / 'src').mkdir()
    script(project, 'writer.py', WRITER)
    plan = command_plans.from_argv([{'argv': ['python3', 'writer.py']}], str(project), writable=['build'])
    jobdir = tmp_path / 'jobs' / 'w'
    result, out, _, _ = run(plan, jobdir=jobdir)
    assert result['state'] == 'succeeded', out
    lines = dict(line.split(' ', 1) for line in out.decode().splitlines())
    assert lines == {'build/out.txt': 'ok', 'build/deep/x.txt': 'ok', 'src/x.txt': 'EPERM', 'top.txt': 'EPERM'}
    assert (project / 'build' / 'out.txt').read_text() == 'w8' and not (project / 'src' / 'x.txt').exists()
    assert not (project / 'top.txt').exists()
    profile = json.loads((jobdir / 'plan.json').read_text())['profile']
    writes = next(l for l in profile.split('\n') if l.startswith('(allow file-write*'))
    assert f'(subpath "{project / "build"}")' in writes and f'(subpath "{project}")' not in writes
    spawned = next(r for r in job_shim.read_records(jobdir) if r['record'] == 'spawned')
    assert spawned['profile_sha256'] == hashlib.sha256(profile.encode()).hexdigest()  # no slots: run as written
    # The reviewed resources are the shim's governors (W6 R6), not the legacy fixed limits.
    assert json.loads((jobdir / 'plan.json').read_text())['limits'] == {
        'timeout': 60, 'output_limit': 192_000, 'cpu_seconds': 60, 'fsize_bytes': 256 << 20, 'nofile': 256,
        'nproc': 64, 'memory_bytes': 2048 << 20}
    assert len(scoped) == 1


OPENER = ("import os\n"
          "held = []\n"
          "try:\n"
          "    while len(held) < 200:\n"
          "        held.append(os.open(os.devnull, os.O_RDONLY))\n"
          "except OSError as exc:\n"
          "    print('stopped', len(held), exc.errno)\n")


@macos
def test_reviewed_resources_are_enforced(project, scoped):
    script(project, 'opener.py', OPENER)
    plan = command_plans.from_argv([{'argv': ['python3', 'opener.py']}], str(project), resources={'nofile': 32})
    result, out, _, _ = run(plan)
    words = out.decode().split()
    assert result['state'] == 'succeeded' and words[0] == 'stopped' and words[2] == '24', out  # EMFILE
    assert int(words[1]) < 32
    assert len(scoped) == 1


def test_the_shim_never_writes_a_core_image(tmp_path):
    code = ('import resource, sys\nfrom pebble import job_shim\n'
            'code = job_shim.main(["x", "--job-shim", sys.argv[1]])\n'
            'print(code, resource.getrlimit(resource.RLIMIT_CORE))\n')
    done = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'missing')], capture_output=True, text=True,
                          env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(ROOT / 'backend')}, timeout=60)
    assert done.stdout.split() == ['2', '(0,', '0)'], done.stderr  # set before the plan is even read


FORKER = ("import os, subprocess\n"
          "def attempt(name, fn):\n"
          "    try:\n"
          "        fn(); print(name, 'allowed')\n"
          "    except OSError as exc:\n"
          "        print(name, 'refused', exc.errno)\n"
          "def fork():\n"
          "    pid = os.fork()\n"
          "    if pid == 0:\n"
          "        os._exit(0)\n"
          "    os.waitpid(pid, 0)\n"
          "attempt('fork', fork)\n"
          "attempt('spawn', lambda: os.waitpid(os.posix_spawn('/bin/echo', ['echo'], {}), 0))\n"
          "attempt('subprocess', lambda: subprocess.run(['/bin/echo'], stdout=subprocess.DEVNULL))\n")


@macos
def test_single_process_model_refuses_every_new_process_and_tree_allows_them(project, scoped):
    script(project, 'forker.py', FORKER)
    single = command_plans.from_argv([{'argv': ['python3', 'forker.py']}], str(project), process_model='single')
    result, out, _, _ = run(single)
    assert result['state'] == 'succeeded', out
    lines = out.decode().split('\n')
    assert {line.split(' ')[0]: line.split(' ')[1] for line in lines if line} == {
        'fork': 'refused', 'spawn': 'refused', 'subprocess': 'refused'}, out
    assert all(line.endswith(' 35') for line in lines if line), out  # EAGAIN: the hard RLIMIT_NPROC of 1
    tree = command_plans.from_argv([{'argv': ['python3', 'forker.py']}], str(project))
    result, out, _, _ = run(tree)
    assert out.decode().split() == ['fork', 'allowed', 'spawn', 'allowed', 'subprocess', 'allowed'], out
    assert len(scoped) == 2


ESCAPEE = ("import os, sys, time\n"
           "BEAT = \"import os, time\\np = os.path.join(os.environ['HOME'], 'escapee.beat')\\n"
           "end = time.time() + 30\\nwhile time.time() < end:\\n"
           "    open(p, 'w').write(str(time.time())); time.sleep(0.05)\\n\"\n"
           "if os.fork() == 0:\n"
           "    os.setsid()\n"
           "    if os.fork() == 0:\n"
           "        os.execv(sys.executable, [sys.executable, '-I', '-c', BEAT])\n"
           "    os._exit(0)\n"
           "os.wait()\n"
           "for _ in range(400):\n"
           "    if os.path.exists(os.path.join(os.environ['HOME'], 'escapee.beat')):\n"
           "        break\n"
           "    time.sleep(0.01)\n"
           "print('ready')\n")


def beat_age(path):
    for _ in range(20):  # a beat being rewritten can read empty for a moment
        try:
            return time.time() - float(path.read_text())
        except (OSError, ValueError):
            time.sleep(0.02)
    return None


@macos
def test_a_pipeline_steps_escapee_is_swept_with_the_instance(project, scoped, tmp_path):
    script(project, 'escapee.py', ESCAPEE)
    script(project, 'upper.py', UPPER)
    plan = command_plans.from_argv([{'argv': ['python3', 'escapee.py']}, {'argv': ['python3', 'upper.py']}],
                                   str(project), pipeline='linear')
    jobdir = tmp_path / 'jobs' / 'esc'
    result, out, _, _ = run(plan, jobdir=jobdir)
    assert (result['state'], result['outcome_uncertain']) == ('succeeded', False) and b'READY' in out
    beat = jobdir.with_name('esc-home') / 'escapee.beat'
    assert beat.exists()  # the setsid + double-fork escapee ran inside the instance
    time.sleep(1.0)
    assert beat_age(beat) > 0.8, 'the escapee outlived the sweep'
    assert exited(jobdir)['contained'] is True and len(scoped) == 1


def test_anchor_recheck_refuses_drifted_files_and_folders(tmp_path):
    exe = copied_tool(tmp_path)
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    folder = (tmp_path / 'cwd').resolve()
    folder.mkdir()
    good = {'mode': 'plan', 'single': False, 'pty': False, 'steps': [step_spec(
        argv=[str(exe)], cwd=str(folder), pin={'realpath': str(exe), 'sha256': digest, 'size': exe.stat().st_size})]}
    plan_runner.check(good)

    def refused(spec, field):
        with pytest.raises(plan_runner.Refused, match=field):
            plan_runner.check(spec)
    step = good['steps'][0]
    refused({**good, 'steps': [{**step, 'pin': {**step['pin'], 'size': step['pin']['size'] + 1}}]},
            r'steps\[0\]\.executable: size')
    refused({**good, 'steps': [{**step, 'pin': {**step['pin'], 'sha256': '0' * 64}}]}, r'steps\[0\]\.executable: sha256')
    refused({**good, 'steps': [{**step, 'cwd': str(folder / 'missing')}]}, r'steps\[0\]\.cwd')
    link = tmp_path / 'link'
    link.symlink_to(folder)
    refused({**good, 'steps': [{**step, 'cwd': str(link)}]}, r'steps\[0\]\.cwd')
    alias = tmp_path / 'alias'
    alias.symlink_to(exe)
    refused({**good, 'steps': [{**step, 'argv': [str(alias)], 'pin': {**step['pin'], 'realpath': str(alias)}}]},
            r'steps\[0\]\.executable: realpath')
    os.chmod(exe, 0o757)
    refused(good, r'steps\[0\]\.executable: mode')
    os.chmod(exe, 0o755)
    exe.unlink()
    refused(good, r'steps\[0\]\.executable: unavailable')


def test_a_later_step_that_cannot_exec_is_reported_with_the_earlier_ones_started(tmp_path):
    """plan_runner.start outside any sandbox: the test reaps the children it forked, by PID, before returning."""
    out = os.open(os.devnull, os.O_WRONLY)
    spec = {'mode': 'plan', 'single': False, 'pty': False, 'steps': [
        step_spec(argv=['/bin/echo', 'first'], cwd='/', env={}),
        step_spec(argv=[str(tmp_path / 'missing-tool')], cwd='/', env={})]}
    pids, firsts = [], []
    try:
        with pytest.raises(plan_runner.StepFailed) as caught:
            plan_runner.start(spec, out, None, pids, on_first=lambda: firsts.append(True))
        assert caught.value.index == 1 and firsts == [True] and len(pids) == 2
    finally:
        os.close(out)
        for pid in pids:
            os.waitpid(pid, 0)  # our own children, unreaped until here


# --- executable drift ------------------------------------------------------------------------

def copied_tool(tmp_path):
    """The python.org interpreter copied into its own toolchain (a copied Apple platform binary such as
    /bin/echo is not used: launch constraints stop it outside its system location)."""
    folder = tmp_path / 'tc' / 'bin'
    folder.mkdir(parents=True)
    exe = folder / 'tool3'
    shutil.copyfile(PY, exe)
    os.chmod(exe, 0o755)
    return exe.resolve()


def flip(exe):
    info, data = exe.stat(), bytearray(exe.read_bytes())
    data[-1] ^= 0xFF  # same size and mtime: only the content hash can tell
    exe.write_bytes(bytes(data))
    os.utime(exe, ns=(info.st_atime_ns, info.st_mtime_ns))


@macos
def test_drift_before_launch_and_at_the_anchor_refuses_before_anything_runs(project, scoped, tmp_path, monkeypatch):
    exe = copied_tool(tmp_path)
    plan = command_plans.from_argv([{'argv': [str(exe), '-c', 'print("RAN")']}], str(project))
    result, out, _, _ = run(plan)
    assert (result['state'], out) == ('succeeded', b'RAN\n'), result
    flip(exe)
    written = len(scoped)
    with pytest.raises(PolicyError, match=r'\(steps\[0\]\.executable: sha256\).*Nothing ran'):
        run(plan)
    assert len(scoped) == written  # refused by the host's re-pin: no supervisor was launched at all
    # A swap after the host's check: the anchor's own re-check, right before exec, refuses it.
    monkeypatch.setattr(async_terminal, '_verify_plan', lambda plan, loopback: command_plans.validate(plan))
    jobdir = tmp_path / 'jobs' / 'drift'
    started = []
    with pytest.raises(PolicyError, match=r'changed after review \(steps\[0\]\.executable: sha256\).*Nothing ran'):
        async_terminal.run_plan(plan, threading.Event(), started.append, lambda chunk: None, launcher=LAUNCHER,
                                jobdir=jobdir)
    assert started == [] and kinds(jobdir) == ['spawning', 'spawned', 'exited', 'teardown']
    record = exited(jobdir)
    assert record['reason'] == 'plan_refused:steps[0].executable: sha256' and record['contained'] is True
    assert job_shim.attach(jobdir).read_spool() == b''
    assert len(scoped) == written + 1


# --- destination-scoped network (X04) --------------------------------------------------------

def certificates(folder):
    folder.mkdir(parents=True)
    (folder / 'ca.cnf').write_text('[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = Pebble W8 Fixture CA\n'
                                   '[v3_ca]\nbasicConstraints = critical,CA:TRUE\nkeyUsage = critical,keyCertSign,cRLSign\n'
                                   'subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid:always\n')
    (folder / 'leaf.cnf').write_text('[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = api.pebble.test\n'
                                     '[v3_leaf]\nbasicConstraints = critical,CA:FALSE\n'
                                     'keyUsage = critical,digitalSignature,keyEncipherment\nextendedKeyUsage = serverAuth\n'
                                     'subjectAltName = DNS:api.pebble.test\nsubjectKeyIdentifier = hash\n'
                                     'authorityKeyIdentifier = keyid\n')
    openssl = '/usr/bin/openssl'
    for argv in ([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '2', '-keyout', 'ca.key',
                  '-out', 'ca.pem', '-config', 'ca.cnf', '-extensions', 'v3_ca'],
                 [openssl, 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'leaf.key', '-out', 'leaf.csr',
                  '-config', 'leaf.cnf'],
                 [openssl, 'x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-set_serial', '4848',
                  '-days', '2', '-sha256', '-extfile', 'leaf.cnf', '-extensions', 'v3_leaf', '-out', 'leaf.pem']):
        result = subprocess.run(argv, cwd=folder, capture_output=True, text=True, timeout=60,
                                env={'PATH': '/usr/bin:/bin', 'HOME': str(folder), 'RANDFILE': str(folder / '.rnd')})
        assert result.returncode == 0, result.stderr
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / 'leaf.pem', folder / 'leaf.key')
    return SimpleNamespace(ca=str(folder / 'ca.pem'), context=context)


@pytest.fixture(scope='module')
def tls(tmp_path_factory):
    return certificates(tmp_path_factory.mktemp('w8tls').resolve() / 'pki')


class Upstream:
    """A loopback HTTPS fixture (W3's shape): records each request; /v1/items answers whether the credential arrived."""

    def __init__(self, context):
        self.context, self.requests = context, []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(16)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(10)
        try:
            secure = self.context.wrap_socket(conn, server_side=True)
        except OSError:
            conn.close()
            return
        try:
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = secure.recv(65536)
                if not chunk:
                    return
                data += chunk
            lines = data.partition(b'\r\n\r\n')[0].decode('latin-1').split('\r\n')
            method, target, _ = lines[0].split(' ')
            headers = [(k.strip().lower(), v.strip()) for k, v in (line.split(':', 1) for line in lines[1:])]
            self.requests.append({'method': method, 'target': target, 'headers': headers})
            authorized = ('authorization', f'Bearer {SECRET}') in headers
            body = b'auth=yes' if authorized and target == '/v1/items' else b'auth=no'
            secure.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(body) + body)
        except OSError:
            pass
        finally:
            secure.close()

    def close(self):
        self.stop.set()
        self.sock.close()


@pytest.fixture
def upstream(tls):
    server = Upstream(tls.context)
    yield server
    server.close()


# The test launcher: in the shim process only, W3's resolve seam answers *.pebble.test with the loopback
# fixture and port 443 is mapped to the fixture's port. Route admission, credential injection and TLS
# verification (SNI api.pebble.test against the fixture CA) are the production code.
NETBOOT = '''import sys
a = sys.argv
if '--job-shim' in a:
    from pebble import egress
    real = egress.EgressBroker._connect
    egress.resolve = lambda host, port: ['127.0.0.1'] if host.endswith('.pebble.test') else []
    egress.EgressBroker._connect = lambda self, ident, addresses, port: real(self, ident, addresses, %d if port == 443 else port)
    if %r:
        egress.EgressBroker.start = lambda self: (_ for _ in ()).throw(OSError('fixture: the broker cannot listen'))
    from pebble import job_shim
    sys.exit(job_shim.main(a))
from pebble import containment
sys.exit(containment.anchor_main(a))
'''


def netboot(port, broken=False):
    return [str(PY), '-c', NETBOOT % (port, broken)], LAUNCHER[1]


CREDENTIAL = {'ref': 'vault:api', 'destination': ['api.pebble.test', 443], 'methods': ['GET'], 'path_prefix': '/v1/'}
NETWORK = {'mode': 'broker', 'destinations': [['api.pebble.test', 443]], 'credentials': [CREDENTIAL]}
CLIENT = r'''import json, os, socket, sys, time
from urllib.parse import urlsplit
def ask(host, port, raw):
    s = socket.create_connection((host, port), timeout=10)
    s.sendall(raw)
    data = b''
    while chunk := s.recv(65536):
        data += chunk
    s.close()
    head, _, body = data.partition(b'\r\n\r\n')
    return [int(head.split(b' ')[1]), body.decode('latin-1')]
route = urlsplit(os.environ['PEBBLE_ROUTE_VAULT_API'])
out = {'argv': sys.argv, 'env': dict(os.environ), 'route_url': os.environ['PEBBLE_ROUTE_VAULT_API']}
out['route'] = ask(route.hostname, route.port, f'GET {route.path}/v1/items HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer forged\r\n\r\n'.encode())
out['outside_prefix'] = ask(route.hostname, route.port, f'GET {route.path}/v2/items HTTP/1.1\r\nHost: x\r\n\r\n'.encode())
proxy = urlsplit(os.environ['HTTPS_PROXY'])
s = socket.create_connection((proxy.hostname, proxy.port), timeout=10)
s.sendall(b'CONNECT evil.pebble.test:443 HTTP/1.1\r\nHost: evil.pebble.test:443\r\n\r\n')
out['connect_unlisted'] = int(s.recv(64).split(b' ')[1])
s.close()
try:
    socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=5).close()
    out['direct'] = 'connected'
except OSError as exc:
    out['direct'] = exc.errno
home = os.environ['HOME']
open(os.path.join(home, 'ready'), 'w').write(str(os.getpid()))
for _ in range(600):
    if os.path.exists(os.path.join(home, 'go')):
        break
    time.sleep(0.05)
print(json.dumps(out))
'''
PEEK = r'''import ctypes, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                        ctypes.c_void_p, ctypes.c_size_t]
for pid in sys.argv[1:]:
    mib = (ctypes.c_int * 3)(1, 49, int(pid))  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(262144)
    buf = ctypes.create_string_buffer(262144)
    ok = libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) == 0
    print(pid, buf.raw[:size.value].hex() if ok else 'errno-%d' % ctypes.get_errno())
'''


class Vault:
    """The parent's fetch(ref) -> bytearray stand-in; keeps every buffer it handed out."""

    def __init__(self, value=SECRET, fail=False):
        self.value, self.fail, self.calls, self.given = value, fail, [], []

    def __call__(self, ref):
        self.calls.append(ref)
        if self.fail:
            raise RuntimeError('vault unavailable: ' + SECRET)  # an error that quotes the secret
        self.given.append(bytearray(self.value, 'ascii'))
        return self.given[-1]


def files_under(folder):
    return [p for p in Path(folder).rglob('*') if p.is_file() and not p.is_symlink()]


@macos
def test_broker_route_reaches_only_its_origin_and_the_credential_stays_in_shim_memory(project, scoped, tmp_path, upstream,
                                                                                      tls, monkeypatch):
    script(project, 'client.py', CLIENT)
    script(project, 'peek.py', PEEK)
    plan = command_plans.from_argv([{'argv': ['python3', 'client.py', str(upstream.port)]}], str(project),
                                   network=NETWORK)
    shims, real_popen = [], subprocess.Popen

    def popen(args, *rest, **kwargs):
        proc = real_popen(args, *rest, **kwargs)
        if '--job-shim' in list(args):
            shims.append(proc)
        return proc
    monkeypatch.setattr(subprocess, 'Popen', popen)
    vault, jobdir = Vault(), tmp_path / 'jobs' / 'net'
    home = jobdir.with_name('net-home')
    outcome = {}

    def work():
        try:
            outcome['value'] = run(plan, launcher=netboot(upstream.port), jobdir=jobdir, fetch=vault, loopback=True,
                                   ca_file=tls.ca)
        except BaseException as exc:
            outcome['error'] = exc
    thread = threading.Thread(target=work)
    thread.start()
    deadline = time.monotonic() + 60
    while not (home / 'ready').exists() and time.monotonic() < deadline and thread.is_alive():
        time.sleep(0.05)
    assert (home / 'ready').exists(), (outcome, kinds(jobdir))
    # A sibling confined job reads the shim's, the anchor's and the step's KERN_PROCARGS2 while they run.
    anchor = next(r for r in job_shim.read_records(jobdir) if r['record'] == 'spawned')['anchor_pid']
    pids = [shims[0].pid, anchor, int((home / 'ready').read_text())]
    peek = command_plans.from_argv([{'argv': ['python3', 'peek.py', *map(str, pids)]}], str(project))
    peeked, peek_out, _, _ = run(peek)
    (home / 'go').write_text('go')
    thread.join(60)
    assert 'error' not in outcome, outcome
    result, out, started, receipt = outcome['value']
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('succeeded', 0, False), out
    assert result['isolation'].endswith('reviewed destinations only')
    facts = json.loads(out)
    assert facts['route'] == [200, 'auth=yes']  # the reviewed origin got the real credential, not the forged one
    assert facts['outside_prefix'][0] == 403 and facts['connect_unlisted'] == 403
    assert facts['direct'] == 1  # EPERM: only the broker's port is reachable
    assert facts['route_url'].startswith('http://127.0.0.1:') and facts['route_url'].endswith('/r/vault-api')
    assert [r['target'] for r in upstream.requests] == ['/v1/items']  # nothing outside the prefix left the broker
    assert vault.calls == ['vault:api'] and all(not any(buf) for buf in vault.given)  # fetched once, then zeroed
    # The receipt: route records with a path hash, never a path or the credential.
    log = receipt['egress_log']
    routed = [r for r in log if r.get('route') == 'vault-api' and r.get('status') == 200]
    assert routed and routed[0]['credential_used'] is True and routed[0]['allowed'] is True
    assert routed[0]['path_hash'] == hashlib.sha256(b'/v1/items').hexdigest()
    assert any(r.get('host') == 'evil.pebble.test' and r.get('allowed') is False for r in log)
    assert all('path' not in r for r in log)
    assert job_shim.attach(jobdir).egress_record() == log  # a restarted host reads the same log from the records
    slotted = json.loads((jobdir / 'plan.json').read_text())['profile']
    spawned = next(r for r in job_shim.read_records(jobdir) if r['record'] == 'spawned')
    assert re.fullmatch(r'[0-9a-f]{64}', spawned['profile_sha256'])
    assert spawned['profile_sha256'] != hashlib.sha256(slotted.encode()).hexdigest()  # the broker port was bound
    # The secret is nowhere a job, a sibling or the evidence could read.
    secret = SECRET.encode()
    assert SECRET not in json.dumps(facts) and secret not in out
    for path in files_under(jobdir) + files_under(home):
        assert secret not in path.read_bytes(), path
    assert peeked['state'] == 'succeeded', peek_out
    views = dict(line.split(' ', 1) for line in peek_out.decode().splitlines())
    seen = {pid: b'' if views[str(pid)].startswith('errno-') else bytes.fromhex(views[str(pid)]) for pid in pids}
    # The probe works (F2): the unsandboxed shim's argument area is readable from a confined sibling.
    assert b'--job-shim' in seen[pids[0]], views[str(pids[0])][:40]
    for pid, marker in zip(pids[1:], (b'--job-anchor', b'client.py')):
        assert seen[pid] == b'' or marker in seen[pid], (pid, views[str(pid)][:40])
    assert all(secret not in view for view in seen.values())
    assert len(scoped) == 2


@macos
def test_credentials_that_cannot_be_fetched_or_granted_leave_the_job_unstarted(project, scoped, tmp_path, upstream, tls):
    script(project, 'client.py', CLIENT)
    plan = command_plans.from_argv([{'argv': ['python3', 'client.py', '1']}], str(project), network=NETWORK)
    with pytest.raises(PolicyError, match='no credential source'):
        run(plan, launcher=netboot(upstream.port), loopback=True, ca_file=tls.ca)  # no fetch: refused before launch
    cases = ((Vault(fail=True), 'failed'), (Vault(value='has space'), 'unusable'), (Vault(value='x' * 9000), 'large'))
    for index, (vault, label) in enumerate(cases):
        jobdir = tmp_path / 'jobs' / f'fetch{index}'
        with pytest.raises(PolicyError, match='credential could not be fetched or granted. Nothing ran') as caught:
            run(plan, launcher=netboot(upstream.port), jobdir=jobdir, fetch=vault, loopback=True, ca_file=tls.ca)
        assert SECRET not in str(caught.value) and caught.value.__cause__ is None, label
        assert kinds(jobdir) == ['spawning', 'exited', 'teardown'], label  # the anchor never started
        assert exited(jobdir)['reason'] == 'anchor_spawn_failed:credentials_unavailable', label
        assert all(not any(buf) for buf in vault.given), label
    assert upstream.requests == []
    assert len(scoped) == 3


@macos
def test_a_broker_that_cannot_start_leaves_the_job_unstarted(project, scoped, tmp_path, upstream, tls):
    script(project, 'client.py', CLIENT)
    plan = command_plans.from_argv([{'argv': ['python3', 'client.py', '1']}], str(project), network=NETWORK)
    jobdir, vault = tmp_path / 'jobs' / 'broker', Vault()
    with pytest.raises(PolicyError, match="network broker could not start. Nothing ran"):
        run(plan, launcher=netboot(upstream.port, broken=True), jobdir=jobdir, fetch=vault, loopback=True,
            ca_file=tls.ca)
    assert kinds(jobdir) == ['spawning', 'exited', 'teardown']
    assert exited(jobdir)['reason'] == 'anchor_spawn_failed:broker_unavailable'
    assert vault.calls == ['vault:api'] and all(not any(buf) for buf in vault.given)
    assert upstream.requests == [] and len(scoped) == 1


def test_route_names_are_stable_distinct_labels():
    refs = [{'ref': r} for r in ('vault:api', 'Vault/API', 'gh.token', '::', 'x' * 60)]
    assert async_terminal.route_names(refs) == ['vault-api', 'vault-api-1', 'gh-token', 'route', 'x' * 29]
    assert all(async_terminal.ROUTE.fullmatch(name) for name in async_terminal.route_names(refs))


# --- admission, the synchronous entry and the legacy guard -----------------------------------

class Host:
    """A real Store, RunJournal, TimeLedger and ProcessJobs with the real (executor) worker."""

    def __init__(self, tmp_path, project, *, fresh=True, resume=True):
        self.home = Path(tmp_path) / 'data'
        self.store = Store(self.home)
        self.runs = RunJournal(self.store, self.home)
        self.ledger = TimeLedger(self.store)
        self.ledger.recover()
        self.events = []
        self.app = SimpleNamespace(home=self.home, store=self.store, runs=self.runs, time_ledger=self.ledger,
                                   active=None, active_run=None, closed=False,
                                   emit=lambda kind, payload: self.events.append((kind, payload)))
        self.ws = Workspace(project)
        self.cid = self.rid = None
        if fresh:
            self.cid = self.store.new_chat(str(self.ws.root))
            self.rid = self.runs.create(self.cid, 'Plan executor fixture', {
                'workspace': str(self.ws.root), 'workspace_identity': list(self.ws.identity),
                'limits': {'max_steps': 8, 'max_seconds': 900.0}})
        self.jobs = ProcessJobs(self.app, resume=resume)
        self.jobs.containment = lambda: {'contained': True, 'reason': 'fixture'}
        self.count = 0

    def payload(self, plan):
        return {**process_jobs.binding(plan), 'plan': plan, 'requires_review': True, 'asynchronous': True}

    def start(self, payload, timeout=60):
        self.count += 1
        call_id = f'{self.count}:call_{uuid.uuid4().hex[:8]}'
        self.runs.begin_tool(self.rid, call_id, 'StartCommand', {'command': payload['command']})
        aid = self.store.action(self.cid, 'command', {**payload, 'timeout': 300, 'permission_mode': 'ask'})
        self.runs.bind_action(self.rid, call_id, aid, 'command')
        self.store.query('UPDATE actions SET status=? WHERE id=?', ('running', aid))
        action = next(a for a in self.store.actions(self.cid) if a['id'] == aid)
        self.app.active, self.app.active_run = self.cid, self.rid
        return self.jobs.start(action, self.cid, payload, timeout=timeout)

    def row(self, process_id):
        return self.store.query('SELECT * FROM process_jobs WHERE id=?', (process_id,))[0]

    def settled(self, process_id, seconds=60):
        end = time.monotonic() + seconds
        while time.monotonic() < end and self.row(process_id)['state'] in ('admitted', 'running'):
            time.sleep(0.05)
        return self.row(process_id)

    def text(self, process_id):
        return b''.join(bytes(r['data']) for r in self.store.query(
            'SELECT data FROM process_output WHERE process_id=? ORDER BY sequence', (process_id,)))

    def close(self):
        self.app.active = self.app.active_run = None
        closed = self.jobs.close()
        self.runs.close()
        self.store.close()
        return closed


@macos
def test_process_jobs_admits_a_pipeline_plan_and_settles_it_from_the_contained_sweep(project, scoped, tmp_path):
    for name, text in (('gen.py', GEN), ('upper.py', UPPER)):
        script(project, name, text)
    (project / 'build').mkdir()
    plan = command_plans.from_argv([{'argv': ['python3', 'gen.py']}, {'argv': ['python3', 'upper.py']}],
                                   str(project), pipeline='linear', writable=['build'], process_model='single')
    host = Host(tmp_path, project)
    try:
        assert host.jobs._executor is True
        payload = {**host.payload(plan), 'writable': True}
        assert payload['command'] == 'plan [["python3","gen.py"],["python3","upper.py"]]'
        view = host.start(payload)
        row = host.settled(view['process_id'])
        assert (row['state'], row['exit_code'], row['outcome_uncertain'], row['contained'], row['settled_by']) == (
            'succeeded', 0, 0, 1, 'worker'), (row['reason'], kinds(Path(row['jobdir'])))
        assert b'B\nA\nC\n' in host.text(view['process_id']) and row['plan_hash'] == plan['plan_hash']
        assert 'started' in kinds(Path(row['jobdir']))
        # A credential plan without the vault callback is refused at admission; nothing is recorded.
        script(project, 'client.py', CLIENT)
        networked = command_plans.from_argv([{'argv': ['python3', 'client.py', '1']}], str(project), network=NETWORK)
        before = host.store.query('SELECT count(*) AS n FROM process_jobs')
        with pytest.raises(PolicyError, match='no credential source'):
            host.start(host.payload(networked))
        assert host.store.query('SELECT count(*) AS n FROM process_jobs') == before
        assert host.jobs.egress_log(host.cid, host.rid, view['process_id'])['log'] == []
    finally:
        assert host.close() is True
    assert len(scoped) == 1


@macos
def test_synchronous_entry_returns_the_terminal_result_shape(project, scoped):
    for name, text in (('gen.py', GEN), ('upper.py', UPPER)):
        script(project, name, text)
    script(project, 'slow.py', 'import time\nprint("slow", flush=True)\ntime.sleep(30)\n')
    plan = command_plans.from_argv([{'argv': ['python3', 'gen.py']}, {'argv': ['python3', 'upper.py']}],
                                   str(project), pipeline='linear')
    payload = {**process_jobs.binding(plan), 'plan': plan}
    result = async_terminal.run_plan_command(payload, threading.Event(), launcher=LAUNCHER)
    assert set(result) == {'exit_code', 'output', 'output_projection', 'controls_removed', 'raw_output_sha256', 'command',
                           'isolation', 'plan_hash', 'step_codes', 'egress_log'}
    assert result['exit_code'] == 0 and 'B\nA\nC\n' in result['output'] and result['step_codes'] == [0, 0]
    assert result['command'] == payload['command'] and result['egress_log'] == []
    slow = command_plans.from_argv([{'argv': ['python3', 'slow.py']}], str(project), resources={'timeout': 2})
    with pytest.raises(CommandOutcomeUncertain):
        async_terminal.run_plan_command({**process_jobs.binding(slow), 'plan': slow}, threading.Event(),
                                        launcher=LAUNCHER)
    interactive = command_plans.from_argv([{'argv': ['python3', 'slow.py'], 'stdin': 'pty'}], str(project),
                                          interactive=True)
    with pytest.raises(PolicyError, match='background process'):
        async_terminal.run_plan_command({**process_jobs.binding(interactive), 'plan': interactive}, threading.Event(),
                                        launcher=LAUNCHER)
    with pytest.raises(PolicyError, match='changed after review'):
        async_terminal.run_plan_command({**payload, 'argv': ['python3', 'upper.py']}, threading.Event(),
                                        launcher=LAUNCHER)
    assert len(scoped) == 2


def test_the_owned_group_path_refuses_plans_it_cannot_honour(project):
    script(project, 'gen.py', GEN)
    (project / 'sub').mkdir()
    script(project / 'sub', 'gen.py', GEN)
    for plan in (command_plans.from_argv([{'argv': ['python3', 'gen.py']}, {'argv': ['sort']}], str(project),
                                         pipeline='linear'),
                 command_plans.from_argv([{'argv': ['python3', 'gen.py'], 'cwd': 'sub'}], str(project)),
                 command_plans.from_argv([{'argv': ['python3', 'gen.py']}], str(project), process_model='single'),
                 command_plans.from_argv([{'argv': ['python3', 'gen.py'], 'env': {'CI': '1'}}], str(project))):
        payload = {**process_jobs.binding(plan), 'plan': plan, 'asynchronous': True}
        assert async_terminal.needs_plan_executor(payload) is True
        with pytest.raises(PolicyError, match='contained plan worker'):
            async_terminal.run_async_command(payload, threading.Event(), lambda info: None, lambda chunk: None)
    simple = command_plans.from_argv([{'argv': ['python3', 'gen.py']}], str(project))
    assert async_terminal.needs_plan_executor({**process_jobs.binding(simple), 'plan': simple}) is False
    assert async_terminal.needs_plan_executor({'command': 'ls'}) is False
    with pytest.raises(PolicyError, match='Typed command plans run only under the containment supervisor'):
        async_terminal.run_plan(plan, threading.Event(), lambda info: None, lambda chunk: None, launcher=None)


@macos
def test_the_owned_group_path_runs_only_the_reviewed_pin(project, tmp_path):
    """A simple plan on the legacy path is re-pinned before spawn; drift refuses with nothing run."""
    exe = copied_tool(tmp_path)
    plan = command_plans.from_argv([{'argv': [str(exe), '-c', 'print("RAN")']}], str(project))
    payload = {**process_jobs.binding(plan), 'plan': plan, 'asynchronous': True}
    flip(exe)
    started = []
    with pytest.raises(PolicyError, match=r'steps\[0\]\.executable: sha256.*Nothing ran'):
        async_terminal.run_async_command(payload, threading.Event(), started.append, lambda chunk: None)
    assert started == []


def test_new_executor_modules_never_signal():
    """plan_runner.py and pty_input.py signal nothing: every stop is the anchor's instance sweep."""
    banned = {'kill', 'killpg', 'send_signal', 'terminate', '__kill', 'pthread_kill', 'sigqueue'}
    for name in ('plan_runner', 'pty_input'):
        tree = ast.parse((ROOT / 'backend' / 'pebble' / (name + '.py')).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', None)
                assert called not in banned, f'{name}: signal call {called} at line {node.lineno}'
