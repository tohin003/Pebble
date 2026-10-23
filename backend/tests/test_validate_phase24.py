"""Phase 24: scripts/validate_phase24.py (t24-validator), modelled on the Phase 23 pair.

- The plan: one planned check per validator_checks name of every fix and evidence track in the Phase 24 plan
  (.runtime/track-notes/t24-tracks.json, embedded below so a clean clone can run this), plus the 1.0 release identity,
  the merges' wiring and the campaign's own seals; names unique; nothing left pending a merge.
- Source and frozen modes record the same check names, and the report carries backend_sha256 and source_sha256, with
  no path under the person's home in an error or a trace.
- A failing fixture for each check family: a regression injected for one test only (a product function or constant
  set back to a 0.6.1-like behaviour, or a synthetic source) makes the family's check fail. The check is never
  weakened to pass; the shell and publish families are also exercised on synthetic sources (attribute order, the
  published tree's own scan, an installer's literal text), so these tests do not depend on today's tree.
Scratch homes only (pytest's tmp_path), the validator's own seal on top of conftest's guards, no network and no model.
"""
import importlib.util
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))


def load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v24 = load('validate_phase24', 'validate_phase24_under_test')

# The plan's validator_checks, by track (t24-tracks.json; t24-install's last entry names its parent check in brackets).
PLAN = {
    't24-auth': ['signin_failure_classes_distinct', 'signin_user_agent_pebble', 'refresh_transient_retried',
                 'refresh_invalid_grant_reconnect', 'model_401_refresh_once', 'vault_no_stale_fallback',
                 'secret_delete_removes_legacy_item', 'retry_window_survives_4s_outage', 'prompt_cache_key_chatgpt_route'],
    't24-core': ['state_query_scales', 'stop_under_1s_at_1000_chats', 'continuation_threads_survive_db_error',
                 'store_query_rolls_back', 'secure_delete_on', 'log_lines_timestamped_and_rotated'],
    't24-data': ['documents_copies_outside_tool_boundary', 'stale_memory_copy_does_not_forget',
                 'unreadable_copy_not_trashed', 'delete_removes_documents_copies', 'trashed_conversation_refuses_send',
                 'cloud_switch_requires_handoff', 'documents_filename_scrubbed', 'delete_chat_atomic',
                 'pebble_md_too_long_truncated_not_stale'],
    't24-memory': ['remember_plus_question_answered', 'remember_when_falls_through', 'person_saved_contradiction_withheld',
                   'correction_supersedes', 'delete_chat_keeps_explicit_preference',
                   'conversational_forget_card_no_model_authority'],
    't24-agent': ['screen_grant_usable_by_model', 'tools_offered_for_natural_phrasing', 'weather_route_lookup_only',
                  'standing_instructions_delivered', 'repeated_failure_loop_stopped', 'four_k_requests_unchanged',
                  'command_transcripts_not_in_history'],
    't24-gate': ['terminal_test_failure_holds', 'test_output_never_host_voiced', 'rejected_edit_not_held',
                 'guard_prompts_forgotten_with_chat', 'guard_state_released', 'gate_held_state_actions',
                 'output_checks_json_csv', 'first_request_not_blocked_by_index', 'controller_loop_survives_db_error',
                 'f8_1_settles_with_own_write'],
    't24-browser': ['chrome_grant_reaches_model', 'pending_grant_binds_next_run', 'connect_chrome_planner_two_files',
                    'secret_field_label_invisible_chars', 'cdp_isolated_world_param'],
    't24-ux-shell': ['muted_contrast_aa', 'approval_card_min_type_size', 'switch_states_exposed', 'first_run_offers_signin',
                     'paste_attaches', 'scroll_holds_position', 'pet_cannot_widen_authority'],
    't24-ux-settings': ['signin_errors_actionable', 'permission_copy_matches_behaviour', 'slash_outputs_plain',
                        'memory_list_searchable'],
    't24-build': ['heic_decoded_by_macos', 'no_gpl_lgpl_in_bundle', 'third_party_notices_complete',
                  'no_user_paths_in_binaries', 'certifi_2026_10_19', 'parser_no_url_dns_services', 'build_from_clean_clone'],
    't24-install': ['rollback_after_move_one_command', 'drill_stages_dot_pebble', 'backups_listing_readonly',
                    'helper_refuses_copied_backend (parent test-bundle check)'],
    't24-publish': ['publish_tree_clean', 'installer_safe_defaults', 'release_assets_verify'],
    't24-bench': ['frozen_bundle_has_no_bench_or_agb_module'],
}
EXTRA = {'fixture_scratch_created_outside_the_checkout', 'release_declarations_name_the_checkout_version',
         'release_version_is_1_0_or_later', 'running_service_reports_the_checkout_release',
         'phase24_routes_main_window_only_and_never_model_tools', 'no_model_tool_added_and_six_memory_layers_exact',
         'rpc_error_code_reaches_window', 'rpc_refusal_plain_words', 'signin_discovery_outside_task_lock',
         'cleanup_legacy_spares_vault_item', 'destructive_routes_need_confirmation', 'fts_forgotten_term_absent',
         'evaluation_driver_refuses_installed_connection', 'no_connection_left_this_machine',
         'backend_inputs_stable_during_campaign'}


def plan_names():
    return {re.sub(r'\s*\(.*\)$', '', name) for names in PLAN.values() for name in names}


@pytest.fixture
def campaign(tmp_path):
    """A Campaign over this checkout's backend/main.py with a fresh scratch and the validator's own seal."""
    v24.remember_private_prefixes()
    found = v24.Campaign([sys.executable, str(v24.BACKEND / 'main.py')], None)
    scratch = tmp_path / 'scratch'
    (scratch / 'person' / 'home').mkdir(parents=True)
    (scratch / 'tmp').mkdir()
    found.facts.update(scratch=scratch, person=scratch / 'person', home=scratch / 'person' / 'home', tmp=scratch / 'tmp')
    found.seal = v24.Seal(found.facts['home'], found.facts['tmp']).install()
    found.trap.start()
    try:
        yield found
    finally:
        for service in list(found.services):
            found.stop(service)
        found.trap.stop()
        found.seal.remove()


def only(campaign, *names):
    """Run just these checks of a scenario (the others are recorded as skipped, never as passes)."""
    real = v24.Campaign.check.__get__(campaign)

    def check(name, fn):
        if name in names:
            return real(name, fn)
        campaign.names.add(name)
        return None
    campaign.check = check


def result(campaign, name):
    found = [c for c in campaign.checks if c['name'] == name]
    assert len(found) == 1, (name, [c['name'] for c in campaign.checks])
    return found[0]


def failed(campaign, name, fragment=None):
    found = result(campaign, name)
    assert found['passed'] is False, (name, found.get('evidence'))
    assert fragment is None or fragment in found['error'], (name, found['error'][:400])
    return found['error']


def passed(campaign, name):
    found = result(campaign, name)
    assert found['passed'] is True, (name, found.get('error', '')[:600])
    return found['evidence']


# ---- the plan --------------------------------------------------------------------------------------------------------

def test_every_plan_check_is_planned_once_with_the_extras_named():
    planned = list(v24.PLANNED)
    assert len(planned) == len(set(planned)) == 93
    assert set(planned) == plan_names() | EXTRA and len(plan_names()) == 78
    assert set(v24.CHECKS) == set(planned) and all(len(row) == 3 for row in v24.CHECKS.values())
    assert v24.PENDING_MERGE == frozenset()  # the final merge (m24c) brought t24-ux-shell and t24-publish
    assert set(v24.SCENARIOS) <= set(v24.SCENARIO_BOUNDS) and planned[0] == 'fixture_scratch_created_outside_the_checkout'
    assert planned[-1] == 'backend_inputs_stable_during_campaign'


def test_the_embedded_plan_matches_the_track_notes_when_they_are_present():
    for candidate in (ROOT / '.runtime/track-notes/t24-tracks.json', ROOT / '.runtime/notes/t24-tracks.json'):
        if candidate.is_file():
            tracks = {t['name']: t['validator_checks'] for t in json.loads(candidate.read_text()) if t.get('validator_checks')}
            assert tracks == PLAN
            return
    pytest.skip('the plan file is not in this checkout (a clean clone)')


def test_the_campaign_registers_phase24_and_keeps_phase22_retired():
    verify = load('verify_harness_candidate', 'verify_for_phase24_test')
    # Phase 25 (the release build) follows phase24 in the campaign; phase24 stays registered and runs in it.
    assert verify.SUITES['phase24'] == 'validate_phase24.py' and verify.PHASES[-8:] == ('phase24', 'phase25', 'phase26', 'phase27', 'phase28', 'phase29', 'phase30', 'phase31')
    suites = verify.default_suites('phase24')
    assert {'phase23', 'phase24'} <= set(suites) and 'phase22' not in suites
    assert 'phase24' not in verify.default_suites('phase23')


# ---- modes and the report --------------------------------------------------------------------------------------------

def test_source_and_frozen_runs_record_the_same_check_names(campaign, tmp_path):
    campaign.scenario_static()
    source = [c['name'] for c in campaign.checks]
    frozen = v24.Campaign(campaign.command, tmp_path / 'frozen' / 'pebble-backend')
    frozen.facts, frozen.seal = dict(campaign.facts, scratch=tmp_path / 'scratch-frozen'), campaign.seal
    frozen.facts['scratch'].mkdir()
    frozen.scenario_static()
    assert frozen.frozen and not campaign.frozen
    assert [c['name'] for c in frozen.checks] == source == list(v24.SCENARIOS['static'])
    for kind_campaign in (campaign, frozen):
        kind_campaign.finish()
        assert [c['name'] for c in sorted(kind_campaign.checks, key=lambda c: v24.PLANNED.index(c['name']))] == \
            [n for n in v24.PLANNED if n != 'backend_inputs_stable_during_campaign']
    frozen.trap.server.server_close()


def test_the_report_carries_its_digests_and_names_no_private_path(campaign, tmp_path, monkeypatch):
    v24.PRIVATE_PREFIXES[:] = ['/Users/alice', str(v24.ROOT)]
    campaign.check('fixture_scratch_created_outside_the_checkout',
                   lambda: (_ for _ in ()).throw(AssertionError('/Users/alice/private/notes.md and ' + str(v24.ROOT))))
    campaign.finish()
    sources = v24.checkout_digests()
    report = v24.build_report(campaign, backend=None, backend_sha=None, sources=sources, sources_after=sources,
                              duration=1.0, scratch=tmp_path, keep=False)
    assert report['backend_sha256'] is None and report['source_sha256'] == sources and report['kind'] == 'source'
    assert sources['scripts/validate_phase24.py'] == v24.sha((SCRIPTS / 'validate_phase24.py').read_bytes())
    assert report['planned_checks'] == list(v24.PLANNED) and report['all_planned_checks_recorded'] is True
    assert report['passed'] is False and set(report['failed_pending_merge']) <= v24.PENDING_MERGE
    text = json.dumps(report)
    assert '/Users/alice' not in text and '<checkout>' in text and str(v24.ROOT) not in json.dumps(report['check_results'])
    frozen_exe = tmp_path / 'pebble-backend'
    frozen_exe.write_bytes(b'frozen fixture')
    report = v24.build_report(campaign, backend=frozen_exe, backend_sha=v24.sha(frozen_exe.read_bytes()), sources=sources,
                              sources_after=sources, duration=1.0, scratch=tmp_path, keep=False)
    assert report['kind'] == 'frozen' and report['backend_sha256'] == v24.sha(b'frozen fixture')
    changed = dict(sources, **{'scripts/validate_phase24.py': '0' * 64})
    report = v24.build_report(campaign, backend=None, backend_sha=None, sources=sources, sources_after=changed,
                              duration=1.0, scratch=tmp_path, keep=False)
    stable = [c for c in report['check_results'] if c['name'] == 'backend_inputs_stable_during_campaign'][-1]
    assert stable['passed'] is False and report['passed'] is False


def test_the_report_path_is_new_and_never_overwritten(tmp_path):
    existing = tmp_path / 'report.json'
    existing.write_text('kept')
    with pytest.raises(SystemExit):
        v24.main(['--report', str(existing)])
    assert existing.read_text() == 'kept'


# ---- one failing fixture per family ----------------------------------------------------------------------------------

def test_static_family_fails_on_a_stale_declaration_and_a_window_route_left_open(campaign, monkeypatch):
    from pebble import tool_authz
    monkeypatch.setattr(v24, 'declared_versions', lambda: {'package.json': '0.6.1-old'})
    monkeypatch.setattr(tool_authz, 'NATIVE_ONLY', frozenset(tool_authz.NATIVE_ONLY - {'run_held_accept'}))
    campaign.scenario_static()
    failed(campaign, 'release_declarations_name_the_checkout_version', 'declarations that differ')
    failed(campaign, 'phase24_routes_main_window_only_and_never_model_tools', 'run_held_accept')
    passed(campaign, 'cdp_isolated_world_param')
    passed(campaign, 'secret_field_label_invisible_chars')


def test_static_family_fails_on_an_invisible_split_secret_label(campaign, monkeypatch):
    from pebble import ax_worker
    real = ax_worker.secret_text
    monkeypatch.setattr(ax_worker, 'secret_text', lambda text: text if '⁦' in str(text) else real(text))
    only(campaign, 'secret_field_label_invisible_chars')
    campaign.scenario_static()
    failed(campaign, 'secret_field_label_invisible_chars', 'U+2066')


def test_service_family_fails_on_a_release_the_service_does_not_report(campaign, monkeypatch):
    monkeypatch.setattr(v24, 'checkout_version', lambda: '9.9.9')
    only(campaign, 'running_service_reports_the_checkout_release', 'trashed_conversation_refuses_send')
    campaign.scenario_service()
    failed(campaign, 'running_service_reports_the_checkout_release', '9.9.9')
    passed(campaign, 'trashed_conversation_refuses_send')


def test_bundle_family_fails_on_an_old_certifi_pin_and_a_heic_decoder_in_process(campaign, monkeypatch):
    real = v24.read
    monkeypatch.setattr(v24, 'read', lambda rel: real(rel).replace('certifi==2026.10.19', 'certifi==2026.7.22')
                        if rel.startswith('backend/requirements') else real(rel))
    from pebble import images
    monkeypatch.setattr(images, 'decode_heif', lambda path, runner=None, scratch=None: (_ for _ in ()).throw(
        ValueError('pillow_heif decoded it in process')))
    only(campaign, 'certifi_2026_10_19', 'heic_decoded_by_macos')
    campaign.scenario_bundle()
    failed(campaign, 'certifi_2026_10_19', 'does not pin certifi')
    failed(campaign, 'heic_decoded_by_macos')


def test_bundle_family_reads_the_frozen_candidate(campaign, tmp_path, monkeypatch):
    package = tmp_path / 'pkg'
    internal = package / '_internal'
    (internal / 'certifi').mkdir(parents=True)
    (internal / 'certifi-2026.7.22.dist-info').mkdir()
    (internal / 'certifi' / 'cacert.pem').write_text('# Issuer: CN=emSign ECC Root CA - G3\n-----BEGIN CERTIFICATE-----\n')
    # Phase 24 (parent, trial 2): the frozen scan refuses this machine's paths (the release builder's definition), so
    # the planted leak is this checkout's own path; another machine's build path is only counted (test below).
    (internal / 'libmachine.so').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00panic at ' + str(v24.ROOT).encode() + b'/src/x.rs\x00')
    with zipfile.ZipFile(internal / 'base_library.zip', 'w') as archive:
        archive.writestr('os.pyc', b'clean')
    exe = package / 'pebble-backend'
    exe.write_bytes(b'\xcf\xfa\xed\xfe frozen fixture')
    campaign.backend = exe
    monkeypatch.setattr(v24.Campaign, 'frozen_modules', lambda self: {'pebble.app', 'benchmarks.corpus_24a'})
    only(campaign, 'certifi_2026_10_19', 'no_user_paths_in_binaries', 'frozen_bundle_has_no_bench_or_agb_module')
    campaign.scenario_bundle()
    failed(campaign, 'certifi_2026_10_19', 'frozen certifi')
    failed(campaign, 'no_user_paths_in_binaries', 'libmachine.so')
    failed(campaign, 'frozen_bundle_has_no_bench_or_agb_module', 'benchmarks.corpus_24a')


def test_a_third_party_build_path_alone_is_counted_not_refused(campaign, tmp_path):
    """Phase 24 (parent, trial 2): wheels built on CI machines and python.org's framework carry their builders' home
    folders; they name no one here, so the frozen scan counts them and passes."""
    package = tmp_path / 'pkg'
    internal = package / '_internal'
    internal.mkdir(parents=True)
    other = b'/' + b'Users' + b'/ci-runner/work/wheel/src/lib.c'
    (internal / 'libwheel.so').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' + other + b'\x00')
    with zipfile.ZipFile(internal / 'base_library.zip', 'w') as archive:
        archive.writestr('os.pyc', b'clean')
    exe = package / 'pebble-backend'
    exe.write_bytes(b'\xcf\xfa\xed\xfe frozen fixture')
    campaign.backend = exe
    only(campaign, 'no_user_paths_in_binaries')
    campaign.scenario_bundle()
    [result] = [c for c in campaign.checks if c['name'] == 'no_user_paths_in_binaries']
    assert result['passed'] is True, result
    assert result['evidence']['frozen']['machine_paths'] == 0 and result['evidence']['frozen']['other_users_prefix_hits'] >= 1


def test_auth_family_fails_when_every_sign_in_failure_reads_the_same(campaign, monkeypatch):
    from pebble import chatgpt
    monkeypatch.setattr(chatgpt, 'describe', lambda code, status=None, grant=None:
                        'ChatGPT authentication request failed. ' + chatgpt.UNCHANGED)
    only(campaign, 'signin_failure_classes_distinct')
    campaign.scenario_auth()
    failed(campaign, 'signin_failure_classes_distinct', 'share a message')


def test_auth_family_fails_when_a_refresh_blip_ends_the_sign_in(campaign, monkeypatch):
    from pebble import chatgpt
    monkeypatch.setattr(chatgpt, '_renewal_failure', lambda error: chatgpt.SignInError('reconnect_required'))
    only(campaign, 'refresh_transient_retried')
    campaign.scenario_auth()
    failed(campaign, 'refresh_transient_retried')


def test_vault_family_fails_when_a_timeout_falls_back_to_the_kept_item(campaign, monkeypatch):
    from pebble import vault
    monkeypatch.setattr(vault.Vault, '_guard', lambda self, accounts, store=None: None)
    only(campaign, 'vault_no_stale_fallback')
    campaign.scenario_vault()
    failed(campaign, 'vault_no_stale_fallback')


def test_store_family_fails_without_secure_delete(campaign, monkeypatch):
    from pebble import store
    monkeypatch.setattr(store, 'SECURE_DELETE', 'OFF')
    only(campaign, 'secure_delete_on')
    campaign.scenario_store()
    failed(campaign, 'secure_delete_on', 'secure_delete is not ON')


def test_data_family_fails_when_one_absence_forgets(campaign, monkeypatch):
    from pebble import data_mirror
    monkeypatch.setattr(data_mirror, 'CONFIRM_SECONDS', 0.0)
    only(campaign, 'stale_memory_copy_does_not_forget')
    campaign.scenario_data()
    failed(campaign, 'stale_memory_copy_does_not_forget')


def test_data_family_fails_when_the_documents_copies_are_a_tool_path(campaign, monkeypatch):
    from pebble import data_home
    monkeypatch.setattr(data_home, 'tool_refused', lambda *args, **kwargs: False)
    only(campaign, 'documents_copies_outside_tool_boundary')
    campaign.scenario_data()
    failed(campaign, 'documents_copies_outside_tool_boundary')


def test_memory_family_fails_when_the_whole_message_is_saved(campaign, monkeypatch):
    from pebble import memory
    monkeypatch.setattr(memory, 'first_statement', lambda text: (text, ''))
    only(campaign, 'remember_plus_question_answered')
    campaign.scenario_memory()
    failed(campaign, 'remember_plus_question_answered')


def test_settings_family_fails_on_a_jargon_description(campaign, monkeypatch):
    from pebble import runtime_commands
    catalogue = [dict(item) for item in runtime_commands.CATALOGUE]
    catalogue[0]['description'] = 'Show the host runtime inference receipts.'
    monkeypatch.setattr(runtime_commands, 'CATALOGUE', catalogue)
    only(campaign, 'slash_outputs_plain')
    campaign.scenario_settings()
    failed(campaign, 'slash_outputs_plain', 'banned word')


def test_agent_family_fails_when_a_haiku_takes_the_weather_route(campaign, monkeypatch):
    from pebble import runner, web
    real = web.weather_intent
    loose = lambda text: real(text) or ({'city': 'Kyoto', 'day': 0} if 'weather' in text.lower() else None)  # noqa: E731
    monkeypatch.setattr(web, 'weather_intent', loose)
    monkeypatch.setattr(runner, 'weather_intent', loose)
    only(campaign, 'weather_route_lookup_only')
    campaign.scenario_agent()
    failed(campaign, 'weather_route_lookup_only')


def test_gate_family_fails_when_a_broken_json_output_passes(campaign, monkeypatch):
    from pebble import output_checks
    monkeypatch.setattr(output_checks, 'issues', lambda goal, workspace, paths: ([], None))
    only(campaign, 'output_checks_json_csv')
    campaign.scenario_gate()
    failed(campaign, 'output_checks_json_csv')


def test_browser_family_fails_when_a_pending_grant_never_reaches_its_run(campaign, monkeypatch):
    from pebble.screen_session import ScreenSessions
    monkeypatch.setattr(ScreenSessions, 'session_for_run', lambda self, run_id: None)
    only(campaign, 'pending_grant_binds_next_run')
    campaign.scenario_browser()
    failed(campaign, 'pending_grant_binds_next_run', 'did not bind')


def test_install_family_fails_when_a_listing_writes(campaign, monkeypatch):
    real = v24.script_module

    def loaded(name, alias=None):
        module = real(name, alias)
        if name == 'pebble_install':
            original = module.COMMANDS['backups']

            def writing(run):
                (run.layout.backups / 'listed.txt').write_text('a listing wrote this')
                return original(run)
            module.COMMANDS['backups'] = writing
        return module
    monkeypatch.setattr(v24, 'script_module', loaded)
    only(campaign, 'rollback_after_move_one_command', 'backups_listing_readonly')
    campaign.scenario_install()
    passed(campaign, 'rollback_after_move_one_command')
    failed(campaign, 'backups_listing_readonly', 'changed something')


def test_bench_family_fails_when_the_driver_accepts_the_apps_connection(campaign, monkeypatch):
    from pebble import evaluation
    monkeypatch.setattr(evaluation, 'require_evaluation_connection',
                        lambda metadata, ledger, installed=None: {'evaluation_connection': 'separate'})
    campaign.scenario_bench()
    failed(campaign, 'evaluation_driver_refuses_installed_connection', 'was accepted')


def test_shell_family_judges_tokens_and_type_sizes_from_the_sources(campaign, monkeypatch):
    real = v24.read
    low = (':root {\n  --bg: #f8f9f6;\n  --side: #f1f3ee;\n  --panel: #fff;\n  --text: #252c28;\n  --secondary: #4d564f;\n'
           '  --muted: #939b93;\n  --danger: #9a3f30;\n  --surface: var(--panel);\n  --soft: #f0f5ed;\n  --code: #f3f5f0;\n'
           '  --user-bubble: #eaf0e5;\n  --hover: #e9eee5;\n  --selected: #e5ebe0;\n}\n'
           ':root[data-theme="dark"] {\n  --bg: #191e1b;\n  --side: #151a17;\n  --panel: #212822;\n  --text: #e0e7de;\n'
           '  --secondary: #adb9ab;\n  --muted: #94a192;\n  --danger: #e3a38f;\n  --soft: #273425;\n  --code: #161d17;\n'
           '  --user-bubble: #2c3928;\n  --hover: #263026;\n  --selected: #293628;\n}\n')
    high = low.replace('--muted: #939b93;', '--muted: #5d665e;')
    sources = {'src/tokens.css': low, 'src/components/ActionReview.vue': '<style>.x { font-size: 9px; }</style>'}
    monkeypatch.setattr(v24, 'read', lambda rel: sources.get(rel) if rel in sources else real(rel))
    only(campaign, 'muted_contrast_aa', 'approval_card_min_type_size')
    campaign.scenario_shell()
    failed(campaign, 'muted_contrast_aa', 'light --muted on --bg')
    failed(campaign, 'approval_card_min_type_size', '9.0')
    sources.update({'src/tokens.css': high, 'src/components/ActionReview.vue': '<style>.x { font-size: 11px; }</style>'})
    campaign.checks.clear()
    campaign.names.clear()
    campaign.scenario_shell()
    passed(campaign, 'muted_contrast_aa')
    passed(campaign, 'approval_card_min_type_size')


def rerun(campaign, scenario):
    campaign.checks.clear()
    campaign.names.clear()
    scenario()


def test_shell_family_reads_switches_whatever_the_attribute_order(campaign, monkeypatch):
    real = v24.read
    row = ('<button type="button" class="setting-row" role="switch" :aria-checked="on ? \'true\' : \'false\'" '
           '@click="flip">\n  <span class="toggle" aria-hidden="true" />\n</button>\n')
    themes = ''.join('<button\n  type="button"\n  :class="{ selected: state.settings.theme === \'%s\' }"\n'
                     '  :aria-pressed="state.settings.theme === \'%s\' ? \'true\' : \'false\'"\n'
                     '  @click="theme(\'%s\')"\n>%s</button>\n' % (t, t, t, t) for t in ('light', 'dark'))
    native = '<label class="setting-row"><div>Status words</div><select aria-label="Status words"></select></label>\n'
    settings = ('<template><nav role="tablist"><button\n  role="tab"\n  :aria-selected="tab === t">T</button></nav>'
                '<div role="radiogroup"><button type="button" role="radio" :aria-checked="mode === id">M</button></div>'
                '</template>')
    sources = {'src/App.vue': '<template>' + themes + row + row.replace('type="button" class', 'class') + native
                              + '</template>',
               'src/components/AdvancedSettings.vue': settings}
    monkeypatch.setattr(v24, 'read', lambda rel: sources.get(rel) if rel in sources else real(rel))
    only(campaign, 'switch_states_exposed')
    campaign.scenario_shell()
    evidence = passed(campaign, 'switch_states_exposed')
    assert (evidence['app_switch_rows'], evidence['labelled_native_controls'], evidence['theme_pressed']) == (2, 1, 2)
    for broken in (row.replace(' role="switch"', ''), row.replace(':aria-checked', ':data-checked'),
                   '<label class="setting-row"><div>Reduce motion</div></label>\n'):
        sources['src/App.vue'] = '<template>' + themes + row + broken + '</template>'
        rerun(campaign, campaign.scenario_shell)
        failed(campaign, 'switch_states_exposed', 'without role=switch and aria-checked')
    sources['src/App.vue'] = '<template>' + themes.replace(':aria-pressed', ':data-pressed', 1) + row + '</template>'
    rerun(campaign, campaign.scenario_shell)
    failed(campaign, 'switch_states_exposed', 'aria-pressed by the current theme')
    sources['src/App.vue'] = '<template>' + themes + row + '</template>'
    sources['src/components/AdvancedSettings.vue'] = settings.replace(' :aria-checked="mode === id"', '')
    rerun(campaign, campaign.scenario_shell)
    failed(campaign, 'switch_states_exposed', 'radios with aria-checked')


def synthetic_checkout(root, files):
    """A checkout with a minimal valid publish manifest and these files (the real publish_tree builds from it)."""
    manifest = {'format': 'pebble.publish-manifest/1', 'prune': ['.runtime'],
                'include': ['README.md', 'docs/**', 'backend/tests/**', 'publish/manifest.json'],
                'exclude': [{'glob': '**/__pycache__/**', 'reason': 'Build output.'}],
                'never': [{'glob': '.runtime/**', 'reason': 'Private.'}], 'scrub': [],
                'scan': {'synthetic_user_names': ['someone'], 'reserved_email_domains': ['example.com'],
                         'allowed_emails': [], 'allowed_email_paths': []},
                'clean_clone': {'provision': [], 'deselect': []}}
    for rel, text in dict(files, **{'publish/manifest.json': json.dumps(manifest)}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_publish_family_scans_the_built_tree_with_its_own_rules(campaign, tmp_path, monkeypatch):
    files = {'README.md': 'Pebble. Write to someone@example.com.\n', 'docs/GUIDE.md': 'Uses anthropic.claude-3 models.\n',
             'backend/tests/test_fixture.py': "HOME = '/Users/someone/fixture'\n"}
    monkeypatch.setattr(v24, 'ROOT', synthetic_checkout(tmp_path / 'clean', files))
    only(campaign, 'publish_tree_clean')
    campaign.scenario_publish()
    evidence = passed(campaign, 'publish_tree_clean')
    named = {(e['rule'], e['path'], e['class'], e['value']) for e in evidence['exceptions']}
    assert ('user_path', 'backend/tests/test_fixture.py', 'synthetic fixture home', 'someone') in named
    assert '/Users/' not in json.dumps(evidence)
    assert ('email', 'README.md', 'reserved domain', None) in named
    assert ('account_folder', 'docs/GUIDE.md', 'dotted model ID', None) in named
    assert evidence['published_files'] == 4 and evidence['uploads'] == 0 and evidence['tree_equals_checkout'] is True
    # Planted at run time, never as literals: this test file is itself published.
    planted = {'user_path': {'docs/NOTES.md': 'Notes at /Users/' + 'pebble-person' + '/notes.md\n'},
               'email': {'README.md': 'Write to person' + '@' + 'mail-provider.com\n'},
               'account_folder': {'docs/SETUP.md': 'Sessions under ~/' + '.claude-' + 'work/projects\n'},
               'synthetic home outside a test file': {'docs/HOMES.md': 'A fixture home /Users/' + 'someone' + '/x\n'},
               'this_mac': {'docs/WHERE.md': None}}
    for index, (rule, extra) in enumerate(planted.items()):
        root = tmp_path / ('planted-%d' % index)
        if rule == 'this_mac':
            extra = {'docs/WHERE.md': 'Built in ' + str(root) + '/src\n'}
        monkeypatch.setattr(v24, 'ROOT', synthetic_checkout(root, dict(files, **extra)))
        rerun(campaign, campaign.scenario_publish)
        error = failed(campaign, 'publish_tree_clean', 'personal path, account folder or e-mail address')
        expected = 'user_path' if rule == 'synthetic home outside a test file' else rule
        assert "'rule': '%s'" % expected in error and 'pebble-person' not in error, (rule, error[:400])


def test_publish_family_refuses_a_builder_that_pushes(campaign, tmp_path, monkeypatch):
    real = v24.script_module

    def pushing(name, alias=None):
        module = real(name, alias)
        if name == 'publish_tree':
            def gate(out, gitleaks, report_path, runner, env):
                runner(['git', 'push', 'origin', 'main'], cwd=str(out), env=env, timeout=5)
                return {'status': 'passed'}
            module.gate_gitleaks = gate
        return module
    monkeypatch.setattr(v24, 'script_module', pushing)
    monkeypatch.setattr(v24, 'ROOT', synthetic_checkout(tmp_path / 'checkout', {'README.md': 'Pebble.\n'}))
    only(campaign, 'publish_tree_clean')
    campaign.scenario_publish()
    failed(campaign, 'publish_tree_clean', 'started a git, gh or upload process')


def test_named_addresses_are_accepted_only_in_their_own_files():
    # Joined at run time: this test file is published, and carries no address of its own.
    icon, npm, author = '128x128' + '@' + '2x.png', 'i' + '@' + 'izs.me', 'a' + '@' + 'b.org'
    assert v24.published_mail('src-tauri/tauri.conf.json', icon, '2x.png')[0] == 'file name'
    assert v24.published_mail('publish/manifest.json', icon, '2x.png')[2] == icon
    assert v24.published_mail('README.md', icon, '2x.png') is None
    assert v24.published_mail('package-lock.json', npm, 'izs.me')[0] == 'third-party text, verbatim'
    assert v24.published_mail('docs/USER_GUIDE.md', npm, 'izs.me') is None
    assert v24.published_mail('src-tauri/resources/licenses/x.txt', author, 'b.org')[0] == \
        'third-party licence text, verbatim'
    assert v24.published_mail('docs/GUIDE.md', author, 'b.org') is None
    assert v24.published_mail('docs/GUIDE.md', 'a' + '@' + 'mail.example.com', 'mail.example.com')[0] == 'reserved domain'


def test_spawn_sites_find_a_process_started_outside_the_runner():
    runner = 'import subprocess\ndef run(argv):\n    return subprocess.run(argv)\n'
    assert v24.spawn_sites(runner) == ({'run'}, True)
    sites, _ = v24.spawn_sites(runner + 'def push():\n    subprocess.run(["git", "push"])\n')
    assert sites == {'run', 'push'}
    assert v24.spawn_sites(runner + 'def main():\n    run(["gh", "release"])\n')[0] == {'run', 'run() called directly in main'}
    printed = 'def parent_commands():\n    return []\ndef main(report):\n    report["parent_commands"] = parent_commands()\n'
    assert v24.spawn_sites(printed) == (set(), True)
    assert v24.spawn_sites(printed + 'def other(runner):\n    runner(parent_commands())\n') == (set(), False)


# An installer in install.sh's shape: its one removal is D18's (the run's own mktemp folder, behind the template guard);
# it backs the existing app up, installs the verified app from its own folder and puts the backup back on failure.
SAFE_INSTALLER = r'''#!/bin/bash
# A small installer in install.sh's shape (fixture).
set -euo pipefail
APP="Pebble.app"
work=""
tmp_root=""

fail() {
  printf 'installer: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  # Only the folder mktemp made for this run.
  if [ -n "$work" ] && [ -n "$tmp_root" ] && [ -d "$work" ]; then
    case "$work" in
      "$tmp_root"/pebble-install.*) rm -rf -- "$work" ;;
    esac
  fi
}

new_backup_root() {
  local root
  root="$HOME/PebbleBackups/$(date -u +%Y%m%dT%H%M%SZ)-$1.noindex"
  mkdir -p "$HOME/PebbleBackups"
  mkdir -m 700 "$root"
  printf '%s\n' "$root"
}

download_and_verify() {
  tmp_root="${TMPDIR:-/tmp}"
  work="$(mktemp -d "$tmp_root/pebble-install.XXXXXXXX")" || fail "no temporary folder"
  curl --proto '=https' --tlsv1.2 -fsSL -o "$work/a.zip" "$1"
  shasum -a 256 -c "$work/SUMS"
  ditto -x -k "$work/a.zip" "$work/unpacked"
  codesign --verify --deep --strict "$work/unpacked/$APP" || fail "Run without sudo; nothing was installed."
}

do_install() {
  local target=/Applications existing="" backup_root=""
  [ "$(uname -m)" = arm64 ] || fail "Apple silicon only"
  [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = 0 ] || fail "not under Rosetta"
  if ps -axo comm= | grep -q '/Pebble.app/'; then fail "Quit Pebble first"; fi
  if [ -e "$target/$APP" ]; then
    existing="$target/$APP"
  fi
  download_and_verify "$URL"
  if [ -n "$existing" ]; then
    backup_root="$(new_backup_root pre-install)"
    mv "$existing" "$backup_root/Pebble.app.backup"
  fi
  if ! mv "$work/unpacked/$APP" "$target/$APP"; then
    if [ -n "$backup_root" ] && [ ! -e "$target/$APP" ]; then
      mv "$backup_root/Pebble.app.backup" "$target/$APP" || true
    fi
    fail "Could not install; the previous app was put back."
  fi
}

case "${1:-}" in --dry-run) exit 0 ;; esac
trap cleanup EXIT
do_install
'''
END = SAFE_INSTALLER.count('\n') + 1  # the line of a command appended to the fixture


def test_the_installer_check_reads_install_sh_and_names_d18(campaign, tmp_path, monkeypatch):
    root = tmp_path / 'tree'
    root.mkdir()
    (root / 'install.sh').write_text(SAFE_INSTALLER + 'sudo mv "$work/unpacked/$APP" "$target/$APP"\n')
    monkeypatch.setattr(v24, 'ROOT', root)
    only(campaign, 'installer_safe_defaults')
    campaign.scenario_publish()
    failed(campaign, 'installer_safe_defaults', 'line %d: sudo' % END)
    (root / 'install.sh').write_text(SAFE_INSTALLER)
    rerun(campaign, campaign.scenario_publish)
    evidence = passed(campaign, 'installer_safe_defaults')
    assert evidence['decision'].startswith('D18: the installer may remove the temporary folder it created')
    assert evidence['allowed_removal'] == {'line': 17, 'command': 'rm -rf -- "$work"', 'variable': 'work',
                                           'made_by': 'mktemp -d "$tmp_root/pebble-install.XXXXXXXX"',
                                           'guard': '$tmp_root/pebble-install.*'}
    assert evidence['moves'] == {'back_up': 1, 'install': 1, 'put_back': 1} and evidence['other_removals'] == 0


@pytest.mark.parametrize('script, reason', [
    (SAFE_INSTALLER + 'rm -rf "$work"\n',
     'line %d: rm: unguarded: not inside a case arm matching the variable against its mktemp template' % END),
    (SAFE_INSTALLER + 'rm -rf "$HOME/.pebble"\n', 'line %d: rm: removes something other than one quoted variable' % END),
    (SAFE_INSTALLER + '/bin/rm -f "$HOME/.pebble/settings.json"\n', 'line %d: rm: removes something other' % END),
    (SAFE_INSTALLER.replace('case "$work" in', 'case "$dir" in').replace('rm -rf -- "$work"', 'rm -rf -- "$dir"')
     .replace('work=""\n', 'work=""\ndir="$HOME/Library/Caches"\n', 1),
     'rm: removes through a variable not made by mktemp -d'),
    (SAFE_INSTALLER + 'work="$HOME"\n', 'line 17: rm: removes through a variable not made by mktemp -d'),
    (SAFE_INSTALLER.replace('"$tmp_root"/pebble-install.*)', '"$tmp_root"/*)'), 'line 17: rm: unguarded'),
    (SAFE_INSTALLER.replace('rm -rf -- "$work" ;;', 'rm -rf -- $work ;;'), 'line 17: rm: the removed variable is not quoted'),
    (SAFE_INSTALLER + 'tidy() {\n  case "$work" in\n    "$tmp_root"/pebble-install.*) rm -rf -- "$work" ;;\n  esac\n}\n',
     'line %d: rm: a second removal (D18 allows one)' % (END + 2)),
    (SAFE_INSTALLER + 'rmdir "$HOME/PebbleBackups"\n', 'line %d: rmdir: a removal command D18 does not allow' % END),
    (SAFE_INSTALLER + 'unlink "$HOME/.pebble/settings.json"\n', 'line %d: unlink: a removal command' % END),
    (SAFE_INSTALLER + 'srm -r "$work"\n', 'line %d: srm: a removal command' % END),
    (SAFE_INSTALLER + 'trash "$HOME/.pebble"\n', 'line %d: trash: a removal command' % END),
    (SAFE_INSTALLER + "osascript -e 'tell application \"Finder\" to delete POSIX file \"/Applications/Pebble.app\"'\n",
     'line %d: osascript: a removal command' % END),
    (SAFE_INSTALLER + 'find "$HOME/.pebble" -name "*.log" -delete\n', 'line %d: find -delete' % END),
    (SAFE_INSTALLER.replace('mv "$existing" "$backup_root/Pebble.app.backup"', 'mv "$existing" "$HOME/.Trash/Pebble.app"'),
     'mv: moves something other than a Pebble.app into ~/PebbleBackups'),
    (SAFE_INSTALLER + 'mv "$HOME/.pebble" "$work/old"\n', 'line %d: mv: moves something other' % END),
    (SAFE_INSTALLER.replace('  download_and_verify "$URL"\n', '').replace(
        '    fail "Could not install; the previous app was put back."\n  fi\n}',
        '    fail "Could not install; the previous app was put back."\n  fi\n  download_and_verify "$URL"\n}'),
     'mv: installs before the SHA-256 and codesign checks'),
    (SAFE_INSTALLER + 'xattr -dr com.apple.quarantine "$target/$APP"\n', "line %d: xattr: xattr outside the run's own" % END),
    (SAFE_INSTALLER + 'id="$(sudo id -u)"\n', 'line %d: sudo' % END),
    (SAFE_INSTALLER + "echo 'one'; kill \"$pid\"\n", 'line %d: kill' % END),
    (SAFE_INSTALLER.replace("--proto '=https' ", ''), 'missing: https only'),
], ids=['unguarded', 'home', 'bin-rm', 'other-variable', 'work-reassigned', 'guard-mismatch', 'unquoted', 'second',
        'rmdir', 'unlink', 'srm', 'trash', 'osascript', 'find-delete', 'mv-to-trash', 'mv-data', 'install-unverified',
        'xattr-outside', 'sudo', 'kill', 'http'])
def test_every_other_removal_move_or_escalation_fails(script, reason):
    _, problems = v24.installer_review(script)
    assert any(reason in problem for problem in problems), problems


def test_words_in_messages_comments_and_heredocs_are_not_commands_and_own_folder_xattr_passes():
    worded = SAFE_INSTALLER + ('printf \'%s\\n\' "Nothing was deleted; this never runs rm -rf, sudo or kill."  # it\'s fine\n'
                               "usage() {\n  cat <<'EOF'\nNever run this with sudo, kill, rm -rf or mv.\nEOF\n}\n")
    assert v24.installer_review(worded)[1] == []
    inside = SAFE_INSTALLER.replace('  codesign --verify',
                                    '  xattr -dr com.apple.quarantine "$work/unpacked/$APP"\n  codesign --verify')
    evidence, problems = v24.installer_review(inside)
    assert problems == [] and evidence['xattr_calls'] == 1


# ---- the whole campaign -----------------------------------------------------------------------------------------------

# Checks allowed to fail here: none. The parent's steps (the 1.0.0 version, the regenerated notices) are done and the
# installer finding was decided (D18), so on the final tree every check passes; a failure is a finding or a validator
# bug, and this test reports which.
PARENT_STEPS = frozenset()
KNOWN_FINDINGS = frozenset()


def test_the_whole_source_campaign_runs_within_the_verifier_budget(tmp_path):
    report_path = tmp_path / 'report.json'
    out = io.StringIO()
    stdout, sys.stdout = sys.stdout, out
    try:
        code = v24.main(['--report', str(report_path)])
    finally:
        sys.stdout = stdout
    report = json.loads(report_path.read_text())
    assert report['kind'] == 'source' and report['all_planned_checks_recorded'] is True and report['fatal_error'] is None
    assert report['duration_seconds'] < v24.BUDGET_SECONDS and report['planned_checks'] == list(v24.PLANNED)
    unexpected = sorted(set(report['failed']) - v24.PENDING_MERGE - PARENT_STEPS - KNOWN_FINDINGS)
    assert not unexpected, {c['name']: c.get('error', '')[:300] for c in report['check_results'] if c['name'] in unexpected}
    crashed = [c['name'] for c in report['check_results'] if not c['passed']
               and not c.get('error', '').startswith('AssertionError')]
    assert not crashed, 'a failing check failed by a validator error, not a finding: %s' % crashed
    assert (code == 0) is report['passed'] and len(report['checks']) + len(report['failed']) == len(v24.PLANNED)
    text = json.dumps({k: v for k, v in report.items() if k not in ('source_sha256',)})
    assert not re.search(r'/Users/(?!<name>)[^/\s"\\]+', text) and str(Path.home()) not in text, 'a path under a home'
