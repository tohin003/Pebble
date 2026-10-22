"""Bounded lexical/metadata recall ranking for the six memory layers.

Deterministic host logic over SQLite FTS5 candidates: weighted term coverage,
adjacent-phrase and entity matches, a small task-intent vocabulary, record
provenance, scope and recency. It is not embedding-based semantic
understanding, starts no model, reads no model settings and never treats a
recalled record as authority. Selection keeps records whole and reports
budget omissions instead of silently implying absence.

The hybrid ranker (v2, `HybridQuery`) keeps every v1 signal and adds number
and unit normalization, a small fixed abbreviation table, word-level trigram
similarity, a substring (trigram) candidate pool, per-layer recency
half-lives, an optional relation filter (`graph`) that withholds contested
records, and an `Embedder` interface whose default embeds nothing. A plain
`Query` still ranks exactly as v1 for comparison. Synonyms without shared
spelling remain out of reach of both.
"""

import json
import math
import re
from bisect import bisect_left
from collections import Counter

TOKEN = re.compile(r'[^\W_]+')
MAX_QUERY_CHARS = 4000
MAX_TERMS = 16
MAX_BIGRAMS = 8
MAX_ENTITIES = 8
MAX_RECORD_TOKENS = 1200
STANDING_LIMIT = 32
LEXICAL_LIMIT = 40
PHRASE_LIMIT = 12
BASELINE_PROFILES = 3
LEXICAL_FLOOR = 0.6
RELATIVE_FLOOR = 0.3
HALF_LIFE_DAYS = 30.0
NOTE_MIN_BUDGET = 400
GENERAL_PROFILE_SLOTS = frozenset({'response-style', 'response-language', 'formatting'})
WEB_EVIDENCE = frozenset({'attributed_web_quote', 'weather_service'})
# Fields that carry meaning in structured records; hashes and labels are noise.
SEARCH_FIELDS = ('path', 'key', 'quote', 'recipe', 'prerequisites', 'source', 'user_request', 'task_outcome',
                 'type', 'target', 'observation', 'current_goal')
STOP = frozenset('''
a an the and or but if then else of to in on at by for with from into onto about as is are was were be been being
am do does did doing done have has had i me my mine we us our you your yours he she it its they them their this
that these those there here what which who whom whose when where why how can could would should will shall may
might must not no yes please thanks thank just also very really some any all each every more most much many such
only own same so than too out up down over under again once now get got let make made use used using tell know
show give find need want like see look help memory memories remember recall saved note notes end final reply
'''.split())

# Hybrid (v2) bounds. The trigram pool scans at most 1 MB of the most recent
# eligible records; the recall budget is about 6% of the model window.
TRIGRAM_LIMIT = 24
TRIGRAM_SCAN_BYTES = 1_000_000
TRIGRAM_SCAN_ROWS = 8000
MAX_KEYS = 24
FUZZY_FLOOR = 0.5
FUZZY_MIN_TERM = 5
FUZZY_CREDIT = 0.6
EXPANSION_CREDIT = 0.9
HALF_LIVES = {'working': 1.0, 'episodic': 14.0, 'semantic': 90.0, 'procedural': 180.0,
              'temporal': 2.0, 'profile': 365.0}
WINDOW_SHARE = 0.06
MIN_RECALL_BUDGET = 600
MAX_RECALL_BUDGET = 4000
MAX_WINDOW = 10_000_000
EMBED_FLOOR = 0.5
EMBED_WEIGHT = 2.0
MAX_EMBED_DIM = 4096
CONFLICT_MARKERS = ('contest', 'contradict', 'conflict')
GRAPH_UNAVAILABLE = 'graph_unavailable'
_LABEL = re.compile(r'[a-z][a-z0-9_]{0,39}')
# Equivalent spellings; a match through this table is credited slightly below
# an exact match. Fixed and small: it is not a thesaurus.
ABBREVIATIONS = (
    ('db', 'database'), ('env', 'environment'), ('cfg', 'config', 'configuration'), ('repo', 'repository'),
    ('auth', 'authentication'), ('dir', 'directory'), ('doc', 'docs', 'documentation'), ('prod', 'production'),
    ('dev', 'development'), ('k8s', 'kubernetes'), ('msg', 'message'), ('pwd', 'password'), ('tz', 'timezone'),
    ('perf', 'performance'), ('deps', 'dependency'), ('pkg', 'package'), ('img', 'image'), ('addr', 'address'),
    ('app', 'application'), ('info', 'information'), ('req', 'request'), ('resp', 'response'),
    ('ver', 'version'), ('num', 'number'), ('py', 'python'), ('js', 'javascript'), ('ts', 'typescript'),
    ('lib', 'library'), ('fn', 'func', 'function'), ('param', 'parameter'), ('arg', 'argument'),
    ('ctx', 'context'), ('err', 'error'), ('svc', 'service'), ('srv', 'server'), ('impl', 'implementation'),
    ('spec', 'specification'), ('demo', 'demonstration'), ('stats', 'statistics'), ('tmp', 'temporary'),
    ('admin', 'administrator'), ('sec', 'second'), ('min', 'minute'), ('hr', 'hour'), ('ms', 'millisecond'),
)
NUMBER_WORDS = {'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6',
                'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10', 'eleven': '11', 'twelve': '12',
                'fifteen': '15', 'twenty': '20', 'thirty': '30', 'forty': '40', 'fifty': '50', 'sixty': '60',
                'hundred': '100', 'thousand': '1000'}
UNITS = {'s': 'second', 'sec': 'second', 'secs': 'second', 'min': 'minute', 'mins': 'minute', 'h': 'hour',
         'hr': 'hour', 'hrs': 'hour', 'd': 'day', 'ms': 'millisecond', 'kb': 'kilobyte', 'mb': 'megabyte',
         'gb': 'gigabyte'}
_GROUPED = re.compile(r'(?<=\d)[,_](?=\d{3}(?!\d))')
_VERSION = re.compile(r'\bv(?=\d)', re.I)
_QUANTITY = re.compile(r'(\d+)([a-z]+)')
_SUFFIXED = re.compile(r'([a-z]{2,})(\d+)')


def stem(token):
    """Light suffix folding for ASCII words; other scripts stay exact."""
    if not token.isascii() or not token.isalpha() or len(token) <= 3:
        return token
    if token.endswith('ies') and len(token) > 4:
        return token[:-3] + 'y'
    if token.endswith('es') and len(token) > 4 and token[:-2].endswith(('s', 'x', 'z', 'ch', 'sh')):
        return token[:-2]
    if token.endswith('ing') and len(token) >= 7:
        return token[:-3]
    if token.endswith('ed') and len(token) >= 6:
        return token[:-2]
    if token.endswith('s') and not token.endswith(('ss', 'us', 'is')):
        return token[:-1]
    return token


def _useful(token):
    return len(token) >= 3 or len(token) >= 2 and (not token.isascii() or token.isdigit())


def _vocabulary(words):
    return frozenset(stem(w) for w in words.split())


def _expansions():
    index = {}
    for group in ABBREVIATIONS:
        roots = [stem(w) for w in group]
        for root in roots:
            index.setdefault(root, set()).update(r for r in roots if r != root)
    return {root: frozenset(others) for root, others in index.items()}


EXPANSIONS = _expansions()


def normalize_numbers(text):
    """'1,200' -> '1200' and 'v20.11' -> '20.11'; other text is unchanged."""
    return _VERSION.sub('', _GROUPED.sub('', text))


def variants(token):
    """Extra comparable forms of one casefolded token (hybrid ranking only):
    number words, split quantities ('45min' -> '45', 'minute', '10k' ->
    '10000') and canonical unit names."""
    found = []
    if token in NUMBER_WORDS:
        found.append(NUMBER_WORDS[token])
    match = _QUANTITY.fullmatch(token)
    if match:
        number, unit = match.groups()
        found.append(number)
        if unit == 'k':
            found.append(number + '000')
        found.append(UNITS.get(unit, unit))
    match = _SUFFIXED.fullmatch(token)
    if match:
        found.extend(match.groups())
    if token in UNITS:
        found.append(UNITS[token])
    return found


def substring_key(term):
    """The leading part of a term the trigram pool looks for anywhere in a
    record ('deletion' -> 'delet', 'configuration' -> 'config')."""
    if term.isdigit():
        return term if len(term) >= 3 else None
    if len(term) < 4:
        return None
    return term[:min(6, max(4, len(term) - 3))]


def trigrams(word):
    return {word[i:i + 3] for i in range(len(word) - 2)}


def similar_words(terms, vocabulary):
    """Vocabulary words that look like morphological or compound variants of
    a term: the same first three letters with trigram Dice >= FUZZY_FLOOR,
    or a longer word containing the whole term ('redeploy', 'healthcheck')."""
    heads, long_words = {}, []
    for word in vocabulary:
        if len(word) >= 4 and word.isalpha():
            heads.setdefault(word[:3], []).append(word)
            if len(word) >= FUZZY_MIN_TERM + 2:
                long_words.append(word)
    result = {}
    for term in terms:
        if len(term) < FUZZY_MIN_TERM or not term.isalpha():
            continue
        grams, found = trigrams(term), set()
        for word in heads.get(term[:3], ()):
            other = trigrams(word)
            if word != term and 2 * len(grams & other) / (len(grams) + len(other)) >= FUZZY_FLOOR:
                found.add(word)
        found.update(w for w in long_words if len(w) >= len(term) + 2 and term in w)
        if found:
            result[term] = frozenset(found)
    return result


INTENTS = {
    'engineering': _vocabulary('code coding bug bugs fix fixes fixing implement implementing refactor refactoring '
                               'function functions class classes module modules compile build debug debugging error '
                               'errors exception traceback failing failure edit edits patch feature script program'),
    'testing': _vocabulary('test tests testing unittest pytest verify verification coverage failing'),
    'current': _vocabulary('weather forecast temperature rain price prices stock stocks news headline headlines '
                           'exchange currency'),
}
_ENTITY = re.compile(
    r'`([^`\n]{1,80})`|"([^"\n]{2,80})"'
    r'|((?:[\w.-]+/)+[\w.-]+|\b[\w-]+\.[A-Za-z][A-Za-z0-9]{0,7}\b|\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b'
    r'|\b[A-Za-z]+_\w+\b|\b[a-z]+[A-Z]\w*\b|\b[A-Z][A-Z0-9_]{2,}\b|\b\d+(?:\.\d+)+\b|\b\d{2,}\b)'
    r'|\b([A-Z][a-z]{2,})\b')
_SENTENCE_START = re.compile(r'(?:^|[.!?:]["\')]*)\s*$')


def entities(text, kinds=False):
    """Quoted spans, paths, identifiers, constants, numbers and mid-sentence
    capitalised names, casefolded and bounded. With kinds, each value is
    paired with True for structured entities and False for plain names."""
    found, strong = [], []
    text = text[:MAX_QUERY_CHARS] if isinstance(text, str) else ''
    for match in _ENTITY.finditer(text):
        if match.group(4) and _SENTENCE_START.search(text[max(0, match.start() - 4):match.start()]):
            continue  # A capital that only starts a sentence is not a name.
        value = next(g for g in match.groups() if g).strip().casefold()
        if value and value not in STOP and value not in found:
            found.append(value)
            strong.append(not match.group(4))
        if len(found) >= MAX_ENTITIES:
            break
    return list(zip(found, strong)) if kinds else found


class Query:
    """Content-derived search features of one request; no model involved."""

    version = 1

    def __init__(self, text):
        text = text[:MAX_QUERY_CHARS] if isinstance(text, str) else ''
        self.text = text
        text = self._prepare(text)
        sequence, surface, terms, originals = [], [], [], []
        for word in TOKEN.findall(text.casefold())[:600]:
            for token in self._forms(word):
                root = stem(token)
                if token in STOP or root in STOP or not self._keep(token):
                    sequence.append(None)
                    surface.append(None)
                    continue
                sequence.append(root)
                surface.append(token)
                if root not in terms and len(terms) < MAX_TERMS:
                    terms.append(root)
                    originals.append(token)
        self.terms = tuple(terms)
        self.originals = tuple(originals)
        pairs, words = [], []
        for (a, b), (x, y) in zip(zip(sequence, sequence[1:]), zip(surface, surface[1:])):
            if a and b and a != b and (a, b) not in pairs:
                pairs.append((a, b))
                words.append((x, y))
        self.bigrams = tuple(pairs[:MAX_BIGRAMS])
        self.phrases = tuple(words[:MAX_BIGRAMS])
        typed = entities(text, kinds=True)
        self.entities = tuple(value for value, _ in typed)
        # A plain name is usually also a query term; it counts less than a
        # path, identifier, number or quoted span so it is not double-counted.
        self.patterns = tuple((re.compile(r'(?<!\w)' + re.escape(value) + r'(?!\w)'), 1.5 if strong else 0.75)
                              for value, strong in typed)
        self.intents = frozenset(name for name, vocabulary in INTENTS.items() if vocabulary.intersection(terms))

    @staticmethod
    def _prepare(text):
        return text

    @staticmethod
    def _forms(token):
        return (token,)

    @staticmethod
    def _keep(token):
        return _useful(token)

    def fts_any(self):
        """OR query; prefix tokens let 'tests' reach 'test'/'testing'."""
        parts = []
        for root, original in zip(self.terms, self.originals):
            prefix = root if original.startswith(root) else original[:len(root) - 1]
            parts.append('"%s"*' % prefix if len(prefix) >= 4 else '"%s"' % original)
        return ' OR '.join(dict.fromkeys(parts))

    def fts_phrases(self):
        """Adjacent query words and multi-token entities as exact phrases."""
        parts = ['"%s %s"' % pair for pair in self.phrases]
        for entity in self.entities:
            tokens = TOKEN.findall(entity)[:6]
            if len(tokens) > 1:
                parts.append('"%s"' % ' '.join(tokens))
        return ' OR '.join(dict.fromkeys(parts))

    def summary(self):
        return {'terms': len(self.terms), 'phrases': len(self.bigrams), 'entities': len(self.entities),
                'intents': sorted(self.intents)}


class HybridQuery(Query):
    """v2 request features: normalized numbers and quantities, table
    abbreviations kept as terms with their other spellings, and substring
    keys for the trigram pool. Still no model."""

    version = 2

    def __init__(self, text):
        super().__init__(text)
        self.expansions = {t: EXPANSIONS[t] for t in self.terms if t in EXPANSIONS}
        keys = []
        for term in self.terms:
            for form in (term, *sorted(self.expansions.get(term, ()))):
                key = substring_key(form)
                if key and key not in keys and len(keys) < MAX_KEYS:
                    keys.append(key)
        self.keys = tuple(keys)

    _prepare = staticmethod(normalize_numbers)

    @staticmethod
    def _forms(token):
        if token in NUMBER_WORDS:
            return (NUMBER_WORDS[token],)
        match = _QUANTITY.fullmatch(token)
        if match:
            number, unit = match.groups()
            return (number + '000',) if unit == 'k' else (number, UNITS.get(unit, unit))
        return (token,)

    @staticmethod
    def _keep(token):
        return _useful(token) or token.isdigit() or stem(token) in EXPANSIONS

    def fts_any(self):
        """v1 terms plus their table spellings ('db' also reaches 'database')."""
        parts = [part for part in super().fts_any().split(' OR ') if part]
        for term in self.terms:
            for alternative in sorted(self.expansions.get(term, ())):
                parts.append('"%s"*' % alternative if len(alternative) >= 4 else '"%s"' % alternative)
        return ' OR '.join(dict.fromkeys(parts))

    def summary(self):
        return {**super().summary(), 'expanded': len(self.expansions), 'keys': len(self.keys)}


def searchable(record):
    """Meaningful text of a record: structured fields, else the content."""
    content = record.get('content') if isinstance(record.get('content'), str) else ''
    if content.startswith('{'):
        try:
            data = json.loads(content)
        except ValueError:
            data = None
        if isinstance(data, dict):
            parts = [str(data[k]) for k in SEARCH_FIELDS
                     if isinstance(data.get(k), (str, int, float)) and not isinstance(data.get(k), bool)]
            if parts:
                return ' '.join(parts)
    return content


def search_text(record):
    """The trigram pool's view of a record: casefolded, number-normalized
    searchable text. Python scans and the optional FTS5 trigram index both
    match substring keys against exactly this text."""
    return normalize_numbers(searchable(record).casefold())


def key_hits(text, keys):
    return frozenset(key for key in keys if key in text)


def trigram_select(hits, updated, size, exclude=(), limit=TRIGRAM_LIMIT):
    """Order trigram-pool nominations deterministically.

    hits maps a record ID to the substring keys it contains, within a window
    of `size` records; rarer keys weigh more. Records already nominated by
    another pool are skipped so the pool adds candidates instead of
    repeating them."""
    frequency = Counter(key for keys in hits.values() for key in keys)
    weight = {key: 1.0 + math.log((size + 1) / (count + 1)) for key, count in frequency.items()}
    scored = sorted((-math.fsum(weight[k] for k in keys), -(updated.get(ident) or 0.0), ident)
                    for ident, keys in hits.items() if keys and ident not in exclude)
    return [ident for _, _, ident in scored[:limit]]


def recall_budget(window):
    """Characters of recalled memory for a model window of `window` tokens:
    6% of the window, clamped to 600-4,000 (4K -> 600, 32K -> 1,966,
    200K -> 4,000)."""
    if type(window) is not int or not 1 <= window <= MAX_WINDOW:
        raise ValueError('Use a model window from 1 to 10,000,000 tokens.')
    return max(MIN_RECALL_BUDGET, min(MAX_RECALL_BUDGET, round(window * WINDOW_SHARE)))


class Embedder:
    """Optional dense similarity for reranking recall candidates.

    embed(texts) returns one finite float vector of a common length per text,
    or None when unavailable. An implementation runs inside the recall call;
    none is bundled. This default embeds nothing, so recall stays lexical and
    no model is downloaded, started or kept resident."""

    name = 'none'

    def embed(self, texts):
        return None


NO_EMBEDDER = Embedder()


def provenance(record):
    if record.get('origin') in ('user', 'direct-user'):
        return 1.0
    evidence = (record.get('proof') or {}).get('evidence_class')
    return {'human_preference': 0.9, 'attributed_file_quote': 0.75, 'observed_test_recipe': 0.75,
            'observed_project_recipe': 0.75,  # Phase 20 G20-recipes: build, lint and typecheck recipes rank as tests did
            'attributed_web_quote': 0.55, 'weather_service': 0.55}.get(
        evidence, 0.45 if record.get('origin') == 'curated' else 0.4)


def scope_weight(record, chat_id=None, workspace=None):
    scope = record.get('scope')
    if chat_id and scope == 'chat:' + chat_id:
        return 1.0
    if workspace and scope == workspace:
        return 0.8
    return 0.6 if scope == 'global' else 0.5


def recency(record, now, half_life=HALF_LIFE_DAYS):
    updated = record.get('updated')
    if type(updated) not in (int, float) or not math.isfinite(updated):
        return 0.0
    return 0.5 ** (max(0.0, now - updated) / 86400 / half_life)


def record_intents(record):
    kind, content = record.get('kind'), record.get('content') or ''
    if kind == 'procedural':
        if content.startswith('Engineering workflow'):
            return {'engineering', 'testing'}
        if content.startswith('Current-information workflow'):
            return {'current'}
        if (record.get('proof') or {}).get('evidence_class') in ('observed_test_recipe', 'observed_project_recipe') or content.startswith('{"recipe"'):
            return {'engineering', 'testing'}
    if kind == 'profile':
        slot = record.get('slot') or ''
        if slot.startswith('code-') or slot == 'commit-messages':
            return {'engineering'}
    return set()


def general_profile(record):
    return record.get('kind') == 'profile' and (
        record.get('origin') in ('user', 'direct-user') or record.get('slot') in GENERAL_PROFILE_SLOTS)


def _match(ordered, tokens, term):
    if term in tokens:
        return 1.0
    if len(term) >= 4:
        index = bisect_left(ordered, term)
        if index < len(ordered) and ordered[index].startswith(term):
            return 0.85
    if len(term) >= 8:
        index = bisect_left(ordered, term[:6])
        if index < len(ordered) and ordered[index].startswith(term[:6]):
            return 0.6
    return 0.0


def conflicted(reason):
    return any(marker in reason for marker in CONFLICT_MARKERS)


def _label(value):
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else 'withheld'


def _withheld(graph, records, report):
    """IDs the relation filter withholds, with content-free reasons.

    graph(ids) returns {id: reason}. A failing or malformed filter withholds
    every candidate (fail closed) rather than recalling unchecked records."""
    ids = [record.get('id') for record in records]
    if graph is None or not ids:
        report['graph'] = 'none' if graph is None else 'callback'
        return {}
    try:
        verdicts = graph(list(ids))
        if not isinstance(verdicts, dict):
            raise TypeError('The relation filter must return a mapping.')
        withheld = {ident: _label(verdicts[ident]) for ident in ids if ident in verdicts}
    except Exception:
        report['graph'] = 'error'
        return {ident: GRAPH_UNAVAILABLE for ident in ids}
    report['graph'] = 'callback'
    return withheld


def _similarities(embedder, query, records, report):
    """Cosine similarity of each record to the query, or None. Missing,
    failing or malformed embeddings leave ranking lexical."""
    report['embedder'] = 'none'
    if embedder is None or not records:
        return None
    texts = [query.text] + [searchable(record)[:MAX_QUERY_CHARS] for record in records]
    try:
        vectors = embedder.embed(texts)
    except Exception:
        report['embedder'] = 'error'
        return None
    if vectors is None:
        return None
    try:
        rows = [list(vector) for vector in vectors]
    except TypeError:
        rows = None
    dimension = len(rows[0]) if rows else 0
    if (rows is None or len(rows) != len(texts) or not 1 <= dimension <= MAX_EMBED_DIM
            or any(len(row) != dimension for row in rows)
            or not all(type(x) in (int, float) and math.isfinite(x) for row in rows for x in row)):
        report['embedder'] = 'error'
        return None
    name = getattr(embedder, 'name', None)
    report['embedder'] = name if isinstance(name, str) and re.fullmatch(r'[\w.:-]{1,40}', name) else 'custom'
    head = rows[0]
    head_norm = math.sqrt(sum(x * x for x in head))
    result = []
    for row in rows[1:]:
        norm = math.sqrt(sum(x * x for x in row))
        result.append(sum(a * b for a, b in zip(head, row)) / (head_norm * norm) if head_norm and norm else 0.0)
    return result


def rank(query, records, *, now, chat_id=None, workspace=None, baseline_limit=BASELINE_PROFILES,
         graph=None, embedder=None, report=None):
    """Score bounded candidates; pure and deterministic.

    records are dicts with id, kind, origin, content, scope, updated and
    optional proof (dict) and slot. Every entry keeps content-free signals so
    callers can explain a decision without exposing memory text.

    A plain Query ranks exactly as v1. A HybridQuery adds normalized numbers
    and quantities, abbreviation and trigram-similarity credit, per-layer
    recency, the optional relation filter and optional embeddings. Withheld
    entries are never relevant; `match` says whether they otherwise would
    have been, so callers can add a host conflict note. `report`, if given,
    receives the filter and embedder status."""
    report = {} if report is None else report
    hybrid = getattr(query, 'version', 1) >= 2
    docs = []
    for record in records:
        lowered = searchable(record).casefold()
        if hybrid:
            lowered = normalize_numbers(lowered)
            words = TOKEN.findall(lowered)[:MAX_RECORD_TOKENS]
            stems = [stem(t) for t in words]
            tokens = set(stems).union(stem(v) for t in words for v in variants(t))
        else:
            stems = [stem(t) for t in TOKEN.findall(lowered)[:MAX_RECORD_TOKENS]]
            tokens = set(stems)
        docs.append((record, lowered, stems, tokens, sorted(tokens)))
    similar = similar_words(query.terms, set().union(*(d[3] for d in docs))) if hybrid and docs else {}
    expansions = getattr(query, 'expansions', {}) if hybrid else {}

    def credit(ordered, tokens, term):
        found = _match(ordered, tokens, term)
        if found or not hybrid:
            return found, 'lexical'
        if any(_match(ordered, tokens, alternative) for alternative in expansions.get(term, ())):
            return EXPANSION_CREDIT, 'expanded'
        return (FUZZY_CREDIT, 'fuzzy') if similar.get(term, frozenset()) & tokens else (0.0, None)

    matrix = [{t: found for t in query.terms if (found := credit(d[4], d[3], t))[0]} for d in docs]
    count = len(docs)
    frequency = {t: sum(1 for hits in matrix if t in hits) for t in query.terms}
    # Terms absent from every candidate cannot discriminate; leaving them out
    # keeps a natural question from penalising the one record that answers it.
    weights = {t: 1.0 + math.log((count + 1) / (frequency[t] + 1)) for t in query.terms if frequency[t]}
    total = sum(weights.values())
    semantic = _similarities(embedder, query, records, report) if hybrid else None
    withheld = _withheld(graph, records, report) if hybrid else {}
    entries = []
    for index, (record, lowered, stems, tokens, ordered) in enumerate(docs):
        hits = {t: m for t, (m, _) in matrix[index].items()}
        coverage = sum(weights[t] * m for t, m in hits.items()) / total if total else 0.0
        breadth = len(hits) / len(query.terms) if query.terms else 0.0
        pairs = set(zip(stems, stems[1:]))
        phrase = sum(1 for p in query.bigrams if p in pairs) / len(query.bigrams) if query.bigrams else 0.0
        matched = [weight for pattern, weight in query.patterns if pattern.search(lowered)]
        entity = len(matched) / len(query.patterns) if query.patterns else 0.0
        intent = bool(query.intents & record_intents(record))
        points = (3.0 * coverage * (0.5 + 0.5 * breadth) + 1.5 * phrase + min(2.5, sum(matched))
                  + (1.0 if intent else 0.0))
        half_life = HALF_LIVES.get(record.get('kind'), HALF_LIFE_DAYS) if hybrid else HALF_LIFE_DAYS
        prior = (0.6 * provenance(record) + 0.2 * scope_weight(record, chat_id, workspace)
                 + 0.2 * recency(record, now, half_life))
        signals = {'coverage': round(coverage, 3), 'breadth': round(breadth, 3),
                   'phrase': round(phrase, 3), 'entity': round(entity, 3), 'intent': intent,
                   'baseline': False}
        if hybrid:
            sources = [source for _, source in matrix[index].values()]
            signals.update(expanded=sources.count('expanded'), fuzzy=sources.count('fuzzy'))
            if semantic is not None:
                signals['semantic'] = round(semantic[index], 3)
                points += EMBED_WEIGHT * max(0.0, (semantic[index] - EMBED_FLOOR) / (1.0 - EMBED_FLOOR))
        entry = {'record': record, 'points': points, 'prior': prior, 'score': points + prior,
                 'relevant': False, 'reason': 'no_match', 'signals': signals}
        if record.get('id') in withheld:
            entry['withheld'] = signals['withheld'] = withheld[record['id']]
        entries.append(entry)
    top = max((e['score'] for e in entries if e['points'] >= LEXICAL_FLOOR and 'withheld' not in e), default=0.0)
    baseline = []
    for entry in entries:
        if 'withheld' in entry:
            entry['match'] = entry['points'] >= LEXICAL_FLOOR or general_profile(entry['record'])
            entry['reason'] = 'withheld'
        elif entry['points'] >= LEXICAL_FLOOR:
            entry['relevant'] = entry['score'] >= RELATIVE_FLOOR * top
            entry['reason'] = 'match' if entry['relevant'] else 'weaker_than_best'
        elif general_profile(entry['record']):
            baseline.append(entry)
    # General preferences apply to most answers; a few stay without a match.
    baseline.sort(key=lambda e: (-e['prior'], -(e['record'].get('updated') or 0), str(e['record'].get('id'))))
    for index, entry in enumerate(baseline):
        entry['signals']['baseline'] = True
        entry['score'] = 1.0 + entry['prior']
        entry['relevant'] = index < baseline_limit
        entry['reason'] = 'baseline_preference' if entry['relevant'] else 'baseline_limit'
    entries.sort(key=lambda e: (-e['score'], -provenance(e['record']), -(e['record'].get('updated') or 0),
                                str(e['record'].get('id'))))
    return entries


def size(item):
    """Serialized characters an item adds to the recalled JSON list."""
    return len(json.dumps(item)) + 2


def omission_note(count):
    return {'kind': 'recall_note', 'origin': 'host', 'omitted': count,
            'content': f'{count} further relevant memory record(s) were omitted to fit the recall budget; '
                       'their absence here is not evidence.'}


def conflict_note(count):
    """Host replacement for matching records the relation filter contests."""
    return {'kind': 'recall_note', 'origin': 'host', 'note': 'conflict', 'withheld': count,
            'content': f'{count} matching record(s) withheld: other records contradict them, so neither value '
                       'is established.'}


def unchecked_note(count):
    return {'kind': 'recall_note', 'origin': 'host', 'note': 'unchecked', 'withheld': count,
            'content': f'{count} matching record(s) withheld: their conflict check was unavailable.'}


def _with_omissions(note, count):
    """A pinned host note that also reports budget omissions, so a small
    budget pays for one note instead of two."""
    return {**note, 'omitted': count,
            'content': note['content'] + f' {count} further relevant record(s) omitted for budget; '
                                         'absence is not evidence.'}


def pack(candidates, budget, pinned=1):
    """Greedy whole-record selection under a serialized character budget.

    candidates are (identifier, item) in rank order; the first `pinned` that
    fit are never displaced. When relevant records do not fit, a host note
    reports the count, displacing the lowest-ranked records if needed. A
    pinned host note (conflict/unchecked) carries that count itself. Host
    notes that do not fit are not counted as omitted records."""
    chosen, omitted, used = [], [], 0
    for position, (ident, item) in enumerate(candidates):
        cost = size(item)
        if used + cost <= budget:
            chosen.append((ident, item, position < pinned))
            used += cost
        elif item.get('kind') != 'recall_note':
            omitted.append(ident)
    if omitted and budget >= NOTE_MIN_BUDGET:
        host = next((i for i, entry in enumerate(chosen) if entry[1].get('kind') == 'recall_note'), None)
        while True:
            if host is None:
                note, extra = omission_note(len(omitted)), size(omission_note(len(omitted)))
            else:
                note = _with_omissions(chosen[host][1], len(omitted))
                extra = size(note) - size(chosen[host][1])
            if used + extra <= budget:
                if host is None:
                    chosen.append((None, note, True))
                else:
                    chosen[host] = (chosen[host][0], note, True)
                used += extra
                break
            movable = [i for i, entry in enumerate(chosen) if not entry[2]]
            if not movable:
                break
            ident, item, _ = chosen.pop(movable[-1])
            used -= size(item)
            omitted.append(ident)
    return [(ident, item) for ident, item, _ in chosen], omitted, used
