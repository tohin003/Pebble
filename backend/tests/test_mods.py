"""Native grant pins, bounded dispatch and atomic data-only mod publication."""

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from pebble import mod_worker
from pebble.artifacts import Artifacts
from pebble.mods import Mods, manifest, validate_artifacts
from pebble.policy import PolicyError
from pebble.runtime import RunJournal
from pebble.store import Store


TABLE = {'columns': [{'id': 'count', 'label': 'Count'}], 'rows': [{'count': 1}]}


def pane(ident='counts', value=1):
    return {'id': ident, 'title': 'Counts', 'kind': 'table', 'data': {**copy.deepcopy(TABLE), 'rows': [{'count': value}]}}


@pytest.fixture
def service(tmp_path, monkeypatch):
    store = Store(tmp_path)
    app = SimpleNamespace(store=store, artifacts=Artifacts(store))
    journal = RunJournal(store, tmp_path)
    monkeypatch.setattr(mod_worker, 'supported', lambda: True)
    monkeypatch.setattr(mod_worker, 'run', lambda source, event, payload, cancel=None: [pane(value=payload.get('count', 1))])
    mods = Mods(app)
    yield app, mods, journal
    journal.close()
    store.close()


def install(mods, name=None, enabled=True, events=None):
    value = mods.example()
    if name:
        value['name'] = name
    if events:
        value['events'] = events
    review = mods.preview(value)
    item = mods.install(review['manifest'], review['digest'])
    return mods.toggle(item['id'], True, item['digest']) if enabled else item


def start(app, mods, journal, pins='current'):
    cid = app.store.new_chat()
    config = {'limits': {'max_steps': 20, 'max_seconds': 60}}
    if pins == 'current':
        config['mods'] = mods.fingerprint()
    elif pins is not None:
        config['mods'] = pins
    rid = journal.create(cid, 'Inspect counts', config)
    journal.transition(rid, 'running')
    return cid, rid


def test_native_full_review_exact_pin_disabled_install_and_enable(service):
    _, mods, _ = service
    value = mods.example()
    assert mods.list() == [] and mods.fingerprint() == []
    review = mods.preview(value)
    assert review['manifest']['source'] == value['source'] and review['digest'] and not review['enabled']
    with pytest.raises(PolicyError, match='exact'):
        mods.install({**value, 'name': 'Changed after review'}, review['digest'])
    item = mods.install(value, review['digest'])
    assert not item['enabled'] and item['grant_digest'] is None and mods.fingerprint() == []
    assert mods.install(value, review['digest'])['id'] == item['id']
    with pytest.raises(PolicyError, match='exact'):
        mods.toggle(item['id'], True, '0' * 64)
    enabled = mods.toggle(item['id'], True, item['digest'])
    assert enabled['grant_digest'] == enabled['digest'] and enabled['grant_revision'] == 1
    assert mods.fingerprint() == [{'id': enabled['id'], 'digest': enabled['digest'], 'grant_digest': enabled['digest']}]
    assert mods.inspect(item['id'])['manifest'] == review['manifest']


def test_install_requires_prior_review_and_registry_is_bounded(service):
    _, mods, _ = service
    from pebble.mods import _hash
    value = mods.example()
    with pytest.raises(PolicyError, match='Review'):
        mods.install(value, _hash(value))
    for index in range(8):
        install(mods, str(index), enabled=False)
    review = mods.preview({**value, 'name': 'Ninth'})
    with pytest.raises(PolicyError, match='eight'):
        mods.install(review['manifest'], review['digest'])
    assert len(mods.list()) == 8


@pytest.mark.parametrize('change', [
    {'version': True}, {'version': 2}, {'events': ['manual', 'manual']}, {'events': ['approval']},
    {'capabilities': ['ui.render', 'core.execute']}, {'capabilities': []}, {'permissions': ['native.rpc']},
    {'source': 'x' * 16001}, {'source': 'x\0x'}, {'source': 'sk-' + 'a' * 30},
    {'source': '\ud800'}, {'name': '\ud800'}, {'name': ''}, {'name': 'name\nsource'},
])
def test_manifest_rejects_unreviewable_source_unknown_events_and_extra_authority(service, change):
    _, mods, _ = service
    with pytest.raises(PolicyError):
        mods.preview({**mods.example(), **change})
    assert mods.list() == []


def test_duplicate_manifest_json_keys_are_rejected():
    with pytest.raises(PolicyError):
        manifest('{"version":1,"version":2,"name":"x","events":["manual"],"capabilities":["ui.render"],"source":"x"}')


def test_current_run_pins_authorize_create_and_update_same_pane(service):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    first = mods.dispatch(cid, rid, 'manual', {'count': 1}, item['id'])
    second = mods.dispatch(cid, rid, 'manual', {'count': 2}, item['id'])
    assert first['status'] == second['status'] == 'completed'
    one, two = first['artifacts'][0], second['artifacts'][0]
    assert one['id'] == two['id'] and two['revision'] == 2 and two['owner_run_id'] == rid
    assert two['data']['rows'] == [{'count': 2}]
    third = mods.dispatch(cid, rid, 'manual', {'count': 2}, item['id'])
    assert third['artifacts'][0]['revision'] == 2
    assert len(app.artifacts.list(cid)) == 1 and len(app.artifacts.history(cid, one['id'])) == 2


@pytest.mark.parametrize('pins', [None, [], [{'id': 'foreign', 'digest': 'x', 'grant_digest': 'x'}]])
def test_missing_legacy_or_foreign_run_pins_never_execute_mod(service, monkeypatch, pins):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal, pins)
    monkeypatch.setattr(mod_worker, 'run', lambda *args, **kwargs: pytest.fail('Unpinned mod executed'))
    assert mods.dispatch(cid, rid, 'manual', {}, item['id'])['status'] == 'disabled'
    assert not app.artifacts.list(cid) and not app.store.query('SELECT * FROM mod_dispatches')


def test_running_conversation_ownership_and_declared_event_are_required(service, monkeypatch):
    app, mods, journal = service
    item = install(mods, events=['manual'])
    cid, rid = start(app, mods, journal)
    foreign = app.store.new_chat()
    with pytest.raises(ValueError):
        mods.dispatch(foreign, rid, 'manual', {}, item['id'])
    assert mods.dispatch(cid, rid, 'context.summary', {'tokens': 1}, item['id'])['status'] == 'disabled'
    journal.transition(rid, 'paused')
    with pytest.raises(ValueError):
        mods.dispatch(cid, rid, 'manual', {}, item['id'])
    assert not app.store.query('SELECT * FROM mod_dispatches')


@pytest.mark.parametrize('event,payload', [
    ('manual', {'token': 'sk-' + 'a' * 30}), ('manual', {'text': 'x' * 8001}), ('manual', {'count': float('nan')}),
    ('run.state', {'state': 'running', 'steps': 1, 'elapsed': 1, 'goal': 'raw goal'}),
    ('run.state', {'state': 'running', 'steps': True, 'elapsed': 1}),
    ('context.summary', {'sources': ['raw source']}), ('context.summary', {'tokens': -1}),
    ('context.summary', {'messages': 1, 'raw': 'model text'}),
    ('tool.result', {'name': 'Read', 'status': 'completed', 'receipt_id': '1:r', 'result': 'raw file data'}),
    ('tool.result', {'name': 'Read', 'status': 'approved', 'receipt_id': '1:r'}),
    ('tool.result', {'name': 'Read', 'status': 'completed', 'receipt_id': '\nspoof'}),
])
def test_payload_bounds_secrets_and_host_event_sanitization(service, event, payload):
    app, mods, journal = service
    install(mods)
    cid, rid = start(app, mods, journal)
    with pytest.raises(PolicyError):
        mods.dispatch(cid, rid, event, payload)
    assert not app.artifacts.list(cid) and not app.store.query('SELECT * FROM mod_dispatches')


@pytest.mark.parametrize('bad', [
    {'kind': 'form', 'data': {'fields': []}}, {'kind': 'html', 'data': {'html': '<script>rpc()</script>'}},
    {'kind': 'mcp_media', 'data': {'run_id': 'r', 'snapshot_id': 's'}},
    {'data': {**TABLE, 'origin': 'human'}}, {'data': {**TABLE, 'rows': [{'count': float('inf')}]}},
    {'data': {**TABLE, 'rows': [{'count': 'sk-' + 'a' * 30}]}}, {'native_rpc': 'approve'},
])
def test_invalid_later_pane_publishes_nothing_and_error_contains_no_input(service, monkeypatch, bad):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    monkeypatch.setattr(mod_worker, 'run', lambda *args, **kwargs: [pane('valid'), {**pane('invalid'), **bad}])
    result = mods.dispatch(cid, rid, 'manual', {'private_note': 'DO NOT LOG ME'}, item['id'])
    assert result['status'] == 'failed' and result['dispatches'][0]['reason'] == 'invalid_artifacts'
    assert not app.artifacts.list(cid) and not app.store.query('SELECT * FROM mod_panes')
    raw = json.dumps(app.store.query('SELECT * FROM mod_dispatches'))
    assert 'DO NOT LOG ME' not in raw and 'pebble.render' not in raw


def test_database_failure_on_second_pane_rolls_back_entire_batch(service, monkeypatch):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    app.store.query("CREATE TRIGGER fail_second BEFORE INSERT ON artifacts WHEN (SELECT count(*) FROM artifacts)=1 BEGIN SELECT RAISE(ABORT,'sensitive disk text'); END")
    monkeypatch.setattr(mod_worker, 'run', lambda *args, **kwargs: [pane('one'), pane('two')])
    result = mods.dispatch(cid, rid, 'manual', {}, item['id'])
    assert result['status'] == 'failed' and result['dispatches'][0]['reason'] == 'publication'
    assert not app.artifacts.list(cid)
    for table in ('artifact_revisions', 'artifact_heads', 'artifact_registry', 'mod_panes'):
        assert not app.store.query('SELECT * FROM ' + table)
    assert 'sensitive' not in json.dumps(result)


@pytest.mark.parametrize('revoke', ['disable', 'remove', 'disable_reenable'])
def test_native_revocation_during_worker_prevents_all_publication(service, monkeypatch, revoke):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)

    def execute(*args, **kwargs):
        if revoke == 'remove':
            mods.remove(item['id'])
        else:
            mods.toggle(item['id'], False, item['digest'])
            if revoke == 'disable_reenable':
                mods.toggle(item['id'], True, item['digest'])
        return [pane()]

    monkeypatch.setattr(mod_worker, 'run', execute)
    result = mods.dispatch(cid, rid, 'manual', {}, item['id'])
    assert result['status'] == 'failed' and result['dispatches'][0]['reason'] == 'revoked'
    assert not app.artifacts.list(cid)


@pytest.mark.parametrize('reason', ['timeout', 'memory', 'memory-monitor', 'runtime', 'unsupported'])
def test_sanitized_worker_failures_persist_and_resource_failures_quarantine(service, monkeypatch, reason):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)

    def execute(*args, **kwargs):
        raise mod_worker.ModWorkerError(reason)

    monkeypatch.setattr(mod_worker, 'run', execute)
    result = mods.dispatch(cid, rid, 'manual', {}, item['id'])
    assert result['status'] == 'failed' and result['dispatches'][0]['reason'] == reason
    current = mods.inspect(item['id'])
    assert current['enabled'] == (reason not in {'timeout', 'memory', 'memory-monitor'})
    assert current['error'] == reason and not app.artifacts.list(cid)
    if not current['enabled']:
        assert current['grant_digest'] is None and mods.fingerprint() == []


@pytest.mark.parametrize('when', ['before', 'worker', 'after_first_worker', 'publication'])
def test_cancellation_never_publishes_staged_panes(service, monkeypatch, when):
    app, mods, journal = service
    first = install(mods, 'One')
    install(mods, 'Two')
    cid, rid = start(app, mods, journal)
    cancel, calls = threading.Event(), []
    if when == 'before':
        cancel.set()

    def execute(*args, **kwargs):
        calls.append(1)
        if when == 'worker' or when == 'after_first_worker' and len(calls) == 2:
            cancel.set()
            raise InterruptedError('untrusted exception text')
        return [pane()]

    monkeypatch.setattr(mod_worker, 'run', execute)
    if when == 'publication':
        append = app.artifacts._append

        def cancelling_append(*args, **kwargs):
            row = append(*args, **kwargs)
            cancel.set()
            return row

        monkeypatch.setattr(app.artifacts, '_append', cancelling_append)
    with pytest.raises(InterruptedError):
        mods.dispatch(cid, rid, 'manual', {}, cancel=cancel)
    assert not app.artifacts.list(cid) and not app.store.query('SELECT * FROM mod_panes')
    assert all(row['status'] == 'cancelled' for row in app.store.query('SELECT * FROM mod_dispatches'))


def test_four_panes_across_events_and_native_stop_cas_are_enforced(service, monkeypatch):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    monkeypatch.setattr(mod_worker, 'run', lambda *args, **kwargs: [pane(str(i)) for i in range(4)])
    first = mods.dispatch(cid, rid, 'manual', {}, item['id'])
    assert len(first['artifacts']) == 4
    monkeypatch.setattr(mod_worker, 'run', lambda *args, **kwargs: [pane('fifth')])
    assert mods.dispatch(cid, rid, 'manual', {}, item['id'])['status'] == 'failed'
    assert len(app.artifacts.list(cid)) == 4
    stopped = app.artifacts.stop(cid, first['artifacts'][0]['id'], 1)
    monkeypatch.setattr(mod_worker, 'run', lambda *args, **kwargs: [pane('0', 2)])
    result = mods.dispatch(cid, rid, 'manual', {}, item['id'])
    assert result['status'] == 'failed' and result['dispatches'][0]['reason'] == 'revision'
    assert app.artifacts.get(cid, stopped['id'])['status'] == 'stopped'


def test_external_owned_update_makes_mod_mapping_stale(service):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    first = mods.dispatch(cid, rid, 'manual', {}, item['id'])['artifacts'][0]
    app.artifacts.update(cid, first['id'], 1, 'Native changed title', 'table', TABLE, rid)
    assert mods.dispatch(cid, rid, 'manual', {}, item['id'])['dispatches'][0]['reason'] == 'revision'
    assert app.artifacts.get(cid, first['id'])['title'] == 'Native changed title'


def test_durable_quota_is_shared_by_all_mods_and_survives_restart(service, monkeypatch):
    app, mods, journal = service
    for i in range(8):
        install(mods, str(i))
    cid, rid = start(app, mods, journal)
    calls = []
    monkeypatch.setattr(mod_worker, 'run', lambda *args, **kwargs: calls.append(1) or [pane()])
    mods.dispatch(cid, rid, 'manual', {})
    mods.dispatch(cid, rid, 'manual', {})
    assert len(calls) == 12 and len(app.store.query('SELECT * FROM mod_dispatches WHERE run_id=?', (rid,))) == 12
    restarted = Mods(app)
    restarted.dispatch(cid, rid, 'manual', {})
    assert len(calls) == 12 and len(app.artifacts.list(cid)) == 8


def test_restart_marks_abandoned_admission_interrupted_and_forget_is_scoped(service):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    mods.dispatch(cid, rid, 'manual', {}, item['id'])
    app.store.query("INSERT INTO mod_dispatches VALUES('abandoned',?,?,?,?,'manual','running',NULL,1,1)", (rid, cid, item['id'], item['digest']))
    restarted = Mods(app)
    assert app.store.query("SELECT status,reason FROM mod_dispatches WHERE id='abandoned'") == [{'status': 'interrupted', 'reason': 'interrupted'}]
    foreign = app.store.new_chat()
    app.store.query("INSERT INTO mod_dispatches VALUES('foreign','foreign-run',?,?,?,'manual','completed',NULL,1,1)", (foreign, item['id'], item['digest']))
    restarted.forget(cid)
    assert not app.store.query('SELECT * FROM mod_panes WHERE chat_id=?', (cid,))
    assert not app.store.query('SELECT * FROM mod_dispatches WHERE chat_id=?', (cid,))
    assert len(restarted.list()) == 1 and len(app.store.query('SELECT * FROM mod_dispatches')) == 1


@pytest.mark.parametrize('tamper', ["UPDATE mods SET manifest='{}' WHERE id=?", "UPDATE mods SET digest='forged' WHERE id=?",
                                     "UPDATE mods SET grant_digest='forged' WHERE id=?", "UPDATE mods SET error='raw secret error' WHERE id=?"])
def test_corrupted_manifest_or_grant_fails_closed(service, tamper):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    app.store.query(tamper, (item['id'],))
    for action in (mods.list, mods.fingerprint, lambda: mods.dispatch(cid, rid, 'manual', {}, item['id'])):
        with pytest.raises(PolicyError):
            action()
    assert not app.artifacts.list(cid)


def test_unsupported_platform_never_falls_back_to_node(service, monkeypatch):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    monkeypatch.setattr(mod_worker, 'supported', lambda: False)
    monkeypatch.setattr(mod_worker, 'run', lambda *args, **kwargs: pytest.fail('Unsupported worker executed'))
    assert not mods.status()['available'] and not mods.list()[0]['runtime_available']
    assert mods.dispatch(cid, rid, 'manual', {}, item['id'])['status'] == 'unsupported'
    with pytest.raises(PolicyError, match='macOS'):
        mods.toggle(item['id'], True, item['digest'])
    assert not app.store.query('SELECT * FROM mod_dispatches')


def test_mod_operations_preserve_callers_uncommitted_work(service):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    actions = [mods.list, mods.fingerprint, lambda: mods.preview(mods.example()), lambda: mods.inspect(item['id']),
               lambda: mods.toggle(item['id'], False, item['digest']), lambda: mods.remove(item['id']),
               lambda: mods.dispatch(cid, rid, 'manual', {}, item['id']), lambda: mods.forget(cid)]
    for action in actions:
        with app.store.lock:
            app.store.db.execute("INSERT INTO settings VALUES('uncommitted','true')")
            with pytest.raises(PolicyError, match='outside transaction'):
                action()
            assert app.store.db.in_transaction
            app.store.db.rollback()
    assert not app.store.query("SELECT * FROM settings WHERE key='uncommitted'")


def test_artifact_batch_requires_unique_short_pane_ids_and_at_most_four():
    for specs in ([pane('same'), pane('same')], [pane(str(i)) for i in range(5)], [pane('__proto__')]):
        with pytest.raises(PolicyError):
            validate_artifacts(specs)


# Phase 19 (p19_mods_inline_only): mods draw dashboards and prototypes from inline data only, and their
# quizzes stay self-check views whose answers are stored with the view; a mod never gets a host binding
# or a host-held answer key.
DASHBOARD = {'columns': 1, 'panels': [{'id': 'tokens', 'title': 'Tokens', 'kind': 'metric', 'data': {'value': 12, 'unit': 'tokens'}},
                                      {'id': 'counts', 'title': 'Counts', 'kind': 'table', 'data': copy.deepcopy(TABLE)}]}
PROTOTYPE = {'start': 'home', 'screens': [{'id': 'home', 'title': 'Home', 'components': [
    {'type': 'heading', 'text': 'Counts'}, {'type': 'button', 'label': 'Refresh', 'goto': 'home'}]}]}
QUIZ = {'duration_minutes': 2, 'questions': [{'question': 'Two plus two?', 'choices': ['3', '4'], 'answer': 1,
                                              'explanation': 'Two and two make four.'}]}


def test_p19_mods_inline_only_dashboards_prototypes_and_self_check_quizzes_publish(service, monkeypatch):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    assert {'dashboard', 'prototype'} <= mod_worker.KINDS and not {'form', 'html', 'mcp_media'} & mod_worker.KINDS
    panes = [{'id': 'board', 'title': 'Board', 'kind': 'dashboard', 'data': DASHBOARD},
             {'id': 'demo', 'title': 'Demo', 'kind': 'prototype', 'data': PROTOTYPE},
             {'id': 'check', 'title': 'Self-check', 'kind': 'quiz', 'data': QUIZ}]
    monkeypatch.setattr(mod_worker, 'run', lambda *args, **kwargs: copy.deepcopy(panes))
    result = mods.dispatch(cid, rid, 'manual', {}, item['id'])
    assert result['status'] == 'completed' and [r['kind'] for r in result['artifacts']] == ['dashboard', 'prototype', 'quiz']
    stored = {r['kind']: app.artifacts.get(cid, r['id'])['data'] for r in result['artifacts']}
    assert stored == {'dashboard': DASHBOARD, 'prototype': PROTOTYPE, 'quiz': QUIZ}  # the mod quiz keeps its own answers
    assert len(app.store.query('SELECT * FROM mod_panes WHERE run_id=?', (rid,))) == 3


@pytest.mark.parametrize('kind,data', [
    ('dashboard', {'panels': [{'id': 'live', 'title': 'Runs', 'kind': 'table', 'binding': {'source': 'runs.recent'}}]}),
    ('dashboard', {'panels': [DASHBOARD['panels'][0], {'id': 'live', 'title': 'Memory', 'kind': 'metric',
                                                       'binding': {'source': 'memory.stats', 'params': {}}}]}),
    ('dashboard', {'panels': [{**DASHBOARD['panels'][0], 'ｓｔｙｌｅ': 'display:none'}]}),
    ('dashboard', {'panels': []}),
    ('prototype', {**PROTOTYPE, 'screens': [{**PROTOTYPE['screens'][0], 'components': [
        {'type': 'button', 'label': 'Approve', 'goto': 'home', 'href': 'https://evil.example'}]}]}),
    ('prototype', {**PROTOTYPE, 'start': 'missing'}),
    ('prototype', {**PROTOTYPE, 'screens': [{**PROTOTYPE['screens'][0], 'components': [{'type': 'heading', 'text': 'javascript:run()'}]}]}),
    ('quiz', {'duration_minutes': 2, 'questions': [{'id': 'q1', 'question': 'Two plus two?', 'choices': ['3', '4']}]}),
    ('quiz', {**QUIZ, 'version': 2}),
])
def test_p19_mods_inline_only_refuse_bindings_links_and_host_graded_quizzes(service, monkeypatch, kind, data):
    app, mods, journal = service
    item = install(mods)
    cid, rid = start(app, mods, journal)
    monkeypatch.setattr(mod_worker, 'run', lambda *args, **kwargs: [pane('valid'), {'id': 'bad', 'title': 'Bad', 'kind': kind, 'data': data}])
    result = mods.dispatch(cid, rid, 'manual', {}, item['id'])
    assert result['status'] == 'failed' and result['dispatches'][0]['reason'] == 'invalid_artifacts'
    assert not app.artifacts.list(cid) and not app.store.query('SELECT * FROM mod_panes')
    with pytest.raises(PolicyError):
        validate_artifacts([{'id': 'bad', 'title': 'Bad', 'kind': kind, 'data': data}])
