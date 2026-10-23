#!/usr/bin/env python3
"""Phase 28 service validator: multi-step Claude tasks survive Anthropic's thinking binding; the benchmark harness fixes.

Phase 27 proved the 1.0.2 candidate (reports/harness-phase27-review.json). The Claude benchmark pilot then found
(.runtime/decisions-after-phase27.json D29, D32, D33):
  - a product failure: Anthropic binds each thinking block to everything sent before it and refuses a changed prefix
    (by default for accounts created since 2026-08-31); Pebble rebuilds the first user message each step, so a task's
    third request failed with "Invalid `signature` in `thinking` block". Requests to api.anthropic.com that carry a
    thinking configuration now set `block_binding.prefix_mismatch_behavior: "drop_block"` with the
    `thinking-binding-controls-2026-08-01` beta header;
  - three harness defects: the question detector missed a reply that asked with "Please send: …" and ended with a FINAL
    line, so the scripted follow-up was never sent (D29); the key prompt fell back to plain, echoing input without a
    terminal; and a relative evaluation home nested inside itself, which stopped the key entry after its profile was
    saved (D32: an absolute home, and --finish to record a saved profile without asking for the key).
Checks, the same in source and frozen modes:
  - Anthropic requests with thinking ask the API to drop a stale block: a three-request tool loop built by the
    backend's provider code (stub transport) carries the binding and the beta header on every request, while the
    first message still changes per step; in frozen mode the archived pebble.providers also carries both;
  - gateways and requests without thinking are unchanged: an owned service (the frozen executable in frozen mode)
    talking to a loopback Anthropic gateway on a thinking model sends neither, and a model without thinking on
    api.anthropic.com sends no beta;
  - a reply that asks gets its follow-up: the detector's shapes, and a scripted Claude Code cell that asks with a
    request and a FINAL line is graded on what it wrote after the follow-up;
  - the key is read only on a terminal, and the evaluation home is absolute;
  - an interrupted key entry is finished without the key;
  - the release version is after 1.0.1 and every declaration agrees.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import contextlib
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_runtime import ROOT  # noqa: E402
from validate_phase13 import bounded, owned_backend, sha  # noqa: E402
from validate_phase20 import Service, backend_path, write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402
from validate_phase27 import AnthropicFixture, TRANSIENT, frozen_module, sse  # noqa: E402

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'anthropic_thinking_requests_ask_to_drop_a_stale_block',
    'gateways_and_requests_without_thinking_are_unchanged',
    'a_reply_that_asks_gets_its_follow_up',
    'the_key_is_read_only_on_a_terminal_and_the_home_is_absolute',
    'an_interrupted_key_entry_is_finished_without_the_key',
    'release_version_is_after_1_0_1',
    'backend_inputs_stable_during_campaign',
)
# The tested sources this validator reads; each is in verify_harness_candidate.sources(), so the campaign binds them.
READS = ('backend/pebble/providers.py', 'benchmarks/drivers/codex_cell.py', 'benchmarks/drivers/claude_cell.py',
         'scripts/bench_claude.py', 'scripts/validate_phase27.py', 'scripts/validate_phase28.py',
         'backend/pebble/__init__.py', 'package.json', 'package-lock.json', 'src-tauri/tauri.conf.json',
         'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock')
MODEL = 'claude-opus-5-5'
BETA = 'thinking-binding-controls-2026-08-01'
BINDING = {'prefix_mismatch_behavior': 'drop_block'}
LIMITS = {'max_steps': 4, 'max_seconds': 60}
FIXTURE_KEY = 'sk-ant-' + 'phase28fixture' * 3  # built at run time; never a literal key-shaped string


def thinking_then_tool(name):
    return [{'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'SIG'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'tu_1', 'name': name, 'input': {}}},
            {'type': 'content_block_delta', 'index': 1,
             'delta': {'type': 'input_json_delta', 'partial_json': json.dumps({'file_path': 'calc.py'})}},
            {'type': 'content_block_stop', 'index': 1},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 5}},
            {'type': 'message_stop'}]


FINAL = [{'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
         {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
         {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'add returns a + b.'}},
         {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 3}},
         {'type': 'message_stop'}]


def load_script(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bench():
    backend_path()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from benchmarks import corpus_24a, plan_24a
    from benchmarks.drivers import claude_cell, codex_cell
    return corpus_24a, plan_24a, claude_cell, codex_cell


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


class Campaign:
    def __init__(self, scratch, backend=None):
        self.scratch, self.backend, self.results = scratch, backend, []

    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
        began = time.monotonic()
        try:
            evidence = fn() or {}
            self.results.append({'name': name, 'passed': True, 'evidence': evidence,
                                 'seconds': round(time.monotonic() - began, 2)})
        except Exception as exc:
            self.results.append({'name': name, 'passed': False, 'error': bounded(exc, 1500),
                                 'trace': traceback.format_exc(limit=3)[-1200:].replace(str(ROOT), '<checkout>')})

    def command(self):
        return [str(self.backend)] if self.backend else [sys.executable, str(ROOT / 'backend/main.py')]

    def in_process(self, url, model, tool_turns):
        """Requests the backend's provider code builds for one task, through a stub transport (no connection)."""
        backend_path()
        from pebble.app import App
        from pebble.providers import Providers
        sent = []

        @contextlib.contextmanager
        def request(provider, target, headers, body=None, timeout=30):
            sent.append((dict(headers), copy.deepcopy(body)))
            tools = [t['name'] for t in body.get('tools') or []]
            events = thinking_then_tool('Read' if 'Read' in tools else tools[0]) if len(sent) <= tool_turns else FINAL
            yield sse(events)

        saved = Providers.request, Providers.key
        Providers.request, Providers.key = request, lambda provider, p: 'phase28-fixture-key'
        home = Path(tempfile.mkdtemp(prefix='in-process-', dir=self.scratch))
        project = home / 'project'
        project.mkdir()
        (project / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
        app = App(home / 'data', home / 'resources', lambda *args: None)
        try:
            app.store.set_setting('model_profiles', [{
                'id': 'p28', 'kind': 'anthropic', 'name': 'Phase 28 Anthropic', 'url': url, 'model': model,
                'context': 200000, 'budget': 200000, 'budget_version': 2, 'context_mode': 'full', 'tools': True}])
            app.store.set_setting('active_profile', 'p28')
            chat = app.new_chat(str(project))
            app.work(chat, 'Read calc.py and say what add does.', False, [])
        finally:
            app.close()
            Providers.request, Providers.key = saved
        return sent

    def run(self):
        scratch = self.scratch

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        def binding():
            sent = self.in_process('https://api.anthropic.com/v1', MODEL, tool_turns=2)
            assert len(sent) >= 3, ('a three-request tool loop', len(sent))
            first = sent[0][1]['messages'][0]
            assert any(body['messages'][0] != first for _, body in sent[1:]), 'the first message no longer changes'
            for headers, body in sent:
                assert body['thinking'] == {'type': 'adaptive', 'block_binding': BINDING}, body.get('thinking')
                assert headers.get('anthropic-beta') == BETA, headers.get('anthropic-beta')
                assert body.get('cache_control') == {'type': 'ephemeral'}
            assert sent[1][1]['messages'][1]['content'][0] == {'type': 'thinking', 'thinking': '', 'signature': 'SIG'}, \
                'the earlier assistant turn is not echoed as received'
            evidence = {'requests': len(sent), 'binding': BINDING, 'beta': BETA, 'first_message_changes': True}
            if self.backend is not None:
                raw = frozen_module(self.backend, 'pebble.providers')
                for needle in (b'block_binding', b'prefix_mismatch_behavior', b'drop_block', BETA.encode()):
                    assert needle in raw, ('the frozen providers module lacks', needle.decode())
                evidence['frozen_providers_module'] = {'bytes': len(raw), 'carries': ['block_binding', 'drop_block', BETA]}
            return evidence
        self.check('anthropic_thinking_requests_ask_to_drop_a_stale_block', binding)

        def unchanged():
            fixture = AnthropicFixture()
            service = Service(self.command(), scratch / 'service-home', scratch / 'service', 'phase28-gateway')
            try:
                profile = service.rpc('model_save', name='Phase 28 gateway fixture', url=fixture.url, model=MODEL,
                                      kind='anthropic', context=200000)['profile_id']
                service.rpc('model_select', profile_id=profile)
                cid = service.rpc('new_chat')['id']
                rid = service.rpc('send', id=cid, text='Say hello.', source_ids=[], limits=dict(LIMITS))['run_id']
                deadline = time.monotonic() + 60
                while True:
                    run = service.rpc('run_get', id=cid, run_id=rid)
                    if run['state'] not in TRANSIENT and not service.rpc('state')['active']:
                        break
                    assert time.monotonic() < deadline, ('the run did not settle', run['state'])
                    time.sleep(0.1)
            finally:
                service.close()
                fixture.close()
            assert run['state'] == 'completed', ('the run settled as', run['state'], str(run.get('error'))[:300])
            with fixture.lock:
                messages = [dict(r) for r in fixture.records if r['path'].endswith('/messages')]
            assert messages and all(isinstance(r['body'].get('thinking'), dict) for r in messages), \
                'the gateway fixture saw no thinking request'
            assert all('block_binding' not in json.dumps(r['body']) and 'anthropic-beta' not in r['headers']
                       for r in messages), 'a gateway got the binding or the beta'
            plain = self.in_process('https://api.anthropic.com/v1', 'fixture-chat', tool_turns=0)
            assert plain and all('thinking' not in body and 'anthropic-beta' not in headers for headers, body in plain)
            return {'service': 'frozen' if self.backend else 'source', 'gateway_requests': len(messages),
                    'plain_requests': len(plain)}
        self.check('gateways_and_requests_without_thinking_are_unchanged', unchanged)

        corpus, bp, cc, cx = bench()

        def follow_up():
            asks = ["The details are missing, so nothing is written yet. Please send:\n1. Room\n2. Date\n3. Time\n"
                    "4. People\n\nFINAL: waiting on the details", 'Which room, date, time and how many people?',
                    'Could you tell me which report you mean?\nFINAL: needs your answer']
            not_asks = ['Done?\nFINAL: done', 'Booked Birch.\nLet me know if you want changes.\nFINAL: done',
                        'Booked Birch. Let me know if you want changes.', 'FINAL: Done.\nWant an invite too?']
            assert all(cx.asks_question(r) for r in asks), [r for r in asks if not cx.asks_question(r)]
            assert not any(cx.asks_question(r) for r in not_asks), [r for r in not_asks if cx.asks_question(r)]
            from benchmarks.corpus_24a.references import REFERENCES
            task = corpus.BY_ID['h11-booking']
            ref = REFERENCES[task.id]
            calls = []

            class Scripted:
                def __init__(self, argv, cwd, env):
                    calls.append(argv)
                    turn = len(calls)
                    writes = ref.get('turn_writes') or [ref.get('writes') or {}]
                    for path, text in (writes[turn - 1] if turn - 1 < len(writes) else {}).items():
                        (Path(cwd) / path).write_text(text)
                    reply = asks[0] if turn == 1 else ref['replies'][turn - 1]
                    init = {'type': 'system', 'subtype': 'init', 'model': MODEL, 'mcp_servers': [], 'plugins': [],
                            'skills': [], 'agents': [], 'slash_commands': [], 'tools': ['Bash', 'Write'],
                            'memory_paths': {'auto': '/private/tmp/x/'}, 'session_id': 's'}
                    self.events = [json.dumps(init), json.dumps({'type': 'result', 'subtype': 'success', 'result': reply,
                                                                 'total_cost_usd': 0.01, 'usage': {'input_tokens': 5,
                                                                                                   'output_tokens': 1}})]
                    self.child = type('Child', (), {'returncode': 0, 'stderr': None, 'poll': lambda self: 0})()

                def lines(self, deadline):
                    return self.events, 0.1

                def stop(self):
                    pass

            # Cell roots live under /private/tmp (benchmarks/drivers/workspace.py); this one is removed afterwards.
            root = Path(tempfile.mkdtemp(prefix='pebble-bench-p28-', dir='/private/tmp'))
            planned = {'position': 1, 'rep': 1, 'phrasing': 'canonical', 'product': 'claude_code'}
            from benchmarks.corpus_24a import graders as gr
            try:
                out, metrics = cc.run_cell(task, planned, root, '/opt/claude', scratch, MODEL, wall=60, spawn=Scripted)
            finally:
                shutil.rmtree(root, ignore_errors=True)
            verdict = gr.grade(task, out)
            outcome = gr.classify(task, out, verdict)
            assert len(calls) == 2 and out.questions == 1 and outcome == 'passed', (len(calls), out.questions, outcome)
            return {'asking_shapes': len(asks), 'non_asking_shapes': len(not_asks), 'turns': len(calls),
                    'outcome': outcome}
        self.check('a_reply_that_asks_gets_its_follow_up', follow_up)

        cli = load_script('bench_claude_for_phase28', 'scripts/bench_claude.py')

        class FakeBackend:
            homes, preset = [], []

            def __init__(self, command, home, **kwargs):
                FakeBackend.homes.append(Path(home))
                self.profiles = list(FakeBackend.preset)

            def rpc(self, method, **params):
                if method == 'model_save':
                    self.profiles = [{'id': 'p-eval', 'connection_id': 'p-eval', 'name': cli.PROVIDER_NAME,
                                      'kind': 'anthropic', 'url': cli.ar.ANTHROPIC_URL, 'model': params['model'],
                                      'context': params['context'], 'budget': params['context'], 'has_key': True,
                                      'local': False, 'tools': True}]
                    return {}
                if method == 'state':
                    return {'profiles': self.profiles}
                raise AssertionError(method)

            def close(self):
                pass

        measure = type('Measure', (), {'Backend': FakeBackend})()
        none = {'connection_ids': set(), 'host_ids': set(), 'stores': 0}

        def terminal():
            read = []
            try:
                cli.hidden_key('key: ', terminal=False, read=lambda prompt: read.append(prompt) or FIXTURE_KEY)
                raise AssertionError('the key was read without a terminal')
            except bp.PlanRefused as exc:
                assert 'key_entry_needs_a_terminal' in str(exc)

            def echoing(prompt):
                import getpass
                import warnings
                warnings.warn('Can not control echo on the terminal.', getpass.GetPassWarning)
                read.append(prompt)
                return FIXTURE_KEY
            try:
                cli.hidden_key('key: ', terminal=True, read=echoing)
                raise AssertionError('the echoing fallback was read')
            except bp.PlanRefused:
                pass
            assert read == [], 'a key was read'
            cwd = os.getcwd()
            os.chdir(scratch)
            try:
                from pebble.evaluation import EvidenceLedger
                cli.eval_provider(backend=scratch / 'b', home='relative-home', ledger=EvidenceLedger(scratch / 'l1'),
                                  metadata=scratch / 'm1.json', model=MODEL, ask=lambda text: FIXTURE_KEY,
                                  measure=measure, installed=none)
            finally:
                os.chdir(cwd)
            home = FakeBackend.homes[-1]
            assert home.is_absolute() and home == (scratch / 'relative-home').resolve(), home
            for path in scratch.rglob('*'):
                if path.is_file():
                    assert FIXTURE_KEY not in path.read_text(errors='ignore'), path
            return {'no_terminal': 'refused before reading', 'echo_fallback': 'refused before reading',
                    'home': 'absolute'}
        self.check('the_key_is_read_only_on_a_terminal_and_the_home_is_absolute', terminal)

        def finished():
            from pebble.evaluation import EvidenceLedger, EvaluationError
            home = scratch / 'saved-home'
            home.mkdir(mode=0o700)
            (home / 'pebble.sqlite').write_bytes(b'')
            FakeBackend.preset = [{'id': 'p-eval', 'connection_id': 'p-eval', 'name': cli.PROVIDER_NAME,
                                   'kind': 'anthropic', 'url': cli.ar.ANTHROPIC_URL, 'model': MODEL, 'context': 1000000,
                                   'budget': 1000000, 'has_key': True, 'local': False, 'tools': True}]
            ledger = EvidenceLedger(scratch / 'l2')
            summary = cli.finish_provider(backend=scratch / 'b', home=home, ledger=ledger, metadata=scratch / 'm2.json',
                                          model=MODEL, measure=measure, installed=none)
            assert summary['evaluation_provider'] == 'recorded'
            meta = json.loads((scratch / 'm2.json').read_text())
            assert cli.ar.require_provider(meta, ledger, MODEL, installed=none)['evaluation_provider'] == 'separate'
            refused = {}
            for name, preset in (('none', []), ('two', FakeBackend.preset * 2)):
                FakeBackend.preset = preset
                try:
                    cli.finish_provider(backend=scratch / 'b', home=home, ledger=EvidenceLedger(scratch / ('l-' + name)),
                                        metadata=scratch / ('m-' + name + '.json'), model=MODEL, measure=measure,
                                        installed=none)
                    raise AssertionError('finished with %s saved profiles' % name)
                except EvaluationError as exc:
                    refused[name] = str(exc)
            FakeBackend.preset = []
            return {'recorded': True, 'refused': refused}
        self.check('an_interrupted_key_entry_is_finished_without_the_key', finished)

        def version():
            current = checkout_version()
            parts = tuple(int(x) for x in current.split('.'))
            assert parts > (1, 0, 1), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_after_1_0_1', version)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase28-')).resolve()
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
