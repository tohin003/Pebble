"""The unified SBPL builder: validation, each network mode and real confinement."""
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from pebble import sandbox_profiles
from pebble.sandbox_profiles import build
from pebble.policy import PolicyError

FW = Path('/Library/Frameworks/Python.framework')
APP_PY = (Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python').resolve()
CLT_PY = Path('/usr/bin/python3')
macos = pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
                           reason='actual macOS confinement')


def marker():
    return uuid.uuid4().hex


def dirs(tmp_path):
    sub = tmp_path / uuid.uuid4().hex[:8]
    temp = sub / 'temp'
    temp.mkdir(parents=True)
    root = sub / 'project'
    root.mkdir()
    return temp.resolve(), root.resolve()


def base(tmp_path, **kw):
    temp, root = dirs(tmp_path)
    kw.setdefault('executable', APP_PY)
    kw.setdefault('exec_paths', [FW])
    kw.setdefault('read_paths', [FW])
    kw.setdefault('project_root', root)
    kw.setdefault('temp', temp)
    kw.setdefault('job_marker', marker())
    return build(**kw), temp, root


# --- validation (pure) ------------------------------------------------------

def test_core_rules_present(tmp_path):
    mk = marker()
    text, temp, root = base(tmp_path, job_marker=mk)
    assert text.startswith('(version 1) (deny default)')
    assert '(allow signal (target same-sandbox))' in text
    assert '(allow process-info* (target same-sandbox))' in text
    assert '(allow sysctl-read)' in text
    assert '(allow process-fork)' in text
    assert mk in text  # the per-job marker makes the text unique
    assert '/dev/urandom' in text and '/dev/random' in text


def test_marker_makes_text_unique(tmp_path):
    a, _, _ = base(tmp_path, job_marker=marker())
    b, _, _ = base(tmp_path, job_marker=marker())
    assert a != b


def test_single_process_model_denies_fork(tmp_path):
    text, _, _ = base(tmp_path, process_model='single')
    assert '(allow process-fork)' not in text


@pytest.mark.parametrize('mode,needle,absent', [
    ('none', None, 'network'),
    ('open', '(allow network*)', None),
])
def test_network_modes_text(tmp_path, mode, needle, absent):
    text, _, _ = base(tmp_path, network=mode)
    if needle:
        assert needle in text
    if absent:
        assert 'allow network' not in text and 'network-outbound' not in text


def test_broker_mode_uses_the_broker_port(tmp_path):
    text, _, _ = base(tmp_path, network='broker', broker_port=54321)
    assert 'network-outbound' in text and 'localhost:54321' in text
    # DNS stays denied under broker mode.
    assert 'mDNSResponder' not in text


def test_open_mode_allows_dns_and_is_marked_legacy(tmp_path):
    text, _, _ = base(tmp_path, network='open')
    assert 'mDNSResponder' in text and 'Legacy' in text


def test_private_deny_toggle(tmp_path):
    on, _, _ = base(tmp_path, private_deny=True)
    off, _, _ = base(tmp_path, private_deny=False)
    assert 'credentials' in on and '.ssh' in on
    assert 'credentials' not in off


@pytest.mark.parametrize('bad', [
    {'job_marker': 'nope'}, {'job_marker': 'A' * 32}, {'job_marker': ''},
    {'network': 'maybe'}, {'process_model': 'dual'}, {'private_deny': 1},
    {'broker_port': 80},  # port without broker mode
    {'network': 'broker'},  # broker without port
    {'network': 'broker', 'broker_port': 0},
])
def test_invalid_arguments_fail_closed(tmp_path, bad):
    temp, root = dirs(tmp_path)
    args = dict(executable=APP_PY, exec_paths=[FW], read_paths=[FW], project_root=root, temp=temp,
                job_marker=marker())
    args.update(bad)
    with pytest.raises(PolicyError):
        build(**args)


@pytest.mark.parametrize('field', ['executable', 'project_root', 'temp'])
def test_paths_must_be_absolute_and_resolved(tmp_path, field):
    temp, root = dirs(tmp_path)
    args = dict(executable=APP_PY, exec_paths=[FW], read_paths=[FW], project_root=root, temp=temp,
                job_marker=marker())
    args[field] = 'relative/path'
    with pytest.raises(PolicyError):
        build(**args)


def test_symlinked_path_is_refused(tmp_path):
    temp, root = dirs(tmp_path)
    link = tmp_path / 'link'
    link.symlink_to(root)
    with pytest.raises(PolicyError):
        build(executable=APP_PY, exec_paths=[FW], read_paths=[FW], project_root=link.resolve() if False else link,
              temp=temp, job_marker=marker())


def test_exec_paths_must_be_a_short_list(tmp_path):
    temp, root = dirs(tmp_path)
    with pytest.raises(PolicyError):
        build(executable=APP_PY, exec_paths='/usr/bin', read_paths=[FW], project_root=root, temp=temp,
              job_marker=marker())


# --- real confinement -------------------------------------------------------

def run_probe(profile, code, *, argv0=None, temp, env_extra=None):
    exe = argv0 or str(APP_PY)
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(temp), 'TMPDIR': str(temp), 'LANG': 'en_US.UTF-8',
           **(env_extra or {})}
    out = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, exe, '-I', '-c', code], env=env,
                         capture_output=True, text=True, timeout=60)
    return out


@macos
def test_clt_python_starts(tmp_path):
    temp, root = dirs(tmp_path)
    profile = build(executable=CLT_PY, exec_paths=[sandbox_profiles.CLT], read_paths=[sandbox_profiles.CLT],
                    project_root=root, temp=temp, job_marker=marker())
    out = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, str(CLT_PY), '-c', 'print("CLT-OK")'],
                         env={'PATH': '/usr/bin:/bin', 'HOME': str(temp), 'TMPDIR': str(temp),
                              'DEVELOPER_DIR': str(sandbox_profiles.CLT)},  # restated 2026-10-30: see test_egress.CLT_ENV
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and 'CLT-OK' in out.stdout, out.stderr


@macos
@pytest.mark.parametrize('mode', ['none', 'broker', 'open'])
def test_network_modes_are_enforced(tmp_path, mode):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        temp, root = dirs(tmp_path)
        kw = {'broker_port': port} if mode == 'broker' else {}
        profile = build(executable=APP_PY, exec_paths=[FW], read_paths=[FW], project_root=root, temp=temp,
                        job_marker=marker(), network=mode, **kw)
        code = ("import socket\n"
                "try:\n"
                f"    socket.create_connection(('127.0.0.1', {port}), timeout=3).close(); print('CONNECTED')\n"
                "except OSError as e:\n"
                "    print('DENIED', type(e).__name__)\n")
        out = run_probe(profile, code, temp=temp)
    assert out.returncode == 0, out.stderr
    if mode == 'none':
        assert 'DENIED' in out.stdout
    else:  # broker allows the broker port; open allows all
        assert 'CONNECTED' in out.stdout


@macos
def test_exec_from_temp_is_denied_but_exec_path_is_allowed(tmp_path):
    temp, root = dirs(tmp_path)
    (root / 'tools').mkdir()
    shutil.copy('/bin/echo', temp / 'planted')
    shutil.copy('/bin/echo', root / 'tools' / 'allowed')
    profile = build(executable=APP_PY, exec_paths=[FW, root / 'tools'], read_paths=[FW],
                    project_root=root, temp=temp, writable_paths=[root], job_marker=marker())
    code = ("import subprocess\n"
            "for name in [%r, %r]:\n"
            "    try:\n"
            "        subprocess.run([name], capture_output=True, timeout=5); print('RAN', name)\n"
            "    except OSError:\n"
            "        print('DENIED', name)\n" % (str(temp / 'planted'), str(root / 'tools' / 'allowed')))
    out = run_probe(profile, code, temp=temp)
    assert ('DENIED ' + str(temp / 'planted')) in out.stdout, out.stdout + out.stderr
    assert ('RAN ' + str(root / 'tools' / 'allowed')) in out.stdout, out.stdout + out.stderr


@macos
def test_private_paths_and_outside_are_denied(tmp_path):
    temp, root = dirs(tmp_path)
    (root / '.env').write_text('API_KEY=secret')
    (root / '.git').mkdir()
    (root / '.git' / 'HEAD').write_text('ref: x')
    (root / 'deploy.pem').write_text('key')
    outside = tmp_path / 'outside.txt'
    outside.write_text('OUTSIDE')
    profile = build(executable=APP_PY, exec_paths=[FW], read_paths=[FW], project_root=root, temp=temp,
                    writable_paths=[root], job_marker=marker())
    code = ("from pathlib import Path\n"
            "for name in ['.env', '.git/HEAD', 'deploy.pem', %r]:\n"
            "    try:\n"
            "        Path(name).read_text(); print('LEAK', name)\n"
            "    except OSError:\n"
            "        print('DENIED', name)\n"
            "try:\n"
            "    Path('ok.txt').write_text('x'); print('WROTE ordinary')\n"
            "except OSError:\n"
            "    print('NO ordinary')\n" % str(outside))
    out = run_probe(profile, code, temp=temp, env_extra={})
    out2 = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, str(APP_PY), '-I', '-c', code], cwd=root,
                          env={'PATH': '/usr/bin:/bin', 'HOME': str(temp), 'TMPDIR': str(temp)},
                          capture_output=True, text=True, timeout=60)
    assert 'LEAK' not in out2.stdout, out2.stdout
    assert out2.stdout.count('DENIED') == 4
    assert 'WROTE ordinary' in out2.stdout
    assert outside.read_text() == 'OUTSIDE'


@macos
def test_job_can_terminate_its_own_child(tmp_path):
    temp, root = dirs(tmp_path)
    profile = build(executable=APP_PY, exec_paths=[FW], read_paths=[FW], project_root=root, temp=temp,
                    writable_paths=[temp], job_marker=marker())
    code = ("import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'])\n"
            "time.sleep(0.3)\n"
            "child.terminate()\n"
            "try:\n"
            "    child.wait(timeout=5); print('TERMINATED', child.returncode)\n"
            "except Exception as e:\n"
            "    print('FAILED', type(e).__name__); child.kill()\n")
    out = run_probe(profile, code, temp=temp)
    assert 'TERMINATED' in out.stdout, out.stdout + out.stderr


@macos
def test_job_cannot_signal_an_outside_canary(tmp_path):
    temp, root = dirs(tmp_path)
    canary = subprocess.Popen([str(APP_PY), '-I', '-c', 'import time; time.sleep(30)'])
    try:
        profile = build(executable=APP_PY, exec_paths=[FW], read_paths=[FW], project_root=root, temp=temp,
                        job_marker=marker())
        code = ("import os, sys\n"
                "try:\n"
                f"    os.kill({canary.pid}, 15); print('SIGNALLED')\n"
                "except PermissionError:\n"
                "    print('DENIED EPERM')\n"
                "except ProcessLookupError:\n"
                "    print('GONE')\n")
        out = run_probe(profile, code, temp=temp)
        assert 'DENIED EPERM' in out.stdout, out.stdout + out.stderr
        assert canary.poll() is None  # the canary survived
    finally:
        canary.terminate()
        canary.wait(timeout=5)
