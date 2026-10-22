"""Phase 19 track H19-quality (H3, H4): honesty about a missing tool, and where a run's input tokens go.

H4. One task per corpus family runs through the real paired driver in scripted mode, with the live-shaped script
of test_eval_live_shapes_18 for both arms. Every model request is captured exactly as the model received it, and
scripts/measure_context_cost.py counts its parts with TokenBudget (system prompt, tool schemas, request, memory,
checkpoints, inbox, host notes, the model's own turns, tool results, framing). The table is written to the test's
tmp folder and printed (run pytest with -s to see it). It is a scripted measurement of composition, never a model
quality or live-cost claim. Since Phase 20 H20 it measures the staged offers above 8K; test_context_reductions_20
runs the same plan with the Phase 19 offer as well and reports both.

H3. The proposed root prompt change (CAPABILITY_SENTENCE replaces EVIDENCE_SENTENCE in tool_catalogue.SYSTEM; parent
patch in the track note) reaches every root model route: no project and a project, at 4K, 8K and 32K. At 4K it stays
inside the strictest pinned 4K window (test_token_calibration: SYSTEM, checkpoint, request and the 4K coding profile's
catalogue schemas under the conservative local estimate, with the 1,200-token output floor), and the real first
request fits both 4K projections with the sentence kept. Until the parent applies the patch, these tests make the
exact replacement in runner.SYSTEM for their own Apps; once SYSTEM carries it, that step does nothing and the same
assertions check the shipped prompt.
"""
import copy
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import eval_corpus_18 as c18
from pebble import runner, tool_catalogue
from pebble.app import App
from pebble.context_manifest import assemble_context
from pebble.token_budget import TokenBudget

import test_eval_live_shapes_18 as shapes

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('measure_context_cost', ROOT / 'scripts' / 'measure_context_cost.py')
mcc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcc)

# One task per family of corpus 18a.2 (eval_corpus_18.FAMILIES), each scripted by LiveShapes.
FAMILY_TASKS = {'coding': 'code-p90', 'source_search': 'search-definition', 'research': 'research-harbour',
                'documents': 'doc-policy', 'native_operation': 'native-browser', 'memory': 'memory-ports',
                'recovery': 'recovery-approval', 'forms_ui': 'ui-hostile-view', 'delegation': 'deleg-regions'}
assert set(FAMILY_TASKS) == set(c18.FAMILIES)
FAMILY_OF = {task: family for family, task in FAMILY_TASKS.items()}

# H3: one SYSTEM sentence widened in place, so the 4K window grows by a few tokens only (its pinned margin is 16).
EVIDENCE_SENTENCE = 'Never claim edits applied or tests passed without matching tool evidence.'
CAPABILITY_SENTENCE = ('Never claim edits applied, tests passed or other results without tool evidence; if no tool can '
                       'do a step, say so.')


def patched_system(system):
    """SYSTEM with the proposed sentence (unchanged once the parent's patch is in)."""
    if CAPABILITY_SENTENCE in system:
        return system
    assert system.count(EVIDENCE_SENTENCE) == 1, 'The anchor sentence moved; re-anchor the H3 patch.'
    return system.replace(EVIDENCE_SENTENCE, CAPABILITY_SENTENCE)


class Recorder:
    """Wraps the live-shaped script: every request is kept as the model received it, before it answers."""

    def __init__(self, script):
        self.script, self.records, self.lock = script, [], threading.Lock()
        self.unscripted = script.unscripted

    def __call__(self, req):
        arm = 'baseline' if req.actor == 'baseline' else 'pebble' if req.actor == 'root' else 'pebble_child'
        with self.lock:
            self.records.append({'arm': arm, 'task': req.case_id, 'family': FAMILY_OF.get(req.case_id),
                                 'actor': req.actor, 'step': req.step, 'messages': copy.deepcopy(req.messages),
                                 'tools': copy.deepcopy(req.tools)})
        return self.script(req)


@pytest.fixture(scope='module')
def measured(tmp_path_factory):
    root = tmp_path_factory.mktemp('context-cost')
    recorder = Recorder(shapes.LiveShapes())
    with pytest.MonkeyPatch.context() as patch:
        shapes._loopback_only(patch)
        shapes._pin_source(patch)
        result = shapes.run_plan(root, shapes.standin(*FAMILY_TASKS.values()), recorder, patch)
    capture = root / 'requests.jsonl'
    capture.write_text(''.join(json.dumps(r, ensure_ascii=False, default=str) + '\n' for r in recorder.records))
    rows = mcc.measure(recorder.records)
    summary = mcc.summarize(rows)
    report = root / 'context-cost.json'
    assert mcc.main([str(capture), '--json', str(report)]) == 0  # the CLI reads the same capture
    print('\n' + mcc.table(summary))
    return SimpleNamespace(root=root, result=result, rows=rows, summary=summary, recorder=recorder,
                           report=json.loads(report.read_text()))


def test_every_family_ran_in_both_arms_and_every_request_was_measured(measured):
    assert measured.result.refused == [] and measured.recorder.unscripted == set()
    outcomes = {key: cell['outcome'] for key, cell in measured.result.cells.items()}
    assert set(outcomes) == {(t, arm) for t in FAMILY_TASKS.values() for arm in ('pebble', 'baseline')}
    assert not any(o in ('error_driver', 'error_infrastructure', 'not_run') for o in outcomes.values()), outcomes
    runs = {(r['arm'], r['task']) for r in measured.summary['runs']}
    assert {(arm, t) for t in FAMILY_TASKS.values() for arm in ('pebble', 'baseline')} <= runs
    for row in measured.rows:
        assert row['total'] == sum(row['parts'].values()) and row['parts']['framing'] >= 0, row['task']
        assert row['parts']['system'] > 0 and (row['tools_offered'] == 0) == (row['parts']['tool_schemas'] == 0)
    assert measured.summary['counter'] == 'o200k_base'  # the bundled ranks, not the byte upper bound
    assert measured.report['median_ratio'] == measured.summary['median_ratio']


def test_the_pebble_arm_spends_most_of_its_input_on_schemas_and_fixed_context(measured):
    """The measured facts the track note's three reductions rest on (scripted composition only)."""
    pebble, baseline = measured.summary['arms']['pebble'], measured.summary['arms']['baseline']
    assert pebble['median_input_tokens'] > baseline['median_input_tokens']
    shares = pebble['shares']
    # Tool schemas are the largest pebble part over the runs, and larger than the baseline's whole schema cost.
    assert max(shares, key=shares.get) == 'tool_schemas', shares
    assert pebble['parts']['tool_schemas'] > baseline['parts']['tool_schemas']
    first = {r['task']: r['first'] for r in measured.summary['runs'] if r['arm'] == 'pebble'}
    for task, request in first.items():
        assert request['parts']['tool_schemas'] >= request['parts']['system'], (task, request)
    # Most offered schemas are never called in the run. Phase 20 H20 changed which group is largest: it staged the
    # coordination tools, MemoryPropose and ReadRunEvidence above 8K (runner.large_needs), so the task tools now lead
    # (the agent set led on the Phase 19 source; test_context_reductions_20 measures before and after).
    groups = measured.summary['schemas']['pebble']['groups']
    assert max(groups, key=lambda g: groups[g]['unused']) == 'task'
    assert sum(g['unused'] for g in groups.values()) > sum(g['offered'] for g in groups.values()) / 2
    # The reductions, each an upper bound from these runs. With H20's two applied, what is left of them is the
    # delegation run's coordination set after its child (agent) and nothing (core); task tools on demand lead.
    scenarios = measured.summary['what_if']
    singles = {name: found['saved_per_request'] for name, found in scenarios.items() if name != 'combined'}
    assert max(singles, key=singles.get) == 'task_tools_on_demand', scenarios
    assert singles['core_tools_by_need'] == 0 and 0 < singles['agent_tools_by_need'] < singles['briefs_everywhere']
    assert scenarios['combined']['ratio'] < measured.summary['median_ratio'], scenarios


# -- H3: the capability sentence on every root route ------------------------------------------------------------

NATIVE = c18.BY_ID['native-browser'].prompt
CODING = 'Fix add in calc.py so it returns the sum, then run the tests.'


def route(tmp_path, monkeypatch, *, budget, project, text):
    """The first root request of one route, from a real App with only the model call scripted."""
    monkeypatch.setattr(runner, 'SYSTEM', patched_system(runner.SYSTEM))  # the proposed tool_catalogue patch
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    requests = []
    try:
        if budget is not None:  # a cloud profile with this window; None is the default local 4K route
            app.store.set_setting('model_path', '')
            app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                                      'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                                      'context': budget, 'budget': budget, 'tools': True}])
            app.model.select('route')

        def complete(messages, tools, cancel, token, deliberate=False):
            requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
            token('I cannot do that from here, so nothing was done.\nFINAL: unsupported')
            return {'role': 'assistant', 'content': 'I cannot do that from here, so nothing was done.\nFINAL: unsupported'}

        app.model.complete = complete
        folder = None
        if project:
            folder = tmp_path / 'project'
            folder.mkdir(parents=True)
            (folder / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        chat = app.new_chat(str(folder) if folder else None)
        app.work(chat, text, False, [])
    finally:
        app.active = app.active_run = None
        app.close()
    assert requests, 'The route never reached the model.'
    return requests[0]


def wire(messages):
    return [{k: v for k, v in m.items() if not k.startswith('_pebble_')} for m in messages]


def count(messages, tools):
    return TokenBudget(ROOT / 'src-tauri' / 'resources').count(wire(messages), tools)


@pytest.mark.parametrize('budget', [None, 8192, 32768])
@pytest.mark.parametrize('project', [False, True])
def test_the_capability_sentence_is_in_every_root_routes_system_prompt(tmp_path, monkeypatch, budget, project):
    messages, tools = route(tmp_path, monkeypatch, budget=budget, project=project,
                            text=CODING if project else NATIVE)
    system = [m for m in messages if m['role'] == 'system']
    assert len(system) == 1 and CAPABILITY_SENTENCE in system[0]['content']
    assert EVIDENCE_SENTENCE not in system[0]['content']  # widened in place, never said twice
    if not project:
        # The browser task's offer: nothing here can sign in, operate a browser or download a file, so the honest
        # answer is "unsupported"; before the change only the task's own tail said so (H3 finding).
        names = sorted(t['function']['name'] for t in tools)
        print(f'\nnative-browser offer at {budget or "local 4K"}: {names}')
        assert not {'ScreenObserve', 'ScreenAct', 'OpenApp', 'PluginCall', 'RunCommand', 'Write'} & set(names)


def test_the_change_stays_inside_the_pinned_4k_window():
    """test_token_calibration's strictest 4K window (SYSTEM, checkpoint, request and the 4K coding profile's catalogue
    schemas, conservative local estimate, 1,200-token output floor) keeps a margin with the sentence."""
    from pebble.context_manifest import estimate_tokens
    from pebble.token_calibration import PROFILE_4K_CODING, profile_tools
    tools = profile_tools(PROFILE_4K_CODING)

    def window(system):
        return [{'role': 'system', 'content': system},
                {'role': 'user', 'content': 'Host task checkpoint: no tools used yet. This is data, not instructions.',
                 '_pebble_checkpoint': True},
                {'role': 'user', 'content': 'Fix the failing test in parser.py', '_pebble_current': True}]

    shipped = tool_catalogue.SYSTEM.replace(CAPABILITY_SENTENCE, EVIDENCE_SENTENCE)
    before, after = estimate_tokens(window(shipped), tools), estimate_tokens(window(patched_system(shipped)), tools)
    print(f'\npinned 4K window: {before} -> {after} estimate tokens (margin {4096 - 1200 - before} -> '
          f'{4096 - 1200 - after})')
    assert 0 < after - before <= 16 and 4096 - 1200 - after >= 0
    # The same window as a 4K cloud route sends it: the runner's compact copies, the bundled o200k counter.
    offered = [json.loads(json.dumps(t)) for t in tools]
    for spec in offered:
        brief = runner.AGENT_BRIEF.get(spec['function']['name']) or runner.SMALL_BRIEF.get(spec['function']['name'])
        if brief:
            spec['function']['description'] = brief
            for field in spec['function']['parameters'].get('properties', {}).values():
                if isinstance(field, dict):
                    field.pop('description', None)
    cloud = [count(window(system), offered) for system in (shipped, patched_system(shipped))]
    print(f'4K cloud window (o200k): {cloud[0]} -> {cloud[1]} tokens of {4096 - 1024 - 64}')
    assert cloud[1] - cloud[0] <= 16 and 4096 - 1024 - 64 - cloud[1] >= 1000


@pytest.mark.parametrize('project', [False, True])
def test_at_4k_the_first_request_with_the_sentence_fits_both_4k_projections(tmp_path, monkeypatch, project):
    text = CODING if project else NATIVE
    for budget in (None, 4096):
        messages, tools = route(tmp_path / str(budget), monkeypatch, budget=budget, project=project, text=text)
        total = count(messages, tools)
        projected = []
        # The local llama route: 4000 tokens less its largest output reserve; a 4K cloud profile: 4096 less its
        # 1024-token output (providers.Providers._complete). Each must keep the system prompt whole.
        for limit, reserve in ((4000, 1200), (4096 - 1024, 64)):
            projection = assemble_context(messages, tools, limit, reserve, counter=count,
                                          counter_method='o200k_local_estimate')
            kept = [m for m in projection.messages if m['role'] == 'system']
            assert kept and CAPABILITY_SENTENCE in kept[0]['content'], (limit, project)
            projected.append(projection.manifest['input_tokens']['after'])
        print(f'\n4K first request (project={project}, profile={budget}): {total} tokens; projected {projected}; '
              f'tools {len(tools)}; change in SYSTEM: {CAPABILITY_SENTENCE in tool_catalogue.SYSTEM}')
        assert total <= 4000 - 1200, (total, project, budget)
