"""Phase 40 (1.6.0, D90-D92): what the Companion's Shell may do, decided before it runs.

The sandbox (companion_shell.py) is the boundary: every command runs under a per-command macOS profile that writes
only in the workbench and the folders the person granted, reads the home folder except the protected list below, and
reaches the network only through the egress broker for approved hosts. These rules decide what to refuse outright
and what to ask the person about; they never widen the sandbox on their own.

A command line is split into its parts (plain commands joined by `&&`, `||`, `;`, `|` or `&`) and each part is
checked. A line with command or process substitution, a subshell, control flow, `eval` or `source` is kept whole as
one opaque part (Codex's rule): it still runs in the sandbox, but nothing about it is taken on trust.

Phase 46 (1.9.29, golden dev d-cd-01, 2026-10-30): a heredoc's body is the input of its command, not commands. Before,
every body line was split off as a command of its own and the whole line was opaque, so the denied-word scan read the
body: a Python heredoc script whose comment said "at" was refused as the `at` scheduler. Now `_scan` reads the body up
to its end line, and `parse` decides what it is by who reads it (Heredoc.kind): a shell reading its standard input
gets it as commands (parsed like `sh -c`, every refusal kept); an interpreter as code (as `python3 -c`: it asks for the
home folders it names, never "always"); a program known to take it only as data (cat, tee, grep ...) as data; any
other reader (xargs, sqlite3, parallel ...) as 1.9.28 did, as commands in an opaque line. A body whose end word is
unquoted and that holds `$(` or a backtick is command substitution, a missing end line leaves the line opaque, and a
protected path in any body is refused, as before.

Decisions:
- deny: privilege changes, system administration, keychain and AppleEvents tools, and any path on the protected list;
- ask: hosts to reach, folders to write outside the workbench, deleting outside the workbench (every time), commands
  AgentGuard flags as irreversible, and anything whose arguments came from untrusted text that reaches the network or
  changes files outside the workbench (D91: Pebble asks only to go beyond the sandbox);
- allow: everything else, inside the sandbox, without asking.
"Always" is never offered for interpreters running inline code, deletes, gated paths or tainted commands.
"""

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .policy import PolicyError

WORKBENCH_NAME = 'Pebble Workbench'
LINE_LIMIT = 8000
NEST_LIMIT = 3

# D92: never readable (or writable) through the Shell, ReadFile or WriteFile: keys and credentials, passwords,
# browser data, Messages, Mail, Pebble's own data and backups, other agents' credentials, shell histories and macOS
# privacy databases. Paths are relative to the home folder; each covers everything below it.
PROTECTED_HOME = (
    # keys and credentials
    '.ssh', '.gnupg', '.aws', '.azure', '.config/gcloud', '.kube', '.docker', '.netrc', '.npmrc', '.pypirc',
    '.git-credentials', '.config/gh', '.config/hub', '.config/op', '.vault-token', '.terraform.d', '.oci', '.s3cfg',
    '.boto', '.pgpass', '.my.cnf', '.config/rclone', '.cargo/credentials', '.cargo/credentials.toml',
    '.gem/credentials', '.m2/settings.xml', '.gradle/gradle.properties', '.config/heroku', '.config/configstore',
    'Library/Keychains',
    # password managers
    'Library/Group Containers/2BUA8C4S2C.com.1password', 'Library/Application Support/1Password',
    'Library/Containers/com.1password.1password', 'Library/Application Support/Bitwarden',
    'Library/Containers/com.bitwarden.desktop', 'Library/Application Support/KeePassXC',
    'Library/Application Support/Dashlane', 'Library/Application Support/Enpass',
    # browser data
    'Library/Safari', 'Library/Containers/com.apple.Safari', 'Library/Cookies', 'Library/HTTPStorages',
    'Library/WebKit', 'Library/Application Support/Google', 'Library/Application Support/Chromium',
    'Library/Application Support/BraveSoftware', 'Library/Application Support/Microsoft Edge',
    'Library/Application Support/Arc', 'Library/Application Support/Firefox', 'Library/Application Support/Vivaldi',
    'Library/Application Support/com.operasoftware.Opera', 'Library/Application Support/Comet',
    'Library/Application Support/Perplexity', 'Library/Application Support/Orion', 'Library/Caches/Google',
    'Library/Caches/BraveSoftware', 'Library/Caches/Firefox', 'Library/Caches/com.apple.Safari',
    # Messages and Mail
    'Library/Messages', 'Library/Containers/com.apple.MobileSMS', 'Library/Mail', 'Library/Containers/com.apple.mail',
    'Library/Group Containers/group.com.apple.mail',
    # Pebble's own data, signing and backups (data_home.protected_roots() adds the live data folders)
    '.pebble', 'Documents/Pebble', 'PebbleBackups', 'Library/Application Support/com.pebble.local',
    # other agents' credentials and sessions
    '.claude', '.claude.json', '.codex', '.gemini', '.config/claude', '.config/github-copilot',
    '.cursor', '.continue',
    # shell and tool histories
    '.zsh_history', '.zsh_sessions', '.bash_history', '.bash_sessions', '.python_history', '.node_repl_history',
    '.psql_history', '.mysql_history', '.sqlite_history', '.irb_history', '.lesshst', '.local/share/fish',
    # macOS privacy databases and accounts
    'Library/Application Support/com.apple.TCC', 'Library/Accounts', 'Library/IdentityServices',
)
# Top-level home entries protected by their prefix: every Claude account folder (.claude, .claude.json and any
# per-account copy), whatever its name.
PROTECTED_PREFIX = ('.claude',)
# Names that are secrets wherever they are. Phase 46 (1.9.28, the w9 security review F3): also Git's credential store,
# web and database password files, kubeconfig, Docker's and kubectl's config, Terraform state, service-account and
# OAuth client keys, and env files by suffix (prod.env); git_workflows.private() reuses this for every Git route.
PROTECTED_NAME = re.compile(
    r'(^|/)(\.env(\.[^/]*)?|\.envrc|credentials(\.json|\.toml|\.ya?ml)?|secrets?(\.json|\.toml|\.ya?ml)?'
    r'|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|[^/]*\.(pem|key|p12|pfx|kdbx|keychain|keychain-db|ppk|jks|keystore)'
    r'|\.git-credentials|\.htpasswd|\.pgpass|\.dockercfg|kubeconfig|\.docker/config\.json|\.kube/config'
    r'|[^/]*\.tfstate(\.[^/]*)?|[^/]+\.env|[^/]*service[-_]account[^/]*\.json|client_secrets?[^/]*\.json)$',
    re.I)
# Writable only with the person's yes each time, never "always": startup files, login items, preferences, Git config.
GATED_HOME = ('.zshrc', '.zprofile', '.zshenv', '.zlogin', '.bashrc', '.bash_profile', '.profile', '.gitconfig',
              '.config/git', 'Library/LaunchAgents', 'Library/Preferences', 'Library/Application Support')

# Refused at a command position: privilege, system administration, keychain and AppleEvents tools, other apps.
DENIED = frozenset({
    'sudo', 'su', 'doas', 'login', 'passwd', 'chsh', 'dscl', 'dseditgroup', 'sysadminctl', 'fdesetup', 'security',
    'osascript', 'launchctl', 'systemsetup', 'networksetup', 'scutil', 'pmset', 'csrutil', 'spctl', 'nvram',
    'kextload', 'kextunload', 'kmutil', 'tccutil', 'profiles', 'diskutil', 'fdisk', 'gpt', 'shutdown', 'reboot',
    'halt', 'killall', 'pkill', 'crontab', 'at', 'atq', 'atrm', 'bless', 'installer', 'softwareupdate',
})
DENIED_PREFIX = ('mkfs', 'newfs_')
# defaults: reading is fine; writing another app's or the system's settings is not.
DEFAULTS_WRITE = frozenset({'write', 'delete', 'import', 'rename'})
INTERPRETERS = frozenset({'python', 'python3', 'node', 'ruby', 'perl', 'php', 'bash', 'sh', 'zsh', 'fish', 'dash',
                          'ksh', 'tcsh', 'csh', 'deno', 'bun', 'lua', 'swift', 'osascript', 'Rscript', 'julia'})
SHELLS = frozenset({'bash', 'sh', 'zsh', 'dash', 'ksh'})
DELETERS = frozenset({'rm', 'rmdir', 'unlink', 'srm', 'trash'})
WRAPPERS = frozenset({'env', 'nohup', 'time', 'nice', 'command', 'exec', 'caffeinate', 'stdbuf'})
# Hints only: commands that usually need the network (the sandbox has none until hosts are approved).
NETWORKED = frozenset({'curl', 'wget', 'ssh', 'scp', 'sftp', 'nc', 'telnet', 'ftp', 'ping', 'dig', 'nslookup',
                       'host', 'whois', 'http', 'https', 'aria2c', 'yt-dlp', 'gh', 'brew'})
NETWORKED_SUB = {'git': {'clone', 'fetch', 'pull', 'push', 'ls-remote', 'submodule'},
                 'pip': {'install', 'download'}, 'pip3': {'install', 'download'},
                 'npm': {'install', 'i', 'ci', 'add', 'publish', 'view', 'update'}, 'npx': None,
                 'yarn': {'add', 'install', 'upgrade'}, 'pnpm': {'add', 'install', 'i', 'update'},
                 'uv': {'pip', 'add', 'sync', 'tool', 'run'}, 'cargo': {'install', 'fetch', 'update', 'add'},
                 'go': {'get', 'install', 'mod'}, 'gem': {'install'}, 'docker': {'pull', 'push', 'run', 'build'}}
OPAQUE_KEYWORDS = frozenset({'if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'until', 'do', 'done', 'case',
                             'esac', 'function', 'select', '[[', '{', '}', 'eval', 'source', '.', 'coproc', 'trap'})
URL = re.compile(r'\bhttps?://([A-Za-z0-9.-]+)', re.I)


@dataclass(frozen=True)
class Part:
    argv: tuple            # words after removing redirections, leading assignments and wrappers
    redirects: tuple       # (operator, target) pairs
    text: str              # the part as written
    assignments: tuple = ()
    replace: str = ''      # Phase 46 (1.9.28): the replacement string of the xargs that runs argv (-I R, -J R), or ''
    piped: bool = False    # Phase 46 (1.9.28): this part's output goes into the next part (a single `|`)


@dataclass(frozen=True)
class Heredoc:
    """Phase 46 (1.9.29): one here-document: its end word, whether that word was quoted (the body is then literal),
    the body as the command reads it, and what it is to its reader: 'data', 'code', 'commands' or 'unread' (read as
    1.9.28 read every body: commands, and the line opaque) (parse, _kind)."""
    word: str
    quoted: bool
    body: str
    kind: str = 'data'


@dataclass(frozen=True)
class Parsed:
    line: str
    parts: tuple           # Part per plain command, in order (nested shell -c strings included)
    opaque: tuple          # reasons the line (or a nested string) could not be split; empty when fully split
    heredocs: tuple = ()   # Phase 46 (1.9.29): Heredoc per here-document, nested ones included

    def unread(self):
        """The line without the bodies its commands read as data: what AgentGuard's command patterns read."""
        text = self.line
        for doc in self.heredocs:
            if doc.kind == 'data' and doc.body:
                text = text.replace(doc.body, '\n', 1)
        return text


@dataclass
class Decision:
    action: str                                   # 'allow', 'ask' or 'deny'
    reasons: list = field(default_factory=list)   # plain words for the person and the model
    hosts: list = field(default_factory=list)     # hosts to approve
    folders: list = field(default_factory=list)   # folders to approve for writing
    gates: list = field(default_factory=list)     # asks that recur every time
    always: bool = True                           # whether "always" may be offered
    parsed: Parsed = None

    def to_json(self):
        return {'action': self.action, 'reasons': self.reasons, 'hosts': self.hosts, 'folders': self.folders,
                'gates': self.gates, 'always_offered': self.always}


def workbench(home=None):
    """Pebble's own working folder: written without asking (D91). Created by companion_shell on first use."""
    return Path(home or Path.home()) / WORKBENCH_NAME


# -- parsing -----------------------------------------------------------------------------------------------------

END_WORD_STOP = set(' \t\n;&|<>()')


def _end_word(line, i):
    """Phase 46 (1.9.29): the end word of a here-document whose operator ends just before i: (word without its quotes,
    whether any of it was quoted, the index after it). An empty word is ('', False, i)."""
    while i < len(line) and line[i] in ' \t':
        i += 1
    word, quoted, quote = [], False, None
    while i < len(line):
        c = line[i]
        if quote:
            if c == quote:
                quote = None
            else:
                word.append(c)
        elif c in ('"', "'"):
            quote, quoted = c, True
        elif c == '\\' and i + 1 < len(line):
            word.append(line[i + 1])
            quoted = True
            i += 1
        elif c in END_WORD_STOP:
            break
        else:
            word.append(c)
        i += 1
    return ''.join(word), quoted, i


def _bodies(line, i, pending, reasons, docs):
    """Phase 46 (1.9.29): read the bodies of the pending here-documents from i (the start of the line after their
    command line), in order; each ends at a line that is exactly its word (leading tabs stripped for `<<-`). Returns
    the index after the last end line. A body that never ends takes the rest and leaves the line opaque."""
    for word, quoted, strip in pending:
        lines, start = [], i
        while True:
            if i >= len(line):
                reasons.append('a heredoc without its end line')
                docs.append(Heredoc(word, quoted, line[start:]))
                return len(line)
            end = line.find('\n', i)
            text = line[i:] if end < 0 else line[i:end]
            i = len(line) if end < 0 else end + 1
            if (text.lstrip('\t') if strip else text) == word:
                break
            lines.append(text)
        body = ''.join(text + '\n' for text in lines)
        if not quoted and ('`' in body or '$(' in body):
            reasons.append('command substitution')  # an unquoted end word: the shell expands the body
        docs.append(Heredoc(word, quoted, body))
    return i


def _scan(line, docs=None):
    """Split at top-level ; && || | & and newlines, respecting quotes and escapes. Returns (pieces, opaque reasons,
    pipes): pipes[k] says whether piece k's output goes into the next piece (a single `|`). Phase 46 (1.9.29): a
    here-document's body is read up to its end line and never split (docs gets a Heredoc for each)."""
    pieces, buf, reasons, pipes = [], [], [], []
    i, n, quote = 0, len(line), None
    docs = [] if docs is None else docs
    pending = []  # (word, quoted, strip tabs) of the here-documents whose bodies start after this command line
    nested = []   # the closing characters of the ${...} and $[...] expansions open here
    while i < n:
        c = line[i]
        if quote == "'":
            buf.append(c)
            if c == "'":
                quote = None
            i += 1
            continue
        if c == '\\' and i + 1 < n:
            buf.append(line[i:i + 2])
            i += 2
            continue
        if quote == '"':
            if c == '"':
                quote = None
            elif c == '`' or line.startswith('$(', i):
                reasons.append('command substitution')
            buf.append(c)
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
            buf.append(c)
            i += 1
            continue
        if line.startswith(('${', '$['), i):
            nested.append(']' if line[i + 1] == '[' else '}')  # a parameter or zsh arithmetic expansion
            buf.append(line[i:i + 2])
            i += 2
            continue
        if nested and c == nested[-1]:
            nested.pop()
        if line.startswith('<<<', i):
            buf.append('<<<')  # a here-string: one word of input, read as any other word
            i += 3
            continue
        if line.startswith('<<', i) and (nested or reasons):
            # not a here-document to the shell (`${a//<<x}`, `$[1<<2]`), or one in a line that is opaque already: as
            # 1.9.28, its lines stay commands and the denied-word scan reads all of it
            reasons.append('a heredoc')
            buf.append('<<')
            i += 2
            continue
        if line.startswith('<<', i):
            strip = line.startswith('<<-', i)
            word, quoted, end = _end_word(line, i + (3 if strip else 2))
            if word:
                pending.append((word, quoted, strip))
            else:
                reasons.append('a heredoc without an end word')
            buf.append(line[i:end])
            i = end
            continue
        if c == '\n' and pending:
            pieces.append(''.join(buf))
            pipes.append(False)
            buf = []
            i = _bodies(line, i + 1, pending, reasons, docs)
            pending = []
            continue
        if c == '`' or line.startswith('$(', i):
            reasons.append('command substitution')
        elif line.startswith(('<(', '>('), i):
            reasons.append('process substitution')
        elif c in '()' and not line.startswith('$(', i - 1 if i else 0):
            reasons.append('a subshell')
        elif c == '#' and (i == 0 or line[i - 1] in ' \t;&|\n'):
            j = line.find('\n', i)
            i = n if j < 0 else j
            continue
        if c in ';\n' or c == '&' and not line.startswith('&>', i) and (i == 0 or line[i - 1] not in '<>'):
            if line.startswith('&&', i):
                i += 1
            pieces.append(''.join(buf))
            pipes.append(False)
            buf = []
            i += 1
            continue
        if c == '|':
            pipes.append(not line.startswith('||', i))
            if line.startswith('||', i):
                i += 1
            pieces.append(''.join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    if quote:
        reasons.append('an unclosed quote')
    if pending:
        reasons.append('a heredoc without its end line')  # the line ends on its command line: no body at all
    pieces.append(''.join(buf))
    pipes.append(False)
    kept = [(p.strip(), pipe) for p, pipe in zip(pieces, pipes) if p.strip()]
    return [p for p, _ in kept], reasons, [pipe for _, pipe in kept]


OPERATOR = set('<>&|')


def _part(text, piped=False):
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars='<>&|')
        lexer.whitespace_split = True
        lexer.commenters = ''
        words = list(lexer)
    except ValueError:
        return None
    argv, redirects, assignments = [], [], []
    i = 0
    while i < len(words):
        word = words[i]
        if word and set(word) <= OPERATOR:
            redirects.append((word, words[i + 1] if i + 1 < len(words) else ''))
            i += 2
            continue
        if word.isdigit() and i + 1 < len(words) and words[i + 1] and set(words[i + 1]) <= OPERATOR:
            i += 1  # a file descriptor before its redirection (2>&1)
            continue
        if not argv and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', word):
            assignments.append(word)
        else:
            argv.append(word)
        i += 1
    # Wrappers run the command after their own options: env FOO=1 cmd, nohup cmd, time cmd, xargs -n1 cmd.
    replace = ''
    while argv:
        head = Path(argv[0]).name
        if head in WRAPPERS:
            rest = argv[1:]
            while rest and (rest[0].startswith('-') or head == 'env' and '=' in rest[0]
                            or head == 'nice' and rest[0].lstrip('-').isdigit()):
                rest = rest[1:]
            argv = rest
            continue
        if head == 'timeout' and len(argv) > 2:
            argv = argv[2:]
            continue
        if head == 'xargs':
            rest = argv[1:]
            while rest and rest[0].startswith('-'):
                # Phase 46 (1.9.28, sec-w7 1): the replacement string is this xargs's own option, never a word of the
                # part's text; BSD -J, -R and -S take a value too (`xargs -J % mv % dest` ran `%` as the command)
                flag = rest[0]
                if flag in ('-I', '-J') and len(rest) > 1:
                    replace = rest[1]
                elif re.fullmatch(r'-[IJ].+', flag):
                    replace = flag[2:]
                elif flag.startswith(('--replace=', '-i')) and flag != '--replace=':
                    replace = flag.split('=', 1)[1] if '=' in flag else flag[2:] or '{}'
                elif flag == '--replace':
                    replace = '{}'
                rest = rest[2:] if flag in ('-n', '-I', '-J', '-L', '-P', '-R', '-S', '-s', '-E', '-d') else rest[1:]
            argv = rest
            continue
        break
    return Part(tuple(argv), tuple(redirects), text, tuple(assignments), replace, piped)


# Phase 46 (1.9.29): shells that run what they read on their standard input (no -c string): a body they read is commands
STDIN_SHELLS = SHELLS | {'fish', 'tcsh', 'csh'}
# Programs whose standard input is only ever data to them (no program of their own read from it: sed and awk only
# without -f). A body any other program reads (xargs, sqlite3's .shell, parallel, patch ...) is read as 1.9.28 read it.
DATA_READERS = frozenset({'cat', 'tee', 'head', 'tail', 'wc', 'sort', 'uniq', 'grep', 'egrep', 'fgrep', 'rg', 'sed', 'awk',
                          'tr', 'cut', 'paste', 'column', 'fmt', 'fold', 'nl', 'rev', 'tac', 'jq', 'base64', 'xxd', 'od',
                          'hexdump', 'diff', 'cmp', 'comm', 'pbcopy', 'iconv', 'shasum', 'md5', 'echo', 'printf',
                          'true', 'read'})


def _shell_flag(argv):
    return next((i for i, a in enumerate(argv[1:], 1) if re.fullmatch(r'-[a-zA-Z]*c[a-zA-Z]*', a)), None)


def _kind(readers):
    """Phase 46 (1.9.29): what one here-document is to the parts that read it (the part holding its `<<` and the parts
    it pipes into, so a pipe cannot hide the reader: `cat <<EOF | sh`): 'commands' when a shell reads its standard
    input, 'unread' for a reader not known to take it as data or code, 'code' when an interpreter reads it, else 'data'."""
    kinds = set()
    for part in readers:
        head = Path(part.argv[0]).name if part.argv else ''
        if head in STDIN_SHELLS and _shell_flag(part.argv) is None:
            kinds.add('commands')
        elif re.search(r'(?:^|\s)xargs\b', part.text) or head in STDIN_SHELLS:
            kinds.add('unread')  # xargs makes it arguments, which a shell given -c '{}' runs
        elif head in INTERPRETERS:
            kinds.add('code')
        elif head not in DATA_READERS or head in ('sed', 'awk') and '-f' in part.argv:
            kinds.add('unread')
    return next((kind for kind in ('commands', 'unread', 'code') if kind in kinds), 'data')


def parse(line, _depth=0):
    """The line's parts; nested `sh -c '...'` strings are parsed too (depth 3), each adding its parts. Phase 46
    (1.9.29): a here-document's body is never split into parts; one a shell reads is parsed as commands (same depth)."""
    if not isinstance(line, str) or not line.strip() or len(line) > LINE_LIMIT or '\x00' in line:
        raise PolicyError('Give one command line under 8,000 characters.')
    docs = []
    pieces, reasons, pipes = _scan(line, docs)
    parts, opaque, heredocs, top = [], list(reasons), [], []
    for piece, piped in zip(pieces, pipes):
        part = _part(piece, piped)
        if part is None:
            opaque.append('quoting that cannot be read')
            continue
        if part.argv and (part.argv[0] in OPAQUE_KEYWORDS or part.argv[0].endswith('()')):
            opaque.append('control flow')
        top.append(len(parts))
        parts.append(part)
        head = Path(part.argv[0]).name if part.argv else ''
        flag = _shell_flag(part.argv)
        if head in SHELLS and flag is not None:
            inner = part.argv[flag + 1:flag + 2]
            if not inner:
                opaque.append('a shell without a command')
            elif _depth >= NEST_LIMIT:
                opaque.append('shells nested too deeply')
            else:
                nested = parse(inner[0], _depth + 1)
                parts.extend(nested.parts)
                opaque.extend(nested.opaque)
                heredocs.extend(nested.heredocs)
    # each body's readers: the n-th `<<` of the line's own parts is the n-th body's, with the parts it pipes into
    holders = [index for index in top for op, _ in parts[index].redirects if op == '<<']
    for number, doc in enumerate(docs):
        readers = []
        if len(holders) == len(docs):
            at = top.index(holders[number])
            readers = [parts[top[at]]]
            while parts[top[at]].piped and at + 1 < len(top):
                at += 1
                readers.append(parts[top[at]])
        kind = _kind(readers) if readers else 'unread'
        if kind == 'unread':
            opaque.append('a heredoc')  # as 1.9.28: its lines are commands and the denied-word scan reads all of it
        if kind in ('commands', 'unread') and doc.body.strip():
            if _depth >= NEST_LIMIT:
                opaque.append('shells nested too deeply')
            else:
                nested = parse(doc.body, _depth + 1)
                parts.extend(nested.parts)
                opaque.extend(nested.opaque)
                heredocs.extend(nested.heredocs)
        heredocs.append(Heredoc(doc.word, doc.quoted, doc.body, kind))
    return Parsed(line, tuple(parts), tuple(dict.fromkeys(opaque)), tuple(heredocs))


# -- paths -------------------------------------------------------------------------------------------------------

def _resolve(word, cwd, home):
    word = HOME_VAR.sub('~', word, count=1)  # Phase 46: "$HOME/Documents" is ~/Documents, not ./$HOME/Documents
    if word == '~' or word.startswith('~/'):
        text = str(home) + word[1:]   # the policy's own home folder, always the person's
    elif word.startswith('~'):
        return None  # another user's home: never resolved
    else:
        text = word
    path = Path(text) if os.path.isabs(text) else Path(cwd) / text
    try:
        return Path(os.path.realpath(path))
    except (OSError, ValueError):
        return None


def protected_path(path, home=None, extra_roots=()):
    """The protected entry `path` falls under (D92), or None. Symlinks are resolved first."""
    home = Path(home or Path.home())
    try:
        real = Path(os.path.realpath(path))
    except (OSError, ValueError):
        return 'an unreadable path'
    text = str(real)
    folded = text.casefold()
    for entry in PROTECTED_HOME:
        base = str(Path(os.path.realpath(home / entry))).casefold()
        if folded == base or folded.startswith(base + '/'):
            return '~/' + entry
    real_home = str(Path(os.path.realpath(home))).casefold()
    if folded.startswith(real_home + '/'):
        top = folded[len(real_home) + 1:].split('/', 1)[0]
        if top.startswith(PROTECTED_PREFIX):
            return '~/' + top
    for root in extra_roots:
        base = str(Path(os.path.realpath(root))).casefold()
        if folded == base or folded.startswith(base + '/'):
            return str(root)
    if PROTECTED_NAME.search(text) and os.path.lexists(text) and inside(real, [Path(os.path.realpath(home))]):
        return Path(text).name
    return None


def gated_path(path, home=None):
    home = Path(home or Path.home())
    folded = str(Path(os.path.realpath(path))).casefold()
    for entry in GATED_HOME:
        base = str(Path(os.path.realpath(home / entry))).casefold()
        if folded == base or folded.startswith(base + '/'):
            return '~/' + entry
    return None


def inside(path, roots):
    folded = str(path).casefold()
    return any(folded == str(r).casefold() or folded.startswith(str(r).casefold().rstrip('/') + '/') for r in roots)


def writable_folder(value, home=None):
    """A folder the person may grant for writing: inside the home folder (not the home folder itself), not protected,
    resolved. Returns (path, gated entry or None); refuses anything else."""
    home = Path(os.path.realpath(home or Path.home()))
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise PolicyError('Name a folder inside your home folder.')
    path = _resolve(value.strip(), home, home)
    if path is None or path == home or not inside(path, [home]):
        # Phase 45 (F066): the refusal names where writing works instead of a control that does not exist
        raise PolicyError('Pebble can be given folders inside your home folder only, never the home folder itself. It '
                          'writes in ~/Pebble Workbench without asking, and in a folder inside your home folder after '
                          'you allow it.')
    entry = protected_path(path, home)
    if entry:
        raise PolicyError(f'{entry} is protected: Pebble never reads or writes it.')
    return path, gated_path(path, home)


HOME_VAR = re.compile(r'^\$(?:HOME\b|\{HOME\})(?=/|$)')
# Phase 46: a call's own temporary folder ($TMPDIR, always writable in the sandbox) was asked for as a folder named
# "$TMPDIR" inside the home folder
SCRATCH_VAR = re.compile(r'^\$(?:TMPDIR\b|\{TMPDIR\})')


# Phase 46 (golden dev d-cd-01 phrasing 3 on 1.9.26, live: three listings with `2>/dev/null` refused in a row, the
# companion handed the task back, "Pebble refused access to ~/Downloads and ~/Documents, even just to list them"):
# /dev/null was taken for a folder outside the home folder. A device sink is never a write target.
DEVICE_SINK = re.compile(r'/dev/(?:null|stdout|stderr|tty|fd/[0-9]{1,3})')


# Phase 46 (1.9.28, measured under sandbox-exec on a throwaway home, each allowed without asking and refused): tools
# that change a file where it is or write their output beside it. sed -i, perl -i, gzip, bzip2, xz, textutil and sips
# --out work only once the file's folder is writable (a temporary file renamed over it, or a new file beside it); sips
# in place, truncate, chmod, chown, chflags, xattr and SetFile once the file itself is.
IN_PLACE = frozenset({'sed', 'gsed', 'perl', 'sips', 'textutil', 'gzip', 'gunzip', 'bzip2', 'bunzip2', 'xz', 'unxz',
                      'truncate', 'chmod', 'chown', 'chflags', 'xattr', 'SetFile'})
SIPS_WRITES = frozenset({'-s', '--setProperty', '-d', '--deleteProperty', '-z', '--resampleHeightWidth', '-Z',
                         '--resampleHeightWidthMax', '--resampleWidth', '--resampleHeight', '-r', '--rotate', '-f',
                         '--flip', '-c', '--cropToHeightWidth', '-p', '--padToHeightWidth', '-i', '--addIcon', '-e',
                         '--embedProfile', '-E', '--embedProfileIfNone', '-m', '--matchTo', '-M', '--matchToWithIntent',
                         '-o', '--out'})


def _in_place(head, argv):
    """Phase 46 (1.9.28): (words, beside, out) for a tool of IN_PLACE: the words that may name the files it changes
    (targets keeps those that exist), whether it writes beside them (their folder must be writable), and the output it
    names (sips --out, textutil -output), whose folder is written. ((), False, None) when the line only reads."""
    args = list(argv[1:])
    flags = [a for a in args if a.startswith('-')]
    short = [a[1:] for a in flags if re.fullmatch(r'-[A-Za-z0-9]+', a)]
    out = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ('--out', '-o', '-output')), None)
    if head in ('sed', 'gsed'):
        edits = any(re.fullmatch(r'-[A-Za-z]{0,3}[iI](?:\.[\w.~-]*)?', a) or a.startswith('--in-place') for a in flags)
        return (args, True, None) if edits else ((), False, None)
    if head == 'perl':
        edits = any(re.fullmatch(r'-[a-z0-9]{0,3}i(?:\.[\w.~-]*)?', a) for a in flags)
        return (args, True, None) if edits else ((), False, None)
    if head == 'sips':
        if not SIPS_WRITES & set(flags):
            return (), False, None
        return ((), False, out) if out else (args, False, None)
    if head == 'textutil':  # with -output only that file's folder is written (sec-w7 minor)
        return (() if out else args, True, out) if {'-convert', '-cat'} & set(flags) else ((), False, None)
    if head in ('gzip', 'gunzip', 'bzip2', 'bunzip2', 'xz', 'unxz'):
        reads = any(set(s) & set('clt') for s in short) or {'--stdout', '--to-stdout', '--list', '--test'} & set(flags)
        return ((), False, None) if reads else (args, True, None)
    if head == 'xattr':
        return (args, False, None) if any(set(s) & set('wdc') for s in short) else ((), False, None)
    return args, False, None  # truncate, chmod, chown, chflags, SetFile: the paths they name


def _find_roots(argv):
    """The path operands of a `find` (after its -H/-L/-P/-E/-X/-d/-s/-x options, up to the first expression word)."""
    words = list(argv[1:])
    while words and re.fullmatch(r'-[HLPEXdsx]+', words[0]):
        words = words[1:]
    roots = []
    for word in words:
        if word.startswith(('-', '(', '!', ')')):
            break
        roots.append(word)
    return roots or ['.']


def targets(parsed, cwd, home, home_edits=None):
    """(writes, deletes): the paths the plain parts visibly write or remove (redirections, tee, touch, mkdir, cp, ln,
    install and mv destinations; rm, rmdir, unlink, srm, trash, find -delete roots and mv sources). The sandbox still
    decides what actually happens; these let Pebble ask before a run instead of after a refusal. A list given as
    home_edits gets (tool, file) for each in-place writer whose file lies in the home folder itself (decide refuses it)."""
    writes, deletes = [], []
    here = cwd
    fed = []  # Phase 46 (1.9.28, sec-w7 2): the path operands of each `find` piped into the current part
    for part in parsed.parts:
        roots, fed = fed, (fed + (_find_roots(part.argv) if part.argv and Path(part.argv[0]).name == 'find' else [])
                           if part.piped else [])
        part_writes, part_deletes = [], []
        for op, target in part.redirects:
            if '>' in op and target and not (op.endswith('&') and (target.isdigit() or target == '-')) \
                    and not DEVICE_SINK.fullmatch(target):
                part_writes.append(target)
        if not part.argv:
            continue
        head = Path(part.argv[0]).name
        words = [a for a in part.argv[1:] if not a.startswith('-')]
        if head == 'cd':
            # Phase 46 (golden dev d-cd-01 phrasings): "cd ~/Downloads && mv receipt-* ../Documents/..." resolved
            # ../Documents from the starting folder, outside the home folder, and was refused
            moved = _resolve(words[0], here, home) if words else _resolve('~', here, home)
            here = moved if moved is not None and moved.is_dir() else here
            continue
        if head in ('tee', 'touch', 'mkdir'):
            part_writes += words
        elif head == 'zip' and words:
            part_writes.append(_folder_of(words[0]))  # zip writes a temporary file beside the archive, then renames
        elif head == 'unzip':
            target = next((part.argv[i + 1] for i, a in enumerate(part.argv[:-1]) if a == '-d'), None)
            part_writes.append(target or '.')
        elif head in ('tar', 'bsdtar'):
            flags = ''.join(a.lstrip('-') for a in part.argv[1:2] if not a.startswith('--')) + ''.join(
                a[1:] for a in part.argv[2:] if a.startswith('-') and not a.startswith('--') and len(a) <= 6)
            archive = next((part.argv[i + 1] for i, a in enumerate(part.argv[:-1])
                            if a in ('-f', '--file') or (a.startswith('-') and not a.startswith('--') and a.endswith('f'))
                            or (i == 1 and not a.startswith('-') and a.endswith('f'))), None)
            if 'c' in flags and archive:
                part_writes.append(_folder_of(archive))
            elif 'x' in flags:
                part_writes.append(next((part.argv[i + 1] for i, a in enumerate(part.argv[:-1]) if a == '-C'), '.'))
        elif head in ('cp', 'ln', 'install', 'rsync', 'ditto') and len(words) > 1:
            part_writes.append(words[-1])
            if head == 'rsync' and '--remove-source-files' in part.argv:
                part_deletes += words[:-1]  # Phase 46 (1.9.28): measured, refused until its sources' folder was writable
        elif head == 'mv' and len(words) > 1:
            part_writes.append(words[-1])
            part_deletes += words[:-1]
        elif head in DELETERS:
            part_deletes += words
        elif head == 'find' and '-delete' in part.argv:
            part_deletes.append(words[0] if words else '.')
        elif head in IN_PLACE:
            named, beside, out = _in_place(head, part.argv)
            for word in [w for w in named if w and not w.startswith('-')] + ([out] if out else []):
                folder = _unglob(word) != word
                path = _resolve(_unglob(word), here, home)
                if path is None or not path.exists() and not (word == out and path.parent.is_dir()):
                    continue  # a script, a mode, an attribute's name: only what exists (or an output's folder) counts
                if folder or path.is_dir():
                    placed = path
                else:
                    placed = path.parent if beside or word == out else path
                if placed != Path(os.path.realpath(home)):
                    writes.append(placed)
                elif home_edits is not None and placed != path:
                    home_edits.append((head, path))  # a file beside which it writes, in the never-given home folder
        elif head in ('split', 'csplit'):
            # Phase 46 (1.9.28, sec-w7 minor): the pieces go beside their prefix (split's last operand, csplit's -f),
            # in the starting folder by default
            taking = ('-a', '-b', '-l', '-n', '-p') if head == 'split' else ('-f', '-n')
            operands = [a for i, a in enumerate(part.argv[1:], 1) if not a.startswith('-')
                        and part.argv[i - 1] not in taking]
            prefix = (operands[1] if head == 'split' and len(operands) > 1 else
                      next((part.argv[i + 1] for i, a in enumerate(part.argv[:-1]) if a == '-f'), None))
            part_writes.append(_folder_of(prefix) if prefix else '.')
        if part.replace:
            # Phase 46 (1.9.28, sec-w7 1-2): a word holding this xargs's replacement string stands for what the pipe
            # gives it. It still counts as the path it reads as (1.9.27's ask), and in a delete every root of the find
            # piped into this xargs is a delete too, so no ask is lost to a second root or a decoy find
            part_deletes += [root for t in part_deletes if part.replace in t for root in roots]
        part_writes = [t for t in part_writes if not SCRATCH_VAR.match(t)]  # the call's own temporary folder
        writes += [p for p in (_resolve(_unglob(t), here, home) for t in part_writes) if p is not None]
        deletes += [p for p in (_resolve(_unglob(t), here, home) for t in part_deletes) if p is not None]
    return writes, deletes


def _folder_of(word):
    return word.rsplit('/', 1)[0] or '/' if '/' in word else '.'


def _unglob(word):
    """Phase 46: a glob names files the shell expands later; the grant goes to the folder that holds them. Granting the
    literal "~/Downloads/receipt-2026-10-*.pdf" let `mv` neither remove nor create a single real file (measured: every
    rename "Operation not permitted" after the person allowed it)."""
    stem = re.split(r'[*?\[]', word, maxsplit=1)[0]
    if stem == word:
        return word
    folder = stem.rsplit('/', 1)[0] if '/' in stem else '.'
    return folder or '/'



HIDDEN_WRITE_PATH = re.compile(r'(?:~|\$HOME|\$\{HOME\})(?:/[^\s;&|`\'"()\\]+)+/?|/Users/[^\s;&|`\'"()\\]+')
# What can write inside a line the policy cannot fully read: a writing command, an in-place edit, or a redirection
# that is not to a device sink (DEVICE_REDIRECT, removed first)
SHELL_WRITE_HINT = re.compile(
    r'\b(?:mv|cp|rm|rmdir|mkdir|touch|tee|ln|rsync|ditto|install|unzip|zip|tar|bsdtar|gzip|gunzip|bzip2|bunzip2|xz|'
    r'unxz|chmod|chown|chflags|xattr|trash|srm|unlink|truncate|dd|patch|split|csplit|sips|textutil|plutil|curl|wget|'
    r'git|defaults|osascript)\b|\b(?:sed|perl)\s+(?:-[a-zA-Z]+\s+)*-[a-zA-Z]*i|>')
DEVICE_REDIRECT = re.compile(r'[0-9]*>{1,2}\s*(?:/dev/(?:null|stdout|stderr|tty|fd/[0-9]{1,3})\b|&\s*[0-9-])')
# Phase 46 (1.9.28, R12 under sandbox-exec: `sed -i "" ... ~/Documents/notes.txt 2>/dev/null; echo updated` exited 0
# saying "updated" with nothing changed): a line that throws its errors away hides the sandbox's refusal too, so a
# writer whose target targets() cannot place asks first for the home folders the line names (quiet_writer)
DISCARDS_ERRORS = re.compile(r'(?:^|[\s;&|])(?:2|&)>{1,2}\s*/dev/null\b|2>&-')
QUIET_WRITERS = frozenset({'dd', 'patch', 'plutil'})


def quiet_writer(parsed, line):
    """Phase 46 (1.9.28, sec-w7 8): the line throws its errors away and runs a writer whose target targets() cannot
    place: dd, patch or plutil, or curl or wget saving a file (-o, -O, --output). The commands themselves decide, never
    a word elsewhere in the line (`cat ~/Documents/split-notes.txt 2>/dev/null` asked for write access)."""
    if not DISCARDS_ERRORS.search(line):
        return False
    for part in parsed.parts:
        head = Path(part.argv[0]).name if part.argv else ''
        if head in QUIET_WRITERS or head in ('curl', 'wget') and any(
                a in ('--output', '--output-document', '--remote-name') or re.fullmatch(r'-[A-Za-z]*[oO][A-Za-z]*', a)
                for a in part.argv[1:]):
            return True
    return False
# An interpreter's inline code that writes, moves or removes files, or runs commands of its own
CODE_WRITE_HINT = re.compile(
    r"""\bopen\s*\([^)]*,\s*['"][^'"]*[wax+]|\.write(?:lines)?\s*\(|\.(?:write_text|write_bytes|unlink|mkdir|touch|"""
    r"""rmdir|symlink_to|hardlink_to|rename|replace|chmod)\s*\(|\b(?:shutil|subprocess|FileUtils)\b|"""
    r"""\bos\.(?:rename|renames|replace|remove|removedirs|rmdir|unlink|makedirs|mkdir|symlink|link|chmod|chown|"""
    r"""truncate|system|popen|exec\w*|spawn\w*)\b|\bfs\.\w*(?:write|append|rename|unlink|rm|mkdir|copy|symlink|"""
    r"""truncate)\w*|\bFile\.(?:write|rename|delete|open)|\bsystem\s*\(|\bexec\s*\(|`""")


def hidden_writes(parsed, line, cwd, home):
    """Phase 46 (golden held-out companion_desktop: 2 false completions and 2 honest incompletes on Opus): a command
    whose writes the policy cannot see (a loop or other opaque line, find -exec/-execdir/-ok, xargs, an interpreter's
    inline code) ran without asking, so its writes outside the workbench were never granted and the sandbox refused
    them, while `find ... -exec mv` still exited 0. Such a command now asks for the home folders it names (for a glob or
    a file name, the folder holding it), so a grant opens what it will write."""
    # Phase 46 (1.9.27, the lead's review): only a command that can write asks. A read-only loop, find -exec ls or
    # python3 -c over home files asked for write access it never used ("It writes outside the workbench: ~/Documents").
    # A write this misses is still refused by the sandbox and reported (companion_shell's sandbox_refused hint).
    shell_writes = bool(SHELL_WRITE_HINT.search(DEVICE_REDIRECT.sub(' ', line)))
    hidden = bool(parsed.opaque) and shell_writes
    # Phase 46 (1.9.29): code an interpreter reads from a here-document writes what it names, as -c code does (asked as
    # when the heredoc made the line opaque)
    if any(doc.kind == 'code' for doc in parsed.heredocs):
        hidden = hidden or shell_writes or bool(CODE_WRITE_HINT.search(line))
    if quiet_writer(parsed, line):
        hidden = hidden or not any(targets(parsed, cwd, home))  # Phase 46 (1.9.28): DISCARDS_ERRORS
    for part in parsed.parts:
        head = Path(part.argv[0]).name if part.argv else ''
        if head == 'xargs' or head == 'find' and any(a in ('-exec', '-execdir', '-ok', '-okdir') for a in part.argv):
            hidden = hidden or shell_writes
        elif head in INTERPRETERS and any(a in ('-c', '-e', '--eval') for a in part.argv[1:3]):
            hidden = hidden or shell_writes or bool(CODE_WRITE_HINT.search(line))
    if not hidden:
        return []
    found = []
    for word in HIDDEN_WRITE_PATH.findall(line):
        word = re.sub(r'^\$\{?HOME\}?', '~', word)
        stem = word.split('*', 1)[0].split('?', 1)[0].split('[', 1)[0]
        if stem != word or re.search(r'\.[A-Za-z0-9]{1,8}$', stem.rstrip('/')):
            stem = stem.rsplit('/', 1)[0] or stem  # a glob or a file: the folder that holds it
        path = _resolve(stem.rstrip('/') or stem, cwd, home)
        if path is not None and path not in found:
            found.append(path)
    return found


def _grantable(path, home):
    """(path, gated entry) for a path the person may open for writing, or PolicyError."""
    return writable_folder(str(path), home)


# -- decisions ---------------------------------------------------------------------------------------------------

def _guard_findings(line):
    """AgentGuard's deterministic risky-command patterns (vendor/agentguard/validate/checks.py), Companion too."""
    from .guard import VENDOR
    import sys
    if VENDOR not in sys.path:
        sys.path.insert(0, VENDOR)
    from agentguard.core.enums import EventType
    from agentguard.core.events import AgentEvent
    from agentguard.validate.checks import risky_command
    event = AgentEvent(event=EventType.PRE_TOOL_USE, tool='Bash', arguments={'command': line})
    return [f.summary for f in risky_command(event)]


def heads(parsed):
    return [Path(p.argv[0]).name for p in parsed.parts if p.argv]


def needs_network(parsed):
    """Hint: the parts that usually reach the network."""
    found = []
    for part in parsed.parts:
        if not part.argv:
            continue
        head = Path(part.argv[0]).name
        sub = NETWORKED_SUB.get(head, ())
        if head in NETWORKED or sub is None or sub and len(part.argv) > 1 and part.argv[1] in sub:
            found.append(head)
    return found


def hosts_in(line):
    return sorted({m.group(1).lower().rstrip('.') for m in URL.finditer(line)})


def untrusted_fragments(texts):
    """Words worth tracking from untrusted text (web pages, search results, screen captures, connector data, files
    outside the workbench): URLs, host names, absolute or home paths and long tokens."""
    found = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for m in re.finditer(r'https?://[^\s"\'<>)]+|~?/[^\s"\'<>]{3,}|\b[a-z0-9-]+(\.[a-z0-9-]+)+\.[a-z]{2,}\b'
                             r'|\b[A-Za-z0-9_\-+=/]{24,}\b', text, re.I):
            found.add(m.group(0).rstrip('.,;:'))
    return found


def tainted(line, fragments):
    return sorted(f for f in fragments if len(f) >= 6 and f in line)[:8]


def decide(line, *, cwd, home=None, grants=None, hosts=(), writable=(), fragments=(), extra_protected=()):
    """The decision for one Shell call. `grants` = {'hosts': {host: scope}, 'folders': {path: scope}} (scope 'session'
    or 'always'); `hosts` and `writable` are what the call asks for; `fragments` come from untrusted_fragments."""
    home = Path(os.path.realpath(home or Path.home()))
    bench = Path(os.path.realpath(workbench(home)))
    grants = grants or {'hosts': {}, 'folders': {}}
    parsed = parse(line)
    decision = Decision('allow', parsed=parsed)
    # 1. Refusals.
    for part in parsed.parts:
        if not part.argv:
            continue
        head = Path(part.argv[0]).name
        if head in DENIED or head.startswith(DENIED_PREFIX):
            return Decision('deny', [f'{head} changes the system or other apps; run it yourself outside Pebble.'],
                            always=False, parsed=parsed)
        if head == 'defaults' and len(part.argv) > 1 and part.argv[1] in DEFAULTS_WRITE:
            return Decision('deny', ['Changing app or system settings with defaults is outside Pebble.'],
                            always=False, parsed=parsed)
        if head == 'dd' and any(a.startswith('of=/dev/') for a in part.argv):
            return Decision('deny', ['Writing to a disk device is outside Pebble.'], always=False, parsed=parsed)
        for word in [*part.argv[1:], *(t for _, t in part.redirects)]:
            if not word.startswith(('/', '~', '.')) and '/' not in word and not PROTECTED_NAME.search(word):
                continue
            path = _resolve(word, cwd, home)
            entry = path and protected_path(path, home, extra_protected)
            if entry:
                return Decision('deny', [f'{entry} is protected (keys, passwords, browser data, Messages, Mail and '
                                         'Pebble\'s own data are never read or written).'], always=False, parsed=parsed)
    if parsed.opaque:
        for word in re.findall(r'(?:^|[;&|(`\s])((?:/[\w.+-]+)*/?(?:' + '|'.join(sorted(map(re.escape, DENIED))) +
                               r'))(?=\s|$|[;&|)`])', line):
            return Decision('deny', [f'{Path(word).name} changes the system or other apps; run it yourself outside '
                                     'Pebble.'], always=False, parsed=parsed)
        if any(protected_path(_resolve(w, cwd, home), home, extra_protected)
               for w in re.findall(r'~?/[^\s;&|`\'")]+', line) if _resolve(w, cwd, home)):
            return Decision('deny', ['That path is protected.'], always=False, parsed=parsed)
    # Phase 46 (1.9.29): a here-document's body still never names a protected path (an opaque line was read whole above)
    for doc in parsed.heredocs:
        if any(protected_path(_resolve(w, cwd, home), home, extra_protected)
               for w in re.findall(r'~?/[^\s;&|`\'")]+', doc.body) if _resolve(w, cwd, home)):
            return Decision('deny', ['That path is protected.'], always=False, parsed=parsed)
    # 2. Hosts the call asks for.
    from .egress import host_name
    asked_hosts = []
    for host in hosts:
        try:
            name = host_name(host.strip().lower().rstrip('.') if isinstance(host, str) else host)
        except PolicyError as exc:
            return Decision('deny', [str(exc)], always=False, parsed=parsed)
        asked_hosts.append(name)
    for name in asked_hosts:
        if name not in grants.get('hosts', {}):
            decision.hosts.append(name)
    # 3. Writing outside the workbench: the folders the call names, and the paths it visibly writes. Each becomes a
    #    path to open for this call once the person agrees; gated paths ask every time.
    granted = [Path(p) for p in grants.get('folders', {})]
    home_edits = []
    writes, deletes = targets(parsed, cwd, home, home_edits)
    writes += [p for p in hidden_writes(parsed, line, cwd, home) if p not in writes]
    # Phase 46 (1.9.28, sec-w7 2 and minor): two writes no grant can open, refused with their reason. An in-place
    # writer's file in the home folder itself (`sed -i ... ~/.zshrc` writes a new file beside it, in the home folder,
    # was allowed and then refused by the sandbox), and an xargs that moves or deletes what `find ~` lists anywhere in
    # the home folder (it was refused as "never the home folder itself")
    if home_edits:
        tool, path = home_edits[0]
        return Decision('deny', [f'{tool} writes a new file beside {json.dumps(path.name[:80])}, in your home folder '
                                 'itself, and Pebble is never given the home folder: change that file with EditFile '
                                 '(Pebble asks you first), or work on a copy in ~/Pebble Workbench.'],
                        always=False, parsed=parsed)
    if home in deletes and any(part.replace for part in parsed.parts):
        return Decision('deny', ['Moving or deleting what a search of your whole home folder finds would need write '
                                 'access to all of it, which Pebble is never given. Search the folders that hold the '
                                 'files instead (for example find ~/Downloads ...), so Pebble can ask for those.'],
                        always=False, parsed=parsed)

    def want(path, gate_words=None):
        path, gated = _grantable(path, home)
        if inside(path, [bench]):
            return
        if gated or gate_words:
            decision.gates.append(gate_words or f'writing in {gated}')
            decision.always = False
        elif inside(path, granted):
            return
        if str(path) not in decision.folders:
            decision.folders.append(str(path))
    try:
        for value in writable:
            want(writable_folder(value, home)[0])
        for path in writes:
            want(path)
        # 4. Deleting or moving away anything outside the workbench asks every time (a gate no setting waives).
        for path in deletes:
            if not inside(path, [bench]):
                want(path, f'deleting or moving {path} outside the workbench')
    except PolicyError as exc:
        return Decision('deny', [str(exc)], always=False, parsed=parsed)
    granted_folders = granted + [Path(p) for p in decision.folders]
    if decision.folders and quiet_writer(parsed, line):
        decision.always = False  # Phase 46 (1.9.28, sec-w7 8): an ask that exists only to be safe is never "always"
    if parsed.opaque and granted_folders and not decision.gates:
        decision.gates.append('a command Pebble cannot fully read (' + ', '.join(parsed.opaque) +
                              ') while it may write outside the workbench')
        decision.always = False
    # 5. AgentGuard's irreversible patterns (Phase 46, 1.9.29: of the commands, not of the text a command reads as data).
    findings = _guard_findings(parsed.unread())
    if findings:
        decision.gates.extend(findings)
        decision.always = False
    # 6. Untrusted origins: a tainted command that reaches the network or writes outside the workbench asks.
    taint = tainted(line, fragments)
    if taint and (asked_hosts or granted_folders):
        decision.gates.append('it uses text from an untrusted source (' + ', '.join(taint[:3]) + ')')
        decision.always = False
    # 7. "Always" is never offered for inline interpreters or deletes (Phase 46, 1.9.29: nor for code in a heredoc).
    for part in parsed.parts:
        head = Path(part.argv[0]).name if part.argv else ''
        if head in INTERPRETERS and any(a in ('-c', '-e', '--eval', '-m') for a in part.argv[1:2]) or head in DELETERS:
            decision.always = False
    if any(doc.kind == 'code' for doc in parsed.heredocs):
        decision.always = False
    if decision.hosts or decision.folders or decision.gates:
        decision.action = 'ask'
        if decision.hosts:
            decision.reasons.append('It needs the network: ' + ', '.join(decision.hosts) + '.')
        if decision.folders:
            decision.reasons.append('It writes outside the workbench: ' + ', '.join(decision.folders) + '.')
        decision.reasons += [f'Pebble asks before {g}.' if g.startswith(('writing', 'deleting', 'a command'))
                             else f'Pebble asks because {g}.' if g.startswith('it uses') else f'AgentGuard: {g}.'
                             for g in decision.gates]
    return decision


def scrubbed_environment(environ):
    """The environment a Shell command gets: no keys, secrets, tokens or passwords; no Pebble internals."""
    secret = re.compile(r'(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH|SESSION|COOKIE)', re.I)
    return {k: v for k, v in environ.items()
            if not secret.search(k) and not k.startswith(('PEBBLE_', 'AGENTGUARD_', 'CLAUDE', 'ANTHROPIC', 'OPENAI'))
            and k not in ('SSH_AUTH_SOCK', 'GPG_AGENT_INFO', 'DYLD_INSERT_LIBRARIES', 'DYLD_LIBRARY_PATH',
                          'LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'NODE_OPTIONS')}


class BlockCounter:
    """Hand-back (D91, Claude Code's rule): after 3 blocked actions in a row or 20 in a run, Pebble stops and asks."""

    def __init__(self):
        self.row = 0
        self.total = 0

    def blocked(self):
        self.row += 1
        self.total += 1
        return self.row >= 3 or self.total >= 20

    def passed(self):
        self.row = 0
