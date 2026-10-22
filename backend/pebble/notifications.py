"""Native notification outbox, scoped settings and host notices (Phase 17, D06).

job_notifications (jobs.py) stays the durable in-app outbox. This module adds:

- native_deliveries: one row per job notice, written in the notice's own
  transaction, recording whether it reached an operating-system banner
  (pending, delivered, suppressed or failed) and why.
- native_banners: what the shell posts. A banner is one notice (its identifier
  is the notice id), several notices of one job within 60 seconds ("3 updates"),
  or one summary: after quiet hours, or once three banners were shown within ten
  minutes. Every banner has a version. The shell posts (id, version) only when
  getDeliveredNotifications does not already show that version, then
  acknowledges it, so a crash between post and acknowledgement never shows two
  banners. Pausing or deleting a job, or acknowledging every notice a banner
  stands for, withdraws it, and the shell removes it.
- host_notices: quiet, host-authored informational notices (wake summaries,
  late skips, DST-skipped days, clock changes). They are in-app only.

Settings are global on/off (off until the person enables it in Settings, which
is also when the shell asks for authorization), per kind, per-job mute, quiet
hours that defer and summarize but never drop, and a content preview that
defaults to title only, because notice bodies are run output. A shown body
loses control, format and private-use characters and URLs and is cut to 140
characters. Banner titles are host-authored. The only action is Open, which
opens the conversation; notifications never offer Approve. Nothing here sends
e-mail or network traffic, and no route here is reachable from a model tool.
"""

import re
import time
import unicodedata
import uuid
from datetime import datetime

from .jobs import HEX, HHMM, MAX_JOBS, ident, iso, time_zone


SETTING, MUTES = 'notifications', 'notification_mutes'
CATEGORIES = ('completed', 'failed', 'action_needed', 'monitor_changed')
PREVIEWS = ('none', 'title', 'body')
DEFAULT_SETTINGS = {'enabled': False, 'kinds': dict.fromkeys(CATEGORIES, True), 'preview': 'title', 'quiet_hours': None}
HOST_KINDS = ('wake_summary', 'late_skipped', 'dst_skipped', 'clock_changed')
FAILURES = ('not_authorized', 'denied', 'unavailable', 'error')
ACTIONS = ('open',)  # never approve, reject or reply
SAME_JOB_WINDOW = 60.0
RATE_WINDOW, RATE_LIMIT = 600.0, 3
STALE_AFTER = 86400.0  # an undelivered banner older than this is noise; quiet-hour deferrals are exempt
MAX_ATTEMPTS = 8  # pulls without an acknowledgement before a post or removal is given up
BODY_LIMIT, TITLE_LIMIT = 140, 120
MAX_BANNERS, MAX_HOST_NOTICES, MAX_ACK = 500, 1000, 200
NOTIFY_DDL = '''
CREATE TABLE IF NOT EXISTS native_deliveries(
  notification_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, chat_id TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN ('completed','failed','action_needed','monitor_changed')),
  state TEXT NOT NULL CHECK(state IN ('pending','delivered','suppressed','failed')),
  reason TEXT, banner_id TEXT, banner_version INTEGER, attempts INTEGER NOT NULL,
  created REAL NOT NULL, posted REAL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS native_deliveries_state ON native_deliveries(state,created);
CREATE INDEX IF NOT EXISTS native_deliveries_banner ON native_deliveries(banner_id);
CREATE TABLE IF NOT EXISTS native_banners(
  id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK(scope IN ('job','quiet_summary','rate_summary')),
  job_id TEXT, chat_id TEXT, members INTEGER NOT NULL, version INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('posting','delivered','withdrawn','removed','failed')),
  delivered_version INTEGER, attempts INTEGER NOT NULL, reason TEXT,
  opened REAL NOT NULL, posted REAL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS native_banners_state ON native_banners(state,opened);
CREATE TABLE IF NOT EXISTS host_notices(
  id TEXT PRIMARY KEY, chat_id TEXT, job_id TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('wake_summary','late_skipped','dst_skipped','clock_changed')),
  fingerprint TEXT NOT NULL UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL,
  created REAL NOT NULL, acknowledged REAL);
CREATE INDEX IF NOT EXISTS host_notices_pending ON host_notices(acknowledged,created);
'''
URL = re.compile(r'(?i)\b[a-z][a-z0-9+.-]{1,31}://\S*|\b(?:mailto|javascript|vbscript|data|file|tel|sms):\S*'
                 r'|\bwww\.\S+|\b(?:[a-z0-9-]+\.)+[a-z]{2,24}/\S*')


def plain(text, limit):
    """One line of display text: no control, format, private-use or unassigned characters, and no URLs."""
    text = ''.join(c for c in str(text or '') if unicodedata.category(c) not in ('Cf', 'Cs', 'Co', 'Cn'))
    text = ''.join(' ' if unicodedata.category(c) in ('Cc', 'Zl', 'Zp') else c for c in text)
    text = ' '.join(URL.sub('[link removed]', text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def minutes(clock):
    return int(clock[:2]) * 60 + int(clock[3:])


def quiet(moment, hours):
    """True when `moment` falls inside the quiet hours, evaluated in their own zone."""
    if not hours:
        return False
    local = datetime.fromtimestamp(moment, time_zone(hours['timezone']))
    now, start, end = local.hour * 60 + local.minute, minutes(hours['start']), minutes(hours['end'])
    return start <= now < end if start < end else now >= start or now < end


def validate_settings(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_SETTINGS):
        raise ValueError('Notification settings need exactly enabled, kinds, preview and quiet_hours.')
    if type(value['enabled']) is not bool:
        raise ValueError('enabled must be true or false.')
    kinds = value['kinds']
    if not isinstance(kinds, dict) or set(kinds) != set(CATEGORIES) or any(type(v) is not bool for v in kinds.values()):
        raise ValueError('Give one true or false for each kind: ' + ', '.join(CATEGORIES) + '.')
    if not isinstance(value['preview'], str) or value['preview'] not in PREVIEWS:
        raise ValueError('Choose a content preview of none, title or body.')
    hours = value['quiet_hours']
    if hours is not None:
        if not isinstance(hours, dict) or set(hours) != {'start', 'end', 'timezone'}:
            raise ValueError('Quiet hours need exactly start, end and timezone, or null.')
        if not all(isinstance(hours[key], str) and HHMM.fullmatch(hours[key]) for key in ('start', 'end')):
            raise ValueError('Give quiet hours as HH:MM in 24-hour time.')
        if hours['start'] == hours['end']:
            raise ValueError('Quiet hours need different start and end times.')
        time_zone(hours['timezone'])
        hours = {key: hours[key] for key in ('start', 'end', 'timezone')}
    return {'enabled': value['enabled'], 'kinds': {key: kinds[key] for key in CATEGORIES},
            'preview': value['preview'], 'quiet_hours': hours}


def host_view(row):
    return {'id': row['id'], 'job_id': row['job_id'], 'fire_id': None, 'chat_id': row['chat_id'], 'run_id': None,
            'kind': row['kind'], 'status': row['kind'], 'title': row['title'], 'body': row['body'],
            'body_origin': 'host', 'source': 'host', 'created': iso(row['created']),
            'acknowledged': iso(row['acknowledged'])}


def _rows(db, sql, args=()):
    cursor = db.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _one(db, sql, args=()):
    rows = _rows(db, sql, args)
    return rows[0] if rows else None


def _ids(value, label):
    if not isinstance(value, list) or len(value) > MAX_ACK:
        raise ValueError(f'Give {label} as a list of at most {MAX_ACK} entries.')
    return value


class Notifications:
    """Outbox decisions. Every method takes Store.lock only, inside any Jobs.lock its caller holds."""

    def __init__(self, app, clock=None):
        self.app, self.store = app, app.store
        self.clock = clock or time.time
        with self.store.lock:
            self.store.db.executescript(NOTIFY_DDL)

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not value >= 0 or value == float('inf'):
            raise ValueError('The notification clock returned an invalid time.')
        return float(value)

    def changed(self):
        """Wake the shell's outbox pump; the payload carries nothing."""
        try:
            self.app.emit('notification_outbox', {})
        except Exception:
            pass

    # Settings

    def settings(self):
        try:
            return validate_settings(self.store.setting(SETTING, DEFAULT_SETTINGS))
        except (TypeError, ValueError):
            return validate_settings(DEFAULT_SETTINGS)  # an unreadable stored value fails closed: off

    def save_settings(self, value):
        """The parent calls this from App.setting('notifications', value)."""
        normalized = validate_settings(value)
        self.store.set_setting(SETTING, normalized)
        self.changed()
        return normalized

    def mutes(self):
        value = self.store.setting(MUTES, [])
        return [v for v in value if isinstance(v, str) and HEX.fullmatch(v)] if isinstance(value, list) else []

    def set_mute(self, job_id, muted):
        ident(job_id, 'Job not found.')
        if type(muted) is not bool:
            raise ValueError('muted must be true or false.')
        with self.store.lock:
            existing = {row['id'] for row in self.store.query('SELECT id FROM jobs')}
            if job_id not in existing:
                raise ValueError('Job not found.')
            kept = [value for value in self.mutes() if value in existing and value != job_id]
            self.store.set_setting(MUTES, (kept + [job_id] if muted else kept)[:MAX_JOBS])
        self.changed()
        return {'job_id': job_id, 'muted': muted}

    # In-transaction hooks called by Jobs

    def enqueue(self, db, notice, category, now):
        category = category if category in CATEGORIES else notice['kind']
        db.execute('INSERT OR IGNORE INTO native_deliveries(notification_id,job_id,chat_id,category,state,reason,'
                   'banner_id,banner_version,attempts,created,posted,updated) '
                   "VALUES(?,?,?,?,'pending',NULL,NULL,NULL,0,?,NULL,?)",
                   (notice['id'], notice['job_id'], notice['chat_id'], category, now, now))

    def host_notice(self, db, kind, chat_id, job_id, fingerprint, title, body, now):
        """Once per fingerprint; a repeat is ignored. Text is host-authored."""
        if kind not in HOST_KINDS:
            raise ValueError('Unknown host notice kind.')
        row = {'id': uuid.uuid4().hex, 'chat_id': chat_id, 'job_id': job_id, 'kind': kind, 'fingerprint': fingerprint,
               'title': plain(title, 200), 'body': plain(body, 600), 'created': now, 'acknowledged': None}
        cursor = db.execute(f'INSERT OR IGNORE INTO host_notices({",".join(row)}) VALUES({",".join("?" * len(row))})',
                            tuple(row.values()))
        if cursor.rowcount != 1:
            return None
        excess = db.execute('SELECT count(*) FROM host_notices').fetchone()[0] - MAX_HOST_NOTICES
        if excess > 0:  # acknowledged first, then the oldest informational notices
            db.execute('DELETE FROM host_notices WHERE id IN (SELECT id FROM host_notices '
                       'ORDER BY acknowledged IS NULL,created LIMIT ?)', (excess,))
        return host_view(row)

    def host_list(self, chat_id, limit=200):
        """Pending host notices for a conversation plus global ones (wake and clock summaries)."""
        rows = self.store.query('SELECT * FROM host_notices WHERE acknowledged IS NULL AND (chat_id=? OR chat_id IS NULL) '
                                'ORDER BY created DESC,rowid DESC LIMIT ?', (chat_id, limit))
        return [host_view(row) for row in rows]

    def host_ack(self, chat_id, notice_id):
        """None when the id is not a host notice visible from this conversation."""
        if not isinstance(notice_id, str) or not HEX.fullmatch(notice_id):
            return None
        now = self._now()
        with self.store.lock, self.store.db as db:
            if not db.execute('SELECT 1 FROM host_notices WHERE id=? AND (chat_id=? OR chat_id IS NULL)',
                              (notice_id, chat_id)).fetchone():
                return None
            changed = db.execute('UPDATE host_notices SET acknowledged=? WHERE id=? AND acknowledged IS NULL',
                                 (now, notice_id)).rowcount
        return {'acknowledged': changed, 'already_acknowledged': 1 - changed}

    # Native outbox: Rust-only routes

    def native_pending(self):
        """What the shell must post and remove now. Repeating the call before an acknowledgement repeats the same posts."""
        now, settings, mutes = self._now(), self.settings(), set(self.mutes())
        posts, removes = [], []
        with self.store.lock, self.store.db as db:
            self._reconcile(db, now, settings, mutes)
            hushed = quiet(now, settings['quiet_hours'])
            if settings['enabled'] and not hushed:
                self._assign(db, now, settings)
                for banner in _rows(db, "SELECT * FROM native_banners WHERE state='posting' ORDER BY opened,rowid"):
                    if banner['attempts'] >= MAX_ATTEMPTS:
                        self._fail(db, banner['id'], 'no_acknowledgement', now)
                        continue
                    db.execute('UPDATE native_banners SET attempts=attempts+1,updated=? WHERE id=?', (now, banner['id']))
                    posts.append(self._post(db, banner, settings))
            for banner in _rows(db, "SELECT id,attempts FROM native_banners WHERE state='withdrawn' ORDER BY updated,rowid"):
                if banner['attempts'] >= MAX_ATTEMPTS:
                    db.execute("UPDATE native_banners SET state='failed',reason='removal_unconfirmed',updated=? "
                               'WHERE id=?', (now, banner['id']))
                    continue
                db.execute('UPDATE native_banners SET attempts=attempts+1,updated=? WHERE id=?', (now, banner['id']))
                removes.append(banner['id'])
            self._prune(db)
        return {'post': posts, 'remove': removes, 'quiet_hours': hushed, 'enabled': settings['enabled']}

    def native_delivered(self, delivered=None, removed=None, failed=None):
        """Shell acknowledgement. Unknown, stale and repeated entries change nothing."""
        delivered, removed, failed = (_ids([] if v is None else v, label) for v, label in
                                      ((delivered, 'delivered'), (removed, 'removed'), (failed, 'failed')))
        for item in delivered:
            if (not isinstance(item, dict) or set(item) != {'id', 'version'} or not isinstance(item['id'], str)
                    or not HEX.fullmatch(item['id']) or type(item['version']) is not int or item['version'] < 1):
                raise ValueError('Acknowledge a delivery as {id, version}.')
        for item in removed:
            ident(item, 'Acknowledge a removal by banner id.')
        for item in failed:
            if (not isinstance(item, dict) or set(item) != {'id', 'reason'} or not isinstance(item['id'], str)
                    or not HEX.fullmatch(item['id']) or item['reason'] not in FAILURES):
                raise ValueError('Report a failure as {id, reason} with reason ' + ', '.join(FAILURES) + '.')
        now, counts = self._now(), dict.fromkeys(('delivered', 'removed', 'failed', 'ignored'), 0)
        with self.store.lock, self.store.db as db:
            for item in delivered:
                banner = _one(db, 'SELECT * FROM native_banners WHERE id=?', (item['id'],))
                if (not banner or banner['state'] not in ('posting', 'delivered') or item['version'] > banner['version']
                        or (banner['delivered_version'] or 0) >= item['version']):
                    counts['ignored'] += 1
                    continue
                final = banner['state'] == 'posting' and item['version'] == banner['version']
                db.execute('UPDATE native_banners SET delivered_version=?,state=?,posted=?,updated=? WHERE id=?',
                           (item['version'], 'delivered' if final else banner['state'], now, now, item['id']))
                db.execute("UPDATE native_deliveries SET state='delivered',posted=?,updated=? "
                           "WHERE banner_id=? AND state='pending' AND banner_version<=?", (now, now, item['id'], item['version']))
                counts['delivered'] += 1
            for banner_id in removed:
                changed = db.execute("UPDATE native_banners SET state='removed',updated=? WHERE id=? AND state='withdrawn'",
                                     (now, banner_id)).rowcount
                counts['removed' if changed else 'ignored'] += 1
            for item in failed:
                if db.execute("SELECT 1 FROM native_banners WHERE id=? AND state='posting'", (item['id'],)).fetchone():
                    self._fail(db, item['id'], item['reason'], now)
                    counts['failed'] += 1
                else:
                    counts['ignored'] += 1
        return counts

    def _fail(self, db, banner_id, reason, now):
        db.execute("UPDATE native_banners SET state='failed',reason=?,updated=? WHERE id=?", (reason, now, banner_id))
        db.execute("UPDATE native_deliveries SET state='failed',reason=?,updated=? WHERE banner_id=? AND state='pending'",
                   (reason, now, banner_id))

    def _withdraw(self, db, banner_id, reason, now):
        db.execute("UPDATE native_banners SET state='withdrawn',reason=?,attempts=0,updated=? WHERE id=?",
                   (reason, now, banner_id))
        db.execute("UPDATE native_deliveries SET state='suppressed',reason=?,updated=? WHERE banner_id=? AND state='pending'",
                   (reason, now, banner_id))

    def _reconcile(self, db, now, settings, mutes):
        """Derive suppression and withdrawal from authoritative notice, job and settings state."""
        hours = settings['quiet_hours']
        for row in _rows(db, '''SELECT d.notification_id,d.job_id,d.category,d.created,n.id AS present,
                n.acknowledged,j.state AS job_state FROM native_deliveries d
                LEFT JOIN job_notifications n ON n.id=d.notification_id LEFT JOIN jobs j ON j.id=d.job_id
                WHERE d.state='pending' '''):
            reason = ('removed' if row['present'] is None else 'acknowledged' if row['acknowledged'] is not None
                      else 'job_deleted' if row['job_state'] is None else 'job_paused' if row['job_state'] == 'paused'
                      else 'disabled' if not settings['enabled'] else 'kind_off' if not settings['kinds'][row['category']]
                      else 'muted' if row['job_id'] in mutes
                      else 'stale' if row['created'] < now - STALE_AFTER and not quiet(row['created'], hours) else None)
            if reason:
                db.execute("UPDATE native_deliveries SET state='suppressed',reason=?,updated=? "
                           "WHERE notification_id=? AND state='pending'", (reason, now, row['notification_id']))
        for banner in _rows(db, "SELECT b.id,b.scope,b.state,j.state AS job_state FROM native_banners b "
                                "LEFT JOIN jobs j ON j.id=b.job_id WHERE b.state IN ('posting','delivered')"):
            live = db.execute("SELECT 1 FROM native_deliveries d JOIN job_notifications n ON n.id=d.notification_id "
                              "WHERE d.banner_id=? AND n.acknowledged IS NULL AND d.state IN ('pending','delivered') "
                              'LIMIT 1', (banner['id'],)).fetchone()
            job = banner['scope'] == 'job'
            reason = ('job_deleted' if job and banner['job_state'] is None else 'job_paused'
                      if job and banner['job_state'] == 'paused' else 'resolved' if not live
                      else 'disabled' if not settings['enabled'] and banner['state'] == 'posting' else None)
            if reason:
                self._withdraw(db, banner['id'], reason, now)

    def _assign(self, db, now, settings):
        rows = _rows(db, "SELECT * FROM native_deliveries WHERE state='pending' AND banner_id IS NULL ORDER BY created,rowid")
        deferred = [row for row in rows if quiet(row['created'], settings['quiet_hours'])]
        if len(deferred) >= 2:  # defer and summarize, never drop
            self._open(db, 'quiet_summary', None, deferred, now, reason='quiet_hours')
            rows = [row for row in rows if row not in deferred]
        for row in rows:
            head = _one(db, "SELECT * FROM native_banners WHERE scope='job' AND job_id=? AND state IN ('posting','delivered') "
                            'AND opened>=? ORDER BY opened DESC,rowid DESC LIMIT 1', (row['job_id'], now - SAME_JOB_WINDOW))
            if head:
                self._join(db, head, row, now)
                continue
            shown = db.execute("SELECT count(*) FROM native_banners WHERE opened>? AND state!='failed'",
                               (now - RATE_WINDOW,)).fetchone()[0]
            if shown < RATE_LIMIT:
                self._open(db, 'job', row['job_id'], [row], now, banner_id=row['notification_id'])
                continue
            summary = _one(db, "SELECT * FROM native_banners WHERE scope='rate_summary' AND opened>? "
                               'ORDER BY opened DESC,rowid DESC LIMIT 1', (now - RATE_WINDOW,))
            if summary:
                self._join(db, summary, row, now)  # one summary per window, revived under the same identifier
            else:
                self._open(db, 'rate_summary', None, [row], now)

    def _open(self, db, scope, job_id, rows, now, banner_id=None, reason=None):
        banner_id = banner_id or uuid.uuid4().hex
        chats = {row['chat_id'] for row in rows}
        db.execute('INSERT INTO native_banners(id,scope,job_id,chat_id,members,version,state,delivered_version,attempts,'
                   "reason,opened,posted,updated) VALUES(?,?,?,?,?,1,'posting',NULL,0,?,?,NULL,?)",
                   (banner_id, scope, job_id, chats.pop() if len(chats) == 1 else None, len(rows), reason, now, now))
        for row in rows:
            db.execute('UPDATE native_deliveries SET banner_id=?,banner_version=1,reason=?,updated=? WHERE notification_id=?',
                       (banner_id, reason, now, row['notification_id']))

    def _join(self, db, banner, row, now):
        live = banner['state'] in ('posting', 'delivered')
        version = banner['version'] + 1
        # Open goes to the conversation only while every member shares it.
        chat = row['chat_id'] if not live else banner['chat_id'] if banner['chat_id'] == row['chat_id'] else None
        db.execute("UPDATE native_banners SET members=?,version=?,state='posting',attempts=0,reason=NULL,chat_id=?,updated=? "
                   'WHERE id=?', ((banner['members'] if live else 0) + 1, version, chat, now, banner['id']))
        db.execute('UPDATE native_deliveries SET banner_id=?,banner_version=?,updated=? WHERE notification_id=?',
                   (banner['id'], version, now, row['notification_id']))

    def _post(self, db, banner, settings):
        count, scope, preview = banner['members'], banner['scope'], settings['preview']
        latest = _one(db, 'SELECT n.title,n.body FROM native_deliveries d JOIN job_notifications n ON n.id=d.notification_id '
                          'WHERE d.banner_id=? ORDER BY d.created DESC,d.rowid DESC LIMIT 1', (banner['id'],))
        body = ''
        if preview == 'none':
            title, body = 'Pebble', 'A schedule has an update.' if count == 1 else f'{count} schedule updates.'
        elif scope == 'job':
            if count == 1 and latest:
                title = latest['title']
            else:
                row = db.execute('SELECT label FROM jobs WHERE id=?', (banner['job_id'],)).fetchone()
                title = (f'Scheduled job “{row[0]}”' if row else 'A scheduled job') + f': {count} updates'
            if preview == 'body' and latest:
                body = plain(latest['body'] if count == 1 else 'Latest: ' + latest['body'], BODY_LIMIT)
        else:
            many = count != 1
            title = 'While quiet hours were on' if scope == 'quiet_summary' else 'More schedule updates'
            body = (f'{count}{"" if scope == "quiet_summary" else " more"} schedule update{"s" if many else ""} '
                    f'{"are" if many else "is"} waiting in Pebble.')
        chat = banner['chat_id'] if isinstance(banner['chat_id'], str) and HEX.fullmatch(banner['chat_id']) else None
        return {'id': banner['id'], 'version': banner['version'], 'thread': banner['job_id'] or 'pebble-summary',
                'title': plain(title, TITLE_LIMIT), 'body': body, 'chat_id': chat, 'count': count,
                'actions': list(ACTIONS)}

    def _prune(self, db):
        db.execute("DELETE FROM native_deliveries WHERE state!='pending' AND NOT EXISTS(SELECT 1 FROM job_notifications n "
                   'WHERE n.id=native_deliveries.notification_id) AND NOT EXISTS(SELECT 1 FROM native_banners b '
                   "WHERE b.id=native_deliveries.banner_id AND b.state IN ('posting','delivered','withdrawn'))")
        excess = db.execute("SELECT count(*) FROM native_banners WHERE state IN ('removed','failed')").fetchone()[0] - MAX_BANNERS
        if excess > 0:
            db.execute("DELETE FROM native_banners WHERE id IN (SELECT id FROM native_banners WHERE state IN "
                       "('removed','failed') ORDER BY updated,rowid LIMIT ?)", (excess,))

    # Native Settings view (main window)

    def status(self):
        now, settings, mutes = self._now(), self.settings(), set(self.mutes())
        with self.store.lock:
            db = self.store.db
            counts = dict.fromkeys(('pending', 'delivered', 'suppressed', 'failed'), 0)
            counts.update({state: n for state, n in db.execute('SELECT state,count(*) FROM native_deliveries GROUP BY state')})
            waiting = [row[0] for row in db.execute(
                "SELECT created FROM native_deliveries WHERE state='pending' AND banner_id IS NULL")]
            failure = _one(db, "SELECT reason,updated FROM native_banners WHERE state='failed' ORDER BY updated DESC,rowid DESC "
                               'LIMIT 1')
            jobs = _rows(db, 'SELECT id,chat_id,label,state FROM jobs ORDER BY created,id LIMIT ?', (MAX_JOBS,))
        return {'settings': settings, 'quiet_now': quiet(now, settings['quiet_hours']), 'outbox': counts,
                'deferred': sum(quiet(created, settings['quiet_hours']) for created in waiting),
                'last_failure': {'reason': failure['reason'], 'at': iso(failure['updated'])} if failure else None,
                'jobs': [{'job_id': job['id'], 'chat_id': job['chat_id'], 'label': job['label'], 'state': job['state'],
                          'muted': job['id'] in mutes} for job in jobs],
                'actions': list(ACTIONS)}


class NotificationAPI:
    """Route adapter. `methods` are main-window webview routes; `native_methods` are Rust-only.

    The parent merges both into App.dispatch, lists only `methods` in main.rs
    ALLOWED (never for the pet), and keeps `native_methods` out of ALLOWED so a
    webview, generated view or mod can never pull, acknowledge or fake an OS
    banner or a lifecycle event. No model tool maps to any route here.
    """

    ROUTES = {'notification_status': (set(), set()), 'notification_mute': ({'job_id', 'muted'}, set())}
    NATIVE = {'lifecycle_event': ({'kind'}, {'timezone'}), 'lifecycle_quit_summary': (set(), set()),
              'notification_native_pending': (set(), set()),
              'notification_native_delivered': (set(), {'delivered', 'removed', 'failed'})}

    def __init__(self, app):
        self.app = app
        self.methods = {name: self._bind(name, self.ROUTES[name]) for name in self.ROUTES}
        self.native_methods = {name: self._bind(name, self.NATIVE[name]) for name in self.NATIVE}

    def _bind(self, name, shape):
        def route(**params):
            required, optional = shape
            if not required <= set(params) or set(params) - required - optional:
                raise ValueError(f'{name} accepts ' + (', '.join(sorted(required | optional)) or 'no fields') + '.')
            return getattr(self, '_' + name)(**params)
        return route

    def _owner(self, name, message):
        owner = getattr(self.app, name, None)
        if owner is None:
            raise ValueError(message)
        return owner

    def _notification_status(self):
        lifecycle = getattr(self.app, 'lifecycle', None)
        result = self._owner('notifications', 'Native notifications are unavailable.').status()
        result['lifecycle'] = lifecycle.status() if lifecycle is not None else None
        return result

    def _notification_mute(self, job_id, muted):
        return self._owner('notifications', 'Native notifications are unavailable.').set_mute(job_id, muted)

    def _lifecycle_event(self, kind, **fields):
        return self._owner('lifecycle', 'Lifecycle observation is unavailable.').event(kind, **fields)

    def _lifecycle_quit_summary(self):
        return self._owner('lifecycle', 'Lifecycle observation is unavailable.').quit_summary()

    def _notification_native_pending(self):
        return self._owner('notifications', 'Native notifications are unavailable.').native_pending()

    def _notification_native_delivered(self, delivered=None, removed=None, failed=None):
        return self._owner('notifications', 'Native notifications are unavailable.').native_delivered(
            delivered, removed, failed)
