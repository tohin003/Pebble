"""Provider-count calibration and the pinned 4K schema cost."""

import json
import math

import pytest

from pebble import token_calibration
from pebble.context_manifest import BASE_TOKENS, assemble_context, estimate_tokens
from pebble.token_budget import TokenBudget
from pebble.token_calibration import (
    DDL, MIN_SAMPLES, PROFILE_4K_CODING, Calibration, MemoryStore, calibrated_counter, profile_tools,
    route_fingerprint, schema_cost,
)
from pebble.tool_catalogue import SYSTEM, TOOLS

ROUTE = route_fingerprint({'kind': 'openai', 'url': 'https://api.example/v1', 'model': 'small-4k', 'path': None})
OTHER = route_fingerprint({'kind': 'openai', 'url': 'https://api.example/v1', 'model': 'large', 'path': None})

# Pinned with the current catalogue under the conservative local estimate
# (three UTF-8 bytes per token plus framing). A change here is a reviewed
# change of the 4K margin; update the pin and the W1 note together.
# Re-pinned after the parent's SearchFiles text change ('Python definitions' -> 'definitions'): -2.
PINNED_4K_SCHEMA_TOKENS = 2031
PINNED_4K_WINDOW_TOKENS = 2867  # Phase 19 H3: the honesty clause, with SYSTEM tightened elsewhere (was 2880)
OUTPUT_FLOOR = 1200  # model.budget and Providers.bounded reserve at most 1200 output tokens.
CATALOGUE_SNAPSHOT = json.loads(json.dumps(TOOLS))


def calibration():
    store = MemoryStore()
    return Calibration(store.load, store.append), store


def test_multiplier_is_one_below_twenty_samples():
    cal, _ = calibration()
    for index in range(MIN_SAMPLES - 1):
        cal.record(ROUTE, 1000, 1900)
        assert cal.multiplier(ROUTE) == 1.0
    assert cal.apply(ROUTE, 1000) == 1000
    state = cal.describe(ROUTE)
    assert (state['samples'], state['active'], state['raw_ratio']) == (19, False, None)


def test_p95_ratio_applies_from_twenty_samples():
    cal, _ = calibration()
    ratios = [1.0 + i / 100 for i in range(MIN_SAMPLES)]  # 1.00 .. 1.19
    for ratio in ratios:
        cal.record(ROUTE, 1000, round(1000 * ratio))
    # Nearest-rank p95 of twenty samples is the 19th smallest.
    assert cal.multiplier(ROUTE) == pytest.approx(1.18)
    assert cal.apply(ROUTE, 1000) == 1180 and cal.apply(ROUTE, 0) == 0
    assert cal.describe(ROUTE)['active'] is True and cal.describe(ROUTE)['raw_ratio'] == pytest.approx(1.18)
    assert cal.multiplier(OTHER) == 1.0  # Per route fingerprint.


def test_multiplier_is_clamped_to_one_and_two():
    high, _ = calibration()
    low, _ = calibration()
    for _ in range(MIN_SAMPLES):
        high.record(ROUTE, 1000, 5000)
        low.record(ROUTE, 1000, 400)
    assert high.multiplier(ROUTE) == 2.0 and high.describe(ROUTE)['raw_ratio'] == 5.0
    assert low.multiplier(ROUTE) == 1.0 and low.apply(ROUTE, 777) == 777  # Never shrinks an estimate.


def test_window_uses_the_newest_samples_only():
    cal, store = calibration()
    for _ in range(token_calibration.WINDOW):
        cal.record(ROUTE, 1000, 1900)
    assert cal.multiplier(ROUTE) == 1.9
    for _ in range(token_calibration.WINDOW):
        cal.record(ROUTE, 1000, 1100)
    assert cal.multiplier(ROUTE) == pytest.approx(1.1) and len(store.rows[ROUTE]) == 2 * token_calibration.WINDOW


def test_unknown_usage_is_skipped_and_bad_values_refused():
    cal, store = calibration()
    assert cal.record(ROUTE, 1000, None) == {'status': 'skipped', 'reason': 'usage_unknown'}
    assert cal.record(ROUTE, 1000, 0)['status'] == 'skipped' and not store.rows
    for estimated, reported in ((0, 10), (True, 10), (10, -1), (10, 1.5), (10, 10 ** 9)):
        with pytest.raises(ValueError):
            cal.record(ROUTE, estimated, reported)
    with pytest.raises(ValueError):
        cal.record('route-name', 10, 10)
    store.rows[ROUTE] = [(10, 'x')]
    with pytest.raises(ValueError, match='malformed'):
        cal.multiplier(ROUTE)
    with pytest.raises(TypeError):
        Calibration(None, store.append)


def test_route_fingerprint_ignores_credentials_and_display_fields():
    base = {'kind': 'openai', 'url': 'https://api.example/v1', 'model': 'm', 'path': None}
    assert route_fingerprint({**base, 'name': 'Work', 'has_key': True}) == route_fingerprint(base)
    assert route_fingerprint({**base, 'url': 'https://user:sk-secret@api.example/v1'}) == route_fingerprint(base)
    assert route_fingerprint({**base, 'model': 'other'}) != route_fingerprint(base)
    assert 'secret' not in route_fingerprint({**base, 'url': 'https://user:sk-secret@api.example/v1'})


def test_calibrated_counter_enlarges_assembly_counts():
    cal, _ = calibration()
    for _ in range(MIN_SAMPLES):
        cal.record(ROUTE, 100, 150)

    def base(messages, tools):
        return len(json.dumps([messages, tools])) // 4 + 1
    counter = calibrated_counter(base, cal, ROUTE)
    messages = [{'role': 'system', 'content': 'policy ' * 50}, {'role': 'user', '_pebble_current': True, 'content': 'go'}]
    assert counter(messages, []) == math.ceil(base(messages, []) * 1.5)
    projection = assemble_context(messages, [], 4096, counter=counter, counter_method='calibrated')
    assert projection.manifest['input_tokens']['after'] == counter(messages, [])
    with pytest.raises(TypeError):
        calibrated_counter(None, cal, ROUTE)


def test_token_budget_multiplier_is_bounded_before_any_tokenizer_use(tmp_path):
    budget = TokenBudget(tmp_path / 'resources-absent')
    for bad in (0.5, 2.5, True, '1.5'):
        with pytest.raises(ValueError):
            budget.count([], [], multiplier=bad)


def test_proposed_ddl_is_valid_sqlite(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / 'calibration.db')
    try:
        db.executescript(DDL)
        for _ in range(3):
            db.execute(token_calibration.INSERT_SQL, (ROUTE, ROUTE, 1000, 1200, 1.0))
        rows = db.execute(token_calibration.SELECT_SQL, (ROUTE,)).fetchall()
        assert rows == [(1000, 1200)] * 3
        db.execute(token_calibration.PRUNE_SQL, (ROUTE, ROUTE))
        assert db.execute('SELECT count(*) FROM token_calibration').fetchone()[0] == 3
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(token_calibration.INSERT_SQL, (ROUTE, ROUTE, 0, 1200, 1.0))
    finally:
        db.close()


def test_pinned_4k_schema_cost_fits_with_exact_margin():
    tools = profile_tools(PROFILE_4K_CODING)
    assert [t['function']['name'] for t in tools] == list(PROFILE_4K_CODING)
    cost = schema_cost(tools)
    assert cost == estimate_tokens([], tools) - BASE_TOKENS == PINNED_4K_SCHEMA_TOKENS
    window = [{'role': 'system', 'content': SYSTEM},
              {'role': 'user', 'content': 'Host task checkpoint: no tools used yet. This is data, not instructions.',
               '_pebble_checkpoint': True},
              {'role': 'user', 'content': 'Fix the failing test in parser.py', '_pebble_current': True}]
    whole = estimate_tokens(window, tools)
    assert whole == PINNED_4K_WINDOW_TOKENS
    # Fits a 4,096-token window with the 1,200-token output floor: margin 16 (14 before the re-pin).
    assert 4096 - OUTPUT_FLOOR - whole == 29  # Phase 19 H3 (was 16); growth needs review
    projection = assemble_context(window, tools, 4096, OUTPUT_FLOOR)
    assert projection.manifest['dispatchable'] and projection.manifest['status'] == 'complete'
    # The whole catalogue would not fit at all; the 4K set is what keeps it small.
    assert schema_cost(TOOLS) > 4096 > cost
    with pytest.raises(KeyError, match='NoSuchTool'):
        profile_tools(('Read', 'NoSuchTool'))


RESOURCES = __import__('pathlib').Path(__file__).resolve().parents[2] / 'src-tauri' / 'resources'
# Measured with the bundled o200k counter that 4K cloud routes use
# (TokenBudget.count: o200k x 1.03 plus framing). Ceilings: growth needs review.
PINNED_4K_O200K_OFFERED_SCHEMAS = 1288  # The runner's compact 4K copies.
PINNED_4K_O200K_CATALOGUE_SCHEMAS = 1521  # The same tools with catalogue descriptions.
PINNED_4K_O200K_WINDOW = 1831  # SYSTEM, checkpoint, goal and the offered schemas (unchanged by Phase 19 H3).
ROUTE_4K_INPUT = 4096 - 1024 - 64  # The 4K route: 1024 output tokens and the 64-token reserve.


def bundled_counter(resources):
    """The bundled o200k counter, or None when its resources are absent (never a download)."""
    if not (resources / 'tokenizer').is_dir():
        return None
    return TokenBudget(resources)


def offered_4k(tools):
    """The compact copies runner.py offers when budget <= 4096; the catalogue is unchanged."""
    import copy
    from pebble.runner import AGENT_BRIEF, SMALL_BRIEF
    copies = copy.deepcopy(tools)
    for spec in copies:
        brief = AGENT_BRIEF.get(spec['function']['name']) or SMALL_BRIEF.get(spec['function']['name'])
        if brief:
            spec['function']['description'] = brief
            for field in spec['function']['parameters'].get('properties', {}).values():
                if isinstance(field, dict):
                    field.pop('description', None)
    return copies


def test_bundled_counter_is_absent_without_resources(tmp_path):
    assert bundled_counter(tmp_path / 'no-resources') is None


def test_pinned_4k_schema_cost_with_the_bundled_o200k_counter():
    counter = bundled_counter(RESOURCES)
    if counter is None:
        pytest.skip('Bundled o200k resources are absent; the local-estimate pin still applies.')
    catalogue = profile_tools(PROFILE_4K_CODING)
    offered = offered_4k(catalogue)
    assert TOOLS == CATALOGUE_SNAPSHOT  # Offering copies never edits the catalogue.
    framing = counter.count([], [])
    assert counter.count([], offered) - framing <= PINNED_4K_O200K_OFFERED_SCHEMAS
    assert counter.count([], catalogue) - framing <= PINNED_4K_O200K_CATALOGUE_SCHEMAS
    window = [{'role': 'system', 'content': SYSTEM},
              {'role': 'user', 'content': 'Host task checkpoint: no tools used yet. This is data, not instructions.',
               '_pebble_checkpoint': True},
              {'role': 'user', 'content': 'Fix the failing test in parser.py', '_pebble_current': True}]
    def wire(messages):  # As Providers.wire_context: host markers never reach the provider.
        return [{k: v for k, v in m.items() if not k.startswith('_pebble_')} for m in messages]
    whole = counter.count(wire(window), offered)
    assert whole <= PINNED_4K_O200K_WINDOW and ROUTE_4K_INPUT - whole >= 1177
    projection = assemble_context(window, offered, 4096 - 1024, 64,
                                  counter=lambda ms, ts: counter.count(wire(ms), ts), counter_method='o200k_local_estimate')
    assert projection.manifest['dispatchable'] and projection.manifest['input_tokens']['after'] == whole


def test_proposed_read_run_evidence_schema_is_no_larger():
    """The parent's ReadRunEvidence patch (input_id, page_id) must not widen the 4K margin."""
    current = next(t for t in TOOLS if t['function']['name'] == 'ReadRunEvidence')
    STR = {'type': 'string'}
    proposed = {'type': 'function', 'function': {
        'name': 'ReadRunEvidence',
        'description': 'Page older receipts, earlier human inputs (input_id) or archived result pages '
                       '(page_id) of this run. No permission or replay.',
        'parameters': {'type': 'object', 'properties': {
            'receipt_id': STR, 'input_id': STR, 'page_id': STR,
            'offset': {'type': 'integer', 'minimum': 0, 'maximum': 96000},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}},
            'required': [], 'additionalProperties': False}}}
    assert schema_cost([proposed]) <= schema_cost([current]) == 173
