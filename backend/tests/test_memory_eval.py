"""MR-1 is seeded, model-free and honest: v2 never recalls a harmful record,
reruns are identical, and every failure (including expected synonym misses)
stays in the report."""

import importlib.util
import itertools
import json
import time
from pathlib import Path

import pytest

from pebble import memory_eval
from pebble.memory_eval import ALWAYS_HARMFUL, DISTRACTORS, FAMILIES, PARAPHRASES

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'evaluate_memory_recall.py'


def ticking():
    counter = itertools.count()
    return lambda: next(counter) * 0.001


def test_corpus_has_twelve_families_three_paraphrases_and_labelled_distractors():
    assert len(FAMILIES) == 12 and all(len(f['queries']) == 3 for f in FAMILIES)
    kinds = {kind for f in FAMILIES for kind, _ in f['queries']}
    assert kinds == set(PARAPHRASES)
    for family in FAMILIES:
        assert family['disabled_layer'][0] != family['kind'] and set(DISTRACTORS) - {'disabled_layer'} <= set(family)
    corpus = memory_eval.Corpus(3, 30, memory_eval.ANCHOR)
    try:
        db = corpus.store.db
        assert len(corpus.queries) == 36 and len(corpus.contested) == 12
        superseded = db.execute("SELECT count(*) FROM memory_evidence WHERE status='superseded'").fetchone()[0]
        expired = db.execute('SELECT count(*) FROM memories WHERE expires IS NOT NULL AND expires<=?', (time.time(),)).fetchone()[0]
        forgotten = db.execute("SELECT count(*) FROM memories WHERE id LIKE 'mr1-%-forgotten'").fetchone()[0]
        assert (superseded, expired, forgotten) == (12, 12, 0)
        assert db.execute('SELECT count(*) FROM memory_tombstones').fetchone()[0] == 12
        assert db.execute('SELECT count(*) FROM memories WHERE scope=?', (memory_eval.OTHER_WORKSPACE,)).fetchone()[0] >= 12
        assert db.execute("SELECT count(*) FROM memories WHERE id LIKE 'mr1-noise-%'").fetchone()[0] == 30
        labels = {label for _, label, _ in corpus.labels.values()}
        assert labels == set(DISTRACTORS) and corpus.oracle()(['mr1-staging-port-contested', 'x']) == {
            'mr1-staging-port-contested': 'contested'}
    finally:
        corpus.close()


def test_v2_has_no_harmful_inclusions_at_every_noise_level():
    report = memory_eval.evaluate(15, rankers=('v2',), windows=(4096,), clock=ticking())
    rows = report['results']
    assert [r['noise'] for r in rows] == [30, 300, 1500]
    assert all(r['harmful_inclusions'] == 0 and not r['harmful_by_label'] for r in rows)
    assert all(r['conflict_note']['expected'] > 0 and r['conflict_note']['present'] == r['conflict_note']['expected']
               for r in rows)
    assert report['gates'] == {'v2_harmful_inclusions_zero': True, 'v2_conflict_note_when_contested_matches': True}
    assert not [f for f in report['failures'] if f['type'] != 'missed_target']
    assert all(r['budget'] == 600 and r['used_chars_mean'] <= 600 for r in rows)


def test_report_compares_v1_with_v2_and_keeps_every_failure():
    report = memory_eval.evaluate(15, noise_levels=(30,), windows=(4096, 32768), clock=ticking())
    assert [(r['ranker'], r['window']) for r in report['results']] == [
        ('v1', 4096), ('v1', 32768), ('v2', 4096), ('v2', 32768)]
    assert [(c['noise'], c['window']) for c in report['comparison']] == [(30, 4096), (30, 32768)]
    v1 = [r for r in report['results'] if r['ranker'] == 'v1']
    # The Phase 11 ranker has no relation filter: contested records get through.
    assert sum(r['harmful_inclusions'] for r in v1) > 0
    failures = report['failures']
    for row in report['results']:
        mine = [f for f in failures if (f['ranker'], f['noise'], f['window']) == (row['ranker'], row['noise'], row['window'])]
        missed = [f for f in mine if f['type'] == 'missed_target']
        assert len(missed) == round((1 - row['recall']) * row['queries'])
        assert len([f for f in mine if f['type'] == 'harmful_inclusion']) == row['harmful_inclusions']
        assert len([f for f in mine if f['type'] == 'missing_conflict_note']) == (
            row['conflict_note']['expected'] - row['conflict_note']['present'])
        assert set(row['by_paraphrase']) == set(PARAPHRASES)
    assert all(f['expected_lexical_miss'] == (f['paraphrase'] == 'synonym') for f in failures if f['type'] == 'missed_target')
    assert {f['label'] for f in failures if f['type'] == 'harmful_inclusion'} <= set(ALWAYS_HARMFUL)
    assert all(row['latency_ms']['p50'] <= row['latency_ms']['p95'] <= row['latency_ms']['max'] for row in report['results'])
    assert report['corpus']['abbreviation_queries_using_ranker_table'] >= 1 and len(report['limits']) >= 5
    assert report['environment']['model'] == 'none' and report['environment']['embedder'].startswith('none')


def test_same_seed_gives_the_same_report_and_seeds_differ():
    first = memory_eval.evaluate(7, noise_levels=(30,), windows=(4096,), clock=ticking())
    second = memory_eval.evaluate(7, noise_levels=(30,), windows=(4096,), clock=ticking())
    assert first == second and json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    real = memory_eval.evaluate(7, noise_levels=(30,), windows=(4096,))  # wall-clock latency
    assert real['deterministic_sha256'] == first['deterministic_sha256'] == memory_eval.digest(real)
    other = memory_eval.evaluate(8, noise_levels=(30,), windows=(4096,), clock=ticking())
    assert other['deterministic_sha256'] != first['deterministic_sha256']


def test_python_scan_and_fts5_trigram_index_agree():
    python = memory_eval.evaluate(5, noise_levels=(300,), windows=(4096,), trigram='python', clock=ticking())
    assert python['environment']['trigram_modes'] == ['python']
    try:
        fts = memory_eval.evaluate(5, noise_levels=(300,), windows=(4096,), trigram='fts5', clock=ticking())
    except ValueError as exc:
        pytest.skip(str(exc))
    assert fts['environment']['trigram_modes'] == ['fts5']
    for key in ('results', 'comparison', 'failures', 'gates', 'corpus'):
        assert fts[key] == python[key]


@pytest.mark.parametrize('kwargs', [{'seed': -1}, {'seed': 1.5}, {'noise_levels': (6000,)}, {'noise_levels': ()},
                                    {'windows': (0,)}, {'rankers': ('v3',)}, {'trigram': 'bogus'}])
def test_invalid_options_are_refused(kwargs):
    with pytest.raises(ValueError):
        memory_eval.evaluate(**kwargs)


def load_script():
    spec = importlib.util.spec_from_file_location('evaluate_memory_recall', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_default_run_writes_a_report_that_passes_the_gates(tmp_path, capsys):
    out = tmp_path / 'mr1-report.json'
    assert load_script().main(['--out', str(out)]) == 0
    report = json.loads(out.read_text())
    assert (report['seed'], report['noise_levels'], report['windows']) == (15, [30, 300, 1500], [4096, 32768])
    assert len(report['results']) == 12 and all(report['gates'].values())
    assert report['deterministic_sha256'] == memory_eval.digest(report)
    printed = capsys.readouterr().out
    assert printed.count('harmful=') == 12 and report['deterministic_sha256'] in printed


def test_cli_writes_only_a_new_report_path(tmp_path, capsys):
    script = load_script()
    out = tmp_path / 'mr1.json'
    assert script.main(['--out', str(out), '--noise', '30', '--windows', '4096', '--seed', '3']) == 0
    report = json.loads(out.read_text())
    assert report['schema'] == memory_eval.SCHEMA and report['seed'] == 3 and report['noise_levels'] == [30]
    assert sorted(p.name for p in tmp_path.iterdir()) == ['mr1.json']
    before = out.read_bytes()
    with pytest.raises(SystemExit):
        script.main(['--out', str(out), '--noise', '30', '--windows', '4096'])
    assert out.read_bytes() == before
    link = tmp_path / 'link.json'
    link.symlink_to(tmp_path / 'elsewhere.json')
    with pytest.raises(SystemExit):
        script.main(['--out', str(link), '--noise', '30', '--windows', '4096'])
    assert not (tmp_path / 'elsewhere.json').exists()
    with pytest.raises(SystemExit):
        script.main(['--out', str(tmp_path / 'missing' / 'r.json'), '--noise', '30', '--windows', '4096'])
    assert not (tmp_path / 'missing').exists()
    with pytest.raises(SystemExit):
        script.main(['--out', str(tmp_path / 'r2.json'), '--noise', 'thirty'])
    assert 'gates=' in capsys.readouterr().out
