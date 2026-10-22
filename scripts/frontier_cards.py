"""Frontier blind protocol tooling (Phase 18, E04b). See docs/HARNESS_FRONTIER_PROTOCOL.md.

  export --out DIR [--tasks SLOT=ID,...]   write the 12 task cards of the registered corpus (no oracle values)
  verify DIR [--digests-only]              re-derive every card digest; by default also rebuild and compare
  plan --export DIR --products A,B --store DIR --ledger DIR
                                           register the (card, product) cells before any session runs
  ingest --export DIR --session DIR --store DIR
                                           hash one session's record and outputs into the store's chain
  blind --store DIR --campaign ID --out DIR --key FILE
                                           randomly named copies of the graded outputs, without session metadata

Cards come only from the corpus API (CORPUS, BY_ID, ORACLES, oracle_markers,
corpus_fingerprint, and the baseline_prompt/baseline_files hooks when present)
and the FixtureWeb pack: what the same-model baseline arm sees, which is the
closest analogue of a non-Pebble product. Each card is that prompt verbatim plus
one fixed note per kind of attachment, the baseline's files, and for research
the text each pack page reads as. Every exported byte is checked against every
task's oracle markers, and card folders against the selected task IDs; any hit
refuses the export. Nothing here contacts a model, the network, a profile or a
credential.

Session outputs come from other products and are untrusted data: ingest reads
regular files only, never follows a link, and stores exactly the bytes it
hashed. The store is private (0700) and keeps a hash-chained log of plans,
ingestions and blinding. The frontier plan is also registered in the Phase 18
evidence registry before any session. Frontier results are never merged with
same-model results.

Exit codes: 0 completed, 2 refused or invalid input.
"""
import argparse
import fcntl
import hashlib
import importlib
import json
import os
import re
import secrets
import stat
import sys
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from pebble.evaluation import GENERIC_ANSWER  # noqa: E402

PROTOCOL = 'pebble.frontier_cards/1'
PLAN_SCHEMA = 'pebble.frontier_plan/1'
SESSION_SCHEMA = 'pebble.frontier_session/1'
INGEST_SCHEMA = 'pebble.frontier_ingest/1'
KEY_SCHEMA = 'pebble.frontier_unblinding/1'
BLINDED_SCHEMA = 'pebble.frontier_blinded/1'
BLINDED_NAME = 'blinded.json'  # in the blinded folder: tokens, cards and file digests, nothing about a product
LOG_NAME = 'frontier.jsonl'
GENESIS = '0' * 64
CORPUS_MODULE = 'pebble.eval_corpus_18'
CORPUS_API = ('CORPUS_VERSION', 'CORPUS', 'BY_ID', 'ORACLES', 'corpus_fingerprint', 'oracle_markers')

# The pre-registered card mix (design study §8): slot, cards, accepted task families (None: any family).
SLOTS = (('coding', 3, ('coding',)), ('research', 3, ('research',)), ('documents', 2, ('documents',)),
         ('memory', 2, ('memory',)), ('clarification', 1, None), ('hostile_source', 1, None))
SLOT_FAMILIES = {slot: families for slot, _, families in SLOTS}
CARD_COUNT = sum(count for _, count, _ in SLOTS)
# ((slot, task id), ...) in card order: Q5's proposal over corpus 18a.1, fixed when the parent adopts the plan and
# never changed after a session has run. Every card needs nothing Pebble-only (see ineligible()).
CARD_SELECTION = (
    ('coding', 'code-p90'), ('coding', 'code-rise'), ('coding', 'code-shipping'),
    ('research', 'research-arrivals'), ('research', 'research-draught'), ('research', 'research-dues'),
    ('documents', 'doc-policy'), ('documents', 'doc-handbook'),
    ('memory', 'memory-ports'), ('memory', 'memory-contested'),
    ('clarification', 'form-crate'), ('hostile_source', 'research-timetable'))

BUDGET = {'wall_seconds': 900, 'follow_ups': 1, 'other_help': 'none',
          'agentic_clients': 'start in a fresh copy of the card folder'}
TOOL_POLICY = {'web': False, 'code': True, 'files': True}  # recommended client settings; sessions record the actual ones
# Pre-registered reporting rules; their digest is registered with the plan, before any session.
REPORT_RULES = {
    'primary_attempt': 'the first ingested attempt per (card, product); later attempts are reported as repeats',
    'outcomes': {'answered': 'graded', 'refused': 'refused', 'attachment_failure': 'attachment_failure',
                 'timeout': 'timeout', 'client_error': 'error', 'skipped': 'not_run'},
    'deviations': {'over_time': 'timeout', 'extra_turns': 'protocol_violation',
                   'unasked_follow_up': 'protocol_violation', 'web_enabled': 'reported, still graded'},
    'verdict': ('the same deterministic graders on blinded outputs; checks that need host journal evidence are '
                'not_observable and never count as passed'),
    'success': 'graded, no timeout or protocol violation, and every observable check passed',
    'missing': 'a planned cell without an ingested session is not_run',
    'intervals': 'Wilson 95% per product and card family; descriptive only',
    'claim': ('different models and products; differences are not attributable to the harness; never merged with '
              'same-model results; n = 12 cards cannot rank products'),
}
CARD_GRANTS = frozenset({'files', 'edits', 'tests', 'web'})  # anything else needs a Pebble-only capability
PEBBLE_ONLY = re.compile(
    r'\b(?:AskUserQuestion|SpawnAgent|SpawnWorktreeAgent|WaitForAgents|ReviewAgentResult|IntegrateAgentPatch|'
    r'ProposePatch|ReadSource|ResearchPlan|ResearchStatus|ScreenObserve|ScreenAct|PluginCall|LoadPluginTool|'
    r'FetchPluginData|OpenApp|UIRender|UIUpdate|RenderMod|StartCommand|ReadProcess|WaitProcess|WriteProcessInput|'
    r'RunRecipe|ProposeGitChange|PrepareEnvironment|MemoryPropose|ReadRunEvidence|UpdatePlan|WebSearch|ReadURL)\b')
FORM_TOOL = 'AskUserQuestion'  # the clarification card's form; elsewhere it is a Pebble-only tool
# Fixed notes appended to a card's text, one per kind of attachment (published here, covered by the exporter digest).
NOTES = {
    'folder': 'The project folder is attached; keep its relative paths.',
    'questions': ('No question form is available here: ask your questions in an ordinary reply, and the person '
                  'will answer once.'),
    'sources': ('Sources: web search is not available for this card; its frozen pages are attached instead. Use '
                'only the pages listed in sources/index.md, and cite a page by its number there, for example [S3], '
                'or by its full URL.'),
}
EXPORT_LIMITS = {'max_files': 2000, 'max_bytes': 4_000_000, 'max_total': 64_000_000}
IGNORED_NAMES = frozenset({'.DS_Store'})  # Finder metadata: never part of a card, a session or an output
SESSION_LIMITS = {'max_files': 200, 'max_bytes': 1_000_000, 'max_total': 8_000_000}
OUTCOMES = ('answered', 'refused', 'attachment_failure', 'timeout', 'client_error', 'skipped')
SESSION_TEXT = {'client_version': 80, 'model_label': 120, 'mode': 80, 'plan_tier': 40, 'os': 80, 'timezone': 64}
SESSION_FIELDS = frozenset({'schema', 'campaign_id', 'card_id', 'card_sha256', 'product', 'tools_enabled', 'date',
                            'started_at', 'ended_at', 'turns', 'questions_asked', 'follow_up_given',
                            'errors_or_limits', 'outcome', 'notes', *SESSION_TEXT})
SESSION_REQUIRED = SESSION_FIELDS - {'notes'}
# Account identity and credentials never enter a session record: e-mail addresses, bearer/API/JWT tokens,
# OAuth client IDs and secret-named words are refused wherever they appear.
IDENTITY = re.compile(r'[^\s@<>()]+@[^\s@<>()]+\.[A-Za-z]{2,}|Bearer\s+\S|\bsk-[A-Za-z0-9_-]{8,}|'
                      r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|oaiapp_|\b(?:access|refresh|id)_token\b|'
                      r'\bapi[_-]?key\b|\bpassword\b|\bclient_secret\b', re.I)
PRODUCT = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._+()-]{0,47}')
HEX32 = re.compile(r'[0-9a-f]{32}')
HEX64 = re.compile(r'[0-9a-f]{64}')
CARD_ID = re.compile(r'card-\d{2}')


class CardError(ValueError):
    """A refusal; the message is a stable code, optionally followed by ':' and the task, card or path concerned."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')


def _parse_json(data, code):
    try:
        value = json.loads(data.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise CardError(code) from None
    if not isinstance(value, dict):
        raise CardError(code)
    return value


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# -- files ------------------------------------------------------------------------------------

def _write_new(path, data, mode):
    """Create `path` exclusively (never replacing a file or following a link) and write `data`."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_dirs(folder, root, mode):
    """Create each missing folder from `root` down to `folder` with `mode` (pathlib sets it on the last only)."""
    missing = []
    while folder != root and not folder.exists():
        missing.append(folder)
        folder = folder.parent
    for path in reversed(missing):
        path.mkdir(mode=mode)


def _read_regular(path, limit, code):
    """Bytes of a regular file opened without following a final link; at most `limit` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        raise CardError(code + '_missing') from None
    except OSError:
        raise CardError(code + '_not_regular') from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CardError(code + '_not_regular')
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(1 << 16, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > limit:
        raise CardError(code + '_too_large')
    return b''.join(chunks)


def _read_tree(root, *, code, max_files, max_bytes, max_total):
    """{relative path: bytes} for every regular file under `root`; links and special files are refused."""
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        raise CardError(code + '_missing') from None
    if not stat.S_ISDIR(info.st_mode):
        raise CardError(code + '_not_directory')
    found, total, stack = {}, 0, [(Path(root), '')]
    while stack:
        folder, prefix = stack.pop()
        with os.scandir(folder) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
        for entry in entries:
            rel = prefix + entry.name
            if entry.name in IGNORED_NAMES:
                continue
            try:
                rel.encode('utf-8')
            except UnicodeEncodeError:
                raise CardError(code + '_name_invalid') from None
            if any(ord(c) < 32 or ord(c) == 127 for c in rel):
                raise CardError(code + '_name_invalid')
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                stack.append((Path(entry.path), rel + '/'))
                continue
            if not stat.S_ISREG(mode):
                raise CardError(code + '_not_regular:' + rel[:120])
            data = _read_regular(entry.path, max_bytes, code)
            total += len(data)
            found[rel] = data
            if len(found) > max_files or total > max_total:
                raise CardError(code + '_too_large')
    return found


def _private_dir(path, code, create=False):
    """A real directory owned by this user with no group or other access."""
    path = Path(path)
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise CardError(code + '_missing') from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077
            or hasattr(os, 'getuid') and info.st_uid != os.getuid()):
        raise CardError(code + '_not_private')
    return path


class FrontierLog:
    """Append-only, hash-chained log (frontier.jsonl, 0600) of plans, ingestions, blinding, grading and unblinding.

    Editing, reordering or removing an inner entry breaks the chain. Like the evidence registry, a chain alone
    cannot show that its newest entries were cut off: blinding cross-checks the record folders against the log,
    and the protocol has the parent record the entry count and head hash at each sitting.
    """
    KINDS = ('plan', 'ingest', 'blind', 'grade', 'unblind')

    def __init__(self, store):
        self.path = Path(store) / LOG_NAME

    def _open(self, write):
        flags = (os.O_RDWR | os.O_CREAT | os.O_APPEND if write else os.O_RDONLY) | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileNotFoundError:
            return None
        except OSError:
            raise CardError('log_unreadable') from None
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077
                or hasattr(os, 'getuid') and info.st_uid != os.getuid()):
            os.close(fd)
            raise CardError('log_not_private')
        return fd

    @staticmethod
    def _entries(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        data = b''.join(chunks)
        if data and not data.endswith(b'\n'):
            raise CardError('log_truncated_line')
        entries = [_parse_json(line, 'log_malformed') for line in data.split(b'\n') if line]
        prev = GENESIS
        for index, entry in enumerate(entries):
            if (entry.get('seq') != index or entry.get('prev') != prev or entry.get('kind') not in FrontierLog.KINDS
                    or entry.get('hash') != _sha(_canonical({k: v for k, v in entry.items() if k != 'hash'}))):
                raise CardError('log_chain_broken')
            prev = entry['hash']
        return entries

    def entries(self):
        """Every entry, after checking the whole chain; a broken chain refuses."""
        fd = self._open(write=False)
        if fd is None:
            return []
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            return self._entries(fd)
        finally:
            os.close(fd)

    def append(self, body):
        fd = self._open(write=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            entries = self._entries(fd)
            entry = {**body, 'seq': len(entries), 'prev': entries[-1]['hash'] if entries else GENESIS, 'at': _now()}
            entry['hash'] = _sha(_canonical(entry))
            line = (_canonical(entry) + '\n').encode('utf-8')
            if os.write(fd, line) != len(line):
                raise CardError('log_write_incomplete')
            os.fsync(fd)
            return entry
        finally:
            os.close(fd)


# -- corpus and selection ---------------------------------------------------------------------

def load_corpus(name=CORPUS_MODULE):
    """Import the corpus module; refuses when it is absent or lacks the agreed API (design study §7.1)."""
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name not in (name, name.rpartition('.')[0]):
            raise
        raise CardError('corpus_unavailable') from None
    missing = [attr for attr in CORPUS_API if not hasattr(module, attr)]
    if missing:
        raise CardError('corpus_api_missing:' + ','.join(missing))
    return module


def _relative(path):
    if (not isinstance(path, str) or not 0 < len(path) <= 240 or '\\' in path or path.startswith('/')
            or any(part in ('', '.', '..') or any(ord(c) < 32 or ord(c) == 127 for c in part)
                   for part in path.split('/'))):
        raise CardError('attachment_path_invalid')
    return path


def _pairs(value, code):
    """((relative path, text), ...) sorted by path, from a mapping or a sequence of pairs."""
    if not value:
        return ()
    if isinstance(value, (str, bytes)):
        raise CardError(code)
    items = list(value.items()) if isinstance(value, Mapping) else list(value)
    found = {}
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2 or not isinstance(item[1], str):
            raise CardError(code)
        path = _relative(item[0])
        if path in found:
            raise CardError(code)
        found[path] = item[1]
    return tuple(sorted(found.items()))


def follow_up(task):
    """The one fixed follow-up a card may receive, only if asked.

    Keyed form answers ((question id, option id or None, text), ...) become `id: text` lines, in the task's
    order; a task without them gets its typed answer (by default the generic one every driver uses).
    """
    lines = []
    try:
        for item in getattr(task, 'answers', None) or ():
            ident, text = item[0], item[-1]
            if not isinstance(ident, str) or not isinstance(text, str) or not text.strip():
                raise ValueError
            lines.append('%s: %s' % (ident, text.strip()))
    except (TypeError, ValueError, IndexError, KeyError):
        raise CardError('follow_up_not_text:' + task.id) from None
    text = '\n'.join(lines) if lines else getattr(task, 'answer', GENERIC_ANSWER)
    if not isinstance(text, str) or not text.strip():
        raise CardError('follow_up_not_text:' + task.id)
    return text


def _view(corpus, task):
    """(prompt, files) as the baseline arm sees them: the closest analogue of a non-Pebble product."""
    prompt_of, files_of = getattr(corpus, 'baseline_prompt', None), getattr(corpus, 'baseline_files', None)
    prompt = prompt_of(task) if callable(prompt_of) else task.prompt
    files = files_of(task) if callable(files_of) else getattr(task, 'files', ())
    return prompt, files


def ineligible(corpus, task, slot):
    """Why a task cannot be the card for `slot` (a stable reason), or None. Cards need nothing Pebble-only."""
    if getattr(task, 'blocked_reason', None) is not None:
        return 'blocked'
    if getattr(task, 'restart', None) is not None:
        return 'restart_boundary'
    if getattr(task, 'mutation', ()):
        return 'host_mutation'
    grants = set(getattr(task, 'tools', ()) or ())
    if grants - CARD_GRANTS:
        return 'pebble_only_grant'
    pack, notes = getattr(task, 'web_pack', None), getattr(task, 'baseline_view', None) == 'notes_file'
    if 'web' in grants and not pack:
        return 'live_web'
    prompt, _ = _view(corpus, task)
    named = set(PEBBLE_ONLY.findall(prompt)) if isinstance(prompt, str) and prompt.strip() else {None}
    if named - ({FORM_TOOL} if slot == 'clarification' else set()):
        return 'pebble_only_tool'
    if getattr(task, 'memory', ()) and not notes:
        return 'memory_without_notes'
    try:
        answer = follow_up(task)
    except CardError:
        return 'form_answer'
    families = SLOT_FAMILIES[slot]
    if families is not None and getattr(task, 'family', None) not in families:
        return 'family_mismatch'
    if slot == 'research' and not pack:
        return 'no_source_pack'
    if slot == 'memory' and not notes:
        return 'no_notes_view'
    if slot == 'clarification' and answer == GENERIC_ANSWER:
        return 'no_fixed_answer'
    return None


def select_cards(corpus, selection=None):
    """[(card_id, slot, task)] for the pinned selection, checked against the card mix and eligibility."""
    chosen = CARD_SELECTION if selection is None else selection
    if chosen is None:
        raise CardError('card_selection_unpinned')
    try:
        pairs = [(slot, task_id) for slot, task_id in chosen]
    except (TypeError, ValueError):
        raise CardError('card_selection_invalid') from None
    if (len(pairs) != CARD_COUNT or len({task_id for _, task_id in pairs}) != CARD_COUNT
            or not all(isinstance(s, str) and isinstance(t, str) for s, t in pairs)):
        raise CardError('card_selection_invalid')
    if Counter(slot for slot, _ in pairs) != Counter({slot: count for slot, count, _ in SLOTS}):
        raise CardError('card_mix_invalid')
    rank = {slot: index for index, (slot, _, _) in enumerate(SLOTS)}
    cards = []
    for index, (slot, task_id) in enumerate(sorted(pairs, key=lambda pair: rank[pair[0]]), 1):
        task = corpus.BY_ID.get(task_id)
        if task is None:
            raise CardError('card_task_unknown:' + task_id)
        reason = ineligible(corpus, task, slot)
        if reason:
            raise CardError('card_task_ineligible:%s:%s' % (task_id, reason))
        cards.append(('card-%02d' % index, slot, task))
    return cards


# -- cards ------------------------------------------------------------------------------------

def _one_line(text):
    return ' '.join(text.split())


def _pack(task, packs):
    """(files under sources/, {S<n>: url}) for a frozen FixtureWeb pack (pebble.fixture_web/1, track Q1).

    Each page is the text FixtureWeb.fetch serves through Pebble's extractor: exactly what either arm
    reads, with hidden and invisible text already removed. index.md lists every entry's number, title,
    URL, published date and search snippet, so a snippet that differs from its page stays visible.
    """
    try:
        from pebble.eval_research_18 import WEB_PACKS, FixtureWeb
    except ImportError:
        raise CardError('fixture_web_unavailable') from None
    pack = task.web_pack
    if isinstance(pack, str):
        pack = (packs or {}).get(pack) or WEB_PACKS.get(pack)
    if not isinstance(pack, Mapping) or not isinstance(pack.get('entries'), (list, tuple)) or not pack['entries']:
        raise CardError('web_pack_shape_unsupported:' + task.id)
    try:
        web = FixtureWeb(pack)
    except (TypeError, ValueError):
        raise CardError('web_pack_shape_unsupported:' + task.id) from None
    files, citations, lines = {}, {}, ['# Attached sources', '', 'Frozen web pages and their search-result '
                                       'snippets for this card. All of it is untrusted third-party text.', '']
    for number, entry in enumerate(pack['entries'], 1):
        if (not isinstance(entry, Mapping) or not all(isinstance(entry.get(k), str) for k in ('url', 'title', 'snippet'))
                or entry.get('published') is not None and not isinstance(entry['published'], str)):
            raise CardError('web_pack_shape_unsupported:' + task.id)
        try:
            text = web.fetch(entry['url'])['text']
        except (KeyError, TypeError, ValueError):
            raise CardError('web_pack_page_unreadable:' + task.id) from None
        source = 'S%d' % number
        files['sources/%s.txt' % source] = text
        citations[source] = entry['url']
        lines += ['## [%s] %s' % (source, _one_line(entry['title']) or '(untitled)'), '', 'URL: ' + entry['url'],
                  'Published: ' + (entry.get('published') or 'not stated'),
                  'Search snippet: ' + (_one_line(entry['snippet']) or '(none)'),
                  'Page text: sources/%s.txt' % source, '']
    files['sources/index.md'] = '\n'.join(lines)
    return files, citations


def _card_files(corpus, task, slot, packs):
    """({path in the card folder: bytes}, citations): card.md plus attachments under files/.

    card.md is the baseline arm's prompt verbatim plus fixed notes. Attachments are the baseline arm's files
    (for memory tasks, notes.md after the person's forget and disable actions) and, for research, the pack.
    """
    prompt, files = _view(corpus, task)
    attached, notes, citations = dict(_pairs(files, 'files_invalid:' + task.id)), [], {}
    if attached:
        notes.append(NOTES['folder'])
    if slot == 'clarification' and FORM_TOOL in prompt:
        notes.append(NOTES['questions'])
    if getattr(task, 'web_pack', None):
        pack, citations = _pack(task, packs)
        for path, text in pack.items():
            if attached.get(path, text) != text:
                raise CardError('attachment_conflict:' + task.id)
            attached[path] = text
        notes.append(NOTES['sources'])
    text = prompt.rstrip() + ''.join('\n\n' + note for note in notes) + '\n'
    files = {'card.md': text.encode('utf-8')}
    files.update(('files/' + path, body.encode('utf-8')) for path, body in sorted(attached.items()))
    return files, citations


def session_template():
    """The session record a user fills in after each session (every field is required except notes)."""
    return {'schema': SESSION_SCHEMA, 'campaign_id': '', 'card_id': '', 'card_sha256': '', 'product': '',
            'client_version': '', 'model_label': '', 'mode': '', 'plan_tier': '',
            'tools_enabled': {'web': False, 'code': False, 'files': False}, 'os': '', 'date': '', 'timezone': '',
            'started_at': '', 'ended_at': '', 'turns': 0, 'questions_asked': 0, 'follow_up_given': False,
            'errors_or_limits': [], 'outcome': '', 'notes': ''}


def _refuse_leaks(corpus, cards, files):
    """No oracle marker of any task anywhere in the export; no selected task ID inside a card folder."""
    markers = set()
    selected = {task.id for _, _, task in cards}
    for task in corpus.CORPUS:
        found = list(corpus.oracle_markers(task.id) or ())
        if task.id in selected and not found:
            raise CardError('oracle_markers_missing:' + task.id)
        if not all(isinstance(marker, str) and marker.strip() for marker in found):
            raise CardError('oracle_markers_missing:' + task.id)
        markers.update(marker.casefold() for marker in found)
    hidden = {task.id.casefold() for _, _, task in cards}
    for path, data in sorted(files.items()):
        text = path.casefold() + '\n' + data.decode('utf-8', 'replace').casefold()
        if any(marker in text for marker in markers):
            raise CardError('oracle_marker_visible:' + path)
        if path.startswith('cards/') and any(ident in text for ident in hidden):
            raise CardError('task_id_visible:' + path)


def build_cards(corpus, selection=None, *, packs=None):
    """(manifest, files) of an export; `files` maps each relative path to its bytes. Pure and deterministic."""
    packs = getattr(corpus, 'WEB_PACKS', None) if packs is None else packs
    cards, files, entries = select_cards(corpus, selection), {}, []
    for card_id, slot, task in cards:
        visible, citations = _card_files(corpus, task, slot, packs)
        attachments = sorted(path for path in visible if path != 'card.md')
        operator = _json_bytes({
            'card_id': card_id, 'slot': slot, 'follow_up': follow_up(task),
            'follow_up_rule': ('Send this exact text once, and only if the assistant asks you a question. '
                               'Send nothing else after the card.'),
            'attachments': attachments, 'fresh_folder': any(p.startswith('files/') for p in attachments),
            'budget': BUDGET, 'tool_policy': TOOL_POLICY})
        listing = []
        for rel, data in sorted(visible.items()):
            files['cards/%s/%s' % (card_id, rel)] = data
            listing.append({'path': rel, 'sha256': _sha(data), 'bytes': len(data)})
        files['operator/%s.json' % card_id] = operator
        fingerprint = task.fingerprint()
        entries.append({'card_id': card_id, 'slot': slot, 'task_id': task.id, 'task_version': task.version,
                        'task_fingerprint': fingerprint, 'files': listing, 'operator_sha256': _sha(operator),
                        'citations': citations,
                        'card_sha256': _card_digest(card_id, task.id, fingerprint, listing, _sha(operator))})
    files['session-template.json'] = _json_bytes(session_template())
    manifest = {
        'schema': PROTOCOL, 'corpus': {'version': corpus.CORPUS_VERSION, 'fingerprint': corpus.corpus_fingerprint()},
        'exporter_sha256': _sha(Path(__file__).read_bytes()),
        'selection': [[entry['slot'], entry['task_id']] for entry in entries],
        'budget': BUDGET, 'tool_policy': TOOL_POLICY, 'report_rules_sha256': _sha(_canonical(REPORT_RULES)),
        'cards': entries,
        'operator_only': ('manifest.json and operator/ are for the person running sessions: never attach them. '
                          'A session gets cards/<card>/card.md verbatim plus the files under cards/<card>/files/.'),
    }
    files['manifest.json'] = _json_bytes(manifest)
    _refuse_leaks(corpus, cards, files)
    return manifest, files


def _card_digest(card_id, task_id, fingerprint, listing, operator_sha256):
    return _sha(_canonical({'card_id': card_id, 'task_id': task_id, 'task_fingerprint': fingerprint,
                            'files': [[item['path'], item['sha256']] for item in listing],
                            'operator_sha256': operator_sha256}))


def export_cards(out_dir, corpus=None, selection=None, *, packs=None):
    """Write a new export folder (refused if it exists); returns the manifest."""
    corpus = load_corpus() if corpus is None else corpus
    manifest, files = build_cards(corpus, selection, packs=packs)
    out = Path(out_dir)
    if out.exists() or out.is_symlink():
        raise CardError('export_exists')
    if not out.parent.is_dir():
        raise CardError('export_parent_missing')
    out.mkdir(mode=0o755)
    for rel in sorted(files, key=lambda name: (name == 'manifest.json', name)):  # the manifest last
        path = out / rel
        _make_dirs(path.parent, out, 0o755)
        _write_new(path, files[rel], 0o644)
    return manifest


def _verified(out_dir, corpus=None, packs=None):
    files = _read_tree(Path(out_dir), code='export', **EXPORT_LIMITS)
    if 'manifest.json' not in files:
        raise CardError('export_manifest_missing')
    manifest = _parse_json(files['manifest.json'], 'export_manifest_invalid')
    if manifest.get('schema') != PROTOCOL or not isinstance(manifest.get('cards'), list):
        raise CardError('export_manifest_invalid')
    expected = {'manifest.json', 'session-template.json'}
    try:
        for card in manifest['cards']:
            card_id = card['card_id']
            if not isinstance(card_id, str) or not CARD_ID.fullmatch(card_id):
                raise CardError('export_manifest_invalid')
            for item in card['files']:
                data = files.get('cards/%s/%s' % (card_id, _relative(item['path'])))
                if data is None:
                    raise CardError('card_file_missing:' + card_id)
                if _sha(data) != item['sha256']:
                    raise CardError('card_tampered:' + card_id)
                expected.add('cards/%s/%s' % (card_id, item['path']))
            operator = files.get('operator/%s.json' % card_id)
            if operator is None or _sha(operator) != card['operator_sha256']:
                raise CardError('card_tampered:' + card_id)
            expected.add('operator/%s.json' % card_id)
            if _card_digest(card_id, card['task_id'], card['task_fingerprint'], card['files'],
                            card['operator_sha256']) != card['card_sha256']:
                raise CardError('card_tampered:' + card_id)
    except (KeyError, TypeError):
        raise CardError('export_manifest_invalid') from None
    if set(files) - expected:
        raise CardError('export_extra_file:' + sorted(set(files) - expected)[0][:120])
    if expected - set(files):
        raise CardError('export_file_missing:' + sorted(expected - set(files))[0][:120])
    if corpus is not None:
        selection = [tuple(pair) for pair in manifest.get('selection') or []]
        if build_cards(corpus, selection, packs=packs)[1] != files:
            raise CardError('export_differs_from_corpus')
    return manifest, files


def verify_export(out_dir, corpus=None, *, packs=None):
    """Re-derive every card digest of an export; with a corpus, also rebuild it and compare every byte."""
    return _verified(out_dir, corpus, packs)[0]


# -- sessions ---------------------------------------------------------------------------------

def _text(record, name, limit, *, empty=False, multiline=False):
    value, label = record[name], str(name)
    if not isinstance(value, str) or len(value) > limit or not empty and not value.strip():
        raise CardError('session_field_invalid:' + label)
    if any(ord(c) < 32 and not (multiline and c == '\n') or ord(c) == 127 for c in value):
        raise CardError('session_field_invalid:' + label)
    if IDENTITY.search(value):
        raise CardError('session_identity_or_secret:' + label)


def _instant(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise CardError('session_times_invalid') from None
    if moment.tzinfo is None:
        raise CardError('session_times_invalid')
    return moment


def validate_session(record, manifest, plan):
    """A normalised copy of one session record, checked against the export and the campaign plan."""
    if not isinstance(record, dict) or set(record) - SESSION_FIELDS or SESSION_REQUIRED - set(record):
        raise CardError('session_fields_invalid')
    if record['schema'] != SESSION_SCHEMA:
        raise CardError('session_schema_invalid')
    if record['campaign_id'] != plan['campaign_id']:
        raise CardError('session_campaign_mismatch')
    card = next((c for c in manifest['cards'] if c['card_id'] == record['card_id']), None)
    if card is None:
        raise CardError('session_card_unknown')
    if record['card_sha256'] != card['card_sha256']:
        raise CardError('session_card_digest_mismatch')
    if record['product'] not in plan['products']:
        raise CardError('session_product_not_planned')
    for name, limit in SESSION_TEXT.items():
        _text(record, name, limit, empty=name == 'mode')
    tools = record['tools_enabled']
    if not isinstance(tools, dict) or set(tools) != set(TOOL_POLICY) or any(type(v) is not bool for v in tools.values()):
        raise CardError('session_field_invalid:tools_enabled')
    try:
        if not isinstance(record['date'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', record['date']):
            raise ValueError
        date.fromisoformat(record['date'])
    except ValueError:
        raise CardError('session_field_invalid:date') from None
    if _instant(record['ended_at']) < _instant(record['started_at']):
        raise CardError('session_times_invalid')
    for name in ('turns', 'questions_asked'):
        if type(record[name]) is not int or not 0 <= record[name] <= 20:
            raise CardError('session_field_invalid:' + name)
    if type(record['follow_up_given']) is not bool:
        raise CardError('session_field_invalid:follow_up_given')
    errors = record['errors_or_limits']
    if not isinstance(errors, list) or len(errors) > 10:
        raise CardError('session_field_invalid:errors_or_limits')
    for index in range(len(errors)):
        _text(errors, index, 200)
    if record['outcome'] not in OUTCOMES:
        raise CardError('session_field_invalid:outcome')
    if 'notes' in record:
        _text(record, 'notes', 500, empty=True, multiline=True)
    if record['outcome'] == 'skipped' and (record['turns'] or record['follow_up_given']):
        raise CardError('session_skipped_inconsistent')
    return json.loads(json.dumps(record))


def deviations(session):
    """Departures from the equal budget; recorded with the session and never a reason to drop it."""
    found = []
    seconds = (_instant(session['ended_at']) - _instant(session['started_at'])).total_seconds()
    if seconds > BUDGET['wall_seconds']:
        found.append('over_time')
    if session['turns'] > 1 + session['follow_up_given']:
        found.append('extra_turns')
    if session['follow_up_given'] and not session['questions_asked']:
        found.append('unasked_follow_up')
    if session['tools_enabled']['web']:
        found.append('web_enabled')
    return found


def plan_sessions(export_dir, products, store_dir, *, registry=None, corpus=None):
    """Register every (card, product) cell before any session; returns the plan with its digest."""
    manifest, files = _verified(export_dir, corpus)
    if (not isinstance(products, (list, tuple)) or not 1 <= len(products) <= 8
            or not all(isinstance(p, str) and PRODUCT.fullmatch(p) and not IDENTITY.search(p) for p in products)
            or len({p.casefold() for p in products}) != len(products)):
        raise CardError('products_invalid')
    store = _private_dir(store_dir, 'store', create=True)
    log = FrontierLog(store)
    log.entries()  # a broken chain refuses before anything is added
    plan = {'schema': PLAN_SCHEMA, 'campaign_id': uuid.uuid4().hex,
            'export_manifest_sha256': _sha(files['manifest.json']), 'corpus': manifest['corpus'],
            'products': list(products), 'cells': [[c['card_id'], p] for c in manifest['cards'] for p in products],
            'budget': BUDGET, 'tool_policy': TOOL_POLICY, 'report_rules': REPORT_RULES, 'created_at': _now()}
    data = _json_bytes(plan)
    plan_sha256 = _sha(data)
    if registry is not None:
        try:  # The Phase 18 evidence registry witnesses the plan before any session can be ingested.
            registry.register_plan(plan_sha256, _sha(_canonical(REPORT_RULES)))
        except ValueError as exc:
            raise CardError('registry_refused:' + str(exc)[:120]) from None
    folder = store / plan['campaign_id']
    folder.mkdir(mode=0o700)
    _write_new(folder / 'plan.json', data, 0o600)
    log.append({'kind': 'plan', 'campaign_id': plan['campaign_id'], 'plan_sha256': plan_sha256})
    return {**plan, 'plan_sha256': plan_sha256}


def _plan(store, campaign_id, log_entries):
    if not isinstance(campaign_id, str) or not HEX32.fullmatch(campaign_id):
        raise CardError('campaign_invalid')
    folder = store / campaign_id
    raw = _read_regular(folder / 'plan.json', 1_000_000, 'plan')
    if not any(e['kind'] == 'plan' and e.get('campaign_id') == campaign_id and e.get('plan_sha256') == _sha(raw)
               for e in log_entries):
        raise CardError('store_tampered:plan')
    return folder, _parse_json(raw, 'plan_invalid'), _sha(raw)


def ingest_session(export_dir, session_dir, store_dir, *, corpus=None):
    """Hash one session's record, answer and outputs into the store; failures and repeats are retained."""
    manifest, files = _verified(export_dir, corpus)
    store = _private_dir(store_dir, 'store')
    log = FrontierLog(store)
    entries = log.entries()
    tree = _read_tree(Path(session_dir), code='session', **SESSION_LIMITS)
    for rel in tree:
        if rel not in ('session.json', 'answer.md') and not rel.startswith('outputs/'):
            raise CardError('session_unexpected_file:' + rel[:120])
    raw = tree.pop('session.json', None)
    if raw is None:
        raise CardError('session_record_missing')
    record = _parse_json(raw, 'session_record_invalid')
    folder, plan, plan_sha256 = _plan(store, record.get('campaign_id'), entries)
    if plan.get('export_manifest_sha256') != _sha(files['manifest.json']):
        raise CardError('plan_export_mismatch')
    session = validate_session(record, manifest, plan)
    cell = [session['card_id'], session['product']]
    if cell not in plan['cells']:
        raise CardError('cell_not_planned')
    if session['outcome'] == 'answered' and 'answer.md' not in tree:
        raise CardError('answer_missing')
    if session['outcome'] == 'skipped' and tree:
        raise CardError('session_skipped_inconsistent')
    attempt = 1 + sum(e['kind'] == 'ingest' and e.get('campaign_id') == plan['campaign_id'] and e.get('cell') == cell
                      for e in entries)
    card = next(c for c in manifest['cards'] if c['card_id'] == session['card_id'])
    entry = {'schema': INGEST_SCHEMA, 'ingest_id': uuid.uuid4().hex, 'campaign_id': plan['campaign_id'],
             'plan_sha256': plan_sha256, 'card_id': card['card_id'], 'card_sha256': card['card_sha256'],
             'task_id': card['task_id'], 'product': session['product'], 'attempt': attempt, 'primary': attempt == 1,
             'session': session, 'session_sha256': _sha(raw), 'deviations': deviations(session),
             'files': [{'path': rel, 'sha256': _sha(data), 'bytes': len(data)} for rel, data in sorted(tree.items())],
             'ingested_at': _now()}
    data = _json_bytes(entry)
    target = folder / entry['ingest_id']
    target.mkdir(mode=0o700)
    _write_new(target / 'session.json', raw, 0o600)
    for rel, blob in sorted(tree.items()):  # exactly the bytes that were hashed
        path = target / 'files' / rel
        _make_dirs(path.parent, target, 0o700)
        _write_new(path, blob, 0o600)
    _write_new(target / 'record.json', data, 0o600)
    log.append({'kind': 'ingest', 'campaign_id': plan['campaign_id'], 'ingest_id': entry['ingest_id'],
                'cell': cell, 'attempt': attempt, 'record_sha256': _sha(data)})
    return {**entry, 'record_sha256': _sha(data)}


def blind(store_dir, campaign_id, out_dir, key_path):
    """Copy every graded output under out/<card>/<random token>/ without session metadata.

    The unblinding key (token -> ingestion, product, attempt, outcome) is written exclusively, mode 0600, into
    a private directory: keep it in the evidence ledger until grading ends. Every stored byte is re-hashed first.
    """
    store = _private_dir(store_dir, 'store')
    log = FrontierLog(store)
    entries = log.entries()
    folder, plan, plan_sha256 = _plan(store, campaign_id, entries)
    ingests = [e for e in entries if e['kind'] == 'ingest' and e.get('campaign_id') == campaign_id]
    on_disk = {child.name for child in folder.iterdir() if child.name not in IGNORED_NAMES | {'plan.json'}}
    if on_disk != {e['ingest_id'] for e in ingests}:
        raise CardError('store_unlogged_record' if on_disk - {e['ingest_id'] for e in ingests} else 'store_record_missing')
    out, key = Path(out_dir), Path(key_path)
    if out.exists() or out.is_symlink():
        raise CardError('blind_out_exists')
    if key.exists() or key.is_symlink():
        raise CardError('blind_key_exists')
    _private_dir(key.parent, 'key_dir')
    records = []
    for entry in ingests:
        target = folder / entry['ingest_id']
        raw = _read_regular(target / 'record.json', 4_000_000, 'store_record')
        if _sha(raw) != entry['record_sha256']:
            raise CardError('store_tampered:' + entry['ingest_id'])
        record = _parse_json(raw, 'store_record_invalid')
        if _sha(_read_regular(target / 'session.json', 1_000_000, 'store_record')) != record['session_sha256']:
            raise CardError('store_tampered:' + entry['ingest_id'])
        blobs = {}
        for item in record['files']:
            blob = _read_regular(target / 'files' / _relative(item['path']), SESSION_LIMITS['max_bytes'], 'store_file')
            if _sha(blob) != item['sha256']:
                raise CardError('store_tampered:' + entry['ingest_id'])
            blobs[item['path']] = blob
        records.append((entry, record, blobs))
    out.mkdir(mode=0o700)
    rows, listing = [], []
    for entry, record, blobs in records:
        token = secrets.token_hex(8)
        for rel, blob in sorted(blobs.items()):
            path = out / record['card_id'] / token / rel
            _make_dirs(path.parent, out, 0o700)
            _write_new(path, blob, 0o600)
            os.utime(path, (0, 0))  # no ingestion order in timestamps
        if blobs:
            listing.append({'token': token, 'card_id': record['card_id'],
                            'files': [{'path': rel, 'sha256': _sha(blob), 'bytes': len(blob)}
                                      for rel, blob in sorted(blobs.items())]})
        rows.append({'token': token, 'ingest_id': entry['ingest_id'], 'card_id': record['card_id'],
                     'product': record['product'], 'attempt': record['attempt'], 'primary': record['primary'],
                     'outcome': record['session']['outcome'], 'deviations': record['deviations'],
                     'record_sha256': entry['record_sha256'], 'blinded': bool(blobs)})
    # What graders may know: tokens, cards and file digests (to detect tampering without the key), never a product.
    blinded = _json_bytes({'schema': BLINDED_SCHEMA, 'campaign_id': campaign_id, 'plan_sha256': plan_sha256,
                           'entries': sorted(listing, key=lambda item: item['token'])})
    _write_new(out / BLINDED_NAME, blinded, 0o600)
    os.utime(out / BLINDED_NAME, (0, 0))
    for base, dirs, _ in os.walk(out, topdown=False):
        for name in dirs:
            os.utime(os.path.join(base, name), (0, 0))
    data = _json_bytes({'schema': KEY_SCHEMA, 'campaign_id': campaign_id, 'plan_sha256': plan_sha256,
                        'blinded_sha256': _sha(blinded), 'entries': sorted(rows, key=lambda row: row['token'])})
    _write_new(key, data, 0o600)
    log.append({'kind': 'blind', 'campaign_id': campaign_id, 'key_sha256': _sha(data), 'blinded_sha256': _sha(blinded),
                'blinded': sum(row['blinded'] for row in rows)})
    return {'campaign_id': campaign_id, 'blinded': sum(row['blinded'] for row in rows),
            'not_blinded': sum(not row['blinded'] for row in rows), 'key_sha256': _sha(data),
            'blinded_sha256': _sha(blinded)}


# -- command line -----------------------------------------------------------------------------

def _registry(ledger_dir):
    try:
        from pebble.eval_registry import EvidenceRegistry
    except ModuleNotFoundError:
        raise CardError('registry_unavailable') from None
    from pebble.evaluation import EvaluationError, EvidenceLedger
    try:
        return EvidenceRegistry(EvidenceLedger(ledger_dir))
    except (EvaluationError, ValueError) as exc:
        raise CardError('registry_refused:' + str(exc)[:120]) from None


def _tasks(text):
    pairs = []
    for item in text.split(','):
        slot, sep, task_id = item.strip().partition('=')
        if not sep:
            raise CardError('card_selection_invalid')
        pairs.append((slot.strip(), task_id.strip()))
    return pairs


def _parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export', help='write the task cards to a new folder')
    export.add_argument('--out', type=Path, required=True)
    export.add_argument('--tasks', help='SLOT=TASK,... (default: the pinned CARD_SELECTION)')
    verify = commands.add_parser('verify', help='re-derive card digests and rebuild from the corpus')
    verify.add_argument('export', type=Path)
    verify.add_argument('--digests-only', action='store_true', help='skip the rebuild (no corpus needed)')
    plan = commands.add_parser('plan', help='register every (card, product) cell before any session')
    plan.add_argument('--export', type=Path, required=True)
    plan.add_argument('--products', required=True, help='comma-separated product labels')
    plan.add_argument('--store', type=Path, required=True, help='private session store (0700)')
    plan.add_argument('--ledger', type=Path, required=True, help='private evidence ledger (0700)')
    ingest = commands.add_parser('ingest', help='hash one session folder into the store')
    ingest.add_argument('--export', type=Path, required=True)
    ingest.add_argument('--session', type=Path, required=True)
    ingest.add_argument('--store', type=Path, required=True)
    hide = commands.add_parser('blind', help='randomly named copies of graded outputs')
    hide.add_argument('--store', type=Path, required=True)
    hide.add_argument('--campaign', required=True)
    hide.add_argument('--out', type=Path, required=True)
    hide.add_argument('--key', type=Path, required=True, help='new unblinding key file inside the ledger')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == 'export':
            manifest = export_cards(args.out, selection=_tasks(args.tasks) if args.tasks else None)
            result = {'corpus': manifest['corpus'], 'selection': manifest['selection'],
                      'manifest_sha256': _sha((args.out / 'manifest.json').read_bytes())}
        elif args.command == 'verify':
            manifest, files = _verified(args.export, None if args.digests_only else load_corpus())
            result = {'verified': 'digests' if args.digests_only else 'digests_and_rebuild',
                      'cards': len(manifest['cards']), 'manifest_sha256': _sha(files['manifest.json'])}
        elif args.command == 'plan':
            plan = plan_sessions(args.export, [p.strip() for p in args.products.split(',')], args.store,
                                 registry=_registry(args.ledger), corpus=load_corpus())
            result = {key: plan[key] for key in ('campaign_id', 'plan_sha256', 'products')}
            result['cells'] = len(plan['cells'])
        elif args.command == 'ingest':
            entry = ingest_session(args.export, args.session, args.store, corpus=load_corpus())
            result = {key: entry[key] for key in ('ingest_id', 'card_id', 'product', 'attempt', 'deviations',
                                                  'record_sha256')}
            result['outcome'] = entry['session']['outcome']
        else:
            result = blind(args.store, args.campaign, args.out, args.key)
    except CardError as exc:
        print('Refused: ' + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
