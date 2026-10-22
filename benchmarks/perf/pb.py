"""PB1-PB5: host-side latency, store scale, idle CPU and memory over runs. Scripted model, synthetic homes.

- PB1 send -> first provider request, by project size (the real App on a scripted ChatGPT Responses route).
- PB2 host time per step: the gap between consecutive provider requests on a 40-step Read run (the scripted provider
  answers at once, so the gap is the host's), fresh and with prior applied edits, at 32K and 128K.
- PB3 store scale over JSON-RPC (backend/main.py on a structured scratch home): `state`, the Documents copy pass
  (`storage_refresh`) and `stop`, each answered, by number of chats.
- PB4 idle CPU of the backend process (libproc counters) over a window, by number of chats.
- PB5 resident memory and AgentGuard mirrors (guard-projects) over repeated runs in one project.
Only Providers.http (or the backend's loopback-free idle) is scripted: no model, network or credential. Numbers are
host costs on this machine, never model latency or quality.
"""

import json
import os
import sqlite3
import statistics
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def _script(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def summary(values):
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return {'n': 0, 'median': None, 'p95': None, 'max': None}
    p95 = values[max(0, -(-95 * len(values) // 100) - 1)]
    return {'n': len(values), 'median': round(statistics.median(values), 4), 'p95': round(p95, 4),
            'max': round(values[-1], 4)}


# -- a scripted ChatGPT Responses route on the real App -------------------------------------------------------------------

def profile(budget=32768):
    return {'id': 'luna', 'name': 'Luna', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
            'model': 'gpt-6-luna', 'context': max(budget, 131072), 'budget': budget, 'tools': True,
            'context_mode': 'custom', 'budget_version': 2, 'context_version': 1, 'connection_id': 'luna'}


class Lines:
    def __init__(self, events):
        self.lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def readline(self, limit=-1):
        return self.lines.pop(0) if self.lines else b''


def events(text=None, call=None):
    item = ({'type': 'function_call', 'id': 'fc_%s' % call[0], 'call_id': call[0], 'name': call[1],
             'arguments': json.dumps(call[2]), 'status': 'completed'} if call else
            {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
             'content': [{'type': 'output_text', 'text': text}]})
    shell = {'status': 'in_progress', 'output': []}
    out = [{'type': 'response.created', 'response': shell}]
    if text is not None:
        out.append({'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'msg_1', 'delta': text})
    return out + [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                  {'type': 'response.completed', 'response': {'status': 'completed', 'output': [],
                                                              'usage': {'input_tokens': 900, 'output_tokens': 9}}}]


class Route:
    """Providers.http stand-in: records when each request arrives and answers from ``reply(number)``."""

    def __init__(self, reply):
        self.reply, self.times = reply, []

    def open(self, req, timeout=None):
        self.times.append(time.perf_counter())
        return Lines(self.reply(len(self.times)))


def scripted_app(home, budget=32768, reply=None):
    from pebble.app import App
    app = App(home, home.parent / 'resources', lambda *a, **k: None)
    app.store.set_setting('model_path', '')
    app.store.set_setting('model_profiles', [profile(budget)])
    app.model.select('luna')
    app.model.auth = SimpleNamespace(access=lambda pid: 'scripted-token')
    route = Route(reply or (lambda number: events('Done.')))
    app.model.http = route
    app.store.set_setting('permissions', {'mode': 'auto', 'tools': {
        'files': True, 'edits': True, 'tests': True, 'apps': False, 'web': False, 'terminal': False,
        'plugins': False, 'views': False, 'screen': False}})
    return app, route


def make_project(folder, files):
    """A synthetic Python project of ``files`` files (modules in a few packages, plus a test)."""
    folder.mkdir(parents=True)
    for index in range(max(1, files - 1)):
        path = folder / ('pkg%d' % (index % 8)) / ('mod_%04d.py' % index)
        path.parent.mkdir(exist_ok=True)
        path.write_text('def value_%d(x):\n    return x + %d\n' % (index, index))
    (folder / 'test_mod.py').write_text('import unittest\n\n\nclass T(unittest.TestCase):\n'
                                         '    def test_one(self):\n        self.assertTrue(True)\n')
    return folder


def settle(app, rid, seconds=60):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        run = app.runs.get(rid)
        if run['state'] not in ('queued', 'running', 'retry_wait') and app.active is None:
            return run['state']
        time.sleep(0.005)
    raise TimeoutError('the run did not settle')


def _close(app):
    app.active = app.active_run = None
    app.close()


def pb1(root, sizes=(50, 400, 1500), samples=20):
    """Send -> first provider request by project size (seconds)."""
    out = {}
    for size in sizes:
        project = make_project(Path(root) / ('pb1-%d' % size) / 'project', size)
        app, route = scripted_app(project.parent / 'home')
        try:
            values = []
            for _ in range(samples):
                cid = app.new_chat(str(project))['id']
                sent = time.perf_counter()
                count = len(route.times)
                rid = app.send(cid, 'Which module defines value_3?')['run_id']
                settle(app, rid)
                values.append(route.times[count] - sent)
            out[str(size)] = summary(values)
        finally:
            _close(app)
    return out


def pb2(root, steps=40, prior_edits=(0, 300, 1000), budgets=(32768, 131072)):
    """Host seconds between consecutive provider requests on a Read-only run."""
    out = {}
    for budget in budgets:
        for prior in prior_edits:
            project = make_project(Path(root) / ('pb2-%d-%d' % (budget, prior)) / 'project', 20)

            def reply(number, steps=steps):
                if number <= steps:
                    return events(call=('call_%d' % number, 'Read', {'file_path': 'pkg0/mod_0000.py'}))
                return events('Done.')
            app, route = scripted_app(project.parent / 'home', budget, reply)
            try:
                cid = app.new_chat(str(project))['id']
                if prior:
                    with app.store.lock, app.store.db as db:
                        db.executemany('INSERT INTO actions VALUES(?,?,?,?,?,?)', [
                            (uuid.uuid4().hex, cid, 'edit', 'applied', json.dumps(
                                {'path': 'pkg0/mod_%04d.py' % (i % 19), 'root': str(project), 'after': 'x = %d\n' % i}),
                             time.time() - i) for i in range(prior)])
                rid = app.send(cid, 'Read pkg0/mod_0000.py repeatedly and tell me what it returns.',
                               limits={'max_steps': steps + 4, 'max_seconds': 600})['run_id']
                settle(app, rid, 600)
                gaps = [b - a for a, b in zip(route.times, route.times[1:])]
                out['%dK-%d' % (budget // 1024, prior)] = summary(gaps)
            finally:
                _close(app)
    return out


def pb5(root, runs=40, files=400):
    """Resident memory (phys_footprint, MB) and AgentGuard mirrors after each run in one project."""
    measure = _script('measure_resources')
    libproc = measure.LibProc()
    project = make_project(Path(root) / 'pb5' / 'project', files)
    reply = lambda number: (events(call=('c%d' % number, 'Read', {'file_path': 'test_mod.py'}))  # noqa: E731
                            if number % 2 else events('Done.'))
    app, route = scripted_app(project.parent / 'home', reply=reply)
    series = []
    try:
        cid = app.new_chat(str(project))['id']
        for _ in range(runs):
            rid = app.send(cid, 'Read test_mod.py and summarise it.')['run_id']
            settle(app, rid)
            usage = libproc.rusage(os.getpid()) if libproc.available else None
            mirrors = app.home / 'guard-projects'
            folders = [p for p in mirrors.iterdir() if p.is_dir()] if mirrors.is_dir() else []
            series.append({'rss_mb': round(usage['phys_footprint'] / 2 ** 20, 1) if usage else None,
                           'mirrors': len(folders),
                           'mirror_bytes': sum(f.stat().st_size for d in folders for f in d.rglob('*') if f.is_file())})
    finally:
        _close(app)
    first, last = series[0], series[-1]
    growth = (None if not first['rss_mb'] or last['rss_mb'] is None
              else round((last['rss_mb'] - first['rss_mb']) / first['rss_mb'], 3))
    return {'runs': runs, 'files': files, 'series': series, 'rss_growth': growth,
            'max_mirrors': max(s['mirrors'] for s in series), 'targets': {'rss_within': 0.10, 'mirrors_per_root': 1}}


# -- the backend over JSON-RPC (PB3, PB4) ---------------------------------------------------------------------------------

def _seed(data, chats, messages=10):
    db = sqlite3.connect(data / 'pebble.sqlite')
    try:
        now = time.time()
        ids = [uuid.uuid4().hex for _ in range(chats)]
        db.executemany('INSERT INTO chats(id,title,workspace,updated) VALUES(?,?,?,?)',
                       [(cid, 'Seeded chat %d' % i, None, now - i * 60) for i, cid in enumerate(ids)])
        db.executemany('INSERT INTO messages(id,chat_id,role,content,created) VALUES(?,?,?,?,?)',
                       [(uuid.uuid4().hex, cid, 'user' if turn % 2 == 0 else 'assistant',
                         'Synthetic message %d in chat %d about invoices, gardens and trains.' % (turn, i),
                         now - i * 60 + turn) for i, cid in enumerate(ids) for turn in range(messages)])
        db.commit()
    finally:
        db.close()


class ScratchHome:
    """A structured Pebble home in a fake HOME (HOME is switched only while the backend starts)."""

    def __init__(self, folder):
        self.user = Path(folder) / 'user'
        self.home = self.user / '.pebble'
        self.home.mkdir(parents=True)
        self.trap = None

    def backend(self, measure):
        """The backend as the shell starts it, with the egress tripwire's proxy variables (attempts are recorded)."""
        saved = os.environ.get('HOME')
        os.environ['HOME'] = str(self.user)
        self.trap = self.trap or measure.EgressTrap().start()
        try:
            return measure.Backend([sys.executable, str(ROOT / 'backend' / 'main.py')], self.home, trap=self.trap)
        finally:
            if saved is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = saved


def _timed(backend, method, count, **params):
    values = []
    for _ in range(count):
        began = time.perf_counter()
        backend.rpc(method, seconds=120, **params)
        values.append(time.perf_counter() - began)
    return summary(values)


def pb3(root, chats=(150, 500, 1000, 1500), samples=20):
    """Seconds for `state`, the copy pass (`storage_refresh`) and `stop` to be answered, by number of chats."""
    measure = _script('measure_resources')
    out = {}
    for count in chats:
        scratch = ScratchHome(Path(root) / ('pb3-%d' % count))
        first = scratch.backend(measure)
        first.rpc('state', seconds=120)
        first.close()
        _seed(scratch.home / 'data', count)
        backend = scratch.backend(measure)
        try:
            backend.rpc('state', seconds=120)
            out[str(count)] = {'state': _timed(backend, 'state', samples),
                               'copy_pass': _timed(backend, 'storage_refresh', max(3, samples // 4)),
                               'stop': _timed(backend, 'cancel', samples),  # the shell's Stop is `cancel`
                               'new_chat': _timed(backend, 'new_chat', samples)}
        finally:
            backend.close()
            out.setdefault(str(count), {})['egress_attempts'] = scratch.trap.report()['attempts']
            scratch.trap.stop()
    out['targets'] = {'at_1000': {'copy_pass_s': 0.5, 'state_s': 0.05, 'stop_s': 1.0}}
    return out


def pb4(root, chats=(1000,), window=40.0, warmup=5.0):
    """Percent of one core the idle backend uses over ``window`` seconds, after ``warmup``."""
    measure = _script('measure_resources')
    libproc = measure.LibProc()
    out = {}
    for count in chats:
        scratch = ScratchHome(Path(root) / ('pb4-%d' % count))
        first = scratch.backend(measure)
        first.rpc('state', seconds=120)
        first.close()
        _seed(scratch.home / 'data', count)
        backend = scratch.backend(measure)
        try:
            backend.rpc('state', seconds=120)
            time.sleep(warmup)
            before, began = libproc.rusage(backend.pid), time.monotonic()
            time.sleep(window)
            after, elapsed = libproc.rusage(backend.pid), time.monotonic() - began
            cpu = None
            if before and after:
                used = (after['user_time_ns'] + after['system_time_ns'] - before['user_time_ns'] - before['system_time_ns'])
                cpu = round(100 * used / 1e9 / elapsed, 3)
            out[str(count)] = {'cpu_percent_of_core': cpu, 'window_seconds': round(elapsed, 2)}
        finally:
            backend.close()
            out.setdefault(str(count), {})['egress_attempts'] = scratch.trap.report()['attempts']
            scratch.trap.stop()
    out['targets'] = {'cpu_percent_of_core_at_1000_max': 1.0}
    return out


DELEGATED = {
    'PB6': 'cold start and service start to the first state: scripts/measure_resources.py run --scenarios r3 '
           '--backend <frozen candidate> (and hyperfine, parent-installed) -- T8 cold start p95 <= 5 s',
    'PB7': 'memory projection at 20k memories and forget latency: backend/tests/test_context_latency_22.py '
           '(1-3 versions per slot, i6-perf-12) -- p95 <= 300 ms',
    'PB8': 'context composition, first-request tokens, prefix reuse and uncached input: scripts/measure_context_cost.py '
           'on the scripted corpus -- T9 published',
    'PB9': 'reply presentation: node --test on the stream reducer, plus the parent\'s Lighthouse desktop runs in both '
           'themes and bundle sizes -- one event per presentation',
    'PB10': 'live headers, first event and first visible text: the live claim-set records after the go-ahead',
}
