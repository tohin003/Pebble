"""The person's context inspector and between-turns compaction (Phase 20 C20-context, frontier gap C09).

Two main-window routes, modelled on the public behaviour of Claude Code's /context and /compact (a view of what
fills the model's context, and a command to compact it) and built only on Pebble's own host records.

`context_inspect(chat_id, run_id=None)` is a read. It rebuilds a run's current or last model request from the
run's checkpoint (the exact messages and tool offer the runner persisted before the request) and the run's last
content-free projection manifest (`context_projected`: which units were sent, and a fingerprint of every tool
schema sent). It then counts that request with the runner's own estimator: `TokenBudget.count` over the request
in its route's form (chat wire messages, or Responses input items and tool shapes), times the route's calibration
multiplier, as the provider adapters count a request against the window. The categories are system, tool schemas
(per tool), memory recall, project and host messages, conversation, tool results, and the estimator's own margin
and framing. They sum to that total by construction, and free space is the projection's input window minus it.
Messages are classified by role, host marker and position only. The reply holds counts, host part names and
catalogue tool names: never message text, tool descriptions, arguments or results.

When the manifest no longer describes the checkpoint (a compaction since then, or no projection recorded), the
stored window that the next model boundary starts from is counted instead, labelled `checkpoint`. A recorded tool
schema that the checkpoint no longer holds is counted as unidentified, never guessed.

`context_compact(chat_id)` compacts the conversation's latest task window between turns, through the existing
path: App.run_compact (compaction.compact_window, then RunJournal.compact_checkpoint and its archive). The task's
next model boundary then starts from the compacted window. It is refused while any task runs. It aims to retire
about half of the stored messages' tokens: older tool exchanges and turns go first, and protected units (the
person's exact instructions, plan, evidence and latest step) never retire. If that target is below the protected
floor, it falls back to run_compact's own target, the route budget. A finished task's window is never reused (a
new message starts a new window), so nothing is compacted for it. Every accepted request writes a content-free
receipt to the run journal (`context_compact_receipt`): what was retired by category, tokens before and after, and
the archive generation. No model is called, no tool or effect is replayed, and no memory layer is touched.

Neither route is a model tool, and neither adds anything to model context. Both are main-window only: the shell
writes `surface` from the calling window's label (main.rs), and anything but 'main' is refused here. Both names
are in tool_authz.NATIVE_ONLY.
"""

import hashlib
import json
import math
import re
import time
import uuid

from .compaction import fingerprint
from .context_manifest import MAX_CONTEXT, ContextBudgetExceeded, assemble_context
from .policy import PolicyError
from .providers import Providers
from .responses import input_items
from .runtime import LIVE, RESUMABLE
from .token_budget import TokenBudget

ROUTES = ('context_inspect', 'context_compact')
RECEIPT_EVENT = 'context_compact_receipt'
CATEGORIES = ('system', 'tool_schemas', 'memory', 'project_host', 'conversation', 'tool_results', 'framing')
PARTS = {'project_host': ('evidence', 'plan_goal', 'inbox', 'skills_plugins', 'sources', 'host_notes'),
         'conversation': ('request', 'constraints', 'history', 'model_turns')}
# Host markers in context_manifest's precedence order (a message with several takes the first), as (category, part).
MARKERS = (
    ('_pebble_current', 'conversation', 'request'),
    ('_pebble_inbox', 'project_host', 'inbox'),
    ('_pebble_plan', 'project_host', 'plan_goal'),
    ('_pebble_goal', 'project_host', 'plan_goal'),
    ('_pebble_constraints', 'conversation', 'constraints'),
    ('_pebble_required_skill', 'project_host', 'skills_plugins'),
    ('_pebble_required_plugin', 'project_host', 'skills_plugins'),
    ('_pebble_checkpoint', 'project_host', 'evidence'),
    ('_pebble_source', 'project_host', 'sources'),
    ('_pebble_skill', 'project_host', 'skills_plugins'),
    ('_pebble_memory', 'memory', 'memory'),
)
MESSAGE_TOKENS = 12     # TokenBudget.count's framing per message
IMAGE_TOKENS = 16000    # TokenBudget.count's cost of one attached raster image (its base64 is not counted)
IMAGE_TEXT = '[attached raster image]'
TARGET_SHARE = 0.5      # context_compact keeps at most this share of the stored messages' tokens
MIN_TARGET = 512        # App.run_compact's smallest input target
STATUSES = ('complete', 'retired', 'exceeded')
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}')
METHOD = re.compile(r'[A-Za-z0-9_.:-]{1,64}')
REFUSED_SURFACE = 'Inspect and compact context from the main window.'
REFUSED_LIVE = 'A task is running. Compact between turns: wait for it to finish, or pause it first.'


def wire(message):
    """A message as the provider adapters send it: host markers stripped (Providers.wire_context)."""
    return {k: v for k, v in message.items() if not k.startswith('_pebble_')}


def request_form(message, protocol='chat'):
    """One message as its route's counter sees it: the chat wire form, or Responses input items (several for an
    assistant turn with calls, none for an empty one)."""
    return input_items([message]) if protocol == 'responses' else [wire(message)]


def tool_form(tool, protocol='chat'):
    """One tool schema as its route's counter sees it (complete_responses reshapes it for Responses)."""
    function = tool.get('function') if isinstance(tool, dict) else None
    return {'type': 'function', **function, 'strict': False} if protocol == 'responses' and isinstance(function, dict) else tool


def route_protocol(run):
    """'responses' when the run's pinned route speaks the Responses protocol (Providers.protocol), else 'chat'."""
    route = (run.get('config') or {}).get('route') or {}
    if route.get('kind') in (None, 'gguf') or not isinstance(route.get('url'), str):
        return 'chat'
    try:
        return 'responses' if Providers.protocol(route) == 'responses' else 'chat'
    except (KeyError, TypeError, ValueError):
        return 'chat'


def classify(message, before_request):
    """(category, part) of one message, from its role, host markers and position only; never its text.

    ``before_request``: the message precedes the person's current request (``_pebble_current``)."""
    for marker, category, part in MARKERS:
        if message.get(marker):
            return category, part
    role = message.get('role')
    if role == 'system':
        return 'system', 'system'
    if role == 'tool':
        return 'tool_results', 'tool_results'
    if before_request:
        return 'conversation', 'history'  # earlier turns, replayed from the conversation
    # After the request, unmarked user messages are host notes (resume, completion and workflow checks); the
    # person's follow-ups arrive through the inbox.
    return ('conversation', 'model_turns') if role == 'assistant' else ('project_host', 'host_notes')


def _normalized(value):
    """The value as TokenBudget.count encodes it, and its number of image data URLs."""
    images = 0

    def walk(item):
        nonlocal images
        if isinstance(item, str) and item.startswith('data:image/'):
            images += 1
            return IMAGE_TEXT
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        return item
    return walk(value), images


def _cost(encoding, value, framing=0):
    data, images = _normalized(value)
    return len(encoding.encode(json.dumps(data, ensure_ascii=False), disallowed_special=())) + framing + IMAGE_TOKENS * images


def _tool_name(tool):
    name = (tool.get('function') or {}).get('name') if isinstance(tool, dict) else None
    return name if isinstance(name, str) and NAME.fullmatch(name) else 'unnamed'


def _tool_label(tool):
    """The schema fingerprint a projection manifest records for a tool (context_manifest's sha256 label)."""
    return 'sha256:' + fingerprint(tool)[:32]


def message_cost(encoding, message, protocol='chat'):
    """Estimator tokens of one message in its route's form: encoded JSON, per-item framing and image cost."""
    return sum(_cost(encoding, item, MESSAGE_TOKENS) for item in request_form(message, protocol))


def compose(messages, tools, window, estimator, multiplier=1.0, protocol='chat'):
    """Counts and names for one request, never content.

    ``total`` is ``estimator.count`` of the request in its route's form (chat wire messages, or Responses input
    items and tool shapes), as the runner's projection counts it. The categories sum to it exactly: each message's
    encoded JSON plus its per-item and image cost, each tool's encoded JSON, and ``framing`` for the rest (the
    estimator's margin, base and envelope). ``used`` applies the route's calibration multiplier as TokenBudget.count
    does; ``free`` and ``over`` compare it with ``window`` (None when unknown)."""
    if type(multiplier) not in (int, float) or not 1.0 <= multiplier <= 2.0:
        raise ValueError('A calibration multiplier must be from 1.0 to 2.0.')
    encoding = estimator.encoding()
    tools = list(tools)
    sent_tools = [tool_form(t, protocol) for t in tools]
    total = estimator.count([item for m in messages for item in request_form(m, protocol)], sent_tools)
    used = total if multiplier == 1.0 else math.ceil(total * multiplier)
    rows = {c: {'id': c, 'tokens': 0, 'messages': 0,
                'parts': {p: {'id': p, 'tokens': 0, 'messages': 0} for p in PARTS.get(c, ())}} for c in CATEGORIES}
    current = next((i for i, m in enumerate(messages) if m.get('_pebble_current')), len(messages))
    for index, message in enumerate(messages):
        category, part = classify(message, index < current)
        cost = message_cost(encoding, message, protocol)
        for row in (rows[category], rows[category]['parts'].get(part)):
            if row is not None:
                row['tokens'] += cost
                row['messages'] += 1
    schemas = sorted(({'name': _tool_name(t), 'tokens': _cost(encoding, sent)} for t, sent in zip(tools, sent_tools)),
                     key=lambda item: (-item['tokens'], item['name']))
    rows['tool_schemas']['tokens'] = sum(item['tokens'] for item in schemas)
    rows['framing']['tokens'] = total - sum(row['tokens'] for c, row in rows.items() if c != 'framing')
    return {
        'counter': getattr(encoding, 'name', 'o200k_base'), 'protocol': protocol,
        'total': total, 'used': used,
        'calibration': {'multiplier': multiplier, 'tokens': used - total},
        'free': None if window is None else max(0, window - used),
        'over': None if window is None else max(0, used - window),
        'categories': [{**row, 'parts': list(row['parts'].values())} for row in rows.values()],
        'tools': schemas,
    }


def _segments(messages):
    """The projection's content-free units for these messages, or None when they cannot be projected."""
    try:
        return assemble_context(messages, [], MAX_CONTEXT).manifest['units']
    except ValueError:  # ContextError and malformed structure: never matched, never repaired
        return None


def match_request(messages, manifest):
    """(the messages the recorded projection sent, how many checkpoint messages it covered), or None.

    The manifest describes a prefix of the checkpoint when its unit fingerprints equal those of the prefix: either
    every input unit (the projection's selection is then applied) or only the selected ones (a compaction stored
    its selection as the checkpoint). Anything else, including a refused projection, is not matched."""
    if not isinstance(manifest, dict) or manifest.get('status') not in ('complete', 'retired'):
        return None
    units, counts = manifest.get('units'), manifest.get('messages')
    if not isinstance(units, list) or not isinstance(counts, dict) or manifest.get('units_listed') != len(units) \
            or manifest.get('units_total') != len(units) or not all(isinstance(u, dict) for u in units):
        return None
    recorded = [u.get('fingerprint') for u in units]
    chosen = [u.get('selected') is True for u in units]
    for size, expected, whole in ((counts.get('input'), recorded, True),
                                  (counts.get('selected'), [f for f, c in zip(recorded, chosen) if c], False)):
        if type(size) is not int or not 0 < size <= len(messages):
            continue
        mine = _segments(messages[:size])
        if mine is None or [u['fingerprint'] for u in mine] != expected:
            continue
        if not whole:
            return messages[:size], size
        keep = [i for u, c in zip(mine, chosen) if c for i in range(u['first_index'], u['last_index'] + 1)]
        return [messages[i] for i in keep], size
    return None


def at_boundary(snapshot):
    """Whether a checkpoint is at a committed model boundary, as App.run_compact requires (it checks again)."""
    messages = snapshot.get('messages')
    if snapshot.get('phase') == 'model':
        return True
    return (snapshot.get('phase') == 'tools' and bool(messages) and isinstance(messages[-1], dict)
            and messages[-1].get('role') == 'assistant' and not messages[-1].get('tool_calls'))


def route_budget(run):
    """The run's pinned route budget, as the runner reads it (4096 for a local model or no route)."""
    route = (run.get('config') or {}).get('route') or {}
    if not route or route.get('kind') == 'gguf':
        return 4096
    value = route.get('budget') if route.get('budget') is not None else route.get('context')
    return value if type(value) is int and value > 0 else None


def _main(surface):
    if surface != 'main':
        raise PolicyError(REFUSED_SURFACE)


def _identity(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise PolicyError('Use the scoped conversation and task identities.')


def _count(value):
    return value if type(value) is int and value >= 0 else None


class ContextInspector:
    """The two main-window routes. Construction creates nothing: no table, no row."""

    def __init__(self, app):
        self.app = app

    def methods(self):
        return {'context_inspect': self.inspect, 'context_compact': self.compact}

    def _estimator(self):
        budget = getattr(self.app.model, 'token_budget', None)
        return budget if isinstance(budget, TokenBudget) else TokenBudget(self.app.resources)

    def _latest(self, chat_id):
        runs = self.app.runs.list(chat_id)
        if not runs and not self.app.store.query('SELECT 1 FROM chats WHERE id=?', (chat_id,)):
            raise ValueError('Conversation not found.')
        return runs[0] if runs else None

    def _multiplier(self, run, method):
        """The route's calibration multiplier when the run's pinned route is the current one, else 1.0."""
        model = self.app.model
        if method not in (None, 'o200k_local_estimate') or not hasattr(model, 'calibration_factor'):
            return 1.0, None
        profile = model.current() if hasattr(model, 'current') else None
        identity = ((run.get('config') or {}).get('route') or {}).get('identity')
        if profile is None and identity is None:
            return 1.0, None  # no cloud route then or now: nothing is calibrated
        if not profile or identity != hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(',', ':'))
                                                    .encode()).hexdigest():
            return 1.0, 'calibration.route_not_current'
        try:
            factor = model.calibration_factor(profile)
        except Exception:
            return 1.0, 'calibration.unavailable'
        return (factor, None) if type(factor) in (int, float) and 1.0 <= factor <= 2.0 else (1.0, 'calibration.unavailable')

    def _tools(self, rid, available, fingerprints):
        """The recorded request's tool schemas, matched by fingerprint against the checkpoint's offer and its
        per-step SpawnAgent budget note (Agents.annotate_tools reads only); unmatched ones are only counted."""
        pool = list(available)
        try:
            pool += list(self.app.agents.annotate_tools(rid, available))
        except Exception:
            pass
        candidates = {}
        for tool in pool:
            try:
                candidates.setdefault(_tool_label(tool), tool)
            except (TypeError, ValueError):
                continue
        found = [candidates.get(f) for f in fingerprints]
        return [t for t in found if t is not None], sum(t is None for t in found)

    def _compact_reason(self, run, latest_id):
        """Why context_compact would not compact this run's window now (None: it would try)."""
        if self.app.active or run['state'] in LIVE:
            return 'live'
        if run['id'] != latest_id:
            return 'not_latest'
        if run['state'] == 'completed':
            return 'finished'
        if run['state'] not in RESUMABLE:
            return 'not_resumable'
        snapshot = run['checkpoint'] or {}
        if not snapshot.get('messages'):
            return 'no_window'
        return None if at_boundary(snapshot) else 'between_tools'

    # Routes

    def inspect(self, chat_id, run_id=None, surface=None):
        _main(surface)
        _identity(chat_id)
        if run_id is not None:
            _identity(run_id)
        app = self.app
        with app.store.lock:  # one consistent read of the run, its checkpoint and its last projection
            latest = self._latest(chat_id)
            rid = run_id or (latest['id'] if latest else None)
            if rid is None:
                return {'chat_id': chat_id, 'run_id': None, 'latest_run_id': None, 'basis': 'none',
                        'compact': {'available': False, 'reason': 'no_window'}, 'authorization': 'none',
                        'inference': 'none'}
            run = app.runs.get(rid, chat_id)
            record = app.runs.context_manifest(rid)
            archives = len(app.runs.compactions(rid))
        snapshot = run['checkpoint'] if isinstance(run['checkpoint'], dict) else {}
        messages = snapshot.get('messages') if isinstance(snapshot.get('messages'), list) else []
        available = snapshot.get('available') if isinstance(snapshot.get('available'), list) else []
        record = record if isinstance(record, dict) else {}
        manifest = record.get('manifest') if isinstance(record.get('manifest'), dict) else None
        omissions = []
        matched = match_request(messages, manifest) if manifest and messages else None
        if matched:
            basis, request, covered = 'last_request', matched[0], matched[1]
        elif messages:
            basis, request, covered = 'checkpoint', messages, None
        else:
            basis, request, covered = 'none', [], None
        recorded_tools = (manifest or {}).get('tools') if isinstance((manifest or {}).get('tools'), dict) else {}
        fingerprints = recorded_tools.get('fingerprints')
        if basis == 'none':
            tools, unidentified, tools_basis = [], 0, 'none'
        elif isinstance(fingerprints, list) and len(fingerprints) == recorded_tools.get('count'):
            tools, unidentified = self._tools(rid, available, fingerprints)
            tools_basis = 'last_request'
            if unidentified:
                omissions.append('tools.unidentified')
        else:
            tools, unidentified, tools_basis = list(available), 0, 'eligible_offer'
        limits = (manifest or {}).get('limits')
        budget = route_budget(run)
        if isinstance(limits, dict) and _count(limits.get('input')):
            window = {'tokens': limits['input'], 'context': _count(limits.get('context')),
                      'output_reserve': _count(limits.get('output_reserve')), 'source': 'last_projection'}
        else:
            window = {'tokens': budget, 'context': budget, 'output_reserve': None, 'source': 'route_budget'}
            if budget is None:
                omissions.append('window.unknown')
        window['route_budget'] = budget
        estimation = (manifest or {}).get('estimation') if isinstance((manifest or {}).get('estimation'), dict) else {}
        method = estimation.get('window_method') if isinstance(estimation.get('window_method'), str) \
            and METHOD.fullmatch(estimation['window_method']) else None
        multiplier, gap = self._multiplier(run, method)
        if gap:
            omissions.append(gap)
        estimator, protocol = self._estimator(), route_protocol(run)
        composition = compose(request, tools, window['tokens'], estimator, multiplier, protocol)
        since = None
        if covered is not None:
            encoding = estimator.encoding()
            later = messages[covered:]
            since = {'messages': len(later), 'tokens': sum(message_cost(encoding, m, protocol) for m in later)}
        recorded = None
        if manifest:
            tokens = manifest.get('input_tokens') if isinstance(manifest.get('input_tokens'), dict) else {}
            omitted = manifest.get('omitted') if isinstance(manifest.get('omitted'), dict) else {}
            recorded = {'status': manifest.get('status') if manifest.get('status') in STATUSES else None,
                        'input_tokens': _count(tokens.get('after')), 'before_retirement': _count(tokens.get('before')),
                        'method': method, 'omitted_messages': _count(omitted.get('messages')),
                        'compacted': isinstance(manifest.get('compaction'), dict), 'step': _count(record.get('step'))}
        reason = self._compact_reason(run, latest['id'] if latest else None)
        return {'chat_id': chat_id, 'run_id': rid, 'latest_run_id': latest['id'] if latest else None,
                'run_state': run['state'], 'steps': _count(run['steps']), 'basis': basis,
                'live': bool(app.active == chat_id and app.active_run == rid) or run['state'] in LIVE,
                **composition, 'window': window, 'tools_basis': tools_basis, 'tools_unidentified': unidentified,
                'recorded': recorded, 'since_last_request': since, 'compactions': archives,
                'compact': {'available': reason is None, 'reason': reason},
                'omissions': sorted(set(omissions)), 'authorization': 'none', 'inference': 'none'}

    def _target(self, snapshot, budget, estimator):
        """Half of the stored messages' tokens on top of the tool schemas, as run_compact's counter measures them;
        None when that is not below the route budget (run_compact's own target is then used)."""
        messages = snapshot.get('messages') or []
        tools = snapshot.get('available') or []
        schemas = estimator.count([], tools)
        stored = estimator.count([wire(m) for m in messages], tools)
        target = max(MIN_TARGET, schemas + math.ceil((stored - schemas) * TARGET_SHARE))
        return target if type(budget) is int and target < budget else None

    def _archived(self, run, generation, before_index, estimator):
        """Retired messages by inspector category: counts and estimator tokens only."""
        archive = self.app.runs.context_archive(run['id'], generation)['archive']
        encoding, protocol, rows = estimator.encoding(), route_protocol(run), {}
        for row in archive:
            message, index = (row.get('message'), row.get('index')) if isinstance(row, dict) else (None, None)
            if not isinstance(message, dict) or type(index) is not int:
                continue
            category, _part = classify(message, before_index is None or index < before_index)
            entry = rows.setdefault(category, {'messages': 0, 'tokens': 0})
            entry['messages'] += 1
            entry['tokens'] += message_cost(encoding, message, protocol)
        return {c: rows[c] for c in CATEGORIES if c in rows}

    def compact(self, chat_id, surface=None):
        _main(surface)
        _identity(chat_id)
        app = self.app
        if app.active:
            raise PolicyError(REFUSED_LIVE)
        latest = self._latest(chat_id)
        if latest is None:
            raise PolicyError('This conversation has no task window to compact yet.')
        run = app.runs.get(latest['id'], chat_id)
        if run['state'] in LIVE:
            raise PolicyError(REFUSED_LIVE)
        if run['state'] not in RESUMABLE and run['state'] != 'completed':
            raise PolicyError('Reconcile this task\'s uncertain step before compacting its window.')
        receipt = {'receipt_id': uuid.uuid4().hex, 'kind': 'context_compact', 'origin': 'human', 'surface': 'main',
                   'chat_id': chat_id, 'run_id': run['id'], 'run_state': run['state'], 'path': 'run_compact',
                   'generation': None, 'target_tokens': None, 'target_reached': None, 'route_budget': None,
                   'archived': {'messages': 0, 'tool_results': 0, 'by_category': {}},
                   'tokens': None, 'kept': None, 'fingerprints': None,
                   'replayed': 0, 'inference': 'none', 'memory': 'unchanged', 'authorization': 'none'}
        if run['state'] == 'completed':
            # Its window is never read again: a new message starts a new window from the conversation.
            receipt['status'] = 'fresh_window'
            return self._record(receipt)
        snapshot = run['checkpoint'] if isinstance(run['checkpoint'], dict) else {}
        messages = snapshot.get('messages') if isinstance(snapshot.get('messages'), list) else []
        if not messages:
            raise PolicyError('This task has no stored window yet.')
        estimator = self._estimator()
        profile = app.model.current() if hasattr(app.model, 'current') else None
        budget = profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 2800  # as run_compact
        target = self._target(snapshot, budget, estimator)
        receipt.update(route_budget=budget, target_tokens=target or budget)
        reached = True
        try:
            result = app.run_compact(chat_id, run['id'], target) if target else app.run_compact(chat_id, run['id'])
        except ContextBudgetExceeded:
            # The protected floor (exact instructions, plan, evidence, latest step) is above the target, or
            # compaction could not progress there; nothing was written. run_compact's own target is the budget.
            if not target:
                raise
            reached = False
            result = app.run_compact(chat_id, run['id'])
        receipt['target_reached'] = reached and result['status'] == 'compacted'
        if result['status'] != 'compacted':
            receipt['status'] = 'already_fits'
            return self._record(receipt)
        manifest = result['manifest']
        progress = manifest.get('progress') or {}
        before_index = next((i for i, m in enumerate(messages) if isinstance(m, dict) and m.get('_pebble_current')), None)
        same = fingerprint(messages) == manifest.get('before_fingerprint')
        receipt.update(
            status='compacted', generation=result['generation'],
            archived={'messages': manifest.get('archived_messages'), 'tool_results': manifest.get('archived_results'),
                      'by_category': self._archived(run, result['generation'], before_index, estimator)
                      if same else None},
            tokens={'before': progress.get('before'), 'after': progress.get('after'), 'freed': progress.get('freed')},
            kept={'exact_human_instructions': manifest.get('exact_human_instructions'),
                  'human_constraints': manifest.get('human_constraints'),
                  'spine_detail': (manifest.get('spine') or {}).get('detail')},
            fingerprints={'before': manifest.get('before_fingerprint'), 'after': manifest.get('after_fingerprint')})
        return self._record(receipt)

    def _record(self, receipt):
        receipt['created'] = time.time()
        try:
            self.app.runs.note(receipt['run_id'], RECEIPT_EVENT, receipt)
            journaled = True
        except Exception:  # a compaction already committed stays authoritative; never mask its outcome
            journaled = False
        try:
            context = self.inspect(receipt['chat_id'], receipt['run_id'], 'main')
        except Exception:
            context = None
        return {**receipt, 'journaled': journaled, 'context': context}
