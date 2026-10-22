"""Baseline-harness cells for corpus 24a: the Phase 18 minimal loop (eval_baseline) on the same owned-App route.

Its definition for 24a, pinned in every harness plan (plan_24a.baseline_definition):
- each human turn is one baseline run; a follow-up carries the earlier turns as a plain transcript (TRANSCRIPT), and a
  turn marked new_chat starts with no transcript;
- its memory is a notes.md file in its folder (seeded with the task's saved notes), which it may read and write, as
  in 18a.3's notes_file view;
- it has no views, so a generated-UI task asks it to write the view as a file (VIEW_NOTE);
- a reply without a FINAL line still ends the turn (state completed), so format_only applies to every product alike.
"""

import threading
import time

from pebble.eval_baseline import run_baseline

from ..corpus_24a import prompt_for
from ..corpus_24a.graders import Output
from . import workspace as ws
from .codex_cell import asks_question
from .pebble_cell import LIMITS, install_route

TRANSCRIPT = 'Earlier in this conversation:\n{history}\n\nThe person now says:\n{text}'
NOTES_NOTE = '\n\nMy saved notes are in notes.md in this folder; you may update it.'
VIEW_NOTE = {'view.json': '\n\nThis harness cannot show views: write what you would show as JSON in view.json.',
             'view.html': '\n\nThis harness cannot show views: write it as a self-contained page in view.html.'}
STATE = {'completed': 'completed', 'ended': 'completed', 'budget_exhausted': 'budget_exhausted', 'failed': 'failed',
         'stopped': 'cancelled'}


def baseline_files(task):
    files = list(task.files)
    if task.family == 'memory':
        files.append(('notes.md', '# Notes\n' + ''.join('- %s\n' % content for _, content in task.memory)))
    return files


def baseline_grants(task):
    grants = set(task.tools) - {'views'}
    if task.family in ('memory', 'generated_ui'):
        grants |= {'files', 'edits'}
    return tuple(sorted(grants))


def suffix(task):
    note = NOTES_NOTE if task.family == 'memory' else ''
    return note + next((VIEW_NOTE[p] for p in task.allowed_paths if p in VIEW_NOTE), '')


def run_cell(task, planned, root, route, *, resources=None, limits=LIMITS, restart=None):
    """One baseline cell (all its turns). Returns (Output, metrics)."""
    from pebble.app import App
    started = time.monotonic()
    deadline = started + limits['cell_wall_seconds']
    folder = ws.cell_dir(root, 'baseline', task.id, planned['rep'])
    files = baseline_files(task)
    workspace = ws.materialize(folder / 'workspace', files) if files or task.workspace else None
    before = ws.inventory(workspace)
    app, error, failure, replies, turn_files, history, state = None, None, None, [], [], [], 'not_run'
    usage, calls, writes, answers, questions, first = {'input_tokens': 0, 'cached_tokens': 0, 'output_tokens': 0}, 0, [], 0, 0, None
    restarts = 0
    try:
        app = App(folder / 'home', resources or folder / 'resources', lambda *a, **k: None)
        install_route(app, task, {**route, 'script': route.get('baseline_script', route.get('script'))}
                      if route['mode'] == 'scripted' else route)
        hook = restart.baseline_due(task) if restart is not None and task.restart else None
        texts = [(prompt_for(task, planned['phrasing']), None)] + [(turn.text, turn) for turn in task.turns]
        for index, (text, turn) in enumerate(texts):
            if turn is not None and turn.if_asked:
                if not asks_question(replies[-1] if replies else None):
                    replies.append(None)
                    turn_files.append(None)
                    continue
                questions += 1
            if turn is not None and turn.new_chat:
                history = []
            remaining = int(deadline - time.monotonic())
            if remaining < 10:
                state = 'timeout'
                break
            prompt = (TRANSCRIPT.format(history='\n'.join(history), text=text) if history else text) + suffix(task)
            cancel, box = threading.Event(), {}

            def work(prompt=prompt):
                try:
                    box['evidence'] = run_baseline(
                        app, task, workspace, {'max_steps': limits['turn_max_steps'],
                                               'max_seconds': max(5, min(limits['turn_max_seconds'], remaining))},
                        answers={}, web=None, restart=hook, cancel=cancel, prompt=prompt, grants=baseline_grants(task))
                except BaseException as exc:
                    box['error'] = exc
            began = time.monotonic()
            thread = threading.Thread(target=work, name='bench-baseline-turn', daemon=True)
            thread.start()
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                cancel.set()
                app.model.cancel()
                thread.join(15.0)
                state = 'timeout'
                break
            if 'error' in box:
                raise box['error']
            result = box['evidence']
            first = first if first is not None else round(time.monotonic() - began, 3) if index == 0 else first
            calls += result.calls
            answers += result.answered
            restarts += result.restarts
            writes += [(w['path'], w['after']) for w in result.writes if w['status'] == 'applied']
            for key in usage:
                value = (result.usage or {}).get(key)
                usage[key] = usage[key] + value if isinstance(usage[key], int) and type(value) is int else None
            replies.append(result.answer)
            turn_files.append(ws.inventory(workspace) if workspace else {})
            history += ['Person: ' + text, 'Assistant: ' + (result.answer or '')]
            state = STATE.get(result.state, 'failed')
            if result.failure_kind in ('auth', 'provider'):
                failure = result.failure_kind
            if state != 'completed':
                break
    except Exception as exc:
        error = 'driver_' + type(exc).__name__.lower()
    finally:
        stopped = app is None or (app.close() or True)
    out = Output(replies=replies, state=state if not error else 'failed', before=before,
                 after=ws.inventory(workspace), turn_files=turn_files, questions=questions + answers, children=0,
                 applied=writes, error=error if stopped else (error or 'baseline_did_not_stop'), failure=failure)
    # The baseline's first output arrives with its completed reply (it does not stream): first_event is that time.
    metrics = {'review': {'answers': answers}, 'requests': calls, 'usage': usage, 'restarts': restarts,
               'wall': round(time.monotonic() - started, 3), 'first': first, 'error': out.error}
    return out, metrics
