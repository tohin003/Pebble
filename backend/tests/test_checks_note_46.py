"""Phase 46 (D163, D164): above 8K a run is told up front the completion checks it can do itself (requirements.checks_note).

Measured first: on golden dev d-lh-01 (gpt-6.1-sol and Opus, 3 of 3 runs) the request's output check ("the output of
`python3 -m shop.cli samples/order1.json` must stay byte-identical to samples/invoice1.txt") was met only after the
completion gate's repair prompt: the run ended with its tests passing, the gate held it, and the repair ran the command
(2-3 requests, about 14 s on sol). The gate is unchanged; the run now hears of the check before its final reply.
"""
import json
import time

import pytest

from pebble import requirements
from test_approval_handoff_22 import env, final  # noqa: F401 (fixture)

LONG = ('Migrate the shop to integer cents. The output of `python3 -m shop.cli samples/order1.json` must stay '
        'byte-identical to samples/invoice1.txt. Update the existing tests and make the whole suite pass.')


def test_the_checks_a_run_can_do_itself_are_named_in_the_requests_own_words():
    note = requirements.checks_note(requirements.extract(LONG))
    assert note.startswith(requirements.CHECKS_NOTE)
    assert ('- run `python3 -m shop.cli samples/order1.json` with RunCommand after your last change: its output must be '
            'byte-identical to samples/invoice1.txt.') in note
    assert "- the project's whole test suite must pass on the current files (RunTests)." in note


def test_checks_the_gate_words_itself_are_not_listed():
    for goal in ("Don't change anything; which file defines reconcile_ledger?", "Don't edit the tests; fix total().",
                 'Delete the generated files that can be rebuilt.', 'Fix total() in pricing.py.'):
        assert requirements.checks_note(requirements.extract(goal)) is None, goal


@pytest.mark.parametrize('context, told', [(32768, True), (8192, False)])
def test_a_run_above_8k_is_told_and_8k_runs_are_unchanged(env, context, told):
    text = ('The output of `python3 calc.py` must stay byte-identical to out.txt. Which function in calc.py adds two '
            'numbers? End with `FINAL: <path>:<name>`.')
    app = env.start(context)
    chat = app.new_chat(str(env.project))
    env.script = [final('add.\n\nFINAL: calc.py:add')] * 6  # the gate's repairs ask again: out.txt is never produced
    app.work(chat, text, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and (app.active is not None or app.runs.get(rid)['state'] in ('queued', 'running')):
        time.sleep(.02)
    shown = [m for m in env.requests[0] if m.get('name') == 'pebble_checks']
    assert bool(shown) is told
    if told:
        assert shown[0]['role'] == 'user' and '`python3 calc.py` with RunCommand' in shown[0]['content']
    noted = app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='requirements'", (rid,))
    assert json.loads(noted[0]['payload'])['requirements'][0]['kind'] == 'output_equals'
