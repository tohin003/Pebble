#!/usr/bin/env python3
"""Phase 41 service validator: 1.7.0, Pebble's Workshop part 2: agent-made tools (D67-D70, D90, D98, D99).

Inside a RunCode script, pebble.make_tool makes a pure tool that is checked statically, then validated in a fresh
sandboxed worker against the maker's tests and about 50 generated inputs (each twice), and callable at once as
pebble.mine.<name>; pebble.keep_tool keeps it in Pebble's Toolbox under D69. These checks drive the checked-in App,
runner, Code Mode, the worker and workshop.py in process with a scripted root model (whose replies also stand in for
the blind check's side call) and a throwaway home folder, the same in source and frozen modes:
  - a tool is made, validated and used in one script, in a conversation without a project (D98);
  - a tool its own tests miss but a generated input breaks is refused, with the finding;
  - Pebble keeps a validated, used, untainted, blind-checked tool on its own and records that it did so;
  - a keep after untrusted output (here the Shell's) waits for the person instead;
  - a kept tool reaches a later script only after re-validation; a changed one is never run and needs attention;
  - FindTool finds a kept tool by what it does;
  - PEBBLE_TOOL_MAKING=0 restores 1.6.0's RunCode description;
  - the Toolbox routes are main-window only;
  - the release version is 1.7.0 and its notes describe this without a saving claim.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
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
import validate_phase40 as v40  # noqa: E402  (its Companion task helper)

for _path in (ROOT / 'backend', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'a_tool_is_made_validated_and_used_in_one_script',
    'a_tool_that_generated_inputs_break_is_refused',
    'pebble_keeps_a_clean_tool_on_its_own_and_records_it',
    'a_keep_after_untrusted_output_waits_for_the_person',
    'kept_tools_are_re_validated_and_a_changed_one_never_runs',
    'find_tool_finds_a_kept_tool',
    'the_switch_restores_the_1_6_0_description',
    'the_toolbox_routes_are_main_window_only',
    'release_version_is_1_7_0_and_its_notes_describe_it',
    'backend_inputs_stable_during_campaign',
)
READS = ('backend/pebble/workshop.py', 'backend/pebble/code_mode.py', 'backend/pebble/code_worker.py',
         'backend/pebble/companion_tools.py', 'backend/pebble/runner.py', 'backend/pebble/app.py',
         'backend/pebble/tool_authz.py', 'src-tauri/src/main.rs', 'scripts/validate_phase34.py',
         'scripts/validate_phase40.py', 'scripts/validate_phase41.py', 'CHANGELOG.md', 'backend/pebble/__init__.py',
         'package.json', 'package-lock.json', 'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock', 'src-tauri/tauri.conf.json')
ADD = "def run(args):\n    return args['a'] + args['b']\n"
INPUTS = {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}}, 'required': ['a', 'b']}
TESTS = [{'args': {'a': 1, 'b': 2}, 'expect': 3}, {'args': {'a': -1, 'b': 1}, 'expect': 0},
         {'args': {'a': 5, 'b': 5}, 'expect': 10}]
BLIND = v34.final(json.dumps([{'args': {'a': n, 'b': n + 1}, 'expect': 2 * n + 1} for n in range(5)]))


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


def script(body):
    return v34.call('RunCode', {'code': body, 'purpose': 'Sum pairs'}, 'rc1')


def make_and_use(keep=False, code=ADD, tests=TESTS):
    lines = [f"r = pebble.make_tool(name='add_pair', purpose='Add two integers', code={code!r}, tests={tests!r}, "
             f"inputs={INPUTS!r})", "print('MADE', r.get('made'), r.get('findings', []))",
             "if r.get('made'):", "    print('SUM', pebble.mine.add_pair(a=20, b=22))"]
    if keep:
        lines += ["    k = pebble.keep_tool(name='add_pair', reason='sums pairs')", "    print('KEEP', json.dumps(k))"]
    return 'import json\n' + '\n'.join(lines) + '\n'


def printed(result, ident='rc1'):
    return v40.outcome(result, ident).get('untrusted_printed', '')


class Campaign(v40.Campaign):
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

    def task(self, *args, **kw):
        result = v40.task(self.home, *args, **kw)
        self.apps.append(result['app'])
        return result

    def run(self):
        scratch = self.scratch
        self.home = scratch / 'home'

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            self.home.mkdir()
            os.environ['HOME'] = str(self.home)
            return {'outside_checkout': True, 'home': 'scratch'}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        def made():
            result = self.task([script(make_and_use())], text='Add these pairs for me.')
            assert 'RunCode' in result['tools'][0], 'RunCode is not offered without a project (D98)'
            out = printed(result)
            assert 'MADE True []' in out and 'SUM 42' in out, out
            notes = [n for n in result['app'].runs.events(result['run_id'], limit=200) if n['kind'] == 'workshop_made']
            assert notes, 'no workshop_made note'
            return {'made': 'add_pair', 'used': 'pebble.mine.add_pair(a=20, b=22) == 42'}
        self.check('a_tool_is_made_validated_and_used_in_one_script', made)

        def broken():
            bad = "def run(args):\n    return args['a'] // args['b']\n"
            tests = [{'args': {'a': 4, 'b': 2}, 'expect': 2}, {'args': {'a': 9, 'b': 3}, 'expect': 3},
                     {'args': {'a': 1, 'b': 1}, 'expect': 1}]
            result = self.task([script(make_and_use(code=bad, tests=tests))])
            out = printed(result)
            assert 'MADE False' in out and 'ZeroDivisionError' in out and 'SUM' not in out, out
            return {'refused': 'a // b with b = 0 among the generated inputs'}
        self.check('a_tool_that_generated_inputs_break_is_refused', broken)

        def kept():
            result = self.task([script(make_and_use(keep=True)), BLIND])
            out = printed(result)
            assert '"kept": true' in out and '"by": "pebble"' in out, out
            box = result['app'].store.setting('toolbox')
            assert [t['spec']['name'] for t in box['tools']] == ['add_pair'] and box['tools'][0]['kept_by'] == 'pebble'
            assert box['tools'][0]['blind'] == {'run': 5, 'passed': 5}
            self.kept_app = result['app']
            return {'kept_by': 'pebble', 'blind_check': '5 of 5', 'notice': 'emitted and recorded (workshop_kept)'}
        self.check('pebble_keeps_a_clean_tool_on_its_own_and_records_it', kept)

        def tainted():
            result = self.task([v34.call('Shell', {'command': 'echo some output'}, 's1'), script(make_and_use(keep=True))])
            out = printed(result)
            assert '"asked": true' in out and 'untrusted' in out, out
            box = result['app'].store.setting('toolbox')
            assert not box['tools'] and box['pending'] and box['pending'][0]['record']['spec']['name'] == 'add_pair'
            return {'asked': True, 'why': 'Shell output read before the tool was made'}
        self.check('a_keep_after_untrusted_output_waits_for_the_person', tainted)

        def revalidated():
            from pebble import workshop
            app = getattr(self, 'kept_app', None)
            assert app is not None, 'Prerequisite: the kept tool of the earlier check.'
            box = app.store.setting('toolbox')

            def seed(target):
                target.store.set_setting('toolbox', box)
            later = self.task([script("print('LATER', pebble.mine.add_pair(a=1, b=1))\n")], app_setup=seed)
            assert 'LATER 2' in printed(later), printed(later)
            tampered = copy.deepcopy(box)
            # A change its own tests and the generated inputs do not notice (it differs only at a=123456): the digest is
            # the layer that must refuse it (a change the tests notice is refused by re-validation as well).
            tampered['tools'][0]['spec']['code'] = ("def run(args):\n"
                                                    "    return args['a'] + args['b'] + (1 if args['a'] == 123456 else 0)\n")

            def seed_bad(target):
                target.store.set_setting('toolbox', tampered)
            bad = self.task([script("print('LATER', pebble.mine.add_pair(a=1, b=1))\n")], app_setup=seed_bad)
            assert 'LATER' not in printed(bad), printed(bad)
            assert workshop.listing(bad['app'])['tools'][0]['state'] == 'needs_attention'
            return {'re_validated_then_used': True, 'changed_tool': 'not run, needs attention'}
        self.check('kept_tools_are_re_validated_and_a_changed_one_never_runs', revalidated)

        def found():
            app = getattr(self, 'kept_app', None)
            assert app is not None, 'Prerequisite: the kept tool of the earlier check.'
            box = app.store.setting('toolbox')
            result = self.task([v34.call('FindTool', {'query': 'add two integers'}, 'f1')],
                               app_setup=lambda target: target.store.set_setting('toolbox', box))
            answer = v40.outcome(result, 'f1')
            assert answer.get('toolbox') and answer['toolbox'][0]['name'] == 'pebble.mine.add_pair', answer
            return {'found': 'pebble.mine.add_pair'}
        self.check('find_tool_finds_a_kept_tool', found)

        def switch():
            probe = ('import sys; sys.path.insert(0, %r); from pebble import code_mode; '
                     'print("make_tool" in code_mode.DESCRIPTION)') % str(ROOT / 'backend')
            on = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=60,
                                env={**os.environ, 'PEBBLE_TOOL_MAKING': '1'})
            off = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=60,
                                 env={**os.environ, 'PEBBLE_TOOL_MAKING': '0'})
            assert on.stdout.strip() == 'True' and off.stdout.strip() == 'False', (on.stdout, off.stdout, off.stderr[-300:])
            return {'on': 'Workshop sentence', 'off': "1.6.0's description"}
        self.check('the_switch_restores_the_1_6_0_description', switch)

        def routes():
            shell = read('src-tauri/src/main.rs')
            allowed = shell[shell.index('const ALLOWED: &[&str] = &['):]
            allowed = allowed[:allowed.index('];')]
            pet = shell[shell.index('if window.label()=="pet" && !['):]
            pet = pet[:pet.index('].contains(')]
            from pebble.tool_authz import NATIVE_ONLY
            # Phase 45 (F178): Put back is the main window's too
            routes = ('toolbox_list', 'toolbox_get', 'toolbox_remove', 'toolbox_decide', 'toolbox_undo_remove')
            for route in routes:
                assert f'"{route}"' in allowed and f'"{route}"' not in pet and route in NATIVE_ONLY, route
            return {'main_window_only': len(routes)}
        self.check('the_toolbox_routes_are_main_window_only', routes)

        def version():
            current = checkout_version()
            assert tuple(int(x) for x in current.split('.')) >= (1, 7, 0), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = changelog_entry('1.7.0')
            for needle in ("Pebble's Workshop", "Pebble's Toolbox", 'make_tool', 'tests', 'always tells you',
                           'asks you', 'View code', 'Remove'):
                assert needle in entry, ('the 1.7.0 notes lack', needle)
            for claim in ('faster', 'cheaper', 'undetectable', '% fewer', 'saves '):
                assert claim not in entry.lower(), ('the 1.7.0 notes claim', claim)
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_1_7_0_and_its_notes_describe_it', version)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase41-', dir='/private/tmp')).resolve()
    real_home = os.environ.get('HOME')
    started = time.monotonic()
    campaign = Campaign(scratch, backend)
    fatal = None
    try:
        campaign.run()
    except Exception as exc:
        fatal = bounded(exc, 1500)
    finally:
        campaign.close()
        if real_home is not None:
            os.environ['HOME'] = real_home
    after_digests = digests()
    stable = before == after_digests and (not backend or sha(backend.read_bytes()) == backend_sha)
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
