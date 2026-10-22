"""Deterministic learning candidates from records the host already owns.

Inputs are a Read receipt excerpt, human-origin text, or settled broker/tool
receipts. A fact candidate is an exact excerpt substring with its local offset;
the learning proof re-checks the current file before promotion. No model output
is parsed, summarized or trusted, and nothing here authorizes an action. Source
text stays a source report: instruction-shaped lines are labelled, never turned
into procedures or preferences.
"""

import re
from bisect import bisect_right
from pathlib import PurePosixPath

from .interaction import SECRET

MAX_FACTS_PER_FILE = 6
MAX_FACTS_PER_RUN = 16
MAX_FACT_CHARS = 300
MAX_SCAN_LINES = 400
MAX_SCAN_LINE_CHARS = 2000
MAX_LESSONS = 4
MAX_REFUSAL_LESSONS = 2
LEADING_CHARS = 240

LINE_BREAKS = '\n\r\x0b\x0c\x1c\x1d\x1e\x85' + chr(0x2028) + chr(0x2029)  # str.splitlines() boundaries

# A quote is whole when it starts a line (after list/comment markers) or a
# sentence, and ends a line or a sentence. Cut fragments such as "use SQLite"
# from "Do not use SQLite." cannot promote.
_LEFT = re.compile(r'[ \t]*(?:(?:#+|//+|/\*+|\*+|--|;+|>+|"""|\'\'\')[ \t]*|(?:[-+]|\d{1,3}[.)])[ \t]+)?')
_RIGHT = re.compile(r'[ \t]*(?:\*/|"""|\'\'\')?[ \t]*')
_SENTENCE_BEFORE = re.compile(r'[.!?][ \t]+$')
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=["\'(\[`]?[A-Z0-9])')
_CLOSER = re.compile(r'\s*(?:\*/|"""|\'\'\')\s*$')

_SECTION = re.compile(r'\s*\[\[?\s*([^\[\]\n]{1,64}?)\s*\]\]?\s*(?:[#;].*)?')
_ASSIGN = re.compile(r'\s*(?:export\s+)?([A-Za-z0-9_][\w.\-]{0,63}|"[^"\n]{1,64}")\s*[=:]\s*(\S.*?)\s*')
_YAML = re.compile(r'([ \t]*)(?:-[ \t]+)?([A-Za-z_][\w.\-]{0,63}|"[^"\n]{1,64}"):(?:[ \t]+(.*?))?[ \t]*')
_JSON = re.compile(r'\s*"([^"\\\n]{1,64})"\s*:\s*("(?:[^"\\\n]|\\.){0,240}"|-?\d[\d.eE+\-]{0,30}|true|false|null)\s*,?\s*')
_JSON_OPEN = re.compile(r'\s*"([^"\\\n]{1,64})"\s*:\s*[\[{]\s*')
_CONSTANT = re.compile(r'\s*(?:export\s+)?(?:(?:const|let|var|final|static|public|private|readonly)\s+)*'
                       r'([A-Z][A-Z0-9_]{1,63})\s*(?::\s*[\w\[\]., |]{1,40})?=\s*(.+?)\s*;?\s*')
_LITERAL = re.compile(r'''(?:-?\d[\d_.]*(?:[eE][+-]?\d+)?|"[^"\n]{0,200}"|'[^'\n]{0,200}'|True|False|None|true|false|null'''
                      r'''|\[[^\[\]\n]{0,160}\]|\([^()\n]{0,160}\))\s*(?:#.*|//.*)?''')
_LABEL = re.compile(r'\s*(?:[-+*]\s+)?([A-Z][\w ()/.-]{0,40}?)\s*:\s+(\S.{0,240})')
_NOTE_LABELS = frozenset({'note', 'notes', 'warning', 'tip', 'example', 'important', 'todo', 'fixme', 'caution',
                          'info', 'summary', 'description', 'usage', 'see', 'step', 'question', 'answer'})
_SECRET_KEY = re.compile(r'pass(?:word|wd|phrase)?|secret|token|api[_-]?key|private[_-]?key|credential|auth|cookie'
                         r'|session|salt|signature', re.I)
_OPAQUE = re.compile(r'[A-Za-z0-9+/=-]{32,}')
_ACTION_NOUNS = {'edit': 'edit', 'command': 'command', 'tests': 'test run', 'app': 'app launch', 'plugin': 'plugin call'}

_PREDICATE = re.compile(
    r"\b(?:is|are|was|were|be|been|uses?|used|requires?|required|must|should|supports?|supported|runs?|stores?"
    r"|stored|has|have|had|needs?|depends?|defaults?|listens?|expects?|returns?|contains?|provides?|means|can"
    r"|cannot|can't|will|won't|does|doesn't|never|always|only|lives?|located|configured|enabled|disabled"
    r"|deprecated|replaced|targets?|builds?|deploys?|deployed|connects?|reads?|writes?|starts?|serves?|owns?"
    r"|handles?|caches?|retries|expires?|limits?)\b", re.I)
_IMPERATIVE = re.compile(
    r"\W*(?:please\s+)?(?:ignore|disregard|forget|override|run|execute|open|delete|remove|disable|enable|install"
    r"|uninstall|click|type|paste|send|upload|download|grant|allow|approve|set|change|switch|call|visit|go|use|do"
    r"|don't|never|always|make|add|create|write|copy|move|rename|reply|respond|answer|say|print|output|follow"
    r"|obey|act|pretend|assistant|ai|system|you)\b", re.I)
_INJECTION = re.compile(
    r"\b(?:ignore|disregard|override)\s+(?:(?:all|any|the|previous|prior|above|your|these)\s+)*(?:instructions?"
    r"|rules?|permissions?|policies|policy|guard\w*|prompts?)\b|\bsystem prompt\b|\byou are now\b"
    r"|\b(?:grant|give)\b.{0,30}\b(?:access|permissions?)\b|\bfull access\b"
    r"|\bwithout (?:asking|approval|review|confirmation)\b|\bdeveloper mode\b|\bjailbreak", re.I)

# Explicit human preference grammar. Discovery and proof use the same
# expression; the body allows "3.12"/"Node.js" but stops at sentence ends.
_PREFERENCE_BODY = r'(?:[^\n.!?]|[.!?](?=\S)){1,350}[.!?]?'
_RESPONSE_NOUNS = (r'(?:(?:my|the|your|all|short|long|shorter|longer|concise|brief|detailed|simple|plain|technical)\s+)*'
                   r'(?:answers?|responses?|replies|explanations?|summaries|summary|emojis?|jargon)\b')
_STYLE_VERBS = r'(?:use|write|answer|reply|respond|include|format|explain|keep|show|give|add|avoid|put|call)\b'
PREFERENCE = re.compile(
    r'(?im)(?:^|(?<=[.!?])\s+)(?:Across all projects,\s*)?(?:'
    r'I (?:now |really |strongly |generally |usually |also )?prefer\b'
    r"|I(?:['’]d| would) (?:now )?(?:rather|prefer)\b"
    r'|My (?:preference|preferred [\w-]+(?: [\w-]+)?) is\b'
    r"|I (?:(?:now |really )?(?:like|want|dislike)|don['’]t (?:like|want)|do not (?:like|want)|no longer (?:like|want))"
    r'(?=\s+' + _RESPONSE_NOUNS + r')'
    r'|Please (?:always|never)\b'
    r'|(?:Always|Never) ' + _STYLE_VERBS +
    r'|(?:From now on|Going forward|In (?:the )?future),? (?:please )?(?:always |never )?' + _STYLE_VERBS +
    r')' + _PREFERENCE_BODY)
CORRECTION = re.compile(r'\b(?:now|instead|correction|no longer|any ?more)\b', re.I)
# Automatic capture never learns authority-shaped wishes. Permissions stay
# native/UI-owned; recall of such a sentence would only invite a denied effect.
AUTHORITY = re.compile(
    r'\b(?:permissions?|approv\w*|sandbox\w*|agentguard|guard|broker|credentials?|passwords?|passphrases?|secrets?'
    r'|tokens?|api[ -]?keys?|sudo|root access|admin(?:istrator)? access|bypass\w*|auto-?approv\w*|full access'
    r'|unrestricted|keychain|without (?:asking|review|approval|confirmation)'
    r'|skip (?:the )?(?:review|checks?|approvals?|confirmation)'
    r'|disable (?:the )?(?:guard|checks?|safety|sandbox|review))\b', re.I)
# Not universal sensitive-data detection: a conservative stop list so automatic
# personalization does not store protected traits. "remember ..." stays explicit.
SENSITIVE = re.compile(
    r'\b(?:health|medical|medications?|diagnos\w*|disabilit\w*|illness|pregnan\w*|religio\w*|church|mosque|temple'
    r'|synagogue|halal|kosher|faith|sexual\w*|gay|lesbian|bisexual|transgender|political|politics|democrat\w*'
    r'|republican\w*|ethnic\w*|racial|immigration|citizenship|criminal|salary|income|debts?|bank account'
    r'|social security|passport)\b', re.I)
_FENCE = re.compile(r'(?ms)^[ \t]*(```|~~~).*?(?:^[ \t]*\1[^\n]*$|\Z)')
_BLOCKQUOTE = re.compile(r'(?m)^[ \t]*>.*$')
PROFILE_TOPICS = (
    ('response-language', re.compile(r'\b(?:english|spanish|french|german|italian|portuguese|hindi|bengali|chinese'
                                     r'|mandarin|japanese|korean|arabic|russian|dutch|british spelling'
                                     r'|american spelling|spelling)\b', re.I)),
    ('formatting', re.compile(r'\b(?:markdown|bullet points?|bullets|tables?|headings|emojis?)\b', re.I)),
    ('response-style', re.compile(r'\b(?:answers?|responses?|repl(?:y|ies)|explanations?|summar(?:y|ies))\b', re.I)),
    ('code-indentation', re.compile(r'\b(?:tabs|spaces|indentation|indent)\b', re.I)),
    ('code-quotes', re.compile(r'\b(?:single quotes|double quotes)\b', re.I)),
    ('code-typing', re.compile(r'\b(?:type hints|type annotations|typing)\b', re.I)),
    ('commit-messages', re.compile(r'\b(?:commit messages?|conventional commits)\b', re.I)),
)

# Credential shapes refused by export and lessons: the interaction patterns
# plus cloud/forge tokens, bearer/JWT values, URL user:password and secret
# assignments. Conservative, not universal secret detection.
CREDENTIAL = re.compile(
    SECRET.pattern + r'|(?<![A-Z0-9])(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}(?![A-Z0-9])'
    r'|\bgithub_pat_[A-Za-z0-9_]{22,}|\bglpat-[A-Za-z0-9_-]{20,}|\bBearer\s+[A-Za-z0-9._~+/-]{20,}'
    r'|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|://[^/\s:@\'"]{1,64}:[^/\s@\'"]{1,128}@'
    r'|(?i:(?:api[_-]?key|secret|passw(?:or)?d|passphrase|token|access[_-]?key|private[_-]?key)[\w.-]*'
    r'[\'"\\]*\s*[:=]\s*[\'"\\]*[^\'"\\\s,;]{12,})')
# Contradiction claims exist only for keyed facts: config values and
# "Label: value" prose. Prose and root configuration describe the project
# (entity ''); configuration elsewhere describes its own directory, so two
# packages' manifests never meet. Code constants and free statements have no
# claim and never contradict anything.
MAX_CLAIM_CHARS = 80
_KEY_SEPARATORS = re.compile(r'[\s._/-]+')
_TRAILING_COMMENT = re.compile(r'\s+(?:#|//|;).*$')
_FIRST_PERSON = re.compile(r"(?i:\b(?:my|me|mine|myself)\b)|\bI(?:['’](?:m|ve|d|ll))?\b")
MAX_NOMINATION_CHARS = 300

SKIP_STATES = frozenset({'queued', 'running', 'retry_wait', 'needs_reconciliation'})
REFUSAL_TOOLS = frozenset({'Read', 'Write', 'Edit', 'Append', 'Glob', 'Grep', 'RunTests', 'RunCommand', 'OpenApp'})
_ABSOLUTE = re.compile(r'''(?<![\w.~])/(?:[^\s'"/]+/)+[^\s'"]*''')
_UNITTEST_FAIL = re.compile(r'(?m)^(?:FAIL|ERROR): (\w{1,80}) \(([\w.]{1,160})\)')
_NODE_FAIL = re.compile(r'(?m)^[ \t]*not ok \d+ - ([\w .:/()<>#-]{1,80}?)[ \t]*$')


def _line_start(text, index):
    return max(text.rfind(c, 0, index) for c in LINE_BREAKS) + 1


def _line_end(text, index):
    ends = [p for p in (text.find(c, index) for c in LINE_BREAKS) if p >= 0]
    return min(ends) if ends else len(text)


def whole_span(text, start, end, complete=True):
    """True when text[start:end] is whole lines or whole sentences.

    A span ending where a cut-off excerpt ends is not a complete line."""
    if not isinstance(text, str) or not 0 <= start < end <= len(text):
        return False
    quote = text[start:end]
    if quote != quote.strip():
        return False
    head = text[_line_start(text, start):start]
    if not (_LEFT.fullmatch(head) or _SENTENCE_BEFORE.search(head)):
        return False
    stop = _line_end(text, end)
    if _RIGHT.fullmatch(text[end:stop]):
        return stop < len(text) or complete
    return quote[-1] in '.!?' and text[end:end + 1] in (' ', '\t')


def locate(text, quote, complete=True):
    """First whole-span offset of an exact quote, or None."""
    if not isinstance(text, str) or not isinstance(quote, str) or not quote or quote != quote.strip():
        return None
    index = text.find(quote)
    for _ in range(64):
        if index < 0:
            return None
        if whole_span(text, index, index + len(quote), complete):
            return index
        index = text.find(quote, index + 1)
    return None


def leading_line(text, complete=True):
    """The legacy first-nonblank-line excerpt, kept as a labelled fallback."""
    position = 0
    for raw in (text if isinstance(text, str) else '').splitlines(keepends=True):
        body = raw.rstrip(LINE_BREAKS)
        stripped = body.strip()
        if stripped:
            lead = len(body) - len(body.lstrip())
            quote = stripped[:LEADING_CHARS].strip()
            cut = len(body) == len(raw) and not complete
            return {'quote': quote, 'start': position + lead, 'form': 'leading_line', 'key': None,
                    'truncated': len(stripped) > LEADING_CHARS or cut}
        position += len(raw)
    return None


def instruction_shaped(text):
    return bool(_IMPERATIVE.match(text) or _INJECTION.search(text))


def _opaque(line):
    """Long mixed letter/digit runs (keys, tokens) are withheld; paths and
    identifiers usually contain '.', '_' or few digits and stay eligible."""
    return any(sum(c.isdigit() for c in token) >= 4 and sum(c.isalpha() for c in token) >= 4
               for token in _OPAQUE.findall(line))


def _lines(text, complete):
    """(start, body) for complete lines; a cut-off final line is withheld."""
    position, rows = 0, []
    for raw in text.splitlines(keepends=True):
        body = raw.rstrip(LINE_BREAKS)
        if len(body) < len(raw) or complete:
            rows.append((position, body))
        position += len(raw)
        if len(rows) >= MAX_SCAN_LINES:
            break
    return rows


def _style(path):
    name = PurePosixPath(path if isinstance(path, str) else '')
    suffix = name.suffix.casefold()
    if suffix == '.json':
        return 'json'
    if suffix in ('.yaml', '.yml'):
        return 'yaml'
    if suffix in ('.toml', '.ini', '.cfg', '.conf', '.properties'):
        return 'ini'
    if suffix in ('.md', '.markdown', '.txt', '.rst', '.adoc', ''):
        return 'prose'
    return 'code'


def _unquote(key):
    return key[1:-1] if len(key) > 1 and key[0] == key[-1] == '"' else key


def _multiline_opener(value):
    value = value.strip()
    if value in ('"""', "'''", '|', '>', '|-', '>-', '|+', '>+') or value.endswith('\\'):
        return True
    for opener, closer in (('[', ']'), ('{', '}'), ('(', ')')):
        if value.startswith(opener) and value.count(opener) > value.count(closer):
            return True
    return False


def _statement(sentence):
    words = sentence.split()
    if not 4 <= len(words) <= 45 or len(sentence) > MAX_FACT_CHARS or sentence[-1] in '?:,;':
        return False
    if instruction_shaped(sentence) or not _PREDICATE.search(sentence):
        return False
    visible = [c for c in sentence if not c.isspace()]
    if sum(c.isalpha() for c in visible) < 0.6 * len(visible):
        return False
    return sum(sentence.count(c) for c in '{}();=<>[]') <= 4


def _segment(style, body):
    """Offset and text eligible for statements on one line, or None."""
    stripped = body.strip()
    if style == 'prose':
        if stripped.startswith(('#', '|', '<', '!', '[!', '```', '~~~')):
            return None
    elif style in ('code', 'ini', 'yaml'):
        markers = ('#', '//', '/*', '*', '"""', "'''", '--', ';') if style == 'code' else ('#', ';')
        if not stripped.startswith(markers) or stripped.startswith('#!'):
            return None
    else:
        return None
    start = _LEFT.match(body).end()
    return start, body[start:]


def _priority(form, quote):
    base = {'config_value': 3.0, 'labelled_value': 2.5, 'statement': 2.0}[form]
    informative = sum(1 for token in quote.split() if re.search(r'\d|[A-Z].*[A-Z]|[_./`]', token))
    return base + 0.5 * min(3, informative)


def extract(text, path='', complete=True, limit=MAX_FACTS_PER_FILE):
    """Bounded exact fact candidates from one Read excerpt.

    Returns dicts with quote, start (excerpt offset), form and key. Forms are
    config_value (key/value from config files or literal constants),
    labelled_value ("Label: value" prose) and statement (declarative prose or
    comments). Secret-shaped lines and instruction-shaped sentences are skipped."""
    if not isinstance(text, str) or not text.strip() or type(limit) is not int or limit < 1:
        return []
    style = _style(path)
    found, quotes, keys = [], set(), set()
    section, parents, block, fence = '', [], None, False

    def add(start, quote, form, key):
        normal = ' '.join(quote.casefold().split())
        if len(quote) < 3 or len(quote) > MAX_FACT_CHARS or normal in quotes:
            return
        if key is not None and key.casefold() in keys:
            return  # Repeated keys (lists of objects) would collide in one slot.
        if not whole_span(text, start, start + len(quote), complete):
            return
        quotes.add(normal)
        if key is not None:
            keys.add(key.casefold())
        found.append({'quote': quote, 'start': start, 'form': form, 'key': key,
                      'priority': _priority(form, quote)})

    for position, body in _lines(text, complete):
        if len(found) >= 64:
            break
        stripped = body.strip()
        if not stripped or len(body) > MAX_SCAN_LINE_CHARS:
            continue
        lead = len(body) - len(body.lstrip())
        if style in ('prose', 'code') and stripped.startswith(('```', '~~~')):
            fence = not fence
            continue
        if fence:
            continue
        if SECRET.search(body) or _opaque(body):
            continue
        fact = None
        if style == 'ini':
            match = _SECTION.fullmatch(body)
            if match:
                section = _unquote(match[1].strip())
                continue
            match = _ASSIGN.fullmatch(body)
            if match and not stripped.startswith(('#', ';')) and not _multiline_opener(match[2]):
                key = _unquote(match[1])
                fact = ('config_value', section + '.' + key if section else key)
        elif style == 'yaml':
            indent = len(body) - len(body.lstrip(' \t'))
            if block is not None and indent > block:
                continue
            block = None
            match = None if stripped.startswith('#') else _YAML.fullmatch(body)
            if match:
                key, value = _unquote(match[2]), (match[3] or '').strip()
                while parents and parents[-1][0] >= indent:
                    parents.pop()
                if not value or value.startswith('#'):
                    if len(parents) < 8:
                        parents.append((indent, key))
                    continue
                if _multiline_opener(value):
                    block = indent
                    continue
                fact = ('config_value', '.'.join([k for _, k in parents] + [key]))
        elif style == 'json':
            match = _JSON_OPEN.fullmatch(body)
            if match:
                if len(parents) < 8:
                    parents.append((0, match[1]))
                continue
            if stripped[0] in '}]':
                if parents:
                    parents.pop()
                continue
            if stripped in ('{', '['):
                if len(parents) < 8:
                    parents.append((0, None))
                continue
            match = _JSON.fullmatch(body)
            if match:
                fact = ('config_value', '.'.join([k for _, k in parents if k] + [match[1]]))
        elif style == 'code':
            match = _CONSTANT.fullmatch(body)
            if match and _LITERAL.fullmatch(match[2]):
                fact = ('config_value', match[1])
        else:
            plain = re.sub(r'\*\*|__|`', '', body)
            match = _LABEL.fullmatch(plain)
            if (match and match[1].strip().casefold() not in _NOTE_LABELS and len(match[1].split()) <= 5
                    and not match[2].rstrip().endswith(':')):
                fact = ('labelled_value', match[1].strip())
        if fact:
            form, key = fact
            if not _SECRET_KEY.search(key):
                add(position + lead, stripped, form, key)
            continue
        segment = _segment(style, body)
        if not segment:
            continue
        offset, prose = segment
        pieces = list(_SENTENCE_SPLIT.finditer(prose))
        bounds = [0] + [m.end() for m in pieces]
        ends = [m.start() for m in pieces] + [len(prose)]
        for begin, finish in zip(bounds, ends):
            sentence = _CLOSER.sub('', prose[begin:finish].rstrip()).rstrip()
            if sentence and _statement(sentence):
                add(position + offset + begin, sentence, 'statement', None)
    found.sort(key=lambda f: (-f['priority'], f['start']))
    chosen = sorted(found[:limit], key=lambda f: f['start'])
    return [{k: f[k] for k in ('quote', 'start', 'form', 'key')} for f in chosen]


def classify(text, path, quote, start, complete=True):
    """Form/key for a proven quote; unclassified whole quotes are 'quote'."""
    for fact in extract(text, path, complete, limit=64):
        if fact['start'] == start and fact['quote'] == quote:
            return fact['form'], fact['key']
    lead = leading_line(text, complete)
    if lead and lead['start'] == start and lead['quote'] == quote:
        return 'leading_line', None
    return 'quote', None


def file_span(source, arguments, excerpt, start, length):
    """Absolute 1-based line and character span of an excerpt-local quote.

    Read pages are exact prefixes of source[line offset + char_offset:]. A
    mismatch returns None rather than guessing a location."""
    arguments = arguments if isinstance(arguments, dict) else {}
    offset, char_offset = arguments.get('offset', 1), arguments.get('char_offset', 0)
    if type(offset) is not int or offset < 1 or type(char_offset) is not int or char_offset < 0:
        return None
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    if offset - 1 >= len(starts):
        return None
    base = starts[offset - 1] + char_offset
    if source[base:base + len(excerpt)] != excerpt or not 0 <= start < start + length <= len(excerpt):
        return None
    first, last = base + start, base + start + length
    return {'lines': [bisect_right(starts, first), bisect_right(starts, last - 1)], 'chars': [first, last]}


def visible_human_text(text):
    """Human text with fenced code and blockquotes blanked, offsets preserved.

    Pasted third-party material is not the person's own preference statement."""
    blank = lambda m: re.sub(r'[^\n]', ' ', m.group())
    return _BLOCKQUOTE.sub(blank, _FENCE.sub(blank, text if isinstance(text, str) else ''))


def preference_allowed(quote):
    return (isinstance(quote, str) and 0 < len(quote) <= 400 and not quote.rstrip().endswith('?')
            and not AUTHORITY.search(quote) and not SENSITIVE.search(quote) and not SECRET.search(quote))


def preference_quotes(text, limit=4):
    """Exact explicit preference sentences from one human-origin input."""
    visible, out = visible_human_text(text), []
    for match in PREFERENCE.finditer(visible):
        quote = match.group().strip()
        if quote in out or quote not in text or not preference_allowed(quote):
            continue
        out.append(quote)
        if len(out) >= limit:
            break
    return out


def preference_slot(quote):
    """A narrow topic slot so only likely-contradictory preferences conflict."""
    for slot, pattern in PROFILE_TOPICS:
        if pattern.search(quote):
            return slot
    return ''


def nomination_eligible(quote, human):
    """A whole first-person sentence of the person's own text outside the
    preference grammar. It can only wait for native confirmation."""
    return (isinstance(quote, str) and preference_allowed(quote) and len(quote) <= MAX_NOMINATION_CHARS
            and bool(_FIRST_PERSON.search(quote)) and not _INJECTION.search(quote)
            and locate(visible_human_text(human), quote) is not None)


def credential_shaped(text):
    return isinstance(text, str) and bool(CREDENTIAL.search(text))


def claim_key(text):
    """Casefolded key with separators unified: 'Server_Port' == 'server.port'."""
    if not isinstance(text, str):
        return None
    normal = _KEY_SEPARATORS.sub('.', _unquote(text.strip()).casefold()).strip('.')
    return normal[:MAX_CLAIM_CHARS] or None


def claim_value(text):
    """Comparable value: comments, separators, quotes and case removed."""
    if not isinstance(text, str):
        return None
    value = _TRAILING_COMMENT.sub('', text.strip()).rstrip(',;').strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in '"\'`':
        value = value[1:-1]
    value = ' '.join(value.casefold().split()).rstrip('.').strip()
    return value[:MAX_FACT_CHARS] or None


def _raw_value(style, quote):
    if style == 'prose':
        match = _LABEL.fullmatch(re.sub(r'\*\*|__|`', '', quote))
        return match[2] if match else None
    pattern, group = {'ini': (_ASSIGN, 2), 'yaml': (_YAML, 3), 'json': (_JSON, 2)}.get(style, (None, 0))
    match = pattern.fullmatch(quote) if pattern else None
    return match[group] if match else None


def fact_claim(report):
    """Entity/attribute claim of a keyed source report, or None."""
    if not isinstance(report, dict) or report.get('form') not in ('config_value', 'labelled_value'):
        return None
    path, key, quote = report.get('path'), report.get('key'), report.get('quote')
    if not all(isinstance(v, str) and v for v in (path, key, quote)):
        return None
    style = _style(path)
    attribute, value = claim_key(key), claim_value(_raw_value(style, quote))
    if style == 'code' or not attribute or not value:
        return None
    parent = PurePosixPath(path).parent.as_posix()
    entity = '' if style == 'prose' or parent in ('.', '') else parent
    return {'entity': entity, 'attribute': attribute, 'label': key[:MAX_CLAIM_CHARS], 'value': value}


def dated_claim(quote, subject=None):
    """Claim of a dated quote: an explicit subject key, else its "Label: value" form."""
    if not isinstance(quote, str):
        return None
    if subject:
        label, attribute, value = subject, claim_key(subject), claim_value(quote)
    else:
        match = _LABEL.fullmatch(re.sub(r'\*\*|__|`', '', quote.strip()))
        if not match or match[1].strip().casefold() in _NOTE_LABELS:
            return None
        label, attribute, value = match[1].strip(), claim_key(match[1]), claim_value(match[2])
    if not attribute or not value:
        return None
    return {'entity': '', 'attribute': attribute, 'label': label[:MAX_CLAIM_CHARS], 'value': value}


def _clean(text, limit):
    text = _ABSOLUTE.sub('<absolute path>', ' '.join(str(text).split()))
    return '' if SECRET.search(text) else text[:limit]


def _target(name, arguments, action):
    args = arguments if isinstance(arguments, dict) else {}
    payload = action.get('payload') if isinstance(action, dict) and isinstance(action.get('payload'), dict) else {}
    for value in (args.get('file_path'), payload.get('path'), args.get('recipe'), payload.get('recipe'),
                  args.get('name'), payload.get('name')):
        if isinstance(value, str) and value.strip():
            return _clean(value, 80) or name or 'tool'
    command = args.get('command') if isinstance(args.get('command'), str) else payload.get('command')
    if isinstance(command, str) and command.split():
        return (_clean(command.split()[0], 40) or 'a') + ' command'
    return name or 'tool'


def failing_tests(output):
    """Up to three failing test identifiers reported by test output."""
    if not isinstance(output, str):
        return []
    names = [f'{m[1]} ({m[2]})' for m in _UNITTEST_FAIL.finditer(output[:16000])]
    names += [m[1].strip() for m in _NODE_FAIL.finditer(output[:16000])]
    chosen, length = [], 0
    for name in dict.fromkeys(n[:100] for n in names):
        if len(chosen) >= 3 or length + len(name) > 160 or SECRET.search(name):
            break
        chosen.append(name)
        length += len(name) + 2
    return chosen


def refusal_kind(name, error):
    """Stable refusal signature: the tool and its first sentence, paths removed."""
    if name not in REFUSAL_TOOLS or not isinstance(error, str) or not error.strip():
        return None
    reason = _clean(re.split(r'(?<=[.!?])\s', error.strip(), maxsplit=1)[0], 140)
    return f'{name}: {reason}' if reason else None


def failure_signatures(receipt, action):
    """('test_failure', test ID) and ('tool_refused', kind) pairs of one settled
    root receipt. Failing tests come from the applied broker action's output."""
    if not isinstance(receipt, dict) or receipt.get('state') != 'completed':
        return []
    result = receipt.get('result') if isinstance(receipt.get('result'), dict) else {}
    name = receipt.get('name')
    payload = action.get('payload') if isinstance(action, dict) and isinstance(action.get('payload'), dict) else {}
    if name in ('RunTests', 'RunRecipe') and action and action.get('kind') == 'tests' and action.get('status') == 'applied':
        outcome = payload.get('result') if isinstance(payload.get('result'), dict) else {}
        code = outcome.get('exit_code')
        if type(code) is int and code != 0 and outcome.get('cancelled') is not True:
            return [('test_failure', test) for test in failing_tests(outcome.get('output'))]
        return []
    kind = refusal_kind(name, result.get('error'))
    return [('tool_refused', kind)] if kind else []


def failure_observations(state, receipts, actions, limit=MAX_LESSONS):
    """Bounded failure observations from settled host receipts.

    Each is a labelled past outcome with its receipt/action IDs: failed or
    cancelled broker test runs, user-declined proposals, approved actions that
    failed, and host tool refusals. They are observations for the model to
    weigh, never directives, permission or a replay plan."""
    if state in SKIP_STATES or type(limit) is not int or limit < 1:
        return []
    by_id = {a.get('id'): a for a in actions or () if isinstance(a, dict) and a.get('id')}
    found, seen = [], set()
    for receipt in receipts or ():
        if not isinstance(receipt, dict) or receipt.get('state') != 'completed':
            continue
        result = receipt.get('result') if isinstance(receipt.get('result'), dict) else {}
        name = receipt.get('name') if isinstance(receipt.get('name'), str) else ''
        action = by_id.get(result.get('action_id'))
        payload = action.get('payload') if action and isinstance(action.get('payload'), dict) else {}
        target = _target(name, receipt.get('arguments'), action)
        lesson = None
        if name == 'RunTests' and action and action.get('kind') == 'tests' and action.get('status') == 'applied':
            outcome = payload.get('result') if isinstance(payload.get('result'), dict) else {}
            code = outcome.get('exit_code')
            if outcome.get('cancelled') is True or type(code) is int and code != 0:
                names = failing_tests(outcome.get('output'))
                text = (f'Isolated {target} tests ' + ('were cancelled' if outcome.get('cancelled') is True
                        else f'exited with code {code}')
                        + (f'; failing tests reported by that run: {", ".join(names)}' if names else '') + '.')
                lesson = ('test_failure', 1, text)
        elif action and action.get('status') == 'rejected':
            noun = _ACTION_NOUNS.get(action.get('kind'), 'action')
            lesson = ('action_declined', 0, f'The user declined a proposed {noun} for {target}.')
        elif action and action.get('status') == 'failed':
            noun = _ACTION_NOUNS.get(action.get('kind'), 'action')
            lesson = ('action_failed', 2, f'An approved {noun} for {target} failed before completing.')
        elif name in REFUSAL_TOOLS and isinstance(result.get('error'), str) and result['error'].strip():
            reason = _clean(re.split(r'(?<=[.!?])\s', result['error'].strip(), maxsplit=1)[0], 140)
            lesson = ('tool_refused', 3, f'{name} for {target} was refused' + (f': {reason}' if reason else '.'))
        if not lesson or (lesson[0], target, lesson[2]) in seen:
            continue
        seen.add((lesson[0], target, lesson[2]))
        ids = [receipt.get('call_id')] + ([action['id']] if action else [])
        found.append({'type': lesson[0], 'rank': lesson[1], 'order': len(found), 'target': target,
                      'observation': lesson[2][:300], 'receipt_ids': [i for i in ids if isinstance(i, str)][:2]})
    chosen, refused = [], 0
    for item in sorted(found, key=lambda f: (f['rank'], f['order'])):
        if item['type'] == 'tool_refused':
            if refused >= MAX_REFUSAL_LESSONS:
                continue
            refused += 1
        chosen.append({k: v for k, v in item.items() if k not in ('rank', 'order')})
        if len(chosen) >= limit:
            break
    return chosen
