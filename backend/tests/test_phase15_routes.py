"""Phase 15 parent integration: message route attribution and the reviewed model handoff routes (C10)."""

import json
import re
import time
import uuid
from pathlib import Path

import pytest

from pebble import model_handoff
from pebble.app import App
from pebble.tool_catalogue import TOOLS

ROOT = Path(__file__).resolve().parents[2]
CLOUD_A = {'id': 'cloud-a', 'kind': 'openai', 'name': 'Cloud A', 'url': 'https://a.example/v1', 'model': 'model-a',
           'context': 32768, 'budget': 32768, 'budget_version': 2, 'context_mode': 'full', 'tools': True}
CLOUD_B = {**CLOUD_A, 'id': 'cloud-b', 'name': 'Cloud B', 'url': 'https://b.example/v1', 'model': 'model-b'}
DEVICE = {**CLOUD_A, 'id': 'loopback', 'name': 'Loopback', 'url': 'http://127.0.0.1:8080/v1', 'model': 'local'}
SECRET = 'device-only detail 7f3a'


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    a.store.set_setting('model_profiles', [dict(CLOUD_A), dict(CLOUD_B), dict(DEVICE)])
    a.store.set_setting('active_profile', 'cloud-a')
    launched = []

    def launch(chat, goal, effort, sources, rid, resume=None):
        # A settled stand-in for the runner: one assistant reply under the run's route.
        launched.append({'chat_id': chat['id'], 'goal': goal, 'run_id': rid,
                         'profile': a.model.current()['id']})
        a.finish(chat['id'], uuid.uuid4().hex, 'Reply to: ' + goal, {}, time.monotonic())
        a.runs.transition(rid, 'completed')
        a.active, a.active_run = None, None
    a.controller.launch = launch
    a.launched = launched
    yield a
    a.close()


def routes(app, cid):
    return {r['content']: r['route_class'] for r in app.store.query(
        'SELECT m.content,r.route_class FROM messages m LEFT JOIN message_routes r ON r.message_id=m.id '
        'WHERE m.chat_id=? ORDER BY m.created', (cid,))}


def mixed_chat(app):
    """A conversation with turns under the loopback route, cloud A, and one unattributed record."""
    cid = app.new_chat()['id']
    app.model_select('loopback')
    app.send(cid, 'Remember the ' + SECRET)
    app.model_select('cloud-a')
    run_id = app.send(cid, 'Summarise the plan')['run_id']
    app.store.query('INSERT INTO messages VALUES(?,?,?,?,?)', ('legacy-1', cid, 'assistant', 'legacy note', time.time()))
    return cid, run_id


def test_messages_carry_the_route_class_they_were_produced_under(app):
    cid, _ = mixed_chat(app)
    seen = routes(app, cid)
    assert seen['Remember the ' + SECRET] == 'device'
    assert seen['Reply to: Remember the ' + SECRET] == 'device'
    assert seen['Summarise the plan'] == 'cloud:https://a.example'
    assert seen['Reply to: Summarise the plan'] == 'cloud:https://a.example'
    assert seen['legacy note'] is None  # Unattributed: unknown to a handoff.
    app.send(cid, '/help')
    assert routes(app, cid)['/help'] == 'device'
    app.delete_chat(cid)
    assert not app.store.query('SELECT 1 FROM message_routes WHERE chat_id=?', (cid,))


def test_preview_is_content_free_and_withholds_other_classes(app):
    cid, run_id = mixed_chat(app)
    preview = app.model_handoff_preview(cid, run_id, 'cloud-b')
    text = json.dumps(preview)
    assert SECRET not in text and 'Summarise' not in text and 'legacy note' not in text
    assert preview['source_class'] == 'cloud:https://a.example'
    assert preview['destination_class'] == 'cloud:https://b.example'
    assert preview['send_count'] == 0 and preview['counts'] == {}
    assert len(preview['withheld_ids']) == 5 and 'legacy-1' in preview['withheld_ids']
    assert preview['withheld_by_class'] == {'cloud:https://a.example': 2, 'device': 2, 'unknown': 1}
    assert re.fullmatch(r'sha256:[0-9a-f]{12}', preview['destination_fingerprint_prefix'])
    assert preview['authorization'] == 'none' and preview['crosses_class'] is True
    released = app.model_handoff_preview(cid, run_id, 'cloud-b', release=['cloud:https://a.example'])
    assert released['send_count'] == 2 and released['counts'] == {
        'assistant': {'cloud:https://a.example': 1}, 'user': {'cloud:https://a.example': 1}}
    assert released['released_classes'] == ['cloud:https://a.example']
    assert released['plan_fingerprint'] != preview['plan_fingerprint']


def test_start_needs_approval_and_seeds_only_cleared_items(app):
    cid, run_id = mixed_chat(app)
    fingerprint = app.model_handoff_preview(cid, run_id, 'cloud-b', release=['cloud:https://a.example'])['plan_fingerprint']
    with pytest.raises(model_handoff.ModelHandoffError, match='not_approved'):
        app.model_handoff_start(cid, fingerprint)
    assert app.model.current()['id'] == 'cloud-a' and len(app.launched) == 2
    assert app.model_handoff_approve(cid, fingerprint) == {
        'approved': True, 'plan_fingerprint': fingerprint, 'authorization': 'none'}
    started = app.model_handoff_start(cid, fingerprint)
    assert started['sent'] == 2 and started['withheld'] == 3 and started['destination_class'] == 'cloud:https://b.example'
    new = started['chat_id']
    assert new != cid and app.model.current()['id'] == 'cloud-b'
    seen = routes(app, new)
    assert seen['Summarise the plan'] == 'cloud:https://a.example'  # Released items keep their origin class.
    assert seen[App.HANDOFF_TEXT] == 'cloud:https://b.example'
    assert not any(SECRET in k or 'legacy note' in k for k in seen)
    assert app.launched[-1] == {'chat_id': new, 'goal': App.HANDOFF_TEXT, 'run_id': started['run_id'],
                                'profile': 'cloud-b'}
    events = {e['kind']: e['payload'] for e in app.runs.events(run_id)}
    assert events['model_handoff_approved']['sent'] == 2 and events['model_handoff_started']['chat_id'] == new
    assert SECRET not in json.dumps(events) and 'Summarise' not in json.dumps(events)
    with pytest.raises(ValueError, match='no longer available'):
        app.model_handoff_start(cid, fingerprint)  # One start per approval.


def test_changed_destination_is_refused_and_nothing_is_sent(app):
    cid, run_id = mixed_chat(app)
    fingerprint = app.model_handoff_preview(cid, run_id, 'cloud-b')['plan_fingerprint']
    app.model_handoff_approve(cid, fingerprint)
    profiles = app.store.setting('model_profiles')
    next(p for p in profiles if p['id'] == 'cloud-b')['model'] = 'model-b-2'
    app.store.set_setting('model_profiles', profiles)
    chats = len(app.store.query('SELECT id FROM chats'))
    with pytest.raises(model_handoff.ModelHandoffError, match='destination_changed'):
        app.model_handoff_start(cid, fingerprint)
    assert len(app.store.query('SELECT id FROM chats')) == chats
    assert app.model.current()['id'] == 'cloud-a' and len(app.launched) == 2


def test_handoff_refusals(app):
    cid, run_id = mixed_chat(app)
    other = app.new_chat()['id']
    with pytest.raises(ValueError, match='Run not found'):
        app.model_handoff_preview(other, run_id, 'cloud-b')
    with pytest.raises(ValueError, match='profile not found'):
        app.model_handoff_preview(cid, run_id, 'missing')
    with pytest.raises(ValueError, match='no longer available'):
        app.model_handoff_approve(cid, 'sha256:' + '0' * 64)
    fingerprint = app.model_handoff_preview(cid, run_id, 'loopback')['plan_fingerprint']
    with pytest.raises(ValueError, match='no longer available'):
        app.model_handoff_approve(other, fingerprint)  # Bound to its own conversation.
    app.model_handoff_approve(cid, fingerprint)
    app.active = 'busy'
    try:
        with pytest.raises(ValueError, match='Stop the current task'):
            app.model_handoff_start(cid, fingerprint)
    finally:
        app.active = None
    app.delete_chat(cid)
    with pytest.raises(ValueError, match='no longer available'):
        app.model_handoff_start(cid, fingerprint)


def test_handoff_routes_are_native_only():
    names = {'model_handoff_preview', 'model_handoff_approve', 'model_handoff_start'}
    assert not names & {t['function']['name'] for t in TOOLS}
    allowed = re.search(r'const ALLOWED: &\[&str\] = &\[(.*?)\];', (ROOT / 'src-tauri/src/main.rs').read_text(), re.S)
    assert names <= set(re.findall(r'"([a-z_]+)"', allowed.group(1)))
    from pebble.tool_authz import NATIVE_ONLY
    assert names <= NATIVE_ONLY  # A catalogue entry with one of these names is refused for every model actor.
    # Phase 46 (1.9.29, D214): remote_policy.py is the iPhone bridge's copy of main.rs's ALLOWED, which names every
    # main-window route; the phone reaches them through the same App.dispatch as the main window, after its unlock.
    for path in (ROOT / 'backend/pebble').glob('*.py'):
        if path.name not in {'app.py', 'model_handoff.py', 'tool_authz.py', 'remote_policy.py'}:
            assert 'model_handoff_start' not in path.read_text(), path.name


# Native memory routes: contests, relations and the portable archive files.

from test_memory_graph import contested_pair, learned  # noqa: E402


@pytest.fixture
def plain(tmp_path):
    a = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield a
    a.close()


def test_contest_routes_show_both_sides_and_resolve_natively(plain, tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    chat, rid, readme, config = contested_pair(plain, root)
    groups = plain.memory_contests()['groups']
    assert {g['memory_id'] for g in groups} == {readme['id'], config['id']}
    group = next(g for g in groups if g['memory_id'] == readme['id'])
    assert [c['memory_id'] for c in group['counterparts']] == [config['id']]
    assert json.loads(group['content'])['quote'] == 'Port: 8080'
    assert json.loads(group['counterparts'][0]['content'])['quote'] == 'port = 3000'
    relations = plain.memory_relations(readme['id'])['relations']
    assert [r['relation'] for r in relations] == ['contradicts']
    with pytest.raises(Exception, match='Choose keep'):
        plain.memory_resolve_contest(readme['id'], 'trust_the_model')
    plain.active = chat['id']
    try:
        with pytest.raises(ValueError, match='Wait for the current task'):
            plain.memory_resolve_contest(readme['id'], 'keep')
    finally:
        plain.active = None
    result = plain.memory_resolve_contest(readme['id'], 'keep')
    assert result['forgotten'] == [config['id']] and result['authorization'] == 'none'
    assert [m['id'] for m in learned(plain, 'semantic', 'active')] == [readme['id']]
    assert plain.memory_contests()['groups'] == []


def test_archive_file_routes_round_trip_and_refuse_overwrite_and_stale_previews(plain, tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    plain.memory.put('semantic', 'Project Orion uses SQLite')  # Global: a project scope needs the project.
    out = tmp_path / 'exports'; out.mkdir()
    target = out / 'memory.pebble-archive'
    exported = plain.memory_archive_export_to(str(target))
    assert exported['bytes'] == target.stat().st_size and (target.stat().st_mode & 0o777) == 0o600
    with pytest.raises(ValueError, match='already exists'):
        plain.memory_archive_export_to(str(target))
    for bad in ('relative/path', str(out / 'missing' / 'x'), str(out)):
        with pytest.raises(ValueError):
            plain.memory_archive_preview_file(bad)
    other = App(tmp_path / 'other' / 'data', tmp_path / 'other' / 'resources', lambda *args: None)
    try:
        preview = other.memory_archive_preview_file(str(target))
        assert preview['counts']['add'] >= 1 and preview['authorization'] == 'none'
        other.memory.put('profile', 'An unrelated local change')  # The plan's decisions are unchanged.
        applied = other.memory_archive_import_file(str(target), preview['plan_digest'])['applied']
        assert applied['imported'] >= 1
        assert any(m['content'] == 'Project Orion uses SQLite' for m in other.memory.list())
        again = other.memory_archive_preview_file(str(target))
        assert again['counts']['add'] == 0 and again['counts']['skip_duplicate'] >= 1
        with pytest.raises(Exception, match='stale_preview|changed'):
            other.memory_archive_import_file(str(target), preview['plan_digest'])  # The old plan no longer holds.
    finally:
        other.close()


def test_memory_routes_are_native_only():
    names = {'memory_contests', 'memory_resolve_contest', 'memory_relations', 'memory_archive_export_to',
             'memory_archive_preview_file', 'memory_archive_import_file'}
    assert not names & {t['function']['name'] for t in TOOLS}
    allowed = re.search(r'const ALLOWED: &\[&str\] = &\[(.*?)\];', (ROOT / 'src-tauri/src/main.rs').read_text(), re.S)
    assert names <= set(re.findall(r'"([a-z_]+)"', allowed.group(1)))


# C08 provider token calibration ------------------------------------------------------------

def test_calibration_records_unmultiplied_samples_and_scales_only_after_twenty(plain):
    import threading
    from pebble.token_calibration import MIN_SAMPLES, route_fingerprint
    model = plain.model
    assert model.calibration_factor(CLOUD_A) == 1.0
    for _ in range(MIN_SAMPLES - 1):
        model.last_input_estimate, model.last_usage = 1000, {'input_tokens': 1500}
        model.calibrate(CLOUD_A, 1.0)
    assert model.calibration_factor(CLOUD_A) == 1.0  # Below the sample floor: exactly uncalibrated.
    model.last_input_estimate, model.last_usage = 1000, {'input_tokens': 1500}
    model.calibrate(CLOUD_A, 1.0)
    assert model.calibration_factor(CLOUD_A) == 1.5
    assert model.calibration_factor(CLOUD_B) == 1.0  # Per route.
    # A calibrated window count is divided back before it is stored.
    model.last_input_estimate, model.last_usage = 1500, {'input_tokens': 1500}
    model.calibrate(CLOUD_A, 1.5)
    rows = plain.store.query('SELECT estimated,reported FROM token_calibration WHERE route=? ORDER BY sequence DESC LIMIT 1',
                             (route_fingerprint(CLOUD_A),))
    assert rows == [{'estimated': 1000, 'reported': 1500}]
    # Unknown provider usage is not a sample.
    count = len(plain.store.query('SELECT 1 FROM token_calibration'))
    model.last_input_estimate, model.last_usage = 1000, {}
    assert model.calibrate(CLOUD_A, 1.5)['status'] == 'skipped'
    assert len(plain.store.query('SELECT 1 FROM token_calibration')) == count
    # A busy store never blocks the request path: the cached factor answers and the sample is skipped.
    answers = []
    with plain.store.lock:
        worker = threading.Thread(target=lambda: answers.append((model.calibration_factor(CLOUD_A),
            setattr(model, 'last_input_estimate', 1000) or setattr(model, 'last_usage', {'input_tokens': 1900})
            or model.calibrate(CLOUD_A, 1.0))))
        worker.start(); worker.join(5)
    assert not worker.is_alive() and answers == [(1.5, None)]
    assert len(plain.store.query('SELECT 1 FROM token_calibration')) == count


def test_calibrated_factor_enlarges_window_counts(plain):
    from pebble.providers import calibrated
    assert calibrated(1.0) == {} and calibrated(1.25) == {'multiplier': 1.25}
    messages = [{'role': 'user', 'content': 'x' * 4000}]
    budget = plain.model.token_budget
    try:
        base = budget.count(messages, [])
    except Exception as exc:  # The bundled tokenizer cache is required offline.
        pytest.skip(f'tokenizer unavailable: {type(exc).__name__}')
    assert budget.count(messages, [], **calibrated(1.25)) == -(-base * 125 // 100)


def test_handoff_conversation_recall_keeps_only_cleared_route_classes(app):
    # W8's h1 leak: an episodic record of a cloud-A run must not reach route B through recall.
    cid, run_id = mixed_chat(app)
    app.memory.put('episodic', 'Earlier goal on route A: ' + SECRET, chat_id=cid, origin='conversation',
                   ident='episode-' + run_id)
    app.memory.put('profile', 'I prefer concise answers.', origin='user')
    fingerprint = app.model_handoff_preview(cid, run_id, 'cloud-b')['plan_fingerprint']
    app.model_handoff_approve(cid, fingerprint)
    new = app.model_handoff_start(cid, fingerprint)['chat_id']
    recalled = [{'id': 'episode-' + run_id, 'content': 'Earlier goal'}]
    saved = [m for m in app.memory.list() if m['content'] == 'I prefer concise answers.']
    assert app.recall_for_route(new, recalled + saved) == saved  # Cloud-A record withheld; the person's record stays.
    assert app.recall_for_route(cid, recalled) == recalled  # Ordinary conversations are unchanged.
    released = app.model_handoff_preview(cid, run_id, 'cloud-b', release=['cloud:https://a.example'])['plan_fingerprint']
    app.model_handoff_approve(cid, released)
    again = app.model_handoff_start(cid, released)['chat_id']
    assert app.recall_for_route(again, recalled) == recalled  # A released class is recalled.
    app.delete_chat(new)
    assert new not in app.store.setting('model_handoff_chats', {})


# Development-worker audit: isolated copies are declared roots, judged by the session's own init cwd.

def test_worker_audit_judges_each_session_against_its_declared_checkout(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('audit_p15', ROOT / 'scripts/audit_dev_workers.py')
    audit = importlib.util.module_from_spec(spec); spec.loader.exec_module(audit)
    copy = tmp_path / 'copy'; (copy / 'backend/pebble').mkdir(parents=True)

    def stream(path, cwd, calls):
        events = [{'type': 'system', 'subtype': 'init', 'session_id': 's', 'model': 'claude-opus-5-5', 'cwd': cwd,
                   'tools': ['Read', 'Bash', 'Write'], 'mcp_servers': []}]
        events += [{'type': 'assistant', 'message': {'model': 'claude-opus-5-5', 'content': [
            {'type': 'tool_use', 'id': str(i), 'name': n, 'input': args}]}} for i, (n, args) in enumerate(calls)]
        events.append({'type': 'result', 'subtype': 'success', 'terminal_reason': 'completed', 'is_error': False})
        path.write_text('\n'.join(json.dumps(e) for e in events))
        return path
    calls = [('Write', {'file_path': str(copy / 'backend/pebble/owned.py')}),
             ('Read', {'file_path': str(copy / 'backend/pebble/app.py')}),
             ('Bash', {'command': 'cd ' + str(copy) + ' && sed -n 1,60p backend/pebble/app.py | cut -c1-80'})]
    declared = audit.audit(stream(tmp_path / 'a-stream.jsonl', str(copy), calls), {'backend/pebble/owned.py'},
                           (audit.ROOT, copy))
    assert declared['scope_violations'] == [] and declared['checkout'] == str(copy.resolve())
    assert declared['writes'] == ['backend/pebble/owned.py'] and declared['non_pytest_bash_read_only']
    undeclared = audit.audit(stream(tmp_path / 'b-stream.jsonl', str(copy), calls), {'backend/pebble/owned.py'})
    assert 'cwd:' + str(copy) in undeclared['scope_violations'] and undeclared['reads_outside_checkout_performed']
    for command in ('sed -i s/a/b/ backend/pebble/app.py', "sed -n '1w out' backend/pebble/app.py", 'sort -o x y'):
        assert 'other' in audit.classify(command, copy.resolve()), command
