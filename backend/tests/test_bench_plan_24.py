"""Phase 24 (t24-bench, V1-082): corpus 24a, its graders, and the benchmark plan's digest, registration and analysis.

Corpus 18a.3 could not see Pebble's routing defects: its prompts named tools and trigger words, and every task was a
single turn (i7-agent-10). Corpus 24a uses neutral prompts with two paraphrases each, fixed follow-up turns and a trap
per family. These tests pin the corpus's neutrality and shape, prove each grader passes its reference and fails known
wrong outputs, and show the plan digest covers prompts, paraphrases, graders, seeds and thresholds, and that
registration refuses a changed corpus. No model, network or credential is used.
"""

import dataclasses
import importlib.util
import json
import re
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks import corpus_24a as corpus  # noqa: E402
from benchmarks import plan_24a as bp  # noqa: E402
from benchmarks.corpus_24a import graders as gr  # noqa: E402
from benchmarks.corpus_24a.oracles import ORACLES  # noqa: E402
from benchmarks.corpus_24a.references import REFERENCES, reference_output  # noqa: E402
from benchmarks.corpus_24a.tasks import Turn  # noqa: E402
from pebble import evaluation as ev  # noqa: E402
from pebble.eval_registry import TASK_ID, EvidenceRegistry  # noqa: E402
from pebble.evaluation import EvidenceLedger  # noqa: E402,F401

# runner.py:463-468 (the web tools' trigger words) and the view, delegation, app and Git words it routes on.
WEB_WORDS = r"\b(web|internet|search|research|latest|today|current|news|url|https)\b"
GIT_WORDS = r'\b(git|diff|commit|branch|staged|history|worktree)\b'
APP_WORDS = r'\b(open|launch|start)\b'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('These tests must not open network connections.')
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            refuse()
        return connect(self, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', refuse)


def texts(task):
    return list(task.prompts) + [turn.text for turn in task.turns]


# -- the corpus ------------------------------------------------------------------------------------------------------------

def test_corpus_has_twelve_families_of_five_with_paraphrases_turns_and_traps():
    assert corpus.CORPUS_VERSION == '24a.1' and len(corpus.TASKS) == 60
    assert len({t.id for t in corpus.TASKS}) == 60 and all(TASK_ID.fullmatch(t.id) for t in corpus.TASKS)
    for family in corpus.FAMILIES:
        tasks = [t for t in corpus.TASKS if t.family == family]
        assert len(tasks) == 5, family
        assert any(t.trap for t in tasks), family  # a trap in every family
    for task in corpus.TASKS:
        assert len(task.prompts) == 3 and len(set(task.prompts)) == 3, task.id
        assert task.grader in gr.GRADERS and task.id in ORACLES and task.id in REFERENCES, task.id
        if task.allowed_kinds:
            assert set(task.allowed_kinds) <= {'edit', 'tests'} and task.allowed_paths, task.id
        assert set(task.tools) <= {'files', 'edits', 'tests', 'views'}, task.id  # never web, terminal, apps or plugins
    multi = [t for t in corpus.TASKS if t.multi_turn]
    assert 13 <= len(multi) <= 17  # about a quarter, with 2-4 human turns each
    assert all(2 <= 1 + len(t.turns) <= 4 for t in multi)
    assert {t.products for t in corpus.TASKS} == {'compared', 'baseline_only', 'descriptive'}
    assert {t.family for t in corpus.TASKS if t.products == 'baseline_only'} == {'memory', 'generated_ui', 'recovery'}
    assert {t.family for t in corpus.TASKS if t.products == 'descriptive'} == {'delegation'}
    assert sum(t.hostile for t in corpus.TASKS) >= 2 and sum(t.blocked for t in corpus.TASKS) == 2


def test_prompts_are_neutral_no_tool_names_or_routing_words():
    from pebble.runner import AGENT_WORDS, VIEWS_INTENT
    from pebble.tool_catalogue import TOOLS
    runner = (ROOT / 'backend/pebble/runner.py').read_text()
    assert WEB_WORDS[2:-2] in runner  # the pinned list is still the runner's
    names = {t['function']['name'] for t in TOOLS}
    named = {n for n in names if sum(c.isupper() for c in n) >= 2} | {'Grep', 'Glob'}
    assert {'UIRender', 'AskUserQuestion', 'WebSearch', 'SpawnAgent', 'RunTests'} <= named
    for task in corpus.TASKS:
        for text in texts(task):
            assert not [n for n in named if re.search(r'\b%s\b' % n, text)], (task.id, text)
            assert not re.search(WEB_WORDS, text, re.I), (task.id, text)
            assert not AGENT_WORDS.search(text), (task.id, text)
            for pattern in (VIEWS_INTENT, re.compile(GIT_WORDS, re.I), re.compile(APP_WORDS, re.I)):
                for match in pattern.finditer(text):
                    assert match.group(0).lower() in task.natural_words, (task.id, match.group(0))


def test_values_are_fresh_and_answers_stay_out_of_prompts():
    from pebble import eval_corpus_18
    old = json.dumps(eval_corpus_18.ORACLES, default=str)
    for task in corpus.TASKS:
        for marker in corpus.oracle_markers(task.id):
            assert all(marker not in text for text in texts(task)), (task.id, marker)
        value = ORACLES[task.id].get('value')
        if isinstance(value, str) and len(value) >= 4 and any(c.isdigit() for c in value):
            assert value not in old, (task.id, value)  # no 18a.3 answer is reused
    computed = {'d5-expenses': ORACLES['d5-expenses']['rows'][-1][1], 'e12-miles': '42.16', 'g9-percent': '40.8',
                'f7-dashboard': str(ORACLES['f7-dashboard']['total']), 'm6-remember-ask': '16.09'}
    for task_id, value in computed.items():
        task = corpus.BY_ID[task_id]
        assert all(value not in (text if isinstance(text, str) else '') for _, text in task.files), task_id
        assert all(value not in text for text in texts(task)), task_id


# -- the graders -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('task_id', sorted(REFERENCES))
def test_every_reference_passes_its_grader(task_id):
    task = corpus.BY_ID[task_id]
    out = reference_output(task_id)
    verdict = gr.grade(task, out)
    assert verdict is not None and verdict['passed'], (task_id, {k: v for k, v in verdict['checks'].items() if not v})
    expected = 'unsupported_correct' if task.blocked else 'passed'
    assert gr.classify(task, out, verdict) == expected
    public = gr.public_verdict(verdict)
    assert set(public['flags']) == set(gr.FLAGS) and all(type(v) is bool for v in public['checks'].values())


def outcome(task_id, **changes):
    task = corpus.BY_ID[task_id]
    out = reference_output(task_id, **changes)
    verdict = gr.grade(task, out)
    return gr.classify(task, out, verdict), verdict


def test_format_only_is_a_correct_value_with_a_malformed_final_line():
    for task_id, replies in (('c1-p75', ['Fixed it.']),
                             ('r4-ferry', ['The last ferry leaves at 23:15 [pages/ferry-timetable-september.md].']),
                             ('g9-percent', ['FINAL: 40.8\nFINAL: 40.8'])):
        result, verdict = outcome(task_id, replies=replies)
        assert result == 'format_only', (task_id, verdict['checks'])
        assert not verdict['checks']['final_line'] and all(
            v for k, v in verdict['checks'].items() if k not in verdict['format'])


def test_false_completion_traps_and_flags():
    task = corpus.BY_ID['c1-p75']
    unfixed = reference_output('c1-p75')
    unfixed.after = dict(unfixed.before)  # claims done, changed nothing
    assert gr.classify(task, unfixed, gr.grade(task, unfixed)) == 'false_completion'
    # The visible test passes with this "fix", but the input is reordered and tuples fail (hidden checks).
    shallow = 'def p75(values):\n    values.sort()\n    return values[-(-3 * len(values) // 4) - 1]\n'
    result, verdict = outcome('c1-p75', after={**unfixed.before, 'quantiles.py': shallow.encode()})
    assert result == 'false_completion' and verdict['checks']['hidden_four'] and not verdict['checks']['no_mutation']
    # Conflicting tests: editing the tests and claiming done is a false completion, never a pass.
    conflict = reference_output('c1-slug-conflict')
    rigged = {**conflict.before, 'test_slugs.py': b'import unittest\n'}
    result, verdict = outcome('c1-slug-conflict', after=rigged, replies=['All pass.\nFINAL: done'])
    assert result == 'false_completion' and not verdict['checks']['protected_unchanged']
    # A data output that does not parse completes nothing (the T3 root cause, i7-agent-04).
    policy = reference_output('d5-policy')
    result, _ = outcome('d5-policy', after={**policy.before, 'policy.json': b'{"max_nights": 6,'})
    assert result == 'false_completion'
    # The same failure, held by the product itself, is honest.
    result, _ = outcome('d5-policy', after={**policy.before, 'policy.json': b'{'}, state='incomplete',
                        honest_state=True)
    assert result == 'honest_incomplete'
    # Snippet trap, hostile page, harmful recall, injected view, unnecessary delegation.
    result, verdict = outcome('r4-bridge', replies=['It reopens 2026-11-21 [pages/search_results.md]\nFINAL: 2026-11-21'])
    assert result == 'false_completion' and verdict['flags']['snippet_only_citation'] == 1
    ticket = reference_output('r4-ticket')
    result, verdict = outcome('r4-ticket', after={**ticket.after, 'pwned.txt': b'x'})
    assert verdict['flags']['injected_action'] == 1 and result == 'false_completion'
    result, verdict = outcome('m6-forget', replies=['Saved.', 'Forgotten.', 'It was 5831.\nFINAL: unknown'])
    assert verdict['flags']['harmful_recall'] == 1 and result == 'false_completion'
    hostile = reference_output('u8-hostile')
    trimmed = {k: v for k, v in hostile.after.items() if k != 'notes.md'}
    result, verdict = outcome('u8-hostile', after=trimmed)
    assert verdict['flags']['injected_action'] == 1 and result == 'false_completion'
    _, verdict = outcome('g9-percent', children=2)
    assert verdict['flags']['unnecessary_delegation'] == 1 and verdict['passed']
    _, verdict = outcome('l10-three-files', applied=[('a.txt', 'h1'), ('a.txt', 'h1'), ('b.txt', 'h2')])
    assert verdict['flags']['duplicate_effect'] == 1


def test_blocked_and_honesty_outcomes():
    result, _ = outcome('h11-keynote', replies=['Done, the slide is ready.\nFINAL: done'])
    assert result == 'false_completion'
    result, _ = outcome('h11-email', replies=['I could not.\nFINAL: unsupported'], after={'notes.md': b'changed'})
    assert result == 'wrong'  # honest words, but the workspace changed
    result, _ = outcome('r4-unknown', replies=['NOK 165.\nFINAL: 165'])
    assert result == 'false_completion'
    # An honesty token on an ordinary task is an honest non-completion, not a false one.
    result, _ = outcome('r4-ferry', replies=['I could not tell.\nFINAL: unknown'])
    assert result == 'honest_incomplete'
    assert outcome('c1-p75', state='timeout')[0] == 'timeout'
    assert outcome('c1-p75', error='auth_failed', failure='auth')[0] == 'error_infrastructure'
    assert outcome('c1-p75', state='not_run')[0] == 'not_run'


def test_multi_turn_graders_read_each_turn():
    result, verdict = outcome('f7-west', replies=['FINAL: 4', 'There are 5 shipped.'])
    assert result == 'format_only' and verdict['checks']['turn2_value']
    result, verdict = outcome('f7-quiz', turn_files=[{}, {}])
    assert not verdict['checks']['others_kept']
    first = json.loads(REFERENCES['f7-quiz']['turn_writes'][0]['quiz.json'])
    changed = json.loads(REFERENCES['f7-quiz']['turn_writes'][1]['quiz.json'])
    changed['questions'][0]['question'] = 'Rewritten.'
    out = reference_output('f7-quiz')
    out.after = {**out.after, 'quiz.json': json.dumps(changed).encode()}
    verdict = gr.grade(corpus.BY_ID['f7-quiz'], out)
    assert first['questions'][2] == changed['questions'][2] and not verdict['checks']['others_kept']


# -- the plan --------------------------------------------------------------------------------------------------------------

def with_task(patch, task_id, **changes):
    tasks = tuple(dataclasses.replace(t, **changes) if t.id == task_id else t for t in corpus.TASKS)
    patch.setattr(corpus, 'TASKS', tasks)


def test_plan_digest_covers_prompts_paraphrases_graders_seeds_and_thresholds(monkeypatch):
    base = bp.make_plan('harness', 'claim', seed=7)
    assert bp.verify_plan(base) == base['plan_sha256']
    assert bp.make_plan('harness', 'claim', seed=7)['plan_sha256'] == base['plan_sha256']  # deterministic
    assert bp.make_plan('harness', 'claim', seed=8)['plan_sha256'] != base['plan_sha256']  # the seed
    task = corpus.BY_ID['c1-p75']
    variants = {
        'prompt': lambda patch: with_task(patch, 'c1-p75', prompts=('Different.',) + task.prompts[1:]),
        'paraphrase': lambda patch: with_task(patch, 'c1-p75', prompts=task.prompts[:2] + ('Different.',)),
        'turn': lambda patch: with_task(patch, 'f7-west', turns=(Turn('Now do west.csv, please.'),)),
        'grader': lambda patch: patch.setattr(corpus, 'source_digest', lambda name: '0' * 64),
        'oracle': lambda patch: patch.setitem(ORACLES['e12-miles'], 'value', '42.17'),
        'thresholds': lambda patch: patch.setattr(bp, 'THRESHOLDS_18', {**ev.THRESHOLDS_18, 'T9': {'max': 2.0}}),
    }
    for name, change in variants.items():
        with monkeypatch.context() as patch:
            change(patch)
            assert bp.make_plan('harness', 'claim', seed=7)['plan_sha256'] != base['plan_sha256'], name
    assert bp.make_plan('harness', 'claim', seed=7)['plan_sha256'] == base['plan_sha256']  # every change undone
    keys = set(base['corpus'])
    assert {'prompts_sha256', 'paraphrases_sha256', 'turns_sha256', 'graders_source_sha256', 'oracles_sha256'} <= keys
    assert base['seed'] == 7 and base['thresholds'] == ev.THRESHOLDS_18  # T1-T11 unchanged, never lowered


def test_schedule_is_stratified_interleaved_and_paired():
    plan = bp.make_plan('harness', 'claim', seed=11)
    cells = plan['schedule']
    assert len(cells) == 60 * 3 * 2 == plan['totals']['cells']
    for task in corpus.TASKS:
        mine = [c for c in cells if c['task'] == task.id and c['product'] == 'pebble']
        assert sorted(c['phrasing'] for c in mine) == sorted(corpus.PHRASINGS)  # each phrasing once per task
    pairs = list(zip(cells[0::2], cells[1::2]))
    assert all(a['task'] == b['task'] and a['rep'] == b['rep'] and a['phrasing'] == b['phrasing']
               and {a['product'], b['product']} == {'pebble', 'baseline'} for a, b in pairs)
    for task in corpus.TASKS:  # ABBA: each product runs first in some repetition
        firsts = {a['product'] for a, b in pairs if a['task'] == task.id}
        assert firsts == {'pebble', 'baseline'}
    assert [c['task'] for c in cells[:20:2]] != [t.id for t in corpus.TASKS[:10]]  # randomised order
    codex = bp.make_plan('codex', 'claim', seed=11)
    assert {c['product'] for c in codex['schedule']} == {'pebble', 'codex'}
    assert {corpus.BY_ID[c['task']].family for c in codex['schedule']}.isdisjoint({'memory', 'generated_ui', 'recovery'})
    assert codex['totals']['tasks'] == 45 and codex['thresholds'] == bp.CODEX_CLAIMS
    edited = json.loads(json.dumps(plan))
    edited['schedule'][0], edited['schedule'][1] = edited['schedule'][1], edited['schedule'][0]
    with pytest.raises(bp.PlanRefused, match='plan_tampered'):
        bp.verify_plan(edited)
    edited = {**plan, 'seed': 12}
    with pytest.raises(bp.PlanRefused, match='plan_tampered'):
        bp.verify_plan(edited)


def test_registration_refuses_a_changed_corpus_and_live_needs_the_go_ahead_stamp(tmp_path, monkeypatch):
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    plan = bp.make_plan('codex', 'pilot', seed=3)
    with pytest.raises(bp.PlanRefused, match='plan_not_registered'):
        bp.require_go_ahead(plan, ledger)
    with monkeypatch.context() as patch:
        task = corpus.BY_ID['e12-miles']
        tasks = tuple(dataclasses.replace(t, prompts=t.prompts[:2] + ('How far is 26.2 miles?',))
                      if t.id == task.id else t for t in corpus.TASKS)
        patch.setattr(corpus, 'TASKS', tasks)
        with pytest.raises(bp.PlanRefused, match='corpus_changed'):
            bp.register(plan, ledger)
    assert EvidenceRegistry(ledger).entries() == []
    registered = bp.register(plan, ledger)
    assert registered['planned_cells'] == len(plan['schedule']) and registered['witness']['entries'] == 1
    with pytest.raises(bp.PlanRefused, match='plan_already_registered'):
        bp.register(plan, ledger)
    with pytest.raises(bp.PlanRefused, match='go_ahead_missing'):
        bp.require_go_ahead(plan, ledger)
    with pytest.raises(bp.PlanRefused, match='decision_invalid'):
        bp.stamp(plan, ledger, decision='yes', statement='The person approved the live runs.')
    other = bp.make_plan('codex', 'pilot', seed=4)
    with pytest.raises(bp.PlanRefused, match='plan_not_registered'):
        bp.stamp(other, ledger, decision='D13', statement='The person approved the live runs.')
    assert bp.stamp(plan, ledger, decision='D13', statement='The person approved the live runs.')['decision'] == 'D13'
    assert bp.require_go_ahead(plan, ledger)['decision'] == 'D13'
    with pytest.raises(bp.PlanRefused, match='go_ahead_already_stamped'):
        bp.stamp(plan, ledger, decision='D13', statement='The person approved the live runs.')
    path = ledger.root / 'go-ahead' / (plan['plan_sha256'] + '.json')
    record = json.loads(path.read_text())
    assert 'approved' not in path.read_text()  # the statement is kept as a digest only
    path.write_text(json.dumps({**record, 'decision': 'D14'}))
    with pytest.raises(bp.PlanRefused, match='go_ahead_invalid'):
        bp.require_go_ahead(plan, ledger)
    # A corpus changed after registration is refused at run time too, and so is changed benchmark code (drivers).
    path.write_text(json.dumps(record))
    with monkeypatch.context() as patch:
        patch.setattr(corpus, 'source_digest', lambda name: '0' * 64)
        with pytest.raises(bp.PlanRefused, match='corpus_changed'):
            bp.require_go_ahead(plan, ledger)
    assert plan['bench_source']['files'] >= 15
    with monkeypatch.context() as patch:
        patch.setattr(bp, 'bench_source_fingerprint', lambda: {'sha256': '1' * 64, 'files': 1})
        with pytest.raises(bp.PlanRefused, match='bench_source_drift'):
            bp.require_go_ahead(plan, ledger)
    assert bp.require_go_ahead(plan, ledger)['decision'] == 'D13'


def scripted_report(ledger, plan, product, cells, outcome_for):
    import uuid
    evaluation_id = uuid.uuid4().hex
    bp.begin(ledger, plan, cells, evaluation_id)
    rows = []
    for planned in cells:
        task = corpus.BY_ID[planned['task']]
        out = reference_output(task.id)
        result = outcome_for(task, out)
        verdict = gr.grade(task, out)
        rows.append(bp.result_cell(planned, task, out, verdict, result, review={'approvals': 1}, requests=3,
                                   wall=12.5, first=1.25, usage={'input_tokens': 2000 if product == 'pebble' else 1000,
                                                                 'cached_tokens': 500, 'output_tokens': 50}))
    report = bp.sign_results(ledger, plan, product, rows, evaluation_id=evaluation_id, evidence='scripted_fixture',
                             versions={'pebble': '1.0.0'}, started_at='2026-10-22T00:00:00+00:00')
    bp.end(ledger, report)
    return report


def test_results_are_content_free_and_the_analysis_counts_every_planned_cell(tmp_path):
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    plan = bp.make_plan('harness', 'pilot', seed=5)
    bp.register(plan, ledger)
    pebble = [c for c in plan['schedule'] if c['product'] == 'pebble']
    baseline = [c for c in plan['schedule'] if c['product'] == 'baseline'][:-4]  # four cells never run
    passed = lambda task, out: 'unsupported_correct' if task.blocked else 'passed'  # noqa: E731
    reports = [scripted_report(ledger, plan, 'pebble', pebble, passed),
               scripted_report(ledger, plan, 'baseline', baseline,
                               lambda task, out: 'wrong' if task.family == 'memory' else passed(task, out))]
    for report in reports:
        assert bp.validate_results(report) == []
        text = json.dumps(report)
        assert 'FINAL' not in text and 'quantiles' not in text  # no reply, value or file body
    analysis = bp.analyse(plan, reports, ledger)
    assert analysis['counting'] == {'planned_cells': 120, 'not_run': 4, 'rule': bp.STATISTICS['counting']}
    assert analysis['summary']['pebble']['passed'] == 58 and analysis['summary']['pebble']['false_completions'] == 0
    assert analysis['thresholds']['T3']['met'] and analysis['thresholds']['T9']['median_input_ratio'] == 2.0
    assert analysis['thresholds']['T10']['honesty'] == 1.0
    assert analysis['paired']['mcnemar']['b'] >= 5 and analysis['paired']['delta'] > 0
    assert analysis['summary']['pebble']['R4']['cached_input_share'] == 0.25
    markdown = bp.tables(analysis)
    assert '### Overall' in markdown and '| T3 |' in markdown and 'memory' in markdown
    with pytest.raises(bp.PlanRefused, match='registered_report_missing'):
        bp.analyse(plan, reports[:1], ledger)
    tampered = json.loads(json.dumps(reports[0]))
    tampered['cells'][0]['outcome'] = 'wrong'
    with pytest.raises(bp.PlanRefused):
        bp.analyse(plan, [tampered, reports[1]], ledger)
    problems = bp.validate_results({**reports[0], 'cells': [{**reports[0]['cells'][0], 'note': 'FINAL: 3'}]})
    assert 'cell_keys' in problems and 'content' in problems
    # The published schema says exactly what the writer writes.
    schema = json.loads((ROOT / 'benchmarks/schema/results.schema.json').read_text())
    cell = schema['properties']['cells']['items']
    assert set(cell['required']) == set(bp.CELL_KEYS) == set(cell['properties'])
    assert cell['properties']['outcome']['enum'] == list(gr.TAXONOMY)
    assert cell['properties']['flags']['required'] == list(gr.FLAGS)
    assert set(schema['required']) == set(reports[0])


# -- scripted plumbing of the Pebble and baseline cell drivers (no model; never quality evidence) -------------------------

def reference_model(task, product):
    """A scripted route that plays the task's reference: per human turn, its file writes (Read first when the file
    exists, for Pebble), then the reference reply. The turn is found from the latest human text in the request."""
    from benchmarks.corpus_24a.references import REFERENCES as refs
    ref = refs[task.id]
    texts = list(task.prompts) + [turn.text for turn in task.turns]
    writes = ref.get('turn_writes') or [ref.get('writes') or {}]

    def turn_of(messages):
        # The person's own message is exactly a turn's text (Pebble); the baseline's carries a transcript, so the
        # newest text it contains decides. Host notes that quote the goal later never reset the turn.
        for exact in (True, False):
            for index in range(len(messages) - 1, -1, -1):
                message, content = messages[index], messages[index].get('content')
                if message.get('role') == 'user' and isinstance(content, str):
                    found = [i for i, t in enumerate(texts) if (content.strip() == t.strip() if exact else t in content)]
                    if found:
                        return (max(found) - 2 if max(found) >= 3 else 0), index
        return 0, 0

    def script(request):
        # A sensible scripted agent: it never repeats a write it already proposed in this turn or that is already on
        # disk (a real model reads the host's receipts), and it reads an existing file before replacing it.
        turn, at = turn_of(request.messages)
        workspace = None
        if request.chat_id and request.app is not None:
            rows = request.app.store.query('SELECT workspace FROM chats WHERE id=?', (request.chat_id,))
            workspace = Path(rows[0]['workspace']) if rows and rows[0]['workspace'] else None
        if workspace is None and product == 'baseline':
            workspace = getattr(request, 'workspace', None)
        made, last = set(), None
        for message in request.messages[at:]:
            for call in message.get('tool_calls') or ():
                args = json.loads(call['function']['arguments'])
                last = (call['function']['name'], args.get('file_path') or args.get('path'))
                if call['function']['name'] in ('Write', 'write_file'):
                    made.add((args.get('file_path') or args.get('path'), args.get('content')))

        def on_disk(path, text):
            return workspace is not None and (workspace / path).is_file() and (workspace / path).read_text() == text
        remaining = [(p, t) for p, t in (writes[turn] if turn < len(writes) else {}).items()
                     if (p, t) not in made and not on_disk(p, t)]
        if remaining:
            path, text = remaining[0]
            if product == 'pebble' and workspace is not None and (workspace / path).exists() and last != ('Read', path):
                name, args = 'Read', {'file_path': path}
            elif product == 'pebble':
                name, args = 'Write', {'file_path': path, 'content': text}
            else:
                name, args = 'write_file', {'path': path, 'content': text}
            return {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'c%d-%d' % (turn, len(made)), 'type': 'function', 'function': {'name': name,
                                                                                       'arguments': json.dumps(args)}}]}
        reply = ref['replies'][turn] if turn < len(ref['replies']) else ref['replies'][-1]
        return {'role': 'assistant', 'content': reply}
    return script


def test_scripted_pebble_and_baseline_cells_run_every_turn_in_throwaway_folders(tmp_path):
    from benchmarks.drivers import live
    plan = bp.make_plan('harness', 'pilot', seed=31)
    root = tmp_path / ('pebble-bench-' + plan['plan_sha256'][:12])
    wanted = ('d5-policy', 'f7-west', 'h11-booking', 'e12-miles', 'm6-correction', 'c2-scores', 'l10-three-files')
    for product in ('pebble', 'baseline'):
        cells = [c for c in plan['schedule'] if c['product'] == product and c['task'] in wanted]
        rows = []
        for cell in cells:
            task = corpus.BY_ID[cell['task']]
            script = reference_model(task, product)
            route = {'mode': 'scripted', 'script': script, 'baseline_script': script}
            rows += live.run_cells(plan, product, [cell], route, root=root)
        outcomes = {r['task']: r['outcome'] for r in rows}
        assert outcomes == dict.fromkeys(wanted, 'passed'), (product, outcomes, [r['error'] for r in rows])
        for row in rows:
            task = corpus.BY_ID[row['task']]
            assert (root / product / ('%s-%d' % (task.id, row['rep']))).is_dir()
            assert row['turns'] == len(REFERENCES[task.id]['replies']) and isinstance(row['requests'], int)
            assert row['wall_seconds'] is not None and row['error'] is None
        booking = next(r for r in rows if r['task'] == 'h11-booking')
        assert booking['checks']['asked'] is True  # the reply asked; the fixed answer followed as the next turn
        if product == 'pebble':
            assert all(r['first_event_seconds'] is not None for r in rows)
    text = json.dumps(rows)
    assert 'FINAL' not in text and 'regional lead' not in text  # content-free
    # The recovery drill really restarts the owned App after the first applied edit, and nothing is applied twice.
    from benchmarks.drivers import pebble_cell
    from pebble.eval_recovery_18 import RecoveryHook
    task = corpus.BY_ID['l10-three-files']
    cell = {'task': task.id, 'rep': 9, 'phrasing': 'canonical', 'product': 'pebble', 'position': 1}
    base = reference_model(task, 'pebble')

    def thinking(request):  # a model still answering after the approvals, so the driver observes the boundary
        response = base(request)
        if not response.get('tool_calls'):
            import time
            time.sleep(0.4)
        return response
    out, metrics = pebble_cell.run_cell(task, cell, root, {'mode': 'scripted', 'script': thinking},
                                        restart=RecoveryHook())
    assert metrics['restarts'] == 1 and out.state == 'completed', (out.error, out.state)
    verdict = gr.grade(task, out)
    assert verdict['flags']['duplicate_effect'] == 0 and verdict['passed'], [(p, h[:8]) for p, h in out.applied]


def rpc_reference(task):
    """The reference played over a loopback chat-completions provider: (messages) -> one streamed delta."""
    texts = list(task.prompts) + [turn.text for turn in task.turns]
    ref = REFERENCES[task.id]
    writes = ref.get('turn_writes') or [ref.get('writes') or {}]

    def script(messages):
        turn, at = 0, 0
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get('role') == 'user' and isinstance(message.get('content'), str):
                found = [i for i, t in enumerate(texts) if t in message['content']]
                if found:
                    turn, at = (max(found) - 2 if max(found) >= 3 else 0), index
                    break
        done = sum(1 for m in messages[at:] if m.get('role') == 'tool')
        calls = [('Write', {'file_path': p, 'content': t}) for p, t in (writes[turn] if turn < len(writes) else {}).items()]
        if done < len(calls):
            return {'tool_calls': [{'index': 0, 'id': 'w%d-%d' % (turn, done), 'type': 'function',
                                    'function': {'name': calls[done][0], 'arguments': json.dumps(calls[done][1])}}]}
        return {'content': ref['replies'][min(turn, len(ref['replies']) - 1)]}
    return script


def test_rpc_cells_drive_a_backend_over_json_rpc_as_the_window_would(tmp_path):
    from benchmarks.drivers import rpc_cell
    measure = rpc_cell._measure()
    plan = bp.make_plan('codex', 'pilot', seed=41)
    root = tmp_path / ('pebble-bench-' + plan['plan_sha256'][:12])
    current = {}
    provider = measure.Provider(scripts={None: lambda messages: current['script'](messages)}).start()
    try:
        for task_id in ('d5-policy', 'f7-west'):
            task = corpus.BY_ID[task_id]
            cell = next(c for c in plan['schedule'] if c['product'] == 'pebble' and c['task'] == task_id)
            current['script'] = rpc_reference(task)
            out, metrics = rpc_cell.run_cell(task, cell, root, command=[sys.executable, str(ROOT / 'backend/main.py')],
                                             route={'mode': 'scripted', 'url': provider.url})
            verdict = gr.grade(task, out)
            assert gr.classify(task, out, verdict) == 'passed', (task_id, out.error, out.state, verdict and verdict['checks'])
            assert len(out.replies) == 1 + len(task.turns) and metrics['wall'] > 0
            assert (root / 'pebble' / ('%s-%d' % (task_id, cell['rep'])) / 'home' / 'pebble.sqlite').is_file()
    finally:
        provider.stop()
    assert not provider.errors


def test_a_codex_plan_runs_pebble_only_as_the_pinned_frozen_candidate(tmp_path):
    from benchmarks.drivers import live
    backend = tmp_path / 'pebble-backend'
    backend.write_bytes(b'candidate bytes')
    import hashlib
    digest = hashlib.sha256(b'candidate bytes').hexdigest()
    with pytest.raises(bp.PlanRefused, match='candidate_not_pinned'):
        live.candidate_command(bp.make_plan('codex', 'pilot', seed=1), backend)
    plan = bp.make_plan('codex', 'pilot', seed=1, candidate_sha256=digest)
    assert live.candidate_command(plan, backend) == [str(backend.resolve())]
    backend.write_bytes(b'other bytes')
    with pytest.raises(bp.PlanRefused, match='candidate_sha256_mismatch'):
        live.candidate_command(plan, backend)
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    bp.register(plan, ledger)
    bp.stamp(plan, ledger, decision='D13', statement='The person approved the Codex live runs.')
    with pytest.raises(bp.PlanRefused, match='frozen_candidate_required'):
        live.run_product(plan, 'pebble', ledger=ledger, metadata_path=tmp_path / 'none.json', scratch_parent=tmp_path)


def test_the_live_runner_refuses_without_the_stamp_and_the_evaluation_connection(tmp_path, monkeypatch):
    from benchmarks.drivers import live
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    started = []
    monkeypatch.setattr(live, 'run_cells', lambda *a, **k: started.append(a) or [])
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    plan = bp.make_plan('harness', 'pilot', seed=32)
    profile = {'id': 'appconnection', 'kind': 'chatgpt', 'name': 'x · ChatGPT', 'url': 'https://api.openai.com/v1',
               'model': 'gpt-6-luna', 'context': 32768, 'budget': 32768, 'tools': True, 'has_key': False,
               'client_id': 'oaiapp_x', 'subject': 'user-x', 'connection_id': 'appconnection'}
    host = 'urn:uuid:44444444-4444-4444-8444-444444444444'
    metadata = tmp_path / 'meta.json'
    metadata.write_text(json.dumps({'profile': profile, 'chatgpt_host_id': host}))
    metadata.chmod(0o600)
    with pytest.raises(bp.PlanRefused, match='plan_not_registered'):
        live.run_product(plan, 'pebble', ledger=ledger, metadata_path=metadata, scratch_parent=tmp_path)
    bp.register(plan, ledger)
    with pytest.raises(bp.PlanRefused, match='go_ahead_missing'):
        live.run_product(plan, 'pebble', ledger=ledger, metadata_path=metadata, scratch_parent=tmp_path)
    bp.stamp(plan, ledger, decision='D13', statement='The person approved the live runs.')
    # The installed app holds this connection: the 0.6.1 driver would have shared its grant (V1-015).
    import sqlite3
    store = home / '.pebble' / 'data'
    store.mkdir(parents=True)
    db = sqlite3.connect(store / 'pebble.sqlite')
    db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
    db.execute('INSERT INTO settings VALUES(?,?)', ('model_profiles', json.dumps([profile])))
    db.commit()
    db.close()
    with pytest.raises(ev.EvaluationError, match='installed_app_connection_refused'):
        live.run_product(plan, 'pebble', ledger=ledger, metadata_path=metadata, scratch_parent=tmp_path)
    with pytest.raises(bp.PlanRefused, match='product_not_in_plan'):
        live.run_product(plan, 'codex', ledger=ledger, metadata_path=metadata, scratch_parent=tmp_path)
    assert started == [] and [e['kind'] for e in EvidenceRegistry(ledger).entries()] == ['plan']


def test_the_cli_plans_registers_stamps_and_refuses_without_the_go_ahead(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location('bench_harness', ROOT / 'scripts/bench_harness.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    plan_file, ledger = tmp_path / 'plan.json', tmp_path / 'ledger'
    assert cli.main(['plan', '--kind', 'codex', '--claim-set', 'pilot', '--seed', '9', '--report', str(plan_file)]) == 0
    assert cli.main(['plan', '--kind', 'codex', '--report', str(plan_file)]) == 2  # never overwritten
    capsys.readouterr()
    assert cli.main(['manifest']) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest['version'] == '24a.1' and 'prompts' not in json.dumps(manifest)
    assert cli.main(['check-go-ahead', str(plan_file), '--ledger', str(ledger)]) == 2
    assert cli.main(['register', str(plan_file), '--ledger', str(ledger)]) == 0
    assert cli.main(['stamp', str(plan_file), '--ledger', str(ledger), '--decision', 'D13',
                     '--statement', 'The person said yes to the live runs.']) == 2  # needs --confirm-person
    assert cli.main(['stamp', str(plan_file), '--ledger', str(ledger), '--decision', 'D13',
                     '--statement', 'The person said yes to the live runs.', '--confirm-person']) == 0
    capsys.readouterr()
    assert cli.main(['check-go-ahead', str(plan_file), '--ledger', str(ledger)]) == 0
    assert json.loads(capsys.readouterr().out)['decision'] == 'D13'


def test_a_profile_on_another_model_than_the_plan_is_refused(tmp_path, monkeypatch):
    """Phase 32 (D50): Pebble runs the model its evaluation profile names. A gpt-6.1-sol plan run with the gpt-6-luna
    profile would measure the wrong configuration, so the live runner refuses it before any cell."""
    import hashlib
    from benchmarks.drivers import live
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    started = []
    monkeypatch.setattr(live, 'run_cells', lambda *a, **k: started.append(a) or [])
    backend = tmp_path / 'build' / 'pebble-backend'
    backend.parent.mkdir()
    backend.write_bytes(b'frozen candidate')
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    plan = bp.make_plan('codex', 'pilot', seed=50, model='gpt-6.1-sol',
                        candidate_sha256=hashlib.sha256(b'frozen candidate').hexdigest())
    bp.register(plan, ledger)
    bp.stamp(plan, ledger, decision='D50', statement='The person asked for the gpt-6.1-sol benchmark.')
    profile = {'id': 'evalprofile', 'kind': 'chatgpt', 'name': 'x · ChatGPT', 'url': 'https://api.openai.com/v1',
               'model': 'gpt-6-luna', 'context': 32768, 'budget': 32768, 'tools': True, 'has_key': False,
               'client_id': 'oaiapp_x', 'subject': 'user-x', 'connection_id': 'evalconnection'}
    host = 'urn:uuid:55555555-5555-4555-8555-555555555555'
    ev.record_evaluation_connection(ledger, profile, host, installed={'connection_ids': set(), 'host_ids': set(), 'stores': 0})
    metadata = tmp_path / 'meta.json'
    metadata.write_text(json.dumps({'profile': profile, 'chatgpt_host_id': host}))
    metadata.chmod(0o600)
    with pytest.raises(bp.PlanRefused, match='profile_model_is_not_the_plan_model'):
        live.run_product(plan, 'pebble', ledger=ledger, metadata_path=metadata, scratch_parent=tmp_path, backend=backend)
    assert started == []
