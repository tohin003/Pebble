"""Phase 19 B2: app-interface prototypes are inert declarative data (p19_prototype_rejects).

Every key and string is checked after NFKC normalisation and casefolding, so full-width,
ligature, soft-hyphen and zero-width spellings of a link, style, markup or handler are
refused like their plain forms. Controls only name one of the prototype's own screens.
"""
import copy
import json

import pytest

from pebble.artifact_schema import (ASPECTS, COMPONENTS, NATIVE_KINDS, REJECTED_KEYS, THEME, VIEWPORTS, inert_text,
                                    validate_native, validate_native_title, validate_prototype)
from pebble.artifacts import validate


def demo():
    return {'viewport': 'phone', 'start': 'home', 'theme': {'accent': 'teal', 'density': 'cozy', 'radius': 8},
            'screens': [
                {'id': 'home', 'title': 'Home', 'components': [
                    {'type': 'heading', 'text': 'Welcome back'},
                    {'type': 'button', 'label': 'Approve payment', 'goto': 'done'},
                    {'type': 'input', 'label': 'Email', 'placeholder': 'name at example'},
                    {'type': 'toggle', 'label': 'Remember me'},
                    {'type': 'list', 'items': ['Notifications', 'Privacy']},
                    {'type': 'card', 'title': 'Today', 'body': 'Three tasks are due.'},
                    {'type': 'image_placeholder', 'label': 'Hero', 'aspect': '16:9'},
                    {'type': 'nav', 'items': [{'label': 'Home', 'goto': 'home'}, {'label': 'Done', 'goto': 'done'}]}]},
                {'id': 'done', 'title': 'Done', 'components': [{'type': 'button', 'label': 'Back', 'goto': 'home'}]}]}


# Slot name -> setter for one display string of the demo.
SLOTS = {
    'heading': lambda v, t: v['screens'][0]['components'][0].update(text=t),
    'button': lambda v, t: v['screens'][0]['components'][1].update(label=t),
    'placeholder': lambda v, t: v['screens'][0]['components'][2].update(placeholder=t),
    'list item': lambda v, t: v['screens'][0]['components'][4]['items'].__setitem__(0, t),
    'card body': lambda v, t: v['screens'][0]['components'][5].update(body=t),
    'image label': lambda v, t: v['screens'][0]['components'][6].update(label=t),
    'nav label': lambda v, t: v['screens'][0]['components'][7]['items'][0].update(label=t),
    'screen title': lambda v, t: v['screens'][1].update(title=t),
}
# Key placement -> the object that receives an extra key.
PLACES = {
    'root': lambda v: v, 'theme': lambda v: v['theme'], 'screen': lambda v: v['screens'][0],
    'component': lambda v: v['screens'][0]['components'][1], 'nav item': lambda v: v['screens'][0]['components'][7]['items'][0],
}
NFKC_KEYS = ('ｈｒｅｆ', 'ＳＴＹＬＥ', 'Src', 'ſrc', 'hr' + chr(0x200B) + 'ef', 'on' + chr(0xAD) + 'click', 'h r e f', 'ＯＮclick', 'onLoad',
             'on_submit', 'onpointerdown', 'Action', 'innerHTML', 'xlink:href', 'srcdoc', 'formaction')
HOSTILE_TEXT = (
    'javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'java\tscript:alert(1)', 'java' + chr(0x200B) + 'script:x', 'ｊａｖａｓｃｒｉｐｔ:x',
    'vbscript:msgbox(1)', 'data:text/html;base64,PHNjcmlwdD4=', 'data:,hello', 'https://evil.example/approve',
    'HTTP://evil.example', '//evil.example/x', 'file:///etc/passwd', 'tauri://localhost/x', 'mailto:a@example.invalid',
    'tel:+15550100', '<script>alert(1)</script> Welcome', '＜script＞x', '<img src=x onerror=host()>', '</div>',
    '<!-- note -->', '&#106;avascript:x', 'x&colon;y', 'url(https://x)', 'expression(alert(1))', '@import "x"',
    '{{constructor.constructor("host()")()}}', '${host()}', 'onclick=host()', 'window.__TAURI__.invoke("approve")',
    'parent.postMessage(1)', '-moz-binding:x', 'behavior: url(x)')
PLAIN_TEXT = ('JavaScript: the basics', 'Tel: 555 0100', 'Email: name at example', 'Data: 3 items', 'About: our team',
              'Open a new window.', 'Price < 5 dollars', 'Q&A and more', 'Ask once; act once', 'Café · 東京 · مرحبا')


def refused(value):
    with pytest.raises(ValueError):
        validate('prototype', json.dumps(value))


def test_p19_prototype_round_trips_every_component_verbatim_and_copies_input():
    value = demo()
    assert 'prototype' in NATIVE_KINDS and set(COMPONENTS) == {c['type'] for c in value['screens'][0]['components']}
    stored = validate('prototype', json.dumps(value))
    assert stored == value and validate_prototype(value) == value
    stored['screens'][0]['title'] = 'Changed'
    assert value == demo()  # the validator works on a strict JSON copy
    minimal = {'start': 'a', 'screens': [{'id': 'a', 'title': 'A', 'components': []}]}
    assert validate_native('prototype', minimal) == minimal  # viewport and theme are optional, never defaulted in


def test_p19_prototype_a_generated_approve_button_is_only_navigation_data():
    stored = validate('prototype', json.dumps(demo()))
    assert stored['screens'][0]['components'][1] == {'type': 'button', 'label': 'Approve payment', 'goto': 'done'}
    for extra in ({'action': 'approve'}, {'rpc': 'approve'}, {'invoke': 'approve'}, {'permission': 'granted'},
                  {'origin': 'human'}, {'approve': True}):
        value = demo()
        value['screens'][0]['components'][1].update(extra)
        refused(value)


@pytest.mark.parametrize('place', sorted(PLACES))
@pytest.mark.parametrize('key', sorted(REJECTED_KEYS) + list(NFKC_KEYS))
def test_p19_prototype_rejects_link_style_markup_handler_and_authority_keys_nfkc_first(place, key):
    value = demo()
    PLACES[place](value)[key] = 'Plain words'
    refused(value)


@pytest.mark.parametrize('slot', sorted(SLOTS))
@pytest.mark.parametrize('text', HOSTILE_TEXT)
def test_p19_prototype_rejects_links_markup_scripts_and_templates_in_every_string(slot, text):
    value = demo()
    SLOTS[slot](value, text)
    refused(value)


@pytest.mark.parametrize('text', PLAIN_TEXT)
def test_p19_prototype_keeps_ordinary_text_verbatim(text):
    for slot in SLOTS:
        value = demo()
        SLOTS[slot](value, text)
        assert validate('prototype', json.dumps(value)) == value, (slot, text)


def test_p19_prototype_controls_reach_only_their_own_screens():
    for target in ('missing', 'https://evil.example/', '../done', 'DONE', 'done ', '', None, 1, ['done']):
        value = demo()
        value['screens'][0]['components'][1]['goto'] = target
        refused(value)
        value = demo()
        value['screens'][0]['components'][7]['items'][1]['goto'] = target
        refused(value)
    for start in ('missing', None, 'Home', ''):
        refused({**demo(), 'start': start})


@pytest.mark.parametrize('change', [
    lambda v: v['screens'].append(copy.deepcopy(v['screens'][1])),  # duplicate screen ID
    lambda v: v['screens'][0].update(id='../home'), lambda v: v['screens'][0].update(id='__proto__'),
    lambda v: v['screens'][0].update(extra=1), lambda v: v['screens'][0].pop('title'),
    lambda v: v['screens'][0].update(components={}), lambda v: v.update(screens=[]),
    lambda v: v.update(screens=[{'id': f's{i}', 'title': 'S', 'components': []} for i in range(13)], start='s0'),
    lambda v: v['screens'][0].update(components=[{'type': 'heading', 'text': 'x'}] * 31),
    lambda v: v['screens'][0]['components'].append({'type': 'iframe', 'label': 'x'}),
    lambda v: v['screens'][0]['components'].append({'type': 'link', 'label': 'x', 'goto': 'home'}),
    lambda v: v['screens'][0]['components'].append({'type': 'form'}),
    lambda v: v['screens'][0]['components'].append({'type': 'card', 'title': 'x'}),
    lambda v: v['screens'][0]['components'].append({'type': 'toggle', 'label': 'x', 'checked': True}),
    lambda v: v['screens'][0]['components'].append({'type': 'list', 'items': ['x'] * 31}),
    lambda v: v['screens'][0]['components'].append({'type': 'list', 'items': [1]}),
    lambda v: v['screens'][0]['components'].append({'type': 'nav', 'items': []}),
    lambda v: v['screens'][0]['components'].append({'type': 'nav', 'items': [{'label': 'x', 'goto': 'home'}] * 9}),
    lambda v: v['screens'][0]['components'].append({'type': 'image_placeholder', 'label': 'x', 'aspect': '5:4'}),
    lambda v: v['screens'][0]['components'].append({'type': 'heading', 'text': ''}),
    lambda v: v['screens'][0]['components'].append({'type': 'heading', 'text': 'x' * 241}),
    lambda v: v['screens'][0]['components'].append({'type': 'card', 'title': 'x', 'body': 'x' * 2001}),
    lambda v: v['screens'][0]['components'].append({'type': 'heading', 'text': 'sk-' + 'a' * 30}),
    lambda v: v.update(viewport='tv'), lambda v: v.update(theme={'accent': '#ff0000'}),
    lambda v: v.update(theme={'density': 'url(x)'}), lambda v: v.update(theme={'radius': 7}),
    lambda v: v.update(theme={'radius': True}), lambda v: v.update(theme={'radius': 8.0}),
    lambda v: v.update(theme={'font': 'serif'}), lambda v: v.update(theme='teal'), lambda v: v.update(extra=1),
])
def test_p19_prototype_rejects_shapes_limits_and_unenumerated_tokens(change):
    value = demo()
    change(value)
    refused(value)


def test_p19_prototype_enumerations_are_the_renderer_tokens():
    assert VIEWPORTS == ('phone', 'tablet', 'desktop')
    assert set(THEME) == {'accent', 'density', 'radius'} and THEME['radius'] == (0, 4, 8, 12, 16)
    assert ASPECTS == ('1:1', '4:3', '3:2', '16:9', '9:16', '3:4', '21:9')
    for accent in THEME['accent']:
        assert validate_prototype({**demo(), 'theme': {'accent': accent}})['theme'] == {'accent': accent}


def test_p19_prototype_size_is_bounded_before_any_shape_check():
    value = demo()
    value['screens'] = [{'id': f's{i}', 'title': 'S', 'components': [{'type': 'card', 'title': 'T', 'body': 'b' * 2000}] * 30}
                        for i in range(12)]
    value['start'] = 's0'
    with pytest.raises(ValueError, match='96 KB'):
        validate('prototype', json.dumps(value))


def test_p19_new_kind_titles_are_inert_text_too():
    assert validate_native_title('prototype', 'Checkout flow') == 'Checkout flow'
    for title in ('https://evil.example', '<b>x</b>', 'ｊａｖａｓｃｒｉｐｔ:x', '{{x}}'):
        for kind in ('prototype', 'dashboard'):
            with pytest.raises(ValueError):
                validate_native_title(kind, title)
    assert validate_native_title('table', 'https://example.invalid/report') == 'https://example.invalid/report'
    assert inert_text('  Spaced  ', 40) == 'Spaced'
