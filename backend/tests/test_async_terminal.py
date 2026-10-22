"""Owned async terminal: exact admission, actual macOS confinement and lifecycle."""
import ctypes
import errno
import io
import os
import shutil
import signal
import socket
import struct
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from pebble import async_terminal
from pebble.async_terminal import run_async_command
from pebble.policy import PolicyError
from pebble.terminal import inspect_command

macos = pytest.mark.skipif(
    sys.platform != 'darwin' or not async_terminal.SANDBOX.is_file(), reason='actual macOS confinement'
)
DENIED = 'macOS sandbox · selected project · network denied'
EFFECT = (
    "import time\n"
    "from pathlib import Path\n"
    "Path('effect.txt').write_text('committed by process')\n"
    "print('EFFECT', flush=True)\n"
    "time.sleep(60)\n"
    "Path('late.txt').write_text('never')\n"
)


class Recorder:
    def __init__(self, stop_on=None):
        self.cancel = threading.Event()
        self.stop_on = stop_on
        self.started, self.chunks, self.order = [], [], []

    def on_started(self, info):
        self.order.append('started')
        self.started.append(dict(info))

    def on_output(self, chunk):
        self.order.append('output')
        self.chunks.append(chunk)
        if self.stop_on is not None and self.stop_on in self.output:
            self.cancel.set()

    @property
    def output(self):
        return b''.join(self.chunks)

    @property
    def pid(self):
        return self.started[0]['pid']


def project(tmp_path, files=(), name='project'):
    root = tmp_path / name
    root.mkdir()
    for path, text in dict(files).items():
        (root / path).write_text(text, encoding='utf-8')
    return root


def reviewed(root, command='python3 probe.py', network=False, writable=True):
    return {**inspect_command(command, root, network), 'writable': writable}


def execute(payload, recorder=None, **limits):
    recorder = recorder or Recorder()
    limits.setdefault('timeout', 60)
    result = run_async_command(payload, recorder.cancel, recorder.on_started, recorder.on_output, **limits)
    return result, recorder


def refuse_spawn(*args, **kwargs):
    raise AssertionError('admission must fail before any process starts')


def spawner(after='', beats=None, escape=False):
    """A leader that starts one heartbeat descendant, then runs `after`."""
    loop = f'for _ in range({beats})' if beats else 'while True'
    beat = f"import time\n{loop}:\n    open('beat.txt', 'a').write('.')\n    time.sleep(0.05)\n"
    streams = '' if escape else ', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL'
    return (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {beat!r}]{streams}, start_new_session={escape})\n"
        "Path('child.pid').write_text(str(child.pid))\n"
        "for _ in range(500):\n"
        "    if Path('beat.txt').exists():\n"
        "        break\n"
        "    time.sleep(0.01)\n"
        "print('READY', flush=True)\n"
        f"{after}\n"
    )


def assert_contract(result, recorder):
    assert set(result) == {'state', 'exit_code', 'isolation', 'outcome_uncertain'}
    assert result['state'] in {'succeeded', 'failed', 'cancelled', 'timed_out', 'output_limited', 'unknown'}
    assert type(result['outcome_uncertain']) is bool
    assert result['exit_code'] is None or type(result['exit_code']) is int
    assert recorder.order[:1] == ['started'] and recorder.order.count('started') == 1
    assert set(recorder.started[0]) == {'pid', 'isolation'} and type(recorder.pid) is int
    # The control PID stays with the manager; the result carries none.
    assert recorder.started[0]['isolation'] == result['isolation'] and str(recorder.pid) not in result['isolation']
    assert all(type(chunk) is bytes and 0 < len(chunk) <= async_terminal.CHUNK_BYTES for chunk in recorder.chunks)


def assert_reaped(pid):
    # No longer a child of this process: reaped, never left as a zombie.
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def assert_quiet(path, seconds=0.4):
    before = path.stat().st_size if path.exists() else None
    time.sleep(seconds)
    assert (path.stat().st_size if path.exists() else None) == before, 'a descendant outlived teardown'


def assert_gone(pid, seconds=3):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            os.kill(pid, 0)  # Probe only; launchd reaps orphaned zombies.
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    pytest.fail(f'descendant {pid} outlived teardown')


def join_late_reapers(seconds=10):
    with async_terminal._late_lock:
        threads = set(async_terminal._late_reapers)
    for thread in threads:
        thread.join(timeout=seconds)
    assert not any(thread.is_alive() for thread in threads)


# Admission: exact limits, reviewed payload and platform, all before spawn.

@pytest.mark.parametrize('limits', [
    {'timeout': 0}, {'timeout': 0.5}, {'timeout': 901}, {'timeout': float('inf')}, {'timeout': float('nan')},
    {'timeout': True}, {'timeout': '30'}, {'timeout': None},
    {'max_output': 0}, {'max_output': 192_001}, {'max_output': 1.5}, {'max_output': True}, {'max_output': -1},
], ids=repr)
def test_limits_are_exact_and_checked_before_any_spawn(tmp_path, monkeypatch, limits):
    root = project(tmp_path, {'a.txt': 'x'})
    monkeypatch.setattr(async_terminal.subprocess, 'Popen', refuse_spawn)
    recorder = Recorder()
    with pytest.raises(PolicyError, match='timeout|output limit'):
        execute(reviewed(root, 'cat a.txt', writable=False), recorder, **limits)
    assert recorder.order == []


@pytest.mark.parametrize('limits', [
    {'timeout': 1}, {'timeout': 900}, {'timeout': 2.5}, {'max_output': 1}, {'max_output': 192_000},
], ids=repr)
def test_boundary_limits_are_admitted_and_pre_spawn_cancel_runs_nothing(tmp_path, monkeypatch, limits):
    root = project(tmp_path, {'a.txt': 'x'})
    monkeypatch.setattr(async_terminal, '_supported', lambda: True)
    monkeypatch.setattr(async_terminal.subprocess, 'Popen', refuse_spawn)
    recorder = Recorder()
    recorder.cancel.set()
    result, _ = execute(reviewed(root, 'cat a.txt', writable=False), recorder, **limits)
    assert result == {'state': 'cancelled', 'exit_code': None, 'isolation': DENIED, 'outcome_uncertain': False}
    assert recorder.order == []


@pytest.mark.parametrize('missing', ['cancel', 'on_started', 'on_output'])
def test_callbacks_and_cancellation_are_required(tmp_path, monkeypatch, missing):
    root = project(tmp_path, {'a.txt': 'x'})
    monkeypatch.setattr(async_terminal.subprocess, 'Popen', refuse_spawn)
    arguments = {'cancel': threading.Event(), 'on_started': lambda info: None, 'on_output': lambda chunk: None}
    arguments[missing] = object()
    with pytest.raises(PolicyError, match='callbacks'):
        run_async_command(reviewed(root, 'cat a.txt', writable=False), arguments['cancel'],
                          arguments['on_started'], arguments['on_output'])


def without(key):
    return lambda payload: {name: value for name, value in payload.items() if name != key}


DRIFT = {
    'argv': (lambda p: {**p, 'argv': ['python3', 'other.py']}, 'changed after review'),
    'command': (lambda p: {**p, 'command': 'python3 other.py'}, 'changed after review'),
    'network-escalated': (lambda p: {**p, 'network': True}, 'changed after review'),
    'risk-missing': (without('risk'), 'changed after review'),
    'network-type': (lambda p: {**p, 'network': 'yes'}, 'explicit'),
    'writable-type': (lambda p: {**p, 'writable': 1}, 'explicit'),
    'identity-missing': (without('root_identity'), 'identity is missing'),
    'identity-not-int': (lambda p: {**p, 'root_identity': [True, False]}, 'identity is missing'),
    'identity-forged': (lambda p: {**p, 'root_identity': [0, 0]}, 'replaced'),
    'root-alias': (lambda p: {**p, 'root': p['root'] + '/.'}, 'replaced'),
    'shell': (lambda p: {**p, 'command': 'sh -c id', 'argv': ['sh', '-c', 'id']}, 'shell wrappers'),
    'chained': (lambda p: {**p, 'command': 'python3 probe.py; rm -rf docs'}, 'one command at a time'),
    'credential': (lambda p: {**p, 'command': 'cat .env', 'argv': ['cat', '.env']}, 'Credential access'),
    'git': (lambda p: {**p, 'command': 'git status', 'argv': ['git', 'status']}, 'typed Git'),
    'not-a-dict': (lambda p: list(p.items()), 'reviewed command is required'),
}


@pytest.mark.parametrize('name', sorted(DRIFT))
def test_reviewed_payload_drift_fails_closed_before_spawn(tmp_path, monkeypatch, name):
    change, message = DRIFT[name]
    root = project(tmp_path, {'probe.py': "print('ran')\n"})
    monkeypatch.setattr(async_terminal.subprocess, 'Popen', refuse_spawn)
    recorder = Recorder()
    with pytest.raises(PolicyError, match=message):
        execute(change(reviewed(root)), recorder)
    assert recorder.order == []


def test_read_only_review_cannot_gain_write_scope(tmp_path, monkeypatch):
    root = project(tmp_path, {'a.txt': 'x'})
    monkeypatch.setattr(async_terminal.subprocess, 'Popen', refuse_spawn)
    with pytest.raises(PolicyError, match='write scope'):
        execute(reviewed(root, 'cat a.txt', writable=True))


def test_missing_project_folder_fails_closed(tmp_path, monkeypatch):
    root = project(tmp_path, {'probe.py': "print('ran')\n"})
    payload = reviewed(root)
    shutil.rmtree(root)
    monkeypatch.setattr(async_terminal.subprocess, 'Popen', refuse_spawn)
    with pytest.raises(PolicyError, match='unavailable'):
        execute(payload)


def test_replaced_project_folder_is_refused_before_anything_runs(tmp_path):
    probe = "from pathlib import Path\nPath('ran.txt').write_text('ran')\n"
    root = project(tmp_path, {'probe.py': probe})
    payload = reviewed(root)
    root.rename(tmp_path / 'reviewed-original')
    project(tmp_path, {'probe.py': probe})  # Same path, a different folder.
    recorder = Recorder()
    with pytest.raises(PolicyError, match='replaced'):
        execute(payload, recorder)
    assert recorder.order == []
    assert not (root / 'ran.txt').exists() and not (tmp_path / 'reviewed-original' / 'ran.txt').exists()


@pytest.mark.parametrize('gap', ['platform', 'sandbox-exec', 'kqueue', 'sigchld-ignored', 'libproc'])
def test_unsupported_platform_fails_closed_before_spawn(tmp_path, monkeypatch, gap):
    root = project(tmp_path, {'a.txt': 'x'})
    payload = reviewed(root, 'cat a.txt', writable=False)
    monkeypatch.setattr(async_terminal.subprocess, 'Popen', refuse_spawn)
    if gap == 'platform':
        monkeypatch.setattr(sys, 'platform', 'win32')
    elif gap == 'sandbox-exec':
        monkeypatch.setattr(async_terminal, 'SANDBOX', tmp_path / 'missing' / 'sandbox-exec')
    elif gap == 'kqueue':
        monkeypatch.delattr(async_terminal.select, 'kqueue', raising=False)
    elif gap == 'libproc':
        # Without libproc, group death can only be inferred from killpg's
        # ambiguous EPERM, so the command is refused rather than run blind.
        monkeypatch.setattr(async_terminal, '_libproc_handle', lambda: None)
    else:
        # An ignored SIGCHLD auto-reaps children and would break the leader hold.
        monkeypatch.setattr(async_terminal.signal, 'getsignal', lambda signum: signal.SIG_IGN)
    recorder = Recorder()
    with pytest.raises(PolicyError, match='requires macOS'):
        execute(payload, recorder)
    assert recorder.order == []


def test_spawn_failure_is_an_admission_failure_with_no_start(tmp_path, monkeypatch):
    root = project(tmp_path, {'a.txt': 'x'})
    monkeypatch.setattr(async_terminal, '_supported', lambda: True)

    def missing(*args, **kwargs):
        raise FileNotFoundError(errno.ENOENT, 'sandbox-exec is missing')

    monkeypatch.setattr(async_terminal.subprocess, 'Popen', missing)
    recorder = Recorder()
    with pytest.raises(PolicyError, match='nothing ran'):
        execute(reviewed(root, 'cat a.txt', writable=False), recorder)
    assert recorder.order == []


# Byte transport: bounded chunks, UTF-8 boundaries, exact limit, captured callback failures.

@pytest.mark.parametrize('data, held', [
    (b'', 0), (b'a', 0), (b'\xc3', 1), (b'\xc3\xa9', 0), (b'a\xe2\x82', 2), (b'\xe2\x82\xac', 0),
    (b'\xf0\x9f\x98', 3), (b'\xf0\x9f\x98\x80', 0), (b'\x80\x80\x80', 0), (b'\xff', 0),
])
def test_partial_utf8_suffix_detection(data, held):
    assert async_terminal._partial_utf8(data) == held


def test_output_chunks_are_bounded_exact_and_never_split_utf8():
    chunks = []
    output = async_terminal._Output(chunks.append, async_terminal.OUTPUT_LIMIT)
    data = ('a' + 'é€😀' * 3000).encode()
    sizes, start, turn = (1, 2, 3, 5, 4093, 2048, 7), 0, 0
    while start < len(data):
        size = sizes[turn % len(sizes)]
        output.accept(data[start:start + size])
        start, turn = start + size, turn + 1
    output.flush()
    assert b''.join(chunks) == data
    assert all(0 < len(chunk) <= async_terminal.CHUNK_BYTES for chunk in chunks)
    assert [chunk.decode('utf-8') for chunk in chunks]  # Strict: no chunk splits a sequence.
    assert not output.limited and output.failure is None


def test_output_limit_is_exact_and_later_bytes_are_dropped():
    chunks = []
    output = async_terminal._Output(chunks.append, 10)
    output.accept(b'0123456789')
    assert not output.limited and b''.join(chunks) == b'0123456789'
    output.accept(b'X')
    output.accept(b'more')
    output.flush()
    assert output.limited and b''.join(chunks) == b'0123456789'


def test_output_limit_delivers_an_exact_prefix_even_inside_a_sequence():
    chunks = []
    output = async_terminal._Output(chunks.append, 4)
    output.accept('ab€'.encode())
    assert output.limited and b''.join(chunks) == b'ab\xe2\x82'


def test_output_callback_failures_are_captured_for_teardown():
    calls = []

    def broken(chunk):
        calls.append(chunk)
        raise KeyboardInterrupt

    output = async_terminal._Output(broken, 100)
    output.accept(b'one')
    output.accept(b'two')
    assert calls == [b'one'] and isinstance(output.failure, KeyboardInterrupt)


# Actual macOS confinement.

@macos
def test_streams_bounded_output_from_an_isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('PEBBLE_ASYNC_SECRET', 'inherited-secret')
    root = project(tmp_path, {'probe.py': (
        "import os, sys\n"
        "print('STDIN', repr(sys.stdin.read()))\n"
        "print('HOME', os.environ['HOME'])\n"
        "print('SECRET', os.environ.get('PEBBLE_ASYNC_SECRET'))\n"
        "print('Z' * 5000)\n"
    )})
    result, recorder = execute(reviewed(root, writable=False))
    assert_contract(result, recorder)
    assert result == {'state': 'succeeded', 'exit_code': 0, 'isolation': DENIED, 'outcome_uncertain': False}
    text = recorder.output.decode()
    assert "STDIN ''" in text and 'SECRET None' in text and 'inherited-secret' not in text
    home = Path(text.split('HOME ', 1)[1].splitlines()[0])
    assert home != Path.home() and home.name.startswith('pebble-command-') and not home.exists()
    assert 'Z' * 5000 in text and len(recorder.chunks) >= 3
    assert_reaped(recorder.pid)
    assert not async_terminal._late_reapers


@macos
def test_nonzero_exit_is_an_observed_failure(tmp_path):
    root = project(tmp_path, {'probe.py': "print('partial', flush=True)\nraise SystemExit(3)\n"})
    result, recorder = execute(reviewed(root, writable=False))
    assert_contract(result, recorder)
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('failed', 3, False)
    assert b'partial' in recorder.output
    assert_reaped(recorder.pid)


@macos
@pytest.mark.parametrize('writable', [False, True], ids=['read-only', 'writable'])
def test_confinement_denies_outside_credentials_and_git_metadata(tmp_path, writable):
    outside = tmp_path / 'outside.txt'
    outside.write_text('OUTSIDE-SECRET')
    root = project(tmp_path, {'.env': 'API_KEY=env-secret', 'deploy.pem': 'pem-secret'})
    (root / 'secrets').mkdir()
    (root / 'secrets' / 'token.txt').write_text('token-secret')
    (root / '.git').mkdir()
    (root / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
    (root / 'probe.py').write_text(
        "from pathlib import Path\n"
        f"for name in [{str(outside)!r}, '.env', 'secrets/token.txt', 'deploy.pem', '.git/HEAD', '.GIT/HEAD']:\n"
        "    try:\n"
        "        print('LEAK', Path(name).read_text(), flush=True)\n"
        "    except OSError:\n"
        "        print('READ DENIED', name, flush=True)\n"
        f"for name in ['.git/HEAD', '.env', {str(outside)!r}]:\n"
        "    try:\n"
        "        Path(name).write_text('changed')\n"
        "        print('WROTE', name, flush=True)\n"
        "    except OSError:\n"
        "        print('WRITE DENIED', name, flush=True)\n"
        "try:\n"
        "    Path('ordinary.txt').write_text('allowed')\n"
        "    print('ORDINARY WRITTEN')\n"
        "except OSError:\n"
        "    print('ORDINARY DENIED')\n"
    )
    result, recorder = execute(reviewed(root, writable=writable))
    text = recorder.output.decode()
    assert result['state'] == 'succeeded', text
    assert 'LEAK' not in text and text.count('READ DENIED') == 6
    assert 'WROTE' not in text and text.count('WRITE DENIED') == 3
    assert ('ORDINARY WRITTEN' in text) is writable and ('ORDINARY DENIED' in text) is (not writable)
    assert outside.read_text() == 'OUTSIDE-SECRET' and (root / '.env').read_text() == 'API_KEY=env-secret'
    assert (root / '.git' / 'HEAD').read_text() == 'ref: refs/heads/main\n'
    assert (root / 'ordinary.txt').exists() is writable


@macos
@pytest.mark.parametrize('approved', [False, True], ids=['denied', 'approved'])
def test_network_needs_the_reviewed_approval(tmp_path, approved):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        root = project(tmp_path, {'probe.py': (
            "import socket\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {port}), timeout=3).close()\n"
            "    print('CONNECTED')\n"
            "except OSError as exc:\n"
            "    print('NETWORK DENIED', type(exc).__name__)\n"
        )})
        result, recorder = execute(reviewed(root, network=approved, writable=False))
    text = recorder.output.decode()
    assert result['state'] == 'succeeded', text
    assert result['isolation'].endswith('network approved' if approved else 'network denied')
    assert ('CONNECTED' in text) is approved and ('NETWORK DENIED' in text) is (not approved)


@macos
def test_multibyte_output_streams_without_split_sequences(tmp_path):
    root = project(tmp_path, {'probe.py': (
        "import sys\n"
        "data = ('a' + 'é€😀' * 4000).encode()\n"
        "for start in range(0, len(data), 1531):\n"
        "    sys.stdout.buffer.write(data[start:start + 1531])\n"
        "    sys.stdout.buffer.flush()\n"
    )})
    result, recorder = execute(reviewed(root, writable=False))
    assert_contract(result, recorder)
    assert result['state'] == 'succeeded'
    assert recorder.output == ('a' + 'é€😀' * 4000).encode()
    assert [chunk.decode('utf-8') for chunk in recorder.chunks]


# Actual lifecycle: cancellation, timeout, output burst and descendants.

@macos
def test_cancel_preserves_seen_output_and_reports_the_partial_effect(tmp_path):
    root = project(tmp_path, {'probe.py': EFFECT})
    began = time.monotonic()
    result, recorder = execute(reviewed(root), Recorder(stop_on=b'EFFECT'))
    assert_contract(result, recorder)
    assert time.monotonic() - began < 20
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('cancelled', -signal.SIGKILL, True)
    assert b'EFFECT' in recorder.output
    assert (root / 'effect.txt').read_text() == 'committed by process' and not (root / 'late.txt').exists()
    assert_reaped(recorder.pid)


@macos
def test_timeout_stops_the_group_and_reports_uncertainty(tmp_path):
    root = project(tmp_path, {'probe.py': EFFECT})
    began = time.monotonic()
    result, recorder = execute(reviewed(root), timeout=1.5)
    elapsed = time.monotonic() - began
    assert_contract(result, recorder)
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('timed_out', -signal.SIGKILL, True)
    assert 1.5 <= elapsed < 12
    assert not (root / 'late.txt').exists()
    assert_reaped(recorder.pid)


@macos
@pytest.mark.parametrize('limit', [async_terminal.OUTPUT_LIMIT, 5000])
def test_output_burst_is_cut_at_the_exact_limit_and_stops_the_writer(tmp_path, limit):
    root = project(tmp_path, {'probe.py': (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.stdout.buffer.write(b'x' * 1_000_000)\n"
        "sys.stdout.buffer.flush()\n"
        "Path('after.txt').write_text('finished')\n"
    )})
    result, recorder = execute(reviewed(root), max_output=limit)
    assert_contract(result, recorder)
    assert (result['state'], result['outcome_uncertain']) == ('output_limited', True)
    assert recorder.output == b'x' * limit
    assert not (root / 'after.txt').exists()
    assert_reaped(recorder.pid)


@macos
@pytest.mark.parametrize('stop_on', [None, b'READY'], ids=['leader-exits', 'cancelled'])
def test_descendants_never_outlive_teardown_and_are_signalled_only_before_reap(tmp_path, monkeypatch, stop_on):
    calls, real_kill, real_wait = [], os.killpg, os.waitpid

    def killpg(pgid, signum):
        calls.append(('signal', pgid, signum))
        return real_kill(pgid, signum)

    def waitpid(pid, options):
        reaped = real_wait(pid, options)
        if reaped[0]:
            calls.append(('reaped', pid, None))
        return reaped

    monkeypatch.setattr(async_terminal, '_killpg', killpg)
    monkeypatch.setattr(async_terminal, '_waitpid', waitpid)
    root = project(tmp_path, {'probe.py': spawner('time.sleep(60)' if stop_on else '')})
    result, recorder = execute(reviewed(root), Recorder(stop_on=stop_on))
    assert_contract(result, recorder)
    assert (root / 'beat.txt').exists(), recorder.output
    if stop_on:
        assert (result['state'], result['exit_code']) == ('cancelled', -signal.SIGKILL)
    else:
        # The leader succeeded, but a live descendant had to be killed.
        assert (result['state'], result['exit_code']) == ('succeeded', 0)
    assert result['outcome_uncertain'] is True
    assert ('signal', recorder.pid, signal.SIGKILL) in calls
    assert {call[1] for call in calls} == {recorder.pid}
    assert [call[0] for call in calls].count('reaped') == 1 and calls[-1] == ('reaped', recorder.pid, None)
    assert_quiet(root / 'beat.txt')
    assert_gone(int((root / 'child.pid').read_text()))
    assert_reaped(recorder.pid)


@macos
def test_descendant_that_leaves_the_group_holding_output_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(async_terminal, 'DRAIN_SECONDS', 0.5)
    root = project(tmp_path, {'probe.py': spawner(beats=60, escape=True)})
    result, recorder = execute(reviewed(root))
    assert_contract(result, recorder)
    assert (result['state'], result['outcome_uncertain']) == ('unknown', True)
    assert_reaped(recorder.pid)
    # Documented limit: the escaped, still-sandboxed process ends on its own.
    assert_gone(int((root / 'child.pid').read_text()), seconds=15)


@macos
def test_concurrent_commands_keep_their_own_leaders_and_status(tmp_path):
    results = {}

    def run(code):
        source = f"import time\ntime.sleep(0.3)\nraise SystemExit({code})\n"
        root = project(tmp_path, {'probe.py': source}, name=f'project{code}')
        results[code] = execute(reviewed(root, writable=False))[0]

    threads = [threading.Thread(target=run, args=(code,)) for code in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert {code: (r['state'], r['exit_code'], r['outcome_uncertain']) for code, r in results.items()} == {
        0: ('succeeded', 0, False), 1: ('failed', 1, False), 2: ('failed', 2, False), 3: ('failed', 3, False)}


# Injected lifecycle negatives.

@macos
@pytest.mark.parametrize('stage', ['on_started', 'on_output'])
def test_callback_failure_tears_down_the_group_and_reports_unknown(tmp_path, stage):
    root = project(tmp_path, {'probe.py': spawner('time.sleep(60)')})
    started = []

    def on_started(info):
        started.append(info['pid'])
        if stage == 'on_started':
            raise RuntimeError('ledger unavailable')

    def on_output(chunk):
        raise RuntimeError('stream unavailable')

    began = time.monotonic()
    result = run_async_command(reviewed(root), threading.Event(), on_started, on_output, timeout=60)
    assert (result['state'], result['outcome_uncertain']) == ('unknown', True)
    assert time.monotonic() - began < 20
    assert_reaped(started[0])
    assert_quiet(root / 'beat.txt')
    child = (root / 'child.pid').read_text() if (root / 'child.pid').exists() else ''
    if child:
        assert_gone(int(child))


@macos
def test_base_exception_from_a_callback_propagates_only_after_teardown(tmp_path):
    root = project(tmp_path, {'probe.py': spawner('time.sleep(60)')})
    started = []

    def on_output(chunk):
        raise SystemExit('manager is stopping')

    with pytest.raises(SystemExit):
        run_async_command(reviewed(root), threading.Event(), lambda info: started.append(info['pid']),
                          on_output, timeout=60)
    assert_reaped(started[0])
    assert_quiet(root / 'beat.txt')
    assert_gone(int((root / 'child.pid').read_text()))
    assert not async_terminal._late_reapers


@macos
def test_folder_swapped_during_spawn_stops_the_command_as_unknown(tmp_path, monkeypatch):
    real, seen = async_terminal._identity, []

    def drifting(root):
        seen.append(root)
        return real(root) if len(seen) == 1 else (0, 0)

    monkeypatch.setattr(async_terminal, '_identity', drifting)
    root = project(tmp_path, {'probe.py': EFFECT})
    began = time.monotonic()
    result, recorder = execute(reviewed(root))
    assert len(seen) == 2 and time.monotonic() - began < 20
    assert (result['state'], result['outcome_uncertain']) == ('unknown', True)
    assert not (root / 'late.txt').exists()
    assert_reaped(recorder.pid)


@macos
def test_unconfirmed_teardown_is_unknown_and_handed_to_a_late_reaper(tmp_path, monkeypatch):
    def refused(pgid, signum):
        raise PermissionError(errno.EPERM, 'refused')

    monkeypatch.setattr(async_terminal, '_killpg', refused)
    monkeypatch.setattr(async_terminal, 'TEARDOWN_SECONDS', 0.3)
    root = project(tmp_path, {'probe.py': EFFECT})
    result, recorder = execute(reviewed(root), Recorder(stop_on=b'EFFECT'))
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('unknown', None, True)
    assert async_terminal._late_reapers
    # The leader is still unreaped, so its group is still owned and safe to kill.
    monkeypatch.setattr(async_terminal, '_killpg', os.killpg)
    join_late_reapers()
    assert_reaped(recorder.pid)
    assert not (root / 'late.txt').exists()


@macos
def test_leader_status_taken_by_another_reaper_is_unknown(tmp_path, monkeypatch):
    real = os.waitpid

    def stolen(pid, options):
        reaped = real(pid, options)
        if reaped[0]:
            raise ChildProcessError(errno.ECHILD, 'reaped elsewhere')
        return reaped

    monkeypatch.setattr(async_terminal, '_waitpid', stolen)
    root = project(tmp_path, {'probe.py': "print('done')\n"})
    result, recorder = execute(reviewed(root, writable=False))
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('unknown', None, True)
    assert b'done' in recorder.output and not async_terminal._late_reapers
    assert_reaped(recorder.pid)


@macos
def test_unobservable_leader_exit_is_unknown_and_still_reaped(tmp_path, monkeypatch):
    def unavailable(queue, pid):
        raise OSError(errno.ENOMEM, 'kqueue unavailable')

    monkeypatch.setattr(async_terminal, '_watch_exit', unavailable)
    root = project(tmp_path, {'probe.py': EFFECT})
    result, recorder = execute(reviewed(root))
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('unknown', None, True)
    join_late_reapers()
    assert_reaped(recorder.pid)
    assert not (root / 'late.txt').exists()


# killpg EPERM is ambiguous on XNU: group death is proven by libproc enumeration,
# never inferred from EPERM. These tests pin that reasoning.

def raiser(exc):
    def call(*args, **kwargs):
        raise exc
    return call


def owned_group(pid=987654):
    process = types.SimpleNamespace(pid=pid, stdout=io.BytesIO(), returncode=None)
    return async_terminal._OwnedGroup(process, async_terminal._Output(lambda chunk: None, 100))


def short_bsd(pid, pgid, status, ppid=1, comm=b'secret-process-name'):
    body = struct.pack('<IIII', pid, ppid, pgid, status) + comm.ljust(16, b'\x00')[:16] + b'\x00' * 32
    assert len(body) == async_terminal.SHORT_BSD_INFO_SIZE
    return body


@pytest.mark.parametrize('status, expected', [(1, 1), (2, 2), (3, 3), (4, 4), (async_terminal.SZOMB, 5)])
def test_parse_short_bsd_returns_member_status(status, expected):
    result = async_terminal._parse_short_bsd(short_bsd(321, 999, status), 64, 321, 999)
    assert result == expected and type(result) is int


def test_parse_short_bsd_moved_out_of_group_is_not_a_member():
    assert async_terminal._parse_short_bsd(short_bsd(321, 777, 2), 64, 321, 999) == 0


def test_parse_short_bsd_identity_mismatch_is_unknown():
    assert async_terminal._parse_short_bsd(short_bsd(654, 999, 2), 64, 321, 999) is None


@pytest.mark.parametrize('length', [0, 15, 16, 63, 65])
def test_parse_short_bsd_incomplete_buffer_cannot_prove_absence(length):
    data = short_bsd(321, 999, 2).ljust(65, b'\x00')[:length]
    assert async_terminal._parse_short_bsd(data, 64, 321, 999) is None


@pytest.mark.parametrize('written', [0, 16, 40, 60, 63, 65, 128])
def test_parse_short_bsd_wrong_abi_size_is_unknown(written):
    assert async_terminal._parse_short_bsd(short_bsd(321, 999, 2), written, 321, 999) is None


@pytest.mark.parametrize('status', [0, 6, 7, 99, 255])
def test_parse_short_bsd_impossible_status_is_unknown(status):
    assert async_terminal._parse_short_bsd(short_bsd(321, 999, status), 64, 321, 999) is None


def test_parse_short_bsd_never_returns_foreign_data():
    # Only the numeric status is surfaced; the process name is never exposed.
    result = async_terminal._parse_short_bsd(short_bsd(321, 999, 2, comm=b'top-secret'), 64, 321, 999)
    assert result == 2 and b'secret' not in repr(result).encode()


def test_live_group_members_zero_only_when_every_member_is_a_zombie(monkeypatch):
    monkeypatch.setattr(async_terminal, '_group_pids', lambda pgid: [10, 11])
    monkeypatch.setattr(async_terminal, '_short_bsd_status', lambda pid, pgid: async_terminal.SZOMB)
    assert async_terminal._live_group_members(999) == 0


def test_live_group_members_counts_live_members(monkeypatch):
    statuses = {10: async_terminal.SZOMB, 11: 2, 12: 3}
    monkeypatch.setattr(async_terminal, '_group_pids', lambda pgid: [10, 11, 12])
    monkeypatch.setattr(async_terminal, '_short_bsd_status', lambda pid, pgid: statuses[pid])
    assert async_terminal._live_group_members(999) == 2


def test_live_group_members_unreadable_member_is_unknown(monkeypatch):
    monkeypatch.setattr(async_terminal, '_group_pids', lambda pgid: [10, 11])
    monkeypatch.setattr(async_terminal, '_short_bsd_status',
                        lambda pid, pgid: None if pid == 11 else async_terminal.SZOMB)
    assert async_terminal._live_group_members(999) is None


def test_live_group_members_truncated_list_is_unknown(monkeypatch):
    monkeypatch.setattr(async_terminal, '_group_pids', lambda pgid: None)
    assert async_terminal._live_group_members(999) is None


def test_live_group_members_vanished_member_not_counted(monkeypatch):
    monkeypatch.setattr(async_terminal, '_group_pids', lambda pgid: [10])
    monkeypatch.setattr(async_terminal, '_short_bsd_status', lambda pid, pgid: 0)
    assert async_terminal._live_group_members(999) == 0


def test_live_group_members_excludes_the_known_exited_leader(monkeypatch):
    # The leader is excluded even if it momentarily reads as live mid-exit.
    monkeypatch.setattr(async_terminal, '_group_pids', lambda pgid: [999, 12])
    monkeypatch.setattr(async_terminal, '_short_bsd_status', lambda pid, pgid: 2)
    assert async_terminal._live_group_members(999, exclude=999) == 1
    assert async_terminal._live_group_members(999) == 2


def test_group_pids_grows_on_truncation_then_returns_complete(monkeypatch):
    def listpids(kind, pgid, buffer, size):
        if len(buffer) <= async_terminal._GROUP_LIST_CAPACITY:
            for index in range(len(buffer)):
                buffer[index] = 7  # Buffer looks exactly full, i.e. maybe truncated.
            return size
        buffer[0], buffer[1] = 101, 202
        return 8
    monkeypatch.setattr(async_terminal, '_libproc_handle',
                        lambda: types.SimpleNamespace(proc_listpids=listpids))
    assert async_terminal._group_pids(555) == [101, 202]


def test_group_pids_persistently_full_is_unknown(monkeypatch):
    def listpids(kind, pgid, buffer, size):
        for index in range(len(buffer)):
            buffer[index] = 7
        return size  # Never comes back short: never a provably complete snapshot.
    monkeypatch.setattr(async_terminal, '_libproc_handle',
                        lambda: types.SimpleNamespace(proc_listpids=listpids))
    assert async_terminal._group_pids(555) is None


@pytest.mark.parametrize('written', [-1, 7, 3])
def test_group_pids_error_or_misaligned_is_unknown(monkeypatch, written):
    monkeypatch.setattr(async_terminal, '_libproc_handle',
                        lambda: types.SimpleNamespace(proc_listpids=lambda *a: written))
    assert async_terminal._group_pids(555) is None


def test_group_pids_empty_group_is_empty_list(monkeypatch):
    monkeypatch.setattr(async_terminal, '_libproc_handle',
                        lambda: types.SimpleNamespace(proc_listpids=lambda *a: 0))
    monkeypatch.setattr(ctypes, 'get_errno', lambda: 0)
    assert async_terminal._group_pids(555) == []


def test_group_pids_zero_with_errno_is_unknown(monkeypatch):
    monkeypatch.setattr(async_terminal, '_libproc_handle',
                        lambda: types.SimpleNamespace(proc_listpids=lambda *a: 0))
    monkeypatch.setattr(ctypes, 'get_errno', lambda: errno.EPERM)
    assert async_terminal._group_pids(555) is None


def test_group_pids_without_libproc_is_unknown(monkeypatch):
    monkeypatch.setattr(async_terminal, '_libproc_handle', lambda: None)
    assert async_terminal._group_pids(555) is None
    assert async_terminal._short_bsd_status(1, 1) is None
    assert async_terminal._live_group_members(555) is None


def test_concurrent_first_libproc_probes_wait_for_the_same_complete_handle(monkeypatch):
    entered, release, second_started, second_done = (threading.Event() for _ in range(4))
    values, calls = [], []
    library = types.SimpleNamespace(proc_listpids=lambda *a: 0, proc_pidinfo=lambda *a: 0)
    def load(path, **kwargs):
        calls.append(path)
        entered.set()
        assert release.wait(3)
        return library
    monkeypatch.setattr(async_terminal, '_libproc', None)
    monkeypatch.setattr(async_terminal, '_libproc_probed', False)
    monkeypatch.setattr(ctypes, 'CDLL', load)
    def first():
        values.append(async_terminal._libproc_handle())
    def second():
        second_started.set()
        values.append(async_terminal._libproc_handle())
        second_done.set()
    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    threads[0].start()
    try:
        assert entered.wait(3)
        threads[1].start()
        assert second_started.wait(3)
        assert not second_done.wait(.05), 'Incomplete initialization was reported as unavailable.'
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(3)
                assert not thread.is_alive()
    assert len(values) == 2 and all(value is library for value in values)
    assert calls == ['/usr/lib/libproc.dylib']


@pytest.mark.parametrize('members', [[0], [-1], [17, 17]])
def test_group_pids_invalid_snapshot_does_not_hide_members(monkeypatch, members):
    def listpids(kind, pgid, buffer, size):
        for index, pid in enumerate(members):
            buffer[index] = pid
        return len(members) * 4
    monkeypatch.setattr(async_terminal, '_libproc_handle',
                        lambda: types.SimpleNamespace(proc_listpids=listpids))
    assert async_terminal._group_pids(555) is None


def test_short_bsd_status_reads_member_status(monkeypatch):
    data = short_bsd(321, 999, 2)

    def pidinfo(pid, flavor, arg, buffer, size):
        ctypes.memmove(buffer, data, len(data))
        return async_terminal.SHORT_BSD_INFO_SIZE

    monkeypatch.setattr(async_terminal, '_libproc_handle',
                        lambda: types.SimpleNamespace(proc_pidinfo=pidinfo))
    assert async_terminal._short_bsd_status(321, 999) == 2


@pytest.mark.parametrize('code, expected', [(errno.ESRCH, 0), (0, None), (errno.EPERM, None), (errno.EACCES, None)])
def test_short_bsd_status_zero_return_depends_on_errno(monkeypatch, code, expected):
    monkeypatch.setattr(async_terminal, '_libproc_handle',
                        lambda: types.SimpleNamespace(proc_pidinfo=lambda *a: 0))
    monkeypatch.setattr(ctypes, 'get_errno', lambda: code)
    assert async_terminal._short_bsd_status(321, 999) == expected


def test_kill_group_eperm_with_live_member_is_not_death(monkeypatch):
    # The parent's blocking issue: a live same-group member we may not signal
    # yields EPERM, which must NOT be read as the group being dead.
    group = owned_group()
    monkeypatch.setattr(async_terminal, '_killpg', raiser(PermissionError(errno.EPERM, 'refused')))
    monkeypatch.setattr(async_terminal, '_live_group_members', lambda pgid, **kw: 1)
    assert group._kill_group() is False


@pytest.mark.parametrize('exited', [False, True])
def test_eperm_excludes_only_a_leader_with_observed_exit(monkeypatch, exited):
    group, observations = owned_group(), []
    group.exited = exited
    monkeypatch.setattr(async_terminal, '_killpg', raiser(PermissionError(errno.EPERM, 'refused')))
    def members(pgid, exclude=()):
        observations.append((pgid, exclude))
        return None
    monkeypatch.setattr(async_terminal, '_live_group_members', members)
    assert group._kill_group() is False
    assert observations == [(group.pid, group.pid if exited else ())]


def test_kill_group_eperm_with_no_live_member_is_death(monkeypatch):
    group = owned_group()
    monkeypatch.setattr(async_terminal, '_killpg', raiser(PermissionError(errno.EPERM, 'refused')))
    monkeypatch.setattr(async_terminal, '_live_group_members', lambda pgid, **kw: 0)
    assert group._kill_group() is True


def test_kill_group_eperm_with_unknown_enumeration_is_not_death(monkeypatch):
    group = owned_group()
    monkeypatch.setattr(async_terminal, '_killpg', raiser(PermissionError(errno.EPERM, 'refused')))
    monkeypatch.setattr(async_terminal, '_live_group_members', lambda pgid, **kw: None)
    assert group._kill_group() is False


def test_kill_group_esrch_is_death_without_enumeration(monkeypatch):
    group = owned_group()
    monkeypatch.setattr(async_terminal, '_killpg', raiser(ProcessLookupError(errno.ESRCH, 'no group')))
    monkeypatch.setattr(async_terminal, '_live_group_members', raiser(AssertionError('no enumeration on ESRCH')))
    assert group._kill_group() is True


def test_kill_group_success_means_a_live_member_remains(monkeypatch):
    group = owned_group()
    monkeypatch.setattr(async_terminal, '_killpg', lambda pgid, signum: None)
    monkeypatch.setattr(async_terminal, '_live_group_members', raiser(AssertionError('no enumeration on success')))
    assert group._kill_group() is False


def test_kill_group_transient_oserror_is_not_death(monkeypatch):
    group = owned_group()
    monkeypatch.setattr(async_terminal, '_killpg', raiser(OSError(errno.ENOMEM, 'transient')))
    assert group._kill_group() is False


def test_group_is_never_signalled_after_reap(monkeypatch):
    group = owned_group()
    group.collected = True
    monkeypatch.setattr(async_terminal, '_killpg', raiser(AssertionError('no signal after reap')))
    with pytest.raises(RuntimeError, match='never signalled'):
        group._kill_group()


@macos
def test_supported_requires_libproc(monkeypatch):
    assert async_terminal._supported() is True
    monkeypatch.setattr(async_terminal, '_libproc_handle', lambda: None)
    assert async_terminal._supported() is False


@macos
def test_live_group_members_enumerates_the_real_kernel():
    mine = async_terminal._live_group_members(os.getpgrp())
    assert type(mine) is int and mine >= 1  # This test process is a live member.
    # A process-group id that cannot exist has no live members (never a false count).
    assert async_terminal._live_group_members(2_000_000_000) in (0, None)


@macos
def test_live_group_members_counts_a_real_running_descendant(tmp_path):
    root = project(tmp_path, {'probe.py': spawner('time.sleep(60)')})
    recorder = Recorder()
    seen = {}

    def on_started(info):
        recorder.on_started(info)
        seen['pid'] = info['pid']

    def on_output(chunk):
        recorder.on_output(chunk)
        if b'READY' in recorder.output and 'live' not in seen:
            # Leader and heartbeat descendant are both live, in the owned group.
            seen['live'] = async_terminal._live_group_members(seen['pid'])
            recorder.cancel.set()

    run_async_command(reviewed(root), recorder.cancel, on_started, on_output, timeout=60)
    assert type(seen['live']) is int and seen['live'] >= 2
    assert_quiet(root / 'beat.txt')
    assert_reaped(seen['pid'])


@macos
def test_eperm_refusal_with_a_live_descendant_is_unknown_not_success(tmp_path, monkeypatch):
    # The leader exits cleanly, but a same-group descendant keeps running and
    # killpg answers EPERM. The old code read that EPERM as group death and
    # would have reported success; enumeration proves the live member, so the
    # outcome is honestly unknown.
    monkeypatch.setattr(async_terminal, '_killpg', raiser(PermissionError(errno.EPERM, 'refused')))
    monkeypatch.setattr(async_terminal, 'TEARDOWN_SECONDS', 0.4)
    root = project(tmp_path, {'probe.py': spawner('')})  # Leader exits; child beats forever.
    recorder = Recorder()
    try:
        result = run_async_command(reviewed(root), recorder.cancel, recorder.on_started, recorder.on_output, timeout=30)
        assert (result['state'], result['outcome_uncertain']) == ('unknown', True)
    finally:
        pidfile = root / 'child.pid'
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
    assert_reaped(recorder.pid)
