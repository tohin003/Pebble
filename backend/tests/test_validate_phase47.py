"""Phase 47: scripts/validate_phase47.py, 2.0.0's version and release notes (source and frozen).

The real checkout passes; each check fails when the defect it guards returns (a figure that differs from the registered
results, "beats" claimed beyond the plan's rule or praise beyond it, a family that trails left out, the unsafe cell or
the release rule left out, a pending placeholder, a saving claim, Context Warping, Stealth Lockdown without its honest
line, About's What's new from another release, a version declaration left behind), so no check passes vacuously. Where
the records exist (the checkout, not a clean clone), the validator's fixed figures are held to the gate reports.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase47_under_test', ROOT / 'scripts/validate_phase47.py')
v47 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase47_under_test'] = v47
spec.loader.exec_module(v47)
GOLDEN = ROOT / '.runtime/phase46-dev/golden'


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v47.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def fails(tmp_path, check):
    result = campaign(tmp_path, {check})[check]
    assert not result['passed'], result.get('evidence')
    return result


def edited(monkeypatch, rel, old, new):
    real = v47.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v47, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v47.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items()
                                                        if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v47.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v47.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v47.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v47_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v47.READS) <= set(vhc.sources()) and vhc.SUITES['phase47'] == 'validate_phase47.py'
    assert 'phase47' in vhc.default_suites('phase47') and 'phase47' not in vhc.default_suites('phase43')


def test_a_version_left_behind_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(v47, 'checkout_version', lambda: '1.9.29')
    fails(tmp_path, 'release_version_is_2_0_0_everywhere')


@pytest.mark.parametrize('old, new', [
    ('235 of 255 (92.2 %)', '236 of 255 (92.5 %)'),
    ('| 3.9 % | 7.1 % |', '| 2.9 % | 7.1 % |'),
    ('| $0.042 | $0.050 |', '| $0.041 | $0.050 |'),
    ('12.1 s, 46.5 s', '11.1 s, 46.5 s'),
    ('+4.7 points (+0.4 to +9.4) against Codex', '+4.7 points (+1.4 to +9.4) against Codex'),
])
def test_a_figure_that_differs_from_the_results_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'CHANGELOG.md', old, new)
    fails(tmp_path, 'the_notes_carry_the_measured_figures')


@pytest.mark.parametrize('old, new', [
    ('  - Against Codex: no single family.', '  - Against Codex: no single family.\n- Pebble beats Codex overall.'),
    ('  - Against Codex: no single family.', '  - Against Codex: long tasks (`long_horizon`).'),
    ('(`workshop_repeat`, 15 against 12)', '(`workshop_repeat`, 15 against 12) and `memory`'),
    ('### How 2.0.0 compares\n', '### How 2.0.0 compares\n\nPebble is better than Claude Code.\n'),
])
def test_beats_beyond_the_plans_rule_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'CHANGELOG.md', old, new)
    fails(tmp_path, 'beats_is_claimed_only_where_the_plan_allows')


@pytest.mark.parametrize('old, new', [
    ('  - long tasks against Codex (`long_horizon`, 11 against 12).\n', ''),
    ('was graded unsafe for Pebble; Claude Code had none', 'was graded for Pebble'),
    ('(decision D219)', '(a decision)'),
])
def test_notes_that_leave_out_what_trails_or_the_rule_fail(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'CHANGELOG.md', old, new)
    fails(tmp_path, 'the_notes_name_what_trails_the_unsafe_cell_and_the_release_rule')


def test_a_pending_placeholder_fails(tmp_path, monkeypatch):
    edited(monkeypatch, 'CHANGELOG.md', '### How 2.0.0 compares\n', '### How 2.0.0 compares\n\nGATE6-PENDING\n')
    fails(tmp_path, 'the_notes_name_what_trails_the_unsafe_cell_and_the_release_rule')


@pytest.mark.parametrize('old, new', [
    ('### How 2.0.0 compares\n', '### How 2.0.0 compares\n\nTasks are 30 % cheaper.\n'),
    ('### How 2.0.0 compares\n', '### How 2.0.0 compares\n\nContext Warping keeps long tasks small.\n'),
    ('not from a\n  camera', 'not from\n  anything'),  # the 2.0.0 entry wraps the line there
])
def test_notes_that_overclaim_fail(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'CHANGELOG.md', old, new)
    fails(tmp_path, 'the_notes_claim_nothing_unmeasured')


@pytest.mark.parametrize('old, new', [
    ('const WHATS_NEW_VERSION = "2.0.0";', 'const WHATS_NEW_VERSION = "1.9.14";'),
    ('"Pebble works across your Mac inside a sandbox', '"Pebble works across your whole Mac inside a sandbox'),
])
def test_whats_new_from_another_release_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'src/App.vue', old, new)
    fails(tmp_path, 'whats_new_is_2_0_0s_highlights')


# ---- the fixed figures against the records (the checkout only) ----------------------------------------------------

def reports():
    named = {'Claude Code': GOLDEN / 'A-pebble-4.gates.json', 'Codex': GOLDEN / 'B-pebble-5.gates.json'}
    if not all(path.is_file() for path in named.values()) or not (GOLDEN / 'rerun-1.9.29-results.md').is_file():
        pytest.skip('the held-out records are not in this checkout (a clean clone)')
    return {rival: json.loads(path.read_text()) for rival, path in named.items()}


def test_the_fixed_figures_are_the_gate_reports():
    found = reports()
    text = (GOLDEN / 'rerun-1.9.29-results.md').read_text()
    figures = ' '.join(v47.FIGURES)
    for rival, report in found.items():
        key, gates = report['competitor'], report['gates']
        families = gates['2_every_family']['families']
        mine, theirs = sum(f['pebble'] for f in families.values()), sum(f[key] for f in families.values())
        assert report['complete'] and gates['1_overall']['pass'] and report['gates_1_to_5_pass']
        assert '| %d of 255 (%.1f %%) | %d of 255 (%.1f %%) |' % (mine, mine / 2.55, theirs, theirs / 2.55) in figures
        honesty = gates['3_honesty']
        assert '| %.1f %% | %.1f %% |' % (honesty['pebble'] * 100, honesty[key] * 100) in figures
        cost = gates['4_cost']
        assert '| $%.3f | $%.3f |' % (cost['pebble'], cost[key]) in figures
        overall = gates['1_overall']
        low, high = (x * 100 for x in overall['ci95'])
        assert '+%.1f points (+%.1f to +%.1f) against %s' % (overall['diff'] * 100, low, high, rival) in figures
        assert sorted(report['beats_families']) == sorted(v47.BEATS[rival])
        behind = sorted(f for f, v in families.items() if v['pebble'] < v[key])
        assert all('`%s`, %d' % (f, families[f]['pebble']) in ' '.join(v47.TRAILS) for f in behind), behind
    # The median and 95th percentile (the analysis's nearest-rank figures) as the results note gives them.
    times = re.findall(r'- (Pebble|Claude Code|Codex): median ([\d.]+) s, p95 ([\d.]+) s', text)
    assert len(times) == 4, times
    (_, a_med, a_p95), (_, c_med, c_p95), (_, b_med, b_p95), (_, x_med, x_p95) = times
    assert '| %s s, %s s | %s s, %s s |' % (a_med, a_p95, c_med, c_p95) in figures
    assert '| %s s, %s s | %s s, %s s |' % (b_med, b_p95, x_med, x_p95) in figures
