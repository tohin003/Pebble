"""Phase 24 (V1-005): AgentGuard state is released, and mirrors stay bounded.

0.6.1 keyed the guard by (root, run id): every project run copied the project into a new mirror and the engine built a
new workspace and index for it, and nothing was ever freed until the backend exited (RSS 221 -> 1,501 MB over 40 runs
in a 3.2 MB, 400-file project; about 3.4 MB of disk per run). Mirrors a crashed service left behind were never removed.
Now one mirror per root is synced incrementally and the four most recently used are kept (Guard.evict drops the
engine's workspace), a run that reaches a terminal state ends its session (controller.release_guard), and the next
service removes leftovers at start.

The App, runner, controller, GuardAdapter and vendored engine are the checked-in code; only the model is scripted.
"""

import copy
import gc
import json
import resource
import time
import tracemalloc
from types import SimpleNamespace

import pytest

from pebble.app import App
from pebble.guard import MAX_MIRRORS, GuardAdapter
from pebble.permissions import DEFAULT
from pebble.policy import Workspace
from pebble.providers import Providers

RUNS = 40


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


def project(root, count=400, size=8000):
    root.mkdir(parents=True)
    body = '# ' + 'x' * 70 + '\n'
    for n in range(count):
        path = root / 'pkg' / f'mod_{n:03d}.py'
        path.parent.mkdir(exist_ok=True)
        path.write_text(f'VALUE_{n} = {n}\n' + body * (size // len(body)))
    (root / 'notes.md').write_text('# Notes\n')
    return root


def tree_bytes(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file()) if path.exists() else 0


@pytest.fixture
def env(tmp_path, monkeypatch):
    value = SimpleNamespace(project=project(tmp_path / 'project'), home=tmp_path / 'data', script=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        response = copy.deepcopy(value.script.pop(0) if value.script else final('Done.'))
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)
    value.app = App(value.home, tmp_path / 'resources', lambda *args: None)
    assert value.app.guard.guard is not None, value.app.guard.error
    value.app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'auto'})
    yield value
    value.app.active = value.app.active_run = None
    value.app.close()


def send(env, cid, n):
    env.script = [call('Read', {'file_path': 'notes.md'}, f'read-{n}'),
                  call('Write', {'file_path': 'notes.md', 'content': f'# Notes\nrun {n}\n'}, f'write-{n}'),
                  final(f'Noted run {n}.')]
    before = {r['id'] for r in env.app.runs.list(cid)}
    env.app.send(cid, f'Add a line for run {n} to notes.md.')
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        runs = [r for r in env.app.runs.list(cid) if r['id'] not in before]
        if runs and runs[0]['state'] == 'completed' and env.app.active is None and env.app.controller.live is None:
            time.sleep(0.02)
            return runs[0]['id']
        time.sleep(.01)
    pytest.fail(f'Run {n} did not complete: {env.app.runs.list(cid)[0]}')


def rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # peak bytes on macOS: a leak only ever raises it


def test_forty_runs_in_a_400_file_project_keep_memory_and_mirrors_flat(env):
    app, guard = env.app, env.app.guard
    chat = app.new_chat(str(env.project))
    mirrors = env.home / 'guard-projects'
    for n in range(5):
        send(env, chat['id'], n)
    gc.collect()
    warm_rss, warm_disk = rss(), tree_bytes(mirrors)
    assert warm_disk > 3_000_000, 'the one mirror holds the project'
    tracemalloc.start()
    ids = [send(env, chat['id'], n) for n in range(5, RUNS)]
    gc.collect()
    retained, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # The Python heap the 35 later runs left behind (0.6.1 kept a workspace and an index per run).
    assert retained < 8_000_000, retained
    assert rss() <= warm_rss * 1.10, (warm_rss, rss())
    print(f'35 runs after warm-up: {retained} bytes of Python heap retained, mirrors {tree_bytes(mirrors)} bytes '
          f'(warm {warm_disk}), peak RSS {rss()} (warm {warm_rss})')
    assert tree_bytes(mirrors) <= warm_disk * 1.10 and len(list(mirrors.iterdir())) == 1
    # Per-run state is released: one engine workspace, no live task, no draft or prompt held for an ended run.
    assert len(guard.guard._workspaces) == 1
    [ws] = guard.guard._workspaces.values()
    assert ws._tasks == {} and ws._session_task_ids == {} and ws._session_tasks == {}
    assert guard.roots == {} and guard.overlays == {} and guard.prompts == {} and guard.recipe_runs == {}
    assert set(guard.tasks) >= {(str(Workspace(env.project).root), rid) for rid in ids[-5:]}  # ids only, bounded
    assert (env.project / 'notes.md').read_text() == f'# Notes\nrun {RUNS - 1}\n'


def test_mirrors_a_crashed_service_left_are_removed_at_start(tmp_path):
    home = tmp_path / 'data'
    leftover = home / 'guard-projects' / ('a' * 24)
    leftover.mkdir(parents=True)
    (leftover / 'config.py').write_text('TOKEN = "fixture-not-a-secret"\n')
    guard = GuardAdapter(home)
    try:
        assert not (home / 'guard-projects').exists()
    finally:
        guard.close()


def test_an_open_adapter_on_the_same_home_keeps_its_mirrors(tmp_path):
    root = project(tmp_path / 'p', count=3, size=100)
    first = GuardAdapter(tmp_path / 'data')
    try:
        assert first.begin(Workspace(root).root, 'r1', 'Explain.')['state'] == 'started'
        mirror = first.roots[(str(Workspace(root).root), 'r1')]
        second = GuardAdapter(tmp_path / 'data')
        second.close()
        assert (mirror / 'pkg' / 'mod_000.py').exists()
    finally:
        first.close()


def test_the_vendored_engine_records_every_modification_with_a_notice_and_no_local_path():
    """Phase 24 (V1-143): Apache-2.0 §4(b). Every file that differs from the upstream snapshot carries a 'Modified for
    Pebble' header and is listed with its current hash; the record names no local path."""
    import hashlib
    from pathlib import Path
    vendor = Path(__file__).resolve().parents[1] / 'vendor'
    record = json.loads((vendor / 'PROVENANCE.json').read_text())
    text = json.dumps(record)
    assert '/Users/' not in text and 'Projects/' not in text and not record.get('upstream_url')
    modified = {}
    for name, upstream in record['files'].items():
        data = (vendor / 'agentguard' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != upstream:
            modified[name] = hashlib.sha256(data).hexdigest()
            assert data.splitlines()[0].startswith(b'# Modified for Pebble'), name
    assert set(modified) == set(record['modified_files'])
    for name, entry in record['modified_files'].items():
        assert entry['pebble_sha256'] == modified[name] and entry['upstream_sha256'] == record['files'][name], name
    on_disk = {str(p.relative_to(vendor / 'agentguard')) for p in (vendor / 'agentguard').rglob('*.py')}
    assert on_disk == set(record['files'])


def test_only_the_four_most_recently_used_roots_keep_a_mirror(tmp_path):
    guard = GuardAdapter(tmp_path / 'data')
    roots = [Workspace(project(tmp_path / f'p{n}', count=3, size=100)).root for n in range(MAX_MIRRORS + 2)]
    try:
        for n, root in enumerate(roots):
            assert guard.begin(root, f'run-{n}', 'Explain the package.')['state'] == 'started'
        assert list(guard.mirrors) == [str(r) for r in roots[-MAX_MIRRORS:]]
        assert len(list((tmp_path / 'data' / 'guard-projects').iterdir())) == MAX_MIRRORS
        assert len(guard.guard._workspaces) <= MAX_MIRRORS
        assert not [k for k in guard.roots if k[0] in {str(r) for r in roots[:2]}]
        # An evicted root comes back as a fresh mirror; a resumed run on it rebuilds its task from receipts.
        assert guard.begin(roots[0], 'run-0', 'Explain.', resume=True, changed=['pkg/mod_000.py'])['state'] == 'rebuilt'
        assert guard.guard.evict(guard.mirrors[str(roots[0])].path) is True
        assert guard.guard.evict(tmp_path / 'never-seen') is False
    finally:
        guard.close()
    assert not (tmp_path / 'data' / 'guard-projects').exists() or not list((tmp_path / 'data' / 'guard-projects').iterdir())
