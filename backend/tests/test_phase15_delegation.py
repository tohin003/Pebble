"""Phase 15 A08: delegation that pays for itself.

Admission floors, duplicate-work refusals, advisory history, compact wait and
tree views, review evidence defaults and the ping-pong limit, through the real
SQLite tree and the real Agents tool handler. No model, network or scheduler:
child work is recorded with the host's own receipt APIs.
"""

import copy
import json
from types import SimpleNamespace

import pytest

from pebble import evaluation
from pebble.agent_manager import COMPACT_NOTE, Agents
from pebble.agent_tree import RESULTS_LIMIT, SUMMARY_CHARS, Tree
from pebble.app import App
from pebble.delegation_history import summarize
from pebble.delegation_policy import (APPROVAL_STEPS, CHILD_FLOOR_STEPS, PING_PONG_ROUNDS, ROOT_INTEGRATION_STEPS,
                                      DelegationRefused, admission, refusal_text, same_question)
from pebble.policy import PolicyError, Workspace, digest
from pebble.runtime import encoded

WINDOW = 4096
PROFILE = {'id': 'delegation-fixture', 'kind': 'openai', 'name': 'Delegation fixture',
           'url': 'https://unused.invalid', 'model': 'fixture', 'context': 8192, 'budget': 4096, 'tools': True}
LARGE = {**PROFILE, 'context': 32768, 'budget': 32768}
SOURCE = 'value = 1\n'
MARKER = 'a08-private-phrase-5d1e'


def ledger(**changes):
    base = {'max_steps': 12, 'max_seconds': 300., 'max_tokens': 12 * 3 * WINDOW, 'window': WINDOW,
            'used_steps': 0, 'dispatches': 0, 'charged_tokens': 0, 'used_seconds': 0., 'tool_receipts': 0,
            'allocated_steps': 0, 'allocated_seconds': 0., 'allocated_tokens': 0, 'nodes_left': 8}
    return {**base, **changes}


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(Agents, 'start', lambda self: None)  # no scheduler: children never run a model
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'source.py').write_text(SOURCE)
    profile = {'value': copy.deepcopy(PROFILE)}
    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(profile['value']))
    monkeypatch.setattr(app.guard, 'handle', lambda *args, **kwargs: {'action': 'allow'})
    yield SimpleNamespace(app=app, project=project, profile=profile, tree=app.agents.tree)
    app.active = app.active_run = None
    app.close()


def root(host, steps=12, prior=1, mode='ask', seconds=180):
    app = host.app
    for live in app.store.query("SELECT id FROM runs WHERE state IN ('queued','running','retry_wait')"):
        app.runs.transition(live['id'], 'stopped')  # one live root at a time
    chat = app.new_chat(str(host.project))
    config = app.runtime_config(chat, False, {'max_steps': steps, 'max_seconds': seconds})
    config['permissions']['mode'] = mode
    rid = app.runs.create(chat['id'], 'Delegate the scoped work.', config)
    app.runs.transition(rid, 'running')
    if prior:
        # Root turns committed before the tree existed; the tree backfills them as used steps.
        app.runs.response(rid, {'role': 'assistant', 'content': 'Delegating.'}, {'messages': []}, steps=prior, elapsed=0)
    app.active, app.active_run = chat['id'], rid
    app.task_policy = copy.deepcopy(config['permissions'])
    return chat['id'], rid


def spawn(host, cid, rid, task='Read source.py and report its value.', name='reader', paths=(), call='spawn', **extra):
    # A small explicit seconds budget keeps several children inside the 180-second root.
    args = {'task': task, 'name': name, 'tools': json.dumps(['Read'] + (['ProposePatch'] if paths else [])),
            'max_seconds': 20, **extra}
    if paths:
        args['write_paths'] = json.dumps(list(paths))
    return host.app.agents.tool(cid, rid, rid, 'SpawnAgent', args, call)


def owner(node):
    return 'owner-' + node['id']


def claim(host, cid, rid, node, generation=1):
    host.tree.claim(cid, rid, node['id'], generation, owner(node))


def read(host, cid, rid, node, call, path='source.py', generation=1):
    data = (host.project / path).read_bytes()
    host.tree.begin_tool(cid, rid, node['id'], generation, owner(node), call, 'Read', {'file_path': path})
    host.tree.end_tool(cid, rid, node['id'], generation, owner(node), call,
                       {'path': path, 'file_hash': digest(data), 'untrusted_source': data.decode()})
    return call


def complete(host, cid, rid, node, reads=('source.py',), text='Report: value is 1.', evidence=None, patch=None):
    """Host-side stand-in for a child worker: claim, record reads, optionally a proposal, settle."""
    claim(host, cid, rid, node)
    ids = [read(host, cid, rid, node, str(index) + ':read', path) for index, path in enumerate(reads, 1)]
    patch_id = None
    if patch:
        patch_id = 'patch-' + node['id'][:8]
        with host.app.store.lock, host.app.store.db as db:
            db.execute("INSERT INTO agent_patches VALUES(?,?,?,?,?,?,?,?,'proposed',0)",
                       (patch_id, rid, node['id'], 1, patch, digest(SOURCE.encode()), 'value = 2\n', 'p'))
    host.tree.settle(cid, rid, node['id'], 1, 'completed', {'text': text}, ids if evidence is None else evidence, owner(node))
    return ids, patch_id


def nodes(host, cid, rid):
    return host.tree.list(cid, rid)


def review(host, cid, rid, node, call='review', **args):
    return host.app.agents.tool(cid, rid, rid, 'ReviewAgentResult', {'agent_id': node['id'], 'generation': 1, **args}, call)


# -- the measured floors ---------------------------------------------------------------------

def test_floors_are_the_w6_scripted_minimal_paths():
    # One spawning turn, the child floor, the root's wait/review/final and one approval continuation.
    write = 1 + CHILD_FLOOR_STEPS['write'] + ROOT_INTEGRATION_STEPS + APPROVAL_STEPS
    read_only = 1 + CHILD_FLOOR_STEPS['read_only'] + ROOT_INTEGRATION_STEPS
    # 8 and 6 today; a runner that parks on pending approvals makes APPROVAL_STEPS 0 and the write floor 7.
    assert (write, read_only) == (7 + APPROVAL_STEPS, 6)
    kinds = {'code-median': 'write', 'files-orders': 'write', 'research-ferries': 'read_only',
             'hostile-maintenance': 'read_only', 'constraints-release': 'write', 'code-invoice': 'write',
             'files-regions': 'write', 'stale-quota': 'read_only'}
    measured = {}
    for corpus in evaluation.MINIMAL_CALLS.values():
        for case, (_, delegated) in corpus.items():
            if delegated:
                measured.setdefault(kinds[case], []).append(sum(delegated))
    # The floor is the smallest measured delegated path of its kind; none is below it.
    assert min(measured['write']) == write and min(measured['read_only']) == read_only


@pytest.mark.parametrize('kind,approvals,used,needed', [
    ('write', 1, 5, None), ('write', 1, 6, 7), ('write', 0, 6, None), ('write', 0, 7, 6),
    ('read_only', 0, 7, None), ('read_only', 0, 8, 5)])
def test_admission_admits_at_the_floor_and_names_the_numbers_below_it(kind, approvals, used, needed):
    refusal = admission(ledger(used_steps=used), kind, approvals)
    if needed is None:
        assert refusal is None
        return
    assert refusal['limit'] == 'shared_steps' and refusal['needed'] == needed and refusal['available'] == 12 - used
    assert refusal['task_kind'] == kind and sum(refusal['floor'].values()) == needed
    text = refusal_text(refusal)
    assert '(shared_steps)' in text and 'needs ' + str(needed) + ' shared steps' in text
    assert str(12 - used) + ' remain' in text and 'Nothing was spawned' in text


def test_admission_checks_child_seconds_tokens_and_a_spawning_childs_own_steps():
    late = admission(ledger(used_steps=1, used_seconds=280.), 'read_only')
    assert (late['limit'], late['needed'], late['available']) == ('shared_seconds', 50, 0)
    poor = admission(ledger(used_steps=1, charged_tokens=26 * WINDOW), 'write', 1)
    assert (poor['limit'], poor['needed'], poor['available']) == ('shared_tokens', 3 * WINDOW, WINDOW)
    # A grandchild: its parent's wait and report, and the root keeps its reserve.
    nested = admission(ledger(max_steps=64, used_steps=2), 'read_only', own={'steps_left': 1})
    assert (nested['limit'], nested['needed'], nested['available']) == ('spawner_steps', 2, 1)
    assert admission(ledger(max_steps=64, used_steps=2), 'read_only', own={'steps_left': 2}) is None
    assert admission(ledger(used_steps=5), 'read_only', own={'steps_left': 4}) is None
    crowded = admission(ledger(used_steps=6), 'read_only', own={'steps_left': 4})
    assert crowded['floor'] == {'child': 2, 'integration': 2, 'approval': 0, 'root_reserve': 3}
    assert (crowded['limit'], crowded['needed'], crowded['available']) == ('shared_steps', 7, 6)
    with pytest.raises(ValueError):
        admission(ledger(), 'swarm')
    with pytest.raises(ValueError):
        admission(ledger(), 'write', -1)


def test_approval_steps_come_from_the_root_permission_mode():
    def config(mode):
        return {'permissions': {'mode': mode, 'tools': {}}}
    assert Tree._approvals(config('ask'), 'write', False) == 1
    assert Tree._approvals(config('ask'), 'write', True) == 2  # the worktree allocation is reviewed too
    assert Tree._approvals(config('auto'), 'write', True) == 0
    assert Tree._approvals(config('full'), 'write', False) == 0
    assert Tree._approvals(config('ask'), 'read_only', False) == 0


def test_real_spawn_admits_a_write_at_the_floor_and_refuses_one_step_below(host):
    # An 8-step tree (root reserve 2), so the measured floor rather than the reserve decides.
    needed = CHILD_FLOOR_STEPS['write'] + ROOT_INTEGRATION_STEPS + APPROVAL_STEPS  # ask mode: 7
    cid, rid = root(host, steps=8, prior=8 - needed)
    admitted = spawn(host, cid, rid, paths=['out.py'])
    assert admitted['status'] == 'queued'
    assert admitted['admission'] == {'task_kind': 'write', 'shared_steps_needed': needed, 'shared_steps_left': needed}
    assert admitted['limits']['max_steps'] >= CHILD_FLOOR_STEPS['write']

    cid, rid = root(host, steps=8, prior=9 - needed)
    before = host.tree.budget(cid, rid)['allocated']
    refused = spawn(host, cid, rid, paths=['out.py'])
    assert refused['status'] == 'refused' and refused['authorization'] == 'none'
    assert {k: refused['refusal'][k] for k in ('kind', 'limit', 'needed', 'available', 'task_kind')} == {
        'kind': 'admission', 'limit': 'shared_steps', 'needed': needed, 'available': needed - 1, 'task_kind': 'write'}
    assert 'needs %d shared steps' % needed in refused['error'] and '%d remain' % (needed - 1) in refused['error']
    assert refused['delegation']['recommendation'] in {'solo', 'small_child'}
    # Nothing was spawned, leased or allocated, and the refusal can never be cited as evidence.
    assert nodes(host, cid, rid) == [] and host.tree.budget(cid, rid)['allocated'] == before
    assert not host.app.store.query('SELECT 1 FROM agent_path_leases WHERE run_id=?', (rid,))
    assert host.app.interactions.resolved_evidence({'state': 'completed', 'result': refused}) is False


def test_real_spawn_read_only_floor_and_explicit_child_steps(host):
    cid, rid = root(host, steps=8, prior=3)
    assert spawn(host, cid, rid)['admission']['shared_steps_needed'] == 5
    cid, rid = root(host, steps=8, prior=4)
    refused = spawn(host, cid, rid)
    assert (refused['refusal']['limit'], refused['refusal']['needed'], refused['refusal']['available']) == ('shared_steps', 5, 4)
    # An explicit child budget below its kind's floor is refused with both numbers.
    cid, rid = root(host, steps=64)
    small = spawn(host, cid, rid, paths=['out.py'], max_steps=2)
    assert (small['refusal']['limit'], small['refusal']['needed'], small['refusal']['available']) == ('child_steps', 3, 2)
    assert nodes(host, cid, rid) == []


def test_the_model_sees_the_typed_refusal_as_an_ordinary_tool_result(host):
    cid, rid = root(host, prior=7)
    args = {'task': 'Write out.py.', 'name': 'writer', 'tools': json.dumps(['Read', 'ProposePatch']),
            'write_paths': json.dumps(['out.py'])}
    result = host.app.execute('SpawnAgent', args, Workspace(host.project), cid, call_id='7:spawn')
    receipt = next(t for t in host.app.runs.tools(rid) if t['call_id'] == '7:spawn')
    assert receipt['state'] == 'completed' and receipt['result']['refusal']['limit'] == 'shared_steps'
    assert result['status'] == 'refused' and not host.app.interactions.resolved_evidence(receipt)


# -- duplicate work -------------------------------------------------------------------------

def test_overlapping_file_sets_are_refused_with_the_other_childs_id(host):
    cid, rid = root(host, steps=64)
    first = spawn(host, cid, rid, task='Rewrite pkg/a.py.', name='a-writer', paths=['pkg/a.py'])
    for paths in (['pkg/a.py'], ['pkg'], ['b.py', 'pkg/a.py']):
        refused = spawn(host, cid, rid, task='Rewrite the package.', name='pkg-writer', paths=paths, call='dup')
        refusal = refused['refusal']
        assert (refusal['kind'], refusal['limit'], refusal['agent_id']) == ('duplicate_work', 'file_set', first['id'])
        assert 'pkg/a.py' in refusal['paths'] and first['id'] in refused['error'] and 'WaitForAgents' in refused['error']
    assert [n['id'] for n in nodes(host, cid, rid)] == [first['id']]
    assert spawn(host, cid, rid, task='Rewrite b.py.', name='b-writer', paths=['b.py'])['status'] == 'queued'


def test_the_same_question_is_refused_under_its_name_and_hinted_under_another(host):
    cid, rid = root(host, steps=64)
    task = 'Summarise the release notes in source.py for ' + MARKER
    first = spawn(host, cid, rid, task=task, name='notes')
    again = spawn(host, cid, rid, task='  summarise the release NOTES in source.py, for ' + MARKER + '!', name='Notes', call='again')
    assert (again['refusal']['limit'], again['refusal']['agent_id']) == ('question', first['id'])
    # Refusals and hints carry host IDs and labels, never the task text.
    assert MARKER not in json.dumps(again['refusal'])
    other = spawn(host, cid, rid, task=task, name='second-opinion', call='other')
    assert other['status'] == 'queued' and other['duplicate_hint']['agent_id'] == first['id']
    assert MARKER not in json.dumps(other['duplicate_hint'])
    assert spawn(host, cid, rid, task='Count the lines in source.py.', name='notes', call='new')['status'] == 'queued'
    assert same_question('Read a b c d e f g h i j', 'read A, b c d e f g h i j.')
    assert not same_question('Summarise section 1 of report.md', 'Summarise section 2 of report.md')


def test_dismissal_releases_the_lease_so_the_root_can_take_the_work_over(host):
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid, task='Rewrite source.py.', name='writer', paths=['source.py'])
    complete(host, cid, rid, child, patch='source.py')
    with pytest.raises(ValueError, match='explicit parent path release'):
        host.tree.assert_path(cid, rid, rid, 'source.py')
    blocked = spawn(host, cid, rid, task='Rewrite source.py again.', name='writer-2', paths=['source.py'], call='blocked')
    assert blocked['refusal']['agent_id'] == child['id'] and 'review it' in blocked['error']
    assert review(host, cid, rid, child, decision='dismiss')['review'] == 'dismissed'
    assert host.tree.assert_path(cid, rid, rid, 'source.py')['agent_id'] == rid
    assert spawn(host, cid, rid, task='Rewrite source.py again.', name='writer-2', paths=['source.py'], call='after')['status'] == 'queued'


# -- history is advice only -----------------------------------------------------------------

def test_history_never_admits_or_refuses_by_itself(host, monkeypatch):
    agents = host.app.agents
    lost = summarize('write', [('dismissed', 9, None)] * 3)
    monkeypatch.setattr(agents.history, 'advice', lambda rid, kind: lost)
    cid, rid = root(host, prior=1)
    admitted = spawn(host, cid, rid, paths=['out.py'])
    assert admitted['status'] == 'queued' and admitted['history']['recommendation'] == 'solo'
    paid = summarize('write', [('accepted', 3, 100)] * 4)
    monkeypatch.setattr(agents.history, 'advice', lambda rid, kind: paid)
    cid, rid = root(host, prior=7)
    refused = spawn(host, cid, rid, paths=['out.py'])
    assert refused['status'] == 'refused' and refused['history']['recommendation'] == 'delegation_paid'


def test_a_failing_history_store_never_changes_a_spawn_or_review(host, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('history unavailable')
    monkeypatch.setattr(host.app.agents.history, 'record', broken)
    monkeypatch.setattr(host.app.agents.history, 'advice', broken)
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid)
    assert child['status'] == 'queued' and 'history' not in child
    complete(host, cid, rid, child)
    assert review(host, cid, rid, child)['review'] == 'accepted'
    events = [e['kind'] for e in host.app.store.query('SELECT kind FROM run_events WHERE run_id=?', (rid,))]
    assert events.count('delegation_history_failed') >= 3


def test_outcomes_are_recorded_by_the_host_without_content(host):
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid, task='Read source.py for ' + MARKER, name=MARKER)
    complete(host, cid, rid, child, text='Report ' + MARKER)
    review(host, cid, rid, child)
    cid2, rid2 = root(host, prior=7)
    spawn(host, cid2, rid2, paths=['out.py'])
    rows = host.app.store.query('SELECT outcome,kind,detail,child_steps FROM delegation_history ORDER BY rowid')
    assert [(r['outcome'], r['kind'], r['detail']) for r in rows] == [
        ('admitted', 'read_only', None), ('accepted', 'read_only', 'completed'), ('refused', 'write', 'shared_steps')]
    dumped = json.dumps(host.app.store.query('SELECT * FROM delegation_history'))
    for forbidden in (MARKER, child['id'], rid, rid2, str(host.project)):
        assert forbidden not in dumped
    advice = host.app.agents.history.advice(rid, 'read_only')
    assert advice['accepted'] == 1 and advice['recommendation'] == 'delegation_paid'


# -- lower overhead: compact waits, evidence defaults, refusals naming IDs --------------------

def test_wait_for_agents_returns_a_compact_review_ready_payload(host):
    host.profile['value'] = copy.deepcopy(LARGE)  # Inline results are for windows above 4K.
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid, task='Rewrite source.py for ' + MARKER, name='writer', paths=['source.py'])
    long_text = 'Changed the value. ' * 40
    ids, patch_id = complete(host, cid, rid, child, text=long_text, patch='source.py')
    result = host.app.agents.tool(cid, rid, rid, 'WaitForAgents', {'agent_ids': json.dumps([child['id']])}, 'wait-1')
    assert result['status'] == 'ready' and result['required'] is False and result['agent_ids'] == [child['id']]
    [entry] = result['results']
    assert {k: entry[k] for k in ('agent_id', 'generation', 'status', 'review', 'evidence_ids', 'changed_paths', 'patch_ids')} == {
        'agent_id': child['id'], 'generation': 1, 'status': 'completed', 'review': 'pending', 'evidence_ids': ids,
        'changed_paths': 1, 'patch_ids': [patch_id]}
    assert entry['untrusted_summary'] == long_text[:SUMMARY_CHARS] and entry['summary_truncated'] is True
    assert not {'task', 'context', 'tools', 'limits', 'receipts'} & set(entry) and MARKER not in json.dumps(result)
    assert 'ReviewAgentResult without evidence_ids' in result['results_note']
    assert len(encoded(result)) < 1500  # one settled child, including the 200-character summary
    # The payload is enough to review and integrate without ReadAgentTree.
    accepted = review(host, cid, rid, child)
    assert accepted['review'] == 'accepted' and accepted['evidence_ids'] == ids and accepted['evidence_source'] == 'host_default'


def test_wait_payload_stays_bounded_for_eight_children_with_long_ids(host):
    host.profile['value'] = copy.deepcopy(LARGE)  # Inline results are for windows above 4K.
    cid, rid = root(host, steps=64)
    tree, settled = host.tree, []
    for index in range(8):
        node = tree.spawn(cid, rid, None, 'Read part ' + str(index), 'part-' + str(index), ['Read'],
                          {'max_steps': 4, 'max_seconds': 10})
        claim(host, cid, rid, node)
        calls = [read(host, cid, rid, node, (str(index) + str(n) + ':') + 'r' * 250) for n in range(8)]
        tree.settle(cid, rid, node['id'], 1, 'completed', {'text': 'x' * 5000}, calls, owner(node))
        settled.append(node['id'])
    result = host.app.agents.tool(cid, rid, rid, 'WaitForAgents', {'agent_ids': json.dumps(settled)}, 'wait-all')
    assert sorted(result['agent_ids']) == sorted(settled)
    assert len(encoded(result['results'])) <= RESULTS_LIMIT
    assert result['omitted_results'] == 8 - len(result['results']) > 0
    assert all(entry['agent_id'] in settled for entry in result['results'])


def test_small_route_wait_keeps_the_compact_phase14_payload(host):
    # A 4K root keeps protected inbox units within the small-window floor: no inline results;
    # the small brief's ReadAgentTree inspection and the host's evidence default still review.
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid, task='Rewrite source.py for ' + MARKER, name='writer', paths=['source.py'])
    ids, _ = complete(host, cid, rid, child, text='Changed the value. ' * 40, patch='source.py')
    result = host.app.agents.tool(cid, rid, rid, 'WaitForAgents', {'agent_ids': json.dumps([child['id']])}, 'wait-1')
    assert result['status'] == 'ready' and result['agent_ids'] == [child['id']]
    assert not {'results', 'results_note', 'omitted_results'} & set(result)
    accepted = review(host, cid, rid, child)
    assert accepted['review'] == 'accepted' and accepted['evidence_ids'] == ids


def test_empty_evidence_reviews_cite_the_childs_final_evidence(host):
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid)
    ids, _ = complete(host, cid, rid, child)
    assert review(host, cid, rid, child, evidence_ids='[]')['evidence_ids'] == ids
    # A read superseded by a later read of the same path is left out of the default.
    again = spawn(host, cid, rid, task='Read source.py twice and report.', name='rereader', call='spawn-2')
    ids, _ = complete(host, cid, rid, again, reads=('source.py', 'source.py'))
    accepted = review(host, cid, rid, again, call='review-2')
    assert accepted['evidence_ids'] == [ids[1]] and accepted['evidence_source'] == 'host_default'


def complete_claimed(host, cid, rid, node):
    ids = [read(host, cid, rid, node, '1:read')]
    host.tree.settle(cid, rid, node['id'], 1, 'completed', {'text': 'Done.'}, ids, owner(node))
    return ids, None


def test_review_refusals_name_the_evidence_they_would_accept(host):
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid)
    claim(host, cid, rid, child)
    with pytest.raises(DelegationRefused, match='this child is running') as running:
        review(host, cid, rid, child, call='r-0')
    assert running.value.refusal['accepted_evidence_ids'] == [] and 'WaitForAgents' in str(running.value)
    ids, _ = complete_claimed(host, cid, rid, child)
    with pytest.raises(DelegationRefused, match='final result evidence') as wrong:
        review(host, cid, rid, child, evidence_ids=json.dumps(['bogus']), call='r-1')
    assert wrong.value.refusal['accepted_evidence_ids'] == ids and wrong.value.refusal['rejected_evidence_ids'] == ['bogus']
    assert json.dumps(ids[0]) in str(wrong.value) and 'omit evidence_ids' in str(wrong.value)
    # A child with no final evidence: the refusal says none would be accepted.
    bare = spawn(host, cid, rid, task='Report from memory only.', name='bare', call='spawn-bare')
    complete(host, cid, rid, bare, reads=(), evidence=[])
    with pytest.raises(ValueError, match=r'Accepted evidence_ids for generation 1: \[\]'):
        review(host, cid, rid, bare, call='r-bare')
    assert review(host, cid, rid, bare, decision='dismiss', call='d-bare')['review'] == 'dismissed'


def test_stale_review_refusals_name_only_stale_ids_through_the_root_path(host):
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid)
    ids, _ = complete(host, cid, rid, child)
    (host.project / 'source.py').write_text('value = 99\n')
    with pytest.raises(PolicyError, match='stale') as stale:
        host.app.execute('ReviewAgentResult', {'agent_id': child['id'], 'generation': 1}, Workspace(host.project), cid,
                         call_id='5:review')
    assert json.dumps(ids[0]) in str(stale.value)
    receipt = next(t for t in host.app.runs.tools(rid) if t['call_id'] == '5:review')
    assert ids[0] in receipt['result']['error'] and host.tree.get(cid, rid, child['id'])['review'] == 'pending'


# -- convergence: the ping-pong limit ----------------------------------------------------------

def test_review_ping_pong_limit_demands_accept_dismiss_or_take_over(host):
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid)
    ids, _ = complete(host, cid, rid, child)
    for index in range(PING_PONG_ROUNDS):
        with pytest.raises(DelegationRefused) as plain:
            review(host, cid, rid, child, evidence_ids=json.dumps(['bogus']), call='bad-' + str(index))
        assert plain.value.refusal['kind'] == 'review'
    with pytest.raises(DelegationRefused, match='Ping-pong limit') as limit:
        review(host, cid, rid, child, evidence_ids=json.dumps(['bogus']), call='bad-3')
    refusal = limit.value.refusal
    assert (refusal['kind'], refusal['limit'], refusal['rounds'], refusal['max_rounds']) == ('ping_pong_limit', 'review_rounds', 2, 2)
    assert refusal['accepted_evidence_ids'] == ids and refusal['options'] == ['accept', 'dismiss', 'take_over']
    assert "decision='dismiss'" in str(limit.value) and 'StopAgent' in str(limit.value)
    assert host.tree.rounds(cid, rid, child['id'], 1) == 3
    # The limit never blocks the convergent choice.
    assert review(host, cid, rid, child, call='accept')['review'] == 'accepted'


def test_steer_ping_pong_limit_counts_with_review_rounds_per_generation(host):
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid)
    claim(host, cid, rid, child)
    tool = host.app.agents.tool
    for index in range(PING_PONG_ROUNDS):
        tool(cid, rid, rid, 'SendAgentMessage', {'agent_id': child['id'], 'text': 'Also check line ' + str(index)}, 'steer-' + str(index))
    with pytest.raises(DelegationRefused, match='WaitForAgents') as limit:
        tool(cid, rid, rid, 'SendAgentMessage', {'agent_id': child['id'], 'text': 'One more thing.'}, 'steer-3')
    assert (limit.value.refusal['limit'], limit.value.refusal['status']) == ('steer_rounds', 'running')
    sent = host.app.store.query('SELECT count(*) AS n FROM agent_messages WHERE run_id=? AND target=?', (rid, child['id']))
    assert sent == [{'n': PING_PONG_ROUNDS}]
    # A new generation starts with no rounds; taking the work over stays possible.
    host.tree.release(cid, rid, child['id'], 1, owner(child))
    host.tree.retry(cid, rid, rid, child['id'])
    assert host.tree.rounds(cid, rid, child['id'], 2) == 0
    host.tree.claim(cid, rid, child['id'], 2, owner(child))
    tool(cid, rid, rid, 'SendAgentMessage', {'agent_id': child['id'], 'text': 'Fresh start.'}, 'steer-g2')
    assert tool(cid, rid, rid, 'StopAgent', {'agent_id': child['id']}, 'stop')['status'] == 'stopped'


def test_one_steer_and_one_refused_review_reach_the_limit(host):
    cid, rid = root(host, steps=64)
    child = spawn(host, cid, rid)
    claim(host, cid, rid, child)
    host.app.agents.tool(cid, rid, rid, 'SendAgentMessage', {'agent_id': child['id'], 'text': 'Focus on value.'}, 'steer')
    complete_claimed(host, cid, rid, child)
    with pytest.raises(DelegationRefused):
        review(host, cid, rid, child, evidence_ids=json.dumps(['bogus']), call='bad-1')
    with pytest.raises(DelegationRefused, match='Ping-pong limit'):
        review(host, cid, rid, child, evidence_ids=json.dumps(['bogus']), call='bad-2')
    assert review(host, cid, rid, child, decision='dismiss', call='dismiss')['review'] == 'dismissed'


# -- compact tree views -----------------------------------------------------------------------

def test_large_window_small_tree_keeps_the_full_phase14_records(host):
    # Earlier graders read full node records (task, result usage) on larger windows.
    cid, rid = root(host, steps=64)
    child = host.app.agents.tool(cid, rid, rid, 'SpawnAgent', {
        'task': 'Read source.py for ' + MARKER, 'name': 'reader', 'tools': json.dumps(['Read'])}, 'spawn')
    complete(host, cid, rid, child, text='Value is 1.')
    host.profile['value'] = copy.deepcopy(LARGE)
    full = host.app.agents.tool(cid, rid, rid, 'ReadAgentTree', {}, 'tree-full')
    [entry] = full['nodes']
    assert MARKER in entry['task'] and 'result' in entry and full.get('projection_note') != COMPACT_NOTE


def test_large_window_tree_view_is_compact_and_small_window_view_is_unchanged(host, monkeypatch):
    from pebble import agent_manager
    monkeypatch.setattr(agent_manager, 'TREE_FULL_BYTES', 64)  # A tree too large for full records.
    cid, rid = root(host, steps=64)
    child = host.app.agents.tool(cid, rid, rid, 'SpawnAgent', {
        'task': 'Read source.py for ' + MARKER, 'name': 'reader', 'tools': json.dumps(['Read']),
        'context': json.dumps({'hint': MARKER})}, 'spawn')
    complete(host, cid, rid, child, text='Value is 1.')
    small = host.app.agents.tool(cid, rid, rid, 'ReadAgentTree', {}, 'tree-small')
    assert set(small['nodes'][0]) == {'id', 'name', 'parent_id', 'generation', 'status', 'review', 'evidence_ids'}
    host.profile['value'] = copy.deepcopy(LARGE)
    large = host.app.agents.tool(cid, rid, rid, 'ReadAgentTree', {}, 'tree-large')
    [entry] = large['nodes']
    assert entry['agent_id'] == child['id'] and entry['untrusted_summary'] == 'Value is 1.'
    assert not {'task', 'context', 'tools', 'limits', 'result'} & set(entry)
    assert MARKER not in json.dumps(large['nodes']) and large['projection_note'] == COMPACT_NOTE
    assert len(encoded(large['nodes'])) * 2 < len(encoded(host.tree.list(cid, rid)))  # under half the full node records
    assert set(large['budget']['usage']) == {'attempts', 'totals', 'complete', 'cost_usd'}
    detail = host.app.agents.tool(cid, rid, rid, 'ReadAgentTree', {'agent_id': child['id']}, 'tree-detail')['detail']
    assert MARKER in detail['task'] and detail['receipts']  # the full record stays one call away
    compact = host.tree.compact(cid, rid)
    assert compact['omitted_nodes'] == 0 and compact['authorization'] == 'none'


# -- epoch-6 schema proposal ------------------------------------------------------------------

def test_new_tables_match_a_fresh_app_once_added_to_the_epoch_reference(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing
    from pebble import memory_graph, migrations
    from pebble.agent_tree import ROUNDS_DDL
    from pebble.delegation_history import HISTORY_DDL
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    instance.close()
    with closing(sqlite3.connect(tmp_path / 'data' / migrations.DATABASE)) as db:
        schema = migrations._schema(db)
    # The parent's epoch-6 REFERENCE_DDL: W3's graph tables plus these, before upgrade_history.
    monkeypatch.setattr(migrations, 'REFERENCE_DDL', migrations.REFERENCE_DDL + memory_graph.GRAPH_DDL + ROUNDS_DDL + HISTORY_DDL)
    migrations._reference.cache_clear()
    try:
        reference = migrations._reference()
    finally:
        monkeypatch.undo()
        migrations._reference.cache_clear()
    owned = ('agent_rounds', 'delegation_history', 'delegation_salt')
    for key in owned:
        have, want = schema['tables'][key], reference['tables'][key]
        assert (have['columns'], have['uniques'], have['checks'], have['tail']) == (
            want['columns'], want['uniques'], want['checks'], want['tail']), key
    assert {name: index for name, index in reference['indexes'].items() if index[0] in owned} == {
        name: index for name, index in schema['indexes'].items() if index[0] in owned}
    assert not any(migrations.MARKER.search(key) for key in owned)
    compat = migrations._compatibility(schema, reference)
    assert (compat['refusals'], compat['missing_tables'], compat['unknown_tables']) == ([], [], [])
