"""Phase 24 track t24-agent: the runner says what happened (V1-083, V1-084, V1-086, V1-089, V1-090, V1-074, V1-005,
V1-029, V1-081).

- The same failing call: a host note on the third identical failure (above 4K), an honest stop on the fifth (V1-083).
- Closing texts name drafts only when the run staged some (V1-083).
- A promise-only reply in a chat without a project is withheld once above 4K (V1-089).
- The project listing is whole names with an omitted count, never cut mid-JSON (V1-090); it reuses AgentGuard's walk
  when the adapter offers one (V1-031).
- Tool errors reach the model once, without absolute paths, and an unoffered tool names the offered ones (V1-084).
- The person reads the gate's person-facing sentences; the model's repair keeps the issues (V1-086).
- Settlement steps are isolated: a failing note or hook never skips cleanup or the lane release (V1-074), and a
  terminal run ends its AgentGuard session (V1-005).
- A fresh run carries a held run's unverified paths into AgentGuard, and "Ask Pebble to finish" gives exactly one
  more repair (V1-029).
- A presented reply is one token event, not one per delta (V1-081).

Scripted models only; no network.
"""
import copy
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import runner
from pebble.app import App
from pebble.policy import PolicyError, Workspace

LARGE = 32768


class Env:
    """A real App on a scripted route; `script(n, messages, tools)` returns each reply (text or a dict)."""

    def __init__(self, tmp_path, budget=None):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.tmp, self.events, self.requests = tmp_path, [], []
        self.app = App(tmp_path / 'data', tmp_path / 'resources', lambda *event: self.events.append(event))
        if budget is not None:
            self.app.store.set_setting('model_path', '')
            self.app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                                           'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                                           'context': budget, 'budget': budget, 'tools': True}])
            self.app.model.select('route')
        self.script = lambda n, messages, tools: 'Done.'
        self.app.model.complete = self.complete

    def complete(self, messages, tools, cancel, token, deliberate=False):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        reply = self.script(len(self.requests), messages, tools)
        if isinstance(reply, str):
            reply = {'role': 'assistant', 'content': reply}
        if reply.get('content'):
            for start in range(0, len(reply['content']), 7):  # many deltas, as a streaming provider sends them
                token(reply['content'][start:start + 7])
        return reply

    def project(self, files=None):
        folder = self.tmp / 'project'
        folder.mkdir(exist_ok=True)
        for name, text in (files or {'calc.py': 'def add(a, b):\n    return a - b\n'}).items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text)
        return folder

    def work(self, text, folder=None, limits=None, chat=None):
        chat = chat or self.app.new_chat(str(folder) if folder else None)
        rid = None
        if limits:
            rid = self.app.runs.create(chat['id'], text, self.app.runtime_config(chat, False, limits), [])
        runner.run(self.app, self.app.chat(chat['id']), text, False, [], run_id=rid, app_inventory=lambda: [])
        run = self.app.runs.list(chat['id'])[0]
        return SimpleNamespace(chat=chat, cid=chat['id'], run=self.app.runs.get(run['id']), rid=run['id'],
                               answer=self.answer(chat['id']), notes=self.notes(run['id']))

    def answer(self, cid):
        messages = self.app.chat(cid)['messages']
        return messages[-1]['content'] if messages and messages[-1]['role'] == 'assistant' else None

    def notes(self, rid):
        return [(r['kind'], json.loads(r['payload'])) for r in self.app.store.query(
            'SELECT kind,payload FROM run_events WHERE run_id=? ORDER BY sequence', (rid,))]

    def close(self):
        self.app.active = self.app.active_run = None
        self.app.close()


@pytest.fixture
def env(tmp_path):
    made = []

    def make(budget=None, name='env'):
        made.append(Env(tmp_path / name, budget))
        return made[-1]
    yield make
    for item in made:
        item.close()


def call(name, args, ident):
    return {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def tool_results(messages):
    return [json.loads(m['content']) for m in messages if m.get('role') == 'tool']


# -- V1-083: the same failing call ---------------------------------------------------------------------------------

@pytest.mark.parametrize('budget', [None, LARGE])
def test_the_third_identical_failure_adds_a_host_note_and_the_fifth_stops_honestly(env, budget):
    e = env(budget)
    e.script = lambda n, messages, tools: call('Read', {'file_path': 'missing.py'}, f'c{n}')
    result = e.work('Fix the parser in missing.py.', e.project())
    assert len(e.requests) == 5, 'stopped after the fifth identical failure (0.6.1 ran to the 64-step limit)'
    results = tool_results(e.requests[-1][0])
    assert len(results) == 4 and all('error' in r for r in results)
    noted = ['host_note' in r for r in results]
    assert noted == ([False, False, True, True] if budget else [False] * 4)  # nothing new at 4K
    assert result.run['state'] == 'failed'
    assert result.run['error'] == 'The same tool call failed 5 times in a row (Read), so Pebble stopped the run.'
    assert result.answer.endswith('I stopped because the same step kept failing (Read, 5 times with the same error). '
                                  'Nothing was changed.')
    assert ('loop_stopped', {'tool': 'Read', 'count': 5}) in result.notes
    assert 'drafts and evidence' not in result.answer


def test_a_success_in_between_resets_the_count(env):
    e = env(LARGE)
    folder = e.project()
    pattern = ['missing.py', 'missing.py', 'calc.py', 'missing.py', 'missing.py', 'missing.py']

    def script(n, messages, tools):
        return call('Read', {'file_path': pattern[n - 1]}, f'c{n}') if n <= len(pattern) else 'I could not find it.'
    e.script = script
    result = e.work('Fix the parser in missing.py.', folder)
    assert result.run['state'] != 'failed' and len(e.requests) == len(pattern) + 1


# -- V1-083: closing texts -----------------------------------------------------------------------------------------

def test_the_step_limit_text_mentions_drafts_only_when_some_were_staged(env):
    e = env()
    # The Weather tool with no city is answered at once, with no network and no change (news words offer it).
    e.script = lambda n, messages, tools: call('Weather', {'city': ''}, f'w{n}')
    nothing = e.work('Search the news for my week plan.', limits={'max_steps': 2, 'max_seconds': 60})
    assert nothing.run['state'] == 'budget_exhausted'
    assert nothing.answer.endswith('I reached the step limit before finishing, and nothing was changed.')
    folder = e.project()
    e.requests.clear()
    steps = [call('Read', {'file_path': 'calc.py'}, 'r1'),
             call('Write', {'file_path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'}, 'w1')]
    e.script = lambda n, messages, tools: steps[n - 1] if n <= len(steps) else call('Weather', {'city': ''}, f'w{n}')
    staged = e.work('Fix add in calc.py.', folder, limits={'max_steps': 2, 'max_seconds': 60})
    assert staged.answer.endswith('I reached the step limit. The drafts staged so far are ready for review.')


def test_the_time_limit_reply_with_nothing_changed_is_still_time_limit_reply():
    assert runner.LIMIT_TEXT['time'][None] == runner.TIME_LIMIT_REPLY
    assert 'drafts' not in runner.TIME_LIMIT_REPLY and 'drafts' in runner.LIMIT_TEXT['time']['staged']


# -- V1-089: promise-only replies in a chat without a project ----------------------------------------------------

def test_a_promise_only_reply_is_withheld_once_above_4k(env):
    e = env(LARGE)
    replies = iter(["Sure, let me search the web for that and I'll get back to you.",
                    'I cannot browse right now, so I cannot confirm the 2026 winner.'])
    e.script = lambda n, messages, tools: next(replies)
    result = e.work('Who won the 2026 Tour de France?')
    assert len(e.requests) == 2 and result.run['state'] == 'completed'
    assert result.answer == 'I cannot browse right now, so I cannot confirm the 2026 winner.'
    assert [d['reason'] for k, d in result.notes if k == 'reply_withheld'] == ['completion_check']
    assert e.requests[1][0][-1]['content'].startswith(runner.PROMISE_CHECK)


def test_at_4k_a_promise_only_reply_completes_as_before(env):
    e = env()
    e.script = lambda n, messages, tools: "Sure, let me search the web for that and I'll get back to you."
    result = e.work('Who won the 2026 Tour de France?')
    assert len(e.requests) == 1 and result.answer.startswith('Sure, let me search')


def test_an_ordinary_answer_that_ends_with_let_me_know_is_not_a_promise(env):
    e = env(LARGE)
    e.script = lambda n, messages, tools: 'Paris is the capital of France. Let me know if you need more.'
    result = e.work('What is the capital of France?')
    assert len(e.requests) == 1 and result.run['state'] == 'completed'


# -- V1-090, V1-031: the project listing ---------------------------------------------------------------------------

def listing_of(messages):
    found = next(m['content'] for m in messages if m['content'].startswith('UNTRUSTED project file listing'))
    return found.split('\n', 1)[1]


def test_the_listing_is_whole_names_with_an_omitted_count_and_never_longer(env):
    e = env()
    names = {f'src/components/settings_panel_{i:03}.py': 'x = 1\n' for i in range(120)}
    folder = e.project(names)
    e.work('Fix the settings panel.', folder)
    text = listing_of(e.requests[0][0])
    body = json.loads(text)  # valid JSON (0.6.1 ended mid-name: `"src/components/set`)
    listed = sorted(names)
    assert body['files'] == listed[:len(body['files'])] and body['omitted'] == 120 - len(body['files'])
    assert len(text) <= len(json.dumps(listed[:70])[:2000])  # no longer than the Phase 23 form


def test_a_small_listing_keeps_its_phase_23_bytes():
    names = ['a.py', 'b/c.py']
    assert runner.listing_text(names) == json.dumps(names)
    truncated = json.loads(runner.listing_text(['x.py'] * 1500, truncated=True))
    assert truncated['more_files_not_counted'] is True and truncated['omitted'] == 1500 - len(truncated['files'])


def test_the_listing_reuses_agentguards_walk_when_the_adapter_offers_it(env, monkeypatch):
    e = env()
    folder = e.project()
    walks = []
    inventory = Workspace.inventory

    def counted(self, *args, **kwargs):
        walks.append(str(self.root))
        return inventory(self, *args, **kwargs)
    monkeypatch.setattr(Workspace, 'inventory', counted)
    # Merge (m24b): GuardAdapter.listing (this track's §5.2) landed, so the baseline turns it off for this instance.
    e.app.guard.listing = None
    e.work('Fix add in calc.py.', folder)
    without = len(walks)
    walks.clear()
    del e.app.guard.listing  # the adapter's own listing: the names of begin()'s walk
    e.requests.clear()
    e.work('Fix add in calc.py.', folder)
    assert listing_of(e.requests[0][0]) == '["calc.py"]' and len(walks) == without - 1, 'the real listing saves a walk'
    walks.clear()
    e.app.guard.listing = lambda root: {'files': ['calc.py', 'from_the_guard.py'], 'truncated': False}
    e.requests.clear()
    e.work('Fix add in calc.py.', folder)
    assert listing_of(e.requests[0][0]) == '["calc.py", "from_the_guard.py"]'
    assert len(walks) == without - 1, 'the runner did not walk the project again'


# -- V1-084: tool errors -----------------------------------------------------------------------------------------

def test_a_refusal_reaches_the_model_once_without_reporting_error(env):
    e = env(LARGE)
    steps = [call('Read', {'file_path': 'calc.py'}, 'r1'),
             call('Edit', {'file_path': 'calc.py', 'old_string': 'not in the file', 'new_string': 'x'}, 'e1')]
    e.script = lambda n, messages, tools: steps[n - 1] if n <= len(steps) else 'I could not edit it.'
    e.work('Fix add in calc.py.', e.project())
    refused = tool_results(e.requests[2][0])[-1]
    assert set(refused) == {'error'}, refused  # 0.6.1 repeated it as reporting_error


def test_an_unoffered_tool_names_the_offered_ones_above_4k_and_keeps_the_pinned_refusal(env):
    for budget in (None, LARGE):
        e = env(budget, name=f'b{budget}')
        e.script = lambda n, messages, tools: call('RunCommand', {'command': 'ls'}, 'x1') if n == 1 else 'No shell here.'
        e.work('List my files.')
        error = tool_results(e.requests[1][0])[0]['error']
        assert error.startswith('Tool request does not match its allowed schema.')  # validate_phase16/17 match this
        if budget:
            offered = [t['function']['name'] for t in e.requests[0][1]]
            assert 'RunCommand is not available in this request. Available tools: ' + ', '.join(offered) + '.' in error
        else:
            assert error == 'Tool request does not match its allowed schema.'  # nothing new at 4K


def test_tool_errors_never_carry_absolute_project_or_home_paths(tmp_path):
    folder = tmp_path / 'project'
    folder.mkdir()
    ws = Workspace(str(folder))
    missing = FileNotFoundError(2, 'No such file or directory', str(ws.root / 'src' / 'a.py'))
    assert runner.tool_error(missing, ws) == 'No such file or directory: src/a.py'
    home = str(Path.home())
    assert runner.project_relative(f'cannot read {home}/notes.txt', None) == 'cannot read ~/notes.txt'
    assert runner.tool_error(PolicyError(f'Path {ws.root}/x is outside the project.'), ws) == \
        'Path x is outside the project.'


# -- V1-086: the person's notice ---------------------------------------------------------------------------------

def test_the_person_reads_the_gates_sentences_and_the_model_keeps_the_issue(env, monkeypatch):
    e = env()
    issue = ('The durable plan still has unfinished steps (s1). Mark the finished ones completed with UpdatePlan '
             'and their evidence_ids (resolved receipts: 1:p1), or report the blocker.')
    notice = "Some steps of the task plan aren't marked done yet."
    monkeypatch.setattr(e.app.controller, 'completion', lambda rid, ws, answer: {
        'state': 'verification_required', 'issues': [issue], 'repairable': False, 'notices': [notice]})
    result = e.work('Plan the trip.')
    assert result.answer.endswith('\n\n> Verification remains incomplete: ' + notice)
    assert 'UpdatePlan' not in result.answer and '1:p1' not in result.answer
    assert result.run['error'] == issue  # the run's record (and every repair turn) keeps the model-facing text


def test_research_issues_stay_in_the_notice():
    gate = {'issues': ['A', 'The claim "EUR 0.47" has no [S<n>] citation.'], 'notices': ['Pebble changed x.', runner.RESEARCH_NOTICE]}
    assert runner.person_notice(gate) == 'A\nThe claim "EUR 0.47" has no [S<n>] citation.'  # pinned by Phase 20
    gate['research_issues'] = ['The claim "EUR 0.47" has no [S<n>] citation.']
    assert runner.person_notice(gate) == 'Pebble changed x.\nThe claim "EUR 0.47" has no [S<n>] citation.'
    assert runner.person_notice({'issues': ['raw'], 'repairable': True}) == 'raw'


# -- V1-074, V1-005: settlement ------------------------------------------------------------------------------------

def test_a_failing_note_never_skips_cleanup_or_the_lane_release(env, monkeypatch):
    e = env()
    app, seen = e.app, []
    monkeypatch.setattr(app.time_ledger, 'end', lambda rid: (_ for _ in ()).throw(sqlite3.OperationalError('locked')))
    note = app.runs.note

    def refusing(rid, kind, data):
        if kind.endswith('_failed'):
            raise sqlite3.OperationalError('database is locked')  # as under a held write lock
        return note(rid, kind, data)
    monkeypatch.setattr(app.runs, 'note', refusing)
    hook = app.hook_event

    def hooks(event, *args, **kwargs):
        if event == 'run_settled':
            raise RuntimeError('observer down')
        return hook(event, *args, **kwargs)
    monkeypatch.setattr(app, 'hook_event', hooks)
    for name, target in (('processes', 'cancel_run'), ('extensions', 'close_run')):
        original = getattr(getattr(app, name), target)
        monkeypatch.setattr(getattr(app, name), target,
                            lambda *a, _o=original, _n=name: (seen.append(_n), _o(*a))[1])
    suspend = app.agents.suspend
    monkeypatch.setattr(app.agents, 'suspend', lambda *a: (seen.append('agents'), suspend(*a))[1])
    result = e.work('Say hello.')
    assert result.run['state'] == 'completed', 'the transition ran although the first note failed (0.6.1: skipped)'
    assert {'processes', 'agents', 'extensions'} <= set(seen)
    assert app.active is None and app.active_run is None and app.controller.live is None


def test_a_terminal_run_ends_its_agentguard_session_and_a_wait_keeps_it(env, monkeypatch):
    e = env()
    ended = []
    monkeypatch.setattr(e.app.guard, 'end', lambda root, session: ended.append((root, session)) or {'ended': 1})
    folder = e.project()
    done = e.work('Explain calc.py.', folder)
    assert done.run['state'] == 'completed' and ended == [(None, done.rid)]
    ended.clear()
    e.requests.clear()
    steps = [call('Read', {'file_path': 'calc.py'}, 'r1'),
             call('Write', {'file_path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'}, 'w1')]
    e.script = lambda n, messages, tools: steps[n - 1] if n <= len(steps) else 'Staged.'
    waiting = e.work('Fix add in calc.py.', folder)
    assert waiting.run['state'] == 'waiting_approval' and ended == []


# -- V1-029: held runs -------------------------------------------------------------------------------------------

def test_a_fresh_run_carries_a_held_runs_unverified_paths_into_agentguard(env, monkeypatch):
    e = env()
    folder = e.project()
    begun = []
    begin = e.app.guard.begin
    monkeypatch.setattr(e.app.guard, 'begin', lambda root, cid, prompt, resume=False, changed=(): (
        begun.append(list(changed)), begin(root, cid, prompt, resume=resume, changed=changed))[1])
    monkeypatch.setattr(e.app.controller, 'carried_paths', lambda cid, rid: ['calc.py'])
    e.work('Now also add a docstring.', folder)
    assert begun == [['calc.py']]


def wait_settled(app, rid, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if app.active is None and app.runs.get(rid)['state'] not in ('queued', 'running', 'retry_wait'):
            return app.runs.get(rid)
        time.sleep(0.02)
    raise AssertionError('the run did not settle')


def test_ask_pebble_to_finish_gives_exactly_one_more_repair(env, monkeypatch):
    e = env(LARGE)
    monkeypatch.setattr(e.app.controller, 'completion', lambda rid, ws, answer: {
        'state': 'verification_required', 'issues': ['Run the covering tests.'], 'repairable': True,
        'notices': ['Pebble changed calc.py but did not run the tests that cover it.']})
    held = e.work('Fix add in calc.py.', e.project())
    assert held.run['state'] == 'verification_required' and len(e.requests) == 3  # two repairs, then held
    before = len(e.requests)
    e.app.controller.held(held.cid, held.rid, 'finish', surface='main')
    settled = wait_settled(e.app, held.rid)
    assert settled['state'] == 'verification_required'
    assert len(e.requests) - before == 2, 'one repair turn after the decision, then the hold again'
    assert any('The person asked you to finish this task.' in m.get('content', '') for m in e.requests[before][0]
               if isinstance(m.get('content'), str))


# -- V1-030: the HOST REVIEW turn (off until the parent's Rig B measurement) ---------------------------------------

P90 = ('Write p90(values) in stats.py: return None for an empty list, do not modify the input, and use linear '
       'interpolation between the closest ranks.')


def test_host_review_ships_off_until_measured():
    assert runner.HOST_REVIEW is False


def test_with_host_review_on_a_run_verified_on_old_tests_gets_one_requirements_check(env, monkeypatch):
    monkeypatch.setattr(runner, 'HOST_REVIEW', True)
    reviewed = []
    monkeypatch.setattr(runner, 'needs_review', lambda app, rid, text, ws, actions: reviewed.append(rid) or True)
    e = env(LARGE)
    e.script = lambda n, messages, tools: 'Done. FINAL: done'
    result = e.work(P90, e.project())
    assert len(e.requests) == 2 and e.requests[1][0][-1]['content'].startswith(runner.REVIEW_TEXT)
    assert [d['reason'] for k, d in result.notes if k == 'reply_withheld'] == ['completion_check']
    assert result.run['state'] == 'completed' and len(reviewed) == 1  # once per run
    for budget in (8192,):  # never at 8K or less
        small = env(budget, name='small')
        small.work(P90, small.project())
        assert len(small.requests) == 1


class GateStore:
    def __init__(self, payload):
        self.payload = payload

    def query(self, sql, args=()):
        return [{'payload': json.dumps(self.payload)}] if self.payload is not None else []


@pytest.mark.parametrize('edits,text,gate,expected', [
    (['stats.py'], P90, {'outcome': 'verified', 'tests': {'passed': 3, 'failed': 0}}, True),
    (['stats.py', 'tests/test_stats.py'], P90, {'outcome': 'verified', 'tests': {'passed': 4}}, False),  # new tests
    (['stats.py'], 'Fix the typo in stats.py.', {'outcome': 'verified', 'tests': {'passed': 3}}, False),  # one clause
    (['stats.py'], P90, {'outcome': 'not_verified', 'tests': {'passed': 0}}, False),
    ([], P90, {'outcome': 'verified', 'tests': {'passed': 3}}, False),
])
def test_needs_review_rule(edits, text, gate, expected):
    app = SimpleNamespace(store=GateStore(gate))
    actions = [{'kind': 'edit', 'status': 'applied', 'payload': {'path': p}} for p in edits]
    assert runner.needs_review(app, 'r1', text, object(), actions) is expected


# -- t24-auth: the network wait is labelled ------------------------------------------------------------------------

def test_a_network_wait_says_so_and_is_recorded(env):
    from pebble.recovery import ProviderFailure
    e = env(LARGE)
    failures = [ProviderFailure('no route to the provider', retryable=True, network=True)]
    scripted = e.complete

    def flaky(messages, tools, cancel, token, deliberate=False):
        if failures:
            raise failures.pop()
        return scripted(messages, tools, cancel, token, deliberate)
    e.app.model.complete = flaky
    result = e.work('Say hello.')
    retries = [d for k, d in result.notes if k == 'model_retry']
    assert len(retries) == 1 and retries[0]['network'] is True and retries[0]['attempt'] == 2
    labels = [d['label'] for name, d in e.events if name == 'progress']
    assert 'Waiting for the network' in labels and result.run['state'] == 'completed'


# -- V1-090: the other two bounded blocks ---------------------------------------------------------------------------

def test_recorded_action_results_are_whole_entries_with_an_omitted_count():
    small = [{'kind': 'edit', 'status': 'applied', 'result': {'path': 'a.py'}}]
    assert runner.evidence_text(small) == json.dumps(small)  # the Phase 23 bytes
    big = [{'kind': 'tests', 'status': 'applied', 'result': {'output': 'x' * 1500}} for _ in range(3)]
    text = runner.evidence_text(big)
    body = json.loads(text)
    assert len(text) <= runner.EVIDENCE_CHARS and body['omitted_older'] == 3 - len(body['results'])
    huge = [{'kind': 'tests', 'status': 'applied', 'result': {'output': 'y' * 9000}}]
    body = json.loads(runner.evidence_text(huge))
    assert body['results'][0]['result'] == {'omitted_chars': len(json.dumps(huge[0]['result']))}


def test_a_long_earlier_turn_says_it_was_shortened_at_8k_and_4k_keeps_the_phase14_cut(env):
    long = 'z' * 3000
    for budget in (None, 8192):
        e = env(budget, name=f'h{budget}')
        chat = e.app.new_chat()
        e.app.store.message(chat['id'], 'user', 'Summarise the log.')
        e.app.store.message(chat['id'], 'assistant', long)
        e.work('Continue.', chat=chat)
        [shown] = [m['content'] for m in e.requests[0][0] if m['role'] == 'assistant']
        if budget is None:
            assert shown == long[:2200]  # test_phase15_security pins the exact 4K cut
        else:
            assert shown == long[:2200] + '\n[Earlier turn shortened: the first 2,200 of 3,000 characters are shown.]'


# -- V1-081: one token event per presentation ----------------------------------------------------------------------

@pytest.mark.parametrize('budget', [None, LARGE])
def test_a_presented_reply_is_one_token_event(env, budget):
    e = env(budget)
    reply = 'Here is a longer answer that a provider sends in many small deltas, as it streams.'
    e.script = lambda n, messages, tools: reply
    result = e.work('Tell me something.')
    tokens = [d for name, d in e.events if name == 'token']
    assert [t['text'] for t in tokens] == [reply] and result.answer == reply
