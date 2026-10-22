"""Native process inspection: human-only, scoped, immutable acknowledgements that grant nothing.

The suites use the real ProcessJobs ledger, ProcessInspections, RunJournal and
Store, a controlled worker that never spawns anything, and the checked-in App
methods (process_list/read/stop/dismiss, perform_action, _execute and
_delete_chat) plus RunController.completion, bound to a minimal host namespace.
No model, shell, network or signal is involved; terminal and edit effects are
replaced by recording stubs.
"""

import json
import queue
import threading
import time
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import pebble.app as app_module
from pebble.agent_effects import TOOL_GROUP
from pebble.agent_tree import CHILD_TOOLS
from pebble.app import App
from pebble.controller import RunController
from pebble.permissions import DEFAULT, GROUP
from pebble.policy import PolicyError, Workspace
from pebble.process_jobs import ProcessJobs
from pebble.process_reviews import ProcessInspections
from pebble.runtime import RunJournal
from pebble.store import Store
from pebble.tool_catalogue import TOOLS


WAIT = 5.0
STARTED = {'pid': 4242, 'isolation': 'test confinement'}
PROCESS_TOOLS = {'StartCommand', 'ReadProcess', 'WaitProcess', 'StopProcess'}
UNCERTAIN = ('cancelled', 'timed_out', 'output_limited', 'unknown')
CERTAIN = ('succeeded', 'failed', 'refused', 'unstarted_cancelled')
WIDE = {'id': 'fixture', 'kind': 'openai', 'context': 32768, 'budget': 16384}
NARROW = {'id': 'fixture', 'kind': 'openai', 'context': 4096, 'budget': 4096}


def result(state='succeeded', exit_code=0, uncertain=False):
    return {'state': state, 'exit_code': exit_code, 'isolation': 'test confinement', 'outcome_uncertain': uncertain}


FINISH = {'succeeded': result('succeeded', 0), 'failed': result('failed', 1),
          'cancelled': result('cancelled', -15, True), 'timed_out': result('timed_out', -9, True),
          'output_limited': result('output_limited', None, True)}


def wait_for(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class Session:
    """One worker invocation, advanced by the test. It never spawns anything."""

    def __init__(self, payload, cancel, timeout):
        self.payload, self.cancel, self.timeout = payload, cancel, timeout
        self.commands, self.acks = queue.Queue(), queue.Queue()
        self.exited = threading.Event()

    def run(self, on_started, on_output):
        try:
            while True:
                kind, value = self.commands.get(timeout=30)
                if kind == 'result':
                    return value
                if kind == 'raise':
                    raise value
                if kind == 'until_cancel':
                    self.acks.put(kind)
                    self.cancel.wait(30)
                    return value
                (on_started if kind == 'started' else on_output)(value)
                self.acks.put(kind)
        finally:
            self.exited.set()

    def step(self, kind, value=None):
        self.commands.put((kind, value))
        assert self.acks.get(timeout=WAIT) == kind


class Controlled:
    def __init__(self):
        self.sessions = []
        self.ready = threading.Condition()

    def __call__(self, payload, cancel, on_started, on_output, *, timeout, max_output):
        session = Session(payload, cancel, timeout)
        with self.ready:
            self.sessions.append(session)
            self.ready.notify_all()
        return session.run(on_started, on_output)

    def release(self):
        for session in list(self.sessions):
            if not session.exited.is_set():
                session.commands.put(('result', result('unknown', None, True)))


class Stub:
    """A collaborator whose unspecified methods are no-ops returning None."""

    def __init__(self, **values):
        self.__dict__.update(values)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Host:
    def __init__(self, tmp_path):
        self.home, self.project = tmp_path / 'data', tmp_path / 'project'
        self.project.mkdir()
        self.store = Store(self.home)
        self.runs = RunJournal(self.store, self.home)
        self.app = SimpleNamespace(store=self.store, runs=self.runs, active=None, active_run=None, closed=False,
                                   emit=lambda kind, payload: None)
        self.worker = Controlled()
        self.jobs = ProcessJobs(self.app, worker=self.worker)
        self.inspections = ProcessInspections(self.app)
        self.ws = Workspace(self.project)
        self.count = 0
        self.guard_calls = []
        self.native = self.bind()
        self.cid = self.store.new_chat(str(self.ws.root))
        self.rid = self.run('running')

    def guard_handle(self, event, root, session, **fields):
        """AgentGuard observer stand-in: records what the host reports, decides nothing."""
        self.guard_calls.append({'event': event, 'root': root, 'session': session, **fields})
        return {'action': 'allow'}

    def bind(self):
        """The checked-in App methods over a minimal host; effects and observers are stubs."""
        host = SimpleNamespace(
            store=self.store, runs=self.runs, processes=self.jobs, process_inspections=self.inspections,
            active=None, active_run=None, closed=False, cancel=threading.Event(), sources={},
            _process_guard_observed=set(),  # App's bounded set of processes already reported to Guard
            task_policy={'mode': 'ask', 'tools': dict(DEFAULT['tools'])},
            agents=Stub(effects=Stub(), busy=lambda: False,
                        completion=lambda cid, rid: {'state': 'completed', 'issues': []}),
            hook_event=lambda *args, **kwargs: {'decision': 'allow', 'require_tests': False},
            controller=Stub(current=lambda: None), model=Stub(current=lambda: dict(WIDE)),
            guard=Stub(handle=self.guard_handle, guard=None),
            guard_session=lambda cid: cid, chat=lambda cid: {'id': cid}, emit=lambda *args: None,
            **{name: Stub() for name in ('git_api', 'jobs', 'memory', 'artifact_inputs', 'views', 'mods',
                                         'artifacts', 'images', 'files', 'plugin_media')})
        for name in ('process_list', 'process_read', 'process_stop', 'process_dismiss', 'perform_action',
                     '_await_start', 'tool_hook', '_execute', '_delete_chat'):
            setattr(host, name, types.MethodType(getattr(App, name), host))
        return host

    def config(self, max_seconds=900.0):
        return {'workspace': str(self.ws.root), 'workspace_identity': list(self.ws.identity),
                'limits': {'max_steps': 64, 'max_seconds': max_seconds}}

    def run(self, state='waiting_approval', cid=None, max_seconds=900.0, elapsed=0.0):
        rid, now = uuid.uuid4().hex, time.time()
        self.store.query('INSERT INTO runs(id,chat_id,goal,state,config,checkpoint,elapsed,created,updated) '
                         'VALUES(?,?,?,?,?,?,?,?,?)',
                         (rid, cid or self.cid, 'Check the project.', state, json.dumps(self.config(max_seconds)),
                          '{}', elapsed, now, now))
        return rid

    def payload(self, **changes):
        self.count += 1
        command = f'ls part{self.count}'
        return {'root': str(self.ws.root), 'root_identity': list(self.ws.identity), 'command': command,
                'argv': command.split(), 'network': False, 'risk': 'execution', 'requires_review': True,
                'writable': False, 'asynchronous': True, **changes}

    def action(self, rid=None, cid=None, reviewed=300, **changes):
        """A StartCommand action bound to its running receipt, as App.propose/App.approve leave it."""
        rid, cid = rid or self.rid, cid or self.cid
        payload = self.payload(**changes)
        call_id = '2:call_' + uuid.uuid4().hex[:8]
        self.runs.begin_tool(rid, call_id, 'StartCommand', {'command': payload['command']})
        aid = self.store.action(cid, 'command', {**payload, 'timeout': reviewed, 'permission_mode': 'ask'})
        self.runs.bind_action(rid, call_id, aid, 'command')
        self.store.query("UPDATE actions SET status='running' WHERE id=?", (aid,))
        action = next(a for a in self.store.actions(cid) if a['id'] == aid)
        return SimpleNamespace(action=action, payload=payload, call_id=call_id, rid=rid, cid=cid, view=None)

    def own(self, rid=None, cid=None):
        rid, cid = rid or self.rid, cid or self.cid
        self.app.active, self.app.active_run = cid, rid
        self.native.active, self.native.active_run = cid, rid

    def start(self, rid=None, cid=None, **changes):
        ctx = self.action(rid, cid, **changes)
        self.own(ctx.rid, ctx.cid)
        ctx.view = self.jobs.start(ctx.action, ctx.cid, ctx.payload)
        return ctx

    def perform(self, ctx):
        """App.perform_action for an approved StartCommand, exactly as the broker calls it."""
        self.own(ctx.rid, ctx.cid)
        ctx.view = self.native.perform_action(ctx.action, ctx.cid)
        return ctx

    def session(self, ctx):
        command = ctx.payload['command']
        with self.worker.ready:
            assert self.worker.ready.wait_for(
                lambda: any(s.payload['command'] == command for s in self.worker.sessions), WAIT)
            return next(s for s in self.worker.sessions if s.payload['command'] == command)

    def read(self, ctx, **kw):
        return self.jobs.read(ctx.cid, ctx.rid, ctx.view['process_id'], **kw)

    def settled(self, ctx):
        assert wait_for(lambda: not self.read(ctx)['live'])
        return self.read(ctx)

    def settle_as(self, state, **kw):
        ctx = self.start(**kw)
        s = self.session(ctx)
        if state == 'unstarted_cancelled':
            self.jobs.stop(ctx.cid, ctx.rid, ctx.view['process_id'])
            s.commands.put(('result', result('cancelled', None, False)))
        elif state == 'refused':
            s.commands.put(('raise', PolicyError('Command changed after review.')))
        elif state == 'unknown':
            s.step('started', STARTED)
            s.step('output', b'partial')
            s.commands.put(('raise', RuntimeError('pipe lost')))
        else:
            s.step('started', STARTED)
            s.step('output', b'partial')
            s.commands.put(('result', FINISH[state]))
        self.settled(ctx)
        return ctx

    def receipt(self, ctx):
        """Commit the broker receipt for the admitted start, as App.approve does."""
        self.runs.commit_action(ctx.action['id'], ctx.action['payload'], ctx.view, 'command',
                                {'run_id': ctx.rid, 'call_id': ctx.call_id})

    def observe(self, ctx):
        """A completed model ReadProcess receipt for this process."""
        call = 'read-' + uuid.uuid4().hex[:8]
        page = self.read(ctx)
        self.runs.begin_tool(ctx.rid, call, 'ReadProcess', {'process_id': ctx.view['process_id']})
        self.runs.end_tool(ctx.rid, call, {**page, 'receipt_id': call})
        return page

    def inspect(self, ctx, observation='effect_observed', note='Checked the build folder.'):
        return self.inspections.acknowledge(ctx.cid, ctx.rid, ctx.view['process_id'], observation, note)

    def completion(self, rid):
        controller = SimpleNamespace(app=self.native, interactions=Stub(plan=lambda rid: None))
        return RunController.completion(controller, rid, None, 'Done.')

    def row(self, ctx):
        rows = self.store.query('SELECT * FROM process_jobs WHERE id=?', (ctx.view['process_id'],))
        return rows[0] if rows else None

    def inspection_rows(self):
        return self.store.query('SELECT * FROM process_inspections ORDER BY created,process_id')

    def shutdown(self):
        self.worker.release()
        closed = self.jobs.close()
        self.runs.close()
        self.store.close()
        return closed


@pytest.fixture(autouse=True)
def thread_failures(monkeypatch):
    failures = []
    monkeypatch.setattr(threading, 'excepthook', lambda args: failures.append(args.exc_type))
    yield failures
    assert failures == []


@pytest.fixture
def h(tmp_path):
    host = Host(tmp_path)
    yield host
    host.shutdown()


@pytest.fixture
def terminal(monkeypatch):
    """Record synchronous terminal effects instead of running them."""
    calls = []

    def run_command(payload, cancel):
        calls.append(payload['command'])
        return {'exit_code': 0, 'output': '', 'command': payload['command'], 'isolation': 'stub'}

    monkeypatch.setattr(app_module, 'run_command', run_command)
    return calls


def sync_command(h, risk, command='make build'):
    payload = {'root': str(h.ws.root), 'root_identity': list(h.ws.identity), 'command': command,
               'argv': command.split(), 'network': False, 'risk': risk, 'requires_review': True,
               'writable': risk != 'read-only'}
    return {'id': uuid.uuid4().hex, 'chat_id': h.cid, 'kind': 'command', 'status': 'running', 'payload': payload}


# Which outcomes need a native inspection

@pytest.mark.parametrize('state', UNCERTAIN + CERTAIN)
def test_inspection_is_required_exactly_for_outcomes_that_may_have_had_effects(h, state):
    ctx = h.settle_as(state)
    view = h.read(ctx)
    assert view['state'] == ('cancelled' if state == 'unstarted_cancelled' else state)
    assert view['outcome_uncertain'] is (state in UNCERTAIN)
    assert h.inspections.pending(str(h.ws.root)) is (state in UNCERTAIN)
    if state in UNCERTAIN:
        receipt = h.inspect(ctx)
        assert receipt == {'process_id': ctx.view['process_id'], 'inspection_recorded': True,
                           'authorization': 'none', 'completion_verified': False}
        assert h.inspections.pending(str(h.ws.root)) is False
    else:
        with pytest.raises(PolicyError, match='uncertain'):
            h.inspect(ctx)
        assert h.inspection_rows() == []
    if state == 'unstarted_cancelled':
        assert 'Nothing ran' in view['reason'] and view['output_bytes'] == 0


def test_a_live_process_cannot_be_inspected(h):
    ctx = h.start()
    h.session(ctx).step('started', STARTED)
    with pytest.raises(PolicyError):
        h.inspect(ctx)
    assert h.inspection_rows() == [] and h.inspections.pending(str(h.ws.root)) is False


def test_inspection_is_scoped_to_its_own_conversation_and_run(h):
    ctx = h.settle_as('unknown')
    pid = ctx.view['process_id']
    other_chat = h.store.new_chat(str(h.ws.root))
    other_run = h.run('waiting_approval')
    foreign_run = h.run('waiting_approval', cid=other_chat)
    for cid, rid, process_id in ((other_chat, h.rid, pid), (h.cid, other_run, pid), (other_chat, foreign_run, pid),
                                 (h.cid, h.rid, uuid.uuid4().hex), (h.cid, h.rid, ''), (h.cid, h.rid, None),
                                 (h.cid, h.rid, 'x' * 129)):
        with pytest.raises(ValueError):
            h.inspections.acknowledge(cid, rid, process_id, 'effect_observed', 'Checked it.')
    assert h.inspection_rows() == [] and h.inspections.pending(str(h.ws.root)) is True


def test_inspection_is_idempotent_and_its_record_is_immutable(h):
    ctx = h.settle_as('cancelled')
    first = h.inspect(ctx, 'no_effect_observed', '  The output folder is unchanged.  ')
    again = h.inspect(ctx, 'no_effect_observed', 'The output folder is unchanged.')
    assert first == again
    rows = h.inspection_rows()
    assert [(r['observation'], r['note']) for r in rows] == [('no_effect_observed', 'The output folder is unchanged.')]
    for observation, note in (('effect_observed', 'The output folder is unchanged.'),
                              ('no_effect_observed', 'A different account of events.'),
                              ('still_uncertain', 'The output folder is unchanged.')):
        with pytest.raises(PolicyError, match='already recorded'):
            h.inspect(ctx, observation, note)
    assert h.inspection_rows() == rows
    events = [e for e in h.runs.events(ctx.rid, 0, 200) if e['kind'] == 'process_inspected']
    assert len(events) == 1
    assert events[0]['payload'] == {'process_id': ctx.view['process_id'], 'origin': 'human',
                                    'observation': 'no_effect_observed', 'authorization': 'none',
                                    'completion_verified': False}


@pytest.mark.parametrize('observation, note', [
    ('approved', 'Looks fine.'), ('succeeded', 'Looks fine.'), (None, 'Looks fine.'), ('effect_observed', ''),
    ('effect_observed', '   '), ('effect_observed', 'é' * 1001), ('effect_observed', None)])
def test_inspection_input_is_a_bounded_observation_never_a_verdict(h, observation, note):
    ctx = h.settle_as('timed_out')
    with pytest.raises(PolicyError):
        h.inspect(ctx, observation, note)
    assert h.inspection_rows() == []
    assert h.inspect(ctx, 'still_uncertain', 'a' * 2000)['inspection_recorded'] is True


def test_inspection_grants_no_success_approval_or_permission(h):
    ctx = h.settle_as('unknown')
    h.receipt(ctx)
    before = {'process': h.row(ctx), 'run': h.runs.get(ctx.rid)['state'],
              'action': h.store.query('SELECT status FROM actions WHERE id=?', (ctx.action['id'],))[0]['status'],
              'permissions': h.store.setting('permissions', DEFAULT)}
    h.inspect(ctx, 'effect_observed', 'The build artefacts exist.')
    after = {'process': h.row(ctx), 'run': h.runs.get(ctx.rid)['state'],
             'action': h.store.query('SELECT status FROM actions WHERE id=?', (ctx.action['id'],))[0]['status'],
             'permissions': h.store.setting('permissions', DEFAULT)}
    assert after == before
    view = h.read(ctx)
    assert view['state'] == 'unknown' and view['outcome_uncertain'] is True and view['exit_code'] is None
    listed = h.native.process_list(h.cid)
    assert listed == [{'id': ctx.view['process_id'], 'run_id': ctx.rid, 'state': 'unknown', 'outcome_uncertain': 1,
                       'inspection_recorded': 1}]
    # An unknown outcome still blocks completion after a human inspection; nothing became verified.
    h.observe(ctx)
    outcome = h.completion(ctx.rid)
    assert outcome['state'] == 'verification_required' and outcome['repairable'] is False
    assert any('unknown outcome' in issue for issue in outcome['issues'])


def test_inspection_is_not_an_observation_of_the_terminal_outcome(h):
    ctx = h.settle_as('cancelled')
    h.receipt(ctx)
    h.inspect(ctx)
    outcome = h.completion(ctx.rid)
    assert outcome['state'] == 'verification_required'
    assert outcome['issues'] == ['Read the terminal command outcome before completing this run.']
    h.observe(ctx)
    assert h.completion(ctx.rid)['state'] == 'completed'


# New effects wait for native inspection

def test_pending_inspection_follows_nested_and_renamed_project_identity(h):
    uncertain = h.settle_as('unknown')
    original = h.ws.root
    nested = original / 'nested'
    nested.mkdir()
    assert h.inspections.pending(str(nested)) is True
    moved = original.with_name(original.name + '-renamed')
    original.rename(moved)
    assert h.inspections.pending(str(moved)) is True
    h.inspect(uncertain)
    assert h.inspections.pending(str(moved)) is False


def test_uncertain_outcome_blocks_new_project_effects_until_native_inspection(h, terminal):
    uncertain = h.settle_as('output_limited')
    blocked = h.action()
    with pytest.raises(PolicyError, match='Inspect'):
        h.perform(blocked)
    with pytest.raises(PolicyError, match='Inspect'):
        h.native.perform_action(sync_command(h, 'execution'), h.cid)
    assert terminal == [] and len(h.worker.sessions) == 1
    assert h.store.query('SELECT count(*) AS n FROM process_jobs')[0]['n'] == 1
    # Read-only inspection commands stay available while the outcome is reviewed.
    assert h.native.perform_action(sync_command(h, 'read-only', 'ls build'), h.cid)['exit_code'] == 0
    assert terminal == ['ls build']
    h.inspect(uncertain, 'no_effect_observed', 'Nothing changed in build/.')
    admitted = h.perform(h.action())
    assert admitted.view['state'] == 'admitted' and len(h.worker.sessions) == 2
    assert h.native.perform_action(sync_command(h, 'execution'), h.cid)['exit_code'] == 0


@pytest.mark.parametrize('state', CERTAIN)
def test_certain_outcomes_never_block_new_effects(h, terminal, state):
    h.settle_as(state)
    assert h.native.perform_action(sync_command(h, 'execution'), h.cid)['exit_code'] == 0
    assert h.perform(h.action()).view['state'] == 'admitted'


def test_uninspected_outcome_also_blocks_project_edits(h, monkeypatch):
    applied = []
    monkeypatch.setattr(Workspace, 'apply', staticmethod(lambda payload: applied.append(payload['path'])))
    uncertain = h.settle_as('unknown')
    draft = Workspace(str(h.ws.root)).draft('notes.txt', 'new text\n')
    edit = {'id': uuid.uuid4().hex, 'chat_id': h.cid, 'kind': 'edit', 'payload': draft}
    with pytest.raises(PolicyError, match='Inspect'):
        h.native.perform_action(edit, h.cid)
    assert applied == [] and not (h.project / 'notes.txt').exists()
    h.inspect(uncertain, 'no_effect_observed', 'notes.txt was untouched.')
    h.native.perform_action(edit, h.cid)
    assert applied == ['notes.txt']


def test_uninspected_outcome_blocks_run_tests_before_the_recipe_executes(h, monkeypatch):
    executed, reported = [], []
    monkeypatch.setattr(app_module, 'run_tests', lambda payload, cancel: executed.append(payload['recipe'])
                        or {'exit_code': 0, 'output': ''})
    h.native.guard = Stub(handle=h.guard_handle, guard=None,
                          test_result=lambda root, session, recipe, result: reported.append(recipe))
    uncertain = h.settle_as('cancelled')
    tests = {'id': uuid.uuid4().hex, 'chat_id': h.cid, 'kind': 'tests',
             'payload': {'root': str(h.ws.root), 'root_identity': list(h.ws.identity), 'recipe': 'python-unittest'}}
    with pytest.raises(PolicyError, match='Inspect'):
        h.native.perform_action(tests, h.cid)
    assert executed == [] and reported == []
    h.inspect(uncertain, 'effect_observed', 'Partial build output exists.')
    assert h.native.perform_action(tests, h.cid) == {'exit_code': 0, 'output': ''}
    assert executed == ['python-unittest'] and reported == ['python-unittest']


# Models and children cannot inspect

def test_models_and_children_have_no_inspection_route(h):
    names = {tool['function']['name'] for tool in TOOLS}
    for name in names | set(GROUP):
        assert not ('inspect' in name.lower() and 'process' in name.lower())
    assert not CHILD_TOOLS & PROCESS_TOOLS
    assert set(TOOL_GROUP) == {'RunTests', 'RunCommand', 'OpenApp'}
    source = Path(app_module.__file__).read_text()
    calls = [line for line in source.splitlines() if 'process_inspections.acknowledge(' in line]
    assert len(calls) == 1 and "'process_inspect':" in calls[0]  # the native dispatch table only
    ctx = h.settle_as('cancelled')
    h.own(ctx.rid, ctx.cid)
    for name in ('InspectProcess', 'process_inspect', 'AcknowledgeProcess'):
        with pytest.raises(PolicyError):
            h.native._execute(name, {'process_id': ctx.view['process_id'], 'observation': 'no_effect_observed',
                                     'note': 'I checked.'}, h.ws, ctx.cid)
    # The owning root can read and stop its process, but that never records an inspection.
    page = h.native._execute('ReadProcess', {'process_id': ctx.view['process_id']}, h.ws, ctx.cid)
    stopped = h.native._execute('StopProcess', {'process_id': ctx.view['process_id']}, h.ws, ctx.cid)
    assert page['state'] == stopped['state'] == 'cancelled' and stopped['stop'] == 'already_settled'
    assert h.inspection_rows() == [] and h.inspections.pending(str(h.ws.root)) is True


# Dismissal and completion

def test_dismissal_requires_inspection_and_never_turns_admission_into_completion(h):
    unknown = h.settle_as('unknown', rid=h.run())
    h.receipt(unknown)
    with pytest.raises(PolicyError, match='inspection'):
        h.native.process_dismiss(unknown.cid, unknown.rid, unknown.view['process_id'])
    assert h.row(unknown) is not None
    h.inspect(unknown, 'still_uncertain', 'Could not tell what changed.')
    assert h.native.process_dismiss(unknown.cid, unknown.rid, unknown.view['process_id'])['dismissed'] is True
    assert h.row(unknown) is None and h.inspection_rows() == []
    outcome = h.completion(unknown.rid)
    assert outcome['state'] == 'verification_required' and outcome['repairable'] is False
    assert any('forgotten' in issue for issue in outcome['issues'])

    unread = h.settle_as('succeeded', rid=h.run())
    h.receipt(unread)
    h.native.process_dismiss(unread.cid, unread.rid, unread.view['process_id'])
    outcome = h.completion(unread.rid)
    assert outcome['state'] == 'verification_required' and any('forgotten' in i for i in outcome['issues'])

    verified = h.settle_as('succeeded', rid=h.run())
    h.receipt(verified)
    assert h.observe(verified)['state'] == 'succeeded'
    h.native.process_dismiss(verified.cid, verified.rid, verified.view['process_id'])
    assert h.completion(verified.rid) == {'state': 'completed', 'issues': [], 'repairable': True}


def test_dismissal_removes_only_its_own_process_and_inspection(h):
    first, second = h.settle_as('cancelled'), h.settle_as('timed_out')
    h.inspect(first)
    h.inspect(second, 'still_uncertain', 'Still checking.')
    with pytest.raises(ValueError):
        h.native.process_dismiss(h.store.new_chat(str(h.ws.root)), first.rid, first.view['process_id'])
    with pytest.raises(ValueError):
        h.native.process_dismiss(first.cid, h.run(), first.view['process_id'])
    assert len(h.inspection_rows()) == 2
    h.native.process_dismiss(first.cid, first.rid, first.view['process_id'])
    assert h.row(first) is None and h.row(second) is not None
    assert [r['process_id'] for r in h.inspection_rows()] == [second.view['process_id']]
    assert h.read(second)['untrusted_process_output'] == 'partial'


def test_deleting_a_conversation_requires_inspection_then_removes_only_its_own_records(h):
    own = h.settle_as('unknown')
    h.inspect(own)
    other_chat = h.store.new_chat(str(h.ws.root))
    other_run = h.run('waiting_approval', cid=other_chat)
    other = h.settle_as('cancelled', rid=other_run, cid=other_chat)
    h.inspect(other)
    live = h.start()
    h.session(live).step('started', STARTED)
    h.native.active = None  # no foreground task; only the background command is live
    with pytest.raises(ValueError, match='Stop owned commands'):
        h.native._delete_chat(h.cid)
    h.jobs.stop(live.cid, live.rid, live.view['process_id'])
    h.session(live).commands.put(('result', FINISH['cancelled']))
    assert h.settled(live)['outcome_uncertain'] is True
    # Every settled process was reported to Guard once; the set holds both conversations' ids.
    for ctx in (own, other, live):
        h.native.process_read(ctx.cid, ctx.rid, ctx.view['process_id'])
    ids = {ctx.view['process_id'] for ctx in (own, other, live)}
    assert h.native._process_guard_observed == ids
    before = (h.store.query('SELECT * FROM process_jobs ORDER BY id'), h.inspection_rows())
    # The stopped command may have had effects and nobody has looked yet.
    with pytest.raises(PolicyError, match='Record an inspection'):
        h.native._delete_chat(h.cid)
    assert (h.store.query('SELECT * FROM process_jobs ORDER BY id'), h.inspection_rows()) == before
    assert h.native._process_guard_observed == ids and h.store.query('SELECT 1 FROM chats WHERE id=?', (h.cid,))
    h.inspect(live, 'effect_observed', 'Half of build/ was written.')
    assert h.native._delete_chat(h.cid) is True
    assert h.store.query('SELECT chat_id FROM process_jobs') == [{'chat_id': other_chat}]
    assert h.store.query('SELECT DISTINCT process_id FROM process_output') == [{'process_id': other.view['process_id']}]
    assert [r['chat_id'] for r in h.inspection_rows()] == [other_chat]
    assert h.native._process_guard_observed == {other.view['process_id']}
    assert h.read(other)['untrusted_process_output'] == 'partial'
    assert h.store.query('SELECT 1 FROM chats WHERE id=?', (h.cid,)) == []


def test_reading_a_settled_process_reports_it_to_guard_once_and_dismissal_forgets_it(h):
    first, second = h.settle_as('failed'), h.settle_as('succeeded')
    for _ in range(3):
        h.native.process_read(first.cid, first.rid, first.view['process_id'])
    h.native.process_read(second.cid, second.rid, second.view['process_id'])
    reported = [json.loads(call['result']) for call in h.guard_calls]
    assert [(r['process_id'], r['state'], r['exit_code']) for r in reported] == [
        (first.view['process_id'], 'failed', 1), (second.view['process_id'], 'succeeded', 0)]
    assert all(call['event'] == 'post_tool_use' and call['tool'] == 'Bash' and call['root'] == str(h.ws.root)
               for call in h.guard_calls)
    h.native.process_dismiss(first.cid, first.rid, first.view['process_id'])
    assert h.native._process_guard_observed == {second.view['process_id']}


# Bounded output and root lifetime

def test_model_reads_are_bounded_to_4k_and_small_contexts_to_500_bytes(h):
    ctx = h.start()
    s = h.session(ctx)
    s.step('started', STARTED)
    for _ in range(5):
        s.step('output', b'x' * 2000)
    s.commands.put(('result', FINISH['succeeded']))
    h.settled(ctx)
    h.own(ctx.rid, ctx.cid)
    pid = ctx.view['process_id']
    page = h.native._execute('ReadProcess', {'process_id': pid, 'limit': 8000}, h.ws, ctx.cid)
    assert len(page['untrusted_process_output'].encode()) == 4000 and page['next_after'] == 4000 and page['more']
    h.native.model = Stub(current=lambda: dict(NARROW))
    page = h.native._execute('ReadProcess', {'process_id': pid, 'after': 4000, 'limit': 8000}, h.ws, ctx.cid)
    assert len(page['untrusted_process_output'].encode()) == 500 and page['next_after'] == 4500
    began = time.monotonic()
    waited = h.native._execute('WaitProcess', {'process_id': pid, 'after': 4500}, h.ws, ctx.cid)
    assert len(waited['untrusted_process_output'].encode()) == 500 and time.monotonic() - began < 1
    for limit in (0, 8001, True, '4000'):
        with pytest.raises(PolicyError):
            h.native._execute('ReadProcess', {'process_id': pid, 'limit': limit}, h.ws, ctx.cid)
    # The native view may page up to the manager bound.
    native = h.native.process_read(ctx.cid, ctx.rid, pid, 0, 8000)
    assert len(native['untrusted_process_output'].encode()) == 8000
    # Bounded pages of one complete, clean output: paging the prefix never changes completeness.
    assert native['output_complete'] is True and native['more'] is True
    assert native['coverage']['end_of_stored_output'] is False and native['coverage']['process_output_complete'] is True
    # Every read above observed the same settled process; Guard heard about it once.
    assert len(h.guard_calls) == 1 and h.native._process_guard_observed == {pid}


def test_root_lifetime_bounds_the_worker_timeout_and_its_end_requires_inspection(h):
    short = h.run('waiting_approval', elapsed=880.0)  # 20 seconds of root lifetime remain
    ctx = h.perform(h.action(rid=short))
    assert h.session(ctx).timeout == pytest.approx(20.0)
    reviewed = h.run('waiting_approval')
    ctx60 = h.perform(h.action(rid=reviewed, reviewed=60))
    assert h.session(ctx60).timeout == 60
    with pytest.raises(PolicyError, match='At most 2'):
        h.perform(h.action(rid=reviewed))
    # The root's end cancels its processes; the stopped outcome then needs a person.
    h.session(ctx).step('started', STARTED)
    h.session(ctx).step('until_cancel', FINISH['cancelled'])
    assert h.jobs.cancel_run(short) == {'run_id': short, 'cancelled': [ctx.view['process_id']]}
    final = h.settled(ctx)
    assert final['state'] == 'cancelled' and final['outcome_uncertain'] is True and final['stop_requested']
    assert h.inspections.pending(str(h.ws.root)) is True


def test_live_deadline_and_exhausted_lifetime(h):
    deadline = time.monotonic() + 12.5
    h.native.controller = Stub(current=lambda: SimpleNamespace(process_deadline=deadline))
    ctx = h.perform(h.action())
    assert 1 <= h.session(ctx).timeout <= 12.5
    h.native.controller = Stub(current=lambda: None)
    spent = h.run('waiting_approval', elapsed=899.5)
    with pytest.raises(PolicyError, match='no command execution time'):
        h.perform(h.action(rid=spent))
    assert h.store.query('SELECT count(*) AS n FROM process_jobs WHERE run_id=?', (spent,))[0]['n'] == 0
