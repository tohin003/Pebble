"""Recovery fixtures verify effects and ownership, not just internal counters."""

import copy
import http.client
import json
import os
import ssl
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pebble.app import App
from pebble.recovery import ProviderFailure, RetryPolicy, complete_with_retry, retry_after
from pebble.runtime import RunJournal, RunLimits
from pebble.store import Store


FAST = RetryPolicy(initial_delay=0, max_delay=0)


def retry(complete, cancel=None, output=None, **kwargs):
    return complete_with_retry(complete, [{'role': 'user', 'content': 'goal'}], [],
        cancel or threading.Event(), (output if output is not None else []).append,
        False, policy=FAST, **kwargs)


def test_reconnect_discards_partial_text_and_keeps_exact_request():
    output, inputs, retries = [], [], []

    def complete(messages, tools, cancel, token, effort):
        inputs.append(copy.deepcopy(messages))
        if len(inputs) == 1:
            token('Abandoned answer and incomplete JSON')
            raise ProviderFailure('EOF', retryable=True)
        token('Committed answer')
        return {'role': 'assistant', 'content': 'Committed answer'}

    result = retry(complete, output=output, on_retry=lambda *args: retries.append(args))
    assert output == ['Committed answer'] and inputs[0] == inputs[1]
    assert result['content'] == 'Committed answer' and len(retries) == 1


@pytest.mark.parametrize('error', [
    ProviderFailure('authentication', status=401), ProviderFailure('billing', status=402),
    ValueError('malformed JSON'), ssl.SSLCertVerificationError('untrusted certificate'),
    urllib.error.URLError(ssl.SSLCertVerificationError('untrusted certificate')),
])
def test_permanent_failure_is_not_retried(error):
    called = []

    def complete(*args):
        called.append(True)
        raise error

    with pytest.raises(type(error)):
        retry(complete)
    assert len(called) == 1


def test_attempts_are_bounded_for_broken_socket():
    called = []

    def complete(*args):
        called.append(True)
        raise http.client.IncompleteRead(b'partial')

    with pytest.raises(http.client.IncompleteRead):
        retry(complete)
    assert len(called) == 3


def test_cancel_during_backoff_prevents_another_request():
    cancel, called = threading.Event(), []

    def complete(*args):
        called.append(True)
        raise ProviderFailure('overloaded', retryable=True)

    with pytest.raises(InterruptedError):
        retry(complete, cancel=cancel, on_retry=lambda *args: cancel.set())
    assert len(called) == 1


def test_long_retry_after_is_respected_without_early_retry():
    called = []

    def complete(*args):
        called.append(True)
        raise ProviderFailure('rate limit', retryable=True, status=429, retry_after=120)

    with pytest.raises(ProviderFailure):
        retry(complete)
    assert len(called) == 1
    assert retry_after('nan') is None and retry_after('5') == 5


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield a
    a.close()


def diagram_call(ident='view-1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': 'UIRender', 'arguments': json.dumps({
            'title': 'Flow', 'kind': 'diagram', 'data': json.dumps({'nodes': [{'id': 'a', 'label': 'A'}], 'edges': []})})}}]}


def final(token, content='Done with committed evidence.'):
    token(content)
    return {'role': 'assistant', 'content': content}


def test_model_reconnect_does_not_repeat_completed_artifact(app):
    chat, requests = app.new_chat(), []

    def complete(messages, tools, cancel, token, effort):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return diagram_call()
        if len(requests) == 2:
            token('This abandoned text must disappear')
            raise ProviderFailure('lost connection', retryable=True)
        return final(token)

    app.model.complete = complete
    app.work(chat, 'Draw a diagram', False, [])
    result = app.chat(chat['id'])
    assert len(result['artifacts']) == 1 and len(requests) == 3
    assert 'abandoned' not in result['messages'][-1]['content']
    run = app.runs.get(result['runs'][0]['id'])
    assert run['state'] == 'completed' and len(app.runs.tools(run['id'])) == 1
    assert result['messages'][-1]['usage']['usage_incomplete'] is True
    assert result['messages'][-1]['usage']['input_tokens'] is None
    events = app.runs.events(run['id'])
    assert [e['sequence'] for e in events] == list(range(1, len(events) + 1))
    assert any(e['payload'].get('to') == 'retry_wait' for e in events)
    assert any(e['kind'] == 'response_committed' and e['payload']['response'].get('tool_calls') for e in events)


def test_pause_reopens_committed_context_without_replaying_tools(app):
    chat, requests = app.new_chat(), []

    def complete(messages, tools, cancel, token, effort):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return diagram_call()
        app.pause_requested.set()
        raise InterruptedError('paused')

    app.model.complete = complete
    app.work(chat, 'Draw a diagram', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'paused'
    captured = []

    def resumed(messages, tools, cancel, token, effort):
        captured.extend(messages)
        return final(token)

    app.model.complete = resumed
    app.run_resume(chat['id'], rid)
    app.worker.join(5)
    assert not app.worker.is_alive()
    assert app.runs.get(rid)['state'] == 'completed'
    assert len(app.artifacts.list(chat['id'])) == 1
    assert any(m['role'] == 'tool' and m['tool_call_id'] == 'view-1' for m in captured)


def test_unacknowledged_effect_blocks_resume_and_requires_user_report(app):
    chat, requests = app.new_chat(), []
    original = app.runs.end_tool

    def lose_commit(*args, **kwargs):
        raise OSError('simulated disk write failure after artifact effect')

    def complete(messages, tools, cancel, token, effort):
        requests.append(True)
        return diagram_call() if len(requests) == 1 else final(token)

    app.runs.end_tool, app.model.complete = lose_commit, complete
    app.work(chat, 'Draw a diagram', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'needs_reconciliation'
    assert len(app.artifacts.list(chat['id'])) == 1
    with pytest.raises(ValueError, match='reconcile'):
        app.run_resume(chat['id'], rid)
    app.runs.end_tool = original
    result = app.run_reconcile(chat['id'], rid, '1:view-1', 'occurred', 'I checked the saved view.')
    assert result['verified'] is False
    assert app.runs.get(rid)['state'] == 'paused'
    snapshot = app.runs.resume_snapshot(rid)
    assert any('HOST RECONCILIATION' in str(m.get('content')) for m in snapshot['messages'])


def test_uncertain_first_effect_stops_sibling_calls_and_next_model_attempt(app):
    chat, attempts = app.new_chat(), []
    render = app.artifacts.render

    def lost_ack(*args):
        render(*args)
        raise OSError('effect occurred but acknowledgement was lost')

    def complete(*args):
        attempts.append(True)
        response = diagram_call('first')
        response['tool_calls'] += diagram_call('second')['tool_calls']
        return response

    app.artifacts.render, app.model.complete = lost_ack, complete
    app.work(chat, 'Draw two diagrams', False, [])
    run = app.runs.list(chat['id'])[0]
    assert run['state'] == 'needs_reconciliation'
    assert len(attempts) == 1 and len(app.artifacts.list(chat['id'])) == 1
    assert [t['state'] for t in app.runs.tools(run['id'])] == ['uncertain', 'planned']


def test_deterministic_host_effect_is_cached_when_finish_failed(app, monkeypatch):
    chat, effects = app.new_chat(), []
    monkeypatch.setattr('pebble.app.installed_apps', lambda: [{'id': 'fixture', 'name': 'Fixture'}])

    def execute(name, args, ws, cid):
        assert name == 'OpenApp'
        effects.append(True)
        return {'status': 'applied', 'result': {'opened': 'Fixture'}}

    finish = app.finish
    app._execute = execute
    app.finish = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('lost finish'))
    app.work(chat, 'Open Fixture', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'failed' and len(effects) == 1
    app.finish = finish
    app.run_resume(chat['id'], rid)
    app.worker.join(5)
    assert app.runs.get(rid)['state'] == 'completed' and len(effects) == 1
    assert app.runs.tools(rid)[0]['call_id'] == 'host:open-app'


def test_failed_terminal_journal_commit_still_releases_process_owner(app):
    chat = app.new_chat()
    app.model.complete = lambda m, t, c, token, e: final(token)
    transition = app.runs.transition

    def unavailable(rid, state, **kwargs):
        if state == 'completed':
            raise OSError('simulated terminal disk failure')
        return transition(rid, state, **kwargs)

    app.runs.transition = unavailable
    with pytest.raises(OSError, match='disk failure'):
        app.work(chat, 'Explain a stack', False, [])
    assert app.active is None and app.active_run is None and app.task_policy is None
    assert app.runs.list(chat['id'])[0]['state'] == 'running'


def test_automatic_episode_does_not_attribute_prior_run_evidence(app):
    chat = app.new_chat()
    aid = app.store.action(chat['id'], 'tests', {'result': {'exit_code': 0}})
    app.store.query("UPDATE actions SET status='applied' WHERE id=?", (aid,))
    app.model.complete = lambda m, t, c, token, e: final(token)
    app.work(chat, 'Explain a stack', False, [])
    episodes = [json.loads(m['content']) for m in app.memory.list('episodic') if m['chat_id'] == chat['id']]
    assert episodes[-1]['task_outcome'] == 'completed'
    assert episodes[-1]['broker_evidence'] == []
    assert episodes[-1]['run_id'] == app.runs.list(chat['id'])[0]['id']


def test_changed_permissions_cannot_resume_an_old_grant(app):
    chat = app.new_chat()
    app.model.complete = lambda *args: (_ for _ in ()).throw(ProviderFailure('auth', status=401))
    app.work(chat, 'Explain a stack', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    policy = app.store.setting('permissions')
    policy['mode'] = 'full'
    app.setting('permissions', policy)
    with pytest.raises(ValueError, match='permissions'):
        app.run_resume(chat['id'], rid)


def test_completed_run_cannot_be_resumed_or_taken_from_another_chat(app):
    chat = app.new_chat()
    app.model.complete = lambda m, t, c, token, e: final(token)
    app.work(chat, 'Explain a stack', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    with pytest.raises(ValueError):
        app.run_resume(chat['id'], rid)
    other = app.new_chat()
    with pytest.raises(ValueError, match='conversation'):
        app.run_get(other['id'], rid)


def test_step_budget_never_claims_completion_and_can_be_extended(app):
    chat, calls = app.new_chat(), []
    config = app.runtime_config(chat, False, {'max_steps': 1, 'max_seconds': 20})
    rid = app.runs.create(chat['id'], 'Draw a diagram', config)
    app.model.complete = lambda *args: diagram_call()
    app.work(chat, 'Draw a diagram', False, [], run_id=rid)
    assert app.runs.get(rid)['state'] == 'budget_exhausted'
    app.model.complete = lambda m, t, c, token, e: (calls.append(m), final(token))[1]
    app.run_resume(chat['id'], rid, limits={'max_steps': 2, 'max_seconds': 20})
    app.worker.join(5)
    assert app.runs.get(rid)['state'] == 'completed' and len(app.artifacts.list(chat['id'])) == 1


def test_deadline_cancels_silent_model_and_records_budget_exhaustion(app):
    chat = app.new_chat()
    config = app.runtime_config(chat, False, {'max_steps': 5, 'max_seconds': 1})
    rid = app.runs.create(chat['id'], 'Explain a stack', config)

    def silent(messages, tools, cancel, token, effort):
        assert cancel.wait(3)
        raise InterruptedError('deadline')

    app.model.complete = silent
    app.work(chat, 'Explain a stack', False, [], run_id=rid)
    assert app.runs.get(rid)['state'] == 'budget_exhausted'


def test_commands_are_host_parsed_and_unknown_commands_do_not_reach_model(app):
    cid = app.new_chat()['id']
    app.model.complete = lambda *args: pytest.fail('A status command must not run inference')
    app.send(cid, '/help')
    assert '/resume' in app.chat(cid)['messages'][-1]['content']
    app.send(cid, '/status')
    assert not app.runs.list(cid)
    with pytest.raises(ValueError, match='Unknown command'):
        app.send(cid, '/invent-tool')


def test_operational_recovery_remains_when_optional_memory_is_off_and_deletion_cascades(app):
    app.setting('memory_enabled', False)
    chat = app.new_chat()
    app.model.complete = lambda m, t, c, token, e: final(token)
    app.work(chat, 'Explain a stack', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['checkpoint']['messages']
    app.delete_chat(chat['id'])
    assert not app.runs.list(chat['id']) and not app.runs.events(rid) and not app.runs.tools(rid)


def test_duplicate_provider_call_ids_execute_nothing(app):
    chat = app.new_chat()
    response = diagram_call()
    response['tool_calls'] *= 2
    app.model.complete = lambda *args: response
    app.work(chat, 'Draw a diagram', False, [])
    assert app.runs.list(chat['id'])[0]['state'] == 'failed'
    assert not app.artifacts.list(chat['id'])


def test_provider_call_id_reuse_on_a_later_step_has_distinct_host_identity(app):
    chat, calls = app.new_chat(), []

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        return diagram_call('provider-reuses-id') if len(calls) <= 2 else final(token)

    app.model.complete = complete
    app.work(chat, 'Draw two diagrams', False, [])
    run = app.runs.list(chat['id'])[0]
    assert run['state'] == 'completed'
    assert len(app.artifacts.list(chat['id'])) == 2
    assert {t['call_id'] for t in app.runs.tools(run['id'])} == {'1:provider-reuses-id', '2:provider-reuses-id'}


@pytest.mark.parametrize('uncertain', [False, True])
def test_real_process_exit_recovers_committed_results_and_refuses_uncertain_effects(tmp_path, uncertain):
    data = tmp_path / 'data'
    script = """
import json,os,sys
from pathlib import Path
from pebble.store import Store
from pebble.runtime import RunJournal
s=Store(Path(sys.argv[1])); j=RunJournal(s,Path(sys.argv[1]))
cid=s.new_chat(); rid=j.create(cid,'original goal',{'limits':{'max_steps':64,'max_seconds':900}})
j.transition(rid,'running')
response={'role':'assistant','content':None,'tool_calls':[{'id':'once','function':{'name':'Write','arguments':'{"file_path":"file.txt","content":"x"}'}},{'id':'unstarted','function':{'name':'Read','arguments':'{"file_path":"file.txt"}'}}]}
j.response(rid,response,{'phase':'tools','messages':[{'role':'user','content':'original goal'},response]},steps=1,elapsed=0.1)
j.begin_tool(rid,'1:once','Write',{'file_path':'file.txt','content':'x'})
if sys.argv[2]=='False':j.end_tool(rid,'1:once',{'status':'applied','action_id':'receipt'})
os._exit(42)
"""
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1])}
    process = subprocess.run([sys.executable, '-c', script, str(data), str(uncertain)], env=env, capture_output=True)
    assert process.returncode == 42, process.stderr.decode()
    store, journal = Store(data), None
    try:
        journal = RunJournal(store, data)
        rid = journal.list()[0]['id']
        if uncertain:
            assert journal.get(rid)['state'] == 'needs_reconciliation'
            with pytest.raises(ValueError):
                journal.resume_snapshot(rid)
        else:
            assert journal.get(rid)['state'] == 'interrupted'
            snapshot = journal.resume_snapshot(rid)
            results = [json.loads(m['content']) for m in snapshot['messages'] if m['role'] == 'tool']
            assert results[0]['action_id'] == 'receipt' and results[1]['status'] == 'not_executed'
    finally:
        if journal:
            journal.close()
        store.close()


def test_second_service_cannot_take_over_a_live_owner(tmp_path):
    store = Store(tmp_path)
    journal = RunJournal(store, tmp_path)
    other = Store(tmp_path)
    try:
        with pytest.raises(ValueError, match='Another Pebble'):
            RunJournal(other, tmp_path)
        assert not journal.lease.closed
    finally:
        other.close()
        journal.close()
        store.close()


@pytest.mark.parametrize('mode,expected', [('429', 2), ('partial', 2), ('401', 1), ('malformed', 1)])
def test_actual_http_stream_and_status_recovery(app, mode, expected):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            first = len(calls) == 1
            if first and mode in {'429', '401'}:
                self.send_response(int(mode))
                self.send_header('Content-Type', 'application/json')
                self.send_header('Retry-After', '0')
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"fixture failure"}}')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            if first and mode == 'malformed':
                self.wfile.write(b'data: {broken JSON}\n\n')
                return
            content = 'discard this partial answer' if first and mode == 'partial' else 'Valid complete answer'
            self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': {'content': content}}]}).encode() + b'\n\n')
            if not (first and mode == 'partial'):
                self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        app.model_save(name='Recovery fixture', url=f'http://127.0.0.1:{server.server_port}', model='fixture')
        chat = app.new_chat()
        app.work(chat, 'Explain a stack', False, [])
        assert len(calls) == expected
        run = app.runs.list(chat['id'])[0]
        assert run['state'] == ('completed' if expected == 2 else 'failed')
        assert not app.model.response
        messages = app.chat(chat['id'])['messages']
        assert all('discard this partial' not in m['content'] for m in messages)
        if expected == 2:
            assert calls[0] == calls[1]
            assert messages[-1]['content'] == 'Valid complete answer'
    finally:
        server.shutdown()
        server.server_close()


def staged_edit_run(app, tmp_path):
    from pebble.policy import Workspace
    root = tmp_path / 'project'
    root.mkdir()
    chat = app.new_chat(str(root))
    rid = app.runs.create(chat['id'], 'Write a file', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    proposal = app.propose(chat['id'], 'edit', Workspace(root).draft('new.txt', 'exact content'))
    app.runs.begin_tool(rid, 'edit-once', 'Write', {'file_path': 'new.txt', 'content': 'exact content'})
    app.runs.end_tool(rid, 'edit-once', proposal)
    app.runs.checkpoint(rid, {'phase': 'model', 'messages': [{'role': 'user', 'content': 'Write a file'}]}, steps=1, elapsed=0)
    app.runs.transition(rid, 'waiting_approval')
    return root, chat, rid, proposal['action_id']


def test_approved_effect_receipt_is_linked_to_original_run(app, tmp_path):
    root, chat, rid, aid = staged_edit_run(app, tmp_path)
    assert not (root / 'new.txt').exists()
    app.approve(chat['id'], aid)
    assert (root / 'new.txt').read_text() == 'exact content'
    tool = app.runs.tools(rid)[0]
    assert tool['state'] == 'completed' and tool['result']['status'] == 'applied'
    assert tool['result']['action_id'] == aid
    snapshot = app.runs.resume_snapshot(rid)
    assert any('HOST ACTION UPDATES' in m['content'] and 'applied' in m['content'] for m in snapshot['messages'])


def test_failed_acknowledgement_after_approved_write_never_replays(app, tmp_path):
    root, chat, rid, aid = staged_edit_run(app, tmp_path)
    actual = app.perform_action

    def lose_reply(action, cid):
        actual(action, cid)
        raise OSError('connection failed after the effect')

    app.perform_action = lose_reply
    with pytest.raises(OSError):
        app.approve(chat['id'], aid)
    assert (root / 'new.txt').read_text() == 'exact content'
    assert app.runs.get(rid)['state'] == 'needs_reconciliation'
    with pytest.raises(ValueError):
        app.run_resume(chat['id'], rid)


def test_service_restart_retains_model_tool_context_without_repeating_artifact(tmp_path):
    home, resources = tmp_path / 'data', tmp_path / 'resources'
    first = App(home, resources, lambda *args: None)
    requests = []
    try:
        chat = first.new_chat()

        def complete(*args):
            requests.append(True)
            if len(requests) == 1:
                return diagram_call()
            raise ProviderFailure('permanent fixture outage', status=401)

        first.model.complete = complete
        first.work(chat, 'Draw a diagram', False, [])
        rid = first.runs.list(chat['id'])[0]['id']
    finally:
        first.close()
    second = App(home, resources, lambda *args: None)
    try:
        seen = []

        def restored(messages, tools, cancel, token, effort):
            seen.extend(messages)
            return final(token)

        second.model.complete = restored
        second.run_resume(chat['id'], rid)
        second.worker.join(5)
        assert second.runs.get(rid)['state'] == 'completed'
        assert len(second.artifacts.list(chat['id'])) == 1
        assert any(m['role'] == 'tool' and m['tool_call_id'] == 'view-1' for m in seen)
    finally:
        second.close()


@pytest.mark.parametrize('limits', [{'max_steps': True}, {'max_seconds': float('inf')}, {'max_steps': 257}, {'unknown': 1}])
def test_owner_limits_are_validated(limits):
    with pytest.raises(ValueError):
        RunLimits.parse(limits)


def test_a_resume_after_a_stop_finishes_a_shown_reply_without_asking_again(app, monkeypatch):
    """Phase 32: the run stopped after its final reply was committed and shown, before it was marked complete (the
    bench's recovery drill under load). Resuming it asked the model to continue, and the scripted reply came twice."""
    chat, requests, checks = app.new_chat(), [], []

    def complete(messages, tools, cancel, token, effort):
        requests.append(copy.deepcopy(messages))
        return final(token, 'Done.\nFINAL: done')
    app.model.complete = complete
    real = app.controller.completion

    def completion(run_id, ws, reply):
        checks.append(reply)
        if len(checks) == 1:  # the app is quitting while the completion gate runs
            app.pause_requested.set()
            raise InterruptedError('paused')
        return real(run_id, ws, reply)
    monkeypatch.setattr(app.controller, 'completion', completion)
    app.work(chat, 'Say done', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'paused' and len(requests) == 1
    app.run_resume(chat['id'], rid)
    app.worker.join(5)
    assert not app.worker.is_alive() and app.runs.get(rid)['state'] == 'completed'
    assert len(requests) == 1  # no new model request
    replies = [m['content'] for m in app.chat(chat['id'])['messages'] if m['role'] == 'assistant']
    assert replies[-1].count('FINAL: done') == 1 and replies[-1].startswith('Done.')
    assert checks[-1].count('FINAL: done') == 1  # the gate judged the reply once, as shown


def test_a_resume_after_a_stop_mid_task_still_asks_the_model(app):
    """The finished-reply path needs a shown turn-end as the checkpoint's last message; after a committed tool result
    the model continues as before."""
    chat, requests = app.new_chat(), []

    def complete(messages, tools, cancel, token, effort):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return diagram_call()
        if len(requests) == 2:
            app.pause_requested.set()
            raise InterruptedError('paused')
        return final(token)
    app.model.complete = complete
    app.work(chat, 'Draw a diagram', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'paused'
    app.run_resume(chat['id'], rid)
    app.worker.join(5)
    assert app.runs.get(rid)['state'] == 'completed' and len(requests) == 3
    assert 'HOST RESUME' in requests[-1][-1]['content']
