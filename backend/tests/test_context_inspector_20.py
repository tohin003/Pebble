"""Phase 20 track C20-context: the person's context inspector and between-turns compaction (frontier gap C09).

Every run here is a real App run; only the model call is scripted. The scripted route projects each request as
Providers._complete does for a chat route (Providers.project_context, counting the wire form with TokenBudget), so
the run journal holds the same `context_projected` manifests and the same automatic compaction as in the product,
and the test sees exactly the request the provider would have sent. Nothing reaches a model or the network.

Until the parent wires the routes (app.py dispatch, tool_authz.NATIVE_ONLY, main.rs), `route` calls the module's
own route table with the same arguments; once wired, the same calls go through App.dispatch. The shell gates run
on main.rs with the proposed patch applied to a copy of its text until the real file carries the wiring.
"""
import copy
import json
import math
import re
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import context_inspector as ci
from pebble import runtime, tool_authz
from pebble.app import App
from pebble.compaction import SPINE_NAME
from pebble.context_manifest import assemble_context
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError
from pebble.providers import calibrated
from pebble.responses import input_items
from pebble.token_budget import ByteEstimate, TokenBudget
from pebble.tool_authz import Actor
from pebble.tool_catalogue import TOOLS
from test_high_fd_waits_21 import close_guard_databases

ROOT = Path(__file__).resolve().parents[2]
MAIN_RS = ROOT / 'src-tauri' / 'src' / 'main.rs'
ESTIMATOR = TokenBudget(ROOT / 'src-tauri' / 'resources')
TEXT = 'Read parser.py and notes.md, then summarize the tokenizer section for me.'
# The parent's main.rs patch (track note): (anchor, replacement), each anchor present exactly once.
MAIN_RS_PATCH = (
    ('"rewind_preview","rewind_apply"];', '"rewind_preview","rewind_apply","context_inspect","context_compact"];'),
    ('if method=="quiz_share" || method=="rewind_preview" || method=="rewind_apply" {',
     'if method=="quiz_share" || method=="rewind_preview" || method=="rewind_apply" || method=="context_inspect" '
     '|| method=="context_compact" {'),
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded(original):
        def attempt(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ('127.0.0.1', '::1'):
                raise AssertionError('Context inspector tests must not open network connections.')
            return original(self, address)
        return attempt
    monkeypatch.setattr(socket.socket, 'connect', guarded(connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded(connect_ex))


def profile(budget, kind='openai'):
    # The context is twice the budget, so the projected window is the route budget itself. budget_version keeps
    # the explicit budget (Providers.profiles otherwise sets it to the whole context).
    return {'id': f'route{budget}', 'kind': kind, 'name': 'Route', 'url': 'https://scripted.invalid/v1',
            'model': 'scripted', 'context': budget * 2, 'budget': budget, 'budget_version': 2, 'tools': True}


@pytest.fixture
def hosts(tmp_path):
    made = []

    def make(budget, name='host', files=None, history=(), memory=None, kind='openai'):
        base = tmp_path / name
        project = base / 'project'
        project.mkdir(parents=True)
        for path, text in (files or default_files()).items():
            (project / path).write_text(text)
        app = App(base / 'data', base / 'resources', lambda *args: None)
        made.append(app)
        assert app.controller.close(), 'approvals must never auto-continue a run behind the test'
        app.store.set_setting('model_path', '')
        app.store.set_setting('model_profiles', [profile(budget, kind)])
        app.model.select(f'route{budget}')
        app.store.set_setting('memory_enabled', True)
        tools = app.store.setting('permissions', DEFAULT)['tools']
        app.setting('permissions', {'mode': 'auto', 'tools': {n: n != 'screen' for n in tools}})
        chat = app.new_chat(str(project))
        for index, (role, content) in enumerate(history):
            app.store.message(chat['id'], role, content, f'history-{index}')  # fixed IDs: two Apps' requests compare
        if memory:
            app.memory_save(kind='semantic', content=memory)
        return SimpleNamespace(app=app, root=project, cid=chat['id'], budget=budget,
                               inspector=getattr(app, 'context_inspector', None) or ci.ContextInspector(app))
    yield make
    for app in made:
        app.active = app.active_run = None
        app.close()
    close_guard_databases(tmp_path)  # resource cleanup only (F21-fd)


def default_files():
    body = ''.join(f'def rule_{i}(text):\n    """Tokenizer rule {i} for quoted strings."""\n    return text.split()\n\n'
                   for i in range(24))
    return {'parser.py': body, 'notes.md': '# Notes\n\n' + 'The tokenizer keeps quoted strings whole. ' * 40}


def call(ident, name, **args):
    return {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


class Route:
    """A scripted chat route: each request is projected as Providers._complete projects it (window, reserve and
    TokenBudget counter of the wire form), recorded as sent, then answered with the next scripted tool calls."""

    def __init__(self, app, *steps, during=None, final='Done.'):
        self.app, self.steps, self.during, self.final = app, list(steps), during, final
        self.requests = []
        app.model.complete = self

    def __call__(self, messages, tools, cancel, token, effort=False):
        model = self.app.model
        p = model.current()
        tools = tools if p['tools'] else []
        max_output = min(1024, p['context'] // 2)
        factor = model.calibration_factor(p)
        sent = model.project_context(
            messages, tools, min(p.get('budget', p['context']), p['context'] - max_output),
            counter=lambda ms, ts: model.token_budget.count(model.wire_context(ms), ts, **calibrated(factor)),
            method='o200k_local_estimate')
        self.requests.append({'messages': copy.deepcopy(sent), 'tools': copy.deepcopy(tools)})
        if self.during:
            self.during(len(self.requests))
        if self.steps:
            return {'role': 'assistant', 'tool_calls': self.steps.pop(0)}
        token(self.final)
        return {'role': 'assistant', 'content': self.final}


class ResponsesRoute(Route):
    """As Route, for a Responses route with a local estimate (complete_responses for a ChatGPT sign-in route): the
    counter sees Responses input items and the reshaped tools."""

    def __call__(self, messages, tools, cancel, token, effort=False):
        model = self.app.model
        p = model.current()
        shaped = [{'type': 'function', **t['function'], 'strict': False} for t in tools]
        factor = model.calibration_factor(p)
        sent = model.project_context(
            messages, tools, min(p.get('budget', p['context']), p['context'] - 1024),
            counter=lambda ms, ts: model.token_budget.count(input_items(ms), shaped, **calibrated(factor)),
            method='o200k_local_estimate')
        self.requests.append({'messages': copy.deepcopy(sent), 'tools': copy.deepcopy(tools),
                              'input': input_items(sent), 'shaped': shaped})
        if self.steps:
            return {'role': 'assistant', 'tool_calls': self.steps.pop(0)}
        token(self.final)
        return {'role': 'assistant', 'content': self.final}


def no_model(*args, **kwargs):
    raise AssertionError('Inspecting or compacting context never calls the model.')


def work(host, text=TEXT, limits=None):
    app = host.app
    chat = app.chat(host.cid)
    rid = app.runs.create(host.cid, text, app.runtime_config(chat, False, limits), [])
    app.work(chat, text, False, [], run_id=rid)
    assert app.runs.get(rid)['state'] not in runtime.LIVE
    return rid


def route(host, method, **params):
    """A native route through App.dispatch once wired; until then the module's own route table, same arguments."""
    try:
        return host.app.dispatch(method, params)
    except ValueError as exc:
        if str(exc) != 'Unknown request.':
            raise
    return host.inspector.methods()[method](**params)


def inspect(host, run_id=None, surface='main'):
    params = {'chat_id': host.cid, 'surface': surface}
    if run_id is not None:
        params['run_id'] = run_id
    return route(host, 'context_inspect', **params)


def compact(host, surface='main'):
    return route(host, 'context_compact', chat_id=host.cid, surface=surface)


def snapshot(app, skip=()):
    """Every row of every data table (minus `skip`), for a "writes nothing" comparison."""
    with app.store.lock:
        names = [n for (n,) in app.store.db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                    "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'")]
        names = [n for n in names if not n.startswith('memory_search') and n not in skip]
        return {n: sorted(map(repr, app.store.db.execute(f'SELECT * FROM "{n}"').fetchall())) for n in names}


def by_id(items):
    return {item['id']: item for item in items}


def tokens(value):
    return len(TokenBudget.encoding().encode(json.dumps(value, ensure_ascii=False), disallowed_special=()))


def rust_list(source, pattern):
    return set(re.findall(r'"([a-z_]+)"', re.search(pattern, source, re.S).group(1)))


def events(app, rid, kind):
    return [e['payload'] for e in app.runs.events(rid, 0, 200) if e['kind'] == kind]


READS = [[call('r1', 'Read', file_path='parser.py')], [call('r2', 'Read', file_path='notes.md')]]
HISTORY = (('user', 'Earlier: keep the summary under ten lines.'), ('assistant', 'Understood, short summaries.'))
MEMORY = 'The tokenizer section of parser.py keeps quoted strings whole.'


# 1. The categories sum to the estimator's total ---------------------------------------------------------------

@pytest.mark.parametrize('budget', [4096, 8192, 32768])
def test_the_categories_sum_to_the_estimators_total_of_the_last_request(hosts, budget):
    host = hosts(budget, history=HISTORY, memory=MEMORY)
    script = Route(host.app, *copy.deepcopy(READS))
    rid = work(host)
    host.app.model.complete = no_model
    before = snapshot(host.app)
    started = time.perf_counter()
    view = inspect(host)
    elapsed = time.perf_counter() - started
    assert snapshot(host.app) == before, 'an inspection is a read: it writes nothing'
    last = script.requests[-1]
    record = host.app.runs.context_manifest(rid)['manifest']
    expected = ESTIMATOR.count(last['messages'], last['tools'])
    assert view['basis'] == 'last_request' and view['run_id'] == rid and view['counter'] == 'o200k_base'
    assert view['protocol'] == 'chat'
    # The runner's own count of the request it sent, the inspector's rebuild and the categories all agree.
    assert view['total'] == expected == record['input_tokens']['after'] == view['recorded']['input_tokens']
    assert sum(c['tokens'] for c in view['categories']) == view['total']
    assert view['calibration'] == {'multiplier': 1.0, 'tokens': 0} and view['used'] == view['total']
    assert view['window'] == {'tokens': record['limits']['input'], 'context': budget, 'output_reserve': 64,
                              'source': 'last_projection', 'route_budget': budget}
    assert view['free'] == view['window']['tokens'] - view['total'] and view['over'] == 0
    rows = by_id(view['categories'])
    assert list(rows) == list(ci.CATEGORIES)
    assert rows['framing']['tokens'] >= 64, 'the estimator margin and base framing are never negative'
    for name in ('system', 'tool_schemas', 'project_host', 'conversation', 'tool_results'):
        assert rows[name]['tokens'] > 0, name
    for name in ('project_host', 'conversation'):
        assert sum(p['tokens'] for p in rows[name]['parts']) == rows[name]['tokens']
        assert sum(p['messages'] for p in rows[name]['parts']) == rows[name]['messages']
    assert sum(r['messages'] for r in rows.values()) == len(last['messages'])
    assert rows['system']['messages'] == 1
    assert rows['tool_results']['messages'] == sum(m['role'] == 'tool' for m in last['messages']) >= 1
    assert rows['memory']['messages'] == record['summary']['memory']['selected_units']
    assert by_id(rows['conversation']['parts'])['request']['messages'] == 1
    # Tool schemas: every offered tool, by name, counted alone; the category is their sum.
    assert sorted(t['name'] for t in view['tools']) == sorted(t['function']['name'] for t in last['tools'])
    assert sorted(t['tokens'] for t in view['tools']) == sorted(tokens(t) for t in last['tools'])
    assert [t['tokens'] for t in view['tools']] == sorted((t['tokens'] for t in view['tools']), reverse=True)
    assert rows['tool_schemas']['tokens'] == sum(t['tokens'] for t in view['tools'])
    assert view['tools_basis'] == 'last_request' and view['tools_unidentified'] == 0
    # After the last request the checkpoint holds the final reply; it is counted apart, never in the request.
    assert view['since_last_request']['messages'] == 1 and view['since_last_request']['tokens'] > 0
    assert view['compact'] == {'available': False, 'reason': 'finished'}
    assert view['authorization'] == 'none' and view['inference'] == 'none' and view['omissions'] == []
    print(f"\n{budget}: total {view['total']} of {view['window']['tokens']} ({len(view['tools'])} tools, "
          f"{elapsed * 1000:.1f} ms to inspect); " + ', '.join(f"{r['id']} {r['tokens']}" for r in view['categories']))


def test_a_run_compacted_automatically_is_rebuilt_from_its_compacted_window(hosts):
    """At 4K the reads overflow the window, so the host's automatic compaction runs mid-run; the last request is
    still rebuilt exactly from the compacted checkpoint and its manifest."""
    big = {f'part{i}.md': f'Part {i}: quoted strings stay whole in the tokenizer. ' * 200 for i in range(8)}
    host = hosts(4096, files=big, history=HISTORY)
    script = Route(host.app, *[[call(f'r{i}', 'Read', file_path=f'part{i}.md')] for i in range(8)])
    rid = work(host)
    host.app.model.complete = no_model
    assert host.app.runs.compactions(rid), 'the scripted reads must overflow the 4K window'
    view = inspect(host)
    last = script.requests[-1]
    assert view['basis'] == 'last_request'
    assert view['total'] == ESTIMATOR.count(last['messages'], last['tools']) == view['recorded']['input_tokens']
    assert sum(c['tokens'] for c in view['categories']) == view['total'] and view['compactions'] >= 1
    assert view['free'] == view['window']['tokens'] - view['total'] >= 0


@pytest.mark.parametrize('budget', [8192, 32768])
def test_a_responses_route_is_counted_in_its_own_form(hosts, budget):
    """The person's ChatGPT sign-in route speaks Responses: its window counts input items and reshaped tools."""
    host = hosts(budget, history=HISTORY, memory=MEMORY, kind='responses')
    script = ResponsesRoute(host.app, *copy.deepcopy(READS))
    work(host)
    host.app.model.complete = no_model
    view = inspect(host)
    last = script.requests[-1]
    assert view['protocol'] == 'responses' and view['basis'] == 'last_request'
    assert view['total'] == ESTIMATOR.count(last['input'], last['shaped']) == view['recorded']['input_tokens']
    assert view['total'] != ESTIMATOR.count(last['messages'], last['tools']), 'the chat form is a different count'
    assert sum(c['tokens'] for c in view['categories']) == view['total'] and by_id(view['categories'])['framing']['tokens'] >= 64
    assert sorted(t['tokens'] for t in view['tools']) == sorted(tokens(t) for t in last['shaped'])
    assert by_id(view['categories'])['tool_results']['messages'] == sum(m['role'] == 'tool' for m in last['messages'])


def test_the_current_request_is_inspected_while_the_run_is_live_and_compaction_is_refused(hosts, monkeypatch):
    host = hosts(8192, history=HISTORY, memory=MEMORY)
    seen = {}
    compactions = []
    monkeypatch.setattr(host.app, 'run_compact', lambda *a, **k: compactions.append(a) or {'status': 'already_fits'})

    def during(step):
        if step != 2:
            return
        before = snapshot(host.app)  # the runner waits on this request, so nothing else writes now
        seen['view'] = inspect(host)
        assert snapshot(host.app) == before, 'a live inspection writes nothing'
        with pytest.raises(PolicyError, match='Compact between turns'):
            compact(host)
        seen['after'] = snapshot(host.app)
        assert seen['after'] == before, 'a refused compaction writes no receipt'
    script = Route(host.app, *copy.deepcopy(READS), during=during)
    rid = work(host)
    view, current = seen['view'], script.requests[1]
    assert view['run_id'] == rid and view['live'] is True and view['run_state'] == 'running'
    assert view['basis'] == 'last_request' and view['since_last_request'] == {'messages': 0, 'tokens': 0}
    assert view['total'] == ESTIMATOR.count(current['messages'], current['tools'])
    assert sum(c['tokens'] for c in view['categories']) == view['total']
    assert view['compact'] == {'available': False, 'reason': 'live'}
    assert compactions == [], 'the existing compaction path is never reached while a run is live'
    assert len(script.requests) == 3, 'the run continued normally after the inspection'


@pytest.mark.parametrize('budget', [4096, 32768])
def test_inspecting_and_a_refused_compaction_mid_run_leave_the_next_request_identical(hosts, budget):
    """Nothing reaches model context: a run inspected (and refused a compaction) at every step sends exactly the
    requests of the same run left alone, at 4K and above."""
    def requests(name, poke):
        host = hosts(budget, name=name, history=HISTORY)
        script = Route(host.app, *copy.deepcopy(READS), during=(lambda step: poke(host)) if poke else None)
        work(host)
        return [(r['messages'], r['tools']) for r in script.requests]

    def poke(host):
        assert inspect(host)['live'] is True
        with pytest.raises(PolicyError):
            compact(host)
    alone, inspected = requests('alone', None), requests('inspected', poke)
    assert len(alone) == len(inspected) == 3
    assert json.dumps(inspected, sort_keys=True) == json.dumps(alone, sort_keys=True)


# 2. No message body ever appears ------------------------------------------------------------------------------

CANARIES = ('zebracanary', 'quokkacanary', 'ibexcanary', 'okapicanary', 'tapircanary', 'lemurcanary', 'gecko_canary')


@pytest.mark.parametrize('budget', [4096, 32768])
def test_no_message_body_tool_text_or_argument_ever_appears_in_a_reply(hosts, budget):
    files = {'parser.py': 'SECRET = "okapicanary"\n' * 30, 'gecko_canary.md': 'tapircanary notes ' * 30}
    host = hosts(budget, files=files, history=(('user', 'Earlier I said quokkacanary matters.'),
                                               ('assistant', 'Noted the ibexcanary detail.')),
                 memory='The lemurcanary tokenizer keeps quoted strings whole.')
    steps = [[call('r1', 'Read', file_path='parser.py')],
             [call('r2', 'Read', file_path='gecko_canary.md'), call('g1', 'Grep', pattern='tapircanary')]]
    Route(host.app, *steps, final='Summary: zebracanary and okapicanary.')
    rid = work(host, 'Read parser.py and gecko_canary.md and explain zebracanary in the tokenizer.',
               limits={'max_steps': 2, 'max_seconds': 900})
    host.app.model.complete = no_model
    view = inspect(host)
    receipt = compact(host)
    journal = events(host.app, rid, ci.RECEIPT_EVENT)
    texts = [t['function']['description'] for t in TOOLS] + [json.dumps(t['function']['parameters']) for t in TOOLS]
    for reply in (view, receipt, journal):
        dumped = json.dumps(reply).lower()
        assert not [c for c in CANARIES if c in dumped], 'message text reached a reply'
        assert not [t for t in texts if len(t) > 40 and t.lower() in dumped], 'tool schema text reached a reply'
    allowed = vocabulary(host, rid) | {receipt['receipt_id']}
    for reply in (view, receipt):
        assert strings(reply) <= allowed, sorted(strings(reply) - allowed)


def vocabulary(host, rid):
    """Every string a reply may hold: identities, host names and labels; nothing from content."""
    parts = {p for names in ci.PARTS.values() for p in names}
    labels = {'last_request', 'checkpoint', 'none', 'eligible_offer', 'last_projection', 'route_budget',
              'o200k_base', 'byte_estimate', 'o200k_local_estimate', 'unnamed', 'context_compact', 'human', 'main',
              'chat', 'responses',
              'run_compact', 'unchanged', 'compacted', 'already_fits', 'fresh_window', 'retained_by_protected_projection',
              'protected_unit', 'absent', 'full', 'reduced', 'minimal', 'counts', 'calibration.route_not_current',
              'calibration.unavailable', 'tools.unidentified', 'window.unknown', 'live', 'not_latest', 'finished',
              'not_resumable', 'no_window', 'between_tools'}
    with host.app.store.lock:
        hashes = {r[0] for r in host.app.store.db.execute('SELECT manifest FROM run_compactions WHERE run_id=?', (rid,))}
    fingerprints = set()
    for manifest in hashes:
        manifest = json.loads(manifest)
        fingerprints |= {manifest['before_fingerprint'], manifest['after_fingerprint']}
    return ({host.cid, rid} | set(ci.CATEGORIES) | parts | labels | set(ci.STATUSES) | runtime.STATES
            | {t['function']['name'] for t in TOOLS} | fingerprints)


def strings(value):
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(strings(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(strings(v) for v in value)) if value else set()
    return set()


# 3. Compaction between turns ------------------------------------------------------------------------------------

def long_run(host, steps=4):
    """A run that stops at its step limit on a model boundary, after `steps` reads (budget_exhausted)."""
    for i in range(steps):
        (host.root / f'part{i}.md').write_text(f'Section {i}. ' + 'Quoted strings stay whole in the tokenizer. ' * 60)
    script = Route(host.app, *[[call(f'r{i}', 'Read', file_path=f'part{i}.md')] for i in range(steps)])
    rid = work(host, 'Read every part file and summarize the tokenizer.', limits={'max_steps': steps, 'max_seconds': 900})
    run = host.app.runs.get(rid)
    assert run['state'] == 'budget_exhausted' and run['checkpoint']['phase'] == 'model'
    return rid, script


@pytest.mark.parametrize('budget', [8192, 32768])
def test_compaction_between_turns_reaches_the_existing_path_and_the_next_boundary_starts_from_it(hosts, budget,
                                                                                                monkeypatch):
    host = hosts(budget, history=HISTORY, memory=MEMORY)
    rid, script = long_run(host)
    host.app.model.complete = no_model
    first = inspect(host)
    assert first['compact'] == {'available': True, 'reason': None} and first['run_state'] == 'budget_exhausted'
    stored = host.app.runs.get(rid)['checkpoint']
    reached = []
    original = host.app.run_compact

    def spy(*args, **kwargs):
        reached.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(host.app, 'run_compact', spy)
    kept = snapshot(host.app, skip=('runs', 'run_events', 'run_compactions'))
    receipt = compact(host)
    # The existing path, once, with a target below the route budget (half of the stored messages' tokens).
    assert len(reached) == 1 and reached[0][:2] == (host.cid, rid) and len(reached[0]) == 3
    target = reached[0][2]
    schemas = ESTIMATOR.count([], stored['available'])
    whole = ESTIMATOR.count([ci.wire(m) for m in stored['messages']], stored['available'])
    assert target == max(512, schemas + math.ceil((whole - schemas) / 2)) < host.app.model.current()['budget']
    assert receipt['status'] == 'compacted' and receipt['generation'] == 1 and receipt['target_reached'] is True
    assert receipt['target_tokens'] == target and receipt['path'] == 'run_compact' and receipt['journaled'] is True
    assert receipt['replayed'] == 0 and receipt['inference'] == 'none' and receipt['memory'] == 'unchanged'
    assert receipt['kept']['exact_human_instructions'] == 'retained_by_protected_projection'
    assert receipt['kept']['human_constraints'] == ('protected_unit' if budget > 4096 else 'absent')
    # What was compacted: the archive itself, by category, with the tokens freed.
    archive = host.app.runs.context_archive(rid, 1)['archive']
    by_category = receipt['archived']['by_category']
    assert receipt['archived']['messages'] == len(archive) == sum(c['messages'] for c in by_category.values()) > 0
    assert receipt['archived']['tool_results'] == by_category['tool_results']['messages'] >= 2
    assert receipt['tokens']['freed'] == receipt['tokens']['before'] - receipt['tokens']['after'] > 0
    assert receipt['tokens']['after'] <= target
    assert events(host.app, rid, ci.RECEIPT_EVENT) == [{k: v for k, v in receipt.items() if k not in ('journaled', 'context')}]
    assert [e['generation'] for e in events(host.app, rid, 'context_compacted')] == [1]
    # Nothing else changed: no tool, action, memory layer or other table; no model call.
    assert snapshot(host.app, skip=('runs', 'run_events', 'run_compactions')) == kept
    after = receipt['context']
    assert after['basis'] == 'checkpoint' and after['compactions'] == 1 and after['tools_basis'] == 'last_request'
    assert after['total'] < first['total'] and sum(c['tokens'] for c in after['categories']) == after['total']
    assert by_id(after['categories'])['tool_results']['messages'] < by_id(first['categories'])['tool_results']['messages']
    print(f"\n{budget}: compacted the stored window {receipt['tokens']['before']} -> {receipt['tokens']['after']} "
          f"(target {target}, budget {receipt['route_budget']}); archived {receipt['archived']['messages']} messages "
          + ', '.join(f"{k} {v['messages']}/{v['tokens']}" for k, v in by_category.items())
          + f"; inspected request {first['total']} -> stored window {after['total']}")
    # The next model boundary starts from the compacted window: resume the task with a larger step limit.
    run = host.app.runs.get(rid)
    host.app.store.query('UPDATE runs SET config=? WHERE id=?',
                         (json.dumps({**run['config'], 'limits': {'max_steps': 8, 'max_seconds': 900}}), rid))
    resumed = Route(host.app)
    resume = host.app.runs.resume_snapshot(rid)  # as App.run_resume: the snapshot, then the queued transition
    host.app.runs.transition(rid, 'queued')
    host.app.work(host.app.chat(host.cid), run['goal'], False, [], run_id=rid, resume=resume)
    request = resumed.requests[0]['messages']
    assert any(m.get('name') == SPINE_NAME for m in request), 'the evidence spine replaced the retired context'
    retired = {row['message'].get('tool_call_id') for row in archive if row['message'].get('role') == 'tool'}
    assert retired and not retired & {m.get('tool_call_id') for m in request}
    assert host.app.runs.get(rid)['state'] not in runtime.LIVE


def test_a_finished_task_is_not_compacted_and_the_receipt_says_why(hosts, monkeypatch):
    host = hosts(32768, history=HISTORY)
    Route(host.app, *copy.deepcopy(READS))
    rid = work(host)
    host.app.model.complete = no_model
    monkeypatch.setattr(host.app, 'run_compact', no_model)
    stored = host.app.runs.get(rid)['checkpoint']
    receipt = compact(host)
    assert receipt['status'] == 'fresh_window' and receipt['run_state'] == 'completed' and receipt['generation'] is None
    assert receipt['archived'] == {'messages': 0, 'tool_results': 0, 'by_category': {}}
    assert host.app.runs.get(rid)['checkpoint'] == stored and host.app.runs.compactions(rid) == []
    assert [r['receipt_id'] for r in events(host.app, rid, ci.RECEIPT_EVENT)] == [receipt['receipt_id']]
    assert receipt['context']['compact'] == {'available': False, 'reason': 'finished'}


def test_a_window_that_is_all_protected_falls_back_to_the_budget_and_retires_nothing(hosts, monkeypatch):
    host = hosts(32768)
    Route(host.app, [call('r1', 'Read', file_path='notes.md')])
    # A long request is protected (the exact current instruction), so half the stored messages is below the floor.
    text = 'Summarize notes.md. Constraints: ' + ' '.join(f'keep rule {i} exactly as written;' for i in range(400))
    rid = work(host, text, limits={'max_steps': 1, 'max_seconds': 900})
    host.app.model.complete = no_model
    assert host.app.runs.get(rid)['state'] == 'budget_exhausted'
    reached = []
    original = host.app.run_compact
    monkeypatch.setattr(host.app, 'run_compact', lambda *a, **k: reached.append(a) or original(*a, **k))
    stored = host.app.runs.get(rid)['checkpoint']
    receipt = compact(host)
    assert [len(a) for a in reached] == [3, 2], 'the target first, then run_compact\'s own budget target'
    assert receipt['status'] == 'already_fits' and receipt['target_reached'] is False and receipt['generation'] is None
    assert host.app.runs.get(rid)['checkpoint'] == stored and host.app.runs.compactions(rid) == []
    assert len(events(host.app, rid, ci.RECEIPT_EVENT)) == 1


def test_compaction_refusals(hosts):
    host = hosts(8192)
    with pytest.raises(PolicyError, match='no task window'):
        compact(host)
    view = inspect(host)
    assert view['basis'] == 'none' and view['run_id'] is None and view['compact']['available'] is False
    for bad in (None, '', 'x' * 129, 7):
        for method in ci.ROUTES:
            with pytest.raises(PolicyError, match='scoped conversation'):
                route(host, method, chat_id=bad, surface='main')
        if bad is not None:  # no run_id means the conversation's latest task
            with pytest.raises(PolicyError, match='scoped conversation'):
                route(host, 'context_inspect', chat_id=host.cid, run_id=bad, surface='main')
    with pytest.raises(ValueError, match='Conversation not found'):
        route(host, 'context_compact', chat_id='missing-chat', surface='main')
    with pytest.raises(ValueError, match='Conversation not found'):
        route(host, 'context_inspect', chat_id='missing-chat', surface='main')
    rid, _ = long_run(host, steps=2)
    host.app.model.complete = no_model
    other = host.app.new_chat(str(host.root))['id']
    with pytest.raises(ValueError, match='not found in this conversation'):
        route(host, 'context_inspect', chat_id=other, run_id=rid, surface='main')
    # Any live task refuses compaction, in this conversation or another.
    for active in (host.cid, other):
        host.app.active, host.app.active_run = active, 'live-run'
        with pytest.raises(PolicyError, match='Compact between turns'):
            compact(host)
        assert inspect(host)['compact'] == {'available': False, 'reason': 'live'}
    host.app.active = host.app.active_run = None
    host.app.store.query("UPDATE runs SET state='needs_reconciliation' WHERE id=?", (rid,))
    with pytest.raises(PolicyError, match='Reconcile'):
        compact(host)
    assert inspect(host)['compact']['reason'] == 'not_resumable'
    assert events(host.app, rid, ci.RECEIPT_EVENT) == [] and host.app.runs.compactions(rid) == []


# 4. The pet, generated views and models cannot reach the routes ----------------------------------------------------

def test_the_pet_cannot_reach_the_routes_and_neither_can_a_model_or_a_generated_view(hosts):
    host = hosts(8192)
    rid, _ = long_run(host, steps=2)
    host.app.model.complete = no_model
    before = snapshot(host.app)
    for surface in (None, 'pet', 'Main', 'main ', 'generated', '', 'view'):
        with pytest.raises(PolicyError, match='main window'):
            inspect(host, surface=surface)
        with pytest.raises(PolicyError, match='main window'):
            compact(host, surface=surface)
    assert snapshot(host.app) == before
    assert not {t['function']['name'].casefold() for t in TOOLS} & {r.casefold() for r in ci.ROUTES}, 'no model tool'
    for name in ('Pet.vue', 'components/PetRunSummary.vue'):
        source = (ROOT / 'src' / name).read_text()
        assert 'ContextPanel' not in source and 'context_inspect' not in source and 'context_compact' not in source
    wired = set(ci.ROUTES) <= tool_authz.NATIVE_ONLY
    native_only = tool_authz.NATIVE_ONLY if wired else tool_authz.NATIVE_ONLY | set(ci.ROUTES)
    source = MAIN_RS.read_text()
    if not set(ci.ROUTES) <= rust_list(source, r'const ALLOWED: &\[&str\] = &\[(.*?)\];'):
        for old, new in MAIN_RS_PATCH:  # the parent's patch, on a copy: its anchors hold and its result is gated
            assert source.count(old) == 1, 'the main.rs patch anchor moved: ' + old
            source = source.replace(old, new)
    shell_gates(source, native_only)
    child = Actor.child('a' * 32, 'b' * 32, 1)
    for name in ci.ROUTES:
        for spelled in (name, name.upper()):
            for actor in (Actor.model(), Actor.root('a' * 32), child):
                decision = tool_authz.check(tool=spelled, args={}, actor=actor, run_config={}, catalogue_digest='0' * 64,
                                            policy_version=0, admitted=lambda _, n=spelled: {n}, layers=('actor',),
                                            native_only=native_only)
                assert not decision.allowed and decision.refusal_layer == 'actor', (spelled, actor)


def shell_gates(source, native_only):
    rpc = source[source.index('async fn rpc('):source.index('fn compact(')]
    pet = rust_list(source, r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains')
    assert not pet & set(ci.ROUTES), 'the pet allowlist never names a context route'
    assert rpc.index('if !["main","pet"].contains(&window.label())') < rpc.index('if !ALLOWED.contains(&method.as_str())')
    assert set(ci.ROUTES) <= rust_list(source, r'const ALLOWED: &\[&str\] = &\[(.*?)\];')
    assert not set(ci.ROUTES) & rust_list(source, r'const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];')
    inject = next(line for line in rpc.splitlines() if 'map.insert("surface"' in line)
    assert all(f'method=="{name}"' in inject for name in ci.ROUTES), 'the shell writes surface for both routes'
    assert rpc.index('if !ALLOWED.contains(&method.as_str())') < rpc.index(inject) < rpc.index('backend.call(&method')
    assert set(ci.ROUTES) <= native_only


# 5. The pure parts ----------------------------------------------------------------------------------------------------

def test_compose_classifies_by_marker_role_and_position_and_sums_exactly():
    image = 'data:image/png;base64,' + 'A' * 4000
    messages = [
        {'role': 'system', 'content': 'policy'},
        {'role': 'user', 'content': 'checkpoint', '_pebble_checkpoint': True},
        {'role': 'user', 'content': 'recalled', '_pebble_memory': True},
        {'role': 'user', 'content': 'earlier turn'},
        {'role': 'assistant', 'content': 'earlier reply'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'look'}, {'type': 'image_url', 'image_url': {'url': image}}],
         '_pebble_current': True},
        {'role': 'user', 'content': 'files', '_pebble_source': True},
        {'role': 'assistant', 'tool_calls': [call('c1', 'Read', file_path='a')]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'result'},
        {'role': 'user', 'content': 'HOST CHECK: continue'},
        {'role': 'user', 'content': 'plan', '_pebble_plan': True, '_pebble_checkpoint': True},
    ]
    tools = [t for t in TOOLS if t['function']['name'] in ('Read', 'Glob')]
    view = ci.compose(messages, tools, 30000, ESTIMATOR)
    rows = by_id(view['categories'])
    assert view['total'] == ESTIMATOR.count([ci.wire(m) for m in messages], tools)
    assert sum(r['tokens'] for r in rows.values()) == view['total'] and rows['framing']['tokens'] >= 64
    parts = {p['id']: p['messages'] for r in rows.values() for p in r['parts'] if p['messages']}
    assert parts == {'evidence': 1, 'history': 2, 'request': 1, 'sources': 1, 'model_turns': 1, 'host_notes': 1,
                     'plan_goal': 1}, 'a message with several markers takes the projection precedence (plan first)'
    assert rows['memory']['messages'] == rows['system']['messages'] == rows['tool_results']['messages'] == 1
    assert by_id(rows['conversation']['parts'])['request']['tokens'] > 16000 > rows['memory']['tokens'], 'image cost'
    assert [t['name'] for t in view['tools']] == sorted(['Read', 'Glob'], key=lambda n: -tokens(
        next(t for t in tools if t['function']['name'] == n)))
    calibrated_view = ci.compose(messages, tools, 30000, ESTIMATOR, multiplier=1.25)
    assert calibrated_view['used'] == math.ceil(view['total'] * 1.25) == ESTIMATOR.count(
        [ci.wire(m) for m in messages], tools, multiplier=1.25)
    assert calibrated_view['calibration']['tokens'] == calibrated_view['used'] - view['total']
    assert calibrated_view['free'] == 30000 - calibrated_view['used']
    shaped = ci.compose(messages, tools, 30000, ESTIMATOR, protocol='responses')
    assert shaped['total'] == ESTIMATOR.count(input_items(messages), [ci.tool_form(t, 'responses') for t in tools])
    assert sum(r['tokens'] for r in shaped['categories']) == shaped['total'] != view['total']
    assert [r['messages'] for r in shaped['categories']] == [r['messages'] for r in view['categories']]
    tight = ci.compose(messages, tools, 1000, ESTIMATOR)
    assert tight['free'] == 0 and tight['over'] == view['total'] - 1000
    assert ci.compose(messages, tools, None, ESTIMATOR)['free'] is None
    with pytest.raises(ValueError):
        ci.compose(messages, tools, 1000, ESTIMATOR, multiplier=2.5)
    odd = ci.compose([], [{'function': {'name': '<b>x</b>'}}, {'function': {}}], 100, ESTIMATOR)
    assert [t['name'] for t in odd['tools']] == ['unnamed', 'unnamed']


def test_compose_sums_exactly_with_the_byte_estimate_too(monkeypatch):
    monkeypatch.setattr(TokenBudget, 'encoding', staticmethod(lambda: ByteEstimate()))
    messages = [{'role': 'system', 'content': 'policy é'}, {'role': 'user', 'content': 'ask ✓', '_pebble_current': True}]
    view = ci.compose(messages, TOOLS[:3], 9000, ESTIMATOR)
    assert view['counter'] == 'byte_estimate'
    assert sum(r['tokens'] for r in view['categories']) == view['total'] == ESTIMATOR.count(
        [ci.wire(m) for m in messages], TOOLS[:3])


def test_match_request_finds_exactly_the_messages_a_projection_sent():
    messages = [{'role': 'system', 'content': 'policy'}, {'role': 'user', 'content': 'c', '_pebble_checkpoint': True}]
    for i in range(6):
        messages += [{'role': 'assistant', 'tool_calls': [call(f'c{i}', 'Read', file_path=f'{i}')]},
                     {'role': 'tool', 'tool_call_id': f'c{i}', 'content': 'r' * 300}]
    messages.insert(2, {'role': 'user', 'content': 'request', '_pebble_current': True})
    whole = assemble_context(messages, [], 100000)
    assert ci.match_request(messages, whole.manifest) == (messages, len(messages))
    retired = assemble_context(messages, [], 400)
    sent, covered = ci.match_request(messages, retired.manifest)
    assert retired.manifest['status'] == 'retired' and sent == retired.messages and covered == len(messages)
    later = messages + [{'role': 'assistant', 'content': 'final'}]
    assert ci.match_request(later, retired.manifest) == (retired.messages, len(messages))
    # A compaction stores its selection as the checkpoint: the selected units are then the prefix.
    assert ci.match_request(retired.messages, retired.manifest) == (retired.messages, len(retired.messages))
    changed = copy.deepcopy(messages)
    changed[-1]['content'] = 'edited'
    assert ci.match_request(changed, whole.manifest) is None
    assert ci.match_request(messages[:-1], whole.manifest) is None
    for broken in ({**whole.manifest, 'status': 'exceeded'}, {**whole.manifest, 'units_listed': 1},
                   {**whole.manifest, 'units': 'x'}, {**whole.manifest, 'messages': None}, None):
        assert ci.match_request(messages, broken) is None


def test_inspecting_a_run_without_a_recorded_projection_counts_its_checkpoint_offer(hosts):
    host = hosts(8192)
    state = {'n': 0}

    def plain(messages, tools, cancel, token, effort=False):  # a provider that records no projection
        state['n'] += 1
        if state['n'] == 1:
            return {'role': 'assistant', 'tool_calls': [call('r1', 'Read', file_path='notes.md')]}
        token('Done.')
        return {'role': 'assistant', 'content': 'Done.'}
    host.app.model.complete = plain
    rid = work(host)
    view = inspect(host, run_id=rid)
    stored = host.app.runs.get(rid)['checkpoint']
    assert view['basis'] == 'checkpoint' and view['recorded'] is None and view['since_last_request'] is None
    assert view['tools_basis'] == 'eligible_offer' and len(view['tools']) == len(stored['available'])
    assert view['window'] == {'tokens': 8192, 'context': 8192, 'output_reserve': None, 'source': 'route_budget',
                              'route_budget': 8192}
    assert view['total'] == ESTIMATOR.count([ci.wire(m) for m in stored['messages']], stored['available'])
    assert sum(c['tokens'] for c in view['categories']) == view['total']
