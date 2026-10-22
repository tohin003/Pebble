"""Retired evidence must be durable before dispatch, without rewriting humans."""

import copy
import json

import pytest

from pebble.app import App
from pebble.compaction import compact_window, fingerprint, is_spine
from pebble.context_manifest import ContextBudgetExceeded, assemble_context


def count(messages, tools):
    return len(json.dumps([messages, tools], ensure_ascii=False)) // 4 + 1


def window():
    return [
        {'role': 'system', 'content': 'host policy'},
        {'role': 'user', '_pebble_checkpoint': True, 'content': 'host facts'},
        {'role': 'user', '_pebble_current': True, 'content': 'Keep 漢字\nNever publish; preserve tabs\tand \\n.'},
        {'role': 'user', '_pebble_inbox': 'human', 'content': 'Human-origin: keep this exact constraint.'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'old', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{"file_path":"a.txt"}'}}],
         '_responses_output': [{'type': 'reasoning', 'encrypted_content': 'OPAQUE' * 200},
            {'type': 'function_call', 'call_id': 'old', 'name': 'Read', 'arguments': '{"file_path":"a.txt"}'}]},
        {'role': 'tool', 'tool_call_id': 'old', 'content': 'OLD RAW SOURCE ' * 450},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'new', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{"file_path":"b.txt"}'}}]},
        {'role': 'tool', 'tool_call_id': 'new', 'content': 'latest evidence'},
    ]


def records():
    return [{'call_id': '1:old', 'name': 'Read', 'arguments': {'file_path': 'a.txt'}, 'state': 'completed', 'result': {'receipt_id': '1:old', 'untrusted_source': 'OLD RAW SOURCE'}},
        {'call_id': '2:new', 'name': 'Read', 'arguments': {'file_path': 'b.txt'}, 'state': 'completed', 'result': {'receipt_id': '2:new', 'file_hash': 'a' * 64}}]


@pytest.fixture
def owned(tmp_path):
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], 'Keep 漢字; never publish.', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    execution, token = app.controller.enter(app.runs.get(rid))
    app.active, app.active_run = chat['id'], rid
    for receipt in records():
        app.runs.begin_tool(rid, receipt['call_id'], receipt['name'], receipt['arguments'])
        app.runs.end_tool(rid, receipt['call_id'], receipt['result'])
    app.runs.checkpoint(rid, {'phase': 'model', 'messages': window(), 'available': []}, steps=2, elapsed=0)
    yield app, chat, rid, execution
    app.active = app.active_run = None
    app.controller.leave(execution, token)
    app.close()


def compact(owned, budget=900):
    app, _, rid, _ = owned
    messages = window()
    projection = assemble_context(messages, [], budget, counter=count, counter_method='fixture')
    return compact_window(messages, projection, app.runs.get(rid), app.runs.tools(rid), [], budget, counter=count, method='fixture')


def test_exact_human_and_latest_exchange_survive_with_opaque_archive(owned):
    before = window()
    value = compact(owned)
    assert value and count(value.projection.messages, []) <= 900
    assert before[2] in value.projection.messages and before[3] in value.projection.messages
    assert before[-2:] == value.projection.messages[-2:]
    archived = [a['message'] for a in value.archive]
    assert archived == before[4:6]
    assert archived[0]['_responses_output'][0]['encrypted_content'] == 'OPAQUE' * 200
    assert 'OLD RAW SOURCE' not in json.dumps(value.projection.messages)
    assert 'OLD RAW SOURCE' not in json.dumps(value.manifest)
    assert 'never publish' not in json.dumps(value.manifest).lower()
    assert next(m for m in value.projection.messages if is_spine(m))['_pebble_checkpoint']
    assert value.manifest['authorization'] == 'none'


def test_compaction_is_deterministic_and_does_not_mutate_inputs(owned):
    first, second = compact(owned), compact(owned)
    assert first.manifest == second.manifest and first.archive == second.archive
    original = window()
    first.archive[0]['message']['_responses_output'][0]['encrypted_content'] = 'changed'
    assert window() == original and first.archive != second.archive


def test_fitting_projection_needs_no_archive_or_extra_counter(owned):
    app, _, rid, _ = owned
    messages = window()
    projection = assemble_context(messages, [], 10000, counter=count, counter_method='fixture')
    assert compact_window(messages, projection, app.runs.get(rid), app.runs.tools(rid), [], 10000,
        counter=lambda *args: pytest.fail('Unneeded repair counter called'), method='fixture') is None


def test_extra_evidence_that_cannot_fit_fails_without_archive(owned):
    app, _, rid, _ = owned
    # Phase 15: the small counts spine is smaller than the Phase 14 one, so the
    # window that fits the initial projection but not the spine is now 200.
    with pytest.raises(ContextBudgetExceeded):
        compact(owned, budget=200)
    assert not app.runs.compactions(rid) and app.runs.get(rid)['checkpoint']['messages'] == window()


def test_archive_checkpoint_and_event_commit_together(owned):
    app, chat, rid, _ = owned
    value = compact(owned)
    assert app.runs.compact_checkpoint(rid, value) == 1
    assert app.runs.get(rid)['checkpoint']['messages'] == value.projection.messages
    assert app.run_context_archive(chat['id'], rid, 1)['archive'] == value.archive
    assert app.runs.compactions(rid)[0]['generation'] == 1
    assert 'manifest' not in app.runs.compactions(rid)[0]
    assert any(e['kind'] == 'context_compacted' for e in app.runs.events(rid))


def test_archive_failure_rolls_back_checkpoint_before_inference(owned, monkeypatch):
    app, _, rid, _ = owned
    old_event = app.runs.event
    def fail(db, run_id, kind, payload):
        if kind == 'context_compacted':
            raise OSError('fixture disk failure')
        return old_event(db, run_id, kind, payload)
    monkeypatch.setattr(app.runs, 'event', fail)
    with pytest.raises(OSError):
        app.runs.compact_checkpoint(rid, compact(owned))
    assert not app.runs.compactions(rid)
    assert app.runs.get(rid)['checkpoint']['messages'] == window()


def test_retry_reuses_archive_and_restores_projected_checkpoint(owned):
    app, _, rid, _ = owned
    value = compact(owned)
    assert app.runs.compact_checkpoint(rid, value) == 1
    assert app.runs.compact_checkpoint(rid, value) == 1
    # An abandoned attempt may have persisted its original preflight input.
    app.runs.checkpoint(rid, {'phase': 'model', 'messages': window()}, steps=2, elapsed=1)
    assert app.runs.compact_checkpoint(rid, value) == 1
    assert len(app.runs.compactions(rid)) == 1
    assert app.runs.get(rid)['checkpoint']['messages'] == value.projection.messages


@pytest.mark.parametrize('phase,mutate', [('tools', False), ('model', True)])
def test_wrong_boundary_or_changed_checkpoint_cannot_retire_context(owned, phase, mutate):
    app, _, rid, _ = owned
    value = compact(owned)
    current = window()
    if mutate:
        current[2]['content'] += ' additional constraint'
    app.runs.checkpoint(rid, {'phase': phase, 'messages': current}, steps=2, elapsed=0)
    with pytest.raises(ValueError):
        app.runs.compact_checkpoint(rid, value)
    assert not app.runs.compactions(rid)


def test_provider_hook_commits_archive_before_wire_dispatch(owned):
    app, _, rid, execution = owned
    wire = app.model.project_context(window(), [], 900, counter=count, method='fixture')
    assert app.runs.compactions(rid) and execution.compaction_generation == 1
    assert app.runs.get(rid)['checkpoint']['messages'] == execution.projected_messages
    assert all(not any(k.startswith('_pebble_') for k in m) for m in wire)
    assert wire[-2:][0]['tool_calls'][0]['id'] == 'new'


def test_read_run_evidence_is_owned_bounded_and_withholds_raw_source(owned):
    app, chat, rid, _ = owned
    result = app.execute('ReadRunEvidence', {'receipt_id': '1:old'}, None, chat['id'], call_id='3:evidence')
    assert result['records'][0]['receipt_id'] == '1:old' and result['records'][0]['outcome'] == 'completed'
    assert 'OLD RAW SOURCE' not in json.dumps(result) and 'arguments' not in result['records'][0]
    assert result['authorization'] == 'none' and len(app.runs.tools(rid)) == 3
    with pytest.raises(ValueError, match='Receipt not found'):
        app.execute('ReadRunEvidence', {'receipt_id': 'foreign'}, None, chat['id'], call_id='4:foreign')
    with pytest.raises(ValueError, match='bounded'):
        app.execute('ReadRunEvidence', {'limit': True}, None, chat['id'], call_id='5:bad')
    other = app.new_chat()
    with pytest.raises(ValueError, match='owning run'):
        app.execute('ReadRunEvidence', {}, None, other['id'])


def test_native_archive_scope_and_manual_active_boundary(owned):
    app, chat, rid, _ = owned
    app.runs.compact_checkpoint(rid, compact(owned))
    other = app.new_chat()
    with pytest.raises(ValueError, match='conversation'):
        app.run_context_archive(other['id'], rid, 1)
    with pytest.raises(ValueError, match='Pause'):
        app.run_compact(chat['id'], rid)
    with pytest.raises(ValueError, match='generation'):
        app.run_context_archive(chat['id'], rid, True)


def test_manual_compaction_does_not_invoke_model_or_replay_tools(owned):
    app, chat, rid, _ = owned
    app.active = app.active_run = None
    app.model.complete = lambda *args: pytest.fail('Manual compaction called inference')
    result = app.run_compact(chat['id'], rid, target_tokens=900)
    assert result['status'] == 'compacted'
    assert len(app.runs.tools(rid)) == 2 and not app.store.actions(chat['id'])


def test_archive_restart_and_forgetting_are_independent_of_memory(tmp_path):
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], 'Keep exact constraints', app.runtime_config(chat, False))
    app.store.set_setting('memory_enabled', False)
    app.runs.checkpoint(rid, {'phase': 'model', 'messages': window(), 'available': []}, steps=2, elapsed=0)
    projection = assemble_context(window(), [], 900, counter=count, counter_method='fixture')
    value = compact_window(window(), projection, app.runs.get(rid), [], [], 900, counter=count, method='fixture')
    app.runs.compact_checkpoint(rid, value)
    app.runs.transition(rid, 'paused')
    app.close()
    restarted = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        assert restarted.run_context_archive(chat['id'], rid, 1)['archive'] == value.archive
        assert restarted.runs.resume_snapshot(rid)['messages'] == value.projection.messages
        restarted.delete_chat(chat['id'])
        assert not restarted.runs.compactions(rid)
    finally:
        restarted.close()


# Phase 15 (C03/C04): the spine comes from the full packet, compaction must make
# progress, and archived results stay readable as pages.

from pebble import compaction as compaction_module  # noqa: E402
from pebble.compaction import (  # noqa: E402
    CompactionStalled, archived_results, build_spine, evidence_packet, read_archived_page, spine_bytes,
)

PRID, PCID, QP = 'run-p15', 'chat-p15', 'q' * 32
H_READ, H_DISK = 'a' * 64, 'b' * 64
PLAN_TITLE = 'PLAN-TITLE-SENTINEL agent words'
QUESTION_TITLE = 'QUESTION-SENTINEL agent words'
GOAL = 'Fix the parser; never edit prod.yaml.'


def p_run(steps=7):
    return {'id': PRID, 'chat_id': PCID, 'goal': GOAL, 'state': 'running', 'steps': steps, 'elapsed': 41.5,
            'config': {'limits': {'max_steps': 64, 'max_seconds': 900.0}}}


def p_receipts():
    return [
        {'call_id': '1:read', 'name': 'Read', 'arguments': {'file_path': 'src/app.py'}, 'state': 'completed',
         'result': {'path': 'src/app.py', 'file_hash': H_READ, 'receipt_id': '1:read'}},
        {'call_id': '2:edit', 'name': 'Edit', 'arguments': {}, 'state': 'completed',
         'result': {'status': 'awaiting_user_approval', 'action_id': 'act-1', 'kind': 'edit', 'receipt_id': '2:edit'}},
        {'call_id': '3:ask', 'name': 'AskUserQuestion', 'arguments': {}, 'state': 'completed',
         'result': {'question_id': QP, 'revision': 1, 'required': True, 'status': 'awaiting_user_input',
                    'receipt_id': '3:ask'}},
        {'call_id': '4:plan', 'name': 'UpdatePlan', 'arguments': {}, 'state': 'completed',
         'result': {'status': 'updated', 'revision': 1, 'receipt_id': '4:plan'}},
    ]


def p_packet(steps=7, **extra):
    inputs = [
        {'id': 'steer-1', 'run_id': PRID, 'sequence': 1, 'kind': 'steering', 'origin': 'human',
         'payload': {'text': 'Also keep the CLI.'}, 'status': 'delivered', 'created': 1.0, 'delivered_step': 1},
        {'id': 'steer-2', 'run_id': PRID, 'sequence': 2, 'kind': 'steering', 'origin': 'human',
         'payload': {'text': 'And the docs.'}, 'status': 'pending', 'created': 2.0, 'delivered_step': None},
    ]
    plan = {'run_id': PRID, 'revision': 1, 'steps': [
        {'id': 'inspect', 'title': PLAN_TITLE, 'status': 'completed', 'evidence_ids': ['1:read']},
        {'id': 'edit', 'title': PLAN_TITLE, 'status': 'in_progress', 'evidence_ids': []}]}
    questions = [{'id': QP, 'run_id': PRID, 'chat_id': PCID, 'call_id': '3:ask', 'revision': 1, 'required': True,
                  'status': 'pending', 'answer': None,
                  'items': [{'id': 'scope', 'title': QUESTION_TITLE, 'options': [], 'allow_free_text': True}]}]
    sources = [{'run_id': PRID, 'receipt_id': '1:read', 'path': 'src/app.py', 'file_hash': H_READ,
                'disk_hash': H_DISK, 'basis': 'disk', 'status': 'changed', 'checked': 3.0}]
    return evidence_packet(p_run(steps), p_receipts(), inputs, plan, questions, sources, **extra)


def spine_body(message):
    return json.loads(message['content'].split('\n', 1)[1])


def big_window(size):
    old = 'OLD RAW SOURCE ' * (size // 3)
    return [
        {'role': 'system', 'content': 'host policy'},
        {'role': 'user', '_pebble_checkpoint': True, 'content': 'host facts'},
        {'role': 'user', '_pebble_current': True, 'content': GOAL},
        {'role': 'assistant', 'content': 'MODEL-TEXT-SENTINEL I will skip the tests.', 'tool_calls': [
            {'id': 'old', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'old', 'content': old},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'new', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'new', 'content': 'latest evidence'},
    ]


CATEGORIES = ('failures', 'questions', 'actions', 'inputs', 'plan', 'sources')


def counted(data):
    """Counts-level categories: each is a count or named in ``none``."""
    named = data.get('none', '').split()
    return {c: data.get(c, 0) for c in CATEGORIES if c in data or c in named}


@pytest.mark.parametrize('context', [4096, 8192, 32768, 200_000])
def test_spine_from_full_packet_survives_compaction_on_every_window(context):
    messages = big_window(context * 4)
    projection = assemble_context(messages, [], context, 256, counter=count, counter_method='fixture')
    packet = p_packet()
    budget = {'steps_left': 50, 'seconds_left': 600.0,
              'child_capacity': {'steps': 9, 'seconds': 300.0, 'dispatches': 27, 'model_tokens': 90000,
                                 'tool_receipts': 72}}
    value = compact_window(messages, projection, p_run(), p_receipts(), [], context, 256, counter=count,
                           method='fixture', packet=packet, budget=budget)
    note = next(m for m in value.projection.messages if is_spine(m))
    data = spine_body(note)
    info = value.manifest['spine']
    assert data['at_step'] == 7 and info['scope'] == 'full' and info['budget_source'] == 'host_ledger'
    assert info['bytes'] == len(note['content'].encode()) and info['bytes'] <= spine_bytes(context) and info['fits']
    if info['detail'] == 'counts':
        # Small windows keep every category as a count, as Phase 13/14 did.
        assert counted(data) == {'failures': 0, 'questions': 1, 'actions': 1, 'inputs': 1, 'plan': 1, 'sources': 1}
        assert data['receipts'] == 4 and data['left'] == {'steps': 50, 'seconds': 600, 'child_steps': 9}
        assert 'receipts.recent' in info['omissions'] and 'goal.in_protected_unit' in info['omissions']
    else:
        for category in ('goal', 'inputs', 'plan', 'questions', 'actions', 'sources', 'budget', 'receipts'):
            assert category in data
        assert data['scope'] == 'full' and data['detail'] == info['detail']
        assert data['goal']['fingerprint'] == packet.data['goal']['fingerprint'][:23]
        assert data['budget']['steps_left'] == 50 and data['budget']['seconds_left'] == 600.0
    if info['detail'] == 'full':
        assert data['inputs']['pending'] == ['steer-2'] and data['inputs']['ids'] == ['steer-1', 'steer-2']
        assert data['plan']['steps'] == [['inspect', 'completed'], ['edit', 'in_progress']]
        assert data['questions']['pending'] == [[QP, True]]
        assert data['actions']['pending'] == [['act-1', 'awaiting_approval']]
        assert data['sources']['items'] == [['src/app.py', 'sha256:' + H_READ[:16], 'changed']]
        assert data['budget']['child_capacity']['steps'] == 9
    elif info['detail'] != 'counts':
        assert info['omissions'] and 'receipts.recent' in info['omissions'] and data['sources']['stale'] == 1
    assert (context <= 4096) == (info['detail'] == 'counts')
    text = note['content']
    for sentinel in (PLAN_TITLE, QUESTION_TITLE, 'MODEL-TEXT-SENTINEL', 'Also keep the CLI', 'never edit prod.yaml'):
        assert sentinel not in text
    assert compaction_module.fingerprint(value.manifest['spine']) and 'src/app.py' not in json.dumps(value.manifest)
    assert value.manifest['progress']['freed'] > 0 and value.manifest['progress']['fits']


def test_spine_detail_levels_keep_every_category_down_to_counts():
    packet = p_packet()
    seen = []
    for size in (4800, 900, 700, 600, 1):
        message, info = build_spine(packet, size)
        data = spine_body(message)
        seen.append(info['detail'])
        assert info['budget_source'] == 'run_record'
        if info['detail'] == 'counts':
            assert set(counted(data)) == set(CATEGORIES) and data['left']['steps'] == 57
        else:
            assert {'goal', 'inputs', 'plan', 'questions', 'actions', 'sources', 'budget', 'receipts'} <= set(data)
            assert data['budget']['steps_left'] == 57
    assert seen[0] == 'full' and seen[-1] == 'counts'
    counts = spine_body(build_spine(packet, 1)[0])
    assert counted(counts) == {'failures': 0, 'questions': 1, 'actions': 1, 'inputs': 1, 'plan': 1, 'sources': 1}
    assert counts == {'at_step': 7, 'receipts': 4, 'questions': 1, 'actions': 1, 'inputs': 1, 'plan': 1,
                      'sources': 1, 'none': 'failures', 'left': {'steps': 57, 'seconds': 858}}
    assert 'inputs.ids' in build_spine(packet, 1)[1]['omissions']
    for detail in compaction_module.DETAIL:  # Forced levels, as compaction degrades while fitting.
        assert build_spine(packet, 1, detail=detail)[1]['detail'] == detail
    with pytest.raises(ValueError):
        build_spine(packet, 4800, detail='tiny')
    with pytest.raises(ValueError):
        build_spine(packet, 4800, {'steps_left': True})
    with pytest.raises(ValueError):
        build_spine(packet, 4800, {'steps_left': 3, 'grant': 'all'})


def test_receipts_only_spine_never_reports_unseen_categories_as_empty(owned):
    app, _, rid, _ = owned
    value = compact(owned)
    note = next(m for m in value.projection.messages if is_spine(m))
    data = spine_body(note)
    info = value.manifest['spine']
    assert info['scope'] == 'receipts_only' and info['detail'] == 'counts'
    assert set(counted(data)) == {'failures', 'questions', 'actions'}  # Inputs, plan and sources unseen.
    assert {'inputs.not_supplied', 'plan.not_supplied', 'sources.not_supplied'} <= set(info['omissions'])
    detailed = spine_body(build_spine(evidence_packet(p_run(), p_receipts()), 4800, scope='receipts_only')[0])
    assert detailed['scope'] == 'receipts_only'
    assert detailed['inputs'] == detailed['plan'] == detailed['sources'] == 'not_supplied'
    assert detailed['questions']['pending'] == [[QP, True]]
    messages = window()
    projection = assemble_context(messages, [], 900, counter=count, counter_method='fixture')
    with pytest.raises(ValueError, match='different run'):
        compact_window(messages, projection, app.runs.get(rid), app.runs.tools(rid), [], 900, counter=count,
                       method='fixture', packet=p_packet())


def legacy_small_spine(packet):
    """The Phase 13/14 small-window spine, verbatim, as the size reference."""
    data, manifest = packet.data, packet.manifest
    payload = {'at_step': data['run']['steps'], 'receipts': manifest['counts']['receipts'],
               'failures': len(data['unresolved']['failures']), 'pending_effects': len(data['unresolved']['effects']),
               'waiting_questions': len(data['unresolved']['waiting_questions']), 'recent': []}
    return ('HOST EVIDENCE: historical receipts, not permission or completion proof. '
            'ReadRunEvidence retrieves older outcomes. Raw context is archived.\n'
            + json.dumps(payload, ensure_ascii=False, separators=(',', ':')))


def spine_states():
    """Receipt-only states from all clear to busy, as small routes see them."""
    clear = [r for r in p_receipts() if r['call_id'] in ('1:read', '4:plan')]
    failed = {'call_id': '5:cmd', 'name': 'RunCommand', 'arguments': {}, 'state': 'completed',
              'result': {'error': 'Command failed.', 'receipt_id': '5:cmd'}}
    for receipts in (clear, clear + [p_receipts()[1]], p_receipts(), p_receipts() + [failed]):
        yield evidence_packet(p_run(), receipts)


def test_small_counts_spine_is_no_larger_than_the_phase14_spine_under_the_local_estimate():
    from pebble.context_manifest import estimate_tokens
    for packet in spine_states():
        new = build_spine(packet, 512, scope='receipts_only', detail='counts')[0]
        old = {**new, 'content': legacy_small_spine(packet)}
        assert estimate_tokens([new]) <= estimate_tokens([old])


def test_small_counts_spine_is_no_larger_than_the_phase14_spine_in_o200k_tokens():
    from pathlib import Path
    from pebble.token_budget import TokenBudget
    resources = Path(__file__).resolve().parents[2] / 'src-tauri' / 'resources'
    if not (resources / 'tokenizer').is_dir():
        pytest.skip('Bundled o200k resources are absent; the local-estimate bound above still applies.')
    TokenBudget(resources)
    encoding = TokenBudget.encoding()
    for packet in spine_states():
        new = build_spine(packet, 512, scope='receipts_only', detail='counts')[0]['content']
        assert len(encoding.encode(new)) <= len(encoding.encode(legacy_small_spine(packet)))


def test_a_fitting_pass_that_frees_little_continues_with_exact_limits():
    # The 4K route shape: a pass that frees under a tenth of the budget but fits must not stop.
    floor = 'PROTECTED POLICY ' * 700
    messages = [{'role': 'system', 'content': floor}, {'role': 'user', '_pebble_current': True, 'content': GOAL},
                {'role': 'user', 'content': 'older human turn ' * 25}, {'role': 'assistant', 'content': 'ok'},
                {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'new', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'new', 'content': 'latest'}]
    without = [m for i, m in enumerate(messages) if i not in (2, 3)]
    spine_note = build_spine(evidence_packet(p_run(1), p_receipts()[:1]), 512, scope='receipts_only',
                             detail='counts')[0]
    # Room for the floor and the counts spine with a few tokens spare: far less than a tenth.
    context = count([without[0], spine_note, *without[1:]], []) + 5 + 64
    projection = assemble_context(messages, [], context, 64, counter=count, counter_method='fixture')
    assert projection.manifest['omitted']['messages'] == 2
    value = compact_window(messages, projection, p_run(1), p_receipts()[:1], [], context, 64, counter=count,
                           method='fixture')
    progress = value.manifest['progress']
    assert 0 < progress['freed'] < progress['headroom'] and progress['target'] == 'limit' and progress['fits']
    assert value.projection.manifest['limits'] == {'context': context, 'output_reserve': 64, 'input': context - 64}
    assert value.projection.manifest['dispatchable'] and value.projection.manifest['compaction']['target'] == 'limit'


def test_minimum_progress_guard_stops_a_non_shrinking_loop():
    """Passes that fit continue however little they free; a loop whose protected
    floor keeps growing cannot converge and stops with a typed refusal."""
    messages = [{'role': 'system', 'content': 'PROTECTED POLICY ' * 300},
                {'role': 'user', '_pebble_current': True, 'content': GOAL}]
    limit = count(messages, []) + 900
    receipts, run, passes, small_passes, stopped = [], p_run(0), 0, 0, None
    for step in range(1, 60):
        call = f'c{step}'
        # Each turn adds protected human input as well as a retirable exchange.
        messages += [{'role': 'user', '_pebble_inbox': f'input-{step}', 'content': f'HOST INPUT {step}: keep going ' * 3},
                     {'role': 'assistant', 'content': None, 'tool_calls': [
                         {'id': call, 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}}]},
                     {'role': 'tool', 'tool_call_id': call, 'content': 'small result ' * 3}]
        receipts.append({'call_id': f'{step}:{call}', 'name': 'Read', 'arguments': {}, 'state': 'completed',
                         'result': {'receipt_id': f'{step}:{call}'}})
        run = {**run, 'steps': step}
        try:
            projection = assemble_context(messages, [], limit, counter=count, counter_method='fixture')
            value = compact_window(messages, projection, run, receipts, [], limit, counter=count, method='fixture')
        except ContextBudgetExceeded as exc:
            stopped = exc
            break
        if value:
            passes += 1
            small_passes += value.manifest['progress']['freed'] < value.manifest['progress']['headroom']
            messages = value.projection.messages
    assert stopped is not None and step < 59, 'a loop whose floor outgrows the window must stop'
    assert passes and small_passes, 'fitting passes that free little are progress, not a stall'
    assert stopped.manifest['dispatchable'] is False and stopped.manifest['status'] in ('exceeded', 'stalled')
    if isinstance(stopped, CompactionStalled):
        assert stopped.reason == 'over_limit' and stopped.manifest['compaction_guard']['reason'] == 'over_limit'
    assert 'small result' not in json.dumps(stopped.manifest) and 'PROTECTED POLICY' not in str(stopped)


def test_pass_that_cannot_fit_or_shrink_stops_with_a_typed_reason():
    messages = [{'role': 'system', 'content': 'PROTECTED POLICY ' * 100},
                {'role': 'user', '_pebble_current': True, 'content': GOAL},
                {'role': 'user', 'content': 'older turn ' * 40}, {'role': 'assistant', 'content': 'ok'},
                {'role': 'user', 'content': 'latest question'}]
    floor = [m for i, m in enumerate(messages) if i not in (2, 3)]
    # The first pass must add a spine; with no room for it the floor cannot fit.
    tight = count(floor, []) + 2
    projection = assemble_context(messages, [], tight, counter=count, counter_method='fixture')
    with pytest.raises(CompactionStalled) as caught:
        compact_window(messages, projection, p_run(1), [], [], tight, counter=count, method='fixture')
    assert caught.value.reason == 'over_limit' and isinstance(caught.value, ContextBudgetExceeded)
    assert caught.value.manifest['compaction_guard'] == {'reason': 'over_limit', 'before': projection.manifest[
        'input_tokens']['before'], 'limit': tight, 'spine_detail': 'counts'}
    assert 'required context' in str(caught.value)
    # A counter that reports no shrink (an inconsistent provider count) cannot loop.
    roomy = count(messages, []) - 1
    projection = assemble_context(messages, [], roomy, counter=count, counter_method='fixture')
    inflated = lambda ms, ts: count(ms, ts) + (10_000 if any(is_spine(m) for m in ms) else 0) if len(ms) > 3 else count(ms, ts)
    with pytest.raises(ContextBudgetExceeded):
        compact_window(messages, projection, p_run(1), [], [], roomy, counter=inflated, method='fixture')
    shrinkless = assemble_context(messages, [], roomy, counter=count, counter_method='fixture')
    shrinkless.manifest['input_tokens']['before'] = 1  # As if the window had not been over the limit.
    with pytest.raises(CompactionStalled) as caught:
        compact_window(messages, shrinkless, p_run(1), [], [], roomy, counter=count, method='fixture')
    assert caught.value.reason == 'no_progress' and caught.value.manifest['compaction_guard']['freed'] <= 0


def test_compaction_leaves_headroom_so_steady_turns_do_not_recompact_each_time():
    messages = [{'role': 'system', 'content': 'policy'}, {'role': 'user', '_pebble_current': True, 'content': GOAL}]
    receipts, run, passes, turns = [], p_run(0), 0, 0
    for step in range(1, 61):
        call = f'c{step}'
        messages += [{'role': 'assistant', 'content': None, 'tool_calls': [
                         {'id': call, 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}}]},
                     {'role': 'tool', 'tool_call_id': call, 'content': 'evidence line ' * 20}]
        receipts.append({'call_id': f'{step}:{call}', 'name': 'Read', 'arguments': {}, 'state': 'completed',
                         'result': {'receipt_id': f'{step}:{call}'}})
        run = {**run, 'steps': step}
        projection = assemble_context(messages, [], 4096, 256, counter=count, counter_method='fixture')
        value = compact_window(messages, projection, run, receipts, [], 4096, 256, counter=count, method='fixture')
        turns += 1
        if value:
            passes += 1
            progress = value.manifest['progress']
            assert progress['freed'] >= progress['headroom'] and progress['target'] == 'headroom'
            assert progress['after'] <= 4096 - 256 - progress['headroom']
            assert value.projection.manifest['limits'] == {'context': 4096, 'output_reserve': 256, 'input': 3840}
            messages = value.projection.messages
    assert 0 < passes < turns // 3


def test_archived_results_are_readable_as_bounded_pages(owned):
    value = compact(owned)
    archives = [{'generation': 1, 'archive': value.archive}]
    index = archived_results(archives, page_chars=1000)
    assert [(e['page_id'], e['tool_call_id']) for e in index] == [('1:5', 'old')]
    assert index[0]['chars'] == len('OLD RAW SOURCE ' * 450) and index[0]['pages'] == 7
    assert 'OLD RAW SOURCE' not in json.dumps(index)
    pages, offset = [], 0
    while offset is not None:
        result = read_archived_page(archives, '1:5', offset, 1000)
        assert result['authorization'] == 'none' and result['freshness'] == 'historical_not_verified'
        assert len(result['untrusted_text']) <= 1000
        pages.append(result['untrusted_text'])
        offset = result['next_offset']
    assert ''.join(pages) == 'OLD RAW SOURCE ' * 450 and len(pages) == 7
    assert value.manifest['archived_results'] == 1
    for bad in (('2:5', 0, 1000), ('1:5', 99_999, 1000), ('1:5', -1, 1000), ('1:5', 0, 0)):
        with pytest.raises(ValueError):
            read_archived_page(archives, *bad)
    with pytest.raises(ValueError):
        archived_results([{'generation': 0, 'archive': []}])


def test_settlement_audit_reaches_the_spine_through_the_packet():
    audit = {'version': 1, 'kind': 'source_audit', 'authorization': 'none',
             'entries': [{'path_id': 'p:' + 'c' * 24, 'state': 'deleted'}],
             'counts': {'unchanged': 0, 'changed': 0, 'deleted': 1, 'unreadable': 0, 'skipped_budget': 0}}
    packet = p_packet(audit=audit)
    data = spine_body(build_spine(packet, 4800)[0])
    assert data['sources']['stale'] == 1  # The witness; the audited ID is in unresolved.stale_sources.
    assert {'path_id': 'p:' + 'c' * 24, 'status': 'deleted', 'basis': 'settlement_audit'} in \
        packet.data['unresolved']['stale_sources']
