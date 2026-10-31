#!/usr/bin/env python3
"""Phase 34 service validator: 1.2.0, the beat plan's precision release (W1-W4) and the Phase 33 compaction race.

The plan to match or beat Claude Code and Codex (.runtime/beat-plan/plan.md, D54) traced Pebble's lost benchmark cells
to causes: it verified by writing tests into the project (W1), applied a style limit to the scaffolding another part
of the request needed (W2), put a required last line in a file or added text to a fixed shape (W3), and wrote the
path of a page it had set aside while citing (W4). These checks drive the checked-in App, broker, recipes, sandboxed
test runner and run journal in process (only the root model is scripted), the same in source and frozen modes:
  - a check runs in the isolated copy only: the host names the file, the project does not change, and the run is kind
    'check', never evidence for the completion gate, AgentGuard or memory lessons; other runners are refused;
  - a test file the request did not ask to change waits for the person in Auto mode, with the host's reason, while a
    request for test changes and Full access apply as before;
  - the writing rules reach the model above 8K, and the test rule rides with the completion rule;
  - the fixes are general: no task id, fixture file or grader check of corpus 24a appears in their text;
  - a transport retry after compaction sends the committed generation (the race of Phase 33 trial 1, reproduced with
    the fix switched off);
  - the release version is 1.2.0 and its notes carry all of this.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import copy
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_runtime import ROOT  # noqa: E402
from validate_phase13 import bounded, owned_backend, sha  # noqa: E402
from validate_phase20 import write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402

for _path in (ROOT / 'backend', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'a_check_runs_in_the_isolated_copy_and_is_never_evidence',
    'an_unrequested_test_change_waits_for_the_person',
    'the_writing_rules_reach_the_model_above_8k',
    'the_fixes_are_general',
    'a_transport_retry_sends_the_committed_compaction',
    'release_version_is_1_2_0_and_its_notes_carry_the_precision_release',
    'backend_inputs_stable_during_campaign',
)
READS = ('backend/pebble/testing.py', 'backend/pebble/edit_scope.py', 'backend/pebble/app.py', 'backend/pebble/runner.py',
         'backend/pebble/tool_catalogue.py', 'backend/pebble/memory_facts.py', 'backend/pebble/controller.py',
         'backend/pebble/runtime.py', 'backend/pebble/compaction.py', 'backend/pebble/recovery.py', 'src/App.vue',
         'benchmarks/corpus_24a/tasks.py', 'benchmarks/corpus_24a/graders.py', 'CHANGELOG.md',
         'scripts/validate_phase34.py', 'backend/pebble/__init__.py', 'package.json', 'package-lock.json',
         'src-tauri/Cargo.toml', 'src-tauri/tauri.conf.json', 'src-tauri/Cargo.lock')
SOURCE = 'def mean(values):\n    return sum(values) / len(values)\n'
FIXED = 'def mean(values):\n    return sum(values) / len(values) if values else 0.0\n'
TESTS = ('import unittest\n\nfrom stats import mean\n\n\n'
         'class MeanTest(unittest.TestCase):\n    def test_mean(self):\n        self.assertEqual(mean([1, 3]), 2)\n')
CHECK = ('import unittest\n\nfrom stats import mean\n\n\n'
         'class EmptyCheck(unittest.TestCase):\n    def test_empty(self):\n        self.assertEqual(mean([]), 0.0)\n')


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def flat(text):
    return re.sub(r'\s+', ' ', text)


def changelog_entry(version):
    text = read('CHANGELOG.md')
    match = re.search(r'^## \[' + re.escape(version) + r'\][^\n]*\n(.*?)(?=^## \[|\Z)', text, re.M | re.S)
    assert match, 'CHANGELOG.md has no entry for ' + version
    return flat(match.group(1))


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


def project(folder):
    folder.mkdir(parents=True)
    (folder / 'stats.py').write_text(SOURCE)
    (folder / 'test_stats.py').write_text(TESTS)
    return folder


def contents(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob('*')) if p.is_file()}


class Scripted:
    """The root model only (Providers.complete), scripted per run; restored on exit."""

    def __init__(self):
        from pebble.providers import Providers
        self.providers, self.requests, self.script = Providers, [], []

    def __enter__(self):
        self.real = self.providers.complete

        def complete(provider, messages, tools, cancel, token, effort=False):
            self.requests.append(copy.deepcopy(messages))
            assert len(self.requests) <= len(self.script), 'Unscripted root model request.'
            response = copy.deepcopy(self.script[len(self.requests) - 1])
            if response.get('content'):
                token(response['content'])
            return response
        self.providers.complete = complete
        return self

    def __exit__(self, *exc):
        self.providers.complete = self.real


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


def app_for(home, mode, context=32768):
    from pebble.app import App
    from pebble.permissions import DEFAULT
    app = App(home / 'data', home / 'resources', lambda *a: None)
    app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': mode})
    app.model_save(name='p34 validator', url='http://127.0.0.1:9', model='p34-validator-%d' % context, context=context)
    return app


def settle(app, cid, scripted, requests, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = app.runs.list(cid)
        if runs and app.active is None and len(scripted.requests) == requests and runs[0]['state'] not in (
                'queued', 'running', 'retry_wait'):
            return app.runs.get(runs[0]['id'])
        time.sleep(.02)
    raise AssertionError(f'The run did not settle after {requests} requests ({len(scripted.requests)} made).')


def tool_result(scripted, request, call_id):
    message = next(m for m in scripted.requests[request] if m.get('role') == 'tool' and m.get('tool_call_id') == call_id)
    return json.loads(message['content'])


def close(app):
    if not app.closed:
        app.active = app.active_run = None
        app.close()


class Campaign:
    def __init__(self, scratch, backend=None, only=None):
        self.scratch, self.backend, self.results, self.only = scratch, backend, [], only

    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
        if self.only is not None and name not in self.only:
            return
        began = time.monotonic()
        try:
            evidence = fn() or {}
            self.results.append({'name': name, 'passed': True, 'evidence': evidence,
                                 'seconds': round(time.monotonic() - began, 2)})
        except Exception as exc:
            self.results.append({'name': name, 'passed': False, 'error': bounded(exc, 1500),
                                 'trace': traceback.format_exc(limit=3)[-1200:].replace(str(ROOT), '<checkout>')})

    def run(self):
        scratch = self.scratch

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        def check_run():
            from pebble import memory_facts, testing
            from pebble.controller import RunController
            from pebble.policy import PolicyError, Workspace
            folder = project(scratch / 'check' / 'project')
            before = contents(folder)
            payload = {'root': str(folder), 'recipe': 'python-unittest', 'drafts': [],
                       'check': testing.check_payload('python-unittest', CHECK)}
            assert payload['check']['path'] == 'test_pebble_check.py'
            result = testing.run_tests(payload, threading.Event())
            assert result['kind'] == 'check' and result['exit_code'] != 0, (result['kind'], result['exit_code'])
            assert 'ZeroDivisionError' in result['output'] and 'test_empty' in result['output'], 'the check did not run'
            assert contents(folder) == before, 'the project changed'
            assert result['snapshot_digest'] == testing.snapshot_digest(Workspace(folder)), 'the check is in the digest'
            passed = testing.run_tests({**payload, 'drafts': [Workspace(folder).draft('stats.py', FIXED)]},
                                       threading.Event())
            assert passed['outcome'] == 'passed' and passed['kind'] == 'check', passed['output']
            ws = Workspace(folder)
            applied = lambda result: {'kind': 'tests', 'status': 'applied', 'created': 1.0,  # noqa: E731
                                      'payload': {'root': str(ws.root), 'root_identity': list(ws.identity),
                                                  'result': result}}
            # The same passing result on the project's current digest: only its kind decides.
            current = {**passed, 'snapshot_digest': testing.snapshot_digest(ws)}
            assert RunController._current_test_evidence([applied(current)], ws) is False, 'a check verified the project'
            assert RunController._current_test_evidence([applied({**current, 'kind': 'test'})], ws) is True
            receipt = {'state': 'completed', 'name': 'RunTests', 'arguments': {}, 'result': {'action_id': 'a'}}
            lesson = {'id': 'a', 'kind': 'tests', 'status': 'applied', 'payload': {'result': result}}
            assert memory_facts.failure_signatures(receipt, lesson) == [], 'a failing check became a lesson'
            assert memory_facts.failure_observations('completed', [receipt], [lesson]) == []
            for recipe in ('python-pytest', 'npm-test', 'go-test', 'cargo-test', 'make-test'):
                try:
                    testing.check_payload(recipe, CHECK)
                    raise AssertionError('a check was accepted for ' + recipe)
                except PolicyError:
                    pass
            (folder / 'test_pebble_check.py').write_text('# the project owns this name\n')
            try:
                testing.run_tests(payload, threading.Event())
                raise AssertionError('a project file with the check name was replaced')
            except PolicyError as exc:
                assert 'already has test_pebble_check.py' in str(exc), str(exc)
            assert (folder / 'test_pebble_check.py').read_text() == '# the project owns this name\n'
            # Through the App: the reviewed payload carries the text, and only the project's own run reaches AgentGuard.
            app_folder = project(scratch / 'check-app' / 'project')
            app = app_for(scratch / 'check-app', 'auto')
            seen = []
            real = app.guard.test_result
            app.guard.test_result = lambda root, cid, recipe, result: (seen.append(result.get('kind')),
                                                                       real(root, cid, recipe, result))[1]
            try:
                with Scripted() as scripted:
                    scripted.script = [call('RunTests', {'recipe': 'python-unittest', 'check': CHECK}, 'check'),
                                       call('RunTests', {'recipe': 'python-unittest'}, 'tests'),
                                       final('mean([]) raises ZeroDivisionError; the project tests pass.')]
                    chat = app.new_chat(str(app_folder))
                    app.work(chat, 'Does mean handle an empty list?', False, [])
                    settle(app, chat['id'], scripted, 3)
                    runs = [a for a in app.store.actions(chat['id']) if a['kind'] == 'tests']
                    assert [a['status'] for a in runs] == ['applied', 'applied'], [a['status'] for a in runs]
                    assert runs[0]['payload']['check'] == testing.check_payload('python-unittest', CHECK)
                    assert [r['payload']['result']['kind'] for r in runs] == ['check', 'test']
                    assert seen == ['test'], seen
                    assert 'does not count' in tool_result(scripted, 1, 'check')['result']['check']['note']
            finally:
                close(app)
            vue = read('src/App.vue')
            for needle in ('function isCheck(', '"Run an ad hoc check"', 'View the check',
                           "Runs Pebble's own check, added to a temporary copy for this run only."):
                assert needle in vue, ('App.vue lacks', needle)
            return {'check_file': 'test_pebble_check.py', 'project_unchanged': True, 'evidence': False,
                    'agentguard_results': seen, 'refused_recipes': 5}
        self.check('a_check_runs_in_the_isolated_copy_and_is_never_evidence', check_run)

        def scope():
            from pebble import edit_scope
            table = {
                ('test_parser.py', 'Make parse accept tuples.'): True,
                ('test_parser.py', 'Fix the bug so the tests pass.'): True,
                ('tests/test_parser.py', "Fix it, but don't change the tests."): True,
                ('test_parser.py', 'Add a test for the empty case.'): False,
                ('test_parser.py', 'Update the unit tests for the new API.'): False,
                ('test_parser.py', 'Add a case to test_parser.py for tuples.'): False,
                ('parser.py', 'Make parse accept tuples.'): False,
            }
            got = {key: edit_scope.review_reason(*key) is not None for key in table}
            assert got == table, {k: v for k, v in got.items() if table[k] != v}
            outcomes = {}
            for mode, text, waits in (('auto', 'Make mean return 0.0 for an empty list.', True),
                                      ('ask', 'Make mean return 0.0 for an empty list.', True),
                                      ('auto', 'Make mean return 0.0 for an empty list, and add a test for it.', False),
                                      ('full', 'Make mean return 0.0 for an empty list.', False)):
                home = scratch / f'scope-{len(outcomes)}'
                folder = project(home / 'project')
                app = app_for(home, mode)
                try:
                    with Scripted() as scripted:
                        scripted.script = [call('Write', {'file_path': 'test_extra.py', 'content': CHECK}, 'extra'),
                                           final('Done.')]
                        chat = app.new_chat(str(folder))
                        app.work(chat, text, False, [])
                        settle(app, chat['id'], scripted, 2)
                        action = next(a for a in app.store.actions(chat['id']) if a['kind'] == 'edit')
                        told = tool_result(scripted, 1, 'extra')
                finally:
                    close(app)
                held = action['payload'].get('scope_review') == edit_scope.REASON
                assert held is waits, (mode, text, action['status'], action['payload'].get('scope_review'))
                if waits:
                    assert action['status'] == 'pending' and told.get('scope_review') == edit_scope.MODEL_NOTE, told
                    assert not (folder / 'test_extra.py').exists()
                elif mode != 'ask':
                    assert action['status'] == 'applied' and (folder / 'test_extra.py').read_text() == CHECK
                outcomes[f'{mode}: {text}'] = action['status']
            assert "typeof p.scope_review === \"string\"" in read('src/App.vue'), 'the card does not say why'
            return {'intent_cases': len(table), 'runs': outcomes}
        self.check('an_unrequested_test_change_waits_for_the_person', scope)

        def rules():
            from pebble import runner
            systems = {}
            for context in (32768, 4096):
                home = scratch / f'rules-{context}'
                folder = project(home / 'project')
                app = app_for(home, 'auto', context)
                try:
                    with Scripted() as scripted:
                        scripted.script = [final('mean divides the sum by the count.')]
                        chat = app.new_chat(str(folder))
                        app.work(chat, 'Explain mean in one sentence.', False, [])
                        settle(app, chat['id'], scripted, 1)
                        systems[context] = scripted.requests[0][0]['content']
                finally:
                    close(app)
            large, small = systems[32768], systems[4096]
            assert runner.PRECISION in large and runner.PRECISION not in small, 'the writing rules are misplaced'
            completion = 'run them with RunTests before your final answer.'
            assert large.index(completion) + len(completion) == large.index(runner.TEST_SCOPE), 'test rule not with it'
            # Phase 46 (1.9.27, restated with its reason): W4 now keeps a set-aside source's figures out too ("without its
            # path, link or figures") and says "quote no figure" for an unknown answer (private d-rf-92 on Opus quoted a
            # rumour's figure beside the right answer; runner.PRECISION, shorter than before)
            for needle in ('satisfies both', 'exactly that shape', 'ends your reply, not a file', 'cite only that source',
                           'without its path, link or figures', 'quote no figure'):
                assert needle in runner.PRECISION, ('PRECISION lacks', needle)
            from pebble.tool_catalogue import TOOLS
            spec = next(t['function'] for t in TOOLS if t['function']['name'] == 'RunTests')
            assert spec['parameters']['properties']['check'] == {'type': 'string', 'maxLength': 16000}
            assert spec['parameters']['required'] == ['recipe'], 'check must stay optional'
            return {'precision_chars': len(runner.PRECISION), 'test_scope_chars': len(runner.TEST_SCOPE)}
        self.check('the_writing_rules_reach_the_model_above_8k', rules)

        def general():
            from benchmarks.corpus_24a import graders, tasks
            from pebble import edit_scope, runner, testing
            from pebble.tool_catalogue import TOOLS
            spec = next(t['function'] for t in TOOLS if t['function']['name'] == 'RunTests')
            texts = {'runner.PRECISION': runner.PRECISION, 'runner.TEST_SCOPE': runner.TEST_SCOPE,
                     'edit_scope.py': read('backend/pebble/edit_scope.py'), 'testing.CHECK_NOTE': testing.CHECK_NOTE,
                     'RunTests description': spec['description'],
                     'CHANGELOG 1.2.0': changelog_entry('1.2.0')}
            ids = sorted({t.id for t in tasks.TASKS})
            files = sorted({name for t in tasks.TASKS for name, _ in t.files})
            terms = sorted({c for c in re.findall(r"checks\['([a-z0-9_]+)'\]", read('benchmarks/corpus_24a/graders.py'))
                            if '_' in c} | {f for f in graders.FLAGS} | {'FINAL:'})
            found = [(where, word) for where, text in texts.items() for word in (*ids, *files, *terms) if word in text]
            assert not found, found
            assert len(ids) >= 45 and len(files) >= 50 and len(terms) >= 10, (len(ids), len(files), len(terms))
            return {'texts': sorted(texts), 'task_ids': len(ids), 'fixture_files': len(files), 'grader_terms': len(terms)}
        self.check('the_fixes_are_general', general)

        def retry():
            from pebble.app import App
            from pebble.compaction import is_spine
            count = lambda messages, tools: len(json.dumps([messages, tools], ensure_ascii=False)) // 4 + 1  # noqa: E731
            window = lambda: [  # noqa: E731
                {'role': 'system', 'content': 'host policy'},
                {'role': 'user', '_pebble_checkpoint': True, 'content': 'host facts'},
                {'role': 'user', '_pebble_current': True, 'content': 'Keep 漢字. Never publish.'},
                {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'old', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{"file_path":"a.txt"}'}}]},
                {'role': 'tool', 'tool_call_id': 'old', 'content': 'OLD RAW SOURCE ' * 450},
                {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'new', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{"file_path":"b.txt"}'}}]},
                {'role': 'tool', 'tool_call_id': 'new', 'content': 'latest evidence'}]
            outcomes = {}
            for fixed in (False, True):
                app = App(scratch / f'retry-{fixed}' / 'data', scratch / f'retry-{fixed}' / 'resources', lambda *a: None)
                chat = app.new_chat()
                rid = app.runs.create(chat['id'], 'Keep 漢字. Never publish.', app.runtime_config(chat, False))
                app.runs.transition(rid, 'running')
                execution, token = app.controller.enter(app.runs.get(rid))
                app.active, app.active_run = chat['id'], rid
                readings = [120.4, 118.9]
                app.time_ledger.remaining = lambda run_id: readings.pop(0) if len(readings) > 1 else readings[0]
                reuse = App._committed_compaction
                try:
                    if not fixed:
                        App._committed_compaction = lambda self, *args: None
                    app.runs.checkpoint(rid, {'phase': 'model', 'messages': window(), 'available': []}, steps=2, elapsed=0)
                    first = app.model.project_context(window(), [], 900, counter=count, method='fixture')
                    assert execution.compaction_generation == 1 and any(is_spine(m) for m in execution.projected_messages)
                    try:
                        again = app.model.project_context(window(), [], 900, counter=count, method='fixture')
                        outcomes[fixed] = 'same wire' if again == first else 'different wire'
                    except ValueError as exc:
                        outcomes[fixed] = str(exc)
                    generations = len(app.runs.compactions(rid))
                finally:
                    App._committed_compaction = reuse
                    app.active = app.active_run = None
                    app.controller.leave(execution, token)
                    app.close()
                assert generations == 1, generations
            assert 'The model checkpoint changed during compaction' in outcomes[False], outcomes
            assert outcomes[True] == 'same wire', outcomes
            return {'without_the_fix': outcomes[False][:80], 'with_the_fix': outcomes[True], 'generations': 1}
        self.check('a_transport_retry_sends_the_committed_compaction', retry)

        def version():
            current = checkout_version()
            # D65 (Phase 35): the release that ships the precision work is 1.2.0 and this check reads its entry; a later
            # checkout keeps that entry and every assertion on it (as D62 did for validate_phase33).
            assert tuple(int(x) for x in current.split('.')) >= (1, 2, 0), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = changelog_entry('1.2.0')
            for needle in ('RunTests takes a check', 'test_pebble_check.py', 'waits for your approval, in Auto mode too',
                           'Writing rules', 'cite only the source relied on',
                           'The model checkpoint changed during compaction', 'is not claimed here'):
                assert needle in entry, ('the 1.2.0 notes lack', needle)
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_1_2_0_and_its_notes_carry_the_precision_release', version)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary folder even on success')
    args = parser.parse_args(argv)
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; choose a new path (earlier raw evidence is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    backend_sha = sha(backend.read_bytes()) if backend else None
    before = digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase34-', dir='/private/tmp')).resolve()
    started = time.monotonic()
    campaign = Campaign(scratch, backend)
    fatal = None
    try:
        campaign.run()
    except Exception as exc:
        fatal = bounded(exc, 1500)
    after = digests()
    stable = before == after and (not backend or sha(backend.read_bytes()) == backend_sha)
    campaign.results.append({'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                             'evidence': {'files_compared': len(before), 'executable_compared': bool(backend)}})
    recorded = {r['name'] for r in campaign.results}
    failed = [r['name'] for r in campaign.results if not r['passed']] + [n for n in PLANNED if n not in recorded]
    passed = fatal is None and not failed and recorded == set(PLANNED)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source', 'passed': passed,
        'checks': [r['name'] for r in campaign.results if r['passed']], 'failed': failed, 'planned_checks': list(PLANNED),
        'check_results': campaign.results, 'fatal_error': fatal,
        'duration_seconds': round(time.monotonic() - started, 1),
        'backend_sha256': backend_sha, 'source_sha256': before,
        'source_note': 'Digests of the tested sources this validator reads; the campaign binds them to its snapshot.',
    }
    if passed and not args.keep_scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    else:
        report['scratch_preserved'] = str(scratch)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_new(report_path, json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']), 'failed': failed,
                      'fatal_error': fatal}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
