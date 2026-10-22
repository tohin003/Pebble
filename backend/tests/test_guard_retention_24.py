"""Phase 24 (V1-020): AgentGuard's database forgets what a deleted conversation typed, and retention runs.

0.6.1 never sent session_end, so AgentGuard's maintenance (its only retention pass) never ran: agentguard.db kept every
project prompt (up to 2,000 characters in tasks), 200-character argument prefixes (Write content, commands) and
challenge text quoting the request, forever, and deleting a conversation left all of it. Now a run that reaches a
terminal state ends its session (ended_at, its tasks closed, maintenance with Pebble's shorter embedded retention), and
GuardAdapter.forget(session_ids) deletes every row the conversation's sessions left (delete_chat v2 calls it).

The App, runner, controller, GuardAdapter and vendored engine and store are the checked-in code; only the model is
scripted. No model HTTP, network or shell.
"""

import copy
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from pebble.app import App
from pebble.guard import RETENTION
from pebble.permissions import DEFAULT
from pebble.policy import Workspace
from pebble.providers import Providers

SOURCE = 'def add(a, b):\n    return a + b\n'
CANARY = 'I24-RETENTION-CANARY-7f3a'
OTHER = 'I24-OTHER-CHAT-CANARY-91c2'


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    (project / 'notes.md').write_text('# Notes\n')
    value = SimpleNamespace(project=project, requests=[], script=[], home=tmp_path / 'data')

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        response = copy.deepcopy(value.script.pop(0) if value.script else final('Done.'))
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)
    value.app = App(value.home, tmp_path / 'resources', lambda *args: None)
    assert value.app.guard.guard is not None, value.app.guard.error
    value.app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'auto'})
    yield value
    value.app.active = value.app.active_run = None
    value.app.close()


def send(env, cid, text, script):
    env.script = list(script)
    before = {r['id'] for r in env.app.runs.list(cid)}
    env.app.send(cid, text)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        runs = [r for r in env.app.runs.list(cid) if r['id'] not in before]
        if runs and runs[0]['state'] not in {'queued', 'running', 'retry_wait'} and env.app.active is None \
                and not env.app.controller.live:
            time.sleep(0.05)  # the worker's release step runs right after the run settles
            return env.app.runs.get(runs[0]['id'])
        time.sleep(.02)
    pytest.fail('The run did not settle.')


def database(env):
    return sqlite3.connect(env.home / 'guard' / 'agentguard.db')


def rows_with(env, text):
    """Every agentguard.db row (any table, any text column) that contains text."""
    found = []
    with database(env) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [r[1] for r in db.execute(f'PRAGMA table_info({table})')]
            for row in db.execute(f'SELECT * FROM {table}'):
                if any(isinstance(v, str) and text in v for v in row):
                    found.append((table, dict(zip(columns, row))))
    return found


def test_a_terminal_run_ends_its_agentguard_session(env):
    chat = env.app.new_chat(str(env.project))
    run = send(env, chat['id'], 'What does calc.py do?', [call('Read', {'file_path': 'calc.py'}, 'read'),
                                                           final('add returns the sum of a and b.')])
    assert run['state'] == 'completed'
    with database(env) as db:
        [(ended,)] = db.execute('SELECT ended_at FROM sessions WHERE id=?', (run['id'],)).fetchall()
        statuses = {r[0] for r in db.execute('SELECT status FROM tasks WHERE session_id=?', (run['id'],))}
    assert ended is not None and statuses == {'closed'}
    guard = env.app.guard
    assert not [k for k in guard.roots if k[1] == run['id']]
    assert not [k for k in guard.overlays if k[1] == run['id']] and not [k for k in guard.prompts if k[1] == run['id']]
    [ws] = guard.guard._workspaces.values()
    assert run['id'] not in ws._session_task_ids and run['id'] not in ws._session_tasks
    assert guard.end(None, run['id']) == {'ended': 0}, 'idempotent'


def test_deleting_a_conversation_forgets_every_row_its_sessions_left(env):
    app = env.app
    chat, other = app.new_chat(str(env.project)), app.new_chat(str(env.project))
    first = send(env, chat['id'], f'Add a line to notes.md: {CANARY}',
                 [call('Read', {'file_path': 'notes.md'}, 'read'),
                  call('Write', {'file_path': 'notes.md', 'content': f'# Notes\n{CANARY}\n'}, 'write'),
                  final('Added the line.')])
    second = send(env, chat['id'], f'Explain {CANARY} in calc.py', [final('calc.py has no such name.')])
    kept = send(env, other['id'], f'Explain {OTHER} in calc.py', [final('No such name.')])
    # A run-less call in the chat (a direct host execute) uses the chat id as its session.
    app.guard.handle('pre_tool_use', Workspace(env.project).root, chat['id'], tool='Bash',
                     arguments={'command': f'rg {CANARY} .'}, read_only=True)
    assert rows_with(env, CANARY), 'the fixture reached agentguard.db'
    removed = app.guard.forget([first['id'], second['id'], chat['id']])
    assert removed['sessions'] == 3 and removed['rows']['tasks'] >= 2 and removed['rows']['events'] > 0
    assert rows_with(env, CANARY) == []
    for suffix in ('', '-wal'):
        path = env.home / 'guard' / ('agentguard.db' + suffix)
        assert not path.exists() or CANARY.encode() not in path.read_bytes(), suffix
    assert rows_with(env, OTHER), "another conversation's records stay"
    assert kept['state'] == 'completed'


def test_embedded_retention_prunes_old_rows_and_keeps_new_ones(env):
    app = env.app
    settings = app.guard.guard.settings.retention
    assert {name: getattr(settings, name) for name in RETENTION} == RETENTION
    assert (settings.raw_events_days, settings.session_summaries_days) == (7, 30)
    chat = app.new_chat(str(env.project))
    send(env, chat['id'], 'What does calc.py do?', [final('It adds.')])
    now, day = time.time(), 86400
    with database(env) as db:
        project = db.execute('SELECT id FROM projects LIMIT 1').fetchone()[0]
        db.execute("INSERT INTO sessions(id,project_id,agent,started_at,ended_at,meta) VALUES('old-s',?, 'pebble',?,?, '{}')",
                   (project, now - 40 * day, now - 40 * day))
        db.execute("INSERT INTO tasks(id,project_id,session_id,prompt,status,created_at,closed_at) "
                   "VALUES('old-t',?, 'old-s', ?, 'closed', ?, ?)", (project, CANARY, now - 40 * day, now - 40 * day))
        db.execute("INSERT INTO events(id,project_id,session_id,type,arguments,ts) VALUES('old-e',?, 'old-s', "
                   "'pre_tool_use', ?, ?)", (project, json.dumps({'content': CANARY}), now - 8 * day))
        db.execute("INSERT INTO decisions(id,project_id,session_id,action,reason,ts) VALUES('old-d',?, 'old-s', "
                   "'challenge', ?, ?)", (project, CANARY, now - 15 * day))
        db.execute("INSERT INTO events(id,project_id,session_id,type,arguments,ts) VALUES('new-e',?, 'new-s', "
                   "'pre_tool_use', '{}', ?)", (project, now - 2 * day))
    removed = app.guard.maintain(force=True)
    assert removed['events'] >= 1 and removed['decisions'] >= 1 and removed['tasks'] >= 1 and removed['sessions'] >= 1
    assert rows_with(env, CANARY) == []
    with database(env) as db:
        assert db.execute("SELECT 1 FROM events WHERE id='new-e'").fetchone()
