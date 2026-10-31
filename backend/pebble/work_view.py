"""Phase 46 (1.9.29, D206, w24-genui-a): the work view, the `view` of each run_activity item, for windows only.

The person asked to see what Pebble does as cards, not rows of text ("show the things doing by pebble instead of just
text, to show its working window"). Each timeline item gains a typed, bounded `view` built here from Pebble's own
records: the tool ledger (run_tools), the action ledger (actions) and the run's content-free events. No model is asked
and nothing here reaches a model request: views are display data, sent only in `activity` events and run_activity
replies (test_work_view_46 pins both).

Cost. ActivityFeed.items() reads the timeline again at every step (Phase 22), benchmark runs included. A view is built
once, when its item first appears or its record changes (the cache key: the item's status, its tool's state and its
action's status), and kept per run beside the feed's other per-run memory (RETAINED_RUNS). A step builds only the views
of its new or changed items, with one bounded read of their tool and action rows (VIEW_SQL, ACTIONS_SQL); every other
item reuses its view. PEBBLE_WORK_VIEW=0 turns building off, the control arm that measures this cost.

Redaction, as run_activity's: plain text only; no prompt, checkpoint, held draft, reasoning text or credential. Command
lines and output pass the terminal_text projection already, then SECRET and the masks below; a URL keeps only its host
name; a private file (git_workflows.private: keys, .env) is never named. Text that came from the project, a page, a
window or a model is marked in SPEC (`untrusted`), and renderers show it as literal text, never as a link or a button.

`SPEC` is the single source of each view type's fields and bounds; scripts/generate_ui_spec.py derives the published
JSON Schema (pebble-ui-spec v1) from it, so the schema cannot drift from what this module builds.
"""

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from urllib.parse import urlsplit

from .interaction import SECRET

VERSION = 1
VIEW_BYTES = 4096  # one view as JSON; past it, lists lose items from the end and `more` is set
TAIL_LINES, LINE_CHARS, COMMAND_CHARS = 8, 160, 400
TEXT_CHARS = 200
FAILING_NAMES, SITES, REQUIREMENTS, PLAN_STEPS = 10, 5, 10, 20
OUTPUT_TAIL = 4000  # characters of a command's output read for its tail (the projection is already bounded)

# One view type -> {field: (kind, bound, untrusted)}. Kinds: text, int, num, bool, enum (bound: the values), list
# (bound: (max items, item spec)), object (bound: its own field map), null allowed for every field but `type`.
# The generator, the bounds check (bounded()) and the renderers' tests all read this table.
STATES = ('waiting', 'running', 'done', 'failed', 'declined', 'held', 'undone', 'stopped')
SPEC = {
    'read': {'path': ('text', TEXT_CHARS, True), 'start': ('int', None, False), 'total': ('int', None, False),
             'private': ('bool', None, False)},
    'search': {'tool': ('enum', ('Grep', 'Glob', 'SearchFiles', 'CodeReferences'), False),
               'query': ('text', 120, True), 'hits': ('int', None, False)},
    'web': {'query': ('text', 120, True), 'sites': ('list', (SITES, ('text', 120, True)), False)},
    'edit': {'path': ('text', TEXT_CHARS, True), 'added': ('int', None, False), 'removed': ('int', None, False),
             'new': ('bool', None, False), 'private': ('bool', None, False), 'state': ('enum', STATES, False),
             'action_id': ('text', 64, False)},
    'command': {'command': ('text', COMMAND_CHARS, True), 'where': ('text', 80, True), 'network': ('bool', None, False),
                'state': ('enum', STATES, False), 'exit': ('int', None, False), 'seconds': ('num', None, False),
                'tail': ('list', (TAIL_LINES, ('text', LINE_CHARS, True)), False), 'cut': ('bool', None, False),
                'action_id': ('text', 64, False)},
    'tests': {'recipe': ('text', 40, False), 'state': ('enum', STATES, False), 'passed': ('int', None, False),
              'failed': ('int', None, False), 'skipped': ('int', None, False), 'errors': ('int', None, False),
              'total': ('int', None, False), 'failing': ('list', (FAILING_NAMES, ('text', LINE_CHARS, True)), False),
              'exit': ('int', None, False), 'seconds': ('num', None, False), 'action_id': ('text', 64, False)},
    'git': {'op': ('text', 24, False), 'summary': ('text', 240, True), 'branch': ('text', 120, True),
            'upstream': ('text', 120, True), 'ahead': ('int', None, False), 'behind': ('int', None, False),
            'changed': ('int', None, False), 'commit': ('text', 12, False), 'push_to': ('text', 64, True),
            'destructive': ('bool', None, False), 'publication': ('bool', None, False),
            'state': ('enum', STATES, False), 'action_id': ('text', 64, False)},
    'window': {'app': ('text', 64, True), 'title': ('text', 60, True), 'verb': ('text', 40, False),
               'target': ('text', 60, True), 'before': ('text', 40, True), 'after': ('text', 40, True),
               'verdict': ('enum', ('happened', 'nothing_changed', 'uncertain', 'refused'), False),
               'picture': ('object', {'ref': ('text', 64, False), 'width': ('int', None, False),
                                      'height': ('int', None, False)}, False),
               'point': ('object', {'x': ('num', None, False), 'y': ('num', None, False)}, False)},
    'agent': {'name': ('text', 60, True), 'state': ('enum', STATES, False), 'depth': ('int', None, False),
              'parent': ('text', 64, False), 'seconds': ('num', None, False), 'tokens': ('int', None, False)},
    'checks': {'verdict': ('text', 80, False),
               'tests': ('object', {'runner': ('text', 40, False), 'passed': ('int', None, False),
                                    'failed': ('int', None, False), 'current': ('bool', None, False)}, False),
               'files_changed': ('int', None, False), 'issues': ('int', None, False),
               'repairable': ('bool', None, False),
               'requirements': ('list', (REQUIREMENTS, ('object', {'text': ('text', 160, True),
                                                                    'state': ('enum', ('met', 'open'), False)})),
                                False),
               'unmet': ('int', None, False), 'repaired': ('bool', None, False)},
    'review': {'action_id': ('text', 64, False), 'action': ('text', 24, False),
               'state': ('enum', STATES, False), 'decided_by': ('enum', ('you', 'pebble', 'mode'), False),
               'quote': ('text', 200, False)},
    'question': {'questions': ('int', None, False), 'state': ('enum', STATES, False)},
}
# The run_activity reply's own parts: `header` (always) and `summary` (once the run settled).
HEADER = {'plan': ('object', {'done': ('int', None, False), 'total': ('int', None, False),
                              'steps': ('list', (PLAN_STEPS, ('object', {
                                  'title': ('text', 120, True),
                                  'status': ('enum', ('pending', 'in_progress', 'blocked', 'completed'), False)})),
                                        False)}, False),
          'state': ('text', 32, False), 'elapsed': ('num', None, False)}
SUMMARY = {'files': ('object', {'count': ('int', None, False), 'added': ('int', None, False),
                                'removed': ('int', None, False)}, False),
           'commands': ('object', {'count': ('int', None, False), 'failed': ('int', None, False)}, False),
           'tests': ('object', {'passed': ('int', None, False), 'failed': ('int', None, False)}, False),
           'checks': ('text', 80, False), 'seconds': ('num', None, False),
           'tokens': ('object', {'input': ('int', None, False), 'cached': ('int', None, False),
                                 'output': ('int', None, False)}, False)}

READS = {'Read', 'ReadFile', 'ReadAttachment'}
SEARCHES = {'Grep', 'Glob', 'SearchFiles', 'CodeReferences'}
WEB = {'WebSearch', 'ReadURL', 'ReadSource'}
EDITS = {'Write', 'Edit', 'Append', 'WriteFile', 'EditFile'}
COMMANDS = {'RunCommand', 'Shell'}
GIT = {'ProposeGitChange', 'ReadGitStatus', 'ReadGitPlanReceipt'}
WINDOW = {'ScreenObserve', 'ScreenAct'}
QUESTIONS = {'AskUserQuestion'}
TOOLS = READS | SEARCHES | WEB | EDITS | COMMANDS | GIT | WINDOW | QUESTIONS | {'RunTests'}

_ARG = "CASE WHEN json_valid(t.arguments) THEN json_extract(t.arguments,'$.{0}') END AS {1}"
_RES = "CASE WHEN json_valid(t.result) THEN json_extract(t.result,'$.{0}') END AS {1}"
# Only the fields views use, for the calls whose view is new or changed (never the whole timeline). SQLite reads the
# JSON in C; no Read excerpt, observation tree or command output beyond its tail reaches Python.
ARGUMENTS = (('file_path', 'a_path'), ('path', 'a_path2'), ('query', 'a_query'), ('pattern', 'a_pattern'),
             ('url', 'a_url'), ('command', 'a_command'), ('x', 'a_x'), ('y', 'a_y'), ('action', 'a_action'),
             ('operation', 'a_op'))
RESULTS = (('action_id', 'r_action'), ('status', 'r_status'), ('path', 'r_path'), ('start_line', 'r_start'),
           ('total_lines', 'r_total'), ('error', 'r_error'), ('url', 'r_url'), ('exit_code', 'r_exit'),
           ('branch', 'r_branch'), ('upstream', 'r_upstream'), ('ahead', 'r_ahead'), ('behind', 'r_behind'),
           ('total', 'r_changed'), ('app', 'r_app'), ('window.title', 'r_title'), ('window.origin', 'r_origin'),
           ('observation_id', 'r_observation'), ('screenshot.width', 'r_width'), ('screenshot.height', 'r_height'),
           ('target', 'r_target'), ('action', 'r_act'), ('verdict', 'r_verdict'), ('refused', 'r_refused'),
           ('changes[0].before', 'r_before'), ('changes[0].after', 'r_after'), ('summary', 'r_summary'),
           ('operation', 'r_op'), ('state', 'r_state'))
PAYLOAD = (('path', 'p_path'), ('baseline', 'p_baseline'), ('command', 'p_command'), ('network', 'p_network'),
           ('plan.steps[0].cwd', 'p_cwd'), ('recipe', 'p_recipe'), ('operation', 'p_op'), ('summary', 'p_summary'),
           ('destructive', 'p_destructive'), ('publication', 'p_publication'), ('bindings.head_ref', 'p_head'),
           ('remote', 'p_remote'), ('result.exit_code', 'p_exit'), ('result.interrupted', 'p_interrupted'),
           ('result.output_cut', 'p_cut'), ('result.tests', 'p_tests'), ('result.outcome', 'p_outcome'),
           ('result.commit', 'p_commit'), ('result.cancelled', 'p_cancelled'))
# Every column of a VIEW_SQL row and an ACTIONS_SQL row (ui_spec builds its fixtures from rows of these names).
ROW_COLUMNS = ('call_id', 'name', 'state', *(a for _, a in ARGUMENTS), 'a_questions', *(a for _, a in RESULTS),
               'r_output', 'r_hits', 'r_results')
ACTION_COLUMNS = ('id', 'kind', 'status', *(a for _, a in PAYLOAD), 'p_output', 'p_diff')
VIEW_SQL = ('SELECT t.call_id,t.name,t.state,' + ','.join(
    [_ARG.format(k, a) for k, a in ARGUMENTS]
    + ["CASE WHEN json_valid(t.arguments) AND json_type(t.arguments,'$.questions')='array' THEN "
       "json_array_length(t.arguments,'$.questions') END AS a_questions"]
    + [_RES.format(k, a) for k, a in RESULTS]
    + ["CASE WHEN json_valid(t.result) THEN substr(json_extract(t.result,'$.output'),-%d) END AS r_output" % OUTPUT_TAIL,
       "CASE WHEN json_valid(t.result) THEN coalesce(json_array_length(t.result,'$.untrusted_matches'),"
       "json_array_length(t.result,'$.files'),json_array_length(t.result,'$.results'),"
       "json_array_length(t.result,'$.untrusted_results')) END AS r_hits",
       "CASE WHEN json_valid(t.result) AND json_type(t.result,'$.untrusted_search_results')='array' THEN "
       "json_extract(t.result,'$.untrusted_search_results') END AS r_results"])
    + ' FROM t WHERE 0')  # completed by _rows: the FROM clause names run_tools and the call ids
_PAY = "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.{0}') END AS {1}"
ACTIONS_SQL = ('SELECT id,kind,status,' + ','.join(
    [_PAY.format(k, a) for k, a in PAYLOAD]
    + ["CASE WHEN json_valid(payload) THEN substr(json_extract(payload,'$.result.output'),-%d) END AS p_output"
       % OUTPUT_TAIL,
       # an edit's unified diff, whose +/- lines are counted once per action and status (diff_counts)
       "CASE WHEN json_valid(payload) AND json_type(payload,'$.diff')='text' THEN json_extract(payload,'$.diff') "
       "END AS p_diff"])
    + ' FROM actions WHERE 0')

_MASKS = ((re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'), '***'),
          (re.compile(r'(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}'), r'\1***'),
          (re.compile(r'(?i)\b((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\s*[=:]\s*)[^\s\'"]+'),
           r'\1***'),
          (re.compile(r'([a-z][a-z0-9+.-]*://)[^/\s@]+@', re.I), r'\1***@'))  # a URL's user and password
# controls, line and paragraph separators and bidi overrides, built from code points so this source holds none
_CONTROL = re.compile('[\\x00-\\x08\\x0b-\\x1f\\x7f' + ''.join(
    chr(a) + '-' + chr(b) for a, b in ((0x2028, 0x2029), (0x202a, 0x202e), (0x2066, 0x2069))) + ']')
# Failing test names, display only (the pass/fail counts and the gate never read these)
_FAILING = (re.compile(r'^FAILED\s+(\S+)', re.M), re.compile(r'^(?:FAIL|ERROR):\s+(\S+(?:\s+\([^)\n]+\))?)', re.M),
            re.compile(r'^\s*--- FAIL:\s+(\S+)', re.M), re.compile('^\\s*[' + chr(0x2715) + chr(0xd7) + ']\\s+(.+)$', re.M),
            re.compile(r'^\s*not ok \d+ - (.+)$', re.M))


def enabled():
    """Views are built unless PEBBLE_WORK_VIEW=0 (a development control arm, as PEBBLE_CONTEXT_SEAL=0 is)."""
    return os.environ.get('PEBBLE_WORK_VIEW', '1') != '0'


def masked(text):
    """Secrets out of display text: SECRET's keys and tokens, cloud keys, bearer tokens, NAME=value credentials, and a
    URL's user and password. The rest stays as Pebble already shows it."""
    text = SECRET.sub('***', text)
    for mask, shown in _MASKS:
        text = mask.sub(shown, text)
    return text


def _path(value):
    """A path as the person reads it: the home folder as ~ (a Companion file), never the full home path."""
    home = os.path.expanduser('~')
    if isinstance(value, str) and home not in ('', '/') and (value == home or value.startswith(home + '/')):
        value = '~' + value[len(home):]
    return _text(value, TEXT_CHARS)


def _text(value, limit):
    """One line of plain display text, masked, at most `limit` characters (an ellipsis marks a cut), else None."""
    if not isinstance(value, str):
        return None
    value = ' '.join(_CONTROL.sub(' ', masked(value)).split())
    if not value:
        return None
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def _int(value):
    return value if type(value) is int and value >= 0 else None


def _num(value):
    return round(float(value), 1) if type(value) in (int, float) and value >= 0 else None


def _short(commit):
    """A commit's short hash (12 characters), or None for anything that is not one."""
    return commit[:12] if isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{7,64}', commit) else None


def _private(path):
    from .git_workflows import private
    try:
        return bool(path) and private(path)
    except Exception:
        return True  # a path the check cannot read is not named


def _host(url):
    try:
        host = urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        return None
    return _text(host[4:] if host and host.startswith('www.') else host, 120)


def _state(action, exit_code=None):
    status = (action or {}).get('status')
    if status in (None, 'pending'):
        return 'waiting'
    if status == 'running':
        return 'running'
    if status == 'applied':
        return 'failed' if type(exit_code) is int and exit_code != 0 else 'done'
    return {'rejected': 'declined', 'superseded': 'declined', 'held': 'held', 'undone': 'undone'}.get(status, 'failed')


def tail(output, lines=TAIL_LINES):
    """The last `lines` non-empty lines of command output, each masked and at most LINE_CHARS characters."""
    if not isinstance(output, str):
        return []
    found = [line for line in (_text(raw, LINE_CHARS) for raw in output.splitlines()[-lines * 4:]) if line]
    return found[-lines:]


def failing(output):
    """Up to FAILING_NAMES failing test names printed by pytest, unittest, go, jest or TAP; display only."""
    text = output if isinstance(output, str) else ''
    found = sorted((match.start(), match.group(1)) for pattern in _FAILING for match in pattern.finditer(text))
    names = []
    for _, raw in found:  # in the order the output printed them
        name = _text(raw, LINE_CHARS)
        if name and name not in names:
            names.append(name)
    return names[:FAILING_NAMES]


def diff_counts(diff):
    """(+lines, -lines) of a unified diff, headers excluded."""
    added = removed = 0
    for line in diff.splitlines() if isinstance(diff, str) else ():
        if line.startswith('+') and not line.startswith('+++'):
            added += 1
        elif line.startswith('-') and not line.startswith('---'):
            removed += 1
    return added, removed


def _view(kind, **fields):
    return {'v': VERSION, 'type': kind, **{k: v for k, v in fields.items() if v is not None}}


# -- the builders: one record in, one bounded view out (or None: the item keeps today's row) ----------------------

def tool_view(row, action, started=None, ended=None):
    """The view of one tool item from its VIEW_SQL row and, when it staged one, its ACTIONS_SQL row."""
    name = row['name']
    seconds = _num(ended - started) if type(started) in (int, float) and type(ended) in (int, float) else None
    if name in READS:
        path = row['r_path'] or row['a_path'] or row['a_path2']
        if _private(path):
            return _view('read', private=True)
        return _view('read', path=_path(path), start=_int(row['r_start']), total=_int(row['r_total']))
    if name in SEARCHES:
        return _view('search', tool=name, query=_text(row['a_query'] or row['a_pattern'], 120), hits=_int(row['r_hits']))
    if name in WEB:
        sites = []
        try:
            results = json.loads(row['r_results']) if isinstance(row['r_results'], str) else []
        except ValueError:
            results = []
        for url in [row['r_url'] or row['a_url']] + [r.get('url') for r in results if isinstance(r, dict)]:
            host = _host(url)
            if host and host not in sites:
                sites.append(host)
        return _view('web', query=_text(row['a_query'], 120), sites=sites[:SITES])
    if name in EDITS:
        path = (action or {}).get('p_path') or row['a_path']
        added, removed = diff_counts((action or {}).get('p_diff'))
        hidden = _private(path)
        return _view('edit', path=None if hidden else _path(path), private=hidden or None,
                     added=added if action else None, removed=removed if action else None,
                     new=action is not None and action.get('p_baseline') is None or None,
                     state=_state(action) if action else 'failed' if row['r_error']
                     else 'running' if row['state'] in ('planned', 'running') else None,
                     action_id=(action or {}).get('id'))
    if name in COMMANDS:
        action = action or {}
        exit_code = action.get('p_exit') if action else row['r_exit']
        output = action.get('p_output') if action else row['r_output']
        state = _state(action, exit_code) if action else ('done' if exit_code == 0 else 'failed' if row['state'] == 'completed' else 'running')
        if action.get('p_interrupted') or action.get('p_cancelled'):
            state = 'stopped'
        cwd = action.get('p_cwd')
        return _view('command', command=_text(action.get('p_command') or row['a_command'], COMMAND_CHARS),
                     where='Project folder' + (' › ' + _text(cwd, 60) if isinstance(cwd, str) and cwd not in ('.', '') else '')
                     if action else None, network=bool(action.get('p_network')) if action else None, state=state,
                     exit=exit_code if type(exit_code) is int else None,
                     seconds=seconds if state not in ('waiting', 'running') else None,
                     tail=tail(output) or None, cut=bool(action.get('p_cut')) or None, action_id=action.get('id'))
    if name == 'RunTests':
        action = action or {}
        try:
            counts = json.loads(action['p_tests']) if isinstance(action.get('p_tests'), str) else {}
        except ValueError:
            counts = {}
        counts = counts if isinstance(counts, dict) else {}
        exit_code = action.get('p_exit')
        state = _state(action, exit_code) if action else None
        return _view('tests', recipe=_text(action.get('p_recipe'), 40), state=state,
                     **{k: _int(counts.get(k)) for k in ('passed', 'failed', 'skipped', 'errors', 'total')},
                     failing=failing(action.get('p_output')) or None, exit=exit_code if type(exit_code) is int else None,
                     seconds=seconds if state not in (None, 'waiting', 'running') else None, action_id=action.get('id'))
    if name in GIT:
        if name == 'ReadGitStatus':
            return _view('git', op='status', branch=_text(row['r_branch'], 120), upstream=_text(row['r_upstream'], 120),
                         ahead=_int(row['r_ahead']), behind=_int(row['r_behind']), changed=_int(row['r_changed']))
        action = action or {}
        head = action.get('p_head')
        return _view('git', op=_text(action.get('p_op') or row['r_op'] or row['a_op'], 24),
                     summary=_text(action.get('p_summary') or row['r_summary'], 240),
                     branch=_text(head[11:] if isinstance(head, str) and head.startswith('refs/heads/') else None, 120),
                     commit=_short(action.get('p_commit')), push_to=_text(action.get('p_remote'), 64),
                     destructive=bool(action['p_destructive']) if action.get('p_destructive') is not None else None,
                     publication=bool(action['p_publication']) if action.get('p_publication') is not None else None,
                     state=_state(action) if action else None, action_id=action.get('id'))
    if name in WINDOW:
        from .run_activity import ACT_VERBS
        refused = row['r_refused']
        verdict = ('refused' if refused else 'happened' if row['r_verdict'] == 'occurred'
                   else 'nothing_changed' if row['r_verdict'] == 'not_observed'
                   else 'uncertain' if row['r_verdict'] else None)
        width, height = _int(row['r_width']), _int(row['r_height'])
        x, y = row['a_x'], row['a_y']
        return _view('window', app=_text(row['r_app'], 64), title=_text(row['r_title'] or _host(row['r_origin']), 60),
                     verb=ACT_VERBS.get(row['r_act'] or row['a_action']) if name == 'ScreenAct' else 'Looked',
                     target=_text(row['r_target'], 60), before=_text(row['r_before'], 40), after=_text(row['r_after'], 40),
                     verdict=verdict if name == 'ScreenAct' else None,
                     # the picture never travels in a view: only which observation holds it (kept in memory only)
                     picture={'ref': row['r_observation'][:64], 'width': width, 'height': height}
                     if isinstance(row['r_observation'], str) and width and height else None,
                     point={'x': float(x), 'y': float(y)} if type(x) in (int, float) and type(y) in (int, float) else None)
    if name in QUESTIONS:
        return _view('question', questions=_int(row['a_questions']),
                     state='done' if row['state'] == 'completed' and row['r_status'] != 'awaiting_user_answer' else 'waiting')
    return None


def review_view(action, kind, decided, quote):
    """An approval item's view: the card's state in place (the card itself stays the window's own)."""
    state = _state(action)
    by = ('pebble' if isinstance(quote, str) else 'you' if decided is not None else 'mode') \
        if state in ('done', 'failed', 'undone') else 'you' if state == 'declined' and decided is not None else None
    return _view('review', action_id=(action or {}).get('id'), action=_text(kind, 24), state=state, decided_by=by,
                 quote=_text(quote, 200) if by == 'pebble' else None)


def agent_view(node, status):
    return _view('agent', name=_text(node.get('name'), 60), state={'running': 'running', 'failed': 'failed'}.get(status, 'done'),
                 depth=_int(node.get('depth')), parent=_text(node.get('parent_id'), 64),
                 seconds=_num(node.get('elapsed')), tokens=_int(node.get('charged_tokens')))


REQUIREMENT_WORDS = {'tests_pass': 'The whole test suite passes', 'read_only': 'Nothing in the project changes',
                     'generated_removed': 'Generated files are removed'}


def requirement_text(requirement):
    """A requirement of AgentGuard's ledger (requirements.extract) in plain words; None for one the gate does not hold
    (a soft or bound golden is soft_checks' own)."""
    kind = requirement.get('kind')
    if requirement.get('soft') or kind == 'golden':
        return None
    if kind == 'output_equals':
        return _text(f'The output of {requirement.get("command")} matches {requirement.get("path")}', 160)
    if kind == 'unchanged':
        paths = [p for p in requirement.get('paths') or () if isinstance(p, str) and not _private(p)]
        if requirement.get('scope') == 'tests' and not paths:
            return 'The tests stay unchanged'
        named = ', '.join(paths[:3]) + (f' and {len(paths) - 3} more' if len(paths) > 3 else '')
        return _text((named or 'The named files') + (' stays' if len(paths) == 1 else ' stay') + ' unchanged', 160)
    if kind == 'kept_ref':
        names = [n for n in requirement.get('names') or () if isinstance(n, str)]
        return _text(('Branch ' if len(names) == 1 else 'Branches ') + ', '.join(names[:3]) + ' kept', 160) if names \
            else 'The named branches are kept'
    return REQUIREMENT_WORDS.get(kind, 'A requirement Pebble checks')


def checks_view(label, data, gate, ledger, unmet, earlier_unmet):
    """A completion check's view: the verdict line, the tests it counted and AgentGuard's requirement ledger. Each
    requirement reads 'met' when the check found none unmet, else 'open': the records keep how many issues were unmet,
    not which requirement each belongs to, so no requirement is called unmet that the check did not name (the chat
    shows the check's own sentences)."""
    from .run_activity import verdict
    tests = gate.get('tests') if isinstance(gate, dict) and isinstance(gate.get('tests'), dict) else None
    texts = [t for t in (requirement_text(r) for r in (ledger or ()) if isinstance(r, dict)) if t][:REQUIREMENTS]
    if not isinstance(gate, dict) and not texts and not data.get('issues'):
        return None  # "Checked the reply" with nothing to show stays the folded row it is today
    return _view('checks', verdict=_text(verdict(gate) or label, 80),
                 tests={'runner': _text(tests.get('runner'), 40) or 'tests', 'passed': _int(tests.get('passed')) or 0,
                        'failed': _int(tests.get('failed')) or 0, 'current': gate.get('snapshot_current') is True}
                 if tests else None,
                 files_changed=_int((gate or {}).get('files_changed')), issues=_int(data.get('issues')) or 0,
                 repairable=bool(data.get('repairable')) if data.get('issues') else None,
                 requirements=[{'text': t, 'state': 'open' if unmet else 'met'} for t in texts] or None,
                 unmet=unmet or None, repaired=True if texts and not unmet and earlier_unmet else None)


def bounded(view, spec=None):
    """The view within its SPEC bounds and VIEW_BYTES: lists lose items from the end, setting `more`."""
    spec = spec or SPEC[view['type']]
    for key, (kind, bound, _) in spec.items():
        value = view.get(key)
        if kind == 'list' and isinstance(value, list) and len(value) > bound[0]:
            view[key], view['more'] = value[:bound[0]], True
    while len(json.dumps(view, ensure_ascii=False)) > VIEW_BYTES:
        lists = [k for k, (kind, _, _) in spec.items() if kind == 'list' and view.get(k)]
        if not lists:
            break
        view[lists[0]] = view[lists[0]][:-1]
        view['more'] = True
    return view


# -- the cache: built once per item and record change --------------------------------------------------------------

def _rows(store, rid, calls):
    rows = {}
    calls = sorted(calls)
    for start in range(0, len(calls), 200):
        chunk = calls[start:start + 200]
        sql = VIEW_SQL.replace(' FROM t WHERE 0', ' FROM run_tools t WHERE t.run_id=? AND t.call_id IN (%s)'
                               % ','.join('?' * len(chunk)))
        rows.update({r['call_id']: r for r in store.query(sql, (rid, *chunk))})
    return rows


def _actions(store, ids):
    found = {}
    ids = sorted(ids)
    for start in range(0, len(ids), 200):
        chunk = ids[start:start + 200]
        sql = ACTIONS_SQL.replace(' FROM actions WHERE 0', ' FROM actions WHERE id IN (%s)' % ','.join('?' * len(chunk)))
        found.update({r['id']: r for r in store.query(sql, chunk)})
    return found


def _digest(view):
    return hashlib.blake2b(json.dumps(view, sort_keys=True).encode(), digest_size=8).hexdigest() if view else None


class Views:
    """Per run: {seq: (key, view, digest)}, for the newest RETAINED_RUNS runs (the feed's own bound). `builds` counts
    the views built, so a test can hold the work to one build per new or changed item."""

    def __init__(self, retained):
        self.retained, self.runs, self.chats, self.builds = retained, OrderedDict(), {}, 0
        self.lock = threading.Lock()  # the run's thread publishes while a window reads

    def attach(self, store, cid, rid, found, sources, tools):
        """Give each item in `found` ({seq: item}) its view. `sources`: {seq: (origin, key, build)} where build()
        returns the view, or ('tool', key, call, started, ended) for a tool item, whose rows are read together, once.
        A source whose key is unchanged since its view was built reuses that view. Never raises: display only."""
        with self.lock:
            try:
                self._attach(store, cid, rid, found, sources, tools)
            except Exception:
                return

    def _attach(self, store, cid, rid, found, sources, tools):
        cache = self.runs.get(rid)
        if cache is None:
            cache = self.runs[rid] = {}
            self.chats[rid] = cid
            while len(self.runs) > self.retained:
                self.chats.pop(self.runs.popitem(last=False)[0], None)
        self.runs.move_to_end(rid)
        missing = {seq: source for seq, source in sources.items() if seq in found
                   and (seq not in cache or cache[seq][0] != source[1])}
        calls = {s[2] for s in missing.values() if s[0] == 'tool' and s[2] in tools and tools[s[2]]['name'] in TOOLS}
        rows = _rows(store, rid, calls) if calls else {}
        staged = {rows[c]['r_action'] for c in rows if isinstance(rows[c]['r_action'], str)}
        acted = _actions(store, staged) if staged else {}
        for seq, source in missing.items():
            try:
                if source[0] == 'tool':
                    row = rows.get(source[2])
                    view = tool_view(row, acted.get(row['r_action']), source[3], source[4]) if row else None
                else:
                    view = source[2]()
                view = bounded(view) if view else None
            except Exception:
                view = None  # display only: an unreadable record keeps today's row
            self.builds += 1
            cache[seq] = (source[1], view, _digest(view))
        for seq, item in found.items():
            entry = cache.get(seq)
            if entry and entry[1]:
                item['view'] = entry[1]

    def digests(self, rid):
        """{seq: digest of its view} for one run, for the publish signature (an item is sent again when its view
        changes)."""
        with self.lock:
            return {seq: entry[2] for seq, entry in self.runs.get(rid, {}).items()}

    def forget_chat(self, cid):
        with self.lock:
            for rid in [r for r, owner in self.chats.items() if owner == cid]:
                self.runs.pop(rid, None)
                self.chats.pop(rid, None)


# -- the reply's header and the settled run's summary --------------------------------------------------------------

def header(plan, state, elapsed):
    """{plan: {done, total, steps}, state, elapsed}: the work view's head. The task line is the person's own message,
    which the window already holds; it is never read here (run_activity reads no prompt)."""
    steps = [s for s in (plan or {}).get('steps') or () if isinstance(s, dict)]
    shown = [{'title': _text(s.get('title'), 120) or 'Step', 'status': s.get('status')}
             for s in steps if s.get('status') in ('pending', 'in_progress', 'blocked', 'completed')][:PLAN_STEPS]
    return {'plan': {'done': sum(s['status'] == 'completed' for s in shown), 'total': len(shown), 'steps': shown}
            if shown else None, 'state': state, 'elapsed': round(elapsed, 1)}


def summary(items, usage, seconds):
    """The end-of-run receipt as data: files changed (+/-), commands, tests, the last check's verdict, time and tokens.
    Pebble records no price (delegation_policy.PRICE_NOTE), so cost is the tokens."""
    files, commands, tests, verdict_line = {}, [0, 0], None, None
    for item in items:
        view = item.get('view') or {}
        if view.get('type') == 'edit' and view.get('state') == 'done':
            key = view.get('path') or 'private:%d' % item['seq']
            added, removed = files.get(key, (0, 0))
            files[key] = (added + (view.get('added') or 0), removed + (view.get('removed') or 0))
        elif view.get('type') == 'command' and view.get('state') in ('done', 'failed', 'stopped'):
            commands[0] += 1
            commands[1] += view['state'] != 'done'
        elif view.get('type') == 'tests' and view.get('state') in ('done', 'failed'):
            tests = {'passed': view.get('passed') or 0, 'failed': view.get('failed') or 0}  # the last run counts
        elif view.get('type') == 'checks':
            verdict_line = view.get('verdict')
    tokens = {k: _int((usage or {}).get(k)) or 0 for k in ('input', 'cached', 'output')} if usage else None
    return {'files': {'count': len(files), 'added': sum(a for a, _ in files.values()),
                      'removed': sum(r for _, r in files.values())} if files else None,
            'commands': {'count': commands[0], 'failed': commands[1]} if commands[0] else None,
            'tests': tests, 'checks': verdict_line, 'seconds': round(seconds, 1), 'tokens': tokens}
