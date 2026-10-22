"""Pinned disclosure, origin, restart and actual decision-worker confinement."""
import json
import threading
import time
from pathlib import Path

import pytest
from pebble.app import App
from pebble import hooks, skill_bundle
from pebble.context_manifest import assemble_context, ContextBudgetExceeded
from pebble.policy import PolicyError


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path/'data', tmp_path/'resources', lambda *args: None)
    yield instance
    instance.close()


def install(app, tmp_path, content=None):
    root = tmp_path/'skill'; root.mkdir(exist_ok=True)
    (root/'references').mkdir(exist_ok=True)
    (root/'references/check.md').write_text('Required Unicode guidance: Δ.\n')
    (root/'references/optional.md').write_text('Details ' * 1000)
    text = content or '---\nname: checker\ndescription: Verify Python checks\nrequired-references: [references/check.md]\n---\nComplete instruction.\n' + 'Body ' * 1100
    (root/'SKILL.md').write_text(text)
    ident = app.extension_import(str(root/'SKILL.md'))
    return root, ident


def owner(app):
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], 'Run bounded work.', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    return chat, rid


def add_hook(app, event='before_tool', source=None, **rules):
    config = {'version':1, 'event':event, **rules}
    if source:
        config['worker'] = {'language':'python','source':source,'timeout_ms':2000}
    ident = app.extension_add(kind='hook',name='Decision hook',content=json.dumps(config))
    app.extension_toggle(ident, True)
    return ident


def test_complete_instructions_required_references_and_metadata_only(app, tmp_path):
    root, ident = install(app, tmp_path)
    assert not app.extensions.skills('Python')
    app.extension_toggle(ident, True)
    menu = app.extensions.skills('Python')
    assert menu[0]['id'] == ident and 'Body' not in json.dumps(menu)
    result = app.extensions.load_skill(ident, menu[0]['digest'])
    assert result['untrusted_skill'] == (root/'SKILL.md').read_text()
    assert len(result['untrusted_skill']) > 4000
    assert result['required_references'][0]['untrusted_source'] == (root/'references/check.md').read_text()
    assert result['authorization'] == 'none'
    (root/'SKILL.md').write_text('Changed original file.')
    assert app.extensions.load_skill(ident)['untrusted_skill'] == result['untrusted_skill']


def test_reference_paging_digest_and_foreign_path(app, tmp_path):
    root, ident = install(app, tmp_path); app.extension_toggle(ident, True)
    digest = app.extensions.select_skill(ident)['digest']
    page = app.extensions.load_skill(ident, digest, 'references/optional.md')
    second = app.extensions.load_skill(ident, digest, 'references/optional.md', page['next_offset'])
    assert page['untrusted_source'] + second['untrusted_source'] == (root/'references/optional.md').read_text()[:6000]
    with pytest.raises(PolicyError): app.extensions.load_skill(ident, '0'*64)
    with pytest.raises(PolicyError): app.extensions.load_skill(ident, digest, 'foreign.txt')


def test_small_model_reference_page_can_be_narrowed_without_skipping(app, tmp_path):
    root,ident=install(app,tmp_path);app.extension_toggle(ident,True)
    digest=app.extensions.select_skill(ident)['digest'];chat=app.new_chat()
    first=app.execute('LoadSkill',{'id':ident,'digest':digest,'reference':'references/optional.md','offset':0},None,chat['id'])
    second=app.execute('LoadSkill',{'id':ident,'digest':digest,'reference':'references/optional.md','offset':first['next_offset'],'limit':20},None,chat['id'])
    assert len(first['untrusted_source'])==500 and first['next_offset']==500
    assert len(second['untrusted_source'])==20 and second['next_offset']==520
    assert first['untrusted_source']+second['untrusted_source']==(root/'references/optional.md').read_text()[:520]


@pytest.mark.parametrize('path', ['../outside.md', '/tmp/a.md', 'references/../a.md', 'a\\b', 'https://x.test/a', 'references/.env', 'a//b', 'a%2fb', 'keys/a.pem'])
def test_reference_paths_deny_escape_and_private(path):
    with pytest.raises(PolicyError): skill_bundle.safe_path(path)


def test_required_missing_symlink_and_oversized_bundle(app, tmp_path):
    root, ident = install(app, tmp_path)
    (root/'references/check.md').unlink()
    with pytest.raises(PolicyError, match='missing'): app.extension_import(str(root/'SKILL.md'))
    (root/'references/check.md').symlink_to(tmp_path/'data/pebble.sqlite')
    with pytest.raises(PolicyError, match='(?i)(symlink|symbolic)'): app.extension_import(str(root/'SKILL.md'))
    with pytest.raises(PolicyError): skill_bundle.make_bundle('x'*64_001, 'too-big')


def test_stored_bundle_tampering_and_disabled_load_fail(app, tmp_path):
    _, ident = install(app, tmp_path)
    with pytest.raises(PolicyError): app.extensions.load_skill(ident)
    app.extension_toggle(ident, True)
    row = app.store.query('SELECT snapshot FROM extension_bundles WHERE id=?',(ident,))[0]
    snapshot = json.loads(row['snapshot']); snapshot['bodies']['SKILL.md'] = 'Zm9yZ2Vk'
    app.store.query('UPDATE extension_bundles SET snapshot=? WHERE id=?',(json.dumps(snapshot),ident))
    with pytest.raises(PolicyError): app.extensions.load_skill(ident)


@pytest.mark.parametrize('text', ['---\nname: [x]\n---\nbody','---\nname: &n hello\ndescription: *n\n---\nbody','---\nrequired-references: [../outside]\n---\nbody','---\nname: missing close'])
def test_malformed_metadata_rejects_without_enabled_install(app, text):
    with pytest.raises(ValueError): app.extension_add(kind='skill', name='Invalid',content=text)
    assert not app.extensions.list()


def test_required_skill_never_silently_retires():
    messages=[{'role':'system','content':'policy'},{'role':'user','content':'x'*4000,'_pebble_required_skill':True},{'role':'user','content':'task','_pebble_current':True}]
    with pytest.raises(ContextBudgetExceeded): assemble_context(messages, [], 512)
    projected=assemble_context(messages, [], 8192)
    assert projected.messages == messages


@pytest.mark.parametrize('config', [{'version':2,'event':'before_tool'},{'version':True,'event':'before_tool'},{'event':'made_up'},{'event':'after_tool','deny_patterns':['Read']},{'event':'model_attempt','require_tests':True},{'event':'before_tool','worker':{'language':'python','source':'bad syntax (','timeout_ms':100}},{'event':'before_tool','worker':{'language':'python','source':'pass','timeout_ms':True}}])
def test_versioned_hook_contract_rejects_invalid(config):
    with pytest.raises(ValueError): hooks.validate_config(config)


def test_deny_precedence_and_scoped_audit(app):
    add_hook(app, review_patterns=['Read ']); add_hook(app, deny_patterns=['Read '])
    chat, rid = owner(app)
    result = app.extensions.hooks.dispatch(app.extensions.list(),'before_tool','Read {}',run_id=rid)
    assert result['decision']=='deny' and result['authorization']=='none'
    assert [r['decision'] for r in app.hook_events(chat['id'])] == ['deny','review']
    other=app.new_chat()
    assert app.hook_events(other['id'])==[]
    with pytest.raises(ValueError): app.hook_events(other['id'],rid)


def test_hook_pin_change_cannot_admit_tool(app):
    ident=add_hook(app,deny_patterns=['Read ']);chat,rid=owner(app)
    app.active, app.active_run=chat['id'],rid
    try:
        app.store.query('UPDATE extensions SET enabled=0 WHERE id=?',(ident,))
        with pytest.raises(PolicyError,match='changed'):app.execute('Read',{},None,chat['id'],call_id='read')
    finally:app.active=app.active_run=None


def test_real_worker_host_envelope_and_confinement(tmp_path, monkeypatch):
    import socket
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    monkeypatch.setenv('PEBBLE_TEST_SECRET', 'disposable-test-sentinel')
    secret=tmp_path/'private.txt';secret.write_text('Private sentinel')
    target=tmp_path/'write.txt'
    source=f'''def handle(event):
    import os, socket
    assert event['origin'] == 'host' and event['authorization'] == 'none'
    for action in [lambda: open({str(secret)!r}).read(), lambda: open({str(target)!r}, 'w'), lambda: socket.create_connection(('127.0.0.1', {listener.getsockname()[1]})), os.fork]:
        try:
            action()
            return {{'decision': 'deny'}}
        except OSError:
            pass
    assert not os.environ.get('PEBBLE_TEST_SECRET')
    return {{'decision': 'allow'}}
'''
    try:
        result=hooks.run_worker({'source':source,'timeout_ms':2000},{'version':1,'event':'before_tool','origin':'host','authorization':'none','data':{}},threading.Event(),2)
    finally:listener.close()
    assert result['decision']=='allow' and not target.exists()


@pytest.mark.parametrize('source', ['def handle(event):\n    while True: pass', 'def handle(event):\n    print("x" * 100000)\n    return {"decision":"allow"}', 'def handle(event):\n    return {"decision":"allow","permissions":"full"}'])
def test_worker_timeout_output_and_authority_fail_closed(app, source):
    add_hook(app,source=source);chat,rid=owner(app)
    result=app.extensions.hooks.dispatch(app.extensions.list(),'before_tool',run_id=rid)
    assert result['decision']=='deny'
    assert app.hook_events(chat['id'])[0]['status']=='worker_failed_closed'
    assert app.store.setting('permissions')['mode']=='ask'


def test_cancel_and_disabled_terminal_do_not_run_worker(app, monkeypatch):
    add_hook(app,source='def handle(event):\n    return {"decision":"allow"}')
    chat,rid=owner(app)
    original=hooks.run_worker
    monkeypatch.setattr(hooks,'run_worker',lambda *args:pytest.fail('Worker ran with disabled terminal'))
    assert app.extensions.hooks.dispatch(app.extensions.list(),'before_tool',run_id=rid,workers_allowed=False)['decision']=='deny'
    flag=threading.Event();flag.set()
    with pytest.raises(InterruptedError): original({'source':'pass'}, {}, flag,1)


def test_observer_cannot_override_committed_outcome(app):
    add_hook(app,event='after_tool',source='def handle(event):\n    return {"decision":"deny"}')
    chat,rid=owner(app)
    result=app.extensions.hooks.dispatch(app.extensions.list(),'after_tool',run_id=rid,data={'state':'completed'})
    assert result['decision']=='allow' and app.hook_events(chat['id'])[0]['status'].startswith('observer_')


def test_worker_budget_is_durable_and_outer_transactions_preserved(app):
    _,rid=owner(app)
    for _ in range(10):app.extensions.hooks.reserve(rid,2)
    with pytest.raises(PolicyError,match='exhausted'):app.extensions.hooks.reserve(rid,.1)
    with app.store.lock:
        app.store.db.execute('BEGIN IMMEDIATE')
        with pytest.raises(PolicyError,match='transaction'):app.extensions.hooks.reserve(rid,.1)
        assert app.store.db.in_transaction
        app.store.db.rollback()


def test_skill_snapshot_restart_and_removal(tmp_path):
    instance=App(tmp_path/'data',tmp_path/'resources',lambda *args:None)
    root,ident=install(instance,tmp_path);instance.extension_toggle(ident,True)
    before=instance.extensions.load_skill(ident);instance.close()
    (root/'SKILL.md').unlink()
    instance=App(tmp_path/'data',tmp_path/'resources',lambda *args:None)
    try:
        assert instance.extensions.load_skill(ident)==before
        instance.extension_remove(ident)
        assert not instance.store.query('SELECT * FROM extension_bundles')
    finally:instance.close()


def test_host_discovery_commands_never_start_model(app, tmp_path):
    install(app,tmp_path);chat=app.new_chat()
    app.send(chat['id'],'/skills');app.send(chat['id'],'/hooks')
    assert not app.runs.list(chat['id'])
    with pytest.raises(ValueError):app.send(chat['id'],'/skill nonexistent task')


def test_required_binary_reference_cannot_be_instructions():
    text='---\nrequired-references: [assets/image.png]\n---\nInstructions'
    with pytest.raises(PolicyError):skill_bundle.make_bundle(text,'binary',{'SKILL.md':text.encode(),'assets/image.png':b'\xff\x00'})


def test_disabled_native_inspection_and_zero_offset_schema(app, tmp_path):
    _,ident=install(app,tmp_path)
    result=app.dispatch('skill_inspect',{'id':ident,'reference':'references/check.md','offset':0})
    assert result['untrusted_source'].startswith('Required Unicode')
    with pytest.raises(PolicyError):app.extensions.load_skill(ident)
    from pebble.tool_catalogue import TOOLS
    app.validate('LoadSkill',{'id':ident,'digest':result['digest'],'reference':'references/check.md','offset':0},TOOLS)


def test_linked_nonstandard_reference_directory_is_snapshotted(app, tmp_path):
    root=tmp_path/'linked';root.mkdir();(root/'custom').mkdir()
    (root/'custom/guide.md').write_text('Linked instructions')
    (root/'SKILL.md').write_text('Use [the guide](custom/guide.md).')
    ident=app.extension_import(str(root/'SKILL.md'))
    assert app.extensions.inspect_skill(ident,reference='custom/guide.md')['untrusted_source']=='Linked instructions'


def test_installs_do_not_commit_an_outer_transaction(app, tmp_path):
    root,ident=install(app,tmp_path)
    for invoke in [lambda:app.extension_import(str(root/'SKILL.md')),lambda:app.extension_add(kind='skill',name='new',content='body'),lambda:app.extension_toggle(ident,True),lambda:app.extension_remove(ident)]:
        with app.store.lock:
            app.store.db.execute('BEGIN IMMEDIATE')
            app.store.db.execute("INSERT INTO settings VALUES('pending-extension-test','true')")
            with pytest.raises(PolicyError,match='transaction'):invoke()
            assert app.store.db.in_transaction
            assert app.store.db.execute("SELECT 1 FROM settings WHERE key='pending-extension-test'").fetchone()
            app.store.db.rollback()


def test_live_hook_cancellation_has_known_no_effect(app, monkeypatch):
    add_hook(app,source='def handle(event):\n    while True: pass')
    chat,rid=owner(app);execution,token=app.controller.enter(app.runs.get(rid))
    app.active,app.active_run=chat['id'],rid
    monkeypatch.setattr(app.artifacts,'render',lambda *args:pytest.fail('Cancelled hook admitted an effect'))
    timer=threading.Timer(.15,execution.cancel.set);timer.start()
    try:
        with pytest.raises(hooks.HookInterrupted):app.execute('UIRender',{'title':'view','kind':'diagram','data':'{}'},None,chat['id'],call_id='view')
        receipt=next(t for t in app.runs.tools(rid) if t['call_id']=='view')
        assert receipt['state']=='completed' and not app.store.actions(chat['id'])
    finally:
        timer.cancel();app.active=app.active_run=None;app.controller.leave(execution,token)


def test_worker_cannot_forge_host_metadata(app):
    chat,rid=owner(app)
    with pytest.raises(PolicyError,match='metadata'):app.extensions.hooks.dispatch([],'before_tool',run_id=rid,data={'origin':'human','permissions':'full'})


def test_native_approval_revalidates_new_hook_before_effect(app, tmp_path):
    root=tmp_path/'project';root.mkdir();chat=app.new_chat(str(root))
    from pebble.policy import Workspace
    pending=app.execute('Write',{'file_path':'new.txt','content':'proposed'},Workspace(root),chat['id'])
    add_hook(app,deny_patterns=['Write '])
    with pytest.raises(PolicyError,match='Blocked by hook'):app.approve(chat['id'],pending['action_id'])
    assert not (root/'new.txt').exists()


def test_explicit_skill_overrides_fast_weather_and_remembers_selection(app, tmp_path, monkeypatch):
    _,ident=install(app,tmp_path,content='---\nname: weather-guide\ndescription: Weather guidance\n---\nUse the task guidance.')
    app.extension_toggle(ident,True);seen=[]
    def complete(messages,tools,cancel,token,effort):
        seen.extend(messages);token('Task handled through selected guidance.')
        return {'role':'assistant','content':'Task handled through selected guidance.'}
    app.model.complete=complete
    monkeypatch.setattr('pebble.app.weather',lambda **args:pytest.fail('Explicit skill bypassed by weather shortcut'))
    chat=app.new_chat();result=app.send(chat['id'],'/skill weather-guide What is the weather today in Paris?')
    app.worker.join(timeout=5)
    assert app.runs.get(result['run_id'])['state']=='completed'
    assert app.runs.get(result['run_id'])['config']['explicit_skill']['id']==ident
    assert any(m.get('_pebble_required_skill') for m in seen)


def test_legacy_inline_skill_migrates_without_reading_source(tmp_path):
    from pebble.store import Store
    store=Store(tmp_path/'data')
    store.query('CREATE TABLE extensions(id TEXT PRIMARY KEY,kind TEXT,name TEXT,content TEXT,config TEXT,enabled INTEGER,created REAL)')
    store.query('INSERT INTO extensions VALUES(?,?,?,?,?,?,?)',('old','skill','Legacy','Complete legacy body','{}',1,time.time()))
    store.db.close()
    instance=App(tmp_path/'data',tmp_path/'resources',lambda *args:None)
    try:assert instance.extensions.load_skill('old')['untrusted_skill']=='Complete legacy body'
    finally:instance.close()
