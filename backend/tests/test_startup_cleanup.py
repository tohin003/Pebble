"""Failure cleanup preserves the original error and live-owner storage."""
from types import SimpleNamespace
import sqlite3
import pytest
import pebble.app as app_module
from pebble.runtime import claim_owner


def test_memory_startup_failure_closes_already_opened_store(tmp_path, monkeypatch):
    opened = []
    store_type = app_module.Store

    def store(home):
        result = store_type(home)
        opened.append(result)
        return result

    def fail_memory(store):
        raise RuntimeError('fixture startup failure')

    monkeypatch.setattr(app_module, 'Store', store)
    monkeypatch.setattr(app_module, 'Memory', fail_memory)
    with pytest.raises(RuntimeError, match='fixture startup failure'):
        app_module.App(tmp_path / 'home', tmp_path / 'resources', lambda *a: None)
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].db.execute('SELECT 1')


def test_partial_startup_does_not_close_storage_under_live_owner():
    app = object.__new__(app_module.App)
    calls = []
    app.jobs = SimpleNamespace(close=lambda: calls.append('jobs') or False)
    app.store = SimpleNamespace(close=lambda: calls.append('store'))
    app._abort_startup()
    assert calls == ['jobs']
    assert app.closed is True


def test_cleanup_failure_does_not_replace_original_startup_error(tmp_path, monkeypatch):
    class BrokenStore:
        def close(self):
            raise OSError('fixture cleanup failure')

    monkeypatch.setattr(app_module, 'Store', lambda home: BrokenStore())
    monkeypatch.setattr(app_module, 'Memory', lambda store: (_ for _ in ()).throw(RuntimeError('original')))
    with pytest.raises(RuntimeError, match='original'):
        app_module.App(tmp_path / 'home', tmp_path / 'resources', lambda *a: None)


def test_second_app_is_refused_before_opening_or_migrating_store(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    app = app_module.App(home, tmp_path / 'resources', lambda *a: None)
    opened = []
    monkeypatch.setattr(app_module, 'Store', lambda home: opened.append(home))
    try:
        with pytest.raises(ValueError, match='Another Pebble service'):
            app_module.App(home, tmp_path / 'resources', lambda *a: None)
        assert opened == []
        assert app.store.query('PRAGMA integrity_check') == [{'integrity_check': 'ok'}]
    finally:
        app.close()


def test_failed_startup_releases_early_service_lease(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setattr(app_module, 'Memory', lambda store: (_ for _ in ()).throw(RuntimeError('fixture')))
    with pytest.raises(RuntimeError, match='fixture'):
        app_module.App(home, tmp_path / 'resources', lambda *a: None)
    lease = claim_owner(home)
    lease.close()


def test_service_lease_refuses_symlink_without_modifying_target(tmp_path):
    target = tmp_path / 'target'
    target.write_bytes(b'untouched')
    before = target.stat().st_mode
    (tmp_path / 'runtime-owner.lock').symlink_to(target)
    with pytest.raises(ValueError, match='lease file is unsafe'):
        claim_owner(tmp_path)
    assert target.read_bytes() == b'untouched' and target.stat().st_mode == before


def test_completion_failure_closes_unstarted_controller_and_releases_owner(tmp_path, monkeypatch):
    from pebble.migrations import Upgrade
    original = Upgrade.complete

    def fail_completion(self):
        raise RuntimeError('fixture completion failure')

    home = tmp_path / 'home'
    monkeypatch.setattr(Upgrade, 'complete', fail_completion)
    with pytest.raises(RuntimeError, match='fixture completion failure'):
        app_module.App(home, tmp_path / 'resources', lambda *a: None)
    lease = claim_owner(home)
    lease.close()
    monkeypatch.setattr(Upgrade, 'complete', original)
    recovered = app_module.App(home, tmp_path / 'resources', lambda *a: None)
    assert recovered.upgrade_receipt['completed'] is True
    recovered.close()
