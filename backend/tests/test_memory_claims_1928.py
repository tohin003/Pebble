"""Phase 46 (1.9.28, F6): a reply that says something was saved for later sessions when the run saved nothing is
withheld once and repaired; never a hold.

Measured first: no check covered "Got it, I'll remember that" or "Saved for future sessions" when nothing was saved
(the runner checked forget claims, V1-034, and Toolbox keeps, F147). Such a first session is what precedes the memory
family's false completions: the next session answers without the preference. Above 4K, a request whose own words ask to
keep something (memory.asks_to_remember; follow-up 1: never pasted or quoted words, test_memory_security_1928's E3) and a
run that saved nothing of it (no host capture, no promoted MemoryPropose
candidate, no preference sentence for the end-of-run collection) get the claim withheld once: the model saves the
person's words with MemoryPropose, or says plainly they were not saved. A claim that comes back gets a host line above
the reply, which keeps its last line. One extra request, only after a false claim.
"""

import pytest

from pebble import runner
from pebble.memory import reply_claims_saving
from test_memory_capture_1928 import FINAL, run_session

CLAIM = "Got it — I'll remember that you want dates as DD.MM.YYYY.\n\nFINAL: done"
NOT_CUED = 'Next time you give me a date, use DD.MM.YYYY.' + FINAL  # the host saves nothing of it


@pytest.mark.parametrize('text', [
    "Got it — I'll remember that you write dates as DD.MM.YYYY.", "I've saved both preferences for future sessions.",
    'Noted for future sessions: dates as DD.MM.YYYY.', 'Saved to memory.', 'Both are now saved.',
    "I'll keep that in mind.", 'This will be applied in future sessions.', 'I have stored your preference in memory.',
    "Understood, I'll use that for future requests."])
def test_claims_of_a_save_are_recognised(text):
    assert reply_claims_saving(text)


@pytest.mark.parametrize('text', [
    "Don't forget to run the tests.", "I can't save memories myself; say 'Remember that ...'.",
    "I couldn't save it to memory.", 'Saved the report to reports/summary.md.', 'I noted the failing test in NOTES.md.',
    'The memory module stores sessions in memory.db.', 'I added a test for future dates.',
    'Nothing was saved for later sessions.', "I'll remember to run the tests next time? No."])
def test_negations_file_notes_and_reminders_are_not_claims(text):
    assert not reply_claims_saving(text)


def test_an_unbacked_claim_is_withheld_once_and_the_repair_saves_the_persons_words(tmp_path):
    result = run_session(tmp_path / 'home', NOT_CUED, script=[
        CLAIM, ('MemoryPropose', {'kind': 'profile', 'content': 'Next time you give me a date, use DD.MM.YYYY.'}),
        'Saved: I will use DD.MM.YYYY next time.\n\nFINAL: done'])
    assert len(result.requests) == 3 and result.run['state'] == 'completed'
    check = [m['content'] for m in result.requests[1][0] if str(m.get('content', '')).startswith('HOST CHECK: Pebble saved')]
    assert check and runner.REMEMBER_SAVE in check[0]  # MemoryPropose is offered: it may save the words
    assert result.answer.startswith('> Saved “Next time you give me a date, use DD.MM.YYYY.” for later sessions.')
    assert "I'll remember" not in result.answer and result.answer.rstrip().splitlines()[-1] == 'FINAL: done'
    assert runner.SAVED_NOTHING not in result.answer


def test_a_claim_that_comes_back_is_shown_with_a_host_line_never_held(tmp_path):
    result = run_session(tmp_path / 'home', NOT_CUED, script=[CLAIM, CLAIM])
    assert len(result.requests) == 2 and result.run['state'] == 'completed'
    assert result.answer.startswith('> ' + runner.SAVED_NOTHING)
    assert result.answer.rstrip().splitlines()[-1] == 'FINAL: done'


@pytest.mark.parametrize('text', [
    'Remember that I prefer tabs.' + FINAL,  # the host saved it: the claim is true
    'From now on, always write dates as DD.MM.YYYY.' + FINAL,  # kept by the host's end-of-run collection
    'Summarise README.md in one line.' + FINAL,  # nothing asked to keep: no memory claim to check
])
def test_a_backed_claim_or_one_outside_a_request_to_remember_costs_no_request(tmp_path, text):
    result = run_session(tmp_path / 'home', text, script=["I'll remember that, and I've saved it for future "
                                                          "sessions.\n\nFINAL: done"])
    assert len(result.requests) == 1 and result.run['state'] == 'completed'


def test_with_memory_off_the_repair_only_asks_for_a_plain_answer(tmp_path):
    result = run_session(tmp_path / 'home', NOT_CUED, memory_on=False, script=[
        CLAIM, 'Memory is off, so this was not saved.\n\nFINAL: done'])
    assert len(result.requests) == 2
    [check] = [m['content'] for m in result.requests[1][0] if str(m.get('content', '')).startswith('HOST CHECK: Pebble')]
    assert runner.REMEMBER_SAVE not in check and runner.REMEMBER_SAY in check
