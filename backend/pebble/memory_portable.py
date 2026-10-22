"""Portable memory archive: export, a pure preview and a one-transaction import.

Format 'pebble-memory-archive/1' is canonical JSON {format, manifest, sections}.
The manifest lists each section's SHA-256 and count. A checksum mismatch, an
unknown or duplicate key, a non-finite number or any size bound refuses the
whole archive. Checksums detect corruption and edits; they do not
authenticate an author, so imported records stay data: origin 'imported',
authorization none.

Export carries records of the layers enabled now (never working checkpoints
or the curated starter procedures), provenance (and a learned record's host
JSON) with run, receipt and input IDs replaced by installation-keyed opaque
tokens, relations among exported records, every tombstone as a keyed scope
plus item digests, folded Bloom filters, the layer switches and the consent
policy. Unlinked records travel verbatim, since their fingerprint covers the
whole text. Pending candidates and operational journals stay behind. A record
with credential-shaped text is refused and reported by ID; its text never
leaves.

Import is preview(archive, local_state) -> plan, then apply(), which rebuilds
the plan inside one write transaction and refuses it if the local state moved.
Tombstones apply first and forget matching local records, which the preview
lists. A record is skipped for a disabled layer (here or at
the source), a local or archived tombstone, a clear watermark, an unknown
project or conversation, expiry or an existing fingerprint. Project scopes
map only by salted path hash, so a record lands only in a local project at
the same path. File facts and recipe guidance arrive as needs_revalidation;
nothing imported executes, grants or counts as current source evidence. Local
switches and consent are never changed, and forgetting persists across
re-import.
"""

import base64
import hashlib
import hmac
import json
import math
import re
import time

from . import memory_facts, memory_graph
from .memory import KINDS
from .memory_learning import fingerprint, lesson_signature, tombstone_slots
from .policy import PolicyError

FORMAT = 'pebble-memory-archive/1'
SECTIONS = ('policy', 'scopes', 'records', 'relations', 'tombstones', 'blooms')
LAYERS = tuple(k for k in KINDS if k != 'working')
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 4000
MAX_RELATIONS = 8000
MAX_TOMBSTONES = 20000
MAX_BLOOMS = 256
MAX_SALTS = 1 + memory_graph.MAX_FOREIGN_SALTS
MAX_CONTENT = 4000
MAX_PROVENANCE = 8000
MAX_EVIDENCE = 1000
MAX_IMPORTED = 3000
MAX_PROJECTS = 1024
MAX_DECISIONS = 200
EXPORT_STATUSES = (None, 'active', 'contested', 'needs_revalidation', 'stale')
OPAQUE_KEYS = frozenset({'run_id', 'receipt_ids', 'action_id', 'input_id', 'call_id', 'candidate_id', 'runs'})
RECORD_KEYS = frozenset({'ref', 'kind', 'scope', 'origin', 'content', 'created', 'updated', 'expires', 'slot', 'status', 'provenance'})
SCOPE = re.compile(r'global|chat:[0-9a-f]{32}|p:[0-9a-f]{12}:[0-9a-f]{40}')
REF = re.compile(r'o:[0-9a-f]{24}')
HEX64 = re.compile(r'[0-9a-f]{64}')
SALT_ID = re.compile(r'[0-9a-f]{12}')


class ArchiveError(PolicyError):
    def __init__(self, reason, detail=''):
        self.reason = reason
        super().__init__(f'The memory archive was refused ({reason}{": " + detail if detail else ""}). Nothing was imported.')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def section_digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def encode(archive):
    return canonical(archive).encode()


def _unique(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate key')
    return dict(pairs)


def _finite(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('non-finite number')
    return value


def _reject(_):
    raise ValueError('non-finite number')


def decode(data):
    """Bytes or text of an archive, bounded before parsing. Dicts are re-encoded."""
    if isinstance(data, dict):
        try:
            data = encode(data)
        except (TypeError, ValueError):
            raise ArchiveError('malformed') from None
    if isinstance(data, str):
        data = data.encode('utf-8')
    if not isinstance(data, (bytes, bytearray)):
        raise ArchiveError('malformed')
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ArchiveError('oversized', f'more than {MAX_ARCHIVE_BYTES} bytes')
    try:
        return json.loads(bytes(data).decode('utf-8'), object_pairs_hook=_unique, parse_float=_finite, parse_constant=_reject)
    except RecursionError:
        raise ArchiveError('malformed', 'nesting') from None
    except (ValueError, UnicodeDecodeError):
        raise ArchiveError('malformed', 'not canonical JSON') from None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _require(condition, reason='malformed', detail=''):
    if not condition:
        raise ArchiveError(reason, detail)


def _hex_salts(scopes):
    salts = scopes.get('salts') if isinstance(scopes, dict) and set(scopes) == {'salts'} else None
    _require(isinstance(salts, dict) and 1 <= len(salts) <= MAX_SALTS, detail='scopes')
    for ident, salt in salts.items():
        _require(SALT_ID.fullmatch(ident) and isinstance(salt, str) and HEX64.fullmatch(salt)
                 and memory_graph.salt_id(salt) == ident, detail='scopes')
    return salts


def validate(archive):
    """Structure, checksums and bounds; returns the sections. Pure."""
    _require(isinstance(archive, dict) and set(archive) == {'format', 'manifest', 'sections'})
    _require(archive['format'] == FORMAT, 'unsupported_format')
    manifest, sections = archive['manifest'], archive['sections']
    _require(isinstance(manifest, dict) and set(manifest) == {'created', 'bloom', 'sections'} and _number(manifest['created']),
             detail='manifest')
    _require(manifest['bloom'] == {'bits': memory_graph.BLOOM_BITS, 'hashes': memory_graph.BLOOM_HASHES}, 'bloom_parameters')
    _require(isinstance(sections, dict) and set(sections) == set(SECTIONS), detail='sections')
    listed = manifest['sections']
    _require(isinstance(listed, dict) and set(listed) == set(SECTIONS), detail='manifest sections')
    for name in SECTIONS:
        entry = listed[name]
        _require(isinstance(entry, dict) and set(entry) == {'sha256', 'count'}, detail='manifest sections')
        _require(entry['sha256'] == section_digest(sections[name]), 'checksum_mismatch', name)
        count = len(sections[name]) if isinstance(sections[name], list) else 1
        _require(entry['count'] == count, 'checksum_mismatch', name + ' count')
    policy = sections['policy']
    _require(isinstance(policy, dict) and set(policy) == {'memory_enabled', 'layers'} and type(policy['memory_enabled']) is bool
             and isinstance(policy['layers'], dict) and set(policy['layers']) == set(KINDS)
             and all(type(v) is bool for v in policy['layers'].values()), detail='policy')
    salts = _hex_salts(sections['scopes'])
    records, relations, tombstones, blooms = (sections[k] for k in ('records', 'relations', 'tombstones', 'blooms'))
    for name, value, limit in (('records', records, MAX_RECORDS), ('relations', relations, MAX_RELATIONS),
                               ('tombstones', tombstones, MAX_TOMBSTONES), ('blooms', blooms, MAX_BLOOMS)):
        _require(isinstance(value, list), detail=name)
        _require(len(value) <= limit, 'oversized', f'{name} over {limit}')

    def scope(value):
        _require(isinstance(value, str) and SCOPE.fullmatch(value), detail='scope')
        _require(not value.startswith('p:') or value[2:14] in salts, detail='scope salt')
        return value

    refs = set()
    for record in records:
        _require(isinstance(record, dict) and set(record) == RECORD_KEYS, detail='record')
        _require(isinstance(record['ref'], str) and REF.fullmatch(record['ref']) and record['ref'] not in refs, detail='record ref')
        refs.add(record['ref'])
        scope(record['scope'])
        _require(record['kind'] in LAYERS, detail='record kind')
        _require(isinstance(record['origin'], str) and 0 < len(record['origin']) <= 40, detail='record origin')
        _require(isinstance(record['content'], str) and 0 < len(record['content']) <= MAX_CONTENT, 'oversized', 'record content')
        _require(_number(record['created']) and _number(record['updated'])
                 and (record['expires'] is None or _number(record['expires'])), detail='record time')
        _require(record['slot'] is None or isinstance(record['slot'], str) and 0 < len(record['slot']) <= 200, detail='record slot')
        _require(record['status'] in EXPORT_STATUSES, detail='record status')
        provenance = record['provenance']
        _require(provenance is None or isinstance(provenance, dict) and len(canonical(provenance)) <= MAX_PROVENANCE,
                 detail='record provenance')
        _require((provenance is None) == (record['slot'] is None), detail='record slot')
    for relation in relations:
        _require(isinstance(relation, dict) and set(relation) == {'from', 'to', 'relation', 'evidence'}
                 and relation['from'] in refs and relation['to'] in refs and relation['from'] != relation['to']
                 and relation['relation'] in memory_graph.RELATIONS and isinstance(relation['evidence'], dict)
                 and len(canonical(relation['evidence'])) <= MAX_EVIDENCE, detail='relation')
    for tombstone in tombstones:
        _require(isinstance(tombstone, dict) and set(tombstone) == {'scope', 'kind', 'slot_item', 'fingerprint_item'}
                 and tombstone['kind'] in KINDS, detail='tombstone')
        scope(tombstone['scope'])
        items = [tombstone['slot_item'], tombstone['fingerprint_item']]
        _require(any(items) and all(i is None or isinstance(i, str) and HEX64.fullmatch(i) for i in items), detail='tombstone')
    for bloom in blooms:
        _require(isinstance(bloom, dict) and set(bloom) == {'scope', 'bits', 'items'} and type(bloom['items']) is int
                 and bloom['items'] >= 0 and isinstance(bloom['bits'], str), detail='bloom')
        scope(bloom['scope'])
        try:
            bits = base64.b64decode(bloom['bits'], validate=True)
        except ValueError:
            bits = b''
        _require(len(bits) == memory_graph.BLOOM_BYTES, 'bloom_parameters')
    return sections


# Export ----------------------------------------------------------------------

def _opaque(salt, kind, value):
    return 'o:' + hmac.new(bytes.fromhex(salt), f'{kind}\x1f{value}'.encode(), 'sha256').hexdigest()[:24]


def _blank_ids(value, salt):
    """Run/receipt/input/action IDs inside host JSON become opaque tokens."""
    if isinstance(value, dict):
        return {k: (_tokens(v, salt, k) if k in OPAQUE_KEYS else _blank_ids(v, salt)) for k, v in value.items()}
    if isinstance(value, list):
        return [_blank_ids(v, salt) for v in value]
    return value


def _tokens(value, salt, kind):
    if isinstance(value, str):
        return _opaque(salt, kind, value)
    if isinstance(value, list):
        return [_opaque(salt, kind, v) if isinstance(v, str) else v for v in value]
    return value


def _export_content(content, salt):
    try:
        value = json.loads(content)
    except ValueError:
        return content
    if not isinstance(value, dict):
        return content
    blanked = _blank_ids(value, salt)
    return content if blanked == value else json.dumps(blanked, ensure_ascii=False, separators=(',', ':'))


def _export_provenance(proof, salt):
    proof = _blank_ids(proof, salt)
    if proof.get('root_identity'):
        # Machine-local (device, inode); the importer can never match it.
        proof['root_identity'] = _opaque(salt, 'root', canonical(proof['root_identity']))
    return proof


def export(learning, now=None):
    """Archive dict plus refusals ({'id', 'reason'}); never writes."""
    learning.outside_transaction()
    now = time.time() if now is None else now
    store = learning.store
    with store.lock:
        db = store.db
        settings = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('memory_enabled','memory_layers')"))
        enabled = bool(json.loads(settings.get('memory_enabled', 'true')))
        switches = json.loads(settings.get('memory_layers', '{}'))
        layers = {k: bool(switches.get(k, True)) for k in KINDS}
        salts = learning.salts(db)
        local = salts[0]
        rows = db.execute('SELECT m.id,m.kind,m.content,m.scope,m.origin,m.created,m.updated,m.expires,e.status,e.slot,e.proof '
                          "FROM memories m LEFT JOIN memory_evidence e ON e.memory_id=m.id WHERE m.kind<>'working' "
                          "AND m.origin<>'curated' ORDER BY m.created,m.id LIMIT ?", (MAX_RECORDS * 2,)).fetchall()
        edges = db.execute('SELECT from_id,to_id,relation,evidence FROM memory_relations ORDER BY created,from_id,to_id LIMIT ?',
                           (MAX_RELATIONS * 2,)).fetchall()
        stones = db.execute('SELECT scope,kind,slot,fingerprint FROM memory_tombstones ORDER BY created,scope,kind,slot LIMIT ?',
                            (MAX_TOMBSTONES + 1,)).fetchall()
        blooms = db.execute('SELECT scope,bits,items FROM memory_tombstone_blooms ORDER BY scope LIMIT ?', (MAX_BLOOMS + 1,)).fetchall()
    if len(stones) > MAX_TOMBSTONES or len(blooms) > MAX_BLOOMS:
        raise ArchiveError('oversized', 'tombstones')
    records, refused, refs = [], [], {}
    for mid, kind, content, scope, origin, created, updated, expires, status, slot, proof in rows:
        if status not in EXPORT_STATUSES or not enabled or not layers.get(kind, True) or expires is not None and expires <= now:
            continue
        # An unlinked record's fingerprint covers its whole text, so its text
        # travels verbatim and a later tombstone from this installation matches.
        entry = {'ref': _opaque(local, 'memory', mid), 'kind': kind, 'scope': memory_graph.scope_key(scope, local),
                 'origin': origin[:40], 'content': _export_content(content, local) if proof else content,
                 'created': created, 'updated': updated,
                 'expires': expires, 'slot': slot if proof else None, 'status': status,
                 'provenance': _export_provenance(json.loads(proof), local) if proof else None}
        if memory_facts.credential_shaped(canonical(entry)) or len(entry['content']) > MAX_CONTENT:
            refused.append({'id': mid, 'reason': 'credential_shaped_text' if len(entry['content']) <= MAX_CONTENT else 'oversized'})
            continue
        records.append(entry)
        refs[mid] = entry['ref']
        if len(records) > MAX_RECORDS:
            raise ArchiveError('oversized', f'more than {MAX_RECORDS} records')
    relations = []
    for source, target, relation, evidence in edges:
        if source in refs and target in refs:
            evidence = memory_graph._load(evidence)
            if memory_facts.credential_shaped(canonical(evidence)):
                evidence = {'basis': evidence.get('basis'), 'redacted': True}
            relations.append({'from': refs[source], 'to': refs[target], 'relation': relation, 'evidence': evidence})
    relations = relations[:MAX_RELATIONS]
    tombstones = [{'scope': memory_graph.scope_key(scope, local), 'kind': kind,
                   'slot_item': memory_graph.item('slot', kind, slot) if slot else None,
                   'fingerprint_item': memory_graph.item('fingerprint', kind, fp) if fp else None}
                  for scope, kind, slot, fp in stones if slot or fp]
    filters = [{'scope': scope, 'bits': base64.b64encode(bits).decode(), 'items': items}
               for scope, bits, items in blooms if len(bits) == memory_graph.BLOOM_BYTES]
    used = {s[2:14] for s in [r['scope'] for r in records] + [t['scope'] for t in tombstones] + [b['scope'] for b in filters]
            if s.startswith('p:')} | {memory_graph.salt_id(local)}
    known = {memory_graph.salt_id(s): s for s in salts}
    if not used <= set(known):
        raise ArchiveError('malformed', 'a tombstone names an unknown salt')
    sections = {'policy': {'memory_enabled': enabled, 'layers': layers}, 'scopes': {'salts': {i: known[i] for i in sorted(used)}},
                'records': records, 'relations': relations, 'tombstones': tombstones, 'blooms': filters}
    archive = {'format': FORMAT, 'sections': sections,
               'manifest': {'created': now, 'bloom': {'bits': memory_graph.BLOOM_BITS, 'hashes': memory_graph.BLOOM_HASHES},
                            'sections': {name: {'sha256': section_digest(value), 'count': len(value) if isinstance(value, list) else 1}
                                         for name, value in sections.items()}}}
    size = len(encode(archive))
    if size > MAX_ARCHIVE_BYTES:
        raise ArchiveError('oversized', f'{size} bytes')
    return {'archive': archive, 'bytes': size, 'refused': refused, 'authorization': 'none',
            'counts': {name: len(value) for name, value in sections.items() if isinstance(value, list)}}


# Import ----------------------------------------------------------------------

def local_state(learning, db):
    """Everything preview() needs from this installation, read under the store lock."""
    settings = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('memory_enabled','memory_layers')"))
    switches = json.loads(settings.get('memory_layers', '{}'))
    salts = learning.salts(db)
    projects = set()
    for sql in ('SELECT DISTINCT workspace FROM chats WHERE workspace IS NOT NULL',
                "SELECT DISTINCT json_extract(config,'$.workspace') FROM runs WHERE json_extract(config,'$.workspace') IS NOT NULL",
                'SELECT DISTINCT scope FROM memories', 'SELECT DISTINCT scope FROM memory_candidates'):
        projects |= {row[0] for row in db.execute(sql + ' LIMIT ?', (MAX_PROJECTS,)) if isinstance(row[0], str) and not memory_graph.literal(row[0])}
    stones = {}
    for scope, kind, slot, fp in db.execute('SELECT scope,kind,slot,fingerprint FROM memory_tombstones'):
        stored = memory_graph.scope_key(scope, salts[0])  # legacy literal rows count under the local key
        stones.setdefault(stored, set()).update(memory_graph.items(kind, [slot], fp))
    fingerprints, records = set(), []
    for mid, kind, scope, content, slot, fp in db.execute('SELECT m.id,m.kind,m.scope,m.content,e.slot,e.fingerprint FROM memories m '
                                                          "LEFT JOIN memory_evidence e ON e.memory_id=m.id WHERE m.kind<>'working'"):
        fp = fp or fingerprint(kind, scope, content)
        fingerprints.add((scope, kind, fp))
        records.append((mid, kind, scope, slot or mid, fp))
    return {
        'now': time.time(),
        'memory_enabled': bool(json.loads(settings.get('memory_enabled', 'true'))),
        'layers': {k: bool(switches.get(k, True)) for k in KINDS},
        'salts': {memory_graph.salt_id(s): s for s in salts}, 'local_salt': salts[0],
        'projects': sorted(projects)[:MAX_PROJECTS],
        'chats': {row[0] for row in db.execute('SELECT id FROM chats')},
        'watermarks': dict(db.execute("SELECT key,through FROM memory_watermarks WHERE key='all' OR key LIKE 'chat:%'")),
        'tombstones': stones,
        'blooms': dict(db.execute('SELECT scope,bits FROM memory_tombstone_blooms')),
        'fingerprints': fingerprints, 'records': records,
        'ids': {row[0] for row in db.execute("SELECT id FROM memories WHERE id LIKE 'imported-%'")},
        'imported': db.execute("SELECT count(*) FROM memories WHERE origin='imported'").fetchone()[0],
    }


def _quote(kind, content, provenance):
    """The text a record's fingerprint covers, as its candidate held it."""
    if provenance is None:
        return content
    if kind == 'procedural':
        return ''
    body = memory_graph._load(content)
    if kind in ('semantic', 'temporal') and isinstance(body.get('quote'), str):
        return body['quote']
    if kind == 'episodic' and body.get('claim_class') == 'host_lesson':
        return lesson_signature(body.get('type'), body.get('signature'))
    if kind == 'episodic' and isinstance(body.get('lesson'), str):
        return body['lesson']
    return content


def preview(archive, state):
    """Pure import plan: decisions per record, tombstones to apply and a plan
    digest that apply() must reproduce. Reads nothing but its arguments."""
    archive = decode(archive)
    sections = validate(archive)
    digest = hashlib.sha256(encode(archive)).hexdigest()
    salts, policy, now = sections['scopes']['salts'], sections['policy'], state['now']
    # Archive scope -> local scope, by salted path hash only.
    mapping = {'global': 'global'}
    for salt in salts.values():
        for path in state['projects']:
            mapping[memory_graph.scope_key(path, salt)] = path

    def local(scope):
        if scope.startswith('chat:'):
            return scope if scope[5:] in state['chats'] else None
        return mapping.get(scope)

    def stored(scope):
        """Where a tombstone lands: the local key, else the archive's own key."""
        target = local(scope)
        if target is not None:
            return memory_graph.scope_key(target, state['local_salt'])
        return scope if scope.startswith('p:') else None

    marks, filters, foreign = {}, {}, set()
    for tombstone in sections['tombstones']:
        key = stored(tombstone['scope'])
        if key is None:
            continue  # a conversation that does not exist here can never learn again
        marks.setdefault(key, set()).update(i for i in (tombstone['slot_item'], tombstone['fingerprint_item']) if i)
        if local(tombstone['scope']) is None:
            foreign.add(tombstone['scope'][2:14])
    for bloom in sections['blooms']:
        key = stored(bloom['scope'])
        if key is None:
            continue
        filters.setdefault(key, []).append(base64.b64decode(bloom['bits']))
        if local(bloom['scope']) is None:
            foreign.add(bloom['scope'][2:14])
    foreign -= set(state['salts'])
    if len(state['salts']) - 1 + len(foreign) > memory_graph.MAX_FOREIGN_SALTS:
        raise ArchiveError('too_many_foreign_salts')
    # Applying a tombstone is forgetting: matching local records go as well.
    erase = []
    for mid, kind, scope, slot, fp in state['records']:
        key = memory_graph.scope_key(scope, state['local_salt'])
        wanted = memory_graph.items(kind, tombstone_slots(kind, slot), fp) if key in marks or key in filters else ()
        if set(wanted) & marks.get(key, set()) or any(memory_graph.bloom_has(b, w) for b in filters.get(key, []) for w in wanted):
            erase.append(mid)

    def forgotten(target, kind, slot, fp):
        wanted = memory_graph.items(kind, tombstone_slots(kind, slot) if slot else (), fp)
        keys = memory_graph.scope_keys(target, [state['local_salt'], *state['salts'].values()])
        for key in [memory_graph.scope_key(target, state['local_salt'])] + keys:
            if set(wanted) & (state['tombstones'].get(key, set()) | marks.get(key, set())):
                return True
            bits = [state['blooms'][key]] if key in state['blooms'] else []
            if any(memory_graph.bloom_has(b, w) for b in bits + filters.get(key, []) for w in wanted):
                return True
        return False

    decisions, imports, seen = [], [], set()
    for record in sections['records']:
        kind, target, provenance = record['kind'], local(record['scope']), record['provenance']
        quote = _quote(kind, record['content'], provenance)
        fp = fingerprint(kind, target, quote) if target else None
        slot = record['slot']
        ident = 'imported-' + hashlib.sha256(canonical([kind, target, fp]).encode()).hexdigest()[:40] if target else None
        watermark = max([state['watermarks'].get('all') or 0] + ([state['watermarks'].get(target) or 0] if target and target.startswith('chat:') else []))
        if not state['memory_enabled'] or not state['layers'][kind]:
            reason = 'layer_disabled'
        elif not policy['memory_enabled'] or not policy['layers'][kind]:
            reason = 'source_layer_disabled'
        elif target is None:
            reason = 'chat_not_present' if record['scope'].startswith('chat:') else 'project_not_present'
        elif record['expires'] is not None and record['expires'] <= now:
            reason = 'expired'
        elif record['created'] <= watermark:
            reason = 'watermark'
        elif memory_facts.credential_shaped(canonical([record['content'], provenance])):
            reason = 'credential_shaped_text'
        elif forgotten(target, kind, slot or ident, fp):
            reason = 'forgotten'
        elif (target, kind, fp) in state['fingerprints'] or ident in state['ids'] or (target, kind, fp) in seen:
            reason = 'already_present'
        else:
            reason = None
        if reason is None:
            seen.add((target, kind, fp))
            file_derived = provenance is not None and (provenance.get('root_identity') or provenance.get('source_file'))
            status = None if provenance is None else 'needs_revalidation' if file_derived or kind == 'procedural' else 'active'
            imports.append({'ref': record['ref'], 'id': ident, 'kind': kind, 'scope': target, 'fingerprint': fp,
                            'slot': slot, 'status': status})
        decisions.append({'ref': record['ref'], 'kind': kind, 'decision': 'skip' if reason else 'import', 'reason': reason})
    if state['imported'] + len(imports) > MAX_IMPORTED:
        raise ArchiveError('import_capacity', f'at most {MAX_IMPORTED} imported records')
    imported = {entry['ref']: entry['id'] for entry in imports}
    relations = [{'from': imported[r['from']], 'to': imported[r['to']], 'relation': r['relation']}
                 for r in sections['relations'] if r['relation'] in ('contradicts', 'derived_from')
                 and r['from'] in imported and r['to'] in imported]
    plan = {'archive_sha256': digest, 'decisions': decisions,
            'tombstones': {k: sorted(v) for k, v in sorted(marks.items())},
            'blooms': {k: [hashlib.sha256(b).hexdigest() for b in v] for k, v in sorted(filters.items())},
            'foreign_salts': sorted(foreign), 'relations': relations, 'erase': sorted(erase),
            'imports': [[e['ref'], e['id'], e['status']] for e in imports]}
    skipped = {}
    for decision in decisions:
        if decision['reason']:
            skipped[decision['reason']] = skipped.get(decision['reason'], 0) + 1
    return {'format': FORMAT, 'archive_sha256': digest, 'plan_digest': section_digest(plan), 'authorization': 'none',
            'policy': policy, 'records': {'import': len(imports), 'skip': skipped, 'total': len(decisions)},
            'needs_revalidation': sum(1 for e in imports if e['status'] == 'needs_revalidation'),
            'tombstones': {'rows': sum(len(v) for v in marks.values()), 'filters': sum(len(v) for v in filters.values()),
                           'scopes': len(set(marks) | set(filters)), 'foreign_salts': len(foreign)},
            'forgets_local': sorted(erase), 'relations': len(relations), 'decisions': decisions[:MAX_DECISIONS],
            '_plan': {'imports': imports, 'marks': marks, 'filters': filters, 'foreign': foreign, 'relations': relations,
                      'erase': erase, 'records': {r['ref']: r for r in sections['records']}, 'salts': salts}}


def _import_provenance(provenance, digest, now):
    proof = dict(provenance)
    if proof.get('root_identity'):
        # Truthy and never equal to a local (device, inode): recall treats it
        # as file-derived and an audit can only find it stale.
        proof['root_identity'] = ['imported', proof['root_identity']]
    proof.update(imported={'archive': digest[:16], 'at': now}, authorization='none')
    return proof


def apply(learning, archive, plan_digest):
    """Import in one transaction. The plan is rebuilt against the locked local
    state and must match the previewed digest; tombstones apply first."""
    learning.outside_transaction()
    archive = decode(archive)
    if not isinstance(plan_digest, str) or not HEX64.fullmatch(plan_digest):
        raise ArchiveError('stale_preview')
    store = learning.store
    with store.lock:
        db = store.db
        if db.in_transaction:
            raise PolicyError('Import memory outside an application transaction.')
        with db:
            state = local_state(learning, db)
            plan = preview(archive, state)
            if plan['plan_digest'] != plan_digest:
                raise ArchiveError('stale_preview', 'local memory changed after the preview')
            inner, now = plan['_plan'], time.time()
            for ident in sorted(inner['foreign']):
                # Later checks key local project paths with this salt too, so
                # forgetting from the archive applies once the project appears.
                db.execute("INSERT OR IGNORE INTO memory_scope_salts VALUES(?,?,'imported',?)", (ident, inner['salts'][ident], now))
            for key, marks in inner['marks'].items():
                memory_graph.bloom_merge(db, key, sorted(marks), count=len(marks), now=now)
            for key, filters in inner['filters'].items():
                for bits in filters:
                    memory_graph.bloom_merge(db, key, bits=bits, now=now)
            for mid in inner['erase']:
                db.execute('DELETE FROM memories WHERE id=?', (mid,))
                db.execute('DELETE FROM memory_evidence WHERE memory_id=?', (mid,))
            memory_graph.detach(db, inner['erase'], now)
            created = []
            for entry in inner['imports']:
                record = inner['records'][entry['ref']]
                chat = entry['scope'][5:] if entry['scope'].startswith('chat:') else None
                db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)', (entry['id'], entry['kind'], record['content'],
                           entry['scope'], chat, 'imported', record['created'], now, record['expires']))
                if record['provenance'] is not None:
                    proof = _import_provenance(record['provenance'], plan['archive_sha256'], now)
                    db.execute('INSERT INTO memory_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                               (entry['id'], 'imported:' + plan['archive_sha256'][:16], 'imported', chat or '', entry['scope'],
                                entry['kind'], entry['slot'], entry['fingerprint'], entry['status'], canonical(proof), None, now))
                    created.append(entry['id'])
            for relation in inner['relations']:
                memory_graph.add_edge(db, relation['from'], relation['to'], relation['relation'], {'basis': 'imported'}, now)
            # Contests travel with the archive; claims, duplicates and new
            # contradictions are recomputed against local records.
            for mid in created:
                memory_graph.relate(db, mid, now)
            memory_graph.settle(db, created, now)
    return {'imported': plan['records']['import'], 'skipped': plan['records']['skip'], 'tombstones': plan['tombstones'],
            'forgotten': len(inner['erase']), 'needs_revalidation': plan['needs_revalidation'],
            'archive_sha256': plan['archive_sha256'], 'authorization': 'none'}


def public(plan):
    """The native preview without the internal plan."""
    return {k: v for k, v in plan.items() if k != '_plan'}
