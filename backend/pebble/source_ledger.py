"""Research source ledger: operational evidence, not a seventh memory layer.

One run's search snippets and fetched pages and PDFs, with hashes, dates and their
text (content-addressed zlib, garbage-collected by reference). Children read into the
root run's ledger under their own agent_id. Source text is untrusted data with no
authority: the model sees a bounded window of it (ReadURL) and explicit pages
(ReadSource), never the whole stored text. Nothing here writes a memory; everything
is deleted with its conversation (forget_chat).

Public API (design study §3.1, plus storage for §3.4 claims and conflicts):

    SourceLedger(store, *, fetch=None, clock=time.time)   # fetch(url, *, max_bytes) is for tests
    .record_search(run_id, query, results, *, plan_id=None, agent_id=None) -> list[dict]
    .read(run_id, url, *, agent_id=None, window_chars, query_terms=()) -> dict   # the ReadURL result
    .page(run_id, source_id, offset, limit, expected_sha256) -> dict           # ReadSource
    .get(run_id, source_id) -> dict; .text(run_id, source_id) -> str           # text() is host-only
    .sources(run_id, *, tree=True) -> list[dict]; .forget_chat(chat_id) -> int
    .set_primary_class(run_id, source_id, label) -> dict
    .record_plan(run_id, questions, queries, budgets) -> str; .plan(run_id, plan_id) -> dict
    .record_claims(run_id, claims) -> dict; .claims(run_id) -> list[dict]
    .conflicts(run_id, *, state=None) -> list[dict]; .resolve_conflict(run_id, conflict_id, resolution) -> dict

Source IDs are run-wide ordinals 'S<n>' (root and children share one sequence), so an
answer's [S<n>] citation names exactly one ledger entry.
"""

import hashlib
import ipaddress
import json
import math
import re
import sqlite3
import time
import uuid
import zlib
from bisect import bisect_left, bisect_right
from urllib.parse import urlsplit

from . import sources
from .memory_facts import instruction_shaped

# Epoch 7 (Phase 16). Dates are epoch seconds; flags and redirects are JSON lists.
RESEARCH_DDL = '''
CREATE TABLE IF NOT EXISTS research_sources(
  run_id TEXT NOT NULL, n INTEGER NOT NULL CHECK(n >= 1), chat_id TEXT NOT NULL, agent_id TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('snippet','page','pdf')), url TEXT NOT NULL, final_url TEXT NOT NULL,
  title TEXT NOT NULL, query_id TEXT, query TEXT, plan_id TEXT, retrieved_at REAL NOT NULL,
  http_date REAL, last_modified REAL, claimed_published REAL, claimed_modified REAL, date_basis TEXT NOT NULL,
  content_type TEXT, encoding TEXT, redirects TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64), body_sha256 TEXT,
  characters INTEGER NOT NULL CHECK(characters >= 0), bytes INTEGER NOT NULL CHECK(bytes >= 0),
  truncated INTEGER NOT NULL CHECK(truncated IN (0,1)), hidden_removed INTEGER NOT NULL CHECK(hidden_removed >= 0),
  domain TEXT NOT NULL,
  primary_class TEXT NOT NULL CHECK(primary_class IN ('primary','original_work','secondary','unknown')),
  flags TEXT NOT NULL,
  PRIMARY KEY(run_id,n));
CREATE INDEX IF NOT EXISTS research_sources_url ON research_sources(run_id,kind,url);
CREATE INDEX IF NOT EXISTS research_sources_chat ON research_sources(chat_id);
CREATE INDEX IF NOT EXISTS research_sources_text ON research_sources(content_sha256);
CREATE TABLE IF NOT EXISTS research_texts(
  sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64), characters INTEGER NOT NULL CHECK(characters >= 0),
  data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS research_plans(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, chat_id TEXT NOT NULL, questions TEXT NOT NULL,
  queries TEXT NOT NULL, budgets TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS research_plans_run ON research_plans(run_id);
CREATE TABLE IF NOT EXISTS research_claims(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, chat_id TEXT NOT NULL, sentence_sha256 TEXT NOT NULL,
  token TEXT NOT NULL, claim_key TEXT, value TEXT, source_n INTEGER, span_start INTEGER, span_end INTEGER,
  span_sha256 TEXT, match TEXT CHECK(match IS NULL OR match IN ('exact','normalized')),
  support TEXT NOT NULL CHECK(support IN ('supported','snippet_only','uncited','unsupported','paraphrase_unverified')),
  created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS research_claims_key ON research_claims(run_id,claim_key);
CREATE TABLE IF NOT EXISTS research_conflicts(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, chat_id TEXT NOT NULL, claim_key TEXT NOT NULL,
  claim_ids TEXT NOT NULL, claim_values TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('open','resolved')),
  resolution TEXT, created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(run_id,claim_key));
'''
TABLES = ('research_sources', 'research_texts', 'research_plans', 'research_claims', 'research_conflicts')
CLASSES = ('primary', 'original_work', 'secondary', 'unknown')
SUPPORT = ('supported', 'snippet_only', 'uncited', 'unsupported', 'paraphrase_unverified')
RESOLUTIONS = ('both_cited', 'newer_explained')
MAX_SOURCE_CHARS = sources.MAX_SOURCE_CHARS  # 200,000
MAX_FETCH_BYTES = sources.MAX_FETCH_BYTES  # 2 MB
MAX_RUN_SOURCES = 32  # page and PDF sources per run
MAX_RUN_BYTES = 4_000_000
MAX_RUN_SNIPPETS = 120
MAX_CLAIMS = 200  # per call
CHUNK = 1000  # ranking granularity, as for attachment excerpts
WINDOW_MIN, WINDOW_MAX, PAGE_MAX = 200, 6500, 12000
DISPLAY_URL = 300  # URLs shown to the model; longer ones end in '…' and are flagged
META_ALLOWANCE = 1500  # ReadURL metadata beyond this narrows the shown window
FUTURE_SLACK = 86400  # a claimed date later than retrieval plus a day is ignored
SOURCE_ID = re.compile(r'\[?\s*[Ss]([1-9]\d{0,5})\s*\]?')
STOP = frozenset(('about after also been before being between does from have into more most only other over '
                  'same some such than that their them then there these they this those through under very '
                  'what when where which while will with would your').split())
KEY_STOP = frozenset('a an and at by for in of on the to'.split())
SEPARATORS = re.compile(r"[\s,'_   ]+")
# Registrable-domain approximation without a public-suffix list: <name>.<second level>.<cc>.
SECOND_LEVEL = frozenset('ac co com edu gc go gob gouv gov govt lg ltd mil ne net nhs nic or org plc police res sch'
                         .split())
GOVERNMENT = frozenset('gc go gob gouv gov govt mil'.split())
# A short reviewed list of standards and registry hosts; a heuristic label, never authority.
STANDARDS = frozenset(('ietf.org rfc-editor.org w3.org whatwg.org iso.org iec.ch ecma-international.org '
                       'unicode.org iana.org icann.org standards.ieee.org europa.eu un.org oecd.org bis.org '
                       'imf.org worldbank.org').split())
ORIGINAL = frozenset({'doi.org', 'arxiv.org'})
COLUMNS = ('run_id', 'n', 'chat_id', 'agent_id', 'kind', 'url', 'final_url', 'title', 'query_id', 'query',
           'plan_id', 'retrieved_at', 'http_date', 'last_modified', 'claimed_published', 'claimed_modified',
           'date_basis', 'content_type', 'encoding', 'redirects', 'content_sha256', 'body_sha256', 'characters',
           'bytes', 'truncated', 'hidden_removed', 'domain', 'primary_class', 'flags')
DATES = ('http_date', 'last_modified', 'claimed_published', 'claimed_modified')


def window_chars(budget):
    """ReadURL's shown window for a context budget: 1,500 at <=4K, 3,000 at <=8K, 6,500 above."""
    return 1500 if budget <= 4096 else 3000 if budget <= 8192 else 6500


def registrable(host):
    """Registrable domain by research.registrable_domain's reviewed suffix rules, so the ledger and the
    planner count one domain per owner (T7 F2); the host itself when no rule applies."""
    from .research import registrable_domain  # research imports only delegation_policy: no cycle
    host = (host or '').rstrip('.').lower()
    return registrable_domain(host) or host

def host_class(url):
    """Provisional primary-source label from the host alone. Hosts the person named or that
    results attribute to the entity are research.classify's job (set_primary_class)."""
    try:
        host = (urlsplit(url).hostname or '').rstrip('.').lower()
    except (TypeError, ValueError):
        host = ''
    if not host or '.' not in host:
        return 'unknown'
    try:
        ipaddress.ip_address(host)
        return 'unknown'
    except ValueError:
        pass
    labels, domain = host.split('.'), registrable(host)
    if labels[-1] in ('gov', 'mil', 'int') or len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in GOVERNMENT:
        return 'primary'
    if domain in ORIGINAL:
        return 'original_work'
    if host in STANDARDS or domain in STANDARDS:
        return 'primary'
    return 'secondary'


def query_words(terms):
    """Distinct lower-case ranking words: longer than three characters, or containing a digit."""
    if isinstance(terms, str):
        terms = [terms]
    words = []
    for term in list(terms or ())[:64]:
        if not isinstance(term, str):
            continue
        for word in re.findall(r'\w+', term.lower()):
            if ((len(word) > 3 or len(word) >= 2 and any(c.isdigit() for c in word))
                    and word not in STOP and word not in words):
                words.append(word)
    return words[:16]


def _lower(text):
    lower = text.lower()
    if len(lower) == len(text):
        return lower
    # A few characters lengthen when lowered; keep offsets aligned with the original.
    return ''.join(c.lower() if len(c.lower()) == 1 else c for c in text)


def _fit(text, start, end, size):
    """Shrink end until the JSON-escaped text fits size + size // 8 characters (one character minimum)."""
    limit, original = size + size // 8, end
    while end > start:
        excess = len(json.dumps(text[start:end], ensure_ascii=False)) - 2 - limit
        if excess <= 0:
            return end
        end -= excess
    return min(original, start + 1)


def window_bounds(text, size, terms=()):
    """[start, end) of one contiguous window of about `size` characters: the run of 1,000-character
    chunks with the most distinct query words (then most hits), the earliest on ties, so without
    words it is the prefix. Edges snap to nearby line or word breaks."""
    total = len(text)
    if total <= size:
        return 0, _fit(text, 0, total, size) if total else 0
    start, hi, words = 0, None, query_words(terms)
    if words:
        lower = _lower(text)
        hits = {}
        for word in words:
            found, at = [], lower.find(word)
            while at != -1 and len(found) < 4000:
                found.append(at)
                at = lower.find(word, at + 1)
            if found:
                hits[word] = found
        best = None
        for begin in range(0, total - size + CHUNK // 2, CHUNK // 2) if hits else ():
            begin = min(begin, total - size)
            counts = [bisect_right(found, begin + size - len(word)) - bisect_left(found, begin)
                      for word, found in hits.items()]
            score = (sum(c > 0 for c in counts), sum(min(c, 5) for c in counts))
            if best is None or score > best[0]:
                best = (score, begin)
        if best:
            # Centre the window on the hits it was ranked by; edge snapping below never drops one.
            begin = best[1]
            inside = [(at, at + len(word)) for word, found in hits.items()
                      for at in found[bisect_left(found, begin):bisect_right(found, begin + size - len(word))]]
            lo, hi = min(a for a, _ in inside), max(b for _, b in inside)
            start = max(0, min(lo - (size - (hi - lo)) // 2, total - size))
    if start:
        cut = text.rfind('\n', max(0, start - 200), start)
        if cut != -1 and (hi is None or cut + 1 + size >= hi):
            start = cut + 1
    end = min(total, start + size)
    if end < total:
        cut = max(text.rfind('\n', end - 200, end), text.rfind(' ', end - 200, end))
        if cut > start and (hi is None or cut >= hi):
            end = cut
    return start, _fit(text, start, end, size)


def claim_key(subject, unit=''):
    """Conflict key: the normalized subject phrase near a precise token, plus its unit."""
    words = [w for w in re.findall(r'\w+', str(subject or '').casefold()) if w not in KEY_STOP][:8]
    units = re.findall(r'\w+|%', str(unit or '').casefold())[:3]
    return ' '.join(words) + ('|' + ' '.join(units) if units else '')


def freshness(source, *, now, days=30):
    """'fresh' when the source's newest content-date claim is within `days` of now, 'stale' when
    older, 'unknown' without one. Content dates are the page's own claims or its Last-Modified
    header, never verified: the field used is returned. HTTP Date and retrieved_at are not
    content dates."""
    dates = source.get('dates') or {}
    for field in ('claimed_modified', 'claimed_published', 'last_modified'):
        stamp = sources.parse_date(dates.get(field))
        if stamp is not None:
            state = 'fresh' if now - stamp <= days * 86400 else 'stale'
            return {'state': state, 'date': sources.iso_date(stamp), 'field': field,
                    'basis': 'http_last_modified' if field == 'last_modified' else dates.get('basis')}
    return {'state': 'unknown', 'date': None, 'field': None, 'basis': 'none'}


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _line(value, limit):
    return ' '.join(value.split())[:limit] if isinstance(value, str) else ''


def _display(url, limit=DISPLAY_URL):
    return url if len(url) <= limit else url[:limit] + '…'


def _n(source_id):
    match = SOURCE_ID.fullmatch(source_id.strip()) if isinstance(source_id, str) else None
    if not match:
        raise ValueError("Use a source_id such as S1 from this run's sources.")
    return int(match.group(1))


def _agent(agent_id):
    if agent_id is not None and (not isinstance(agent_id, str) or not 0 < len(agent_id) <= 200):
        raise ValueError('agent_id must be a host agent identifier.')
    return agent_id


def _stamp(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _flags(values):
    return [f[:40] for f in values if isinstance(f, str)][:32] if isinstance(values, list) else []


def _normalized(text):
    return SEPARATORS.sub('', text.casefold())


class SourceLedger:
    def __init__(self, store, *, fetch=None, clock=time.time):
        self.store, self._fetch, self.clock = store, fetch, clock
        with store.lock:
            store.db.executescript('BEGIN IMMEDIATE;' + RESEARCH_DDL + 'COMMIT;')

    # -- reading -----------------------------------------------------------------------------

    def fetch(self, url, max_bytes):
        if self._fetch is not None:
            return self._fetch(url, max_bytes=max_bytes)
        return sources.fetch_document(url, max_bytes=max_bytes)

    def record_search(self, run_id, query, results, *, plan_id=None, agent_id=None):
        """Snippet sources for one search's results, deduplicated by URL and text within the run. Returns the
        results to show: title, url, snippet, published when claimed, and source_id (None once the
        run's 120-snippet limit is reached)."""
        chat_id, agent_id = self._chat(run_id), _agent(agent_id)
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise ValueError('Search queries must be 1–300 characters.')
        if plan_id is not None and (not isinstance(plan_id, str) or not 0 < len(plan_id) <= 64):
            raise ValueError('plan_id must name a stored research plan.')
        if not isinstance(results, list):
            raise ValueError('Search results must be a list.')
        query_id = 'q-' + _sha(' '.join(query.lower().split()))[:16]
        now, shown = float(self.clock()), []
        with self.store.lock, self.store.db as db:
            count = db.execute("SELECT count(*) FROM research_sources WHERE run_id=? AND kind='snippet'",
                               (run_id,)).fetchone()[0]
            for item in results[:10]:
                url = item.get('url') if isinstance(item, dict) else None
                try:
                    sources.check_url(url)
                except ValueError:
                    continue
                title, snippet = _line(item.get('title'), 180), _line(item.get('snippet'), 650)
                published, flags = sources.parse_date(item.get('published')), []
                if published is not None and published > now + FUTURE_SLACK:
                    published = None
                    flags.append('future_date_ignored')
                text = '\n'.join(part for part in (title, snippet) if part)
                if instruction_shaped(text):
                    flags.append('instruction_shaped')
                entry = {'title': title, 'url': url, 'snippet': snippet}
                if published is not None:
                    entry['published'] = sources.iso_date(published)
                # The same URL with the same shown text is one source; a changed snippet is a new one,
                # so [S<n>] always names exactly the text that was shown.
                existing = db.execute("SELECT n FROM research_sources WHERE run_id=? AND kind='snippet' AND url=? "
                                      "AND content_sha256=? ORDER BY n LIMIT 1", (run_id, url, _sha(text))).fetchone()
                if existing:
                    n = existing[0]
                elif count >= MAX_RUN_SNIPPETS:
                    n = None
                else:
                    n = self._insert(db, {
                        'run_id': run_id, 'chat_id': chat_id, 'agent_id': agent_id, 'kind': 'snippet', 'url': url,
                        'final_url': url, 'title': title, 'query_id': query_id, 'query': query[:300],
                        'plan_id': plan_id, 'retrieved_at': now, 'http_date': None, 'last_modified': None,
                        'claimed_published': published, 'claimed_modified': None,
                        'date_basis': 'rss_pubdate' if published is not None else 'none', 'content_type': None,
                        'encoding': None, 'redirects': '[]', 'content_sha256': self._put_text(db, text),
                        'body_sha256': None, 'characters': len(text), 'bytes': 0, 'truncated': 0,
                        'hidden_removed': 0, 'domain': registrable(urlsplit(url).hostname),
                        'primary_class': host_class(url), 'flags': json.dumps(flags)})
                    count += 1
                shown.append({'source_id': f'S{n}' if n else None, **entry})
        return shown

    def read(self, run_id, url, *, agent_id=None, window_chars, query_terms=()):
        """The ReadURL result: the pinned url/retrieved_at/untrusted_source keys plus ledger metadata
        and one ranked window. A URL this run already read (as requested or final URL) is served
        from the ledger without fetching again. Never carries next_offset."""
        chat_id, agent_id = self._chat(run_id), _agent(agent_id)
        if type(window_chars) is not int:
            raise ValueError('window_chars must be a number of characters.')
        size = max(WINDOW_MIN, min(WINDOW_MAX, window_chars))
        sources.check_url(url)
        row, reused = self._find(run_id, url), True
        if row is None:
            count, used = self._usage(run_id)
            if count >= MAX_RUN_SOURCES:
                raise ValueError(f'This run already read {MAX_RUN_SOURCES} sources, its limit. Answer from the '
                                 'sources read so far and say what remains unread.')
            if used >= MAX_RUN_BYTES:
                raise ValueError('This run already fetched 4 MB of sources, its limit. Answer from the sources '
                                 'read so far and say what remains unread.')
            document = self.fetch(url, min(MAX_FETCH_BYTES, MAX_RUN_BYTES - used))
            row, reused = self._record(run_id, chat_id, agent_id, url, document)
        text, flags = self._text(row), json.loads(row['flags'])
        urls = [url, row['final_url'], *json.loads(row['redirects'])]
        result = {
            'url': _display(url),
            'retrieved_at': row['retrieved_at'],
            'untrusted_source': '',
            'source_id': f"S{row['n']}",
            'kind': row['kind'],
            'title': row['title'][:160],
            'final_url': _display(row['final_url']),
            'redirects': [_display(u) for u in urls[2:]],
            'content_type': row['content_type'],
            'content_sha256': row['content_sha256'],
            'characters': row['characters'],
            'shown': [0, 0],
            'omitted_characters': row['characters'],
            'truncated_at_fetch': bool(row['truncated']),
            'dates': self._dates(row),
            'hidden_text_removed': row['hidden_removed'],
            'instruction_shaped': 'instruction_shaped' in flags,
            'authorization': 'none',
        }
        shown_flags = [f for f in flags if f != 'instruction_shaped']
        if any(len(u) > DISPLAY_URL for u in urls):
            shown_flags.append('url_display_shortened')  # the ledger keeps every URL whole
        if shown_flags:
            result['flags'] = shown_flags
        if reused:
            result['reused'] = True
        # Unusually large metadata narrows the window, so the whole result still fits the encoder.
        overhead = len(json.dumps(result, ensure_ascii=False))
        room = size - max(0, overhead - META_ALLOWANCE)
        start, end = window_bounds(text, room, query_terms) if room > 0 else (0, 0)
        result.update(untrusted_source=text[start:end], shown=[start, end],
                      omitted_characters=row['characters'] - (end - start))
        return result

    def page(self, run_id, source_id, offset, limit, expected_sha256):
        """ReadSource: characters [offset, offset+limit) of a page or PDF source, bound to its hash."""
        row = self._row(run_id, source_id)
        if row['kind'] == 'snippet':
            raise ValueError(f"S{row['n']} is a search snippet, not a page. Read its URL with ReadURL first.")
        expected = expected_sha256.strip().lower() if isinstance(expected_sha256, str) else ''
        if not re.fullmatch(r'[0-9a-f]{16,64}', expected) or not row['content_sha256'].startswith(expected):
            raise ValueError('source_changed: expected_sha256 does not match this source. Use the content_sha256 '
                             'from its ReadURL result.')
        if type(offset) is not int or not 0 <= offset <= row['characters']:
            raise ValueError(f"offset must be between 0 and {row['characters']}.")
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be a positive number of characters.')
        text = self._text(row)
        end = _fit(text, offset, min(len(text), offset + min(limit, PAGE_MAX)), min(limit, PAGE_MAX))
        end = max(end, offset)
        return {
            'source_id': f"S{row['n']}",
            'url': _display(row['url']),
            'kind': row['kind'],
            'content_sha256': row['content_sha256'],
            'characters': row['characters'],
            'retrieved_at': row['retrieved_at'],
            'shown': [offset, end],
            'untrusted_source': text[offset:end],
            'next_offset': end if end < len(text) else None,
            'omitted_characters': len(text) - (end - offset),
            'truncated_at_fetch': bool(row['truncated']),
            'authorization': 'none',
        }

    def get(self, run_id, source_id):
        return self._describe(self._row(run_id, source_id))

    def text(self, run_id, source_id):
        """The whole stored text. Host-only: never offered to the model or a generated view."""
        return self._text(self._row(run_id, source_id))

    def sources(self, run_id, *, tree=True):
        """Every source of the run in [S<n>] order; tree=False keeps only the root's own."""
        sql = 'SELECT * FROM research_sources WHERE run_id=?' + ('' if tree else ' AND agent_id IS NULL')
        return [self._describe(row) for row in self._rows(sql + ' ORDER BY n', (run_id,))]

    def set_primary_class(self, run_id, source_id, label):
        """research.classify's label (it knows the entity's hosts); a heuristic, never authority."""
        if label not in CLASSES:
            raise ValueError('Unknown primary-source label.')
        row = self._row(run_id, source_id)
        self.store.query('UPDATE research_sources SET primary_class=? WHERE run_id=? AND n=?',
                         (label, run_id, row['n']))
        return self.get(run_id, source_id)

    def forget_chat(self, chat_id):
        """Delete every ledger row of a conversation and unreferenced texts; returns sources deleted."""
        with self.store.lock, self.store.db as db:
            deleted = db.execute('DELETE FROM research_sources WHERE chat_id=?', (chat_id,)).rowcount
            for table in ('research_plans', 'research_claims', 'research_conflicts'):
                db.execute(f'DELETE FROM {table} WHERE chat_id=?', (chat_id,))
            db.execute('DELETE FROM research_texts WHERE NOT EXISTS(SELECT 1 FROM research_sources s '
                       'WHERE s.content_sha256=research_texts.sha256)')
        return deleted

    # -- plans, claims and conflicts (§3.2 storage, §3.4) ---------------------------------------

    def record_plan(self, run_id, questions, queries, budgets):
        """Store a research plan so an evaluation can reproduce it; returns its plan_id."""
        chat_id = self._chat(run_id)
        if (not isinstance(questions, list) or not 0 < len(questions) <= 4
                or not all(isinstance(q, str) and 0 < len(q) <= 500 for q in questions)):
            raise ValueError('A plan has one to four questions of at most 500 characters.')
        if (not isinstance(queries, list) or len(queries) > 10
                or not all(isinstance(q, str) and 0 < len(q) <= 300 for q in queries)):
            raise ValueError('A plan has at most ten queries of at most 300 characters.')
        if not isinstance(budgets, dict) or len(json.dumps(budgets)) > 2000:
            raise ValueError('Plan budgets must be a small object.')
        plan_id = 'plan-' + uuid.uuid4().hex[:16]
        self.store.query('INSERT INTO research_plans VALUES(?,?,?,?,?,?,?)',
                         (plan_id, run_id, chat_id, json.dumps(questions), json.dumps(queries),
                          json.dumps(budgets, sort_keys=True), float(self.clock())))
        return plan_id

    def plan(self, run_id, plan_id):
        rows = self.store.query('SELECT * FROM research_plans WHERE id=? AND run_id=?', (plan_id, run_id))
        if not rows:
            raise ValueError('Research plan not found in this run.')
        row = rows[0]
        return {'plan_id': row['id'], 'questions': json.loads(row['questions']), 'queries': json.loads(row['queries']),
                'budgets': json.loads(row['budgets']), 'created': row['created']}

    def record_claims(self, run_id, claims):
        """Store claim records; the host re-reads every cited span from its own text and hashes it.

        A claim is {sentence | sentence_sha256, token, support, source_id?, span?: [start, end],
        match?: 'exact' | 'normalized', key?, value?}. 'supported' needs a page or PDF source and a
        span: 'exact' is verified here (case, spaces and digit separators ignored); 'normalized' is
        the planner's unit/date normalization, recorded as such. A snippet never supports a claim.
        Different supported values under one key open a conflict. Returns claim_ids and the
        conflicts the call touched."""
        chat_id = self._chat(run_id)
        if not isinstance(claims, list) or not 0 < len(claims) <= MAX_CLAIMS:
            raise ValueError(f'Record one to {MAX_CLAIMS} claims at a time.')
        now, ids, keys = float(self.clock()), [], set()
        with self.store.lock, self.store.db as db:
            for claim in claims:
                values = self._claim(db, run_id, claim)
                ident = 'claim-' + uuid.uuid4().hex[:16]
                db.execute('INSERT INTO research_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (ident, run_id, chat_id, values['sentence_sha256'], values['token'], values['key'],
                            values['value'], values['n'], values['start'], values['end'], values['span_sha256'],
                            values['match'], values['support'], now))
                ids.append(ident)
                if values['support'] == 'supported' and values['key'] and values['value']:
                    keys.add(values['key'])
            touched = [self._conflict(db, run_id, chat_id, key, now) for key in sorted(keys)]
        return {'claim_ids': ids, 'conflicts': [
            self._conflict_view(run_id, self._rows('SELECT * FROM research_conflicts WHERE id=?', (ident,))[0])
            for ident in touched if ident]}

    def claims(self, run_id):
        return [{'claim_id': r['id'], 'sentence_sha256': r['sentence_sha256'], 'token': r['token'],
                 'key': r['claim_key'], 'value': r['value'],
                 'source_id': f"S{r['source_n']}" if r['source_n'] else None,
                 'span': [r['span_start'], r['span_end']] if r['span_start'] is not None else None,
                 'span_sha256': r['span_sha256'], 'match': r['match'], 'support': r['support']}
                for r in self._rows('SELECT * FROM research_claims WHERE run_id=? ORDER BY created, rowid', (run_id,))]

    def conflicts(self, run_id, *, state=None):
        """Conflicts with each side's value, source and dates, so an answer can cite both or say which is newer."""
        if state not in (None, 'open', 'resolved'):
            raise ValueError('state is open, resolved or None.')
        sql = 'SELECT * FROM research_conflicts WHERE run_id=?' + (' AND state=?' if state else '')
        rows = self._rows(sql + ' ORDER BY created, id', (run_id, state) if state else (run_id,))
        return [self._conflict_view(run_id, row) for row in rows]

    def resolve_conflict(self, run_id, conflict_id, resolution):
        """Close a conflict once the answer cites both sides with dates, or explains which is newer."""
        if resolution not in RESOLUTIONS:
            raise ValueError('resolution is both_cited or newer_explained.')
        with self.store.lock, self.store.db as db:
            changed = db.execute("UPDATE research_conflicts SET state='resolved', resolution=?, updated=? "
                                 "WHERE id=? AND run_id=?",
                                 (resolution, float(self.clock()), conflict_id, run_id)).rowcount
        if not changed:
            raise ValueError('Conflict not found in this run.')
        return self._conflict_view(run_id, self._rows('SELECT * FROM research_conflicts WHERE id=?',
                                                      (conflict_id,))[0])

    # -- internals ---------------------------------------------------------------------------

    def _chat(self, run_id):
        row = None
        if isinstance(run_id, str) and run_id:
            with self.store.lock:
                try:
                    row = self.store.db.execute('SELECT chat_id FROM runs WHERE id=?', (run_id,)).fetchone()
                except sqlite3.OperationalError:
                    row = None
        if not row:
            raise ValueError('Run not found.')
        return row[0]

    def _rows(self, sql, args):
        with self.store.lock:
            cursor = self.store.db.execute(sql, args)
            names = [d[0] for d in cursor.description]
            return [dict(zip(names, r)) for r in cursor.fetchall()]

    def _row(self, run_id, source_id):
        rows = self._rows('SELECT * FROM research_sources WHERE run_id=? AND n=?', (run_id, _n(source_id)))
        if not rows:
            raise ValueError('Source not found in this run.')
        return rows[0]

    def _find(self, run_id, url):
        rows = self._rows("SELECT * FROM research_sources WHERE run_id=? AND kind!='snippet' AND (url=? OR final_url=?) "
                          "ORDER BY n LIMIT 1", (run_id, url, url))
        return rows[0] if rows else None

    def _usage(self, run_id):
        rows = self._rows("SELECT count(*) AS sources, coalesce(sum(bytes),0) AS bytes FROM research_sources "
                          "WHERE run_id=? AND kind!='snippet'", (run_id,))
        return rows[0]['sources'], rows[0]['bytes']

    @staticmethod
    def _put_text(db, text):
        data = text.encode('utf-8')
        digest = hashlib.sha256(data).hexdigest()
        db.execute('INSERT OR IGNORE INTO research_texts VALUES(?,?,?)', (digest, len(text), zlib.compress(data, 6)))
        return digest

    def _text(self, row):
        found = self._rows('SELECT data FROM research_texts WHERE sha256=?', (row['content_sha256'],))
        data = zlib.decompress(found[0]['data']) if found else None
        if data is None or hashlib.sha256(data).hexdigest() != row['content_sha256']:
            raise ValueError('This source text failed its integrity check.')
        return data.decode('utf-8')

    @staticmethod
    def _insert(db, values):
        values['n'] = db.execute('SELECT coalesce(max(n),0)+1 FROM research_sources WHERE run_id=?',
                                 (values['run_id'],)).fetchone()[0]
        db.execute(f"INSERT INTO research_sources({','.join(COLUMNS)}) VALUES({','.join('?' * len(COLUMNS))})",
                   tuple(values[c] for c in COLUMNS))
        return values['n']

    def _record(self, run_id, chat_id, agent_id, url, document):
        """(row, reused): a fetched document stored atomically, run caps re-checked inside the write."""
        if (not isinstance(document, dict) or document.get('kind') not in ('page', 'pdf')
                or not isinstance(document.get('text'), str)):
            raise ValueError('The source reader returned an unexpected result.')
        retrieved_at = float(self.clock())
        text, flags = document['text'], _flags(document.get('flags'))
        truncated = bool(document.get('truncated'))
        if len(text) > MAX_SOURCE_CHARS:
            text, truncated = text[:MAX_SOURCE_CHARS], True
            flags.append('source_chars_cap')
        if instruction_shaped(text):
            flags.append('instruction_shaped')
        dates = {key: _stamp(document.get(key)) for key in DATES}
        for key, value in dates.items():
            if value is not None and value > retrieved_at + FUTURE_SLACK:
                dates[key] = None
                flags.append('future_date_ignored')
        basis = document.get('date_basis') if isinstance(document.get('date_basis'), str) else 'none'
        if dates['claimed_published'] is None and dates['claimed_modified'] is None:
            basis = 'none'
        final_url = document.get('final_url') if isinstance(document.get('final_url'), str) else url
        redirects = [u[:3000] for u in document.get('redirects') or [] if isinstance(u, str)][:sources.MAX_REDIRECTS]
        size = document.get('bytes') if type(document.get('bytes')) is int and document['bytes'] >= 0 else len(text)
        hidden = document.get('hidden_text_removed')
        with self.store.lock, self.store.db as db:
            existing = db.execute("SELECT n FROM research_sources WHERE run_id=? AND kind!='snippet' AND "
                                  "(url=? OR final_url=?) ORDER BY n LIMIT 1", (run_id, url, url)).fetchone()
            if existing:  # a concurrent read of the same URL won; serve its record
                n, reused = existing[0], True
            else:
                count, used = db.execute("SELECT count(*), coalesce(sum(bytes),0) FROM research_sources "
                                         "WHERE run_id=? AND kind!='snippet'", (run_id,)).fetchone()
                if count >= MAX_RUN_SOURCES or used + size > MAX_RUN_BYTES:
                    raise ValueError('This run reached its source reading limit. Answer from the sources read '
                                     'so far and say what remains unread.')
                n, reused = self._insert(db, {
                    'run_id': run_id, 'chat_id': chat_id, 'agent_id': agent_id, 'kind': document['kind'],
                    'url': url, 'final_url': final_url[:3000], 'title': _line(document.get('title'), 180),
                    'query_id': None, 'query': None, 'plan_id': None, 'retrieved_at': retrieved_at, **dates,
                    'date_basis': basis[:40], 'content_type': str(document.get('content_type') or '')[:100] or None,
                    'encoding': str(document.get('encoding') or '')[:40] or None,
                    'redirects': json.dumps(redirects), 'content_sha256': self._put_text(db, text),
                    'body_sha256': document.get('body_sha256') if isinstance(document.get('body_sha256'), str)
                    and re.fullmatch(r'[0-9a-f]{64}', document['body_sha256']) else None,
                    'characters': len(text), 'bytes': size, 'truncated': int(truncated),
                    'hidden_removed': hidden if type(hidden) is int and hidden >= 0 else 0,
                    'domain': registrable(urlsplit(final_url).hostname), 'primary_class': host_class(final_url),
                    'flags': json.dumps(list(dict.fromkeys(flags)))}), False
            row = db.execute('SELECT * FROM research_sources WHERE run_id=? AND n=?', (run_id, n))
            names = [d[0] for d in row.description]
            return dict(zip(names, row.fetchone())), reused

    @staticmethod
    def _dates(row):
        return {'http_date': sources.iso_date(row['http_date']), 'last_modified': sources.iso_date(row['last_modified']),
                'claimed_published': sources.iso_date(row['claimed_published']),
                'claimed_modified': sources.iso_date(row['claimed_modified']), 'basis': row['date_basis']}

    def _describe(self, row):
        flags = json.loads(row['flags'])
        return {
            'source_id': f"S{row['n']}", 'n': row['n'], 'kind': row['kind'], 'url': row['url'],
            'final_url': row['final_url'], 'title': row['title'], 'agent_id': row['agent_id'],
            'query_id': row['query_id'], 'query': row['query'], 'plan_id': row['plan_id'],
            'retrieved_at': row['retrieved_at'], 'dates': self._dates(row), 'content_type': row['content_type'],
            'encoding': row['encoding'], 'redirects': json.loads(row['redirects']),
            'content_sha256': row['content_sha256'], 'body_sha256': row['body_sha256'],
            'characters': row['characters'], 'bytes': row['bytes'], 'truncated': bool(row['truncated']),
            'hidden_text_removed': row['hidden_removed'], 'instruction_shaped': 'instruction_shaped' in flags,
            'domain': row['domain'], 'primary_class': row['primary_class'], 'flags': flags,
            'authorization': 'none',
        }

    def _claim(self, db, run_id, claim):
        if not isinstance(claim, dict) or claim.get('support') not in SUPPORT:
            raise ValueError('Each claim needs a support of ' + ', '.join(SUPPORT) + '.')
        sentence = claim.get('sentence')
        digest = _sha(sentence) if isinstance(sentence, str) and len(sentence) <= 4000 else claim.get('sentence_sha256')
        token = claim.get('token')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Each claim needs its sentence (at most 4,000 characters) or sentence_sha256.')
        if not isinstance(token, str) or not 0 < len(token) <= 200:
            raise ValueError('Each claim needs its precise token (at most 200 characters).')
        key, value = claim.get('key'), claim.get('value')
        key = key.strip()[:200] if isinstance(key, str) and key.strip() else None
        value = ' '.join(value.casefold().split())[:200] if isinstance(value, str) and value.strip() else None
        values = {'sentence_sha256': digest, 'token': token, 'key': key, 'value': value, 'n': None, 'start': None,
                  'end': None, 'span_sha256': None, 'match': None, 'support': claim['support']}
        if claim.get('source_id') is not None:
            found = db.execute('SELECT n, kind, content_sha256, characters FROM research_sources WHERE run_id=? AND n=?',
                               (run_id, _n(claim['source_id']))).fetchone()
            if not found:
                raise ValueError('A claim cites a source that is not in this run.')
            values['n'] = found[0]
            kind, content_sha256, characters = found[1:]
        if claim['support'] != 'supported':
            return values
        if values['n'] is None:
            raise ValueError('A supported claim must cite a source.')
        if kind == 'snippet':
            raise ValueError('A search snippet never supports a precise claim; record it as snippet_only.')
        span, match = claim.get('span'), claim.get('match', 'exact')
        if (not isinstance(span, list) or len(span) != 2 or not all(type(i) is int for i in span)
                or not 0 <= span[0] < span[1] <= characters or span[1] - span[0] > 2000):
            raise ValueError('A supported claim needs a span [start, end] of at most 2,000 characters in its source.')
        if match not in ('exact', 'normalized'):
            raise ValueError('match is exact or normalized.')
        excerpt = self._text({'content_sha256': content_sha256})[span[0]:span[1]]
        if match == 'exact' and _normalized(token) not in _normalized(excerpt):
            raise ValueError('unsupported: the token does not appear in the cited span.')
        values.update(start=span[0], end=span[1], span_sha256=_sha(excerpt), match=match)
        return values

    @staticmethod
    def _conflict(db, run_id, chat_id, key, now):
        rows = db.execute("SELECT id, value FROM research_claims WHERE run_id=? AND claim_key=? AND support='supported' "
                          "AND value IS NOT NULL ORDER BY created, rowid", (run_id, key)).fetchall()
        values = sorted({value for _, value in rows})
        if len(values) < 2:
            return None
        ids = [ident for ident, _ in rows]
        existing = db.execute('SELECT id, claim_values, state FROM research_conflicts WHERE run_id=? AND claim_key=?',
                              (run_id, key)).fetchone()
        if existing:
            reopened = json.loads(existing[1]) != values
            db.execute('UPDATE research_conflicts SET claim_ids=?, claim_values=?, updated=?' +
                       (", state='open', resolution=NULL" if reopened else '') + ' WHERE id=?',
                       (json.dumps(ids), json.dumps(values), now, existing[0]))
            return existing[0]
        ident = 'conflict-' + uuid.uuid4().hex[:16]
        db.execute("INSERT INTO research_conflicts VALUES(?,?,?,?,?,?,'open',NULL,?,?)",
                   (ident, run_id, chat_id, key, json.dumps(ids), json.dumps(values), now, now))
        return ident

    def _conflict_view(self, run_id, row):
        ids = json.loads(row['claim_ids'])
        sides = []
        for claim in self._rows('SELECT id, value, source_n FROM research_claims WHERE run_id=? AND id IN (%s) '
                                'ORDER BY created, rowid' % ','.join('?' * len(ids)), (run_id, *ids)):
            source = self._rows('SELECT * FROM research_sources WHERE run_id=? AND n=?', (run_id, claim['source_n']))
            side = {'claim_id': claim['id'], 'value': claim['value'], 'source_id': f"S{claim['source_n']}"}
            if source:
                side.update(retrieved_at=source[0]['retrieved_at'], dates=self._dates(source[0]))
            sides.append(side)
        return {'conflict_id': row['id'], 'key': row['claim_key'], 'values': json.loads(row['claim_values']),
                'state': row['state'], 'resolution': row['resolution'], 'sides': sides,
                'created': row['created'], 'updated': row['updated']}
