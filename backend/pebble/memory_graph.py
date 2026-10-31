"""Memory relationship graph, contradiction claims and tombstone lifecycle.

Edges connect memory IDs and are host records, never model text:

- supersedes: newer version -> the version it replaced;
- contradicts: newer -> older record with the same normalized entity/attribute
  claim and a different value (read in both directions);
- duplicates: hidden copy -> the canonical record it repeats;
- derived_from: derived record (a host lesson) -> a record it came from.

A contested record has evidence status 'contested', which recall already
withholds. withheld() also withholds a global record wherever the record
contesting it is visible, collapses a duplicate recalled with its canonical
record, and conflict_notes() names both IDs of a disagreement. Host processes
(new evidence, maintenance, expiry, eviction, import) never clear a contest;
only a native resolution or native forgetting removes or resolves a
contradicts edge. Edge evidence holds a bounded key label and basis, never a
quote or value.

Tombstones never expire by time. Project scopes are stored as salted HMACs;
'global' and 'chat:<id>' stay literal. On overflow a scope's oldest rows fold
into its Bloom filter, whose false positives forget and never recall. Nothing
here reads files, starts a model, executes or grants anything.

Records the person saved (Settings, claim-set seeds, "Remember that ...") have
person claims of their own: 'my <attribute> is <value>' and '<Entity>: the
<attribute> is <value>' (V1-035). Person claims only meet person claims. A
record enters the graph only when it relates to another one; it then gets a
'person:' evidence row with empty proof, so recall presents it as before. A
same-key, different-value pair is contested like learned records; a direct
correction ("Remember that my name is Sam") supersedes the older same-scope
versions instead. A conflict note may name the person's own disputed values;
learned contests stay content-free.
"""

import hashlib
import hmac
import json
import re
import secrets

from . import memory_facts

GRAPH_DDL = '''
CREATE TABLE IF NOT EXISTS memory_relations(
  from_id TEXT NOT NULL, to_id TEXT NOT NULL,
  relation TEXT NOT NULL CHECK(relation IN ('supersedes','contradicts','duplicates','derived_from')),
  evidence TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(from_id,to_id,relation));
CREATE INDEX IF NOT EXISTS memory_relations_to ON memory_relations(to_id,relation);
CREATE TABLE IF NOT EXISTS memory_claims(
  memory_id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL, claim_key TEXT NOT NULL,
  label TEXT NOT NULL, value TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS memory_claims_key ON memory_claims(claim_key,kind,scope);
CREATE TABLE IF NOT EXISTS memory_tombstone_blooms(
  scope TEXT PRIMARY KEY, bits BLOB NOT NULL, items INTEGER NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_scope_salts(
  salt_id TEXT PRIMARY KEY, salt TEXT NOT NULL UNIQUE,
  origin TEXT NOT NULL CHECK(origin IN ('local','imported')), created REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS memory_scope_salts_local ON memory_scope_salts(origin) WHERE origin='local';
'''
RELATIONS = ('supersedes', 'contradicts', 'duplicates', 'derived_from')
LIVE = ('active', 'contested')
LIVE_MARKS = ','.join('?' * len(LIVE))
CLAIM_KINDS = ('semantic', 'temporal')
MAX_PEERS = 32
MAX_SCREEN = 512
MAX_NOTES = 4
MAX_FOREIGN_SALTS = 8
BLOOM_BITS = 1 << 16
BLOOM_HASHES = 7
BLOOM_BYTES = BLOOM_BITS // 8
NOTE = ('These learned records disagree, so neither is recalled as current. Check current evidence; '
        'only a native resolution in Settings clears this.')
PERSON_ORIGINS = ('user', 'direct-user')
PERSON_KINDS = ('semantic', 'profile')
PERSON_KEY = 'person|'
PERSON_CANDIDATE = 'person:'  # evidence candidate_id and slot prefix of a person record in the graph
PERSON_RUN = 'person'
MAX_CLAIM_TEXT = 300
MAX_SHOWN = 60
MAX_DISPUTED = 4
_ATTRIBUTE = r"(?P<attr>[^\W\d_][\w'’ -]{0,39}?)"
_SELF_CLAIM = re.compile(r'my\s+' + _ATTRIBUTE + r'\s+(?:is|are)\s+(?P<value>\S.*)', re.I)
_LABELLED_CLAIM = re.compile(r'(?P<entity>[^\W_][^:\n]{0,59}?)\s*:\s+(?:the|my|our)\s+' + _ATTRIBUTE
                             + r'\s+(?:is|are)\s+(?P<value>\S.*)', re.I)
# A value ends at a parenthesis, clause break or sentence end ("3071 (from the letter)" -> "3071").
_VALUE_END = re.compile(r'\s*(?:[(\[,;—–]|\s-\s|\s(?:and|but|because|since|which|who|until|unless|so)\s|[.!?](?:\s|$))',
                        re.I)


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _load(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# Scopes, salts and tombstone items ---------------------------------------

def new_salt():
    return secrets.token_hex(32)


def salt_id(salt):
    return hashlib.sha256(bytes.fromhex(salt)).hexdigest()[:12]


def literal(scope):
    """Stored as given: global, a conversation or an already keyed project."""
    return scope == 'global' or scope.startswith(('chat:', 'p:'))


def scope_key(scope, salt):
    """The stored tombstone scope: literal, or a salted HMAC of a project path."""
    if literal(scope):
        return scope
    return 'p:' + salt_id(salt) + ':' + hmac.new(bytes.fromhex(salt), scope.encode(), 'sha256').hexdigest()[:40]


def scope_keys(scope, salts):
    """Every stored form naming a scope: a legacy literal row and each known salt."""
    if literal(scope):
        return [scope]
    return list(dict.fromkeys([scope] + [scope_key(scope, salt) for salt in salts]))


def item(namespace, kind, value):
    return hashlib.sha256('\x1f'.join(('pebble-tombstone/1', namespace, kind, value)).encode()).hexdigest()


def items(kind, slots=(), fingerprint=None):
    """Bloom items of a tombstone: one per slot, plus its fingerprint."""
    found = [item('slot', kind, slot) for slot in dict.fromkeys(slots) if slot]
    return found + ([item('fingerprint', kind, fingerprint)] if fingerprint else [])


def _positions(digest):
    raw = bytes.fromhex(digest)
    first, step = int.from_bytes(raw[:8], 'big'), int.from_bytes(raw[8:16], 'big') | 1
    return [(first + i * step) % BLOOM_BITS for i in range(BLOOM_HASHES)]


def bloom_add(bits, digests):
    bits = bytearray(bits if bits is not None else bytes(BLOOM_BYTES))
    for digest in digests:
        for position in _positions(digest):
            bits[position >> 3] |= 1 << (position & 7)
    return bytes(bits)


def bloom_has(bits, digest):
    # A malformed filter answers "forgotten" for everything.
    return len(bits) != BLOOM_BYTES or all(bits[p >> 3] >> (p & 7) & 1 for p in _positions(digest))


def bloom_merge(db, key, digests=(), bits=None, count=0, now=0.0):
    row = db.execute('SELECT bits,items FROM memory_tombstone_blooms WHERE scope=?', (key,)).fetchone()
    merged = bytes(BLOOM_BYTES) if not row else row[0] if len(row[0]) == BLOOM_BYTES else b'\xff' * BLOOM_BYTES
    if bits is not None:
        merged = bytes(a | b for a, b in zip(merged, bits))
    db.execute('INSERT OR REPLACE INTO memory_tombstone_blooms VALUES(?,?,?,?)',
               (key, bloom_add(merged, digests), (row[1] if row else 0) + count, now))


def bloom_forgotten(db, keys, kind, slots, fingerprint):
    rows = db.execute('SELECT bits FROM memory_tombstone_blooms WHERE scope IN (' + ','.join('?' * len(keys)) + ')',
                      list(keys)).fetchall()
    wanted = items(kind, slots, fingerprint)
    return any(bloom_has(bits, digest) for (bits,) in rows for digest in wanted)


def fold(db, key, keep, now):
    """Fold all but the newest `keep` tombstone rows of one stored scope into its filter."""
    rows = db.execute('SELECT kind,slot,fingerprint FROM memory_tombstones WHERE scope=? '
                      'ORDER BY created DESC,kind,slot,fingerprint LIMIT -1 OFFSET ?', (key, keep)).fetchall()
    if not rows:
        return 0
    bloom_merge(db, key, [d for kind, slot, fp in rows for d in items(kind, [slot], fp)], count=len(rows), now=now)
    db.executemany('DELETE FROM memory_tombstones WHERE scope=? AND kind=? AND slot=? AND fingerprint=?',
                   [(key, *row) for row in rows])
    return len(rows)


# Edges and statuses --------------------------------------------------------

def add_edge(db, source, target, relation, evidence, now):
    if relation in RELATIONS and source != target:
        db.execute('INSERT OR IGNORE INTO memory_relations VALUES(?,?,?,?,?)',
                   (source, target, relation, packed(evidence), now))


def edge(db, a, b, relation):
    """Evidence of the relation between a and b in either direction, or None."""
    row = db.execute('SELECT evidence FROM memory_relations WHERE relation=? AND (from_id=? AND to_id=? OR from_id=? AND to_id=?)',
                     (relation, a, b, b, a)).fetchone()
    return _load(row[0]) if row else None


def migrate_supersedes(db, now):
    """Idempotent: every superseded_by link becomes a supersedes edge."""
    before = db.total_changes
    db.execute("INSERT OR IGNORE INTO memory_relations SELECT superseded_by,memory_id,'supersedes',?,? FROM memory_evidence "
               'WHERE superseded_by IS NOT NULL AND superseded_by<>memory_id', (packed({'basis': 'superseded_by'}), now))
    return db.total_changes - before


def status(db, mid):
    row = db.execute('SELECT status FROM memory_evidence WHERE memory_id=?', (mid,)).fetchone()
    return row[0] if row else None


def scope(db, mid):
    row = db.execute('SELECT scope FROM memories WHERE id=?', (mid,)).fetchone()
    return row[0] if row else None


def open_contradictions(db, mid):
    """(counterpart, evidence) of the unresolved contradicts edges touching mid."""
    rows = db.execute("SELECT from_id,to_id,evidence FROM memory_relations WHERE relation='contradicts' "
                      'AND (from_id=? OR to_id=?) ORDER BY created,from_id,to_id LIMIT ?', (mid, mid, MAX_PEERS * 4)).fetchall()
    found = []
    for a, b, text in rows:
        evidence = _load(text)
        if not evidence.get('resolution'):
            found.append((b if a == mid else a, evidence))
    return found


def _blocks(own, other):
    """A counterpart contests a record when both are recalled together (same
    scope or a global counterpart), or when it is no longer stored."""
    return other is None or other == own or other == 'global'


def settle(db, ids, now):
    """Recompute live statuses from open contradicts edges: contested or active."""
    changed = {}
    for mid in dict.fromkeys(ids):
        current = status(db, mid)
        own = scope(db, mid)
        if current not in LIVE or own is None:
            continue
        new = 'contested' if any(_blocks(own, scope(db, other)) for other, _ in open_contradictions(db, mid)) else 'active'
        if new != current:
            db.execute('UPDATE memory_evidence SET status=?,updated=? WHERE memory_id=?', (new, now, mid))
            changed[mid] = new
    return changed


# Claims and relating -------------------------------------------------------

def claim_of(kind, content, proof):
    """{'key', 'label', 'value'} of a keyed semantic/temporal record, or None.
    The value is a digest; the label is the source's own bounded key."""
    if kind not in CLAIM_KINDS:
        return None
    report = _load(content)
    if kind == 'semantic':
        found = memory_facts.fact_claim(report)
    else:
        found = memory_facts.dated_claim(report.get('quote'), (proof or {}).get('subject'))
    if not found:
        return None
    return {'key': found['entity'] + '|' + found['attribute'], 'label': found['label'],
            'value': hashlib.sha256(found['value'].encode()).hexdigest()[:32]}


def _duplicate(db, newer, older, basis, now):
    if edge(db, newer, older, 'duplicates') is None:
        add_edge(db, newer, older, 'duplicates', {'basis': basis}, now)


def relate(db, mid, now):
    """Record mid's claim and its duplicate/contradiction edges with live peers
    in comparable scopes (its own and global), then settle every touched
    status. Returns {'status', 'contested_with'} or None for a non-live record."""
    row = db.execute('SELECT m.kind,m.scope,m.content,m.created,e.status,e.fingerprint,e.proof,e.candidate_id FROM memories m '
                     'JOIN memory_evidence e ON e.memory_id=m.id WHERE m.id=?', (mid,)).fetchone()
    if not row or row[4] not in LIVE:
        return None
    if row[7].startswith(PERSON_CANDIDATE):
        # Person claims are related when the person saves (relate_person); a learned claim never replaces them.
        return {'status': row[4], 'contested_with': [other for other, _ in open_contradictions(db, mid)]}
    kind, own, content, created, _, fp, proof, _ = row
    claim = claim_of(kind, content, _load(proof))
    db.execute('INSERT OR REPLACE INTO memory_claims VALUES(?,?,?,?,?,?,?)',
               (mid, own, kind, *(claim[k] if claim else '' for k in ('key', 'label', 'value')), created))
    touched = [mid]
    if claim:
        peers = db.execute('SELECT c.memory_id,c.value,c.created FROM memory_claims c JOIN memory_evidence e ON e.memory_id=c.memory_id '
                           'WHERE c.claim_key=? AND c.kind=? AND c.scope IN (?,?) AND c.memory_id<>? AND e.status IN (' + LIVE_MARKS + ') '
                           'ORDER BY c.created,c.memory_id LIMIT ?', (claim['key'], kind, own, 'global', mid, *LIVE, MAX_PEERS)).fetchall()
        for peer, value, peer_created in peers:
            newer, older = (mid, peer) if (created, mid) > (peer_created, peer) else (peer, mid)
            if value == claim['value']:
                _duplicate(db, newer, older, 'claim', now)
            elif edge(db, mid, peer, 'contradicts') is None:
                add_edge(db, newer, older, 'contradicts', {'basis': 'claim', 'kind': kind, 'key': claim['label']}, now)
            touched.append(peer)
    if fp and kind != 'procedural':  # every procedural candidate shares the empty-content fingerprint
        peers = db.execute('SELECT e.memory_id,m.created FROM memory_evidence e JOIN memories m ON m.id=e.memory_id '
                           'WHERE e.scope=? AND e.kind=? AND e.fingerprint=? AND e.memory_id<>? AND e.status IN (' + LIVE_MARKS + ') '
                           'ORDER BY m.created,e.memory_id LIMIT ?', (own, kind, fp, mid, *LIVE, MAX_PEERS)).fetchall()
        for peer, peer_created in peers:
            newer, older = (mid, peer) if (created, mid) > (peer_created, peer) else (peer, mid)
            _duplicate(db, newer, older, 'fingerprint', now)
            touched.append(peer)
    settle(db, touched, now)
    return {'status': status(db, mid), 'contested_with': [other for other, _ in open_contradictions(db, mid)]}


# Person-saved records (V1-035) ---------------------------------------------

def person_claim(content):
    """{'key', 'label', 'value', 'shown'} of a one-line person statement, or None.

    Two narrow shapes only: 'my <attribute> is <value>' and '<Entity>: the
    <attribute> is <value>'. The value is cut at a parenthesis, clause or
    sentence end; 'value' is its digest and 'shown' its bounded text."""
    text = content.strip() if isinstance(content, str) else ''
    if not text or len(text) > MAX_CLAIM_TEXT or '\n' in text:
        return None
    match = _SELF_CLAIM.fullmatch(text)
    entity = label = None
    if match:
        entity = 'me'
    else:
        match = _LABELLED_CLAIM.fullmatch(text)
        if not match:
            return None
        label = ' '.join(match['entity'].split())
        entity = label.casefold()
    attribute = ' '.join(match['attr'].split())
    if len(attribute.split()) > 5:
        return None
    shown = _VALUE_END.split(match['value'], 1)[0].strip().strip('"\'“”‘’`*').strip()
    value = ' '.join(shown.casefold().split()).rstrip('.').strip()
    if not value or len(value) > memory_facts.MAX_CLAIM_CHARS or value.startswith(('not ', 'no longer')):
        return None
    return {'key': PERSON_KEY + entity + '|' + attribute.casefold(),
            'label': (attribute if label is None else label + ': ' + attribute)[:memory_facts.MAX_CLAIM_CHARS],
            'value': hashlib.sha256(value.encode()).hexdigest()[:32], 'shown': shown[:MAX_SHOWN]}


def person_record(db, mid):
    """Whether mid's evidence row is a person row (not learned or imported evidence)."""
    row = db.execute('SELECT candidate_id FROM memory_evidence WHERE memory_id=?', (mid,)).fetchone()
    return bool(row and row[0].startswith(PERSON_CANDIDATE))


def _person_link(db, mid, now, fingerprint):
    """Give a person record an evidence row once it relates to another record.
    The proof is empty, so recall presents it exactly as an unlinked record;
    the slot is its own, so forgetting one side of a contest keeps the other."""
    if db.execute('SELECT 1 FROM memory_evidence WHERE memory_id=?', (mid,)).fetchone():
        return
    kind, own, content = db.execute('SELECT kind,scope,content FROM memories WHERE id=?', (mid,)).fetchone()
    db.execute('INSERT INTO memory_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (mid, PERSON_CANDIDATE + mid, PERSON_RUN, '', own, kind, PERSON_CANDIDATE + mid,
                fingerprint(kind, own, content), 'active', '{}', None, now))


def relate_person(db, mid, now, fingerprint, correction=False):
    """The relation step for a record the person saved; the caller holds the transaction.

    Peers are person records of the same kind and claim key in mid's scope or
    'global' (any scope when mid is global) that are not superseded. A peer with
    the same value is a duplicate. A different value is contested, except that
    a direct correction (correction=True, the person's own "Remember that ...")
    supersedes same-scope peers and ends their open contests: the most recent
    explicit statement wins. Returns {'status', 'key', 'contested_with',
    'superseded', 'duplicates'}, or None when mid has no person claim."""
    row = db.execute('SELECT kind,scope,content,created,origin FROM memories WHERE id=?', (mid,)).fetchone()
    if not row or row[0] not in PERSON_KINDS or row[4] not in PERSON_ORIGINS:
        return None
    kind, own, content, created, _ = row
    link = db.execute('SELECT candidate_id,status FROM memory_evidence WHERE memory_id=?', (mid,)).fetchone()
    if link and not link[0].startswith(PERSON_CANDIDATE):
        return None
    claim = person_claim(content)
    if not claim:
        db.execute('DELETE FROM memory_claims WHERE memory_id=?', (mid,))
        return None
    if link and link[1] not in LIVE:
        if not correction:
            return {'status': link[1], 'key': claim['label'], 'contested_with': [], 'superseded': [], 'duplicates': []}
        # Saying a superseded value again makes it current again.
        db.execute("UPDATE memory_evidence SET status='active',superseded_by=NULL,updated=? WHERE memory_id=?", (now, mid))
        db.execute("DELETE FROM memory_relations WHERE to_id=? AND relation='supersedes'", (mid,))
    db.execute('INSERT OR REPLACE INTO memory_claims VALUES(?,?,?,?,?,?,?)',
               (mid, own, kind, claim['key'], claim['label'], claim['value'], created))
    peers = db.execute(
        'SELECT c.memory_id,c.value,c.scope,c.created FROM memory_claims c JOIN memories m ON m.id=c.memory_id '
        'LEFT JOIN memory_evidence e ON e.memory_id=c.memory_id WHERE c.claim_key=? AND c.kind=? AND c.memory_id<>? '
        "AND (c.scope IN (?,'global') OR ?='global') AND m.origin IN (?,?) "
        'AND (e.status IS NULL OR e.status IN (' + LIVE_MARKS + ')) ORDER BY c.created,c.memory_id LIMIT ?',
        (claim['key'], kind, mid, own, own, *PERSON_ORIGINS, *LIVE, MAX_PEERS)).fetchall()
    result = {'key': claim['label'], 'contested_with': [], 'superseded': [], 'duplicates': []}
    touched = [mid]
    for peer, value, where, peer_created in peers:
        newer, older = (mid, peer) if (created, mid) > (peer_created, peer) else (peer, mid)
        if value == claim['value']:
            _duplicate(db, newer, older, 'person_claim', now)
            result['duplicates'].append(peer)
        elif correction and where == own:
            _person_link(db, mid, now, fingerprint)
            _person_link(db, peer, now, fingerprint)
            db.execute("UPDATE memory_evidence SET status='superseded',superseded_by=?,updated=? WHERE memory_id=?",
                       (mid, now, peer))
            add_edge(db, mid, peer, 'supersedes', {'basis': 'person_correction', 'key': claim['label']}, now)
            others = [other for other, _ in open_contradictions(db, peer)]
            resolve_edges(db, peer, others, 'person_correction', now)
            touched += others
            result['superseded'].append(peer)
        else:
            if edge(db, mid, peer, 'contradicts') is None:
                _person_link(db, mid, now, fingerprint)
                _person_link(db, peer, now, fingerprint)
                add_edge(db, newer, older, 'contradicts', {'basis': 'person_claim', 'kind': kind, 'key': claim['label']}, now)
            touched.append(peer)
    settle(db, touched, now)
    result['contested_with'] = [other for other, _ in open_contradictions(db, mid)]
    current = status(db, mid)
    result['status'] = ('contested' if current == 'contested' else 'updated' if result['superseded']
                        else 'duplicate' if result['duplicates'] else 'saved')
    return result


# Phase 46 (1.9.28, F3; golden held-out memory): an update that names the value it replaces ("dates should be DD.MM.YYYY,
# not DD/MM/YYYY", "use ISO dates instead of DD.MM.YYYY") left the old record a standing preference beside the new one.
# Only a distinctive value counts (a digit, '.', '/', '_', '-', ':' or '@', or a capitalised code such as CHF), so
# "tabs, not spaces" never replaces a record that mentions spaces.
_REPLACED = re.compile(r"\b(?:instead\s+of|rather\s+than|not|no\s+longer|stop\s+using|in\s+place\s+of|replac\w*)\s+"
                       r"(?:the\s+|my\s+|using\s+|with\s+)?[`'\"“‘(]*([^\s,;:()`'\"“”‘’]+)", re.I)
_TOKEN = re.compile(r"[^\s,;:()`'\"“”‘’]+")
# Follow-up 1 (the w5 security review, finding 6: "the server uses Python 3.11, not 3.12" replaced "my laptop runs
# Python 3.12"; "my build uses Node 20, not 18", global, replaced another project's record): the older record must have
# the same subject, the words before its verb ("dates", "payments account"), and the same scope.
_SUBJECT_LEAD = re.compile(r"^(?:(?:please|also|so|and|actually|correction|update)[,:]? )*(?:(?:remember|note|keep in mind)"
                           r"(?: that)?[,:]? )?(?:(?:for|in|across) (?:all )?(?:future|later|next|upcoming|our next) "
                           r"(?:sessions?|time)[,:]? )?", re.I)
_SUBJECT_VERB = re.compile(r"(?:is|are|was|were|should|must|will|can|may|might|would|could|shall|has|have|had|uses?|used|"
                           r"runs?|keeps?|lives?|goes|go|moved?|stays?|needs?|means?|points?|holds?|sits?|starts?|works?|"
                           r"belongs?|listens?|comes?|takes?|gets?|prefers?|wants?|likes?|writes?|now)", re.I)
_ARTICLES = frozenset('the a an my our your their its this that these those'.split())


def replaced_values(content):
    """The distinctive values a person statement says it replaces, casefolded."""
    found = []
    for match in _REPLACED.finditer(content if isinstance(content, str) else ''):
        value = match[1].rstrip('.!?')
        if len(value) >= 2 and (re.search(r'[\d./_:@-]', value) or len(value) >= 3 and value.isupper()):
            found.append(value.casefold())
    return found


def subject_of(content):
    """The words before a statement's verb, casefolded and without articles ("payments account" for "my payments
    account is DE89-1234."), or None without a verb among its first ten words."""
    words = re.findall(r"[\w'’./-]+", _SUBJECT_LEAD.sub('', ' '.join(str(content).split())))[:10]
    subject = []
    for word in words:
        if _SUBJECT_VERB.fullmatch(word):
            return tuple(subject) or None
        if word.casefold() not in _ARTICLES:
            subject.append(word.casefold())
    return None


def supersede_named(db, mid, now, fingerprint):
    """Phase 46 (1.9.28, F3): the person's statement mid supersedes an older saved record of theirs, or a verified
    learned profile record, that holds a value mid says it replaces; saying a replaced statement again makes it current
    again. Follow-up 1: one record at most, in mid's own scope (never another project's, never across global and a
    project) and with mid's subject (subject_of). The caller holds the transaction. Returns [(ID, content)] replaced."""
    row = db.execute('SELECT kind,scope,content,created,origin FROM memories WHERE id=?', (mid,)).fetchone()
    if not row or row[0] not in PERSON_KINDS or row[4] not in PERSON_ORIGINS:
        return []
    _, own, content, created, _ = row
    if status(db, mid) == 'superseded':
        db.execute("UPDATE memory_evidence SET status='active',superseded_by=NULL,updated=? WHERE memory_id=?", (now, mid))
        db.execute("DELETE FROM memory_relations WHERE to_id=? AND relation='supersedes'", (mid,))
    values, subject = replaced_values(content), subject_of(content)
    if not values or not subject:
        return []
    peers = db.execute(
        "SELECT m.id,m.content FROM memories m LEFT JOIN memory_evidence e ON e.memory_id=m.id "
        "WHERE m.id<>? AND m.kind IN ('profile','semantic') AND m.scope=? "
        "AND (m.origin IN (?,?) OR m.origin='auto-learned' AND m.kind='profile') "
        'AND (e.status IS NULL OR e.status IN (' + LIVE_MARKS + ')) AND m.created<=? ORDER BY m.created DESC LIMIT ?',
        (mid, own, *PERSON_ORIGINS, *LIVE, created, MAX_PEERS)).fetchall()
    for peer, text in peers:
        if not set(values) & {token.casefold().rstrip('.!?') for token in _TOKEN.findall(text)} \
                or subject_of(text) != subject:
            continue
        _person_link(db, mid, now, fingerprint)
        _person_link(db, peer, now, fingerprint)  # a learned peer has its evidence row already
        db.execute("UPDATE memory_evidence SET status='superseded',superseded_by=?,updated=? WHERE memory_id=?",
                   (mid, now, peer))
        add_edge(db, mid, peer, 'supersedes', {'basis': 'person_replaced_value'}, now)
        return [(peer, text)]
    return []


def supersede_slot(db, mid, now, fingerprint, learned=False):
    """Phase 46 (1.9.28, follow-up 2, D183): the person's newer statement of what a slot holds (memory_facts
    .preference_slots: a date format, a currency, decimals, units, the clock) replaces one older record of theirs, or a
    verified learned profile record, in mid's own scope that holds another value of those slots and says nothing else
    (memory_facts.slot_only): "dates should be YYYY-MM-DD from now on" replaces "I want dates written as DD.MM.YYYY.",
    never "... and my staging settings are in deploy/staging.ini" nor a record about another slot. `learned`: mid is the
    person's host-verified quote (memory_learning promotes it). The caller holds the transaction. Returns [(ID,
    content)] replaced. w17 (the follow-ups review's finding 5): mid must be the person's own word on the slot itself
    (memory_facts.own_slot), so "our invoices are in EUR" never replaces "I want conversions in CHF"."""
    row = db.execute('SELECT kind,scope,content,created,origin FROM memories WHERE id=?', (mid,)).fetchone()
    if not row or row[0] not in PERSON_KINDS or row[4] not in PERSON_ORIGINS and not (learned and row[4] == 'auto-learned'):
        return []
    _, own, content, created, _ = row
    new = memory_facts.preference_slots(content)
    if not new or not memory_facts.own_slot(content) or status(db, mid) not in (None, 'active'):
        return []
    peers = db.execute(
        "SELECT m.id,m.content FROM memories m LEFT JOIN memory_evidence e ON e.memory_id=m.id "
        "WHERE m.id<>? AND m.kind IN ('profile','semantic') AND m.scope=? "
        "AND (m.origin IN (?,?) OR m.origin='auto-learned' AND m.kind='profile') "
        'AND (e.status IS NULL OR e.status IN (' + LIVE_MARKS + ')) AND m.created<=? ORDER BY m.created DESC LIMIT ?',
        (mid, own, *PERSON_ORIGINS, *LIVE, created, MAX_PEERS)).fetchall()
    for peer, text in peers:
        old = memory_facts.preference_slots(text)
        if not old or not set(old) <= set(new) or all(old[slot] == new[slot] for slot in old) \
                or not memory_facts.slot_only(text):
            continue
        _person_link(db, mid, now, fingerprint)
        _person_link(db, peer, now, fingerprint)
        db.execute("UPDATE memory_evidence SET status='superseded',superseded_by=?,updated=? WHERE memory_id=?",
                   (mid, now, peer))
        add_edge(db, mid, peer, 'supersedes', {'basis': 'person_slot_value'}, now)
        return [(peer, text)]
    return []


def disputed(db, notes, limit=MAX_DISPUTED):
    """The person's own values for each disagreement among conflict `notes`
    whose records are both person claims with one key: [{'topic', 'values'}].
    Learned records never contribute, so their notes stay content-free."""
    topics = {}
    for note in notes:
        rows = [db.execute('SELECT m.created,m.id,m.content FROM memories m JOIN memory_evidence e ON e.memory_id=m.id '
                           'WHERE m.id=? AND e.candidate_id LIKE ? AND m.origin IN (?,?)',
                           (mid, PERSON_CANDIDATE + '%', *PERSON_ORIGINS)).fetchone() for mid in note.get('memory_ids', ())]
        claims = [person_claim(row[2]) for row in sorted(rows)] if None not in rows else []  # older value first
        if len(claims) != 2 or None in claims or claims[0]['key'] != claims[1]['key']:
            continue
        entry = topics.setdefault(claims[0]['key'], {'topic': claims[0]['label'], 'values': []})
        for claim in claims:
            if claim['shown'].casefold() not in (v.casefold() for v in entry['values']) and len(entry['values']) < 4:
                entry['values'].append(claim['shown'])
        if len(topics) >= limit:
            break
    return list(topics.values())


def detach(db, ids, now, native=True):
    """Drop claims and edges of removed records and settle their neighbours.
    Host removal (expiry, eviction) keeps contradicts edges: the surviving
    record stays contested until a native resolution."""
    removed, touched = set(ids), set()
    for mid in removed:
        db.execute('DELETE FROM memory_claims WHERE memory_id=?', (mid,))
        for a, b, relation in db.execute('SELECT from_id,to_id,relation FROM memory_relations WHERE from_id=? OR to_id=?',
                                         (mid, mid)).fetchall():
            if relation == 'contradicts' and not native:
                continue
            db.execute('DELETE FROM memory_relations WHERE from_id=? AND to_id=? AND relation=?', (a, b, relation))
            touched.add(b if a == mid else a)
    touched -= removed
    settle(db, touched, now)
    return touched


def duplicates_of(db, mid):
    return [row[0] for row in db.execute("SELECT CASE WHEN from_id=? THEN to_id ELSE from_id END FROM memory_relations "
                                         "WHERE relation='duplicates' AND (from_id=? OR to_id=?) LIMIT ?",
                                         (mid, mid, mid, MAX_PEERS)).fetchall()]


def resolve_edges(db, mid, others, resolution, now):
    """Mark contradicts edges between mid and others natively resolved."""
    for other in others:
        for a, b in ((mid, other), (other, mid)):
            row = db.execute("SELECT evidence FROM memory_relations WHERE from_id=? AND to_id=? AND relation='contradicts'",
                             (a, b)).fetchone()
            if row:
                evidence = {**_load(row[0]), 'resolution': resolution, 'resolved': now}
                db.execute("UPDATE memory_relations SET evidence=? WHERE from_id=? AND to_id=? AND relation='contradicts'",
                           (packed(evidence), a, b))


def drop_contradictions(db, mid, others):
    for other in others:
        db.execute("DELETE FROM memory_relations WHERE relation='contradicts' AND (from_id=? AND to_id=? OR from_id=? AND to_id=?)",
                   (mid, other, other, mid))


# Recall and inspection -----------------------------------------------------

def _visible(db, ids, scopes):
    return set(scopes) if scopes is not None else {s for s in (scope(db, i) for i in ids) if s}


def withheld(db, ids, scopes=None):
    """Recall relation filter: {id: reason} for candidates not to show as current.

    Contested records are withheld, and so is any record with an open
    contradiction whose counterpart is visible (stored in `scopes`; by default
    the candidates' own scopes) or no longer stored. A duplicate recalled
    together with the record it repeats collapses into that record. Other
    non-active learning statuses are reported as themselves."""
    ids = [i for i in dict.fromkeys(ids) if isinstance(i, str)][:MAX_SCREEN]
    visible, verdicts, candidates = _visible(db, ids, scopes), {}, set(ids)
    for mid in ids:
        current = status(db, mid)
        if current is not None and current != 'active':
            verdicts[mid] = current if current in ('contested', 'stale', 'superseded', 'needs_revalidation') else 'withheld'
            continue
        own = scope(db, mid)
        if any(where is None or where == own or where in visible
               for where in (scope(db, other) for other, _ in open_contradictions(db, mid))):
            verdicts[mid] = 'contested'
            continue
        canonical = [row[0] for row in db.execute("SELECT to_id FROM memory_relations WHERE from_id=? AND relation='duplicates'", (mid,))]
        if any(c in candidates and status(db, c) in (None, 'active') and scope(db, c) == own for c in canonical):
            verdicts[mid] = 'duplicate'
    return verdicts


def conflict_notes(db, ids, scopes=None, limit=MAX_NOTES):
    """Host notes naming both records of each open disagreement among `ids`."""
    ids = [i for i in dict.fromkeys(ids) if isinstance(i, str)][:MAX_SCREEN]
    visible, notes, seen = _visible(db, ids, scopes), [], set()
    for mid in ids:
        own = scope(db, mid)
        for other, evidence in open_contradictions(db, mid):
            where = scope(db, other)
            pair = tuple(sorted((mid, other)))
            if pair in seen or where is not None and where != own and where not in visible:
                continue
            seen.add(pair)
            body = {'claim_class': 'host_conflict_note', 'memory_ids': list(pair), 'kind': evidence.get('kind'),
                    'key': evidence.get('key'), 'note': NOTE, 'authorization': 'none'}
            notes.append({'id': 'conflict:' + hashlib.sha256('\x1f'.join(pair).encode()).hexdigest()[:24],
                          'kind': 'recall_note', 'origin': 'host', 'note': 'conflict', 'memory_ids': list(pair),
                          'content': packed(body), 'authorization': 'none'})
            if len(notes) >= limit:
                return notes
    return notes


def relations(db, mid=None, limit=100):
    rows = db.execute('SELECT from_id,to_id,relation,evidence,created FROM memory_relations'
                      + (' WHERE from_id=? OR to_id=?' if mid else '') + ' ORDER BY created DESC,from_id,to_id LIMIT ?',
                      (mid, mid, limit) if mid else (limit,)).fetchall()
    return [{'from_id': a, 'to_id': b, 'relation': r, 'evidence': _load(e), 'created': c} for a, b, r, e, c in rows]


# Consolidation steps -------------------------------------------------------

def backfill(db, now, limit):
    """Relate live records that have no claim row yet (legacy, imported)."""
    ids = [row[0] for row in db.execute('SELECT e.memory_id FROM memory_evidence e JOIN memories m ON m.id=e.memory_id '
                                        'LEFT JOIN memory_claims c ON c.memory_id=e.memory_id WHERE c.memory_id IS NULL '
                                        'AND e.status IN (' + LIVE_MARKS + ') ORDER BY m.created LIMIT ?', (*LIVE, limit)).fetchall()]
    for mid in ids:
        relate(db, mid, now)
    return len(ids)


def collect_garbage(db, now, limit):
    """Claims and edges whose records are gone. A dangling contradicts edge
    stays while its surviving record is contested (host removal never clears)."""
    db.execute('DELETE FROM memory_claims WHERE memory_id IN (SELECT c.memory_id FROM memory_claims c '
               'WHERE NOT EXISTS(SELECT 1 FROM memories m WHERE m.id=c.memory_id) LIMIT ?)', (limit,))
    rows = db.execute("SELECT from_id,to_id,relation FROM memory_relations r WHERE NOT EXISTS(SELECT 1 FROM memories m WHERE m.id=r.from_id) "
                      "OR NOT EXISTS(SELECT 1 FROM memories m WHERE m.id=r.to_id) ORDER BY relation='contradicts' LIMIT ?",
                      (limit,)).fetchall()
    touched, removed = set(), 0
    for a, b, relation in rows:
        if relation == 'contradicts' and 'contested' in (status(db, a), status(db, b)):
            continue
        db.execute('DELETE FROM memory_relations WHERE from_id=? AND to_id=? AND relation=?', (a, b, relation))
        touched |= {a, b}
        removed += 1
    settle(db, touched, now)
    return removed
