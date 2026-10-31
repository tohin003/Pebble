"""Bounded, evidence-backed personalization; never an authorization service.

Candidates contain exact human/source quotations, host recipe guidance or
lessons built from linked receipts. Promotion uses owned receipts, not model
confidence. Source facts are whole lines or sentences re-checked against the
current file, root and hash, with their span. Known recipes (test, build,
lint and typecheck) need two distinct successful root runs with the same
recipe, argv, root and manifest/lock binding, and go stale when any of them
changes. All writes use the existing SQLite writer; no maintenance model,
background socket or executable recipe is started.

A second bounded stage, consolidate(), keeps the relationship graph
(memory_graph), the tombstone lifecycle and host lessons current. Each of its
steps is one transaction, so a crash leaves the previous state or the next one,
never a partial or repeated promotion.
"""

import json
import math
import re
import time
from pathlib import Path

from . import memory, memory_facts, memory_graph, recipes
from .policy import MAX_FILE, PolicyError, Workspace, digest
from .recipes import REGISTRY

KINDS = ('semantic', 'procedural', 'temporal', 'profile', 'episodic')
MAX_PENDING = 1000
MAX_PER_RUN = 32
MAX_LEARNED = 1500
MAX_RETAINED_CANDIDATES = 3000
MAX_BATCH = 16
MAX_TTL = 7 * 86400
MAX_AUDIT_BYTES = 2_000_000
MAX_SCANNED_RECEIPTS = 64
MAX_OUTCOME_RECEIPTS = 8
MAX_READ_RECEIPTS = 24
MAX_LESSON_CHARS = 400
MAX_LESSON_RUNS = 24
MAX_LESSONS_PER_RUN = 2
MAX_RUN_RECEIPTS = 400
MAX_CONSOLIDATION = 64
MAX_TOMBSTONES_PER_SCOPE = 256
FOLD_KEEP = 128
PREFERENCE = memory_facts.PREFERENCE
LEGACY_FILE_SLOT = re.compile(r'(file:[0-9a-f]{24}):.+')
RECIPE_TOOLS = ('RunTests', 'RunRecipe')
EDIT_TOOLS = ('Edit', 'Write', 'Append')
UNSETTLED = frozenset({'running', 'queued', 'retry_wait', 'waiting_approval', 'waiting_input', 'paused',
                       'interrupted', 'budget_exhausted'})
AWAITING = 'awaiting_native_confirmation'
CONTEST_DECISIONS = ('keep', 'reject', 'keep_both')
# Phase 46 (1.9.28, F2; the 1.9.26 dev cell d-me-01 k3: two bare "unsupported" results, then "FINAL: blocked — the
# memory save came back unsupported", graded format_only): a profile proposal that was not saved says why and what to do.
# Phase 46 (1.9.29, w22-memory-r2): the rule as it now is: the sentences and list items right after that sentence that
# spell out what to keep count too (memory._spec_after), never this reply's own instructions or anything for now
NOT_SAVED_NOTE = ("Not saved for later sessions. Pebble keeps the person's own words only from the sentence of their "
                  "message that asks to remember them and the sentences or list items right after it that spell out what "
                  "to keep, and never a task, this reply's own instructions or anything for now: quote one whole "
                  "sentence, clause or list item of them exactly (kind profile), one per call. What Pebble already saved "
                  "from this message needs no call. If it cannot be saved, say plainly that it was not saved, and finish "
                  "the rest of the task.")
WAITING_NOTE = ("Not saved yet: it waits for the person's confirmation in Settings → Memory. Do not say it was saved; "
                "finish the rest of the task.")
# Deleting a conversation keeps the global and project records the person saved in it with
# "Remember that ..." (V1-021) and unlinks them. Settings and seed rows (origin 'user') keep
# their 0.6.1 coupling: test_companion pins that a chat-linked one leaves with its chat.
KEEP_PERSON = ("UPDATE memories SET chat_id=NULL WHERE chat_id=? AND origin='direct-user' "
               "AND scope NOT LIKE 'chat:%'")


class DeferredEvidence(PolicyError):
    pass


class AwaitingConfirmation(PolicyError):
    """An exact human quote outside the preference grammar: native confirmation decides."""


def packed(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def normalized(content):
    return ' '.join(content.casefold().split())


def fingerprint(kind, scope, content):
    return digest(packed([kind, scope, normalized(content)]).encode())


def tombstone_slots(kind, slot):
    """A per-file source tombstone (the pre-fact slot shape) still covers
    every fact slot later learned from that file."""
    match = LEGACY_FILE_SLOT.fullmatch(slot or '') if kind == 'semantic' else None
    return (slot, match[1]) if match else (slot, slot)


def lesson_signature(kind, signature):
    """Candidate content of a host lesson: its failure signature, nothing else."""
    return packed({'type': kind, 'signature': signature})


def recipe_slot(recipe):
    """Procedure slot of a registry recipe ('test:go-test', 'build:npm-build',
    'typecheck:npm-typecheck'). Test slots are unchanged since Phase 15."""
    return recipes.procedure(recipe) + ':' + recipe


def recipe_evidence(procedure):
    """Test recipes keep their Phase 15 evidence class; build, lint and
    typecheck recipes are observed project recipes."""
    return 'observed_test_recipe' if procedure == 'test' else 'observed_project_recipe'


def recipe_guidance(recipe, argv_digest):
    """Host-constructed 'known recipe' data. No larger than the Phase 15 test
    guidance for any registry recipe, so small windows recall nothing bigger;
    its searchable fields (recipe, prerequisites) keep every Phase 15 word."""
    spec, kind = REGISTRY[recipe], recipes.procedure(recipe)
    return packed({'recipe': recipe, 'recipe_kind': kind, 'language': spec.language, 'argv_digest': argv_digest[:16],
        'prerequisites': 'Same project root, pinned recipe and manifests; inspect changed source.',
        'expected_evidence': 'Fresh receipt: exit 0, not cancelled' + (', passing tests.' if kind == 'test' else '.'),
        'reuse': 'Known recipe, data only: nothing runs from memory. Each run needs a fresh plan, review and permission. '
                 'Past success proves no future result.'})


class Learning:
    def __init__(self, app):
        self.app, self.store = app, app.store
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Initialize learning outside an application transaction.')
            db.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS memory_candidates(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, chat_id TEXT NOT NULL,
              kind TEXT NOT NULL, scope TEXT NOT NULL, slot TEXT NOT NULL,
              content TEXT NOT NULL, fingerprint TEXT NOT NULL, evidence TEXT NOT NULL,
              input_id TEXT, expires REAL, supersedes TEXT, status TEXT NOT NULL,
              reason TEXT, memory_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
              validated_proof TEXT);
            CREATE INDEX IF NOT EXISTS memory_candidates_queue ON memory_candidates(status,created);
            CREATE INDEX IF NOT EXISTS memory_candidates_run ON memory_candidates(run_id);
            CREATE TABLE IF NOT EXISTS memory_evidence(
              memory_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, run_id TEXT NOT NULL,
              chat_id TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL,
              slot TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
              proof TEXT NOT NULL, superseded_by TEXT, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS memory_evidence_slot ON memory_evidence(scope,kind,slot,status);
            CREATE TABLE IF NOT EXISTS memory_tombstones(
              scope TEXT NOT NULL, kind TEXT NOT NULL, slot TEXT NOT NULL,
              fingerprint TEXT NOT NULL, created REAL NOT NULL,
              PRIMARY KEY(scope,kind,slot,fingerprint));
            CREATE TABLE IF NOT EXISTS memory_watermarks(key TEXT PRIMARY KEY,through REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_run_consent(
              run_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,enabled INTEGER NOT NULL,layers TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_recipe_failures(
              scope TEXT NOT NULL,recipe TEXT NOT NULL,through REAL NOT NULL,
              PRIMARY KEY(scope,recipe));
            ''' + memory_graph.GRAPH_DDL + '''
            COMMIT;
            ''')
            if 'validated_proof' not in {r[1] for r in db.execute('PRAGMA table_info(memory_candidates)')}:
                db.execute('ALTER TABLE memory_candidates ADD COLUMN validated_proof TEXT')
                db.commit()
            now = time.time()
            db.execute("INSERT OR IGNORE INTO memory_watermarks VALUES('learning-installed',?)", (now,))
            # Tombstones key project scopes with this installation's salt.
            if not db.execute("SELECT 1 FROM memory_scope_salts WHERE origin='local'").fetchone():
                salt = memory_graph.new_salt()
                db.execute("INSERT INTO memory_scope_salts VALUES(?,?,'local',?)", (memory_graph.salt_id(salt), salt, now))
            memory_graph.migrate_supersedes(db, now)
            db.commit()
        app.memory.learning = self

    def on(self, kind=None, db=None):
        with self.store.lock:
            db = db or self.store.db
            rows = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('memory_enabled','memory_layers')"))
            return json.loads(rows.get('memory_enabled', 'true')) and (kind is None or
                json.loads(rows.get('memory_layers', '{}')).get(kind, True))

    def outside_transaction(self):
        with self.store.lock:
            if self.store.db.in_transaction:
                raise PolicyError('Learning requires a boundary outside an application transaction.')

    def salts(self, db=None):
        """This installation's salt first, then salts registered by imports."""
        with self.store.lock:
            db = db or self.store.db
            return [row[0] for row in db.execute("SELECT salt FROM memory_scope_salts ORDER BY origin='imported',created,salt_id")]

    def _forgotten(self, db, scope, kind, slot=None, fp=None):
        """Tombstone rows (legacy literal or salted scope) or the scope's Bloom filter."""
        keys = memory_graph.scope_keys(scope, self.salts(db))
        slots = tombstone_slots(kind, slot) if slot else ()
        clauses = (['slot IN (?,?)'] if slots else []) + (['fingerprint=?'] if fp else [])
        if not clauses:
            return False
        if db.execute('SELECT 1 FROM memory_tombstones WHERE scope IN (' + ','.join('?' * len(keys)) + ') AND kind=? AND ('
                      + ' OR '.join(clauses) + ') LIMIT 1', (*keys, kind, *slots, *([fp] if fp else []))).fetchone():
            return True
        return memory_graph.bloom_forgotten(db, keys, kind, slots, fp)

    def forgotten(self, scope, kind, slot=None, fingerprint=None):
        """Whether forgetting covers this record; the check memory.py episodes use."""
        with self.store.lock:
            return self._forgotten(self.store.db, scope, kind, slot, fingerprint)

    def _tombstone(self, db, scope, kind, slot, fp, now):
        db.execute('INSERT OR REPLACE INTO memory_tombstones VALUES(?,?,?,?,?)',
                   (memory_graph.scope_key(scope, self.salts(db)[0]), kind, slot, fp, now))

    def begin_run(self, rid):
        self.outside_transaction()
        run = self.app.runs.get(rid)
        with self.store.lock, self.store.db as db:
            installed = db.execute("SELECT through FROM memory_watermarks WHERE key='learning-installed'").fetchone()[0]
            settings = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('memory_enabled','memory_layers')"))
            enabled = bool(json.loads(settings.get('memory_enabled', 'true'))) and run['created'] >= installed
            db.execute('INSERT OR IGNORE INTO memory_run_consent VALUES(?,?,?,?)', (rid, run['chat_id'], enabled, settings.get('memory_layers', '{}')))

    def consent(self, rid, kind):
        with self.store.lock:
            row = self.store.db.execute('SELECT enabled,layers FROM memory_run_consent WHERE run_id=?', (rid,)).fetchone()
        return bool(row and row[0] and json.loads(row[1]).get(kind, True))

    def disable_run(self, rid, layers=None):
        if not rid:
            return
        if layers is None:
            self.store.query('UPDATE memory_run_consent SET enabled=0 WHERE run_id=?', (rid,))
        else:
            with self.store.lock, self.store.db as db:
                row = db.execute('SELECT layers FROM memory_run_consent WHERE run_id=?', (rid,)).fetchone()
                if row:
                    current = json.loads(row[0])
                    current.update({k: False for k, v in layers.items() if not v})
                    db.execute('UPDATE memory_run_consent SET layers=? WHERE run_id=?', (packed(current), rid))

    def _human(self, run, input_id=None):
        if input_id is None:
            return run['goal']
        rows = self.store.query("SELECT origin,payload FROM run_inbox WHERE run_id=? AND id=?", (run['id'], input_id))
        if not rows or rows[0]['origin'] != 'human':
            raise PolicyError('Memory input must be a human-origin record in this run.')
        payload = json.loads(rows[0]['payload'])
        return payload.get('text', '') or '\n'.join(a.get('text', '') for a in payload.get('answers', {}).values() if 'option_id' not in a)

    def propose(self, rid, kind, content='', key='', evidence_ids=None, input_id=None, expires=None, supersedes=None):
        self.outside_transaction()
        if kind not in KINDS or not isinstance(content, str) or len(content) > 1000:
            raise PolicyError('Propose a supported memory layer with at most 1000 characters.')
        if kind == 'procedural':
            content = ''  # The host constructs every reusable step.
        if not isinstance(key, str) or not re.fullmatch(r'[\w:./ -]{0,80}', key):
            raise PolicyError('Use a short memory key without control characters.')
        evidence_ids = [] if evidence_ids is None else evidence_ids
        if not isinstance(evidence_ids, list) or len(evidence_ids) > 8 or any(
            not isinstance(e, str) or not 1 <= len(e) <= 320 for e in evidence_ids):
            raise PolicyError('Use at most eight owned receipt IDs.')
        if input_id is not None and (not isinstance(input_id, str) or not 1 <= len(input_id) <= 100):
            raise PolicyError('Use a bounded human input ID.')
        if supersedes is not None and (not isinstance(supersedes, str) or not 1 <= len(supersedes) <= 100):
            raise PolicyError('Use a bounded memory ID.')
        if kind == 'episodic' and (not content.strip() or len(content) > MAX_LESSON_CHARS or not evidence_ids
                                   or input_id is not None or supersedes is not None or expires is not None):
            raise PolicyError('A lesson needs at most 400 characters and the owned receipt IDs it rests on.')
        now = time.time()
        if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires) or not now < expires <= now + MAX_TTL):
            raise PolicyError('Automatic validity must end within seven days.')
        if kind == 'temporal' and expires is None:
            raise PolicyError('Temporal proposals require an expiry.')
        if memory_facts.credential_shaped(content):
            raise PolicyError('Recognizable credentials cannot enter automatic memory.')
        run = self.app.runs.get(rid)
        if kind == 'semantic' and not evidence_ids and memory.requested_quote(content, self._human(run, input_id)):
            # Phase 46 (1.9.28, F2): "my manager is Jordan Lee." proposed as a fact has no file to quote; it is the
            # person's own words they asked to keep, a profile record (the receipt-backed fact path is unchanged)
            kind = 'profile'
        self.begin_run(rid)
        if not self.consent(rid, kind):
            return {'status': 'disabled', 'authorization': 'none'}
        workspace = run['config']['workspace']
        scope = workspace or 'chat:' + run['chat_id']
        if kind == 'profile':
            human = self._human(run, input_id)
            if not content.strip() or content.strip() not in human:
                raise PolicyError('A profile proposal must quote current human input exactly.')
            words = normalized(content).rstrip(' .!')
            same = [row['id'] for row in self.store.query(
                "SELECT id,content FROM memories WHERE chat_id=? AND origin='direct-user' AND created>=? LIMIT 32",
                (run['chat_id'], run['created'])) if words in normalized(row['content'])]
            if same:
                # Phase 46 (1.9.28): the host saved these words from this message already; a second record would only
                # repeat them in every later session's SAVED PREFERENCES
                return {'status': 'already_saved', 'memory_id': same[0], 'authorization': 'none'}
            if (content.startswith('Across all projects,') or self._companion_wide(run, content, human)) \
                    and memory_facts.own_quote(human, content):
                scope = 'global'  # follow-up 1 (review finding 3, G1): only the person's own words widen the scope
        slot = key or kind + ':' + digest(normalized(content).encode())[:20]
        if kind == 'episodic':
            # Model-written lessons never take a host lesson slot.
            slot = 'model-lesson:' + digest(normalized(content).encode())[:24]
        elif kind == 'temporal' and key and not key.startswith('dated:'):
            # Per-source slots: a second source with the same subject contests
            # the first instead of waiting as a same-slot conflict.
            slot = key + '@' + self._source_tag(rid, evidence_ids)
        return self._queue(run, kind, scope, slot, content, evidence_ids, input_id, expires, supersedes)

    @staticmethod
    def _companion_wide(run, content, human=None):
        """Phase 46 (1.9.28, follow-up 2, D177, PEBBLE_1928_COMPANION_LATER, on unless "0"): a later-session
        wish said in a Companion chat (no project; memory.later_wish: "From now on, ...", "Please always ...") applies in
        every conversation, as the person's "Remember that ..." does there; shown with Forget (runner.learned_cards).
        Phase 46 (1.9.29, w22): also a MemoryPropose quote whose later-session marker is in the person's sentence that
        asks to keep it, not in the quote ("I want dates as DD.MM.YYYY" from "Moving forward, please keep this in mind:
        ..."; memory.later_request): it stayed in its conversation, so no later one had it."""
        return not run['config']['workspace'] and memory.companion_later() and (
            memory.later_wish(content) or human is not None and memory.later_request(content, human))

    def _source_tag(self, rid, evidence_ids):
        receipt = next((t for t in self.app.runs.tools(rid) if evidence_ids and t['call_id'] == evidence_ids[0]), None)
        result = receipt.get('result') if receipt else None
        source = result.get('url') if receipt and receipt['name'] == 'ReadURL' and isinstance(result, dict) else receipt and receipt['name']
        return digest(str(source).encode())[:12]

    def _queue(self, run, kind, scope, slot, content, evidence_ids, input_id=None, expires=None, supersedes=None):
        rid = run['id']
        fp = fingerprint(kind, scope, content)
        ident = digest(packed([rid, kind, scope, slot, fp, sorted(set(evidence_ids)), input_id, supersedes]).encode())
        now = time.time()
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Queue learning outside an application transaction.')
            with db:
                if not self.on(kind, db):
                    return {'status': 'disabled', 'authorization': 'none'}
                previous = db.execute('SELECT status,memory_id,reason FROM memory_candidates WHERE id=?', (ident,)).fetchone()
                if previous:
                    return {'candidate_id': ident, 'status': previous[0], 'memory_id': previous[1], 'reason': previous[2], 'authorization': 'none'}
                if db.execute('SELECT count(*) FROM memory_candidates WHERE run_id=?', (rid,)).fetchone()[0] >= MAX_PER_RUN or db.execute("SELECT count(*) FROM memory_candidates WHERE status='pending'").fetchone()[0] >= MAX_PENDING:
                    raise PolicyError('The bounded learning queue is full; the task can continue without learning.')
                watermark = db.execute("SELECT max(through) FROM memory_watermarks WHERE key IN ('all',?)", ('chat:' + run['chat_id'],)).fetchone()[0]
                forgotten = self._forgotten(db, scope, kind, slot, fp)
                status = 'forgotten' if forgotten or watermark is not None and run['created'] <= watermark else 'pending'
                if status == 'forgotten':
                    return {'status': status, 'authorization': 'none'}
                db.execute('INSERT INTO memory_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (ident, rid, run['chat_id'], kind, scope, slot, content.strip(), fp, packed(evidence_ids), input_id, expires, supersedes, status, None, None, now, now, None))
        return {'candidate_id': ident, 'status': status, 'authorization': 'none'}

    def _proof(self, candidate):
        run = self.app.runs.get(candidate['run_id'], candidate['chat_id'])
        kind, content = candidate['kind'], candidate['content']
        ids = json.loads(candidate['evidence'])
        receipts = {t['call_id']: t for t in self.app.runs.tools(run['id'])}
        selected = []
        for ident in ids:
            t = receipts.get(ident)
            result = t.get('result') if t else None
            # A lesson may rest on a refusal; every other layer needs a clean receipt.
            if not t or t['state'] != 'completed' or not isinstance(result, dict) or result.get('error') and kind != 'episodic':
                raise PolicyError('unsupported_receipt')
            selected.append(t)
        proof = {'version': 1, 'authorization': 'none', 'run_id': run['id'], 'receipt_ids': ids, 'observed_at': time.time()}
        if kind == 'profile':
            human = self._human(run, candidate['input_id'])
            # Quoted/pasted blocks are not the person's own statement, and
            # authority-shaped or protected-trait wishes are never automatic.
            if content not in memory_facts.visible_human_text(human) or not memory_facts.preference_allowed(content) \
                    or not memory_facts.own_quote(human, content):
                # follow-up 1 (the w5 security review): pasted or quoted words are someone else's, in every class
                raise PolicyError('durable_human_preference_required')
            if PREFERENCE.fullmatch(content):
                proof.update(evidence_class='human_preference', input_id=candidate['input_id'])
            elif memory.requested_quote(content, human) and not memory_facts._INJECTION.search(content):
                # Phase 46 (1.9.28, F2; golden held-out memory): the person's exact words from the sentence of their
                # message that asks to keep them ("Next time you give me a date, use DD.MM.YYYY.") waited for a native
                # confirmation no later session gives, or were refused. They are saved as asked; never another sentence
                # of the message, nor a task ("Delete the old build folder when you're done."): memory.requested_quote
                proof.update(evidence_class='requested_human_quote', input_id=candidate['input_id'])
            elif not memory_facts.nomination_eligible(content, human):
                raise PolicyError('durable_human_preference_required')
            elif candidate['reason'] == 'native_confirm':
                proof.update(evidence_class='confirmed_human_quote', input_id=candidate['input_id'], confirmation='native')
            else:
                raise AwaitingConfirmation(AWAITING)
            return content, proof
        if kind == 'semantic':
            if not self.store.setting('permissions')['tools']['files']:
                raise DeferredEvidence('file_permission_unavailable')
            if len(selected) != 1 or selected[0]['name'] != 'Read' or not content.strip():
                raise PolicyError('exact_source_quote_required')
            receipt = selected[0]
            excerpt = receipt['result'].get('untrusted_source', '')
            if not isinstance(excerpt, str) or content not in excerpt:
                raise PolicyError('quote_not_observed')
            rows = self.store.query("SELECT * FROM run_sources WHERE run_id=? AND receipt_id=? AND basis='disk'", (run['id'], receipt['call_id']))
            if len(rows) != 1:
                raise PolicyError('current_disk_witness_required')
            row = rows[0]
            ws = Workspace(run['config']['workspace'])
            if ws.identity != (row['root_dev'], row['root_ino']):
                raise PolicyError('source_changed')
            data = ws.snapshot(row['path'])
            if digest(data) != row['file_hash']:
                raise PolicyError('source_changed')
            # Whole lines or sentences only: a cut fragment ("use SQLite" from
            # "Do not use SQLite.") is a model abstraction, not an observation.
            # The host's legacy leading excerpt stays accepted and labelled.
            complete = receipt['result'].get('excerpt_complete') is not False
            start, truncated = memory_facts.locate(excerpt, content, complete), False
            if start is None:
                lead = memory_facts.leading_line(excerpt, complete)
                if not lead or lead['quote'] != content:
                    raise PolicyError('quote_not_whole_statement')
                start, truncated = lead['start'], lead['truncated']
            span = memory_facts.file_span(data.decode('utf-8'), receipt['arguments'], excerpt, start, len(content))
            if span is None:
                raise PolicyError('excerpt_alignment_failed')
            form, key = memory_facts.classify(excerpt, row['path'], content, start, complete)
            proof.update(evidence_class='attributed_file_quote', source_file=row['path'], source_hash=row['file_hash'],
                         root_identity=list(ws.identity), span=span, form=form)
            report = {'claim_class': 'source_report', 'path': row['path'], 'quote': content,
                      'file_hash': row['file_hash'], 'lines': span['lines'], 'form': form}
            if key:
                report['key'] = key
            if truncated:
                report['excerpt_truncated'] = True
            if memory_facts.instruction_shaped(content):
                report['instruction_shaped'] = True
            return packed(report), proof
        if kind == 'temporal':
            if not candidate['expires'] or candidate['expires'] <= time.time() or len(selected) != 1:
                raise PolicyError('fresh_temporal_receipt_required')
            receipt, result = selected[0], selected[0]['result']
            if receipt['name'] == 'ReadURL' and content and content in result.get('untrusted_source', ''):
                observed = result.get('retrieved_at')
                url = result.get('url')
                if type(observed) not in (int, float) or not math.isfinite(observed) or not 0 <= time.time() - observed <= 86400 or not isinstance(url, str):
                    raise PolicyError('dated_source_required')
                proof.update(evidence_class='attributed_web_quote', url=url, observed_at=observed)
            elif receipt['name'] == 'Weather' and result.get('status') == 'verified' and content == result.get('answer', '')[:1000]:
                proof.update(evidence_class='weather_service', observed_at=run['updated'])
            else:
                raise PolicyError('quote_not_observed')
            if '@' in candidate['slot'] and not candidate['slot'].startswith('dated:'):
                proof['subject'] = candidate['slot'].rsplit('@', 1)[0]
            report = {'claim_class': 'dated_source_report', 'quote': content, 'source': proof.get('url', 'weather-service'), 'observed_at': proof['observed_at']}
            if memory_facts.instruction_shaped(content):
                report['instruction_shaped'] = True  # Phase 16 (T1 patch 7): web text stays labelled data
            return packed(report), proof
        if kind == 'procedural':
            if not self.store.setting('permissions')['tools']['files']:
                raise DeferredEvidence('file_permission_unavailable')
            if run['state'] in UNSETTLED:
                raise DeferredEvidence('wait_for_settled_test_evidence')
            if run['state'] != 'completed' or len(selected) != 1 or selected[0]['name'] not in RECIPE_TOOLS:
                raise PolicyError('completed_test_recipe_required')
            receipt = selected[0]
            if receipt['call_id'] in self._child_calls(run['id']):
                raise PolicyError('child_receipt_not_counted')
            action = self._action(run, receipt)
            payload = action['payload'] if action else {}
            if not self._passed(receipt, action):
                raise PolicyError('successful_broker_test_required')
            recipe = self._recipe(receipt, payload)
            if recipe is None or candidate['slot'] != recipe_slot(recipe):
                raise PolicyError('recipe_identity_mismatch')
            failure = self.store.query('SELECT through FROM memory_recipe_failures WHERE scope=? AND recipe=?', (candidate['scope'], recipe))
            if failure and run['created'] <= failure[0]['through']:
                raise PolicyError('new_test_evidence_required_after_failure')
            ws = Workspace(run['config']['workspace'])
            if payload.get('root_identity') is not None and list(payload['root_identity']) != list(ws.identity):
                raise PolicyError('root_identity_changed')
            procedure = recipes.procedure(recipe)
            proof.update(evidence_class=recipe_evidence(procedure), recipe=recipe, procedure=procedure,
                         argv_digest=self._argv_digest(recipe, payload), root_identity=list(ws.identity),
                         draft_snapshot=bool(payload.get('drafts')),
                         **self._binding(run, receipt, recipe, payload, ws, candidate.get('validated_proof')))
            # Host constructs the typed guidance. Model-written steps are never learned.
            return recipe_guidance(recipe, proof['argv_digest']), proof
        if kind == 'episodic':
            if candidate['slot'].startswith('lesson:'):
                return self._host_lesson(run, candidate, proof)
            if not candidate['slot'].startswith('model-lesson:') or not selected or not content.strip():
                raise PolicyError('lesson_evidence_required')
            if memory_facts.credential_shaped(content) or memory_facts._INJECTION.search(content):
                raise PolicyError('unsafe_lesson_text')
            proof.update(evidence_class='model_lesson_unverified')
            return packed({'claim_class': 'model_lesson_unverified', 'lesson': content, 'receipt_ids': ids,
                'reuse': 'Model-written and unverified; not an instruction, permission or replay plan. Check the linked receipts.'}), proof
        raise PolicyError('unsupported_kind')

    # Recipe receipts -------------------------------------------------------

    def _child_calls(self, rid):
        """Root receipts that executed a child's effect request. They never count
        as root evidence for recipes or lessons."""
        with self.store.lock:
            db = self.store.db
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_effects'").fetchone():
                return set()
            return {row[0] for row in db.execute('SELECT effect_call_id FROM agent_effects WHERE run_id=? AND effect_call_id IS NOT NULL', (rid,))}

    def _prefetch_named(self, rid):
        """Phase 46: the files the run's request named among those the host prefetched (runner.prefetch_files)."""
        named = set()
        for row in self.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='prefetch'", (rid,)):
            try:
                named.update(p for p in json.loads(row['payload']).get('named') or () if isinstance(p, str))
            except (TypeError, ValueError, AttributeError):
                continue
        return named

    def _action(self, run, receipt):
        result = receipt.get('result') if isinstance(receipt.get('result'), dict) else {}
        # One keyed row, not the whole chat's action history (V1-079).
        rows = self.store.query('SELECT * FROM actions WHERE id=? AND chat_id=?', (result.get('action_id'), run['chat_id']))
        for row in rows:
            row['payload'] = json.loads(row['payload'])
        return rows[0] if rows else None

    @staticmethod
    def _recipe(receipt, payload):
        """The registry recipe the broker actually ran; 'auto' resolves to it."""
        recipe = payload.get('recipe') if isinstance(payload, dict) else None
        requested = (receipt.get('arguments') or {}).get('recipe')
        return recipe if recipe in REGISTRY and requested in (recipe, 'auto') else None

    @classmethod
    def _passed(cls, receipt, action):
        """An applied broker run of a registry recipe with exit 0, not cancelled
        and (when recorded) a passing classification."""
        if not action or action['kind'] != 'tests' or action['status'] != 'applied':
            return False
        payload = action['payload']
        outcome = payload.get('result') if isinstance(payload.get('result'), dict) else {}
        code = outcome.get('exit_code')
        # Phase 46 (1.9.28): a check run alone used a recipe that is not the project's own (testing.CHECK_ALONE)
        return (type(code) is int and code == 0 and not outcome.get('cancelled') and outcome.get('outcome', 'passed') == 'passed'
                and cls._recipe(receipt, payload) is not None and 'check_alone' not in payload)

    @staticmethod
    def _argv_digest(recipe, payload):
        """Digest of the pinned argv; legacy records without a plan share 'unpinned'."""
        plan = payload.get('plan')
        if plan is None:
            argv = 'unpinned'
        elif isinstance(plan, dict) and plan.get('recipe') == recipe and isinstance(plan.get('argv'), list) and plan['argv'] and all(isinstance(a, str) for a in plan['argv']):
            argv = plan['argv']
        else:
            raise PolicyError('recipe_plan_malformed')
        return digest(packed(['argv', recipe, argv]).encode())

    @staticmethod
    def _overlay(ws, payload):
        """The receipt's staged drafts over ws, as its test snapshot held them."""
        drafts = payload.get('drafts') if isinstance(payload.get('drafts'), list) else []
        ws.drafts = {Path(d['path']).as_posix(): d for d in drafts if isinstance(d, dict) and isinstance(d.get('path'), str)}
        return ws

    def _binding(self, run, receipt, recipe, payload, ws, validated=None):
        """Proof fields binding one receipt to the manifests and lockfiles it
        ran under. A binding the host recorded at proposal ('bound_files') must
        still hold. Without one (earlier actions) the project at settlement,
        its drafts overlaid, is bound, and an edit applied to a bound file
        after the receipt refuses it. A pending candidate keeps the binding it
        was first validated with."""
        current, _ = recipes.bindings(recipe, self._overlay(ws, payload))
        manifest = recipes.binding_digest(recipe, current)
        if 'bound_files' in payload:
            if payload['bound_files'] is None:
                raise PolicyError('bound_files_unavailable')
            if recipes.binding_digest(recipe, payload['bound_files']) != manifest:
                raise PolicyError('bound_files_changed')
            basis = 'reviewed'
        else:
            basis = 'settlement'
            if current and self._bound_edit_after(run, receipt, current):
                raise PolicyError('bound_files_changed')
        # A candidate validated before bindings existed has no digest: refused.
        if validated and json.loads(validated).get('manifest_digest') != manifest:
            raise PolicyError('bound_files_changed')
        return {'manifest_digest': manifest, 'bound_files': list(current), 'binding_basis': basis}

    def _bound_edit_after(self, run, receipt, names):
        """Whether this run applied an edit to a bound file after the receipt.
        A receipt beyond the bounded scan counts as edited (fail closed)."""
        after = False
        for row in self._run_receipts(run['id']):
            if row['call_id'] == receipt['call_id']:
                after = True
                continue
            result = row['result'] if isinstance(row['result'], dict) else {}
            if not after or row['name'] not in EDIT_TOOLS:
                continue
            action = self.store.query("SELECT payload FROM actions WHERE id=? AND chat_id=? AND kind='edit' AND status='applied'",
                                      (result.get('action_id'), run['chat_id']))
            path = json.loads(action[0]['payload']).get('path') if action else None
            if isinstance(path, str) and Path(path).as_posix() in names:
                return True
        return not after

    def _run_manifest(self, run, recipe, payload):
        """Manifest digest a receipt ran under, as far as it is known now; else None."""
        try:
            if 'bound_files' in payload:
                return recipes.binding_digest(recipe, payload['bound_files'])
            ws = self._overlay(Workspace(run['config']['workspace']), payload)
            return recipes.binding_digest(recipe, recipes.bindings(recipe, ws)[0])
        except (OSError, TypeError, ValueError):
            return None

    def _peer_success(self, db, rid, proof):
        """In-transaction re-check of a peer run's own successful root receipt."""
        run = db.execute('SELECT state,chat_id,config FROM runs WHERE id=?', (rid,)).fetchone()
        if not run or run[0] != 'completed':
            return False
        child = self._child_calls(rid)
        for call_id, name, raw, arguments in db.execute("SELECT call_id,name,result,arguments FROM run_tools WHERE run_id=? AND state='completed' AND name IN (?,?)", (rid, *RECIPE_TOOLS)).fetchall():
            result = json.loads(raw or '{}')
            if call_id in child or result.get('error'):
                continue
            row = db.execute('SELECT kind,status,payload FROM actions WHERE id=? AND chat_id=?', (result.get('action_id'), run[1])).fetchone()
            if not row:
                continue
            action = {'kind': row[0], 'status': row[1], 'payload': json.loads(row[2])}
            receipt = {'arguments': json.loads(arguments), 'result': result}
            try:
                same = (self._passed(receipt, action) and self._recipe(receipt, action['payload']) == proof['recipe']
                        and self._argv_digest(proof['recipe'], action['payload']) == proof['argv_digest']
                        and (action['payload'].get('root_identity') is None or list(action['payload']['root_identity']) == proof['root_identity'])
                        # A binding recorded at proposal must be this one; otherwise the peer's validated proof was compared.
                        and ('bound_files' not in action['payload']
                             or recipes.binding_digest(proof['recipe'], action['payload']['bound_files']) == proof['manifest_digest']))
            except PolicyError:
                same = False
            if same:
                return True
        return False

    def _revalidate_after(self, scope, recipe, through):
        """Runs created at or before `through` no longer count for this recipe."""
        self.store.query('INSERT INTO memory_recipe_failures VALUES(?,?,?) ON CONFLICT(scope,recipe) '
                         'DO UPDATE SET through=max(through,excluded.through)', (scope, recipe, through))

    def _observe_recipe(self, run, receipt):
        """A consented root recipe receipt: a failure needs revalidation, and a
        changed argv digest or manifest binding makes the learned guidance
        stale. Either way, earlier successes stop counting toward new guidance."""
        action = self._action(run, receipt)
        payload = action['payload'] if action else {}
        recipe = self._recipe(receipt, payload)
        if not action or action['kind'] != 'tests' or action['status'] != 'applied' or recipe is None:
            return
        scope, now = run['config']['workspace'], time.time()
        # Guidance learned before Phase 20 kept every recipe in a test: slot.
        slots = (recipe_slot(recipe), 'test:' + recipe)
        code = (payload.get('result') if isinstance(payload.get('result'), dict) else {}).get('exit_code')
        if type(code) is int and code != 0:
            self._revalidate_after(scope, recipe, run['updated'])
            self.store.query("UPDATE memory_evidence SET status='needs_revalidation',updated=? WHERE scope=? AND kind='procedural' AND slot IN (?,?) AND status='active'", (now, scope, *slots))
        try:
            argv = self._argv_digest(recipe, payload)
        except PolicyError:
            return
        # An unknown binding leaves the manifest check to the recall audit.
        manifest = self._run_manifest(run, recipe, payload)
        changed = [row['memory_id'] for row in self.store.query("SELECT memory_id,proof FROM memory_evidence WHERE scope=? AND kind='procedural' AND slot IN (?,?) AND status='active'", (scope, *slots))
                   if json.loads(row['proof']).get('argv_digest') != argv
                   or manifest is not None and json.loads(row['proof']).get('manifest_digest') != manifest]
        for mid in changed:
            self.store.query("UPDATE memory_evidence SET status='stale',updated=? WHERE memory_id=?", (now, mid))
        earlier = self.store.query('SELECT max(created) AS through FROM runs WHERE created<?', (run['created'],))[0]['through']
        if changed and earlier is not None:
            # This run may start the new pair; older pending evidence may not.
            self._revalidate_after(scope, recipe, earlier)

    # Host lessons ----------------------------------------------------------

    def _run_receipts(self, rid):
        """Settled root receipts of one run in commit order; child effects excluded."""
        child, found = self._child_calls(rid), []
        for row in self.store.query('SELECT rowid AS seq,call_id,name,arguments,state,result FROM run_tools WHERE run_id=? ORDER BY rowid LIMIT ?', (rid, MAX_RUN_RECEIPTS)):
            if row['call_id'] in child or row['state'] != 'completed':
                continue
            try:
                row['arguments'] = json.loads(row['arguments'] or '{}')
                row['result'] = json.loads(row['result']) if row['result'] else None
            except ValueError:
                continue
            found.append(row)
        return found

    def _lesson_pass(self, receipts, actions):
        """The first passing recipe receipt after an applied edit in one run."""
        edits = []
        for receipt in receipts:
            result = receipt['result'] if isinstance(receipt['result'], dict) else {}
            action = actions.get(result.get('action_id'))
            if receipt['name'] in EDIT_TOOLS and action and action['kind'] == 'edit' and action['status'] == 'applied':
                edits.append((receipt['call_id'], action['payload'].get('path')))
            elif receipt['name'] in RECIPE_TOOLS and edits and self._passed(receipt, action):
                paths = [memory_facts._clean(p, 80) for _, p in edits if isinstance(p, str)]
                return {'seq': receipt['seq'], 'call_id': receipt['call_id'], 'edit_call': edits[-1][0],
                        'recipe': self._recipe(receipt, action['payload']), 'paths': list(dict.fromkeys(p for p in paths if p))[:3]}
        return None

    def _lesson_runs(self, run):
        """Consented, settled runs of the same scope up to this one (bounded)."""
        workspace = run['config'].get('workspace')
        if workspace:
            rows = self.store.query("SELECT id,chat_id,state FROM runs WHERE json_extract(config,'$.workspace')=? AND created<=? ORDER BY created DESC LIMIT ?", (workspace, run['created'], MAX_LESSON_RUNS))
        else:
            rows = self.store.query("SELECT id,chat_id,state FROM runs WHERE chat_id=? AND json_extract(config,'$.workspace') IS NULL AND created<=? ORDER BY created DESC LIMIT ?", (run['chat_id'], run['created'], MAX_LESSON_RUNS))
        return [r for r in rows if (r['id'] == run['id'] or r['state'] not in memory_facts.SKIP_STATES) and self.allowed_run(r['id'], 'episodic')]

    def _lessons(self, run):
        """Failure signatures (a failing test ID or refusal kind) seen in two or
        more consented root runs of this scope, followed in this run by a
        passing recipe after an applied edit."""
        receipts = self._run_receipts(run['id'])
        owned = {run['chat_id']: {a['id']: a for a in self.store.actions(run['chat_id'])}}
        passing = self._lesson_pass(receipts, owned[run['chat_id']])
        if not passing:
            return []
        seen = {}
        for other in self._lesson_runs(run):
            actions = owned.setdefault(other['chat_id'], {a['id']: a for a in self.store.actions(other['chat_id'])})
            for receipt in receipts if other['id'] == run['id'] else self._run_receipts(other['id']):
                if other['id'] == run['id'] and receipt['seq'] >= passing['seq']:
                    break
                result = receipt['result'] if isinstance(receipt['result'], dict) else {}
                for signature in memory_facts.failure_signatures(receipt, actions.get(result.get('action_id'))):
                    seen.setdefault(signature, {}).setdefault(other['id'], receipt['call_id'])
        lessons = []
        for (kind, signature), runs in sorted(seen.items(), key=lambda item: (-len(item[1]), item[0])):
            if len(runs) >= 2 and not memory_facts.credential_shaped(signature):
                lessons.append({'type': kind, 'signature': signature, 'runs': sorted(runs), 'pass': passing,
                                'receipts': [f'{rid}/{call}' for rid, call in sorted(runs.items())]})
            if len(lessons) >= MAX_LESSONS_PER_RUN:
                break
        return lessons

    def _collect_lessons(self, run):
        if not self.on('episodic') or not self.consent(run['id'], 'episodic'):
            return
        scope = run['config']['workspace'] or 'chat:' + run['chat_id']
        for lesson in self._lessons(run):
            content = lesson_signature(lesson['type'], lesson['signature'])
            try:
                self._queue(run, 'episodic', scope, 'lesson:' + digest(content.encode())[:24], content,
                            [lesson['pass']['call_id'], lesson['pass']['edit_call']])
            except PolicyError:
                continue

    def _host_lesson(self, run, candidate, proof):
        if run['state'] in UNSETTLED or run['state'] in memory_facts.SKIP_STATES:
            raise DeferredEvidence('wait_for_settled_lesson_evidence')
        lesson = next((l for l in self._lessons(run) if lesson_signature(l['type'], l['signature']) == candidate['content']), None)
        if not lesson or [lesson['pass']['call_id'], lesson['pass']['edit_call']] != json.loads(candidate['evidence']):
            raise PolicyError('lesson_evidence_not_current')
        passing = lesson['pass']
        noun = ('Failing test ' + lesson['signature'] if lesson['type'] == 'test_failure'
                else 'The refusal "' + lesson['signature'] + '"')
        observation = (f'{noun} was observed in {len(lesson["runs"])} consented runs here; a later run passed '
                       f'{passing["recipe"]} after an applied edit' + (f' to {", ".join(passing["paths"])}' if passing['paths'] else '') + '.')
        linked = lesson['receipts'] + [f'{run["id"]}/{passing["edit_call"]}', f'{run["id"]}/{passing["call_id"]}']
        proof.update(evidence_class='host_lesson', receipt_ids=linked, runs=lesson['runs'])
        return packed({'claim_class': 'host_lesson', 'type': lesson['type'], 'signature': lesson['signature'],
            'observation': observation, 'failing_runs': len(lesson['runs']), 'receipt_ids': linked,
            'reuse': 'Host-recorded pattern from linked receipts; not an instruction, permission or replay plan. Check current evidence.'}), proof

    # Promotion -------------------------------------------------------------

    def maintain(self, limit=MAX_BATCH, candidate_id=None, human_resolution=False):
        self.outside_transaction()
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise PolicyError('Maintenance handles one to sixteen candidates per pass.')
        if not self.on():
            return {'processed': 0, 'status': 'disabled'}
        # Called under the host task lock, including settlement and tool admission.
        # Nominations waiting for a native answer are not retried by the queue.
        rows = self.store.query("SELECT * FROM memory_candidates WHERE status='pending'" + (' AND id=?' if candidate_id else " AND (reason IS NULL OR reason<>'" + AWAITING + "')") + ' ORDER BY updated,created LIMIT ?', (candidate_id, limit) if candidate_id else (limit,))
        outcomes = []
        deadline = time.monotonic() + 1.0
        for candidate in rows:
            if time.monotonic() >= deadline:
                break
            status, reason, content, proof, contested = 'promoted', None, None, None, False
            if not self.on(candidate['kind']):
                continue
            if not self.consent(candidate['run_id'], candidate['kind']):
                self.store.query("UPDATE memory_candidates SET status='disabled',reason='run_consent_disabled',updated=? WHERE id=?", (time.time(), candidate['id']))
                continue
            try:
                content, proof = self._proof(candidate)
                if memory_facts.credential_shaped(content) or len(content) > 4000:
                    raise PolicyError('sensitive_or_oversized_evidence')
            except AwaitingConfirmation:
                status, reason = 'pending', AWAITING
            except DeferredEvidence:
                status, reason = 'pending', 'wait_for_settled_evidence'
            except (PolicyError, OSError, ValueError, TypeError, KeyError):
                status, reason = 'unsupported', 'evidence_not_supported_or_current'
            now = time.time()
            with self.store.lock:
                db = self.store.db
                if db.in_transaction:
                    raise PolicyError('Promote learning outside an application transaction.')
                with db:
                    if not self.on(candidate['kind'], db):
                        continue
                    current = db.execute('SELECT status FROM memory_candidates WHERE id=?', (candidate['id'],)).fetchone()
                    if not current or current[0] != 'pending':
                        continue
                    watermark = db.execute("SELECT max(through) FROM memory_watermarks WHERE key IN ('all',?)", ('chat:' + candidate['chat_id'],)).fetchone()[0]
                    forgotten = self._forgotten(db, candidate['scope'], candidate['kind'], candidate['slot'], candidate['fingerprint'])
                    run_created = db.execute('SELECT created FROM runs WHERE id=?', (candidate['run_id'],)).fetchone()
                    if forgotten or not run_created or watermark is not None and run_created[0] <= watermark:
                        status, reason = 'forgotten', 'forgetting_tombstone'
                    if candidate['expires'] is not None and candidate['expires'] <= now:
                        status, reason = 'expired', 'validity_ended'
                    # A contested record is still the slot's current version.
                    old = db.execute("SELECT e.memory_id,e.fingerprint,e.run_id,e.proof FROM memory_evidence e JOIN memories m ON m.id=e.memory_id WHERE e.scope=? AND e.kind=? AND e.slot=? AND e.status IN (" + memory_graph.LIVE_MARKS + ") ORDER BY e.updated DESC", (candidate['scope'], candidate['kind'], candidate['slot'], *memory_graph.LIVE)).fetchone()
                    if status == 'promoted' and not old:
                        # Equivalent quotes from the same source/revision dedupe
                        # even when the model and automatic collector chose
                        # different keys. Different source identities stay apart.
                        duplicates = db.execute("SELECT memory_id,fingerprint,run_id,proof FROM memory_evidence WHERE scope=? AND kind=? AND fingerprint=? AND status IN (" + memory_graph.LIVE_MARKS + ")", (candidate['scope'], candidate['kind'], candidate['fingerprint'], *memory_graph.LIVE)).fetchall()
                        for duplicate in duplicates:
                            prior = json.loads(duplicate[3])
                            if all(prior.get(k) == proof.get(k) for k in ('evidence_class','source_file','source_hash','root_identity','recipe','url')):
                                old = duplicate
                                break
                    mid = None
                    if status == 'promoted' and candidate['supersedes'] and (not old or old[0] != candidate['supersedes']):
                        status, reason = 'unsupported', 'supersession_scope_mismatch'
                    if status == 'promoted' and old and old[1] != candidate['fingerprint']:
                        correction = candidate['kind'] == 'profile' and candidate['supersedes'] == old[0] and memory_facts.CORRECTION.search(candidate['content'])
                        changed_file = candidate['kind'] == 'semantic' and proof.get('source_file') == json.loads(old[3]).get('source_file') and proof.get('source_hash') != json.loads(old[3]).get('source_hash')
                        dated_update = candidate['kind'] == 'temporal' and proof.get('url') == json.loads(old[3]).get('url') and proof['observed_at'] >= json.loads(old[3]).get('observed_at', 0)
                        if not (human_resolution or candidate['reason'] == 'native_accept' or correction or changed_file or dated_update):
                            status, reason = 'conflict', 'active_value_differs'
                    if status == 'promoted':
                        # Typed recipe guidance requires two distinct successful
                        # root runs with the same recipe, argv digest, root and
                        # manifest binding.
                        if candidate['kind'] == 'procedural':
                            peers = db.execute("SELECT run_id,validated_proof FROM memory_candidates WHERE kind='procedural' AND scope=? AND slot=? AND status IN ('pending','promoted') AND validated_proof IS NOT NULL AND run_id<>? LIMIT ?", (candidate['scope'], candidate['slot'], candidate['run_id'], MAX_PER_RUN)).fetchall()
                            verified = False
                            for rid, peer_proof in peers:
                                prior = json.loads(peer_proof)
                                if any(prior.get(k) != proof.get(k) for k in ('root_identity', 'recipe', 'argv_digest', 'manifest_digest')):
                                    continue
                                failure = db.execute('SELECT through FROM memory_recipe_failures WHERE scope=? AND recipe=?', (candidate['scope'], proof['recipe'])).fetchone()
                                peer_created = db.execute('SELECT created FROM runs WHERE id=?', (rid,)).fetchone()
                                if not peer_created or failure and peer_created[0] <= failure[0]:
                                    continue
                                peer = next((p for p in self.store_rows(db, rid) if p['kind'] == 'procedural' and p['slot'] == candidate['slot']), None)
                                # Proof validation is kept outside this transaction.
                                # Only the peer run's own receipts, re-read here, count.
                                verified |= bool(peer) and self._peer_success(db, rid, proof)
                            if not verified:
                                status, reason = 'pending', 'needs_two_successful_runs'
                        if status == 'promoted':
                            # Guidance under another argv or manifest binding is a new version, not the same record.
                            same_revision = old and all(proof.get(k) == json.loads(old[3]).get(k) for k in ('source_hash','root_identity','observed_at','argv_digest','manifest_digest') if k != 'observed_at' or candidate['kind'] == 'temporal')
                            mid = old[0] if old and old[1] == candidate['fingerprint'] and same_revision else 'learned-' + candidate['id'][:40]
                            if old and old[0] != mid:
                                db.execute("UPDATE memory_evidence SET status='superseded',superseded_by=?,updated=? WHERE memory_id=?", (mid, now, old[0]))
                                memory_graph.add_edge(db, mid, old[0], 'supersedes', {'basis': reason or 'newer_evidence'}, now)
                            if not old or old[0] != mid:
                                db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)', (mid, candidate['kind'], content, candidate['scope'], candidate['chat_id'], 'auto-learned', now, now, candidate['expires']))
                                db.execute('INSERT INTO memory_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (mid, candidate['id'], candidate['run_id'], candidate['chat_id'], candidate['scope'], candidate['kind'], candidate['slot'], candidate['fingerprint'], 'active', packed(proof), None, now))
                            if proof.get('evidence_class') == 'host_lesson':
                                for source in proof['runs']:
                                    if db.execute('SELECT 1 FROM memories WHERE id=?', ('episode-' + source,)).fetchone():
                                        memory_graph.add_edge(db, mid, 'episode-' + source, 'derived_from', {'basis': 'host_lesson'}, now)
                            # Claims and edges commit with the promotion itself.
                            related = memory_graph.relate(db, mid, now)
                            contested = bool(related and related['status'] == 'contested')
                            if candidate['kind'] == 'profile' and proof.get('evidence_class') in memory.VERIFIED_QUOTES \
                                    and not contested:
                                # Phase 46 (1.9.28, follow-up 2, D183): the person's own words with a newer value of a
                                # slot replace the older record holding only that slot, as their "Remember that ..." does
                                memory_graph.supersede_slot(db, mid, now, fingerprint, learned=True)
                    db.execute('UPDATE memory_candidates SET status=?,reason=?,memory_id=?,updated=?,validated_proof=? WHERE id=?', (status, reason, mid, now, packed(proof) if proof else None, candidate['id']))
                    self._bound(db)
            # A contested record is stored but withheld: not completed evidence.
            outcomes.append({'candidate_id': candidate['id'], 'status': 'conflict' if contested else status, 'memory_id': mid,
                             **({'reason': 'contested_record'} if contested else {'reason': reason} if reason == AWAITING else {})})
        return {'processed': len(outcomes), 'outcomes': outcomes, 'authorization': 'none'}

    @staticmethod
    def store_rows(db, rid):
        cur = db.execute('SELECT * FROM memory_candidates WHERE run_id=?', (rid,))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    @staticmethod
    def _bound(db):
        evicted = [mid for (mid,) in db.execute("SELECT id FROM memories WHERE origin='auto-learned' ORDER BY updated DESC LIMIT -1 OFFSET ?", (MAX_LEARNED,)).fetchall()]
        for mid in evicted:
            db.execute('DELETE FROM memories WHERE id=?', (mid,))
            db.execute('DELETE FROM memory_evidence WHERE memory_id=?', (mid,))
        # Retention is a host removal: it never clears a contest.
        memory_graph.detach(db, evicted, time.time(), native=False)
        db.execute("DELETE FROM memory_candidates WHERE id IN (SELECT id FROM memory_candidates WHERE status<>'pending' ORDER BY updated DESC LIMIT -1 OFFSET ?)", (MAX_RETAINED_CANDIDATES,))
        db.execute('DELETE FROM memory_evidence WHERE memory_id NOT IN (SELECT id FROM memories)')

    def collect(self, rid):
        self.outside_transaction()
        if not self.on():
            return {'processed': 0, 'status': 'disabled'}
        run = self.app.runs.get(rid)
        self.begin_run(rid)
        self._collect_preferences(run)
        receipts = self.app.runs.tools(rid)[-MAX_SCANNED_RECEIPTS:]
        outcomes = [r for r in receipts if r['name'] in (*RECIPE_TOOLS, 'ReadURL', 'Weather') and r['state'] == 'completed'
                    and isinstance(r.get('result'), dict) and not r['result'].get('error')][-MAX_OUTCOME_RECEIPTS:]
        child = self._child_calls(rid)
        for receipt in outcomes:
            result = receipt['result']
            try:
                if receipt['name'] in RECIPE_TOOLS:
                    if not self.on('procedural') or not self.consent(rid, 'procedural') or receipt['call_id'] in child:
                        continue
                    self._observe_recipe(run, receipt)
                    action = self._action(run, receipt)
                    recipe = self._recipe(receipt, action['payload'] if action else {})
                    key = recipe_slot(recipe) if recipe else 'test:' + receipt['arguments'].get('recipe', '')
                    self.propose(rid, 'procedural', '', key=key, evidence_ids=[receipt['call_id']])
                elif receipt['name'] in ('ReadURL', 'Weather'):
                    if receipt['name'] == 'Weather':
                        quote = result.get('answer', '')[:1000]
                    elif isinstance(result.get('source_id'), str):
                        # T7 F14: a page read through the source ledger is remembered only by a host-verified
                        # claim span of that source, never by its leading line (often a hostile title).
                        quote = self._claim_quote(rid, result)
                        if not quote:
                            continue
                    else:
                        quote = next((line for line in result.get('untrusted_source', '').splitlines() if line.strip()), '')[:240]
                    self.propose(rid, 'temporal', quote, key='dated:' + digest(str(result.get('url', 'weather')).encode())[:24], evidence_ids=[receipt['call_id']], expires=time.time() + (300 if receipt['name'] == 'Weather' else 86400))
            except PolicyError:
                pass
        self._collect_facts(run, receipts)
        self._collect_lessons(run)
        outcome = self.maintain()
        self.consolidate()
        return outcome

    def _claim_quote(self, rid, result):
        """The first supported research claim span of this source that lies in the shown window, else None."""
        ledger, source = getattr(self.app, 'ledger', None), result.get('source_id')
        if ledger is None or not isinstance(source, str):
            return None
        shown = result.get('untrusted_source', '')
        try:
            claims = ledger.claims(rid)
            text = ledger.text(rid, source)
        except (ValueError, KeyError, PolicyError):
            return None
        for claim in claims:
            span = claim.get('span')
            if claim.get('support') == 'supported' and claim.get('source_id') == source and span:
                quote = text[span[0]:span[1]].strip()[:240]
                return quote if quote and quote in shown else None
        return None

    def _collect_preferences(self, run):
        """Explicit preference sentences from the goal and human inbox only.

        Tool, source and model text never reach this path. A quote with an
        explicit correction word ("now", "instead", ...) names the active
        record in its topic slot so the existing correction rule can apply."""
        rid = run['id']
        inputs = [(None, run['goal'])]
        for row in self.store.query("SELECT id FROM run_inbox WHERE run_id=? AND origin='human' ORDER BY sequence LIMIT 20", (rid,)):
            inputs.append((row['id'], self._human(run, row['id'])))
        default_scope = run['config']['workspace'] or 'chat:' + run['chat_id']
        for input_id, human in inputs:
            # Phase 46 (1.9.29, w22): never a wish for now ("I prefer YYYY-MM-DD for this answer.") nor a message the
            # person said not to keep (memory.learnable_quotes)
            for text in memory.learnable_quotes(human):
                key, supersedes = memory_facts.preference_slot(text), None
                if key and memory_facts.CORRECTION.search(text):
                    scope = 'global' if text.startswith('Across all projects,') or self._companion_wide(run, text) \
                        else default_scope  # follow-up 2 (D177): the scope propose gives it
                    active = self.store.query("SELECT e.memory_id FROM memory_evidence e JOIN memories m ON m.id=e.memory_id WHERE e.scope=? AND e.kind='profile' AND e.slot=? AND e.status='active' LIMIT 1", (scope, key))
                    supersedes = active[0]['memory_id'] if active else None
                try:
                    self.propose(rid, 'profile', text, key=key, input_id=input_id, supersedes=supersedes)
                except PolicyError:
                    pass

    def _collect_facts(self, run, receipts):
        """Exact fact candidates from owned disk-basis Read excerpts.

        Bounded per file and per run; an older view of a file that was read
        again at a newer revision is skipped. Slots are per fact (config key or
        statement digest) under the file slot, so facts from one file do not
        conflict and a changed value supersedes its earlier version."""
        rid = run['id']
        if not self.on('semantic') or not self.consent(rid, 'semantic'):
            return
        remaining, files = memory_facts.MAX_FACTS_PER_RUN, {}
        # Phase 46: the host's prefetch reads (call ids host:...) teach only for the files the request named, as the
        # model's own Read of them would; the others (a tiny project read whole, files earlier turns changed) were read
        # for speed, not chosen, and learning from them would add facts and contests nobody asked about.
        named = self._prefetch_named(rid)
        reads = [r for r in receipts if r['name'] == 'Read' and r['state'] == 'completed'
                 and (not str(r.get('call_id', '')).startswith('host:')
                      or isinstance(r.get('arguments'), dict) and r['arguments'].get('file_path') in named)][-MAX_READ_RECEIPTS:]
        for receipt in reversed(reads):
            result, arguments = receipt.get('result'), receipt.get('arguments')
            if not isinstance(result, dict) or result.get('error') or not isinstance(arguments, dict):
                continue
            excerpt, path, witness = result.get('untrusted_source'), arguments.get('file_path'), result.get('source')
            if (not isinstance(excerpt, str) or not isinstance(path, str) or not path
                    or not isinstance(witness, dict) or witness.get('basis') != 'disk'):
                continue
            state = files.setdefault(path, {'hash': result.get('file_hash'), 'count': 0, 'quotes': set(), 'fallback': False})
            if state['hash'] != result.get('file_hash') or state['count'] >= memory_facts.MAX_FACTS_PER_FILE:
                continue
            complete = result.get('excerpt_complete') is not False
            facts = memory_facts.extract(excerpt, path, complete, memory_facts.MAX_FACTS_PER_FILE - state['count'])
            if not facts and not state['count'] and not state['fallback']:
                lead = memory_facts.leading_line(excerpt, complete)
                if lead and memory_facts.instruction_shaped(lead['quote']):
                    lead = None
                facts, state['fallback'] = ([lead] if lead else []), True
            parent = 'file:' + digest(path.encode())[:24]
            for fact in facts:
                if remaining <= 0:
                    return
                normal = normalized(fact['quote'])
                if normal in state['quotes']:
                    continue
                state['quotes'].add(normal)
                if fact['form'] == 'leading_line':
                    slot = parent + ':lead'
                elif fact['key']:
                    slot = parent + ':k:' + digest(fact['key'].casefold().encode())[:16]
                else:
                    slot = parent + ':s:' + digest(normal.encode())[:16]
                try:
                    self.propose(rid, 'semantic', fact['quote'], key=slot, evidence_ids=[receipt['call_id']])
                except PolicyError:
                    continue
                state['count'] += 1
                remaining -= 1

    # Consolidation ---------------------------------------------------------

    def consolidate(self, limit=MAX_CONSOLIDATION):
        """Second bounded maintenance stage. Each step is one transaction and
        idempotent, so a crash at any point loses or repeats nothing; the next
        pass continues. With memory off only forgetting hygiene runs."""
        self.outside_transaction()
        if type(limit) is not int or not 1 <= limit <= MAX_CONSOLIDATION:
            raise PolicyError('Consolidation handles one to sixty-four records per step.')
        steps = [('relations', self._step_relations), ('tombstones', self._step_tombstones)]
        if self.on():
            steps += [('graph', self._step_graph), ('lessons', self._step_lessons)]
        report, deadline = {}, time.monotonic() + 1.0
        for name, step in steps:
            if time.monotonic() >= deadline:
                report['deferred'] = name
                break
            with self.store.lock:
                db = self.store.db
                if db.in_transaction:
                    raise PolicyError('Consolidate learning outside an application transaction.')
                with db:
                    report[name] = step(db, limit, time.time())
        return {'stage': 'consolidation', 'authorization': 'none', **report}

    @staticmethod
    def _step_relations(db, limit, now):
        return {'supersedes': memory_graph.migrate_supersedes(db, now), 'removed': memory_graph.collect_garbage(db, now, limit)}

    @staticmethod
    def _step_graph(db, limit, now):
        return {'related': memory_graph.backfill(db, now, limit)}

    def _step_tombstones(self, db, limit, now):
        """Tombstones never expire by time. Legacy literal project scopes are
        re-keyed; a deleted conversation's tombstones go only once its runs and
        candidates are gone; an overflowing scope folds into its Bloom filter."""
        salt, rekeyed, released, folded = self.salts(db)[0], 0, 0, 0
        for scope, kind, slot, fp, created in db.execute("SELECT scope,kind,slot,fingerprint,created FROM memory_tombstones WHERE scope<>'global' AND scope NOT LIKE 'chat:%' AND scope NOT LIKE 'p:%' LIMIT ?", (limit * 4,)).fetchall():
            db.execute('INSERT OR IGNORE INTO memory_tombstones VALUES(?,?,?,?,?)', (memory_graph.scope_key(scope, salt), kind, slot, fp, created))
            db.execute('DELETE FROM memory_tombstones WHERE scope=? AND kind=? AND slot=? AND fingerprint=?', (scope, kind, slot, fp))
            rekeyed += 1
        chats = db.execute("SELECT scope FROM memory_tombstones WHERE scope LIKE 'chat:%' UNION SELECT scope FROM memory_tombstone_blooms WHERE scope LIKE 'chat:%' LIMIT ?", (limit,)).fetchall()
        for (scope,) in chats:
            cid = scope[5:]
            if any(db.execute(sql, (cid,)).fetchone() for sql in ('SELECT 1 FROM chats WHERE id=?', 'SELECT 1 FROM runs WHERE chat_id=?', 'SELECT 1 FROM memory_candidates WHERE chat_id=?')):
                continue
            released += db.execute('DELETE FROM memory_tombstones WHERE scope=?', (scope,)).rowcount
            db.execute('DELETE FROM memory_tombstone_blooms WHERE scope=?', (scope,))
        for (scope,) in db.execute('SELECT scope FROM memory_tombstones GROUP BY scope HAVING count(*)>? LIMIT 4', (MAX_TOMBSTONES_PER_SCOPE,)).fetchall():
            folded += memory_graph.fold(db, scope, FOLD_KEEP, now)
        return {'rekeyed': rekeyed, 'released': released, 'folded': folded}

    @staticmethod
    def _step_lessons(db, limit, now):
        """A host lesson whose linked runs are gone loses its evidence and goes."""
        gone = []
        for mid, raw in db.execute("SELECT memory_id,proof FROM memory_evidence WHERE kind='episodic' AND run_id<>'imported' AND status IN (" + memory_graph.LIVE_MARKS + ") ORDER BY updated LIMIT ?", (*memory_graph.LIVE, limit)).fetchall():
            proof = json.loads(raw)
            if proof.get('evidence_class') == 'host_lesson' and any(
                    not db.execute('SELECT 1 FROM runs WHERE id=?', (rid,)).fetchone() for rid in proof.get('runs', [])):
                gone.append(mid)
        for mid in gone:
            db.execute('DELETE FROM memories WHERE id=?', (mid,))
            db.execute('DELETE FROM memory_evidence WHERE memory_id=?', (mid,))
        memory_graph.detach(db, gone, now, native=False)
        return {'removed': len(gone)}

    # Inspection, recall helpers and native resolution ------------------------

    def audit(self, scope, files_allowed=True, memory_ids=()):
        self.outside_transaction()
        rows = self.store.query("SELECT * FROM memory_evidence WHERE scope=? AND status='active' ORDER BY updated DESC LIMIT ?", (scope, MAX_LEARNED))
        rows = [r for r in rows if r['memory_id'] in set(memory_ids)][:32]
        ws, snapshots, bound = None, {}, {}
        verified, read_bytes = set(), 0
        for row in rows:
            proof = json.loads(row['proof'])
            status = 'active'
            if proof.get('source_file') or proof.get('root_identity'):
                if not files_allowed:
                    continue  # Recall separately excludes file-derived records.
                try:
                    ws = ws or Workspace(scope)
                    if list(ws.identity) != proof['root_identity']:
                        status = 'stale'
                    elif proof.get('source_file'):
                        path = proof['source_file']
                        if path not in snapshots:
                            if read_bytes + MAX_FILE > MAX_AUDIT_BYTES:
                                continue
                            data = ws.snapshot(path)
                            read_bytes += len(data)
                            snapshots[path] = digest(data)
                        if snapshots[path] != proof['source_hash']:
                            status = 'stale'
                    elif row['kind'] == 'procedural':
                        # A known recipe holds only while its manifests and lockfiles are unchanged.
                        if not proof.get('manifest_digest'):
                            status = 'needs_revalidation'  # learned before bindings existed
                        else:
                            files = recipes.bound_files(proof['recipe'])
                            if files not in bound:
                                bound[files], used = recipes.bindings(proof['recipe'], ws, MAX_AUDIT_BYTES - read_bytes)
                                read_bytes += used
                            if recipes.binding_digest(proof['recipe'], bound[files]) != proof['manifest_digest']:
                                status = 'stale'
                except recipes.BindingBudget:
                    continue  # Unchecked this time: withheld, not marked.
                except (OSError, PolicyError, ValueError, KeyError, TypeError):
                    status = 'stale'
            if status != 'active':
                self.store.query('UPDATE memory_evidence SET status=?,updated=? WHERE memory_id=?', (status, time.time(), row['memory_id']))
            else:
                verified.add(row['memory_id'])
        return verified

    def allowed_run(self, rid, kind=None):
        self.outside_transaction()
        run = self.app.runs.get(rid)
        if not self.consent(rid, kind or 'episodic'):
            return False
        keys = ['all', 'chat:' + run['chat_id']]
        if kind == 'working':
            keys.append('working:' + run['chat_id'])
        with self.store.lock:
            rows = self.store.db.execute('SELECT through FROM memory_watermarks WHERE key IN (' + ','.join('?' for _ in keys) + ')', keys).fetchall()
        return all(run['created'] > row[0] for row in rows)

    def candidates(self, cid=None, limit=100):
        self.outside_transaction()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise PolicyError('Inspect at most 100 candidates.')
        if cid is not None:
            self.app.chat(cid)
        return self.store.query('SELECT * FROM memory_candidates' + (' WHERE chat_id=?' if cid else '') + ' ORDER BY updated DESC LIMIT ?', (cid, limit) if cid else (limit,))

    def relations(self, memory_id=None, limit=100):
        """Native inspection of graph edges; evidence is content-free."""
        self.outside_transaction()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise PolicyError('Inspect at most 100 relations.')
        with self.store.lock:
            return memory_graph.relations(self.store.db, memory_id, limit)

    def withheld(self, ids, scopes=None):
        with self.store.lock:
            return memory_graph.withheld(self.store.db, ids, scopes)

    def conflict_notes(self, ids, scopes=None):
        with self.store.lock:
            return memory_graph.conflict_notes(self.store.db, ids, scopes)

    def disputed(self, notes):
        with self.store.lock:
            return memory_graph.disputed(self.store.db, notes)

    def relate_person(self, mid, correction=False):
        """V1-035: the relation step for a record the person saved (memory_save,
        capture_user). One transaction; see memory_graph.relate_person. It reads
        and writes memory rows only and never grants anything."""
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Relate memory outside an application transaction.')
            with db:
                return memory_graph.relate_person(db, mid, time.time(), fingerprint, correction)

    def proposal_note(self, result):
        """Phase 46 (1.9.28, F2): a MemoryPropose result, with NOT_SAVED_NOTE or WAITING_NOTE when a profile proposal
        was not saved (the statuses and reasons are unchanged)."""
        ident = result.get('candidate_id') if isinstance(result, dict) else None
        rows = self.store.query('SELECT kind FROM memory_candidates WHERE id=?', (ident,)) if ident else []
        if not rows or rows[0]['kind'] != 'profile':
            return result
        if result.get('status') == 'unsupported':
            return {**result, 'note': NOT_SAVED_NOTE}
        if result.get('status') == 'pending' and result.get('reason') == AWAITING:
            return {**result, 'note': WAITING_NOTE}
        return result

    def supersede_named(self, mid):
        """Phase 46 (1.9.28, F3): a captured statement that names the value it replaces supersedes the older records
        holding it (memory_graph.supersede_named). One transaction; memory rows only, never a grant."""
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Relate memory outside an application transaction.')
            with db:
                return memory_graph.supersede_named(db, mid, time.time(), fingerprint)

    def supersede_slot(self, mid):
        """Phase 46 (1.9.28, follow-up 2, D183): a captured statement holding a newer value of a slot (a date format, a
        currency ...) supersedes the older record of the person's that holds only that slot (memory_graph
        .supersede_slot). One transaction; memory rows only, never a grant."""
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Relate memory outside an application transaction.')
            with db:
                return memory_graph.supersede_slot(db, mid, time.time(), fingerprint)

    def resolve(self, ident, accept):
        """Native answer to a conflicting candidate, a nomination waiting for
        confirmation, or a contested record (accept keeps it, reject forgets it)."""
        self.outside_transaction()
        if type(accept) is not bool:
            raise PolicyError('Choose whether to accept the supported conflicting record.')
        if self.store.query("SELECT 1 FROM memory_evidence WHERE memory_id=? AND status='contested'", (ident,)):
            return self.resolve_contest(ident, 'keep' if accept else 'reject')
        rows = self.store.query("SELECT * FROM memory_candidates WHERE id=? AND (status='conflict' OR status='pending' AND reason=?)", (ident, AWAITING))
        if not rows:
            raise PolicyError('No conflicting candidate found.')
        nomination = rows[0]['status'] == 'pending'
        self.store.query('UPDATE memory_candidates SET status=?,reason=?,updated=? WHERE id=?', ('pending' if accept else 'rejected', ('native_confirm' if nomination else 'native_accept') if accept else 'native_reject', time.time(), ident))
        return self.maintain(candidate_id=ident, human_resolution=not nomination) if accept else {'status': 'rejected'}

    def resolve_contest(self, memory_id, decision):
        """Native resolution, the only way a contest clears. 'keep' forgets the
        contradicting records, 'reject' forgets this one, 'keep_both' marks the
        disagreement resolved so neither is contested again by these versions."""
        self.outside_transaction()
        if decision not in CONTEST_DECISIONS:
            raise PolicyError('Choose keep, reject or keep_both.')
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Resolve memory outside an application transaction.')
            with db:
                if memory_graph.status(db, memory_id) != 'contested':
                    raise PolicyError('Only a contested record can be resolved.')
                now = time.time()
                others = [other for other, _ in memory_graph.open_contradictions(db, memory_id)]
                if decision == 'reject':
                    self._forget(db, memory_id, now)
                elif decision == 'keep':
                    for other in others:
                        self._forget(db, other, now)
                    memory_graph.drop_contradictions(db, memory_id, others)  # counterparts no longer stored
                else:
                    memory_graph.resolve_edges(db, memory_id, others, 'native_keep_both', now)
                memory_graph.settle(db, [memory_id, *others], now)
                result = memory_graph.status(db, memory_id)
        if decision != 'keep_both':
            self._compact()
        return {'memory_id': memory_id, 'decision': decision, 'status': result or 'forgotten', 'authorization': 'none'}

    # Forgetting ------------------------------------------------------------

    def forget(self, ident, working=False, compact=True):
        """Native forgetting of one record (or a chat's working record). The
        FTS index is merged afterwards so the forgotten text leaves its segment
        blocks (V1-057); a caller forgetting a batch passes compact=False and
        calls Memory.compact_search() once at the end."""
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Forget memory outside an application transaction.')
            with db:
                if working:
                    db.execute('DELETE FROM checkpoints WHERE chat_id=?', (ident,))
                    db.execute('INSERT OR REPLACE INTO memory_watermarks VALUES(?,?)', ('working:' + ident, time.time()))
                    return True
                self._forget(db, ident, time.time())
        if compact:
            self._compact()
        return True

    def _compact(self):
        compact = getattr(getattr(self.app, 'memory', None), 'compact_search', None)
        if callable(compact):
            compact()

    def _forget(self, db, ident, now):
        """Native forgetting of a record, its slot versions and its same-scope
        duplicates. Each leaves a tombstone; graph edges go with the records."""
        pending, done, removed = [ident], set(), set()
        origin = db.execute('SELECT scope,kind FROM memories WHERE id=?', (ident,)).fetchone()
        if not origin:
            return
        while pending and len(done) < memory_graph.MAX_PEERS * 2:
            mid = pending.pop()
            if mid in done:
                continue
            done.add(mid)
            row = db.execute('SELECT scope,kind,content FROM memories WHERE id=?', (mid,)).fetchone()
            if not row or tuple(row[:2]) != tuple(origin):
                continue
            link = db.execute('SELECT slot,fingerprint FROM memory_evidence WHERE memory_id=?', (mid,)).fetchone()
            # Unlinked records tombstone their own ID, so host records
            # with stable IDs (episodes, outcome observations) stay
            # forgotten when a resumed run settles again.
            slot, fp = link or (mid, fingerprint(row[1], row[0], row[2]))
            self._tombstone(db, row[0], row[1], slot, fp, now)
            db.execute("UPDATE memory_candidates SET content='',evidence='[]',status='forgotten',reason='human_forget',updated=? WHERE scope=? AND kind=? AND (slot=? OR fingerprint=?)", (now, row[0], row[1], slot, fp))
            ids = [r[0] for r in db.execute('SELECT memory_id FROM memory_evidence WHERE scope=? AND kind=? AND slot=?', (row[0], row[1], slot))] if link else [mid]
            # A person record's slot is its own; the versions it superseded go with it (V1-035).
            pending += [r[0] for r in db.execute('SELECT memory_id FROM memory_evidence WHERE superseded_by=? AND candidate_id LIKE ?',
                                                 (mid, memory_graph.PERSON_CANDIDATE + '%'))]
            for version in ids:
                pending += memory_graph.duplicates_of(db, version)
                db.execute('DELETE FROM memories WHERE id=?', (version,))
                db.execute('DELETE FROM memory_evidence WHERE memory_id=?', (version,))
                removed.add(version)
        memory_graph.detach(db, removed, now, native=True)

    def clear(self, cid=None):
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Clear learning outside an application transaction.')
            with db:
                now = time.time()
                db.execute('INSERT OR REPLACE INTO memory_watermarks VALUES(?,?)', ('chat:' + cid if cid else 'all', now))
                if cid:
                    # V1-021: what the person asked Pebble to remember outlives the conversation it was typed in.
                    db.execute(KEEP_PERSON, (cid,))
                    # A native deletion: contests with this conversation's records end.
                    memory_graph.detach(db, [r[0] for r in db.execute('SELECT id FROM memories WHERE chat_id=?', (cid,))], now)
                else:
                    db.execute('DELETE FROM memory_relations')
                    db.execute('DELETE FROM memory_claims')
                for table in ('memory_candidates', 'memory_evidence', 'memories', 'checkpoints'):
                    db.execute('DELETE FROM ' + table + (' WHERE chat_id=?' if cid else ''), (cid,) if cid else ())
                if cid:
                    db.execute('DELETE FROM memory_run_consent WHERE chat_id=?', (cid,))
        return True
