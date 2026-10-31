"""Phase 46 (1.9.28, w9-git): recovery's honest stops (golden held-out A: 9 of 15, 6 honest incomplete, on 1.9.26 and
1.9.27), the part the second opinion leaves to the terminal (second-opinion.md 4).

A RunCommand is killed at 60 s (terminal.COMMAND_SECONDS) and, since 1.9.27, reported as a result with its output so
far. A command that simply needs longer then had no other way to run: StartCommand (up to 300 s, in the background) was
offered only to requests with async or background wording. Now a RunCommand stopped at its time bound offers
StartCommand and the process tools from the run's next step (above 8K, where found tools join), and its note says so.
The terminal's Phase 10 contracts on NAME=value and chmod +x changed only by the person's D178 (reviewed, on unless
PEBBLE_1928_TERMINAL=0; test_terminal_1928); sh script.sh stays refused.

Scripted model only; the sandboxed command is scripted (test_recovery_46.timed_out).
"""

import pytest

import pebble.app as app_module
from pebble import terminal
from pebble.providers import Providers
from test_guard_terminal_outcomes_24 import call, env, final, settled  # noqa: F401 (fixture)
from test_recovery_46 import contents, large, timed_out

PROCESS = {'StartCommand', 'WaitProcess', 'ReadProcess', 'StopProcess'}


def offered(env, monkeypatch):
    """The tool names of every root request, beside the env fixture's scripted replies."""
    seen, scripted = [], Providers.complete

    def complete(provider, messages, tools, cancel, token, effort=False):
        seen.append({t['function']['name'] for t in tools or ()})
        return scripted(provider, messages, tools, cancel, token, effort)
    monkeypatch.setattr(Providers, 'complete', complete)
    return seen


def run(env, text):
    app = env.app
    chat = app.new_chat(str(env.project))
    env.script = [call('RunCommand', {'command': 'python3 export.py'}, 'run'),
                  final('export.py needs longer than 60 s here; nothing else was changed.\n\nFINAL: blocked')]
    app.work(chat, text, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, rid, 'waiting_approval', 1)
    command = next(a for a in app.store.actions(chat['id']) if a['kind'] == 'command' and a['status'] == 'pending')
    app.approve(chat['id'], command['id'])
    settled(env, rid, 'completed', 2)
    return rid


@pytest.mark.parametrize('budget', [32768, 8192])
def test_a_command_stopped_at_its_time_bound_offers_start_command_from_the_next_step(env, monkeypatch, budget):
    # Phase 46 (1.9.28, D184; restated by the lead): a confined command runs without a card in Auto now; this test is
    # about what follows the command, on the approval path every step that still asks takes, so it keeps the card
    monkeypatch.setenv('PEBBLE_AUTO_RISKY_ONLY', '0')
    monkeypatch.setattr(app_module, 'run_command', timed_out)
    large(env.app, budget)
    seen = offered(env, monkeypatch)
    rid = run(env, 'Run export.py and tell me how many rows it wrote. End with `FINAL: <rows>` or `FINAL: blocked`.')
    assert not PROCESS & seen[0]  # no async wording: the first request has RunCommand only, as before
    resumed = contents(env.requests[1])
    assert 'stale lock' in resumed and 'timed_out' in resumed  # 1.9.27's result and note stay
    if budget > 8192:
        assert PROCESS <= seen[1] and 'run it again with StartCommand' in resumed
        receipt = next(t for t in env.app.runs.tools(rid) if t['name'] == 'RunCommand')
        assert receipt['result']['result']['note'].endswith(terminal.LONGER_NOTE)
    else:  # 4K and 8K requests keep their tools (and the golden fixtures); the note names no tool they lack
        assert not PROCESS & seen[1] and 'StartCommand' not in resumed


def test_a_command_that_ends_in_time_offers_nothing_new(env, monkeypatch):
    # Phase 46 (1.9.28, D184; restated by the lead): a confined command runs without a card in Auto now; this test is
    # about what follows the command, on the approval path every step that still asks takes, so it keeps the card
    monkeypatch.setenv('PEBBLE_AUTO_RISKY_ONLY', '0')
    large(env.app)
    seen = offered(env, monkeypatch)
    env.output = 'wrote 9 rows to out/orders.csv\n'
    run(env, 'Run export.py and tell me how many rows it wrote. End with `FINAL: <rows>` or `FINAL: blocked`.')
    assert not PROCESS & seen[1] and 'StartCommand' not in contents(env.requests[1])
