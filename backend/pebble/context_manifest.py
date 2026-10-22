"""Deterministic context projection with an inspectable, content-free manifest.

API::

    assemble_context(messages, tools, context_limit, output_reserve=0,
                     counter=None, counter_method=None) -> ContextProjection

``ContextProjection.messages`` is a deep copy of the chat-format input with
whole units omitted. Nothing is rewritten, shortened or summarised, and host
``_pebble_*`` markers stay because the provider encoders own wire stripping.
``ContextProjection.manifest`` is JSON data for persistence: roles, marker
names, input indices, fingerprints and sizes, never message or schema text.
It describes structure and budget only; it does not establish source
freshness, truth or permission.

Units are classified only by role and host marker, never by message text:

    policy          system message
    goal            _pebble_current
    inbox           _pebble_inbox
    plan            _pebble_plan
    constraints     _pebble_constraints: exact earlier human turns built by
                    constraints.py; the marker is the SHA-256 of the content,
                    so a rewritten or silently cut unit fails integrity
    evidence        _pebble_checkpoint
    source, skill, memory
                    _pebble_source, _pebble_skill, _pebble_memory
    tool_exchange   assistant tool_calls plus exactly one directly following
                    result per call
    history         an unmarked user message with the assistant text after
                    it, or assistant text alone

Policy, goal, inbox, plan, constraints and evidence units, the latest tool
exchange and a final history unit (the turn being answered) are protected. When the window
exceeds ``context_limit - output_reserve``, other units retire whole: older
tool exchanges, then history, memory, skill and source, oldest first within
each. Opaque provider data (``_responses_output``, ``_anthropic_content``,
per-call ``extra_content``) belongs to its message, so it never separates
from its assistant turn. Malformed, incomplete or mismatched exchanges raise
ContextIntegrityError instead of being repaired.

``counter(messages, tools)`` returns a non-negative int for a complete window,
framing included. It runs once when everything fits; otherwise it measures
the protected floor and binary-searches the retirement order: at most
``2 + ceil(log2(retirable units))`` calls, plus one to diagnose an impossible
budget. Counter exceptions, such as cancellation, propagate unchanged.
Without a counter, a conservative local estimate is used and labelled
``local_estimate``. Per-unit sizes are always local estimates; they sum to the
window total only for that estimate, because real tokenizers and provider
framing do not compose additively.
"""

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field

__all__ = [
    'ContextBudgetExceeded', 'ContextCounterError', 'ContextError',
    'ContextIntegrityError', 'ContextProjection', 'assemble_context',
    'estimate_tokens',
]

MANIFEST_VERSION = 1
MAX_CONTEXT = 10_000_000  # Same ceiling as providers.MAX_CONTEXT.
MAX_MESSAGES = 8192  # A full manifest must also fit the 8 MB journal event bound.
MAX_TOOLS = 512
MAX_DEPTH = 100
MAX_NODES = 500_000
MAX_CHARS = 32_000_000  # Matches the durable checkpoint bound.
MAX_COUNT = 10 ** 12
DIAGNOSTIC_UNITS = 200

# Local estimate. Three UTF-8 bytes per token overstates English and code and
# gives CJK about a token per character. Scripts tokenized byte by byte can
# exceed it, so an injected tokenizer or provider count is preferred.
BYTES_PER_TOKEN = 3
IMAGE_TOKENS = 16000  # As token_budget.py: a bounded raster; base64 is not text.
MESSAGE_TOKENS = 12
TOOL_TOKENS = 8
BASE_TOKENS = 64
LOCAL_ESTIMATE = {'bytes_per_token': BYTES_PER_TOKEN, 'image_tokens': IMAGE_TOKENS,
                  'message_tokens': MESSAGE_TOKENS, 'tool_tokens': TOOL_TOKENS,
                  'base_tokens': BASE_TOKENS}

ROLES = ('system', 'user', 'assistant', 'tool')
# Trusted host markers, in precedence order when a message carries several.
MARKERS = {
    '_pebble_current': 'goal',
    '_pebble_inbox': 'inbox',
    '_pebble_plan': 'plan',
    '_pebble_goal': 'plan',
    '_pebble_constraints': 'constraints',
    '_pebble_required_skill': 'required_skill',
    '_pebble_required_plugin': 'required_plugin',
    '_pebble_checkpoint': 'evidence',
    '_pebble_source': 'source',
    '_pebble_skill': 'skill',
    '_pebble_memory': 'memory',
}
PROTECTED = ('policy', 'goal', 'inbox', 'plan', 'constraints', 'required_skill', 'required_plugin', 'evidence')
RETIREMENT = ('tool_exchange', 'history', 'memory', 'skill', 'source')
CATEGORIES = PROTECTED + RETIREMENT
# Opaque provider output replayed instead of tool_calls, and its call ID field.
OPAQUE = {'_responses_output': ('function_call', 'call_id'),
          '_anthropic_content': ('tool_use', 'id')}
NOT_VERIFIED = ('source_freshness', 'source_truth', 'permissions')
SUMMARY_FIELDS = ('units', 'messages', 'selected_units', 'omitted_units',
                  'selected_estimated_tokens', 'omitted_estimated_tokens')

PROBLEMS = {
    'not_list': 'Messages must be a list',
    'not_object': 'A message is not an object',
    'bad_role': 'A message has an unsupported role',
    'bad_tool': 'Tools must be a list of schema objects',
    'not_json': 'Context contains a value that is not strict JSON data',
    'too_deep': 'Context data is nested too deeply',
    'too_large': 'Context data exceeds the projection size limit',
    'too_many_messages': f'Context exceeds {MAX_MESSAGES} messages',
    'too_many_tools': f'Context exceeds {MAX_TOOLS} tool schemas',
    'unknown_marker': 'A message carries an unrecognized host marker',
    'marker_role': 'Host markers are valid only on system or user messages',
    'malformed_call': 'A tool call is malformed, lacks an ID or is not on an assistant message',
    'duplicate_call': 'An assistant message repeats a tool call ID',
    'opaque_shape': 'Provider output data is not a list on an assistant message',
    'opaque_mismatch': 'Provider output data names different tool calls than its message',
    'malformed_result': 'A tool result lacks a tool call ID',
    'orphan_result': 'A tool result does not directly follow its assistant tool call',
    'duplicate_result': 'A tool call has more than one result',
    'incomplete_exchange': 'An assistant tool call has no directly following result',
    'counter_mutation': 'The token counter changed the context it measured',
    'constraints_integrity': 'A human constraints unit is not an unmodified host user message',
}


class ContextError(ValueError):
    """The context was not projected. Messages never contain prompt text."""


class ContextIntegrityError(ContextError):
    """Messages or schemas are malformed, ambiguous or oversized."""

    def __init__(self, reason, index=None):
        self.reason, self.index = reason, index
        where = '' if index is None else f' (message {index})'
        super().__init__(f'{PROBLEMS[reason]}{where}. Context was not projected.')


class ContextCounterError(ContextError):
    """The injected token counter returned an unusable value."""


class ContextBudgetExceeded(ContextError):
    """Protected context or tool schemas exceed the input budget.

    ``manifest`` is a bounded, content-free diagnostic for the run journal."""

    def __init__(self, message, manifest):
        super().__init__(message)
        self.manifest = manifest


@dataclass(frozen=True)
class ContextProjection:
    messages: list
    manifest: dict


@dataclass
class _Unit:
    id: int
    category: str
    start: int
    end: int
    calls: int = 0
    markers: list = field(default_factory=list)
    opaque: set = field(default_factory=set)
    protected: bool = False
    reason: str = ''


def _utf8(text):
    return len(text) if text.isascii() else len(text.encode('utf-8', 'surrogatepass'))


def _size(value, allowance, index=None):
    """Validate strict JSON data; return (UTF-8 text bytes, image count).

    Iterative and bounded, so cyclic, deeply nested or shared-reference inputs
    fail clearly. Image data URLs count as images rather than base64 text."""
    size = images = 0
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        allowance[0] -= 1
        if allowance[0] < 0:
            raise ContextIntegrityError('too_large', index)
        if depth > MAX_DEPTH:
            raise ContextIntegrityError('too_deep', index)
        if isinstance(item, str):
            allowance[1] -= len(item)
            if allowance[1] < 0:
                raise ContextIntegrityError('too_large', index)
            if item.startswith('data:image/'):
                images += 1
            else:
                size += _utf8(item) + 2
        elif isinstance(item, (dict, list, tuple)):
            if len(item) > allowance[0]:
                raise ContextIntegrityError('too_large', index)
            if isinstance(item, dict):
                size += 2
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ContextIntegrityError('not_json', index)
                    allowance[1] -= len(key)
                    if allowance[1] < 0:
                        raise ContextIntegrityError('too_large', index)
                    size += _utf8(key) + 4
                    stack.append((child, depth + 1))
            else:
                size += 2 + len(item)
                stack.extend((child, depth + 1) for child in item)
        elif item is None or isinstance(item, bool):
            size += 5
        elif isinstance(item, int) and item.bit_length() <= 128:
            size += len(str(item))
        elif isinstance(item, float) and math.isfinite(item):
            size += len(repr(item))
        else:
            raise ContextIntegrityError('not_json', index)
    return size, images


def _tokens(size, images, overhead):
    return overhead + -(-size // BYTES_PER_TOKEN) + images * IMAGE_TOKENS


def estimate_tokens(messages, tools=()):
    """Conservative local estimate of a whole window, additive by construction.

    About three UTF-8 bytes per token plus per-message, per-tool and window
    framing. Each image data URL counts IMAGE_TOKENS instead of its base64."""
    allowance = [MAX_NODES, MAX_CHARS]
    return (BASE_TOKENS
            + sum(_tokens(*_size(m, allowance, i), MESSAGE_TOKENS) for i, m in enumerate(messages))
            + sum(_tokens(*_size(t, allowance), TOOL_TOKENS) for t in tools))


def _digest(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(text.encode('ascii')).hexdigest()


def _join(label, digests):
    return hashlib.sha256((label + ':' + ','.join(digests)).encode('ascii')).hexdigest()


def _label(digest):
    return 'sha256:' + digest[:32]


def _inspect(m, i):
    """Return a message's role, host markers, tool call IDs and opaque fields."""
    if not isinstance(m, dict):
        raise ContextIntegrityError('not_object', i)
    role = m.get('role')
    if role not in ROLES:
        raise ContextIntegrityError('bad_role', i)
    if any(k.startswith('_pebble_') and k not in MARKERS for k in m):
        raise ContextIntegrityError('unknown_marker', i)
    markers = [k for k in MARKERS if m.get(k)]
    if markers and role not in ('system', 'user'):
        raise ContextIntegrityError('marker_role', i)
    if '_pebble_constraints' in m:
        # Human turns are carried exactly or with explicit host markers; any
        # later rewrite, including a silent cut, changes this digest.
        content = m.get('content')
        if (role != 'user' or type(content) is not str or m['_pebble_constraints'] != 'sha256:' + hashlib.sha256(
                content.encode('utf-8', 'surrogatepass')).hexdigest()):
            raise ContextIntegrityError('constraints_integrity', i)
    calls = m.get('tool_calls')
    if calls is None or calls == []:
        calls = []
    elif not isinstance(calls, list) or role != 'assistant':
        raise ContextIntegrityError('malformed_call', i)
    ids = [c.get('id') if isinstance(c, dict) else None for c in calls]
    if not all(isinstance(x, str) and x for x in ids):
        raise ContextIntegrityError('malformed_call', i)
    if len(set(ids)) < len(ids):
        raise ContextIntegrityError('duplicate_call', i)
    opaque = []
    for key, (kind, link) in OPAQUE.items():
        value = m.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or role != 'assistant':
            raise ContextIntegrityError('opaque_shape', i)
        if value:
            # Encoders replay this instead of tool_calls; both must name the same calls.
            linked = [x.get(link) for x in value if isinstance(x, dict) and x.get('type') == kind]
            if not all(isinstance(x, str) for x in linked) or sorted(linked) != sorted(ids):
                raise ContextIntegrityError('opaque_mismatch', i)
            opaque.append(key)
    return role, markers, ids, opaque


def _segment(messages):
    """Split messages into contiguous retirement units, refusing unsafe structure."""
    units, pending, answered = [], None, set()
    for i, m in enumerate(messages):
        role, markers, ids, opaque = _inspect(m, i)
        if role == 'tool':
            ident = m.get('tool_call_id')
            if not isinstance(ident, str) or not ident:
                raise ContextIntegrityError('malformed_result', i)
            if pending is not None and ident in answered:
                raise ContextIntegrityError('duplicate_result', i)
            if pending is None or ident not in pending:
                raise ContextIntegrityError('orphan_result', i)
            answered.add(ident)
            units[-1].end = i + 1
            continue
        if pending is not None and answered != pending:
            raise ContextIntegrityError('incomplete_exchange', units[-1].start)
        pending = None
        if ids:
            pending, answered = set(ids), set()
            units.append(_Unit(len(units), 'tool_exchange', i, i + 1, calls=len(ids), opaque=set(opaque)))
        elif role == 'assistant' and units and units[-1].category == 'history':
            units[-1].end = i + 1  # Assistant text stays with the turn it answers.
            units[-1].opaque.update(opaque)
        else:
            category = 'policy' if role == 'system' else MARKERS[markers[0]] if markers else 'history'
            units.append(_Unit(len(units), category, i, i + 1, markers=markers, opaque=set(opaque)))
    if pending is not None and answered != pending:
        raise ContextIntegrityError('incomplete_exchange', units[-1].start)
    return units


def assemble_context(messages, tools, context_limit, output_reserve=0, counter=None, counter_method=None):
    """Project ``messages`` into ``context_limit - output_reserve`` input tokens.

    Raises ValueError or TypeError for invalid limits or counter,
    ContextIntegrityError for unsafe structure, ContextCounterError for an
    invalid count, and ContextBudgetExceeded when protected context or tool
    schemas cannot fit."""
    if type(context_limit) is not int or not 1 <= context_limit <= MAX_CONTEXT:
        raise ValueError(f'Context limit must be an integer from 1 to {MAX_CONTEXT} tokens.')
    if type(output_reserve) is not int or not 0 <= output_reserve < context_limit:
        raise ValueError('Output reserve must be a non-negative integer below the context limit.')
    if counter is not None and not callable(counter):
        raise TypeError('The token counter must be callable.')
    if counter_method is not None and (counter is None or not isinstance(counter_method, str)
                                       or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', counter_method)):
        raise ValueError('Counter method must be a short label for an injected counter.')
    tools = () if tools is None else tools
    if not isinstance(messages, (list, tuple)):
        raise ContextIntegrityError('not_list')
    if len(messages) > MAX_MESSAGES:
        raise ContextIntegrityError('too_many_messages')
    if not isinstance(tools, (list, tuple)):
        raise ContextIntegrityError('bad_tool')
    if len(tools) > MAX_TOOLS:
        raise ContextIntegrityError('too_many_tools')
    if not all(isinstance(t, dict) for t in tools):
        raise ContextIntegrityError('bad_tool')

    # Validate and size the caller's data before copying anything.
    allowance = [MAX_NODES, MAX_CHARS]
    measured = [_size(m, allowance, i) for i, m in enumerate(messages)]
    tool_sizes = [_tokens(*_size(t, allowance), TOOL_TOKENS) for t in tools]
    units = _segment(messages)
    work = [copy.deepcopy(m) for m in messages]
    tool_work = [copy.deepcopy(t) for t in tools]
    sizes = [_tokens(*pair, MESSAGE_TOKENS) for pair in measured]
    images = [n for _, n in measured]
    digests = [_digest(m) for m in work]
    tool_digests = [_digest(t) for t in tool_work]
    unit_digests = [_join('unit', digests[u.start:u.end]) for u in units]
    tools_digest = _join('tools', tool_digests)

    latest = next((u for u in reversed(units) if u.category == 'tool_exchange'), None)
    for u in units:
        if u.category in PROTECTED:
            u.reason = 'protected_' + u.category
        elif u is latest:
            u.reason = 'latest_tool_exchange'
        elif u is units[-1] and u.category == 'history':
            u.reason = 'latest_turn'
        u.protected = bool(u.reason)
    retirable = sorted((u for u in units if not u.protected),
                       key=lambda u: (RETIREMENT.index(u.category), u.start))

    limit = context_limit - output_reserve
    if counter is None:
        method = 'local_estimate'
        by_id = {id(m): s for m, s in zip(work, sizes)}
        schemas = BASE_TOKENS + sum(tool_sizes)

        def measure(window, _tools):
            return schemas + sum(by_id[id(m)] for m in window)
    else:
        method, measure = counter_method or 'injected_counter', counter
    calls, counts = 0, {}

    def count(window):
        nonlocal calls
        calls += 1
        value = measure(window, list(tool_work))
        if type(value) is not int or not 0 <= value <= MAX_COUNT:
            raise ContextCounterError('The token counter returned an invalid count. Context was not projected.')
        return value

    def fits(n):
        # n is a prefix of the retirement order; counts are cached per prefix.
        if n not in counts:
            retired = {u.id for u in retirable[:n]}
            counts[n] = count([m for u in units if u.id not in retired for m in work[u.start:u.end]])
        return counts[n] <= limit

    def verify_counter():
        if counter is not None and (any(_digest(m) != digest for m, digest in zip(work, digests))
                                    or [_digest(t) for t in tool_work] != tool_digests):
            raise ContextIntegrityError('counter_mutation')

    def manifest(state, chosen, tokens, unit_limit=None):
        records = []
        for u in units:
            span = range(u.start, u.end)
            records.append({
                'unit': u.id,
                'category': u.category,
                'first_index': u.start,
                'last_index': u.end - 1,
                'roles': [work[i]['role'] for i in span],
                'markers': list(u.markers),
                'opaque_fields': sorted(u.opaque),
                'tool_calls': u.calls,
                'images': sum(images[i] for i in span),
                'fingerprint': _label(unit_digests[u.id]),
                'estimated_tokens': sum(sizes[i] for i in span),
                'protected': u.protected,
                'selected': u.id in chosen,
                'reason': u.reason,
            })
        summary = {c: dict.fromkeys(SUMMARY_FIELDS, 0) for c in CATEGORIES}
        for r in records:
            row, kept = summary[r['category']], r['selected']
            row['units'] += 1
            row['messages'] += len(r['roles'])
            row['selected_units' if kept else 'omitted_units'] += 1
            row['selected_estimated_tokens' if kept else 'omitted_estimated_tokens'] += r['estimated_tokens']
        omitted = [r for r in records if not r['selected']]
        omitted_messages = sum(len(r['roles']) for r in omitted)
        listed = records if unit_limit is None else [r for r in records if r['protected']][:unit_limit]
        return {
            'version': MANIFEST_VERSION,
            'status': state,
            'dispatchable': state != 'exceeded',
            'limits': {'context': context_limit, 'output_reserve': output_reserve, 'input': limit},
            'input_tokens': tokens,
            'estimation': {
                'window_method': method,
                'unit_method': 'local_estimate',
                'units_additive': counter is None,
                'counter_calls': calls,
                'local_estimate': dict(LOCAL_ESTIMATE),
            },
            'tools': {
                'count': len(tool_work),
                'fingerprint': _label(tools_digest),
                'fingerprints': [_label(d) for d in tool_digests],
                'estimated_tokens': sum(tool_sizes),
            },
            'messages': {'input': len(work), 'selected': len(work) - omitted_messages},
            'omitted': {'units': len(omitted), 'messages': omitted_messages,
                        'estimated_tokens': sum(r['estimated_tokens'] for r in omitted)},
            'summary': summary,
            'units': listed,
            'units_total': len(records),
            'units_listed': len(listed),
            'window_fingerprint': None if state == 'exceeded' else _label(
                _join('window', [tools_digest] + [unit_digests[u.id] for u in units if u.id in chosen])),
            'not_verified': list(NOT_VERIFIED),
        }

    if fits(0):
        retire = 0
    elif fits(len(retirable)):
        # Invariant: prefix ``low`` does not fit and prefix ``retire`` does.
        low, retire = 0, len(retirable)
        while retire - low > 1:
            mid = (low + retire) // 2
            if fits(mid):
                retire = mid
            else:
                low = mid
    else:
        tools_only = count([])
        verify_counter()
        for u in retirable:
            u.reason = 'retired_for_budget'
        tokens = {'before': counts[0], 'after': None,
                  'protected_floor': counts[len(retirable)], 'tools_only': tools_only}
        raise ContextBudgetExceeded(
            'The enabled tool schemas alone exceed the model input budget. Disable tools or select a larger model.'
            if tools_only > limit else
            'The current request and required context exceed the model context. Shorten the request or select a larger model.',
            manifest('exceeded', {u.id for u in units if u.protected}, tokens, DIAGNOSTIC_UNITS))

    retired = {u.id for u in retirable[:retire]}
    for u in retirable:
        u.reason = 'retired_for_budget' if u.id in retired else 'within_budget'
    chosen = {u.id for u in units} - retired
    indices = [i for u in units if u.id in chosen for i in range(u.start, u.end)]
    verify_counter()
    tokens = {'before': counts[0], 'after': counts[retire],
              'protected_floor': counts.get(len(retirable)), 'tools_only': None}
    return ContextProjection([work[i] for i in indices],
                             manifest('retired' if retired else 'complete', chosen, tokens))
