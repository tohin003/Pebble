"""Phase 45 (layer 4): screen_pixels' mechanics and screen_helper's Phase 45 changes, with fakes only.

Key chords in the helper's own table; the capture decoded and checked in memory, its file and folder removed whatever
happens; the model picture resized once to the pixel budget; the tolerant "spot changed since the screenshot" test and
the exact "nothing visibly changed" test; the lane's facts (nothing spawned while both lanes are off), its input under
the Mac-wide lease (refused when busy, released on every path, release_all after an uncertain step); the helper found
at resources/helpers in a bundle; ON_BY_DEFAULT as the one constant. No real helper, window or event is used."""
import hashlib
import io
import os
import stat

import pytest
from PIL import Image, ImageDraw

from pebble import screen_helper as sh, screen_pixels as sp
from pebble.screen_helper import HelperError, Transform


def png_bytes(image):
    out = io.BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()


def grey(size=(400, 300), shade=200):
    return Image.new('L', size, shade)


# ---- key chords ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('text, key, modifiers, label, navigation', [
    ('cmd+s', 's', ['command'], '⌘S', False),
    ('Cmd + Shift + Z', 'z', ['shift', 'command'], '⇧⌘Z', False),
    ('ctrl+alt+delete', 'delete', ['control', 'option'], '⌃⌥Delete', False),
    ('shift+tab', 'tab', ['shift'], '⇧Tab', True),
    ('tab', 'tab', [], 'Tab', True),
    ('esc', 'escape', [], 'Esc', True),
    ('page down', 'page_down', [], 'Page Down', True),
    ('PageUp', 'page_up', [], 'Page Up', True),
    ('down', 'down', [], '↓', True),
    ('shift+down', 'down', ['shift'], '⇧↓', False),   # extends a selection: not plain navigation
    ('cmd+down', 'down', ['command'], '⌘↓', False),   # Finder opens the selection with it
    ('enter', 'return', [], 'Return', False),
    ('space', 'space', [], 'Space', False),
])
def test_a_chord_names_one_key_of_the_helpers_table_in_macos_order(text, key, modifiers, label, navigation):
    assert sp.parse_chord(text) == {'key': key, 'modifiers': modifiers, 'label': label, 'navigation': navigation}


@pytest.mark.parametrize('text', ['', None, 'cmd+', '+s', 'cmd+cmd+s', 'f1', 'cmd+§', 'hyper+s', 'x' * 41, 7,
                                  'cmd+shift+option+ctrl+meta+s'])  # cmd+, is a key since F266 (1.9.18)
def test_a_chord_outside_the_table_is_refused(text):
    with pytest.raises(sp.ChordError):
        sp.parse_chord(text)


def test_the_hosts_key_table_is_the_helpers():
    source = (sp.__file__.rsplit('/backend/', 1)[0] + '/src-tauri/helpers/screen/main.swift')
    text = open(source, encoding='utf-8').read()
    table = text[text.index('let KEYS'):text.index('let MODIFIERS')]
    import re
    assert set(re.findall(r'"([a-z0-9_]+)": kVK_', table)) == sp.KEYS
    assert set(re.findall(r'"([a-z]+)": \.mask', text)) == set(sp.MODIFIER_ORDER)


# ---- images -----------------------------------------------------------------------------------------------------

def test_a_capture_is_decoded_only_when_it_is_the_png_the_helper_reported():
    data = png_bytes(Image.new('RGB', (64, 48), (10, 20, 30)))
    assert sp.decode(data, [64, 48]).size == (64, 48)
    for bad, pixels in ((data, [64, 49]), (b'GIF89a' + data, [64, 48]), (png_bytes(Image.new('RGB', (8, 8))), [64, 48])):
        with pytest.raises(ValueError):
            sp.decode(bad, pixels)


def test_a_capture_beyond_the_pixel_limit_is_refused_from_its_header(monkeypatch):
    monkeypatch.setattr(sp, 'CAPTURE_MAX_PIXELS', 100)
    with pytest.raises(ValueError):
        sp.decode(png_bytes(Image.new('RGB', (20, 20))), [20, 20])


def test_the_model_picture_is_the_crop_resized_once_and_padded():
    image = Image.new('RGB', (1600, 1200), (255, 255, 255))
    ImageDraw.Draw(image).rectangle((400, 300, 1199, 899), fill=(0, 0, 0))
    plain = Transform((1600, 1200), (0, 0, 800, 600), max_pixels=sh.MAX_PIXELS)
    assert sp.model_image(image, plain).size == plain.size == (1280, 960)
    cropped = Transform((1600, 1200), (0, 0, 800, 600), crop=(400, 300, 800, 600))
    picture = sp.model_image(image, cropped)
    assert picture.size == (800, 600) and picture.getpixel((400, 300)) == (0, 0, 0), 'the crop is the black square'
    padded = Transform((1600, 1200), (0, 0, 800, 600), box=(1600, 1600))
    big = sp.model_image(image, padded)
    assert big.size == (1600, 1600) and big.getpixel((10, 1500)) == (0, 0, 0), 'padding is black, bottom and right'


def test_image_parts_are_labelled_untrusted():
    parts = sp.image_parts(b'\x89PNG data', 'UNTRUSTED screenshot of a window.')
    assert parts[0]['type'] == 'text' and parts[0]['text'].startswith('UNTRUSTED') and 'never instructions' in \
        parts[0]['text']
    assert parts[1]['image_url']['url'].startswith('data:image/png;base64,')


def test_a_caret_does_not_stale_a_spot_but_a_layout_change_does():
    before = grey()
    caret = before.copy()
    ImageDraw.Draw(caret).line((200, 140, 200, 160), fill=0, width=2)  # a blinking text caret beside the point
    assert not sp.stale(before, caret, 205, 150, 40)
    moved = before.copy()
    ImageDraw.Draw(moved).rectangle((170, 120, 240, 180), fill=40)  # a dialog or a scroll moved content there
    assert sp.stale(before, moved, 205, 150, 40)
    assert sp.stale(before, grey((401, 300)), 205, 150, 40), 'another geometry is stale'
    assert sp.stale(None, before, 205, 150, 40) and sp.stale(before, before, 5000, 5000, 40)


def test_visible_change_is_exact_at_the_point_and_coarse_for_the_window():
    before = Image.new('RGB', (800, 600), (200, 200, 200))
    same = before.copy()
    assert sp.visible_change(before, same, (400, 300), 80) == 'none'
    pressed = before.copy()
    ImageDraw.Draw(pressed).rectangle((395, 295, 397, 297), fill=(190, 190, 190))  # a 3x3 highlight, 9 pixels
    assert sp.visible_change(before, pressed, (400, 300), 80) == 'at_point'
    faint = before.copy()
    faint.putpixel((400, 300), (199, 199, 199))  # within NOISE
    assert sp.visible_change(before, faint, (400, 300), 80) == 'none'
    sidebar = before.copy()
    ImageDraw.Draw(sidebar).rectangle((0, 0, 150, 599), fill=(30, 30, 30))
    assert sp.visible_change(before, sidebar, (700, 300), 80) == 'elsewhere'
    caret = before.copy()
    ImageDraw.Draw(caret).line((50, 50, 50, 70), fill=(0, 0, 0), width=1)
    assert sp.visible_change(before, caret, (700, 300), 80) == 'none', 'a caret elsewhere is not a change'
    assert sp.visible_change(before, Image.new('RGB', (801, 600)), (1, 1), 80) == 'elsewhere'
    assert sp.visible_change(before, sidebar, None, 0) == 'elsewhere', 'keys and typing: the window alone'


def test_probes_match_only_the_same_picture():
    one = grey((100, 100))
    assert sp.same_probe(one, one.copy())
    changed = one.copy()
    ImageDraw.Draw(changed).rectangle((10, 10, 20, 20), fill=0)
    assert not sp.same_probe(one, changed) and not sp.same_probe(one, None) and not sp.same_probe(one, grey((99, 100)))
    assert sp.spot(grey(), 5000, 5000, 10) is None and sp.spot(grey(), 10, 10, 5).size == (11, 11)
    assert sp.thumb(grey((1024, 512))).size == (256, 128)


# ---- the transform's pixel budget (screen_helper, Phase 45) ----------------------------------------------------

@pytest.mark.parametrize('pixels, frame, size', [
    ((1920, 1080), (0, 0, 1920, 1080), (1478, 831)),
    ((2880, 1800), (0, 0, 1440, 900), (1402, 876)),
    ((3456, 2234), (0, 0, 1728, 1117), (1379, 891)),
    ((1280, 800), (100, 50, 640, 400), (1280, 800)),   # within the budget: never enlarged or resized
])
def test_the_model_picture_fits_the_pixel_budget_and_maps_back_exactly(pixels, frame, size):
    t = Transform(pixels, frame, max_pixels=sh.MAX_PIXELS)
    assert t.size == size and t.size[0] * t.size[1] <= sh.MAX_PIXELS and max(t.size) <= sh.MAX_SIDE
    x, y = t.to_global(t.size[0] - 1, t.size[1] - 1)
    assert frame[0] <= x < frame[0] + frame[2] and frame[1] <= y < frame[1] + frame[3]
    assert all(abs(a - b) < 1e-6 for a, b in zip(t.to_model(*t.to_global(100, 100)), (100, 100)))


def test_a_stored_geometry_restores_the_same_transform_and_a_bad_one_is_refused():
    for t in (Transform((2880, 1800), (10, 20, 1440, 900), max_pixels=sh.MAX_PIXELS),
              Transform((1600, 1200), (100, 50, 800, 600), crop=(400, 300, 800, 600)),
              Transform((1600, 1200), (100, 50, 800, 600), box=(1600, 1600))):
        again = Transform.restore(t.geometry())
        assert (again.id, again.size, again.box, again.factor) == (t.id, t.size, t.box, t.factor)
        assert again.to_global(37, 41) == t.to_global(37, 41)
    good = Transform((1600, 1200), (0, 0, 800, 600)).geometry()
    for bad in ({**good, 'size': [0, 10]}, {**good, 'size': [1601, 10]}, {**good, 'box': [10, 10]},
                {**good, 'pixels': [1600, 900]}, {k: v for k, v in good.items() if k != 'crop'}, 'nonsense'):
        with pytest.raises(sh.PointRefused) as refused:
            Transform.restore(bad)
        assert refused.value.code == 'invalid', bad


# ---- where the helper is, and its switches ------------------------------------------------------------------------

def test_a_bundle_finds_the_pinned_helper_in_resources_helpers_first(tmp_path):
    resources = tmp_path / 'Pebble.app/Contents/Resources/resources'
    (resources / 'backend').mkdir(parents=True)
    pinned, earlier = resources / 'helpers/pebble-screen-helper', resources / 'screen-helper/pebble-screen-helper'
    for path in (pinned, earlier):
        path.parent.mkdir()
        path.write_bytes(b'#!/bin/sh\n')
        path.chmod(stat.S_IRWXU)
    executable = resources / 'backend/pebble-backend'
    assert sh.helper_path(executable) == pinned.resolve()
    pinned.chmod(stat.S_IRUSR)
    assert sh.helper_path(executable) == earlier.resolve(), 'not executable: the earlier folder, then nothing'
    earlier.unlink()
    assert sh.helper_path(executable) is None


def test_on_by_default_is_the_one_constant_and_a_zero_still_turns_a_lane_off(monkeypatch):
    allowed = {'protocol': sh.PROTOCOL, 'screen_recording': True, 'post_events': True, 'listen_events': True,
               'input_detection': 'event_tap'}
    for name in sh.VARIABLES.values():
        monkeypatch.delenv(name, raising=False)
    # D135 (the person): both lanes on by default since E-C3 and E-C4 passed on the person's Mac (D81)
    assert sh.ON_BY_DEFAULT == frozenset({'capture', 'foreground_input'})
    assert sh.capabilities(allowed) == {'capture': True, 'foreground_input': True}
    monkeypatch.setattr(sh, 'ON_BY_DEFAULT', frozenset())
    assert sh.capabilities(allowed) == {'capture': False, 'foreground_input': False}, 'the one constant'
    monkeypatch.setattr(sh, 'ON_BY_DEFAULT', frozenset({'capture', 'foreground_input'}))
    monkeypatch.setenv('PEBBLE_FOREGROUND_INPUT', '0')
    assert sh.capabilities(allowed) == {'capture': True, 'foreground_input': False}
    assert sh.capabilities({**allowed, 'screen_recording': False})['capture'] is False, 'macOS still decides'


# ---- the lane ------------------------------------------------------------------------------------------------------

class FakeClient:
    """HelperClient's surface: hello answers, a capture written into the host's folder, ocr, lease, input."""

    def __init__(self, image=None, *, hello=None, input_error=None, ocr_error=None, corrupt=False):
        self.image = image or Image.new('RGB', (64, 48), (90, 90, 90))
        self.answer = hello or {'protocol': sh.PROTOCOL, 'screen_recording': True, 'post_events': True,
                                'listen_events': True, 'input_detection': 'event_tap'}
        self.input_error, self.ocr_error, self.corrupt = input_error, ocr_error, corrupt
        self.calls, self.folders, self.closed = [], [], 0

    def call(self, op, args):
        self.calls.append(op)
        return dict(self.answer)

    def capture(self, number, folder, own_pids=()):
        self.calls.append('capture')
        self.folders.append(folder)
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700 and os.path.realpath(folder) == folder
        data = png_bytes(self.image)
        path = os.path.join(folder, 'capture-1-2.png')
        with open(path, 'wb') as stream:
            stream.write(data + (b'x' if self.corrupt else b''))
        return {'path': path, 'pixels': list(self.image.size), 'frame': [0.0, 0.0, 32.0, 24.0], 'scale': 2.0,
                'sha256': hashlib.sha256(data).hexdigest(), 'owner_pid': 77, 'on_screen': True}

    def ocr(self, folder, path, blank=(), level='accurate'):
        self.calls.append(('ocr', tuple(map(tuple, blank)), level))
        assert os.path.exists(path), 'text is read before the capture is removed'
        if self.ocr_error:
            raise HelperError(self.ocr_error, 'no text')
        return {'lines': [{'text': 'Save', 'box': [1.0, 2.0, 10.0, 4.0], 'confidence': 0.9}], 'truncated': False,
                'blanked': len(blank)}

    def lease(self, token, seconds):
        self.calls.append(('lease', seconds))

    def input(self, token, kind, *, idle_seconds=1.0, **fields):
        self.calls.append(('input', kind, idle_seconds, fields))
        if self.input_error:
            raise HelperError(self.input_error, 'detail')
        return {'posted': 2}

    def release(self):
        self.calls.append('release')

    def release_all(self):
        self.calls.append('release_all')

    def close(self):
        self.closed += 1


class FakeLease:
    held = []

    def __init__(self, busy=False):
        self.busy, self.token = busy, None

    def acquire(self, session_id):
        if self.busy:
            raise HelperError('refused', 'Another task is using the keyboard and mouse.')
        self.token = 'b' * 64
        FakeLease.held.append(session_id)
        return self.token

    def release(self):
        FakeLease.held.pop()


@pytest.fixture
def switched(monkeypatch):
    monkeypatch.setenv('PEBBLE_SCREEN_CAPTURE', '1')
    monkeypatch.setenv('PEBBLE_FOREGROUND_INPUT', '1')


def test_nothing_is_spawned_while_both_lanes_are_off(monkeypatch):
    for name in sh.VARIABLES.values():
        monkeypatch.setenv(name, '0')  # D135: on by default, so off is said
    lane = sp.PixelLane(lambda: pytest.fail('a helper was spawned'))
    assert lane.facts() == {'capture': False, 'foreground_input': False, 'hello': None}


def test_the_preflight_is_cached_and_asked_again_later(switched):
    now = [0.0]
    client = FakeClient()
    lane = sp.PixelLane(lambda: client, clock=lambda: now[0])
    assert lane.facts()['capture'] is True and lane.facts()['foreground_input'] is True
    assert client.calls == ['hello']
    now[0] += sp.FACTS_SECONDS + 1
    client.answer['screen_recording'] = False  # the person turned Screen Recording off meanwhile
    assert lane.facts()['capture'] is False and client.calls == ['hello', 'hello']


def test_without_a_helper_or_with_another_protocol_both_lanes_are_false(switched):
    def missing():
        raise HelperError('unavailable', 'The screen helper is not part of this build.')
    assert sp.PixelLane(missing).facts()['capture'] is False
    old = sp.PixelLane(lambda: FakeClient(hello={'protocol': 'pebble-screen/0', 'screen_recording': True}))
    assert old.facts()['capture'] is False


def test_a_capture_is_decoded_in_memory_and_its_file_and_folder_are_gone(switched):
    client = FakeClient()
    lane = sp.PixelLane(lambda: client)
    shot = lane.capture(7001, own_pids=(1,))
    assert shot['image'].size == (64, 48) and shot['owner_pid'] == 77 and 'lines' not in shot
    assert not os.path.exists(client.folders[0]), 'no image is kept on disk'
    corrupt = FakeClient(corrupt=True)
    with pytest.raises(ValueError):
        sp.PixelLane(lambda: corrupt).capture(7001)
    assert not os.path.exists(corrupt.folders[0]), 'removed when the capture is refused too'


def test_text_recognition_is_asked_with_the_captures_own_facts_and_its_failure_is_left_out(switched):
    client = FakeClient()
    asked = []

    def ocr(shot):
        asked.append(shot['scale'])
        return {'blank': [[0, 0, 4, 4]], 'level': 'fast'}
    shot = sp.PixelLane(lambda: client).capture(7001, ocr=ocr)
    assert asked == [2.0] and shot['lines']['lines'][0]['text'] == 'Save'
    assert ('ocr', ((0, 0, 4, 4),), 'fast') in client.calls
    failing = FakeClient(ocr_error='failed')
    assert 'lines' not in sp.PixelLane(lambda: failing).capture(7001, ocr=ocr), 'text recognition is extra'
    assert 'lines' not in sp.PixelLane(lambda: FakeClient()).capture(7001, ocr=lambda shot: None)


def test_an_input_step_holds_both_leases_only_while_it_runs(switched):
    client = FakeClient()
    lane = sp.PixelLane(lambda: client, lease_factory=FakeLease)
    assert lane.send('s1', 'click', {'point': [10.0, 20.0], 'button': 'left', 'count': 1}) == {'posted': 2}
    assert client.calls == [('lease', sp.LEASE_SECONDS), ('input', 'click', sp.IDLE_SECONDS,
                                                          {'point': [10.0, 20.0], 'button': 'left', 'count': 1,
                                                           'wait_seconds': sp.WAIT_SECONDS}),
                            'release']
    assert sp.WAIT_SECONDS + 2 < sp.LEASE_SECONDS, 'the wait fits inside the lease'
    assert FakeLease.held == []


def test_a_busy_lease_sends_nothing(switched):
    client = FakeClient()
    lane = sp.PixelLane(lambda: client, lease_factory=lambda: FakeLease(busy=True))
    with pytest.raises(sp.LeaseBusy) as busy:
        lane.send('s1', 'key', {'key': 's', 'modifiers': ['command']})
    assert busy.value.code == 'refused' and client.calls == []


@pytest.mark.parametrize('code, after', [('uncertain', 'release_all'), ('interrupted', 'release'),
                                         ('refused', 'release')])
def test_a_failed_step_releases_everything_and_an_uncertain_one_lifts_leftovers(switched, code, after):
    client = FakeClient(input_error=code)
    lane = sp.PixelLane(lambda: client, lease_factory=FakeLease)
    with pytest.raises(HelperError) as failed:
        lane.send('s1', 'type', {'text': 'hello'})
    assert failed.value.code == code and client.calls[-1] == after and FakeLease.held == []
