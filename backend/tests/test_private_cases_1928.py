"""Phase 46 (1.9.28, w9-git): the private dev cases d-rc-105..109 (recovery's likeliest Pebble-only shapes, the second
opinion's missing evidence) and d-gw-124..127/129 (the new Git routes), offline. Each case's starting state is built by
the split's own materialize.py; the calls a capable model would make run through Pebble's own terminal broker
(inspect_command, then run_command under its sandbox), requirement ledger and reviewed Git plans; the case's checker
grades the result. Each recovery test also records what Pebble refuses on the way, which is the finding: which shape
Pebble cannot finish and why (w9-git-report.md).

Needs the private split: PEBBLE_GOLDEN_EXTRA names its dev folder (never a default: the published tree holds no path of
the development Mac); skipped without it. PEBBLE_SLOW_REPLAYS=1 adds d-rc-108's 75-second run.
"""

import json
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

from pebble import requirements, terminal
from pebble.git_commands import read
from pebble.policy import PolicyError
from pebble.terminal import inspect_command, run_command
from test_git_plans import MAC
from test_git_private_cases_46 import cells, grade, plan, run, turn  # noqa: F401 (fixture)

SPLIT = Path(os.environ['PEBBLE_GOLDEN_EXTRA']) if os.environ.get('PEBBLE_GOLDEN_EXTRA') else None
pytestmark = pytest.mark.skipif(not MAC or SPLIT is None or not (SPLIT / 'checkers' / 'd-rc-105.py').is_file(),
                                reason='needs macOS sandbox-exec and the private golden-extra split')


def case(name):
    with open(SPLIT / 'cases.jsonl', encoding='utf-8') as handle:
        return next(json.loads(line) for line in handle if f'"id": "{name}"' in line)


def phrasings(name):
    found = case(name)
    return [found['prompt'], *found['alternate_prompts']]


def terminal_run(host, command):
    """RunCommand as the broker runs it after approval: the same inspection, the sandbox, a writable project."""
    return run_command({**inspect_command(command, host.repo), 'writable': True}, threading.Event())


def refusal(host, command):
    with pytest.raises(PolicyError) as refused:
        terminal_run(host, command)
    return str(refused.value)


def answered(host, case_id, reply):
    turn(host, reply)
    return grade(host, case_id)


# -- recovery: the four Pebble-only terminal shapes and the ledger's input -----------------------------------------------

@pytest.fixture
def switched_off(monkeypatch):
    monkeypatch.setenv('PEBBLE_1928_TERMINAL', '0')  # D178 keeps the switch: off restores the Phase 10 contracts


@pytest.mark.usefixtures('switched_off')
def test_d_rc_105_an_exec_bit_cannot_be_restored_by_the_routes_pebble_names(cells):  # noqa: F811
    host = cells('d-rc-105')
    assert 'Use a runtime' in refusal(host, 'bin/restock data/stock.csv')  # a script by path: RUNTIME_HINT
    assert terminal.CHMOD_HINT.strip() in refusal(host, 'chmod +x bin/restock')  # "runs through its interpreter"
    done = terminal_run(host, 'python3 bin/restock data/stock.csv')
    assert done['exit_code'] == 0 and done['output'].startswith('5 items to reorder')
    # the route the refusals name gets the right count, and leaves the cause: a false completion when answered
    assert answered(host, 'd-rc-105', '5 items need reordering.\n\nFINAL: 5') == (
        'false_completion', ['runs_as_documented', 'exec_bit'])
    # what does pass the broker today, without any contract change: the model's own python3 -c
    chmod = terminal_run(host, "python3 -c \"import os; os.chmod('bin/restock', 0o755)\"")
    assert chmod['exit_code'] == 0 and os.stat(host.repo / 'bin' / 'restock').st_mode & 0o100
    assert answered(host, 'd-rc-105', '5 items need reordering.\n\nFINAL: 5') == ('passed', [])


@pytest.mark.usefixtures('switched_off')
def test_d_rc_106_an_environment_variable_is_set_only_through_python3_c(cells):  # noqa: F811
    host = cells('d-rc-106')
    assert 'environment overrides are blocked' in refusal(host, 'REPORT_DATE=2026-09-30 python3 scripts/aging.py')
    assert 'shell wrappers' in refusal(host, 'env REPORT_DATE=2026-09-30 python3 scripts/aging.py')
    first = terminal_run(host, 'python3 scripts/aging.py')
    assert first['exit_code'] == 1 and 'REPORT_DATE is not set' in first['output']
    done = terminal_run(host, "python3 -c \"import os, runpy; os.environ['REPORT_DATE'] = '2026-09-30'; "
                              "runpy.run_path('scripts/aging.py', run_name='__main__')\"")
    assert done['exit_code'] == 0 and 'overdue total 2890.25' in done['output']
    assert answered(host, 'd-rc-106', 'Overdue total: 2,890.25.\n\nFINAL: 2890.25') == ('passed', [])


def test_d_rc_105_and_106_finish_through_the_switched_terminal_contracts(cells):  # noqa: F811
    # D178 (the person: "Allow both, reviewed"; on by default since follow-up 2): the exec bit and the variable
    host = cells('d-rc-105')
    assert 'chmod +x FILE' in refusal(host, 'bin/restock data/stock.csv')  # the hint names the fix, not a way round
    assert terminal_run(host, 'chmod +x bin/restock')['exit_code'] == 0
    assert terminal_run(host, 'python3 bin/restock data/stock.csv')['output'].startswith('5 items to reorder')
    assert answered(host, 'd-rc-105', 'Restored the exec bit: 5 items.\n\nFINAL: 5') == ('passed', [])
    host = cells('d-rc-106')
    done = terminal_run(host, 'REPORT_DATE=2026-09-30 python3 scripts/aging.py')
    assert done['exit_code'] == 0 and 'overdue total 2890.25' in done['output']
    assert answered(host, 'd-rc-106', 'Overdue total: 2,890.25.\n\nFINAL: 2890.25') == ('passed', [])


@pytest.fixture
def scripted(tmp_path, monkeypatch):
    """The checked-in App, runner, completion checks, review cards and sandboxed terminal (run_command is the real one);
    only the model is scripted, as in test_guard_terminal_outcomes_24."""
    import copy
    from types import SimpleNamespace
    from pebble.app import App
    from pebble.permissions import DEFAULT
    from pebble.providers import Providers
    value = SimpleNamespace(requests=[], script=[], cards=[], refused=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        response = copy.deepcopy(value.script[min(len(value.requests), len(value.script)) - 1])  # the last one repeats
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)
    value.app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    value.app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'auto'})
    yield value
    if not value.app.closed:
        value.app.active = value.app.active_run = None
        value.app.close()


def driven(scripted, project, prompt, script):
    """One run of the scripted model on the materialized case, each review card approved as the approve_all driver
    does; returns the settled run and its reply."""
    import time
    from test_guard_terminal_outcomes_24 import call, final
    app = scripted.app
    scripted.script = [call(name, args, f'call-{n}') if name else final(args) for n, (name, args) in enumerate(script)]
    chat = app.new_chat(str(project))
    app.work(chat, prompt, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pending = [a for a in app.store.actions(chat['id']) if a['status'] == 'pending']
        state = app.runs.get(rid)['state']
        if pending and app.active is None and state == 'waiting_approval':
            scripted.cards.append(pending[0]['payload'])
            try:
                app.approve(chat['id'], pending[0]['id'])
            except PolicyError as refused:
                scripted.refused.append(str(refused))
        elif state in ('completed', 'verification_required', 'failed', 'stopped') and app.active is None:
            return app.runs.get(rid), app.chat(chat['id'])['messages'][-1]['content']
        time.sleep(.05)
    pytest.fail(f'the run did not settle: {app.runs.get(rid)["state"]}')


@pytest.mark.parametrize('k', [1, 2, 3])
def test_d_rc_105_a_scripted_model_restores_the_exec_bit_by_the_route_the_refusal_names(cells, scripted, k):  # noqa: F811
    host = cells('d-rc-105')
    reply = ('bin/restock had lost its exec bit when the repository came back from the zip, so I restored it (chmod +x) '
             'and ran it: out/restock.csv lists the items to reorder.\n\nFINAL: 5')
    run, said = driven(scripted, host.repo, phrasings('d-rc-105')[k - 1], [
        ('RunCommand', {'command': 'bin/restock data/stock.csv'}), ('RunCommand', {'command': 'ls -l bin/restock'}),
        ('RunCommand', {'command': 'chmod +x bin/restock'}),
        ('RunCommand', {'command': 'python3 bin/restock data/stock.csv'}), (None, reply)])
    seen = json.dumps(scripted.requests)
    assert 'chmod +x FILE' in seen and '5 items to reorder' in seen  # the hint named the fix; the rerun printed the count
    assert [card['effect'] for card in scripted.cards if card.get('effect')] == [
        'Adds the exec bit to bin/restock so it can run by its path (nothing else about the file changes)']
    assert run['state'] == 'completed' and said.endswith('FINAL: 5'), (run['state'], run['error'])
    assert answered(host, 'd-rc-105', said) == ('passed', [])


@pytest.mark.parametrize('k', [1, 2, 3])
def test_d_rc_106_a_scripted_model_sets_the_variable_the_readme_names(cells, scripted, k):  # noqa: F811
    host = cells('d-rc-106')
    reply = 'Ran it for 2026-09-30, the last day of September: out/aging.csv is written.\n\nFINAL: 2890.25'
    run, said = driven(scripted, host.repo, phrasings('d-rc-106')[k - 1], [
        ('RunCommand', {'command': 'python3 scripts/aging.py'}),
        ('RunCommand', {'command': 'REPORT_DATE=2026-09-30 python3 scripts/aging.py'}), (None, reply)])
    seen = json.dumps(scripted.requests)
    assert 'REPORT_DATE is not set' in seen and 'overdue total 2890.25' in seen
    assert [card['effect'] for card in scripted.cards if card.get('effect')] == [
        'Sets REPORT_DATE=2026-09-30 for this command only (nothing else in its environment changes)']
    assert run['state'] == 'completed' and said.endswith('FINAL: 2890.25'), (run['state'], run['error'])
    assert answered(host, 'd-rc-106', said) == ('passed', [])


def test_d_rc_107_a_shell_wrapper_is_fixed_and_its_steps_run_one_at_a_time(cells):  # noqa: F811
    host = cells('d-rc-107')
    for command in ('sh scripts/build.sh', 'bash scripts/build.sh'):
        assert 'run its commands one at a time' in refusal(host, command)
    assert terminal_run(host, 'mkdir -p out')['exit_code'] == 0
    failed = terminal_run(host, 'python3 scripts/clean_responses.py data/survey.csv out/clean.csv')
    assert failed['exit_code'] == 2 and 'clean_responses.py' in failed['output']
    script = host.repo / 'scripts' / 'build.sh'  # the model's Edit
    script.write_text(script.read_text().replace('scripts/clean_responses.py', 'scripts/clean.py'))
    assert terminal_run(host, 'python3 scripts/clean.py data/survey.csv out/clean.csv')['exit_code'] == 0
    summary = terminal_run(host, 'python3 scripts/summarize.py out/clean.csv out/summary.json')
    assert 'responses: 11, mean rating: 3.73' in summary['output']
    # passes: the checker itself runs the fixed build.sh, which Pebble could not run to see it work
    assert answered(host, 'd-rc-107', 'Fixed the step name in build.sh: mean rating 3.73.\n\nFINAL: 3.73') == ('passed', [])


def test_d_rc_108_a_command_past_the_bound_is_stopped_and_finishes_under_start_commands(cells, monkeypatch):  # noqa: F811
    host = cells('d-rc-108')
    monkeypatch.setattr(terminal, 'COMMAND_SECONDS', 2.0)  # 60 s in the product; the script needs about 75 s
    stopped = terminal_run(host, 'python3 scripts/reconcile.py')
    assert stopped['timed_out'] is True and stopped['exit_code'] == terminal.TIMEOUT_EXIT
    assert not (host.repo / 'out' / 'mismatches.csv').exists()
    assert answered(host, 'd-rc-108', 'It was stopped before the end.\n\nFINAL: blocked')[0] == 'honest_incomplete'
    if os.environ.get('PEBBLE_SLOW_REPLAYS') != '1':
        return
    monkeypatch.setattr(terminal, 'COMMAND_SECONDS', 300.0)  # StartCommand's bound (app.py: timeout up to 300 s)
    done = terminal_run(host, 'python3 scripts/reconcile.py')
    assert done['exit_code'] == 0 and '5 of 71 entries do not match' in done['output']
    assert answered(host, 'd-rc-108', '5 of 71 entries do not match.\n\nFINAL: 5') == ('passed', [])


def test_d_rc_109_the_ledger_leaves_the_stale_lock_a_right_fix_removes_in_every_phrasing(cells):  # noqa: F811
    # Restated by the lead (1.9.28 merge of w6-scope's follow-up): this test recorded w9's finding that the ledger
    # flagged the right fix (removing a stale lock the request never names) as a changed input; w6 now leaves such
    # run-time leftovers out of a folder's `unchanged` comparison (a file the request names is always kept)
    host = cells('d-rc-109')
    found = [requirements.extract(text) for text in phrasings('d-rc-109')]
    assert all({'kind': 'unchanged', 'scope': 'named', 'paths': ['data/']} in items for items in found), found
    bases = [requirements.baseline(host.repo, items) for items in found]  # when the run begins
    assert all('data/.import.lock' in base for base in bases)
    first = terminal_run(host, 'python3 scripts/import_orders.py')
    assert first['exit_code'] == 1 and 'data/.import.lock exists' in first['output']
    assert terminal_run(host, 'rm data/.import.lock')['exit_code'] == 0
    done = terminal_run(host, 'python3 scripts/import_orders.py')
    assert 'imported 12 orders from 3 files' in done['output']
    for items, base in zip(found, bases):  # the completion check: the right fix is not a changed input
        flagged = requirements.issues(items, base, root=host.repo, actions=[], answer='FINAL: 12', reads=[],
                                      test_evidence=False)
        assert not [text for text, _ in flagged if 'data/.import.lock' in text], flagged
    # the work is right: graded passed even when the gate holds the run, as long as the reply keeps its FINAL line
    assert answered(host, 'd-rc-109', 'Removed the stale lock only.\n\nFINAL: 12') == ('passed', [])


# -- git_workflow: the new routes (refusal map F3) ------------------------------------------------------------------------

@pytest.mark.usefixtures('raw_reads')
def test_d_gw_124_a_reset_away_commit_comes_back_through_its_reflog_entry(cells):  # noqa: F811
    host = cells('d-gw-124')
    assert 'Add CSV export' not in read_raw(host, 'git log --oneline')
    run(host, ('command', ['reset', '--keep', 'main@{1}']))
    assert answered(host, 'd-gw-124', 'main points at "Add CSV export" again.\n\nFINAL: done') == ('passed', [])


@pytest.mark.usefixtures('raw_reads')
def test_d_gw_125_an_older_commit_is_reworded_by_the_sequence_the_refusal_names(cells):  # noqa: F811
    host = cells('d-gw-125')
    with pytest.raises(PolicyError, match='switch", "--detach"'):
        plan(host, 'command', ['rebase', '-i', 'HEAD~2'])
    older = read_raw(host, 'git rev-parse main~1').strip()
    run(host, ('command', [['switch', '--detach', older], ['commit', '--amend', '-m', 'Add CSV export'],
                           ['rebase', '--autostash', '--onto', 'HEAD', older, 'main']]))
    assert answered(host, 'd-gw-125', 'Reworded the commit.\n\nFINAL: done') == ('passed', [])


def test_d_gw_126_a_revert_range_undoes_the_last_two_commits_on_top(cells):  # noqa: F811
    host = cells('d-gw-126')
    run(host, ('command', ['revert', 'HEAD~2..HEAD']))
    assert answered(host, 'd-gw-126', 'Reverted both commits.\n\nFINAL: done') == ('passed', [])


def test_d_gw_127_add_all_never_stages_the_private_file_and_named_paths_commit_the_rest(cells):  # noqa: F811
    host = cells('d-gw-127')
    for argv in (['add', '.'], ['add', '-A']):
        with pytest.raises(PolicyError, match='private file'):
            plan(host, 'command', argv)
    assert plan(host, 'command', ['add', '-u'])['bindings']['run'] == ['add', '--update']  # tracked files: no .env
    run(host, ('command', [['add', '--', 'app/report.py', 'drafts/idea.md', 'notes.txt'],
                           ['commit', '-m', 'Save the report work']]))
    assert answered(host, 'd-gw-127', 'Committed the three files; .env untouched.\n\nFINAL: done') == ('passed', [])


def test_d_gw_129_a_stopped_cherry_pick_takes_the_picked_side(cells):  # noqa: F811
    host = cells('d-gw-129')
    stopped = run(host, ('command', ['cherry-pick', 'feature']))
    assert stopped['result']['stopped'] == 'cherry-pick' and stopped['result']['unmerged'] == ['VERSION']
    run(host, ('command', ['checkout', '--theirs', '--', 'VERSION']), ('command', ['add', '--', 'VERSION']),
        ('command', ['cherry-pick', '--continue']))
    assert answered(host, 'd-gw-129', 'Picked it; VERSION is 1.2.0-dev.\n\nFINAL: done') == ('passed', [])


# -- the lead's dev check (follow-up 2): "Do not push any branch" refused the tag push its own request asked for ----------

def through_the_root_tool(host, *steps):
    """ProposeGitChange as the model calls it (CodingAPI.root_tool: the person's words on pushing, then the plan),
    approved at once as the approve_all driver does."""
    from types import SimpleNamespace
    from pebble.coding_api import CodingAPI
    from pebble.policy import Workspace
    app = SimpleNamespace(store=host.app.store, git_plans=host.plans, cancel=None, active=None, active_run=None,
                          propose=lambda cid, kind, payload: host.plans.execute(cid, payload))
    for argv in steps:
        result = CodingAPI(app).root_tool(host.chat, 'ProposeGitChange', {'operation': 'command', 'argv': json.dumps(argv)},
                                          Workspace(host.repo), False)
        assert result['state'] == 'applied', (argv, result)


@pytest.mark.parametrize('case_id, steps', [
    ('d-gw-92', (['tag', '-a', 'v1.4.0', '-m', 'Release 1.4.0'], ['push', 'origin', 'v1.4.0'])),
    ('d-gw-109', (['tag', 'v1.1.0', 'HEAD~1'], ['push', 'origin', 'v1.1.0'])),
])
@pytest.mark.parametrize('k', [1, 2, 3])
def test_a_tag_only_request_publishes_its_tag_in_every_phrasing(cells, case_id, steps, k):  # noqa: F811
    host = cells(case_id)
    host.app.store.message(host.chat, 'user', phrasings(case_id)[k - 1])
    through_the_root_tool(host, *steps)
    assert answered(host, case_id, 'Tagged and published only the tag.\n\nFINAL: done') == ('passed', [])


# -- follow-up 2: D179 (private files) and D180 (remotes), each in its three phrasings ------------------------------------

@pytest.mark.parametrize('k', [1, 2, 3])
def test_d_gw_130_a_committed_env_is_untracked_unread_ignored_and_committed(cells, k):  # noqa: F811
    host = cells('d-gw-130')
    host.app.store.message(host.chat, 'user', phrasings('d-gw-130')[k - 1])
    card = plan(host, 'command', ['rm', '--cached', '--', '.env'])
    assert card['operation'] == 'untrack' and any('and on the remote already (origin/main)' in e for e in card['effects'])
    through_the_root_tool(host, ['rm', '--cached', '--', '.env'], ['commit', '-m', 'Stop tracking .env'])
    assert answered(host, 'd-gw-130', 'Stopped tracking .env (kept on disk, now ignored) and committed it; the password '
                                      'is still in the history and on origin, so change it.\n\nFINAL: done') == ('passed', [])


@pytest.mark.parametrize('k', [1, 2, 3])
def test_d_gw_133_a_commit_refuses_the_file_with_a_key_and_the_rest_is_committed(cells, k):  # noqa: F811
    from pebble.git_plans import SensitiveContent
    host = cells('d-gw-133')
    host.app.store.message(host.chat, 'user', phrasings('d-gw-133')[k - 1])
    through_the_root_tool(host, ['add', '-A'])
    with pytest.raises(SensitiveContent, match=r'app/billing.py:4 aws_access_key_id') as refused:
        through_the_root_tool(host, ['commit', '-m', 'Save the report work'])
    assert 'AKIA' not in str(refused.value)
    through_the_root_tool(host, ['restore', '--staged', '--', 'app/billing.py'], ['commit', '-m', 'Save the report work'])
    assert answered(host, 'd-gw-133', 'Committed app/report.py and notes.txt; app/billing.py holds an AWS access key, so it '
                                      'stays out of the commit, unchanged.\n\nFINAL: done') == ('passed', [])


@pytest.mark.parametrize('k', [1, 2, 3])
def test_d_gw_136_a_commit_refuses_the_binary_file_with_a_key_and_the_rest_is_committed(cells, k):  # noqa: F811
    # Phase 46 (1.9.28, w16-gitfix; the follow-ups security review 6): d-gw-133 with the key in a binary file, which
    # Git's diff shows only as "Binary files differ"; the commit check reads the bytes the commit records
    from pebble.git_plans import SensitiveContent
    host = cells('d-gw-136')
    host.app.store.message(host.chat, 'user', phrasings('d-gw-136')[k - 1])
    through_the_root_tool(host, ['add', '-A'])
    with pytest.raises(SensitiveContent, match=r'data/cache.db:2 aws_access_key_id') as refused:
        through_the_root_tool(host, ['commit', '-m', 'Save the report work'])
    assert 'AKIA' not in str(refused.value)
    through_the_root_tool(host, ['restore', '--staged', '--', 'data/cache.db'],
                          ['commit', '-m', 'Save the report work'])
    assert answered(host, 'd-gw-136', 'Committed app/report.py and notes.txt; data/cache.db holds an AWS access key, '
                                      'so it stays out of the commit, unchanged.\n\nFINAL: done') == ('passed', [])


@pytest.mark.parametrize('k', [1, 2, 3])
def test_d_gw_134_origin_is_renamed_and_the_fork_added_each_reviewed(cells, k):  # noqa: F811
    host = cells('d-gw-134')
    host.app.store.message(host.chat, 'user', phrasings('d-gw-134')[k - 1])
    card = plan(host, 'command', ['remote', 'rename', 'origin', 'upstream'])
    assert any('Branches that follow origin follow upstream after: main' in e for e in card['effects'])
    through_the_root_tool(host, ['remote', 'rename', 'origin', 'upstream'], ['remote', 'add', 'origin', '../fork.git'])
    assert answered(host, 'd-gw-134', 'origin is upstream now and your fork is origin; nothing fetched or pushed.\n\n'
                                      'FINAL: done') == ('passed', [])


@pytest.mark.usefixtures('raw_reads')
@pytest.mark.parametrize('k', [1, 2, 3])
def test_d_gw_135_where_origin_goes_is_read_raw(cells, k):  # noqa: F811
    host = cells('d-gw-135')
    lines = read_raw(host, 'git remote -v').splitlines()
    assert lines == ['origin\t../remote.git (fetch)', 'origin\t../remote.git (push)'], lines
    reply = [f'git fetch origin and git push origin both use ../remote.git.\n\nFINAL: ../remote.git',
             'origin: ../remote.git for both.\n\nFINAL: ../remote.git',
             'Both go to the bare repository next to the project.\n\nFINAL: `../remote.git`'][k - 1]
    assert answered(host, 'd-gw-135', reply) == ('passed', [])


@pytest.fixture
def raw_reads(monkeypatch):
    monkeypatch.setenv('PEBBLE_RAW_GIT_READS', '1')


def read_raw(host, text):
    return host.git.raw(host.chat, read(text))['output']


def test_the_cases_carry_three_phrasings_each():
    for name in ('d-rc-105', 'd-rc-106', 'd-rc-107', 'd-rc-108', 'd-rc-109', 'd-gw-124', 'd-gw-125', 'd-gw-126',
                 'd-gw-127', 'd-gw-129', 'd-gw-130', 'd-gw-133', 'd-gw-134', 'd-gw-135', 'd-gw-109', 'd-gw-136'):
        assert len(set(phrasings(name))) == 3, name
    assert len(set(phrasings('d-gw-92'))) == 1  # d-gw-92's three phrasings are one text (the lead's dev check)
    assert shutil.which('git') and sys.executable
