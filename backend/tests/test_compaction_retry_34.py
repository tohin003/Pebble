"""Phase 34: the compaction race of Phase 33 trial 1 (.runtime/phase33-trial1/trial1-preserved.json), reproduced and fixed.

validate_compaction (frozen, pooled under CPU load) once failed its run with "The model checkpoint changed during
compaction. Nothing was retired." The mechanism, read from the code and reproduced here:

- recovery.complete_with_retry regenerates a failed attempt from the same step messages, the pre-compaction ones;
- so the retry's preflight compacts them again, and the new evidence spine carries the time left *now*;
- RunJournal.compact_checkpoint treats a retry as a duplicate only when both fingerprints equal the committed
  generation's, so a retry in a later second (likelier under load) is not a duplicate, and its "before" no longer
  matches the committed checkpoint: the run failed.

The fix (App._committed_compaction): when the messages are exactly the newest compaction's input and the checkpoint is
still its output, the retry sends what was committed. The checked-in App, journal, provider projection and compaction
run unchanged; only the time ledger's reading is set by the test.
"""
import pytest

from pebble.app import App
from pebble.compaction import fingerprint, is_spine
from test_compaction import count, owned, window  # noqa: F401  (owned is a fixture)


def clock(app, monkeypatch, *readings):
    values = list(readings)
    monkeypatch.setattr(app.time_ledger, 'remaining', lambda rid: values.pop(0) if len(values) > 1 else values[0])


def spine(messages):
    return next(m for m in messages if is_spine(m))


def test_without_the_fix_a_retry_in_a_later_second_fails_the_run(owned, monkeypatch):
    app, _, rid, execution = owned
    monkeypatch.setattr(App, '_committed_compaction', lambda self, *args: None)
    clock(app, monkeypatch, 120.4, 118.9)
    app.model.project_context(window(), [], 900, counter=count, method='fixture')
    assert execution.compaction_generation == 1
    with pytest.raises(ValueError, match='The model checkpoint changed during compaction'):
        app.model.project_context(window(), [], 900, counter=count, method='fixture')


def test_a_retry_in_the_same_second_was_already_a_duplicate(owned, monkeypatch):
    app, _, rid, execution = owned
    monkeypatch.setattr(App, '_committed_compaction', lambda self, *args: None)
    clock(app, monkeypatch, 120.4, 120.1)
    first = app.model.project_context(window(), [], 900, counter=count, method='fixture')
    assert app.model.project_context(window(), [], 900, counter=count, method='fixture') == first
    assert len(app.runs.compactions(rid)) == 1


def test_a_retry_sends_the_committed_generation(owned, monkeypatch):
    app, _, rid, execution = owned
    clock(app, monkeypatch, 120.4, 118.9)
    first = app.model.project_context(window(), [], 900, counter=count, method='fixture')
    committed = app.runs.get(rid)['checkpoint']['messages']
    again = app.model.project_context(window(), [], 900, counter=count, method='fixture')
    # The same wire, the same generation and archive, and the checkpoint is untouched.
    assert again == first and execution.compaction_generation == 1
    assert len(app.runs.compactions(rid)) == 1 and app.runs.get(rid)['checkpoint']['messages'] == committed
    assert execution.projected_messages == committed
    assert [e['payload'] for e in app.runs.events(rid) if e['kind'] == 'compaction_reused'] == [{'generation': 1}]


def test_other_messages_still_compact_as_before(owned, monkeypatch):
    """A later step (new messages) is not a retry: it compacts against the checkpoint it extends, and a changed
    checkpoint still refuses to retire anything."""
    app, _, rid, execution = owned
    clock(app, monkeypatch, 120.4)
    app.model.project_context(window(), [], 900, counter=count, method='fixture')
    later = window()
    later[2]['content'] += ' one more constraint'
    assert app._committed_compaction(execution, later, [], 900, 0, count, 'fixture') is None
    with pytest.raises(ValueError, match='changed during compaction'):
        app.model.project_context(later, [], 900, counter=count, method='fixture')


def test_a_checkpoint_that_moved_on_is_not_reused(owned, monkeypatch):
    app, _, rid, execution = owned
    clock(app, monkeypatch, 120.4)
    app.model.project_context(window(), [], 900, counter=count, method='fixture')
    snapshot = app.runs.get(rid)['checkpoint']
    moved = [*snapshot['messages'], {'role': 'user', 'content': 'a later host note'}]
    app.runs.checkpoint(rid, {**snapshot, 'messages': moved}, steps=3, elapsed=2)
    assert fingerprint(moved) != fingerprint(snapshot['messages'])
    assert app._committed_compaction(execution, window(), [], 900, 0, count, 'fixture') is None
