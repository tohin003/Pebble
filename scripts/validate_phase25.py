#!/usr/bin/env python3
"""Phase 25 service validator: the 1.0 release build carries no path of the machine that built it.

Phase 24 proved the 1.0 source (reports/harness-phase24-review.json). Its first app build then refused at copy-out
(machine_paths_in_shell): Tauri embeds the build config in the shell, and build_candidate_app gave it an absolute
frontendDist (this checkout's path); and the pinned toolchain's rust-objcopy could not load libLLVM, so the release
profile's strip silently failed and the linker's debug map kept the toolchain's paths. The fix changed
scripts/build_candidate_app.py (a tested source), so it is proved in its own namespace. Checks, the same in source and
frozen modes (the shell is not part of the frozen backend; frozen mode records the executable it was given):
  - the release and test merge configs embed no absolute path, and frontendDist is relative to src-tauri;
  - the release profile strips the shell, and the pinned toolchain's strip tool runs;
  - the copy-out guard finds this machine's paths and only counts another machine's;
  - every release declaration names the checkout's version, 1.0 or later;
  - AgentGuard grounds a task's scope whenever the index build finishes. Trial 1 of this phase failed once in the full
    suite: GuardAdapter.begin read the index after the engine had handled the prompt, so a build finishing in between
    left the task unscoped and six unreached helpers went unchallenged (backend/pebble/guard.py, now read before). The
    check runs that trajectory against the checkout's GuardAdapter and engine in this process for each build order; in
    frozen mode it proves the source, not the executable, as validate_phase24's in-process checks do.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_runtime import ROOT  # noqa: E402
from validate_phase13 import bounded, owned_backend, sha  # noqa: E402
from validate_phase20 import backend_path, write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'release_merge_configs_embed_no_machine_path',
    'release_profile_strips_the_shell',
    'pinned_toolchain_strip_tool_runs',
    'shell_guard_refuses_this_machines_paths_only',
    'release_version_is_1_0_or_later',
    'guard_grounds_the_task_whatever_the_index_build_order',
    'backend_inputs_stable_during_campaign',
)
# The tested sources this validator reads; each is in verify_harness_candidate.sources(), so the campaign binds them.
READS = ('scripts/build_candidate_app.py', 'src-tauri/Cargo.toml', 'rust-toolchain.toml', 'scripts/validate_phase25.py',
         'backend/pebble/__init__.py', 'package.json', 'package-lock.json', 'src-tauri/tauri.conf.json',
         'src-tauri/Cargo.lock', 'backend/pebble/guard.py', 'backend/vendor/agentguard/core/engine.py',
         'backend/vendor/agentguard/repo/index.py', 'backend/vendor/agentguard/validate/checks.py')
GOAL = 'Fix total() in pricing.py so it returns the sum of the prices, as test_pricing.py expects.'
PROJECT = {
    'pricing.py': 'def total(prices):\n    raise NotImplementedError\n',
    'test_pricing.py': ('import unittest\n\nfrom pricing import total\n\n\nclass TotalTest(unittest.TestCase):\n'
                        '    def test_sum(self):\n        self.assertEqual(total([1, 2]), 3)\n'),
    **{'util/%s.py' % n: 'def helper_%s():\n    return "%s"\n' % (n, n) for n in 'abcdef'},
}
# When the background index build finishes: before the prompt reaches the engine, after the engine handled it but
# before GuardAdapter.begin returns (the Phase 25 trial 1 race), or only at the first mutating check's wait.
BUILD_ORDERS = ('before', 'between', 'later')


def build_tool():
    spec = importlib.util.spec_from_file_location('build_candidate_app_for_phase25', ROOT / 'scripts/build_candidate_app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guard_adapter():
    backend_path()
    from pebble.guard import VENDOR, GuardAdapter
    if VENDOR not in sys.path:
        sys.path.insert(0, VENDOR)  # as GuardAdapter does, so the engine's index can be held back before one exists
    return GuardAdapter


def drift_refusals(scratch, order):
    """R10's trajectory against the checkout's GuardAdapter and vendored engine, in process, with the index build held
    back to `order`: the fix to pricing.py, then six helpers the task does not reach. Returns the refusals."""
    adapter = guard_adapter()
    from pebble.policy import Workspace
    from agentguard.repo.index import RepoIndex
    project = scratch / order / 'project'
    for name, text in PROJECT.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text)
    held, original_async = [], RepoIndex.build_async
    if order != 'before':
        RepoIndex.build_async = lambda self: held.append(self) or self
    saved_home = os.environ.get('AGENTGUARD_HOME')  # GuardAdapter points it at its own home for this process
    guard = adapter(scratch / order / 'data')
    try:
        root, rid = Workspace(project).root, 'run-phase25'
        handle = guard.handle

        def release():
            for index in held:
                if not index.is_built:
                    index.build()

        def ordered(event, at, cid, **kwargs):
            if event == 'user_prompt' and order == 'before':
                assert guard.guard.workspace(guard.roots[(str(at), cid)]).index.ready(timeout=30), 'no index'
            result = handle(event, at, cid, **kwargs)
            if event == 'user_prompt' and order == 'between':
                release()
            return result
        guard.handle = ordered
        assert guard.begin(root, rid, GOAL)['state'] == 'started'
        if order == 'later':
            release()
        refused = []
        writes = [('pricing.py', 'def total(prices):\n    return sum(prices)\n')]
        writes += [('util/%s.py' % n, 'def helper_%s():\n    return "%s!"\n' % (n, n)) for n in 'abcdef']
        for name, content in writes:
            decision = guard.handle('pre_tool_use', root, rid, tool='Write', arguments={'file_path': name, 'content': content})
            if decision['action'] != 'allow':
                refused.append({'file': name, 'action': decision['action'],
                                'scope': 'that the task does not reach' in decision.get('reason', '')})
                continue
            (project / name).write_text(content)
            guard.stage(root, rid, name, content)
            guard.handle('post_tool_use', root, rid, tool='Write', arguments={'file_path': name, 'content': content},
                         result='{}')
        return refused
    finally:
        RepoIndex.build_async = original_async
        guard.close()
        if saved_home is None:
            os.environ.pop('AGENTGUARD_HOME', None)
        else:
            os.environ['AGENTGUARD_HOME'] = saved_home


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


class Campaign:
    def __init__(self, scratch):
        self.scratch, self.results = scratch, []

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

    def run(self):
        scratch = self.scratch

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)
        bca = build_tool()

        def configs():
            stand = type('Build', (), {})()
            stand.root, stand.out, stand.copy_mode = ROOT, ROOT / '.runtime/phase25-candidate-app', False
            home = str(Path.home())
            seen = {}
            for kind in ('release', 'test'):
                build = bca.Build.merge_config(stand, kind)['build']
                dist = build['frontendDist']
                assert not os.path.isabs(dist), (kind, 'frontendDist is absolute')
                text = json.dumps(build)
                assert str(ROOT) not in text and home not in text and '/' + 'Users/' not in text, (kind, 'a machine path')
                assert (ROOT / 'src-tauri' / dist).resolve() == (stand.out / 'frontend/dist').resolve(), (kind, dist)
                seen[kind] = 'relative'
            return {'frontend_dist': seen}
        self.check('release_merge_configs_embed_no_machine_path', configs)

        def strips():
            profile = tomllib.loads((ROOT / 'src-tauri/Cargo.toml').read_text())['profile']['release']
            assert profile.get('strip') in (True, 'symbols', 'debuginfo'), profile.get('strip')
            return {'strip': profile.get('strip')}
        self.check('release_profile_strips_the_shell', strips)

        def strip_tool():
            channel = bca.toolchain_channel((ROOT / bca.TOOLCHAIN_PIN).read_text())
            assert channel and bca.TOOLCHAIN.startswith(channel), (channel, bca.TOOLCHAIN)
            tool = (ROOT / '.runtime/toolchain/rustup/toolchains' / bca.TOOLCHAIN / 'lib/rustlib/aarch64-apple-darwin/bin'
                    / 'rust-objcopy')
            assert tool.is_file(), 'the pinned toolchain has no rust-objcopy'
            done = subprocess.run([str(tool), '--version'], capture_output=True, text=True, timeout=30)
            assert done.returncode == 0 and 'objcopy' in done.stdout, ('rust-objcopy does not run', done.returncode,
                                                                       done.stderr[-300:].replace(str(ROOT), '<checkout>'))
            return {'toolchain': bca.TOOLCHAIN, 'rust_objcopy': 'runs'}
        self.check('pinned_toolchain_strip_tool_runs', strip_tool)

        def guard():
            source = (ROOT / 'scripts/build_candidate_app.py').read_text()
            assert "Refused('machine_paths_in_shell'" in source and "Refused('machine_paths_in_stage'" in source
            needles = bca.machine_needles(ROOT, Path.home())
            leak = scratch / 'leak'
            leak.mkdir()
            (leak / 'pebble').write_bytes(b'\xcf\xfa\xed\xfe\x00' + str(ROOT).encode() + b'/frontend/dist\x00')
            found = bca.machine_path_scan(leak, needles)
            assert found['paths'] == ['pebble'], ('this machine\'s path was not found', found['paths'])
            other = scratch / 'other'
            other.mkdir()
            (other / 'pebble').write_bytes(b'\xcf\xfa\xed\xfe\x00/' + b'Users/ci-runner/work/lib.c\x00')
            counted = bca.machine_path_scan(other, needles)
            assert counted['paths'] == [] and counted['users_prefix'] >= 1, counted
            return {'this_machine_found': True, 'other_machine_counted': True}
        self.check('shell_guard_refuses_this_machines_paths_only', guard)

        def version():
            current = checkout_version()
            major = int(current.split('.')[0])
            assert major >= 1, current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_1_0_or_later', version)

        def grounding():
            seen = {}
            for order in BUILD_ORDERS:
                refused = drift_refusals(scratch / 'grounding', order)
                assert [(r['file'], r['action'], r['scope']) for r in refused] == [('util/f.py', 'challenge', True)], (
                    order, refused)
                seen[order] = 'challenged at util/f.py'
            return {'build_orders': seen}
        self.check('guard_grounds_the_task_whatever_the_index_build_order', grounding)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase25-')).resolve()
    started = time.monotonic()
    campaign = Campaign(scratch)
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
