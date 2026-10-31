"""Phase 46 (1.9.28): the answer checks that 1.9.27 added refused right answers about code.

A (Opus) on the golden held-out split: repo_navigation 13 of 15 with 2 honest_incomplete on 1.9.27, 15 of 15 on 1.9.26.
Offline, on realistic shapes of the dev case d-rn-01 (rebuilt by hand below; the private cases d-rn-92..98 hold the
same shapes), 1.9.27's _grounding refused 7 right answers of 7 where 1.9.26 refused none: the default reached the
logging function through its caller (the logger in its own module), through two imports, through a Black-style
parenthesized import, or in JavaScript; the quoted message lived in the module that uses the setting asked for; a
docstring elsewhere quoted the message while the right function had a signature spread over lines; the request asked
which function decides, not which one logs. A model that read the files by their absolute paths was refused on every
version (the second opinion). Each refusal withheld the right reply and asked for a fix with REPAIR_ADVICE ("change the
code or the command ... do not claim success"); a reply that then said blocked, appended the source to its FINAL line
or moved to the file the issue named ended held with no matching answer (honest_incomplete).

The scope is now what the named file imports (directly or through other modules) and each caller that uses the
answer's name, with what it imports; function bodies span their signature and leave docstrings out; the function check
runs only when the request asks which function emits the text; a doubted answer about code gets GROUNDING_ADVICE. Every
wrong answer 1.9.27 caught is still caught, and two it missed are caught (a docstring shape, and a look-alike reached
through a shared facade is kept out as before), while a look-alike that merely imports the named file stays out too.
"""
import json
import os

import pytest

from pebble import requirements as rq
from pebble import runner

RN = ('Which function logs the warning "retry budget exhausted", and after how many attempts does it fire for a Client '
      "created with default settings? Don't change anything. End your reply with the line "
      '`FINAL: <file path>:<function name> <number>`, or `FINAL: blocked` and the reason if you can\'t.')
DECIDES = ('Which function decides how many attempts are made before "retry budget exhausted" is logged, and how many '
           'is that by default? Read-only. Last line: `FINAL: <file path>:<function name> <number>`.')
SETTING = ('Which setting decides when users see "session expired, please sign in again", and what is its default in '
           "minutes? Don't change anything. End your reply with the line `FINAL: <file path>:<name> <number>`.")

CLIENT = ('import logging\n\nfrom . import defaults\n\nlog = logging.getLogger(__name__)\n\n\nclass Client:\n'
          '    def __init__(self, transport, max_retries=None):\n        self.transport = transport\n'
          '        self.max_retries = defaults.MAX_RETRIES if max_retries is None else max_retries\n\n'
          '    def send(self, request):\n        attempts = 0\n        while True:\n            attempts += 1\n'
          '            try:\n                return self.transport(request)\n            except ConnectionError:\n'
          '                if attempts >= self.max_retries:\n                    _give_up(attempts)\n'
          '                    raise\n\n\ndef _give_up(attempts):\n'
          "    log.warning('retry budget exhausted after %d attempts', attempts)\n")
DEFAULTS = 'MAX_RETRIES = 5\nPOOL_SIZE = 8\nBACKOFF_SECONDS = 0.5\n'
POOL = ('import logging\n\nlog = logging.getLogger(__name__)\nPOOL_RETRIES = 3\n\n\ndef _drain(pool_name, failures):\n'
        "    if failures >= POOL_RETRIES:\n        log.warning('retry budget exceeded for pool %s', pool_name)\n"
        '        return False\n    return True\n')
NOTES = '# Notes\n\n- The warning "retry budget exhausted" fires after 4 attempts by default (2025 behaviour).\n'
BASE = {'netkit/__init__.py': '', 'netkit/client.py': CLIENT, 'netkit/defaults.py': DEFAULTS, 'netkit/pool.py': POOL,
        'docs/notes.md': NOTES}

PAREN = CLIENT.replace('from . import defaults\n', 'from .defaults import (\n    BACKOFF_SECONDS,\n    MAX_RETRIES,\n)\n'
                       ).replace('defaults.MAX_RETRIES', 'MAX_RETRIES')
CALLER = CLIENT.replace('from . import defaults\n', 'from . import defaults\nfrom .retry import give_up\n').replace(
    '_give_up(attempts)\n', 'give_up(attempts)\n').split('\n\ndef _give_up')[0] + '\n'
RETRY = ('import logging\n\nlog = logging.getLogger("netkit.client")\n\n\ndef give_up(attempts):\n'
         "    log.warning('retry budget exhausted after %d attempts', attempts)\n")
TWO_HOPS = CLIENT.replace('from . import defaults\n', 'from .config import Settings\n').replace(
    'defaults.MAX_RETRIES', 'Settings().max_retries')
CONFIG = ('from dataclasses import dataclass\n\nfrom . import defaults\n\n\n@dataclass\nclass Settings:\n'
          '    max_retries: int = defaults.MAX_RETRIES\n')
DOCSTRING = CLIENT.replace('    def send(self, request):\n', '    def send(self, request):\n        """Send; logs '
                           "'retry budget exhausted' once the budget is spent.\"\"\"\n").replace(
    'def _give_up(attempts):\n', 'def _give_up(\n    attempts,\n):\n')
DECIDING = ('import logging\n\nfrom . import defaults\n\nlog = logging.getLogger(__name__)\n\n\n'
            'def retry_limit(max_retries=None):\n    return defaults.MAX_RETRIES if max_retries is None else max_retries\n\n\n'
            'def send(transport, request, max_retries=None):\n    limit = retry_limit(max_retries)\n'
            '    for attempt in range(1, limit + 1):\n        try:\n            return transport(request)\n'
            '        except ConnectionError:\n            if attempt == limit:\n'
            "                log.warning('retry budget exhausted after %d attempts', attempt)\n                raise\n")
JS_CLIENT = ("import { MAX_RETRIES } from './defaults.js';\n\nexport class Client {\n"
             '  constructor(transport, maxRetries = MAX_RETRIES) { this.transport = transport; this.maxRetries = maxRetries; }\n'
             '}\n\nfunction giveUp(attempts) {\n  console.warn(`retry budget exhausted after ${attempts} attempts`);\n}\n')
JS_DEFAULTS = 'export const MAX_RETRIES = 5;\n'
JS_POOL = 'export const POOL_RETRIES = 3;\nexport function drain(name, failures) {\n  return failures >= POOL_RETRIES;\n}\n'
SETTINGS = 'SESSION_TIMEOUT_MINUTES = 30\n'
LEGACY_SETTINGS = 'SESSION_TIMEOUT_MINUTES = 20  # 2024 value, unused\n'
VIEWS = ('from . import settings\n\n\ndef check_session(session, now):\n    idle = (now - session.last_seen) / 60\n'
         '    if idle > settings.SESSION_TIMEOUT_MINUTES:\n'
         '        return {"error": "session expired, please sign in again"}\n    return None\n')
# look-alikes connected to the named file: a pool of clients (it imports the client), and a facade that imports both
POOL_OF_CLIENTS = 'from .client import Client\n' + POOL
API = 'from .client import Client\nfrom .pool import _drain\n\n__all__ = ["Client", "_drain"]\n'

SHAPES = {
    'paren': ({**BASE, 'netkit/client.py': PAREN}, RN),
    'caller': ({**BASE, 'netkit/client.py': CALLER, 'netkit/retry.py': RETRY}, RN),
    'two_hops': ({**BASE, 'netkit/client.py': TWO_HOPS, 'netkit/config.py': CONFIG}, RN),
    'docstring': ({**BASE, 'netkit/client.py': DOCSTRING}, RN),
    'deciding': ({'netkit/__init__.py': '', 'netkit/client.py': DECIDING, 'netkit/defaults.py': DEFAULTS}, DECIDES),
    'javascript': ({'src/client.js': JS_CLIENT, 'src/defaults.js': JS_DEFAULTS, 'src/pool.js': JS_POOL}, RN),
    'setting': ({'app/__init__.py': '', 'app/settings.py': SETTINGS, 'app/legacy_settings.py': LEGACY_SETTINGS,
                 'app/views.py': VIEWS}, SETTING),
    'pool_of_clients': ({**BASE, 'netkit/pool.py': POOL_OF_CLIENTS}, RN),
    'facade': ({**BASE, 'netkit/api.py': API}, RN),
}


def project(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def check(tmp_path, shape, final, absolute=False):
    files, goal = SHAPES[shape]
    root = project(tmp_path / 'r', files)
    reads = [str(root / rel) for rel in files] if absolute else list(files)
    return rq.issues([], {}, root=root, actions=[], answer='The answer.\n\nFINAL: ' + final, reads=reads,
                     test_evidence=False, goal=goal)


RIGHT = [('paren', 'netkit/client.py:_give_up 5'), ('caller', 'netkit/retry.py:give_up 5'),
         ('two_hops', 'netkit/client.py:_give_up 5'), ('docstring', 'netkit/client.py:_give_up 5'),
         ('deciding', 'netkit/client.py:retry_limit 5'), ('javascript', 'src/client.js:giveUp 5'),
         ('setting', 'app/settings.py:SESSION_TIMEOUT_MINUTES 30'),
         ('pool_of_clients', 'netkit/client.py:_give_up 5'), ('facade', 'netkit/client.py:_give_up 5')]


@pytest.mark.parametrize('shape, final', RIGHT)
def test_a_right_answer_reached_through_other_modules_is_not_refused(tmp_path, shape, final):
    # 1.9.27 refused the first seven (1.9.26 none)
    assert check(tmp_path, shape, final) == []


@pytest.mark.parametrize('shape, final', RIGHT[:3])
def test_files_read_by_their_absolute_paths_count_as_read(tmp_path, shape, final):
    # every version before refused these: "netkit/client.py is named in the answer but this run never read it"
    assert check(tmp_path, shape, final, absolute=True) == []
    # macOS: the model may name /tmp/... for a project the host resolved to /private/tmp/... (and the other way round)
    root = os.path.realpath(tmp_path)
    assert rq._project_paths(root, [root + '/netkit/client.py', './netkit/pool.py', 'netkit//defaults.py',
                                    '/elsewhere/netkit/client.py']) == {'netkit/client.py', 'netkit/pool.py',
                                                                        'netkit/defaults.py'}


@pytest.mark.parametrize('shape, final, problem', [
    ('paren', 'netkit/client.py:_give_up 3', 'the value 3 is not in netkit/client.py or the code connected to it by '
                                             'imports; this run read it in netkit/pool.py'),
    ('paren', 'netkit/client.py:send 5', 'quoted text "retry budget exhausted" is inside _give_up, not send'),
    ('caller', 'netkit/retry.py:give_up 3', 'the value 3 is not in netkit/retry.py'),
    ('caller', 'netkit/pool.py:_drain 3', 'quoted text "retry budget exhausted" is not in netkit/pool.py'),
    ('two_hops', 'netkit/client.py:_give_up 3', 'this run read it in netkit/pool.py'),
    ('docstring', 'netkit/client.py:send 5', 'is inside _give_up, not send'),  # 1.9.27 missed this one
    ('javascript', 'src/client.js:giveUp 3', 'this run read it in src/pool.js'),
    ('javascript', 'src/pool.js:drain 3', 'quoted text "retry budget exhausted" is not in src/pool.js'),
    ('setting', 'app/legacy_settings.py:SESSION_TIMEOUT_MINUTES 20', 'is not in app/legacy_settings.py'),
    ('setting', 'app/settings.py:SESSION_TIMEOUT_MINUTES 20', 'this run read it in app/legacy_settings.py'),
    # a look-alike that imports the named file, or a facade beside it, never makes its number the answer's
    ('pool_of_clients', 'netkit/client.py:_give_up 3', 'this run read it in netkit/pool.py'),
    ('facade', 'netkit/client.py:_give_up 3', 'this run read it in netkit/pool.py'),
])
def test_a_wrong_answer_is_still_caught(tmp_path, shape, final, problem):
    [(issue, _)] = check(tmp_path, shape, final)
    assert issue.startswith(rq.GROUNDING) and problem in issue, issue


def test_a_package_that_imports_all_its_modules_does_not_join_them():
    # follow-up 1: a hub is passed through only to the module that holds the names imported from it
    texts = {'pkg/__init__.py': 'from .client import Client\nfrom .pool import Pool\n', 'pkg/client.py': 'X = 1\n',
             'pkg/pool.py': 'POOL_RETRIES = 3\n'}
    assert rq._scope('pkg/client.py', set(texts), texts.get) == {'pkg/client.py'}
    texts['pkg/client.py'] = 'from . import Pool\n'
    assert rq._scope('pkg/client.py', set(texts), texts.get) == {'pkg/client.py', 'pkg/__init__.py', 'pkg/pool.py'}
    texts['pkg/client.py'] = 'import pkg\nfrom . import Client\n'  # the package whole, or a name pool.py does not hold
    assert rq._scope('pkg/client.py', set(texts), texts.get) == {'pkg/client.py', 'pkg/__init__.py'}
    # a caller is followed only when it calls the answer's function
    texts = {'a.py': 'def f():\n    return 1\n', 'b.py': 'from a import f\nfrom c import N\nf()\n', 'c.py': 'N = 5\n'}
    assert rq._scope('a.py', set(texts), texts.get, 'f') == {'a.py', 'b.py', 'c.py'}
    assert rq._scope('a.py', set(texts), texts.get, 'g') == {'a.py'}
    assert rq._scope('a.py', set(texts), texts.get) == {'a.py'}  # no name, no caller (the security review H1)


def test_imports_parse_parenthesized_continued_and_javascript_forms():
    read = {'netkit/defaults.py', 'netkit/pool.py', 'src/defaults.ts', 'src/util/index.ts', 'src/legacy.js',
            'src/lib/format.ts'}
    assert rq._imports('from .defaults import (\n    A,  # first\n    B,\n)\n', 'netkit/client.py', read) == {
        'netkit/defaults.py'}
    assert rq._imports('from .pool import \\\n    POOL_RETRIES\n', 'netkit/client.py', read) == {'netkit/pool.py'}
    assert rq._imports("import { A } from './defaults.js';\nimport u from './util';\nconst l = require('./legacy');\n"
                       "import { f } from '@/lib/format';\n", 'src/client.ts', read) == {
        'src/defaults.ts', 'src/util/index.ts', 'src/legacy.js', 'src/lib/format.ts'}
    assert rq._imports("import x from 'lodash';\n", 'src/client.ts', read) == set()
    assert rq._imports('package main\n', 'cmd/main.go', read) == set()


def test_function_bodies_span_a_signature_over_lines_and_leave_docstrings_and_comments_out():
    bodies = rq._function_bodies(DOCSTRING)
    assert "'retry budget exhausted after" in bodies['_give_up']
    assert 'retry budget exhausted' not in bodies['send']
    assert rq._function_bodies('def f(\n    a,\n):\n    return a  # note\n')['f'].rstrip().endswith('return a')
    # a '#' inside a string is not a comment
    assert "'#%d: retry budget exhausted'" in rq._function_bodies("def g(n):\n    log('#%d: retry budget exhausted', n)\n")['g']


@pytest.mark.parametrize('text, runs', [
    (RN, True), ('Who logs "retry budget exhausted", and after how many attempts with a default Client?', True),
    ('I keep seeing the warning "retry budget exhausted" in our logs. Which function logs it?', True),
    ('Where is "retry budget exhausted" logged, and in which function?', True),
    ('Which part of the code prints "retry budget exhausted"?', True),
    # follow-up 1 (the security review H3): the check runs as in 1.9.27 unless the request asks what decides a value
    ('In which function is "retry budget exhausted" logged, and after how many attempts by default?', True),
    ('Which function is responsible for the "retry budget exhausted" warning, and after how many attempts?', True),
    ('Which function triggers "retry budget exhausted", and after how many attempts by default?', True),
    (DECIDES, False), (SETTING, False),
    ('Function that decides the attempts before "retry budget exhausted" is logged, and the default?', False),
    ('Before "retry budget exhausted" shows up, something decides how many attempts are made. Which function is that?',
     False),
    ('Which function decides how many attempts are made before it logs "retry budget exhausted"?', False),
])
def test_the_function_check_follows_what_the_request_asks(text, runs):
    assert (not rq.DECIDES.search(text)) is runs


GO_CLIENT = ('package netkit\n\nfunc send(req string, attempts int) {\n\tif attempts >= MaxRetries {\n\t\tgiveUp(attempts)\n'
             '\t}\n}\n\nfunc giveUp(n int) { log.Printf("retry budget exhausted after %d", n) }\n')


def test_another_language_keeps_1927s_protection_and_takes_its_package_folder(tmp_path):
    # follow-up 1 (the security review L5): a Go answer is scoped by its package folder, and a value from another file
    # counts only when the named file's code uses a name that holds it
    files = {'cmd/client.go': GO_CLIENT, 'cmd/defaults.go': 'package netkit\n\nconst MaxRetries = 5\n',
             'cmd/pool.go': 'package netkit\n\nconst PoolRetries = 3\n', 'cmd/other.go': 'package netkit\n\nfunc send2() {}\n'}
    root = project(tmp_path / 'g', files)

    def go(final):
        return rq.issues([], {}, root=root, actions=[], answer='x\n\nFINAL: ' + final, reads=list(files),
                         test_evidence=False, goal=RN)
    assert go('cmd/client.go:giveUp 5') == []
    for final, problem in [('cmd/client.go:giveUp 3', 'cmd/client.go uses no name that holds it there'),
                           ('cmd/other.go:send2 5', 'cmd/other.go uses no name that holds it there'),
                           ('cmd/client.go:giveUp 4', 'the value 4 does not appear in any code file this run read')]:
        [(issue, _)] = go(final)
        assert problem in issue, issue
    # the quoted text must be in the named file, or in a file of its package that uses the name (1.9.27: the file)
    [(issue, _)] = go('cmd/other.go:send2 5')
    assert 'quoted text "retry budget exhausted" is not in cmd/other.go' in issue


# -- the repair a doubted answer about code gets (runner) ---------------------------------------------------------------

def drive(tmp_path, files, replies, request=RN, reads=()):
    """One project run through runner.run above 8K with a scripted model: Reads of `reads` first, then each reply in
    turn (the last repeats). Returns the host messages after the first request, the stored reply and the run's state."""
    from pebble.app import App
    from test_high_fd_waits_21 import close_guard_databases
    folder = project(tmp_path / 'project', files)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    app.store.set_setting('model_path', '')
    app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                              'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                              'context': 32768, 'budget': 32768, 'tools': True}])
    app.model.select('route')
    steps = [{'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'r%d' % n, 'type': 'function', 'function': {'name': 'Read', 'arguments': json.dumps({'file_path': path})}}
        for n, path in enumerate(reads)]}] if reads else []
    replies, host = list(replies), []

    def complete(messages, tools, cancel, token, deliberate=False):
        last = messages[-1]
        host.append(last.get('content') if last.get('role') == 'user' and isinstance(last.get('content'), str) else '')
        step = steps.pop(0) if steps else {'role': 'assistant', 'content': replies.pop(0) if len(replies) > 1 else replies[0]}
        if step.get('content'):
            token(step['content'])
        return step
    app.model.complete = complete
    try:
        chat = app.new_chat(str(folder))
        runner.run(app, app.chat(chat['id']), request, False, [])
        run = app.runs.list(chat['id'])[0]
        return host[1:], app.chat(chat['id'])['messages'][-1]['content'], app.runs.get(run['id'])['state']
    finally:
        app.active = app.active_run = None
        app.close()
        close_guard_databases(tmp_path)


def test_a_doubted_answer_about_code_is_asked_to_be_checked_again_not_fixed(tmp_path):
    host, reply, state = drive(tmp_path, BASE, ['The pool gives up after 3.\n\nFINAL: netkit/client.py:_give_up 3'])
    repairs = [m for m in host if m.startswith('HOST COMPLETION CHECK')]
    assert len(repairs) == 1 and runner.GROUNDING_ADVICE in repairs[0] and runner.REPAIR_ADVICE not in repairs[0]
    # still held: a wrong answer the model keeps is never presented as checked
    assert state == 'verification_required' and reply.endswith('FINAL: netkit/client.py:_give_up 3')


def test_a_right_answer_in_a_refused_shape_is_given_once(tmp_path):
    # 1.9.27: the caller shape's right answer was withheld, the model was told to change code, and the run ended held
    files = {**BASE, 'netkit/client.py': CALLER, 'netkit/retry.py': RETRY}
    host, reply, state = drive(tmp_path, files, ['give_up logs it; a default Client allows 5 attempts.\n\n'
                                                 'FINAL: netkit/retry.py:give_up 5'])
    assert host == [] and state == 'completed' and reply.endswith('FINAL: netkit/retry.py:give_up 5')


def test_an_answer_read_by_absolute_paths_is_given_once(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'PREFETCH', False)  # only the model's own Reads, by absolute path (prefetch is relative)
    folder = os.path.realpath(tmp_path / 'project')
    host, reply, state = drive(tmp_path, BASE, ['_give_up logs it after 5 attempts.\n\nFINAL: netkit/client.py:_give_up 5'],
                               reads=[folder + '/netkit/client.py', folder + '/netkit/defaults.py'])
    # before: "netkit/client.py is named in the answer but this run never read it", a repair, then held
    assert not [m for m in host if m.startswith('HOST COMPLETION CHECK')] and state == 'completed'
    assert reply.endswith('FINAL: netkit/client.py:_give_up 5')
