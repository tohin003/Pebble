"""Connector effect classes, recipient consent, idempotency and readback (D05).

Everything here is host-side around PluginCall; no model tool is added. The
person assigns each allowlisted plugin tool an effect class when reviewing the
plugin (review_effects). Server annotations are untrusted hints that can only
narrow: a tool the server marks as not read-only never serves as a readback.
The reviewed map joins the plugin's catalogue digest, so any drift refuses.

    read     no external effect; may serve as the reviewed readback of another tool
    draft    saved but delivered to nobody
    send     delivered to recipients
    modify   changes an existing remote object
    delete   removes a remote object

A plugin without a reviewed map keeps its earlier behaviour: every call is
natively reviewed with its raw arguments, and its tools count as an unreviewed
'send'. In a reviewed map, a tool without a class counts as 'send'.

Consent. Recipients must come from the person. A send, modify or delete whose
recipient appears only in tool, page or mail output, or nowhere in this run, is
refused (recipient_not_from_person) until the person confirms that exact
recipient set natively (connector_send_confirm). A send also needs an explicit,
non-negated send instruction in the current human turn, aimed at those
recipients; otherwise the model must save a draft. Tool names never change a
class: a send tool called draft_email is still a send.

Idempotence. When the reviewed map names an idempotency pointer, the host
writes pebble-<action_id> there; otherwise the receipt says idempotency 'none'.
Every classified call is journaled 'admitted' before dispatch. A restart turns
'admitted' into 'unknown'; nothing is retried, and an unreconciled identical
send, modify or delete blocks a new proposal.

Readback. readback() is the reconcile verdict for plugin actions. The person
triggers it natively; it calls only the reviewed read-class tool, by
idempotency key, remote ID or subject, recipients and time window. Like the
rest of reconcile it writes nothing; native settlement records the verdict.
Verdicts are evidence with authorization 'none'.
"""

import copy
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone

from .policy import PolicyError


CLASSES = ('read', 'draft', 'send', 'modify', 'delete')
CONFIRMED = ('send', 'modify', 'delete')     # recipients must be the person's, or natively confirmed
FACTS = ('idempotency_key', 'remote_id', 'subject', 'recipient', 'since', 'until')
HINTS = ('readOnlyHint', 'destructiveHint', 'idempotentHint', 'openWorldHint')
SPEC_KEYS = frozenset({'class', 'recipients', 'destination', 'subject', 'body', 'idempotency', 'readback', 'server_hints'})
READBACK_KEYS = frozenset({'tool', 'arguments', 'id_from', 'match'})
MATCH_KEYS = frozenset({'items', 'key', 'id', 'subject', 'recipients', 'time', 'fields'})
STATES = ('admitted', 'responded', 'not_dispatched', 'uncertain', 'unknown')
UNRESOLVED = ('admitted', 'uncertain', 'unknown')
TOKEN = re.compile(r'[A-Za-z0-9_.-]{1,64}|\*')
HEX = re.compile(r'[0-9a-f]{32}')
# Domain labels and dots never overlap, so a long dotted run cannot backtrack.
ADDRESS = re.compile(r'[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}')
MAX_RECIPIENTS = 32
MAX_ITEMS = 500
PREVIEW = 280
KEY_LENGTH = len('pebble-') + 32
WINDOW = 120            # seconds of clock skew around a readback time match
READBACK_TIMEOUT = 10
TEXT_LIMIT = 64_000     # per text, when checking recipient provenance
UNTRUSTED_LIMIT = 2_000_000

# Explicit sending. Weak verbs (email, message, ...) count only with an object,
# so "read my email from x" is not an instruction to send.
VERBS = 'send|sending|reply|respond|forward|post|publish|dispatch|invite'
SEND_VERB = re.compile(r'\b(?:' + VERBS + r')\b', re.I)
WEAK_VERB = re.compile(r"\b(?:e-?mail|mail|message|text|dm|ping)\s+(?:it|this|that|him|her|them|back|to\b|"
                       + ADDRESS.pattern + r')', re.I)
NEGATED = re.compile(r"\b(?:don[’']?t|do\s+not|never|without|not|no\s+need\s+to|shouldn[’']?t|should\s+not|"
                     r"stop|hold\s+off(?:\s+on)?|avoid)\b(?!\s+(?:forget|hesitate|wait)\b)(?:\s+\w+){0,3}?\s+(?:"
                     + VERBS + r"|sent|e-?mail|e-?mailing|mail|message)\b", re.I)
REPLY_NOUN = re.compile(r'\b(?:draft|write|prepare|compose)\s+(?:a|an|the|my)?\s*(?:reply|response|answer)\b', re.I)
DRAFT_ONLY = re.compile(r'\b(?:just|only)\s+(?:a\s+|the\s+)?draft\b|\bdraft\s+only\b|\bas\s+a\s+draft\b', re.I)

DDL = (
    'CREATE TABLE IF NOT EXISTS connector_effects(action_id TEXT PRIMARY KEY,chat_id TEXT,run_id TEXT,'
    'plugin_id TEXT NOT NULL,tool TEXT NOT NULL,effect_class TEXT NOT NULL,assigned TEXT NOT NULL,'
    'catalogue_digest TEXT NOT NULL,request_digest TEXT NOT NULL,idempotency_key TEXT,'
    'recipients_sha256 TEXT NOT NULL,facts TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)',
    'CREATE INDEX IF NOT EXISTS connector_effects_request ON connector_effects(request_digest,state)',
    'CREATE TABLE IF NOT EXISTS connector_effect_confirmations(action_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,'
    'recipients_sha256 TEXT NOT NULL,confirmed REAL NOT NULL)',
)
MESSAGES = {
    'missing': ('send_intent_missing: sending needs the person\'s explicit instruction in this turn, '
                'such as "send it to name@example.com". Save a draft instead'),
    'negated': 'send_intent_negated: the person asked not to send, or only to draft. Save a draft instead',
    'target_mismatch': ('send_target_mismatch: the person\'s send instruction names other recipients. '
                        'Save a draft or ask the person'),
}


def packed(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def sha256(value):
    return hashlib.sha256((value if isinstance(value, str) else packed(value)).encode()).hexdigest()


# JSON pointers (RFC 6901 subset: plain tokens, numeric indices, one '*' fan-out)

def pointer(value, *, wildcard=False, empty=False):
    """Tokens of a reviewed pointer, or PolicyError. '' (the whole value) only when empty is allowed."""
    if empty and value == '':
        return ()
    if not isinstance(value, str) or not value.startswith('/') or len(value) > 512:
        raise PolicyError('Effect fields are JSON pointers such as /to or /message/subject.')
    tokens = tuple(value[1:].split('/'))
    if (not 1 <= len(tokens) <= 8 or not all(TOKEN.fullmatch(t) for t in tokens)
            or tokens.count('*') > (1 if wildcard else 0)):
        raise PolicyError('Effect pointers have one to eight plain tokens' + (' and at most one *.' if wildcard else '.'))
    return tokens


def _types(node):
    kind = node.get('type') if isinstance(node, dict) else None
    return {kind} if isinstance(kind, str) else set(kind) if isinstance(kind, list) else set()


def _schema_at(schema, tokens):
    """The subschema a pointer reaches through plain properties/items, or None."""
    node = schema
    for token in tokens:
        if not isinstance(node, dict) or '$ref' in node or '$dynamicRef' in node:
            return None
        if token == '*' or token.isdigit():
            if 'array' not in _types(node):
                return None
            node = node.get('items')
        else:
            properties = node.get('properties')
            if not isinstance(properties, dict) or token not in properties:
                return None
            node = properties[token]
    return node if isinstance(node, dict) and '$ref' not in node and '$dynamicRef' not in node else None


def _values(value, tokens):
    """Every value at tokens; '*' fans out over a list."""
    current = [value]
    for token in tokens:
        found = []
        for node in current:
            if token == '*':
                if isinstance(node, list):
                    found.extend(node[:MAX_RECIPIENTS + 1])
            elif isinstance(node, dict):
                if token in node:
                    found.append(node[token])
            elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                found.append(node[int(token)])
        current = found
    return current


def _one(value, tokens):
    if tokens == ():
        return value
    found = _values(value, tokens)
    return found[0] if found else None


def _assign(value, tokens, item):
    node = value
    for token in tokens[:-1]:
        if not isinstance(node, dict):
            raise PolicyError('The reviewed idempotency field cannot be set in these arguments.')
        node = node.setdefault(token, {})
    if not isinstance(node, dict):
        raise PolicyError('The reviewed idempotency field cannot be set in these arguments.')
    previous = node.get(tokens[-1])
    node[tokens[-1]] = item
    return previous


def _drop(value, tokens):
    node = value
    for token in tokens[:-1]:
        if not isinstance(node, dict) or token not in node:
            return
        node = node[token]
    if isinstance(node, dict):
        node.pop(tokens[-1], None)


# Review: the person's class map, checked against the reviewed schemas

def _hints(spec):
    annotations = spec.get('annotations') if isinstance(spec, dict) else None
    if not isinstance(annotations, dict):
        return {}
    return {k: annotations[k] for k in HINTS if type(annotations.get(k)) is bool}


def _string_at(schema, tokens, what, *, numeric=False, array=False):
    """Refuse unless the pointer reaches a string (or an array of strings / a number when allowed)."""
    node = _schema_at(schema, tokens)
    kinds = _types(node)
    if node is not None and ('string' in kinds or numeric and kinds & {'number', 'integer'}):
        return node
    if node is not None and array and 'array' in kinds and 'string' in _types(node.get('items')):
        return node
    raise PolicyError(f'The reviewed {what} pointer must reach a declared string field of the tool schema.')


def readback_eligible(spec, reviewed):
    """A read-class tool the server does not mark as writing."""
    hints = (reviewed or {}).get('server_hints') or _hints(spec)
    return (reviewed or {}).get('class') == 'read' and hints.get('readOnlyHint') is not False \
        and hints.get('destructiveHint') is not True


def _review_tool(tool, spec, specs):
    if not isinstance(spec, dict) or not spec.keys() <= SPEC_KEYS or spec.get('class') not in CLASSES:
        raise PolicyError(f'Give {tool} one effect class: read, draft, send, modify or delete.')
    cls = spec['class']
    schema = specs[tool].get('inputSchema') if specs[tool] is not None else None
    reviewed = {'class': cls, 'recipients': [], 'destination': None, 'subject': None, 'body': None,
                'idempotency': None, 'readback': None, 'server_hints': _hints(specs[tool])}
    if cls == 'read':
        if any(spec.get(k) not in (None, []) for k in ('recipients', 'idempotency', 'readback')):
            raise PolicyError('Read-class tools have no recipients, idempotency key or readback.')
    recipients = spec.get('recipients', [])
    if not isinstance(recipients, list) or len(recipients) > 4 or len(set(map(str, recipients))) != len(recipients):
        raise PolicyError('Name at most four distinct recipient pointers.')
    for value in recipients:
        tokens = pointer(value, wildcard=True)
        if schema is not None:
            _string_at(schema, tokens, 'recipient', array=True)
        reviewed['recipients'].append(value)
    for key in ('destination', 'subject', 'body'):
        if spec.get(key) is not None:
            tokens = pointer(spec[key])
            if schema is not None:
                _string_at(schema, tokens, key, numeric=key == 'destination')
            reviewed[key] = spec[key]
    if spec.get('idempotency') is not None:
        tokens = pointer(spec['idempotency'])
        if schema is not None:
            node = _string_at(schema, tokens, 'idempotency')
            if (any(k in node for k in ('enum', 'const', 'pattern', 'format', 'contentEncoding'))
                    or type(node.get('maxLength', KEY_LENGTH)) is not int or node.get('maxLength', KEY_LENGTH) < KEY_LENGTH
                    or type(node.get('minLength', 0)) is not int or node.get('minLength', 0) > KEY_LENGTH):
                raise PolicyError('The idempotency field must accept the host key pebble-<action id> unchanged.')
        reviewed['idempotency'] = spec['idempotency']
    if spec.get('readback') is not None:
        reviewed['readback'] = _review_readback(tool, cls, spec['readback'], reviewed, specs)
    return reviewed


def _review_readback(tool, cls, value, reviewed, specs):
    if not isinstance(value, dict) or not value.keys() <= READBACK_KEYS or not {'tool', 'arguments', 'match'} <= value.keys():
        raise PolicyError('A readback names its read tool, arguments and match.')
    reader = value['tool']
    if not isinstance(reader, str) or reader not in specs or reader == tool:
        raise PolicyError('A readback uses another allowlisted tool of this plugin.')
    arguments = value['arguments']
    if not isinstance(arguments, dict) or not 1 <= len(arguments) <= 6 or any(f not in FACTS for f in arguments.values()):
        raise PolicyError('Readback arguments map one to six pointers to host facts.')
    for target in arguments:
        pointer(target)
    id_from = value.get('id_from')
    if id_from is not None:
        if not isinstance(id_from, dict) or len(id_from) != 1 or next(iter(id_from)) not in ('arguments', 'result'):
            raise PolicyError('A readback takes the remote ID from the effect arguments or its result.')
        source, target = next(iter(id_from.items()))
        tokens = pointer(target)
        if specs[tool] is not None and source == 'arguments':
            _string_at(specs[tool].get('inputSchema'), tokens, 'remote ID', numeric=True)
        if source == 'result' and cls == 'delete':
            raise PolicyError('A delete readback takes the remote ID from its arguments.')
    match = value['match']
    if not isinstance(match, dict) or not match.keys() <= MATCH_KEYS or 'items' not in match:
        raise PolicyError('A readback match names the result items and the fields to compare.')
    pointer(match['items'], empty=True)
    for key in ('key', 'id', 'subject', 'time'):
        if match.get(key) is not None:
            pointer(match[key])
    if match.get('recipients') is not None:
        pointer(match['recipients'], wildcard=True)
    fields = match.get('fields')
    if fields is not None:
        if cls != 'modify' or not isinstance(fields, dict) or not 1 <= len(fields) <= 8:
            raise PolicyError('Field comparisons are for modify readbacks: one to eight item-to-argument pointers.')
        for item, argument in fields.items():
            pointer(item), pointer(argument)
            if specs[tool] is not None and _schema_at(specs[tool].get('inputSchema'), pointer(argument)) is None:
                raise PolicyError('A compared field must be a declared argument of the effect tool.')
    if not any(match.get(k) is not None for k in ('key', 'id', 'subject', 'recipients')):
        raise PolicyError('A readback matches by idempotency key, remote ID, subject or recipients.')
    need = {'idempotency_key': reviewed['idempotency'], 'remote_id': id_from, 'subject': reviewed['subject'],
            'recipient': reviewed['recipients']}
    used = set(arguments.values()) | {name for name, key in (('idempotency_key', 'key'), ('remote_id', 'id'),
                                      ('subject', 'subject'), ('recipient', 'recipients')) if match.get(key) is not None}
    if any(not need[f] for f in used if f in need):
        raise PolicyError('A readback uses only facts the effect review records (key, remote ID, subject, recipients).')
    if cls in ('modify', 'delete') and (not id_from or match.get('id') is None):
        raise PolicyError('Modify and delete readbacks find the object by its remote ID.')
    if specs[reader] is not None:
        schema = specs[reader].get('inputSchema')
        for target, fact in arguments.items():
            _string_at(schema, pointer(target), 'readback argument', numeric=fact in ('since', 'until', 'remote_id'))
    return {'tool': reader, 'arguments': dict(sorted(arguments.items())), 'id_from': id_from,
            'match': {k: match[k] for k in sorted(match) if match[k] is not None}}


def review_effects(schemas, effects):
    """The person's normalized class map for these reviewed schemas, or PolicyError.

    effects maps tool -> {class, recipients: [pointer], destination, subject, body,
    idempotency, readback: {tool, arguments: {pointer: fact}, id_from, match}}.
    None (no map) returns None: the plugin keeps its unreviewed behaviour.
    schemas=None checks shape only (at install, before the server is reached).
    """
    if effects is None:
        return None
    specs = ({s['name']: s for s in schemas if isinstance(s, dict) and isinstance(s.get('name'), str)}
             if not isinstance(schemas, dict) else schemas)
    if not isinstance(effects, dict) or not 1 <= len(effects) <= 8 or not effects.keys() <= specs.keys():
        raise PolicyError('Effect classes apply to one to eight allowlisted plugin tools.')
    reviewed = {tool: _review_tool(tool, effects[tool], specs) for tool in sorted(effects)}
    for tool, spec in reviewed.items():
        reader = spec['readback'] and spec['readback']['tool']
        if reader and (reader not in reviewed or not readback_eligible(specs[reader], reviewed[reader])):
            raise PolicyError('A readback tool must be reviewed as read-class and not marked writing by its server.')
    return reviewed


def check_effects(effects, tools):
    """Shape-only review at install time, before any schema is known."""
    return review_effects({name: None for name in tools}, effects)


# Classification

def _text(value):
    if isinstance(value, str):
        return value
    return packed(value) if value is not None else None


def _address(value):
    text = value.strip()
    angle = re.fullmatch(r'[^<>]*<([^<>\s]{1,320})>\s*', text)
    if angle:
        text = angle[1]
    if text.lower().startswith('mailto:'):
        text = text[7:]
    return text.casefold()


def _mentioned(value, texts):
    if not value:
        return False
    found = re.compile(r'(?<![\w@.+%-])' + re.escape(value) + r'(?![\w@+%-])(?!\.\w)')
    return any(found.search(text) for text in texts)


def _bounded(texts, limit):
    out, total = [], 0
    for text in texts or ():
        if isinstance(text, str):
            text = text[:TEXT_LIMIT].casefold()
            total += len(text)
            if total > limit:
                break
            out.append(text)
    return out


def send_intent(current, recipients=()):
    """'explicit', 'missing', 'negated' or 'target_mismatch' for the current human turn."""
    text = current if isinstance(current, str) else ''
    if NEGATED.search(text) or DRAFT_ONLY.search(text):
        return 'negated'
    if not (SEND_VERB.search(text) or WEAK_VERB.search(text)):
        return 'missing'
    # T7 F10: "draft a reply to ..." names a reply as a thing to write, not an instruction to send.
    rest = REPLY_NOUN.sub(' ', text)
    if rest != text and not (SEND_VERB.search(rest) or WEAK_VERB.search(rest)):
        return 'missing'
    folded = text[:TEXT_LIMIT].casefold()
    if ADDRESS.search(text) and any(not _mentioned(_address(r), [folded]) for r in recipients):
        return 'target_mismatch'
    return 'explicit'


def effect_facts(config, tool, args):
    """The static facts of one call: class, recipients and the review card fields. No provenance."""
    effects = config.get('effects')
    spec = effects.get(tool) if isinstance(effects, dict) else None
    assigned = 'unreviewed' if effects is None else 'person' if spec else 'default'
    recipients = []
    for value in (spec or {}).get('recipients', []):
        for found in _values(args, pointer(value, wildcard=True)):
            for item in found if isinstance(found, list) else [found]:
                if item is None:
                    continue
                if not isinstance(item, str) or not 1 <= len(item) <= 320 or any(ord(c) < 32 for c in item):
                    raise PolicyError('recipients_invalid: each recipient is a short single-line string.')
                recipients.append(item)
    if len(recipients) > MAX_RECIPIENTS:
        raise PolicyError(f'recipients_invalid: at most {MAX_RECIPIENTS} recipients per call.')
    shown = {}
    for key in ('destination', 'subject', 'body'):
        target = (spec or {}).get(key)
        shown[key] = _text(_one(args, pointer(target))) if target else None
    body = shown['body']
    addresses = sorted({_address(r) for r in recipients})
    return {'class': spec['class'] if spec else 'send', 'assigned': assigned, 'recipients': recipients,
            'recipient_count': len(addresses), 'recipients_sha256': sha256(addresses),
            'destination': shown['destination'][:PREVIEW] if shown['destination'] else None,
            'subject': shown['subject'][:PREVIEW] if shown['subject'] else None,
            'body_sha256': sha256(body) if body is not None else None,
            'body_preview': ' '.join(body.split())[:PREVIEW] if body else None,
            'idempotency': 'host_injected' if spec and spec.get('idempotency') else 'none'}


def classify_call(config, tool, args, human_texts, untrusted_texts=()):
    """{'class','recipients','destination','idempotency_key','requires_review':True,'blocked',...}.

    human_texts are this run's human-origin turns, oldest first; the last is the
    current turn. untrusted_texts are tool, page and mail output of this run.
    blocked is None or a refusal code; requires_confirmation means the person
    must confirm the recipient set natively before execution.
    """
    facts = effect_facts(config, tool, args)
    human, untrusted = _bounded(human_texts, UNTRUSTED_LIMIT), _bounded(untrusted_texts, UNTRUSTED_LIMIT)
    sources = {}
    for recipient in facts['recipients']:
        address = _address(recipient)
        sources[recipient] = ('person' if _mentioned(address, human) else
                              'untrusted_output' if _mentioned(address, untrusted) else 'unknown')
    reviewed = facts['assigned'] != 'unreviewed'
    unconfirmed = [r for r in facts['recipients'] if sources[r] != 'person']
    current = human_texts[-1] if human_texts and isinstance(human_texts[-1], str) else ''
    intent = send_intent(current, facts['recipients'])
    blocked = None
    if reviewed and facts['class'] == 'send' and intent != 'explicit':
        blocked = {'missing': 'send_intent_missing', 'negated': 'send_intent_negated',
                   'target_mismatch': 'send_target_mismatch'}[intent]
    return {**facts, 'recipient_sources': sources, 'unconfirmed_recipients': unconfirmed,
            'requires_confirmation': reviewed and facts['class'] in CONFIRMED and bool(unconfirmed),
            'send_intent': intent, 'idempotency_key': None, 'requires_review': True, 'blocked': blocked,
            'authorization': 'none'}


def request_digest(config, plugin_id, tool, args):
    """Identity of one requested effect, without the host-injected idempotency key."""
    spec = (config.get('effects') or {}).get(tool) or {}
    value = copy.deepcopy(args)
    if spec.get('idempotency'):
        _drop(value, pointer(spec['idempotency']))
    return sha256({'plugin_id': plugin_id, 'tool': tool, 'arguments': value})


def inject_idempotency(config, tool, args, action_id):
    """{'arguments','idempotency_key','idempotency','replaced_model_value'}; the host key wins."""
    spec = (config.get('effects') or {}).get(tool) or {}
    if not spec.get('idempotency'):
        return {'arguments': args, 'idempotency_key': None, 'idempotency': 'none', 'replaced_model_value': False}
    if not isinstance(action_id, str) or not HEX.fullmatch(action_id):
        raise PolicyError('An idempotency key needs its broker action.')
    key = 'pebble-' + action_id
    value = copy.deepcopy(args)
    previous = _assign(value, pointer(spec['idempotency']), key)
    return {'arguments': value, 'idempotency_key': key, 'idempotency': key,
            'replaced_model_value': previous is not None and previous != key}


def result_remote_id(config, tool, result):
    """The remote object ID a reviewed readback takes from the effect's structured result, or None."""
    spec = (config.get('effects') or {}).get(tool) or {}
    id_from = (spec.get('readback') or {}).get('id_from') or {}
    structured = result.get('untrusted_structured') if result.get('has_structured') else None
    if 'result' not in id_from or not isinstance(structured, dict):
        return None
    found = _one(structured, pointer(id_from['result']))
    return str(found)[:256] if isinstance(found, (str, int)) and type(found) is not bool else None


# Host-collected human and untrusted text of a run

def _table(store, name):
    return bool(store.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _strings(value, out):
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out)
    elif isinstance(value, list):
        for item in value:
            _strings(item, out)
    return out


def run_texts(store, run_id):
    """(human turns oldest first, untrusted tool output) of a root run. Only the host writes these origins."""
    rows = store.query('SELECT goal,created FROM runs WHERE id=?', (run_id,)) if run_id and _table(store, 'runs') else []
    if not rows:
        return [], []
    turns = [(rows[0]['created'], rows[0]['goal'])]
    if _table(store, 'run_inbox'):
        for row in store.query("SELECT created,payload FROM run_inbox WHERE run_id=? AND origin='human' AND kind='steering'",
                               (run_id,)):
            text = json.loads(row['payload']).get('text')
            if isinstance(text, str):
                turns.append((row['created'], text))
    if _table(store, 'run_questions'):
        for row in store.query("SELECT answered,answer FROM run_questions WHERE run_id=? AND status='answered' "
                               'AND answer IS NOT NULL', (run_id,)):
            turns.append((row['answered'] or 0, ' '.join(_strings(json.loads(row['answer']), []))))
    untrusted, total = [], 0
    if _table(store, 'run_tools'):
        for row in store.query('SELECT result FROM run_tools WHERE run_id=? AND result IS NOT NULL ORDER BY rowid', (run_id,)):
            total += len(row['result'])
            if total > UNTRUSTED_LIMIT:
                break
            untrusted.append(row['result'])
    return [text for _, text in sorted(turns, key=lambda pair: pair[0])], untrusted


# The effect ledger

class ConnectorEffects:
    """Journal of classified connector calls: admitted before dispatch, never replayed.

    Construct once per process at startup: recovery turns every 'admitted' row
    into 'unknown', because a previous process may have dispatched it.
    """

    def __init__(self, store, *, clock=time.time, recover=True):
        self.store, self.clock = store, clock
        for statement in DDL:
            store.query(statement)
        if recover:
            store.query("UPDATE connector_effects SET state='unknown',updated=? WHERE state='admitted'", (clock(),))

    def classify(self, item, payload, human_texts, untrusted_texts=(), exposure=None):
        """The proposal payload with its host classification, or PolicyError when it must not be proposed."""
        config = item['config']
        effect = classify_call(config, payload['tool'], payload['arguments'], human_texts, untrusted_texts)
        effect['request_sha256'] = request_digest(config, payload['plugin_id'], payload['tool'], payload['arguments'])
        effect['connector'] = {'name': item['name'], 'credential_exposure': exposure}
        if effect['blocked']:
            drafts = sorted(t for t, s in (config.get('effects') or {}).items() if s['class'] == 'draft')
            raise PolicyError(MESSAGES[effect['send_intent']] + (' (draft tools: ' + ', '.join(drafts) + ')' if drafts else '') + '.')
        if effect['class'] in CONFIRMED:
            if self.unresolved(effect['request_sha256']):
                raise PolicyError('duplicate_unreconciled: an identical connector effect has an unknown outcome. '
                                  'Reconcile it before proposing it again; effects are never replayed.')
            effect['repeat_of'] = self.completed(effect['request_sha256'])[:4]
        return {**payload, 'effect': effect}

    def check(self, item, payload, action_id):
        """Before dispatch: the reviewed classification still holds and any confirmation exists."""
        effect = payload.get('effect')
        facts = effect_facts(item['config'], payload['tool'], payload['arguments'])
        if (not isinstance(effect, dict) or any(effect.get(k) != facts[k] for k in ('class', 'assigned', 'recipients_sha256', 'idempotency'))
                or effect.get('request_sha256') != request_digest(item['config'], payload['plugin_id'], payload['tool'], payload['arguments'])):
            raise PolicyError('The connector effect classification changed after review; propose it again.')
        if effect.get('blocked'):
            raise PolicyError('This connector effect was blocked at proposal.')
        if effect.get('requires_confirmation') and not self.confirmed(action_id, effect['recipients_sha256']):
            raise PolicyError('recipient_not_from_person: confirm these recipients natively before approving.')
        return effect

    def confirm(self, chat_id, action_id, recipients_sha256):
        """Native only (connector_send_confirm): the person confirms the exact reviewed recipient set."""
        rows = self.store.query('SELECT kind,status,payload FROM actions WHERE id=? AND chat_id=?', (action_id, chat_id))
        effect = json.loads(rows[0]['payload']).get('effect') if rows else None
        if (not rows or rows[0]['kind'] != 'plugin' or rows[0]['status'] != 'pending' or not isinstance(effect, dict)
                or not effect.get('requires_confirmation') or not isinstance(recipients_sha256, str)
                or effect.get('recipients_sha256') != recipients_sha256):
            raise PolicyError('Only a pending connector effect with these exact reviewed recipients can be confirmed.')
        self.store.query('INSERT OR REPLACE INTO connector_effect_confirmations VALUES(?,?,?,?)',
                         (action_id, chat_id, recipients_sha256, self.clock()))
        return {'action_id': action_id, 'recipients_sha256': recipients_sha256, 'state': 'confirmed',
                'confirmed_recipients': len(effect.get('unconfirmed_recipients', []))}

    def confirmed(self, action_id, recipients_sha256):
        return bool(self.store.query('SELECT 1 FROM connector_effect_confirmations c JOIN actions a ON a.id=c.action_id '
                                     'AND a.chat_id=c.chat_id WHERE c.action_id=? AND c.recipients_sha256=?',
                                     (action_id, recipients_sha256)))

    def admit(self, action_id, payload, item, run_id, injected):
        """Commit 'admitted' before dispatch. A second admission of one action is refused: never replayed."""
        effect = payload['effect']
        rows = self.store.query('SELECT chat_id,kind FROM actions WHERE id=?', (action_id,))
        if not rows or rows[0]['kind'] != 'plugin':
            raise PolicyError('A connector effect needs its broker action.')
        spec = (item['config'].get('effects') or {}).get(payload['tool']) or {}
        remote = None
        id_from = (spec.get('readback') or {}).get('id_from') or {}
        if 'arguments' in id_from:
            remote = _one(payload['arguments'], pointer(id_from['arguments']))
        facts = {'recipients': sorted({_address(r) for r in effect.get('recipients', [])}), 'subject': effect.get('subject'),
                 'remote_id': str(remote)[:256] if isinstance(remote, (str, int)) and type(remote) is not bool else None,
                 'admitted_at': self.clock(), 'is_error': None}
        with self.store.lock:
            if self.store.db.in_transaction:
                raise PolicyError('Connector effects are journaled outside a transaction.')
            if self.unresolved(effect['request_sha256'], exclude=action_id):
                raise PolicyError('duplicate_unreconciled: an identical connector effect has an unknown outcome.')
            try:
                with self.store.db as db:
                    db.execute('INSERT INTO connector_effects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                        action_id, rows[0]['chat_id'], run_id, payload['plugin_id'], payload['tool'], effect['class'],
                        effect['assigned'], payload['digest'], effect['request_sha256'], injected['idempotency_key'],
                        effect['recipients_sha256'], packed(facts), 'admitted', facts['admitted_at'], facts['admitted_at']))
            except sqlite3.IntegrityError:
                raise PolicyError('This connector effect was already admitted; it is never replayed.') from None
            except sqlite3.Error:
                raise PolicyError('The connector effect could not be journaled; nothing was sent.') from None
        return facts

    def settle(self, action_id, state, *, is_error=None, remote_id=None):
        """Record the transport outcome of an admitted call. Only 'admitted' rows move."""
        if state not in ('responded', 'not_dispatched', 'uncertain'):
            raise ValueError('Unknown connector effect outcome.')
        with self.store.lock:
            rows = self.store.query("SELECT facts FROM connector_effects WHERE action_id=? AND state='admitted'", (action_id,))
            if not rows:
                return False
            facts = json.loads(rows[0]['facts'])
            facts['is_error'] = is_error if type(is_error) is bool else None
            if remote_id is not None and facts.get('remote_id') is None:
                facts['remote_id'] = str(remote_id)[:256]
            self.store.query("UPDATE connector_effects SET state=?,facts=?,updated=? WHERE action_id=? AND state='admitted'",
                             (state, packed(facts), self.clock(), action_id))
        return True

    def record(self, action_id):
        rows = self.store.query('SELECT * FROM connector_effects WHERE action_id=?', (action_id,))
        if not rows:
            return None
        return {**rows[0], 'facts': json.loads(rows[0]['facts'])}

    def _owned(self, request):
        """Rows of one requested effect with their owning run tool's state and result, if any."""
        owned = _table(self.store, 'run_action_owners') and _table(self.store, 'run_tools')
        query = ('SELECT e.action_id,e.effect_class,e.state,e.facts,'
                 + ('t.state AS tool_state,t.result AS tool_result ' if owned else 'NULL AS tool_state,NULL AS tool_result ')
                 + 'FROM connector_effects e ' + ('LEFT JOIN run_action_owners o ON o.action_id=e.action_id '
                 'LEFT JOIN run_tools t ON t.run_id=o.run_id AND t.call_id=o.call_id ' if owned else '')
                 + 'WHERE e.request_digest=? ORDER BY e.created')
        return self.store.query(query, (request,))

    def unresolved(self, request, exclude=None):
        """Action IDs of identical effects whose outcome is unknown and not yet reconciled."""
        return [r['action_id'] for r in self._owned(request)
                if r['action_id'] != exclude and r['effect_class'] in CONFIRMED and r['state'] in UNRESOLVED
                and r['tool_state'] in (None, 'running', 'uncertain')]

    def completed(self, request):
        """Action IDs of identical effects that the connector reported, or the person settled, as done."""
        done = []
        for r in self._owned(request):
            settled = json.loads(r['tool_result']) if r['tool_result'] and r['tool_state'] == 'completed' else {}
            if (r['state'] == 'responded' and json.loads(r['facts']).get('is_error') is False
                    or r['state'] in UNRESOLVED and isinstance(settled, dict) and settled.get('effect') == 'occurred'):
                done.append(r['action_id'])
        return done

    def run_effects(self, run_id):
        return _run_effects(self.store, run_id)

    def unresolved_in_chat(self, chat_id):
        """Action IDs of this chat's send, modify or delete effects whose outcome is unknown and unreconciled."""
        requests = {r['request_digest'] for r in self.store.query(
            "SELECT request_digest FROM connector_effects WHERE chat_id=? AND state IN ('admitted','uncertain','unknown')",
            (chat_id,))}
        mine = {r['action_id'] for r in self.store.query('SELECT action_id FROM connector_effects WHERE chat_id=?', (chat_id,))}
        return sorted({a for request in requests for a in self.unresolved(request) if a in mine})

    def forget_chat(self, chat_id):
        rows = self.store.query('SELECT action_id FROM connector_effects WHERE chat_id=?', (chat_id,))
        self.store.query('DELETE FROM connector_effects WHERE chat_id=?', (chat_id,))
        self.store.query('DELETE FROM connector_effect_confirmations WHERE chat_id=?', (chat_id,))
        return len(rows)


# Summaries and completion

def summary(payload, result):
    """The native summary of a classified effect, or None to keep the unreviewed wording."""
    effect = payload.get('effect') if isinstance(payload, dict) else None
    if not isinstance(effect, dict) or effect.get('assigned') == 'unreviewed':
        return None
    label = '**' + payload['name'] + ' / ' + payload['tool'] + '**'
    cls, count = effect.get('class'), effect.get('recipient_count', 0)
    if result.get('is_error'):
        head = f'{label} reported an error. Nothing is confirmed to have been {"read" if cls == "read" else "changed or sent"}.'
    elif cls == 'draft':
        head = f'Draft saved (not sent) through {label}.'
    elif cls == 'send':
        head = f'Sent to {count} recipient{"" if count == 1 else "s"} through {label}, as reported by the connector.'
    elif cls == 'modify':
        head = f'Changed through {label}, as reported by the connector.'
    elif cls == 'delete':
        head = f'Deleted through {label}, as reported by the connector.'
    else:
        head = f'{label} returned the following untrusted result:'
    output = result.get('untrusted_plugin_output', '')[:5000].replace('```', '` ` `')
    return head + '\n\n```text\n' + output + '\n```'


def _run_effects(store, run_id):
    """Content-free receipts of a run's connector effects, oldest first."""
    return [{'action_id': r['action_id'], 'tool': r['tool'], 'class': r['effect_class'], 'state': r['state'],
             'is_error': json.loads(r['facts']).get('is_error')}
            for r in store.query('SELECT action_id,tool,effect_class,state,facts FROM connector_effects '
                                 'WHERE run_id=? ORDER BY created', (run_id,))]


def completion_issues(store, run_id):
    """[{'code': 'draft_only', ...}] when the person asked to send but only a draft was saved."""
    human, _ = run_texts(store, run_id)
    if not human or send_intent(human[-1]) != 'explicit' or not _table(store, 'connector_effects'):
        return []
    rows = _run_effects(store, run_id)
    done = [r for r in rows if r['state'] == 'responded' and r['is_error'] is False]
    if any(r['class'] == 'draft' for r in done) and not any(r['class'] == 'send' for r in done):
        return [{'code': 'draft_only', 'message': 'The person asked to send, but only a draft was saved; nothing was sent.',
                 'actions': [r['action_id'] for r in done if r['class'] == 'draft'][:8]}]
    return []


# Readback: the reconcile verdict for a plugin action

def _seconds(value):
    if type(value) in (int, float):
        return float(value)
    if isinstance(value, str) and len(value) <= 64:
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _fact_value(fact, row, schema_node, now):
    facts = row['facts']
    value = {'idempotency_key': row['idempotency_key'], 'remote_id': facts.get('remote_id'), 'subject': facts.get('subject'),
             'recipient': (facts.get('recipients') or [None])[0], 'since': facts['admitted_at'] - WINDOW,
             'until': now + WINDOW}[fact]
    if fact in ('since', 'until') and 'string' in _types(schema_node) and not _types(schema_node) & {'number', 'integer'}:
        return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    if fact == 'remote_id' and _types(schema_node) & {'integer'} and isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def _compare(cls, match, row, items, arguments, now):
    """(verdict, basis, facts) from the read tool's items. Exact comparisons only; nothing is echoed."""
    facts = row['facts']

    def at(item, key, many=False):
        found = _values(item, pointer(match[key], wildcard=many))
        return found if many else (found[0] if found else None)

    if match.get('key') and row['idempotency_key']:
        hits = [i for i in items if at(i, 'key') == row['idempotency_key']]
        if hits:
            return 'occurred', 'found_by_key', {'match_basis': 'idempotency_key', 'matches': len(hits),
                                                'duplicates': len(hits) - 1}
        return 'not_occurred', 'absent_by_key', {'match_basis': 'idempotency_key', 'matches': 0}
    if match.get('id') and facts.get('remote_id') is not None:
        hits = [i for i in items if at(i, 'id') is not None and str(at(i, 'id')) == facts['remote_id']]
        if cls == 'delete':
            return (('not_occurred', 'item_present') if hits else ('occurred', 'item_absent')) + ({'match_basis': 'remote_id', 'matches': len(hits)},)
        if not hits:
            return 'diverged', 'item_absent', {'match_basis': 'remote_id', 'matches': 0}
        fields = match.get('fields')
        if cls == 'modify' and fields:
            equal = all(_one(hits[0], pointer(k)) == _one(arguments, pointer(v)) for k, v in fields.items())
            if not equal:
                return 'unknown', 'after_state_unproven', {'match_basis': 'remote_id', 'matches': len(hits)}
        return 'occurred', 'found_by_id', {'match_basis': 'remote_id', 'matches': len(hits)}
    if cls in ('draft', 'send') and (match.get('subject') or match.get('recipients')):
        wanted = set(facts.get('recipients') or [])
        hits = []
        for item in items:
            if match.get('subject') and (facts.get('subject') is None or at(item, 'subject') != facts['subject']):
                continue
            if match.get('recipients'):
                got = {_address(v) for found in at(item, 'recipients', True)
                       for v in (found if isinstance(found, list) else [found]) if isinstance(v, str)}
                if not wanted or not wanted <= got:
                    continue
            if match.get('time'):
                moment = _seconds(at(item, 'time'))
                if moment is None or not facts['admitted_at'] - WINDOW <= moment <= now + WINDOW:
                    continue
            hits.append(item)
        evidence = {'match_basis': 'fields', 'matches': len(hits)}
        if len(hits) == 1:
            return 'occurred', 'found_by_fields', evidence
        return 'unknown', 'ambiguous_match' if hits else 'absent_by_fields', evidence
    return 'unknown', 'no_match_facts', {}


def readback(app, durable, base):
    """The reconcile verdict for one owned plugin action. Calls only the reviewed read-class tool."""
    from .reconcile import _result
    payload = durable['payload']
    effect = payload.get('effect')
    if payload.get('data_kind') or not isinstance(effect, dict):
        return _result('unknown', 'remote_effect', base)
    extensions = getattr(app, 'extensions', None)
    ledger = getattr(extensions, 'effects', None)
    base = {**base, 'effect_class': effect.get('class'), 'plugin_id': payload.get('plugin_id'), 'tool': payload.get('tool')}
    if ledger is None:
        return _result('unknown', 'no_service', base)
    row = ledger.record(durable['id'])
    if row is None:
        if durable['status'] == 'applied':
            return _result('unknown', 'record_missing', base)
        # The ledger commits 'admitted' before dispatch, so without a row nothing was sent.
        return _result('not_occurred', 'no_admission_record', base)
    if (row['chat_id'] != durable['chat_id'] or row['plugin_id'] != payload.get('plugin_id') or row['tool'] != payload.get('tool')
            or row['request_digest'] != effect.get('request_sha256') or row['catalogue_digest'] != payload.get('digest')):
        return _result('unknown', 'binding_mismatch', base)
    base = {**base, 'effect_state': row['state']}
    if row['state'] == 'not_dispatched':
        return _result('not_occurred', 'not_dispatched', base)
    if row['state'] == 'admitted':
        return _result('unknown', 'still_running', base)
    if row['effect_class'] == 'read':
        return _result('unknown', 'read_class', base)
    try:
        item = extensions.checked_plugin(payload['plugin_id'], payload['digest'])
    except Exception:
        return _result('unknown', 'connector_changed', base)
    spec = (item['config'].get('effects') or {}).get(payload['tool']) or {}
    reviewed = spec.get('readback')
    if not reviewed:
        if row['state'] == 'responded' and row['facts'].get('is_error') is False:
            # The connector's own success response was journaled before the receipt was lost.
            return _result('occurred', 'response_recorded', base, connector_claim=True)
        return _result('unknown', 'no_readback_tool', base)
    from .permissions import DEFAULT
    policy = app.store.setting('permissions', DEFAULT)['tools']
    if not policy.get('plugins') or item['config'].get('transport') == 'stdio' and not (policy.get('terminal') and policy.get('files')):
        return _result('unknown', 'plugins_disabled', base)
    reader = next((s for s in item['config'].get('schemas', []) if s['name'] == reviewed['tool']), None)
    if reader is None:
        return _result('unknown', 'connector_changed', base)
    now = time.time()
    arguments = {}
    for target, fact in reviewed['arguments'].items():
        value = _fact_value(fact, row, _schema_at(reader.get('inputSchema'), pointer(target)), now)
        if value is None:
            return _result('unknown', 'no_match_facts', base)
        _assign(arguments, pointer(target), value)
    try:
        result = extensions.readback_call(payload['plugin_id'], reviewed['tool'], arguments, payload['digest'])
    except Exception as exc:
        return _result('unknown', 'connector_unavailable', base, reason='The read-class readback failed: ' + type(exc).__name__)
    base = {**base, 'readback_tool': reviewed['tool'], 'authority': 'connector_read'}
    if result.get('is_error'):
        return _result('unknown', 'readback_error', base)
    structured = result.get('untrusted_structured')
    if not result.get('has_structured') or not isinstance(structured, dict):
        return _result('unknown', 'readback_unstructured', base)
    items = [structured] if reviewed['match']['items'] == '' else _one(structured, pointer(reviewed['match']['items']))
    if not isinstance(items, list) or len(items) > MAX_ITEMS or any(not isinstance(i, dict) for i in items):
        return _result('unknown', 'readback_malformed', base)
    verdict, basis, facts = _compare(row['effect_class'], reviewed['match'], row, items, payload['arguments'], now)
    return _result(verdict, basis, base, **facts)
