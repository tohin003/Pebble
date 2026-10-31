"""Phase 46 (1.9.29, D214, D215): what the iPhone may ask of Pebble, and what each call needs.

Contract PEBBLE_REMOTE_BRIDGE sections 3.4, 3.6, 3.7 and 3.11 (v0.3), with proposal v0.5 (claude-w's answer of
2026-10-30 10:53). The person's R2 and R6: the phone does everything the main window does, except what needs the Mac's
own files and pickers, the person's sign-ins, or installing code.

- PHONE_ALLOWED = ALLOWED - PHONE_EXCLUDED. ALLOWED is the main window's list in src-tauri/src/main.rs, copied here;
  test_remote_policy_46 fails when the copy and main.rs differ. The shell's NATIVE_ONLY never reaches a window, so it
  never reaches the phone either.
- MAC_ROUTES are Pebble Remote's own controls in Settings > iPhone (turning it on, pairing, confirming a pairing,
  removing a phone). They are the main window's and never the phone's: a phone that could pair or confirm a pairing
  would make the Mac's confirmation (X-1) mean nothing.
- Elevated calls carry a Face ID proof (3.6); every other call that is not a read needs the unlock (C1) and an `idem`
  (v0.3 item 4). A `setting` key is classified here, and an unknown key needs a proof (fail closed).

Nothing here is a model tool, and nothing here changes what a model sees.
"""

import re

# src-tauri/src/main.rs ALLOWED, in its order (test_remote_policy_46 compares the two). Phase 46 (1.9.29) adds
# MAC_ROUTES at its end, main window only.
ALLOWED = (
    'state', 'new_chat', 'get_chat', 'set_workspace', 'send', 'cancel', 'model_load', 'model_unload', 'set_model',
    'approve', 'reject', 'apps', 'attach', 'attach_url', 'attach_bytes', 'remove_source', 'delete_chat', 'chat_rename',
    'chat_search', 'setting', 'open_app', 'undo', 'model_profiles', 'model_discover', 'model_save', 'model_save_many',
    'model_select', 'model_effort', 'model_remove', 'memory_list', 'memory_save', 'memory_delete', 'memory_clear',
    'memory_candidates', 'memory_maintain', 'memory_resolve', 'memory_export', 'extensions', 'skill_inspect',
    'hook_events', 'extension_add', 'extension_import', 'extension_toggle', 'extension_remove', 'media_artifact',
    'media_download', 'plugin_package_preview', 'plugin_package_import', 'plugin_package_install',
    'plugin_package_update', 'plugin_package_history', 'plugin_package_inspect', 'plugin_package_preview_rollback',
    'plugin_package_rollback', 'connector_discover_interop', 'connector_preview_registration',
    'connector_register_client', 'connector_metadata', 'connector_discover', 'connector_preview', 'connector_start',
    'connector_status', 'connector_cancel', 'connector_save_bearer', 'connector_save_environment',
    'connector_disconnect', 'terminal_propose', 'open_url', 'chatgpt_start', 'chatgpt_status', 'chatgpt_cancel',
    'chatgpt_sign_out', 'view_stage', 'view_stop', 'artifact_get', 'artifact_history', 'artifact_submit',
    'artifact_stop', 'artifact_view_open', 'mods', 'mod_example', 'mod_preview', 'mod_install', 'mod_toggle',
    'mod_remove', 'run_list', 'run_get', 'run_activity', 'run_handoff', 'run_compact', 'run_context_archive',
    'search_stats', 'run_events', 'stream_read', 'process_list', 'process_read', 'process_stop', 'process_inspect',
    'process_dismiss', 'run_pause', 'run_resume', 'run_held_tests', 'run_held_finish', 'run_held_accept',
    'run_reconcile', 'run_readback', 'upgrade_backups', 'upgrade_discard_backup', 'upgrade_retry', 'process_terminal',
    'process_input_human', 'plan_credentials', 'plan_credential_save', 'plan_credential_delete', 'memory_contests',
    'memory_resolve_contest', 'memory_relations', 'memory_forget_preview', 'memory_archive_export_to',
    'memory_archive_preview_file', 'memory_archive_import_file', 'model_handoff_preview', 'model_handoff_approve',
    'model_handoff_start', 'connector_save_environment_route', 'connector_send_confirm', 'research_sources',
    'screen_windows', 'screen_session_grant', 'screen_sessions', 'screen_handoff_done', 'screen_session_revoke',
    'browser_status', 'browser_start', 'browser_stop', 'browser_tabs', 'browser_open_tab', 'browser_profile_forget',
    'command_catalogue', 'run_steer', 'question_answer', 'question_cancel', 'agent_list', 'agent_get', 'agent_steer',
    'agent_cancel', 'job_list', 'job_get', 'job_preview', 'job_create', 'job_pause', 'job_preview_resume',
    'job_resume', 'job_delete', 'notification_ack', 'git_capabilities', 'git_identity', 'git_status', 'git_diff',
    'git_history', 'git_worktrees', 'git_inspect', 'git_preview_create', 'git_create_worktree', 'git_preview_remove',
    'git_remove_worktree', 'git_forget_worktree', 'git_export', 'git_export_file', 'git_plan_propose',
    'git_plan_receipts', 'git_plan_readback', 'agent_worktrees', 'agent_worktree_retire', 'agent_worktree_reconcile',
    'project_recipes', 'environment_list', 'environment_plan', 'environment_remove', 'notification_status',
    'notification_mute', 'goal_list', 'goal_get', 'goal_create', 'goal_state', 'goal_criterion_add',
    'goal_criterion_edit', 'goal_criterion_check', 'goal_criterion_review', 'goal_step_add', 'goal_step_edit',
    'goal_step_block', 'goal_blockers', 'goal_continue', 'export_preview', 'export_render', 'export_cancel',
    'export_save', 'export_list', 'export_get', 'export_discard', 'telemetry_get', 'telemetry_set', 'telemetry_export',
    'telemetry_clear', 'artifact_refresh', 'quiz_attempt', 'quiz_start', 'quiz_save', 'quiz_submit', 'quiz_share',
    'rewind_preview', 'rewind_apply', 'context_inspect', 'context_compact', 'browser_extension_status',
    'browser_extension_pause', 'browser_extension_resume', 'browser_extension_stop', 'browser_extension_unpair',
    'storage_summary', 'storage_reveal', 'storage_delete', 'storage_set', 'storage_settings_resolve',
    'storage_restore_chat', 'storage_purge_chat', 'storage_refresh', 'storage_trash_chat', 'storage_trash_all',
    'storage_purge_all', 'storage_forget_resolve', 'storage_cleanup_legacy', 'browser_connect_status',
    'browser_connect_install', 'shell_grants', 'shell_grant_add', 'shell_grant_remove', 'shell_undo', 'toolbox_list',
    'toolbox_get', 'toolbox_remove', 'toolbox_decide', 'app_control_status', 'app_control_request', 'approve_simple',
    'reject_simple', 'toolbox_undo_remove',
    'remote_status', 'remote_set', 'remote_pair_start', 'remote_pair_cancel', 'remote_pair_confirm',
    'remote_device_remove')
# main.rs NATIVE_ONLY: routes only the shell itself calls (never a window, so never the phone).
SHELL_NATIVE_ONLY = frozenset({
    'lifecycle_event', 'lifecycle_quit_summary', 'notification_native_pending', 'notification_native_delivered',
    'export_destination_bind', 'export_native_ready', 'export_pdf_attach', 'export_pdf_failed',
    'telemetry_destination_bind'})
# Pebble Remote's own controls (Settings > iPhone): main window only, never the pet, never the phone.
MAC_ROUTES = ('remote_status', 'remote_set', 'remote_pair_start', 'remote_pair_cancel', 'remote_pair_confirm',
              'remote_device_remove')

# v0.5 item 3: opening things on the Mac's screen, allowed with a proof on every call.
OPEN_ON_MAC = ('open_url', 'open_app', 'view_stage', 'artifact_view_open', 'browser_start', 'browser_open_tab',
               'quiz_share', 'terminal_propose', 'process_terminal')
# Section 3.7 as v0.5 leaves it: the Mac's files and pickers, sign-ins and credentials, installing code, Mac
# maintenance and the browser profile; every connector_* but connector_status and every plugin_package_* (below); and
# Pebble Remote's own controls.
PHONE_EXCLUDED = frozenset({
    # local files and pickers (set_workspace with a folder no conversation uses is decided by needs_proof and policy)
    'attach', 'model_save', 'model_save_many', 'model_remove', 'model_discover', 'set_model', 'memory_export',
    'memory_archive_export_to', 'memory_archive_preview_file', 'memory_archive_import_file', 'export_save',
    'git_export', 'git_export_file', 'media_download', 'telemetry_export', 'telemetry_clear', 'storage_reveal',
    'storage_set', 'storage_settings_resolve', 'storage_forget_resolve', 'storage_cleanup_legacy', 'storage_delete',
    # sign-ins and credentials
    'chatgpt_start', 'chatgpt_cancel', 'chatgpt_sign_out', 'plan_credentials', 'plan_credential_save',
    'plan_credential_delete',
    # installing code
    'mod_install', 'mod_example', 'mod_preview', 'mod_remove', 'extension_add', 'extension_import', 'extension_remove',
    'browser_connect_install',
    # Mac maintenance and the browser profile
    'upgrade_backups', 'upgrade_discard_backup', 'upgrade_retry', 'browser_profile_forget', 'browser_extension_unpair',
}) | frozenset(MAC_ROUTES)


def phone_excluded(method):
    return (method in PHONE_EXCLUDED or method in SHELL_NATIVE_ONLY or method.startswith('plugin_package_')
            or (method.startswith('connector_') and method != 'connector_status'))


# Section 3.8, C1's remote.unlock and remote.lock (S-05), C3's remote.screenshot and v0.5's remote.folders.
REMOTE_METHODS = ('remote.status', 'remote.devices', 'remote.revoke_device', 'remote.keep_awake', 'remote.chat_tail',
                  'remote.chat_before', 'remote.ping', 'remote.unlock', 'remote.lock', 'remote.screenshot',
                  'remote.folders')
PHONE_ALLOWED = tuple(m for m in ALLOWED if not phone_excluded(m))
METHODS = PHONE_ALLOWED + REMOTE_METHODS
METHOD_SET = frozenset(METHODS)

# Section 3.6 (approvals, deletions that cannot be undone, more reach, work without the person), C1's remote.unlock
# and v0.5's opening methods: a Face ID proof on every call.
ELEVATED_ALWAYS = frozenset({
    'approve', 'approve_simple', 'run_held_accept', 'run_held_finish', 'model_handoff_approve',
    'storage_purge_chat', 'storage_purge_all', 'storage_trash_all', 'delete_chat', 'memory_delete', 'memory_clear',
    'git_remove_worktree', 'agent_worktree_retire', 'environment_remove', 'rewind_apply',
    'screen_session_grant', 'app_control_request', 'shell_grant_add', 'shell_grant_remove', 'extension_toggle',
    'mod_toggle', 'process_input_human',
    'job_create', 'job_resume',
    'remote.unlock'}) | frozenset(OPEN_ON_MAC)
APPROVALS = frozenset({'approve', 'approve_simple', 'run_held_accept', 'run_held_finish', 'model_handoff_approve'})
# Every key App.setting accepts, and every key of settings_file.KEYS, classified (claude-w's 3.6 addition, v0.3 item
# 5). 'elevated' keys can widen what Pebble does without asking (settings_file.widening covers exactly these); an
# unknown key, or one that is not a string, needs a proof. test_remote_policy_46 fails when a key arrives unclassified.
SETTING_CLASSES = {
    'permissions': 'elevated', 'git_decisions': 'elevated', 'memory_enabled': 'elevated', 'memory_layers': 'elevated',
    'notifications': 'elevated', 'storage': 'elevated',
    # w27's review F10: Stealth Lockdown silences the Mac's notices of what a phone does (C3, C7), so a phone changes it
    # only with Face ID, never with the unlock alone
    'stealth': 'elevated',
    'fast_replies': 'plain', 'theme': 'plain', 'reduced_motion': 'plain', 'companion': 'plain',
    # the lead, 1.9.29 merge (w24-genui-a's Work view switch): presentation only, settings_file.widening gives it no
    # reasons, so a phone may set it after the unlock like the theme
    'work_view': 'plain',
    # Phase 46 (1.9.29, w25-genui-b): "Visual answers" changes what the model is offered, never access (no widening)
    'visual_answers': 'plain'}
ELEVATED = tuple(sorted(m for m in METHODS if m in ELEVATED_ALWAYS)) + tuple(
    sorted('setting:' + key for key, kind in SETTING_CLASSES.items() if kind == 'elevated'))

# C1's reads, which work before the unlock, and S-05's remote.lock.
UNLOCK_FREE = frozenset({'state', 'get_chat', 'chat_search', 'run_list', 'run_get', 'run_activity', 'run_events',
                         'stream_read', 'artifact_get', 'remote.status', 'remote.devices', 'remote.ping',
                         'remote.chat_tail', 'remote.chat_before', 'remote.lock'})
# v0.3 item 4: `idem` is optional only on these reads (the list lanes B and C agreed, 01:16). remote.screenshot and
# remote.folders change nothing but need the unlock: they show the Mac's screen and its folder names.
READ_ONLY = UNLOCK_FREE | frozenset({
    'model_profiles', 'screen_sessions', 'toolbox_list', 'toolbox_get', 'job_list', 'job_get', 'job_preview',
    'job_preview_resume', 'goal_list', 'goal_get', 'notification_status', 'command_catalogue', 'storage_summary',
    'browser_status', 'remote.screenshot', 'remote.folders'})
# main.rs writes `surface` on these from the calling window's label, whatever the window sent. The phone acts with the
# main window's authority (R2, D215), so the bridge writes 'main' on exactly these, whatever the phone sent.
SURFACE_ROUTES = frozenset({'quiz_share', 'rewind_preview', 'rewind_apply', 'context_inspect', 'context_compact',
                            'run_held_tests', 'run_held_finish', 'run_held_accept'})
# The bridge's own refusal codes (3.4, 3.11 item 11). A code Pebble gives that equals one of them is dropped, so
# Pebble's text can never make the phone ask for Face ID or retry elevated (security review X-6).
BRIDGE_CODES = frozenset({'not_allowed', 'proof_required', 'bad_proof', 'too_large', 'rate_limited', 'pebble_offline',
                          'timeout', 'bad_request', 'unlock_required'})
# Events the windows never get (main.rs keeps native_export_pdf for itself), so neither does the phone (C4). Pebble
# Remote's own Mac events (remote_*: a pairing waiting for the Mac, the Settings page, the menu-bar mark) stay on the
# Mac: one phone has no use for another phone's pairing code.
NEVER_FORWARDED = frozenset({'native_export_pdf'})
MAC_EVENT_PREFIX = 'remote_'

# Section 3.11 item 2: request ids and idem keys (both are lines of T_proof).
ID = re.compile(r'[A-Za-z0-9_-]{1,64}')


def valid_id(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def forwarded(event):
    return isinstance(event, str) and event not in NEVER_FORWARDED and not event.startswith(MAC_EVENT_PREFIX)


def workspace_class(value, known):
    """What a `set_workspace` path or a `new_chat` workspace asks for: 'none' (null or empty: the Companion), 'known'
    (a project a listed conversation uses; `known()` gives them) or 'new'. Anything else is 'new', so it fails closed."""
    if value is None or value == '':
        return 'none'
    if isinstance(value, str) and value in known():
        return 'known'
    return 'new'


def needs_proof(method, params, known, caller):
    """Whether a plain call (params as an object, no proof) must be refused with proof_required.

    - 3.6: the elevated methods, always.
    - `setting`: by its key's class; a key that is not a string, or an unknown one, needs a proof (security review
      M-2 and X-4: the key Pebble would act on is the key judged, since the params relayed are the params parsed here).
    - v0.5 item 2: `set_workspace` and `new_chat` with a folder no conversation uses (`new_chat` follows the same rule,
      or it would be a way around set_workspace's; the iOS side's contract issue 8). The answer is the same for every
      new value, so a phone without a proof learns nothing about the Mac's folders.
    - Security review X-8: removing another phone needs a proof; a phone removing itself needs only the unlock.
    """
    if method in ELEVATED_ALWAYS:
        return True
    if method == 'setting':
        key = params.get('key')
        return not isinstance(key, str) or SETTING_CLASSES.get(key, 'elevated') == 'elevated'
    if method == 'set_workspace':
        return workspace_class(params.get('path'), known) == 'new'
    if method == 'new_chat':
        return workspace_class(params.get('workspace'), known) == 'new'
    if method == 'remote.revoke_device':
        return params.get('device_id') != caller
    return False


# What the Mac's activity line says a phone did (contract 4.8, C7): fixed words per method, in Pebble's voice. A method
# not named here reads by its area; reads are never recorded.
DONE = {
    'send': 'sent a message', 'new_chat': 'started a conversation', 'run_steer': 'guided a task',
    'cancel': 'stopped a task', 'run_pause': 'paused a task', 'run_resume': 'resumed a task',
    'approve': 'approved', 'approve_simple': 'approved', 'reject': 'declined', 'reject_simple': 'declined',
    'run_held_accept': 'accepted a held task', 'run_held_finish': 'asked a held task to finish',
    'run_held_tests': 'asked a held task for tests', 'model_handoff_approve': 'approved a model handoff',
    'undo': 'undid a change', 'question_answer': 'answered a question', 'question_cancel': 'skipped a question',
    'artifact_submit': 'answered a form', 'chat_rename': 'renamed a conversation', 'set_workspace': 'changed a project',
    'storage_trash_chat': 'moved a conversation to Recently deleted', 'storage_restore_chat': 'restored a conversation',
    'storage_purge_chat': 'deleted a conversation for good', 'storage_trash_all': 'moved every conversation to '
    'Recently deleted', 'storage_purge_all': 'deleted every conversation for good', 'delete_chat': 'deleted a '
    'conversation', 'model_select': 'chose a model', 'model_effort': 'changed the effort', 'model_load': 'loaded the '
    'model', 'model_unload': 'unloaded the model', 'screen_session_revoke': 'ended screen access',
    'screen_session_grant': 'allowed a window', 'app_control_request': 'asked for app control',
    'shell_grant_add': 'allowed a shell folder or site', 'shell_grant_remove': 'removed a shell permission',
    'rewind_apply': 'rewound files', 'job_create': 'made a schedule', 'job_resume': 'resumed a schedule',
    'job_pause': 'paused a schedule', 'job_delete': 'deleted a schedule', 'extension_toggle': 'turned an extension '
    'on or off', 'mod_toggle': 'turned a view on or off', 'process_input_human': 'typed into a process',
    'open_url': 'opened a page on this Mac', 'open_app': 'asked to open an app on this Mac',
    'view_stage': 'opened a view on this Mac', 'artifact_view_open': 'opened a page for review on this Mac',
    'browser_start': "started Pebble's browser on this Mac", 'browser_open_tab': "opened a tab in Pebble's browser",
    'quiz_share': 'shared a quiz result', 'terminal_propose': 'proposed a terminal command',
    'process_terminal': 'opened a process in Terminal on this Mac', 'memory_delete': 'deleted a memory',
    'memory_clear': 'cleared memory', 'remote.screenshot': 'took a screenshot of this Mac',
    'remote.keep_awake': 'changed keep awake', 'remote.revoke_device': 'removed a phone', 'attach_bytes': 'added a file',
    'attach_url': 'added a web source', 'remove_source': 'removed a source'}
AREAS = (('memory_', 'changed memory'), ('goal_', 'changed a goal'), ('agent_', 'changed a helper task'),
         ('job_', 'changed a schedule'), ('toolbox_', 'changed the Toolbox'), ('git_', 'used Git'),
         ('process_', 'changed a background process'), ('screen_', 'changed screen access'),
         ('browser_', "changed Pebble's browser"), ('shell_', 'changed shell access'), ('storage_', 'changed Data & '
         'storage'), ('context_', 'compacted a conversation'), ('export_', 'worked on an export'),
         ('quiz_', 'worked on a quiz'), ('notification_', 'changed notifications'), ('run_', 'changed a task'),
         ('environment_', 'changed a project environment'), ('telemetry_', 'changed telemetry'),
         ('artifact_', 'changed a view'), ('model_', 'changed models'))
SETTING_WORDS = {'permissions': 'Permissions', 'git_decisions': 'Git decisions', 'memory_enabled': 'memory',
                 'memory_layers': 'memory layers', 'notifications': 'notifications', 'fast_replies': 'Fast replies',
                 'stealth': 'Stealth Lockdown', 'theme': 'the theme', 'reduced_motion': 'Reduce motion',
                 'companion': 'the companion', 'work_view': 'the work view', 'visual_answers': 'Visual answers'}
# C7: these also post a notification on the Mac (approvals, v0.5's opening methods, C3's screenshots).
NOTIFIED = APPROVALS | frozenset(OPEN_ON_MAC) | {'remote.screenshot'}


def done_words(method, params):
    """The verb phrase for a call that changed something, or None for a read (never recorded)."""
    if method in READ_ONLY and method != 'remote.screenshot':
        return None
    if method in ('remote.unlock', 'remote.lock', 'remote.ping'):
        return None
    if method == 'setting':
        return 'changed ' + SETTING_WORDS.get(params.get('key'), 'a setting')
    if method in DONE:
        return DONE[method]
    return next((words for prefix, words in AREAS if method.startswith(prefix)), 'changed something in Pebble')
