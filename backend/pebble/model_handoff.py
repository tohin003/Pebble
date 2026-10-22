"""Reviewed model handoff between route classes; nothing leaves without native approval.

API::

    route_class(profile) -> 'device' | 'cloud:<origin>'
    destination(profile) -> {'class', 'fingerprint'}
    plan(history, source_class, destination, *, release=()) -> HandoffPlan
    preview(plan, *, actor) -> dict
    approve(preview_record, *, actor) -> dict
    packet(plan) -> dict
    bind(packet, destination_fingerprint) -> BoundPacket
    check_at_start(bound, destination_fingerprint, approval) -> dict

A route is ``device`` when it runs a local model file or a loopback endpoint;
every other endpoint is ``cloud:<scheme>://<host>[:<port>]``. ``history`` is
host chat records ``{'id', 'role', 'content', 'route_class'}``; a record
without ``route_class`` originated under ``source_class``, and a host that
cannot attribute a record labels it ``unknown`` (never a destination). Records from any
class other than the destination's are withheld unless the person releases
that class in the native review (``release``), and the preview lists every
withheld ID.

``preview`` and ``approve`` accept only ``actor=NATIVE``: the trusted main
window. The preview is content-free (counts, IDs, classes and fingerprints).
``bind`` pins the planned packet to the destination fingerprint, and
``check_at_start`` refuses when the approval is missing or for another plan,
the packet changed after binding, or the destination's fingerprint changed.
Only then does it return the payload for the new route. The packet grants
nothing: the new route revalidates configuration and effects as any resume.
"""

import copy
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = [
    'DEVICE', 'NATIVE', 'UNKNOWN', 'BoundPacket', 'HandoffPlan', 'ModelHandoffError', 'approve', 'bind',
    'check_at_start', 'destination', 'packet', 'plan', 'preview', 'route_class',
]

DEVICE = 'device'
UNKNOWN = 'unknown'  # History whose route was not recorded: never a destination, withheld unless released.
NATIVE = 'native_main_window'
VERSION = 1
CLASS = re.compile(r'device|unknown|cloud:https?://[a-z0-9.-]{1,253}(?::\d{1,5})?|cloud:\[[0-9a-f:]{2,39}\](?::\d{1,5})?')
FINGERPRINT = re.compile(r'sha256:[0-9a-f]{64}')
ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}')
ROLES = ('user', 'assistant', 'host')
MAX_ITEMS = 8192
MAX_BYTES = 32_000_000  # As the durable checkpoint bound.


class ModelHandoffError(ValueError):
    """The handoff was refused; nothing was sent. ``reason`` is a label."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f'The reviewed model handoff was refused ({reason}). Nothing was sent.')


@dataclass(frozen=True)
class HandoffPlan:
    source_class: str
    destination_class: str
    destination_fingerprint: str
    items: tuple
    withheld: tuple
    released: tuple
    fingerprint: str


@dataclass(frozen=True)
class BoundPacket:
    packet: dict
    packet_fingerprint: str
    destination_fingerprint: str
    binding: str


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def _digest(label, value):
    return 'sha256:' + hashlib.sha256((label + '\n' + _canonical(value)).encode('ascii')).hexdigest()


def _class(value):
    if type(value) is not str or not CLASS.fullmatch(value):
        raise ModelHandoffError('bad_class')
    return value


def _loopback(host):
    # Only names that never reach a DNS answer: 'localhost' (as providers.endpoint) and loopback literals.
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def route_class(profile):
    """``device`` for a local model or loopback endpoint, else ``cloud:<origin>``."""
    if not isinstance(profile, dict) or type(profile.get('kind')) is not str:
        raise ModelHandoffError('bad_route')
    if profile['kind'] == 'gguf':
        return DEVICE
    try:
        parts = urlsplit(profile.get('url') or '')
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ModelHandoffError('bad_route') from None
    if parts.scheme not in ('http', 'https') or not host:
        raise ModelHandoffError('bad_route')
    if _loopback(host):
        return DEVICE
    shown = f'[{host}]' if ':' in host else host
    return _class(f'cloud:{parts.scheme}://{shown}' + (f':{port}' if port else ''))


def destination(profile):
    """The destination class plus a fingerprint over the whole route profile.

    Any change of endpoint, model, account, context or effort changes it."""
    if not isinstance(profile, dict):
        raise ModelHandoffError('bad_route')
    return {'class': route_class(profile), 'fingerprint': _digest('model_handoff.destination', profile)}


def _history(history, source_class):
    if not isinstance(history, (list, tuple)) or len(history) > MAX_ITEMS:
        raise ModelHandoffError('bad_history')
    records, seen, size = [], set(), 0
    for item in history:
        if not isinstance(item, dict) or set(item) - {'id', 'role', 'content', 'route_class', 'created'}:
            raise ModelHandoffError('bad_history')
        ident, role, content = item.get('id'), item.get('role'), item.get('content')
        if type(ident) is not str or not ID.fullmatch(ident) or ident in seen or role not in ROLES or type(content) is not str:
            raise ModelHandoffError('bad_history')
        seen.add(ident)
        size += len(content.encode('utf-8', 'surrogatepass'))
        if size > MAX_BYTES:
            raise ModelHandoffError('too_large')
        origin = item.get('route_class')
        records.append({'id': ident, 'role': role, 'content': content,
                        'route_class': source_class if origin is None else _class(origin)})
    return records


def plan(history, source_class, destination, *, release=()):
    """Select the history the destination may receive; withhold every other class."""
    source = _class(source_class)
    if not isinstance(destination, dict) or set(destination) != {'class', 'fingerprint'}:
        raise ModelHandoffError('bad_destination')
    target = _class(destination['class'])
    if target == UNKNOWN:
        raise ModelHandoffError('bad_destination')
    if type(destination['fingerprint']) is not str or not FINGERPRINT.fullmatch(destination['fingerprint']):
        raise ModelHandoffError('bad_destination')
    if isinstance(release, (str, bytes)) or not isinstance(release, (list, tuple, set, frozenset)):
        raise ModelHandoffError('bad_release')
    released = tuple(sorted({_class(c) for c in release} - {target}))
    records = _history(history, source)
    allowed = {target, *released}
    items = tuple(r for r in records if r['route_class'] in allowed)
    withheld = tuple({'id': r['id'], 'role': r['role'], 'route_class': r['route_class']}
                     for r in records if r['route_class'] not in allowed)
    value = _value(source, target, destination['fingerprint'], items, withheld, released)
    return HandoffPlan(source, target, destination['fingerprint'], copy.deepcopy(items), withheld, released,
                       _digest('model_handoff.plan', value))


def _value(source, target, fingerprint, items, withheld, released):
    """Exactly what the plan fingerprint covers and the packet carries."""
    return {'version': VERSION, 'source_class': source, 'destination_class': target,
            'destination_fingerprint': fingerprint, 'items': [dict(r) for r in items],
            'withheld': [dict(r) for r in withheld], 'released': list(released)}


def _native(actor):
    if actor != NATIVE:
        raise PermissionError('Only the trusted main window can review a model handoff.')


def _counts(records, key):
    counts = {}
    for r in records:
        counts[r[key]] = counts.get(r[key], 0) + 1
    return dict(sorted(counts.items()))


def preview(plan, *, actor):
    """Content-free review record: counts, IDs, classes and fingerprints only."""
    _native(actor)
    if not isinstance(plan, HandoffPlan):
        raise ModelHandoffError('bad_plan')
    return {
        'kind': 'model_handoff_preview', 'version': VERSION, 'authorization': 'none',
        'status': 'awaiting_native_approval',
        'source_class': plan.source_class, 'destination_class': plan.destination_class,
        'destination_fingerprint': plan.destination_fingerprint, 'plan_fingerprint': plan.fingerprint,
        'crosses_class': plan.source_class != plan.destination_class,
        'send': {'count': len(plan.items), 'ids': [r['id'] for r in plan.items],
                 'by_role': _counts(plan.items, 'role'), 'by_class': _counts(plan.items, 'route_class'),
                 'bytes': sum(len(r['content'].encode('utf-8', 'surrogatepass')) for r in plan.items)},
        'withheld': {'count': len(plan.withheld), 'ids': [r['id'] for r in plan.withheld],
                     'by_class': _counts(plan.withheld, 'route_class')},
        'released_classes': list(plan.released),
        'statement': 'Nothing is sent until you approve this exact preview. The new route revalidates '
                     'configuration and effects; this handoff grants no permission.',
    }


def approve(preview_record, *, actor):
    """Record the person's approval of one exact preview."""
    _native(actor)
    if (not isinstance(preview_record, dict) or preview_record.get('kind') != 'model_handoff_preview'
            or not FINGERPRINT.fullmatch(str(preview_record.get('plan_fingerprint')))
            or not FINGERPRINT.fullmatch(str(preview_record.get('destination_fingerprint')))):
        raise ModelHandoffError('bad_preview')
    return {'kind': 'model_handoff_approval', 'version': VERSION, 'actor': NATIVE,
            'plan_fingerprint': preview_record['plan_fingerprint'],
            'destination_fingerprint': preview_record['destination_fingerprint'],
            'destination_class': _class(preview_record['destination_class'])}


def packet(plan):
    """The packet for a plan: the exact value its fingerprint covers."""
    if not isinstance(plan, HandoffPlan):
        raise ModelHandoffError('bad_plan')
    return {'kind': 'model_handoff_packet', 'version': VERSION, 'authorization': 'none',
            'plan_fingerprint': plan.fingerprint,
            'plan': _value(plan.source_class, plan.destination_class, plan.destination_fingerprint,
                           plan.items, plan.withheld, plan.released)}


def bind(packet_or_plan, destination_fingerprint):
    """Pin a planned packet (or its plan) to one destination fingerprint."""
    value = packet(packet_or_plan) if isinstance(packet_or_plan, HandoffPlan) else packet_or_plan
    if (not isinstance(value, dict) or value.get('kind') != 'model_handoff_packet'
            or not isinstance(value.get('plan'), dict)):
        raise ModelHandoffError('bad_packet')
    if type(destination_fingerprint) is not str or not FINGERPRINT.fullmatch(destination_fingerprint):
        raise ModelHandoffError('bad_destination')
    frozen = json.loads(_canonical(value))
    digest = _digest('model_handoff.packet', frozen)
    return BoundPacket(frozen, digest, destination_fingerprint,
                       _digest('model_handoff.binding', [digest, destination_fingerprint]))


def check_at_start(bound, destination_fingerprint, approval):
    """Refuse unless an approved, unchanged packet meets an unchanged destination."""
    if not isinstance(bound, BoundPacket):
        raise ModelHandoffError('bad_packet')
    if not isinstance(approval, dict) or approval.get('kind') != 'model_handoff_approval' or approval.get('actor') != NATIVE:
        raise ModelHandoffError('not_approved')
    digest = _digest('model_handoff.packet', bound.packet)
    if digest != bound.packet_fingerprint or bound.binding != _digest(
            'model_handoff.binding', [digest, bound.destination_fingerprint]):
        raise ModelHandoffError('packet_changed')
    value = bound.packet['plan']
    # The items sent are exactly those whose plan fingerprint the person approved.
    if (_digest('model_handoff.plan', value) != approval.get('plan_fingerprint')
            or bound.packet.get('plan_fingerprint') != approval.get('plan_fingerprint')
            or value.get('destination_fingerprint') != bound.destination_fingerprint
            or approval.get('destination_fingerprint') != bound.destination_fingerprint
            or approval.get('destination_class') != value.get('destination_class')):
        raise ModelHandoffError('approval_mismatch')
    if destination_fingerprint != bound.destination_fingerprint:
        raise ModelHandoffError('destination_changed')
    allowed = {value['destination_class'], *value['released']}
    if any(item['route_class'] not in allowed for item in value['items']):
        raise ModelHandoffError('class_mismatch')
    return {'status': 'cleared', 'authorization': 'none', 'destination_class': value['destination_class'],
            'items': [{k: item[k] for k in ('id', 'role', 'content')} for item in value['items']],
            'binding': bound.binding, 'withheld': len(value['withheld'])}
