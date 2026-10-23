"""Corpus 24a benchmark runner (Phase 24, t24-bench). Parent-run; see docs/PEBBLE_V1_BENCHMARKS.md.

  manifest                          public corpus manifest (ids, families, fingerprints; no prompts or answers)
  plan --kind harness|codex|claude [--claim-set pilot|claim] [--seed N] [--candidate-sha256 H] [--model ID]
       [--budget-file FILE] --report FILE
                                    dry-run plan with its digest and seeded schedule (no request); a claude plan needs
                                    --model and --budget-file (cap, stop line and per-million-token prices, D20)
  register PLAN --ledger DIR        pre-register the plan (refuses an edited plan or a changed corpus)
  stamp PLAN --ledger DIR --decision D13 --statement TEXT --confirm-person
                                    record the person's go-ahead for this registered plan (the parent asks first)
  check-go-ahead PLAN --ledger DIR  verify the plan is unchanged, registered and stamped
  run PLAN --product pebble|baseline --ledger DIR --profile-metadata FILE --report FILE --confirm-live
                                    run this product's planned cells live (needs the stamp and the evaluation
                                    connection, V1-015); Codex CLI cells run through scripts/bench_codex.py, and every
                                    cell of a claude plan through scripts/bench_claude.py
  analyse PLAN RESULTS... --ledger DIR [--report FILE] [--markdown FILE] [--charts NEW_DIR]
                                    offline intention-to-treat analysis, the report tables and the SVG charts (no
                                    request)

Nothing here reads a credential. Live cells use the private ledger's evaluation connection only and send requests
only after --confirm-live, a registered plan and the go-ahead stamp. Exit codes: 0 done, 2 refused, 3 source drift.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT))

from benchmarks import corpus_24a as corpus  # noqa: E402
from benchmarks import plan_24a as bp  # noqa: E402
from pebble.eval_registry import RegistryRefused  # noqa: E402
from pebble.evaluation import EvaluationError, EvidenceLedger, EvidenceRefused  # noqa: E402


def _new(path):
    if path is not None and (path.exists() or path.is_symlink()):
        raise bp.PlanRefused('report_path_exists')
    return path


def _write_new(path, data):
    with Path(path).open('x', encoding='utf-8') as handle:
        handle.write(data if isinstance(data, str) else json.dumps(data, indent=2, sort_keys=True) + '\n')


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64_000_000:
        raise bp.PlanRefused('file_unreadable')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        raise bp.PlanRefused('file_unreadable') from None


def _parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('manifest', help='public corpus manifest')
    plan = commands.add_parser('plan', help='dry-run plan with digest and schedule')
    plan.add_argument('--kind', choices=sorted(bp.KINDS), default='harness')
    plan.add_argument('--claim-set', choices=sorted(bp.CLAIM_SETS), default='pilot')
    plan.add_argument('--seed', type=int)
    plan.add_argument('--candidate-sha256')
    plan.add_argument('--model')
    plan.add_argument('--budget-file', type=Path)
    plan.add_argument('--report', type=Path, required=True)
    for name in ('register', 'check-go-ahead'):
        sub = commands.add_parser(name)
        sub.add_argument('plan', type=Path)
        sub.add_argument('--ledger', type=Path, required=True)
    stamp = commands.add_parser('stamp', help='record the person\'s go-ahead (D13) for a registered plan')
    stamp.add_argument('plan', type=Path)
    stamp.add_argument('--ledger', type=Path, required=True)
    stamp.add_argument('--decision', required=True)
    stamp.add_argument('--statement', required=True)
    stamp.add_argument('--confirm-person', action='store_true', help='the person gave this go-ahead')
    run = commands.add_parser('run', help='run one product\'s planned cells live')
    run.add_argument('plan', type=Path)
    run.add_argument('--product', choices=('pebble', 'baseline'), required=True)
    run.add_argument('--ledger', type=Path, required=True)
    run.add_argument('--profile-metadata', type=Path, required=True)
    run.add_argument('--report', type=Path, required=True)
    run.add_argument('--resources', type=Path)
    run.add_argument('--backend', type=Path, help='the frozen candidate pebble-backend (required for a codex plan)')
    run.add_argument('--scratch-parent', type=Path, default=Path('/private/tmp'))
    run.add_argument('--positions', help='comma-separated schedule positions (default: all of this product)')
    run.add_argument('--installed-home', type=Path, action='append', default=None)
    run.add_argument('--confirm-live', action='store_true')
    analyse = commands.add_parser('analyse', help='offline analysis and report tables')
    analyse.add_argument('plan', type=Path)
    analyse.add_argument('results', nargs='+', type=Path)
    analyse.add_argument('--ledger', type=Path, required=True)
    analyse.add_argument('--report', type=Path)
    analyse.add_argument('--markdown', type=Path)
    analyse.add_argument('--charts', type=Path, help='a new folder for the SVG charts (benchmarks/charts.py)')
    return parser


def _run(args):
    """Live cells for one product (the drivers do the work; see benchmarks/drivers)."""
    if not args.confirm_live:
        print('Refused: live benchmark cells send requests through the evaluation connection; pass --confirm-live.',
              file=sys.stderr)
        return 2
    _new(args.report)
    from benchmarks.drivers import live
    report = live.run_product(_read(args.plan), args.product, ledger=EvidenceLedger(args.ledger),
                              metadata_path=args.profile_metadata, resources=args.resources, backend=args.backend,
                              scratch_parent=args.scratch_parent, installed_homes=args.installed_home,
                              positions=[int(p) for p in args.positions.split(',')] if args.positions else None,
                              progress=lambda cell, outcome: print('%s %s %s: %s' % (
                                  cell['position'], cell['task'], cell['product'], outcome), file=sys.stderr))
    _write_new(args.report, report)
    print(json.dumps({'evaluation_id': report['evaluation_id'], 'cells': len(report['cells']),
                      'outcomes': {o: sum(c['outcome'] == o for c in report['cells'])
                                   for o in sorted({c['outcome'] for c in report['cells']})}}, indent=2, sort_keys=True))
    return 0


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == 'manifest':
            print(json.dumps(corpus.manifest(), indent=2, sort_keys=True))
            return 0
        if args.command == 'plan':
            _new(args.report)
            budget = _read(args.budget_file) if args.budget_file else None
            plan = bp.make_plan(args.kind, args.claim_set, seed=args.seed, candidate_sha256=args.candidate_sha256,
                                model=args.model, budget=budget)
            _write_new(args.report, plan)
            print(json.dumps({'plan_sha256': plan['plan_sha256'], 'kind': plan['kind'], 'totals': plan['totals']},
                             indent=2, sort_keys=True))
            return 0
        if args.command == 'register':
            print(json.dumps(bp.register(_read(args.plan), EvidenceLedger(args.ledger)), indent=2, sort_keys=True))
            return 0
        if args.command == 'stamp':
            if not args.confirm_person:
                print('Refused: a go-ahead stamp records the person\'s own decision; ask them first, then pass '
                      '--confirm-person.', file=sys.stderr)
                return 2
            result = bp.stamp(_read(args.plan), EvidenceLedger(args.ledger), decision=args.decision,
                              statement=args.statement)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == 'check-go-ahead':
            print(json.dumps(bp.require_go_ahead(_read(args.plan), EvidenceLedger(args.ledger)), indent=2,
                             sort_keys=True))
            return 0
        if args.command == 'run':
            return _run(args)
        if args.command == 'analyse':
            _new(args.report)
            _new(args.markdown)
            _new(args.charts)
            ledger = EvidenceLedger(args.ledger)
            analysis = bp.analyse(_read(args.plan), [_read(p) for p in args.results], ledger)
            if args.report:
                _write_new(args.report, analysis)
            if args.markdown:
                _write_new(args.markdown, bp.tables(analysis))
            if args.charts:
                from benchmarks import charts
                plan = _read(args.plan)
                args.charts.mkdir(mode=0o755)
                for name, svg in charts.render_all(analysis, {'model': plan['route']['model']}).items():
                    (args.charts / name).write_text(svg, encoding='utf-8')
            print(json.dumps({'plan_sha256': analysis['plan_sha256'], 'counting': analysis['counting']}, indent=2,
                             sort_keys=True))
            return 0
    except (bp.PlanRefused, EvaluationError, EvidenceRefused, RegistryRefused) as exc:
        print('Refused: ' + str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == '__main__':
    sys.exit(main())
