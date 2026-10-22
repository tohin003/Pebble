"""Pebble cells for corpus 24a: one owned App per cell, in the cell's throwaway folder.

The App is the real one (controller, runner, broker, AgentGuard, memory, views); only the route differs: the separate
evaluation connection (live, V1-015) or a scripted fixture (plumbing only, never quality evidence). The permission
mode is the task's (Auto in the project, §7.2; Ask for the recovery drills), with the task's groups only: never web,
terminal, apps, plugins or screen. The driver approves only the task's declared in-scope proposals, answers any
question with the task's fixed answer, sends fixed follow-ups after each reply settles, and restarts the App at a
recovery task's declared boundary (eval_recovery_18). Nothing in a result cell carries a reply or a file body.
"""

import time
from types import SimpleNamespace

from pebble import evaluation as ev

from ..corpus_24a import prompt_for
from ..corpus_24a.graders import Output
from . import workspace as ws
from .codex_cell import asks_question

LIMITS = {'cell_wall_seconds': 900, 'turn_max_steps': 24, 'turn_max_seconds': 300}
STATE = {'completed': 'completed', 'verification_required': 'incomplete', 'needs_reconciliation': 'incomplete',
         'failed': 'failed', 'budget_exhausted': 'budget_exhausted', 'stopped': 'cancelled', 'paused': 'cancelled',
         'interrupted': 'cancelled'}
WRITE_TOOLS = frozenset({'Write', 'Edit', 'Append'})


class Recorder:
    """The App's event sink and model-request tap: first token time per run, request count and offered tools."""

    def __init__(self):
        self.tokens, self.requests, self.offered, self.sent_at = {}, 0, [], None

    def emit(self, event, data=None):
        if event == 'token' and self.sent_at is not None and 'first' not in self.tokens:
            self.tokens['first'] = time.monotonic() - self.sent_at

    def tap(self, app):
        complete = app.model.complete

        def tapped(messages, tools, *args, **kwargs):
            self.requests += 1
            self.offered.append(frozenset(t.get('function', {}).get('name') for t in tools or () if isinstance(t, dict)))
            return complete(messages, tools, *args, **kwargs)
        app.model.complete = tapped


def permissions(task):
    return {'mode': task.mode, 'tools': {group: group in set(task.tools) for group in ev.PERMISSION_GROUPS_18}}


def install_route(app, task, route):
    if route['mode'] == 'live':
        return ev._install_live(app, route['profile'], route.get('auth_factory'), route.get('host_id'))
    return ev._install_scripted(app, task, route['script'])


def _reply(app, cid, rid):
    run = app.runs.get(rid, cid)
    mid = (run.get('checkpoint') or {}).get('message_id')
    rows = app.store.query("SELECT content FROM messages WHERE id=? AND chat_id=? AND role='assistant'", (mid, cid)) if mid else []
    return rows[0]['content'] if rows else ''


def _applied(app, cids):
    found = []
    for cid in cids:
        for action in app.store.actions(cid):
            if action['kind'] == 'edit' and action['status'] == 'applied':
                result = action['payload'].get('result') if isinstance(action['payload'].get('result'), dict) else {}
                found.append((action['payload'].get('path'), result.get('hash') or action['payload'].get('after_hash')))
    return found


def _usage(app, cids):
    rows = [row for cid in cids for row in app.store.query('SELECT payload FROM metrics WHERE chat_id=?', (cid,))]
    usage = ev._usage(rows)
    return {'input_tokens': usage['root_input_tokens'], 'cached_tokens': usage['root_cached_tokens'],
            'output_tokens': usage['root_output_tokens']}


def run_cell(task, planned, root, route, *, resources=None, limits=LIMITS, prepare_app=None, restart=None):
    """One Pebble cell (all its turns). Returns (Output, metrics). ``route`` is {'mode': 'live', 'profile', 'host_id',
    'auth_factory'} or {'mode': 'scripted', 'script'}; ``restart`` is an eval_recovery_18.RecoveryHook or None."""
    started = time.monotonic()
    deadline = started + limits['cell_wall_seconds']
    folder = ws.cell_dir(root, 'pebble', task.id, planned['rep'])
    workspace = ws.materialize(folder / 'workspace', task.files) if task.workspace else None
    before = ws.inventory(workspace)
    recorder, counters = Recorder(), ev._Counters()
    app, error, failure, cids, rids, replies, turn_files = None, None, None, [], [], [], []
    state, questions, drive = 'not_run', 0, 'settled'
    ctx = SimpleNamespace(restart=restart if task.restart else None, connectors=None, module=None)

    def open_app():
        from pebble.app import App
        new = App(folder / 'home', resources or folder / 'resources', recorder.emit)
        install_route(new, task, route)
        recorder.tap(new)
        if prepare_app is not None:
            prepare_app(new, task.id)
        return new

    try:
        app = open_app()
        if route['mode'] == 'live' and app.guard.guard is None:
            raise ev.EvaluationError('guard_unavailable')
        app.setting('permissions', permissions(task))
        for layer, content in task.memory:
            app.memory_save(layer, content)
        cid = app.new_chat(str(workspace) if workspace else None)['id']
        cids.append(cid)
        texts = [(prompt_for(task, planned['phrasing']), None)] + [(turn.text, turn) for turn in task.turns]
        for index, (text, turn) in enumerate(texts):
            if turn is not None and turn.if_asked:
                if not (counters['answered'] or asks_question(replies[-1] if replies else None)):
                    replies.append(None)
                    turn_files.append(None)
                    continue
                if not counters['answered']:
                    questions += 1  # asked in its reply rather than through a question form
            if turn is not None and turn.new_chat:
                cid = app.new_chat(str(workspace) if workspace else None)['id']
                cids.append(cid)
            remaining = int(deadline - time.monotonic())
            if remaining < 10:
                drive = 'timeout'
                break
            if index == 0:
                recorder.sent_at = time.monotonic()  # time to the first output event of the cell's first turn
            rid = app.send(cid, text, effort='medium' if route['mode'] == 'live' else None,
                           limits={'max_steps': limits['turn_max_steps'],
                                   'max_seconds': max(5, min(limits['turn_max_seconds'], remaining))})['run_id']
            rids.append((cid, rid))

            def reopen(old, rid=rid, cid=cid):
                counters.add('restarts')
                if not ev._close(old):
                    raise ev.EvaluationError('owned_app_did_not_stop')
                new = open_app()
                if new.runs.get(rid, cid)['state'] in ev.RESUMABLE_AFTER_RESTART:
                    try:
                        new.run_resume(cid, rid)
                    except ValueError:
                        counters.add('resume_refused')
                return new

            drive, app = ev._drive_18(app, task, cid, rid, workspace, deadline, counters, ctx, reopen)
            if (drive == 'settled' and counters['restarts'] and ctx.restart is not None
                    and app.runs.get(rid, cid)['state'] == 'needs_reconciliation'
                    and ctx.restart.pebble_reconcile(app, task, cid, rid)):
                drive, app = ev._drive_18(app, task, cid, rid, workspace, deadline, counters, ctx, reopen)
            replies.append(_reply(app, cid, rid))
            turn_files.append(ws.inventory(workspace) if workspace else {})
            run = app.runs.get(rid, cid)
            state = 'timeout' if drive == 'timeout' else STATE.get(run['state'], 'failed')
            if state == 'failed':
                from pebble.eval_baseline import text_failure_kind
                failure = text_failure_kind(run.get('error'))
            if drive == 'timeout' or state == 'failed':
                break
        if drive == 'timeout' and rids:
            ev._halt(app, *rids[-1])
        artifacts = [a for cid in cids for a in ev._grader_artifacts(app, cid)]
        children = sum(len(app.agents.tree.list(cid, rid)) for cid, rid in rids)
        loops = sum(len(app.store.query("SELECT 1 FROM run_events WHERE run_id=? AND kind LIKE '%loop%'", (rid,)))
                    for _, rid in rids)
        applied, usage = _applied(app, cids), _usage(app, cids)
        held = any(app.runs.get(rid, cid)['state'] in ('verification_required', 'needs_reconciliation')
                   for cid, rid in rids[-1:])
    except ev.EvaluationError as exc:
        error, artifacts, children, loops, applied, usage, held = str(exc), [], None, 0, None, None, False
    except Exception as exc:
        error, artifacts, children, loops, applied, usage, held = ('driver_' + type(exc).__name__.lower(), [], None,
                                                                    0, None, None, False)
    finally:
        stopped = ev._close(app)
    if not stopped:
        error = error or 'owned_app_did_not_stop'
    needed = WRITE_TOOLS if task.allowed_paths else frozenset({'Read'}) if task.workspace else frozenset()
    out = Output(replies=replies, state=state if not error else 'failed', honest_state=held, before=before,
                 after=ws.inventory(workspace), turn_files=turn_files, artifacts=artifacts,
                 questions=questions + counters['answered'], children=children, applied=applied, error=error,
                 failure=failure, loop_stopped=bool(loops),
                 routing_miss=bool(needed and recorder.offered and not any(needed & o for o in recorder.offered)))
    metrics = {'review': {'approvals': counters['approved'], 'rejections': counters['rejected'],
                          'answers': counters['answered']},
               'requests': recorder.requests, 'usage': usage, 'restarts': counters['restarts'],
               'wall': round(time.monotonic() - started, 3),
               'first': None if 'first' not in recorder.tokens else round(recorder.tokens['first'], 3),
               'error': error}
    return out, metrics
