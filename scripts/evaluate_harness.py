"""Harness evaluation CLI (Phases 10a, 15 and 18). Parent-run; see docs/HARNESS_EVALUATION.md.

  corpus [--corpus V]               print a public corpus manifest (no prompts or oracles)
  live --profile-metadata FILE      run cases through owned Apps on the selected ChatGPT sign-in route
  compare A B --ledger DIR          paired same-revision comparison: (solo, delegated) or (solo, adaptive)
  compare-revisions A B --ledger    labelled cross-revision listing: same arm, different source; descriptive only
  economics [--corpus V]            scripted minimal paths per arm (fixture route, no model request)
  plan [--claim-set pilot|claim]    dry run: exactly which live runs a claim set needs (no request)

Phase 18 paired campaign (pebble against baseline on corpus 18a.2):

  campaign-plan [--claim-set pilot|claim|claim2]   dry-run plan with its seeded schedule (no request)
  register PLAN --ledger DIR        pre-register the plan, its thresholds digest and planned cell count
  block --plan PLAN --block N --invocation K --profile-metadata FILE --ledger DIR --report FILE --confirm-live
                                    run one planned invocation: each task in both arms, back to back
  registry-head --ledger DIR        print the verified registry head to witness outside the ledger
  analyse PLAN REPORT... --ledger DIR [--witness ENTRIES:HEAD]...
                                    offline intention-to-treat analysis (no request)

Phase 24 (V1-015), the separate evaluation connection:

  eval-connection --home DIR --ledger DIR --metadata FILE --confirm-sign-in [--reconnect]
                                    one ChatGPT sign-in through an owned App in an evaluation home
                                    that is not the app's: its own connection id, Keychain record and
                                    host id. Writes the private metadata file and records the
                                    connection (salted fingerprints only) in the ledger.

Live requests count against the selected ChatGPT account's plan, so --confirm-live
is required. This script reads no credentials: the owned App's ChatGPT adapter
obtains them natively from the OS vault. The metadata file must be private and
hold only a profile (optionally with its registered chatgpt_host_id). economics,
plan, campaign-plan, register, registry-head and analyse never send a request.
live and block run only the evaluation connection recorded in their ledger, and
refuse any connection or host id the installed app holds (its settings are read
read-only: profile ids and the host id, never a message or credential), so a
live run never refreshes the installed app's ChatGPT grant.

Exit codes: 0 completed, 2 refused or invalid input, 3 harness source drift,
4 stopped on an auth failure (reconnect the evaluation connection), 5 block stopped
as invalid_infrastructure or on route/effort drift.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from pebble.eval_registry import EvidenceRegistry, RegistryRefused  # noqa: E402
from pebble.evaluation import (ARMS, CLAIM_SETS, CLAIM_SETS_18, CORPORA, CORPORA_18, CORPUS_VERSION,  # noqa: E402
                               EVALUATION_CONNECTION_FILE, EvaluationError,
                               EvidenceLedger, EvidenceRefused, analyse_campaign, campaign_plan, compare_reports,
                               compare_revisions, corpus_manifest, installed_app_homes, installed_connection_identities,
                               live_plan, load_live_metadata, planned_cell_count, record_evaluation_connection,
                               require_evaluation_connection, run_evaluation, run_paired_block, scripted_economics,
                               validate_host_id, validate_live_profile, verify_plan)

SIGN_IN_SECONDS = 330  # chatgpt.ChatGPT expires a pending sign-in after 300 s
AUTH_STOPPED = ('Stopped: the evaluation connection\'s ChatGPT sign-in failed. Reconnect it with '
                '`scripts/evaluate_harness.py eval-connection --reconnect`; the installed app\'s sign-in was not used.')


def _write_new(path, data):
    # Exclusive creation: earlier evidence is never overwritten.
    with Path(path).open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + '\n')


def _read_report(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
        raise EvidenceRefused(['report_unreadable'])
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        raise EvidenceRefused(['report_unreadable']) from None


def _names(value):
    return [item.strip() for item in value.split(',')] if value else None


def _parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    corpus = commands.add_parser('corpus', help='print a public corpus manifest')
    corpus.add_argument('--corpus', choices=CORPORA, default=CORPUS_VERSION)
    live = commands.add_parser('live', help='evaluate the selected ChatGPT sign-in route through owned Apps')
    live.add_argument('--profile-metadata', type=Path, required=True, help='private metadata file (no credentials)')
    live.add_argument('--arm', choices=ARMS, required=True)
    live.add_argument('--corpus', choices=CORPORA, default=CORPUS_VERSION)
    live.add_argument('--cases', help='comma-separated case IDs (default: the whole corpus)')
    live.add_argument('--max-steps', type=int)
    live.add_argument('--max-seconds', type=int)
    live.add_argument('--wall-seconds', type=int)
    live.add_argument('--total-wall-seconds', type=int)
    live.add_argument('--ledger', type=Path, required=True, help='private evidence ledger directory (mode 0700)')
    live.add_argument('--report', type=Path, required=True, help='new public report file')
    live.add_argument('--resources', type=Path, help='App resources directory (tokenizer cache)')
    live.add_argument('--scratch', type=Path, help='parent directory for owned disposable scratch')
    live.add_argument('--confirm-live', action='store_true', help='acknowledge plan usage on the selected account')
    compare = commands.add_parser('compare', help='compare one solo report with one delegated or adaptive report')
    compare.add_argument('reports', nargs=2, type=Path)
    compare.add_argument('--ledger', type=Path, required=True)
    compare.add_argument('--report', type=Path, help='new comparison file')
    revisions = commands.add_parser('compare-revisions', help='descriptive same-arm listing across two sources')
    revisions.add_argument('reports', nargs=2, type=Path, metavar=('BASELINE', 'CANDIDATE'))
    revisions.add_argument('--ledger', type=Path, required=True)
    revisions.add_argument('--report', type=Path, help='new listing file')
    economics = commands.add_parser('economics', help='scripted minimal paths per arm; no model request')
    economics.add_argument('--corpus', choices=CORPORA, default=CORPUS_VERSION)
    economics.add_argument('--cases', help='comma-separated case IDs (default: the whole corpus)')
    economics.add_argument('--arms', help='comma-separated arms (default: all)')
    economics.add_argument('--max-steps', type=int)
    economics.add_argument('--max-seconds', type=int)
    economics.add_argument('--scratch', type=Path, help='parent directory for owned disposable scratch')
    economics.add_argument('--report', type=Path, help='new economics file')
    plan = commands.add_parser('plan', help='dry run: the live runs a claim set needs; no request')
    plan.add_argument('--claim-set', choices=sorted(CLAIM_SETS), default='pilot')
    plan.add_argument('--corpora', help='comma-separated corpus versions (default: all)')
    plan.add_argument('--arms', help='comma-separated arms (default: all)')
    plan.add_argument('--repetitions', type=int)
    plan.add_argument('--max-steps', type=int)
    plan.add_argument('--max-seconds', type=int)
    plan.add_argument('--report', type=Path, help='new plan file')
    campaign = commands.add_parser('campaign-plan', help='Phase 18: dry-run paired pebble/baseline plan; no request')
    campaign.add_argument('--claim-set', choices=sorted(CLAIM_SETS_18), default='pilot')
    campaign.add_argument('--blocks', type=int, help='planned blocks (default: the claim set)')
    campaign.add_argument('--report', type=Path, help='new plan file')
    register = commands.add_parser('register', help='Phase 18: pre-register a plan in the ledger registry')
    register.add_argument('plan', type=Path)
    register.add_argument('--ledger', type=Path, required=True, help='private evidence ledger directory (mode 0700)')
    block = commands.add_parser('block', help='Phase 18: run one planned invocation of a block')
    block.add_argument('--plan', type=Path, required=True)
    block.add_argument('--block', type=int, required=True)
    block.add_argument('--invocation', type=int, required=True)
    block.add_argument('--profile-metadata', type=Path, required=True, help='private metadata file (no credentials)')
    block.add_argument('--ledger', type=Path, required=True, help='private evidence ledger directory (mode 0700)')
    block.add_argument('--report', type=Path, required=True, help='new public block report file')
    block.add_argument('--resources', type=Path, help='App resources directory (tokenizer cache)')
    block.add_argument('--scratch', type=Path, help='parent directory for owned disposable scratch')
    block.add_argument('--confirm-live', action='store_true', help='acknowledge plan usage on the selected account')
    block.add_argument('--diagnostics', action='store_true',
                       help='also keep each cell\'s final reply and run error in a private 0600 file in the ledger')
    head = commands.add_parser('registry-head', help='Phase 18: the verified registry head, to witness outside')
    head.add_argument('--ledger', type=Path, required=True)
    analyse = commands.add_parser('analyse', help='Phase 18: offline analysis of every registered block report')
    analyse.add_argument('plan', type=Path)
    analyse.add_argument('reports', nargs='*', type=Path)
    analyse.add_argument('--ledger', type=Path, required=True)
    analyse.add_argument('--witness', action='append', default=[], metavar='ENTRIES:HEAD',
                         help='a registry head recorded outside the ledger (repeatable)')
    analyse.add_argument('--report', type=Path, help='new analysis file')
    connect = commands.add_parser('eval-connection', help='Phase 24: sign in the separate evaluation connection')
    connect.add_argument('--home', type=Path, required=True, help='evaluation home (never the installed app\'s)')
    connect.add_argument('--ledger', type=Path, required=True, help='private evidence ledger directory (mode 0700)')
    connect.add_argument('--metadata', type=Path, required=True, help='new private metadata file (0600)')
    connect.add_argument('--resources', type=Path, help='App resources directory (tokenizer cache)')
    connect.add_argument('--reconnect', action='store_true', help='sign in again for the recorded connection')
    connect.add_argument('--confirm-sign-in', action='store_true', help='the person signs in a separate connection')
    for sub in (live, block, connect):
        sub.add_argument('--installed-home', type=Path, action='append', default=None,
                         help='an installed app data folder to refuse (default: ~/.pebble/data, PEBBLE_HOME/data '
                              'and the old app-data folder)')
    return parser


def _witness(value):
    count, _, head = value.partition(':')
    if not count.isdigit():
        raise EvaluationError('witness_invalid')
    return {'entries': int(count), 'head': head}


def _limits(args, keys):
    return {key: getattr(args, key) for key in keys if getattr(args, key, None) is not None} or None


def _emit(args, result):
    if args.report:
        _write_new(args.report, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _hook(module, factory):
    """Track Q3's hook object (restart_hook / connectors_hook) once its module exists, else None."""
    import importlib
    import importlib.util
    if importlib.util.find_spec(module) is None:
        return None
    made = getattr(importlib.import_module(module), factory, None)
    return made() if callable(made) else None


def _installed(args):
    """The installed app's connection and host ids (read-only), before any owned App starts."""
    return installed_connection_identities(args.installed_home or None)


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + '\n')


def _owned_app(home, resources):
    from pebble.app import App
    return App(home, resources or ROOT / 'src-tauri' / 'resources', lambda *a, **k: None)


def _evaluation_connection(args, app_factory=_owned_app, clock=time.monotonic, sleep=time.sleep):
    """One sign-in through an owned App in the evaluation home (V1-015). Prints only a content-free result."""
    if not args.confirm_sign_in:
        print('Refused: the evaluation connection needs one ChatGPT sign-in by the person; pass --confirm-sign-in.',
              file=sys.stderr)
        return 2
    installed = _installed(args)
    home = args.home.expanduser().resolve()
    for other in (args.installed_home or installed_app_homes()):
        other = Path(other).expanduser().resolve()
        if home == other or home.is_relative_to(other) or other.is_relative_to(home):
            raise EvaluationError('evaluation_home_is_installed_home')
    ledger = EvidenceLedger(args.ledger)
    if args.reconnect:
        recorded = load_live_metadata(args.metadata)
        require_evaluation_connection(recorded, ledger, installed=installed)
        profile_id = recorded['profile']['id']
    else:
        if args.metadata.exists() or args.metadata.is_symlink():
            raise EvaluationError('evaluation_metadata_exists')
        if (ledger.root / EVALUATION_CONNECTION_FILE).exists():
            raise EvaluationError('evaluation_connection_already_recorded')
        profile_id = None
    app = app_factory(home, args.resources)
    try:
        started = app.chatgpt.start(profile_id)
        print('Sign in the evaluation connection (not the app\'s) by opening this address in your browser:\n'
              + started['url'], file=sys.stderr)
        deadline = clock() + SIGN_IN_SECONDS
        while True:
            status = app.chatgpt.status()
            if status.get('state') == 'connected':
                break
            if status.get('state') == 'failed':
                print('Refused: the evaluation sign-in failed: ' + str(status.get('error'))[:250], file=sys.stderr)
                return 2
            if clock() > deadline:
                app.chatgpt.cancel()
                raise EvaluationError('evaluation_sign_in_timed_out')
            sleep(1.0)
        profile = next((p for p in app.model.profiles() if p.get('id') == status.get('profile_id')), None)
        host_id = app.store.setting('chatgpt_host_id')
    finally:
        app.close()
    metadata = {'profile': validate_live_profile(profile), 'chatgpt_host_id': validate_host_id(host_id)}
    if args.reconnect:
        require_evaluation_connection(metadata, ledger, installed=installed)  # still the recorded connection
        result = 'reconnected'
    else:
        # Recording refuses the app's own connection or host id first, so a refused sign-in writes nothing.
        record_evaluation_connection(ledger, metadata['profile'], metadata['chatgpt_host_id'], installed=installed)
        _write_private(args.metadata, metadata)
        result = 'recorded'
    print(json.dumps({'evaluation_connection': result, 'installed_stores_checked': installed['stores']},
                     indent=2, sort_keys=True))
    return 0


def _campaign(args):
    """Phase 18 subcommands. Live work keeps --confirm-live and refuses existing report paths first."""
    if args.command == 'campaign-plan':
        if args.report and (args.report.exists() or args.report.is_symlink()):
            print('Refused: the report path already exists.', file=sys.stderr)
            return 2
        return _emit(args, campaign_plan(args.claim_set, blocks=args.blocks))
    if args.command == 'register':
        plan = _read_report(args.plan)
        plan_sha256 = verify_plan(plan)
        registry = EvidenceRegistry(EvidenceLedger(args.ledger))
        entry = registry.register_plan(plan_sha256, plan['thresholds_sha256'], planned_cell_count(plan))
        print(json.dumps({'registered': entry['kind'], 'plan_sha256': plan_sha256, 'seq': entry['seq'],
                          'entry_sha256': entry['hash'], 'planned_cells': entry['planned_cells'],
                          'witness': registry.head()}, indent=2, sort_keys=True))
        return 0
    if args.command == 'registry-head':
        print(json.dumps(EvidenceRegistry(EvidenceLedger(args.ledger)).head(), indent=2, sort_keys=True))
        return 0
    if args.command == 'analyse':
        if args.report and (args.report.exists() or args.report.is_symlink()):
            print('Refused: the report path already exists.', file=sys.stderr)
            return 2
        ledger = EvidenceLedger(args.ledger)
        return _emit(args, analyse_campaign(_read_report(args.plan), [_read_report(path) for path in args.reports],
                                            ledger, EvidenceRegistry(ledger),
                                            witnessed=[_witness(value) for value in args.witness]))
    if not args.confirm_live:
        print('Refused: live evaluation sends requests through the selected ChatGPT account; '
              'pass --confirm-live.', file=sys.stderr)
        return 2
    if args.report.exists() or args.report.is_symlink():
        print('Refused: the report path already exists.', file=sys.stderr)
        return 2
    plan = _read_report(args.plan)
    try:
        schedule = plan['schedule'][args.block - 1]
        case_ids = schedule['invocations'][args.invocation - 1] if args.invocation >= 1 and args.block >= 1 else None
    except (KeyError, TypeError, IndexError):
        case_ids = None
    if case_ids is None:
        raise EvaluationError('invocation_not_in_plan')
    metadata = load_live_metadata(args.profile_metadata)
    ledger = EvidenceLedger(args.ledger)
    connection = require_evaluation_connection(metadata, ledger, installed=_installed(args))  # V1-015
    # The registered corpus; run_paired_block refuses a plan made for any other corpus or fingerprint.
    report = run_paired_block(corpus=CORPORA_18[-1], block=args.block, seed=plan.get('seed'), case_ids=case_ids,
                              profile=metadata['profile'], chatgpt_host_id=metadata['chatgpt_host_id'], ledger=ledger,
                              registry=EvidenceRegistry(ledger), plan=plan, resources=args.resources,
                              scratch_root=args.scratch, restart=_hook('pebble.eval_recovery_18', 'restart_hook'),
                              connectors=_hook('pebble.eval_connectors_18', 'connectors_hook'),
                              diagnostics=args.diagnostics,
                              progress=lambda task, arm, outcome: print(task + ' ' + arm + ': ' + outcome,
                                                                        file=sys.stderr))
    _write_new(args.report, report)
    stop = report['controller']['stop']
    print(json.dumps({'evaluation_id': report['evaluation_id'], 'block': report['block'],
                      'invocation': report['invocation'], 'evidence': report['evidence'],
                      'summary': report['summary'], 'controller_stop': stop,
                      'source_stable': report['source']['stable'],
                      'evaluation_connection': connection['evaluation_connection'],
                      'witness': EvidenceRegistry(ledger).head()}, indent=2, sort_keys=True))
    if not report['source']['stable']:
        return 3
    if stop == 'auth_failure':
        print(AUTH_STOPPED, file=sys.stderr)
        return 4
    return 5 if stop else 0


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command in {'campaign-plan', 'register', 'registry-head', 'block', 'analyse'}:
            return _campaign(args)
        if args.command == 'eval-connection':
            return _evaluation_connection(args)
        if args.command == 'corpus':
            print(json.dumps(corpus_manifest(args.corpus), indent=2, sort_keys=True))
            return 0
        if args.command in {'compare', 'compare-revisions'}:
            compare = compare_reports if args.command == 'compare' else compare_revisions
            return _emit(args, compare(_read_report(args.reports[0]), _read_report(args.reports[1]),
                                       EvidenceLedger(args.ledger)))
        if args.command == 'plan':
            return _emit(args, live_plan(args.claim_set, corpora=_names(args.corpora) or CORPORA,
                                         arms=_names(args.arms) or ARMS, repetitions=args.repetitions,
                                         limits=_limits(args, ('max_steps', 'max_seconds'))))
        if args.command == 'economics':
            if args.report and (args.report.exists() or args.report.is_symlink()):
                print('Refused: the report path already exists.', file=sys.stderr)
                return 2
            return _emit(args, scripted_economics(corpus=args.corpus, case_ids=_names(args.cases),
                                                  arms=_names(args.arms) or ARMS, scratch_root=args.scratch,
                                                  limits=_limits(args, ('max_steps', 'max_seconds'))))
        if not args.confirm_live:
            print('Refused: live evaluation sends requests through the selected ChatGPT account; '
                  'pass --confirm-live.', file=sys.stderr)
            return 2
        if args.report.exists() or args.report.is_symlink():
            print('Refused: the report path already exists.', file=sys.stderr)
            return 2
        metadata = load_live_metadata(args.profile_metadata)
        ledger = EvidenceLedger(args.ledger)
        require_evaluation_connection(metadata, ledger, installed=_installed(args))  # V1-015
        limits = _limits(args, ('max_steps', 'max_seconds', 'wall_seconds', 'total_wall_seconds'))
        report = run_evaluation(mode='live', arm=args.arm, profile=metadata['profile'],
                                chatgpt_host_id=metadata['chatgpt_host_id'], case_ids=_names(args.cases),
                                limits=limits, corpus=args.corpus,
                                ledger=ledger, resources=args.resources,
                                scratch_root=args.scratch,
                                progress=lambda case, outcome: print(case + ': ' + outcome, file=sys.stderr))
        _write_new(args.report, report)
        summary = report['summary']
        print(json.dumps({'evaluation_id': report['evaluation_id'], 'arm': report['arm'],
                          'corpus': report['corpus']['version'], 'evidence': report['evidence'],
                          'selected': summary['selected'], 'passed': summary['passed'],
                          'outcomes': summary['outcomes'], 'source_stable': report['source']['stable']},
                         indent=2, sort_keys=True))
        return 0 if report['source']['stable'] else 3
    except (EvaluationError, EvidenceRefused, RegistryRefused) as exc:
        print('Refused: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
