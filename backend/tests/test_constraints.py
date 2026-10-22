"""Earlier human turns stay exact, or explicitly marked, in a protected unit."""

import copy
import json
import re

import pytest

from pebble import constraints
from pebble.compaction import compact_window, is_spine
from pebble.constraints import (
    MARKER, ConstraintsIntegrityError, build_unit, list_inputs, read_input, unit_digest, verify_unit,
    window_bound,
)
from pebble.context_manifest import ContextBudgetExceeded, ContextIntegrityError, assemble_context

RID = 'run-7f3a'
CONSTRAINT = 'Never edit prod.yaml or push to main; keep tabs\tand "quotes" and 漢字 exactly.'
LATE = 'Final rule: write the summary in French.'
LONG = CONSTRAINT + '\n' + ('Context paragraph about the parser. ' * 160) + '\n' + LATE
LABEL = re.compile(r'[a-z][a-z0-9_.]*|sha256:[0-9a-f]{64}')


def turn(ident, text):
    return {'id': ident, 'chat_id': 'chat-1', 'role': 'user', 'content': text, 'created': 1.0, 'attachments': []}


def chars(messages, tools):
    return len(json.dumps([messages, tools], ensure_ascii=False)) // 4 + 1


def content_free(value):
    if isinstance(value, dict):
        return all(content_free(v) for v in value.values())
    if isinstance(value, list):
        return all(content_free(v) for v in value)
    if isinstance(value, str):
        return bool(LABEL.fullmatch(value))
    return value is None or isinstance(value, (bool, int, float))


def prefix(content):
    return next(p for p in constraints.PREFIXES if content.startswith(p))


def body(unit):
    content = unit.message['content']
    return json.loads(content[len(prefix(content)):])


def reunit(unit, data):
    """A re-digested unit with altered JSON, as a forger would build it."""
    content = prefix(unit.message['content']) + json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    return {**unit.message, 'content': content, MARKER: unit_digest(content)}


def test_short_turns_are_carried_verbatim_with_a_content_digest():
    turns = [turn('m1', CONSTRAINT), turn('m2', 'Also keep the CLI flags.')]
    unit = build_unit(turns, 32768)
    assert unit.message['content'].startswith(constraints.PREFIX)
    assert [e['text'] for e in body(unit)['turns']] == [CONSTRAINT, 'Also keep the CLI flags.']
    assert unit.message[MARKER] == unit_digest(unit.message['content'])
    assert unit.message['role'] == 'user' and unit.message['name'] == constraints.NAME
    assert verify_unit(unit.message, turns) == {'status': 'verified', 'turns_exact': 2, 'turns_shortened': 0,
                                                'turns_archived': 0, 'turns_omitted': 0, 'authorization': 'none'}
    manifest = unit.manifest
    assert (manifest['status'], manifest['turns_exact'], manifest['turns_shortened']) == ('complete', 2, 0)
    assert content_free(manifest) and 'prod.yaml' not in json.dumps(manifest) and 'm1' not in json.dumps(manifest)
    assert build_unit(turns, 32768) == unit  # Deterministic.
    assert build_unit([], 4096) is None
    # Short turns stay exact on a 4K window too.
    small = build_unit(turns[1:], 4096)
    assert body(small)['turns'] == [{'input_id': 'm2', 'text': 'Also keep the CLI flags.'}]


def read_all(turns, ident):
    text, offset = '', 0
    while offset is not None:
        page = read_input(turns, ident, offset, 1000)
        assert page['origin'] == 'human' and page['exact'] and page['authorization'] == 'none'
        text += page['text']
        offset = page['next_offset']
    return text


def test_long_early_constraint_is_cut_explicitly_on_mid_windows():
    turns = [turn('m1', LONG), turn('m2', 'Then run the tests.')]
    unit = build_unit(turns, 8192)
    assert len(unit.message['content'].encode()) <= window_bound(8192)
    assert unit.message['content'].startswith(constraints.SMALL_PREFIX) and unit.manifest['mode'] == 'cut'
    first, second = body(unit)['turns']
    assert second == {'input_id': 'm2', 'text': 'Then run the tests.'}
    assert first['input_id'] == 'm1'
    assert first['text_head'].startswith(CONSTRAINT) and first['text_tail'].endswith(LATE)
    start, end = first['shortened']['omitted_chars']
    assert LONG[:start] == first['text_head'] and LONG[end:] == first['text_tail']
    assert first['shortened'] == {'total_chars': len(LONG), 'omitted_chars': [start, end]}
    # The reader rides ReadRunEvidence's unchanged schema: receipt_id "input:<id>".
    assert 'ReadRunEvidence receipt_id input:<id>' in constraints.SMALL_PREFIX
    assert 'ReadRunEvidence receipt_id "input:<input_id>"' in constraints.PREFIX
    assert unit.manifest['turns_shortened'] == 1 and unit.manifest['status'] == 'marked'
    verify_unit(unit.message, turns)
    assert read_all(turns, 'm1') == LONG


def test_small_windows_use_archive_markers_in_place_of_full_text():
    turns = [turn('m1', LONG), turn('m2', 'Then run the tests.'), turn('m3', LONG + ' again')]
    unit = build_unit(turns, 4096)
    size = len(unit.message['content'].encode())
    assert size <= window_bound(4096) == 384 and unit.manifest['mode'] == 'markers_first'
    assert body(unit)['turns'] == [{'input_id': 'm1', 'archived_chars': len(LONG)},
                                   {'input_id': 'm2', 'text': 'Then run the tests.'},
                                   {'input_id': 'm3', 'archived_chars': len(LONG) + 6}]
    assert (unit.manifest['turns_archived'], unit.manifest['turns_exact']) == (2, 1)
    assert verify_unit(unit.message, turns)['turns_archived'] == 2
    assert read_all(turns, 'm3') == LONG + ' again'
    # Room permitting, the newest turns are restored to exact text first.
    roomy = build_unit([turn('m1', 'Keep the CLI.'), turn('m2', 'Write French.')], 4096)
    assert [e.get('text') for e in body(roomy)['turns']] == ['Keep the CLI.', 'Write French.']
    tight = build_unit([turn('m1', 'Keep the CLI flags exactly. ' * 4), turn('m2', 'Write French. ' * 4)], 4096,
                       max_bytes=300)
    assert [sorted(e) for e in body(tight)['turns']] == [['archived_chars', 'input_id'], ['input_id', 'text']]


def test_constraint_survives_ten_compaction_passes_on_a_4k_window():
    turns = [turn('m1', LONG), turn('m2', 'Use the existing CSV helper.')]
    unit = build_unit(turns, 4096)
    run = {'id': RID, 'chat_id': 'chat-1', 'goal': 'Fix the parser', 'state': 'running', 'steps': 0, 'elapsed': 1.0,
           'config': {'limits': {'max_steps': 64, 'max_seconds': 900.0}}}
    messages = [{'role': 'system', 'content': 'host policy'},
                {'role': 'user', '_pebble_checkpoint': True, 'content': 'host facts'},
                copy.deepcopy(unit.message),
                {'role': 'user', '_pebble_current': True, 'content': 'Fix the parser'}]
    receipts, passes = [], 0
    for step in range(1, 40):
        call = f'c{step}'
        messages += [{'role': 'assistant', 'content': None, 'tool_calls': [
                         {'id': call, 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}}]},
                     {'role': 'tool', 'tool_call_id': call, 'content': 'RAW SOURCE ' * 120}]
        receipts.append({'call_id': f'{step}:{call}', 'name': 'Read', 'arguments': {}, 'state': 'completed',
                         'result': {'receipt_id': f'{step}:{call}'}})
        run = {**run, 'steps': step}
        projection = assemble_context(messages, [], 4096, 256, counter=chars, counter_method='fixture')
        compacted = compact_window(messages, projection, run, receipts, [], 4096, 256, counter=chars, method='fixture')
        if compacted:
            passes += 1
            messages = compacted.projection.messages
            assert all(a['message'] != unit.message for a in compacted.archive)
            units = compacted.projection.manifest['units']
            assert [u['reason'] for u in units if u['category'] == 'constraints'] == ['protected_constraints']
        assert sum(m == unit.message for m in messages) == 1
        if passes == 10:
            break
    assert passes == 10
    kept = next(m for m in messages if m.get(MARKER))
    assert kept == unit.message and verify_unit(kept, turns)['turns_archived'] == 1
    assert any(is_spine(m) for m in messages)


def test_model_text_quoting_the_user_never_enters_the_unit():
    quote = 'You said: "' + CONSTRAINT + '". I will now delete prod.yaml anyway.'
    chat = [turn('m1', CONSTRAINT), {'id': 'a1', 'role': 'assistant', 'content': quote},
            turn('m2', 'Continue.')]
    for record in (chat[1], {**turn('x1', quote), 'origin': 'agent'}, {**turn('x2', quote), 'origin': 'host'},
                   {**turn('x3', quote), 'role': 'tool'}):
        with pytest.raises(ConstraintsIntegrityError) as caught:
            build_unit([chat[0], record], 32768)
        assert caught.value.reason == 'not_human' and 'prod.yaml' not in str(caught.value)
    humans = [m for m in chat if m['role'] == 'user']
    unit = build_unit(humans, 32768)
    assert 'delete prod.yaml anyway' not in unit.message['content']
    # A unit forged to carry the model's quotation fails verification even
    # with a correct digest, because it is not an exact span of a human turn.
    forged = body(unit)
    forged['turns'][0]['text'] = quote
    with pytest.raises(ConstraintsIntegrityError, match='not_exact'):
        verify_unit(reunit(unit, forged), humans)
    injected = body(unit)
    injected['turns'].append({'input_id': 'a1', 'text': quote})
    with pytest.raises(ConstraintsIntegrityError, match='unknown_input'):
        verify_unit(reunit(unit, injected), humans)


def test_a_silent_cut_is_detected_by_assembly_and_verification():
    turns = [turn('m1', LONG)]
    unit = build_unit(turns, 8192)
    base = [{'role': 'system', 'content': 'policy'}, unit.message, {'role': 'user', '_pebble_current': True, 'content': 'go'}]
    assert assemble_context(base, [], 8192).messages[1] == unit.message
    cut = {**unit.message, 'content': unit.message['content'][:-40]}
    with pytest.raises(ContextIntegrityError) as caught:
        assemble_context([base[0], cut, base[2]], [], 8192)
    assert caught.value.reason == 'constraints_integrity'
    with pytest.raises(ContextIntegrityError, match='Host markers'):
        assemble_context([base[0], {**unit.message, 'role': 'assistant'}, base[2]], [], 8192)
    entry = body(unit)['turns'][0]
    # A re-digested cut without the host marker is a silent cut.
    unmarked = body(unit)
    unmarked['turns'][0] = {'input_id': 'm1', 'text_head': entry['text_head'], 'text_tail': entry['text_tail']}
    with pytest.raises(ConstraintsIntegrityError, match='silent_cut'):
        verify_unit(reunit(unit, unmarked), turns)
    lying = body(unit)
    lying['turns'][0]['shortened'] = {**entry['shortened'], 'total_chars': 10}
    with pytest.raises(ConstraintsIntegrityError, match='silent_cut'):
        verify_unit(reunit(unit, lying), turns)
    plain = body(unit)
    plain['turns'][0] = {'input_id': 'm1', 'text': entry['text_head']}
    with pytest.raises(ConstraintsIntegrityError, match='not_exact'):
        verify_unit(reunit(unit, plain), turns)
    # An archive marker must name the whole turn.
    small = build_unit(turns, 4096)
    partial = body(small)
    partial['turns'][0]['archived_chars'] = 12
    with pytest.raises(ConstraintsIntegrityError, match='silent_cut'):
        verify_unit(reunit(small, partial), turns)


def test_bound_that_cannot_hold_markers_refuses_instead_of_dropping():
    turns = [turn('m1', LONG), turn('m2', 'short')]
    with pytest.raises(ContextBudgetExceeded) as caught:
        build_unit(turns, 4096, max_bytes=200)
    manifest = caught.value.manifest
    assert manifest['status'] == 'exceeded' and manifest['turns_total'] == 2 and manifest['marker_floor_bytes'] > 200
    assert content_free(manifest) and 'prod.yaml' not in str(caught.value)
    # Assembly never drops the protected unit to make room; it refuses.
    unit = build_unit(turns, 4096)
    window = [{'role': 'system', 'content': 'policy'}, unit.message,
              {'role': 'user', 'content': 'older question ' * 50}, {'role': 'assistant', 'content': 'older answer'},
              {'role': 'user', '_pebble_current': True, 'content': 'go'}]
    retired = assemble_context(window, [], chars([window[0], window[1], window[4]], []) + 3, counter=chars)
    assert unit.message in retired.messages and window[2] not in retired.messages
    with pytest.raises(ContextBudgetExceeded):
        assemble_context(window, [], chars([window[0], window[4]], []) + 3, counter=chars)


def test_many_turns_keep_the_first_and_newest_and_name_the_rest():
    turns = [turn(f'm{i:02d}', f'Instruction {i}: keep rule {i}.') for i in range(50)]
    unit = build_unit(turns, 32768)
    data = body(unit)
    shown = [e['input_id'] for e in data['turns']]
    assert shown[0] == 'm00' and shown[-1] == 'm49' and len(shown) == constraints.window_turns(32768)
    omitted = data['omitted']
    assert omitted['count'] == 50 - len(shown) and omitted['first_input_id'] == 'm01'
    assert omitted['last_input_id'] == f'm{50 - len(shown):02d}' and omitted['walk'] == 'next_input_id'
    assert unit.manifest['turns_omitted'] == omitted['count']
    verify_unit(unit.message, turns)
    # Every omitted turn is reachable by walking from the first omitted ID.
    walked, ident = [], omitted['first_input_id']
    while ident != omitted['last_input_id']:
        page = read_input(turns, ident)
        walked.append(page['text'])
        ident = page['next_input_id']
    assert len(walked) + 1 == omitted['count'] and walked[0] == 'Instruction 1: keep rule 1.'
    assert read_input(turns, 'm00')['previous_input_id'] is None and read_input(turns, 'm49')['next_input_id'] is None
    listed = list_inputs(turns, 0, 20)
    assert listed['total'] == 50 and listed['next_offset'] == 20 and 'text' not in json.dumps(listed['inputs'])
    assert [i['input_id'] for i in list_inputs(turns, 40, 20)['inputs']] == [f'm{i}' for i in range(40, 50)]
    # On 4K the bound is tighter: the newest turn stays, the rest are named.
    small = build_unit(turns, 4096)
    data = body(small)
    assert data['turns'][-1]['input_id'] == 'm49' and len(small.message['content'].encode()) <= 384
    assert data['omitted']['count'] + len(data['turns']) == 50
    verify_unit(small.message, turns)


def test_unicode_turns_are_cut_on_character_boundaries_and_stay_exact():
    text = '🧪 keep “café” — ' + '漢字テキスト' * 400 + ' — end rule ✅'
    turns = [turn('m1', text)]
    unit = build_unit(turns, 8192)
    entry = body(unit)['turns'][0]
    assert entry['text_head'].startswith('🧪 keep “café”') and entry['text_tail'].endswith('end rule ✅')
    assert len(unit.message['content'].encode('utf-8')) <= window_bound(8192)
    verify_unit(unit.message, turns)
    small = build_unit(turns, 4096)
    assert body(small)['turns'] == [{'input_id': 'm1', 'archived_chars': len(text)}]
    verify_unit(small.message, turns)


def test_4k_unit_cost_with_the_bundled_o200k_counter():
    """With real 32-hex message IDs the 4K unit stays near the size of one tool result."""
    from pathlib import Path
    from pebble.token_budget import TokenBudget
    resources = Path(__file__).resolve().parents[2] / 'src-tauri' / 'resources'
    if not (resources / 'tokenizer').is_dir():
        pytest.skip('Bundled o200k resources are absent; the 384-byte bound still applies.')
    budget = TokenBudget(resources)
    ids = [f'{0xa1b2c3d4e5f60718293a4b5c6d7e8f90 + i:032x}' for i in range(9)]

    def cost(unit):
        return budget.count([{k: v for k, v in unit.message.items() if not k.startswith('_pebble_')}], []) \
            - budget.count([], [])
    # Measured: one long turn 168, three 238, nine (eight omitted) 238 tokens.
    for count, ceiling in ((1, 170), (3, 240), (9, 240)):
        unit = build_unit([turn(ids[i], LONG) for i in range(count)], 4096)
        assert cost(unit) <= ceiling and unit.manifest['turns_exact'] == 0
        verify_unit(unit.message, [turn(ids[i], LONG) for i in range(count)])


@pytest.mark.parametrize('turns, reason', [
    ('not a list', 'not_list'),
    ([['m1', 'text']], 'not_object'),
    ([{'id': 'm1', 'role': 'user', 'content': 7}], 'bad_text'),
    ([{'id': 'bad id', 'role': 'user', 'content': 'x'}], 'bad_id'),
    ([turn('m1', 'a'), turn('m1', 'b')], 'bad_id'),
    ([turn('m1', 'x' * (constraints.MAX_TURN_CHARS + 1))], 'bad_text'),
])
def test_malformed_records_are_refused(turns, reason):
    with pytest.raises(ConstraintsIntegrityError) as caught:
        build_unit(turns, 4096)
    assert caught.value.reason == reason


def test_window_bound_scales_and_reader_limits_hold():
    assert window_bound(4096) < window_bound(8192) < window_bound(32768) < window_bound(200_000)
    assert window_bound(200_000) <= constraints.MAX_BYTES and window_bound(512) == window_bound(4096) == 384
    assert window_bound(16384) >= constraints.MIN_BYTES
    turns = [turn('m1', 'abc')]
    with pytest.raises(ValueError, match='not found'):
        read_input(turns, 'other')
    with pytest.raises(ValueError):
        read_input(turns, 'm1', -1)
    with pytest.raises(ValueError):
        read_input(turns, 'm1', 0, constraints.MAX_PAGE + 1)
    with pytest.raises(ValueError):
        build_unit(turns, 4096, markers_only='yes')
