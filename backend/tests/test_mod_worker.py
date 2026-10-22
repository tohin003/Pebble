"""Real JavaScriptCore execution, bounded output and native process confinement."""
import errno
import json
import os
import socket
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from pebble import mod_worker as worker


requires_macos = pytest.mark.skipif(not worker.supported(), reason='macOS sandbox worker required')


def script(body, event='manual'):
    return 'pebble.on(' + json.dumps(event) + ', function(payload) {' + body + '});'


def pane(ident='pane', kind='table', data='{columns:["value"],rows:[[3]]}'):
    return 'pebble.render({id:' + json.dumps(ident) + ',title:"Example",kind:' + json.dumps(kind) + ',data:' + data + '});'


@requires_macos
@pytest.mark.parametrize('event', sorted(worker.EVENTS))
def test_real_jsc_events_emit_native_data_and_frozen_context(event):
    source = script('''
      if (!Object.isFrozen(payload) || !Object.isFrozen(payload.budget)) throw Error("Mutable input");
      if (typeof process !== "undefined" || typeof require !== "undefined" ||
          typeof fetch !== "undefined" || typeof document !== "undefined" ||
          typeof console !== "undefined" || typeof WebAssembly !== "undefined" || typeof readFile !== "undefined" ||
          typeof Python !== "undefined") throw Error("Unexpected bridge");
      if (!Object.isFrozen(pebble) || Object.keys(pebble).sort().join(",") !== "on,render") throw Error("SDK");
      pebble.render({id:"budget",title:"Context budget",kind:"table",
        data:{columns:["Used","Limit"],rows:[[payload.budget.used,payload.budget.limit]]}});
    ''', event)
    result = worker.run(source, event, {'budget': {'used': 100, 'limit': 8192}})
    assert result == [{'id': 'budget', 'title': 'Context budget', 'kind': 'table',
        'data': {'columns': ['Used', 'Limit'], 'rows': [[100, 8192]]}}]


@requires_macos
def test_only_selected_event_and_no_cross_invocation_state():
    source = script(pane('manual')) + script(pane('state'), 'run.state')
    assert worker.run(source, 'manual', {})[0]['id'] == 'manual'
    assert worker.run(source, 'run.state', {})[0]['id'] == 'state'
    assert worker.run(source, 'context.summary', {}) == []
    assert worker.run('if (typeof saved !== "undefined") throw Error(); globalThis.saved = 1;', 'manual', {}) == []
    assert worker.run('if (typeof saved !== "undefined") throw Error();', 'manual', {}) == []


@requires_macos
def test_trusted_serialization_survives_global_and_prototype_tampering():
    source = '''
      JSON.stringify = () => "forged";
      JSON.parse = () => {throw Error("forged")};
      Object.freeze = () => {throw Error("forged")};
      Reflect.apply = () => {throw Error("forged")};
      Object.prototype.toJSON = () => ({html:"forged"});
      Array.prototype.toJSON = () => ({html:"forged"});
      Object.defineProperty(Array.prototype,"0",{set(){throw Error("Intercepted")},configurable:true});
      pebble.on("manual", function(payload) {
        if (!Object.isFrozen(payload.nested)) throw Error("Mutable");
        pebble.render({id:"safe",title:"Safe",kind:"table",data:{columns:["Number"],rows:[[payload.nested.n]]}});
      });
    '''
    assert worker.run(source, 'manual', {'nested': {'n': 7}})[0]['data']['rows'] == [[7]]


@requires_macos
@pytest.mark.parametrize('body', [
    'throw new Error("private diagnostic must not escape");',
    'pebble.render({id:"x",title:"X",kind:"html",data:{html:"<script>"}});',
    'pebble.render({id:"x",title:"X",kind:"form",data:{}});',
    'pebble.render({id:"x",title:"X",kind:"table",data:{f:()=>1}});',
    'pebble.render({id:"x",title:"X",kind:"table",data:{n:NaN}});',
    'pebble.render({id:"x",title:"X",kind:"table",data:{n:Infinity}});',
    'pebble.render({id:"x",title:"X",kind:"table",data:{big:1n}});',
    'pebble.render({id:"x",title:"X",kind:"table",data:{get secret(){return 1}}});',
    'pebble.render({id:"x",title:"X",kind:"table",data:{a:new Date()}});',
    'pebble.render({id:"x",title:"X",kind:"table",data:{a:Array(3)}});',
    'const x={};x.self=x;pebble.render({id:"x",title:"X",kind:"table",data:x});',
    'pebble.render({id:"x",title:"X",kind:"table",data:JSON.parse("{\\"__proto__\\":1}")});',
    'pebble.render({id:"x",title:"X",kind:"table",data:{},capabilities:["network"]});',
    'pebble.render({id:"x",title:"X",kind:"table",data:{s:"x".repeat(4001)}});',
    'return Promise.resolve();',
    'return {tool:"Read",arguments:{path:"private.txt"}};',
    'pebble.exec("touch /tmp/a");',
    pane('same') + pane('same'),
    'for(let i=0;i<5;i++){try{pebble.render({id:"p"+i,title:"X",kind:"table",data:{}})}catch(e){}}',
    'try{pebble.on("network",()=>{});}catch(e){}' + pane(),
    'try{Object.prototype.value=1;pebble.render({id:"p",title:"X",kind:"table",data:{get v(){return 7}}});}catch(e){}',
])
def test_malformed_data_capabilities_async_and_caught_boundary_failures_reject(body):
    with pytest.raises(worker.ModWorkerError) as caught:
        worker.run(script(body), 'manual', {})
    assert 'private diagnostic' not in str(caught.value)


@requires_macos
@pytest.mark.parametrize('source', ['pebble.on("manual",(', 'throw Error("private diagnostic");', pane()])
def test_invalid_source_and_render_outside_dispatch_fail(source):
    with pytest.raises(worker.ModWorkerError):
        worker.run(source, 'manual', {})


@requires_macos
def test_four_distinct_panes_allowed_all_six_kinds_are_data_only():
    for kind in sorted(worker.KINDS):
        assert worker.run(script(pane(kind, kind, '{}')), 'manual', {})[0]['kind'] == kind
    assert len(worker.run(script(''.join(pane('p' + str(i)) for i in range(4))), 'manual', {})) == 4


@requires_macos
def test_total_utf8_output_bound_is_enforced_before_host_accepts_panes():
    body = '''
      for (let i=0;i<4;i++) {
        pebble.render({id:"p"+i,title:"Unicode",kind:"table",data:{a:"😀".repeat(1800),b:"😀".repeat(1800)}});
      }
    '''
    with pytest.raises(worker.ModWorkerError):
        worker.run(script(body), 'manual', {})


@pytest.mark.parametrize('source,event,payload,timeout', [
    ('x' * 16001, 'manual', {}, 2), ('\0', 'manual', {}, 2), ('\ud800', 'manual', {}, 2),
    ('// source', 'network', {}, 2), ('// source', 'manual', [], 2),
    ('// source', 'manual', {'text': 'x' * 4001}, 2),
    ('// source', 'manual', {'text': '😀' * 1900}, 2),
    ('// source', 'manual', {'token': float('nan')}, 2),
    ('// source', 'manual', {'__proto__': {}}, 2),
    ('// source', 'manual', {'token': lambda: 1}, 2),
    ('// source', 'manual', {}, True), ('// source', 'manual', {}, 0),
    ('// source', 'manual', {}, 3), ('// source', 'manual', {}, float('inf')),
])
def test_invalid_input_never_spawns(source, event, payload, timeout, monkeypatch):
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda *a, **kw: pytest.fail('Invalid input spawned a child'))
    with pytest.raises(worker.ModWorkerError, match='invalid'):
        worker.run(source, event, payload, timeout=timeout)


def test_unsupported_platform_fails_closed(monkeypatch):
    monkeypatch.setattr(worker.sys, 'platform', 'linux')
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda *a, **kw: pytest.fail('Unsupported child'))
    with pytest.raises(worker.ModWorkerError, match='unsupported'):
        worker.run('// source', 'manual', {})


def test_pre_cancel_does_not_spawn(monkeypatch):
    flag = threading.Event(); flag.set()
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda *a, **kw: pytest.fail('Cancelled child'))
    with pytest.raises(InterruptedError):
        worker.run('// source', 'manual', {}, flag)


@pytest.fixture
def owned_children(monkeypatch):
    children = []
    original = worker.subprocess.Popen
    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append((process, kwargs['cwd']))
        assert kwargs['close_fds'] is True and kwargs['start_new_session'] is True
        assert not Path(kwargs['cwd']).stat().st_mode & 0o077
        return process
    monkeypatch.setattr(worker.subprocess, 'Popen', spawn)
    yield children
    for process, folder in children:
        assert process.poll() is not None
        assert process.stdin.closed and process.stdout.closed and process.stderr.closed
        assert not Path(folder).exists()
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)


@requires_macos
def test_endless_loop_times_out_and_reaps_with_scratch_cleanup(owned_children):
    started = time.monotonic()
    with pytest.raises(worker.ModWorkerError) as caught:
        worker.run(script('while(true){}'), 'manual', {}, timeout=.3)
    assert caught.value.reason == 'timeout'
    assert time.monotonic() - started < 1.5
    assert len(owned_children) == 1


@requires_macos
def test_real_allocation_hits_resident_memory_budget_independently_of_cpu(owned_children):
    started = time.monotonic()
    with pytest.raises(worker.ModWorkerError) as caught:
        worker.run(script('const held=[];while(true){const x=new Uint8Array(1024*1024);x.fill(1);held.push(x);}'), 'manual', {})
    assert caught.value.reason == 'memory'
    assert time.monotonic() - started < 2
    assert len(owned_children) == 1


@requires_macos
def test_midflight_cancel_reaps_before_returning(owned_children):
    flag = threading.Event()
    timer = threading.Timer(.12, flag.set); timer.start()
    try:
        with pytest.raises(InterruptedError):
            worker.run(script('while(true){}'), 'manual', {}, flag)
    finally:
        timer.cancel(); timer.join()
    assert len(owned_children) == 1


@requires_macos
def test_success_and_js_failure_cleanup(owned_children):
    assert worker.run(script(pane()), 'manual', {})
    with pytest.raises(worker.ModWorkerError):
        worker.run(script('throw Error("failure")'), 'manual', {})
    assert len(owned_children) == 2


@requires_macos
def test_native_sandbox_blocks_private_files_network_fork_env_and_inherited_handles(tmp_path, monkeypatch):
    # A trusted Python probe verifies the exact worker OS profile, independently
    # of the VM's lack of a filesystem/network/ctypes bridge.
    secret = tmp_path / 'private.txt'; secret.write_text('disposable-private-sentinel')
    target = tmp_path / 'write.txt'
    monkeypatch.setenv('PEBBLE_MOD_SECRET', 'disposable-env-sentinel')
    handle = os.open(secret, os.O_RDONLY); os.set_inheritable(handle, True)
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(1)
    probe = f'''
import json,os,socket,ctypes,resource
checks = {{}}
for name,action in [
    ('read',lambda:open({str(secret)!r}).read()),
    ('project',lambda:open({str(Path(__file__).resolve())!r}).read()),
    ('write',lambda:open({str(target)!r},'w')),
    ('network',lambda:socket.create_connection(('127.0.0.1',{listener.getsockname()[1]}))),
    ('inherited',lambda:os.fstat({handle})),
]:
    try:
        action(); checks[name]=False
    except OSError:
        checks[name]=True
try:
    pid=os.fork()
except OSError:
    checks['fork']=True
else:
    if pid==0:os._exit(91)
    os.waitpid(pid,0);checks['fork']=False
checks['environment']=not os.environ.get('PEBBLE_MOD_SECRET')
lib=ctypes.CDLL({worker.JSC_LIBRARY!r})
checks['jsc']=bool(lib.JSGlobalContextCreate)
print(json.dumps(checks))
'''
    try:
        folder = tmp_path / 'scratch'; folder.mkdir(mode=0o700)
        command, environment = worker._launch(folder.resolve())
        process = subprocess.Popen(command[:-1] + ['-c', probe], cwd=folder,
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
        try:
            out, err = process.communicate(timeout=2)
            assert process.returncode == 0, err.decode(errors='replace')
            checks = json.loads(out)
            assert checks and all(checks.values()), checks
            assert not target.exists()
        finally:
            # This probe has DEVNULL stdin, so _reap's owned pipe handling still
            # works, and kills the group if fork were unexpectedly allowed.
            worker._reap(process)
    finally:
        os.close(handle); listener.close()


@requires_macos
def test_memory_monitor_failure_is_closed_and_cleaned(owned_children, monkeypatch):
    def unavailable(pid):
        raise worker.ModWorkerError('memory-monitor')
    monkeypatch.setattr(worker, '_memory_reader', lambda: unavailable)
    with pytest.raises(worker.ModWorkerError) as caught:
        worker.run(script('while(true){}'), 'manual', {})
    assert caught.value.reason == 'memory-monitor'


@requires_macos
@pytest.mark.parametrize('descriptor,size', [(1, 100_000), (2, 4_000)])
def test_parent_pipe_budget_stops_output_flood_and_cleans_up(descriptor, size, owned_children, monkeypatch):
    # A trusted native probe injects over-budget bytes into the actual child
    # transport; untrusted JS itself has no stdout/stderr callback.
    original = worker.subprocess.Popen
    def flood(command, **kwargs):
        return original(command[:-1] + ['-c', f'import os;os.write({descriptor},b"x"*{size})'], **kwargs)
    monkeypatch.setattr(worker.subprocess, 'Popen', flood)
    with pytest.raises(worker.ModWorkerError) as caught:
        worker.run('// source', 'manual', {})
    assert caught.value.reason == 'output'


@requires_macos
def test_child_installs_cpu_file_core_and_descriptor_limits(owned_children, monkeypatch):
    original = worker.subprocess.Popen
    code = f'''
import importlib.util,resource
spec=importlib.util.spec_from_file_location('mod_probe',{str(Path(worker.__file__).resolve())!r})
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def limits(packet):
    return [{{'id':'limits','title':'Limits','kind':'table','data':{{
        'cpu':list(resource.getrlimit(resource.RLIMIT_CPU)),
        'files':list(resource.getrlimit(resource.RLIMIT_FSIZE)),
        'fds':list(resource.getrlimit(resource.RLIMIT_NOFILE)),
        'core':list(resource.getrlimit(resource.RLIMIT_CORE))
    }}}}]
m._evaluate=limits;m.worker_main()
'''
    def probe(command, **kwargs):
        return original(command[:-1] + ['-c', code], **kwargs)
    monkeypatch.setattr(worker.subprocess, 'Popen', probe)
    data = worker.run('// source', 'manual', {}, timeout=.5)[0]['data']
    assert data == {'cpu': [1, 2], 'files': [32768, 32768], 'fds': [64, 64], 'core': [0, 0]}


@requires_macos
def test_kernel_cpu_soft_limit_is_timeout_before_parent_wall_deadline(owned_children, monkeypatch):
    original = worker.subprocess.Popen
    code = 'import resource;resource.setrlimit(resource.RLIMIT_CORE,(0,0));resource.setrlimit(resource.RLIMIT_CPU,(1,2));\nwhile True:pass'
    def cpu_probe(command, **kwargs):
        return original(command[:-1] + ['-c', code], **kwargs)
    monkeypatch.setattr(worker.subprocess, 'Popen', cpu_probe)
    started = time.monotonic()
    with pytest.raises(worker.ModWorkerError) as caught:
        worker.run('// source', 'manual', {}, timeout=2)
    assert caught.value.reason == 'timeout'
    assert owned_children[0][0].returncode == -signal.SIGXCPU
    assert time.monotonic() - started < 2


@requires_macos
def test_wrapped_cpu_signal_exit_is_also_timeout(owned_children, monkeypatch):
    original = worker.subprocess.Popen
    def wrapped_exit(command, **kwargs):
        return original(command[:-1] + ['-c', 'import os;os._exit(' + str(128 + signal.SIGXCPU) + ')'], **kwargs)
    monkeypatch.setattr(worker.subprocess, 'Popen', wrapped_exit)
    with pytest.raises(worker.ModWorkerError) as caught:
        worker.run('// source', 'manual', {})
    assert caught.value.reason == 'timeout'


# Reaping races. Every child here is spawned by the test or run() and owned in a
# new session; kill signals go only to an owned, still-unreaped group.

def own(code):
    return subprocess.Popen([sys.executable, '-I', '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, close_fds=True, start_new_session=True)


def finish_owned(process, killpg=os.killpg):
    """Explicit real cleanup: kill only while unreaped, then reap and close pipes."""
    try:
        if process.poll() is None:
            try:
                killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as refusal:
                try:
                    process.wait(timeout=.1)  # Only a confirmed exited leader may be tolerated.
                except subprocess.TimeoutExpired:
                    raise refusal
            process.wait(timeout=5)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def refused():
    return PermissionError(errno.EPERM, os.strerror(errno.EPERM))


@requires_macos
def test_reap_finishes_an_exited_unreaped_owned_group_and_closes_every_pipe(monkeypatch):
    real, replies = os.killpg, []

    def observed(pid, sig):
        try:
            real(pid, sig)
        except OSError as exc:
            replies.append((pid, sig, type(exc)))
            raise
        replies.append((pid, sig, None))
    process = own('pass')
    try:
        # Exited but not reaped: the owned group now holds only its zombie leader.
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        monkeypatch.setattr(worker.os, 'killpg', observed)
        worker._reap(process)
        # The real kernel refuses the group signal; the exited leader is still reaped.
        assert replies == [(process.pid, signal.SIGKILL, PermissionError)]
        assert process.returncode == 0
        assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    finally:
        finish_owned(process)


@requires_macos
@pytest.mark.parametrize('outcome', ['timeout', 'cancel'])
def test_confirmed_zombie_group_refusal_preserves_timeout_and_cancellation(outcome, owned_children, monkeypatch):
    real, injected = os.killpg, []

    def zombie_group(pid, sig):
        if sig != signal.SIGKILL or pid in injected or pid not in {p.pid for p, _ in owned_children}:
            return real(pid, sig)
        injected.append(pid)
        real(pid, sig)  # The owned group really is killed first.
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)  # Exited, still unreaped: no poll race.
        raise refused()  # The confirmed macOS reply for a zombie-only group.
    monkeypatch.setattr(worker.os, 'killpg', zombie_group)
    if outcome == 'timeout':
        with pytest.raises(worker.ModWorkerError) as caught:
            worker.run(script('while(true){}'), 'manual', {}, timeout=.3)
        assert caught.value.reason == 'timeout'
    else:
        flag = threading.Event()
        timer = threading.Timer(.12, flag.set); timer.start()
        try:
            with pytest.raises(InterruptedError):
                worker.run(script('while(true){}'), 'manual', {}, flag)
        finally:
            timer.cancel(); timer.join()
    assert len(owned_children) == 1
    process = owned_children[0][0]
    assert injected == [process.pid] and process.returncode == -signal.SIGKILL


@requires_macos
def test_reap_surfaces_a_permission_refusal_for_a_live_owned_group(monkeypatch):
    real, refusals = os.killpg, []

    def refuse_live(pid, sig):
        if pid == process.pid and sig == signal.SIGKILL and not refusals:
            refusals.append(pid)
            raise refused()
        return real(pid, sig)
    process = own('import time\ntime.sleep(30)')
    try:
        monkeypatch.setattr(worker.os, 'killpg', refuse_live)
        with pytest.raises(PermissionError):
            worker._reap(process)
        # Not mistaken for an exited zombie: alive and never waited for, yet no pipe leaks.
        assert refusals == [process.pid] and process.returncode is None and process.poll() is None
        assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    finally:
        finish_owned(process, real)


@requires_macos
def test_run_surfaces_a_live_group_refusal_instead_of_its_classified_timeout(owned_children, monkeypatch):
    real, refusals = os.killpg, []

    def refuse_live(pid, sig):
        if sig == signal.SIGKILL and not refusals and pid in {p.pid for p, _ in owned_children}:
            refusals.append(pid)
            raise refused()
        return real(pid, sig)
    monkeypatch.setattr(worker.os, 'killpg', refuse_live)
    try:
        with pytest.raises(PermissionError):
            worker.run(script('while(true){}'), 'manual', {}, timeout=.3)
        process = owned_children[0][0]
        assert refusals == [process.pid] and process.poll() is None
        assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    finally:
        for process, _ in owned_children:
            finish_owned(process, real)


@requires_macos
def test_reap_propagates_a_wait_timeout_and_still_closes_every_pipe():
    process = own('import time\ntime.sleep(30)')
    actual, timeouts = process.wait, []

    def stuck(timeout=None):
        if not timeouts:
            timeouts.append(timeout)
            raise subprocess.TimeoutExpired(process.args, timeout)
        return actual(timeout=timeout)
    process.wait = stuck
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            worker._reap(process)
        assert timeouts == [2]
        assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    finally:
        finish_owned(process)


FROZEN_BINARY = Path(__file__).resolve().parents[2] / '.runtime/harness-candidate/pebble-backend/pebble-backend'


@requires_macos
@pytest.mark.skipif(not FROZEN_BINARY.is_file(), reason='Real onedir frozen candidate required')
def test_real_frozen_vm_allocation_is_observed_by_owned_pid_rss_and_reaped(owned_children, monkeypatch):
    # This fails if a bootloader leader is watched while the actual VM allocates
    # in an unmonitored child. The candidate is executed only via --mod-worker.
    monkeypatch.setattr(worker.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(worker.sys, 'executable', str(FROZEN_BINARY))
    monkeypatch.setattr(worker.sys, '_MEIPASS', str(FROZEN_BINARY.parent / '_internal'), raising=False)
    observations = []
    actual_reader = worker._memory_reader
    def memory_reader():
        resident = actual_reader()
        def observed(pid):
            size = resident(pid); observations.append((pid, size)); return size
        return observed
    monkeypatch.setattr(worker, '_memory_reader', memory_reader)
    assert worker.run(script(pane()), 'manual', {})[0]['id'] == 'pane'
    observations.clear()
    started = time.monotonic()
    with pytest.raises(worker.ModWorkerError) as caught:
        worker.run(script('const held=[];while(true){const x=new Uint8Array(1024*1024);x.fill(1);held.push(x);}'), 'manual', {})
    assert caught.value.reason == 'memory' and time.monotonic() - started < 2
    allocating_pid = owned_children[-1][0].pid
    assert {pid for pid, _ in observations} == {allocating_pid}
    assert max(size for _, size in observations) > worker.RSS_BYTES


def test_frozen_command_uses_early_worker_flag(tmp_path, monkeypatch):
    executable = tmp_path / 'bundle' / 'pebble-backend'; executable.parent.mkdir(); executable.touch()
    monkeypatch.setattr(worker.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(worker.sys, 'executable', str(executable))
    monkeypatch.setattr(worker.sys, '_MEIPASS', str(executable.parent), raising=False)
    command, env = worker._launch(tmp_path.resolve())
    assert command[-2:] == [str(executable.resolve()), '--mod-worker']
    assert set(env) == {'HOME', 'TMPDIR', 'PATH', 'LANG', 'PYTHONNOUSERSITE',
        'PYTHONDONTWRITEBYTECODE', 'JSC_useJIT', 'JSC_useWasm'}


@requires_macos
def test_reap_confirms_exit_when_first_poll_lags_zombie_group_refusal(monkeypatch):
    process = own('pass')
    real = os.killpg
    try:
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        actual_poll, polls = process.poll, []

        def stale_poll():
            polls.append(True)
            return None if len(polls) == 1 else actual_poll()

        def refused_group(pid, sig):
            assert (pid, sig) == (process.pid, signal.SIGKILL)
            raise refused()

        process.poll = stale_poll
        monkeypatch.setattr(worker.os, 'killpg', refused_group)
        worker._reap(process)
        assert process.returncode == 0 and polls
        assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    finally:
        finish_owned(process, real)


@requires_macos
def test_task_info_exit_race_requires_confirmed_exit_before_accepting_pane(owned_children, monkeypatch):
    actual_reader = worker._memory_reader
    observations = []

    def reader():
        resident = actual_reader()

        def disappearing(pid):
            observations.append(pid)
            if len(observations) != 2:
                return resident(pid)
            process = owned_children[-1][0]
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
            actual_poll, polls = process.poll, []

            def stale_poll():
                polls.append(True)
                return None if len(polls) == 1 else actual_poll()

            process.poll = stale_poll
            raise worker.ModWorkerError('memory-monitor')

        return disappearing

    monkeypatch.setattr(worker, '_memory_reader', reader)
    assert worker.run(script(pane()), 'manual', {})[0]['id'] == 'pane'
    assert len(observations) == 2 and owned_children[-1][0].returncode == 0
