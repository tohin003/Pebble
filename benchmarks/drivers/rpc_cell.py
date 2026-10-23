"""Pebble cells over JSON-RPC (§7.2: the frozen 1.0 candidate, SHA-256-equal to the installed backend, Rig B).

The backend runs as the shell runs it (scripts/measure_resources.Backend: the main.rs environment allowlist and the
egress tripwire), on a flat scratch --home inside the cell folder. A setup session writes the route and permissions;
the measured session drives every turn over JSON-RPC exactly as the window would: send, native approvals of the
task's declared in-scope effects, typed answers to questions, then the reply from the chat record.

Routes: live, the evaluation connection's profile and host id written into the scratch home's settings (the
candidate then reads the connection's own Keychain record, never the installed app's, V1-015); scripted, a loopback
chat-completions provider (tests only).
"""

import json
import sqlite3
import time
from pathlib import Path

from ..corpus_24a import prompt_for
from ..corpus_24a.graders import Output
from . import workspace as ws
from .codex_cell import asks_question, last_turn_changed
from .pebble_cell import LIMITS, STATE, permissions

STATE_OF = {**STATE, 'timeout': 'timeout'}


def _measure():
    from ..perf.pb import _script
    return _script('measure_resources')


def _write_route(home, profile, host_id):
    db = sqlite3.connect(Path(home) / 'pebble.sqlite')
    try:
        db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)', [
            ('model_path', json.dumps('')), ('model_profiles', json.dumps([profile])),
            ('chatgpt_host_id', json.dumps(host_id))])
        db.commit()
    finally:
        db.close()


def _allowed(task, workspace, action):
    payload = action.get('payload') or {}
    if action.get('kind') not in task.allowed_kinds or payload.get('root') != str(workspace):
        return False
    if action['kind'] == 'edit':
        return payload.get('path') in task.allowed_paths
    if action['kind'] == 'tests':
        return payload.get('recipe') in task.allowed_recipes and all(
            isinstance(d, dict) and d.get('path') in task.allowed_paths for d in payload.get('drafts') or [])
    return False


def _reply(chat, rid):
    mine = [m for m in chat['messages'] if m['role'] == 'assistant' and m.get('run_id') == rid]
    if mine:
        return mine[-1]['content']
    assistants = [m for m in chat['messages'] if m['role'] == 'assistant']
    return assistants[-1]['content'] if assistants else ''


def run_cell(task, planned, root, *, command, route, resources=None, limits=LIMITS, trap=None):
    """One Pebble cell over JSON-RPC. ``route``: {'mode': 'live', 'profile', 'host_id'} or {'mode': 'scripted',
    'url'} (a loopback provider). Returns (Output, metrics)."""
    measure = _measure()
    started = time.monotonic()
    deadline = started + limits['cell_wall_seconds']
    folder = ws.cell_dir(root, 'pebble', task.id, planned['rep'])
    workspace = ws.materialize(folder / 'workspace', task.files) if task.workspace else None
    home = folder / 'home'
    home.mkdir(mode=0o700)
    before = ws.inventory(workspace)
    kwargs = {'resources': resources} if resources else {}
    replies, turn_files, error, state, approvals, rejections, answers, asked, first = [], [], None, 'not_run', 0, 0, 0, 0, None
    usage, requests = None, None
    setup = measure.Backend(command, home, trap=trap, **kwargs)
    try:
        if route['mode'] == 'scripted':
            setup.rpc('model_save', name='Bench fixture', url=route['url'], model='bench-fixture', context=32768)
        setup.rpc('setting', key='permissions', value=permissions(task))
        for layer, content in task.memory:
            setup.rpc('memory_save', kind=layer, content=content)
    finally:
        setup.close()
    if route['mode'] == 'live':
        _write_route(home, route['profile'], route['host_id'])
    backend = measure.Backend(command, home, trap=trap, **kwargs)
    try:
        if route['mode'] == 'live':
            backend.rpc('model_select', profile_id=route['profile']['id'])
        cid = backend.rpc('new_chat', workspace=str(workspace) if workspace else None)['id']
        texts = [(prompt_for(task, planned['phrasing']), None)] + [(turn.text, turn) for turn in task.turns]
        for index, (text, turn) in enumerate(texts):
            if turn is not None and turn.if_asked:
                if not (answers or asks_question(replies[-1] if replies else None,
                                                 changed=last_turn_changed(before, turn_files))):
                    replies.append(None)
                    turn_files.append(None)
                    continue
                asked += 0 if answers else 1
            if turn is not None and turn.new_chat:
                cid = backend.rpc('new_chat', workspace=str(workspace) if workspace else None)['id']
            remaining = int(deadline - time.monotonic())
            if remaining < 10:
                state = 'timeout'
                break
            mark, sent = backend.mark(), time.monotonic()
            rid = backend.rpc('send', id=cid, text=text, effort='medium' if route['mode'] == 'live' else None,
                              limits={'max_steps': limits['turn_max_steps'],
                                      'max_seconds': max(5, min(limits['turn_max_seconds'], remaining))})['run_id']
            if first is None and index == 0:
                tokened = backend.wait_event(lambda item: item.get('event') == 'token', min(60.0, remaining), after=mark)
                first = round(time.monotonic() - sent, 3) if tokened else None
            result = 'timeout'
            for _ in range(12):  # each round: settle, then the person's decisions
                result = measure.settle(backend, cid, rid, after=mark, seconds=max(1.0, deadline - time.monotonic()))
                if result not in ('waiting_approval', 'waiting_input'):
                    break
                chat, mark = backend.rpc('get_chat', id=cid), backend.mark()
                for action in [a for a in chat['actions'] if a['status'] == 'pending']:
                    if _allowed(task, workspace, action):
                        backend.rpc('approve', id=cid, action_id=action['id'])
                        approvals += 1
                    else:
                        backend.rpc('reject', id=cid, action_id=action['id'])
                        rejections += 1
                for question in [q for q in chat['questions'] if q['status'] == 'pending' and q['run_id'] == rid]:
                    backend.rpc('question_answer', id=cid, question_id=question['id'], revision=question['revision'],
                                answers={item['id']: {'text': task.answer} for item in question['items']})
                    answers += 1
            chat = backend.rpc('get_chat', id=cid)
            replies.append(_reply(chat, rid))
            turn_files.append(ws.inventory(workspace) if workspace else {})
            state = STATE_OF.get(result, 'failed')
            if state in ('failed', 'timeout'):
                break
        chat = backend.rpc('get_chat', id=cid)
        metered = [m['usage'] for m in chat['messages'] if isinstance(m.get('usage'), dict)]
        if metered:  # the host's own metrics rows, as evaluation._usage reads them; unknown stays None
            usage = {key: (sum(m[key] for m in metered) if all(type(m.get(key)) is int for m in metered) else None)
                     for key in ('input_tokens', 'cached_tokens', 'output_tokens')}
            requests = sum(m['calls'] for m in metered if type(m.get('calls')) is int)
    except Exception as exc:
        error = 'rpc_' + type(exc).__name__.lower()
    finally:
        backend.close()
    out = Output(replies=replies, state=state if not error else 'failed', honest_state=state == 'incomplete',
                 before=before, after=ws.inventory(workspace), turn_files=turn_files,
                 questions=asked + answers, applied=None, error=error)
    metrics = {'review': {'approvals': approvals, 'rejections': rejections, 'answers': answers},
               'requests': requests, 'usage': usage, 'wall': round(time.monotonic() - started, 3), 'first': first,
               'error': error}
    return out, metrics
