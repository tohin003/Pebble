"""Interactive runs must retain context, origin and ownership across waits."""

import copy
import json
import threading
import time

import pytest

from pebble.app import App
from pebble.interaction import question_items
from pebble.providers import Providers


ITEMS = [{'id': 'scope', 'title': 'Which scope should I use?', 'options': [
    {'id': 'project', 'label': 'This project'}, {'id': 'all', 'label': 'All projects'}]}]


@pytest.fixture
def app(tmp_path):
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield value
    value.close()


def call(name, args, ident='request'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def ask(required=True, ident='question'):
    return call('AskUserQuestion', {'questions': json.dumps(ITEMS), 'required': required}, ident)


def view(ident='view'):
    return call('UIRender', {'title': 'Fixture', 'kind': 'diagram', 'data': json.dumps({
        'nodes': [{'id': 'a', 'label': 'A'}], 'edges': []})}, ident)


def final(token):
    token('Finished using the committed input.')
    return {'role': 'assistant', 'content': 'Finished using the committed input.'}


def settled(app, rid, state='completed'):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = app.runs.get(rid)
        if run['state'] == state and app.active is None:
            return run
        time.sleep(.01)
    pytest.fail(f'Run did not settle as {state}: {app.runs.get(rid)["state"]}')


def requested(app, required=True):
    chat, attempts = app.new_chat(), []

    def complete(messages, tools, cancel, token, effort):
        attempts.append(copy.deepcopy(messages))
        return ask(required) if len(attempts) == 1 else final(token)

    app.model.complete = complete
    app.work(chat, 'Draw a diagram after clarifying the scope', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    question = app.interactions.questions(chat['id'])[0]
    return chat, rid, question, attempts


def test_required_question_suspends_and_human_answer_resumes_same_run(app):
    chat, rid, question, attempts = requested(app)
    assert app.runs.get(rid)['state'] == 'waiting_input' and len(attempts) == 1
    app.question_answer(chat['id'], question['id'], 1, {'scope': {'option_id': 'project'}})
    settled(app, rid)
    assert len(attempts) == 2 and len(app.runs.list(chat['id'])) == 1
    assert any('human-origin' in str(m.get('content')) and 'question_answer' in str(m.get('content')) and 'This project' in str(m.get('content')) for m in attempts[-1])
    assert not app.interactions.questions(chat['id'])
    assert app.runs.tools(rid)[0]['result']['receipt_id'] == '1:question'


def test_required_question_does_not_execute_sibling_effect(app):
    chat = app.new_chat()
    response = ask()
    response['tool_calls'] += view()['tool_calls']
    app.model.complete = lambda *args: response
    app.work(chat, 'Draw a diagram after clarifying the scope', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'waiting_input'
    assert not app.artifacts.list(chat['id'])
    assert {t['name']: t['state'] for t in app.runs.tools(rid)} == {'AskUserQuestion': 'completed', 'UIRender': 'planned'}


@pytest.mark.parametrize('cancel', [False, True])
def test_question_resolved_before_worker_wait_is_not_lost(app, cancel):
    attempts, chat = [], app.new_chat()

    def event(name, data):
        if name == 'question' and data['status'] == 'pending':
            if cancel:
                app.question_cancel(chat['id'], data['question_id'], 1)
            else:
                app.question_answer(chat['id'], data['question_id'], 1, {'scope': {'text': 'current project'}})

    def complete(m, t, c, token, e):
        attempts.append(True)
        return ask() if len(attempts) == 1 else final(token)

    app.emit, app.model.complete = event, complete
    app.work(chat, 'Draw a diagram after clarifying the scope', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(app, rid, 'stopped' if cancel else 'completed')
    assert len(attempts) == (1 if cancel else 2)


def test_optional_question_allows_sibling_work_and_later_answer(app):
    chat, attempts = app.new_chat(), []

    def complete(messages, tools, cancel, token, effort):
        attempts.append(copy.deepcopy(messages))
        if len(attempts) == 1:
            response = ask(False)
            response['tool_calls'] += view()['tool_calls']
            return response
        return final(token)

    app.model.complete = complete
    app.work(chat, 'Draw a diagram with an optional choice', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    question = app.interactions.questions(chat['id'])[0]
    assert len(app.artifacts.list(chat['id'])) == 1 and len(attempts) == 2
    assert app.runs.get(rid)['state'] == 'waiting_input'
    app.question_answer(chat['id'], question['id'], 1, {'scope': {'text': 'Only the current project'}})
    settled(app, rid)
    assert len(attempts) == 3 and len(app.artifacts.list(chat['id'])) == 1


@pytest.mark.parametrize('bad', [
    {'scope': {'option_id': 'forged'}}, {'scope': {'text': ''}},
    {'scope': {'text': 'x', 'origin': 'human'}}, {'other': {'text': 'x'}},
    {'scope': {'text': 'sk-' + 'x' * 40}},
])
def test_invalid_or_sensitive_answers_are_not_persisted(app, bad):
    chat, rid, question, attempts = requested(app)
    with pytest.raises(ValueError):
        app.question_answer(chat['id'], question['id'], 1, bad)
    assert app.interactions.question(question['id'])['answer'] is None
    assert len(attempts) == 1 and app.runs.get(rid)['state'] == 'waiting_input'


def test_other_chat_and_stale_revision_cannot_answer(app):
    chat, _, question, _ = requested(app)
    other = app.new_chat()
    with pytest.raises(ValueError, match='conversation'):
        app.question_answer(other['id'], question['id'], 1, {'scope': {'text': 'x'}})
    with pytest.raises(ValueError, match='stale'):
        app.question_answer(chat['id'], question['id'], 2, {'scope': {'text': 'x'}})


def test_chat_plan_tracks_active_or_pending_question_owner(app):
    chat, old, _, _ = requested(app)
    app.runs.transition(old, 'running')
    app.interactions.update_plan(old, [{'id': 'old', 'title': 'Clarify older request', 'status': 'pending'}])
    app.runs.transition(old, 'waiting_input')
    new = app.runs.create(chat['id'], 'Newer task', app.runtime_config(chat, False))
    app.runs.transition(new, 'running')
    app.interactions.update_plan(new, [{'id': 'new', 'title': 'Newer request', 'status': 'pending'}])
    assert app.chat(chat['id'])['plan']['run_id'] == new
    app.runs.transition(new, 'completed')
    snapshot = app.chat(chat['id'])
    assert snapshot['runs'][0]['id'] == new
    assert snapshot['plan']['run_id'] == old
    assert snapshot['questions'][0]['run_id'] == old


def test_duplicate_answer_is_idempotent_but_conflicting_replay_is_refused(app):
    chat, rid, question, attempts = requested(app)
    answer = {'scope': {'text': 'current project'}}
    app.question_answer(chat['id'], question['id'], 1, answer)
    settled(app, rid)
    app.question_answer(chat['id'], question['id'], 1, answer)
    assert len(attempts) == 2
    assert len(app.store.query('SELECT * FROM run_inbox WHERE run_id=?', (rid,))) == 1
    with pytest.raises(ValueError):
        app.question_answer(chat['id'], question['id'], 1, {'scope': {'text': 'different'}})


def test_required_cancel_stops_and_late_answer_does_not_resume(app):
    chat, rid, question, attempts = requested(app)
    app.question_cancel(chat['id'], question['id'], 1)
    assert app.runs.get(rid)['state'] == 'stopped'
    with pytest.raises(ValueError):
        app.question_answer(chat['id'], question['id'], 1, {'scope': {'text': 'x'}})
    assert len(attempts) == 1


def test_optional_skip_is_committed_context_and_continues(app):
    chat, rid, question, attempts = requested(app, required=False)
    app.question_cancel(chat['id'], question['id'], 1)
    settled(app, rid)
    assert len(attempts) == 3
    assert any('question_cancelled' in str(m.get('content')) for m in attempts[-1])


def test_answer_to_paused_run_does_not_automatically_resume(app):
    chat, rid, question, attempts = requested(app)
    app.run_pause(chat['id'], rid)
    app.question_answer(chat['id'], question['id'], 1, {'scope': {'text': 'current project'}})
    assert app.runs.get(rid)['state'] == 'paused' and len(attempts) == 1
    app.run_resume(chat['id'], rid)
    settled(app, rid)


def test_explicit_resume_reopens_stopped_question_without_inference(app):
    chat, rid, question, attempts = requested(app)
    app.stop(chat['id'], rid)
    with pytest.raises(ValueError):
        app.question_answer(chat['id'], question['id'], 1, {'scope': {'text': 'x'}})
    assert app.run_resume(chat['id'], rid)['status'] == 'waiting_input'
    assert len(attempts) == 1
    app.question_answer(chat['id'], question['id'], 1, {'scope': {'text': 'current project'}})
    settled(app, rid)


def test_pending_question_and_plan_restore_after_service_restart(tmp_path):
    home, resources = tmp_path / 'data', tmp_path / 'resources'
    first = App(home, resources, lambda *args: None)
    try:
        chat, rid, question, _ = requested(first)
    finally:
        first.close()
    second = App(home, resources, lambda *args: None)
    try:
        second.model.complete = lambda m, t, c, token, e: final(token)
        assert second.chat(chat['id'])['questions'][0]['id'] == question['id']
        second.question_answer(chat['id'], question['id'], 1, {'scope': {'text': 'current project'}})
        settled(second, rid)
        assert len(second.runs.tools(rid)) == 1
    finally:
        second.close()


def test_steering_during_generation_is_delivered_at_next_boundary(app):
    chat, attempts = app.new_chat(), []
    entered, release = threading.Event(), threading.Event()

    def complete(messages, tools, cancel, token, effort):
        attempts.append(copy.deepcopy(messages))
        if len(attempts) == 1:
            entered.set()
            assert release.wait(3)
            return view()
        return final(token)

    app.model.complete = complete
    result = app.send(chat['id'], 'Draw a diagram')
    assert entered.wait(3)
    followup = app.send(chat['id'], 'Use project scope only')
    assert followup['steering'] and followup['run_id'] == result['run_id']
    release.set()
    settled(app, result['run_id'])
    assert len(attempts) == 2 and any('Use project scope only' in str(m.get('content')) and m.get('_pebble_inbox') for m in attempts[-1])


def test_followup_at_completion_is_not_lost(app):
    chat, attempts = app.new_chat(), []
    entered, release = threading.Event(), threading.Event()

    def complete(messages, tools, cancel, token, effort):
        attempts.append(copy.deepcopy(messages))
        if len(attempts) == 1:
            entered.set()
            assert release.wait(3)
        return final(token)

    app.model.complete = complete
    result = app.send(chat['id'], 'Explain a diagram')
    assert entered.wait(3)
    app.run_steer(chat['id'], 'Also explain the edges', result['run_id'], 'unique-input')
    app.run_steer(chat['id'], 'Also explain the edges', result['run_id'], 'unique-input')
    with pytest.raises(ValueError):
        app.run_steer(chat['id'], 'changed body', result['run_id'], 'unique-input')
    release.set()
    settled(app, result['run_id'])
    assert len(attempts) == 2
    assert sum(bool(m.get('_pebble_inbox')) for m in attempts[-1]) == 1


def test_answer_dispatch_revalidates_permissions_before_inference(app):
    chat, rid, question, attempts = requested(app)
    policy = app.store.setting('permissions')
    policy['tools']['web'] = False
    app.setting('permissions', policy)
    app.question_answer(chat['id'], question['id'], 1, {'scope': {'text': 'current project'}})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        continuation = app.interactions.continuation(rid)
        if continuation and continuation['state'] == 'blocked':
            break
        time.sleep(.01)
    assert app.interactions.continuation(rid)['state'] == 'blocked' and len(attempts) == 1


def test_plan_requires_run_scoped_resolved_receipts(app):
    chat, responses = app.new_chat(), []
    plan = [{'id': 'draw', 'title': 'Draw a diagram', 'status': 'in_progress'}]

    def complete(messages, tools, cancel, token, effort):
        responses.append(True)
        if len(responses) == 1:
            return call('UpdatePlan', {'steps': json.dumps(plan)})
        if len(responses) == 2:
            return view()
        if len(responses) == 3:
            return call('UpdatePlan', {'steps': json.dumps([{**plan[0], 'status': 'completed', 'evidence_ids': ['2:view']}])})
        return final(token)

    app.model.complete = complete
    app.work(chat, 'Draw a diagram', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'completed'
    assert app.interactions.plan(rid)['revision'] == 2
    assert app.chat(chat['id'])['plan']['steps'][0]['evidence_ids'] == ['2:view']
    other = app.new_chat()
    other_rid = app.runs.create(other['id'], 'Other task', app.runtime_config(other, False))
    app.runs.transition(other_rid, 'running')
    with pytest.raises(ValueError, match='this run'):
        app.interactions.update_plan(other_rid, [{**plan[0], 'status': 'completed', 'evidence_ids': ['2:view']}])
    app.runs.transition(other_rid, 'stopped')


@pytest.mark.parametrize('always_block', [False, True])
def test_guard_completion_repairs_are_bounded_and_run_scoped(app, tmp_path, always_block):
    project = tmp_path / 'project'
    project.mkdir()
    chat, attempts, stops = app.new_chat(str(project)), [], []

    def guard(event, root, ident, **kwargs):
        if event == 'stop':
            stops.append(ident)
            return {'action': 'block', 'reason': 'Missing fixture evidence'} if always_block or len(stops) < 3 else {'action': 'allow'}
        return {'action': 'allow'}

    app.guard.handle = guard
    app.model.complete = lambda m, t, c, token, e: (attempts.append(copy.deepcopy(m)) or final(token))
    app.work(chat, 'Explain this project', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert len(attempts) == 3 and stops == [rid] * 3
    assert app.runs.get(rid)['state'] == ('verification_required' if always_block else 'completed')


def test_approval_continues_original_run_with_applied_receipt(app, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    chat, attempts = app.new_chat(str(project)), []
    app.guard.handle = lambda *args, **kwargs: {'action': 'allow'}

    def complete(messages, tools, cancel, token, effort):
        attempts.append(copy.deepcopy(messages))
        if len(attempts) == 1:
            return call('Write', {'file_path': 'new.txt', 'content': 'one exact effect'}, 'write')
        return final(token)

    app.model.complete = complete
    app.work(chat, 'Create a file', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'waiting_approval'
    action = app.store.actions(chat['id'])[0]
    app.approve(chat['id'], action['id'])
    settled(app, rid)
    assert (project / 'new.txt').read_text() == 'one exact effect'
    assert len(attempts) == 3 and len(app.runs.tools(rid)) == 1
    assert any('action_resolution' in str(m.get('content')) and 'applied' in str(m.get('content')) for m in attempts[-1])


def test_chat_deletion_cascades_questions_inbox_plans_and_jobs(app):
    chat, rid, question, _ = requested(app)
    app.question_cancel(chat['id'], question['id'], 1)
    app.delete_chat(chat['id'])
    for table in ('run_questions', 'run_inbox', 'run_plans', 'run_continuations'):
        assert not app.store.query(f'SELECT * FROM {table} WHERE run_id=?', (rid,))


def test_approval_followup_storage_failure_preserves_applied_effect(app, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    chat, attempts, effects = app.new_chat(str(project)), [], []
    app.guard.handle = lambda *args, **kwargs: {'action': 'allow'}
    actual = app.perform_action

    def perform(action, cid):
        effects.append(action['id'])
        return actual(action, cid)

    def complete(messages, tools, cancel, token, effort):
        attempts.append(copy.deepcopy(messages))
        return call('Write', {'file_path': 'once.txt', 'content': 'committed'}, 'write') if len(attempts) == 1 else final(token)

    app.model.complete, app.perform_action = complete, perform
    app.work(chat, 'Create a file', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    aid = app.store.actions(chat['id'])[0]['id']
    app.store.query("CREATE TRIGGER fail_followup BEFORE INSERT ON run_inbox BEGIN SELECT RAISE(ABORT,'fixture followup failure'); END")
    with pytest.raises(Exception, match='fixture followup failure'):
        app.approve(chat['id'], aid)
    assert (project / 'once.txt').read_text() == 'committed'
    assert app.store.actions(chat['id'])[0]['status'] == 'applied'
    assert app.runs.tools(rid)[0]['state'] == 'completed'
    assert app.runs.tools(rid)[0]['result']['status'] == 'applied'
    assert app.runs.get(rid)['state'] == 'interrupted'
    app.store.query('DROP TRIGGER fail_followup')
    app.run_resume(chat['id'], rid)
    settled(app, rid)
    assert effects == [aid]


@pytest.mark.parametrize('pause', [False, True])
def test_stop_during_approval_preserves_receipt_without_auto_resume(app, tmp_path, pause):
    project = tmp_path / 'project'
    project.mkdir()
    chat, attempts = app.new_chat(str(project)), []
    app.guard.handle = lambda *args, **kwargs: {'action': 'allow'}

    def complete(messages, tools, cancel, token, effort):
        attempts.append(True)
        return call('Write', {'file_path': 'once.txt', 'content': 'committed'}, 'write') if len(attempts) == 1 else final(token)

    app.model.complete = complete
    app.work(chat, 'Create a file', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    before = len(attempts)
    actual = app.perform_action

    def perform(action, cid):
        result = actual(action, cid)
        (app.run_pause if pause else app.stop)(cid, rid)
        return result

    app.perform_action = perform
    app.approve(chat['id'], app.store.actions(chat['id'])[0]['id'])
    assert app.runs.get(rid)['state'] == ('paused' if pause else 'stopped')
    assert app.runs.tools(rid)[0]['state'] == 'completed'
    assert len(attempts) == before
    assert (project / 'once.txt').read_text() == 'committed'
    app.run_resume(chat['id'], rid)
    settled(app, rid)
    assert len(app.store.actions(chat['id'])) == 1


def test_automatic_effect_receipt_survives_reporting_failure(app, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    chat, attempts = app.new_chat(str(project)), []
    policy = app.store.setting('permissions')
    policy['mode'] = 'auto'
    app.setting('permissions', policy)
    app.guard.handle = lambda *args, **kwargs: {'action': 'allow'}

    def event(name, data):
        if name == 'actions' and any(a['status'] == 'applied' for a in data['actions']):
            raise OSError('fixture UI delivery failure')

    def complete(messages, tools, cancel, token, effort):
        attempts.append(True)
        return call('Write', {'file_path': 'once.txt', 'content': 'committed'}, 'write') if len(attempts) == 1 else final(token)

    app.model.complete, app.emit = complete, event
    app.work(chat, 'Create a file', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert (project / 'once.txt').read_text() == 'committed'
    assert app.store.actions(chat['id'])[0]['status'] == 'applied'
    assert app.runs.tools(rid)[0]['state'] == 'completed'
    assert app.runs.get(rid)['state'] == 'completed'
    assert len(attempts) == 2
    assert len(app.store.actions(chat['id'])) == 1


def test_plan_cannot_use_failed_test_output_as_completion_evidence(app):
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], 'Verify a change', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    app.runs.begin_tool(rid, '1:tests', 'RunTests', {'recipe': 'pytest'})
    app.runs.end_tool(rid, '1:tests', {'status': 'applied', 'result': {'exit_code': 7, 'output': 'Failure'}})
    with pytest.raises(ValueError, match='resolved evidence'):
        app.interactions.update_plan(rid, [{'id': 'verify', 'title': 'Verify the change', 'status': 'completed', 'evidence_ids': ['1:tests']}])
    app.runs.transition(rid, 'stopped')


def test_broker_receipt_transaction_failure_stops_after_one_effect(app, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    chat, attempts = app.new_chat(str(project)), []
    policy = app.store.setting('permissions')
    policy['mode'] = 'auto'
    app.setting('permissions', policy)
    app.guard.handle = lambda *args, **kwargs: {'action': 'allow'}
    app.store.query("CREATE TRIGGER fail_receipt BEFORE UPDATE ON run_tools WHEN NEW.state='completed' BEGIN SELECT RAISE(ABORT,'fixture receipt failure'); END")

    def complete(messages, tools, cancel, token, effort):
        attempts.append(True)
        return call('Write', {'file_path': 'once.txt', 'content': 'external effect'}, 'write')

    app.model.complete = complete
    app.work(chat, 'Create a file', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert (project / 'once.txt').read_text() == 'external effect'
    assert app.store.actions(chat['id'])[0]['status'] != 'applied'
    assert app.runs.get(rid)['state'] == 'needs_reconciliation'
    receipt = app.runs.tools(rid)[0]
    assert receipt['state'] == 'uncertain' and receipt['result']['action_id'] == app.store.actions(chat['id'])[0]['id']
    assert len(attempts) == 1


def test_optional_memory_failure_does_not_overturn_settled_run(app):
    chat, attempts = app.new_chat(), []
    app.model.complete = lambda m, t, c, token, e: (attempts.append(True) or final(token))

    def fail_memory(*args, **kwargs):
        raise OSError('fixture optional memory failure')

    app.memory.episode = fail_memory
    app.work(chat, 'Explain a stack', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'completed' and app.active is None
    assert len(attempts) == 1
    assert any(e['kind'] == 'memory_update_failed' for e in app.runs.events(rid))


@pytest.mark.parametrize('items', [
    [], ITEMS * 2, [{'id': 'x', 'title': 'Paste your API key'}],
    [{'id': 'x', 'title': 'Scope?', 'options': [{'id': 'a', 'label': 'A'}, {'id': 'a', 'label': 'B'}]}],
])
def test_question_schema_bounds_and_credential_requests(items):
    with pytest.raises(ValueError):
        question_items(items)


def test_pending_records_name_only_host_held_work(app):
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], 'Hold for input', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    assert app.interactions.pending_records(app.store.db, rid) == {'questions': [], 'actions': [], 'uncertain': []}
    question = app.interactions.create_question(rid, '1:ask', ITEMS, False)
    aid = app.store.action(chat['id'], 'edit', {'path': 'a.txt'})
    app.runs.begin_tool(rid, '2:edit', 'Edit', {})
    app.runs.end_tool(rid, '2:edit', {'action_id': aid, 'kind': 'edit', 'status': 'awaiting_user_approval'})
    app.runs.begin_tool(rid, '3:write', 'Write', {})
    app.runs.end_tool(rid, '3:write', {'error': 'lost'}, uncertain=True)
    held = app.interactions.pending_records(app.store.db, rid)
    assert held == {'questions': [(question['id'], 'Which scope should I use?')], 'actions': [aid], 'uncertain': ['3:write']}
    app.store.query("UPDATE actions SET status='rejected' WHERE id=?", (aid,))
    app.interactions.cancel_question(chat['id'], question['id'], 1)
    assert app.interactions.pending_records(app.store.db, rid) == {'questions': [], 'actions': [], 'uncertain': ['3:write']}
    app.runs.transition(rid, 'stopped')


def test_chat_deletion_cascades_goal_records(app):
    from pebble.goals import Goals
    goals = Goals(app)
    chat, kept = app.new_chat(), app.new_chat()
    gid = goals.create(chat['id'], 'Ship the release')['id']
    goals.add_criterion(chat['id'], gid, 'Notes reviewed')
    goals.add_step(chat['id'], gid, 'Draft notes')
    other = goals.create(kept['id'], 'Keep this goal')['id']
    app.delete_chat(chat['id'])
    for table in ('goals', 'goal_criteria', 'goal_steps', 'goal_events'):
        key = 'id' if table == 'goals' else 'goal_id'
        assert not app.store.query(f'SELECT 1 FROM {table} WHERE {key}=?', (gid,))
    assert goals.current(kept['id'])['id'] == other


def test_cloud_context_retains_inbox_input_and_strips_host_metadata():
    messages = [{'role': 'system', 'content': 'policy'},
        {'role': 'user', 'content': 'old history ' * 500},
        {'role': 'user', 'content': 'Use this project only', '_pebble_inbox': 'human-input'},
        {'role': 'assistant', 'content': 'Working'},
        {'role': 'user', 'content': 'Original goal', '_pebble_current': True}]
    bounded = Providers.bounded(messages, 600, reserve=50)
    assert any(m['content'] == 'Use this project only' for m in bounded)
    assert not any(k.startswith('_pebble_') for m in bounded for k in m)
    assert messages[2]['_pebble_inbox'] == 'human-input'
