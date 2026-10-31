"""Phase 46 (1.9.27): recovery (golden held-out: 9 of 15 cells passed on both models and 6 were honest stops, against
Codex's 12 and Claude Code's 10), where recovery means reading an error and doing the step another way.

Measured offline on the family's shape (dev d-rc-01: a script that fails, is fixed and rerun; never the sealed split):

- A RunCommand that ran past its 60 s deadline (a script polling a stale lock, a loop) was raised as an uncertain
  outcome, which held the whole run for the person's reconciliation (needs_reconciliation) and discarded what the
  command had printed. With nobody there to reconcile, the task ended. The kill is now a result the model reads and
  acts on, with the output so far and a note (as the Companion's Shell and Codex report a timeout); the person's own
  stop still raises.
- A refusal of the terminal broker (an environment override, a redirect, a shell wrapper, chmod, a script run by
  path) said only what was blocked, so the model tried variants until the loop note told it to give up. Each refusal
  now says what works instead; nothing new is allowed.
- The completion check's repair turn said "collect the missing evidence ... If blocked, state the blocker", which read
  as a choice; it now says to fix the cause first.

Scripted models only; the sandboxed command is scripted except where a test says otherwise (macOS only).
"""
import json
import os
import sys
import threading

import pytest

import pebble.app as app_module
from pebble import runner, terminal
from pebble.policy import PolicyError
from pebble.terminal import CommandOutcomeUncertain, inspect_command, run_command
from test_guard_terminal_outcomes_24 import CHANGED, call, env, final, settled  # noqa: F401 (fixture)

SANDBOX = sys.platform == 'darwin' and os.path.exists('/usr/bin/sandbox-exec')


# -- a refusal says what works instead --------------------------------------------------------------------------------

@pytest.mark.parametrize('command, hint', [
    ('PYTHONPATH=src python3 -m pytest -q', 'sys.path.insert'),
    ('python3 -c "$(cat run.txt)"', 'Compute the value inside the program'),
    ('cd scripts && python3 monthly.py', '`cd` is not needed'),
    ('python3 gen.py > out/report.txt', 'let the program write it'),
    ('cat data/sales.csv | head -3', 'run each command on its own'),
    ('bash scripts/build.sh', 'run its commands one at a time'),
    ('sh setup.sh', 'run its commands one at a time'),
    ('chmod +x scripts/run.sh', 'through its interpreter'),  # D178: a missing file; an existing one is reviewed
])
def test_a_refusal_says_what_works_instead(tmp_path, command, hint):
    with pytest.raises(PolicyError) as refused:
        inspect_command(command, tmp_path)
    assert hint in str(refused.value)
    assert 'blocked' in str(refused.value)  # still a refusal of the same command


def test_the_old_refusal_words_stay_for_the_tests_that_match_them(tmp_path, monkeypatch):
    # Phase 46 (1.9.28, D178: the person allows NAME=value outside a deny list, reviewed, unless PEBBLE_1928_TERMINAL=0):
    # a quoted assignment keeps the old words with the switch on, and `FOO=1 ls` keeps them with the switch off
    with pytest.raises(PolicyError, match='environment overrides'):
        inspect_command("'FOO=1' ls", tmp_path)
    monkeypatch.setenv('PEBBLE_1928_TERMINAL', '0')
    with pytest.raises(PolicyError, match='environment overrides'):
        inspect_command('FOO=1 ls', tmp_path)
    monkeypatch.delenv('PEBBLE_1928_TERMINAL')
    with pytest.raises(PolicyError, match='Privileged commands'):
        inspect_command('sudo ls', tmp_path)
    assert terminal.CHMOD_HINT not in str(pytest.raises(PolicyError, inspect_command, 'sudo ls', tmp_path).value)


def test_a_project_script_run_by_path_is_told_its_runtime():
    for name in ('./scripts/run.sh', '.venv/bin/python', 'bin/report'):
        with pytest.raises(PolicyError, match='Use a runtime') as refused:
            terminal.resolve_executable(name)
        assert 'one at a time' in str(refused.value) and 'virtual environment' in str(refused.value)


# -- a command past its deadline is a result, not a held run -----------------------------------------------------------

def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    return root


@pytest.mark.skipif(not SANDBOX, reason='actual macOS confinement')
def test_a_command_past_its_deadline_is_killed_and_reported_as_a_result(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, 'COMMAND_SECONDS', 1.0)
    root = project(tmp_path)
    (root / 'wait.py').write_text("import time\nprint('waiting for lock data/.export.lock (pid 48213) ...', flush=True)\n"
                                  "while True:\n    time.sleep(0.2)\n")
    result = run_command({**inspect_command('python3 wait.py', root), 'writable': True}, threading.Event())
    assert result['exit_code'] == terminal.TIMEOUT_EXIT == 124
    assert result['timed_out'] is True and result['interrupted'] is True and result['output_limit_reached'] is False
    assert 'waiting for lock data/.export.lock (pid 48213)' in result['output']
    assert result['note'].startswith('Pebble stopped this command after 1 s') and 'stale lock' in result['note']


@pytest.mark.skipif(not SANDBOX, reason='actual macOS confinement')
def test_a_command_past_the_output_limit_is_killed_and_reported_as_a_result(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, 'OUTPUT_LIMIT', 4000)
    root = project(tmp_path)
    (root / 'chatty.py').write_text("import time\nfor n in range(100000):\n    print('line %d %s' % (n, 'x' * 80), flush=True)\n"
                                    "    time.sleep(0.005)\n")
    result = run_command({**inspect_command('python3 chatty.py', root), 'writable': True}, threading.Event())
    assert result['exit_code'] == 124 and result['output_limit_reached'] is True and result['timed_out'] is False
    assert result['output'].startswith('line 0 ') and '4,000 bytes of output' in result['note']


@pytest.mark.skipif(not SANDBOX, reason='actual macOS confinement')
def test_a_finished_command_whose_output_was_cut_says_so(tmp_path):
    # Phase 46 (1.9.28, b-new): output past SHOWN_BYTES was cut with nothing saying so
    root = project(tmp_path)
    (root / 'long.py').write_text("for n in range(400):\n    print('row %03d %s' % (n, 'y' * 70))\n")
    result = run_command({**inspect_command('python3 long.py', root), 'writable': True}, threading.Event())
    assert result['exit_code'] == 0 and result['output_cut'] is True and result['output_bytes'] > terminal.SHOWN_BYTES
    assert result['output'].startswith('row 000 ') and 'row 399' not in result['output']
    assert result['note'].startswith('Only the first 16,000 of ') and 'Read' in result['note']
    (root / 'short.py').write_text("print('done')\n")
    short = run_command({**inspect_command('python3 short.py', root), 'writable': True}, threading.Event())
    assert short['output'].strip() == 'done' and 'output_cut' not in short and 'note' not in short


@pytest.mark.skipif(not SANDBOX, reason='actual macOS confinement')
def test_the_persons_own_stop_still_raises_an_uncertain_outcome(tmp_path):
    root = project(tmp_path)
    (root / 'slow.py').write_text('import time\ntime.sleep(30)\n')
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    with pytest.raises(CommandOutcomeUncertain, match='outcome is uncertain'):
        run_command({**inspect_command('python3 slow.py', root), 'writable': True}, cancel)


@pytest.mark.skipif(not SANDBOX, reason='actual macOS confinement')
def test_a_command_with_network_past_its_deadline_stays_an_uncertain_outcome(tmp_path, monkeypatch):
    # The lead's review: with approved network access it may have half done something outside the Mac (an upload),
    # and a retry could repeat it, so the person inspects it as before
    monkeypatch.setattr(terminal, 'COMMAND_SECONDS', 1.0)
    root = project(tmp_path)
    (root / 'upload.py').write_text('import time\nwhile True:\n    time.sleep(0.2)\n')
    with pytest.raises(CommandOutcomeUncertain, match='outcome is uncertain'):
        run_command({**inspect_command('python3 upload.py', root, network=True), 'writable': True}, threading.Event())


def timed_out(payload, cancel):
    """The result run_command returns for a command killed at its deadline (the fields above), scripted."""
    return {'exit_code': terminal.TIMEOUT_EXIT, 'output': 'waiting for lock data/.export.lock (pid 48213) ...\n',
            'output_projection': 'terminal_text/1', 'controls_removed': 0, 'raw_output_sha256': '0' * 64,
            'command': payload['command'], 'isolation': 'fixture', 'interrupted': True, 'timed_out': True,
            'output_limit_reached': False, 'note': terminal.STOPPED_NOTE.format(seconds=terminal.COMMAND_SECONDS)}


def contents(request):
    return '\n'.join(str(m.get('content')) for m in request)


def large(app, budget=32768):
    """A scripted route above 8K (the staged stop and the held reply apply, as in the golden runs)."""
    app.store.set_setting('model_path', '')
    app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                              'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                              'context': budget, 'budget': budget, 'tools': True}])
    app.model.select('route')


def test_a_timed_out_command_reaches_the_model_as_a_result_and_the_run_goes_on(env, monkeypatch):
    # Phase 46 (1.9.28, D184; restated by the lead): a confined command runs without a card in Auto now; the 1.9.26 fault
    # was in the approval path, which every step that still asks takes, so this run keeps the command's card
    monkeypatch.setenv('PEBBLE_AUTO_RISKY_ONLY', '0')
    monkeypatch.setattr(app_module, 'run_command', timed_out)
    app = env.app
    large(app)
    chat = app.new_chat(str(env.project))
    env.script = [call('RunCommand', {'command': 'python3 export.py'}, 'run'),
                  final('export.py waits on a stale lock, data/.export.lock, so it never finished; nothing was '
                        'changed.\n\nFINAL: blocked')]
    app.work(chat, 'Run export.py and tell me how many rows it wrote. End with `FINAL: <rows>` or `FINAL: blocked`.',
             False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, rid, 'waiting_approval', 1)
    command = next(a for a in app.store.actions(chat['id']) if a['kind'] == 'command' and a['status'] == 'pending')
    app.approve(chat['id'], command['id'])
    # 1.9.26: the approval raised CommandOutcomeUncertain, the run went to needs_reconciliation and no request followed
    settled(env, rid, 'completed', 2)
    resumed = contents(env.requests[1])
    assert 'timed_out' in resumed and 'waiting for lock data/.export.lock (pid 48213)' in resumed
    assert 'stale lock' in resumed and 'files it was writing may be incomplete' in resumed
    command_receipts = [t for t in app.runs.tools(rid) if t['name'] == 'RunCommand']
    assert [t['state'] for t in command_receipts] == ['completed']  # never 'uncertain'
    assert command_receipts[0]['result']['result']['timed_out'] is True
    assert app.chat(chat['id'])['messages'][-1]['content'].endswith('FINAL: blocked')


# -- the repair turn says to fix the cause ---------------------------------------------------------------------------

def test_the_completion_check_asks_for_the_cause_to_be_fixed_before_a_blocker(env):
    app = env.app
    large(app)
    chat = app.new_chat(str(env.project))
    env.script = [call('Read', {'file_path': 'calc.py'}, 'read'),
                  call('Write', {'file_path': 'calc.py', 'content': CHANGED}, 'write'),
                  final('Documented add().'), final('Done.'), final('Done.')]
    app.work(chat, 'Update calc.py so add documents its behaviour.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, rid, 'verification_required', 5)
    repairs = [m['content'] for m in env.requests[-1] if 'HOST COMPLETION CHECK' in str(m.get('content'))]
    assert len(repairs) == 2
    for repair in repairs:
        assert runner.REPAIR_ADVICE in repair and 'no tests were run' in repair
        assert 'collect the missing evidence. If blocked' not in repair
        assert repair.index('Fix the cause') < repair.index('state that blocker')
    assert runner.REPAIR_ADVICE.startswith('Fix the cause with permitted tools') and json.dumps(runner.REPAIR_ADVICE)
