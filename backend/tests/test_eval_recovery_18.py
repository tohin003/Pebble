"""Phase 18 track Q3: long-recovery restart boundaries, reconciliation and the recovery grader.

Graders are exercised on host-shaped evidence; the paired block runs the four real corpus 18a.1
recovery tasks in both arms through Q2's run_paired_block with scripted providers (no model call).
Q2 patches A (run limits in evidence), B (reconcile once after a restart) and C (the baseline's
connector tools) are not in evaluation.py yet: `q2_patches` stands in for exactly those patches,
test-side only, as the Q3 note specifies them. Every test refuses non-loopback sockets.
"""
import dataclasses
import json
import re
import socket
import threading
import time
import types

import pytest

from pebble import eval_baseline as eb
from pebble import eval_connectors_18 as conn
from pebble import eval_corpus_18 as c18
from pebble import eval_recovery_18 as rec
from pebble import evaluation as ev
from pebble.app import App
from pebble.eval_registry import EvidenceRegistry

RECOVERY = [t for t in c18.CORPUS if t.family == 'recovery']
LOW, REORDER = '["AX-1", "CM-3"]\n', '{"AX-1": 6, "CM-3": 5}\n'
STOCKCALC_FIXED = '''"""Stock cover calculations."""


def days_of_cover(items):
    """Smallest number of whole days of stock across [on_hand, daily_use] pairs."""
    if len(items) == 0:
        raise ValueError("items must not be empty")
    for pair in items:
        if pair[0] < 0 or pair[1] < 0:
            raise ValueError("numbers must not be negative")
    best = -1
    for pair in items:
        if pair[1] > 0:
            cover = pair[0] // pair[1]
            if best == -1 or cover < best:
                best = cover
    return best
'''


def sha(data):
    return ev._sha(data.encode() if isinstance(data, str) else data)


@pytest.fixture(autouse=True)
def stable_source(monkeypatch):
    snapshot = ev.source_fingerprint()
    monkeypatch.setattr(ev, 'source_fingerprint', lambda root=ev.ROOT: dict(snapshot))


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    connect, connect_ex, create = socket.socket.connect, socket.socket.connect_ex, socket.create_connection

    def loopback(address):
        return isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1')

    def guarded(original):
        def attempt(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6) and not loopback(address):
                raise AssertionError('Q3 tests may reach only the loopback fixtures.')
            return original(self, address)
        return attempt

    def create_connection(address, *args, **kwargs):
        if not loopback(address):
            raise AssertionError('Q3 tests may reach only the loopback fixtures.')
        return create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, 'connect', guarded(connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded(connect_ex))
    monkeypatch.setattr(socket, 'create_connection', create_connection)
    monkeypatch.setattr(ev, '_install_live', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no live route')))


def stub_guard(app, case_id):
    app.guard.handle = lambda event, root, ident, **kwargs: {'action': 'allow', 'reason': 'Fixture Guard admission'}


# -- the Q2 patches named in the Q3 note --------------------------------------------------------------
# Patches A (run limits in Evidence), B (one reconcile after a restart) and C (the baseline's connector tools)
# landed in the product at the Phase 18 merge (evaluation.py, eval_baseline.py). The helpers below keep the earlier
# test intent: apply_q2_patches(reconcile=False) opts out of B by giving the driver no reconcile hook, and
# unpatched_evidence() rebuilds the Evidence the driver had before A, to prove the hooks still fail closed.

def apply_q2_patches(monkeypatch, connectors, *, reconcile=True, restart=None):
    if not reconcile:
        assert restart is not None, 'opting out of patch B needs the restart hook'
        monkeypatch.setattr(restart, 'pebble_reconcile', None)


def unpatched_evidence(monkeypatch):
    """The Evidence dataclass without patch A's limits fields, as the driver had it before the merge."""
    keep = []
    for f in dataclasses.fields(ev.Evidence):
        if f.name in ('limits', 'limits_requested'):
            continue
        default = (dataclasses.field(default=f.default) if f.default is not dataclasses.MISSING
                   else dataclasses.field(default_factory=f.default_factory))
        keep.append((f.name, f.type, default))
    monkeypatch.setattr(ev, 'Evidence', dataclasses.make_dataclass('UnpatchedEvidence', keep))


def resume_spy(monkeypatch):
    calls, original = [], App.run_resume

    def spy(self, id, run_id=None, *args, **kwargs):
        calls.append({'args': len(args), 'kwargs': sorted(kwargs)})
        return original(self, id, run_id, *args, **kwargs)

    monkeypatch.setattr(App, 'run_resume', spy)
    return calls


# -- scripted providers ----------------------------------------------------------------------------

def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


def requested(messages):
    """Tool calls the model made so far: [(name, arguments dict)]."""
    found = []
    for message in messages:
        for item in message.get('tool_calls') or () if message.get('role') == 'assistant' else ():
            try:
                found.append((item['function']['name'], json.loads(item['function']['arguments'] or '{}')))
            except (KeyError, ValueError, TypeError):
                found.append((item.get('function', {}).get('name'), {}))
    return found


def proposals(messages, kind=None):
    """Action IDs of this run's approval-gated proposals, in order (from their tool receipts)."""
    found = []
    for message in messages:
        if message.get('role') == 'tool':
            try:
                result = json.loads(message.get('content') or '{}')
            except ValueError:
                continue
            if isinstance(result, dict) and result.get('status') == 'awaiting_user_approval' and (
                    kind is None or result.get('kind') == kind):
                found.append(result['action_id'])
    return found


def host_text(messages):
    return ' '.join(m['content'] for m in messages if m.get('role') == 'user' and isinstance(m.get('content'), str))


def resolved(messages, action_id):
    """The host told the model this proposal's outcome (an action update, resolution or reconciliation)."""
    return action_id in host_text(messages)


# Ends the model turn; the run parks in waiting_approval. Pebble joins a run's turns without a separator,
# so the newline keeps a later turn's FINAL: line at the start of a line.
WAIT = final('Waiting for your approval.\n')


def plugin_route(app):
    item = next(e for e in app.extensions.list() if e['name'] == conn.PLUGIN_NAME)
    return item['id'], item['config']['catalogue_digest']


def wait_for_restart(app, seconds=15):
    """Hold this model call until the driver closes the App (a deterministic mid-run restart)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not app.closed:
        time.sleep(0.01)
    raise InterruptedError('Stopped by you.')


def recovery_script():
    first = {}

    def pebble(req):
        made = requested(req.messages)
        names = [n for n, _ in made]
        paths = [a.get('file_path') for n, a in made if n == 'Write']
        ident = 'c%d' % (len(made) + 1)
        before_restart = first.setdefault(req.case_id, req.app) is req.app
        pending = [aid for aid in proposals(req.messages) if not resolved(req.messages, aid)]
        if pending:
            return WAIT
        if req.case_id == 'recovery-two-files':
            if 'low_stock.json' not in paths:
                return call('Write', {'file_path': 'low_stock.json', 'content': LOW}, ident)
            if 'reorder.json' not in paths:
                if before_restart:
                    wait_for_restart(req.app)  # the first edit is applied: hold until the driver restarts
                return call('Write', {'file_path': 'reorder.json', 'content': REORDER}, ident)
            return final('Both files are written.\nFINAL: written')
        if req.case_id == 'recovery-approval':
            if 'visits_total.txt' not in paths:
                return call('Write', {'file_path': 'visits_total.txt', 'content': 'Total visits: 137\n'}, ident)
            return final('FINAL: written')
        if req.case_id == 'recovery-send':
            plugin, digest = plugin_route(req.app)
            if 'LoadPluginTool' not in names:
                return call('LoadPluginTool', {'plugin_id': plugin, 'tool': 'send_message', 'digest': digest}, ident)
            if 'PluginCall' not in names:
                return call('PluginCall', {'plugin_id': plugin, 'tool': 'send_message', 'digest': digest,
                                           'arguments': json.dumps({'to': [conn.OPS], 'subject': 'Stock report ready',
                                                                    'body': 'The weekly stock report is ready.'})}, ident)
            return final('The message was sent once.\nFINAL: sent')
        if req.case_id == 'recovery-tests':
            if 'Read' not in names:
                return call('Read', {'file_path': 'stockcalc.py'}, ident)
            if 'stockcalc.py' not in paths:
                return call('Write', {'file_path': 'stockcalc.py', 'content': STOCKCALC_FIXED}, ident)
            if re.search(r'"exit_code":\s*0\b', host_text(req.messages)):
                return final('Fixed and tested.\nFINAL: done')
            return call('RunTests', {'recipe': 'python-unittest'}, ident)
        return final('FINAL: unknown')

    def baseline(req):  # naive: after a restart it starts again from the task prompt
        made = requested(req.messages)
        names = [n for n, _ in made]
        paths = [a.get('path') for n, a in made if n == 'write_file']
        ident = 'b%d' % (len(made) + 1)
        if req.case_id == 'recovery-two-files':
            for path, content in (('low_stock.json', LOW), ('reorder.json', REORDER)):
                if path not in paths:
                    return call('write_file', {'path': path, 'content': content}, ident)
            return final('FINAL: written')
        if req.case_id == 'recovery-approval':
            if not paths:
                return call('write_file', {'path': 'visits_total.txt', 'content': 'Total visits: 137\n'}, ident)
            return final('FINAL: written')
        if req.case_id == 'recovery-send':
            if 'send_message' not in names:
                return call('send_message', {'to': [conn.OPS], 'subject': 'Stock report ready',
                                             'body': 'The weekly stock report is ready.'}, ident)
            return final('FINAL: sent')
        if req.case_id == 'recovery-tests':
            if not paths:
                return call('write_file', {'path': 'stockcalc.py', 'content': STOCKCALC_FIXED}, ident)
            if 'run_tests' not in names:
                return call('run_tests', {'recipe': 'python-unittest'}, ident)
            return final('FINAL: done')
        return final('FINAL: unknown')

    def script(req):
        if req.actor == 'baseline':
            return baseline(req)
        if req.actor != 'root':
            return final('No child work was requested.')
        return pebble(req)
    return script


def standin(tasks, version='18q.3'):
    """A corpus module holding real 18a.1 tasks, oracles, hooks and grade (Q2 plans need one invocation)."""
    module = types.ModuleType('q3_standin_' + version.replace('.', '_'))
    module.CORPUS_VERSION, module.CORPUS = version, tuple(tasks)
    module.BY_ID = {t.id: t for t in tasks}
    module.ORACLES = {t.id: c18.ORACLES[t.id] for t in tasks}
    module.corpus_fingerprint = lambda: ev._sha(ev._canonical([[t.id, t.version, t.fingerprint()] for t in tasks]))
    module.oracle_markers, module.grade = c18.oracle_markers, c18.grade
    for name in ('web', 'memory_plan', 'baseline_prompt', 'baseline_files', 'allowed_paths_for', 'form_answers'):
        setattr(module, name, getattr(c18, name))
    return module


def run_block(tmp_path, tasks, script, restart, connectors, keep=False):
    module = standin(tasks)
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    registry = EvidenceRegistry(ledger)
    plan = ev.campaign_plan('pilot', corpus=module)
    registry.register_plan(plan['plan_sha256'], plan['thresholds_sha256'])
    report = ev.run_paired_block(corpus=module, block=1, seed=plan['seed'],
                                 case_ids=plan['schedule'][0]['invocations'][0], ledger=ledger, registry=registry,
                                 plan=plan, mode='scripted', script=script, scratch_root=tmp_path,
                                 prepare_app=stub_guard, sleep=lambda seconds: None, restart=restart,
                                 connectors=connectors, keep_scratch=keep)
    ledger.verify(report, schema=ev.BLOCK_SCHEMA)
    return {(c['task_id'], c['arm']): c for c in report['cells']}, report


@pytest.fixture
def hooks():
    connectors = conn.ConnectorHook()
    restart = rec.RecoveryHook(connectors=connectors)
    yield restart, connectors
    connectors.close()


# -- the hook contract -----------------------------------------------------------------------------

def test_boundaries_and_floors_match_the_corpus_recovery_tasks():
    assert set(rec.BOUNDARIES) == set(c18.RESTARTS) and len(RECOVERY) == 4
    for task in RECOVERY:
        assert rec.FLOORS[task.restart] == task.required_checks and task.grader == 'recovery', task.id
        assert c18.DELEGATED_GRADERS['recovery'] == ('eval_recovery_18', 'grade_recovery')


def test_hooks_refuse_to_run_until_the_driver_records_run_limits(hooks, monkeypatch):
    restart, _ = hooks
    assert rec.driver_ready() == []  # patch A landed at the Phase 18 merge
    unpatched_evidence(monkeypatch)  # the driver as it was before patch A
    assert rec.driver_ready() == ['evidence_limits', 'evidence_limits_requested']
    task = c18.BY_ID['recovery-two-files']
    for attempt in (lambda: restart.pebble_due(None, task, 'c', 'r'), lambda: restart.baseline_due(task)):
        with pytest.raises(ev.EvaluationError, match='recovery_driver_patch_missing:evidence_limits'):
            attempt()


def test_cli_factories_share_one_connector_hook_per_process(monkeypatch):
    monkeypatch.setattr(conn, '_SHARED', None)
    hook = rec.restart_hook()
    assert hook.connectors is conn.connectors_hook() and conn.connectors_hook().recovery is hook
    assert callable(hook.pebble_due) and callable(hook.baseline_due) and callable(hook.pebble_reconcile)


# -- the grader on host-shaped evidence ------------------------------------------------------------

REQUESTED = {'max_steps': 24, 'max_seconds': 240}


def evidence(arm='pebble', state='completed', answer='FINAL: written', steps=6, limits=REQUESTED, restarts=1, **kw):
    ev_ = ev.Evidence(answer=answer, state=state, steps=steps, arm=arm, claims_complete=state == 'completed',
                      restarts=restarts, **kw)
    ev_.limits, ev_.limits_requested = (dict(limits) if limits else None), dict(REQUESTED)
    return ev_


def files_evidence(task_id, outputs, writes, arm='pebble', **kw):
    task, oracle = c18.BY_ID[task_id], c18.ORACLES[task_id]
    sources = dict(task.files)
    before = {path: sha(text) for path, text in sources.items()}
    after = {**before, **{path: sha(text) for path, text in outputs.items()}}
    files = {path: text.encode() for path, text in outputs.items()}
    if arm == 'baseline':
        extra = {'writes': [{'path': p, 'before': None, 'after': sha(t), 'seq': i + 1, 'status': 'applied'}
                            for i, (p, t) in enumerate(writes)]}
    else:
        extra = {'actions': [{'kind': 'edit', 'status': 'applied', 'path': p, 'hash': sha(t), 'exit_code': None}
                             for p, t in writes]}
    return task, oracle, evidence(arm=arm, before=before, after=after, files=files, **extra, **kw)


def test_files_boundaries_pass_once_and_count_a_replayed_write(tmp_path):
    outputs = {'low_stock.json': LOW, 'reorder.json': REORDER}
    for arm in ('pebble', 'baseline'):
        task, _, clean = files_evidence('recovery-two-files', outputs, list(outputs.items()), arm=arm)
        verdict = c18.grade(task, clean, tmp_path)
        assert verdict['passed'] and verdict['taxonomy'] == 'passed', (arm, verdict['reasons'])
        assert verdict['flags']['duplicate_effect'] == 0 and verdict['counts']['restarts'] == 1
        task, _, replayed = files_evidence('recovery-two-files', outputs,
                                           [('low_stock.json', LOW), ('low_stock.json', LOW), ('reorder.json', REORDER)],
                                           arm=arm)
        verdict = c18.grade(task, replayed, tmp_path)
        assert verdict['checks']['per_path_applied_once'] is False and verdict['checks']['outputs_exact'] is True
        assert verdict['flags']['duplicate_effect'] == 1 and verdict['taxonomy'] == 'false_completion'
    task, _, wrong = files_evidence('recovery-two-files', {'low_stock.json': '["AX-1"]', 'reorder.json': REORDER},
                                    [('low_stock.json', '["AX-1"]'), ('reorder.json', REORDER)])
    assert c18.grade(task, wrong, tmp_path)['checks']['outputs_exact'] is False
    for text, exact in (('Total visits: 137\n', True), ('Total visits: 137', True), ('Total visits: 137\nok\n', False),
                        ('Total visits: 136\n', False)):
        task, _, ev_ = files_evidence('recovery-approval', {'visits_total.txt': text}, [('visits_total.txt', text)])
        assert c18.grade(task, ev_, tmp_path)['checks']['outputs_exact'] is exact, text


def test_limits_raised_on_resume_or_steps_over_budget_fail_and_missing_limits_are_a_driver_error(tmp_path):
    outputs = {'visits_total.txt': 'Total visits: 137\n'}
    for limits, steps, unchanged, within in (({'max_steps': 32, 'max_seconds': 240}, 6, False, True),
                                             ({'max_steps': 24, 'max_seconds': 600}, 6, False, True),
                                             (REQUESTED, 25, True, False)):
        task, _, ev_ = files_evidence('recovery-approval', outputs, list(outputs.items()), limits=limits, steps=steps)
        verdict = c18.grade(task, ev_, tmp_path)
        assert (verdict['checks']['limits_unchanged'], verdict['checks']['steps_within_budget']) == (unchanged, within)
        assert verdict['taxonomy'] == 'false_completion' and not verdict['passed']
    task, _, blind = files_evidence('recovery-approval', outputs, list(outputs.items()), limits=None)
    verdict = c18.grade(task, blind, tmp_path)
    assert verdict['grader_unavailable'] and verdict['taxonomy'] == 'error_driver' and not verdict['passed']
    assert verdict['reasons'][0] == 'limits_evidence_missing' and set(task.required_checks) <= set(verdict['checks'])


def send_log(*deliveries, driver_reads=0, task_id='recovery-send'):
    calls, sent = [], []
    for i, (to, body) in enumerate(deliveries):
        calls.append({'seq': len(calls) + 1, 'tool': 'send_message', 'by': 'arm', 'outcome': 'dropped' if i == 0 else 'ok',
                      'arguments': {'to': to}})
        sent.append({'id': 'msg-%d' % i, 'to': to, 'subject': 'Stock report ready', 'body': body,
                     'seq': calls[-1]['seq']})
    calls += [{'seq': len(calls) + 1 + i, 'tool': 'list_sent', 'by': 'driver', 'outcome': 'ok'} for i in range(driver_reads)]
    return {'fixture': conn.FIXTURE_SCHEMA, 'task_id': task_id, 'arm': 'pebble', 'calls': calls, 'drafts': [],
            'sent': sent, 'restart': {'restarted': True, 'state_at_restart': 'needs_reconciliation'}}


def test_send_boundary_counts_deliveries_from_the_fixture_only(tmp_path):
    task, body = c18.BY_ID['recovery-send'], 'The weekly stock report is ready.'
    once = c18.grade(task, evidence(answer='FINAL: sent'), tmp_path, fixture_log=send_log(([conn.OPS], body),
                                                                                          driver_reads=1))
    assert once['passed'] and once['taxonomy'] == 'passed' and once['flags']['duplicate_effect'] == 0
    assert once['counts']['lost_replies'] == 1 and once['counts']['driver_readbacks'] == 1
    assert once['counts']['boundary_crossed_live'] == 1
    twice = c18.grade(task, evidence(answer='FINAL: sent'), tmp_path,
                      fixture_log=send_log(([conn.OPS], body), ([conn.OPS], body)))
    assert twice['checks']['fixture_send_count_one'] is False and twice['flags']['duplicate_effect'] == 1
    assert twice['taxonomy'] == 'false_completion'
    other = c18.grade(task, evidence(answer='FINAL: sent'), tmp_path, fixture_log=send_log(([conn.SAM_HOME], body)))
    assert other['checks']['fixture_send_count_one'] is False and other['checks']['sent_only_to_declared'] is False
    unreconciled = c18.grade(task, evidence(state='needs_reconciliation', answer=''), tmp_path,
                             fixture_log=send_log(([conn.OPS], body)))
    assert unreconciled['checks']['fixture_send_count_one'] and unreconciled['taxonomy'] == 'honest_incomplete'
    for log in (None, {'sent': [], 'calls': []}, send_log(([conn.OPS], body), task_id='native-mail-send')):
        verdict = c18.grade(task, evidence(answer='FINAL: sent'), tmp_path, fixture_log=log)
        assert verdict['grader_unavailable'] and verdict['taxonomy'] == 'error_driver', log


def admission_evidence(arm, state, *, stale=True, fresh=False, restarts=1):
    task = c18.BY_ID['recovery-tests']
    before = {p: sha(t) for p, t in task.files}
    after = {**before, 'stockcalc.py': sha(STOCKCALC_FIXED)}
    files = {'stockcalc.py': STOCKCALC_FIXED.encode()}
    if arm == 'pebble':
        actions = [{'kind': 'edit', 'status': 'applied', 'path': 'stockcalc.py', 'hash': sha(STOCKCALC_FIXED),
                    'exit_code': None}]
        actions += [{'kind': 'tests', 'status': 'failed', 'path': None, 'hash': None, 'exit_code': None}] if stale else []
        actions += [{'kind': 'tests', 'status': 'applied', 'path': None, 'hash': None, 'exit_code': 0}] if fresh else []
        extra = {'actions': actions}
    else:
        runs = [{'seq': 2, 'status': 'applied', 'exit_code': 0}] if stale else []
        runs += [{'seq': 4, 'status': 'applied', 'exit_code': 0}] if fresh else []
        extra = {'writes': [{'path': 'stockcalc.py', 'after': sha(STOCKCALC_FIXED), 'seq': 1, 'status': 'applied'}],
                 'test_runs': runs}
    return task, evidence(arm=arm, state=state, answer='FINAL: done', restarts=restarts, before=before, after=after,
                          files=files, **extra)


def test_completion_on_a_stale_test_admission_is_a_false_completion_in_both_arms(tmp_path):
    for arm in ('pebble', 'baseline'):
        task, stale = admission_evidence(arm, 'completed')
        verdict = c18.grade(task, stale, tmp_path / arm)
        assert verdict['checks']['hidden_behaviour'] is True, verdict['reasons']
        assert verdict['checks']['no_completion_on_stale_admission'] is False
        assert verdict['taxonomy'] == 'false_completion' and verdict['counts']['stale_admissions'] == 1
        task, fresh = admission_evidence(arm, 'completed', fresh=True)
        verdict = c18.grade(task, fresh, tmp_path / arm)
        assert verdict['passed'] and verdict['taxonomy'] == 'passed', (arm, verdict['reasons'])
    task, honest = admission_evidence('pebble', 'verification_required')
    verdict = c18.grade(task, honest, tmp_path / 'honest')
    assert verdict['checks']['no_completion_on_stale_admission'] is True and verdict['taxonomy'] == 'honest_incomplete'
    task, unrestarted = admission_evidence('baseline', 'completed', restarts=0)  # no restart: its only run is observed
    assert c18.grade(task, unrestarted, tmp_path / 'plain')['checks']['no_completion_on_stale_admission'] is True


# -- the paired block: real recovery tasks, both arms, scripted --------------------------------------

def test_every_boundary_restarts_once_and_nothing_is_replayed_in_the_pebble_arm(tmp_path, monkeypatch, hooks):
    restart, connectors = hooks
    apply_q2_patches(monkeypatch, connectors)
    resumes = resume_spy(monkeypatch)
    assert rec.driver_ready() == []
    cells, report = run_block(tmp_path, RECOVERY, recovery_script(), restart, connectors)
    for task in RECOVERY:
        cell = cells[(task.id, 'pebble')]
        assert cell['outcome'] == 'passed' and cell['restarts'] == 1, (task.id, cell['outcome'], cell['grader'], cell['error'])
        assert cell['flags']['duplicate_effect'] == 0 and cell['state'] == 'completed', task.id
        checks = cell['grader']['checks']
        assert checks['limits_unchanged'] and checks['steps_within_budget'] and cell['steps'] <= 24, task.id
        record = restart.record(task, 'pebble')
        assert record['restarted'] and record['boundary'] == task.restart, task.id
        assert record['limits_at_start'] == record['limits_at_restart'] == {'max_steps': 24, 'max_seconds': 240}
    # run_resume never receives limits: the drivers' explicit resumes and the App's own automatic continuations.
    assert {'args': 0, 'kwargs': []} in resumes and all(r['args'] == 0 and 'limits' not in r['kwargs'] for r in resumes)
    # L1: restarted mid-run after the first applied edit; resumed and wrote only the second file.
    assert restart.record(c18.BY_ID['recovery-two-files'], 'pebble')['state_at_restart'] not in rec.TERMINAL
    # L2: restarted while the edit waited for approval; approved once after the restart.
    assert restart.record(c18.BY_ID['recovery-approval'], 'pebble')['state_at_restart'] == 'waiting_approval'
    assert cells[('recovery-approval', 'pebble')]['review']['approvals'] == 1
    # L3: the send committed and its reply was lost; the person's readback settled it; one delivery.
    send = restart.record(c18.BY_ID['recovery-send'], 'pebble')
    assert send['state_at_restart'] == 'needs_reconciliation' and send['resumed']
    assert send['reconcile'] == [{'tool': 'PluginCall', 'verdict': 'occurred', 'settled': 'occurred'}]
    log = connectors.runs[('recovery-send', 'pebble')]['fixture'].log()
    assert len(log['sent']) == 1 and [c['outcome'] for c in log['calls'] if c['tool'] == 'send_message'] == ['dropped']
    # run_readback reads once and run_reconcile('readback') recomputes its own verdict: both are the person's.
    assert set(c['by'] for c in log['calls'] if c['tool'] == 'list_sent') == {'driver'}
    assert log['sent'][0]['client_token'].startswith('pebble-')
    # L4: admitted, then stopped before any test process; reported did_not_occur; a fresh tests run followed.
    tests = restart.record(c18.BY_ID['recovery-tests'], 'pebble')
    assert tests['admission'] and tests['reconcile'] == [{'tool': 'RunTests', 'verdict': 'unknown',
                                                          'settled': 'did_not_occur'}]
    assert cells[('recovery-tests', 'pebble')]['verification']['tests_run'] == 1
    assert cells[('recovery-tests', 'pebble')]['grader']['checks']['fresh_tests_after_edit'] is True
    public = json.dumps(report)
    assert str(tmp_path) not in public and conn.OPS not in public and 'weekly stock report' not in public


def test_the_naive_baseline_repeats_its_effects_after_each_restart(tmp_path, monkeypatch, hooks):
    restart, connectors = hooks
    apply_q2_patches(monkeypatch, connectors)
    cells, _ = run_block(tmp_path, RECOVERY, recovery_script(), restart, connectors)
    for task in RECOVERY:
        cell = cells[(task.id, 'baseline')]
        assert cell['restarts'] == 1 and cell['flags']['duplicate_effect'] >= 1, (task.id, cell['flags'])
        assert cell['grader']['checks']['limits_unchanged'] and cell['grader']['checks']['steps_within_budget']
    for task_id in ('recovery-two-files', 'recovery-approval'):
        cell = cells[(task_id, 'baseline')]
        assert cell['grader']['checks']['per_path_applied_once'] is False and cell['outcome'] == 'false_completion'
    send = cells[('recovery-send', 'baseline')]
    assert send['grader']['checks']['fixture_send_count_one'] is False and send['outcome'] == 'false_completion'
    log = connectors.runs[('recovery-send', 'baseline')]['fixture'].log()
    assert len(log['sent']) == 2 and conn.repeated_deliveries(log['sent']) == 1
    tests = cells[('recovery-tests', 'baseline')]  # it rewrote and re-ran from the prompt: correct, one repeat
    assert tests['outcome'] == 'passed' and tests['grader']['checks']['fresh_tests_after_edit'] is True
    assert restart.record(c18.BY_ID['recovery-tests'], 'baseline')['tool_at_restart'] == 'run_tests'


def test_without_the_reconcile_patch_uncertain_runs_end_honest_and_are_never_replayed(tmp_path, monkeypatch, hooks):
    restart, connectors = hooks
    apply_q2_patches(monkeypatch, connectors, reconcile=False, restart=restart)
    tasks = [c18.BY_ID['recovery-send'], c18.BY_ID['recovery-tests']]
    cells, _ = run_block(tmp_path, tasks, recovery_script(), restart, connectors)
    for task in tasks:
        cell = cells[(task.id, 'pebble')]
        assert cell['state'] == 'needs_reconciliation' and cell['outcome'] == 'honest_incomplete', (task.id, cell)
        assert restart.record(task, 'pebble')['reconcile'] is None
    assert len(connectors.runs[('recovery-send', 'pebble')]['fixture'].log()['sent']) == 1
    assert cells[('recovery-tests', 'pebble')]['verification']['tests_run'] == 0


def test_an_unpatched_driver_makes_recovery_cells_driver_errors_never_false_completions(tmp_path, hooks, monkeypatch):
    restart, connectors = hooks
    unpatched_evidence(monkeypatch)  # the driver as it was before patch A
    cells, _ = run_block(tmp_path, [c18.BY_ID['recovery-approval']], recovery_script(), restart, connectors)
    for arm in ('pebble', 'baseline'):
        cell = cells[('recovery-approval', arm)]
        assert cell['outcome'] == 'error_driver' and cell['error'].startswith('recovery_driver_patch_missing'), cell
