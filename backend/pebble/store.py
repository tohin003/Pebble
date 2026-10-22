import contextlib
import errno
import json
import sqlite3
import threading
import time
import uuid
import weakref
from pathlib import Path


# Epoch 6 (C10): the route class each message was produced under. A message without a row is
# "unknown" to a reviewed model handoff, which withholds it unless the person releases that class.
ROUTES_DDL = """
CREATE TABLE IF NOT EXISTS message_routes(message_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,route_class TEXT NOT NULL);
"""
# Phase 22 (s22-storage): Recently deleted. One row in the existing settings table maps a hidden conversation to
# when and how it was deleted; App.delete_chat removes it for good after TRASH_DAYS. No table, so no new epoch.
TRASH_KEY = 'recently_deleted'
TRASH_DAYS = 30
TRASH_ORIGINS = ('documents', 'app')
# The chat list without hidden conversations (App.state's parent patch uses this exact clause).
VISIBLE_CHATS = ("id NOT IN (SELECT key FROM json_each(COALESCE((SELECT value FROM settings WHERE key='"
                 + TRASH_KEY + "'),'{}')))")
# V1-011: the conversations App.state lists: those with a message, an action or a job, minus Recently deleted. One
# set built per call; the earlier EXISTS form scanned the unindexed messages table once per conversation (1.6 s at
# 1,000 conversations), with the same rows. Needs the jobs table, which App creates.
LISTED_CHATS = ("SELECT * FROM chats WHERE id IN (SELECT chat_id FROM messages UNION SELECT chat_id FROM actions "
                "UNION SELECT chat_id FROM jobs) AND " + VISIBLE_CHATS + " ORDER BY updated DESC")
# V1-057: deleted rows are overwritten with zeros, freed pages included, so text deleted for good does not stay in
# pebble.sqlite. A connection setting (no schema change). FAST would leave freed pages, such as a long reply's
# overflow pages, as they were.
SECURE_DELETE = 'ON'
CHECKPOINT_MODES = ('PASSIVE', 'FULL', 'RESTART', 'TRUNCATE')
READ_TIMEOUT = 5.0  # a WAL reader waits only while another connection recovers the log

# V1-067: database failures said in words. The codes travel in the RPC error payload (main.py).
DISK_FULL = 'disk_full'
TASK_RECORDING = 'task_still_recording'
STORE_MESSAGES = {
    DISK_FULL: 'Your Mac is out of disk space, so Pebble could not save this. Free some space, then try again.',
    TASK_RECORDING: 'The previous task is still being recorded. Try again in a few seconds.',
}


class StoreRefusal(ValueError):
    """A database failure the person can act on, with a content-free code (see STORE_MESSAGES)."""

    def __init__(self, code):
        super().__init__(STORE_MESSAGES[code])
        self.code = code


def friendly_error(exc):
    """A StoreRefusal for a full disk (SQLITE_FULL, ENOSPC, EDQUOT) or the one-live-task constraint (runs_one_owner),
    found on exc or its explicit causes; None for anything else, which callers report as before."""
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, StoreRefusal):
            return exc
        if isinstance(exc, sqlite3.Error):
            code = getattr(exc, 'sqlite_errorcode', None)
            if (code is not None and code & 0xff == sqlite3.SQLITE_FULL) or 'database or disk is full' in str(exc):
                return StoreRefusal(DISK_FULL)
            if isinstance(exc, sqlite3.IntegrityError) and 'runs_one_owner' in str(exc):
                return StoreRefusal(TASK_RECORDING)
        elif isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return StoreRefusal(DISK_FULL)
        exc, seen = exc.__cause__, seen + 1
    return None


def error_kind(exc):
    """A content-free name for a failure, for logs: the class, plus SQLite's fixed result-code name when there is one
    (for example 'OperationalError/SQLITE_BUSY'). Never the message, which may quote data."""
    name = getattr(exc, 'sqlite_errorname', None)
    return type(exc).__name__ + ('/' + name if isinstance(name, str) and name.isidentifier() else '')


class Store:
    # Phase 22: no settings.json sync unless __init__ finds a structured home. A class default, so a store built
    # without Store.__init__ (memory_eval's private in-memory store) keeps exactly the pre-Phase-22 behaviour.
    settings_file = None
    path = None  # V1-011: no read connections without a database file; read() then uses query()
    _readers = None

    def __init__(self, home: Path):
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = home / "pebble.sqlite"
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=" + SECURE_DELETE)
        self.lock = threading.RLock()
        # V1-011: per-thread read-only connections (read_connection), closed with the store.
        self._readers = {'local': threading.local(), 'open': [], 'lock': threading.Lock(), 'closed': False}
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,title TEXT,workspace TEXT,updated REAL);
          CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,chat_id TEXT,role TEXT,content TEXT,created REAL);
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,chat_id TEXT,kind TEXT,status TEXT,payload TEXT,created REAL);
        """ + ROUTES_DDL)
        self.db.commit()
        # Phase 22: a structured home (~/.pebble) keeps settings.json in step with the settings rows. A flat
        # --home (tests, validators, every home before Phase 22) has none, and nothing below changes for it.
        self.settings_file = None
        from .data_home import for_data
        layout, _ = for_data(home)
        if layout is not None and layout.structured:
            from .settings_file import SettingsSync
            self.settings_file = SettingsSync(layout.root, self)

    def query(self, sql, args=()):
        with self.lock:
            outer = self.db.in_transaction
            try:
                cur = self.db.execute(sql, args)
                names = [d[0] for d in cur.description] if cur.description else []
                rows = [dict(zip(names, r)) for r in cur.fetchall()] if names else []
                self.db.commit()
            except BaseException:
                # V1-073: a refused statement (a constraint, a locked or full database) must not leave the shared
                # connection inside the transaction Python opened for it: every subsystem that needs an outside
                # transaction boundary would refuse work until some later query committed. A transaction the
                # caller opened before this query stays the caller's to end.
                if not outer and self.db.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        self.db.rollback()
                raise
            return rows

    # V1-011: reads that must not hold Store.lock (App.state, the Documents copy pass).

    def read_connection(self):
        """This thread's read-only connection to the same database file. It never takes Store.lock and can never
        write; in WAL mode each statement sees the latest committed data (never another connection's open
        transaction). Created on first use in a thread, reused by that thread, closed by close(). Fetch results
        fully: an unfinished statement keeps its snapshot open and delays a checkpoint."""
        readers = self._readers
        if readers is None or self.path is None:
            raise sqlite3.ProgrammingError('This store has no database file to read from.')
        db = getattr(readers['local'], 'db', None)
        if db is not None:
            return db
        with readers['lock']:
            if readers['closed']:
                raise sqlite3.ProgrammingError('Cannot operate on a closed database.')
            # Connections of threads that have ended are closed here, so short-lived threads leave none behind.
            for entry in [e for e in readers['open'] if e[0]() is None or not e[0]().is_alive()]:
                readers['open'].remove(entry)
                with contextlib.suppress(sqlite3.Error):
                    entry[1].close()
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, check_same_thread=False,
                                 timeout=READ_TIMEOUT)
            db.execute('PRAGMA query_only=1')
            readers['open'].append((weakref.ref(threading.current_thread()), db))
        readers['local'].db = db
        return db

    def read(self, sql, args=()):
        """query() for one read statement through read_connection(): rows as dicts, no Store.lock. A store without
        a database file (memory_eval's) reads through query()."""
        if self._readers is None or self.path is None:
            return self.query(sql, args)
        cur = self.read_connection().execute(sql, args)
        names = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(names, r)) for r in cur.fetchall()] if names else []

    @contextlib.contextmanager
    def no_wait(self):
        """f24-locked: holds Store.lock; statements inside never wait on another connection's write lock (busy
        timeout 0), so a held lock refuses at once with sqlite3.OperationalError instead of after the 5 s default.
        Pebble's own writes on this connection queue on Store.lock as usual, so only another connection can refuse.
        For a write after a run's worker announced 'idle': a whole busy timeout there would keep Store.lock, and every
        status read queued behind it, after the service said the worker was released (F8-2). The caller's previous
        busy timeout is restored."""
        with self.lock:
            previous = int(self.db.execute('PRAGMA busy_timeout').fetchone()[0])
            self.db.execute('PRAGMA busy_timeout=0')
            try:
                yield
            finally:
                self.db.execute(f'PRAGMA busy_timeout={previous}')

    def checkpoint(self, mode='TRUNCATE', timeout=0.5):
        """V1-057: copy the write-ahead log into pebble.sqlite and, with TRUNCATE (the default), empty the -wal file,
        so text deleted for good leaves it too. For callers after a purge or forget batch. Waits at most timeout
        seconds for other connections; True when every frame was copied (and the log emptied), False otherwise (the
        next call retries). Never inside an open transaction of this connection."""
        if mode not in CHECKPOINT_MODES:
            raise ValueError('Unknown checkpoint mode.')
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise ValueError('Use a checkpoint timeout from 0 to 30 seconds.')
        with self.lock:
            if self.db.in_transaction:
                return False
            previous = int(self.db.execute('PRAGMA busy_timeout').fetchone()[0])
            self.db.execute(f'PRAGMA busy_timeout={int(timeout * 1000)}')
            try:
                busy, frames, copied = self.db.execute(f'PRAGMA wal_checkpoint({mode})').fetchone()
            finally:
                self.db.execute(f'PRAGMA busy_timeout={previous}')
        return busy == 0 and (frames == copied or frames <= 0)

    def setting(self, key, default=None):
        rows = self.query("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key, value):
        self.query(
            "INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value))
        )
        if self.settings_file is not None:
            self.settings_file.written(key, value)  # never raises; the next sync reconciles a failed write

    # Recently deleted (Phase 22). Hidden conversations keep every row until App.delete_chat removes them.

    def trashed(self):
        """{chat_id: {'deleted': seconds, 'origin': 'documents'|'app', 'title': str}} for conversations that exist."""
        with self.lock:
            entries = self.setting(TRASH_KEY, {}) or {}
            if not isinstance(entries, dict):
                return {}
            present = {r['id'] for r in self.query('SELECT id FROM chats')}
            return {cid: entry for cid, entry in entries.items() if cid in present and isinstance(entry, dict)}

    def trash_chat(self, cid, origin, now=None):
        if origin not in TRASH_ORIGINS:
            raise ValueError('Unknown deletion origin.')
        with self.lock:
            rows = self.query('SELECT title FROM chats WHERE id=?', (cid,))
            if not rows:
                raise ValueError('Conversation not found.')
            entries = self.trashed()
            entries[cid] = {'deleted': float(now if now is not None else time.time()), 'origin': origin,
                            'title': str(rows[0]['title'] or '')[:200]}
            self.query('INSERT OR REPLACE INTO settings VALUES(?,?)', (TRASH_KEY, json.dumps(entries)))
            return entries[cid]

    def untrash_chat(self, cid):
        """Restore a hidden conversation, or drop the entry of one that was deleted for good."""
        with self.lock:
            entries = self.setting(TRASH_KEY, {}) or {}
            if not isinstance(entries, dict) or cid not in entries:
                return False
            entries.pop(cid)
            self.query('INSERT OR REPLACE INTO settings VALUES(?,?)', (TRASH_KEY, json.dumps(entries)))
            return True

    def trash_due(self, now=None):
        """Hidden conversations older than TRASH_DAYS, oldest first."""
        limit = (now if now is not None else time.time()) - TRASH_DAYS * 86400
        entries = self.trashed()
        return sorted((cid for cid, e in entries.items() if (e.get('deleted') or 0) <= limit),
                      key=lambda cid: entries[cid].get('deleted') or 0)

    def new_chat(self, workspace=None):
        cid = uuid.uuid4().hex
        self.query(
            "INSERT INTO chats VALUES(?,?,?,?)",
            (cid, "New conversation", workspace, time.time()),
        )
        return cid

    def message(self, cid, role, content, mid=None, route_class=None):
        mid = mid or uuid.uuid4().hex
        self.query(
            "INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?)",
            (mid, cid, role, content, time.time()),
        )
        if route_class is not None:
            self.query("INSERT OR REPLACE INTO message_routes VALUES(?,?,?)", (mid, cid, route_class))
        self.query("UPDATE chats SET updated=? WHERE id=?", (time.time(), cid))
        if role == "user":
            self.query(
                "UPDATE chats SET title=? WHERE id=? AND title='New conversation'",
                (content[:54].replace("\n", " "), cid),
            )
        return mid

    def action(self, cid, kind, payload):
        aid = uuid.uuid4().hex
        self.query(
            "INSERT INTO actions VALUES(?,?,?,?,?,?)",
            (aid, cid, kind, "pending", json.dumps(payload), time.time()),
        )
        return aid

    def actions(self, cid):
        rows = self.query(
            "SELECT * FROM actions WHERE chat_id=? ORDER BY created", (cid,)
        )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def close(self):
        readers = self._readers
        if readers is not None:
            # Readers first: the read-write connection, closed last, then checkpoints and removes the -wal file.
            with readers['lock']:
                readers['closed'] = True
                for _, db in readers['open']:
                    with contextlib.suppress(sqlite3.Error):
                        db.close()
                readers['open'].clear()
        with self.lock:
            self.db.close()
