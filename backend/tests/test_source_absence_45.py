"""Phase 45 (1.9.5+, F117 from audit round R3): a Read that finds a file gone is the run's latest view of it, so a file
the run deleted (at the person's request, then confirmed gone by a fresh read) no longer holds completion back as
"changed or deleted afterwards"; a file deleted with no fresh read still does."""
import pytest

from test_provenance import owned, read  # noqa: F401 (owned is a fixture)


def deleted_count(app, rid, ws):
    return app.source_audit(rid, ws)['counts']['deleted']


def test_a_fresh_read_that_finds_the_file_gone_settles_it(owned):
    app, chat, rid, ws = owned
    target = ws.root / 'a.txt'
    target.write_text('old')
    read(owned)
    target.unlink()
    assert deleted_count(app, rid, ws) == 1, 'deleted after the read and not read again: still stale'
    with pytest.raises(FileNotFoundError):
        read(owned, ident='again')
    assert deleted_count(app, rid, ws) == 0
    assert app.source_audit(rid, ws)['counts']['unchanged'] == 1
    latest = app.provenance.latest(rid)
    assert [(r['path'], r['disk_hash'], r['status']) for r in latest] == [('a.txt', None, 'deleted')]


def test_a_file_that_comes_back_is_stale_again(owned):
    app, chat, rid, ws = owned
    target = ws.root / 'a.txt'
    target.write_text('old')
    read(owned)
    target.unlink()
    with pytest.raises(FileNotFoundError):
        read(owned, ident='again')
    target.write_text('someone wrote it again')
    assert app.source_audit(rid, ws)['counts']['changed'] == 1


def test_absence_needs_the_owning_read(owned):
    app, chat, rid, ws = owned
    with pytest.raises(Exception):
        app.provenance.observe_absent(rid, 'no-such-call', ws, 'gone.txt')
