"""Parent verifier: run fixed synthetic service suites and bind their raw evidence.

This host CLI is not an agent tool. It runs only checked-in validators against
disposable homes. Source, validator and complete candidate digests are checked
around every invocation so unbound legacy reports cannot become stale proof.
"""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    'runtime': 'validate_runtime.py', 'interaction': 'validate_interaction.py',
    'handoff': 'validate_handoff.py', 'compaction': 'validate_compaction.py',
    'search': 'validate_search.py', 'memory': 'validate_memory.py',
    'extensions': 'validate_extensions.py', 'mcp': 'validate_mcp.py',
    'auth': 'validate_connector_auth.py', 'content': 'validate_mcp_content.py',
    'phase6': 'validate_phase6.py', 'oauth-interop': 'validate_oauth_interop.py',
    'phase7': 'validate_phase7.py', 'phase8': 'validate_phase8.py',
    'phase8-interaction': 'validate_phase8_interaction.py', 'phase9': 'validate_phase9.py',
    'phase9-effects': 'validate_phase9_effects.py', 'phase9-git': 'validate_phase9_git.py',
    'phase10-migrations': 'validate_phase10_migrations.py',
    'phase10-security': 'validate_phase10_security.py',
    'phase11': 'validate_phase11.py',
    'phase11-upgrade': 'validate_phase11_upgrade.py',
    'phase12': 'validate_phase12.py',
    'phase12-upgrade': 'validate_phase12_upgrade.py',
    'phase13': 'validate_phase13.py',
    'phase13-upgrade': 'validate_phase13_upgrade.py',
    'phase14': 'validate_phase14.py',
    'phase14-upgrade': 'validate_phase14_upgrade.py',
    'phase15': 'validate_phase15.py',
    'phase15-upgrade': 'validate_phase15_upgrade.py',
    'phase16': 'validate_phase16.py',
    'phase16-upgrade': 'validate_phase16_upgrade.py',
    'phase17': 'validate_phase17.py',
    'phase17-upgrade': 'validate_phase17_upgrade.py',
    'phase18': 'validate_phase18.py',
    'phase19': 'validate_phase19.py',
    'phase19-upgrade': 'validate_phase19_upgrade.py',
    'phase20': 'validate_phase20.py',
    'phase20-faults': 'validate_phase20_faults.py',
    # Phase 21: release identity and HEIC end to end, source and frozen (V21-validator).
    'phase21': 'validate_phase21.py',
    # Phase 22: stream watchdog, reasoning summaries, usage, run_activity, the ~/.pebble data home and its Documents
    # copies, settings.json, the vault helper protocol, Pebble for Chrome and release 0.6.0 (v22-validator).
    'phase22': 'validate_phase22.py',
    # Phase 23 (hotfix 0.6.1): validate_phase22's campaign with the release identity taken from the checkout; it
    # supersedes phase22, which pins RELEASE = '0.6.0' (retired below).
    'phase23': 'validate_phase23.py',
    # Phase 24 (1.0): the fix tracks' checks, source and frozen (t24-validator's validate_phase24.py).
    'phase24': 'validate_phase24.py',
    'phase25': 'validate_phase25.py',  # the 1.0 release build carries no machine path; the guard grounds every task (Phase 25)
    'phase26': 'validate_phase26.py',  # the 1.0.1 release ships no component without its notice (Phase 26)
    'phase27': 'validate_phase27.py',  # 1.0.2 caches Claude requests; the Claude Code benchmark arm stays isolated (Phase 27)
    'phase28': 'validate_phase28.py',  # multi-step Claude tasks survive the thinking binding; the harness fixes (Phase 28)
    'phase29': 'validate_phase29.py',  # the benchmark's Claude Code CLI is pinned; an absent CLI is never an answer (Phase 29)
    'phase30': 'validate_phase30.py',  # 1.0.3 claims only what it does; evaluation never runs the app's identity (Phase 30)
    'phase31': 'validate_phase31.py',  # 1.0.4's full workspace: full screen, no Dock in the way, back to the pebble (Phase 31)
    'phase32': 'validate_phase32.py',  # faster tests that prove the same; a benchmark harness fair to every product (Phase 32)
    'phase33': 'validate_phase33.py',  # 1.1.0's Stealth Lockdown: one switch, hidden from capture, never taking focus (Phase 33)
    'phase34': 'validate_phase34.py',  # 1.2.0's precision: checks off the project, tests stay yours, the retry race (Phase 34)
}
PHASES = ('phase10', 'phase11', 'phase12', 'phase13', 'phase14', 'phase15', 'phase16', 'phase17', 'phase18', 'phase19',
          'phase20', 'phase21', 'phase22', 'phase23', 'phase24', 'phase25', 'phase26', 'phase27', 'phase28', 'phase29',
          'phase30', 'phase31', 'phase32', 'phase33', 'phase34')
# Historical suites that can no longer pass, kept unchanged as historical evidence, with the phase from
# which they leave the default campaign and the reason. Each stays runnable with --suite.
# Phase 32 (T4): suites that measure time or the machine, run alone after the pooled ones under --jobs N.
# phase18 samples CPU, memory and connections of a live service; phase24 bounds a first request at 1.0 s
# (Phase 31 trial 1 measured 1.11 s once under load). More join only from a measured flake (serial_register
# rules apply: a reason each).
SERIAL_SUITES = frozenset({'phase18', 'phase24'})
RETIRED = {
    name: ('phase14', 'Its fixture holds a synthetic temporal memory that expired at 2026-10-12 12:48 and that '
                      'Pebble correctly deletes; the grader requires the row forever. Superseded by the '
                      'time-aware phase14-upgrade checks on the same untouched fixtures (user decision, 2026-10-12).')
    for name in ('phase11-upgrade', 'phase12-upgrade', 'phase13-upgrade')}
RETIRED['phase14-upgrade'] = (
    'phase15', 'Its subject is the upgrade to epoch 5 (it asserts SCHEMA_EPOCH == FROM_EPOCH + 1 == 5); the product '
               'is now at epoch 6. Superseded by the time-aware phase15-upgrade checks, which upgrade the epoch-5 '
               'fixture and the untouched epoch 1-4 fixtures to the current epoch (upgrade-validator lifecycle, '
               'user decision 2026-10-12).')
RETIRED['phase22'] = (
    'phase23', 'It pins RELEASE = \'0.6.0\' (the Phase 22 release), so it cannot pass once the release moves on. '
               'Superseded by phase23 (validate_phase23.py): the same 46 checks, scenarios and fixtures, with the '
               'release identity taken from the checkout\'s pebble.VERSION (the person\'s decision, 2026-10-14).')


def default_suites(phase):
    """Every suite whose namespace is not newer than `phase`, minus suites retired by then."""
    rank = PHASES.index(phase)
    return [name for name in SUITES
            if not any(name.startswith(later) for later in PHASES[rank + 1:])
            # A retirement applies from its phase on; one whose phase is not registered (a test's cut table) does not.
            and not (name in RETIRED and RETIRED[name][0] in PHASES and rank >= PHASES.index(RETIRED[name][0]))]


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def sources():
    names = set()
    # scripts/fixtures data (Phase 17): validators read and report these inputs, so the snapshot binds them too.
    # Phase 22: Pebble for Chrome (shipped in the bundle by build_candidate_app.py) and the vault helper's C source.
    # Phase 27: the benchmark harness (benchmarks/), whose Claude Code driver validate_phase27 reads.
    for directory, suffixes in [('backend', {'.py'}), ('src', {'.vue', '.ts', '.mts', '.mjs', '.css'}), ('scripts', {'.py'}),
                                ('scripts/fixtures', {'.json', '.md'}), ('benchmarks', {'.py'}),
                                ('browser-extension', {'.js', '.mjs', '.json', '.html', '.css', '.svg', '.png'}),
                                ('src-tauri/helpers', {'.c', '.h'})]:
        names.update(path.relative_to(ROOT).as_posix() for path in (ROOT / directory).rglob('*')
                     if path.is_file() and not path.is_symlink() and path.suffix in suffixes)
    names.update(['backend/requirements.txt', 'package.json', 'package-lock.json', 'src-tauri/src/main.rs',
                  'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock', 'src-tauri/tauri.conf.json',
                  # Phase 17: the native command manifest and capability grants (both changed this phase).
                  'src-tauri/build.rs', 'src-tauri/capabilities/main.json',
                  # Phase 24 (V1-048, V1-076): the relocatable spec, the hashed lock the build environment is made
                  # from, the developer requirements and the Rust toolchain pin. The epoch-8 fixture builder's
                  # checked-in copy (backend/tests/fixtures/phase19/) is covered as backend Python.
                  'build/pebble-backend.spec', 'backend/requirements.lock', 'backend/requirements-dev.txt',
                  'rust-toolchain.toml'])
    # Phase 32: which tests run alone (the serial register) and pytest's own settings decide what a full run executes,
    # and validate_phase32 reads both; covered wherever they exist, as a clean clone of an older tree has neither.
    names.update(rel for rel in ('backend/tests/serial_register.json', 'backend/tests/pytest.ini') if (ROOT / rel).is_file())
    # Phase 33: the menu-bar glyphs the shell compiles in (include_bytes!), which validate_phase33 reads.
    names.update(rel for rel in ('src-tauri/icons/tray-off.png', 'src-tauri/icons/tray-on.png') if (ROOT / rel).is_file())
    # The private inputs earlier phases' proofs hash: the old absolute-path spec (Phase 10) and the epoch-8 fixture
    # builder whose IDs and rows phase19-upgrade imports and reports (Phase 19). Covered wherever they exist, so the
    # parent's checkout keeps every earlier snapshot and a clean clone, which has no .runtime, still verifies.
    names.update(rel for rel in ('.runtime/pebble-backend.spec', '.runtime/phase19-baseline/build_epoch8_fixture.py')
                 if (ROOT / rel).is_file())
    # Phase 22 (k22-identity): the pinned vault helper and its manifest, once scripts/build_vault_helper.py made them.
    names.update(rel for rel in ('src-tauri/resources/helpers/pebble-vault', 'src-tauri/resources/helpers/pebble-vault.json')
                 if (ROOT / rel).is_file())
    # Phase 24 (t24-validator): inputs validate_phase24 reads that no rule above covers: the golden 4K/8K request
    # fixture (four_k_requests_unchanged), the generated third-party notices and t24-publish's installer and allowlist.
    names.update(rel for rel in ('backend/tests/fixtures/phase24/agent_golden_requests.json',
                                 'src-tauri/resources/licenses/THIRD-PARTY-NOTICES.txt', 'install.sh',
                                 'publish/manifest.json',
                                 # Phase 24 (parent, trial 2): validate_phase24's data-folder checks read it too;
                                 # unbound, the campaign refused its report as stale source.
                                 'docs/DATA_FOLDERS.md',
                                 # Phase 30: validate_phase30 reads the public text and the zoom permission's schema.
                                 'README.md', 'docs/USER_GUIDE.md', 'CHANGELOG.md', 'src-tauri/gen/schemas/desktop-schema.json')
                 if (ROOT / rel).is_file())
    return {name: sha(ROOT / name) for name in sorted(names)}


def candidate_manifest(executable):
    sys.path.insert(0, str(ROOT / 'backend'))
    from pebble.release_readiness import Root, bundle_digest, bundle_entries
    root = Root(str(executable.parent), 'candidate_root')
    try:
        entries, links, problems = bundle_entries(root)
        if problems:
            raise RuntimeError('Candidate bundle is unsafe or incomplete: ' + str(problems[:4]))
    finally:
        root.close()
    return {'bundle_sha256': bundle_digest(entries),
            'algorithm': 'SHA256 of sorted-key compact JSON file entries in relative-path order', 'files': entries}


def run_validator(command, log):
    with log.open('w') as sink:
        process = subprocess.Popen(command, cwd=ROOT, stdout=sink, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        collected = False
        try:
            result = process.wait(timeout=240)
            collected = True
            return result
        finally:
            # A timeout leaves this leader unreaped, pinning its group ID.
            # Never signal after wait collected it: that ID can be reused.
            # Completed validators own their fixture teardown and report any
            # uncertain/orphan observation; their scratch evidence is retained.
            if not collected:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--suite', action='append', choices=list(SUITES))
    parser.add_argument('--kind', action='append', choices=['source', 'frozen'])
    parser.add_argument('--jobs', type=int, default=1, help='Phase 32: suites run this many at a time (1: in order)')
    parser.add_argument('--serial-suite', action='append', choices=list(SUITES),
                        help='Phase 32: also run this suite alone, after the pooled ones')
    parser.add_argument('--phase', choices=list(PHASES), default='phase10',
                        help='Choose a fresh proof namespace; existing reports are never overwritten.')
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error('--jobs must be 1-8')
    candidate = args.candidate.resolve(strict=True)
    snapshot = json.loads(args.snapshot.read_text())
    if sources() != snapshot:
        raise RuntimeError('Source no longer matches the parent-tested snapshot.')
    bundle = candidate_manifest(candidate)
    executable_sha = sha(candidate)
    raw_root = ROOT / ('.runtime/' + args.phase + '-raw-service')
    raw_root.mkdir(mode=0o700, exist_ok=True)
    log_root = ROOT / ('.runtime/' + args.phase + '-service-logs')
    log_root.mkdir(mode=0o700, exist_ok=True)
    produced = []
    suites = args.suite or default_suites(args.phase)
    later = PHASES[PHASES.index(args.phase) + 1:]
    for name in suites:
        newer = next((phase for phase in later if name.startswith(phase)), None)
        if newer:
            raise RuntimeError('Phase ' + newer[5:] + ' proof requires its own namespace.')
    jobs = [(suite, kind) for suite in suites for kind in (args.kind or ['source', 'frozen'])]

    def run_job(suite, kind):
        validator = ROOT / 'scripts' / SUITES[suite]
        name = 'harness-' + args.phase + '-' + suite + '-' + kind + '.json'
        raw = raw_root / name
        log = log_root / (name[:-5] + '.log')
        output = ROOT / 'reports' / name
        for path in (raw, log, output):
            if path.is_symlink():
                raise RuntimeError('Refused symbolic verification output.')
        if output.exists():
            raise RuntimeError('Verification output already exists; preserve it or select a new phase.')
        if sources() != snapshot or candidate_manifest(candidate) != bundle:
            raise RuntimeError('Verification inputs changed before a service invocation.')
        command = [sys.executable, str(validator), '--report', str(raw)]
        if kind == 'frozen':
            command += ['--backend', str(candidate)]
        exit_code = run_validator(command, log)
        if exit_code:
            raise RuntimeError('Validator failed: ' + suite + '/' + kind + '; see owned log ' + str(log))
        if sources() != snapshot or candidate_manifest(candidate) != bundle:
            raise RuntimeError('Verification inputs changed during a service invocation.')
        from pebble.release_readiness import parse_json
        report = parse_json(raw.read_bytes(), 'raw_service_report')
        checks = report.get('checks')
        if (report.get('passed') is not True or report.get('kind') != kind or
                not isinstance(checks, list) or not checks or
                not all(isinstance(c, str) and c.strip() for c in checks) or len(set(checks)) != len(checks)):
            raise RuntimeError('Validator evidence is incomplete: ' + suite + '/' + kind)
        if kind == 'frozen' and report.get('backend_sha256') != executable_sha:
            raise RuntimeError('Validator ran a different frozen executable.')
        if any(snapshot.get(p) != digest for p, digest in report.get('source_sha256', {}).items()):
            raise RuntimeError('Validator reported stale source.')
        report['source_sha256'] = snapshot
        report['parent_invocation'] = {
            'schema': 1, 'suite': suite, 'kind': kind,
            'driver_sha256': sha(Path(__file__)), 'validator_sha256': sha(validator),
            'raw_report_sha256': sha(raw), 'snapshot_sha256': sha(args.snapshot),
            'candidate_bundle_sha256': bundle['bundle_sha256'], 'exit_code': exit_code,
            'inputs_unchanged_before_and_after': True,
            'completed_at': datetime.now(timezone.utc).isoformat(),
        }
        output.write_text(json.dumps(report, indent=2) + '\n')
        done = {'suite': suite, 'kind': kind, 'checks': len(checks), 'report': name, 'sha256': sha(output)}
        print(json.dumps(done), flush=True)
        return done

    # Phase 32 (T4): with --jobs N the suites outside SERIAL_SUITES run N at a time; the suites in it (and any named
    # with --serial-suite) run afterwards, one at a time, with nothing beside them. Each job keeps every check above.
    # The first failure stops new jobs; the ones already running finish, and the failure is raised.
    serial = SERIAL_SUITES | set(args.serial_suite or ())
    pooled = [job for job in jobs if job[0] not in serial] if args.jobs > 1 else []
    alone = [job for job in jobs if job not in pooled]
    if pooled:
        from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(run_job, *job) for job in pooled]
            finished, pending = wait(futures, return_when=FIRST_EXCEPTION)
            failed = [f for f in finished if f.exception()]
            if failed:
                for future in pending:
                    future.cancel()
                wait(pending)
                raise failed[0].exception()
            produced.extend(f.result() for f in futures)
    for job in alone:
        produced.append(run_job(*job))
    print(json.dumps({'reports': len(produced), 'checks': sum(p['checks'] for p in produced)}), flush=True)


if __name__ == '__main__':
    main()
