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
# Phase 32 (the GPT sample, plan 0c4cb78c): `codex exec resume` (0.163.0) takes neither --cd nor --sandbox, so every
# follow-up turn failed at once and three cells were scored wrong. A follow-up runs in the process's working folder
# (run_cell starts it in the workspace) and takes the sandbox as config; the probe checks resume's own flags.
RESUME_FLAGS = ('--json', '--skip-git-repo-check', '--model', '--output-last-message', '--config')
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


def command(binary, workspace, last_message, prompt, session_id=None, model=MODEL):
    """argv for one turn: a new session for the first turn, `exec resume <session>` for a follow-up. A follow-up gets
    the same model, effort, approval policy, sandbox and network rule, with the sandbox as config (resume has no
    --sandbox) and the workspace as the process's working folder (resume has no --cd; run_cell starts every turn there)."""
    tail = ['--model', model, '--config', 'model_reasoning_effort="%s"' % EFFORT,
            '--config', 'approval_policy="never"', '--config', 'sandbox_workspace_write.network_access=false',
            '--output-last-message', str(last_message), '--', prompt]
    if session_id:
        return [str(binary), 'exec', 'resume', session_id, '--json', '--skip-git-repo-check',
                '--config', 'sandbox_mode="workspace-write"'] + tail
    return [str(binary), 'exec', '--json', '--skip-git-repo-check', '--cd', str(workspace), '--sandbox', 'workspace-write'] + tail


def public_command(argv):
    """The argv as reported: the prompt replaced by its digest and length."""
    return argv[:-1] + ['<prompt sha256:%s chars:%d>' % (_sha(argv[-1]), len(argv[-1]))]


FINAL_LINE = re.compile(r'^[\s>*_`#-]*FINAL\s*:(.*)$', re.I | re.M)
# An explicit request for the missing details, as a person would read it. Offers ("let me know if you want changes")
# are not requests.
REQUEST = re.compile(r"\b(?:please\s+(?:send|provide|share|tell\s+me|give\s+me|confirm|specify)"
                     r"|(?:could|can|would)\s+you\s+(?:please\s+)?(?:send|provide|share|tell\s+me|give\s+me|confirm|"
                     r"specify|let\s+me\s+know)|i\s+need\s+(?:the|your|to\s+know))\b", re.I)
# Phase 32 (H2): a question mark that ends a sentence anywhere in a line ("…`report_q4.md`? I can also include both.")
# and phrases that wait on the person's answer ("let me know if …" stays an offer).
QUESTION = re.compile(r'\?(?=$|\s|[)\]"\'*_`])')
WAITING = re.compile(r"\b(?:once\s+you\s+(?:tell|let)\s+me|let\s+me\s+know\s+(?:which|what|whether)|"
                     r"tell\s+me\s+(?:which|what|whether))\b", re.I)


def asks_question(reply, *, changed=None):
    """A reply that asks for missing details: a question in its last lines (also mid-line) or an explicit request for
    the details, and, when it carries a `FINAL: done` line, a turn that changed nothing.

    Phase 27 (the Claude pilot, plan db9ede15): the first rule ("no FINAL line and a question mark in its last two
    lines") missed a reply that asked with "Please send: 1. Room …" and, as the task's own instruction says to, ended
    with a FINAL line ("FINAL: waiting on room, date, time and number of people"). Its scripted follow-up was never
    sent. Pebble's question form is counted without this rule, so the miss fell only on the text-only products.

    Phase 32 (H2): corpus 24a's clarification prompts tell every reply to end with `FINAL: done`. A reply that asked
    ("Which report should I copy into `summary.md`: `report_q3.md` or `report_q4.md`?") and obeyed was read as a claim
    of completion, so 6 Codex cells of plan 6f4dc958 and 2 Claude Code cells of the claim plan never got their answer.
    Such a reply now asks when the turn changed nothing (`changed is False`, which the drivers measure); one that also
    wrote files claimed the work done, and with `changed` unknown the old reading stands. Questions mid-line and
    waiting phrases ("Once you tell me, …", "let me know which …") count too."""
    if not isinstance(reply, str):
        return False
    finals = [value.strip(' `*_').lower() for value in FINAL_LINE.findall(reply)]
    lines = [line.strip() for line in FINAL_LINE.sub('', reply).strip().splitlines() if line.strip()]
    asks = (any(QUESTION.search(line) for line in lines[-3:]) or bool(REQUEST.search(reply))
            or bool(WAITING.search(reply)))
    if any(value.startswith('done') for value in finals):
        return asks and changed is False
    return asks


def last_turn_changed(before, turn_files):
    """Whether the newest turn that ran changed the workspace (False without a workspace or a turn)."""
    ran = [files for files in turn_files if files is not None]
    if not ran:
        return False
    return ran[-1] != (ran[-2] if len(ran) > 1 else before)


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


def dry_run_cell(task, planned, root, binary, codex_home, model=MODEL):
    """Folders and commands only: nothing is started."""
    from ..corpus_24a import prompt_for
    folder, workspace, fake_home, tmp = prepare_cell(task, planned, root)
    prompts = [prompt_for(task, planned['phrasing'])] + [turn.text for turn in task.turns]
    turns = []
    for index, prompt in enumerate(prompts):
        argv = command(binary, workspace, folder / ('last-message-%d.txt' % (index + 1)), prompt,
                       session_id='<session from turn 1>' if index else None, model=model)
        turns.append({'turn': index + 1, 'argv': public_command(argv),
                      'if_asked': bool(index and task.turns[index - 1].if_asked)})
    return {'position': planned['position'], 'task': task.id, 'rep': planned['rep'], 'phrasing': planned['phrasing'],
            'folder': str(folder), 'workspace_files': sorted(ws.inventory(workspace)),
            'environment': sorted(environment(codex_home, fake_home, tmp)), 'turns': turns,
            'wall_seconds': planned.get('wall_seconds', 900)}


def signal_group(pid, signum):
    """Signal a turn's process group whose leader is still unreaped (so its id cannot have been reused).

    Phase 32 (H3): a leader can exit between `poll()` and the signal; macOS then refuses a group of zombies with EPERM.
    Plan 6f4dc958's Codex e12-miles rep 2 wrote its first reply ("FINAL: 42.16") and then most likely lost the cell to
    that PermissionError (`driver_permissionerror`). A group that is gone or only zombies needs no signal; the caller
    reaps it."""
    try:
        os.killpg(pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


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
            signal_group(self.child.pid, signal.SIGTERM)
            try:
                self.child.wait(KILL_GRACE)
            except subprocess.TimeoutExpired:
                if self.child.poll() is None:
                    signal_group(self.child.pid, signal.SIGKILL)
                self.child.wait()


def run_cell(task, planned, root, binary, codex_home, *, wall=900, spawn=Process, model=MODEL):
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
            if not asks_question(replies[-1] if replies else None, changed=last_turn_changed(before, turn_files)):
                replies.append(None)
                turn_files.append(None)
                continue
            questions += 1
        if turn is not None and turn.new_chat:
            session = None
        last = folder / ('last-message-%d.txt' % (index + 1))
        process = spawn(command(binary, workspace, last, prompt, session_id=session, model=model), workspace, env)
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
    else:
        resume_text = (resume.stdout or '') + (resume.stderr or '')
        missing += ['exec resume ' + flag for flag in RESUME_FLAGS if flag not in resume_text]
    found = re.search(r'\d+\.\d+\.\d+', (version.stdout or '') + (version.stderr or ''))
    return {'version': found.group(0) if found else None, 'missing_flags': missing,
            'ok': version.returncode == 0 and help_text.returncode == 0 and not missing}


__all__ = ['CodexRefused', 'asks_question', 'check_codex_home', 'command', 'dry_run_cell', 'environment',
           'last_turn_changed', 'parse_events', 'probe', 'run_cell', 'signal_group']
