"""Phase 45 (1.9.7, F107 from audit round R3): a reply stopped while it was being written keeps the words written so
far, said to be unchecked (the person had been reading them in the live draft since F105); other early endings keep
only what was presented, as before."""
from pebble.runner import STOPPED_UNCHECKED, stopped_reply
from pebble.runtime import RunJournal
from pebble.store import Store
from pebble.stream_journal import StreamJournal


def test_the_streamed_text_of_an_attempt_reads_back_in_order(tmp_path):
    store = Store(tmp_path)
    runs = RunJournal(store, tmp_path)
    streams = StreamJournal(store, runs)
    try:
        cid = store.new_chat()
        rid = runs.create(cid, 'goal', {'limits': {'max_steps': 3, 'max_seconds': 30}})
        runs.transition(rid, 'running')
        aid = streams.begin(rid, 1, 1)
        for part in ('Once ', 'upon ', 'a time'):
            streams.append(aid, part)
        assert streams.attempt_text(aid) == 'Once upon a time'
        assert streams.attempt_text(aid, limit=4) == 'time'
        streams.settle(aid, 'interrupted')
        assert streams.attempt_text(aid) == 'Once upon a time', 'a settled attempt still reads back'
    finally:
        runs.close()
        store.close()


def test_a_stopped_reply_keeps_its_draft_said_to_be_unchecked():
    assert stopped_reply('', 'Once upon a time', 'stopped') == 'Once upon a time\n\n' + STOPPED_UNCHECKED
    assert stopped_reply('Earlier part.', ' and then ', 'stopped') == 'Earlier part.\n\nand then\n\n' + STOPPED_UNCHECKED
    assert stopped_reply('Shown.', '', 'stopped') == 'Shown.\n\n*Run stopped.*'
    assert stopped_reply('', '', 'stopped') == ''


def test_other_endings_keep_only_what_was_shown():
    assert stopped_reply('Shown.', 'draft', 'paused') == 'Shown.\n\n*Run paused.*'
    assert stopped_reply('', 'draft', 'budget_exhausted') == ''
