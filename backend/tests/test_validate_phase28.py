"""Phase 28: scripts/validate_phase28.py, the thinking binding fix and the harness fixes (source and frozen, the same
names).

The real checkout passes; each check fails on a fixture that restores the defect it guards (a provider that sends no
binding, a frozen archive without it, a gateway that receives it, the old question detector, a key read without a
terminal, a relative evaluation home, a finish that accepts two profiles, a 1.0.1 version), so no check passes
vacuously.
"""
import importlib.util
import json
import re
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase28_under_test', ROOT / 'scripts/validate_phase28.py')
v28 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase28_under_test'] = v28
spec.loader.exec_module(v28)
corpus, bp, cc, cx = v28.bench()


def campaign(tmp_path, backend=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v28.Campaign(scratch, backend)
    run.run()
    return {r['name']: r for r in run.results}


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v28.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v28.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v28.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v28.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v28_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v28.READS) <= set(vhc.sources()) and vhc.SUITES['phase28'] == 'validate_phase28.py'


def test_a_provider_that_sends_no_binding_fails(tmp_path, monkeypatch):
    from pebble import providers
    monkeypatch.setattr(providers, 'urlsplit',
                        lambda url: urllib.parse.urlsplit(url.replace('api.anthropic.com', 'gateway.example.test')))
    assert not campaign(tmp_path)['anthropic_thinking_requests_ask_to_drop_a_stale_block']['passed']


def frozen_fixture(tmp_path):
    executable = tmp_path / 'dist/pebble-backend/pebble-backend'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'frozen')
    return executable


def test_a_frozen_archive_without_the_binding_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(v28, 'frozen_module', lambda path, name: b'cache_control ephemeral api.anthropic.com')
    assert not campaign(tmp_path, frozen_fixture(tmp_path))['anthropic_thinking_requests_ask_to_drop_a_stale_block']['passed']


def test_a_gateway_that_receives_the_binding_fails(tmp_path, monkeypatch):
    real = v28.AnthropicFixture

    class Marked(real):
        def close(self):
            super().close()
            with self.lock:
                for record in self.records:
                    record['headers']['anthropic-beta'] = v28.BETA
    monkeypatch.setattr(v28, 'AnthropicFixture', Marked)
    assert not campaign(tmp_path)['gateways_and_requests_without_thinking_are_unchanged']['passed']


def test_the_old_question_detector_fails(tmp_path, monkeypatch):
    def old(reply):
        if not isinstance(reply, str) or re.search(r'^[\s>*_`#-]*FINAL\s*:', reply, re.I | re.M):
            return False
        lines = [line.strip() for line in reply.strip().splitlines() if line.strip()]
        return any(line.endswith('?') for line in lines[-2:])
    monkeypatch.setattr(cx, 'asks_question', old)
    monkeypatch.setattr(cc, 'asks_question', old)
    assert not campaign(tmp_path)['a_reply_that_asks_gets_its_follow_up']['passed']


def test_a_key_read_without_a_terminal_or_a_relative_home_fails(tmp_path, monkeypatch):
    real = v28.load_script

    def loose(name, rel):
        module = real(name, rel)
        if rel == 'scripts/bench_claude.py':
            module.hidden_key = lambda prompt, *, terminal=None, read=None: (read or (lambda p: ''))(prompt)
        return module
    monkeypatch.setattr(v28, 'load_script', loose)
    assert not campaign(tmp_path)['the_key_is_read_only_on_a_terminal_and_the_home_is_absolute']['passed']

    def relative(name, rel):
        module = real(name, rel)
        if rel == 'scripts/bench_claude.py':
            module._provider_paths = lambda home, ledger, metadata: (Path(home), Path(metadata))
        return module
    monkeypatch.setattr(v28, 'load_script', relative)
    assert not campaign(tmp_path / 'b')['the_key_is_read_only_on_a_terminal_and_the_home_is_absolute']['passed']


def test_a_finish_that_accepts_two_profiles_fails(tmp_path, monkeypatch):
    real = v28.load_script

    def lax(name, rel):
        module = real(name, rel)
        if rel == 'scripts/bench_claude.py':
            original = module._record_saved

            def first(state, **kwargs):
                return original({'profiles': (state.get('profiles') or [])[:1]}, **kwargs)
            module._record_saved = first
        return module
    monkeypatch.setattr(v28, 'load_script', lax)
    assert not campaign(tmp_path)['an_interrupted_key_entry_is_finished_without_the_key']['passed']


@pytest.mark.parametrize('version', ['1.0.1', '1.0.0'])
def test_a_version_not_after_1_0_1_fails(tmp_path, monkeypatch, version):
    monkeypatch.setattr(v28, 'checkout_version', lambda: version)
    assert not campaign(tmp_path)['release_version_is_after_1_0_1']['passed']
