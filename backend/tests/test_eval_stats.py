"""Phase 18 evaluation statistics: stdlib-only, deterministic, checked against exact values."""
import ast
import math
from pathlib import Path

import pytest

from pebble import eval_stats as st


def binomial_cdf(k, n, p):
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def test_module_imports_only_the_standard_library():
    tree = ast.parse(Path(st.__file__).read_text(encoding='utf-8'))
    imported = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {'math', 'random'}


def test_wilson_matches_the_closed_form():
    low, high = st.wilson(5, 10)
    assert low == pytest.approx(0.236593, abs=1e-6) and high == pytest.approx(0.763407, abs=1e-6)
    low, high = st.wilson(0, 10)
    assert low == pytest.approx(0.0, abs=1e-12) and high == pytest.approx(0.277533, abs=1e-6)
    assert st.wilson(10, 10)[1] == pytest.approx(1.0) and st.wilson(0, 0) == (0.0, 1.0)
    for bad in ((-1, 3), (4, 3), (1.0, 3), (True, 3)):
        with pytest.raises(ValueError):
            st.wilson(*bad)


def test_clopper_pearson_upper_is_exact():
    assert st.clopper_pearson_upper(0, 120) == pytest.approx(1 - 0.05 ** (1 / 120))
    assert st.clopper_pearson_upper(0, 120) < 0.025  # the design study's "2.5% at n = 120"
    for k, n in ((1, 10), (3, 40), (7, 120), (59, 60)):
        bound = st.clopper_pearson_upper(k, n)
        assert k / n < bound < 1 and binomial_cdf(k, n, bound) == pytest.approx(0.05, abs=1e-9)
    assert st.clopper_pearson_upper(1, 10) == pytest.approx(0.39416, abs=1e-4)
    assert st.clopper_pearson_upper(5, 5) == 1.0 and st.clopper_pearson_upper(0, 0) == 1.0
    assert st.clopper_pearson_upper(2, 50, alpha=0.01) > st.clopper_pearson_upper(2, 50)
    with pytest.raises(ValueError):
        st.clopper_pearson_upper(1, 10, alpha=1.5)


def test_cluster_bootstrap_resamples_whole_clusters_and_is_seeded():
    # One cluster all successes, one all failures: any whole-cluster resample has mean 0, 0.5 or 1.
    rows = [{'task': 'a', 'ok': 1}] * 50 + [{'task': 'b', 'ok': 0}] * 50
    mean = lambda rs: sum(r['ok'] for r in rs) / len(rs)
    result = st.cluster_bootstrap(rows, mean, cluster='task', resamples=2000, seed=7)
    assert result['point'] == 0.5 and result['clusters'] == 2 and result['seed'] == 7
    assert result['low'] == 0.0 and result['high'] == 1.0  # 100 rows give no false precision
    assert result == st.cluster_bootstrap(rows, mean, cluster=lambda r: r['task'], resamples=2000, seed=7)
    assert st.cluster_bootstrap(rows, mean, cluster='task', resamples=2000, seed=8)['seed'] == 8
    # Undefined resamples are counted, not hidden.
    odd = st.cluster_bootstrap(rows, lambda rs: None if all(r['ok'] for r in rs) else mean(rs), cluster='task',
                               resamples=400, seed=3)
    assert 0 < odd['undefined_resamples'] < 400
    empty = st.cluster_bootstrap([], mean, cluster='task', resamples=10, seed=1)
    assert empty['point'] is None and empty['low'] is None and empty['clusters'] == 0
    for kwargs in ({'resamples': 0, 'seed': 1}, {'resamples': 10, 'seed': 1.5}, {'resamples': 10, 'seed': 1, 'level': 1}):
        with pytest.raises(ValueError):
            st.cluster_bootstrap(rows, mean, cluster='task', **kwargs)


def test_sign_flip_exact_enumeration_and_seeded_sampling():
    assert st.sign_flip([1, 1, 1], flips=100_000, seed=1)['p_value'] == pytest.approx(2 / 8)
    exact = st.sign_flip([1] * 10, flips=100_000, seed=1)
    assert exact['exact'] is True and exact['p_value'] == pytest.approx(2 / 1024) and exact['patterns'] == 1024
    assert st.sign_flip([1, -1, 1, -1], flips=1000, seed=1)['p_value'] == 1.0
    assert st.sign_flip([0, 0, 0], flips=10, seed=1) == {
        'n': 3, 'nonzero': 0, 'mean': 0.0, 'flips': 10, 'seed': 1, 'method': 'paired_sign_flip_two_sided',
        'p_value': 1.0, 'exact': True, 'patterns': 1}
    # Rational differences compare exactly (thirds from three blocks): ties at |sum| = 1 count as hits.
    thirds = st.sign_flip([1 / 3, 2 / 3, -1 / 3, 1 / 3], flips=100_000, seed=2)
    assert thirds['exact'] and thirds['p_value'] == pytest.approx(8 / 16)
    sampled = st.sign_flip([1] * 20, flips=2000, seed=5)
    assert sampled['exact'] is False and sampled['p_value'] == pytest.approx(1 / 2001)
    mixed = [0.5, 1, -0.5, 1, 0, 1, 0.5, -1, 1, 1, 0.5, 1, 0, 1, 1, -0.5, 1, 1, 0.5, 1]
    assert st.sign_flip(mixed, flips=5000, seed=9) == st.sign_flip(mixed, flips=5000, seed=9)
    enumerated = st.sign_flip(mixed[:12], flips=100_000, seed=9)
    sampled = st.sign_flip(mixed[:12], flips=1000, seed=9)
    assert enumerated['exact'] and not sampled['exact']
    assert abs(enumerated['p_value'] - sampled['p_value']) < 0.05
    with pytest.raises(ValueError):
        st.sign_flip([float('nan')], flips=10, seed=1)


def test_mcnemar_exact_two_sided():
    assert st.mcnemar_exact(0, 0) == 1.0
    assert st.mcnemar_exact(0, 6) == pytest.approx(2 * 0.5 ** 6)
    assert st.mcnemar_exact(9, 1) == pytest.approx(2 * 11 / 1024)
    assert st.mcnemar_exact(5, 5) == 1.0
    with pytest.raises(ValueError):
        st.mcnemar_exact(-1, 2)


def test_nearest_rank_matches_the_search_benchmark_definition():
    values = list(range(1, 21))
    assert st.nearest_rank(values, 0.95) == 19 and st.nearest_rank(values, 0.5) == 10
    assert st.nearest_rank(values[::-1], 1.0) == 20 and st.nearest_rank([3.5], 0.95) == 3.5
    assert st.nearest_rank([], 0.95) is None
    with pytest.raises(ValueError):
        st.nearest_rank(values, 0)


def test_holm_step_down():
    assert st.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert st.holm([0.2, 0.9]) == pytest.approx([0.4, 0.9]) and st.holm([]) == []
