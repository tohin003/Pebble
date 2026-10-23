"""Claude Code CLI cells for the Pebble-against-Claude-Code plan (Phase 27; docs/PEBBLE_V1_BENCHMARKS.md §5b).

Claude Code runs non-interactively (`claude -p --output-format stream-json`) in the cell's throwaway workspace, on the
plan's Claude model at medium effort, with its sub-agents pinned to that model too (CLAUDE_CODE_SUBAGENT_MODEL). Its
configuration folder is either a scratch CLAUDE_CONFIG_DIR the person signed in (no CLAUDE.md, hooks, plugins, skills
or MCP servers in it), or, with ``existing_login`` (D25), the person's own signed-in folder, used as it is and never
edited. Either way the isolation is per process: only the project settings source is read (the workspace has none),
`--strict-mcp-config` with an empty server list, and HOME is a scratch folder whose one link into the real home is
Library/Keychains, where Claude Code keeps the login, so no shell rc file, git config or other dotfile reaches a cell.
Every turn's init event is checked (isolation_breaches): an MCP server, a plugin that is not built in, a CLAUDE.md, a
personal skill, agent or command, a web tool or another model stops the run. Follow-up turns resume the same session
with fixed text.

Autonomy and confinement match the Codex cells: Claude Code never asks (`--permission-mode bypassPermissions`, as
Codex's approval policy "never"), web tools are off (`--disallowedTools WebFetch WebSearch`, as Codex's tool network
off), and the whole process runs under a macOS Seatbelt profile that refuses every file write under /Users except the
configuration folder and Library/Keychains (so a refreshed login is saved, never lost), so the task's writes land only
in the cell folder and its temp folder.

Every assistant message's model is recorded: a cell where any reply came from another model (the CLI can fall back
near plan limits) is reported as `model_drift` and never counted as the plan's model; models that appear only in the
usage table (Claude Code's small background helpers) are reported as helper models. A subscription's quota windows,
as the stream reports them (rate_limit_event), pace the run: quota_wait says how long to pause before the next cell.
This module never reads credentials: the sign-in check runs `claude auth status`, which reports whether a login exists.
"""

import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from ..corpus_24a.graders import Output
from . import workspace as ws
from .codex_cell import KILL_GRACE, asks_question, last_turn_changed, signal_group

EFFORT = 'medium'
REQUIRED_FLAGS = ('--print', '--output-format', '--model', '--permission-mode', '--resume', '--strict-mcp-config',
                  '--mcp-config', '--setting-sources', '--disallowedTools', '--effort', '--verbose')
SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
AUTH_TEXT = re.compile(r'\b(401|403|unauthori[sz]ed|not logged in|login required|invalid api key|oauth)\b', re.I)
PROVIDER_TEXT = re.compile(r'\b(429|5\d\d|rate.?limit|overloaded|usage limit|connection (?:reset|refused)|timed out)\b',
                           re.I)
QUOTA_TEXT = re.compile(r'(usage limit|limit reached|hit your limit|limit will reset|out of (?:usage|credits)|'
                        r'weekly limit|(?:five|5).?hour limit)', re.I)
# A cell starts only while every quota window Claude Code reports is below PAUSE_AT; a longer wait than MAX_PAUSE
# stops the run instead (its remaining cells are recorded not_run with error quota_stop).
PAUSE_AT, MAX_PAUSE = 0.80, 6 * 3600
PREFLIGHT_PROMPT = 'Reply with exactly: OK'
INIT_KEYS = ('model', 'mcp_servers', 'plugins', 'skills', 'agents', 'slash_commands', 'tools', 'memory_paths')
# The project folders a benchmark turn leaves in the configuration folder (projects/<workspace slug>), and nothing else.
RESIDUE = re.compile(r'-private-tmp-pebble-(?:bench|claude-probe)[A-Za-z0-9-]*')
# Folders and files that would carry personal configuration into a scratch Claude Code configuration.
PERSONAL = ('CLAUDE.md', 'CLAUDE.local.md', 'plugins', 'skills', 'agents', 'commands', 'hooks', 'output-styles')
SETTINGS_KEYS_REFUSED = ('hooks', 'enabledPlugins', 'mcpServers', 'permissions', 'env', 'apiKeyHelper',
                         'statusLine', 'outputStyle', 'extraKnownMarketplaces')


class ClaudeRefused(RuntimeError):
    pass


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def check_claude_config(path, user_home=None, existing_login=False):
    """The CLAUDE_CONFIG_DIR the cells use. By default a scratch folder: private, not a personal ~/.claude* folder, and
    holding no personal configuration. With ``existing_login`` (D25) the person's own signed-in folder, owned by this
    user: it is used as it is and never edited, and what it holds stays out through the per-process flags, checked on
    every turn (isolation_breaches)."""
    path = Path(path).expanduser()
    if not path.is_dir() or path.is_symlink():
        raise ClaudeRefused('claude_config_missing')
    resolved = path.resolve()
    if existing_login:
        if os.lstat(resolved).st_uid != os.getuid():
            raise ClaudeRefused('claude_config_not_owned')
        return resolved
    home =(Path(user_home) if user_home else Path(os.path.expanduser('~'))).resolve()
    for personal in home.glob('.claude*'):
        personal = personal.resolve()
        if resolved == personal or resolved.is_relative_to(personal):
            raise ClaudeRefused('claude_config_is_personal')
    info = os.lstat(resolved)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ClaudeRefused('claude_config_not_private')
    for name in PERSONAL:
        candidate = resolved / name
        if candidate.is_file() or (candidate.is_dir() and any(candidate.iterdir())):
            raise ClaudeRefused('claude_config_has_' + name.lower().replace('.', '_').replace('-', '_'))
    settings = resolved / 'settings.json'
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ClaudeRefused('claude_config_settings_unreadable') from None
        if not isinstance(data, dict) or any(key in data for key in SETTINGS_KEYS_REFUSED):
            raise ClaudeRefused('claude_config_settings_personal')
    return resolved


def personal_names(config_dir):
    """The names of the person's own skills, agents and commands in a configuration folder (names only: no file is
    read), so a turn that loaded one of them is caught."""
    config, names = Path(config_dir), set()
    if (config / 'skills').is_dir():
        names.update(found.parent.name for found in (config / 'skills').rglob('SKILL.md'))
    for kind in ('agents', 'commands'):
        if (config / kind).is_dir():
            names.update(found.stem for found in (config / kind).rglob('*.md'))
    return frozenset(names)


def isolation_breaches(init, model, personal=frozenset()):
    """What a turn's init event shows loaded beyond the product's own defaults; empty when the isolation held."""
    if not isinstance(init, dict):
        return ['init_missing']
    breaches = []
    if init.get('mcp_servers'):
        breaches.append('mcp_servers')
    if any(not isinstance(p, dict) or p.get('path') != 'builtin' for p in init.get('plugins') or ()):
        breaches.append('plugins')
    memory = init.get('memory_paths')
    if (isinstance(memory, dict) and set(memory) - {'auto'}) or (memory and not isinstance(memory, dict)):
        breaches.append('memory_files')
    loaded = {n for key in ('skills', 'agents', 'slash_commands') for n in (init.get(key) or ()) if isinstance(n, str)}
    if loaded & set(personal):
        breaches.append('personal_skills')
    if {'WebFetch', 'WebSearch'} & set(init.get('tools') or ()):
        breaches.append('web_tools')
    if init.get('model') != model:
        breaches.append('model')
    return breaches


def account():
    """This user's account name (from the account database, not $USER): Claude Code finds its Keychain login by it."""
    return pwd.getpwuid(os.getuid()).pw_name


def keychains():
    """The person's Keychain folder (from the account database, not $HOME): the one part of the real home a cell's
    scratch HOME links to, because Claude Code keeps its login there."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / 'Library' / 'Keychains'


def scratch_home(home):
    """A private scratch HOME whose only link into the real home is Library/Keychains."""
    home = Path(home)
    home.mkdir(mode=0o700)
    (home / 'Library').mkdir(mode=0o700)
    (home / 'Library' / 'Keychains').symlink_to(keychains())
    return home


def sandbox_profile(config_dir):
    """Seatbelt: everything as usual except file writes under /Users, which are refused outside the configuration
    folder and Library/Keychains (where a refreshed login is saved)."""
    allowed = (Path(config_dir).resolve(), keychains().resolve())
    return ('(version 1)(allow default)(deny file-write* (subpath "/Users"))'
            + ''.join('(allow file-write* (subpath %s))' % json.dumps(str(path)) for path in allowed))


def environment(config_dir, fake_home, tmpdir, claude_bin, model=None):
    """The whole child environment: no API key, proxy or personal setting is inherited; sub-agents on the plan model."""
    node_dir = str(Path(claude_bin).resolve().parent)
    env = {'PATH': node_dir + ':' + SYSTEM_PATH, 'HOME': str(fake_home), 'CLAUDE_CONFIG_DIR': str(config_dir),
           'TMPDIR': str(tmpdir) + '/', 'LANG': 'en_US.UTF-8', 'USER': account(), 'LOGNAME': account(),
           'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_DISABLE_TELEMETRY': '1',
           'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1', 'DISABLE_BUG_COMMAND': '1'}
    if model:
        env['CLAUDE_CODE_SUBAGENT_MODEL'] = model
    return env


def command(binary, model, prompt, config_dir, session_id=None, sandbox='/usr/bin/sandbox-exec'):
    """argv for one turn under the Seatbelt profile: a new session for the first turn, `--resume <session>` after."""
    claude = [str(binary), '-p', '--output-format', 'stream-json', '--verbose', '--model', model, '--effort', EFFORT,
              '--permission-mode', 'bypassPermissions', '--setting-sources', 'project', '--strict-mcp-config',
              '--mcp-config', '{"mcpServers":{}}', '--disallowedTools', 'WebFetch', 'WebSearch']
    if session_id:
        claude += ['--resume', session_id]
    return [sandbox, '-p', sandbox_profile(config_dir)] + claude + ['--', prompt]


def public_command(argv):
    """The argv as reported: the prompt replaced by its digest and length."""
    return argv[:-1] + ['<prompt sha256:%s chars:%d>' % (_sha(argv[-1]), len(argv[-1]))]


def parse_events(lines):
    """Session, usage, cost, the last result text, models seen, tool calls, errors, the init event's loaded set, the
    last quota report and the usage table's models from stream-json lines."""
    session, usage, cost, message, models, tools, errors, turns, subtype = None, None, None, None, set(), 0, [], None, None
    init, rate, usage_models, api_status = None, None, [], None
    for raw in lines:
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        session = session or (event.get('session_id') if isinstance(event.get('session_id'), str) else None)
        if kind == 'system' and event.get('subtype') == 'init' and init is None:
            init = {key: event.get(key) for key in INIT_KEYS}
        if kind == 'rate_limit_event' and isinstance(event.get('rate_limit_info'), dict):
            rate = event['rate_limit_info']
        if kind == 'assistant' and isinstance(event.get('message'), dict):
            body = event['message']
            if isinstance(body.get('model'), str):
                models.add(body['model'])
            for part in body.get('content') or ():
                if isinstance(part, dict) and part.get('type') == 'tool_use':
                    tools += 1
                if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
                    message = part['text']
        if kind == 'result':
            subtype = event.get('subtype')
            if isinstance(event.get('result'), str):
                message = event['result']
            if isinstance(event.get('total_cost_usd'), (int, float)):
                cost = float(event['total_cost_usd'])
            if isinstance(event.get('num_turns'), int):
                turns = event['num_turns']
            found = event.get('usage') if isinstance(event.get('usage'), dict) else {}
            usage = normalize_usage(found)
            if isinstance(event.get('modelUsage'), dict):
                usage_models = sorted(str(name) for name in event['modelUsage'])
            if isinstance(event.get('api_error_status'), int):
                api_status = event['api_error_status']
            if event.get('is_error') or (subtype and subtype != 'success'):
                errors.append(str(event.get('result') or subtype)[:300])
        if kind == 'error' or (kind == 'system' and event.get('subtype') == 'error'):
            errors.append(str(event.get('message') or event.get('error') or kind)[:300])
    return {'session': session, 'usage': usage, 'cost_usd': cost, 'message': message, 'models': sorted(models),
            'tool_calls': tools, 'errors': errors, 'turns': turns, 'subtype': subtype, 'init': init,
            'rate_limit': rate_summary(rate), 'usage_models': usage_models, 'api_error_status': api_status}


def rate_summary(rate):
    """The quota report, content-free: its status and each window's utilisation and reset time."""
    if not isinstance(rate, dict):
        return None
    windows = rate.get('unifiedWindows') if isinstance(rate.get('unifiedWindows'), dict) else {}
    if not windows and isinstance(rate.get('rateLimitType'), str):
        windows = {rate['rateLimitType']: {'utilization': rate.get('utilization'), 'resetsAt': rate.get('resetsAt')}}
    number = lambda value: value if isinstance(value, (int, float)) and not isinstance(value, bool) else None  # noqa: E731
    return {'status': rate.get('status') if isinstance(rate.get('status'), str) else None,
            'resets_at': number(rate.get('resetsAt')),
            'windows': {str(name): {'utilization': number(w.get('utilization')), 'resets_at': number(w.get('resetsAt'))}
                        for name, w in windows.items() if isinstance(w, dict)}}


def quota_wait(rate, now=None):
    """Seconds to pause before the next cell: 0 while every reported window is below PAUSE_AT and the last status was
    not a refusal; else until the latest blocking window resets (plus a minute); None when that is beyond MAX_PAUSE or
    unknown, which stops the run."""
    if not rate:
        return 0
    now = time.time() if now is None else now
    blocking = [w['resets_at'] for w in rate['windows'].values()
                if isinstance(w['utilization'], (int, float)) and w['utilization'] >= PAUSE_AT]
    if rate['status'] == 'rejected':
        blocking.append(rate['resets_at'] or max((w['resets_at'] or 0 for w in rate['windows'].values()), default=None))
    if not blocking:
        return 0
    if any(not isinstance(at, (int, float)) or not at for at in blocking):
        return None
    wait = max(blocking) - now + 60
    return max(0, round(wait)) if wait <= MAX_PAUSE else None


def normalize_usage(found):
    """Anthropic usage in Pebble's terms (stats.usage_metrics): input counts cache reads and writes, cached is the
    cache-read part of it, so both products' tokens compare on the same footing."""
    count = lambda value: value if type(value) is int and value >= 0 else None  # noqa: E731
    base, read, write = (count(found.get(k)) for k in ('input_tokens', 'cache_read_input_tokens',
                                                        'cache_creation_input_tokens'))
    total = None if base is None else base + (read or 0) + (write or 0)
    return {'input_tokens': total, 'cached_tokens': read if read is not None else (0 if base is not None else None),
            'cache_write_tokens': write, 'output_tokens': count(found.get('output_tokens'))}


def add_usage(total, part):
    if part is None:
        return total
    if total is None:
        return dict(part)
    return {k: (total.get(k) + part.get(k) if isinstance(total.get(k), int) and isinstance(part.get(k), int) else None)
            for k in total}


def failure_kind(text, rate=None):
    if AUTH_TEXT.search(text or ''):
        return 'auth'
    if (rate or {}).get('status') == 'rejected' or QUOTA_TEXT.search(text or ''):
        return 'quota'
    if PROVIDER_TEXT.search(text or ''):
        return 'provider'
    return None


def prepare_cell(task, planned, root):
    """The cell's folders and fixture copy; returns (cell folder, workspace, fake home, tmp)."""
    folder = ws.cell_dir(root, 'claude_code', task.id, planned['rep'])
    workspace = ws.materialize(folder / 'workspace', task.files)
    fake_home, tmp = scratch_home(folder / 'home'), folder / 'tmp'
    tmp.mkdir(mode=0o700)
    return folder, workspace, fake_home, tmp


def dry_run_cell(task, planned, root, binary, config_dir, model):
    """Folders and commands only: nothing is started."""
    from ..corpus_24a import prompt_for
    folder, workspace, fake_home, tmp = prepare_cell(task, planned, root)
    prompts = [prompt_for(task, planned['phrasing'])] + [turn.text for turn in task.turns]
    turns = []
    for index, prompt in enumerate(prompts):
        argv = command(binary, model, prompt, config_dir, session_id='<session from turn 1>' if index else None)
        turns.append({'turn': index + 1, 'argv': public_command(argv),
                      'if_asked': bool(index and task.turns[index - 1].if_asked)})
    return {'position': planned['position'], 'task': task.id, 'rep': planned['rep'], 'phrasing': planned['phrasing'],
            'folder': str(folder), 'workspace_files': sorted(ws.inventory(workspace)),
            'environment': sorted(environment(config_dir, fake_home, tmp, binary, model)), 'turns': turns,
            'wall_seconds': planned.get('wall_seconds', 900)}


class Process:
    """One Claude Code turn as a child process in its own session; killed only while its leader is unreaped."""

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
            signal_group(self.child.pid, signal.SIGTERM)  # Phase 32 (H3): EPERM on a zombie group
            try:
                self.child.wait(KILL_GRACE)
            except subprocess.TimeoutExpired:
                if self.child.poll() is None:
                    signal_group(self.child.pid, signal.SIGKILL)
                self.child.wait()


def run_cell(task, planned, root, binary, config_dir, model, *, wall=900, spawn=Process, personal=frozenset()):
    """One live cell (all its turns). Returns (Output, metrics) for the graders and the results cell."""
    from ..corpus_24a import prompt_for
    folder, workspace, fake_home, tmp = prepare_cell(task, planned, root)
    env = environment(config_dir, fake_home, tmp, binary, model)
    before = ws.inventory(workspace)
    started = time.monotonic()
    deadline = started + wall
    replies, turn_files, session, usage, cost, models, first, state, failure, questions = \
        [], [], None, None, 0.0, set(), None, 'completed', None, 0
    tool_calls, cost_known, breaches, rate, helpers = 0, True, [], None, set()
    texts = [(prompt_for(task, planned['phrasing']), None)] + [(turn.text, turn) for turn in task.turns]
    for prompt, turn in texts:
        if turn is not None and turn.if_asked:
            if not asks_question(replies[-1] if replies else None, changed=last_turn_changed(before, turn_files)):
                replies.append(None)
                turn_files.append(None)
                continue
            questions += 1
        if turn is not None and turn.new_chat:
            session = None
        process = spawn(command(binary, model, prompt, config_dir, session_id=session), workspace, env)
        lines, first_line = process.lines(deadline)
        timed_out = process.child.poll() is None
        process.stop()
        stderr = process.child.stderr.read() if process.child.stderr else ''
        parsed = parse_events(lines)
        first = first if first is not None else (None if first_line is None else round(first_line, 3))
        session = parsed['session'] or session
        usage = add_usage(usage, parsed['usage'])
        if parsed['cost_usd'] is None:
            cost_known = False
        else:
            cost += parsed['cost_usd']
        models |= set(parsed['models'])
        helpers |= set(parsed['usage_models']) - {model}
        tool_calls += parsed['tool_calls']
        rate = parsed['rate_limit'] or rate
        replies.append(parsed['message'])
        turn_files.append(ws.inventory(workspace))
        if timed_out:
            state = 'timeout'
            break
        if parsed['init'] is None and process.child.returncode != 0:
            # The CLI never started a session (no init event): the program under test was not there to answer, as
            # when the installed CLI was replaced mid-run (the claim plan, batch 7). A driver failure, never an answer.
            state, failure = 'failed', 'unavailable'
            break
        if parsed['init'] is not None or process.child.returncode == 0:
            breaches = isolation_breaches(parsed['init'], model, personal)
        if breaches:
            state, failure = 'failed', 'isolation'
            break
        if process.child.returncode != 0 or not parsed['message']:
            state, failure = 'failed', failure_kind(stderr + ' ' + ' '.join(parsed['errors']), parsed['rate_limit'])
            break
    drift = sorted(m for m in models if m != model)
    out = Output(replies=replies, state=state, before=before, after=ws.inventory(workspace), turn_files=turn_files,
                 questions=questions, children=None, applied=None, failure=failure)
    error = ({'isolation': 'claude_isolation_breach'}.get(failure, 'claude_' + failure) if failure
             else ('model_drift' if drift else None))
    metrics = {'review': {'escalations': 0, 'tool_calls': tool_calls}, 'requests': None, 'usage': usage,
               'wall': round(time.monotonic() - started, 3), 'first': first, 'error': error,
               'cost_usd_reported': round(cost, 6) if cost_known else None, 'models': sorted(models),
               'model_drift': drift, 'helper_models': sorted(helpers), 'isolation': breaches, 'rate_limit': rate}
    return out, metrics


def _file_sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def pin_binary(binary, root):
    """A private copy of the Claude Code executable for a plan's scratch root, made once and reused by every batch, so an
    update or reinstall of the installed CLI during a run cannot change or remove the program under test (the claim
    plan: every cell of batch 7 failed in two seconds while the installed CLI was replaced). Returns (path, sha256)."""
    source = Path(shutil.which(str(binary)) or binary).resolve()
    if not source.is_file():
        raise ClaudeRefused('claude_binary_missing')
    digest = _file_sha(source)
    folder = Path(root) / 'claude-cli' / digest[:16]
    target = folder / source.name
    if not target.is_file():
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        partial = folder / (source.name + '.partial')
        shutil.copyfile(source, partial)
        os.chmod(partial, 0o755)
        os.replace(partial, target)
    if _file_sha(target) != digest:
        raise ClaudeRefused('claude_pinned_binary_changed')
    return target, digest


def unchanged(path, digest):
    """Whether the pinned executable still has its digest (checked before each batch)."""
    return Path(path).is_file() and _file_sha(path) == digest


def probe(binary, run=subprocess.run):
    """Claude Code's version and whether `claude --help` offers every flag this driver uses (no network, no model)."""
    env = {'PATH': str(Path(binary).resolve().parent) + ':' + SYSTEM_PATH, 'HOME': '/private/tmp', 'LANG': 'en_US.UTF-8',
           'DISABLE_AUTOUPDATER': '1'}
    version = run([str(binary), '--version'], capture_output=True, text=True, timeout=60, env=env)
    help_text = run([str(binary), '--help'], capture_output=True, text=True, timeout=60, env=env)
    text = (help_text.stdout or '') + (help_text.stderr or '')
    missing = [flag for flag in REQUIRED_FLAGS if flag not in text]
    found = re.search(r'\d+\.\d+\.\d+', (version.stdout or '') + (version.stderr or ''))
    return {'version': found.group(0) if found else None, 'missing_flags': missing,
            'ok': version.returncode == 0 and help_text.returncode == 0 and not missing}


def signed_in(binary, config_dir, run=subprocess.run):
    """Whether the configuration holds a login that a cell's scratch HOME reaches (`claude auth status`); only the
    login state and the subscription type are kept, and the credential itself is never read."""
    with tempfile.TemporaryDirectory(prefix='pebble-claude-auth-', dir=str(ws.TMP)) as scratch:
        env = {'PATH': str(Path(binary).resolve().parent) + ':' + SYSTEM_PATH,
               'HOME': str(scratch_home(Path(scratch) / 'home')), 'CLAUDE_CONFIG_DIR': str(config_dir),
               'USER': account(), 'LOGNAME': account(), 'LANG': 'en_US.UTF-8', 'DISABLE_AUTOUPDATER': '1'}
        done = run([str(binary), 'auth', 'status', '--json'], capture_output=True, text=True, timeout=60, env=env)
    try:
        data = json.loads(done.stdout or '{}')
    except ValueError:
        data = {}
    logged = bool(isinstance(data, dict) and (data.get('loggedIn') or data.get('logged_in')))
    plan = data.get('subscriptionType') or data.get('subscription_type') if isinstance(data, dict) else None
    return {'signed_in': logged and done.returncode == 0, 'subscription': plan if isinstance(plan, str) else None}


def preflight(binary, config_dir, model, *, personal=frozenset(), spawn=Process, wall=180):
    """One fixed, tiny turn outside every plan and registry, in its own throwaway folder: the login works through a
    scratch HOME, the isolation holds on the plan's model, and the quota windows as Claude Code reports them. Its
    transcript in the configuration folder is benchmark residue (residue/remove_residue)."""
    folder = Path(tempfile.mkdtemp(prefix='pebble-bench-preflight-', dir=str(ws.TMP)))
    workspace, tmp = folder / 'workspace', folder / 'tmp'
    workspace.mkdir(mode=0o700)
    tmp.mkdir(mode=0o700)
    home = scratch_home(folder / 'home')
    process = spawn(command(binary, model, PREFLIGHT_PROMPT, config_dir), workspace,
                    environment(config_dir, home, tmp, binary, model))
    lines, _ = process.lines(time.monotonic() + wall)
    process.stop()
    parsed = parse_events(lines)
    breaches = isolation_breaches(parsed['init'], model, personal)
    replied = (parsed['message'] or '').strip().strip('.') == 'OK'
    failure = None if replied and process.child.returncode == 0 else (
        failure_kind(' '.join(parsed['errors'] + [parsed['message'] or '']), parsed['rate_limit']) or 'no_reply')
    shutil.rmtree(folder, ignore_errors=True)
    return {'ok': replied and not breaches and not failure and not set(parsed['models']) - {model},
            'isolation': breaches, 'failure': failure, 'models': parsed['models'],
            'helper_models': sorted(set(parsed['usage_models']) - {model}), 'rate_limit': parsed['rate_limit'],
            'cost_usd_reported': parsed['cost_usd']}


def _sha_file(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def snapshot(config_dir, taken_at):
    """Names and digests of a configuration folder before benchmarks (never a value): the top-level names, the project
    folders, the settings keys and digest, and the global config's keys and project paths."""
    config = Path(config_dir)
    settings = config / 'settings.json'
    data = json.loads(settings.read_text(encoding='utf-8')) if settings.is_file() else {}
    found = {'schema': 'pebble.claude_w_snapshot/1', 'taken_at': taken_at, 'config': str(config),
             'top_level': sorted(os.listdir(config)), 'projects': sorted(os.listdir(config / 'projects'))
             if (config / 'projects').is_dir() else [], 'settings_keys': sorted(data) if isinstance(data, dict) else [],
             'settings_sha256': _sha_file(settings)}
    glob = config / '.claude.json'
    if glob.is_file():
        state = json.loads(glob.read_text(encoding='utf-8'))
        found['global_config_keys'] = sorted(state)
        found['global_config_project_paths'] = sorted((state.get('projects') or {}).keys())
    return found


def residue(before, config_dir):
    """What benchmarks left in a configuration folder against its snapshot: the benchmark's own project folders (to
    remove) and, by name only, every other difference (left for the person)."""
    now = snapshot(config_dir, before.get('taken_at'))
    remove = sorted(n for n in now['projects'] if RESIDUE.fullmatch(n))
    return {'remove': remove,
            'other_new_projects': sorted(set(now['projects']) - set(before['projects']) - set(remove)),
            'top_level_new': sorted(n for n in set(now['top_level']) - set(before['top_level'])
                                    if not n.endswith('.lock')),
            'top_level_gone': sorted(n for n in set(before['top_level']) - set(now['top_level'])
                                     if not n.endswith('.lock')),
            'settings_unchanged': now['settings_sha256'] == before.get('settings_sha256'),
            'settings_keys_unchanged': now['settings_keys'] == before.get('settings_keys'),
            'global_config_keys_new': sorted(set(now.get('global_config_keys', [])) -
                                             set(before.get('global_config_keys', []))),
            'global_config_project_paths_new': sorted(set(now.get('global_config_project_paths', [])) -
                                                      set(before.get('global_config_project_paths', [])))}


def remove_residue(before, config_dir):
    """Remove only the benchmark's own project folders (RESIDUE names, real folders, inside projects/); returns what
    was removed and the differences that remain."""
    found = residue(before, config_dir)
    projects = (Path(config_dir) / 'projects').resolve()
    removed = []
    for name in found['remove']:
        target = projects / name
        if RESIDUE.fullmatch(name) and target.is_dir() and not target.is_symlink() and target.parent == projects:
            shutil.rmtree(target)
            removed.append(name)
    return {'removed': removed, 'after': residue(before, config_dir)}


__all__ = ['ClaudeRefused', 'account', 'add_usage', 'pin_binary', 'unchanged', 'check_claude_config', 'command', 'dry_run_cell', 'environment',
           'isolation_breaches', 'keychains', 'normalize_usage', 'parse_events', 'personal_names', 'preflight', 'probe',
           'quota_wait', 'rate_summary', 'remove_residue', 'residue', 'run_cell', 'sandbox_profile', 'scratch_home',
           'signed_in', 'snapshot']
