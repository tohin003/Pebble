"""Phase 24 (V1-031): AgentGuard never sits between the person's message and the first model request.

0.6.1 re-copied the project into a fresh mirror for every message and then waited up to five seconds for a new index
in SessionStart, holding the adapter's process-wide lock: 2.0 s at 400 files and a 6.7 s median at 1,500 before the
first request (T8 asks for p95 <= 3 s), and an unrelated guard call waited 4.81 s behind another run's index. Now the
root's one mirror is synced incrementally, the index builds in the background, and only the first mutating check and
the stop gate wait for it, outside the adapter's lock.

The App, runner, controller, GuardAdapter and vendored engine are the checked-in code; only the model is scripted.
Timings are scripted (a fixture project on this machine), not the person's projects.
"""

import copy
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from pebble.app import App
from pebble.guard import VENDOR, GuardAdapter
from pebble.permissions import DEFAULT
from pebble.policy import Workspace
from pebble.providers import Providers

if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)


def project(root, count, size=600):
    root.mkdir(parents=True)
    line = '# ' + 'y' * 60 + '\n'
    for n in range(count):
        path = root / f'pkg{n // 100:02d}' / f'mod_{n:04d}.py'
        path.parent.mkdir(exist_ok=True)
        path.write_text(f'def value_{n}():\n    return {n}\n' + line * (size // len(line)))
    return root


@pytest.fixture
def env(tmp_path, monkeypatch):
    value = SimpleNamespace(project=project(tmp_path / 'project', 1500), first=[], sent=None)

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.first.append(time.monotonic())
        token('It returns its own number.')
        return {'role': 'assistant', 'content': 'It returns its own number.'}

    monkeypatch.setattr(Providers, 'complete', complete)
    value.app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    assert value.app.guard.guard is not None, value.app.guard.error
    value.app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'auto'})
    yield value
    value.app.active = value.app.active_run = None
    value.app.close()


def first_request_after_send(env, cid, text):
    env.first.clear()
    began = time.monotonic()
    env.app.send(cid, text)
    deadline = began + 30
    while not env.first and time.monotonic() < deadline:
        time.sleep(0.002)
    assert env.first, 'the model was never asked'
    elapsed = env.first[0] - began
    while env.app.active is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    return elapsed


@pytest.mark.parametrize('index_seconds', [0, 3])
def test_send_to_first_model_request_stays_under_a_second_at_1500_files(env, monkeypatch, index_seconds):
    """index_seconds 3 stands for a project whose index takes that long to build (0.6.1 waited up to five)."""
    from agentguard.repo.index import RepoIndex
    original = RepoIndex.build

    def build(self):
        time.sleep(index_seconds)
        return original(self)
    monkeypatch.setattr(RepoIndex, 'build', build)
    chat = env.app.new_chat(str(env.project))
    first = first_request_after_send(env, chat['id'], 'What does value_7 in pkg00/mod_0007.py return?')
    second = first_request_after_send(env, chat['id'], 'And value_8?')
    assert first < 1.0 and second < 1.0, (first, second)
    mirror = env.app.guard.mirrors[str(Workspace(env.project).root)]
    assert mirror.ready.wait(30) and not mirror.broken  # the index was built in the background meanwhile
    print(f'send to first model request at 1,500 files (index {index_seconds} s): first {first * 1000:.0f} ms, '
          f'second {second * 1000:.0f} ms')


def test_an_unrelated_guard_call_never_waits_on_another_runs_index(tmp_path, monkeypatch):
    from agentguard.repo.index import RepoIndex
    slow_root = Workspace(project(tmp_path / 'slow', 20)).root
    fast_root = Workspace(project(tmp_path / 'fast', 20)).root
    original = RepoIndex.build
    release = threading.Event()
    guard = GuardAdapter(tmp_path / 'data')
    slow_mirror = guard._mirror_path(str(slow_root)).resolve()

    def build(self):
        if self.root == slow_mirror:
            release.wait(10)  # this root's index takes as long as the test says
        return original(self)
    monkeypatch.setattr(RepoIndex, 'build', build)
    try:
        began = time.monotonic()
        assert guard.begin(slow_root, 'slow-run', 'Explain value_3.')['state'] == 'started'
        assert time.monotonic() - began < 1.0, 'begin never waits for the index'
        guard.stage(slow_root, 'slow-run', 'pkg00/mod_0003.py', 'def value_3():\n    return 3\n')
        guard.handle('post_tool_use', slow_root, 'slow-run', tool='Write',
                     arguments={'file_path': 'pkg00/mod_0003.py', 'content': 'def value_3():\n    return 3\n'})
        stopped = {}
        waiter = threading.Thread(target=lambda: stopped.update(
            guard.handle('stop', slow_root, 'slow-run', last_assistant_message='Done.')))
        waiter.start()
        time.sleep(0.2)
        assert waiter.is_alive(), 'the stop gate waits for its own index'
        timings = {}
        for name, call in (
                ('another root begins', lambda: guard.begin(fast_root, 'fast-run', 'Explain value_2.')),
                ('another root writes', lambda: guard.handle('pre_tool_use', fast_root, 'fast-run', tool='Write',
                                                             arguments={'file_path': 'pkg00/mod_0001.py', 'content': 'x = 1\n'})),
                ('a read on the slow root', lambda: guard.handle('pre_tool_use', slow_root, 'slow-run', tool='Read',
                                                                 arguments={'file_path': 'pkg00/mod_0001.py'}))):
            started = time.monotonic()
            call()
            timings[name] = time.monotonic() - started
        assert all(seconds < 0.5 for seconds in timings.values()), timings
        assert waiter.is_alive()
        release.set()
        waiter.join(20)
        assert stopped.get('action') == 'allow', stopped  # no tests cover the changed module: nothing to demand
        quiet = time.monotonic()
        assert guard.handle('stop', fast_root, 'fast-run', last_assistant_message='Explained.')['action'] == 'allow'
        assert time.monotonic() - quiet < 0.5, 'a run that changed nothing never waits for an index'
    finally:
        release.set()
        guard.close()
