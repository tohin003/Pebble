"""Release readiness on real temporary evidence trees: fail-closed local gates,
production gates that no JSON claim can satisfy, and no privileged or mutating calls."""

import hashlib
import importlib.util
import json
import os
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest

from pebble import release_readiness as rr

pytestmark = pytest.mark.skipif(not rr.anchored_io_supported() or not hasattr(os, 'symlink'),
                                reason='Descriptor-anchored no-follow reads need macOS or Linux.')

REPO = Path(__file__).resolve().parents[2]
REVIEW_DATE, REPORT_DATE = '2026-05-01T12:00:00+00:00', '2026-05-01T11:00:00+00:00'
SECRET = 'sk-fixture-SECRET-must-not-appear-7f3a'
PHASE10_COVERAGE = {
    'include': [['backend', ['*.py']], ['src', ['*.vue', '*.ts', '*.mjs', '*.css']], ['scripts', ['*.py']]],
    'fixed': ['backend/requirements.txt', 'package.json', 'package-lock.json', 'src-tauri/src/main.rs',
              'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock', 'src-tauri/tauri.conf.json', '.runtime/pebble-backend.spec'],
    'skip_dirs': ['__pycache__'],
}
TOOLS = {'codesign': '/usr/bin/codesign', 'spctl': '/usr/sbin/spctl',
         'stapler': '/Library/Developer/CommandLineTools/usr/bin/stapler'}
DEVELOPER_ID = ('Executable=/x\nIdentifier=com.pebble.local\nFormat=app bundle with Mach-O thin (arm64)\n'
                'CodeDirectory v=20500 size=1234 flags=0x10000(runtime) hashes=28+7 location=embedded\n'
                'Authority=Developer ID Application: Secret Person (ABCDE12345)\n'
                'Authority=Developer ID Certification Authority\nAuthority=Apple Root CA\n'
                'Timestamp=Oct 11, 2026 at 10:00:00\nTeamIdentifier=ABCDE12345\n')
ADHOC = ('Executable=/x\nIdentifier=pebble\n'
         'CodeDirectory v=20400 size=500 flags=0x20002(adhoc,linker-signed) hashes=10+0 location=embedded\n'
         'Signature=adhoc\nTeamIdentifier=not set\n')
GOOD_NATIVE = {
    'verify': (0, '/x: valid on disk\n/x: satisfies its Designated Requirement\n'),
    'display': (0, DEVELOPER_ID),
    'assess': (0, '/x: accepted\nsource=Notarized Developer ID\norigin=Developer ID Application: Secret Person (ABCDE12345)\n'),
    'stapled_ticket': (0, 'Processing: /x\nThe validate action worked!\n'),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dump(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def legacy_manifest(package):
    """The Phase 9 finalizer's walk reproduced independently: pathlib order, regular
    files and followed file links, directory links not descended."""
    found = []
    for folder, directories, files in os.walk(package):
        for name in directories + files:
            path = Path(folder) / name
            if path.is_file():
                found.append(path)
    found.sort(key=lambda path: PurePosixPath(path.relative_to(package).as_posix()))
    entries = [{'path': path.relative_to(package).as_posix(), 'bytes': len(path.read_bytes()),
                'sha256': sha(path.read_bytes())} for path in found]
    return entries, sha(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode())


def evaluation_report(evidence='live_model', outcomes=('passed', 'passed', 'non_delegated'), **summary):
    """The per-arm shape written by backend/pebble/evaluation.py."""
    counts = Counter(outcomes)
    body = {'schema': 'pebble.harness_evaluation/1', 'evidence': evidence, 'arm': 'delegated',
            'quality_claim': 'descriptive results only',
            'cases': [{'case_id': f'case-{n}', 'arm': 'delegated', 'outcome': outcome, 'success': outcome == 'passed'}
                      for n, outcome in enumerate(outcomes)],
            'summary': {'selected': len(outcomes), 'passed': counts['passed'], 'not_passed': len(outcomes) - counts['passed'],
                        'outcomes': dict(sorted(counts.items()))},
            'signature': 'f' * 64}
    body['summary'].update(summary)
    return body


def codes(result, gate):
    return {issue['code'] for issue in result['gates'][gate]['issues']}


def every_code(result):
    return set().union(*(codes(result, gate) for gate in result['gates']))


def status(result, gate):
    return result['gates'][gate]['status']


def malformed(result):
    return {item['code'] for item in result['malformed']}


def load_cli():
    spec = importlib.util.spec_from_file_location('check_harness_release', REPO / 'scripts/check_harness_release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class World:
    """A complete, internally consistent synthetic evidence set hashed with plain hashlib."""

    def __init__(self, base):
        self.base, self.source, self.candidate, self.reports = base, base / 'source', base / 'candidate', base / 'reports'
        self.sources = {
            'backend/main.py': b'from pebble import core\n',
            'backend/pebble/__init__.py': b'',
            'backend/pebble/core.py': b'ANSWER = 42  # ' + SECRET.encode() + b'\n',
            'backend/requirements.txt': b'synthetic==0\n',
            'scripts/validate_world.py': b'print("synthetic")\n',
            'scripts/world_tool.py': b'print("tool")\n',
            'src/App.vue': b'<template><main /></template>\n',
            'src-tauri/tauri.conf.json': dump({'bundle': {'macOS': {'signingIdentity': '-'}}}),
        }
        for rel, data in self.sources.items():
            write(self.source / rel, data)
        self.exe = b'\xcf\xfa\xed\xfe' + bytes(range(256)) * 64
        write(self.candidate / 'pebble-backend', self.exe, 0o755)
        write(self.candidate / '_internal/base_library.zip', bytes((i * 7) % 256 for i in range(4096)))
        write(self.candidate / '_internal/data/model.bin', bytes(range(255, -1, -1)) * 32)
        write(self.candidate / '_internal/data/catalogue.json', b'{"items": [1, 2, 3]}\n')
        write(self.candidate / '_internal/certifi/cacert.pem', b'-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n')
        write(self.candidate / '_internal/Lib.framework/Versions/1/Lib', b'\x00library\x00' * 100)
        os.symlink('1', self.candidate / '_internal/Lib.framework/Versions/Current')
        os.symlink('Versions/Current/Lib', self.candidate / '_internal/Lib.framework/Lib')
        os.symlink('Lib.framework/Versions/1/Lib', self.candidate / '_internal/Lib')
        self.checks = ['synthetic check alpha', 'synthetic check beta ' + SECRET]
        self.log = (b'collecting ... collected 5 items\n'
                    + b''.join(b'backend/tests/test_world.py::test_%d PASSED [%3d%%]\n' % (n, 20 * (n + 1)) for n in range(5))
                    + b'============================== 5 passed in 0.42s ==============================\n')
        write(base / 'logs/full-tests.log', self.log)

    def record(self, coverage=PHASE10_COVERAGE):
        self.snapshot = {rel: sha(data) for rel, data in sorted(self.sources.items())}
        self.snapshot_sha = sha(dump(self.snapshot))
        self.entries, self.bundle = legacy_manifest(self.candidate)
        write(self.base / 'manifest.json', dump({'bundle_sha256': self.bundle, 'algorithm': 'legacy', 'files': self.entries}))
        self.service = {}
        for kind in ('source', 'frozen'):
            name = f'harness-world-{kind}.json'
            data = dump(self.report(kind))
            write(self.reports / name, data)
            self.service[name] = {'passed': True, 'checks': len(self.checks), 'kind': kind, 'date': REPORT_DATE, 'sha256': sha(data)}
        self.review = {
            'date': REVIEW_DATE, 'scope': 'Synthetic test review ' + SECRET, 'passed': True,
            'backend_tests': 5, 'backend_tests_skipped': 0,
            'backend_outcomes': {'collected': 5, 'passed': 5, 'skipped': 0, 'xfailed': 0, 'xpassed': 0, 'skipped_ids': []},
            'phase10_suite_passes': {'test_world': 5},
            'service_checks': 2 * len(self.checks), 'service_reports': self.service,
            'candidate': {'path': '.runtime/harness-candidate/pebble-backend/pebble-backend', 'executable_sha256': sha(self.exe),
                          'bundle_sha256': self.bundle, 'files': len(self.entries),
                          'bytes': sum(entry['bytes'] for entry in self.entries)},
            'source_sha256': self.snapshot, 'source_snapshot_sha256': self.snapshot_sha,
            'validator_sha256': {'scripts/validate_world.py': self.snapshot['scripts/validate_world.py']},
            'phase10_modules': {'backend/pebble/core.py': self.snapshot['backend/pebble/core.py']},
            'verification_sha256': {'logs/full-tests.log': sha(self.log)},
        }
        if coverage is not None:
            self.review['source_coverage'] = coverage
        self.save()
        return self

    def report(self, kind):
        # Shaped like the parent's source-bound reports from scripts/verify_harness_candidate.py.
        return {'date': REPORT_DATE, 'kind': kind, 'passed': True, 'checks': list(self.checks),
                'synthetic_provider_attempts': 2, 'request_metadata': [{'prompt': SECRET}],
                'source_sha256': dict(self.snapshot),
                'backend_sha256': sha(self.exe) if kind == 'frozen' else self.snapshot['backend/main.py'],
                'parent_invocation': {'schema': 1, 'suite': 'harness-world', 'kind': kind, 'snapshot_sha256': self.snapshot_sha,
                                      'candidate_bundle_sha256': self.bundle, 'exit_code': 0,
                                      'inputs_unchanged_before_and_after': True}}

    def save(self):
        write(self.base / 'review.json', dump(self.review))

    def rewrite(self, name, mutate, rebind=True):
        path = self.reports / name
        report = json.loads(path.read_bytes())
        mutate(report)
        data = dump(report)
        write(path, data)
        if rebind:
            self.review['service_reports'][name].update(sha256=sha(data), date=report['date'])
            self.save()

    def args(self, **overrides):
        args = dict(review=self.base / 'review.json', reports_root=self.reports, source_root=self.source,
                    candidate_root=self.candidate, candidate_manifest=self.base / 'manifest.json',
                    artifact_root=self.base, test_log=self.base / 'logs/full-tests.log')
        args.update(overrides)
        return args

    def run(self, **overrides):
        return rr.check_release(**self.args(**overrides))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / 'evidence').record()


# ---- Complete evidence and compatibility --------------------------------------------------------


def test_complete_local_evidence_is_local_ready_but_never_production_ready(world):
    result = world.run()
    assert result['exit_code'] == rr.EXIT_LOCAL_READY and result['evidence'] == 'well_formed'
    assert result['readiness'] == {'local_source': True, 'local_candidate': True, 'production': False}
    assert result['production_ready'] is False and result['fixture'] is False
    assert all(status(result, gate) == 'passed' for gate in rr.LOCAL_GATES)
    for gate in rr.PRODUCTION_GATES:
        rendered = result['gates'][gate]
        assert rendered['status'] == 'unverified' and rendered['reason'] and rendered['missing_inputs']
        assert rendered['version'] == rr.GATE_VERSIONS[gate] >= 1
    observed = result['gates']['candidate_bundle']['observed']
    assert observed['bundle_sha256'] == world.bundle and observed['files'] == len(world.entries)
    assert observed['links'] == 3 and observed['directory_links'] == 1
    assert result['gates']['source_freshness']['observed']['coverage'] == 'declared'
    assert result['gates']['source_services']['observed']['reports_without_source_binding'] == 0
    assert result['gates']['backend_tests']['observed']['test_log'] == 'cross_checked'
    assert all(value is True for value in result['history'].values())
    assert result['freshness'] == {'source_matches_snapshot': True, 'candidate_matches_evidence': True}
    assert result['gates']['macos_distribution_signing']['observed']['build_config'] == {
        'state': 'read', 'configured_signing': 'adhoc'}


def test_walker_reproduces_the_phase9_bundle_algorithm_with_binary_assets_and_aliases(world):
    with rr.Root(world.candidate, 'candidate') as root:
        entries, links, problems = rr.bundle_entries(root)
    assert problems == [] and entries == world.entries and rr.bundle_digest(entries) == world.bundle
    assert rr.legacy_entries(str(world.candidate)) == world.entries
    assert {link['path']: link['kind'] for link in links} == {
        '_internal/Lib': 'file', '_internal/Lib.framework/Lib': 'file',
        '_internal/Lib.framework/Versions/Current': 'directory'}
    paths = [entry['path'] for entry in entries]
    assert '_internal/certifi/cacert.pem' in paths  # public CA bundles are not key material
    assert '_internal/Lib.framework/Versions/Current/Lib' not in paths  # directory aliases are not descended


def test_preserved_phase9_history_is_integrity_not_current_source_proof():
    review_path = REPO / 'reports/harness-phase9-review.json'
    manifest_path = REPO / 'reports/harness-phase9-candidate-manifest.json'
    if not review_path.is_file() or not manifest_path.is_file():
        pytest.skip('Phase 9 history is not present in this checkout.')
    review = rr.read_review(review_path.read_bytes())
    manifest = rr.read_manifest(manifest_path.read_bytes())
    assert rr.bundle_digest(manifest['files']) == manifest['bundle_sha256'] == review['candidate']['bundle_sha256']
    assert len(manifest['files']) == review['candidate']['files']
    assert sha(dump(review['source_sha256'])) == review['source_snapshot_sha256']
    result = rr.check_release(review=review_path, reports_root=REPO / 'reports', candidate_manifest=manifest_path)
    assert result['history']['reports_match_review'] is True and result['history']['manifest_matches_review'] is True
    # Historical source reports carry no source binding; they cannot prove the current source ran.
    assert result['gates']['source_services']['observed']['reports_without_source_binding'] > 0
    assert status(result, 'source_services') != 'passed'
    assert result['readiness']['local_source'] is False and result['production_ready'] is False
    assert result['exit_code'] == rr.EXIT_NOT_READY


def test_path_rules_refuse_unsafe_and_private_paths():
    assert rr.path_problem('_internal/certifi/cacert.pem', bundle=True) is None
    assert rr.path_problem('certs/server.pem') == 'private_path'
    for bad in ('', '/abs', '../x', 'a/../b', 'a//b', 'a/./b', 'a/', 'a\\b', 'C:/x', 'a\x00b', 'a\nb', 'x' * 513, '\udcff'):
        assert rr.path_problem(bad, bundle=True) == 'unsafe_path', repr(bad)
    for private in ('.env', 'config/.env.local', '.ssh/id', 'a/credentials/x', 'Secrets/x', 'k/signing.P12',
                    'id_ed25519', 'x.keychain-db', '.git/config'):
        assert rr.path_problem(private, bundle=True) == 'private_path', private
    assert rr.collision_key('Backend/Main.py') == rr.collision_key('backend/main.py')


# ---- Source coverage and freshness --------------------------------------------------------------


def test_phase10_coverage_includes_every_script_and_skips_pycache(world):
    assert rr.read_coverage(PHASE10_COVERAGE) == {
        'name': 'declared', 'include': (('backend', ('*.py',)), ('src', ('*.vue', '*.ts', '*.mjs', '*.css')), ('scripts', ('*.py',))),
        'fixed': tuple(rr.DEFAULT_COVERAGE['fixed']), 'skip_dirs': ('__pycache__',)}
    write(world.source / 'backend/pebble/__pycache__/stale.py', b'ignored\n')
    assert world.run()['exit_code'] == rr.EXIT_LOCAL_READY
    write(world.source / 'scripts/new_release_tool.py', b'print(1)\n')
    declared = world.run()
    unlisted = [issue for issue in declared['gates']['source_freshness']['issues'] if issue['code'] == 'source_unlisted']
    assert unlisted and unlisted[0]['subjects'] == ['scripts/new_release_tool.py']
    assert declared['exit_code'] == rr.EXIT_NOT_READY
    del world.review['source_coverage']
    world.save()
    default = world.run()  # the Phase 9 default covers only scripts/validate_*.py
    assert status(default, 'source_freshness') == 'passed'
    assert default['gates']['source_freshness']['observed']['coverage'] == 'pebble-default-v1'


@pytest.mark.parametrize('coverage', [
    {'include': []},
    {'include': [['../outside', ['*.py']]]},
    {'include': [['backend', ['nested/*.py']]]},
    {'include': [['backend', ['*.py']]], 'skip_dirs': ['..']},
    {'include': [['backend', ['*.py']]], 'fixed': ['.env']},
    {'include': [['backend', ['*.py']]], 'unknown': True},
])
def test_invalid_declared_coverage_is_malformed(world, coverage):
    world.review['source_coverage'] = coverage
    world.save()
    result = world.run()
    assert result['exit_code'] == rr.EXIT_MALFORMED and result['production_ready'] is False


def test_stale_source_fails_without_losing_historical_integrity(world):
    write(world.source / 'backend/pebble/core.py', b'ANSWER = 43\n')
    write(world.source / 'backend/pebble/new_module.py', b'pass\n')
    (world.source / 'src/App.vue').unlink()
    result = world.run()
    assert result['exit_code'] == rr.EXIT_NOT_READY and result['readiness']['local_source'] is False
    assert {'source_modified', 'source_unlisted', 'source_missing'} <= codes(result, 'source_freshness')
    assert all(value is True for value in result['history'].values())
    assert result['freshness']['source_matches_snapshot'] is False


def test_linked_source_files_and_directories_fail_freshness(world):
    core = world.source / 'backend/pebble/core.py'
    copy = world.base / 'core-copy.py'
    copy.write_bytes(core.read_bytes())
    core.unlink()
    os.symlink(copy, core)
    os.symlink(world.base, world.source / 'src/linked')
    result = world.run()
    assert result['exit_code'] == rr.EXIT_NOT_READY and 'source_symlink' in codes(result, 'source_freshness')
    subjects = next(issue for issue in result['gates']['source_freshness']['issues'] if issue['code'] == 'source_symlink')['subjects']
    assert {'backend/pebble/core.py', 'src/linked'} <= set(subjects)


# ---- Service reports ----------------------------------------------------------------------------


def _historical(report):
    report.pop('source_sha256')
    report.pop('parent_invocation')
    report['backend_sha256'] = None


def _backend_only(report):
    report.pop('source_sha256')
    report.pop('parent_invocation')


@pytest.mark.parametrize('mutate,expected,code', [
    (_historical, 'unverified', None),
    (_backend_only, 'unverified', None),
    (lambda report: report.update(source_sha256={}), 'unverified', None),
    (lambda report: report['source_sha256'].update({'backend/pebble/core.py': sha(b'old')}), 'failed', 'report_source_stale'),
    (lambda report: report['source_sha256'].update({'backend/pebble/ghost.py': sha(b'x')}), 'failed', 'report_source_unlisted'),
    (lambda report: report['parent_invocation'].update(snapshot_sha256=sha(b'old')), 'failed', 'report_invocation_snapshot_stale'),
    (lambda report: report['parent_invocation'].update(candidate_bundle_sha256=sha(b'old')), 'failed', 'report_invocation_candidate_stale'),
    (lambda report: report['parent_invocation'].update(inputs_unchanged_before_and_after=False), 'failed', 'report_inputs_changed_during_run'),
    (lambda report: report.update(backend_sha256=sha(b'old main')), 'failed', 'source_backend_stale'),
])
def test_unbound_or_stale_source_reports_cannot_prove_current_source(world, mutate, expected, code):
    world.rewrite('harness-world-source.json', mutate)
    result = world.run()
    assert status(result, 'source_services') == expected and result['exit_code'] == rr.EXIT_NOT_READY
    if code:
        assert code in codes(result, 'source_services')
    else:
        observed = result['gates']['source_services']['observed']
        assert observed['unbound_source_reports'] == ['harness-world-source.json']
        assert result['gates']['source_services']['missing_inputs']
    assert result['history']['reports_match_review'] is True


def _drop_frozen_mode(world):
    del world.review['service_reports']['harness-world-frozen.json']
    world.review['service_checks'] = len(world.checks)
    (world.reports / 'harness-world-frozen.json').unlink()
    world.save()


@pytest.mark.parametrize('mutate,code', [
    (lambda w: w.rewrite('harness-world-frozen.json', lambda r: r.update(backend_sha256=sha(b'other build'))), 'frozen_backend_mismatch'),
    (lambda w: w.rewrite('harness-world-frozen.json', lambda r: r.pop('backend_sha256')), 'frozen_backend_missing'),
    (lambda w: w.rewrite('harness-world-source.json', lambda r: r.update(backend_sha256=sha(w.exe))), 'source_report_ran_candidate'),
    (lambda w: w.rewrite('harness-world-frozen.json', lambda r: r.update(checks=['synthetic check alpha', 'another check'])),
     'suite_checks_differ_between_modes'),
    (lambda w: (w.rewrite('harness-world-source.json', lambda r: r.update(approved_source_sha256='a' * 64)),
                w.rewrite('harness-world-frozen.json', lambda r: r.update(approved_source_sha256='b' * 64))),
     'suite_approved_source_differs'),
    (_drop_frozen_mode, 'suite_missing_frozen_mode'),
])
def test_frozen_reports_bind_to_the_candidate_and_both_modes_pair(world, mutate, code):
    mutate(world)
    result = world.run()
    assert code in codes(result, 'source_services') | codes(result, 'frozen_services')
    assert result['exit_code'] == rr.EXIT_NOT_READY and result['readiness']['local_candidate'] is False


def test_required_suites_must_exist_in_both_modes(world):
    result = world.run(require_suites=['harness-world', 'phase10-security'])
    assert 'required_suite_missing' in codes(result, 'source_services') & codes(result, 'frozen_services')
    assert result['exit_code'] == rr.EXIT_NOT_READY
    assert world.run(require_suites=['Bad Suite'])['exit_code'] == rr.EXIT_MALFORMED


def _later_date(report):
    report['date'] = '2026-05-02T00:00:00+00:00'


@pytest.mark.parametrize('mutate,code', [
    (lambda w: w.rewrite('harness-world-source.json', lambda r: r.update(note='tampered'), rebind=False), 'report_digest_mismatch'),
    (lambda w: w.rewrite('harness-world-source.json', lambda r: r['checks'].append('extra check')), 'report_check_count_mismatch'),
    (lambda w: (w.reports / 'harness-world-frozen.json').unlink(), 'report_missing'),
    (lambda w: (w.review.update(service_checks=w.review['service_checks'] + 1), w.save()), 'service_check_total_mismatch'),
    (lambda w: w.rewrite('harness-world-frozen.json', _later_date), 'report_after_review'),
    (lambda w: w.rewrite('harness-world-source.json', lambda r: r.update(passed=False)), 'report_not_passed'),
    (lambda w: w.rewrite('harness-world-frozen.json', lambda r: r.update(failures=['one'])), 'report_lists_failures'),
    (lambda w: w.rewrite('harness-world-frozen.json', lambda r: r.update(kind='source')), 'report_kind_mismatch'),
    (lambda w: (w.review['validator_sha256'].update({'scripts/validate_world.py': sha(b'x')}), w.save()),
     'review_digest_disagrees_with_snapshot'),
    (lambda w: (w.review.update(source_snapshot_sha256=sha(b'x')), w.save()), 'snapshot_digest_unreproducible'),
    (lambda w: (w.review.update(passed=False), w.save()), 'review_not_passed'),
])
def test_tampered_or_inconsistent_reports_fail_local_readiness(world, mutate, code):
    mutate(world)
    result = world.run()
    assert code in every_code(result)
    assert result['exit_code'] == rr.EXIT_NOT_READY and result['production_ready'] is False


def test_symlinked_inputs_and_roots_are_malformed(world):
    link = world.base / 'reports-link'
    os.symlink(world.reports, link)
    assert 'root_is_symlink' in malformed(world.run(reports_root=link))
    review_link = world.base / 'review-link.json'
    os.symlink(world.base / 'review.json', review_link)
    assert 'input_is_symlink' in malformed(world.run(review=review_link))
    name = world.reports / 'harness-world-frozen.json'
    copy = world.base / 'frozen-copy.json'
    copy.write_bytes(name.read_bytes())
    name.unlink()
    os.symlink(copy, name)
    result = world.run()
    assert 'report_symlink' in malformed(result) and result['exit_code'] == rr.EXIT_MALFORMED
    assert all(status(result, gate) == 'failed' for gate in rr.LOCAL_GATES)


# ---- Backend outcomes ---------------------------------------------------------------------------


@pytest.mark.parametrize('outcomes,code', [
    ({'passed': 4, 'skipped': 1, 'skipped_ids': ['backend/tests/test_world.py::test_4']}, 'tests_skipped'),
    ({'passed': 4, 'failed': 1}, 'tests_failed'),
    ({'passed': 4, 'errors': 1}, 'tests_errors'),
    ({'passed': 4, 'xfailed': 1}, 'tests_xfailed'),
    ({'collected': 6}, 'outcomes_do_not_cover_collection'),
    ({'collected': 0, 'passed': 0}, 'no_tests_collected'),
])
def test_backend_outcomes_must_cover_every_collected_test(world, outcomes, code):
    world.review['backend_outcomes'].update(outcomes)
    world.save()
    result = world.run()
    assert code in codes(result, 'backend_tests') and status(result, 'backend_tests') == 'failed'
    assert result['exit_code'] == rr.EXIT_NOT_READY


def test_suite_passes_floors_and_outcome_schema(world):
    assert 'below_required_minimum' in codes(world.run(min_backend_tests=6), 'backend_tests')
    world.review['phase10_suite_passes']['test_world'] = 0
    world.save()
    assert 'suite_without_passes' in codes(world.run(), 'backend_tests')
    del world.review['backend_outcomes']['xpassed']
    world.save()
    assert world.run()['exit_code'] == rr.EXIT_MALFORMED


def _bind_log(world, log):
    write(world.base / 'logs/full-tests.log', log)
    world.review['verification_sha256']['logs/full-tests.log'] = sha(log)
    world.save()


def test_pytest_log_is_cross_checked_and_bound_to_the_review(world):
    _bind_log(world, world.log.replace(b'5 passed in', b'4 passed, 1 failed in'))
    assert {'test_log_outcome_mismatch'} <= codes(world.run(), 'backend_tests')
    _bind_log(world, world.log.replace(b'5 passed in', b'5 passed, 2 deselected in'))
    assert 'test_log_deselected' in codes(world.run(), 'backend_tests')
    write(world.base / 'logs/full-tests.log', world.log + b'\n')  # not the reviewed log
    result = world.run()
    assert 'test_log_not_bound_to_review' in codes(result, 'backend_tests')
    assert 'artifact_modified' in codes(result, 'evidence_integrity')


# ---- Candidate ----------------------------------------------------------------------------------


def _flip_byte(world):
    path = world.candidate / '_internal/data/model.bin'
    data = bytearray(path.read_bytes())
    data[100] ^= 1
    path.write_bytes(bytes(data))


def _retarget_alias(world):
    link = world.candidate / '_internal/Lib'
    link.unlink()
    os.symlink('base_library.zip', link)


@pytest.mark.parametrize('mutate,code', [
    (_flip_byte, 'candidate_file_modified'),
    (lambda w: (w.candidate / '_internal/data/catalogue.json').unlink(), 'candidate_file_missing'),
    (lambda w: write(w.candidate / '_internal/data/extra.bin', b'\x00\x01'), 'candidate_file_unlisted'),
    (_retarget_alias, 'candidate_file_modified'),
    (lambda w: write(w.candidate / 'pebble-backend', b'\xcf\xfa\xed\xfe rebuilt', 0o755), 'candidate_executable_mismatch'),
    (lambda w: (w.candidate / 'pebble-backend').chmod(0o644), 'candidate_executable_not_executable'),
])
def test_candidate_drift_fails_local_candidate_readiness(world, mutate, code):
    mutate(world)
    result = world.run()
    assert code in codes(result, 'candidate_bundle')
    assert result['readiness'] == {'local_source': True, 'local_candidate': False, 'production': False}
    assert result['exit_code'] == rr.EXIT_NOT_READY and result['freshness']['candidate_matches_evidence'] is False


def _manifest_entry(world, **changes):
    manifest = json.loads((world.base / 'manifest.json').read_bytes())
    manifest['files'].append({**manifest['files'][0], **changes})
    write(world.base / 'manifest.json', dump(manifest))


def _manifest_first(world, **changes):
    manifest = json.loads((world.base / 'manifest.json').read_bytes())
    manifest['files'][0].update(changes)
    write(world.base / 'manifest.json', dump(manifest))


@pytest.mark.parametrize('mutate', [
    lambda w: _manifest_entry(w),
    lambda w: _manifest_entry(w, path=w.entries[0]['path'].upper()),
    lambda w: _manifest_entry(w, path='../escape.bin'),
    lambda w: _manifest_entry(w, path='/absolute.bin'),
    lambda w: _manifest_entry(w, path='a//b.bin'),
    lambda w: _manifest_entry(w, path='./x.bin'),
    lambda w: _manifest_entry(w, path='_internal/.env'),
    lambda w: _manifest_entry(w, path='keys/signing.p12'),
    lambda w: _manifest_entry(w, path='bad\\name'),
    lambda w: _manifest_entry(w, path='nul\x00name'),
    lambda w: _manifest_first(w, mode=493),
    lambda w: _manifest_first(w, bytes=True),
    lambda w: (w.base / 'manifest.json').write_bytes(b'{"files": [], "files": [], "bundle_sha256": "' + b'a' * 64 + b'"}'),
])
def test_manifest_entries_must_be_exact_unique_and_safe(world, mutate):
    mutate(world)
    result = world.run()
    assert result['exit_code'] == rr.EXIT_MALFORMED and result['evidence'] == 'malformed'
    assert all(status(result, gate) == 'failed' for gate in rr.LOCAL_GATES)


@pytest.mark.parametrize('kind,code', [
    ('escape', 'candidate_link_escapes_root'),
    ('absolute', 'candidate_link_absolute'),
    ('loop', 'candidate_link_loop'),
    ('dangling', 'candidate_dangling_link'),
    ('fifo', 'candidate_special_file'),
    ('private', 'candidate_private_path'),
])
def test_unsafe_candidate_entries_fail_without_reading_them(world, kind, code):
    internal = world.candidate / '_internal'
    outside = write(world.base / 'outside.txt', b'outside ' + SECRET.encode())
    if kind == 'escape':
        os.symlink('../../outside.txt', internal / 'escape')
    elif kind == 'absolute':
        os.symlink(str(outside), internal / 'absolute')
    elif kind == 'loop':
        os.symlink('loop_b', internal / 'loop_a')
        os.symlink('loop_a', internal / 'loop_b')
    elif kind == 'dangling':
        os.symlink('missing.bin', internal / 'dangling')
    elif kind == 'fifo':
        os.mkfifo(internal / 'pipe')
    else:
        write(internal / '.env', SECRET.encode())
    result = world.run()
    assert code in codes(result, 'candidate_bundle')
    assert result['exit_code'] == rr.EXIT_NOT_READY and SECRET not in json.dumps(result)


def test_case_colliding_spellings_are_refused_on_either_file_system(world):
    """Platform-specific by necessity, never skipped. A case-sensitive file system can
    hold two physical spellings, and the walker must refuse them. A case-insensitive
    volume (default APFS) cannot: both spellings name one inode, so no double-file
    containment is claimed there. Instead the walker must see a single entry, and a
    manifest listing both spellings must be refused as a duplicate."""
    internal = world.candidate / '_internal'
    write(world.base / 'outside.txt', b'outside ' + SECRET.encode())
    original, variant = internal / 'data' / 'catalogue.json', internal / 'DATA' / 'catalogue.json'
    if not (internal / 'DATA').exists():  # case-sensitive: create a second physical spelling
        write(variant, b'case variant\n')
        assert not os.path.samefile(original, variant) and original.read_bytes() != variant.read_bytes()
        result = world.run()
        assert 'candidate_path_collision' in codes(result, 'candidate_bundle')
        assert result['exit_code'] == rr.EXIT_NOT_READY
    else:  # case-insensitive: the two spellings address the same file
        assert os.path.samefile(original, variant) and original.read_bytes() == variant.read_bytes()
        with rr.Root(world.candidate, 'candidate') as root:
            entries, _, problems = rr.bundle_entries(root)
        assert problems == []
        assert [e['path'] for e in entries if e['path'].casefold() == '_internal/data/catalogue.json'] == [
            '_internal/data/catalogue.json']
        manifest = json.loads((world.base / 'manifest.json').read_bytes())
        listed = next(entry for entry in manifest['files'] if entry['path'] == '_internal/data/catalogue.json')
        manifest['files'].append({**listed, 'path': '_internal/DATA/catalogue.json'})
        write(world.base / 'manifest.json', dump(manifest))
        result = world.run()
        assert result['exit_code'] == rr.EXIT_MALFORMED and 'duplicate_entry' in malformed(result)
    assert SECRET not in json.dumps(result)


def test_unsafe_on_disk_names_are_redacted(world):
    write(world.candidate / '_internal' / 'odd\nname', b'x')
    result = world.run()
    issue = next(issue for issue in result['gates']['candidate_bundle']['issues'] if issue['code'] == 'candidate_unsafe_path')
    assert issue['subjects'] and all(item.startswith('sha256:') for item in issue['subjects'])
    assert 'odd\\nname' not in json.dumps(result)


# ---- Production gates ---------------------------------------------------------------------------


def test_fake_signing_quality_and_audit_claims_are_ignored(world, tmp_path):
    world.review.update(signed=True, notarized=True, production_ready=True, score=0.99, human_audit={'passed': True},
                        independent_audit='Approved by an external reviewer', installed_verified=True)
    world.review['candidate']['signature'] = 'Developer ID Application: Example (ABCDE12345)'
    world.save()
    evaluation = write(tmp_path / 'live.json', dump(evaluation_report()))
    result = world.run(evaluation_reports=[evaluation], signing_identity_status='available')
    assert result['exit_code'] == rr.EXIT_LOCAL_READY and result['production_ready'] is False
    assert all(status(result, gate) == 'unverified' for gate in rr.PRODUCTION_GATES)
    claims = {(item['document'], item['key']) for item in result['ignored_claims']}
    assert {('review', 'signed'), ('review', 'notarized'), ('review', 'production_ready'), ('review', 'score'),
            ('review', 'human_audit'), ('review', 'independent_audit'), ('review', 'installed_verified'),
            ('review', 'candidate.signature')} <= claims
    assert result['gates']['macos_distribution_signing']['observed']['operator_reported_identity'] == 'available'
    assert 'ABCDE12345' not in json.dumps(result)


def test_evaluation_reports_are_observed_but_never_satisfy_quality(world, tmp_path):
    def quality(*bodies):
        paths = [write(tmp_path / f'evaluation-{n}.json', dump(body)) for n, body in enumerate(bodies)]
        result = world.run(evaluation_reports=paths)
        assert result['production_ready'] is False
        return result['gates']['real_model_quality']

    live = quality(evaluation_report())
    assert live['status'] == 'unverified' and 'release quality threshold' in live['missing_inputs']
    assert live['observed']['kinds'] == {'live_model_recorded': 1}
    assert live['observed']['outcomes'] == {'non_delegated': 1, 'passed': 2} and live['observed']['passed_cases'] == 2
    assert live['observed']['ledger_signatures_present_unverified'] == 1
    every_outcome = quality(evaluation_report(outcomes=tuple(sorted(rr.EVALUATION_OUTCOMES))))
    assert every_outcome['observed']['kinds'] == {'live_model_recorded': 1}
    scripted = quality(evaluation_report('scripted_fixture', ('passed',)))
    assert scripted['status'] == 'unverified' and 'scripted' in scripted['reason']
    assert quality(evaluation_report(selected=4))['status'] == 'failed'
    assert 'evaluation_counts_inconsistent' in {issue['code'] for issue in quality(evaluation_report(passed=3))['issues']}
    unrecognized = quality({**evaluation_report(), 'schema': 'other/1'},
                           {**evaluation_report(), 'evidence': 'real'},
                           evaluation_report(outcomes=('passed', 'skipped')))
    assert unrecognized['status'] == 'unverified' and unrecognized['observed']['kinds'] == {'unrecognized': 3}
    absent = world.run()['gates']['real_model_quality']
    assert absent['status'] == 'unverified' and absent['missing_inputs'] == ['live-model evaluation report']


def owned_app(base, embed):
    app = base / 'build' / 'Pebble.app'
    write(app / 'Contents/Info.plist', b'<plist/>')
    write(app / 'Contents/MacOS/Pebble', b'\xcf\xfa\xed\xfe shell')
    if embed is not None:
        write(app / 'Contents/Resources/resources/backend/pebble-backend', embed, 0o755)
    return app


def fake_runner(calls=None, **overrides):
    outputs = {**GOOD_NATIVE, **overrides}

    def run(argv):
        if calls is not None:
            calls.append(list(argv))
        verb = {'--verify': 'verify', '--display': 'display', '--assess': 'assess', 'validate': 'stapled_ticket'}[argv[1]]
        code, text = outputs[verb]
        return {'exit': code, 'timed_out': code is None, 'truncated': False, 'text': text}
    return run


@pytest.mark.parametrize('overrides,embed,missing_tool,expected,code', [
    ({}, 'candidate', None, 'passed', None),
    ({'display': (0, ADHOC)}, 'candidate', None, 'failed', 'adhoc_signature'),
    ({'display': (1, '/x: code object is not signed at all\n')}, 'candidate', None, 'failed', 'unsigned'),
    ({'display': (0, DEVELOPER_ID.replace('flags=0x10000(runtime)', 'flags=0x0(none)'))}, 'candidate', None, 'failed',
     'hardened_runtime_missing'),
    ({'display': (0, DEVELOPER_ID.replace('Timestamp=', 'Signed Time='))}, 'candidate', None, 'failed', 'secure_timestamp_missing'),
    ({'verify': (1, '/x: invalid signature\n')}, 'candidate', None, 'failed', 'codesign_verification_failed'),
    ({'assess': (3, '/x: rejected\nsource=no usable signature\n')}, 'candidate', None, 'failed', 'gatekeeper_rejected'),
    ({'assess': (0, '/x: accepted\nsource=Developer ID\n')}, 'candidate', None, 'failed', 'not_notarized'),
    ({'stapled_ticket': (65, 'Processing: /x\nThe validate action failed! Error 65.\n')}, 'candidate', None, 'failed',
     'ticket_not_stapled'),
    ({}, 'other', None, 'failed', 'app_does_not_embed_candidate'),
    ({}, None, None, 'failed', 'app_does_not_embed_candidate'),
    ({}, 'candidate', 'stapler', 'unverified', None),
    ({'assess': (None, '')}, 'candidate', None, 'unverified', None),
])
def test_native_signing_needs_developer_id_notarization_staple_and_the_candidate(world, tmp_path, overrides, embed,
                                                                                missing_tool, expected, code):
    app = owned_app(tmp_path, {'candidate': world.exe, 'other': b'not the candidate', None: None}[embed])
    tools = {**TOOLS, **({missing_tool: None} if missing_tool else {})}
    calls = []
    result = world.run(native_app=app, platform='darwin', runner=fake_runner(calls, **overrides), tools=tools)
    gate = result['gates']['macos_distribution_signing']
    assert gate['status'] == expected
    if code:
        assert code in {issue['code'] for issue in gate['issues']}
    # Other production gates stay unverified, so even a Developer ID pass is not production readiness.
    assert result['production_ready'] is False and result['exit_code'] == rr.EXIT_LOCAL_READY
    assert all(argv[-1] == os.path.realpath(app) for argv in calls)
    text = json.dumps(result)
    assert 'Secret Person' not in text and 'ABCDE12345' not in text


def test_native_checks_need_macos_and_an_explicit_app(world, tmp_path):
    def refuse(argv):
        raise AssertionError('native tools must not run')
    app = owned_app(tmp_path, world.exe)
    linux = world.run(native_app=app, platform='linux', runner=refuse, tools=TOOLS)['gates']['macos_distribution_signing']
    assert linux['status'] == 'unverified' and linux['observed']['platform_supported'] is False
    absent = world.run(runner=refuse)['gates']['macos_distribution_signing']
    assert absent['status'] == 'unverified' and absent['missing_inputs'][0].startswith('--native-app')


def test_native_command_lines_are_fixed_read_only_and_bounded(monkeypatch):
    app = '/private/tmp/build/Pebble.app'
    argvs = rr.native_argv(TOOLS, app)
    allowed = {'--verify', '--deep', '--strict', '--verbose=2', '--display', '--verbose=4', '--assess', '--type',
               'execute', 'validate'}
    forbidden = {'--sign', '-s', '--force', '-f', '--remove-signature', 'staple', '--add', '--master-disable',
                 '--global-disable', '--enable', '--disable', '--remove', 'sudo', 'open', 'install'}
    for argv in argvs.values():
        assert argv[0] in TOOLS.values() and argv[-1] == app
        assert set(argv[1:-1]) <= allowed and not set(argv) & forbidden
    assert rr.native_argv(dict.fromkeys(TOOLS), app) == dict.fromkeys(argvs)
    captured, killed = {}, []

    class Process:
        def __init__(self, argv, **kwargs):
            captured.update(argv=argv, **kwargs)
            read, write_end = os.pipe()
            os.write(write_end, captured.get('payload', b'x' * 300))
            os.close(write_end)
            self.stdout, self.pid, self.returncode = os.fdopen(read, 'rb'), 424242, None

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, 'Popen', Process)
    monkeypatch.setattr(os, 'killpg', lambda pid, sig: killed.append(pid))
    outcome = rr.run_bounded(['/usr/bin/codesign', '--verify', app], seconds=5, limit=100)
    assert outcome['truncated'] and outcome['text'] == 'x' * 100 and killed == [424242]
    assert captured['stdin'] is subprocess.DEVNULL and captured['start_new_session'] is True
    assert captured['env'] == rr.NATIVE_ENV and captured['cwd'] == '/' and captured['close_fds'] is True
    assert 'shell' not in captured
    complete = rr.run_bounded(['/usr/bin/codesign', '--verify', app], seconds=5, limit=1000)
    assert complete == {'exit': 0, 'timed_out': False, 'truncated': False, 'text': 'x' * 300}
    for bad in (['codesign', '--verify', app], ['/usr/bin/codesign'], ['/usr/bin/codesign', 'a\x00b'],
                ['/usr/bin/codesign'] + ['x'] * 9):
        with pytest.raises(ValueError):
            rr.run_bounded(bad)


def test_native_app_must_be_an_owned_candidate_bundle(tmp_path):
    app = owned_app(tmp_path, None)
    assert rr.owned_app_path(app) == os.path.realpath(app)
    link = tmp_path / 'Linked.app'
    os.symlink(app, link)
    empty = tmp_path / 'Empty.app'
    empty.mkdir()
    cases = {'/Applications/Pebble.app': 'native_app_installed_location',
             '/System/Applications/Calculator.app': 'native_app_installed_location',
             os.path.expanduser('~/Applications/Pebble.app'): 'native_app_installed_location',
             str(link): 'native_app_is_symlink', str(tmp_path / 'build'): 'native_app_not_bundle',
             str(tmp_path / 'Missing.app'): 'native_app_missing', 'bad\nname.app': 'native_app_invalid',
             str(empty): 'native_app_not_bundle'}
    for value, code in cases.items():
        with pytest.raises(rr.EvidenceError) as caught:
            rr.owned_app_path(value)
        assert caught.value.code == code, value


# ---- Parsing, bounds and host support -----------------------------------------------------------


def test_strict_json_rejects_duplicates_non_finite_values_bom_and_excess(monkeypatch):
    cases = {b'{"a": 1, "a": 2}': 'duplicate_json_key', b'{"a": {"b": 1, "b": 1}}': 'duplicate_json_key',
             b'{"a": NaN}': 'non_finite_json_number', b'{"a": -Infinity}': 'non_finite_json_number',
             b'\xef\xbb\xbf{}': 'json_byte_order_mark', b'{"a": "\xff"}': 'json_invalid',
             b'{"a": ' + b'9' * 5000 + b'}': 'json_invalid', b'[' * 60 + b']' * 60: 'json_too_complex',
             b'{"a": [1,]}': 'json_invalid'}
    for data, code in cases.items():
        with pytest.raises(rr.EvidenceError) as caught:
            rr.parse_json(data, 'probe')
        assert caught.value.code == code and caught.value.subject == 'probe'
    monkeypatch.setattr(rr, 'MAX_JSON_BYTES', 10)
    with pytest.raises(rr.EvidenceError) as caught:
        rr.parse_json(b'{"a": "0123456789"}', 'probe')
    assert caught.value.code == 'json_too_large'


@pytest.mark.parametrize('mutate', [
    lambda r: r['backend_outcomes'].pop('collected'),
    lambda r: r.update(backend_tests='5'),
    lambda r: r['source_sha256'].update({'.env': 'a' * 64}),
    lambda r: r['source_sha256'].update({'../escape.py': 'a' * 64}),
    lambda r: r['source_sha256'].update({'BACKEND/MAIN.PY': r['source_sha256']['backend/main.py']}),
    lambda r: r['service_reports']['harness-world-source.json'].update(kind='installed'),
    lambda r: r['candidate'].update(executable_sha256='zz'),
    lambda r: r.update(date='2026-05-01T12:00:00'),
    lambda r: r['verification_sha256'].update({'keys/release.p12': 'a' * 64}),
    lambda r: r.update(phase10_suite_passes={'test_world': -1}),
])
def test_review_schema_violations_are_malformed(world, mutate):
    mutate(world.review)
    world.save()
    result = world.run()
    assert result['exit_code'] == rr.EXIT_MALFORMED and result['production_ready'] is False
    assert all(status(result, gate) == 'failed' for gate in rr.LOCAL_GATES)


def test_duplicate_review_keys_are_malformed(world):
    data = dump(world.review).replace(b'"passed": true,', b'"passed": true, "passed": true,', 1)
    (world.base / 'review.json').write_bytes(data)
    result = world.run()
    assert 'duplicate_json_key' in malformed(result) and result['exit_code'] == rr.EXIT_MALFORMED


def test_artifacts_need_their_root_and_detect_changes(world):
    result = world.run(artifact_root=None)
    assert status(result, 'evidence_integrity') == 'unverified'
    assert '--artifact-root' in result['gates']['evidence_integrity']['missing_inputs']
    assert result['exit_code'] == rr.EXIT_NOT_READY
    write(world.base / 'logs/full-tests.log', b'rewritten\n')
    assert 'artifact_modified' in codes(world.run(test_log=None), 'evidence_integrity')


@pytest.mark.parametrize('key,gate', [('source_root', 'source_freshness'), ('candidate_root', 'candidate_bundle'),
                                      ('candidate_manifest', 'candidate_bundle')])
def test_missing_explicit_inputs_leave_their_gate_unverified(world, key, gate):
    result = world.run(**{key: None})
    assert status(result, gate) == 'unverified' and result['gates'][gate]['missing_inputs']
    assert result['exit_code'] == rr.EXIT_NOT_READY


def test_unsupported_host_leaves_local_gates_unverified(world, monkeypatch):
    monkeypatch.setattr(rr, 'anchored_io_supported', lambda: False)
    result = world.run()
    assert result['evidence'] == 'well_formed' and result['exit_code'] == rr.EXIT_NOT_READY
    assert all(status(result, gate) == 'unverified' for gate in rr.LOCAL_GATES)


def test_results_are_deterministic_and_metadata_only(world):
    first, second = world.run(), world.run()
    assert first == second
    text = json.dumps(first)
    for forbidden in (str(world.base), SECRET, 'ANSWER', 'synthetic check alpha', 'signingIdentity'):
        assert forbidden not in text
    write(world.source / 'backend/pebble/core.py', b'changed ' + SECRET.encode())
    failing = json.dumps(world.run())
    assert 'backend/pebble/core.py' in failing and SECRET not in failing


def test_no_subprocess_runs_without_an_explicit_native_app(world, monkeypatch):
    def refuse(*unused, **also_unused):
        raise AssertionError('no subprocess may run')
    for name in ('Popen', 'run', 'call', 'check_call', 'check_output'):
        monkeypatch.setattr(subprocess, name, refuse)
    monkeypatch.setattr(os, 'system', refuse)
    assert world.run()['exit_code'] == rr.EXIT_LOCAL_READY
    assert rr.run_fixture()['exit_code'] == rr.EXIT_LOCAL_READY


# ---- Report output, fixture and CLI -------------------------------------------------------------


def test_report_is_written_atomically_and_never_over_evidence(world, tmp_path):
    result = world.run()
    out = tmp_path / 'out'
    out.mkdir()
    target = out / 'harness-phase10-release.json'
    rr.write_report(target, result)
    assert target.read_bytes() == (json.dumps(result, indent=2, sort_keys=True) + '\n').encode()
    rr.write_report(target, result)  # an earlier readiness result may be replaced
    assert sorted(path.name for path in out.iterdir()) == ['harness-phase10-release.json']
    review_before = (world.base / 'review.json').read_bytes()
    link = out / 'linked.json'
    os.symlink(target, link)
    refused = [world.base / 'review.json', world.reports / 'harness-world-source.json', out / 'harness-phase9-review.json',
               world.candidate / 'readiness.json', out / 'notes.txt', out / '.hidden.json', link,
               tmp_path / 'absent-dir' / 'result.json']
    for path in refused:
        with pytest.raises(rr.EvidenceError):
            rr.write_report(path, result, protected_trees=(world.candidate,))
    assert (world.base / 'review.json').read_bytes() == review_before
    assert not (world.candidate / 'readiness.json').exists()
    assert sorted(path.name for path in out.iterdir()) == ['harness-phase10-release.json', 'linked.json']


def test_fixture_mode_stays_synthetic_and_never_production_ready(world, tmp_path):
    result = rr.run_fixture()
    assert result['fixture'] is True and result['production_ready'] is False
    assert result['exit_code'] == rr.EXIT_LOCAL_READY
    assert all(status(result, gate) == 'passed' for gate in rr.LOCAL_GATES)
    assert all(status(result, gate) == 'unverified' for gate in rr.PRODUCTION_GATES)
    assert result['gates']['real_model_quality']['observed']['kinds'] == {'scripted_fixture': 1}
    assert result['gates']['source_freshness']['observed']['coverage'] == 'declared'
    assert {'signed', 'notarized', 'production_ready', 'score'} <= {item['key'] for item in result['ignored_claims']}
    assert result == rr.run_fixture()  # deterministic across fresh temporary trees
    marked = world.run(fixture=True)
    assert marked['fixture'] is True and marked['production_ready'] is False
    cli = load_cli()
    assert cli.main(['--fixture']) == rr.EXIT_LOCAL_READY
    report = tmp_path / 'fixture-readiness.json'
    assert cli.main(['--fixture', '--report', str(report)]) == rr.EXIT_LOCAL_READY
    assert json.loads(report.read_text())['production_ready'] is False


def test_cli_exit_codes_distinguish_malformed_evidence_from_not_ready(world, capsys):
    cli = load_cli()
    args = ['--review', str(world.base / 'review.json'), '--reports-root', str(world.reports),
            '--source-root', str(world.source), '--candidate-root', str(world.candidate),
            '--candidate-manifest', str(world.base / 'manifest.json'), '--artifact-root', str(world.base),
            '--test-log', str(world.base / 'logs/full-tests.log'), '--require-suite', 'harness-world',
            '--signing-identity-status', 'absent']
    assert cli.main(args) == rr.EXIT_LOCAL_READY
    printed = json.loads(capsys.readouterr().out)
    assert printed['production_ready'] is False
    assert printed['gates']['macos_distribution_signing']['observed']['operator_reported_identity'] == 'absent'
    write(world.source / 'backend/pebble/core.py', b'stale\n')
    assert cli.main(args) == rr.EXIT_NOT_READY
    (world.base / 'review.json').write_bytes(b'{"passed": true, "passed": true}')
    assert cli.main(args) == rr.EXIT_MALFORMED
    with pytest.raises(SystemExit) as usage:
        cli.main(['--reports-root', str(world.reports)])
    assert usage.value.code == 2
