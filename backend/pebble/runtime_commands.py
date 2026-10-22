"""Shared host command catalogue. Commands do not invoke a model or shell.

Phase 24 (V1-108, i11b-features-08, i11a-core-17): descriptions and replies are plain words for the person. No JSON,
digests or internal names; lists are short Markdown lists; tasks are named by their first words, not by IDs (except
where an ID is the argument another command takes). Behaviour, names and arguments are unchanged.
"""

import re
import time

CATALOGUE = [
    {'name': 'jobs', 'arguments': '', 'description': 'List this conversation\'s schedules and waiting notifications.'},
    {'name': 'git', 'arguments': '', 'description': 'Show which files changed in this project\'s Git repository.'},
    {'name': 'agents', 'arguments': '', 'description': 'List the agents working on the latest task and what they have used.'},
    {'name': 'agent', 'arguments': 'agent-id', 'description': 'Show one agent\'s task, progress and result.'},
    {'name': 'views', 'arguments': '', 'description': 'List the views saved in this conversation.'},
    {'name': 'mods', 'arguments': '', 'description': 'List installed display code (mods), a developer feature.'},
    {"name": "help", "arguments": "", "description": "List the commands you can type here."},
    {"name": "status", "arguments": "", "description": "Show how the latest task in this conversation is going."},
    {"name": "runs", "arguments": "", "description": "List recent tasks in this conversation."},
    {"name": "pause", "arguments": "", "description": "Pause the running task at a safe point."},
    {"name": "resume", "arguments": "[run-id] [--steps N] [--seconds N]", "description": "Resume a paused or stopped task, optionally with more steps or time."},
    {"name": "cancel", "arguments": "", "description": "Stop this conversation's running task."},
    {'name': 'steer', 'arguments': 'text', 'description': 'Send a follow-up to the running task.'},
    {'name': 'plan', 'arguments': '', 'description': 'Show the task\'s plan and what supports each step.'},
    {'name': 'questions', 'arguments': '', 'description': 'List questions waiting for your answer.'},
    {'name': 'context', 'arguments': '[run-id]', 'description': 'Show how much the model sees for a task and which files need a fresh read.'},
    {'name': 'handoff', 'arguments': '[run-id]', 'description': 'Save what a task needs to continue later, and show what it holds.'},
    {'name': 'compact', 'arguments': '[run-id]', 'description': 'Make room by archiving older context between steps; no model is asked.'},
    {'name': 'memory', 'arguments': '[status|candidates|maintain]', 'description': 'Show memory counts or waiting memories, or check waiting memories now.'},
    {'name': 'skills', 'arguments': '', 'description': 'List installed skills and whether each is on.'},
    {'name': 'skill', 'arguments': '"name or ID" task', 'description': 'Start a new task with complete pinned instructions from one enabled skill.'},
    {'name': 'plugins', 'arguments': '', 'description': 'List the connectors you have enabled and their tools.'},
    {'name': 'hooks', 'arguments': '', 'description': 'List the hook checks recorded in this conversation.'},
    {'name': 'goal', 'arguments': '["title"|done|pause|resume|abandon]', 'description': 'Start, show or settle this conversation\'s goal; done needs every criterion met.'},
    {'name': 'criteria', 'arguments': '[add [--evidence] "text"|check|uncheck|accept|reject criterion-id]', 'description': 'List or change the goal\'s completion criteria; evidence criteria need current results of finished steps.'},
    {'name': 'blockers', 'arguments': '', 'description': 'List what holds up the goal or task: questions, reviews, failed or unclear results.'},
    {'name': 'exports', 'arguments': '', 'description': 'List this conversation\'s export files and whether each was checked; nothing is published.'},
    {'name': 'export', 'arguments': 'view-id pdf|docx|xlsx|csv|html', 'description': 'Plan an export of a view; saving needs the Save dialog and nothing is published.'},
]

ARGUMENTS = {'resume', 'steer', 'context', 'handoff', 'compact', 'memory', 'agent', 'goal', 'criteria', 'export'}
EXPORT_FORMATS = ('pdf', 'docx', 'xlsx', 'csv', 'html')
QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”')
# Typed commands never carry C0 controls other than tab and line breaks. This also keeps the word split identical to
# Rust's split_whitespace, which the pet's goal-settling check (main.rs settles_goal) uses (X17 F1).
COMMAND_CONTROL = re.compile('[\x00-\x08\x0e-\x1f\x7f]')
GOAL_USAGE = 'Use /goal "title", /goal, or /goal done|pause|resume|abandon.'
CRITERIA_USAGE = 'Use /criteria, /criteria add [--evidence] "text", or /criteria check|uncheck|accept|reject criterion-id.'
NO_GOAL = 'This conversation has no current goal. Start one with /goal "title".'
# "no inference" stays in the /goal reply: test_phase17_security.py pins it (V1-108 keeps that one phrase).
GOAL_FOOTER = 'A goal is a checklist Pebble keeps for you: it starts no task, grants no permission and uses no inference.'
EVIDENCE_NOTE = 'Evidence links point to finished task steps; they are not proof of correctness.'
STATE_WORDS = {
    'queued': 'waiting to start', 'running': 'running', 'retry_wait': 'retrying', 'waiting_approval': 'waiting for your review',
    'waiting_input': 'waiting for your answer', 'paused': 'paused', 'stopped': 'stopped', 'interrupted': 'interrupted',
    'failed': 'failed', 'completed': 'completed', 'budget_exhausted': 'out of steps or time',
    'verification_required': 'held for a check', 'needs_reconciliation': 'needs you to check an unclear result',
}
STATUS_LETTERS = {'M': 'modified', 'A': 'added', 'D': 'deleted', 'R': 'renamed', 'C': 'copied', 'T': 'type changed', 'U': 'in conflict'}


def _state(value):
    return STATE_WORDS.get(value, str(value).replace('_', ' '))


def _first_words(text, limit=60):
    text = ' '.join(str(text or '').split())
    return (text if len(text) <= limit else text[:limit - 1].rstrip() + '…') or 'Untitled task'


def _when(seconds):
    try:
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(float(seconds)))
    except (TypeError, ValueError, OverflowError, OSError):
        return 'an unknown time'


def _code(text, limit=200):
    """Repository or record text inside a Markdown code span: never Markdown, never a line break."""
    return '`' + ' '.join(str(text).replace('`', "'").split())[:limit] + '`'


def _task(app, cid, run):
    try:
        goal = app.runs.get(run['id'], cid).get('goal')
    except (ValueError, KeyError, TypeError):
        goal = None
    return '“' + _first_words(goal) + '”'


def _quoted(text):
    match = QUOTED.fullmatch(text.strip())
    return next(g for g in match.groups() if g is not None).strip() if match else None


def _goals(app):
    goals = getattr(app, 'goals', None)
    if goals is None:
        raise ValueError('Goals are not available in this build.')
    return goals


def _blocker_line(item):
    detail = item.get('detail')
    if detail:
        detail = f" — {detail}" + (' (agent-authored)' if item.get('detail_origin') == 'agent' else '')
    where = f" (step {item['step_id']})" if item.get('step_id') else ''
    return f"- {item['label']}{where}: {item['kind']} `{item['ref']}`{detail or ''}"


def _goal_text(goal):
    progress = goal['progress']
    lines = [f"Goal `{goal['id'][:8]}` — **{goal['title']}** ({goal['state']}, revision {goal['revision']})."]
    accepted = [c for c in goal['criteria'] if c['status'] not in ('candidate', 'rejected')]
    lines.append(f"Criteria: {progress['criteria_met']}/{progress['criteria_total']} met." + ('\n' if accepted else ' Add one with /criteria add "text".')
        + '\n'.join(f"- `{c['id']}` [{c['kind'].replace('_', ' ')}] {c['status']} — {c['text']}" + (f" ({c['detail']})" if c['detail'] else '') for c in accepted))
    candidates = [c for c in goal['criteria'] if c['status'] == 'candidate']
    if candidates:
        lines.append('Proposed by Pebble (accept or reject with /criteria):\n' + '\n'.join(f"- `{c['id']}` [{c['kind'].replace('_', ' ')}] {c['text']}" for c in candidates))
    if goal['steps']:
        lines.append(f"Steps: {progress['steps_completed']}/{progress['steps_total']} completed.\n" + '\n'.join(
            f"- `{s['id']}` {s['title']} — {s['status'].replace('_', ' ')}" + (f"; after {', '.join(s['depends_on'])}" if s['depends_on'] else '')
            + (f" ({len(s['evidence'])} evidence link{'' if len(s['evidence']) == 1 else 's'}{'' if s['evidence_current'] else ', not current'})" if s['status'] == 'completed' else '') for s in goal['steps']))
    if goal['blockers']:
        lines.append('Blockers:\n' + '\n'.join(_blocker_line(b) for b in goal['blockers']))
    lines.append(GOAL_FOOTER + ' ' + EVIDENCE_NOTE)
    return '\n\n'.join(lines)


def _goal_command(app, cid, name, args, raw):
    goals = _goals(app)
    if name == 'blockers':
        packet = goals.blockers(cid)
        if not packet['items']:
            # validate_phase17.py (c1) pins this reply exactly (historical validators are never edited), so V1-108's
            # plainer wording is not used here.
            return 'No host-recorded blockers.' if packet['goal_id'] or packet.get('run_id') else 'This conversation has no goal or runs yet.'
        return 'Blockers:\n\n' + '\n'.join(_blocker_line(b) for b in packet['items'])
    current = goals.current(cid)
    if name == 'goal':
        if not raw:
            return _goal_text(current) if current else NO_GOAL
        title = _quoted(raw)
        if title is not None:
            goal = goals.create(cid, title)
            return (f"Started goal `{goal['id'][:8]}`: **{goal['title']}**. Add completion criteria with /criteria add \"text\".\n\n" + GOAL_FOOTER)
        if len(args) != 1 or args[0].lower() not in {'done', 'pause', 'resume', 'abandon'}:
            raise ValueError(GOAL_USAGE)
        if not current:
            raise ValueError(NO_GOAL)
        action = args[0].lower()
        goal = goals.set_state(cid, current['id'], {'done': 'done', 'pause': 'paused', 'resume': 'active', 'abandon': 'abandoned'}[action])
        if action == 'done':
            return (f"Goal `{goal['id'][:8]}` is done: all {goal['progress']['criteria_total']} criteria are met. "
                    'Your checks record your judgment; evidence criteria were checked against the current results of finished steps, which are not proof of correctness.')
        return f"Goal `{goal['id'][:8]}` is {goal['state']}." + (' Pebble will not work on it until you resume it.' if action == 'pause' else '')
    if not raw:
        if not current:
            return NO_GOAL
        criteria = [c for c in current['criteria'] if c['status'] != 'rejected']
        return ('Completion criteria:\n\n' + '\n'.join(f"- `{c['id']}` [{c['kind'].replace('_', ' ')}] {c['status']}" + (' (proposed by Pebble)' if c['status'] == 'candidate' else '')
            + f" — {c['text']}" + (f" ({c['detail']})" if c['detail'] else '') for c in criteria)) if criteria else 'This goal has no criteria. Add one with /criteria add "text".'
    if not current:
        raise ValueError(NO_GOAL)
    action = args[0].lower()
    if action == 'add':
        rest, kind = raw[len(args[0]):].strip(), 'human_check'
        flag = re.match(r'--evidence\s+', rest)
        if flag:
            rest, kind = rest[flag.end():], 'evidence'
        text = _quoted(rest)
        if text is None:
            raise ValueError(CRITERIA_USAGE)
        ident = goals.add_criterion(cid, current['id'], text, kind)['changed']
        return (f"Added criterion `{ident}` ({kind.replace('_', ' ')}). " + ('Check it with /criteria check ' + ident + ' when you judge it met.' if kind == 'human_check'
                else 'It is met only by the current results of the goal steps linked to it.'))
    if action not in {'check', 'uncheck', 'accept', 'reject'} or len(args) != 2:
        raise ValueError(CRITERIA_USAGE)
    ident = args[1]
    if action in {'accept', 'reject'}:
        goals.review_criterion(cid, current['id'], ident, action)
        return f"Criterion `{ident}` {'accepted' if action == 'accept' else 'rejected'}."
    goal = goals.check_criterion(cid, current['id'], ident, met=action == 'check')
    item = next(c for c in goal['criteria'] if c['id'] == ident)
    if action == 'uncheck':
        return f"Criterion `{ident}` marked not met."
    if item['kind'] == 'evidence':
        count = len(item['evidence'])
        return f"Criterion `{ident}` is met: Pebble checked {count} current result{'' if count == 1 else 's'} of finished steps. They are evidence, not proof of correctness."
    return f"Criterion `{ident}` checked by you."


def _export_command(app, cid, name, args):
    exports = getattr(app, 'exports', None)
    if name == 'exports':
        if exports is None or not hasattr(exports, 'list'):
            return 'Exports are not available in this build.'
        rows = exports.list(cid)
        if not rows:
            return 'This conversation has no exports.'
        def line(r):
            source = r.get('source') or {}
            status = r.get('status') or ('verified' if r.get('verified') else 'unverified')
            return f"- `{r.get('export_id') or r.get('id')}` — {r.get('format')} of {source.get('kind', 'source')} `{source.get('id')}` revision {source.get('revision')}: {status}"
        return 'Exports (local files in Pebble\'s data folder; nothing is published):\n\n' + '\n'.join(line(r) for r in rows[:50])
    if len(args) != 2 or args[1].lower() not in EXPORT_FORMATS or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args[0]):
        raise ValueError('Use /export view-id pdf|docx|xlsx|csv|html.')
    view, fmt = app.artifacts.get(cid, args[0]), args[1].lower()
    if exports is None or not hasattr(exports, 'preview'):
        return 'Exports are not available in this build. Nothing was written.'
    plan = exports.preview(cid, {'kind': 'artifact', 'id': view['id']}, fmt, {})
    source = plan.get('source') or {}
    pages = f", about {plan['pages_estimate']} pages" if plan.get('pages_estimate') is not None else ''
    return (f"Export preview `{plan['plan_id']}`: {plan.get('format', fmt)} of view `{view['id']}` (\"{view['title']}\") revision {source.get('revision', view['revision'])}"
            + (f", digest `{source['digest']}`" if source.get('digest') else '') + pages + '.\n\n'
            'This is a preview only. Nothing was written, saved or published. Save it from the Export panel with the native Save dialog.')


def _schedule_words(schedule):
    schedule = schedule if isinstance(schedule, dict) else {}
    if schedule.get('kind') == 'once':
        return 'once, at ' + _when(schedule.get('at'))
    if schedule.get('kind') == 'interval':
        seconds = schedule.get('seconds')
        if isinstance(seconds, (int, float)) and seconds > 0:
            for unit, size in (('day', 86400), ('hour', 3600), ('minute', 60)):
                if seconds % size == 0:
                    count = int(seconds // size)
                    return 'every ' + (unit if count == 1 else f'{count} {unit}s')
            return f'every {int(seconds)} seconds'
        return 'repeating'
    if schedule.get('kind') == 'daily':
        zone = schedule.get('timezone')
        return 'every day at ' + str(schedule.get('time') or '?') + (f' ({zone})' if zone else '')
    return 'on its own schedule'


def _jobs_text(packet):
    """/jobs: the conversation's schedules and waiting notifications as a Markdown list (V1-108)."""
    jobs = packet.get('jobs') or []
    notices = packet.get('notifications') or []
    service = packet.get('service') or {}
    lines = []
    if jobs:
        lines.append('Schedules in this conversation (they run only while Pebble is open):\n\n' + '\n'.join(
            f"- **{_first_words(j.get('name'), 80)}** — {_schedule_words(j.get('schedule'))}; {str(j.get('state', 'unknown')).replace('_', ' ')}"
            + (f"; next run {_when(j['next_fire'])}" if j.get('next_fire') else '')
            + (f". Needs attention: {_first_words(j['error'], 160)}" if j.get('error') else '') for j in jobs[:50]))
    else:
        lines.append('This conversation has no schedules. Create one under Details › Tools.')
    if notices:
        lines.append('Waiting notifications:\n\n' + '\n'.join(
            f"- {_first_words(n.get('title'), 120)} ({_when(n.get('created'))})" for n in notices[:20]))
    if jobs and not service.get('running', True):
        lines.append('Schedules are not running right now; they start again when Pebble is open.')
    return '\n\n'.join(lines)


def _git_entry(entry):
    if entry.get('kind') == 'untracked':
        words = 'new, not tracked yet'
    elif entry.get('kind') == 'unmerged':
        words = 'in conflict'
    else:
        staged, unstaged = entry.get('index') or '.', entry.get('worktree') or '.'
        parts = ([STATUS_LETTERS.get(staged, 'changed') + ', staged'] if staged != '.' else []) + \
                ([STATUS_LETTERS.get(unstaged, 'changed') + (', not staged' if staged != '.' else '')] if unstaged != '.' else [])
        words = '; '.join(parts) or 'changed'
    return f"- {_code(entry.get('path', ''))} — {words}" + (' (submodule)' if entry.get('submodule') else '')


def _git_text(view):
    """/git: Git status as a file list (V1-114). File names are repository data, shown as code text."""
    branch = view.get('branch')
    where = f"on branch {_code(branch, 100)}" if branch else 'on a detached checkout'
    lines = [f"Git status {where} (file names are repository data):"]
    if view.get('upstream'):
        ahead, behind = view.get('ahead') or 0, view.get('behind') or 0
        lines.append(f"{ahead} commit{'' if ahead == 1 else 's'} ahead of and {behind} behind {_code(view['upstream'], 100)}.")
    entries = view.get('entries') or []
    if view.get('clean') or not entries:
        lines.append('No changes: the files match the last commit.' if view.get('clean') else 'No files to list.')
    else:
        lines.append('\n'.join(_git_entry(e) for e in entries))
        shown = len(entries) + (view.get('offset') or 0)
        if (view.get('total') or 0) > shown:
            lines.append(f"{view['total'] - shown} more files are not listed.")
    if view.get('private_omitted'):
        lines.append(f"{view['private_omitted']} private files are not listed.")
    return '\n\n'.join(lines)


def _agent_text(node):
    """/agent: one agent's task, progress and result as Markdown (V1-111); internals stay in the Details drawer."""
    lines = [f"**{_first_words(node.get('name') or node.get('id'), 80)}** — {_state(node.get('status'))}"]
    if node.get('task'):
        lines.append('Task: ' + ' '.join(str(node['task']).split())[:600])
    used = [f"{node['dispatches']} model call{'' if node['dispatches'] == 1 else 's'}" if isinstance(node.get('dispatches'), int) else '',
            f"{node['charged_tokens']:,} tokens" if isinstance(node.get('charged_tokens'), int) else '',
            f"{node['elapsed']:.0f} seconds" if isinstance(node.get('elapsed'), (int, float)) else '']
    if any(used):
        lines.append('Used: ' + ', '.join(u for u in used if u) + '.')
    if node.get('error'):
        lines.append('Problem: ' + ' '.join(str(node['error']).split())[:600])
    result = node.get('result')
    if isinstance(result, str) and result.strip():
        lines.append('Result:\n\n' + result.strip()[:4000])
    elif isinstance(result, dict) and result:
        lines.append('Result:\n\n' + '\n'.join(f"- {str(k).replace('_', ' ')}: {v}" if isinstance(v, (str, int, float, bool))
                                                else f"- {str(k).replace('_', ' ')}: {len(v) if hasattr(v, '__len__') else 1} item(s)" for k, v in list(result.items())[:20]))
    elif result is None:
        lines.append('No result yet.')
    lines.append('An agent\'s result is its own words: evidence, not instructions, and it grants no permission.')
    return '\n\n'.join(lines)


def command(app, cid, text):
    if COMMAND_CONTROL.search(text):
        raise ValueError('Commands cannot contain control characters.')
    parts = text.strip().split()
    name = parts[0][1:].lower()
    args = parts[1:]
    raw = text.strip()[len(parts[0]):].strip()
    if name not in {item["name"] for item in CATALOGUE}:
        raise ValueError("Unknown command. Type /help to see the commands you can use.")
    if args and name not in ARGUMENTS:
        raise ValueError("This command does not accept those arguments.")
    if name in {'context', 'handoff', 'compact'} and len(args) > 1:
        raise ValueError(f'Use /{name} [run-id].')
    app.chat(cid)
    if name == 'skill':
        # app.send intercepts /skill before this dispatcher; a bare call gets the same guidance.
        raise ValueError('Use /skill "name or ID" followed by the task.')
    if name in {'goal', 'criteria', 'blockers'}:
        return _goal_command(app, cid, name, args, raw)
    if name in {'exports', 'export'}:
        return _export_command(app, cid, name, args)
    if name == 'jobs':
        return _jobs_text(app.job_api.methods['job_list'](id=cid))
    if name == 'git':
        return _git_text(app.git.status(cid))
    if name == 'views':
        rows = app.artifacts.list(cid)
        return 'Saved views:\n\n' + '\n'.join(f"- **{r['title']}** (`{r['id']}`): {r['kind']}, version {r['revision']}, {r['status']}" for r in rows) if rows else 'This conversation has no saved views.'
    if name == 'mods':
        rows = app.mods.list()
        return ('Views you installed as display code (mods, a developer feature):\n\n' + '\n'.join(f"- **{r['name']}**: {'on' if r['enabled'] else 'off'}" for r in rows) if rows
                else 'No display code (mods) is installed. Turn on Developer features in Settings → Skills & connectors to review one.')
    if name == 'skills':
        items = [i for i in app.extensions.list() if i['kind'] == 'skill']
        return 'Installed skills:\n\n' + '\n'.join(f"- **{i['name']}**: {'on' if i['enabled'] else 'off'}" for i in items) if items else 'No skills installed. Import a SKILL.md in Settings → Skills & connectors.'
    if name == 'plugins':
        items = app.extensions.plugin_index()
        return ('Connectors you enabled:\n\n' + '\n'.join(f"- **{i['name']}**: {i['tool_count']} tool{'' if i['tool_count'] == 1 else 's'}, "
                + ('a local program' if i['transport'] == 'stdio' else 'over HTTP') for i in items) if items
                else 'No connectors are enabled. Add and review one in Settings → Skills & connectors.')
    if name == 'hooks':
        rows = app.hook_events(cid)
        return 'Hook checks in this conversation:\n\n' + '\n'.join(f"- {str(r['event']).replace('_', ' ')}: {r['decision']} ({r['status']})" for r in rows) if rows else 'No hook checks were recorded in this conversation.'
    if name == 'memory':
        if len(args) > 1 or args and args[0] not in {'status', 'candidates', 'maintain'}:
            raise ValueError('Use /memory [status|candidates|maintain].')
        option = args[0] if args else 'status'
        if option == 'maintain':
            result = app.memory_maintain()
            return f"Checked {result['processed']} waiting memor{'y' if result['processed'] == 1 else 'ies'}. No model was asked."
        if option == 'candidates':
            rows = app.learning.candidates(cid)
            return ('Memories Pebble proposed in this conversation:\n\n' + '\n'.join(
                f"- {r['kind']}: {r['status']} ({str(r['reason'] or 'checked against its evidence').replace('_', ' ')})" for r in rows) if rows
                else 'Pebble has proposed no memories in this conversation.')
        stats = app.memory.stats()
        waiting = {k: v for k, v in (stats.get('learning', {}).get('status_counts') or {}).items() if v}
        return ('Personalized memory: ' + ('on' if stats['enabled'] else 'off') + '.\n\n' +
            '\n'.join(f"- {r['name']}: {r['count']} records; {'on' if r['enabled'] else 'off'}" for r in stats['types']) + '\n\n' +
            ('Proposed memories: ' + ', '.join(f"{v} {str(k).replace('_', ' ')}" for k, v in sorted(waiting.items())) + '.' if waiting else 'No proposed memories.')
            + ' Memory never grants permissions.')
    if name == "help":
        return "Commands you can type:\n\n" + "\n".join(
            f"- /{c['name']} {c['arguments']} — {c['description']}" for c in CATALOGUE)
    if name in {"pause", "cancel"}:
        if name == "pause":
            app.run_pause(cid)
            return "Pause requested. The task stops after its current step."
        app.stop(cid)
        return "Stop requested. Finished steps and their records are kept."
    if name == "resume":
        rid = args.pop(0) if args and not args[0].startswith('--') else None
        limits = {}
        while args:
            flag = args.pop(0)
            if flag not in {'--steps', '--seconds'} or not args:
                raise ValueError('Use /resume [run-id] [--steps N] [--seconds N].')
            key = 'max_steps' if flag == '--steps' else 'max_seconds'
            if key in limits:
                raise ValueError('Each run limit may be specified once.')
            try:
                limits[key] = int(args.pop(0)) if flag == '--steps' else float(args.pop(0))
            except ValueError:
                raise ValueError('Run limits must be numbers.') from None
        if limits:
            run = app.runs.get(rid, cid) if rid else next((app.runs.get(r['id'], cid) for r in app.runs.list(cid) if r['state'] in {'paused','stopped','interrupted','failed','budget_exhausted','waiting_input','waiting_approval','verification_required'}), None)
            if not run:
                raise ValueError('No resumable run found.')
            limits = {**run['config']['limits'], **limits}
        result = app.run_resume(cid, rid, limits=limits or None)
        return f"Resume requested: the task is {_state(result['status'])}."
    if name == 'steer':
        if not args:
            raise ValueError('Use /steer followed by the additional instruction.')
        app.run_steer(cid, ' '.join(args))
        return 'Your follow-up will reach the task at its next step.'
    if name == 'questions':
        questions = app.interactions.questions(cid)
        return 'Questions waiting for your answer:\n\n' + '\n'.join('- ' + i['title'] for q in questions for i in q['items']) if questions else 'No questions are waiting.'
    runs = app.runs.list(cid)
    if name in {'context', 'handoff', 'compact'} and args:
        app.runs.get(args[0], cid)
    if not runs:
        return "This conversation has no tasks yet."
    if name in {'agents', 'agent'}:
        if name == 'agent':
            if len(args) != 1:
                raise ValueError('Use /agent agent-id.')
            return _agent_text(app.agents.get(cid, runs[0]['id'], args[0]))
        packet = app.agents.list(cid, runs[0]['id'])
        return ('Agents on the latest task:\n\n' + '\n'.join(
            f"- **{_first_words(n.get('name') or n['id'], 80)}** ({_code(n['id'], 64)}): {_state(n['status'])}" + (f"; review {n['review']}" if n.get('review') else '')
            for n in packet['nodes']) if packet['nodes'] else 'The latest task has no agents.')
    if name == 'compact':
        result = app.run_compact(cid, args[0] if args else runs[0]['id'])
        return ('This task already fits in the model\'s window; nothing was archived.' if result['status'] == 'already_fits' else
            f"Archived {result['manifest']['archived_messages']} older messages (archive {result['generation']}). Your current instructions and the evidence from finished steps are kept. No model was asked and nothing was run.")
    if name == 'handoff':
        packet = app.run_handoff(cid, args[0] if args else runs[0]['id'])
        manifest = packet['manifest']
        counts = manifest['counts']
        unresolved = manifest['unresolved']
        return (f"Saved a handoff for this task: {counts['receipts']} finished steps, {counts['inputs']['human']} of your messages, "
            f"{counts['sources']['total']} file views ({counts['sources']['stale']} out of date).\n\n"
            f"Still open: {unresolved['effects']} unclear results, {unresolved['waiting_questions']} questions, "
            f"{unresolved['open_plan_steps']} plan steps.\n\n"
            'The handoff keeps your instructions and the evidence. It grants no permission and replays no tools.')
    if name == 'context':
        from .policy import Workspace, PolicyError
        run = app.runs.get(args[0] if args else runs[0]['id'], cid)
        if run['config']['workspace']:
            try:
                ws = Workspace(run['config']['workspace'])
            except (OSError, PolicyError):
                ws = None
            app.provenance.audit(run['id'], ws)
        projected = app.runs.context_manifest(run['id'])
        witnesses = app.provenance.latest(run['id'])
        stale = [s for s in witnesses if s['status'] != 'current']
        views = len(witnesses)
        lines = [f"Task “{_first_words(run.get('goal'))}”: Pebble has {views} file view{'' if views == 1 else 's'}; {len(stale)} need a fresh read."]
        if projected:
            manifest = projected['manifest']
            lines.append(f"The model sees about {manifest['input_tokens']['after']:,} of the {manifest['limits']['input']:,} tokens this task may use.")
        else:
            lines.append('No request to the model has been recorded for this task yet.')
        lines.extend(f"- {_code(s['path'])} — {s['status']} (read from {s['basis']})" for s in stale[:20])
        if len(stale) > 20:
            lines.append(f'{len(stale) - 20} more changed files are listed in Details › Context.')
        return '\n\n'.join(lines)
    if name == 'plan':
        plan = app.interactions.plan(runs[0]['id'])
        return 'Task plan:\n\n' + '\n'.join(f"- {s['title']} — {s['status']} ({len(s['evidence_ids'])} evidence links)" for s in plan['steps']) if plan else 'The latest task has no saved plan.'
    if name == "status":
        run = app.runs.get(runs[0]["id"], cid)
        limits = run["config"]["limits"]
        return (f"Latest task, “{_first_words(run.get('goal'))}”: **{_state(run['state'])}**. "
                f"{run['steps']} of {limits['max_steps']} steps and {run['elapsed']:.0f} of {limits['max_seconds']:.0f} seconds used."
                + ("\n\n" + run["error"] if run["error"] else ""))
    return "Recent tasks:\n\n" + "\n".join(
        f"- {_task(app, cid, r)} — {_state(r['state'])}, {r['steps']} step{'' if r['steps'] == 1 else 's'}, started {_when(r.get('created'))}" for r in runs[:20])
