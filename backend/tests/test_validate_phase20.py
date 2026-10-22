"""Phase 20 track V20-validator: scripts/validate_phase20.py, the phase20 service validator.

The default tests start no backend. They pin the report contract (unique planned names in campaign order, a new
report created with O_EXCL, source digests inside the verifier's snapshot), the static shell and catalogue checks,
the scripted model turns, and every pinned 4K/8K tool offer, recomputed here in-process with runner.LARGE_BY_NEED
empty: test_context_reductions_20's definition of "the Phase 19 offer, exactly". The readiness contract v2 checks
run in-process on a synthetic evidence tree with throwaway ssh-keygen keys in pytest tmp.

The whole campaign is the validator's own run: verify_harness_candidate runs validate_phase20.py in source and
frozen modes. Clean-up (i20b): the two explicit-request tests that repeated that run were removed, so the default
run has no skip. One always-run test drives the same entry point, main(), on a small campaign (the static checks and
one real service's setup), in source mode and through a --backend launcher. No model inference and no network: every
model turn is the validator's scripted loopback provider.
"""

import copy
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))  # the validator imports its sibling validators by name
_spec = importlib.util.spec_from_file_location('validate_phase20', SCRIPTS / 'validate_phase20.py')
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)


def load_script(name):
    spec = importlib.util.spec_from_file_location(name + '_for_phase20_validator', SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub():
    """A campaign that never starts its provider or a service."""
    campaign = v.Campaign(['unused'], None)
    campaign.provider.server.server_close()
    campaign.trap.server.server_close()
    return campaign


def turn(provider, messages, model=v.MODEL_WIDE, tools=()):
    return provider.respond({'model': model, 'messages': messages, 'tools': list(tools)})[0]


def goal(tag):
    return [{'role': 'system', 'content': 'synthetic'}, {'role': 'user', 'content': v.GOALS[tag] + ' ' + v.MARK + tag}]


def answer(messages, delta, value):
    messages.append({'role': 'assistant', **delta})
    for call in delta.get('tool_calls', []):
        messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(value(call))})


def emitted(delta):
    return [c['function']['name'] for c in delta['tool_calls']] if 'tool_calls' in delta else delta['content']


# The report contract --------------------------------------------------------------------------------------------

def test_planned_names_are_unique_ordered_and_every_scenario_is_bounded():
    assert len(v.PLANNED) == len(set(v.PLANNED)) and v.PLANNED[-1] == 'backend_inputs_stable_during_campaign'
    assert v.PLANNED[0] == 'fixture_scratch_created_outside_the_checkout'
    assert set(v.SCENARIO_BOUNDS) == set(v.SCENARIOS) and all(0 < b < v.BUDGET_SECONDS for b in v.SCENARIO_BOUNDS.values())
    assert v.BUDGET_SECONDS < v.VERIFIER_SECONDS == 240
    assert all(re.fullmatch(r'[a-z][0-9]', tag) for tag in v.GOALS) and set(v.PHASE19_OFFERS) <= set(v.GOALS)
    assert {pin['profile'] for pin in v.PHASE19_OFFERS.values()} == {'4k', '8k'}  # pins exist at both small windows
    assert set(v.GAPS) == {'routes'}


def test_the_report_is_a_new_file_and_every_source_it_hashes_is_in_the_verifier_snapshot(tmp_path, monkeypatch, capsys):
    verify = load_script('verify_harness_candidate')
    # Names only: this copy need not hold .runtime/phase19-baseline/build_epoch8_fixture.py, which sources() hashes.
    monkeypatch.setattr(verify, 'sha', lambda path: None)
    covered = set(verify.sources())
    digests = v.checkout_digests()
    assert set(digests) <= covered, sorted(set(digests) - covered)
    assert all(isinstance(value, str) and len(value) == 64 for value in digests.values())
    assert 'scripts/validate_phase20.py' in digests and 'scripts/check_harness_release.py' in digests
    existing, link = tmp_path / 'report.json', tmp_path / 'link.json'
    existing.write_text('earlier evidence\n')
    link.symlink_to(tmp_path / 'missing-target.json')
    for path in (existing, link):
        with pytest.raises(SystemExit):
            v.main(['--report', str(path)])
        assert 'already exists' in capsys.readouterr().err
    with pytest.raises(FileExistsError):
        v.write_new(existing, 'replacement')
    with pytest.raises(OSError):
        v.write_new(link, 'through a link')
    assert existing.read_text() == 'earlier evidence\n' and not (tmp_path / 'missing-target.json').exists()
    fresh = tmp_path / 'fresh.json'
    v.write_new(fresh, '{}\n')
    assert fresh.read_text() == '{}\n' and (fresh.stat().st_mode & 0o777) == 0o600
    # The report shape: source digests, the executable digest (null in source mode), unique names in planned order.
    campaign = stub()
    campaign.finish()
    report = v.build_report(campaign, backend=None, backend_sha=None, sources=digests, sources_after=dict(digests),
                            duration=1.0, scratch=tmp_path, keep=True)
    assert report['kind'] == 'source' and report['backend_sha256'] is None and report['source_sha256'] == digests
    assert [c['name'] for c in report['check_results']] == list(v.PLANNED) and report['all_planned_checks_recorded']
    assert report['passed'] is False and report['checks'] == ['backend_inputs_stable_during_campaign']
    assert report['duration_budget_seconds'] == 240 and report['scratch_preserved'] == str(tmp_path)
    changed = dict(digests, **{'scripts/validate_phase20.py': '0' * 64})
    campaign = stub()
    campaign.finish()
    moved = v.build_report(campaign, backend=None, backend_sha=None, sources=digests, sources_after=changed,
                           duration=1.0, scratch=tmp_path, keep=False)
    assert 'backend_inputs_stable_during_campaign' in moved['failed'] and moved['scratch_preserved'] is None


def test_the_proposed_phase20_suite_registration_runs_only_in_the_phase20_namespace():
    """The parent patch (track note): SUITES gains 'phase20' beside 'phase20-faults'. Applied here to a loaded copy
    of the verifier only; no earlier campaign changes and the verifier's 240 s limit is the validator's budget."""
    verify = load_script('verify_harness_candidate')
    before = {phase: verify.default_suites(phase) for phase in verify.PHASES}
    suites = {}
    for name, script in verify.SUITES.items():
        if name == 'phase20-faults':
            suites['phase20'] = 'validate_phase20.py'
        suites[name] = script
    verify.SUITES = suites
    after = {phase: verify.default_suites(phase) for phase in verify.PHASES}
    assert {phase: after[phase] for phase in verify.PHASES[:-1]} == {phase: before[phase] for phase in verify.PHASES[:-1]}
    assert after['phase20'] == before['phase19'] + ['phase20', 'phase20-faults']
    assert (SCRIPTS / verify.SUITES['phase20']).is_file()
    source = (SCRIPTS / 'verify_harness_candidate.py').read_text(encoding='utf-8')
    assert 'process.wait(timeout=240)' in source and v.VERIFIER_SECONDS == 240


def test_checks_are_planned_unique_and_pending_is_never_passed():
    campaign = stub()
    assert campaign.check('fixture_scratch_created_outside_the_checkout', lambda: campaign.need('missing')) is False
    assert 'Prerequisite' in campaign.checks[-1]['error']
    for bad in ('fixture_scratch_created_outside_the_checkout', 'an_unplanned_check'):
        with pytest.raises(AssertionError):
            campaign.check(bad, lambda: None)

    class Missing:
        @staticmethod
        def rpc(method, **params):
            raise RuntimeError('Unknown request.')
    name = 'w1_the_rewind_routes_refuse_the_pet_and_every_other_surface'
    assert campaign.check(name, lambda: campaign.refused(Missing, 'rewind_preview', chat_id='c', run_id='r')) is False
    assert campaign.checks[-1]['gap'] == 'routes' and 'rewind_preview' in campaign.checks[-1]['pending']
    campaign.observations['scenario_errors'] = {'lock_faults': 'Not started: its bound no longer fits.'}
    campaign.finish()
    recorded = {c['name']: c for c in campaign.checks}
    assert set(recorded) == set(v.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert not any(c['passed'] for c in campaign.checks)
    assert 'bound no longer fits' in recorded['f8_1_the_run_does_not_stay_running_once_the_database_is_free']['error']


def test_a_scenario_that_no_longer_fits_the_budget_is_not_started():
    campaign = stub()
    ran = []
    campaign.deadline = v.time.monotonic() + v.SCENARIO_BOUNDS['lock_faults'] - 1
    campaign.scenario('lock_faults', lambda: ran.append(True))
    assert not ran and 'Not started' in campaign.observations['scenario_errors']['lock_faults']
    campaign.scenario('static', lambda: ran.append(True))
    assert ran == [True] and 'static' in campaign.observations['scenario_seconds']


# Static checks ----------------------------------------------------------------------------------------------------

def test_static_checks_pass_on_this_checkout():
    campaign = stub()
    campaign.static_checks()
    assert [c['name'] for c in campaign.checks] == list(v.SCENARIOS['static'])
    assert all(c['passed'] for c in campaign.checks), json.dumps([c.get('error') for c in campaign.checks])[:3000]


@pytest.mark.parametrize('change, expected', [
    (('&& !["state",', '&& !["state","rewind_apply",'), 'pet window reaches a rewind route'),
    ((' || method=="rewind_preview" || method=="rewind_apply"', ''), 'does not write the rewind surface'),
])
def test_the_shell_check_fails_when_the_pet_reaches_rewind_or_the_shell_stops_writing_its_surface(
        tmp_path, monkeypatch, change, expected):
    text = v.MAIN_RS.read_text(encoding='utf-8')
    changed = text.replace(*change, 1)
    assert changed != text, 'main.rs no longer has the anchor this test edits.'
    hostile = tmp_path / 'main.rs'
    hostile.write_text(changed, encoding='utf-8')
    monkeypatch.setattr(v, 'MAIN_RS', hostile)
    campaign = stub()
    campaign.static_checks()
    found = campaign.checks[0]
    assert not found['passed'] and 'pending' not in found and expected in found['error'], found


def test_the_hosts_texts_the_validator_matches_are_this_checkouts():
    from pebble import diagnostics, research, runner, testing
    assert research.PROGRESS_HEAD.startswith(v.COVERAGE_HEAD)
    source = Path(runner.__file__).read_text(encoding='utf-8')
    assert v.HOST_LINE in source and v.H1C in source and 'HOST RESUME:' in source
    assert runner.HELD_RESUME.strip() and runner.WITHHELD_NOTE.strip() and set(runner.WITHHELD) == {
        'approval', 'completion_check', 'promise_check'}
    text = Path(diagnostics.__file__).read_text(encoding='utf-8')
    assert 'HOST DIAGNOSTICS (untrusted tool data): ' in text and v.LARGE_DIAGNOSTICS in text
    assert diagnostics.SHOWN_CHARS['large'] == 5000
    assert v.REFUSED_AFTER_REVIEW in Path(testing.__file__).read_text(encoding='utf-8')
    assert v.SMALL_DIAGNOSTICS.fullmatch('HOST DIAGNOSTICS (untrusted tool data): 1 error, 1 warning in 1 current file '
                                         'from npm-lint; the recipe output has the locations.')


def test_the_lint_fixture_parses_to_the_diagnostics_the_validator_expects(tmp_path):
    """The fixture's stylish output, as the recipe prints it in its snapshot, binds both entries to app.js."""
    from pebble import diagnostics
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    output = ('\n> phase20-lint-fixture@1.0.0 lint\n> node lint.js\n\n' + str(snapshot / 'app.js') + '\n'
              "  1:7  error    'unused' is assigned a value but never used  no-unused-vars\n"
              '  5:1  warning  Unexpected console statement                  no-console\n\n2 problems (1 error, 1 warning)\n')
    assert json.loads(v.LINT_PACKAGE)['scripts'] == {'lint': 'node lint.js'}
    entries = [line for line in output.splitlines() if line.startswith('  ')]
    assert len(entries) == 2 and all(line in v.LINT_SCRIPT for line in entries)
    record = diagnostics.extract('npm-lint', 'lint', 1, output, {'app.js': v.sha(v.APP_JS)}, roots=(str(snapshot),))
    assert record['counts'] == {'error': 1, 'warning': 1, 'info': 0} and record['unbound'] == 0
    assert [(d['path'], d['line'], d['column'], d['severity'], d['code']) for d in record['diagnostics']] == [
        ('app.js', 1, 7, 'error', 'no-unused-vars'), ('app.js', 5, 1, 'warning', 'no-console')]


# The Phase 19 offer at 4K and 8K, pinned ------------------------------------------------------------------------

def phase19_offer(tmp_path, tag):
    """The first request's tools for one pinned conversation, from a real App with runner.LARGE_BY_NEED empty, sent
    as the service sends it (App.send with the validator's limits: SpawnAgent's budget note names max_steps)."""
    from pebble import runner
    from pebble.app import App
    pin = v.PHASE19_OFFERS[tag]
    model, context, budget = v.PROFILES[pin['profile']]
    projects = v.write_projects(tmp_path)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    seen = []
    try:
        app.setting('permissions', {'mode': 'ask', 'tools': dict(v.TOOLS_ON)})
        app.store.set_setting('model_path', '')
        # The fields providers.save stores; without budget_version, profiles() resets the budget to the capacity.
        app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                                  'url': 'https://scripted.invalid/v1', 'model': model,
                                                  'context': context, 'budget': budget, 'tools': True,
                                                  'context_version': 1, 'budget_version': 2,
                                                  'context_mode': 'full' if budget == context else 'custom'}])
        assert (app.model.profiles()[0]['context'], app.model.profiles()[0]['budget']) == (context, budget)
        app.model.select('route')

        def complete(messages, tools, cancel, token, *rest, **options):
            seen.append(copy.deepcopy(tools))
            token(v.DONE)
            return {'role': 'assistant', 'content': v.DONE}
        app.model.complete = complete
        cid = app.new_chat(str(projects[pin['project']]))['id']
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(runner, 'LARGE_BY_NEED', frozenset())
            rid = app.send(cid, v.GOALS[tag] + ' ' + v.MARK + tag, limits=dict(v.LIMITS))['run_id']
            deadline = v.time.monotonic() + 30
            while app.active or app.runs.get(rid)['state'] in v.TRANSIENT:
                assert v.time.monotonic() < deadline, 'The pinned route did not settle.'
                v.time.sleep(0.02)
    finally:
        app.close()
    assert seen, 'The route never reached the model.'
    return [t['function']['name'] for t in seen[0]], v.tools_digest(seen[0])


@pytest.mark.parametrize('tag', sorted(v.PHASE19_OFFERS))
def test_each_pinned_small_window_offer_is_the_phase19_offer_of_this_checkout(tmp_path, tag):
    names, digest = phase19_offer(tmp_path, tag)
    pin = v.PHASE19_OFFERS[tag]
    assert (tuple(pin['tools'] or ()), pin['sha256']) == (tuple(names), digest), json.dumps(
        {'tag': tag, 'tools': names, 'sha256': digest})
    if pin['profile'] == '4k' or tag == 'o2':
        assert 'MemoryPropose' not in names  # no learning words: the Phase 19 rule
    assert 'ReadRunEvidence' in names and ('MemoryPropose' in names) == (tag == 'o8')


# Readiness contract v2 -------------------------------------------------------------------------------------------

def test_contract_v2_checks_pass_in_process(tmp_path):
    if not os.access(v.SSH_KEYGEN, os.X_OK):
        pytest.skip('Needs /usr/bin/ssh-keygen for throwaway keys.')
    campaign = stub()
    campaign.contract_checks(tmp_path / 'release')
    assert [c['name'] for c in campaign.checks] == list(v.SCENARIOS['contract'])
    assert all(c['passed'] for c in campaign.checks), json.dumps(
        {c['name']: c.get('error') for c in campaign.checks if not c['passed']})[:4000]
    evidence = {c['name']: c['evidence'] for c in campaign.checks}
    assert set(evidence['t1_with_the_shipped_empty_roots_every_production_gate_stays_unverified']['cli_gates'].values()) \
        == {'unverified'}
    harness = evidence['t1_test_roots_reach_the_check_only_through_the_harness_never_a_flag_or_variable']
    assert {g: harness['harness_gates'][g] for g in ('installed_application', 'independent_human_audit', 'signed_updates',
                                                     'real_model_quality')} == dict.fromkeys(
        ('installed_application', 'independent_human_audit', 'signed_updates', 'real_model_quality'), 'passed')


# Scripted turns ---------------------------------------------------------------------------------------------------

def test_route_tags_roots_and_children_and_ignores_host_text():
    mark = v.MARK
    root = [{'role': 'system', 'content': 'synthetic'},
            {'role': 'user', 'content': 'RETRIEVED MEMORY: an older goal ' + mark + 'o3'},
            {'role': 'user', 'content': 'Earlier turn ' + mark + 'o3'},
            {'role': 'user', 'content': 'Now explain. ' + mark + 'o5'},
            {'role': 'user', 'content': 'HOST COMPLETION CHECK: quoted ' + mark + 'a1'}]
    assert v.route(root) == ('o5', 'root')
    child = [{'role': 'user', 'content': v.ASSIGNMENT + json.dumps({'task': v.CHILD_TASK})}]
    assert '\\u00a7' in child[0]['content'] and v.route(child) == ('o7', 'child')
    assert v.route([{'role': 'user', 'content': 'No tag here.'}]) == (None, 'root')


def test_approval_scripts_stage_an_edit_and_a_test_then_draft_and_reply_after_the_resume(tmp_path):
    campaign = stub()
    projects = v.write_projects(tmp_path)
    campaign.script_approvals(projects)
    provider = campaign.provider
    messages = goal('a1')
    delta = turn(provider, messages)
    assert emitted(delta) == ['Read', 'Write']
    write = json.loads(delta['tool_calls'][1]['function']['arguments'])
    assert write == {'file_path': 'calc.py', 'content': v.CALC_FIXED}
    answer(messages, delta, lambda call: {'status': 'awaiting_user_approval', 'action_id': 'x'})
    delta = turn(provider, messages)
    assert emitted(delta) == ['RunTests']
    answer(messages, delta, lambda call: {'status': 'awaiting_user_approval', 'action_id': 'y'})
    assert turn(provider, messages)['content'] == v.DRAFT
    messages.append({'role': 'user', 'content': 'HOST RESUME: Continue committed results.'})
    assert turn(provider, messages)['content'] == v.REPLY
    # a2: the draft follows the person's edit of the file the run read; the repair rereads, then replies.
    messages = goal('a2')
    delta = turn(provider, messages)
    assert emitted(delta) == ['Read']
    answer(messages, delta, lambda call: {'content': v.NOTES})
    assert turn(provider, messages)['content'] == v.REPAIR_DRAFT
    assert (projects['a2'] / 'notes.md').read_text() == v.NOTES_CHANGED
    messages.append({'role': 'assistant', 'content': v.REPAIR_DRAFT})
    messages.append({'role': 'user', 'content': 'HOST COMPLETION CHECK: a file changed.'})
    delta = turn(provider, messages)
    assert emitted(delta) == ['Read'] and delta['tool_calls'][0]['id'] == 'a2-read-2'
    answer(messages, delta, lambda call: {'content': v.NOTES_CHANGED})
    assert turn(provider, messages)['content'] == v.REPAIR_REPLY
    assert v.DRAFT_MARK in v.DRAFT and v.REPLY_MARK in v.REPLY and v.DRAFT.count('FINAL:') == v.REPLY.count('FINAL:') == 1
    assert not provider.anomalies and not provider.errors


def test_offering_scripts_spawn_wait_and_dismiss_one_child_and_the_rest_answer_at_once():
    campaign = stub()
    campaign.script_offering()
    provider = campaign.provider
    for tag in ('o1', 'o2', 'o3', 'o4', 'o5', 'o8', 'o9'):
        assert turn(provider, goal(tag))['content'] == v.DONE
    messages = goal('o6')
    delta = turn(provider, messages)
    spawn = json.loads(delta['tool_calls'][0]['function']['arguments'])
    assert emitted(delta) == ['SpawnAgent'] and spawn['task'] == v.CHILD_TASK and json.loads(spawn['tools']) == ['Read']
    answer(messages, delta, lambda call: {'id': 'agent-1', 'generation': 1})
    delta = turn(provider, messages)
    assert emitted(delta) == ['WaitForAgents'] and json.loads(json.loads(
        delta['tool_calls'][0]['function']['arguments'])['agent_ids']) == ['agent-1']
    answer(messages, delta, lambda call: {'status': 'completed'})
    delta = turn(provider, messages)
    review = json.loads(delta['tool_calls'][0]['function']['arguments'])
    assert emitted(delta) == ['ReviewAgentResult'] and review == {'agent_id': 'agent-1', 'decision': 'dismiss', 'generation': 1}
    answer(messages, delta, lambda call: {'status': 'dismissed'})
    assert turn(provider, messages)['content'] == v.DONE
    child = [{'role': 'user', 'content': v.ASSIGNMENT + json.dumps({'task': v.CHILD_TASK})}]
    assert 'child summary' in turn(provider, child)['content']
    assert not provider.anomalies and not provider.errors


def test_the_fixture_requests_without_words_carry_none_of_the_words_that_bring_tools_back():
    from pebble import runner
    for tag in ('o1', 'o2', 'o3', 'o5', 'o6'):
        assert not runner.AGENT_WORDS.search(v.GOALS[tag]) and not runner.MEMORY_WORDS.search(v.GOALS[tag]), tag
    assert runner.MEMORY_WORDS.search(v.GOALS['o4']) and not runner.AGENT_WORDS.search(v.GOALS['o4'])
    assert runner.AGENT_WORDS.search(v.GOALS['o9'])
    assert runner.AGENT_WORDS.search(v.CHILD_TASK) is None


def test_recipe_diagnostics_rewind_and_research_scripts_emit_each_call_once_then_finish():
    campaign = stub()
    campaign.script_diagnostics()
    campaign.script_rewind()
    campaign.script_recipes()
    campaign.script_research()
    provider = campaign.provider
    expected = {'d1': [['RunRecipe'], ['Read', 'Write']], 'w1': [['Read', 'Write']] * 3 + [['RunCommand']],
                'p1': [['RunRecipe']], 'p4': [['RunRecipe']],
                'r1': [['ResearchPlan'], ['ResearchStatus'], ['ResearchStatus']]}
    arguments = {}
    for tag, steps in expected.items():
        messages, seen = goal(tag), []
        while 'tool_calls' in (delta := turn(provider, messages)):
            seen.append(emitted(delta))
            arguments.setdefault(tag, [json.loads(c['function']['arguments']) for c in delta['tool_calls']])
            answer(messages, delta, lambda call: {'status': 'applied'})
        assert seen == steps, (tag, seen)
        assert turn(provider, goal(tag)).get('tool_calls') is None  # each call is emitted once, even on a new turn
    assert not provider.anomalies and not provider.errors
    assert arguments['w1'] == [{'file_path': 'a.txt'}, {'file_path': 'a.txt', 'content': 'two\n'}]
    assert arguments['d1'] == [{'recipe': 'npm-lint'}] and arguments['p1'] == [{'recipe': 'npm-build'}]
    assert arguments['p4'] == [{'recipe': 'npm-typecheck'}]
    from pebble import recipes
    assert recipes.procedure('npm-typecheck') == 'typecheck' and recipes.REGISTRY['npm-typecheck'].kind == 'lint'
    assert set(json.loads(v.BUILD_PACKAGE)['scripts']) == {'build', 'typecheck'}
    assert json.loads(arguments['r1'][0]['questions']) == list(v.HARBOUR)


def test_unscripted_private_and_unknown_routes_are_flagged():
    provider = stub().provider
    turn(provider, [{'role': 'user', 'content': 'Nothing scripted here. ' + v.MARK + 'z9'}])
    assert provider.anomalies[-1]['reason'] == 'unscripted request'
    for body in ({'model': 'another-model', 'messages': []},
                 {'model': v.MODEL_WIDE, 'messages': [{'role': 'user', 'content': 'x', '_pebble_goal': True}]}):
        with pytest.raises(AssertionError):
            provider.respond(body)


def test_the_provider_records_the_phase20_host_messages_and_the_tool_digest():
    campaign = stub()
    campaign.script_offering()
    tools = [{'type': 'function', 'function': {'name': 'Read', 'description': 'd', 'parameters': {'type': 'object'}}}]
    messages = goal('o1') + [
        {'role': 'user', 'content': 'HOST DIAGNOSTICS (untrusted tool data): 1 error'},
        {'role': 'user', 'content': v.COVERAGE_HEAD + ' (labels only): stop'},
        {'role': 'user', 'content': 'HOST HUMAN CONSTRAINTS: use ReadRunEvidence for older turns.'}]
    turn(campaign.provider, messages, model=v.MODEL_4K, tools=tools)
    record = campaign.provider.records[-1]
    assert record['scenario'] == 'o1' and record['tools'] == ['Read'] and record['tools_sha256'] == v.tools_digest(tools)
    assert len(record['diagnostics']) == len(record['coverage']) == len(record['names_evidence']) == 1
    assert record['emitted'] == 'content'
    # The report keeps request metadata content-free: the host texts the checks read become counts.
    campaign.finish()
    report = v.build_report(campaign, backend=None, backend_sha=None, sources={}, sources_after={}, duration=0.1,
                            scratch=Path('/nonexistent'), keep=False)
    meta = report['request_metadata'][-1]
    assert (meta['diagnostics'], meta['coverage'], meta['names_evidence'], meta['completion_checks']) == (1, 1, 1, 0)
    assert 'HOST' not in json.dumps(meta) and campaign.provider.records[-1]['diagnostics']  # the campaign's copy is kept


# The command line on a small campaign (always run) ------------------------------------------------------------------

# The scenarios this test replaces with a no-op; static and setup run as written, on one real service.
REPLACED = {'contract': 'contract_checks', 'offering': 'scenario_offering', 'approvals': 'scenario_approvals',
            'research': 'scenario_research', 'diagnostics': 'scenario_diagnostics', 'rewind': 'scenario_rewind',
            'recipes': 'scenario_recipes', 'extension_faults': 'scenario_extension_faults',
            'lock_faults': 'scenario_lock_faults'}


@pytest.mark.parametrize('frozen', [False, True], ids=['source', 'backend'])
def test_the_command_line_runs_a_small_campaign_and_binds_the_backend_it_launched(tmp_path, monkeypatch, frozen):
    """main() and the real run_all on the static checks and the setup of one real service; every other scenario is
    a no-op, so its checks are recorded "Not run" and the report fails. With --backend (an owned launcher around the
    source backend: the plumbing, not a frozen binary) the service starts through it and the report binds it."""
    assert set(REPLACED) == set(v.SCENARIOS) - {'static', 'setup'}
    for method in REPLACED.values():
        monkeypatch.setattr(v.Campaign, method, lambda self, *args: None)
    temporary = tmp_path / 'tmp'
    temporary.mkdir()
    monkeypatch.setattr(v.tempfile, 'tempdir', str(temporary))  # main()'s scratch, outside the checkout
    report_path, launcher, marker = tmp_path / 'report.json', tmp_path / 'pebble-backend', tmp_path / 'launched'
    argv = ['--report', str(report_path)]
    if frozen:
        launcher.write_text('#!/bin/sh\necho launched > ' + json.dumps(str(marker)) + '\nexec ' + json.dumps(sys.executable)
                            + ' ' + json.dumps(str(ROOT / 'backend/main.py')) + ' "$@"\n')
        launcher.chmod(0o755)
        argv += ['--backend', str(launcher)]
    assert v.main(argv) == 1  # a campaign that ran only part of its scenarios never passes
    assert (report_path.stat().st_mode & 0o777) == 0o600
    report = json.loads(report_path.read_text())
    results = {c['name']: c for c in report['check_results']}
    assert report['kind'] == ('frozen' if frozen else 'source') and report['passed'] is False
    assert report['backend_sha256'] == (v.sha(launcher.read_bytes()) if frozen else None)
    assert report['backend_path'] == (str(launcher.resolve()) if frozen else None)
    assert marker.is_file() is frozen  # the service started through the launcher, and only with --backend
    assert report['all_planned_checks_recorded'] and list(results) == list(v.PLANNED)
    ran = [*v.COMMON_FIRST, *v.SCENARIOS['static'], *v.SCENARIOS['setup'], 'synthetic_provider_protocol_clean',
           'no_proxy_honouring_egress_was_attempted', 'backend_inputs_stable_during_campaign']
    assert report['checks'] == ran, {name: results[name].get('error') for name in ran if not results[name]['passed']}
    assert all(results[name].get('not_run') and not results[name]['passed']
               for scenario in REPLACED for name in v.SCENARIOS[scenario])
    assert report['failed'] == [name for name in v.PLANNED if name not in ran] and not report['pending']
    assert 'native_phase20_routes_are_never_offered_to_models' in report['failed']  # nothing was offered to compare
    services = report['observations']['services']
    assert [(s['label'], s['exit_code']) for s in services] == [('main', 0)] and services[0]['startup_seconds'] > 0
    assert report['synthetic_provider_attempts'] == 0 and report['fatal_error'] is None
    assert set(report['source_sha256']) == set(v.checkout_digests())
    scratch = Path(report['scratch_preserved'])  # kept because the report failed
    assert scratch.parent == temporary and (scratch / 'home' / 'pebble.sqlite').is_file()
