"""Phase 34 (trial 1): killing a worker's process group when its leader has just exited.

Trial 1's full suite failed once in test_connector_route (no_loopback): mcp_transport.terminate saw the MCP worker
running (poll() is None), the worker exited, and os.killpg answered PermissionError, which macOS returns for a group
of zombies. terminate raised instead of reaping. The benchmark harness had the same race (Phase 32, H3). The same
pattern was open in hooks.py and mcp_media.py; the other killpg sites already tolerate it.

Each site now treats EPERM as "the group is gone" only when the leader has really exited, and still kills a live
leader directly, so a real permission problem never passes as a stopped worker. The killpg answer is forced here;
everything else is the checked-in code.
"""
import signal

import pytest

from pebble import hooks, mcp_media, mcp_transport


class Proc:
    """A child whose leader exits between the first poll() and the group signal (or never does)."""

    def __init__(self, exits_before_signal):
        self.pid, self.exits, self.polls, self.killed, self.waited, self.returncode = 4242, exits_before_signal, 0, 0, 0, None
        self.stdin = None

    def poll(self):
        self.polls += 1
        if self.polls > 1 and self.exits:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        self.waited += 1
        return 0


@pytest.fixture
def eperm(monkeypatch):
    calls = []

    def killpg(pid, sig):
        calls.append((pid, sig))
        raise PermissionError(1, 'Operation not permitted')
    monkeypatch.setattr(mcp_transport.os, 'killpg', killpg)
    monkeypatch.setattr(mcp_media.os, 'killpg', killpg)
    monkeypatch.setattr(hooks.os, 'killpg', killpg)
    return calls


@pytest.mark.parametrize('exits, killed', [(True, 0), (False, 1)])
def test_mcp_terminate_reaps_an_exited_leader_and_kills_a_live_one(eperm, exits, killed):
    proc = Proc(exits)
    mcp_transport.terminate(proc)  # before the fix: PermissionError
    assert eperm == [(4242, signal.SIGKILL)] and proc.killed == killed and proc.waited == 1


@pytest.mark.parametrize('exits, killed', [(True, 0), (False, 1)])
def test_mcp_media_stop_reaps_an_exited_leader_and_kills_a_live_one(eperm, exits, killed):
    proc = Proc(exits)
    proc.polls = 1  # _stop signals without polling first
    mcp_media._stop(proc)
    assert eperm == [(4242, signal.SIGKILL)] and proc.killed == killed and proc.waited == 1


def test_other_signal_errors_still_raise(monkeypatch):
    monkeypatch.setattr(mcp_transport.os, 'killpg', lambda pid, sig: (_ for _ in ()).throw(OSError(22, 'Invalid')))
    with pytest.raises(OSError):
        mcp_transport.terminate(Proc(False))


def test_the_hook_worker_teardown_tolerates_the_same_race():
    source = (hooks.__file__ and open(hooks.__file__, encoding='utf-8').read())
    teardown = source[source.index('os.killpg(process.pid, signal.SIGKILL)'):]
    teardown = teardown[:teardown.index('process.wait(timeout=2)')]
    assert 'except PermissionError:' in teardown and 'if process.poll() is None:' in teardown
    assert 'process.kill()' in teardown
