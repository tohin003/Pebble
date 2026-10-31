"""Phase 45, cu-1.0 run 1 (2026-10-27, `.runtime/phase46-dev/cu/run-1/REPORT.md`): the Mac-app dev cases' findings.

- F258: after Pebble's key presses a modifier stayed held (Fn after ⌘↑ and ⇧End, the left ⌘ after Delete and ⌘S), so
  the person's next keys were dropped or acted as shortcuts. Measured on a real TextEdit document
  (`native/modifiers-1.9.17.jsonl`: every chord's flags stayed in the session's and the HID system's state) and fixed
  in the screen helper (`native/modifiers-f258-pinned-3.jsonl`: clean after all 12 chords, ⌘A still selects all).
  These source checks hold the helper to what was measured (D81).
- F259: "Use Calculator to multiply ..." offered no OpenApp (only open/launch/start did, though OpenApp's gate accepts
  a request naming the app), and the model gave up saying OpenApp was missing after a Shortcuts verb timed out.
- F260: Pebble's own check (D141) held using Preview's window and a click in it although the request was "In
  Preview, delete the blank page, ...": steps of a named task in its app are a plain part of it.
- F261: no triple-click (count 1 or 2 only) and no Mac text keys in the description; the model pressed ⇧End.
"""
import hashlib
import json
import re
from pathlib import Path

import pytest

from pebble import policy, runner, screen_session as ss, step_judge
from pebble.screen_session import ScreenRefused
from test_tool_offering_24 import LARGE, first, names, system_of

ROOT = Path(__file__).resolve().parents[2]
SWIFT = ROOT / 'src-tauri' / 'helpers' / 'screen' / 'main.swift'


# -- F258 ---------------------------------------------------------------------------------------------------------------

def _function(source, name):
    start = source.index('func ' + name)
    end = source.index('\n}\n', start)
    return source[start:end]


def test_a_key_act_puts_the_modifier_state_back_after_its_key_up():
    source = SWIFT.read_text()
    case = source[source.index('    case "key":'):source.index('    case "type":')]
    before = case.index('let before = CGEventSource.flagsState(.hidSystemState)')
    up = case.index('try post(up)')
    restore = case.index('restoreModifiers(before, used: used)')
    assert before < case.index('try post(down)') < up < restore
    assert 'FN_CARRIERS.contains(code) ? flags.union(.maskSecondaryFn) : flags' in case


def test_the_release_names_each_modifier_and_never_reads_the_state_back():
    source = SWIFT.read_text()
    restore = _function(source, 'restoreModifiers')
    assert 'flagsState' not in restore  # read right after posting, the state is stale (measured: half released nothing)
    assert 'event?.type = .flagsChanged' in restore and 'CGKeyCode(key)' in restore
    carriers = source[source.index('let FN_CARRIERS'):source.index(']', source.index('let FN_CARRIERS'))]
    for key in ('kVK_LeftArrow', 'kVK_RightArrow', 'kVK_UpArrow', 'kVK_DownArrow', 'kVK_Home', 'kVK_End',
                'kVK_PageUp', 'kVK_PageDown', 'kVK_ForwardDelete'):
        assert key in carriers
    order = source[source.index('let RELEASE_ORDER'):source.index('\n\n', source.index('let RELEASE_ORDER'))]
    for flag, key in (('maskSecondaryFn', 'kVK_Function'), ('maskControl', 'kVK_Control'),
                      ('maskAlternate', 'kVK_Option'), ('maskShift', 'kVK_Shift'), ('maskCommand', 'kVK_Command')):
        assert re.search(r'\(\.' + flag + r', ' + key + r'\)', order), flag
    assert 'restoreModifiers(before, used: used)' in _function(source, 'releaseAll')


def test_the_pinned_helper_is_built_from_this_source():
    manifest = json.loads((ROOT / 'src-tauri' / 'resources' / 'helpers' / 'pebble-screen-helper.json').read_text())
    assert manifest['source']['sha256'] == hashlib.sha256(SWIFT.read_bytes()).hexdigest()
    binary = ROOT / 'src-tauri' / 'resources' / 'helpers' / 'pebble-screen-helper'
    assert manifest['sha256'] == hashlib.sha256(binary.read_bytes()).hexdigest()


# -- F259 ---------------------------------------------------------------------------------------------------------------

def test_a_request_names_an_installed_app_by_its_exact_name():
    apps = ('Calculator', 'TextEdit', 'Preview', 'Notes')
    assert policy.names_installed_app('Use Calculator to multiply 1,248 by 0.85.', apps) == 'Calculator'
    assert policy.names_installed_app('In TextEdit, open welcome.rtf and make the title bold.', apps) == 'TextEdit'
    assert policy.names_installed_app('take notes about the meeting', apps) is None
    assert policy.names_installed_app('Fix the calculator module in calc.py', apps) is None
    assert policy.names_installed_app('the PreviewPane widget', apps) is None


def test_pebbles_own_name_is_never_an_app_to_open():
    # validate_phase16's c4 ("Reply to the newest message in my Pebble test inbox ...") must get no app tool
    assert 'Pebble' not in policy.installed_app_names()
    assert policy.names_installed_app('Reply to the newest message in my Pebble test inbox.') is None


def test_a_request_naming_an_app_is_offered_openapp_above_8k(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'names_installed_app', lambda text: 'Calculator' if 'Calculator' in (text or '') else None)
    messages, tools = first(tmp_path / 'named', budget=LARGE, text='Use Calculator to multiply 1,248 by 0.85.')
    assert 'OpenApp' in names(tools)
    assert 'OpenApp only opens an app on an explicit request' in system_of(messages)
    _, plain = first(tmp_path / 'plain', budget=LARGE, text='Multiply 1,248 by 0.85 for me.')
    assert 'OpenApp' not in names(plain)


def test_the_guidance_sends_other_apps_to_their_window():
    sentences = dict((tuple(sorted(k)), v) for k, v in runner.TOOL_SENTENCES)
    assert 'never for another app' in sentences[('AppAction',)]
    screen = [v for k, v in runner.TOOL_SENTENCES if k == frozenset({'ScreenRequest'})]
    assert any('already open needs no OpenApp' in v for v in screen)


# -- F260 ---------------------------------------------------------------------------------------------------------------

def test_the_check_counts_a_named_tasks_steps_in_its_app_as_asked():
    assert ('When they ask for a task in an app or on a site, using its window and the ordinary clicks, drags, keys '
            'and presses that task needs there are a plain part of it.') in ' '.join(step_judge.SYSTEM.split())
    assert 'Spending money is never yours to approve.' in step_judge.SYSTEM


# -- F261 ---------------------------------------------------------------------------------------------------------------

def test_a_triple_click_selects_a_line_and_four_clicks_are_refused():
    spec = ss.pixel_spec('click_at', x=5, y=6, count=3)
    assert spec['count'] == 3 and ss.pixel_phrase(spec) == 'triple-click'
    with pytest.raises(ScreenRefused):
        ss.pixel_spec('click_at', x=5, y=6, count=4)
    with pytest.raises(ScreenRefused):
        ss.pixel_spec('click_at', x=5, y=6, button='right', count=3)
    act = ss.SCHEMAS[1]['function']
    assert act['parameters']['properties']['count']['maximum'] == 3
    assert 'count 3: line' in act['description'] and 'cmd+arrow: line/text end' in act['description']
    assert 'guard (1...3).contains(count)' in SWIFT.read_text()


# -- F262 ---------------------------------------------------------------------------------------------------------------
# measured: native/drag-f262-pinned.jsonl (a drag around a blue square in Preview drew the selection, cmd+k cropped
# the picture to it)

def test_a_drag_names_both_ends_and_is_always_reviewed():
    spec = ss.pixel_spec('drag_at', x=10, y=12, x2=60, y2=40)
    assert spec == {'action': 'drag_at', 'x': 10, 'y': 12, 'x2': 60, 'y2': 40} and ss.pixel_phrase(spec) == 'drag from'
    image = {'role': 'AXImage', 'path': 'p', 'frame': [0, 0, 1, 1], 'secure': False, 'label': 'board'}
    assert ss.classify_pixel(spec, {'element': image, 'ancestors': []})[:3] == ('press', 'commit', ['drag'])
    assert ss.full_access_class('commit', ['drag'], True) == ('commit', ['drag'])  # D141's check, never D137's pass
    prepared = {'point': (100.0, 120.0), 'end': (150.0, 140.0), 'number': 7}
    assert ss._helper_input(spec, prepared) == ('drag', {'point': [100.0, 120.0], 'to': [150.0, 140.0],
                                                         'window_number': 7})


@pytest.mark.parametrize('args, words', [
    ({'x': 10, 'y': 10, 'x2': 11, 'y2': 12}, 'at least 3 pixels'),
    ({'x': 10, 'y': 10}, 'where the drag ends'),
    ({'x': 10, 'y': 10, 'x2': -1, 'y2': 40}, 'where the drag ends'),
    ({'x': 10, 'y': 10, 'x2': 60, 'y2': 40, 'count': 2}, 'not count'),
])
def test_a_malformed_drag_is_refused_before_anything_happens(args, words):
    with pytest.raises(ScreenRefused) as refused:
        ss.pixel_spec('drag_at', **args)
    assert words in refused.value.detail


def test_the_helper_drags_only_inside_the_named_window_and_releases_on_every_path():
    source = SWIFT.read_text()
    holds = _function(source, 'targetHolds')
    assert 'kind == "drag", topWindow(at: try point(args, "to")) != number' in holds
    case = source[source.index('    case "drag":'):source.index('    case "scroll":')]
    assert case.index('heldButtons.insert(CGMouseButton.left.rawValue)') < case.index('try post(down)')
    assert case.count('try stillClear(args, since: began)') == 2  # before the press and between moves
    assert case.index('try post(up)') < case.index('heldButtons.remove(CGMouseButton.left.rawValue)')
    act = ss.SCHEMAS[1]['function']
    assert {'x2', 'y2'} <= set(act['parameters']['properties']) and 'drag_at' in act['parameters']['properties'][
        'action']['enum'] and 'drag_at (x, y to x2, y2)' in act['description']


# -- F266 ---------------------------------------------------------------------------------------------------------------
# measured: native/triple-click-f266-pinned.jsonl (cmd+| centred TextEdit's text: a visible change)

@pytest.mark.parametrize('chord, key, modifiers, label', [
    ('cmd+|', 'backslash', ['shift', 'command'], '⇧⌘\\'),
    ('cmd+shift+\\', 'backslash', ['shift', 'command'], '⇧⌘\\'),
    ('cmd+,', 'comma', ['command'], '⌘,'),
    ('cmd+=', 'equal', ['command'], '⌘='),
    ('cmd+plus', 'equal', ['shift', 'command'], '⇧⌘='),
    ('cmd+[', 'left_bracket', ['command'], '⌘['),
    ('option+cmd+`', 'grave', ['option', 'command'], '⌥⌘`'),
])
def test_mac_shortcuts_with_punctuation_are_keys(chord, key, modifiers, label):
    from pebble import screen_pixels
    parsed = screen_pixels.parse_chord(chord)
    assert (parsed['key'], parsed['modifiers'], parsed['label'], parsed['navigation']) == (key, modifiers, label, False)


def test_every_host_key_is_in_the_helpers_table():
    from pebble import screen_pixels
    table = SWIFT.read_text()
    table = table[table.index('let KEYS: [String: Int] = ['):table.index(']', table.index('"grave"'))]
    assert {m for m in re.findall(r'"([a-z0-9_]+)": kVK_', table)} == set(screen_pixels.KEYS)
