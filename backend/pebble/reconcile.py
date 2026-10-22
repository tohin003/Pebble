"""Authoritative read-only readback for uncertain broker effects (R10).

readback() compares the current authoritative state with the facts an action
recorded before its effect. It never replays, retries, repairs, signals or
writes: it only reads the store, project files through Workspace's anchored
no-follow reads, the repository through GitPlans' confined readback, and
managed environment folders. Verdicts:

    occurred      the reviewed after-state is present, or a supervisor observed
                  the process end with every member gone
    not_occurred  the before-state is present, or a record registered before
                  every effect proves nothing started
    diverged      the current state matches neither
    unknown       it cannot be observed or proven; nothing may be settled

Verdicts describe current state, so an effect that was later undone reads as
not_occurred; evidence keeps the broker status. The action must be the durable
broker record, owned by a run tool receipt in its own conversation. A verdict
is evidence, never permission: every result carries authorization 'none' and
callers decide what to settle.

Plugin actions classified by connector_effects read back through the connector
itself: the person's reviewed read-class tool, never the effect tool, and only
from these native routes. Unclassified plugin actions stay unknown.
"""

import json
import os
import re
import sqlite3
import stat

from .policy import PolicyError, Workspace, digest


VERDICTS = ('occurred', 'not_occurred', 'diverged', 'unknown')
NEVER_PERFORMED = ('pending', 'rejected', 'superseded')  # the broker marks 'running' before any effect
MAX_RUN_ITEMS = 64
HEX = re.compile(r'[0-9a-f]{32}')
SHA = re.compile(r'[0-9a-f]{64}')
REASONS = {
    'not_found': 'The action is not a broker record in this conversation.',
    'forged': 'The supplied action does not match its durable broker record.',
    'no_ownership': 'No run tool receipt in this conversation owns this action.',
    'ownership_mismatch': 'The action no longer matches the run tool receipt that owns it.',
    'malformed': 'The action payload lacks the facts readback needs.',
    'receipt_inconsistent': 'The recorded before and after facts disagree with each other.',
    'root_unavailable': 'The reviewed project folder is not available.',
    'root_replaced': 'The reviewed project folder was replaced, so its files cannot be compared.',
    'unreadable': 'The project file could not be read.',
    'binding_mismatch': 'The effect record belongs to a different action, run or project.',
    'record_missing': 'The action is recorded as applied, but its effect record is missing.',
    'repository_unverified': 'The repository could not be verified or read back.',
    'indistinguishable': 'The recorded before and after facts cannot be told apart.',
    'no_expected_facts': 'The effect started before its expected outcome was recorded.',
    'still_running': 'The effect is still running in this service.',
    'no_supervisor_record': 'No supervisor record proves whether this process ran or ended.',
    'supervisor_error': 'The supervisor record could not be read.',
    'exit_unobserved': 'The supervisor did not observe the process exit.',
    'containment_unproven': 'The process exited, but no sweep proved every member gone.',
    'storage_unverified': 'The managed environment storage folder is shared, replaced or unreadable.',
    'folder_unverified': 'A folder exists where the environment was, but it cannot be verified as that environment.',
    'tree_unmeasured': 'The environment folder could not be measured.',
    'no_recorded_tree': 'The environment folder exists, but no verified tree digest was recorded for it.',
    'remote_effect': 'Remote effects need a connector readback, which this connector does not provide.',
    'no_readback_tool': 'The person did not review a read-class readback for this connector tool.',
    'read_class': 'A read-class connector call has no external effect to read back; report it with a note.',
    'connector_changed': 'The connector or its reviewed catalogue changed since this effect; review it again first.',
    'connector_unavailable': 'The connector could not be reached for a read-class readback.',
    'plugins_disabled': 'Plugin access is disabled in Permissions, so the connector cannot be read back.',
    'not_observable': 'The granted window or tab can no longer be observed, so the act cannot be read back.',
    'not_provable_by_reobservation': 'Re-observing the window cannot prove whether this act took effect.',
    'readback_error': 'The read-class readback reported an error.',
    'readback_unstructured': 'The read-class readback returned no structured result to compare.',
    'readback_malformed': 'The read-class readback result does not have the reviewed item list.',
    'absent_by_fields': 'No match by subject, recipients and time; this cannot prove the effect did not occur.',
    'ambiguous_match': 'Several items match by subject, recipients and time, so this effect cannot be identified.',
    'after_state_unproven': 'The object exists but does not show the reviewed change; it may not have occurred or was changed again.',
    'no_match_facts': 'The recorded facts are not enough for the reviewed readback.',
    'unsupported_kind': 'Readback does not support this action kind.',
    'no_service': 'The service that owns this effect record is not available.',
    'no_broker_action': 'This tool began without a broker action, so there is nothing to read back.',
    'readback_limit': f'Only the first {MAX_RUN_ITEMS} uncertain outcomes are read back at once.',
    'readback_failed': 'Readback failed unexpectedly.',
}


def _result(verdict, basis, base=None, **facts):
    evidence = {**(base or {}), 'basis': basis, **facts}
    if verdict == 'unknown':
        evidence.setdefault('reason', REASONS.get(basis, 'The outcome could not be proven.'))
    return {'verdict': verdict, 'evidence': evidence, 'authorization': 'none'}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _record(text):
    try:
        value = json.loads(text) if isinstance(text, str) and text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _brief(exc):
    return ' '.join(str(exc).split())[:300] or type(exc).__name__


def _binding(app, action):
    """(durable row, owner) for a genuine owned action, or (None, refusal result)."""
    if not isinstance(action, dict) or not isinstance(action.get('id'), str) or not HEX.fullmatch(action['id']):
        return None, _result('unknown', 'not_found')
    aid = action['id']
    with app.store.lock:
        db = app.store.db
        row = db.execute('SELECT id,chat_id,kind,status,payload FROM actions WHERE id=?', (aid,)).fetchone()
        if not row:
            return None, _result('unknown', 'not_found', {'action_id': aid})
        base = {'action_id': aid, 'kind': row[2], 'action_status': row[3]}
        payload = _record(row[4])
        if (action.get('chat_id') != row[1] or action.get('kind', row[2]) != row[2]
                or ('payload' in action and _canonical(action['payload']) != _canonical(payload))):
            return None, _result('unknown', 'forged', base)
        if not all(_table(db, name) for name in ('run_action_owners', 'runs', 'run_tools')):
            return None, _result('unknown', 'no_ownership', base)
        owner = db.execute('SELECT o.run_id,o.call_id,o.kind,r.chat_id FROM run_action_owners o '
                           'JOIN runs r ON r.id=o.run_id WHERE o.action_id=?', (aid,)).fetchone()
        if not owner:
            return None, _result('unknown', 'no_ownership', base)
        tool = db.execute('SELECT result FROM run_tools WHERE run_id=? AND call_id=?', (owner[0], owner[1])).fetchone()
        if owner[2] != row[2] or owner[3] != row[1] or not tool or _record(tool[0]).get('action_id') != aid:
            return None, _result('unknown', 'ownership_mismatch', base)
    durable = {'id': aid, 'chat_id': row[1], 'kind': row[2], 'status': row[3], 'payload': payload}
    return (durable, {'run_id': owner[0], 'call_id': owner[1]}), {**base, 'run_id': owner[0], 'call_id': owner[1]}


def readback(app, action, *, job_evidence=None):
    """Read the current authoritative state of one owned broker action.

    Returns {'verdict', 'evidence', 'authorization': 'none'}. job_evidence, if
    given, is called as job_evidence(process_id) and returns None or the
    supervisor's record {'spawning', 'spawned', 'exited', 'contained', 'exit_code'}.
    """
    base = None
    try:
        bound, base = _binding(app, action)
        if bound is None:
            return base
        durable, owner = bound
        if durable['status'] in NEVER_PERFORMED:
            return _result('not_occurred', 'never_performed', base)
        kind, payload = durable['kind'], durable['payload']
        if kind == 'edit':
            return _edit(payload, base)
        if kind == 'git':
            return _git(app, durable, owner, base)
        if kind in ('command', 'tests'):
            return _process(app, durable, owner, base, job_evidence)
        if kind == 'install':
            return _install(app, durable, base)
        if kind == 'plugin':
            from .connector_effects import readback as connector_readback
            return connector_readback(app, durable, base)
        if kind == 'screen':
            # Phase 16 D04: re-observe the granted window; never dispatch. not_observed stays unknown.
            screens = getattr(app, 'screens', None)
            if screens is None:
                return _result('unknown', 'not_observable', base)
            r = screens.readback((payload or {}).get('proposal_id'))
            verdict = r.get('verdict') if r.get('verdict') in ('occurred',) else 'unknown'
            return _result(verdict, (r.get('evidence') or {}).get('basis', 'readback_failed'), base)
        return _result('unknown', 'unsupported_kind', base)
    except (sqlite3.Error, OSError) as exc:
        return _result('unknown', 'unreadable', base, reason=_brief(exc))
    except Exception as exc:  # an unobservable outcome is unknown; it never settles anything
        return _result('unknown', 'readback_failed', base, reason=type(exc).__name__ + ': ' + _brief(exc))


def _edit(payload, base):
    root, identity, path = payload.get('root'), payload.get('root_identity'), payload.get('path')
    before, after, baseline = payload.get('before'), payload.get('after'), payload.get('baseline', '')
    result = payload.get('result')
    receipt = result.get('hash') if isinstance(result, dict) else None
    if not (isinstance(root, str) and root and isinstance(path, str) and path and isinstance(before, str)
            and isinstance(after, str) and isinstance(identity, list) and len(identity) == 2
            and all(type(v) is int for v in identity) and (baseline is None or isinstance(baseline, str) and SHA.fullmatch(baseline))
            and (receipt is None or isinstance(receipt, str) and SHA.fullmatch(receipt))):
        return _result('unknown', 'malformed', base)
    try:
        expected = {digest(after.encode('utf-8'))} | ({receipt} if receipt else set())
        consistent = before == '' if baseline is None else digest(before.encode('utf-8')) == baseline
    except UnicodeEncodeError:
        return _result('unknown', 'malformed', base)
    facts = {'path': path, 'before_hash': baseline, 'after_hashes': sorted(expected)}
    if not consistent:
        return _result('unknown', 'receipt_inconsistent', base, **facts)
    try:
        ws = Workspace(root)
    except (OSError, ValueError):
        return _result('unknown', 'root_unavailable', base, **facts)
    if str(ws.root) != root or list(ws.identity) != identity:
        return _result('unknown', 'root_replaced', base, **facts)
    try:
        data = ws.snapshot(path, missing_ok=True)
    except PolicyError as exc:
        return _result('unknown', 'unreadable', base, reason=_brief(exc), **facts)
    current = digest(data) if data is not None else None
    facts['current_hash'] = current
    if current in expected:
        return _result('occurred', 'after_present', base, indistinguishable=baseline in expected, **facts)
    if current == baseline:
        return _result('not_occurred', 'before_present' if current else 'absent_on_create', base, **facts)
    return _result('diverged', 'neither_present', base, **facts)


def _git(app, durable, owner, base):
    plan_id = durable['payload'].get('plan_id')
    if not isinstance(plan_id, str) or not HEX.fullmatch(plan_id):
        return _result('unknown', 'malformed', base)
    plans = getattr(app, 'git_plans', None)
    with app.store.lock:
        db = app.store.db
        row = db.execute('SELECT chat_id,run_id,action_id,state,receipt FROM git_plan_receipts WHERE id=?',
                         (plan_id,)).fetchone() if _table(db, 'git_plan_receipts') else None
    base = {**base, 'receipt_id': plan_id}
    if not row:
        if durable['status'] == 'applied':
            return _result('unknown', 'record_missing', base)
        # GitPlans registers its receipt before any Git effect.
        return _result('not_occurred', 'no_receipt', base)
    chat, run, aid, state, receipt = row
    if chat != durable['chat_id'] or aid != durable['id'] or (run is not None and run != owner['run_id']):
        return _result('unknown', 'binding_mismatch', base)
    receipt, base = _record(receipt), {**base, 'receipt_state': state}
    if state == 'executing':
        return _result('unknown', 'still_running', base)
    if state in ('failed', 'uncertain') and receipt.get('effect_started') is False:
        # The started marker is persisted before the first ref, index or worktree effect.
        return _result('not_occurred', 'no_effect_started', base)
    if plans is None:
        return _result('unknown', 'no_service', base)
    try:
        view = plans.readback(durable['chat_id'], plan_id)
    except Exception as exc:  # any failure to observe is unknown, never a settlement
        return _result('unknown', 'repository_unverified', base, reason=_brief(exc))
    if not isinstance(view, dict) or view.get('verified') is not True:
        issue = view.get('issue') if isinstance(view, dict) else None
        return _result('unknown', 'repository_unverified', base, **({'reason': _brief(issue)} if issue else {}))
    after, before = view.get('matches_receipt'), view.get('matches_before')
    facts = {'matches_receipt': after, 'matches_before': before}
    if after is True and before is not True:
        return _result('occurred', 'after_present', base, **facts)
    if before is True and after is not True:
        return _result('not_occurred', 'before_present', base, **facts)
    if after is True and before is True:
        if state == 'applied':
            return _result('occurred', 'after_present', base, indistinguishable=True, **facts)
        return _result('unknown', 'indistinguishable', base, **facts)
    if after is False and before is False:
        return _result('diverged', 'neither_present', base, **facts)
    return _result('unknown', 'no_expected_facts', base, **facts)


def _evidence(job_evidence, process_id):
    """The supervisor record, strictly typed, or None."""
    if job_evidence is None:
        return None
    record = job_evidence(process_id)
    if record is None:
        return None
    if not isinstance(record, dict):
        raise TypeError('malformed supervisor record')
    flags = {}
    for key in ('spawning', 'spawned', 'exited', 'contained'):
        value = record.get(key)
        if value is not None and type(value) is not bool:
            raise TypeError('malformed supervisor record')
        flags[key] = value
    code = record.get('exit_code')
    flags['exit_code'] = code if type(code) is int else None
    return flags


def _process(app, durable, owner, base, job_evidence):
    payload = durable['payload']
    with app.store.lock:
        db = app.store.db
        job = db.execute('SELECT id,chat_id,run_id,call_id,state,outcome_uncertain,exit_code FROM process_jobs '
                         'WHERE action_id=?', (durable['id'],)).fetchone() if _table(db, 'process_jobs') else None
    if not job:
        result = payload.get('result') if isinstance(payload.get('result'), dict) else {}
        if durable['status'] == 'applied' and type(result.get('exit_code')) is int and not result.get('process_id'):
            # A synchronous command's receipt commits only after its observed exit.
            return _result('occurred', 'exit_receipt', base, exit_code=result['exit_code'])
        if payload.get('asynchronous') is True and durable['kind'] == 'command':
            if durable['status'] == 'applied':
                return _result('unknown', 'record_missing', base)
            # ProcessJobs commits the job row before its worker can start anything.
            return _result('not_occurred', 'no_admission_record', base)
        return _result('unknown', 'no_supervisor_record', base)
    process_id, chat, run, call, state, uncertain, code = job
    base = {**base, 'process_id': process_id, 'process_state': state}
    if (chat, run, call) != (durable['chat_id'], owner['run_id'], owner['call_id']):
        return _result('unknown', 'binding_mismatch', base)
    if state == 'refused':
        return _result('not_occurred', 'refused_before_start', base)
    if state in ('admitted', 'running'):
        return _result('unknown', 'still_running', base)
    if state in ('succeeded', 'failed') and not uncertain:
        return _result('occurred', 'exit_observed', base, exit_code=code)
    try:
        record = _evidence(job_evidence, process_id)
    except Exception as exc:
        return _result('unknown', 'supervisor_error', base, reason=_brief(exc))
    if record is None:
        return _result('unknown', 'no_supervisor_record', base)
    if record['spawning'] is False:
        # The supervisor fsyncs `spawning` before fork: without it nothing ran.
        return _result('not_occurred', 'no_spawn_record', base)
    if record['exited'] is not True:
        return _result('unknown', 'exit_unobserved', base)
    if record['contained'] is not True:
        return _result('unknown', 'containment_unproven', base)
    return _result('occurred', 'supervised_exit', base, exit_code=record['exit_code'],
                   partial_possible=state not in ('succeeded', 'failed') or record['exit_code'] is None)


def _install(app, durable, base):
    from .environments import lstat, tree
    payload = durable['payload']
    ident = payload.get('environment_id')
    if not isinstance(ident, str) or not HEX.fullmatch(ident):
        return _result('unknown', 'malformed', base)
    base = {**base, 'environment_id': ident}
    with app.store.lock:
        db = app.store.db
        row = db.execute('SELECT chat_id,root,root_identity,kind,lock_digest,state,evidence FROM project_environments '
                         'WHERE id=?', (ident,)).fetchone() if _table(db, 'project_environments') else None
    if not row:
        if durable['status'] == 'applied':
            return _result('unknown', 'record_missing', base)
        # Environments registers its row before the folder or installer exists.
        return _result('not_occurred', 'no_registration', base)
    chat, root, identity, kind, lock, state, evidence = row
    base = {**base, 'environment_state': state}
    try:
        identity = json.loads(identity)
    except ValueError:
        identity = None
    if (chat, root, identity, kind, lock) != (durable['chat_id'], payload.get('root'), payload.get('root_identity'),
                                             payload.get('kind'), payload.get('lock_digest')):
        return _result('unknown', 'binding_mismatch', base)
    if state == 'installing':
        return _result('unknown', 'still_running', base)
    envs = getattr(app, 'environments', None)
    if envs is None:
        return _result('unknown', 'no_service', base)
    evidence, path = _record(evidence), envs.anchor / ident
    if lstat(envs.anchor) is not None:
        try:
            envs._anchor_check()
        except PolicyError:
            return _result('unknown', 'storage_unverified', base)
        info = lstat(path)
    else:
        info = None
    if info is None:
        return _result('not_occurred', 'folder_absent', base)
    if (not stat.S_ISDIR(info.st_mode) or os.path.realpath(path) != str(path) or info.st_uid != os.getuid()
            or evidence.get('dir_identity') != [info.st_dev, info.st_ino]):
        return _result('unknown', 'folder_unverified', base)
    recorded = evidence.get('tree_digest')
    if not isinstance(recorded, str) or not SHA.fullmatch(recorded):
        return _result('unknown', 'no_recorded_tree', base, folder_present=True)
    try:
        measured = tree(path)
    except (PolicyError, OSError) as exc:
        return _result('unknown', 'tree_unmeasured', base, reason=_brief(exc))
    facts = {'recorded_tree': recorded, 'current_tree': measured['digest'], 'tree_partial': measured['partial']}
    if measured['digest'] == recorded:
        return _result('occurred', 'after_present', base, **facts)
    return _result('diverged', 'neither_present', base, **facts)


def reconcile_run(app, run_id, *, job_evidence=None):
    """Content-free verdicts for a run's unsettled outcomes, in receipt order. Read-only.

    Covers every running or uncertain tool receipt and every process job of
    the run with an uncertain outcome. Each item carries identifiers, verdict
    and basis only: no paths, hashes, output or file content.
    """
    if not isinstance(run_id, str) or not HEX.fullmatch(run_id):
        raise PolicyError('Run not found.')
    with app.store.lock:
        db = app.store.db
        run = db.execute('SELECT chat_id FROM runs WHERE id=?', (run_id,)).fetchone() if _table(db, 'runs') else None
        if not run:
            raise PolicyError('Run not found.')
        tools = db.execute("SELECT call_id,name,result FROM run_tools WHERE run_id=? AND state IN ('running','uncertain') "
                           'ORDER BY rowid', (run_id,)).fetchall()
        owners = dict(db.execute('SELECT call_id,action_id FROM run_action_owners WHERE run_id=?', (run_id,)).fetchall()
                      if _table(db, 'run_action_owners') else [])
        jobs = db.execute('SELECT j.call_id,t.name,j.action_id FROM process_jobs j LEFT JOIN run_tools t ON '
                          't.run_id=j.run_id AND t.call_id=j.call_id WHERE j.run_id=? AND j.outcome_uncertain=1 '
                          'ORDER BY j.created,j.id', (run_id,)).fetchall() if _table(db, 'process_jobs') else []
    items, seen = [], set()
    for call_id, name, result in tools:
        aid = owners.get(call_id) or _record(result).get('action_id')
        items.append((call_id, name, aid if isinstance(aid, str) else None))
    for call_id, name, aid in jobs:
        if aid not in {item[2] for item in items}:
            items.append((call_id, name, aid))
    out = []
    for call_id, name, aid in items:
        entry = {'call_id': call_id, 'tool': name, 'action_id': aid, 'kind': None, 'process_id': None}
        if aid is None:
            verdict = _result('unknown', 'no_broker_action')
        elif aid in seen:
            continue
        elif len(seen) >= MAX_RUN_ITEMS:
            verdict = _result('unknown', 'readback_limit')
        else:
            verdict = readback(app, {'id': aid, 'chat_id': run[0]}, job_evidence=job_evidence)
        if aid is not None:
            seen.add(aid)
        evidence = verdict['evidence']
        entry.update(kind=evidence.get('kind'), process_id=evidence.get('process_id'), verdict=verdict['verdict'],
                     basis=evidence['basis'], authorization='none')
        out.append(entry)
    return out
