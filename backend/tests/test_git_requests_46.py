"""Phase 46 (w1-git, the lead's follow-up 2): git_workflow's cost in model requests.

Measured by the lead first (private d-gw-93 on sol, integrated fixes: passed, 73.9 s, 15 model requests, 68.7 s of it in
the model): stash, switch -c, commit, switch back and stash pop were each a ProposeGitChange of their own, each a staged
stop (D156: in Auto, Git plans wait for the person) and each a model request; and the final reply was withheld once
because app/greet.py, last seen as the run's own edit on the hotfix branch, changed when the run's own approved switch
went back to feature, which cost a Read and a second reply (about 8 s).

Several ProposeGitChange calls in one response do not help: each plan binds the repository state at review, so the
first one's effect makes the next one drift (pinned below). Two changes instead: a reviewed command's argv may list up
to 8 local commands under one review (git_plans.SEQUENCE_SUBS), and the completion check reads up to three small
changed files again itself (runner.recheck_sources), so the reply needs no Read round trip.
"""

import json
import time

import pytest

from pebble import runner
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError
from test_approval_handoff_22 import call, env, final, no_network  # noqa: F401 (fixtures)
from test_git_commands_46 import files, wip
from test_git_plans import MAC, apply, git, host, rev  # noqa: F401 (fixture)


def sequence(host, *argv):  # noqa: F811
    return apply(host, 'command', argv=json.dumps([list(item) for item in argv]))


def test_several_plans_in_one_response_drift_after_the_first_runs(host):  # noqa: F811
    wip(host)
    first = host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['stash', 'push', '-u'])})
    second = host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['switch', '-c', 'hotfix', 'main'])})
    assert host.plans.execute(host.chat, first)['state'] == 'applied'
    with pytest.raises(PolicyError, match='changed after review'):
        host.plans.execute(host.chat, second)


def test_a_sequence_is_one_review_run_in_order_with_one_undo(host):  # noqa: F811
    wip(host)
    before, base = files(host.repo), rev(host.repo)
    plan, done = sequence(host, ['stash', 'push', '-u', '-m', 'wip'], ['switch', '-c', 'hotfix', 'main'])
    assert plan['operation'] == 'command' and plan['argv'] == [['stash', 'push', '--include-untracked', '-m', 'wip'],
                                                               ['switch', '-c', 'hotfix', 'main']]
    assert sum(effect.startswith(('1. git stash', '2. git switch')) for effect in plan['effects']) == 2
    assert done['state'] == 'applied' and done['result']['ran'] == 2 and done['result']['failed_at'] is None
    assert git(host.repo, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/hotfix' and 'wip' in git(
        host.repo, 'stash', 'list').stdout
    _, undone = apply(host, 'undo', receipt_id=done['receipt_id'])
    assert undone['state'] == 'applied' and files(host.repo) == before and rev(host.repo) == base
    assert git(host.repo, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/main' and git(host.repo, 'stash',
                                                                                             'list').stdout == ''


def test_a_sequence_stops_at_the_first_command_that_fails_and_keeps_its_checks(host):  # noqa: F811
    (host.repo / 'app.py').write_text('print("staged")\n')
    # a later command is planned at its turn, with every check of a command of its own: refused there, it stops the rest
    plan, done = sequence(host, ['add', '--', 'app.py'], ['revert', 'no-such-commit'], ['commit', '-m', 'Never'])
    assert done['state'] == 'applied' and done['result']['failed_at'] == 1 and done['result']['ran'] == 2
    assert 'does not name a commit' in done['result']['commands'][1]['refused'] and 'next' in done['result']
    assert git(host.repo, 'log', '-1', '--format=%s').stdout.strip() != 'Never'
    assert 'app.py' in git(host.repo, 'diff', '--cached', '--name-only').stdout
    # and one Git itself fails stops the rest too
    (host.repo / 'app.py').write_text('print("staged again")\n')
    _, done = sequence(host, ['add', '--', 'app.py'], ['switch', 'no-such-branch'], ['commit', '-m', 'Never'])
    assert done['result']['failed_at'] == 1 and done['result']['commands'][1]['exit_code'] != 0
    assert git(host.repo, 'log', '-1', '--format=%s').stdout.strip() != 'Never'
    with pytest.raises(PolicyError, match='changed nothing'):
        sequence(host, ['switch', 'no-such-branch'], ['commit', '-m', 'Never'])
    for argv in ([['stash', 'push'], ['push', 'origin', 'main']], [['add', '-A'], ['config', 'user.name', 'X']],
                 [['add', '-A'], ['clean', '-f']], [['add', '-A'], ['bisect', 'start']], [['add', '-A']] * 9,
                 [['add', '-A'], ['commit', '-c', 'x', '-m', 'y']]):
        with pytest.raises(PolicyError):
            host.plans.prepare(host.chat, 'command', {'argv': json.dumps(argv)})
    plan = host.plans.prepare(host.chat, 'command', {'argv': json.dumps([['add', '-A'], ['commit', '-m', 'Later']])})
    (host.repo / 'other.txt').write_text('after review\n')
    with pytest.raises(PolicyError, match='changed after review'):
        host.plans.execute(host.chat, plan)


def test_the_host_reads_again_only_a_few_small_changed_files(tmp_path):
    from types import SimpleNamespace
    from pebble.policy import Workspace
    for name, size in (('a.txt', 10), ('b.txt', 20), ('big.txt', 5000)):
        (tmp_path / name).write_text('x' * size)
    reads, notes = [], []

    def execute(name, args, ws, cid, call_id=None):
        reads.append((name, args['file_path'], call_id))
        return {'path': args['file_path'], 'total_lines': 1, 'untrusted_source': (tmp_path / args['file_path']).read_text()}

    def recheck(stale, restored=True):
        app = SimpleNamespace(stale_sources=lambda rid, ws: stale, execute=execute,
                              git_restored=lambda rid, cid, ws, paths: restored,
                              runs=SimpleNamespace(note=lambda *a: notes.append(a)))
        return runner.recheck_sources(app, 'chat', 'run', Workspace(tmp_path), 32768)

    for stale in ([('a.txt', 'changed')] * 4, [('a.txt', 'changed'), ('gone.txt', 'deleted')],
                  [('big.txt', 'changed')], []):
        assert recheck(stale) == ('', False) and reads == []  # too many, deleted, too large or nothing: the model reads
    # changed by something other than the run's own Git plans (a script, a command, an approved edit): the model reads
    assert recheck([('a.txt', 'changed')], restored=False) == ('', False) and reads == []
    text, every = recheck([('a.txt', 'changed'), ('b.txt', 'changed')])
    assert every and text.startswith(runner.RECHECK_NOTE) and '"a.txt": ' in text and '"b.txt": ' in text
    assert reads == [('Read', 'a.txt', 'host:recheck_1'), ('Read', 'b.txt', 'host:recheck_2')]  # host reads only


# -- the d-gw-93 flow through the runner, counting model requests ----------------------------------------------------

TEXT = ('Set my uncommitted work aside, create the branch hotfix/readme from main, fix the typo "Helo" in README.md there '
        'and commit it as "Fix README typo", then go back to feature and bring my uncommitted work back exactly as it was. '
        'Do not push. End your reply with the line `FINAL: done`.')
REPLY = final('Fixed the typo on hotfix/readme and brought your work back on feature.\n\nFINAL: done')


def command(ident, *argv):
    return call('ProposeGitChange', {'operation': 'command', 'argv': json.dumps(list(argv))}, ident)


def gw93_repo(project):
    git(project, 'init', '-q')
    (project / 'README.md').write_text('# Greeter\n\nHelo, world.\n')
    (project / 'notes.txt').write_text('one\n')
    git(project, 'add', '-A')
    git(project, 'commit', '-q', '-m', 'Initial import')
    git(project, 'config', 'user.name', 'Reviewed Author')
    git(project, 'config', 'user.email', 'author@example.invalid')
    git(project, 'switch', '-q', '-c', 'feature')
    (project / 'farewell.txt').write_text('Bye\n')
    git(project, 'add', 'farewell.txt')
    git(project, 'commit', '-q', '-m', 'Start the farewell')
    (project / 'notes.txt').write_text('one\ntwo (draft)\n')
    (project / 'draft.md').write_text('# plan\n')


def script(grouped, recheck, settle=False):
    edit = [call('Read', {'file_path': 'README.md'}, 'read'),
            call('Edit', {'file_path': 'README.md', 'old_string': 'Helo', 'new_string': 'Hello'}, 'edit')]
    if grouped:
        steps = [command('g1', ['stash', 'push', '-u', '-m', 'wip'], ['switch', '-c', 'hotfix/readme', 'main']), *edit,
                 command('g2', ['add', '--', 'README.md'], ['commit', '-m', 'Fix README typo'], ['switch', 'feature'],
                         ['stash', 'pop'])]
    else:
        steps = [command('g1', 'stash', 'push', '-u', '-m', 'wip'), command('g2', 'switch', '-c', 'hotfix/readme', 'main'),
                 *edit, call('ProposeGitChange', {'operation': 'commit', 'paths': json.dumps(['README.md']),
                                                  'message': 'Fix README typo'}, 'g3'),
                 command('g4', 'switch', 'feature'), command('g5', 'stash', 'pop')]
    if settle:  # Phase 46 (1.9.28): the host re-read when the switch's result returned; the first reply is checked
        return steps + [REPLY]
    return steps + [REPLY] + ([] if recheck else [call('Read', {'file_path': 'README.md'}, 'reread')]) + [REPLY]


def finished(app, cid, rid, timeout=90):
    """Approve each staged Git plan as the golden harness's approve_all driver does, until the run ends."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = app.runs.get(rid)
        if app.active is None and run['state'] == 'waiting_approval':
            waiting = [a for a in app.store.actions(cid) if a['status'] == 'pending']
            if waiting:
                app.approve(cid, waiting[0]['id'])
                continue
        if app.active is None and run['state'] in ('completed', 'failed', 'held', 'budget_exhausted'):
            return run
        time.sleep(.02)
    pytest.fail(f'The run did not end: {app.runs.get(rid)["state"]}')


@pytest.mark.skipif(not MAC, reason='Confined Git plans are a macOS sandbox-exec product surface.')
# Phase 46 (1.9.28, w10-tail F3; restated): the host also reads again, when a plan's result returns, the files the run
# read that the plan itself changed or removed (runner.settle_git_sources), so the reply after the last switch is not
# withheld at all. The 1.9.27 counts are kept with that re-read off; with it on, one request fewer again.
# Phase 46 (1.9.28, D184; restated): every plan of this flow keeps its Undo (git_plans.undo_kept), so in Auto none waits
# for a card and none is a staged stop; the requests are as many as with the cards (`cards` keeps them, as in 1.9.27).
@pytest.mark.parametrize('cards', [False, True])
@pytest.mark.parametrize('grouped, recheck, settle, requests', [
    (False, False, False, 10), (False, True, False, 9), (False, True, True, 8),
    (True, False, False, 7), (True, True, False, 6), (True, True, True, 5)])
def test_the_gw93_flow_needs_fewer_model_requests(env, monkeypatch, grouped, recheck, settle, requests, cards):  # noqa: F811
    if cards:
        monkeypatch.setenv('PEBBLE_AUTO_RISKY_ONLY', '0')
    gw93_repo(env.project)
    app = env.start(32768)
    app.store.set_setting('permissions', {**DEFAULT, 'mode': 'auto'})
    monkeypatch.setattr(app.guard, 'handle', lambda *args, **kwargs: {'action': 'allow'})
    if not recheck:
        monkeypatch.setattr(runner, 'recheck_sources', lambda *args: ('', False))
    if not settle:
        monkeypatch.setattr(runner, 'settle_git_sources', lambda *args: '')
    env.script = script(grouped, recheck, settle)
    chat = app.new_chat(str(env.project))
    app.work(chat, TEXT, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    run = finished(app, chat['id'], rid)
    events = [r['kind'] for r in app.store.query('SELECT kind FROM run_events WHERE run_id=?', (rid,))]
    assert run['state'] == 'completed', (run['state'], run['error'])
    assert len(env.requests) == requests and events.count('staged_stop') == ((2 if grouped else 5) if cards else 0)
    assert ('source_recheck' in events) is (recheck or settle)
    assert ('reply_withheld' in events) is not settle
    assert git(env.project, 'log', '-1', '--format=%s', 'hotfix/readme').stdout.strip() == 'Fix README typo'
    assert git(env.project, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/feature'
    assert (env.project / 'notes.txt').read_text() == 'one\ntwo (draft)\n' and (env.project / 'draft.md').exists()
