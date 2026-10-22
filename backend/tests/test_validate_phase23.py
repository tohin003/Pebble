"""Phase 23: validate_phase23.py supersedes the retired validate_phase22.py (the person's decision, 2026-10-14).

It must run exactly Phase 22's campaign (same scenarios, fixtures and 46 checks) with the release identity taken from
the checkout, rename only the two checks that spelled out 0.6.0, leave the shared validate_phase22 module untouched,
and still fail on a mismatched release.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))


def load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v23 = load('validate_phase23', 'validate_phase23_under_test')
v22 = load('validate_phase22', 'validate_phase22_reference_for_phase23')
OLD = ('v1_every_release_declaration_names_0_6_0_as_pebble_version', 'v2_the_running_service_reports_release_0_6_0')
NEW = ('v1_every_release_declaration_names_the_checkout_version_as_pebble_version',
       'v2_the_running_service_reports_the_checkout_release')


def stub(module):
    campaign = module.Campaign(['unused'], None)
    campaign.provider.stop()
    campaign.trap.server.server_close()
    return campaign


def test_the_successor_runs_phase22s_checks_with_only_the_two_version_names_renamed():
    import pebble
    m = v23.adopt()
    assert m.RELEASE == pebble.VERSION and v22.RELEASE == '0.6.0'
    assert len(m.PLANNED) == len(set(m.PLANNED)) == len(v22.PLANNED) == 46
    renamed = dict(zip(OLD, NEW))
    assert list(m.PLANNED) == [renamed.get(name, name) for name in v22.PLANNED]
    assert not set(OLD) & set(m.PLANNED) and set(NEW) <= set(m.PLANNED)
    assert {k: list(x) for k, x in m.SCENARIOS.items()} == {k: [renamed.get(n, n) for n in x] for k, x in v22.SCENARIOS.items()}


def test_adopting_is_idempotent_and_leaves_the_shared_phase22_module_alone():
    first = v23.adopt()
    second = v23.adopt()
    assert first is second and len(second.PLANNED) == 46
    shared = sys.modules.get('validate_phase22')
    if shared is not None:  # another test imported the real module: it still pins 0.6.0 and the old names
        assert shared.RELEASE == '0.6.0' and set(OLD) <= set(shared.PLANNED)


def test_static_checks_pass_on_this_checkout_under_the_new_names():
    m = v23.adopt()
    campaign = stub(m)
    campaign.static_checks()
    names = [c['name'] for c in campaign.checks]
    assert names == list(m.SCENARIOS['static']) and NEW[0] in names
    assert all(c['passed'] for c in campaign.checks), [c.get('error') for c in campaign.checks]


def test_the_release_check_still_fails_on_a_mismatched_declaration(monkeypatch):
    m = v23.adopt()
    monkeypatch.setattr(m, 'checkout_version', lambda: '0.5.0')
    campaign = stub(m)
    campaign.static_checks()
    found = {c['name']: c for c in campaign.checks}[NEW[0]]
    assert not found['passed'] and '0.5.0' in found['error']


def test_the_report_names_the_successor_among_its_sources():
    m = v23.adopt()
    digests = m.checkout_digests()
    assert 'scripts/validate_phase23.py' in digests and 'scripts/validate_phase22.py' in digests


def test_the_campaign_retires_phase22_and_runs_phase23_from_phase23_on():
    verify = load('verify_harness_candidate', 'verify_for_phase23_test')
    # Phase 24 appends 'phase24' after phase23, so phase23 is located by position (same strength).
    rank = verify.PHASES.index('phase23')
    assert verify.SUITES['phase23'] == 'validate_phase23.py' and verify.PHASES[rank - 1:rank + 1] == ('phase22', 'phase23')
    assert verify.RETIRED['phase22'][0] == 'phase23'
    assert 'phase22' in verify.default_suites('phase22') and 'phase23' not in verify.default_suites('phase22')
    later = verify.default_suites('phase23')
    assert 'phase23' in later and 'phase22' not in later and len(later) == len(verify.default_suites('phase22'))
