"""Phase 27: scripts/validate_phase27.py, the prompt-caching fix and the Claude Code arm's isolation (source and frozen,
the same names).

The real checkout passes; each check fails on a fixture that breaks it (a provider that drops the marker, a frozen
archive without it, a gateway that receives one, a lax isolation check, a scratch home that links the whole home, a
quota that never waits, a restore that would take the person's folders, a 1.0.1 version), so no check passes vacuously.
"""
import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase27_under_test', ROOT / 'scripts/validate_phase27.py')
v27 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase27_under_test'] = v27
spec.loader.exec_module(v27)
cc = v27.claude_cell()


def campaign(tmp_path, backend=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    run = v27.Campaign(scratch, backend)
    run.run()
    return {r['name']: r for r in run.results}


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v27.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v27.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v27.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v27.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v27_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v27.READS) <= set(vhc.sources()) and vhc.SUITES['phase27'] == 'validate_phase27.py'


def test_a_provider_that_drops_the_marker_fails(tmp_path, monkeypatch):
    from pebble import providers
    # The pre-Phase 27 behaviour: Anthropic's host is treated as any gateway.
    monkeypatch.setattr(providers, 'urlsplit',
                        lambda url: urllib.parse.urlsplit(url.replace('api.anthropic.com', 'gateway.example.test')))
    assert not campaign(tmp_path)['anthropic_api_requests_carry_one_cache_marker']['passed']


def frozen_fixture(tmp_path):
    executable = tmp_path / 'dist/pebble-backend/pebble-backend'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'frozen')
    return executable


def test_a_frozen_archive_without_the_marker_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(v27, 'frozen_module', lambda path, name: b'anthropic-version 2023-06-01 /messages')
    assert not campaign(tmp_path, frozen_fixture(tmp_path))['anthropic_api_requests_carry_one_cache_marker']['passed']


def test_a_frozen_archive_with_the_marker_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(v27, 'frozen_module', lambda path, name: b'..cache_control..ephemeral..api.anthropic.com..')
    result = campaign(tmp_path, frozen_fixture(tmp_path))['anthropic_api_requests_carry_one_cache_marker']
    assert result['passed'] and result['evidence']['frozen_providers_module']['carries'] == [
        'cache_control', 'ephemeral', 'api.anthropic.com']


def test_a_gateway_that_receives_a_marker_fails(tmp_path, monkeypatch):
    real = v27.AnthropicFixture

    class Marked(real):
        def close(self):
            super().close()
            with self.lock:
                for record in self.records:
                    record['body']['cache_control'] = {'type': 'ephemeral'}
    monkeypatch.setattr(v27, 'AnthropicFixture', Marked)
    assert not campaign(tmp_path)['compatible_gateways_get_no_cache_marker']['passed']


@pytest.mark.parametrize('name, replacement', [
    ('isolation_breaches', lambda *a, **k: []),
    ('personal_names', lambda config: frozenset()),
    ('sandbox_profile', lambda config: '(version 1)(allow default)'),
])
def test_a_lax_isolation_fails(tmp_path, monkeypatch, name, replacement):
    monkeypatch.setattr(cc, name, replacement)
    assert not campaign(tmp_path)['claude_code_cells_isolated_on_the_existing_login']['passed']


def test_a_scratch_home_that_links_the_whole_home_fails(tmp_path, monkeypatch):
    def whole(home):
        home.parent.mkdir(parents=True, exist_ok=True)
        home.symlink_to(Path.home())
        return home
    monkeypatch.setattr(cc, 'scratch_home', whole)
    assert not campaign(tmp_path)['claude_code_cells_isolated_on_the_existing_login']['passed']


def test_a_quota_that_never_waits_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, 'quota_wait', lambda rate, now=None: 0)
    assert not campaign(tmp_path)['quota_paces_and_stops_claude_code_cells']['passed']


def test_a_restore_that_would_take_the_persons_folders_fails(tmp_path, monkeypatch):
    import re
    monkeypatch.setattr(cc, 'RESIDUE', re.compile(r'-private-tmp-pebble-[A-Za-z0-9-]*'))
    assert not campaign(tmp_path)['restore_removes_only_benchmark_residue']['passed']


@pytest.mark.parametrize('version', ['1.0.1', '1.0.0'])
def test_a_version_not_after_1_0_1_fails(tmp_path, monkeypatch, version):
    monkeypatch.setattr(v27, 'checkout_version', lambda: version)
    assert not campaign(tmp_path)['release_version_is_after_1_0_1']['passed']
