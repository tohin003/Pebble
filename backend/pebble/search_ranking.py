"""Deterministic lexical relevance for project file discovery.

Pure functions over text the caller already authorized: no I/O, model calls or
embeddings. Query terms are identifier subwords, so ``SearchIndex``,
``search_index`` and ``search index`` all mean ``search`` + ``index``. Matching
folds ASCII letters only (exactly ``bytes.lower`` on UTF-8), so other characters
must match exactly. A file's score combines path/name, Python definition, phrase
and saturated term-frequency signals weighted by each term's rarity among the
files actually examined, scaled by the share of query terms the file matched.
Integer points with a relative-path tie-break make every ordering reproducible.
Excerpts are untrusted source text and never instructions.
"""

import math
import re
import string
from collections import namedtuple

from .policy import PolicyError

VERSION = 'lexical-1'
DESCRIPTION = ('lexical: path/name, Python definition, phrase and term-frequency signals weighted by '
               'term rarity; ASCII case-insensitive; not semantic or embedding search')
MAX_QUERY = 256
MAX_TERMS = 16
MAX_SPANS = 2  # evidence lines per ranked file
SYMBOL_SPANS = 2  # of which matching definitions
SPAN_CHARS = 100
K1, B = 1.2, 0.4  # term-frequency saturation and length normalization
WEIGHTS = {'name': 2.5, 'directory': 0.8, 'symbol': 2.0, 'phrase': 1.5, 'path_phrase': 1.0,
           'exact_name': 3.0, 'exact_symbol': 3.0}

_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_CHUNK = re.compile(r'[^\W_]+')
_PART = re.compile(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+')

Plan = namedtuple('Plan', 'text words terms phrase')
Signals = namedtuple('Signals', 'tf phrase name directory exact_name path_phrase symbol exact_symbol '
                                'length searched')


def fingerprint():
    """Everything that changes an ordering or a page; cursors bind it."""
    return repr((VERSION, K1, B, sorted(WEIGHTS.items()), MAX_TERMS, MAX_SPANS, SYMBOL_SPANS, SPAN_CHARS))


def fold(text):
    """ASCII-only lowercase; ``fold(s).encode()`` equals ``s.encode().lower()``."""
    return text.translate(_LOWER)


def subwords(text):
    """Folded identifier parts split at underscores, punctuation and ASCII camel case."""
    words = []
    for chunk in _CHUNK.findall(text):
        if chunk.isascii():
            words.extend(fold(part) for part in _PART.findall(chunk))
        else:  # no case-boundary guesses outside ASCII
            words.append(fold(chunk))
    return words


def plan(query):
    """Validate a search query and derive its terms and literal phrase."""
    if not isinstance(query, str) or not 1 <= len(query) <= MAX_QUERY:
        raise PolicyError(f'Search needs words, an identifier or a phrase of at most {MAX_QUERY} characters.')
    if query.splitlines() != [query]:
        raise PolicyError('Search queries are one line; remove line breaks.')
    try:
        query.encode('utf-8')
    except UnicodeEncodeError:
        raise PolicyError('Search queries must be valid Unicode text.') from None
    text = ' '.join(query.split())
    if not text:
        raise PolicyError('Search needs words, an identifier or a phrase.')
    words = list(dict.fromkeys(subwords(text)))
    if len(words) > 1:
        words = [word for word in words if len(word) > 1] or words
    if len(words) > MAX_TERMS:
        raise PolicyError(f'Use at most {MAX_TERMS} distinct search terms.')
    terms = tuple(word.encode('utf-8') for word in words)
    phrase = fold(text).encode('utf-8')
    return Plan(text, tuple(words), terms, None if terms == (phrase,) else phrase)


def same_name(name, plan):
    """Whether an identifier is the query itself or exactly its subword sequence."""
    if name == plan.text:
        return True
    return (bool(plan.words) and fold(name).replace('_', '') == ''.join(plan.words)
            and subwords(name) == list(plan.words))


def path_signals(path, plan):
    """(per-term name hits, per-term directory-only hits, exact file stem, phrase in path)."""
    slash = path.rfind('/')
    leaf, folder = path[slash + 1:], path[:slash + 1]
    base, parent = fold(leaf).encode('utf-8'), fold(folder).encode('utf-8')
    name = tuple(term in base for term in plan.terms)
    directory = tuple(not hit and term in parent for term, hit in zip(plan.terms, name))
    stem = leaf.rsplit('.', 1)[0] if '.' in leaf.lstrip('.') else leaf
    exact = bool(plan.words) and subwords(stem) == list(plan.words)
    phrase = plan.phrase is not None and plan.phrase in fold(path).encode('utf-8')
    return name, directory, exact, phrase


def content_signals(folded, plan):
    """Per-term and phrase occurrence counts in ``bytes.lower()`` text."""
    return (tuple(folded.count(term) for term in plan.terms),
            folded.count(plan.phrase) if plan.phrase is not None else 0)


def symbol_signals(symbols, plan):
    """(per-term hits in definition names, whether a definition is the query's exact name)."""
    hits, exact = [False] * len(plan.terms), False
    for symbol in symbols:
        name = fold(symbol.name).encode('utf-8')
        for index, term in enumerate(plan.terms):
            if not hits[index] and term in name:
                hits[index] = True
        exact = exact or same_name(symbol.name, plan)
    return tuple(hits), exact


def rarity(df, total):
    """BM25-style inverse document frequency over the examined files."""
    return tuple(math.log(1 + (total - count + 0.5) / (count + 0.5)) for count in df)


def score(signals, idf, average, plan):
    """Integer points (0 means no evidence) and the query words this file matched."""
    total, matched = 0.0, []
    norm = 1 - B + B * signals.length / average if signals.searched and average > 0 else 1.0
    for index, weight in enumerate(idf):
        tf = signals.tf[index] if signals.tf else 0
        symbol = bool(signals.symbol and signals.symbol[index])
        if not (tf or signals.name[index] or signals.directory[index] or symbol):
            continue
        matched.append(plan.words[index])
        if tf:  # saturates, so repeating a word cannot buy unbounded rank
            total += weight * tf * (K1 + 1) / (tf + K1 * norm)
        if signals.name[index]:
            total += weight * WEIGHTS['name']
        elif signals.directory[index]:
            total += weight * WEIGHTS['directory']
        if symbol:
            total += weight * WEIGHTS['symbol']
    scale = sum(idf) if idf else 1.0
    if signals.phrase:
        total += scale * WEIGHTS['phrase'] * signals.phrase * (K1 + 1) / (signals.phrase + K1)
    if signals.path_phrase:
        total += scale * WEIGHTS['path_phrase']
    if signals.exact_name:
        total += scale * WEIGHTS['exact_name']
    if signals.exact_symbol:
        total += scale * WEIGHTS['exact_symbol']
    if plan.terms:
        total *= len(matched) / len(plan.terms)
    points = round(total * 100)
    return (max(points, 1) if total > 0 else 0), matched


def explain(signals):
    """Nonzero signal counts behind a score, without any source text."""
    found = {
        'name': sum(signals.name), 'directory': sum(signals.directory),
        'symbol': sum(signals.symbol) if signals.symbol else 0,
        'content': sum(1 for count in signals.tf if count) if signals.tf else 0,
        'occurrences': sum(signals.tf) if signals.tf else 0, 'phrase': signals.phrase,
        'path_phrase': int(signals.path_phrase), 'exact_name': int(signals.exact_name),
        'exact_symbol': int(signals.exact_symbol),
    }
    return {key: value for key, value in found.items() if value}


def excerpt(line, column, chars=SPAN_CHARS):
    """A bounded window of one line that keeps ``column`` (0-based) visible."""
    start = max(0, min(column - chars // 5, len(line) - chars))
    return {'text': line[start:start + chars], 'text_start': start + 1, 'text_truncated': len(line) > chars}


def spans(text, plan, symbols=(), limit=MAX_SPANS, chars=SPAN_CHARS, label=None):
    """Best evidence lines: matching definitions first, then phrase and term lines.

    Line numbers follow ``str.splitlines`` like ``Read``; ``text`` is untrusted.
    ``label(symbol)`` returns the (bounded display name, shortened) shown for a
    definition; matching itself always uses the full parsed name.
    """
    label = label or (lambda symbol: (symbol.name, False))
    lines = text.splitlines()
    chosen, used = [], set()
    ranked = []
    for position, symbol in enumerate(symbols):
        name = fold(symbol.name).encode('utf-8')
        hits = sum(term in name for term in plan.terms)
        exact = same_name(symbol.name, plan)
        if hits or exact:
            ranked.append(((-(exact * (len(plan.terms) + 1) + hits), symbol.depth, symbol.line,
                            symbol.column, position), symbol))
    ranked.sort(key=lambda item: item[0])
    for _key, symbol in ranked:
        if len(chosen) >= min(SYMBOL_SPANS, limit):
            break
        if symbol.line in used or not 1 <= symbol.line <= len(lines):
            continue
        used.add(symbol.line)
        shown, shortened = label(symbol)
        span = {'kind': 'symbol', 'line': symbol.line, 'end_line': symbol.end_line,
                'symbol': shown, 'symbol_kind': symbol.kind, 'column': symbol.column,
                **excerpt(lines[symbol.line - 1], symbol.column - 1, chars)}
        if shortened:
            span['symbol_truncated'] = True  # a display form, not the canonical qualified name
        if symbol.uncertain:
            span['end_line_uncertain'] = True
        chosen.append(span)
    hits = []
    for number, line in enumerate(lines, 1):
        if number in used:
            continue
        folded = fold(line).encode('utf-8')
        phrase = plan.phrase is not None and plan.phrase in folded
        distinct = sum(term in folded for term in plan.terms)
        if phrase or distinct:
            hits.append((-(phrase * (len(plan.terms) + 1) + distinct), number, folded, phrase))
    hits.sort(key=lambda item: item[:2])
    for _weight, number, folded, phrase in hits[:max(limit - len(chosen), 0)]:
        needles = [plan.phrase] if phrase else [term for term in plan.terms if term in folded]
        offset = min(folded.find(needle) for needle in needles)
        column = len(folded[:offset].decode('utf-8', 'ignore'))
        line = lines[number - 1]
        chosen.append({'kind': 'phrase' if phrase else 'terms', 'line': number, 'end_line': number,
                       'column': column + 1, **excerpt(line, column, chars)})
    return chosen
