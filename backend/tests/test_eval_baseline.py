"""Phase 18 baseline arm: a minimal loop over the owned App's model route.

The model is a scripted fake (or the real Responses adapter over an in-memory
transport for X1); nothing here opens a network connection or reads a profile,
vault or credential.
"""
import ast
import copy
import hashlib
import json
import os
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import eval_baseline as eb
from pebble import evaluation as ev
from pebble.capabilities import choose
from pebble.recovery import ProviderFailure
from pebble.responses import complete_responses

PROFILE = dict(ev.SCRIPTED_PROFILE)
USAGE = {'input_tokens': 10, 'output_tokens': 2, 'cached_tokens': 1}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('Baseline tests must not open network connections.')
        return connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))


class FakeModel:
    """The provider surface run_baseline uses: current(), complete(), last_usage, compaction hooks."""

    def __init__(self, replies, profile=PROFILE):
        self.replies, self.profile, self.requests = list(replies), profile, []
        self.last_usage, self.context_transform, self.context_observer = {}, 'pebble-compaction', 'pebble-observer'
        self.cancelled = 0

    def current(self):
        return self.profile

    def complete(self, messages, tools, cancel, token, effort):
        assert self.context_transform is None and self.context_observer is None  # no Pebble compaction
        self.requests.append({'messages': copy.deepcopy(messages), 'tools': [t['function']['name'] for t in tools],
                              'effort': effort})
        reply = self.replies.pop(0) if self.replies else final('FINAL: done')
        if callable(reply):
            reply = reply(messages)
        if isinstance(reply, BaseException):
            raise reply
        self.last_usage = dict(USAGE)
        return copy.deepcopy(reply)

    def cancel(self):
        self.cancelled += 1


def app_with(replies, profile=PROFILE):
    return SimpleNamespace(model=FakeModel(replies, profile))


def call(name, args, ident='c1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


def task(**extra):
    return SimpleNamespace(id='t1', prompt='Do the task. End with a final line `FINAL: <value>`.',
                           answer='The typed task answer.', **extra)


def results(model):
    """Tool results the model saw, by call ID, from its last request."""
    return {m['tool_call_id']: json.loads(m['content']) for m in model.requests[-1]['messages'] if m['role'] == 'tool'}


LIMITS = {'max_steps': 12, 'max_seconds': 60}


def workspace(tmp_path, files):
    root = tmp_path / 'ws'
    root.mkdir()
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


# -- published definition ------------------------------------------------------------------

def test_system_prompt_and_tools_are_neutral_minimal_and_published():
    assert len(eb.SYSTEM_PROMPT.split()) <= 150
    assert 'Pebble' not in eb.SYSTEM_PROMPT and 'FINAL:' not in eb.SYSTEM_PROMPT
    assert [t['function']['name'] for t in eb.TOOLS] == ['list_files', 'read_file', 'write_file', 'search_text',
                                                         'ask_user']
    assert [t['function']['name'] for t in eb.WEB_TOOLS] == ['web_search', 'read_url']
    assert [t['function']['name'] for t in eb.TEST_TOOLS] == ['run_tests']
    assert set(eb.TEST_TOOLS[0]['function']['parameters']['properties']) == {'recipe'}  # no command, argv or path
    from pebble.tool_catalogue import TOOLS as PEBBLE_TOOLS
    assert not ({t['function']['name'] for t in eb.TOOLS + eb.TEST_TOOLS + eb.WEB_TOOLS}
                & {t['function']['name'] for t in PEBBLE_TOOLS})
    # It reuses no harness feature, only primitives: the effort chooser, the provider retry policy, and the
    # host's own confined test runner (recipe resolver, workspace identity) and web window sizes.
    tree = ast.parse(Path(eb.__file__).read_text(encoding='utf-8'))
    relative = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level}
    assert relative == {'capabilities', 'recovery', 'policy', 'recipes', 'testing', 'source_ledger'}
    absolute = {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    assert absolute <= {'hashlib', 'json', 'os', 're', 'stat', 'threading', 'time'}


# -- the loop --------------------------------------------------------------------------------

def test_loop_lists_reads_searches_writes_and_finishes_with_a_journal(tmp_path):
    root = workspace(tmp_path, {'a.txt': 'hello world\n', 'sub/b.txt': 'second hello\n'})
    app = app_with([call('list_files', {'path': '.'}, 'l1'), call('read_file', {'path': 'a.txt'}, 'r1'),
                    call('search_text', {'query': 'HELLO'}, 's1'),
                    call('write_file', {'path': 'out/summary.txt', 'content': 'two'}, 'w1'),
                    final('All done.\nFINAL: written')])
    evidence = eb.run_baseline(app, task(), root, LIMITS, answers={})
    assert evidence.state == 'completed' and evidence.claims_complete is True and evidence.error is None
    assert evidence.calls == evidence.steps == 5 and evidence.attempts == 5
    assert evidence.answer == 'All done.\nFINAL: written'
    assert [t['name'] for t in evidence.tools] == ['list_files', 'read_file', 'search_text', 'write_file']
    assert all(t['state'] == 'completed' for t in evidence.tools)
    assert evidence.writes == [{'path': 'out/summary.txt', 'before': None, 'after': hashlib.sha256(b'two').hexdigest(),
                                'bytes': 3, 'step': 4, 'seq': 1, 'status': 'applied'}]
    assert (root / 'out/summary.txt').read_text() == 'two'
    seen = results(app.model)
    assert seen['l1']['files'] == ['a.txt', 'sub/b.txt'] and seen['r1']['content'] == 'hello world\n'
    assert [m['path'] for m in seen['s1']['matches']] == ['a.txt', 'sub/b.txt']
    assert evidence.usage == {'input_tokens': 50, 'output_tokens': 10, 'cached_tokens': 5, 'calls': 5, 'attempts': 5,
                              'usage_complete': True, 'cost_usd': None}
    assert evidence.effort == choose(PROFILE, None) and all(r['effort'] == evidence.effort for r in app.model.requests)
    # Corpus 18a.3: the system message is the published prompt with this run's date filled in (eb.system_prompt).
    assert app.model.requests[0]['messages'][0] == {'role': 'system', 'content': eb.system_prompt()}
    assert app.model.requests[0]['tools'] == ['list_files', 'read_file', 'write_file', 'search_text', 'ask_user']
    # The Pebble compaction hooks are detached only during the baseline's calls.
    assert (app.model.context_transform, app.model.context_observer) == ('pebble-compaction', 'pebble-observer')


@pytest.mark.parametrize('name,args', [
    ('read_file', {'path': '../outside.txt'}), ('read_file', {'path': '/etc/hosts'}), ('read_file', {'path': '~/x'}),
    ('read_file', {'path': 'linkdir/secret.txt'}), ('read_file', {'path': 'link.txt'}),
    ('read_file', {'path': 'a\\..\\..\\x'}), ('write_file', {'path': '../escape.txt', 'content': 'x'}),
    ('write_file', {'path': 'linkdir/new.txt', 'content': 'x'}), ('write_file', {'path': 'link.txt', 'content': 'x'}),
    ('write_file', {'path': 'C:/x.txt', 'content': 'x'}), ('write_file', {'path': 'big.txt', 'content': 'x' * 100_001}),
    ('write_file', {'path': '.', 'content': 'x'}), ('list_files', {'path': '..'}), ('list_files', {'path': 'linkdir'}),
    ('search_text', {'query': 'secret', 'path': '../'}), ('read_file', {'path': 'a.txt', 'offset': -1}),
    ('unknown_tool', {}),
])
def test_tools_never_leave_the_workspace_or_follow_links(tmp_path, name, args):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_text('SECRET-CANARY')
    (tmp_path / 'outside.txt').write_text('SECRET-CANARY')
    root = workspace(tmp_path, {'a.txt': 'inside'})
    os.symlink(outside, root / 'linkdir')
    os.symlink(outside / 'secret.txt', root / 'link.txt')
    before = sorted(p.name for p in tmp_path.rglob('*'))
    app = app_with([call(name, args), final('FINAL: done')])
    evidence = eb.run_baseline(app, task(), root, LIMITS, answers={})
    assert evidence.tools[0]['state'] == 'refused' and evidence.writes == []
    assert 'SECRET-CANARY' not in json.dumps(app.model.requests[-1]['messages'])
    assert sorted(p.name for p in tmp_path.rglob('*')) == before
    assert (outside / 'secret.txt').read_text() == 'SECRET-CANARY'


def test_symlinked_listing_entries_are_skipped_and_reads_are_windowed(tmp_path):
    root = workspace(tmp_path, {'long.txt': 'x' * 25_000, 'b.txt': 'b'})
    os.symlink(root / 'b.txt', root / 'alias.txt')
    app = app_with([call('list_files', {}, 'l'), call('read_file', {'path': 'long.txt'}, 'r'),
                    call('read_file', {'path': 'long.txt', 'offset': 20_000}, 'r2'), final('FINAL: ok')])
    eb.run_baseline(app, task(), root, LIMITS, answers={})
    seen = results(app.model)
    assert seen['l']['files'] == ['b.txt', 'long.txt']
    assert len(seen['r']['content']) == eb.READ_CHARS and seen['r']['next_offset'] == eb.READ_CHARS
    assert len(seen['r2']['content']) == 5_000 and seen['r2']['next_offset'] is None


def test_states_budget_and_symmetric_completion_claim(tmp_path):
    root = workspace(tmp_path, {'a.txt': 'a'})
    looping = app_with([call('read_file', {'path': 'a.txt'}, 'c%d' % i) for i in range(10)])
    exhausted = eb.run_baseline(looping, task(), root, {'max_steps': 3, 'max_seconds': 60}, answers={})
    assert exhausted.state == 'budget_exhausted' and exhausted.calls == 3 and not exhausted.claims_complete
    ended = eb.run_baseline(app_with([final('I could not decide.')]), task(), root, LIMITS, answers={})
    assert ended.state == 'ended' and ended.claims_complete is False
    done = eb.run_baseline(app_with([final('**FINAL: 42**')]), task(), root, LIMITS, answers={})
    assert done.state == 'completed' and done.claims_complete is True
    cancel = threading.Event()
    cancel.set()
    stopped = eb.run_baseline(app_with([final('FINAL: x')]), task(), root, LIMITS, answers={}, cancel=cancel)
    assert stopped.state == 'stopped' and stopped.calls == 0 and not stopped.claims_complete
    no_folder = eb.run_baseline(app_with([call('list_files', {}), final('FINAL: none')]), task(), None, LIMITS, answers={})
    assert no_folder.tools[0]['state'] == 'refused' and no_folder.state == 'completed'
    for bad in ({'max_steps': 0, 'max_seconds': 5}, {'max_steps': 2}, {'max_steps': 2.0, 'max_seconds': 5}):
        with pytest.raises(ValueError):
            eb.run_baseline(app_with([]), task(), root, bad, answers={})


@pytest.mark.parametrize('exc,code,kind', [
    (ProviderFailure('Provider HTTP 503: busy', status=503), 'provider_failure', 'provider'),
    (ProviderFailure('Could not connect to the model provider. Check its URL and network connection.'),
     'provider_failure', 'provider'),
    (ValueError('Reconnect your ChatGPT account.'), 'auth_failure', 'auth'),
    (ValueError('Continue with ChatGPT to renew this account.'), 'auth_failure', 'auth'),
    (ProviderFailure('Provider HTTP 401: expired', status=401), 'auth_failure', 'auth'),
    (ProviderFailure('Provider HTTP 400: bad request', status=400), 'model_call_failed', 'driver'),
])
def test_provider_and_auth_failures_are_classified_and_usage_marked_incomplete(tmp_path, exc, code, kind):
    evidence = eb.run_baseline(app_with([call('list_files', {}), exc]), task(), workspace(tmp_path, {'a': 'a'}),
                               LIMITS, answers={})
    assert (evidence.state, evidence.error, evidence.failure_kind) == ('failed', code, kind)
    assert evidence.claims_complete is False and evidence.calls == 1
    assert evidence.usage['usage_complete'] is False and evidence.usage['input_tokens'] is None


def test_transient_failures_retry_inference_only_and_never_a_tool(tmp_path):
    root = workspace(tmp_path, {'a.txt': 'a'})
    app = app_with([call('write_file', {'path': 'n.txt', 'content': '1'}, 'w'),
                    ProviderFailure('Provider HTTP 429: slow down', retryable=True, status=429, retry_after=0),
                    final('FINAL: ok')])
    evidence = eb.run_baseline(app, task(), root, LIMITS, answers={})
    assert evidence.state == 'completed' and evidence.calls == 2 and evidence.attempts == 3
    assert len(evidence.writes) == 1 and evidence.usage['usage_complete'] is False


def test_text_failure_kind_is_the_shared_rule():
    assert eb.text_failure_kind('Provider HTTP 502: gateway') == 'provider'
    assert eb.text_failure_kind('The run reached its time budget while reconnecting.') == 'provider'
    assert eb.text_failure_kind('ChatGPT refresh did not rotate its credential. Reconnect.') == 'auth'
    assert eb.text_failure_kind('Provider HTTP 403: forbidden') == 'auth'
    assert eb.text_failure_kind('Provider HTTP 400: invalid tool schema') is None
    assert eb.text_failure_kind('The local model could not complete a valid tool call.') is None
    assert eb.text_failure_kind(None) is None


def test_questions_follow_the_fixed_policy_by_question_id(tmp_path):
    items = [{'id': 'zone', 'question': 'Which zone?'},
             {'id': 'size', 'question': 'Which size?', 'options': [{'id': 's', 'label': 'Small'},
                                                                  {'id': 'l', 'label': 'Large'}]},
             {'id': 'other', 'question': 'Anything else?'},
             {'id': 'missing', 'question': 'Pick one', 'options': [{'id': 'y', 'label': 'Yes'}]}]
    app = app_with([call('ask_user', {'items': items}, 'q1'), call('ask_user', {'items': items[:1]}, 'q2'),
                    call('ask_user', {'items': items[:1]}, 'q3'), final('FINAL: ok')])
    evidence = eb.run_baseline(app, task(), workspace(tmp_path, {}), LIMITS,
                               answers={'zone': {'text': 'Southern'}, 'size': {'option_id': 'l'},
                                        'missing': {'option_id': 'x', 'text': 'Typed instead'}})
    seen = results(app.model)
    # An offered option is answered with its label; an option the form does not offer falls back to the text.
    assert seen['q1'] == {'status': 'answered', 'answers': {'zone': 'Southern', 'size': 'Large',
                                                            'other': 'The typed task answer.',
                                                            'missing': 'Typed instead'},
                          'origin': 'typed by the user'}
    assert seen['q2']['answers'] == {'zone': 'Southern'} and seen['q3']['status'] == 'cancelled'
    assert evidence.answered == 2 and [q['status'] for q in evidence.questions] == ['answered', 'answered', 'cancelled']
    assert evidence.inbox == [{'kind': 'question_answer', 'origin': 'human', 'status': 'delivered', 'delivered_step': 1},
                              {'kind': 'question_answer', 'origin': 'human', 'status': 'delivered', 'delivered_step': 2}]
    assert evidence.steps > evidence.inbox[0]['delivered_step']


class FakeWeb:
    def __init__(self):
        self.log = []

    def search(self, query):
        self.log.append(('search', query))
        return {'untrusted_search_results': [
            {'title': 'Primary ' + 'T' * 300, 'url': 'https://stats.example.gov/a', 'snippet': 'value 41 ' + 's' * 900,
             'published': '2026-01-02', 'internal': 'never shown'}] * 12}

    def fetch(self, url, *, max_bytes):
        self.log.append(('fetch', url, max_bytes))
        if 'missing' in url:
            raise ValueError('This fixture page does not exist.')
        return {'url': url, 'final_url': url, 'title': 'Primary', 'text': 'The value is 42. ' * 2000}


def test_web_tools_are_offered_only_with_the_fixture_and_use_pebbles_bounds_and_windows(tmp_path):
    from pebble.source_ledger import MAX_FETCH_BYTES, PAGE_MAX, window_chars
    root = workspace(tmp_path, {})
    plain = app_with([final('FINAL: x')])
    eb.run_baseline(plain, task(), root, LIMITS, answers={})
    assert 'web_search' not in plain.model.requests[0]['tools']
    web = FakeWeb()
    app = app_with([call('web_search', {'query': 'value'}, 's'), call('read_url', {'url': 'https://stats.example.gov/a'}, 'r'),
                    call('read_url', {'url': 'https://stats.example.gov/a', 'offset': 6500, 'limit': 50_000}, 'p'),
                    call('read_url', {'url': 'https://stats.example.gov/a', 'offset': 6500}, 'd'),
                    call('read_url', {'url': 'https://stats.example.gov/missing'}, 'm'), final('FINAL: 42')])
    evidence = eb.run_baseline(app, task(), root, LIMITS, answers={}, web=web)
    assert app.model.requests[0]['tools'][-2:] == ['web_search', 'read_url']
    seen = results(app.model)
    shown = seen['s']['untrusted_search_results']
    assert len(shown) == eb.WEB_RESULTS == 10 and set(shown[0]) == {'title', 'url', 'snippet', 'published'}
    assert len(shown[0]['title']) == 180 and len(shown[0]['snippet']) == 650  # WebSearch's shown bounds
    # The first read is ReadURL's window for this budget; offset pages are capped at ReadSource's limit.
    assert len(seen['r']['untrusted_page_text']) == window_chars(PROFILE['budget']) == 6500
    assert seen['r']['shown'] == [0, 6500] and seen['r']['next_offset'] == 6500
    assert seen['p']['shown'] == [6500, 6500 + PAGE_MAX] and seen['d']['shown'] == [6500, 6500 + eb.WEB_PAGE_DEFAULT]
    assert seen['m'] == {'error': 'This fixture page does not exist.'}
    assert web.log == [('search', 'value')] + [('fetch', 'https://stats.example.gov/a', MAX_FETCH_BYTES)] * 3 + [
        ('fetch', 'https://stats.example.gov/missing', MAX_FETCH_BYTES)]
    assert [t['state'] for t in evidence.tools] == ['completed'] * 4 + ['refused']
    for budget, first, page in ((4096, 1500, None), (8192, 3000, 3000), (32768, 6500, PAGE_MAX)):
        small = app_with([call('read_url', {'url': 'https://x.example/a'}, 'r'),
                          call('read_url', {'url': 'https://x.example/a', 'offset': 10, 'limit': 99_999}, 'p'),
                          final('FINAL: x')], {**PROFILE, 'budget': budget, 'context': budget})
        eb.run_baseline(small, task(), root, LIMITS, answers={}, web=FakeWeb())
        seen = results(small.model)
        assert len(seen['r']['untrusted_page_text']) == first == window_chars(budget)
        if page is None:  # ReadSource is never offered at 4K, so neither is paging
            assert 'not available' in seen['p']['error']
        else:
            assert len(seen['p']['untrusted_page_text']) == page
    # A fruitless search is an ordinary completed tool result, exactly as Pebble's WebSearch records it
    # ({'error': message}, not uncertain), never a driver error.
    class Fruitless(FakeWeb):
        def search(self, query):
            raise ValueError('No web results found. Try a more specific query.')

    app = app_with([call('web_search', {'query': 'zzzz'}, 's'), call('web_search', {'query': 7}, 'b'),
                    final('FINAL: unknown')])
    evidence = eb.run_baseline(app, task(), root, LIMITS, answers={}, web=Fruitless())
    assert results(app.model)['s'] == {'error': 'No web results found. Try a more specific query.'}
    assert [t['state'] for t in evidence.tools] == ['completed', 'completed'] and evidence.error is None
    assert evidence.state == 'completed' and evidence.claims_complete
    with pytest.raises(ValueError):
        eb.run_baseline(app_with([]), task(), root, LIMITS, answers={}, web=object())


# -- primitive parity: the declared tests through the host's own confined runner ----------------

BUGGY = 'def double(x):\n    return x + 2\n'
FIXED = 'def double(x):\n    return x * 2\n'
VISIBLE_TEST = ('import unittest\n\nfrom mathx import double\n\n\nclass DoubleTest(unittest.TestCase):\n'
                '    def test_double(self):\n        self.assertEqual(double(5), 10)\n')
SANDBOX = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()


def granted(tools=('files', 'edits', 'tests'), recipes=('python-unittest',)):
    return SimpleNamespace(id='t1', prompt='Fix double() in mathx.py, then run the tests. End with `FINAL: done`.',
                           answer='typed', tools=tools, allowed_recipes=recipes)


def passing_after_last_write(evidence, path):
    """Corpus 18a.1's arm-neutral check for the baseline: a passing test run journaled after the last write."""
    writes = [w['seq'] for w in evidence.writes if w['path'] == path]
    return bool(writes) and any(r['exit_code'] == 0 and r['seq'] > max(writes) for r in evidence.test_runs)


def test_run_tests_executes_only_the_declared_recipe_through_the_host_runner(tmp_path, monkeypatch):
    from pebble import testing
    from pebble.policy import Workspace as HostWorkspace
    from pebble.recipes import resolve
    root = workspace(tmp_path, {'mathx.py': BUGGY, 'test_mathx.py': VISIBLE_TEST})
    payloads = []

    def recorder(payload, cancel):
        payloads.append(copy.deepcopy(payload))
        assert isinstance(cancel, threading.Event)
        return {'exit_code': 0, 'outcome': 'passed', 'tests': {'passed': 1}, 'output': 'OK', 'output_truncated': False,
                'recipe': payload['recipe'], 'isolation': testing.ISOLATION, 'snapshot_digest': 'x'}

    monkeypatch.setattr(testing, 'run_tests', recorder)
    app = app_with([call('run_tests', {'command': 'rm -rf /'}, 'x1'), call('run_tests', {'recipe': 'node-test'}, 'x2'),
                    call('run_tests', {'recipe': 'python-unittest', 'argv': ['sh', '-c', 'id']}, 'x3'),
                    call('run_tests', {'recipe': 'npm-build'}, 'x4'), call('run_command', {'command': 'ls'}, 'x5'),
                    call('bash', {'script': 'ls'}, 'x6'),
                    call('write_file', {'path': 'mathx.py', 'content': FIXED}, 'w'), call('run_tests', {}, 't'),
                    final('FINAL: done')])
    evidence = eb.run_baseline(app, granted(), root, LIMITS, answers={})
    offered = app.model.requests[0]['tools']
    assert offered == ['list_files', 'read_file', 'write_file', 'search_text', 'ask_user', 'run_tests']
    assert [t['state'] for t in evidence.tools] == ['refused'] * 6 + ['completed', 'completed']
    seen = results(app.model)
    assert 'no other command' in seen['x1']['error'] and 'declared test recipes' in seen['x2']['error']
    assert 'no other command' in seen['x3']['error'] and 'declared test recipes' in seen['x4']['error']
    assert seen['x5'] == seen['x6'] == {'error': 'Unknown tool.'}
    assert seen['t'] == {'exit_code': 0, 'outcome': 'passed', 'tests': {'passed': 1}, 'output': 'OK',
                         'output_truncated': False, 'recipe': 'python-unittest', 'isolation': testing.ISOLATION}
    # Exactly one run, with the payload an approved Pebble RunTests action executes: a host-resolved plan.
    assert len(payloads) == 1
    payload, host = payloads[0], HostWorkspace(root)
    assert set(payload) == {'root', 'root_identity', 'recipe', 'plan', 'mount', 'drafts'}
    assert payload['root'] == str(host.root) and payload['root_identity'] == list(host.identity)
    assert payload['mount'] is None and payload['drafts'] == [] and payload['recipe'] == 'python-unittest'
    expected = resolve('python-unittest', host, mount=None)
    for key in ('recipe', 'kind', 'argv', 'env', 'timeout', 'output_limit', 'network', 'cwd'):
        assert payload['plan'][key] == expected[key], key
    assert payload['plan']['network'] is False and 'rm' not in json.dumps(payload['plan']['argv'])
    # One journal sequence: the run after the write is ordered after it.
    assert evidence.writes[0]['seq'] == 1 and evidence.test_runs == [
        {'seq': 2, 'step': 8, 'recipe': 'python-unittest', 'status': 'applied', 'exit_code': 0, 'outcome': 'passed',
         'tests': {'passed': 1}}]
    assert passing_after_last_write(evidence, 'mathx.py')


def test_grants_gate_the_primitives_exactly_as_for_pebble(tmp_path, monkeypatch):
    from pebble import testing
    monkeypatch.setattr(testing, 'run_tests', lambda payload, cancel: pytest.fail('no test may run'))
    root = workspace(tmp_path, {'mathx.py': BUGGY, 'test_mathx.py': VISIBLE_TEST})
    for tools, recipes, offered in ((('files',), ('python-unittest',), ['list_files', 'read_file', 'search_text', 'ask_user']),
                                    (('files', 'edits'), ('python-unittest',),
                                     ['list_files', 'read_file', 'write_file', 'search_text', 'ask_user']),
                                    (('files', 'edits', 'tests'), (), ['list_files', 'read_file', 'write_file',
                                                                        'search_text', 'ask_user'])):
        app = app_with([call('run_tests', {}, 't'), call('write_file', {'path': 'x.txt', 'content': 'x'}, 'w'),
                        final('FINAL: done')])
        evidence = eb.run_baseline(app, granted(tools, recipes), root, LIMITS, answers={})
        assert app.model.requests[0]['tools'] == offered
        assert results(app.model)['t'] == {'error': 'Unknown tool.'} and evidence.test_runs == []
        assert ('write_file' in offered) == bool(evidence.writes)
    assert (root / 'mathx.py').read_text() == BUGGY


@pytest.mark.skipif(not SANDBOX, reason='the host test runner is the macOS sandbox')
def test_baseline_passes_a_test_requiring_task_through_the_real_confined_runner(tmp_path):
    root = workspace(tmp_path, {'mathx.py': BUGGY, 'test_mathx.py': VISIBLE_TEST})
    app = app_with([call('run_tests', {}, 't1'), call('write_file', {'path': 'mathx.py', 'content': FIXED}, 'w'),
                    call('run_tests', {'recipe': 'python-unittest'}, 't2'), final('Fixed and tested.\nFINAL: done')])
    evidence = eb.run_baseline(app, granted(), root, LIMITS, answers={})
    seen = results(app.model)
    assert seen['t1']['exit_code'] != 0 and seen['t1']['outcome'] != 'passed' and 'FAILED' in seen['t1']['output']
    assert seen['t2']['exit_code'] == 0 and 'OK' in seen['t2']['output']
    assert seen['t2']['isolation'] == 'macOS sandbox · temporary project snapshot · network denied'
    assert [(r['seq'], r['exit_code'] == 0) for r in evidence.test_runs] == [(1, False), (3, True)]
    assert evidence.writes[0]['seq'] == 2 and passing_after_last_write(evidence, 'mathx.py')
    assert evidence.state == 'completed' and evidence.claims_complete


@pytest.mark.skipif(not SANDBOX, reason='the host test runner is the macOS sandbox')
def test_baseline_test_runs_keep_the_hosts_confinement(tmp_path):
    sentinel = tmp_path / 'secret.txt'
    sentinel.write_text('private')
    root = workspace(tmp_path, {'mathx.py': FIXED})
    escape = (f"import unittest, socket\nfrom pathlib import Path\nclass Isolation(unittest.TestCase):\n"
              f" def test_no_external_access(self):\n"
              f"  with self.assertRaises(PermissionError): Path({str(sentinel)!r}).read_text()\n"
              f"  with self.assertRaises(PermissionError): Path({str(root / 'mathx.py')!r}).write_text('corrupt')\n"
              f"  with self.assertRaises(OSError): socket.create_connection(('1.1.1.1', 443), timeout=1)\n")
    app = app_with([call('write_file', {'path': 'test_isolation.py', 'content': escape}, 'w'), call('run_tests', {}, 't'),
                    final('FINAL: done')])
    evidence = eb.run_baseline(app, granted(), root, LIMITS, answers={})
    seen = results(app.model)
    assert seen['t']['exit_code'] == 0, seen['t']['output']  # every escape attempt was denied inside the sandbox
    assert (root / 'mathx.py').read_text() == FIXED and evidence.test_runs[0]['status'] == 'applied'


def test_restart_boundary_restarts_once_from_the_prompt_with_the_remaining_budget(tmp_path):
    root = workspace(tmp_path, {})
    app = app_with([call('write_file', {'path': 'a.txt', 'content': 'same'}, 'w1'),
                    call('write_file', {'path': 'a.txt', 'content': 'same'}, 'w2'),
                    call('write_file', {'path': 'b.txt', 'content': 'b'}, 'w3'), final('FINAL: written')])
    events = []

    def boundary(event):
        events.append(event)
        return event['write']

    evidence = eb.run_baseline(app, task(), root, {'max_steps': 4, 'max_seconds': 60}, answers={}, restart=boundary,
                               prompt='An overriding baseline prompt. End with `FINAL: <value>`.')
    assert evidence.restarts == 1 and evidence.state == 'completed' and evidence.calls == 4
    assert [len(r['messages']) for r in app.model.requests] == [2, 2, 4, 6]  # the restart began from the prompt
    assert app.model.requests[1]['messages'][1]['content'].startswith('An overriding baseline prompt.')
    assert [(w['path'], w['step'], w['seq']) for w in evidence.writes] == [('a.txt', 1, 1), ('a.txt', 2, 2),
                                                                          ('b.txt', 3, 3)]
    assert events[0] == {'step': 1, 'tool': 'write_file', 'state': 'completed', 'write': True, 'tests': False}
    with pytest.raises(ValueError):
        eb.run_baseline(app_with([]), task(), root, LIMITS, answers={}, restart='later')


def test_history_is_truncated_naively_by_whole_exchanges(tmp_path):
    root = workspace(tmp_path, {'big.txt': 'y' * 6_000})
    small = {**PROFILE, 'budget': 1000, 'context': 1000}
    app = app_with([call('read_file', {'path': 'big.txt'}, 'r%d' % i) for i in range(6)] + [final('FINAL: ok')], small)
    eb.run_baseline(app, task(), root, LIMITS, answers={})
    last = app.model.requests[-1]['messages']
    assert [m['role'] for m in last[:2]] == ['system', 'user']
    assert len(last) < 2 + 2 * 6 and last[-1]['role'] == 'tool' and last[-1]['tool_call_id'] == 'r5'
    for index, message in enumerate(last):
        if message['role'] == 'tool':
            owner = next(m for m in reversed(last[:index]) if m['role'] == 'assistant')
            assert message['tool_call_id'] in {c['id'] for c in owner['tool_calls']}


# -- X1: the baseline's schemas and replay through the real Responses adapter --------------

CHATGPT = {'id': 'fixture', 'name': 'Fixture', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
           'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'tools': True}


class Stream:
    def __init__(self, events):
        self.lines = []
        for event in events:
            self.lines += [b'data: ' + json.dumps(event).encode() + b'\n', b'\n']

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def readline(self, limit):
        return self.lines.pop(0) if self.lines else b''


class Transport:
    """complete_responses' provider surface with fixed in-memory streams; no socket."""

    def __init__(self, responses):
        self.responses, self.requests, self.response, self.last_usage = list(responses), [], None, {}

    def key(self, profile):
        return 'fixture-token'

    def headers(self, kind, key):
        return {'Authorization': 'Bearer ' + key}

    def request(self, url, headers, body, timeout=None):
        self.requests.append(copy.deepcopy(body))
        return Stream(self.responses.pop(0))


def sse_call(name, arguments, call_id):
    item = {'type': 'function_call', 'id': 'fc_' + call_id, 'call_id': call_id, 'name': name,
            'arguments': arguments, 'status': 'completed'}
    return [{'type': 'response.created', 'response': {'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'status': 'in_progress',
                                                                                'arguments': ''}},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': [],
                                                        'usage': {'input_tokens': 40, 'output_tokens': 8}}}]


def sse_message(text):
    item = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
    return [{'type': 'response.created', 'response': {'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'content': [],
                                                                                'status': 'in_progress'}},
            {'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'msg_1', 'delta': text},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': [],
                                                        'usage': {'input_tokens': 60, 'output_tokens': 4}}}]


def test_x1_baseline_schemas_and_replay_pass_through_the_real_responses_adapter(tmp_path):
    transport = Transport([sse_call('list_files', '{"path": "."}', 'call_1'), sse_message('Listed.\nFINAL: done')])
    transport.project_context = lambda messages, tools, budget, counter=None, method=None: messages

    class Adapter:
        context_transform = context_observer = None
        last_usage = {}

        def current(self):
            return CHATGPT

        def complete(self, messages, tools, cancel, token, effort):
            result = complete_responses(transport, CHATGPT, messages, tools, cancel, token, effort)
            self.last_usage = transport.last_usage
            return result

        def cancel(self):
            pass

    root = workspace(tmp_path, {'notes.md': 'n'})
    evidence = eb.run_baseline(SimpleNamespace(model=Adapter()), task(), root, LIMITS, answers={})
    assert evidence.state == 'completed' and evidence.calls == 2 and evidence.tools[0]['state'] == 'completed'
    first, second = transport.requests
    assert [t['name'] for t in first['tools']] == [t['function']['name'] for t in eb.TOOLS]
    assert all(t['type'] == 'function' and t['strict'] is False and t['parameters']['type'] == 'object'
               for t in first['tools'])
    # Corpus 18a.3: the published prompt with this run's date filled in (eb.system_prompt).
    assert first['input'][:2] == [{'role': 'system', 'content': eb.system_prompt()},
                                  {'role': 'user', 'content': task().prompt}]
    replay = [i for i in second['input'] if i.get('type') in {'function_call', 'function_call_output'}]
    assert [i['type'] for i in replay] == ['function_call', 'function_call_output']
    assert replay[0]['call_id'] == replay[1]['call_id'] == 'call_1'
    assert json.loads(replay[1]['output'])['files'] == ['notes.md']
    assert evidence.effort == choose(CHATGPT, None) and evidence.usage['input_tokens'] == 100
    # The stream reported no cached-token detail: that field alone is unknown, so usage is incomplete.
    assert evidence.usage['cached_tokens'] is None and evidence.usage['usage_complete'] is False
