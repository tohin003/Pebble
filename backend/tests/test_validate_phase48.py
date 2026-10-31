"""Phase 48: scripts/validate_phase48.py, 2.0.0's About credit and the iPhone app's wording (D222; source and frozen).

The real checkout passes; each check fails when the defect it guards returns (bloub named again under Pebble's version
or AgentGuard dropped from it, the credit gone, inside the text column, as large as the line, under the 11 px floor or out of the corner, a
second bloub mention, a place in the notes that no longer says the iPhone app is offered only as source code, an App
Store or TestFlight offer), so no check passes vacuously. The Pebble Remote guide, published but outside the tested
snapshot, is held to the same wording here.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase48_under_test', ROOT / 'scripts/validate_phase48.py')
v48 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase48_under_test'] = v48
spec.loader.exec_module(v48)


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v48.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def fails(tmp_path, check):
    result = campaign(tmp_path, {check})[check]
    assert not result['passed'], result.get('evidence')
    return result


def edited(monkeypatch, rel, old, new):
    real = v48.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v48, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v48.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items()
                                                        if not r['passed']}
    evidence = results['bloub_is_credited_in_smaller_type_in_the_corner']['evidence']
    assert evidence['credit_px'] == 11 < evidence['line_px'] == 12, evidence  # the floor (V1-119), under the line


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v48.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v48.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v48.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v48_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v48.READS) <= set(vhc.sources()) and vhc.SUITES['phase48'] == 'validate_phase48.py'
    assert 'phase48' in vhc.default_suites('phase48') and 'phase48' not in vhc.default_suites('phase47')
    assert 'phase47' in vhc.default_suites('phase48')


@pytest.mark.parametrize('old, new', [
    ('<small>Reliability: AgentGuard (Apache 2.0).</small>',
     '<small>Avatar engine: bloub (MIT). Reliability: AgentGuard (Apache 2.0).</small>'),
    ('<small>Reliability: AgentGuard (Apache 2.0).</small>', '<small>Reliability: checked (Apache 2.0).</small>'),
])
def test_bloub_under_the_version_or_agentguard_dropped_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'src/App.vue', old, new)
    fails(tmp_path, 'about_names_agentguard_under_the_version')


CREDIT = v48.CREDIT


@pytest.mark.parametrize('old, new', [
    (CREDIT, ''),
    (CREDIT, CREDIT.replace('(MIT)', '')),
    ('<small>Reliability: AgentGuard (Apache 2.0).</small>\n            </div>\n            <!-- 2.0.0 (D222): the avatar '
     "engine's credit, in smaller type in the panel's corner. -->\n            " + CREDIT,
     '<small>Reliability: AgentGuard (Apache 2.0).</small>' + CREDIT + '\n            </div>'),  # moved into the column
    ("<summary>What's new in", "<summary>What's new (bloub) in"),
])
def test_a_credit_gone_moved_or_doubled_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'src/App.vue', old, new)
    fails(tmp_path, 'bloub_is_credited_in_smaller_type_in_the_corner')


@pytest.mark.parametrize('old, new', [
    ('  font-size: 11px;\n  line-height: 1.3;\n  text-align: right;', '  font-size: 12px;\n  line-height: 1.3;\n  text-align: right;'),
    ('  font-size: 11px;\n  line-height: 1.3;\n  text-align: right;', '  font-size: 10px;\n  line-height: 1.3;\n  text-align: right;'),
    ('  align-self: flex-end;\n  margin: 0 0 0 auto;', '  margin: 0 0 0 auto;'),
    ('  margin: 0 0 0 auto;\n  font-size: 11px;', '  margin: 0;\n  font-size: 11px;'),
    ('  font-size: 11px;\n  line-height: 1.3;\n  text-align: right;', '  font-size: 11px;\n  line-height: 1.3;\n  text-align: left;'),
])
def test_a_credit_as_large_or_out_of_the_corner_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'src/style.css', old, new)
    fails(tmp_path, 'bloub_is_credited_in_smaller_type_in_the_corner')


@pytest.mark.parametrize('old, new', [
    ("cards. The iPhone app won't be on the App Store: it will be\noffered only as source code, to build and install "
     "with Xcode.", 'cards.'),
    ("- The iPhone app won't be on the App Store. It will be offered only as source code, which you build and install "
     "on your\n  iPhone with Xcode; that code is not published yet.\n", ''),
    ("- The iPhone app's source code is not published yet, so until it is, Pebble Remote has no phone to pair with. "
     "The app\n  won't be on the App Store; it will be offered only as source code, to build with Xcode.\n", ''),
    ('### Highlights\n', '### Highlights\n\n- Get Pebble Remote on the App Store.\n'),
    ('### Highlights\n', '### Highlights\n\n- Pebble Remote is in TestFlight.\n'),
])
def test_notes_that_offer_the_app_otherwise_fail(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'CHANGELOG.md', old, new)
    fails(tmp_path, 'the_iphone_app_is_offered_only_as_source_code')


def test_the_guide_says_the_app_is_offered_only_as_source_code():
    guide = re.sub(r'\s+', ' ', (ROOT / 'docs/PEBBLE_REMOTE.md').read_text(encoding='utf-8'))
    need = guide[guide.index('## What you need'):guide.index('## ', guide.index('## What you need') + 3)]
    assert "Pebble Remote won't be on the App Store" in need and 'offered only as source code' in need, need
    assert 'not published yet' in need and 'TestFlight' not in guide
