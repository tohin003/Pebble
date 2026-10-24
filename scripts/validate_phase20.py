"""Phase 20 source/frozen JSON-RPC proof: the reply after the host's checks, context offered by need, rewind,
recipe diagnostics, research coverage, recipe promotion, readiness contract v2 and the four fault fixes.

Drives the *actual* service over its JSON-RPC stdio contract, launched from main.rs's environment allowlist
names with HOME and TMPDIR in owned scratch and measure_resources' egress trap as its proxy, with one owned
loopback SSE provider and owned homes outside the checkout. Every model turn is scripted synthetic output;
every approval, rewind and extension change is the native route a person (or the native shell) uses. No
real model, external network, account, vault, Keychain or user data is used.

"4K" is a 4,096-token working budget, the runner's 4K path, on a model with 8,192 tokens of capacity (the
provider client reserves output tokens from the capacity; at a 4,096 capacity an approval round cannot fit).
"8K" is 8,192 of both and "32K" 32,768 of both.

Scenarios (each tagged with an opaque marker, in its own conversation unless noted):

  s1  Static: main.rs lets only the main window reach rewind_preview and rewind_apply and writes their
      surface from the calling window; both are native-only routes, never model tools. c1: the model
      catalogue is the Phase 19 catalogue and the six memory layers are exact.
  t1  Readiness contract v2, no service: a v1 fixture review gives results byte-identical to the Phase 19
      flow through scripts/check_harness_release.py; with the shipped empty trust roots every production
      gate stays unverified, even for validly signed envelopes; throwaway test roots (ssh-keygen keys in
      owned scratch) reach the check only as a function argument, never a flag or variable.
  o1-o9  Context offering: 4K and 8K requests offer exactly the pinned Phase 19 tool lists; above 8K the
      coordination tools wait for the first child (SpawnAgent alone before it), MemoryPropose follows
      learning words and ReadRunEvidence a host message that names it (o5 is o3's second turn).
  a1  32K, Ask mode: an edit plus a test request await approval; the chat holds no model text until the
      decision, and exactly one reply follows it.
  a2  32K: a file the run read changes before its turn-end; the completion check withdraws the draft,
      marked on its streamed attempt, and the repair turn alone is checked and presented.
  a3  4K: the same edit and test request keep the Phase 19 path.
  r1  32K research plan: one replaced coverage note after research steps; r2: none at 8K.
  d1  4K, Auto mode: a reviewed npm-lint recipe's output reaches the model as one counts-only line; d2
      above 4K as a bounded list; a write to the linted file drops it at the next request.
  w1  Auto mode: three edits and a command, then rewind_preview and rewind_apply over RPC from the main
      surface: one file restored, a file the person changed refused, the pet and any other surface
      refused, and nothing run again.
  p1-p6  npm-build: one successful run teaches nothing yet; a second promotes a data-only recipe bound to
      the manifest and lockfile; a changed lockfile makes it stale. npm-typecheck (a lint-kind recipe with
      its own procedure) is promoted only by its second run. A lockfile changed after review refuses the
      run before launch.
  f7  An extension row with a corrupt config and a skill whose snapshot no longer verifies (F7-1, F7-2):
      startup survives, both are disabled and reported, the corrupt rows are kept and neither re-enables.
  f8  An external BEGIN EXCLUSIVE held through a run's settlement (F8-1, F8-2): a status read answers
      while locked, and the run does not stay running once the database is free.

Each check is recorded independently under a planned unique name with content-free evidence; source and
frozen runs record the same names. A check that needs a missing native route (named in integration_gaps)
is recorded as pending, never as passed, and the report then does not pass.
backend_inputs_stable_during_campaign re-hashes the checkout, and in frozen mode the executable too.

Usage:
  python scripts/validate_phase20.py --report PATH [--backend PATH] [--keep-scratch]

--report must be a new file: it is created with O_CREAT|O_EXCL|O_NOFOLLOW, so an existing path or a link
is refused and nothing is overwritten. --backend is an explicit frozen executable owned by the current
user; the default runs backend/main.py from this checkout.

This is fixture evidence on this machine. It is not live-model quality, the native shell or webviews (the
validator reads main.rs and stands in for the shell on the surface it writes), installed or signed desktop
behaviour, a real person's trust key, an independent audit, disk-full, sleep/wake, signals or other
platforms.
"""

import argparse
import base64
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT
from validate_phase13 import bounded, calls, final, history, owned_backend, sha
from validate_phase14 import ENV_ALLOWLIST
from validate_phase19 import LATER_TOOLS, LAYERS, NATIVE_ONLY_18, PHASE18_TOOLS, compact, shell, tool_name
from measure_resources import EgressTrap

BACKEND = ROOT / 'backend'
MAIN_RS = ROOT / 'src-tauri/src/main.rs'
RELEASE_CLI = ROOT / 'scripts/check_harness_release.py'
SSH_KEYGEN = '/usr/bin/ssh-keygen'
MARK = '\N{SECTION SIGN}'
TAG = re.compile(MARK + r'([a-z][0-9])\b')
ASSIGNMENT = 'HOST ASSIGNMENT (task data, no new authorization):\n'
MODEL_4K, MODEL_8K, MODEL_WIDE = 'phase20-4k-fixture', 'phase20-8k-fixture', 'phase20-wide-fixture'
MODELS = frozenset({MODEL_4K, MODEL_8K, MODEL_WIDE})
# key -> (model, capacity, working budget). The runner takes its 4K path from the working budget (<= 4096). The 4K
# profile's model has 8,192 tokens of capacity, so after the provider reserves its output tokens an approval round
# or a read and write after a lint still fit the 4,096-token budget (at a 4,096 capacity they cannot).
PROFILES = {'4k': (MODEL_4K, 8192, 4096), '8k': (MODEL_8K, 8192, 8192), 'wide': (MODEL_WIDE, 32768, 32768)}
TRANSIENT = frozenset({'queued', 'running', 'retry_wait'})
SETTLE_SECONDS = 45
VERIFIER_SECONDS = 240  # verify_harness_candidate.run_validator stops a validator after this
BUDGET_SECONDS = 215    # every scenario ends by here; the report, teardown and the interpreter fit in the rest
LIMITS = {'max_steps': 10, 'max_seconds': 120}
TOKEN = os.urandom(4).hex()
DRAFT_MARK, REPLY_MARK = 'draft-' + TOKEN, 'reply-' + TOKEN
PHASE19_TOOLS = PHASE18_TOOLS  # Phase 19 added no model tool (validate_phase19 pins its catalogue)
# Permission groups for every scenario: all on except screen (off by default; nothing here observes a screen).
TOOLS_ON = {'files': True, 'edits': True, 'tests': True, 'apps': True, 'web': True, 'terminal': True,
            'plugins': True, 'views': True, 'screen': False}
HOST_LINE = 'I prepared the actions shown below. Review them before they run.'  # runner.py's line for no text
H1C = 'The person already sees your reply so far; continue it without repeating it.'  # Phase 19 resume line
COVERAGE_HEAD = 'HOST RESEARCH COVERAGE'  # research.PROGRESS_HEAD starts with it (pinned by the tests)
DIAGNOSTICS_HEAD = 'HOST DIAGNOSTICS'
SMALL_DIAGNOSTICS = re.compile(r'HOST DIAGNOSTICS \(untrusted tool data\): 1 error, 1 warning in 1 current file '
                               r'from npm-lint; the recipe output has the locations\.')
LARGE_DIAGNOSTICS = 'HOST DIAGNOSTICS (the host parsed reviewed recipe output'
REFUSED_AFTER_REVIEW = 'A project manifest or lockfile changed after review'
BUSY_TIMEOUT = 5.0                 # store.py: sqlite3.connect's default busy timeout
OUTAGE_CAP_SECONDS = 50.0          # f8: the external lock is never held longer than this
STATUS_PROBE_SECONDS = 20.0        # f8: how long one status read is observed before it is no answer
SETTLE_AFTER_OUTAGE_SECONDS = 12.0  # f8: the controller's dispatch loop wakes every 5 s; one busy timeout more

# Fixture texts and files.
CODING = 'Fix add in calc.py so it returns the sum.'
LEARN = 'Fix add in calc.py so it returns the sum, and learn from the tests which recipe this project uses.'
DELEGATE = 'Delegate the add fix in calc.py to a child agent, then run the tests.'
HARBOUR = ('Who is the current harbour master of Port Ellery?',
           "In what year was Port Ellery's north breakwater completed?")
GOALS = {
    'o1': CODING, 'o2': CODING, 'o3': CODING, 'o4': LEARN, 'o5': 'Now explain what calc.py does.',
    'o6': 'Summarise notes.md in this project with a helper, then report the summary.',
    'o8': LEARN, 'o9': DELEGATE,
    'a1': 'Fix add in calc.py so it returns the sum, then run the tests.',
    'a2': 'Read notes.md and summarise it in one line.',
    'a3': 'Fix add in calc.py so it returns the sum, then run the tests.',
    'r1': 'Research Port Ellery and cite sources: ' + ' '.join(HARBOUR),
    'r2': 'Research Port Ellery and cite sources: ' + ' '.join(HARBOUR),
    'd1': 'Run the lint recipe and fix the lint error in app.js.',
    'd2': 'Run the lint recipe and fix the lint error in app.js.',
    'w1': 'Update a.txt, b.txt and c.txt, then touch build.stamp.',
    'p1': 'Run the build recipe for this project.', 'p2': 'Run the build recipe for this project.',
    'p3': 'Run the build recipe for this project.', 'p4': 'Run the typecheck recipe for this project.',
    'p5': 'Run the typecheck recipe for this project.', 'p6': 'Run the build recipe for this project.',
    'k1': 'Reply with one short sentence about the lock fixture.',
    'k2': 'Reply with one short sentence after the lock fixture.',
}
CHILD_TASK = 'Summarise notes.md in one line. ' + MARK + 'o7'
DONE = 'Synthetic fixture: the scripted steps finished; host receipts record each outcome.'
DRAFT = 'The fix is staged and the tests are requested (' + DRAFT_MARK + ').\nFINAL: staged'
REPLY = 'The fix is applied and the tests passed (' + REPLY_MARK + ').\nFINAL: done'
REPAIR_DRAFT = 'Notes summary (' + DRAFT_MARK + ').\nFINAL: summarised'
REPAIR_REPLY = 'Notes summary after the fresh read (' + REPLY_MARK + ').\nFINAL: summarised'
RESEARCH_FINAL = ('q1 (the harbour master) could not be verified from page sources. q2 (the breakwater year) could '
                  'not be verified from page sources.')
CALC = 'def add(a, b):\n    return a - b\n'
CALC_FIXED = 'def add(a, b):\n    return a + b\n'
TEST_CALC = ('import unittest\n\nfrom calc import add\n\n\nclass AddTest(unittest.TestCase):\n    def test_add(self):\n'
             '        self.assertEqual(add(1, 2), 3)\n\n\nif __name__ == "__main__":\n    unittest.main()\n')
NOTES = 'Synthetic notes for the Phase 20 fixture.\n'
NOTES_CHANGED = 'Synthetic notes, changed by the person after the read.\n'
LINT_PACKAGE = json.dumps({'name': 'phase20-lint-fixture', 'version': '1.0.0', 'private': True,
                           'scripts': {'lint': 'node lint.js'}}, indent=2) + '\n'
# eslint "stylish" output: the file's absolute path (the snapshot the recipe runs in), then its entries.
LINT_SCRIPT = ("const path = require('path');\n"
               "console.log('');\n"
               "console.log(path.join(process.cwd(), 'app.js'));\n"
               "console.log(\"  1:7  error    'unused' is assigned a value but never used  no-unused-vars\");\n"
               "console.log('  5:1  warning  Unexpected console statement                  no-console');\n"
               "console.log('');\n"
               "console.log('2 problems (1 error, 1 warning)');\n"
               "process.exitCode = 1;\n")
APP_JS = ("const unused = 1;\nfunction greet(name) {\n  return 'hello ' + name;\n}\n"
          "console.log(greet('pebble'));\n")
APP_JS_FIXED = "function greet(name) {\n  return 'hello ' + name;\n}\nmodule.exports = { greet };\n"
BUILD_PACKAGE = json.dumps({'name': 'phase20-build-fixture', 'version': '1.0.0', 'private': True,
                            'scripts': {'build': 'node build.js', 'typecheck': 'node typecheck.js'}}, indent=2) + '\n'
BUILD_SCRIPT = "console.log('phase20 build fixture: built');\n"
TYPECHECK_SCRIPT = "console.log('phase20 typecheck fixture: 0 errors');\n"


def lockfile(note=None):
    package = {'name': 'phase20-build-fixture', 'version': '1.0.0', **({'license': note} if note else {})}
    return json.dumps({'name': 'phase20-build-fixture', 'version': '1.0.0', 'lockfileVersion': 3, 'requires': True,
                       'packages': {'': package}}, indent=2) + '\n'


REWIND_FILES = {'a.txt': ('one\n', 'two\n'), 'b.txt': ('alpha\n', 'beta\n'), 'c.txt': ('gamma\n', 'delta\n')}
PERSON_C = 'changed by the person after the run\n'
SKILLS = {'alpha': '---\nname: phase20-fixture-alpha\ndescription: Phase 20 fixture alpha guidance for the fault '
                   'checks.\n---\nAlpha guidance body.\n',
          'beta': '---\nname: phase20-fixture-beta\ndescription: Phase 20 fixture beta guidance for the fault '
                  'checks.\n---\nBeta guidance body.\n'}
CANARY_TAMPER = 'tampered-skill-' + TOKEN
PROJECTS = {
    'offer': {'calc.py': CALC, 'test_calc.py': TEST_CALC, 'notes.md': NOTES},
    'a1': {'calc.py': CALC, 'test_calc.py': TEST_CALC}, 'a3': {'calc.py': CALC, 'test_calc.py': TEST_CALC},
    'a2': {'notes.md': NOTES},
    'd1': {'package.json': LINT_PACKAGE, 'lint.js': LINT_SCRIPT, 'app.js': APP_JS},
    'd2': {'package.json': LINT_PACKAGE, 'lint.js': LINT_SCRIPT, 'app.js': APP_JS},
    'w1': {name: texts[0] for name, texts in REWIND_FILES.items()},
    'p': {'package.json': BUILD_PACKAGE, 'build.js': BUILD_SCRIPT, 'typecheck.js': TYPECHECK_SCRIPT,
          'package-lock.json': lockfile()},
}

# The Phase 19 offer of each pinned request: the exact tool names in order and the SHA-256 of the canonical JSON
# of the tools array the provider receives. 4K and 8K routes are untouched by Phase 20 (runner.large_needs runs
# above 8192 only); backend/tests/test_validate_phase20.py recomputes every pin in-process with
# runner.LARGE_BY_NEED empty, which is test_context_reductions_20's definition of "the Phase 19 offer, exactly".
_CODING_OFFER = ('SearchFiles', 'AskUserQuestion', 'UpdatePlan', 'ReadRunEvidence', 'Glob', 'Read', 'Grep', 'Write',
                 'Append', 'Edit', 'RunTests', 'RunCommand')
PHASE19_OFFERS = {
    'o1': {'profile': '4k', 'project': 'offer', 'tools': _CODING_OFFER,
           'sha256': '94f888007ab18a44a86ccf0e4222be34b93067d8542372a10d1759be56642919'},
    'o9': {'profile': '4k', 'project': 'offer', 'tools': _CODING_OFFER + ('ListProjectRecipes', 'SpawnAgent', 'ReadAgentTree'),
           'sha256': '4749751222ecaca9a8615471884d1f69fc5c6fd139a002676f6fe6853db76e51'},
    'o2': {'profile': '8k', 'project': 'offer', 'tools': _CODING_OFFER + ('CodeReferences',),
           'sha256': '9bf8d83321fa6b57bc6f3436eba8387c8d5d500a4bf8ff38b9baab9482bc998b'},
    'o8': {'profile': '8k', 'project': 'offer',
           'tools': ('SearchFiles', 'MemoryPropose', 'AskUserQuestion', 'UpdatePlan', 'ReadRunEvidence', 'Glob', 'Read',
                     'Grep', 'Write', 'Append', 'Edit', 'RunTests', 'RunRecipe', 'RunCommand', 'ListProjectRecipes',
                     'CodeReferences'),
           'sha256': 'ce4957c92fda34da27bc39e9e8aded51f253193938429adeb33852b13ebe9b08'},
}

GAPS = {
    'routes': 'Parent patch: the Phase 20 rewind routes (rewind_preview, rewind_apply) in app.py dispatch, '
              'tool_authz.NATIVE_ONLY and main.rs ALLOWED with the shell-written surface.',
}

# Scenario -> its planned checks, in campaign order.
SCENARIOS = {
    'static': (
        's1_rewind_routes_are_main_window_only_in_the_shell_and_never_model_tools',
        'c1_no_model_tool_is_added_and_the_six_memory_layers_are_exact',
    ),
    'contract': (
        't1_a_v1_review_gives_results_byte_identical_to_the_phase19_flow',
        't1_with_the_shipped_empty_roots_every_production_gate_stays_unverified',
        't1_test_roots_reach_the_check_only_through_the_harness_never_a_flag_or_variable',
    ),
    'setup': ('loopback_profiles_saved_and_permissions_set',),
    'offering': (
        'o1_at_4096_and_8192_tokens_requests_offer_exactly_the_phase19_tool_lists',
        'o2_above_8192_coordination_tools_wait_for_the_first_child',
        'o3_above_8192_memory_propose_and_read_run_evidence_appear_by_need',
    ),
    'approvals': (
        'a1_while_an_edit_and_a_test_await_the_decision_the_chat_holds_no_model_text',
        'a1_exactly_one_reply_follows_the_decision',
        'a2_a_gate_repair_withdraws_the_draft_and_marks_its_attempt',
        'a2_the_repair_turn_alone_is_checked_and_presented_as_the_reply',
        'a3_at_4096_tokens_the_phase19_reply_path_is_unchanged',
    ),
    'research': (
        'r1_above_8192_a_planned_run_carries_one_replaced_coverage_note',
        'r2_at_8192_tokens_no_coverage_note_appears',
    ),
    'diagnostics': (
        'd1_at_4096_tokens_lint_diagnostics_are_one_counts_only_line',
        'd2_above_4096_tokens_lint_diagnostics_are_a_bounded_list',
        'd3_a_changed_file_drops_its_diagnostics',
    ),
    'rewind': (
        'w1_rewind_preview_and_apply_from_the_main_surface_restore_a_file',
        'w1_the_rewind_routes_refuse_the_pet_and_every_other_surface',
        'w1_a_file_the_person_changed_is_refused_and_kept',
        'w1_a_rewind_runs_nothing_again',
    ),
    'recipes': (
        'p1_one_successful_build_run_does_not_promote_a_recipe',
        'p2_a_second_successful_build_run_promotes_a_data_only_recipe',
        'p3_a_changed_lockfile_makes_the_learned_recipe_stale',
        'p4_two_typecheck_runs_promote_the_typecheck_procedure_after_the_second',
        'p5_a_lockfile_changed_after_review_refuses_the_run_before_launch',
    ),
    'extension_faults': (
        'f7_1_startup_survives_a_corrupt_extension_config_and_reports_it_disabled',
        'f7_2_a_skill_whose_snapshot_no_longer_verifies_is_disabled_and_reported_at_startup',
        'f7_the_corrupt_rows_are_kept_and_neither_extension_can_be_enabled_again',
    ),
    'lock_faults': (
        'f8_2_a_status_read_under_a_held_lock_answers_without_a_database_error',
        'f8_1_the_run_does_not_stay_running_once_the_database_is_free',
    ),
}
COMMON_FIRST = ('fixture_scratch_created_outside_the_checkout',)
COMMON_LAST = ('native_phase20_routes_are_never_offered_to_models', 'synthetic_provider_protocol_clean',
               'no_proxy_honouring_egress_was_attempted', 'backend_inputs_stable_during_campaign')
PLANNED = COMMON_FIRST + tuple(name for checks in SCENARIOS.values() for name in checks) + COMMON_LAST
# The longest each scenario may need (frozen cold starts included). A scenario starts only if its bound still fits
# BUDGET_SECONDS, so a slow machine records it as not run instead of overrunning the verifier's limit.
SCENARIO_BOUNDS = {'static': 10, 'contract': 30, 'setup': 50, 'offering': 45, 'approvals': 45, 'research': 25,
                   'diagnostics': 35, 'rewind': 25, 'recipes': 45, 'extension_faults': 50, 'lock_faults': 80}


class Pending(AssertionError):
    """A check waiting for a named parent patch; never a pass."""

    def __init__(self, gap, detail=''):
        super().__init__(GAPS[gap] + (' ' + detail if detail else ''))
        self.gap = gap


def backend_path():
    path = str(BACKEND)
    if path not in sys.path:
        sys.path.insert(0, path)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def tools_digest(tools):
    """SHA-256 of the canonical JSON of an offered tools array (as the provider receives it)."""
    return sha(canonical(json.loads(json.dumps(tools or []))))


def route(messages):
    """(scenario tag, role) of one request: a child carries the host assignment; a root's tag is the last one in
    its human turns (host-written context and recalled memory never count)."""
    for message in messages:
        content = message.get('content')
        if message.get('role') == 'user' and isinstance(content, str) and content.startswith(ASSIGNMENT):
            try:  # the host writes the assignment as ASCII JSON, so the mark arrives escaped
                task = json.loads(content[len(ASSIGNMENT):].split('\n', 1)[0]).get('task')
            except (ValueError, AttributeError):
                task = None
            found = TAG.findall(task) if isinstance(task, str) else []
            return (found[-1] if found else None), 'child'
    tag = None
    for message in messages:
        content = message.get('content')
        if (message.get('role') == 'user' and isinstance(content, str)
                and not content.startswith(('HOST ', 'RETRIEVED MEMORY'))):
            found = TAG.findall(content)
            if found:
                tag = found[-1]
    return tag, 'root'


def write_projects(scratch):
    """The fixture projects, each in its own folder under the scratch root; returns {key: path}."""
    made = {}
    for key, files in PROJECTS.items():
        folder = scratch / ('project-' + key)
        folder.mkdir()
        for name, text in files.items():
            (folder / name).write_text(text, encoding='utf-8')
        made[key] = folder
    return made


def write_new(path, text):
    """Create the report; an existing path (or a link) is refused, never overwritten."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as out:
        out.write(text)


class View:
    """What one scripted turn may react to: its own calls and their host results, and host-written inputs."""

    def __init__(self, provider, tag, role, index, messages):
        self.provider, self.tag, self.role, self.index, self.messages = provider, tag, role, index, messages
        self.calls = history(messages)
        self.results = {c['id']: c['result'] for c in self.calls}
        with provider.lock:
            self.done = {c['id'] for c in self.calls} | set(provider.emitted.get((tag, role), ()))
        users = [m.get('content') for m in messages if m.get('role') == 'user' and isinstance(m.get('content'), str)]
        self.host = [text for text in users if text.startswith('HOST ')]

    def result(self, ident):
        found = self.results.get(ident)
        return found if isinstance(found, dict) else {}

    def resumed(self):
        return any(text.startswith('HOST RESUME') for text in self.host)

    def repairing(self):
        return any(text.startswith('HOST COMPLETION CHECK') for text in self.host)


class Provider:
    """Owned loopback SSE provider. It records each request's offered tools (names and digest) and the host
    messages Phase 20 adds, and answers from the script registered for the request's (tag, role)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.records, self.errors, self.anomalies = [], [], []
        self.scripts, self.counts, self.emitted = {}, {}, {}
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    delta, usage = provider.respond(body)
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
                    self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
                    self.wfile.write(b'data: ' + json.dumps({'choices': [], 'usage': usage}).encode() + b'\n\n')
                    self.wfile.write(b'data: [DONE]\n\n')
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name='phase20-provider', daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def script(self, tag, fn, role='root'):
        with self.lock:
            self.scripts[(tag, role)] = fn

    def mark(self, tag, role, idents):
        with self.lock:
            self.emitted.setdefault((tag, role), set()).update(idents)

    def anomaly(self, tag, role, index, reason):
        with self.lock:
            self.anomalies.append({'scenario': tag, 'role': role, 'index': index, 'reason': reason[:300]})

    def matching(self, **match):
        with self.lock:
            return [dict(r) for r in self.records if all(r.get(k) == v for k, v in match.items())]

    def respond(self, body):
        model = body.get('model')
        if model not in MODELS:
            raise AssertionError('Unexpected model route reached the fixture.')
        raw = json.dumps(body, ensure_ascii=False)
        if '_pebble_' in raw:
            raise AssertionError('Host-private message fields reached the provider.')
        messages = body.get('messages') or []
        tag, role = route(messages)
        users = [m.get('content') for m in messages if m.get('role') == 'user' and isinstance(m.get('content'), str)]
        with self.lock:
            index = self.counts.get((tag, role), 0)
            self.counts[(tag, role)] = index + 1
            script = self.scripts.get((tag, role))
        view = View(self, tag, role, index, messages)
        record = {'scenario': tag, 'role': role, 'model': model, 'index': index,
                  'tools': [tool_name(t) for t in body.get('tools') or [] if tool_name(t)],
                  'tools_sha256': tools_digest(body.get('tools')),
                  'seen_calls': sorted(view.results),
                  'diagnostics': [t for t in users if t.startswith(DIAGNOSTICS_HEAD)],
                  'coverage': [t for t in users if t.startswith(COVERAGE_HEAD)],
                  'completion_checks': [t for t in view.host if t.startswith('HOST COMPLETION CHECK')],
                  'resume': [t for t in view.host if t.startswith('HOST RESUME')],
                  'names_evidence': [t[:48] for t in users if 'ReadRunEvidence' in t and t.startswith('HOST ')],
                  'drafts_seen': DRAFT_MARK in raw, 'time': time.monotonic()}
        with self.lock:
            self.records.append(record)
        if script is None:
            self.anomaly(tag, role, index, 'unscripted request')
            delta = final('Synthetic fixture: no script for this request.')
        else:
            delta = script(view)
        with self.lock:
            record['emitted'] = ([c['function']['name'] for c in delta['tool_calls']]
                                 if 'tool_calls' in delta else 'content')
        return delta, {'prompt_tokens': max(1, len(raw) // 16), 'completion_tokens': 30}

    def sequence(self, tag, groups, finish, role='root'):
        """Register a script that emits each group of calls once, in order, then answers `finish`."""
        def script(view):
            for group in groups:
                if group[0][1] in view.done:
                    continue
                items = [(name, ident, args(view) if callable(args) else args) for name, ident, args in group]
                self.mark(tag, role, [ident for _, ident, _ in items])
                return calls(*items)
            return final(finish(view) if callable(finish) else finish)
        self.script(tag, script, role)
        return script


class Locker:
    """A second SQLite connection on a home's database that holds BEGIN EXCLUSIVE until released, never longer
    than `cap` seconds."""

    def __init__(self, path, cap):
        self.path, self.cap = Path(path), cap
        self.acquired, self.released, self.release_now = threading.Event(), threading.Event(), threading.Event()
        self.facts = {}

    def acquire(self, wait=20.0):
        threading.Thread(target=self._hold, name='phase20-locker', daemon=True).start()
        if not self.acquired.wait(wait):
            raise AssertionError('The external lock was not acquired: ' + str(self.facts.get('error')))

    def release(self):
        self.release_now.set()

    def _hold(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            started = time.monotonic()
            db.execute('BEGIN EXCLUSIVE')
            self.facts['acquired_at'] = time.monotonic()
            self.facts['acquire_seconds'] = round(self.facts['acquired_at'] - started, 3)
            self.acquired.set()
            self.release_now.wait(self.cap)
            db.execute('ROLLBACK')
        except Exception as exc:
            self.facts['error'] = bounded(exc, 300)
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()
            self.facts['released_at'] = time.monotonic()
            if 'acquired_at' in self.facts:
                self.facts['held_seconds'] = round(self.facts['released_at'] - self.facts['acquired_at'], 2)
            self.released.set()


class Service:
    """One owned backend over JSON-RPC stdio, launched from main.rs's environment allowlist names with HOME and
    TMPDIR in owned scratch plus the egress trap's proxy variables. Events are kept with arrival times."""

    def __init__(self, command, home, scratch, label, extra_env=None):
        self.label, self.events, self.responses = label, [], {}
        self.cond = threading.Condition()
        self.stopped, self.starting = False, True
        self.facts = {'label': label}
        scratch.mkdir(mode=0o700, exist_ok=True)
        for folder in ('user-home', 'tmp'):
            (scratch / folder).mkdir(mode=0o700, exist_ok=True)
        env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
        env.update(HOME=str(scratch / 'user-home'), TMPDIR=str(scratch / 'tmp'), **(extra_env or {}))
        self.stderr_path = scratch / (label + '.stderr.log')
        self.stderr = open(self.stderr_path, 'x+', encoding='utf-8')
        self.started = time.monotonic()
        self.process = subprocess.Popen([*command, '--home', str(home), '--resources', str(ROOT / 'src-tauri/resources')],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True,
                                        env=env, cwd=str(scratch))
        threading.Thread(target=self._read, name='phase20-service-reader', daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                item = json.loads(line)
                with self.cond:
                    if item.get('event'):
                        self.events.append({**item, 'at': time.monotonic()})
                    else:
                        self.responses[item.get('id')] = item
                    self.cond.notify_all()
        except (ValueError, OSError) as exc:
            self.facts['protocol_error'] = bounded(exc, 300)
        finally:
            with self.cond:
                self.stopped = True
                self.cond.notify_all()

    def stderr_tail(self, limit=3000):
        with contextlib.suppress(Exception):
            self.stderr.flush()
            return self.stderr_path.read_text(encoding='utf-8', errors='replace')[-limit:]
        return ''

    def rpc(self, method, wait=15.0, **params):
        ident = os.urandom(8).hex()
        try:
            self.process.stdin.write(json.dumps({'id': ident, 'method': method, 'params': params}) + '\n')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            raise RuntimeError(f'Backend stopped before {method}; stderr: ' + self.stderr_tail()) from None
        limit = time.monotonic() + (45.0 if self.starting else wait)
        with self.cond:
            while ident not in self.responses:
                if self.stopped:
                    raise RuntimeError(f'Backend stopped during {method}; stderr: ' + self.stderr_tail())
                now = time.monotonic()
                if now >= limit:
                    raise RuntimeError(f'Backend timed out during {method}; stderr: ' + self.stderr_tail())
                self.cond.wait(limit - now)
            item = self.responses.pop(ident)
        if self.starting:
            self.starting = False
            self.facts['startup_seconds'] = round(time.monotonic() - self.started, 2)
        if 'error' in item:
            raise RuntimeError(item['error'])
        return item['result']

    def mark(self):
        with self.cond:
            return len(self.events)

    def since(self, mark, name=None, **data):
        with self.cond:
            found = self.events[mark:]
        return [e for e in found if (name is None or e.get('event') == name)
                and all((e.get('data') or {}).get(k) == v for k, v in data.items())]

    def close(self):
        """Close stdin and wait; a backend that never exits is stopped through its live handle and recorded."""
        started = time.monotonic()
        with contextlib.suppress(Exception):
            self.process.stdin.close()
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.facts['stopped_through_live_handle'] = True
            self.process.kill()
            self.process.wait(timeout=5)
        self.facts['exit_code'] = self.process.returncode
        self.facts['exit_seconds'] = round(time.monotonic() - started, 2)
        with contextlib.suppress(Exception):
            self.process.stdout.close()
        with contextlib.suppress(Exception):
            self.stderr.close()
        return self.facts


class Keys:
    """Throwaway Ed25519 keys and SSHSIG signatures from the system ssh-keygen, in owned scratch: HOME is that
    folder, PATH is /usr/bin:/bin and no agent socket is passed, so the person's keys are never consulted."""

    def __init__(self, base):
        self.base = base
        (base / 'home').mkdir(parents=True, mode=0o700)
        self.env = {'HOME': str(base / 'home'), 'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}
        self.count = 0

    def run(self, *args):
        done = subprocess.run([SSH_KEYGEN, *args], env=self.env, cwd=self.base, capture_output=True, timeout=60)
        if done.returncode:
            raise AssertionError('ssh-keygen failed: ' + done.stderr.decode('utf-8', 'replace')[:300])
        return done

    def make(self, name):
        self.run('-q', '-N', '', '-t', 'ed25519', '-C', 'pebble-phase20-validator-' + name, '-f', str(self.base / name))

    def pub(self, name):
        return (self.base / (name + '.pub')).read_text()

    def sign(self, name, data):
        self.count += 1
        target = self.base / f'payload-{self.count}'
        target.write_bytes(data)
        self.run('-Y', 'sign', '-n', 'pebble-attestation@1', '-f', str(self.base / name), str(target))
        return Path(str(target) + '.sig').read_text()


def phase19_check_release(*, review, reports_root, source_root=None, source_snapshot=None, candidate_root=None,
                          candidate_manifest=None, artifact_root=None, test_log=None, evaluation_reports=(),
                          native_app=None, executable=None, require_suites=(), min_backend_tests=None,
                          signing_identity_status='unknown', fixture=False, platform=None, runner=None, tools=None):
    """The Phase 19 release_readiness.check_release, step for step, before contract v2 (the reference the v1
    byte-identity check compares with; backend/tests/test_attestation.py keeps the same copy)."""
    backend_path()
    from pebble import release_readiness as rr
    if signing_identity_status not in ('available', 'absent', 'unknown'):
        raise ValueError('signing_identity_status must be available, absent or unknown.')
    if min_backend_tests is not None and not rr._count(min_backend_tests):
        raise ValueError('min_backend_tests must be a non-negative integer.')
    run = rr.Assessment(bool(fixture), rr.PLATFORM if platform is None else platform)
    run.gates['macos_distribution_signing'].observed['operator_reported_identity'] = signing_identity_status
    try:
        for suite in require_suites:
            if not (isinstance(suite, str) and rr.SUITE.fullmatch(suite)):
                raise rr.EvidenceError('required_suite_invalid')
        review_doc = run.guard(run.load_review, review)
        candidate = None
        if review_doc is not None:
            coverage = run.guard(rr.read_coverage, review_doc.get('source_coverage'))
            run.guard(run.check_snapshot, review_doc, source_snapshot)
            run.guard(run.check_internal, review_doc)
            log = run.guard(run.load_test_log, test_log) if test_log is not None else None
            run.guard(run.check_backend_tests, review_doc, log, min_backend_tests)
            run.guard(run.check_reports, review_doc, reports_root, tuple(require_suites))
            run.guard(run.check_artifacts, review_doc, artifact_root)
            if coverage is not None:
                run.guard(run.check_source, review_doc, source_root, coverage)
            candidate = run.guard(run.check_candidate, review_doc, candidate_root, candidate_manifest, executable)
        run.guard(run.check_evaluations, list(evaluation_reports))
        run.guard(run.check_native, native_app, candidate, runner, tools)
        run.guard(run.observe_build, review_doc)
    except rr.EvidenceError as error:
        run.malformed.add((error.code, error.subject or ''))
    finally:
        for root in run.roots:
            root.close()
    return run.result()


class Campaign:
    def __init__(self, command, backend):
        self.command, self.backend = command, backend
        self.provider = Provider()
        self.trap = EgressTrap()  # proxy-honouring egress from the backend is refused on loopback and recorded
        self.checks, self.names, self.facts, self.gaps, self.pending_gaps = [], set(), {}, set(), {}
        self.observations = {'run_states': {}, 'scenario_seconds': {}, 'services': [], 'locks': {}}
        self.fatal = None
        self.stderr_tails = {}
        self.services = []
        self.deadline = time.monotonic() + BUDGET_SECONDS

    # Check bookkeeping ---------------------------------------------------------------------

    def check(self, name, fn):
        assert name in PLANNED, 'Unplanned check name: ' + name
        assert name not in self.names, 'Check names must be unique: ' + name
        self.names.add(name)
        try:
            evidence = fn() or {}
            self.checks.append({'name': name, 'passed': True, 'evidence': evidence})
            return True
        except Pending as exc:
            self.gaps.add(exc.gap)
            self.pending_gaps[name] = exc
            self.checks.append({'name': name, 'passed': False, 'pending': str(exc)[:600], 'gap': exc.gap})
            return False
        except Exception as exc:
            self.checks.append({'name': name, 'passed': False, 'error': bounded(exc, 2000),
                                'trace': traceback.format_exc(limit=4)[-1500:]})
            return False

    def finish(self):
        owner = {check: scenario for scenario, names in SCENARIOS.items() for check in names}
        errors = self.observations.get('scenario_errors', {})
        for name in PLANNED:
            if name in self.names or name == 'backend_inputs_stable_during_campaign':
                continue
            self.names.add(name)
            reason = errors.get(owner.get(name)) or self.fatal or 'an earlier step stopped the campaign.'
            self.checks.append({'name': name, 'passed': False, 'not_run': True, 'error': 'Not run: ' + reason[:600]})

    def passing(self):
        return all(c['passed'] for c in self.checks)

    def need(self, key):
        if key not in self.facts:
            raise AssertionError('Prerequisite was not established: ' + key)
        return self.facts[key]

    def scenario(self, name, fn, *args):
        """Run one scenario; an unexpected error is recorded for its checks and the campaign goes on."""
        started = time.monotonic()
        try:
            if started + SCENARIO_BOUNDS[name] > self.deadline:
                raise AssertionError(f'Not started: its {SCENARIO_BOUNDS[name]} s bound no longer fits the campaign\'s '
                                     f'{BUDGET_SECONDS} s budget.')
            fn(*args)
        except Exception as exc:
            self.observations.setdefault('scenario_errors', {})[name] = bounded(exc, 1500)
        finally:
            self.observations['scenario_seconds'][name] = round(time.monotonic() - started, 1)

    # Service helpers ------------------------------------------------------------------------

    def service(self, home, folder, label):
        found = Service(self.command, home, folder, label, extra_env=self.trap.environment())
        self.services.append(found)
        return found

    def stop(self, service):
        if service in self.services:
            self.services.remove(service)
            if not self.passing():
                self.stderr_tails[service.label] = service.stderr_tail()
            self.observations['services'].append(service.close())

    @staticmethod
    def call(service, method, gap='routes', **params):
        """A native route; a route the service does not have is pending, by name."""
        try:
            return service.rpc(method, **params)
        except RuntimeError as exc:
            if str(exc) == 'Unknown request.':
                raise Pending(gap, '(the service answered Unknown request. for ' + method + ')') from None
            raise

    def refused(self, service, method, gap='routes', **params):
        try:
            result = self.call(service, method, gap, **params)
        except Pending:
            raise
        except RuntimeError as exc:
            text = str(exc)
            if text.startswith(('Backend timed out', 'Backend stopped')):
                raise AssertionError('No refusal was observed; the service failed: ' + text[:300]) from None
            return text[:200]
        raise AssertionError(method + ' was accepted: ' + compact(result)[:300])

    @staticmethod
    def rows(home, sql, args=()):
        """A read-only view of a running home's database (the service keeps writing to it)."""
        path = home / 'pebble.sqlite'
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, args)]

    @staticmethod
    def stopped_rows(home, sql, args=()):
        """Reads of a stopped home (a read-only open of a closed WAL database is not portable)."""
        with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite', timeout=10)) as db:
            return db.execute(sql, args).fetchall()

    def until(self, predicate, seconds, what, interval=0.1):
        limit = min(self.deadline + 20, time.monotonic() + seconds)
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() >= limit:
                raise AssertionError('Timed out waiting for ' + what + '.')
            time.sleep(interval)

    def configure(self, service, method, **params):
        """A configuration route, retried (bounded) while a settled run's child workers still release
        (app.py refuses model and permission changes while agents.busy())."""
        limit = time.monotonic() + 20
        while True:
            try:
                return service.rpc(method, **params)
            except RuntimeError as exc:
                if not str(exc).startswith(('Stop the current task', 'Stop the task')) or time.monotonic() > limit:
                    raise
                self.observations['configuration_retries'] = self.observations.get('configuration_retries', 0) + 1
                time.sleep(0.1)

    def permissions(self, service, mode):
        self.configure(service, 'setting', key='permissions', value={'mode': mode, 'tools': dict(TOOLS_ON)})

    def select(self, service, profile):
        self.configure(service, 'model_select', profile_id=self.need('profile:' + profile))

    def send(self, service, cid, tag):
        return service.rpc('send', id=cid, text=GOALS[tag] + ' ' + MARK + tag, limits=LIMITS)['run_id']

    def settle(self, service, cid, rid, tag, decide=None, stop_at_approval=False, pending=TRANSIENT,
               seconds=SETTLE_SECONDS):
        """Wait until the run settles. A run that waits for approval stops here (stop_at_approval) or has each
        pending action decided by `decide` (a person's choice); without it any proposal is unexpected."""
        limit = min(self.deadline + 20, time.monotonic() + seconds)
        while True:
            run = service.rpc('run_get', id=cid, run_id=rid)
            active = service.rpc('state')['active']
            if run['state'] == 'waiting_approval' and not active:
                if stop_at_approval:
                    break
                waiting = [a for a in service.rpc('get_chat', id=cid)['actions'] if a['status'] == 'pending']
                for action in waiting:
                    choice = decide(action) if decide else None
                    if choice != 'approve':
                        self.provider.anomaly(tag, 'root', None, 'unexpected proposal: ' + str(action.get('kind')))
                        service.rpc('reject', id=cid, action_id=action['id'])
                    else:
                        service.rpc('approve', id=cid, action_id=action['id'], wait=60)
                if waiting:
                    continue
                break
            if run['state'] not in pending and not active:
                break
            if time.monotonic() > limit:
                with contextlib.suppress(Exception):
                    service.rpc('cancel', id=cid, run_id=rid)
                raise AssertionError('Timed out waiting for ' + tag + ' to settle: ' + run['state'])
            time.sleep(0.1)
        self.observations['run_states'][tag] = run['state']
        if run.get('error'):
            self.observations.setdefault('run_errors', {})[tag] = str(run['error'])[:400]
        return {'cid': cid, 'rid': rid, 'state': run['state'], 'tag': tag}

    def conversation(self, service, profile, tag, workspace=None, **settle):
        cid = service.rpc('new_chat', **({'workspace': str(workspace)} if workspace else {}))['id']
        self.select(service, profile)
        return self.settle(service, cid, self.send(service, cid, tag), tag, **settle)

    @staticmethod
    def events(service, run, kind=None):
        rows, after = [], 0
        while True:
            page = service.rpc('run_events', id=run['cid'], run_id=run['rid'], after=after, limit=200)
            rows += page
            if len(page) < 200:
                break
            after = page[-1]['sequence']
        return [r for r in rows if kind is None or r['kind'] == kind]

    @staticmethod
    def attempts(service, run):
        """Stream attempts in order: [{'id', 'step', 'state', 'text'}] from stream_read pages."""
        found, after = {}, 0
        while True:
            page = service.rpc('stream_read', id=run['cid'], run_id=run['rid'], after=after, limit=100)
            for event in page['events']:
                item = found.setdefault(event['attempt_id'], {'id': event['attempt_id'], 'step': event['step'],
                                                              'state': 'open', 'text': ''})
                if event['kind'] == 'delta':
                    item['text'] += event['text']
                elif event['kind'] == 'settle':
                    item['state'] = event['state']
            after = page['next_after']
            if not page['more']:
                return list(found.values())

    @staticmethod
    def assistant(chat):
        return [m['content'] or '' for m in chat['messages'] if m.get('role') == 'assistant']

    @staticmethod
    def tokens(service, mark, cid):
        return [e['data'].get('text') or '' for e in service.since(mark, 'token', chat_id=cid)]

    def requests(self, tag, role='root'):
        return self.provider.matching(scenario=tag, role=role)

    # Campaign --------------------------------------------------------------------------------

    def run_all(self, scratch):
        self.facts.update(scratch=scratch, home=scratch / 'home')

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout and .runtime.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            self.facts['projects'] = write_projects(scratch)
            return {'outside_checkout': True, 'projects': len(PROJECTS)}
        self.check('fixture_scratch_created_outside_the_checkout', created)
        try:
            self.scenario('static', self.static_checks)
            self.scenario('contract', self.contract_checks, scratch / 'release')
            self.provider.start()
            self.trap.start()
            service = self.service(self.facts['home'], scratch / 'service-main', 'main')
            try:
                self.scenario('setup', self.setup, service)
                if 'profile:wide' in self.facts:
                    for name, scenario in (('offering', self.scenario_offering), ('approvals', self.scenario_approvals),
                                           ('research', self.scenario_research),
                                           ('diagnostics', self.scenario_diagnostics),
                                           ('rewind', self.scenario_rewind), ('recipes', self.scenario_recipes)):
                        self.scenario(name, scenario, service)
            finally:
                self.stop(service)
            self.scenario('extension_faults', self.scenario_extension_faults)
            self.scenario('lock_faults', self.scenario_lock_faults)
            self.final_checks()
        except Exception as exc:
            self.fatal = bounded(exc, 3000)
        finally:
            for service in list(self.services):
                with contextlib.suppress(Exception):
                    self.stop(service)
            with contextlib.suppress(Exception):
                self.provider.stop()
            with contextlib.suppress(Exception):
                self.trap.stop()
            self.finish()

    # s1 and c1: declarations ----------------------------------------------------------------

    def static_checks(self):
        backend_path()

        def routes():
            from pebble import tool_authz
            from pebble.rewind import ROUTES
            from pebble.tool_catalogue import TOOLS
            facts = self.facts['shell'] = shell(MAIN_RS.read_text(encoding='utf-8'))
            allowed, pet, native = set(facts['allowed']), set(facts['pet']), set(facts['native_only'])
            missing = sorted(set(ROUTES) - allowed) + sorted('NATIVE_ONLY:' + r for r in set(ROUTES) - tool_authz.NATIVE_ONLY)
            if missing:
                raise Pending('routes', 'Missing: ' + ', '.join(missing))
            assert native == NATIVE_ONLY_18, sorted(native ^ NATIVE_ONLY_18)  # Phase 20 adds no shell-only route
            assert not set(ROUTES) & pet, 'The pet window reaches a rewind route: ' + ', '.join(sorted(set(ROUTES) & pet))
            rpc = facts['rpc']
            line = next((text for text in rpc.splitlines()
                         if all(f'method=="{r}"' in text.replace(' ', '') for r in ROUTES) and '"surface"' in text), None)
            assert line and 'window.label()' in line.replace(' ', ''), 'The shell does not write the rewind surface.'
            at = rpc.index(line)
            assert rpc.index('if !ALLOWED.contains(&method.as_str())') < at < rpc.index('backend.call(&method'), \
                'The rewind surface is not written between the route check and the backend call.'
            names = {tool_name(t).casefold() for t in TOOLS}
            assert not names & {r.casefold() for r in ROUTES}, 'A rewind route is a model tool.'
            return {'routes': list(ROUTES), 'main_window_only': True, 'surface_from_window_label': True,
                    'native_only_routes': len(tool_authz.NATIVE_ONLY)}
        self.check('s1_rewind_routes_are_main_window_only_in_the_shell_and_never_model_tools', routes)

        def catalogue():
            from pebble.memory import KINDS
            from pebble.tool_catalogue import TOOLS
            names = [tool_name(t) for t in TOOLS]
            catalogue = PHASE19_TOOLS | LATER_TOOLS  # D72: tools added after Phase 19, each by its phase
            assert len(names) == len(set(names)) and set(names) == catalogue, sorted(set(names) ^ catalogue)
            assert tuple(KINDS) == LAYERS, tuple(KINDS)
            return {'model_tools': len(names), 'layers': list(LAYERS)}
        self.check('c1_no_model_tool_is_added_and_the_six_memory_layers_are_exact', catalogue)

    # t1: readiness contract v2 ----------------------------------------------------------------

    @staticmethod
    def release_arguments(fixture):
        return {key: fixture[key] for key in ('review', 'reports_root', 'source_root', 'candidate_root',
                                              'candidate_manifest', 'artifact_root', 'test_log', 'evaluation_reports')}

    @staticmethod
    def release_cli(arguments, base, extra=(), env=None):
        """scripts/check_harness_release.py exactly as a person runs it, with a minimal environment."""
        argv = [sys.executable, str(RELEASE_CLI), '--review', arguments['review'], '--reports-root',
                arguments['reports_root'], '--source-root', arguments['source_root'], '--candidate-root',
                arguments['candidate_root'], '--candidate-manifest', arguments['candidate_manifest'],
                '--artifact-root', arguments['artifact_root'], '--test-log', arguments['test_log']]
        for path in arguments['evaluation_reports']:
            argv += ['--evaluation-report', path]
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(base), 'LANG': 'en_US.UTF-8', **(env or {})}
        return subprocess.run([*argv, *extra], cwd=str(base), env=environment, capture_output=True, text=True,
                              timeout=90)

    @staticmethod
    def synthetic_app(base, candidate):
        """An owned synthetic .app holding the fixture candidate (its digest completes the subject)."""
        app = base / 'Pebble.app'
        (app / 'Contents' / 'MacOS').mkdir(parents=True)
        (app / 'Contents' / 'Info.plist').write_bytes(b'<plist/>')
        (app / 'Contents' / 'MacOS' / 'pebble').write_bytes(b'\xcf\xfa\xed\xfe synthetic shell')
        (app / 'Contents' / 'Resources' / 'resources' / 'backend').mkdir(parents=True)
        (app / 'Contents' / 'Resources' / 'resources' / 'backend' / 'pebble-backend').write_bytes(candidate.read_bytes())
        return app

    def contract_checks(self, base):
        backend_path()
        from pebble import attestation as at, migrations, release_readiness as rr
        base.mkdir(mode=0o700)

        def v1():
            fixture = rr.build_fixture(base / 'v1')
            arguments = self.release_arguments(fixture)
            done = self.release_cli(arguments, base)
            expected = json.dumps(phase19_check_release(**arguments), indent=2, sort_keys=True) + '\n'
            assert done.stdout == expected, 'The command line result differs from the Phase 19 flow.'
            result = json.loads(done.stdout)
            assert done.returncode == result['exit_code'] and result['contract_version'] == 1
            assert 'attestations' not in result and set(result['gate_versions'].values()) == {1}
            # With a synthetic app too (no native tool runs: tools is empty), the in-process check is byte-identical.
            app = self.synthetic_app(base / 'v1-app', Path(fixture['candidate_root']) / 'pebble-backend')
            with_app = dict(arguments, native_app=str(app), tools={})
            assert json.dumps(rr.check_release(**with_app), sort_keys=True) == \
                json.dumps(phase19_check_release(**with_app), sort_keys=True)
            return {'exit_code': result['exit_code'], 'stdout_sha256': sha(done.stdout), 'compared_bytes': len(expected),
                    'production_ready': result['production_ready']}

        def envelopes():
            """A v2 review, an app, four validly signed envelopes over the computed subject and their test roots."""
            if not os.access(SSH_KEYGEN, os.X_OK):
                raise AssertionError('Needs /usr/bin/ssh-keygen for throwaway keys.')
            fixture = rr.build_fixture(base / 'v2')
            review = json.loads(Path(fixture['review']).read_text())
            Path(fixture['review']).write_text(json.dumps(dict(review, readiness_contract=rr.CONTRACT_V2), indent=2) + '\n')
            app = self.synthetic_app(base / 'v2-app', Path(fixture['candidate_root']) / 'pebble-backend')
            subject = {'source_snapshot_sha256': review['source_snapshot_sha256'],
                       'candidate_executable_sha256': review['candidate']['executable_sha256'],
                       'candidate_bundle_sha256': review['candidate']['bundle_sha256'],
                       'app_bundle_sha256': at.app_bundle_digest(app), 'platform': at.subject_platform()}
            assert all(subject.values()), subject
            keys = Keys(base / 'keys')
            now = datetime.now(timezone.utc)
            issued, roots = now - timedelta(minutes=1), {}
            for role in at.ROLES:
                keys.make(role)
                kid, entry = at.root_entry(keys.pub(role), roles=(role,),
                                           gates=tuple(g for g, r in at.GATE_ROLES.items() if r == role),
                                           not_before='2026-01-01T00:00:00Z', not_after=at.stamp(now + timedelta(days=365)),
                                           label='phase20-validator-' + role)
                roots[kid] = entry
            manifest_payload = at.build_release_payload(
                version='0.5.0', build=21, app_bundle_sha256=subject['app_bundle_sha256'],
                backend_executable_sha256=subject['candidate_executable_sha256'], min_data_epoch=0,
                data_epoch=migrations.SCHEMA_EPOCH, previous=sha('phase20 previous backend'),
                rollback=sha('phase20 backup record'), issued_at=issued, not_after=issued + timedelta(days=7))
            manifest = at.build_envelope(manifest_payload, keys.sign('release', manifest_payload))
            supplied, folder = [], base / 'envelopes'
            folder.mkdir()
            for gate, role in at.GATE_ROLES.items():
                evidence = manifest if gate == 'signed_updates' else canonical({'probe': gate, 'synthetic': True}).encode()
                payload = at.build_payload(gate=gate, subject=subject, evidence_sha256=rr.sha256(evidence),
                                           result='passed', issued_at=issued, not_after=issued + timedelta(days=7))
                envelope_file, evidence_file = folder / (gate + '.envelope.json'), folder / (gate + '.evidence')
                envelope_file.write_bytes(at.build_envelope(payload, keys.sign(role, payload)))
                evidence_file.write_bytes(evidence)
                supplied.append((gate, envelope_file, evidence_file))
            return {'fixture': fixture, 'app': app, 'subject': subject, 'roots': roots, 'supplied': supplied, 'now': now}

        def empty():
            made = self.facts['attested'] = envelopes()
            arguments = self.release_arguments(made['fixture'])
            ledger = base / 'ledger-cli'
            attested = [part for gate, envelope, evidence in made['supplied']
                        for part in ('--attestation', gate, str(envelope), str(evidence))]
            done = self.release_cli(arguments, base, [*attested, '--attestation-ledger', str(ledger)])
            result = json.loads(done.stdout)
            assert result['contract_version'] == 2 and result['production_ready'] is False, result.get('contract_version')
            gates = {gate: result['gates'][gate]['status'] for gate in rr.PRODUCTION_GATES}
            assert set(gates.values()) == {'unverified'}, gates
            assert not ledger.exists(), 'The command line recorded an attestation with the shipped roots.'
            # The same envelopes with the app's digest completing the subject, still the shipped (empty) roots.
            shipped_ledger = base / 'ledger-shipped'
            shipped = rr.check_release(**arguments, native_app=str(made['app']), tools={},
                                       attestations=made['supplied'], attestation_ledger=shipped_ledger,
                                       now=made['now'])
            statuses = {gate: shipped['gates'][gate]['status'] for gate in rr.PRODUCTION_GATES}
            assert set(statuses.values()) == {'unverified'} and shipped['production_ready'] is False, statuses
            codes = sorted({(v.get('verdict') or {}).get('code') for v in shipped['attestations']})
            assert codes == ['unknown_key'], codes
            assert all(not shipped['gates'][gate]['observed']['pinned_keys'] for gate in at.GATE_ROLES)
            assert not shipped_ledger.exists()
            return {'cli_exit_code': done.returncode, 'cli_gates': gates, 'shipped_refusals': codes,
                    'ledger_written': False}
        self.check('t1_a_v1_review_gives_results_byte_identical_to_the_phase19_flow', v1)
        self.check('t1_with_the_shipped_empty_roots_every_production_gate_stays_unverified', empty)

        def harness():
            made = self.facts.get('attested')
            if made is None:
                raise AssertionError('Prerequisite: the signed envelopes of the previous check.')
            arguments = self.release_arguments(made['fixture'])
            helped = subprocess.run([sys.executable, str(RELEASE_CLI), '--help'], capture_output=True, text=True,
                                    timeout=60, env={'PATH': '/usr/bin:/bin', 'HOME': str(base)}, cwd=str(base))
            options = set(re.findall(r'(--[a-z][a-z-]+)', helped.stdout))
            assert '--attestation' in options and '--review' in options, sorted(options)
            assert not [o for o in options if re.search(r'trust|key|revok|pin|signer|allowed', o)], sorted(options)
            assert {o for o in options if o.endswith('-root')} == {'--reports-root', '--source-root', '--candidate-root',
                                                                   '--artifact-root'}, sorted(options)
            # A variable naming the test roots changes nothing: the command line reads only trust_roots.py.
            pins = json.dumps({kid: dict(entry, roles=list(entry['roles']), gates=list(entry['gates']))
                               for kid, entry in made['roots'].items()})
            attested = [part for gate, envelope, evidence in made['supplied']
                        for part in ('--attestation', gate, str(envelope), str(evidence))]
            plain = self.release_cli(arguments, base, [*attested, '--attestation-ledger', str(base / 'ledger-env')])
            variables = {name: pins for name in ('PEBBLE_TRUST_ROOTS', 'PEBBLE_ATTESTATION_ROOTS', 'TRUST_ROOTS')}
            seeded = self.release_cli(arguments, base, [*attested, '--attestation-ledger', str(base / 'ledger-env')],
                                      env=variables)
            assert seeded.stdout == plain.stdout and seeded.returncode == plain.returncode
            assert not (base / 'ledger-env').exists()
            # The harness: the same envelopes with the test roots as a function argument pass all four gates, so the
            # unverified results above are the empty roots' doing, not the envelopes'.
            passed = rr.check_release(**arguments, native_app=str(made['app']), tools={}, attestations=made['supplied'],
                                      attestation_ledger=base / 'ledger-harness', trust=(made['roots'], {}),
                                      now=made['now'])
            gates = {gate: passed['gates'][gate]['status'] for gate in rr.PRODUCTION_GATES}
            assert {gate: gates[gate] for gate in at.GATE_ROLES} == dict.fromkeys(at.GATE_ROLES, 'passed'), gates
            assert {gates[gate] for gate in at.UNATTESTABLE} == {'unverified'} and passed['production_ready'] is False
            assert not (base / 'ledger-harness').exists(), 'A dry check recorded an attestation.'
            return {'options': len(options), 'variables_ignored': sorted(variables), 'harness_gates': gates,
                    'production_ready': passed['production_ready']}
        self.check('t1_test_roots_reach_the_check_only_through_the_harness_never_a_flag_or_variable', harness)

    # Setup ------------------------------------------------------------------------------------

    def setup(self, service):
        def profiles():
            for key, (model, context, budget) in PROFILES.items():
                self.facts['profile:' + key] = service.rpc('model_save', name='Phase 20 ' + key + ' fixture',
                                                           url=self.provider.url, model=model, context=context,
                                                           budget=budget)['profile_id']
            saved = {p['model']: (p['context'], p.get('budget')) for p in service.rpc('model_profiles')}
            assert all(saved.get(model) == (context, budget) for model, context, budget in PROFILES.values()), saved
            self.permissions(service, 'ask')
            settings = service.rpc('state')['settings']
            assert settings['permissions'] == {'mode': 'ask', 'tools': TOOLS_ON}, settings['permissions']
            assert settings['memory_enabled'] is True and all(settings['memory_layers'].values())
            return {'profiles': len(PROFILES), 'permission_groups': len(TOOLS_ON),
                    'startup_seconds': service.facts.get('startup_seconds')}
        self.check('loopback_profiles_saved_and_permissions_set', profiles)

    # o1-o9: context offering -------------------------------------------------------------------

    def script_offering(self):
        for tag in ('o1', 'o2', 'o3', 'o4', 'o5', 'o8', 'o9'):
            self.provider.sequence(tag, [], DONE)
        spawned = self.facts.setdefault('spawned', {})

        def helper(view):
            spawn = view.result('o6-spawn')
            if isinstance(spawn.get('id'), str):
                spawned.update(id=spawn['id'], generation=spawn.get('generation', 1))
            if 'o6-spawn' not in view.done:
                self.provider.mark('o6', 'root', ['o6-spawn'])
                return calls(('SpawnAgent', 'o6-spawn', {'task': CHILD_TASK, 'name': 'Notes helper',
                                                         'tools': json.dumps(['Read']), 'max_steps': 3,
                                                         'max_seconds': 30, 'model_tokens': 100000}))
            if 'id' not in spawned:
                return final('Synthetic fixture: the helper was not spawned.')
            for ident, name, args in (('o6-wait', 'WaitForAgents', {'agent_ids': json.dumps([spawned['id']])}),
                                      ('o6-dismiss', 'ReviewAgentResult', {'agent_id': spawned['id'], 'decision': 'dismiss',
                                                                           'generation': spawned['generation']})):
                if ident not in view.done:
                    self.provider.mark('o6', 'root', [ident])
                    return calls((name, ident, args))
            return final(DONE)
        self.provider.script('o6', helper)
        self.provider.sequence('o7', [], 'Synthetic child summary of notes.md.', role='child')

    def scenario_offering(self, service):
        project = self.need('projects')['offer']
        self.permissions(service, 'ask')
        self.script_offering()
        runs = {}
        for tag, profile in (('o1', '4k'), ('o9', '4k'), ('o2', '8k'), ('o8', '8k'), ('o3', 'wide'), ('o4', 'wide')):
            runs[tag] = self.conversation(service, profile, tag, workspace=project)
        self.select(service, 'wide')
        runs['o5'] = self.settle(service, runs['o3']['cid'], self.send(service, runs['o3']['cid'], 'o5'), 'o5')
        runs['o6'] = self.conversation(service, 'wide', 'o6', workspace=project, pending=TRANSIENT | {'waiting_input'})
        nodes = service.rpc('agent_list', chat_id=runs['o6']['cid'], run_id=runs['o6']['rid'])['nodes']
        backend_path()
        from pebble.tool_catalogue import AGENT_ROOT_TOOLS

        def pinned():
            found = {}
            for tag, pin in PHASE19_OFFERS.items():
                requests = self.requests(tag)
                assert requests, 'No request for ' + tag
                record = requests[0]
                assert record['model'] == PROFILES[pin['profile']][0], (tag, record['model'])
                assert pin['tools'] is not None and pin['sha256'] is not None, 'The Phase 19 offer of ' + tag + ' is not pinned.'
                assert tuple(record['tools']) == tuple(pin['tools']), (tag, sorted(set(record['tools']) ^ set(pin['tools'])))
                assert record['tools_sha256'] == pin['sha256'], (tag, 'tool schemas differ from the Phase 19 offer',
                                                                 record['tools_sha256'])
                assert all(r['tools_sha256'] == pin['sha256'] for r in requests), (tag, 'a later request differs')
                found[tag] = {'budget': PROFILES[pin['profile']][2], 'tools': len(pin['tools']), 'requests': len(requests)}
            return {'pinned_requests': found}
        self.check('o1_at_4096_and_8192_tokens_requests_offer_exactly_the_phase19_tool_lists', pinned)

        def first_child():
            plain = [r for tag in ('o3', 'o4', 'o5') for r in self.requests(tag)]
            assert plain and all(r['model'] == MODEL_WIDE for r in plain)
            for record in plain:
                assert set(record['tools']) & AGENT_ROOT_TOOLS == {'SpawnAgent'}, (record['scenario'], record['tools'])
            roots, children = self.requests('o6'), self.requests('o7', 'child')
            assert len(roots) >= 3 and children, ([r.get('emitted') for r in roots], len(children))
            assert set(roots[0]['tools']) & AGENT_ROOT_TOOLS == {'SpawnAgent'}, roots[0]['tools']
            whole = AGENT_ROOT_TOOLS - {'SpawnWorktreeAgent'}  # the Phase 19 large-route set without delegation words
            for record in roots[1:]:
                assert set(record['tools']) & AGENT_ROOT_TOOLS == whole, sorted(whole ^ set(record['tools']))
            changes = sum(a['tools'] != b['tools'] for a, b in zip(roots, roots[1:]))
            assert changes == 1, ('the tool list changed', changes, 'times')
            assert len(nodes) == 1 and nodes[0].get('review') == 'dismissed', [(n.get('status'), n.get('review')) for n in nodes]
            assert runs['o6']['state'] == 'completed', runs['o6']['state']
            return {'requests_without_a_child': len(plain), 'root_requests': len(roots), 'child_requests': len(children),
                    'coordination_tools_before': 1, 'coordination_tools_after': len(whole), 'list_changes': changes}
        self.check('o2_above_8192_coordination_tools_wait_for_the_first_child', first_child)

        def by_need():
            o3, o4, o5 = self.requests('o3')[0], self.requests('o4')[0], self.requests('o5')[0]
            assert not {'MemoryPropose', 'ReadRunEvidence'} & set(o3['tools']), o3['tools']
            assert 'MemoryPropose' in o4['tools'] and 'ReadRunEvidence' not in o4['tools'], o4['tools']
            assert 'ReadRunEvidence' in o5['tools'] and 'MemoryPropose' not in o5['tools'], o5['tools']
            assert o5['names_evidence'] and not o3['names_evidence'], (o5['names_evidence'], o3['names_evidence'])
            small = [self.requests(tag)[0] for tag in ('o1', 'o2', 'o8')]
            assert all('ReadRunEvidence' in r['tools'] for r in small)  # 4K and 8K keep the Phase 19 offer
            assert 'MemoryPropose' in self.requests('o8')[0]['tools'] and 'MemoryPropose' not in self.requests('o2')[0]['tools']
            return {'plain_32k': len(o3['tools']), 'learning_words_32k': 'MemoryPropose',
                    'host_message_names_evidence': o5['names_evidence'][0]}
        self.check('o3_above_8192_memory_propose_and_read_run_evidence_appear_by_need', by_need)

    # a1-a3: the reply after the host's checks -------------------------------------------------

    def script_approvals(self, projects):
        for tag in ('a1', 'a3'):
            self.provider.sequence(tag, [[('Read', tag + '-read', {'file_path': 'calc.py'}),
                                          ('Write', tag + '-write', {'file_path': 'calc.py', 'content': CALC_FIXED})],
                                         [('RunTests', tag + '-tests', {'recipe': 'python-unittest'})]],
                                   lambda view: REPLY if view.resumed() else DRAFT)
        changed = []

        def repair(view):
            if 'a2-read-1' not in view.done:
                self.provider.mark('a2', 'root', ['a2-read-1'])
                return calls(('Read', 'a2-read-1', {'file_path': 'notes.md'}))
            if not view.repairing():
                if not changed:  # the person edits the file the run read, just before the turn ends
                    (projects['a2'] / 'notes.md').write_text(NOTES_CHANGED, encoding='utf-8')
                    changed.append(True)
                return final(REPAIR_DRAFT)
            if 'a2-read-2' not in view.done:
                self.provider.mark('a2', 'root', ['a2-read-2'])
                return calls(('Read', 'a2-read-2', {'file_path': 'notes.md'}))
            return final(REPAIR_REPLY)
        self.provider.script('a2', repair)

    def scenario_approvals(self, service):
        projects = self.need('projects')
        self.permissions(service, 'ask')
        self.script_approvals(projects)
        self.held(service, 'a1', 'wide', projects['a1'])
        self.repaired(service, projects['a2'])
        self.held(service, 'a3', '4k', projects['a3'])
        backend_path()
        from pebble.runner import HELD_RESUME, WITHHELD, WITHHELD_NOTE

        def quiet():
            facts = self.need('held:a1')
            before = facts['before']
            assert before['state'] == 'waiting_approval', before['state']
            assert facts['pending'] == ['edit', 'tests'], facts['pending']
            texts = self.assistant(before['chat'])
            assert all(text == HOST_LINE for text in texts), texts  # only the host's own line
            assert ''.join(before['tokens']) in ('', HOST_LINE), before['tokens']
            assert DRAFT_MARK not in compact(before['chat']) and not any('FINAL:' in t for t in before['tokens'])
            withheld = [e['payload'] for e in before['events'] if e['kind'] == 'reply_withheld']
            assert [(w['reason'], w['attempt_id']) for w in withheld] == [('approval', None)], withheld
            assert not any(DRAFT_MARK in a['text'] for a in before['attempts']), 'A held turn streamed.'
            assert any(r['emitted'] == 'content' for r in self.requests('a1'))  # the model did end its turn
            return {'assistant_messages': len(texts), 'host_line_only': True, 'withheld': 'approval',
                    'pending': facts['pending']}
        self.check('a1_while_an_edit_and_a_test_await_the_decision_the_chat_holds_no_model_text', quiet)

        def one_reply():
            facts = self.need('held:a1')
            after = facts['after']
            assert after['state'] == 'completed', after['state']
            texts = self.assistant(after['chat'])
            replies = [t for t in texts if REPLY_MARK in t]
            assert len(replies) == 1 and replies[0].count(REPLY_MARK) == 1, texts
            finals = [line for t in texts for line in t.splitlines() if line.startswith('FINAL:')]
            assert finals == ['FINAL: done'], finals
            assert HOST_LINE not in texts, 'The host line was not replaced by the reply.'
            assert DRAFT_MARK not in compact(after['chat']) and not any(DRAFT_MARK in t for t in after['tokens'])
            resumed = [r for r in self.requests('a1') if r['resume']]
            assert resumed and all(HELD_RESUME.strip() in r['resume'][-1] for r in resumed), [r['resume'] for r in resumed]
            assert [w['reason'] for w in (e['payload'] for e in after['events'] if e['kind'] == 'reply_withheld')] == ['approval']
            return {'state': after['state'], 'replies': 1, 'final_lines': len(finals), 'resumed_requests': len(resumed),
                    'decision_to_settle_seconds': facts['seconds']}
        self.check('a1_exactly_one_reply_follows_the_decision', one_reply)

        def withdrawn():
            facts = self.need('repair')
            withheld = [e['payload'] for e in facts['events'] if e['kind'] == 'reply_withheld']
            assert [w['reason'] for w in withheld] == ['completion_check'], withheld
            attempt = withheld[0]['attempt_id']
            drafts = [a for a in facts['attempts'] if DRAFT_MARK in a['text']]
            assert len(drafts) == 1 and drafts[0]['id'] == attempt and drafts[0]['state'] == 'committed', \
                ([(a['state'], a['id'] == attempt) for a in drafts], attempt)
            marks = [e['data'] for e in facts['emitted'] if e['event'] == 'stream' and e['data'].get('type') == 'withdrawn']
            assert [m['attempt_id'] for m in marks] == [attempt] and marks[0]['authorization'] == 'none', marks
            labels = [e['data'].get('label') for e in facts['emitted']
                      if e['event'] == 'progress' and e['data'].get('run_id') == facts['run']['rid']]
            assert WITHHELD['completion_check'] in labels, labels[-6:]
            checks = [c for r in self.requests('a2') for c in r['completion_checks']]
            assert checks and all(c.endswith(WITHHELD_NOTE) for c in checks), [c[-120:] for c in checks]
            return {'reason': 'completion_check', 'attempt_state': 'committed', 'withdrawn_events': len(marks),
                    'host_checks_with_note': len(checks)}
        self.check('a2_a_gate_repair_withdraws_the_draft_and_marks_its_attempt', withdrawn)

        def alone():
            facts = self.need('repair')
            assert facts['run']['state'] == 'completed', facts['run']['state']
            texts = self.assistant(facts['chat'])
            replies = [t for t in texts if REPLY_MARK in t]
            assert len(replies) == 1 and replies[0].count(REPLY_MARK) == 1, texts
            assert [line for t in texts for line in t.splitlines() if line.startswith('FINAL:')] == ['FINAL: summarised']
            assert DRAFT_MARK not in compact(facts['chat']) and not any(DRAFT_MARK in t for t in facts['tokens'])
            assert 'Verification remains incomplete' not in compact(texts)
            reads = [t for t in facts['tools'] if t['name'] == 'Read']
            assert len(reads) == 2 and facts['changed'], (len(reads), facts['changed'])
            requests = self.requests('a2')
            assert requests[-1]['emitted'] == 'content' and requests[-1]['completion_checks']
            return {'state': facts['run']['state'], 'replies': 1, 'reads': len(reads), 'requests': len(requests)}
        self.check('a2_the_repair_turn_alone_is_checked_and_presented_as_the_reply', alone)

        def unchanged():
            facts = self.need('held:a3')
            before, after = facts['before'], facts['after']
            assert before['state'] == 'waiting_approval' and facts['pending'] == ['edit', 'tests'], \
                (before['state'], facts['pending'])
            assert any(DRAFT_MARK in t for t in self.assistant(before['chat'])), 'The 4K draft was not presented.'
            assert any(DRAFT_MARK in t for t in before['tokens']), 'The 4K draft was not sent as tokens.'
            events = [e for e in after['events'] if e['kind'] == 'reply_withheld']
            assert not events, events
            requests = self.requests('a3')
            resumed = [r for r in requests if r['resume']]
            assert resumed, ('no resumed request', after['state'], after.get('error'), [r['emitted'] for r in requests])
            assert all(H1C in r['resume'][-1] and HELD_RESUME.strip() not in r['resume'][-1] for r in resumed), \
                [r['resume'][-1][-200:] for r in resumed]
            assert not any(WITHHELD_NOTE in c for r in requests for c in r['completion_checks'])
            assert all(r['model'] == MODEL_4K for r in requests)
            texts = self.assistant(after['chat'])
            assert any(DRAFT_MARK in t and REPLY_MARK in t for t in texts), (after['state'], after.get('error'), texts)
            return {'presented_before_decision': True, 'withheld_events': 0, 'resume_line': 'phase19',
                    'state': after['state']}
        self.check('a3_at_4096_tokens_the_phase19_reply_path_is_unchanged', unchanged)

    def held(self, service, tag, profile, project):
        """An edit and a test request awaiting approval: the state before the decision, then after it."""
        cid = service.rpc('new_chat', workspace=str(project))['id']
        self.select(service, profile)
        mark = service.mark()
        run = self.settle(service, cid, self.send(service, cid, tag), tag, stop_at_approval=True)
        chat = service.rpc('get_chat', id=cid)
        before = {'state': run['state'], 'chat': chat, 'tokens': self.tokens(service, mark, cid),
                  'events': self.events(service, run), 'attempts': self.attempts(service, run)}
        waiting = sorted((a for a in chat['actions'] if a['status'] == 'pending'),
                         key=lambda a: ({'edit': 0, 'tests': 1}.get(a['kind'], 2), a.get('created') or 0))
        decided = time.monotonic()
        for action in waiting:  # the person approves the edit, then the test run
            service.rpc('approve', id=cid, action_id=action['id'], wait=90)
        settled = self.settle(service, cid, run['rid'], tag)
        after = {'state': settled['state'], 'chat': service.rpc('get_chat', id=cid),
                 'tokens': self.tokens(service, mark, cid), 'events': self.events(service, run),
                 'error': (service.rpc('run_get', id=cid, run_id=run['rid']).get('error') or '')[:400]}
        self.facts['held:' + tag] = {'before': before, 'after': after, 'pending': [a['kind'] for a in waiting],
                                     'seconds': round(time.monotonic() - decided, 2)}

    def repaired(self, service, project):
        cid = service.rpc('new_chat', workspace=str(project))['id']
        self.select(service, 'wide')
        mark = service.mark()
        run = self.settle(service, cid, self.send(service, cid, 'a2'), 'a2')
        self.facts['repair'] = {'run': run, 'chat': service.rpc('get_chat', id=cid), 'events': self.events(service, run),
                                'attempts': self.attempts(service, run), 'emitted': service.since(mark),
                                'tokens': self.tokens(service, mark, cid),
                                'tools': service.rpc('run_get', id=cid, run_id=run['rid'])['tools'],
                                'changed': (project / 'notes.md').read_text(encoding='utf-8') == NOTES_CHANGED}

    # r1, r2: research coverage -----------------------------------------------------------------

    def script_research(self):
        for tag in ('r1', 'r2'):
            self.provider.sequence(tag, [[('ResearchPlan', tag + '-plan', {'questions': json.dumps(list(HARBOUR))})],
                                         [('ResearchStatus', tag + '-status-1', {})],
                                         [('ResearchStatus', tag + '-status-2', {})]], RESEARCH_FINAL)

    def scenario_research(self, service):
        self.script_research()
        runs = {}
        for tag, profile in (('r1', 'wide'), ('r2', '8k')):
            runs[tag] = self.conversation(service, profile, tag)
            runs[tag]['notes'] = [e['payload'] for e in self.events(service, runs[tag], 'research_progress')]

        def above():
            requests = self.requests('r1')
            assert len(requests) >= 4 and 'ResearchPlan' in requests[0]['tools'], [r['tools'][:3] for r in requests]
            assert requests[0]['coverage'] == [], 'A note before any research step.'
            assert all(len(r['coverage']) == 1 for r in requests[1:4]), [len(r['coverage']) for r in requests]
            assert all(len(r['coverage']) <= 1 for r in requests), 'The note accumulated.'
            notes = runs['r1']['notes']
            assert len(notes) == 3 and all(set(n) == {'recommendation', 'all_covered', 'covered', 'uncovered',
                                                      'open_conflicts'} for n in notes), notes
            assert all(n['recommendation'] in ('stop', 'continue') for n in notes)
            assert 'http' not in compact(notes) and not any('http' in c for r in requests for c in r['coverage'])
            return {'requests': len(requests), 'notes_recorded': len(notes), 'one_note_per_request': True,
                    'recommendation': notes[-1]['recommendation']}
        self.check('r1_above_8192_a_planned_run_carries_one_replaced_coverage_note', above)

        def at_8k():
            requests = self.requests('r2')
            assert requests and all(r['model'] == MODEL_8K for r in requests)
            assert not any('ResearchPlan' in r['tools'] for r in requests), 'ResearchPlan was offered at 8K.'
            assert all(r['coverage'] == [] for r in requests) and runs['r2']['notes'] == []
            return {'requests': len(requests), 'notes': 0}
        self.check('r2_at_8192_tokens_no_coverage_note_appears', at_8k)

    # d1-d3: recipe diagnostics -----------------------------------------------------------------

    def script_diagnostics(self):
        for tag in ('d1', 'd2'):
            self.provider.sequence(tag, [[('RunRecipe', tag + '-lint', {'recipe': 'npm-lint'})],
                                         [('Read', tag + '-read', {'file_path': 'app.js'}),
                                          ('Write', tag + '-write', {'file_path': 'app.js', 'content': APP_JS_FIXED})]],
                                   DONE)

    def scenario_diagnostics(self, service):
        projects = self.need('projects')
        self.permissions(service, 'auto')
        self.script_diagnostics()
        runs = {}
        for tag, profile in (('d1', '4k'), ('d2', 'wide')):
            runs[tag] = self.conversation(service, profile, tag, workspace=projects[tag], decide=lambda action: 'approve')
            runs[tag]['records'] = [e['payload'] for e in self.events(service, runs[tag], 'recipe_diagnostics')]
            runs[tag]['tools'] = service.rpc('run_get', id=runs[tag]['cid'], run_id=runs[tag]['rid'])['tools']
        self.permissions(service, 'ask')

        def phases(tag):
            """(the request right after the lint, the request after the write) of one run."""
            requests = self.requests(tag)
            lint = next((r for r in requests if tag + '-lint' in r['seen_calls'] and tag + '-write' not in r['seen_calls']), None)
            write = next((r for r in requests if tag + '-write' in r['seen_calls']), None)
            records = runs[tag]['records']
            lint_receipt = next((t for t in runs[tag]['tools'] if t['call_id'].endswith(tag + '-lint')), None)
            assert lint_receipt and isinstance(lint_receipt.get('result'), dict), 'No lint receipt for ' + tag
            assert not lint_receipt['result'].get('error'), lint_receipt['result'].get('error')
            assert len(records) == 1 and records[0]['recipe'] == 'npm-lint' and \
                records[0]['counts'] == {'error': 1, 'warning': 1, 'info': 0}, records
            assert list(records[0]['files']) == ['app.js'] and records[0]['files']['app.js'] == sha(APP_JS)
            assert requests[0]['diagnostics'] == [] and lint is not None and write is not None, \
                [(r['seen_calls'], len(r['diagnostics'])) for r in requests]
            return lint, write

        def small():
            lint, _ = phases('d1')
            assert lint['model'] == MODEL_4K and len(lint['diagnostics']) == 1, lint['diagnostics']
            line = lint['diagnostics'][0]
            assert SMALL_DIAGNOSTICS.fullmatch(line), line
            assert 'app.js' not in line and '\n' not in line  # counts only: the recipe output has the locations
            return {'line_characters': len(line), 'counts': {'error': 1, 'warning': 1}}
        self.check('d1_at_4096_tokens_lint_diagnostics_are_one_counts_only_line', small)

        def large():
            lint, _ = phases('d2')
            assert lint['model'] == MODEL_WIDE and len(lint['diagnostics']) == 1, lint['diagnostics']
            text = lint['diagnostics'][0]
            assert text.startswith(LARGE_DIAGNOSTICS), text[:120]
            body = json.loads(text.split('\n', 1)[1])
            shown = [(d['path'], d['line'], d['column'], d['severity'], d['code']) for d in body['diagnostics']]
            assert shown == [('app.js', 1, 7, 'error', 'no-unused-vars'), ('app.js', 5, 1, 'warning', 'no-console')], shown
            assert body['counts'] == {'error': 1, 'warning': 1, 'info': 0} and body['omitted'] == 0
            assert len(text.split('\n', 1)[1]) <= 5000 + 200  # the bounded list: 5,000 entry characters above 8K
            return {'entries': len(shown), 'body_characters': len(text.split('\n', 1)[1])}
        self.check('d2_above_4096_tokens_lint_diagnostics_are_a_bounded_list', large)

        def stale():
            found = {}
            for tag in ('d1', 'd2'):
                lint, write = phases(tag)
                assert write['diagnostics'] == [], (tag, write['diagnostics'])
                later = [r for r in self.requests(tag) if r['time'] >= write['time']]
                assert all(r['diagnostics'] == [] for r in later), tag
                writes = [t for t in runs[tag]['tools'] if t['call_id'].endswith(tag + '-write')]
                assert writes and isinstance(writes[0].get('result'), dict) and writes[0]['result'].get('action_id')
                found[tag] = {'requests_after_the_write': len(later), 'state': runs[tag]['state']}
            assert (projects['d1'] / 'app.js').read_text(encoding='utf-8') == APP_JS_FIXED
            return found
        self.check('d3_a_changed_file_drops_its_diagnostics', stale)

    # w1: rewind ------------------------------------------------------------------------------------

    def rewind_counts(self, run):
        home = self.facts['home']
        return {'actions': {r['kind']: r['n'] for r in self.rows(home, 'SELECT kind,count(*) n FROM actions GROUP BY kind')},
                'run_tools': self.rows(home, 'SELECT count(*) n FROM run_tools WHERE run_id=?', (run['rid'],))[0]['n'],
                'messages': self.rows(home, 'SELECT count(*) n FROM messages')[0]['n'],
                'run_events': self.rows(home, 'SELECT count(*) n FROM run_events WHERE run_id=?', (run['rid'],))[0]['n']}

    def script_rewind(self):
        groups = [[('Read', f'w1-read-{name[0]}', {'file_path': name}),
                   ('Write', f'w1-write-{name[0]}', {'file_path': name, 'content': texts[1]})]
                  for name, texts in REWIND_FILES.items()]
        groups.append([('RunCommand', 'w1-touch', {'command': 'touch build.stamp'})])
        self.provider.sequence('w1', groups, DONE)

    def scenario_rewind(self, service):
        project = self.need('projects')['w1']
        self.script_rewind()
        self.permissions(service, 'auto')
        run = self.conversation(service, 'wide', 'w1', workspace=project, decide=lambda action: 'approve')
        self.permissions(service, 'ask')
        cid, rid = run['cid'], run['rid']
        stamp = project / 'build.stamp'
        assert stamp.is_file(), 'The command did not run: ' + run['state']
        (project / 'c.txt').write_text(PERSON_C, encoding='utf-8')  # the person edits c.txt after the run
        stamp_before = (stamp.stat().st_mtime_ns, stamp.stat().st_ino)
        requests_before = len(self.requests('w1'))
        counts = self.rewind_counts(run)
        preview = self.call(service, 'rewind_preview', chat_id=cid, run_id=rid, step=1, surface='main')
        after_preview = self.rewind_counts(run)
        refusals = {}
        for label, extra in (('omitted', {}), ('pet', {'surface': 'pet'}), ('Main', {'surface': 'Main'}),
                             ('main_space', {'surface': 'main '}), ('empty', {'surface': ''}), ('view', {'surface': 'view'}),
                             ('null', {'surface': None})):
            refusals['preview:' + label] = self.refused(service, 'rewind_preview', chat_id=cid, run_id=rid, step=1, **extra)
        for label in ('pet', 'view'):
            refusals['apply:' + label] = self.refused(service, 'rewind_apply', chat_id=cid, run_id=rid, step=1,
                                                      digest=preview['digest'], surface=label)
        after_refusals = self.rewind_counts(run)
        b_after_refusals = (project / 'b.txt').read_text(encoding='utf-8')
        applied = self.call(service, 'rewind_apply', chat_id=cid, run_id=rid, step=1, digest=preview['digest'],
                            surface='main', wait=60)
        after_apply = self.rewind_counts(run)
        rewinds = self.rows(self.facts['home'], "SELECT kind,status,payload FROM actions WHERE kind='edit' AND "
                                                 "json_extract(payload,'$.rewind.rewind_id')=?", (applied['rewind_id'],))
        files = {name: (project / name).read_text(encoding='utf-8') for name in REWIND_FILES}
        events = [e['payload'] for e in self.events(service, run, 'rewind_applied')]
        listed = {f['path']: f for f in preview['files']}
        done = {f['path']: f for f in applied['files']}

        def restored():
            assert preview['authorization'] == 'none' and preview['effects_reverted'] == 0 and preview['step'] == 1
            assert listed['b.txt']['status'] == 'revert' and listed['b.txt']['diff'], listed.get('b.txt')
            assert after_preview == counts, ('the preview wrote', counts, after_preview)
            assert done['b.txt']['status'] == 'reverted' and done['b.txt']['verified'] is True, done['b.txt']
            assert files['b.txt'] == REWIND_FILES['b.txt'][0] and files['a.txt'] == REWIND_FILES['a.txt'][1], files
            assert applied['counts']['reverted'] == 1 and applied['counts']['verified'] == 1, applied['counts']
            assert len(events) == 1 and events[0]['rewind_id'] == applied['rewind_id'] and applied['journaled'] is True
            return {'step': 1, 'reverted': ['b.txt'], 'kept_after_step': ['a.txt'], 'digest_sha256_chars': len(preview['digest']),
                    'journaled': True}
        self.check('w1_rewind_preview_and_apply_from_the_main_surface_restore_a_file', restored)

        def surfaces():
            assert len(refusals) == 9 and all(refusals.values()), refusals
            assert after_refusals == counts and b_after_refusals == REWIND_FILES['b.txt'][1], 'A refused rewind wrote.'
            return {'refused': sorted(refusals), 'sample': refusals['preview:pet'][:120]}
        self.check('w1_the_rewind_routes_refuse_the_pet_and_every_other_surface', surfaces)

        def person():
            assert listed['c.txt']['status'] == 'refused' and listed['c.txt']['reason'] == 'changed_since', listed.get('c.txt')
            assert done['c.txt']['status'] == 'refused' and files['c.txt'] == PERSON_C, (done.get('c.txt'), files['c.txt'])
            assert applied['counts']['refused'] == 1
            return {'refused': ['c.txt'], 'reason': 'changed_since', 'kept_person_text': True}
        self.check('w1_a_file_the_person_changed_is_refused_and_kept', person)

        def nothing_again():
            assert applied['replayed'] == 0 and applied['effects_reverted'] == 0 and applied['authorization'] == 'none'
            commands = [e for e in preview['effects'] if e['tool'] == 'RunCommand']
            assert len(commands) == 1 and commands[0]['kind'] == 'command' and commands[0]['reverted'] is False, commands
            assert (stamp.stat().st_mtime_ns, stamp.stat().st_ino) == stamp_before, 'The command ran again.'
            added = {kind: after_apply['actions'].get(kind, 0) - counts['actions'].get(kind, 0)
                     for kind in set(after_apply['actions']) | set(counts['actions'])}
            assert {k: v for k, v in added.items() if v} == {'edit': 1}, added
            assert len(rewinds) == 1 and rewinds[0]['status'] == 'applied', rewinds
            assert after_apply['run_tools'] == counts['run_tools'], 'The rewind added tool receipts.'
            assert len(self.requests('w1')) == requests_before, 'The rewind reached the model.'
            return {'actions_added': added, 'command_effects_listed': len(commands), 'stamp_unchanged': True,
                    'model_requests_added': 0}
        self.check('w1_a_rewind_runs_nothing_again', nothing_again)

    # p1-p6: recipe promotion ---------------------------------------------------------------------

    def procedural(self, slot='build:npm-build'):
        home = self.facts['home']
        evidence = self.rows(home, "SELECT memory_id,status,proof FROM memory_evidence WHERE kind='procedural' AND "
                                   'slot=?', (slot,))
        candidates = self.rows(home, "SELECT status,reason,run_id FROM memory_candidates WHERE kind='procedural' AND "
                                     'slot=?', (slot,))
        return evidence, candidates

    def judged(self, slot, what):
        """This slot's candidates once maintenance has judged each one (a reason, or a status other than pending)."""
        return self.until(lambda: (lambda found: found if found and all(c['status'] != 'pending' or c['reason']
                                                                        for c in found) else None)(self.procedural(slot)[1]),
                          8, what, 0.2)

    def script_recipes(self):
        for tag, recipe in (('p1', 'npm-build'), ('p2', 'npm-build'), ('p3', 'npm-build'), ('p4', 'npm-typecheck'),
                            ('p5', 'npm-typecheck'), ('p6', 'npm-build')):
            self.provider.sequence(tag, [[('RunRecipe', tag + '-recipe', {'recipe': recipe})]],
                                   'Synthetic fixture: the recipe ran; its receipt records the outcome.')

    def scenario_recipes(self, service):
        project = self.need('projects')['p']
        self.script_recipes()
        self.permissions(service, 'auto')
        runs = {}
        runs['p1'] = self.conversation(service, 'wide', 'p1', workspace=project, decide=lambda action: 'approve')
        first = self.judged('build:npm-build', 'the first run\'s procedural candidate to be judged')
        first_evidence = self.procedural()[0]

        def one():
            assert runs['p1']['state'] == 'completed', runs['p1']['state']
            assert [(c['status'], c['reason']) for c in first] == [('pending', 'needs_two_successful_runs')], first
            assert not [e for e in first_evidence if e['status'] == 'active'], first_evidence
            return {'state': runs['p1']['state'], 'candidate': 'pending', 'reason': 'needs_two_successful_runs'}
        self.check('p1_one_successful_build_run_does_not_promote_a_recipe', one)
        runs['p2'] = self.conversation(service, 'wide', 'p2', workspace=project, decide=lambda action: 'approve')
        active = self.until(lambda: [e for e in self.procedural()[0] if e['status'] == 'active'] or None, 10,
                            'the second run to promote the recipe', 0.2)
        memory = self.rows(self.facts['home'], 'SELECT content,origin FROM memories WHERE id=?', (active[0]['memory_id'],))

        def two():
            assert runs['p2']['state'] == 'completed' and len(active) == 1, (runs['p2']['state'], active)
            proof = json.loads(active[0]['proof'])
            assert proof.get('evidence_class') == 'observed_project_recipe' and proof.get('procedure') == 'build', proof
            assert re.fullmatch(r'[0-9a-f]{64}', proof.get('manifest_digest') or ''), proof
            assert sorted(proof.get('bound_files') or []) == ['package-lock.json', 'package.json'], proof
            content = json.loads(memory[0]['content'])
            assert content['recipe'] == 'npm-build' and content['recipe_kind'] == 'build', content
            assert 'nothing runs from memory' in content['reuse'] and memory[0]['origin'] == 'auto-learned'
            return {'slot': 'build:npm-build', 'evidence_class': proof['evidence_class'],
                    'binding_basis': proof.get('binding_basis'), 'bound_files': sorted(proof['bound_files'])}
        self.check('p2_a_second_successful_build_run_promotes_a_data_only_recipe', two)
        (project / 'package-lock.json').write_text(lockfile('MIT'), encoding='utf-8')
        runs['p3'] = self.conversation(service, 'wide', 'p3', workspace=project, decide=lambda action: 'approve')
        mid = active[0]['memory_id']
        status = self.until(lambda: next((e['status'] for e in self.procedural()[0] if e['memory_id'] == mid
                                          and e['status'] != 'active'), None), 10, 'the learned recipe to go stale', 0.2)

        def changed():
            assert status == 'stale', status
            assert not [e for e in self.procedural()[0] if e['status'] == 'active' and e['memory_id'] == mid]
            return {'status_after_lockfile_change': status, 'run_state': runs['p3']['state']}
        self.check('p3_a_changed_lockfile_makes_the_learned_recipe_stale', changed)
        typecheck = 'typecheck:npm-typecheck'
        runs['p4'] = self.conversation(service, 'wide', 'p4', workspace=project, decide=lambda action: 'approve')
        typecheck_first = self.judged(typecheck, 'the first typecheck candidate to be judged')
        typecheck_before = [e for e in self.procedural(typecheck)[0] if e['status'] == 'active']
        runs['p5'] = self.conversation(service, 'wide', 'p5', workspace=project, decide=lambda action: 'approve')
        typecheck_active = self.until(lambda: [e for e in self.procedural(typecheck)[0] if e['status'] == 'active'] or None,
                                      10, 'the second typecheck run to promote its recipe', 0.2)

        def typechecked():
            assert runs['p4']['state'] == runs['p5']['state'] == 'completed', (runs['p4']['state'], runs['p5']['state'])
            assert [(c['status'], c['reason']) for c in typecheck_first] == [('pending', 'needs_two_successful_runs')]
            assert not typecheck_before and len(typecheck_active) == 1, (typecheck_before, typecheck_active)
            proof = json.loads(typecheck_active[0]['proof'])
            # npm-typecheck is a lint-kind registry recipe; it teaches its own typecheck procedure.
            assert proof.get('procedure') == 'typecheck' and proof.get('evidence_class') == 'observed_project_recipe', proof
            assert sorted(proof.get('bound_files') or []) == ['package-lock.json', 'package.json'], proof
            return {'slot': typecheck, 'procedure': proof['procedure'], 'after_first_run': 'pending',
                    'binding_basis': proof.get('binding_basis')}
        self.check('p4_two_typecheck_runs_promote_the_typecheck_procedure_after_the_second', typechecked)
        self.permissions(service, 'ask')
        cid = service.rpc('new_chat', workspace=str(project))['id']
        self.select(service, 'wide')
        held = self.settle(service, cid, self.send(service, cid, 'p6'), 'p6', stop_at_approval=True)
        waiting = [a for a in service.rpc('get_chat', id=cid)['actions'] if a['status'] == 'pending']
        (project / 'package-lock.json').write_text(lockfile('ISC'), encoding='utf-8')  # changed after review
        try:
            service.rpc('approve', id=cid, action_id=waiting[0]['id'], wait=60) if waiting else None
            refusal = None
        except RuntimeError as exc:
            refusal = str(exc)
        action = next((a for a in service.rpc('get_chat', id=cid)['actions'] if waiting and a['id'] == waiting[0]['id']), {})
        with contextlib.suppress(Exception):
            service.rpc('cancel', id=cid, run_id=held['rid'])

        def refused():
            assert held['state'] == 'waiting_approval' and len(waiting) == 1 and waiting[0]['kind'] == 'tests', \
                (held['state'], [a['kind'] for a in waiting])
            assert isinstance(waiting[0]['payload'].get('bound_files'), dict), 'No reviewed binding was recorded.'
            assert refusal and REFUSED_AFTER_REVIEW in refusal, refusal
            assert action.get('status') == 'failed' and 'result' not in action.get('payload', {}), action.get('status')
            return {'refusal': refusal[:160], 'action_status': action['status'], 'launched': False}
        self.check('p5_a_lockfile_changed_after_review_refuses_the_run_before_launch', refused)

    # f7: corrupt and unverifiable extensions at startup ---------------------------------------------

    @staticmethod
    def reported_disabled(item):
        """Disabled, with a host-written reason the person can read in the extensions list."""
        config = item.get('config') if isinstance(item.get('config'), dict) else {}
        reason = item.get('error') or config.get('startup_error') or config.get('migration_error')
        return item.get('enabled') is False and isinstance(reason, str) and bool(reason.strip())

    def extension_evidence(self, home, ids):
        rows = self.stopped_rows
        return {'integrity': rows(home, 'PRAGMA integrity_check')[0][0],
                'alpha_config_sha256': sha(rows(home, 'SELECT config FROM extensions WHERE id=?', (ids['alpha'],))[0][0]),
                'beta_snapshot_sha256': sha(rows(home, 'SELECT snapshot FROM extension_bundles WHERE id=?', (ids['beta'],))[0][0]),
                'extensions': rows(home, 'SELECT count(*) FROM extensions')[0][0]}

    def scenario_extension_faults(self):
        scratch = self.need('scratch')
        home = scratch / 'home-f7'
        ids = {}
        service = self.service(home, scratch / 'service-f7a', 'f7-install')
        try:
            for key, content in SKILLS.items():
                ids[key] = service.rpc('extension_add', kind='skill', name='Phase 20 fixture ' + key, content=content)
                service.rpc('extension_toggle', id=ids[key], enabled=True)
            listed = {x['id']: x for x in service.rpc('extensions')}
            assert all(listed[i]['enabled'] for i in ids.values()), 'The fixture skills were not enabled.'
        finally:
            self.stop(service)
        snapshot = json.loads(self.stopped_rows(home, 'SELECT snapshot FROM extension_bundles WHERE id=?', (ids['beta'],))[0][0])
        body = base64.b64decode(snapshot['bodies']['SKILL.md']).decode('utf-8')
        snapshot['bodies']['SKILL.md'] = base64.b64encode(body.replace('Beta guidance body.', 'Beta guidance body. '
                                                                       + CANARY_TAMPER).encode()).decode()
        with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite', timeout=10)) as db, db:
            db.execute('UPDATE extension_bundles SET snapshot=? WHERE id=?',
                       (json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')), ids['beta']))
            db.execute('UPDATE extensions SET config=? WHERE id=?', ('{"bundle_digest": "truncated', ids['alpha']))
        before = self.extension_evidence(home, ids)
        service = self.service(home, scratch / 'service-f7b', 'f7-restart')
        outcome = {}
        try:
            try:
                service.rpc('state')
                outcome['listed'] = {x['id']: x for x in service.rpc('extensions')}
            except RuntimeError as exc:
                outcome['startup_error'] = str(exc)[:1500]
            if 'listed' in outcome:
                outcome['toggles'] = {key: self.refused(service, 'extension_toggle', id=ids[key], enabled=True)
                                      for key in ('alpha', 'beta')}
                outcome['listed_after'] = {x['id']: x for x in service.rpc('extensions')}
        finally:
            self.stop(service)
        after = self.extension_evidence(home, ids)

        def corrupt():
            assert 'startup_error' not in outcome, 'Startup did not survive: ' + outcome.get('startup_error', '')
            alpha = outcome['listed'].get(ids['alpha'])
            assert alpha is not None and self.reported_disabled(alpha), ('the corrupt extension is listed as', alpha)
            return {'enabled': alpha['enabled'], 'reported': True}
        self.check('f7_1_startup_survives_a_corrupt_extension_config_and_reports_it_disabled', corrupt)

        def unverified():
            assert 'startup_error' not in outcome, 'Startup did not survive.'
            beta = outcome['listed'].get(ids['beta'])
            assert beta is not None and self.reported_disabled(beta), ('the unverifiable skill is listed as',
                                                                       {'enabled': (beta or {}).get('enabled')})
            return {'enabled': beta['enabled'], 'reason': (beta.get('config') or {}).get('startup_error') or 'error'}
        self.check('f7_2_a_skill_whose_snapshot_no_longer_verifies_is_disabled_and_reported_at_startup', unverified)

        def kept():
            assert 'toggles' in outcome and all(outcome['toggles'].values()), outcome.get('toggles')
            again = outcome['listed_after']
            assert not again[ids['alpha']]['enabled'] and not again[ids['beta']]['enabled']
            assert after == before and before['integrity'] == 'ok', (before, after)
            return {'refusals': {k: v[:100] for k, v in outcome['toggles'].items()}, 'evidence_unchanged': True}
        self.check('f7_the_corrupt_rows_are_kept_and_neither_extension_can_be_enabled_again', kept)

    # f8: an external database lock through settlement -------------------------------------------------

    def scenario_lock_faults(self):
        scratch = self.need('scratch')
        home = scratch / 'home-f8'
        service = self.service(home, scratch / 'service-f8', 'f8')
        locker = Locker(home / 'pebble.sqlite', OUTAGE_CAP_SECONDS)

        def locked(view):
            locker.acquire()
            return final('Synthetic fixture: answer k1.')
        self.provider.script('k1', locked)
        self.provider.sequence('k2', [], 'Synthetic fixture: a later task.')
        outage = {}
        try:
            profile = service.rpc('model_save', name='Phase 20 lock fixture', url=self.provider.url, model=MODEL_WIDE,
                                  context=32768)['profile_id']
            service.rpc('model_select', profile_id=profile)
            cid = service.rpc('new_chat')['id']
            rid = self.send(service, cid, 'k1')
            try:
                self.until(locker.acquired.is_set, 20, 'the model request to take the external lock')
                started = locker.facts['acquired_at']
                # No RPC while the run settles: the service announces idle for this run when it releases its worker.
                idle = lambda: service.since(0, 'idle', run_id=rid)  # noqa: E731
                self.until(idle, OUTAGE_CAP_SECONDS - 6, 'the service to release its worker while the database is locked',
                           interval=0.2)
                outage['worker_released_after'] = round(idle()[0]['at'] - started, 1)
                probe = time.monotonic()
                try:
                    service.rpc('state', wait=STATUS_PROBE_SECONDS)
                    outage['status_read'] = {'seconds': round(time.monotonic() - probe, 2)}
                except RuntimeError as exc:
                    outage['status_read'] = {'seconds': round(time.monotonic() - probe, 2), 'error': str(exc)[:200]}
                outage['status_read']['while_locked'] = not locker.released.is_set()
            finally:
                locker.release()
            assert locker.released.wait(10) and 'error' not in locker.facts, locker.facts
            released = time.monotonic()

            def settled():
                state = service.rpc('run_get', id=cid, run_id=rid)['state']
                return state if state not in TRANSIENT else None
            try:
                outage['state'] = self.until(settled, SETTLE_AFTER_OUTAGE_SECONDS, 'the locked run to settle', 0.5)
                outage['settled_after_release'] = round(time.monotonic() - released, 1)
            except AssertionError:
                outage['state'] = service.rpc('run_get', id=cid, run_id=rid)['state']
            outage['answers'] = self.assistant(service.rpc('get_chat', id=cid))
            outage['errors'] = [e['data'].get('message', '')[:160] for e in service.since(0, 'error', chat_id=cid)]
            later = service.rpc('new_chat')['id']
            outage['later'] = self.settle(service, later, self.send(service, later, 'k2'), 'k2', seconds=20)['state']
        finally:
            self.stop(service)
            self.observations['locks']['k1'] = dict(locker.facts)
            self.observations['outage'] = {k: v for k, v in outage.items() if k != 'answers'}

        def status_read():
            found = outage.get('status_read')
            assert found and found['while_locked'], ('Prerequisite: a status read while the database was locked', found)
            assert 'error' not in found, ('a status read failed while the database was locked', found)
            return {**found, 'worker_released_after_seconds': outage['worker_released_after']}
        self.check('f8_2_a_status_read_under_a_held_lock_answers_without_a_database_error', status_read)

        def not_running():
            assert outage.get('state') not in (None, *TRANSIENT), ('the run stayed', outage.get('state'))
            answered = any('answer k1' in text for text in outage['answers'])
            assert outage['state'] == 'completed' or not answered, ('an unsettled answer was shown', outage['state'])
            assert outage['later'] == 'completed', ('a later task after the lock', outage['later'])
            return {'state': outage['state'], 'settled_after_release_seconds': outage.get('settled_after_release'),
                    'answer_shown': answered, 'later_task': outage['later'], 'errors_reported': len(outage['errors'])}
        self.check('f8_1_the_run_does_not_stay_running_once_the_database_is_free', not_running)

    # Final -------------------------------------------------------------------------------------

    def final_checks(self):
        def routes():
            backend_path()
            from pebble.rewind import ROUTES
            with self.provider.lock:
                offered = {n for r in self.provider.records for n in r['tools']}
            facts = self.facts.get('shell') or {}
            names = set(ROUTES) | set(facts.get('allowed', ())) | set(facts.get('native_only', ()))
            leaked = sorted(n for n in offered if n.casefold() in {r.casefold() for r in names})
            assert offered and not leaked, leaked
            return {'tool_names_seen': len(offered), 'route_names_compared': len(names)}
        self.check('native_phase20_routes_are_never_offered_to_models', routes)

        def clean():
            with self.provider.lock:
                assert not self.provider.errors, self.provider.errors[-5:]
                assert not self.provider.anomalies, self.provider.anomalies[-5:]
                assert not [r for r in self.provider.records if r['model'] not in MODELS]
                return {'provider_attempts': len(self.provider.records)}
        self.check('synthetic_provider_protocol_clean', clean)

        def egress():
            found = self.trap.report()
            assert found['attempts'] == 0, found
            return found
        self.check('no_proxy_honouring_egress_was_attempted', egress)


def checkout_digests():
    """Digests of every checkout file this campaign reads: the backend it launches or imports, the shell it
    parses, this validator, the validators it imports and the release command line it runs."""
    paths = ['backend/main.py', *[p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'src-tauri/src/main.rs', 'scripts/validate_phase20.py', 'scripts/validate_phase19.py',
             'scripts/validate_phase17.py', 'scripts/validate_phase14.py', 'scripts/validate_phase13.py',
             'scripts/validate_runtime.py', 'scripts/measure_resources.py', 'scripts/check_harness_release.py']
    digests = {}
    for path in paths:
        try:
            digests[path] = sha((ROOT / path).read_bytes())
        except OSError:
            digests[path] = None
    return digests


def build_report(campaign, *, backend, backend_sha, sources, sources_after, duration, scratch, keep):
    results = campaign.checks
    changed = sorted(p for p in set(sources) | set(sources_after) if sources.get(p) != sources_after.get(p))
    backend_after = sha(backend.read_bytes()) if backend else None
    stable = not changed and backend_after == backend_sha
    stability = {'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                 'evidence': {'checkout_files_compared': len(sources), 'executable_compared': bool(backend)}}
    if not stable:
        stability['error'] = ('Backend inputs changed during the campaign: '
                              + json.dumps({'checkout_files': changed[:20],
                                            'executable': bool(backend) and backend_after != backend_sha}))
    results.append(stability)
    results.sort(key=lambda c: PLANNED.index(c['name']))
    complete = [c['name'] for c in results] == list(PLANNED)
    passed = complete and campaign.fatal is None and all(c['passed'] for c in results)
    texts = ('diagnostics', 'coverage', 'completion_checks', 'resume', 'names_evidence')
    with campaign.provider.lock:  # request metadata stays content-free: host message texts become counts
        records = [{k: (len(value) if k in texts else value) for k, value in r.items()} for r in campaign.provider.records]
        errors, anomalies = list(campaign.provider.errors), list(campaign.provider.anomalies)
    return {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source',
        'passed': passed,
        'checks': [c['name'] for c in results if c['passed']],
        'failed': [c['name'] for c in results if not c['passed'] and 'pending' not in c],
        'pending': [c['name'] for c in results if 'pending' in c],
        'planned_checks': list(PLANNED), 'all_planned_checks_recorded': complete,
        'scenarios': {name: list(checks) for name, checks in SCENARIOS.items()},
        'integration_gaps': {key: GAPS[key] for key in sorted(campaign.gaps)},
        'check_results': results, 'fatal_error': campaign.fatal,
        'observations': campaign.observations,
        'duration_seconds': duration, 'duration_budget_seconds': VERIFIER_SECONDS,
        'scenario_budget_seconds': BUDGET_SECONDS, 'scenario_bounds_seconds': dict(SCENARIO_BOUNDS),
        'synthetic_provider_attempts': len(records), 'request_metadata': records,
        'provider_errors': errors, 'provider_anomalies': anomalies,
        'backend_sha256': backend_sha,
        'backend_path': str(backend) if backend else None,
        'source_sha256': sources,
        'source_note': ('Digests of this checkout. In frozen mode they do not prove the executable was built from '
                        'these files; the candidate manifest is bound separately.'),
        'scratch_preserved': str(scratch) if keep else None,
        'backend_stderr_tail': campaign.stderr_tails if not passed else {},
        'limits': ('Owned loopback synthetic provider, owned temporary homes outside the checkout, the actual service '
                   'JSON-RPC launched from the main.rs environment allowlist names (HOME and TMPDIR owned, proxy '
                   'egress trapped), native main-window routes, real sandboxed recipe and test runs, and the shell read '
                   'as source (the validator stands in for it on the rewind surface). The readiness checks run on a '
                   'synthetic evidence tree with throwaway keys. Fixture evidence on this machine: no live model, '
                   'webview, installed or signed desktop, real trust key, independent audit, disk-full, sleep/wake, '
                   'signal, other platform or frontier evidence.'),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary homes even on success')
    args = parser.parse_args(argv)
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; move it aside or choose a new path (earlier raw evidence, '
                     'including a failed run, is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    command = [str(backend)] if backend else [sys.executable, str(ROOT / 'backend/main.py')]
    backend_sha = sha(backend.read_bytes()) if backend else None
    sources = checkout_digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase20-')).resolve()
    if scratch.is_relative_to(ROOT):
        parser.error('The temporary folder is inside this checkout; set TMPDIR elsewhere.')
    started = time.monotonic()
    campaign = Campaign(command, backend)
    try:
        campaign.run_all(scratch)
    finally:
        duration = round(time.monotonic() - started, 1)
        sources_after = checkout_digests()
    keep = args.keep_scratch or not (campaign.fatal is None and campaign.passing())
    report = build_report(campaign, backend=backend, backend_sha=backend_sha, sources=sources,
                          sources_after=sources_after, duration=duration, scratch=scratch, keep=keep)
    if not report['passed']:
        keep = True
        report['scratch_preserved'] = str(scratch)
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_new(report_path, json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': report['passed'], 'passed_checks': len(report['checks']),
                      'failed': report['failed'], 'pending': report['pending'], 'fatal_error': campaign.fatal,
                      'duration_seconds': duration, 'scenario_seconds': campaign.observations.get('scenario_seconds'),
                      'scratch_preserved': report['scratch_preserved']}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
