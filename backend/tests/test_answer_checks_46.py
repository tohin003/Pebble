"""Phase 46 (1.9.27): two answer checks, each measured first on the golden held-out aggregates (never the sealed split).

- repo_navigation (sol: 2 false completions of 15): AgentGuard's grounding accepted a wrong answer that named a code
  file this run read, a function that file defines and a number some other code file holds. Three cross-checks on the
  read text (requirements._grounding): a number must be in the answer's file or a module it imports; the request's
  quoted text must be in the answer's file when code this run read holds it; and when that text sits inside one
  function of that file, the answer's function must be that one. The dev prompts' shapes below (d-rn-01's fixture
  shape, rebuilt by hand); a right answer is never refused.
- clarification (Opus: 3 false completions of 15): a request that describes a document ("copy the Q3 report") where
  the project holds two that fit was done on a guess and said done. One system sentence (runner.PICK_ONE), only for
  such a request, above 8K, in a project.
"""
import pytest

from pebble import requirements as rq
from pebble import runner
from test_runner_honesty_24 import LARGE, env  # noqa: F401 (fixture)

RN = ('Which function logs the warning "retry budget exhausted", and after how many attempts does it fire for a Client '
      "created with default settings? Don't change anything. End your reply with the line "
      '`FINAL: <file path>:<function name> <number>`, or `FINAL: blocked` and the reason if you can\'t.')
CLIENT = ('import logging\n\nfrom . import defaults\n\nlog = logging.getLogger(__name__)\n\n\nclass Client:\n'
          '    def __init__(self, transport, max_retries=None):\n        self.transport = transport\n'
          '        self.max_retries = defaults.MAX_RETRIES if max_retries is None else max_retries\n\n'
          '    def send(self, request):\n        attempts = 0\n        while True:\n            attempts += 1\n'
          '            try:\n                return self.transport(request)\n            except ConnectionError:\n'
          '                if attempts >= self.max_retries:\n                    _give_up(attempts)\n'
          '                    raise\n\n\ndef _give_up(attempts):\n'
          "    log.warning('retry budget exhausted after %d attempts', attempts)\n")
DEFAULTS = 'MAX_RETRIES = 5\nPOOL_SIZE = 8\nBACKOFF_SECONDS = 0.5\n'
LEGACY = ('import logging\n\nlog = logging.getLogger(__name__)\n\n\ndef _give_up_legacy(attempts):\n'
          "    log.warning('retry limit exhausted after %d attempts', attempts)\n")
POOL = ('import logging\n\nlog = logging.getLogger(__name__)\nPOOL_RETRIES = 3\n\n\ndef _drain(pool_name, failures):\n'
        "    if failures >= POOL_RETRIES:\n        log.warning('retry budget exceeded for pool %s', pool_name)\n"
        '        return False\n    return True\n')
NOTES = '# Notes\n\n- The warning "retry budget exhausted" fires after 4 attempts by default (2025 behaviour).\n'
READ = ['netkit/client.py', 'netkit/defaults.py', 'netkit/legacy.py', 'netkit/pool.py', 'docs/notes.md']


def netkit(tmp_path, files=None):
    root = tmp_path / 'r'
    (root / 'netkit').mkdir(parents=True)
    (root / 'docs').mkdir()
    for rel, text in (files or {'netkit/client.py': CLIENT, 'netkit/defaults.py': DEFAULTS, 'netkit/legacy.py': LEGACY,
                                'netkit/pool.py': POOL, 'docs/notes.md': NOTES}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def grounded(root, final, reads=READ, goal=RN):
    return rq.issues([], {}, root=root, actions=[], answer='The answer.\n\nFINAL: ' + final, reads=reads,
                     test_evidence=False, goal=goal)


def test_the_right_answer_is_never_refused(tmp_path):
    root = netkit(tmp_path)
    assert grounded(root, 'netkit/client.py:_give_up 5') == []
    assert grounded(root, 'netkit/client.py:_give_up 5 attempts') == []
    assert grounded(root, '`netkit/client.py:_give_up()` 5') == []
    assert grounded(root, 'netkit/client.py:_give_up 5', goal=None) == []  # without the request, as before


@pytest.mark.parametrize('final, problem', [
    ('netkit/legacy.py:_give_up_legacy 5', 'quoted text "retry budget exhausted" is not in netkit/legacy.py; this run '
                                          'read it in netkit/client.py'),
    ('netkit/pool.py:_drain 3', 'quoted text "retry budget exhausted" is not in netkit/pool.py'),
    # Phase 46 (1.9.28): the scope is the code connected to the named file by imports (requirements._scope), no longer
    # the file and its one-hop imports, so the sentence names the file only; the same catch
    ('netkit/client.py:_give_up 3', 'the value 3 is not in netkit/client.py or the code connected to it by imports; '
                                    'this run read it in netkit/pool.py'),
    ('netkit/client.py:send 5', 'in netkit/client.py the request\'s quoted text "retry budget exhausted" is inside '
                                '_give_up, not send'),
    ('netkit/client.py:_give_up 4', 'the value 4 does not appear in any code file this run read'),
])
def test_a_wrong_but_grounded_answer_is_caught_against_the_read_text(tmp_path, final, problem):
    [(issue, notice)] = grounded(netkit(tmp_path), final)
    assert issue.startswith(rq.GROUNDING) and problem in issue, issue
    assert notice == "Pebble couldn't confirm the answer from the code it read."


def test_a_message_built_at_run_time_or_a_module_constant_is_not_a_wrong_file(tmp_path):
    # the quoted text is in no code file this run read (an f-string, a constant elsewhere): nothing to compare
    dynamic = CLIENT.replace("'retry budget exhausted after %d attempts'", "'retry budget ' + WORD + ' after %d attempts'")
    root = netkit(tmp_path, {'netkit/client.py': dynamic, 'netkit/defaults.py': DEFAULTS})
    assert grounded(root, 'netkit/client.py:_give_up 5', reads=['netkit/client.py', 'netkit/defaults.py']) == []
    # the text is a module-level constant of the answer's file, inside no function: the function check has nothing to say
    constant = CLIENT.replace("log.warning('retry budget exhausted after %d attempts', attempts)", 'log.warning(MESSAGE, attempts)')
    constant = constant.replace('log = logging.getLogger(__name__)\n', "log = logging.getLogger(__name__)\n"
                                "MESSAGE = 'retry budget exhausted after %d attempts'\n")
    root = netkit(tmp_path / 'b', {'netkit/client.py': constant, 'netkit/defaults.py': DEFAULTS})
    assert grounded(root, 'netkit/client.py:_give_up 5', reads=['netkit/client.py', 'netkit/defaults.py']) == []


def test_a_message_kept_in_a_module_the_file_imports_counts_as_the_files(tmp_path):
    # The lead's review: error and log messages often live in a constants module; the function that uses one is right
    imported = CLIENT.replace('from . import defaults\n', 'from . import defaults\nfrom .messages import EXHAUSTED\n')
    imported = imported.replace("log.warning('retry budget exhausted after %d attempts', attempts)",
                                'log.warning(EXHAUSTED, attempts)')
    root = netkit(tmp_path, {'netkit/client.py': imported, 'netkit/defaults.py': DEFAULTS, 'netkit/legacy.py': LEGACY,
                             'netkit/messages.py': "EXHAUSTED = 'retry budget exhausted after %d attempts'\n"})
    reads = ['netkit/client.py', 'netkit/defaults.py', 'netkit/legacy.py', 'netkit/messages.py']
    assert grounded(root, 'netkit/client.py:_give_up 5', reads=reads) == []
    # a file that does not import it is still the wrong file
    [(issue, _)] = grounded(root, 'netkit/legacy.py:_give_up_legacy 5', reads=reads)
    assert 'quoted text "retry budget exhausted" is not in netkit/legacy.py' in issue


def test_imports_follow_pythons_relative_and_absolute_forms_and_a_src_layout():
    read = {'netkit/client.py', 'netkit/defaults.py', 'netkit/pool.py', 'src/shop/money.py', 'src/shop/__init__.py',
            'other/x.py'}
    assert rq._imports('from . import defaults\n', 'netkit/client.py', read) == {'netkit/defaults.py'}
    assert rq._imports('from .defaults import MAX_RETRIES\n', 'netkit/client.py', read) == {'netkit/defaults.py'}
    assert rq._imports('from netkit.pool import _drain\nimport netkit.defaults\n', 'netkit/client.py', read) == {
        'netkit/pool.py', 'netkit/defaults.py'}
    assert rq._imports('from shop.money import fmt\nimport shop\n', 'src/shop/cart.py', read) == {
        'src/shop/money.py', 'src/shop/__init__.py'}
    assert rq._imports('import os, json\nfrom ..other import x\n', 'netkit/client.py', read) == {'other/x.py'}
    assert rq._imports('x = 1\n', 'netkit/client.py', read) == set()


def test_function_bodies_are_cut_at_their_indentation():
    bodies = rq._function_bodies(CLIENT)
    assert set(bodies) == {'__init__', 'send', '_give_up'}
    assert "'retry budget exhausted" in bodies['_give_up'] and "'retry budget exhausted" not in bodies['send']
    assert '_give_up(attempts)' in bodies['send'] and 'def _give_up' not in bodies['send']


# -- clarification: a described document that several project files may fit -----------------------------------------

ASK = ('Copy the Q3 report into summary.md at the top level of this folder. End your reply with the line `FINAL: done`, '
       'or `FINAL: blocked` and the reason if something stops you.')
ASK_SHORT = 'Copy the Q3 report to ./summary.md. Last line: `FINAL: done` (or `FINAL: blocked` + reason).'
EXACT = ('In config/app.toml, change timeout_seconds in the [server] section from 30 to 45. Leave the [client] section '
         'alone. End your reply with the line `FINAL: done`, or `FINAL: blocked` and the reason if something stops you.')
EXACT_SHORT = 'config/app.toml: [server] timeout_seconds 30 -> 45; [client] unchanged. Last line: `FINAL: done`.'
REPORT_PRINTED = ('Add a method `low_stock(threshold=5)` to Store. The report printed by `python3 -m inventory.cli '
                  'data/stock.json` must stay byte-identical to samples/report.txt. End with `FINAL: done`.')


@pytest.mark.parametrize('text, said', [(ASK, True), (ASK_SHORT, True), (EXACT, False), (EXACT_SHORT, False),
                                        (REPORT_PRINTED, False), ('Open the latest draft and fix the typos.', True),
                                        ('Summarize the meeting transcript into notes.md.', True),
                                        ('Fix the bug in report.py and run the tests.', False)])
def test_the_requests_that_describe_a_document(text, said):
    assert bool(runner.DESCRIBED_ITEM.search(text)) is said


def system_of(e, text, folder):
    e.script = lambda n, messages, tools: 'Which one do you mean: finance or operations?'
    e.work(text, folder)
    return e.requests[0][0][0]['content']


def test_a_described_document_adds_the_sentence_above_8k_in_a_project(env):
    e = env(LARGE)
    folder = e.project({'exports/report-2026-q3-finance.md': '# Q3 finance\n', 'exports/report-2026-q3-ops.md': '# Q3 ops\n'})
    assert runner.PICK_ONE in system_of(e, ASK, folder)
    assert runner.PICK_ONE not in system_of(env(LARGE, 'exact'), EXACT, folder)
    assert runner.PICK_ONE not in system_of(env(8192, 'small'), ASK, folder)  # 8K and below: unchanged
    assert runner.PICK_ONE not in system_of(env(LARGE, 'chat'), ASK, None)   # no project: nothing to pick from
    assert 'AskUserQuestion before changing anything' in runner.PICK_ONE
