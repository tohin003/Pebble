"""Phase 17 source/frozen JSON-RPC proof: goals, UpdateGoal exposure and authority, verified exports, native
notifications and lifecycle, host commands, the no-download tokenizer and the shell's route lists.

Drives the *actual* service over its JSON-RPC stdio contract, launched from the
main.rs environment allowlist, with one owned loopback SSE provider and an owned
temporary home outside the checkout. Every model turn is scripted synthetic
output; every goal edit, review, export save, notification acknowledgement and
lifecycle hint is the native route a person or the native shell uses. No real
model, external network, account, vault or user data is used.

Scenarios (each tagged with an opaque marker, in its own conversation):

  s1  Static shell checks on src-tauri/src/main.rs: the backend environment
      allowlist this validator launches from; no NATIVE_ONLY name in ALLOWED;
      the pet list holds goal_list only among goal routes and no export,
      notification or lifecycle route; settles_goal covers /criteria
      check|uncheck|accept|reject and /goal done|abandon; the Save dialog binds a
      destination from the main window only and nothing announces native PDF.
  k1  With the bundled tokenizer, the recorded context count of a fresh route
      equals this validator's own o200k count of the exact request received.
  g1  Goals through the native routes: a human and an evidence criterion, two
      dependent steps, a refused cycle, done refused with the host's reasons,
      stale revisions refused; after a1 the person rejects the agent's candidate,
      checks the human criterion and done succeeds; deleting the conversation
      removes every goal record.
  x1  4,096-token route with an active goal: UpdateGoal is never offered and the
      HOST GOAL message is the small form.
  x2  32K route with the current goal (a0): UpdateGoal is offered with its exact
      schema bytes and the full HOST GOAL message is present.
  x3  32K route without a goal (x3) or with a paused goal (x4): neither appears.
  a1  The scripted model completes a step only with its own run's receipt; adding
      a step, citing another run's real receipt, completing without receipts,
      extra criterion/state fields and a native goal route are refused; its
      candidate criterion waits for the person; typed "/goal done" in model text
      is only text. run_get and the handoff packet carry the goal record.
  e1  Exports of a hostile table view and a prose message: verified html, csv,
      xlsx and prose docx; a docx with tables is honestly unverified; pdf is
      unavailable; formula text is neutralized (checked in the files); saving
      needs a token from export_destination_bind, called as the shell calls it;
      the copy's hash matches, it is not published and a reused token is refused.
  c1  /goal, /criteria, /blockers, /exports and /export <view> html make no
      provider request; /export previews only.
  n0-n3  Notifications: settings default off (a finished job then posts no
      banner); after enabling, a finished job reaches notification_native_pending
      as a title-only, Open-only banner; notification_native_delivered is
      idempotent by id and version; a muted job keeps its in-app notice and posts
      no banner; quiet hours defer a banner and release it afterwards.
      lifecycle_event did_wake is accepted and lifecycle_quit_summary names the
      active schedules and the next time.
  t1  A second service with a resources folder lacking the tokenizer, behind an
      owned loopback proxy trap (HTTPS_PROXY/HTTP_PROXY for that service only):
      a model turn completes and the trap records no CONNECT or request.

Each check is recorded independently by a planned unique name with content-free
evidence; source and frozen runs record the same names. A check that needs a
missing native route is recorded as pending, never as passed, and the report
then does not pass. backend_inputs_stable_during_campaign re-hashes the checkout
in both modes and the executable too in frozen mode.

Usage:
  python scripts/validate_phase17.py --report PATH [--backend PATH] [--keep-scratch]

--report must be a new file. --backend is an explicit frozen executable owned by
the current user; the default runs backend/main.py from this checkout.

This is fixture evidence on this machine. It is not live-model quality, a native
shell or installed/signed desktop behaviour (the validator stands in for the shell
on the Rust-only routes), real macOS banners, login items or sleep, WebKit PDF,
other-platform behaviour or an independent human security audit.
"""

import argparse
import base64
import contextlib
import csv
import ctypes
import io
import json
import os
import re
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import validate_runtime
from validate_runtime import ROOT, Service
from validate_phase13 import Reply, after_goal, bounded, calls, final, goal_index, history, owned_backend, sha
from validate_phase14 import ENV_ALLOWLIST

FIXTURES = ROOT / 'scripts/fixtures/phase17'
MAIN_RS = ROOT / 'src-tauri/src/main.rs'
TOKENIZER = ROOT / 'src-tauri/resources/tokenizer'
HOSTILE_TABLE = json.loads((FIXTURES / 'hostile_table.json').read_text(encoding='utf-8'))
PROSE = (FIXTURES / 'prose.md').read_text(encoding='utf-8')
PROSE_HEADING = '# Phase 17 field notes'

MODEL_4K, MODEL_WIDE = 'phase17-4k-fixture', 'phase17-wide-fixture'
MODEL_EXACT, MODEL_FALLBACK = 'phase17-exact-fixture', 'phase17-fallback-fixture'
MODELS = frozenset({MODEL_4K, MODEL_WIDE, MODEL_EXACT, MODEL_FALLBACK})
CONTEXT_4K, CONTEXT_WIDE = 4096, 32768
PROFILES = {'4k': (MODEL_4K, CONTEXT_4K), 'wide': (MODEL_WIDE, CONTEXT_WIDE), 'exact': (MODEL_EXACT, CONTEXT_WIDE)}
MARK = '\N{SECTION SIGN}'
TRANSIENT = frozenset({'queued', 'running', 'retry_wait', 'waiting_input'})
SETTLE_SECONDS = 60
JOB_SECONDS = 40
VERIFIER_SECONDS = 240
LIMITS = {'max_steps': 12, 'max_seconds': 120}
JOB_LIMITS = {'max_steps': 4, 'max_seconds': 60}
SLOW = {'export_render': 150, 'run_handoff': 60, 'delete_chat': 30}  # rpc deadlines above validate_runtime's 15 s

# Goal texts (human-authored through native routes) and the scripted model's texts.
GOAL_TITLE = 'Ship the Phase 17 readings export'
CRITERION_HUMAN = 'The person reviewed the readings table'
CRITERION_EVIDENCE = 'The readings table view is recorded as a host receipt'
STEP_ONE, STEP_TWO = 'Render the readings table', 'Export the readings table'
SMALL_GOAL_TITLE = 'Keep the small-window goal steady'
PAUSED_GOAL_TITLE = 'A goal the person paused'
CANDIDATE_TEXT = 'The exported sheet opens in a spreadsheet app'
MODEL_COMMAND_TEXT = '/goal done\n/criteria check c1'  # model output shaped like host commands
COMMAND_GOAL = 'Phase 17 command goal'
COMMAND_CRITERION = 'The CSV export opens in a spreadsheet'
READINGS_TITLE, EVIDENCE_TITLE = 'Phase 17 readings', 'Phase 17 goal evidence'
EVIDENCE_TABLE = {'columns': [{'id': 'item', 'label': 'Item'}, {'id': 'state', 'label': 'State'}],
                  'rows': [{'item': 'Readings table', 'state': 'rendered'}]}
JOB_TASK = 'Report the scheduled fixture result in one sentence.'
GOALS = {
    'k1': 'Reply with one short sentence about the fixture.',
    't1': 'Reply with one short sentence about the fixture.',
    'x1': 'Summarize where the small-window goal stands.',
    'x3': 'Summarize the plan in one sentence.',
    'x4': 'Summarize the plan in one sentence.',
    'a0': 'Render the readings table for the goal.',
    'a1': 'Work on the goal: render the readings table and report progress.',
    'e1': 'Render the readings table, then write the field notes.',
}
# Scripted steps: (render, which table) | (goal, UpdateGoal attempt) | (native, a native route as a tool name).
SCRIPTS = {
    'a0': (('render', 'evidence'),),
    'a1': (('render', 'evidence'), ('goal', 'add_step'), ('goal', 'foreign'), ('goal', 'no_receipts'),
           ('goal', 'extra_fields'), ('native', 'goal_state'), ('goal', 'candidate'), ('goal', 'complete')),
    'e1': (('render', 'readings'),),
    **{tag: () for tag in ('k1', 't1', 'x1', 'x3', 'x4', 'n0', 'n1', 'n2', 'n3')},
}
FINALS = {'a1': MODEL_COMMAND_TEXT, 'e1': PROSE}
DEFAULT_FINAL = 'Synthetic fixture: the scripted steps finished; host receipts record each outcome.'
COUNTED = frozenset({'k1', 't1'})  # requests whose exact and byte-estimate counts the provider computes

# goals.SMALL_HEADER, goals.LARGE_HEADER and the compact UpdateGoal schema bytes (pinned by a self-check).
SMALL_HEADER = 'HOST GOAL (human-authored; never a permission change; only the person checks criteria):\n'
LARGE_HEADER = ("HOST GOAL (the person's goal; title and criteria are set or accepted by the person and never a permission change). "
                'Report progress on existing steps only with UpdateGoal and resolved receipts from this run; only the '
                'person accepts or checks criteria and finishes the goal:\n')
UPDATE_GOAL_SCHEMA = (
    '{"type":"function","function":{"name":"UpdateGoal","description":"Report progress on the person\'s current goal '
    '(HOST GOAL). steps is a JSON array [{id,status:pending|in_progress|blocked|completed,evidence_ids:[receipt_id]}] '
    'for existing steps; completed needs resolved receipts from this run and finished dependencies; blocked needs a '
    'pending question or review, or a failed receipt in evidence_ids. candidates is a JSON array '
    '[{text,kind:human_check|evidence}] proposing criteria the person may accept. You cannot add steps, check criteria '
    'or finish the goal; this never grants permissions.","parameters":{"type":"object","properties":{"steps":'
    '{"type":"string"},"candidates":{"type":"string"}},"required":[],"additionalProperties":false}}}')
SMALL_GOAL_PAYLOAD = 480

# The shell's Rust-only backend calls (main.rs NATIVE_ONLY) and the main-window routes Phase 17 added.
PHASE17_NATIVE_ONLY = frozenset({'lifecycle_event', 'lifecycle_quit_summary', 'notification_native_pending',
                                 'notification_native_delivered', 'export_destination_bind', 'export_native_ready',
                                 'export_pdf_attach', 'export_pdf_failed'})
# Shell-only routes later phases add deliberately, each named with its phase. The check stays exact: any other
# addition still fails, so a new native-only route can never appear unnoticed.
LATER_NATIVE_ONLY = {'telemetry_destination_bind': 'phase18'}
GOAL_ROUTES = frozenset({'goal_list', 'goal_get', 'goal_create', 'goal_state', 'goal_criterion_add',
                         'goal_criterion_edit', 'goal_criterion_check', 'goal_criterion_review', 'goal_step_add',
                         'goal_step_edit', 'goal_step_block', 'goal_blockers', 'goal_continue'})
EXPORT_ROUTES = frozenset({'export_preview', 'export_render', 'export_cancel', 'export_save', 'export_list',
                           'export_get', 'export_discard'})
NOTIFICATION_ROUTES = frozenset({'notification_status', 'notification_mute'})
NATIVE_SHAPED = re.compile(r'^(goal|export|notification|lifecycle|job)_')
SETTLING = {'/criteria': {'check', 'uncheck', 'accept', 'reject'}, '/goal': {'done', 'abandon'}}
TYPED_EXAMPLES = ('/criteria check c1', '  /Criteria UNCHECK c2', '/criteria accept c3', '/criteria reject c3',
                  '/goal done', '/GOAL abandon', '/goal pause', '/goal', '/criteria add "x"', '/goal "title"')

# Notification settings (notifications.DEFAULT_SETTINGS) and the categories they name.
CATEGORIES = ('completed', 'failed', 'action_needed', 'monitor_changed')
DEFAULT_NOTIFICATIONS = {'enabled': False, 'kinds': dict.fromkeys(CATEGORIES, True), 'preview': 'title',
                         'quiet_hours': None,
                         'replies': True}  # Phase 45 (D134, the person): banners for replies and cards, under enabled
SETTINGS_ON = {**DEFAULT_NOTIFICATIONS, 'enabled': True}
DAILY_ZONE = 'Asia/Kolkata'

# Export formula neutralization (export_worker.TRIGGERS and plain_number, pinned by a self-check).
TRIGGERS = ('=', '+', '-', '@', '\t', '\r', '＝', '＋', '－', '＠')
PLAIN_NUMBER = re.compile(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')
FORMULA_TEXT = frozenset({'=Total', '=1+2', '+SUM(A1:A2)', '-2+3', '@SUM(A1:A2)', '＝full width'})
PLAIN_NUMBER_TEXT = '-5'  # formula-like at a glance but a plain number: left unprefixed
XLSX_PART_LIMIT = 1_000_000

PROXY_NAMES = ('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy', 'ALL_PROXY', 'all_proxy')
TRAP_CONTROL_HOST = 'phase17-trap.invalid'

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    's1_service_launch_environment_matches_the_shell_allowlist',
    's1_no_native_only_route_is_in_the_webview_allowlist',
    's1_pet_list_holds_only_goal_list_among_goal_routes_and_no_export_or_notification_route',
    's1_settles_goal_covers_criteria_check_uncheck_accept_reject_and_goal_done_abandon',
    's1_save_dialog_binds_destinations_from_the_main_window_and_no_native_pdf_is_announced',
    'loopback_profiles_saved_and_actual_guard_available',
    'k1_bundled_tokenizer_context_counts_are_exact_o200k',
    'g1_goal_created_with_human_and_evidence_criteria_and_dependent_steps',
    'g1_a_dependency_cycle_is_refused_and_nothing_changes',
    'g1_done_is_refused_with_the_hosts_reasons',
    'g1_stale_revisions_are_refused',
    'x1_update_goal_is_never_offered_at_4096_and_the_host_goal_message_is_the_small_form',
    'x2_update_goal_and_the_full_host_goal_message_are_offered_above_4096_with_a_current_goal',
    'x3_update_goal_and_host_goal_are_absent_without_a_goal_or_with_a_paused_goal',
    'a1_update_goal_completes_a_step_only_with_receipts_from_its_own_run',
    'a1_the_model_cannot_add_steps_check_criteria_or_finish_the_goal',
    'a1_proposed_criteria_wait_for_the_persons_review',
    'a1_run_get_and_handoff_carry_the_linked_goal_record',
    'g1_the_person_checks_the_criteria_then_done_succeeds',
    'g1_deleting_the_conversation_removes_its_goal_records',
    'e1_html_csv_and_xlsx_exports_of_a_table_view_read_back_verified',
    'e1_prose_docx_export_of_a_message_reads_back_verified',
    'e1_docx_of_a_table_view_is_honestly_unverified_because_tables_are_not_kept',
    'e1_pdf_is_unavailable_in_this_build_and_writes_nothing',
    'e1_hostile_table_formula_text_is_neutralized_in_csv_and_xlsx',
    'e1_export_save_works_only_with_a_token_from_export_destination_bind',
    'e1_saved_copy_hash_matches_it_is_not_published_and_a_reused_token_is_refused',
    'c1_goal_criteria_blockers_and_exports_commands_make_no_provider_request',
    'c1_export_command_previews_only_and_writes_saves_or_publishes_nothing',
    'n1_notification_settings_default_off_and_a_notice_while_off_posts_no_banner',
    'n1_after_enabling_a_finished_job_reaches_native_pending_as_a_title_only_open_only_banner',
    'n1_native_delivered_is_idempotent_by_id_and_version',
    'n1_a_muted_schedule_keeps_its_in_app_notice_and_posts_no_banner',
    'n1_quiet_hours_defer_a_banner_without_dropping_it',
    'n1_lifecycle_did_wake_is_accepted_and_quit_summary_names_the_active_schedules',
    't1_without_a_bundled_tokenizer_a_model_turn_completes_with_no_outbound_connection',
    'native_phase17_routes_are_never_offered_to_models',
    'synthetic_provider_protocol_clean',
    'backend_inputs_stable_during_campaign',
)


class Pending(AssertionError):
    """A check that waits for a missing native route. It is never recorded as passed."""


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def tool_name(spec):
    function = spec.get('function') if isinstance(spec, dict) else None
    name = function.get('name') if isinstance(function, dict) else None
    return name if isinstance(name, str) else None


def triggered(text):
    return isinstance(text, str) and text.startswith(TRIGGERS) and not PLAIN_NUMBER.fullmatch(text)


def host_goal(messages):
    """The HOST GOAL message a request carries: its form, payload size and parsed payload; None when absent."""
    for message in messages:
        content = message.get('content')
        if message.get('role') != 'user' or not isinstance(content, str) or not content.startswith('HOST GOAL'):
            continue
        form = 'small' if content.startswith(SMALL_HEADER) else 'large' if content.startswith(LARGE_HEADER) else 'other'
        text = content[len(SMALL_HEADER if form == 'small' else LARGE_HEADER):] if form != 'other' else ''
        try:
            payload = json.loads(text) if text else None
        except ValueError:
            payload = None
        return {'form': form, 'chars': len(text), 'payload': payload, 'names_update_goal': 'UpdateGoal' in content,
                'candidate_text': CANDIDATE_TEXT in content}
    return None


# The bundled o200k encoding, built here from the checkout's file (never tiktoken's downloading loader).
_ENCODING, _ENCODING_LOCK = [], threading.Lock()


def o200k():
    with _ENCODING_LOCK:
        if not _ENCODING:
            if str(ROOT / 'backend') not in sys.path:
                sys.path.insert(0, str(ROOT / 'backend'))
            from pebble import token_budget
            import tiktoken
            data = (TOKENIZER / token_budget.TOKENIZER_FILE).read_bytes()
            if sha(data) != token_budget.TOKENIZER_SHA256:
                raise AssertionError('The bundled tokenizer differs from its published SHA-256.')
            ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in data.splitlines() if line)}
            _ENCODING.append(tiktoken.Encoding('phase17-o200k', pat_str=token_budget.O200K_PATTERN, mergeable_ranks=ranks,
                                               special_tokens=token_budget.O200K_SPECIAL))
        return _ENCODING[0]


def expected_counts(messages, tools):
    """TokenBudget.count's formula on the exact wire payload: with o200k ranks and with one token per byte."""
    payload = json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False)
    extra = len(messages) * 12 + 64
    return {'exact': int(len(o200k().encode(payload, disallowed_special=())) * 1.03) + extra,
            'bytes': int(len(payload.encode('utf-8', errors='surrogatepass')) * 1.03) + extra,
            'messages': len(messages)}


def inspect_csv(data):
    """Formula-like fields in a CSV export: protected by a leading apostrophe, or not."""
    text = data.decode('utf-8-sig')
    fields = [field for row in csv.reader(io.StringIO(text, newline='')) for field in row]
    return {'byte_order_mark': data.startswith(b'\xef\xbb\xbf'), 'fields': len(fields),
            'protected': sorted(f[1:] for f in fields if f.startswith("'") and triggered(f[1:])),
            'unprotected': sorted(f for f in fields if triggered(f)), 'plain': fields}


def inspect_xlsx(data):
    """Formula cells and formula-like inline strings with and without a quotePrefix style, from the package itself."""
    local = lambda element: element.tag.rsplit('}', 1)[-1]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        sizes = {info.filename: info.file_size for info in archive.infolist()}
        if any(size > XLSX_PART_LIMIT for size in sizes.values()):
            raise AssertionError('An XLSX part exceeds the inspection bound.')
        styles = ElementTree.fromstring(archive.read('xl/styles.xml'))
        xfs = next((node for node in styles.iter() if local(node) == 'cellXfs'), None)
        quoted = {index for index, xf in enumerate(list(xfs) if xfs is not None else [])
                  if xf.get('quotePrefix') in ('1', 'true')}
        report = {'formulas': 0, 'protected': [], 'unprotected': [], 'texts': [], 'sheets': 0,
                  'external_relationships': sum(b'External' in archive.read(name) for name in sizes if name.endswith('.rels'))}
        for name in sorted(n for n in sizes if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)):
            report['sheets'] += 1
            for cell in ElementTree.fromstring(archive.read(name)).iter():
                if local(cell) == 'f':
                    report['formulas'] += 1
                if local(cell) != 'c' or cell.get('t') != 'inlineStr':
                    continue
                text = ''.join(node.text or '' for node in cell.iter() if local(node) == 't')
                report['texts'].append(text)
                if triggered(text):
                    report['protected' if int(cell.get('s', '0')) in quoted else 'unprotected'].append(text)
    report['protected'].sort()
    report['unprotected'].sort()
    return report


def goal_record_problems(linked, unlinked, goal_id):
    """Handoff packets (run_handoff data): the linked run's goal_record holds that goal's snapshot; a run with no
    linked goal has no goal_record section. Returns the problems found."""
    record, problems = linked.get('goal_record'), []
    snapshot = record.get('record') if isinstance(record, dict) else None
    if not isinstance(record, dict) or (record.get('recorded_by'), record.get('semantics')) != ('goals', 'snapshot_not_completion'):
        problems.append("the linked run's packet has no goals goal_record: " + str(record)[:160])
    elif not isinstance(snapshot, dict) or snapshot.get('id') != goal_id:
        problems.append(f'goal_record.record is a {type(snapshot).__name__}, not the linked goal snapshot: ' + str(snapshot)[:120])
    if 'goal_record' in unlinked:
        extra = unlinked['goal_record']
        problems.append('a run without a linked goal carries a goal_record: '
                        + str(extra.get('record') if isinstance(extra, dict) else extra)[:120])
    return problems


def shell_facts(text):
    """What the native shell source declares: route lists, settles_goal arms, the launch environment, Save dialog."""
    def block(pattern, what):
        found = re.search(pattern, text, re.S)
        if not found:
            raise AssertionError('main.rs no longer has ' + what + '.')
        return found.group(1)
    strings = lambda chunk: re.findall(r'"([A-Za-z0-9_]+)"', chunk)
    settles = block(r'fn settles_goal\(text:&str\) -> bool \{(.*?)\n\}', 'settles_goal')
    arms = {}
    for command, words in re.findall(r'\(Some\("(/[a-z]+)"\),\s*Some\(([^)]*)\)\)\s*=>\s*true', settles):
        arms.setdefault(command, set()).update(re.findall(r'"([a-z]+)"', words))
    choose = block(r'async fn export_choose_destination\((.*?)\n\}\n', 'export_choose_destination')
    bind = re.search(r'native_call\(&backend, "export_destination_bind", json!\(\{(.*?)\}\)', choose, re.S)
    returned = re.findall(r'Ok\(json!\(\{(.*?)\}\)\)', choose, re.S)
    return {
        'native_only': strings(block(r'const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];', 'the NATIVE_ONLY list')),
        'allowed': strings(block(r'const ALLOWED: &\[&str\] = &\[(.*?)\];', 'the ALLOWED list')),
        'pet': strings(block(r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains\(&method\.as_str\(\)\)', 'the pet list')),
        'settles_arms': arms,
        'settles_normalizes': 'trim_start()' in settles and 'to_lowercase' in settles,
        'pet_send_gate': bool(re.search(r'window\.label\(\)=="pet" && method=="send" && params\.get\("text"\)'
                                        r'\.and_then\(Value::as_str\)\.is_some_and\(settles_goal\)', text)),
        'rpc_refuses_native_only': bool(re.search(r'if NATIVE_ONLY\.contains\(&method\.as_str\(\)\) \{ return Err\(', text)),
        'native_call_guard': bool(re.search(r'fn native_call\([^)]*\)[^{]*\{\s*if !NATIVE_ONLY\.contains\(&method\)', text)),
        'debug_assert': 'debug_assert!(ALLOWED.iter().all(|m| !NATIVE_ONLY.contains(m)));' in text,
        'environment': strings(block(r'command\.env_clear\(\);\s*for key in \[(.*?)\] \{', 'the backend environment allowlist')),
        'handlers': re.findall(r'\w+', block(r'generate_handler!\[(.*?)\]', 'the command handler list')),
        'choose_main_only': 'if window.label()!="main" { return Err(' in choose,
        'bind_keys': re.findall(r'"([a-z_]+)":', bind.group(1)) if bind else None,
        'choose_returns': re.findall(r'"([a-z_]+)":', returned[-1]) if returned else None,
        'native_ready_mentions': text.count('"export_native_ready"'),
        'pdf_event_fails_closed': bool(re.search(r'Some\("native_export_pdf"\).*?native_call\(&owner, "export_pdf_failed"',
                                                 text, re.S)),
    }


def settles(arms, typed):
    """The Rust match in settles_goal, read from its parsed arms."""
    words = typed.lstrip().split()
    first, second = (words + [None, None])[:2]
    return bool(first and second and second.lower() in arms.get(first.lower(), ()))


def procargs(pid):
    """The initial argv and environment any same-user process can read (KERN_PROCARGS2)."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(262144)
    buffer = ctypes.create_string_buffer(262144)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), 'KERN_PROCARGS2 is unreadable')
    return buffer.raw[:size.value]


def proxy_environment(url):
    return dict.fromkeys(PROXY_NAMES, url)


class Phase17Service(Service):
    """validate_runtime.Service launched from main.rs's environment allowlist, optionally with another resources
    folder and extra variables (only the t1 proxy trap), and with longer deadlines for slow native routes."""

    def __init__(self, command, home, resources=None, extra_env=None):
        real = subprocess.Popen
        env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
        env.update(extra_env or {})

        def popen(args, *rest, **kwargs):
            args = list(args)
            if resources is not None:
                args[args.index('--resources') + 1] = str(resources)
            return real(args, *rest, **{**kwargs, 'env': env})
        validate_runtime.subprocess.Popen = popen
        try:
            super().__init__(command, home)
        finally:
            validate_runtime.subprocess.Popen = real

    def rpc(self, method, **params):
        ident = uuid.uuid4().hex
        self.process.stdin.write(json.dumps({'id': ident, 'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        deadline = time.monotonic() + (45 if self.starting else SLOW.get(method, 15))
        while True:
            try:
                item = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except Exception:
                self.stderr.seek(0)
                raise RuntimeError(f'Backend timed out during {method}; stderr: {self.stderr.read()[-3000:]}') from None
            if item is None:
                self.stderr.seek(0)
                raise RuntimeError(f'Backend stopped during {method}; stderr: {self.stderr.read()[-3000:]}')
            if item.get('protocol_error'):
                raise RuntimeError(item['protocol_error'])
            if item.get('event'):
                self.events.append(item)
            elif item.get('id') == ident:
                self.starting = False
                if 'error' in item:
                    raise RuntimeError(item['error'])
                return item['result']


class ProxyTrap:
    """Owned loopback HTTP proxy for t1: refuses every request with 403 and records its method and target host."""

    def __init__(self):
        self.lock = threading.Lock()
        self.records = []
        trap = self

        class Handler(socketserver.StreamRequestHandler):
            timeout = 3

            def handle(self):
                try:
                    line = self.rfile.readline(4096).decode('latin-1').strip()
                except OSError:
                    line = ''
                method, _, rest = line.partition(' ')
                target = rest.split(' ', 1)[0]
                host = target.split('/')[2] if '://' in target else target
                with trap.lock:
                    trap.records.append({'method': method[:16], 'target': host[:200]})
                with contextlib.suppress(OSError):
                    self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')

        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.started = False

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def start(self):
        if not self.started:
            self.started = True
            self.thread.start()

    def stop(self):
        if self.started:
            self.server.shutdown()
        self.server.server_close()

    def snapshot(self):
        with self.lock:
            return [dict(r) for r in self.records]

    def control(self):
        """Positive control: a proxy-honoring HTTPS client reaches the trap with a CONNECT. Returns that record."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': self.url, 'http': self.url}))
        with contextlib.suppress(Exception):
            opener.open(f'https://{TRAP_CONTROL_HOST}/', timeout=5).close()
        with self.lock:
            seen, self.records = list(self.records), []
        return seen


class Provider:
    """Owned loopback SSE provider. It records every offered tool and HOST GOAL message and scripts each scenario
    from the host results the run can see."""

    def __init__(self):
        self.lock = threading.RLock()
        self.records, self.errors, self.anomalies = [], [], []
        self.observed = {}  # (tag, step ident) -> the host result the model saw
        self.emitted = {}
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    reply = provider.respond(body)
                except Exception as exc:
                    with provider.lock:
                        provider.errors.append(bounded(exc))
                    with contextlib.suppress(Exception):
                        self.send_error(500)
                    return
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': reply.delta}]}).encode() + b'\n\n')
                    self.wfile.write(b'data: ' + json.dumps({'choices': [], 'usage': reply.usage}).encode() + b'\n\n')
                    self.wfile.write(b'data: [DONE]\n\n')
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def anomaly(self, tag, step, reason):
        with self.lock:
            self.anomalies.append({'scenario': tag, 'step': step, 'reason': reason[:300]})

    def matching(self, **match):
        with self.lock:
            return [dict(r) for r in self.records if all(r.get(k) == v for k, v in match.items())]

    def count(self):
        with self.lock:
            return len(self.records)

    def seen(self, tag, ident):
        with self.lock:
            return self.observed.get((tag, ident))

    def respond(self, body):
        model = body.get('model')
        if model not in MODELS:
            raise AssertionError('Unexpected model route reached the fixture.')
        raw = json.dumps(body)
        if '_pebble_' in raw:
            raise AssertionError('Host-private message fields reached the provider.')
        messages = body.get('messages') or []
        tools = [t for t in body.get('tools') or [] if tool_name(t)]
        index = goal_index(messages)
        found = re.search(MARK + '([a-z][0-9])', messages[index]['content']) if index is not None else None
        tag = found.group(1) if found else None
        step = sum(1 for m in after_goal(messages) if m.get('role') == 'assistant')
        update = next((t for t in tools if tool_name(t) == 'UpdateGoal'), None)
        record = {'scenario': tag, 'model': model, 'step': step, 'tools': sorted(tool_name(t) for t in tools),
                  'update_goal_pinned': None if update is None else compact(update) == UPDATE_GOAL_SCHEMA,
                  'host_goal': host_goal(messages), 'time': time.time()}
        if tag in COUNTED:
            record['expected_counts'] = expected_counts(messages, body.get('tools') or [])
        with self.lock:
            self.records.append(record)
            self.observed.update({(tag, d['id']): d['result'] for d in history(messages)})
        reply = self.script(tag, step, messages)
        # Below any local estimate, so route calibration never enlarges counts (k1 compares exact numbers).
        reply.usage = {'prompt_tokens': max(1, len(raw) // 16), 'completion_tokens': 30}
        with self.lock:
            record['emitted'] = ([c['function']['name'] for c in reply.delta['tool_calls']]
                                 if 'tool_calls' in reply.delta else 'content')
        return reply

    def script(self, tag, step, messages):
        if tag not in SCRIPTS:
            self.anomaly(tag, step, 'unscripted conversation')
            return Reply(final('Synthetic fixture: no script.'))
        with self.lock:
            done = {d['id'] for d in history(messages)} | self.emitted.get(tag, set())
        for index, item in enumerate(SCRIPTS[tag]):
            ident = f'{tag}-{index}-{item[0]}'
            if ident in done:
                continue
            try:
                name, args = self.call_for(tag, item, messages)
            except LookupError as exc:
                self.anomaly(tag, step, str(exc))
                return Reply(final('Synthetic fixture: a needed host result was not visible.'))
            with self.lock:
                self.emitted.setdefault(tag, set()).add(ident)
            return Reply(calls((name, ident, args)))
        return Reply(final(FINALS.get(tag, DEFAULT_FINAL)))

    def receipt(self, tag, ident, messages=None):
        """The receipt ID the host returned for one scripted call (from the run's own visible results)."""
        found = next((d['result'] for d in history(messages) if d['id'] == ident), None) if messages is not None \
            else self.seen(tag, ident)
        receipt = found.get('receipt_id') if isinstance(found, dict) else None
        if not isinstance(receipt, str):
            raise LookupError(f'The receipt of {ident} was not visible.')
        return receipt

    def call_for(self, tag, item, messages):
        kind, what = item
        if kind == 'render':
            title, data = (READINGS_TITLE, HOSTILE_TABLE) if what == 'readings' else (EVIDENCE_TITLE, EVIDENCE_TABLE)
            return 'UIRender', {'title': title, 'kind': 'table', 'data': json.dumps(data, ensure_ascii=False)}
        if kind == 'native':
            return what, {'state': 'done'}
        if what == 'candidate':
            return 'UpdateGoal', {'candidates': json.dumps([{'text': CANDIDATE_TEXT, 'kind': 'human_check'}])}
        if what == 'extra_fields':
            return 'UpdateGoal', {'steps': json.dumps([{'id': 's1', 'status': 'in_progress'}]),
                                  'criteria': json.dumps([{'id': 'c1', 'status': 'met'}]), 'state': 'done'}
        if what == 'no_receipts':
            return 'UpdateGoal', {'steps': json.dumps([{'id': 's1', 'status': 'completed', 'evidence_ids': []}])}
        if what == 'foreign':  # a real, resolved receipt of the earlier a0 run linked to the same goal
            return 'UpdateGoal', {'steps': json.dumps([{'id': 's1', 'status': 'completed',
                                                        'evidence_ids': [self.receipt('a0', 'a0-0-render')]}])}
        own = self.receipt(tag, f'{tag}-0-render', messages)
        step = 's9' if what == 'add_step' else 's1'
        return 'UpdateGoal', {'steps': json.dumps([{'id': step, 'status': 'completed', 'evidence_ids': [own]}])}


class Campaign:
    def __init__(self, command, backend):
        self.command, self.backend = command, backend
        self.provider = Provider()
        self.trap = ProxyTrap()
        self.checks, self.names, self.facts = [], set(), {}
        self.observations = {'run_states': {}, 'scenario_seconds': {}}
        self.fatal = None
        self.stderr_tails = []

    # Check bookkeeping -----------------------------------------------------------------

    def check(self, name, fn):
        assert name in PLANNED, 'Unplanned check name: ' + name
        assert name not in self.names, 'Check names must be unique: ' + name
        self.names.add(name)
        try:
            evidence = fn() or {}
            self.checks.append({'name': name, 'passed': True, 'evidence': evidence})
            return True
        except Pending as exc:
            self.checks.append({'name': name, 'passed': False, 'pending': str(exc)[:600]})
            return False
        except Exception as exc:
            self.checks.append({'name': name, 'passed': False, 'error': bounded(exc, 2000),
                                'trace': traceback.format_exc(limit=4)[-1500:]})
            return False

    def finish(self):
        """Record every planned check that never ran as not run."""
        for name in PLANNED:
            if name in self.names or name == 'backend_inputs_stable_during_campaign':
                continue
            self.names.add(name)
            self.checks.append({'name': name, 'passed': False, 'not_run': True,
                                'error': 'Not run: an earlier check in its scenario failed or the campaign stopped.'})

    def passing(self):
        return all(c['passed'] for c in self.checks)

    def need(self, key):
        if key not in self.facts:
            raise AssertionError('Prerequisite was not established: ' + key)
        return self.facts[key]

    def timed(self, name, scenario, *args):
        started = time.monotonic()
        try:
            return scenario(*args)
        finally:
            self.observations['scenario_seconds'][name] = round(time.monotonic() - started, 1)

    # Service helpers --------------------------------------------------------------------

    def close(self, service, keep_tail):
        if keep_tail:
            try:
                service.stderr.seek(0)
                self.stderr_tails.append(service.stderr.read()[-4000:])
            except Exception as exc:
                self.stderr_tails.append('stderr unavailable: ' + bounded(exc))
        service.close()

    @staticmethod
    def call(service, method, **params):
        """A native route; a route the service does not have is pending, by name."""
        try:
            return service.rpc(method, **params)
        except RuntimeError as exc:
            if str(exc) == 'Unknown request.':
                raise Pending('parent integration: the native route is missing: ' + method) from None
            raise

    def refusal(self, service, method, **params):
        """The refusal message of a route that must refuse; AssertionError if it was accepted or the service failed."""
        try:
            result = self.call(service, method, **params)
        except RuntimeError as exc:
            text = str(exc)
            if text.startswith(('Backend timed out', 'Backend stopped')):
                raise AssertionError('No refusal was observed; the service failed: ' + text[:300]) from None
            return text
        raise AssertionError(method + ' was accepted: ' + json.dumps(result, default=str)[:300])

    @staticmethod
    def open_permissions(service, mode='ask'):
        """Every permission group the service knows on, natively."""
        deadline = time.monotonic() + 10
        while True:
            groups = service.rpc('state')['settings']['permissions']['tools']
            try:
                return service.rpc('setting', key='permissions', value={'mode': mode, 'tools': dict.fromkeys(groups, True)})
            except RuntimeError as exc:
                if not str(exc).startswith('Stop ') or time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def flow(self, service, cid, tag, seconds=SETTLE_SECONDS):
        """Send one tagged human turn and wait until its run settles; any proposal is unexpected and rejected."""
        rid = service.rpc('send', id=cid, text=GOALS[tag] + ' ' + MARK + tag, limits=LIMITS)['run_id']
        deadline = time.monotonic() + seconds
        while True:
            run = service.rpc('run_get', id=cid, run_id=rid)
            active = service.rpc('state')['active']
            if run['state'] == 'waiting_approval' and not active:
                pending = [a for a in service.rpc('get_chat', id=cid)['actions'] if a['status'] == 'pending']
                for action in pending:
                    self.provider.anomaly(tag, None, 'unexpected proposal: ' + str(action.get('kind')))
                    service.rpc('reject', id=cid, action_id=action['id'])
                if pending:
                    continue
            elif run['state'] not in TRANSIENT and run['state'] != 'waiting_approval' and not active:
                break
            if time.monotonic() > deadline:
                with contextlib.suppress(Exception):
                    service.rpc('cancel', id=cid, run_id=rid)
                with self.provider.lock:
                    detail = {'state': run['state'], 'provider_errors': self.provider.errors[-3:],
                              'anomalies': [a for a in self.provider.anomalies if a['scenario'] == tag][-3:]}
                raise AssertionError('Timed out waiting for ' + tag + ' to settle: ' + json.dumps(detail))
            time.sleep(0.1)
        self.observations['run_states'][tag] = run['state']
        return {'cid': cid, 'rid': rid, 'state': run['state'], 'run': run}

    def store_rows(self, sql, args=()):
        """Read-only rows from the owned temporary home's store (authoritative readback)."""
        path = self.need('home') / 'pebble.sqlite'
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, args)]

    # Campaign ------------------------------------------------------------------------------

    def run_all(self, scratch):
        home = scratch / 'home'
        self.facts.update(scratch=scratch, home=home)

        def created():
            assert not scratch.is_relative_to(ROOT), 'Scratch must live outside this checkout and .runtime.'
            for folder in (home, scratch / 'saved', scratch / 'home-no-tokenizer', scratch / 'resources-without-tokenizer'):
                folder.mkdir()
            return {'outside_checkout': True, 'folders': 4}
        self.check('fixture_scratch_created_outside_the_checkout', created)
        try:
            self.timed('static', self.static_checks)
            self.provider.start()
            service = Phase17Service(self.command, home)
            try:
                if self.setup(service):
                    for name, scenario in (('k1', self.scenario_exact), ('goals', self.scenario_goals),
                                           ('exposure', self.scenario_exposure), ('authority', self.scenario_authority),
                                           ('exports', self.scenario_exports), ('commands', self.scenario_commands),
                                           ('notifications', self.scenario_notifications)):
                        self.timed(name, scenario, service)
            finally:
                self.close(service, keep_tail=not self.passing())
            self.timed('tokenizer', self.scenario_tokenizer, scratch)
            self.final_checks()
        except Exception as exc:
            self.fatal = bounded(exc, 3000)
        finally:
            for server in (self.provider, self.trap):
                with contextlib.suppress(Exception):
                    server.stop()
            self.finish()

    # s1: the native shell's declarations ---------------------------------------------------

    def static_checks(self):
        def parsed():
            if 'shell' not in self.facts:
                self.facts['shell'] = shell_facts(MAIN_RS.read_text(encoding='utf-8'))
            return self.facts['shell']

        def environment():
            names = tuple(parsed()['environment'])
            assert names == ENV_ALLOWLIST, (names, ENV_ALLOWLIST)
            return {'names': list(names), 'env_clear': True}
        self.check('s1_service_launch_environment_matches_the_shell_allowlist', environment)

        def native_only():
            shell = parsed()
            native, allowed = set(shell['native_only']), set(shell['allowed'])
            expected = PHASE17_NATIVE_ONLY | set(LATER_NATIVE_ONLY)
            assert native == expected, sorted(native ^ expected)
            assert not native & allowed, sorted(native & allowed)
            assert len(allowed) == len(shell['allowed']), 'ALLOWED repeats a route.'
            assert shell['rpc_refuses_native_only'] and shell['native_call_guard'] and shell['debug_assert'], shell
            missing = sorted((GOAL_ROUTES | EXPORT_ROUTES | NOTIFICATION_ROUTES) - allowed)
            assert not missing, ('Main-window routes missing from ALLOWED.', missing)
            return {'native_only': sorted(native), 'allowed': len(allowed), 'overlap': 0,
                    'rpc_refuses_by_name': True, 'native_call_refuses_others': True}
        self.check('s1_no_native_only_route_is_in_the_webview_allowlist', native_only)

        def pet():
            shell = parsed()
            names = set(shell['pet'])
            goals = sorted(n for n in names if n.startswith('goal_'))
            barred = sorted(n for n in names if n.startswith(('export_', 'notification_', 'lifecycle_')))
            assert goals == ['goal_list'] and not barred, (goals, barred)
            assert names <= set(shell['allowed']) and not names & PHASE17_NATIVE_ONLY, sorted(names - set(shell['allowed']))
            return {'pet_routes': len(names), 'goal_routes': goals, 'export_or_notification_routes': 0}
        self.check('s1_pet_list_holds_only_goal_list_among_goal_routes_and_no_export_or_notification_route', pet)

        def settling():
            shell = parsed()
            arms = shell['settles_arms']
            assert arms == SETTLING, arms
            assert shell['settles_normalizes'] and shell['pet_send_gate'], (shell['settles_normalizes'], shell['pet_send_gate'])
            refused = {typed: settles(arms, typed) for typed in TYPED_EXAMPLES}
            assert [t for t, s in refused.items() if s] == list(TYPED_EXAMPLES[:6]), refused
            return {'arms': {k: sorted(v) for k, v in arms.items()}, 'pet_send_refuses': list(TYPED_EXAMPLES[:6]),
                    'pet_send_allows': list(TYPED_EXAMPLES[6:])}
        self.check('s1_settles_goal_covers_criteria_check_uncheck_accept_reject_and_goal_done_abandon', settling)

        def save_dialog():
            shell = parsed()
            assert shell['choose_main_only'] and 'export_choose_destination' in shell['handlers'], 'Save dialog scope.'
            assert 'export_choose_destination' not in shell['allowed']
            assert shell['bind_keys'] == ['chat_id', 'export_id', 'path', 'replace'], shell['bind_keys']
            assert shell['choose_returns'] == ['destination_token', 'name'], shell['choose_returns']
            assert shell['native_ready_mentions'] == 1 and shell['pdf_event_fails_closed'], (
                shell['native_ready_mentions'], shell['pdf_event_fails_closed'])
            return {'bind_keys': shell['bind_keys'], 'returned_to_webview': shell['choose_returns'],
                    'export_native_ready_called': False, 'native_pdf_jobs_fail_closed': True}
        self.check('s1_save_dialog_binds_destinations_from_the_main_window_and_no_native_pdf_is_announced', save_dialog)

    # Setup and k1 ----------------------------------------------------------------------------

    def setup(self, service):
        def profiles():
            for key, (model, context) in PROFILES.items():
                self.facts['profile:' + key] = service.rpc('model_save', name='Phase 17 ' + key + ' fixture',
                                                           url=self.provider.url, model=model, context=context)['profile_id']
            state = service.rpc('state')
            assert state['guard']['available'] is True, 'The real vendored AgentGuard engine must be available.'
            saved = {p['id']: (p.get('context'), p.get('budget', p.get('context'))) for p in state['profiles']}
            for key, (_, context) in PROFILES.items():
                assert saved.get(self.facts['profile:' + key]) == (context, context), (key, saved.get(self.facts['profile:' + key]))
            self.open_permissions(service)
            service.rpc('model_select', profile_id=self.facts['profile:wide'])
            return {'guard_available': True, 'budgets': {k: c for k, (_, c) in PROFILES.items()}}
        return self.check('loopback_profiles_saved_and_actual_guard_available', profiles)

    def scenario_exact(self, service):
        def exact():
            o200k()  # load and hash-check the bundled file before the provider needs it
            service.rpc('model_select', profile_id=self.need('profile:exact'))
            try:
                fact = self.flow(service, service.rpc('new_chat')['id'], 'k1')
            finally:
                service.rpc('model_select', profile_id=self.need('profile:wide'))
            assert fact['state'] == 'completed', fact['state']
            records = self.provider.matching(scenario='k1')
            assert records, 'No k1 request was captured.'
            expected = records[-1]['expected_counts']
            manifest = (fact['run'].get('context') or {}).get('manifest') or {}
            assert manifest.get('estimation', {}).get('window_method') == 'o200k_local_estimate', manifest.get('estimation')
            counted = manifest['input_tokens']['after']
            assert counted == expected['exact'] and expected['exact'] != expected['bytes'], (counted, expected)
            return {'counted': counted, 'o200k_recount': expected['exact'], 'byte_estimate_would_be': expected['bytes'],
                    'messages': expected['messages'], 'requests': len(records), 'tokenizer_sha256_matches': True}
        self.check('k1_bundled_tokenizer_context_counts_are_exact_o200k', exact)

    # g1: goals through the native routes -------------------------------------------------------

    @staticmethod
    def item(snapshot, key, ident):
        return next((entry for entry in snapshot[key] if entry['id'] == ident), None)

    def goal(self, service):
        return service.rpc('goal_get', chat_id=self.need('g:cid'), goal_id=self.need('goal'))

    def scenario_goals(self, service):
        cid = self.facts['g:cid'] = service.rpc('new_chat')['id']

        def created():
            goal = service.rpc('goal_create', chat_id=cid, title=GOAL_TITLE)
            assert (goal['origin'], goal['state'], goal['revision'], goal['authorization']) == ('human', 'active', 1, 'none'), goal
            gid = goal['id']
            human = service.rpc('goal_criterion_add', chat_id=cid, goal_id=gid, revision=1, text=CRITERION_HUMAN)
            evidence = service.rpc('goal_criterion_add', chat_id=cid, goal_id=gid, revision=human['revision'],
                                   text=CRITERION_EVIDENCE, kind='evidence')
            first = service.rpc('goal_step_add', chat_id=cid, goal_id=gid, revision=evidence['revision'], title=STEP_ONE,
                                criteria=[evidence['changed']])
            second = service.rpc('goal_step_add', chat_id=cid, goal_id=gid, revision=first['revision'], title=STEP_TWO,
                                 depends_on=[first['changed']])
            criteria = [(c['id'], c['kind'], c['status'], c['origin']) for c in second['criteria']]
            steps = [(s['id'], s['depends_on'], s['criteria'], s['status'], s['ready']) for s in second['steps']]
            assert criteria == [('c1', 'human_check', 'open', 'human'), ('c2', 'evidence', 'open', 'human')], criteria
            assert steps == [('s1', [], ['c2'], 'pending', True), ('s2', ['s1'], [], 'pending', False)], steps
            assert second['revision'] == 5 and not second['blockers'], (second['revision'], second['blockers'])
            self.facts['goal'] = gid
            return {'revision': 5, 'criteria': [c[:2] for c in criteria], 'steps': [s[:3] for s in steps]}
        if not self.check('g1_goal_created_with_human_and_evidence_criteria_and_dependent_steps', created):
            return
        gid = self.facts['goal']

        def cycle():
            loop = self.refusal(service, 'goal_step_edit', chat_id=cid, goal_id=gid, revision=5, step_id='s1', depends_on=['s2'])
            itself = self.refusal(service, 'goal_step_edit', chat_id=cid, goal_id=gid, revision=5, step_id='s2',
                                  depends_on=['s2'])
            assert 'cannot form a cycle' in loop and 'cannot depend on itself' in itself, (loop, itself)
            goal = self.goal(service)
            assert goal['revision'] == 5 and [s['depends_on'] for s in goal['steps']] == [[], ['s1']], goal['revision']
            return {'cycle': 'refused', 'self_dependency': 'refused', 'revision_after': 5}
        self.check('g1_a_dependency_cycle_is_refused_and_nothing_changes', cycle)

        def not_done():
            message = self.refusal(service, 'goal_state', chat_id=cid, goal_id=gid, revision=5, state='done')
            assert message.startswith('This goal is not done yet:'), message[:300]
            for reason in ('criterion c1 is open (your check)', 'criterion c2 is open (waiting for step s1)'):
                assert reason in message, (reason, message[:300])
            goal = self.goal(service)
            assert (goal['state'], goal['revision']) == ('active', 5), (goal['state'], goal['revision'])
            return {'refusal': message[:200], 'state_after': 'active'}
        self.check('g1_done_is_refused_with_the_hosts_reasons', not_done)

        def stale():
            messages = [self.refusal(service, 'goal_criterion_check', chat_id=cid, goal_id=gid, revision=4,
                                     criterion_id='c1', met=True),
                        self.refusal(service, 'goal_state', chat_id=cid, goal_id=gid, revision=4, state='paused'),
                        self.refusal(service, 'goal_step_add', chat_id=cid, goal_id=gid, revision=1, title='Late step')]
            assert all('This goal changed (revision 5). Refresh it before editing.' == m for m in messages), messages
            goal = self.goal(service)
            assert (goal['revision'], goal['state'], self.item(goal, 'criteria', 'c1')['status'], len(goal['steps'])) == (
                5, 'active', 'open', 2)
            return {'refused': ['goal_criterion_check', 'goal_state', 'goal_step_add'], 'revision_after': 5}
        self.check('g1_stale_revisions_are_refused', stale)

    # x1-x3: UpdateGoal exposure ------------------------------------------------------------------

    def scenario_exposure(self, service):
        def small():
            cid = service.rpc('new_chat')['id']
            goal = service.rpc('goal_create', chat_id=cid, title=SMALL_GOAL_TITLE)
            service.rpc('goal_criterion_add', chat_id=cid, goal_id=goal['id'], revision=1, text=CRITERION_HUMAN)
            service.rpc('model_select', profile_id=self.need('profile:4k'))
            try:
                fact = self.flow(service, cid, 'x1')
            finally:
                service.rpc('model_select', profile_id=self.need('profile:wide'))
            records = self.provider.matching(scenario='x1')
            assert records and all(r['model'] == MODEL_4K for r in records), (len(records), fact['state'])
            assert not [r for r in records if 'UpdateGoal' in r['tools']], 'UpdateGoal was offered at 4096.'
            forms = [r['host_goal'] for r in records]
            assert all(g and g['form'] == 'small' for g in forms), forms
            assert all(g['chars'] <= SMALL_GOAL_PAYLOAD and not g['names_update_goal'] for g in forms), forms
            assert all((g['payload'] or {}).get('goal_id') == goal['id'] for g in forms)
            linked = service.rpc('run_get', id=cid, run_id=fact['rid'])['goal']
            assert linked and linked['id'] == goal['id'], 'The 4K run was not linked to its goal.'
            return {'budget': CONTEXT_4K, 'requests': len(records), 'update_goal_offered': 0,
                    'host_goal_payload_chars': max(g['chars'] for g in forms), 'run_linked': True}
        self.check('x1_update_goal_is_never_offered_at_4096_and_the_host_goal_message_is_the_small_form', small)

        def full():
            cid, gid = self.need('g:cid'), self.need('goal')
            fact = self.facts['a0'] = self.flow(service, cid, 'a0')
            assert fact['state'] == 'completed', fact['state']
            records = self.provider.matching(scenario='a0')
            assert records and all(r['model'] == MODEL_WIDE and 'UpdateGoal' in r['tools'] for r in records), len(records)
            assert all(r['update_goal_pinned'] is True for r in records), 'UpdateGoal schema bytes changed.'
            forms = [r['host_goal'] for r in records]
            assert all(g and g['form'] == 'large' and g['names_update_goal'] for g in forms), forms
            payload = forms[-1]['payload'] or {}
            assert (payload.get('goal_id'), payload.get('title'), payload.get('candidates_waiting')) == (gid, GOAL_TITLE, 0)
            assert [c['id'] for c in payload['criteria']] == ['c1', 'c2'] and [s['id'] for s in payload['steps']] == ['s1', 's2']
            receipts = [t for t in fact['run']['tools'] if t['name'] == 'UIRender' and t['state'] == 'completed']
            assert len(receipts) == 1 and self.provider.receipt('a0', 'a0-0-render') == receipts[0]['call_id'], receipts
            assert fact['rid'] in self.goal(service)['runs'], 'a0 is not linked to the goal.'
            self.facts['foreign'] = {'run_id': fact['rid'], 'call_id': receipts[0]['call_id']}
            return {'budget': CONTEXT_WIDE, 'requests': len(records), 'update_goal_schema_sha256': sha(UPDATE_GOAL_SCHEMA),
                    'host_goal': 'large', 'payload_chars': forms[-1]['chars']}
        self.check('x2_update_goal_and_the_full_host_goal_message_are_offered_above_4096_with_a_current_goal', full)

        def absent():
            plain = self.facts['x3'] = self.flow(service, service.rpc('new_chat')['id'], 'x3')
            cid = service.rpc('new_chat')['id']
            goal = service.rpc('goal_create', chat_id=cid, title=PAUSED_GOAL_TITLE)
            paused = service.rpc('goal_state', chat_id=cid, goal_id=goal['id'], revision=1, state='paused')
            assert paused['state'] == 'paused', paused['state']
            held = self.flow(service, cid, 'x4')
            records = {tag: self.provider.matching(scenario=tag) for tag in ('x3', 'x4')}
            assert all(records.values()) and all(r['model'] == MODEL_WIDE for rs in records.values() for r in rs)
            offered = [r for rs in records.values() for r in rs if 'UpdateGoal' in r['tools'] or r['host_goal']]
            assert not offered, [(r['scenario'], r['step']) for r in offered]
            for fact in (plain, held):
                assert service.rpc('run_get', id=fact['cid'], run_id=fact['rid'])['goal'] is None, fact['cid']
            after = service.rpc('goal_get', chat_id=cid, goal_id=goal['id'])
            assert after['state'] == 'paused' and after['runs'] == [], (after['state'], after['runs'])
            self.need('foreign')  # the same 32K route offered both with a current goal (x2)
            return {'without_goal_requests': len(records['x3']), 'paused_goal_requests': len(records['x4']),
                    'update_goal_offered': 0, 'host_goal_messages': 0, 'paused_goal_linked_runs': 0}
        self.check('x3_update_goal_and_host_goal_are_absent_without_a_goal_or_with_a_paused_goal', absent)

    # a1: UpdateGoal authority --------------------------------------------------------------------

    def a1_results(self):
        """The host result the scripted model saw for each a1 step, by its purpose."""
        return {what: self.provider.seen('a1', f'a1-{index}-{kind}') or {}
                for index, (kind, what) in enumerate(SCRIPTS['a1'])}

    def scenario_authority(self, service):
        cid, gid = self.need('g:cid'), self.need('goal')

        def own_receipts():
            foreign = self.need('foreign')
            fact = self.facts['a1'] = self.flow(service, cid, 'a1')
            assert fact['state'] == 'completed', fact['state']
            seen = self.a1_results()
            own = seen['evidence'].get('receipt_id')
            assert own in {t['call_id'] for t in fact['run']['tools'] if t['name'] == 'UIRender' and t['state'] == 'completed'}
            assert f"receipt {foreign['call_id']} was not found in its run" in str(seen['foreign'].get('error')), seen['foreign']
            assert 'Completed steps need resolved, current receipts from this run.' in str(seen['no_receipts'].get('error'))
            assert (seen['complete'].get('status'), seen['complete'].get('steps')) == (
                'updated', [{'id': 's1', 'status': 'completed'}]), seen['complete']
            goal = self.goal(service)
            step = self.item(goal, 'steps', 's1')
            assert (step['status'], step['evidence'], step['evidence_current']) == (
                'completed', [{'run_id': fact['rid'], 'call_id': own}], True), step
            evidence = self.item(goal, 'criteria', 'c2')
            assert (evidence['status'], evidence['checked_by'], evidence['evidence']) == (
                'met', 'host', [{'run_id': fact['rid'], 'call_id': own}]), evidence
            return {'own_receipt': 'accepted', 'other_runs_real_receipt': 'refused_not_in_its_run',
                    'no_receipts': 'refused', 'evidence_criterion': 'met_by_host'}
        if not self.check('a1_update_goal_completes_a_step_only_with_receipts_from_its_own_run', own_receipts):
            return

        def no_authority():
            seen = self.a1_results()
            assert 'Agents cannot add steps' in str(seen['add_step'].get('error')), seen['add_step']
            for key in ('extra_fields', 'goal_state'):
                assert 'does not match its allowed schema' in str(seen[key].get('error')), (key, seen[key])
            records = self.provider.matching(scenario='a1')
            assert records and all(r['update_goal_pinned'] is True for r in records)
            assert not [n for r in records for n in r['tools'] if NATIVE_SHAPED.match(n)], 'A native goal route was offered.'
            goal = self.goal(service)
            human = self.item(goal, 'criteria', 'c1')
            assert [s['id'] for s in goal['steps']] == ['s1', 's2'] and goal['state'] == 'active', goal['state']
            assert (human['status'], human['checked_by']) == ('open', None), human
            last = service.rpc('get_chat', id=cid)['messages'][-1]
            assert last['role'] == 'assistant' and MODEL_COMMAND_TEXT in last['content'], 'The model text was changed.'
            return {'add_step': 'refused', 'criteria_and_state_fields': 'refused_by_schema', 'native_goal_route': 'refused',
                    'typed_goal_done_in_model_text': 'text_only', 'goal_state_after': 'active'}
        self.check('a1_the_model_cannot_add_steps_check_criteria_or_finish_the_goal', no_authority)

        def review():
            seen, a1 = self.a1_results(), self.need('a1')
            assert (seen['candidate'].get('status'), seen['candidate'].get('candidates')) == ('updated', ['c3']), seen['candidate']
            goal = self.goal(service)
            candidate = self.item(goal, 'criteria', 'c3')
            assert (candidate['status'], candidate['origin'], candidate['kind'], candidate['proposed_run_id']) == (
                'candidate', 'agent', 'human_check', a1['rid']), candidate
            waiting = [r['host_goal'] for r in self.provider.matching(scenario='a1')
                       if ((r['host_goal'] or {}).get('payload') or {}).get('candidates_waiting') == 1]
            assert waiting and not [r for r in self.provider.matching(scenario='a1') if (r['host_goal'] or {}).get('candidate_text')]
            refused = self.refusal(service, 'goal_state', chat_id=cid, goal_id=gid, revision=goal['revision'], state='done')
            assert 'accept or reject 1 proposed criterion' in refused and 'criterion c1 is open (your check)' in refused
            assert 'criterion c2' not in refused, refused[:300]
            early = self.refusal(service, 'goal_criterion_check', chat_id=cid, goal_id=gid, revision=goal['revision'],
                                 criterion_id='c3', met=True)
            assert 'Accept this proposed criterion before checking it.' in early, early
            reviewed = service.rpc('goal_criterion_review', chat_id=cid, goal_id=gid, revision=goal['revision'],
                                   criterion_id='c3', decision='reject')
            assert self.item(reviewed, 'criteria', 'c3')['status'] == 'rejected' and reviewed['progress']['candidates'] == 0
            return {'candidate': 'c3', 'host_goal_shows_count_only': True, 'done_refusal': refused[:200],
                    'check_before_review': 'refused', 'person_review': 'rejected'}
        self.check('a1_proposed_criteria_wait_for_the_persons_review', review)

        def records():
            a1, plain = self.need('a1'), self.need('x3')
            linked = service.rpc('run_get', id=cid, run_id=a1['rid'])['goal']
            assert linked and (linked['id'], linked['origin'], linked['authorization']) == (gid, 'human', 'none'), linked
            # Accepted criteria in full; agent proposals and rejected criteria as counts only (X17 F9, merge).
            assert {c['id'] for c in linked['criteria']} == {'c1', 'c2'}, linked['criteria']
            assert (linked['candidates'], linked['rejected_criteria']) == (0, 1), linked
            linked_packet = service.rpc('run_handoff', id=cid, run_id=a1['rid']).get('data') or {}
            unlinked_packet = service.rpc('run_handoff', id=plain['cid'], run_id=plain['rid']).get('data') or {}
            problems = goal_record_problems(linked_packet, unlinked_packet, gid)
            assert not problems, problems
            return {'run_get_goal': gid[:8], 'handoff_goal_record': 'snapshot_not_completion',
                    'unlinked_run_goal_record': 'absent'}
        self.check('a1_run_get_and_handoff_carry_the_linked_goal_record', records)

        def done():
            goal = self.goal(service)
            checked = service.rpc('goal_criterion_check', chat_id=cid, goal_id=gid, revision=goal['revision'],
                                  criterion_id='c1', met=True)
            human, evidence = self.item(checked, 'criteria', 'c1'), self.item(checked, 'criteria', 'c2')
            assert (human['status'], human['checked_by'], evidence['status'], evidence['checked_by']) == (
                'met', 'human', 'met', 'host'), (human, evidence)
            finished = service.rpc('goal_state', chat_id=cid, goal_id=gid, revision=checked['revision'], state='done')
            assert finished['state'] == 'done' and finished['progress']['criteria_met'] == finished['progress']['criteria_total'] == 2
            closed = self.refusal(service, 'goal_criterion_add', chat_id=cid, goal_id=gid, revision=finished['revision'],
                                  text='One more criterion')
            assert 'This goal is done; its record is closed.' in closed, closed
            return {'human_check': 'met_by_person', 'evidence': 'met_by_host', 'state': 'done', 'closed_after_done': True}
        self.check('g1_the_person_checks_the_criteria_then_done_succeeds', done)

        def deleted():
            counts = lambda: {table: self.store_rows(f'SELECT count(*) AS n FROM {table} WHERE {column}=?', (gid,))[0]['n']
                              for table, column in (('goals', 'id'), ('goal_criteria', 'goal_id'), ('goal_steps', 'goal_id'),
                                                    ('goal_events', 'goal_id'))}
            before = counts()
            assert before['goals'] == 1 and all(before.values()), before
            assert service.rpc('delete_chat', id=cid) is True
            listed = self.refusal(service, 'goal_list', chat_id=cid)
            assert 'Conversation not found.' in listed, listed
            after = counts()
            assert not any(after.values()), after
            return {'rows_before': before, 'rows_after': after}
        self.check('g1_deleting_the_conversation_removes_its_goal_records', deleted)

    # e1: exports -------------------------------------------------------------------------------

    def export(self, service, cid, source, fmt):
        plan = self.call(service, 'export_preview', chat_id=cid, source=source, format=fmt)
        return plan, self.call(service, 'export_render', chat_id=cid, plan_id=plan['plan_id'])

    def export_file(self, cid, receipt):
        return (self.need('home') / 'exports' / cid / receipt['export_id'] / receipt['file']['name']).read_bytes()

    def scenario_exports(self, service):
        cid = self.facts['e:cid'] = service.rpc('new_chat')['id']

        def verified():
            fact = self.flow(service, cid, 'e1')
            assert fact['state'] == 'completed', fact['state']
            chat = service.rpc('get_chat', id=cid)
            view = next(a for a in chat['artifacts'] if a['title'] == READINGS_TITLE and a['kind'] == 'table')
            prose = next(m for m in chat['messages'] if m['role'] == 'assistant' and m['content'].startswith(PROSE_HEADING))
            self.facts.update(view=view, prose=prose)
            source = {'kind': 'artifact', 'id': view['id']}
            receipts = {}
            for fmt in ('html', 'csv', 'xlsx'):
                plan, receipt = self.export(service, cid, source, fmt)
                failed = [(c['name'], c['detail']) for c in receipt['checks'] if not c['passed']]
                assert receipt['status'] == 'verified' and not failed, (fmt, receipt['status'], receipt['reason'], failed)
                assert (receipt['published'], receipt['authorization'], receipt['source']) == (False, 'none', plan['source'])
                assert sha(self.export_file(cid, receipt)) == receipt['file']['sha256']
                receipts[fmt] = receipt
            self.facts['exports'] = receipts
            return {fmt: {'status': r['status'], 'checks': sorted(c['name'] for c in r['checks']), 'bytes': r['file']['bytes']}
                    for fmt, r in receipts.items()}
        if not self.check('e1_html_csv_and_xlsx_exports_of_a_table_view_read_back_verified', verified):
            return

        def prose_docx():
            _, receipt = self.export(service, cid, {'kind': 'message', 'id': self.need('prose')['id']}, 'docx')
            failed = [(c['name'], c['detail']) for c in receipt['checks'] if not c['passed']]
            assert receipt['status'] == 'verified' and not failed, (receipt['status'], receipt['reason'], failed)
            names = {c['name'] for c in receipt['checks']}
            assert {'parsed', 'text_coverage', 'no_external_refs', 'no_macros', 'thumbnail'} <= names, sorted(names)
            assert 'tables_preserved' not in names and receipt['published'] is False
            return {'status': 'verified', 'checks': sorted(names), 'text_coverage': receipt['readback']['text_coverage']}
        self.check('e1_prose_docx_export_of_a_message_reads_back_verified', prose_docx)

        def table_docx():
            _, receipt = self.export(service, cid, {'kind': 'artifact', 'id': self.need('view')['id']}, 'docx')
            assert receipt['status'] == 'unverified' and receipt['failed_checks'] == ['tables_preserved'], (
                receipt['status'], receipt['failed_checks'], receipt['reason'])
            assert receipt['file'] and receipt['file']['bytes'] > 0 and receipt['published'] is False
            return {'status': 'unverified', 'failed_checks': ['tables_preserved'], 'file_offered': True}
        self.check('e1_docx_of_a_table_view_is_honestly_unverified_because_tables_are_not_kept', table_docx)

        def pdf():
            events = len(service.events)
            _, receipt = self.export(service, cid, {'kind': 'artifact', 'id': self.need('view')['id']}, 'pdf')
            assert receipt['status'] == 'unavailable' and receipt['file'] is None, (receipt['status'], receipt['file'])
            assert 'not available in this build' in receipt['reason'] and 'Nothing was written' in receipt['reason']
            assert not (self.need('home') / 'exports' / cid / receipt['export_id']).exists(), 'A PDF folder was left.'
            announced = [e for e in service.events[events:] if e.get('event') == 'native_export_pdf']
            assert not announced, 'A native PDF job was announced.'
            attach = self.refusal(service, 'export_pdf_attach', chat_id=cid, export_id=receipt['export_id'],
                                  attach_token='phase17-not-a-token', sha256='0' * 64)
            assert 'No native PDF export is waiting for this file.' in attach, attach
            return {'status': 'unavailable', 'file': None, 'native_pdf_jobs': 0, 'attach_without_job': 'refused'}
        self.check('e1_pdf_is_unavailable_in_this_build_and_writes_nothing', pdf)

        def neutralized():
            receipts = self.need('exports')
            sheet = inspect_csv(self.export_file(cid, receipts['csv']))
            book = inspect_xlsx(self.export_file(cid, receipts['xlsx']))
            assert sheet['byte_order_mark'] and not sheet['unprotected'] and set(sheet['protected']) == FORMULA_TEXT, sheet
            assert PLAIN_NUMBER_TEXT in sheet['plain'], 'A plain number was prefixed.'
            assert book['formulas'] == 0 and not book['unprotected'] and set(book['protected']) == FORMULA_TEXT, book
            assert not book['external_relationships'] and PLAIN_NUMBER_TEXT in book['texts']
            for fmt in ('csv', 'xlsx'):
                passed = {c['name'] for c in receipts[fmt]['checks'] if c['passed']}
                assert 'formulas_neutralized' in passed and (fmt == 'csv' or 'no_formulas' in passed), (fmt, sorted(passed))
            return {'formula_like_cells': len(FORMULA_TEXT), 'csv_apostrophe_prefixed': len(sheet['protected']),
                    'xlsx_quote_prefixed': len(book['protected']), 'xlsx_formula_cells': 0, 'unprotected': 0}
        self.check('e1_hostile_table_formula_text_is_neutralized_in_csv_and_xlsx', neutralized)

        def tokens():
            receipt = self.need('exports')['csv']
            eid, target = receipt['export_id'], self.need('scratch') / 'saved' / 'readings.csv'
            refusals = {
                'forged_token': self.refusal(service, 'export_save', chat_id=cid, export_id=eid, destination_token=uuid.uuid4().hex),
                'path_as_token': self.refusal(service, 'export_save', chat_id=cid, export_id=eid, destination_token=str(target)),
                'inside_pebble_data': self.refusal(service, 'export_destination_bind', chat_id=cid, export_id=eid,
                                                   path=str(self.need('home') / 'readings.csv'), replace=False),
            }
            other = self.call(service, 'export_destination_bind', chat_id=cid, export_id=self.need('exports')['xlsx']['export_id'],
                              path=str(self.need('scratch') / 'saved' / 'readings.xlsx'), replace=False)
            refusals['token_for_another_export'] = self.refusal(service, 'export_save', chat_id=cid, export_id=eid,
                                                                destination_token=other['destination_token'])
            assert all('Choose where to save again with the Save dialog. Nothing was written.' in refusals[key]
                       for key in ('forged_token', 'path_as_token', 'token_for_another_export')), refusals
            assert "outside Pebble's own data" in refusals['inside_pebble_data'], refusals['inside_pebble_data']
            assert not os.path.lexists(target) and not os.path.lexists(self.need('home') / 'readings.csv')
            # As export_choose_destination calls it after the Save dialog: exactly these keys, then only the token returns.
            keys = self.need('shell')['bind_keys']
            params = dict(zip(keys, (cid, eid, str(target), False)))
            assert list(params) == ['chat_id', 'export_id', 'path', 'replace'], keys
            bound = self.call(service, 'export_destination_bind', **params)
            assert set(bound) == {'destination_token', 'expires_at', 'name'} and bound['name'] == target.name, sorted(bound)
            saved = self.call(service, 'export_save', chat_id=cid, export_id=eid, destination_token=bound['destination_token'])
            self.facts['saved'] = {'result': saved, 'token': bound['destination_token'], 'target': target, 'receipt': receipt}
            return {'refused': sorted(refusals), 'bound_keys': keys, 'saved': True}
        if not self.check('e1_export_save_works_only_with_a_token_from_export_destination_bind', tokens):
            return

        def copy():
            facts = self.need('saved')
            target, receipt = facts['target'], facts['receipt']
            data, stat = target.read_bytes(), target.stat()
            assert facts['result'] == {'path': str(target), 'sha256': receipt['file']['sha256'], 'verified': True,
                                       'published': False}, facts['result']
            assert sha(data) == receipt['file']['sha256'], 'The saved copy differs from the verified export.'
            reused = self.refusal(service, 'export_save', chat_id=cid, export_id=receipt['export_id'],
                                  destination_token=facts['token'])
            assert 'Choose where to save again with the Save dialog. Nothing was written.' in reused, reused
            assert target.read_bytes() == data and target.stat().st_mtime_ns == stat.st_mtime_ns, 'The saved copy changed.'
            saves = self.call(service, 'export_get', chat_id=cid, export_id=receipt['export_id'])['saves']
            listed = self.call(service, 'export_list', chat_id=cid)
            assert [(s['state'], s['verified']) for s in saves] == [('saved', True)], saves
            assert listed and all(entry['published'] is False for entry in listed), listed
            return {'sha256_matches': True, 'published': False, 'reused_token': 'refused', 'saves': 1,
                    'exports_listed': len(listed)}
        self.check('e1_saved_copy_hash_matches_it_is_not_published_and_a_reused_token_is_refused', copy)

    # c1: host commands -------------------------------------------------------------------------

    def scenario_commands(self, service):
        cid = self.need('e:cid')
        texts = ('/goal "' + COMMAND_GOAL + '"', '/criteria add "' + COMMAND_CRITERION + '"', '/goal', '/criteria',
                 '/blockers', '/exports')

        def no_inference():
            view = self.need('view')
            self.facts['exports_before'] = len(self.call(service, 'export_list', chat_id=cid))
            self.facts['folders_before'] = sorted(p.name for p in (self.need('home') / 'exports' / cid).iterdir())
            before, runs = self.provider.count(), len(service.rpc('run_list', id=cid))
            replies = {}
            for text in texts + (f'/export {view["id"]} html',):
                result = service.rpc('send', id=cid, text=text)
                assert result.get('command') is True and 'run_id' not in result, (text, result)
                replies[text] = service.rpc('get_chat', id=cid)['messages'][-1]['content']
            assert self.provider.count() == before, ('Commands reached the provider.', self.provider.count() - before)
            assert len(service.rpc('run_list', id=cid)) == runs, 'A command started a run.'
            assert COMMAND_GOAL in replies['/goal'] and 'Criteria: 0/1 met.' in replies['/goal'], replies['/goal'][:300]
            assert COMMAND_CRITERION in replies['/criteria'] and replies['/blockers'] == 'No host-recorded blockers.'
            assert all(r['export_id'] in replies['/exports'] for r in self.need('exports').values())
            self.facts['export_reply'] = replies[f'/export {view["id"]} html']
            return {'commands': [t.split()[0] for t in texts] + ['/export'], 'provider_requests': 0, 'runs_started': 0}
        if not self.check('c1_goal_criteria_blockers_and_exports_commands_make_no_provider_request', no_inference):
            return

        def preview_only():
            reply = self.need('export_reply')
            assert 'This is a preview only. Nothing was written, saved or published.' in reply, reply[:300]
            plan = re.search(r'Export preview `([0-9a-f]{32})`', reply)
            assert plan and 'html' in reply, reply[:300]
            assert len(self.call(service, 'export_list', chat_id=cid)) == self.need('exports_before'), 'A record was added.'
            assert sorted(p.name for p in (self.need('home') / 'exports' / cid).iterdir()) == self.need('folders_before')
            missing = self.refusal(service, 'export_get', chat_id=cid, export_id=plan.group(1))
            assert 'Export not found in this conversation.' in missing, missing
            return {'records_added': 0, 'folders_added': 0, 'plan_is_not_an_export': True}
        self.check('c1_export_command_previews_only_and_writes_saves_or_publishes_nothing', preview_only)

    # n0-n3: notifications and lifecycle -------------------------------------------------------------

    def delivery(self, notice_id):
        rows = self.store_rows('SELECT state,reason,banner_id,banner_version FROM native_deliveries WHERE notification_id=?',
                               (notice_id,))
        return rows[0] if rows else None

    def fire_job(self, service, cid, tag, label, mute=False):
        """A native once-job a few seconds ahead; wakes the scheduler with did_wake, then waits for its notice."""
        at = int(time.time()) + 2
        schedule = {'kind': 'once', 'at': at}
        pin = service.rpc('job_preview', id=cid, schedule=schedule, limits=JOB_LIMITS)
        job = service.rpc('job_create', id=cid, name=label, task=JOB_TASK + ' ' + MARK + tag, schedule=schedule,
                          monitor=False, limits=JOB_LIMITS, config_fingerprint=pin['config_fingerprint'])
        if mute:
            self.facts['muted'] = self.call(service, 'notification_mute', job_id=job['id'], muted=True)
        while time.time() < at + 0.3:
            time.sleep(0.1)
        self.facts.setdefault('wakes', []).append(self.call(service, 'lifecycle_event', kind='did_wake'))
        deadline = time.monotonic() + JOB_SECONDS
        while True:
            packet = service.rpc('job_list', id=cid)
            notices = [n for n in packet['notifications'] if n.get('job_id') == job['id']]
            if notices:
                break
            if time.monotonic() > deadline:
                raise AssertionError(f'The {tag} job left no notice: ' + json.dumps(
                    [{k: j.get(k) for k in ('state', 'error', 'last_run_state')} for j in packet['jobs'] if j['id'] == job['id']]))
            time.sleep(0.25)
        view = next(j for j in packet['jobs'] if j['id'] == job['id'])
        self.observations['run_states'][tag] = view['last_run_state']
        return {'job': job, 'notice': notices[0], 'run_state': view['last_run_state'], 'notices': packet['notifications']}

    def scenario_notifications(self, service):
        cid = service.rpc('new_chat')['id']

        def default_off():
            status = self.call(service, 'notification_status')
            assert status['settings'] == DEFAULT_NOTIFICATIONS and status['actions'] == ['open'], status['settings']
            fired = self.fire_job(service, cid, 'n0', 'Phase 17 notice while off')
            assert fired['run_state'] == 'completed' and fired['notice']['kind'] == 'completed', fired['notice']
            batch = self.call(service, 'notification_native_pending')
            assert batch == {'post': [], 'remove': [], 'quiet_hours': False, 'enabled': False}, batch
            row = self.delivery(fired['notice']['id'])
            assert row and (row['state'], row['reason']) == ('suppressed', 'disabled'), row
            self.facts['n0'] = fired
            return {'settings': 'default_off', 'preview': 'title', 'notice_in_app': True, 'banner': 'none_disabled'}
        self.check('n1_notification_settings_default_off_and_a_notice_while_off_posts_no_banner', default_off)

        def enabled():
            assert service.rpc('setting', key='notifications', value=SETTINGS_ON) is True
            assert self.call(service, 'notification_status')['settings'] == SETTINGS_ON
            fired = self.fire_job(service, cid, 'n1', 'Phase 17 notice one')
            assert fired['run_state'] == 'completed', fired['run_state']
            batch = self.call(service, 'notification_native_pending')
            notice = fired['notice']
            assert batch['enabled'] is True and batch['quiet_hours'] is False and batch['remove'] == [], batch
            assert batch['post'] == [{'id': notice['id'], 'version': 1, 'thread': fired['job']['id'], 'title': notice['title'],
                                      'body': '', 'chat_id': cid, 'count': 1, 'actions': ['open']}], batch['post']
            self.facts['banner'] = batch['post'][0]
            return {'posts': 1, 'banner_id_is_notice_id': True, 'body': 'title_only', 'actions': ['open'],
                    'earlier_notice_while_off_posted': False}
        if not self.check('n1_after_enabling_a_finished_job_reaches_native_pending_as_a_title_only_open_only_banner', enabled):
            return

        def idempotent():
            post = self.need('banner')
            ack = lambda version: self.call(service, 'notification_native_delivered',
                                            delivered=[{'id': post['id'], 'version': version}])
            first, again, ahead = ack(1), ack(1), ack(2)
            assert first == {'delivered': 1, 'removed': 0, 'failed': 0, 'ignored': 0}, first
            assert again == {'delivered': 0, 'removed': 0, 'failed': 0, 'ignored': 1}, again
            assert ahead == {'delivered': 0, 'removed': 0, 'failed': 0, 'ignored': 1}, ahead
            malformed = self.refusal(service, 'notification_native_delivered', delivered=[{'id': post['id'], 'version': 0}])
            assert 'Acknowledge a delivery as {id, version}.' in malformed, malformed
            assert self.call(service, 'notification_native_pending')['post'] == [], 'A delivered banner was posted again.'
            row = self.delivery(post['id'])
            assert row and row['state'] == 'delivered', row
            return {'first': 'delivered', 'repeat': 'ignored', 'newer_version': 'ignored', 'malformed': 'refused',
                    'reposted': False}
        self.check('n1_native_delivered_is_idempotent_by_id_and_version', idempotent)

        def muted():
            fired = self.fire_job(service, cid, 'n2', 'Phase 17 muted notice', mute=True)
            assert self.need('muted') == {'job_id': fired['job']['id'], 'muted': True}
            batch = self.call(service, 'notification_native_pending')
            assert batch['post'] == [], batch['post']
            row = self.delivery(fired['notice']['id'])
            assert row and (row['state'], row['reason']) == ('suppressed', 'muted'), row
            kept = [n for n in service.rpc('job_list', id=cid)['notifications'] if n['id'] == fired['notice']['id']]
            assert kept, 'The muted job\'s in-app notice was dropped.'
            listed = {j['job_id']: j['muted'] for j in self.call(service, 'notification_status')['jobs']}
            assert listed.get(fired['job']['id']) is True, listed
            return {'in_app_notice': 'kept', 'banner': 'suppressed_muted', 'status_lists_mute': True}
        self.check('n1_a_muted_schedule_keeps_its_in_app_notice_and_posts_no_banner', muted)

        def quiet():
            now = datetime.now(timezone.utc)
            window = {'start': (now - timedelta(hours=1)).strftime('%H:%M'), 'end': (now + timedelta(hours=1)).strftime('%H:%M'),
                      'timezone': 'UTC'}
            assert service.rpc('setting', key='notifications', value={**SETTINGS_ON, 'quiet_hours': window}) is True
            fired = self.fire_job(service, cid, 'n3', 'Phase 17 quiet notice')
            held = self.call(service, 'notification_native_pending')
            status = self.call(service, 'notification_status')
            row = self.delivery(fired['notice']['id'])
            assert held['post'] == [] and held['quiet_hours'] is True, held
            assert status['quiet_now'] is True and status['deferred'] == 1, (status['quiet_now'], status['deferred'])
            assert row and row['state'] == 'pending' and row['banner_id'] is None, row
            assert service.rpc('setting', key='notifications', value=SETTINGS_ON) is True  # quiet hours are over
            released = self.call(service, 'notification_native_pending')
            assert [p['id'] for p in released['post']] == [fired['notice']['id']], released['post']
            ack = self.call(service, 'notification_native_delivered',
                            delivered=[{'id': released['post'][0]['id'], 'version': released['post'][0]['version']}])
            assert ack['delivered'] == 1 and self.delivery(fired['notice']['id'])['state'] == 'delivered', ack
            return {'during_quiet_hours': {'posts': 0, 'deferred': 1, 'delivery': 'pending'},
                    'after_quiet_hours': {'posts': 1, 'delivery': 'delivered'}, 'dropped': 0}
        self.check('n1_quiet_hours_defer_a_banner_without_dropping_it', quiet)

        def lifecycle():
            woke = self.need('wakes')[0]
            assert woke.get('accepted') is True and woke.get('kind') == 'did_wake' and re.fullmatch(r'[0-9a-f]{32}', str(
                woke.get('observation_id'))), woke
            extra = self.refusal(service, 'lifecycle_event', kind='did_wake', timezone='UTC')
            unknown = self.refusal(service, 'lifecycle_event', kind='resume')
            assert 'accepts no fields' in extra and 'Unknown lifecycle event.' in unknown, (extra, unknown)
            observed = self.call(service, 'notification_status')['lifecycle']['observations']
            assert any((o['kind'], o['source']) == ('did_wake', 'native') for o in observed), observed[:4]
            clock = (datetime.now(ZoneInfo(DAILY_ZONE)) + timedelta(hours=6)).strftime('%H:%M')
            schedule = {'kind': 'daily', 'time': clock, 'timezone': DAILY_ZONE}
            pin = service.rpc('job_preview', id=cid, schedule=schedule, limits=JOB_LIMITS)
            daily = service.rpc('job_create', id=cid, name='Phase 17 daily check', task=JOB_TASK, schedule=schedule,
                                monitor=False, limits=JOB_LIMITS, config_fingerprint=pin['config_fingerprint'])
            active = [j['id'] for j in service.rpc('job_list', id=cid)['jobs'] if j['state'] == 'active' and j['next_fire']]
            summary = self.call(service, 'lifecycle_quit_summary')
            assert active == [daily['id']], active
            assert summary['active'] == 1 and summary['message'] == (
                f'1 schedule will not fire until Pebble runs; next at {clock} {DAILY_ZONE}.'), summary
            paused = service.rpc('job_pause', id=cid, job_id=daily['id'], revision=daily['revision'])
            quiet_summary = self.call(service, 'lifecycle_quit_summary')
            assert quiet_summary == {'active': 0, 'next_due': None, 'message': None}, quiet_summary
            service.rpc('job_delete', id=cid, job_id=daily['id'], revision=paused['revision'])
            return {'did_wake': 'accepted', 'malformed_events': 'refused', 'quit_message': summary['message'],
                    'after_pause': 'no_confirmation'}
        self.check('n1_lifecycle_did_wake_is_accepted_and_quit_summary_names_the_active_schedules', lifecycle)

    # t1: tokenizer-less service behind the proxy trap -------------------------------------------------

    def scenario_tokenizer(self, scratch):
        def fallback():
            resources = scratch / 'resources-without-tokenizer'
            assert resources.is_dir() and not (resources / 'tokenizer').exists()
            self.trap.start()
            control = self.trap.control()
            assert {'method': 'CONNECT', 'target': TRAP_CONTROL_HOST + ':443'} in control, control
            service = Phase17Service(self.command, scratch / 'home-no-tokenizer', resources=resources,
                                     extra_env=proxy_environment(self.trap.url))
            try:
                launched = procargs(service.process.pid)
                assert all(f'{name}={self.trap.url}'.encode() in launched for name in PROXY_NAMES), 'The trap was not armed.'
                profile = service.rpc('model_save', name='Phase 17 fallback fixture', url=self.provider.url,
                                      model=MODEL_FALLBACK, context=CONTEXT_WIDE)['profile_id']
                service.rpc('model_select', profile_id=profile)
                fact = self.flow(service, service.rpc('new_chat')['id'], 't1')
            finally:
                self.close(service, keep_tail=not self.passing())
            attempts = self.trap.snapshot()
            assert fact['state'] == 'completed', fact['state']
            assert not attempts, ('Outbound attempts reached the proxy trap.', attempts[:4])
            records = self.provider.matching(scenario='t1')
            assert records, 'No t1 request was captured.'
            expected = records[-1]['expected_counts']
            counted = fact['run']['context']['manifest']['input_tokens']['after']
            # Source mode falls back to the checkout's own bundle (token_budget._SOURCE); a frozen bundle has none.
            counter = ('o200k_source_checkout_bundle' if counted == expected['exact'] else
                       'byte_estimate' if counted == expected['bytes'] else None)
            assert counter, (counted, expected)
            return {'resources_tokenizer': 'absent', 'proxy_control_connects': len(control), 'proxy_attempts': 0,
                    'proxy_variables_in_backend_environment': list(PROXY_NAMES), 'counter': counter, 'counted': counted}
        self.check('t1_without_a_bundled_tokenizer_a_model_turn_completes_with_no_outbound_connection', fallback)

    # Campaign-wide ------------------------------------------------------------------------------

    def final_checks(self):
        def native_routes():
            with self.provider.lock:
                offered = {n for r in self.provider.records for n in r['tools']}
            shell = self.facts.get('shell') or {}
            routes = PHASE17_NATIVE_ONLY | GOAL_ROUTES | EXPORT_ROUTES | NOTIFICATION_ROUTES | set(shell.get('allowed', ()))
            leaked = sorted(n for n in offered if n in routes or NATIVE_SHAPED.match(n))
            assert offered and not leaked, leaked
            return {'tool_names_seen': len(offered), 'route_names_compared': len(routes)}
        self.check('native_phase17_routes_are_never_offered_to_models', native_routes)

        def clean():
            with self.provider.lock:
                assert not self.provider.errors, self.provider.errors[-5:]
                assert not self.provider.anomalies, self.provider.anomalies[-5:]
                assert not [r for r in self.provider.records if r['model'] not in MODELS]
                return {'provider_attempts': len(self.provider.records)}
        self.check('synthetic_provider_protocol_clean', clean)


def checkout_digests():
    paths = ['backend/main.py', *[p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'src-tauri/src/main.rs', 'scripts/validate_phase17.py', 'scripts/validate_phase14.py',
             'scripts/validate_phase13.py', 'scripts/validate_runtime.py',
             *[p.relative_to(ROOT).as_posix() for p in sorted(FIXTURES.iterdir()) if p.is_file()]]
    digests = {}
    for path in paths:
        try:
            digests[path] = sha((ROOT / path).read_bytes())
        except OSError:
            digests[path] = None
    return digests


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary homes even on success')
    args = parser.parse_args()
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; move it aside or choose a new path (earlier raw evidence, '
                     'including a failed run, is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    command = [str(backend)] if backend else [sys.executable, str(ROOT / 'backend/main.py')]
    backend_before = sha(backend.read_bytes()) if backend else None
    sources = checkout_digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase17-')).resolve()
    started = time.monotonic()
    campaign = Campaign(command, backend)
    try:
        campaign.run_all(scratch)
    finally:
        duration = round(time.monotonic() - started, 1)
        sources_after = checkout_digests()
        backend_after = sha(backend.read_bytes()) if backend else None
    results = campaign.checks
    changed = sorted(p for p in set(sources) | set(sources_after) if sources.get(p) != sources_after.get(p))
    stable = not changed and backend_after == backend_before
    stability = {'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                 'evidence': {'checkout_files_compared': len(sources), 'executable_compared': bool(backend)}}
    if not stable:
        stability['error'] = ('Backend inputs changed during the campaign: '
                              + json.dumps({'checkout_files': changed[:20],
                                            'executable': bool(backend) and backend_after != backend_before}))
    results.append(stability)
    results.sort(key=lambda c: PLANNED.index(c['name']))
    complete = [c['name'] for c in results] == list(PLANNED)
    passed = complete and campaign.fatal is None and all(c['passed'] for c in results)
    keep = args.keep_scratch or not passed
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    with campaign.provider.lock:
        records = [{k: v for k, v in r.items() if k != 'host_goal'} | {'host_goal_form': (r['host_goal'] or {}).get('form')}
                   for r in campaign.provider.records]
        errors, anomalies = list(campaign.provider.errors), list(campaign.provider.anomalies)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source',
        'passed': passed,
        'checks': [c['name'] for c in results if c['passed']],
        'failed': [c['name'] for c in results if not c['passed'] and 'pending' not in c],
        'pending': [c['name'] for c in results if 'pending' in c],
        'planned_checks': list(PLANNED), 'all_planned_checks_recorded': complete,
        'check_results': results, 'fatal_error': campaign.fatal,
        'observations': campaign.observations,
        'duration_seconds': duration,
        'duration_budget_seconds': VERIFIER_SECONDS,
        'synthetic_provider_attempts': len(records), 'request_metadata': records,
        'provider_errors': errors, 'provider_anomalies': anomalies,
        'proxy_trap_attempts': campaign.trap.snapshot(),
        'backend_sha256': backend_before,
        'backend_path': str(backend) if backend else None,
        'source_sha256': sources,
        'source_note': ('Digests of this checkout. In frozen mode they do not prove the executable was built from '
                        'these files; the candidate manifest is bound separately.'),
        'scratch_preserved': str(scratch) if keep else None,
        'backend_stderr_tail': campaign.stderr_tails if not passed else [],
        'limits': ('Owned loopback synthetic provider, an owned loopback proxy trap (t1 only), temporary homes outside '
                   'the checkout, actual service JSON-RPC launched from the main.rs environment allowlist (t1 adds '
                   'proxy variables only), native main-window routes and the Rust-only routes called as the shell '
                   'calls them. Fixture evidence: no live model, native shell, macOS banner, login item, real sleep '
                   'or time-zone change, WebKit PDF, signed/installed desktop, other platform or frontier evidence, '
                   'and not an independent human security audit. The proxy trap sees only proxy-honoring clients. '
                   'In source mode the tokenizer-less service still finds the checkout bundle (token_budget._SOURCE); '
                   'the byte fallback is reported by the counter field and proven in-process by a self-check.'),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, 'x', encoding='utf-8') as out:
        out.write(json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']),
                      'failed': report['failed'], 'pending': report['pending'], 'fatal_error': campaign.fatal,
                      'duration_seconds': duration, 'scenario_seconds': campaign.observations.get('scenario_seconds'),
                      'scratch_preserved': report['scratch_preserved']}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())


# Development self-checks, run only by pytest on this file; the campaign never calls
# them. They pin this validator's constants to the checkout, parse main.rs, drive the
# scripted turns with synthetic host results and exercise the tokenizer fallback and
# the proxy trap in process. No service is started except by the explicit-request
# campaign test at the end.

def _backend_path():
    path = str(ROOT / 'backend')
    if path not in sys.path:
        sys.path.insert(0, path)


def _closed_provider():
    provider = Provider()
    provider.server.server_close()
    return provider


def _turn(provider, messages, model=MODEL_WIDE, tools=()):
    return provider.respond({'model': model, 'messages': messages, 'tools': list(tools)}).delta


def _goal(tag):
    return [{'role': 'system', 'content': 'synthetic'}, {'role': 'user', 'content': GOALS.get(tag, 'Task.') + ' ' + MARK + tag}]


def _answer(messages, delta, value):
    messages.append({'role': 'assistant', **delta})
    for call in delta.get('tool_calls', []):
        messages.append({'role': 'tool', 'tool_call_id': call['id'],
                         'content': json.dumps(value(call) if callable(value) else value)})


def test_planned_names_are_unique_and_scripts_cover_every_scenario():
    assert len(PLANNED) == len(set(PLANNED)) and PLANNED[-1] == 'backend_inputs_stable_during_campaign'
    assert set(GOALS) <= set(SCRIPTS) and set(FINALS) <= set(SCRIPTS)
    assert all(re.fullmatch(r'[a-z][0-9]', tag) for tag in SCRIPTS)


def test_pinned_goal_headers_schema_routes_and_triggers_match_this_checkout():
    _backend_path()
    import types
    from pebble import export_worker, goals, notifications, tool_catalogue
    assert (goals.SMALL_HEADER, goals.LARGE_HEADER) == (SMALL_HEADER, LARGE_HEADER)
    assert compact(goals.UPDATE_GOAL_TOOL) == UPDATE_GOAL_SCHEMA
    catalogued = [t for t in tool_catalogue.TOOLS if tool_name(t) == 'UpdateGoal']
    assert len(catalogued) == 1 and compact(catalogued[0]) == UPDATE_GOAL_SCHEMA
    assert set(goals.Goals.methods(types.SimpleNamespace())) == GOAL_ROUTES
    assert set(notifications.NotificationAPI.ROUTES) == NOTIFICATION_ROUTES
    exports_native = {'export_destination_bind', 'export_native_ready', 'export_pdf_attach', 'export_pdf_failed'}
    assert set(notifications.NotificationAPI.NATIVE) | exports_native == PHASE17_NATIVE_ONLY
    assert notifications.DEFAULT_SETTINGS == DEFAULT_NOTIFICATIONS and notifications.CATEGORIES == CATEGORIES
    assert export_worker.TRIGGERS == TRIGGERS and export_worker._NUMBER.pattern == PLAIN_NUMBER.pattern


def test_static_shell_checks_pass_on_this_checkout():
    campaign = Campaign(['unused'], None)
    campaign.provider.server.server_close()
    campaign.trap.server.server_close()
    campaign.static_checks()
    assert [c['name'] for c in campaign.checks] == [n for n in PLANNED if n.startswith('s1_')]
    assert all(c['passed'] for c in campaign.checks), json.dumps([c.get('error') for c in campaign.checks])[:3000]


def test_shell_parser_flags_route_and_settling_violations():
    text = MAIN_RS.read_text(encoding='utf-8')
    facts = shell_facts(text)
    assert set(facts['native_only']) == PHASE17_NATIVE_ONLY | set(LATER_NATIVE_ONLY) and settles(facts['settles_arms'], ' /Goal DONE')
    leaked = text.replace('const ALLOWED: &[&str] = &["state",', 'const ALLOWED: &[&str] = &["state","lifecycle_event",', 1)
    assert set(shell_facts(leaked)['native_only']) & set(shell_facts(leaked)['allowed']) == {'lifecycle_event'}
    pet = text.replace('window.label()=="pet" && !["state",', 'window.label()=="pet" && !["export_save","goal_state","state",', 1)
    assert pet != text and {'export_save', 'goal_state'} <= set(shell_facts(pet)['pet'])
    narrowed = text.replace('Some("done" | "abandon")', 'Some("done")', 1)
    assert narrowed != text and shell_facts(narrowed)['settles_arms']['/goal'] == {'done'}
    announced = text.replace('spawn_backend(app.handle(), &backend)?;',
                             'spawn_backend(app.handle(), &backend)?; let _ = native_call(&backend, "export_native_ready", '
                             'json!({"pdf":true}), Duration::from_secs(5));', 1)
    assert announced != text and shell_facts(announced)['native_ready_mentions'] == 2
    try:
        shell_facts(text.replace('fn settles_goal', 'fn removed_goal', 1))
    except AssertionError as exc:
        assert 'settles_goal' in str(exc)
    else:
        raise AssertionError('A missing settles_goal was not reported.')


def test_authority_script_cites_its_own_and_another_runs_receipts():
    provider = _closed_provider()
    messages = _goal('a0')
    delta = _turn(provider, messages)
    assert delta['tool_calls'][0]['function']['name'] == 'UIRender'
    _answer(messages, delta, {'id': 'v0', 'status': 'ready', 'receipt_id': '1:a0-0-render'})
    assert 'content' in _turn(provider, messages) and provider.receipt('a0', 'a0-0-render') == '1:a0-0-render'
    messages, emitted = _goal('a1'), []
    results = {'goal_state': {'error': 'Tool request does not match its allowed schema.'}}
    while 'tool_calls' in (delta := _turn(provider, messages)):
        call = delta['tool_calls'][0]
        name, args = call['function']['name'], json.loads(call['function']['arguments'])
        emitted.append((name, args))
        value = ({'id': 'v1', 'status': 'ready', 'receipt_id': '1:' + call['id']} if name == 'UIRender'
                 else results.get(name, {'status': 'updated'}))
        _answer(messages, delta, value)
    assert delta['content'] == MODEL_COMMAND_TEXT and not provider.anomalies and not provider.errors
    steps = [json.loads(args['steps']) if 'steps' in args else None for _, args in emitted]
    assert [name for name, _ in emitted] == ['UIRender', 'UpdateGoal', 'UpdateGoal', 'UpdateGoal', 'UpdateGoal',
                                             'goal_state', 'UpdateGoal', 'UpdateGoal']
    assert steps[1] == [{'id': 's9', 'status': 'completed', 'evidence_ids': ['1:a1-0-render']}]
    assert steps[2] == [{'id': 's1', 'status': 'completed', 'evidence_ids': ['1:a0-0-render']}]
    assert steps[3] == [{'id': 's1', 'status': 'completed', 'evidence_ids': []}]
    assert set(emitted[4][1]) == {'steps', 'criteria', 'state'} and emitted[5][1] == {'state': 'done'}
    assert json.loads(emitted[6][1]['candidates']) == [{'text': CANDIDATE_TEXT, 'kind': 'human_check'}]
    assert steps[7] == [{'id': 's1', 'status': 'completed', 'evidence_ids': ['1:a1-0-render']}]
    assert provider.seen('a1', 'a1-5-native') == results['goal_state']
    # Without its own visible receipt the script stops with an anomaly, never a guess.
    blind = _goal('a1')
    blind_provider = _closed_provider()
    _answer(blind, _turn(blind_provider, blind), {'id': 'v2', 'status': 'ready'})
    assert 'content' in _turn(blind_provider, blind) and 'was not visible' in blind_provider.anomalies[-1]['reason']


def test_exports_and_scheduled_scripts_answer_from_their_fixtures():
    provider = _closed_provider()
    messages = _goal('e1')
    delta = _turn(provider, messages)
    args = json.loads(delta['tool_calls'][0]['function']['arguments'])
    assert args['kind'] == 'table' and json.loads(args['data']) == HOSTILE_TABLE and args['title'] == READINGS_TITLE
    _answer(messages, delta, {'id': 'v3', 'status': 'ready', 'receipt_id': '1:e1-0-render'})
    assert _turn(provider, messages)['content'] == PROSE and PROSE.startswith(PROSE_HEADING)
    job = [{'role': 'system', 'content': 'synthetic'}, {'role': 'user', 'content': JOB_TASK + ' ' + MARK + 'n2'}]
    assert _turn(provider, job)['content'] == DEFAULT_FINAL and not provider.anomalies


def test_host_goal_messages_are_classified_by_their_pinned_headers():
    small = SMALL_HEADER + json.dumps({'goal_id': 'g', 'candidates_waiting': 0})
    large = LARGE_HEADER + json.dumps({'goal_id': 'g', 'candidates_waiting': 1})
    assert host_goal([{'role': 'user', 'content': small}])['form'] == 'small'
    found = host_goal([{'role': 'system', 'content': 'x'}, {'role': 'user', 'content': large}])
    assert found['form'] == 'large' and found['names_update_goal'] and found['payload']['candidates_waiting'] == 1
    assert host_goal([{'role': 'user', 'content': 'HOST GOAL changed header\n{}'}])['form'] == 'other'
    assert host_goal([{'role': 'user', 'content': 'No goal here.'}]) is None


def test_expected_counts_match_token_budget_and_the_fallback_never_connects(tmp_path, monkeypatch):
    _backend_path()
    import socket
    from pebble import token_budget
    messages = [{'role': 'system', 'content': 'synthetic'}, {'role': 'user', 'content': 'Phase 17 مرحبا 你好 ' + MARK + 'k1'}]
    tools = [json.loads(UPDATE_GOAL_SCHEMA)]
    expected = expected_counts(messages, tools)
    monkeypatch.setattr(token_budget, '_bundled', [])
    monkeypatch.setattr(token_budget, '_loaded', {})
    assert token_budget.TokenBudget(ROOT / 'src-tauri/resources').count(messages, tools) == expected['exact']
    attempts = []

    def refuse(self, address):
        attempts.append(address)
        raise OSError('Outbound connections are refused in this self-check.')
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(token_budget, '_bundled', [])
    monkeypatch.setattr(token_budget, '_loaded', {})
    monkeypatch.setattr(token_budget, '_SOURCE', str(tmp_path / 'no-source-bundle'))
    budget = token_budget.TokenBudget(tmp_path / 'resources-without-tokenizer')
    assert budget.encoding().name == 'byte_estimate' and budget.count(messages, tools) == expected['bytes']
    assert expected['exact'] < expected['bytes'] and not attempts


def test_goal_record_check_accepts_the_snapshot_and_reports_the_run_text():
    snapshot = {'id': 'g' * 32, 'origin': 'human', 'title': GOAL_TITLE, 'authorization': 'none'}
    section = {'origin': 'per_field', 'recorded_by': 'goals', 'semantics': 'snapshot_not_completion'}
    fixed = goal_record_problems({'goal_record': {**section, 'record': snapshot}}, {'goal': {}}, 'g' * 32)
    assert fixed == []
    # The shape the service produced while make_handoff rebound goal to the run's text (F2): two problems.
    found = goal_record_problems({'goal_record': {**section, 'record': GOALS['a1']}},
                                 {'goal_record': {**section, 'record': GOALS['x3']}}, 'g' * 32)
    assert len(found) == 2 and found[0].startswith('goal_record.record is a str') and 'without a linked goal' in found[1]
    assert goal_record_problems({}, {}, 'g' * 32)[0].startswith("the linked run's packet has no goals goal_record")


def test_proxy_trap_records_a_connect_from_a_proxy_honoring_client():
    trap = ProxyTrap()
    trap.start()
    try:
        assert trap.control() == [{'method': 'CONNECT', 'target': TRAP_CONTROL_HOST + ':443'}]
        assert trap.snapshot() == []
        assert proxy_environment(trap.url) == dict.fromkeys(PROXY_NAMES, trap.url)
    finally:
        trap.stop()


def test_neutralization_inspectors_accept_pebble_writers_and_flag_raw_formulas():
    _backend_path()
    from pebble import export_worker
    columns = HOSTILE_TABLE['columns']
    table = {'type': 'table', 'caption': None, 'columns': [c['label'] for c in columns],
             'rows': [[row[c['id']] for c in columns] for row in HOSTILE_TABLE['rows']]}
    sheet = inspect_csv(export_worker.write_csv(table))
    assert sheet['byte_order_mark'] and not sheet['unprotected'] and set(sheet['protected']) == FORMULA_TEXT
    assert PLAIN_NUMBER_TEXT in sheet['plain']
    book = inspect_xlsx(export_worker.write_xlsx([('Readings', table)]))
    assert book['formulas'] == 0 and not book['unprotected'] and set(book['protected']) == FORMULA_TEXT
    raw = inspect_csv(('﻿' + 'Site\r\n=1+2\r\n-5\r\n').encode())
    assert raw['unprotected'] == ['=1+2'] and raw['protected'] == []
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('xl/styles.xml', '<styleSheet><cellXfs><xf/></cellXfs></styleSheet>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet><sheetData><row><c r="A1"><f>1+2</f></c>'
                         '<c r="A2" t="inlineStr"><is><t>=1+2</t></is></c></row></sheetData></worksheet>')
    crafted = inspect_xlsx(stream.getvalue())
    assert crafted['formulas'] == 1 and crafted['unprotected'] == ['=1+2']


def test_unscripted_private_and_unknown_routes_are_flagged():
    provider = _closed_provider()
    _turn(provider, [{'role': 'user', 'content': 'Nothing scripted here. ' + MARK + 'z9'}])
    assert provider.anomalies[-1]['reason'] == 'unscripted conversation'
    for body in ({'model': 'another-model', 'messages': []},
                 {'model': MODEL_WIDE, 'messages': [{'role': 'user', 'content': 'x', '_pebble_goal': True}]}):
        try:
            provider.respond(body)
        except AssertionError:
            continue
        raise AssertionError('The provider accepted an unexpected request.')


def test_checks_are_planned_unique_and_pending_is_never_passed():
    campaign = Campaign(['unused'], None)
    campaign.provider.server.server_close()
    campaign.trap.server.server_close()
    assert campaign.check('fixture_scratch_created_outside_the_checkout', lambda: campaign.need('missing')) is False
    assert 'Prerequisite' in campaign.checks[-1]['error']
    for bad in ('fixture_scratch_created_outside_the_checkout', 'an_unplanned_check'):
        try:
            campaign.check(bad, lambda: None)
        except AssertionError:
            continue
        raise AssertionError('A duplicate or unplanned check name was accepted.')

    class Missing:
        @staticmethod
        def rpc(method, **params):
            raise RuntimeError('Unknown request.')
    assert campaign.check('n1_native_delivered_is_idempotent_by_id_and_version',
                          lambda: campaign.call(Missing, 'notification_native_delivered')) is False
    assert 'native route is missing: notification_native_delivered' in campaign.checks[-1]['pending']
    campaign.finish()
    recorded = {c['name']: c for c in campaign.checks}
    assert set(recorded) == set(PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert recorded['synthetic_provider_protocol_clean']['not_run'] and not any(c['passed'] for c in campaign.checks)


def _campaign(request, tmp_path, node, *extra):
    import pytest
    if not any(str(arg).endswith('::' + node) for arg in request.config.args):
        pytest.skip('Starts the actual service; name this test explicitly to run it.')
    report, log = tmp_path / 'report.json', tmp_path / 'campaign.log'
    with log.open('w') as sink:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--report', str(report), *extra], cwd=ROOT,
                              env={**os.environ, 'TMPDIR': str(tmp_path)}, stdout=sink, stderr=subprocess.STDOUT,
                              timeout=VERIFIER_SECONDS + 60)
    outcome = json.loads(report.read_text())
    import warnings
    warnings.warn('campaign evidence: ' + json.dumps({
        'seconds': outcome['duration_seconds'], 'pending': outcome['pending'], 'failed': outcome['failed'],
        'scenario_seconds': outcome['observations'].get('scenario_seconds'),
        **{c['name']: c.get('evidence') for c in outcome['check_results'] if c['passed']}}, default=str)[:20000])
    if outcome['failed'] or outcome['pending'] or outcome['fatal_error']:
        warnings.warn('campaign diagnostics: ' + json.dumps({
            'run_states': outcome['observations'].get('run_states'),
            'errors': {c['name']: c.get('error') or c.get('pending') for c in outcome['check_results'] if not c['passed']},
            'anomalies': outcome['provider_anomalies'][-5:], 'provider_errors': outcome['provider_errors'][-5:],
            'stderr_tail': [tail[-2500:] for tail in outcome['backend_stderr_tail']]}, default=str)[:12000])
    assert outcome['kind'] == 'source' and outcome['all_planned_checks_recorded'] is True
    assert outcome['duration_seconds'] < VERIFIER_SECONDS
    return done, outcome


def test_source_campaign_run_on_explicit_request(request, tmp_path):
    """The whole campaign in source mode: every planned check passes and none is pending."""
    done, outcome = _campaign(request, tmp_path, 'test_source_campaign_run_on_explicit_request')
    assert done.returncode == 0 and outcome['passed'] is True and not outcome['pending'], json.dumps(
        {'failed': outcome['failed'], 'pending': outcome['pending'], 'fatal': outcome['fatal_error'],
         'errors': {c['name']: c.get('error') or c.get('pending') for c in outcome['check_results'] if not c['passed']}},
        default=str)[:6000]
    assert outcome['checks'] == list(PLANNED)
