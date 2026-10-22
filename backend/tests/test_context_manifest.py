"""Context projection keeps host state and whole exchanges, with a content-free manifest."""

import copy
import json
import math

import pytest

from pebble import context_manifest
from pebble.context_manifest import (
    BASE_TOKENS, DIAGNOSTIC_UNITS, IMAGE_TOKENS, MAX_CONTEXT, MAX_MESSAGES,
    ContextBudgetExceeded, ContextCounterError, ContextIntegrityError,
    assemble_context, estimate_tokens,
)

SECRET = 'SENTINEL-raw-text-7f3a'
TOOLS = [{'type': 'function', 'function': {
    'name': 'Read', 'description': 'Read a project file. ' + SECRET,
    'parameters': {'type': 'object', 'properties': {'file_path': {'type': 'string'}}}}}]
BASE = [{'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'goal ' + SECRET, '_pebble_current': True}]


def chars(messages, tools):
    """Deterministic additive counter: a token per four serialized characters."""
    return sum(len(json.dumps(m)) // 4 for m in messages) + len(json.dumps(tools)) // 4 + 7


class Recording:
    def __init__(self):
        self.calls = self.seen = 0

    def __call__(self, messages, tools):
        self.calls += 1
        self.seen += len(messages)
        return chars(messages, tools)


def result(ident, content='{"status":"ok"}'):
    return {'role': 'tool', 'tool_call_id': ident, 'content': content}


def calls(*ids, **extra):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': i, 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}} for i in ids], **extra}


def exchange(*ids, size=200):
    call = {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': i, 'type': 'function', 'function': {'name': 'Read', 'arguments': json.dumps({'file_path': i + '.py'})}}
        for i in ids]}
    return [call] + [result(i, json.dumps({'status': 'ok', 'source': SECRET + 'x' * size})) for i in ids]


def conversation():
    return [
        {'role': 'system', 'content': 'Host policy. ' + SECRET},
        {'role': 'user', 'content': 'Host checkpoint: read a.py', '_pebble_checkpoint': True},
        {'role': 'user', 'content': 'Recalled note ' + 'm' * 400, '_pebble_memory': True},
        {'role': 'user', 'content': 'Earlier question ' + 'q' * 400},
        {'role': 'assistant', 'content': 'Earlier answer ' + 'a' * 400},
        {'role': 'user', 'content': 'Current goal ' + SECRET, '_pebble_current': True},
        {'role': 'user', 'content': 'Plan: inspect, edit, test', '_pebble_plan': 'plan-1'},
        *exchange('old1', 'old2', size=800),
        *exchange('mid1', size=600),
        *exchange('new1', 'new2', size=300),
        {'role': 'user', 'content': 'HOST INPUT: also update docs', '_pebble_inbox': 'input-1'},
    ]


PROTECTED_INDICES = (0, 1, 5, 6, 12, 13, 14, 15)


def pick(messages, indices):
    return [messages[i] for i in indices]


def assert_paired(messages):
    open_calls = set()
    for m in messages:
        if m['role'] == 'tool':
            assert m['tool_call_id'] in open_calls
            open_calls.remove(m['tool_call_id'])
        else:
            assert not open_calls
            open_calls = {c['id'] for c in m.get('tool_calls') or []}
    assert not open_calls


def test_small_budget_keeps_host_state_and_latest_whole_exchange():
    messages, counter = conversation(), Recording()
    protected = pick(messages, PROTECTED_INDICES)
    floor = chars(protected, TOOLS)
    projection = assemble_context(messages, TOOLS, floor + 503, output_reserve=500, counter=counter)
    assert projection.messages == protected
    assert_paired(projection.messages)
    manifest = projection.manifest
    assert manifest['status'] == 'retired' and manifest['dispatchable'] is True
    assert manifest['limits'] == {'context': floor + 503, 'output_reserve': 500, 'input': floor + 3}
    assert manifest['input_tokens'] == {'before': chars(messages, TOOLS), 'after': floor,
                                        'protected_floor': floor, 'tools_only': None}
    assert [(u['category'], u['first_index'], u['last_index'], u['selected'], u['reason'])
            for u in manifest['units']] == [
        ('policy', 0, 0, True, 'protected_policy'),
        ('evidence', 1, 1, True, 'protected_evidence'),
        ('memory', 2, 2, False, 'retired_for_budget'),
        ('history', 3, 4, False, 'retired_for_budget'),
        ('goal', 5, 5, True, 'protected_goal'),
        ('plan', 6, 6, True, 'protected_plan'),
        ('tool_exchange', 7, 9, False, 'retired_for_budget'),
        ('tool_exchange', 10, 11, False, 'retired_for_budget'),
        ('tool_exchange', 12, 14, True, 'latest_tool_exchange'),
        ('inbox', 15, 15, True, 'protected_inbox'),
    ]
    latest = manifest['units'][8]
    assert latest['tool_calls'] == 2 and latest['roles'] == ['assistant', 'tool', 'tool']
    assert manifest['units'][9]['markers'] == ['_pebble_inbox']
    assert manifest['messages'] == {'input': 16, 'selected': 8}
    assert manifest['omitted']['units'] == 4 and manifest['omitted']['messages'] == 8
    assert manifest['summary']['tool_exchange']['omitted_units'] == 2
    assert counter.calls == manifest['estimation']['counter_calls'] <= 2 + math.ceil(math.log2(4))


def test_older_whole_exchanges_retire_before_dialogue():
    messages = conversation()
    without_old = [m for i, m in enumerate(messages) if i not in (7, 8, 9)]
    projection = assemble_context(messages, TOOLS, chars(without_old, TOOLS) + 3, counter=chars)
    assert projection.messages == without_old
    without_exchanges = [m for i, m in enumerate(messages) if not 7 <= i <= 11]
    projection = assemble_context(messages, TOOLS, chars(without_exchanges, TOOLS) + 3, counter=chars)
    assert projection.messages == without_exchanges
    assert_paired(projection.messages)
    encoded = json.dumps(projection.messages)
    assert all(ident not in encoded for ident in ('old1', 'old2', 'mid1'))
    reasons = [u['reason'] for u in projection.manifest['units']]
    assert reasons[2] == reasons[3] == 'within_budget'


def test_manifest_is_stable_and_content_free():
    messages = conversation()
    limit = chars([m for i, m in enumerate(messages) if i not in (7, 8, 9)], TOOLS) + 3
    first = assemble_context(messages, TOOLS, limit, counter=chars).manifest
    second = assemble_context(copy.deepcopy(messages), copy.deepcopy(TOOLS), limit, counter=chars).manifest
    assert first == second
    encoded = json.dumps(first)
    for raw in (SECRET, 'Current goal', 'Plan: inspect', 'HOST INPUT', 'Earlier question', 'xxxx', 'a.py'):
        assert raw not in encoded
    assert all(u['fingerprint'].startswith('sha256:') and len(u['fingerprint']) == 39 for u in first['units'])
    assert len({u['fingerprint'] for u in first['units']}) == len(first['units'])

    edited = copy.deepcopy(messages)
    edited[3]['content'] += '!'
    third = assemble_context(edited, TOOLS, limit, counter=chars).manifest
    changed = [a['unit'] for a, b in zip(first['units'], third['units']) if a['fingerprint'] != b['fingerprint']]
    assert changed == [3]
    assert third['window_fingerprint'] != first['window_fingerprint']
    assert third['tools'] == first['tools']

    tools = copy.deepcopy(TOOLS)
    tools[0]['function']['description'] += '.'
    fourth = assemble_context(messages, tools, limit + 1, counter=chars).manifest
    assert fourth['tools']['fingerprint'] != first['tools']['fingerprint']
    assert [u['fingerprint'] for u in fourth['units']] == [u['fingerprint'] for u in first['units']]


def test_inputs_are_never_mutated_and_markers_remain_for_encoders():
    messages, tools = conversation(), copy.deepcopy(TOOLS)
    before, before_tools = copy.deepcopy(messages), copy.deepcopy(tools)
    protected = pick(messages, PROTECTED_INDICES)
    projection = assemble_context(messages, tools, chars(protected, tools) + 3, counter=chars)
    assert messages == before and tools == before_tools
    assert not any(r is m for r in projection.messages for m in messages)
    projection.messages[0]['content'] = 'changed'
    next(m for m in projection.messages if m.get('tool_calls'))['tool_calls'][0]['id'] = 'forged'
    projection.messages[-1].pop('_pebble_inbox')
    assert messages == before
    window = assemble_context(messages, tools, MAX_CONTEXT, counter=chars).messages
    markers = {k for m in window for k in m if k.startswith('_pebble_')}
    assert markers == {'_pebble_checkpoint', '_pebble_memory', '_pebble_current', '_pebble_plan', '_pebble_inbox'}


def responses_turn(ident, opaque):
    arguments = json.dumps({'file_path': 'a.py'})
    return {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': ident, 'type': 'function', 'function': {'name': 'Read', 'arguments': arguments}}],
            '_responses_output': [
                {'type': 'reasoning', 'id': 'rs_' + opaque, 'summary': [], 'encrypted_content': 'gAAAA' + opaque * 40},
                {'type': 'function_call', 'call_id': ident, 'name': 'Read', 'arguments': arguments}]}


def anthropic_turn(ident, opaque):
    return {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': ident, 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}}],
            '_anthropic_content': [
                {'type': 'thinking', 'thinking': 'Deliberation ' + opaque, 'signature': 'sig-' + opaque},
                {'type': 'tool_use', 'id': ident, 'name': 'Read', 'input': {}}]}


def gemini_turn(ident, opaque):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'},
        'extra_content': {'google': {'thought_signature': 'sig-' + opaque}}}]}


@pytest.mark.parametrize('turn, fields', [
    (responses_turn, ['_responses_output']),
    (anthropic_turn, ['_anthropic_content']),
    (gemini_turn, []),
])
def test_opaque_provider_data_stays_with_its_assistant_turn(turn, fields):
    messages = [
        {'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'goal', '_pebble_current': True},
        turn('call_old', 'opaque-old'), result('call_old', json.dumps({'source': 'x' * 900})),
        turn('call_new', 'opaque-new'), result('call_new'),
    ]
    before = copy.deepcopy(messages)
    kept = pick(messages, (0, 1, 4, 5))
    projection = assemble_context(messages, TOOLS, chars(kept, TOOLS) + 3, counter=chars)
    assert projection.messages == kept and messages == before
    assert projection.messages[2] is not messages[4]
    assert 'opaque-old' not in json.dumps(projection.messages)
    units = projection.manifest['units']
    assert [u['opaque_fields'] for u in units if u['category'] == 'tool_exchange'] == [fields, fields]
    assert units[-1]['reason'] == 'latest_tool_exchange' and not units[2]['selected']
    assert 'opaque-new' not in json.dumps(projection.manifest)


def test_reasoning_on_a_text_turn_retires_with_that_turn():
    answer = {'role': 'assistant', 'content': 'Earlier answer', '_responses_output': [
        {'type': 'reasoning', 'id': 'rs_hist', 'summary': [], 'encrypted_content': 'gAAAA' + 'r' * 900},
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Earlier answer'}]}]}
    messages = [
        {'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'Earlier question'},
        answer,
        {'role': 'user', 'content': 'goal', '_pebble_current': True},
        {'role': 'user', 'content': 'Continue'},
    ]
    kept = pick(messages, (0, 3, 4))
    projection = assemble_context(messages, [], chars(kept, []) + 3, counter=chars)
    assert projection.messages == kept
    assert 'rs_hist' not in json.dumps(projection.messages)
    history = projection.manifest['units'][1]
    assert (history['first_index'], history['last_index'], history['opaque_fields'], history['selected']) == (
        1, 2, ['_responses_output'], False)


def cyclic():
    message = {'role': 'user', 'content': []}
    message['content'].append(message)
    return message


MALFORMED = [
    ('orphan_result', 2, lambda: [result('a')]),
    ('orphan_result', 3, lambda: [{'role': 'assistant', 'content': 'text'}, result('a')]),
    ('orphan_result', 3, lambda: [calls('a'), result('b')]),
    ('duplicate_result', 4, lambda: [calls('a'), result('a'), result('a')]),
    ('incomplete_exchange', 2, lambda: [calls('a', 'b'), result('a')]),
    ('incomplete_exchange', 2, lambda: [calls('a', 'b'), result('a'),
                                        {'role': 'user', 'content': 'steer', '_pebble_inbox': 'in-1'}, result('b')]),
    ('duplicate_call', 2, lambda: [calls('a', 'a'), result('a')]),
    ('malformed_call', 2, lambda: [{'role': 'assistant', 'content': None, 'tool_calls': [{'type': 'function'}]}]),
    ('malformed_call', 2, lambda: [{'role': 'user', 'content': 'x', 'tool_calls': [{'id': 'a'}]}]),
    ('malformed_result', 3, lambda: [calls('a'), {'role': 'tool', 'content': '{}'}]),
    ('opaque_mismatch', 2, lambda: [calls('a', _responses_output=[
        {'type': 'function_call', 'call_id': 'b', 'name': 'Read', 'arguments': '{}'}]), result('a')]),
    ('opaque_mismatch', 2, lambda: [calls('a', _anthropic_content=[
        {'type': 'tool_use', 'id': 'z', 'name': 'Read', 'input': {}}]), result('a')]),
    ('opaque_mismatch', 2, lambda: [{'role': 'assistant', 'content': 'done', '_responses_output': [
        {'type': 'function_call', 'call_id': 'a', 'name': 'Read', 'arguments': '{}'}]}]),
    ('opaque_shape', 2, lambda: [{'role': 'user', 'content': 'x', '_responses_output': [{'type': 'reasoning'}]}]),
    ('opaque_shape', 2, lambda: [{'role': 'assistant', 'content': 'x', '_anthropic_content': {'type': 'text'}}]),
    ('marker_role', 3, lambda: [calls('a'), {**result('a'), '_pebble_checkpoint': True}]),
    ('marker_role', 2, lambda: [{'role': 'assistant', 'content': 'x', '_pebble_current': True}]),
    ('unknown_marker', 2, lambda: [{'role': 'user', 'content': 'x', '_pebble_priority': True}]),
    ('bad_role', 2, lambda: [{'role': 'developer', 'content': 'x'}]),
    ('not_object', 2, lambda: ['plain text']),
    ('not_json', 2, lambda: [{'role': 'user', 'content': float('nan')}]),
    ('not_json', 2, lambda: [{'role': 'user', 'content': {'a', 'b'}}]),
    ('too_deep', 2, lambda: [cyclic()]),
]


@pytest.mark.parametrize('reason, index, extra', MALFORMED,
                         ids=[f'{r}-{n}' for n, (r, _, _) in enumerate(MALFORMED)])
def test_malformed_or_ambiguous_context_is_refused(reason, index, extra):
    with pytest.raises(ContextIntegrityError) as caught:
        assemble_context(BASE + extra(), TOOLS, 100_000, counter=chars)
    assert (caught.value.reason, caught.value.index) == (reason, index)
    assert SECRET not in str(caught.value)


def test_oversized_inputs_are_refused_before_copying(monkeypatch):
    with pytest.raises(ContextIntegrityError) as caught:
        assemble_context([{'role': 'user', 'content': 'x'}] * (MAX_MESSAGES + 1), [], 4096)
    assert caught.value.reason == 'too_many_messages'
    bomb = 'x'
    for _ in range(40):
        bomb = [bomb, bomb]  # Shared references expand to 2**40 leaves.
    monkeypatch.setattr(context_manifest, 'MAX_NODES', 10_000)
    with pytest.raises(ContextIntegrityError) as caught:
        assemble_context([{'role': 'user', 'content': bomb}], [], 4096)
    assert (caught.value.reason, caught.value.index) == ('too_large', 0)


def test_impossible_protected_context_reports_a_diagnostic_manifest():
    messages = [
        {'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'old ' + 'h' * 400},
        {'role': 'user', 'content': SECRET + 'g' * 4000, '_pebble_current': True},
        *exchange('a', size=400),
        *exchange('b', size=100),
    ]
    before, counter = copy.deepcopy(messages), Recording()
    with pytest.raises(ContextBudgetExceeded, match='required context') as caught:
        assemble_context(messages, TOOLS, 900, output_reserve=100, counter=counter)
    assert messages == before
    manifest = caught.value.manifest
    assert manifest['status'] == 'exceeded' and manifest['dispatchable'] is False
    assert manifest['input_tokens'] == {
        'before': chars(messages, TOOLS), 'after': None,
        'protected_floor': chars(pick(messages, (0, 2, 5, 6)), TOOLS), 'tools_only': chars([], TOOLS)}
    assert manifest['input_tokens']['protected_floor'] > manifest['limits']['input'] == 800
    assert manifest['input_tokens']['tools_only'] <= 800
    assert manifest['estimation']['counter_calls'] == counter.calls == 3
    assert [u['category'] for u in manifest['units']] == ['policy', 'goal', 'tool_exchange']
    assert all(u['protected'] for u in manifest['units'])
    assert manifest['units_total'] == 5 and manifest['omitted']['units'] == 2
    assert manifest['window_fingerprint'] is None
    assert SECRET not in json.dumps(manifest) and SECRET not in str(caught.value)


def test_budget_diagnostic_is_bounded_for_many_protected_units():
    messages = [{'role': 'system', 'content': 'rules'}] + [
        {'role': 'user', 'content': 'HOST INPUT ' + SECRET + 'i' * 100, '_pebble_inbox': f'in-{n}'}
        for n in range(300)]
    counter = Recording()
    with pytest.raises(ContextBudgetExceeded) as caught:
        assemble_context(messages, TOOLS, 1000, counter=counter)
    manifest = caught.value.manifest
    assert manifest['units_total'] == 301
    assert manifest['units_listed'] == len(manifest['units']) == DIAGNOSTIC_UNITS
    assert manifest['summary']['inbox']['units'] == 300
    assert counter.calls == manifest['estimation']['counter_calls'] == 2
    encoded = json.dumps(manifest)
    assert len(encoded) < 150_000 and SECRET not in encoded


def test_oversized_tool_schemas_fail_before_dispatch():
    tools = [{'type': 'function', 'function': {
        'name': 'Huge', 'description': SECRET + 'd' * 8000, 'parameters': {'type': 'object'}}}]
    with pytest.raises(ContextBudgetExceeded, match='tool schemas') as caught:
        assemble_context(BASE, tools, 1000, counter=chars)
    manifest = caught.value.manifest
    assert manifest['input_tokens']['tools_only'] > manifest['limits']['input'] == 1000
    assert manifest['tools']['count'] == 1 and SECRET not in json.dumps(manifest)
    with pytest.raises(ContextBudgetExceeded) as caught:
        assemble_context(BASE, tools, 1000)
    assert caught.value.manifest['estimation']['window_method'] == 'local_estimate'


@pytest.mark.parametrize('limit, reserve', [
    (True, 0), (0, 0), (-1, 0), (4096.0, 0), ('4096', 0), (None, 0), (float('inf'), 0), (MAX_CONTEXT + 1, 0),
    (4096, -1), (4096, 4096), (4096, False), (4096, 1.5), (4096, None),
])
def test_invalid_budget_is_refused(limit, reserve):
    with pytest.raises(ValueError):
        assemble_context(BASE, TOOLS, limit, reserve, counter=chars)


@pytest.mark.parametrize('value', [-1, True, 1.5, '12', None, 10 ** 13, float('nan')])
def test_invalid_counter_values_are_refused(value):
    with pytest.raises(ContextCounterError):
        assemble_context(BASE, TOOLS, 4096, counter=lambda messages, tools: value)


def test_invalid_counter_or_tool_configuration_is_refused():
    with pytest.raises(TypeError):
        assemble_context(BASE, TOOLS, 4096, counter=42)
    with pytest.raises(ValueError):
        assemble_context(BASE, TOOLS, 4096, counter_method='tiktoken')
    with pytest.raises(ValueError):
        assemble_context(BASE, TOOLS, 4096, counter=chars, counter_method='label with spaces')
    for tools in (['not a schema'], 'tools'):
        with pytest.raises(ContextIntegrityError) as caught:
            assemble_context(BASE, tools, 4096, counter=chars)
        assert caught.value.reason == 'bad_tool'


def test_counter_that_mutates_measured_context_is_refused():
    messages = conversation()
    before = copy.deepcopy(messages)

    def mutating(window, tools):
        window[0]['content'] = 'rewritten'
        return 1

    with pytest.raises(ContextIntegrityError) as caught:
        assemble_context(messages, TOOLS, 4096, counter=mutating)
    assert caught.value.reason == 'counter_mutation' and messages == before


def long_history():
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'goal', '_pebble_current': True}]
    for n in range(400):
        messages += [{'role': 'user', 'content': f'question {n} ' + 'q' * 120},
                     {'role': 'assistant', 'content': f'answer {n} ' + 'a' * 120}]
    for n in range(1200):
        messages += exchange(f'c{n}', size=120)
    return messages


def test_counter_mutation_of_retired_history_is_also_refused():
    messages = [*BASE, {'role': 'user', 'content': 'old' * 300},
        {'role': 'user', 'content': 'latest'}]
    before = copy.deepcopy(messages)

    def counter(window, tools):
        if len(window) == 4:
            window[2]['content'] += 'mutated'
            return 9000
        return 100

    with pytest.raises(ContextIntegrityError, match='counter changed'):
        assemble_context(messages, [], 4096, counter=counter)
    assert messages == before


@pytest.mark.parametrize('injected', [True, False])
def test_long_history_uses_a_bounded_number_of_counts(injected):
    messages = long_history()
    kept = messages[:802] + messages[-600:]  # Policy, goal, all dialogue, newest 300 exchanges.
    counter = Recording() if injected else None
    measure = chars if injected else estimate_tokens
    projection = assemble_context(messages, TOOLS, measure(kept, TOOLS) + 3, counter=counter)
    assert projection.messages == kept
    assert_paired(projection.messages)
    bound = 2 + math.ceil(math.log2(1199 + 400))
    used = projection.manifest['estimation']['counter_calls']
    assert used <= bound
    assert projection.manifest['omitted']['units'] == 900 and projection.manifest['omitted']['messages'] == 1800
    if injected:
        assert counter.calls == used and counter.seen <= bound * len(messages)


def test_local_estimate_is_labelled_and_additive():
    messages = conversation()
    projection = assemble_context(messages, TOOLS, estimate_tokens(messages, TOOLS) - 1)
    manifest = projection.manifest
    assert manifest['status'] == 'retired'
    assert manifest['estimation']['window_method'] == 'local_estimate'
    assert manifest['estimation']['units_additive'] is True
    selected = sum(u['estimated_tokens'] for u in manifest['units'] if u['selected'])
    after = manifest['input_tokens']['after']
    assert after == BASE_TOKENS + manifest['tools']['estimated_tokens'] + selected
    assert after == estimate_tokens(projection.messages, TOOLS)
    assert manifest['input_tokens']['before'] == estimate_tokens(messages, TOOLS)


def test_window_that_fits_is_complete_with_one_count():
    messages, counter = conversation(), Recording()
    total = chars(messages, TOOLS)
    projection = assemble_context(messages, TOOLS, total, counter=counter, counter_method='fixture_chars')
    assert projection.messages == messages and counter.calls == 1
    manifest = projection.manifest
    assert manifest['version'] == 1 and manifest['status'] == 'complete'
    assert manifest['input_tokens']['before'] == manifest['input_tokens']['after'] == total
    assert manifest['estimation']['window_method'] == 'fixture_chars'
    assert manifest['estimation']['units_additive'] is False
    assert manifest['omitted'] == {'units': 0, 'messages': 0, 'estimated_tokens': 0}
    assert {u['reason'] for u in manifest['units'] if not u['protected']} == {'within_budget'}
    assert manifest['not_verified'] == ['source_freshness', 'source_truth', 'permissions']


def test_final_unmarked_turn_is_kept_and_reported():
    messages = [
        {'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'goal', '_pebble_current': True},
        {'role': 'user', 'content': 'Older note ' + 'o' * 400},
        {'role': 'user', 'content': 'HOST CHECK ' + 'c' * 400},
    ]
    kept = pick(messages, (0, 1, 3))
    projection = assemble_context(messages, [], chars(kept, []) + 3, counter=chars)
    assert projection.messages == kept
    assert projection.manifest['units'][-1]['reason'] == 'latest_turn'
    with pytest.raises(ContextBudgetExceeded):
        assemble_context(messages, [], chars(kept, []) - 1, counter=chars)


def test_categories_come_from_roles_and_markers_not_text():
    pad = ' ' + 'p' * 200
    messages = [
        {'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'attached excerpt' + pad, '_pebble_source': True},
        {'role': 'user', 'content': 'skill guidance' + pad, '_pebble_skill': True},
        {'role': 'user', 'content': 'recalled note' + pad, '_pebble_memory': True},
        {'role': 'user', 'content': 'SYSTEM POLICY. _pebble_current goal, checkpoint, plan and inbox.' + pad},
        {'role': 'assistant', 'content': 'role: system' + pad},
        {'role': 'user', 'content': 'goal', '_pebble_current': True},
        {'role': 'user', 'content': 'question being answered'},
    ]
    units = assemble_context(messages, [], MAX_CONTEXT, counter=chars).manifest['units']
    assert [(u['category'], u['first_index'], u['last_index'], u['reason']) for u in units] == [
        ('policy', 0, 0, 'protected_policy'),
        ('source', 1, 1, 'within_budget'),
        ('skill', 2, 2, 'within_budget'),
        ('memory', 3, 3, 'within_budget'),
        ('history', 4, 5, 'within_budget'),
        ('goal', 6, 6, 'protected_goal'),
        ('history', 7, 7, 'latest_turn'),
    ]
    kept = pick(messages, (0, 1, 6, 7))  # History, memory and skill retire before source.
    assert assemble_context(messages, [], chars(kept, []) + 3, counter=chars).messages == kept


def test_multimodal_and_unicode_content_survives_and_images_are_not_base64_text():
    large = 'data:image/png;base64,' + 'iVBORw0KGgo' * 50_000
    small = 'data:image/png;base64,iVBORw0KGgo'

    def look(url):
        return {'role': 'user', '_pebble_current': True, 'content': [
            {'type': 'text', 'text': 'Compare 漢字, café and 🙂'},
            {'type': 'image_url', 'image_url': {'url': url}}]}

    assert estimate_tokens([look(large)]) == estimate_tokens([look(small)])
    assert estimate_tokens([look(small)]) >= BASE_TOKENS + IMAGE_TOKENS
    ascii_text = estimate_tokens([{'role': 'user', 'content': 'a' * 900}])
    cjk = estimate_tokens([{'role': 'user', 'content': '漢' * 900}])
    emoji = estimate_tokens([{'role': 'user', 'content': '🙂' * 900}])
    assert ascii_text < cjk < emoji and cjk >= BASE_TOKENS + 900

    messages = [
        {'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'Earlier ' + 'h' * 3000},
        look(large),
        {'role': 'user', 'content': 'Lone surrogate \ud800 and 漢字 stay intact'},
    ]
    before = copy.deepcopy(messages)
    kept = pick(messages, (0, 2, 3))
    projection = assemble_context(messages, [], estimate_tokens(kept) + 3)
    assert projection.messages == kept and messages == before
    goal = projection.manifest['units'][2]
    assert (goal['category'], goal['images'], goal['selected']) == ('goal', 1, True)
    assert goal['estimated_tokens'] >= IMAGE_TOKENS
    encoded = json.dumps(projection.manifest)
    assert 'iVBOR' not in encoded and 'Earlier' not in encoded


def test_human_constraints_unit_is_protected_and_integrity_checked():
    import hashlib
    content = 'HOST HUMAN CONSTRAINTS: {"turns":[{"input_id":"m1","text":"Never edit prod.yaml ' + SECRET + '"}]}'
    unit = {'role': 'user', 'name': 'pebble_human_constraints', 'content': content,
            '_pebble_constraints': 'sha256:' + hashlib.sha256(content.encode()).hexdigest()}
    messages = conversation()
    messages.insert(2, unit)
    protected = [m for m in messages if m is unit or messages.index(m) in [i + (i >= 2) for i in PROTECTED_INDICES]]
    projection = assemble_context(messages, TOOLS, chars(protected, TOOLS) + 3, counter=chars)
    assert projection.messages == protected
    record = next(u for u in projection.manifest['units'] if u['category'] == 'constraints')
    assert (record['protected'], record['selected'], record['reason']) == (True, True, 'protected_constraints')
    assert record['markers'] == ['_pebble_constraints']
    assert projection.manifest['summary']['constraints'] == {
        'units': 1, 'messages': 1, 'selected_units': 1, 'omitted_units': 0,
        'selected_estimated_tokens': record['estimated_tokens'], 'omitted_estimated_tokens': 0}
    assert 'prod.yaml' not in json.dumps(projection.manifest)
    # Too small for the protected unit: refuse rather than retire it.
    with pytest.raises(ContextBudgetExceeded):
        assemble_context(messages, TOOLS, chars(protected, TOOLS) - 1, counter=chars)
    for bad, reason in (({**unit, 'content': content[:-5]}, 'constraints_integrity'),
                        ({**unit, 'role': 'system'}, 'constraints_integrity'),
                        ({**unit, '_pebble_constraints': False}, 'constraints_integrity'),
                        ({**unit, 'role': 'assistant'}, 'marker_role')):
        with pytest.raises(ContextIntegrityError) as caught:
            assemble_context(BASE + [bad], TOOLS, 100_000, counter=chars)
        assert caught.value.reason == reason and SECRET not in str(caught.value)
