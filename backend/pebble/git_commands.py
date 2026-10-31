"""Reviewed Git commands: the allowlist and parser for ProposeGitChange's `command` operation (Phase 46).

Measured on the golden set 2.0's held-out git_workflow family ("everyday git work in repositories with unrelated work in
progress and a remote", 5 cases, 3 repetitions): Pebble 1.9.20 passed 1 of 15 cells, Codex and Claude Code 15 of 15.
Pebble's terminal refuses raw Git, and the typed plans (stage, unstage, commit, create_branch, switch_branch, merge,
delete_branch, discard, push) have no revert, cherry-pick, tag, stash, amend, uncommit, rebase, restore, rename, fetch
or pull, so the runs ended "blocked".

A reviewed Git command is one porcelain command from this allowlist. `parse` turns the model's argv into a canonical
command or refuses it: every option must be one its subcommand lists (no -c, --exec, editors, interactive or patch
modes, pagers or external tools; a branch push, forced only with a lease or deleting, becomes the reviewed push
operation), short options are never combined, a value option takes the
next token or `--name=value`, and positionals are typed (a revision, a branch or tag name, a stash entry, a remote, a
literal relative path). The result says whether the command is destructive (it rewrites history, overwrites or deletes
files, or deletes a ref) or a publication (it changes a remote), which decides the person's review; git_plans runs it
with GitWorkflows._git's fixed overrides (no hooks, filters, pagers, helpers, editors or network) and keeps a
before-snapshot for Undo. Repository text stays data: nothing here reads the repository.

`read` admits the read-only Git a model types in the terminal (git status, log, show, diff, branch -a ...), which
GitWorkflows.raw runs as the read tools run; `review` (Phase 46, 1.9.29) admits a changing one that `parse` takes as
typed, which App runs as the `command` operation; anything else stays refused there."""

import json
import os
import re
import shlex
import unicodedata
from pathlib import Path, PurePosixPath

from .git_workflows import private, relative
from .policy import PolicyError

MAX_ARGV, MAX_TOKEN, MAX_TEXT, MAX_REVS = 40, 300, 4000, 16
CONTROL = ('--continue', '--abort', '--skip', '--quit')
REV = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./^~+-]{0,199}')        # a revision: sha, name, origin/main, HEAD~2, v1^2
RANGE = re.compile(r'([A-Za-z0-9_][A-Za-z0-9_./^~+-]{0,199})?\.\.\.?([A-Za-z0-9_][A-Za-z0-9_./^~+-]{0,199})?')
# Pseudo-refs name files Git rewrites on its own (FETCH_HEAD can list several commits a merge takes at once): never a
# reviewed revision (security review, 2026-10-27).
PSEUDO = re.compile(r'(?:[A-Z_]+_HEAD|AUTO_MERGE)(?:[~^].*)?')
# Phase 46 (1.9.28, git_workflow refusal map F3a): a reflog entry (HEAD@{2}, main@{1}, stash@{0}) or ORIG_HEAD names one
# commit, which runs only as the commit ID git_plans binds at review (`parse` refuses one that would run as a name, and
# the card shows both); a moved reflog is drift. FETCH_HEAD, MERGE_HEAD and the other pseudo-refs, dates
# (main@{1.hour.ago}), @{upstream} and @{-1} stay refused.
BOUND = re.compile(r'(?:ORIG_HEAD|(?:HEAD|[A-Za-z0-9][A-Za-z0-9._/-]{0,99})?@\{[0-9]{1,3}\})(?:[~^][0-9]{0,3})*')
DOT = ('"." (the whole project) is not a reviewed path for git {sub}: name the paths (ReadGitStatus lists them); '
       '["add", "-A"] stages every change, ["stash", "push", "-u"] sets every change aside and ["reset", "--hard"] '
       'discards the tracked ones.')
# Phase 46 (1.9.28, F3d): what an interactive rebase or a fixup commit is for, as reviewed commands (sequences run in
# order under one review, with one Undo).
REWRITE_RECIPES = ('No editor or todo list opens, so do it as reviewed commands: reword an older commit X of branch B '
                   'with [["switch", "--detach", "X"], ["commit", "--amend", "-m", "<new message>"], ["rebase", '
                   '"--autostash", "--onto", "HEAD", "X", "B"]]; drop it with ["rebase", "--autostash", "--onto", "X~1", '
                   '"X", "B"]; squash the last N commits with [["reset", "--soft", "HEAD~N"], ["commit", "-m", '
                   '"<message>"]]; fold staged changes into the last commit with ["commit", "--amend", "--no-edit"].')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}')            # a branch or tag name (git check-ref-format follows)
REMOTE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
STASH = re.compile(r'stash@\{([0-9]{1,3})\}|([0-9]{1,3})')
READ_ELSEWHERE = {'status', 'log', 'diff', 'show', 'blame', 'grep', 'ls-files', 'shortlog', 'describe', 'rev-parse',
                  'reflog', 'cat-file', 'ls-tree', 'whatchanged', 'show-ref', 'for-each-ref'}
# Phase 46 (git_workflow, everyday requests): config (the repository's own identity), clean (the dry run's exact list)
# and bisect (a reviewed sequence) left this list; see _config, _clean and _bisect. Phase 46 (1.9.28, D180): so did
# remote (add, set-url and rename, reviewed; its reads run raw, credentials hidden); see _remote_command.
NEVER = {'submodule', 'gc', 'prune', 'repack', 'filter-branch', 'filter-repo', 'replace',
         'update-ref', 'symbolic-ref', 'reflog', 'notes', 'am', 'apply', 'format-patch', 'send-email', 'archive',
         'bundle', 'clone', 'init', 'daemon', 'credential', 'difftool', 'mergetool', 'instaweb', 'svn', 'p4',
         'hook', 'maintenance', 'sparse-checkout', 'lfs', 'request-pull', 'fsck', 'pack-objects',
         'upload-pack', 'receive-pack', 'fast-import', 'fast-export', 'read-tree', 'update-index', 'commit-tree'}
CONFIG_KEYS = ('user.name', 'user.email')
MAX_IDENT = {'user.name': 200, 'user.email': 254}
# Phase 46 (the lead's design question, 2026-10-28): the read-only Git a model types in the terminal. Codex auto-approves
# git status/log/diff/show/branch and inspects their arguments since rust-v0.95.0 (GHSA-w5fx-fh39-j5rw: git show
# --output wrote .git/config); Claude Code runs "read-only forms of git" without a prompt. Pebble runs these in its
# confined Git (GitWorkflows.raw). On by default (D173, the person, 2026-10-28: as Codex and Claude Code allow it
# without asking); PEBBLE_RAW_GIT_READS=0 turns it off.
READS = {'status', 'log', 'show', 'diff', 'branch', 'tag', 'stash', 'rev-parse', 'blame', 'for-each-ref', 'show-ref',
         'describe', 'shortlog', 'ls-files', 'ls-tree', 'merge-base', 'rev-list', 'grep', 'reflog', 'cat-file',
         'name-rev', 'whatchanged', 'cherry', 'worktree',  # Phase 46 (1.9.28, F3e): worktree list only (_read_form)
         'remote'}  # Phase 46 (1.9.28, D180): remote, -v and get-url only (_read_form); GitWorkflows.raw hides credentials
# Phase 46 (1.9.29, golden held-out git_workflow: one task's three phrasings ended honestly on both models after a Git
# command typed in RunCommand was refused): what a model types around a Git command that changes nothing about it here.
# Pebble never pages or opens an editor (GitWorkflows.OVERRIDES, git_plans.COMMAND_ENV), so exactly these values are
# dropped (`GIT_EDITOR=true git rebase --continue`, `git -c core.editor=true ...`, `git -P log`), as is `-C` naming the
# project folder itself; any other variable, value, -c or global option keeps the terminal's refusal.
QUIET_ENV = {'GIT_EDITOR': ('true', ':'), 'GIT_SEQUENCE_EDITOR': ('true', ':'), 'GIT_MERGE_AUTOEDIT': ('no',),
             'GIT_PAGER': ('cat',), 'PAGER': ('cat',), 'GIT_TERMINAL_PROMPT': ('0',)}
QUIET_CONFIG = {'core.editor': ('true', ':'), 'sequence.editor': ('true', ':'), 'core.pager': ('cat',),
                'color.ui': ('never', 'false')}
CAT_TYPES = ('blob', 'tree', 'commit', 'tag')  # Phase 46 (1.9.29): `git cat-file <type> <object>` prints the object
# Phase 46 (1.9.28, D180): the addresses a reviewed remote add or set-url takes. Credentials never go in one (git-config's
# transfer.credentialsInUrl: "in favor of using git-credential(1)"); a remote helper (ext::, fd::) can run a program;
# http:// and git:// carry the work unencrypted
ADDRESS_SCHEMES = ('https', 'ssh', 'git+ssh', 'ssh+git', 'file')
CREDENTIAL_ADVICE = ('Keep credentials out of the address and let Git ask the system\'s credential helper, which keeps '
                     'them in the macOS Keychain (git config --global credential.helper osxkeychain; or sign in with '
                     'gh auth login or Git Credential Manager): give the address without them, e.g. '
                     '"https://github.com/owner/repo.git", or Git\'s user@host:owner/repo.git form for SSH.')
# Phase 46 (1.9.28, the follow-ups security review 7: a Cyrillic letter in "github.com" read the same on the card, and a
# right-to-left override was accepted): no address holds an invisible, format, control or separator character, and no
# host has letters outside ASCII. Refused, not shown with a warning: Git hosts are ASCII in practice (an
# internationalized one can still be given in its punycode, xn--, form, which the card flags), a warning is easy to miss
# and Full access shows no card, and Python's IDNA 2003 codec need not give the punycode curl would reach.
HIDDEN = frozenset({'Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp'})
PUNYCODE_NOTE = (' (warning: this host name is written in punycode, xn--, which stands for letters outside plain ASCII '
                 'and can look like another host\'s; check it is the host you mean before you push there)')
NON_ASCII_NOTE = (' (warning: this host name has letters outside plain ASCII, shown here as code points, which can '
                  'look like another host\'s; check it is the host you mean before you push there)')
# Phase 46 (1.9.29, D210; w16's open point: "ASCII look-alikes (rn/m, l/I) cannot be detected without a host list"): a
# host that reads as a well-known code host in plain letters, by the pairs in LOOKS_ALIKE (github.corn, gitIab.com,
# bitbucket.0rg), is refused as a non-ASCII look-alike is, and flagged on an existing remote's card. Only look-alikes of
# these hosts; every other host, and these hosts and the ones below them, are as before.
KNOWN_HOSTS = ('github.com', 'gitlab.com', 'bitbucket.org', 'codeberg.org', 'sourceforge.net', 'gitea.com', 'sr.ht',
               'huggingface.co', 'dev.azure.com', 'visualstudio.com')
LOOKS_ALIKE = (('rn', 'm'), ('vv', 'w'), ('cl', 'd'), ('0', 'o'), ('1', 'l'), ('i', 'l'))  # read alike in most fonts
LOOKALIKE_NOTE = (' (warning: this host name reads like {known} but is not it; check it is the host you mean before you '
                  'push there)')


def _skeleton(host):
    for seen, read in LOOKS_ALIKE:
        host = host.replace(seen, read)
    return host


def lookalike(host):
    """The well-known code host (KNOWN_HOSTS) that `host` imitates in plain ASCII, or None (a host that is one of them,
    or below one, imitates nothing)."""
    host = (host or '').lower().rstrip('.')
    if not host.isascii() or any(host == known or host.endswith('.' + known) for known in KNOWN_HOSTS):
        return None
    shape = _skeleton(host)
    return next((known for known in KNOWN_HOSTS
                 if shape == _skeleton(known) or shape.endswith('.' + _skeleton(known))), None)
# Options that write a file, run a program, read a file or reach another repository; the confined runner refuses writes
# and programs anyway.
READ_DENIED = ('--output', '--ext-diff', '--textconv', '--exec', '--open-files-in-pager', '-O', '--paginate',
               '--no-index', '--config-env', '--upload-pack', '--receive-pack', '--git-dir', '--work-tree', '--namespace',
               '--super-prefix', '--filters', '--batch', '--contents', '--ignore-revs-file', '--untracked',
               '--follow-symlinks')
LISTS = {'branch': ({'-a', '--all', '-r', '--remotes', '-v', '-vv', '--verbose', '-l', '--list', '--show-current',
                     '--no-column', '--no-abbrev', '-i', '--ignore-case', '--omit-empty', '--no-color'},
                    ('--contains', '--no-contains', '--merged', '--no-merged', '--points-at', '--sort', '--format',
                     '--abbrev', '--column', '--color'), set('arvli')),
         'tag': ({'-l', '--list', '-i', '--ignore-case', '--no-column', '--omit-empty', '--no-color'},
                 ('--contains', '--no-contains', '--merged', '--no-merged', '--points-at', '--sort', '--format',
                  '--column', '--color'), set('li'))}
PATCH = re.compile(r'-p|-u|--patch|--patch-with-stat|--patch-with-raw|--cc|-c|-m|--binary|--full-index|--remerge-diff'
                   r'|--word-diff.*|--color-words.*|-U\d*|--unified.*|-L.*|--diff-merges.*')


class Command:
    """A parsed, canonical command. `argv` starts with the subcommand; `revisions` are the revision arguments the plan
    resolves and binds; `refs` the branch or tag names it creates, moves or deletes; `paths` the literal paths;
    `remote` the remote it reads or changes; `destructive`, `publication` and `rewrites` classify it; `midway` marks a
    command that may run while a merge, rebase, revert or cherry-pick is stopped (to resolve and finish it)."""

    def __init__(self, sub, argv, *, revisions=(), refs=(), paths=(), remote=None, destructive=False,
                 publication=False, rewrites=False, midway=False, worktree=True, summary='', folder=None,
                 slots=None, names=()):
        self.sub, self.argv, self.folder, self.names = sub, list(argv), folder, list(names)
        # argv positions git_plans runs with the commit ID it bound at review (a name that moves, or a range, cannot
        # change what runs); `folder` is at the argv position folder_at
        self.slots = sorted(slots if slots is not None else
                            {i for i, item in enumerate(self.argv) if i and item in revisions and '..' not in item})
        self.folder_at = None
        self.push = None  # Phase 46: a push's force, lease, delete and upstream requests (_push), for git_plans
        self.side = None  # Phase 46 (1.9.28): --ours or --theirs of a stopped conflict (_side), for git_plans
        self.revisions, self.refs, self.paths = list(revisions), list(refs), list(paths)
        self.remote, self.destructive, self.publication = remote, destructive, publication
        self.rewrites, self.midway, self.worktree, self.summary = rewrites, midway, worktree, summary

    def data(self):
        return {'subcommand': self.sub, 'argv': self.argv, 'revisions': self.revisions, 'refs': self.refs,
                'paths': self.paths, 'remote': self.remote, 'destructive': self.destructive,
                'publication': self.publication, 'rewrites': self.rewrites, 'midway': self.midway}


def argv_arg(value):
    """The tool's `argv`: a JSON array of strings (or a list), the subcommand first, without the leading `git`."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise PolicyError('argv must be a JSON array of strings: the Git subcommand and its arguments, for '
                              'example ["revert", "--no-edit", "a1b2c3d"].') from None
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ARGV:
        raise PolicyError(f'argv must list the Git subcommand and at most {MAX_ARGV - 1} arguments.')
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_TEXT \
                or any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in item):
            raise PolicyError('Each argv item must be a non-empty string without NUL or other control characters.')
    if value[0] == 'git':
        value = value[1:]
        if not value:
            raise PolicyError('Name the Git subcommand after "git".')
    return value


def located(value):
    """Phase 46 (1.9.29): ProposeGitChange's argv with a leading "-C", "<folder>" (git -C, as typed in a terminal) taken
    off: (folder or None, argv as JSON). Every command of a sequence names the same folder, or none does; git_plans runs
    it in that folder only when it is the project or one of its own linked worktrees (GitWorkflows.worktree_view)."""
    try:
        items = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return None, value  # parse says what argv must be
    sequence = isinstance(items, list) and bool(items) and all(isinstance(item, list) for item in items)
    commands = [list(item) for item in (items if sequence else [items])] if isinstance(items, list) else []
    folders = []
    for argv in commands:
        at = 1 if argv[:1] == ['git'] else 0
        folders.append(argv[at + 1] if argv[at:at + 1] == ['-C'] and len(argv) > at + 2 else None)
        if folders[-1] is not None:
            del argv[at:at + 2]
    if not any(folders):
        return None, value
    if len(set(folders)) != 1 or not isinstance(folders[0], str):
        raise PolicyError('Give every command of the sequence the same "-C", "<folder>", or leave -C out of all of them.')
    return folders[0], json.dumps(commands if sequence else commands[0])


def bound(value):
    """Phase 46 (1.9.28): whether a revision is a reflog entry or ORIG_HEAD, bound to its commit ID at review; never a
    pseudo-ref's reflog (FETCH_HEAD@{0}, MERGE_HEAD@{0}: the w9 security review's note) or ORIG_HEAD@{N}."""
    return (isinstance(value, str) and BOUND.fullmatch(value) is not None and '..' not in value and '//' not in value
            and '.@' not in value and '/@' not in value
            and not (PSEUDO.fullmatch(value.split('@{', 1)[0]) and '@{' in value))


def bound_parts(token):
    """Phase 46 (1.9.28, the w9 security review F6): the reflog entries and ORIG_HEADs a revision or a range names."""
    match = RANGE.fullmatch(token) if isinstance(token, str) and '..' in token else None
    parts = [part for part in match.groups() if part] if match else [token]
    return [part for part in parts if bound(part)]


def _rev(value, label='revision'):
    if bound(value):
        return value  # Phase 46 (1.9.28): bound to its commit ID at review (parse, git_plans)
    if (not REV.fullmatch(value) or '..' in value or value.endswith(('.', '/', '.lock')) or '//' in value
            or PSEUDO.fullmatch(value)):
        raise PolicyError(f'Use a plain {label}: a commit ID, a branch or tag name, or HEAD~N (got {_shown(value)}); a '
                          'reflog entry such as HEAD@{1} and ORIG_HEAD work too, FETCH_HEAD, dates and @{-1} do not.')
    return value


def _range(value):
    match = RANGE.fullmatch(value)
    if match and (match.group(1) or match.group(2)):
        for part in match.groups():
            if part:
                _rev(part)
        return value
    return _rev(value)


def _name(value, label):
    if (not NAME.fullmatch(value) or '..' in value or '//' in value or '@{' in value or value.endswith(('/', '.'))
            or value == 'HEAD' or any(p.startswith('.') or p.endswith('.lock') for p in value.split('/'))):
        raise PolicyError(f'Use a plain {label} name: letters, digits, ".", "_", "-" and "/" (got {_shown(value)}).')
    return value


def _remote(value):
    if not REMOTE.fullmatch(value):
        raise PolicyError(f'Use a configured remote name such as origin (got {_shown(value)}).')
    return value


def _entry(value):
    match = STASH.fullmatch(value)
    if not match:
        raise PolicyError(f'Name a stash entry as stash@{{N}} (got {_shown(value)}).')
    return 'stash@{%s}' % (match.group(1) or match.group(2))


def _text(value):
    if not value.strip() or len(value) > MAX_TEXT:
        raise PolicyError('A message must be 1 to 4000 characters.')
    return value


def _int(value):
    if not re.fullmatch(r'[1-9]', value):
        raise PolicyError('Use a parent number from 1 to 9.')
    return value


def _shown(value):
    return json.dumps(value[:80])


def _strategy(value):
    if value not in ('ours', 'theirs'):
        raise PolicyError('A merge strategy option is "ours" or "theirs" (which side wins a conflicting hunk).')
    return value


KINDS = {'text': _text, 'int': _int, 'rev': _rev, 'name': lambda v: _name(v, 'branch or tag'), 'remote': _remote,
         'strategy': _strategy}


def _path(sub, value, untrack=False):
    """A literal project path; Phase 46 (1.9.28, F3d): "." says what works instead of reading as a scope refusal."""
    if isinstance(value, str) and value.strip() and PurePosixPath(value).as_posix() == '.':
        raise PolicyError(DOT.format(sub=sub))
    return relative(value, untrack)


def _tokens(sub, args, flags, values, paths=False, untrack=False):
    """Options (canonical order kept), positionals and `--` paths. `flags`: option -> canonical name; `values`:
    option -> (canonical name, kind); `untrack` (rm --cached) admits private paths for git_plans' untrack plan."""
    found, positionals, listed, seen = [], [], [], set()
    items = list(args)
    while items:
        token = items.pop(0)
        if token == '--':
            if not paths:
                raise PolicyError(f'git {sub} takes no paths here.')
            if not items:
                raise PolicyError('List the paths after "--".')
            listed = [_path(sub, item, untrack) for item in items]
            break
        name, value = token, None
        if token.startswith('--') and '=' in token:
            name, value = token.split('=', 1)
        if re.fullmatch(r'-[A-Za-z]{2,8}', token) and token not in flags and token not in values:
            # Phase 46: "-am" is "-a -m" when every letter is one of this subcommand's own options (a value option
            # last); anything else is refused below as before
            letters = ['-' + letter for letter in token[1:]]
            if all(item in flags for item in letters[:-1]) and (letters[-1] in flags or letters[-1] in values):
                items[:0] = letters
                continue
        if name in flags and value is None:
            canon = flags[name]
            if canon in seen:
                raise PolicyError(f'Give {name} once.')
            seen.add(canon)
            found.append([canon])
        elif name in values:
            canon, kind = values[name]
            if canon in seen and not (canon == '-m' and kind == 'text'):
                raise PolicyError(f'Give {name} once.')
            if value is None:
                if not items:
                    raise PolicyError(f'{name} needs a value.')
                value = items.pop(0)
            if canon in seen:  # Phase 46: -m "subject" -m "body" is one message of paragraphs, as git makes it
                earlier = next(item for item in found if item[0] == canon)
                earlier[1] = KINDS[kind](earlier[1] + '\n\n' + value)
                continue
            seen.add(canon)
            found.append([canon, KINDS[kind](value)])
        elif token.startswith('-') and token != '-':
            raise PolicyError(f'git {sub} option {_shown(token)} is not available to reviewed Git commands. '
                              + (f'Allowed: {", ".join(sorted(set(flags) | set(values)))}.' if flags or values else ''))
        else:
            positionals.append(token)
    return found, positionals, listed, seen


def _canon(sub, found, positionals, listed):
    argv = [sub]
    for item in found:
        argv += item
    argv += positionals
    if listed:
        argv += ['--', *listed]
    return argv


def _control(sub, args, allowed=CONTROL):
    if len(args) == 1 and args[0] in allowed:
        return Command(sub, [sub, args[0]], midway=True, destructive=args[0] in ('--abort', '--skip', '--quit'),
                       summary=f'git {sub} {args[0]}')
    if any(token in CONTROL for token in args):
        raise PolicyError(f'Give {"/".join(allowed)} on its own: ["{sub}", "--continue"].')
    return None


def parse(value):
    """A canonical Command from the tool's argv, or PolicyError naming what is not allowed and what to use instead."""
    argv = argv_arg(value)
    sub, args = argv[0], argv[1:]
    if sub in READ_ELSEWHERE:
        raise PolicyError(f'git {sub} reads; use ReadGitStatus (changed files, branches, tags, origin/... branches and '
                          'stash entries), ReadGitDiff or ReadGitHistory (from a branch, tag, origin/main, HEAD~N or '
                          'stash@{N}) instead (no review needed).'  # Phase 46: they show the refs now
                          # Phase 46 (1.9.28, F3d): the reflog is shown by the terminal's raw read only
                          + (f' RunCommand also runs "git {sub} ..." as typed, read-only.'
                             if os.environ.get('PEBBLE_RAW_GIT_READS', '1') != '0' else ''))
    handler = PARSERS.get(sub)
    if handler is None:
        if sub in NEVER:
            raise PolicyError(f'git {sub} is outside reviewed Git commands (it can change configuration, remotes, '
                              'hooks, shared objects or files Pebble cannot restore). Do it outside Pebble.')
        raise PolicyError(f'git {_shown(sub)} is not an allowed reviewed Git command. Allowed: '
                          + ', '.join(sorted(PARSERS)) + '.')
    command = handler(sub, args)
    # Phase 46 (1.9.28, F3a; the security review of 2026-10-27): a reflog entry or ORIG_HEAD runs only as the commit ID
    # bound at review, never as a name Git resolves again when the command runs. A message (after -m) and a path (after
    # "--") are text, not revisions (the w9 security review's note).
    paths_from = command.argv.index('--') if '--' in command.argv else len(command.argv)
    unbound = [item for at, item in enumerate(command.argv) if at not in command.slots and BOUND.fullmatch(item)
               and at < paths_from and command.argv[at - 1] != '-m'
               and not (sub == 'stash' and STASH.fullmatch(item))]  # a stash entry: git_plans binds the stash list
    if unbound:
        raise PolicyError(f'{_shown(unbound[0])} can only name the commit a command works on; give that commit\'s ID '
                          'here (RunCommand "git rev-parse ..." shows it).')
    return command


def review_switch():
    return os.environ.get('PEBBLE_1929_RAW_GIT_REVIEW', '1') != '0'  # Phase 46 (1.9.29): on by default


def typed(command, root=None, elsewhere=False):
    """Phase 46 (1.9.29): the Git arguments (subcommand first) of one git command typed in the terminal, after the words
    that change nothing here (--no-pager or -P, `-C` naming the project folder `root`, QUIET_ENV and QUIET_CONFIG at
    exactly those values); None for anything else (no git, a pipe or chain, a substitution, another variable, value or
    global option), which the terminal refuses as before. With `elsewhere`, one `-C <folder>` naming another folder, or
    a leading `cd <folder> &&`, stays in front as ["-C", folder, ...]: GitWorkflows.worktree_view runs it only in one of
    the project's own linked worktrees and refuses any other folder with the rule."""
    if not isinstance(command, str) or len(command) > 4000 or any(c in command for c in '\n\r\0') \
            or re.search(r'\$\(|`|\$\{', command):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>')
        lexer.whitespace_split, lexer.commenters = True, ''
        argv = list(lexer)
    except ValueError:
        return None
    folder, elsewhere = None, elsewhere and root is not None  # a worktree is checked against the project
    if elsewhere and len(argv) > 3 and argv[0] == 'cd' and argv[2] == '&&':
        folder, argv = argv[1], argv[3:]  # Phase 46 (1.9.29): "cd <folder> && git ...", as git -C <folder> ...
    if any(re.fullmatch(r'[;&|<>]+', a) for a in argv):
        return None
    while argv and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', argv[0], re.S):
        name, _, value = argv[0].partition('=')
        if value not in QUIET_ENV.get(name, ()):
            return None
        argv = argv[1:]
    if len(argv) < 2 or Path(argv[0]).name.casefold() != 'git':
        return None
    rest = argv[1:]
    while rest and rest[0].startswith('-'):
        if rest[0] in ('--no-pager', '-P'):  # Pebble never pages
            rest = rest[1:]
        elif rest[0] == '-C' and len(rest) > 1 and root is not None and folder is None and _project(rest[1], root):
            rest = rest[2:]  # (after a cd or another -C, Git would read a relative -C from there: refused below)
        elif rest[0] == '-C' and len(rest) > 1 and elsewhere and folder is None and rest[1] and not rest[1].startswith('-'):
            folder, rest = rest[1], rest[2:]
        elif rest[0] == '-c' and len(rest) > 1 and rest[1].partition('=')[2] in QUIET_CONFIG.get(
                rest[1].partition('=')[0].lower(), ()):
            rest = rest[2:]
        else:
            return None  # -C elsewhere, -c, --git-dir and the other global options stay refused
    if folder is not None and rest and not _project(folder, root):
        return ['-C', folder, *rest]
    return rest or None


def _project(folder, root):
    """Whether `git -C folder` names the project folder itself (".", its own path, or a spelling of it)."""
    try:
        return os.path.realpath(os.path.join(str(root), folder)) == os.path.realpath(str(root))
    except (TypeError, ValueError, OSError):
        return False


def review(command, root=None):
    """Phase 46 (1.9.29): the argv (without "git") of a changing Git command typed in the terminal that `parse` takes as
    typed, which App runs as ProposeGitChange's "command" operation (the same plan, review, snapshots and Undo); None when
    it is no single git command or the switch is off (the terminal's refusal stands, and a read is `read`'s). PolicyError
    with parse's reason for a git command parse refuses, which App adds to the terminal's refusal."""
    argv = typed(command, root, elsewhere=True) if review_switch() else None
    rest = argv[2:] if argv and argv[0] == '-C' else argv  # Phase 46 (1.9.29): in a linked worktree (git_plans)
    if not rest or rest[0].startswith('-') or '--help' in rest:
        return None
    parse(rest)
    return argv


def read(command, root=None):
    """Phase 46: the argv (without "git") of a read-only Git command a model typed in the terminal, which
    GitWorkflows.raw runs; None for anything else (no single git command, a global option, a subcommand or form that
    changes something), which terminal.inspect_command refuses as before. PolicyError for a read with an option or a
    path Pebble does not run. Phase 46 (1.9.29): the words `typed` drops are dropped here too."""
    rest = typed(command, root, elsewhere=review_switch()) if os.environ.get('PEBBLE_RAW_GIT_READS', '1') != '0' else None
    where = rest[:2] if rest and rest[0] == '-C' else []  # Phase 46 (1.9.29): GitWorkflows.raw checks the worktree
    rest = rest[len(where):] if rest else rest
    if not rest or rest[0].startswith('-') or rest[0] not in READS or not _read_form(rest[0], rest[1:]):
        return None  # -C, -c, --git-dir and the other global options stay refused, as do writes
    if '--help' in rest:
        return None  # Phase 46 (the lead's review, F3): Git's help opens a viewer; left to the terminal's refusal
    sub, args = rest[0], rest[1:]
    for token in args:
        if token.startswith(READ_DENIED) or (sub == 'grep' and token in ('-f', '--file')):
            raise PolicyError(f'git {sub} {_shown(token)} can write a file, run a program or read outside the repository, '
                              'so Pebble does not run it; leave the option out.')
        if token.startswith(('/', '~')) or token == '..' or token.startswith('../'):
            raise PolicyError('Git paths must stay in the selected project.')
        # Phase 46 (the lead's review, F2): the path after a colon (HEAD:../x, :/etc/x) and a path through ".." stay in
        # the project too; Git refused them and the sandbox reads only the repository, so this is a third layer
        _, colon, after = token.partition(':')
        paths = [after] if colon else ([] if token.startswith('-') else [token])
        if any(path.startswith(('/', '~')) or '..' in PurePosixPath(path).parts for path in paths if path):
            raise PolicyError('Git paths must stay in the selected project.')
        if any(private(part) for part in token.split(':') if part and not part.startswith('-')):
            raise PolicyError('Private paths (.env, keys, .git and the like) stay out of raw Git; ReadGitDiff and '
                              'ReadGitHistory leave them out.')
    return [*where, sub, *args]


def _read_form(sub, args):
    """Whether `git sub args` only reads: branch and tag list (a bare name would create one), stash list or show, a
    reflog that is shown and cat-file's type, size or existence checks."""
    if sub in LISTS:
        flags, values, letters = LISTS[sub]
        listing, items = False, list(args)
        while items:
            token = items.pop(0)
            if token in ('-l', '--list'):
                listing = True
            if token in flags:
                continue
            if token.split('=', 1)[0] in values:
                if '=' not in token and items and not items[0].startswith('-'):
                    items.pop(0)  # --contains <commit>, --sort <key> ...
                continue
            if sub == 'tag' and re.fullmatch(r'-n[0-9]*', token):
                continue
            if re.fullmatch(r'-[A-Za-z]{2,8}', token) and set(token[1:]) <= letters:
                listing = listing or 'l' in token
                continue
            if token.startswith('-') or not listing:
                return False  # -d, -m, -f, -u ... or a name that creates a branch or tag
        return True
    if sub == 'stash':
        return bool(args) and args[0] in ('list', 'show')
    if sub == 'reflog':
        return not args or args[0] not in ('expire', 'delete', 'drop')
    if sub == 'cat-file':
        # Phase 46 (1.9.29): also an object's contents (-p, or its type), as git show prints them; `content` puts them
        # behind GitWorkflows.raw's private-history check
        return bool(args) and (args[0] in ('-t', '-s', '-e') or len(args) == 2 and args[0] in ('-p', *CAT_TYPES))
    if sub == 'worktree':  # Phase 46 (1.9.28, F3e): the linked worktrees, as git lists them
        return args[:1] == ['list'] and all(token in ('--porcelain', '-v', '--verbose', '-z') for token in args[1:])
    if sub == 'remote':  # Phase 46 (1.9.28, D180): the names, -v, and get-url [--push] [--all] <name>
        if args[:1] == ['get-url']:
            rest = [token for token in args[1:] if token not in ('--push', '--all')]
            return len(rest) == 1 and REMOTE.fullmatch(rest[0]) is not None
        return args in ([], ['-v'], ['--verbose'])
    return True


def content(argv):
    """Whether a read admitted by `read` can print file contents (GitWorkflows.raw then checks for private files)."""
    sub, args = argv[0], argv[1:]
    if sub in ('show', 'diff', 'grep', 'blame') or sub == 'cat-file' and args[:1] not in (['-t'], ['-s'], ['-e']):
        return True  # Phase 46 (1.9.29): cat-file -p and cat-file <type> print an object's contents
    if sub in ('log', 'whatchanged'):
        return any(PATCH.fullmatch(token) for token in args)
    return sub == 'stash' and args[:1] == ['show'] and any(PATCH.fullmatch(token) for token in args[1:])


# -- subcommands ----------------------------------------------------------------------------------------------------

def _revert(sub, args):
    control = _control(sub, args)
    if control:
        return control
    flags = {'--no-commit': '--no-commit', '-n': '--no-commit', '--no-edit': '--no-edit', '-x': '-x'}
    if sub == 'cherry-pick':
        flags.update({'--allow-empty': '--allow-empty', '--keep-redundant-commits': '--keep-redundant-commits',
                      '--ff': '--ff'})
    else:
        flags.pop('-x')
    # Phase 46 (1.9.28, F3b): which side wins a conflicting hunk, as merge takes it; a revert range A..B is expanded to
    # its commits at review, newest first as Git reverts a range, as cherry-pick ranges are (git_plans)
    found, positionals, _, seen = _tokens(sub, args, flags, {'-m': ('-m', 'int'), '--mainline': ('-m', 'int'),
                                                             '-X': ('-X', 'strategy'), '--strategy-option': ('-X', 'strategy')})
    if not 1 <= len(positionals) <= MAX_REVS:
        raise PolicyError(f'Name 1 to {MAX_REVS} commits for git {sub}.')
    revisions = [_range(p) for p in positionals]
    if sub == 'revert' and '--no-commit' not in seen and '--no-edit' not in seen:
        found.append(['--no-edit'])  # never an editor: Git's default revert message
    verb = 'Revert' if sub == 'revert' else 'Apply'
    argv = _canon(sub, found, revisions, [])
    first = len(argv) - len(revisions)
    return Command(sub, argv, revisions=[r for r in revisions if '..' not in r],
                   slots={first + i for i, r in enumerate(revisions) if '..' not in r},
                   summary=f'{verb} {", ".join(revisions)} as new commit(s) on the current branch'
                           + (' (changes left uncommitted)' if '--no-commit' in seen else ''))


def _tag(sub, args):
    found, positionals, _, seen = _tokens(sub, args, {'-a': '-a', '--annotate': '-a', '-d': '-d', '--delete': '-d',
                                                      '-f': '-f', '--force': '-f'},
                                          {'-m': ('-m', 'text'), '--message': ('-m', 'text')})
    if '-d' in seen:
        if seen - {'-d'} or not 1 <= len(positionals) <= 8:
            raise PolicyError('git tag -d takes only tag names (1 to 8).')
        names = [_name(p, 'tag') for p in positionals]
        return Command(sub, ['tag', '-d', *names], refs=[f'refs/tags/{n}' for n in names], destructive=True,
                       worktree=False, summary='Delete tag(s) ' + ', '.join(names))
    if not 1 <= len(positionals) <= 2:
        raise PolicyError('git tag takes a tag name and optionally the commit to tag.')
    name = _name(positionals[0], 'tag')
    target = [_rev(positionals[1])] if len(positionals) == 2 else []
    if '-a' in seen and '-m' not in seen:
        raise PolicyError('An annotated tag needs its message: ["tag", "-a", "v1.2.0", "-m", "Release 1.2.0"].')
    if '-m' in seen and '-a' not in seen:
        found.insert(0, ['-a'])
    return Command(sub, _canon(sub, found, [name, *target], []), revisions=target or ['HEAD'],
                   refs=[f'refs/tags/{name}'], destructive='-f' in seen, worktree=False,
                   summary=f'{"Move" if "-f" in seen else "Create"} tag {name} at {target[0] if target else "HEAD"}')


def _stash_command(sub, args):
    action = args[0] if args and not args[0].startswith('-') else 'push'
    rest = args[1:] if args and not args[0].startswith('-') else args
    if action == 'save':
        # Phase 46 (1.9.29): Git's older spelling of push -m, which many still type; git-stash(1): "all non-option
        # arguments are concatenated to form the stash message", and it takes no paths
        found, positionals, _, _ = _tokens('stash save', rest, {'-u': '-u', '--include-untracked': '-u', '-k': '-k',
                                                                '--keep-index': '-k', '-S': '-S', '--staged': '-S'}, {})
        return _stash_command(sub, ['push', *[t for item in found for t in item],
                                    *(['-m', ' '.join(positionals)] if positionals else [])])
    if action == 'push':
        found, positionals, listed, seen = _tokens(
            'stash push', rest, {'-u': '--include-untracked', '--include-untracked': '--include-untracked',
                                 '-k': '--keep-index', '--keep-index': '--keep-index', '-S': '--staged',
                                 '--staged': '--staged'},
            {'-m': ('-m', 'text'), '--message': ('-m', 'text')}, paths=True)
        if positionals:
            raise PolicyError('List the paths to stash after "--": ["stash", "push", "-m", "WIP", "--", "a.py"].')
        return Command(sub, ['stash', 'push', *[t for item in found for t in item], *(['--', *listed] if listed else [])],
                       paths=listed, refs=['refs/stash'],
                       summary='Set aside ' + ('the changes in ' + ', '.join(listed) if listed else 'the uncommitted changes')
                               + ' in a new stash entry')
    if action in ('pop', 'apply', 'drop'):
        found, positionals, _, seen = _tokens(f'stash {action}', rest, {'--index': '--index'} if action != 'drop' else {},
                                              {})
        if len(positionals) > 1:
            raise PolicyError(f'git stash {action} takes at most one stash entry.')
        entry = [_entry(positionals[0])] if positionals else []
        what = entry[0] if entry else 'the latest stash entry'
        return Command(sub, ['stash', action, *[t for item in found for t in item], *entry], refs=['refs/stash'],
                       destructive=action == 'drop', worktree=action != 'drop',
                       summary={'pop': f'Restore {what} into the working tree and remove it from the stash',
                                'apply': f'Restore {what} into the working tree and keep it in the stash',
                                'drop': f'Delete {what}'}[action])
    if action == 'branch':
        if not 1 <= len(rest) <= 2 or any(t.startswith('-') for t in rest):
            raise PolicyError('git stash branch takes a new branch name and optionally a stash entry.')
        name = _name(rest[0], 'branch')
        entry = [_entry(rest[1])] if len(rest) == 2 else []
        return Command(sub, ['stash', 'branch', name, *entry], refs=[f'refs/heads/{name}', 'refs/stash'],
                       summary=f'Create branch {name} from a stash entry and restore it there')
    if action in ('list', 'show'):
        raise PolicyError(f'git stash {action} reads; ReadGitStatus lists the stash entries (refs.stashes) and '
                          'ReadGitDiff or ReadGitHistory with commit "stash@{N}" shows one.')
    raise PolicyError('Use git stash push (or save), pop, apply, drop or branch (stash clear is not available: it deletes '
                      'every entry; drop them one at a time with ["stash", "drop", "stash@{N}"]).')


def _commit(sub, args):
    if any(token.split('=', 1)[0] in ('--fixup', '--squash') for token in args):
        raise PolicyError(f'git commit --fixup and --squash need an interactive rebase. {REWRITE_RECIPES}')
    found, positionals, listed, seen = _tokens(
        sub, args, {'--amend': '--amend', '--no-edit': '--no-edit', '--allow-empty': '--allow-empty', '-a': '--all',
                    '--all': '--all', '--reset-author': '--reset-author'},
        {'-m': ('-m', 'text'), '--message': ('-m', 'text')}, paths=True)
    if positionals:
        raise PolicyError('List paths for git commit after "--".')
    if '-m' not in seen and '--no-edit' not in seen:
        raise PolicyError('Give the commit message with -m (or --no-edit with --amend); no editor opens.')
    # Phase 46 (1.9.28, F3c): without --amend, --no-edit takes the message Git prepared for a stopped merge, cherry-pick
    # or revert; git_plans refuses it when none is stopped
    amend = '--amend' in seen
    return Command(sub, _canon(sub, found, [], listed), paths=listed, revisions=['HEAD'] if amend else [],
                   destructive=amend, rewrites=amend, midway=not amend, worktree=False,
                   summary=('Replace the last commit' if amend else 'Commit the staged changes')
                           + (' and the listed paths' if listed else '') + (' with every tracked change' if '--all' in seen else ''))


def _reset(sub, args):
    modes = {'--soft': '--soft', '--mixed': '--mixed', '--hard': '--hard', '--keep': '--keep', '--merge': '--merge'}
    found, positionals, listed, seen = _tokens(sub, args, modes, {}, paths=True)
    mode = [m for m in modes.values() if m in seen]
    if len(mode) > 1:
        raise PolicyError('Choose one of --soft, --mixed, --hard, --keep and --merge.')
    if any(PurePosixPath(item).as_posix() == '.' for item in positionals):
        raise PolicyError(DOT.format(sub=sub))  # Phase 46 (1.9.28, F3d): what works instead
    if listed or (positionals and not mode and len(positionals) > 1):
        if mode:
            raise PolicyError('git reset with paths only unstages them; drop the mode option.')
        target = [_rev(positionals[0])] if positionals else []
        names = listed or [relative(p) for p in positionals[1:]]
        source = target[0] if target and target[0] != 'HEAD' else None
        return Command(sub, _canon(sub, [], target, names), paths=names, revisions=target, midway=True, worktree=False,
                       summary=(f'Stage the version of {", ".join(names)} in {source} (the working files stay as they are)'
                                if source else 'Unstage ' + ', '.join(names)))
    if len(positionals) > 1:
        raise PolicyError('git reset takes one commit.')
    target = _rev(positionals[0]) if positionals else 'HEAD'
    mode = mode[0] if mode else '--mixed'
    moves = target != 'HEAD'
    return Command(sub, [sub, mode, target], revisions=[target], refs=['HEAD'] if moves else [],
                   destructive=mode in ('--hard', '--keep', '--merge') or moves, rewrites=moves,
                   worktree=mode in ('--hard', '--keep', '--merge'),
                   summary={'--soft': f'Move the current branch to {target}, keeping every change staged',
                            '--mixed': f'Move the current branch to {target}, keeping every change unstaged',
                            '--hard': f'Move the current branch to {target} and discard every uncommitted change',
                            '--keep': f'Move the current branch to {target}, keeping uncommitted changes where possible',
                            '--merge': f'Reset to {target}, keeping unstaged changes'}[mode])


def _rebase(sub, args):
    control = _control(sub, args)
    if control:
        return control
    if any(token in ('-i', '--interactive', '--autosquash', '--edit-todo') for token in args):
        raise PolicyError(f'git rebase {"-i" if "-i" in args else "--interactive"} opens a todo list in an editor. '
                          + REWRITE_RECIPES)
    found, positionals, _, seen = _tokens(sub, args, {'--autostash': '--autostash', '--no-autostash': '--no-autostash',
                                                      '--keep-empty': '--keep-empty', '--root': '--root',
                                                      '--no-fork-point': '--no-fork-point'},
                                          {'--onto': ('--onto', 'rev'),
                                           # Phase 46 (1.9.28, F3b): which side wins a conflicting hunk, as merge takes it
                                           '-X': ('-X', 'strategy'), '--strategy-option': ('-X', 'strategy')})
    if not (1 <= len(positionals) <= 2 or ('--root' in seen and len(positionals) <= 1)):
        raise PolicyError('git rebase takes the upstream commit or branch and optionally the branch to rebase.')
    revs = [_rev(p) for p in positionals]
    if len(revs) == 2:
        revs[1] = _name(revs[1], 'branch')  # the branch to rebase: switched to by name
    onto = next((item[1] for item in found if item[0] == '--onto'), None)
    argv = _canon(sub, found, revs, [])
    slots = {argv.index('--onto') + 1} if onto else set()
    if revs:
        slots.add(len(argv) - len(revs))
    return Command(sub, argv, revisions=revs[:1] + ([onto] if onto else []), refs=['HEAD'], slots=slots,
                   destructive=True, rewrites=True,
                   summary=f'Replay the current branch\'s commits onto {onto or (revs[0] if revs else "its root")}')


def _merge(sub, args):
    control = _control(sub, args, ('--continue', '--abort', '--quit'))
    if control:
        return control
    found, positionals, _, seen = _tokens(sub, args, {'--no-ff': '--no-ff', '--ff-only': '--ff-only', '--ff': '--ff',
                                                      '--squash': '--squash', '--no-edit': '--no-edit',
                                                      '--no-commit': '--no-commit',
                                                      '--allow-unrelated-histories': '--allow-unrelated-histories',
                                                      # Phase 46 (1.9.29): uncommitted work set aside and put back, as
                                                      # rebase has it (git-merge(1), Git 2.27)
                                                      '--autostash': '--autostash'},
                                          {'-m': ('-m', 'text'), '--message': ('-m', 'text'),
                                           # Phase 46: "keep theirs where they conflict", the everyday conflict choice
                                           '-X': ('-X', 'strategy'), '--strategy-option': ('-X', 'strategy')})
    if not 1 <= len(positionals) <= 4:
        raise PolicyError('git merge takes 1 to 4 branches or commits.')
    revs = [_rev(p) for p in positionals]
    if '-m' not in seen and '--no-edit' not in seen:
        found.append(['--no-edit'])
    return Command(sub, _canon(sub, found, revs, []), revisions=revs, refs=['HEAD'],
                   summary=f'Merge {", ".join(revs)} into the current branch')


def _switch(sub, args):
    found, positionals, _, seen = _tokens(sub, args, {'--detach': '--detach', '-d': '--detach',
                                                      '--no-track': '--no-track'},
                                          {'-c': ('-c', 'name'), '--create': ('-c', 'name'),
                                           '-C': ('-C', 'name'), '--force-create': ('-C', 'name')})
    if len(positionals) > 1 or (not positionals and not ({'-c', '-C'} & seen)):
        raise PolicyError('git switch takes one branch (or commit with --detach), or -c NAME [START].')
    created = next((item[1] for item in found if item[0] in ('-c', '-C')), None)
    start = [_rev(positionals[0])] if positionals else []
    if created is None and positionals and '--detach' not in seen:
        start = [_name(positionals[0], 'branch')]
    return Command(sub, _canon(sub, found, start, []), revisions=start if created or '--detach' in seen else [],
                   refs=(['HEAD'] + ([f'refs/heads/{created}'] if created else [])), destructive='-C' in seen,
                   summary=(f'Create branch {created}' + (f' at {start[0]}' if start else '') + ' and switch to it')
                           if created else f'Switch to {start[0]}')


def _side(sub, found, positionals, listed, seen):
    """Phase 46 (1.9.28, F3c): checkout/restore --ours or --theirs: one side of a stopped merge, rebase, cherry-pick or
    revert for the conflicted paths (git_plans refuses it while nothing is stopped); add and --continue finish it."""
    side = '--ours' if '--ours' in seen else '--theirs'
    names = listed or [_path(sub, p) for p in positionals]
    # a commit beside a side is no path, and Git refuses the pair: never dropped silently (the w9 security review's note)
    if {'--ours', '--theirs'} <= seen or any(item[0] not in (side, '--worktree') for item in found) or not names \
            or (listed and positionals):
        raise PolicyError(f'Take one side for the conflicted paths: ["{sub}", "{side}", "--", "path"], then stage them '
                          'with ["add", "--", "path"] and finish with --continue.')
    command = Command(sub, [sub, side, '--', *names], paths=names, destructive=True, midway=True,
                      summary=f'Overwrite {", ".join(names)} with the {side[2:]} side of the stopped conflict')
    command.side = side
    return command


def _checkout(sub, args):
    found, positionals, listed, seen = _tokens(sub, args, {'--detach': '--detach', '--ours': '--ours',
                                                           '--theirs': '--theirs'},
                                               {'-b': ('-b', 'name'), '-B': ('-B', 'name')}, paths=True)
    if {'--ours', '--theirs'} & seen:
        return _side(sub, found, positionals, listed, seen)
    if listed:
        if found or len(positionals) > 1:
            raise PolicyError('To restore files: ["checkout", "<commit>", "--", "path"] or ["restore", "--", "path"].')
        source = [_rev(positionals[0])] if positionals else []
        # Phase 46 (1.9.28, F3c): also while a merge is stopped, to take a version of a conflicted file
        return Command(sub, _canon(sub, [], source, listed), paths=listed, revisions=source, destructive=True,
                       midway=True,
                       summary=f'Overwrite {", ".join(listed)} with their version in {source[0] if source else "the index"}')
    created = next((item[1] for item in found if item[0] in ('-b', '-B')), None)
    if len(positionals) > 1 or (not positionals and not created):
        raise PolicyError('git checkout takes one branch, or -b NAME [START]; restore files with "--" and paths.')
    start = [_rev(positionals[0])] if positionals else []
    # A trailing "--" makes Git read the target only as a branch or commit: without it, a name that is no branch
    # ("src") falls back to a path and its unstaged edits are overwritten (security review, 2026-10-27). Phase 46
    # (1.9.28): a reflog entry or ORIG_HEAD names no branch, so it detaches at its bound commit ID.
    bound = created or '--detach' in seen or bool(start and BOUND.fullmatch(start[0]))
    return Command(sub, _canon(sub, found, start, []) + ['--'], revisions=start,
                   refs=['HEAD'] + ([f'refs/heads/{created}'] if created else []), destructive='-B' in seen,
                   slots=set() if not bound else None,
                   summary=(f'Create branch {created} and switch to it' if created else f'Switch to {start[0]}'))


def _restore(sub, args):
    found, positionals, listed, seen = _tokens(sub, args, {'--staged': '--staged', '-S': '--staged',
                                                           '--worktree': '--worktree', '-W': '--worktree',
                                                           '--ours': '--ours', '--theirs': '--theirs'},
                                               {'--source': ('--source', 'rev'), '-s': ('--source', 'rev')}, paths=True)
    if {'--ours', '--theirs'} & seen:
        return _side(sub, found, positionals, listed, seen)
    names = listed or [_path(sub, p) for p in positionals]
    if not names:
        raise PolicyError('Name the paths to restore.')
    worktree = '--worktree' in seen or '--staged' not in seen
    source = next((item[1] for item in found if item[0] == '--source'), None)
    # Phase 46 (1.9.28, F3c): every restore may run while a merge is stopped (taking a version of a conflicted file)
    return Command(sub, [sub, *[t for item in found for t in item], '--', *names], paths=names,
                   revisions=[source] if source else [], destructive=worktree, midway=True, worktree=worktree,
                   summary=('Overwrite ' if worktree else 'Unstage ') + ', '.join(names)
                           + (f' from {source}' if source else ''))


def _branch(sub, args):
    found, positionals, _, seen = _tokens(sub, args, {'-m': '-m', '--move': '-m', '-M': '-M', '-d': '-d',
                                                      '--delete': '-d', '-D': '-D', '-c': '-c', '--copy': '-c',
                                                      '--unset-upstream': '--unset-upstream',
                                                      '-f': '-f', '--force': '-f'},  # Phase 46 (1.9.29)
                                          {'-u': ('-u', 'rev'), '--set-upstream-to': ('-u', 'rev')})
    if '-f' in seen:
        # Phase 46 (1.9.29): `git branch -f NAME START` points an existing branch elsewhere (Git refuses the checked-out
        # one), reviewed as the move it is, with the snapshot Undo; with -d or -m it is -D or -M, as in git-branch(1)
        if seen & {'-d', '-m'} and not seen & {'-D', '-M', '-c', '--unset-upstream', '-u'}:
            seen = (seen - {'-f', '-d', '-m'}) | ({'-D'} if '-d' in seen else {'-M'})
        elif seen - {'-f'}:
            raise PolicyError('git branch -f moves a branch: ["branch", "-f", "<name>", "<commit>"] (with -d it is -D, '
                              'with -m it is -M).')
        else:
            if not 1 <= len(positionals) <= 2:
                raise PolicyError('git branch -f takes the branch and the commit to point it at.')
            name = _name(positionals[0], 'branch')
            start = [_rev(positionals[1])] if len(positionals) == 2 else ['HEAD']
            return Command(sub, ['branch', '-f', name, *start], revisions=start, refs=[f'refs/heads/{name}'], slots={3},
                           destructive=True, worktree=False,
                           summary=f'Point branch {name} at {start[0]}, moving it if it exists (commits only it had are '
                                   'left off it)')
    mode = [m for m in ('-m', '-M', '-d', '-D', '-c', '--unset-upstream', '-u') if m in seen]
    if len(mode) > 1:
        raise PolicyError('Choose one git branch action.')
    mode = mode[0] if mode else None
    if mode in ('-d', '-D'):
        if not 1 <= len(positionals) <= 8:
            raise PolicyError('Name 1 to 8 branches to delete.')
        names = [_name(p, 'branch') for p in positionals]
        return Command(sub, [sub, mode, *names], refs=[f'refs/heads/{n}' for n in names], destructive=True,
                       worktree=False, summary='Delete branch(es) ' + ', '.join(names)
                                               + (' even if unmerged' if mode == '-D' else ''))
    if mode in ('-m', '-M', '-c'):
        if not 1 <= len(positionals) <= 2:
            raise PolicyError(f'git branch {mode} takes [OLD] NEW.')
        names = [_name(p, 'branch') for p in positionals]
        return Command(sub, [sub, mode, *names], refs=[f'refs/heads/{n}' for n in names] + ['HEAD'],
                       destructive=mode == '-M', worktree=False,
                       summary=('Copy' if mode == '-c' else 'Rename') + ' branch ' + ' to '.join(names))
    if mode in ('-u', '--unset-upstream'):
        if len(positionals) > 1:
            raise PolicyError('Name at most one branch.')
        names = [_name(p, 'branch') for p in positionals]
        upstream = next((item[1] for item in found if item[0] == '-u'), None)
        return Command(sub, _canon(sub, found, names, []), worktree=False, refs=[],
                       summary=(f'Set the upstream of {names[0] if names else "the current branch"} to {upstream}'
                                if upstream else 'Remove the upstream setting'))
    if not 1 <= len(positionals) <= 2:
        raise PolicyError('git branch NAME [START] creates a branch; reading branches is ReadGitStatus.')
    name = _name(positionals[0], 'branch')
    start = [_rev(positionals[1])] if len(positionals) == 2 else []
    return Command(sub, [sub, name, *start], revisions=start or ['HEAD'], refs=[f'refs/heads/{name}'], worktree=False,
                   summary=f'Create branch {name} at {start[0] if start else "HEAD"} (without switching)')


def _rm(sub, args):
    # Phase 46 (1.9.28, D179): rm --cached may name a private file; git_plans makes that the untrack plan (the file stays
    # on disk unread, its path joins .gitignore), and refuses it beside other paths or inside a sequence
    cached = '--cached' in (args[:args.index('--')] if '--' in args else args)
    found, positionals, listed, seen = _tokens(sub, args, {'--cached': '--cached', '-r': '-r', '-q': '-q'}, {},
                                               paths=True, untrack=cached)
    names = listed or [_path(sub, p, cached) for p in positionals]
    if not names:
        raise PolicyError('Name the paths to remove.')
    return Command(sub, [sub, *[t for item in found for t in item], '--', *names], paths=names, destructive=not cached,
                   midway=True, worktree=not cached,
                   summary=('Stop tracking ' if cached else 'Delete and stop tracking ') + ', '.join(names)
                           + (' (the files stay on disk)' if cached else ''))


def _mv(sub, args):
    found, positionals, listed, _ = _tokens(sub, args, {}, {}, paths=True)
    names = [_path(sub, p) for p in positionals] + listed
    if len(names) < 2:
        raise PolicyError('git mv takes a source and a destination.')
    return Command(sub, [sub, '--', *names], paths=names, midway=True,
                   summary=f'Move {", ".join(names[:-1])} to {names[-1]}')


def _add(sub, args):
    # Phase 46 (1.9.28, F3d): "git add ." from the project root is "git add -A" (every change, new files included), with
    # the private-file check git_plans makes for -A; "." beside other paths covers them too
    if any(not token.startswith('-') and PurePosixPath(token).as_posix() == '.' for token in args):
        args = [token for token in args if token.startswith('-') and token != '--']
        if {'-N', '--intent-to-add'} & set(args):
            raise PolicyError(DOT.format(sub=sub))
        if not {'-u', '--update', '-A', '--all'} & set(args):
            args.append('-A')
    found, positionals, listed, seen = _tokens(sub, args, {'-u': '--update', '--update': '--update', '-A': '--all',
                                                           '--all': '--all', '-N': '--intent-to-add',
                                                           '--intent-to-add': '--intent-to-add'}, {}, paths=True)
    names = listed or [_path(sub, p) for p in positionals]
    if not names and not ({'--update', '--all'} & seen):
        raise PolicyError('Name the paths to stage, or use -u or -A.')
    return Command(sub, [sub, *[t for item in found for t in item], *(['--', *names] if names else [])], paths=names,
                   midway=True, worktree=False,
                   summary='Stage ' + (', '.join(names) if names else ('every tracked change' if '--update' in seen
                                                                       else 'every change, new files included')))


def _fetch(sub, args):
    found, positionals, _, seen = _tokens(sub, args, {'--prune': '--prune', '-p': '--prune', '--tags': '--tags',
                                                      '--no-tags': '--no-tags'}, {})
    if len(positionals) > 9:
        raise PolicyError('git fetch takes a remote and at most 8 branch names.')
    remote = _remote(positionals[0]) if positionals else 'origin'
    names = [_name(p, 'branch') for p in positionals[1:]]
    return Command(sub, [sub, *[t for item in found for t in item], remote, *names], remote=remote, worktree=False,
                   names=names, summary=f'Fetch from {remote} (updates remote-tracking branches only)')


def _pull(sub, args):
    found, positionals, _, seen = _tokens(sub, args, {'--ff-only': '--ff-only', '--rebase': '--rebase',
                                                      '--no-rebase': '--no-rebase', '--ff': '--ff', '--no-ff': '--no-ff',
                                                      '--no-edit': '--no-edit',
                                                      '--autostash': '--autostash'}, {})  # Phase 46 (1.9.29)
    if len(positionals) > 2:
        raise PolicyError('git pull takes a remote and optionally one branch.')
    if '--rebase' in seen and '--no-rebase' in seen:
        raise PolicyError('Choose --rebase or --no-rebase.')
    remote = _remote(positionals[0]) if positionals else 'origin'
    names = [_name(p, 'branch') for p in positionals[1:]]
    if '--rebase' not in seen and '--no-rebase' not in seen and '--ff-only' not in seen:
        found.append(['--no-rebase'])  # a merge when it cannot fast-forward, as Git's default pull does
    if '--no-edit' not in seen and '--rebase' not in seen:
        found.append(['--no-edit'])
    rebase = '--rebase' in seen
    return Command(sub, [sub, *[t for item in found for t in item], remote, *names], remote=remote, refs=['HEAD'],
                   destructive=rebase, rewrites=rebase, names=names,
                   summary=f'Fetch from {remote} and ' + ('rebase onto' if rebase else 'merge') + ' its branch')


def _unqualified(spec):
    for prefix in ('refs/heads/', 'refs/tags/'):
        if spec.startswith(prefix):
            return spec[len(prefix):]
    return spec


def _push(sub, args):
    """Named tags, as before (git_plans checks that the remote has their commits), and since Phase 46 one branch, which
    git_plans makes the reviewed push operation (bundle transfer, compare-and-swap, readback, Undo): -u records the
    upstream; --force-with-lease, -f or "+branch" may replace rewritten history, always bound to the remote commit seen
    at review; --delete or ":branch" removes a remote branch; no branch means the current one (to its upstream)."""
    lease, rest = None, []
    for token in args:
        if token.startswith('--force-with-lease='):
            if lease is not None or not token[19:]:
                raise PolicyError('Give --force-with-lease once: --force-with-lease or --force-with-lease=<branch>[:<commit>].')
            lease, token = token[19:], '--force-with-lease'
        rest.append(token)
    found, positionals, _, seen = _tokens(sub, rest, {'-u': '--set-upstream', '--set-upstream': '--set-upstream',
                                                      '--tags': '--tags', '--follow-tags': '--follow-tags',
                                                      '--force-with-lease': '--force-with-lease', '-f': '--force',
                                                      '--force': '--force', '--force-if-includes': '--force-if-includes',
                                                      '-d': '--delete', '--delete': '--delete'}, {})
    if len(positionals) > 9:
        raise PolicyError('Push at most 8 branches or tags at once.')
    remote = _remote(positionals[0]) if positionals else None
    forced = bool({'--force', '--force-with-lease'} & seen)
    deleting = '--delete' in seen or any(spec.startswith(':') for spec in positionals[1:])
    specs = []
    for spec in positionals[1:]:
        if spec.startswith('+'):
            forced, spec = True, spec[1:]
        if deleting:
            specs.append(_name(_unqualified(spec[1:] if spec.startswith(':') else spec), 'branch'))
            continue
        if spec.count(':') > 1:
            raise PolicyError('Push "branch" or "branch:remote-branch" (one colon each).')
        source, _, target = spec.partition(':')
        source = 'HEAD' if source == 'HEAD' else _name(_unqualified(source), 'branch or tag')
        specs.append(':'.join([source] + ([_name(_unqualified(target), 'branch or tag')] if target else [])))
    tags = bool({'--tags', '--follow-tags'} & seen)
    if deleting and (forced or tags or '--set-upstream' in seen or not specs):
        raise PolicyError('Delete remote branches on their own: ["push", "origin", "--delete", "<branch>"].')
    argv = [sub, *[t if t != '--force-with-lease' or lease is None else f'--force-with-lease={lease}'
                   for item in found for t in item], *([remote] if remote else []), *specs]
    what = ', '.join(specs) or ('tags' if tags else 'the current branch')
    command = Command(sub, argv, remote=remote, publication=True, destructive=forced or deleting, worktree=False,
                      names=specs, summary=(f'Delete {what} from {remote}' if deleting else
                                            f'Push {what} to {remote or "its upstream"}'
                                            + (', replacing its rewritten history (with a lease on the remote commit '
                                               'seen at review)' if forced else '')
                                            + (' and set the upstream' if '--set-upstream' in seen else '')))
    command.push = {'force': forced, 'lease': lease, 'delete': deleting, 'upstream': '--set-upstream' in seen}
    return command


def _worktree(sub, args):
    """git worktree add/remove: a linked working folder beside the project (or inside it); the folder is resolved and
    bounded by git_plans (`folder` is the path as given). Phase 46 (1.9.29): also prune (Git's records of worktrees whose
    folders are gone, listed at review) and lock/unlock (one linked worktree's lock)."""
    if not args or args[0] not in ('add', 'remove', 'prune', 'lock', 'unlock'):
        # Phase 46 (1.9.28, F3e): ReadGitWorktrees lists only Pebble's own managed worktrees; the terminal lists these.
        # Phase 46 (1.9.29): move and repair stay out (rare; a move is remove and add, each reviewed)
        raise PolicyError('Use ["worktree", "add", "<folder>", "<branch>"], ["worktree", "add", "-b", "<new>", "<folder>", '
                          '"<start>"], ["worktree", "remove", "<folder>"], ["worktree", "prune"] or ["worktree", "lock"|'
                          '"unlock", "<folder>"]; RunCommand "git worktree list" lists them (read-only, no review).')
    action, rest = args[0], args[1:]
    if action == 'prune':
        # Phase 46 (1.9.29): only Git's own records (.git/worktrees/<name>) of linked worktrees whose folders are gone; no
        # folder is touched. git_plans binds Git's dry-run list at review and runs nothing when it is empty
        found, positionals, _, seen = _tokens('worktree prune', rest, {'-v': '-v', '--verbose': '-v', '-n': '-n',
                                                                       '--dry-run': '-n'}, {})
        if positionals:
            raise PolicyError('git worktree prune takes no folder: it forgets every linked worktree whose folder is gone.')
        return Command(sub, ['worktree', 'prune', *(['-n'] if '-n' in seen else []), '-v'], destructive='-n' not in seen,
                       worktree=False, summary=('List' if '-n' in seen else 'Forget') + ' the linked worktrees whose '
                                                                                        'folders are gone')
    if action in ('lock', 'unlock'):
        found, positionals, _, seen = _tokens(f'worktree {action}', rest, {},
                                              {'--reason': ('--reason', 'text')} if action == 'lock' else {})
        if len(positionals) != 1:
            raise PolicyError(f'git worktree {action} takes the linked worktree\'s folder.')
        command = Command(sub, ['worktree', action, *[t for item in found for t in item], positionals[0]],
                          folder=positionals[0], worktree=False, slots=set(),
                          summary=f'{action.capitalize()} the linked worktree {positionals[0]}'
                                  + (' so git worktree prune keeps it' if action == 'lock' else ''))
        command.folder_at = len(command.argv) - 1
        return command
    if action == 'remove':
        found, positionals, _, seen = _tokens('worktree remove', rest, {'--force': '--force'}, {})
        if len(positionals) != 1:
            raise PolicyError('git worktree remove takes the worktree folder.')
        command = Command(sub, ['worktree', 'remove', *[t for item in found for t in item], positionals[0]],
                          folder=positionals[0], destructive=True, worktree=False, slots=set(),
                          summary=f'Remove the linked worktree {positionals[0]}' + (' and its uncommitted changes' if found else ''))
        command.folder_at = len(command.argv) - 1
        return command
    if any(token in ('--track', '--no-track') for token in rest):
        # Phase 46 (1.9.29): the start runs as the commit ID bound at review, which Git cannot track
        raise PolicyError('A new worktree branch starts at the commit bound at review, so --track has nothing to follow: '
                          'use ["worktree", "add", "-b", "<new>", "<folder>", "origin/<branch>"], then ["branch", "-u", '
                          '"origin/<branch>", "<new>"] if it should track it.')
    # Phase 46 (1.9.29): --lock and -q as git-worktree(1) has them; -f (a branch checked out twice), --orphan and
    # --no-checkout stay out
    found, positionals, _, seen = _tokens('worktree add', rest, {'--detach': '--detach', '--lock': '--lock',
                                                                 '-q': '-q', '--quiet': '-q'},
                                          {'-b': ('-b', 'name'), '-B': ('-B', 'name'),
                                           # 1.9.29 (the lead; final check d-gw-203 phrasing 1 stopped on it): Git's own
                                           # "add --lock --reason <text>", as worktree lock already takes it
                                           '--reason': ('--reason', 'text')})
    if '--reason' in seen and '--lock' not in seen:
        raise PolicyError('git worktree add takes --reason only with --lock: ["worktree", "add", "--lock", "--reason", '
                          '"<why>", "<folder>", "<branch>"].')
    if not 1 <= len(positionals) <= 2:
        raise PolicyError('git worktree add takes the new folder and the branch or commit to check out there.')
    folder = positionals[0]
    if not folder or folder.startswith('-') or any(c in folder for c in '\0\n\r'):
        raise PolicyError('Name the new worktree folder.')
    created = next((item[1] for item in found if item[0] in ('-b', '-B')), None)
    start = [positionals[1]] if len(positionals) == 2 else []
    if start:
        start = [_rev(start[0]) if (created or '--detach' in seen) else _name(start[0], 'branch')]
    elif not created:
        raise PolicyError('Name the branch for the new worktree, or create one with -b NAME.')
    argv = ['worktree', 'add', *[t for item in found for t in item if t != '-q'], folder, *start]  # its output is shown
    at = len(argv) - 1 - len(start)
    command = Command(sub, argv, folder=folder, revisions=start if created or '--detach' in seen else [],
                      slots={at + 1} if start and (created or '--detach' in seen) else set(),
                      refs=[f'refs/heads/{created}'] if created else [], destructive=True, worktree=False,
                      summary=f'Create a linked worktree in {folder} on ' + (f'a new branch {created}' if created else start[0]))
    command.folder_at = at
    return command


def _config(sub, args):
    """Phase 46 (git_workflow: "set this repository's user.name and user.email"): the repository's own .git/config and
    only the identity a person asks for. Other keys, --global, --system, --file, includes and editors stay refused;
    git_plans binds the previous value at review for Undo."""
    if any(token in ('--global', '--system', '--worktree', '--file', '-f', '--blob') or token.startswith('--file=')
           for token in args):
        raise PolicyError('Pebble changes only this repository\'s own Git settings (.git/config), never your global or '
                          'system ones: ["config", "user.name", "Ada Lovelace"].')
    found, positionals, _, seen = _tokens(sub, args, {'--local': '--local', '--unset': '--unset'}, {})
    key = positionals[0].lower() if positionals else None
    if key not in CONFIG_KEYS:
        raise PolicyError('Reviewed Git config sets only user.name or user.email in this repository: '
                          '["config", "user.email", "ada@example.com"].')
    if '--unset' in seen:
        if len(positionals) != 1:
            raise PolicyError(f'git config --unset takes only the key: ["config", "--unset", "{key}"].')
        return Command(sub, ['config', '--local', '--unset', key], worktree=False,
                       summary=f'Remove {key} from this repository\'s Git settings (.git/config)')
    if len(positionals) != 2:
        raise PolicyError(f'Give the key and its value: ["config", "{key}", "<value>"] (no editor opens).')
    value = positionals[1]
    if (not 1 <= len(value) <= MAX_IDENT[key] or value.strip(' .,:;<>"\\\'') != value
            or any(ord(c) < 32 or ord(c) == 127 or c in '<>' for c in value)):
        raise PolicyError(f'{key} must be 1 to {MAX_IDENT[key]} characters without control characters, "<", ">" or '
                          'leading or trailing punctuation.')
    return Command(sub, ['config', '--local', key, value], worktree=False,
                   summary=f'Set {key} to {_shown(value)} in this repository\'s Git settings (.git/config)')


def address(value):
    """Phase 46 (1.9.28, D180): a remote address a reviewed remote add or set-url may take, or PolicyError saying why
    not; an address with credentials says how to keep them in the system's credential helper instead, never echoing it."""
    if (not isinstance(value, str) or not value or len(value) > 1000 or value != value.strip() or value.startswith('-')
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise PolicyError('Give the remote address as one plain value: an https or SSH URL, user@host:path, or a folder '
                          'path (no leading "-", control characters or surrounding spaces).')
    scheme = re.match(r'([A-Za-z][A-Za-z0-9+.-]*)://', value)
    userinfo = re.match(r'[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*)@', value)
    if userinfo and (scheme.group(1).lower() not in ('ssh', 'git+ssh', 'ssh+git') or ':' in userinfo.group(1)) \
            or re.match(r'[^/@:]*:[^/@]*@[^/]*:', value) \
            or re.search(r'[?&#](?:access_?token|private_token|token|auth|password|passwd|pwd|key|secret)=', value, re.I):
        raise PolicyError('This address carries a credential (a password or token), so Pebble will not save it in '
                          '.git/config, where it would sit in plain text and show in git remote -v. ' + CREDENTIAL_ADVICE)
    if '::' in value.split('/', 1)[0] or value.startswith(('ext::', 'fd::')):
        raise PolicyError('Remote helper addresses (ext::, fd:: and the like) can run programs; Pebble does not set one.')
    if scheme and scheme.group(1).lower() not in ADDRESS_SCHEMES:
        raise PolicyError(f'{scheme.group(1)}:// addresses are not set by Pebble' + (
            ': they send your work unencrypted; use the https:// or SSH address instead.'
            if scheme.group(1).lower() in ('http', 'git', 'ftp', 'rsync') else '; use an https or SSH address.'))
    hidden = [c for c in value if _hidden(c)]
    if hidden:  # Phase 46 (1.9.28, review 7)
        raise PolicyError(f'This address holds an invisible or formatting character ({_code(hidden[0])}), which can '
                          'make it read as another address, so Pebble does not set it. Give the address without it.')
    host = address_host(value)
    if host and not host.isascii():
        letters = ', '.join(dict.fromkeys(_code(c) for c in host if not c.isascii()))
        raise PolicyError(f'The host in this address has letters outside plain ASCII ({letters}), which can look like '
                          'another host\'s, so Pebble does not set it. Give the host in plain letters; an '
                          'internationalized host name goes in its punycode (xn--) form, which the review flags.')
    imitated = lookalike(host)
    if imitated:  # Phase 46 (1.9.29, D210)
        raise PolicyError(f'The host in this address, {json.dumps(host[:120])}, reads like {imitated} but is not it, so '
                          f'Pebble does not set it. Check the address; if you mean {imitated}, give it as written there.')
    return value


def _code(char):
    return f'U+{ord(char):04X} {unicodedata.name(char, "")}'.rstrip()


def _hidden(char):
    return unicodedata.category(char) in HIDDEN or unicodedata.category(char) == 'Zs' and char != ' '


def address_host(value):
    """Phase 46 (1.9.28, the follow-ups security review 1 and 7): the host a remote address names, lowercased (a URL's,
    or Git's user@host:path form, which has no slash before its first colon), or None for a folder."""
    if not isinstance(value, str):
        return None
    found = re.match(r'[A-Za-z][A-Za-z0-9+.-]*://(?:[^/?#]*@)?(\[[^\]/]*\]|[^/:?#]*)', value)
    if found:
        return found.group(1).lower() or None
    found = re.match(r'(?:[^@/:]*@)?([^/:]+):', value)
    return found.group(1).lower() if found and '/' not in value.split(':', 1)[0] else None


def host_note(host):
    """Phase 46 (1.9.28, review 7): what the card adds after a host that can pass for another: a punycode one, or one
    with letters outside ASCII (set outside Pebble, shown as code points)."""
    if not host.isascii():
        return NON_ASCII_NOTE
    imitated = lookalike(host)  # Phase 46 (1.9.29, D210)
    if imitated:
        return LOOKALIKE_NOTE.format(known=imitated)
    return PUNYCODE_NOTE if any(label.startswith('xn--') for label in host.lower().split('.')) else ''


def visible(text, host=False):
    """Phase 46 (1.9.28, review 7): `text` for the card with each invisible, format, control or separator character as
    its code point (<U+202E>); for a host, each letter outside ASCII too."""
    return ''.join(f'<U+{ord(c):04X}>' if _hidden(c) or host and not c.isascii() else c for c in text)


def planted_words(value):
    """Phase 46 (1.9.28, the follow-ups security review 1b): the hosts that the remote addresses in ProposeGitChange's
    argv name, each written as a link names it (an SSH address names its host as no link does), for
    App.planted_refusal as RunCommand words get it; [] when there are none. Other Git words stay out: a branch such as
    `main` that a page puts in backticks is no planted file."""
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []
    commands = value if isinstance(value, list) and value and all(isinstance(i, list) for i in value) else [value]
    hosts = []
    for argv in commands:
        argv = [word for word in argv if isinstance(word, str)] if isinstance(argv, list) else []
        argv = argv[1:] if argv[:1] == ['git'] else argv
        if argv[:1] == ['remote']:
            hosts += [address_host(word) for word in argv[2:]]
    return [f'https://{host}/' for host in hosts if host]


def _remote_command(sub, args):
    """Phase 46 (1.9.28, D180; the person: "Do as how frontiers do for this or do better then them along with making the UX
    better and giving the user a eaze of mind"): remote add, set-url [--push] and rename, each reviewed with the
    addresses git fetch and git push use before and after (git_plans) and an Undo; the reads run raw."""
    action = args[0] if args else None
    if action in (None, '-v', '--verbose', 'get-url', 'show'):
        raise PolicyError('git remote ' + ' '.join(args[:1]) + ' reads; RunCommand runs "git remote -v" and "git remote '
                          'get-url origin" as typed (read-only, no review; credentials in an address are hidden).')
    if action == 'prune':
        # Phase 46 (1.9.29): the remote-tracking branches whose branches the remote no longer has, read from the remote's
        # own refs and listed at review (git_plans; a local remote, as fetch reads one), deleted with the snapshot Undo;
        # -n lists them only. git fetch --prune does the same after its fetch.
        found, positionals, _, seen = _tokens('remote prune', args[1:], {'-n': '-n', '--dry-run': '-n'}, {})
        if len(positionals) != 1:
            raise PolicyError('git remote prune takes one remote name: ["remote", "prune", "origin"].')
        name = _remote(positionals[0])
        return Command(sub, ['remote', 'prune', *(['-n'] if '-n' in seen else []), name], remote=name, names=[name],
                       destructive='-n' not in seen, worktree=False,
                       summary=('List' if '-n' in seen else 'Delete') + f' the remote-tracking branches of {name} whose '
                                                                        'branches it no longer has')
    if action not in ('add', 'set-url', 'rename'):
        # Phase 46 (1.9.29): remove stays out (its Undo would have to rebuild the remote's whole config section)
        raise PolicyError(f'git remote {_shown(action)} is not a reviewed Git command; Pebble adds a remote, changes its '
                          'address or renames it, each with Undo: ["remote", "add", "<name>", "<address>"], ["remote", '
                          '"set-url", "<name>", "<address>"] (--push for the push address only), ["remote", "rename", '
                          '"<old>", "<new>"], and deletes the remote-tracking branches a remote no longer has: ["remote", '
                          '"prune", "<name>"].')
    found, positionals, _, seen = _tokens(f'remote {action}', args[1:], {'--push': '--push'} if action == 'set-url' else {},
                                          {})
    if len(positionals) != 2:
        raise PolicyError({'add': 'git remote add takes a name and an address: ["remote", "add", "upstream", '
                                  '"https://github.com/owner/repo.git"].',
                           'set-url': 'git remote set-url takes a name and the new address (no --add or --delete).',
                           'rename': 'git remote rename takes the old name and the new one.'}[action])
    names = [_remote(positionals[0])] + ([_remote(positionals[1])] if action == 'rename' else [])
    url = address(positionals[1]) if action != 'rename' else None
    argv = ['remote', action, *[t for item in found for t in item], *positionals[:1], *([url] if url else names[1:])]
    command = Command(sub, argv, worktree=False, destructive=True, names=names,
                      summary={'add': f'Add the remote {names[0]}',
                               'set-url': f'Change the {"push " if "--push" in seen else ""}address of the remote {names[0]}',
                               'rename': f'Rename the remote {names[0]} to {names[-1]}'}[action])
    command.remote_change = {'action': action, 'name': names[0], 'new_name': names[-1] if action == 'rename' else None,
                             'url': url, 'push': '--push' in seen}
    return command


def _clean(sub, args):
    """Phase 46 (git_workflow: "remove the untracked build output"): untracked files (-d folders too; -x ignored ones
    too, -X only ignored ones). git_plans binds git clean -n's exact list at review, shows it and removes only those."""
    found, positionals, listed, seen = _tokens(sub, args, {'-f': '-f', '--force': '-f', '-d': '-d', '-x': '-x', '-X': '-X',
                                                           '-n': '-n', '--dry-run': '-n', '-q': '-q', '--quiet': '-q'},
                                               {}, paths=True)
    if '-x' in seen and '-X' in seen:
        raise PolicyError('Choose -x (untracked and ignored files) or -X (only ignored files).')
    if not {'-f', '-n'} & seen:
        raise PolicyError('git clean needs -f (the review lists the exact files first) or -n to only list them.')
    names = listed or [_path(sub, p) for p in positionals]
    dry = '-n' in seen
    options = [flag for flag in ('-d', '-x', '-X') if flag in seen]
    which = 'ignored' if '-X' in seen else 'untracked and ignored' if '-x' in seen else 'untracked'
    return Command(sub, ['clean', '-n' if dry else '-f', *options, *(['--', *names] if names else [])], paths=names,
                   destructive=not dry, worktree=not dry,
                   summary=('List' if dry else 'Delete') + f' the {which} files' + (' and folders' if '-d' in seen else '')
                           + (' in ' + ', '.join(names) if names else '') + (' (nothing is deleted)' if dry else
                                                                             ' that the review lists'))


BISECT_MARKS = ('good', 'bad', 'new', 'old', 'skip')


def _bisect(sub, args):
    """Phase 46 (git_workflow: "find the commit that broke the test"): a reviewed bisect sequence. start, the marks and
    reset move HEAD as Git's own bisect does; the test runs between steps with Pebble's tools. bisect run and replay
    would run a program or read a file inside Git, so they stay refused."""
    action, rest = (args[0], args[1:]) if args else (None, [])
    if action in ('run', 'replay', 'visualize', 'view'):
        raise PolicyError(f'git bisect {action} is not available: run the test yourself (RunTests or RunCommand) after '
                          'each step and mark the commit with ["bisect", "good"] or ["bisect", "bad"].')
    if action == 'start':
        found, positionals, _, _ = _tokens('bisect start', rest, {'--first-parent': '--first-parent'}, {})
        if len(positionals) > MAX_REVS:
            raise PolicyError(f'Name at most {MAX_REVS} commits: the bad one first, then good ones.')
        revs = [_rev(p) for p in positionals]
        return Command(sub, ['bisect', 'start', *[t for item in found for t in item], *revs], revisions=revs,
                       refs=['HEAD'], summary='Start a bisect' + (f' between bad {revs[0]} and good {", ".join(revs[1:])}'
                                                                  if len(revs) > 1 else '') + ' and check out a commit to test')
    if action in BISECT_MARKS:
        _, positionals, _, _ = _tokens(f'bisect {action}', rest, {}, {})
        if len(positionals) > (1 if action in ('bad', 'new') else MAX_REVS):
            raise PolicyError(f'git bisect {action} takes at most one commit.' if action in ('bad', 'new')
                              else f'Name at most {MAX_REVS} commits.')
        revs = [_rev(p) for p in positionals]
        return Command(sub, ['bisect', action, *revs], revisions=revs, refs=['HEAD'], midway=True,
                       summary=f'Mark {", ".join(revs) or "the checked-out commit"} as {action} and check out the next '
                               'commit to test')
    if action in ('reset', 'log', 'terms') and not rest:
        return Command(sub, ['bisect', action], refs=['HEAD'] if action == 'reset' else [], midway=True,
                       worktree=action == 'reset',
                       summary={'reset': 'End the bisect and return to the branch it started on',
                                'log': 'Show what this bisect has marked so far',
                                'terms': 'Show the terms this bisect uses'}[action])
    raise PolicyError('Use ["bisect", "start", "<bad>", "<good>"], then ["bisect", "good"] or ["bisect", "bad"] after '
                      'testing each checked-out commit, and ["bisect", "reset"] at the end.')


PARSERS = {'revert': _revert, 'cherry-pick': _revert, 'tag': _tag, 'stash': _stash_command, 'commit': _commit,
           'reset': _reset, 'rebase': _rebase, 'merge': _merge, 'switch': _switch, 'checkout': _checkout,
           'restore': _restore, 'branch': _branch, 'rm': _rm, 'mv': _mv, 'add': _add, 'fetch': _fetch, 'pull': _pull,
           'push': _push, 'worktree': _worktree, 'config': _config, 'clean': _clean, 'bisect': _bisect,
           'remote': _remote_command}
