"""Portable memory archive: export, pure preview and one-transaction import.
Tombstones travel and apply first, consent and layer switches are never
widened, tampered or oversized archives are refused, and nothing imported
counts as current source evidence or grants anything."""

import base64
import json
import os
import time

import pytest

from pebble import memory_graph, memory_portable as portable
from pebble.app import App
from pebble.memory import KINDS
from pebble.policy import Workspace


@pytest.fixture
def pair(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    apps = [App(tmp_path / name / 'data', tmp_path / name / 'resources', lambda *args: None) for name in ('source', 'target')]
    yield apps[0], apps[1], root
    for instance in apps:
        instance.close()


def start(app, root=None, goal='Inspect project evidence.', chat=None):
    chat = chat or app.new_chat(str(root) if root else None)
    rid = app.runs.create(chat['id'], goal, app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    app.learning.begin_run(rid)
    return chat, rid


def read(app, chat, rid, root, name, text=None, call_id='read'):
    if text is not None:
        (root / name).write_text(text)
    execution, token = app.controller.enter(app.runs.get(rid))
    app.active, app.active_run = chat['id'], rid
    try:
        return app.execute('Read', {'file_path': name}, Workspace(root), chat['id'], call_id=call_id)
    finally:
        app.active = app.active_run = None
        app.controller.leave(execution, token)


def page(app, rid, call, url, text):
    app.runs.begin_tool(rid, call, 'ReadURL', {'url': url})
    app.runs.end_tool(rid, call, {'url': url, 'retrieved_at': time.time(), 'untrusted_source': text})


def recipe_run(app, root):
    chat, rid = start(app, root)
    aid = app.store.action(chat['id'], 'tests', {'recipe': 'go-test', 'root': str(root), 'root_identity': list(Workspace(root).identity),
        'plan': {'recipe': 'go-test', 'argv': ['/usr/local/go/bin/go', 'test', './...']}, 'drafts': [],
        'result': {'exit_code': 0, 'cancelled': False, 'outcome': 'passed'}})
    app.store.query("UPDATE actions SET status='applied' WHERE id=?", (aid,))
    app.runs.begin_tool(rid, 'tests', 'RunRecipe', {'recipe': 'go-test'})
    app.runs.end_tool(rid, 'tests', {'status': 'applied', 'action_id': aid, 'result': {'exit_code': 0, 'cancelled': False}})
    app.runs.transition(rid, 'completed'); app.learning.collect(rid)


def populate(app, root):
    chat, rid = start(app, root, 'I prefer concise answers. Check the config.')
    read(app, chat, rid, root, 'config.toml', 'port = 8080\n', call_id='read-config-7f3c')
    page(app, rid, 'url-9d2e', 'https://a.example/status', 'Next release: November 2\n')
    app.runs.transition(rid, 'completed'); app.learning.collect(rid)
    app.memory.episode(chat['id'], 'Check the config.', str(root), [], 'completed', run_id=rid)
    recipe_run(app, root); recipe_run(app, root)
    app.memory.put('semantic', 'The staging host is named quartz.', scope=str(root))
    return chat, rid


def state(app):
    with app.store.lock:
        return portable.local_state(app.learning, app.store.db)


def import_all(app, archive):
    plan = portable.preview(archive, state(app))
    return plan, portable.apply(app.learning, archive, plan['plan_digest'])


def exported(app):
    return portable.export(app.learning)['archive']


def imported(app, kind=None):
    return [m for m in app.memory.list(kind) if m['origin'] == 'imported']


def reseal(archive):
    archive = json.loads(json.dumps(archive))
    for name, value in archive['sections'].items():
        archive['manifest']['sections'][name] = {'sha256': portable.section_digest(value),
                                                 'count': len(value) if isinstance(value, list) else 1}
    return archive


def fact(app):
    return next(m for m in app.memory.list('semantic') if m['origin'] == 'auto-learned')


# Export --------------------------------------------------------------------

def test_export_is_checksummed_opaque_and_leaves_operational_state_behind(pair):
    source, _, root = pair
    chat, rid = populate(source, root)
    source.learning.propose(rid, 'episodic', 'Pending lesson Z9Q.', evidence_ids=['read-config-7f3c'])
    before = source.store.db.total_changes
    result = portable.export(source.learning)
    assert source.store.db.total_changes == before and result['refused'] == [] and result['authorization'] == 'none'
    archive = result['archive']
    data = portable.encode(archive)
    assert archive['format'] == 'pebble-memory-archive/1' and portable.decode(data) == archive
    for name, value in archive['sections'].items():
        assert archive['manifest']['sections'][name]['sha256'] == portable.section_digest(value)
    records = archive['sections']['records']
    assert {r['kind'] for r in records} == {'profile', 'semantic', 'temporal', 'procedural', 'episodic'}
    text = data.decode()
    for secret in ('Pending lesson Z9Q', 'Engineering workflow', 'read-config-7f3c', 'url-9d2e', str(root)):
        assert secret not in text
    for record in records:
        if record['provenance']:
            assert all(r.startswith('o:') for r in record['provenance']['receipt_ids'])
            assert record['provenance']['run_id'].startswith('o:')
            assert not isinstance(record['provenance'].get('root_identity'), list)
    assert archive['sections']['policy'] == {'memory_enabled': True, 'layers': dict.fromkeys(KINDS, True)}


def test_credential_shaped_records_are_refused_on_export(pair):
    source, _, root = pair
    now = time.time()
    for ident, text in (('cred-1', 'Deploy with api_key: A1b2C3d4E5f6G7h8J9'), ('cred-2', 'Token ghp_' + 'a' * 36)):
        source.store.query('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)', (ident, 'semantic', text, str(root), None, 'user', now, now, None))
    source.memory.put('semantic', 'The staging host is named quartz.', scope=str(root))
    result = portable.export(source.learning)
    assert sorted(result['refused'], key=lambda r: r['id']) == [{'id': 'cred-1', 'reason': 'credential_shaped_text'},
                                                                {'id': 'cred-2', 'reason': 'credential_shaped_text'}]
    text = portable.encode(result['archive']).decode()
    assert 'A1b2C3d4E5f6G7h8J9' not in text and 'ghp_' not in text and 'quartz' in text


# Import --------------------------------------------------------------------

def test_round_trip_imports_once_as_data_needing_revalidation(pair):
    source, target, root = pair
    populate(source, root)
    archive = portable.encode(exported(source))
    chat = target.new_chat(str(root))
    before = target.store.db.total_changes
    plan = portable.preview(archive, state(target))
    assert target.store.db.total_changes == before  # preview is pure
    assert plan['records']['import'] == 6 and plan['needs_revalidation'] == 2 and plan['authorization'] == 'none'
    result = portable.apply(target.learning, archive, plan['plan_digest'])
    assert result['imported'] == 6
    records = imported(target)
    assert len(records) == 6 and {m['scope'] for m in records} == {str(root)}
    status = {m['kind']: m['learning_status'] for m in records if m['learning_status']}
    assert status == {'semantic': 'needs_revalidation', 'procedural': 'needs_revalidation', 'profile': 'active', 'temporal': 'active'}
    recalled = target.memory.retrieve('How should answers look? port 8080 go-test quartz', chat['id'], str(root))
    contents = json.dumps(recalled)
    assert 'concise answers' in contents and 'quartz' in contents
    assert 'port = 8080' not in contents and 'go-test' not in contents  # nothing imported is current source evidence
    assert all(i.get('authorization') == 'none' for i in recalled if i.get('evidence_class'))
    assert target.store.setting('permissions')['mode'] == 'ask' and not target.store.actions(chat['id'])
    again = portable.preview(archive, state(target))
    assert again['records']['import'] == 0 and again['records']['skip'] == {'already_present': 6}
    assert portable.apply(target.learning, archive, again['plan_digest'])['imported'] == 0
    assert len(imported(target)) == 6


def test_imported_file_facts_never_match_a_replaced_root(pair, tmp_path):
    source, target, root = pair
    populate(source, root)
    archive = exported(source)
    os.rename(root, tmp_path / 'old-project'); root.mkdir(); (root / 'config.toml').write_text('port = 8080\n')
    target.new_chat(str(root))
    import_all(target, archive)
    semantic = next(m for m in imported(target, 'semantic') if m['learning_status'])
    assert semantic['learning_status'] == 'needs_revalidation'
    target.store.query("UPDATE memory_evidence SET status='active' WHERE memory_id=?", (semantic['id'],))
    assert target.learning.audit(str(root), memory_ids=[semantic['id']]) == set()
    assert next(m for m in target.memory.list('semantic') if m['id'] == semantic['id'])['learning_status'] == 'stale'


def test_expired_temporal_records_are_dropped(pair):
    source, target, root = pair
    populate(source, root)
    archive = exported(source)
    target.new_chat(str(root))
    later = {**state(target), 'now': time.time() + 2 * 86400}
    plan = portable.preview(archive, later)
    assert {d['reason'] for d in plan['decisions'] if d['kind'] == 'temporal'} == {'expired'}


def test_projects_map_only_by_path_and_never_cross(pair, tmp_path):
    source, target, root = pair
    populate(source, root)
    archive = exported(source)
    other = tmp_path / 'other'; other.mkdir()
    elsewhere = target.new_chat(str(other))
    plan, result = import_all(target, archive)
    assert result['imported'] == 0 and plan['records']['skip'] == {'project_not_present': 6}
    recalled = target.memory.retrieve('concise answers quartz', elsewhere['id'], str(other))
    assert not imported(target) and not [i for i in recalled if i.get('origin') == 'imported']
    target.new_chat(str(root))
    assert import_all(target, archive)[1]['imported'] == 6
    assert {m['scope'] for m in imported(target)} == {str(root)}


def test_tombstones_apply_first_and_forget_matching_local_records(pair):
    source, target, root = pair
    populate(source, root)
    first = exported(source)
    source.memory_delete(fact(source)['id'])
    second = exported(source)
    assert len(second['sections']['tombstones']) == 1
    chat = target.new_chat(str(root))
    import_all(target, first)
    port = next(m for m in imported(target, 'semantic') if 'port = 8080' in m['content'])
    plan, result = import_all(target, second)
    assert plan['forgets_local'] == [port['id']] and result['forgotten'] == 1
    assert not any('port = 8080' in m['content'] for m in target.memory.list('semantic'))
    assert target.store.query('SELECT scope FROM memory_tombstone_blooms')[0]['scope'] == \
        memory_graph.scope_key(str(root), target.learning.salts()[0])
    plan, _ = import_all(target, first)
    assert [d['reason'] for d in plan['decisions'] if d['kind'] == 'semantic' and d['reason'] != 'already_present'] == ['forgotten']
    # Local learning from the same file stays blocked.
    _, rid = start(target, root, chat=chat); read(target, chat, rid, root, 'config.toml')
    target.runs.transition(rid, 'completed'); target.learning.collect(rid)
    assert not any('port = 8080' in m['content'] for m in target.memory.list('semantic'))


def test_a_tombstone_in_the_same_archive_blocks_its_record(pair):
    source, target, root = pair
    populate(source, root)
    archive = exported(source)
    record = next(r for r in archive['sections']['records'] if r['kind'] == 'semantic' and r['provenance'])
    tampered = reseal(archive)
    tampered['sections']['tombstones'].append({'scope': record['scope'], 'kind': 'semantic',
        'slot_item': memory_graph.item('slot', 'semantic', record['slot']), 'fingerprint_item': None})
    tampered = reseal(tampered)
    target.new_chat(str(root))
    plan, result = import_all(target, tampered)
    assert next(d for d in plan['decisions'] if d['ref'] == record['ref'])['reason'] == 'forgotten'
    assert result['imported'] == 5


def test_forgetting_an_imported_record_persists_across_reimport_and_restart(pair, tmp_path):
    source, _, root = pair
    populate(source, root)
    archive = exported(source)
    home, resources = tmp_path / 'restarted' / 'data', tmp_path / 'restarted' / 'resources'
    target = App(home, resources, lambda *args: None)
    try:
        target.new_chat(str(root))
        import_all(target, archive)
        target.memory_delete(imported(target, 'profile')[0]['id'])
    finally:
        target.close()
    target = App(home, resources, lambda *args: None)
    try:
        plan, result = import_all(target, archive)
        assert next(d for d in plan['decisions'] if d['kind'] == 'profile')['reason'] == 'forgotten'
        assert not imported(target, 'profile') and result['imported'] == 0
    finally:
        target.close()


def test_unmapped_project_tombstones_keep_forgetting_when_the_project_appears(pair):
    source, target, root = pair
    populate(source, root)
    source.memory_delete(fact(source)['id'])
    archive = exported(source)
    plan, result = import_all(target, archive)  # the target does not know this project yet
    assert result['imported'] == 0 and plan['tombstones']['foreign_salts'] == 1
    registered = target.store.query("SELECT salt FROM memory_scope_salts WHERE origin='imported'")
    assert [row['salt'] for row in registered] == [source.learning.salts()[0]]
    assert target.learning.salts() == [target.store.query("SELECT salt FROM memory_scope_salts WHERE origin='local'")[0]['salt'],
                                       source.learning.salts()[0]]
    chat, rid = start(target, root); read(target, chat, rid, root, 'config.toml')
    target.runs.transition(rid, 'completed'); target.learning.collect(rid)
    assert not any('port = 8080' in m['content'] for m in target.memory.list('semantic'))


# Consent and disabled layers ---------------------------------------------

def test_disabled_layers_never_backfill_on_import(pair):
    source, target, root = pair
    populate(source, root)
    archive = exported(source)
    target.new_chat(str(root))
    target.setting('memory_layers', {kind: kind != 'profile' for kind in KINDS})
    plan, result = import_all(target, archive)
    assert next(d for d in plan['decisions'] if d['kind'] == 'profile')['reason'] == 'layer_disabled'
    target.setting('memory_layers', dict.fromkeys(KINDS, True))
    assert not imported(target, 'profile') and result['imported'] == 5
    # A layer the source had switched off stays off, even in a crafted archive.
    crafted = reseal(archive)
    crafted['sections']['policy']['layers']['temporal'] = False
    crafted = reseal(crafted)
    again = portable.preview(crafted, state(target))
    assert next(d for d in again['decisions'] if d['kind'] == 'temporal')['reason'] == 'source_layer_disabled'


def test_source_layer_switched_off_is_not_exported_and_crafted_records_are_skipped(pair):
    source, target, root = pair
    populate(source, root)
    source.setting('memory_layers', {kind: kind != 'temporal' for kind in KINDS})
    archive = exported(source)
    assert archive['sections']['policy']['layers']['temporal'] is False
    assert not [r for r in archive['sections']['records'] if r['kind'] == 'temporal']
    source.setting('memory_layers', dict.fromkeys(KINDS, True))
    full = exported(source)
    crafted = reseal(full)
    crafted['sections']['policy']['layers']['temporal'] = False
    crafted = reseal(crafted)
    target.new_chat(str(root))
    plan, _ = import_all(target, crafted)
    assert next(d for d in plan['decisions'] if d['kind'] == 'temporal')['reason'] == 'source_layer_disabled'
    assert not imported(target, 'temporal')


def test_import_never_changes_switches_or_consent_and_memory_off_imports_only_forgetting(pair):
    source, target, root = pair
    populate(source, root)
    source.memory_delete(fact(source)['id'])
    archive = exported(source)
    target.new_chat(str(root))
    target.setting('memory_enabled', False)
    settings = {key: target.store.setting(key) for key in ('memory_enabled', 'memory_layers', 'permissions')}
    plan, result = import_all(target, archive)
    assert result['imported'] == 0 and set(plan['records']['skip']) == {'layer_disabled'}
    assert target.store.query('SELECT 1 FROM memory_tombstone_blooms')
    assert {key: target.store.setting(key) for key in settings} == settings
    assert not target.store.query('SELECT 1 FROM memory_run_consent')


def test_a_contest_travels_with_the_archive(pair):
    source, target, root = pair
    chat, rid = start(source, root)
    page(source, rid, 'a', 'https://a.example/status', 'Next release: November 2\n')
    page(source, rid, 'b', 'https://b.example/news', 'Next release: November 9\n')
    source.runs.transition(rid, 'completed'); source.learning.collect(rid)
    archive = exported(source)
    assert [r['relation'] for r in archive['sections']['relations']] == ['contradicts']
    target_chat = target.new_chat(str(root))
    import_all(target, archive)
    assert {m['learning_status'] for m in imported(target, 'temporal')} == {'contested'}
    assert not [i for i in target.memory.retrieve('next release', target_chat['id'], str(root)) if i.get('kind') == 'temporal']


# Refusals ------------------------------------------------------------------

def _edit(archive, change):
    archive = json.loads(json.dumps(archive))
    change(archive)
    return archive


MUTATIONS = {
    'edited_content': (lambda a: a['sections']['records'][0].update(content='Changed.'), False, 'checksum_mismatch'),
    'wrong_count': (lambda a: a['manifest']['sections']['records'].update(count=99), False, 'checksum_mismatch'),
    'format': (lambda a: a.update(format='pebble-memory-archive/2'), False, 'unsupported_format'),
    'extra_key': (lambda a: a.update(extra=1), False, 'malformed'),
    'working_record': (lambda a: a['sections']['records'][0].update(kind='working'), True, 'malformed'),
    'unknown_salt': (lambda a: a['sections']['records'][0].update(scope='p:' + '0' * 12 + ':' + '0' * 40), True, 'malformed'),
    'bloom_size': (lambda a: a['sections']['blooms'].append(
        {'scope': 'global', 'bits': base64.b64encode(b'\x00' * 10).decode(), 'items': 1}), True, 'bloom_parameters'),
    'bloom_parameters': (lambda a: a['manifest'].update(bloom={'bits': 8, 'hashes': 1}), False, 'bloom_parameters'),
    'oversized_text': (lambda a: a['sections']['records'][0].update(content='x' * 4001), True, 'oversized'),
}


@pytest.mark.parametrize('name', sorted(MUTATIONS))
def test_tampered_archives_are_refused_and_import_nothing(pair, name):
    source, target, root = pair
    populate(source, root)
    change, sealed, reason = MUTATIONS[name]
    archive = _edit(exported(source), change)
    archive = reseal(archive) if sealed else archive
    target.new_chat(str(root))
    with pytest.raises(portable.ArchiveError) as refused:
        portable.preview(archive, state(target))
    assert refused.value.reason == reason
    with pytest.raises(portable.ArchiveError):
        portable.apply(target.learning, archive, '0' * 64)
    assert not imported(target) and not target.store.query('SELECT 1 FROM memory_tombstone_blooms')


@pytest.mark.parametrize('data,reason', [
    (b'{"format":"pebble-memory-archive/1","format":"x"}', 'malformed'),
    (b'{"format": NaN}', 'malformed'),
    (b'{"created": 1e400}', 'malformed'),
    (b'[' * 100000 + b']' * 100000, 'malformed'),
    (b'\xff\xfe', 'malformed'),
    (12, 'malformed'),
])
def test_malformed_bytes_are_refused_before_validation(data, reason):
    with pytest.raises(portable.ArchiveError) as refused:
        portable.decode(data)
    assert refused.value.reason == reason


def test_oversized_archives_are_refused_before_parsing(pair, monkeypatch):
    source, target, root = pair
    populate(source, root)
    archive = portable.encode(exported(source))
    target.new_chat(str(root))
    monkeypatch.setattr(portable, 'MAX_ARCHIVE_BYTES', len(archive) - 1)
    with pytest.raises(portable.ArchiveError, match='oversized'):
        portable.preview(archive, state(target))
    monkeypatch.setattr(portable, 'MAX_ARCHIVE_BYTES', 8 * 1024 * 1024)
    monkeypatch.setattr(portable, 'MAX_RECORDS', 2)
    with pytest.raises(portable.ArchiveError) as refused:
        portable.preview(archive, state(target))
    assert refused.value.reason == 'oversized'
    with pytest.raises(portable.ArchiveError, match='oversized'):
        portable.export(source.learning)


def test_stale_preview_is_refused_and_import_is_one_transaction(pair):
    source, target, root = pair
    populate(source, root)
    source.memory_delete(fact(source)['id'])
    archive = exported(source)
    target.new_chat(str(root))
    plan = portable.preview(archive, state(target))
    target.setting('memory_layers', {kind: kind != 'episodic' for kind in KINDS})
    with pytest.raises(portable.ArchiveError, match='stale_preview'):
        portable.apply(target.learning, archive, plan['plan_digest'])
    target.setting('memory_layers', dict.fromkeys(KINDS, True))
    plan = portable.preview(archive, state(target))
    target.store.query("CREATE TRIGGER fail_import BEFORE INSERT ON memory_evidence BEGIN SELECT RAISE(ABORT,'fixture crash'); END")
    with pytest.raises(Exception, match='fixture crash'):
        portable.apply(target.learning, archive, plan['plan_digest'])
    assert not imported(target) and not target.store.query('SELECT 1 FROM memory_tombstone_blooms')
    target.store.query('DROP TRIGGER fail_import')
    assert portable.apply(target.learning, archive, plan['plan_digest'])['imported'] == plan['records']['import'] > 0
    assert portable.public(plan).keys() == plan.keys() - {'_plan'}
