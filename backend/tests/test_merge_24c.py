"""Phase 24 wave-C/D merge (m24c): the seams t24-ux-shell, t24-publish and t24-validator reach only through the parent.

- chat_rename (V1-125): the sidebar's Rename changes only the title; a conversation in Recently deleted is refused; the
  route is main-window only (main.rs ALLOWED, never the pet, not shell-only, tool_authz.NATIVE_ONLY, never a model tool);
- per-run counts in get_chat (V1-121): each settled run carries {steps, retries, waited} from its run_activity items, so a
  reopened reply reads "Worked for … · 6 steps"; a resumed run that settles again is counted again; None when unreadable;
- V1-002 (P0), the host half: the reviewed handoff with release ['device'] sends the withheld on-device turns only after
  the person approves the exact plan and starts it (previewing and approving send nothing; a start without an approval
  is refused). src/merge24c.test.mjs drives the window half (Review… on the notice, through the real ModelHandoff);
- main.rs (not compiled here): the first-run reveal, RunEvent::Reopen and the pet's narrow-only rule, statically;
- the root and app notices agree, and the candidate snapshot binds validate_phase24's extra inputs.
Scripted fakes only: no network, model, Keychain or browser.
"""

import re
import time
from pathlib import Path

import pytest

import test_model_route_privacy_24 as privacy
from pebble import data_mirror, model_handoff, tool_authz
from pebble.tool_catalogue import TOOLS
from test_merge_24b import MAIN_RS, chat, rust_list
from test_merge_24b import env, fake_home  # noqa: F401 (fixtures)
from test_model_route_privacy_24 import app  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parents[2]


# ---- chat_rename (V1-125) -------------------------------------------------------------------------------------------

def test_chat_rename_changes_only_the_title(env):
    app = env.app
    cid, other = chat(app, 'alpha'), chat(app, 'beta')
    before = app.store.query('SELECT * FROM chats WHERE id=?', (cid,))[0]
    messages = app.chat(cid)['messages']
    assert app.dispatch('chat_rename', {'id': cid, 'title': '  Trip \n to   Lisbon '}) == {'id': cid, 'title': 'Trip to Lisbon'}
    after = app.store.query('SELECT * FROM chats WHERE id=?', (cid,))[0]
    assert after == {**before, 'title': 'Trip to Lisbon'}, 'not its order (updated), project or anything else'
    assert app.chat(cid)['messages'] == messages
    assert app.chat(other)['title'] == 'Plan beta', 'another conversation is untouched'
    assert app.chat_rename(cid, 'x' * 500)['title'] == 'x' * 200
    for title in ('', '   \n', None, 5, ['name']):
        with pytest.raises(ValueError, match='Enter a name for the conversation.'):
            app.dispatch('chat_rename', {'id': cid, 'title': title})
    with pytest.raises(ValueError, match='Conversation not found.'):
        app.dispatch('chat_rename', {'id': 'f' * 32, 'title': 'Ghost'})
    assert app.chat(cid)['title'] == 'x' * 200


def test_chat_rename_refuses_a_conversation_in_recently_deleted(env):
    app = env.app
    cid = chat(app, 'alpha')
    app.dispatch('storage_trash_chat', {'chat_id': cid})
    with pytest.raises(data_mirror.TrashedConversation) as refused:
        app.dispatch('chat_rename', {'id': cid, 'title': 'Renamed'})
    assert refused.value.code == 'trashed_conversation'
    assert app.store.query('SELECT title FROM chats WHERE id=?', (cid,))[0]['title'] == 'Plan alpha'


def test_chat_rename_is_a_main_window_route_and_never_a_model_tool(env):
    assert 'chat_rename' in env.app._route_table()
    source = MAIN_RS.read_text(encoding='utf-8')
    assert 'chat_rename' in rust_list(source, r'const ALLOWED: &\[&str\] = &\[(.*?)\];')
    assert 'chat_rename' not in rust_list(source, r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains')
    assert 'chat_rename' not in rust_list(source, r'const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];')
    assert 'chat_rename' in tool_authz.NATIVE_ONLY
    assert 'chat_rename' not in {t['function']['name'].casefold() for t in TOOLS}


# ---- per-run counts in get_chat (V1-121) ----------------------------------------------------------------------------

def _run(app, cid, tools=0, retries=0):
    rid = app.runs.create(cid, 'A task.', app.runtime_config(app.chat(cid), False))
    app.runs.transition(rid, 'running')
    _work(app, rid, tools, retries)
    return rid


def _work(app, rid, tools, retries, start=0):
    for index in range(start, start + tools):
        app.runs.note(rid, 'tool_started', {'call_id': f'call-{index}'})
        app.runs.note(rid, 'tool_finished', {'call_id': f'call-{index}'})
    for attempt in range(retries):
        app.runs.note(rid, 'model_retry', {'attempt': attempt + 2, 'delay': 1.0})


def _counts(app, cid, rid):
    return next(r for r in app.dispatch('get_chat', {'id': cid})['runs'] if r['id'] == rid).get('counts', 'absent')


def test_get_chat_gives_each_settled_run_its_receipt_counts(env):
    app = env.app
    cid = chat(app, 'alpha')
    rid = _run(app, cid, tools=6, retries=1)
    # One approval the person took 12 s to decide on (the waiting is the approval row's duration).
    aid = app.store.action(cid, 'command', {'command': 'ls'})
    app.runs.note(rid, 'action_bound', {'action_id': aid, 'call_id': 'call-0'})
    app.runs.note(rid, 'approval_started', {'action_id': aid})
    bound = app.store.query("SELECT created FROM run_events WHERE run_id=? AND kind='action_bound'", (rid,))[0]['created']
    app.store.query("UPDATE run_events SET created=? WHERE run_id=? AND kind='approval_started'", (bound + 12, rid))
    assert _counts(app, cid, rid) == 'absent', 'a live run has no receipt yet'
    app.runs.transition(rid, 'completed')
    assert _counts(app, cid, rid) == {'steps': 6, 'retries': 1, 'waited': 12.0}
    # The same numbers the window's tally() counts from run_activity: tool, check and agent rows are steps.
    _, items, _ = app.activity.items(rid)
    assert sum(1 for i in items if i['kind'] in ('tool', 'check', 'child')) == 6
    assert _counts(app, cid, rid) == {'steps': 6, 'retries': 1, 'waited': 12.0}, 'kept'


def test_a_resumed_run_that_settles_again_is_counted_again_and_unreadable_counts_are_none(env, monkeypatch):
    app = env.app
    cid = chat(app, 'alpha')
    rid = _run(app, cid, tools=2)
    app.runs.transition(rid, 'failed', error='Stopped by a test.')
    assert _counts(app, cid, rid) == {'steps': 2, 'retries': 0, 'waited': 0}
    time.sleep(0.01)
    app.runs.transition(rid, 'running')  # resumed: the same run id continues
    assert _counts(app, cid, rid) == 'absent'
    _work(app, rid, 3, 0, start=2)
    time.sleep(0.01)
    app.runs.transition(rid, 'completed')
    assert _counts(app, cid, rid) == {'steps': 5, 'retries': 0, 'waited': 0}, 'not the counts cached before the resume'
    fresh = _run(app, cid, tools=1)
    app.runs.transition(fresh, 'completed')

    def unreadable(run_id):
        raise ValueError('Run not found in this conversation.')
    monkeypatch.setattr(app.activity, 'items', unreadable)
    assert _counts(app, cid, fresh) is None, 'the window then says "Took N s"'
    assert app.chat(cid)['messages'], 'the conversation still opens'


# ---- V1-002 (P0): the reviewed handoff is the only way the on-device turns reach the cloud ----------------------------

def test_the_reviewed_handoff_sends_the_released_on_device_turns_only_after_approval_and_start(app):  # noqa: F811
    cid, device_run, _ = privacy.device_history(app)
    privacy.send(app, cid, 'Summarise what we discussed.', 'luna')
    requests = app.model.http.requests
    assert privacy.leaked(requests[-1]['body']) == [], 'the send went ahead without them'
    sent = len(requests)
    device = {r['message_id'] for r in app.store.query(
        "SELECT message_id FROM message_routes WHERE chat_id=? AND route_class='device'", (cid,))}
    assert len(device) == 2
    # Review… (the window passes release ['device']): the plan lists them as released; nothing is sent.
    preview = app.dispatch('model_handoff_preview', {'id': cid, 'run_id': device_run, 'profile_id': 'luna',
                                                     'release': ['device']})
    assert preview['released_classes'] == ['device'] and not device & set(preview['withheld_ids'])
    assert preview['authorization'] == 'none' and len(requests) == sent, 'previewing sends nothing'
    plan = {'id': cid, 'plan_fingerprint': preview['plan_fingerprint']}
    with pytest.raises(model_handoff.ModelHandoffError):
        app.dispatch('model_handoff_start', plan)
    assert len(requests) == sent, 'a start without the approval sends nothing'
    assert app.dispatch('model_handoff_approve', plan)['approved'] is True
    assert len(requests) == sent, 'approving sends nothing'
    # The person starts it: only now do the on-device turn and reply reach the cloud route.
    started = app.dispatch('model_handoff_start', plan)
    assert privacy.settle(app, started['run_id']) == 'completed'
    body = requests[-1]['body']
    assert len(requests) == sent + 1
    assert privacy.CANARY['turn'].encode() in body and privacy.CANARY['reply'].encode() in body
    with pytest.raises(ValueError, match='Review the handoff again'):
        app.dispatch('model_handoff_start', plan)  # one start per approval: never replayed


def test_without_the_release_the_handoff_keeps_the_on_device_turns_out(app):  # noqa: F811
    cid, device_run, _ = privacy.device_history(app)
    privacy.send(app, cid, 'Summarise what we discussed.', 'luna')
    preview = app.dispatch('model_handoff_preview', {'id': cid, 'run_id': device_run, 'profile_id': 'luna'})
    assert preview['released_classes'] == [] and len(preview['withheld_ids']) >= 2
    plan = {'id': cid, 'plan_fingerprint': preview['plan_fingerprint']}
    app.dispatch('model_handoff_approve', plan)
    started = app.dispatch('model_handoff_start', plan)
    assert privacy.settle(app, started['run_id']) == 'completed'
    assert privacy.leaked(app.model.http.requests[-1]['body']) == []


# ---- main.rs, statically (the parent runs cargo check and the Rust unit test) ----------------------------------------

def test_the_shell_reveals_the_workspace_without_a_model_and_on_reopen_and_the_pet_only_narrows():
    source = MAIN_RS.read_text(encoding='utf-8')
    setup = source[source.index('.setup(|app| {'):source.index('.on_window_event(')]
    first_run = setup[setup.index('pebble-first-run') - 200:]
    assert 'backend.call("state"' in first_run and '"/model/configured"' in first_run
    assert re.search(r'if configured != Some\(true\) \{.*?run_on_main_thread\(move \|\| reveal\(&shown\)\)', first_run, re.S)
    assert setup.index('configure_pet(app.handle())?;') < setup.index('pebble-first-run'), 'after the pet is placed'
    assert '.tooltip("Pebble")' in setup and 'local companion' not in source
    run = source[source.index('application.run('):]
    assert re.search(r'#\[cfg\(target_os="macos"\)\]\s*tauri::RunEvent::Reopen \{ \.\. \} => reveal\(app\),', run)
    rpc = source[source.index('async fn rpc('):source.index('fn compact(')]
    line = next(l for l in rpc.splitlines() if 'Open the workspace to turn this on.' in l)
    assert 'from_pet && method=="setting" && pet_widens(&backend, &params)?' in line
    assert rpc.index('pet_widens(') < rpc.index('backend.call(&method'), 'refused before the change reaches the host'
    assert 'let from_pet = window.label()=="pet";' in rpc
    widens = source[source.index('fn widens('):source.index('fn pet_widens(')]
    assert 'else { return true };' in widens and 'None => true' in widens, 'unreadable values count as wider'
    assert 'fn the_pet_may_only_narrow()' in source and '#[cfg(test)]' in source


# ---- t24-publish and t24-validator requests ---------------------------------------------------------------------------

def _section(text, heading):
    start = text.index('## ' + heading)
    end = text.find('\n## ', start + 1)
    return text[start:end if end >= 0 else len(text)].strip()


def test_the_root_and_app_notices_agree():
    assert (ROOT / 'LICENSE').read_bytes() == (ROOT / 'src-tauri/resources/LICENSE').read_bytes()
    root = (ROOT / 'NOTICE.md').read_text(encoding='utf-8')
    app_notice = (ROOT / 'src-tauri/resources/NOTICE.md').read_text(encoding='utf-8')
    assert _section(root, 'Services') == _section(app_notice, 'Services'), 'the app names Bing and Open-Meteo too'
    components = lambda text: re.findall(r'^- \*\*([^*]+)\*\*', text, re.M)
    assert components(root) == components(app_notice) == ['AgentGuard', 'bloub', 'llama.cpp', 'CPython', 'Pillow']
    assert [h for h in re.findall(r'^## (\S+)', root, re.M)] == [h for h in re.findall(r'^## (\S+)', app_notice, re.M)]


def test_the_candidate_snapshot_binds_validate_phase24s_other_inputs():
    import importlib.util
    spec = importlib.util.spec_from_file_location('verify_m24c', ROOT / 'scripts' / 'verify_harness_candidate.py')
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    names = set(verify.sources())
    for rel in ('backend/tests/fixtures/phase24/agent_golden_requests.json', 'install.sh', 'publish/manifest.json',
                'src-tauri/resources/licenses/THIRD-PARTY-NOTICES.txt'):
        assert (rel in names) == (ROOT / rel).is_file(), rel
    assert 'install.sh' in names and 'publish/manifest.json' in names
