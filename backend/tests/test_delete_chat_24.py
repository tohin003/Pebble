"""Phase 24 (t24-data): deleting a conversation removes Pebble's copies, is safe to retry, and goes to Recently deleted.

- V1-016: DataServices.forget_chat(cid) removes Pebble's own copies of a conversation (its transcript, attachment,
  artifact and export copies) from the current copies folder and every earlier one, also after copies were turned off
  or moved; a copy the person changed stays. A discarded export's copy goes too (forget_export).
- V1-063 (D2): the main window's Delete moves a conversation to Recently deleted with origin 'app'; Delete now and the
  30-day purge delete it for good. V1-061: Delete all conversations and Delete all now, both confirmed.
- V1-064, V1-020, V1-156 (the parent's delete_chat v2 patch): every subsystem forgets first, GuardAdapter.forget gets
  the run ids and the chat id, the conversation's own rows go last in one transaction, a failure on the way leaves it
  listed and a retry completes, and the windows hear which artifacts went.
- V1-018 (parent patches): a conversation in Recently deleted refuses send and resume; chat() reports 'trashed'.
Tests that need the parent's app.py patches are strict xfails until those patches land (they fail on 0.6.1's code).
Under a fake HOME with an injected clock. No network, model call or Keychain.
"""

import hashlib
import inspect
import json
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from pebble import data_home, data_mirror
from pebble.app import App
from pebble.data_home import resolve
from pebble.data_mirror import DataServices
from pebble.store import LISTED_CHATS, TRASH_DAYS


def _source(function):
    return inspect.getsource(function) + (function.__doc__ or '')


PATCHED_DELETE = 'chat_deleted' in _source(App._delete_chat)
PATCHED_SEND = 'refuse_if_trashed' in _source(App.send)
PATCHED_CHAT = 'is_trashed' in _source(App.chat)
PATCHED_RESUME = 'refuse_if_trashed' in _source(App.run_resume)
PATCHED_DISCARD = hasattr(App, 'export_discard')


def needs(flag, what):
    return pytest.mark.xfail(not flag, strict=True, reason=f"needs the parent's t24-data app.py patch ({what})")


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    monkeypatch.setenv('AGENTGUARD_HOME', str(tmp_path / 'guard-unused'))
    data_home.deactivate()
    yield home
    data_home.deactivate()


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def env(fake_home):
    layout = resolve(str(fake_home / '.pebble'), None, env={})
    data_home.ensure_layout(layout)
    report = data_home.migrate(layout)
    data_home.activate(layout, Path(report['data']), report)
    events, clock = [], Clock()
    original = DataServices.start
    DataServices.start = lambda self: None  # these tests drive the App's own storage service by hand
    try:
        app = App(Path(report['data']), fake_home / 'resources', lambda *event: events.append(event))
    finally:
        DataServices.start = original
    services = app.storage  # the instance App.delete_chat reaches
    services.clock, services.opener = clock, lambda path: None
    yield type('Env', (), {'app': app, 'services': services, 'docs': fake_home / 'Documents' / 'Pebble',
                           'events': events, 'clock': clock, 'home': fake_home})
    app.active = app.active_run = None
    services.stop()
    app.close()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def chat(app, tag):
    cid = app.store.new_chat()
    app.store.message(cid, 'user', f'Plan {tag}')
    app.store.message(cid, 'assistant', f'Here is the {tag} plan.')
    return cid


def seed(app, cid, tag):
    """One file and one image attachment, an artifact and a verified export, all with `tag` in their names."""
    mid = app.store.query("SELECT id FROM messages WHERE chat_id=? AND role='user'", (cid,))[0]['id']
    data = Path(app.home)
    fid, body = uuid.uuid4().hex, f'%PDF {tag}'.encode()
    (data / 'attachments' / (fid + '.pdf')).write_bytes(body)
    app.store.query('INSERT INTO file_attachments VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (fid, cid, mid, f'{tag}.pdf', 'pdf', len(body), 'text', None, '.pdf', sha256(body)))
    app.store.query('INSERT INTO image_attachments VALUES(?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, cid, mid, f'{tag}.png', b'\x89PNG ' + tag.encode(), None, 1, 1))
    view = app.artifacts.render(cid, f'{tag} table', 'table', {'columns': [{'id': 'a', 'label': 'A'}],
                                                               'rows': [{'a': tag}]})
    eid = uuid.uuid4().hex
    folder = data / 'exports' / cid / eid
    folder.mkdir(parents=True)
    csv = f'a\n{tag}\n'.encode()
    (folder / 'out.csv').write_bytes(csv)
    receipt = {'status': 'verified', 'file': {'name': 'out.csv', 'suggested_name': f'{tag}.csv', 'sha256': sha256(csv)}}
    app.store.query('INSERT INTO export_records VALUES(?,?,?,?,?,?,?,?,?)',
                    (eid, cid, 'p', 'csv', 'artifact', 'verified', json.dumps(receipt), time.time(), time.time()))
    return view['id'], eid


def copies(root, tag):
    """Every file below `root` whose name carries `tag` (conversation, attachment, artifact and export copies)."""
    return sorted(p.relative_to(root).as_posix() for p in Path(root).rglob('*') if p.is_file() and tag in p.name)


def listed(app):
    return {r['id'] for r in app.store.query(LISTED_CHATS)}


def gone(app, cid):
    for table in ('chats', 'messages', 'message_routes', 'actions', 'runs', 'artifacts', 'export_records',
                  'file_attachments', 'image_attachments'):
        column = 'id' if table == 'chats' else 'chat_id'
        if app.store.query(f'SELECT 1 FROM {table} WHERE {column}=?', (cid,)):
            return False
    return True


def finished_run(app, cid):
    rid = app.runs.create(cid, 'Plan the trip.', app.runtime_config(app.chat(cid), False))
    app.runs.transition(rid, 'running')
    app.runs.transition(rid, 'completed')
    return rid


# ---- V1-016: Pebble's own copies leave with the conversation ---------------------------------------------------------

def test_forget_chat_removes_its_copies_in_the_current_and_every_earlier_folder(env):
    alpha, beta = chat(env.app, 'alpha'), chat(env.app, 'beta')
    artifact, _ = seed(env.app, alpha, 'alpha')
    seed(env.app, beta, 'beta')
    env.services.tick()
    elsewhere = env.home / 'Notes' / 'Pebble copies'
    env.services.methods()['storage_set'](documents_folder=str(elsewhere))
    for root in (env.docs, elsewhere):
        assert len(copies(root, 'alpha')) == 5 and len(copies(root, 'beta')) == 5, copies(root, 'alpha')
    result = env.services.forget_chat(alpha)
    assert result == {'removed': 10, 'kept': 0, 'artifacts': [artifact]}
    for root in (env.docs, elsewhere):
        assert copies(root, 'alpha') == [], 'every copy of the deleted conversation is gone'
        assert len(copies(root, 'beta')) == 5, 'another conversation keeps its copies'
    assert alpha not in json.dumps(env.services.manifest)
    assert env.services.forget_chat(alpha) == {'removed': 0, 'kept': 0, 'artifacts': [artifact]}, 'idempotent'


def test_a_copy_the_person_changed_is_theirs_and_stays(env):
    alpha = chat(env.app, 'alpha')
    seed(env.app, alpha, 'alpha')
    env.services.tick()
    [pdf] = [p for p in (env.docs / 'Attachments').iterdir() if p.name.endswith('alpha.pdf')]
    pdf.write_bytes(b'%PDF annotated by the person')
    result = env.services.forget_chat(alpha)
    assert result['removed'] == 4 and result['kept'] == 1
    assert copies(env.docs, 'alpha') == ['Attachments/' + pdf.name]


def test_turning_copies_off_then_deleting_removes_the_transcript_copy(env):
    alpha = chat(env.app, 'alpha')
    env.services.tick()
    [transcript] = copies(env.docs, 'alpha')
    env.services.methods()['storage_set'](documents_mirror=False)
    assert copies(env.docs, 'alpha') == [transcript], 'turning copies off leaves the files'
    assert env.services.forget_chat(alpha)['removed'] == 1
    assert copies(env.docs, 'alpha') == []


def test_discarding_an_export_removes_its_copy(env):
    alpha = chat(env.app, 'alpha')
    _, export = seed(env.app, alpha, 'alpha')
    env.services.tick()
    assert any(name.startswith('Exports/') for name in copies(env.docs, 'alpha'))
    assert env.services.forget_export(alpha, export) == {'removed': 1, 'kept': 0}
    assert not any(name.startswith('Exports/') for name in copies(env.docs, 'alpha'))
    assert len(copies(env.docs, 'alpha')) == 4


@needs(PATCHED_DISCARD, 'export_discard route')
def test_the_export_discard_route_removes_the_copy(env, monkeypatch):
    alpha = chat(env.app, 'alpha')
    _, export = seed(env.app, alpha, 'alpha')
    env.services.tick()
    monkeypatch.setattr(env.app.exports, 'discard', lambda cid, eid: {'export_id': eid, 'status': 'discarded'})
    assert env.app.dispatch('export_discard', {'chat_id': alpha, 'export_id': export})['status'] == 'discarded'
    assert not any(name.startswith('Exports/') for name in copies(env.docs, 'alpha'))


# ---- V1-063 (D2) and V1-061: Recently deleted from the app --------------------------------------------------------------

def test_the_in_app_delete_goes_to_recently_deleted(env):
    alpha, beta = chat(env.app, 'alpha'), chat(env.app, 'beta')
    env.services.tick()
    trash = env.services.decision_methods()['storage_trash_chat']
    env.app.active = alpha
    with pytest.raises(ValueError, match='running a task'):
        trash(chat_id=alpha)
    env.app.active = None
    result = trash(chat_id=alpha)
    assert result['trashed'] == alpha and result['delete_after'] == pytest.approx(env.clock.now + TRASH_DAYS * 86400)
    assert env.app.store.trashed()[alpha]['origin'] == 'app' and listed(env.app) == {beta}
    assert ('storage', {'changed': 'recently_deleted'}) in env.events
    env.services.tick()
    assert copies(env.docs, 'alpha') == [], 'hidden, so its copy leaves Documents'
    assert env.app.store.query('SELECT 1 FROM messages WHERE chat_id=?', (alpha,)), 'nothing is deleted yet'
    env.services.methods()['storage_restore_chat'](chat_id=alpha)
    env.services.tick()
    assert listed(env.app) == {alpha, beta} and copies(env.docs, 'alpha')
    trash(chat_id=alpha)
    env.clock.now += TRASH_DAYS * 86400 + 1
    env.services.tick()
    assert gone(env.app, alpha) and alpha not in env.app.store.trashed()


def test_delete_all_and_delete_all_now_need_a_confirmation(env):
    chats = [chat(env.app, tag) for tag in ('alpha', 'beta', 'gamma')]
    routes = env.services.decision_methods()
    for name in ('storage_trash_all', 'storage_purge_all'):
        with pytest.raises(ValueError, match='Confirm'):
            routes[name]()
    env.app.active = chats[2]
    result = routes['storage_trash_all'](confirm=True)
    env.app.active = None
    assert result['moved'] == 2 and [k['chat_id'] for k in result['kept']] == [chats[2]]
    assert 'running a task' in result['kept'][0]['reason'] and listed(env.app) == {chats[2]}
    assert all(env.app.store.trashed()[c]['origin'] == 'app' for c in chats[:2])
    result = routes['storage_purge_all'](confirm=True)
    assert result == {'deleted': 2, 'kept': []}
    assert all(gone(env.app, c) for c in chats[:2]) and env.app.store.trashed() == {} and listed(env.app) == {chats[2]}


# ---- V1-064, V1-020, V1-156: delete_chat v2 (parent patch) -------------------------------------------------------------

@needs(PATCHED_DELETE, 'delete_chat v2')
def test_a_failure_mid_delete_leaves_the_conversation_listed_and_a_retry_completes(env, monkeypatch):
    alpha = chat(env.app, 'alpha')
    seed(env.app, alpha, 'alpha')
    finished_run(env.app, alpha)
    real, failures = env.app.artifacts.forget, []

    def flaky(cid):
        if not failures:
            failures.append(cid)
            raise sqlite3.OperationalError('database is locked')
        return real(cid)
    monkeypatch.setattr(env.app.artifacts, 'forget', flaky)
    with pytest.raises(sqlite3.OperationalError):
        env.app.delete_chat(alpha)
    assert alpha in {c['id'] for c in env.app.state()['chats']}, 'still listed, so it can be deleted again'
    assert env.app.store.query('SELECT 1 FROM messages WHERE chat_id=?', (alpha,))
    assert env.app.delete_chat(alpha) is True
    assert gone(env.app, alpha) and alpha not in {c['id'] for c in env.app.state()['chats']}


@needs(PATCHED_DELETE, 'delete_chat v2')
def test_delete_chat_forgets_the_agentguard_sessions_and_reports_a_refusal(env, monkeypatch):
    alpha, beta = chat(env.app, 'alpha'), chat(env.app, 'beta')
    rid = finished_run(env.app, alpha)
    calls = []
    monkeypatch.setattr(env.app.guard, 'forget', lambda ids: calls.append(list(ids)) or {'sessions': len(ids)})
    assert env.app.delete_chat(alpha) is True
    assert calls == [[rid, alpha]] and gone(env.app, alpha)

    def refuse(ids):
        raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(env.app.guard, 'forget', refuse)
    with pytest.raises(sqlite3.OperationalError):
        env.app.delete_chat(beta)
    assert beta in listed(env.app) and env.app.store.query('SELECT 1 FROM messages WHERE chat_id=?', (beta,))


@needs(PATCHED_DELETE, 'delete_chat v2')
def test_deleting_for_good_removes_the_copies_and_names_the_artifacts(env):
    alpha = chat(env.app, 'alpha')
    artifact, _ = seed(env.app, alpha, 'alpha')
    env.services.tick()
    env.services.methods()['storage_set'](documents_mirror=False)
    env.events.clear()
    assert env.app.delete_chat(alpha) is True
    assert copies(env.docs, 'alpha') == [], 'also after the copies were turned off'
    assert ('storage', {'changed': 'deleted', 'chat_id': alpha, 'deleted_artifacts': [artifact]}) in env.events
    assert gone(env.app, alpha)


# ---- V1-018: Recently deleted takes no new messages -----------------------------------------------------------------

def test_refuse_if_trashed_carries_its_code(env):
    alpha = chat(env.app, 'alpha')
    data_mirror.refuse_if_trashed(env.app.store, alpha)
    env.app.store.trash_chat(alpha, 'app')
    with pytest.raises(data_mirror.TrashedConversation) as caught:
        data_mirror.refuse_if_trashed(env.app.store, alpha)
    assert caught.value.code == 'trashed_conversation' and isinstance(caught.value, ValueError)
    assert str(caught.value) == ('This conversation is in Recently deleted. Restore it in Settings -> Data & storage '
                                 'to keep using it.')


@needs(PATCHED_SEND and PATCHED_CHAT and PATCHED_RESUME, 'send, chat and run_resume')
def test_a_trashed_conversation_refuses_new_messages_and_says_so(env):
    alpha = chat(env.app, 'alpha')
    rid = finished_run(env.app, alpha)
    assert env.app.chat(alpha)['trashed'] is False
    env.services.decision_methods()['storage_trash_chat'](chat_id=alpha)
    before = len(env.app.store.query('SELECT 1 FROM messages WHERE chat_id=?', (alpha,)))
    for text in ('One more thing about the trip.', '/help'):
        with pytest.raises(data_mirror.TrashedConversation) as caught:
            env.app.send(alpha, text)
        assert caught.value.code == 'trashed_conversation'
    with pytest.raises(data_mirror.TrashedConversation):
        env.app.run_resume(alpha, rid)
    assert len(env.app.store.query('SELECT 1 FROM messages WHERE chat_id=?', (alpha,))) == before
    assert env.app.chat(alpha)['trashed'] is True
    env.services.methods()['storage_restore_chat'](chat_id=alpha)
    assert env.app.chat(alpha)['trashed'] is False
