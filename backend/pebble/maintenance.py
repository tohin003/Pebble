"""Startup maintenance for upgrade refusals the user can resolve.

When startup refuses an upgrade for a reason the user can fix (today only a
full backup budget), the backend serves this instead of exiting. The main window
can list the retained upgrade backups, remove one it names with the manifest
hash it was shown, and retry startup in place. Nothing here deletes a backup
implicitly. While it waits, it holds the service lease, so no second backend
can start, and no other route exists until startup succeeds.
"""

import threading

from . import migrations
from .runtime import claim_owner

REMEDIABLE = frozenset({'backup_budget_exhausted'})
METHODS = frozenset({'state', 'upgrade_backups', 'upgrade_discard_backup', 'upgrade_retry'})


class Maintenance:
    def __init__(self, home, refusal, start, emit):
        if refusal.reason not in REMEDIABLE:
            raise refusal
        self.home, self.refusal, self.start, self.emit = home, refusal, start, emit
        self.app = None
        self.lock = threading.Lock()
        self.lease = claim_owner(home)

    def dispatch(self, method, params):
        if self.app is not None:
            return self.app.dispatch(method, params)
        with self.lock:
            if self.app is not None:
                return self.app.dispatch(method, params)
            if method not in METHODS:
                raise ValueError('Pebble is waiting for an upgrade step. Open Pebble to finish it.')
            if method == 'state':
                return {'maintenance': self.view()}
            if method == 'upgrade_backups':
                return self.view()
            if method == 'upgrade_discard_backup':
                if set(params) != {'backup_id', 'manifest_sha256'}:
                    raise ValueError('Name the backup and the manifest hash shown for it.')
                result = migrations.discard_backup(self.home, params['backup_id'], params['manifest_sha256'])
                return {**result, 'maintenance': self.view()}
            if params:
                raise ValueError('Retry takes no options.')
            return self.retry()

    def view(self):
        report = migrations.inspect(self.home)
        backups = [{key: item.get(key) for key in ('id', 'state', 'from_version', 'to_version', 'created', 'bytes',
                                                    'manifest_sha256')}
                   for item in report.get('backups', [])]
        return {'reason': self.refusal.reason, 'message': str(self.refusal), 'backups': backups,
                'budget': report.get('budget'), 'authorization': 'none'}

    def retry(self):
        """Release the lease and start the real service; stay in maintenance if it is still refused."""
        lease, self.lease = self.lease, None
        if lease is not None:
            lease.close()
        try:
            app = self.start()
        except migrations.MigrationRefused as exc:
            self.lease = claim_owner(self.home)
            if exc.reason not in REMEDIABLE:
                raise
            self.refusal = exc
            return {'ready': False, 'maintenance': self.view()}
        except BaseException:
            self.lease = claim_owner(self.home)  # keep waiting in maintenance; no second backend may start
            raise
        self.app = app
        self.emit('ready', {})
        return {'ready': True}

    def close(self):
        if self.app is not None and hasattr(self.app, 'close'):
            self.app.close()
        if self.lease is not None:
            self.lease.close()
            self.lease = None
