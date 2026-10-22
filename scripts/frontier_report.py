"""Frontier protocol, second stage (Phase 18, E04b): blind grading and the separate frontier report.

  grade --export DIR --blinded DIR --store DIR --out GRADES
        grade every blinded output with the registered corpus's own graders, then record the grades in the store log
  report --export DIR --store DIR --campaign ID --blinded DIR --grades FILE --key FILE --out REPORT
        unblind only after complete, recorded grading; record the unblinding; write pebble.frontier_report/1

Grading uses the same deterministic graders as the same-model campaign (eval_corpus_18.grade, which
sends research cards to eval_research_18.grade_research) on what the product exported and nothing
else. The evidence for one output:

- answer: answer.md as written, with the card's own [S<n>] numbers rewritten to their page URLs;
- arm: 'baseline', since a product is a non-Pebble harness with no Pebble ledger, broker or journal;
- workspace: the card's files, with the product's outputs laid over them;
- research: every attached page counts as served, and every pack URL as shown in search results;
- claims: the run claims completion only if the answer ends on a FINAL: line, as the baseline arm does.

The product's own claims are never evidence. Checks that need a host journal cannot be observed in an
exported output: they are reported as not_observable, never counted as passed, and left out of the
verdict. question_asked is taken from the operator's session record, at unblinding.

Grading reads the export, the blinded folder and the store's hash-chained log. It never reads the
unblinding key, the session records or the store's record folders. Every blinded byte is checked
against blinded.json, whose digest the log recorded at blinding. A campaign is graded once.

Unblinding (report) refuses until the log holds that grading, the grades file still has its logged
digest and covers every blinded output, the blinded folder and the key are unchanged, and the store
holds no session added after blinding. The unblinding is logged, with the report's digest, before the
report is written. Every planned (card, product) cell is counted. A missing session is not_run.
Refused, unsupported and over-budget sessions keep their outcomes; repeats are listed and never
replace the first attempt. The report is never merged with same-model results.

Exit codes: 0 completed, 2 refused or invalid input.
"""
import argparse
import importlib.util
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path


def _load_cards():
    """frontier_cards, loaded by path (never by putting scripts/ first on sys.path); it puts backend/ on the path."""
    if 'frontier_cards' not in sys.modules:
        spec = importlib.util.spec_from_file_location('frontier_cards', Path(__file__).resolve().with_name('frontier_cards.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules['frontier_cards'] = module
        spec.loader.exec_module(module)
    return sys.modules['frontier_cards']


fc = _load_cards()

from pebble.eval_research_18 import CITE_GROUP, final_lines  # noqa: E402
from pebble.eval_stats import wilson  # noqa: E402
from pebble.evaluation import Evidence  # noqa: E402

GRADES_SCHEMA = 'pebble.frontier_grades/1'
REPORT_SCHEMA = 'pebble.frontier_report/1'
ARM = 'baseline'
STATEMENT = ('Different models and products; differences are not attributable to the harness. Descriptive only: '
             '12 cards cannot rank products. Not merged with same-model results.')
NEVER_MERGED = ('NEVER MERGED: this frontier report is separate from the same-model pebble/baseline campaign. '
                'analyse_campaign refuses it, and none of its numbers may appear in a same-model table or claim.')
# Checks that need Pebble's or the baseline's host journal; an exported output cannot show them.
NOT_OBSERVABLE = frozenset({
    'effect_linked', 'outputs_linked', 'test_file_linked', 'tests_passed_after_edit', 'no_effect_proposals',
    'no_effect_tools', 'no_unapproved_effect', 'typed_answer_delivered', 'resumed_after_answer',
    'only_driver_approvals', 'no_agent_origin_input', 'no_injected_effects'})
# Checks taken from the operator's session record, applied only at unblinding.
OPERATOR_RECORDED = {'question_asked': lambda session: session['questions_asked'] > 0}
OUTCOMES = ('passed', 'wrong', 'false_completion', 'honest_incomplete', 'refused_supported', 'refused',
            'attachment_failure', 'timeout', 'error', 'protocol_violation', 'not_run')
BLINDED_LIMITS = {'max_files': 5000, 'max_bytes': fc.SESSION_LIMITS['max_bytes'], 'max_total': 400_000_000}
CODE = re.compile(r'[a-z0-9_:.-]{1,80}')
TOKEN = re.compile(r'[0-9a-f]{16}')
LIMITATIONS = (
    'Different models and products: no difference here is attributable to the harness.',
    'One session per (card, product) and 12 cards: intervals are wide and the results are descriptive.',
    'Verdicts are output-only: checks that need a host journal are not_observable, so these verdicts are not '
    'comparable with same-model verdicts.',
    'Session facts (versions, tier, times, turns, questions) are recorded by the operator, not verified.',
    'Blinding is best effort: a product can name itself in its answer.',
    'Code shown only inline in an answer is not an output file; deletions in an output folder are not observed.',
)


def _refuse(code):
    raise fc.CardError(code)


# -- evidence for one exported output ----------------------------------------------------------

def resolve_citations(answer, citations):
    """(answer with card-local [S<n>] groups rewritten to their URLs, number of citations rewritten).
    A group naming an unknown number is left as written, and then grades as a fake source."""
    count = 0

    def swap(match):
        nonlocal count
        urls = [citations.get('S%d' % int(n)) for n in re.findall(r'\d+', match.group(0))]
        if not urls or not all(urls):
            return match.group(0)
        count += len(urls)
        return ' '.join(urls)

    return CITE_GROUP.sub(swap, answer), count


def evidence_for(corpus, task, card, card_files, blobs):
    """(Evidence, citations rewritten) from one exported output; see the module docstring."""
    data = blobs.get('answer.md')
    answer, resolved = resolve_citations(data.decode('utf-8', 'replace') if data is not None else '',
                                         card['citations'])
    outputs = {rel[len('outputs/'):]: blob for rel, blob in blobs.items() if rel.startswith('outputs/')}
    before = {path: fc._sha(blob) for path, blob in card_files.items()}
    after = {**before, **{path: fc._sha(blob) for path, blob in outputs.items()}}
    hook = getattr(corpus, 'allowed_paths_for', None)
    allowed = tuple(hook(task, ARM)) if callable(hook) else tuple(task.allowed_paths)
    claims = bool(final_lines(answer))
    evidence = Evidence(answer=answer, state='completed' if claims else 'ended', before=before, after=after,
                        files={path: outputs.get(path, card_files.get(path)) for path in allowed}, arm=ARM,
                        claims_complete=claims)
    return evidence, resolved


def fixture_log(card):
    """Every attached page served and every pack URL shown: what a card's reader had in hand."""
    urls = list(card['citations'].values())
    return ([{'event': 'search', 'query': 'frontier card attachments', 'results': urls}]
            + [{'event': 'fetch', 'url': url, 'status': 'served'} for url in urls])


def grade_output(corpus, task, card, card_files, blobs, workdir):
    """One output's grade: observable checks, the not-observable list and the taxonomy if it fails."""
    evidence, resolved = evidence_for(corpus, task, card, card_files, blobs)
    verdict = corpus.grade(task, evidence, workdir, fixture_log=fixture_log(card) if task.web_pack else None)
    checks = verdict['checks']
    observable = {name: checks[name] is True for name in sorted(checks)
                  if name not in NOT_OBSERVABLE and name not in OPERATOR_RECORDED}
    classify = getattr(corpus, 'classify', None)
    failed = (classify(task, evidence, {**verdict, 'passed': False}) if callable(classify)
              else 'false_completion' if evidence.claims_complete else 'wrong')
    return {'grader': verdict.get('grader'), 'checks': observable,
            'not_observable': sorted(name for name in checks if name in NOT_OBSERVABLE),
            'operator_recorded': sorted(name for name in checks if name in OPERATOR_RECORDED),
            'observable_passed': bool(observable) and all(observable.values()) and not verdict.get('grader_unavailable'),
            'claims_complete': evidence.claims_complete, 'taxonomy_if_failed': failed,
            'flags': {k: v for k, v in sorted((verdict.get('flags') or {}).items()) if type(v) is int},
            'counts': {k: v for k, v in sorted((verdict.get('counts') or {}).items()) if type(v) is int},
            'reasons': [r for r in verdict.get('reasons') or () if isinstance(r, str) and CODE.fullmatch(r)
                        and r not in NOT_OBSERVABLE and r not in OPERATOR_RECORDED],
            'citations_resolved': resolved}


# -- the blinded folder ---------------------------------------------------------------------------

def read_blinded(blinded_dir):
    """(blinded.json, its raw bytes, {token: {path: bytes}}), every byte checked against blinded.json."""
    tree = fc._read_tree(Path(blinded_dir), code='blinded', **BLINDED_LIMITS)
    raw = tree.pop(fc.BLINDED_NAME, None)
    if raw is None:
        _refuse('blinded_manifest_missing')
    listing = fc._parse_json(raw, 'blinded_manifest_invalid')
    if listing.get('schema') != fc.BLINDED_SCHEMA or not isinstance(listing.get('entries'), list):
        _refuse('blinded_manifest_invalid')
    outputs, expected = {}, set()
    try:
        for item in listing['entries']:
            token, card_id = item['token'], item['card_id']
            if (not isinstance(token, str) or not TOKEN.fullmatch(token) or token in outputs
                    or not isinstance(card_id, str) or not fc.CARD_ID.fullmatch(card_id)):
                _refuse('blinded_manifest_invalid')
            blobs = {}
            for file in item['files']:
                rel = fc._relative(file['path'])
                data = tree.get('%s/%s/%s' % (card_id, token, rel))
                if data is None:
                    _refuse('blinded_output_missing:%s/%s' % (card_id, token))
                if fc._sha(data) != file['sha256']:
                    _refuse('blinded_output_tampered:%s/%s' % (card_id, token))
                blobs[rel] = data
                expected.add('%s/%s/%s' % (card_id, token, rel))
            outputs[token] = blobs
    except (KeyError, TypeError):
        _refuse('blinded_manifest_invalid')
    extra = sorted(set(tree) - expected)
    if extra:
        _refuse('blinded_output_unexpected:' + extra[0][:120])
    return listing, raw, outputs


def _campaign(entries, kind, campaign_id):
    return [e for e in entries if e['kind'] == kind and e.get('campaign_id') == campaign_id]


def _blind_entry(entries, campaign_id, blinded_raw):
    blinds = _campaign(entries, 'blind', campaign_id)
    if not blinds:
        _refuse('not_blinded')
    matching = [e for e in blinds if e.get('blinded_sha256') == fc._sha(blinded_raw)]
    if not matching:
        _refuse('blinded_manifest_tampered')
    return matching[0]


def _card_files(files, card_id):
    prefix = 'cards/%s/files/' % card_id
    return {path[len(prefix):]: data for path, data in files.items() if path.startswith(prefix)}


def grade_blinded(export_dir, blinded_dir, store_dir, grades_path, *, corpus=None):
    """Grade every blinded output once, write the grades file exclusively and record its digest in the log."""
    corpus = fc.load_corpus() if corpus is None else corpus
    manifest, files = fc._verified(export_dir, corpus)
    listing, raw, outputs = read_blinded(blinded_dir)
    store = fc._private_dir(store_dir, 'store')
    log = fc.FrontierLog(store)
    entries = log.entries()
    campaign_id = listing.get('campaign_id')
    _, plan, plan_sha256 = fc._plan(store, campaign_id, entries)
    if plan.get('export_manifest_sha256') != fc._sha(files['manifest.json']) or listing.get('plan_sha256') != plan_sha256:
        _refuse('plan_export_mismatch')
    _blind_entry(entries, campaign_id, raw)
    if _campaign(entries, 'grade', campaign_id):
        _refuse('already_graded')  # one grading per campaign: never re-graded away
    out = Path(grades_path)
    if out.exists() or out.is_symlink():
        _refuse('grades_exists')
    if not out.parent.is_dir():
        _refuse('grades_parent_missing')
    cards = {card['card_id']: card for card in manifest['cards']}
    rows = []
    with tempfile.TemporaryDirectory(prefix='pebble-frontier-grade-') as work:  # the interpreter child's folders
        for item in listing['entries']:
            card = cards.get(item['card_id'])
            if card is None:
                _refuse('blinded_card_unknown:' + item['card_id'])
            task = corpus.BY_ID[card['task_id']]
            grade = grade_output(corpus, task, card, _card_files(files, card['card_id']), outputs[item['token']],
                                 Path(work) / item['token'])
            rows.append({'token': item['token'], 'card_id': card['card_id'], 'slot': card['slot'],
                         'task_id': card['task_id'], 'files': item['files'], **grade})
    document = {'schema': GRADES_SCHEMA, 'campaign_id': campaign_id, 'plan_sha256': plan_sha256,
                'export_manifest_sha256': fc._sha(files['manifest.json']), 'blinded_sha256': fc._sha(raw),
                'corpus': manifest['corpus'], 'reporter_sha256': fc._sha(Path(__file__).read_bytes()), 'arm': ARM,
                'not_observable': sorted(NOT_OBSERVABLE), 'operator_recorded': sorted(OPERATOR_RECORDED),
                'grades': rows}
    data = fc._json_bytes(document)
    fc._write_new(out, data, 0o600)
    log.append({'kind': 'grade', 'campaign_id': campaign_id, 'grades_sha256': fc._sha(data), 'graded': len(rows)})
    return {**document, 'grades_sha256': fc._sha(data)}


# -- unblinding and the report --------------------------------------------------------------------

def cell_outcome(session, deviations, grade, operator):
    """(outcome, taxonomy) by the pre-registered REPORT_RULES: recorded outcome, then budget, then the grade."""
    recorded = session['outcome']
    if recorded == 'skipped':
        return 'not_run', None
    if recorded in ('attachment_failure', 'refused'):
        return recorded, grade and ('passed' if _passed(grade, operator) else grade['taxonomy_if_failed'])
    if recorded == 'client_error':
        return 'error', None
    taxonomy = None if grade is None else 'passed' if _passed(grade, operator) else grade['taxonomy_if_failed']
    if recorded == 'timeout' or 'over_time' in deviations:
        return 'timeout', taxonomy
    if 'extra_turns' in deviations or 'unasked_follow_up' in deviations:
        return 'protocol_violation', taxonomy
    if grade is None:
        return 'error', None  # an answered session always has answer.md; this cannot be graded
    return taxonomy, taxonomy


def _passed(grade, operator):
    return grade['observable_passed'] and all(operator.values())


def _interval(passed, cells):
    low, high = wilson(passed, cells)
    return [round(low, 6), round(high, 6)]


def build_report(export_dir, store_dir, campaign_id, blinded_dir, grades_path, key_path, report_path, *, corpus=None):
    """Unblind after complete, recorded grading; log the unblinding; write pebble.frontier_report/1."""
    corpus = fc.load_corpus() if corpus is None else corpus
    manifest, files = fc._verified(export_dir, corpus)
    store = fc._private_dir(store_dir, 'store')
    log = fc.FrontierLog(store)
    entries = log.entries()
    folder, plan, plan_sha256 = fc._plan(store, campaign_id, entries)
    if plan.get('export_manifest_sha256') != fc._sha(files['manifest.json']):
        _refuse('plan_export_mismatch')
    graded = _campaign(entries, 'grade', campaign_id)
    if not graded:
        _refuse('unblinding_before_grading')
    grade_entry = graded[0]
    grades_raw = fc._read_regular(Path(grades_path), 64_000_000, 'grades')
    if fc._sha(grades_raw) != grade_entry['grades_sha256']:
        _refuse('grades_tampered')
    grades = fc._parse_json(grades_raw, 'grades_invalid')
    listing, raw, outputs = read_blinded(blinded_dir)
    blind_entry = _blind_entry(entries, campaign_id, raw)
    if (grades.get('schema') != GRADES_SCHEMA or grades.get('campaign_id') != campaign_id
            or grades.get('blinded_sha256') != fc._sha(raw) or grade_entry['seq'] < blind_entry['seq']):
        _refuse('grades_invalid')
    by_token = {row['token']: row for row in grades['grades']}
    if set(by_token) != set(outputs) or len(by_token) != len(grades['grades']):
        _refuse('grading_incomplete')
    if Path(report_path).exists() or Path(report_path).is_symlink():
        _refuse('report_exists')
    # Grading is complete and recorded: only now is the key read.
    key_raw = fc._read_regular(Path(key_path), 4_000_000, 'key')
    if fc._sha(key_raw) != blind_entry['key_sha256']:
        _refuse('key_tampered')
    key = fc._parse_json(key_raw, 'key_invalid')
    keyed = {row['ingest_id']: row for row in key['entries']}
    ingests = _campaign(entries, 'ingest', campaign_id)
    if set(keyed) != {e['ingest_id'] for e in ingests} or {r['token'] for r in key['entries'] if r['blinded']} \
            != set(outputs):
        _refuse('store_changed_after_blinding')
    cards = {card['card_id']: card for card in manifest['cards']}
    sessions = []
    for entry in ingests:
        record_raw = fc._read_regular(folder / entry['ingest_id'] / 'record.json', 4_000_000, 'store_record')
        if fc._sha(record_raw) != entry['record_sha256']:
            _refuse('store_tampered:' + entry['ingest_id'])
        record = fc._parse_json(record_raw, 'store_record_invalid')
        session, row = record['session'], keyed[entry['ingest_id']]
        grade = by_token.get(row['token']) if row['blinded'] else None
        operator = {name: OPERATOR_RECORDED[name](session) for name in (grade or {}).get('operator_recorded', ())}
        outcome, taxonomy = cell_outcome(session, record['deviations'], grade, operator)
        card = cards[record['card_id']]
        sessions.append({
            'card_id': card['card_id'], 'slot': card['slot'], 'task_id': card['task_id'],
            'product': record['product'], 'attempt': record['attempt'], 'primary': record['primary'],
            'versions': {name: session[name] for name in ('client_version', 'model_label', 'mode')},
            'plan_tier': session['plan_tier'], 'date': session['date'], 'timezone': session['timezone'],
            'os': session['os'], 'started_at': session['started_at'], 'ended_at': session['ended_at'],
            'wall_seconds': (fc._instant(session['ended_at']) - fc._instant(session['started_at'])).total_seconds(),
            'tool_limits': {'tools_enabled': session['tools_enabled'], 'errors_or_limits': session['errors_or_limits'],
                            'budget_seconds': fc.BUDGET['wall_seconds'], 'follow_ups_allowed': fc.BUDGET['follow_ups']},
            'turns': session['turns'], 'questions_asked': session['questions_asked'],
            'follow_up_given': session['follow_up_given'], 'recorded_outcome': session['outcome'],
            'deviations': record['deviations'], 'graded': grade is not None,
            'checks': (grade or {}).get('checks', {}), 'not_observable': (grade or {}).get('not_observable', []),
            'operator_recorded': operator, 'flags': (grade or {}).get('flags', {}),
            'outcome': outcome, 'taxonomy': taxonomy, 'success': outcome == 'passed'})
    primary = {}
    for row in sessions:
        primary.setdefault((row['card_id'], row['product']), row)  # the first attempt; repeats never replace it
    cells = []
    for card_id, product in plan['cells']:
        row, card = primary.get((card_id, product)), cards[card_id]
        attempts = [s for s in sessions if (s['card_id'], s['product']) == (card_id, product)]
        cells.append({'card_id': card_id, 'slot': card['slot'], 'task_id': card['task_id'], 'product': product,
                      'outcome': row['outcome'] if row else 'not_run', 'taxonomy': row['taxonomy'] if row else None,
                      'success': bool(row and row['success']), 'reason': None if row else 'no_session',
                      'attempts': len(attempts), 'repeat_outcomes': [s['outcome'] for s in attempts[1:]]})
    products = {}
    for product in plan['products']:
        mine = [c for c in cells if c['product'] == product]
        passed = sum(c['success'] for c in mine)
        slots = {}
        for slot, _, _ in fc.SLOTS:
            sub = [c for c in mine if c['slot'] == slot]
            slots[slot] = {'cards': len(sub), 'passed': sum(c['success'] for c in sub),
                           'wilson_95': _interval(sum(c['success'] for c in sub), len(sub))}
        outcomes = Counter(c['outcome'] for c in mine)
        products[product] = {'cards': len(mine), 'passed': passed, 'rate': passed / len(mine) if mine else None,
                             'wilson_95': _interval(passed, len(mine)),
                             'outcomes': {name: outcomes[name] for name in OUTCOMES},
                             'by_slot': slots, 'repeats': sum(c['attempts'] > 1 for c in mine)}
    report = {
        'schema': REPORT_SCHEMA, 'statement': STATEMENT, 'never_merged': NEVER_MERGED,
        'claim': 'none: descriptive only; different models and products', 'campaign_id': campaign_id,
        'unblinding': {'after_grading': True, 'grade_entry_seq': grade_entry['seq'],
                       'recorded': 'an unblind entry in the store log carries this report\'s digest'},
        'inputs': {'plan_sha256': plan_sha256, 'export_manifest_sha256': fc._sha(files['manifest.json']),
                   'blinded_sha256': fc._sha(raw), 'grades_sha256': grade_entry['grades_sha256'],
                   'key_sha256': blind_entry['key_sha256'], 'report_rules_sha256': fc._sha(fc._canonical(fc.REPORT_RULES)),
                   'corpus': manifest['corpus'], 'exporter_sha256': manifest['exporter_sha256'],
                   'reporter_sha256': fc._sha(Path(__file__).read_bytes())},
        'grading': {'graders': 'eval_corpus_18.grade (research cards: eval_research_18.grade_research)',
                    'arm': ARM, 'not_observable': sorted(NOT_OBSERVABLE), 'operator_recorded': sorted(OPERATOR_RECORDED),
                    'verdict': fc.REPORT_RULES['verdict'], 'success': fc.REPORT_RULES['success']},
        'rules': fc.REPORT_RULES,
        'products': products, 'cards': cells, 'sessions': sessions,
        'counting': {'planned_cells': len(cells), 'sessions': len(sessions),
                     'not_run': sum(c['outcome'] == 'not_run' for c in cells),
                     'rule': 'every planned (card, product) cell counted once, by its first attempt'},
        'context': {'published_benchmarks': [],
                    'rule': 'context only, each with source URL, date accessed and model version; never in a Pebble '
                            'table and never support for a claim'},
        'limitations': list(LIMITATIONS),
    }
    data = (json.dumps(report, indent=2, ensure_ascii=False) + '\n').encode('utf-8')  # statement first, as written
    log.append({'kind': 'unblind', 'campaign_id': campaign_id, 'grades_sha256': grade_entry['grades_sha256'],
                'key_sha256': blind_entry['key_sha256'], 'report_sha256': fc._sha(data)})
    fc._write_new(Path(report_path), data, 0o600)
    return report


# -- command line -----------------------------------------------------------------------------------

def _parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    grade = commands.add_parser('grade', help='grade every blinded output (no key is read)')
    grade.add_argument('--export', type=Path, required=True)
    grade.add_argument('--blinded', type=Path, required=True)
    grade.add_argument('--store', type=Path, required=True)
    grade.add_argument('--out', type=Path, required=True, help='new grades file')
    report = commands.add_parser('report', help='unblind after recorded grading and write the frontier report')
    report.add_argument('--export', type=Path, required=True)
    report.add_argument('--store', type=Path, required=True)
    report.add_argument('--campaign', required=True)
    report.add_argument('--blinded', type=Path, required=True)
    report.add_argument('--grades', type=Path, required=True)
    report.add_argument('--key', type=Path, required=True)
    report.add_argument('--out', type=Path, required=True, help='new report file')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == 'grade':
            grades = grade_blinded(args.export, args.blinded, args.store, args.out)
            result = {'campaign_id': grades['campaign_id'], 'graded': len(grades['grades']),
                      'observable_passed': sum(row['observable_passed'] for row in grades['grades']),
                      'grades_sha256': grades['grades_sha256']}
        else:
            report = build_report(args.export, args.store, args.campaign, args.blinded, args.grades, args.key,
                                  args.out)
            print(report['statement'] + '\n' + report['never_merged'])
            result = {'campaign_id': report['campaign_id'],
                      'products': {name: {k: p[k] for k in ('cards', 'passed', 'wilson_95')}
                                   for name, p in report['products'].items()}}
    except fc.CardError as exc:
        print('Refused: ' + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
