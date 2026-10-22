"""Phase 24 track t24-agent: context cost above 8K, measured (V1-077, V1-078, V1-079).

The Phase 19 plan (one task per family of corpus 18a.2, the real paired driver in scripted mode, the live-shaped script
for both arms, every request captured as the model received it; test_context_cost_18) runs once per ARM on this
source, changing only runner constants:

    shipped   this track's sitting-a source, as it ships
    t9        runner.T9_PACKAGE: CodeReferences and Glob by intent, UpdatePlan for multi-step work, SpawnAgent in a
              chat without a project only for delegation or research, no restated working record or curated procedure
    tail      runner.TAIL_LAYOUT: the per-step host state as one HOST STATE tail message (an append-only request)
    both      the two together

scripts/measure_context_cost.py counts the tokens (bundled o200k_base). The prefix reuse share is the part of each
request (after a run's first) that repeats the previous request's leading messages exactly, with the same tools: what
a provider's prompt cache could reuse (counted tokens, T9, do not change with it). Scripted composition only: never a
live-cost or quality claim. Both switches stay off: the track entry adopts them only after the live claim set shows no
T1/T3 regression, and this scripted plan is the baseline that claim set starts from.
"""
import inspect
import json
from types import SimpleNamespace

import pytest

from pebble import runner
from pebble.app import App

import test_context_cost_18 as h19
import test_eval_live_shapes_18 as shapes
from test_context_reductions_20 import calls, names
from test_high_fd_waits_21 import close_guard_databases

mcc = h19.mcc
ARMS = {'shipped': {}, 't9': {'T9_PACKAGE': True}, 'tail': {'TAIL_LAYOUT': True},
        'both': {'T9_PACKAGE': True, 'TAIL_LAYOUT': True}}


def wire(message):
    return {k: v for k, v in message.items() if not k.startswith('_pebble_')}


def prefix_reuse(records):
    """(shared tokens, request tokens) over every Pebble root request after its run's first."""
    by_run, shared, total = {}, 0, 0
    for record in records:
        if record['arm'] == 'pebble':
            by_run.setdefault(record['task'], []).append(record)
    for requests in by_run.values():
        for previous, current in zip(requests, requests[1:]):
            size = h19.count(current['messages'], current['tools'])
            total += size
            if json.dumps(previous['tools'], sort_keys=True) != json.dumps(current['tools'], sort_keys=True):
                continue  # a changed tool list breaks the cached prefix (tools precede the messages)
            common = 0
            for old, new in zip(previous['messages'], current['messages']):
                if wire(old) != wire(new):
                    break
                common += 1
            shared += h19.count(current['messages'][:common], current['tools']) if common else 0
    return shared, total


@pytest.fixture(scope='module')
def arms(tmp_path_factory):
    found = {}
    for arm, constants in ARMS.items():
        root = tmp_path_factory.mktemp('arm-' + arm)
        recorder = h19.Recorder(shapes.LiveShapes())
        with pytest.MonkeyPatch.context() as patch:
            for name, value in constants.items():
                patch.setattr(runner, name, value)
            shapes._loopback_only(patch)
            shapes._pin_source(patch)
            result = shapes.run_plan(root, shapes.standin(*h19.FAMILY_TASKS.values()), recorder, patch)
        close_guard_databases(root)  # resource cleanup only (F21-fd)
        summary = mcc.summarize(mcc.measure(recorder.records))
        shared, total = prefix_reuse(recorder.records)
        found[arm] = SimpleNamespace(result=result, recorder=recorder, summary=summary,
                                     reuse=round(shared / total, 3) if total else 0.0)
    shipped = found['shipped'].summary
    print('\n' + mcc.table(shipped))
    print('\nFirst request per family (shipped):')
    for run in sorted(shipped['runs'], key=lambda r: r['task']):
        if run['arm'] == 'pebble':
            first = run['first']
            print(f"  {h19.FAMILY_OF.get(run['task'], '?'):16} {run['task']:18} first {first.get('total')} tokens "
                  f"(schemas {first['parts']['tool_schemas']}, system {first['parts']['system']}); "
                  f"run {run['input_tokens']} over {run.get('requests')} requests")
    for arm in ('t9', 'tail', 'both'):
        comparison = mcc.compare(shipped, found[arm].summary)
        print(f'\nShipped -> {arm}:\n' + mcc.comparison_table(comparison))
        found[arm].comparison = comparison
    for arm, item in found.items():
        per_request = item.summary['arms']['pebble']['median_input_tokens']
        print(f"{arm}: median ratio {item.summary['median_ratio']}x, median run {per_request} tokens, "
              f"prefix reuse share {item.reuse}")
    return found


def test_every_arm_makes_the_same_calls_with_the_same_outcomes(arms):
    shipped = arms['shipped']
    outcomes = {key: cell['outcome'] for key, cell in shipped.result.cells.items()}
    assert set(outcomes) == {(t, a) for t in h19.FAMILY_TASKS.values() for a in ('pebble', 'baseline')}
    assert not any(o in shapes.BROKEN for o in outcomes.values()), outcomes
    expected = calls(shipped.recorder.script.log)
    for name, arm in arms.items():
        assert arm.result.refused == [] and arm.recorder.unscripted == set(), name
        assert {key: cell['outcome'] for key, cell in arm.result.cells.items()} == outcomes, name
        assert calls(arm.recorder.script.log) == expected, name


def test_the_shipped_offer_keeps_the_phase_20_bound(arms):
    summary = arms['shipped'].summary
    assert summary['median_ratio'] <= 2.75, summary['median_ratio']  # as test_context_reductions_20 pins it
    assert summary['counter'] == 'o200k_base'


def test_the_t9_package_lowers_pebbles_input_without_removing_a_called_tool(arms):
    shipped, t9 = arms['shipped'].summary, arms['t9'].summary
    totals = {name: sum(r['input_tokens'] for r in arm.summary['runs'] if r['arm'] == 'pebble') for name, arm in arms.items()}
    assert totals['t9'] < totals['shipped']
    assert t9['median_ratio'] <= shipped['median_ratio']
    # Every tool the scripted model called on a task was offered on that task (the package removed nothing it used).
    offered = {}
    for record in arms['t9'].recorder.records:
        if record['arm'] == 'pebble':
            offered.setdefault(record['task'], set()).update(names(record['tools']))
    for entry in arms['t9'].recorder.script.log:
        if entry['actor'] == 'root':
            assert {name for name, _ in entry['tools']} <= offered[entry['case']], entry['case']


def test_the_tail_layout_raises_the_prefix_reuse_share(arms):
    shipped, tail = arms['shipped'].reuse, arms['tail'].reuse
    assert tail > shipped + 0.15, (shipped, tail)  # measured 0.525 -> 0.718 when this test was written
    # Counted tokens (T9) stay about the same: the same text moves, nothing is added but one framing message.
    totals = {name: sum(r['input_tokens'] for r in arms[name].summary['runs'] if r['arm'] == 'pebble')
              for name in ('shipped', 'tail')}
    assert abs(totals['tail'] - totals['shipped']) <= 0.05 * totals['shipped'], totals


# -- t24-auth (V1-078 key part) and V1-079 --------------------------------------------------------------------------

def scripted_app(tmp_path, budget):
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    app.store.set_setting('model_path', '')
    app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                              'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                              'context': budget, 'budget': budget, 'tools': True}])
    app.model.select('route')
    return app


def test_each_run_sets_its_prompt_cache_key_and_clears_it(tmp_path):
    app = scripted_app(tmp_path, 32768)
    keys = []

    def complete(messages, tools, cancel, token, deliberate=False):
        keys.append(getattr(app.model, 'prompt_cache_key', None))
        token('Hello.')
        return {'role': 'assistant', 'content': 'Hello.'}
    app.model.complete = complete
    try:
        chat = app.new_chat()
        runner.run(app, app.chat(chat['id']), 'Say hello.', False, [])
        rid = app.runs.list(chat['id'])[0]['id']
        assert keys == [rid] and app.model.prompt_cache_key is None
    finally:
        app.active = app.active_run = None
        app.close()


def test_working_memory_is_synced_once_per_boundary_and_the_chats_actions_are_not_read(tmp_path):
    app = scripted_app(tmp_path, 32768)
    syncs, checkpoints = [], []
    sync, checkpoint = app.memory.sync_working, app.memory.checkpoint
    app.memory.sync_working = lambda run_id, chat_id=None: (syncs.append(run_id), sync(run_id, chat_id))[1]

    def checkpointing(*args, **kwargs):
        if inspect.stack()[1].filename.endswith('runner.py'):  # the runner's own calls (app.py's are the parent's)
            checkpoints.append(args[2])
        return checkpoint(*args, **kwargs)
    app.memory.checkpoint = checkpointing
    folder = tmp_path / 'project'
    folder.mkdir()
    (folder / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
    replies = [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'r1', 'type': 'function', 'function': {
        'name': 'Read', 'arguments': json.dumps({'file_path': 'calc.py'})}}]}]

    def complete(messages, tools, cancel, token, deliberate=False):
        if replies:
            return replies.pop(0)
        token('It subtracts.')
        return {'role': 'assistant', 'content': 'It subtracts.'}
    app.model.complete = complete
    try:
        chat = app.new_chat(str(folder))
        runner.run(app, app.chat(chat['id']), 'What does add do in calc.py?', False, [])
    finally:
        app.active = app.active_run = None
        app.close()
        close_guard_databases(tmp_path)
    assert len(checkpoints) == 2 and all(actions == () for actions in checkpoints), checkpoints  # V1-079
    # The run's start (its checkpoint and its first persist), step 1, its one tool result, step 2, and the settled
    # episode's final phase: six writes. 0.6.1 made seven: the end of the tool batch rewrote the unchanged record.
    assert len(syncs) == 6, syncs
