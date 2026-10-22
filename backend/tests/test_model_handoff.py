"""Reviewed model handoff: class withholding, content-free native preview, bound destination."""

import copy
import json
import re

import pytest

from pebble import model_handoff
from pebble.model_handoff import (
    DEVICE, NATIVE, ModelHandoffError, approve, bind, check_at_start, destination, plan, preview, route_class,
)

LOCAL = {'id': 'local', 'kind': 'gguf', 'path': '/models/small.gguf', 'url': None, 'context': 4096}
LOOPBACK = {'id': 'lm', 'kind': 'openai', 'url': 'http://127.0.0.1:1234/v1', 'model': 'm', 'context': 8192}
CLOUD = {'id': 'oa', 'kind': 'openai', 'url': 'https://api.example.com/v1', 'model': 'gpt-x', 'context': 128000,
         'budget': 64000, 'effort': 'medium', 'connection_id': 'acct-1'}
OTHER_CLOUD = {'id': 'an', 'kind': 'anthropic', 'url': 'https://llm.other.example:8443/v1', 'model': 'c', 'context': 200000}
DEVICE_SECRET = 'DEVICE-ONLY-SENTINEL tax notes'
CLOUD_TEXT = 'CLOUD-TURN-SENTINEL'


def history():
    return [
        {'id': 'm1', 'role': 'user', 'content': DEVICE_SECRET, 'route_class': DEVICE},
        {'id': 'm2', 'role': 'assistant', 'content': 'Local reply quoting ' + DEVICE_SECRET, 'route_class': DEVICE},
        {'id': 'm3', 'role': 'user', 'content': CLOUD_TEXT + ' question', 'route_class': 'cloud:https://api.example.com'},
        {'id': 'm4', 'role': 'assistant', 'content': CLOUD_TEXT + ' answer', 'route_class': 'cloud:https://api.example.com'},
        {'id': 'm5', 'role': 'user', 'content': 'Unlabelled turn under the source route'},
    ]


def reviewed(items=None, source=DEVICE, profile=CLOUD, release=()):
    target = destination(profile)
    planned = plan(history() if items is None else items, source, target, release=release)
    shown = preview(planned, actor=NATIVE)
    return planned, shown, approve(shown, actor=NATIVE), target


def test_route_classes():
    assert route_class(LOCAL) == route_class(LOOPBACK) == DEVICE
    assert route_class({**LOOPBACK, 'url': 'http://localhost:8080/v1'}) == DEVICE
    assert route_class({**LOOPBACK, 'url': 'http://[::1]:8080/v1'}) == DEVICE
    assert route_class(CLOUD) == 'cloud:https://api.example.com'
    assert route_class(OTHER_CLOUD) == 'cloud:https://llm.other.example:8443'
    # A LAN server is not this device, whatever the profile calls it.
    assert route_class({**LOOPBACK, 'url': 'http://192.168.1.20:1234/v1', 'local': True}) == 'cloud:http://192.168.1.20:1234'
    for bad in ({'kind': 'openai', 'url': 'ftp://x'}, {'kind': 'openai', 'url': ''}, {'url': 'https://x'}, 'x'):
        with pytest.raises(ModelHandoffError):
            route_class(bad)


def test_cross_class_history_is_withheld_from_a_cloud_destination():
    planned, shown, approval, target = reviewed()
    assert planned.destination_class == 'cloud:https://api.example.com'
    assert [r['id'] for r in planned.items] == ['m3', 'm4']
    assert [r['id'] for r in planned.withheld] == ['m1', 'm2', 'm5']  # m5 originated under the device source.
    cleared = check_at_start(bind(planned, target['fingerprint']), target['fingerprint'], approval)
    sent = json.dumps(cleared)
    assert DEVICE_SECRET not in sent and 'Unlabelled turn' not in sent and CLOUD_TEXT in sent
    assert [i['id'] for i in cleared['items']] == ['m3', 'm4'] and cleared['withheld'] == 3
    assert cleared['authorization'] == 'none'
    # Between clouds, the other origin's history is withheld as well.
    other, _, _, _ = reviewed(source='cloud:https://api.example.com', profile=OTHER_CLOUD)
    assert [r['id'] for r in other.items] == [] and len(other.withheld) == 5
    # Same class: nothing is withheld except records explicitly from elsewhere.
    same, _, _, _ = reviewed(source='cloud:https://api.example.com')
    assert [r['id'] for r in same.items] == ['m3', 'm4', 'm5']


def test_release_is_an_explicit_reviewed_choice_shown_in_the_preview():
    planned, shown, approval, target = reviewed(release=[DEVICE])
    assert [r['id'] for r in planned.items] == ['m1', 'm2', 'm3', 'm4', 'm5'] and planned.withheld == ()
    assert shown['released_classes'] == [DEVICE] and shown['send']['by_class'][DEVICE] == 3
    unreleased, _, _, _ = reviewed()
    assert unreleased.fingerprint != planned.fingerprint
    with pytest.raises(ModelHandoffError):
        plan(history(), DEVICE, destination(CLOUD), release=DEVICE)


def test_unattributed_history_fails_closed():
    items = history() + [{'id': 'm6', 'role': 'user', 'content': 'old turn', 'route_class': model_handoff.UNKNOWN}]
    for source, profile in ((DEVICE, CLOUD), ('cloud:https://api.example.com', CLOUD), (DEVICE, LOCAL)):
        planned, shown, _, _ = reviewed(items, source=source, profile=profile)
        assert 'm6' not in [r['id'] for r in planned.items] and 'm6' in shown['withheld']['ids']
    released, _, _, _ = reviewed(items, release=[model_handoff.UNKNOWN])
    assert 'm6' in [r['id'] for r in released.items]
    with pytest.raises(ModelHandoffError):
        plan(items, DEVICE, {'class': model_handoff.UNKNOWN, 'fingerprint': destination(CLOUD)['fingerprint']})


def test_preview_is_native_only_and_content_free():
    planned, shown, _, target = reviewed()
    text = json.dumps(shown)
    for leak in (DEVICE_SECRET, CLOUD_TEXT, 'Unlabelled', 'gpt-x', 'acct-1', '/v1'):
        assert leak not in text
    assert shown['send'] == {'count': 2, 'ids': ['m3', 'm4'], 'by_role': {'assistant': 1, 'user': 1},
                             'by_class': {'cloud:https://api.example.com': 2},
                             'bytes': len((CLOUD_TEXT + ' question').encode()) + len((CLOUD_TEXT + ' answer').encode())}
    assert shown['withheld'] == {'count': 3, 'ids': ['m1', 'm2', 'm5'], 'by_class': {DEVICE: 3}}
    assert shown['crosses_class'] is True and shown['status'] == 'awaiting_native_approval'
    assert shown['destination_fingerprint'] == target['fingerprint'] and shown['plan_fingerprint'] == planned.fingerprint
    allowed = re.compile(r'[a-z_]+|m\d|sha256:[0-9a-f]{64}|device|cloud:https://api\.example\.com')

    def scalars(value):
        if isinstance(value, dict):
            return all(scalars(v) for k, v in value.items() if k != 'statement') and all(allowed.fullmatch(k) for k in value)
        if isinstance(value, list):
            return all(scalars(v) for v in value)
        return not isinstance(value, str) or bool(allowed.fullmatch(value))
    assert scalars(shown)
    for actor in ('model', 'child', 'mod', None):
        with pytest.raises(PermissionError):
            preview(planned, actor=actor)
        with pytest.raises(PermissionError):
            approve(shown, actor=actor)


def test_nothing_is_cleared_without_native_approval_of_this_exact_plan():
    planned, shown, approval, target = reviewed()
    bound = bind(planned, target['fingerprint'])
    for missing in (None, {}, {**approval, 'actor': 'model'}, {**approval, 'kind': 'something_else'}):
        with pytest.raises(ModelHandoffError) as caught:
            check_at_start(bound, target['fingerprint'], missing)
        assert caught.value.reason == 'not_approved'
    released, _, released_approval, _ = reviewed(release=[DEVICE])
    with pytest.raises(ModelHandoffError) as caught:
        check_at_start(bound, target['fingerprint'], released_approval)
    assert caught.value.reason == 'approval_mismatch'


def test_destination_fingerprint_change_refuses_at_start():
    planned, _, approval, target = reviewed()
    bound = bind(planned, target['fingerprint'])
    for changed in ({**CLOUD, 'model': 'gpt-y'}, {**CLOUD, 'connection_id': 'acct-2'}, {**CLOUD, 'effort': 'high'},
                    {**CLOUD, 'url': 'https://api.example.com/v2'}):
        current = destination(changed)
        assert current['fingerprint'] != target['fingerprint']
        with pytest.raises(ModelHandoffError) as caught:
            check_at_start(bound, current['fingerprint'], approval)
        assert caught.value.reason == 'destination_changed' and DEVICE_SECRET not in str(caught.value)
    assert check_at_start(bound, destination(copy.deepcopy(CLOUD))['fingerprint'], approval)['status'] == 'cleared'


def test_packet_changes_after_or_before_binding_are_refused():
    planned, _, approval, target = reviewed()
    bound = bind(planned, target['fingerprint'])
    bound.packet['plan']['items'].append({'id': 'm1', 'role': 'user', 'content': DEVICE_SECRET, 'route_class': DEVICE})
    with pytest.raises(ModelHandoffError) as caught:
        check_at_start(bound, target['fingerprint'], approval)
    assert caught.value.reason == 'packet_changed'
    # A packet altered before binding no longer matches the approved plan.
    forged = model_handoff.packet(planned)
    forged['plan']['items'].append({'id': 'm1', 'role': 'user', 'content': DEVICE_SECRET, 'route_class': DEVICE})
    with pytest.raises(ModelHandoffError) as caught:
        check_at_start(bind(forged, target['fingerprint']), target['fingerprint'], approval)
    assert caught.value.reason == 'approval_mismatch'
    # Binding to another destination than the one reviewed is refused too.
    other = destination(OTHER_CLOUD)['fingerprint']
    with pytest.raises(ModelHandoffError) as caught:
        check_at_start(bind(planned, other), other, approval)
    assert caught.value.reason == 'approval_mismatch'


@pytest.mark.parametrize('items, reason', [
    ('x', 'bad_history'),
    ([{'id': 'm1', 'role': 'tool', 'content': 'x'}], 'bad_history'),
    ([{'id': 'm1', 'role': 'user', 'content': 'x'}, {'id': 'm1', 'role': 'user', 'content': 'y'}], 'bad_history'),
    ([{'id': 'm1', 'role': 'user', 'content': 'x', 'route_class': 'cloud:evil'}], 'bad_class'),
    ([{'id': 'm1', 'role': 'user', 'content': 'x', 'secret': 'y'}], 'bad_history'),
    ([{'id': 'm1', 'role': 'user', 'content': ['x']}], 'bad_history'),
])
def test_malformed_history_is_refused(items, reason):
    with pytest.raises(ModelHandoffError) as caught:
        plan(items, DEVICE, destination(CLOUD))
    assert caught.value.reason == reason


def test_destination_and_bind_inputs_are_validated():
    with pytest.raises(ModelHandoffError):
        plan(history(), DEVICE, {'class': 'cloud:https://api.example.com'})
    with pytest.raises(ModelHandoffError):
        plan(history(), 'somewhere', destination(CLOUD))
    planned, _, _, _ = reviewed()
    with pytest.raises(ModelHandoffError):
        bind(planned, 'not-a-fingerprint')
    with pytest.raises(ModelHandoffError):
        bind({'kind': 'other'}, destination(CLOUD)['fingerprint'])
