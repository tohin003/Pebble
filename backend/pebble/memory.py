"""Six bounded memory stores. Retrieved content is evidence, never authority.

SQLite FTS5 keeps retrieval cheap on an 8 GB machine. Recall ranks bounded FTS
and trigram candidates with deterministic lexical/metadata signals; no
embedding model or background LLM is resident. The working record is derived
from host journal rows (goal, plan statuses, pending question/action IDs and
remaining budget), not from model text or lossy summaries of tool outcomes.
"""

import importlib
import importlib.util
import inspect
import json
import re
import sqlite3
import time
import uuid
import hashlib
import math

from . import memory_retrieval

KINDS = {
    "working": "Working context",
    "episodic": "Past experiences",
    "semantic": "Facts & knowledge",
    "procedural": "Procedures",
    "temporal": "Time-sensitive information",
    "profile": "Your preferences",
}
RANKERS = ('v1', 'v2')
DEFAULT_RECALL_BUDGET = 2000
WORKING_SCHEMA = 'pebble-working-state/1'
WORKING_GOAL_LIMIT = 8000
WORKING_IDS = 16
WORKING_RECENT = 6
WORKING_VIEW_IDS = 8
PLAN_STATUSES = frozenset({'pending', 'in_progress', 'blocked', 'completed'})
PLAN_ID = re.compile(r'[A-Za-z0-9_-]{1,40}')
OPEN_ACTIONS = ('pending', 'running')
# Connection-local (temp) index; never part of the persisted schema. Its
# creation is the runtime probe for the FTS5 trigram tokenizer.
TRIGRAM_TABLE = "CREATE VIRTUAL TABLE temp.memory_trigram USING fts5(body, tokenize='trigram')"
STRATEGIES = {
    'v1': 'lexical/metadata ranking over SQLite FTS5 candidates; no embeddings or model',
    'v2': 'hybrid lexical/trigram/metadata ranking over bounded SQLite candidates; no embeddings or model',
}
LIST_LIMIT = 300
MAX_LIST_OFFSET = 1_000_000
MAX_LIST_QUERY = 200
MAX_LIST_TERMS = 8
# Explicit capture (V1-033): "Remember (that|this) <statement>". Only the first
# sentence is saved; a recollection, a question or a reminder is not a statement.
CAPTURE = re.compile(r"^(?:please\s+)?remember(?:\s+that|\s+this)?\s*[:,-]?\s+(.+)$", re.I | re.S)
NOT_A_STATEMENT = re.compile(r"^(?:please\s+)?remember\s+(?:when|how|what|where|who|whom|whose|why|which|if|whether"
                             r"|(?:the|that)\s+(?:time|day|night|moment)|to\s+\w+)\b", re.I)
SENTENCE_BREAK = re.compile(r'[.!?]+(?=\s)|\n')
ABBREVIATIONS = frozenset('e.g i.e etc vs mr mrs ms dr st jr sr approx dept fig inc ltd co'.split())
CAPTURE_LIMIT = 2000
MEMORY_OFF = 'Memory is off. Enable it in Settings → Memory to save this.'
LAYER_OFF = 'Memory is off for this layer. Enable it in Settings → Memory to save this.'
# Conversational forgetting (V1-034) is a request for a native Forget card,
# never a model action: these only find records for the person to choose.
FORGET = re.compile(r"^(?:(?:hey\s+)?pebble[,:]?\s+)?(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
                    r"(?:forget|stop\s+remembering|do\s+not\s+remember|don['’]t\s+remember)\b\s*(?P<subject>.*)$", re.I | re.S)
FORGET_ERASE = re.compile(r"^(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?(?:delete|remove|erase|clear|wipe)\s+"
                          r"(?P<subject>.*?)\s+from\s+(?:your\s+|my\s+|the\s+)?(?:memory|memories|saved\s+memories)\W*$", re.I | re.S)
FORGET_LEAD = re.compile(r"^(?:about\s+|that\s+|what\s+I\s+(?:said|told\s+you)\s+about\s+|everything\s+about\s+|the\s+fact\s+that\s+)+", re.I)
FORGET_TAIL = re.compile(r"(?:\s+(?:please|for\s+me|now|too|as\s+well))*[\s.!?]*$", re.I)
FORGET_PRONOUNS = frozenset({'', 'it', 'that', 'this', 'them', 'those', 'these', 'what i said', 'what i just said',
                             'the last thing', 'the last thing i said', 'my last message', 'the last one'})
FORGET_LIMIT = 20
MAX_FORGET_TEXT = 500
# A reply claiming that something was forgotten, deleted or will no longer be
# remembered. The model has no forget tool, so such a claim is never backed.
_ADVERB = r"(?:now\s+|just\s+|already\s+|successfully\s+|also\s+)?"
FORGET_CLAIM = re.compile(
    r"\bI(?:['’]ve|\s+have)\s+" + _ADVERB + r"forgotten\b(?!\s+to\b)"
    r"|\bI(?:['’]ll|\s+will)\s+" + _ADVERB + r"forget\b(?!\s+to\b)"
    r"|\bI(?:\s+won['’]t|\s+will\s+not|\s+will\s+no\s+longer|['’]ll\s+no\s+longer|['’]ll\s+not|\s+no\s+longer)\s+remember\b"
    r"|\b(?:has|have)\s+(?:now\s+)?been\s+(?:forgotten|erased|wiped)\b|\bis\s+now\s+forgotten\b"
    r"|\bconsider\s+it\s+forgotten\b|^\W*(?:done|ok(?:ay)?)\W+(?:it['’]s\s+|that['’]s\s+)?forgotten\b"
    r"|\b(?:I(?:['’]ve|\s+have)?|it\s+(?:has\s+been|was|is)|(?:has|have)\s+been)\s+" + _ADVERB
    + r"(?:deleted|removed|erased|cleared|wiped|purged)\b[^.!?\n]{0,80}?\b(?:memor(?:y|ies)|saved|remember\w*)"
    r"|\bmemor(?:y|ies)\s+(?:has\s+been\s+|have\s+been\s+|is\s+|are\s+)?(?:deleted|removed|erased|cleared|wiped|forgotten)\b",
    re.I | re.M)


class MemoryOff(ValueError):
    """A save request while memory, or its layer, is off. Nothing is stored;
    `result` is the capture result the run continues with (V1-033)."""

    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


def reply_claims_forgetting(text):
    """Whether a reply claims a memory was forgotten, deleted or will no longer
    be remembered (V1-034). Pebble forgets only through the person's native
    Forget action, so the runner withholds such a claim when the run holds no
    memory_delete receipt. Requests ("Don't forget to call") and refusals ("I
    can't delete memories") are not claims."""
    return isinstance(text, str) and bool(FORGET_CLAIM.search(text[:8000]))


def forget_request(text):
    """The subject of a conversational forget request ('my name' for 'Please
    forget my name.'), '' for one that names no subject ('Forget that.'), else
    None. A dismissal followed by another request ('Forget it, what is ...')
    is not a forget request."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_FORGET_TEXT:
        return None
    text = text.strip()
    match = FORGET.match(text) or FORGET_ERASE.match(text)
    if not match:
        return None
    subject = FORGET_TAIL.sub('', FORGET_LEAD.sub('', match['subject'].strip()))
    if subject.casefold() in FORGET_PRONOUNS:
        return ''
    head = subject.split(',', 1)[0].strip().casefold()
    if head in FORGET_PRONOUNS - {''} or re.search(r'[.!?]\s+\S', subject):
        return None  # "forget it, ..." or more than one sentence: not a request to forget a memory
    return subject


def first_statement(text):
    """(first sentence, the rest) of a message body; '3.12', 'e.g.' and 'J. Smith' do not end a sentence."""
    text = text.strip()
    for match in SENTENCE_BREAK.finditer(text):
        if match.group() == '.':
            word = re.search(r'([A-Za-z.]+)$', text[:match.start()])
            if word and (word[1].casefold().rstrip('.') in ABBREVIATIONS or len(word[1]) == 1 and word[1].isupper()):
                continue
        head, rest = text[:match.end()].strip(), text[match.end():].strip()
        if head:
            return head, rest
    return text, ''


def working_view(state, budget, request=''):
    """Bounded recall view of a working record. The goal is shown up to a
    quarter of the budget with an explicit truncation flag, and omitted when
    it is the request being answered (already in context)."""
    goal = state.get('current_goal') if isinstance(state.get('current_goal'), str) else ''
    view = {}
    if goal and goal.strip() == (request or '').strip():
        view['current_goal_is_request'] = True
    else:
        limit = max(80, min(600, budget // 4))
        view['current_goal'] = goal[:limit]
        if len(goal) > limit or state.get('goal_truncated'):
            view.update(goal_truncated=True, goal_chars=state.get('goal_chars', len(goal)))
    if 'phase' in state:
        view['phase'] = state['phase']
    if state.get('schema') != WORKING_SCHEMA:
        return view
    plan = state.get('plan')
    if plan:
        steps = plan['steps']
        view['plan'] = {'revision': plan['revision'], 'total': len(steps),
                        'completed': sum(s['status'] == 'completed' for s in steps),
                        'open': [[s['id'], s['status']] for s in steps if s['status'] != 'completed'][:WORKING_VIEW_IDS]}
    for key in ('pending_question_ids', 'pending_action_ids', 'uncertain_receipt_ids', 'pending_input_ids'):
        if state.get(key):
            view[key] = state[key][:WORKING_VIEW_IDS]
    if state.get('required_question_pending'):
        view['required_question_pending'] = True
    view['remaining'] = state.get('remaining')
    return view


class Memory:
    def __init__(self, store):
        self.store = store
        # Recall options. The default embedder embeds nothing; graph None
        # means W3's memory_graph.withheld when that module is installed.
        self.embedder = memory_retrieval.NO_EMBEDDER
        self.graph = None
        self.trigram_fts = True
        self._trigram = None
        self._trigram_index, self._trigram_ids = {}, {}
        with store.lock:
            store.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
              scope TEXT NOT NULL, chat_id TEXT, origin TEXT NOT NULL,
              created REAL NOT NULL, updated REAL NOT NULL, expires REAL);
            CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope,kind,updated);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(id UNINDEXED,content,tokenize='unicode61');
            CREATE TRIGGER IF NOT EXISTS memory_insert AFTER INSERT ON memories BEGIN
              INSERT INTO memory_search(id,content) VALUES(new.id,new.content); END;
            CREATE TRIGGER IF NOT EXISTS memory_delete AFTER DELETE ON memories BEGIN
              DELETE FROM memory_search WHERE id=old.id; END;
            CREATE TRIGGER IF NOT EXISTS memory_update AFTER UPDATE OF content ON memories BEGIN
              DELETE FROM memory_search WHERE id=old.id;
              INSERT INTO memory_search(id,content) VALUES(new.id,new.content); END;
            CREATE TABLE IF NOT EXISTS checkpoints(chat_id TEXT PRIMARY KEY,payload TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS metrics(message_id TEXT PRIMARY KEY,chat_id TEXT,payload TEXT);
            """)
            store.db.commit()
        if not store.setting("memory_initialized", False):
            if self.layer_enabled('procedural'):
                for content in (
                    "Engineering workflow: inspect relevant source; make a minimal change; verify with relevant tests; distinguish drafts from applied edits and successful tests from unverified claims.",
                    "Current-information workflow: retrieve a current source for weather, prices and news. Record retrieval time and source. Opening an app does not answer a factual question.",
                ):
                    self.put('procedural', content, origin='curated')
            # Do not backfill requests made while personalization was disabled.
            # Existing legacy memories remain readable; new learning is run-owned.
            store.set_setting('memory_initialized', True)

    def enabled(self):
        return self.store.setting("memory_enabled", True)

    def layer_enabled(self, kind):
        return self.enabled() and self.store.setting('memory_layers', {}).get(kind, True)

    def capture_user(self, text, cid=None, workspace=None):
        """The person's explicit "Remember (that) <statement>" (V1-033).

        Only a single declarative statement is saved: the first sentence of the
        message. Returns None when the message is not one (no "remember"
        prefix; "remember when/how/what/if ...", "remember the time ...",
        "remember to <verb>", or a first sentence that is a question), so the
        run goes to the model unchanged. Otherwise returns {'saved', 'id',
        'kind', 'content', 'statement', 'continue_run', 'relation', 'reply'}:
        continue_run is true when the message says more than the statement, and
        the run then answers the whole message. A correction of a saved value
        with the same key supersedes it ('Updated your saved name.').

        With memory or the layer off nothing is stored and MemoryOff (a
        ValueError) is raised; its `result` has saved False and continue_run
        True, so the runner falls through to the model."""
        match = CAPTURE.match(text) if isinstance(text, str) else None
        if not match or NOT_A_STATEMENT.match(text):
            return None
        statement, rest = first_statement(match[1])
        statement = re.sub(r'^[-*•]\s+', '', statement).strip()
        if not statement or statement.endswith('?'):
            return None
        kind = "profile" if re.search(r"\b(i|my|me|i'm)\b", statement, re.I) else "semantic"
        layer = "your preferences" if kind == "profile" else "facts & knowledge"
        if not self.layer_enabled(kind):
            raise MemoryOff(MEMORY_OFF if not self.enabled() else LAYER_OFF, {
                'saved': False, 'id': None, 'kind': kind, 'content': None, 'statement': statement, 'continue_run': True,
                'relation': None, 'reply': "Memory is off, so I didn't save this. You can turn it on in Settings → Memory."})
        if len(statement) > CAPTURE_LIMIT:
            raise ValueError("Save a shorter memory (up to 2,000 characters).")
        scope = "global" if kind == "profile" else workspace or "global"
        ident = "direct-" + hashlib.sha256((scope + statement).encode()).hexdigest()[:24]
        self.put(kind, statement, scope, cid, origin="direct-user", ident=ident)
        relation = self.relate_saved(ident, correction=True)
        status = relation['status'] if relation else 'saved'
        where = "Settings → Memory"
        if status == 'updated':
            reply = f"Updated your saved {relation['key']}. You can inspect or forget it in {where}."
        elif status == 'contested':
            reply = (f"Saved this in **{layer}**, but it disagrees with another saved memory, so neither is used "
                     f"until you choose one in {where}.")
        elif rest:
            shown = statement if len(statement) <= 120 else statement[:119] + '…'
            reply = f"Saved “{shown}” in **{layer}**. You can inspect or forget it in {where}."
        else:
            reply = f"Saved this in **{layer}**. You can inspect or forget it in {where}."
        return {'saved': True, 'id': ident, 'kind': kind, 'content': statement, 'statement': statement,
                'continue_run': bool(rest), 'relation': status, 'reply': reply}

    def relate_saved(self, ident, correction=False):
        """The relation step for a record the person saved (V1-035). memory_save
        calls it after put(): a same-key, different-value record the person saved
        earlier makes both contested (withheld from recall and listed by
        memory_contests). capture_user calls it with correction=True: the new
        statement supersedes the older same-scope versions. None without
        learning or without a person claim; else memory_graph.relate_person's
        result."""
        learning = getattr(self, 'learning', None)
        if learning is None or not isinstance(ident, str):
            return None
        return learning.relate_person(ident, correction)

    def put(
        self,
        kind,
        content,
        scope="global",
        chat_id=None,
        origin="user",
        expires=None,
        ident=None,
        created=None,
    ):
        if (
            kind not in KINDS
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 4000
        ):
            raise ValueError("Choose a memory type and enter 1–4000 characters.")
        if not self.layer_enabled(kind):
            raise ValueError('Memory is off for this layer.')
        if expires is not None and (
            type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time()
        ):
            raise ValueError("The expiry must be in the future.")
        now = time.time()
        ident = ident or uuid.uuid4().hex
        self.store.query(
            "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET content=excluded.content,updated=excluded.updated,expires=excluded.expires",
            (
                ident,
                kind,
                content.strip(),
                scope,
                chat_id,
                origin,
                created or now,
                now,
                expires,
            ),
        )
        # Bound automatic history without silently deleting user-authored memory.
        self.store.query(
            "DELETE FROM memories WHERE origin='conversation' AND id IN (SELECT id FROM memories WHERE origin='conversation' ORDER BY updated DESC LIMIT -1 OFFSET 1500)"
        )
        return ident

    def list(self, kind=None, query=None, offset=0, limit=LIST_LIMIT):
        """Native inspection, newest first, one page of at most 300 records (V1-099).

        query keeps records whose text has every query word (as a word prefix,
        through the memory_search FTS index); a query without words matches
        the text as a substring. Page with offset += len(page) while a page is
        full. The defaults return the 0.6.1 list."""
        self.expire()
        if kind and kind not in KINDS:
            raise ValueError("Unknown memory type.")
        if query is not None and (not isinstance(query, str) or len(query) > MAX_LIST_QUERY):
            raise ValueError("Search with at most 200 characters.")
        if type(offset) is not int or not 0 <= offset <= MAX_LIST_OFFSET or type(limit) is not int or not 1 <= limit <= LIST_LIMIT:
            raise ValueError("Use an offset from 0 and a page of 1 to 300 memories.")
        learned = hasattr(self, 'learning')
        where, args = [], []
        if kind:
            where.append("m.kind=?")
            args.append(kind)
        query = ' '.join(query.split()) if query else ''
        if query:
            terms = list(dict.fromkeys(t.casefold() for t in memory_retrieval.TOKEN.findall(query)))[:MAX_LIST_TERMS]
            if terms:
                where.append("m.id IN (SELECT id FROM memory_search WHERE memory_search MATCH ?)")
                args.append(' AND '.join('"%s"*' % t for t in terms))
            else:
                where.append("instr(lower(m.content),lower(?))>0")
                args.append(query)
        return self.store.query(
            "SELECT m.*" + (',e.status AS learning_status,e.proof AS provenance' if learned else '')
            + ' FROM memories m' + (' LEFT JOIN memory_evidence e ON e.memory_id=m.id' if learned else '')
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY m.updated DESC,m.rowid DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )

    def expire(self, wait=True):
        """Delete expired memories and settle expired candidates. wait=False never waits on another connection's
        write lock (busy timeout 0): it gives up at once, writes nothing and returns False; the next pass retries."""
        if not wait:
            with self.store.lock:
                db = self.store.db
                previous = int(db.execute('PRAGMA busy_timeout').fetchone()[0])
                db.execute('PRAGMA busy_timeout=0')
                try:
                    self.expire()
                    return True
                except sqlite3.OperationalError:
                    db.rollback()  # the refused statement wrote nothing
                    return False
                finally:
                    db.execute(f'PRAGMA busy_timeout={previous}')
        self.store.query(
            "DELETE FROM memories WHERE expires IS NOT NULL AND expires<=?",
            (time.time(),),
        )
        if hasattr(self, 'learning'):
            self.store.query("UPDATE memory_candidates SET status='expired',reason='validity_ended',updated=? WHERE expires<=? AND status IN ('pending','promoted')", (time.time(), time.time()))
            self.store.query('DELETE FROM memory_evidence WHERE memory_id NOT IN (SELECT id FROM memories)')

    def stats(self):
        # F8-2: a status read never waits on a write. Merge (i20): it still expires rows when the database is free,
        # as before (the time-aware upgrade validators read a home right after one `state` call), but gives up at
        # once while another connection holds a write lock; App.idle, list() and recall retry. Expired rows are
        # never counted either way.
        self.expire(wait=False)
        counts = {
            r["kind"]: r["n"]
            for r in self.store.query(
                "SELECT kind,count(*) n FROM memories WHERE expires IS NULL OR expires>? GROUP BY kind", (time.time(),)
            )
        }
        counts["working"] = self.store.query("SELECT count(*) n FROM checkpoints")[0][
            "n"
        ]
        result = {
            "enabled": self.enabled(),
            "types": [
                {"id": k, "name": v, "count": counts.get(k, 0), 'enabled': self.layer_enabled(k)}
                for k, v in KINDS.items()
            ],
            "strategy": "SQLite FTS5 · hybrid lexical/trigram/metadata ranking · bounded retrieval · no extra model",
        }
        if hasattr(self, 'learning'):
            from . import memory_facts
            result['learning'] = {'status_counts': {r['status']: r['n'] for r in self.store.query('SELECT status,count(*) AS n FROM memory_candidates GROUP BY status')},
                'last_error_type': getattr(self, 'maintenance_error', None),
                'pending_limit': 1000, 'per_run_limit': 32, 'maintenance_batch': 16, 'automatic_record_limit': 1500,
                'facts_per_file': memory_facts.MAX_FACTS_PER_FILE, 'facts_per_run': memory_facts.MAX_FACTS_PER_RUN,
                'observations_per_run': memory_facts.MAX_LESSONS,
                'classifier': 'owned human quotes, whole-line attributed source facts, host test, build, lint and typecheck recipes and settled outcome observations'}
        return result

    def checkpoint(
        self, chat_id, goal, actions=(), phase="complete", observations=None, run_id=None
    ):
        """Compatibility entry point used at existing persist boundaries.

        With a run (given, or the chat's latest), the record is re-derived
        from the host journal and the caller's goal, actions, phase and
        observations are not stored. Only a chat without any run keeps the
        legacy goal/actions record."""
        if not self.layer_enabled('working'):
            return
        run_id = run_id or self._latest_run(chat_id)
        if run_id:
            self.sync_working(run_id, chat_id)
            return
        if hasattr(self, 'learning') and self.store.query(
                'SELECT 1 FROM memory_watermarks WHERE key IN (?,?)', ('all', 'working:' + chat_id)):
            return
        previous = self.store.query(
            "SELECT payload FROM checkpoints WHERE chat_id=?", (chat_id,)
        )
        old = json.loads(previous[0]["payload"]) if previous else {}
        goals = old.get("recent_goals", [])
        if not goals or goals[-1] != goal[:350]:
            goals = (goals + [goal[:350]])[-3:]
        payload = {
            "current_goal": goal[:900],
            "recent_goals": goals,
            "phase": phase,
            "actions": [
                {
                    "kind": a["kind"],
                    "status": a["status"],
                    "path": a["payload"].get("path"),
                    "result": self.result_summary(a["payload"].get("result")),
                }
                for a in actions[-6:]
            ],
        }
        self.store.query(
            "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?)",
            (chat_id, json.dumps(payload), time.time()),
        )

    def sync_working(self, run_id, chat_id=None):
        """Persist-boundary hook: rewrite the run's working record from the
        journal. Nothing is written when memory or the working layer is off,
        the run lacked start-time consent or a clear/forget watermark covers
        it, or chat_id names a different conversation. Operational recovery
        never reads this record. Returns the stored payload or None."""
        if not self.layer_enabled('working'):
            return None
        derived = self._derive_working(run_id)
        if derived is None or chat_id is not None and derived[0] != chat_id:
            return None
        owner, payload = derived
        if hasattr(self, 'learning') and not self.learning.allowed_run(run_id, 'working'):
            return None
        previous = self.store.query("SELECT payload FROM checkpoints WHERE chat_id=?", (owner,))
        try:
            old = json.loads(previous[0]['payload']) if previous else {}
        except ValueError:
            old = {}
        goals = [g for g in old.get('recent_goals', []) if isinstance(g, str)] if isinstance(old, dict) else []
        if not goals or goals[-1] != payload['current_goal'][:350]:
            goals = (goals + [payload['current_goal'][:350]])[-3:]
        payload['recent_goals'] = goals
        self.store.query("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?)",
                         (owner, json.dumps(payload, ensure_ascii=False), time.time()))
        return payload

    def working_state(self, run_id):
        """The working record of one run, derived only from host journal
        rows: the exact goal (bounded, with a truncation flag), plan step IDs
        and statuses (never model-written titles), pending question, input and
        broker action IDs, uncertain receipts and the remaining step/second
        budget as of the last persist boundary. A pure read, available even
        when memory is off; None when the run is unknown."""
        derived = self._derive_working(run_id)
        return derived[1] if derived else None

    def _latest_run(self, chat_id):
        with self.store.lock:
            db = self.store.db
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
                return None
            row = db.execute('SELECT id FROM runs WHERE chat_id=? ORDER BY created DESC,rowid DESC LIMIT 1',
                             (chat_id,)).fetchone()
        return row[0] if row else None

    def _derive_working(self, run_id):
        with self.store.lock:
            db = self.store.db
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'runs' not in tables or not isinstance(run_id, str):
                return None
            run = db.execute('SELECT chat_id,goal,state,config,steps,elapsed FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run:
                return None

            def rows(table, sql):
                return db.execute(sql, (run_id,)).fetchall() if table in tables else []

            plan = rows('run_plans', 'SELECT revision,steps FROM run_plans WHERE run_id=?')
            questions = rows('run_questions', "SELECT id,required FROM run_questions WHERE run_id=? AND status='pending' "
                                              'ORDER BY created,id LIMIT %d' % WORKING_IDS)
            inputs = rows('run_inbox', "SELECT id FROM run_inbox WHERE run_id=? AND status='pending' "
                                       'ORDER BY sequence LIMIT %d' % WORKING_IDS)
            tools = rows('run_tools', 'SELECT call_id,name,state,result FROM run_tools WHERE run_id=? ORDER BY rowid DESC LIMIT 400')[::-1]
            owned = {r[0] for r in rows('run_action_owners', 'SELECT action_id FROM run_action_owners WHERE run_id=?')}
            receipts = []
            for call_id, name, tool_state, raw in tools:
                try:
                    result = json.loads(raw) if raw else None
                except ValueError:
                    result = None
                if isinstance(result, dict) and isinstance(result.get('action_id'), str):
                    owned.add(result['action_id'])
                status = result.get('status') if isinstance(result, dict) else None
                receipts.append({'receipt_id': call_id, 'tool': name, 'state': tool_state,
                                 'status': status if isinstance(status, str) and re.fullmatch(r'[a-z_]{1,40}', status) else None})
            # V1-079: only this run's own actions, by primary key; the chat's whole history is never read.
            actions, ids = [], sorted(owned)
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                actions += db.execute('SELECT id,kind,status,payload,created FROM actions WHERE chat_id=? AND id IN (%s)'
                                      % ','.join('?' * len(chunk)), (run[0], *chunk)).fetchall()
        actions.sort(key=lambda a: (a[4] is not None, a[4] or 0, a[0]))
        chat_id, goal, state, config, steps, elapsed = run
        mine = []
        for ident, kind, status, raw, _ in actions:
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {}
            payload = payload if isinstance(payload, dict) else {}
            path = payload.get('path')
            mine.append({'id': ident, 'kind': kind, 'status': status,
                         'path': path[:300] if isinstance(path, str) else None,
                         'result': self.result_summary(payload.get('result') if isinstance(payload.get('result'), dict) else None)})
        try:
            limits = json.loads(config or '{}').get('limits') or {}
        except (ValueError, AttributeError):
            limits = {}
        max_steps, max_seconds = limits.get('max_steps'), limits.get('max_seconds')
        number = (int, float)
        remaining = {
            'steps': max(0, max_steps - steps) if type(max_steps) is int and type(steps) is int else None,
            'seconds': round(max(0.0, max_seconds - elapsed), 1)
            if type(max_seconds) in number and type(elapsed) in number and math.isfinite(max_seconds - elapsed) else None,
            'as_of': 'last_persist_boundary'}
        plan_state = None
        if plan:
            try:
                listed = json.loads(plan[0][1])
            except ValueError:
                listed = []
            plan_steps = [{'id': s['id'], 'status': s['status'], 'evidence': len(s.get('evidence_ids') or [])}
                          for s in listed if isinstance(s, dict) and isinstance(s.get('id'), str)
                          and PLAN_ID.fullmatch(s['id']) and s.get('status') in PLAN_STATUSES][:20]
            plan_state = {'revision': plan[0][0], 'steps': plan_steps}
        goal = goal if isinstance(goal, str) else ''
        payload = {
            'schema': WORKING_SCHEMA, 'derived_from': 'host_journal', 'authorization': 'none',
            'run_id': run_id, 'run_state': state, 'phase': state,
            'current_goal': goal[:WORKING_GOAL_LIMIT], 'goal_chars': len(goal),
            'plan': plan_state,
            'pending_question_ids': [q[0] for q in questions],
            'required_question_pending': any(bool(q[1]) for q in questions),
            'pending_input_ids': [i[0] for i in inputs],
            'pending_action_ids': [a['id'] for a in mine if a['status'] in OPEN_ACTIONS][:WORKING_IDS],
            'uncertain_receipt_ids': [r['receipt_id'] for r in receipts if r['state'] in ('running', 'uncertain')][:WORKING_IDS],
            'remaining': remaining,
            'receipts': receipts[-WORKING_RECENT:],
            'actions': [{k: a[k] for k in ('kind', 'status', 'path', 'result')} for a in mine[-WORKING_RECENT:]],
        }
        if len(goal) > WORKING_GOAL_LIMIT:
            payload['goal_truncated'] = True
        return chat_id, payload

    @staticmethod
    def result_summary(result):
        if not result:
            return None
        return {
            k: result[k]
            for k in ("opened", "applied", "exit_code", "cancelled")
            if k in result
        }

    def episode(self, chat_id, goal, workspace, actions, outcome="completed", run_id=None):
        if self.layer_enabled('episodic') and not (
                run_id and hasattr(self, 'learning') and not self.learning.allowed_run(run_id)):
            self._record_episode(chat_id, goal, workspace, actions, outcome, run_id)
        # Settlement is a persist boundary for the working layer, which has
        # its own switch and consent; the episodic layer does not gate it.
        self.checkpoint(chat_id, goal, actions, outcome, run_id=run_id)

    def _record_episode(self, chat_id, goal, workspace, actions, outcome, run_id):
        from .context import encode_result
        evidence = [
            {
                "action_id": a["id"],
                "kind": a["kind"],
                "status": a["status"],
                "result": self.result_summary(a["payload"].get("result")),
            }
            for a in actions[-4:]
        ]
        ident = 'episode-' + run_id if run_id else None
        forgotten = ident and hasattr(self, 'learning') and self._forgotten(
            workspace or 'chat:' + chat_id, 'episodic', ident)
        if not forgotten:
            self.put(
                "episodic",
                encode_result(
                    {
                        "user_request": goal[:650],
                        "run_id": run_id,
                        "task_outcome": outcome,
                        "broker_evidence": evidence,
                        "note": "Completion is not proof of factual correctness.",
                    }, limit=4000
                ),
                scope=workspace or 'chat:' + chat_id,
                chat_id=chat_id,
                origin="conversation",
                ident=ident,
            )
        if run_id and hasattr(self, 'learning'):
            self.observe_outcomes(chat_id, workspace, run_id, actions, outcome)

    def observe_outcomes(self, chat_id, workspace, run_id, actions, outcome):
        """Bounded episodic observations of settled failures in one run.

        Built only from host receipts and broker records (failed/cancelled
        tests, declined proposals, failed approved actions, tool refusals).
        Each is labelled as a past outcome, never a directive, permission or
        replay plan. IDs are stable per conversation so a resumed run updates
        rather than duplicates them, and a forgotten one stays forgotten.

        The method checks its own boundary: the episodic layer must be on now
        and in the owning run's start-time consent (no clear/forget
        watermark), and chat/workspace must be that run's. Broker records are
        reloaded from this run's receipts; caller-supplied action dicts are
        not trusted."""
        from . import memory_facts
        from .memory_learning import fingerprint
        if not run_id or not hasattr(self, 'learning') or not self.layer_enabled('episodic'):
            return []
        try:
            run = self.learning.app.runs.get(run_id, chat_id)
        except ValueError:
            return []
        if run['config'].get('workspace') != workspace or not self.learning.allowed_run(run_id):
            return []
        rows = self.store.query('SELECT call_id,name,arguments,state,result FROM run_tools WHERE run_id=? LIMIT 400', (run_id,))
        receipts = []
        for row in rows:
            try:
                receipts.append({**row, 'arguments': json.loads(row['arguments'] or '{}'),
                                 'result': json.loads(row['result']) if row['result'] else None})
            except ValueError:
                continue
        referenced = {r['result'].get('action_id') for r in receipts if isinstance(r['result'], dict)}
        owned = self._actions(chat_id, referenced)
        scope = workspace or 'chat:' + chat_id
        stored = []
        for lesson in memory_facts.failure_observations(outcome, receipts, owned):
            ident = 'outcome-' + hashlib.sha256(json.dumps(
                [chat_id, scope, lesson['type'], lesson['target'], lesson['observation']]).encode()).hexdigest()[:32]
            content = json.dumps({'claim_class': 'observed_outcome', 'type': lesson['type'],
                'observation': lesson['observation'], 'run_id': run_id, 'observed_at': round(time.time(), 3),
                'receipt_ids': lesson['receipt_ids'],
                'reuse': 'Host-recorded past outcome; not an instruction, permission or replay plan. Check current evidence.'},
                ensure_ascii=False, separators=(',', ':'))
            if self._forgotten(scope, 'episodic', ident, fingerprint('episodic', scope, content)):
                continue
            stored.append(self.put('episodic', content, scope=scope, chat_id=chat_id, origin='conversation', ident=ident))
        return stored

    def _actions(self, chat_id, ids):
        """store.actions(chat_id) restricted to `ids`, read by primary key (V1-079)."""
        ids, found = sorted(i for i in ids if isinstance(i, str)), []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            found += self.store.query('SELECT * FROM actions WHERE chat_id=? AND id IN (%s)' % ','.join('?' * len(chunk)),
                                      (chat_id, *chunk))
        for row in found:
            row['payload'] = json.loads(row['payload'])
        return sorted(found, key=lambda a: (a['created'] is not None, a['created'] or 0, a['id']))

    def _forgotten(self, scope, kind, slot, fp=None):
        """Forgetting check for host records with stable IDs. Learning's
        check also covers salted project scopes and folded (Bloom) tombstones;
        the literal table query remains for older learning modules."""
        check = getattr(self.learning, 'forgotten', None)
        if callable(check):
            return bool(check(scope, kind, slot, fp))
        return bool(self.store.query('SELECT 1 FROM memory_tombstones WHERE scope=? AND kind=? AND (slot=? OR fingerprint=?)',
                                     (scope, kind, slot, fp)))

    def retrieve(self, text, chat_id, workspace=None, budget=2000):
        """Recalled records for one request (compatible list form)."""
        return self.recall(text, chat_id, workspace, budget)['items']

    def recall(self, text, chat_id, workspace=None, budget=None, *, window=None, ranker='v2',
               embedder=None, graph=None, now=None):
        """Ranked bounded recall with content-free diagnostics.

        Candidates come from bounded SQLite/FTS5 queries over enabled layers
        and the explicit scopes only. memory_retrieval ranks them with lexical,
        phrase/entity, provenance, scope and recency signals; unrelated
        standing preferences/procedures are not padded in. Learned records stay
        whole; a host note reports relevant records omitted for budget.

        budget is explicit characters (0-32000); otherwise `window` (model
        context tokens) scales it to about 6% clamped to 600-4,000, else 2,000.
        ranker 'v2' (default) adds the trigram pool, hybrid ranking, the
        relation filter (`graph`: a callback, False for none, default W3's
        memory_graph when installed) with a host conflict note, and the
        optional embedder (default: none). 'v1' is the Phase 11 ranker, kept
        for comparison. `now` pins the ranking clock for evaluation."""
        if not self.enabled():
            return {'items': [], 'diagnostics': {'enabled': False}}
        self.expire()
        budget, source = self._recall_budget(budget, window)
        if ranker not in RANKERS:
            raise ValueError('Choose recall ranker v1 or v2.')
        hybrid = ranker == 'v2'
        diagnostics = {'enabled': True, 'strategy': STRATEGIES[ranker], 'ranker': ranker,
                       'budget': budget, 'budget_source': source, 'used': 0}
        if window is not None:
            diagnostics['window'] = window
        if budget == 0:
            return {'items': [], 'diagnostics': {**diagnostics, 'records': []}}
        learned = hasattr(self, 'learning')
        policy = self.store.setting('permissions', {'tools': {'files': True, 'web': True}})
        files, web = policy['tools']['files'], policy['tools']['web']
        scopes = ("global", workspace or "global", "chat:" + chat_id)
        # Disabled layers are never queried, so they cannot leak into ranking
        # or diagnostics.
        kinds = [k for k in KINDS if k != 'working' and self.layer_enabled(k)]
        query = (memory_retrieval.HybridQuery if hybrid else memory_retrieval.Query)(text)
        rows, pools, trigram = self._recall_candidates(query, scopes, kinds, learned, hybrid)
        records, decisions, seen = [], {}, set()
        for row in rows:
            if row['id'] in seen:
                continue
            seen.add(row['id'])
            proof = json.loads(row.get('proof') or '{}')
            if proof.get('root_identity') and not files or proof.get('evidence_class') in memory_retrieval.WEB_EVIDENCE and not web:
                decisions[row['id']] = 'permission'
                continue
            records.append({**row, 'proof': proof})
        report = {}
        embedder = (self.embedder if embedder is None else embedder) if hybrid else None
        relation = self._graph_filter(graph, scopes) if hybrid else None
        # v2 admits records whose learning status is already 'contested' so
        # they can be replaced by a conflict note; they are always withheld,
        # with or without a relation filter.
        disputed = {r['id'] for r in records if r.get('evidence_status') == 'contested'}

        def relations(ids):
            verdicts = {ident: 'contested' for ident in ids if ident in disputed}
            if relation is not None:
                verdicts.update(relation(ids))
            return verdicts

        ranked = memory_retrieval.rank(query, records, now=time.time() if now is None else now, chat_id=chat_id,
                                       workspace=workspace, graph=relations if relation or disputed else None,
                                       embedder=embedder, report=report)
        if relation is None and report.get('graph') == 'callback':
            report['graph'] = 'status_only'
        relevant = [e for e in ranked if e['relevant']]
        # Only the leading file-derived records are re-hashed; the audit itself
        # caps records and bytes, and anything unaudited is withheld.
        sourced = [e['record']['id'] for e in relevant if e['record']['proof'].get('root_identity')][:32]
        audited = self.learning.audit(workspace, files, sourced) if learned and workspace and sourced else set()
        candidates, contents = [], set()
        # Withheld matches are replaced by content-free host notes, first so a
        # small budget keeps them.
        withheld = [e for e in ranked if 'withheld' in e]
        contested = sum(1 for e in withheld if e['match'] and memory_retrieval.conflicted(e['withheld']))
        unchecked = sum(1 for e in withheld if e['match'] and e['withheld'] == memory_retrieval.GRAPH_UNAVAILABLE)
        if contested:
            note = memory_retrieval.conflict_note(contested)
            if learned:
                # Name the contested records (content-free IDs) so the person can resolve them natively.
                ids = [e['record']['id'] for e in withheld if e['match'] and memory_retrieval.conflicted(e['withheld'])]
                try:
                    named = self.learning.conflict_notes(ids, list(scopes))
                except Exception:
                    named = []  # The count-only note stands.
                if named:
                    note['memory_ids'] = sorted({i for n in named for i in n['memory_ids']})[:8]
                if named and (window is None or window > 4096):
                    try:
                        # V1-035: when both sides are the person's own saved notes, their values are shown as
                        # disputed, never as current; learned contests stay content-free, as does every
                        # note at a 4K window (nothing new at a budget <= 4096).
                        disputed = self.learning.disputed(named)
                    except Exception:
                        disputed = []
                    if disputed:
                        note['disputed'] = disputed
            candidates.append(('note:conflict', note))
        if unchecked:
            candidates.append(('note:unchecked', memory_retrieval.unchecked_note(unchecked)))
        working = self._working_item(chat_id, budget, text) if self.layer_enabled('working') else None
        if working:
            candidates.append(('working', working))
        pinned = len(candidates)
        for entry in relevant:
            row = entry['record']
            if row['proof'].get('root_identity') and row['id'] not in audited:
                decisions[row['id']] = 'source_not_current'
                continue
            normal = ' '.join(row['content'].casefold().split())
            if normal in contents:
                decisions[row['id']] = 'duplicate_content'
                continue
            contents.add(normal)
            candidates.append((row['id'], self._recall_item(row)))
        chosen, omitted, used = memory_retrieval.pack(candidates, budget, pinned)
        selected_ids = {ident for ident, _ in chosen}
        for ident in omitted:
            decisions[ident] = 'omitted_for_budget'
        for ident in selected_ids:
            decisions[ident] = 'selected'
        diagnostics.update(used=used, query=query.summary(), pools=pools, considered=len(records),
            relevant=len(relevant), selected=len([i for i in selected_ids
                                                  if i not in (None, 'working') and not str(i).startswith('note:')]),
            omitted_for_budget=len(omitted), records=[{
                'id': e['record']['id'], 'kind': e['record']['kind'], 'origin': e['record']['origin'],
                'score': round(e['score'], 3), 'signals': e['signals'],
                'decision': decisions.get(e['record']['id'], e['reason']),
                **({'match': e['match']} if 'withheld' in e else {})} for e in ranked]
            + [{'id': ident, 'decision': 'permission'} for ident, why in decisions.items() if why == 'permission'])
        if hybrid:
            diagnostics.update(trigram=trigram, graph=report.get('graph', 'none'),
                               embedder=report.get('embedder', 'none'), withheld=len(withheld),
                               conflict_note='note:conflict' in selected_ids)
            if report.get('embedder') not in (None, 'none', 'error'):
                diagnostics['strategy'] = ('hybrid lexical/trigram/metadata ranking over bounded SQLite candidates '
                                           'with optional embedding reranking; no generative model')
        return {'items': [item for _, item in chosen], 'diagnostics': diagnostics}

    @staticmethod
    def _recall_budget(budget, window):
        if budget is not None:
            if type(budget) is not int or not 0 <= budget <= 32000:
                raise ValueError('Use a memory recall budget from 0 to 32000 characters.')
            return budget, 'explicit'
        if window is not None:
            return memory_retrieval.recall_budget(window), 'window'
        return DEFAULT_RECALL_BUDGET, 'default'

    def _working_item(self, chat_id, budget, request):
        """Pinned working view. A journal-derived record is re-derived at
        read time so plan, question, action and budget state are current."""
        rows = self.store.query("SELECT payload FROM checkpoints WHERE chat_id=?", (chat_id,))
        if not rows:
            return None
        try:
            state = json.loads(rows[0]['payload'])
        except ValueError:
            return None
        if not isinstance(state, dict):
            return None
        if state.get('schema') == WORKING_SCHEMA:
            derived = self._derive_working(state.get('run_id'))
            if derived and derived[0] == chat_id:
                state = {**derived[1], 'recent_goals': state.get('recent_goals', [])}
        return {"kind": "working", "origin": "broker",
                "content": json.dumps(working_view(state, budget, request), ensure_ascii=False)}

    def _graph_filter(self, graph, scopes=None):
        """The relation filter for v2 recall: an explicit callback, False for
        none, else Memory.graph, else W3's memory_graph.withheld(db, ids)
        (given the recall scopes when it accepts `scopes`) when that module is
        installed and learning tables exist. A broken module fails closed: the
        callback raises and ranking withholds every candidate with a note."""
        if graph is False:
            return None
        if graph is not None:
            return graph
        if self.graph is not None:
            return self.graph or None
        name = __package__ + '.memory_graph'
        if not hasattr(self, 'learning') or importlib.util.find_spec(name) is None:
            return None
        try:
            withheld = getattr(importlib.import_module(name), 'withheld', None)
            scoped = callable(withheld) and 'scopes' in inspect.signature(withheld).parameters
        except Exception:
            withheld, scoped = None, False

        def check(ids):
            if not callable(withheld):
                raise RuntimeError('The memory relation filter is unavailable.')
            with self.store.lock:
                if scoped and scopes is not None:
                    return withheld(self.store.db, ids, scopes=list(scopes))
                return withheld(self.store.db, ids)
        return check

    def trigram_mode(self):
        """'fts5' when this SQLite has the trigram tokenizer and the
        connection-local index is enabled, else 'python'."""
        with self.store.lock:
            db = self.store.db
            return 'fts5' if self.trigram_fts and not db.in_transaction and self._trigram_ready(db) else 'python'

    def discard_recall_index(self):
        """Empty the connection-local trigram index (never persisted). Recall
        re-syncs it from current rows; forget/clear paths may call this so
        removed text does not wait for the next recall to leave the index."""
        with self.store.lock:
            db = self.store.db
            self._trigram_index.clear()
            self._trigram_ids.clear()
            if self._trigram:
                self._trigram = None  # dropped and rebuilt before its next use
                if not db.in_transaction:
                    try:
                        db.execute('DROP TABLE IF EXISTS temp.memory_trigram')
                    except sqlite3.Error:
                        pass

    def _trigram_ready(self, db):
        """Probe once per connection; the caller is outside a transaction."""
        if self._trigram is None:
            self._trigram_index.clear()
            self._trigram_ids.clear()
            try:
                db.execute('DROP TABLE IF EXISTS temp.memory_trigram')
                db.execute(TRIGRAM_TABLE)
                self._trigram = True
            except sqlite3.Error:
                self._trigram = False
        elif self._trigram and not db.execute("SELECT 1 FROM temp.sqlite_master WHERE name='memory_trigram'").fetchone():
            self._trigram = None
            return self._trigram_ready(db)
        return self._trigram

    def _trigram_window(self, db, where, args, content):
        """The most recent eligible records up to 1 MB (UTF-8) and 8,000 rows."""
        cursor = db.execute('SELECT m.id,m.updated,length(CAST(m.content AS BLOB))' + (',m.content' if content else '')
                            + where + ' ORDER BY m.updated DESC,m.id', args)
        window, texts, total = {}, {}, 0
        try:
            while True:
                batch = cursor.fetchmany(256)
                if not batch:
                    break
                for row in batch:
                    if total + row[2] > memory_retrieval.TRIGRAM_SCAN_BYTES or len(window) >= memory_retrieval.TRIGRAM_SCAN_ROWS:
                        return window, texts, total
                    total += row[2]
                    window[row[0]] = row[1]
                    if content:
                        texts[row[0]] = memory_retrieval.search_text({'content': row[3]})
        finally:
            cursor.close()
        return window, texts, total

    def _trigram_sync(self, db, window):
        """Mirror the window into the connection-local FTS5 trigram table."""
        for ident in [i for i, (_, updated) in self._trigram_index.items() if window.get(i) != updated]:
            rowid = self._trigram_index.pop(ident)[0]
            self._trigram_ids.pop(rowid, None)
            db.execute('DELETE FROM temp.memory_trigram WHERE rowid=?', (rowid,))
        missing = [i for i in window if i not in self._trigram_index]
        for start in range(0, len(missing), 256):
            chunk = missing[start:start + 256]
            for ident, content in db.execute('SELECT id,content FROM memories WHERE id IN (%s)' % ','.join('?' * len(chunk)), chunk).fetchall():
                rowid = db.execute('INSERT INTO temp.memory_trigram(body) VALUES(?)',
                                   (memory_retrieval.search_text({'content': content}),)).lastrowid
                self._trigram_index[ident] = (rowid, window[ident])
                self._trigram_ids[rowid] = ident
        if db.in_transaction:
            db.commit()

    def _trigram_pool(self, query, where, args, extra_select, exclude):
        """Substring-key candidates from the recent window: an FTS5 trigram
        index when available, else a Python scan of the same window. Both see
        the same text and keys, so they nominate the same records."""
        keys = query.keys
        with self.store.lock:
            db = self.store.db
            mode = 'fts5' if self.trigram_fts and not db.in_transaction and self._trigram_ready(db) else 'python'
            hits = {}
            if mode == 'fts5':
                try:
                    window, _, total = self._trigram_window(db, where, args, False)
                    self._trigram_sync(db, window)
                    for key in keys:
                        for (rowid,) in db.execute('SELECT rowid FROM temp.memory_trigram WHERE memory_trigram MATCH ?',
                                                   ('"%s"' % key,)).fetchall():
                            ident = self._trigram_ids.get(rowid)
                            if ident in window:
                                hits.setdefault(ident, set()).add(key)
                except sqlite3.Error:
                    # Degrade cleanly for the rest of this session.
                    if db.in_transaction:
                        db.rollback()
                    self._trigram = False
                    self._trigram_index.clear()
                    self._trigram_ids.clear()
                    try:
                        db.execute('DROP TABLE IF EXISTS temp.memory_trigram')
                    except sqlite3.Error:
                        pass
                    mode, hits = 'python', {}
            if mode == 'python':
                window, texts, total = self._trigram_window(db, where, args, True)
                for ident, text in texts.items():
                    found = memory_retrieval.key_hits(text, keys)
                    if found:
                        hits[ident] = found
            selected = memory_retrieval.trigram_select(hits, window, len(window), exclude)
            found = []
            if selected:
                cursor = db.execute('SELECT m.*' + extra_select + where + ' AND m.id IN (%s)' % ','.join('?' * len(selected)),
                                    (*args, *selected))
                names = [d[0] for d in cursor.description]
                by_id = {row[0]: dict(zip(names, row)) for row in cursor.fetchall()}
                found = [by_id[i] for i in selected if i in by_id]
        return found, {'mode': mode, 'window': len(window), 'bytes': total}

    def _recall_candidates(self, query, scopes, kinds, learned, hybrid=False):
        """Bounded candidate pools: standing preferences/procedures, an FTS OR
        query, an FTS phrase/entity query and (v2) a trigram substring pool
        over at most 1 MB of recent records. Every pool has a LIMIT."""
        pools = {}
        if not kinds:
            return [], pools, None
        join = ' LEFT JOIN memory_evidence e ON e.memory_id=m.id' if learned else ''
        valid = " AND (e.status IS NULL OR e.status='active')" if learned else ''
        extra = ',e.proof,e.slot' if learned else ''
        if learned and hybrid:
            # Contested records are ranked only to be withheld behind a note.
            valid = " AND (e.status IS NULL OR e.status IN ('active','contested'))"
            extra += ',e.status AS evidence_status'
        rows = []
        standing = [k for k in ('profile', 'procedural') if k in kinds]
        if standing:
            found = self.store.query(
                "SELECT m.*" + extra + " FROM memories m" + join + " WHERE m.scope IN (?,?,?) AND m.kind IN ("
                + ','.join('?' * len(standing)) + ")" + valid
                + " ORDER BY CASE m.origin WHEN 'user' THEN 0 WHEN 'direct-user' THEN 0 ELSE 1 END,m.updated DESC LIMIT ?",
                (*scopes, *standing, memory_retrieval.STANDING_LIMIT))
            pools['standing'] = len(found)
            rows += found
        marks = ','.join('?' * len(kinds))
        for name, match, limit in (('lexical', query.fts_any(), memory_retrieval.LEXICAL_LIMIT),
                                   ('phrase', query.fts_phrases(), memory_retrieval.PHRASE_LIMIT)):
            if not match:
                continue
            try:
                found = self.store.query(
                    "SELECT m.*,bm25(memory_search) AS rank" + extra + " FROM memory_search JOIN memories m ON m.id=memory_search.id"
                    + join + " WHERE memory_search MATCH ? AND m.scope IN (?,?,?) AND m.kind IN (" + marks + ")" + valid
                    + " ORDER BY rank,m.updated DESC LIMIT ?",
                    (match, *scopes, *kinds, limit))
            except sqlite3.OperationalError:
                found, pools[name + '_error'] = [], True
            pools[name] = len(found)
            rows += found
        trigram = None
        if hybrid and query.keys:
            where = " FROM memories m" + join + " WHERE m.scope IN (?,?,?) AND m.kind IN (" + marks + ")" + valid
            found, trigram = self._trigram_pool(query, where, (*scopes, *kinds), extra, {r['id'] for r in rows})
            pools['trigram'] = len(found)
            rows += found
        return rows, pools, trigram

    @staticmethod
    def _recall_item(row):
        item = {k: row[k] for k in ("id", "kind", "origin", "content", "updated", "expires")}
        proof = row.get('proof') or {}
        if proof:
            item['evidence_class'] = proof.get('evidence_class')
            item['receipt_ids'] = proof.get('receipt_ids', [])
            item['authorization'] = 'none'
        elif len(item['content']) > 600 and not row['id'].startswith('outcome-'):
            # Outcome observations are host-bounded JSON and stay whole.
            item['content'] = item['content'][:600] + '\n[Memory excerpt; inspect the complete record before relying on omissions.]'
        return item

    def forget_chat(self, cid):
        """A deleted conversation's memory leaves with it, except the global and
        project records the person saved in it with "Remember that ...": those
        are kept and unlinked (chat_id NULL), as Settings promises (V1-021)."""
        if hasattr(self, 'learning'):
            self.learning.clear(cid)
        else:
            from .memory_learning import KEEP_PERSON
            self.store.query(KEEP_PERSON, (cid,))
        for table in ("memories", "checkpoints", "metrics"):
            self.store.query(f"DELETE FROM {table} WHERE chat_id=?", (cid,))
        self.discard_recall_index()
        self.compact_search()

    def clear(self):
        if hasattr(self, 'learning'):
            result = self.learning.clear()
        else:
            self.store.query("DELETE FROM memories")
            self.store.query("DELETE FROM checkpoints")
            result = True
        self.discard_recall_index()
        self.compact_search()
        return result

    def compact_search(self):
        """Merge the memory_search FTS index into one segment, which drops the
        entries of deleted and forgotten records from its segment blocks
        (V1-057). Run after a forget batch; False when called inside another
        transaction (nothing is merged then; the next batch merges)."""
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                return False
            db.execute("INSERT INTO memory_search(memory_search) VALUES('optimize')")
            db.commit()
        return True

    reply_claims_forgetting = staticmethod(reply_claims_forgetting)
    forget_request = staticmethod(forget_request)

    def forget_candidates(self, text, chat=None, limit=5):
        """Stored records matching a conversational forget request, for the
        person's native Forget card (V1-034). `text` is the message ('Please
        forget my name.') or a plain subject; `chat` is a conversation ID whose
        scopes (global, its project, the conversation) bound the search, or None
        for every scope. A request without a subject ('Forget that.') returns
        the records the person saved in that conversation, newest first.

        A read: nothing is forgotten here, no model is involved and the result
        carries no authority; memory_delete stays the only forgetting route.
        Superseded versions are not listed: forgetting their current version
        removes them."""
        if type(limit) is not int or not 1 <= limit <= FORGET_LIMIT:
            raise ValueError('Show 1 to 20 memories.')
        if not isinstance(text, str) or len(text) > MAX_FORGET_TEXT:
            return []
        subject = forget_request(text)
        subject = text.strip() if subject is None else subject
        scopes, cid = None, None
        if chat is not None:
            rows = self.store.query('SELECT id,workspace FROM chats WHERE id=?', (chat,))
            if not rows:
                return []
            cid = rows[0]['id']
            scopes = ('global', rows[0]['workspace'] or 'global', 'chat:' + cid)
        learned = hasattr(self, 'learning')
        join = ' LEFT JOIN memory_evidence e ON e.memory_id=m.id' if learned else ''
        live = " AND (e.status IS NULL OR e.status<>'superseded')" if learned else ''
        within = ' AND m.scope IN (?,?,?)' if scopes else ''
        person = "CASE WHEN m.origin IN ('user','direct-user') THEN 0 ELSE 1 END"
        match = memory_retrieval.Query(subject).fts_any()
        self.expire()
        if not match:
            if cid is None:
                return []
            found = self.store.query(
                'SELECT m.* FROM memories m' + join + " WHERE m.chat_id=? AND m.origin IN ('user','direct-user')" + live
                + ' ORDER BY m.updated DESC,m.rowid DESC LIMIT ?', (cid, limit))
        else:
            try:
                found = self.store.query(
                    'SELECT m.* FROM memory_search JOIN memories m ON m.id=memory_search.id' + join
                    + ' WHERE memory_search MATCH ?' + within + live
                    + ' ORDER BY ' + person + ',bm25(memory_search),m.updated DESC LIMIT ?',
                    (match, *(scopes or ()), limit))
            except sqlite3.OperationalError:
                found = []
        return [self._forget_item(row) for row in found]

    def forget_preview(self, text=None, ids=None, chat=None, limit=5):
        """The Forget card's records: by request text (forget_candidates) or by
        memory IDs the card already shows. {'items', 'subject', 'authorization'}."""
        if ids is not None:
            if not isinstance(ids, list) or not 1 <= len(ids) <= FORGET_LIMIT or not all(
                    isinstance(i, str) and 0 < len(i) <= 100 for i in ids):
                raise ValueError('Choose 1 to 20 memories.')
            rows = self.store.query('SELECT * FROM memories WHERE id IN (%s)' % ','.join('?' * len(ids)), tuple(ids))
            order = {ident: index for index, ident in enumerate(ids)}
            items = [self._forget_item(row) for row in sorted(rows, key=lambda r: order[r['id']])]
            return {'items': items, 'subject': None, 'authorization': 'none'}
        if not isinstance(text, str):
            raise ValueError('Say what to forget.')
        return {'items': self.forget_candidates(text, chat, limit), 'subject': forget_request(text),
                'authorization': 'none'}

    @staticmethod
    def _forget_item(row):
        text = memory_retrieval.searchable(row)
        scope = row['scope']
        return {'id': row['id'], 'kind': row['kind'], 'layer': KINDS.get(row['kind'], row['kind']),
                'content': text if len(text) <= 300 else text[:299] + '…', 'origin': row['origin'],
                'where': 'all' if scope == 'global' else 'conversation' if scope.startswith('chat:') else 'project',
                'updated': row['updated'], 'authorization': 'none'}
