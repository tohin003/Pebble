"""Deterministic learning candidates from records the host already owns.

Inputs are a Read receipt excerpt, human-origin text, or settled broker/tool
receipts. A fact candidate is an exact excerpt substring with its local offset;
the learning proof re-checks the current file before promotion. No model output
is parsed, summarized or trusted, and nothing here authorizes an action. Source
text stays a source report: instruction-shaped lines are labelled, never turned
into procedures or preferences.
"""

import re
import unicodedata
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
_ACTION_NOUNS = {'edit': 'edit', 'command': 'command', 'tests': 'test run', 'app': 'app launch', 'plugin': 'plugin call',
                 'screen': 'step'}  # Phase 45 (F256, audit round R11)

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
# Phase 46 (1.9.28, follow-up 1; the w5 security review, findings 1, 3 and 8): a saved memory reaches every later
# session as the person's own standing instruction, so it may only come from the person's own words. Material they paste
# or quote is someone else's, whatever it says ("Please remember that our bank details changed: ...", "going forward I
# want all replies sent to ..."): fenced code also when opened mid-line, blockquotes, indented lines, HTML, a forwarded or
# replied e-mail, a letter after its salutation, and whatever a line of theirs introduces ("Summarize this email:",
# "What does this email want from me?", "Here is what our vendor sent:"; never "Remember the following:"). Only the
# first MAX_OWN_SCAN characters are read; the rest is material. Every scan here is one pass or bounded per position.
MAX_OWN_SCAN = 16000
# a line of the person's that asks to keep something introduces their own list, never material
_REQUEST_CUE = re.compile(r"\b(?:remember\w*|memori[sz]e|keep\b[^\n]{0,30}?\bin\s+mind|bear\b[^\n]{0,30}?\bin\s+mind|"
                          r"don['’]?t\s+forget|note|save|store|future|later|next\s+(?:time|sessions?)|from\s+now\s+on|"
                          r"going\s+forward|across\s+sessions|"
                          # Phase 46 (1.9.29, w22): "Commit this to memory: ...", "Add this to your memory: ..." ask too
                          # (they read as "Translate this:"); the curly "Don’t" macOS types
                          r"(?:commit|add|put)\s+(?:this|that|it|these|those|the\s+following)\s+(?:to|in|into)\s+"
                          r"(?:your\s+|my\s+)?memory)\b", re.I)
_MATERIAL = re.compile(r"\b(?:this|these|below|following|attached|forwarded|enclosed|pasted|quoted)\b[^\n]{0,40}?\b(?:"
                       r"e-?mails?|mails?|messages?|letters?|notes?|texts?|snippets?|documents?|docs?|logs?|threads?|posts?|"
                       r"comments?|replies|reply|drafts?|pages?|articles?|transcripts?|chats?|notices?|announcements?|memos?|"
                       r"minutes|code|output|excerpts?|passages?|paragraphs?|quotes?|tweets?|reviews?|tickets?)\b|"
                       r"\bhere(?:'s|\s+is|\s+are)\b", re.I)
_REPORTED = re.compile(r"\b(?:wrote|writes|said|says|sent|asks|asked|replied|replies|posted|commented|emailed|messaged|"
                       r"told\s+(?:me|us)|this|these|the\s+following|below|here(?:'s|\s+is|\s+are)|notice|message|e-?mail|"
                       r"letter|announcement|memo|reply|comment|post|tweet|review|ticket|from\s+(?:the|our|my|your|an?)\s+\w+)\b",
                       re.I)
_ASCII_LOWER = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')
_EMAIL_LINE = re.compile(r"(?:-{2,}\s*(?:forwarded|original)\s+message\s*-{2,}|begin\s+forwarded\s+message\s*:|"
                         r"-{3,}\s*original\s+message|(?:from|subject|sent|reply-to)\s*:\s*\S|on\s[^\n]{1,200}\bwrote\s*:\s*$|"
                         r"[^\n]{1,120}\bwrote\s*:\s*$|_{8,}\s*$)", re.I)
_HTML_LINE = re.compile(r'<[A-Za-z!?/]')
_SALUTATION = re.compile(r"(?:hi|hello|hey|dear|greetings|good\s+(?:morning|afternoon|evening))\b(?!\s*,?\s*pebble\b)"
                         r"(?:\s+[A-Z][\w.'’-]*){0,3}\s*[,!:]", re.I)
_HTML_BLOCKS = ('blockquote', 'pre', 'code', 'q', 'cite', 'samp', 'kbd', 'textarea', 'script', 'style', 'template', 'xmp')
# quotation marks and inline code: someone else's words inside a sentence of the person's
_QUOTE_PAIRS = {'"': '"', '“': '”', '„': '“”', '‚': '‘’', '«': '»', '»': '«', '‹': '›', '›': '‹', '‘': '’', "'": "'",
                '「': '」', '『': '』', '`': '`'}
_QUOTE_MARKS = re.compile('[%s]' % re.escape(''.join(_QUOTE_PAIRS)))
_SPAN_LIMIT, _MAX_MARKS = 600, 4000
PLACEHOLDER = '·'  # stands in for a quoted character in cue_text: not a space and not a word character
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

    Pasted third-party material is not the person's own preference statement.

    Phase 46 (1.9.28, follow-up 1): also fences opened mid-line, indented lines, HTML, a forwarded or replied e-mail,
    a letter after its salutation, and what a line of theirs introduces as material (see _material_start); invisible
    format, tag and bidi characters become spaces and compatibility forms their plain character (a fullwidth ＞ is a
    blockquote). The result has the input's length; past MAX_OWN_SCAN it is blank."""
    text = text if isinstance(text, str) else ''
    head, tail = text[:MAX_OWN_SCAN], text[MAX_OWN_SCAN:]
    blank = lambda m: re.sub(r'[^\n]', ' ', m.group())  # noqa: E731
    head = _BLOCKQUOTE.sub(blank, _FENCE.sub(blank, neutral_text(head)))
    head = _blank_inline_fences(_blank_html_blocks(head))
    lines, kept = head.split('\n'), []
    for line in lines:  # indented lines: pasted code or a pasted letter
        kept.append(' ' * len(line) if line.startswith(('    ', '\t')) and line.strip() else line)
    head = '\n'.join(kept)
    cut = _material_start(head)
    if cut is not None:
        head = head[:cut] + re.sub(r'[^\n]', ' ', head[cut:])
    return _blank_reported(head) + re.sub(r'[^\n]', ' ', tail)


def neutral_text(text):
    """Phase 46 (1.9.28, follow-up 1, finding 8): invisible format, tag and variation characters as spaces,
    compatibility forms as their plain character (same length: a form that normalizes to several characters stays as it
    is)."""
    out = []
    for char in text:
        code = ord(char)
        if code < 0x80:
            out.append(char)
        elif unicodedata.category(char) == 'Cf' or 0xFE00 <= code <= 0xFE0F or 0xE0000 <= code <= 0xE0FFF:
            out.append(' ')
        else:
            plain = unicodedata.normalize('NFKC', char)
            out.append(plain if len(plain) == 1 else char)
    return ''.join(out)


def clean_text(text):
    """Phase 46 (1.9.28, follow-up 1, finding 8): text as it is saved and compared: NFKC, without invisible format, tag,
    bidi or variation characters, whitespace runs as one space."""
    text = unicodedata.normalize('NFKC', text if isinstance(text, str) else '')
    text = ''.join(c for c in text if not (unicodedata.category(c) == 'Cf' or 0xFE00 <= ord(c) <= 0xFE0F
                                            or 0xE0000 <= ord(c) <= 0xE0FFF))
    return ' '.join(text.split())


def _blank_inline_fences(text):
    """A fence opened mid-line ("Check this snippet ```") to its closing fence or the end. Each marker's next place is
    looked up only once the scan has passed it, so the whole is one pass."""
    out, index, next_at = [], 0, {'```': text.find('```'), '~~~': text.find('~~~')}
    while True:
        for mark, at in list(next_at.items()):
            if 0 <= at < index:
                next_at[mark] = text.find(mark, index)
        found = [i for i in next_at.values() if i >= 0]
        if not found:
            out.append(text[index:])
            return ''.join(out)
        start = min(found)
        marker = text[start:start + 3]
        close = text.find(marker, start + 3)
        end = len(text) if close < 0 else close + 3
        out.append(text[index:start])
        out.append(re.sub(r'[^\n]', ' ', text[start:end]))
        index = end


def _blank_html_blocks(text):
    """<blockquote>, <pre>, <code>, <q> ... elements, to their closing tag or the end (one pass)."""
    lower, out, index = text.translate(_ASCII_LOWER), [], 0  # same length as text, unlike str.lower()
    while True:
        start = lower.find('<', index)
        if start < 0:
            out.append(text[index:])
            return ''.join(out)
        name = re.match(r'<([a-z]+)\b', lower[start:start + 16])
        if not name or name[1] not in _HTML_BLOCKS:
            out.append(text[index:start + 1])
            index = start + 1
            continue
        close = lower.find('</' + name[1], start)
        end = len(text) if close < 0 else (lower.find('>', close) + 1 or len(text))
        out.append(text[index:start])
        out.append(re.sub(r'[^\n]', ' ', text[start:end]))
        index = end


def _material_start(text):
    """The offset where pasted material begins, or None: a forwarded or replied e-mail's marker or header line, an HTML
    line, a salutation after the person's first line, or the line after one of theirs that introduces material (ends
    with ':', or names "this email", "the following", "here is" ...) without asking to keep anything."""
    position, seen, pending = 0, False, None
    for line in text.split('\n'):
        start, position = position, position + len(line) + 1
        stripped = line.strip()
        if not stripped:
            continue
        if pending is not None:
            return pending
        head = stripped[:300]
        if _EMAIL_LINE.match(head) or _HTML_LINE.match(head) or seen and _SALUTATION.match(head):
            return start
        last = re.split(r'(?<=[.!?])\s+', stripped[-300:])[-1]
        if stripped.endswith(':') and not _REQUEST_CUE.search(last):
            pending = start + len(line) + 1
        elif _MATERIAL.search(stripped[:300]) and not _REQUEST_CUE.search(stripped[:600]):
            pending = start + len(line) + 1
        seen = True
    return None


def _blank_reported(text):
    """Someone else's words after a colon on the person's line ("My colleague wrote: ...", "Translate this: ..."): the
    rest of the line, when what comes before the colon reports or points and asks to keep nothing."""
    lines = text.split('\n')
    for number, line in enumerate(lines):
        colon = line.find(':', 0, 400)
        tries = 0
        while colon >= 0 and tries < 3:
            head = re.split(r'(?<=[.!?])\s+', line[:colon])[-1]
            if _REPORTED.search(head) and not _REQUEST_CUE.search(head):
                lines[number] = line[:colon + 1] + ' ' * (len(line) - colon - 1)
                break
            colon, tries = line.find(':', colon + 1, 400), tries + 1
    return '\n'.join(lines)


def quotation_spans(text, limit=_SPAN_LIMIT, marks=_MAX_MARKS):
    """[(start, end)] of quotations and inline code in text ("…", “…”, «…», ‘…’, '…', `…` ...), each within a paragraph
    and at most `limit` characters; an apostrophe ("don't", "users'") never opens or closes one. One pass; past `marks`
    quotation marks the rest of the text counts as quoted (nothing there is the person's own request)."""
    spans, index, size = [], 0, len(text)
    while True:
        found = _QUOTE_MARKS.search(text, index)
        if not found:
            return spans
        index, marks = found.start(), marks - 1
        if marks < 0:
            spans.append((index, size))
            return spans
        if not _opens(text, index):
            index += 1
            continue
        end = _closer(text, index + 1, _QUOTE_PAIRS[text[index]], min(size, index + 1 + limit))
        if end < 0:
            index += 1
            continue
        spans.append((index, end + 1))
        index = end + 1


def _opens(text, index):
    after = text[index + 1:index + 2]
    if not after or after.isspace():
        return False
    return text[index] not in "'‘’`" or index == 0 or not text[index - 1].isalnum()


def _closer(text, start, close, stop):
    """The closing mark's index (at most 8 candidates looked at), or -1."""
    breaks = text.find('\n\n', start, stop)
    stop = breaks if breaks >= 0 else stop
    index = start
    for _ in range(8):
        hits = [i for i in (text.find(mark, index, stop) for mark in close) if i >= 0]
        if not hits:
            return -1
        index = min(hits)
        apostrophe = text[index] in "'’`" and index + 1 < len(text) and text[index + 1].isalnum()  # "don't", "it’s"
        if not text[index - 1].isspace() and not apostrophe:
            return index
        index += 1
    return -1


def cue_text(text):
    """Phase 46 (1.9.28, follow-up 1): text with every quotation and inline code span replaced by PLACEHOLDER (same
    length; newlines kept), so a cue inside them ("«please remember that ...»") is no request of the person's."""
    if not isinstance(text, str) or not _QUOTE_MARKS.search(text):
        return text if isinstance(text, str) else ''
    out, last = [], 0
    for start, end in quotation_spans(text):
        out += [text[last:start], re.sub(r'[^\n]', PLACEHOLDER, text[start:end])]
        last = end
    return ''.join(out) + text[last:]


def own_quote(human, quote):
    """Phase 46 (1.9.28, follow-up 1): whether `quote` is the person's own words in `human`: it occurs in the visible
    text (pasted material blanked) at a place that is not inside a quotation."""
    if not isinstance(quote, str) or not quote.strip():
        return False
    visible = visible_human_text(human)
    marked = cue_text(visible)
    quote = quote.strip()
    index = visible.find(quote)
    while index >= 0:
        if marked[index] != PLACEHOLDER:
            return True
        index = visible.find(quote, index + 1)
    return False


def preference_allowed(quote):
    return (isinstance(quote, str) and 0 < len(quote) <= 400 and not quote.rstrip().endswith('?')
            and not AUTHORITY.search(quote) and not SENSITIVE.search(quote) and not SECRET.search(quote))


def preference_quotes(text, limit=4):
    """Exact explicit preference sentences from one human-origin input. Phase 46 (1.9.28, follow-up 1): never one
    inside a quotation ("My colleague wrote “Always use tabs.”")."""
    visible, out = visible_human_text(text), []
    marked = cue_text(visible)
    for match in PREFERENCE.finditer(visible):
        quote = match.group().strip()
        if marked[match.start() + len(match.group()) - len(match.group().lstrip())] == PLACEHOLDER:
            continue
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


# Phase 46 (1.9.28, follow-up 2: D183, D190 and D176): what a reply's own text can be checked against, as {slot: value}:
# 'date' (a numeric pattern, 'DD.MM.YYYY', 'YYYY-MM-DD', 'MM/DD/YYYY' ...), 'currency' (an ISO code), 'decimals' (a
# count), 'units' ('metric' or 'imperial') and 'clock' ('24h' or '12h'). Only what the person wrote in so many words; the
# rest of a preference is no slot. Every pattern is bounded; a text is read up to SLOT_TEXT characters.
SLOT_TEXT = 2000
CURRENCIES = ('CHF', 'EUR', 'USD', 'GBP', 'JPY', 'CAD', 'AUD', 'SEK', 'NOK', 'DKK', 'PLN', 'CZK', 'CNY', 'HKD', 'SGD',
              'NZD', 'INR', 'ZAR', 'BRL', 'MXN')
_CODES = '|'.join(CURRENCIES)
CURRENCY_WORDS = {'swiss franc': 'CHF', 'franc': 'CHF', 'euro': 'EUR', 'us dollar': 'USD', 'pound sterling': 'GBP',
                  'british pound': 'GBP', 'yen': 'JPY'}
_DATE_PATTERN = re.compile(r'(?<![\w.])([DMY]{1,4})([./-])([DMY]{1,4})\2([DMY]{1,4})(?![\w])', re.I)
_ISO_DATES = re.compile(r'\bISO(?:[ -]?8601)?\b', re.I)
_CURRENCY = re.compile(r'(?i:\b(?:currenc(?:y|ies)|conversions?|convert\w*|amounts?|prices?|money|costs?|budgets?|totals?)\b'
                       r'[^.;\n]{0,40}?|\b(?:in|into|to|as) )\b(' + _CODES + r'|swiss francs?|francs?|euros?|us dollars?|'
                       r'pounds? sterling|british pounds?|yen)\b', re.I)
_DECIMALS = re.compile(r'\b(no|zero|one|two|three|four|[0-4]) (?:decimals?|decimal places?|decimal digits?)\b|\b([0-4]) ?dp\b',
                       re.I)
_NUMBER_WORDS = {'no': 0, 'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4}
_UNITS = re.compile(r'\b(metric|imperial|us customary)\b', re.I)
_CLOCK = re.compile(r'\b(24h|24-hour|24 hour|military time)(?: (?:clock|time|format))?\b|\b(12h|12-hour|12 hour|am/pm)'
                    r'(?: (?:clock|time|format))?\b', re.I)
_CLOCK_CONTEXT = re.compile(r'\b(?:times?|clock|hours? format|24h|12h|am/pm|military)\b', re.I)
SLOT_TOPICS = {'date': re.compile(r'\bdates?\b', re.I),
               'currency': re.compile(r'\bcurrenc(?:y|ies)\b|\bconversions?\b|\b(?:' + _CODES + r')\b', re.I),
               'decimals': re.compile(r'\bdecimals?\b', re.I),
               'units': re.compile(r'\bunits?\b|\bmetric\b|\bimperial\b', re.I),
               'clock': re.compile(r'\b(?:times|clock|24h|12h|24-hour|12-hour|am/pm)\b', re.I)}
_SLOT_FILLER = frozenset(
    "i i'd i'm i've my me we our please always never want wants like likes prefer prefers preferred preference use uses "
    "used write writes written give given show shown display displayed format formatted formats as in into with to be "
    "is are should must the a an for from now on going forward future later next sessions session time remember that "
    "this all of and keep mind dates date amounts amount prices price conversions conversion currency units unit "
    "decimals decimal places digits times clock hour hours style numbers number every across also just here it them "
    "make sure so would rather instead not no longer don't do any more anymore franc francs euro euros dollar dollars "
    "pound pounds sterling swiss british us yen iso metric imperial customary 24h 12h am pm dp military "
    # w17 (the follow-ups review's finding 5): what an update says around its value ("Actually, I'd rather have amounts
    # in EUR now, thanks") and a count's words ("two decimals"); never a thing an amount or a date belongs to
    "actually okay ok thanks thank you your though really correction sorry yes yeah well then again back let's go have "
    "get everything everywhere replies reply answers answer responses response messages zero one two three four "
    # 1.9.29 (the lead): a change request's own words ("Please change my date preference to YYYY-MM-DD") say
    # only its slot too, so the newest record decides it and the reply check holds the new value
    "change switch set update setting settings default".split())


def date_format(token):
    """'DD.MM.YYYY' for a written date pattern ("dd.mm.yyyy", "D.M.YYYY"), or None: one day, one month and one year
    part, in any order, with one separator."""
    match = _DATE_PATTERN.fullmatch(token.strip())
    if not match:
        return None
    parts = [match[1].upper(), match[3].upper(), match[4].upper()]
    if sorted(part[0] for part in parts) != ['D', 'M', 'Y'] or any(len(set(part)) != 1 for part in parts):
        return None
    if any(part[0] in 'DM' and len(part) > 2 or part[0] == 'Y' and len(part) not in (2, 4) for part in parts):
        return None
    return match[2].join({'D': 'DD', 'M': 'MM', 'Y': 'YYYY'}[part[0]] for part in parts)


def preference_slots(text):
    """{slot: value} of what a saved preference says in so many words (above); {} for anything else."""
    text = clean_text(text)[:SLOT_TEXT] if isinstance(text, str) else ''
    found = {}
    for match in _DATE_PATTERN.finditer(text):
        value = date_format(match.group())
        if value:
            found.setdefault('date', value)
    if 'date' not in found and _ISO_DATES.search(text) and SLOT_TOPICS['date'].search(text):
        found['date'] = 'YYYY-MM-DD'
    currency = _CURRENCY.search(text)
    if currency:
        word = currency[1].casefold().rstrip('s')
        found['currency'] = currency[1].upper() if currency[1].upper() in CURRENCIES else next(
            (code for name, code in CURRENCY_WORDS.items() if word.endswith(name)), None)
        if not found['currency']:
            del found['currency']
    decimals = _DECIMALS.search(text)
    if decimals:
        count = decimals[1] or decimals[2]
        found['decimals'] = int(count) if count.isdigit() else _NUMBER_WORDS[count.casefold()]
    units = _UNITS.search(text)
    if units:
        found['units'] = 'metric' if units[1].casefold() == 'metric' else 'imperial'
    clock = _CLOCK.search(text)
    if clock and _CLOCK_CONTEXT.search(text):
        found['clock'] = '24h' if clock[1] else '12h'
    return found


def slots_touched(text):
    """The slots a text speaks of, by a value or a topic word ("my date format preference", "what I said about
    dates")."""
    text = clean_text(text)[:SLOT_TEXT] if isinstance(text, str) else ''
    return set(preference_slots(text)) | {slot for slot, topic in SLOT_TOPICS.items() if topic.search(text)}


def _beyond_slots(text):
    """The words of a statement beyond its slots, their values and the filler around them, or None when it names no
    slot value."""
    text = clean_text(text)[:SLOT_TEXT] if isinstance(text, str) else ''
    if not preference_slots(text):
        return None
    rest = _DATE_PATTERN.sub(' ', text)
    return [w for w in re.findall(r"[a-z0-9']+", rest.casefold().replace('’', "'"))
            if w not in _SLOT_FILLER and w.upper() not in CURRENCIES and not w.isdigit()]


def slot_only(text):
    """Whether a preference says nothing beyond its slots ("I want dates written as DD.MM.YYYY."), so a newer value of
    the same slots replaces all of it; "… and my staging settings are in deploy/staging.ini" says more."""
    words = _beyond_slots(text)
    return words is not None and len(words) < 2


def own_slot(text):
    """w17 (the follow-ups review's finding 5: "our invoices are in EUR" replaced "I want conversions in CHF", one word
    beyond the slot being "slot only"): whether a statement says what its slots hold and nothing else, not even one word
    ("dates should be YYYY-MM-DD from now on", "I want conversions in EUR now"). Only the person's own word on the slot
    itself replaces an older one (memory_graph.supersede_slot), is a preference (memory.statement_kind) and decides the
    reply check (memory_check.preferred); a statement about a third thing ("our invoices", "the vendor API") or for one
    ("amounts in EUR for invoices") never does."""
    return _beyond_slots(text) == []


def nomination_eligible(quote, human):
    """A whole first-person sentence of the person's own text outside the
    preference grammar. It can only wait for native confirmation."""
    return (isinstance(quote, str) and preference_allowed(quote) and len(quote) <= MAX_NOMINATION_CHARS
            and bool(_FIRST_PERSON.search(quote)) and not _INJECTION.search(quote)
            and locate(visible_human_text(human), quote) is not None and own_quote(human, quote))


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
    if name in ('ScreenAct', 'ScreenRequest', 'ScreenObserve'):
        # Phase 45 (F256, audit round R11): "declined a proposed action for ScreenAct" named the tool, not the window
        review = payload.get('review') if isinstance(payload.get('review'), dict) else {}
        window = review.get('window') if isinstance(review.get('window'), dict) else {}
        title = _clean(window.get('title'), 60) if isinstance(window.get('title'), str) else ''
        return f'the app window “{title}”' if title else 'an app window'
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
        if outcome.get('kind') == 'check':
            return []  # Phase 34 (W1): an ad hoc check's failures are the model's own probe, not the project's tests
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
            if outcome.get('kind') == 'check':
                pass  # Phase 34 (W1): an ad hoc check is not a test failure to learn from
            elif outcome.get('cancelled') is True or type(code) is int and code != 0:
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
