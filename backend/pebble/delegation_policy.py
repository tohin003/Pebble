"""Host-owned delegation budget policy: root completion reserve, child fit, advice
and per-dispatch provider usage validation.

Pure functions over ledger numbers that Tree reads inside its own transaction.
Nothing here touches the database, a provider, task text, child results,
profiles or credentials. The policy never spawns, never forces delegation and
never judges task difficulty or success.

Root reserve: child admissions cannot consume the last
``min(max_steps, max(2, min(3, max_steps // 4)))`` logical steps of a shared
tree: at least review and a final answer when the cap allows, three (adding one
verification) from twelve steps up, and never more than the cap itself. A
one- or two-step tree is wholly reserved, so no child can ever be admitted
there. Each reserved step keeps the allowance the tree grants every step: three
dispatch admissions, three pinned windows of reservation tokens,
``max_seconds / max_steps`` elapsed seconds and eight tool receipts. The root
itself may spend the whole shared pool; the reserve never raises a configured
cap and nothing restores it after a retry or restart.

Admission gate (Phase 15, A08): a model-requested spawn is admitted only when
the shared pool still covers the measured delegated floor for its task kind,
plus the spawner's own integration steps, and the remaining seconds and tokens
cover a minimal child. Refusals are typed and name the numbers. History and
model estimates never enter this decision.
"""

import math
import re
from collections import Counter

ROOT_RESERVE_STEPS = 3      # review, one verification step, final response
MIN_RESERVE_STEPS = 2       # review and final response, whenever the cap allows
RESERVE_SHARE = 4           # above the minimum, at most a quarter of the shared steps
ATTEMPTS_PER_STEP = 3       # the tree's existing retry admissions per logical step
RECEIPTS_PER_STEP = 8       # the tree's existing tool receipts per logical step
DEFAULT_CHILD_STEPS = 8
COORDINATION_STEPS = 1      # the spawner's next WaitForAgents turn shares the same pool
DEFAULT_CHILD_SECONDS = 120.
MIN_CHILD_SECONDS = 1.
USEFUL_CHILD_STEPS = 2      # one tool step and a final report
ROOMY_CHILD_STEPS = 6
MAX_REPORTED_TOKENS = 1_000_000_000  # the SpawnAgent model_tokens schema ceiling
USAGE_FIELDS = ('input_tokens', 'output_tokens', 'cached_tokens')
# reported/partial/unreported/invalid are settled ledger rows. pending is an
# admitted dispatch still in flight; unrecorded is a dispatch that ended without
# settlement (interruption, restart, or admission before this ledger existed).
USAGE_STATES = ('reported', 'partial', 'unreported', 'invalid', 'pending', 'unrecorded')
CAPACITY = {'max_steps': 'steps', 'max_seconds': 'seconds', 'model_tokens': 'model_tokens'}
UNITS = {'max_steps': 'steps', 'max_seconds': 'seconds', 'model_tokens': 'model tokens'}
PRICE_NOTE = ('No per-request price is recorded: subscription routes have none and API prices are not estimated. '
              'Provider-reported token counts are the only measured usage.')
RESERVATION_NOTE = 'Admission charges are conservative reservations, never measured provider usage.'
NOTES = {
    'tree_limit': 'Budget advice only: this tree cannot add another child here (depth, node or capability limit). Continue solo.',
    'solo': 'Budget advice only: no useful child fits beside the root reserve now. Continue solo; unfit explicit limits are refused.',
    'small_child': 'Budget advice only: at most a small child fits (largest_child). Delegation is optional; solo work avoids coordination steps.',
    'delegation_fits': 'Budget advice only: a child fits within largest_child. Delegation is optional; small tasks need none.',
}


# Admission floors: the W6 scripted minimal paths (scripted_economics on 10a.2 and
# 15a.1). A delegated write passes at 8 shared steps and fails at 7; a read-only
# delegation passes at 6. The spawning turn has already been counted when the gate
# runs, so a root needs 7 (write) or 5 (read-only) more steps.
TASK_KINDS = ('read_only', 'write')
CHILD_FLOOR_STEPS = {'read_only': 2, 'write': 3}  # read, (propose), report
ROOT_INTEGRATION_STEPS = 3   # wait, review (with integration), final answer
CHILD_INTEGRATION_STEPS = 2  # a spawning child's own wait and report
# An approval-gated write costs one more root turn: the root answers before the
# native approval and again after its continuation (runner.py parks only on
# `required` results). Drop to 0 if the runner parks on pending approvals.
APPROVAL_STEPS = 1
PING_PONG_ROUNDS = 2         # review refusals plus root steers per child generation
DUPLICATE_SIMILARITY = 0.9   # word-set Jaccard above which two tasks ask the same question
DUPLICATE_MIN_WORDS = 4
WORDS = re.compile(r'\w+')
ADMISSION_UNITS = {'shared_steps': 'shared steps', 'spawner_steps': 'own steps', 'child_steps': 'child steps',
                   'shared_seconds': 'seconds', 'shared_tokens': 'model tokens'}


class UnfitLimits(ValueError):
    """A child budget cannot fit beside the root reserve. Nothing was spawned or changed."""

    def __init__(self, message, advice=None):
        super().__init__(message)
        self.advice = advice


class DelegationRefused(ValueError):
    """A typed host refusal (admission, duplicate work, review or ping-pong limit).

    ``refusal`` holds only host numbers, host IDs and fixed labels. Nothing was
    spawned, accepted or sent.
    """

    def __init__(self, message, refusal):
        super().__init__(message)
        self.refusal = refusal


def task_kind(write_paths, worktree=False):
    """'write' when the host leased project paths or assigned a worktree; else 'read_only'."""
    return 'write' if write_paths or worktree else 'read_only'


def floor_parts(ledger, kind, approvals=0, own=None):
    """The shared steps one delegation of ``kind`` still needs after its spawning turn."""
    if kind not in TASK_KINDS:
        raise ValueError('Unknown delegated task kind.')
    if type(approvals) is not int or approvals < 0:
        raise ValueError('Approval steps are a nonnegative host count.')
    parts = {'child': CHILD_FLOOR_STEPS[kind], 'integration': CHILD_INTEGRATION_STEPS if own else ROOT_INTEGRATION_STEPS,
             'approval': approvals}
    if own:
        # A grandchild's parent integrates it; the root still keeps its own reserve.
        parts['root_reserve'] = reserved_steps(ledger['max_steps'])
    return parts


def admission(ledger, kind, approvals=0, own=None):
    """None when one more child of ``kind`` fits the measured floor, else a typed refusal.

    ``ledger`` is Tree._ledger; ``approvals`` counts the host-known native
    approvals the integration needs (permission mode, never the model's word);
    ``own`` is a spawning child's view (Tree._own), whose integration is its own
    wait and report while the root reserve stays untouched. Checked in order:
    shared steps, the spawner's own steps, then child seconds and tokens beside
    the root reserve at the tree's per-step share.
    """
    parts = floor_parts(ledger, kind, approvals, own)  # validates kind and approvals first
    floor = CHILD_FLOOR_STEPS[kind]
    needed, available = sum(parts.values()), max(0, ledger['max_steps'] - ledger['used_steps'])

    def refused(limit, need, have):
        return {'kind': 'admission', 'limit': limit, 'needed': need, 'available': have,
                'unit': ADMISSION_UNITS[limit], 'task_kind': kind, 'floor': parts}

    if available < needed:
        return refused('shared_steps', needed, available)
    if own and own['steps_left'] < CHILD_INTEGRATION_STEPS:
        return refused('spawner_steps', CHILD_INTEGRATION_STEPS, own['steps_left'])
    cap = child_capacity(ledger)
    seconds = floor * ledger['max_seconds'] / max(1, ledger['max_steps'])
    if cap['seconds'] < seconds:
        return refused('shared_seconds', math.ceil(seconds), int(cap['seconds']))
    if cap['model_tokens'] < floor * ledger['window']:
        return refused('shared_tokens', floor * ledger['window'], cap['model_tokens'])
    return None


def child_floor_refusal(kind, steps):
    """A typed refusal when a resolved child step budget is below its kind's floor."""
    if steps >= CHILD_FLOOR_STEPS[kind]:
        return None
    return {'kind': 'admission', 'limit': 'child_steps', 'needed': CHILD_FLOOR_STEPS[kind], 'available': steps,
            'unit': ADMISSION_UNITS['child_steps'], 'task_kind': kind,
            'floor': {'child': CHILD_FLOOR_STEPS[kind]}}


def refusal_text(refusal):
    """Fixed host wording that names the limit and its numbers."""
    kind = refusal['kind']
    if kind == 'admission':
        limit, unit = refusal['limit'], refusal['unit']
        task = 'a ' + refusal['task_kind'].replace('_', '-') + ' delegation'
        parts = ', '.join(k.replace('_', ' ') + ' ' + str(v) for k, v in refusal['floor'].items() if v)
        if limit == 'child_steps':
            return ('Delegation refused (child_steps): ' + task + ' needs a child with at least '
                    + str(refusal['needed']) + ' steps; its budget allows ' + str(refusal['available'])
                    + '. Nothing was spawned; omit max_steps for a host-fitted budget or continue solo.')
        if limit == 'spawner_steps':
            return ('Delegation refused (spawner_steps): waiting and reporting need ' + str(refusal['needed'])
                    + ' of your own steps; ' + str(refusal['available']) + ' remain. Nothing was spawned; continue yourself.')
        return ('Delegation refused (' + limit + '): ' + task + ' needs ' + str(refusal['needed']) + ' ' + unit
                + ((' (' + parts + ')') if limit == 'shared_steps' else ' for a minimal child') + '; '
                + str(refusal['available']) + ' remain. Nothing was spawned; continue solo.')
    if kind == 'duplicate_work':
        what = ('its paths overlap ' + ', '.join(refusal['paths'])) if refusal['limit'] == 'file_set' else 'it asks the same question'
        status, review = refusal['status'], refusal['review']
        if status in ('queued', 'running', 'waiting_parent', 'waiting_input'):
            action = 'use WaitForAgents or SendAgentMessage on it'
        elif status == 'completed' and review == 'accepted':
            action = 'integrate its accepted patch (IntegrateAgentPatch) first'
        elif status == 'completed':
            action = "review it (ReviewAgentResult) or dismiss it (decision='dismiss') first"
        elif status == 'interrupted':
            action = 'retry it (RetryAgent) or stop it (StopAgent) first'
        else:
            action = "dismiss it (ReviewAgentResult decision='dismiss') or stop it (StopAgent) first"
        return ('Duplicate work refused (' + refusal['limit'] + '): child ' + refusal['agent_id'] + ' (' + status
                + ', review ' + review + ') already holds this work; ' + what + '. Nothing was spawned; ' + action + '.')
    if kind == 'ping_pong_limit':
        accepted = refusal['accepted_evidence_ids']
        if refusal['status'] in ('queued', 'running', 'waiting_parent', 'waiting_input'):
            choice = ('wait for its result (WaitForAgents), then accept or dismiss it, or take the work over '
                      '(StopAgent, then do it yourself).')
        else:
            choice = (('accept it (ReviewAgentResult; omit evidence_ids to cite its final evidence ' + json_ids(accepted) + '), '
                       if accepted else 'it has no acceptable final evidence, so ')
                      + "dismiss it (decision='dismiss'), or take the work over (StopAgent, then do it yourself).")
        return ('Ping-pong limit (' + refusal['limit'] + '): ' + str(refusal['rounds']) + ' review or steer rounds already ran on child '
                + refusal['agent_id'] + ' generation ' + str(refusal['generation']) + '. Now ' + choice)
    if kind == 'review':
        accepted = refusal['accepted_evidence_ids']
        if accepted:
            tail = '; omit evidence_ids to cite them.'
        elif refusal['status'] in ('queued', 'running', 'waiting_parent', 'waiting_input'):
            tail = '; none until it settles, so wait for it (WaitForAgents) or take the work over (StopAgent).'
        else:
            tail = "; none, so dismiss it (decision='dismiss') or take the work over."
        return (refusal['reason'] + ' Accepted evidence_ids for generation ' + str(refusal['generation']) + ': '
                + json_ids(accepted) + tail)
    raise ValueError('Unknown refusal kind.')


def json_ids(ids):
    return '[' + ', '.join('"' + str(i) + '"' for i in ids) + ']'


def refuse(refusal):
    return DelegationRefused(refusal_text(refusal), refusal)


def same_question(first, second):
    """True when two task texts ask the same question: equal words, or a word-set
    Jaccard of at least DUPLICATE_SIMILARITY on tasks of DUPLICATE_MIN_WORDS or more."""
    a, b = WORDS.findall(first.casefold()), WORDS.findall(second.casefold())
    if not a or not b:
        return False
    if a == b:
        return True
    left, right = set(a), set(b)
    return (min(len(left), len(right)) >= DUPLICATE_MIN_WORDS
            and len(left & right) / len(left | right) >= DUPLICATE_SIMILARITY)


def ping_pong_refusal(agent_id, generation, rounds, accepted, limit, status):
    """After PING_PONG_ROUNDS review or steer rounds on one child generation the
    root must accept, dismiss or take the work over; None before that."""
    if rounds < PING_PONG_ROUNDS:
        return None
    return {'kind': 'ping_pong_limit', 'limit': limit, 'agent_id': agent_id, 'generation': generation,
            'status': status, 'rounds': rounds, 'max_rounds': PING_PONG_ROUNDS,
            'accepted_evidence_ids': list(accepted), 'options': ['accept', 'dismiss', 'take_over']}


def window(max_tokens, max_steps):
    """The pinned per-attempt reservation the tree derived its token cap from."""
    return max_tokens // (max(1, max_steps) * ATTEMPTS_PER_STEP)


def reserved_steps(max_steps):
    """Root completion steps children can never take; bounded by the root cap."""
    max_steps = max(0, max_steps)
    return min(max_steps, max(MIN_RESERVE_STEPS, min(ROOT_RESERVE_STEPS, max_steps // RESERVE_SHARE)))


def root_reserve(ledger):
    steps = reserved_steps(ledger['max_steps'])
    return {'steps': steps, 'dispatches': steps * ATTEMPTS_PER_STEP,
            'model_tokens': steps * ATTEMPTS_PER_STEP * ledger['window'],
            'seconds': ledger['max_seconds'] * steps / ledger['max_steps'] if steps else 0.,
            'tool_receipts': steps * RECEIPTS_PER_STEP}


def child_capacity(ledger):
    """What all children together may still consume before reaching the root reserve.

    ``used_steps`` counts completed and in-flight dispatches, so concurrent
    admissions, grandchildren, retries and restarts all draw on one durable pool.
    """
    reserve = root_reserve(ledger)
    return {'steps': max(0, ledger['max_steps'] - reserve['steps'] - ledger['used_steps']),
            'dispatches': max(0, ledger['max_steps'] * ATTEMPTS_PER_STEP - reserve['dispatches'] - ledger['dispatches']),
            'model_tokens': max(0, ledger['max_tokens'] - reserve['model_tokens'] - ledger['charged_tokens']),
            'seconds': max(0., ledger['max_seconds'] - reserve['seconds'] - ledger['used_seconds']),
            'tool_receipts': max(0, ledger['max_steps'] * RECEIPTS_PER_STEP - reserve['tool_receipts'] - ledger['tool_receipts'])}


def child_denial(ledger, charged):
    """'root_reserve' when one more child dispatch of ``charged`` tokens would
    consume root completion capacity; None when it fits. Hard caps are checked
    by the caller first. A wholly reserved tree denies every child dispatch."""
    cap = child_capacity(ledger)
    if cap['steps'] < 1 or cap['dispatches'] < 1 or cap['model_tokens'] < charged or cap['seconds'] <= 0:
        return 'root_reserve'
    return None


def _unallocated(ledger):
    return {'max_steps': ledger['max_steps'] - ledger['allocated_steps'],
            'max_seconds': ledger['max_seconds'] - ledger['allocated_seconds'],
            'model_tokens': ledger['max_tokens'] - ledger['allocated_tokens']}


def _amount(key, value):
    if key == 'max_seconds':
        return str(int(value)) + ' seconds'
    return str(value) + ' ' + UNITS[key]


def resolve_child_limits(ledger, limits, parent=None):
    """Host defaults for omitted child limits; explicit values are returned unchanged.

    A default is the documented ceiling (8 steps, 120 seconds, three pinned
    windows per step) clamped to what children can still use beside the root
    reserve (less one step for the spawner's own next wait), to unallocated
    budget outside that reserve and, for a grandchild, to its parent's limits.
    Returns ``(budget, sources)``; each source is 'explicit', 'default' or
    'default_fitted'. A default that cannot fit raises UnfitLimits before
    anything is written.
    """
    for key, value in limits.items():
        if key == 'max_seconds':
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        else:
            valid = type(value) is int and value >= 1
        if not valid:
            raise ValueError('Child budgets require positive bounded numbers.')
    cap, reserve, free = child_capacity(ledger), root_reserve(ledger), _unallocated(ledger)
    parent = parent or {}
    budget, sources = {}, {}

    def resolve(key, base, floor):
        if key in limits:
            budget[key], sources[key] = limits[key], 'explicit'
            return
        shared = cap[CAPACITY[key]] - (COORDINATION_STEPS if key == 'max_steps' else 0)
        allocatable = free[key] - reserve[CAPACITY[key]]
        value = min([base, shared, allocatable] + ([parent[key]] if key in parent else []))
        if value < floor:
            if allocatable < floor and allocatable <= shared:
                message = ('Child allocations cannot multiply the root budget: existing child allocations already hold the '
                           + UNITS[key] + ' outside the root reserve. Nothing was spawned; wait for children to settle, '
                           'pass smaller explicit limits, or continue solo.')
            elif key in parent and parent[key] < floor:
                message = ('A grandchild cannot exceed its parent budget: the parent ' + key
                           + ' leaves no room for a host-fitted default. Nothing was spawned; continue in this agent.')
            else:
                message = ('No default child fits beside the root reserve: ' + _amount(key, max(0, shared))
                           + ' of the shared budget remain for children'
                           + (" after the spawner's next wait" if key == 'max_steps' else '')
                           + ', and the root keeps ' + _amount(key, reserve[CAPACITY[key]])
                           + ' for review and its final answer. Nothing was spawned; continue solo.')
            raise UnfitLimits(message, delegation_advice(ledger))
        budget[key], sources[key] = value, 'default' if value == base else 'default_fitted'

    resolve('max_steps', min(DEFAULT_CHILD_STEPS, ledger['max_steps']), 1)
    resolve('max_seconds', min(DEFAULT_CHILD_SECONDS, float(ledger['max_seconds'])), MIN_CHILD_SECONDS)
    tokens = min(ledger['max_tokens'], budget['max_steps'] * ATTEMPTS_PER_STEP * ledger['window'])
    resolve('model_tokens', tokens, min(ledger['window'], tokens))
    budget['max_seconds'] = float(budget['max_seconds'])
    return budget, sources


def check_child_fit(ledger, budget, sources):
    """Refuse an explicit limit no child could spend beside the root reserve.

    Tree checks hard allocation and parent ceilings first. An explicit request
    is admitted exactly as written or refused here; it is never reduced.
    """
    cap, reserve = child_capacity(ledger), root_reserve(ledger)
    for key in ('max_steps', 'max_seconds', 'model_tokens'):
        room = cap[CAPACITY[key]]
        if sources.get(key) != 'explicit' or budget[key] <= room:
            continue
        shown = int(room) if key == 'max_seconds' else room
        held = _amount(key, reserve[CAPACITY[key]])
        if shown < 1:
            tail = 'continue solo'
        else:
            tail = 'request at most ' + _amount(key, shown) + ', omit ' + key + ' for a host-fitted default, or continue solo'
        value = format(budget[key], 'g') if key == 'max_seconds' else str(budget[key])
        raise UnfitLimits('Explicit ' + key + ' ' + value + ' does not fit: children can use ' + _amount(key, shown)
                          + ' more of the shared budget while the root keeps ' + held
                          + ' for review and its final answer. Nothing was spawned; ' + tail + '.',
                          delegation_advice(ledger))


def delegation_advice(ledger, own=None):
    """Content-free advice from host budget numbers only.

    ``own`` describes a child considering a grandchild: its limits (which bound
    a grandchild), its remaining steps and whether it may spawn at all. The
    advice informs a choice between solo work and smaller children; it never
    requests a spawn and never predicts success.
    """
    cap, reserve, free = child_capacity(ledger), root_reserve(ledger), _unallocated(ledger)
    # largest_child leaves the spawner's next wait step; explicit limits up to
    # child_steps_left are still admitted.
    largest = {'max_steps': max(0, min(cap['steps'] - COORDINATION_STEPS, free['max_steps'])),
               'max_seconds': max(0, int(min(cap['seconds'], free['max_seconds']))),
               'model_tokens': max(0, min(cap['model_tokens'], free['model_tokens']))}
    room = ledger['nodes_left'] > 0
    if own is not None:
        room = room and own['can_spawn']
        largest = {key: min(value, int(own['limits'][key])) for key, value in largest.items()}
    if not room:
        recommendation, reason = 'solo', 'tree_limit'
        largest = dict.fromkeys(largest, 0)
    elif (largest['max_steps'] < USEFUL_CHILD_STEPS or largest['model_tokens'] < ledger['window']
          or largest['max_seconds'] < MIN_CHILD_SECONDS):
        recommendation, reason = 'solo', 'budget'
    elif largest['max_steps'] < ROOMY_CHILD_STEPS:
        recommendation, reason = 'small_child', 'budget'
    else:
        recommendation, reason = 'delegation_fits', 'budget'
    advice = {'recommendation': recommendation, 'reason': reason, 'root_reserved_steps': reserve['steps'],
              'child_steps_left': cap['steps'], 'largest_child': largest,
              'note': NOTES['tree_limit' if reason == 'tree_limit' else recommendation]}
    if own is not None:
        advice['own_steps_left'] = own['steps_left']
    return advice


def schema_note(max_steps):
    """Static per-tree SpawnAgent schema text; per-step numbers stay in results,
    so tool definitions (and provider prompt caches) do not churn."""
    reserved = reserved_steps(max_steps)
    if reserved >= max_steps:
        return ' Budget: root keeps all ' + str(max_steps) + ' shared steps; no child fits.'
    return (' Budget: root keeps ' + str(reserved) + ' of ' + str(max_steps) + ' shared steps'
            '; omitted limits are host-fitted, unfit explicit ones fail.')


def validate_usage(value):
    """Validate one attempt's provider-reported usage. Never raises.

    Returns ``(counts, state)``. Counts hold input/output/cached tokens or None.
    A missing count stays None and never becomes zero; a reported zero stays
    zero. A malformed record (non-mapping, non-integer, bool, negative or
    implausibly large count, or cached tokens above input tokens) is 'invalid'
    and keeps no counts at all.
    """
    counts = dict.fromkeys(USAGE_FIELDS)
    if value is None or isinstance(value, dict) and not value:
        return counts, 'unreported'
    if not isinstance(value, dict):
        return counts, 'invalid'
    for key in USAGE_FIELDS:
        item = value.get(key)
        if item is None:
            continue
        if type(item) is not int or not 0 <= item <= MAX_REPORTED_TOKENS:
            return dict.fromkeys(USAGE_FIELDS), 'invalid'
        counts[key] = item
    if None not in (counts['cached_tokens'], counts['input_tokens']) and counts['cached_tokens'] > counts['input_tokens']:
        return dict.fromkeys(USAGE_FIELDS), 'invalid'
    if counts['input_tokens'] is not None and counts['output_tokens'] is not None:
        return counts, 'reported'
    if any(item is not None for item in counts.values()):
        return counts, 'partial'
    return counts, 'unreported'


def aggregate_usage(rows):
    """Per-field sums that never turn an unknown attempt into zero.

    ``totals`` holds exact sums, or None for a field any attempt did not
    report. ``known`` holds lower bounds over reporting attempts, and
    ``unknown_attempts`` counts the rest per field. With no attempts the totals
    are a true zero.
    """
    states = Counter(row['state'] for row in rows)
    known = {key: sum(row[key] for row in rows if row[key] is not None) for key in USAGE_FIELDS}
    unknown = {key: sum(row[key] is None for row in rows) for key in USAGE_FIELDS}
    exact = {key: None if unknown[key] else known[key] for key in USAGE_FIELDS}
    return {'attempts': len(rows), 'states': {state: states[state] for state in USAGE_STATES if states[state]},
            'totals': exact, 'known': known, 'unknown_attempts': unknown,
            'complete': exact['input_tokens'] is not None and exact['output_tokens'] is not None}
