"""AgentGuard ablation, AGB-24 (Phase 24, t24-bench). Parent-run; see docs/PEBBLE_V1_BENCHMARKS.md.

  scripted --report FILE [--cases R1,...] [--arms on,off,shadow] [--scratch-parent DIR] [--plan-id ID]
                    the scripted arms in a sandbox under /private/tmp/pebble-agb-<plan-id>/; compares every cell with
                    the predicted table (a mismatch is a finding). Needs PEBBLE_AGB_ABLATION=1 in the environment.
  plan --report FILE [--repetitions 3] [--seed N]
                    the live plan (arms ON and SHADOW) with its digest; register and stamp it with bench_harness's
                    ledger before any live call
  register PLAN --ledger DIR | live PLAN --ledger DIR --profile-metadata FILE --report FILE --confirm-live
                    live cells: refused unless the plan is registered and stamped (D13) and the metadata is the
                    evaluation connection (V1-015)

Every executor is a recorder: commands, processes, app launches, plugins and web reads never run. Tests are graded on
the host by the restricted interpreter. The ablation claims reliability metrics only, never security.
"""

import argparse
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT))

from benchmarks import corpus_24g  # noqa: E402
from benchmarks import plan_24a as bp  # noqa: E402
from benchmarks.agb import ablation as ab  # noqa: E402
from pebble.eval_registry import EvidenceRegistry, RegistryRefused  # noqa: E402
from pebble.evaluation import EvaluationError, EvidenceLedger, EvidenceRefused  # noqa: E402

PLAN_SCHEMA = 'pebble.agb_plan/1'
SCHEMA = 'pebble.agb_scripted/1'
LIVE_ARMS = ('on', 'shadow')


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _table():
    """The prediction table for this source: after t24-gate once its adapter lifecycle exists, else 0.6.1's."""
    from pebble.guard import GuardAdapter
    merged = hasattr(GuardAdapter, 'unstage') and hasattr(GuardAdapter, 'end')
    return ('after_t24_gate', corpus_24g.PREDICTED_AFTER_T24_GATE) if merged else ('0.6.1', corpus_24g.PREDICTED_0_6_1)


def make_plan(repetitions=3, seed=None):
    seed = secrets.randbits(63) if seed is None else seed
    cases = [{'id': c.id, 'family': c.family, 'scripted': c.scripted, 'live_only': c.live_only,
              'fingerprint': _sha({'prompt': c.prompt, 'files': c.files, 'steps': [s.__dict__ for s in c.steps],
                                   'mode': c.mode, 'tools': c.tools, 'approve': c.approve, 'reject': c.reject,
                                   'policy': c.policy})} for c in corpus_24g.CASES]
    body = {'schema': PLAN_SCHEMA, 'corpus': corpus_24g.CORPUS_VERSION, 'cases': cases, 'arms': list(LIVE_ARMS),
            'bench_source': bp.bench_source_fingerprint(),
            'repetitions': repetitions, 'seed': seed, 'predictions': {'0.6.1': corpus_24g.PREDICTED_0_6_1,
                                                                    'after_t24_gate': corpus_24g.PREDICTED_AFTER_T24_GATE},
            'claims': {'claimed': 'AgentGuard prevented N of M paired false-completion opportunities (CI), at X ms per '
                                  'message and Y extra requests per held completion, with Z false blocks per 100 runs',
                       'not_claimed': 'any security protection'},
            'statistics': 'exact McNemar for false completions and false blocks; case-clustered bootstrap CIs; '
                          'medians and p95 with bootstrap CIs; latency from the scripted ON-OFF pairs',
            'sandbox': '/private/tmp/pebble-agb-<plan>/; recorder executors; host-graded tests; fake HOME with canaries'}
    return {**body, 'plan_sha256': _sha(body), 'planned_cells': len(cases) * len(LIVE_ARMS) * repetitions}


def verify_plan(plan):
    if not isinstance(plan, dict) or plan.get('schema') != PLAN_SCHEMA:
        raise bp.PlanRefused('plan_invalid')
    body = {k: v for k, v in plan.items() if k not in ('plan_sha256', 'planned_cells')}
    if plan.get('plan_sha256') != _sha(body):
        raise bp.PlanRefused('plan_tampered')
    current = make_plan(plan['repetitions'], plan['seed'])
    if current['plan_sha256'] != plan['plan_sha256']:
        raise bp.PlanRefused('corpus_changed')
    return plan['plan_sha256']


def scripted(report_path, *, parent, plan_id, cases=None, arms=ab.ARMS):
    if os.environ.get(ab.ENV) != '1':
        raise ab.AblationRefused('ablation_not_enabled')
    root = ab.sandbox_root(plan_id, parent)
    root.mkdir(mode=0o700)
    label, table = _table()
    records = ab.scripted_table(root, cases=cases, arms=arms)
    found = ab.mismatches(records, table)
    result = {'schema': SCHEMA, 'evidence': 'scripted_fixture', 'predictions': label, 'records': records,
              'mismatches': found, 'claim': 'reliability metrics of the host on scripted trajectories; no model; '
                                             'no security claim'}
    if report_path:
        with Path(report_path).open('x', encoding='utf-8') as handle:
            handle.write(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return result


RESULTS_SCHEMA = 'pebble.agb_results/1'
REGISTRY_ARM = {'on': 'pebble', 'shadow': 'baseline'}  # the registry's two arm slots: ON and SHADOW


def live(plan, *, ledger, metadata_path, installed_homes=None, parent=ab.TMP, resources=None):
    """Live ON and SHADOW cells of a registered, stamped ablation plan, on the evaluation connection (V1-015)."""
    import uuid
    from benchmarks.drivers.live import HostAllowlist
    from pebble import evaluation as ev
    plan_sha256 = verify_plan(plan)
    go_ahead = bp.go_ahead_for(plan_sha256, ledger)
    metadata = ev.load_live_metadata(metadata_path)
    ev.require_evaluation_connection(metadata, ledger, installed=ev.installed_connection_identities(installed_homes))
    if os.environ.get(ab.ENV) != '1':
        raise ab.AblationRefused('ablation_not_enabled')
    route = {'profile': ev.validate_live_profile(metadata['profile']), 'host_id': metadata['chatgpt_host_id']}
    root = ab.sandbox_root(plan_sha256[:12], parent)
    root.mkdir(mode=0o700)
    cells = [(case, arm, rep) for rep in range(1, plan['repetitions'] + 1) for case in corpus_24g.CASES
             for arm in LIVE_ARMS]
    order = sorted(range(len(cells)), key=lambda i: hashlib.sha256(('%d:%d' % (plan['seed'], i)).encode()).hexdigest())
    evaluation_id = uuid.uuid4().hex
    EvidenceRegistry(ledger).begin(evaluation_id, plan_sha256,
                                   [[case.id, REGISTRY_ARM[arm], rep] for case, arm, rep in cells])
    records, allowlist = [], HostAllowlist()
    with allowlist.active():
        for index in order:
            case, arm, rep = cells[index]
            try:
                records.append(ab.run_case(case, arm, root, resources=resources, go_ahead=go_ahead, route=route,
                                           rep=rep))
            except Exception as exc:  # one attempt per cell; a crash is a retained record
                records.append({'case': case.id, 'arm': arm, 'rep': rep, 'error': type(exc).__name__})
    report = ledger.sign({'schema': RESULTS_SCHEMA, 'evaluation_id': evaluation_id, 'plan_sha256': plan_sha256,
                          'evidence': 'live_model', 'records': records, 'network': allowlist.report()})
    EvidenceRegistry(ledger).end(evaluation_id, hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest())
    return report


def _parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('scripted')
    run.add_argument('--report', type=Path)
    run.add_argument('--cases')
    run.add_argument('--arms', default=','.join(ab.ARMS))
    run.add_argument('--scratch-parent', type=Path, default=ab.TMP)
    run.add_argument('--plan-id', default='scripted-' + secrets.token_hex(4))
    plan = commands.add_parser('plan')
    plan.add_argument('--report', type=Path, required=True)
    plan.add_argument('--repetitions', type=int, default=3)
    plan.add_argument('--seed', type=int)
    register = commands.add_parser('register')
    register.add_argument('plan', type=Path)
    register.add_argument('--ledger', type=Path, required=True)
    stamp = commands.add_parser('stamp', help='record the person\'s go-ahead (D13) for the registered plan')
    stamp.add_argument('plan', type=Path)
    stamp.add_argument('--ledger', type=Path, required=True)
    stamp.add_argument('--decision', required=True)
    stamp.add_argument('--statement', required=True)
    stamp.add_argument('--confirm-person', action='store_true')
    live = commands.add_parser('live')
    live.add_argument('plan', type=Path)
    live.add_argument('--ledger', type=Path, required=True)
    live.add_argument('--profile-metadata', type=Path, required=True)
    live.add_argument('--report', type=Path, required=True)
    live.add_argument('--installed-home', type=Path, action='append', default=None)
    live.add_argument('--confirm-live', action='store_true')
    return parser


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise bp.PlanRefused('file_unreadable') from None


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == 'scripted':
            result = scripted(args.report, parent=args.scratch_parent, plan_id=args.plan_id,
                              cases=args.cases.split(',') if args.cases else None, arms=tuple(args.arms.split(',')))
            print(json.dumps({'cells': len(result['records']), 'mismatches': result['mismatches'],
                              'predictions': result['predictions']}, indent=2, sort_keys=True))
            return 0 if not result['mismatches'] else 1
        if args.command == 'plan':
            plan = make_plan(args.repetitions, args.seed)
            with args.report.open('x', encoding='utf-8') as handle:
                handle.write(json.dumps(plan, indent=2, sort_keys=True) + '\n')
            print(json.dumps({'plan_sha256': plan['plan_sha256'], 'planned_cells': plan['planned_cells']}, indent=2))
            return 0
        if args.command == 'register':
            plan = _read(args.plan)
            plan_sha256 = verify_plan(plan)
            entry = EvidenceRegistry(EvidenceLedger(args.ledger)).register_plan(
                plan_sha256, _sha(plan['predictions']), plan['planned_cells'])
            print(json.dumps({'registered': entry['kind'], 'plan_sha256': plan_sha256, 'seq': entry['seq']}, indent=2))
            return 0
        if args.command == 'stamp':
            if not args.confirm_person:
                print('Refused: a go-ahead stamp records the person\'s own decision; ask them first, then pass '
                      '--confirm-person.', file=sys.stderr)
                return 2
            result = bp.stamp_registered(verify_plan(_read(args.plan)), EvidenceLedger(args.ledger),
                                         decision=args.decision, statement=args.statement)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if not args.confirm_live:
            print('Refused: live ablation cells use the evaluation connection; pass --confirm-live.', file=sys.stderr)
            return 2
        if args.report.exists() or args.report.is_symlink():
            raise bp.PlanRefused('report_path_exists')
        report = live(_read(args.plan), ledger=EvidenceLedger(args.ledger), metadata_path=args.profile_metadata,
                      installed_homes=args.installed_home)
        with args.report.open('x', encoding='utf-8') as handle:
            handle.write(json.dumps(report, indent=2, sort_keys=True) + '\n')
        print(json.dumps({'evaluation_id': report['evaluation_id'], 'records': len(report['records'])}, indent=2))
        return 0
    except (bp.PlanRefused, ab.AblationRefused, EvaluationError, EvidenceRefused, RegistryRefused) as exc:
        print('Refused: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
