"""Phase 22 (t22-stream): a run's activity timeline and live reasoning summaries, for display only.

Items come from host records alone: content-free run events, tool records, the action ledger and agent nodes. Nothing
here reads a checkpoint, a committed response, a held or withdrawn draft, a prompt or a credential; every string is
display data, plain text. Reasoning-summary text lives only in this process's memory (bounded per run and in the
number of runs), never in the database, a memory layer, an export or model context. A step whose text is held (an
approval hold, a buffered step) keeps its summary unpublished in the same way, until its text is shown.

`seq` is an item's identity: SLOTS times the sequence of the run event that anchors it, plus a slot (0 the item, 1 a
model attempt's connect phase, 2 its reasoning, 3 and up the children anchored there: by their spawn call, else by
the last event at or before their creation, in creation order). The same item has the same seq in every live
`activity` event and every run_activity read, and an update (status, duration) is re-sent with its seq.
run_activity(after=N) returns the items above N and every item still running.

Model items carry two more fields than the contract's seven: `headers_ms` (the request's start to its response
headers) and `first_event_ms` (to the stream's first line), integers or null.

Phase 46 (1.9.29, D206): in live events and run_activity replies an item may also carry `view`, its work view: typed,
bounded card data built once per item from the same host records (work_view.py, pebble-ui-spec v1). A window that does
not know a view's type shows the item's label and detail as before. The reply adds `header` (plan progress, state,
elapsed time) and, once the run settled, `summary` (its receipt as data).
"""

import json
import re
import threading
import time
import uuid
from collections import OrderedDict

from . import work_view
from .stats import add_report

KINDS = ('context', 'model', 'reasoning', 'tool', 'retry', 'approval', 'child', 'check', 'note')
LABEL_CHARS, DETAIL_CHARS, REASONING_CHARS = 80, 2000, 20_000
PAGE_ITEMS = 400
RETAINED_RUNS = 32
SLOTS = 64  # seq slots per anchoring run event; an agent tree holds at most eight nodes
CONNECT_SLOT, REASONING_SLOT, CHILD_SLOT = 1, 2, 3
CONNECT_SHOWN = 1.0  # a connect phase this long or longer gets its own item
REASONING_EVENT_CHARS = 2000  # a released summary is sent in pieces of at most this size
LIVE = {'queued', 'running', 'retry_wait'}
WAITING = {'waiting_approval', 'waiting_input'}  # Phase 46 (1.9.29): not settled, so no receipt summary yet
EVENT_KINDS = ('state', 'context_ready', 'model_attempt_started', 'model_connecting', 'model_attempt', 'model_retry',
               'tool_authorized', 'tool_refused', 'tool_started', 'tool_finished', 'action_bound', 'approval_started',
               'completion_checked', 'reply_withheld', 'completion_gate', 'held_decision',
               'route_history_withheld',  # Phase 24 (V1-002, t24-data): read for its note below
               'step_judged', 'step_held',  # Phase 45 (D141, F221): Pebble's own check of a step (it was never read)
               'confined_command',  # Phase 46 (1.9.28, D184): what a command changed that the request keeps
               'requirements', 'requirements_unmet')  # Phase 46 (1.9.29): AgentGuard's ledger, for the checks view
# Phase 45 (D141, audit round R9): why Pebble's own check kept a card for a step
STEP_HELD = {'spends': 'it may spend money', 'not_asked': "your messages don't ask for this step",
             'check_failed': 'its check of your messages could not run', 'no_messages': 'there was no message to check',
             # Phase 45 (F241, audit round R10)
             'quote_unmatched': "it could not match your words to this step",
             # Phase 46 (1.9.28, D185): a step that deletes, overwrites, publishes or uses the network
             'not_named': "your words don't name this step or its target"}
# Phase 46 (1.9.28, D185): a held step went back to Pebble (no card), or waits for the person after repeated holds
HELD_BACK = 'Pebble went on without a step: '
HELD_STUCK = {'row': 'Pebble held 3 steps in a row, so this one waits for you',
              'total': 'Pebble held 20 steps in this task, so this one waits for you'}
HELD_FROM_DATA = "Its words came from a file, a page or a tool's output, not from you."
HELD_TOOL = "Pebble's check held this step; it did not run."
# Phase 45 (F238, audit round R10): a refused screen act's detail spoke the model's words ("HOST CAPABILITIES", "element
# refs", "pixel act"); the person reads what Pebble did not do and why
SCREEN_REFUSED = {
    'stale_ui': "Pebble didn't act: the window had changed since its last look.",
    'unknown_observation': "Pebble didn't act: it needed a fresh look at the window first.",
    'covered': "Pebble didn't click: something else was in front of that spot.",
    'no_screenshot': "Pebble didn't click: its last look had no picture of the window to aim at.",
    'outside_target': "Pebble didn't click: that spot is outside the allowed window.",
    'action_not_applicable': "Pebble didn't act: that kind of step doesn't work on this control.",
    'action_not_granted': "Pebble didn't act: that step isn't allowed in this window.",
    'focus_elsewhere': "Pebble didn't type: the keyboard was in another window.",
    'repeat_ineffective': "Pebble didn't repeat a step that had changed nothing.",
    'pixel_unavailable': "Pebble didn't act: it can't use the mouse and keyboard here.",
    'secure_field': 'Pebble never types into password or code fields.',
    'interrupted': "Pebble didn't act: the window is paused until you check it.",
    'already_playing': "Pebble didn't press Play or Pause: something is already playing.",
    'file_panel': "Pebble doesn't use Open or Save panels.",
    'limit': "Pebble didn't act: this window's step limit was reached.",
}
SCREEN_REFUSED_OTHER = "Pebble's check stopped this step; nothing reached the app."
# Phase 24 (V1-029): the person's decision on a held run, as a timeline note.
HELD = {'tests': 'You asked Pebble to run the tests', 'finish': 'You asked Pebble to finish',
        'accept': 'You accepted this as unverified'}


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def verdict(gate):
    """Phase 24 (V1-095): the completion check's one-line receipt from AgentGuard's content-free summary
    ("Verified · unittest 3 passed on current files", "Not verified · 1 changed file, no tests run"), or None when the
    run changed no code (the check then reads as before)."""
    if not isinstance(gate, dict):
        return None
    outcome, reason = gate.get('outcome'), gate.get('reason')
    tests = gate.get('tests') if isinstance(gate.get('tests'), dict) else {}
    runner = tests.get('runner') if isinstance(tests.get('runner'), str) and tests.get('runner') else 'tests'
    passed, failed, changed = _count(tests.get('passed')), _count(tests.get('failed')), _count(gate.get('files_changed'))
    files = f'{changed} changed file{"s" if changed != 1 else ""}'
    if outcome == 'verified':
        return f'Verified · {runner} {passed} passed' + (' on current files' if gate.get('snapshot_current') else '')
    if outcome == 'unavailable':
        return "Not verified · the checker wasn't ready"
    if outcome == 'held' and reason == 'failing_tests':
        return f'Not verified · {runner} {max(failed, 1)} failed'
    if outcome == 'held' and reason == 'unparsable':
        return "Not verified · a changed file doesn't parse"
    if outcome in ('held', 'not_verified') and changed:
        if passed and gate.get('snapshot_current') is False:
            return 'Not verified · tests ran before the last change'
        return f'Not verified · {files}, no tests run' if not passed else f'Not verified · {files}'
    return None
WITHHELD = {'approval': 'Waiting for your decision; the reply follows it',
            'completion_check': 'Draft withdrawn: a check asked for a fix; the reply follows it',
            'promise_check': 'Draft withdrawn: no change was recorded yet; the reply follows it'}
STATE_NOTES = {'waiting_approval': 'Waiting for your approval', 'waiting_input': 'Waiting for your answer',
               'paused': 'Paused', 'stopped': 'Stopped', 'failed': 'The run stopped with an error',
               'budget_exhausted': "Reached this run's budget", 'needs_reconciliation': 'An uncertain step needs your review',
               'interrupted': 'Interrupted when Pebble closed', 'verification_required': 'Verification remains incomplete'}
CHILD_ACTIVE = {'queued', 'running', 'waiting_parent', 'waiting_input'}
CHILD_FAILED = {'failed', 'interrupted', 'stopped', 'budget_exhausted'}
SPAWN_TOOLS = {'SpawnAgent', 'SpawnWorktreeAgent'}
SHOWN = ('live', 'released')
_CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
TOOLS_SQL = ("SELECT call_id,name,state,"
             "CASE WHEN json_valid(arguments) THEN json_extract(arguments,'$.file_path') END AS file_path,"
             "CASE WHEN json_valid(arguments) THEN json_extract(arguments,'$.name') END AS arg_name,"
             # Phase 45 (F148, audit round R4): a script's purpose and length (never its code) name its row
             "CASE WHEN json_valid(arguments) THEN json_extract(arguments,'$.purpose') END AS purpose,"
             "CASE WHEN json_valid(arguments) AND json_type(arguments,'$.code')='text' THEN "
             "length(json_extract(arguments,'$.code'))-length(replace(json_extract(arguments,'$.code'),char(10),''))+1 "
             "END AS code_lines,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.status') END AS status,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.error') END AS error,"
             # Phase 45 (F238, audit round R10): a screen refusal's code, worded for the person (SCREEN_REFUSED)
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.refused') END AS refused_code,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.agent_id') END AS agent_id,"
             # Phase 45 (F182): whether a look at an app window had a screenshot, and why not (a code, never pixels)
             "CASE WHEN json_valid(result) THEN json_type(result,'$.screenshot') END AS screenshot,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.screenshot_unavailable') END AS no_screenshot,"
             # Phase 45 (F204): an act's target, its verdict and its first change (window text, shown as data)
             "CASE WHEN json_valid(result) THEN coalesce(json_extract(result,'$.target'),"
             "json_extract(result,'$.under_point.label')) END AS act_target,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.action') END AS act_action,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.verdict') END AS act_verdict,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.changes[0].before') END AS change_before,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.changes[0].after') END AS change_after,"
             # Phase 45 (F222, audit round R9): a Chess move changes two squares; the second change is shown too
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.changes[1].before') END AS change2_before,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.changes[1].after') END AS change2_after "
             "FROM run_tools WHERE run_id=?")


def _label_args(tool):
    """The arguments a row's label is made from (Phase 45, F148: a script's purpose and length too)."""
    lines = tool.get('code_lines')
    return {'file_path': str(tool['file_path'] or ''), 'name': str(tool['arg_name'] or ''),
            'purpose': str(tool.get('purpose') or ''), **({'code_lines': lines} if type(lines) is int else {})}


# Phase 45 (F204, audit round R7): what an app act did, in words: "Pressed “C”: 100×10 → 100×"
ACT_VERBS = {'press': 'Pressed', 'click_at': 'Clicked', 'type': 'Typed into', 'type_text': 'Typed into',
             'key': 'Pressed a key in', 'enter': 'Pressed Return in', 'select': 'Chose', 'toggle': 'Switched',
             'focus': 'Focused', 'expand': 'Opened', 'collapse': 'Closed', 'scroll': 'Scrolled', 'scroll_at': 'Scrolled at',
             'drag_at': 'Dragged from'}  # F262 (1.9.18)


def act_detail(tool):
    target = str(tool.get('act_target') or '').strip()
    if not target:
        return None
    said = ACT_VERBS.get(tool.get('act_action'), 'Acted on') + ' “' + target[:60] + '”'
    before, after = tool.get('change_before'), tool.get('change_after')
    if isinstance(before, str) and isinstance(after, str) and before != after:
        return f'{said}: {before[:40]} → {after[:40]}'
    if tool.get('act_verdict') == 'not_observed':
        return said + ': nothing changed'
    return said + '.'


# Phase 45 (F207, audit round R8): the row's title is the press itself; its detail, what changed
def act_title(tool):
    target = str(tool.get('act_target') or '').strip()
    return ACT_VERBS.get(tool.get('act_action'), 'Acted on') + ' “' + target[:60] + '”' if target else None


def act_change(tool):
    before, after = tool.get('change_before'), tool.get('change_after')
    if isinstance(before, str) and isinstance(after, str) and before != after:
        said = f'{before[:40] or "(nothing)"} → {after[:40] or "(nothing)"}'
        more, then = tool.get('change2_before'), tool.get('change2_after')  # F222: a move's second square
        if isinstance(more, str) and isinstance(then, str) and more != then:
            said += f'; {more[:40] or "(nothing)"} → {then[:40] or "(nothing)"}'
        return said
    if tool.get('act_verdict') == 'not_observed':
        return 'Nothing changed.'
    return 'The window changed.' if tool.get('act_verdict') == 'occurred' else None


TRACEBACK = 'Traceback (most recent call last):'


# Phase 45 (F180, F182): why a look at an app window had no screenshot, for the person
SCREENSHOT_MISSING = {
    'capture_timeout': 'No screenshot: macOS may be asking whether Pebble may see the screen (choose Allow).',
    'capture_failed': 'No screenshot: macOS may be asking whether Pebble may see the screen (choose Allow).',
    'capture_not_permitted': 'No screenshot: macOS has not allowed Pebble to record the screen.',
    'not_in_view': 'No screenshot: the window was on another desktop or minimized.',
}


def script_failure(error):
    """Phase 45 (F148): a script's Python traceback as one sentence, its last line (the exception); other errors as
    they are."""
    if not isinstance(error, str) or TRACEBACK not in error:
        return error
    last = next((line.strip() for line in reversed(error.splitlines()) if line.strip()), '')
    return 'The script stopped with an error: ' + last if last and last != TRACEBACK else 'The script stopped with an error.'


def plain(text, limit):
    """Display text: control characters become spaces, at most `limit` characters (an ellipsis marks a cut)."""
    if not isinstance(text, str):
        return None
    text = ' '.join(_CONTROL.sub(' ', text).split()) if limit <= LABEL_CHARS else _CONTROL.sub(' ', text).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if limit <= LABEL_CHARS and ' ' in cut[limit // 2:]:  # Phase 45 (F257): a label is cut at a word
        cut = cut.rsplit(' ', 1)[0]
    return cut.rstrip() + '…'


def provider_name(config):
    route = (config or {}).get('route') or {}
    return 'ChatGPT' if route.get('kind') == 'chatgpt' else 'the model'


def _seconds(value):
    return round(float(value), 3) if type(value) in (int, float) and value >= 0 else None


def _whole(seconds):
    return max(1, round(seconds))


class ActivityFeed:
    def __init__(self, store, runs, emit, label=None):
        self.store, self.runs, self.emit = store, runs, emit
        self.label = label or (lambda name, args: name)
        self._lock = threading.RLock()
        self._reasoning = OrderedDict()  # run_id -> {'chat_id', 'attempts': OrderedDict[(step, attempt)] -> record}
        self._sent = OrderedDict()       # run_id -> (chat_id, {seq: signature of the last emitted item})
        self._nodes = None
        self._plans = None
        self._views = work_view.Views(RETAINED_RUNS)  # Phase 46 (1.9.29): each item's work view, built once

    # -- reasoning summaries (memory only) ------------------------------------------------------------------------

    def _run(self, cid, rid):
        record = self._reasoning.get(rid)
        if record is None:
            record = self._reasoning[rid] = {'chat_id': cid, 'attempts': OrderedDict()}
            while len(self._reasoning) > RETAINED_RUNS:
                self._reasoning.popitem(last=False)
        self._reasoning.move_to_end(rid)
        return record

    def reasoning(self, cid, rid, step, attempt, part, *, live, attempt_id=None):
        """Keep one summary delta of an open attempt; publish it now when its step streams live, else hold it."""
        if not isinstance(part, str) or not part:
            return
        first = False
        with self._lock:
            attempts = self._run(cid, rid)['attempts']
            key = (step, attempt)
            entry = attempts.get(key)
            if entry is None:
                entry = attempts[key] = {'id': attempt_id if live and attempt_id else uuid.uuid4().hex, 'parts': [],
                                         'size': 0, 'state': 'live' if live else 'held', 'committed': False,
                                         'shown': live}
                first = True
            if entry['state'] == 'discarded':
                return
            # At most REASONING_CHARS per run: the oldest other attempts go first, then the rest of this one is cut.
            used = sum(e['size'] for e in attempts.values())  # a discarded attempt keeps no text
            while used + len(part) > REASONING_CHARS:
                oldest = next((k for k in attempts if k != key), None)
                if oldest is None:
                    break
                used -= attempts.pop(oldest)['size']
            part = part[:max(0, REASONING_CHARS - used)]
            if not part:
                return
            entry['parts'].append(part)
            entry['size'] += len(part)
            shown, ident = entry['state'] == 'live', entry['id']
        if shown:
            self.emit('reasoning', {'chat_id': cid, 'run_id': rid, 'attempt_id': ident, 'delta': part})
            if first:
                self.publish(cid, rid)

    def settle(self, rid, step, attempt, state):
        """An attempt ended: 'committed' keeps its summary, 'discarded' drops it (a retry thinks again)."""
        with self._lock:
            entry = self._reasoning.get(rid, {}).get('attempts', {}).get((step, attempt))
            if entry is None:
                return
            if state == 'discarded':
                entry.update(state='discarded', parts=[], size=0)
            elif state == 'committed':
                entry['committed'] = True

    def release(self, cid, rid, step):
        """The step's text is being shown: publish the summaries its committed attempts held."""
        released = []
        with self._lock:
            for (number, _), entry in self._reasoning.get(rid, {}).get('attempts', {}).items():
                if number == step and entry['state'] == 'held' and entry['committed']:
                    entry.update(state='released', shown=True)
                    released.append((entry['id'], ''.join(entry['parts'])))
        for ident, text in released:
            for offset in range(0, len(text), REASONING_EVENT_CHARS):
                self.emit('reasoning', {'chat_id': cid, 'run_id': rid, 'attempt_id': ident,
                                        'delta': text[offset:offset + REASONING_EVENT_CHARS]})
        if released:
            self.publish(cid, rid)

    def _entries(self, rid):
        """{(step, attempt): whether its summary was ever published} for the summaries this process holds for a run."""
        with self._lock:
            return {key: entry['shown'] for key, entry in self._reasoning.get(rid, {}).get('attempts', {}).items()}

    def text(self, rid):
        with self._lock:
            attempts = self._reasoning.get(rid, {}).get('attempts', {})
            text = '\n\n'.join(''.join(e['parts']) for e in attempts.values() if e['state'] in SHOWN and e['size'])
        return text[:REASONING_CHARS]

    def forget_chat(self, cid):
        with self._lock:
            for rid in [r for r, v in self._reasoning.items() if v['chat_id'] == cid]:
                self._reasoning.pop(rid, None)
            for rid in [r for r, (owner, _) in self._sent.items() if owner == cid]:
                self._sent.pop(rid, None)
            self._views.forget_chat(cid)

    # -- the timeline, from host records --------------------------------------------------------------------------

    def _agent_nodes(self, rid):
        if self._nodes is None:
            self._nodes = bool(self.store.query("SELECT 1 FROM sqlite_master WHERE name='agent_nodes'"))
        if not self._nodes:
            return []
        return self.store.query('SELECT id,name,status,elapsed,created,updated,parent_id,depth,charged_tokens '
                                'FROM agent_nodes WHERE run_id=? ORDER BY created,id', (rid,))

    def _records(self, rid):
        if not isinstance(rid, str):
            raise ValueError('Run not found in this conversation.')
        rows = self.store.query('SELECT id,chat_id,state,elapsed,created,updated,config FROM runs WHERE id=?', (rid,))
        if not rows:
            raise ValueError('Run not found in this conversation.')  # the same refusal as run_get
        run = rows[0]
        run['config'] = json.loads(run['config']) if run['config'] else {}
        events = self.store.query('SELECT sequence,kind,payload,created FROM run_events WHERE run_id=? AND kind IN (%s) '
                                  'ORDER BY sequence' % ','.join('?' * len(EVENT_KINDS)), (rid, *EVENT_KINDS))
        for event in events:
            try:
                event['payload'] = json.loads(event['payload'])
            except ValueError:
                event['payload'] = {}
            if not isinstance(event['payload'], dict):
                event['payload'] = {}
        tools = {t['call_id']: t for t in self.store.query(TOOLS_SQL, (rid,))}
        actions = {a['id']: a for a in self.store.query('SELECT id,kind,status FROM actions WHERE chat_id=?',
                                                        (run['chat_id'],))}
        return run, events, tools, actions, self._agent_nodes(rid)

    def items(self, rid, views=False):
        """(run, items, usage). Phase 46 (1.9.29): `views` gives each item its work view (work_view.py), built once
        per item and record change; the run_activity read and live publishing ask for them, the receipt counts don't."""
        run, events, tools, actions, nodes = self._records(rid)
        start, live = run['created'], run['state'] in LIVE
        who = provider_name(run['config'])
        entries = self._entries(rid)
        found = {}

        def add(seq, created, kind, label, detail=None, duration=None, status='done'):
            found[seq] = {'seq': seq, 'at': round(max(0.0, created - start), 3), 'kind': kind,
                          'label': plain(label, LABEL_CHARS) or kind, 'detail': plain(detail, DETAIL_CHARS),
                          'duration': _seconds(duration), 'status': status,
                          **({'headers_ms': None, 'first_event_ms': None} if kind == 'model' else {})}
        add.found = found

        attempts, anchors, finished, refused, bound, decided = OrderedDict(), {}, {}, set(), {}, {}
        judged = {}  # Phase 45 (D141): action id -> the person's words Pebble's own check quoted
        held = {}  # Phase 46 (1.9.28, D185): action id -> why Pebble's check held it and went on without it
        stuck = {}  # action id -> why the card that asks the person after repeated holds was held
        last_end, usage, gate = None, None, None
        sources = {}  # Phase 46 (1.9.29): seq -> what its work view is built from (work_view.Views.attach)
        ledger, unmet, earlier_unmet = [], 0, 0  # AgentGuard's requirements, and the issues its checks found unmet
        for event in events:
            kind, data, seq, created = event['kind'], event['payload'], event['sequence'] * SLOTS, event['created']
            if kind == 'context_ready':
                count = data.get('memories') if type(data.get('memories')) is int else 0
                seconds = data.get('seconds')
                label = f'Recalled {count} memor{"y" if count == 1 else "ies"}' if count else 'Context ready'
                add(seq, created, 'context', label, f'Context ready in {round(seconds * 1000)} ms'
                    if type(seconds) in (int, float) else None, seconds)
            elif kind == 'model_attempt_started':
                attempts[(data.get('step'), data.get('attempt'))] = {'seq': seq, 'created': created, 'end': None,
                                                                     'connecting': False}
            elif kind == 'model_connecting':
                entry = attempts.get((data.get('step'), data.get('attempt')))
                if entry is not None:
                    entry['connecting'] = True
            elif kind == 'model_attempt':
                entry = attempts.get((data.get('step'), data.get('attempt')))
                if entry is not None:
                    entry['end'] = data
                last_end = data
                if isinstance(data.get('usage'), dict):
                    usage = add_report(usage, data['usage'])
            elif kind == 'model_retry':
                number = data.get('attempt') if type(data.get('attempt')) is int else 2
                # Phase 24 (t24-auth, V1-066): a retry while no network answered says so (the runner marks it).
                label = (f'Waiting for the network (attempt {number})' if data.get('network') is True
                         else f'Reconnecting to {who} (attempt {number})')
                add(seq, created, 'retry', label, self._retry_reason(who, last_end, data.get('status')),
                    data.get('delay'))
            elif kind in ('tool_authorized', 'tool_refused', 'tool_started'):
                call = data.get('call_id')
                if isinstance(call, str):
                    anchors.setdefault(call, (seq, created))
                    if kind == 'tool_refused':
                        refused.add(call)
            elif kind == 'tool_finished':
                if isinstance(data.get('call_id'), str):
                    finished.setdefault(data['call_id'], (created, bool(data.get('uncertain'))))
            elif kind == 'action_bound':
                if isinstance(data.get('action_id'), str):
                    bound.setdefault(data['action_id'], (seq, created, data.get('call_id')))
            elif kind == 'approval_started':
                if isinstance(data.get('action_id'), str):
                    decided.setdefault(data['action_id'], created)
            elif kind == 'step_judged':
                if isinstance(data.get('action_id'), str) and isinstance(data.get('quote'), str):
                    judged.setdefault(data['action_id'], data['quote'])
            elif kind == 'step_held' and data.get('held') in STEP_HELD:
                if data.get('returned') is True:  # Phase 46 (1.9.28, D185): no card; the same step again is not shown
                    if isinstance(data.get('action_id'), str):
                        held.setdefault(data['action_id'], data['held'])
                    if data.get('repeat') is not True:
                        add(seq, created, 'note', HELD_BACK + STEP_HELD[data['held']],
                            HELD_FROM_DATA if data.get('from_data') is True else None)
                elif data.get('asked_person') in HELD_STUCK:
                    # follow-up 1 (finding 2): what Pebble held before the card, and why it held this one
                    earlier = [self.label(tools[bound[a][2]]['name'], _label_args(tools[bound[a][2]]))
                               for a in held if a in bound and bound[a][2] in tools][-3:]
                    if isinstance(data.get('action_id'), str):
                        stuck.setdefault(data['action_id'], data['held'])
                    add(seq, created, 'note', HELD_STUCK[data['asked_person']],
                        ('Held before it: ' + '; '.join(earlier) + '. ' if earlier else '')
                        + 'Why this one was held: ' + STEP_HELD[data['held']] + '.')
                else:
                    add(seq, created, 'note', "Pebble's check kept the card: " + STEP_HELD[data['held']],
                        'The check could not run: ' + data['error'] if isinstance(data.get('error'), str) else None)
            elif kind == 'confined_command' and (data.get('restored') or data.get('failed')):
                # Phase 46 (1.9.28, D184): a command Auto ran without a card changed files the request keeps; Pebble put
                # them back from the copy it made just before (or says it could not)
                for key, label in (('restored', 'A command changed what your request keeps; Pebble put it back'),
                                   ('failed', "A command changed what your request keeps; Pebble couldn't put it back")):
                    if isinstance(data.get(key), list) and data[key]:
                        add(seq + (key == 'failed'), created, 'note', label, ', '.join(str(path) for path in data[key]))
            elif kind == 'completion_gate':
                gate = data  # the next completion check's AgentGuard summary (counts only)
            elif kind == 'requirements':  # Phase 46 (1.9.29): the ledger the next checks hold the run to
                ledger = data['requirements'] if isinstance(data.get('requirements'), list) else []
            elif kind == 'requirements_unmet':
                unmet += data['count'] if type(data.get('count')) is int and data['count'] > 0 else 0
            elif kind == 'held_decision':
                add(seq, created, 'note', HELD.get(data.get('decision'), 'You decided on the held task'))
            elif kind == 'completion_checked':
                issues = data.get('issues') if type(data.get('issues')) is int else 0
                if not issues:
                    label, detail = 'Checked the reply', None
                elif data.get('repairable'):
                    label, detail = 'A check asked for more evidence', f'{issues} item{"s" if issues != 1 else ""} to resolve'
                else:
                    label, detail = 'Verification remains incomplete', f'{issues} item{"s" if issues != 1 else ""} unresolved'
                if views:  # Phase 46 (1.9.29): a check's record never changes, so its view's key is constant
                    sources[seq] = ('check', 0, lambda d=data, g=gate, n=ledger, u=unmet, e=earlier_unmet, w=label:
                                    work_view.checks_view(w, d, g, n, u, e))
                    earlier_unmet, unmet = earlier_unmet + unmet, 0
                label = verdict(gate) or label
                gate = None
                add(seq, created, 'check', label, detail, data.get('seconds'))
            elif kind == 'reply_withheld':
                add(seq, created, 'note', WITHHELD.get(data.get('reason'), 'The reply follows the host checks'))
            elif kind == 'route_history_withheld':  # Phase 24 (V1-002): content-free; the window offers Review
                add(seq, created, 'note', 'Earlier on-device messages were not shared', data.get('label'))
            elif kind == 'state' and data.get('to') in STATE_NOTES:
                add(seq, created, 'note', STATE_NOTES[data['to']],
                    data.get('error') if data['to'] == 'failed' else None)

        for (step, number), entry in attempts.items():
            # Phase 45 (F247, audit round R11): a failed request that Pebble tried again reads as that, not as a red
            # failure with internal timings
            retried = type(number) is int and (step, number + 1) in attempts
            self._attempt(add, who, live, step, number, entry, entries, retried=retried)
        owner = {call: action for action, (_, _, call) in bound.items()}
        for call, (seq, created) in anchors.items():
            self._tool(add, tools.get(call), seq, created, finished.get(call), call in refused, live)
            if views and call in tools:  # rebuilt when the item, its tool record or its action changes
                staged = actions.get(owner.get(call)) or {}
                sources[seq] = ('tool', (found[seq]['status'], tools[call]['state'], staged.get('status')), call,
                                decided.get(owner.get(call), created), (finished.get(call) or (None,))[0])
        for action, (seq, created, call) in bound.items():
            self._approval(add, tools.get(call), actions.get(action), seq, created, decided.get(action),
                           judged.get(action), held=held.get(action), stuck=stuck.get(action))
            if views and action in actions:
                record, quote = actions[action], judged.get(action)
                sources[seq] = ('approval', (record['status'], decided.get(action) is not None, quote),
                                lambda a=record, d=decided.get(action), q=quote: work_view.review_view(a, a['kind'], d, q))
        spawned = {t['agent_id']: anchors[c][0] for c, t in tools.items()
                   if t['name'] in SPAWN_TOOLS and isinstance(t['agent_id'], str) and c in anchors}
        used = {}  # anchor seq -> child slots taken, in creation order (nodes are only ever appended)
        for node in nodes:
            if node['id'] in spawned:
                anchor = spawned[node['id']]
            else:
                before = [e['sequence'] for e in events if e['created'] <= node['created']]
                anchor = (before[-1] if before else 0) * SLOTS
            slot = CHILD_SLOT + used.get(anchor, 0)
            if slot >= SLOTS:
                continue  # beyond any agent tree's node limit; never reuses another item's seq
            used[anchor] = used.get(anchor, 0) + 1
            status = 'running' if node['status'] in CHILD_ACTIVE else 'failed' if node['status'] in CHILD_FAILED else 'done'
            duration = node['elapsed'] if node['elapsed'] else (node['updated'] - node['created']) if status != 'running' else None
            add(anchor + slot, node['created'], 'child', 'Agent ' + str(node['name']), None, duration, status)
            if views:
                sources[anchor + slot] = ('child', (node['status'], node['elapsed'], node.get('charged_tokens')),
                                          lambda n=node, s=status: work_view.agent_view(n, s))
        if views and work_view.enabled():
            self._views.attach(self.store, run['chat_id'], rid, found, sources, tools)
        report = None if usage is None else {k: usage[k] for k in ('input', 'cached', 'output', 'reasoning', 'tokens_per_second')}
        return run, [found[seq] for seq in sorted(found)], report

    @staticmethod
    def _retry_reason(who, end, status):
        end, name = end or {}, who[0].upper() + who[1:]
        unsent = ' Nothing was sent, so it used no tokens.' if end.get('sent') is False else ''
        if end.get('stall') == 'first_event':
            return f'No response from {who} within the first-event budget, so Pebble asked again; nothing had run.{unsent}'
        if end.get('stall') == 'idle':
            return f'{name} stopped sending before the reply finished, so Pebble asked again; nothing had run.'
        if end.get('stall') == 'sleep':  # Phase 24 (t24-auth, V1-068): the stream did not survive the Mac's sleep
            return f'The Mac slept while waiting for {who}, so Pebble asked again; nothing had run.'
        if type(status) is int:
            return f'{name} returned HTTP {status}.'
        if unsent:
            return f'Could not connect to {who}.{unsent}'
        return 'The connection dropped before the reply finished.'

    def _attempt(self, add, who, live, step, number, entry, entries, retried=False):
        end, suffix = entry['end'], f' (attempt {number})' if type(number) is int and number > 1 else ''
        name = who[0].upper() + who[1:]
        if end is None:
            status = 'running' if live else 'failed'
            label = f'Waiting for {who}' if live else f'Interrupted while waiting for {who}'
            detail = duration = None
        else:
            outcome, stall = end.get('outcome'), end.get('stall')
            status = 'done' if outcome == 'completed' else 'failed'
            label = (f'{name} responded' if outcome == 'completed' else f'Stopped waiting for {who}' if outcome == 'cancelled'
                     else f'No response from {who}' if stall == 'first_event'
                     else f'{name} stopped sending' if stall in ('idle', 'sleep')  # 'sleep': V1-068
                     else f'{name} request failed')
            parts = []
            if type(end.get('projection')) in (int, float):
                parts.append(f'context projected in {round(end["projection"] * 1000)} ms')
            for field, words in (('headers', 'headers'), ('first_event', 'first event'), ('first_token', 'first text')):
                if type(end.get(field)) in (int, float):
                    parts.append(f'{words} after {end[field]:.1f} s')
            if end.get('sent') is False:
                parts.append('the request was never sent, so it used no tokens')
            detail = '; '.join(parts) if parts else None
            detail = detail and detail[0].upper() + detail[1:] + '.'
            duration = end.get('seconds')
            if retried and outcome not in ('completed', 'cancelled'):
                detail = ('The request was never sent, so it used no tokens; ' if end.get('sent') is False else '') \
                    + 'Pebble tried again, and the task went on.'
                detail = detail[0].upper() + detail[1:]
        add(entry['seq'], entry['created'], 'model', label + suffix, detail, duration, status)
        timings = {field + '_ms': round(value * 1000) for field in ('headers', 'first_event')
                   for value in [(end or {}).get(field)] if type(value) in (int, float) and value >= 0}
        add.found[entry['seq']].update(timings)
        # The connect phase gets its own item when it took a second or more: live from the provider's notice, after
        # the attempt from its time to headers ("Connecting to …", then connected or not).
        headers = (end or {}).get('headers')
        slow = type(headers) in (int, float) and headers >= CONNECT_SHOWN
        connect = entry['seq'] + CONNECT_SLOT
        if entry['connecting'] or slow:
            if end is None:
                add(connect, entry['created'], 'model', f'Connecting to {who}…', None, None, 'running' if live else 'failed')
            elif type(headers) in (int, float):
                add(connect, entry['created'], 'model', f'Connected to {who} in {headers:.1f} s', None, headers, 'done')
            else:
                add(connect, entry['created'], 'model', f'Could not connect to {who}',
                    'Nothing was sent.' if end.get('sent') is False else None, end.get('seconds'), 'failed')
            add.found[connect].update({k: v for k, v in timings.items() if k == 'headers_ms'})
        # The reasoning item exists once its summary was shown: live, or released with its step's text (a discarded
        # attempt's item then settles as failed). After a restart (no summary in memory) the attempt's own record
        # says whether it streamed live; a held summary stays unlisted.
        shown = entries.get((step, number))
        reasoned = (end or {}).get('reasoning_first') is not None
        if shown or shown is None and reasoned and (end or {}).get('live'):
            if end is None:
                add(entry['seq'] + REASONING_SLOT, entry['created'], 'reasoning', 'Thinking', None, None,
                    'running' if live else 'failed')
            else:
                thought = next((v for v in (end.get('first_token'), end.get('reasoning_last'), end.get('seconds'))
                                if type(v) in (int, float)), None)
                add(entry['seq'] + REASONING_SLOT, entry['created'], 'reasoning',
                    f'Thought for {_whole(thought)} s' if thought is not None else 'Thought', None, thought,
                    'done' if end.get('outcome') == 'completed' else 'failed')

    def _tool(self, add, tool, seq, created, finished, refused, live):
        name = tool['name'] if tool else 'Tool'
        label = self.label(name, _label_args(tool)) if tool else 'Host step'
        detail, duration = None, (finished[0] - created) if finished else None
        if refused:
            status, detail = 'failed', "Refused by the host's checks; nothing ran."
        elif finished and finished[1]:
            status, detail = 'failed', 'Its outcome is uncertain; review it before resuming.'
        elif finished and tool and tool.get('status') == 'held':
            status, detail = 'done', HELD_TOOL  # Phase 46 (1.9.28, D185): its error is the model's note, not the person's
        elif finished:
            error = tool.get('error') if tool else None
            status = 'failed' if isinstance(error, str) and error else 'done'
            detail = (SCREEN_REFUSED.get(tool.get('refused_code'), SCREEN_REFUSED_OTHER) if tool.get('refused_code')
                      else script_failure(error)) if status == 'failed' else (
                'Staged for your approval.' if tool and tool['status'] == 'awaiting_user_approval' else None)
            if status == 'done' and name == 'ScreenObserve' and detail is None:  # Phase 45 (F182)
                detail = ('With a screenshot of the window.' if tool.get('screenshot') == 'object'
                          else SCREENSHOT_MISSING.get(tool.get('no_screenshot')))
            if status == 'done' and name == 'ScreenAct' and detail is None:  # Phase 45 (F204, F207)
                title = act_title(tool)
                if title:  # the row says the press itself ("Pressed “e3”"); its detail, what changed
                    label, detail = title, act_change(tool)
        elif tool and tool['state'] == 'completed':
            status = 'done'
        elif live:
            status = 'running'
        else:
            status, detail = 'failed', 'Interrupted before its outcome was recorded.'
        add(seq, created, 'tool', label, detail, duration, status)

    def _approval(self, add, tool, action, seq, created, decided, quote=None, held=None, stuck=None):
        label = self.label(tool['name'], _label_args(tool)) if tool else 'a proposed change'
        state, detail = (action or {}).get('status'), None
        if state == 'held':  # Phase 46 (1.9.28, D185): Pebble's check held it, and the task went on without it
            add(seq, created, 'approval', 'Held by Pebble, not run: ' + label,
                ('Why: ' + STEP_HELD[held] + '.') if held in STEP_HELD else None, None, 'done')
            return
        if state in (None, 'pending'):
            status, text = 'running', 'Waiting for your approval: '
            if stuck in STEP_HELD:  # follow-up 1 (finding 2): the card after repeated holds says why it was held
                detail = "Pebble's check held it: " + STEP_HELD[stuck] + '.'
        elif state == 'running':
            status, text = 'running', 'Applying the approved step: '
        elif state in ('applied', 'undone'):
            # Phase 45 (F231, audit round R10): every approval row says who approved it: the person (their decision was
            # journaled), Pebble's own check (a quote), or the permission mode (it ran without asking)
            who = 'Approved by you' if decided is not None else 'Allowed by your permission mode'
            status, text = 'done', who + (': ' if state == 'applied' else ', then undone: ')
            if isinstance(quote, str):  # Phase 45 (D141): Pebble's own check approved it
                # F230, F257 (audit rounds R10, R11): the row's label is cut at a word (plain); when it is cut, the
                # whole quote and the step are in the row's details
                text = f'Approved by Pebble (you asked: “{quote}”)' + (': ' if state == 'applied' else ', then undone: ')
                if len(text + label) > LABEL_CHARS:
                    detail = f'You asked: “{quote}”. The step: {label}.'
        elif state == 'rejected':
            # Phase 45 (F246, audit round R11): who declined it, as for approvals (F231)
            status, text = 'done', 'Declined by you: ' if decided is not None else 'Declined: '
        else:
            status, text = 'failed', 'Approval did not complete: '
        add(seq, created, 'approval', text + label, detail, (decided - created) if decided else None, status)

    # -- reads and live events ------------------------------------------------------------------------------------

    def read(self, run_id, after=0):
        """The run_activity route: the timeline after a cursor, the summary text so far and the reported usage."""
        if type(after) is not int or after < 0:
            raise ValueError('Use a nonnegative whole activity cursor.')
        run, items, usage = self.items(run_id, views=True)
        page = [item for item in items if item['seq'] > after or item['status'] == 'running'][:PAGE_ITEMS]
        text = self.text(run_id)
        elapsed = self._elapsed(run)
        # Phase 46 (1.9.29): the work view's head (plan progress, state, time) and, once the run settled, its receipt
        return {'run_id': run_id, 'state': run['state'], 'started': run['created'],
                'elapsed': round(elapsed, 3), 'items': page,
                'reasoning': {'available': bool(text), 'text': text}, 'usage': usage,
                'next': max([after] + [item['seq'] for item in page]),
                'header': work_view.header(self._plan(run_id), run['state'], elapsed),
                'summary': None if run['state'] in LIVE | WAITING else work_view.summary(items, usage, elapsed)}

    def _plan(self, rid):
        """The run's UpdatePlan steps (interaction.Interactions.plan's table), or None."""
        if self._plans is None:
            self._plans = bool(self.store.query("SELECT 1 FROM sqlite_master WHERE name='run_plans'"))
        rows = self.store.query('SELECT steps FROM run_plans WHERE run_id=?', (rid,)) if self._plans else []
        try:
            return {'steps': json.loads(rows[0]['steps'])} if rows else None
        except ValueError:
            return None

    @staticmethod
    def _elapsed(run):
        elapsed = run['elapsed'] or 0.0
        if run['state'] in LIVE:
            elapsed += max(0.0, time.time() - run['updated'])
        return elapsed

    def publish(self, cid, rid):
        """Emit each item that is new or changed since this run's last publish. Display only: never raises (the
        runner calls it at every step, unguarded, so nothing in a display path may end a run)."""
        try:
            self._publish(cid, rid)
        except Exception:  # the lead, 1.9.29: the whole publish is display only, not just building the items
            return

    def _publish(self, cid, rid):
        try:
            _, items, _ = self.items(rid, views=True)
        except Exception:
            return
        digests = self._views.digests(rid)  # Phase 46 (1.9.29): a changed view sends its item again
        changed = []
        with self._lock:
            if rid not in self._sent:
                self._sent[rid] = (cid, {})
                while len(self._sent) > RETAINED_RUNS:
                    self._sent.popitem(last=False)
            self._sent.move_to_end(rid)
            sent = self._sent[rid][1]
            for item in items:
                signature = (item['label'], item['detail'], item['status'], item['duration'], digests.get(item['seq']))
                if sent.get(item['seq']) != signature:
                    sent[item['seq']] = signature
                    changed.append(item)
        for item in changed:
            try:
                self.emit('activity', {'chat_id': cid, 'run_id': rid, 'item': item})
            except Exception:
                return
