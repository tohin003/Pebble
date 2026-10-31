"""Phase 43 (1.9.0, M3 groundwork): the screen helper's host side over a fake helper served in-process over real pipes:
framing, deadlines, an input that never answers is uncertain and never retried, checked answers; the coordinate
transform (the backing scale measured, negative display origins, crops, padding and stale transforms refused); the
Mac-wide input lease; both lanes off unless switched on and allowed. No real helper, window or event is used."""
import json
import os
import stat
import threading
import time

import pytest

from pebble import screen_helper as sh
from pebble.screen_helper import HelperClient, HelperError, PointRefused, Transform


class FakeHelper:
    """The helper's contract over real pipes, answered by a thread. delay: seconds per op; mute: ops never answered;
    garble: an op answered with junk, or ('id', op) answered with the wrong id."""

    def __init__(self, folder, *, delay=None, mute=(), garble=None, hello=None):
        self.folder, self.delay, self.mute, self.garble = folder, dict(delay or {}), set(mute), garble
        self.hello = hello or {'protocol': sh.PROTOCOL, 'screen_recording': True, 'post_events': True,
                               'listen_events': True, 'input_detection': 'event_tap'}
        self.requests, self.lines = [], []
        host_in, helper_out = os.pipe()
        helper_in, host_out = os.pipe()
        self.stdin, self.stdout = os.fdopen(host_out, 'wb'), os.fdopen(host_in, 'rb')
        self._in, self._out = os.fdopen(helper_in, 'rb'), os.fdopen(helper_out, 'wb')
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            for line in self._in:
                self.lines.append(line)
                request = json.loads(line)
                self.requests.append(request)
                op = request['op']
                time.sleep(self.delay.get(op, 0))
                if op in self.mute:
                    continue
                if self.garble == op:
                    self._out.write(b'not json\n')
                else:
                    rid = request['id'] + (1 if self.garble == ('id', op) else 0)
                    self._out.write(json.dumps({'id': rid, 'ok': True, 'result': self.result(op, request['args'])})
                                    .encode() + b'\n')
                self._out.flush()
        except Exception:
            pass
        finally:
            for stream in (self._in, self._out):
                try:
                    stream.close()
                except Exception:
                    pass

    def result(self, op, args):
        if op == 'hello':
            return dict(self.hello)
        if op == 'capture':
            return {'path': str(self.folder / 'capture-1-2.png'), 'pixels': [1600, 1200],
                    'frame': [100.0, 50.0, 800.0, 600.0], 'scale': 2.0, 'sha256': 'a' * 64, 'owner_pid': 77,
                    'on_screen': True}
        if op == 'ocr':
            hidden = 'Play\N{RIGHT-TO-LEFT OVERRIDE} video\x07'  # an invisible override and a control character
            return {'lines': [{'text': hidden, 'confidence': 0.98, 'box': [10, 20, 80, 16]},
                              {'text': 'no box'}], 'blanked': len(args['blank']), 'truncated': False}
        if op == 'input':
            return {'posted': 2}
        return {'leased': True}

    def ops(self):
        return [r['op'] for r in self.requests]

    def poll(self):
        return None if self.thread.is_alive() else 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        self.thread.join(timeout)


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / 'captures'
    path.mkdir(mode=0o700)
    return path


def client_for(folder, spawned, **fake):
    def spawn(argv):
        spawned.append(FakeHelper(folder, **fake))
        return spawned[-1]
    return HelperClient(['unused'], spawn=spawn, deadlines={op: 2.0 for op in sh.OPS})


# ---- the client ----------------------------------------------------------------------------------------------

def test_a_request_is_one_framed_line_and_the_answer_is_matched_by_id(folder):
    spawned = []
    client = client_for(folder, spawned)
    try:
        assert client.hello()['protocol'] == sh.PROTOCOL
        assert client.capture(7001, folder, own_pids=[42])['pixels'] == [1600, 1200]
        line = spawned[0].lines[1]
        assert line.endswith(b'\n') and line.count(b'\n') == 1
        request = json.loads(line)
        assert set(request) == {'id', 'op', 'args'} and request['op'] == 'capture'
        assert request['args'] == {'window_number': 7001, 'folder': str(folder), 'own_pids': [42]}
        assert len(spawned) == 1, 'one helper for both calls'
    finally:
        client.close()


def test_a_slow_answer_times_out_kills_the_helper_and_the_next_call_starts_another(folder):
    spawned = []
    client = client_for(folder, spawned, delay={'ocr': 0.5})
    client.deadlines['ocr'] = 0.1
    try:
        with pytest.raises(HelperError) as slow:
            client.ocr(folder, folder / 'capture-1-2.png')
        assert slow.value.code == 'timeout' and client.proc is None
        assert client.hello()['protocol'] == sh.PROTOCOL and len(spawned) == 2
    finally:
        client.close()


def test_an_input_that_never_answers_is_uncertain_and_never_retried(folder):
    spawned = []
    client = client_for(folder, spawned, mute={'input'})
    client.deadlines['input'] = 0.1
    try:
        with pytest.raises(HelperError) as lost:
            client.input('b' * 64, 'click', point=[10.0, 20.0])
        assert lost.value.code == 'uncertain' and client.proc is None
        assert spawned[0].ops() == ['input']
        client.hello()
        assert [h.ops() for h in spawned] == [['input'], ['hello']], 'the input was never sent again'
    finally:
        client.close()


@pytest.mark.parametrize('garble', ['hello', ('id', 'hello')])
def test_a_garbled_or_misnumbered_answer_is_a_protocol_failure(folder, garble):
    spawned = []
    client = client_for(folder, spawned, garble=garble)
    try:
        with pytest.raises(HelperError) as broken:
            client.hello()
        assert broken.value.code == 'protocol' and client.proc is None
    finally:
        client.close()


def test_an_oversized_request_is_refused_before_anything_is_sent(folder):
    spawned = []
    client = client_for(folder, spawned)
    try:
        with pytest.raises(HelperError) as big:
            client.input('b' * 64, 'type', text='x' * (sh.MAX_REQUEST_BYTES + 1))
        assert big.value.code == 'invalid' and spawned == []
    finally:
        client.close()


def test_hello_is_asked_once_per_helper_and_must_speak_the_protocol(folder):
    spawned = []
    client = client_for(folder, spawned)
    try:
        client.hello()
        client.hello()
        assert spawned[0].ops() == ['hello']
    finally:
        client.close()
    other = client_for(folder, [], hello={'protocol': 'pebble-screen/0'})
    with pytest.raises(HelperError) as old:
        other.hello()
    assert old.value.code == 'protocol' and other.proc is None


def test_a_refusal_keeps_its_code(folder):
    class Refusing(FakeHelper):
        def _serve(self):
            for line in self._in:
                request = json.loads(line)
                self._out.write(json.dumps({'id': request['id'], 'ok': False, 'error': 'not_permitted',
                                            'detail': 'Screen Recording is off.'}).encode() + b'\n')
                self._out.flush()
    client = HelperClient(['unused'], spawn=lambda argv: Refusing(folder))
    try:
        with pytest.raises(HelperError) as refused:
            client.capture(5, folder)
        assert refused.value.code == 'not_permitted' and 'Screen Recording' in refused.value.detail
    finally:
        client.close()


def test_a_capture_answer_is_checked(folder, tmp_path):
    good = FakeHelper(folder).result('capture', {})
    checked = sh.checked_capture(good, folder)
    assert checked['scale'] == 2.0 and checked['frame'] == [100.0, 50.0, 800.0, 600.0]
    for broken in ({**good, 'path': str(tmp_path / 'capture-1-2.png')}, {**good, 'path': str(folder / 'x.png')},
                   {**good, 'sha256': 'A' * 64}, {**good, 'pixels': [0, 10]}, {**good, 'frame': [0, 0, 0, 10]},
                   {**good, 'scale': float('nan')}):
        with pytest.raises(HelperError) as bad:
            sh.checked_capture(broken, folder)
        assert bad.value.code == 'protocol'


def test_recognised_text_is_bounded_and_stripped(folder):
    spawned = []
    client = client_for(folder, spawned)
    try:
        read = client.ocr(folder, folder / 'capture-1-2.png', blank=[[0, 0, 10, 10]])
        assert read['lines'] == [{'text': 'Play video', 'box': [10.0, 20.0, 80.0, 16.0], 'confidence': 0.98}]
        assert read['blanked'] == 1 and spawned[0].requests[0]['args']['blank'] == [[0.0, 0.0, 10.0, 10.0]]
    finally:
        client.close()
    many = sh.checked_lines({'lines': [{'text': 'x' * 900, 'box': [0, 0, 1, 1]}] * (sh.MAX_LINES + 5)})
    assert len(many['lines']) == sh.MAX_LINES and many['truncated'] is True
    assert len(many['lines'][0]['text']) == sh.MAX_LINE_TEXT


def test_without_a_helper_the_client_refuses(monkeypatch):
    monkeypatch.setattr(sh, 'helper_path', lambda executable=None: None)
    with pytest.raises(HelperError) as none:
        HelperClient()
    assert none.value.code == 'unavailable'


def test_the_helper_is_found_beside_the_bundled_backend_or_named_in_a_source_run(tmp_path, monkeypatch):
    resources = tmp_path / 'Pebble.app/Contents/Resources'
    (resources / 'backend').mkdir(parents=True)
    helper = resources / 'screen-helper' / 'pebble-screen-helper'
    helper.parent.mkdir()
    helper.write_bytes(b'#!/bin/sh\n')
    assert sh.helper_path(resources / 'backend' / 'pebble-backend') is None, 'not executable'
    helper.chmod(stat.S_IRWXU)
    assert sh.helper_path(resources / 'backend' / 'pebble-backend') == helper.resolve()
    monkeypatch.setenv('PEBBLE_SCREEN_HELPER', str(helper))
    assert sh.helper_path() == helper
    monkeypatch.delenv('PEBBLE_SCREEN_HELPER')
    assert sh.helper_path() is None


def test_the_helper_starts_with_a_cleared_environment(monkeypatch):
    seen = {}

    class Popen:
        def __init__(self, argv, **kwargs):
            seen.update(kwargs)
    monkeypatch.setattr(sh.subprocess, 'Popen', Popen)
    monkeypatch.setenv('PEBBLE_SECRET_SETTING', 'x')
    sh._spawn(['helper'])
    assert set(seen['env']) == {'PATH', 'TMPDIR'} and seen['start_new_session'] is True


# ---- the transform (§6's coordinate spaces; the CSS viewport is the browser adapter's) --------------------------

def test_a_plain_display_maps_one_to_one():
    t = Transform((800, 600), (100, 50, 800, 600))
    assert t.scale == (1.0, 1.0) and t.size == (800, 600)
    assert t.to_global(10, 20) == (110.0, 70.0)


def test_a_retina_capture_uses_its_measured_scale_never_a_plain_doubling():
    t = Transform((1600, 1200), (100, 50, 800, 600))
    assert t.scale == (2.0, 2.0) and t.size == (1568, 1176), 'resized to fit, never enlarged'
    x, y = t.to_global(784, 588)
    assert (round(x, 6), round(y, 6)) == (500.0, 350.0)
    assert (x, y) != (100 + 784 * 2, 50 + 588 * 2), 'not doubled'
    half = Transform((1200, 900), (0, 0, 800, 600))
    assert half.scale == (1.5, 1.5) and half.to_global(150, 90) == (100.0, 60.0)


def test_a_window_on_a_display_left_of_and_above_the_main_one_has_negative_points():
    t = Transform((1600, 1200), (-1440, -200, 800, 600))
    x, y = t.to_global(0, 0)
    assert (x, y) == (-1440.0, -200.0)
    x, y = t.to_global(784, 588)
    assert (round(x, 6), round(y, 6)) == (-1040.0, 100.0)
    assert all(abs(a - b) < 1e-6 for a, b in zip(t.to_model(x, y), (784, 588))), 'round trip'


def test_a_crop_maps_back_through_its_origin():
    t = Transform((1600, 1200), (100, 50, 800, 600), crop=(400, 300, 800, 600))
    assert t.size == (800, 600) and t.to_global(0, 0) == (300.0, 200.0)
    assert t.to_global(799, 599) == (699.5, 499.5)


def test_points_in_the_padding_or_outside_the_picture_are_refused():
    t = Transform((1600, 1200), (100, 50, 800, 600), box=(1600, 1600))
    for point, code in (((100, 1300), 'in_padding'), ((1570, 10), 'in_padding'), ((1700, 10), 'outside_image'),
                        ((-1, 10), 'outside_image'), ((float('nan'), 10), 'outside_image')):
        with pytest.raises(PointRefused) as refused:
            t.to_global(*point)
        assert refused.value.code == code, point


def test_a_moved_resized_or_rescaled_window_or_another_transform_is_stale():
    t = Transform((1600, 1200), (100, 50, 800, 600))
    assert t.to_global(10, 10, frame=(100.3, 50, 800, 600), pixels=(1600, 1200), transform_id=t.id)
    for current in ({'frame': (110, 50, 800, 600)}, {'frame': (100, 50, 820, 600)}, {'pixels': (800, 600)},
                    {'transform_id': '0' * 16}):
        with pytest.raises(PointRefused) as stale:
            t.to_global(10, 10, **current)
        assert stale.value.code == 'stale_transform', current
    assert Transform((1600, 1200), (100, 50, 800, 600)).id == t.id
    assert Transform((1600, 1200), (101, 50, 800, 600)).id != t.id


@pytest.mark.parametrize('pixels, frame, kw', [
    ((1600, 1000), (0, 0, 800, 600), {}),                        # the scales differ by axis
    ((1600, 1200), (0, 0, 800, 600), {'crop': (1000, 0, 800, 600)}),
    ((1600, 1200), (0, 0, 800, 600), {'box': (100, 100)}),
    ((0, 1200), (0, 0, 800, 600), {}),
    ((1600, 1200), (0, 0, 0, 600), {}),
])
def test_a_capture_that_does_not_match_its_window_or_a_bad_crop_is_refused(pixels, frame, kw):
    with pytest.raises(PointRefused) as bad:
        Transform(pixels, frame, **kw)
    assert bad.value.code == 'invalid'


# ---- the lease and the switches -------------------------------------------------------------------------------

def test_one_session_holds_the_keyboard_and_mouse_at_a_time(tmp_path):
    one, two = sh.InputLease(tmp_path), sh.InputLease(tmp_path)
    token = one.acquire('s1')
    assert len(token) == 64 and all(c in '0123456789abcdef' for c in token)
    with pytest.raises(HelperError) as busy:
        two.acquire('s2')
    assert busy.value.code == 'refused' and two.token is None
    with pytest.raises(HelperError):
        one.acquire('s1')
    one.release()
    assert two.acquire('s2') != token
    two.release()
    assert oct(os.stat(one.path).st_mode & 0o777) == '0o600'


def test_both_lanes_follow_their_switch_and_need_macos_to_allow_them(monkeypatch):
    allowed = {'screen_recording': True, 'post_events': True, 'listen_events': True, 'input_detection': 'event_tap'}
    monkeypatch.delenv('PEBBLE_SCREEN_CAPTURE', raising=False)
    monkeypatch.delenv('PEBBLE_FOREGROUND_INPUT', raising=False)
    # Phase 45 (D135, the person): on by default since E-C3 passed on the person's Mac (off by default before, D81)
    assert sh.capabilities(allowed) == {'capture': True, 'foreground_input': True}, 'on by default (D135)'
    monkeypatch.setenv('PEBBLE_SCREEN_CAPTURE', '0')
    monkeypatch.setenv('PEBBLE_FOREGROUND_INPUT', '0')
    assert sh.capabilities(allowed) == {'capture': False, 'foreground_input': False}, 'a lane set to 0 is off'
    monkeypatch.setenv('PEBBLE_SCREEN_CAPTURE', '1')
    monkeypatch.setenv('PEBBLE_FOREGROUND_INPUT', '1')
    assert sh.capabilities(allowed) == {'capture': True, 'foreground_input': True}
    assert sh.capabilities({**allowed, 'screen_recording': False})['capture'] is False
    assert sh.capabilities({**allowed, 'input_detection': 'none'})['foreground_input'] is False, \
        'no reliable way to notice the person: the lane stays off'
    assert sh.capabilities({**allowed, 'post_events': False})['foreground_input'] is False
    assert sh.capabilities(None) == {'capture': False, 'foreground_input': False}


def test_nothing_is_spawned_while_both_switches_are_off(monkeypatch, folder):
    monkeypatch.setenv('PEBBLE_SCREEN_CAPTURE', '0')  # D135: on by default, so off is said
    monkeypatch.setenv('PEBBLE_FOREGROUND_INPUT', '0')
    made = []
    assert sh.probe(lambda: made.append(1)) == {'capture': False, 'foreground_input': False} and made == []
    monkeypatch.setenv('PEBBLE_SCREEN_CAPTURE', '1')
    spawned = []
    assert sh.probe(lambda: client_for(folder, spawned)) == {'capture': True, 'foreground_input': False}
    assert spawned and spawned[0].ops() == ['hello']
