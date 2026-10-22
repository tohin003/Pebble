"""Phase 13 source/frozen JSON-RPC proof: child worktrees, reviewed Git, recipes and undo.

Drives the *actual* service over its JSON-RPC stdio contract (validate_runtime.
Service) with one owned loopback SSE provider, a disposable temporary home and
owned projects under a private temporary root (never under .runtime). Git
projects and local bare remotes are created with the system Git, hooks
disabled and no user configuration; the service itself uses only its confined
Git. Every model turn is scripted synthetic output. Permission modes are set
through the native `setting` RPC; approval is the native `approve` RPC a person
uses. No real model, external network, account, credential, keychain,
dependency install or user data is used.

Scenarios (each tagged with an opaque marker, in its own conversation and project):

  g1  Ask mode: ProposeGitChange commits one selected path and stays pending.
      Nothing changes before native approval; the applied receipt's commit
      holds exactly the reviewed blob and only that path; other dirty and
      staged work stays as it was; repository hooks never run; native readback
      matches the receipt.
  g2  the fixture changes the selected file after proposal: approval is refused,
      the ref is unmoved and the receipt is failed with no effect started.
  g3  Full mode: a commit applies automatically while discard waits for native
      review; the approved discard keeps the prior bytes as a readable blob and
      a separately reviewed undo plan restores them.
  g4  a push to a local bare remote waits for native review even in Full mode
      and reads back on the remote; the remote's (non-executable) hook never
      runs; a remote with an executable hook and an https remote are refused at
      proposal with no pending action.
  n1  committing a file inside a nested repository (a folder with its own
      .git) is refused at proposal with no pending action or receipt.
  u1  Auto mode: native undo of an applied edit records payload.undo.verified;
      undo after an external file change is refused and the change is kept.
  r1  Ask mode: ListProjectRecipes, then RunTests auto in a pytest project,
      natively approved, passes with counted tests and a snapshot digest.
  r2  Ask mode: a failing project yields failed.
  r3  Auto mode: a zero-test project yields no_tests, and a declarative
      require_tests task_end hook keeps the run from completing.
  e1  Full mode: PrepareEnvironment still waits for native review; a lockfile
      naming a foreign registry host is refused at proposal; the pending install
      is natively rejected, never approved, so nothing installs.
  w1  Auto mode: a root SpawnWorktreeAgent allocation (the parent's renamed
      SpawnAgent(workspace="worktree") route) applies automatically; the child
      runs only on its active worktree and reads and
      writes there while the project file is untouched; the root accepts the
      result, exports, reads and integrates the file (auto-applied) and retires
      the worktree; the project's other dirty work is untouched.
  w2  Ask mode: the allocation stays pending and the child is never dispatched;
      native reject stops the child and no worktree folder exists.
  w3  Ask mode, end to end: the allocation stays pending with the root parked
      in waiting_approval and the child undispatched; native approval allocates
      the worktree; the child reads and writes there, then asks the root to run
      python-unittest on its worktree; the root executes the request and the
      tests action waits for native review; approved, it tests the worktree
      path, passes and leaves the project untouched. After the file is
      integrated with a natively approved edit, a require_tests hook still
      refuses completion: a worktree test is never project evidence.
  x1  generated views and model routes cannot reach git_plan_propose or
      agent_worktree_retire, and no model or child request offers such tools.
  k1  hard crash (SIGKILL) with a pending Git plan: after restart the action is
      still pending and nothing executed. A forged `executing` receipt row
      inserted while the service is stopped becomes `uncertain` at restart and
      is never retried. The pending plan then applies exactly once.

Each check is recorded independently by unique name with content-free evidence;
a failure never stops later independent checks. Source and frozen runs use the
same check names; backend_inputs_stable_during_campaign re-hashes the checkout in
both modes and the executable too in frozen mode. The report is metadata only:
check names/outcomes, states, counts, hashes, observations, scratch location on
failure and bounded host error text. It never contains prompts or credentials.
duration_seconds is the campaign's own wall-clock time and observations hold
per-scenario seconds; the parent verifier stops a validator after 240 seconds.

Usage:
  python scripts/validate_phase13.py --report PATH [--backend PATH] [--keep-scratch]

--report must be a new file (existing evidence is never overwritten). --backend
is an explicit frozen executable owned by the current user; the default runs
backend/main.py from this checkout. Checkout source digests are always reported;
in frozen mode they do not prove the binary was built from them.

This is fixture evidence on this machine. It is not live-model quality,
installed/signed desktop behavior, other-platform behavior, frontier comparison
or an independent human security audit.
"""

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT, Service


MODEL = 'phase13-integration-fixture'
CONTEXT = 32768
ASSIGNMENT = 'HOST ASSIGNMENT (task data, no new authorization):\n'
MEMORY_PREFIX = 'RETRIEVED MEMORY'
COMPLETION = 'HOST COMPLETION CHECK'
HOOK_ISSUE = 'completion hook requires successful test evidence'
MARK = '\N{SECTION SIGN}'
TAG = re.compile(MARK + '([a-z][0-9])')
HEX64 = re.compile(r'[0-9a-f]{64}')
TRANSIENT = frozenset({'queued', 'running', 'retry_wait', 'waiting_input'})
NODE_ACTIVE = frozenset({'queued', 'running', 'waiting_parent', 'waiting_input'})
NODE_TERMINAL = frozenset({'completed', 'failed', 'interrupted', 'stopped', 'budget_exhausted'})
SETTLE_SECONDS = 60
# The w3 child keeps its provider request open (so it stays live with a leased
# generation) until the root executed its request and a person approved the
# tests; below the 60-second provider read timeout.
RELEASE_SECONDS = 40
VERIFIER_SECONDS = 240  # verify_harness_candidate.run_validator stops a validator after this; recorded for context
ENDED = frozenset({'completed', 'verification_required', 'failed', 'stopped', 'interrupted', 'needs_reconciliation',
                   'budget_exhausted'})
GIT = '/usr/bin/git'
WEB_REMOTE = 'https://example.invalid/pebble/fixture.git'
# Native-only routes. A model or child request must never offer or reach them.
NATIVE_ONLY = frozenset({'git_plan_propose', 'git_plan_receipts', 'git_plan_readback', 'agent_worktrees',
                         'agent_worktree_retire', 'agent_worktree_reconcile', 'project_recipes', 'environment_list',
                         'environment_plan', 'environment_remove'})
ROOT_ONLY = frozenset({'ProposeGitChange', 'ReadGitPlanReceipt', 'ListProjectRecipes', 'PrepareEnvironment', 'RunRecipe',
                       'IntegrateWorktreeFile', 'PreviewWorktreeExport', 'ReadWorktreeExport',
                       'SpawnWorktreeAgent'})
# Removing a worktree stays native-only (Phase 9 contract); no model tool may offer it.
NEVER_OFFERED = frozenset({'RetireAgentWorktree'})
WATCHED = ROOT_ONLY | {'RunTests', 'SpawnAgent', 'WriteWorktreeFile', 'EditWorktreeFile', 'Read', 'Write',
                       'RequestAgentEffect', 'ReadAgentEffect', 'ReadAgentEffects', 'ExecuteAgentEffect'}
TOOLS = ('files', 'edits', 'tests', 'apps', 'web', 'terminal', 'plugins', 'views')
CHILD_USAGE = {'prompt_tokens': 900, 'completion_tokens': 20}
DONE = 'The scripted steps finished; the host receipts record each outcome.'

A_HEAD, A_WORK = 'alpha: committed line\n', 'alpha: reviewed change for one selected path\n'
B_HEAD, B_WORK = 'bravo: committed line\n', 'bravo: unstaged work that must stay unstaged\n'
C_HEAD, C_STAGED = 'charlie: committed line\n', 'charlie: staged work that must stay staged\n'
D_HEAD, D_WORK = 'delta: committed line\n', 'delta: unstaged work a discard must preserve\n'
G2_DRIFT = 'alpha: changed by the person after the plan was proposed\n'
FEATURE_HEAD = 'greeting = "hello"\n'
FEATURE_CHILD = 'greeting = "hello from the child worktree"\n'
OTHER_HEAD, OTHER_WORK = 'other: committed line\n', 'other: dirty project work the child never sees\n'
SCRATCH = 'untracked scratch notes kept by the person\n'
U1_A_OLD, U1_A_NEW = 'Notes A kept by the person.\n', 'Notes A with the new wording.\n'
U1_B_OLD, U1_B_NEW = 'Notes B kept by the person.\n', 'Notes B with the new wording.\n'
U1_B_EXTERNAL = 'Notes B changed outside Pebble after the edit applied.\n'
R3_OLD, R3_NEW = 'Release notes.\n', 'Release notes, updated before the test run.\n'
APP_HEAD, APP_WORK = 'VERSION = 1\n', 'VERSION = 2  # changed inside the nested repository\n'
WORKTREE_TESTS = {'recipe': 'python-unittest', 'target': 'worktree'}
PYTEST_INI = '[pytest]\n'
PASSING = '''import unittest


class SampleTest(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(1 + 1, 2)

    def test_text(self):
        self.assertTrue('pebble'.startswith('peb'))
'''
FAILING = '''import unittest


class SampleTest(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(1 + 1, 2)

    def test_regression(self):
        self.assertEqual(2 + 2, 5)
'''
REQUIREMENTS = 'idna==3.7 --hash=sha256:' + hashlib.sha256(b'pebble phase 13 fixture wheel').hexdigest() + '\n'
PACKAGE_JSON = json.dumps({'name': 'pebble-fixture', 'version': '1.0.0', 'dependencies': {'leftpad': '1.0.0'}}) + '\n'
PACKAGE_LOCK = json.dumps({'name': 'pebble-fixture', 'version': '1.0.0', 'lockfileVersion': 3, 'requires': True, 'packages': {
    '': {'name': 'pebble-fixture', 'version': '1.0.0', 'dependencies': {'leftpad': '1.0.0'}},
    'node_modules/leftpad': {'version': '1.0.0', 'resolved': 'https://registry.example.invalid/leftpad/-/leftpad-1.0.0.tgz',
                             'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(b'leftpad').digest()).decode()}}}) + '\n'
GIT_PROJECTS = ('g1', 'g2', 'g3', 'g4', 'n1', 'w1', 'w2', 'w3', 'x1', 'k1')

GOALS = {tag: text + ' ' + MARK + tag for tag, text in {
    'g1': 'Commit only a.txt with git as one reviewed change.',
    'g2': 'Commit a.txt with git as one reviewed change.',
    'g3': 'Commit a.txt with git, then discard the unstaged change in d.txt.',
    'g4': 'Push the main branch with git to each configured remote.',
    'n1': 'Commit vendor/inner/app.py with git as one reviewed change.',
    'u1': 'Update a.txt and b.txt with the new wording.',
    'r1': 'List the project recipes and run the project tests.',
    'r2': 'Run the project tests.',
    'r3': 'Update notes.txt, then run the project tests.',
    'e1': 'Install the project dependencies into a managed environment from the lockfiles.',
    'w1': 'Delegate the feature.txt edit to a child agent in its own worktree, then export and integrate it.',
    'w2': 'Delegate the feature.txt edit to a child agent in its own worktree.',
    'w3': 'Delegate the feature.txt edit to a child agent in its own worktree, run its tests there, then export and integrate the file.',
    'x1': 'Commit a.txt with git and retire any agent worktree through the native routes.',
    'k1': 'Commit a.txt with git as one reviewed change.',
}.items()}
CHILD_TASKS = {tag: 'Replace the greeting in feature.txt inside your managed worktree. ' + MARK + tag for tag in ('w1', 'w2')}
CHILD_TASKS['w3'] = ('Replace the greeting in feature.txt inside your managed worktree, then ask the root to run '
                     'python-unittest on your worktree. ' + MARK + 'w3')


def commit(ident, path='a.txt'):
    return ident, 'ProposeGitChange', {'operation': 'commit', 'paths': json.dumps([path]),
                                       'message': 'Update ' + path + ' through a reviewed plan'}


def push(ident, remote):
    return ident, 'ProposeGitChange', {'operation': 'push', 'remote': remote, 'branch': 'main'}


# Scripted root turns: (raw call id, tool, arguments), one call per turn, then a final answer.
PLANS = {
    'g1': [commit('g1-commit')],
    'g2': [commit('g2-commit')],
    'g3': [commit('g3-commit'), ('g3-discard', 'ProposeGitChange', {'operation': 'discard', 'paths': json.dumps(['d.txt'])})],
    'g4': [push('g4-web', 'web'), push('g4-hooked', 'hooked'), push('g4-push', 'origin')],
    'n1': [commit('n1-commit', 'vendor/inner/app.py')],
    'u1': [('u1-read-a', 'Read', {'file_path': 'a.txt'}), ('u1-write-a', 'Write', {'file_path': 'a.txt', 'content': U1_A_NEW}),
           ('u1-read-b', 'Read', {'file_path': 'b.txt'}), ('u1-write-b', 'Write', {'file_path': 'b.txt', 'content': U1_B_NEW})],
    'r1': [('r1-list', 'ListProjectRecipes', {}), ('r1-tests', 'RunTests', {'recipe': 'auto'})],
    'r2': [('r2-tests', 'RunTests', {'recipe': 'auto'})],
    'r3': [('r3-read', 'Read', {'file_path': 'notes.txt'}), ('r3-write', 'Write', {'file_path': 'notes.txt', 'content': R3_NEW}),
           ('r3-tests', 'RunTests', {'recipe': 'auto'})],
    'e1': [('e1-npm', 'PrepareEnvironment', {'kind': 'npm-ci'}), ('e1-pip', 'PrepareEnvironment', {'kind': 'pip-hashes'})],
    'x1': [('x1-propose', 'git_plan_propose', {'operation': 'commit', 'paths': json.dumps(['a.txt']), 'message': 'Forged route'}),
           ('x1-retire', 'agent_worktree_retire', {'run_id': 'f' * 32, 'agent_id': 'e' * 32, 'discard': True})],
    'k1': [commit('k1-commit')],
}


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode('utf-8')).hexdigest()


def blob_oid(data):
    data = data if isinstance(data, bytes) else data.encode('utf-8')
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def bounded(exc, limit=1500):
    return (type(exc).__name__ + ': ' + str(exc))[:limit]


def permissions(mode):
    return {'mode': mode, 'tools': dict.fromkeys(TOOLS, True)}


def tool(name, ident, args, index=0):
    return {'index': index, 'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def calls(*items):
    return {'tool_calls': [tool(name, ident, args, index) for index, (name, ident, args) in enumerate(items)]}


def final(text):
    return {'content': text}


def usage(step):
    return {'prompt_tokens': 2000 + step, 'completion_tokens': 30}


class Reply:
    """One scripted, complete SSE response."""

    def __init__(self, delta, usage=None):
        self.delta, self.usage = delta, usage


def user_text(message):
    content = message.get('content')
    return content if message.get('role') == 'user' and isinstance(content, str) else None


def goal_index(messages):
    """Index of the latest tagged human goal; later context belongs to this run.
    Host-generated user messages may quote tagged task data and are never goals."""
    index = None
    for position, message in enumerate(messages):
        text = user_text(message)
        if text and not text.startswith((MEMORY_PREFIX, 'HOST ')) and TAG.search(text):
            index = position
    return index


def after_goal(messages):
    index = goal_index(messages)
    return messages if index is None else messages[index + 1:]


def result(messages, call_id):
    """The host tool result for one raw call id, decoded; None if absent."""
    for message in reversed(messages):
        if message.get('role') == 'tool' and message.get('tool_call_id') == call_id:
            try:
                value = json.loads(message.get('content') or 'null')
            except (TypeError, ValueError):
                return {'unparsed': True}
            return value if isinstance(value, dict) else {'value_type': type(value).__name__}
    return None


def history(messages):
    done = []
    for message in after_goal(messages):
        if message.get('role') != 'assistant':
            continue
        for call in message.get('tool_calls') or []:
            try:
                args = json.loads(call['function'].get('arguments') or '{}')
            except ValueError:
                args = {}
            done.append({'name': call['function']['name'], 'id': call['id'],
                         'args': args if isinstance(args, dict) else {}, 'result': result(messages, call['id'])})
    return done


def last_call(messages):
    assistants = [m for m in after_goal(messages) if m.get('role') == 'assistant']
    if not assistants:
        return None, None
    tool_calls = assistants[-1].get('tool_calls') or []
    if not tool_calls:
        return 'final', None
    return tool_calls[-1]['function']['name'], tool_calls[-1]['id']


def summary(value):
    if not isinstance(value, dict):
        return {'absent': True}
    inner = value['result'] if isinstance(value.get('result'), dict) else {}
    return {'error': str(value['error'])[:300] if value.get('error') else None, 'status': value.get('status'),
            'kind': value.get('kind'), 'action_present': bool(value.get('action_id')),
            'result_state': inner.get('state'), 'outcome': inner.get('outcome')}


def spawn_summary(value):
    if not isinstance(value, dict):
        return {'absent': True}
    tree = value.get('worktree') if isinstance(value.get('worktree'), dict) else {}
    return {'error': str(value['error'])[:300] if value.get('error') else None, 'agent_present': bool(value.get('agent_id')),
            'worktree_status': tree.get('status'), 'worktree_action_present': bool(tree.get('action_id')),
            'worktree_present': bool(tree.get('worktree_id'))}


def nodes_of(value):
    return [n for n in (value or {}).get('nodes', []) if isinstance(n, dict)]


def rejected(service, method, **params):
    try:
        service.rpc(method, **params)
    except RuntimeError as exc:
        text = str(exc)
        if text.startswith(('Backend timed out', 'Backend stopped')):
            raise AssertionError('No refusal was observed; the service failed instead: ' + text[:300]) from None
        return text[:300]
    raise AssertionError('Expected a refusal from ' + method + ' ' + json.dumps(sorted(params)))


class GitFixture:
    """Fixture-side system Git: hooks disabled, no system or global configuration."""

    def __init__(self, scratch):
        self.home, self.template = scratch / 'fixture-home', scratch / 'git-template'
        self.home.mkdir(exist_ok=True)
        self.template.mkdir(exist_ok=True)
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
                    'GIT_AUTHOR_NAME': 'Pebble Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                    'GIT_COMMITTER_NAME': 'Pebble Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
                    'GIT_AUTHOR_DATE': '1700000000 +0000', 'GIT_COMMITTER_DATE': '1700000000 +0000'}
        for key in ('TMPDIR', 'DEVELOPER_DIR'):
            if os.environ.get(key):
                self.env[key] = os.environ[key]

    def __call__(self, cwd, *args, ok=(0,)):
        done = subprocess.run([GIT, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'commit.gpgSign=false',
                               '-c', 'init.defaultBranch=main', *args], cwd=cwd, env=self.env, capture_output=True, timeout=30)
        if done.returncode not in ok:
            raise AssertionError(('Fixture git failed.', list(args[:2]), done.returncode,
                                  done.stderr.decode('utf-8', 'replace')[-300:]))
        return done.stdout

    def init(self, root, files, dirty=None, staged=None, untracked=None):
        root.mkdir(parents=True)
        self(root, 'init', '-q', '--template=' + str(self.template))
        self(root, 'config', 'user.name', 'Pebble Fixture')
        self(root, 'config', 'user.email', 'fixture@example.invalid')
        for name, body in files.items():
            (root / name).write_text(body)
        self(root, 'add', '--', *files)
        self(root, 'commit', '-q', '-m', 'Fixture base')
        for name, body in (staged or {}).items():
            (root / name).write_text(body)
            self(root, 'add', '--', name)
        for name, body in {**(dirty or {}), **(untracked or {})}.items():
            (root / name).write_text(body)
        return root

    def bare(self, path, hook=None, executable=False):
        self(path.parent, 'init', '-q', '--bare', '--template=' + str(self.template), str(path))
        if hook:
            (path / 'hooks').mkdir()
            script = path / 'hooks' / 'pre-receive'
            script.write_text(hook)
            script.chmod(0o755 if executable else 0o644)
        return path


def marker_hook(marker):
    return '#!/bin/sh\nprintf ran > ' + shlex.quote(str(marker)) + '\n'


def staged_names(git, root):
    """Paths whose index entry differs from HEAD (read-only; never refreshes the index)."""
    return sorted(name for name in git(root, 'diff', '--cached', '--name-only', '-z').decode().split('\0') if name)


def build_projects(scratch, git):
    """Owned fixture projects and remotes; every root is a canonical path outside .runtime."""
    projects, markers, remotes = scratch / 'projects', scratch / 'markers', scratch / 'remotes'
    for folder in (projects, markers, remotes):
        folder.mkdir()
    roots = {}

    def repo(tag, files, **changes):
        roots[tag] = git.init(projects / (tag + '-project'), files, **changes)
        return roots[tag]

    def plain(tag, files):
        root = roots[tag] = projects / (tag + '-project')
        root.mkdir()
        for name, body in files.items():
            (root / name).write_text(body)
        return root

    g1 = repo('g1', {'a.txt': A_HEAD, 'b.txt': B_HEAD, 'c.txt': C_HEAD}, dirty={'a.txt': A_WORK, 'b.txt': B_WORK},
              staged={'c.txt': C_STAGED})
    (g1 / '.git' / 'hooks').mkdir()
    for name in ('pre-commit', 'post-commit'):
        hook = g1 / '.git' / 'hooks' / name
        hook.write_text(marker_hook(markers / ('g1-' + name)))
        hook.chmod(0o755)
    repo('g2', {'a.txt': A_HEAD}, dirty={'a.txt': A_WORK})
    repo('g3', {'a.txt': A_HEAD, 'd.txt': D_HEAD}, dirty={'a.txt': A_WORK, 'd.txt': D_WORK})
    g4 = repo('g4', {'a.txt': A_HEAD})
    origin = git.bare(remotes / 'g4-origin.git', marker_hook(markers / 'g4-origin-pre-receive'), executable=False)
    hooked = git.bare(remotes / 'g4-hooked.git', marker_hook(markers / 'g4-hooked-pre-receive'), executable=True)
    git(g4, 'remote', 'add', 'origin', str(origin))
    git(g4, 'remote', 'add', 'hooked', str(hooked))
    git(g4, 'remote', 'add', 'web', WEB_REMOTE)
    # A nested repository is an untracked folder of the outer one; its file changed in the inner repository only.
    n1 = repo('n1', {'a.txt': A_HEAD})
    git.init(n1 / 'vendor' / 'inner', {'app.py': APP_HEAD}, dirty={'app.py': APP_WORK})
    repo('w1', {'feature.txt': FEATURE_HEAD, 'other.txt': OTHER_HEAD}, dirty={'other.txt': OTHER_WORK},
         untracked={'scratch.txt': SCRATCH})
    repo('w2', {'feature.txt': FEATURE_HEAD})
    # Clean, so after integration the project holds exactly the worktree's files: only the root binding,
    # not a snapshot digest, can tell a worktree test from project evidence.
    repo('w3', {'feature.txt': FEATURE_HEAD, 'test_sample.py': PASSING})
    repo('x1', {'a.txt': A_HEAD}, dirty={'a.txt': A_WORK})
    repo('k1', {'a.txt': A_HEAD}, dirty={'a.txt': A_WORK})
    plain('u1', {'a.txt': U1_A_OLD, 'b.txt': U1_B_OLD})
    plain('r1', {'pytest.ini': PYTEST_INI, 'test_sample.py': PASSING})
    plain('r2', {'pytest.ini': PYTEST_INI, 'test_sample.py': FAILING})
    plain('r3', {'pytest.ini': PYTEST_INI, 'notes.txt': R3_OLD})
    plain('e1', {'requirements.txt': REQUIREMENTS, 'package.json': PACKAGE_JSON, 'package-lock.json': PACKAGE_LOCK})
    return {'roots': roots, 'markers': markers, 'remotes': {'origin': origin, 'hooked': hooked}}


class Fixture:
    """Owned loopback SSE provider. Scripts are stateless per request: the step
    is the number of assistant turns after the latest tagged goal, and every
    script decides from the host results it can see, so retries, resumes and
    completion repairs stay aligned; repeated attempts of one step are counted."""

    def __init__(self):
        self.lock = threading.RLock()
        self.records, self.errors, self.anomalies = [], [], []
        self.seen, self.counts, self.shared = {}, {}, {}
        # Set by the campaign after native approval; the w3 child's open request waits on it.
        self.events = {'w3': threading.Event()}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    reply = fixture.respond(body)
                except Exception as exc:
                    with fixture.lock:
                        fixture.errors.append(bounded(exc))
                    try:
                        self.send_error(500)
                    except Exception:
                        pass
                    return
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': reply.delta}]}).encode() + b'\n\n')
                    if reply.usage is not None:
                        self.wfile.write(b'data: ' + json.dumps({'choices': [], 'usage': reply.usage}).encode() + b'\n\n')
                    self.wfile.write(b'data: [DONE]\n\n')
                except OSError:
                    pass  # a crashed service closed its end

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()

    def stop(self):
        for event in self.events.values():
            event.set()  # never leave a handler thread waiting
        self.server.shutdown()
        self.server.server_close()

    def state(self, tag):
        with self.lock:
            return self.seen.setdefault(tag, {})

    def attempts(self, *tags):
        with self.lock:
            return sum(1 for r in self.records if r['scenario'] in tags)

    def anomaly(self, tag, role, step, reason):
        with self.lock:
            self.anomalies.append({'scenario': tag, 'role': role, 'step': step, 'reason': reason[:300]})

    @staticmethod
    def classify(messages):
        for message in messages:
            text = user_text(message)
            if text and text.startswith(ASSIGNMENT):
                node, _ = json.JSONDecoder().raw_decode(text[len(ASSIGNMENT):])
                found = TAG.search(str(node.get('task', '')))
                return 'child', found.group(1) if found else None
        index = goal_index(messages)
        return 'root', TAG.search(messages[index]['content']).group(1) if index is not None else None

    def respond(self, body):
        if body.get('model') != MODEL:
            raise AssertionError('Unexpected model route reached the fixture.')
        if '_pebble_' in json.dumps(body):
            raise AssertionError('Host-private message fields reached the provider.')
        messages = body.get('messages') or []
        names = sorted(t['function']['name'] for t in body.get('tools', []))
        role, tag = self.classify(messages)
        step = sum(1 for m in (messages if role == 'child' else after_goal(messages)) if m.get('role') == 'assistant')
        with self.lock:
            key = (role, tag, step)
            self.counts[key] = attempt = self.counts.get(key, 0) + 1
            record = {'scenario': tag, 'role': role, 'step': step, 'attempt': attempt, 'tools': names}
            self.records.append(record)
        if role == 'root' and tag in PLANS:
            reply = self.plan(tag, step, messages, names)
        else:
            script = getattr(self, ('child_' if role == 'child' else 'script_') + str(tag), None)
            if script is None:
                self.anomaly(tag, role, step, 'unscripted conversation')
                reply = Reply(final('Synthetic fixture: no script.'))
            else:
                reply = script(step, attempt, messages, names)
        with self.lock:
            record['emitted'] = ([c['function']['name'] for c in reply.delta['tool_calls']]
                                 if 'tool_calls' in reply.delta else 'content')
        return reply

    def completion_check(self, tag, messages):
        last = user_text(messages[-1]) if messages else None
        if last and last.startswith(COMPLETION):
            seen = self.state(tag)
            with self.lock:
                seen['completion_repairs'] = seen.get('completion_repairs', 0) + 1
                seen['hook_issue'] = seen.get('hook_issue', False) or HOOK_ISSUE in last
            return True
        return False

    def finish(self, tag, step, messages, text=DONE):
        self.completion_check(tag, messages)
        seen = self.state(tag)
        with self.lock:
            seen['finals'] = seen.get('finals', 0) + 1
        return Reply(final(text), usage(step))

    def give_up(self, tag, role, step, reason):
        self.anomaly(tag, role, step, reason)
        return Reply(final('The scripted fixture stopped: ' + reason + '.'), usage(step) if role == 'root' else CHILD_USAGE)

    # Plan scenarios ---------------------------------------------------------------

    def plan(self, tag, step, messages, names):
        """Emit each planned call once, in order, then a final answer (repeated for repairs)."""
        seen = self.state(tag)
        if step == 0:
            with self.lock:
                seen['offered'] = sorted(WATCHED & set(names))
        done = {d['id']: d for d in history(messages)}
        steps = PLANS[tag]
        with self.lock:
            seen['results'] = {ident: summary(done[ident]['result']) for ident, _, _ in steps if ident in done}
            for item in done.values():
                listed = item['result'] if item['name'] == 'ListProjectRecipes' and isinstance(item['result'], dict) else None
                if listed is not None:
                    seen['recipes'] = [[r.get('id'), r.get('available')] for r in listed.get('recipes', []) if isinstance(r, dict)]
        if step >= 24:
            return self.give_up(tag, 'root', step, 'plan script exceeded its bound')
        for ident, name, args in steps:
            if ident not in done:
                return Reply(calls((name, ident, args)), usage(step))
        return self.finish(tag, step, messages)

    # Child worktree scenarios ---------------------------------------------------------

    def script_w1(self, step, attempt, messages, names):
        """Spawn a worktree child, wait, accept its result, export, read, integrate, retire."""
        seen = self.state('w1')
        if step == 0:
            with self.lock:
                seen['offered'] = sorted(WATCHED & set(names))
            return Reply(calls(('SpawnWorktreeAgent', 'w1-spawn', {'task': CHILD_TASKS['w1'], 'name': 'Worktree child',
                                                                   'tools': json.dumps(['Read'])})), usage(step))
        if step >= 30:
            return self.give_up('w1', 'root', step, 'worktree script exceeded its bound')
        done = history(messages)
        called = [d['name'] for d in done]
        latest = {d['name']: d for d in done}
        node = (latest.get('SpawnWorktreeAgent') or {}).get('result') or {}
        tree = node.get('worktree') if isinstance(node.get('worktree'), dict) else {}
        with self.lock:
            seen['spawn'] = spawn_summary(node)
        agent, worktree = node.get('agent_id'), tree.get('worktree_id')
        if node.get('error') or not agent or not worktree:
            return self.give_up('w1', 'root', step, 'the worktree child was not admitted')
        if 'WaitForAgents' not in called:
            return Reply(calls(('WaitForAgents', 'w1-wait', {'agent_ids': json.dumps([agent])})), usage(step))
        review = latest.get('ReviewAgentResult')
        if review is None:
            if done[-1]['name'] != 'ReadAgentTree':
                return Reply(calls(('ReadAgentTree', f'w1-tree-{step}', {})), usage(step))
            child = next((n for n in nodes_of(done[-1]['result']) if n.get('id') == agent), None)
            with self.lock:
                seen['child_status'] = child and child.get('status')
            if child is None:
                return self.give_up('w1', 'root', step, 'the child is missing from the agent tree')
            if child.get('status') in NODE_ACTIVE:
                if called.count('WaitForAgents') >= 6:
                    return self.give_up('w1', 'root', step, 'the child did not settle within the wait bound')
                return Reply(calls(('WaitForAgents', f'w1-wait-{step}', {'agent_ids': json.dumps([agent])})), usage(step))
            if child.get('status') != 'completed' or not child.get('evidence_ids'):
                return self.give_up('w1', 'root', step, 'the child settled without completed evidence')
            return Reply(calls(('ReviewAgentResult', 'w1-review', {
                'agent_id': agent, 'generation': child['generation'], 'evidence_ids': json.dumps(child['evidence_ids']),
                'decision': 'accept'})), usage(step))
        with self.lock:
            seen['review'] = {**summary(review['result']), 'review': (review['result'] or {}).get('review')}
        if (review['result'] or {}).get('error'):
            return self.give_up('w1', 'root', step, 'the child result was not accepted')
        preview = latest.get('PreviewWorktreeExport')
        if preview is None:
            return Reply(calls(('PreviewWorktreeExport', 'w1-preview', {'worktree_id': worktree,
                                                                         'file_paths': json.dumps(['feature.txt'])})), usage(step))
        export = preview['result'] or {}
        with self.lock:
            seen['preview'] = {'error': str(export['error'])[:300] if export.get('error') else None,
                               'paths': [p.get('path') for p in export.get('proposals', []) if isinstance(p, dict)],
                               'content_included': export.get('content_included')}
        if export.get('error') or not export.get('export_id'):
            return self.give_up('w1', 'root', step, 'the worktree export was not prepared')
        args = {'export_id': export['export_id'], 'file_path': 'feature.txt'}
        read = latest.get('ReadWorktreeExport')
        if read is None:
            return Reply(calls(('ReadWorktreeExport', 'w1-read', args)), usage(step))
        page = read['result'] or {}
        with self.lock:
            seen['export_read'] = {'error': str(page['error'])[:300] if page.get('error') else None,
                                   'content_matches': page.get('untrusted_content') == FEATURE_CHILD}
        integrate = latest.get('IntegrateWorktreeFile')
        if integrate is None:
            if page.get('error'):
                return self.give_up('w1', 'root', step, 'the exported file could not be read')
            with self.lock:
                root = self.shared.get('w1_root')
                seen['project_before_integration'] = bool(root) and (Path(root) / 'feature.txt').read_text() == FEATURE_HEAD
            return Reply(calls(('IntegrateWorktreeFile', 'w1-integrate', args)), usage(step))
        with self.lock:
            seen['integrate'] = summary(integrate['result'])
            seen['retire_offered_to_model'] = bool(NEVER_OFFERED & set(names))
        if (integrate['result'] or {}).get('error'):
            return self.give_up('w1', 'root', step, 'the exported file was not integrated')
        return self.finish('w1', step, messages)

    def script_w2(self, step, attempt, messages, names):
        """Spawn a worktree child in Ask mode and stop; after native review, settle the tree."""
        seen = self.state('w2')
        if step == 0:
            with self.lock:
                seen['offered'] = sorted(WATCHED & set(names))
            return Reply(calls(('SpawnWorktreeAgent', 'w2-spawn', {'task': CHILD_TASKS['w2'], 'name': 'Pending worktree child',
                                                                   'tools': json.dumps(['Read'])})), usage(step))
        done = history(messages)
        spawn = next((d for d in done if d['id'] == 'w2-spawn'), None)
        with self.lock:
            seen['spawn'] = spawn_summary(spawn and spawn['result'])
        if step == 1:
            return self.finish('w2', step, messages, 'The child worktree allocation awaits native review.')
        called = [d['name'] for d in done]
        if 'ReviewAgentResult' in called or called.count('ReadAgentTree') >= 4 or step >= 16:
            return self.finish('w2', step, messages)
        name, ident = last_call(messages)
        if name != 'ReadAgentTree':
            return Reply(calls(('ReadAgentTree', f'w2-tree-{step}', {})), usage(step))
        nodes = nodes_of(result(messages, ident))
        with self.lock:
            seen['child_status'] = [n.get('status') for n in nodes]
        reviews = [n for n in nodes if n.get('status') in NODE_TERMINAL and n.get('review') == 'pending']
        if reviews:
            return Reply(calls(*[('ReviewAgentResult', f'w2-dismiss-{step}-{index}', {
                'agent_id': n['id'], 'generation': n['generation'], 'decision': 'dismiss'}) for index, n in enumerate(reviews)]),
                usage(step))
        active = [n['id'] for n in nodes if n.get('status') in NODE_ACTIVE]
        if active:
            return Reply(calls(('WaitForAgents', f'w2-wait-{step}', {'agent_ids': json.dumps(active)})), usage(step))
        return self.finish('w2', step, messages)

    def child_w1(self, step, attempt, messages, names):
        """Read feature.txt in the managed worktree, write the new greeting there, report."""
        seen = self.state('w1')
        with self.lock:
            seen['child_requests'] = seen.get('child_requests', 0) + 1
            seen['child_offered'] = sorted(set(seen.get('child_offered', [])) | (WATCHED & set(names)))
        done = {d['id']: d for d in history(messages)}
        if 'cw-read' not in done:
            return Reply(calls(('Read', 'cw-read', {'file_path': 'feature.txt'})), CHILD_USAGE)
        read = done['cw-read']['result'] or {}
        with self.lock:
            seen['child_read'] = {'error': str(read['error'])[:300] if read.get('error') else None,
                                  'base_bytes': read.get('file_hash') == sha(FEATURE_HEAD)}
        if 'cw-write' not in done:
            if not read.get('file_hash'):
                return self.give_up('w1', 'child', step, 'the worktree read failed')
            return Reply(calls(('WriteWorktreeFile', 'cw-write', {'file_path': 'feature.txt', 'content': FEATURE_CHILD,
                                                                   'expected_hash': read['file_hash']})), CHILD_USAGE)
        write = done['cw-write']['result'] or {}
        with self.lock:
            root = self.shared.get('w1_root')
            seen['child_write'] = {'error': str(write['error'])[:300] if write.get('error') else None,
                                   'after_matches': write.get('after_hash') == sha(FEATURE_CHILD),
                                   'project_changed': write.get('project_changed')}
            seen['project_at_child_end'] = bool(root) and (Path(root) / 'feature.txt').read_text() == FEATURE_HEAD
        if step >= 6:
            return self.give_up('w1', 'child', step, 'child step beyond script')
        return Reply(final('Replaced the greeting in feature.txt inside my managed worktree; receipts cw-read and cw-write.'),
                     CHILD_USAGE)

    def child_w2(self, step, attempt, messages, names):
        seen = self.state('w2')
        with self.lock:
            seen['child_requests'] = seen.get('child_requests', 0) + 1
        return Reply(final('Nothing was done: this child should never have been dispatched.'), CHILD_USAGE)

    def script_w3(self, step, attempt, messages, names):
        """Ask mode end to end. The root parks three times for native review: the allocation, the
        child's worktree tests (executed as the root's own call) and the integrated edit."""
        seen = self.state('w3')
        if step == 0:
            with self.lock:
                seen['offered'] = sorted(WATCHED & set(names))
            return Reply(calls(('SpawnWorktreeAgent', 'w3-spawn', {'task': CHILD_TASKS['w3'], 'name': 'Tested worktree child',
                                                                   'tools': json.dumps(['Read'])})), usage(step))
        if step >= 40:
            return self.give_up('w3', 'root', step, 'worktree test script exceeded its bound')
        done = history(messages)
        called = [d['name'] for d in done]
        latest = {d['name']: d for d in done}
        node = (latest.get('SpawnWorktreeAgent') or {}).get('result') or {}
        tree = node.get('worktree') if isinstance(node.get('worktree'), dict) else {}
        with self.lock:
            seen['spawn'] = {**spawn_summary(node), 'status': node.get('status'), 'action_present': bool(node.get('action_id'))}
        agent, worktree = node.get('agent_id'), tree.get('worktree_id')
        if node.get('error') or not agent or not worktree:
            return self.give_up('w3', 'root', step, 'the worktree child was not admitted')
        name, ident = last_call(messages)
        if name == 'SpawnWorktreeAgent':
            return self.finish('w3', step, messages, 'The child worktree allocation awaits native review.')
        if called.count('WaitForAgents') >= 12:
            return self.give_up('w3', 'root', step, 'the child did not progress within the wait bound')
        wait = Reply(calls(('WaitForAgents', f'w3-wait-{step}', {'agent_ids': json.dumps([agent])})), usage(step))
        execute = latest.get('ExecuteAgentEffect')
        if execute is None:
            # Wait for the child's worktree test request, read it, then run it as the root's own call.
            if name == 'WaitForAgents':
                return Reply(calls(('ReadAgentEffects', f'w3-effects-{step}', {})), usage(step))
            if name == 'ReadAgentEffects':
                requests = [r for r in (result(messages, ident) or {}).get('requests', []) if isinstance(r, dict)]
                found = next((r for r in requests if r.get('tool') == 'RunTests' and r.get('status') == 'proposed'), None)
                if found:
                    with self.lock:
                        seen['request_view'] = {'arguments': found.get('untrusted_arguments'), 'executable': found.get('executable'),
                                                'blocked_reason': str(found['blocked_reason'])[:300] if found.get('blocked_reason') else None}
                    if not found.get('executable'):
                        return self.give_up('w3', 'root', step, 'the child test request is not executable')
                    return Reply(calls(('ExecuteAgentEffect', 'w3-execute', {'request_id': found['request_id']})), usage(step))
            return wait
        ran = execute['result'] or {}
        with self.lock:
            seen['execute'] = {**summary(ran), 'effect_status': ran.get('effect_status')}
        if ran.get('error'):
            return self.give_up('w3', 'root', step, 'the child test request was not executed')
        if name == 'ExecuteAgentEffect':
            return self.finish('w3', step, messages, 'The worktree tests await native review.')
        review = latest.get('ReviewAgentResult')
        if review is None:
            if name != 'ReadAgentTree':
                return Reply(calls(('ReadAgentTree', f'w3-tree-{step}', {})), usage(step))
            child = next((n for n in nodes_of(result(messages, ident)) if n.get('id') == agent), None)
            with self.lock:
                seen['child_status'] = child and child.get('status')
            if child is None:
                return self.give_up('w3', 'root', step, 'the child is missing from the agent tree')
            if child.get('status') in NODE_ACTIVE:
                return wait
            if child.get('status') != 'completed' or not child.get('evidence_ids'):
                return self.give_up('w3', 'root', step, 'the child settled without completed evidence')
            return Reply(calls(('ReviewAgentResult', 'w3-review', {
                'agent_id': agent, 'generation': child['generation'], 'evidence_ids': json.dumps(child['evidence_ids']),
                'decision': 'accept'})), usage(step))
        with self.lock:
            seen['review'] = {**summary(review['result']), 'review': (review['result'] or {}).get('review')}
        if (review['result'] or {}).get('error'):
            return self.give_up('w3', 'root', step, 'the child result was not accepted')
        preview = latest.get('PreviewWorktreeExport')
        if preview is None:
            return Reply(calls(('PreviewWorktreeExport', 'w3-preview', {'worktree_id': worktree,
                                                                         'file_paths': json.dumps(['feature.txt'])})), usage(step))
        export = preview['result'] or {}
        with self.lock:
            seen['preview'] = {'error': str(export['error'])[:300] if export.get('error') else None,
                               'paths': [p.get('path') for p in export.get('proposals', []) if isinstance(p, dict)]}
        if export.get('error') or not export.get('export_id'):
            return self.give_up('w3', 'root', step, 'the worktree export was not prepared')
        args = {'export_id': export['export_id'], 'file_path': 'feature.txt'}
        read = latest.get('ReadWorktreeExport')
        if read is None:
            return Reply(calls(('ReadWorktreeExport', 'w3-read', args)), usage(step))
        page = read['result'] or {}
        with self.lock:
            seen['export_read'] = {'error': str(page['error'])[:300] if page.get('error') else None,
                                   'content_matches': page.get('untrusted_content') == FEATURE_CHILD}
        integrate = latest.get('IntegrateWorktreeFile')
        if integrate is None:
            if page.get('error'):
                return self.give_up('w3', 'root', step, 'the exported file could not be read')
            return Reply(calls(('IntegrateWorktreeFile', 'w3-integrate', args)), usage(step))
        with self.lock:
            seen['integrate'] = summary(integrate['result'])
        if name == 'IntegrateWorktreeFile':
            if (integrate['result'] or {}).get('error'):
                return self.give_up('w3', 'root', step, 'the exported file was not staged')
            return self.finish('w3', step, messages, 'The integrated file awaits native review.')
        return self.finish('w3', step, messages)

    def child_w3(self, step, attempt, messages, names):
        """Read and write feature.txt in the worktree, ask the root to test the worktree, stay live until released."""
        seen = self.state('w3')
        with self.lock:
            seen['child_requests'] = seen.get('child_requests', 0) + 1
            seen['child_offered'] = sorted(set(seen.get('child_offered', [])) | (WATCHED & set(names)))
            root = self.shared.get('w3_root')
        if step >= 8:
            return self.give_up('w3', 'child', step, 'child step beyond script')
        done = {d['id']: d for d in history(messages)}
        if 'cw3-read' not in done:
            return Reply(calls(('Read', 'cw3-read', {'file_path': 'feature.txt'})), CHILD_USAGE)
        read = done['cw3-read']['result'] or {}
        with self.lock:
            seen['child_read'] = {'error': str(read['error'])[:300] if read.get('error') else None,
                                  'base_bytes': read.get('file_hash') == sha(FEATURE_HEAD)}
        if 'cw3-write' not in done:
            if not read.get('file_hash'):
                return self.give_up('w3', 'child', step, 'the worktree read failed')
            return Reply(calls(('WriteWorktreeFile', 'cw3-write', {'file_path': 'feature.txt', 'content': FEATURE_CHILD,
                                                                    'expected_hash': read['file_hash']})), CHILD_USAGE)
        write = done['cw3-write']['result'] or {}
        with self.lock:
            seen['child_write'] = {'error': str(write['error'])[:300] if write.get('error') else None,
                                   'after_matches': write.get('after_hash') == sha(FEATURE_CHILD),
                                   'project_changed': write.get('project_changed'),
                                   'project_unchanged': bool(root) and (Path(root) / 'feature.txt').read_text() == FEATURE_HEAD}
        if 'cw3-request' not in done:
            if write.get('error'):
                return self.give_up('w3', 'child', step, 'the worktree write failed')
            return Reply(calls(('RequestAgentEffect', 'cw3-request', {
                'tool': 'RunTests', 'arguments': json.dumps(WORKTREE_TESTS),
                'purpose': 'Run python-unittest on my managed worktree.'})), CHILD_USAGE)
        request = done['cw3-request']['result'] or {}
        with self.lock:
            seen['child_request'] = {'error': str(request['error'])[:300] if request.get('error') else None,
                                     'status': request.get('status'), 'authorization': request.get('authorization'),
                                     'request_present': bool(request.get('request_id'))}
        if 'cw3-outcome' not in done:
            if not request.get('request_id'):
                return self.give_up('w3', 'child', step, 'the worktree test request was not stored')
            # This open provider request keeps the child live, holding its leased generation, until the
            # root executed the request and a person approved the tests (as validate_phase9_effects does).
            released = self.events['w3'].wait(RELEASE_SECONDS)
            with self.lock:
                seen['child_released'] = released
            if not released:
                self.anomaly('w3', 'child', step, 'native test approval never released the child')
            return Reply(calls(('ReadAgentEffect', 'cw3-outcome', {'request_id': request['request_id']})), CHILD_USAGE)
        outcome = done['cw3-outcome']['result'] or {}
        with self.lock:
            seen['child_outcome'] = {'error': str(outcome['error'])[:300] if outcome.get('error') else None,
                                     'status': outcome.get('status'), 'evidence': outcome.get('evidence'),
                                     'authorization': outcome.get('authorization')}
        return Reply(final('Replaced the greeting in my worktree; the root ran the worktree tests (receipt data only).'),
                     CHILD_USAGE)


class Campaign:
    def __init__(self, command, backend):
        self.command, self.backend = command, backend
        self.fixture = Fixture()
        self.checks, self.names = [], set()
        self.facts, self.observations = {}, {'run_states': {}}
        self.fatal = None
        self.stderr_tails = []
        self.git = None

    # Check bookkeeping ---------------------------------------------------------

    def check(self, name, fn):
        assert name not in self.names, 'Check names must be unique: ' + name
        self.names.add(name)
        try:
            evidence = fn() or {}
            self.checks.append({'name': name, 'passed': True, 'evidence': evidence})
            return True
        except Exception as exc:
            self.checks.append({'name': name, 'passed': False, 'error': bounded(exc, 2000),
                                'trace': traceback.format_exc(limit=4)[-1500:]})
            return False

    def passing(self):
        return all(c['passed'] for c in self.checks)

    def need(self, key):
        if key not in self.facts:
            raise AssertionError('Prerequisite was not established: ' + key)
        return self.facts[key]

    def seen(self, tag):
        return self.fixture.state(tag)

    # Service helpers -------------------------------------------------------------

    def boot(self, home):
        return Service(self.command, home)

    def close(self, service, keep_tail):
        if keep_tail:
            try:
                service.stderr.seek(0)
                self.stderr_tails.append(service.stderr.read()[-4000:])
            except Exception as exc:
                self.stderr_tails.append('stderr unavailable: ' + bounded(exc))
        service.close()

    def crash(self, service):
        """SIGKILL: no shutdown code runs, exactly like a hard crash."""
        service.process.kill()
        service.process.wait(timeout=10)
        for stream in (service.process.stdin, service.process.stdout):
            try:
                stream.close()
            except Exception:
                pass
        try:
            service.stderr.seek(0)
            self.stderr_tails.append('crash tail: ' + service.stderr.read()[-2000:])
            service.stderr.close()
        except Exception:
            pass

    @staticmethod
    def until(predicate, seconds, label):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f'Timed out after {seconds}s waiting for {label}.')

    def diagnostics(self, run, tag):
        with self.fixture.lock:
            return {'state': run and run.get('state'), 'run_error': run and (run.get('error') or '')[:300],
                    'tools': [[t['call_id'], t['name'], t['state']] for t in (run or {}).get('tools', [])][-20:],
                    'provider_errors': self.fixture.errors[-5:],
                    'anomalies': [a for a in self.fixture.anomalies if a['scenario'] == tag][-5:]}

    def release(self, service, cid, rid, tag):
        """After a settle timeout, stop the run so later independent scenarios keep their lane."""
        try:
            service.rpc('cancel', id=cid, run_id=rid)
            self.until(lambda: not service.rpc('state')['active'], 15, tag + ' to release the active lane')
            self.observations.setdefault('released_runs', []).append(tag)
        except Exception as exc:
            self.observations.setdefault('release_failures', []).append([tag, bounded(exc, 300)])

    def settle(self, service, cid, rid, tag, seconds=SETTLE_SECONDS, accept=None):
        seen = {}

        def probe():
            seen['run'] = run = service.rpc('run_get', id=cid, run_id=rid)
            if accept is not None:
                return run if accept(run) else None
            if run['state'] in TRANSIENT or service.rpc('state')['active']:
                return None
            return run
        try:
            run = self.until(probe, seconds, tag + ' to settle')
        except AssertionError:
            self.release(service, cid, rid, tag)
            raise AssertionError(json.dumps(self.diagnostics(seen.get('run'), tag))) from None
        self.observations['run_states'][tag] = run['state']
        return run

    def resumed(self, service, fact, tag, seconds=SETTLE_SECONDS):
        cid, rid = fact['cid'], fact['rid']
        self.until(lambda: service.rpc('run_get', id=cid, run_id=rid)['state'] != 'waiting_approval', 20,
                   tag + ' to resume after native review')
        fact['run'] = run = self.settle(service, cid, rid, tag, seconds)
        return run

    @staticmethod
    def idle(service, method, **params):
        """A native configuration RPC, retried briefly while settled child workers release the lane."""
        deadline = time.monotonic() + 10
        while True:
            try:
                return service.rpc(method, **params)
            except RuntimeError as exc:
                if not str(exc).startswith('Stop ') or time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def chat(self, service, tag):
        cid = service.rpc('new_chat')['id']
        self.idle(service, 'set_workspace', id=cid, path=str(self.need('root:' + tag)))
        return cid

    def start(self, service, tag, mode, cid=None, steps=32):
        """Set the native permission mode, then send the tagged goal in a new conversation."""
        root = self.need('root:' + tag)
        self.idle(service, 'setting', key='permissions', value=permissions(mode))
        cid = cid or self.chat(service, tag)
        rid = service.rpc('send', id=cid, text=GOALS[tag], limits={'max_steps': steps, 'max_seconds': 300})['run_id']
        fact = self.facts[tag] = {'cid': cid, 'rid': rid, 'root': root, 'mode': mode}
        return fact

    @staticmethod
    def actions(service, cid, kind=None, status=None):
        return [a for a in service.rpc('get_chat', id=cid)['actions']
                if (kind is None or a['kind'] == kind) and (status is None or a['status'] == status)]

    def one_pending(self, service, fact, kind):
        pending = self.actions(service, fact['cid'], kind, 'pending')
        if len(pending) != 1:
            raise AssertionError(('Expected exactly one pending ' + kind + ' action.',
                                  [(a['kind'], a['status']) for a in self.actions(service, fact['cid'])]))
        return pending[0]

    def receipts(self, service, cid):
        return service.rpc('git_plan_receipts', id=cid)['items']

    def repo_state(self, root, files=()):
        git = self.git
        return {'head': git(root, 'rev-parse', 'HEAD').decode().strip(),
                'refs': sha(git(root, 'for-each-ref', '--format=%(objectname) %(refname)')),
                'index': sha(git(root, 'ls-files', '--stage', '-z')),
                'files': {name: sha((root / name).read_bytes()) if (root / name).exists() else None for name in files}}

    def staged_names(self, root):
        return staged_names(self.git, root)

    def changed_paths(self, root, commit):
        return sorted(n for n in self.git(root, 'diff-tree', '--no-commit-id', '--name-only', '-r', '-z', commit).decode().split('\0') if n)

    def tree_blob(self, root, commit, path):
        return self.git(root, 'rev-parse', commit + ':' + path).decode().strip()

    def remote_ref(self, remote, ref='refs/heads/main'):
        found = self.git(remote.parent, '--git-dir=' + str(remote), 'rev-parse', '--verify', '--quiet', ref, ok=(0, 1))
        return found.decode().strip() or None

    def markers(self, *names):
        folder = self.need('markers')
        return {name: (folder / name).exists() for name in names}

    # Campaign --------------------------------------------------------------------

    def run(self, scratch):
        home = scratch / 'home'
        home.mkdir()
        self.facts['home'] = home

        def projects():
            assert not scratch.is_relative_to(ROOT), 'Fixture projects must live outside this checkout and .runtime.'
            self.git = GitFixture(scratch)
            built = build_projects(scratch, self.git)
            for tag, root in built['roots'].items():
                self.facts['root:' + tag] = root
            self.facts['markers'] = built['markers']
            self.facts['remotes'] = built['remotes']
            with self.fixture.lock:
                self.fixture.shared['w1_root'] = str(built['roots']['w1'])
                self.fixture.shared['w3_root'] = str(built['roots']['w3'])
            return {'projects': len(built['roots']), 'git_projects': len(GIT_PROJECTS), 'bare_remotes': 2}
        self.check('fixture_projects_created_with_system_git_outside_runtime', projects)
        self.fixture.start()
        try:
            service = self.boot(home)
            crashed = False
            try:
                crashed = self.phase_a(service)
            finally:
                if not crashed:
                    self.close(service, keep_tail=not self.passing())
            self.check('k1_forged_executing_receipt_inserted_while_service_stopped', self.forge)
            service = self.boot(home)
            try:
                self.timed(self.phase_b, service)
            finally:
                self.close(service, keep_tail=not self.passing())
        except Exception as exc:
            self.fatal = bounded(exc, 3000)
        finally:
            self.fixture.stop()
            with self.fixture.lock:
                # Tool names only: shows at a glance which Phase 13 routes each root was offered.
                self.observations['offered'] = {tag: seen.get('offered') for tag, seen in self.fixture.seen.items()}
                self.observations['completion_repairs'] = {tag: seen.get('completion_repairs', 0)
                                                           for tag, seen in self.fixture.seen.items()}

    def phase_a(self, service):
        def profile():
            service.rpc('model_save', name='Phase 13 fixture', url=self.fixture.url, model=MODEL, context=CONTEXT)
            service.rpc('setting', key='memory_enabled', value=False)  # memory is not under test here
            state = service.rpc('state')
            assert state['guard']['available'] is True, 'The real vendored AgentGuard engine must be available.'
            return {'guard_available': True}
        if not self.check('loopback_profile_saved_and_actual_guard_available', profile):
            return False

        def platform():
            assert sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(), (
                'Confined Git, tests and child worktrees need macOS sandbox-exec; elsewhere they must fail closed.')
            assert Path(GIT).is_file(), 'The fixture needs the system Git to build owned repositories.'
            return {'platform': sys.platform}
        self.check('platform_supports_confined_git_tests_and_worktrees', platform)
        for scenario in (self.scenario_g1, self.scenario_g2, self.scenario_g3, self.scenario_g4, self.scenario_n1,
                         self.scenario_u1, self.scenario_r1, self.scenario_e1, self.scenario_w1, self.scenario_w2,
                         self.scenario_w3, self.scenario_x1):
            self.timed(scenario, service)
        return self.timed(self.scenario_k1, service)

    def timed(self, scenario, service):
        """Run one scenario and record its wall-clock seconds (observations only)."""
        started = time.monotonic()
        try:
            return scenario(service)
        finally:
            self.observations.setdefault('scenario_seconds', {})[scenario.__name__.replace('scenario_', '')] = \
                round(time.monotonic() - started, 1)

    def require_tests(self, service, enabled):
        """Toggle one declarative task_end require_tests hook, created on first use; never while a task runs."""
        hook = self.facts.get('require_tests_hook')
        if hook is None:
            if not enabled:
                return
            hook = self.facts['require_tests_hook'] = service.rpc(
                'extension_add', kind='hook', name='Phase 13 require tests',
                content=json.dumps({'version': 1, 'event': 'task_end', 'require_tests': True}))
        self.idle(service, 'extension_toggle', id=hook, enabled=enabled)

    # g1: Ask-mode commit of one selected path ----------------------------------------

    def scenario_g1(self, service):
        files = ('a.txt', 'b.txt', 'c.txt')

        def pending():
            root = self.need('root:g1')
            before = self.repo_state(root, files)
            staged = self.staged_names(root)
            fact = self.start(service, 'g1', 'ask')
            fact.update(before=before, staged_before=staged)
            fact['first_state'] = self.settle(service, fact['cid'], fact['rid'], 'g1')['state']
            action = fact['action'] = self.one_pending(service, fact, 'git')
            payload = action['payload']
            fact['before_approval'] = self.repo_state(root, files)
            assert fact['first_state'] == 'waiting_approval', fact['first_state']
            assert payload.get('operation') == 'commit' and payload.get('paths') == ['a.txt'], payload.get('paths')
            assert payload.get('requires_review') is False and payload.get('hooks_run') is False, 'A local commit plan changed shape.'
            got = self.seen('g1').get('results', {}).get('g1-commit') or {}
            assert got.get('status') == 'awaiting_user_approval' and got.get('action_present'), got
            return {'first_state': fact['first_state'], 'operation': 'commit', 'paths': payload['paths']}
        self.check('g1_ask_commit_plan_stays_pending_for_native_review', pending)

        def offered():
            assert 'ProposeGitChange' in (self.seen('g1').get('offered') or []), self.seen('g1').get('offered')
            return {'offered': self.seen('g1')['offered']}
        self.check('g1_reviewed_git_plan_tool_offered_to_the_root', offered)

        def unchanged():
            fact = self.need('g1')
            assert fact['before'] == fact['before_approval'], 'The repository changed before native approval.'
            assert self.receipts(service, fact['cid']) == [], 'A receipt exists before native approval.'
            return {'head': fact['before']['head'][:12], 'receipts': 0}
        self.check('g1_nothing_changes_before_native_approval', unchanged)

        def applied():
            fact, root = self.need('g1'), self.need('root:g1')
            binding = next(p for p in fact['action']['payload']['bindings']['paths'] if p['path'] == 'a.txt')
            fact['receipt'] = receipt = service.rpc('approve', id=fact['cid'], action_id=fact['action']['id'])
            fact['after'] = after = self.repo_state(root, files)
            commit_id = (receipt.get('result') or {}).get('commit')
            assert receipt.get('state') == 'applied' and receipt.get('effect_started') is True, \
                (receipt.get('state'), receipt.get('error'))
            assert commit_id == after['head'] != fact['before']['head'], 'HEAD does not hold the receipt commit.'
            assert self.tree_blob(root, 'HEAD', 'a.txt') == binding['blob_oid'] == blob_oid(A_WORK), \
                "HEAD's a.txt is not the reviewed blob."
            assert self.git(root, 'rev-parse', 'HEAD^').decode().strip() == fact['before']['head']
            self.resumed(service, fact, 'g1')
            return {'receipt_state': 'applied', 'blob': binding['blob_oid'][:12]}
        self.check('g1_native_approval_applies_and_head_holds_the_exact_reviewed_blob', applied)

        def selected():
            fact, root = self.need('g1'), self.need('root:g1')
            changed = self.changed_paths(root, fact['after']['head'])
            assert changed == ['a.txt'], changed
            return {'changed_paths': changed}
        self.check('g1_commit_contains_only_the_selected_path', selected)

        def others():
            fact, root = self.need('g1'), self.need('root:g1')
            after = fact.get('after') or self.repo_state(root, files)
            assert after['files']['b.txt'] == fact['before']['files']['b.txt'] == sha(B_WORK), 'Dirty b.txt changed.'
            assert after['files']['c.txt'] == fact['before']['files']['c.txt'] == sha(C_STAGED), 'Staged c.txt changed.'
            entries = self.git(root, 'ls-files', '--stage', '--', 'b.txt', 'c.txt').decode().split('\n')
            assert [e.split()[1] for e in entries if e] == [blob_oid(B_HEAD), blob_oid(C_STAGED)], 'An index entry changed.'
            assert self.staged_names(root) == fact['staged_before'] == ['c.txt'], 'The staged set changed.'
            return {'dirty_kept': ['b.txt'], 'staged_kept': ['c.txt']}
        self.check('g1_other_dirty_and_staged_work_stays_exactly_as_before', others)

        def hooks():
            found = self.markers('g1-pre-commit', 'g1-post-commit')
            assert not any(found.values()), found
            return {'markers': found}
        self.check('g1_repository_hooks_never_run', hooks)

        def readback():
            fact = self.need('g1')
            receipt_id = (fact.get('receipt') or {}).get('receipt_id')
            view = service.rpc('git_plan_readback', id=fact['cid'], receipt_id=receipt_id)
            assert view.get('verified') is True and view.get('matches_receipt') is True, \
                (view.get('verified'), view.get('matches_receipt'), str(view.get('issue'))[:300])
            assert view.get('authorization') == 'none' and view.get('effect') == 'none'
            return {'matches_receipt': True}
        self.check('g1_native_readback_matches_the_receipt', readback)

    # g2: stale plan ---------------------------------------------------------------------

    def scenario_g2(self, service):
        def refused():
            root = self.need('root:g2')
            fact = self.start(service, 'g2', 'ask')
            self.settle(service, fact['cid'], fact['rid'], 'g2')
            action = fact['action'] = self.one_pending(service, fact, 'git')
            fact['head'] = self.repo_state(root)['head']
            (root / 'a.txt').write_text(G2_DRIFT)  # the person edits the selected file after proposal
            fact['error'] = error = rejected(service, 'approve', id=fact['cid'], action_id=action['id'])
            assert 'changed after review' in error, error
            return {'refusal': error[:160]}
        self.check('g2_stale_plan_is_refused_at_native_approval', refused)

        def unmoved():
            fact, root = self.need('g2'), self.need('root:g2')
            assert self.repo_state(root)['head'] == fact['head'], 'The ref moved after a refused approval.'
            assert (root / 'a.txt').read_text() == G2_DRIFT, "The person's later change was not kept."
            items = [r for r in self.receipts(service, fact['cid']) if r['action_id'] == fact['action']['id']]
            assert [(r['state'], r['effect_started']) for r in items] == [('failed', False)], \
                [(r['state'], r['effect_started']) for r in items]
            status = [a['status'] for a in self.actions(service, fact['cid'], 'git')]
            assert status == ['failed'], status
            return {'receipt': 'failed', 'effect_started': False}
        self.check('g2_ref_unmoved_and_receipt_failed_before_any_effect', unmoved)

    # g3: Full-mode commit, reviewed discard and reviewed undo --------------------------------

    def scenario_g3(self, service):
        def automatic():
            root = self.need('root:g3')
            fact = self.start(service, 'g3', 'full')
            fact['before'] = before = self.repo_state(root, ('a.txt', 'd.txt'))
            fact['first_state'] = self.settle(service, fact['cid'], fact['rid'], 'g3')['state']
            commits = [a for a in self.actions(service, fact['cid'], 'git') if a['payload'].get('operation') == 'commit']
            assert [a['status'] for a in commits] == ['applied'], [a['status'] for a in commits]
            got = self.seen('g3').get('results', {}).get('g3-commit') or {}
            assert got.get('status') == 'applied' and got.get('result_state') == 'applied', got
            head = self.repo_state(root)['head']
            assert head != before['head'] and self.tree_blob(root, head, 'a.txt') == blob_oid(A_WORK)
            return {'commit_status': 'applied', 'first_state': fact['first_state']}
        self.check('g3_full_mode_commit_applies_automatically', automatic)

        def discard_pending():
            fact, root = self.need('g3'), self.need('root:g3')
            action = fact['discard'] = self.one_pending(service, fact, 'git')
            payload = action['payload']
            assert payload.get('operation') == 'discard' and payload.get('paths') == ['d.txt'], payload.get('paths')
            assert payload.get('destructive') is True and payload.get('requires_review') is True
            assert payload.get('permission_mode') == 'full', payload.get('permission_mode')
            assert (root / 'd.txt').read_text() == D_WORK, 'd.txt changed before native review.'
            return {'destructive': True, 'permission_mode': 'full'}
        self.check('g3_discard_stays_pending_for_native_review_in_full_mode', discard_pending)

        def preserved():
            fact, root = self.need('g3'), self.need('root:g3')
            fact['discard_receipt'] = receipt = service.rpc('approve', id=fact['cid'], action_id=fact['discard']['id'])
            kept = ((receipt.get('result') or {}).get('preserved') or {}).get('d.txt') or {}
            assert receipt.get('state') == 'applied', (receipt.get('state'), receipt.get('error'))
            assert (root / 'd.txt').read_text() == D_HEAD, 'd.txt was not restored to its index content.'
            assert kept.get('blob_oid') == blob_oid(D_WORK), 'The receipt does not name the prior bytes.'
            assert self.git(root, 'cat-file', 'blob', kept['blob_oid']) == D_WORK.encode(), 'git cat-file cannot read the prior bytes.'
            self.resumed(service, fact, 'g3')
            return {'preserved_blob': kept['blob_oid'][:12]}
        self.check('g3_approved_discard_preserves_prior_bytes_as_a_readable_blob', preserved)

        def undo():
            fact, root = self.need('g3'), self.need('root:g3')
            original = (fact.get('discard_receipt') or {}).get('receipt_id')
            staged = service.rpc('git_plan_propose', id=fact['cid'], operation='undo', receipt_id=original)
            assert staged.get('status') == 'awaiting_user_approval' and staged.get('action_id'), staged.get('status')
            assert (root / 'd.txt').read_text() == D_HEAD, 'The undo plan changed d.txt before native review.'
            receipt = service.rpc('approve', id=fact['cid'], action_id=staged['action_id'])
            assert receipt.get('state') == 'applied', (receipt.get('state'), receipt.get('error'))
            assert (root / 'd.txt').read_text() == D_WORK, 'The reviewed undo did not restore the preserved bytes.'
            states = {r['receipt_id']: r['state'] for r in self.receipts(service, fact['cid'])}
            assert states.get(original) == 'undone' and states.get(receipt['receipt_id']) == 'applied', states
            return {'undo': 'applied', 'discard_receipt': 'undone'}
        self.check('g3_reviewed_undo_plan_restores_the_discarded_bytes', undo)

    # g4: push to a local bare remote ----------------------------------------------------------

    def scenario_g4(self, service):
        remotes = lambda: self.need('remotes')

        def pending():
            root = self.need('root:g4')
            fact = self.start(service, 'g4', 'full')
            fact['head'] = self.repo_state(root)['head']
            self.settle(service, fact['cid'], fact['rid'], 'g4')
            action = fact['action'] = self.one_pending(service, fact, 'git')
            payload = action['payload']
            assert payload.get('operation') == 'push' and payload.get('remote') == 'origin', payload.get('remote')
            assert payload.get('publication') is True and payload.get('requires_review') is True
            assert payload.get('network') is False and payload.get('hooks_run') is False
            assert self.remote_ref(remotes()['origin']) is None, 'The remote moved before native review.'
            return {'publication': True, 'permission_mode': payload.get('permission_mode')}
        self.check('g4_push_waits_for_native_review_as_publication_in_full_mode', pending)

        def published():
            fact = self.need('g4')
            receipt = fact['receipt'] = service.rpc('approve', id=fact['cid'], action_id=fact['action']['id'])
            assert receipt.get('state') == 'applied', (receipt.get('state'), receipt.get('error'))
            assert self.remote_ref(remotes()['origin']) == fact['head'] == (receipt.get('result') or {}).get('new_oid')
            self.git(remotes()['origin'].parent, '--git-dir=' + str(remotes()['origin']), 'cat-file', '-e', fact['head'] + '^{commit}')
            view = service.rpc('git_plan_readback', id=fact['cid'], receipt_id=receipt['receipt_id'])
            assert view.get('verified') is True and view.get('matches_receipt') is True, (view.get('verified'), view.get('issue'))
            self.resumed(service, fact, 'g4')
            return {'remote_head': fact['head'][:12], 'matches_receipt': True}
        self.check('g4_approved_push_reads_back_on_the_local_bare_remote', published)

        def hooks():
            found = self.markers('g4-origin-pre-receive', 'g4-hooked-pre-receive')
            assert not any(found.values()), found
            return {'markers': found}
        self.check('g4_remote_hooks_never_run', hooks)

        def hooked():
            fact = self.need('g4')
            got = self.seen('g4').get('results', {}).get('g4-hooked') or {}
            assert got.get('error') and 'executable hooks' in got['error'] and not got.get('action_present'), got
            assert self.remote_ref(remotes()['hooked']) is None, 'A refused remote moved.'
            assert len(self.actions(service, fact['cid'], 'git')) == 1, 'A refused push staged an action.'
            return {'refusal': got['error'][:120]}
        self.check('g4_remote_with_an_executable_hook_is_refused_at_proposal', hooked)

        def https():
            fact = self.need('g4')
            got = self.seen('g4').get('results', {}).get('g4-web') or {}
            assert got.get('error') and 'transport_unsupported' in got['error'] and not got.get('action_present'), got
            assert [a['payload'].get('remote') for a in self.actions(service, fact['cid'], 'git')] == ['origin']
            return {'refusal': 'transport_unsupported'}
        self.check('g4_https_remote_is_refused_as_transport_unsupported_with_no_pending_action', https)

    # n1: nested repository boundary -------------------------------------------------------------

    def scenario_n1(self, service):
        def nested():
            root = self.need('root:n1')
            inner = root / 'vendor' / 'inner'
            before = (self.repo_state(root, ('a.txt',)), self.repo_state(inner, ('app.py',)))
            fact = self.start(service, 'n1', 'ask')
            self.settle(service, fact['cid'], fact['rid'], 'n1')
            got = self.seen('n1').get('results', {}).get('n1-commit') or {}
            assert got.get('error') and 'nested Git repository' in got['error'] and not got.get('action_present'), got
            assert self.actions(service, fact['cid']) == [], 'A refused plan staged an action.'
            assert self.receipts(service, fact['cid']) == [], 'A refused plan left a receipt.'
            assert (self.repo_state(root, ('a.txt',)), self.repo_state(inner, ('app.py',))) == before, 'A repository changed.'
            assert (inner / 'app.py').read_text() == APP_WORK, 'The nested working file changed.'
            return {'refusal': got['error'][:160], 'pending_actions': 0}
        self.check('n1_commit_inside_a_nested_repository_is_refused_at_proposal_with_no_pending_action', nested)

    # u1: verified local undo ------------------------------------------------------------------

    def scenario_u1(self, service):
        def undone():
            root = self.need('root:u1')
            fact = self.start(service, 'u1', 'auto')
            self.settle(service, fact['cid'], fact['rid'], 'u1')
            edits = {a['payload'].get('path'): a for a in self.actions(service, fact['cid'], 'edit', 'applied')}
            assert sorted(edits) == ['a.txt', 'b.txt'], sorted(edits)
            fact['edits'] = edits
            assert (root / 'a.txt').read_text() == U1_A_NEW and (root / 'b.txt').read_text() == U1_B_NEW
            assert service.rpc('undo', id=fact['cid'], action_id=edits['a.txt']['id']) is True
            action = next(a for a in self.actions(service, fact['cid'], 'edit') if a['id'] == edits['a.txt']['id'])
            receipt = action['payload'].get('undo') or {}
            assert action['status'] == 'undone' and receipt.get('verified') is True, (action['status'], receipt)
            assert receipt.get('readback_hash') == receipt.get('expected_hash') == sha(U1_A_OLD)
            assert (root / 'a.txt').read_text() == U1_A_OLD
            return {'status': 'undone', 'verified': True}
        self.check('u1_undo_of_an_applied_edit_records_a_verified_receipt', undone)

        def refused():
            fact, root = self.need('u1'), self.need('root:u1')
            ident = fact['edits']['b.txt']['id']
            (root / 'b.txt').write_text(U1_B_EXTERNAL)
            error = rejected(service, 'undo', id=fact['cid'], action_id=ident)
            assert 'changed' in error.lower(), error
            assert (root / 'b.txt').read_text() == U1_B_EXTERNAL, 'A refused undo overwrote the external change.'
            action = next(a for a in self.actions(service, fact['cid'], 'edit') if a['id'] == ident)
            assert action['status'] == 'applied' and 'undo' not in action['payload'], action['status']
            return {'refusal': error[:120]}
        self.check('u1_undo_after_an_external_change_is_refused_and_the_change_kept', refused)

    # r1-r3: project recipes --------------------------------------------------------------------

    def scenario_r1(self, service):
        def available():
            self.idle(service, 'setting', key='permissions', value=permissions('ask'))
            cid = self.facts['r1_cid'] = self.chat(service, 'r1')
            rows = {row['id']: row for row in service.rpc('project_recipes', id=cid)['recipes']}
            pytest = rows.get('python-pytest') or {}
            assert pytest.get('available') is True, ('The python-pytest recipe needs the framework Python with pytest '
                                                     'installed.', str(pytest.get('issue'))[:300])
            return {'recipes': sorted(rows), 'python_pytest': True}
        self.check('r1_python_pytest_recipe_available_through_the_framework_python', available)

        def listed():
            fact = self.start(service, 'r1', 'ask', cid=self.need('r1_cid'))
            self.settle(service, fact['cid'], fact['rid'], 'r1')
            got = self.seen('r1')
            listing = dict(map(tuple, got.get('recipes') or []))
            assert not (got.get('results', {}).get('r1-list') or {}).get('error'), got.get('results')
            assert listing.get('python-pytest') is True, listing
            return {'recipes': sorted(listing)}
        self.check('r1_list_project_recipes_reports_the_detected_recipe', listed)

        def passed():
            fact = self.need('r1')
            action = self.one_pending(service, fact, 'tests')
            assert action['payload'].get('recipe') == 'python-pytest', action['payload'].get('recipe')
            outcome = service.rpc('approve', id=fact['cid'], action_id=action['id'])
            tests = outcome.get('tests') or {}
            assert outcome.get('outcome') == 'passed' and outcome.get('exit_code') == 0, (outcome.get('outcome'), outcome.get('exit_code'))
            assert type(tests.get('total')) is int and tests['total'] > 0 and tests.get('passed') == tests['total'], tests
            assert HEX64.fullmatch(str(outcome.get('snapshot_digest'))), 'No snapshot digest was recorded.'
            self.resumed(service, fact, 'r1')
            return {'outcome': 'passed', 'total': tests['total'], 'recipe': outcome.get('recipe')}
        self.check('r1_run_tests_auto_passes_after_native_approval_with_a_snapshot_digest', passed)

        def failed():
            fact = self.start(service, 'r2', 'ask')
            self.settle(service, fact['cid'], fact['rid'], 'r2')
            action = self.one_pending(service, fact, 'tests')
            outcome = service.rpc('approve', id=fact['cid'], action_id=action['id'])
            tests = outcome.get('tests') or {}
            assert outcome.get('outcome') == 'failed' and tests.get('failed', 0) >= 1, (outcome.get('outcome'), tests)
            self.resumed(service, fact, 'r2')
            return {'outcome': 'failed', 'failed': tests.get('failed')}
        self.check('r2_failing_project_yields_failed', failed)

        def zero():
            self.require_tests(service, True)
            try:
                fact = self.start(service, 'r3', 'auto')
                fact['run'] = self.settle(service, fact['cid'], fact['rid'], 'r3', seconds=90)
            finally:
                self.require_tests(service, False)
            tests = self.actions(service, fact['cid'], 'tests', 'applied')
            edits = self.actions(service, fact['cid'], 'edit', 'applied')
            assert len(edits) == 1 and len(tests) == 1, (len(edits), len(tests))
            outcome = tests[0]['payload'].get('result') or {}
            assert outcome.get('outcome') == 'no_tests', outcome.get('outcome')
            return {'outcome': 'no_tests', 'recipe': outcome.get('recipe')}
        self.check('r3_zero_test_project_yields_no_tests', zero)

        def blocked():
            fact = self.need('r3')
            run = fact.get('run') or {}
            got = self.seen('r3')
            answers = [m.get('content') or '' for m in service.rpc('get_chat', id=fact['cid'])['messages']
                       if m.get('role') == 'assistant']
            reported = any(HOOK_ISSUE in str(text) for text in answers)
            assert run.get('state') == 'verification_required', run.get('state')
            assert got.get('hook_issue') is True or reported, 'Neither a completion check nor the answer named the require_tests hook.'
            return {'run_state': run['state'], 'completion_repairs': got.get('completion_repairs', 0),
                    'repair_named_hook': bool(got.get('hook_issue')), 'answer_named_hook': reported}
        self.check('r3_require_tests_hook_keeps_completion_unsatisfied', blocked)

    # e1: managed dependency environments -----------------------------------------------------

    def scenario_e1(self, service):
        def pending():
            fact = self.start(service, 'e1', 'full')
            self.settle(service, fact['cid'], fact['rid'], 'e1')
            action = fact['action'] = self.one_pending(service, fact, 'install')
            payload = action['payload']
            assert payload.get('kind') == 'pip-hashes' and payload.get('requires_review') is True, payload.get('kind')
            assert payload.get('network') == 'allowlisted_registry_only' and payload.get('permission_mode') == 'full'
            got = self.seen('e1').get('results', {}).get('e1-pip') or {}
            assert got.get('status') == 'awaiting_user_approval', got
            return {'kind': 'pip-hashes', 'permission_mode': 'full'}
        self.check('e1_install_plan_stays_pending_for_native_review_in_full_mode', pending)

        def foreign():
            fact = self.need('e1')
            got = self.seen('e1').get('results', {}).get('e1-npm') or {}
            assert got.get('error') and 'another registry host' in got['error'] and not got.get('action_present'), got
            kinds = [a['payload'].get('kind') for a in self.actions(service, fact['cid'], 'install')]
            assert kinds == ['pip-hashes'], kinds
            return {'refusal': got['error'][:120]}
        self.check('e1_lockfile_with_a_foreign_registry_host_is_refused_at_proposal', foreign)

        def nothing():
            fact = self.need('e1')
            assert service.rpc('reject', id=fact['cid'], action_id=fact['action']['id']) is True
            items = service.rpc('environment_list', id=fact['cid'])['items']
            assert not [i for i in items if i.get('state') in ('installing', 'ready', 'uncertain')], [i.get('state') for i in items]
            self.resumed(service, fact, 'e1')
            return {'environments': len(items), 'approved': False}
        self.check('e1_rejected_install_never_runs', nothing)

    # w1: Auto-mode child worktree, end to end ---------------------------------------------------

    def scenario_w1(self, service):
        def allocated():
            fact = self.start(service, 'w1', 'auto', steps=40)
            root = fact['root']
            fact['before'] = self.repo_state(root, ('feature.txt', 'other.txt', 'scratch.txt'))
            fact['run'] = self.settle(service, fact['cid'], fact['rid'], 'w1', seconds=120)
            spawn = self.seen('w1').get('spawn') or {}
            assert spawn.get('agent_present') and spawn.get('worktree_status') == 'applied', spawn
            allocations = [a for a in self.actions(service, fact['cid'], 'worktree')
                           if a['payload'].get('operation') == 'allocate_child_worktree']
            assert [a['status'] for a in allocations] == ['applied'], [a['status'] for a in allocations]
            bindings = service.rpc('agent_worktrees', id=fact['cid'], run_id=fact['rid'])
            assert len(bindings) == 1, len(bindings)
            fact['binding'] = bindings[0]
            return {'allocation': 'applied', 'permission_mode': allocations[0]['payload'].get('permission_mode')}
        self.check('w1_worktree_allocation_applies_automatically_in_auto_mode', allocated)

        def ordered():
            got = self.seen('w1')
            read = got.get('child_read') or {}
            assert got.get('child_requests', 0) > 0, 'The worktree child never ran.'
            assert not read.get('error') and read.get('base_bytes') is True, read
            offered = got.get('child_offered') or []
            assert 'WriteWorktreeFile' in offered and 'Write' not in offered, offered
            return {'child_requests': got['child_requests'], 'child_offered': offered}
        self.check('w1_child_runs_only_on_its_active_worktree', ordered)

        def isolated():
            got = self.seen('w1')
            write = got.get('child_write') or {}
            assert not write.get('error') and write.get('after_matches') is True and write.get('project_changed') is False, write
            assert got.get('project_at_child_end') is True, 'The project file changed while the child wrote its worktree.'
            assert got.get('project_before_integration') is True, 'The project file changed before integration.'
            return {'project_untouched_by_child': True}
        self.check('w1_child_reads_and_writes_only_its_worktree', isolated)

        def accepted():
            fact = self.need('w1')
            nodes = service.rpc('agent_list', chat_id=fact['cid'], run_id=fact['rid'])['nodes']
            assert [(n['status'], n['review']) for n in nodes] == [('completed', 'accepted')], \
                [(n.get('status'), n.get('review')) for n in nodes]
            return {'review': 'accepted'}
        self.check('w1_root_accepts_the_completed_child_result', accepted)

        def integrated():
            fact = self.need('w1')
            got = self.seen('w1')
            preview, read, integrate = got.get('preview') or {}, got.get('export_read') or {}, got.get('integrate') or {}
            assert preview.get('paths') == ['feature.txt'] and preview.get('content_included') is False, preview
            assert not read.get('error') and read.get('content_matches') is True, read
            assert integrate.get('status') == 'applied' and integrate.get('action_present'), integrate
            assert (fact['root'] / 'feature.txt').read_text() == FEATURE_CHILD, 'The integrated bytes are not in the project.'
            edits = [a for a in self.actions(service, fact['cid'], 'edit') if a['payload'].get('path') == 'feature.txt']
            assert [a['status'] for a in edits] == ['applied'], [a['status'] for a in edits]
            return {'edit': 'applied', 'permission_mode': edits[0]['payload'].get('permission_mode')}
        self.check('w1_export_read_and_integrate_auto_apply_in_auto_mode', integrated)

        def retired():
            fact = self.need('w1')
            root, binding = fact['root'], fact['binding']
            assert self.seen('w1').get('retire_offered_to_model') is False, 'A model was offered worktree removal.'
            # Removal is native-only: the trusted route retires the fully integrated worktree.
            got = service.rpc('agent_worktree_retire', id=fact['cid'], run_id=fact['rid'], agent_id=binding['agent_id'])
            assert got.get('state') == 'retired' and got.get('integrated') == ['feature.txt'] and got.get('preserved') == [], got
            current = {b['worktree_id']: b for b in service.rpc('agent_worktrees', id=fact['cid'], run_id=fact['rid'])}
            assert current[binding['worktree_id']]['state'] == 'retired', current[binding['worktree_id']]['state']
            assert not os.path.lexists(binding['path']), 'The retired worktree folder still exists.'
            assert not os.path.lexists(root / '.git' / 'worktrees' / binding['worktree_id']), 'Git still lists the worktree.'
            after = self.repo_state(root, ('feature.txt', 'other.txt', 'scratch.txt'))
            before = fact['before']
            assert after['files']['other.txt'] == before['files']['other.txt'] == sha(OTHER_WORK), 'Dirty other.txt changed.'
            assert after['files']['scratch.txt'] == before['files']['scratch.txt'] == sha(SCRATCH), 'Untracked scratch.txt changed.'
            assert (after['head'], after['index'], after['refs']) == (before['head'], before['index'], before['refs'])
            return {'state': 'retired', 'folder_present': False}
        self.check('w1_retire_removes_the_worktree_and_keeps_other_dirty_work', retired)

    # w2: Ask-mode allocation, never dispatched, natively rejected ---------------------------------

    def scenario_w2(self, service):
        def pending():
            fact = self.start(service, 'w2', 'ask')

            def parked(run):
                return (run['state'] in ('waiting_input', 'waiting_approval') and self.seen('w2').get('finals')
                        and not service.rpc('state')['active'])
            self.settle(service, fact['cid'], fact['rid'], 'w2', accept=parked)
            action = fact['action'] = self.one_pending(service, fact, 'worktree')
            assert action['payload'].get('operation') == 'allocate_child_worktree'
            bindings = service.rpc('agent_worktrees', id=fact['cid'], run_id=fact['rid'])
            assert [b['state'] for b in bindings] == ['proposed'], [b['state'] for b in bindings]
            fact['binding'] = bindings[0]
            time.sleep(1.5)  # the agent dispatcher polls every quarter second
            nodes = service.rpc('agent_list', chat_id=fact['cid'], run_id=fact['rid'])['nodes']
            assert [n['status'] for n in nodes] == ['queued'], [n.get('status') for n in nodes]
            assert not self.seen('w2').get('child_requests'), 'The child was dispatched before its worktree existed.'
            assert not os.path.lexists(bindings[0]['path']), 'A worktree folder exists before native review.'
            spawn = self.seen('w2').get('spawn') or {}
            assert spawn.get('worktree_status') == 'awaiting_user_approval', spawn
            return {'allocation': 'pending', 'child': 'queued', 'child_requests': 0}
        self.check('w2_ask_allocation_stays_pending_and_the_child_is_not_dispatched', pending)

        def stopped():
            fact = self.need('w2')
            assert service.rpc('reject', id=fact['cid'], action_id=fact['action']['id']) is True
            run = self.settle(service, fact['cid'], fact['rid'], 'w2')
            nodes = service.rpc('agent_list', chat_id=fact['cid'], run_id=fact['rid'])['nodes']
            assert [n['status'] for n in nodes] and all(n['status'] in NODE_TERMINAL - {'completed'} for n in nodes), \
                [n.get('status') for n in nodes]
            bindings = service.rpc('agent_worktrees', id=fact['cid'], run_id=fact['rid'])
            assert [b['state'] for b in bindings] == ['rejected'], [b['state'] for b in bindings]
            assert not os.path.lexists(fact['binding']['path']), 'A worktree folder exists after native rejection.'
            assert not self.seen('w2').get('child_requests'), 'A rejected child reached the provider.'
            return {'child': [n['status'] for n in nodes], 'run_state': run['state']}
        self.check('w2_native_reject_stops_the_child_without_a_worktree', stopped)

    # w3: Ask-mode worktree child, approved, tested on its worktree, integrated -------------------------

    def scenario_w3(self, service):
        files = ('feature.txt', 'test_sample.py')

        def parked_for(kind):
            """Settle predicate: the root parked in waiting_approval with one pending `kind` action."""
            def accept(run):
                if run['state'] in ENDED:
                    raise AssertionError('The w3 run ended before parking for native review: ' + run['state'])
                return (run['state'] == 'waiting_approval' and not service.rpc('state')['active']
                        and bool(self.actions(service, self.facts['w3']['cid'], kind, 'pending')))
            return accept

        def parked():
            root = self.need('root:w3')
            self.require_tests(service, True)  # enabled before the run starts, so the run pins it
            fact = self.start(service, 'w3', 'ask', steps=48)
            fact['before'] = self.repo_state(root, files)
            run = self.settle(service, fact['cid'], fact['rid'], 'w3', accept=parked_for('worktree'))
            action = fact['allocation'] = self.one_pending(service, fact, 'worktree')
            assert action['payload'].get('operation') == 'allocate_child_worktree'
            bindings = service.rpc('agent_worktrees', id=fact['cid'], run_id=fact['rid'])
            assert [b['state'] for b in bindings] == ['proposed'], [b['state'] for b in bindings]
            fact['binding'] = bindings[0]
            time.sleep(1.5)  # the agent dispatcher polls every quarter second
            nodes = service.rpc('agent_list', chat_id=fact['cid'], run_id=fact['rid'])['nodes']
            assert [n['status'] for n in nodes] == ['queued'], [n.get('status') for n in nodes]
            assert not self.seen('w3').get('child_requests'), 'The child was dispatched before its worktree existed.'
            assert not os.path.lexists(bindings[0]['path']), 'A worktree folder exists before native review.'
            spawn = self.seen('w3').get('spawn') or {}
            assert spawn.get('status') == 'awaiting_user_approval' and spawn.get('action_present'), spawn
            assert service.rpc('run_get', id=fact['cid'], run_id=fact['rid'])['state'] == 'waiting_approval'
            return {'run_state': run['state'], 'allocation': 'pending', 'child': 'queued', 'child_requests': 0}

        def allocated():
            fact = self.need('w3')
            binding = self.need('w3')['binding']
            receipt = service.rpc('approve', id=fact['cid'], action_id=fact['allocation']['id'])
            assert receipt.get('state') == 'active' and receipt.get('worktree_id') == binding['worktree_id'], \
                (receipt.get('state'), receipt.get('error'))
            current = {b['worktree_id']: b for b in service.rpc('agent_worktrees', id=fact['cid'], run_id=fact['rid'])}
            assert current[binding['worktree_id']]['state'] == 'active', current[binding['worktree_id']]['state']
            assert Path(binding['path']).is_dir() and not Path(binding['path']).is_symlink(), 'The worktree folder is missing.'
            status = [a['status'] for a in self.actions(service, fact['cid'], 'worktree')]
            assert status == ['applied'], status
            self.facts['w3_allocated'] = True
            return {'state': 'active', 'folder_present': True}

        def dispatched():
            self.need('w3_allocated')
            self.until(lambda: self.seen('w3').get('child_request'), 45, 'the w3 child to request worktree tests')
            got = self.seen('w3')
            read, write = got.get('child_read') or {}, got.get('child_write') or {}
            assert not read.get('error') and read.get('base_bytes') is True, read
            assert (not write.get('error') and write.get('after_matches') is True and write.get('project_changed') is False
                    and write.get('project_unchanged') is True), write
            offered = got.get('child_offered') or []
            assert {'WriteWorktreeFile', 'RequestAgentEffect'} <= set(offered) and 'Write' not in offered, offered
            return {'child_requests': got['child_requests'], 'child_offered': offered}

        def requested():
            fact = self.need('w3')
            self.need('w3_allocated')
            got = self.seen('w3').get('child_request') or {}
            assert (not got.get('error') and got.get('status') == 'proposed' and got.get('authorization') == 'none'
                    and got.get('request_present')), got
            node = service.rpc('agent_list', chat_id=fact['cid'], run_id=fact['rid'])['nodes'][0]
            effects = service.rpc('agent_get', chat_id=fact['cid'], run_id=fact['rid'], agent_id=node['id'])['effects']
            assert [e['tool'] for e in effects] == ['RunTests'], [e.get('tool') for e in effects]
            self.facts['w3_requested'] = True
            return {'status': 'proposed', 'authorization': 'none'}

        def pending_tests():
            fact, root = self.need('w3'), self.need('root:w3')
            self.need('w3_requested')
            self.settle(service, fact['cid'], fact['rid'], 'w3', accept=parked_for('tests'))
            action = fact['tests'] = self.one_pending(service, fact, 'tests')
            payload, path = action['payload'], fact['binding']['path']
            info = os.stat(path)
            assert payload.get('root') == path != str(root), ('The tests action is not rooted at the worktree.', payload.get('root') == str(root))
            assert payload.get('root_identity') == [info.st_dev, info.st_ino], 'The tests action does not pin the worktree identity.'
            assert payload.get('recipe') == 'python-unittest', payload.get('recipe')
            view = self.seen('w3').get('request_view') or {}
            assert view.get('arguments') == WORKTREE_TESTS and view.get('executable') is True, view
            ran = self.seen('w3').get('execute') or {}
            assert ran.get('status') == 'awaiting_user_approval' and ran.get('action_present'), ran
            return {'root': 'worktree', 'recipe': 'python-unittest', 'permission_mode': payload.get('permission_mode')}

        def passed():
            fact, root = self.need('w3'), self.need('root:w3')
            action = fact['tests']
            try:
                outcome = service.rpc('approve', id=fact['cid'], action_id=action['id'])
            finally:
                self.fixture.events['w3'].set()  # the child may now read its outcome and finish
            tests = outcome.get('tests') or {}
            assert outcome.get('outcome') == 'passed' and outcome.get('exit_code') == 0, (outcome.get('outcome'), outcome.get('exit_code'))
            assert type(tests.get('total')) is int and tests['total'] > 0 and tests.get('passed') == tests['total'], tests
            assert self.repo_state(root, files) == fact['before'], 'The project changed while its worktree was tested.'
            self.facts['w3_passed'] = True
            return {'outcome': 'passed', 'total': tests['total'], 'project_untouched': True}

        def integrated():
            fact, root = self.need('w3'), self.need('root:w3')
            self.need('w3_passed')
            self.settle(service, fact['cid'], fact['rid'], 'w3', seconds=90, accept=parked_for('edit'))
            edit = self.one_pending(service, fact, 'edit')
            got = self.seen('w3')
            assert (got.get('review') or {}).get('review') == 'accepted', got.get('review')
            assert (got.get('child_outcome') or {}).get('status') == 'applied', got.get('child_outcome')
            assert (got.get('export_read') or {}).get('content_matches') is True, got.get('export_read')
            assert edit['payload'].get('path') == 'feature.txt' and edit['payload'].get('root') == str(root)
            assert (root / 'feature.txt').read_text() == FEATURE_HEAD, 'The project changed before native review of the edit.'
            receipt = service.rpc('approve', id=fact['cid'], action_id=edit['id'])
            assert (root / 'feature.txt').read_text() == FEATURE_CHILD, ('The integrated bytes are not in the project.', receipt.get('applied'))
            self.facts['w3_integrated'] = True
            return {'edit': 'applied', 'child_outcome': 'applied'}

        def gate():
            fact, root = self.need('w3'), self.need('root:w3')
            self.need('w3_integrated')
            run = self.resumed(service, fact, 'w3', seconds=90)
            got = self.seen('w3')
            answers = [m.get('content') or '' for m in service.rpc('get_chat', id=fact['cid'])['messages']
                       if m.get('role') == 'assistant']
            reported = any(HOOK_ISSUE in str(text) for text in answers)
            tests = self.actions(service, fact['cid'], 'tests', 'applied')
            assert [(t['payload'].get('root'), (t['payload'].get('result') or {}).get('outcome')) for t in tests] == \
                [(fact['binding']['path'], 'passed')], 'The only test evidence must be the passing worktree run.'
            assert run['state'] == 'verification_required', run['state']
            assert got.get('hook_issue') is True or reported, 'Neither a completion check nor the answer named the require_tests hook.'
            return {'run_state': run['state'], 'completion_repairs': got.get('completion_repairs', 0),
                    'repair_named_hook': bool(got.get('hook_issue')), 'answer_named_hook': reported}

        try:
            for name, fn in (('w3_ask_allocation_pending_child_undispatched_and_root_parked', parked),
                             ('w3_native_approval_allocates_an_active_worktree_folder', allocated),
                             ('w3_dispatched_child_reads_and_writes_only_its_worktree', dispatched),
                             ('w3_child_requests_worktree_tests_as_a_data_only_proposal', requested),
                             ('w3_root_executes_the_request_and_worktree_tests_wait_for_native_review', pending_tests),
                             ('w3_approved_tests_run_on_the_worktree_pass_and_leave_the_project_untouched', passed),
                             ('w3_child_file_is_integrated_only_after_native_review', integrated),
                             ('w3_worktree_test_never_satisfies_the_project_require_tests_gate', gate)):
                self.check(name, fn)
        finally:
            self.fixture.events['w3'].set()
            try:
                self.require_tests(service, False)
            except Exception as exc:
                self.observations.setdefault('hook_disable_failures', []).append(bounded(exc, 300))

    # x1: native-only coding routes ----------------------------------------------------------------

    def scenario_x1(self, service):
        def bridge():
            rust = (ROOT / 'src-tauri/src/main.rs').read_text()
            allowed = re.search(r'const ALLOWED: &\[&str\] = &\[(.*?)\];', rust)
            pet = re.search(r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains\(&method\.as_str\(\)\)', rust)
            assert allowed and pet and 'Generated views cannot invoke native agent controls.' in rust
            assert 'if !["main","pet"].contains(&window.label())' in rust, 'Generated-view windows are not refused first.'
            for method in ('git_plan_propose', 'agent_worktree_retire'):
                assert '"' + method + '"' in allowed.group(1), method + ' is not a reviewed main-window route.'
                assert '"' + method + '"' not in pet.group(1), method + ' is reachable from the Pet window.'
            return {'main_window_only': ['agent_worktree_retire', 'git_plan_propose']}
        self.check('x1_native_bridge_keeps_coding_effect_routes_main_window_only', bridge)

        def model():
            root = self.need('root:x1')
            head = self.repo_state(root, ('a.txt',))
            fact = self.start(service, 'x1', 'ask')
            self.settle(service, fact['cid'], fact['rid'], 'x1')
            got = self.seen('x1').get('results', {})
            for ident in ('x1-propose', 'x1-retire'):
                item = got.get(ident) or {}
                assert item.get('error') and not item.get('action_present'), (ident, item)
            assert self.actions(service, fact['cid']) == [], 'A model call to a native route staged an action.'
            assert self.receipts(service, fact['cid']) == [], 'A model call to a native route made a Git plan.'
            assert service.rpc('agent_worktrees', id=fact['cid']) == []
            assert self.repo_state(root, ('a.txt',)) == head, 'The repository changed.'
            return {'refused_calls': 2}
        self.check('x1_model_calls_to_native_coding_routes_are_refused', model)

    # k1: hard crash with a pending Git plan --------------------------------------------------------

    def scenario_k1(self, service):
        def pending():
            root = self.need('root:k1')
            fact = self.start(service, 'k1', 'ask')
            self.settle(service, fact['cid'], fact['rid'], 'k1')
            action = fact['action'] = self.one_pending(service, fact, 'git')
            fact['state'] = self.repo_state(root, ('a.txt',))
            fact['receipts'] = len(self.receipts(service, fact['cid']))
            assert fact['receipts'] == 0
            return {'pending': action['payload'].get('operation')}
        if not self.check('k1_git_plan_pending_before_the_hard_crash', pending):
            return False
        self.crash(service)
        self.facts['k1_records'] = self.fixture.attempts('k1')
        return True

    def forge(self):
        """While the service is stopped, insert an `executing` receipt as a crash mid-effect would leave it."""
        fact = self.need('k1')
        payload = fact['action']['payload']
        ident = uuid.uuid4().hex
        plan = {**{key: value for key, value in payload.items() if key != 'permission_mode'}, 'plan_id': ident}
        receipt = {'receipt_id': ident, 'operation': plan['operation'], 'plan_hash': plan['plan_hash'], 'state': 'executing',
                   'phase': 'registered before any Git effect', 'effect_started': False, 'plan': plan, 'authorization': 'none'}
        now = time.time()
        with contextlib.closing(sqlite3.connect(str(self.need('home') / 'pebble.sqlite'), timeout=10)) as db:
            with db:
                db.execute('INSERT INTO git_plan_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (ident, fact['cid'], fact['rid'], None, plan['operation'], plan['plan_hash'], 'executing',
                            canonical(receipt), now, now))
        fact['forged'] = ident
        return {'state': 'executing'}

    def phase_b(self, service):
        def survives():
            fact, root = self.need('k1'), self.need('root:k1')
            action = next(a for a in self.actions(service, fact['cid']) if a['id'] == fact['action']['id'])
            run = service.rpc('run_get', id=fact['cid'], run_id=fact['rid'])
            assert action['status'] == 'pending', action['status']
            assert run['state'] == 'waiting_approval', run['state']
            assert self.repo_state(root, ('a.txt',)) == fact['state'], 'The repository changed across the crash.'
            ids = {r['receipt_id'] for r in self.receipts(service, fact['cid'])}
            assert fact['action']['payload']['plan_id'] not in ids, 'The pending plan has a receipt.'
            return {'action': 'pending', 'run_state': run['state']}
        self.check('k1_pending_action_survives_restart_and_nothing_executed', survives)

        def uncertain():
            fact = self.need('k1')
            forged = self.need('k1').get('forged') or ''
            items = {r['receipt_id']: r for r in self.receipts(service, fact['cid'])}
            row = items.get(forged) or {}
            assert row.get('state') == 'uncertain' and row.get('effect_started') is False, (row.get('state'), row.get('effect_started'))
            assert row.get('note'), 'Recovery left no note on the interrupted receipt.'
            return {'state': 'uncertain'}
        self.check('k1_forged_executing_receipt_becomes_uncertain_at_restart', uncertain)

        def never():
            fact, root = self.need('k1'), self.need('root:k1')
            forged = fact.get('forged') or ''
            time.sleep(6)  # the continuation dispatcher wakes at least every five seconds
            items = self.receipts(service, fact['cid'])
            assert [(r['receipt_id'], r['state']) for r in items] == [(forged, 'uncertain')], [(r['receipt_id'][:8], r['state']) for r in items]
            assert self.repo_state(root, ('a.txt',)) == fact['state'], 'Recovery retried a Git effect.'
            assert self.fixture.attempts('k1') == self.need('k1_records'), 'Restart dispatched a model attempt.'
            return {'receipts': 1, 'provider_attempts': self.facts['k1_records']}
        self.check('k1_uncertain_receipt_is_never_retried', never)

        def once():
            fact, root = self.need('k1'), self.need('root:k1')
            receipt = service.rpc('approve', id=fact['cid'], action_id=fact['action']['id'])
            assert receipt.get('state') == 'applied', (receipt.get('state'), receipt.get('error'))
            head = self.repo_state(root)['head']
            assert self.git(root, 'rev-parse', 'HEAD^').decode().strip() == fact['state']['head'], 'More than one commit landed.'
            assert self.changed_paths(root, head) == ['a.txt']
            self.resumed(service, fact, 'k1')
            states = sorted(r['state'] for r in self.receipts(service, fact['cid']))
            assert states == ['applied', 'uncertain'], states
            return {'receipts': states}
        self.check('k1_pending_plan_applies_exactly_once_after_restart', once)

        def offered():
            with self.fixture.lock:
                records = [dict(r) for r in self.fixture.records]
            assert records, 'No provider request was recorded.'
            leaked = sorted({name for r in records for name in r['tools'] if name in NATIVE_ONLY})
            assert not leaked, ('A provider request offered native-only routes.', leaked)
            children = [r for r in records if r['role'] == 'child']
            assert children, 'No child request was recorded, so child tool scope is unproven.'
            root_only = sorted({name for r in children for name in r['tools'] if name in ROOT_ONLY})
            assert not root_only, ('A child was offered root-only coding tools.', root_only)
            return {'requests': len(records), 'child_requests': len(children)}
        self.check('x1_no_model_or_child_request_offers_native_or_root_only_coding_tools', offered)

        def clean():
            with self.fixture.lock:
                assert not self.fixture.errors, self.fixture.errors[-5:]
                assert not self.fixture.anomalies, self.fixture.anomalies[-5:]
            return {'provider_attempts': len(self.fixture.records)}
        self.check('synthetic_provider_protocol_clean', clean)


def owned_backend(parser, value):
    given = Path(value)
    try:
        info = os.lstat(given)
    except OSError as exc:
        parser.error('--backend is not readable: ' + bounded(exc))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        parser.error('--backend must be a regular file, not a symbolic link.')
    if hasattr(os, 'getuid') and info.st_uid != os.getuid():
        parser.error('--backend must be owned by the current user.')
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        parser.error('--backend must not be group or world writable.')
    if not os.access(given, os.X_OK):
        parser.error('--backend must be executable.')
    return given.resolve()


def checkout_digests():
    """Digests of the source backend, the native bridge this campaign inspects and this validator."""
    paths = ['backend/main.py', *[p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'src-tauri/src/main.rs', 'scripts/validate_phase13.py', 'scripts/validate_runtime.py']
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
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary home/projects even on success')
    args = parser.parse_args()
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; move it aside or choose a new path (earlier raw evidence, '
                     'including a failed run, is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    command = [str(backend)] if backend else [sys.executable, str(ROOT / 'backend/main.py')]
    backend_before = sha(backend.read_bytes()) if backend else None
    sources = checkout_digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase13-')).resolve()
    started = time.monotonic()
    campaign = Campaign(command, backend)
    try:
        campaign.run(scratch)
    finally:
        duration = round(time.monotonic() - started, 1)  # the campaign itself, before cleanup and reporting
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
    passed = bool(results) and campaign.fatal is None and all(c['passed'] for c in results)
    keep = args.keep_scratch or not passed
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    with campaign.fixture.lock:
        records = [dict(r) for r in campaign.fixture.records]
        errors, anomalies = list(campaign.fixture.errors), list(campaign.fixture.anomalies)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source',
        'passed': passed,
        # The parent verifier contract: unique names of checks that actually passed.
        'checks': [c['name'] for c in results if c['passed']],
        'failed': [c['name'] for c in results if not c['passed']],
        'check_results': results, 'fatal_error': campaign.fatal,
        # Recorded facts, not pass/fail claims.
        'observations': campaign.observations,
        'duration_seconds': duration,
        'duration_budget_seconds': VERIFIER_SECONDS,
        'synthetic_provider_attempts': len(records), 'request_metadata': records,
        'provider_errors': errors, 'provider_anomalies': anomalies,
        'backend_sha256': backend_before,
        'backend_path': str(backend) if backend else None,
        'source_sha256': sources,
        'source_note': ('Digests of this checkout. In frozen mode they do not prove the executable was built from '
                        'these files; the candidate manifest is bound separately.'),
        'scratch_preserved': str(scratch) if keep else None,
        'backend_stderr_tail': campaign.stderr_tails if not passed else [],
        'limits': ('Owned loopback synthetic provider, temporary home and owned projects outside .runtime, system Git '
                   'only for fixture setup and observation, actual service JSON-RPC, native approval, the actual '
                   'vendored AgentGuard, actual macOS confinement of Git, tests and child worktrees, and a local bare '
                   'remote on this machine. No live model, network remote, dependency install, account, credential, '
                   'billing, installer, signed/installed desktop, other-platform or frontier evidence. Fixture '
                   'evidence, not an independent human security audit.'),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, 'x', encoding='utf-8') as out:
        out.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']),
                      'failed': report['failed'], 'fatal_error': campaign.fatal, 'duration_seconds': duration,
                      'scenario_seconds': campaign.observations.get('scenario_seconds'),
                      'scratch_preserved': report['scratch_preserved']}, indent=2))
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()


# Development self-checks, run only by pytest on this file; the campaign never
# calls them. They build the fixture projects under pytest's tmp_path and drive
# the scripted turns with synthetic host results. No service is started.

def _backend_path():
    path = str(ROOT / 'backend')
    if path not in sys.path:
        sys.path.insert(0, path)


def _closed_fixture():
    fixture = Fixture()
    fixture.server.server_close()
    return fixture


def _turn(fixture, messages, names=()):
    body = {'model': MODEL, 'messages': messages, 'tools': [{'function': {'name': name}} for name in names]}
    return fixture.respond(body).delta


def _answer(messages, delta, value):
    messages.append({'role': 'assistant', **delta})
    for call in delta.get('tool_calls', []):
        messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(value)})


def test_fixture_projects_match_backend_admission_rules(tmp_path):
    _backend_path()
    from pebble.environments import PUBLIC, check_npm, check_requirements
    from pebble.git_plans import REMOTE_CONFIG, GitPlans, destination
    from pebble.git_workflows import CONFIG
    from pebble.policy import PolicyError
    from pebble.recipes import _detect_pytest, _detect_unittest
    scratch = tmp_path.resolve()
    git = GitFixture(scratch)
    built = build_projects(scratch, git)
    roots = built['roots']
    assert sorted(roots) == sorted(set(PLANS) | {'w1', 'w2', 'w3'}) and all(r == r.resolve() for r in roots.values())
    inner = roots['n1'] / 'vendor' / 'inner'
    assert (inner / '.git').is_dir() and (inner / 'app.py').read_text() == APP_WORK
    assert git(roots['n1'], 'ls-files', '--others', '--exclude-standard').decode().split() == ['vendor/inner/']
    try:
        GitPlans._embedded({'repo': {'root': roots['n1']}}, ['vendor/inner/app.py'])
    except PolicyError as exc:
        assert 'nested Git repository' in str(exc)
    else:
        raise AssertionError('The nested repository path was accepted.')
    GitPlans._embedded({'repo': {'root': roots['n1']}}, ['a.txt'])  # the outer repository's own file passes
    w3 = roots['w3']
    assert git(w3, 'status', '--porcelain', '--untracked-files=all') == b'', 'The w3 project must be clean.'
    assert _detect_unittest({'files': ('feature.txt', 'test_sample.py')})
    for tag in GIT_PROJECTS:
        entries = git(roots[tag], 'config', '--file', '.git/config', '--null', '--list').split(b'\0')
        keys = [entry.partition(b'\n')[0].decode() for entry in entries if entry]
        assert keys and all(CONFIG.fullmatch(key) for key in keys), (tag, keys)
        assert not (roots[tag] / '.git' / 'info').exists() and not (roots[tag] / '.gitmodules').exists()
    for name, remote in built['remotes'].items():
        entries = git(remote.parent, 'config', '--file', str(remote / 'config'), '--null', '--list').split(b'\0')
        keys = [entry.partition(b'\n')[0].decode() for entry in entries if entry]
        assert keys and all(REMOTE_CONFIG.fullmatch(key) for key in keys), (name, keys)
        executable = bool((remote / 'hooks' / 'pre-receive').stat().st_mode & 0o111)
        assert executable is (name == 'hooked')
    try:
        destination(WEB_REMOTE)
    except PolicyError as exc:
        assert 'transport_unsupported' in str(exc)
    else:
        raise AssertionError('The https remote was accepted.')
    g1 = roots['g1']
    assert staged_names(git, g1) == ['c.txt']
    assert (g1 / 'a.txt').read_text() == A_WORK and (g1 / 'b.txt').read_text() == B_WORK
    hooks = sorted(p.name for p in (g1 / '.git' / 'hooks').iterdir() if p.stat().st_mode & 0o111)
    assert hooks == ['post-commit', 'pre-commit'] and not any(built['markers'].iterdir())
    assert git(roots['w1'], 'ls-files', '--others', '--exclude-standard').decode().split() == ['scratch.txt']
    try:
        check_npm(PACKAGE_JSON.encode(), PACKAGE_LOCK.encode(), PUBLIC['npm-ci'])
    except PolicyError as exc:
        assert 'another registry host' in str(exc) and 'leftpad' in str(exc)
    else:
        raise AssertionError('The foreign lockfile host was accepted.')
    assert check_requirements(REQUIREMENTS.encode()) == 1
    for tag, files in (('r1', ['pytest.ini', 'test_sample.py']), ('r2', ['pytest.ini', 'test_sample.py']), ('r3', ['notes.txt', 'pytest.ini'])):
        facts = {'files': tuple(sorted(p.name for p in roots[tag].iterdir())), 'texts': {}, 'present': {'pytest.ini'}}
        assert list(facts['files']) == files and _detect_pytest(facts)
        assert bool(_detect_unittest(facts)) is (tag != 'r3')


def test_plan_scripts_emit_each_call_once_then_answer_repairs():
    fixture = _closed_fixture()
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': GOALS['g3']}]
    first = _turn(fixture, messages, ['ProposeGitChange', 'Read'])
    assert [c['id'] for c in first['tool_calls']] == ['g3-commit']
    assert json.loads(first['tool_calls'][0]['function']['arguments'])['paths'] == '["a.txt"]'
    _answer(messages, first, {'status': 'applied', 'action_id': 'a1', 'kind': 'git', 'result': {'state': 'applied'}})
    second = _turn(fixture, messages)
    assert [c['id'] for c in second['tool_calls']] == ['g3-discard']
    _answer(messages, second, {'status': 'awaiting_user_approval', 'action_id': 'a2', 'kind': 'git'})
    third = _turn(fixture, messages)
    assert third == {'content': DONE}
    _answer(messages, third, None)
    messages.append({'role': 'user', 'content': 'HOST RESUME: Continue committed results.'})
    assert _turn(fixture, messages) == {'content': DONE}
    messages.append({'role': 'assistant', 'content': DONE})
    messages.append({'role': 'user', 'content': COMPLETION + ': The ' + HOOK_ISSUE + ' that covers the current project files.'})
    assert _turn(fixture, messages) == {'content': DONE}
    seen = fixture.state('g3')
    assert seen['offered'] == ['ProposeGitChange', 'Read'] and seen['completion_repairs'] == 1 and seen['hook_issue']
    assert seen['results']['g3-commit']['result_state'] == 'applied' and seen['results']['g3-discard']['action_present']
    assert not fixture.anomalies and [r['step'] for r in fixture.records] == [0, 1, 2, 3, 4]


def test_unscripted_and_private_requests_are_flagged():
    fixture = _closed_fixture()
    assert _turn(fixture, [{'role': 'user', 'content': 'No marker here.'}]) == {'content': 'Synthetic fixture: no script.'}
    assert fixture.anomalies and fixture.anomalies[0]['reason'] == 'unscripted conversation'
    for body in ({'model': 'other', 'messages': []}, {'model': MODEL, 'messages': [{'role': 'user', '_pebble_x': 1}]}):
        try:
            fixture.respond(body)
        except AssertionError:
            continue
        raise AssertionError('A malformed provider request was accepted.')


def test_worktree_root_and_child_scripts_follow_the_reviewed_flow(tmp_path):
    fixture = _closed_fixture()
    (tmp_path / 'feature.txt').write_text(FEATURE_HEAD)
    fixture.shared['w1_root'] = str(tmp_path)
    agent, worktree = 'c' * 32, 'd' * 32
    child = [{'role': 'system', 'content': 'child'},
             {'role': 'user', 'content': ASSIGNMENT + json.dumps({'id': agent, 'task': CHILD_TASKS['w1'], 'tools': ['Read']})}]
    read = _turn(fixture, child, ['Read', 'WriteWorktreeFile'])
    assert read['tool_calls'][0]['function']['name'] == 'Read'
    _answer(child, read, {'path': 'feature.txt', 'file_hash': sha(FEATURE_HEAD)})
    write = _turn(fixture, child)
    args = json.loads(write['tool_calls'][0]['function']['arguments'])
    assert write['tool_calls'][0]['function']['name'] == 'WriteWorktreeFile' and args['expected_hash'] == sha(FEATURE_HEAD)
    _answer(child, write, {'after_hash': sha(FEATURE_CHILD), 'project_changed': False})
    assert 'content' in _turn(fixture, child)
    root = [{'role': 'system', 'content': 'root'}, {'role': 'user', 'content': GOALS['w1']}]
    script = [({'agent_id': agent, 'id': agent, 'generation': 1, 'worktree': {'worktree_id': worktree, 'status': 'applied', 'action_id': 'w'}}, 'SpawnWorktreeAgent'),
              ({'status': 'awaiting_agent_results', 'required': True}, 'WaitForAgents'),
              ({'nodes': [{'id': agent, 'status': 'running', 'review': 'pending', 'generation': 1}]}, 'ReadAgentTree'),
              ({'status': 'settled'}, 'WaitForAgents'),
              ({'nodes': [{'id': agent, 'status': 'completed', 'review': 'pending', 'generation': 1, 'evidence_ids': ['1:cw-read']}]}, 'ReadAgentTree'),
              ({'review': 'accepted'}, 'ReviewAgentResult'),
              ({'export_id': 'x1', 'proposals': [{'path': 'feature.txt'}], 'content_included': False}, 'PreviewWorktreeExport'),
              ({'untrusted_content': FEATURE_CHILD}, 'ReadWorktreeExport'),
              ({'status': 'applied', 'action_id': 'e', 'kind': 'edit'}, 'IntegrateWorktreeFile')]
    emitted = []
    for value, expected in script:
        delta = _turn(fixture, root, ['SpawnWorktreeAgent'])
        emitted.append(delta['tool_calls'][0]['function']['name'])
        assert emitted[-1] == expected, emitted
        _answer(root, delta, value)
    assert _turn(fixture, root) == {'content': DONE}
    seen = fixture.state('w1')
    assert seen['child_read']['base_bytes'] and seen['child_write']['after_matches'] and seen['project_at_child_end']
    assert seen['project_before_integration'] and seen['retire_offered_to_model'] is False
    assert seen['spawn']['worktree_status'] == 'applied' and seen['preview']['paths'] == ['feature.txt']
    assert 'WriteWorktreeFile' in seen['child_offered'] and not fixture.anomalies


def test_pending_worktree_script_parks_then_dismisses_the_stopped_child():
    fixture = _closed_fixture()
    agent = 'c' * 32
    messages = [{'role': 'system', 'content': 'root'}, {'role': 'user', 'content': GOALS['w2']}]
    spawn = _turn(fixture, messages, ['SpawnWorktreeAgent'])
    assert spawn['tool_calls'][0]['function']['name'] == 'SpawnWorktreeAgent'
    assert 'workspace' not in json.loads(spawn['tool_calls'][0]['function']['arguments'])
    _answer(messages, spawn, {'agent_id': agent, 'id': agent, 'generation': 1,
                              'worktree': {'worktree_id': 'd' * 32, 'status': 'awaiting_user_approval', 'action_id': 'w'}})
    parked = _turn(fixture, messages)
    assert 'content' in parked and fixture.state('w2')['finals'] == 1
    _answer(messages, parked, None)
    tree = _turn(fixture, messages)
    assert tree['tool_calls'][0]['function']['name'] == 'ReadAgentTree'
    _answer(messages, tree, {'nodes': [{'id': agent, 'status': 'stopped', 'review': 'pending', 'generation': 1}]})
    dismiss = _turn(fixture, messages)
    args = json.loads(dismiss['tool_calls'][0]['function']['arguments'])
    assert dismiss['tool_calls'][0]['function']['name'] == 'ReviewAgentResult' and args['decision'] == 'dismiss'
    _answer(messages, dismiss, {'review': 'dismissed'})
    assert 'content' in _turn(fixture, messages)
    assert fixture.state('w2')['spawn']['worktree_status'] == 'awaiting_user_approval' and not fixture.anomalies


def test_tested_worktree_child_holds_its_request_until_native_approval_releases_it(tmp_path):
    fixture = _closed_fixture()
    (tmp_path / 'feature.txt').write_text(FEATURE_HEAD)
    fixture.shared['w3_root'] = str(tmp_path)
    child = [{'role': 'system', 'content': 'child'},
             {'role': 'user', 'content': ASSIGNMENT + json.dumps({'id': 'c' * 32, 'task': CHILD_TASKS['w3'], 'tools': ['Read']})}]
    names = ['Read', 'WriteWorktreeFile', 'EditWorktreeFile', 'RequestAgentEffect', 'ReadAgentEffect']
    read = _turn(fixture, child, names)
    assert read['tool_calls'][0]['function']['name'] == 'Read'
    _answer(child, read, {'path': 'feature.txt', 'file_hash': sha(FEATURE_HEAD)})
    write = _turn(fixture, child)
    assert json.loads(write['tool_calls'][0]['function']['arguments'])['expected_hash'] == sha(FEATURE_HEAD)
    _answer(child, write, {'after_hash': sha(FEATURE_CHILD), 'project_changed': False})
    request = _turn(fixture, child)
    args = json.loads(request['tool_calls'][0]['function']['arguments'])
    assert request['tool_calls'][0]['function']['name'] == 'RequestAgentEffect' and args['tool'] == 'RunTests'
    assert json.loads(args['arguments']) == WORKTREE_TESTS
    _answer(child, request, {'request_id': 'r' * 32, 'status': 'proposed', 'authorization': 'none'})
    held = {}
    thread = threading.Thread(target=lambda: held.update(delta=_turn(fixture, child)))
    thread.start()
    thread.join(0.3)
    assert thread.is_alive() and 'delta' not in held, 'The child answered before native approval released it.'
    fixture.events['w3'].set()
    thread.join(5)
    assert held['delta']['tool_calls'][0]['function']['name'] == 'ReadAgentEffect'
    _answer(child, held['delta'], {'status': 'applied', 'evidence': 'data_only', 'authorization': 'none'})
    assert 'content' in _turn(fixture, child)
    seen = fixture.state('w3')
    assert seen['child_released'] is True and seen['child_request']['status'] == 'proposed'
    assert seen['child_write']['project_unchanged'] and seen['child_outcome']['status'] == 'applied'
    assert set(names) <= set(seen['child_offered']) and not fixture.anomalies


def test_tested_worktree_root_parks_for_each_native_review():
    fixture = _closed_fixture()
    agent, worktree = 'c' * 32, 'd' * 32
    root = [{'role': 'system', 'content': 'root'}, {'role': 'user', 'content': GOALS['w3']}]
    resume = {'role': 'user', 'content': 'HOST RESUME: Continue committed results.'}
    steps = [('SpawnWorktreeAgent', {'agent_id': agent, 'id': agent, 'generation': 1, 'status': 'awaiting_user_approval',
                                     'action_id': 'w', 'worktree': {'worktree_id': worktree, 'status': 'awaiting_user_approval', 'action_id': 'w'}}),
             ('park', None),
             ('WaitForAgents', {'status': 'awaiting_agent_results', 'required': True}),
             ('ReadAgentEffects', {'requests': [{'request_id': 'q' * 32, 'tool': 'RunTests', 'status': 'proposed', 'executable': True,
                                                 'untrusted_arguments': WORKTREE_TESTS}]}),
             ('ExecuteAgentEffect', {'status': 'awaiting_user_approval', 'action_id': 't', 'kind': 'tests', 'effect_status': 'awaiting_approval'}),
             ('park', None),
             ('ReadAgentTree', {'nodes': [{'id': agent, 'status': 'running', 'review': 'pending', 'generation': 1}]}),
             ('WaitForAgents', {'status': 'settled'}),
             ('ReadAgentTree', {'nodes': [{'id': agent, 'status': 'completed', 'review': 'pending', 'generation': 1,
                                           'evidence_ids': ['1:cw3-read']}]}),
             ('ReviewAgentResult', {'review': 'accepted'}),
             ('PreviewWorktreeExport', {'export_id': 'x3', 'proposals': [{'path': 'feature.txt'}]}),
             ('ReadWorktreeExport', {'untrusted_content': FEATURE_CHILD}),
             ('IntegrateWorktreeFile', {'status': 'awaiting_user_approval', 'action_id': 'e', 'kind': 'edit'}),
             ('park', None)]
    for expected, value in steps:
        delta = _turn(fixture, root, ['SpawnWorktreeAgent', 'ExecuteAgentEffect'])
        if expected == 'park':
            assert 'content' in delta, delta
            _answer(root, delta, None)
            root.append(resume)  # native approval resumes the parked root
            continue
        assert delta['tool_calls'][0]['function']['name'] == expected, (expected, delta)
        _answer(root, delta, value)
    assert _turn(fixture, root) == {'content': DONE}
    root.append({'role': 'assistant', 'content': DONE})
    root.append({'role': 'user', 'content': COMPLETION + ': The ' + HOOK_ISSUE + ' that covers the current project files.'})
    assert _turn(fixture, root) == {'content': DONE}
    seen = fixture.state('w3')
    assert seen['finals'] == 5 and seen['hook_issue'] and seen['spawn']['status'] == 'awaiting_user_approval'
    assert seen['request_view'] == {'arguments': WORKTREE_TESTS, 'executable': True, 'blocked_reason': None}
    assert seen['execute']['status'] == 'awaiting_user_approval' and seen['review']['review'] == 'accepted'
    assert seen['integrate']['status'] == 'awaiting_user_approval' and not fixture.anomalies


def test_checks_are_independent_and_names_unique():
    campaign = Campaign(['unused'], None)
    campaign.fixture.server.server_close()
    assert campaign.check('first', lambda: {'ok': True}) is True
    assert campaign.check('second', lambda: 1 / 0) is False
    assert campaign.check('third', lambda: campaign.need('missing')) is False
    assert [c['passed'] for c in campaign.checks] == [True, False, False]
    assert campaign.checks[1]['error'].startswith('ZeroDivisionError') and 'Prerequisite' in campaign.checks[2]['error']
    try:
        campaign.check('first', lambda: None)
    except AssertionError:
        pass
    else:
        raise AssertionError('A duplicate check name was accepted.')
    assert len(set(TAG.findall(' '.join(GOALS.values())))) == len(GOALS)
    assert not re.search(r"\b(I'll|I will|let me)\b", DONE, re.I)
