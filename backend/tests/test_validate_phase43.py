"""Phase 43: scripts/validate_phase43.py, 1.9.0's M1 (window numbers, bringing a window forward, Chromium page
accessibility, the observation envelope and typed results, the capability line, the worker's protocol 2).

The command line passes on this checkout, and each check fails when the guarantee it holds is broken (patched in
process for one campaign run; the checkout is never edited).
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/validate_phase43.py'


def load():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('validate_phase43_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def campaign(tmp_path, monkeypatch, only, v43=None):
    v43 = v43 or load()
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setenv('HOME', os.environ.get('HOME', '/tmp'))
    run = v43.Campaign(scratch, only={'fixture_scratch_created_outside_the_checkout', *only})
    try:
        run.run()
    finally:
        run.close()
    return {r['name']: r for r in run.results}


def fails(tmp_path, monkeypatch, *names, v43=None):
    results = campaign(tmp_path, monkeypatch, set(names), v43)
    assert not results[names[-1]]['passed'], results[names[-1]]


def test_the_command_line_passes_on_this_checkout(tmp_path):
    report = tmp_path / 'v43.json'
    done = subprocess.run([sys.executable, str(SCRIPT), '--report', str(report)], capture_output=True, text=True,
                          cwd=ROOT, timeout=300, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    data = json.loads(report.read_text())
    assert done.returncode == 0 and data['passed'] is True, (done.stdout[-2000:], done.stderr[-2000:])
    assert data['checks'] == list(load().PLANNED)


def test_a_request_that_names_no_number_fails_the_number_check(tmp_path, monkeypatch):
    from pebble import screen_session as ss
    real = ss.request_proposal

    def titled(app, args, **kw):
        payload = real(app, args, **kw)
        payload['window'].pop('window_number', None)
        return payload
    monkeypatch.setattr(ss, 'request_proposal', titled)
    fails(tmp_path, monkeypatch, 'a_window_is_named_by_its_number_and_survives_a_title_change')


def test_reading_a_minimized_window_without_asking_fails_the_forward_check(tmp_path, monkeypatch):
    from pebble.screen_session import ScreenSessions
    monkeypatch.setattr(ScreenSessions, '_needs_bring_forward', staticmethod(lambda row, snap: False))
    fails(tmp_path, monkeypatch, 'a_minimized_or_off_desktop_window_comes_forward_after_one_review')


def test_asking_again_after_the_approval_fails_the_forward_check(tmp_path, monkeypatch):
    from pebble.screen_session import ScreenSessions
    real = ScreenSessions.raise_window

    def forgetful(self, session_id):
        result = real(self, session_id)
        self.runtime[session_id].pop('raise_approved', None)
        return result
    monkeypatch.setattr(ScreenSessions, 'raise_window', forgetful)
    fails(tmp_path, monkeypatch, 'a_minimized_or_off_desktop_window_comes_forward_after_one_review')


def test_never_restoring_page_accessibility_fails_the_chromium_check(tmp_path, monkeypatch):
    from pebble.screen_session import ScreenSessions
    monkeypatch.setattr(ScreenSessions, '_restore_ended', lambda self: None)
    fails(tmp_path, monkeypatch, 'a_chromium_app_is_enhanced_once_and_restored_at_the_last_end')


def test_switching_every_app_fails_the_chromium_check(tmp_path, monkeypatch):
    from pebble import screen_session as ss
    monkeypatch.setattr(ss, 'chromium_family', lambda path: True)
    fails(tmp_path, monkeypatch, 'a_chromium_app_is_enhanced_once_and_restored_at_the_last_end')


def test_an_unversioned_envelope_fails_the_envelope_check(tmp_path, monkeypatch):
    from pebble import screen_session as ss
    monkeypatch.setattr(ss, 'ENVELOPE_VERSION', 1)
    fails(tmp_path, monkeypatch, 'a_window_is_named_by_its_number_and_survives_a_title_change',
          'observations_carry_the_envelope_and_acts_their_typed_results')


def test_a_missing_capability_line_fails_its_check(tmp_path, monkeypatch):
    from pebble import runner
    monkeypatch.setattr(runner, 'announce_capabilities', lambda app, messages, run_id, offered: messages)
    fails(tmp_path, monkeypatch, 'a_window_is_named_by_its_number_and_survives_a_title_change',
          'the_capability_line_states_host_facts_beside_the_screen_tools')


def test_a_worker_without_window_numbers_fails_the_protocol_check(tmp_path, monkeypatch):
    from pebble.ax_worker import MacBackend
    monkeypatch.setattr(MacBackend, 'window_numbers', property(lambda self: False))
    fails(tmp_path, monkeypatch, 'the_worker_speaks_protocol_2_and_its_switch_restores_1_8_0')


def test_a_search_that_never_asks_fails_the_site_search_check(tmp_path, monkeypatch):
    from pebble import open_url
    monkeypatch.setattr(open_url, 'own_words', lambda query, person_text: True)
    fails(tmp_path, monkeypatch, 'a_site_search_opens_the_sites_own_results_and_asks_unless_the_words_are_the_persons')


def test_a_private_read_that_leaves_no_taint_fails_the_verbs_check(tmp_path, monkeypatch):
    from pebble import app_actions
    monkeypatch.setattr(app_actions, 'private_texts', lambda result: [])
    fails(tmp_path, monkeypatch, 'app_verbs_read_at_once_taint_the_run_and_a_shortcut_waits_for_review')


def test_a_shortcut_run_without_review_fails_the_verbs_check(tmp_path, monkeypatch):
    from pebble import app_actions
    real = app_actions.proposal
    monkeypatch.setattr(app_actions, 'proposal', lambda args, **kw: {**real(args, **kw), 'requires_review': False})
    fails(tmp_path, monkeypatch, 'app_verbs_read_at_once_taint_the_run_and_a_shortcut_waits_for_review')


def test_a_script_with_a_shell_call_fails_the_argv_check(tmp_path, monkeypatch):
    from pebble import app_actions
    app, klass, needs, may, script, limit = app_actions.VERBS['notes.search']
    verbs = {**app_actions.VERBS, 'notes.search': (app, klass, needs, may, script.replace(
        'return out', 'do shell script "true"\n  return out'), limit)}
    monkeypatch.setattr(app_actions, 'VERBS', verbs)
    fails(tmp_path, monkeypatch, 'the_scripting_verbs_carry_arguments_in_argv_and_never_shell_ui_or_javascript')


def test_restoring_the_front_app_after_the_persons_input_fails_the_background_check(tmp_path, monkeypatch):
    from pebble import native_apps
    real = native_apps.watch_focus
    monkeypatch.setattr(native_apps, 'watch_focus', lambda *a, **kw: real(*a, **{**kw, 'idle': lambda: 10 ** 6}))
    fails(tmp_path, monkeypatch, 'an_app_opened_in_the_background_is_put_back_only_when_the_jump_was_pebbles')


def test_a_browser_grant_made_before_review_fails_its_check(tmp_path, monkeypatch):
    from pebble import screen_session as ss
    real = ss.browser_request_proposal

    def eager(app, args):
        payload = real(app, args)
        ss.grant_browser_requested(app, app.active, app.active_run, payload)
        return payload
    monkeypatch.setattr(ss, 'browser_request_proposal', eager)
    fails(tmp_path, monkeypatch, 'a_browser_request_is_granted_only_by_review_and_opens_pebbles_own_tab')


def test_handing_back_a_read_site_fails_the_read_site_check(tmp_path, monkeypatch):
    from pebble import screen_session as ss
    real = ss.handoff_triggers
    monkeypatch.setattr(ss, 'handoff_triggers', lambda kind, target, window, elements: real(
        kind, {k: v for k, v in target.items() if k != 'read'}, window, elements))
    fails(tmp_path, monkeypatch, 'a_read_site_is_observed_and_never_acted_on')


def test_writing_every_browser_fails_the_connect_check(tmp_path, monkeypatch):
    from pebble import native_host_setup as setup
    real = setup.chosen
    monkeypatch.setattr(setup, 'chosen', lambda browsers: real(['chrome', 'comet']))
    fails(tmp_path, monkeypatch, 'connect_a_browser_writes_only_the_ticked_browsers')


def test_a_host_still_on_protocol_one_fails_the_protocol_check(tmp_path, monkeypatch):
    from pebble import browser_bridge as bb
    monkeypatch.setattr(bb, 'PROTOCOL', 'pebble-browser/1')
    fails(tmp_path, monkeypatch, 'host_native_host_and_extension_speak_pebble_browser_2')


def test_release_notes_that_claim_less_review_fail_the_version_check(tmp_path, monkeypatch):
    v43 = load()
    real = v43.v42.read

    def edited(path):
        text = real(path)
        return text.replace('still waits for you,\n  in every mode', 'is routine') if path == 'CHANGELOG.md' else text
    monkeypatch.setattr(v43.v42, 'read', edited)
    fails(tmp_path, monkeypatch, 'release_version_is_1_9_0_and_its_notes_describe_it', v43=v43)
