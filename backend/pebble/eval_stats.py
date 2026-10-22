"""Phase 18 evaluation statistics (design study §3.4). Standard library only.

Every function is deterministic for its inputs and seed. Seeded procedures
return their seed and resample count so a report can be recomputed exactly.

- ``wilson(k, n)``: two-sided 95% Wilson score interval for a run-level rate (descriptive).
- ``clopper_pearson_upper(k, n, alpha)``: exact one-sided upper bound, for zero-event metrics.
- ``cluster_bootstrap(rows, stat, *, cluster, resamples, seed)``: percentile interval that
  resamples whole clusters (tasks), so repetitions of one task are never treated as independent.
- ``sign_flip(diffs, *, flips, seed)``: two-sided paired permutation test on per-task differences
  (exact enumeration when it is no larger than the requested flips).
- ``mcnemar_exact(b, c)``: two-sided exact McNemar on run-level discordant pairs; it ignores clustering.
- ``nearest_rank(values, q)``: the nearest-rank quantile used by the search benchmarks.
"""

import math
import random

Z95 = 1.959963984540054
SCALE = 10 ** 9  # Differences are compared as exact integers after rounding to 1e-9.


def _count(k, n):
    if type(k) is not int or type(n) is not int or n < 0 or not 0 <= k <= n:
        raise ValueError('Counts must be integers with 0 <= k <= n.')


def wilson(k, n, z=Z95):
    """(low, high) Wilson score interval; (0.0, 1.0) when n is 0."""
    _count(k, n)
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def _binomial_cdf(k, n, p):
    """P(X <= k) for X ~ Binomial(n, p), summed in log space."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    base = math.lgamma(n + 1)
    terms = [base - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_p + (n - i) * log_q for i in range(k + 1)]
    top = max(terms)
    return min(1.0, math.exp(top) * math.fsum(math.exp(t - top) for t in terms))


def clopper_pearson_upper(k, n, alpha=0.05):
    """Exact one-sided (1 - alpha) upper bound for a binomial rate; 1.0 when n is 0 or k equals n."""
    _count(k, n)
    if not 0 < alpha < 1:
        raise ValueError('alpha must lie strictly between 0 and 1.')
    if n == 0 or k == n:
        return 1.0
    if k == 0:
        return 1.0 - alpha ** (1.0 / n)
    low, high = k / n, 1.0
    for _ in range(200):  # The CDF falls monotonically in p; bisection to float precision.
        middle = (low + high) / 2
        if _binomial_cdf(k, n, middle) > alpha:
            low = middle
        else:
            high = middle
        if high - low < 1e-15:
            break
    return high


def _key(cluster):
    if callable(cluster):
        return cluster
    if isinstance(cluster, str):
        return lambda row: row[cluster]
    raise ValueError('cluster must be a field name or a callable.')


def cluster_bootstrap(rows, stat, *, cluster, resamples=10_000, seed, level=0.95):
    """Percentile bootstrap that resamples clusters with replacement.

    ``stat`` receives the concatenated rows of the drawn clusters (a cluster drawn
    twice contributes its rows twice) and returns a number or None. Resamples
    whose statistic is undefined are counted, never silently dropped.
    """
    if not callable(stat):
        raise ValueError('stat must be callable.')
    if type(resamples) is not int or not 1 <= resamples <= 1_000_000:
        raise ValueError('resamples must be an integer from 1 to 1,000,000.')
    if type(seed) is not int:
        raise ValueError('seed must be an integer.')
    if not 0 < level < 1:
        raise ValueError('level must lie strictly between 0 and 1.')
    key, groups = _key(cluster), {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    order = sorted(groups, key=repr)
    point = stat([row for name in order for row in groups[name]]) if order else None
    values, undefined = [], 0
    if order:
        rng = random.Random(seed)
        size = len(order)
        for _ in range(resamples):
            drawn = [order[rng.randrange(size)] for _ in range(size)]
            value = stat([row for name in drawn for row in groups[name]])
            if value is None:
                undefined += 1
            else:
                values.append(value)
    values.sort()
    tail = (1 - level) / 2
    low = values[max(0, math.ceil(tail * len(values)) - 1)] if values else None
    high = values[max(0, math.ceil((1 - tail) * len(values)) - 1)] if values else None
    return {'point': point, 'low': low, 'high': high, 'level': level, 'method': 'cluster_percentile_bootstrap',
            'clusters': len(order), 'resamples': resamples, 'undefined_resamples': undefined, 'seed': seed}


def _scaled(diffs):
    values = []
    for value in diffs:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Differences must be finite numbers.')
        values.append(round(value * SCALE))
    return values


def _subset_tables(values, width=8):
    """Signed-sum lookup tables for chunks of ``width`` values (bit set means a flipped sign)."""
    tables = []
    for start in range(0, len(values), width):
        chunk = values[start:start + width]
        table = [0] * (1 << len(chunk))
        for mask in range(1 << len(chunk)):
            table[mask] = sum(-v if mask >> i & 1 else v for i, v in enumerate(chunk))
        tables.append((len(chunk), table))
    return tables


def sign_flip(diffs, *, flips=100_000, seed):
    """Two-sided sign-flip permutation test of mean(diffs) == 0.

    The statistic is |sum(d_i)|. Zero differences carry no sign. When
    2**(nonzero count) <= flips every sign pattern is enumerated and the p-value
    is exact; otherwise ``flips`` seeded random patterns give p = (1 + hits) / (1 + flips).
    """
    if type(flips) is not int or not 1 <= flips <= 10_000_000:
        raise ValueError('flips must be an integer from 1 to 10,000,000.')
    if type(seed) is not int:
        raise ValueError('seed must be an integer.')
    scaled = _scaled(diffs)
    nonzero = [v for v in scaled if v]
    observed = abs(sum(scaled))
    mean = sum(scaled) / SCALE / len(scaled) if scaled else None
    base = {'n': len(scaled), 'nonzero': len(nonzero), 'mean': mean, 'flips': flips, 'seed': seed,
            'method': 'paired_sign_flip_two_sided'}
    if not nonzero:
        return {**base, 'p_value': 1.0, 'exact': True, 'patterns': 1}
    tables = _subset_tables(nonzero)
    if 1 << len(nonzero) <= flips:
        hits = 0
        for mask in range(1 << len(nonzero)):
            total, shift = 0, 0
            for width, table in tables:
                total += table[mask >> shift & ((1 << width) - 1)]
                shift += width
            hits += abs(total) >= observed
        return {**base, 'p_value': hits / (1 << len(nonzero)), 'exact': True, 'patterns': 1 << len(nonzero)}
    rng, hits = random.Random(seed), 0
    for _ in range(flips):
        total = 0
        for width, table in tables:
            total += table[rng.getrandbits(width)]
        hits += abs(total) >= observed
    return {**base, 'p_value': (1 + hits) / (1 + flips), 'exact': False, 'patterns': flips}


def mcnemar_exact(b, c):
    """Two-sided exact McNemar p-value from discordant counts b and c (Binomial(b + c, 1/2))."""
    if type(b) is not int or type(c) is not int or b < 0 or c < 0:
        raise ValueError('Discordant counts must be non-negative integers.')
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * _binomial_cdf(min(b, c), n, 0.5))


def nearest_rank(values, q):
    """Nearest-rank quantile: ordered[ceil(q * n) - 1]; None for no values."""
    if not 0 < q <= 1:
        raise ValueError('q must lie in (0, 1].')
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def holm(p_values):
    """Holm step-down adjusted p-values (family-wise), in the input order."""
    indexed = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted, running = [None] * len(indexed), 0.0
    for rank, (index, p) in enumerate(indexed):
        if not 0 <= p <= 1:
            raise ValueError('p-values must lie in [0, 1].')
        running = max(running, min(1.0, (len(indexed) - rank) * p))
        adjusted[index] = running
    return adjusted
