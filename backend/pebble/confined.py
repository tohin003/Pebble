"""Phase 46 (1.9.28, D184 and D189, the person: "Ask only for risky steps"): in Auto mode a command confined to the
project runs without a card, with a copy of the project behind it.

D184: "a sandboxed command without network runs without a card. Pebble makes an instant APFS copy of the project
first, checks afterwards what changed, and puts back anything the person said to keep." D189: the copies may use "up
to 3 GB a task"; each is deleted when its task ends, and a project too big for this asks before risky commands as before.

- `shape`: why an inspected RunCommand (terminal.inspect_command plus App's typed plan) is not confined, or None. A
  confined command has no network; runs in the project's sandbox (terminal.run_command: writes only to the project and
  its own temporary folder, programs only from system, toolchain and project paths, .git and credentials closed, its
  process group killed at the end); is no reviewed NAME=value or exec bit (T2), installer, network client, install or
  publishing step, or background process; and names no path outside the project. App keeps the card for anything a
  hook or AgentGuard holds, and outside Auto.
- `Copies`: before the command, an APFS clone (clonefile) of the project without .git and caches (requirements.ignored)
  in Pebble's data folder, which no command's sandbox can read or write; after it, what it added, changed and removed
  (lstat: size, mtime, the kernel's ctime, mode, inode); what the request's ledger keeps unchanged (its `unchanged`
  files and folders, with the completion gate's own leftover exception) put back from the clone, with a notice for the
  model and the person; and an Undo receipt that restores the command's changes from the clone (App.undo). A project
  on another volume, of more than MAX_FILES entries, or whose copies would pass BUDGET asks as before. Restores and
  removals walk the project folder by folder without following a link, so a link a command left never leads outside.
- `end_run`: when a task ends (runner.settle_run) its clones stay a day, so its Undo still works (Phase 46, 1.9.29, D209,
  the person: "keep Auto mode's undo copies for a day"); `sweep` then removes them, and its receipts say so.

A request that keeps the whole project unchanged already runs its commands without write access (b-new G3), so they
need no copy, and a command cannot reach .git, so a kept branch (kept_ref) is never a command's to move.
PEBBLE_AUTO_RISKY_ONLY=0 turns this off: Auto asks for every such command and Git change as 1.9.27 did.

Phase 46 (1.9.28, the w13b security review): the card was the only human check of these commands, and the sandbox did
not contain everything, so the card-free path adds what the card stood for:
- F1: its profile denies writes in the folders where later runs load code (CODE_PARTS: dependencies, environments,
  bytecode and tool caches, at any depth; an install, which must write there, keeps its card) and to bytecode files
  anywhere (Python imports a .pyc that has no source beside it), and the walk skips them since they cannot change. A
  command refused such a write keeps its card when it runs again. The planted-injection
  check also reads the code of -c/-e and the recipes of the make target or package.json script a command names
  (inner_words); a make target Pebble cannot read keeps its card, and so does every command of a task that read text
  aimed at AI assistants (requirements.planted).
- F2, F3: it runs under Pebble's containment anchor (Contained, containment.py), only once the containment self-test
  passed on this Mac (App.containment_status): the anchor's child sets its rlimits, the host stops it when its process
  tree or measured memory passes its bounds, and its end sweeps the whole sandbox instance, so nothing it started
  (setsid, a double fork) outlives it or writes after the after-walk.
- F4: a kept file whose folder a command replaced with a link or a file is put back with its folder.
- F5 (a note): such a command may read world-readable system files (sandbox_profiles.SYSTEM_READ, e.g. /etc/hosts) and
  copy them into the project, as an approved one could; publishing them still asks (a push keeps its card), and the
  person's home, credentials and .git stay closed to it."""

import ctypes
import errno
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

from . import requirements
from .policy import PolicyError

SWITCH = 'PEBBLE_AUTO_RISKY_ONLY'
BUDGET = 3_000_000_000  # D189: "up to 3 GB a task"
# Phase 46 (1.9.29, D209, the person: keep Auto mode's undo copies for a day): an ended task's copies stay this long, so
# its Undo still works the next morning; together they hold at most KEPT_BUDGET (one task's), the oldest going first
KEEP_SECONDS = 86_400
KEPT_BUDGET = BUDGET
ENDED = 'ended'  # the stamp in a task's clones folder: when it ended (a copy id is 32 hex characters, never this)
MAX_FILES = 20_000  # entries walked before and after a command (companion_shell.SNAPSHOT_FILES)
SHOWN = 8  # paths a notice names
CLONE_NOFOLLOW, AT_FDCWD = 0x0001, -2
# terminal.inspect_command's dangerous executables that install or reach another machine: they keep their card (the
# others, rm, rmdir, mv, python3, node, ruby, perl and make, run confined; test_confined_commands_1928 holds the split)
OUTSIDE = frozenset({'install', 'brew', 'pip', 'pip3', 'npm', 'npx', 'yarn', 'pnpm', 'uv', 'cargo', 'curl', 'wget', 'ssh',
                     'scp', 'docker'})
INSTALL_MODULES = frozenset({'pip', 'ensurepip', 'pipx', 'poetry', 'twine', 'uv', 'conda', 'venv', 'virtualenv'})
PUBLISH_WORDS = frozenset({'install', 'publish', 'deploy', 'release', 'upload', 'push'})
# F1 (the w13b security review): folders where later runs load code (dependencies, environments, bytecode, tool caches;
# requirements.IGNORED_PARTS' code-bearing ones and more). A card-free command may not write there. The data caches
# IGNORED_PARTS also skips (.pytest_cache, .mypy_cache, .ruff_cache, .hypothesis) hold data a tool reads, never code
# that runs, and stay writable and unlisted.
CODE_PARTS = ('node_modules', '__pycache__', '.venv', 'venv', 'vendor', '.cache', '.tox', '.nox', 'site-packages',
              'bower_components')
CODE_SUFFIXES = ('pyc', 'pyo')  # requirements.IGNORED_SUFFIXES' bytecode, which the walk skips at any path
# Phase 46 (1.9.29, golden held-out recovery, D212: runs stopped right after one failed command that printed a lock with
# "operation not permitted"): "it then waits for the person's approval" read as a reason to stop and hand the task back,
# where running the same command again is the way on (its card asks the person; approved, it may write there).
CODE_NOTE = ('Pebble keeps the places where later runs load code ({parts}) closed to commands that run without the '
             "person's approval, and this command was refused a write there. If it must write there (its own lock, "
             'cache or state files, say), run the same command again now: Pebble then asks the person to approve it, and '
             'once approved it may write there. This refusal alone is no reason to stop.')
# F2: a card-free command's bounds (command_plans.RESOURCES' defaults): the anchor's child sets CPU seconds, the largest
# file and a process cap; the host stops it, through the anchor, at PROCESSES members or MEMORY_BYTES measured footprint
PROCESSES, MEMORY_BYTES, CPU_SECONDS, FSIZE_BYTES = 64, 2048 << 20, 62, 256 << 20
SWEEP_SECONDS = 12.0  # the anchor's sweep (containment.SWEEP_DEADLINE) and its exit
RESOURCE_NOTE = ('Pebble stopped this command because it {what} (its limit for commands that run without approval), '
                 'and stopped every process it started: the output above is what it printed until then, and files it was '
                 'writing may be incomplete. Check what it left, then find what grew without bound (a loop that starts '
                 'processes or keeps data) before running it again.')
LIMITS = {'processes': f'started more than {PROCESSES} processes at once', 'memory': 'used more than 2 GB of memory'}
MAKEFILE_BYTES, RECIPE_TARGETS = 65_536, 32  # what inner_words reads of a Makefile or package.json
WRITE_REFUSED = re.compile(r'operation not permitted|permission denied|read-only file system|\bE(?:PERM|ACCES|ROFS)\b',
                           re.I)  # app.WRITE_REFUSED's words
# a task in one of these states has not ended: its clones stay (the person may still undo while it waits or is paused)
OPEN = frozenset({'queued', 'running', 'retry_wait', 'waiting_input', 'waiting_approval', 'paused'})
KEPT_NOTE = ('The request says to leave {what} unchanged, and this command {did}. Pebble put {them} back from the copy '
             'it made just before the command, so {they_are} as {they_were}. Do not change {them} another way; if the '
             'task needed {them} changed, say so in your reply instead of reporting it done.')
FAILED_NOTE = (' Pebble could not put back {paths} ({why}): restore {them} yourself, or say in your reply that {they_are} '
               'not as the request keeps {them}.')


def enabled():
    return os.environ.get(SWITCH, '1') != '0'


def shape(data, root):
    """Why an inspected RunCommand is not confined (a few words for the journal), or None when it is."""
    argv = data.get('argv') or []
    if data.get('network') is not False:
        return 'network'
    if data.get('asynchronous'):
        return 'background process'
    if data.get('env') or data.get('effect'):
        return 'reviewed NAME=value or exec bit'
    plan = data.get('plan')
    if plan is not None:
        step = plan['steps'][0]
        if (plan['pipeline'] != 'none' or plan['network']['mode'] != 'none' or plan['writable'] not in ([], ['.'])
                or plan['process_model'] != 'tree' or plan['interactive'] or step['env']):
            return 'plan outside the project sandbox'
    if not argv:
        return 'no command'
    name = Path(argv[0]).name.casefold()
    words = [arg.casefold() for arg in argv[1:]]
    if name in OUTSIDE:
        return 'installs or reaches another machine'
    module = words[words.index('-m') + 1].split('.')[0] if '-m' in words[:-1] else None
    if module in INSTALL_MODULES or any(word in PUBLISH_WORDS for word in words):
        return 'installs or publishes'
    if (name == 'make' or name == 'node' and '--run' in words) and _recipes(argv, root) is None:
        return 'a make target or package script Pebble cannot read'  # F1: its payload would be unchecked
    base = Path(root)
    for arg in argv[1:]:
        for text in (arg, arg.split('=', 1)[1] if '=' in arg else ''):
            if text.startswith('~'):
                return 'a path outside the project'
            if text.startswith('/') and text != '/dev/null' or '..' in PurePosixPath(text).parts:
                try:
                    inside = (base / text).resolve(strict=False).is_relative_to(base)  # an absolute text replaces base
                except (OSError, RuntimeError, ValueError):
                    inside = False
                if not inside:
                    return 'a path outside the project'
    return None


def admit(app, data, ws, run_id):
    """The `confined` payload entry for a command that runs without a card ({'copy': None} without write access, else its
    copy's facts), or None when it asks as before (the reason goes to the task's journal)."""
    if not enabled():
        return None
    why = shape(data, ws.root)
    if why is None and write_denied(ws.root) is None:
        why = 'a project path the sandbox rule cannot name'
    if why is None and data['command'] in copies(app).refused.get(run_id, ()):
        why = 'it was refused a write where later runs load code'  # F1: its second run asks
    if why is None and deletes(data['argv']) and risky_conventions(app, run_id):
        why = "it deletes, and the project's AGENTS.md orders risky steps"  # D210: the card is the person's check
    if why is None and _planted_seen(app, ws, run_id):
        why = 'the task read text aimed at AI assistants'  # F1: a likely injection; its commands show their card
    if why is None and (app.containment_status() or {}).get('contained') is not True:
        why = 'process containment is not verified on this Mac'  # F2, F3: the anchor's sweep is proven first
    copy = None
    if why is None and data.get('writable'):
        copy, why = copies(app).take(run_id, ws.root)
    if why is not None:
        app.runs.note(run_id, 'confined_declined', {'reason': why})
        return None
    return {'copy': copy}


# Phase 46 (1.9.29, D210; w17's open point 4, the conventions review's third direction): a project whose AGENTS.md or
# CLAUDE.md orders deleting, pushing or publishing (a line Pebble left out, conventions.KINDS 'destroy') may have led
# the model to a delete it would not otherwise make; the line is never shown, but the file can be read. Such a task's
# deletes keep their card in Auto: the command itself (rm, rmdir, unlink, srm, trash, find -delete) or its -c/-e code.
DELETERS = frozenset({'rm', 'rmdir', 'unlink', 'srm', 'trash'})
CODE_DELETES = re.compile(r'\b(?:rmtree|unlink|rmdir|remove|removedirs|rm_rf|rm_r|rmSync|rm)\s*\(|\bFileUtils\.rm', re.I)


def deletes(argv):
    """D210: whether a command visibly deletes: a deleting tool, find -delete, or inline code that removes files."""
    head = PurePosixPath(argv[0]).name if argv else ''
    if head in DELETERS or head == 'find' and '-delete' in argv:
        return True
    code = [argv[i + 1] for i, word in enumerate(argv[:-1]) if word in CODE_FLAGS]
    return any(CODE_DELETES.search(text) for text in code)


def risky_conventions(app, run_id):
    """D210: whether this task was shown the project's conventions with a line left out that orders deleting, pushing or
    publishing (its `conventions` note, counts only). A note that cannot be read counts as one."""
    try:
        rows = app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='conventions' "
                               'ORDER BY sequence DESC LIMIT 1', (run_id,))
        return bool(rows) and bool((json.loads(rows[0]['payload']).get('left_out') or {}).get('destroy'))
    except Exception:
        return True


def _planted_seen(app, ws, run_id):
    """Whether a file this task saw holds an instruction aimed at AI assistants that names a file or host
    (requirements.planted, the planted check's own reading). A check that cannot run counts as seen."""
    try:
        found = requirements.planted(ws.root, requirements.seen_paths(ws.root, app.runs.tools(run_id), app.current_text))
    except Exception:
        return True
    return bool(found['files'] or found['hosts'])


_PREWARM = threading.Lock()


def prewarm(app):
    """Start the containment self-test (App.containment_status, once per process) in the background when an Auto task
    begins, so its first card-free command does not wait for it (about 4 s); with the switch off nothing needs it."""
    if enabled() and _PREWARM.acquire(blocking=False):  # never released: one start per process
        threading.Thread(target=app.containment_status, name='pebble-containment-prewarm', daemon=True).start()


def write_denied(root):
    """F1: the SBPL regex of every folder named in CODE_PARTS under root, at any depth, and of every bytecode file
    (CODE_SUFFIXES) under it (None when the root holds a character the rule cannot carry)."""
    text = str(root)
    if '"' in text or any(ord(c) < 32 for c in text):
        return None
    escape = lambda value: ''.join('\\' + c if c in '\\.^$|?*+()[]{}' else c for c in value)  # noqa: E731
    return ('^' + escape(text) + '/((.*/)?(' + '|'.join(escape(part) for part in CODE_PARTS) + ')(/|$)|.*\\.('
            + '|'.join(CODE_SUFFIXES) + ')$)')


CODE_FLAGS = ('-c', '-e', '-p', '--eval', '--print')
TOKEN = re.compile(r"[\w.@:/~%+=?&#-]{3,}")


def inner_words(argv, root):
    """F1: the words inside a command that the planted-injection check reads besides its arguments: the tokens of the
    code a -c/-e/--eval runs, and of the recipes of the make targets or package.json script it names. Bounded by the
    command's own length and MAKEFILE_BYTES."""
    words = []
    for index, arg in enumerate(argv[1:-1], 1):
        if arg in CODE_FLAGS:
            words += TOKEN.findall(argv[index + 1])
    recipes = _recipes(argv, root) if argv and Path(argv[0]).name in ('make', 'node') else None
    for text in recipes or ():
        words += TOKEN.findall(text)
    return words[:2000]


def _recipes(argv, root):
    """The recipe text of the make targets (and their prerequisites) or the package.json script (node --run NAME) a
    command names, or None when Pebble cannot read it soundly: another makefile or folder (-f, -C), an include, a
    $(shell ...), a recursive make, a target only a pattern or a variable defines, or a file past MAKEFILE_BYTES."""
    name, args = Path(argv[0]).name, argv[1:]
    try:
        if name == 'node':
            script = args[args.index('--run') + 1]
            data = json.loads(_read(Path(root) / 'package.json'))
            text = data.get('scripts', {}).get(script) if isinstance(data, dict) else None
            return [text] if isinstance(text, str) else None
        if any(arg.startswith(('-f', '-C', '--file', '--makefile', '--directory', '-I', '--include')) for arg in args):
            return None
        source = next((Path(root) / file for file in ('GNUmakefile', 'makefile', 'Makefile') if (Path(root) / file).is_file()),
                      None)
        text = _read(source) if source else None
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    if text is None or re.search(r'^\s*-?s?include\b|\$[({](?:shell|eval|MAKE)\b|^\t.*\bmake\b|\$\(MAKE', text, re.M):
        return None
    rules, recipes, current = {}, {}, None
    for line in text.splitlines():
        if line.startswith('\t') and current is not None:
            for target in current:
                recipes.setdefault(target, []).append(line[1:])
            continue
        match = re.match(r'^([^\s:=#][^:=#]*?)\s*::?(?!=)\s*([^=;#]*)', line)
        if match:
            current = match.group(1).split()
            for target in current:
                rules.setdefault(target, []).extend(match.group(2).split())
        elif line.strip() and not line.startswith('#'):
            current = None
    goals = [arg for arg in args if not arg.startswith('-') and '=' not in arg]
    if not goals:
        default = re.search(r'^\.DEFAULT_GOAL\s*:?=\s*(\S+)', text, re.M)
        goals = [default.group(1)] if default else [t for t in rules if not t.startswith('.')][:1]
    out, seen = [], set()
    while goals:
        goal = goals.pop(0)
        if goal in seen:
            continue
        seen.add(goal)
        if goal not in rules or '%' in goal or '$' in goal or len(seen) > RECIPE_TARGETS:
            if (Path(root) / goal).is_file() and goal not in rules:
                continue  # a prerequisite that is a project file, not a rule
            return None
        out += recipes.get(goal, [])
        goals += rules[goal]
    return out


def _read(path):
    if path.is_symlink() or path.stat().st_size > MAKEFILE_BYTES:
        raise ValueError(path)
    return path.read_text(encoding='utf-8', errors='replace')


def waiting(app, cid, run_id):
    """Whether the task already waits for the person's decision on another of its steps (runner.awaiting_decision): a
    confined command or an undoable Git step then keeps its card, which joins that review, and the runner's staged stop
    ends the turn as in 1.9.28 (F5: otherwise the next reply is written, held for the decision and written again, W5)."""
    return bool(run_id) and bool(app.store.query(
        "SELECT 1 FROM actions a JOIN run_action_owners o ON o.action_id=a.id WHERE o.run_id=? AND a.chat_id=? "
        "AND a.status='pending' LIMIT 1", (run_id, cid)))


def keeps_refs(app, run_id):
    """Whether the task's request keeps a branch or tag (the ledger's kept_ref): in Auto its Git steps then keep their
    card, where the person sees which branch each one moves."""
    rows = app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='requirements' ORDER BY sequence "
                           "DESC LIMIT 1", (run_id,)) if run_id else []
    return any(item.get('kind') == 'kept_ref' for item in (json.loads(rows[0]['payload']).get('requirements') or ())
               if isinstance(item, dict)) if rows else False


def copies(app):
    found = getattr(app, '_confined_copies', None)
    if found is None:
        found = app._confined_copies = Copies(Path(app.home) / 'clones')
        sweep(app)  # Phase 46 (1.9.29, D209): once a process, the copies whose day passed while Pebble was closed
    return found


def end_run(app, run_id):
    """The task ended: its copies stay KEEP_SECONDS for Undo (D209; D189 removed them now), and the copies whose day
    has passed go (sweep)."""
    found = getattr(app, '_confined_copies', None) or Copies(Path(app.home) / 'clones')
    found.end(run_id)
    sweep(app, found)


def sweep(app, found=None):
    """Phase 46 (1.9.29, D209): remove the copies of every ended task (not OPEN) a day after it ended, and the oldest
    beyond KEPT_BUDGET, and mark their Undo receipts as gone. A task that ended while Pebble was closed starts its day
    now. Never raises: a copy that cannot be removed now is tried again at the next task's end."""
    found = found or getattr(app, '_confined_copies', None) or Copies(Path(app.home) / 'clones')

    def ended(run_id):
        rows = app.store.query('SELECT state FROM runs WHERE id=?', (run_id,))
        return not rows or rows[0]['state'] not in OPEN
    try:
        gone, chats = found.sweep(ended), set()
        for run_id, ids in gone.items():
            for row in app.store.query("SELECT a.id,a.chat_id,a.payload FROM actions a JOIN run_action_owners o ON "
                                       "o.action_id=a.id WHERE o.run_id=? AND a.kind='command'", (run_id,)):
                payload = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
                result = payload.get('result') if isinstance(payload, dict) else None
                if isinstance(result, dict) and result.get('undo') == 'available' and \
                        ((payload.get('confined') or {}).get('copy') or {}).get('id') in ids:
                    app.store.query('UPDATE actions SET payload=? WHERE id=?',
                                    (json.dumps({**payload, 'result': {**result, 'undo': 'removed'}}), row['id']))
                    chats.add(row['chat_id'])
        for cid in chats:  # the Undo buttons go
            app.emit('actions', {'chat_id': cid, 'actions': app.store.actions(cid)})
    except Exception:  # a store or folder that cannot be read now: the next task's end tries again
        return


# -- the walk --------------------------------------------------------------------------------------------------------

def _walk(root, limit=MAX_FILES):
    """{rel: (kind, size, mtime_ns, ctime_ns, mode, inode)} of everything under root except .git, caches and the folders
    a card-free command cannot write (kind 'd' for a folder, 'l' for a link, 'f' otherwise), never through a link; None
    past `limit` entries."""
    found, stack = {}, ['']
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(os.path.join(root, folder) if folder else root))
        except OSError:
            continue
        for entry in entries:
            rel = f'{folder}/{entry.name}' if folder else entry.name
            if requirements.ignored(rel) or entry.name in CODE_PARTS:  # F1: those a card-free command cannot write
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            kind = 'l' if entry.is_symlink() else 'd' if entry.is_dir(follow_symlinks=False) else 'f'
            found[rel] = (kind, info.st_size if kind != 'd' else 0, info.st_mtime_ns, info.st_ctime_ns, info.st_mode,
                          info.st_ino)
            if len(found) > limit:
                return None
            if kind == 'd':
                stack.append(rel)
    return found


def _diff(before, after):
    """(added, changed, removed): the files and links a command added, changed or removed (folders only by their files)."""
    added = sorted(rel for rel, entry in after.items() if entry[0] != 'd'
                   and (rel not in before or before[rel][0] != entry[0]))
    removed = sorted(rel for rel, entry in before.items() if entry[0] != 'd'
                     and (rel not in after or after[rel][0] != entry[0]))
    changed = sorted(rel for rel, entry in before.items() if entry[0] != 'd' and rel in after
                     and after[rel][0] == entry[0] and after[rel] != entry)
    return added, changed, removed


def _size(walk, paths):
    return sum(walk[rel][1] for rel in paths if rel in walk and walk[rel][0] == 'f')


# -- clonefile and link-free paths ------------------------------------------------------------------------------------

def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _clone(src, dst):
    fn = _libc().clonefile
    fn.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    if fn(os.fsencode(src), os.fsencode(dst), CLONE_NOFOLLOW) != 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))


def _clone_at(src, folder, name):
    """Clone src (a clone's file or link) as `name` in the open folder `folder` (clonefileat, never following a link)."""
    fn = _libc().clonefileat
    fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
    if fn(AT_FDCWD, os.fsencode(src), folder, os.fsencode(name), CLONE_NOFOLLOW) != 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))


def _folder(root, rel, modes=None, replaced=None):
    """An open descriptor of rel's folder, reached from root one folder at a time without following a link. With
    `modes` ({rel: st_mode} of the folders before the command), a missing folder is made again with its mode, and (F4)
    a link or file that took a folder's place is removed first (its path goes to `replaced`); the link is unlinked, never
    followed."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = PurePosixPath(rel).parts[:-1]
        for index, part in enumerate(parts):
            path = '/'.join(parts[:index + 1])
            try:
                inner = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if modes is None or exc.errno not in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR) \
                        or exc.errno != errno.ENOENT and path not in modes:
                    raise
                if exc.errno != errno.ENOENT:
                    os.unlink(part, dir_fd=fd)
                    if replaced is not None:
                        replaced.append(path)
                os.mkdir(part, (modes.get(path) or 0o755) & 0o7777, dir_fd=fd)
                inner = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = inner
        return fd
    except BaseException:
        os.close(fd)
        raise


def _remove(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (info.st_mode & 0o170000) == 0o040000:
        shutil.rmtree(name, dir_fd=fd)  # fd-based, never follows a link (rmtree.avoids_symlink_attacks)
    else:
        os.unlink(name, dir_fd=fd)


def _put_back(root, tree, rel, modes, replaced=None):
    """Make root/rel what tree/rel holds (tree/rel absent: remove root/rel)."""
    try:
        fd = _folder(root, rel, modes if os.path.lexists(os.path.join(tree, rel)) else None, replaced)
    except FileNotFoundError:
        return  # nothing to remove: its folder is gone too
    try:
        name = PurePosixPath(rel).name
        _remove(fd, name)
        if os.path.lexists(os.path.join(tree, rel)):
            _clone_at(os.path.join(tree, rel), fd, name)
    finally:
        os.close(fd)


def _same(now, then):
    """Whether a path is still as a command (or Pebble's restore) left it: a folder by its kind (its own times move with
    every file in it), anything else by its whole entry."""
    return now == then if not then or then[0] != 'd' else bool(now) and now[0] == 'd'


def _stat(root, rel):
    """rel's walk entry now (None when absent), reached without following a link."""
    try:
        fd = _folder(root, rel)
    except OSError:
        return None
    try:
        info = os.stat(PurePosixPath(rel).name, dir_fd=fd, follow_symlinks=False)
    except OSError:
        return None
    finally:
        os.close(fd)
    kind = 'l' if (info.st_mode & 0o170000) == 0o120000 else 'd' if (info.st_mode & 0o170000) == 0o040000 else 'f'
    return [kind, info.st_size if kind != 'd' else 0, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_ino]


# -- the card-free run (F2, F3) ---------------------------------------------------------------------------------------

class Contained:
    """F2, F3 (the w13b security review): how terminal.run_command runs a command Auto admitted without a card. Pebble's
    containment anchor (containment.py, Phase 14) starts it inside the sandbox and, when it ends, is stopped or its
    deadline passes, sweeps the whole sandbox instance: kill(-1) reaches every member of that instance (a setsid or
    double-forked escapee included) and nothing outside it, which App.containment_status proved on this Mac before any
    such command runs, and the anchor reports whether its sweep finished. The anchor's single-threaded child sets the
    command's rlimits (CPU seconds, the largest file, no core file, a process cap for the UID counted here, outside the
    sandbox); the host stops it, through the anchor, when its tree reaches PROCESSES members or MEMORY_BYTES of
    measured footprint (job_shim's sampling), every third tick. Its profile denies writes where later runs load code
    (F1, write_denied). Nothing here signals a stored PID: stop is a byte on the anchor's lifeline."""

    def __init__(self, root, exe, writable):
        from .containment import launcher
        from .job_shim import _count_pids
        self.prefix, reads = launcher(bool(getattr(sys, 'frozen', False)))
        self.reads = [Path(path).resolve() for path in reads]
        self.exe, self.root, self.ticks = exe, Path(root), 0
        self.denied = write_denied(root) if writable else None
        base = _count_pids()
        if base is None or (writable and self.denied is None):
            raise PolicyError('Pebble could not set the limits of a command that runs without approval; nothing ran.')
        self.cap, self.fds = base + PROCESSES, {}

    def profile(self, toolchain):
        """sandbox_profiles.build arguments: the anchor is what sandbox-exec starts, the command its pinned child."""
        return {'executable': Path(self.prefix[0]).resolve(), 'executables': [self.exe],
                'exec_paths': [toolchain, self.root, *self.reads], 'read_paths': [toolchain, *self.reads],
                **({'write_denied': self.denied} if self.denied else {})}

    def start(self, profile, env, log, argv, seconds):
        """Start the anchor under sandbox-exec and hand it its spec: the command, its environment, its deadline and
        limits. The command's own output goes to `log` through the anchor."""
        from .containment import ROOT
        self.fds.update(zip(('spec_r', 'spec_w'), os.pipe()))
        self.fds.update(zip(('status_r', 'status_w'), os.pipe()))
        anchor = {'PATH': '/usr/bin:/bin', 'HOME': env['HOME'], 'TMPDIR': env['TMPDIR'], 'LANG': 'en_US.UTF-8',
                  'PYTHONDONTWRITEBYTECODE': '1', 'PEBBLE_ANCHOR_SPEC_FD': str(self.fds['spec_r']),
                  'PEBBLE_ANCHOR_STATUS_FD': str(self.fds['status_w']), 'PEBBLE_ANCHOR_OUT_FD': str(log.fileno())}
        if not getattr(sys, 'frozen', False):
            anchor['PYTHONPATH'] = str(ROOT / 'backend')
        try:
            process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile, *self.prefix, '--job-anchor',
                                        env['TMPDIR']], cwd=self.root, env=anchor, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                       pass_fds=(self.fds['spec_r'], self.fds['status_w'], log.fileno()),
                                       start_new_session=True)
        except BaseException:
            self.close()
            raise
        self._close('spec_r', 'status_w')
        os.set_blocking(self.fds['status_r'], False)
        spec = json.dumps({'argv': [str(self.exe), *argv[1:]], 'cwd': str(self.root), 'env': env,
                           'timeout': seconds + 2, 'limits': {'cpu_seconds': CPU_SECONDS, 'fsize_bytes': FSIZE_BYTES,
                                                              'nproc': self.cap}}).encode()
        try:
            os.write(self.fds['spec_w'], b'%d\n' % len(spec) + spec)
        except OSError:
            pass  # the anchor is gone already: finish() reports what it recorded
        return process

    def note(self, stopped):
        return RESOURCE_NOTE.format(what=LIMITS[stopped])

    def governor(self, process):
        """'processes' or 'memory' when the command's tree passed its bounds (sampled every third tick), else None."""
        self.ticks += 1
        if self.ticks % 3 or process.poll() is not None:
            return None
        from .job_shim import _children, _footprint
        members, frontier = [process.pid], [process.pid]
        while frontier:
            kids = _children(frontier.pop())
            for kid in kids or ():
                if kid not in members:
                    members.append(kid)
                    frontier.append(kid)
            if len(members) >= PROCESSES:
                return 'processes'
        return 'memory' if sum(_footprint(pid) for pid in members) > MEMORY_BYTES else None

    def stop(self, process):
        """Ask the anchor to sweep (a byte on its lifeline) and wait for it; kill its group only if it never ends."""
        if process.returncode is None:
            try:
                os.write(self.fds['spec_w'], b's')
            except (OSError, KeyError):
                pass
            try:
                process.wait(timeout=SWEEP_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def finish(self):
        """(exit code, reason) once the anchor ended. PolicyError when the command never started; an uncertain outcome
        (terminal.CommandOutcomeUncertain) when the anchor did not report a finished sweep."""
        from .terminal import CommandOutcomeUncertain
        data = b''
        try:
            while chunk := os.read(self.fds['status_r'], 65536):
                data += chunk
        except (BlockingIOError, OSError, KeyError):
            pass
        events = []
        for line in data.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        exited = next((e for e in events if isinstance(e, dict) and e.get('event') == 'exit'), {})
        swept = next((e for e in reversed(events) if isinstance(e, dict) and e.get('event') == 'sweep'
                      and e.get('phase') == 'complete'), {})
        if str(exited.get('reason', '')).startswith('anchor_spawn_failed'):
            raise PolicyError('The confined command could not start; nothing ran.')
        if swept.get('thorough') is not True:
            raise CommandOutcomeUncertain('Pebble could not confirm that every process this command started has ended. '
                                          'Its outcome is uncertain; inspect it before retrying.')
        code = exited.get('code') if exited.get('code') is not None else swept.get('code')
        return code, exited.get('reason')

    def close(self):
        self._close(*list(self.fds))

    def _close(self, *names):
        for name in names:
            fd = self.fds.pop(name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


# -- copies -----------------------------------------------------------------------------------------------------------

class Copies:
    """The clones of each task, in `base`/<run>/<copy>/ (tree/ and receipt.json), with what they may hold (D189)."""

    def __init__(self, base):
        self.base, self.lock, self.runs, self.pending = Path(base), threading.Lock(), {}, {}
        self.refused = {}  # F1: run -> the commands refused a write where later runs load code (they ask next time)

    def _state(self, run_id):
        state = self.runs.get(run_id)
        if state is None:  # after a restart: what this task's earlier copies already hold
            held = 0
            for receipt in sorted((self.base / run_id).glob('*/receipt.json')) if (self.base / run_id).is_dir() else ():
                try:
                    held += int(json.loads(receipt.read_text()).get('held', 0))
                except (OSError, ValueError, TypeError):
                    held = BUDGET  # an unreadable receipt: no more copies for this task
            state = self.runs[run_id] = {'held': held, 'last': None}
        return state

    def take(self, run_id, root):
        """(the copy's facts, None) after cloning the project, or (None, why) when the command must ask instead."""
        with self.lock:
            state = self._state(run_id)
            started = time.monotonic()
            walk = _walk(root, MAX_FILES)
            if walk is None:
                return None, f'more than {MAX_FILES} files'
            size = _size(walk, walk)
            if state['last'] is not None:  # edits since the last command: the earlier copies now hold their old bytes
                added, changed, removed = _diff(state['last'], walk)
                state['held'] += _size(state['last'], changed + removed)
            if state['held'] + size > BUDGET:
                return None, 'the copies would pass 3 GB for this task'
            store = self.base / run_id
            try:
                store.mkdir(parents=True, exist_ok=True, mode=0o700)
                if os.stat(store).st_dev != os.stat(root).st_dev:
                    return None, 'the project is on another volume'
                ident = uuid.uuid4().hex
                tree = store / ident / 'tree'
                tree.mkdir(parents=True, mode=0o700)
                walked = time.monotonic()
                for name in sorted({rel.split('/', 1)[0] for rel in walk}):
                    _clone(os.path.join(root, name), tree / name)
            except OSError as exc:
                return None, 'the project could not be copied (' + (exc.strerror or type(exc).__name__) + ')'
            done = time.monotonic()
            facts = {'id': ident, 'files': sum(entry[0] != 'd' for entry in walk.values()), 'bytes': size,
                     'walk_ms': round((walked - started) * 1000, 1), 'clone_ms': round((done - walked) * 1000, 1)}
            self.pending[ident] = {'run_id': run_id, 'root': str(root), 'before': walk, 'facts': facts}
            return facts, None

    def refusal(self, run_id, command, output):
        """F1: the note for a command the sandbox refused a write in CODE_PARTS or to a bytecode file (its next run
        asks), or None."""
        if not WRITE_REFUSED.search(output or '') or not (any(part in (output or '') for part in CODE_PARTS)
                                                          or re.search(r'\.py[co]\b', output or '')):
            return None
        with self.lock:
            self.refused.setdefault(run_id, set()).add(command)
        return CODE_NOTE.format(parts=', '.join(CODE_PARTS[:6]) + ', .pyc files and the like')

    def settle(self, ident, requirement_list):
        """After the command: its changes, what the ledger keeps put back, the receipt. Returns the result fields for the
        model ({'changes', 'kept'?, 'undo'}) and the journal's facts."""
        with self.lock:
            record = self.pending.pop(ident)
            root, before, state = record['root'], record['before'], self._state(record['run_id'])
            started = time.monotonic()
            after = _walk(root, MAX_FILES * 5)
            folder = self.base / record['run_id'] / ident
            if after is None:  # too many new files to list: the receipt cannot say what to undo
                facts = {**record['facts'], 'undo': 'unavailable'}
                self._write(folder, {'root': root, 'held': record['facts']['bytes'], 'undo': 'unavailable'})
                state['held'] += record['facts']['bytes']
                state['last'] = None
                return {'changes': f'more than {MAX_FILES * 5} files now; Pebble did not list them', 'undo': 'unavailable'}, facts
            added, changed, removed = _diff(before, after)
            state['held'] += _size(before, changed + removed)
            kept = kept_paths(requirement_list, sorted(set(added + changed + removed)))
            restored, failed, replaced = [], [], []
            modes = {rel: entry[4] for rel, entry in before.items() if entry[0] == 'd'}
            for rel in kept:
                try:
                    _put_back(root, str(folder / 'tree'), rel, modes, replaced)
                    restored.append(rel)
                except OSError as exc:
                    failed.append((rel, exc.strerror or type(exc).__name__))
            final = {rel: _stat(root, rel) for rel in added + changed + removed}
            state['last'] = {**after, **{rel: tuple(entry) for rel, entry in final.items() if entry is not None}}
            for rel, entry in final.items():
                if entry is None:
                    state['last'].pop(rel, None)
            undo = 'available' if any(rel not in restored for rel in added + changed + removed) else 'none'
            self._write(folder, {'root': root, 'held': _size(before, changed + removed), 'undo': undo,
                                 'added': added, 'changed': changed, 'removed': removed, 'restored': restored,
                                 'after': final, 'modes': modes})
            out = {'undo': undo}
            if added or changed or removed:
                out['changes'] = describe(added, changed, removed)
            if restored or failed:
                out['kept'] = kept_note(restored, failed, added, removed) + (
                    f' A link or file had taken the place of the folder {_listed(replaced)}; Pebble removed it to put '
                    'the folder back.' if replaced else '')  # F4
            facts = {**record['facts'], 'added': len(added), 'changed': len(changed), 'removed': len(removed),
                     'restored': restored[:SHOWN], 'failed': [rel for rel, _ in failed][:SHOWN],
                     'replaced': replaced[:SHOWN], 'diff_ms': round((time.monotonic() - started) * 1000, 1)}
            return out, facts

    def undo(self, run_id, ident):
        """Put the project back as it was before the command: each path it added, changed or removed, only while every
        one is still as the command left it (all or nothing). Returns the paths restored."""
        with self.lock:
            folder = self.base / run_id / ident
            try:
                receipt = json.loads((folder / 'receipt.json').read_text())
            except (OSError, ValueError):
                raise PolicyError('Pebble keeps a command\'s copy for a day after its task ends, and this one is gone, so '
                                  'Pebble cannot undo it.') from None
            if receipt.get('undo') != 'available':
                raise PolicyError('Pebble has no record of what this command changed, so it cannot undo it.')
            root, paths = receipt['root'], [rel for key in ('added', 'changed', 'removed') for rel in receipt[key]]
            then = receipt['after']
            moved = [rel for rel in paths if not _same(_stat(root, rel), then.get(rel))]
            if moved:
                raise PolicyError('These files changed after the command, so Undo is blocked and nothing was written: '
                                  + ', '.join(moved[:SHOWN]) + '.')
            for rel in paths:
                _put_back(root, str(folder / 'tree'), rel, receipt.get('modes') or {})
            self._write(folder, {**receipt, 'undo': 'undone'})
            self.runs.pop(run_id, None)  # recount from the receipts: the project is as it was before the command
            return paths

    def end(self, run_id, now=None):
        """The task ended: no more copies for it; its clones stay, stamped with the time it ended (D209: sweep removes
        them a day later). A task resumed and ended again starts its day again."""
        with self.lock:
            self.runs.pop(run_id, None)
            for ident, record in list(self.pending.items()):
                if record['run_id'] == run_id:
                    self.pending.pop(ident)
            store = self.base / run_id
            if store.is_dir() and not store.is_symlink():
                try:
                    (store / ENDED).write_text(repr(time.time() if now is None else now))
                except OSError:
                    pass  # sweep stamps it when it first sees it

    def _ended(self, store, now=None):
        """When the task of `store` ended (its ENDED stamp, written now when it has none), or None for no store."""
        if not store.is_dir() or store.is_symlink():
            return None
        stamp = store / ENDED
        try:
            return float(stamp.read_text())
        except (OSError, ValueError):
            when = time.time() if now is None else now
            try:
                stamp.write_text(repr(when))
            except OSError:
                pass
            return when

    def sweep(self, ended, now=None):
        """Phase 46 (1.9.29, D209): remove the clones of the tasks `ended(run_id)` says have ended, KEEP_SECONDS after
        they did, then the oldest while the rest hold more than KEPT_BUDGET. {run_id: [copy ids removed]}."""
        now = time.time() if now is None else now
        with self.lock:
            if not self.base.is_dir() or self.base.is_symlink():
                return {}
            kept, gone = [], {}
            for store in sorted(self.base.iterdir()):
                if store.is_symlink() or not store.is_dir() or store.name in self.runs or not ended(store.name):
                    continue
                when = self._ended(store, now)
                if now - when >= KEEP_SECONDS:
                    gone[store.name] = self._remove(store)
                else:
                    kept.append((when, store))
            held = {store: self._held(store) for _, store in kept}
            for _, store in sorted(kept, key=lambda item: item[0]):  # the oldest first
                if sum(held.values()) <= KEPT_BUDGET:
                    break
                gone[store.name] = self._remove(store)
                held.pop(store)
            return gone

    @staticmethod
    def _held(store):
        total = 0
        for receipt in store.glob('*/receipt.json'):
            try:
                total += int(json.loads(receipt.read_text()).get('held', 0))
            except (OSError, ValueError, TypeError):
                total += BUDGET  # an unreadable receipt: counted as a whole task's copies
        return total

    @staticmethod
    def _remove(store):
        ids = [child.name for child in store.iterdir() if child.is_dir() and not child.is_symlink()]
        shutil.rmtree(store, ignore_errors=True)
        return ids

    @staticmethod
    def _write(folder, receipt):
        (folder / 'receipt.json').write_text(json.dumps(receipt))


# -- the ledger and the words --------------------------------------------------------------------------------------

def kept_paths(requirement_list, paths):
    """The paths among a command's changes that the ledger keeps unchanged: covered by an `unchanged` requirement (its
    named files and folders, or the tests), except a leftover lock, state or temporary file the request never named that
    existed before (the completion gate's own exception, requirements.ARTEFACT: a recovery may need it gone)."""
    out = []
    for rel in paths:
        for item in requirement_list or ():
            if item.get('kind') != 'unchanged' or item.get('soft') or not requirements._covers(item, rel):
                continue
            named = {p.rstrip('/') for p in item.get('paths') or ()}
            if rel not in named and requirements.ARTEFACT.search(rel):
                continue
            out.append(rel)
            break
    return out


def _listed(paths):
    return ', '.join(paths[:SHOWN]) + (f' and {len(paths) - SHOWN} more' if len(paths) > SHOWN else '')


def describe(added, changed, removed):
    """What a command did to the project's files, in a few words for the model ("added out.txt; removed old.log")."""
    return '; '.join(f'{verb} {_listed(paths)}' for verb, paths in (('added', added), ('changed', changed),
                                                                     ('removed', removed)) if paths)


def kept_note(restored, failed, added, removed):
    text = ''
    if restored:
        did = ('removed ' if all(rel in removed for rel in restored) else 'added ' if all(rel in added for rel in restored)
               else 'changed ') + ('it' if len(restored) == 1 else 'them')
        one = len(restored) == 1
        text = KEPT_NOTE.format(what=_listed(restored), did=did, them='it' if one else 'them',
                                they_are='it is' if one else 'they are', they_were='it was' if one else 'they were')
    if failed:
        paths = [rel for rel, _ in failed]
        one = len(paths) == 1
        text += FAILED_NOTE.format(paths=_listed(paths), why=failed[0][1], them='it' if one else 'them',
                                   they_are='it is' if one else 'they are')
    return text.strip()
