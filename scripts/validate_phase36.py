#!/usr/bin/env python3
"""Phase 36 service validator: 1.4.0, a long task's older tool results are sealed in large chunks (beat plan W6).

Since 1.3.0 every request above 8K extends the previous one, so a provider's cache reads all but the newest step; a
long task's window still grows with every result. Above a 32K budget, once the window passes 32K estimated tokens (or a
quarter of a smaller budget), older tool results are replaced in one chunk by stubs that keep the tool, the result's
facts and a `page:<generation>:<index>` handle ReadRunEvidence serves from the run's archive. These checks drive the
checked-in App, runner, journal and broker in process (only the root model is scripted; some steps answer what the
window shows), the same in source and frozen modes:
  - a long task seals in chunks of at least 20K tokens and at least twice the part the provider reads fresh again;
    every other request extends the one before it, and ReadRunEvidence joins the tools in the first seal's step;
  - a sealed result reads back byte for byte through the handle the run itself made;
  - every tool call stays, the newest exchanges stay word for word, and a test result and a failed result are never
    sealed;
  - a short task never seals and is the same with the switch off; a 32K window never seals;
  - with PEBBLE_CONTEXT_SEAL=0 a long task never seals and ReadRunEvidence joins at half the window, as in 1.3.0;
  - the run journal refuses a seal built for another archive generation;
  - the release version is 1.4.0 and its notes describe this without a saving claim or the Context Warping name.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import copy
import hashlib
import json
import os
import re
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
from validate_phase20 import write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402
import validate_phase34 as v34  # noqa: E402  (its scripted-model harness)

for _path in (ROOT / 'backend', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'a_long_task_seals_older_results_in_chunks',
    'a_sealed_result_reads_back_byte_for_byte',
    'calls_the_newest_exchanges_and_never_sealed_results_stay',
    'short_tasks_and_32k_windows_never_seal',
    'the_switch_off_keeps_the_1_3_0_requests',
    'the_journal_refuses_a_stale_seal',
    'release_version_is_1_4_0_and_its_notes_describe_it',
    'backend_inputs_stable_during_campaign',
)
READS = ('backend/pebble/context_seal.py', 'backend/pebble/runner.py', 'backend/pebble/runtime.py',
         'backend/pebble/app.py', 'backend/pebble/compaction.py', 'scripts/validate_phase34.py', 'CHANGELOG.md',
         'scripts/validate_phase36.py', 'backend/pebble/__init__.py', 'package.json', 'package-lock.json',
         'src-tauri/Cargo.toml', 'src-tauri/tauri.conf.json', 'src-tauri/Cargo.lock')
BUDGET = 131072
MIN_CHUNK_TOKENS = 20000   # the release's promise, fixed here so a changed constant cannot pass by changing both
NEWEST_KEPT = 3            # at least this many newest exchanges stay word for word


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


class Responder(v34.Scripted):
    """validate_phase34's scripted root model; a callable step answers the messages it is sent, and each request's
    offered tool names are kept."""

    def __enter__(self):
        self.real = self.providers.complete
        self.tools = []

        def complete(provider, messages, tools, cancel, token, effort=False):
            self.requests.append(copy.deepcopy(messages))
            self.tools.append([t['function']['name'] for t in tools or ()])
            assert len(self.requests) <= len(self.script), 'Unscripted root model request.'
            step = self.script[len(self.requests) - 1]
            response = copy.deepcopy(step(messages) if callable(step) else step)
            if response.get('content'):
                token(response['content'])
            return response
        self.providers.complete = complete
        return self


def parts(folder, count, lines=260):
    """`count` files of `lines` distinct records (about 64 characters each), in a new folder (or v34's project)."""
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        rows = [f'record {i}-{j}: ' + hashlib.sha256(f'{i}-{j}'.encode()).hexdigest() for j in range(lines)]
        (folder / f'part{i:02d}.txt').write_text('\n'.join(rows) + '\n')
    return folder


def reads(count):
    return [v34.call('Read', {'file_path': f'part{i:02d}.txt'}, f'r{i}') for i in range(count)]


def all_events(app, rid):
    found, after = [], 0
    while True:
        page = app.runs.events(rid, after=after, limit=200)
        if not page:
            return found
        found += page
        after = page[-1]['sequence']


def task(home, script, count=20, lines=260, context=BUDGET, switch=None, coding=False):
    """One scripted task: {'run', 'events', 'requests', 'tools', 'receipts', 'enabled'}."""
    folder = v34.project(home / 'project') if coding else home / 'project'
    parts(folder, count, lines)
    saved = os.environ.get('PEBBLE_CONTEXT_SEAL')
    if switch is not None:
        os.environ['PEBBLE_CONTEXT_SEAL'] = switch
    try:
        app = v34.app_for(home, 'auto', context)  # the App reads the switch when it starts
    finally:
        if saved is None:
            os.environ.pop('PEBBLE_CONTEXT_SEAL', None)
        else:
            os.environ['PEBBLE_CONTEXT_SEAL'] = saved
    try:
        with Responder() as scripted:
            scripted.script = script + [v34.final('Done.')] * 3
            chat = app.new_chat(str(folder))
            app.work(chat, 'Read every part file in order, then say you are done.', False, [])
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                runs = app.runs.list(chat['id'])
                if runs and app.active is None and runs[0]['state'] not in ('queued', 'running', 'retry_wait'):
                    break
                time.sleep(.02)
            rid = app.runs.list(chat['id'])[0]['id']
            return {'run': app.runs.get(rid), 'events': all_events(app, rid), 'requests': list(scripted.requests),
                    'tools': list(scripted.tools), 'receipts': app.runs.tools(rid),
                    'enabled': getattr(app, 'context_seal_enabled', None)}
    finally:
        v34.close(app)


def cost(message):
    """One message's tokens by context_manifest's conservative estimate (additive; the window framing left out)."""
    from pebble.context_manifest import BASE_TOKENS, estimate_tokens
    return estimate_tokens([message]) - BASE_TOKENS


def stubs(messages):
    return [json.loads(m['content']) for m in messages
            if m.get('role') == 'tool' and isinstance(m.get('content'), str) and m['content'].startswith('{"sealed"')]


def seals(result):
    return [e['payload'] for e in result['events'] if e['kind'] == 'context_sealed']


def prefixes(result):
    return [e['payload'].get('prefix') for e in result['events'] if e['kind'] == 'model_attempt']


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

        def chunks():
            evidence = {}
            # At 64K the half-window point (1.3.0's ReadRunEvidence offer) comes before the first seal; at 128K they
            # fall close together. Both budgets must change their request once per seal and at no other step.
            for budget in (BUDGET, 65536):
                result = task(scratch / f'long-{budget}', reads(20), context=budget)
                assert result['run']['state'] == 'completed', result['run']['state']
                sealed = seals(result)
                assert sealed and [s['generation'] for s in sealed] == list(range(1, len(sealed) + 1)), sealed
                reports = prefixes(result)
                changed = [i for i, p in enumerate(reports) if p and not p.get('first') and not p['append_only']]
                assert len(changed) == len(sealed), ('requests that did not extend the previous one', budget,
                                                     changed, len(sealed))
                fresh = []
                for step, event in zip(changed, sealed):
                    manifest = event['manifest']
                    assert manifest['kind'] == 'seal' and manifest['archived_results'] >= 1, manifest
                    request, first = result['requests'][step], reports[step]['first_change']['index']
                    assert first is not None and request[first].get('role') == 'tool', reports[step]
                    # Sizes recomputed here from the requests, with the product's conservative estimator: the
                    # results sealed in this step (as the request before showed them) and what follows the first.
                    earlier = {m['tool_call_id']: m for m in result['requests'][step - 1] if m.get('role') == 'tool'}
                    new = [m['tool_call_id'] for m in request if m.get('role') == 'tool' and stubs([m])
                           and not stubs([earlier.get(m['tool_call_id'], m)])]
                    assert len(new) == manifest['archived_results'], (new, manifest['archived_results'])
                    chunk = sum(cost(earlier[c]) for c in new)
                    assert chunk >= MIN_CHUNK_TOKENS, ('a chunk below 20K tokens', chunk)
                    reread = sum(cost(m) for m in request[first:] if not stubs([m]))
                    if manifest['payback_waived']:
                        # Only past three quarters of the budget (conservative estimate), where ReadRunEvidence joins
                        # anyway and the 100 % compaction, which rewrites more, is near.
                        from pebble.context_manifest import estimate_tokens
                        assert manifest['budget'] == budget and manifest['window_tokens'] > budget * 3 // 4, (
                            'a waived payback below three quarters', budget, step, manifest.get('window_tokens'))
                        assert estimate_tokens(result['requests'][step - 1]) > budget // 2, ('a waived payback', step)
                    else:
                        assert chunk >= 1.5 * reread, ('a chunk that does not repay its fresh read', chunk, reread)
                    fresh.append((chunk, reread))
                offered = ['ReadRunEvidence' in names for names in result['tools']]
                assert True in offered and offered.index(True) == changed[0], (
                    'ReadRunEvidence joined apart from a seal', budget, offered, changed)
                before, after = result['requests'][changed[0] - 1], result['requests'][changed[0]]
                assert len(json.dumps(after)) < len(json.dumps(before)) // 2, 'the first seal did not halve the window'
                evidence[str(budget)] = {'seals': len(sealed), 'requests': len(result['requests']),
                                         'seal_steps': changed,
                                         'payback_waived': [s['manifest']['payback_waived'] for s in sealed],
                                         'sealed_chars': [s['manifest']['sealed_chars'] for s in sealed],
                                         'chunk_and_fresh_tokens': fresh, 'other_requests_append_only': True}
            return evidence
        self.check('a_long_task_seals_older_results_in_chunks', chunks)

        def read_back():
            def first_page(messages):
                return v34.call('ReadRunEvidence', {'receipt_id': stubs(messages)[0]['receipt_id']}, 'back0')

            def next_page(n):
                def step(messages):
                    last = json.loads(messages[-1]['content'])
                    if last.get('next_receipt_id') is None:
                        return v34.final('Done.')
                    return v34.call('ReadRunEvidence', {'receipt_id': last['next_receipt_id']}, f'back{n}')
                return step
            result = task(scratch / 'read-back', reads(20) + [first_page] + [next_page(n) for n in range(1, 8)])
            assert result['run']['state'] == 'completed', result['run']['state']
            final = result['requests'][-1]
            pages = [json.loads(m['content']) for m in final
                     if m.get('role') == 'tool' and m['tool_call_id'].startswith('back')]
            stub = stubs(final)[0]
            call_id = stub_call(final, stub)
            original = next(m['content'] for request in result['requests'] for m in request
                            if m.get('tool_call_id') == call_id and not m['content'].startswith('{"sealed"'))
            assert pages and pages[-1].get('next_offset') is None, pages[-1:]
            assert stub['chars'] == len(original) == pages[0]['total_chars']
            assert ''.join(p['untrusted_text'] for p in pages) == original, 'the archive page differs from the result'
            assert all(p['origin'] == 'archived_tool_result' and p['authorization'] == 'none' for p in pages)
            facts = json.loads(original)
            assert stub['path'] == facts['path'] and stub['file_hash'] == facts['file_hash'], stub
            return {'handle': stub['receipt_id'], 'pages': len(pages), 'chars': len(original), 'byte_for_byte': True}
        self.check('a_sealed_result_reads_back_byte_for_byte', read_back)

        def kept():
            script = [v34.call('RunTests', {'recipe': 'python-unittest'}, 't0'),
                      v34.call('Read', {'file_path': 'missing.txt'}, 'e0')] + reads(20)
            result = task(scratch / 'kept', script, coding=True)
            assert result['run']['state'] == 'completed', result['run']['state']
            assert seals(result), 'the long task did not seal'
            final = result['requests'][-1]
            calls = [c['id'] for m in final for c in m.get('tool_calls') or []]
            assert calls == ['t0', 'e0'] + [f'r{i}' for i in range(20)], calls

            def first_seen(call_id):
                return next(m for request in result['requests'] for m in request if m.get('tool_call_id') == call_id)
            by_call = {m['tool_call_id']: m for m in final if m.get('role') == 'tool'}
            for call_id in ['t0', 'e0'] + [f'r{i}' for i in range(20 - NEWEST_KEPT, 20)]:
                assert by_call[call_id] == first_seen(call_id), ('changed', call_id)
            assert 'error' in json.loads(by_call['e0']['content']), by_call['e0']['content'][:200]
            sealed = [c for c, m in by_call.items() if m['content'].startswith('{"sealed"')]
            assert sealed and not {'t0', 'e0'} & set(sealed), sealed
            for number, request in enumerate(result['requests']):  # at a seal too, the newest results stay
                newest = [m for m in request if m.get('role') == 'tool'][-NEWEST_KEPT:]
                assert not stubs(newest), ('a newest result was sealed', number)
            outside = [m for m in final if m.get('role') == 'user' and not m.get('tool_call_id')]
            assert any('Read every part file' in str(m.get('content')) for m in outside), 'the request is gone'
            return {'calls': len(calls), 'sealed': len(sealed), 'kept_word_for_word': ['t0', 'e0'] + [
                f'r{i}' for i in range(20 - NEWEST_KEPT, 20)]}
        self.check('calls_the_newest_exchanges_and_never_sealed_results_stay', kept)

        def short():
            on = task(scratch / 'short-on', reads(6))
            off = task(scratch / 'short-off', reads(6), switch='0')
            assert on['enabled'] is True and off['enabled'] is False
            assert on['run']['state'] == off['run']['state'] == 'completed'
            assert not seals(on) and not stubs(on['requests'][-1])
            assert on['requests'] == off['requests'] and on['tools'] == off['tools'], 'a short task differs'
            small = task(scratch / 'at-32k', reads(12), count=12, lines=60, context=32768)
            assert small['run']['state'] == 'completed' and not seals(small) and not stubs(small['requests'][-1])
            return {'short_requests': len(on['requests']), 'same_with_switch_off': True,
                    'at_32k_requests': len(small['requests']), 'sealed': 0}
        self.check('short_tasks_and_32k_windows_never_seal', short)

        def switch():
            from pebble.context_manifest import estimate_tokens
            result = task(scratch / 'switch-off', reads(20), switch='0')
            assert result['enabled'] is False and result['run']['state'] == 'completed'
            assert not seals(result) and not stubs(result['requests'][-1]), 'sealed with the switch off'
            first = next(i for i, names in enumerate(result['tools']) if 'ReadRunEvidence' in names)
            requests = result['requests']
            assert estimate_tokens(requests[first]) > BUDGET // 2 >= estimate_tokens(requests[first - 1]), first
            return {'requests': len(result['requests']), 'read_run_evidence_from_request': first,
                    'offer': 'half the window, as in 1.3.0'}
        self.check('the_switch_off_keeps_the_1_3_0_requests', switch)

        def journal():
            from pebble.context_seal import plan, seal
            app = v34.app_for(scratch / 'journal', 'auto', BUDGET)
            try:
                chat = app.new_chat(None)
                rid = app.runs.create(chat['id'], 'Read the parts.', app.runtime_config(chat, False), [])
                messages = [{'role': 'system', 'content': 'You are Pebble.'},
                            {'role': 'user', 'content': 'Read the parts.', '_pebble_current': True}]
                for i in range(20):
                    messages += [{'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': f'c{i}', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}}]},
                        {'role': 'tool', 'tool_call_id': f'c{i}',
                         'content': json.dumps({'path': f'p{i}', 'x': 'y' * 8000})}]
                chosen = plan(messages, BUDGET)
                assert chosen, 'no seal planned for the fixture window'
                _, archive, manifest = seal(messages, chosen, 1)
                refused = []
                for generation in (2, 0):
                    try:
                        app.runs.seal(rid, archive, manifest, generation)
                    except ValueError as exc:
                        refused.append(str(exc))
                assert len(refused) == 2 and all('another archive generation' in r for r in refused), refused
                assert app.runs.seal(rid, archive, manifest, 1) == 1 and app.runs.next_generation(rid) == 2
                try:
                    app.runs.seal(rid, archive, manifest, 1)
                    raise AssertionError('a second seal for generation 1 was accepted')
                except ValueError as exc:
                    assert 'another archive generation' in str(exc), exc
                assert app.runs.context_archive(rid, 1)['archive'] == archive
            finally:
                v34.close(app)
            return {'refused': ['generation 2 first', 'generation 0', 'generation 1 twice'], 'committed': 1}
        self.check('the_journal_refuses_a_stale_seal', journal)

        def version():
            current = checkout_version()
            assert current == '1.4.0', current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = changelog_entry('1.4.0')
            for needle in ('sealed', 'ReadRunEvidence', 'exactly as it was', 'PEBBLE_CONTEXT_SEAL=0',
                           'no saving is claimed here'):
                assert needle in entry, ('the 1.4.0 notes lack', needle)
            assert 'Context Warping' not in entry, 'Context Warping is named before its gate (D55)'
            claims = re.findall(r'\d+(?:\.\d+)?\s?%\s*(?:cheaper|less|lower|faster|saving|saved|off|smaller)\b'
                                r'|\bcheaper\b|\bsaves?\b', entry, re.I)
            assert not claims, ('the 1.4.0 notes claim a saving', claims)
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_1_4_0_and_its_notes_describe_it', version)


def stub_call(messages, stub):
    """The tool call ID of the message that carries `stub`."""
    text = json.dumps(stub, ensure_ascii=False)
    return next(m['tool_call_id'] for m in messages if m.get('role') == 'tool' and m.get('content') == text)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase36-', dir='/private/tmp')).resolve()
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
