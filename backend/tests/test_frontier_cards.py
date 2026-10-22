"""Phase 18 frontier protocol tooling (scripts/frontier_cards.py): gates G24-G31 of the threat model.

The exporter runs against corpus 18a.1 (eval_corpus_18, track Q1) with the pinned
CARD_SELECTION. Attacks use variants of the real corpus: tasks replaced field by
field (dataclasses.replace), with the corpus module's own markers, baseline views
and FixtureWeb pack. Nothing here contacts a model, the network, a profile or a
credential.
"""
import dataclasses
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import eval_corpus_18 as ec
from pebble import evaluation as ev
from pebble.eval_research_18 import WEB_PACKS, FixtureWeb

ROOT = Path(__file__).resolve().parents[2]


def _load_script():
    spec = importlib.util.spec_from_file_location('frontier_cards_under_test', ROOT / 'scripts/frontier_cards.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fc = _load_script()
SELECTION = fc.CARD_SELECTION
CARD = {task_id: 'card-%02d' % n for n, (_, task_id) in enumerate(SELECTION, 1)}
ELLERY = WEB_PACKS['ellery-2026']


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def variant(markers=None, **replacements):
    """Corpus 18a.1 with some tasks' fields replaced: variant(**{'code-p90': {'files': ...}})."""
    tasks = tuple(dataclasses.replace(t, **replacements[t.id]) if t.id in replacements else t for t in ec.CORPUS)
    return SimpleNamespace(CORPUS_VERSION=ec.CORPUS_VERSION, CORPUS=tasks, BY_ID={t.id: t for t in tasks},
                           ORACLES=ec.ORACLES, oracle_markers=markers or ec.oracle_markers,
                           corpus_fingerprint=lambda: sha(canonical([[t.id, t.version, t.fingerprint()] for t in tasks])),
                           baseline_prompt=ec.baseline_prompt, baseline_files=ec.baseline_files, WEB_PACKS=WEB_PACKS)


def marker(task_id):
    return ec.oracle_markers(task_id)[0]


def files_with(task_id, path, extra):
    return tuple((p, t + extra if p == path else t) for p, t in ec.BY_ID[task_id].files)


def pack_with(*entries, base=ELLERY):
    return {'schema': base['schema'], 'entries': list(base['entries']) + list(entries)}


def tree(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


class Witness:
    """Records the Phase 18 registry call the plan step makes (EvidenceRegistry.register_plan)."""

    def __init__(self):
        self.plans = []

    def register_plan(self, plan_sha256, thresholds_sha256):
        self.plans.append((plan_sha256, thresholds_sha256))


@pytest.fixture(scope='module')
def built():
    return fc.build_cards(ec)


@pytest.fixture
def campaign(tmp_path):
    export = tmp_path / 'export'
    manifest = fc.export_cards(export, ec)
    ledger = tmp_path / 'ledger'
    ledger.mkdir(mode=0o700)
    store, witness = ledger / 'frontier', Witness()
    plan = fc.plan_sessions(export, ['ChatGPT', 'Claude'], store, registry=witness, corpus=ec)
    return SimpleNamespace(export=export, manifest=manifest, ledger=ledger, store=store, plan=plan, witness=witness,
                           tmp=tmp_path)


def record(c, card_id='card-01', product='ChatGPT', **changes):
    card = next((item for item in c.manifest['cards'] if item['card_id'] == card_id), {'card_sha256': 'f' * 64})
    value = {'schema': fc.SESSION_SCHEMA, 'campaign_id': c.plan['campaign_id'], 'card_id': card_id,
             'card_sha256': card['card_sha256'], 'product': product, 'client_version': '1.2026.284 (4410)',
             'model_label': 'Frontier Model 5 Thinking', 'mode': 'default', 'plan_tier': 'Plus',
             'tools_enabled': {'web': False, 'code': True, 'files': True}, 'os': 'macOS 27.0',
             'date': '2026-10-12', 'timezone': 'Asia/Kolkata', 'started_at': '2026-10-12T10:00:00+05:30',
             'ended_at': '2026-10-12T10:09:30+05:30', 'turns': 1, 'questions_asked': 0, 'follow_up_given': False,
             'errors_or_limits': [], 'outcome': 'answered', 'notes': ''}
    value.update(changes)
    return value


def session_dir(c, name, value, answer='Fixed.\nFINAL: done\n', outputs=None):
    folder = c.tmp / name
    folder.mkdir()
    (folder / 'session.json').write_text(json.dumps(value))
    if answer is not None:
        (folder / 'answer.md').write_text(answer)
    for rel, text in (outputs or {}).items():
        path = folder / 'outputs' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return folder


def store_state(c):
    log = (c.store / fc.LOG_NAME).read_bytes()
    return log, sorted(p.name for p in (c.store / c.plan['campaign_id']).iterdir())


# -- G24: cards are oracle-free and task-anonymous ---------------------------------------------

def test_g24_the_18a1_cards_carry_no_oracle_marker_and_no_task_identity(built):
    manifest, files = built
    markers = [m.casefold() for t in ec.CORPUS for m in ec.oracle_markers(t.id)]
    assert len(markers) > 40
    assert all(not any(m in data.decode().casefold() for m in markers) for data in files.values())
    cards = {path: data for path, data in files.items() if path.startswith('cards/')}
    assert all(t.id not in data.decode() for t in ec.CORPUS for data in cards.values())
    assert [(c['card_id'], c['slot'], c['task_id']) for c in manifest['cards']] == [
        ('card-%02d' % n, slot, task_id) for n, (slot, task_id) in enumerate(SELECTION, 1)]
    assert manifest['corpus'] == {'version': ec.CORPUS_VERSION, 'fingerprint': ec.corpus_fingerprint()}
    # The follow-up answer lives only in the operator file and reaches a session only if the product asks.
    operator = json.loads(files['operator/%s.json' % CARD['form-crate']])
    assert operator['follow_up'] == 'crate_size: large\ndestination: Hill quarter depot. The crate holds fragile glassware.'
    assert all(b'fragile glassware' not in data for data in cards.values())
    assert json.loads(files['operator/card-01.json'])['follow_up'] == ev.GENERIC_ANSWER


def test_g24_card_text_is_the_baseline_prompt_verbatim_plus_fixed_notes(built):
    _, files = built
    text = {task_id: files['cards/%s/card.md' % card].decode() for task_id, card in CARD.items()}
    assert text['code-p90'] == ec.baseline_prompt(ec.BY_ID['code-p90']) + '\n\n' + fc.NOTES['folder'] + '\n'
    assert text['research-arrivals'] == ec.baseline_prompt(ec.BY_ID['research-arrivals']) + '\n\n' + fc.NOTES['sources'] + '\n'
    assert text['memory-ports'] == ec.baseline_prompt(ec.BY_ID['memory-ports']) + '\n\n' + fc.NOTES['folder'] + '\n'
    assert text['form-crate'].endswith('\n\n' + fc.NOTES['folder'] + '\n\n' + fc.NOTES['questions'] + '\n')
    for task_id, card in CARD.items():
        expected = {'files/' + p: t.encode() for p, t in ec.baseline_files(ec.BY_ID[task_id])}
        attached = {p[len('cards/%s/' % card):]: d for p, d in files.items()
                    if p.startswith('cards/%s/files/' % card) and '/sources/' not in p}
        assert attached == expected, task_id
    assert files['cards/%s/files/notes.md' % CARD['memory-ports']] == ec.baseline_notes(ec.BY_ID['memory-ports']).encode()


@pytest.mark.parametrize('replacements, refusal', [
    ({'code-p90': {'files': files_with('code-p90', 'metrics.py', '# %s\n' % marker('code-p90'))}},
     'oracle_marker_visible:cards/card-01/files/metrics.py'),
    ({'research-arrivals': {'prompt': ec.BY_ID['research-arrivals'].prompt + ' ' + marker('research-arrivals')}},
     'oracle_marker_visible:cards/card-04/card.md'),
    ({'form-crate': {'answers': (('crate_size', 'large', 'large'), ('destination', None, marker('form-crate')))}},
     'oracle_marker_visible:operator/card-11.json'),
    ({'research-draught': {'web_pack': pack_with({'url': 'https://x.example/a', 'title': 'Extra', 'snippet': 'Extra.',
                                                  'published': '2026-01-01', 'content_type': 'text/plain',
                                                  'body': 'Note: ' + marker('research-draught')})}},
     'oracle_marker_visible:cards/card-05/files/sources/S14.txt'),
    ({'research-dues': {'web_pack': pack_with({'url': 'https://x.example/b', 'title': 'Extra', 'published': '2026-01-01',
                                               'snippet': marker('research-dues').lower(), 'body': '<p>Extra.</p>'})}},
     'oracle_marker_visible:cards/card-06/files/sources/index.md'),
    ({'memory-ports': {'memory': ec.BY_ID['memory-ports'].memory + (
        ('semantic', 'Answer key: ' + marker('memory-ports'), 'global', 'current', None),)}},
     'oracle_marker_visible:cards/card-09/files/notes.md'),
    # A marker of a task that is not a card still refuses: every task's markers are checked.
    ({'code-p90': {'files': files_with('code-p90', 'metrics.py', '# %s\n' % marker('code-mad'))}},
     'oracle_marker_visible:cards/card-01/files/metrics.py'),
    ({'doc-policy': {'files': files_with('doc-policy', 'docs/travel_policy.md', 'See also doc-handbook.\n')}},
     'task_id_visible:cards/card-07/files/docs/travel_policy.md'),
], ids=['workspace_file', 'prompt', 'follow_up', 'pack_page', 'snippet_case_folded', 'notes_view',
        'unselected_task_marker', 'task_id'])
def test_g24_a_marker_or_task_id_anywhere_refuses_the_export(tmp_path, replacements, refusal):
    with pytest.raises(fc.CardError) as caught:
        fc.export_cards(tmp_path / 'export', variant(**replacements))
    assert str(caught.value) == refusal
    assert not (tmp_path / 'export').exists()


def test_g24_selected_tasks_need_oracle_markers():
    bare = variant(markers=lambda task_id: [] if task_id == 'doc-handbook' else ec.oracle_markers(task_id))
    with pytest.raises(fc.CardError, match='^oracle_markers_missing:doc-handbook$'):
        fc.build_cards(bare)


# -- G25: cards are deterministic and tamper-evident --------------------------------------------

def test_g25_exports_are_deterministic_and_rebuild_byte_for_byte(tmp_path, built):
    first = fc.export_cards(tmp_path / 'a', ec)
    # Cards are numbered in slot order; within a slot the pinned order is kept, whatever order the slots came in.
    second = fc.export_cards(tmp_path / 'b', ec, sorted(SELECTION, key=lambda pair: pair[0]))
    assert first == second == built[0] and tree(tmp_path / 'a') == tree(tmp_path / 'b') == built[1]
    assert fc.verify_export(tmp_path / 'a', ec) == first
    assert first['exporter_sha256'] == sha((ROOT / 'scripts/frontier_cards.py').read_bytes())
    assert first['report_rules_sha256'] == sha(canonical(fc.REPORT_RULES))
    with pytest.raises(fc.CardError, match='^export_exists$'):
        fc.export_cards(tmp_path / 'a', ec)


def _edit(path, old, new):
    data = path.read_bytes()
    assert old in data
    path.write_bytes(data.replace(old, new, 1))


def _tamper_card(root):
    _edit(root / 'cards/card-02/files/series.py', b'def ', b'def  ')


def _tamper_operator(root):
    _edit(root / 'operator/card-11.json', b'Hill quarter', b'Low town')


def _extra_file(root):
    (root / 'cards/card-01/files/hint.txt').write_text('The answer is in metrics.py.')


def _missing_file(root):
    (root / 'cards/card-04/files/sources/S2.txt').unlink()


def _linked_file(root):
    path = root / 'cards/card-07/files/docs/travel_policy.md'
    path.unlink()
    path.symlink_to(root.parent / 'elsewhere.md')


def _consistent_forgery(root):
    """Edit a card and recompute its digests in the manifest: only the rebuild can tell."""
    path = root / 'cards/card-08/card.md'
    _edit(path, b'Edit handbook.md', b'Rewrite handbook.md')
    manifest = json.loads((root / 'manifest.json').read_text())
    card = manifest['cards'][7]
    for item in card['files']:
        if item['path'] == 'card.md':
            item['sha256'], item['bytes'] = sha(path.read_bytes()), len(path.read_bytes())
    card['card_sha256'] = fc._card_digest(card['card_id'], card['task_id'], card['task_fingerprint'], card['files'],
                                          card['operator_sha256'])
    (root / 'manifest.json').write_bytes(fc._json_bytes(manifest))


@pytest.mark.parametrize('tamper, digests_only, refusal', [
    (_tamper_card, True, 'card_tampered:card-02'),
    (_tamper_operator, True, 'card_tampered:card-11'),
    (_extra_file, True, 'export_extra_file:cards/card-01/files/hint.txt'),
    (_missing_file, True, 'card_file_missing:card-04'),
    (_linked_file, True, 'export_not_regular:cards/card-07/files/docs/travel_policy.md'),
    (_consistent_forgery, False, 'export_differs_from_corpus'),
], ids=['card_byte', 'operator_answer', 'extra_file', 'missing_file', 'symlink', 'consistent_forgery'])
def test_g25_tampered_exports_are_refused(tmp_path, tamper, digests_only, refusal):
    fc.export_cards(tmp_path / 'export', ec)
    (tmp_path / 'elsewhere.md').write_text('# Travel policy\n')
    tamper(tmp_path / 'export')
    if not digests_only:
        fc.verify_export(tmp_path / 'export')  # the digests alone agree
    with pytest.raises(fc.CardError) as caught:
        fc.verify_export(tmp_path / 'export', None if digests_only else ec)
    assert str(caught.value) == refusal


def test_g25_a_changed_corpus_fails_the_rebuild_and_finder_metadata_is_ignored(tmp_path):
    fc.export_cards(tmp_path / 'export', ec)
    (tmp_path / 'export/cards/.DS_Store').write_bytes(b'\0Bud1')
    assert fc.verify_export(tmp_path / 'export', ec)['cards'][0]['task_id'] == 'code-p90'
    changed = variant(**{'doc-handbook': {'files': files_with('doc-handbook', 'handbook.md', '\n')}})
    with pytest.raises(fc.CardError, match='^export_differs_from_corpus$'):
        fc.verify_export(tmp_path / 'export', changed)


# -- G26: the card selection is pre-registered, complete and eligible --------------------------

def _swap(slot, task_id):
    index = next(i for i, (s, _) in enumerate(SELECTION) if s == slot)
    return SELECTION[:index] + ((slot, task_id),) + SELECTION[index + 1:]


@pytest.mark.parametrize('selection, replacements, refusal', [
    (SELECTION[:-1], {}, 'card_selection_invalid'),
    (SELECTION[:-1] + (('coding', 'code-p90'),), {}, 'card_selection_invalid'),
    (SELECTION[:-1] + (('coding', 'code-mad'),), {}, 'card_mix_invalid'),
    (_swap('coding', 'missing-task'), {}, 'card_task_unknown:missing-task'),
    (_swap('documents', 'native-numbers'), {}, 'card_task_ineligible:native-numbers:blocked'),
    (_swap('documents', 'recovery-two-files'), {}, 'card_task_ineligible:recovery-two-files:restart_boundary'),
    (_swap('documents', 'ui-quiz'), {}, 'card_task_ineligible:ui-quiz:pebble_only_grant'),
    (_swap('documents', 'doc-clauses'), {'doc-clauses': {'mutation': ('contract_v1.md', 'changed\n')}},
     'card_task_ineligible:doc-clauses:host_mutation'),
    (_swap('research', 'research-gauge'), {'research-gauge': {'web_pack': None}},
     'card_task_ineligible:research-gauge:live_web'),
    # The form tool is allowed only on the clarification card, which then gets the 'questions' note.
    (tuple(('documents', 'form-crate') if pair[1] == 'doc-policy' else ('clarification', 'doc-clauses')
           if pair[0] == 'clarification' else pair for pair in SELECTION), {},
     'card_task_ineligible:form-crate:pebble_only_tool'),
    (_swap('memory', 'memory-format'), {'memory-format': {'baseline_view': 'same'}},
     'card_task_ineligible:memory-format:memory_without_notes'),
    (SELECTION, {'form-crate': {'answers': (('crate_size', 'large', None),)}},
     'card_task_ineligible:form-crate:form_answer'),
    (_swap('coding', 'search-definition'), {}, 'card_task_ineligible:search-definition:family_mismatch'),
    (_swap('research', 'deleg-research'), {}, 'card_task_ineligible:deleg-research:family_mismatch'),
    (_swap('clarification', 'doc-clauses'), {}, 'card_task_ineligible:doc-clauses:no_fixed_answer'),
    (_swap('research', 'research-gauge'), {'research-gauge': {'web_pack': None, 'tools': ()}},
     'card_task_ineligible:research-gauge:no_source_pack'),
    (_swap('memory', 'memory-format'), {'memory-format': {'baseline_view': 'same', 'memory': ()}},
     'card_task_ineligible:memory-format:no_notes_view'),
])
def test_g26_the_selection_must_be_complete_and_eligible(selection, replacements, refusal):
    with pytest.raises(fc.CardError) as caught:
        fc.build_cards(variant(**replacements), selection)
    assert str(caught.value) == refusal


def test_g26_the_pinned_selection_has_the_preregistered_mix(monkeypatch):
    assert fc.CARD_COUNT == 12 and {s: n for s, n, _ in fc.SLOTS} == {
        'coding': 3, 'research': 3, 'documents': 2, 'memory': 2, 'clarification': 1, 'hostile_source': 1}
    assert all(fc.ineligible(ec, ec.BY_ID[task_id], slot) is None for slot, task_id in SELECTION)
    assert ec.BY_ID['research-timetable'].category == 'hostile_page' and ec.BY_ID['code-rise'].category == \
        'false_completion_trap' and ec.BY_ID['research-draught'].category == 'snippet_trap'
    monkeypatch.setattr(fc, 'CARD_SELECTION', None)
    with pytest.raises(fc.CardError, match='^card_selection_unpinned$'):
        fc.build_cards(ec)


def test_g26_research_cards_attach_the_reader_text_of_the_frozen_pack(built):
    manifest, files = built
    card = CARD['research-draught']
    sources = {p.rsplit('/', 1)[1]: d.decode() for p, d in files.items()
               if p.startswith('cards/%s/files/sources/' % card)}
    entries = ELLERY['entries']
    assert set(sources) == {'S%d.txt' % n for n in range(1, len(entries) + 1)} | {'index.md'}
    web = FixtureWeb('ellery-2026')  # what either arm reads: the extractor's text, never the raw body
    assert all(sources['S%d.txt' % n] == web.fetch(e['url'])['text'] for n, e in enumerate(entries, 1))
    assert manifest['cards'][4]['citations'] == {'S%d' % n: e['url'] for n, e in enumerate(entries, 1)}
    berth = next(n for n, e in enumerate(entries, 1) if e['url'].endswith('/berths/berth-7'))
    # The snippet trap stays visible: the cached snippet (11.5 m) differs from the page (12.8 m).
    assert '11.5 m' in sources['index.md'] and '12.8 m' in sources['S%d.txt' % berth]
    assert '11.5' not in sources['S%d.txt' % berth]
    hostile = '\n'.join(files[p].decode() for p in files if p.startswith('cards/%s/files/' % CARD['research-timetable']))
    assert 'CANARY-R5-7Q4' in hostile  # visible page text ships, as either arm reads it
    assert 'SYSTEM OVERRIDE' not in hostile and 'CANARY-R5-HID' not in hostile  # hidden text never does
    assert not any(0xE0000 <= ord(c) <= 0xE007F or c in '‍⁠' for c in hostile)


def test_g26_memory_cards_attach_only_what_the_person_kept(tmp_path):
    _, files = fc.build_cards(ec, _swap('memory', 'memory-forgotten'))  # replaces card-09
    notes = files['cards/card-09/files/notes.md'].decode()
    assert notes == ec.baseline_notes(ec.BY_ID['memory-forgotten'])
    assert 'gate code is 2580' in notes and 'padlock code is 9134' not in notes  # forgotten by the person
    _, scoped = fc.build_cards(ec, _swap('memory', 'memory-scope'))
    assert 'port 8899' not in scoped['cards/card-09/files/notes.md'].decode()  # a disabled layer


@pytest.mark.parametrize('replacements, refusal', [
    ({'research-dues': {'web_pack': 'unknown-pack'}}, 'web_pack_shape_unsupported:research-dues'),
    ({'research-dues': {'web_pack': {'schema': 'pebble.fixture_web/1', 'entries': []}}},
     'web_pack_shape_unsupported:research-dues'),
    ({'research-dues': {'web_pack': {'schema': 'pebble.fixture_web/1', 'entries': [
        {k: v for k, v in ELLERY['entries'][0].items() if k != 'published'}]}}},
     'web_pack_page_unreadable:research-dues'),
    ({'code-p90': {'files': (('../metrics.py', 'x = 1\n'),)}}, 'attachment_path_invalid'),
    ({'code-p90': {'files': (('metrics.py', b'binary'),)}}, 'files_invalid:code-p90'),
])
def test_g26_unknown_shapes_refuse_rather_than_guess(replacements, refusal):
    with pytest.raises(fc.CardError) as caught:
        fc.build_cards(variant(**replacements))
    assert str(caught.value) == refusal


# -- G27: sessions are planned first; failures and repeats are retained -------------------------

def test_g27_the_plan_is_witnessed_before_any_session_and_lists_every_cell(campaign):
    plan = campaign.plan
    assert campaign.witness.plans == [(plan['plan_sha256'], sha(canonical(fc.REPORT_RULES)))]
    assert len(plan['cells']) == 24 and ['card-12', 'Claude'] in plan['cells']
    folder = campaign.store / plan['campaign_id']
    assert stat.S_IMODE(os.lstat(campaign.store).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(folder / 'plan.json').st_mode) == 0o600
    assert sha((folder / 'plan.json').read_bytes()) == plan['plan_sha256']
    log = fc.FrontierLog(campaign.store).entries()
    assert [(e['kind'], e['plan_sha256']) for e in log] == [('plan', plan['plan_sha256'])]
    assert stat.S_IMODE(os.lstat(campaign.store / fc.LOG_NAME).st_mode) == 0o600


def test_g27_the_phase18_evidence_registry_witnesses_the_frontier_plan(tmp_path):
    from pebble.eval_registry import EvidenceRegistry
    fc.export_cards(tmp_path / 'export', ec)
    registry = EvidenceRegistry(ev.EvidenceLedger(tmp_path / 'ledger'))
    plan = fc.plan_sessions(tmp_path / 'export', ['Gemini'], tmp_path / 'ledger' / 'frontier', registry=registry,
                            corpus=ec)
    assert [(e['kind'], e['plan_sha256']) for e in registry.entries()] == [('plan', plan['plan_sha256'])]
    assert registry.verify_chain() is True


@pytest.mark.parametrize('products', [[], ['A'] * 2, ['ChatGPT', 'chatgpt'], ['me@example.com'], ['a/b'],
                                      ['x' * 49], ['P%d' % i for i in range(9)]])
def test_g27_product_labels_are_short_distinct_and_anonymous(tmp_path, products):
    fc.export_cards(tmp_path / 'export', ec)
    with pytest.raises(fc.CardError, match='^products_invalid$'):
        fc.plan_sessions(tmp_path / 'export', products, tmp_path / 'store', corpus=ec)


def test_g27_failures_and_repeats_are_retained_with_their_deviations(campaign):
    c = campaign
    first = fc.ingest_session(c.export, session_dir(c, 's1', record(c), outputs={'metrics.py': ec.METRICS}), c.store,
                              corpus=ec)
    assert (first['attempt'], first['primary'], first['deviations']) == (1, True, [])
    assert [f['path'] for f in first['files']] == ['answer.md', 'outputs/metrics.py']
    skipped = fc.ingest_session(c.export, session_dir(c, 's2', record(c, 'card-02', outcome='skipped', turns=0),
                                                      answer=None), c.store, corpus=ec)
    assert skipped['session']['outcome'] == 'skipped' and skipped['files'] == []
    slow = fc.ingest_session(c.export, session_dir(c, 's3', record(
        c, 'card-11', 'Claude', ended_at='2026-10-12T10:20:00+05:30', turns=3, follow_up_given=True,
        tools_enabled={'web': True, 'code': True, 'files': True})), c.store, corpus=ec)
    assert slow['deviations'] == ['over_time', 'extra_turns', 'unasked_follow_up', 'web_enabled']
    refused = fc.ingest_session(c.export, session_dir(c, 's4', record(c, outcome='refused'),
                                                      answer='I cannot help with that.'), c.store, corpus=ec)
    # A second session of the same cell is kept as a repeat; it never replaces the first.
    assert (refused['attempt'], refused['primary']) == (2, False)
    log = fc.FrontierLog(c.store).entries()
    assert [(e['kind'], e.get('cell'), e.get('attempt')) for e in log] == [
        ('plan', None, None), ('ingest', ['card-01', 'ChatGPT'], 1), ('ingest', ['card-02', 'ChatGPT'], 1),
        ('ingest', ['card-11', 'Claude'], 1), ('ingest', ['card-01', 'ChatGPT'], 2)]
    folder = c.store / c.plan['campaign_id'] / first['ingest_id']
    assert sha((folder / 'record.json').read_bytes()) == first['record_sha256'] == log[1]['record_sha256']
    assert (folder / 'files/outputs/metrics.py').read_bytes() == ec.METRICS.encode()
    assert first['task_id'] == 'code-p90'
    assert all(stat.S_IMODE(os.lstat(p).st_mode) == (0o700 if p.is_dir() else 0o600) for p in folder.rglob('*'))


def test_g27_unplanned_or_mismatched_sessions_are_refused_and_leave_no_trace(campaign):
    c = campaign
    cases = [
        (record(c, product='Gemini'), 'Answer\n', 'session_product_not_planned'),
        (record(c, card_sha256='0' * 64), 'Answer\n', 'session_card_digest_mismatch'),
        (record(c, card_id='card-13'), 'Answer\n', 'session_card_unknown'),
        (record(c, campaign_id='f' * 32), 'Answer\n', 'plan_missing'),
        (record(c, campaign_id='../plan'), 'Answer\n', 'campaign_invalid'),
        (record(c), None, 'answer_missing'),
        (record(c, outcome='skipped', turns=0), 'Late answer\n', 'session_skipped_inconsistent'),
        (record(c, outcome='skipped', turns=1), None, 'session_skipped_inconsistent'),
        (record(c, answer_text='FINAL: done'), 'Answer\n', 'session_fields_invalid'),
        (record(c, schema='pebble.frontier_session/0'), 'Answer\n', 'session_schema_invalid'),
    ]
    before = store_state(c)
    for index, (value, answer, refusal) in enumerate(cases):
        with pytest.raises(fc.CardError) as caught:
            fc.ingest_session(c.export, session_dir(c, 'bad%d' % index, value, answer=answer), c.store, corpus=ec)
        assert str(caught.value) == refusal, value
    assert store_state(c) == before


# -- G28: session records carry versions, never identity or credentials -------------------------

@pytest.mark.parametrize('changes, refusal', [
    ({'model_label': 'Model for alice@example.com'}, 'session_identity_or_secret:model_label'),
    ({'notes': 'Signed in as Bob <bob.smith@corp.example.org>'}, 'session_identity_or_secret:notes'),
    ({'notes': 'header Bearer abc.def'}, 'session_identity_or_secret:notes'),
    ({'client_version': 'sk-proj-ABCDEFGH1234'}, 'session_identity_or_secret:client_version'),
    ({'errors_or_limits': ['token eyJhbGciOiJIUzI1.eyJzdWIiOiIx.sig']}, 'session_identity_or_secret:0'),
    ({'mode': 'oaiapp_client'}, 'session_identity_or_secret:mode'),
    ({'plan_tier': 'password Plus'}, 'session_identity_or_secret:plan_tier'),
    ({'account_email': 'x'}, 'session_fields_invalid'),
    ({'os': 'macOS\x1b[31m'}, 'session_field_invalid:os'),
    ({'model_label': ''}, 'session_field_invalid:model_label'),
    ({'started_at': '2026-10-12T10:00:00'}, 'session_times_invalid'),
    ({'ended_at': '2026-10-12T09:00:00+05:30'}, 'session_times_invalid'),
    ({'turns': True}, 'session_field_invalid:turns'),
    ({'date': '2026-02-30'}, 'session_field_invalid:date'),
    ({'tools_enabled': {'web': False, 'code': True}}, 'session_field_invalid:tools_enabled'),
    ({'outcome': 'passed'}, 'session_field_invalid:outcome'),
    ({'errors_or_limits': ['limit'] * 11}, 'session_field_invalid:errors_or_limits'),
])
def test_g28_session_records_refuse_identity_secrets_and_malformed_fields(campaign, changes, refusal):
    with pytest.raises(fc.CardError) as caught:
        fc.validate_session(record(campaign, **changes), campaign.manifest, campaign.plan)
    assert str(caught.value) == refusal


def test_g28_a_complete_record_with_versions_and_tier_is_accepted(campaign):
    value = record(campaign, notes='Client asked one question.\nNo errors.', errors_or_limits=['upload limit 10 files'])
    assert fc.validate_session(value, campaign.manifest, campaign.plan) == value
    assert set(fc.session_template()) == fc.SESSION_FIELDS


# -- G29: product outputs are untrusted ---------------------------------------------------------

def _linked_output(folder, tmp):
    (tmp / 'outside.txt').write_text('secret')
    (folder / 'outputs').mkdir()
    (folder / 'outputs/metrics.py').symlink_to(tmp / 'outside.txt')


def _linked_folder(folder, tmp):
    (tmp / 'outside').mkdir()
    (folder / 'outputs').symlink_to(tmp / 'outside')


def _fifo_output(folder, tmp):
    (folder / 'outputs').mkdir()
    os.mkfifo(folder / 'outputs/pipe')


def _large_output(folder, tmp):
    (folder / 'outputs').mkdir()
    (folder / 'outputs/big.bin').write_bytes(b'0' * (fc.SESSION_LIMITS['max_bytes'] + 1))


def _many_outputs(folder, tmp):
    (folder / 'outputs').mkdir()
    for index in range(fc.SESSION_LIMITS['max_files'] + 1):
        (folder / 'outputs' / ('f%d.txt' % index)).write_text('x')


def _stray_file(folder, tmp):
    (folder / 'browser-history.txt').write_text('history')


def _control_name(folder, tmp):
    (folder / 'outputs').mkdir()
    (folder / 'outputs/a\nb.txt').write_text('x')


@pytest.mark.parametrize('attack, refusal', [
    (_linked_output, 'session_not_regular:outputs/metrics.py'),
    (_linked_folder, 'session_not_regular:outputs'),
    (_fifo_output, 'session_not_regular:outputs/pipe'),
    (_large_output, 'session_too_large'),
    (_many_outputs, 'session_too_large'),
    (_stray_file, 'session_unexpected_file:browser-history.txt'),
    (_control_name, 'session_name_invalid'),
])
def test_g29_outputs_are_read_as_bounded_regular_files_only(campaign, attack, refusal):
    folder = session_dir(campaign, 'attack', record(campaign))
    attack(folder, campaign.tmp)
    before = store_state(campaign)
    with pytest.raises(fc.CardError) as caught:
        fc.ingest_session(campaign.export, folder, campaign.store, corpus=ec)
    assert str(caught.value) == refusal
    assert store_state(campaign) == before


def test_g29_a_linked_session_folder_or_public_store_is_refused(campaign):
    folder = session_dir(campaign, 'real', record(campaign))
    (campaign.tmp / 'link').symlink_to(folder)
    with pytest.raises(fc.CardError, match='^session_not_directory$'):
        fc.ingest_session(campaign.export, campaign.tmp / 'link', campaign.store, corpus=ec)
    os.chmod(campaign.store, 0o755)
    try:
        with pytest.raises(fc.CardError, match='^store_not_private$'):
            fc.ingest_session(campaign.export, folder, campaign.store, corpus=ec)
    finally:
        os.chmod(campaign.store, 0o700)


# -- G30: blinding ------------------------------------------------------------------------------

def test_g30_blinded_copies_carry_no_session_metadata_and_the_key_stays_private(campaign):
    c = campaign
    answers = {('card-01', 'ChatGPT'): 'Patched the rank.\nFINAL: done\n', ('card-01', 'Claude'): 'FINAL: done\n',
               ('card-04', 'Claude'): 'The authority lists 14,382 arrivals [S1].\nFINAL: 14382 [S1]\n'}
    for index, ((card_id, product), answer) in enumerate(answers.items()):
        fc.ingest_session(c.export, session_dir(c, 'ok%d' % index, record(c, card_id, product), answer=answer,
                                                outputs={'metrics.py': ec.METRICS} if card_id == 'card-01' else None),
                          c.store, corpus=ec)
    fc.ingest_session(c.export, session_dir(c, 'skip', record(c, 'card-05', outcome='skipped', turns=0), answer=None),
                      c.store, corpus=ec)
    result = fc.blind(c.store, c.plan['campaign_id'], c.tmp / 'blinded', c.ledger / 'unblinding.json')
    assert (result['blinded'], result['not_blinded']) == (3, 1)
    files = tree(c.tmp / 'blinded')
    listing = json.loads(files.pop(fc.BLINDED_NAME))  # tokens, cards and digests only, for tamper checks
    assert sha(fc._json_bytes(listing)) == result['blinded_sha256'] == fc.FrontierLog(c.store).entries()[-1]['blinded_sha256']
    assert {f'{e["card_id"]}/{e["token"]}/{f["path"]}': f['sha256'] for e in listing['entries'] for f in e['files']} \
        == {p: sha(d) for p, d in files.items()}
    assert not any(leak in json.dumps(listing) for leak in ('ChatGPT', 'Claude', 'attempt', 'outcome', 'ingest'))
    assert sorted({p.split('/')[0] for p in files}) == ['card-01', 'card-04']
    assert all(len(p.split('/')[1]) == 16 and p.split('/', 2)[2] in ('answer.md', 'outputs/metrics.py') for p in files)
    texts = b'\n'.join(files.values()) + '\n'.join(files).encode()
    for leak in (b'ChatGPT', b'Claude', b'Frontier Model', b'1.2026.284', b'Asia/Kolkata', b'session', b'Plus'):
        assert leak not in texts
    assert sorted(files.values()) == sorted([a.encode() for a in answers.values()] + [ec.METRICS.encode()] * 2)
    assert all(os.stat(c.tmp / 'blinded' / p).st_mtime == 0 for p in files)
    key_path = c.ledger / 'unblinding.json'
    assert stat.S_IMODE(os.lstat(key_path).st_mode) == 0o600
    key = json.loads(key_path.read_text())
    assert key['schema'] == fc.KEY_SCHEMA and sha(key_path.read_bytes()) == result['key_sha256']
    assert sorted((e['card_id'], e['product'], e['outcome'], e['blinded']) for e in key['entries']) == [
        ('card-01', 'ChatGPT', 'answered', True), ('card-01', 'Claude', 'answered', True),
        ('card-04', 'Claude', 'answered', True), ('card-05', 'ChatGPT', 'skipped', False)]
    assert fc.FrontierLog(c.store).entries()[-1]['kind'] == 'blind'


def _edit_stored_answer(c, ingest_id):
    (c.store / c.plan['campaign_id'] / ingest_id / 'files/answer.md').write_text('FINAL: 14382 [S1]\n')


def _unlogged_record(c, ingest_id):
    (c.store / c.plan['campaign_id'] / ('a' * 32)).mkdir(mode=0o700)


def _removed_record(c, ingest_id):
    folder = c.store / c.plan['campaign_id'] / ingest_id
    for path in sorted(folder.rglob('*'), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    folder.rmdir()


def _edited_log(c, ingest_id):
    path = c.store / fc.LOG_NAME
    lines = path.read_bytes().splitlines(keepends=True)
    lines[0] = lines[0].replace(b'"kind":"plan"', b'"kind":"plan","note":"x"')
    path.write_bytes(b''.join(lines))


@pytest.mark.parametrize('tamper, refusal', [
    (_edit_stored_answer, 'store_tampered:'), (_unlogged_record, 'store_unlogged_record'),
    (_removed_record, 'store_record_missing'), (_edited_log, 'log_chain_broken'),
])
def test_g30_blinding_rehashes_the_store_and_refuses_tampering(campaign, tamper, refusal):
    c = campaign
    entry = fc.ingest_session(c.export, session_dir(c, 's', record(c)), c.store, corpus=ec)
    tamper(c, entry['ingest_id'])
    with pytest.raises(fc.CardError) as caught:
        fc.blind(c.store, c.plan['campaign_id'], c.tmp / 'blinded', c.ledger / 'key.json')
    assert str(caught.value).startswith(refusal)
    assert not (c.tmp / 'blinded').exists() and not (c.ledger / 'key.json').exists()


def test_g30_the_key_must_be_new_and_in_a_private_folder(campaign):
    c = campaign
    fc.ingest_session(c.export, session_dir(c, 's', record(c)), c.store, corpus=ec)
    public = c.tmp / 'public'
    public.mkdir()
    os.chmod(public, 0o755)
    with pytest.raises(fc.CardError, match='^key_dir_not_private$'):
        fc.blind(c.store, c.plan['campaign_id'], c.tmp / 'blinded', public / 'key.json')
    (c.ledger / 'key.json').write_text('{}')
    with pytest.raises(fc.CardError, match='^blind_key_exists$'):
        fc.blind(c.store, c.plan['campaign_id'], c.tmp / 'blinded', c.ledger / 'key.json')
    (c.tmp / 'blinded').mkdir()
    with pytest.raises(fc.CardError, match='^blind_out_exists$'):
        fc.blind(c.store, c.plan['campaign_id'], c.tmp / 'blinded', c.ledger / 'other.json')


def test_g30_the_store_log_detects_inner_edits_but_not_a_cut_tail(campaign):
    """Inner edits and reordering break the chain. Cutting the newest entries does not (residual R4 of the
    threat model, the same as the evidence registry's F1): blinding then refuses the record it cannot place."""
    c = campaign
    for name in ('s1', 's2'):
        fc.ingest_session(c.export, session_dir(c, name, record(c)), c.store, corpus=ec)
    path = c.store / fc.LOG_NAME
    original = path.read_bytes()
    lines = original.splitlines(keepends=True)
    path.write_bytes(lines[0] + lines[2] + lines[1])
    with pytest.raises(fc.CardError, match='^log_chain_broken$'):
        fc.FrontierLog(c.store).entries()
    path.write_bytes(b''.join(lines[:2]))
    assert len(fc.FrontierLog(c.store).entries()) == 2  # a cut tail still verifies
    with pytest.raises(fc.CardError, match='^store_unlogged_record$'):
        fc.blind(c.store, c.plan['campaign_id'], c.tmp / 'blinded', c.ledger / 'key.json')
    path.write_bytes(original[:-1])
    with pytest.raises(fc.CardError, match='^log_truncated_line$'):
        fc.FrontierLog(c.store).entries()


# -- G31: frontier results never enter the same-model analysis --------------------------------------

def test_g31_frontier_records_and_plans_never_enter_the_same_model_analysis(campaign):
    from pebble.eval_registry import EvidenceRegistry
    c = campaign
    entry = fc.ingest_session(c.export, session_dir(c, 's', record(c)), c.store, corpus=ec)
    plan = ev.campaign_plan('pilot')
    ledger = ev.EvidenceLedger(c.tmp / 'same-model')
    registry = EvidenceRegistry(ledger)
    registry.register_plan(plan['plan_sha256'], plan['thresholds_sha256'])
    registry.register_plan(c.plan['plan_sha256'], sha(canonical(fc.REPORT_RULES)))  # one shared registry
    with pytest.raises(ev.EvidenceRefused) as caught:
        ev.analyse_campaign(plan, [entry], ledger, registry)
    assert caught.value.reasons == ['schema_mismatch']
    with pytest.raises(ev.EvidenceRefused) as caught:
        ev.analyse_campaign(c.plan, [], ledger, registry)
    assert caught.value.reasons == ['plan_invalid']
    assert 'never merged with same-model results' in fc.REPORT_RULES['claim']
    assert 'not attributable to the harness' in fc.REPORT_RULES['claim']


# -- command line ---------------------------------------------------------------------------------

def test_cli_round_trip_and_refusals(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, 'pebble.eval_corpus_18', None)
    assert fc.main(['export', '--out', str(tmp_path / 'none')]) == 2
    assert 'corpus_unavailable' in capsys.readouterr().err
    monkeypatch.undo()
    assert fc.main(['export', '--out', str(tmp_path / 'x'), '--tasks', 'coding']) == 2
    assert 'card_selection_invalid' in capsys.readouterr().err
    assert fc.main(['export', '--out', str(tmp_path / 'export')]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['manifest_sha256'] == sha((tmp_path / 'export/manifest.json').read_bytes())
    assert printed['selection'] == [list(pair) for pair in SELECTION]
    assert fc.main(['verify', str(tmp_path / 'export')]) == 0
    assert json.loads(capsys.readouterr().out)['verified'] == 'digests_and_rebuild'
    ledger = tmp_path / 'ledger'
    ledger.mkdir(mode=0o700)
    assert fc.main(['plan', '--export', str(tmp_path / 'export'), '--products', 'ChatGPT,Pebble (installed)',
                    '--store', str(ledger / 'store'), '--ledger', str(ledger)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['cells'] == 24 and plan['products'] == ['ChatGPT', 'Pebble (installed)']
    monkeypatch.setitem(sys.modules, 'pebble.eval_registry', None)
    assert fc.main(['plan', '--export', str(tmp_path / 'export'), '--products', 'Claude', '--store',
                    str(ledger / 'store'), '--ledger', str(ledger)]) == 2
    assert 'registry_unavailable' in capsys.readouterr().err
