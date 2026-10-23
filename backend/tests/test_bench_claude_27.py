"""Phase 27 (D20/D21/D25): the Pebble-against-Claude-Code benchmark: the Claude Code driver, the Anthropic evaluation
provider, the ``claude`` plan kind and the USD 100 spend meter.

Nothing here starts Claude Code, a backend or a model, opens a connection or reads a credential: the Claude Code turns
are scripted stand-ins that write the corpus reference answers, the Pebble cells are a stub of live.run_cells, and the
key prompt is a stub that returns a fixture string which must never appear in any file or report.
"""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks import corpus_24a as corpus  # noqa: E402
from benchmarks import plan_24a as bp  # noqa: E402
from benchmarks.corpus_24a.references import REFERENCES  # noqa: E402
from benchmarks.drivers import anthropic_route as ar  # noqa: E402
from benchmarks.drivers import claude_cell as cc  # noqa: E402
from pebble import evaluation as ev  # noqa: E402
from pebble.eval_registry import EvidenceRegistry  # noqa: E402
from pebble.evaluation import EvaluationError  # noqa: E402

PRICES = {'input': 3.0, 'cache_read': 0.3, 'output': 15.0}
BUDGET = {'usd_cap': 100, 'stop_at_usd': 90, 'prices_per_mtok': PRICES,
          'prices_source': 'https://www.anthropic.com/pricing', 'prices_retrieved': '2026-10-23'}
MODEL = 'claude-sonnet-5-5'
NONE_INSTALLED = {'connection_ids': set(), 'host_ids': set(), 'stores': 0}
FIXTURE_KEY = 'sk-ant-' + 'fixture' * 4  # built at run time: never a literal key-shaped string in the source


def load_cli():
    spec = importlib.util.spec_from_file_location('bench_claude', ROOT / 'scripts/bench_claude.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def claude_plan(seed=27, **changes):
    return bp.make_plan('claude', 'pilot', seed=seed, model=MODEL, budget={**BUDGET, **changes})


def config_dir(tmp_path, **files):
    folder = tmp_path / 'claude-config'
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir(mode=0o700)
    for name, text in files.items():
        (folder / name).write_text(text)
    return folder


# -- the plan ----------------------------------------------------------------------------------------------------------

def test_existing_plan_kinds_keep_their_exact_shape():
    codex, harness = bp.make_plan('codex', 'pilot', seed=5), bp.make_plan('harness', 'pilot', seed=5)
    for plan in (codex, harness):
        assert 'budget' not in plan and plan['fairness'] == list(bp.FAIRNESS)
        assert plan['route']['model'] == 'gpt-6-luna' and plan['route']['kind'] == 'chatgpt'
    assert bp.make_plan('codex', 'pilot', seed=5, model='gpt-6.1-sol')['route']['model'] == 'gpt-6.1-sol'


def test_a_claude_plan_pins_model_budget_and_claims():
    plan = claude_plan()
    assert plan['products'] == ['pebble', 'claude_code'] and plan['route'] == {**plan['route'], 'kind': 'anthropic',
                                                                               'model': MODEL, 'effort': 'medium'}
    assert plan['budget']['stop_at_usd'] == 90 and plan['thresholds'] == bp.CLAUDE_CLAIMS
    assert plan['totals']['cells'] == 90 and bp.REGISTRY_ARMS['claude_code'] == 'baseline'
    assert any('Claude Code' in rule for rule in plan['fairness']) and bp.verify_plan(plan) == plan['plan_sha256']


@pytest.mark.parametrize('change, reason', [
    ({'usd_cap': 101}, 'budget_cap_invalid'), ({'stop_at_usd': 100}, 'budget_stop_invalid'),
    ({'prices_per_mtok': {'input': 3.0}}, 'budget_prices_invalid'), ({'prices_source': 'http://x'}, 'budget_source_invalid'),
])
def test_a_claude_plan_refuses_a_budget_beyond_d20(change, reason):
    with pytest.raises(bp.PlanRefused, match=reason):
        claude_plan(**change)


def test_models_and_budgets_belong_to_their_kind():
    with pytest.raises(bp.PlanRefused, match='model_not_for_kind'):
        bp.make_plan('claude', 'pilot', seed=1, model='gpt-6-luna', budget=BUDGET)
    with pytest.raises(bp.PlanRefused, match='budget_required'):
        bp.make_plan('claude', 'pilot', seed=1, model=MODEL)
    with pytest.raises(bp.PlanRefused, match='budget_not_for_kind'):
        bp.make_plan('codex', 'pilot', seed=1, budget=BUDGET)
    with pytest.raises(bp.PlanRefused, match='model_not_for_kind'):
        bp.make_plan('harness', 'pilot', seed=1, model='gpt-6.1-sol')


def test_cell_cost_prices_uncached_input_cache_reads_and_output():
    usage = {'input_tokens': 1_000_000, 'cached_tokens': 400_000, 'output_tokens': 100_000}
    assert bp.cell_cost_usd(usage, PRICES) == round(0.6 * 3.0 + 0.4 * 0.3 + 0.1 * 15.0, 6)
    assert bp.cell_cost_usd({'input_tokens': None, 'cached_tokens': 0, 'output_tokens': 1}, PRICES) is None
    assert bp.cell_cost_usd(None, PRICES) is None


# -- the Claude Code driver ----------------------------------------------------------------------------------------------

def test_a_scratch_config_is_private_and_holds_nothing_personal(tmp_path):
    assert cc.check_claude_config(config_dir(tmp_path)) == (tmp_path / 'claude-config').resolve()
    for index, (name, text, reason) in enumerate((('CLAUDE.md', 'be nice', 'claude_md'),
                                                  ('settings.json', '{"hooks": {}}', 'settings_personal'),
                                                  ('settings.json', '{"enabledPlugins": {}}', 'settings_personal'))):
        other = tmp_path / ('case-%d' % index)
        other.mkdir()
        with pytest.raises(cc.ClaudeRefused, match=reason):
            cc.check_claude_config(config_dir(other, **{name: text}))
    loose = tmp_path / 'loose'
    loose.mkdir(mode=0o755)
    with pytest.raises(cc.ClaudeRefused, match='not_private'):
        cc.check_claude_config(loose)
    home = tmp_path / 'home'
    personal = home / '.claude'
    personal.mkdir(parents=True, mode=0o700)
    with pytest.raises(cc.ClaudeRefused, match='is_personal'):
        cc.check_claude_config(personal, user_home=home)


def test_the_command_is_sandboxed_never_asks_has_no_web_or_mcp_and_resumes(tmp_path):
    folder = config_dir(tmp_path)
    first = cc.command('/opt/claude', MODEL, 'Fix it.', folder)
    assert first[:2] == ['/usr/bin/sandbox-exec', '-p'] and str(folder) in first[2] and '(deny file-write*' in first[2]
    assert first[3] == '/opt/claude' and first[-2:] == ['--', 'Fix it.']
    for flag in ('--print', '-p'):
        assert flag in first or flag == '--print'
    assert first[first.index('--model') + 1] == MODEL and first[first.index('--effort') + 1] == 'medium'
    assert first[first.index('--permission-mode') + 1] == 'bypassPermissions'
    assert first[first.index('--setting-sources') + 1] == 'project' and '--strict-mcp-config' in first
    assert 'WebFetch' in first and 'WebSearch' in first and '--resume' not in first
    follow = cc.command('/opt/claude', MODEL, 'And the tests?', folder, session_id='s-1')
    assert follow[follow.index('--resume') + 1] == 's-1'
    assert cc.public_command(first)[-1].startswith('<prompt sha256:')


def test_stream_json_is_parsed_into_pebble_terms():
    lines = [json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'abc'}),
             json.dumps({'type': 'assistant', 'session_id': 'abc', 'message': {'model': MODEL, 'content': [
                 {'type': 'tool_use', 'name': 'Edit'}, {'type': 'text', 'text': 'Working.'}]}}),
             json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'FINAL: done',
                         'session_id': 'abc', 'total_cost_usd': 0.12, 'num_turns': 3,
                         'usage': {'input_tokens': 100, 'cache_read_input_tokens': 900,
                                   'cache_creation_input_tokens': 50, 'output_tokens': 40}}), 'not json']
    parsed = cc.parse_events(lines)
    assert parsed['session'] == 'abc' and parsed['message'] == 'FINAL: done' and parsed['tool_calls'] == 1
    assert parsed['usage'] == {'input_tokens': 1050, 'cached_tokens': 900, 'cache_write_tokens': 50, 'output_tokens': 40}
    assert parsed['cost_usd'] == 0.12 and parsed['models'] == [MODEL] and parsed['errors'] == []


# The init event's shape as Claude Code 2.1.308 printed it in the D25 probe (names shortened).
INIT = {'type': 'system', 'subtype': 'init', 'model': MODEL, 'mcp_servers': [],
        'plugins': [{'name': 'cc-plugin-agents-md', 'path': 'builtin', 'source': 'cc-plugin-agents-md@builtin'}],
        'skills': ['debug', 'verify'], 'agents': ['Explore', 'Plan'], 'slash_commands': ['debug', 'verify'],
        'tools': ['Bash', 'Edit', 'Read', 'Write'], 'memory_paths': {'auto': '/private/tmp/x/memory/'}}


def rate_event(utilization, resets_at, status='allowed'):
    return json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {
        'status': status, 'resetsAt': resets_at, 'rateLimitType': 'five_hour', 'overageStatus': 'rejected',
        'unifiedWindows': {'five_hour': {'utilization': utilization, 'resetsAt': resets_at}}}})


PIN = lambda binary, root: (binary, 'f' * 64)  # noqa: E731 - the scripted CLI has no executable to pin


def preflight_ok(binary, config, model, personal=frozenset(), spawn=None):
    return {'ok': True, 'isolation': [], 'failure': None, 'models': [model], 'helper_models': [],
            'rate_limit': None, 'cost_usd_reported': 0.001}


class ScriptedClaude:
    """Stands in for a Claude Code turn: writes the reference reply and files, emits stream-json; nothing starts."""
    turns = {}
    model = MODEL
    replies = {}   # turn number -> reply text, in place of the reference reply
    init = {}      # changes to the init event (an isolation breach)
    rate = None    # a rate_limit_event line, or None
    result = {}    # changes to the result event (a quota refusal)

    def __init__(self, argv, cwd, env):
        task = corpus.BY_ID[Path(cwd).parent.name.rsplit('-', 1)[0]]
        ref = REFERENCES[task.id]
        turn = ScriptedClaude.turns[cwd] = ScriptedClaude.turns.get(cwd, 0) + 1
        assert argv[0] == '/usr/bin/sandbox-exec' and env['CLAUDE_CONFIG_DIR'].endswith('claude-config')
        assert ('--resume' in argv) == (turn > 1) and 'ANTHROPIC_API_KEY' not in env
        assert env['CLAUDE_CODE_SUBAGENT_MODEL'] == MODEL and (Path(env['HOME']) / 'Library/Keychains').is_symlink()
        writes = (ref.get('turn_writes') or [ref.get('writes') or {}])
        for path, text in (writes[turn - 1] if turn - 1 < len(writes) else {}).items():
            (Path(cwd) / path).write_text(text)
        reply = ScriptedClaude.replies.get(turn, ref['replies'][turn - 1])
        result = {'type': 'result', 'subtype': 'success', 'result': reply, 'total_cost_usd': 0.05,
                  'usage': {'input_tokens': 300, 'cache_read_input_tokens': 200, 'output_tokens': 20},
                  'modelUsage': {MODEL: {}, 'claude-haiku-5-5': {}}, **ScriptedClaude.result}
        self.events = ([json.dumps({**INIT, 'session_id': 'cc-%s' % task.id, **ScriptedClaude.init})]
                       + ([ScriptedClaude.rate] if ScriptedClaude.rate else [])
                       + [json.dumps({'type': 'assistant', 'message': {'model': ScriptedClaude.model, 'content': []}}),
                          json.dumps(result)])
        code = 1 if result.get('is_error') else 0
        self.child = type('Child', (), {'returncode': code, 'stderr': None, 'poll': lambda self: code})()

    @staticmethod
    def reset(model=MODEL):
        ScriptedClaude.turns, ScriptedClaude.model, ScriptedClaude.init = {}, model, {}
        ScriptedClaude.rate, ScriptedClaude.result, ScriptedClaude.replies = None, {}, {}

    def lines(self, deadline):
        return self.events, 0.3

    def stop(self):
        pass


def _stamped(tmp_path, plan):
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    bp.register(plan, ledger)
    bp.stamp(plan, ledger, decision='D21', statement='The person approved the Claude benchmark runs.')
    return ledger


def _positions(plan, product, tasks):
    return [c['position'] for c in plan['schedule'] if c['product'] == product and c['task'] in tasks]


def test_claude_code_cells_run_scripted_and_sign_content_free_results(tmp_path):
    cli = load_cli()
    plan = claude_plan(seed=31)
    ledger = _stamped(tmp_path, plan)
    cli._online = lambda: True
    ScriptedClaude.reset()
    report = cli.run_claude_code(plan, ledger=ledger, binary='/opt/claude', config_dir=config_dir(tmp_path),
                                 parent=tmp_path / 's', positions=_positions(plan, 'claude_code', ('c1-p75', 'f7-west')),
                                 probe=lambda b: {'ok': True, 'version': '2.1.308', 'missing_flags': []},
                                 signed=lambda b, c: {'signed_in': True, 'subscription': 'team'}, spawn=ScriptedClaude, pin=PIN,
                                 check=preflight_ok)
    assert bp.validate_results(report) == [] and report['product'] == 'claude_code'
    assert {c['task']: c['outcome'] for c in report['cells']} == {'c1-p75': 'passed', 'f7-west': 'passed'}
    cell = next(c for c in report['cells'] if c['task'] == 'f7-west')
    assert cell['usage'] == {'input_tokens': 1000, 'cached_tokens': 400, 'output_tokens': 40}
    assert report['versions']['model_drift_cells'] == 0 and report['versions']['reported_cost_usd'] == 0.15
    assert report['versions']['helper_models'] == ['claude-haiku-5-5'] and report['versions']['subagent_model'] == MODEL
    assert report['versions']['plan'].startswith('team subscription') and report['versions']['login'] == 'scratch'
    ledger.verify(report, schema=bp.RESULTS_SCHEMA)
    assert [e['kind'] for e in EvidenceRegistry(ledger).entries()] == ['plan', 'begin', 'end']


def test_a_reply_from_another_model_is_never_counted_as_the_plan_model(tmp_path):
    cli = load_cli()
    plan = claude_plan(seed=32)
    ledger = _stamped(tmp_path, plan)
    cli._online = lambda: True
    ScriptedClaude.reset('claude-opus-4-8')
    report = cli.run_claude_code(plan, ledger=ledger, binary='/opt/claude', config_dir=config_dir(tmp_path),
                                 parent=tmp_path / 's', positions=_positions(plan, 'claude_code', ('c1-p75',)),
                                 probe=lambda b: {'ok': True, 'version': '2.1.308', 'missing_flags': []},
                                 signed=lambda b, c: {'signed_in': True, 'subscription': 'team'}, spawn=ScriptedClaude, pin=PIN,
                                 check=preflight_ok)
    assert report['cells'][0]['outcome'] == 'error_driver' and report['cells'][0]['error'] == 'model_drift'
    assert report['versions']['model_drift_cells'] == 1


def test_claude_code_needs_the_stamp_a_sign_in_and_a_claude_plan(tmp_path):
    cli = load_cli()
    plan = claude_plan(seed=33)
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    bp.register(plan, ledger)
    kwargs = dict(ledger=ledger, binary='/opt/claude', config_dir=config_dir(tmp_path), parent=tmp_path / 's',
                  probe=lambda b: {'ok': True, 'version': '2.1.308', 'missing_flags': []},
                  signed=lambda b, c: {'signed_in': False, 'subscription': None}, spawn=ScriptedClaude, pin=PIN,
                  check=preflight_ok)
    with pytest.raises(bp.PlanRefused, match='go_ahead'):
        cli.run_claude_code(plan, **kwargs)
    bp.stamp(plan, ledger, decision='D21', statement='The person approved the Claude benchmark runs.')
    with pytest.raises(bp.PlanRefused, match='claude_config_not_signed_in'):
        cli.run_claude_code(plan, **kwargs)
    with pytest.raises(bp.PlanRefused, match='plan_kind_not_claude'):
        cli.run_claude_code(bp.make_plan('codex', 'pilot', seed=1), **kwargs)


# -- D25: the person's existing login, isolated per process, paced by the quota ----------------------------------------

def personal_config(tmp_path):
    """A stand-in for the person's own folder: plugins, a synced skill, an agent and personal settings."""
    folder = tmp_path / 'home' / '.claude'
    (folder / 'skills/synced/abc/morning').mkdir(parents=True)
    (folder / 'skills/synced/abc/morning/SKILL.md').write_text('---\nname: morning\n---\n')
    (folder / 'agents').mkdir()
    (folder / 'agents/reviewer.md').write_text('an agent')
    (folder / 'plugins/cache').mkdir(parents=True)
    (folder / 'settings.json').write_text(json.dumps({'enabledPlugins': {'vercel@x': True}, 'model': 'opus'}))
    (folder / 'projects/-private-tmp-pebble-p15-ws').mkdir(parents=True)
    (folder / 'projects/-Users-x-Projects-Mine').mkdir(parents=True)
    return folder


def test_an_existing_login_is_used_as_it_is_and_its_personal_names_are_known(tmp_path):
    folder = personal_config(tmp_path)
    with pytest.raises(cc.ClaudeRefused, match='is_personal|claude_config'):
        cc.check_claude_config(folder, user_home=tmp_path / 'home')
    before = sorted(str(p.relative_to(folder)) for p in folder.rglob('*'))
    assert cc.check_claude_config(folder, user_home=tmp_path / 'home', existing_login=True) == folder.resolve()
    assert cc.personal_names(folder) == {'morning', 'reviewer'}
    assert sorted(str(p.relative_to(folder)) for p in folder.rglob('*')) == before  # nothing written or switched off


@pytest.mark.parametrize('change, breach', [
    ({'mcp_servers': [{'name': 'graph'}]}, 'mcp_servers'),
    ({'plugins': [{'name': 'vercel', 'path': '/Users/x/.claude/plugins/cache/vercel'}]}, 'plugins'),
    ({'memory_paths': {'auto': '/a/', 'project': ['/Users/x/CLAUDE.md']}}, 'memory_files'),
    ({'skills': ['debug', 'morning']}, 'personal_skills'), ({'agents': ['reviewer']}, 'personal_skills'),
    ({'tools': ['Bash', 'WebFetch']}, 'web_tools'), ({'model': 'claude-opus-4-8'}, 'model'),
])
def test_every_turn_init_event_is_checked_for_what_loaded(change, breach):
    assert cc.isolation_breaches(INIT, MODEL, {'morning', 'reviewer'}) == []
    assert breach in cc.isolation_breaches({**INIT, **change}, MODEL, {'morning', 'reviewer'})
    assert cc.isolation_breaches(None, MODEL) == ['init_missing']


def test_the_scratch_home_links_only_the_keychains_and_the_sandbox_allows_only_config_and_keychains(tmp_path):
    home = cc.scratch_home(tmp_path / 'home')
    assert sorted(str(p.relative_to(home)) for p in home.rglob('*')) == ['Library', 'Library/Keychains']
    assert os.readlink(home / 'Library/Keychains') == str(cc.keychains())
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    profile = cc.sandbox_profile(tmp_path / 'claude-config')
    assert profile.count('(allow file-write*') == 2 and str(cc.keychains().resolve()) in profile
    env = cc.environment(tmp_path / 'claude-config', home, tmp_path, '/opt/claude', MODEL)
    assert env['CLAUDE_CODE_SUBAGENT_MODEL'] == MODEL and env['HOME'] == str(home)
    assert not {'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'HTTPS_PROXY'} & set(env)


def test_the_sign_in_check_reaches_the_keychain_by_account_and_keeps_only_two_fields(tmp_path):
    seen = []

    def run(argv, **kwargs):
        env = kwargs['env']
        seen.append((argv, env, (Path(env['HOME']) / 'Library/Keychains').is_symlink()))
        out = json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'email': 'person@example.test',
                          'orgName': 'Org', 'subscriptionType': 'team'})
        return type('Done', (), {'stdout': out, 'returncode': 0})()

    assert cc.signed_in('/opt/claude', tmp_path, run=run) == {'signed_in': True, 'subscription': 'team'}
    argv, env, linked = seen[0]
    assert argv[1:] == ['auth', 'status', '--json'] and linked and env['USER'] == env['LOGNAME'] == cc.account()
    assert not Path(env['HOME']).exists()  # the scratch HOME is gone after the check
    assert cc.environment(tmp_path, tmp_path, tmp_path, '/opt/claude')['USER'] == cc.account()


def test_the_quota_report_paces_the_cells():
    now = 1_000_000
    rate = cc.rate_summary(json.loads(rate_event(0.13, now + 3600))['rate_limit_info'])
    assert rate == {'status': 'allowed', 'resets_at': now + 3600,
                    'windows': {'five_hour': {'utilization': 0.13, 'resets_at': now + 3600}}}
    assert cc.quota_wait(None, now) == 0 and cc.quota_wait(rate, now) == 0
    high = cc.rate_summary(json.loads(rate_event(0.85, now + 3600))['rate_limit_info'])
    assert cc.quota_wait(high, now) == 3660
    assert cc.quota_wait(cc.rate_summary(json.loads(rate_event(0.85, now + 7 * 3600))['rate_limit_info']), now) is None
    refused = cc.rate_summary(json.loads(rate_event(0.5, now + 600, status='rejected'))['rate_limit_info'])
    assert cc.quota_wait(refused, now) == 660
    assert cc.failure_kind("You've hit your limit · resets 3am") == 'quota'
    assert cc.failure_kind('anything', {'status': 'rejected'}) == 'quota'
    assert cc.failure_kind('Not logged in · Please run /login') == 'auth'


def _claude_run(cli, plan, ledger, tmp_path, tasks, **changes):
    kwargs = dict(ledger=ledger, binary='/opt/claude', config_dir=config_dir(tmp_path), parent=tmp_path / 's',
                  positions=_positions(plan, 'claude_code', tasks),
                  probe=lambda b: {'ok': True, 'version': '2.1.308', 'missing_flags': []},
                  signed=lambda b, c: {'signed_in': True, 'subscription': 'team'}, spawn=ScriptedClaude, pin=PIN,
                  check=preflight_ok)
    return cli.run_claude_code(plan, **{**kwargs, **changes})


def test_a_high_window_pauses_until_it_resets_and_a_long_one_stops_the_run(tmp_path):
    cli = load_cli()
    plan = claude_plan(seed=40)
    ledger = _stamped(tmp_path, plan)
    cli._online = lambda: True
    ScriptedClaude.reset()
    ScriptedClaude.rate = rate_event(0.85, 1_003_600)
    slept = []
    report = _claude_run(cli, plan, ledger, tmp_path, ('c1-p75', 'f7-west'), sleep=slept.append,
                         clock=lambda: 1_000_000)
    assert [c['outcome'] for c in report['cells']] == ['passed', 'passed'] and slept == [3660]
    assert report['versions']['quota_pauses'] == 1 and report['versions']['quota_paused_seconds'] == 3660
    ScriptedClaude.reset()
    ScriptedClaude.rate = rate_event(0.85, 1_000_000 + 7 * 3600)
    report = _claude_run(cli, plan, ledger, tmp_path / 'b', ('c1-p75', 'f7-west'), sleep=slept.append,
                         clock=lambda: 1_000_000)
    assert [c['outcome'] for c in report['cells']] == ['passed', 'not_run']
    assert report['cells'][1]['error'] == 'quota_stop' and report['versions']['quota_stopped_cells'] == 1


def test_a_quota_refusal_mid_run_is_retained_and_the_next_cell_waits_for_the_reset(tmp_path):
    cli = load_cli()
    plan = claude_plan(seed=41)
    ledger = _stamped(tmp_path, plan)
    cli._online = lambda: True
    ScriptedClaude.reset()
    ScriptedClaude.rate = rate_event(0.5, 1_000_600, status='rejected')
    ScriptedClaude.result = {'subtype': 'error', 'is_error': True, 'result': "You've hit your limit", 'usage': {}}
    slept = []
    report = _claude_run(cli, plan, ledger, tmp_path, ('c1-p75', 'f7-west'), sleep=slept.append,
                         clock=lambda: 1_000_000)
    assert [c['error'] for c in report['cells']] == ['claude_quota', 'claude_quota'] and slept == [660]
    assert report['versions']['quota_cells'] == 2


def test_an_isolation_breach_is_never_counted_and_stops_the_run(tmp_path):
    cli = load_cli()
    plan = claude_plan(seed=42)
    ledger = _stamped(tmp_path, plan)
    cli._online = lambda: True
    ScriptedClaude.reset()
    ScriptedClaude.init = {'mcp_servers': [{'name': 'graph', 'status': 'connected'}]}
    report = _claude_run(cli, plan, ledger, tmp_path, ('c1-p75', 'f7-west'))
    assert [(c['outcome'], c['error']) for c in report['cells']] == [('error_driver', 'claude_isolation_breach'),
                                                                    ('not_run', 'isolation_stop')]
    assert report['versions']['isolation_stopped_cells'] == 1


def test_a_failed_preflight_begins_nothing(tmp_path):
    cli = load_cli()
    plan = claude_plan(seed=43)
    ledger = _stamped(tmp_path, plan)
    cli._online = lambda: True
    ScriptedClaude.reset()
    breach = lambda *a, **k: {**preflight_ok(*a, **k), 'ok': False, 'isolation': ['plugins']}  # noqa: E731
    with pytest.raises(bp.PlanRefused, match='isolation:plugins'):
        _claude_run(cli, plan, ledger, tmp_path, ('c1-p75',), check=breach)
    low = lambda *a, **k: {**preflight_ok(*a, **k), 'rate_limit': cc.rate_summary(  # noqa: E731
        json.loads(rate_event(0.95, 1_000_000 + 30 * 3600))['rate_limit_info'])}
    with pytest.raises(bp.PlanRefused, match='claude_quota_low'):
        _claude_run(cli, plan, ledger, tmp_path / 'b', ('c1-p75',), check=low, clock=lambda: 1_000_000)
    assert [e['kind'] for e in EvidenceRegistry(ledger).entries()] == ['plan']


class PreflightClaude:
    seen = []

    def __init__(self, argv, cwd, env):
        PreflightClaude.seen.append((argv, cwd, env))
        assert argv[-1] == cc.PREFLIGHT_PROMPT and (Path(env['HOME']) / 'Library/Keychains').is_symlink()
        self.child = type('Child', (), {'returncode': 0, 'stderr': None, 'poll': lambda self: 0})()

    def lines(self, deadline):
        return [json.dumps(INIT), rate_event(0.2, 2_000_000),
                json.dumps({'type': 'assistant', 'message': {'model': MODEL, 'content': [{'type': 'text', 'text': 'OK'}]}}),
                json.dumps({'type': 'result', 'subtype': 'success', 'result': 'OK', 'total_cost_usd': 0.001,
                            'usage': {'input_tokens': 5, 'output_tokens': 1}})], 0.2

    def stop(self):
        pass


def test_the_preflight_turn_checks_login_isolation_and_quota_and_leaves_no_folder(tmp_path):
    PreflightClaude.seen = []
    keychains = cc.keychains().exists()
    found = cc.preflight('/opt/claude', config_dir(tmp_path), MODEL, personal={'morning'}, spawn=PreflightClaude)
    assert found['ok'] and found['isolation'] == [] and found['rate_limit']['windows']['five_hour']['utilization'] == 0.2
    cwd = Path(PreflightClaude.seen[0][1])
    assert cwd.name == 'workspace' and cwd.parent.name.startswith('pebble-bench-preflight-')
    # The throwaway folder is gone, and removing it unlinked the Keychains link without following it.
    assert not cwd.parent.exists() and cc.keychains().exists() == keychains


def test_restore_removes_only_the_benchmarks_own_project_folders(tmp_path):
    folder = personal_config(tmp_path)
    before = cc.snapshot(folder, '2026-10-23T00:00:00+00:00')
    assert 'enabledPlugins' in before['settings_keys'] and 'vercel@x' not in json.dumps(before)
    for name in ('-private-tmp-pebble-bench-0123456789ab-claude-code-c1-p75-0-workspace',
                 '-private-tmp-pebble-bench-preflight-x1y2-workspace', '-private-tmp-pebble-claude-probe2-R9-ws',
                 '-Users-x-Projects-New'):
        (folder / 'projects' / name).mkdir()
        (folder / 'projects' / name / 'session.jsonl').write_text('{}')
    found = cc.residue(before, folder)
    assert len(found['remove']) == 3 and found['other_new_projects'] == ['-Users-x-Projects-New']
    assert found['settings_unchanged'] and found['top_level_new'] == []
    done = cc.remove_residue(before, folder)
    assert sorted(done['removed']) == sorted(found['remove']) and done['after']['remove'] == []
    assert sorted(p.name for p in (folder / 'projects').iterdir()) == [
        '-Users-x-Projects-Mine', '-Users-x-Projects-New', '-private-tmp-pebble-p15-ws']
    assert (folder / 'settings.json').exists() and (folder / 'skills/synced/abc/morning/SKILL.md').exists()


def test_a_question_asked_with_a_request_and_a_final_line_gets_its_follow_up(tmp_path):
    # The pilot's h11-booking reply: it asked with "Please send:" and ended with the FINAL line the task asks for. The
    # follow-up with the details must be sent, and the booking graded on what was written after it.
    cli = load_cli()
    plan = claude_plan(seed=44)
    ledger = _stamped(tmp_path, plan)
    cli._online = lambda: True
    ScriptedClaude.reset()
    ScriptedClaude.replies = {1: "The booking details are missing, so nothing is written yet. Please send:\n1. Room\n"
                                 "2. Date\n3. Time\n4. People\n\nFINAL: waiting on the details"}
    report = _claude_run(cli, plan, ledger, tmp_path, ('h11-booking',))
    cell = report['cells'][0]
    assert cell['outcome'] == 'passed' and cell['turns'] == 2 and cell['checks']['asked'] is True, cell


def test_the_cli_is_pinned_once_per_plan_and_an_update_cannot_reach_it(tmp_path):
    installed = tmp_path / 'node' / 'claude.exe'
    installed.parent.mkdir()
    installed.write_bytes(b'cli version one')
    installed.chmod(0o755)
    root = tmp_path / 'pebble-bench-root'
    pinned, digest = cc.pin_binary(installed, root)
    assert pinned != installed and pinned.read_bytes() == b'cli version one' and pinned.parent.parent.name == 'claude-cli'
    assert cc.pin_binary(installed, root) == (pinned, digest) and cc.unchanged(pinned, digest)
    installed.unlink()  # the installed CLI is replaced mid-run
    installed.write_bytes(b'cli version two')
    assert pinned.read_bytes() == b'cli version one' and cc.unchanged(pinned, digest)
    with pytest.raises(cc.ClaudeRefused, match='claude_binary_missing'):
        cc.pin_binary(tmp_path / 'nowhere' / 'claude.exe', root)


class SilentClaude(ScriptedClaude):
    """A CLI that never starts a session: no event at all and a failing exit, as while the install was replaced."""

    def __init__(self, argv, cwd, env):
        self.events = []
        self.child = type('Child', (), {'returncode': 1, 'stderr': None, 'poll': lambda self: 1})()


def test_a_cli_that_never_starts_is_a_driver_error_and_stops_the_run(tmp_path):
    cli = load_cli()
    plan = claude_plan(seed=45)
    ledger = _stamped(tmp_path, plan)
    cli._online = lambda: True
    ScriptedClaude.reset()
    report = _claude_run(cli, plan, ledger, tmp_path, ('c1-p75', 'f7-west', 'd5-minutes'), spawn=SilentClaude, pin=PIN)
    assert [(c['outcome'], c['error']) for c in report['cells']] == [
        ('error_driver', 'claude_unavailable'), ('not_run', 'driver_stop'), ('not_run', 'driver_stop')]
    assert report['versions']['driver_stopped_cells'] == 2 and report['versions']['claude_code_sha256'] == 'f' * 64


# -- the Anthropic evaluation provider -----------------------------------------------------------------------------------

def provider_profile(**changes):
    return {'id': 'p-eval', 'connection_id': 'p-eval', 'name': 'Benchmark Anthropic', 'kind': 'anthropic',
            'url': ar.ANTHROPIC_URL, 'model': MODEL, 'context': 1_000_000, 'budget': 1_000_000, 'has_key': True,
            'local': False, 'tools': True, **changes}


@pytest.mark.parametrize('changes, reason', [
    ({'kind': 'chatgpt'}, 'must_be_anthropic'), ({'url': 'https://example.test/v1'}, 'must_be_anthropic'),
    ({'has_key': False}, 'key_or_local'), ({'local': True}, 'key_or_local'),
    ({'note': FIXTURE_KEY}, 'holds_a_secret'), ({'api_key': 'x'}, 'holds_a_secret'),
])
def test_the_provider_profile_is_an_anthropic_key_profile_with_no_secret_in_it(changes, reason):
    with pytest.raises(EvaluationError, match=reason):
        ar.validate_provider_profile(provider_profile(**changes))


def test_the_provider_is_recorded_once_and_never_the_installed_apps(tmp_path):
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    with pytest.raises(EvaluationError, match='installed_app_provider_refused'):
        ar.record_provider(ledger, provider_profile(), installed={**NONE_INSTALLED, 'connection_ids': {'p-eval'}})
    assert ar.record_provider(ledger, provider_profile(), installed=NONE_INSTALLED) == {'evaluation_provider': 'recorded'}
    record = (ledger.root / ar.PROVIDER_FILE)
    assert stat.S_IMODE(record.stat().st_mode) == 0o600 and 'p-eval' not in record.read_text()
    with pytest.raises(EvaluationError, match='already_recorded'):
        ar.record_provider(ledger, provider_profile(), installed=NONE_INSTALLED)
    metadata = {'profile': provider_profile()}
    assert ar.require_provider(metadata, ledger, MODEL, installed=NONE_INSTALLED)['evaluation_provider'] == 'separate'
    with pytest.raises(EvaluationError, match='mismatch'):
        ar.require_provider({'profile': provider_profile(id='other', connection_id='other')}, ledger, MODEL,
                            installed=NONE_INSTALLED)
    with pytest.raises(EvaluationError, match='not_the_plan_model'):
        ar.require_provider(metadata, ledger, 'claude-opus-5-5', installed=NONE_INSTALLED)


class FakeBackend:
    saved = []
    homes = []
    preset = []    # profiles already in the home (an interrupted entry)

    def __init__(self, command, home, **kwargs):
        FakeBackend.homes.append(home)
        self.profiles = list(FakeBackend.preset)

    def rpc(self, method, **params):
        if method == 'model_save':
            FakeBackend.saved.append(params)
            assert params['kind'] == 'anthropic' and params['url'] == ar.ANTHROPIC_URL
            self.profiles = [provider_profile(model=params['model'], context=params['context'])]
            return {}
        if method == 'state':
            return {'profiles': self.profiles}
        raise AssertionError(method)

    def close(self):
        pass


def test_the_key_goes_from_the_hidden_prompt_to_the_backend_and_nowhere_else(tmp_path, capsys):
    cli = load_cli()
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    FakeBackend.saved = []
    measure = type('Measure', (), {'Backend': FakeBackend})()
    prompts = []

    def ask(text):
        prompts.append(text)
        return FIXTURE_KEY

    summary = cli.eval_provider(backend=tmp_path / 'pebble-backend', home=tmp_path / 'eval-home', ledger=ledger,
                                metadata=tmp_path / 'eval-anthropic.json', model=MODEL, ask=ask, measure=measure,
                                installed=NONE_INSTALLED)
    assert summary['evaluation_provider'] == 'recorded' and 'hidden' in prompts[0]
    assert FakeBackend.saved[0]['api_key'] == FIXTURE_KEY and FakeBackend.saved[0]['context'] == cli.CONTEXT
    metadata = tmp_path / 'eval-anthropic.json'
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o600
    for path in [metadata, *ledger.root.rglob('*')]:
        if path.is_file():
            assert FIXTURE_KEY not in path.read_text(errors='ignore'), path
    assert FIXTURE_KEY not in json.dumps(summary) and FIXTURE_KEY not in capsys.readouterr().out
    with pytest.raises(bp.PlanRefused, match='key_shape_unexpected'):
        cli.eval_provider(backend=tmp_path / 'b', home=tmp_path / 'h2', ledger=ev.EvidenceLedger(tmp_path / 'l2'),
                          metadata=tmp_path / 'm2.json', model=MODEL, ask=lambda text: 'not a key', measure=measure,
                          installed=NONE_INSTALLED)


def test_the_key_is_never_read_without_a_terminal(tmp_path):
    cli = load_cli()
    read = []
    with pytest.raises(bp.PlanRefused, match='key_entry_needs_a_terminal'):
        cli.hidden_key('key: ', terminal=False, read=lambda prompt: read.append(prompt) or FIXTURE_KEY)
    assert read == []

    def echoing(prompt):
        import getpass
        import warnings
        warnings.warn('Can not control echo on the terminal.', getpass.GetPassWarning)
        read.append(prompt)
        return FIXTURE_KEY
    with pytest.raises(bp.PlanRefused, match='key_entry_needs_a_terminal'):
        cli.hidden_key('key: ', terminal=True, read=echoing)
    assert read == [], 'the fallback that would echo the key is stopped before it reads'
    assert cli.hidden_key('key: ', terminal=True, read=lambda prompt: FIXTURE_KEY) == FIXTURE_KEY
    with pytest.raises(bp.PlanRefused, match='key_entry_needs_a_terminal'):
        cli.eval_provider(backend=tmp_path / 'b', home=tmp_path / 'h', ledger=ev.EvidenceLedger(tmp_path / 'l'),
                          metadata=tmp_path / 'm.json', model=MODEL, installed=NONE_INSTALLED)
    assert not (tmp_path / 'm.json').exists()


def test_the_home_is_absolute_and_an_interrupted_entry_is_finished_without_the_key(tmp_path, monkeypatch):
    cli = load_cli()
    measure = type('Measure', (), {'Backend': FakeBackend})()
    monkeypatch.chdir(tmp_path)
    FakeBackend.saved, FakeBackend.homes, FakeBackend.preset = [], [], []
    cli.eval_provider(backend=tmp_path / 'b', home='relative-home', ledger=ev.EvidenceLedger(tmp_path / 'l1'),
                      metadata=tmp_path / 'm1.json', model=MODEL, ask=lambda text: FIXTURE_KEY, measure=measure,
                      installed=NONE_INSTALLED)
    assert FakeBackend.homes[-1] == tmp_path / 'relative-home' and FakeBackend.homes[-1].is_absolute()
    # The first key entry stopped after model_save: one profile is saved and nothing is recorded yet.
    home = tmp_path / 'saved-home'
    home.mkdir(mode=0o700)
    (home / 'pebble.sqlite').write_bytes(b'')
    ledger = ev.EvidenceLedger(tmp_path / 'l2')
    FakeBackend.preset = [provider_profile()]
    asked = []
    summary = cli.finish_provider(backend=tmp_path / 'b', home=home, ledger=ledger, metadata=tmp_path / 'm2.json',
                                  model=MODEL, measure=measure, installed=NONE_INSTALLED)
    assert summary['evaluation_provider'] == 'recorded' and not asked and FakeBackend.saved == [
        s for s in FakeBackend.saved if s.get('api_key') == FIXTURE_KEY]
    assert stat.S_IMODE((tmp_path / 'm2.json').stat().st_mode) == 0o600
    assert ar.require_provider(json.loads((tmp_path / 'm2.json').read_text()), ledger, MODEL,
                               installed=NONE_INSTALLED)['evaluation_provider'] == 'separate'
    for preset, reason in (([], 'provider_profile_not_saved'),
                           ([provider_profile(), provider_profile(id='p2', connection_id='p2')], 'ambiguous')):
        FakeBackend.preset = preset
        with pytest.raises(EvaluationError, match=reason):
            cli.finish_provider(backend=tmp_path / 'b', home=home, ledger=ev.EvidenceLedger(tmp_path / reason),
                                metadata=tmp_path / (reason + '.json'), model=MODEL, measure=measure,
                                installed=NONE_INSTALLED)
    with pytest.raises(bp.PlanRefused, match='evaluation_home_invalid'):
        cli.finish_provider(backend=tmp_path / 'b', home=tmp_path / 'empty', ledger=ev.EvidenceLedger(tmp_path / 'l3'),
                            metadata=tmp_path / 'm3.json', model=MODEL, measure=measure, installed=NONE_INSTALLED)
    FakeBackend.preset = []


# -- the Pebble arm and the spend meter ----------------------------------------------------------------------------------

def _pebble_setup(tmp_path, monkeypatch, seed):
    cli = load_cli()
    backend = tmp_path / 'pebble-backend'
    backend.write_bytes(b'frozen candidate fixture')
    import hashlib
    plan = bp.make_plan('claude', 'pilot', seed=seed, model=MODEL, budget=BUDGET,
                        candidate_sha256=hashlib.sha256(backend.read_bytes()).hexdigest())
    ledger = _stamped(tmp_path, plan)
    ar.record_provider(ledger, provider_profile(), installed=NONE_INSTALLED)
    metadata = tmp_path / 'eval-anthropic.json'
    metadata.write_text(json.dumps({'profile': provider_profile()}))
    metadata.chmod(0o600)
    trap = type('Trap', (), {'start': lambda self: self, 'stop': lambda self: None,
                             'report': lambda self: {'attempts': 0}})
    monkeypatch.setattr(cli.live.rpc_cell, '_measure', lambda: type('M', (), {'EgressTrap': trap})())
    cli._online = lambda: True
    return cli, plan, ledger, backend, metadata


def _fake_cells(usage):
    def run_cells(plan, product, cells, route, **kwargs):
        assert product == 'pebble' and route['profile']['kind'] == 'anthropic' and route['host_id'] is None
        task = corpus.BY_ID[cells[0]['task']]
        return [{**bp.result_cell(cells[0], task, None, None, 'not_run'), 'outcome': 'passed', 'success': True,
                 'state': 'completed', 'usage': dict(usage)}]
    return run_cells


def test_pebble_cells_are_metered_and_stop_at_the_line(tmp_path, monkeypatch):
    cli, plan, ledger, backend, metadata = _pebble_setup(tmp_path, monkeypatch, seed=34)
    positions = [c['position'] for c in plan['schedule'] if c['product'] == 'pebble'][:4]
    # Each cell costs 40 USD at the plan's prices. The meter is predictive (spend plus the largest cell so far must stay
    # under the 90 USD line), so after two cells (80 USD) the third would cross it: it and the fourth never run.
    usage = {'input_tokens': 10_000_000, 'cached_tokens': 0, 'output_tokens': 666_667}
    report = cli.run_pebble(plan, ledger=ledger, metadata_path=metadata, backend=backend, parent=tmp_path / 's',
                            positions=positions, run_cells=_fake_cells(usage), installed=NONE_INSTALLED)
    assert [c['outcome'] for c in report['cells']] == ['passed', 'passed', 'not_run', 'not_run']
    assert report['versions']['route'] == 'anthropic' and report['versions']['spent_usd_after'] == 80.0
    assert all(c['error'] == 'budget_stop' for c in report['cells'] if c['outcome'] == 'not_run')
    spent = cli.spend(plan, [report], ledger)
    assert spent['spent_usd'] == report['versions']['spent_usd_after'] and spent['stop_at_usd'] == 90
    with pytest.raises(bp.PlanRefused, match='budget_exhausted'):
        cli.run_pebble(plan, ledger=ledger, metadata_path=metadata, backend=backend, parent=tmp_path / 's2',
                       prior_reports=[report], positions=positions[:1], run_cells=_fake_cells(usage),
                       installed=NONE_INSTALLED)


def test_the_meter_never_starts_a_cell_past_the_line(tmp_path, monkeypatch):
    cli, plan, ledger, backend, metadata = _pebble_setup(tmp_path, monkeypatch, seed=35)
    positions = [c['position'] for c in plan['schedule'] if c['product'] == 'pebble'][:3]
    usage = {'input_tokens': 10_000_000, 'cached_tokens': 0, 'output_tokens': 1_000_000}  # 45 USD a cell
    report = cli.run_pebble(plan, ledger=ledger, metadata_path=metadata, backend=backend, parent=tmp_path / 's',
                            positions=positions, run_cells=_fake_cells(usage), installed=NONE_INSTALLED)
    # 45 + 45 would reach the line, so only the first cell runs: the total never crosses 90 USD.
    assert [c['outcome'] for c in report['cells']] == ['passed', 'not_run', 'not_run']
    assert report['versions']['spent_usd_after'] == 45.0 and report['versions']['budget_stopped_cells'] == 2


def test_the_pebble_arm_refuses_a_foreign_provider_or_candidate(tmp_path, monkeypatch):
    cli, plan, ledger, backend, metadata = _pebble_setup(tmp_path, monkeypatch, seed=36)
    other = tmp_path / 'other-backend'
    other.write_bytes(b'not the pinned candidate')
    with pytest.raises(bp.PlanRefused, match='candidate_sha256_mismatch'):
        cli.run_pebble(plan, ledger=ledger, metadata_path=metadata, backend=other, parent=tmp_path / 's',
                       run_cells=_fake_cells({}), installed=NONE_INSTALLED)
    with pytest.raises(EvaluationError, match='installed_app_provider_refused'):
        cli.run_pebble(plan, ledger=ledger, metadata_path=metadata, backend=backend, parent=tmp_path / 's',
                       run_cells=_fake_cells({}), installed={**NONE_INSTALLED, 'connection_ids': {'p-eval'}})


def test_the_chatgpt_runner_refuses_a_claude_plan(tmp_path):
    from benchmarks.drivers import live
    with pytest.raises(bp.PlanRefused, match='claude_plan_runs_through_bench_claude'):
        live.run_product(claude_plan(seed=37), 'pebble', ledger=ev.EvidenceLedger(tmp_path / 'ledger'),
                         metadata_path=tmp_path / 'none.json')


def test_the_harness_cli_plans_a_claude_plan_from_a_budget_file(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location('bench_harness', ROOT / 'scripts/bench_harness.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    budget = tmp_path / 'budget.json'
    budget.write_text(json.dumps(BUDGET))
    assert cli.main(['plan', '--kind', 'claude', '--model', MODEL, '--budget-file', str(budget), '--seed', '9',
                     '--report', str(tmp_path / 'plan.json')]) == 0
    plan = json.loads((tmp_path / 'plan.json').read_text())
    assert plan['kind'] == 'claude' and plan['budget']['prices_per_mtok'] == PRICES and bp.verify_plan(plan)
    assert cli.main(['plan', '--kind', 'claude', '--model', MODEL, '--report', str(tmp_path / 'p2.json')]) == 2


def test_scripted_results_analyse_and_chart_end_to_end(tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET
    from benchmarks import charts
    cli, plan, ledger, backend, metadata = _pebble_setup(tmp_path, monkeypatch, seed=38)
    tasks = ('c1-p75', 'f7-west')
    usage = {'input_tokens': 20_000, 'cached_tokens': 5_000, 'output_tokens': 2_000}
    pebble = cli.run_pebble(plan, ledger=ledger, metadata_path=metadata, backend=backend, parent=tmp_path / 'p',
                            positions=_positions(plan, 'pebble', tasks), run_cells=_fake_cells(usage),
                            installed=NONE_INSTALLED)
    ScriptedClaude.reset()
    claude = cli.run_claude_code(plan, ledger=ledger, binary='/opt/claude', config_dir=config_dir(tmp_path),
                                 parent=tmp_path / 'c', positions=_positions(plan, 'claude_code', tasks),
                                 probe=lambda b: {'ok': True, 'version': '2.1.308', 'missing_flags': []},
                                 signed=lambda b, c: {'signed_in': True, 'subscription': 'team'}, spawn=ScriptedClaude, pin=PIN,
                                 check=preflight_ok)
    analysis = bp.analyse(plan, [pebble, claude], ledger)
    assert analysis['kind'] == 'claude' and set(analysis['cost_usd']) == {'pebble', 'claude_code'}
    assert analysis['claims']['rule'] == bp.CLAUDE_CLAIMS['rule'] and 'API-equivalent' in analysis['cost_note']
    rendered = charts.render_all(analysis, {'model': MODEL})
    assert sorted(rendered) == ['efficiency.svg', 'families.svg', 'paired.svg', 'success.svg']
    for name, svg in rendered.items():
        root = ET.fromstring(svg)
        text = ' '.join(t.text or '' for t in root.iter('{http://www.w3.org/2000/svg}text'))
        assert 'Pebble' in text and (name == 'paired.svg' or 'Claude Code CLI' in text) and MODEL in text, name
        assert '/Users/' not in svg and FIXTURE_KEY not in svg
