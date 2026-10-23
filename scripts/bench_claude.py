"""Corpus 24a ``claude`` plans: Pebble against Claude Code CLI on the same Claude model (Phase 27, D20/D21). Parent-run.

  probe --claude BIN               `claude --version` and `claude --help` only: the version and whether every flag exists
  signed-in --claude BIN --config DIR [--existing-login]
                                   whether the CLAUDE_CONFIG_DIR holds a login a cell's scratch HOME reaches (`claude
                                   auth status`); the credential is never read
  preflight --claude BIN --config DIR --model ID [--existing-login]
                                   one fixed tiny turn outside every plan: login, isolation on the model, quota windows
  snapshot-claude-w --config DIR --out NEW_FILE
                                   names and digests of the person's configuration folder before benchmarks (D25)
  restore-claude-w SNAPSHOT --config DIR [--apply]
                                   list (or, with --apply, remove) only the benchmark's own project folders in it; every
                                   other difference is reported by name and left alone
  dry-run PLAN --claude BIN --config DIR [--positions N,...] [--report FILE]
                                   build each Claude Code cell's throwaway folder and print the exact commands (prompts as
                                   digests); starts nothing
  eval-provider --backend EXE --home DIR --ledger DIR --metadata NEW_FILE --model ID --confirm-key-entry [--finish]
                                   RUN BY THE PERSON: types the Anthropic key into a hidden prompt; the frozen candidate
                                   backend stores it in its own Keychain record (model_save) for a new evaluation profile;
                                   the ledger records the profile as fingerprints. The key is never printed or written
                                   here.
                                   --finish records the profile an interrupted entry already saved (exactly one, its key
                                   in Keychain), asking for nothing
  run PLAN --product pebble --ledger DIR --metadata FILE --backend EXE --report FILE --confirm-live
          [--prior-report FILE ...] [--positions N,...]
                                   Pebble's planned cells over JSON-RPC on the evaluation provider, metered at the plan's
                                   prices: a cell starts only while the spend (prior signed reports included) plus the
                                   largest cell so far stays below the plan's stop line; the rest are recorded not_run
                                   with error budget_stop
  run PLAN --product claude_code --ledger DIR --claude BIN --config DIR --report FILE --confirm-live
          [--existing-login] [--positions ...]
                                   Claude Code's planned cells on the person's subscription (no money is spent): a free
                                   preflight turn first, then cells paced by the quota windows (a cell starts only
                                   while every window is below 80%; a pause longer than 6 hours stops the run, the rest
                                   recorded not_run with error quota_stop); an isolation breach stops the run
  spend PLAN --ledger DIR REPORT...
                                   the metered spend of signed Pebble reports of this plan (no request)

Live cells need a registered ``claude`` plan, the person's go-ahead stamp (D13) and --confirm-live. Exit codes: 0 done,
2 refused.
"""

import argparse
import getpass
import json
import os
import socket
import sys
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT))

from benchmarks import corpus_24a as corpus  # noqa: E402
from benchmarks import plan_24a as bp  # noqa: E402
from benchmarks.corpus_24a import graders as gr  # noqa: E402
from benchmarks.drivers import anthropic_route as ar  # noqa: E402
from benchmarks.drivers import claude_cell as cc  # noqa: E402
from benchmarks.drivers import live  # noqa: E402
from benchmarks.drivers import workspace as ws  # noqa: E402
from pebble.eval_registry import RegistryRefused  # noqa: E402
from pebble.evaluation import EvaluationError, EvidenceLedger, EvidenceRefused  # noqa: E402
from pebble import evaluation as ev  # noqa: E402

DRY_RUN_SCHEMA = 'pebble.bench_claude_dry_run/1'
SPEND_SCHEMA = 'pebble.bench_claude_spend/1'
PROVIDER_NAME = 'Benchmark Anthropic'
# The Claude models' context window, as the person's app lists it for them, so Pebble works with the same room as
# Claude Code (whose window is the model's own).
CONTEXT = 1_000_000


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise bp.PlanRefused('file_unreadable')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise bp.PlanRefused('file_unreadable') from None


def _write_new(path, data, mode=0o644):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + '\n')


def claude_plan(plan):
    bp.verify_plan(plan)
    bp.corpus_unchanged(plan)
    if plan['kind'] != 'claude':
        raise bp.PlanRefused('plan_kind_not_claude')
    bp.check_budget(plan.get('budget'))
    return plan


def _cells(plan, product, positions):
    cells = [c for c in plan['schedule'] if c['product'] == product]
    if positions:
        wanted = set(positions)
        cells = [c for c in cells if c['position'] in wanted]
        if len(cells) != len(wanted):
            raise bp.PlanRefused('positions_not_cells_of_product')
    return cells


def _online():
    try:
        socket.getaddrinfo('api.anthropic.com', 443)
        return True
    except OSError:
        return False


# -- the metered spend ------------------------------------------------------------------------------------------------

def spend(plan, reports, ledger):
    """USD spent by signed Pebble reports of this plan, at the plan's prices; refuses a foreign or unsigned report."""
    plan = claude_plan(plan)
    prices = plan['budget']['prices_per_mtok']
    total, cells, unknown, seen, largest = 0.0, 0, 0, set(), 0.0
    for report in reports:
        try:
            ledger.verify(report, schema=bp.RESULTS_SCHEMA)
        except EvidenceRefused as exc:
            raise bp.PlanRefused(exc.reasons) from None
        if report['plan_sha256'] != plan['plan_sha256'] or report['product'] != 'pebble':
            raise bp.PlanRefused('report_not_a_pebble_report_of_this_plan')
        if report['evaluation_id'] in seen:
            raise bp.PlanRefused('duplicate_report_supplied')
        seen.add(report['evaluation_id'])
        for cell in report['cells']:
            cost = bp.cell_cost_usd(cell.get('usage'), prices)
            if cost is None:
                unknown += cell['outcome'] != 'not_run'
            else:
                total += cost
                cells += 1
                largest = max(largest, cost)
    return {'schema': SPEND_SCHEMA, 'plan_sha256': plan['plan_sha256'], 'spent_usd': round(total, 4),
            'metered_cells': cells, 'cells_without_usage': unknown, 'largest_cell_usd': round(largest, 4),
            'stop_at_usd': plan['budget']['stop_at_usd'],
            'usd_cap': plan['budget']['usd_cap'], 'prices_per_mtok': plan['budget']['prices_per_mtok']}


# -- the person's key entry --------------------------------------------------------------------------------------------

def hidden_key(prompt, *, terminal=None, read=getpass.getpass):
    """The key from a hidden prompt on a real terminal, or a refusal before anything is read. Without a terminal (a
    pipe, or a tool running the command for the person), getpass falls back to plain input and warns that the key may
    be echoed; here that warning is an error, so the key never reaches a transcript (Phase 27, found at the first key
    entry)."""
    terminal = (sys.stdin.isatty() and sys.stderr.isatty()) if terminal is None else terminal
    if not terminal:
        raise bp.PlanRefused('key_entry_needs_a_terminal')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return read(prompt)
        except getpass.GetPassWarning:
            raise bp.PlanRefused('key_entry_needs_a_terminal') from None


def _provider_paths(home, ledger, metadata):
    metadata = Path(metadata)
    if metadata.exists() or metadata.is_symlink():
        raise bp.PlanRefused('metadata_path_exists')
    if (ledger.root / ar.PROVIDER_FILE).exists():
        raise EvaluationError('evaluation_provider_already_recorded')
    # Absolute: the backend starts in its home folder, so a relative --home would nest a second home inside it
    # (the first key entry, Phase 27: "relative paths can't be expressed as file URIs").
    return Path(home).expanduser().resolve(), metadata


def _record_saved(state, *, model, ledger, metadata, home, installed):
    profiles = [p for p in (state.get('profiles') or []) if isinstance(p, dict) and p.get('name') == PROVIDER_NAME]
    if len(profiles) != 1:
        raise EvaluationError('provider_profile_not_saved' if not profiles else 'provider_profiles_ambiguous')
    profile = ar.validate_provider_profile(profiles[0], model=model)
    _write_new(metadata, {'profile': profile}, mode=0o600)
    recorded = ar.record_provider(ledger, profile, installed=installed)
    return {**recorded, 'model': model, 'metadata': str(metadata), 'home': str(home)}


def eval_provider(*, backend, home, ledger, metadata, model, ask=hidden_key, measure=None, installed=None):
    """The person types the key; the frozen candidate stores it (its own Keychain record); returns a summary."""
    live.evaluation_backend(backend)  # Phase 30 (D41): never the release bundle, which owns the person's Keychain item
    if model not in bp.MODELS['claude']:
        raise bp.PlanRefused('model_not_for_kind')
    home, metadata = _provider_paths(home, ledger, metadata)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.stat(home).st_mode & 0o077:
        raise bp.PlanRefused('evaluation_home_not_private')
    key = ask('Anthropic API key for the Pebble benchmark (input hidden): ')
    if not ar.key_shape_ok(key):
        raise bp.PlanRefused('key_shape_unexpected')
    measure = measure or live.rpc_cell._measure()
    backend_process = measure.Backend([str(Path(backend).resolve())], home)
    try:
        backend_process.rpc('model_save', name=PROVIDER_NAME, url=ar.ANTHROPIC_URL, model=model, kind='anthropic',
                            api_key=key.strip(), context=CONTEXT, budget=CONTEXT)
        state = backend_process.rpc('state')
    finally:
        key = None  # noqa: F841 - the only copy leaves this process with the call above
        backend_process.close()
    return _record_saved(state, model=model, ledger=ledger, metadata=metadata, home=home, installed=installed)


def finish_provider(*, backend, home, ledger, metadata, model, measure=None, installed=None):
    """Record a profile the person's key entry already saved when the entry stopped after model_save: exactly one
    benchmark profile, with its key in Keychain (has_key), on the plan's model. No key is asked for or read."""
    live.evaluation_backend(backend)  # Phase 30 (D41)
    if model not in bp.MODELS['claude']:
        raise bp.PlanRefused('model_not_for_kind')
    home, metadata = _provider_paths(home, ledger, metadata)
    if not (home / 'pebble.sqlite').is_file() or os.stat(home).st_mode & 0o077:
        raise bp.PlanRefused('evaluation_home_invalid')
    measure = measure or live.rpc_cell._measure()
    backend_process = measure.Backend([str(Path(backend).resolve())], home)
    try:
        state = backend_process.rpc('state')
    finally:
        backend_process.close()
    return _record_saved(state, model=model, ledger=ledger, metadata=metadata, home=home, installed=installed)


# -- live runs --------------------------------------------------------------------------------------------------------

def run_pebble(plan, *, ledger, metadata_path, backend, prior_reports=(), parent=ws.TMP, positions=None,
               resources=None, progress=None, run_cells=live.run_cells, installed=None):
    """Pebble's planned cells on the evaluation provider, metered; returns the signed, content-free results report."""
    plan = claude_plan(plan)
    bp.require_go_ahead(plan, ledger)
    model = plan['route']['model']
    metadata = ar.load_provider_metadata(metadata_path)
    ar.require_provider(metadata, ledger, model, installed=installed)
    command = live.candidate_command(plan, backend)
    source = ev.source_fingerprint()
    if source['sha256'] != plan['source']['sha256']:
        raise bp.PlanRefused('source_drift')
    budget = spend(plan, list(prior_reports), ledger)
    prices, stop = plan['budget']['prices_per_mtok'], plan['budget']['stop_at_usd']
    if budget['spent_usd'] + budget['largest_cell_usd'] >= stop:
        raise bp.PlanRefused('budget_exhausted')
    cells = _cells(plan, 'pebble', positions)
    route = {'mode': 'live', 'profile': metadata['profile'], 'host_id': None}
    root = ws.plan_root(plan['plan_sha256'], parent)
    evaluation_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    network = {'online_at_start': _online()}
    bp.begin(ledger, plan, cells, evaluation_id)
    spent, rows, stopped, largest = budget['spent_usd'], [], 0, budget['largest_cell_usd']
    trap = live.rpc_cell._measure().EgressTrap().start()
    allowlist = live.HostAllowlist(ar.ALLOWED_HOSTS)
    try:
        with allowlist.active():
            for planned in cells:
                task = corpus.BY_ID[planned['task']]
                # Predictive: a cell starts only while the spend plus the largest cell so far stays under the line.
                if spent + largest >= stop:
                    rows.append(bp.result_cell(planned, task, None, None, 'not_run', error='budget_stop'))
                    stopped += 1
                    continue
                row = run_cells(plan, 'pebble', [planned], route, root=root, resources=resources,
                                backend_command=command, trap=trap)[0]
                rows.append(row)
                cost = bp.cell_cost_usd(row.get('usage'), prices)
                spent = round(spent + (cost or 0.0), 6)
                largest = max(largest, cost or 0.0)
                if progress:
                    progress(planned, row['outcome'], spent)
    finally:
        network.update(allowlist.report(), backend_proxy_attempts=trap.report()['attempts'])
        trap.stop()
    network['online_at_end'] = _online()
    if ev.source_fingerprint()['sha256'] != source['sha256']:
        rows = [{**row, 'outcome': 'error_driver', 'success': False, 'error': 'source_drift'} for row in rows]
    from pebble import VERSION
    report = bp.sign_results(ledger, plan, 'pebble', rows, evaluation_id=evaluation_id, evidence='live_model',
                             versions={'pebble': VERSION, 'source_sha256': source['sha256'],
                                       'candidate_sha256': plan['candidate']['backend_sha256'], 'driver': 'json_rpc',
                                       'model': model, 'effort': 'medium', 'route': 'anthropic',
                                       'spent_usd_after': round(spent, 4), 'budget_stopped_cells': stopped},
                             started_at=started_at, network=network)
    bp.end(ledger, report)
    return report


def run_claude_code(plan, *, ledger, binary, config_dir, parent=ws.TMP, positions=None, probe=cc.probe,
                    signed=cc.signed_in, spawn=cc.Process, progress=None, existing_login=False, check=cc.preflight,
                    sleep=time.sleep, clock=time.time, pin=cc.pin_binary):
    """Claude Code's planned cells; returns the signed, content-free results report (with model-drift, quota and
    isolation counts)."""
    plan = claude_plan(plan)
    bp.require_go_ahead(plan, ledger)
    model = plan['route']['model']
    config = cc.check_claude_config(config_dir, existing_login=existing_login)
    personal = cc.personal_names(config)
    root = ws.plan_root(plan['plan_sha256'], parent)
    # The program under test is a private copy for the whole plan: an update of the installed CLI mid-run cannot reach it.
    binary, binary_sha = pin(binary, root)
    checked = probe(binary)
    if not checked['ok']:
        raise bp.PlanRefused(['claude_probe_failed'] + ['missing:' + f for f in checked['missing_flags']])
    login = signed(binary, config)
    if not login['signed_in']:
        raise bp.PlanRefused('claude_config_not_signed_in')
    # Outside the registry: nothing is begun until the login, the isolation and the quota are known to hold.
    pre = check(binary, config, model, personal=personal, spawn=spawn)
    if not pre['ok']:
        raise bp.PlanRefused(['claude_preflight_failed'] + ['isolation:' + b for b in pre['isolation']]
                             + (['failure:' + pre['failure']] if pre['failure'] else []))
    rate, pauses, paused = pre['rate_limit'], 0, 0
    wait = cc.quota_wait(rate, clock())
    if wait is None:
        raise bp.PlanRefused('claude_quota_low')
    if wait:
        sleep(wait)
        pauses, paused = 1, wait
    cells = _cells(plan, 'claude_code', positions)
    evaluation_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    network = {'online_at_start': _online()}
    bp.begin(ledger, plan, cells, evaluation_id)
    rows, drift, reported_cost, helpers, quota_cells, stop = [], 0, 0.0, set(), 0, None
    for planned in cells:
        task = corpus.BY_ID[planned['task']]
        if stop is None:
            wait = cc.quota_wait(rate, clock())
            if wait is None:
                stop = 'quota_stop'
            elif wait:
                sleep(wait)
                pauses, paused = pauses + 1, paused + wait
        if stop:
            rows.append(bp.result_cell(planned, task, None, None, 'not_run', error=stop))
            if progress:
                progress(planned, 'not_run')
            continue
        try:
            out, metrics = cc.run_cell(task, planned, root, binary, config, model,
                                       wall=plan['limits']['cell_wall_seconds'], spawn=spawn, personal=personal)
            verdict = gr.grade(task, out) if out.state in ('completed', 'failed') else None
            outcome = gr.classify(task, out, verdict)
            if metrics['model_drift'] or metrics['error'] in ('claude_isolation_breach', 'claude_unavailable'):
                drift += bool(metrics['model_drift'])
                outcome = 'error_driver'  # never counted as the plan's model, the product as shipped, or an answer
            reported_cost += metrics['cost_usd_reported'] or 0.0
            helpers |= set(metrics['helper_models'])
            rate = metrics['rate_limit'] or rate
            if metrics['error'] == 'claude_isolation_breach':
                stop = 'isolation_stop'
            if metrics['error'] == 'claude_unavailable':
                stop = 'driver_stop'  # the CLI did not start: no further cell begins in this run
            if metrics['error'] == 'claude_quota':
                quota_cells += 1
                # The quota refused this cell: never start another before the window resets.
                rate = {**rate, 'status': 'rejected'} if rate else {'status': 'rejected', 'resets_at': None,
                                                                    'windows': {}}
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
    subscription = login.get('subscription') or 'unknown'
    report = bp.sign_results(ledger, plan, 'claude_code', rows, evaluation_id=evaluation_id, evidence='live_model',
                             versions={'claude_code': checked['version'], 'claude_code_sha256': binary_sha,
                                       'model': model, 'effort': cc.EFFORT,
                                       'subagent_model': model, 'model_drift_cells': drift,
                                       'helper_models': sorted(helpers),
                                       'reported_cost_usd': round(reported_cost, 4),
                                       'login': 'existing configuration (D25)' if existing_login else 'scratch',
                                       'plan': '%s subscription: the reported cost is API-equivalent, not spent'
                                               % subscription,
                                       'quota_pauses': pauses, 'quota_paused_seconds': int(paused),
                                       'quota_cells': quota_cells,
                                       'quota_stopped_cells': sum(r['error'] == 'quota_stop' for r in rows),
                                       'isolation_stopped_cells': sum(r['error'] == 'isolation_stop' for r in rows),
                                       'driver_stopped_cells': sum(r['error'] == 'driver_stop' for r in rows),
                                       'preflight_cost_usd_reported': pre.get('cost_usd_reported')},
                             started_at=started_at, network=network)
    bp.end(ledger, report)
    return report


def dry_run(plan, *, binary, config_dir, parent=ws.TMP, positions=None):
    plan = claude_plan(plan)
    root = ws.plan_root(plan['plan_sha256'], parent)
    cells = [cc.dry_run_cell(corpus.BY_ID[c['task']], c, root, binary, config_dir or '<scratch CLAUDE_CONFIG_DIR>',
                             plan['route']['model']) for c in _cells(plan, 'claude_code', positions)]
    return {'schema': DRY_RUN_SCHEMA, 'plan_sha256': plan['plan_sha256'], 'model': plan['route']['model'],
            'cells': cells, 'starts_processes': False}


def _positions(text):
    if not text:
        return None
    try:
        return [int(x) for x in text.split(',') if x.strip()]
    except ValueError:
        raise bp.PlanRefused('positions_invalid') from None


def _parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('probe')
    check.add_argument('--claude', required=True)
    signed = commands.add_parser('signed-in')
    signed.add_argument('--claude', required=True)
    signed.add_argument('--config', type=Path, required=True)
    signed.add_argument('--existing-login', action='store_true', help="the person's own signed-in folder (D25)")
    pre = commands.add_parser('preflight')
    pre.add_argument('--claude', required=True)
    pre.add_argument('--config', type=Path, required=True)
    pre.add_argument('--model', required=True, choices=bp.MODELS['claude'])
    pre.add_argument('--existing-login', action='store_true')
    snap = commands.add_parser('snapshot-claude-w')
    snap.add_argument('--config', type=Path, required=True)
    snap.add_argument('--out', type=Path, required=True)
    restore = commands.add_parser('restore-claude-w')
    restore.add_argument('snapshot', type=Path)
    restore.add_argument('--config', type=Path, required=True)
    restore.add_argument('--apply', action='store_true')
    dry = commands.add_parser('dry-run')
    dry.add_argument('plan', type=Path)
    dry.add_argument('--claude', default='claude')
    dry.add_argument('--config', type=Path)
    dry.add_argument('--scratch-parent', type=Path, default=ws.TMP)
    dry.add_argument('--positions')
    dry.add_argument('--report', type=Path)
    provider = commands.add_parser('eval-provider')
    provider.add_argument('--backend', type=Path, required=True)
    provider.add_argument('--home', type=Path, required=True)
    provider.add_argument('--ledger', type=Path, required=True)
    provider.add_argument('--metadata', type=Path, required=True)
    provider.add_argument('--model', required=True, choices=bp.MODELS['claude'])
    provider.add_argument('--confirm-key-entry', action='store_true', help='the person is typing the key')
    provider.add_argument('--finish', action='store_true',
                          help='record the profile an interrupted key entry already saved (no key is asked for)')
    run = commands.add_parser('run')
    run.add_argument('plan', type=Path)
    run.add_argument('--product', choices=('pebble', 'claude_code'), required=True)
    run.add_argument('--ledger', type=Path, required=True)
    run.add_argument('--report', type=Path, required=True)
    run.add_argument('--metadata', type=Path)
    run.add_argument('--backend', type=Path)
    run.add_argument('--prior-report', type=Path, action='append', default=[])
    run.add_argument('--claude')
    run.add_argument('--config', type=Path)
    run.add_argument('--existing-login', action='store_true', help="the person's own signed-in folder (D25)")
    run.add_argument('--resources', type=Path)
    run.add_argument('--scratch-parent', type=Path, default=ws.TMP)
    run.add_argument('--positions')
    run.add_argument('--confirm-live', action='store_true')
    total = commands.add_parser('spend')
    total.add_argument('plan', type=Path)
    total.add_argument('--ledger', type=Path, required=True)
    total.add_argument('reports', type=Path, nargs='*')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == 'probe':
            print(json.dumps(cc.probe(args.claude), indent=2))
            return 0
        if args.command == 'signed-in':
            config = cc.check_claude_config(args.config, existing_login=args.existing_login)
            print(json.dumps(cc.signed_in(args.claude, config), indent=2))
            return 0
        if args.command == 'preflight':
            config = cc.check_claude_config(args.config, existing_login=args.existing_login)
            print(json.dumps(cc.preflight(args.claude, config, args.model, personal=cc.personal_names(config)),
                             indent=2))
            return 0
        if args.command == 'snapshot-claude-w':
            if args.out.exists() or args.out.is_symlink():
                raise bp.PlanRefused('report_path_exists')
            config = cc.check_claude_config(args.config, existing_login=True)
            _write_new(args.out, cc.snapshot(config, datetime.now(timezone.utc).isoformat(timespec='seconds')),
                       mode=0o600)
            print(json.dumps({'snapshot': str(args.out)}))
            return 0
        if args.command == 'restore-claude-w':
            config = cc.check_claude_config(args.config, existing_login=True)
            before = _read(args.snapshot)
            if before.get('schema') != 'pebble.claude_w_snapshot/1' or Path(before.get('config', '')) != config:
                raise bp.PlanRefused('snapshot_not_of_this_config')
            found = cc.remove_residue(before, config) if args.apply else cc.residue(before, config)
            print(json.dumps(found, indent=2))
            return 0
        if args.command == 'dry-run':
            if args.report and (args.report.exists() or args.report.is_symlink()):
                raise bp.PlanRefused('report_path_exists')
            result = dry_run(_read(args.plan), binary=args.claude, config_dir=args.config, parent=args.scratch_parent,
                             positions=_positions(args.positions))
            if args.report:
                _write_new(args.report, result)
            print(json.dumps({'cells': len(result['cells']), 'model': result['model'], 'starts_processes': False}))
            return 0
        if args.command == 'eval-provider':
            if args.finish:
                print(json.dumps(finish_provider(backend=args.backend, home=args.home, ledger=EvidenceLedger(args.ledger),
                                                 metadata=args.metadata, model=args.model), indent=2))
                return 0
            if not args.confirm_key_entry:
                raise bp.PlanRefused('confirm_key_entry_required')
            print(json.dumps(eval_provider(backend=args.backend, home=args.home, ledger=EvidenceLedger(args.ledger),
                                           metadata=args.metadata, model=args.model), indent=2))
            return 0
        if args.command == 'spend':
            reports = [_read(p) for p in args.reports]
            print(json.dumps(spend(_read(args.plan), reports, EvidenceLedger(args.ledger)), indent=2))
            return 0
        if args.command == 'run':
            if not args.confirm_live:
                raise bp.PlanRefused('confirm_live_required')
            if args.report.exists() or args.report.is_symlink():
                raise bp.PlanRefused('report_path_exists')
            ledger = EvidenceLedger(args.ledger)
            plan = _read(args.plan)
            if args.product == 'pebble':
                if not args.metadata or not args.backend:
                    raise bp.PlanRefused('metadata_and_backend_required')
                report = run_pebble(plan, ledger=ledger, metadata_path=args.metadata, backend=args.backend,
                                    prior_reports=[_read(p) for p in args.prior_report], parent=args.scratch_parent,
                                    positions=_positions(args.positions), resources=args.resources,
                                    progress=lambda c, o, s: print(json.dumps({'position': c['position'], 'outcome': o,
                                                                               'spent_usd': round(s, 4)}), flush=True))
            else:
                if not args.claude or not args.config:
                    raise bp.PlanRefused('claude_and_config_required')
                report = run_claude_code(plan, ledger=ledger, binary=args.claude, config_dir=args.config,
                                         parent=args.scratch_parent, positions=_positions(args.positions),
                                         existing_login=args.existing_login,
                                         progress=lambda c, o: print(json.dumps({'position': c['position'],
                                                                                 'outcome': o}), flush=True))
            _write_new(args.report, report)
            print(json.dumps({'cells': len(report['cells']), 'report': str(args.report)}))
            return 0
    except (bp.PlanRefused, cc.ClaudeRefused, EvaluationError, EvidenceRefused, RegistryRefused) as exc:
        print(json.dumps({'refused': getattr(exc, 'reasons', None) or str(exc)}), file=sys.stderr)
        return 2
    return 2


if __name__ == '__main__':
    sys.exit(main())
