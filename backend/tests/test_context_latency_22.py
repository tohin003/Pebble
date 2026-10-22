"""Phase 22 (t22-stream): the latency budget for a simple question's context, on realistic stores.

The person suspected memory retrieval when "do you know my name" took 131.7 s; the parent measured context projection,
memory included, at 0.03 s (the time went to a stalled model request; see test_stream_watchdog_22). This test seeds a
temporary database the size of a heavy user's (5,000 memories across the five long-term layers with learning
evidence, 200 chats of 10 messages, 50 settled runs with their events and tools) and measures, on a fresh App:
- recall for the question, cold (the first recall builds the connection's trigram index) and warm;
- the runner's own context time (its `context_ready` record: run start to the first model request), and the
  provider's projection of that context into the request (`model_attempt.projection`);
- the whole path from `send` to the first byte the model provider would receive.
The bounds are generous for this machine yet catch a slowdown of ten times or more. Only Providers.http is scripted.
"""

import json
import math
import random
import socket
import statistics
import time
import uuid
from types import SimpleNamespace

import pytest

from pebble.app import App

LUNA = {'id': 'luna', 'name': 'Luna', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
        'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'tools': True, 'context_mode': 'custom',
        'budget_version': 2, 'context_version': 1, 'connection_id': 'luna'}
QUESTION = 'do you know my name'
NAME = 'My name is Asha Verma and I live in Pune.'
MEMORIES, CHATS, MESSAGES_PER_CHAT, RUNS = 5000, 200, 10, 50
CONVERSATION_CAP = 1400  # below Memory.put's 1,500 bound on automatic 'conversation' records
# Bounds (seconds), set from this machine's measurements (MEASURED, in the note): each is several times the measured
# value, so ordinary load passes, and below ten times it, so a 10x regression fails. "first" is the first question
# after the App starts (the trigram index and, in a fresh process, the o200k tokenizer load once); "next" is the
# question after it on the same App.
MEASURED = {'recall_cold': 0.0666, 'recall_warm': 0.0077,  # medians of three runs, 2026-10-14, macOS arm64
            'first': {'ready': 0.0702, 'projection': 0.1319, 'request': 0.2087},
            'next': {'ready': 0.0119, 'projection': 0.0022, 'request': 0.0175}}
RECALL_COLD, RECALL_WARM = 0.6, 0.07
BOUNDS = {'first': {'ready': 0.5, 'projection': 1.0, 'request': 1.5},
          'next': {'ready': 0.1, 'projection': 0.02, 'request': 0.15}}

WORDS = ('project deadline invoice garden recipe flight hotel meeting doctor appointment birthday gift budget rent '
         'salary laptop keyboard python rust typescript release review design sketch painting guitar lesson school '
         'exam physics chemistry biology history novel chapter poem coffee tea breakfast dinner market train bus '
         'airport visa passport bank account loan insurance policy claim weather rain summer winter festival diwali '
         'holiday beach mountain trek yoga running marathon gym protein vitamin allergy medicine pharmacy clinic '
         'friend sister brother mother father cousin neighbour colleague manager client contract proposal draft '
         'spreadsheet presentation slides chart dashboard quiz notes summary research paper citation source link '
         'repository branch commit deploy server database backup restore migration schema index cache latency').split()
KINDS = (('semantic', 2000), ('episodic', 1400), ('procedural', 400), ('temporal', 700), ('profile', 500))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('These tests must not open network connections.')
        return connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))


def sentence(rng, low=12, high=40):
    words = [rng.choice(WORDS) for _ in range(rng.randint(low, high))]
    return (' '.join(words).capitalize() + '.')[:3900]


@pytest.fixture(scope='module')
def home(tmp_path_factory):
    """A heavy user's data directory, seeded once through the host's own tables."""
    return seed(tmp_path_factory.mktemp('latency'), KINDS, MEMORIES)


# The design study's bar (D22 §8): 20,000 records across the six layers, about 8 MB, a context-projection p95 of at most
# 300 ms and a maximum of at most 1 s at a 32K window. (Working rows are stored like any other; recall pins the working
# view from the run journal instead, so they only add weight.)
STUDY_KINDS = (('semantic', 7000), ('episodic', 5000), ('procedural', 1500), ('temporal', 2500), ('profile', 2000),
               ('working', 2000))
STUDY_MEMORIES, STUDY_P95, STUDY_MAX, STUDY_QUESTIONS = 20000, 0.3, 1.0, 20


@pytest.fixture(scope='module')
def study_home(tmp_path_factory):
    return seed(tmp_path_factory.mktemp('latency-study'), STUDY_KINDS, STUDY_MEMORIES, words=(55, 85))


def seed(root, kind_counts, total, words=(12, 40)):
    rng = random.Random(22)
    app = App(root / 'data', root / 'resources', lambda *args: None)
    try:
        now = time.time()
        chats = [uuid.uuid4().hex for _ in range(CHATS)]
        memories, evidence = [], []
        kinds = [kind for kind, count in kind_counts for _ in range(count)]
        for index, kind in enumerate(kinds[:total - 1]):
            ident = 'seed-%05d' % index
            scope = 'global' if index % 5 else 'chat:' + chats[index % CHATS]
            # Memory.put keeps at most 1,500 'conversation' records, so a real store never holds more: the rest are
            # learned (with evidence) or the person's own.
            origin = 'learned' if index % 2 else 'conversation' if index < 2 * CONVERSATION_CAP else 'user'
            memories.append((ident, kind, sentence(rng, *words), scope, chats[index % CHATS], origin,
                             now - index * 60, now - index * 30, None))
            if origin == 'learned':
                evidence.append((ident, 'cand-' + ident, 'run-seed', chats[index % CHATS], scope, kind,
                                 'slot-%d' % (index % 400), uuid.uuid4().hex, 'active',
                                 json.dumps({'evidence_class': 'conversation', 'receipt_ids': ['seed:%d' % index]}),
                                 None, now - index * 30))
        memories.append(('seed-name', 'profile', NAME, 'global', None, 'user', now, now, None))
        messages = [(uuid.uuid4().hex, cid, 'user' if turn % 2 == 0 else 'assistant', sentence(rng, 20, 160),
                     now - c * 3600 + turn) for c, cid in enumerate(chats) for turn in range(MESSAGES_PER_CHAT)]
        with app.store.lock, app.store.db as db:
            db.executemany('INSERT INTO chats VALUES(?,?,?,?)', [(cid, 'Seeded chat', None, now) for cid in chats])
            db.executemany('INSERT INTO messages VALUES(?,?,?,?,?)', messages)
            db.executemany('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)', memories)
            db.executemany('INSERT INTO memory_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', evidence)
        config = app.runtime_config(app.chat(chats[0]), False)
        for index in range(RUNS):
            rid = app.runs.create(chats[index], sentence(rng, 6, 12), config)
            with app.store.lock, app.store.db as db:
                for step in range(1, 9):
                    call = '%d:c%d' % (step, step)
                    db.execute('INSERT INTO run_tools VALUES(?,?,?,?,?,?)', (rid, call, 'WebSearch', json.dumps(
                        {'query': sentence(rng, 3, 6)}), 'completed', json.dumps({'status': 'ok', 'results': sentence(rng)})))
                    for kind, payload in (('context_projected', {'step': step, 'manifest': {'note': sentence(rng, 200, 400)}}),
                                          ('tool_authorized', {'call_id': call}), ('tool_started', {'call_id': call}),
                                          ('tool_finished', {'call_id': call, 'uncertain': False}),
                                          ('response_committed', {'step': step, 'response': {'content': sentence(rng)}})):
                        app.runs.event(db, rid, kind, payload)
                db.execute("UPDATE runs SET state='completed' WHERE id=?", (rid,))
        counts = {name: app.store.query('SELECT count(*) AS n FROM ' + name)[0]['n']
                  for name in ('memories', 'memory_evidence', 'chats', 'messages', 'runs', 'run_events')}
        counts['memory_bytes'] = app.store.query('SELECT sum(length(CAST(content AS BLOB))) AS n FROM memories')[0]['n']
    finally:
        app.close()
    assert counts['memories'] >= total and counts['chats'] == CHATS and counts['runs'] == RUNS
    return SimpleNamespace(root=root, chats=chats, counts=counts)


class Lines:
    def __init__(self, text):
        item = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': text}]}
        events = [{'type': 'response.created', 'response': {'status': 'in_progress', 'output': []}},
                  {'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'msg_1', 'delta': text},
                  {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                  {'type': 'response.completed', 'response': {'status': 'completed', 'output': [],
                                                              'usage': {'input_tokens': 900, 'output_tokens': 9}}}]
        self.lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def readline(self, limit=-1):
        return self.lines.pop(0) if self.lines else b''


def open_app(home):
    app = App(home.root / 'data', home.root / 'resources', lambda *args: None)
    return app


def timed(call):
    began = time.perf_counter()
    result = call()
    return time.perf_counter() - began, result


def test_recall_for_a_simple_question_stays_within_budget_cold_and_warm(home):
    app = open_app(home)
    try:
        cid = app.store.new_chat(None)
        cold, first = timed(lambda: app.memory.recall(QUESTION, cid, None, window=LUNA['budget']))
        warm = [timed(lambda: app.memory.recall(QUESTION, cid, None, window=LUNA['budget']))[0] for _ in range(7)]
    finally:
        app.close()
    print('\nrecall over %d memories: cold %.1f ms, warm median %.1f ms (max %.1f ms); trigram %s'
          % (home.counts['memories'], cold * 1000, statistics.median(warm) * 1000, max(warm) * 1000,
             first['diagnostics'].get('trigram')))
    assert any(NAME in item.get('content', '') for item in first['items'])  # the answer's memory is recalled
    assert cold < RECALL_COLD and statistics.median(warm) < RECALL_WARM
    assert RECALL_COLD < 10 * MEASURED['recall_cold'] and RECALL_WARM < 10 * MEASURED['recall_warm']


def scripted(home, opened):
    """A fresh App on the seeded home whose model requests are answered by a script (the real run path otherwise)."""
    app = open_app(home)
    app.store.set_setting('model_path', '')
    app.store.set_setting('model_profiles', [dict(LUNA)])
    app.model.select('luna')
    app.model.auth = SimpleNamespace(access=lambda pid: 'scripted-token')

    def respond(req):
        opened.append((time.perf_counter(), json.loads(req.data)))
        return Lines('Yes, you are Asha Verma.')

    app.model.http = SimpleNamespace(open=lambda req, timeout=None: respond(req))
    return app


def ask(app, cid, opened):
    """One question through send: the runner's context time, the provider's projection, send to request."""
    began = time.perf_counter()
    rid = app.send(cid, QUESTION)['run_id']
    deadline, settled = time.monotonic() + 60, None
    while time.monotonic() < deadline and not (app.active is None and app.runs.get(rid)['state'] == 'completed'):
        if settled is None and app.runs.get(rid)['state'] == 'completed':
            settled = time.perf_counter()
        time.sleep(0.002)
    assert app.runs.get(rid)['state'] == 'completed' and app.active is None
    idle = time.perf_counter()
    sent, body = opened[-1]
    context, attempt = [json.loads(r['payload']) for r in app.store.query(
        "SELECT payload FROM run_events WHERE run_id=? AND kind IN ('context_ready','model_attempt') "
        'ORDER BY sequence', (rid,))]
    assert NAME in json.dumps(body['input'])  # the request carries the recalled memory
    [item] = [i for i in app.dispatch('run_activity', {'run_id': rid})['items'] if i['kind'] == 'context']
    assert item['label'].startswith('Recalled ')
    return {'ready': context['seconds'], 'projection': attempt['projection'], 'request': sent - began,
            'label': item['label'], 'after_settle': idle - (settled or idle), 'total': idle - began}


def test_a_simple_questions_context_stays_within_budget_on_the_real_run_path(home):
    measured, opened = {}, []
    app = scripted(home, opened)
    try:
        for leg, cid in (('first', home.chats[0]), ('next', home.chats[1])):
            measured[leg] = ask(app, cid, opened)
    finally:
        app.active = app.active_run = None
        app.close()
    assert len(opened) == 2
    for leg, values in measured.items():
        print('\n%s question: context ready %.1f ms (%s), projection %.1f ms, send to request %.1f ms'
              % (leg, values['ready'] * 1000, values['label'], values['projection'] * 1000, values['request'] * 1000))
    for leg, values in measured.items():
        for key, bound in BOUNDS[leg].items():
            assert values[key] < bound, (leg, key, values[key], bound)
            assert bound < 10 * MEASURED[leg][key]  # the bound still catches a tenfold slowdown


def test_idle_memory_maintenance_on_twenty_thousand_records_is_measured(study_home):
    """App.idle runs memory maintenance under the task lock between turns; measure what one pass costs here."""
    app = open_app(study_home)
    try:
        app.closed = True  # this test runs the pass itself; the service's idle thread stays out of the way
        with app.task_lock:
            expire, _ = timed(app.memory.expire)
            maintain, _ = timed(app.learning.maintain)
            consolidate, _ = timed(app.learning.consolidate)
            again = [timed(f)[0] for f in (app.memory.expire, app.learning.maintain, app.learning.consolidate)]
        app.closed = False
        # Forgetting one memory: its FTS delete trigger filters memory_search by an UNINDEXED id (a full scan).
        forgets = [timed(lambda ident=ident: app.memory_delete(ident))[0] for ident in ('seed-%05d' % i for i in range(1, 21, 2))]
    finally:
        app.closed = False
        app.close()
    print('\nidle maintenance over %d memories: expire %.1f ms, maintain %.1f ms, consolidate %.1f ms; second pass %s ms'
          % (study_home.counts['memories'], expire * 1000, maintain * 1000, consolidate * 1000,
             [round(t * 1000, 1) for t in again]))
    print('memory_delete of one record: median %.1f ms (max %.1f ms)' % (statistics.median(forgets) * 1000,
                                                                       max(forgets) * 1000))
    assert expire + maintain + consolidate < 1.0  # a pass between turns never holds the task lock for long


def test_twenty_thousand_records_meet_the_design_studys_bar(study_home):
    """D22 §8: p95 <= 300 ms and max <= 1 s for the context projection at a 32K window, the first question after the
    App starts included. Measured per question as the host's own time before the request: the runner's context time
    plus the provider's projection."""
    assert study_home.counts['memories'] >= STUDY_MEMORIES and study_home.counts['memory_bytes'] >= 7_000_000
    opened = []
    app = scripted(study_home, opened)
    try:
        samples = [ask(app, study_home.chats[index], opened) for index in range(STUDY_QUESTIONS)]
    finally:
        app.active = app.active_run = None
        app.close()
    projected = sorted(s['ready'] + s['projection'] for s in samples)
    p95 = projected[math.ceil(0.95 * len(projected)) - 1]
    print('\n%d memories (%.1f MB): context projection p50 %.1f ms, p95 %.1f ms, max %.1f ms (first %.1f ms); '
          'send to request p95 %.1f ms'
          % (study_home.counts['memories'], study_home.counts['memory_bytes'] / 1e6, statistics.median(projected) * 1000,
             p95 * 1000, projected[-1] * 1000, (samples[0]['ready'] + samples[0]['projection']) * 1000,
             sorted(s['request'] for s in samples)[math.ceil(0.95 * len(samples)) - 1] * 1000))
    print('after settlement until the next task may start: %s ms' % [round(s['after_settle'] * 1000) for s in samples])
    print('send until the next task may start: %s ms' % [round(s['total'] * 1000) for s in samples])
    assert p95 <= STUDY_P95 and projected[-1] <= STUDY_MAX
    assert max(s['after_settle'] for s in samples) < 1.0  # the settled run's memory work never holds up the next turn
