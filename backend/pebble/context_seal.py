"""Phase 36 (1.4.0, beat plan W6 stage 2): older tool results of a long task are sealed in large chunks.

Since 1.3.0 every request above 8K extends the previous one, so a provider's cache reads everything before the newest
step and only rewriting earlier context costs a fresh cache write. A long task's window still grows with every result
until the 100 % compaction retires whole exchanges. A seal acts earlier and keeps more: once the window passes a soft
size, the results of older tool exchanges, chosen in one large chunk, are replaced by short stubs. Each stub keeps what
the model needs to trust and recover the result: the tool, its receipt facts (path, hash, status, counts) and a
`page:<generation>:<index>` handle that ReadRunEvidence already serves, byte for byte, from the run's archive. Every
tool call stays, so the model still sees what it did.

Deterministic and model-free. Never sealed: the newest exchanges, a result that reports an error, a test or recipe
result, an archive page read back, and anything that is not a tool result (the request, the person's inputs, the plan,
the constraints, host notes). A seal changes an earlier message, so the provider reads everything after the first
sealed result fresh once; a seal therefore happens only when its chunk is large (SEAL_MIN_TOKENS) and at least
SEAL_PAYBACK times what it makes the provider read fresh, so later requests at a cached price repay it within a few
steps. Past three quarters of the budget (past_offer) that test is waived: ReadRunEvidence joins the tools there
anyway, and the 100 % compaction, which rewrites more, is near. Sizes are context_manifest's conservative estimate,
the measure the runner's other budget decisions use. Off at or below SEAL_FROM_BUDGET and when the app's switch is off.
"""
import copy
import json

from .compaction import fingerprint
from .context_manifest import BASE_TOKENS, ContextError, estimate_tokens

SEAL_FROM_BUDGET = 32768        # only windows larger than this seal
SEAL_SOFT_SHARE = 0.25          # consider a seal once the estimated window passes this share of the budget ...
SEAL_SOFT_TOKENS = 32000        # ... or this many tokens, whichever is smaller (cost is counted in tokens)
SEAL_MIN_TOKENS = 20000         # a chunk is at least this many estimated tokens ...
SEAL_PAYBACK = 2.0              # ... and at least this multiple of what it makes the provider read fresh
KEEP_EXCHANGES = 8              # the newest tool exchanges stay word for word ...
KEEP_MIN_EXCHANGES = 3          # ... at least these, whatever their size ...
KEEP_TOKENS = 6000              # ... and the rest only while their results fit this many estimated tokens
STUB_TOKENS = 170               # the estimated size of one stub (the conservative estimate below)
NEVER_SEALED_TOOLS = frozenset({'RunTests', 'RunRecipe', 'ReadRunEvidence', 'AskUserQuestion', 'UpdatePlan',
                                'UpdateGoal', 'MemoryPropose'})
FACTS = ('receipt_id', 'path', 'file_hash', 'status', 'exit_code', 'outcome', 'start_line', 'total_lines', 'kind',
         'tests', 'snapshot_digest', 'url', 'source_id')
STUB_NOTE = ('Older tool result sealed to keep this long task within its window. The facts in this stub are from the '
             'result; ReadRunEvidence with this receipt_id returns the full result exactly as it was.')


def sealing(app, budget):
    """Whether this window seals: above SEAL_FROM_BUDGET, unless the app's switch is off (PEBBLE_CONTEXT_SEAL=0)."""
    return type(budget) is int and budget > SEAL_FROM_BUDGET and getattr(app, 'context_seal_enabled', True) is not False


def window_tokens(messages):
    """context_manifest's conservative estimate of the window, or None when it cannot read it."""
    try:
        return estimate_tokens(messages)
    except ContextError:
        return None


def past_offer(messages, budget):
    """Whether the window passed three quarters of the budget by context_manifest's conservative estimate. Where sealing
    is on, ReadRunEvidence joins the tools there even without a seal, before the 100 % compaction retires anything; and
    from there that compaction, which rewrites more, is near. So a seal that is possible then is made then, without its
    payback test."""
    tokens = window_tokens(messages)
    return tokens is None or tokens > budget * 3 // 4


def estimate(messages):
    """context_manifest's conservative estimate of a message list (about three UTF-8 bytes per token plus framing,
    additive by message), the measure large_needs and the offer use; a list it cannot read counts three characters of
    its JSON per token."""
    tokens = window_tokens(messages)
    return tokens if tokens is not None else len(json.dumps(messages, ensure_ascii=False)) // 3


def _cost(message):
    """One message's share of estimate() (the estimate is additive; the window framing is counted once)."""
    return estimate([message]) - BASE_TOKENS


def _exchanges(messages):
    """[(assistant index, [(tool message index, tool name)])] for each assistant turn with tool calls, in order."""
    found = []
    for index, message in enumerate(messages):
        if message.get('role') != 'assistant' or not message.get('tool_calls'):
            continue
        names = {c.get('id'): (c.get('function') or {}).get('name') for c in message['tool_calls']}
        results, cursor = [], index + 1
        while cursor < len(messages) and messages[cursor].get('role') == 'tool':
            results.append((cursor, names.get(messages[cursor].get('tool_call_id'))))
            cursor += 1
        found.append((index, results))
    return found


def _sealable(message, name):
    if name in NEVER_SEALED_TOOLS or not isinstance(message.get('content'), str):
        return False
    content = message['content']
    if content.startswith('{"sealed"'):
        return False
    try:
        data = json.loads(content)
    except ValueError:
        return True
    return not (isinstance(data, dict) and (data.get('error') or data.get('status') in ('error', 'failed', 'refused')))


def _facts(content):
    try:
        data = json.loads(content)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: (data[k] if isinstance(data[k], (int, float, bool)) or data[k] is None else str(data[k])[:200])
            for k in FACTS if k in data}


def _kept(exchanges, messages):
    """How many of the newest tool exchanges stay word for word: KEEP_EXCHANGES, or fewer (never below
    KEEP_MIN_EXCHANGES) when their results pass KEEP_TOKENS."""
    kept, tokens = 0, 0
    for _, results in reversed(exchanges):
        size = sum(_cost(messages[i]) for i, _ in results)
        if kept >= KEEP_EXCHANGES or (kept >= KEEP_MIN_EXCHANGES and tokens + size > KEEP_TOKENS):
            break
        kept, tokens = kept + 1, tokens + size
    return kept


def plan(messages, budget, pressing=False):
    """The seal for this window, or None: {'indices': [tool message indices], 'chars': sealed characters,
    'kept_exchanges': n, 'payback_waived': bool}.

    A seal changes an earlier message, so the provider reads everything after the first sealed result fresh once
    (the kept region); later requests read the smaller window from its cache. The chunk must be at least SEAL_PAYBACK
    times that kept region, so it repays itself within a few later requests at a cached price of a tenth. `pressing`
    (past_offer) waives that test: the request changes in this step anyway."""
    if type(budget) is not int or budget <= SEAL_FROM_BUDGET:
        return None
    window = estimate(messages)
    if window <= min(SEAL_SOFT_SHARE * budget, SEAL_SOFT_TOKENS):
        return None
    exchanges = _exchanges(messages)
    keep = _kept(exchanges, messages)
    chosen, chars, sealed_tokens = [], 0, 0
    for _, results in exchanges[:len(exchanges) - keep]:
        for index, name in results:
            if _sealable(messages[index], name):
                chosen.append(index)
                chars += len(messages[index]['content'])
                sealed_tokens += _cost(messages[index])
    if not chosen or sealed_tokens < SEAL_MIN_TOKENS:
        return None
    reread = estimate(messages[chosen[0]:]) - BASE_TOKENS - sealed_tokens + STUB_TOKENS * len(chosen)
    waived = sealed_tokens < SEAL_PAYBACK * reread
    if waived and not pressing:
        return None
    return {'indices': chosen, 'chars': chars, 'tokens': sealed_tokens, 'reread_tokens': reread,
            'kept_exchanges': keep, 'payback_waived': waived, 'window_tokens': window, 'budget': budget}


def seal(messages, chosen, generation):
    """(sealed messages, archive rows, manifest) for one seal. The input list is not changed; the new list holds the
    same message objects except the sealed results, so any message the runner keeps a reference to stays current."""
    sealed = list(messages)
    names = {}
    for _, results in _exchanges(messages):
        names.update({index: name for index, name in results})
    archive = []
    for index in chosen['indices']:
        original = messages[index]
        archive.append({'index': index, 'message': copy.deepcopy(original)})
        facts = _facts(original['content'])
        receipt = facts.pop('receipt_id', None)
        stub = {'sealed': True, 'tool': names.get(index), **facts,
                **({'result_receipt_id': receipt} if receipt else {}), 'chars': len(original['content']),
                'receipt_id': f'page:{generation}:{index}', 'note': STUB_NOTE}
        sealed[index] = {**original, 'content': json.dumps(stub, ensure_ascii=False)}
    manifest = {'version': 1, 'kind': 'seal', 'authorization': 'none',
                'before_fingerprint': fingerprint(messages), 'after_fingerprint': fingerprint(sealed),
                'archived_messages': len(archive), 'archived_results': len(archive),
                'archived_fingerprint': fingerprint(archive), 'sealed_chars': chosen['chars'],
                'sealed_tokens': chosen.get('tokens'), 'reread_tokens': chosen.get('reread_tokens'),
                'kept_exchanges': chosen['kept_exchanges'], 'payback_waived': chosen.get('payback_waived', False),
                'window_tokens': chosen.get('window_tokens'), 'budget': chosen.get('budget'),
                'semantics': 'tool results replaced by stubs with their facts and an archive handle; calls kept'}
    return sealed, archive, manifest
