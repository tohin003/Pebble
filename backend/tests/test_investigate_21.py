"""Phase 21 track F21-investigate: two Pebble failures in claim set C1 that the parent could not explain.

Scripted reproduction only: no model call and no network. Every model request below goes through a real App and
the real provider client (Providers.request, complete_responses or _complete) with only the HTTP opener scripted, or
through the real paired driver with the live-shaped script of test_eval_live_shapes_18.

1. research-draught ended `budget_exhausted` after one step with an empty answer (1 of 3 runs). Of the step-1 paths,
   only one gives exactly that record: the run's time budget ends while its first model request is still in flight
   (the deadline timer cancels the request, no retry). Context refusals, provider context-length errors and transient
   failures settle `failed` with their reason. E1's fitted reply does not apply on the ChatGPT route. A model refusal
   is shown as text. Product defect fixed (runner.py): that ending recorded no reason and left the person with no
   reply at all. It now records TIME_LIMIT (naming the model request in flight) and shows TIME_LIMIT_REPLY when
   nothing was shown. Same family: an agent-tree budget denial now keeps the text the person already saw.
2. code-payroll failed static_gate, hidden_behaviour and tracks_dependency_changes in all 3 Pebble runs. The grader
   evaluates both behaviour checks only after the static gate passes, so the three are one failure: the final
   payroll.py held a construct the gate refuses. policy.py is listed, offered and reachable by every discovery tool at
   the 32K offer, and both arms see the same request and the same dependency text before writing, so this is not a
   Pebble-side visibility defect. Each gate refusal of an otherwise correct module gives exactly this triple while
   the visible test passes ("1 test passed").
"""
import copy
import importlib.util
import io
import json
import sys
import threading
import time
import types
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import eval_corpus_18 as c18
from pebble import runner
from pebble.app import App
from pebble.capabilities import capabilities
from pebble.eval_baseline import text_failure_kind
from pebble.providers import FITTED_REPLY, Providers, fitted_reply

import test_eval_corpus_18 as corpus_tests
import test_eval_live_shapes_18 as shapes
import test_process_recovery as prc

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('measure_context_cost', ROOT / 'scripts' / 'measure_context_cost.py')
mcc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcc)

# The C1 route's shape (ChatGPT, gpt-6-luna): a 1,050,000-token model used with a 32,768-token working budget. A
# saved custom budget carries budget_version 2; without it Providers.profiles() migrates the profile to its full context.
LUNA = {'id': 'luna', 'name': 'Luna', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
        'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'context_mode': 'custom', 'budget_version': 2,
        'context_version': 1, 'tools': True, 'connection_id': 'luna'}
# A 32K chat-completions model without an effort parameter: the only route shape Phase 20's E1 fitted reply serves.
PLAIN32 = {'id': 'plain32', 'name': 'Plain 32K', 'kind': 'openai', 'url': 'http://127.0.0.1:9/v1',
           'model': 'fixture-32k', 'context': 32768, 'budget': 32768, 'tools': True}
DRAUGHT = c18.BY_ID['research-draught']
PAYROLL = c18.BY_ID['code-payroll']
# Well above 32,768 tokens within the 100 KB message limit (rare 4-byte characters take several tokens each), so the
# protected request alone exceeds the 32K input budget on both routes.
OVERSIZED = 'Use web search for this note: ' + ''.join(chr(0x20000 + (i * 7919) % 40000) for i in range(24000))


# -- scripted HTTP ----------------------------------------------------------------------------------------------

class Lines:
    """An HTTP response as the provider client reads it: readline for Responses, iteration for chat completions."""

    def __init__(self, lines, pause=None, cap=20.0):
        self.lines, self.pause, self.cap, self.started = list(lines), pause, cap, time.monotonic()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def readline(self, limit=-1):
        if self.lines:
            return self.lines.pop(0)
        if self.pause is not None and time.monotonic() - self.started < self.cap:
            time.sleep(self.pause)  # a model still reasoning: the stream stays open and only keep-alives arrive
            return b': keep-alive\n'
        return b''

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line


def responses_message(text, kind='output_text'):
    """A Responses stream holding one finished message: output text, or a refusal part (no text deltas)."""
    part = {'type': 'refusal', 'refusal': text} if kind == 'refusal' else {'type': 'output_text', 'text': text}
    item = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed', 'content': [part]}
    events = [{'type': 'response.created', 'response': {'status': 'in_progress', 'output': []}},
              {'type': 'response.output_item.added', 'output_index': 0,
               'item': {**item, 'status': 'in_progress', 'content': []}}]
    if kind == 'output_text':
        events.append({'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'msg_1', 'delta': text})
    events += [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
               {'type': 'response.completed', 'response': {'status': 'completed', 'output': [],
                                                           'usage': {'input_tokens': 10, 'output_tokens': 5}}}]
    return Lines([b'data: ' + json.dumps(e).encode() + b'\n' for e in events])


class Http:
    """Providers.http: each model request goes through the real Providers.request with this opener."""

    def __init__(self, respond):
        self.respond, self.bodies = respond, []

    def open(self, req, timeout=None):
        self.bodies.append(json.loads(req.data))
        return self.respond(req, len(self.bodies))


def http_error(status, message, code=None):
    def respond(req, number):
        body = io.BytesIO(json.dumps({'error': {'message': message, 'code': code, 'param': 'input'}}).encode())
        raise urllib.error.HTTPError(req.full_url, status, 'error', {}, body)
    return respond


def drive(tmp_path, profile, respond, text=DRAUGHT.prompt, limits=None):
    """One research-shaped root run on a real App (the task's grants, no project); only HTTP is scripted."""
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    http = Http(respond)
    try:
        app.store.set_setting('model_path', '')
        app.store.set_setting('model_profiles', [dict(profile)])
        app.model.select(profile['id'])
        app.model.auth = SimpleNamespace(access=lambda pid: 'scripted-token')
        app.model.http = http
        app.setting('permissions', DRAUGHT.permissions())
        cid = app.new_chat(None)['id']
        rid = app.send(cid, text, limits=limits or {'max_steps': 24, 'max_seconds': 240})['run_id']
        assert prc.wait_for(lambda: app.active is None and app.runs.get(rid)['state'] not in
                            ('queued', 'running', 'retry_wait'), 60), app.runs.get(rid)['state']
        run = app.runs.get(rid)
        notes = [(r['kind'], json.loads(r['payload'])) for r in app.store.query(
            'SELECT kind,payload FROM run_events WHERE run_id=? ORDER BY sequence', (rid,))]
        answers = [m['content'] for m in app.chat(cid)['messages'] if m['role'] == 'assistant']
    finally:
        app.active = app.active_run = None
        app.close()
    return SimpleNamespace(state=run['state'], steps=run['steps'], error=run['error'], answers=answers,
                           notes=notes, bodies=http.bodies)


def kinds(result):
    return [kind for kind, _ in result.notes]


# -- 1. research-draught: budget_exhausted after one step, empty answer -------------------------------------------

def test_the_time_budget_ending_inside_the_first_request_is_the_c1_record_and_now_says_why(tmp_path):
    """The only step-1 path to this record: the deadline timer cancels the first model request while its stream is
    still open (a slow or stalled reasoning model). One request, no retry, nothing committed. Before F21 the run
    also recorded no error and stored no message; now both say what happened."""
    result = drive(tmp_path, LUNA, lambda req, number: Lines([], pause=0.02), limits={'max_steps': 24, 'max_seconds': 2})
    assert (result.state, result.steps, len(result.bodies)) == ('budget_exhausted', 1, 1)
    failed = [payload for kind, payload in result.notes if kind == 'model_attempt_failed']
    assert failed == [{'attempt': 1, 'status': None, 'type': 'InterruptedError'}] and 'model_retry' not in kinds(result)
    assert result.error == runner.TIME_LIMIT + ' while waiting for the model.'
    assert result.answers == [runner.TIME_LIMIT_REPLY + '\n\n*Run budget_exhausted.*']
    assert 'FINAL' not in result.answers[0]  # a notice, never an answer line


def test_a_budget_reached_between_model_requests_records_the_plain_reason(tmp_path):
    """The same ending outside a model request (here a tool step that outlasts the budget) names no model request."""
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    requests = []
    try:
        app.store.set_setting('model_path', '')
        app.store.set_setting('model_profiles', [dict(PLAIN32)])
        app.model.select(PLAIN32['id'])
        app.setting('permissions', DRAUGHT.permissions())
        execute = app.execute

        def slow_tool(name, *args, **kwargs):
            if name == 'UpdatePlan':
                time.sleep(1.5)  # the tool outlasts the run's one-second budget
            return execute(name, *args, **kwargs)

        def complete(messages, tools, cancel, token, deliberate=False):
            requests.append(True)
            return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'p1', 'type': 'function', 'function': {
                'name': 'UpdatePlan', 'arguments': json.dumps({'steps': '[]'})}}]}

        app.execute = slow_tool
        app.model.complete = complete
        chat = app.new_chat(None)
        config = app.runtime_config(chat, False, {'max_steps': 5, 'max_seconds': 1})
        rid = app.runs.create(chat['id'], 'Plan the trip', config)
        app.work(chat, 'Plan the trip', False, [], run_id=rid)
        run = app.runs.get(rid)
        answers = [m['content'] for m in app.chat(chat['id'])['messages'] if m['role'] == 'assistant']
    finally:
        app.active = app.active_run = None
        app.close()
    assert len(requests) == 1 and run['state'] == 'budget_exhausted' and run['steps'] == 1
    assert run['error'] == runner.TIME_LIMIT + '.'
    assert answers == [runner.TIME_LIMIT_REPLY + '\n\n*Run budget_exhausted.*']


@pytest.mark.parametrize('case', ['transient', 'provider_context_length', 'host_refusal', 'host_refusal_e1'])
def test_other_first_request_failures_settle_failed_with_their_reason(tmp_path, case):
    """None of these is the C1 record: each settles `failed` at step 1 with a recorded reason."""
    if case == 'transient':  # three overloaded replies inside the budget: the shared retry policy, then failed
        result = drive(tmp_path, LUNA, http_error(503, 'The server is overloaded.'))
        assert len(result.bodies) == 3 and kinds(result).count('model_retry') == 2
        assert result.error.startswith('Provider HTTP 503') and text_failure_kind(result.error) == 'provider'
    elif case == 'provider_context_length':  # the provider's own window refuses the first request
        result = drive(tmp_path, LUNA, http_error(400, 'Your input exceeds the context window of this model.',
                                                  'context_length_exceeded'))
        assert len(result.bodies) == 1 and result.error.startswith('Provider HTTP 400: Your input exceeds')
        assert text_failure_kind(result.error) is None  # attributable to the arm's request, as in the baseline
    else:  # the host's projection refuses: the protected request alone exceeds the 32K input budget
        profile = LUNA if case == 'host_refusal' else PLAIN32
        result = drive(tmp_path, profile, lambda req, number: pytest.fail('A refused request reaches no provider.'),
                       text=OVERSIZED)
        assert result.bodies == [] and result.error.startswith('The current request and required context exceed')
    assert (result.state, result.steps, result.answers) == ('failed', 1, [])
    assert runner.TIME_LIMIT not in result.error


def test_e1_never_serves_the_chatgpt_route_and_fits_only_a_narrow_band_at_32k():
    """Phase 20 E1 lives in Providers._complete (chat completions, models without an effort parameter). The ChatGPT
    route returns into complete_responses first, and gpt-6-luna has an effort parameter, so E1 cannot act there."""
    assert Providers.protocol(LUNA) == 'responses' and capabilities(LUNA)['effort_parameter'] == 'reasoning'
    assert Providers.protocol(PLAIN32) == 'openai' and not capabilities(PLAIN32)['effort_parameter']
    refused = lambda floor: {'input_tokens': {'protected_floor': floor}}  # noqa: E731
    # At 32K a 1,024-token reply leaves 31,744 input tokens; E1 rescues only floors up to 32,768 - 64 - 512.
    assert fitted_reply(refused(31744 - 64 + 1), 32768, 32768, 1024) == 1024 - 1
    assert fitted_reply(refused(32768 - 64 - FITTED_REPLY), 32768, 32768, 1024) == FITTED_REPLY
    assert fitted_reply(refused(32768 - 64 - FITTED_REPLY + 1), 32768, 32768, 1024) is None
    assert fitted_reply(refused(40000), 32768, 32768, 1024) is None


def test_a_model_refusal_on_the_first_request_is_shown_not_lost(tmp_path):
    result = drive(tmp_path, LUNA, lambda req, number: responses_message('I can’t help with that.', 'refusal'))
    assert result.state == 'completed' and result.steps == 1 and result.error is None
    assert result.answers and 'I can’t help with that.' in result.answers[-1]


def test_an_agent_budget_denial_keeps_the_text_the_person_already_saw(tmp_path):
    """Same symptom family (budget_exhausted, empty stored answer): the AgentBudgetExhausted ending stored no message,
    unlike every other ending, so text already streamed to the person vanished from the conversation."""
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    calls = []
    try:
        app.store.set_setting('model_path', '')
        app.store.set_setting('model_profiles', [dict(PLAIN32)])
        app.model.select(PLAIN32['id'])
        app.setting('permissions', DRAUGHT.permissions())

        def complete(messages, tools, cancel, token, deliberate=False):
            calls.append(True)
            cid, rid = app.active, app.active_run
            app.agents.tree.budget(cid, rid)  # the tree now exists (as after a ResearchPlan admission preview)
            for index in range(3):  # and its shared dispatch budget is spent (max_steps 2: six dispatches)
                dispatch = 'spent-' + str(index)
                assert app.agents.tree.reserve_model(cid, rid, rid, 0, 1024, 0, dispatch)['admitted']
                app.agents.tree.finish_model(cid, rid, rid, 0, dispatch, failed=True)
            token('Checking the plan first.')
            return {'role': 'assistant', 'content': 'Checking the plan first.', 'tool_calls': [{
                'id': 'p1', 'type': 'function', 'function': {'name': 'UpdatePlan', 'arguments': json.dumps({'steps': '[]'})}}]}

        app.model.complete = complete
        chat = app.new_chat(None)
        config = app.runtime_config(chat, False, {'max_steps': 2, 'max_seconds': 120})
        rid = app.runs.create(chat['id'], 'Plan the trip', config)
        app.work(chat, 'Plan the trip', False, [], run_id=rid)
        run = app.runs.get(rid)
        answers = [m['content'] for m in app.chat(chat['id'])['messages'] if m['role'] == 'assistant']
    finally:
        app.active = app.active_run = None
        app.close()
    assert len(calls) == 1 and run['state'] == 'budget_exhausted' and 'model budget' in run['error']
    assert answers == ['Checking the plan first.\n\n*Run budget_exhausted.*']


# -- 2. code-payroll: static_gate, hidden_behaviour and tracks_dependency_changes ---------------------------------

def test_the_payroll_dependency_is_offered_and_reachable_at_the_32k_offer(tmp_path):
    """policy.py through every discovery tool Pebble offers at a 32K budget (the C1 route's working budget)."""
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    seen = []
    calls = [('Glob', {'pattern': '**/*.py'}),
             ('SearchFiles', {'query': 'union_dues_cents', 'mode': 'symbols'}),
             ('SearchFiles', {'query': 'HOURLY_CENTS overtime policy'}),
             ('Grep', {'pattern': 'OVERTIME_PERCENT'}),
             ('CodeReferences', {'mode': 'imports', 'path': 'payroll.py'}),
             ('CodeReferences', {'mode': 'definition', 'path': 'payroll.py', 'name': 'union_dues_cents'}),
             ('Read', {'file_path': 'policy.py'})]
    try:
        app.store.set_setting('model_path', '')
        app.store.set_setting('model_profiles', [dict(PLAIN32)])
        app.model.select(PLAIN32['id'])
        app.setting('permissions', PAYROLL.permissions())

        def complete(messages, tools, cancel, token, deliberate=False):
            if not seen:
                seen.append(({t['function']['name'] for t in tools},
                             next(m['content'] for m in messages if m.get('_pebble_source'))))
                return {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'c%d' % i, 'type': 'function', 'function': {'name': n, 'arguments': json.dumps(a)}}
                    for i, (n, a) in enumerate(calls)]}
            seen.append([json.loads(m['content']) for m in messages if m.get('role') == 'tool'])
            token('Done.\nFINAL: done')
            return {'role': 'assistant', 'content': 'Done.\nFINAL: done'}

        app.model.complete = complete
        folder = tmp_path / 'project'
        folder.mkdir()
        for name, text in PAYROLL.files:
            (folder / name).write_text(text)
        app.work(app.new_chat(str(folder)), PAYROLL.prompt, False, [])
    finally:
        app.active = app.active_run = None
        app.close()
    (offered, listing), results = seen
    assert {'Read', 'Glob', 'Grep', 'SearchFiles', 'CodeReferences', 'Write', 'Edit', 'RunTests'} <= offered
    assert '"policy.py"' in listing  # the first request already lists it
    glob, symbols, ranked, grep, imports, definition, read = results
    assert glob['files'] == ['payroll.py', 'policy.py', 'test_payroll.py']
    assert [(s['path'], s['kind']) for s in symbols['untrusted_symbols']] == [('policy.py', 'function')]
    assert ranked['untrusted_results'][0]['path'] == 'policy.py'
    assert 'policy.py' in {m['path'] for m in grep['untrusted_matches']}
    assert {row['target'] for row in imports['untrusted_imports']} == {'policy.py'}
    assert [d['path'] for d in definition['untrusted_definitions']] == ['policy.py']
    assert read['untrusted_source'] == c18.POLICY and read['excerpt_complete'] is True


class Recorder:
    """Wraps the live-shaped script: every request is kept as the model received it, before it answers."""

    def __init__(self, script):
        self.script, self.records, self.lock = script, [], threading.Lock()
        self.unscripted = script.unscripted

    def __call__(self, req):
        arm = 'baseline' if req.actor == 'baseline' else 'pebble' if req.actor == 'root' else 'pebble_child'
        with self.lock:
            self.records.append({'arm': arm, 'task': req.case_id, 'step': req.step,
                                 'messages': copy.deepcopy(req.messages), 'tools': copy.deepcopy(req.tools)})
        return self.script(req)


@pytest.fixture(scope='module')
def paired(tmp_path_factory):
    """code-payroll and research-draught in both arms through the real paired driver (block 1 of a pilot plan)."""
    recorder = Recorder(shapes.LiveShapes())
    with pytest.MonkeyPatch.context() as patch:
        shapes._loopback_only(patch)
        shapes._pin_source(patch)
        result = shapes.run_plan(tmp_path_factory.mktemp('paired'), shapes.standin('code-payroll', 'research-draught'),
                                 recorder, patch)
    return SimpleNamespace(result=result, records=recorder.records)


def _tool_text(record, name, path):
    """The text the model was shown by its read of `path` (Pebble's Read or the baseline's read_file)."""
    calls = {c['id']: json.loads(c['function']['arguments']) for m in record['messages'] if m['role'] == 'assistant'
             for c in m.get('tool_calls') or ()}
    for message in record['messages']:
        if message['role'] == 'tool' and calls.get(message['tool_call_id'], {}).get(name) == path:
            result = json.loads(message['content'])
            return result.get('untrusted_source', result.get('content'))
    return None


def test_both_arms_read_the_same_request_and_dependency_before_writing(paired):
    assert paired.result.refused == [] and not paired.result.guards.count(False)
    assert {k: c['outcome'] for k, c in paired.result.cells.items()} == {
        (t, a): 'passed' for t in ('code-payroll', 'research-draught') for a in ('pebble', 'baseline')}
    writing = {}
    for record in paired.records:
        if record['task'] == 'code-payroll' and any(m['role'] == 'tool' for m in record['messages']):
            writing.setdefault(record['arm'], record)  # the request that answers the reads with the write
    pebble, baseline = writing['pebble'], writing['baseline']
    request = [m['content'] for m in pebble['messages'] if m.get('_pebble_current')]
    assert request == [PAYROLL.prompt] == [m['content'] for m in baseline['messages'] if m['role'] == 'user'][:1]
    for path, text in (('policy.py', c18.POLICY), ('payroll.py', c18.PAYROLL)):
        assert _tool_text(pebble, 'file_path', path) == text == _tool_text(baseline, 'path', path)


def test_first_request_sizes_per_arm_are_measured(paired):
    """Measurement only (scripted composition, o200k): the first request of each run, per arm."""
    first = {}
    for record in paired.records:
        key = (record['task'], record['arm'])
        if key not in first and record['arm'] != 'pebble_child':
            found = mcc.composition(record['messages'], record['tools'])
            first[key] = (found['total'], found['parts']['tool_schemas'], len(record['tools']))
    print('\nfirst request (total tokens, schema tokens, tools):')
    for key in sorted(first):
        print('  %-18s %-8s %s' % (key + (first[key],)))
    for task in ('code-payroll', 'research-draught'):
        assert first[(task, 'pebble')][0] > first[(task, 'baseline')][0]


def _visible_test_passes(candidate):
    """Run the candidate natively against the task's own policy.py, as its visible test does."""
    policy = types.ModuleType('policy')
    exec(c18.POLICY, policy.__dict__)
    saved = sys.modules.get('policy')
    sys.modules['policy'] = policy
    try:
        module = types.ModuleType('payroll')
        exec(candidate, module.__dict__)
        return module.net_week_cents([8, 8, 8, 8, 6]) == 80614
    finally:
        if saved is None:
            sys.modules.pop('policy', None)
        else:
            sys.modules['policy'] = saved


REF = corpus_tests.PAYROLL_REF
GATED = {  # a correct module with one construct the grader's grammar gate refuses, and the gate's reason code
    'name_not_allowed': REF.replace('    for worked in days:\n',
                                    '    for worked in days:\n        if type(worked) is not int:\n'
                                    '            raise ValueError("whole hours only")\n'),
    'import_not_allowed': REF.replace('def net_week_cents(days):',
                                      'from typing import Sequence\n\n\ndef net_week_cents(days: Sequence[int]) -> int:'),
    'construct_not_allowed': REF.replace('    if len(days) == 0 or len(days) > 7:',
                                         '    limits = {"days": 7}\n    if len(days) == 0 or len(days) > limits["days"]:'),
    'function_missing': REF.replace('def net_week_cents(days):',
                                    'def net_week_cents(days):\n    return 0\n\n\ndef net_week_cents(days):'),
    'module_import': REF.replace('from policy import', 'import policy\nfrom policy import').replace(
        'union_dues_cents(gross)', 'policy.union_dues_cents(gross)'),
}


@pytest.mark.parametrize('code', sorted(GATED))
def test_any_gate_refusal_alone_fails_exactly_the_three_payroll_checks_while_the_visible_test_passes(code, tmp_path):
    candidate = GATED[code]
    assert candidate != REF and _visible_test_passes(candidate)
    verdict = corpus_tests.grade('code-payroll', corpus_tests.run('code-payroll', {'payroll.py': candidate}), tmp_path)
    assert corpus_tests.failed(verdict) == {'static_gate', 'hidden_behaviour', 'tracks_dependency_changes'}
    assert verdict['checks']['cross_file_assembly'] is True and verdict['counts']['hidden_passed'] == 0
    expected = 'import_not_allowed' if code == 'module_import' else code
    assert verdict['reasons'][0] == expected  # private only: public_verdict drops reasons (evaluation.py)
    reference = corpus_tests.grade('code-payroll', corpus_tests.run('code-payroll', {'payroll.py': REF}),
                                   tmp_path / 'reference')
    assert reference['passed']
