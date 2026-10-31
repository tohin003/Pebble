#!/usr/bin/env python3
"""Phase 35 service validator: 1.3.0, requests that extend each other above 8K (beat plan W5) and speed (W7).

A provider's prompt cache can only reuse a request's prefix. Measured on the 1.2.0 source (runner.prefix_report on a
scripted 32K coding task), every request from the second on kept only the system message. These checks drive the
checked-in App, runner, providers and broker in process (only the model, or the provider's transport, is scripted),
the same in source and frozen modes:
  - above 8K every request of a coding task and of a planned task extends the previous one, and the source audit and
    the plan are appended (only the newest plan is protected);
  - at 8K and below nothing changes: the checkpoint still carries the latest observations, RunTests has no check and
    the ChatGPT route asks for one tool call at a time;
  - the system prompt above 8K carries the date only, the time is in the run's checkpoint, and two tasks in a row
    send the same system prompt;
  - a web run keeps one tool list until it records a page (ReadSource still joins only then, as Phase 16 R1 requires);
  - on the Anthropic route the first message stays the same for every request of a task, and the stale-block
    binding is still requested;
  - above 8K the ChatGPT route allows several tool calls per response, performed one at a time;
  - the release version is 1.3.0 and its notes describe this without a cost claim.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
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
import validate_phase34 as v34  # noqa: E402  (its scripted-model harness)

for _path in (ROOT / 'backend', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'requests_above_8k_extend_each_other',
    'eight_k_and_below_are_unchanged',
    'the_system_prompt_is_the_same_for_a_days_tasks',
    'a_run_keeps_one_tool_list_until_a_page_is_recorded',
    'anthropic_requests_keep_their_first_message',
    'chatgpt_allows_parallel_tool_calls_above_8k',
    'release_version_is_1_3_0_and_its_notes_describe_it',
    'backend_inputs_stable_during_campaign',
)
READS = ('backend/pebble/runner.py', 'backend/pebble/responses.py', 'backend/pebble/providers.py',
         'backend/pebble/tool_catalogue.py', 'backend/pebble/token_calibration.py', 'backend/pebble/app.py',
         'scripts/validate_phase34.py', 'scripts/validate_phase28.py', 'CHANGELOG.md', 'scripts/validate_phase35.py',
         'backend/pebble/__init__.py', 'package.json', 'package-lock.json', 'src-tauri/Cargo.toml',
         'src-tauri/tauri.conf.json', 'src-tauri/Cargo.lock')
CHATGPT = {'id': 'p35', 'name': 'Phase 35 ChatGPT fixture', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
           'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'tools': True}
PROBE_TOOLS = [{'type': 'function', 'function': {'name': 'Read', 'description': 'Read a project file.',
                                                  'parameters': {'type': 'object', 'properties': {}}}}]
PLAN1 = json.dumps([{'id': 'read', 'title': 'Read stats.py', 'status': 'in_progress'},
                    {'id': 'fix', 'title': 'Fix mean', 'status': 'pending'}])
PLAN2 = json.dumps([{'id': 'read', 'title': 'Read stats.py', 'status': 'completed'},
                    {'id': 'fix', 'title': 'Fix mean', 'status': 'in_progress'}])


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


class Scripted(v34.Scripted):
    """validate_phase34's scripted root model, also keeping the tool names each request offered."""

    def __enter__(self):
        super().__enter__()
        self.tools, inner = [], self.providers.complete

        def complete(provider, messages, tools, cancel, token, effort=False):
            self.tools.append([t['function']['name'] for t in tools or ()])
            return inner(provider, messages, tools, cancel, token, effort)
        self.providers.complete = complete
        return self


class Capture(Exception):
    """Raised by Probe once the request body is built."""


class Probe:
    """The provider surface complete_responses uses; it keeps the body and sends nothing."""
    last_usage = None
    prompt_cache_key = None

    def __init__(self):
        self.bodies = []

    def key(self, profile):
        return 'fixture'

    def headers(self, kind, key):
        return {}

    def request(self, url, headers, body, timeout=None):
        self.bodies.append((url, body))
        raise Capture()

    def project_context(self, messages, tools, budget, counter=None, method=None):
        return messages


def scripted_run(home, script, text, context=32768, web=False):
    """(prefix reports, the requests as the model received them, the run, offered tool names) for one task."""
    folder = None if web else v34.project(home / 'project')
    app = v34.app_for(home, 'auto', context)
    try:
        with Scripted() as scripted:
            scripted.script = script + [v34.final('Done.')] * 3
            chat = app.new_chat(str(folder) if folder else None)
            app.work(chat, text, False, [])
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                runs = app.runs.list(chat['id'])
                if runs and app.active is None and runs[0]['state'] not in ('queued', 'running', 'retry_wait'):
                    break
                time.sleep(.02)
            rid = app.runs.list(chat['id'])[0]['id']
            reports = [e['payload'].get('prefix') for e in app.runs.events(rid, limit=200) if e['kind'] == 'model_attempt']
            return reports, list(scripted.requests), app.runs.get(rid), list(scripted.tools)
    finally:
        v34.close(app)


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

        def extend():
            from pebble import runner
            coding, requests, _, _ = scripted_run(scratch / 'coding', [
                v34.call('Read', {'file_path': 'stats.py'}, 'r1'), v34.call('Read', {'file_path': 'test_stats.py'}, 'r2'),
                v34.call('Write', {'file_path': 'stats.py', 'content': v34.FIXED}, 'w1'),
                v34.call('RunTests', {'recipe': 'python-unittest', 'check': v34.CHECK}, 'c1'),
                v34.call('RunTests', {'recipe': 'python-unittest'}, 't1')], 'Make mean return 0.0 for an empty list.')
            # Phase 46 (runner.PREFETCH): the host's prefetch of the tiny project's two files is appended after the
            # request (5 messages without it; its views are current, so no audit lists them until one changes); every
            # later request still extends the one before.
            extra = ['pebble_prefetch'] if runner.PREFETCH else []
            assert coding[0] == {'messages': 5 + len(extra), 'first': True}, coding[0]
            assert [m.get('name') for m in requests[0][5:]] == extra, [m.get('name') for m in requests[0]]
            assert len(coding) >= 6 and all(r and r['append_only'] for r in coding[1:]), coding
            audits = [m for m in requests[-1] if m.get('name') == 'pebble_source_status']
            assert len(audits) >= 2 and all(m.get('_pebble_source') and runner.AUDIT_TEXT in m['content'] for m in audits)
            planned, requests, _, _ = scripted_run(scratch / 'plan', [
                v34.call('UpdatePlan', {'steps': PLAN1}, 'p1'), v34.call('Read', {'file_path': 'stats.py'}, 'r1'),
                v34.call('UpdatePlan', {'steps': PLAN2}, 'p2')], 'First plan the work, then make mean return 0.0.')
            assert all(r and r['append_only'] for r in planned[1:]), planned
            plans = [m for m in requests[-1] if isinstance(m.get('content'), str) and m['content'].startswith('HOST PLAN')]
            assert plans and plans[-1].get('_pebble_plan') and all(not m.get('_pebble_plan') for m in plans[:-1])
            return {'coding_requests': len(coding), 'planned_requests': len(planned), 'audits_appended': len(audits),
                    'append_only': True}
        self.check('requests_above_8k_extend_each_other', extend)

        def small():
            from pebble.responses import complete_responses
            from pebble.tool_catalogue import TOOLS, without_check
            evidence = {}
            for context in (8192, 4096):
                reports, requests, _, offered = scripted_run(scratch / f'small-{context}', [
                    v34.call('Read', {'file_path': 'stats.py'}, 'r1')], 'What does stats.py do?', context=context)
                assert all(r is None for r in reports), reports
                checkpoint = next(m for m in requests[-1] if m.get('_pebble_checkpoint') and m.get('name') is None)
                assert checkpoint['content'].startswith('HOST TASK CHECKPOINT') and 'stats.py' in checkpoint['content']
                evidence[context] = 'checkpoint rewritten as before'
            spec = next(t for t in TOOLS if t['function']['name'] == 'RunTests')
            assert 'check' not in without_check(spec)['function']['parameters']['properties']
            probe = Probe()
            try:
                complete_responses(probe, {**CHATGPT, 'budget': 8192}, [{'role': 'user', 'content': 'Hello.', '_pebble_current': True}],
                                   PROBE_TOOLS, threading.Event(), [].append, 'medium')
            except Capture:
                pass
            assert probe.bodies and probe.bodies[-1][1]['parallel_tool_calls'] is False
            evidence.update(runtests_offered_at_8k='without check', parallel_tool_calls_at_8k=False)
            return evidence
        self.check('eight_k_and_below_are_unchanged', small)

        def system():
            from pebble import runner
            first = scripted_run(scratch / 'day-1', [], 'What does stats.py do?')[1][0]
            second = scripted_run(scratch / 'day-2', [], 'Summarise test_stats.py.')[1][0]
            assert first[0]['content'] == second[0]['content'], 'the system prompt differs between two tasks'
            assert re.search(r'Current date: \d{4}-\d{2}-\d{2}\.', first[0]['content']) and 'Local time' not in first[0]['content']
            checkpoint = next(m for m in first if m.get('_pebble_checkpoint') and m.get('name') is None)
            assert checkpoint['content'].startswith(runner.STABLE_CHECKPOINT)
            assert re.search(r'Local time when this task started: \d{2}:\d{2}', checkpoint['content'])
            return {'system_chars': len(first[0]['content']), 'time_in': 'the task checkpoint'}
        self.check('the_system_prompt_is_the_same_for_a_days_tasks', system)

        def tools():
            reports, _, _, offered = scripted_run(scratch / 'web', [v34.call('Weather', {'city': ''}, 'w1')],
                                                  'Search the web for the Port Ellery harbour dues.', web=True)
            assert len(offered) >= 2 and {'WebSearch', 'ReadURL'} <= set(offered[0]), offered
            assert all('ReadSource' not in names for names in offered), 'ReadSource without a recorded page (Phase 16 R1)'
            assert all(names == offered[0] for names in offered[1:]), offered
            assert all(r is None or not r.get('tools_changed') for r in reports), reports
            return {'requests': len(offered), 'tools_offered': len(offered[0]), 'read_source': 'only after a recorded page'}
        self.check('a_run_keeps_one_tool_list_until_a_page_is_recorded', tools)

        def anthropic():
            import validate_phase28 as v28
            (scratch / 'anthropic-runs').mkdir()
            sent = v28.Campaign(scratch / 'anthropic-runs').in_process('https://api.anthropic.com/v1', v28.MODEL, tool_turns=2)
            assert len(sent) >= 3, len(sent)
            first = sent[0][1]['messages'][0]
            assert all(body['messages'][0] == first for _, body in sent[1:]), 'the first message changes per step'
            for headers, body in sent:
                assert body['thinking'] == {'type': 'adaptive', 'block_binding': v28.BINDING}, body.get('thinking')
                assert body.get('cache_control') == {'type': 'ephemeral'}
            return {'requests': len(sent), 'first_message_kept': True, 'binding': v28.BINDING}
        self.check('anthropic_requests_keep_their_first_message', anthropic)

        def parallel():
            from pebble.responses import complete_responses
            seen = {}
            for budget in (32768, 8193, 8192, 4096):
                probe = Probe()
                try:
                    complete_responses(probe, {**CHATGPT, 'budget': budget}, [{'role': 'user', 'content': 'Hello.',
                                       '_pebble_current': True}], PROBE_TOOLS, threading.Event(), [].append, 'medium')
                except Capture:
                    pass
                seen[budget] = probe.bodies[-1][1]['parallel_tool_calls']
            assert seen == {32768: True, 8193: True, 8192: False, 4096: False}, seen
            return {'parallel_tool_calls': {str(k): v for k, v in seen.items()}}
        self.check('chatgpt_allows_parallel_tool_calls_above_8k', parallel)

        def version():
            current = checkout_version()
            # D66 (Phase 36): the release that ships cache-first requests is 1.3.0 and this check reads its entry; a
            # later checkout keeps that entry and every assertion on it (as D65 did for validate_phase34).
            assert tuple(int(x) for x in current.split('.')) >= (1, 3, 0), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = changelog_entry('1.3.0')
            for needle in ('extends the previous one', 'prompt cache', 'several tool calls', 'not claimed here'):
                assert needle in entry, ('the 1.3.0 notes lack', needle)
            assert 'Context Warping' not in entry, 'Context Warping is named before its gate (D55)'
            # No saving is claimed before the benchmark measures one (D55): a measured fact like "99 % of their time"
            # is fine, a percentage cheaper, lower, faster or saved is not.
            claims = re.findall(r'\d+(?:\.\d+)?\s?%\s*(?:cheaper|less|lower|faster|saving|saved|off)\b|\bcheaper\b', entry, re.I)
            assert not claims, ('the 1.3.0 notes claim a saving', claims)
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_1_3_0_and_its_notes_describe_it', version)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase35-', dir='/private/tmp')).resolve()
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
