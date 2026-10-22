"""Parent audit of restricted Claude development worker sessions.

Reads each worker's stream-json log and derives, from recorded tool calls rather
than the worker's own claims: the actual model, the offered tool set, Bash
commands, write targets, reads outside the checkout, permission denials and
the final result. It never prints reasoning or tool output content. The output
is metadata for the phase finalizer; a worker's completion message is not
verification.

Usage:
  python scripts/audit_dev_workers.py --dir .runtime/phase13-claude --owned owned.json --out metadata.json
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST = re.compile(r'^\.runtime/agent-env/bin/python -m pytest( |$)')
READ_ONLY = re.compile(r'^(ls|cat|head|tail|wc|grep|find)( |$)')
PRIVATE = re.compile(r'(phase10-selected-profile|profile\.json|^/Users/[^/]+/Library/|\.claude-[^/]+|vault)', re.I)
# Checked-in source files whose names match PRIVATE but hold no secret: reading the
# vault adapter's code is not reading the vault. Exact relative paths only.
# Source files whose paths contain a private-looking word but hold no secret (Phase 22: the vault helper's code and tests).
SOURCE_NOT_PRIVATE = frozenset({'backend/pebble/vault.py', 'backend/tests/test_vault_consolidated_22.py',
                                'backend/tests/test_build_vault_helper_22.py', 'scripts/build_vault_helper.py',
                                'src-tauri/helpers/pebble-vault/pebble_vault.c',
                                # Phase 24: t24-install's and t24-auth's new tests, and the pinned helper's public manifest.
                                'backend/tests/test_vault_helper_source_24.py', 'backend/tests/test_vault_fallback_24.py',
                                'src-tauri/resources/helpers/pebble-vault.json',
                                # Phase 24: t24-sec-recheck-arch's own reproduction test in its own scratch (fakes and a
                                # synthetic canary only; verified by the parent from the archived scratch).
                                '/private/tmp/pebble-p24-t24-sec-recheck-arch-scratch/repro/test_vault_confused_deputy.py'})
SEPARATORS = {'&&', '||', ';', '|', '&'}
READ_ONLY_PROGRAMS = {'ls', 'cat', 'head', 'tail', 'wc', 'grep', 'find', 'true', 'echo'}
FIND_EFFECTS = {'-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint', '-fprintf', '-fls'}


def segments(command):
    """Each simple command of a compound shell line, or None when it cannot be tokenized."""
    import shlex
    # Heredoc bodies are data, not commands; newlines separate commands like ';'.
    command = re.sub(r"<<-?\s*(['\"]?)(\w+)\1\n(?:.*?\n)?\2(?=\n|$)", '<< HEREDOC', command, flags=re.S)
    command = command.replace('\n', ' ; ')
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    parts, current = [], []
    for token in tokens:
        if token in SEPARATORS:
            parts.append(current)
            current = []
        else:
            current.append(token)
    parts.append(current)
    return [part for part in parts if part]


def writes_file(tokens):
    """True for an output redirection to a file. fd duplication (2>&1) and /dev/null are not writes."""
    for index, token in enumerate(tokens):
        target = tokens[index + 1] if index + 1 < len(tokens) else ''
        if token in ('>&', '<&') and (target.isdigit() or target == '-'):
            continue
        if token in ('>', '>>', '>|', '&>', '&>>', '>&') and target != '/dev/null':
            return True
    return False


SED_PRINT = re.compile(r'^\d+(,(\d+|\$))?p$')  # sed -n 1,60p FILE: prints a line range, nothing else.


def read_only_filter(part):
    """Text filters with no write or exec forms: `sed -n <range>p`, `cut`, `sort` without -o."""
    if part[0] == 'sed':
        args = part[1:]
        return '-n' in args and all(a == '-n' or SED_PRINT.match(a) or not a.startswith('-') for a in args) \
            and any(SED_PRINT.match(a) for a in args) and not any(a.startswith(('-i', '--in-place')) for a in args) \
            and all(SED_PRINT.match(a) or a == '-n' or '/' in a or '.' in a for a in args)
    if part[0] == 'cut':
        return True
    if part[0] == 'sort':
        return not any(a == '-o' or a.startswith(('-o', '--output')) for a in part[1:])
    return False


def classify(command, root=ROOT):
    """('pytest' | 'read_only' | 'other') per segment: a chained command is judged by every part."""
    parts = segments(command)
    if parts is None:
        return ['other']
    kinds = []
    for part in parts:
        if writes_file(part):
            kinds.append('other')
        elif part[:3] == ['.runtime/agent-env/bin/python', '-m', 'pytest']:
            kinds.append('pytest')
        elif part[0] == 'cd' and (len(part) == 1 or len(part) == 2 and inside(part[1], root)):
            kinds.append('read_only')
        elif read_only_filter(part):
            kinds.append('read_only')
        elif part[0] in READ_ONLY_PROGRAMS and not (part[0] == 'find' and FIND_EFFECTS & set(part)):
            kinds.append('read_only')
        else:
            kinds.append('other')
    return kinds
# The CLI's own background-task output for the worker's long-running commands.
OWN_OUTPUT = re.compile(r'^/private/tmp/claude-\d+/[^/]+/[0-9a-f-]+/tasks/[A-Za-z0-9]+\.output$')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(path, root=ROOT):
    try:
        Path(path).resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def relative(path, root=ROOT):
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return str(path)


def session_root(stream, roots):
    """The checkout a session ran in: its recorded init cwd when that is a declared root; the main
    checkout when the stream records no cwd (older streams); None for an undeclared cwd."""
    for line in Path(stream).read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') == 'system' and event.get('subtype') == 'init':
            cwd = event.get('cwd')
            if cwd is None:
                return ROOT
            if isinstance(cwd, str) and Path(cwd).resolve() in roots:
                return Path(cwd).resolve()
            return None
    return ROOT


def audit(stream, owned, roots=(ROOT,)):
    init, result, calls, denials, errored = None, None, [], [], set()
    models, model_counts = set(), {}
    for line in Path(stream).read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') == 'system' and event.get('subtype') == 'init' and init is None:
            init = event
        elif event.get('type') == 'assistant':
            message = event.get('message') or {}
            if message.get('model'):
                models.add(message['model'])
                model_counts[message['model']] = model_counts.get(message['model'], 0) + 1
            for item in message.get('content') or []:
                if item.get('type') == 'tool_use':
                    calls.append((item.get('name'), item.get('input') or {}, item.get('id')))
        elif event.get('type') == 'user':
            # Phase 19: a file tool whose result is an error read or wrote nothing (for example the CLI's --restricted
            # refusal of a path outside the checkout), so it is not counted as performed. Bash still counts: a failing
            # command ran.
            for item in (event.get('message') or {}).get('content') or []:
                if isinstance(item, dict) and item.get('type') == 'tool_result' and item.get('is_error') is True:
                    errored.add(item.get('tool_use_id'))
        elif event.get('type') == 'result':
            result = event
            denials += event.get('permission_denials') or []  # every result record (a session can have several)
    denied_ids = {d.get('tool_use_id') for d in denials}
    # Isolated copies are declared roots (--allowed-root); paths are relative to the session's own checkout.
    root = session_root(stream, {Path(r).resolve() for r in roots})
    checkout = [] if root is not None else ['cwd:' + str((init or {}).get('cwd'))]
    root = root or ROOT
    inside_root = lambda path: inside(path, root)
    relative_root = lambda path: relative(path, root)
    performed = [(n, i) for n, i, ident in calls if ident not in denied_ids
                 and not (ident in errored and n in ('Read', 'Glob', 'Grep', 'Edit', 'Write'))]
    attempted = [(n, i) for n, i, ident in calls]
    writes = sorted({relative_root(i.get('file_path', '')) for n, i in performed if n in ('Edit', 'Write')})
    unowned = [w for w in writes if w not in owned]
    reads = [i.get('file_path') or i.get('path') or '' for n, i in performed if n in ('Read', 'Glob', 'Grep')]
    tried = [i.get('file_path') or i.get('path') or '' for n, i in attempted if n in ('Read', 'Glob', 'Grep')]
    outside = sorted({str(r) for r in reads if r and not inside_root(r) and not OWN_OUTPUT.match(str(r))})
    own_output = sorted({str(r) for r in reads if r and OWN_OUTPUT.match(str(r))})
    denied_outside = sorted({str(r) for r in tried if r and not inside_root(r)} - set(outside) - set(own_output))
    private = sorted({relative_root(r) for r in reads if r and PRIVATE.search(str(r)) and relative_root(r) not in SOURCE_NOT_PRIVATE})
    bash = [i.get('command', '') for n, i in performed if n == 'Bash']
    kinds = {c: classify(c, root) for c in bash}
    # A command counts as pytest only when every segment is pytest; anything chained is reported.
    other_bash = [c[:160] for c in bash if set(kinds[c]) != {'pytest'}]
    denied = [{'tool': d.get('tool_name'), 'input': str(d.get('tool_input'))[:160]} for d in denials]
    return {
        'session_id': (init or {}).get('session_id'),
        'checkout': str(root),
        'actual_model': (init or {}).get('model'),
        'message_models': sorted(models),
        # Per-model assistant message counts: a CLI fallback model or its '<synthetic>' notices are visible here.
        'message_model_counts': dict(sorted(model_counts.items())),
        'tools': sorted((init or {}).get('tools') or []),
        'mcp_servers': (init or {}).get('mcp_servers'),
        'cli_version': (init or {}).get('claude_code_version'),
        'completed': bool(result) and result.get('subtype') == 'success' and result.get('terminal_reason') == 'completed',
        'is_error': (result or {}).get('is_error'),
        'turns': (result or {}).get('num_turns'),
        'cost_usd': round((result or {}).get('total_cost_usd') or 0, 2),
        'tool_calls': len(calls),
        'reads': len(reads),
        'reads_outside_checkout_performed': outside,
        'reads_of_own_cli_task_output': own_output,
        'reads_outside_checkout_denied': denied_outside,
        'private_reads_performed': private,
        'writes': writes,
        'unowned_writes': unowned,
        'pytest_commands': sum(1 for c in bash if set(kinds[c]) == {'pytest'}),
        'non_pytest_bash': other_bash,
        'non_pytest_bash_read_only': all('other' not in kinds[c] for c in bash),
        # Exact commands outside the pytest/read-only rule, for a declared scope exception (sha256 of the full text).
        'non_read_only_bash': [{'sha256': hashlib.sha256(c.encode()).hexdigest(), 'command': c[:160]}
                               for c in bash if 'other' in kinds[c]],
        'permission_denials': denied,
        'scope_violations': unowned + private + outside + checkout,
        'stream_sha256': sha(stream),
    }


def worker_files(folder, worker, streams_only=False):
    """This worker's own files only. A plain prefix glob also matched another worker whose name extends this one
    (Phase 20: d20-install / d20-installed, d20-faults / d20-faults2), so names are matched exactly: <worker>-stream.jsonl,
    <worker>-followup<N>-stream.jsonl and, for artifacts, the same with -stderr.log plus <worker>-task.md, -account,
    -session-id, -followup-<N>.md and governor resume notes."""
    w = re.escape(worker)
    stream = re.compile(w + r'(-followup\d+)?-stream\.jsonl')
    other = re.compile(w + r'(-followup\d+)?-stderr\.log|' + w + r'-(task\.md|account|session-id)|'
                       + w + r'-followup-\d+\.md|' + w + r'-governor-resume-\d+\.md|' + w + r'-resume-(next|used-\d+)\.md')
    found = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and (stream.fullmatch(path.name) or (not streams_only and other.fullmatch(path.name))):
            found.append(path)
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dir', required=True, type=Path)
    parser.add_argument('--owned', required=True, type=Path, help='JSON {worker: [owned relative paths]}')
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--allowed-root', action='append', default=[], type=Path,
                        help='An isolated copy a session may run in (its init cwd); the main checkout is always allowed')
    args = parser.parse_args()
    roots = (ROOT, *args.allowed_root)
    owned = json.loads(args.owned.read_text())
    tracks = []
    for worker, paths in sorted(owned.items()):
        sessions = []
        for stream in worker_files(args.dir, worker, streams_only=True):
            sessions.append({'stream': relative(stream), **audit(stream, set(paths), roots)})
        artifacts = {relative(p): sha(p) for p in worker_files(args.dir, worker)}
        tracks.append({'track': worker, 'owned': sorted(paths), 'sessions': sessions, 'artifacts_sha256': artifacts,
                       'actual_model': sorted({s['actual_model'] for s in sessions}),
                       'completed': bool(sessions) and all(s['completed'] for s in sessions),
                       'scope_violations': sorted({v for s in sessions for v in s['scope_violations']})})
    if args.out.exists():
        raise SystemExit('Refusing to overwrite existing audit output.')
    descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(json.dumps(tracks, indent=2) + '\n')
    print(json.dumps({t['track']: {'sessions': len(t['sessions']), 'completed': t['completed'],
                                    'violations': t['scope_violations']} for t in tracks}))


if __name__ == '__main__':
    main()
