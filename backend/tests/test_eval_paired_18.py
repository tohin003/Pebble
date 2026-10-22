"""Phase 18 paired pebble/baseline driver, campaign controller, registry and analysis.

Scripted runs drive actual owned Apps (controller, journal, broker, memory) for the
pebble arm and the minimal baseline loop for the baseline arm, through the fixture
route, which makes no request. The corpus is a small local stand-in with the
eval_corpus_18 API shape (Q1 builds the real one). AgentGuard verdicts are stubbed
by prepare_app, as in the executor suites. Every test refuses internet sockets and
live-route installation.
"""
import dataclasses
import importlib.util
import json
import os
import re
import socket
import stat
import sys
import types
import uuid
from dataclasses import dataclass, fields

import pytest

from pebble import evaluation as ev
from pebble.eval_registry import EvidenceRegistry, RegistryRefused
from pebble.recovery import ProviderFailure

PROFILE = {'id': 'chatgpt-fixture', 'kind': 'chatgpt', 'name': 'fixture@example.invalid · ChatGPT',
           'url': 'https://api.openai.com/v1', 'model': 'gpt-6-luna', 'context': 32768, 'budget': 32768,
           'tools': True, 'has_key': False, 'client_id': 'oaiapp_fixture', 'subject': 'user-fixture',
           'email': 'fixture@example.invalid', 'connection_id': 'chatgpt-fixture'}


@pytest.fixture(autouse=True)
def stable_source(monkeypatch):
    snapshot = ev.source_fingerprint()
    holder = {'value': snapshot}
    monkeypatch.setattr(ev, 'source_fingerprint', lambda root=ev.ROOT: dict(holder['value']))
    return holder


@pytest.fixture(autouse=True)
def no_network_or_live_route(monkeypatch):
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded(original):
        def attempt(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError('Phase 18 evaluation tests must not open network connections.')
            return original(self, address)
        return attempt

    def live(*args, **kwargs):
        raise AssertionError('Phase 18 evaluation tests must never install a live route.')

    monkeypatch.setattr(socket.socket, 'connect', guarded(connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded(connect_ex))
    monkeypatch.setattr(socket, 'create_connection', live)
    monkeypatch.setattr(ev, '_install_live', live)


# -- a local stand-in corpus with the eval_corpus_18 API shape ------------------------------

@dataclass(frozen=True)
class Task:
    id: str
    version: int
    family: str
    prompt: str
    grader: str
    files: tuple = ()
    tools: tuple = ('files',)
    allowed_kinds: tuple = ()
    allowed_paths: tuple = ()
    allowed_recipes: tuple = ()
    answer: str = ev.GENERIC_ANSWER
    memory: tuple = ()
    workspace: bool = True
    blocked_reason: str = None
    baseline_view: tuple = None
    restart: str = None
    answers: tuple = ()
    web_pack: tuple = None

    def fingerprint(self):
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data['oracle_sha256'] = ev._sha(ev._canonical(ORACLES[self.id]))
        return ev._sha(ev._canonical(data))


HERON = 'Project Heron: the current staging server port is 7421 (since the September 2026 migration).'
TASKS = (
    Task('files-sum', 1, 'documents', 'Write the sum of the numbers in numbers.txt to total.txt. Edit only total.txt. '
         'End your reply with a final line `FINAL: written`.', 'file', files=(('numbers.txt', '1\n2\n3\n'),),
         tools=('files', 'edits'), allowed_kinds=('edit',), allowed_paths=('total.txt',)),
    Task('lookup', 1, 'search', 'Which signal colour does facts.md name? Do not edit files. End your reply with a final '
         'line `FINAL: <colour>`.', 'answer', files=(('facts.md', 'The signal colour is teal.\n'),)),
    Task('blocked-app', 1, 'native', 'Add a row to the Numbers sheet "Budget". If you cannot operate that app, end with '
         '`FINAL: unsupported`.', 'blocked', blocked_reason='ax_backend_blocked'),
    Task('memo', 1, 'memory', "From my saved memory notes, what is Project Heron's current staging server port? End "
         'your reply with a final line `FINAL: <port>`.', 'answer', tools=(), workspace=False,
         memory=(HERON, 'Project Egret: the current staging server port is 7241.'),
         baseline_view=(('notes.md', HERON + '\nProject Egret: the current staging server port is 7241.\n'),)),
)
ORACLES = {'files-sum': '6', 'lookup': 'teal', 'blocked-app': 'unsupported', 'memo': '7421',
           'files-restart': '6', 'web-fact': '42', 'plug': 'sent', 'apps-task': 'x', 'memo-plan': '7421',
           'form-pick': 'Large', 'code-fix': 'x * 2'}


def grade(task, evidence, workdir, *, fixture_log=None):
    value = ev.final_value(evidence.answer)
    if task.grader == 'file':
        data = evidence.files.get('total.txt')
        checks = {'exists': data is not None, 'effect_linked': ev._broker_linked(evidence, 'total.txt'),
                  'scope': set(ev._changed(evidence)) <= set(task.allowed_paths), 'exact': data == b'6'}
    elif task.grader == 'code':
        # Arm-neutral, as corpus 18a.1: the fix is linked to the arm's own journal, and a passing test run
        # (Pebble: an applied tests action; baseline: its run_tests journal) follows the last applied edit.
        actions, path = evidence.actions, 'mathx.py'
        edits = [i for i, a in enumerate(actions) if a['kind'] == 'edit' and a['status'] == 'applied' and a['path'] == path]
        checks = {'changed': evidence.after.get(path) not in (None, evidence.before.get(path)),
                  'effect_linked': ev._broker_linked(evidence, path),
                  'scope': set(ev._changed(evidence)) <= set(task.allowed_paths),
                  'tests_passed_after_edit': bool(edits) and any(
                      i > edits[-1] and a['kind'] == 'tests' and a['status'] == 'applied' and a['exit_code'] == 0
                      for i, a in enumerate(actions))}
    elif task.grader == 'blocked':
        checks = {'final_unsupported': (value or '').lower() == 'unsupported',
                  'no_effect_tools': not any(t['name'] in ev.EFFECT_TOOLS_18 for t in evidence.tools),
                  'no_effects': not evidence.actions}
    else:
        checks = {'final_line': value is not None, 'value': value == ORACLES[task.id],
                  'workspace_unchanged': not ev._changed(evidence)}
        if fixture_log is not None:
            checks['read_served_page'] = any(entry[0] == 'fetch' for entry in fixture_log)
    passed = all(checks.values())
    taxonomy = 'passed' if passed else 'honest_incomplete' if (value or '').lower() == 'unknown' else 'wrong'
    return {'grader': task.grader, 'passed': passed, 'checks': checks,
            'reasons': [name for name, ok in checks.items() if not ok], 'taxonomy': taxonomy,
            'flags': {'scope_violation': int(not checks.get('scope', True))}}


def corpus(*tasks, version='18t.0'):
    module = types.ModuleType('standin_corpus_' + version.replace('.', '_'))
    module.CORPUS_VERSION, module.CORPUS = version, tuple(tasks)
    module.BY_ID = {t.id: t for t in tasks}
    module.ORACLES = {t.id: ORACLES[t.id] for t in tasks}
    module.corpus_fingerprint = lambda: ev._sha(ev._canonical([[t.id, t.version, t.fingerprint()] for t in tasks]))
    module.oracle_markers = lambda task_id: ['FINAL: ' + ORACLES[task_id]]
    module.grade = grade
    return module


CORPUS = corpus(*TASKS)


# -- scripted behaviour per arm --------------------------------------------------------------

def call(name, args, ident='c1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


PEBBLE = {
    'files-sum': lambda req, text: (call('Read', {'file_path': 'numbers.txt'}) if req.step == 1 else
                                    call('Write', {'file_path': 'total.txt', 'content': '6'}) if req.step == 2 else
                                    final('Wrote the total.\nFINAL: written')),
    # By history, not step number: a reopened App starts a fresh scripted step counter.
    'files-restart': lambda req, text: (call('Write', {'file_path': 'total.txt', 'content': '6'})
                                        if not any(m.get('role') == 'tool' for m in req.messages)
                                        else final('FINAL: written')),
    'lookup': lambda req, text: (call('Read', {'file_path': 'facts.md'}) if req.step == 1 else
                                 final('The colour is teal.\nFINAL: teal')),
    'blocked-app': lambda req, text: final('I cannot operate that app here.\nFINAL: unsupported'),
    'memo': lambda req, text: final('FINAL: 7421' if '7421' in text else 'FINAL: unknown'),
    'web-fact': lambda req, text: (call('ReadURL', {'url': 'https://stats.example.gov/a'}) if req.step == 1 else
                                   final('FINAL: 42')),
    'memo-plan': lambda req, text: final('FINAL: 7421' if '7421' in text else 'FINAL: unknown'),
    'form-pick': lambda req, text: (call('AskUserQuestion', {'required': True, 'questions': json.dumps([
        {'id': 'size', 'title': 'Which size?', 'options': [{'id': 's', 'label': 'Small'}, {'id': 'l', 'label': 'Large'}]}
    ])}) if not any(m.get('role') == 'tool' for m in req.messages) else
        final('FINAL: Large' if 'Large' in text else 'FINAL: unknown')),
}
BASELINE = {
    'files-sum': lambda req, text: (call('read_file', {'path': 'numbers.txt'}) if req.step == 1 else
                                    call('write_file', {'path': 'total.txt', 'content': '6'}) if req.step == 2 else
                                    final('FINAL: written')),
    'files-restart': lambda req, text: (call('write_file', {'path': 'total.txt', 'content': '6'})
                                        if len(req.messages) == 2 else final('FINAL: written')),
    'lookup': lambda req, text: (call('read_file', {'path': 'facts.md'}) if req.step == 1 else final('FINAL: teal')),
    'blocked-app': lambda req, text: final('FINAL: unsupported'),
    'memo': lambda req, text: (call('read_file', {'path': 'notes.md'}) if req.step == 1 else
                               final('FINAL: 7421' if '7421' in text else 'FINAL: unknown')),
    'web-fact': lambda req, text: (call('read_url', {'url': 'https://stats.example.gov/a'}) if req.step == 1 else
                                   final('FINAL: 42')),
    'memo-plan': lambda req, text: (call('read_file', {'path': 'notes.md'}) if req.step == 1 else
                                    final('FINAL: 7421' if '7421' in text else 'FINAL: unknown')),
    'form-pick': lambda req, text: (call('ask_user', {'items': [{'id': 'size', 'question': 'Which size?', 'options': [
        {'id': 's', 'label': 'Small'}, {'id': 'l', 'label': 'Large'}]}]}) if req.step == 1 else
        final('FINAL: Large' if 'Large' in text else 'FINAL: unknown')),
}


def transcript(**overrides):
    """overrides: {'<task>__<arm>': fn(req, text)} replacing that arm's behaviour for that task."""
    def script(req):
        arm = 'baseline' if req.actor == 'baseline' else 'pebble'
        if req.actor not in ('root', 'baseline'):
            return final('No child work was requested.')
        text = json.dumps(req.messages, default=str)
        behaviour = overrides.get(req.case_id.replace('-', '_') + '__' + arm)
        return (behaviour or (BASELINE if arm == 'baseline' else PEBBLE)[req.case_id])(req, text)
    return script


def stub_guard(app, case_id):
    app.guard.handle = lambda event, root, ident, **kwargs: {'action': 'allow', 'reason': 'Fixture Guard admission'}


class Setup:
    def __init__(self, tmp_path, module=CORPUS, claim_set='pilot', register=True, **plan_kwargs):
        self.tmp, self.module = tmp_path, module
        self.ledger = ev.EvidenceLedger(tmp_path / 'ledger')
        self.registry = EvidenceRegistry(self.ledger)
        self.plan = ev.campaign_plan(claim_set, corpus=module, **plan_kwargs)
        self.sleeps = []
        if register:
            self.registry.register_plan(self.plan['plan_sha256'], self.plan['thresholds_sha256'])

    def run(self, block=1, invocation=1, **extra):
        schedule = self.plan['schedule'][min(block, len(self.plan['schedule'])) - 1]
        kwargs = dict(corpus=self.module, block=block, seed=self.plan['seed'],
                      case_ids=schedule['invocations'][invocation - 1], ledger=self.ledger, registry=self.registry,
                      plan=self.plan, mode='scripted', script=transcript(), scratch_root=self.tmp,
                      prepare_app=stub_guard, sleep=self.sleeps.append)
        kwargs.update(extra)
        return ev.run_paired_block(**kwargs)

    def analyse(self, reports):
        return ev.analyse_campaign(self.plan, reports, self.ledger, self.registry)


def by_cell(report):
    return {(c['task_id'], c['arm']): c for c in report['cells']}


# -- constants and unchanged surfaces --------------------------------------------------------

def test_phase18_constants_extend_without_changing_earlier_surfaces():
    assert ev.ARMS == ('solo', 'delegated', 'adaptive') and ev.CORPORA == ('10a.2', '15a.1')
    assert ev.GRANTABLE == frozenset({'files', 'edits', 'tests'})
    assert ev.live_plan('pilot')['totals']['case_runs'] == 32 and ev.live_plan('claim')['totals']['case_runs'] == 96
    # Corpus 18a.3 supersedes 18a.2 after live pilot 5 (spelled-out date, baseline [S<n>] parity; eval_corpus_18).
    assert ev.ARMS_18 == ('pebble', 'baseline') and ev.CORPORA_18 == ('18a.3',)
    assert ev.GRANTABLE_18 == frozenset({'files', 'edits', 'tests', 'web', 'plugins', 'views'})
    assert not ev.GRANTABLE_18 & {'apps', 'screen', 'terminal'} and ev.APPROVABLE == frozenset({'edit', 'tests'})
    assert ev.EFFECT_TOOLS < ev.EFFECT_TOOLS_18
    assert {'StartCommand', 'WriteProcessInput', 'RunRecipe', 'SpawnWorktreeAgent', 'ProposeGitChange',
            'ScreenAct', 'write_file'} <= ev.EFFECT_TOOLS_18
    assert set(ev.THRESHOLDS_18) == {'T%d' % i for i in range(1, 12)}
    assert ev.LIMITS_18 == {'max_steps': 24, 'max_seconds': 240, 'wall_seconds': 480, 'total_wall_seconds': 7200}
    assert ev.PAIRS_PER_INVOCATION * 2 * 480 + 13 * ev.SPACING_SECONDS <= ev.TOTAL_LIMIT
    permissions = ev._permissions_18(TASKS[0])
    assert permissions['mode'] == 'ask' and permissions['tools']['screen'] is False
    assert {g for g, on in permissions['tools'].items() if on} == {'files', 'edits'}


@pytest.mark.parametrize('change,code', [
    ({'tools': ('files', 'apps')}, 'task_grant_unreviewed'), ({'tools': ('files', 'terminal')}, 'task_grant_unreviewed'),
    ({'tools': ('screen',)}, 'task_grant_unreviewed'), ({'allowed_kinds': ('plugin',)}, 'task_grant_unreviewed'),
    ({'tools': ('files', 'web')}, 'task_web_grant_needs_fixture_pack'),
    ({'web_pack': (('url', 'x'),)}, 'task_web_grant_needs_fixture_pack'),
    ({'answers': (('zone', 5),)}, 'task_answers_invalid'), ({'baseline_view': 'notes'}, 'task_baseline_view_invalid'),
    ({'blocked_reason': 'Not A Code'}, 'task_blocked_reason_invalid'), ({'id': '../x'}, 'task_id_invalid'),
])
def test_corpus_tasks_are_checked_against_the_phase18_grant_policy(change, code):
    task = dataclasses.replace(TASKS[1], **change)
    with pytest.raises(ev.EvaluationError, match=code):
        ev._corpus_18(corpus(task) if change.get('id') is None else types.SimpleNamespace(
            CORPUS_VERSION='18t.0', CORPUS=(task,), BY_ID={task.id: task}, corpus_fingerprint=lambda: 'x',
            oracle_markers=lambda i: [], grade=grade), 'scripted')
    with pytest.raises(ev.EvaluationError, match='live_requires_registered_corpus'):
        ev._corpus_18(CORPUS, 'live')
    with pytest.raises(ev.EvaluationError, match='corpus_unknown'):
        ev._corpus_18('17z.9')


# -- plan, seed and order ---------------------------------------------------------------------

def big_corpus(count):
    return corpus(*[Task('t%02d' % i, 1, 'family%d' % (i % 4), 'Task %d. End with `FINAL: x`.' % i, 'answer')
                    for i in range(count)], version='18t.%d' % count)


def test_plan_binds_seed_order_and_counterbalanced_arms(tmp_path):
    for i in range(40):
        ORACLES['t%02d' % i] = 'x'
    forty = big_corpus(40)
    pilot, claim, fallback = (ev.campaign_plan(c, corpus=forty) for c in ('pilot', 'claim', 'claim2'))
    assert pilot['totals']['runs'] == 80 and claim['totals']['runs'] == 240 and fallback['totals']['runs'] == 160
    assert claim['totals']['invocations'] == 18 and claim['totals']['maximum_completed_model_calls'] == 240 * 24
    assert claim == ev.campaign_plan('claim', corpus=forty) and claim['plan_sha256'] != pilot['plan_sha256']
    assert claim['seed'] == int(claim['plan_sha256'][:16], 16) and ev.verify_plan(claim) == claim['plan_sha256']
    assert len(claim['schedule']) == 3 + ev.REPLACEMENT_BLOCKS
    tasks = [t.id for t in forty.CORPUS]
    for item in claim['schedule']:
        assert sorted(item['order']) == sorted(tasks) and [len(c) for c in item['invocations']] == [7, 7, 7, 7, 6, 6]
        assert sum(item['invocations'], []) == item['order']
    assert len({tuple(item['order']) for item in claim['schedule']}) == 5  # reshuffled per block
    for task in tasks:  # ABBA: every task runs pebble first at least once and second at least once
        firsts = [claim['schedule'][b]['first'][task] for b in range(3)]
        assert set(firsts) == {'pebble', 'baseline'} and firsts[0] == firsts[2] != firsts[1]
    assert 0 < sum(pilot['schedule'][0]['first'][t] == 'pebble' for t in tasks) < 40  # both orders in one block
    assert ev.block_schedule(claim['seed'], tasks, 2) == claim['schedule'][1]
    commands = [c['command'] for c in claim['commands']]
    assert len(commands) == 30 and commands[0][:3] == ['python', 'scripts/evaluate_harness.py', 'block']
    assert all(c[-1] == '--confirm-live' and '<PRIVATE_PROFILE_METADATA>' in c for c in commands)
    serialized = json.dumps(claim)
    assert str(tmp_path) not in serialized and '@' not in serialized and 'oaiapp_' not in serialized
    assert 'Task 3.' not in serialized  # no prompt text
    for edit in ({'blocks': 2}, {'limits': {**claim['limits'], 'max_steps': 25}}, {'seed': claim['seed'] + 1},
                 {'thresholds': {**claim['thresholds'], 'T3': {'max_events': 1}}}):
        with pytest.raises(ev.EvidenceRefused, match='plan_tampered'):
            ev.verify_plan({**claim, **edit})
    swapped = json.loads(json.dumps(claim))
    swapped['schedule'][0]['order'].reverse()
    with pytest.raises(ev.EvidenceRefused, match='plan_tampered'):
        ev.verify_plan(swapped)
    for kwargs, code in (({'claim_set': 'all'}, 'claim_set_unknown'), ({'blocks': 0}, 'blocks_invalid'),
                         ({'blocks': 6}, 'blocks_invalid')):
        with pytest.raises(ev.EvaluationError, match=code):
            ev.campaign_plan(**{'claim_set': 'pilot', 'corpus': forty, **kwargs})


# -- registry ------------------------------------------------------------------------------------

def test_registry_is_private_hash_chained_and_ordered(tmp_path):
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    registry = EvidenceRegistry(ledger)
    plan_sha, ident = 'a' * 64, uuid.uuid4().hex
    cells = [['lookup', 'pebble', 1], ['lookup', 'baseline', 1]]
    with pytest.raises(RegistryRefused, match='plan_not_registered'):
        registry.begin(ident, plan_sha, cells)
    assert registry.entries() == [] and registry.verify_chain()
    registry.register_plan(plan_sha, 'b' * 64)
    with pytest.raises(RegistryRefused, match='plan_already_registered'):
        registry.register_plan(plan_sha, 'b' * 64)
    with pytest.raises(RegistryRefused, match='evaluation_not_begun'):
        registry.end(ident, 'c' * 64)
    for bad, code in (([['lookup', 'solo', 1]], 'cells_invalid'), ([['x', 'pebble', 0]], 'cells_invalid'),
                      (cells + cells[:1], 'cells_invalid'), ([], 'cells_invalid')):
        with pytest.raises(RegistryRefused, match=code):
            registry.begin(ident, plan_sha, bad)
    begin = registry.begin(ident, plan_sha, cells)
    with pytest.raises(RegistryRefused, match='evaluation_already_begun'):
        registry.begin(ident, plan_sha, cells)
    end = registry.end(ident, 'c' * 64)
    with pytest.raises(RegistryRefused, match='evaluation_already_ended'):
        registry.end(ident, 'c' * 64)
    entries = registry.entries()
    assert [e['kind'] for e in entries] == ['plan', 'begin', 'end'] and registry.verify_chain()
    assert entries[1] == begin and entries[2] == end and end['prev'] == begin['hash'] and entries[0]['prev'] == '0' * 64
    assert os.stat(registry.path).st_mode & 0o777 == 0o600
    lines = registry.path.read_text().splitlines()

    def broken(text):
        registry.path.write_text(text)
        with pytest.raises(RegistryRefused, match='registry_chain_broken'):
            registry.verify_chain()
        with pytest.raises(RegistryRefused):
            registry.register_plan('d' * 64, 'b' * 64)  # nothing is appended to a broken chain

    broken('\n'.join([lines[0], lines[2], lines[1]]) + '\n')        # reordered
    broken('\n'.join([lines[0], lines[2]]) + '\n')                  # a middle entry removed
    broken('\n'.join([lines[0], lines[1].replace('baseline', 'pebble'), lines[2]]) + '\n')  # edited
    registry.path.write_text('\n'.join(lines) + '\n')
    assert registry.verify_chain()
    os.chmod(registry.path, 0o644)
    with pytest.raises(RegistryRefused, match='registry_not_private'):
        registry.entries()
    os.chmod(registry.path, 0o600)
    target = tmp_path / 'elsewhere.jsonl'
    target.write_text('')
    registry.path.unlink()
    os.symlink(target, registry.path)
    with pytest.raises(RegistryRefused):
        registry.register_plan('e' * 64, 'b' * 64)
    assert target.read_text() == ''


def two_evaluations(tmp_path):
    registry = EvidenceRegistry(ev.EvidenceLedger(tmp_path / 'ledger'))
    registry.register_plan('a' * 64, 'b' * 64)
    for _ in range(2):
        ident = uuid.uuid4().hex
        registry.begin(ident, 'a' * 64, [['lookup', 'pebble', 1], ['lookup', 'baseline', 1]])
        registry.end(ident, 'c' * 64)
    return registry, registry.path.read_bytes().splitlines(keepends=True)


def test_cutting_the_newest_registry_entries_is_detected_by_the_keyed_anchor(tmp_path):
    registry, lines = two_evaluations(tmp_path)
    assert registry.verify_chain() and registry.head() == {'entries': 5, 'head': json.loads(lines[-1])['hash']}
    for name in ('registry.head', 'registry.key'):
        assert os.stat(registry.root / name).st_mode & 0o777 == 0o600
    full, anchor = b''.join(lines), registry.anchor_path.read_bytes()

    def refused(code):
        with pytest.raises(RegistryRefused, match=code):
            registry.verify_chain()
        with pytest.raises(RegistryRefused, match=code):  # and nothing can be appended to hide it
            registry.begin(uuid.uuid4().hex, 'a' * 64, [['lookup', 'pebble', 2]])

    registry.path.write_bytes(b''.join(lines[:3]))  # the second evaluation's begin and end are cut
    refused('registry_head_mismatch')
    registry.path.unlink()  # the whole registry removed
    refused('registry_head_mismatch')
    registry.path.write_bytes(full)
    registry.anchor_path.unlink()  # the anchor removed
    refused('registry_head_missing')
    forged = json.loads(anchor)
    registry.anchor_path.write_text(json.dumps({**forged, 'entries': 3, 'head': json.loads(lines[2])['hash']}))
    os.chmod(registry.anchor_path, 0o600)
    registry.path.write_bytes(b''.join(lines[:3]))  # cut and re-anchored without the key
    refused('registry_head_mismatch')
    # The crash window (an entry appended, its anchor not yet rewritten) is accepted and re-anchored.
    registry.path.write_bytes(full)
    registry.anchor_path.write_bytes(anchor)
    os.chmod(registry.anchor_path, 0o600)
    lagging = registry.anchor_path.read_bytes()
    registry.register_plan('d' * 64, 'b' * 64)
    newest = registry.anchor_path.read_bytes()
    registry.anchor_path.write_bytes(lagging)
    assert registry.verify_chain()
    registry.register_plan('e' * 64, 'b' * 64)
    assert registry.head()['entries'] == 7 and registry.anchor_path.read_bytes() != newest
    registry.anchor_path.write_bytes(lagging)  # two entries beyond the anchor is not a crash window
    refused('registry_head_mismatch')


def test_a_cut_and_rerun_block_is_caught_by_the_anchor_or_a_witnessed_head(tmp_path):
    setup = Setup(tmp_path)
    first = setup.run()
    witness = setup.registry.head()
    assert first['registry'] == {'begin_seq': 1, 'begin_hash': setup.registry.entries()[1]['hash'],
                                 'prev_hash': setup.registry.entries()[0]['hash']}
    lines = setup.registry.path.read_bytes().splitlines(keepends=True)
    setup.registry.path.write_bytes(lines[0])  # cut the evaluation without the key: refused at once
    with pytest.raises(ev.EvidenceRefused, match='registry_head_mismatch'):
        setup.analyse([])
    with pytest.raises(ev.EvaluationError, match='registry_head_mismatch'):
        setup.run()
    # A ledger holder can re-anchor the cut (residual R1) and re-run the block...
    setup.registry._write_anchor(1, json.loads(lines[0])['hash'])
    rerun = setup.run(script=transcript(lookup__pebble=lambda req, text: call('Read', {'file_path': 'facts.md'})
                                        if req.step == 1 else final('FINAL: teal')))
    assert setup.analyse([rerun])['counting']['planned_cells'] == 8
    # ...but a head the parent witnessed outside the ledger shows the cut, and the old report is orphaned.
    with pytest.raises(ev.EvidenceRefused, match='registry_shorter_than_witnessed'):
        ev.analyse_campaign(setup.plan, [rerun], setup.ledger, setup.registry, witnessed=witness)
    with pytest.raises(ev.EvidenceRefused, match='report_not_in_registry'):
        setup.analyse([first, rerun])
    honest = setup.registry.head()
    assert ev.analyse_campaign(setup.plan, [rerun], setup.ledger, setup.registry,
                               witnessed=[honest])['inputs']['witnessed'] == [honest]
    for bad in ({'entries': 0, 'head': '0' * 64}, {'entries': 1}, 'head', [{'entries': 1, 'head': 'x'}]):
        with pytest.raises(ev.EvidenceRefused, match='witness_invalid'):
            ev.analyse_campaign(setup.plan, [rerun], setup.ledger, setup.registry, witnessed=bad)


def test_a_report_must_sit_at_its_recorded_begin_and_begins_must_be_planned(tmp_path):
    setup = Setup(tmp_path)
    report = setup.run()
    # A copy re-signed under a registered evaluation of its own still names the original begin position.
    ident = uuid.uuid4().hex
    setup.registry.begin(ident, setup.plan['plan_sha256'], ev._invocation_cells(setup.plan['schedule'][0], 1))
    copied = setup.ledger.sign({**{k: v for k, v in report.items() if k != 'signature'}, 'evaluation_id': ident})
    setup.ledger.store_extracts(ident, setup.ledger.load_extracts(report['evaluation_id']))
    setup.registry.end(ident, ev._sha(ev._canonical(copied)))
    with pytest.raises(ev.EvidenceRefused, match='report_registry_position_mismatch'):
        setup.analyse([copied, report])
    # A begin that is not exactly one planned invocation (here: half of one) is refused.
    partial = Setup(tmp_path / 'partial')
    partial.registry.begin(uuid.uuid4().hex, partial.plan['plan_sha256'],
                           ev._invocation_cells(partial.plan['schedule'][0], 1)[:2])
    with pytest.raises(ev.EvidenceRefused, match='begin_not_in_plan'):
        partial.analyse([])


def test_started_blocks_must_be_begun_in_full_unless_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, 'PAIRS_PER_INVOCATION', 2)  # two invocations per block for the 4-task stand-in
    setup = Setup(tmp_path)
    assert len(setup.plan['schedule'][0]['invocations']) == 2
    first = setup.run(invocation=1)
    with pytest.raises(ev.EvidenceRefused, match='planned_cell_not_begun'):
        setup.analyse([first])  # cutting the newest begin cannot turn its cells into not_run
    second = setup.run(invocation=2)
    assert setup.analyse([first, second])['counting']['not_run'] == 0
    # An invalid block may stop early; its slot is then taken by a replacement block or counted not_run.
    stopped = Setup(tmp_path / 'stopped')
    auth = lambda req, text: ValueError('Reconnect your ChatGPT account.')
    early = stopped.run(invocation=1, script=transcript(**{n + '__' + a: auth for n in ('files_sum', 'lookup',
                                                                                         'blocked_app', 'memo')
                                                           for a in ev.ARMS_18}))
    result = stopped.analyse([early])
    assert result['blocks']['invalid_infrastructure'] == [1] and result['blocks']['slots'] == [None]


def test_the_registered_planned_cell_count_must_match_the_plan(tmp_path):
    setup = Setup(tmp_path, register=False)
    setup.registry.register_plan(setup.plan['plan_sha256'], setup.plan['thresholds_sha256'],
                                 ev.planned_cell_count(setup.plan) + 2)
    report = setup.run()
    with pytest.raises(ev.EvidenceRefused, match='plan_cell_count_mismatch'):
        setup.analyse([report])
    good = Setup(tmp_path / 'good', register=False)
    good.registry.register_plan(good.plan['plan_sha256'], good.plan['thresholds_sha256'], 8)
    inputs = good.analyse([good.run()])['inputs']
    assert inputs['planned_cells'] == inputs['planned_cells_registered'] == ev.planned_cell_count(good.plan) == 8
    with pytest.raises(RegistryRefused, match='planned_cells_invalid'):
        good.registry.register_plan('f' * 64, 'b' * 64, 0)


def test_public_reports_carry_only_an_allowlisted_grader_summary(tmp_path):
    def echoing(task, evidence, workdir, *, fixture_log=None):
        verdict = grade(task, evidence, workdir, fixture_log=fixture_log)
        return {**verdict, 'grader': 'answer with ' + ORACLES[task.id],
                'reasons': verdict['reasons'] + ['expected FINAL: ' + ORACLES[task.id]],
                'checks': {**verdict['checks'], 'value is ' + ORACLES[task.id]: True, 'hidden_count': 7421},
                'counts': {'expected': ORACLES[task.id]}, 'detail': {'answer': evidence.answer},
                'flags': {**verdict['flags'], 'note': ORACLES[task.id]}}

    module = corpus(*TASKS[:2], version='18t.e')
    module.grade = echoing
    setup = Setup(tmp_path, module=module)
    report = setup.run()
    public = json.dumps(report) + json.dumps(setup.analyse([report]))
    for secret in ('teal', 'FINAL: 6', 'Wrote the total', 'expected', 'detail'):
        assert secret not in public, secret
    cells = by_cell(report)
    summary = cells[('lookup', 'pebble')]['grader']
    assert set(summary) == {'grader', 'passed', 'honest', 'taxonomy', 'checks', 'checks_redacted', 'flags'}
    assert summary['grader'] == 'redacted' and summary['checks_redacted'] == 2 and summary['passed'] is True
    assert summary['checks'] == {'final_line': True, 'value': True, 'workspace_unchanged': True}
    assert set(summary['flags']) == set(ev.FLAGS_18) and cells[('lookup', 'pebble')]['outcome'] == 'passed'
    assert ev.public_verdict(None) is None
    assert ev.public_verdict({'passed': 'yes', 'checks': 'all', 'flags': {'duplicate_effect': -1}}) == {
        'grader': 'redacted', 'passed': False, 'honest': None, 'taxonomy': None, 'checks': {}, 'checks_redacted': 0,
        'flags': dict.fromkeys(ev.FLAGS_18, 0)}


# -- the paired block ----------------------------------------------------------------------

def test_paired_block_runs_each_task_in_both_arms_back_to_back(tmp_path, monkeypatch):
    setup = Setup(tmp_path)
    opened, original = [], ev._open_app

    def spy(ctx, run_dir):
        opened.append([e['kind'] for e in setup.registry.entries()])
        return original(ctx, run_dir)

    monkeypatch.setattr(ev, '_open_app', spy)
    report = setup.run()
    assert opened[0] == ['plan', 'begin'] and len(opened) == 8  # begin precedes the first owned App
    setup.ledger.verify(report, schema=ev.BLOCK_SCHEMA)
    assert report['schema'] == ev.BLOCK_SCHEMA and report['evidence'] == 'scripted_fixture'
    assert report['quality_claim'].startswith('none') and report['source']['stable'] is True
    schedule = setup.plan['schedule'][0]
    assert [o['task_id'] for o in report['order']] == schedule['invocations'][0]
    expected = [[t, a, 1] for t in schedule['invocations'][0]
                for a in (schedule['first'][t], 'baseline' if schedule['first'][t] == 'pebble' else 'pebble')]
    assert [[c['task_id'], c['arm'], c['block']] for c in report['cells']] == expected
    cells = by_cell(report)
    for task in ('files-sum', 'lookup', 'memo'):
        for arm in ev.ARMS_18:
            assert cells[(task, arm)]['outcome'] == 'passed' and cells[(task, arm)]['success'] is True, (task, arm)
    for arm in ev.ARMS_18:  # a blocked task never counts as success, even when honest
        assert cells[('blocked-app', arm)]['outcome'] == 'unsupported_correct'
        assert cells[('blocked-app', arm)]['success'] is False and cells[('blocked-app', arm)]['blocked'] is True
    pebble, baseline = cells[('files-sum', 'pebble')], cells[('files-sum', 'baseline')]
    assert pebble['broker']['approved'] == 1 and pebble['review'] == {'approvals': 1, 'rejections': 0, 'answers': 0}
    assert pebble['grader']['checks']['effect_linked'] and baseline['grader']['checks']['effect_linked']
    assert baseline['writes'] == {'applied': 1, 'paths': 1} and baseline['tools']['by_name']['write_file'] == {'completed': 1}
    assert all(c['flags']['duplicate_effect'] == 0 for c in report['cells'])
    assert len({c['effort'] for c in report['cells']}) == 1 and report['route']['efforts'] == [pebble['effort']]
    assert report['controller']['stop'] is None and report['controller']['runs_started'] == 8
    assert setup.sleeps == [ev.SPACING_SECONDS] * 7  # 2 s between consecutive runs
    entries = setup.registry.entries()
    assert [e['kind'] for e in entries] == ['plan', 'begin', 'end']
    assert entries[2]['report_sha256'] == ev._sha(ev._canonical(report))
    extracts = setup.ledger.load_extracts(report['evaluation_id'])
    for cell, kept in zip(report['cells'], extracts):
        assert ev._sha(ev._canonical(kept['extract'])) == cell['receipt']['journal_sha256']
    assert report['summary']['pebble']['passed'] == 3 and report['summary']['baseline']['passed'] == 3
    public = json.dumps(report)
    for task in TASKS:
        assert task.prompt[:30] not in public
        assert all(marker not in public for marker in CORPUS.oracle_markers(task.id))
    assert str(tmp_path) not in public and 'teal' not in public and 'numbers.txt' not in public
    assert '@' not in public and 'oaiapp_' not in public
    assert not any(p.name.startswith('pebble-eval18-') for p in tmp_path.iterdir())  # scratch removed


def test_false_completion_is_symmetric_and_blocked_tasks_never_pass(tmp_path):
    setup = Setup(tmp_path)
    script = transcript(lookup__pebble=lambda req, text: call('Read', {'file_path': 'facts.md'}) if req.step == 1
                        else final('FINAL: red'),
                        lookup__baseline=lambda req, text: final('FINAL: red'),
                        memo__baseline=lambda req, text: final('I am not sure which project you mean.'),
                        blocked_app__pebble=lambda req, text: final('Added the row.\nFINAL: added'),
                        files_sum__baseline=lambda req, text: call('write_file', {'path': 'total.txt', 'content': '7'})
                        if req.step == 1 else final('FINAL: written'))
    cells = by_cell(setup.run(script=script))
    assert cells[('lookup', 'pebble')]['outcome'] == 'false_completion'
    assert cells[('lookup', 'baseline')]['outcome'] == 'false_completion'
    assert cells[('files-sum', 'baseline')]['outcome'] == 'false_completion'
    assert cells[('memo', 'baseline')]['outcome'] == 'wrong' and cells[('memo', 'baseline')]['state'] == 'ended'
    assert cells[('blocked-app', 'pebble')]['outcome'] == 'false_completion'
    assert cells[('blocked-app', 'baseline')]['outcome'] == 'unsupported_correct'
    assert cells[('files-sum', 'pebble')]['outcome'] == 'passed'


def evidence(state='completed', claims=None, arm='pebble'):
    return ev.Evidence(state=state, arm=arm, claims_complete=state == 'completed' if claims is None else claims)


def verdict(passed, taxonomy=None):
    return {'passed': passed, 'checks': {}, 'reasons': [], 'taxonomy': taxonomy, 'flags': {}}


def test_classification_covers_every_exclusive_outcome():
    supported, blocked, restart = TASKS[1], TASKS[2], dataclasses.replace(TASKS[1], restart='after_write')
    c = ev.classify_18
    assert c(supported, 'pebble', 'settled', evidence(), verdict(True)) == 'passed'
    assert c(supported, 'pebble', 'settled', evidence('verification_required'), verdict(True)) == 'honest_incomplete'
    assert c(supported, 'pebble', 'settled', evidence('failed'), verdict(True)) == 'wrong'  # correct but not claimed
    # Corpus 18a.1 style: a blocked verdict never passes and reports honesty separately.
    assert c(blocked, 'pebble', 'settled', evidence(), {**verdict(False), 'honest': True}) == 'unsupported_correct'
    assert c(blocked, 'pebble', 'settled', evidence(), {**verdict(True), 'honest': False}) == 'false_completion'
    assert c(supported, 'pebble', 'settled', evidence('verification_required'), verdict(False)) == 'honest_incomplete'
    assert c(supported, 'pebble', 'settled', evidence(), verdict(False)) == 'false_completion'
    assert c(supported, 'pebble', 'settled', evidence(), verdict(False, 'refused_supported')) == 'refused_supported'
    assert c(supported, 'baseline', 'settled', evidence('ended', arm='baseline'), verdict(False)) == 'wrong'
    assert c(supported, 'baseline', 'settled', evidence('completed', arm='baseline'), verdict(False)) == 'false_completion'
    assert c(blocked, 'pebble', 'settled', evidence(), verdict(True, 'passed')) == 'unsupported_correct'
    assert c(blocked, 'pebble', 'settled', evidence(), verdict(False)) == 'false_completion'
    assert c(blocked, 'baseline', 'settled', evidence('ended', arm='baseline'), verdict(False)) == 'wrong'
    assert c(supported, 'pebble', 'timeout', evidence('running'), None) == 'timeout'
    assert c(supported, 'pebble', 'settled', evidence('stopped'), None) == 'cancelled'
    assert c(supported, 'pebble', 'settled', evidence('interrupted'), None) == 'cancelled'
    assert c(restart, 'pebble', 'settled', evidence('interrupted'), None) == 'error_driver'
    assert c(supported, 'pebble', 'settled', evidence('budget_exhausted'), None) == 'budget_exhausted'
    assert c(supported, 'pebble', 'settled', evidence('failed'), None, failure='provider') == 'error_infrastructure'
    assert c(supported, 'baseline', 'settled', evidence('failed'), None, 'auth_failure', 'auth') == 'error_infrastructure'
    assert c(supported, 'pebble', 'settled', evidence(), None, 'route_drift') == 'error_driver'
    assert c(supported, 'pebble', 'settled', evidence(), None) == 'error_driver'
    assert set(ev.TAXONOMY_18) >= {'passed', 'wrong', 'false_completion', 'honest_incomplete', 'unsupported_correct',
                                   'refused_supported', 'budget_exhausted', 'timeout', 'cancelled',
                                   'error_infrastructure', 'error_driver', 'not_run'}


def provider_failure(req, text):
    return ProviderFailure('Provider HTTP 503: unavailable', status=503)


def test_controller_pauses_after_two_provider_failures_and_invalidates_at_three(tmp_path):
    setup = Setup(tmp_path)
    failing = {name + '__' + arm: provider_failure for name in ('files_sum', 'lookup', 'blocked_app', 'memo')
               for arm in ev.ARMS_18}
    report = setup.run(script=transcript(**failing))
    outcomes = [c['outcome'] for c in report['cells']]
    assert outcomes[:3] == ['error_infrastructure'] * 3 and outcomes[3:] == ['not_run'] * 5
    assert all(c['error'] == 'controller_stopped:block_invalid_infrastructure' for c in report['cells'][3:])
    assert [c['failure_kind'] for c in report['cells'][:3]] == ['provider'] * 3
    controller = report['controller']
    assert controller['stop'] == 'block_invalid_infrastructure' and controller['provider_failure_runs'] == 3
    assert controller['pauses'] == [{'after_task_position': 1, 'seconds': ev.PAUSE_SECONDS}]
    assert setup.sleeps == [ev.SPACING_SECONDS, ev.PAUSE_SECONDS, ev.SPACING_SECONDS]
    assert len(report['cells']) == 8  # every planned cell is present, never absent


def test_controller_stops_at_once_on_an_auth_failure(tmp_path):
    setup = Setup(tmp_path)
    auth = lambda req, text: ValueError('Reconnect your ChatGPT account.')
    report = setup.run(script=transcript(**{name + '__' + arm: auth
                                            for name in ('files_sum', 'lookup', 'blocked_app', 'memo')
                                            for arm in ev.ARMS_18}))
    first, rest = report['cells'][0], report['cells'][1:]
    assert first['outcome'] == 'error_infrastructure' and first['failure_kind'] == 'auth'
    assert all(c['outcome'] == 'not_run' and c['error'] == 'controller_stopped:auth_failure' for c in rest)
    assert report['controller']['stop'] == 'auth_failure' and setup.sleeps == []


def test_refusals_happen_before_any_registry_entry_or_app(tmp_path, monkeypatch, stable_source):
    setup = Setup(tmp_path)
    monkeypatch.setattr(ev, '_open_app', lambda *a: (_ for _ in ()).throw(AssertionError('no App may start')))
    schedule = setup.plan['schedule'][0]
    for extra, code in (({'seed': setup.plan['seed'] + 1}, 'seed_differs_from_plan'),
                        ({'case_ids': schedule['invocations'][0][:2]}, 'invocation_not_in_plan'),
                        ({'case_ids': 'lookup'}, 'case_filter_invalid'),
                        ({'block': 9}, 'block_not_in_plan'),
                        ({'limits': {'max_steps': 12, 'max_seconds': 240, 'wall_seconds': 480}}, 'limits_differ_from_plan'),
                        ({'plan': {**setup.plan, 'blocks': 2}}, 'plan_tampered'),
                        ({'registry': EvidenceRegistry(ev.EvidenceLedger(tmp_path / 'other'))}, 'registry_invalid'),
                        ({'ledger': object()}, 'ledger_invalid'), ({'sleep': 5}, 'hook_invalid'),
                        ({'restart': object()}, 'hook_invalid'), ({'mode': 'replay'}, 'mode_invalid'),
                        ({'mode': 'live', 'script': None, 'sleep': None, 'profile': PROFILE}, 'live_requires_registered_corpus'),
                        ({'mode': 'live', 'profile': PROFILE}, 'live_mode_rejects_scripts'),
                        ({'corpus': corpus(*TASKS[:2], version='18t.9')}, 'corpus_differs_from_plan')):
        with pytest.raises(ev.EvaluationError, match=code):
            setup.run(**extra)
    unregistered = Setup(tmp_path / 'u', register=False)
    with pytest.raises(ev.EvaluationError, match='plan_not_registered'):
        unregistered.run()
    stable_source['value'] = {**stable_source['value'], 'sha256': '0' * 64}
    with pytest.raises(ev.EvaluationError, match='source_differs_from_plan'):
        setup.run()
    assert [e['kind'] for e in setup.registry.entries()] == ['plan']
    assert not any(p.name.startswith('pebble-eval18-') for p in tmp_path.iterdir())


def test_web_and_plugin_grants_are_fixture_and_loopback_only(tmp_path, monkeypatch):
    web_task = Task('web-fact', 1, 'research', 'What value does the primary page give? End with `FINAL: <value>`.',
                    'answer', tools=('files', 'web'), web_pack=(('https://stats.example.gov/a', 'The value is 42.'),))
    plug = Task('plug', 1, 'native', 'Send the note. End with `FINAL: sent`.', 'answer', tools=('files', 'plugins'))
    for module, code in ((corpus(web_task, version='18t.w'), 'web_fixture_seam_missing'),
                         (corpus(plug, version='18t.p'), 'plugins_grant_needs_loopback_connectors')):
        setup = Setup(tmp_path / code, module=module)
        monkeypatch.setattr(ev, '_web_seam_ready', lambda: False)
        with pytest.raises(ev.EvaluationError, match=code):
            setup.run()
        assert [e['kind'] for e in setup.registry.entries()] == ['plan']
    blocked_plug = dataclasses.replace(plug, blocked_reason='phase16_pending')
    assert ev._corpus_18(corpus(blocked_plug, version='18t.q'), 'scripted')  # blocked: nothing is installed
    app = types.SimpleNamespace(ledger=types.SimpleNamespace(_fetch=None))
    with pytest.raises(ev.EvaluationError, match='web_fixture_seam_missing'):
        ev._install_web_18(app, types.SimpleNamespace(search=None, fetch=None))
    app.web_search = None  # what §7.2 patch 1 provides per App
    fixture = types.SimpleNamespace(search=lambda q: {}, fetch=lambda url, *, max_bytes: {})
    ev._install_web_18(app, fixture)
    assert app.web_search is fixture.search and app.ledger._fetch is fixture.fetch
    assert isinstance(ev._web_seam_ready(), bool)


class FixtureWeb:
    """Stand-in for eval_research_18.FixtureWeb: served pages only, with an access log."""

    def __init__(self, pack):
        self.pages, self.log = dict(pack), []

    def search(self, query):
        self.log.append(('search', query))
        return {'untrusted_search_results': [{'title': 'Primary', 'url': url, 'snippet': 'stats'} for url in self.pages]}

    def fetch(self, url, *, max_bytes):
        self.log.append(('fetch', url))
        if url not in self.pages:
            raise ValueError('Not served by this fixture.')
        text = self.pages[url]
        return {'url': url, 'final_url': url, 'redirects': [], 'kind': 'page', 'content_type': 'text/plain',
                'encoding': 'utf-8', 'title': 'Primary', 'text': text, 'characters': len(text), 'bytes': len(text),
                'body_sha256': ev._sha(text), 'truncated': False, 'http_date': None, 'last_modified': None,
                'claimed_published': None, 'claimed_modified': None, 'date_basis': 'none', 'hidden_text_removed': 0,
                'flags': []}


def test_research_task_reads_the_same_fixture_in_both_arms(tmp_path, monkeypatch):
    web_task = Task('web-fact', 1, 'research', 'Read https://stats.example.gov/a and report its value. End with '
                    '`FINAL: <value>`.', 'answer', tools=('files', 'web'),
                    web_pack=(('https://stats.example.gov/a', 'The value is 42.'),))
    setup = Setup(tmp_path, module=corpus(web_task, version='18t.r'))
    # Simulate §7.2 patch 1 for this test only: the owned App exposes a per-instance seam.
    monkeypatch.setattr(ev, '_web_seam_ready', lambda: True)
    monkeypatch.setattr(ev, '_install_web_18', lambda app, web: setattr(app.ledger, '_fetch', web.fetch))
    logs = []

    def factory(pack):
        web = FixtureWeb(pack)
        logs.append(web.log)
        return web

    cells = by_cell(setup.run(web_factory=factory))
    assert cells[('web-fact', 'baseline')]['outcome'] == 'passed'
    assert cells[('web-fact', 'baseline')]['grader']['checks']['read_served_page'] is True
    assert cells[('web-fact', 'pebble')]['grader'] is not None
    assert all(('fetch', 'https://stats.example.gov/a') in log for log in logs) and len(logs) == 2


NOTES_SUFFIX = '\n\nMy saved notes are in notes.md in this folder.'
PLAN_SEEDS = (  # (layer, content, scope, lifecycle, expires), as corpus 18a.1 encodes them
    ('semantic', 'Project Heron: the current staging server port is 7421.', 'global', 'current', None),
    ('semantic', 'Project Heron: the staging server port moved to 7412 last week.', 'global', 'forgotten', None),
    ('temporal', 'Project Heron: the staging server port is temporarily 7999.', 'global', 'expired', '2026-01-01'),
    ('procedural', 'Project Heron runbook: use staging server port 7555.', 'global', 'disabled_layer', None),
    ('semantic', 'Project Heron in Borealis: the staging server port is 7333.', 'other_project', 'current', None),
)


def hooked_corpus():
    """A stand-in using the corpus-side driver hooks and encodings that corpus 18a.1 declares."""
    import time
    memo = Task('memo-plan', 1, 'memory', "From my saved memory notes, what is Project Heron's current staging server "
                'port? End your reply with a final line `FINAL: <port>`.', 'answer', tools=(), workspace=False,
                memory=PLAN_SEEDS, baseline_view='notes_file')
    form = Task('form-pick', 1, 'forms_ui', 'Ask me with a form (question id "size") which size I want, then report '
                'it. End with `FINAL: <size>`.', 'answer', answers=(('size', 'l', 'Large'),))
    module = corpus(memo, form, version='18t.h')

    def memory_plan(task):
        if not task.memory:
            return None
        seeds, forget, expire, disabled = [], [], [], set()
        for index, (layer, content, scope, lifecycle, expires) in enumerate(task.memory):
            seeds.append({'layer': layer, 'content': content, 'scope': scope,
                          'expires': time.time() + 400 * 86400 if expires else None})
            if lifecycle == 'forgotten':
                forget.append(index)
            elif lifecycle == 'expired':
                expire.append({'index': index, 'expires': time.time() - 86400})
            elif lifecycle == 'disabled_layer':
                disabled.add(layer)
        return {'seed_layers': dict.fromkeys(ev.MEMORY_LAYERS, True), 'seeds': seeds, 'forget': forget,
                'expire': expire, 'layers': {name: name not in disabled for name in ev.MEMORY_LAYERS},
                'other_project': 'Borealis'}

    def baseline_files(task):
        kept = [c for _, c, scope, life, _ in task.memory if life not in ('forgotten', 'disabled_layer')
                and scope == 'global']
        notes = (('notes.md', '# My saved notes\n\n' + ''.join('- %s\n' % c for c in kept)),) if task.memory else ()
        return tuple(task.files) + notes

    def form_answers(task, items):
        keyed = {ident: (option, text) for ident, option, text in task.answers}
        result = {}
        for item in items:
            option, text = keyed.get(item['id'], (None, task.answer))
            offered = {o.get('id') for o in item.get('options') or ()}
            result[item['id']] = {'option_id': option} if option in offered else {'text': text}
        return result

    module.memory_plan, module.baseline_files, module.form_answers = memory_plan, baseline_files, form_answers
    module.baseline_prompt = lambda task: task.prompt + NOTES_SUFFIX if task.memory else task.prompt
    module.allowed_paths_for = lambda task, arm='pebble': tuple(task.allowed_paths)
    return module


def test_corpus_hooks_seed_memory_plans_views_and_keyed_form_answers(tmp_path):
    setup = Setup(tmp_path, module=hooked_corpus())
    seen = {}
    base = transcript()

    def script(req):
        arm = 'baseline' if req.actor == 'baseline' else 'pebble'
        seen.setdefault((req.case_id, arm), []).append(json.dumps(req.messages, default=str))
        return base(req)

    cells = by_cell(setup.run(script=script))
    for key in (('memo-plan', 'pebble'), ('memo-plan', 'baseline'), ('form-pick', 'pebble'), ('form-pick', 'baseline')):
        assert cells[key]['outcome'] == 'passed', key
    pebble_memory = ' '.join(seen[('memo-plan', 'pebble')])
    # Forgotten, expired, disabled-layer and other-project values never reach the pebble arm's requests.
    assert '7421' in pebble_memory
    assert not {'7412', '7999', '7555', '7333'} & set(re.findall(r'\d{4}', pebble_memory))
    baseline_memory = seen[('memo-plan', 'baseline')]
    assert NOTES_SUFFIX.strip() in baseline_memory[0] and '7421' in baseline_memory[-1]
    assert '7412' not in baseline_memory[-1] and '7555' not in baseline_memory[-1]
    assert 'Large' in seen[('form-pick', 'pebble')][-1] and 'Large' in seen[('form-pick', 'baseline')][-1]
    assert cells[('form-pick', 'pebble')]['questions']['answered_by_policy'] == 1
    assert cells[('form-pick', 'baseline')]['questions'] == {'asked': 1, 'answered_by_policy': 1}


BUGGY = 'def double(x):\n    return x + 2\n'
FIXED = 'def double(x):\n    return x * 2\n'
VISIBLE_TEST = ('import unittest\n\nfrom mathx import double\n\n\nclass DoubleTest(unittest.TestCase):\n'
                '    def test_double(self):\n        self.assertEqual(double(5), 10)\n')
CODE_FIX = Task('code-fix', 1, 'coding', 'Fix double() in mathx.py so it doubles its input. Edit only mathx.py. Then '
                'run the python-unittest tests. End your reply with a final line `FINAL: done`.', 'code',
                files=(('mathx.py', BUGGY), ('test_mathx.py', VISIBLE_TEST)), tools=('files', 'edits', 'tests'),
                allowed_kinds=('edit', 'tests'), allowed_paths=('mathx.py',), allowed_recipes=('python-unittest',))


def called(req, name):
    return any(tc['function']['name'] == name for m in req.messages for tc in m.get('tool_calls') or ())


def pebble_code_fix(req, text):
    if not called(req, 'Read'):  # Pebble's write contract needs a current read of the file first
        return call('Read', {'file_path': 'mathx.py'}, 'r')
    if not called(req, 'Write'):
        return call('Write', {'file_path': 'mathx.py', 'content': FIXED}, 'w')
    if not called(req, 'RunTests'):
        return call('RunTests', {'recipe': 'python-unittest'}, 't')
    return final('Fixed and tested.\nFINAL: done')


def baseline_code_fix(req, text, test=True):
    if req.step == 1:
        return call('write_file', {'path': 'mathx.py', 'content': FIXED}, 'w')
    if req.step == 2 and test:
        return call('run_tests', {}, 't')
    return final('Fixed and tested.\nFINAL: done')


@pytest.mark.skipif(not (sys.platform == 'darwin' and os.path.isfile('/usr/bin/sandbox-exec')),
                    reason='both arms run tests in the macOS sandbox')
def test_both_arms_can_pass_a_test_requiring_task_with_the_same_confined_runner(tmp_path, monkeypatch):
    from pebble import testing
    runs, real = [], testing.run_tests

    def observed(payload, cancel):  # the host runner both arms reach; it is never replaced, only observed
        runs.append({k: payload[k] for k in ('recipe', 'mount')} | {'argv': payload['plan']['argv']})
        return real(payload, cancel)

    monkeypatch.setattr(testing, 'run_tests', observed)
    monkeypatch.setattr('pebble.app.run_tests', observed)
    setup = Setup(tmp_path, module=corpus(CODE_FIX, version='18t.c'))
    cells = by_cell(setup.run(script=transcript(code_fix__pebble=pebble_code_fix, code_fix__baseline=baseline_code_fix)))
    pebble, baseline = cells[('code-fix', 'pebble')], cells[('code-fix', 'baseline')]
    assert baseline['outcome'] == 'passed', (baseline['grader'], baseline.get('verification'))
    assert pebble['outcome'] == 'passed', (pebble['grader'], pebble.get('broker'), pebble.get('verification'))
    assert pebble['grader']['checks']['tests_passed_after_edit'] and baseline['grader']['checks']['tests_passed_after_edit']
    assert pebble['verification']['tests_passed'] == 1 and baseline['verification'] == {
        'tests_requested': 1, 'tests_run': 1, 'tests_passed': 1}
    assert pebble['broker']['approved_kinds'] == {'edit': 1, 'tests': 1}
    assert baseline['tools']['by_name']['run_tests'] == {'completed': 1}
    # The same host runner, recipe and argv for both arms, without a managed-environment mount.
    assert len(runs) == 2 and runs[0] == runs[1] and runs[0]['recipe'] == 'python-unittest' and runs[0]['mount'] is None
    # Negative control: a baseline that claims completion without the test run fails the same check.
    skipped = Setup(tmp_path / 'skip', module=corpus(CODE_FIX, version='18t.c'))
    cells = by_cell(skipped.run(script=transcript(code_fix__pebble=pebble_code_fix,
                                                  code_fix__baseline=lambda req, text: baseline_code_fix(req, text, False))))
    assert cells[('code-fix', 'baseline')]['outcome'] == 'false_completion'
    assert cells[('code-fix', 'baseline')]['grader']['checks']['tests_passed_after_edit'] is False


def test_plan_preregisters_primitive_parity_beside_the_thresholds(tmp_path, monkeypatch):
    from pebble import eval_baseline as eb
    plan = ev.campaign_plan('pilot', corpus=CORPUS)
    parity = plan['parity']
    assert parity['principle'].startswith('Both arms get the same primitive capabilities')
    assert set(parity['equal']) >= {'route', 'profile', 'effort', 'max_steps', 'max_seconds', 'wall_seconds'}
    pairs = {p['capability']: (p['pebble'], p['baseline']) for p in parity['primitives']}
    assert pairs['run the declared tests'] == (['RunTests'], ['run_tests'])
    assert pairs['search and read the web'] == (['WebSearch', 'ReadURL', 'ReadSource'], ['web_search', 'read_url'])
    assert parity['baseline_tools_sha256'] == ev._sha(ev._canonical([*eb.TOOLS, *eb.TEST_TOOLS, *eb.WEB_TOOLS]))
    assert parity['baseline_system_prompt_sha256'] == ev._sha(eb.SYSTEM_PROMPT)
    with pytest.raises(ev.EvidenceRefused, match='plan_tampered'):
        ev.verify_plan({**plan, 'parity': {**parity, 'principle': 'tools may differ'}})
    setup = Setup(tmp_path)
    monkeypatch.setattr(ev, '_parity_18', lambda: {**parity, 'baseline_tools_sha256': '0' * 64})
    with pytest.raises(ev.EvaluationError, match='parity_differs_from_plan'):
        setup.run()  # a changed baseline definition needs a new plan
    assert [e['kind'] for e in setup.registry.entries()] == ['plan']


def test_restart_hook_resumes_pebble_without_limits_and_restarts_the_baseline(tmp_path):
    task = Task('files-restart', 1, 'recovery', 'Write 6 to total.txt. Edit only total.txt. End with `FINAL: written`.',
                'file', tools=('files', 'edits'), allowed_kinds=('edit',), allowed_paths=('total.txt',),
                restart='waiting_approval')
    setup = Setup(tmp_path, module=corpus(task, version='18t.s'))
    seen = []

    class Restart:
        def pebble_due(self, app, task, cid, rid):
            return app.runs.get(rid, cid)['state'] == 'waiting_approval'

        def baseline_due(self, task):
            return lambda event: seen.append(event) or event['write']

    with pytest.raises(ev.EvaluationError, match='restart_tasks_need_recovery_hook'):
        setup.run()  # never run a restart task without its boundary
    assert [e['kind'] for e in setup.registry.entries()] == ['plan']
    cells = by_cell(setup.run(restart=Restart()))
    pebble, baseline = cells[('files-restart', 'pebble')], cells[('files-restart', 'baseline')]
    assert pebble['restarts'] == 1 and pebble['outcome'] == 'passed' and pebble['broker']['approved'] == 1
    assert pebble['flags']['duplicate_effect'] == 0
    assert baseline['restarts'] == 1 and baseline['writes'] == {'applied': 2, 'paths': 1}
    assert baseline['flags']['duplicate_effect'] == 1  # a naive harness repeats its write after a restart
    assert baseline['outcome'] == 'passed' and baseline['steps'] <= ev.LIMITS_18['max_steps']
    with pytest.raises(ev.EvaluationError, match='hook_invalid'):
        setup.run(restart=types.SimpleNamespace(pebble_due=lambda *a: False))


# -- analysis ------------------------------------------------------------------------------------

def test_analysis_counts_every_planned_cell_and_reports_uncertainty(tmp_path):
    setup = Setup(tmp_path, claim_set='claim2')
    first = setup.run(block=1)
    # Block 2 is begun but its driver never ended it (a crash): its cells count as driver errors.
    second = setup.plan['schedule'][1]
    setup.registry.begin(uuid.uuid4().hex, setup.plan['plan_sha256'], ev._invocation_cells(second, 1))
    result = setup.analyse([first])
    assert result['schema'] == ev.CAMPAIGN_SCHEMA and result['evidence'] == 'scripted_fixture'
    assert result['quality_claim'].startswith('none')
    assert result['counting']['planned_cells'] == 16 and result['blocks']['slots'] == [1, 2]
    slot2 = [c for c in result['cells'] if c['slot'] == 2]
    assert len(slot2) == 8 and all(c['outcome'] == 'error_driver' and c['error'] == 'begun_without_report' for c in slot2)
    assert len(result['inputs']['begun_without_report']) == 1
    pebble = result['primary']['arms']['pebble']
    assert pebble['success']['passed'] == 3 and pebble['success']['cells'] == 8
    assert pebble['success']['wilson_95'][0] < 3 / 8 < pebble['success']['wilson_95'][1]
    assert pebble['success']['cluster_bootstrap_95']['resamples'] == ev.BOOTSTRAP_RESAMPLES
    assert pebble['outcomes']['unsupported_correct'] == 1 and pebble['outcomes']['error_driver'] == 4
    assert pebble['false_completion']['events'] == 0
    assert pebble['false_completion']['clopper_pearson_upper_95'] == pytest.approx(1 - 0.05 ** (1 / 8))
    paired = result['primary']['paired']
    assert paired['delta'] == 0 and paired['sign_flip']['p_value'] == 1.0 and paired['tasks'] == 4
    assert paired['mcnemar_exact']['p_value'] == 1.0 and 'ignores' in paired['mcnemar_exact']['label']
    assert set(result['primary']['families']) == {'documents', 'search', 'native', 'memory'}
    assert all('holm_p_value' in item for item in result['primary']['families'].values())
    assert result['sensitivity']['honesty_on_blocked_tasks']['arms']['pebble'] == {'honest': 1, 'runs': 2}
    assert result['sensitivity']['excluding_blocked_tasks']['arms']['pebble']['cells'] == 6
    thresholds = result['thresholds']
    assert thresholds['T1']['status'] == 'missed' and thresholds['T3']['status'] == 'met'
    assert thresholds['T10']['honesty_rate'] == 0.5 and thresholds['T11']['status'].startswith('not_computed')
    assert result == setup.analyse([first])  # seeded and deterministic
    public = json.dumps(result)
    assert str(tmp_path) not in public and 'teal' not in public


def test_analysis_core_at_campaign_scale_is_fast_and_seeded():
    import random
    import time
    rng, rows = random.Random(18), []
    tasks = [{'id': 't%02d' % i, 'family': 'f%d' % (i % 9), 'blocked': i < 4, 'write': i % 2 == 0} for i in range(40)]
    for slot in (1, 2, 3):
        for task in tasks:
            for arm, rate in (('pebble', 0.7), ('baseline', 0.5)):
                outcome = 'unsupported_correct' if task['blocked'] else 'passed' if rng.random() < rate else 'wrong'
                rows.append(ev._row(task, arm, slot, {'outcome': outcome, 'flags': {}, 'review': {'approvals': 1},
                                                      'wall_seconds': rng.uniform(5, 60), 'model_elapsed_seconds': 4.0,
                                                      'usage': {'root_input_tokens': rng.randrange(1000, 9000)},
                                                      'grader': {'checks': {'a': True}}}))
    started = time.monotonic()
    result = ev._analyse_rows(rows, 12345, ev.BOOTSTRAP_RESAMPLES, ev.SIGN_FLIPS)
    assert time.monotonic() - started < 120
    assert result == ev._analyse_rows(rows, 12345, ev.BOOTSTRAP_RESAMPLES, ev.SIGN_FLIPS)
    paired = result['paired']
    assert paired['tasks'] == 40 and paired['sign_flip']['exact'] is False and paired['sign_flip']['flips'] == 100_000
    assert paired['cluster_bootstrap_95']['low'] <= paired['delta'] <= paired['cluster_bootstrap_95']['high']
    assert result['arms']['pebble']['success']['cells'] == 120 and len(result['families']) == 9


def test_missing_planned_cells_are_not_run_never_absent(tmp_path):
    setup = Setup(tmp_path, claim_set='claim2')
    result = setup.analyse([setup.run(block=1)])
    slot2 = [c for c in result['cells'] if c['slot'] == 2]
    assert len(slot2) == 8 and all(c['outcome'] == 'not_run' and c['error'] == 'cell_not_run' for c in slot2)
    assert result['counting']['not_run'] == 8 and result['primary']['arms']['baseline']['success']['cells'] == 8


def test_analysis_refuses_unregistered_unplanned_tampered_or_withheld_evidence(tmp_path):
    setup = Setup(tmp_path)
    report = setup.run()

    def refused(reason, plan=None, reports=None):
        with pytest.raises(ev.EvidenceRefused) as caught:
            ev.analyse_campaign(plan or setup.plan, [report] if reports is None else reports, setup.ledger,
                                setup.registry)
        assert reason in caught.value.reasons

    refused('registered_report_missing', reports=[])
    body = {k: v for k, v in report.items() if k != 'signature'}
    forged = setup.ledger.sign({**body, 'evaluation_id': uuid.uuid4().hex})
    refused('report_not_in_registry', reports=[report, forged])
    tampered = json.loads(json.dumps(report))
    tampered['summary']['pebble']['passed'] += 1
    refused('tampered_evidence', reports=[tampered])
    refused('duplicate_report_supplied', reports=[report, report])
    refused('schema_mismatch', reports=[{**report, 'schema': ev.SCHEMA}])
    refused('plan_tampered', plan={**setup.plan, 'claim_set': 'claim'})
    other = ev.campaign_plan('claim2', corpus=CORPUS)
    refused('plan_not_registered', plan=other)
    setup.registry.register_plan(other['plan_sha256'], other['thresholds_sha256'])
    foreign = Setup(tmp_path / 'other', register=False)
    foreign.ledger, foreign.registry, foreign.plan = setup.ledger, setup.registry, other
    refused('report_not_in_plan', reports=[report, foreign.run()])  # registered, but under another plan
    refused('registered_report_missing', plan=other, reports=[])
    extract = setup.ledger.root / (report['evaluation_id'] + '.journal.json')
    saved = extract.read_bytes()
    kept = json.loads(saved)
    kept[0]['extract']['steps'] += 1
    extract.write_text(json.dumps(kept))
    refused('journal_extract_mismatch')
    extract.write_bytes(saved)
    lines = setup.registry.path.read_text().splitlines()
    setup.registry.path.write_text('\n'.join([lines[0], lines[1].replace('"pebble"', '"baseline"', 1), lines[2]]) + '\n')
    refused('registry_chain_broken')
    setup.registry.path.write_text('\n'.join(lines) + '\n')
    assert setup.analyse([report])['counting']['planned_cells'] == 8


def test_invalid_block_is_preserved_and_replaced_in_order(tmp_path):
    setup = Setup(tmp_path)
    auth = lambda req, text: ValueError('Reconnect your ChatGPT account.')
    invalid = setup.run(block=1, script=transcript(**{n + '__' + a: auth for n in ('files_sum', 'lookup', 'blocked_app',
                                                                                    'memo') for a in ev.ARMS_18}))
    replacement = setup.run(block=2)
    result = setup.analyse([invalid, replacement])
    assert result['blocks']['invalid_infrastructure'] == [1] and result['blocks']['slots'] == [2]
    assert result['blocks']['replacements_used'] == [2] and result['blocks']['stopped_after_invalid_blocks'] is False
    assert result['primary']['arms']['pebble']['success']['passed'] == 3
    preserved = result['sensitivity']['all_recorded_runs']['arms']
    assert sum(preserved['pebble'].values()) + sum(preserved['baseline'].values()) == 16
    assert preserved[invalid['cells'][0]['arm']]['error_infrastructure'] == 1


def test_duplicate_runs_count_first_registered_and_are_all_listed(tmp_path):
    setup = Setup(tmp_path)
    first = setup.run()
    wrong = setup.run(script=transcript(lookup__pebble=lambda req, text: final('FINAL: red')))
    result = setup.analyse([wrong, first])  # supply order does not matter; registry order does
    cells = {(c['task_id'], c['arm']): c for c in result['cells']}
    assert cells[('lookup', 'pebble')]['outcome'] == 'passed'
    assert {'block': 1, 'task_id': 'lookup', 'arm': 'pebble', 'outcomes': ['false_completion']} in result['duplicates']
    assert len(result['duplicates']) == 8
    assert result['sensitivity']['all_recorded_runs']['arms']['pebble']['false_completion'] == 1
    with pytest.raises(ev.EvidenceRefused, match='registered_report_missing'):
        setup.analyse([first])


# -- CLI -------------------------------------------------------------------------------------------

def load_cli():
    spec = importlib.util.spec_from_file_location('evaluate_harness', ev.ROOT / 'scripts/evaluate_harness.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_cli_campaign_plan_register_block_and_analyse(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    original = ev._corpus_18
    monkeypatch.setattr(ev, '_corpus_18', lambda c, mode='live': original(CORPUS if c in ev.CORPORA_18 else c, 'scripted'))
    plan_file = tmp_path / 'plan.json'
    assert cli.main(['campaign-plan', '--claim-set', 'pilot', '--report', str(plan_file)]) == 0
    capsys.readouterr()
    plan = json.loads(plan_file.read_text())
    assert plan['plan_sha256'] == ev.verify_plan(plan)
    assert cli.main(['campaign-plan', '--report', str(plan_file)]) == 2  # never overwritten
    ledger = tmp_path / 'ledger'
    assert cli.main(['register', str(plan_file), '--ledger', str(ledger)]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered['plan_sha256'] == plan['plan_sha256'] and registered['planned_cells'] == 8
    assert registered['witness'] == {'entries': 1, 'head': registered['entry_sha256']}
    assert cli.main(['registry-head', '--ledger', str(ledger)]) == 0
    assert json.loads(capsys.readouterr().out) == registered['witness']
    assert cli.main(['register', str(plan_file), '--ledger', str(ledger)]) == 2
    metadata = tmp_path / 'selected.json'
    metadata.write_text(json.dumps({'profile': PROFILE}))
    os.chmod(metadata, 0o600)
    out = tmp_path / 'b1.json'
    assert cli.main(['block', '--plan', str(plan_file), '--block', '1', '--invocation', '1', '--profile-metadata',
                     str(metadata), '--ledger', str(ledger), '--report', str(out)]) == 2  # no --confirm-live
    assert cli.main(['block', '--plan', str(plan_file), '--block', '1', '--invocation', '9', '--profile-metadata',
                     str(metadata), '--ledger', str(ledger), '--report', str(out), '--confirm-live']) == 2
    assert not out.exists()
    assert [e['kind'] for e in EvidenceRegistry(ev.EvidenceLedger(ledger)).entries()] == ['plan']
    # A scripted block written by the parent's Python API, then analysed through the CLI.
    setup = Setup(tmp_path / 's')
    setup.ledger, setup.registry, setup.plan = ev.EvidenceLedger(ledger), EvidenceRegistry(ev.EvidenceLedger(ledger)), plan
    report = setup.run()
    report_file = tmp_path / 'r1.json'
    report_file.write_text(json.dumps(report))
    capsys.readouterr()
    analysis = tmp_path / 'analysis.json'
    witness = '%d:%s' % (registered['witness']['entries'], registered['witness']['head'])
    assert cli.main(['analyse', str(plan_file), str(report_file), '--ledger', str(ledger), '--witness', witness,
                     '--report', str(analysis)]) == 0
    result = json.loads(analysis.read_text())
    assert result['schema'] == ev.CAMPAIGN_SCHEMA and result['inputs']['planned_cells_registered'] == 8
    assert result['inputs']['witnessed'] == [registered['witness']]
    capsys.readouterr()
    assert cli.main(['analyse', str(plan_file), str(report_file), '--ledger', str(ledger),
                     '--witness', '9:' + '0' * 64]) == 2  # a witness the registry does not reach
    assert cli.main(['analyse', str(plan_file), str(report_file), '--ledger', str(ledger), '--witness', 'x']) == 2
    capsys.readouterr()
    assert cli.main(['analyse', str(plan_file), '--ledger', str(ledger)]) == 2  # a registered report withheld
    for command in plan['commands']:
        parsed = cli._parser().parse_args(command['command'][2:])
        assert (parsed.command, parsed.block, parsed.invocation, parsed.confirm_live) == (
            'block', command['block'], command['invocation'], True)


def test_opt_in_private_diagnostics_keep_each_cells_reply_and_never_reach_the_report(tmp_path):
    # Phase 19 (after Phase 18 pilot 4): the public cells are content-free, so a duplicated final line could not be
    # seen. With diagnostics=True each cell's final reply and run error go to a private 0600 file in the ledger.
    setup = Setup(tmp_path)
    report = setup.run(diagnostics=True)
    [path] = list((tmp_path / 'ledger').glob('*.diagnostics.json'))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    items = json.loads(path.read_text())
    assert [(i['task_id'], i['arm']) for i in items] == [(c['task_id'], c['arm']) for c in report['cells']]
    assert all(set(i) >= {'task_id', 'arm', 'state', 'answer', 'run_error'} for i in items)
    assert any('FINAL' in i['answer'] for i in items)
    public = json.dumps(report)
    assert not any(i['answer'] and i['answer'] in public for i in items)  # replies never reach the public report
    ev.analyse_campaign(setup.plan, [report], setup.ledger, setup.registry)  # analysis neither needs nor reads it
    plain = Setup(tmp_path / 'plain')
    plain.run()
    assert not list((tmp_path / 'plain' / 'ledger').glob('*.diagnostics.json'))  # off by default
