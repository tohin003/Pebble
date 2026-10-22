"""Phase 20 track H20-context: Pebble's input tokens after staging schemas above 8K, measured per reduction.

H19 (test_context_cost_18) found tool schemas were about 62% of Pebble's input in its nine-family scripted plan, and
the coordination tools offered on every large route were the largest group of schemas never called. H20 changes
only runner.py: above 8K a step offers the coordination tools, MemoryPropose and ReadRunEvidence when the run can use
them (runner.large_needs). Routes of 8K and less are unchanged, and no tool is added.

The H19 plan (one task per family of corpus 18a.2, the real paired driver in scripted mode, the live-shaped script
for both arms, every request captured as the model received it) runs once per ARM, on this source, changing only the
runner's two offer constants, so each reduction is measured alone and together:

    before       LARGE_BY_NEED empty: the Phase 19 offer, exactly
    agents_tree  coordination tools by stage only, ReadAgentTree and SpawnAgent before the first child (root_tools)
    agents       coordination tools by stage only, as shipped (SpawnAgent alone before the first child)
    core         MemoryPropose and ReadRunEvidence by need only
    after        the shipped constants

Every arm must make the same tool calls with the same outcomes as `before`. scripts/measure_context_cost.py counts
the tokens (bundled o200k_base) and prints the before/after table (run pytest with -s). Scripted composition only:
never a live-cost or quality claim.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import runner
from pebble.app import App
from pebble.tool_catalogue import AGENT_ROOT_TOOLS

import test_context_cost_18 as h19
import test_eval_live_shapes_18 as shapes
from test_high_fd_waits_21 import close_guard_databases

mcc = h19.mcc
CORE_BY_NEED = frozenset({'MemoryPropose', 'ReadRunEvidence'})
ARMS = {
    'before': (frozenset(), runner.FIRST_CHILD_TOOLS),
    'agents_tree': (frozenset(AGENT_ROOT_TOOLS), frozenset({'SpawnAgent', 'ReadAgentTree'})),
    'agents': (frozenset(AGENT_ROOT_TOOLS), runner.FIRST_CHILD_TOOLS),
    'core': (CORE_BY_NEED, runner.FIRST_CHILD_TOOLS),
    'after': (runner.LARGE_BY_NEED, runner.FIRST_CHILD_TOOLS),
}
DELEGATING = 'deleg-regions'  # the only plan task whose model spawns a child (no delegation words in its request)


def names(tools):
    return [t['function']['name'] for t in tools or ()]


def calls(log):
    """{(task, actor kind): [tool calls in order]} from LiveShapes.log, without run or agent identities."""
    out = {}
    for entry in log:
        kind = entry['actor'] if entry['actor'] in ('root', 'baseline') else 'child'
        out.setdefault((entry['case'], kind), []).append([name for name, _ in entry['tools']])
    return out


@pytest.fixture(scope='module')
def arms(tmp_path_factory):
    found = {}
    for arm, (staged, first) in ARMS.items():
        root = tmp_path_factory.mktemp('arm-' + arm)
        recorder = h19.Recorder(shapes.LiveShapes())
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(runner, 'LARGE_BY_NEED', staged)
            patch.setattr(runner, 'FIRST_CHILD_TOOLS', first)
            shapes._loopback_only(patch)
            shapes._pin_source(patch)
            result = shapes.run_plan(root, shapes.standin(*h19.FAMILY_TASKS.values()), recorder, patch)
        close_guard_databases(root)  # resource cleanup only (F21-fd): the plan's Apps are closed by now
        capture = root / 'requests.jsonl'
        capture.write_text(''.join(json.dumps(r, ensure_ascii=False, default=str) + '\n' for r in recorder.records))
        summary = mcc.summarize(mcc.measure(recorder.records))
        found[arm] = SimpleNamespace(root=root, result=result, recorder=recorder, summary=summary, capture=capture)
    before = found['before'].summary
    for arm in ARMS:
        if arm != 'before':
            found[arm].comparison = mcc.compare(before, found[arm].summary)
    print('\n' + mcc.table(found['after'].summary))
    print('\nBefore (Phase 19 offer) -> after (H20):\n' + mcc.comparison_table(found['after'].comparison))
    for arm in ('agents_tree', 'agents', 'core'):
        c = found[arm].comparison
        print(f'Only {arm}: median {c["median_input_tokens"][1]} input tokens per run ({c["median_ratio"][1]}x the '
              f'baseline), {c["per_request"][0] - c["per_request"][1]} fewer per pebble request.')
    # Candidates measured on the shipped offer but not applied (see the track note for why).
    after = found['after'].summary
    requests = after['schemas']['pebble']['requests']
    references = after['schemas']['pebble']['tools'].get('CodeReferences', {})
    viewless = sum(1 for r in found['after'].recorder.records if r['arm'] == 'pebble'
                   and not {'UIRender', 'UIUpdate', 'RenderMod'} & set(names(r['tools'])))
    sentence = mcc.tokens(' ui.render views are local data and never confer host permissions.')
    print(f'Not applied: 4K briefs above 8K {after["what_if"]["briefs_everywhere"]["saved_per_request"]} per request; '
          f'CodeReferences by intent above 8K {round(references.get("offered", 0) / requests)} per request '
          f'({references.get("requests", 0)} of {requests} requests offered it, none called it); the views sentence '
          f'only with a view tool {round(sentence * viewless / requests)} per request ({sentence} tokens, '
          f'{viewless} requests without one).')
    return found


def offers(arm, side='pebble'):
    """{task: [offered tool names per request, in order]} for one side; a child's requests interleave with its
    root's in time, so each side is ordered on its own."""
    out = {}
    for record in arm.recorder.records:
        if record['arm'] == side:
            out.setdefault(record['task'], []).append(names(record['tools']))
    return out


# -- the plan, per arm -------------------------------------------------------------------------------------------

def test_every_arm_makes_the_same_calls_with_the_same_outcomes(arms):
    before = arms['before']
    expected_calls = calls(before.recorder.script.log)
    outcomes = {key: cell['outcome'] for key, cell in before.result.cells.items()}
    assert set(outcomes) == {(t, a) for t in h19.FAMILY_TASKS.values() for a in ('pebble', 'baseline')}
    assert not any(o in shapes.BROKEN for o in outcomes.values()), outcomes
    for name, arm in arms.items():
        assert arm.result.refused == [] and arm.recorder.unscripted == set(), name
        assert {key: cell['outcome'] for key, cell in arm.result.cells.items()} == outcomes, name
        assert calls(arm.recorder.script.log) == expected_calls, name
        # Only Pebble's offer changed: the baseline arm's runs are the same size (up to a test timing's digits).
        sizes = [[r['input_tokens'] for r in a.summary['runs'] if r['arm'] == 'baseline'] for a in (arm, before)]
        assert len(sizes[0]) == len(sizes[1]) and all(abs(x - y) <= 0.01 * y + 8 for x, y in zip(*sizes)), name


def test_each_reduction_lowers_pebbles_input_and_together_they_lower_it_most(arms):
    totals = {name: sum(r['input_tokens'] for r in arm.summary['runs'] if r['arm'] == 'pebble')
              for name, arm in arms.items()}
    schemas = {name: arm.summary['arms']['pebble']['parts']['tool_schemas'] for name, arm in arms.items()}
    assert totals['agents_tree'] < totals['before'] and totals['core'] < totals['before']
    assert totals['agents'] < totals['agents_tree']  # ReadAgentTree before the first child, measured alone
    assert totals['after'] < min(totals['agents'], totals['core'])
    # Schema tokens are additive: the shipped saving is the agent saving plus the core saving.
    saved = {name: schemas['before'] - schemas[name] for name in arms}
    assert saved['after'] == saved['agents'] + saved['core'], saved
    # Nothing but the offer moved: every non-schema part of Pebble's input is the same in every arm, up to the
    # o200k length of fresh random identifiers (run and receipt IDs) in tool results.
    parts = {name: arm.summary['arms']['pebble']['parts'] for name, arm in arms.items()}
    for name in arms:
        for part in ('system', 'request', 'constraints', 'memory', 'checkpoint', 'sources', 'host_notes', 'assistant'):
            assert abs(parts[name][part] - parts['before'][part]) <= 0.02 * parts['before'][part] + 8, (name, part)
    after = arms['after'].comparison
    assert after['median_ratio'][1] < after['median_ratio'][0]
    assert after['median_ratio'][1] <= 2.75, after['median_ratio']  # 3.83x -> 2.51x when this test was written
    assert after['per_request'][0] - after['per_request'][1] >= 1500, after['per_request']
    # Schemas are no longer the majority of Pebble's input, though still its largest part.
    shares = arms['after'].summary['arms']['pebble']['shares']
    assert shares['tool_schemas'] < 0.5 < arms['before'].summary['arms']['pebble']['shares']['tool_schemas']


def test_the_shipped_offer_only_removes_schemas_and_keeps_spawn_agent(arms):
    assert offers(arms['after'], 'pebble_child') == offers(arms['before'], 'pebble_child')  # children are untouched
    before, after = offers(arms['before']), offers(arms['after'])
    assert set(before) == set(after) == set(h19.FAMILY_TASKS.values())
    for task, requests in after.items():
        assert len(requests) == len(before[task]), task
        # Every request, including recovery-approval's after its restart (resumed from a snapshot whose `available`
        # is the full Phase 19 list), is narrowed the same way.
        for step, (old, offered) in enumerate(zip(before[task], requests)):
            kept = iter(old)
            assert all(name in kept for name in offered), (task, step)  # an ordered subset: removals only
            removed = set(old) - set(offered)
            assert removed <= runner.LARGE_BY_NEED, (task, step, removed)
            # Delegation and ResearchPlan's synthesis gate (app.step_tool_names) still see SpawnAgent.
            assert 'SpawnAgent' in offered, (task, step)
            assert not {'MemoryPropose', 'ReadRunEvidence'} & set(offered), (task, step)  # none named or needed
            if task != DELEGATING:
                assert set(offered) & AGENT_ROOT_TOOLS == {'SpawnAgent'}, (task, step)


def test_a_spawned_child_brings_back_the_whole_coordination_set_once(arms):
    before, after = offers(arms['before'])[DELEGATING], offers(arms['after'])[DELEGATING]
    assert len(before) == len(after) >= 3
    assert set(after[0]) & AGENT_ROOT_TOOLS == {'SpawnAgent'}
    for old, new in zip(before[1:], after[1:]):
        # After the spawn: the Phase 19 coordination set, so a review and its integration still share one response.
        assert set(new) & AGENT_ROOT_TOOLS == set(old) & AGENT_ROOT_TOOLS
        assert {'WaitForAgents', 'ReviewAgentResult', 'IntegrateAgentPatch'} <= set(new)
    # Prompt caches: each run's tool list changes at most once more than before (here, at the first child).
    old = offers(arms['before'])
    for name, arm in arms.items():
        for task, lists in offers(arm).items():
            changes = sum(a != b for a, b in zip(lists, lists[1:]))
            assert changes <= sum(a != b for a, b in zip(old[task], old[task][1:])) + (task == DELEGATING), (name, task)


def test_the_cli_compares_two_captures(arms, tmp_path, capsys):
    report = tmp_path / 'report.json'
    assert mcc.main([str(arms['after'].capture), '--before', str(arms['before'].capture), '--json', str(report)]) == 0
    printed = capsys.readouterr().out
    saved = json.loads(report.read_text())
    assert saved['comparison']['median_ratio'] == arms['after'].comparison['median_ratio']
    assert saved['median_ratio'] == arms['after'].summary['median_ratio']
    assert 'median ratio to the baseline arm' in printed and len(saved['comparison']['rows']) == len(h19.FAMILY_TASKS)


# -- routes: one real App, only the model call scripted ------------------------------------------------------------

CODING = h19.CODING
DELEGATE = 'Delegate the add fix in calc.py to a child agent, then run the tests.'
LEARN = 'Fix add in calc.py so it returns the sum, and learn from the tests which recipe this project uses.'


def first_request(tmp_path, *, budget, text, project=True, staged=None, memory=True, history=()):
    """The first root request of one route (as h19.route), optionally with earlier turns and the Phase 19 offer."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    requests = []
    try:
        if budget is not None:
            app.store.set_setting('model_path', '')
            app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                                      'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                                      'context': budget, 'budget': budget, 'tools': True}])
            app.model.select('route')
        app.store.set_setting('memory_enabled', memory)

        def complete(messages, tools, cancel, token, deliberate=False):
            requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
            token('Nothing was changed.\nFINAL: none')
            return {'role': 'assistant', 'content': 'Nothing was changed.\nFINAL: none'}

        app.model.complete = complete
        folder = None
        if project:
            folder = tmp_path / 'project'
            folder.mkdir()
            (folder / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        cid = app.new_chat(str(folder) if folder else None)['id']
        for role, content in history:
            app.store.message(cid, role, content)
        if staged is None:
            app.work(app.chat(cid), text, False, [])
        else:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(runner, 'LARGE_BY_NEED', staged)
                app.work(app.chat(cid), text, False, [])
    finally:
        app.active = app.active_run = None
        app.close()
        close_guard_databases(tmp_path)  # resource cleanup only (F21-fd)
    assert requests, 'The route never reached the model.'
    return requests[0]


@pytest.mark.parametrize('budget', [None, 4096, 8192])
@pytest.mark.parametrize('text', [CODING, DELEGATE, LEARN, h19.NATIVE])
def test_routes_of_8k_and_less_offer_exactly_what_they_did(tmp_path, budget, text):
    project = text != h19.NATIVE
    old = first_request(tmp_path / 'before', budget=budget, text=text, project=project, staged=frozenset())
    new = first_request(tmp_path / 'after', budget=budget, text=text, project=project)
    assert names(new[1]) == names(old[1]) and new[1] == old[1]

    def shown(messages):  # recalled memory carries each App's own random record IDs
        return [(m['role'], None if m.get('_pebble_memory') else m['content']) for m in messages]

    assert shown(new[0]) == shown(old[0])


@pytest.mark.parametrize('project', [False, True])
def test_a_large_route_without_delegation_or_memory_words_offers_spawn_agent_alone(tmp_path, project):
    text = CODING if project else h19.NATIVE
    old = names(first_request(tmp_path / 'before', budget=32768, text=text, project=project, staged=frozenset())[1])
    new = names(first_request(tmp_path / 'after', budget=32768, text=text, project=project)[1])
    removed = set(old) - set(new)
    assert [n for n in old if n not in removed] == new  # order kept
    assert removed == (set(old) & AGENT_ROOT_TOOLS) - {'SpawnAgent'} | {'MemoryPropose', 'ReadRunEvidence'}
    assert {'SpawnAgent', 'AskUserQuestion', 'UpdatePlan'} <= set(new)
    print(f'\n32K first offer (project={project}): {len(old)} -> {len(new)} tools; removed {sorted(removed)}')


def test_a_large_route_with_delegation_words_keeps_the_whole_set(tmp_path):
    old = first_request(tmp_path / 'before', budget=32768, text=DELEGATE, staged=frozenset())[1]
    new = first_request(tmp_path / 'after', budget=32768, text=DELEGATE)[1]
    assert set(names(new)) & AGENT_ROOT_TOOLS == set(names(old)) & AGENT_ROOT_TOOLS
    assert 'SpawnWorktreeAgent' in names(new) and 'WaitForAgents' in names(new)
    assert set(names(old)) - set(names(new)) == {'MemoryPropose', 'ReadRunEvidence'}


def test_memory_propose_follows_learning_words_and_the_memory_switch(tmp_path):
    assert 'MemoryPropose' in names(first_request(tmp_path / 'on', budget=32768, text=LEARN)[1])
    assert 'MemoryPropose' not in names(first_request(tmp_path / 'off', budget=32768, text=LEARN, memory=False)[1])
    assert 'MemoryPropose' not in names(first_request(tmp_path / 'none', budget=32768, text=CODING)[1])


def test_read_run_evidence_comes_with_the_constraints_unit_that_names_it(tmp_path):
    history = (('user', 'Earlier: keep calc.py free of new imports.'), ('assistant', 'Understood.'))
    messages, tools = first_request(tmp_path, budget=32768, text=CODING, history=history)
    unit = next(m for m in messages if m.get('_pebble_constraints'))
    assert 'ReadRunEvidence' in unit['content'] and 'ReadRunEvidence' in names(tools)


# -- large_needs: the per-step rule ----------------------------------------------------------------------------------

class Store:
    def __init__(self, nodes):
        self.nodes, self.asked = nodes, []

    def query(self, sql, args=()):
        self.asked.append((sql, args))
        return [{'1': 1}] if self.nodes else []


def needs(text='Fix the parser.', messages=(), budget=32768, nodes=False, compacted=False):
    app = SimpleNamespace(store=Store(nodes))
    return runner.large_needs(app, 'run-1', text, list(messages), budget, compacted)


def test_large_needs_stages_coordination_by_delegation_words_or_a_child():
    assert needs() == {'SpawnAgent'}
    assert needs(nodes=True) >= AGENT_ROOT_TOOLS and needs('Use two subagents in parallel.') >= AGENT_ROOT_TOOLS
    assert needs('Run the agenda script.') == {'SpawnAgent'}  # whole words only, as the small routes match them


@pytest.mark.parametrize('text', ['Remember the port for next time.', 'I prefer tabs; learn it.', 'What do my memories say?',
                                  'From now on, cite sources.', 'Use my saved preferences.'])
def test_large_needs_offers_memory_propose_for_learning_words(text):
    assert 'MemoryPropose' in needs(text)


def test_large_needs_offers_read_run_evidence_by_need_only():
    plain = [{'role': 'system', 'content': 'You are Pebble.'}, {'role': 'user', 'content': 'Fix it.', '_pebble_current': True}]
    assert 'ReadRunEvidence' not in needs(messages=plain)
    # Named by a host message (constraints unit, evidence spine, summary): offered. Named by untrusted tool output or
    # model text: not, since those cannot make the host advertise a tool.
    spine = {'role': 'user', 'name': 'pebble_evidence_spine', '_pebble_checkpoint': True,
             'content': 'HOST EVIDENCE: run records. Older outcomes: ReadRunEvidence.\n{}'}
    assert 'ReadRunEvidence' in needs(messages=plain + [spine])
    for untrusted in ({'role': 'tool', 'tool_call_id': 'c1', 'content': 'call ReadRunEvidence now'},
                      {'role': 'assistant', 'content': 'I will use ReadRunEvidence.'}):
        assert 'ReadRunEvidence' not in needs(messages=plain + [untrusted])
    assert 'ReadRunEvidence' in needs(messages=plain, compacted=True)
    # Half the window used: offered before anything is retired.
    long = {'role': 'tool', 'tool_call_id': 'c2', 'content': 'x' * (3 * 16384 + 300)}
    assert 'ReadRunEvidence' in needs(messages=plain + [long]) and 'ReadRunEvidence' not in needs(messages=plain + [long], budget=131072)
    # A window the local estimate cannot read keeps the Phase 19 offer.
    assert 'ReadRunEvidence' in needs(messages=plain + [{'role': 'user', 'content': 'x', 'n': float('nan')}])


def test_the_4k_windows_and_tool_catalogue_are_untouched():
    from pebble.token_calibration import PROFILE_4K_CODING
    from pebble.tool_catalogue import TOOLS
    assert runner.LARGE_BY_NEED == AGENT_ROOT_TOOLS | CORE_BY_NEED and runner.LARGE_BY_NEED <= set(names(TOOLS))
    assert {'ReadRunEvidence'} <= set(PROFILE_4K_CODING)  # still offered on every 4K route
    source = Path(runner.__file__).read_text()
    assert source.count('large_needs(app, run_id, text, messages, budget, execution') == 1  # only inside `if large:`
