"""Phase 42: scripts/validate_phase42.py, 1.8.0's app control (ScreenRequest, the real AX backend, the Space anchor).

The command line passes on this checkout, and each check fails when the guarantee it holds is broken (patched in
process for one campaign run; the checkout is never edited).
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/validate_phase42.py'


def load():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('validate_phase42_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def campaign(tmp_path, monkeypatch, only, v42=None):
    v42 = v42 or load()
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setenv('HOME', os.environ.get('HOME', '/tmp'))
    run = v42.Campaign(scratch, only={'fixture_scratch_created_outside_the_checkout', *only})
    try:
        run.run()
    finally:
        run.close()
    return {r['name']: r for r in run.results}


def fails(tmp_path, monkeypatch, *names, v42=None):
    results = campaign(tmp_path, monkeypatch, set(names), v42)
    assert not results[names[-1]]['passed'], results[names[-1]]


def test_the_command_line_passes_on_this_checkout(tmp_path):
    report = tmp_path / 'v42.json'
    done = subprocess.run([sys.executable, str(SCRIPT), '--report', str(report)], capture_output=True, text=True,
                          cwd=ROOT, timeout=300, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    data = json.loads(report.read_text())
    assert done.returncode == 0 and data['passed'] is True, (done.stdout[-2000:], done.stderr[-2000:])
    assert data['checks'] == list(load().PLANNED)


def test_a_request_granted_without_review_fails_the_review_check(tmp_path, monkeypatch):
    from pebble import screen_session as ss
    real = ss.request_proposal

    def eager(app, args, **kw):
        payload = real(app, args, **kw)
        ss.grant_requested(app, app.active, app.active_run, payload)
        return payload
    monkeypatch.setattr(ss, 'request_proposal', eager)
    fails(tmp_path, monkeypatch, 'a_requested_window_is_granted_only_by_the_persons_review')


def test_screen_tools_that_still_need_ui_words_fail_the_follow_check(tmp_path, monkeypatch):
    from pebble import screen_session as ss
    real = ss.offered_tools
    monkeypatch.setattr(ss, 'offered_tools', lambda requested=False, **kw: real(**kw))
    fails(tmp_path, monkeypatch, 'a_requested_window_is_granted_only_by_the_persons_review',
          'the_screen_tools_follow_the_granted_window_without_ui_words')


def test_an_act_without_the_observed_element_fails_the_recheck(tmp_path, monkeypatch):
    from pebble.screen_session import AppAdapter

    def blind(self, target, pin, element, action, text):
        instance = self._instance(target, pin['start'])
        self.backend.perform(instance['pid'], float(instance['start']), pin['window_ref'], element['path'], action, text)
    monkeypatch.setattr(AppAdapter, 'perform', blind)
    fails(tmp_path, monkeypatch, 'a_requested_window_is_granted_only_by_the_persons_review',
          'typing_runs_and_a_press_waits_for_review_with_the_element_rechecked')


def test_operating_every_app_fails_the_never_operated_check(tmp_path, monkeypatch):
    from pebble import screen_session as ss
    monkeypatch.setattr(ss, '_refuse_unsupported', lambda target: None)
    fails(tmp_path, monkeypatch, 'never_operated_apps_are_refused_without_a_card')


def test_an_offer_without_screen_control_fails_the_offer_check(tmp_path, monkeypatch):
    from pebble import runner
    from pebble.screen_session import REQUEST_SCHEMA
    monkeypatch.setattr(runner, 'with_screen_request', lambda app, offered, ws, large: offered + [REQUEST_SCHEMA]
                        if not any(t['function']['name'] == 'ScreenRequest' for t in offered) else offered)
    fails(tmp_path, monkeypatch, 'a_requested_window_is_granted_only_by_the_persons_review',
          'the_request_is_offered_with_screen_control_in_every_conversation')


def test_a_backend_that_types_into_secure_fields_fails_the_backend_check(tmp_path, monkeypatch):
    from pebble import ax_worker
    monkeypatch.setattr(ax_worker, 'is_secure', lambda element: False)
    fails(tmp_path, monkeypatch, 'the_mac_backend_rechecks_elements_and_never_acts_on_secure_fields')


@pytest.mark.parametrize('rel, old, new, check', [
    ('src-tauri/src/main.rs', 'if full != Some(true) { create(); }', 'create();',
     'the_space_anchor_is_made_on_a_desktop_space_only'),
    ('src-tauri/src/main.rs', 'space_anchor::place(&handle, "space_changed");', '',
     'the_space_anchor_is_made_on_a_desktop_space_only'),
    ('src-tauri/src/main.rs', '"toolbox_decide","app_control_status","app_control_request"', '"toolbox_decide"',
     'the_accessibility_routes_are_main_window_only'),
    ('src/components/ScreenSessions.vue', '@click="askAccess"', '@vue:mounted="askAccess"',
     'the_accessibility_routes_are_main_window_only'),
    ('CHANGELOG.md', 'never retried', 'retried', 'release_version_is_1_8_0_and_its_notes_describe_it'),
])
def test_sources_without_the_change_fail(tmp_path, monkeypatch, rel, old, new, check):
    v42 = load()
    real = v42.read

    def edited(path):
        text = real(path)
        if path == rel:
            assert old in text, old
            return text.replace(old, new)
        return text
    monkeypatch.setattr(v42, 'read', edited)
    fails(tmp_path, monkeypatch, check, v42=v42)


def test_a_browser_that_keeps_device_pixels_fails_the_pixel_check(tmp_path, monkeypatch):
    from pebble import browser_session as bs
    monkeypatch.setattr(bs.BrowserAdapter, '_scale', lambda self, tab, doc: 1.0)
    fails(tmp_path, monkeypatch, 'the_browser_reads_pages_in_css_pixels')
