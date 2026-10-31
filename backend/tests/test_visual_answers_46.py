"""Phase 46 (1.9.29, D195, w25-genui-b): visual answers, layer B.

- The catalog's kinds (compare, metrics, timeline, record) and the chart and diagram variants: what artifact_schema and
  artifacts.validate accept and refuse, the generated-UI authority rules in each (REJECTED_KEYS, REJECTED_TEXT, SECRET),
  and pebble-ui-spec v1 agreeing with them.
- The offer (artifact_schema.visual_offer): the catalog reaches the model only when the person asks for a visual in
  their own words (D216/D217: never on its own for data, comparisons or research while "Visual answers" is "ask", the
  default); it is decided from the conversation's human turns, so it changes at most once and then stays.
- Scripted models through the real App and runner: a request without a visual ask is byte-identical to the request
  before this change (the catalog forced off), requests of the benchmark's Git, memory and recovery kinds get no offer,
  the tokens a visual ask adds are measured, and the offer holds steady across a conversation's messages.
"""

import copy
import json
import re
import statistics
import time
from types import SimpleNamespace

import jsonschema
import pytest

from pebble import artifact_schema, artifacts, runner, settings_file, ui_spec
from pebble.app import App
from pebble.artifact_schema import (CATALOG_BRIEF, CATALOG_KINDS, CATALOG_SCHEMAS, LEGACY_KINDS, NEW_KINDS, SCHEMAS,
                                    offered_brief, offered_kinds, schema_error, visual_ask, visual_offer)
from pebble.permissions import DEFAULT
from pebble.providers import Providers
from test_high_fd_waits_21 import close_guard_databases
from test_reasoning_summary_22 import answer, no_network  # noqa: F401 (no_network is an autouse fixture)

VALID = {
    'compare': {'options': [{'id': 'a', 'label': 'Plan A', 'summary': 'Cheaper'}, {'id': 'b', 'label': 'Plan B'}],
                'criteria': [{'label': 'Price', 'values': [10, 14.5]}, {'label': 'Offline', 'values': [True, False]},
                             {'label': 'Support', 'values': ['Email', 'Phone and email']}],
                'recommended': 'a', 'caption': 'Monthly prices'},
    'metrics': {'tiles': [{'label': 'Passed', 'value': 41, 'delta': 3, 'better': 'up'},
                          {'label': 'Rate', 'value': 0.97, 'format': 'percent'}], 'caption': 'This week'},
    'timeline': {'events': [{'date': '2024', 'title': 'Started'}, {'date': '2024-02-29', 'title': 'Leap day',
                                                                   'tone': 'accent', 'detail': 'A real date'}]},
    'record': {'fields': [{'label': 'Name', 'value': 'Asha'}, {'label': 'Desk', 'value': 412}], 'subtitle': 'New starter'},
}


def validated(kind, data):
    return artifacts.validate(kind, json.loads(json.dumps(data)))


def spec_errors(kind, data):
    schema = ui_spec.schema()
    return list(jsonschema.Draft202012Validator({**schema['$defs']['native.' + kind], '$defs': schema['$defs']})
                .iter_errors(data))


# -- the catalog's schemas --------------------------------------------------------------------------------------------

@pytest.mark.parametrize('kind', CATALOG_KINDS)
def test_each_catalog_kind_accepts_its_shape_and_the_spec_agrees(kind):
    assert validated(kind, VALID[kind]) == json.loads(json.dumps(VALID[kind]))
    assert not spec_errors(kind, VALID[kind])
    assert kind in artifact_schema.NATIVE_KINDS and kind in ui_spec.kinds() and kind not in SCHEMAS


def _with(kind, path, value):
    data = copy.deepcopy(VALID[kind])
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


REFUSED = [
    # shape and bounds
    ('compare', ('options',), [{'id': 'a', 'label': 'A'}], 'two to four options'),
    ('compare', ('options',), [{'id': f'o{i}', 'label': 'O'} for i in range(5)], 'two to four options'),
    ('compare', ('criteria', 0, 'values'), [1], 'one value per option'),
    ('compare', ('recommended',), 'zzz', 'one of the options'),
    ('compare', ('options', 1, 'id'), 'a', 'unique'),
    ('compare', ('criteria', 0, 'values'), [{'nested': 1}, 2], 'nonempty text'),
    ('metrics', ('tiles',), [{'label': 'One', 'value': 1}], 'two to four tiles'),
    ('metrics', ('tiles', 0, 'value'), '41', 'bounded finite number'),
    ('metrics', ('tiles', 0, 'value'), 1e16, 'bounded finite number'),
    ('metrics', ('tiles', 0, 'format'), 'money', 'number, percent or compact'),
    ('metrics', ('tiles', 0, 'better'), 'sideways', 'better'),
    ('timeline', ('events', 0, 'date'), '31/10/2026', 'YYYY'),
    ('timeline', ('events', 0, 'date'), '2026-02-29', 'YYYY'),  # not a leap year
    ('timeline', ('events', 0, 'date'), '2026-13', 'YYYY'),
    ('timeline', ('events',), [{'date': '2026', 'title': 'Only one'}], 'two to twenty'),
    ('timeline', ('events', 0, 'tone'), 'red', 'Tones'),
    ('record', ('fields', 0, 'value'), True, 'text or numbers'),
    ('record', ('fields',), [], 'one to twelve'),
    ('record', ('fields',), [{'label': 'F', 'value': 'v'}] * 13, 'one to twelve'),
    # the authority rules: no link, style, handler or authority keys (NFKC-folded), no URL, markup or template text,
    # no credentials, anywhere in the data
    ('compare', ('options', 0, 'href'), 'x', 'links, styles'),
    ('metrics', ('tiles', 0, 'onclick'), 'x', 'links, styles'),
    ('timeline', ('events', 0, 'ｈｒｅｆ'), 'x', 'links, styles'),
    ('record', ('fields', 0, 'approve'), True, 'links, styles'),
    ('record', ('permission',), 'full', 'links, styles'),
    ('compare', ('caption',), 'See https://evil.example/x', 'plain text only'),
    ('metrics', ('tiles', 0, 'label'), '<img src=x onerror=alert(1)>', 'plain text only'),
    ('timeline', ('events', 1, 'title'), '{{ constructor.constructor("x")() }}', 'plain text only'),
    ('record', ('fields', 0, 'value'), 'javascript:alert(1)', 'plain text only'),
    ('compare', ('criteria', 0, 'label'), 'ｊａｖａｓｃｒｉｐｔ:alert(1)', 'plain text only'),
    ('record', ('note',), 'call __TAURI__.invoke', 'plain text only'),
    ('record', ('fields', 1, 'value'), 'sk-live-0123456789abcdefghijklmnop', 'Credentials'),
]


@pytest.mark.parametrize('kind, path, value, words', REFUSED)
def test_the_catalog_refuses_what_it_must(kind, path, value, words):
    with pytest.raises(ValueError, match=re.escape(words) if words != 'YYYY' else 'YYYY'):
        validated(kind, _with(kind, path, value))


def test_the_spec_refuses_the_catalogs_shapes_too():
    for kind, path, value, _ in REFUSED[:19]:
        if [kind, path, value] in [['compare', ('recommended',), 'zzz'], ['compare', ('options', 1, 'id'), 'a'],
                                   ['compare', ('criteria', 0, 'values'), [1]],
                                   ['timeline', ('events', 0, 'date'), '2026-02-29'],
                                   ['timeline', ('events', 0, 'date'), '2026-13']]:
            continue  # cross-field or calendar rules the JSON Schema cannot express; artifacts.validate holds them
        assert spec_errors(kind, _with(kind, path, value)), (kind, path, value)


def test_titles_of_catalog_views_are_inert_like_dashboards():
    assert artifact_schema.validate_native_title('compare', 'Plans side by side') == 'Plans side by side'
    for title in ('https://evil.example', '<b>x</b>', '{{x}}'):
        with pytest.raises(ValueError):
            artifact_schema.validate_native_title('metrics', title)


CHARTS = {
    'area': {'type': 'area', 'x_key': 'm', 'y_keys': ['v'], 'rows': [{'m': 'Jan', 'v': 1}, {'m': 'Feb', 'v': 3}]},
    'scatter': {'type': 'scatter', 'x_key': 'x', 'y_keys': ['y'], 'rows': [{'x': 1, 'y': 2}, {'x': 3.5, 'y': 1}]},
    'stacked': {'type': 'stacked', 'x_key': 'w', 'y_keys': ['a', 'b'], 'rows': [{'w': 'W1', 'a': 1, 'b': 2}]},
    'pie': {'type': 'pie', 'x_key': 'p', 'y_keys': ['s'], 'rows': [{'p': 'A', 's': 3}, {'p': 'B', 's': 1}]},
    'donut': {'type': 'donut', 'x_key': 'p', 'y_keys': ['s'], 'format': 'percent',
              'rows': [{'p': 'A', 's': 0.75}, {'p': 'B', 's': 0.25}]},
    'unit': {'type': 'line', 'x_key': 'd', 'y_keys': ['ms'], 'unit': 'ms', 'format': 'number',
             'rows': [{'d': 'Mon', 'ms': 120}]},
}


@pytest.mark.parametrize('name', sorted(CHARTS))
def test_chart_variants_are_accepted_by_pebble_and_the_spec(name):
    assert validated('chart', CHARTS[name])['type'] == CHARTS[name]['type']
    assert not spec_errors('chart', CHARTS[name])


@pytest.mark.parametrize('data, words', [
    ({**CHARTS['pie'], 'y_keys': ['s', 't'], 'rows': [{'p': 'A', 's': 1, 't': 1}, {'p': 'B', 's': 1, 't': 1}]}, 'one series'),
    ({**CHARTS['pie'], 'rows': [{'p': str(i), 's': 1} for i in range(9)]}, 'two to eight'),
    ({**CHARTS['pie'], 'rows': [{'p': 'A', 's': -1}, {'p': 'B', 's': 2}]}, 'zero or more'),
    ({**CHARTS['pie'], 'rows': [{'p': 'A', 's': 0}, {'p': 'B', 's': 0}]}, 'total above zero'),
    ({**CHARTS['scatter'], 'rows': [{'x': 'one', 'y': 2}]}, 'number on both axes'),
    ({**CHARTS['scatter'], 'rows': [{'x': True, 'y': 2}]}, 'number on both axes'),
    ({**CHARTS['stacked'], 'rows': [{'w': 'W1', 'a': -1, 'b': 2}]}, 'zero or more'),
    ({**CHARTS['unit'], 'format': 'currency'}, 'number, percent or compact'),
    ({**CHARTS['unit'], 'unit': 'a://b'}, 'plain text only'),
    ({**CHARTS['area'], 'rows': [{'m': '<script>x</script>', 'v': 1}]}, 'plain text only'),
    ({**CHARTS['area'], 'type': 'radar'}, 'bar or line'),
    ({**CHARTS['area'], 'style': 'x'}, 'Unsupported declarative view fields'),
])
def test_chart_variants_refuse_what_they_cannot_draw_honestly(data, words):
    with pytest.raises(ValueError, match=re.escape(words)):
        validated('chart', data)


def test_phase_19_charts_validate_exactly_as_before():
    legacy = {'type': 'bar', 'x_key': 'day', 'y_keys': ['runs'], 'rows': [{'day': '<b>Mon</b>', 'runs': 4}]}
    assert validated('chart', legacy) == legacy  # a Phase 19 bar chart's x labels were never inert-checked; still not


DIAGRAMS = {
    'flow': {'layout': 'flow', 'direction': 'right', 'nodes': [{'id': 'a', 'label': 'Draft', 'tone': 'accent'},
                                                                 {'id': 'b', 'label': 'Review', 'sub': 'two people'}],
             'edges': [{'from': 'a', 'to': 'b', 'label': ''}]},
    'layers': {'layout': 'layers', 'caption': 'Stack', 'nodes': [{'id': 'ui', 'label': 'Window', 'group': 'Mac'},
                                                                  {'id': 'run', 'label': 'Runner', 'group': 'Pebble'}],
               'edges': [{'from': 'ui', 'to': 'run', 'label': 'asks'}]},
    'sequence': {'layout': 'sequence', 'nodes': [{'id': 'u', 'label': 'You'}, {'id': 'p', 'label': 'Pebble'}],
                 'edges': [{'from': 'u', 'to': 'p', 'label': 'Ask'}, {'from': 'p', 'to': 'u', 'label': 'Answer'}]},
}


@pytest.mark.parametrize('name', sorted(DIAGRAMS))
def test_diagram_layouts_are_accepted_and_phase_18_diagrams_are_unchanged(name):
    assert validated('diagram', DIAGRAMS[name]) == DIAGRAMS[name]
    assert not spec_errors('diagram', DIAGRAMS[name])
    phase18 = {'nodes': [{'id': 'a', 'label': 'https://kept.example'}], 'edges': []}
    assert validated('diagram', phase18) == phase18, 'a Phase 18 diagram validates exactly as before'


@pytest.mark.parametrize('data, words', [
    ({**DIAGRAMS['flow'], 'layout': 'radial'}, 'flow, layers or sequence'),
    ({**DIAGRAMS['flow'], 'direction': 'up'}, 'down or right'),
    ({**DIAGRAMS['layers'], 'nodes': [{'id': 'ui', 'label': 'Window'}, {'id': 'run', 'label': 'Runner', 'group': 'P'}]}, 'every node a group'),
    ({**DIAGRAMS['layers'], 'nodes': [{'id': f'n{i}', 'label': 'N', 'group': f'g{i}'} for i in range(7)], 'edges': []}, 'six groups'),
    ({**DIAGRAMS['sequence'], 'nodes': [{'id': f'n{i}', 'label': 'N'} for i in range(9)], 'edges': [{'from': 'n0', 'to': 'n1', 'label': ''}]}, 'eight participants'),
    ({**DIAGRAMS['sequence'], 'edges': []}, 'at least one message'),
    ({**DIAGRAMS['flow'], 'nodes': [{'id': 'a', 'label': '<img src=x>', 'tone': 'ok'}, {'id': 'b', 'label': 'B'}]}, 'plain text only'),
    ({**DIAGRAMS['flow'], 'nodes': [{'id': 'a', 'label': 'A', 'tone': 'red'}, {'id': 'b', 'label': 'B'}]}, 'Tones'),
    ({**DIAGRAMS['flow'], 'nodes': [{'id': 'a', 'label': 'A', 'href': 'x'}, {'id': 'b', 'label': 'B'}]}, 'ID and label'),
    ({**DIAGRAMS['flow'], 'onload': 'x'}, '1–40 nodes'),
    ({**DIAGRAMS['flow'], 'edges': [{'from': 'a', 'to': 'b', 'label': 'see javascript:alert(1)'}]}, 'plain text only'),
])
def test_diagram_variants_refuse_what_they_must(data, words):
    with pytest.raises(ValueError, match=re.escape(words)):
        validated('diagram', data)


def test_every_published_variant_passes_the_schema_and_pebbles_own_validator():
    variants = ui_spec.fixtures()['native_variants']
    assert {v['kind'] for v in variants.values()} == {'chart', 'diagram'} and len(variants) >= 8
    for name, variant in variants.items():
        assert not spec_errors(variant['kind'], variant['data']), name
        assert validated(variant['kind'], variant['data']) is not None, name


def test_a_catalog_failure_carries_only_its_own_schema():
    message = schema_error('timeline', ValueError('Timelines need two to twenty events.'))
    assert message.endswith('Schema: ' + CATALOG_SCHEMAS['timeline'])
    assert schema_error('table', ValueError('x')).endswith('Schema: ' + SCHEMAS['table']), 'Phase 19 failures unchanged'


# -- the offer ---------------------------------------------------------------------------------------------------------

ASKS = ['Show me a chart of monthly sales: Jan 10, Feb 12, Mar 9', 'Draw a diagram of how OAuth works',
        'Can you visualize these numbers?', 'Compare the two plans side by side', 'Put this in a timeline please',
        'Plot it as a line chart', 'Give me a pie chart of where the time went', 'Make a flowchart of the signup steps',
        'Show the results as a bar graph', 'Show the old and new prices side by side',
        'Give me a side-by-side comparison of the two laptops']
NOT_ASKS = ['Fix the failing test in parser.py', 'Show me the git log', 'What does the commit graph look like?',
            'What does the chart on page 3 say?', 'Add a chart to the Excel file report.xlsx',
            'Write a Python script that plots the data', 'Create a chart component in React',
            'Draw a diagram in mermaid', 'Build a dashboard of my runs this week', 'Make a plan for the refactor',
            'Make a table of the fruits I like', 'Summarize these research papers', 'Compare the release dates in the pages',
            'Save the chart as chart.png', 'Show me a timeline of the commits on main',
            # "side by side" without compare, show, display or view (the lead, 1.9.29; private d-gw-95's words)
            'Set up a second working folder for the branch release/1.x next to this project folder, named '
            'release-1x, so I can work on both branches side by side. Do not push anything.',
            'Put the two windows side by side on my screen']
# Requests of the benchmark's Git, memory and recovery kinds (golden-2.0-CHARTER.md, "Families"), written for this
# test: none of them asks for a visual, so none is offered the catalog, under either setting for Git and recovery.
FAMILIES = {
    'git_workflow': ['Stash my uncommitted changes, pull the latest main and reapply them.',
                     'Commit only the files under src/ with the message "Fix parser" and push the branch.',
                     'Rebase feature/login onto main and show me the log afterwards.',
                     'Undo the last commit but keep its changes in the working tree.',
                     'Create a tag v1.2.0 on the current commit; do not push anything.'],
    'memory': ['Remember that I prefer tabs over spaces in this repository.',
               'From now on, always run the linter before you commit.',
               'What did I ask you to remember about the test command?',
               'Use the convention I asked you to keep for new files.'],
    'recovery': ['Run the import script; it may fail, so read the error, fix the cause and run it again.',
                 'The build is failing after the dependency update. Find out why and repair it without deleting data.',
                 'Run the migration. If a step errors, fix the input file and rerun from that step.'],
}


@pytest.mark.parametrize('text', ASKS)
def test_a_visual_ask_in_the_persons_words_offers_the_catalog(text):
    assert visual_ask(text) and visual_offer(8192, [text])


@pytest.mark.parametrize('text', NOT_ASKS)
def test_nouns_files_code_dashboards_and_research_get_no_catalog(text):
    assert not visual_ask(text) and not visual_offer(8192, [text]) and not visual_offer(32768, [text])


@pytest.mark.parametrize('family', sorted(FAMILIES))
def test_the_benchmarks_git_memory_and_recovery_requests_get_no_offer(family):
    for text in FAMILIES[family]:
        assert not visual_offer(32768, [text], 'ask'), text
        assert not visual_offer(32768, [text], 'auto'), (text, 'even "auto" never offers it for changes')
        assert offered_kinds(32768, text, visual_offer(32768, [text])) == offered_kinds(32768, text)


def test_the_repositorys_benchmark_corpus_gets_the_catalog_only_where_it_asks_for_a_view():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from benchmarks import corpus_24a as corpus
    offered = {}
    for task in corpus.TASKS:
        turns = list(task.prompts) + [turn.text for turn in task.turns]
        if any(visual_offer(32768, [prompt, *turns[len(task.prompts):]]) for prompt in task.prompts):
            offered.setdefault(task.family, []).append(task.id)
    print(f'\ncorpus 24a: {len(corpus.TASKS)} tasks; offered the catalog: {offered or "none"}')
    assert set(offered) <= {'generated_ui'}, offered


def test_the_offer_is_decided_from_the_conversation_and_then_stays():
    conversation = ['Hi, I have some sales numbers.', 'Jan 10, Feb 12, Mar 9.', 'Show me a chart of them.', 'Thanks!',
                    'What is the average?', 'And the maximum?', 'One more question.', 'Last one.']
    offers = [visual_offer(16384, conversation[:n]) for n in range(1, len(conversation) + 1)]
    assert offers == [False, False, True, True, True, True, True, True], 'one change, at the ask, and never back'
    assert not any(visual_offer(4096, conversation[:n]) for n in range(1, 9)), 'nothing new at 4K'
    assert not visual_offer(8192, ['Hi', None, 42, 'Fix the bug']), 'only text turns count'
    # "auto" (off by default): decided by the first turn only, so a later turn never flips it
    assert visual_offer(8192, ['How does a B-tree work?', 'thanks'], 'auto')
    assert not visual_offer(8192, ['Hello', 'How does a B-tree work?'], 'auto')
    assert not visual_offer(8192, ['How does a B-tree work?'], 'ask'), '"ask" (the default) waits for an ask'


def test_the_kinds_and_brief_are_unchanged_without_the_catalog():
    for text in ('Make a table of fruit', 'Build a dashboard of runs', 'Sketch a prototype', None):
        assert offered_kinds(8192, text, False) == offered_kinds(8192, text)
    assert offered_brief(LEGACY_KINDS) == ''
    assert offered_brief(LEGACY_KINDS + ('dashboard',)) == ' ' + SCHEMAS['dashboard'], 'the Phase 19 suffix exactly'
    assert offered_kinds(8192, 'x', True) == LEGACY_KINDS + CATALOG_KINDS
    assert offered_kinds(4096, 'x', True) == LEGACY_KINDS, '4K never sees a new kind'
    assert offered_brief(offered_kinds(8192, 'x', True)) == ' ' + CATALOG_BRIEF
    assert not set(CATALOG_KINDS) & set(NEW_KINDS) and not set(CATALOG_KINDS) & set(SCHEMAS)
    for text in (*CATALOG_SCHEMAS.values(), CATALOG_BRIEF):
        assert not artifact_schema.REJECTED_TEXT.search(artifact_schema._fold(text)), text


def test_measured_deciding_the_offer_costs_microseconds_per_run():
    turns = ['Here is a long message about my project and its many files, with numbers 1, 2, 3. ' * 20] * 60
    for _ in range(3):
        visual_offer(32768, turns)
    times = []
    for _ in range(31):
        began = time.perf_counter()
        visual_offer(32768, turns)
        times.append((time.perf_counter() - began) * 1000)
    median = statistics.median(times)
    typical = ['Stash my changes and pull main.', 'Now reapply them.', FAMILIES['recovery'][0]]
    began = time.perf_counter()
    for _ in range(1000):
        visual_offer(32768, typical)
    usual = (time.perf_counter() - began)
    print(f'\nvisual_offer, once per run (never per step): 60 human turns of ~1,600 characters, median {median:.3f} ms; '
          f'a usual 3-turn conversation {usual:.3f} ms per run (1,000 runs: {usual * 1000:.1f} ms)')
    # bounds with headroom for a shared Mac under load; a model request takes seconds
    assert median < 10.0, median
    assert usual < 0.5, usual  # 0.5 ms per run


# -- settings ----------------------------------------------------------------------------------------------------------

def test_the_visual_answers_setting_is_ask_by_default_and_never_a_widening():
    assert 'visual_answers' in settings_file.KEYS
    assert settings_file.default('visual_answers') == 'ask'
    assert settings_file.validate('visual_answers', 'auto') == 'auto'
    for bad in ('always', True, None, ''):
        with pytest.raises(ValueError, match='"ask" or "auto"'):
            settings_file.validate('visual_answers', bad)
    assert settings_file.widening('visual_answers', 'ask', 'auto') == []
    assert settings_file.schema()['properties']['visual_answers']['enum'] == ['ask', 'auto']


# -- scripted models through the real App and runner -------------------------------------------------------------------

def _app(root):
    root.mkdir(parents=True)
    app = App(root / 'data', root / 'resources', lambda *args: None)
    app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'ask'})
    app.model_save(name='Visual fixture', url='http://127.0.0.1:9', model='visual-fixture', context=32768)
    return app


def _settle(app, cid, count, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = app.runs.list(cid)
        if len(runs) >= count and all(r['state'] in ('completed', 'failed') for r in runs) and app.active is None:
            return runs
        time.sleep(0.02)
    pytest.fail('runs did not settle')


def _conversation(tmp_path, monkeypatch, name, messages, forced=None, setting=None):
    """Sends `messages` one by one in a new conversation; returns each message's first request (messages, tools)."""
    captured = []
    if forced is not None:
        monkeypatch.setattr(runner, 'visual_offer', lambda *args, **kwargs: forced)
    else:
        monkeypatch.setattr(runner, 'visual_offer', artifact_schema.visual_offer)
    root = tmp_path / name
    app = _app(root)
    if setting:
        app.setting('visual_answers', setting)
    state = SimpleNamespace(during=[], events=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        captured.append({'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools)})
        return answer('Noted.')(token, state)
    monkeypatch.setattr(Providers, 'complete', complete)
    try:
        chat = app.new_chat()
        firsts = []
        for count, text in enumerate(messages, 1):
            before = len(captured)
            app.send(chat['id'], text)
            _settle(app, chat['id'], count)
            firsts.append(captured[before] if len(captured) > before else None)  # None: Pebble answered by itself
        return str(root), firsts
    finally:
        app.active = app.active_run = None
        app.close()
        close_guard_databases(root)


def _normalized(request, root):
    text = json.dumps(request, sort_keys=True, default=str).replace(root, '<root>')
    text = re.sub(r'\b[0-9a-f]{32}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', '<id>', text)
    text = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?', '<time>', text)
    text = re.sub(r'\b1[6-9]\d{8}(\.\d+)?\b', '<epoch>', text)  # a record's save time (the two arms run apart)
    text = re.sub(r'sha256:[0-9a-f]{64}', '<digest>', text)  # digests over records that hold those IDs and times
    return re.sub(r'\b\d{1,2}:\d{2}\b', '<clock>', text)


def _ui(request):
    return {t['function']['name']: t for t in (request or {'tools': []})['tools'] if t['function']['name'] in ('UIRender', 'UIUpdate')}


def test_requests_without_a_visual_ask_are_byte_identical_to_before(tmp_path, monkeypatch):
    # "table" and "plan" already offer UIRender (runner.VIEWS_INTENT): the strongest case for a change to show
    messages = ['Make a table of the three fruits I like: apple, pear and fig.', 'Now make a plan to eat them this week.',
                *FAMILIES['memory'][:2], FAMILIES['recovery'][0]]
    root_new, new = _conversation(tmp_path, monkeypatch, 'new', messages)
    root_old, old = _conversation(tmp_path, monkeypatch, 'old', messages, forced=False)
    assert len(new) == len(old) == len(messages) and sum(r is not None for r in new) >= 4
    for index, (a, b) in enumerate(zip(new, old)):
        assert _normalized(a, root_new) == _normalized(b, root_old), messages[index]
    assert _ui(new[0]), 'UIRender was offered (VIEWS_INTENT), with its Phase 18 kinds only'
    assert _ui(new[0])['UIRender']['function']['parameters']['properties']['kind']['enum'] == list(LEGACY_KINDS)
    assert all(CATALOG_BRIEF not in json.dumps(request) for request in new)


def test_a_visual_ask_adds_the_catalog_once_and_it_stays_for_the_conversation(tmp_path, monkeypatch):
    messages = ['Here are my sales: Jan 10, Feb 12, Mar 9.', 'Show me a chart of them.', 'Thanks!',
                'What is the average?', 'And the maximum?', 'Which month was lowest?']
    _, on = _conversation(tmp_path, monkeypatch, 'on', messages)
    _, off = _conversation(tmp_path, monkeypatch, 'off', messages, forced=False)
    assert not _ui(on[0]), 'before the ask: no view tools'
    offered = [_ui(request) for request in on[1:]]
    assert all(offered), 'from the ask on, every message offers them'
    assert all(json.dumps(o, sort_keys=True) == json.dumps(offered[0], sort_keys=True) for o in offered), \
        'the same view tools, byte for byte, for the rest of the conversation (the prompt cache holds)'
    enum = offered[0]['UIRender']['function']['parameters']['properties']['kind']['enum']
    assert enum == list(LEGACY_KINDS + CATALOG_KINDS)
    assert offered[0]['UIRender']['function']['description'].endswith(' ' + CATALOG_BRIEF)
    # before this change the view offer followed the 3 newest turns (runner.EARLIER_TURNS) and dropped away
    assert [bool(_ui(request)) for request in off] == [False, True, True, True, True, False]

    def tokens(request):
        # conservative: the request's JSON text (quotes escaped) at 4 characters a token
        return len(json.dumps(request['tools'], ensure_ascii=False)) / 4
    added_to_ui = tokens(on[1]) - tokens(off[1])
    print(f'\nthe catalog adds about {added_to_ui:.0f} tokens (escaped JSON / 4; the brief itself is {len(CATALOG_BRIEF)} '
          f'characters, about {len(CATALOG_BRIEF) / 4:.0f} tokens, twice: UIRender and UIUpdate) to a request that '
          'already offers UIRender, and nothing to a request without a visual ask')
    assert 0 < added_to_ui < 560, added_to_ui


def test_a_visual_ask_without_view_words_brings_the_view_tools_and_is_measured(tmp_path, monkeypatch):
    ask = ['Compare these two phones side by side.']  # no view word (runner.VIEWS_INTENT) of its own
    _, on = _conversation(tmp_path, monkeypatch, 'on', ask)
    _, off = _conversation(tmp_path, monkeypatch, 'off', ask, forced=False)
    assert _ui(on[0]) and not _ui(off[0])
    added = (len(json.dumps(on[0]['tools'], ensure_ascii=False)) - len(json.dumps(off[0]['tools'], ensure_ascii=False))) / 4
    print(f'\na visual ask with no view word: the two view tools and the catalog add about {added:.0f} tokens')
    assert 0 < added < 1800, added


def test_auto_is_off_by_default_and_when_chosen_offers_for_a_suitable_first_request(tmp_path, monkeypatch):
    text = ['How does a B-tree work?']
    _, default = _conversation(tmp_path, monkeypatch, 'default', text)
    _, auto = _conversation(tmp_path, monkeypatch, 'auto', text, setting='auto')
    assert not _ui(default[0]), 'the shipped default offers nothing here (D216/D217)'
    assert _ui(auto[0])['UIRender']['function']['parameters']['properties']['kind']['enum'] == list(LEGACY_KINDS + CATALOG_KINDS)
