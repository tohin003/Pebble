"""Phase 22 (t22-stream): provider-reported usage on the run path.

Live, "do you know my name" showed "— tok/s · In — · Cached — · Out —": its first attempt was abandoned (usage unknown,
so the message totals are unknown, as they must stay: an abandoned attempt may still be billed), and nothing said
what the provider did report for the attempt that answered. Now:
- usage_metrics also reads reasoning tokens; complete_responses takes usage from the terminal event, else from the
  stream's last event that carried it;
- the message keeps its totals' rules and adds `reported`, the sum of what the provider reported;
- the `usage` event is sent only when the provider reported counts, so a route that reports none sends nothing;
- run_activity's `usage` is the reported sum; agent_usage records a tracked root's attempts as before.
Only Providers.http is scripted: no network, model or credential.
"""

import copy
import io
import json
import socket
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

from pebble.app import App
from pebble.responses import complete_responses
from pebble.stats import add_report, attempt_report, usage_metrics

LUNA = {'id': 'luna', 'name': 'Luna', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
        'model': 'gpt-6-luna', 'context': 32768, 'budget': 32768, 'tools': True, 'context_mode': 'custom',
        'budget_version': 2, 'context_version': 1, 'connection_id': 'luna'}
REPORTED = {'input_tokens': 1300, 'output_tokens': 40, 'input_tokens_details': {'cached_tokens': 1024},
            'output_tokens_details': {'reasoning_tokens': 24}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('These tests must not open network connections.')
        return connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))


# -- normalisation ----------------------------------------------------------------------------------------------------

def test_reasoning_tokens_are_read_and_malformed_ones_stay_unknown():
    assert usage_metrics(REPORTED, {}, 0, 2)['reasoning_tokens'] == 24
    chat = {'prompt_tokens': 50, 'completion_tokens': 9, 'completion_tokens_details': {'reasoning_tokens': 5}}
    assert usage_metrics(chat, {}, 0, 1)['reasoning_tokens'] == 5
    for details in ({'reasoning_tokens': 41}, {'reasoning_tokens': -1}, {'reasoning_tokens': True},
                    {'reasoning_tokens': '24'}, 'reasoning', None, {}):
        assert usage_metrics({**REPORTED, 'output_tokens_details': details}, {}, 0, 2)['reasoning_tokens'] is None
    assert usage_metrics({'output_tokens_details': {'reasoning_tokens': 3}}, {}, 0, 2)['reasoning_tokens'] is None
    assert usage_metrics({}, None, None, 1)['reasoning_tokens'] is None


def test_an_attempt_report_holds_only_what_was_reported():
    assert attempt_report({}) is None and attempt_report(None) is None
    assert attempt_report(usage_metrics({}, {}, None, 1)) is None  # a provider that reported nothing
    report = attempt_report(usage_metrics(REPORTED, {}, 0.0, 2.0))
    assert report == {'input': 1300, 'cached': 1024, 'output': 40, 'reasoning': 24, 'tokens_per_second': 20.0}
    assert attempt_report({'input_tokens': 7, 'output_tokens': None, 'tokens_per_second': float('inf')}) == {
        'input': 7, 'cached': None, 'output': None, 'reasoning': None, 'tokens_per_second': None}
    total = add_report(None, report)
    total = add_report(total, {'input': 200, 'cached': None, 'output': 10, 'reasoning': None, 'tokens_per_second': 10.0})
    assert {k: total[k] for k in ('input', 'cached', 'output', 'reasoning', 'attempts')} == {
        'input': 1500, 'cached': 1024, 'output': 50, 'reasoning': 24, 'attempts': 2}
    assert total['tokens_per_second'] == round(50 / (40 / 20.0 + 10 / 10.0), 1)


class Lines:
    def __init__(self, events):
        self.lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events]
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def close(self):
        self.closed = True

    def readline(self, limit=-1):
        return self.lines.pop(0) if self.lines else b''


def text_events(text, usage=REPORTED, early_usage=None, calls=False):
    item = ({'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': 'UpdatePlan',
             'arguments': json.dumps({'steps': '[]'}), 'status': 'completed'} if calls else
            {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
             'content': [{'type': 'output_text', 'text': text}]})
    shell = {'status': 'in_progress', 'output': [], **({'usage': early_usage} if early_usage else {})}
    events = [{'type': 'response.created', 'response': shell}, {'type': 'response.in_progress', 'response': shell}]
    if not calls:
        events.append({'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'msg_1', 'delta': text})
    terminal = {'status': 'completed', 'output': [], **({'usage': usage} if usage else {})}
    return events + [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                     {'type': 'response.completed', 'response': terminal}]


class Provider:
    def __init__(self, events):
        self.events, self.response, self.last_usage = events, None, {}

    def key(self, profile):
        return 'scripted-token'

    def headers(self, kind, key):
        return {}

    def request(self, url, headers, body, timeout=None):
        return Lines(self.events)

    def project_context(self, messages, tools, budget, counter=None, method=None):
        return messages


def test_usage_comes_from_the_terminal_event_else_from_the_streams_last_report():
    messages = [{'role': 'user', 'content': 'hi', '_pebble_current': True}]
    provider = Provider(text_events('Hello.', usage=None, early_usage={'input_tokens': 77, 'output_tokens': 0}))
    complete_responses(provider, LUNA, messages, [], threading.Event(), [].append, 'medium')
    assert provider.last_usage['input_tokens'] == 77
    provider = Provider(text_events('Hello.', early_usage={'input_tokens': 77, 'output_tokens': 0}))
    complete_responses(provider, LUNA, messages, [], threading.Event(), [].append, 'medium')
    assert provider.last_usage['input_tokens'] == 1300 and provider.last_usage['reasoning_tokens'] == 24
    provider = Provider(text_events('Hello.', usage=None))
    complete_responses(provider, LUNA, messages, [], threading.Event(), [].append, 'medium')
    assert provider.last_usage['input_tokens'] is None and attempt_report(provider.last_usage) is None


# -- the run path -----------------------------------------------------------------------------------------------------

class Http:
    def __init__(self, respond):
        self.respond, self.bodies = respond, []

    def open(self, req, timeout=None):
        self.bodies.append(json.loads(req.data))
        return self.respond(req, len(self.bodies))


def overloaded(req):
    raise urllib.error.HTTPError(req.full_url, 503, 'error', {}, io.BytesIO(json.dumps(
        {'error': {'message': 'The server is overloaded.'}}).encode()))


@pytest.fixture
def host(tmp_path):
    made = []

    def start(respond):
        events = []
        app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(copy.deepcopy(args)))
        app.store.set_setting('model_path', '')
        app.store.set_setting('model_profiles', [dict(LUNA)])
        app.model.select('luna')
        app.model.auth = SimpleNamespace(access=lambda pid: 'scripted-token')
        app.model.http = Http(lambda req, number: respond(app, req, number))
        made.append(app)
        return app, events

    yield start
    for app in made:
        app.active = app.active_run = None
        app.close()


def run(app, text):
    cid = app.new_chat(None)['id']
    rid = app.send(cid, text)['run_id']
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (app.active is None and app.runs.get(rid)['state'] not in
                                               ('queued', 'running', 'retry_wait')):
        time.sleep(0.02)
    message = [m for m in app.chat(cid)['messages'] if m['role'] == 'assistant'][-1]
    return cid, rid, message


def usage_events(events, cid):
    return [d['usage'] for name, d in events if name == 'usage' and d['chat_id'] == cid]


def test_after_an_abandoned_attempt_the_totals_stay_unknown_and_the_reported_counts_are_shown(host):
    app, events = host(lambda app, req, number: overloaded(req) if number == 1 else Lines(text_events('Asha.')))
    cid, rid, message = run(app, 'do you know my name')
    usage = message['usage']
    # The totals keep their rule (an unreported attempt makes them unknown, never zero) ...
    assert usage['usage_incomplete'] is True and usage['failed_attempts'] == 1
    assert usage['input_tokens'] is None and usage['output_tokens'] is None and usage['cached_tokens'] is None
    # ... and `reported` says what the provider did report, for the meter.
    reported = usage['reported']
    assert {k: reported[k] for k in ('input', 'cached', 'output', 'reasoning', 'attempts')} == {
        'input': 1300, 'cached': 1024, 'output': 40, 'reasoning': 24, 'attempts': 1}
    assert reported['tokens_per_second'] is not None and reported['tokens_per_second'] > 0
    sent = usage_events(events, cid)
    assert sent and all(u['reported']['input'] == 1300 for u in sent)  # every event carries real numbers
    assert sent[-1] == usage
    activity = app.dispatch('run_activity', {'run_id': rid})['usage']
    assert activity == {k: reported[k] for k in ('input', 'cached', 'output', 'reasoning', 'tokens_per_second')}
    [first, second] = [json.loads(r['payload']) for r in app.store.query(
        "SELECT payload FROM run_events WHERE run_id=? AND kind='model_attempt' ORDER BY sequence", (rid,))]
    assert 'usage' not in first and second['usage']['input'] == 1300


def test_a_provider_that_reports_no_usage_sends_no_usage_event(host):
    app, events = host(lambda app, req, number: Lines(text_events('Hello.', usage=None)))
    cid, rid, message = run(app, 'hi')
    assert message['content'] == 'Hello.' and usage_events(events, cid) == []
    stored = message['usage']  # the stored record keeps its unknown fields as before
    assert stored['input_tokens'] is None and 'reported' not in stored and stored['route'] == 'model'
    assert app.dispatch('run_activity', {'run_id': rid})['usage'] is None


def test_a_host_route_still_says_it_used_no_model_tokens(host):
    app, events = host(lambda app, req, number: pytest.fail('The memory route makes no model request.'))
    cid, rid, message = run(app, 'remember that my name is Asha')
    assert [u['route'] for u in usage_events(events, cid)] == ['memory']


def test_a_tracked_roots_attempt_is_recorded_in_agent_usage(host):
    def respond(app, req, number):
        if number == 1:
            app.agents.tree.budget(app.active, app.active_run)  # the root now has a tree (as after a ResearchPlan preview)
            return Lines(text_events(None, calls=True))
        return Lines(text_events('Planned.'))

    app, events = host(respond)
    cid, rid, message = run(app, 'Plan my week')
    assert app.runs.get(rid)['state'] == 'completed'
    rows = app.store.query('SELECT input_tokens,output_tokens,cached_tokens,outcome FROM agent_usage WHERE run_id=? '
                           'ORDER BY recorded', (rid,))
    assert rows == [{'input_tokens': 1300, 'output_tokens': 40, 'cached_tokens': 1024, 'outcome': 'completed'}]
    assert message['usage']['reported']['attempts'] == 2 and message['usage']['input_tokens'] == 2600
