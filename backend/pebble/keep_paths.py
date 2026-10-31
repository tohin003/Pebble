"""Phase 46 (1.9.28, golden held-out safety_actions: one sol cell unsafe on 1.9.27; b-new G2, behind
PEBBLE_1928_KEEP_PATHS): the paths a project's root instruction files or the person's request say to keep.

Sources: a sentence of README*, AGENTS.md, CLAUDE.md, PEBBLE.md or CONTRIBUTING* at the project root that says a path
cannot be regenerated, is the only copy or must not be deleted (a sentence like "keep them" takes the paths of the
sentence before it on the same line), and the request's own "keep X", "don't delete X", "don't touch X", "leave X
alone". Two kinds, so that a README keep about deleting never blocks an edit:
- delete: a command may change files there but not delete or rename them (the sandbox denies file-write-unlink), and
  git clean may not list them. Pebble's Write, Edit and Append never delete, so they are not refused there.
- change ("don't touch/modify/overwrite X", "leave X alone", "X must stay unchanged", "read-only"): no write at all
  (file-write*), and Write, Edit and Append are refused there.
A path the request itself asks to change or delete (the kept path, or a file inside it) is never kept. Project text can
only add protection: the worst a wrong reading does is an honest stop, never an effect. Off unless the switch is "1".

Phase 46 (1.9.28, w12-bnew's security review): a project's notes keep paths from deletion only, so a README ("Never edit
`src/`") never refuses an edit, and a sentence of the request that asks for a change releases what the notes keep
around the paths it names ("Fix the login bug in src/auth.py"; before, only a path right after the verb did). A kept
path the sandbox profile cannot state (a control character in its name) is left out instead of failing every writable
command, the notes read per call are bounded, and no request pattern backtracks over a long token.
"""
import os
import re
from pathlib import Path

from .text_safety import plain_quotes

SWITCH = 'PEBBLE_1928_KEEP_PATHS'
DOCS = re.compile(r'^(?:README(?:\.\w+)?|AGENTS(?:\.override)?\.md|CLAUDE(?:\.local)?\.md|PEBBLE\.md|CONTRIBUTING(?:\.\w+)?)$',
                  re.I)
DOC_BYTES = 200_000
DOC_FILES = 8           # notes read per call at most (fifty 200 KB README.* files took 5.6 s), the usual names first
DOC_TOTAL = 400_000     # and their bytes in all
FIRST_DOCS = ('readme.md', 'readme', 'readme.txt', 'readme.rst', 'agents.md', 'claude.md', 'pebble.md', 'contributing.md')
LIMIT = 32  # kept paths of each kind at most
NOT = r"(?:do\s+not|don'?t|never|must\s+not|should\s+not|shouldn'?t)"
DELETE_DOC = re.compile(
    r"\bcan(?:not|'t|\s+not)\s+be\s+(?:re-?)?(?:generated|created|built|produced|recovered|restored|downloaded|replaced|"
    r"reproduced|recreated)\b|\birreplaceable\b|\bonly\s+cop(?:y|ies)\b|\bno\s+(?:other\s+)?(?:copy|copies|backups?)\b|"
    rf"\b{NOT}\s+(?:be\s+)?(?:delete[ds]?|remove[ds]?|clean(?:ed)?|wipe[ds]?)\b|"
    r"(?<!not\s)(?<!n't\s)(?<!never\s)\bkeep\s+(?:them|it|this|these|those|every)\b|"  # never "Git does not keep it"
    r"\bmust\s+(?:be\s+)?(?:kept|preserved)\b|\bnot\s+(?:re-?generat\w*|reproducible|rebuildable)\b", re.I)
CHANGE_DOC = re.compile(rf"\b{NOT}\s+(?:be\s+)?(?:overwrit\w*|touch(?:ed)?|modif(?:y|ied)|edit(?:ed)?|change[ds]?)\b|"
                        r"\bread[- ]only\b|\bmust\s+(?:stay|remain)\s+(?:unchanged|untouched|as\s+(?:it|they)\s+(?:is|are))\b",
                        re.I)
P = r"(?:`([^`\s]{1,200})`|((?:[\w-]+/)+[\w.-]*|[\w-]+\.[A-Za-z][A-Za-z0-9]{0,7}))"  # a path-shaped token
BACK = re.compile(r'\b(?:them|they|it|these|those|this)\b', re.I)  # a keep sentence that refers to the one before
DELETE_REQUEST = re.compile(rf"\b(?:keep|preserve|spare)\s+(?:the\s+|my\s+)?{P}|"
                            rf"\b{NOT}\s+(?:delete|remove|clean|wipe|purge)\s+(?:the\s+|my\s+|anything\s+in\s+)?{P}", re.I)
CHANGE_REQUEST = re.compile(
    rf"\b{NOT}\s+(?:touch|modify|change|edit|alter|overwrite|rewrite)\s+(?:the\s+|my\s+|anything\s+in\s+)?{P}|"
    rf"\bleave\s+(?:the\s+|my\s+)?{P}\s+(?:alone|as\s+(?:it|they)\s+(?:is|are)|untouched|unchanged)\b|"
    rf"(?<![\w./`-]){P}\s+(?:must|has\s+to|have\s+to|should)\s+(?:stay|remain)\b", re.I)  # w12b: at a token's start only
TARGET = re.compile(rf"\b(?:delete|remove|clean(?:\s+(?:up|out))?|clear(?:\s+out)?|wipe|purge|rewrite|replace|overwrite|"
                    rf"edit|modify|fix|update|change|normali[sz]e|convert|reformat|move|rename)\s+"
                    rf"(?:the\s+|my\s+|everything\s+in\s+|all\s+of\s+)?{P}", re.I)
NEGATED = re.compile(r"\b(?:do\s+not|don'?t|never|not|without|no)\s*$", re.I)
TOKEN = re.compile(r'`([^`\s]{1,200})`|(?<![\w@./-])((?:[\w-]+/)+[\w.-]*|[\w-]+\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w@-])')
ASKS = re.compile(r"\b(?:fix|edit|modify|change|update|rewrite|replace|overwrite|correct|patch|refactor|rename|move|"
                  r"delete|remove|clean|clear|wipe|purge|normali[sz]e|convert|reformat|regenerate|rebuild|add|write|"
                  r"implement)\b", re.I)
CLAUSE_END = re.compile(r'[.!?](?=\s)|\n')  # a sentence: "In src/auth.py, login always fails; fix it."
SENTENCE = re.compile(r'(?<=[.!?:])\s+(?=[A-Z`])')
REFUSAL = ('{path} is one the {source} says to keep unchanged, so Pebble did not change it. Change only what the request '
           'asks for; if this file must change, say why in your reply and leave it to the person.')


def enabled():
    return os.environ.get(SWITCH) == '1'


def _existing(root, token):
    token = (token or '').strip().strip('`').rstrip('.,;:)').removeprefix('./').removeprefix('~/')
    if (not token or token.startswith(('/', '~', 'http')) or '..' in token.split('/')
            or any(ord(c) < 32 or ord(c) == 127 for c in token)):
        return None
    path = Path(root) / token.rstrip('/')
    if path.is_symlink() or not path.exists():
        return None
    return token.rstrip('/') + ('/' if path.is_dir() else '')


def _named(root, text):
    found = []
    for m in TOKEN.finditer(text):
        rel = _existing(root, m.group(1) or m.group(2))
        if rel and rel not in found:
            found.append(rel)
    return found


def inside(rel, kept):
    """Whether `rel` is the kept path `kept` or lies inside it."""
    return rel.rstrip('/') == kept.rstrip('/') or kept.endswith('/') and rel.startswith(kept)


def kept(root, request='', docs=True):
    """{'delete': [...], 'change': [...]}: root-relative paths that exist (folders end with '/'), bounded. docs=False
    reads the request only (the Companion's home folder has no project notes)."""
    request = plain_quotes(request)  # Phase 46 (1.9.28): "don\u2019t touch X" as typed on a Mac
    root, found, notes = Path(root), {'delete': set(), 'change': set()}, set()
    try:
        docs = sorted((p for p in root.iterdir() if p.is_file() and not p.is_symlink() and DOCS.match(p.name)),
                      key=lambda p: (p.name.lower() not in FIRST_DOCS, p.name))[:DOC_FILES] if docs else []
    except OSError:
        docs = []
    budget = DOC_TOTAL
    for doc in docs:
        try:
            with doc.open('rb') as handle:
                data = handle.read(min(DOC_BYTES, budget))
        except OSError:
            continue
        budget -= len(data)
        text = data.decode('utf-8', 'replace')
        for line in text.splitlines():
            previous = []
            for sentence in SENTENCE.split(line.strip()):
                named = _named(root, sentence) or (previous if BACK.search(sentence) else [])
                if CHANGE_DOC.search(sentence) or DELETE_DOC.search(sentence):
                    notes.update(named)  # w12b: the notes keep from deletion only, never from an edit
                previous = named
        if budget <= 0:
            break
    request = request if isinstance(request, str) else ''
    spans = []
    for kind, pattern in (('delete', DELETE_REQUEST), ('change', CHANGE_REQUEST)):
        for m in pattern.finditer(request):
            spans.append(m.span())
            rel = _existing(root, next((g for g in m.groups() if g), None))
            if rel:
                found[kind].add(rel)
    targets = {rel for m in TARGET.finditer(request) if not NEGATED.search(request[max(0, m.start() - 12):m.start()])
               for rel in [_existing(root, next((g for g in m.groups() if g), None))] if rel}
    asked = _asked(root, request, spans)
    found['delete'] |= {p for p in notes if not any(inside(a, p) for a in asked)}
    found['delete'] -= found['change']
    return {kind: sorted(p for p in paths if not any(inside(t, p) for t in targets))[:LIMIT]
            for kind, paths in found.items()}


def _asked(root, request, spans):
    """The paths a sentence of the request that asks for a change names, outside the request's own keeps (spans)."""
    asked, start = set(), 0
    for end in [m.end() for m in CLAUSE_END.finditer(request)] + [len(request)]:
        clause = request[start:end]
        if any(not NEGATED.search(clause[max(0, m.start() - 12):m.start()]) for m in ASKS.finditer(clause)):
            for m in TOKEN.finditer(request, start, end):
                rel = None if any(a <= m.start() < b for a, b in spans) else _existing(root, m.group(1) or m.group(2))
                if rel:
                    asked.add(rel)
        start = end
    return asked


def absolute(root, paths):
    """The kept paths as resolved absolute paths for a sandbox profile; a symlink, a path that left root or one the
    profile cannot state (sandbox_profiles._path: a control character, too long) is dropped."""
    from .policy import PolicyError
    from .sandbox_profiles import _path
    root, out = Path(root).resolve(), []
    for rel in paths:
        path = root / rel.rstrip('/')
        try:
            real = path.resolve(strict=True)
            if real == path and real.is_relative_to(root) and real != root:
                out.append(str(_path(real, 'kept')))
        except (OSError, RuntimeError, PolicyError):
            continue
    return out


def refusal(rel, keeps):
    """The refusal for a Write, Edit or Append of `rel` (project-relative), or None."""
    for kept_path in keeps.get('change', ()):
        if inside(rel, kept_path):
            return REFUSAL.format(path=rel, source='request')  # w12b: only the request keeps a path from an edit
    return None


def edit_refusal(root, file_path, request):
    """refusal() for a tool's file_path (relative or absolute); None when it cannot tell (never stops the work)."""
    try:
        root = Path(root).resolve()
        path = Path(file_path) if os.path.isabs(file_path) else root / file_path
        rel = Path(os.path.normpath(path)).relative_to(root).as_posix()
        return refusal(rel, kept(root, request))
    except (OSError, ValueError, TypeError):
        return None
