"""Bounded terminal broker: argv execution, risk review and OS confinement."""

import hashlib
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from . import recipes
from .policy import PRIVATE, Workspace, PolicyError
from .process_exit import LeaderWatch
from .terminal_text import sanitize as sanitize_terminal
from .zone import person_zone

BLOCKED = {
    "sudo",
    "su",
    "doas",
    "shutdown",
    "reboot",
    "halt",
    "diskutil",
    "fdisk",
    "mkfs",
    "dd",
    "launchctl",
    "osascript",
    "security",
    "reg",
    "powershell",
    "pwsh",
    "cmd",
    "bash",
    "sh",
    "zsh",
    "fish",
    "csh",
    "chmod",
    "chown",
    "chflags",
}
READ_ONLY = {
    "pwd",
    "ls",
    "rg",
    "cat",
    "head",
    "tail",
    "wc",
    "file",
    "uname",
    "which",
    "echo",
}


class CommandOutcomeUncertain(RuntimeError):
    """An admitted process stopped before its external outcome was observed."""


# Phase 46 (1.9.27, golden held-out recovery: 6 honest stops of 15 on both models, where recovery means reading an
# error and doing the step another way): a refusal says what works instead, so the model takes the other way at once
# rather than trying variants until the loop note (runner.LOOP_NOTE_AT) tells it to give up. Every refusal still
# refuses the same commands; only the words changed (SWE-agent, arXiv 2405.15793 §A.1: an error without the way out
# makes the agent re-issue the same command).
# Phase 46 (1.9.28, T1; the lead's follow-up to w9-git): a hint no longer points the model at a way round that turns
# the task into a false completion (private case d-rc-105: the right count through python3 while the script's exec bit,
# which the task needed, stayed off). When the task needs the thing itself (the file executable, the variable set for
# the program as written, the script run whole), the model says so plainly instead of reporting the task done.
PLAINLY = ' say so plainly in your reply instead of reporting the task done.'
SUBSTITUTION_HINT = (' Compute the value inside the program instead when the program is yours to change '
                     '(sys.path.insert(0, "src"), a variable in the script) or pass it as an argument; the import path '
                     'needs nothing: commands and RunTests already have the project folder and its src/ on it. If the '
                     'task needs a variable set for a program as written (one you were not asked to change),' + PLAINLY)
ONE_COMMAND_HINT = (' Commands already start in the project folder, so `cd` is not needed; run each command on its '
                    'own, and to keep output in a file let the program write it (or write it with Write after '
                    'reading the result).')
SHELL_HINT = (' To run a shell script\'s steps, read it and run its commands one at a time with RunCommand (python3, '
              'node, make and the other runtimes run directly); the script itself cannot run here, so if the task needs '
              'it to work as a whole (you fixed it, or a tool runs it), say it was not run as a whole, and' + PLAINLY)
CHMOD_HINT = (' To run a script once, call it through its interpreter (python3 script.py, node script.js). chmod is not '
              'available, so a file cannot be made executable here: if the task needs the file itself executable (its '
              'README runs ./script, or a tool calls it by path),' + PLAINLY)
RUNTIME_HINT = ("Use a runtime to execute project scripts, e.g. python3 script.py or node script.js; a shell "
                "script's commands run one at a time with RunCommand, and a virtual environment's interpreter is "
                "not used (python3 with the project's own files is). A script cannot run by its own path here: if the "
                "task needs that (its exec bit, or a tool that calls it by path)," + PLAINLY)
# Phase 46 (1.9.28, D178): with the terminal switch on (the default), the exec bit itself can be added under review, so
# a task that needs the script runnable by its path finishes instead of stopping (private case d-rc-105)
EXEC_RUNTIME_HINT = RUNTIME_HINT.split(' A script cannot')[0] + (
    ' A script does not run by its own path here. If the task needs it to run by its path as well (its README runs '
    './script, or a tool calls it by path), `ls -l` shows whether it has its exec bit and `chmod +x FILE` adds it under '
    'review; then run it here through its runtime.')
EXEC_BIT_HINT = ' To run a script once, call it through its interpreter (python3 script.py, node script.js).'
# Phase 46: a RunCommand's bounds. After COMMAND_SECONDS, or OUTPUT_LIMIT bytes of output, the command's group is
# killed and the result says so (run_command); TIMEOUT_EXIT is timeout(1)'s and Codex's status for a stopped command.
COMMAND_SECONDS = 60.0
OUTPUT_LIMIT = 192_000
TIMEOUT_EXIT = 124
STOPPED_NOTE = ('Pebble stopped this command after {seconds:g} s (its process group was killed), so it did not finish: '
                'the output above is what it printed until then, and files it was writing may be incomplete. Check what '
                'it left before relying on it, then fix what made it slow or wait (a loop, a stale lock, a prompt for '
                'input) before running it again.')
# Phase 46 (1.9.28): what App.offer_process_tools adds to STOPPED_NOTE when StartCommand joins the run's next step
LONGER_NOTE = (' If its output shows it was making progress and it only needs more time, run it again with StartCommand '
               '(timeout up to 300 s; it runs in the background) and follow it with WaitProcess until it ends.')
OUTPUT_NOTE = ('Pebble stopped this command after {limit:,} bytes of output (its process group was killed), so it did '
               'not finish: the output above is its first part, and files it was writing may be incomplete. Check what '
               'it left, then run it with less output (a quieter flag, a smaller input, or the program writing to a '
               'file) before relying on it.')
# Phase 46 (1.9.28, b-new research: d-pw-92's 31 KB inventory reached the model cut at 16,000 bytes with nothing saying
# so, which invites an answer from half a listing): a command that finished with more output than the result keeps
# says how much is missing
SHOWN_BYTES = 16000
CUT_NOTE = ('Only the first {shown:,} of {size:,} bytes of its output are shown, and the rest is kept nowhere. Before '
            'relying on what it printed, get the part you need: where running it again is safe, run it with less output '
            '(a filter such as grep, head or tail, or a quieter flag); otherwise have it write to a file you then Read.')
# Phase 46 (1.9.29, golden held-out recovery: 6 honest stops of 15 on both models, the same cells; D212: in each, the
# one command the run made failed printing a lock, busy resource or port with "operation not permitted" or "permission
# denied", or a version mismatch, and the run then wrote nothing and stopped; Claude Code and Codex fix the cause and run
# again): every result says "macOS sandbox", so a permission error reads as the sandbox's, a limit nobody can lift. When
# a failed command printed one, the host says which cause it can see and what works: the path's own permissions (the
# project's to change, chmod u+w under review), Pebble's sandbox (a path outside the project and the command's own
# temporary folder, Git metadata or a private file, a process the command did not start, a socket), never a guess. No
# model request is spent: the note rides the command's result.
DENIED = re.compile(r'permission denied|operation not permitted|read-only file system|\bE(?:PERM|ACCES|ROFS)\b', re.I)
QUOTED = re.compile(r"""['"‘’`]([^'"‘’`\n]{1,400})['"‘’`]""")
COLON_PATH = re.compile(r'(?:^|:\s)([^\s:\'"‘’`][^:\n\'"‘’`]{0,400}?):\s+(?:permission denied|operation not permitted|'
                        r'read-only file system)', re.I)  # "rm: run/x.lock: Permission denied"
PRIVATE_PART = re.compile(r'(?:^|/)(?:\.git|\.env[^/]*|\.ssh|\.aws|\.gnupg|\.netrc|\.npmrc|\.pypirc|\.pebble|credentials|'
                          r'secrets)(?:/|$)|\.(?:pem|key|p12|pfx|gguf)$', re.I)
SIGNAL_WORDS = re.compile(r'\bkill(?:pg)?\b|\.send_signal\(|\.terminate\(|\bsignal\b', re.I)
SOCKET_WORDS = re.compile(r'\bsocket\b|\bbind\b|\bconnect\b|\blisten\b|create_connection|\baddress\b|\bport\b|'
                          r'EADDRINUSE|EADDRNOTAVAIL|localhost|127\.0\.0\.1', re.I)
MODE_CAUSE = ('{path} is read-only by its own permissions ({mode}), which the project set, not Pebble\'s sandbox (it '
              'lets commands write anywhere in the project). Commands may change them: `chmod u+w {path}` (reviewed) '
              'gives its owner write access{inside}; then run the command again.')
# Phase 46 (1.9.29, the lead after the live check, Opus d-rc-232 phrasing 2: a build's hook could not run, "Permission
# denied" from subprocess, and the run stopped, reading the hook's exec bit as an edit of a file the request keeps; the
# other five runs of 1.9.28 and 1.9.29 looked at its mode and ran `chmod +x`): a regular project file its owner may read
# and write but nobody may run, named in a permission error that came from running a program (EXEC_WORDS), is said to be
# just that, with the reviewed fix and what it leaves alone. A bare "Permission denied" naming such a file says nothing.
EXEC_WORDS = re.compile(r'_execute_child|\bPopen\b|\bexecv\w*|\bspawn\b|^(?:/bin/|/usr/bin/)?(?:ba|z|da|k)?sh:|^env:|'
                        r'^make(?:\[\d+\])?:|^xargs:', re.I | re.M)
EXEC_CAUSE = ('{path} cannot run by its path: its exec bit is off ({mode}). `chmod +x {path}` (reviewed) turns that bit on '
              'and changes nothing else (not its contents); then run the command again.')
OUTSIDE_CAUSE = ('{path} is outside the project, and Pebble\'s sandbox lets a command write only inside the project and in '
                 'its own temporary folder (TMPDIR, which is also HOME for it, removed when it ends). Have the program '
                 'write inside the project instead (an argument or a setting it reads), then run it again, or say '
                 'plainly that it must write there.')
PRIVATE_CAUSE = ('{path} is Git metadata or a private file, which Pebble\'s sandbox keeps closed to commands; Git works '
                 'through the Git tools (ReadGitStatus, ProposeGitChange).')
SIGNAL_CAUSE = ('A command may signal only the processes it started itself (Pebble\'s sandbox), so "Operation not '
                'permitted" here says only that the process is not one this command started, not whether it runs. Pebble '
                'ends every process a command started when that command ends (a StartCommand process runs until '
                'StopProcess ends it), and a lock or PID file an earlier run left is stale once its process is gone.')
SOCKET_CAUSE = ('Commands run without network in Pebble\'s sandbox, and that includes this Mac\'s own addresses '
                '(localhost, 127.0.0.1): binding or connecting a socket fails with "Operation not permitted" whatever '
                'the port, so this error does not show that another program holds it. Run the project without the '
                'socket (a flag or setting that skips the server or the port check) where it allows that, or say '
                'plainly that the step needs network access.')
# A process id that a lock, PID or port file names, beside the words that say so ("pid 48213", "process 48213"). Inside
# the sandbox a command cannot see other processes (ps and kill -0 answer as if none ran), so the host, outside it,
# says whether that process runs now: the one fact that tells a stale lock from a live one.
PID_WORDS = re.compile(r'\b(?:pid|process(?:\s+id)?)\s*[=:#]?\s*\(?(\d{2,7})\b|\bkill:\s*(\d{2,7}):', re.I)
HELD_WORDS = re.compile(r'lock|\.pid\b|pid ?file|running|in use|busy|held|holds|port', re.I)
STALE_PID = ('No process with PID {pid} runs on this Mac now (Pebble checked from outside the sandbox), so the lock or '
             'PID file that names it is stale: removing it is the fix, then run the command again.')
LIVE_PID = ('PID {pid} is a running process on this Mac now ({name}; Pebble checked from outside the sandbox), and it is '
            'not one this task started. If it is not the program the lock or PID file stands for (PIDs are reused), '
            'the file is stale.')
# Phase 46 (1.9.29, the lead; D212 counts of golden held-out recovery after w21's notes: in 3 failing cells on each
# model the one failed command named a lock file inside the project that held no process id and printed none, so no
# note came and the run stopped, unable to see from inside the sandbox whether another run holds the lock): a process
# lock or PID file the output names on a line that speaks of contention (CONTENTION), inside the project, small, regular
# and not private, is read by the host. A process id in it gets STALE_PID or LIVE_PID as above. Without one, the host
# says whether any process on this Mac has the file open (lsof) or runs the failed command's own script (ps), facts the
# sandbox hides. It says the lock is stale only when it can see both, and only for a script or module an interpreter ran
# (a runner such as make never holds a lock itself); when it cannot tell, it says nothing. Dependency lockfiles
# (package-lock.json, yarn.lock, Cargo.lock ...) are never process locks. Tightened after an independent review (the
# lead's reviewer, 2026-10-30 ~21:05): option values, paths with spaces, regex patterns, fixtures and leaks.
LOCK_WORD = re.compile(r'lock|lck|pid', re.I)
PATHISH = re.compile(r'[\w./~-]+')  # linear: tokens first, then filters (a nested pattern backtracked on long words)
CONTENTION = re.compile(r'\b(?:exists?|another|already|locked|held|holds|holding|in use|running|busy|in progress|'
                        r'wait(?:s|ing)?|acquired?|stale|remove|delete|clear)\b', re.I)
PROCESS_LOCK = re.compile(r'^(?:.*[._-])?(?:lock|lck|pid)(?:file)?$', re.I)
DEPENDENCY_LOCK = re.compile(r'^(?:yarn|cargo|poetry|pipfile|gemfile|composer|flake|uv|bun|mix|pubspec|podfile|pdm|'
                             r'deno|gradle|conda|package|packages|paket|requirements(?:[._-]\w+)?|renv|pixi|shard|'
                             r'module\.bazel)[._-]lock(?:b|file)?$|\.lockfile$', re.I)
# what a dependency lockfile holds and a process lock never does (a small project's lockfile may be under 4 KB)
DEPENDENCY_TEXT = re.compile(r'lockfileVersion|\[\[package\]\]|\bdependencies\b|\bresolved\b|\bintegrity\b|'
                             r'\bchecksum\b|\bsha256\b|\bsha512\b|\brequires\b|==\d', re.I)
FILE_PID = re.compile(r'^\s*(\d{2,7})\s*$|\bpid\b\W{0,4}(\d{2,7})\b|"pid"\s*:\s*(\d{2,7})\b', re.I | re.M)
# programs that run a script or module named among their arguments (macOS shows framework Python as .../MacOS/Python)
LAUNCHER = re.compile(r'^(?:python(?:3(?:\.\d+)?)?|pypy3?|node|nodejs|bun|deno|tsx|ts-node|ruby|perl|php|bash|sh|zsh|'
                      r'dash|ksh|uv|poetry|pipenv|pdm|hatch|npx)$', re.I)
INLINE = re.compile(r'^-(?:[a-zA-Z]*c[a-zA-Z]*|e|E|p|r)$|^--(?:eval|print|command)\b')  # inline code: no script
SCRIPT_LIKE = re.compile(r'\.(?:py|pyw|js|mjs|cjs|ts|mts|rb|pl|sh|bash|zsh|php)$', re.I)
PLAIN_NAME = re.compile(r'[\w.-]{1,120}')
FREE_LOCK = ('{path} holds no process id; no process on this Mac has it open, and none runs {program} now (Pebble '
             'checked from outside the sandbox, which commands cannot see past). Unless another program takes this lock '
             'while it runs, it is stale: removing it is the fix, then run the command again.')
HELD_LOCK = ('{path} is in use: {name} (PID {pid}) {how} on this Mac now (Pebble checked from outside the sandbox). '
             'Removing the lock while that runs could break it.')
# The same fact asked directly (the lead, 1.9.29, Opus d-rc-240 phrasing 1: the run read the lock, tried `ps` and
# `pgrep -af rebuild_catalog`, both blind in the sandbox, and stopped): a pgrep that cannot get the process list is
# answered by the host from outside it, for a plain name only, by program and script names only (an executable's name,
# or a script or module a launcher runs), never other arguments, which may hold secrets.
SANDBOX_EXEC = re.compile(r"sandbox-exec: execvp\(\) of .{1,400} failed: operation not permitted", re.I)
BLIND_LIST = re.compile(r'cannot get process list|sysmond service not found|execvp\(\) of \S*/(?:ps|pgrep)\b', re.I)
LISTED = ('Commands cannot see this Mac\'s processes from inside Pebble\'s sandbox, so pgrep saw none. Pebble checked '
          'from outside the sandbox: {answer}')
PGREP_FLAGS = frozenset('afilnox')
# ps and the other process listers cannot run in the sandbox at all (Opus d-rc-240 phrasing 3 read the lock, tried
# `ps -eo ...`, was refused and stopped): their refusal or failure names the question the host can answer.
PROCESS_LISTERS = frozenset({'ps', 'top', 'htop', 'lsof', 'pidof', 'pstree'})
LIST_HINT = ('Commands cannot see this Mac\'s processes from inside Pebble\'s sandbox. To ask whether a program runs, '
             'run `pgrep <part of its name>`: Pebble answers that from outside the sandbox, by program name.')


def cause_note(output, root, skip=(), command=None):
    """Phase 46 (1.9.29): what a failed command's permission error comes from, as far as the host can see it (DENIED
    lines of its output; `skip` holds path parts another note covers, the card-free command's code folders), and whether
    a process its lock or PID file names still runs, or, for a lock file in the project that names none, whether a
    process holds it or runs the failed `command`'s program; None when the host sees nothing."""
    root = Path(root).resolve()
    notes, seen = [], set()

    def add(text):
        if text not in notes and len(notes) < 3:
            notes.append(text)
    lines = (output or '').splitlines()[:400]
    for index, line in enumerate(lines):
        if not DENIED.search(line) or SANDBOX_EXEC.search(line):
            continue  # 1.9.29 (the lead): the sandbox refusing to run a program is no file permission to explain
        context = '\n'.join(lines[max(0, index - 6):index + 1])  # the traceback frame or tool line before it
        names = [m.group(1) for m in QUOTED.finditer(line)] + [m.group(1).strip() for m in COLON_PATH.finditer(line)]
        found = False
        for name in names:
            name = name.strip()
            if not name or name in seen or len(name) > 400 or not re.search(r'[/.\w]', name) or name[0] in '[~':
                continue  # "[Errno 13]"; "~" is the command's own temporary HOME
            seen.add(name)
            target = Path(os.path.normpath(name if os.path.isabs(name) else str(root / name)))
            if target.parts[:2] == ('/', 'dev'):
                continue
            inside = target == root or target.is_relative_to(root)
            rel = target.relative_to(root).as_posix() if inside else str(target)
            if inside and any(part in skip for part in Path(rel).parts):
                found = True  # another note covers it
                continue
            if PRIVATE_PART.search(rel):
                add(PRIVATE_CAUSE.format(path=rel))
                found = True
            elif not inside:
                add(OUTSIDE_CAUSE.format(path=name))
                found = True
            else:
                held = _read_only(root, target)
                if held is not None:
                    path, mode, folder = held
                    add(MODE_CAUSE.format(path=path, mode=mode, inside=', so files in it can be added and removed '
                                          'again' if folder else ''))
                    found = True
                elif EXEC_WORDS.search(context):
                    runnable = _cannot_run(root, target)
                    if runnable is not None:
                        add(EXEC_CAUSE.format(path=runnable[0], mode=runnable[1]))
                        found = True
        if found or not re.search(r'operation not permitted|\bEPERM\b', line, re.I):
            continue
        if SIGNAL_WORDS.search(context) and not SOCKET_WORDS.search(line):
            add(SIGNAL_CAUSE)
        elif SOCKET_WORDS.search(context):
            add(SOCKET_CAUSE)
    pids = []
    for index, line in enumerate(lines):
        near = ' '.join(lines[max(0, index - 1):index + 2])
        pids += [int(m.group(1) or m.group(2)) for m in PID_WORDS.finditer(line) if m.group(2) or HELD_WORDS.search(near)]
    for pid in list(dict.fromkeys(pids))[:2]:
        state = _process(pid)
        if state == 'gone':
            add(STALE_PID.format(pid=pid))
        elif state:
            add(LIVE_PID.format(pid=pid, name=state))
    if not pids:
        for text in _lock_notes(lines, root, command, skip):
            add(text)
    if BLIND_LIST.search(output or ''):
        answer = _pgrep_answer(command)
        if answer:
            add(LISTED.format(answer=answer))
        elif _lister(command):
            add(LIST_HINT)
    return ' '.join(notes) or None


def _lister(command):
    """Whether a command line runs a process lister (PROCESS_LISTERS), after any NAME=value words."""
    words = _words(command)
    return bool(words) and Path(words[0]).name in PROCESS_LISTERS


def process_list_hint(error, command):
    """LIST_HINT for a process lister the sandbox refused to start (App appends it to the refusal), else None."""
    return LIST_HINT if isinstance(error, PolicyError) and 'could not start' in str(error) and _lister(command) \
        else None


def _words(command):
    """A command line's words after any NAME=value words; [] when it cannot be read."""
    try:
        words = shlex.split(command or '')
    except ValueError:
        return []
    while words and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', words[0], re.S):
        words = words[1:]
    return words


def _unpathed(match):
    """PATHISH.sub's replacement: a path or file name (a slash, or a dot inside a word) becomes a space."""
    token = match.group(0)
    return ' ' if '/' in token or re.search(r'\w\.\w|^\.\w', token) else token


def _processes():
    """[(pid, executable path, argument words)] of every process on this Mac but Pebble's own, seen from the host (ps);
    None when it cannot tell. The executable comes from ps's comm column printed last, which keeps a path's spaces
    (the second review: "/…/My Bin/bin/syncd" read as "My" from the arguments). Arguments are compared here, and only
    a script's own name (_script_of) is ever shown."""
    try:
        comm = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, errors='replace',
                              timeout=5)
        args = subprocess.run(['/bin/ps', '-axo', 'pid=,args='], capture_output=True, text=True, errors='replace',
                              timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if comm.returncode != 0 or args.returncode != 0:
        return None
    exes = {}
    for line in comm.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            exes[int(parts[0])] = parts[1]
    found = []
    for line in args.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) != os.getpid() and int(parts[0]) in exes:
            found.append((int(parts[0]), exes[int(parts[0])], parts[1].split()))
    return found


def _script_of(words):
    """The script or module a launcher's words (after the launcher itself) run: the module after -m before any
    operand, the first word with a script suffix, or the first operand with a slash that no option precedes. None for
    inline code, a slash word right after an option (it may be the option's value: `node --require dotenv/config`,
    `perl -I lib/perl5`), a word with ':', '@' or '=' (an address, an assignment) or no script."""
    after_option = False
    for index, word in enumerate(words):
        if INLINE.match(word):
            return None
        if word == '-m':
            module = words[index + 1] if index + 1 < len(words) else ''
            return module if PLAIN_NAME.fullmatch(module) else None
        if word.startswith('-'):
            after_option = True
            continue
        if ':' in word or '@' in word or '=' in word:
            return None
        name = Path(word).name
        if SCRIPT_LIKE.search(word) or ('/' in word and not after_option):
            return name if PLAIN_NAME.fullmatch(name) else None
        if '/' in word or not after_option:
            return None  # a slash word after an option, or a bare first operand: not sure which is the script
        after_option = False  # the option's value
    return None


def _names(exe, words):
    """The program names a process may be found by: its executable's name and, for a launcher, the script or module
    it runs (_script_of: never another argument)."""
    name = Path(exe).name
    names = {name} if name else set()
    if LAUNCHER.match(name):
        script = _script_of(words[1:])
        if script:
            names.add(script)
    return names


def _shown(name):
    """A program name as a note may show it: a plain name, else nothing."""
    return name if PLAIN_NAME.fullmatch(name or '') else ''


def _pgrep_answer(command):
    """For `pgrep [-afilnox] <plain name>`: which running programs' names hold the name (-i: any case; -x: the whole
    name), as the host sees them: an exact name first, then scripts, then other programs, at most three and how many
    more; None for any other pgrep (a pattern, a PID file, a user, a parent ...), any other command, or when the host
    cannot tell."""
    words = _words(command)
    if not words or Path(words[0]).name != 'pgrep':
        return None
    flags = [word[1:] for word in words[1:] if word.startswith('-')]
    operands = [word for word in words[1:] if not word.startswith('-')]
    if any(not flag or not set(flag) <= PGREP_FLAGS for flag in flags) or len(operands) != 1 \
            or not PLAIN_NAME.fullmatch(operands[0]):
        return None
    options, pattern = ''.join(flags), operands[0]
    processes = _processes()
    if processes is None:
        return None
    fold = (lambda text: text.casefold()) if 'i' in options else (lambda text: text)
    found = []
    for pid, exe, args in processes:
        exe_name = Path(exe).name
        if exe_name == 'pgrep':
            continue
        script = next(iter(_names(exe, args) - {exe_name}), None)
        for name, rank in ((script, 1), (exe_name, 2)):
            if not name:
                continue
            hit = fold(name) == fold(pattern) if 'x' in options else fold(pattern) in fold(name)
            if hit:
                exact = fold(name) == fold(pattern) or fold(Path(name).stem) == fold(pattern)
                found.append((0 if exact else rank, pid, _shown(name), _shown(exe_name) or 'its name unreadable'))
                break
    if not found:
        return ('no process on this Mac runs a program or script whose name %s "%s" (Pebble compares program and script '
                'names, never other arguments).' % ('is' if 'x' in options else 'contains', pattern))
    found.sort(key=lambda item: (item[0], item[1]))
    shown = ['%s (PID %d, %s)' % (name, pid, exe) if name and name != exe else '%s (PID %d)' % (exe, pid)
             for _, pid, name, exe in found[:3]]
    more = ' and %d more' % (len(found) - 3) if len(found) > 3 else ''
    return 'running now: ' + '; '.join(shown) + more + '.'


def _lock_notes(lines, root, command, skip=()):
    """The host's facts about the process lock files a failed command's output names on a line that speaks of
    contention, at most two notes from at most four files: inside the project (by real path), regular, owned by the
    person, at most 4 KB, neither private nor in a code folder another note covers (`skip`), never a dependency
    lockfile."""
    notes, seen, examined = [], set(), 0
    for line in lines:
        if len(notes) >= 2 or examined >= 4:
            break
        if not LOCK_WORD.search(line) or not CONTENTION.search(PATHISH.sub(_unpathed, line)):
            continue  # the second review: a path on the line ("tests/fixtures/stale.lock") never says contention
        for token in PATHISH.findall(line):
            token = token.rstrip('.,;:')
            name = token.rsplit('/', 1)[-1]
            if len(notes) >= 2 or examined >= 4 or len(token) > 400 or not ('.' in token or '/' in token) \
                    or token[0] == '~' or not PROCESS_LOCK.match(name) or DEPENDENCY_LOCK.search(name):
                continue
            target = Path(os.path.normpath(token if os.path.isabs(token) else str(root / token)))
            real = Path(os.path.realpath(target))
            if real in seen or not target.is_relative_to(root) or target == root or not real.is_relative_to(root):
                continue  # once per file; no linked folder on the way out
            seen.add(real)
            rel = target.relative_to(root).as_posix()
            if PRIVATE_PART.search(rel) or 'dependency-locks' in Path(rel).parts \
                    or any(part in skip for part in Path(rel).parts):
                continue
            examined += 1
            text = _small_text(target)
            if text is None or DEPENDENCY_TEXT.search(text) or text.count('\n') > 8:
                continue  # unreadable, a dependency lockfile or other data: not a process lock
            named = FILE_PID.search(text)
            if named:
                pid = int(next(group for group in named.groups() if group))
                state = _process(pid)
                if state == 'gone':
                    notes.append(STALE_PID.format(pid=pid))
                elif state:
                    notes.append(LIVE_PID.format(pid=pid, name=state))
                continue
            holders = _open_by(target)
            if holders is None:
                continue  # the host cannot tell: nothing is said
            if holders:
                notes.append(HELD_LOCK.format(path=rel, name=holders[0][1], pid=holders[0][0], how='has it open'))
                continue
            program = _program(command, root)
            running = _running(program) if program else None
            if running is None:
                continue
            if running:
                notes.append(HELD_LOCK.format(path=rel, name=running[1], pid=running[0], how='runs ' + program))
            else:
                notes.append(FREE_LOCK.format(path=rel, program=program))
    return notes


READ_FREE = ('No process on this Mac has {path} open (Pebble checked from outside the sandbox, which commands cannot see '
             'past). If the program that takes this lock is not running (`pgrep <part of its name>` asks Pebble), the lock '
             'is stale: removing it is the fix.')


def lock_read_note(root, path):
    """Phase 46 (1.9.29, the lead; final check d-rc-109 phrasing 3 on tree 29g: the run read the lock, asked ps and
    pgrep, learned no import ran, and still left the stale lock because the request kept its folder: the decisive
    "removing it is the fix" came only with a failed run's note): the host's fact about a process lock or PID file the
    model Reads, as _lock_notes gives it for a failed command's output. A process id in it gets STALE_PID or LIVE_PID;
    without one, whether a process has it open (HELD_LOCK) or READ_FREE. None for any other file, or when the host
    cannot tell. Never raises."""
    try:
        root = Path(root).resolve()
        target = Path(os.path.normpath(path if os.path.isabs(path) else str(root / path)))
        name = target.name
        if not PROCESS_LOCK.match(name) or DEPENDENCY_LOCK.search(name) or not target.is_relative_to(root) \
                or target == root or not Path(os.path.realpath(target)).is_relative_to(root):
            return None
        rel = target.relative_to(root).as_posix()
        if PRIVATE_PART.search(rel) or 'dependency-locks' in Path(rel).parts:
            return None
        text = _small_text(target)
        if text is None or DEPENDENCY_TEXT.search(text) or text.count('\n') > 8:
            return None
        named = FILE_PID.search(text)
        if named:
            pid = int(next(group for group in named.groups() if group))
            state = _process(pid)
            return STALE_PID.format(pid=pid) if state == 'gone' else LIVE_PID.format(pid=pid, name=state) if state \
                else None
        holders = _open_by(target)
        if holders is None:
            return None
        if holders:
            return HELD_LOCK.format(path=rel, name=holders[0][1], pid=holders[0][0], how='has it open')
        return READ_FREE.format(path=rel)
    except Exception:  # a note never costs the read
        return None


def _small_text(target):
    """The text of a regular file of at most 4 KB owned by the person, read without following a link and without
    blocking (a FIFO swapped in after lstat never hangs the backend: the opened file must be the one lstat saw); None
    for anything else."""
    try:
        info = os.lstat(target)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 4096:
        return None
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return None
        return os.read(fd, 4096).decode('utf-8', 'replace')
    except OSError:
        return None
    finally:
        os.close(fd)


def _open_by(path):
    """The processes (pid, name) that have `path` open on this Mac, seen from the host (lsof); [] for none, None
    when the host cannot tell (lsof's 1 means none only with nothing on stderr)."""
    try:
        out = subprocess.run(['/usr/sbin/lsof', '-F', 'pc', '--', str(path)], capture_output=True, text=True,
                             errors='replace', timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode not in (0, 1) or (out.returncode == 1 and (out.stdout.strip() or out.stderr.strip())):
        return None
    found, pid = [], None
    for line in out.stdout.splitlines():
        if line[:1] == 'p' and line[1:].isdigit():
            pid = int(line[1:])
        elif line[:1] == 'c' and pid is not None and pid != os.getpid():
            found.append((pid, _shown(line[1:].strip()) or 'its name unreadable'))
    return found


def _program(command, root=None):
    """The script or module a failed command line ran, as a name to look for among running processes: the script a
    launcher ran (_script_of: `python3 -W ignore scripts/sync.py --once` -> sync.py; `python3 -m tools.sync` ->
    tools.sync), or a script run by its path inside the project (`./scripts/sync.sh` -> sync.sh). None for a runner
    such as make or npm, wherever it lives (it never holds a lock itself), inline code, an option's value that could be
    the script, or a line that cannot be read: then the host never says a lock is stale."""
    words = _words(command)
    if not words:
        return None
    exe = Path(words[0]).name
    if LAUNCHER.match(exe):
        return _script_of(words[1:])
    if '/' not in words[0] or root is None:
        return None
    root = Path(root)
    target = Path(os.path.normpath(words[0] if os.path.isabs(words[0]) else str(root / words[0])))
    return _shown(exe) or None if target.is_relative_to(root) and target != root else None


def _running(program):
    """A process (pid, name) on this Mac that runs `program`: an executable of that name, or a launcher with an
    argument of that file name (a shell that runs it inline counts too, so the host errs towards "in use"); () for
    none, None when the host cannot tell."""
    processes = _processes()
    if processes is None:
        return None
    for pid, exe, args in processes:
        name = Path(exe).name
        mentioned = bool(LAUNCHER.match(name)) and any(Path(word.strip('\'"`;&|()')).name == program
                                                       for word in args[1:])
        if name == program or mentioned:
            return pid, _shown(name) or 'its name unreadable'
    return ()


def _process(pid):
    """Whether process `pid` runs on this Mac, seen from the host: 'gone', its name when it runs, or None (unknown, or
    Pebble's own process)."""
    if not 1 < pid < 2 ** 31 or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)  # signal 0: nothing is sent, only whether the process exists
    except ProcessLookupError:
        return 'gone'
    except PermissionError:
        pass  # another account's process: it runs
    except OSError:
        return None
    try:
        out = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'comm='], capture_output=True, text=True,
                             errors='replace', timeout=5)  # comm alone (last) keeps the whole path
        return (_shown(Path(out.stdout.strip()).name) if out.returncode == 0 else '') or 'its name unreadable'
    except (OSError, subprocess.SubprocessError):
        return 'its name unreadable'


def _cannot_run(root, target):
    """Phase 46 (1.9.29, the lead): (project-relative path, its mode in ls form) of a regular file inside root that its
    owner can read and write and nobody may execute, or None (a link, a folder, anything with an exec bit)."""
    if target == root or not target.is_relative_to(root):
        return None
    try:
        info = os.lstat(target)
    except OSError:
        return None
    mode = info.st_mode
    if not stat.S_ISREG(mode) or mode & 0o111 or not (mode & stat.S_IRUSR and mode & stat.S_IWUSR):
        return None
    return target.relative_to(root).as_posix(), stat.filemode(mode)


def _read_only(root, target):
    """(project-relative path, its mode in ls form, whether a folder) of the nearest of target and its folders inside
    root that its owner cannot write (the file itself, else the folder that holds it), or None."""
    for path in (target, *target.parents):
        if path == root or not path.is_relative_to(root):
            return None  # the project folder itself is not chmod's (_mode)
        try:
            info = os.lstat(path)
        except OSError:
            continue  # absent: the folder that would hold it decides
        if stat.S_ISLNK(info.st_mode):
            return None
        if not info.st_mode & stat.S_IWUSR:
            return path.relative_to(root).as_posix(), stat.filemode(info.st_mode), stat.S_ISDIR(info.st_mode)
        if path == target and stat.S_ISREG(info.st_mode):
            continue  # a writable file in a read-only folder can still not be removed or renamed
        return None
    return None


# Phase 46 (1.9.28, T2; D178, the person's decision "Allow both, reviewed": on unless PEBBLE_1928_TERMINAL=0): a reviewed
# command may add the exec bit to one regular project file (chmod +x, u+x or a+x) and set NAME=value variables for
# itself. Names that decide how programs are found, loaded or configured stay refused, as do values with $, a backtick
# or a newline, and words written with quotes or escapes (a shell runs `'FOO=1' ls` as a program named FOO=1); sh
# script.sh, env and every other Phase 10 refusal stay as they are. Each equals what python3 -c already does in the same
# sandbox.
ENV_WORD = re.compile(r'([A-Za-z_][A-Za-z0-9_]{0,63})=(.*)', re.S)
PLAIN_ENV = re.compile(r'(?:[A-Za-z_][A-Za-z0-9_]*=[^\s\'"\\;&|<>]*(?:\s+|$|(?=[;&|<>])))*')
DENIED_ENV = re.compile(r'PATH|HOME|TMPDIR|SHELL|USER|LOGNAME|IFS|ENV|BASH_ENV|ZDOTDIR|PS4|PROMPT_COMMAND|CDPATH'
                        r'|DEVELOPER_DIR|SDKROOT|DYLD_\w*|LD_\w*|PYTHON\w*|NODE_\w*|NPM_\w*|PERL\w*|RUBY\w*|GEM_\w*|GIT_\w*'
                        r'|SSL_\w*|SSH_\w*|CURL_\w*|REQUESTS_CA_BUNDLE|\w*_PROXY|MALLOC\w*|__CF\w*'
                        r'|XDG_\w*|BROWSER|EDITOR|VISUAL|PAGER|PYTEST_\w*', re.I)
# Phase 46 (1.9.28, the follow-ups security review 11: JAVA_TOOL_OPTIONS, GOFLAGS, RUSTC_WRAPPER, CC and PIP_INDEX_URL
# passed the deny list, which no list of tools' settings can complete): a variable is set only for the project's own
# program, as written, that reads it: a project file or module run by python3 or node, their inline code, or the
# project's tests under pytest or unittest (d-rc-106: REPORT_DATE=2026-09-30 python3 scripts/aging.py). Any other
# program (make, go, java, cargo, npm, pip, ls ...) gets only the names that shape every program's output (OUTPUT_ENV);
# its own settings decide how it is found, loaded or configured. Above, the deny list keeps what reaches the runtimes
# themselves and what their standard libraries start or read settings from (XDG_*, BROWSER, EDITOR, VISUAL, PAGER,
# PYTEST_*).
OUTPUT_ENV = re.compile(r'TZ|LANG|LC_[A-Z]+|NO_COLOR|CI')
RUNTIMES = {'python3': ('-c',), 'python': ('-c',), 'node': ('-e', '--eval')}
TEST_MODULES = ('pytest', 'unittest')
EXEC_BITS = ('+x', 'u+x', 'a+x')
# Phase 46 (1.9.29, golden held-out recovery: 6 honest stops of 15 on both models; D212: the failed command printed a
# lock with a permission error, then the run wrote nothing and stopped): a project file or folder left read-only (an
# interrupted run's output folder, a lock it could not remove) could only be reported, since chmod took nothing but the
# exec bit (D178), where Claude Code and Codex run `chmod u+w` and go on. A reviewed chmod may now also set the read and
# write bits of one project file or folder (u+w, u-w, 644, 755; -R for a folder), as the project's own
# `python3 -c "os.chmod(...)"` already may in the same sandbox. Group and world write stay refused (on a Mac every
# account shares the staff group, so either lets another account change the project's code), and so do setuid, setgid
# and sticky bits, ACLs, links, several paths at once, the project folder itself, Git metadata and private files. A
# mode without "who" (`+w`) is the umask's to narrow, as in the person's own terminal. PEBBLE_1929_MODES=0 restores 1.9.28.
MODE_CLAUSE = re.compile(r'([ugoa]*)([+=-])([rwX]*)')
OCTAL_MODE = re.compile(r'0?[0-7]{3}')
MODE_HINT = (' chmod here adds a file\'s exec bit (+x), or sets the read and write bits of one project file or folder '
             '(u+w, u-w, 644, 755; -R for a folder); group or world write (g+w, o+w, a+w, 777, 666), setuid, setgid and '
             'sticky bits, links, other modes, options and several files at once are blocked. `chmod u+w PATH` gives its '
             'owner write access.')  # test_phase10_security pins "other modes, options and several files at once ..."


def mode_switch():
    return os.environ.get('PEBBLE_1929_MODES', '1') != '0'  # Phase 46 (1.9.29): on by default


def terminal_switch():
    return os.environ.get('PEBBLE_1928_TERMINAL', '1') != '0'  # Phase 46 (1.9.28, D178): on by default


def inspect_command(command, root, network=False):
    ws = Workspace(root)
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > 4000
        or type(network) is not bool
        or "\n" in command
        or "\r" in command
        or "\x00" in command
    ):
        raise PolicyError("Enter a single command shorter than 4000 characters.")
    if re.search(r"\$\(|`|\$\{", command):
        raise PolicyError(
            "Command substitutions and environment overrides are blocked." + SUBSTITUTION_HINT
        )
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = list(lexer)
    except ValueError as exc:
        raise PolicyError("Check the command's quoting.") from exc
    # Only a leading NAME=value word overrides the environment (as in shell_policy); the same text inside an argument,
    # such as an assignment in `python3 -c "p = 1; q=2"`, reaches the program as it is.
    env = {}
    if argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0], re.S):
        if not terminal_switch():
            raise PolicyError(
                "Command substitutions and environment overrides are blocked." + SUBSTITUTION_HINT
            )
        plain = PLAIN_ENV.match(command.lstrip()).group().split()
        while argv and ENV_WORD.fullmatch(argv[0]):  # Phase 46 (1.9.28, T2; D178)
            name, value = ENV_WORD.fullmatch(argv[0]).groups()
            if len(env) >= len(plain) or plain[len(env)] != argv[0]:
                raise PolicyError('Quoted or escaped environment overrides are blocked: write NAME=value plainly before '
                                  'the command (no quotes, spaces or backslashes).' + SUBSTITUTION_HINT)
            if DENIED_ENV.fullmatch(name) or any(c in value for c in '$`\n\r\0') or len(value) > 1000 or name in env:
                raise PolicyError(f'{name} cannot be set for a command here: environment overrides that decide how '
                                  'programs are found, loaded or configured, or whose value holds $, a backtick or a '
                                  'newline, are blocked.' + SUBSTITUTION_HINT)
            env[name] = value
            argv = argv[1:]
    if not argv or any(re.fullmatch(r"[;&|<>]+", a) for a in argv):
        raise PolicyError(
            "Run one command at a time. Pipes, redirections and chained shell commands are blocked." + ONE_COMMAND_HINT
        )
    executable = Path(argv[0]).name.casefold()
    effect = None
    if executable == 'chmod' and terminal_switch():  # Phase 46 (1.9.28, T2): the exec bit of one project file only
        effect = _exec_bit(argv, ws) if not mode_switch() or argv[1:2] and argv[1] in EXEC_BITS else _mode(argv, ws)
    if executable == 'git':
        raise PolicyError('Raw Git commands are outside the generic terminal broker; use the typed Git tools. Read with '
                          'ReadGitStatus, ReadGitDiff or ReadGitHistory (RunCommand also runs read-only git status, log, '
                          'show, diff, branch, tag and blame as typed); change with ProposeGitChange, whose operation '
                          '"command" runs other everyday Git commands under review, e.g. argv ["revert", "a1b2c3d"] or '
                          '["stash", "push"].')  # Phase 46: app.py routes those reads to GitWorkflows.raw
    if (
        executable in BLOCKED and effect is None
        or executable.startswith("mkfs")
        or executable in {"env", "xargs", "eval", "exec", "nohup"}
    ):
        raise PolicyError(
            "Privileged commands, shell wrappers and system administration are blocked. Run them yourself outside Pebble."
            + (SHELL_HINT if executable in {"bash", "sh", "zsh", "fish", "csh", "env", "exec", "eval"} else
               CHMOD_HINT if executable == "chmod" else "")
        )
    if re.search(
        r"(?:^|[/\\\s])(?:\.git|\.env(?:\.[\w-]+)?|\.ssh|\.aws|\.gnupg|\.netrc|\.npmrc|\.pypirc|\.pebble|credentials|secrets)(?:[/\\\s]|$)|\.(?:pem|key|p12|pfx)(?:\s|$)",
        command,
        re.I,
    ):
        raise PolicyError("Credential access is blocked.")
    from .data_home import tool_refused
    for arg in [*argv[1:], *env.values()]:  # Phase 46 (1.9.28, D178): a variable's value is held to the same paths
        if arg in {"/", str(Path.home()), "~", ".."} or arg.startswith("../"):
            raise PolicyError("Command paths must stay in the selected project.")
        # Phase 22 (s22 P5): ~/.pebble, PEBBLE_HOME and the old app-data folder are outside command access.
        if arg.startswith(("/", "~")) and tool_refused(arg):
            raise PolicyError("Pebble's own data folders are outside command access.")
    named = next((name for name in env if not OUTPUT_ENV.fullmatch(name)), None)
    if named and not _own_program(argv, ws):  # Phase 46 (1.9.28, the follow-ups security review 11)
        raise PolicyError(f'{named} cannot be set for {Path(argv[0]).name} here: a variable is set only for the '
                          "project's own program, which reads it as written (python3 or node running a project file or "
                          "module, -c code, or the project's tests under pytest or unittest). Other programs get only "
                          'TZ, LANG, LC_*, NO_COLOR and CI: their own settings decide how they are found, loaded or '
                          'configured, so they are blocked.' + SUBSTITUTION_HINT)
    dangerous = (
        executable
        in {
            "rm",
            "rmdir",
            "mv",
            "install",
            "brew",
            "pip",
            "pip3",
            "npm",
            "npx",
            "yarn",
            "pnpm",
            "uv",
            "cargo",
            "python",
            "python3",
            "node",
            "ruby",
            "perl",
            "curl",
            "wget",
            "ssh",
            "scp",
            "docker",
            "make",
        }
        or executable == "git"
        and any(
            a
            in {
                "reset",
                "clean",
                "push",
                "checkout",
                "restore",
                "rebase",
                "commit",
                "apply",
                "clone",
                "pull",
                "fetch",
            }
            for a in argv[1:]
        )
        or executable == "find"
        and any(a in {"-delete", "-exec", "-execdir"} for a in argv)
    )
    readonly = (
        executable in READ_ONLY
        or executable == "git"
        and len(argv) > 1
        and argv[1] in {"status", "diff", "log", "show", "branch", "rev-parse"}
        and not any(
            a in {"-D", "-d", "--delete", "-c", "--exec", "--output"} for a in argv
        )
    )
    checked = {
        "root": str(ws.root),
        'root_identity': list(ws.identity),
        "command": command,
        "argv": argv,
        "network": network,
        "risk": "network"
        if network
        else "dangerous"
        if dangerous
        else "read-only"
        if readonly
        else "execution",
        "requires_review": network or dangerous or not readonly,
    }
    if env:  # Phase 46 (1.9.28, T2): what the review card says in words, and what run_command applies
        checked.update(env=env, risk='dangerous', requires_review=True,
                       effect='Sets ' + ', '.join(f'{name}={value}' for name, value in env.items())
                              + ' for this command only (nothing else in its environment changes)')
    if effect:
        checked.update(risk='dangerous', requires_review=True, effect=effect)
    return checked


def _own_program(argv, ws):
    """Phase 46 (1.9.28, review 11): whether argv runs the project's own code through its runtime: a project file or
    module, inline code, or the project's tests (TEST_MODULES)."""
    inline = RUNTIMES.get(Path(argv[0]).name.casefold())
    rest = argv[1:]
    while inline is not None and rest and rest[0].startswith('-') and rest[0] not in ('-m', *inline):
        rest = rest[1:]  # the runtime's own flags (-u, -B, --no-warnings)
    if inline is None or not rest:
        return False
    if rest[0] in inline:
        return True
    if rest[0] == '-m':
        module = rest[1] if len(rest) > 1 else ''
        parts = module.split('.')
        return module in TEST_MODULES or all(re.fullmatch(r'[A-Za-z_]\w*', part) for part in parts) and any(
            base.joinpath(*parts[:-1], parts[-1] + '.py').is_file() or base.joinpath(*parts, '__init__.py').is_file()
            for base in (ws.root, ws.root / 'src'))
    try:
        ws.path(rest[0], create=True)  # a project file (the runtime itself says when it is missing)
    except (PolicyError, OSError):
        return False
    return True


def _exec_bit(argv, ws):
    """Phase 46 (1.9.28, T2): `chmod +x|u+x|a+x FILE`, one regular file inside the project (no link, no private file):
    the card's sentence; anything else is refused as before."""
    if len(argv) != 3 or argv[1] not in EXEC_BITS:
        raise PolicyError('Only chmod +x (or u+x, a+x) of one regular project file is available here; other modes, '
                          'options and several files at once are blocked.' + EXEC_BIT_HINT)
    try:
        path = ws.path(argv[2])
        regular = stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        regular = False
    if not regular:
        raise PolicyError('chmod +x takes one existing regular file inside the project; links, folders and missing '
                          'files are blocked.' + EXEC_BIT_HINT)
    return (f'Adds the exec bit to {path.relative_to(ws.root).as_posix()} so it can run by its path (nothing else about '
            'the file changes)')


def _mode(argv, ws):
    """Phase 46 (1.9.29): `chmod [-R] MODE PATH` for the read and write bits of one project file or folder (the exec bit
    of a file is _exec_bit's): the card's sentence; PolicyError for anything else (MODE_HINT)."""
    args = argv[1:]
    recursive = args[:1] == ['-R']
    args = args[1:] if recursive else args
    if len(args) != 2:
        raise PolicyError('This chmod is blocked: one mode and one path only.' + MODE_HINT)
    mode, name = args
    if OCTAL_MODE.fullmatch(mode):
        wide = int(mode, 8) & 0o022
    elif all(MODE_CLAUSE.fullmatch(part) for part in mode.split(',')):
        wide = any('w' in perms and op in '+=' and set(who) & set('goa')
                   for who, op, perms in (MODE_CLAUSE.fullmatch(part).groups() for part in mode.split(',')))
    else:
        raise PolicyError(f'chmod {mode} is blocked here.' + MODE_HINT)
    if wide:
        raise PolicyError(f'chmod {mode} would let other accounts write there, which is blocked.' + MODE_HINT)
    raw, root = os.path.normpath(name), str(ws.root).rstrip('/')
    rel = Path(raw[len(root) + 1:] if raw.startswith(root + '/') else raw)  # an absolute path inside, as Workspace.path
    if rel.is_absolute() or rel.parts[:1] in ((), ('.',), ('..',)) or any(
            part.lower() in PRIVATE or part.lower().startswith('.env') for part in rel.parts):
        raise PolicyError('chmod takes one file or folder inside the project (not the project folder itself, Git '
                          'metadata or a private file).' + MODE_HINT)
    path, cursor = ws.root / rel, ws.root
    for part in rel.parts:  # no link anywhere on the way, as Workspace.path
        cursor = cursor / part
        if cursor.is_symlink():
            raise PolicyError('chmod does not follow links here.' + MODE_HINT)
    try:
        kind = os.lstat(path).st_mode
    except OSError:
        kind = 0
    if not (stat.S_ISREG(kind) or stat.S_ISDIR(kind)) or recursive and not stat.S_ISDIR(kind):
        raise PolicyError('chmod takes one existing file or folder inside the project (-R a folder only).' + MODE_HINT)
    return (f'Sets the permissions of {rel.as_posix()}{" and of everything in it" if recursive else ""} to {mode} (its '
            'contents and anything else stay as they are; no other account gains write access)')


def resolve_executable(name):
    path = Path(name)
    if path.is_absolute():
        if not path.is_file():
            raise PolicyError("Command executable does not exist.")
        return path.resolve()
    if "/" in name or "\\" in name:
        raise PolicyError(EXEC_RUNTIME_HINT if terminal_switch() else RUNTIME_HINT)  # Phase 46 (1.9.28, D178)
    if name in {"python", "python3"}:
        options = [
            Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"),
            Path("/usr/bin/python3"),
        ]
    elif name == "node":
        # Phase 46 (1.9.27): the node RunTests runs (recipes._node: the one on PATH, else nvm's newest by version), not
        # the last nvm folder in text order, which put v9 after v24 and v20.9 after v20.18. The golden checkers run the
        # node on PATH (or GOLDEN_NODE), so a terminal check under another Node could pass where theirs fails.
        try:
            options = [recipes._node()]
        except PolicyError:
            options = []
    else:
        options = [
            Path(p) / name
            for p in ("/usr/bin", "/bin", "/opt/homebrew/bin", "/usr/local/bin")
        ]
    executable = next((p.resolve() for p in options if p.is_file()), None)
    if not executable:
        found = shutil.which(name)
        executable = Path(found).resolve() if found else None
    if not executable:
        raise PolicyError("This command is not installed on your device.")
    return executable


def project_environment(root):
    """Phase 46 (1.9.27): what the golden checkers give every command they run (checkers/common.py run_env), so a
    command passes or fails here as it does there: PYTHONDONTWRITEBYTECODE=1, and PYTHONPATH the project root, then src/
    when it is a folder. Without them `python3 scripts/report.py` could not import the project's own package (the
    checker could, and Codex and Claude Code type PYTHONPATH=.; NAME=value is refused here), a src-layout project's
    `python3 -m pkg.cli` failed only here (so a request's own output check could never pass), and each Python run wrote
    __pycache__ into the project, where a same-size edit within the same second runs the old bytecode."""
    root = Path(root)
    paths = [root]
    if (root / "src").is_dir() and not (root / "src").is_symlink():
        paths.append(root / "src")
    env = {"PYTHONDONTWRITEBYTECODE": "1"}
    if not any(":" in str(path) for path in paths):  # ":" separates PYTHONPATH entries
        env["PYTHONPATH"] = ":".join(str(path) for path in paths)
    return env


def developer_environment():
    """Phase 46 (1.9.29, w21's sandbox probe): {'DEVELOPER_DIR': the Command Line Tools} when they are installed, as
    git_workflows and recipes already set it. Apple's /usr/bin shims (git, make, clang, cc) follow xcode-select, and with
    Xcode selected (this Mac since 2026-10-29; test_egress.CLT_ENV) every one of them died in a command's sandbox, which
    reads the Command Line Tools and not Xcode: "xcrun: error: unable to load libxcrun", a configuration error that no
    change to the project could fix. Nothing else changes; the sandbox allows the same reads."""
    from .sandbox_profiles import CLT
    return {'DEVELOPER_DIR': str(CLT)} if CLT.is_dir() else {}


def run_command(payload, cancel):
    from .platform_providers import require as require_platform
    require_platform('command')
    checked = inspect_command(
        payload["command"], payload["root"], payload.get("network", False)
    )
    if checked["argv"] != payload["argv"] or checked.get('env') != payload.get('env'):  # Phase 46 (1.9.28, T2): env too
        raise PolicyError("Command changed after review.")
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise PolicyError(
            "Confined terminal execution currently requires macOS. Windows terminal remains disabled until its OS sandbox is available."
        )
    exe = resolve_executable(checked["argv"][0])
    plan = payload.get('plan')
    if plan is not None:
        from .command_plans import verify
        plan = verify(plan, payload['root'])  # re-pins the executable and root identity right before spawn
        step = plan['steps'][0]
        if (plan['pipeline'] != 'none' or plan['network']['mode'] != 'none' or plan['writable'] not in ([], ['.'])
                or bool(plan['writable']) != bool(payload.get('writable', False)) or step['argv'] != payload['argv']
                or plan['process_model'] != 'tree' or plan['interactive'] or step['cwd'] != '.' or step['env']):
            raise PolicyError('This command plan needs the contained plan worker. Nothing was run.')
        exe = Path(step['executable']['realpath'])  # the reviewed pin, not a fresh PATH lookup
    root = Path(checked["root"])
    if payload.get('root_identity') is not None and checked['root_identity'] != payload['root_identity']:
        raise PolicyError('The reviewed project folder was replaced before terminal execution.')
    quote = lambda p: json_quote(str(p))
    with tempfile.TemporaryDirectory(prefix="pebble-command-") as temp:
        temp = Path(temp).resolve()
        toolchain = exe.parent.parent
        if exe.is_relative_to("/usr/bin") or exe.is_relative_to("/bin"):
            toolchain = Path("/Library/Developer/CommandLineTools")
        elif "Python.framework" in str(exe):
            toolchain = Path("/Library/Frameworks/Python.framework")
        if toolchain == Path("/") or toolchain == Path.home():
            raise PolicyError("Cannot establish a confined command runtime.")
        # Phase 46 (1.9.28, the w13b security review F1-F3): a command Auto runs without a card (its payload's
        # `confined`) starts under the containment anchor, with enforced limits and no write where later runs load code
        contained = None
        if payload.get('confined') is not None:
            from .confined import Contained
            contained = Contained(root, exe, bool(payload.get('writable', False)))
        # The unified Phase 14 profile: own-child signals, mach allowlist, exec
        # limited to system paths, the toolchain and the project, the private-path
        # deny and (legacy) open network only when this command was approved for it.
        from .sandbox_profiles import build
        from .keep_paths import absolute
        keep = payload.get('keep') or {}  # Phase 46 (1.9.28, b-new G2): the host's kept paths, none unless switched on
        profile = build(**(contained.profile(toolchain) if contained else
                           {'executable': exe, 'exec_paths': [toolchain, root], 'read_paths': [toolchain]}), project_root=root,
                        temp=temp, writable_paths=[root] if payload.get("writable", False) else (),
                        network="open" if checked["network"] else "none", job_marker=uuid.uuid4().hex,
                        process_model="tree", keep_paths=absolute(root, keep.get('change', ())),
                        undeletable_paths=absolute(root, keep.get('delete', ())))
        env = {
            "PATH": str(exe.parent) + ":/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": str(temp),
            "TMPDIR": str(temp),
            "LANG": "en_US.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            **person_zone(),  # Phase 46: the person's TZ (zone.py)
            **project_environment(root),  # Phase 46: no bytecode; the project on the import path, as the checkers run
            **developer_environment(),  # Phase 46 (1.9.29): /usr/bin's git, make and cc run with Xcode selected too
            **checked.get('env', {}),  # Phase 46 (1.9.28, T2): the reviewed NAME=value words, only while the switch is on
        }
        output = temp / "output.log"
        with output.open("wb") as log:
            if contained:
                process = contained.start(profile, env, log, checked["argv"], COMMAND_SECONDS)
            else:
                process = subprocess.Popen(
                    [
                        "/usr/bin/sandbox-exec",
                        "-p",
                        profile,
                        str(exe),
                        *checked["argv"][1:],
                    ],
                    cwd=root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            stop = contained.stop if contained else _sweep  # Phase 46 (1.9.28, F3): the anchor sweeps its whole instance
            deadline = time.monotonic() + COMMAND_SECONDS
            # The group is signalled only while its leader is unreaped (a zombie
            # still reserves the PID), so the sweep can never reach a reused group.
            watch = LeaderWatch(process.pid)
            stopped = None  # Phase 46: 'timeout' or 'output' when Pebble killed the command (the person's stop raises)
            try:
                while not watch.exited(0.1 if watch.reliable else 0):
                    if cancel.is_set():
                        stop(process)
                        process.wait()
                        raise CommandOutcomeUncertain(
                            "Command stopped, timed out or exceeded the output limit after starting. Its outcome is uncertain; inspect it before retrying."
                        )
                    if time.monotonic() > deadline:
                        stopped = 'timeout'
                    elif output.stat().st_size > OUTPUT_LIMIT:
                        stopped = 'output'
                    elif contained:
                        stopped = contained.governor(process)  # Phase 46 (1.9.28, F2): 'processes' or 'memory'
                    if stopped:
                        break
                    if not watch.reliable:
                        if process.poll() is not None:
                            break
                        time.sleep(0.1)
                # Descendants left in the leader's group must not outlive the command.
                stop(process)
                process.wait()
                code = process.returncode
                if contained:
                    code, reason = contained.finish()
                    stopped = stopped or ('timeout' if reason == 'deadline' else None)
                if stopped and checked["network"]:
                    # Phase 46 (the lead's review): a command with approved network access may have half done
                    # something outside the Mac (an upload, a request), and a retry could repeat it; its kill stays an
                    # uncertain outcome for the person to inspect. Without network everything it did is in the
                    # sandboxed project and temporary folders, where the model can check what it left.
                    raise CommandOutcomeUncertain(
                        "Command stopped, timed out or exceeded the output limit after starting. Its outcome is uncertain; inspect it before retrying."
                    )
                size = output.stat().st_size
                with output.open('rb') as handle:
                    raw = handle.read(SHOWN_BYTES)
                shown, removed, _ = sanitize_terminal(raw, final=True)
                result = {
                    "exit_code": code if stopped is None else TIMEOUT_EXIT,
                    # The model and UI get the terminal_text projection; the raw prefix is attested by hash.
                    "output": shown,
                    "output_projection": "terminal_text/1",
                    "controls_removed": removed,
                    "raw_output_sha256": hashlib.sha256(raw).hexdigest(),
                    "command": checked["command"],
                    "isolation": "macOS sandbox · selected project · "
                    + ("network approved" if checked["network"] else "network denied"),
                }
                if size > len(raw) and not stopped:
                    result.update(output_cut=True, output_bytes=size, note=CUT_NOTE.format(shown=len(raw), size=size))
                if stopped:
                    # Phase 46 (1.9.27, golden held-out recovery): a command that ran past its deadline was raised as
                    # an uncertain outcome, which held the whole run for the person's inspection (needs_reconciliation);
                    # with nobody there to inspect it, a script waiting on a stale lock or looping ended the task.
                    # The kill is still reported, with what the command printed until then, as a result the model can
                    # read and act on (as the Companion's Shell reports its timed_out outcome, and as Codex returns
                    # exit 124 with the output captured so far: codex-rs/core/src/exec.rs). The person's own stop
                    # keeps raising above: nothing continues after it.
                    result.update(interrupted=True, timed_out=stopped == 'timeout', output_limit_reached=stopped == 'output',
                                  note=(STOPPED_NOTE.format(seconds=COMMAND_SECONDS) if stopped == 'timeout'
                                        else OUTPUT_NOTE.format(limit=OUTPUT_LIMIT) if stopped == 'output'
                                        else contained.note(stopped)))  # Phase 46 (1.9.28, F2)
                return result
            finally:
                watch.close()
                if process.returncode is None:
                    stop(process)
                    process.wait()
                if contained:
                    contained.close()


def _sweep(process):
    """SIGKILL the command's group; only ever called before the leader is reaped."""
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def json_quote(value):
    # SBPL string syntax, not shell interpolation.
    import json

    return json.dumps(value)
