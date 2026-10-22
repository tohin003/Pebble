"""Independent attack tests for the Phase 10a evaluator boundary.

Scope: backend/pebble/evaluation.py — the restricted-Python static gate and
host interpreter, hidden-check grading, the grading-child result contract, the
host approval helper's run ownership, delegation attribution and signed-report
ownership. Candidate source strings are only ever handed to the checked-in
`static_gate` / `interpret_checks`; nothing here uses eval/exec, and no model,
network, credential, live ledger or user data is involved.

These are *language-isolation* tests run in-process. They say nothing about the
grading child's OS sandbox (macOS `sandbox-exec`, or plain process isolation
elsewhere), installed builds or other platforms. Weaknesses found during this
review are reported in .runtime/phase10-claude/security-proposals/evaluator-review.md
rather than encoded here as expected failures.
"""

import copy
import json
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

import pebble.evaluation as evaluation
from pebble.agent_manager import Agents
from pebble.app import App
from pebble.controller import CURRENT
from pebble.evaluation import (EvidenceLedger, EvidenceRefused, ORACLES, SCHEMA, interpret_checks,
                               static_gate)
from pebble.permissions import DEFAULT
from pebble.policy import Workspace


ORACLE = ORACLES['code-median']
INPUTS = [{'name': c['name'], 'args': list(c['args']), 'tuple': bool(c.get('tuple'))} for c in ORACLE['checks']]
PROBE = [{'name': 'probe', 'args': [3, 1, 2], 'tuple': False}]
PROFILE = {'id': 'fixture-cloud', 'kind': 'openai', 'name': 'Fixture provider', 'url': 'https://unused.invalid',
           'model': 'fixture', 'context': 8192, 'budget': 4096, 'tools': True}

# A correct, non-mutating candidate inside the documented subset (positive control).
CORRECT = '''def median(values):
    if len(values) == 0:
        raise ValueError("empty")
    data = sorted(values)
    n = len(data)
    mid = n // 2
    return data[mid] if n % 2 else (data[mid - 1] + data[mid]) / 2
'''


def candidate(body, header=''):
    return header + 'def median(values):\n' + ''.join('    ' + line + '\n' for line in body.splitlines())


def hidden_passed(results):
    return {c['name'] for c, r in zip(ORACLE['checks'], results) if evaluation._meets(c, r)}


# -- 1. language isolation: reflective, module, stdout and file access ---------------

@pytest.mark.parametrize('header,body', [
    ('', 'open(MARKER, "w").write("x")\nreturn 0'),
    ('', 'print("leak")\nreturn 0'),
    ('', 'return __import__("os").getpid()'),
    ('', 'return getattr(values, "copy")()'),
    ('', 'return len(values.__class__.__mro__)'),
    ('', 'return type(values)'),
    ('', 'return len(globals())'),
    ('import os\n', 'return 0'),
    ('from functools import partial\n', 'return 0'),
    ('import math as m\n', 'return m'),                       # module object as a value
    ('import statistics as s\n', 'return len([s])'),
    ('import math\n', 'return math.floor.__self__'),           # member traversal
    ('def wrap(f):\n    return f\n@wrap\n', 'return 0'),       # decorator
    ('', 'try:\n    return 1 // 0\nexcept Exception as e:\n    return e.__traceback__'),
    ('', 'return max(*values)'),
    ('', 'values.extra = 1\nreturn 0'),
])
def test_reflective_module_and_io_access_is_refused_before_any_effect(tmp_path, capsys, header, body):
    marker = tmp_path / 'marker.txt'
    source = candidate(body.replace('MARKER', json.dumps(str(marker))), header)
    assert static_gate(source.encode(), 'median') is not None
    results = interpret_checks(source, 'median', INPUTS)
    assert [r['status'] for r in results] == ['halted'] * len(INPUTS)
    assert not marker.exists()
    captured = capsys.readouterr()
    assert captured.out == '' and captured.err == ''


# -- 2. the interpreter re-checks members for names the gate admits ------------------

@pytest.mark.parametrize('header,body', [
    ('', 'return "abc".count("a")'),                            # str has no admitted methods
    ('', 'return ValueError("x").count("x")'),                  # exception objects expose nothing
    ('', 't = tuple(values)\nt.append(1)\nreturn len(t)'),       # list method on a tuple
    ('from functools import cmp_to_key\n', 'k = cmp_to_key(lambda a, b: 0)(1)\nreturn k.index(1)'),
])
def test_runtime_member_table_holds_for_names_the_gate_admits(header, body):
    source = candidate(body, header)
    assert static_gate(source.encode(), 'median') is None, 'This case targets the runtime re-check.'
    results = interpret_checks(source, 'median', PROBE)
    assert results[0]['status'] == 'raised' and results[0]['value'] is None


# -- 3. budget halts cannot be caught by candidate except clauses --------------------

@pytest.mark.parametrize('body,reason', [
    ('try:\n    while True:\n        pass\nexcept Exception:\n    return 1', 'operation_budget'),
    ('def again(n):\n    try:\n        return again(n + 1)\n    except Exception:\n        return -1\nreturn again(0)',
     'call_depth'),
])
def test_candidate_except_clauses_cannot_catch_budget_halts(body, reason):
    results = interpret_checks(candidate(body), 'median', PROBE)
    assert results[0]['status'] == 'halted' and results[0]['reason'] == reason
    assert results[0]['value'] is None


# -- 4. direct value explosions stop inside the interpreter budget -------------------

@pytest.mark.parametrize('body', [
    'return len("ab" * 1000000)',
    'return len([0] * 1000000)',
    'return 3 ** 100000',
    'return len(range(10 ** 12))',
    'x = 3\nwhile True:\n    x = x * x',
    'return len([i for i in range(1000) for j in range(1000)])',
    'return len(f"{1:>99999}")',
    'def deeper(n):\n    return deeper(n + 1)\nreturn deeper(0)',
])
def test_direct_value_and_depth_explosions_halt_within_budget(body):
    started = time.monotonic()
    results = interpret_checks(candidate(body), 'median', PROBE)
    assert results[0]['status'] == 'halted'
    assert results[0]['reason'] in {'value_budget', 'operation_budget', 'call_depth'}
    assert time.monotonic() - started < 15


# -- 5. type confusion and non-finite returns never satisfy hidden checks ------------

@pytest.mark.parametrize('body', [
    'return True',
    'return "2.5"',
    'return float("nan")',
    'return float("inf")',
    'return 2 ** 64',
    'return [2.5]',
    'return None',
])
def test_type_confusion_and_non_finite_returns_pass_no_hidden_check(body):
    results = interpret_checks(candidate(body), 'median', INPUTS)
    assert hidden_passed(results) == set()


# -- 6. grading integrity: in-place mutation is caught even with correct values ------

def test_in_place_mutation_fails_only_the_unmutated_check():
    assert hidden_passed(interpret_checks(CORRECT, 'median', INPUTS)) == {c['name'] for c in ORACLE['checks']}
    mutating = CORRECT.replace('data = sorted(values)',
                               'data = values if isinstance(values, list) else list(values)\n    data.sort()')
    failed = {c['name'] for c in ORACLE['checks']} - hidden_passed(interpret_checks(mutating, 'median', INPUTS))
    assert failed == {'hidden_input_not_mutated'}


# -- 7. the grading child gets no expectations; malformed output fails closed --------

def _evidence(source):
    before, after = 'a' * 64, evaluation._sha(source)
    return evaluation.Evidence(before={'stats_util.py': before}, after={'stats_util.py': after},
                               files={'stats_util.py': source},
                               actions=[{'kind': 'edit', 'status': 'applied', 'path': 'stats_util.py',
                                         'hash': after, 'exit_code': None}])


def test_grading_child_receives_no_expected_values_and_malformed_results_fail_closed(tmp_path, monkeypatch):
    case, source = evaluation.BY_ID['code-median'], CORRECT.encode()
    honest = interpret_checks(CORRECT, 'median', INPUTS)
    sent, reply = [], {}

    def fake_run(command, **kwargs):
        sent.append(kwargs['input'])
        return SimpleNamespace(returncode=reply['code'], stdout=reply['stdout'])
    monkeypatch.setattr(evaluation.subprocess, 'run', fake_run)

    def grade(code, stdout):
        reply.update(code=code, stdout=stdout)
        return evaluation._grade_python(case, ORACLE, _evidence(source), tmp_path)

    control = grade(0, json.dumps(honest).encode())
    assert control['passed'] is True and control['hidden_passed'] == len(INPUTS)
    request = json.loads(sent[0])
    assert set(request) == {'source', 'function', 'inputs'}
    assert all(set(item) == {'name', 'args', 'tuple'} for item in request['inputs'])

    def edited(index, **changes):
        rows = copy.deepcopy(honest)
        rows[index].update(changes)
        return json.dumps(rows).encode()
    forged = [
        (0, json.dumps(honest[:-1]).encode()),                       # a result dropped
        (0, edited(0, name='hidden_renamed')),                       # result re-labelled
        (0, edited(0, value='2.5')),                                 # non-numeric value
        (0, edited(0, status='passed')),                             # invented status
        (0, edited(7, status='halted', reason='escaped')),           # invented halt reason
        (0, b'{"all": "passed"}'),
        (0, b'not json'),
        (1, json.dumps(honest).encode()),                            # child failed
        (0, json.dumps(honest).encode() + b' ' * 70_000),            # oversized output
    ]
    for code, stdout in forged:
        verdict = grade(code, stdout)
        assert verdict['passed'] is False and verdict['hidden_passed'] == 0
        assert verdict['checks']['hidden_behaviour'] is False


# -- App fixture for the host-helper boundaries ----------------------------------------

def _no_model(*args, **kwargs):
    raise AssertionError('Evaluator security fixtures make no model request.')


@pytest.fixture
def host_app(tmp_path, monkeypatch):
    monkeypatch.setattr(Agents, 'start', lambda self: None)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'notes.txt').write_text('Synthetic note for child reads.\n')
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *a: None)
    assert app.jobs.close()
    monkeypatch.setattr(app.model, 'current', lambda: dict(PROFILE))
    monkeypatch.setattr(app.model, 'complete', _no_model)
    monkeypatch.setattr(app.guard, 'handle', lambda *a, **k: {'action': 'allow', 'reason': 'fixture'})
    app.store.set_setting('permissions', {'mode': 'ask', 'tools': dict(DEFAULT['tools'])})
    yield app, project
    _release(app)
    app.close()


def _release(app):
    app.active = app.active_run = None
    app.controller.live = None
    CURRENT.set(None)


def _start_run(app, project, cid=None):
    for live in app.runs.list():
        if live['state'] in {'queued', 'running', 'retry_wait'}:
            app.runs.transition(live['id'], 'paused')
    chat = app.chat(cid) if cid else app.new_chat(str(project))
    config = app.runtime_config(chat, False, {'max_steps': 64, 'max_seconds': 300})
    rid = app.runs.create(chat['id'], 'Synthetic evaluator fixture run.', config)
    app.runs.transition(rid, 'running')
    app.controller.live = None
    app.controller.enter(app.runs.get(rid, chat['id']))
    app.active, app.active_run = chat['id'], rid
    return chat['id'], rid


def _status(app, cid, action_id):
    return next(a['status'] for a in app.store.actions(cid) if a['id'] == action_id)


# -- 8. the host approval helper never touches actions this run does not own ---------

def test_host_review_approves_only_exact_effects_owned_by_this_run(host_app):
    app, project = host_app
    ws = Workspace(project)
    case = evaluation.Case('security-ownership', 1, 'files', 'Synthetic.', 'json_exact', tools=('files', 'edits'),
                           allowed_kinds=('edit',), allowed_paths=('a.txt', 'b.txt'))
    cid, rid = _start_run(app, project)
    owned = app.execute('Write', {'file_path': 'a.txt', 'content': 'approved\n'}, ws, cid, call_id='w-a')['action_id']
    off_scope = app.execute('Write', {'file_path': 'c.txt', 'content': 'off scope\n'}, ws, cid, call_id='w-c')['action_id']
    _, other_rid = _start_run(app, project, cid)  # same chat, another run
    sibling = app.execute('Write', {'file_path': 'b.txt', 'content': 'other run\n'}, ws, cid, call_id='w-b')['action_id']
    payload = next(a['payload'] for a in app.store.actions(cid) if a['id'] == owned)
    forged = app.store.action(cid, 'edit', {**payload, 'after': 'forged\n'})  # no ownership record
    owners = {row['action_id']: row['run_id'] for row in app.store.query('SELECT action_id,run_id FROM run_action_owners')}
    assert owners == {owned: rid, off_scope: rid, sibling: other_rid}  # fixture precondition
    _release(app)

    counters = evaluation._Counters()
    evaluation._review_pending(app, case, cid, rid, ws.root, counters)
    assert (project / 'a.txt').read_text() == 'approved\n' and _status(app, cid, owned) == 'applied'
    assert _status(app, cid, off_scope) == 'rejected' and not (project / 'c.txt').exists()
    # Foreign actions are neither approved nor rejected, and change no bytes.
    assert _status(app, cid, sibling) == 'pending' and not (project / 'b.txt').exists()
    assert _status(app, cid, forged) == 'pending'
    assert counters.foreign == {sibling, forged}
    assert (counters['approved'], counters['rejected']) == (1, 1)
    assert counters.matching('rejected:') == {'path_not_allowed': 1}

    evaluation._review_pending(app, case, cid, rid, ws.root, counters)  # a second pass changes nothing
    assert (counters['approved'], counters['rejected']) == (1, 1)
    assert _status(app, cid, sibling) == 'pending' and _status(app, cid, forged) == 'pending'


# -- 9. delegation is never credited from errored or foreign child receipts ----------

def _child(app, project, cid, rid, name):
    tree = app.agents.tree
    node = tree.spawn(cid, rid, None, 'Read the synthetic note.', name, ['Read'],
                      {'max_steps': 4, 'max_seconds': 60, 'model_tokens': 32768})
    owner = 'worker-' + name
    tree.claim(cid, rid, node['id'], 1, owner)
    return {'cid': cid, 'rid': rid, 'id': node['id'], 'generation': 1, 'owner': owner,
            'cancel': threading.Event(), 'workspace': Workspace(project)}


def _child_read(app, worker, path, call_id):
    tree = app.agents.tree
    identity = (worker['cid'], worker['rid'], worker['id'], 1, worker['owner'], call_id)
    args = {'file_path': path}
    tree.begin_tool(*identity, 'Read', args)
    try:
        result = app.agents.child_tool(worker, 'Read', args, call_id)
    except (ValueError, OSError) as exc:
        result = {'error': str(exc)[:200], 'status': 'rejected'}
    return tree.end_tool(*identity, result)


def _review(app, cid, rid, worker, evidence):
    return app.agents.tool(cid, rid, rid, 'ReviewAgentResult', {'agent_id': worker['id'], 'generation': 1,
                                                                 'evidence_ids': json.dumps(evidence)}, 'review')


def test_delegation_is_not_credited_for_errored_or_foreign_receipts(host_app):
    app, project = host_app
    tree = app.agents.tree
    cid, rid = _start_run(app, project)
    claimant, holder = _child(app, project, cid, rid, 'claimant'), _child(app, project, cid, rid, 'holder')
    assert 'error' in _child_read(app, claimant, 'missing.txt', '1:miss')
    assert 'error' not in _child_read(app, holder, 'notes.txt', '1:read')  # successful, but another node's
    for evidence in (['1:miss'], ['1:read']):
        with pytest.raises(ValueError):
            tree.settle(cid, rid, claimant['id'], 1, 'completed', {'text': 'Done.'}, evidence, claimant['owner'])
    tree.settle(cid, rid, claimant['id'], 1, 'completed', {'text': 'Done.'}, [], claimant['owner'])
    for evidence in ([], ['1:read']):
        with pytest.raises(ValueError):
            _review(app, cid, rid, claimant, evidence)
    evidence, public, _ = evaluation._collect(app, cid, rid, evaluation._Counters(), evaluation._known_tools())
    assert public['delegation']['attributable'] == 0 and public['delegation']['successful_child_work'] == 0
    assert evaluation._outcome('settled', evidence, {'passed': True}, 'delegated', public['delegation']) == 'non_delegated'

    # Control in a fresh run: real, reviewed child evidence is credited.
    cid, rid = _start_run(app, project)
    reader = _child(app, project, cid, rid, 'reader')
    _child_read(app, reader, 'notes.txt', '1:read')
    _child_read(app, reader, 'missing.txt', '2:miss')
    tree.settle(cid, rid, reader['id'], 1, 'completed', {'text': 'Read it.'}, ['1:read'], reader['owner'])
    assert _review(app, cid, rid, reader, ['1:read'])['review'] == 'accepted'
    evidence, public, _ = evaluation._collect(app, cid, rid, evaluation._Counters(), evaluation._known_tools())
    assert public['delegation']['attributable'] == 1 and public['delegation']['successful_child_work'] == 1
    assert evaluation._outcome('settled', evidence, {'passed': True}, 'delegated', public['delegation']) != 'non_delegated'


# -- 10. signed reports: tampering, import and identity forgery are refused -----------

def test_signed_reports_refuse_tampering_import_and_identity_forgery(tmp_path):
    ledger, other = EvidenceLedger(tmp_path / 'ledger'), EvidenceLedger(tmp_path / 'other')
    body = {'schema': SCHEMA, 'evaluation_id': uuid.uuid4().hex, 'arm': 'solo',
            'cases': [{'case_id': 'code-median', 'outcome': 'failed', 'success': False}]}
    report = ledger.sign(body)
    assert ledger.verify(report) is True

    def refused(target, report_value, reason):
        with pytest.raises(EvidenceRefused) as caught:
            target.verify(report_value)
        assert reason in caught.value.reasons

    promoted = copy.deepcopy(report)
    promoted['cases'][0].update(outcome='passed', success=True)
    refused(ledger, promoted, 'tampered_evidence')
    refused(other, report, 'unknown_or_imported_evidence')
    refused(ledger, {**report, 'signature': {**report['signature'], 'key_id': uuid.uuid4().hex}}, 'key_identity_mismatch')
    refused(ledger, {k: v for k, v in report.items() if k != 'signature'}, 'unsigned_evidence')

    # A report identity cannot be re-signed with a different body, or name a path.
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob('*'))
    with pytest.raises(FileExistsError):
        ledger.sign({**body, 'cases': promoted['cases']})
    with pytest.raises(EvidenceRefused):
        ledger.sign({**body, 'evaluation_id': '../' + uuid.uuid4().hex})
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob('*')) == before
    assert ledger.verify(report) is True
    with pytest.raises(EvidenceRefused):
        evaluation.compare_reports(report, report, ledger)


# -- 11. Phase 15 cross-file code: the child gets sources and inputs, never expectations --

def test_cross_file_grading_child_receives_no_expected_values_and_fails_closed(tmp_path, monkeypatch):
    import re
    from pebble import eval_corpus_15 as corpus15
    task, oracle = corpus15.BY_ID['code-invoice'], corpus15.ORACLES['code-invoice']
    source = corpus15.REFERENCE_INVOICE.encode()
    before = {'invoice.py': 'a' * 64, 'rates.py': evaluation._sha(corpus15.RATES.encode()),
              'test_invoice.py': evaluation._sha(corpus15.INVOICE_VISIBLE_TEST.encode())}
    after = {**before, 'invoice.py': evaluation._sha(source)}
    evidence = evaluation.Evidence(before=before, after=after, files={'invoice.py': source},
                                   actions=[{'kind': 'edit', 'status': 'applied', 'path': 'invoice.py',
                                             'hash': after['invoice.py'], 'exit_code': None}])
    sent, replies = [], []

    def fake_run(command, **kwargs):
        request = json.loads(kwargs['input'])
        sent.append(request)
        honest = interpret_checks(request['source'], request['function'], request['inputs'])
        reply = replies.pop(0) if replies else json.dumps
        return SimpleNamespace(returncode=0, stdout=reply(honest).encode())
    monkeypatch.setattr(evaluation.subprocess, 'run', fake_run)

    control = corpus15.grade(task, evidence, tmp_path / 'control')
    assert control['passed'] and (control['hidden_passed'], control['variant_passed']) == (12, 4)
    assert len(sent) == 2
    payload = json.dumps(sent)
    for request in sent:
        assert set(request) == {'source', 'function', 'inputs'}
        assert all(set(item) == {'name', 'args', 'tuple'} for item in request['inputs'])
    for check in oracle['checks'] + oracle['variant']['checks']:
        if 'value' in check:
            assert not re.search(r'(?<![\d.])%d(?![\d.])' % check['value'], payload), check['name']
    # The hidden variant dependency reaches only the isolated child, as data.
    assert 'TAX_PERCENT = 20' in sent[1]['source'] and 'TAX_PERCENT = 20' not in sent[0]['source']

    def forged(rows):
        return json.dumps([{**row, 'status': 'passed'} for row in rows])
    for index, bad in enumerate((lambda rows: json.dumps(rows[:-1]), lambda rows: 'not json', forged,
                                 lambda rows: json.dumps([{**row, 'type': 'str'} for row in rows]))):
        replies[:] = [json.dumps, bad]  # honest visible run, forged variant run
        verdict = corpus15.grade(task, evidence, tmp_path / ('bad%d' % index))
        assert not verdict['passed'] and verdict['checks']['hidden_behaviour'] is True
        assert verdict['checks']['tracks_dependency_changes'] is False and verdict['variant_passed'] == 0


# -- 12. Phase 15 long-constraint grader: lookalikes, claims and partial documents fail ----

def test_release_notes_grader_refuses_lookalike_claimed_or_partial_documents(tmp_path):
    from pebble import eval_corpus_15 as corpus15
    task = corpus15.BY_ID['constraints-release']
    good = corpus15.reference_release_notes()

    def grade(text, linked=True):
        data = text.encode('utf-8')
        before = {'changes.csv': evaluation._sha(corpus15.CHANGES.encode()), 'docs/style_notes.md': 'b' * 64}
        after = {**before, 'RELEASE_NOTES.md': evaluation._sha(data)}
        action = {'kind': 'edit', 'status': 'applied', 'path': 'RELEASE_NOTES.md',
                  'hash': after['RELEASE_NOTES.md'] if linked else '0' * 64, 'exit_code': None}
        verdict = corpus15.grade(task, evaluation.Evidence(state='completed', before=before, after=after,
                                                           files={'RELEASE_NOTES.md': data}, actions=[action]),
                                 tmp_path)
        assert set(verdict['checks']) >= set(task.required_checks)  # retained: nothing omitted
        return verdict

    assert grade(good)['passed']
    for text, failures in (
            (good.replace('- Glob', '‐ Glob'), {'ascii_only', 'document_exact'}),           # Unicode hyphen
            (good + '\nFINAL: all ten rules satisfied\n', {'only_allowed_lines', 'footer_last'}),  # claims are not evidence
            (good.split('## Fixed')[0], {'sections_in_order', 'footer_last', 'document_exact'}),
            (good.replace('- None.', '- Unused telemetry upload flag'), {'no_internal_rows', 'empty_section_marker'}),
            (good.replace('[PB-202]', '[pb-202]'), {'bullet_format', 'document_exact'}),
            (good.replace('# Pebble 4.2.0 release notes', '#  Pebble 4.2.0 release notes'), {'title_first'})):
        verdict = grade(text)
        assert not verdict['passed'] and failures <= set(verdict['reasons']), (failures, verdict['reasons'])
    unlinked = grade(good, linked=False)
    assert not unlinked['passed'] and unlinked['reasons'] == ['broker_linked']
