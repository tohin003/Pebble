"""Phase 18 long-recovery cases (track Q3): restart boundaries, reconciliation and graders.

Design study §2.1 (long recovery L1-L4), §2.2 (restart handling) and §7.2 patch 6. RecoveryHook
implements Q2's restart protocol for evaluation.run_paired_block:

    pebble_due(app, task, cid, rid)        polled by the driver before each review; True restarts the
                                           owned App once, at the task's boundary
    baseline_due(task)                     a predicate(event) the baseline loop calls after each tool;
                                           True restarts the loop once from the task prompt
    pebble_reconcile(app, task, cid, rid)  after the restart, the person's native reconciliation of the
                                           run's uncertain outcomes, then run_resume WITHOUT limits
                                           (called by the driver; Q2 patch B)

Boundaries (corpus 18a.1 RESTARTS):

    after_first_applied_edit  mid-effect: one declared write is applied and the run is still live (L1)
    waiting_approval          before a tool: the effect is proposed and waits for approval (L2)
    after_send_commit         after an uncertain effect: the connector committed the send and its reply
                              was lost (L3; the connector hook arms the loopback fixture)
    after_tests_admission     after an admission, before its terminal observation (L4)

At the L4 boundary the hook simulates the service stopping right after the person admitted the
exact-scope tests action: it makes approve()'s own two admission writes (runs.begin_approval and the
broker's 'running' status) under the App's task lock, and the restart follows before any test
process starts. The reopened App's recovery marks the tool uncertain and the run
needs_reconciliation, exactly as after a crash at that point.

Nothing is ever replayed. pebble_reconcile settles a connector effect only from the host's own
read-class readback (run_reconcile 'readback'); for the simulated admission, which the driver knows
never started, the person reports did_not_occur with a note. Anything else stays uncertain and the
run stays needs_reconciliation (honest). run_resume is never given limits (G12).

The baseline has no approval gate or journal: it restarts right after its first write (L1, L2), its
first send call (L3) or its first test run (L4; before the model sees the result), and its repeats
are measured.

grade_recovery(task, oracle, evidence, workdir, *, fixture_log=None) is Q1's delegated grader for
the recovery family. Evidence must carry the host's run limits (Q2 patch A): `limits`, the run's
recorded config limits when it settled, and `limits_requested`, what the driver sent. Without them,
or without the connector fixture log for L3, the verdict is grader_unavailable (error_driver), never
a pass or a false completion.
"""

import copy
import threading
from collections import Counter
from dataclasses import fields
from pathlib import Path

from . import eval_connectors_18 as connectors
from .eval_corpus_18 import (GRADERS, _typed_equal, _unchanged, claims_completion, effect_linked, scope_violations,
                             tests_after_writes)
from .eval_research_18 import answer_text, arm_of, effect_proposals, single_final
from .evaluation import EvaluationError, _strict_json, _verdict

BOUNDARIES = {
    'after_first_applied_edit': 'mid-effect: one declared write applied, the run still live',
    'waiting_approval': 'before a tool: the effect is proposed and pending approval',
    'after_send_commit': 'after an uncertain effect: the send committed and its reply was lost',
    'after_tests_admission': 'after an admission, before its terminal observation',
}
# Q1's required_checks for the four 18a.1 recovery tasks; the grader records each of them.
FLOORS = {
    'after_first_applied_edit': ('per_path_applied_once', 'outputs_exact', 'limits_unchanged', 'steps_within_budget'),
    'waiting_approval': ('per_path_applied_once', 'outputs_exact', 'limits_unchanged', 'steps_within_budget'),
    'after_send_commit': ('fixture_send_count_one', 'limits_unchanged', 'steps_within_budget'),
    'after_tests_admission': ('no_completion_on_stale_admission', 'hidden_behaviour', 'limits_unchanged',
                              'steps_within_budget'),
}
# Q2 patch A: the evidence fields this grader reads.
EVIDENCE_FIELDS = ('limits', 'limits_requested')
TERMINAL = frozenset({'completed', 'failed', 'stopped', 'cancelled', 'budget_exhausted', 'verification_required'})
NOTE_READBACK = 'Evaluation driver (as the person): settle from the host read-class readback after the restart.'
NOTE_ADMISSION = ('Evaluation driver (as the person): the service was stopped right after this action was admitted '
                  'and before it started, so no test process ran.')
LIMITATIONS = (
    'Restarts are in-process close and reopen of the owned App on the same home, not a process kill.',
    'The L4 boundary is simulated with approve()\'s own admission writes; the test process never starts.',
    'A boundary the driver did not observe before the run settled is not restarted; counts report restarts.',
    'The baseline restarts after its first write, send or test run: it has no approval state to stop in.',
)


def driver_ready():
    """Names of missing driver patches this module needs (empty when ready)."""
    from . import evaluation
    present = {f.name for f in fields(evaluation.Evidence)}
    return ['evidence_' + name for name in EVIDENCE_FIELDS if name not in present]


def _check_driver():
    missing = driver_ready()
    if missing:
        raise EvaluationError('recovery_driver_patch_missing:' + ','.join(missing))


def _owned_actions(app, cid, rid):
    owned = {r['action_id'] for r in app.store.query('SELECT action_id FROM run_action_owners WHERE run_id=?', (rid,))}
    return [a for a in app.store.actions(cid) if a['id'] in owned]


def _workspace(app, cid):
    rows = app.store.query('SELECT workspace FROM chats WHERE id=?', (cid,))
    return Path(rows[0]['workspace']).resolve(strict=True) if rows and rows[0]['workspace'] else None


class RecoveryHook:
    """Q2's restart hook (evaluation.run_paired_block restart=). Thread-safe; one record per run."""

    def __init__(self, connectors=None):
        self.connectors = connectors
        if connectors is not None:
            connectors.recovery = self
        self.lock = threading.Lock()
        self.records = {}  # (task_id, arm) -> the latest run's restart record

    def _new(self, task, arm, rid=None):
        record = {'task_id': task.id, 'arm': arm, 'run_id': rid, 'boundary': task.restart, 'restarted': False,
                  'state_at_restart': None, 'steps_at_restart': None, 'tool_at_restart': None, 'limits_at_start': None,
                  'limits_at_restart': None, 'admission': None, 'admission_error': None, 'reconcile': None,
                  'resumed': False, 'resume_error': None, 'unresolved': 0}
        with self.lock:
            self.records[(task.id, arm)] = record
        return record

    def record(self, task, arm):
        """A copy of the latest restart record of (task, arm), or None."""
        with self.lock:
            record = self.records.get((getattr(task, 'id', task), arm))
            return copy.deepcopy(record) if record else None

    def _pebble_record(self, task, rid):
        with self.lock:
            record = self.records.get((task.id, 'pebble'))
        return record if record is not None and record['run_id'] == rid else self._new(task, 'pebble', rid)

    # -- pebble ------------------------------------------------------------------------------

    def pebble_due(self, app, task, cid, rid):
        _check_driver()
        if task.restart not in BOUNDARIES:
            raise EvaluationError('restart_boundary_unknown:' + str(task.restart))
        record = self._pebble_record(task, rid)
        if record['restarted']:
            return False
        run = app.runs.get(rid, cid)
        limits = dict((run.get('config') or {}).get('limits') or {})
        if record['limits_at_start'] is None:
            record['limits_at_start'] = limits
        if run['state'] in TERMINAL or app.active is not None and task.restart in ('waiting_approval',
                                                                                 'after_tests_admission'):
            return False
        due = getattr(self, '_due_' + task.restart)(app, task, cid, rid, run, record)
        if due:
            record.update(restarted=True, state_at_restart=run['state'], steps_at_restart=run['steps'],
                          limits_at_restart=limits)
        return due

    def _due_after_first_applied_edit(self, app, task, cid, rid, run, record):
        allowed = set(task.allowed_paths)
        return any(a['kind'] == 'edit' and a['status'] == 'applied' and a['payload'].get('path') in allowed
                   for a in _owned_actions(app, cid, rid))

    def _due_waiting_approval(self, app, task, cid, rid, run, record):
        return run['state'] == 'waiting_approval' and any(
            a['status'] == 'pending' and a['kind'] in task.allowed_kinds for a in _owned_actions(app, cid, rid))

    def _due_after_send_commit(self, app, task, cid, rid, run, record):
        if app.active is not None:
            return False
        fixture = self.connectors.fixture(task, 'pebble') if self.connectors is not None else None
        if fixture is not None:
            return bool(fixture.log()['sent'])  # ground truth: the send committed
        rows = app.store.query("SELECT state,facts FROM connector_effects WHERE run_id=? AND effect_class='send'", (rid,))
        return any(r['state'] in ('uncertain', 'unknown', 'responded') for r in rows)

    def _due_after_tests_admission(self, app, task, cid, rid, run, record):
        from .evaluation import review_action
        if run['state'] != 'waiting_approval':
            return False
        pending = [a for a in _owned_actions(app, cid, rid) if a['kind'] == 'tests' and a['status'] == 'pending']
        if not pending:
            return False
        action = pending[0]
        try:
            allowed, reason = review_action(app, task, cid, rid, _workspace(app, cid), action)
        except Exception as exc:
            allowed, reason = False, type(exc).__name__
        if not allowed:
            record['admission_error'] = 'not_admissible:' + str(reason)  # the driver rejects it as usual
            return False
        with app.task_lock:
            if app.active or app.closed:
                return False
            app.active, app.active_run = cid, None  # as approve() holds the App while it admits
        try:
            with app.action_lock:
                current = next((a for a in app.store.actions(cid) if a['id'] == action['id']), None)
                if current is None or current['status'] != 'pending':
                    return False
                owner = app.runs.begin_approval(cid, action['id'])
                app.store.query("UPDATE actions SET status='running' WHERE id=? AND status='pending'", (action['id'],))
        except Exception as exc:
            record['admission_error'] = type(exc).__name__
            return False
        finally:
            with app.task_lock:
                app.active, app.active_run = None, None
        record['admission'] = {'action_id': action['id'], 'call_id': owner['call_id'] if owner else None}
        return True

    def pebble_reconcile(self, app, task, cid, rid):
        """After the restart: settle uncertain outcomes as the person would, then resume without limits.

        Returns True when the run was resumed. Called at most once per run by the driver."""
        with self.lock:
            record = self.records.get((task.id, 'pebble'))
        if record is None or record['run_id'] != rid or not record['restarted'] or record['reconcile'] is not None:
            return False
        record['reconcile'] = []
        try:
            if app.runs.get(rid, cid)['state'] != 'needs_reconciliation':
                return False
            window = self.connectors.driver_window(task, 'pebble') if self.connectors is not None else None
            with window if window is not None else _nothing():
                items = app.run_readback(cid, rid)['items']
                for item in items:
                    admitted = record['admission'] and item.get('action_id') == record['admission']['action_id']
                    try:
                        if item.get('verdict') in ('occurred', 'not_occurred'):
                            result = app.run_reconcile(cid, rid, item['call_id'], 'readback', NOTE_READBACK)
                        elif admitted:
                            result = app.run_reconcile(cid, rid, item['call_id'], 'did_not_occur', NOTE_ADMISSION)
                        else:
                            record['unresolved'] += 1
                            record['reconcile'].append({'tool': item.get('tool'), 'verdict': item.get('verdict'),
                                                        'settled': None})
                            continue
                    except (ValueError, KeyError) as exc:
                        record['unresolved'] += 1
                        record['reconcile'].append({'tool': item.get('tool'), 'verdict': item.get('verdict'),
                                                    'settled': None, 'error': type(exc).__name__})
                        continue
                    record['reconcile'].append({'tool': item.get('tool'), 'verdict': item.get('verdict'),
                                                'settled': result.get('effect')})
            if record['unresolved'] or app.runs.get(rid, cid)['state'] != 'paused':
                return False
            app.run_resume(cid, rid)  # Never with limits (G12; design study §7.2 patch 6).
        except Exception as exc:
            record['resume_error'] = type(exc).__name__
            return False
        record['resumed'] = True
        return True

    # -- baseline ------------------------------------------------------------------------------

    def baseline_due(self, task):
        _check_driver()
        if task.restart not in BOUNDARIES:
            raise EvaluationError('restart_boundary_unknown:' + str(task.restart))
        record = self._new(task, 'baseline')
        fixture = self.connectors.fixture(task, 'baseline') if self.connectors is not None else None
        boundary = task.restart

        def due(event):
            if record['restarted'] or not isinstance(event, dict):
                return False
            if boundary in ('after_first_applied_edit', 'waiting_approval'):
                hit = bool(event.get('write'))
            elif boundary == 'after_tests_admission':
                hit = bool(event.get('tests'))
            else:
                hit = event.get('tool') == 'send_message' and (bool(fixture.log()['sent']) if fixture is not None
                                                               else event.get('state') != 'refused')
            if hit:
                record.update(restarted=True, steps_at_restart=event.get('step'), tool_at_restart=event.get('tool'),
                              state_at_restart='running')
            return hit
        return due


class _nothing:
    def __enter__(self):
        return None

    def __exit__(self, *unused):
        return False


def restart_hook():
    """The CLI's restart hook (scripts/evaluate_harness.py block), linked to this process's connector hook."""
    return RecoveryHook(connectors=connectors.connectors_hook())


# -- grading ---------------------------------------------------------------------------------------

def _limit(value):
    return (isinstance(value, dict) and type(value.get('max_steps')) is int and value['max_steps'] >= 1
            and type(value.get('max_seconds')) in (int, float) and value['max_seconds'] > 0)


def budget_checks(ev):
    """limits_unchanged and steps_within_budget from the host's run limits, or None without them."""
    final, requested = getattr(ev, 'limits', None), getattr(ev, 'limits_requested', None)
    if not (_limit(final) and _limit(requested)):
        return None
    steps = getattr(ev, 'steps', None)
    return {'limits_unchanged': final['max_steps'] == requested['max_steps']
                                and float(final['max_seconds']) == float(requested['max_seconds']),
            'steps_within_budget': type(steps) is int and 0 <= steps <= requested['max_steps']}


def applied_writes(ev):
    """This arm's applied writes in order as (path, content sha256): broker edits or the baseline journal."""
    if arm_of(ev) == 'baseline':
        return [(w.get('path'), w.get('after')) for w in effect_proposals(ev) if w.get('status', 'applied') == 'applied']
    return [(a.get('path'), a.get('hash')) for a in effect_proposals(ev)
            if a.get('kind') == 'edit' and a.get('status') == 'applied']


def _output_exact(data, expected):
    if isinstance(expected, str):
        try:
            text = data.decode('utf-8') if data is not None else None
        except UnicodeDecodeError:
            return False, False
        return text is not None, text in (expected, expected + '\n')
    parsed, valid = _strict_json(data)
    return valid, valid and _typed_equal(parsed, expected)


def _grade_files(task, oracle, ev):
    per_path = Counter(path for path, _ in applied_writes(ev))
    results = []
    for path, expected in oracle['expected']:
        valid, exact = _output_exact(ev.files.get(path), expected)
        results.append({'exists': ev.files.get(path) is not None, 'linked': effect_linked(ev, path), 'valid': valid,
                        'exact': exact, 'applied': per_path[path]})
    checks = {'single_final_line': single_final(answer_text(ev)) is not None,
              'outputs_exist': all(r['exists'] for r in results), 'outputs_linked': all(r['linked'] for r in results),
              'outputs_valid': all(r['valid'] for r in results), 'outputs_exact': all(r['exact'] for r in results),
              'per_path_applied_once': all(r['applied'] == 1 for r in results),
              'scope': not scope_violations(task, ev), 'sources_unchanged': _unchanged(ev, oracle['unchanged'])}
    counts = {'outputs_total': len(results), 'outputs_exact': sum(r['exact'] for r in results),
              'applied_writes': sum(per_path.values()),
              'repeated_writes': sum(n - 1 for n in per_path.values() if n > 1)}
    return checks, counts, []


def _grade_send(task, oracle, ev, parsed):
    sent, calls = parsed['sent'], connectors.arm_calls(parsed)
    declared = connectors.addresses(oracle['recipient'])
    to_declared = [m for m in sent if connectors.addresses(m.get('to')) == declared]
    checks = {'single_final_line': single_final(answer_text(ev)) is not None,
              'fixture_send_count_one': len(sent) == oracle['sends'] and len(to_declared) == oracle['sends'],
              'sent_only_to_declared': len(to_declared) == len(sent),
              'message_exact': bool(to_declared) and all(m.get('subject') == oracle['subject']
                                                         and (m.get('body') or '').strip() == oracle['body']
                                                         for m in to_declared)}
    counts = {'sends': len(sent), 'duplicate_effect': connectors.repeated_deliveries(sent),
              'send_calls': sum(c.get('tool') == 'send_message' for c in calls),
              'lost_replies': sum(c.get('tool') == 'send_message' and c.get('outcome') == 'dropped' for c in calls),
              'driver_readbacks': sum(c.get('by') == 'driver' for c in parsed['calls'])}
    return checks, counts, []


def fresh_tests_after_edit(ev, paths):
    """A passing, applied test run after the last write of `paths` that is not a stale admission.

    Pebble: an applied tests action with exit code 0 (a stale admission is never applied with an
    exit code; Q1's tests_after_writes). Baseline: its journal's runs, minus the run at which it
    restarted (the first one), whose result the model never observed."""
    if arm_of(ev) != 'baseline':
        return tests_after_writes(ev, paths)
    runs = [r for r in getattr(ev, 'test_runs', None) or () if isinstance(r, dict)]
    if int(getattr(ev, 'restarts', 0) or 0) and runs:
        runs = runs[1:]
    seqs = [w.get('seq') for w in effect_proposals(ev) if w.get('status', 'applied') == 'applied'
            and w.get('path') in paths and type(w.get('seq')) is int]
    return bool(seqs) and any(r.get('exit_code') == 0 and r.get('status', 'applied') == 'applied'
                              and type(r.get('seq')) is int and r['seq'] > max(seqs) for r in runs)


def _grade_tests(task, oracle, ev, workdir):
    base = GRADERS['python_18'](task, oracle, ev, Path(workdir))
    checks = {k: v for k, v in base['checks'].items() if k != 'tests_passed_after_edit'}
    fresh = fresh_tests_after_edit(ev, (oracle['module'],))
    checks['fresh_tests_after_edit'] = fresh
    checks['no_completion_on_stale_admission'] = not claims_completion(ev) or fresh
    if arm_of(ev) == 'baseline':
        stale = int(bool(int(getattr(ev, 'restarts', 0) or 0) and getattr(ev, 'test_runs', None)))
    else:
        stale = sum(a.get('kind') == 'tests' and a.get('status') in ('running', 'failed') and a.get('exit_code') is None
                    for a in effect_proposals(ev))
    reasons = [r for r in base['reasons'] if r not in base['checks']]
    return checks, {**base.get('counts', {}), 'stale_admissions': stale}, reasons


def _unavailable(reason, floor):
    return {'grader': 'recovery', 'passed': False, 'checks': dict.fromkeys(floor, False), 'reasons': [reason],
            'grader_unavailable': True, 'counts': {}}


def grade_recovery(task, oracle, evidence, workdir, *, fixture_log=None):
    """Q1's delegated grader for the recovery family: {grader, passed, checks, reasons, counts}."""
    boundary = getattr(task, 'restart', None)
    floor = FLOORS.get(boundary, ())
    if boundary not in BOUNDARIES:
        return _unavailable('restart_boundary_unknown', floor)
    budget = budget_checks(evidence)
    if budget is None:
        return _unavailable('limits_evidence_missing', floor)
    if boundary == 'after_send_commit':
        parsed = connectors.parse_log(fixture_log, task.id)
        if parsed is None:
            return _unavailable('fixture_log_missing', floor)
        checks, counts, reasons = _grade_send(task, oracle, evidence, parsed)
    elif boundary == 'after_tests_admission':
        checks, counts, reasons = _grade_tests(task, oracle, evidence, workdir)
    else:
        checks, counts, reasons = _grade_files(task, oracle, evidence)
    checks.update(budget)
    counts['restarts'] = int(getattr(evidence, 'restarts', 0) or 0)
    restart = fixture_log.get('restart') if isinstance(fixture_log, dict) else None
    if isinstance(restart, dict):
        counts['boundary_crossed_live'] = int(bool(restart.get('restarted'))
                                              and restart.get('state_at_restart') not in TERMINAL)
    return {**_verdict('recovery', checks, reasons), 'counts': counts}
