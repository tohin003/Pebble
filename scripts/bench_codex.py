"""Codex CLI cells of a corpus 24a `codex` plan (Phase 24, t24-bench). Dry run by default; parent-run.

  dry-run PLAN [--scratch-parent DIR] [--codex BIN] [--codex-home DIR] [--positions N,...] [--report FILE]
                    build each Codex cell's throwaway folder under /private/tmp/pebble-bench-<plan>/codex/ and print
                    the exact commands (prompts as digests). Starts nothing and opens no connection.
  probe --codex BIN --confirm-tool
                    `codex --version` and `codex exec --help` only: the version and whether every flag exists.
  run PLAN --ledger DIR --codex BIN --codex-home DIR --report FILE --confirm-live [--positions N,...]
                    the live cells: refused unless the plan is a registered `codex` plan with the person's go-ahead
                    stamp (D13), Codex passes the probe, and CODEX_HOME is a clean scratch sign-in (never ~/.codex).

Codex runs `codex exec --json` with --sandbox workspace-write, no tool network, approval policy never, gpt-6-luna at
medium effort, a scratch HOME and CODEX_HOME, 15 minutes per cell, one attempt per cell. Exit codes: 0 done, 2 refused.
"""

import argparse
import json
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT))

from benchmarks import corpus_24a as corpus  # noqa: E402
from benchmarks import plan_24a as bp  # noqa: E402
from benchmarks.corpus_24a import graders as gr  # noqa: E402
from benchmarks.drivers import codex_cell as cx  # noqa: E402
from benchmarks.drivers import workspace as ws  # noqa: E402
from pebble.eval_registry import RegistryRefused  # noqa: E402
from pebble.evaluation import EvaluationError, EvidenceLedger, EvidenceRefused  # noqa: E402

DRY_RUN_SCHEMA = 'pebble.bench_codex_dry_run/1'


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise bp.PlanRefused('file_unreadable')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise bp.PlanRefused('file_unreadable') from None


def _write_new(path, data):
    with Path(path).open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + '\n')


def _codex_plan(plan):
    bp.verify_plan(plan)
    bp.corpus_unchanged(plan)
    if plan['kind'] != 'codex':
        raise bp.PlanRefused('plan_kind_not_codex')
    return plan


def _cells(plan, positions):
    cells = [c for c in plan['schedule'] if c['product'] == 'codex']
    if positions:
        wanted = {int(p) for p in positions.split(',')}
        cells = [c for c in cells if c['position'] in wanted]
        if len(cells) != len(wanted):
            raise bp.PlanRefused('positions_not_codex_cells')
    return cells


def dry_run(plan, *, parent=ws.TMP, binary='codex', codex_home=None, positions=None):
    plan = _codex_plan(plan)
    home = cx.check_codex_home(codex_home) if codex_home else Path('<scratch CODEX_HOME>')
    root = ws.plan_root(plan['plan_sha256'], parent)
    cells = [cx.dry_run_cell(corpus.BY_ID[c['task']], c, root, binary, home) for c in _cells(plan, positions)]
    return {'schema': DRY_RUN_SCHEMA, 'plan_sha256': plan['plan_sha256'], 'performs_requests': False,
            'root': str(root), 'codex_home': str(home), 'model': cx.MODEL, 'effort': cx.EFFORT, 'cells': cells}


def _online():
    try:
        socket.getaddrinfo('chatgpt.com', 443)
        return True
    except OSError:
        return False


def run(plan, *, ledger, binary, codex_home, parent=ws.TMP, positions=None, probe=cx.probe, spawn=cx.Process,
        progress=None):
    """Live Codex cells of a registered, stamped codex plan; returns the signed, content-free results report."""
    plan = _codex_plan(plan)
    bp.require_go_ahead(plan, ledger)
    home = cx.check_codex_home(codex_home)
    checked = probe(binary)
    if not checked['ok']:
        raise bp.PlanRefused(['codex_probe_failed'] + ['missing:' + f for f in checked['missing_flags']])
    root = ws.plan_root(plan['plan_sha256'], parent)
    cells = _cells(plan, positions)
    evaluation_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    network = {'online_at_start': _online()}
    bp.begin(ledger, plan, cells, evaluation_id)
    rows = []
    for planned in cells:
        task = corpus.BY_ID[planned['task']]
        try:
            out, metrics = cx.run_cell(task, planned, root, binary, home, wall=plan['limits']['cell_wall_seconds'],
                                       spawn=spawn)
            verdict = gr.grade(task, out) if out.state in ('completed', 'failed') else None
            outcome = gr.classify(task, out, verdict)
        except Exception as exc:  # a crash counts as a retained cell, never a retry
            out, verdict, outcome = None, None, 'error_driver'
            metrics = {'review': {}, 'requests': None, 'usage': None, 'wall': None, 'first': None,
                       'error': 'driver_' + type(exc).__name__.lower()}
        rows.append(bp.result_cell(planned, task, out, verdict, outcome, review=metrics['review'],
                                   requests=metrics['requests'], wall=metrics['wall'], first=metrics['first'],
                                   usage=metrics['usage'], error=metrics['error']))
        if progress:
            progress(planned, outcome)
    network['online_at_end'] = _online()
    report = bp.sign_results(ledger, plan, 'codex', rows, evaluation_id=evaluation_id, evidence='live_model',
                             versions={'codex': checked['version'], 'model': cx.MODEL, 'effort': cx.EFFORT},
                             started_at=started_at, network=network)
    bp.end(ledger, report)
    return report


def _parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    dry = commands.add_parser('dry-run')
    dry.add_argument('plan', type=Path)
    dry.add_argument('--scratch-parent', type=Path, default=ws.TMP)
    dry.add_argument('--codex', default='codex')
    dry.add_argument('--codex-home', type=Path)
    dry.add_argument('--positions')
    dry.add_argument('--report', type=Path)
    check = commands.add_parser('probe')
    check.add_argument('--codex', required=True)
    check.add_argument('--confirm-tool', action='store_true')
    live = commands.add_parser('run')
    live.add_argument('plan', type=Path)
    live.add_argument('--ledger', type=Path, required=True)
    live.add_argument('--codex', required=True)
    live.add_argument('--codex-home', type=Path, required=True)
    live.add_argument('--report', type=Path, required=True)
    live.add_argument('--scratch-parent', type=Path, default=ws.TMP)
    live.add_argument('--positions')
    live.add_argument('--confirm-live', action='store_true')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == 'dry-run':
            if args.report and (args.report.exists() or args.report.is_symlink()):
                raise bp.PlanRefused('report_path_exists')
            result = dry_run(_read(args.plan), parent=args.scratch_parent, binary=args.codex,
                             codex_home=args.codex_home, positions=args.positions)
            if args.report:
                _write_new(args.report, result)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == 'probe':
            if not args.confirm_tool:
                print('Refused: the probe runs the Codex binary (version and help only); pass --confirm-tool.',
                      file=sys.stderr)
                return 2
            print(json.dumps(cx.probe(args.codex), indent=2, sort_keys=True))
            return 0
        if not args.confirm_live:
            print('Refused: live Codex cells use the person\'s ChatGPT account; pass --confirm-live.', file=sys.stderr)
            return 2
        if args.report.exists() or args.report.is_symlink():
            raise bp.PlanRefused('report_path_exists')
        report = run(_read(args.plan), ledger=EvidenceLedger(args.ledger), binary=args.codex,
                     codex_home=args.codex_home, parent=args.scratch_parent, positions=args.positions,
                     progress=lambda cell, outcome: print('%s %s: %s' % (cell['position'], cell['task'], outcome),
                                                          file=sys.stderr))
        _write_new(args.report, report)
        print(json.dumps({'evaluation_id': report['evaluation_id'], 'cells': len(report['cells'])}, indent=2))
        return 0
    except (bp.PlanRefused, cx.CodexRefused, ws.SandboxRefused, EvaluationError, EvidenceRefused,
            RegistryRefused) as exc:
        print('Refused: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
