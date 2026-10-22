"""Readable copies in ~/Documents/Pebble, what deleting them means, and the Data & storage page (Phase 22).

DataServices(app) is one App's storage service (parent patch: App constructs, starts, stops it and merges
its methods() into dispatch). On a flat --home it is inert: no thread, and its routes say so. On a
structured home (~/.pebble) a polling thread (no FSEvents, no new dependency) runs, at launch and then
every POLL seconds:

1. settings.json -> the settings in effect (settings_file.SettingsSync; widening changes wait for the app);
2. PEBBLE.md -> one profile-layer memory with origin 'user', exactly what Settings -> Memory -> Your
   preferences saves, so it reaches the model by the existing recall path and only while that layer is on;
3. mcp.json, a read-only view of the connectors (no secrets);
4. Recently deleted: conversations hidden for TRASH_DAYS are deleted for good through App.delete_chat;
5. the Documents copies: first reconcile what the person deleted, then refresh what changed.

The copies are derived views written atomically: user and assistant messages only (never a held draft, a
private checkpoint, a tool payload or a quiz answer key), memories without working-state content, and
every text and file name passed through scrub(), which removes secret-shaped text (best effort). Nothing in a
copy is ever read back as an instruction or as data. Only deletions are read back, from markers Pebble wrote,
and only when they are certain (Phase 24, V1-004 and V1-014):
- a Conversations file (found by its recorded name, or by the chat id on its first line) that is gone on two
  passes at least CONFIRM_SECONDS apart moves its conversation to Recently deleted (restorable for 30 days). A file
  that is present but unreadable, empty or an iCloud stand-in still counts as present, and is never read;
- a Memory entry (found by its `pebble:memory id=` line) that was in the version of the file the person saved (its
  `pebble:memory-version` line) and is gone on two passes CONFIRM_SECONDS apart is forgotten through the host's
  forget path, which leaves a tombstone. An editor saving an older copy forgets nothing that was added since. More
  than FORGET_WITHOUT_ASKING entries of one layer, or a whole layer file, wait for the person's confirmation on the
  Data & storage page;
- an Attachments, Artifacts or Exports copy that disappears is simply not written again;
- a folder that disappears, a moved or reset Documents location, an iCloud placeholder (".name.icloud") or a
  file Pebble cannot recognise never counts as a deletion.
The copies' folders (current and earlier ones) are protected roots (data_home.protect_mirror): Pebble's own tools
can neither read nor change them, so a tool can never cause a trash or a forget (V1-003). Deleting a conversation
for good removes Pebble's own copies of it in every copies folder it ever wrote (forget_chat, V1-016).
"""

import ctypes
import ctypes.util
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import data_home, settings_file

FORMAT = 'pebble-mirror/2'         # Phase 24: chat ids on copies, retired roots, two-pass deletions, memory versions
READ_FORMATS = ('pebble-mirror/1', FORMAT)
MANIFEST = 'mirror.json'           # in ~/.pebble/runtime
ROOT_MARKER = '.pebble-mirror.json'
POLL = 10.0
PEBBLE_MD_ID = 'pebble-md'
PEBBLE_MD_LIMIT = 4000
MAX_MEMORY = 2000
COPY_BUDGET = 64 << 20             # bytes of attachment and export copies per pass
LOG_TAIL = 256 << 10
MAX_EVENTS = 40
MAX_TEXT_FILE = 8 << 20
MAX_RETIRED = 8                    # earlier copies folders remembered, so deletions still remove their copies
# V1-004 and V1-014: a deletion read from Documents counts only when it is certain. The background poll needs the
# absence on two passes at least CONFIRM_SECONDS apart (a sync rename, an eviction or an editor's save in progress is
# over by then). A pass the person starts (Update copies now, and every pass not run by the poll) confirms within the
# pass: two looks CONFIRM_GAP apart, the file absent by name both times, its folder listed both times, no iCloud
# placeholder and no atomic-save temporary beside it. A forget of more than FORGET_WITHOUT_ASKING entries of one layer,
# or of a whole layer, then still waits for the person.
CONFIRM_SECONDS = 60.0
CONFIRM_GAP = 2.0
FORGET_WITHOUT_ASKING = 3
SF_DATALESS = 0x40000000           # macOS st_flags: the file's data is in iCloud; reading it downloads it
PLACEHOLDER = re.compile(r'^\.(.+)\.icloud$')
CHAT_HEADER = re.compile(r'^<!-- pebble:conversation id=([0-9a-f]{32}) -->')
MEMORY_HEADER = re.compile(r'^<!-- pebble:memory-layer ([a-z]+) -->')
MEMORY_VERSION = re.compile(r'^<!-- pebble:memory-version ([0-9a-f]{16})\.([0-9]{1,9}) -->$', re.M)
MEMORY_ENTRY = re.compile(r'^<!-- pebble:memory id=([A-Za-z0-9_.:-]{1,128}) -->$', re.M)
MEMORY_ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}')
HEX32 = re.compile(r'[0-9a-f]{32}')
TOKEN = re.compile(r'[0-9a-f]{16}')
NAME_BAD = re.compile(r'[\x00-\x1f\x7f/\\:]+')
LOG_HEADER = '# The last '

LAYER_FILES = {'working': 'Working context.md', 'episodic': 'Past experiences.md', 'semantic': 'Facts & knowledge.md',
               'procedural': 'Procedures.md', 'temporal': 'Time-sensitive information.md',
               'profile': 'Your preferences.md'}

# One table drives the READMEs, docs/DATA_FOLDERS.md and the Data & storage page.
CATEGORIES = (
    {'id': 'conversations', 'place': 'documents', 'folder': 'Conversations', 'label': 'Conversations',
     'what': 'One Markdown file per conversation, named "YYYY-MM-DD Title.md" and kept up to date.',
     'safe': 'Yes, but it deletes the conversation', 'deletable': False,
     'if_deleted': "The conversation moves to Pebble's Recently deleted once Pebble is sure (after a minute, or at once "
                   'with Update copies now). You can restore it there for 30 days; then it is deleted for good.'},
    {'id': 'memory', 'place': 'documents', 'folder': 'Memory', 'label': 'Memory',
     'what': 'One file per memory layer listing what Pebble remembers, with secret-shaped text removed (best effort).',
     'safe': 'Yes, but it makes Pebble forget', 'deletable': False,
     'if_deleted': 'Deleting an entry, or a whole file, makes Pebble forget those memories once it is sure (after a '
                   'minute, or at once with Update copies now); more than 3 entries or a whole file wait until you '
                   'confirm in Data & storage. Pebble keeps a note that you removed them, so it does not learn them '
                   'again from the same source.'},
    {'id': 'attachments', 'place': 'documents', 'folder': 'Attachments', 'label': 'Attachments',
     'what': 'Copies of the files and images you attached to conversations.', 'safe': 'Yes', 'deletable': True,
     'if_deleted': 'Only the copy goes. Your conversations keep their attachments, and Pebble does not copy them again.'},
    {'id': 'artifacts', 'place': 'documents', 'folder': 'Artifacts', 'label': 'Artifacts',
     'what': 'Dashboards, quizzes (questions only, never the answers) and app demos Pebble made.', 'safe': 'Yes',
     'deletable': True, 'if_deleted': 'Only the copy goes; the artifact stays in its conversation.'},
    {'id': 'exports', 'place': 'documents', 'folder': 'Exports', 'label': 'Exports',
     'what': 'Copies of the documents, tables and pages you exported.', 'safe': 'Yes', 'deletable': True,
     'if_deleted': 'Only the copy goes. Files you saved somewhere else with Save stay where you put them.'},
    {'id': 'downloads', 'place': 'documents', 'folder': 'Downloads', 'label': 'Downloads',
     'what': "Files downloaded by Pebble's browser.", 'safe': 'Yes', 'deletable': True,
     'if_deleted': 'The file is gone.'},
    {'id': 'logs', 'place': 'documents', 'folder': 'Logs', 'label': 'Logs',
     'what': 'Recent troubleshooting logs, with secret-shaped text removed (best effort). Not copied while your '
             'Documents folder syncs with iCloud Drive.', 'safe': 'Yes', 'deletable': True,
     'if_deleted': 'Nothing changes; new logs appear as Pebble runs.'},
    {'id': 'database', 'place': 'internal', 'label': 'Database',
     'what': 'pebble.sqlite and its -wal and -shm files: every conversation, memory and history.', 'safe': 'No',
     'deletable': False, 'if_deleted': 'Everything in Pebble is lost.'},
    {'id': 'data', 'place': 'internal', 'label': 'Other data',
     'what': "Attachments, exports, the browser profile, AgentGuard data and the local model's log.", 'safe': 'No',
     'deletable': False, 'if_deleted': 'Conversations lose their attachments and history.'},
    {'id': 'upgrade_backups', 'place': 'internal', 'label': 'Upgrade backups',
     'what': 'Copies of the database from before an upgrade (at most 3). They hold conversations you deleted since.',
     'safe': 'Only if you no longer need to go back', 'deletable': False,
     'if_deleted': 'You can no longer go back to the database from before that upgrade.'},
    {'id': 'backups', 'place': 'internal', 'label': 'Backups',
     'what': 'Reserved for backups. The install tool keeps its backups in ~/PebbleBackups instead (see below).',
     'safe': 'Only if you no longer need them', 'deletable': False, 'if_deleted': 'You cannot restore from them.'},
    {'id': 'config', 'place': 'internal', 'label': 'Settings and instructions',
     'what': 'settings.json, PEBBLE.md, mcp.json, skills and the READMEs.', 'safe': 'Settings: with Pebble closed',
     'deletable': False, 'if_deleted': 'Pebble writes settings.json and the READMEs again from its database.'},
    {'id': 'runtime', 'place': 'internal', 'label': 'Working files',
     'what': 'The last move report, the settings sync state and the Documents copy index.', 'safe': 'With Pebble closed',
     'deletable': False, 'if_deleted': 'A settings change or a forget waiting for your confirmation is dropped.'},
    {'id': 'cache', 'place': 'internal', 'label': 'Cache', 'what': 'Rebuildable caches.', 'safe': 'Yes',
     'deletable': True, 'if_deleted': 'Pebble rebuilds what it needs.'},
    {'id': 'service_logs', 'place': 'internal', 'label': 'Service logs',
     'what': 'service.log and backend.log, with their earlier parts.', 'safe': 'Yes', 'deletable': False,
     'if_deleted': 'Nothing; new logs start. Delete them with Logs.'},
    # Phase 24 (V1-056): places outside both folders that hold copies of your data; sizes only, never changed here.
    {'id': 'old_folder', 'place': 'elsewhere', 'label': 'Old app-data folder',
     'what': 'Everything Pebble 0.5 and earlier kept, frozen when it moved to ~/.pebble. It still holds conversations '
             'you deleted since.', 'safe': 'Yes, once you are happy with the move', 'deletable': False,
     'if_deleted': 'An older Pebble can no longer use it. Delete it yourself in Finder.'},
    {'id': 'install_backups', 'place': 'elsewhere', 'label': 'Install backups',
     'what': '~/PebbleBackups: one folder per install, each with a copy of your data and the previous app.',
     'safe': 'Only if you no longer need to roll back', 'deletable': False,
     'if_deleted': 'You can no longer roll back to that install. List them with scripts/pebble_install.py backups, '
                   'then move a folder to the Trash.'},
    {'id': 'web_storage', 'place': 'elsewhere', 'label': 'Window storage',
     'what': "The app window's own storage: quiz drafts, panel layout and the pet's last conversation.",
     'safe': 'With Pebble closed', 'deletable': False, 'if_deleted': 'Quiz drafts and window layout are reset.'},
)
CATEGORY = {c['id']: c for c in CATEGORIES}
DOCUMENT_CATEGORIES = tuple(c['id'] for c in CATEGORIES if c['place'] == 'documents')
DELETABLE = frozenset(c['id'] for c in CATEGORIES if c['deletable'])
COPIED = ('attachments', 'artifacts', 'exports')
REVEALED = ('old_folder', 'install_backups')       # outside both folders, shown in Finder only
EDITED = {'pebble_md': data_home.INSTRUCTIONS, 'settings_file': data_home.SETTINGS}  # opened in the text editor

_SECRETS = (
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.S),
    re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'
               r'|xox[baprs]-[A-Za-z0-9-]{20,}|AIza[0-9A-Za-z_-]{35}|(?:AKIA|ASIA)[A-Z0-9]{16}'
               r'|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'
               # Phase 24 (V1-062): Stripe, Hugging Face, GitLab, npm, Google OAuth, Slack app and Shopify tokens.
               r'|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|hf_[A-Za-z0-9]{30,}|glpat-[A-Za-z0-9_-]{20,}'
               r'|npm_[A-Za-z0-9]{36}|ya29\.[A-Za-z0-9_.-]{20,}|xapp-[A-Za-z0-9-]{20,}|shp(?:at|ss|ca|pa)_[a-fA-F0-9]{32})\b'),
    re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}'),
)
# Phase 24 (V1-062): a cookie header's value, secret-bearing URL parameters, a password said in words.
_LABELLED = (
    re.compile(r'(?i)\b((?:set-)?cookie\s*:\s*)[^\r\n]+'),
    re.compile(r'(?i)([?&#;](?:code|access_token|id_token|refresh_token|token|api_?key|key|client_secret|password|sig'
               r'|signature|session(?:_?id)?|auth)=)(?!\[secret removed\])[^&#\s"\'<>]{4,}'),
    re.compile(r'(?i)\b((?:password|passcode|passphrase|passwd)\s+(?:is|was)\s+)(?!not\b|\[secret removed\])[^\s"\']{4,}'),
)
# A key name ending in token, secret, password (or one of the key names below), optionally prefixed (access_token,
# id_token, client-secret, sessionId), then ':' or '='. The prefix is one bounded character class, so a long run of
# letters cannot make the match backtrack without end.
_ASSIGNED = re.compile(r'(?i)\b([a-z0-9_.-]{0,40}?(?:api[_-]?key|access[_-]?key|private[_-]?key|secret[_-]?key'
                       r'|signing[_-]?key|client[_-]?secret|auth[_-]?token|refresh[_-]?token|session[_-]?id|token'
                       r'|secret|password|passwd|pwd|credentials?)["\']?\s*[:=]\s*["\']?)(?!\[secret removed\])'
                       r'([^\s"\',;]{6,})')
_CARD = re.compile(r'\b(?:\d[ -]?){12,18}\d\b')
_FLAG = re.compile(r'(?i)^--?[a-z0-9-]*(?:token|key|secret|password|passwd|auth|credential)[a-z0-9-]*$')
REMOVED = '[secret removed]'


def _luhn(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _card(match):
    digits = re.sub(r'\D', '', match.group(0))
    if 13 <= len(digits) <= 19 and digits[0] in '23456' and _luhn(digits):
        return REMOVED
    return match.group(0)


def scrub(text):
    """Secret-shaped text (keys, tokens, private keys, password assignments, cookies, card numbers) replaced by a
    fixed label. Best effort: a secret with no recognisable shape stays."""
    text = str(text or '')
    for pattern in _SECRETS:
        text = pattern.sub(lambda m: (m.group(1) + ' ' + REMOVED) if m.re.groups else REMOVED, text)
    for pattern in _LABELLED:
        text = pattern.sub(lambda m: m.group(1) + REMOVED, text)
    text = _ASSIGNED.sub(lambda m: m.group(1) + REMOVED, text)
    return _CARD.sub(_card, text)


def inert(text):
    """Text for a copy: scrubbed, with HTML comments made visible so no content can look like a Pebble marker."""
    return scrub(text).replace('<!--', '&lt;!--')


def safe_name(text, limit=80, fallback='Untitled'):
    name = ' '.join(NAME_BAD.sub(' ', str(text or '')).split()).lstrip('. ')
    return (name[:limit].rstrip(' .') or fallback)


def copy_name(text, limit=80, fallback='Untitled'):
    """A file name for a copy: scrubbed first (V1-019), so a key in a title never reaches Finder, Spotlight or iCloud."""
    return safe_name(scrub(text), limit, fallback)


def local_date(seconds):
    try:
        return datetime.fromtimestamp(float(seconds)).strftime('%Y-%m-%d')
    except (TypeError, ValueError, OverflowError, OSError):
        return '0000-00-00'


def local_time(seconds):
    try:
        return datetime.fromtimestamp(float(seconds)).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return 'unknown time'


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _open_in_finder(path):
    if sys.platform != 'darwin':
        raise ValueError('Show in Finder is available on macOS.')
    subprocess.run(['/usr/bin/open', str(path)], check=False, timeout=10, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _open_in_editor(path):
    """Open one of Pebble's own text files in the person's default text editor (never run, never another app)."""
    if sys.platform != 'darwin':
        raise ValueError('Opening files is available on macOS.')
    subprocess.run(['/usr/bin/open', '-t', str(path)], check=False, timeout=10, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _default_vault():
    from .vault import Vault
    return Vault()


def _xattr_names(path):
    """Extended attribute names of `path` through libc (macOS); empty when unavailable."""
    if sys.platform != 'darwin':
        return []
    try:
        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        listxattr = libc.listxattr
        listxattr.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int)
        listxattr.restype = ctypes.c_ssize_t
        raw = os.fsencode(str(path))
        size = listxattr(raw, None, 0, 1)
        if size <= 0:
            return []
        buffer = ctypes.create_string_buffer(size)
        size = listxattr(raw, buffer, size, 1)
        return [n.decode('utf-8', 'replace') for n in buffer.raw[:max(size, 0)].split(b'\0') if n]
    except (OSError, AttributeError, TypeError):
        return []


def icloud_status(folder):
    """Whether the Documents copies appear to sync with iCloud Drive. Pebble's database is never there."""
    home = Path.home()
    cloud = home / 'Library' / 'Mobile Documents'
    evidence = []
    target = os.path.realpath(folder) if folder else None
    if target and (target.casefold() + os.sep).startswith(os.path.realpath(cloud).casefold() + os.sep):
        evidence.append('inside_icloud_drive')
    if os.path.isdir(cloud / 'com~apple~CloudDocs' / 'Documents'):
        evidence.append('icloud_drive_has_documents')
    names = [n for n in _xattr_names(home / 'Documents') if 'icloud' in n.lower() or 'fileprovider' in n.lower()
             or 'file-provider' in n.lower()]
    if names:
        evidence.append('documents_folder_is_managed')
    return {'documents_synced': bool(evidence), 'evidence': evidence, 'database_synced': False,
            'note': ('iCloud Drive appears to sync your Documents folder, so these copies sync too (troubleshooting '
                     "logs are not copied there). Pebble's database is in ~/.pebble/data, which iCloud does not sync.")
            if evidence else "Your Documents folder does not appear to sync with iCloud Drive. Pebble's database never "
                             'does.'}


def _lstat(path):
    return os.lstat(path)


def _dataless(path):
    """True for a file whose data is in iCloud only (SF_DATALESS): present, and never read, since reading downloads it."""
    try:
        return bool(getattr(_lstat(path), 'st_flags', 0) & SF_DATALESS)
    except OSError:
        return False


def _saving(names, name):
    """Whether a folder listing (case-folded names) shows an atomic save of `name` in progress: a temporary sibling such
    as Pebble's own .name.tmp-*, an editor's name~, name.tmp, .name.swp, name.sb-* (macOS safe save) or ~$name."""
    base = name.casefold()
    stem = base[:-3] if base.endswith('.md') else base
    return any(entry != base and entry.startswith((base, '.' + base, stem + '.sb-', '.~' + stem, '~$' + stem,
                                                   '.' + stem + '.sw')) for entry in names)


def _listing(folder):
    """(names, iCloud placeholder names), both case-folded, or None when the folder itself is gone."""
    try:
        entries = os.listdir(folder)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    names, cloud = set(), set()
    for name in entries:
        match = PLACEHOLDER.match(name)
        if match:
            cloud.add(match.group(1).casefold())
        else:
            names.add(name.casefold())
    return names, cloud


def _head(path, size=256):
    try:
        fd = os.open(path, os.O_RDONLY | data_home._NOFOLLOW | data_home._CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, size).decode('utf-8', 'replace')
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_text(path, limit=MAX_TEXT_FILE):
    try:
        fd = os.open(path, os.O_RDONLY | data_home._NOFOLLOW | data_home._CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with os.fdopen(fd, 'rb') as source:
            fd = None
            return source.read(limit + 1).decode('utf-8', 'replace')
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _signature(path):
    """(size, mtime_ns) of a regular file, never through a link; None otherwise."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return [info.st_size, info.st_mtime_ns] if stat.S_ISREG(info.st_mode) else None


def _measure(path, deadline, limit=200_000):
    """(bytes, files, partial) below `path`, never following links, bounded in entries and time."""
    total = files = seen = 0
    stack = [str(path)]
    try:
        info = os.lstat(path)
    except OSError:
        return 0, 0, False
    if stat.S_ISREG(info.st_mode):
        return info.st_size, 1, False
    if not stat.S_ISDIR(info.st_mode):
        return 0, 0, False
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > limit or time.monotonic() > deadline:
                        return total, files, True
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
                    elif stat.S_ISREG(st.st_mode):
                        total += st.st_size
                        files += 1
        except OSError:
            continue
    return total, files, False


# ---- Refusals the host raises from send() and run_resume() (parent patches; main.py sends .code) -----------------


class TrashedConversation(ValueError):
    """V1-018: a conversation in Recently deleted takes no new messages until the person restores it."""
    code = 'trashed_conversation'

    def __init__(self):
        super().__init__('This conversation is in Recently deleted. Restore it in Settings -> Data & storage to keep '
                         'using it.')


WITHHELD_NOTICE = 'Earlier on-device messages were not shared with {model}. Review to include them.'


def withheld_notice(model):
    return WITHHELD_NOTICE.format(model=model or 'this model')


def is_trashed(store, cid):
    from .store import TRASH_KEY
    entries = store.setting(TRASH_KEY, {}) or {}
    return isinstance(entries, dict) and cid in entries


def refuse_if_trashed(store, cid):
    if is_trashed(store, cid):
        raise TrashedConversation()


def _route_class(route):
    from .model_handoff import ModelHandoffError, route_class
    try:
        return route_class(route) if isinstance(route, dict) else None
    except ModelHandoffError:
        return None


def route_view(store, cid, messages, route):
    """V1-002: what a request on `route` may carry of this conversation. A cloud request never carries a turn that was
    answered on this Mac, nor the recorded results of the actions an on-device run took: they stay in the conversation
    and reach a cloud route only through the reviewed model handoff (C10), where the person can release them. Nothing
    else changes, and a device route sees everything, as before.

    A turn is on-device when its recorded route class is 'device' (C10's classification), or, for a turn stored before
    route classes were recorded, when it is an on-device run's reply or the request that started one. A conversation
    that never ran on a device route has no such turns ('device' rows there are 0.6.1's records of slash commands, which
    no model produced). In a handed-off conversation the classes the person released toward that destination stay.

    Returns {'messages': the messages to send (the same dicts, in order), 'withheld': withheld message ids, 'actions':
    withheld action ids, 'destination': the route class, 'source_run_id': the newest on-device run (the one a reviewed
    handoff starts from), or None}."""
    destination = _route_class(route)
    view = {'messages': list(messages), 'withheld': [], 'actions': [], 'destination': destination, 'source_run_id': None}
    if destination is None or not destination.startswith('cloud:'):
        return view
    scope = (store.setting('model_handoff_chats', {}) or {}).get(cid) or {}
    released = list(scope.get('allowed') or ())  # [the handoff's destination class, *the classes released to it]
    exempt = set()
    if released[:1] == [destination] and 'device' in released:
        if not isinstance(scope.get('released_messages'), list):
            return view  # a handoff started before Phase 24's scoping keeps its class-wide release, as before
        # Phase 24 (parent, t24-sec-recheck-arch-02): the person released the messages they reviewed; a turn answered
        # on this Mac later in this conversation is withheld like anywhere else, with the same notice.
        exempt = set(scope['released_messages'])
    device = []
    for row in store.query("SELECT id,goal,json_extract(config,'$.route') AS route,CASE WHEN json_valid(checkpoint) THEN "
                           "json_extract(checkpoint,'$.message_id') END AS mid FROM runs WHERE chat_id=? AND "
                           'json_valid(config) ORDER BY created,id', (cid,)):
        try:
            found = _route_class(json.loads(row['route'])) if row['route'] else None
        except ValueError:
            found = None
        if found == 'device':
            device.append(row)
    if not device:
        return view
    recorded = {r['message_id']: r['route_class'] for r in store.query(
        'SELECT message_id,route_class FROM message_routes WHERE chat_id=?', (cid,))}
    replies = {r['mid'] for r in device if isinstance(r['mid'], str)}
    goals = {str(r['goal'] or '').strip() for r in device}
    withheld = []
    for message in messages:
        if message.get('id') in exempt:
            continue
        cls = recorded.get(message.get('id'))
        if cls == 'device' or cls is None and (message.get('id') in replies or message.get('role') == 'user'
                                                and str(message.get('content') or '').strip() in goals):
            withheld.append(message.get('id'))
    ids = [r['id'] for r in device]
    actions = [r['action_id'] for r in store.query(
        'SELECT action_id FROM run_action_owners WHERE run_id IN (%s)' % ','.join('?' * len(ids)), ids)]
    hidden = set(withheld)
    view.update(messages=[m for m in messages if m.get('id') not in hidden], withheld=withheld, actions=actions,
                source_run_id=device[-1]['id'])
    return view


def working_origin(store, cid):
    """V1-002: the run this conversation's pinned working record was derived from (recall carries it without an id), or
    None when unknown. A run that wrote no record of its own (no start-time consent) leaves an earlier run's record,
    perhaps an on-device one, so recall_for_route judges the record by that run's route like any other learned record."""
    rows = store.query('SELECT payload FROM checkpoints WHERE chat_id=?', (cid,))
    try:
        payload = json.loads(rows[0]['payload']) if rows else None
    except (TypeError, ValueError):
        return None
    run = payload.get('run_id') if isinstance(payload, dict) else None
    return run if isinstance(run, str) and run else None


class DataServices:
    def __init__(self, app, layout=None, *, poll=POLL, opener=None, clock=time.time, editor=None, vault=None,
                 sleep=time.sleep):
        self.app = app
        self.sleep = sleep  # the CONFIRM_GAP wait inside a pass the person started
        found, report = data_home.for_data(app.home)
        self.layout = layout or found
        self.migration = report if report is not None else data_home.read_status(self.layout) or {}
        self.active = bool(self.layout and self.layout.structured)
        self.poll, self.clock = poll, clock
        self.opener = opener or _open_in_finder
        # A test that injects an opener gets the files too, so no test ever starts a real editor.
        self.editor = editor or (opener if opener is not None else _open_in_editor)
        self.vault = vault or _default_vault
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self.thread = None
        self.events, self.problems = [], {}
        self.last = {}
        self.settings = None
        self._names = {}
        self._icloud = None
        self.migrated_from = None
        if self.active:
            self.settings = getattr(app.store, 'settings_file', None)
            if self.settings is None:
                self.settings = app.store.settings_file = settings_file.SettingsSync(self.layout.root, app.store)
            self.manifest = self._load_manifest()
            self._protect(self.manifest.get('root'), *(r['root'] for r in self.manifest['retired']))
            try:
                self.mirror_root()  # V1-003: the configured folder is protected before the first pass writes it
            except Exception:
                pass

    # ---- Lifecycle ----------------------------------------------------------------------------

    def start(self):
        if not self.active or self.thread is not None:
            return
        self.thread = threading.Thread(target=self._loop, name='pebble-storage', daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        thread, self.thread = self.thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _loop(self):
        launch = True
        while not self._stop.is_set() and not getattr(self.app, 'closed', False):
            try:
                self.tick(launch=launch, background=True)
            except Exception as error:  # the storage service never stops Pebble
                self.problems['service'] = type(error).__name__
            launch = False
            self._stop.wait(self.poll)

    def tick(self, launch=False, background=False):
        """One pass: settings, PEBBLE.md, mcp.json, Recently deleted, then the Documents copies. `background`: the
        poll's pass, where a deletion counts only after two passes CONFIRM_SECONDS apart; any other pass (Update copies
        now, a storage change in the app) confirms a deletion within itself, CONFIRM_GAP apart."""
        if not self.active:
            return {'active': False}
        with self.lock:
            before = (self.settings.error, tuple(p['id'] for p in self.settings.pending))
            out = {'settings': self._step('settings', lambda: self.settings.sync(self._apply_setting))}
            after = (self.settings.error, tuple(p['id'] for p in self.settings.pending))
            if after[0] and after[0] != before[0]:
                self._changed('settings_invalid')  # the windows show it; the last good settings stay
            if set(after[1]) - set(before[1]):
                self._changed('settings_pending')  # a change from settings.json waits for the person
            out['pebble_md'] = self._step('pebble_md', self._sync_pebble_md)
            out['mcp'] = self._step('mcp', self._write_mcp)
            out['purged'] = self._step('recently_deleted', self._purge_due)
            root, reason = self.mirror_root()
            if root is None:
                if self.manifest.get('root') is not None:
                    self._retire()
                    self._reset_manifest(None)
                self.last['mirror'] = reason
                self._icloud = None
            else:
                self._icloud = self._step('icloud', lambda: self._notice_icloud(root))
                out['reconcile'] = self._step('reconcile', lambda: self._reconcile(root, background))
                out['refresh'] = self._step('refresh', lambda: self._refresh(root))
                self.last['mirror'] = 'on'
            self._save_manifest()
            self.last['tick'] = self.clock()
            if launch:
                self.last['launch'] = self.last['tick']
            return out

    def _step(self, name, task):
        try:
            result = task()
            self.problems.pop(name, None)
            return result
        except PermissionError:
            self.problems[name] = ('macOS did not allow Pebble to write there. Allow Pebble in System Settings -> '
                                   'Privacy & Security -> Files and Folders, then try again.')
        except Exception as error:
            self.problems[name] = f'{type(error).__name__}: {scrub(str(error))[:200]}'
        return None

    def _event(self, kind, text, **facts):
        self.events = (self.events + [{'at': self.clock(), 'kind': kind, 'text': text, **facts}])[-MAX_EVENTS:]

    def _changed(self, what, **facts):
        try:
            self.app.emit('storage', {'changed': what, **facts})
        except Exception:
            pass

    def _read(self, sql, args=()):
        """V1-011: the pass reads through the store's read-only connection for this thread, never holding Store.lock."""
        reader = getattr(self.app.store, 'read', None)
        return reader(sql, args) if callable(reader) else self.app.store.query(sql, args)

    def _protect(self, *roots):
        for root in roots:
            if root:
                data_home.protect_mirror(root, owner=self.app.home)

    # ---- Settings, PEBBLE.md and mcp.json -------------------------------------------------------

    def _apply_setting(self, key, value):
        """Apply one setting through the host, so its side effects run; ValueError means not now."""
        if key == settings_file.STORAGE:
            previous = settings_file.applied(self.app.store)[settings_file.STORAGE]
            self.app.store.set_setting(key, settings_file.validate(key, value))
            if previous['documents_folder'] != value['documents_folder']:
                self._retire()
                self._reset_manifest(None)
            return
        self.app.dispatch('setting', {'key': key, 'value': value})
        if key == 'permissions':
            self.settings.written(key, value)  # App.setting writes this row directly, past Store.set_setting

    def _sync_pebble_md(self):
        state = self.manifest.setdefault('pebble_md', {})
        path = self.layout.root / data_home.INSTRUCTIONS
        raw = _read_text(path, 256 * 1024)
        text = re.sub(r'<!--.*?(?:-->|\Z)', '', raw or '', flags=re.S).strip()
        digest = sha256(text.encode())
        if state.get('digest') == digest and state.get('applied'):
            return state.get('status')
        exists = bool(self.app.store.query('SELECT 1 FROM memories WHERE id=?', (PEBBLE_MD_ID,)))
        used = text
        if len(text) > PEBBLE_MD_LIMIT:
            # V1-036: never the previous version. A longer file is used up to its last full line within the limit; one
            # line longer than the limit cannot be cut at a line, so nothing from it is used and the old record goes.
            cut = text.rfind('\n', 0, PEBBLE_MD_LIMIT + 1)
            used = text[:cut].rstrip() if cut > 0 else ''
        if not used:
            if exists:
                self.app.memory_delete(PEBBLE_MD_ID)  # ValueError while a task runs: retried next pass
            if text:
                state.update(digest=digest, applied=True, status='too_long', used_chars=0, total_chars=len(text),
                             message=f'PEBBLE.md has {len(text):,} characters on one line, so Pebble does not use it. '
                                     f'Break it into lines, or shorten it to {PEBBLE_MD_LIMIT:,} characters.')
            else:
                state.update(digest=digest, applied=True, status='empty', message=None, used_chars=0, total_chars=0)
            return state['status']
        try:
            self.app.memory.put('profile', used, 'global', None, origin='user', ident=PEBBLE_MD_ID)
        except ValueError as error:  # memory, or the "Your preferences" layer, is off
            state.update(digest=digest, applied=False, status='memory_off', message=str(error)[:200], used_chars=0,
                         total_chars=len(text))
            return state['status']
        if used != text:
            state.update(digest=digest, applied=True, status='truncated', used_chars=len(used), total_chars=len(text),
                         message=f'PEBBLE.md has {len(text):,} characters; Pebble uses the first {len(used):,}, up to '
                                 f'its last full line within {PEBBLE_MD_LIMIT:,}. Shorten it to use all of it.')
        else:
            state.update(digest=digest, applied=True, status='used', message=None, used_chars=len(text),
                         total_chars=len(text))
        return state['status']

    def pebble_md_status(self):
        """{'status', 'used_chars', 'total_chars'} of PEBBLE.md as last applied (for the runner's 'first N of M' note)."""
        state = dict((self.manifest.get('pebble_md') or {}) if self.active else {})
        return {k: state.get(k) for k in ('status', 'used_chars', 'total_chars')}

    def _write_mcp(self):
        connectors = []
        for item in self.app.extensions.list():
            if item.get('kind') != 'plugin':
                continue
            config = item.get('config') or {}
            entry = {'name': item.get('name'), 'enabled': bool(item.get('enabled')),
                     'transport': config.get('transport', 'http'),
                     'tools': config.get('tools', []), 'resources': config.get('resources', []),
                     'prompts': config.get('prompts', []), 'read_only': bool(config.get('read_only', False))}
            if entry['transport'] == 'stdio':
                entry['command'] = config.get('command')
                args = [a for a in config.get('args', []) if isinstance(a, str)]
                # V1-062: the value after a flag such as --token or --api-key is a secret whatever its shape.
                entry['args'] = [REMOVED if index and _FLAG.match(args[index - 1]) else scrub(a)
                                 for index, a in enumerate(args)]
            else:
                url = str(config.get('url') or '')
                entry['url'] = scrub(re.sub(r'^([a-z]+://)[^/@]*@', r'\1', url).split('?')[0].split('#')[0])
            connectors.append(entry)
        view = {'_readme': 'Generated by Pebble from Settings -> Extensions. A read-only view: edits here are not '
                           'applied, and passwords and tokens are never written here (they stay in the Keychain). '
                           'Add, review or remove connectors in the app.',
                'connectors': connectors}
        data_home.refresh(self.layout.root / data_home.MCP, (json.dumps(view, indent=2, ensure_ascii=False) + '\n').encode())
        return len(connectors)

    # ---- Recently deleted -----------------------------------------------------------------------

    def _trash(self, cid, origin='documents'):
        """Hide one conversation (Recently deleted). The full deletion checks run later in App.delete_chat."""
        if getattr(self.app, 'active', None) == cid:
            raise ValueError('It is running a task.')
        try:
            live = self.app.store.query("SELECT 1 FROM process_jobs WHERE chat_id=? AND state IN ('admitted','running') "
                                        'LIMIT 1', (cid,))
        except Exception:
            live = []
        if live:
            raise ValueError('Its commands are still running.')
        try:
            scheduled = self.app.store.query("SELECT 1 FROM jobs WHERE chat_id=? AND state='active' LIMIT 1", (cid,))
        except Exception:
            scheduled = []
        if scheduled:  # a hidden conversation must not keep running on a schedule
            raise ValueError('Its schedule is active. Pause or delete the schedule first.')
        return self.app.store.trash_chat(cid, origin, now=self.clock())

    def _purge_due(self):
        done = []
        for cid in self.app.store.trash_due(now=self.clock()):
            try:
                self.app.delete_chat(cid)
            except ValueError as error:  # PolicyError too: the full checks refused; it stays hidden and is retried
                self.problems['purge:' + cid] = str(error)[:200]
                continue
            self.app.store.untrash_chat(cid)
            self.problems.pop('purge:' + cid, None)
            done.append(cid)
        if done:
            self._checkpoint()
            self._event('purged', f'Deleted {len(done)} conversation(s) for good after 30 days in Recently deleted.')
            self._changed('recently_deleted')
        return done

    def _checkpoint(self):
        """V1-057: after a purge or forget batch, the deleted text leaves the write-ahead log too (best effort)."""
        checkpoint = getattr(self.app.store, 'checkpoint', None)
        if callable(checkpoint):
            try:
                return checkpoint()
            except Exception:
                return False
        return False

    # ---- Documents: configuration and manifest ----------------------------------------------------

    def mirror_root(self):
        storage = settings_file.applied(self.app.store)[settings_file.STORAGE]
        if not storage['documents_mirror']:
            return None, 'off'
        if storage['documents_folder']:
            root = Path(os.path.abspath(os.path.expanduser(storage['documents_folder'])))
        elif self.layout.is_default:
            root = Path.home() / 'Documents' / 'Pebble'
        else:
            return None, 'no_folder'  # a home other than ~/.pebble copies only to a folder settings.json names
        self._protect(root)  # V1-003: before anything is written there
        return root, None

    def downloads_folder(self):
        """Where Pebble's browser should save downloads (b22): Documents/Pebble/Downloads while the copies are on,
        else None (the browser keeps its own folder). Created on demand; never inside ~/.pebble."""
        if not self.active:
            return None
        root, _ = self.mirror_root()
        if root is None:
            return None
        folder = root / CATEGORY['downloads']['folder']
        with self.lock:
            self._folders(root)
        return folder

    def _empty_manifest(self, root):
        return {'format': FORMAT, 'root': str(root) if root else None, 'mirror_id': secrets.token_hex(8) if root else None,
                'conversations': {}, 'memory': {}, 'files': {c: {} for c in COPIED}, 'logs': {},
                'pending': {'trash': {}, 'forget': {}}, 'absent': {'conversations': {}, 'memory': {}}, 'confirm': {},
                'retired': [], 'pebble_md': {}, 'icloud': {}}

    def _load_manifest(self):
        value = data_home.read_json(self.layout.root / data_home.RUNTIME / MANIFEST, 32 << 20)
        if not value or value.get('format') not in READ_FORMATS:
            return self._empty_manifest(None)
        if value['format'] != FORMAT:
            self.migrated_from = value['format']
        base = self._empty_manifest(None)
        for key in base:
            if not isinstance(value.get(key), type(base[key])) and base[key] is not None:
                value[key] = base[key]
        value['format'] = FORMAT
        for category in COPIED:
            if not isinstance(value['files'].get(category), dict):
                value['files'][category] = {}
        for key in ('trash', 'forget'):
            if not isinstance(value['pending'].get(key), dict):
                value['pending'][key] = {}
        for key in ('conversations', 'memory'):
            if not isinstance(value['absent'].get(key), dict):
                value['absent'][key] = {}
        value['retired'] = [r for r in value['retired'] if isinstance(r, dict) and isinstance(r.get('root'), str)]
        for layer in value['memory'].values():
            if isinstance(layer, dict) and not isinstance(layer.get('added'), dict):
                # pebble-mirror/1: every listed id was in Pebble's last write; the file carries no version yet, so the
                # first changed save is uncertain and forgets nothing.
                layer['added'] = {i: 0 for i in layer.get('ids') or []}
                layer.setdefault('rev', 0)
        if self.migrated_from:
            self._chat_ids(value)
        self._saved = None if self.migrated_from else json.dumps(value, sort_keys=True)
        return value

    def _chat_ids(self, manifest):
        """pebble-mirror/1 copies carry no chat id: fill them in from the database while the rows exist."""
        lookups = {'attachments': lambda key: self._owner(*key.split(':', 1)), 'artifacts': lambda key: self._one(
            'SELECT chat_id FROM artifacts WHERE id=?', key), 'exports': lambda key: self._one(
            'SELECT chat_id FROM export_records WHERE id=?', key)}
        for category, lookup in lookups.items():
            for key, entry in manifest['files'][category].items():
                if isinstance(entry, dict) and not entry.get('chat'):
                    try:
                        entry['chat'] = lookup(key)
                    except Exception:
                        continue

    def _one(self, sql, value):
        rows = self.app.store.query(sql, (value,))
        return rows[0]['chat_id'] if rows else None

    def _owner(self, kind, ident):
        return self._one('SELECT chat_id FROM %s WHERE id=?' % ('image_attachments' if kind == 'image' else
                                                                'file_attachments'), ident)

    def _save_manifest(self):
        text = json.dumps(self.manifest, sort_keys=True)
        if text == getattr(self, '_saved', None):
            return
        try:
            data_home.write_atomic(self.layout.root / data_home.RUNTIME / MANIFEST, text.encode())
            self._saved = text
        except OSError as error:
            self.problems['manifest'] = type(error).__name__

    def _retire(self):
        """V1-016: the copies folder Pebble stops writing (copies off, another location, a reset index) is remembered,
        with its conversation and file copies, so deleting a conversation later still removes them. It stays protected."""
        m = self.manifest
        if not m.get('root'):
            return
        record = {'root': m['root'], 'mirror_id': m.get('mirror_id'), 'retired': self.clock(),
                  'conversations': {cid: {'file': e['file'], 'sha256': e.get('sha256')}
                                    for cid, e in m['conversations'].items() if isinstance(e, dict) and e.get('file')},
                  'files': {c: {k: {'file': e['file'], 'sha256': e.get('sha256'), 'chat': e.get('chat')}
                                for k, e in m['files'][c].items() if isinstance(e, dict) and not e.get('dismissed')}
                            for c in COPIED}}
        retired = [r for r in m['retired'] if r['root'].casefold() != record['root'].casefold()]
        earlier = next((r for r in m['retired'] if r['root'].casefold() == record['root'].casefold()), None)
        if earlier:
            record['conversations'] = {**(earlier.get('conversations') or {}), **record['conversations']}
            record['files'] = {c: {**((earlier.get('files') or {}).get(c) or {}), **record['files'][c]} for c in COPIED}
        m['retired'] = sorted(retired + [record], key=lambda r: r.get('retired') or 0)[-MAX_RETIRED:]
        self._protect(record['root'])

    def _reset_manifest(self, root):
        """A new or moved Documents folder starts a new index: nothing missing from it counts as a deletion."""
        keep = {k: self.manifest.get(k) for k in ('pebble_md', 'pending', 'retired', 'confirm', 'icloud')}
        self.manifest = self._empty_manifest(root)
        for key in ('pebble_md', 'retired', 'confirm', 'icloud'):
            self.manifest[key] = keep[key] or self.manifest[key]
        if root is not None:
            self.manifest['pending'] = keep['pending'] or self.manifest['pending']
            self._protect(root)
            data_home.private_dir(root)
            data_home.write_json(root / ROOT_MARKER, {
                'format': FORMAT, 'mirror_id': self.manifest['mirror_id'],
                'note': 'Marks this folder as Pebble\'s copies. If you delete this file, Pebble starts a new index and '
                        'counts nothing as deleted.'})

    def _notice_icloud(self, root):
        """V1-058: the first pass that finds the copies syncing with iCloud Drive tells the windows once."""
        status = icloud_status(root)
        seen = self.manifest.setdefault('icloud', {})
        if status['documents_synced'] and not seen.get('first_seen'):
            seen['first_seen'] = self.clock()
            self._event('icloud', 'Your Documents folder syncs with iCloud Drive, so the copies there sync too. '
                                  'Troubleshooting logs are not copied there.')
            self._changed('icloud', icloud_first_seen=True, path=str(root))
        return status

    # ---- Documents: reconcile deletions -----------------------------------------------------------

    def _reconcile(self, root, background=False):
        marker = data_home.read_json(root / ROOT_MARKER)
        m = self.manifest
        if not marker or marker.get('mirror_id') != m.get('mirror_id') or m.get('root') != str(root):
            self._retire()
            self._reset_manifest(root)
            return {'reset': True}
        out = {'trashed': self._reconcile_conversations(root / 'Conversations', background),
               'forgotten': self._reconcile_memory(root / 'Memory', background),
               'dismissed': sum(self._reconcile_files(c, root / CATEGORY[c]['folder']) for c in COPIED)}
        out['retried'] = self._retry_pending()
        out['waiting'] = len(m['absent']['conversations']) + len(m['absent']['memory'])
        self.last['reconcile'] = self.clock()
        if out['trashed'] or out['forgotten']:
            self._changed('documents')
        return out

    def _look(self, folder, entries):
        """One look at the Conversations folder: (names, iCloud placeholders, missing chat ids, {chat id: the name its
        renamed file has now}, unsure), or None when the folder is gone or cannot be listed. A missing file the person
        renamed is still its conversation: its first line names it. Only names Pebble did not write are read, never an
        iCloud stand-in (reading downloads it); one that cannot be read leaves the answer open (unsure)."""
        try:
            raw = os.listdir(folder)
        except OSError:
            return None
        cloud = {m.group(1).casefold() for m in map(PLACEHOLDER.match, raw) if m}
        names = {n.casefold() for n in raw if not PLACEHOLDER.match(n)}
        missing = {cid for cid, e in entries.items() if e['file'].casefold() not in names and e['file'].casefold() not in cloud}
        present, unsure = {}, False
        if missing:  # V1-011: a pass with every file in place reads no file at all
            known = {e['file'].casefold() for e in entries.values()}
            for name in sorted(raw):
                if not name.endswith('.md') or name.casefold() in known or name == 'README.md' or PLACEHOLDER.match(name):
                    continue
                path = folder / name
                if _dataless(path):
                    unsure = True
                    continue
                head = _head(path)
                if head is None:
                    unsure = True
                    continue
                match = CHAT_HEADER.match(head)
                if match:
                    present.setdefault(match.group(1), name)
        return names, cloud, missing, present, unsure

    def _reconcile_conversations(self, folder, background=False):
        entries, absent = self.manifest['conversations'], self.manifest['absent']['conversations']
        first = self._look(folder, entries)
        if first is None:
            self.manifest['conversations'] = {}  # the folder itself is gone: write it all again, delete nothing
            absent.clear()
            return 0
        names, cloud, missing, present, unsure = first
        for cid in [c for c in absent if c not in missing]:
            absent.pop(cid)  # back (or no longer ours to watch)
        now = self.clock()
        for cid in sorted(missing & set(present)):
            entries[cid]['file'] = present[cid]  # renamed by the person: still there; the refresh restores its name
            absent.pop(cid, None)
        candidates = {cid for cid in missing - set(present) if not _saving(names, entries[cid]['file'])}
        for cid in missing - set(present):
            absent.setdefault(cid, now)
        if unsure or not candidates:
            return 0  # V1-014: an unreadable file that may be one of these, or an atomic save in progress
        if background:
            ripe = {cid for cid in candidates if now - absent[cid] >= CONFIRM_SECONDS}
        else:
            # A pass the person started: a second look CONFIRM_GAP later must agree on every point.
            self.sleep(CONFIRM_GAP)
            second = self._look(folder, entries)
            if second is None or second[4]:
                return 0
            names2, cloud2, missing2, present2, _ = second
            ripe = {cid for cid in candidates if cid in missing2 and cid not in present2
                    and entries[cid]['file'].casefold() not in cloud2 and not _saving(names | names2, entries[cid]['file'])}
        trashed, moved = self.app.store.trashed(), 0
        for cid in sorted(ripe):
            entry = entries[cid]
            del entries[cid]
            absent.pop(cid, None)
            if cid in trashed or not self.app.store.query('SELECT 1 FROM chats WHERE id=?', (cid,)):
                continue
            title = scrub(entry.get('title') or entry['file'])[:120]
            try:
                self._trash(cid)
                moved += 1
                self._event('trashed', f'Moved "{title}" to Recently deleted because its file was deleted.', chat_id=cid)
            except ValueError as error:
                self.manifest['pending']['trash'][cid] = {'since': now, 'reason': str(error)[:200], 'title': title}
                self._event('trash_waiting', f'"{title}" moves to Recently deleted when {str(error).rstrip(".").lower()} '
                                             'stops.', chat_id=cid)
        return moved

    def _gone(self, kind, entry, path, cloud):
        """(ids the person removed, whole) for one layer file, or (None, False) when that is not certain. An id counts
        only if it was in the version of the file the person saved: an editor saving an older copy removes nothing
        that was added since (V1-004)."""
        ids = entry.get('ids') or []
        if not os.path.lexists(path):
            if entry['file'].casefold() in cloud:
                return None, False
            return set(ids), True
        if _dataless(path):
            return None, False  # an iCloud stand-in: never read, never a deletion
        if _signature(path) == [entry.get('size'), entry.get('mtime_ns')]:
            return set(), False  # Pebble's own last write, untouched
        text = _read_text(path)
        if text is None:
            return None, False
        if entry.get('sha256') and sha256(text.encode()) == entry['sha256']:
            return set(), False
        header = MEMORY_HEADER.match(text)
        version = MEMORY_VERSION.search(text)
        if not header or header.group(1) != kind or not version or version.group(1) != self.manifest.get('mirror_id'):
            return None, False  # rewritten beyond recognition, or a copy of another index: nothing counts as deleted
        base = int(version.group(2))
        if base > int(entry.get('rev') or 0):
            return None, False
        found, added = set(MEMORY_ENTRY.findall(text)), entry.get('added') or {}
        gone = {i for i in ids if i not in found and int(added.get(i, 0)) <= base}
        return gone, len(ids) >= 2 and not found & set(ids)

    def _reconcile_memory(self, folder, background=False):
        absent = self.manifest['absent']['memory']
        listing = _listing(folder)
        if listing is None:
            self.manifest['memory'] = {}  # the folder itself is gone: write it all again, forget nothing
            absent.clear()
            return 0
        names, cloud = listing
        now, forgotten, seen = self.clock(), 0, {}
        for kind, entry in self.manifest['memory'].items():
            if not entry.get('ids'):
                continue
            gone, whole = self._gone(kind, entry, folder / entry['file'], cloud)
            if gone is None:
                continue  # uncertain this pass: nothing advances, nothing is forgotten
            for ident in [i for i, a in absent.items() if a.get('kind') == kind and i not in gone]:
                absent.pop(ident)
            for ident in gone:
                absent.setdefault(ident, {'kind': kind, 'since': now})
            if gone and not _saving(names, entry['file']):
                seen[kind] = (gone, whole)
        if background:
            decided = {kind: ({i for i in gone if now - absent[i]['since'] >= CONFIRM_SECONDS}, whole)
                       for kind, (gone, whole) in seen.items()}
        else:
            decided = self._look_again(folder, seen, names) if seen else {}
        for kind, (ripe, whole) in decided.items():
            entry, gone = self.manifest['memory'][kind], seen[kind][0]
            ripe = sorted(ripe)
            if not ripe or whole and len(ripe) < len(gone):
                continue
            for ident in ripe:
                absent.pop(ident, None)
            entry['ids'] = [i for i in entry['ids'] if i not in ripe]
            if whole or len(ripe) > FORGET_WITHOUT_ASKING:
                token = secrets.token_hex(8)
                self.manifest['confirm'][token] = {'kind': kind, 'ids': ripe, 'since': now, 'whole': bool(whole)}
                what = 'the whole file' if whole else f'{len(ripe)} entries'
                self._event('forget_waiting', f'You deleted {what} from "{LAYER_FILES[kind]}". Pebble forgets '
                                              f'{len(ripe)} memor{"y" if len(ripe) == 1 else "ies"} once you confirm '
                                              'here.', layer=kind, id=token)
                self._changed('forget_confirm', id=token)
                continue
            forgotten += self._forget_many(ripe, kind)
        if forgotten:
            self._event('forgotten', f'Forgot {forgotten} memor{"y" if forgotten == 1 else "ies"} deleted from the Memory '
                                     'folder.')
        return forgotten

    def _look_again(self, folder, seen, names):
        """In a pass the person started: a layer's removals stand only if a second look CONFIRM_GAP later finds the
        same ones, with the folder listed, no iCloud placeholder and no atomic save in progress."""
        self.sleep(CONFIRM_GAP)
        listing = _listing(folder)
        if listing is None:
            return {}
        names2, cloud2 = listing
        decided = {}
        for kind, (gone, whole) in seen.items():
            entry = self.manifest['memory'][kind]
            if _saving(names | names2, entry['file']):
                continue
            again, whole2 = self._gone(kind, entry, folder / entry['file'], cloud2)
            if again:
                decided[kind] = (gone & again, whole and whole2)
        return decided

    def _forget_one(self, ident):
        """The host's forget path (App.memory_delete) without the per-record FTS merge, which _forget_many runs once."""
        if getattr(self.app, 'active', None):
            raise ValueError('Stop the current task before forgetting memories.')
        learning = getattr(self.app, 'learning', None)
        if learning is None:
            self.app.memory_delete(ident)
            return
        learning.forget(ident, compact=False)

    def _forget_many(self, idents, kind):
        done = 0
        for ident in idents:
            try:
                self._forget_one(ident)
                self.manifest['pending']['forget'].pop(ident, None)
                done += 1
            except ValueError as error:
                self.manifest['pending']['forget'][ident] = {'kind': kind, 'since': self.clock(),
                                                             'reason': str(error)[:200]}
        if done:
            memory = getattr(self.app, 'memory', None)
            for name in ('discard_recall_index', 'compact_search'):
                task = getattr(memory, name, None)
                if callable(task):
                    task()  # forgotten text leaves the trigram index and the FTS segments (V1-057)
            self._checkpoint()
        return done

    def _forget(self, ident, kind):
        return self._forget_many([ident], kind)

    def _reconcile_files(self, category, folder):
        listing = _listing(folder)
        if listing is None:
            # The folder itself is gone: copy again what the person had not deleted one by one.
            files = self.manifest['files'][category]
            self.manifest['files'][category] = {k: e for k, e in files.items() if e.get('dismissed')}
            return 0
        names, cloud = listing
        dismissed = 0
        for entry in self.manifest['files'][category].values():
            if entry.get('dismissed'):
                continue
            name = entry['file'].casefold()
            if name not in names and name not in cloud:
                entry['dismissed'] = True
                dismissed += 1
        return dismissed

    def _retry_pending(self):
        done = 0
        pending = self.manifest['pending']
        for cid, item in list(pending['trash'].items()):
            if not self.app.store.query('SELECT 1 FROM chats WHERE id=?', (cid,)) or cid in self.app.store.trashed():
                pending['trash'].pop(cid)
                continue
            try:
                self._trash(cid)
                pending['trash'].pop(cid)
                done += 1
                self._event('trashed', f'Moved "{scrub(item.get("title"))[:120]}" to Recently deleted.', chat_id=cid)
            except ValueError as error:
                item['reason'] = str(error)[:200]
        waiting = [i for i in pending['forget'] if self.app.store.query('SELECT 1 FROM memories WHERE id=?', (i,))]
        for ident in [i for i in pending['forget'] if i not in waiting]:
            pending['forget'].pop(ident)
        if waiting and not getattr(self.app, 'active', None):
            done += self._forget_many(waiting, None)
        return done

    # ---- Documents: refresh -----------------------------------------------------------------------

    def _refresh(self, root):
        self._folders(root)
        out = {}
        for name, task in (('conversations', lambda: self._refresh_conversations(root / 'Conversations')),
                           ('memory', lambda: self._refresh_memory(root / 'Memory')),
                           ('copies', lambda: self._refresh_copies(root)),
                           ('logs', lambda: self._refresh_logs(root / 'Logs'))):
            out[name] = self._step('documents:' + name, task)
        self.last['refresh'] = self.clock()
        return out

    def _folders(self, root):
        data_home.private_dir(root)
        data_home.refresh(root / 'README.md', documents_readme().encode())
        for category in DOCUMENT_CATEGORIES:
            folder = root / CATEGORY[category]['folder']
            data_home.private_dir(folder)
            data_home.refresh(folder / 'README.md', folder_readme(category).encode())

    def _remove_ours(self, path, cid):
        """Delete a conversation copy only if its first line names this conversation; True when it was removed."""
        match = CHAT_HEADER.match(_head(path) or '')
        if match and match.group(1) == cid and not os.path.islink(path):
            try:
                os.unlink(path)
                return True
            except FileNotFoundError:
                pass
        return False

    def _title_name(self, title):
        name = self._names.get(title)
        if name is None:
            if len(self._names) > 10_000:
                self._names.clear()
            name = self._names[title] = copy_name(title, fallback='Conversation')
        return name

    # V1-011: one grouped pass over messages instead of three correlated scans per conversation (the same rows).
    CONVERSATIONS = ("SELECT c.id,c.title,c.updated,coalesce(s.n,0) AS n,s.first,s.last FROM chats c LEFT JOIN "
                     "(SELECT chat_id,sum(role IN ('user','assistant')) AS n,min(created) AS first,max(created) AS last "
                     'FROM messages GROUP BY chat_id) s ON s.chat_id=c.id')

    def _refresh_conversations(self, folder):
        store = self.app.store
        trashed = store.trashed()
        waiting = self.manifest['pending']['trash']
        rows = self._read(self.CONVERSATIONS)
        live = {r['id']: r for r in rows if r['n'] and r['id'] not in trashed and r['id'] not in waiting
                and HEX32.fullmatch(r['id'] or '')}
        conversations = self.manifest['conversations']
        names_on_disk, cloud = _listing(folder) or (set(), set())
        for cid, entry in list(conversations.items()):
            if cid not in live:
                self._remove_ours(folder / entry['file'], cid)
                del conversations[cid]
                self.manifest['absent']['conversations'].pop(cid, None)
        names, used = {}, set()
        for cid in sorted(live, key=lambda c: (live[c]['first'] or live[c]['updated'] or 0, c)):
            row = live[cid]
            base = local_date(row['first'] or row['updated']) + ' ' + self._title_name(row['title'])
            name = base + '.md'
            if name.casefold() in used:
                name = f'{base} ({cid[:6]}).md'
            used.add(name.casefold())
            names[cid] = name
        written = 0
        for cid, row in live.items():
            name, entry = names[cid], conversations.get(cid)
            # V1-019: the index keeps a digest of the title, never the title itself (it may hold a pasted key).
            stamp = [row['updated'], row['n'], row['last'], sha256(str(row['title'] or '').encode())[:16]]
            path = folder / name
            if entry is not None:
                if entry['file'].casefold() not in names_on_disk:
                    continue  # missing: the reconcile decides (V1-014); never written back meanwhile
                if entry['file'] == name and entry.get('stamp') == stamp:
                    continue
            elif name.casefold() in cloud:
                continue  # an iCloud placeholder stands for it; never write over it
            text = self._render_conversation(cid, row)
            if text is None:
                continue  # its held drafts could not be checked: the previous copy stays, the next pass retries
            data = text.encode()
            digest = sha256(data)
            if not (entry and entry['file'] == name and entry.get('sha256') == digest):
                data_home.write_atomic(path, data)
                written += 1
            if entry and entry['file'] != name:
                self._remove_ours(folder / entry['file'], cid)
            conversations[cid] = {'file': name, 'sha256': digest, 'stamp': stamp, 'title': scrub(str(row['title'] or ''))[:120]}
        return written

    def _withheld(self, cid):
        """The held or withdrawn turn-end drafts of this conversation's runs (X20 F20-02), or None when they cannot be
        read. A message row holds only text that was shown, so this is defence in depth (merge i22): the copy is
        built with the same rule as the window routes. Only runs that held or withdrew a draft are read."""
        reader = getattr(self.app, '_withheld_texts', None)
        if not callable(reader):
            return set()
        try:
            rows = self.app.store.query(
                "SELECT id FROM runs r WHERE chat_id=? AND (EXISTS(SELECT 1 FROM run_events e WHERE e.run_id=r.id AND "
                "e.kind='reply_withheld') OR (json_valid(r.checkpoint) AND json_extract(r.checkpoint,'$.held_reply')))",
                (cid,))
            texts = set()
            for run in rows:
                texts |= reader(run['id'])
            return texts
        except Exception:
            return None

    def _render_conversation(self, cid, row):
        withheld = self._withheld(cid)
        if withheld is None:
            return None
        messages = self._read("SELECT id,role,content,created FROM messages WHERE chat_id=? AND role IN ('user','assistant') "
                              'ORDER BY created,id', (cid,))
        if withheld:
            hidden = getattr(self.app, 'WITHHELD_TEXT', '[A draft that was not shown as the reply.]')
            messages = [{**m, 'content': hidden} if m['role'] == 'assistant' and m['content'] in withheld else m
                        for m in messages]
        attached = {}
        for table in ('file_attachments', 'image_attachments'):
            try:
                for item in self._read(f'SELECT message_id,name FROM {table} WHERE chat_id=? AND message_id IS NOT NULL',
                                       (cid,)):
                    attached.setdefault(item['message_id'], []).append(copy_name(item['name'], 120, 'file'))
            except Exception:
                continue
        title = ' '.join(str(row['title'] or 'Conversation').split())
        lines = [f'<!-- pebble:conversation id={cid} -->', f'# {inert(title)}', '',
                 f'*Pebble conversation `{cid}`, started {local_time(row["first"])}, updated {local_time(row["last"])}. '
                 'This file is a copy: editing it changes nothing in Pebble. Delete it to move the conversation to '
                 "Pebble's Recently deleted for 30 days.*", '']
        for message in messages:
            who = 'You' if message['role'] == 'user' else 'Pebble'
            lines += ['---', '', f'**{who}** · {local_time(message["created"])}', '', inert(message['content']), '']
            if attached.get(message['id']):
                lines += ['*Attached: ' + ', '.join(attached[message['id']]) + '*', '']
        return '\n'.join(lines).rstrip() + '\n'

    def _memory_stamps(self, now):
        """V1-011: per layer, what its copy depends on, read in one grouped query: count, latest and summed update times."""
        stamps = {r['kind']: [r['n'], r['u'], r['t']] for r in self._read(
            'SELECT kind,count(*) AS n,max(updated) AS u,total(updated) AS t FROM memories WHERE expires IS NULL OR '
            'expires>? GROUP BY kind', (now,))}
        stamps['working'] = [self._read('SELECT count(*) AS n FROM checkpoints')[0]['n']]
        return stamps

    def _refresh_memory(self, folder):
        from .memory import KINDS
        now = self.clock()
        waiting = set(self.manifest['pending']['forget'])
        hidden = waiting | {i for c in self.manifest['confirm'].values() for i in c.get('ids') or []}
        held = {a.get('kind') for a in self.manifest['absent']['memory'].values()}
        layers = self.manifest['memory']
        listing = _listing(folder) or (set(), set())
        stamps = self._memory_stamps(now)
        mirror_id = self.manifest.get('mirror_id') or ''
        written = 0
        for kind, label in KINDS.items():
            name = LAYER_FILES[kind]
            entry = layers.get(kind)
            path = folder / name
            if kind in held:
                continue  # V1-004: the person's file stays as they saved it until the next pass confirms the deletion
            if entry and entry.get('ids') and not os.path.lexists(path):
                continue  # deleted after this pass's reconcile: the next pass decides
            if not entry and name.casefold() in listing[1]:
                continue
            asking = sum(1 for c in self.manifest['confirm'].values() if c.get('kind') == kind)
            stamp = [stamps.get(kind), sorted(hidden), asking]
            if entry and entry.get('stamp') == stamp and _signature(path) == [entry.get('size'), entry.get('mtime_ns')]:
                continue  # V1-011: nothing changed in Pebble or in the file
            rev = int(entry.get('rev') or 0) + 1 if entry else 1
            if kind == 'working':
                text, ids = _working_text(stamps['working'][0]), []
            else:
                total = (stamps.get(kind) or [0])[0]
                rows = self._read('SELECT id,content,scope,origin,created,updated,expires FROM memories WHERE kind=? AND '
                                  '(expires IS NULL OR expires>?) ORDER BY updated DESC,id LIMIT ?',
                                  (kind, now, MAX_MEMORY + len(hidden) + 1))
                own = kind == 'profile' and any(r['id'] == PEBBLE_MD_ID for r in rows)
                rows = [r for r in rows if r['id'] not in hidden and r['id'] != PEBBLE_MD_ID][:MAX_MEMORY]
                text = _memory_text(kind, label, rows, total, own, f'{mirror_id}.{rev}', asking)
                ids = [r['id'] for r in rows if MEMORY_ID.fullmatch(r['id'] or '')]
            data = text.encode()
            digest = sha256(data)
            data_home.write_atomic(path, data)
            written += 1
            previous = set(entry.get('ids') or []) if entry else set()
            before = (entry.get('added') or {}) if entry else {}
            layers[kind] = {'file': name, 'ids': ids, 'sha256': digest, 'rev': rev, 'stamp': stamp,
                            'added': {i: (before.get(i, 0) if i in previous else rev) for i in ids}}
            signature = _signature(path) or [None, None]
            layers[kind]['size'], layers[kind]['mtime_ns'] = signature
        return written

    def _unique(self, folder, category, name):
        taken = {e['file'].casefold() for e in self.manifest['files'][category].values()}
        stem, dot, ext = name.rpartition('.')
        if not dot or not stem:
            stem, ext = name, ''
        candidate, n = name, 2
        while candidate.casefold() in taken or os.path.lexists(folder / candidate):
            candidate = f'{stem} ({n}).{ext}' if ext else f'{stem} ({n})'
            n += 1
        return candidate

    def _refresh_copies(self, root):
        store = self.app.store
        budget = [COPY_BUDGET]
        trashed = store.trashed()
        out = {'attachments': 0, 'artifacts': 0, 'exports': 0}
        folder, files = root / 'Attachments', self.manifest['files']['attachments']
        sources = []
        for table, kind in (('file_attachments', 'file'), ('image_attachments', 'image')):
            columns = 'f.id,f.chat_id,f.name' + (',f.suffix,f.sha256' if kind == 'file' else '')
            try:
                sources += [(kind, r) for r in self._read(
                    f'SELECT {columns},(SELECT created FROM messages m WHERE m.id=f.message_id) AS created '
                    f'FROM {table} f WHERE f.message_id IS NOT NULL')]
            except Exception:
                continue
        for kind, row in sources:
            key = kind + ':' + str(row['id'])
            if key in files or row['chat_id'] in trashed or budget[0] <= 0:
                continue
            name = self._unique(folder, 'attachments', local_date(row['created'] or self.clock()) + ' '
                                + copy_name(row['name'], 100, 'file'))
            if kind == 'file':
                source = Path(self.app.home) / 'attachments' / (str(row['id']) + str(row['suffix'] or ''))
                data = _read_bytes(source)
                if data is None or (row.get('sha256') and sha256(data) != row['sha256']):
                    continue
            else:
                blob = store.query('SELECT data FROM image_attachments WHERE id=?', (row['id'],))
                data = bytes(blob[0]['data']) if blob and blob[0]['data'] is not None else None
                if data is None:
                    continue
            data_home.write_atomic(folder / name, data)
            budget[0] -= len(data)
            files[key] = {'file': name, 'sha256': sha256(data), 'chat': row['chat_id']}
            out['attachments'] += 1
        out['artifacts'] = self._refresh_artifacts(root / 'Artifacts', trashed)
        out['exports'] = self._refresh_exports(root / 'Exports', trashed, budget)
        return out

    def _refresh_artifacts(self, folder, trashed):
        files = self.manifest['files']['artifacts']
        written = 0
        try:
            # V1-011: the head revision comes with the list, so an unchanged artifact is never loaded again.
            rows = self._read('SELECT a.id,a.chat_id,a.kind,h.revision FROM artifacts a LEFT JOIN artifact_heads h ON '
                              'h.artifact_id=a.id ORDER BY a.created,a.id')
        except Exception:
            try:
                rows = self._read('SELECT id,chat_id,kind,NULL AS revision FROM artifacts ORDER BY created,id')
            except Exception:
                return 0
        for row in rows:
            if row['chat_id'] in trashed or row['kind'] == 'mcp_media':
                continue
            entry = files.get(row['id'])
            if entry and (entry.get('dismissed') or row['revision'] is not None and entry.get('revision') == row['revision']):
                continue
            try:
                artifact = self.app.artifacts.get(row['chat_id'], row['id'])
            except Exception:
                continue
            revision = artifact.get('revision')
            if entry and (entry.get('revision') == revision or not os.path.lexists(folder / entry['file'])):
                continue
            title = ' '.join(str(artifact.get('title') or 'Artifact').split())
            name = entry['file'] if entry else self._unique(
                folder, 'artifacts', local_date(artifact.get('created')) + ' ' + copy_name(title, 90, 'Artifact') + '.md')
            data = _artifact_text(artifact, title).encode()
            data_home.write_atomic(folder / name, data)
            files[row['id']] = {'file': name, 'sha256': sha256(data), 'revision': revision, 'chat': row['chat_id']}
            written += 1
        return written

    def _refresh_exports(self, folder, trashed, budget):
        files = self.manifest['files']['exports']
        written = 0
        try:
            rows = self._read('SELECT id,chat_id,receipt,created FROM export_records ORDER BY created')
        except Exception:
            return 0
        for row in rows:
            if row['id'] in files or row['chat_id'] in trashed or budget[0] <= 0:
                continue
            try:
                receipt = json.loads(row['receipt'])
            except (TypeError, ValueError):
                continue
            item = receipt.get('file') if isinstance(receipt, dict) else None
            if receipt.get('status') not in ('verified', 'unverified') or not isinstance(item, dict):
                continue
            stored = str(item.get('name') or '')
            if not stored or stored != safe_name(stored, 200, '') or not HEX32.fullmatch(str(row['chat_id'])) \
                    or not HEX32.fullmatch(str(row['id'])):
                continue
            data = _read_bytes(Path(self.app.home) / 'exports' / row['chat_id'] / row['id'] / stored)
            if data is None or sha256(data) != item.get('sha256'):
                continue
            name = self._unique(folder, 'exports', local_date(row['created']) + ' '
                                + copy_name(item.get('suggested_name') or stored, 100, 'Export'))
            data_home.write_atomic(folder / name, data)
            budget[0] -= len(data)
            files[row['id']] = {'file': name, 'sha256': sha256(data), 'chat': row['chat_id']}
            written += 1
        return written

    def _log_sources(self):
        return {'service.log': self.layout.root / data_home.LOGS / 'service.log',
                'backend.log': self.layout.root / data_home.LOGS / 'backend.log',
                'model.log': Path(self.app.home) / 'model.log'}

    def _refresh_logs(self, folder):
        logs = self.manifest['logs']
        if (self._icloud or {}).get('documents_synced'):
            # V1-058: troubleshooting logs never go to iCloud. Pebble's own earlier copies leave; nothing else does.
            removed = 0
            for name in self._log_sources():
                path = folder / name
                if (_head(path, 64) or '').startswith(LOG_HEADER) and not os.path.islink(path):
                    try:
                        os.unlink(path)
                        removed += 1
                    except OSError:
                        pass
            self.manifest['logs'] = {}
            return 0
        written = 0
        for name, source in self._log_sources().items():
            try:
                info = os.lstat(source)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            stamp = [info.st_size, info.st_mtime_ns]
            if logs.get(name) == stamp and os.path.lexists(folder / name):
                continue
            with open(source, 'rb') as reader:
                reader.seek(max(0, info.st_size - LOG_TAIL))
                tail = reader.read(LOG_TAIL).decode('utf-8', 'replace')
            header = (f'{LOG_HEADER}{min(info.st_size, LOG_TAIL) // 1024 or 1} KB of {name} as of '
                      f'{local_time(self.clock())}, with secret-shaped text removed (best effort). A copy; Pebble writes '
                      'it again as it runs.\n')
            data_home.write_atomic(folder / name, (header + scrub(tail)).encode())
            logs[name] = stamp
            written += 1
        return written

    # ---- Deletion: Pebble's own copies of a conversation and of a discarded export (V1-016) ----------------------

    def _chat_keys(self, cid):
        """The manifest keys of this conversation's copies, read from the database while its rows still exist."""
        keys = {c: set() for c in COPIED}
        artifacts = []
        for sql, category, prefix in (('SELECT id FROM file_attachments WHERE chat_id=?', 'attachments', 'file:'),
                                      ('SELECT id FROM image_attachments WHERE chat_id=?', 'attachments', 'image:'),
                                      ('SELECT id FROM artifacts WHERE chat_id=?', 'artifacts', ''),
                                      ('SELECT id FROM export_records WHERE chat_id=?', 'exports', '')):
            try:
                found = [str(r['id']) for r in self.app.store.query(sql, (cid,))]
            except Exception:
                continue
            keys[category] |= {prefix + i for i in found}
            if category == 'artifacts':
                artifacts = found
        return keys, artifacts

    def _records(self):
        """The current index (when it has a folder) and every retired one: (root, conversations, files) each."""
        out = []
        if self.manifest.get('root'):
            out.append((Path(self.manifest['root']), self.manifest['conversations'], self.manifest['files']))
        for record in self.manifest['retired']:
            out.append((Path(record['root']), record.setdefault('conversations', {}),
                        record.setdefault('files', {c: {} for c in COPIED})))
        return out

    def forget_chat(self, cid):
        """Remove Pebble's own copies of one conversation (its transcript, attachments, artifacts and exports) from the
        current copies folder and every retired one. A copy is removed only if it is a regular file that is still what
        Pebble wrote: the transcript by its first line, the others by their SHA-256; a file the person changed is
        theirs and stays. Called by App.delete_chat before the conversation's rows go (parent patch: delete_chat v2).
        Returns {'removed', 'kept', 'artifacts'} (artifact ids, for the window's own storage)."""
        keys, artifacts = self._chat_keys(cid)
        out = {'removed': 0, 'kept': 0, 'artifacts': artifacts}
        if not self.active:
            return out
        with self.lock:
            for root, conversations, files in self._records():
                entry = conversations.pop(cid, None)
                if entry and self._remove_ours(root / 'Conversations' / entry['file'], cid):
                    out['removed'] += 1
                for category in COPIED:
                    bucket = files.setdefault(category, {})
                    for key in [k for k, e in bucket.items() if k in keys[category] or isinstance(e, dict)
                                and e.get('chat') == cid]:
                        item = bucket.pop(key)
                        if item.get('dismissed'):
                            continue
                        removed = _remove_copy(root / CATEGORY[category]['folder'] / item['file'], item.get('sha256'))
                        out['removed' if removed else 'kept'] += removed is not None
            self.manifest['pending']['trash'].pop(cid, None)
            self.manifest['absent']['conversations'].pop(cid, None)
            self._save_manifest()
        return out

    def chat_deleted(self, cid, forgotten=None):
        """After App.delete_chat committed: tell the windows which artifacts went, so their own storage (quiz drafts
        under pebble.quiz.<artifact id>) can go too (V1-156)."""
        self._changed('deleted', chat_id=cid, deleted_artifacts=list((forgotten or {}).get('artifacts') or []))

    def forget_export(self, chat_id, export_id):
        """V1-016: a discarded export's Documents copies go with it (parent patch: the export_discard route)."""
        out = {'removed': 0, 'kept': 0}
        if not self.active:
            return out
        with self.lock:
            for root, _, files in self._records():
                item = files.setdefault('exports', {}).pop(str(export_id), None)
                if item and not item.get('dismissed'):
                    removed = _remove_copy(root / CATEGORY['exports']['folder'] / item['file'], item.get('sha256'))
                    out['removed' if removed else 'kept'] += removed is not None
            self._save_manifest()
        return out

    # ---- The Data & storage page (main window only; never the pet, a view or a model tool) -------

    def methods(self):
        return {
            'storage_summary': lambda **params: self.summary(),
            'storage_reveal': lambda category: self.reveal(category),
            'storage_delete': lambda category, confirm=False: self.delete(category, confirm),
            'storage_set': lambda documents_mirror=None, documents_folder='unchanged': self.set_storage(
                documents_mirror, documents_folder),
            'storage_settings_resolve': lambda id, accept: self.resolve_setting(id, accept),
            'storage_restore_chat': lambda chat_id: self.restore_chat(chat_id),
            'storage_purge_chat': lambda chat_id, confirm=False: self.purge_chat(chat_id, confirm),
            'storage_refresh': lambda **params: self.refresh_now(),
        }

    def decision_methods(self):
        """Phase 24 routes, main window only like methods() (parent patch: merged into App's routes, main.rs ALLOWED,
        tool_authz.NATIVE_ONLY; never the pet, a view or a model tool). Each is one deliberate choice of the person."""
        return {
            'storage_trash_chat': lambda chat_id: self.trash_chat(chat_id),
            'storage_trash_all': lambda confirm=False: self.trash_all(confirm),
            'storage_purge_all': lambda confirm=False: self.purge_all(confirm),
            'storage_forget_resolve': lambda id, accept: self.resolve_forget(id, accept),
            'storage_cleanup_legacy': lambda confirm=False: self.cleanup_legacy(confirm),
        }

    def _require(self):
        if not self.active:
            raise ValueError('This Pebble data folder is managed by a test or a tool, so Data & storage is not available.')

    def _paths(self):
        root, _ = self.mirror_root()
        layout = self.layout
        data = Path(self.app.home)
        legacy = layout.legacy if layout.legacy and os.path.isdir(layout.legacy) and not os.path.islink(layout.legacy) \
            else None
        paths = {'home': layout.root, 'data_folder': data, 'documents': root,
                 'backups': layout.root / data_home.BACKUPS, 'runtime': layout.root / data_home.RUNTIME,
                 'cache': layout.root / data_home.CACHE, 'service_logs': layout.root / data_home.LOGS,
                 'upgrade_backups': data / 'upgrade-backups', 'old_folder': legacy,
                 'install_backups': Path.home() / 'PebbleBackups',
                 'web_storage': Path.home() / 'Library' / 'WebKit' / 'com.pebble.local'}
        for category in DOCUMENT_CATEGORIES:
            paths[category] = root / CATEGORY[category]['folder'] if root else None
        return paths

    def summary(self):
        self._require()
        deadline = time.monotonic() + 3.0
        paths = self._paths()
        data = paths['data_folder']
        categories = []
        upgrade = _measure(paths['upgrade_backups'], deadline)
        for category in CATEGORIES:
            cid = category['id']
            if cid == 'database':
                sizes = [_measure(data / (data_home.DATABASE + s), deadline) for s in ('', '-wal', '-shm')]
                size = (sum(s[0] for s in sizes), sum(s[1] for s in sizes), any(s[2] for s in sizes))
                path = data
            elif cid == 'data':
                whole = _measure(data, deadline)
                base = sum(_measure(data / (data_home.DATABASE + s), deadline)[0] for s in ('', '-wal', '-shm'))
                size, path = (max(0, whole[0] - base - upgrade[0]), max(0, whole[1] - upgrade[1]), whole[2]), data
            elif cid == 'upgrade_backups':
                size, path = upgrade, paths['upgrade_backups']
            elif cid == 'config':
                parts = [_measure(self.layout.root / n, deadline) for n in (
                    data_home.SETTINGS, data_home.SCHEMA, data_home.INSTRUCTIONS, data_home.MCP, data_home.README,
                    data_home.SKILLS)]
                size, path = (sum(p[0] for p in parts), sum(p[1] for p in parts), any(p[2] for p in parts)), self.layout.root
            else:
                path = paths.get(cid)
                size = _measure(path, deadline) if path else (0, 0, False)
            categories.append({'id': cid, 'label': category['label'], 'place': category['place'],
                               'path': str(path) if path else None, 'bytes': size[0], 'files': size[1],
                               'partial': size[2], 'what': category['what'], 'safe_to_delete': category['safe'],
                               'deletable': category['deletable'], 'if_deleted': category['if_deleted'],
                               'revealable': cid in REVEALED and path is not None and os.path.isdir(path)})
        root, reason = self.mirror_root()
        storage = settings_file.applied(self.app.store)[settings_file.STORAGE]
        trashed = self.app.store.trashed()
        with self.lock:
            m = self.manifest
            pending = {'trash': len(m['pending']['trash']), 'forget': len(m['pending']['forget']),
                       'waiting': {'conversations': len(m['absent']['conversations']),
                                   'memory': len(m['absent']['memory'])}}
            confirm = [{'id': token, 'kind': c.get('kind'), 'file': LAYER_FILES.get(c.get('kind'), ''),
                        'count': len(c.get('ids') or []), 'whole': bool(c.get('whole')), 'since': c.get('since')}
                       for token, c in sorted(m['confirm'].items(), key=lambda kv: kv[1].get('since') or 0)]
            pebble = dict(m.get('pebble_md') or {})
            icloud_seen = (m.get('icloud') or {}).get('first_seen')
            events = list(self.events[-12:])
            problems = dict(self.problems)
        migration = {k: self.migration.get(k) for k in (
            'state', 'reason', 'message', 'moved_at', 'moved_from', 'legacy_changed', 'warning', 'checked_at',
            'left_in_old_folder', 'anchored', 'files', 'bytes') if self.migration.get(k) is not None}
        icloud = icloud_status(root)
        return {
            'available': True,
            'home': {'path': str(self.layout.root), 'data': str(data), 'default': self.layout.is_default,
                     'on_old_folder': not self.layout.structured or Path(data) != self.layout.data},
            'migration': migration,
            'documents': {'enabled': root is not None, 'reason': reason, 'path': str(root) if root else None,
                          'default_path': str(Path.home() / 'Documents' / 'Pebble') if self.layout.is_default else None,
                          'custom': storage['documents_folder'] is not None, 'icloud': icloud,
                          'icloud_first_seen': icloud_seen, 'logs_copied': root is not None and not icloud['documents_synced'],
                          'last_refresh': self.last.get('refresh'), 'last_reconcile': self.last.get('reconcile'),
                          'pending': pending, 'confirm': confirm, 'confirm_seconds': CONFIRM_SECONDS,
                          'poll_seconds': self.poll},
            'categories': categories,
            'settings_file': self.settings.status(),
            'pebble_md': {'path': str(self.layout.root / data_home.INSTRUCTIONS), 'status': pebble.get('status'),
                          'message': pebble.get('message'), 'used_chars': pebble.get('used_chars'),
                          'total_chars': pebble.get('total_chars'), 'limit': PEBBLE_MD_LIMIT},
            'recently_deleted': [{'chat_id': cid, 'title': e.get('title') or '', 'deleted': e.get('deleted'),
                                  'origin': e.get('origin'), 'delete_after': (e.get('deleted') or 0) + 30 * 86400}
                                 for cid, e in sorted(trashed.items(), key=lambda kv: -(kv[1].get('deleted') or 0))][:200],
            'events': events, 'problems': problems,
        }

    def reveal(self, category):
        self._require()
        if category in EDITED:
            return self._edit(category)
        paths = self._paths()
        if category not in paths or category in ('data_folder', 'upgrade_backups', 'web_storage'):
            raise ValueError('Choose a folder to show.')
        path = paths[category]
        if path is None:
            if category == 'old_folder':
                raise ValueError('There is no old app-data folder on this Mac.')
            raise ValueError('Copies in Documents are off, so that folder does not exist.')
        if not os.path.isdir(path) and category in DOCUMENT_CATEGORIES + ('documents',):
            with self.lock:
                self._folders(paths['documents'])
        if not os.path.isdir(path) or os.path.islink(path):
            raise ValueError('That folder does not exist yet.')
        self.opener(path)
        return {'revealed': str(path)}

    def _edit(self, category):
        """V1-101: open PEBBLE.md or settings.json in the default text editor. Pebble reads them as it always does."""
        path = self.layout.root / EDITED[category]
        if category == 'pebble_md' and not os.path.lexists(path):
            data_home.write_atomic(path, data_home.PEBBLE_MD_TEMPLATE.encode())
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError(f'{EDITED[category]} does not exist yet.')
        self.editor(path)
        return {'opened': str(path)}

    def delete(self, category, confirm=False):
        """Delete one safe category after the person confirmed it in the page. READMEs stay."""
        self._require()
        if category not in DELETABLE:
            raise ValueError('Only attachments, artifacts, exports, downloads, logs and the cache can be deleted here.')
        if confirm is not True:
            raise ValueError('Confirm the deletion first.')
        paths = self._paths()
        with self.lock:
            if category == 'cache':
                folders = [paths['cache']]
            else:
                if paths[category] is None:
                    raise ValueError('Copies in Documents are off, so there is nothing to delete there.')
                folders = [paths[category]]
            removed = freed = 0
            for folder in folders:
                r, f = _empty_folder(folder, keep=('README.md',))
                removed, freed = removed + r, freed + f
            if category in COPIED:
                for entry in self.manifest['files'][category].values():
                    entry['dismissed'] = True
            if category == 'logs':
                for source in self._log_sources().values():
                    freed += _truncate(source)
                self.manifest['logs'] = {}
            self._save_manifest()
            self._event('deleted', f'Deleted {CATEGORY[category]["label"].lower()} ({removed} item(s)).')
        return {'category': category, 'removed': removed, 'freed_bytes': freed}

    def set_storage(self, documents_mirror=None, documents_folder='unchanged'):
        """The person's own change in the main window: applied at once (the app is the confirmation)."""
        self._require()
        current = settings_file.applied(self.app.store)[settings_file.STORAGE]
        value = dict(current)
        if documents_mirror is not None:
            if type(documents_mirror) is not bool:
                raise ValueError('Choose on or off.')
            value['documents_mirror'] = documents_mirror
        if documents_folder != 'unchanged':
            value['documents_folder'] = documents_folder
        value = settings_file.validate(settings_file.STORAGE, value)
        with self.lock:
            self._apply_setting(settings_file.STORAGE, value)
        self._event('storage', 'Copies in Documents are ' + ('on.' if value['documents_mirror'] else 'off. Existing '
                                                                   'files stay where they are.'))
        return self.refresh_now()

    def resolve_setting(self, id, accept):
        self._require()
        if type(accept) is not bool or not isinstance(id, str):
            raise ValueError('Choose apply or dismiss.')
        result = self.settings.resolve(id, accept, self._apply_setting)
        self._event('settings', ('Applied' if accept else 'Dismissed') + f' the settings.json change to {result["key"]}.')
        return self.summary()

    def restore_chat(self, chat_id):
        self._require()
        if not isinstance(chat_id, str) or not HEX32.fullmatch(chat_id):
            raise ValueError('Conversation not found.')
        with self.lock:
            if not self.app.store.untrash_chat(chat_id):
                raise ValueError('That conversation is not in Recently deleted.')
            self.manifest['pending']['trash'].pop(chat_id, None)
            self._event('restored', 'Restored a conversation from Recently deleted.', chat_id=chat_id)
        self._changed('recently_deleted')
        return {'restored': chat_id}

    def purge_chat(self, chat_id, confirm=False):
        self._require()
        if confirm is not True:
            raise ValueError('Confirm the deletion first.')
        if not isinstance(chat_id, str) or chat_id not in self.app.store.trashed():
            raise ValueError('That conversation is not in Recently deleted.')
        self.app.delete_chat(chat_id)
        self.app.store.untrash_chat(chat_id)
        self._checkpoint()
        self._changed('recently_deleted')
        return {'deleted': chat_id}

    def refresh_now(self):
        self._require()
        self.tick()
        return self.summary()

    def trash_chat(self, chat_id):
        """D2 (V1-063): the main window's Delete moves a conversation to Recently deleted (origin 'app'), restorable
        for 30 days; Delete now in Data & storage (storage_purge_chat) or the 30-day purge deletes it for good."""
        self._require()
        if not isinstance(chat_id, str) or not HEX32.fullmatch(chat_id):
            raise ValueError('Conversation not found.')
        with self.lock:
            entry = self._trash(chat_id, 'app')
            self.manifest['pending']['trash'].pop(chat_id, None)
            self._event('trashed', 'Moved a conversation to Recently deleted.', chat_id=chat_id)
        self._changed('recently_deleted')
        return {'trashed': chat_id, 'delete_after': entry['deleted'] + 30 * 86400}

    def trash_all(self, confirm=False):
        """V1-061: Delete all conversations moves every listed conversation to Recently deleted, after the person
        confirmed. One that runs a task, its commands or an active schedule stays, with the reason."""
        self._require()
        if confirm is not True:
            raise ValueError('Confirm the deletion first.')
        from .store import LISTED_CHATS
        moved, kept = 0, []
        with self.lock:
            for row in self._read(LISTED_CHATS):
                try:
                    self._trash(row['id'], 'app')
                    moved += 1
                except ValueError as error:
                    kept.append({'chat_id': row['id'], 'title': row['title'] or '', 'reason': str(error)[:200]})
            if moved:
                self._event('trashed', f'Moved {moved} conversation(s) to Recently deleted.')
        self._changed('recently_deleted')
        return {'moved': moved, 'kept': kept}

    def purge_all(self, confirm=False):
        """V1-061: Delete all now… in Recently deleted: every conversation there is deleted for good through
        App.delete_chat, after the person confirmed. One the full checks refuse stays, with the reason."""
        self._require()
        if confirm is not True:
            raise ValueError('Confirm the deletion first.')
        deleted, kept = 0, []
        for cid in list(self.app.store.trashed()):
            try:
                self.app.delete_chat(cid)
            except ValueError as error:
                kept.append({'chat_id': cid, 'reason': str(error)[:200]})
                continue
            self.app.store.untrash_chat(cid)
            deleted += 1
        if deleted:
            self._checkpoint()
            self._event('purged', f'Deleted {deleted} conversation(s) for good.')
        self._changed('recently_deleted')
        return {'deleted': deleted, 'kept': kept}

    def resolve_forget(self, id, accept):
        """V1-004: the person's answer to a forget that waits for them (more than 3 entries, or a whole layer file,
        deleted from Documents/Memory). Forget goes through the host's forget path; Keep writes the entries back."""
        self._require()
        if type(accept) is not bool or not isinstance(id, str) or not TOKEN.fullmatch(id):
            raise ValueError('Choose forget or keep.')
        with self.lock:
            request = self.manifest['confirm'].get(id)
            if request is None:
                raise ValueError('That question is no longer open.')
            if accept:
                if getattr(self.app, 'active', None):
                    raise ValueError('Stop the current task before forgetting memories.')
                live = [i for i in request['ids'] if self.app.store.query('SELECT 1 FROM memories WHERE id=?', (i,))]
                done = self._forget_many(live, request.get('kind'))
                self._event('forgotten', f'Forgot {done} memor{"y" if done == 1 else "ies"} you deleted from '
                                         f'"{LAYER_FILES.get(request.get("kind"), "Memory")}".')
            else:
                self._event('kept', f'Kept the memories deleted from "{LAYER_FILES.get(request.get("kind"), "Memory")}"; '
                                    'their entries come back in the file.')
            self.manifest['confirm'].pop(id, None)
            self._save_manifest()
        self._changed('documents')
        return self.refresh_now()

    def cleanup_legacy(self, confirm=False):
        """V1-017: remove the old per-item Keychain copies of sign-ins and keys that Pebble kept for going back to a
        version before the vault (Vault.cleanup_legacy). Counts only; after it, such a version needs a new sign-in."""
        self._require()
        if confirm is not True:
            raise ValueError('Confirm first.')
        report = self.vault().cleanup_legacy()
        totals = {k: sum(int((r or {}).get(k) or 0) for r in report.values()) for k in ('removed', 'absent', 'failed')}
        self._event('sign_in_copies', f'Removed {totals["removed"]} old sign-in cop{"y" if totals["removed"] == 1 else "ies"} '
                                      'from the Keychain.' + (f' {totals["failed"]} could not be removed; try again.'
                                                              if totals['failed'] else ''))
        return {**totals, 'services': {name: dict(r) for name, r in report.items()}}


def _read_bytes(path, limit=64 << 20):
    try:
        fd = os.open(path, os.O_RDONLY | data_home._NOFOLLOW | data_home._CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with os.fdopen(fd, 'rb') as source:
            fd = None
            return source.read(limit + 1)
    finally:
        if fd is not None:
            os.close(fd)


def _remove_copy(path, digest):
    """Unlink a copy only while it is a regular file with the SHA-256 Pebble wrote: True removed, False kept (changed or
    unreadable), None when it is already gone."""
    if not os.path.lexists(path):
        return None
    data = _read_bytes(path, 1 << 30)
    if data is None or not digest or sha256(data) != digest or os.path.islink(path):
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    return True


def _empty_folder(folder, keep=()):
    removed = freed = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0, 0
    for entry in entries:
        if entry.name in keep:
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                freed += _measure(entry.path, time.monotonic() + 2.0)[0]
                shutil.rmtree(entry.path)
            else:
                freed += info.st_size if stat.S_ISREG(info.st_mode) else 0
                os.unlink(entry.path)
            removed += 1
        except OSError:
            continue
    return removed, freed


def _truncate(path):
    try:
        fd = os.open(path, os.O_WRONLY | data_home._NOFOLLOW | data_home._CLOEXEC)
    except OSError:
        return 0
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return 0
        os.ftruncate(fd, 0)  # writers append, so they continue at the new end
        return info.st_size
    finally:
        os.close(fd)


def _working_text(count):
    return ('<!-- pebble:memory-layer working -->\n# Working context\n\n'
            f'*Working context is each conversation\'s private task state: its goal, plan and pending steps. It is not '
            f'copied here. Pebble has {count} working record{"" if count == 1 else "s"}; clear them in Pebble -> '
            'Settings -> Memory.*\n')


def _memory_text(kind, label, rows, total, own, version=None, asking=0):
    origins = {'user': 'you saved it', 'direct-user': 'you asked Pebble to remember it', 'curated': 'built into Pebble',
               'conversation': 'learned from a conversation'}
    lines = [f'<!-- pebble:memory-layer {kind} -->']
    if version:
        lines.append(f'<!-- pebble:memory-version {version} -->')
    lines += [f'# {label}', '',
              f'*What Pebble remembers in its {kind} layer: {total} entr{"y" if total == 1 else "ies"}'
              + (f', the {len(rows):,} most recent shown' if total > len(rows) else '') + '. This file is a copy: editing '
              "an entry's text changes nothing. Delete an entry (its whole block, including its `pebble:memory` line) or "
              'this file to make Pebble forget it. Pebble acts once it is sure (after a minute, or at once with Update '
              'copies now in Settings -> Data & storage), asks you there before forgetting more than 3 entries or a '
              'whole file, and then keeps a note so it does not learn '
              'it again from the same source. Keep the `pebble:memory-version` line: without it Pebble cannot tell which '
              'version you edited, so it forgets nothing.*', '']
    if asking:
        lines += [f'*Entries you deleted here wait for your answer in Pebble -> Settings -> Data & storage, so they are '
                  'not listed.*', '']
    if own:
        lines += ['*Your PEBBLE.md is also used here. Edit or empty ~/.pebble/PEBBLE.md to change it.*', '']
    for row in rows:
        scope = 'personal' if row['scope'] == 'global' else 'project'
        detail = f'{local_date(row["updated"])} · {scope} · {origins.get(row["origin"], "recorded by Pebble")}'
        if row['expires']:
            detail += f' · until {local_date(row["expires"])}'
        lines += [f'## {detail}', '', inert(row['content']).strip(), f'<!-- pebble:memory id={row["id"]} -->', '']
    if not rows:
        lines += ['*Nothing here yet.*', '']
    return '\n'.join(lines).rstrip() + '\n'


def _artifact_text(artifact, title):
    data = artifact.get('data')
    body = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) if data is not None else '{}'
    kind = str(artifact.get('kind') or 'artifact')
    return '\n'.join([
        f'<!-- pebble:artifact id={artifact.get("id")} -->', f'# {inert(title)}', '',
        f'*A {kind} made in Pebble (revision {artifact.get("revision")}, {local_time(artifact.get("updated") or artifact.get("created"))}). '
        'This file is a copy: editing it changes nothing in Pebble. Quiz answers are never included.*', '',
        # A zero-width space (U+200B) breaks a ``` run in the content, so it cannot close the code block early.
        '```json', inert(body).replace('```', '`\u200b``'), '```', ''])


def documents_readme():
    rows = ''.join(f'| `{c["folder"]}/` | {c["what"]} | {c["safe"]} | {c["if_deleted"]} |\n'
                   for c in CATEGORIES if c['place'] == 'documents')
    return f"""# Pebble

This folder holds readable copies of what you make with Pebble: conversations, what Pebble remembers, attachments,
artifacts, exports, downloads and logs. Pebble keeps them up to date. They are copies: editing the text of a file here
changes nothing in Pebble, and Pebble never reads these files back as instructions or as information. Only deleting
some of them changes Pebble, as the table says. Secret-shaped text such as API keys, tokens and passwords is removed
from the copies and their file names on a best-effort basis. Pebble's own tools cannot open or change this folder.

Pebble's settings and its database are in the hidden folder `~/.pebble` (see the README there). If your Documents
folder syncs with iCloud Drive, these copies sync too; Pebble's database never does.

| Folder | What it is | Safe to delete? | What happens if you delete it |
| --- | --- | --- | --- |
{rows}
- Deleting a whole folder, rather than files inside it, changes nothing in Pebble: Pebble makes the folder again.
- Moving a conversation or memory file out of its folder counts as deleting it. To keep a copy elsewhere, copy it
  (hold Option while you drag) instead.
- Pebble acts on a deletion only when it is sure: after it has lasted a minute, or at once when you choose Update
  copies now in Pebble (it checks twice, a moment apart). A file that is there but cannot be read, that iCloud keeps
  only online, or that an app is still saving never counts as deleted.
- To stop these copies, turn off "Copies in Documents" in Pebble -> Settings -> Data & storage. Existing files stay.
- Recently deleted conversations are in Pebble -> Settings -> Data & storage, where you can restore them.
"""


def folder_readme(category):
    texts = {
        'conversations': "Each conversation with Pebble as one Markdown file, named by the day it started and its title, "
                         "and rewritten whenever the conversation changes. Only your messages and Pebble's replies are "
                         "included, with secret-shaped text removed (best effort). Editing a file changes nothing in "
                         "Pebble and is overwritten at the next change. Deleting a file moves that conversation to "
                         "Pebble's Recently deleted once Pebble is sure (after a minute, or at once with Update copies "
                         "now), where you can restore it for 30 days.",
        'memory': "What Pebble remembers, one file per memory layer. Each entry ends with a `pebble:memory` line. Editing "
                  "an entry changes nothing; deleting an entry (or a whole file) makes Pebble forget it once it is sure "
                  "(after a minute, or at once with Update copies now), "
                  "and Pebble asks you in Settings -> Data & storage first when it is more than 3 entries or a whole "
                  "file. Pebble then keeps a note so it does not learn it again from the same source. Saving an older "
                  "copy of a file forgets nothing Pebble added since. Working context is private task state and is "
                  "only counted here.",
        'attachments': "Copies of the files and images you attached, named by the day you sent them. Deleting a copy "
                       "only removes the copy: the conversation keeps its attachment and Pebble does not copy it again.",
        'artifacts': "Dashboards, quizzes and app demos Pebble made, one Markdown file each with their content. Quiz "
                     "answers are never written here. Deleting a copy only removes the copy.",
        'exports': "Copies of the exports Pebble rendered (PDF, Word, Excel, CSV or web page). Deleting a copy only "
                   "removes the copy; a file you saved elsewhere with Save stays.",
        'downloads': "Files downloaded by Pebble's own browser. They are yours: deleting one simply deletes it.",
        'logs': "The last part of Pebble's logs, with secret-shaped text removed (best effort), for troubleshooting. "
                "They are not copied while your Documents folder syncs with iCloud Drive; the full logs are in "
                "~/.pebble/logs. Deleting them is safe; Pebble writes new ones as it runs.",
    }
    label = CATEGORY[category]['label']
    return f'# {label}\n\n{texts[category]}\n'
