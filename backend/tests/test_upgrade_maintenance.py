"""Startup maintenance for a full upgrade-backup budget (Phase 14, found by W7).

A home that already retains the maximum number of upgrade backups cannot
upgrade. Before this, the backend exited and the app showed "offline". Now it
serves only the maintenance routes. A backup is removed only by its id and the
manifest hash the user was shown, and startup is retried in place.
"""
import functools

import pytest

from pebble import migrations
from pebble.maintenance import Maintenance, REMEDIABLE
from pebble.migrations import Limits, MigrationRefused
from pebble.runtime import claim_owner
from test_migrations import boot, entries, legacy_home


@pytest.fixture
def full_budget(tmp_path, monkeypatch):
    """A home at the current epoch with one retained backup, then a later build that needs another upgrade."""
    home = legacy_home(tmp_path)
    boot(home).close()
    monkeypatch.setattr(migrations, 'SCHEMA_EPOCH', migrations.SCHEMA_EPOCH + 1)
    monkeypatch.setattr(migrations, 'Limits', functools.partial(Limits, max_backups=1))
    return home


def start_for(home, calls):
    """A stand-in for App(): the real upgrade check, then a stub service."""
    def start():
        calls.append('start')
        migrations.prepare(home).abort()
        return Stub()
    return start


class Stub:
    closed = False

    def dispatch(self, method, params):
        return {'served_by': 'app', 'method': method}

    def close(self):
        self.closed = True


def refusal(home):
    with pytest.raises(MigrationRefused) as caught:
        migrations.prepare(home)
    return caught.value


def test_remediable_refusal_serves_only_maintenance_routes_and_holds_the_lease(full_budget):
    exc = refusal(full_budget)
    assert exc.reason == 'backup_budget_exhausted' and exc.reason in REMEDIABLE
    events, calls = [], []
    service = Maintenance(full_budget, exc, start_for(full_budget, calls), lambda *event: events.append(event))
    try:
        view = service.dispatch('state', {})['maintenance']
        assert view['reason'] == 'backup_budget_exhausted' and view['authorization'] == 'none'
        [backup] = view['backups']
        assert backup['manifest_sha256'] and backup['state'] != 'pending' and backup['bytes'] > 0
        assert service.dispatch('upgrade_backups', {}) == view
        for method in ('get_chat', 'send', 'setting', 'approve', 'memory_export'):
            with pytest.raises(ValueError, match='waiting for an upgrade step'):
                service.dispatch(method, {'id': 'x'})
        with pytest.raises(ValueError, match='Another Pebble service'):
            claim_owner(full_budget)  # no second backend can start while maintenance waits
        assert calls == [] and events == []
    finally:
        service.close()


def test_a_backup_is_removed_only_by_its_id_and_the_manifest_hash_shown(full_budget):
    service = Maintenance(full_budget, refusal(full_budget), start_for(full_budget, []), lambda *event: None)
    try:
        [backup] = service.view()['backups']
        before = entries(full_budget)
        for params in ({'backup_id': backup['id']}, {'backup_id': backup['id'], 'manifest_sha256': backup['manifest_sha256'], 'force': True}):
            with pytest.raises(ValueError, match='manifest hash'):
                service.dispatch('upgrade_discard_backup', params)
        with pytest.raises(MigrationRefused) as caught:
            service.dispatch('upgrade_discard_backup', {'backup_id': backup['id'], 'manifest_sha256': '0' * 64})
        assert caught.value.reason == 'backup_tampered' and entries(full_budget) == before
        result = service.dispatch('upgrade_discard_backup', {'backup_id': backup['id'], 'manifest_sha256': backup['manifest_sha256']})
        assert result['discarded'] == backup['id'] and result['maintenance']['backups'] == []
        assert entries(full_budget) == []
    finally:
        service.close()


def test_retry_stays_in_maintenance_until_startup_succeeds_then_hands_over(full_budget):
    events, calls = [], []
    service = Maintenance(full_budget, refusal(full_budget), start_for(full_budget, calls), lambda *event: events.append(event))
    with pytest.raises(ValueError, match='Retry takes no options'):
        service.dispatch('upgrade_retry', {'force': True})
    still = service.dispatch('upgrade_retry', {})
    assert still['ready'] is False and still['maintenance']['reason'] == 'backup_budget_exhausted'
    assert calls == ['start'] and events == []
    with pytest.raises(ValueError, match='Another Pebble service'):
        claim_owner(full_budget)  # the lease is held again after a refused retry
    [backup] = still['maintenance']['backups']
    service.dispatch('upgrade_discard_backup', {'backup_id': backup['id'], 'manifest_sha256': backup['manifest_sha256']})
    assert service.dispatch('upgrade_retry', {}) == {'ready': True}
    assert calls == ['start', 'start'] and events == [('ready', {})]
    assert service.dispatch('get_chat', {'id': 'x'}) == {'served_by': 'app', 'method': 'get_chat'}
    app = service.app
    service.close()
    assert app.closed is True
    claim_owner(full_budget).close()  # maintenance released its lease when the service started


@pytest.mark.parametrize('reason', ['future_version', 'backup_tampered', 'insufficient_space', 'preservation_failed'])
def test_other_refusals_still_stop_startup(tmp_path, reason):
    home = legacy_home(tmp_path)
    with pytest.raises(MigrationRefused) as caught:
        Maintenance(home, MigrationRefused(reason), lambda: Stub(), lambda *event: None)
    assert caught.value.reason == reason
    claim_owner(home).close()  # nothing was left holding the lease


def test_a_failed_retry_keeps_maintenance_waiting_with_the_lease(full_budget):
    def broken():
        raise RuntimeError('startup failed')
    service = Maintenance(full_budget, refusal(full_budget), broken, lambda *event: None)
    try:
        with pytest.raises(RuntimeError, match='startup failed'):
            service.dispatch('upgrade_retry', {})
        assert service.app is None and service.dispatch('state', {})['maintenance']['reason'] == 'backup_budget_exhausted'
        with pytest.raises(ValueError, match='Another Pebble service'):
            claim_owner(full_budget)
    finally:
        service.close()
