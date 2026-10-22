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
"""

import json
import re
import threading
import time
import uuid
from collections import OrderedDict

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
EVENT_KINDS = ('state', 'context_ready', 'model_attempt_started', 'model_connecting', 'model_attempt', 'model_retry',
               'tool_authorized', 'tool_refused', 'tool_started', 'tool_finished', 'action_bound', 'approval_started',
               'completion_checked', 'reply_withheld', 'completion_gate', 'held_decision',
               'route_history_withheld')  # Phase 24 (V1-002, t24-data): read for its note below
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
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.status') END AS status,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.error') END AS error,"
             "CASE WHEN json_valid(result) THEN json_extract(result,'$.agent_id') END AS agent_id "
             "FROM run_tools WHERE run_id=?")


def plain(text, limit):
    """Display text: control characters become spaces, at most `limit` characters (an ellipsis marks a cut)."""
    if not isinstance(text, str):
        return None
    text = ' '.join(_CONTROL.sub(' ', text).split()) if limit <= LABEL_CHARS else _CONTROL.sub(' ', text).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


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

    # -- the timeline, from host records --------------------------------------------------------------------------

    def _agent_nodes(self, rid):
        if self._nodes is None:
            self._nodes = bool(self.store.query("SELECT 1 FROM sqlite_master WHERE name='agent_nodes'"))
        if not self._nodes:
            return []
        return self.store.query('SELECT id,name,status,elapsed,created,updated FROM agent_nodes WHERE run_id=? '
                                'ORDER BY created,id', (rid,))

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
        actions = {a['id']: a for a in self.store.query('SELECT id,status FROM actions WHERE chat_id=?', (run['chat_id'],))}
        return run, events, tools, actions, self._agent_nodes(rid)

    def items(self, rid):
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
        last_end, usage, gate = None, None, None
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
            elif kind == 'completion_gate':
                gate = data  # the next completion check's AgentGuard summary (counts only)
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
            self._attempt(add, who, live, step, number, entry, entries)
        for call, (seq, created) in anchors.items():
            self._tool(add, tools.get(call), seq, created, finished.get(call), call in refused, live)
        for action, (seq, created, call) in bound.items():
            self._approval(add, tools.get(call), actions.get(action), seq, created, decided.get(action))
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

    def _attempt(self, add, who, live, step, number, entry, entries):
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
        label = self.label(name, {'file_path': str(tool['file_path'] or '') if tool else '',
                                  'name': str(tool['arg_name'] or '') if tool else ''}) if tool else 'Host step'
        detail, duration = None, (finished[0] - created) if finished else None
        if refused:
            status, detail = 'failed', "Refused by the host's checks; nothing ran."
        elif finished and finished[1]:
            status, detail = 'failed', 'Its outcome is uncertain; review it before resuming.'
        elif finished:
            error = tool.get('error') if tool else None
            status = 'failed' if isinstance(error, str) and error else 'done'
            detail = error if status == 'failed' else ('Staged for your approval.' if tool and tool['status'] == 'awaiting_user_approval'
                                                       else None)
        elif tool and tool['state'] == 'completed':
            status = 'done'
        elif live:
            status = 'running'
        else:
            status, detail = 'failed', 'Interrupted before its outcome was recorded.'
        add(seq, created, 'tool', label, detail, duration, status)

    def _approval(self, add, tool, action, seq, created, decided):
        label = self.label(tool['name'], {'file_path': str(tool['file_path'] or ''), 'name': str(tool['arg_name'] or '')}) \
            if tool else 'a proposed change'
        state = (action or {}).get('status')
        if state in (None, 'pending'):
            status, text = 'running', 'Waiting for your approval: '
        elif state == 'running':
            status, text = 'running', 'Applying the approved step: '
        elif state in ('applied', 'undone'):
            status, text = 'done', 'Approved: ' if state == 'applied' else 'Approved, then undone: '
        elif state == 'rejected':
            status, text = 'done', 'Declined: '
        else:
            status, text = 'failed', 'Approval did not complete: '
        add(seq, created, 'approval', text + label, None, (decided - created) if decided else None, status)

    # -- reads and live events ------------------------------------------------------------------------------------

    def read(self, run_id, after=0):
        """The run_activity route: the timeline after a cursor, the summary text so far and the reported usage."""
        if type(after) is not int or after < 0:
            raise ValueError('Use a nonnegative whole activity cursor.')
        run, items, usage = self.items(run_id)
        page = [item for item in items if item['seq'] > after or item['status'] == 'running'][:PAGE_ITEMS]
        text = self.text(run_id)
        return {'run_id': run_id, 'state': run['state'], 'started': run['created'],
                'elapsed': round(self._elapsed(run), 3), 'items': page,
                'reasoning': {'available': bool(text), 'text': text}, 'usage': usage,
                'next': max([after] + [item['seq'] for item in page])}

    @staticmethod
    def _elapsed(run):
        elapsed = run['elapsed'] or 0.0
        if run['state'] in LIVE:
            elapsed += max(0.0, time.time() - run['updated'])
        return elapsed

    def publish(self, cid, rid):
        """Emit each item that is new or changed since this run's last publish. Display only: never raises."""
        try:
            _, items, _ = self.items(rid)
        except Exception:
            return
        changed = []
        with self._lock:
            if rid not in self._sent:
                self._sent[rid] = (cid, {})
                while len(self._sent) > RETAINED_RUNS:
                    self._sent.popitem(last=False)
            self._sent.move_to_end(rid)
            sent = self._sent[rid][1]
            for item in items:
                signature = (item['label'], item['detail'], item['status'], item['duration'])
                if sent.get(item['seq']) != signature:
                    sent[item['seq']] = signature
                    changed.append(item)
        for item in changed:
            try:
                self.emit('activity', {'chat_id': cid, 'run_id': rid, 'item': item})
            except Exception:
                return
