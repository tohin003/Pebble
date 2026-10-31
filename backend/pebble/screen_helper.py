"""The host side of Pebble's screen helper (Phase 43, 1.9.0, M3; design §6, decisions 5 and 6).

The helper (`src-tauri/helpers/screen/main.swift`; Phase 45: pinned by scripts/build_screen_helper.py and bundled as
resources/helpers/pebble-screen-helper beside resources/backend/, next to the vault helper) captures one window,
recognises text in a captured image on the device, and posts foreground keyboard and mouse input under a lease.
Protocol pebble-screen/1: README.md beside its source. Phase 45 (layer 4): screen_pixels.py drives it for screenshots
in observations and pixel actions.

client      HelperClient spawns it lazily, frames one JSON request per line with a deadline per op, kills it on any
            protocol failure, and reports an input whose answer never arrives as `uncertain`: it may or may not have
            happened, and it is never retried.
transform   Transform owns the coordinate spaces of §6 for one capture: capture pixels, model image pixels (after the
            crop, one resize and padding on the right and bottom), window content points and global desktop points
            (the browser's CSS viewport is the browser adapter's). The backing scale is measured from the capture,
            never assumed; a point in the padding, outside the image or the window, or on a transform the window has
            since moved, resized or changed display for, is refused.
lease       InputLease: one session holds the keyboard and mouse at a time across the Mac (an exclusive lock in the
            person's own temporary folder); its token is what the helper checks before any event.
switches    capabilities() reports capture and foreground input false unless PEBBLE_SCREEN_CAPTURE=1 or
            PEBBLE_FOREGROUND_INPUT=1 is set and the helper's own preflight says macOS allows them. Both lanes ship off
            until they are measured with the person (D81); their permissions are the person's to grant (D111).
            Phase 45: ON_BY_DEFAULT is the one constant that turns a lane on by default once E-C3/E-C4 pass (its
            variable set to 0 still turns it off).
"""

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import selectors
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .policy import PolicyError

PROTOCOL = 'pebble-screen/1'
OPS = ('hello', 'capture', 'ocr', 'lease', 'release', 'release_all', 'input')
DEADLINES = {'hello': 5.0, 'capture': 12.0, 'ocr': 20.0, 'lease': 5.0, 'release': 5.0, 'release_all': 5.0,
             'input': 15.0}
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 1_048_576
MAX_LINES = 400
MAX_LINE_TEXT = 500
MAX_SIDE = 1568           # the model image's longer side at most (a resize never enlarges)
# Phase 45 (layer 4, report 5 §1 and §6): and at most this many pixels, 1568 image tokens of 28 x 28 px (about 1.2 MP;
# a 1920 x 1080 capture becomes 1478 x 831), so a provider need not resize it again behind the coordinates' back.
MAX_PIXELS = 1_229_312
EDGE = 0.5                # points: how far a window may have moved or resized and still match its capture
ASPECT = 0.01             # a capture whose pixel aspect differs more than this from its frame's is refused
HEX64 = re.compile(r'[0-9a-f]{64}')
CODES = ('unavailable', 'invalid', 'not_permitted', 'not_found', 'refused', 'interrupted', 'timeout', 'unsupported',
         'failed', 'protocol', 'uncertain')


class HelperError(Exception):
    """A refused or failed helper call. code is one of CODES; detail is short, content-free text."""

    def __init__(self, code, detail=''):
        super().__init__(detail or code)
        self.code = code if code in CODES else 'failed'
        self.detail = ' '.join(str(detail).split())[:300]


class PointRefused(PolicyError):
    """A point the transform will not map. code: stale_transform, in_padding, outside_image or outside_window."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


BUNDLED = ('helpers', 'pebble-screen-helper')       # Phase 45: where the candidate builder stages the pinned helper
EARLIER = ('screen-helper', 'pebble-screen-helper')  # Phase 43's planned folder (never staged by a build)


def helper_path(executable=None):
    """The bundled helper beside resources/backend/: resources/helpers/pebble-screen-helper (Phase 45, the pinned one
    the candidate builder stages and checks), else Phase 43's resources/screen-helper/; in a source run, a development
    build named by PEBBLE_SCREEN_HELPER. None when there is none."""
    if getattr(sys, 'frozen', False) or executable is not None:
        resources = Path(executable or sys.executable).resolve().parent.parent
        paths = [resources.joinpath(*BUNDLED), resources.joinpath(*EARLIER)]
    else:
        named = os.environ.get('PEBBLE_SCREEN_HELPER')
        paths = [Path(named)] if named else []
    return next((path for path in paths if path.is_file() and os.access(path, os.X_OK)), None)


def _spawn(argv):
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            close_fds=True, start_new_session=True,
                            env={'PATH': '/usr/bin:/bin', 'TMPDIR': os.environ.get('TMPDIR', '/tmp')})


class HelperClient:
    """Host side of the protocol. One request at a time; any failure kills the helper.

    spawn(argv) returns an object with stdin, stdout (with fileno), poll(), kill() and wait(); tests pass a pipe pair
    served by an in-process thread.
    """

    def __init__(self, argv=None, *, spawn=None, clock=time.monotonic, deadlines=None):
        path = None if argv is not None else helper_path()
        if argv is None and path is None:
            raise HelperError('unavailable', 'The screen helper is not part of this build.')
        self.argv = list(argv) if argv is not None else [str(path)]
        self.spawn, self.clock = spawn or _spawn, clock
        self.deadlines = {**DEADLINES, **(deadlines or {})}
        self.proc, self.buffer, self.next_id, self._hello = None, b'', 0, None
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            self._kill()

    def _kill(self):
        proc, self.proc, self.buffer, self._hello = self.proc, None, b'', None
        if proc is None:
            return
        for stream in (getattr(proc, 'stdin', None), getattr(proc, 'stdout', None)):
            try:
                stream.close()
            except Exception:
                pass
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass

    def _readline(self, deadline):
        fd = self.proc.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise HelperError('timeout', 'The screen helper did not answer in time.')
                if not selector.select(remaining):
                    continue
                chunk = os.read(fd, 65_536)
                if not chunk:
                    raise HelperError('protocol', 'The screen helper exited.')
                self.buffer += chunk
                if len(self.buffer) > MAX_RESPONSE_BYTES + 1:
                    raise HelperError('protocol', 'The screen helper answer exceeded its size limit.')
        line, _, self.buffer = self.buffer.partition(b'\n')
        return line

    def call(self, op, args):
        if op not in OPS:
            raise HelperError('invalid', 'op')
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            data = json.dumps({'id': rid, 'op': op, 'args': args}, ensure_ascii=False, separators=(',', ':'),
                              allow_nan=False).encode('utf-8') + b'\n'
            if len(data) > MAX_REQUEST_BYTES:
                raise HelperError('invalid', 'The request exceeded its size limit.')
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._kill()
                    self.proc = self.spawn(self.argv)
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
                response = json.loads(self._readline(self.clock() + self.deadlines[op]))
                if not isinstance(response, dict) or response.get('id') != rid or type(response.get('ok')) is not bool:
                    raise HelperError('protocol', 'The screen helper answered out of order.')
            except (HelperError, OSError, ValueError) as exc:
                self._kill()
                if op == 'input':  # the events may or may not have been posted; never retry them
                    raise HelperError('uncertain', getattr(exc, 'detail', type(exc).__name__)) from None
                if isinstance(exc, HelperError):
                    raise
                raise HelperError('protocol', type(exc).__name__) from None
        if not response['ok']:
            raise HelperError(response.get('error'), response.get('detail') or '')
        result = response.get('result')
        if not isinstance(result, dict):
            raise HelperError('protocol', 'The screen helper answered without a result.')
        return result

    def hello(self):
        """The helper's preflight, asked once per helper process (nothing in it prompts)."""
        if self._hello is not None and self.proc is not None and self._hello[0] is self.proc:
            return self._hello[1]
        result = self.call('hello', {})
        if result.get('protocol') != PROTOCOL:
            self.close()
            raise HelperError('protocol', 'The screen helper speaks another protocol.')
        self._hello = (self.proc, result)
        return result

    def capture(self, window_number, folder, own_pids=()):
        """One window's own pixels as a new PNG in `folder` (Pebble's, mode 700); the answer's facts checked."""
        result = self.call('capture', {'window_number': window_number, 'folder': str(folder),
                                       'own_pids': [int(p) for p in own_pids][:16]})
        return checked_capture(result, folder)

    def ocr(self, folder, path, blank=(), level='accurate'):
        """Text lines of a capture, with the host's rectangles (secure fields) blanked before any text is read."""
        result = self.call('ocr', {'folder': str(folder), 'path': str(path), 'level': level,
                                   'blank': [[float(v) for v in rect] for rect in blank]})
        return checked_lines(result)

    def lease(self, token, seconds):
        return self.call('lease', {'token': token, 'seconds': float(seconds)})

    def release(self):
        return self.call('release', {})

    def release_all(self):
        return self.call('release_all', {})

    def input(self, token, kind, *, idle_seconds=1.0, **fields):
        """One input step under the lease; an answer that never arrives raises `uncertain`."""
        return self.call('input', {'lease': token, 'kind': kind, 'idle_seconds': float(idle_seconds), **fields})


def _number(value, low=0.0):
    return type(value) in (int, float) and math.isfinite(value) and value >= low


def checked_capture(result, folder):
    """The capture answer, or HelperError('protocol') when a fact is out of shape or the file is outside `folder`."""
    pixels, frame, path = result.get('pixels'), result.get('frame'), result.get('path')
    ok = (isinstance(path, str) and Path(path).parent == Path(folder) and Path(path).name.startswith('capture-')
          and path.endswith('.png') and isinstance(pixels, list) and len(pixels) == 2
          and all(type(v) is int and 0 < v <= 32_768 for v in pixels) and isinstance(frame, list) and len(frame) == 4
          and all(_number(v, -1e6) for v in frame) and frame[2] > 0 and frame[3] > 0
          and _number(result.get('scale'), 0.01) and isinstance(result.get('sha256'), str)
          and HEX64.fullmatch(result['sha256']) is not None)
    if not ok:
        raise HelperError('protocol', 'The capture answer is out of shape.')
    return {'path': path, 'pixels': list(pixels), 'frame': [float(v) for v in frame], 'scale': float(result['scale']),
            'sha256': result['sha256'], 'owner_pid': result.get('owner_pid'),
            'on_screen': result.get('on_screen') is True}


def checked_lines(result):
    """Recognised lines, bounded: at most MAX_LINES, each text clipped and stripped of control characters (the text
    is untrusted page or window content, never instructions)."""
    from .text_safety import INVISIBLE, UNSAFE
    lines = []
    for item in (result.get('lines') or [])[:MAX_LINES]:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            continue
        box = item.get('box')
        if not (isinstance(box, list) and len(box) == 4 and all(_number(v, -1e6) for v in box)):
            continue
        text = UNSAFE.sub(' ', INVISIBLE.sub('', item['text']))[:MAX_LINE_TEXT].strip()
        confidence = item.get('confidence')
        if text:
            lines.append({'text': text, 'box': [float(v) for v in box],
                          'confidence': float(confidence) if _number(confidence) else None})
    return {'lines': lines, 'truncated': result.get('truncated') is True or len(result.get('lines') or []) > MAX_LINES,
            'blanked': result.get('blanked') if type(result.get('blanked')) is int else 0}


class Transform:
    """One capture's coordinate spaces: capture pixels -> model image pixels -> window content points -> global points.

    `pixels` is the capture's size; `frame` the window's (x, y, w, h) in global points when it was taken, so the
    backing scale is pixels per point on each axis, measured, never a plain Retina doubling. The model image is `crop`
    (x, y, w, h in capture pixels; the whole capture by default) resized by one factor to fit MAX_SIDE (Phase 45: and
    max_pixels, when given), then padded on the right and bottom to `box`. The ID names this whole geometry.
    """

    def __init__(self, pixels, frame, *, crop=None, max_side=MAX_SIDE, box=None, max_pixels=None):
        pw, ph = pixels
        fx, fy, fw, fh = (float(v) for v in frame)
        if not (type(pw) is int and type(ph) is int and pw > 0 and ph > 0 and fw > 0 and fh > 0
                and all(math.isfinite(v) for v in (fx, fy, fw, fh))):
            raise PointRefused('invalid', 'The capture geometry is malformed.')
        self.scale = (pw / fw, ph / fh)
        if abs(self.scale[0] - self.scale[1]) > ASPECT * max(self.scale):
            raise PointRefused('invalid', 'The capture does not match its window (the scales differ by axis).')
        cx, cy, cw, ch = crop if crop is not None else (0, 0, pw, ph)
        if not (0 <= cx and 0 <= cy and 0 < cw and 0 < ch and cx + cw <= pw and cy + ch <= ph):
            raise PointRefused('invalid', 'The crop lies outside the capture.')
        factor = min(1.0, max_side / max(cw, ch))
        if max_pixels is not None and cw * ch * factor * factor > max_pixels:
            factor = math.sqrt(max_pixels / (cw * ch))  # Phase 45: the model's pixel budget, never an enlargement
        size = (max(1, round(cw * factor)), max(1, round(ch * factor)))
        if max_pixels is not None and size[0] * size[1] > max_pixels:
            size = (max(1, math.floor(cw * factor)), max(1, math.floor(ch * factor)))
        box = tuple(box) if box is not None else size
        if box[0] < size[0] or box[1] < size[1]:
            raise PointRefused('invalid', 'The padded image is smaller than the resized crop.')
        self.pixels, self.frame, self.crop = (pw, ph), (fx, fy, fw, fh), (cx, cy, cw, ch)
        self.factor, self.size, self.box = (size[0] / cw, size[1] / ch), size, box
        geometry = {'pixels': self.pixels, 'frame': self.frame, 'crop': self.crop, 'size': self.size, 'box': self.box}
        self.id = hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()[:16]

    def geometry(self):
        """Phase 45: the numbers that name this transform (kept with a reviewed pixel act; no image)."""
        return {'pixels': list(self.pixels), 'frame': list(self.frame), 'crop': list(self.crop), 'size': list(self.size),
                'box': list(self.box)}

    @classmethod
    def restore(cls, geometry):
        """Phase 45: the transform a stored geometry names, exactly (its size is taken, not recomputed), its ID
        recomputed from it; PointRefused('invalid') when the numbers do not describe one."""
        try:
            pixels, frame, crop, size, box = (tuple(geometry[k]) for k in ('pixels', 'frame', 'crop', 'size', 'box'))
            sized = len(size) == 2 and len(box) == 2 and all(type(v) is int and 0 < v <= 32_768 for v in size + box) \
                and len(crop) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in crop)
        except (KeyError, TypeError, ValueError):
            sized = False
        if not sized or not 0 < size[0] <= crop[2] or not 0 < size[1] <= crop[3] or box[0] < size[0] \
                or box[1] < size[1]:
            raise PointRefused('invalid', 'The stored geometry is malformed.')
        made = cls(pixels, frame, crop=crop, max_side=max(crop[2], crop[3]))  # the capture checks, unresized
        made.factor, made.size, made.box = (size[0] / crop[2], size[1] / crop[3]), size, box
        geometry = {'pixels': made.pixels, 'frame': made.frame, 'crop': made.crop, 'size': made.size, 'box': made.box}
        made.id = hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()[:16]
        return made

    def check(self, *, transform_id=None, frame=None, pixels=None):
        """Refuse when the caller's transform is another, or the window has since moved, resized or changed scale
        (`frame` and `pixels` are its current ones, read again just before an input)."""
        if transform_id is not None and transform_id != self.id:
            raise PointRefused('stale_transform', 'That point belongs to another look at the window. Look again.')
        if frame is not None and any(abs(float(a) - b) > EDGE for a, b in zip(frame, self.frame)):
            raise PointRefused('stale_transform', 'The window moved or changed size since Pebble looked. Look again.')
        if pixels is not None and tuple(pixels) != self.pixels:
            raise PointRefused('stale_transform', 'The window changed display or scale since Pebble looked. '
                                                  'Look again.')

    def to_global(self, x, y, **current):
        """Global desktop points for a point in the model image."""
        self.check(**current)
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (x, y)):
            raise PointRefused('outside_image', 'The point is not a number.')
        if not (0 <= x < self.size[0] and 0 <= y < self.size[1]):
            inside_box = 0 <= x < self.box[0] and 0 <= y < self.box[1]
            raise PointRefused('in_padding' if inside_box else 'outside_image',
                               'The point is in the padding around the picture.' if inside_box
                               else 'The point is outside the picture.')
        px, py = self.crop[0] + x / self.factor[0], self.crop[1] + y / self.factor[1]
        wx, wy = px / self.scale[0], py / self.scale[1]
        if not (0 <= wx < self.frame[2] and 0 <= wy < self.frame[3]):
            raise PointRefused('outside_window', 'The point is outside the window.')
        return self.frame[0] + wx, self.frame[1] + wy

    def to_model(self, gx, gy):
        """The model-image point for a global point (for drawing what the tree knows onto the picture)."""
        px = (gx - self.frame[0]) * self.scale[0]
        py = (gy - self.frame[1]) * self.scale[1]
        return (px - self.crop[0]) * self.factor[0], (py - self.crop[1]) * self.factor[1]


class InputLease:
    """The Mac-wide single controller: an exclusive, non-blocking lock in the person's own temporary folder. Holding
    it gives the token the helper checks before every event; releasing it (or this process ending) frees it."""

    def __init__(self, folder=None):
        self.path = Path(folder or tempfile.gettempdir()) / f'pebble-input-{os.getuid()}.lock'
        self.fd, self.token, self.session = None, None, None

    def acquire(self, session_id):
        if self.fd is not None:
            raise HelperError('refused', 'This lease is already held.')
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise HelperError('refused', 'Another task is using the keyboard and mouse.') from None
        self.fd, self.token, self.session = fd, secrets.token_hex(32), session_id
        return self.token

    def release(self):
        fd, self.fd, self.token, self.session = self.fd, None, None, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


# Phase 45 (layer 4, D81): the lanes on by default. Empty until the parent measures E-C3 (input) and E-C4 (capture and
# the transform) on the person's Mac; adding 'capture' and 'foreground_input' here is the whole change. A lane's
# variable set to 1 turns it on, and set to 0 keeps it off even when it is on by default.
# D135 (the person, 2026-10-26): on by default now that E-C3 passed on the person's Mac (audit round R6 on 1.9.10:
# every observation carried a screenshot, 22 of 23; Chess 2/2 and Calculator finished with both lanes on).
ON_BY_DEFAULT = frozenset({'capture', 'foreground_input'})
VARIABLES = {'capture': 'PEBBLE_SCREEN_CAPTURE', 'foreground_input': 'PEBBLE_FOREGROUND_INPUT'}


def switches():
    return {lane: os.environ.get(name) == '1' or lane in ON_BY_DEFAULT and os.environ.get(name) != '0'
            for lane, name in VARIABLES.items()}


def capabilities(hello=None):
    """Capture and foreground input as host facts (data, never permission): each true only with its switch on and the
    helper's preflight saying macOS allows it. Input also needs the event tap that notices the person's own input."""
    on, facts = switches(), hello if isinstance(hello, dict) else {}
    return {'capture': on['capture'] and facts.get('screen_recording') is True,
            'foreground_input': on['foreground_input'] and facts.get('post_events') is True
            and facts.get('listen_events') is True and facts.get('input_detection') == 'event_tap'}


def probe(client_factory=HelperClient):
    """capabilities() with the helper asked only when a switch is on (so nothing is spawned while both are off)."""
    if not any(switches().values()):
        return capabilities(None)
    try:
        client = client_factory()
    except HelperError:
        return capabilities(None)
    try:
        return capabilities(client.hello())
    except HelperError:
        return capabilities(None)
    finally:
        client.close()
