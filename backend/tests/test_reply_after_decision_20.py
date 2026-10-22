"""Phase 20 H20: the reply is presented after the host's checks for its turn-end (live pilot 5, option (c) extended).

Above 4096, a model turn that ends (no tool calls) is presented only after the host's checks for that turn-end:
- while this run's proposals await the person's decision it is held: the run settles waiting_approval as before, and
  after the decision the resume line says that message was not shown, so the reply written then is the answer;
- when the promise check or a repairable completion-gate issue asks for more, it is withdrawn: the next turn-end,
  checked on its own, is the reply; when repairs run out the last attempt is presented with the host's notice.
A held or withdrawn turn stays in the run's private checkpoint and audit records and never reaches the chat or its
tokens. A withdrawn draft that streamed live stays a committed attempt in the stream journal and is announced as
withdrawn. The staged-draft workflow inside the turn, questions, denials, restarts and 4K windows keep their paths.

The App runner, broker, approval routes, run journal, stream journal, completion gate and research check are the
checked-in code. Only the root model (Providers.complete), the approved test action's sandboxed subprocess (run_tests)
and the web (the corpus's offline FixtureWeb pack, as the evaluation installs it) are scripted. No model HTTP,
network, shell or credential is used.
"""

import copy
import json
import socket
import time
from types import SimpleNamespace

import pytest

import pebble.app as app_module
from pebble import evaluation as ev
from pebble.app import App
from pebble.eval_research_18 import GOV, FixtureWeb
from pebble.permissions import DEFAULT
from pebble.providers import Providers
from pebble.runner import APPROVAL_NOTE, HELD_RESUME, WITHHELD_NOTE
from test_high_fd_waits_21 import close_guard_databases

SOURCE = 'def add(a, b):\n    return a + b\n'
CHANGED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
TESTS = ('import unittest\n\nfrom calc import add\n\n\n'
         'class AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n')
PASSED = {'exit_code': 0, 'output': 'test_add (test_calc.AddTest.test_add) ... ok\n\n' + '-' * 70
          + '\nRan 1 test in 0.001s\n\nOK\n'}
NOTES = '# Notes\n\n`add(a, b)` returns the sum of a and b.\n'
ITEMS = [{'id': 'style', 'title': 'Which note style?', 'options': [{'id': 'short', 'label': 'One line'}]}]
H1C = ' The person already sees your reply so far; continue it without repeating it.'
FALLBACK = 'I prepared the actions shown below. Review them before they run.'
NOTICE = '> Verification remains incomplete: '
SCHEDULE, DUES_NOTICE = GOV + '/docs/schedule-of-dues-2026', GOV + '/notices/2026-14-dues'
DUES_GOAL = 'Search the web for the current Port Ellery harbour dues for cargo vessels. End with `FINAL: <answer>`.'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('These tests must not open network connections.')
        return connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))


def call(name, args, ident, text=None):
    return {'role': 'assistant', 'content': text, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    (project / 'test_calc.py').write_text(TESTS)
    value = SimpleNamespace(project=project, home=tmp_path / 'data', requests=[], script=[], results=[],
                            events=[], apps=[])

    # Class-level patches also cover an App constructed after a simulated restart.
    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        assert len(value.requests) <= len(value.script), 'Unscripted root model request.'
        step = value.script[len(value.requests) - 1]
        response = step(messages) if callable(step) else copy.deepcopy(step)  # a callable reads its own results
        if response.get('content'):
            token(response['content'])  # streams live through the runner's presentation path when it is on
        return response

    def run_tests(payload, cancel):
        assert payload['recipe'] == 'python-unittest'
        return copy.deepcopy(value.results.pop(0))

    monkeypatch.setattr(Providers, 'complete', complete)
    monkeypatch.setattr(app_module, 'run_tests', run_tests)

    def start(context=32768, web=False):
        app = App(value.home, tmp_path / 'resources', lambda *args: value.events.append(copy.deepcopy(args)))
        value.apps.append(app)
        if app.store.setting('permissions')['mode'] != 'ask':
            app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'ask'})
        if web:
            # The research tasks' offline pack through the evaluation's own per-App seam; the web group is granted.
            value.web = FixtureWeb('ellery-2026')
            ev._install_web_18(app, value.web)
            groups = app.store.setting('permissions', DEFAULT)['tools']
            app.store.set_setting('permissions', {'mode': 'ask', 'tools': {**groups, 'web': True}})
        if not app.model.current() or app.model.current().get('budget') != context:
            app.model_save(name='H20 fixture', url='http://127.0.0.1:9', model='h20-fixture-%d' % context,
                           context=context)
        assert app.model.current()['budget'] == context
        return app

    value.start = start
    yield value
    for app in value.apps:
        if not app.closed:
            app.active = app.active_run = None
            app.close()
    close_guard_databases(tmp_path)  # resource cleanup only (F21-fd)


def settled(env, app, rid, state, requests, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = app.runs.get(rid)
        if run['state'] == state and app.active is None and len(env.requests) == requests:
            return run
        time.sleep(.02)
    last = [m.get('content') for m in env.requests[-1] if m.get('role') == 'user'][-1:] if env.requests else []
    pytest.fail(f'Run did not settle as {state} after {requests} requests: '
                f'{app.runs.get(rid)["state"]} after {len(env.requests)} ({app.runs.get(rid)["error"]}); last host '
                f'message: {str(last)[:600]}')


def pending(app, cid, kind):
    return next(a for a in app.store.actions(cid) if a['kind'] == kind and a['status'] == 'pending')


def reply(app, rid, cid):
    """The run's own reply message (the one the evaluation grades): None while nothing is stored."""
    mid = app.runs.get(rid)['checkpoint']['message_id']
    return next((m['content'] for m in app.chat(cid)['messages'] if m['id'] == mid), None)


def finals(app, cid):
    """Every assistant message in the chat with a final line (the host's own action summaries have none)."""
    return [m['content'] for m in app.chat(cid)['messages'] if m['role'] == 'assistant' and ev.FINAL_LINE.search(m['content'])]


def shown(env, kind='token'):
    return ''.join(data['text'] for name, data in env.events if name == kind)


def streamed(app, rid):
    return ''.join(r['text'] for r in app.store.query(
        "SELECT text FROM run_stream_events WHERE run_id=? AND kind='delta' ORDER BY sequence", (rid,)))


def committed_responses(app, rid):
    return [json.loads(r['payload'])['response'] for r in app.store.query(
        "SELECT payload FROM run_events WHERE run_id=? AND kind='response_committed' ORDER BY sequence", (rid,))]


def resume_lines(messages):
    return [m['content'] for m in messages if m.get('role') == 'user' and str(m.get('content')).startswith('HOST RESUME')]


def withheld(app, rid):
    return [json.loads(r['payload']) for r in app.store.query(
        "SELECT payload FROM run_events WHERE run_id=? AND kind='reply_withheld' ORDER BY sequence", (rid,))]


def progress_labels(env, rid):
    return [d['label'] for name, d in env.events if name == 'progress' and d.get('run_id') == rid]


def last_host(messages):
    return next(m['content'] for m in reversed(messages) if m.get('role') == 'user')


def attempts(app, rid):
    """{attempt id: (settled state, text)} from the stream journal."""
    rows = app.store.query('SELECT attempt_id,kind,state,text FROM run_stream_events WHERE run_id=? ORDER BY sequence',
                           (rid,))
    found = {}
    for row in rows:
        state, text = found.get(row['attempt_id'], (None, ''))
        found[row['attempt_id']] = (row['state'] if row['kind'] == 'settle' else state,
                                    text + (row['text'] or '') if row['kind'] == 'delta' else text)
    return found


def labels(messages):
    """{url: [S<n>]} for the pages this run read, as a model reads them off its own ReadURL results."""
    calls = {c['id']: json.loads(c['function']['arguments']) for m in messages if m.get('role') == 'assistant'
             for c in m.get('tool_calls') or () if c['function']['name'] == 'ReadURL'}
    found = {}
    for m in messages:
        if m.get('role') == 'tool' and m.get('tool_call_id') in calls:
            result = json.loads(m['content'])
            if isinstance(result, dict) and result.get('source_id'):
                found[calls[m['tool_call_id']]['url']] = '[%s]' % result['source_id']
    return found


def dues(prose):
    """A research-dues answer: `prose`, then the corpus's canonical cited final line, from the run's own labels."""
    def answer(messages):
        cite = labels(messages)
        return final(prose.format(**{'notice': cite[DUES_NOTICE], 'schedule': cite[SCHEDULE]})
                     + 'FINAL: EUR 0.47 (2026-07-01) %s; EUR 0.42 (2026-01-15) %s' % (cite[DUES_NOTICE], cite[SCHEDULE]))
    return answer


DUES_WORK = [call('WebSearch', {'query': 'Port Ellery harbour dues cargo vessels per gross tonne'}, 'search',
                  'Searching the web.'),
             {'role': 'assistant', 'content': 'Reading the two official pages.', 'tool_calls': [
                 {'id': 'notice', 'type': 'function', 'function': {'name': 'ReadURL', 'arguments': json.dumps({'url': DUES_NOTICE})}},
                 {'id': 'schedule', 'type': 'function', 'function': {'name': 'ReadURL', 'arguments': json.dumps({'url': SCHEDULE})}}]}]
# Pilot 5 case 2: one precise claim in a prose sentence without an [S<n>] citation (its own block, so the cited final
# line below it does not carry a citation to it); the repair cites it.
UNCITED = 'Cargo vessels pay EUR 0.47 per gross tonne since 2026-07-01.\n\n'
CITED = 'Cargo vessels pay EUR 0.47 per gross tonne since 2026-07-01 {notice}.\n\n'


def assert_held(env, app, cid, rid, text):
    """The held text is in the model's context and the audit record, and nowhere the person reads it."""
    run = app.runs.get(rid)
    assert run['state'] == 'waiting_approval'
    assert run['checkpoint'].get('held_reply') is True
    assert any(m.get('role') == 'assistant' and m.get('content') == text for m in run['checkpoint']['messages'])
    assert any(r.get('content') == text for r in committed_responses(app, rid))
    assert text not in shown(env) and text not in streamed(app, rid)
    assert all(text not in m['content'] for m in app.chat(cid)['messages'])
    assert progress_labels(env, rid)[-1] == 'Waiting for your decision; the reply follows it'
    assert withheld(app, rid)[-1] == {'step': run['steps'], 'reason': 'approval', 'attempt_id': None}


# -- an edit plus a test request, staged and approved: exactly one reply, written after the decision -------------

def test_an_edit_and_a_test_request_approved_give_one_reply_written_after_the_decision(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    early = 'I staged the change in calc.py and the test run; they await your approval.\nFINAL: done'
    after = 'The docstring is applied and the tests passed.\nFINAL: done'
    env.results = [PASSED]
    env.script = [call('Read', {'file_path': 'calc.py'}, 'read'),
                  call('Write', {'file_path': 'calc.py', 'content': CHANGED}, 'write', 'Adding the docstring.'),
                  call('RunTests', {'recipe': 'python-unittest'}, 'tests'),
                  final(early), final(after)]
    app.work(chat, 'Document add in calc.py and run the tests. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 4)
    # The model kept the Phase 19 note (the staged-draft workflow is unchanged inside the turn).
    notes = [json.loads(m['content']).get('note') for m in env.requests[3] if m.get('role') == 'tool']
    assert notes[-2:] == [APPROVAL_NOTE, APPROVAL_NOTE]
    assert_held(env, app, cid, rid, early)
    # Before the decision the person sees the narration that came with a tool call, and the approval cards.
    assert reply(app, rid, cid) == 'Adding the docstring.'
    assert {a['kind'] for a in app.store.actions(cid) if a['status'] == 'pending'} == {'edit', 'tests'}
    assert (env.project / 'calc.py').read_text() == SOURCE
    before = len(env.events)
    app.approve(cid, pending(app, cid, 'edit')['id'])
    assert len(env.requests) == 4  # the tests still await a decision, so the run does not continue yet
    app.approve(cid, pending(app, cid, 'tests')['id'])
    settled(env, app, rid, 'completed', 5)
    resumed = resume_lines(env.requests[4])
    assert resumed and resumed[-1].endswith(HELD_RESUME) and H1C not in resumed[-1]
    assert any(m.get('role') == 'assistant' and m.get('content') == early for m in env.requests[4])
    answer = reply(app, rid, cid)
    assert answer == 'Adding the docstring.\n' + after
    assert [v.strip() for v in ev.FINAL_LINE.findall(answer)] == ['done']
    assert finals(app, cid) == [answer]
    assert early not in shown(env) and early not in streamed(app, rid)
    assert ''.join(d['text'] for name, d in env.events[before:] if name == 'token') == '\n' + after
    assert 'held_reply' not in app.runs.get(rid)['checkpoint']
    assert (env.project / 'calc.py').read_text() == CHANGED


# -- a denial: the reply after it is the only one the person reads ------------------------------------------------

def test_a_denial_gives_one_reply_written_after_the_decision(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    early = 'notes.md is staged for your approval.\nFINAL: written'
    after = 'You declined the change, so notes.md was not written.\nFINAL: declined'
    env.script = [call('Write', {'file_path': 'notes.md', 'content': NOTES}, 'write'), final(early), final(after)]
    app.work(chat, 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 2)
    assert_held(env, app, cid, rid, early)
    # No model text was shown, so the host's own status stands in the chat until the reply replaces it.
    assert reply(app, rid, cid) == FALLBACK
    app.reject(cid, pending(app, cid, 'edit')['id'])
    settled(env, app, rid, 'completed', 3)
    resumed = resume_lines(env.requests[2])
    assert resumed and resumed[-1].endswith(HELD_RESUME)
    updates = [m['content'] for m in env.requests[2] if str(m.get('content')).startswith('HOST ACTION UPDATES')]
    assert updates and '"rejected"' in updates[-1]
    assert reply(app, rid, cid) == after
    assert finals(app, cid) == [after]
    assert early not in shown(env) and not (env.project / 'notes.md').exists()


# -- the staged-draft workflow: the model keeps working with the draft before it ends its turn --------------------

def test_the_model_keeps_working_on_the_staged_draft_and_its_narration_is_shown(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    early = 'notes.md is staged and checked; it awaits your approval.\nFINAL: written'
    after = 'notes.md is written.\nFINAL: written'
    # After the decision the applied file differs from the draft view the model read, so the existing source audit
    # asks for a fresh Read before the reply; the model rereads, then writes its reply.
    env.script = [call('Write', {'file_path': 'notes.md', 'content': NOTES}, 'write'),
                  call('Read', {'file_path': 'notes.md'}, 'read', 'Checking the staged draft.'),
                  call('Grep', {'pattern': 'sum'}, 'grep', 'Searching it.'),
                  final(early), call('Read', {'file_path': 'notes.md'}, 'reread'), final(after)]
    app.work(chat, 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 4)
    # The draft is readable and searchable before the decision; nothing reached the disk.
    read = next(json.loads(m['content']) for m in env.requests[2] if m.get('tool_call_id') == 'read')
    assert 'sum of a and b' in json.dumps(read) and not read.get('error'), read
    assert 'sum of a and b' in next(m['content'] for m in env.requests[3] if m.get('tool_call_id') == 'grep')
    assert not (env.project / 'notes.md').exists()
    assert_held(env, app, cid, rid, early)
    # The narration that came with tool calls while the decision was pending is shown once committed, not streamed.
    assert reply(app, rid, cid) == 'Checking the staged draft.\nSearching it.'
    assert 'Checking the staged draft.' in shown(env) and 'Checking the staged draft.' not in streamed(app, rid)
    app.approve(cid, pending(app, cid, 'edit')['id'])
    settled(env, app, rid, 'completed', 6)
    assert resume_lines(env.requests[4])[-1].endswith(HELD_RESUME)
    assert reply(app, rid, cid) == 'Checking the staged draft.\nSearching it.\n' + after
    assert finals(app, cid) == ['Checking the staged draft.\nSearching it.\n' + after]
    assert (env.project / 'notes.md').read_text() == NOTES


# -- a question while an approval pends: the waiting_input path is unchanged ---------------------------------------

def test_a_question_while_an_approval_pends_keeps_the_waiting_input_path(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    after = 'notes.md is written in one line.\nFINAL: written'
    env.script = [call('Write', {'file_path': 'notes.md', 'content': NOTES}, 'write'),
                  call('AskUserQuestion', {'questions': json.dumps(ITEMS), 'required': True}, 'ask', 'One question first.'),
                  final(after)]
    app.work(chat, 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_input', 2)
    run = app.runs.get(rid)
    assert 'held_reply' not in run['checkpoint']
    # The text that came with the question is shown and stored, as before.
    assert reply(app, rid, cid) == 'One question first.'
    question = app.interactions.questions(cid)[0]
    app.question_answer(cid, question['id'], 1, {'style': {'option_id': 'short'}})
    time.sleep(.2)
    assert len(env.requests) == 2 and app.runs.get(rid)['state'] == 'waiting_input'  # the edit still awaits a decision
    app.approve(cid, pending(app, cid, 'edit')['id'])
    settled(env, app, rid, 'completed', 3)
    resumed = resume_lines(env.requests[2])
    assert resumed and resumed[-1].endswith(H1C) and HELD_RESUME not in resumed[-1]
    assert reply(app, rid, cid) == 'One question first.\n' + after


# -- a restart while waiting: the held text stays private and the reply follows the decision ----------------------

def test_a_restart_while_waiting_keeps_the_held_text_private_and_the_reply_follows_the_decision(env):
    first = env.start()
    chat = first.new_chat(str(env.project))
    cid = chat['id']
    early = 'notes.md awaits your approval.\nFINAL: written'
    after = 'notes.md is written.\nFINAL: written'
    env.script = [call('Write', {'file_path': 'notes.md', 'content': NOTES}, 'write'), final(early), final(after)]
    first.work(chat, 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = first.runs.list(cid)[0]['id']
    settled(env, first, rid, 'waiting_approval', 2)
    first.close()
    second = env.start()
    assert second.runs.get(rid)['state'] == 'waiting_approval'
    assert_held(env, second, cid, rid, early)
    second.approve(cid, pending(second, cid, 'edit')['id'])
    settled(env, second, rid, 'completed', 3)
    assert resume_lines(env.requests[2])[-1].endswith(HELD_RESUME)
    assert reply(second, rid, cid) == after
    assert all(early not in m['content'] for m in second.chat(cid)['messages'])
    assert (env.project / 'notes.md').read_text() == NOTES


# -- three approval rounds in one run: one reply ------------------------------------------------------------------

def test_three_approval_rounds_in_one_run_give_one_reply(env):
    # Pilot 5 code-payroll and recovery-tests: one final line per approval round, three in all.
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    rounds = ['%s.md is staged and awaits your approval.\nFINAL: written' % name for name in ('one', 'two', 'three')]
    after = 'one.md, two.md and three.md are written.\nFINAL: written'
    env.script = [call('Write', {'file_path': 'one.md', 'content': NOTES}, 'one'), final(rounds[0]),
                  call('Write', {'file_path': 'two.md', 'content': NOTES}, 'two', 'Writing the second note.'),
                  final(rounds[1]),
                  call('Write', {'file_path': 'three.md', 'content': NOTES}, 'three'), final(rounds[2]), final(after)]
    app.work(chat, 'Write one.md, two.md and three.md describing add, one at a time. End with `FINAL: <word>`.',
             False, [])
    rid = app.runs.list(cid)[0]['id']
    for index, requests in enumerate((2, 4, 6)):
        settled(env, app, rid, 'waiting_approval', requests)
        assert_held(env, app, cid, rid, rounds[index])
        app.approve(cid, pending(app, cid, 'edit')['id'])
    settled(env, app, rid, 'completed', 7)
    for request in (2, 4, 6):
        assert resume_lines(env.requests[request])[-1].endswith(HELD_RESUME)
    answer = reply(app, rid, cid)
    assert answer == 'Writing the second note.\n' + after
    assert [v.strip() for v in ev.FINAL_LINE.findall(answer)] == ['written'] and finals(app, cid) == [answer]
    assert all(text not in shown(env) for text in rounds)
    assert [w['reason'] for w in withheld(app, rid)] == ['approval'] * 3


# -- a completion-gate repair: the draft that failed the check is withdrawn, the repair is the reply --------------

def test_a_gate_repair_whose_second_attempt_passes_gives_one_reply_that_completes(env):
    # Pilot 5 case 2 (research-harbour, research-dues, deleg-research): one precise claim without an [S<n>] citation.
    app = env.start(web=True)
    chat = app.new_chat()
    cid = chat['id']
    env.script = [*DUES_WORK, dues(UNCITED), dues(CITED)]
    app.work(chat, DUES_GOAL, False, [])
    rid = app.runs.list(cid)[0]['id']
    run = settled(env, app, rid, 'completed', 4)
    draft, repaired = (r['content'] for r in committed_responses(app, rid)[-2:])
    check = last_host(env.requests[3])
    assert check.startswith('HOST COMPLETION CHECK: ') and 'have no [S<n>] citation' in check
    assert check.endswith(WITHHELD_NOTE)
    # The draft stays in the model's context; the repair, checked on its own, is the only reply.
    assert any(m.get('role') == 'assistant' and m.get('content') == draft for m in env.requests[3])
    answer = reply(app, rid, cid)
    assert answer == 'Searching the web.\nReading the two official pages.\n' + repaired
    assert NOTICE not in answer and [v.strip()[:8] for v in ev.FINAL_LINE.findall(answer)] == ['EUR 0.47']
    assert draft not in shown(env) and all(draft not in m['content'] for m in app.chat(cid)['messages'])
    assert run['error'] is None and 'held_reply' not in run['checkpoint']
    # The draft streamed live: its attempt stays committed in the journal (it is the model's committed response) and
    # is announced as withdrawn, in a run event and to the live panel, with a visible activity line.
    streams = attempts(app, rid)
    [(attempt_id, _)] = [(a, s) for a, s in streams.items() if s == ('committed', draft)]
    assert withheld(app, rid) == [{'step': 3, 'reason': 'completion_check', 'attempt_id': attempt_id}]
    assert [d for name, d in env.events if name == 'stream' and d.get('type') == 'withdrawn'] == [
        {'chat_id': cid, 'run_id': rid, 'attempt_id': attempt_id, 'type': 'withdrawn', 'authorization': 'none'}]
    assert 'Draft withdrawn: the completion check asked for a fix; the reply follows it' in progress_labels(env, rid)
    assert ('committed', repaired) in streams.values()


def test_exhausted_repairs_present_the_last_attempt_with_the_hosts_notice(env):
    # The research repair is offered once: a second uncited attempt is the reply, with the notice; the first never.
    app = env.start(web=True)
    chat = app.new_chat()
    cid = chat['id']
    second = UNCITED.replace('since', 'from')
    env.script = [*DUES_WORK, dues(UNCITED), dues(second)]
    app.work(chat, DUES_GOAL, False, [])
    rid = app.runs.list(cid)[0]['id']
    run = settled(env, app, rid, 'verification_required', 4)
    draft, last = (r['content'] for r in committed_responses(app, rid)[-2:])
    answer = reply(app, rid, cid)
    assert answer.startswith('Searching the web.\nReading the two official pages.\n' + last + '\n\n' + NOTICE)
    assert 'have no [S<n>] citation' in answer.split(NOTICE, 1)[1] and run['error']
    assert len(ev.FINAL_LINE.findall(answer)) == 1 and draft not in answer and draft not in shown(env)
    assert [w['reason'] for w in withheld(app, rid)] == ['completion_check']
    assert 'held_reply' not in run['checkpoint']


def test_a_gate_repair_inside_a_run_that_also_waits_for_an_approval(env):
    # After the decision, the draft the model read was applied, so the source audit holds the first reply; the model
    # rereads and the second reply is the only one shown, along with the narration.
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    early = 'notes.md is staged and checked; it awaits your approval.\nFINAL: written'
    first = 'notes.md is written.\nFINAL: written'
    second = 'notes.md is written and reads as staged.\nFINAL: written'
    env.script = [call('Write', {'file_path': 'notes.md', 'content': NOTES}, 'write'),
                  call('Read', {'file_path': 'notes.md'}, 'read', 'Checking the staged draft.'),
                  final(early), final(first), call('Read', {'file_path': 'notes.md'}, 'reread'), final(second)]
    app.work(chat, 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 3)
    assert_held(env, app, cid, rid, early)
    app.approve(cid, pending(app, cid, 'edit')['id'])
    settled(env, app, rid, 'completed', 6)
    assert resume_lines(env.requests[3])[-1].endswith(HELD_RESUME)
    check = last_host(env.requests[4])
    assert check.startswith('HOST COMPLETION CHECK: ') and 'changed' in check and check.endswith(WITHHELD_NOTE)
    answer = reply(app, rid, cid)
    assert answer == 'Checking the staged draft.\n' + second and finals(app, cid) == [answer]
    assert all(text not in shown(env) for text in (early, first))
    assert [w['reason'] for w in withheld(app, rid)] == ['approval', 'completion_check']
    assert withheld(app, rid)[-1]['attempt_id'] in attempts(app, rid)  # the first reply streamed, then was withdrawn


def test_a_promise_without_a_change_is_withdrawn_and_the_reply_follows_the_work(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    promise = "I'll create notes.md now."
    early = 'notes.md awaits your approval.\nFINAL: written'
    after = 'notes.md is written.\nFINAL: written'
    env.script = [final(promise), call('Write', {'file_path': 'notes.md', 'content': NOTES}, 'write', 'Writing notes.md.'),
                  final(early), final(after)]
    app.work(chat, 'Create notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 3)
    check = last_host(env.requests[1])
    assert check.startswith('HOST CHECK: No file change was recorded') and check.endswith(WITHHELD_NOTE)
    app.approve(cid, pending(app, cid, 'edit')['id'])
    settled(env, app, rid, 'completed', 4)
    assert reply(app, rid, cid) == 'Writing notes.md.\n' + after
    assert promise not in shown(env) and [w['reason'] for w in withheld(app, rid)] == ['promise_check', 'approval']


# -- merge (i20): a claim-free repair after a withdrawn draft never completes a research run ------------------------
# The H20 worker found that after a withdrawn draft, a repair sentence with no claims could pass the research
# completion gate alone (it is checked on its own). g20-research's rule asks each uncovered sub-question of a planned
# run for its own "could not be verified" statement, so that sentence must not complete; for any run, the dropped-claims
# rule (research.completion_issues, prior_claims) holds it too. No final-line format is used.

DUES_QUESTIONS = ("What are Port Ellery's current harbour dues for cargo vessels per gross tonne?",
                  'Which cargo vessel rate did the current rate replace, and when did each rate take effect?')
CLAIM_FREE = 'Each value above is stated in the page I cite next to it, so the answer stands.'


def planned_searches(messages):
    plan = next(json.loads(m['content']) for m in messages if m.get('role') == 'tool' and m.get('tool_call_id') == 'plan')
    queries = [q['text'] for q in plan['queries'] if q['id'].endswith('.literal')]
    return {'role': 'assistant', 'content': 'Searching the web.', 'tool_calls': [
        {'id': 'search%d' % i, 'type': 'function', 'function': {'name': 'WebSearch', 'arguments': json.dumps({'query': q})}}
        for i, q in enumerate(queries)]}


def dropped_answer(env, app, cid, rid, requests):
    """The draft fails the check and is withdrawn; the repair drops the answer: the run must not complete."""
    run = settled(env, app, rid, 'verification_required', requests)
    assert run['state'] != 'completed' and run['error']
    draft = committed_responses(app, rid)[-2]['content']
    answer = reply(app, rid, cid)
    assert CLAIM_FREE in answer and NOTICE in answer and answer.index(CLAIM_FREE) < answer.index(NOTICE)
    assert draft not in answer and draft not in shown(env)
    assert [w['reason'] for w in withheld(app, rid)] == ['completion_check']
    return answer.split(NOTICE, 1)[1]


def test_a_planned_research_run_whose_repair_drops_the_answer_ends_verification_required(env):
    app = env.start(web=True)
    chat = app.new_chat()
    cid = chat['id']
    draft = ('Cargo vessels pay EUR 0.47 per gross tonne since 2026-07-01.\n\n'
             'The previous rate was EUR 0.42 per gross tonne from 2026-01-15 {schedule}.')
    env.script = [call('ResearchPlan', {'questions': json.dumps(list(DUES_QUESTIONS))}, 'plan', 'Planning the two sub-questions.'),
                  planned_searches, DUES_WORK[1], lambda messages: final(draft.format(schedule=labels(messages)[SCHEDULE])),
                  final(CLAIM_FREE)]
    app.work(chat, 'Research the current Port Ellery harbour dues for cargo vessels and cite your sources.', False, [])
    rid = app.runs.list(cid)[0]['id']
    notice = dropped_answer(env, app, cid, rid, 5)
    assert 'not covered by page sources; say what could not be verified' in notice
    assert all(question[:40] in notice for question in DUES_QUESTIONS)  # each uncovered question is named for the person


# Parent follow-up 3: without a ResearchPlan there are no sub-questions, so this case was a strict xfail (the
# claim-free repair completed, run14). research.completion_issues now treats a reply after a research repair that
# states no claim and no "could not be verified", following a draft that made claims, as one unrepairable issue.
UNPLANNED_GOAL = 'Search the web for the current Port Ellery harbour dues for cargo vessels and cite your sources.'


def claimed_draft(messages):
    return final(UNCITED + 'The schedule lists EUR 0.42 from 2026-01-15 %s.' % labels(messages)[SCHEDULE])


def test_an_unplanned_research_run_whose_repair_drops_the_answer_ends_verification_required(env):
    app = env.start(web=True)
    chat = app.new_chat()
    cid = chat['id']
    env.script = [*DUES_WORK, claimed_draft, final(CLAIM_FREE)]
    app.work(chat, UNPLANNED_GOAL, False, [])
    rid = app.runs.list(cid)[0]['id']
    notice = dropped_answer(env, app, cid, rid, 4)
    assert 'states none of the claims of the draft before it and does not say what could not be verified' in notice
    checks = [json.loads(r['payload']) for r in app.store.query(
        "SELECT payload FROM run_events WHERE run_id=? AND kind='research_check' ORDER BY sequence", (rid,))]
    assert [c['dropped'] for c in checks] == [False, True] and checks[0]['issues'] >= 1


def test_an_unplanned_run_whose_first_reply_honestly_makes_no_claim_still_completes(env):
    # Control: no draft made claims and no repair happened, so nothing was dropped; the reply is shown at once.
    app = env.start(web=True)
    chat = app.new_chat()
    cid = chat['id']
    honest = 'I read the harbour notice and the schedule of dues for this request.'
    env.script = [*DUES_WORK, final(honest)]
    app.work(chat, UNPLANNED_GOAL, False, [])
    rid = app.runs.list(cid)[0]['id']
    run = settled(env, app, rid, 'completed', 3)
    assert run['error'] is None and reply(app, rid, cid).endswith(honest) and NOTICE not in reply(app, rid, cid)
    assert withheld(app, rid) == []


def test_a_repair_that_says_what_could_not_be_verified_still_completes(env):
    # Control: after the withdrawn draft, a claim-free repair that states what could not be verified is honest.
    app = env.start(web=True)
    chat = app.new_chat()
    cid = chat['id']
    honest = 'I could not verify the current cargo vessel rate from the pages I read.'
    env.script = [*DUES_WORK, claimed_draft, final(honest)]
    app.work(chat, UNPLANNED_GOAL, False, [])
    rid = app.runs.list(cid)[0]['id']
    run = settled(env, app, rid, 'completed', 4)
    answer = reply(app, rid, cid)
    assert run['error'] is None and answer.endswith(honest) and NOTICE not in answer
    assert committed_responses(app, rid)[-2]['content'] not in answer
    assert [w['reason'] for w in withheld(app, rid)] == ['completion_check']


# -- paths that keep the Phase 19 behaviour ------------------------------------------------------------------------

def test_a_4k_window_keeps_the_phase19_path(env):
    # Nothing new at a budget of 4096: the turn-end text is shown and the H1c resume line is used, so a model that
    # writes a final line before the decision still shows two (the pilot 5 shape; above 4K the first is held).
    app = env.start(context=4096)
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    early = 'notes.md is staged for your approval.\nFINAL: written'
    env.script = [call('Write', {'file_path': 'notes.md', 'content': NOTES}, 'write'), final(early),
                  final('It is applied.\nFINAL: written')]
    app.work(chat, 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 2)
    assert reply(app, rid, cid) == early and early in streamed(app, rid)
    assert 'held_reply' not in app.runs.get(rid)['checkpoint']
    app.approve(cid, pending(app, cid, 'edit')['id'])
    settled(env, app, rid, 'completed', 3)
    resumed = resume_lines(env.requests[2])
    assert resumed[-1].endswith(H1C) and HELD_RESUME not in resumed[-1]
    assert reply(app, rid, cid) == early + '\nIt is applied.\nFINAL: written'
    assert len(ev.FINAL_LINE.findall(reply(app, rid, cid))) == 2


def test_a_turn_without_a_pending_proposal_is_shown_and_streamed_as_before(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [call('Read', {'file_path': 'calc.py'}, 'read', 'Reading calc.py.'), final('It adds.\nFINAL: done')]
    app.work(chat, 'Check that add in calc.py adds two numbers. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'completed', 2)
    assert reply(app, rid, cid) == 'Reading calc.py.\nIt adds.\nFINAL: done'
    assert streamed(app, rid) == 'Reading calc.py.It adds.\nFINAL: done'
    assert 'held_reply' not in app.runs.get(rid)['checkpoint']


def test_the_held_resume_clause_cost_is_pinned():
    # Said once, on the resume after a held turn, above 4096 only (o200k, the bundled tokenizer).
    from pebble.token_budget import TokenBudget
    count = lambda text: len(TokenBudget.encoding().encode(text, disallowed_special=()))
    assert (count(HELD_RESUME), count(H1C)) == (28, 15)  # measured; the H1c clause is the one it replaces
    # Said with each completion-gate or promise check above 4096 (it is withdrawn there), never at 4K.
    assert count(WITHHELD_NOTE) == 29  # measured


def test_the_last_allowed_step_is_not_held_because_no_reply_could_follow(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    text = 'Write notes.md describing add in calc.py.'
    early = 'notes.md is staged for your approval.'
    rid = app.runs.create(cid, text, app.runtime_config(chat, False, {'max_steps': 2, 'max_seconds': 120}), [])
    env.script = [call('Write', {'file_path': 'notes.md', 'content': NOTES}, 'write'), final(early)]
    app.work(chat, text, False, [], run_id=rid)
    settled(env, app, rid, 'waiting_approval', 2)
    assert reply(app, rid, cid) == early
    assert 'held_reply' not in app.runs.get(rid)['checkpoint']
