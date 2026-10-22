"""Codex CLI cells for the Pebble-against-Codex plan (docs/PEBBLE_V1_INSPECTION.md §7.2).

Codex runs non-interactively (`codex exec --json`) in the cell's throwaway workspace, with `--sandbox workspace-write`,
no network for its tools, an approval policy that never escalates, the same model and effort (gpt-6-luna, medium),
and its own sign-in in a scratch CODEX_HOME that the parent prepared with the person (D13). HOME is a scratch folder
too, so no personal Codex configuration or AGENTS.md is read. Follow-up turns resume the same session with fixed text.

The dry run builds the cell folders and the exact commands and starts nothing. The live runner is reached only from
scripts/bench_codex.py run, after the plan is registered and stamped with the person's go-ahead. This module never
reads auth.json: it checks only that the file exists.
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from ..corpus_24a.graders import Output
from . import workspace as ws

MODEL, EFFORT = 'gpt-6-luna', 'medium'
REQUIRED_FLAGS = ('--json', '--skip-git-repo-check', '--cd', '--sandbox', '--model', '--output-last-message', '--config')
ENV_KEYS = ('PATH', 'HOME', 'CODEX_HOME', 'TMPDIR', 'LANG', 'USER', 'LOGNAME')
PATH = '/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin'
AUTH_TEXT = re.compile(r'\b(401|403|unauthori[sz]ed|not logged in|login required|sign in again|refresh token)\b', re.I)
PROVIDER_TEXT = re.compile(r'\b(429|5\d\d|rate limit|stream disconnected|connection (?:reset|refused)|timed out)\b', re.I)
KILL_GRACE = 5.0


class CodexRefused(RuntimeError):
    pass


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def check_codex_home(path, user_home=None):
    """A scratch CODEX_HOME holding only Codex's own sign-in: never ~/.codex, no AGENTS.md, no config.toml."""
    path = Path(path).expanduser()
    if not path.is_dir() or path.is_symlink():
        raise CodexRefused('codex_home_missing')
    resolved = path.resolve()
    real = (Path(user_home) if user_home else Path(os.path.expanduser('~'))).resolve() / '.codex'
    if resolved == real or resolved.is_relative_to(real):
        raise CodexRefused('codex_home_is_personal')
    info = os.lstat(resolved)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CodexRefused('codex_home_not_private')
    for name in ('AGENTS.md', 'AGENTS.override.md', 'config.toml'):
        if (resolved / name).exists():
            raise CodexRefused('codex_home_has_' + name.lower().replace('.', '_'))
    if not (resolved / 'auth.json').is_file():
        raise CodexRefused('codex_not_signed_in')  # existence only; the file is never opened here
    return resolved


def environment(codex_home, fake_home, tmpdir):
    """The whole child environment: no API key, proxy or personal setting is inherited."""
    return {'PATH': PATH, 'HOME': str(fake_home), 'CODEX_HOME': str(codex_home), 'TMPDIR': str(tmpdir),
            'LANG': 'en_US.UTF-8', 'USER': os.environ.get('USER', 'bench'), 'LOGNAME': os.environ.get('USER', 'bench')}


def command(binary, workspace, last_message, prompt, session_id=None):
    """argv for one turn: a new session for the first turn, `exec resume <session>` for a follow-up."""
    head = [str(binary), 'exec'] + (['resume', session_id] if session_id else [])
    return head + ['--json', '--skip-git-repo-check', '--cd', str(workspace), '--sandbox', 'workspace-write',
                   '--model', MODEL, '--config', 'model_reasoning_effort="%s"' % EFFORT,
                   '--config', 'approval_policy="never"', '--config', 'sandbox_workspace_write.network_access=false',
                   '--output-last-message', str(last_message), '--', prompt]


def public_command(argv):
    """The argv as reported: the prompt replaced by its digest and length."""
    return argv[:-1] + ['<prompt sha256:%s chars:%d>' % (_sha(argv[-1]), len(argv[-1]))]


def asks_question(reply):
    """A reply that asks for missing details: no FINAL line and a question mark in its last two lines."""
    if not isinstance(reply, str) or re.search(r'^[\s>*_`#-]*FINAL\s*:', reply, re.I | re.M):
        return False
    lines = [line.strip() for line in reply.strip().splitlines() if line.strip()]
    return any(line.endswith('?') for line in lines[-2:])


def parse_events(lines):
    """Session id, usage, last agent message, escalations and errors from `codex exec --json` lines."""
    session, usage, message, escalations, errors, commands = None, None, None, 0, [], 0
    for raw in lines:
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get('type') or (event.get('msg') or {}).get('type') or '')
        session = session or event.get('thread_id') or event.get('session_id') or (event.get('msg') or {}).get('session_id')
        found = event.get('usage') or (event.get('msg') or {}).get('usage')
        if isinstance(found, dict):
            usage = usage or {'input_tokens': 0, 'cached_tokens': 0, 'output_tokens': 0}
            for key, source in (('input_tokens', 'input_tokens'), ('cached_tokens', 'cached_input_tokens'),
                                ('output_tokens', 'output_tokens')):
                value = found.get(source)
                usage[key] = usage[key] + value if isinstance(usage[key], int) and type(value) is int else None
        item = event.get('item') if isinstance(event.get('item'), dict) else {}
        if item.get('type') in ('agent_message', 'assistant_message') and isinstance(item.get('text'), str):
            message = item['text']
        if item.get('type') == 'command_execution' and kind.endswith('completed'):
            commands += 1
        if 'approval' in kind or 'approval' in str(item.get('type', '')):
            escalations += 1
        if kind in ('error', 'turn.failed'):
            errors.append(str(event.get('message') or event.get('error') or kind)[:300])
    return {'session': session if isinstance(session, str) else None, 'usage': usage, 'message': message,
            'escalations': escalations, 'errors': errors, 'commands': commands}


def failure_kind(text):
    if AUTH_TEXT.search(text or ''):
        return 'auth'
    if PROVIDER_TEXT.search(text or ''):
        return 'provider'
    return None


def prepare_cell(task, planned, root):
    """The cell's folders and fixture copy; returns (cell folder, workspace, fake home, tmp)."""
    folder = ws.cell_dir(root, 'codex', task.id, planned['rep'])
    workspace = ws.materialize(folder / 'workspace', task.files)
    fake_home, tmp = folder / 'home', folder / 'tmp'
    fake_home.mkdir(mode=0o700)
    tmp.mkdir(mode=0o700)
    return folder, workspace, fake_home, tmp


def dry_run_cell(task, planned, root, binary, codex_home):
    """Folders and commands only: nothing is started."""
    from ..corpus_24a import prompt_for
    folder, workspace, fake_home, tmp = prepare_cell(task, planned, root)
    prompts = [prompt_for(task, planned['phrasing'])] + [turn.text for turn in task.turns]
    turns = []
    for index, prompt in enumerate(prompts):
        argv = command(binary, workspace, folder / ('last-message-%d.txt' % (index + 1)), prompt,
                       session_id='<session from turn 1>' if index else None)
        turns.append({'turn': index + 1, 'argv': public_command(argv),
                      'if_asked': bool(index and task.turns[index - 1].if_asked)})
    return {'position': planned['position'], 'task': task.id, 'rep': planned['rep'], 'phrasing': planned['phrasing'],
            'folder': str(folder), 'workspace_files': sorted(ws.inventory(workspace)),
            'environment': sorted(environment(codex_home, fake_home, tmp)), 'turns': turns,
            'wall_seconds': planned.get('wall_seconds', 900)}


class Process:
    """One Codex turn as a child process in its own session; killed only while its leader is unreaped."""

    def __init__(self, argv, cwd, env):
        self.started = time.monotonic()
        self.child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, start_new_session=True, text=True)

    def lines(self, deadline):
        import selectors
        selector = selectors.DefaultSelector()
        selector.register(self.child.stdout, selectors.EVENT_READ)
        first, lines = None, []
        while time.monotonic() < deadline:
            if not selector.select(timeout=min(1.0, max(0.0, deadline - time.monotonic()))):
                if self.child.poll() is not None:
                    break
                continue
            line = self.child.stdout.readline()
            if not line:
                break
            first = first if first is not None else time.monotonic() - self.started
            lines.append(line)
        selector.close()
        return lines, first

    def stop(self):
        if self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGTERM)
            try:
                self.child.wait(KILL_GRACE)
            except subprocess.TimeoutExpired:
                if self.child.poll() is None:
                    os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait()


def run_cell(task, planned, root, binary, codex_home, *, wall=900, spawn=Process):
    """One live cell (all its turns). Returns (Output, metrics) for the graders and the results cell."""
    from ..corpus_24a import prompt_for
    folder, workspace, fake_home, tmp = prepare_cell(task, planned, root)
    env = environment(codex_home, fake_home, tmp)
    before = ws.inventory(workspace)
    started = time.monotonic()
    deadline = started + wall
    replies, turn_files, session, usage, escalations, first, state, failure, questions = [], [], None, None, 0, None, 'completed', None, 0
    texts = [(prompt_for(task, planned['phrasing']), None)] + [(turn.text, turn) for turn in task.turns]
    for index, (prompt, turn) in enumerate(texts):
        if turn is not None and turn.if_asked:
            if not asks_question(replies[-1] if replies else None):
                replies.append(None)
                turn_files.append(None)
                continue
            questions += 1
        if turn is not None and turn.new_chat:
            session = None
        last = folder / ('last-message-%d.txt' % (index + 1))
        process = spawn(command(binary, workspace, last, prompt, session_id=session), workspace, env)
        lines, first_line = process.lines(deadline)
        timed_out = process.child.poll() is None
        process.stop()
        stderr = process.child.stderr.read() if process.child.stderr else ''
        parsed = parse_events(lines)
        first = first if first is not None else (None if first_line is None else round(first_line, 3))
        session = parsed['session'] or session
        if parsed['usage']:
            usage = parsed['usage'] if usage is None else {k: (usage[k] + parsed['usage'][k]
                                                              if isinstance(usage[k], int) and isinstance(parsed['usage'][k], int)
                                                              else None) for k in usage}
        escalations += parsed['escalations']
        reply = last.read_text(encoding='utf-8', errors='replace') if last.is_file() else parsed['message']
        replies.append(reply)
        turn_files.append(ws.inventory(workspace))
        if timed_out:
            state = 'timeout'
            break
        if process.child.returncode != 0 or not reply:
            state, failure = 'failed', failure_kind(stderr + ' '.join(parsed['errors']))
            break
    out = Output(replies=replies, state=state, before=before, after=ws.inventory(workspace), turn_files=turn_files,
                 questions=questions, children=None, applied=None, failure=failure)
    metrics = {'review': {'escalations': escalations}, 'requests': None, 'usage': usage,
               'wall': round(time.monotonic() - started, 3), 'first': first,
               'error': ('codex_' + failure) if failure else None}
    return out, metrics


def probe(binary, run=subprocess.run):
    """Codex's version and whether `codex exec` offers every flag this driver uses (no network, no model)."""
    env = {'PATH': PATH, 'HOME': '/private/tmp', 'LANG': 'en_US.UTF-8'}
    version = run([str(binary), '--version'], capture_output=True, text=True, timeout=30, env=env)
    help_text = run([str(binary), 'exec', '--help'], capture_output=True, text=True, timeout=30, env=env)
    resume = run([str(binary), 'exec', 'resume', '--help'], capture_output=True, text=True, timeout=30, env=env)
    text = (help_text.stdout or '') + (help_text.stderr or '')
    missing = [flag for flag in REQUIRED_FLAGS if flag not in text]
    if resume.returncode != 0:
        missing.append('exec resume')
    found = re.search(r'\d+\.\d+\.\d+', (version.stdout or '') + (version.stderr or ''))
    return {'version': found.group(0) if found else None, 'missing_flags': missing,
            'ok': version.returncode == 0 and help_text.returncode == 0 and not missing}


__all__ = ['CodexRefused', 'asks_question', 'check_codex_home', 'command', 'dry_run_cell', 'environment',
           'parse_events', 'probe', 'run_cell']
