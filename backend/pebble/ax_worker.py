"""Accessibility (AX) worker for scoped screen sessions (Phase 16, D04).

Two halves share this module so the frozen executable needs no extra file:

worker   `pebble-backend --ax-worker` reads one JSON request per line on stdin
         and answers one JSON line on stdout. Every answer passes through the
         pure sanitizers below before it is written, so secure-field values
         never leave the worker and every string is bounded.
client   WorkerClient spawns the worker lazily, frames requests, enforces a
         per-request deadline and kills the worker on any protocol failure. A
         perform whose answer never arrives raises AXError('uncertain'); it is
         never retried.

The PID in a request is a transient lookup the host re-resolved for this one
call from the bundle ID plus the process start time. Backends must refuse
(`instance_changed`) when the PID's start time no longer equals `start`, so a
reused PID can never be driven (handoff contract 10).

Real Accessibility is blocked on the person's grant and experiment E-N2 (is
TCC attributed to Pebble.app when a frozen backend child calls AX?). Until
that evidence exists the only production backend is BlockedBackend, which
refuses every call without touching any OS API. Tests drive the protocol with
a fake backend. Nothing here captures the screen, launches apps, synthesizes
keyboard events or reads real windows.
"""

import hashlib
import json
import math
import os
import re
import selectors
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from .text_safety import INVISIBLE as SHARED_INVISIBLE, UNSAFE, secret_text


# Phase 43 (1.9.0, D112): protocol 2 names windows by their window-server number, admits the focused and main windows
# AXWindows leaves out, and adds enhance and restore (a Chromium or Electron app's page accessibility) and raise.
PROTOCOL = 'pebble-ax/2'
OPS = ('hello', 'windows', 'tree', 'perform', 'enhance', 'restore', 'raise')
# Phase 45 (layer 4): hit, what is under a global point or holds the keyboard, before every pixel act. The worker and
# the host ship in one backend, so it is served without joining hello's list (1.9.x's hello stays as it was).
EXTRA_OPS = ('hit',)
HIT_ANCESTORS = 4           # ancestors a hit reports (the control a click on its text reaches)
HIT_ATTRIBUTES = ('AXRole', 'AXSubrole', 'AXTitle', 'AXDescription', 'AXPlaceholderValue', 'AXHelp', 'AXEnabled',
                  'AXFocused', 'AXSelected', 'AXExpanded', 'AXPosition', 'AXSize', 'AXURL', 'AXParent', 'AXWindow')
MENU_ROLES = frozenset({'AXMenu', 'AXMenuItem', 'AXMenuBar', 'AXMenuBarItem'})
ACTIONS = ('focus', 'scroll', 'expand', 'collapse', 'select', 'toggle', 'type', 'press', 'enter')
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 1_048_576
REQUEST_SECONDS = 5.0
MAX_WINDOWS = 32
MAX_ELEMENTS = 300
MAX_LABEL_SOURCE = 512      # each label source is clipped here before hashing, in the worker and the host alike
MAX_TITLE_SOURCE = 1024
MAX_VALUE = 200
# Phase 45 (Phase 46 S4's finding): TextEdit's document read as one 200-character line, so "the third line" had to be
# guessed; a multi-line text area shows its lines, up to this many characters (its digest is computed as before)
MAX_AREA_VALUE = 2000
MAX_PATH = 512
MAX_REF = 128
MAX_URL = 2048
MAX_TEXT = 4000
ROLE = re.compile(r'AX[A-Za-z]{1,48}')
HEX64 = re.compile(r'[0-9a-f]{64}')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}')
LABEL_KEYS = ('title', 'description', 'placeholder', 'help')
FLAGS = ('enabled', 'focused', 'selected', 'expanded', 'editable', 'multiline', 'download')
SECURE_ROLES = frozenset({'AXSecureTextField'})
TEXT_ROLES = frozenset({'AXTextField', 'AXTextArea', 'AXComboBox', 'AXSearchField', 'AXSecureTextField'})
SECURE_AUTOCOMPLETE = re.compile(r'(?:^|\s)(one-time-code|cc-[a-z-]+|current-password|new-password)(?:\s|$)')
# A label can only raise a field to secure, never lower one: the role, subrole,
# HTML type and autocomplete decide first.
SECRET_LABEL = re.compile(
    r'\b(pass[\s-]?(?:word|code|phrase)s?|pins?|one[- ]?time|otp|2fa|mfa|two[- ](?:factor|step)|'
    r'(?:verification|security|authentication|auth|login|sign[- ]in|access|confirmation) code|'
    r'cvv|cvc|csc|card number|credit card|secret|private key|api key|recovery (?:code|phrase|key)|'
    r'seed phrase|ssn|social security)\b', re.I)
# Phase 24 (V1-054, V1-168): one shared set, written as escapes (text_safety). It now also covers the bidi
# isolates U+2066-U+2069, U+061C, U+180E and the tag block, which sources.INVISIBLE already stripped.
CONTROL = UNSAFE
# Invisible format characters are removed (not spaced) so they cannot split a
# word like "Pass" + U+202E + "code" past the secret-label check; controls become spaces.
INVISIBLE = SHARED_INVISIBLE
# Window facts an adapter may report. 'instance' is added by the host adapter
# (bundle ID @ process start), never by the worker.
WINDOW_TEXT = {'window_ref': MAX_REF, 'ax_identifier': 200, 'tab_id': MAX_REF, 'origin': 300, 'url': MAX_URL,
               'navigation_id': MAX_REF, 'loader_id': MAX_REF, 'instance': 300}
WINDOW_PATH = '.'   # perform(path='.', action='focus') raises the window itself


class AXError(Exception):
    """A refused or failed AX call. code is one of CODES; detail is short, content-free text."""

    CODES = ('unavailable', 'not_trusted', 'not_found', 'instance_changed', 'unsupported', 'invalid', 'failed',
             'timeout', 'uncertain', 'protocol')

    def __init__(self, code, detail=''):
        super().__init__(detail or code)
        self.code = code if code in self.CODES else 'failed'
        self.detail = ' '.join(str(detail).split())[:300]


def sha256(text):
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()


def clip(value, limit, *, lower=False, lines=False):
    """Printable NFC text clipped to limit characters, or None for non-text. lines keeps line breaks (as \\n)."""
    if not isinstance(value, str):
        return None
    text = INVISIBLE.sub('', unicodedata.normalize('NFC', value))
    if lines:
        text = '\n'.join(CONTROL.sub(' ', line) for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'))
    else:
        text = CONTROL.sub(' ', text)
    text = text[:limit]
    return text.lower() if lower else text


def _frame(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e6 for v in value):
        return None
    return [int(round(v)) for v in value]


def editable_text(element):
    return element.get('role') in TEXT_ROLES or element.get('editable') is True


def label_sources(element):
    """The texts a label hash covers. Static text carries its words in the value."""
    sources = [element.get(k) for k in LABEL_KEYS]
    if element.get('role') == 'AXStaticText' and isinstance(element.get('value'), str):
        sources.append(element['value'])
    return sources


def label_text(element):
    """The words a person would read for this element: title, description, static text, then hints."""
    static = element.get('value') if element.get('role') == 'AXStaticText' else None
    for text in (element.get('title'), element.get('description'), static, element.get('placeholder'),
                 element.get('help')):
        if isinstance(text, str) and text.strip():
            return ' '.join(text.split())
    return ''


def label_sha256(element):
    return sha256(json.dumps(label_sources(element), ensure_ascii=False, separators=(',', ':')))


def is_secure(element):
    """True for a field whose value Pebble must never read or type: role, HTML type,
    autocomplete, or (for editable text only) a secret-looking label."""
    if element.get('secure') is True:
        return True
    if element.get('role') in SECURE_ROLES or element.get('subrole') in SECURE_ROLES:
        return True
    if element.get('html_type') == 'password':
        return True
    if isinstance(element.get('autocomplete'), str) and SECURE_AUTOCOMPLETE.search(element['autocomplete']):
        return True
    # Phase 24 (V1-054): the label is folded first (secret_text), so no invisible, format or combining character
    # and no compatibility form can split or disguise a secret word. Raise-only: it never lowers a field.
    return editable_text(element) and any(
        isinstance(t, str) and SECRET_LABEL.search(secret_text(t)) for t in [element.get(k) for k in LABEL_KEYS])


def sanitize_element(raw):
    """A bounded, redacted copy of one adapter element, or None when malformed.

    Idempotent: sanitizing an already sanitized element returns an equal dict,
    so the host can re-apply it to worker output as defence in depth.
    """
    if not isinstance(raw, dict):
        return None
    role, path = raw.get('role'), raw.get('path')
    if not isinstance(role, str) or not ROLE.fullmatch(role):
        return None
    if not isinstance(path, str) or not 1 <= len(path) <= MAX_PATH or CONTROL.search(path):
        return None
    subrole = raw.get('subrole')
    out = {'role': role, 'subrole': subrole if isinstance(subrole, str) and ROLE.fullmatch(subrole) else None,
           'path': path}
    for key in LABEL_KEYS:
        out[key] = clip(raw.get(key), MAX_LABEL_SOURCE)
    for key in FLAGS:
        out[key] = raw[key] if type(raw.get(key)) is bool else None
    out['frame'] = _frame(raw.get('frame'))
    out['html_type'] = clip(raw.get('html_type'), 32, lower=True)
    out['autocomplete'] = clip(raw.get('autocomplete'), 64, lower=True)
    out['href'] = clip(raw.get('href'), MAX_URL)
    value = raw.get('value')
    out['value'] = None
    if out['role'] == 'AXStaticText' and isinstance(value, str):
        out['value'] = clip(value, MAX_LABEL_SOURCE)  # a label source, hashed in full like the others
    out['secure'] = bool(raw.get('secure') is True or is_secure({**out, 'secure': None}))
    out['value_sha256'] = out['value_truncated'] = None
    if out['secure']:
        out['value'] = None
    elif out['role'] != 'AXStaticText':
        if type(value) is bool or type(value) is int and abs(value) < 1 << 53:
            out['value'] = value
        elif type(value) is float and math.isfinite(value):
            out['value'] = value
        elif isinstance(value, str):
            text = clip(value, MAX_TEXT * 4)
            area = out['role'] == 'AXTextArea'  # a multi-line text area keeps its lines (MAX_AREA_VALUE)
            # an area's shown value normalises line breaks, so its digest (of the original) is kept from the first pass
            kept = raw.get('value_sha256') if raw.get('value_truncated') is True or area else None
            limit = MAX_AREA_VALUE if area else MAX_VALUE
            out['value'] = clip(value, limit, lines=True) if area else text[:MAX_VALUE]
            out['value_truncated'] = raw.get('value_truncated') is True or len(text) > limit
            out['value_sha256'] = kept if isinstance(kept, str) and HEX64.fullmatch(kept) else sha256(text)
    return out


def value_digest(text):
    """The value_sha256 an element reports once its value equals text."""
    return sha256(clip(text, MAX_TEXT * 4))


def sanitize_window(raw):
    if not isinstance(raw, dict):
        return None
    out = {'title': clip(raw.get('title'), MAX_TITLE_SOURCE) or '', 'focused': raw.get('focused') is True,
           'frame': _frame(raw.get('frame'))}
    for key, limit in WINDOW_TEXT.items():
        text = clip(raw.get(key), limit)
        out[key] = text if text else None
    out['title_sha256'] = sha256(out['title'])
    if 'window_number' in raw:
        # Phase 43: a protocol-2 row's facts, bounded scalars only: the window server's number, whether AXWindows
        # listed it (a focused or main window on another Space is not listed), and whether it is the app's main window,
        # minimized, and its subrole. A 1.8.0 row has none of them and stays as it was.
        number, subrole = raw.get('window_number'), raw.get('subrole')
        out['window_number'] = number if type(number) is int and 0 < number < 1 << 32 else None
        out['listed'] = raw.get('listed') is not False
        out['main'] = raw.get('main') is True
        out['minimized'] = raw.get('minimized') is True
        out['subrole'] = subrole if isinstance(subrole, str) and ROLE.fullmatch(subrole) else None
    return out


def sanitize_hit(raw):
    """Phase 45: a bounded copy of a hit answer: the owning pid (or None), the element and at most HIT_ANCESTORS
    ancestors (each through sanitize_element: labels clipped, a secure field's value never kept), whether the element
    lies in the granted window (True, False or None when Accessibility does not say) and whether it is part of a menu."""
    if not isinstance(raw, dict):
        raise AXError('protocol', 'The hit answer is not an object.')
    pid, element, items = raw.get('pid'), raw.get('element'), raw.get('ancestors')
    ancestors = [a for a in map(sanitize_element, items[:HIT_ANCESTORS]) if a is not None] \
        if isinstance(items, list) else []
    return {'pid': pid if type(pid) is int and 0 < pid < 1 << 31 else None,
            'element': sanitize_element(element) if element is not None else None, 'ancestors': ancestors,
            'in_window': raw['in_window'] if type(raw.get('in_window')) is bool else None, 'menu': raw.get('menu') is True}


def sanitize_snapshot(raw, limit=MAX_ELEMENTS):
    """{'window', 'elements', 'truncated', 'dropped'} with at most limit elements."""
    if not isinstance(raw, dict):
        raise AXError('protocol', 'The snapshot is not an object.')
    window = sanitize_window(raw.get('window'))
    if window is None:
        raise AXError('protocol', 'The snapshot has no window.')
    items = raw.get('elements')
    if not isinstance(items, list):
        raise AXError('protocol', 'The snapshot has no element list.')
    prior = raw.get('dropped')
    elements, dropped = [], prior if type(prior) is int and 0 <= prior <= 1 << 20 else 0
    for item in items[:limit]:
        element = sanitize_element(item)
        if element is None:
            dropped += 1
        else:
            elements.append(element)
    return {'window': window, 'elements': elements,
            'truncated': raw.get('truncated') is True or len(items) > limit, 'dropped': dropped}


# ---- worker -----------------------------------------------------------------

class BlockedBackend:
    """The production backend until E-N2: refuses every call, touches no OS API."""

    name = 'blocked'
    available = False
    REASON = ('Accessibility control is not enabled yet: it waits for the Accessibility grant and the E-N2 '
              'check that macOS attributes it to Pebble.')

    def windows(self, pid, start):
        raise AXError('unavailable', self.REASON)

    def tree(self, pid, start, window_ref, limit):
        raise AXError('unavailable', self.REASON)

    def perform(self, pid, start, window_ref, path, action, text):
        raise AXError('unavailable', self.REASON)

    def enhance(self, pid, start):
        raise AXError('unavailable', self.REASON)

    def restore(self, pid, start, changed):
        raise AXError('unavailable', self.REASON)

    def raise_window(self, pid, start, window_ref):
        raise AXError('unavailable', self.REASON)

    def hit(self, pid, start, window_ref, point=None, focused=False):
        raise AXError('unavailable', self.REASON)


# ---- the macOS backend (Phase 42, E-N2) ---------------------------------------
#
# Read-only until E-N2's evidence: windows and trees through the Accessibility client API, never a secure field's
# value, the instance checked before any call. perform refuses. Production keeps BlockedBackend; only
# `--ax-worker --ax-backend mac` (the E-N2 probe below) serves this one.

UTF8 = 0x08000100                 # kCFStringEncodingUTF8
AX_API_DISABLED, AX_INVALID, AX_CANNOT_COMPLETE = -25211, -25202, -25204
AX_NOT_IMPLEMENTED = -25208
MESSAGING_SECONDS = 1.0           # each AX message to the app (AXUIElementSetMessagingTimeout)
TREE_SECONDS = 3.0                # one tree walk, inside the client's REQUEST_SECONDS
MAX_DEPTH = 48
MAX_CHILDREN = 512
LABEL_UNITS = 2048                # UTF-16 units read for a label or title, clipped by the sanitizers afterwards
VALUE_UNITS = 65_536              # UTF-16 units read for a value; a longer one reports value_truncated
ELEMENT_ATTRIBUTES = ('AXRole', 'AXSubrole', 'AXTitle', 'AXDescription', 'AXPlaceholderValue', 'AXHelp', 'AXEnabled',
                      'AXFocused', 'AXSelected', 'AXExpanded', 'AXPosition', 'AXSize', 'AXChildren', 'AXURL')
WINDOW_ATTRIBUTES = ('AXRole', 'AXTitle', 'AXIdentifier', 'AXFocused', 'AXMain', 'AXPosition', 'AXSize',
                     'AXSubrole', 'AXMinimized')  # Phase 43: the window's subrole and minimized state
APP_WINDOW_ATTRIBUTES = ('AXWindows', 'AXFocusedWindow', 'AXMainWindow')  # Phase 43: the app's front window
# 1.8.0's w<index>.<title digest>, or Phase 43's n<window number> (decision 7: a title is only a hint).
WINDOW_REF = re.compile(r'w(\d{1,3})\.([0-9a-f]{16})|n([1-9]\d{0,9})')
# Phase 43 (decision 7): a Chromium or Electron app's page accessibility, on the app element. AXManualAccessibility
# first; AXEnhancedUserInterface (which disturbs window managers) only when the page stays empty without it.
ENHANCE_ATTRIBUTES = ('AXManualAccessibility', 'AXEnhancedUserInterface')
ENHANCE_MANUAL_WAIT = 1.0         # seconds for a filled web area after AXManualAccessibility alone
ENHANCE_WAIT = 4.0                # seconds after AXEnhancedUserInterface (Chromium debounces it about 2 s), every POLL
ENHANCE_POLL = 0.1
ENHANCE_SET_SECONDS = 3.0         # one switch's message: Chromium turns accessibility on before it answers
ENHANCE_SECONDS = 15.0            # the client's deadline for one enhance (two switches and their waits)
WEB_AREA_SCAN = 400               # elements looked at, breadth first, to find the page's web area
WEB_AREA_DEPTH = 16               # levels looked through for it
# Phase 44 (1.9.1, measured on YouTube in Comet): a signed-in person's guide (navigation) came before the results (main)
# and filled all 300 rows, so a page's banner (its search box) and main landmarks are walked first, then the rest.
LANDMARK_ORDER = ('AXLandmarkBanner', 'AXLandmarkMain')
LANDMARK_SCAN = 600               # elements of the page looked at, breadth first, to find them
LANDMARK_DEPTH = 14
NOT_TRUSTED = ('Pebble is not allowed to use Accessibility. Turn Pebble on in System Settings > Privacy & Security > '
               'Accessibility.')


def _ax_error(code):
    if code == AX_API_DISABLED:
        return AXError('not_trusted', NOT_TRUSTED)
    if code == AX_INVALID:
        return AXError('not_found', 'The window or element is gone.')
    if code == AX_CANNOT_COMPLETE:
        return AXError('timeout', 'The app did not answer.')
    return AXError('failed', f'AX error {code}')


class ElementHandle(int):
    """An element reference read as a single attribute value (not a list), so it is never taken for a number."""


class MacAX:
    """ctypes bindings for the macOS Accessibility client API and the CoreFoundation values it returns.

    Every CF object a call copies joins the open session's arena and is released when the session ends; element
    handles handed out are valid only inside it.
    """

    def __init__(self):
        if sys.platform != 'darwin':
            raise AXError('unavailable', 'Accessibility is macOS only.')
        import ctypes
        self.c = ctypes
        try:
            cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            ax = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        except OSError as exc:
            raise AXError('unavailable', type(exc).__name__) from None
        self.ax_lib, self.cf_lib = ax, cf

        class Range(ctypes.Structure):
            _fields_ = [('location', ctypes.c_long), ('length', ctypes.c_long)]
        self.Range = Range
        P, L, U, I32, B = ctypes.c_void_p, ctypes.c_long, ctypes.c_ulong, ctypes.c_int32, ctypes.c_bool

        def sig(fn, result, *args):
            fn.restype, fn.argtypes = result, list(args)
            return fn
        self.is_trusted = sig(ax.AXIsProcessTrusted, B)
        self.create_app = sig(ax.AXUIElementCreateApplication, P, ctypes.c_int)
        self.copy_one = sig(ax.AXUIElementCopyAttributeValue, I32, P, P, ctypes.POINTER(P))
        self.copy_many = sig(ax.AXUIElementCopyMultipleAttributeValues, I32, P, P, ctypes.c_uint32, ctypes.POINTER(P))
        self.is_settable = sig(ax.AXUIElementIsAttributeSettable, I32, P, P, ctypes.POINTER(B))
        self.set_timeout = sig(ax.AXUIElementSetMessagingTimeout, I32, P, ctypes.c_float)
        self.element_type = sig(ax.AXUIElementGetTypeID, U)()
        self.value_type = sig(ax.AXValueGetTypeID, U)()
        self.value_kind = sig(ax.AXValueGetType, ctypes.c_int, P)
        self.value_get = sig(ax.AXValueGetValue, B, P, ctypes.c_int, P)
        self.release = sig(cf.CFRelease, None, P)
        self.type_of = sig(cf.CFGetTypeID, U, P)
        self.string_type = sig(cf.CFStringGetTypeID, U)()
        self.array_type = sig(cf.CFArrayGetTypeID, U)()
        self.boolean_type = sig(cf.CFBooleanGetTypeID, U)()
        self.number_type = sig(cf.CFNumberGetTypeID, U)()
        self.url_type = sig(cf.CFURLGetTypeID, U)()
        self.make_string = sig(cf.CFStringCreateWithCString, P, P, ctypes.c_char_p, ctypes.c_uint32)
        self.length = sig(cf.CFStringGetLength, L, P)
        self.max_size = sig(cf.CFStringGetMaximumSizeForEncoding, L, L, ctypes.c_uint32)
        self.get_string = sig(cf.CFStringGetCString, B, P, ctypes.c_char_p, L, ctypes.c_uint32)
        self.substring = sig(cf.CFStringCreateWithSubstring, P, P, P, Range)
        self.count = sig(cf.CFArrayGetCount, L, P)
        self.item = sig(cf.CFArrayGetValueAtIndex, P, P, L)
        self.make_array = sig(cf.CFArrayCreate, P, P, ctypes.POINTER(P), L, P)
        self.boolean = sig(cf.CFBooleanGetValue, B, P)
        self.number = sig(cf.CFNumberGetValue, B, P, ctypes.c_int, P)
        self.is_float = sig(cf.CFNumberIsFloatType, B, P)
        self.url_string = sig(cf.CFURLGetString, P, P)
        self.callbacks = ctypes.addressof(ctypes.c_byte.in_dll(cf, 'kCFTypeArrayCallBacks'))
        self.names, self.lists, self.arena = {}, {}, None  # attribute CFStrings and CFArrays: made once, kept

    def trusted(self):
        return bool(self.is_trusted())

    def _own(self, ref):
        if ref:
            self.arena.append(ref)
        return ref

    def open(self, pid):
        """Start a session on pid's application element; close() releases everything it copied."""
        self.arena = []
        app = self._own(self.create_app(pid))
        if not app:
            raise AXError('failed', 'The app element could not be made.')
        return app

    def close(self):
        arena, self.arena = self.arena or [], None
        for ref in reversed(arena):
            self.release(ref)

    def _name(self, text):
        ref = self.names.get(text)
        if ref is None:
            ref = self.names[text] = self.make_string(None, text.encode('ascii'), UTF8)
        return ref

    def text(self, ref, units):
        """(text, truncated) for a CFString, reading at most `units` UTF-16 units."""
        length = self.length(ref)
        cut = length > units
        if cut:
            ref = self._own(self.substring(None, ref, self.Range(0, units)))
            length = units
        size = self.max_size(length, UTF8) + 1
        buffer = self.c.create_string_buffer(size)
        if not ref or not self.get_string(ref, buffer, size, UTF8):
            return None, cut
        return buffer.value.decode('utf-8', 'replace'), cut

    def python(self, ref, units=LABEL_UNITS):
        """A CF value as Python: str, bool, int, float, a [a, b] point or size, a list of element handles; else None
        (ranges, rects and the AXError markers of attributes an element lacks)."""
        if not ref:
            return None
        kind = self.type_of(ref)
        if kind == self.string_type:
            return self.text(ref, units)[0]
        if kind == self.boolean_type:
            return bool(self.boolean(ref))
        if kind == self.number_type:
            if self.is_float(ref):
                number = self.c.c_double()
                return number.value if self.number(ref, 13, self.c.byref(number)) else None  # kCFNumberDoubleType
            number = self.c.c_int64()
            return number.value if self.number(ref, 4, self.c.byref(number)) else None    # kCFNumberSInt64Type
        if kind == self.value_type:
            which = self.value_kind(ref)
            if which in (1, 2):  # kAXValueCGPointType, kAXValueCGSizeType
                pair = (self.c.c_double * 2)()
                return [pair[0], pair[1]] if self.value_get(ref, which, pair) else None
            return None
        if kind == self.array_type:
            items = [self.item(ref, i) for i in range(min(self.count(ref), MAX_CHILDREN))]
            return [r for r in items if r and self.type_of(r) == self.element_type]
        if kind == self.element_type:  # Phase 43: one element (an app's focused or main window), valid in the session
            return ElementHandle(ref)
        if kind == self.url_type:
            url = self.url_string(ref)
            return self.text(url, MAX_URL)[0] if url else None
        return None

    def attributes(self, element, names):
        """{name: value} for the attributes element has; None when it cannot answer. Raises not_trusted."""
        self.set_timeout(element, MESSAGING_SECONDS)
        array = self.lists.get(names)
        if array is None:
            refs = (self.c.c_void_p * len(names))(*[self._name(n) for n in names])
            array = self.lists[names] = self.make_array(None, refs, len(names), self.callbacks)
        out = self.c.c_void_p()
        code = self.copy_many(element, array, 0, self.c.byref(out))
        if code == 0 and out.value:
            values = self._own(out.value)
            raw = [self.item(values, i) for i in range(min(self.count(values), len(names)))]
        elif code in (AX_API_DISABLED, AX_INVALID, AX_CANNOT_COMPLETE):
            if code == AX_API_DISABLED:
                raise _ax_error(code)
            return None
        else:  # one call per attribute when the batch call is refused
            raw = []
            for name in names:
                one = self.c.c_void_p()
                got = self.copy_one(element, self._name(name), self.c.byref(one))
                if got == AX_API_DISABLED:
                    raise _ax_error(got)
                raw.append(self._own(one.value) if got == 0 else None)
        facts = {}
        for name, ref in zip(names, raw):
            value = self.python(ref, MAX_URL if name == 'AXURL' else LABEL_UNITS)
            if value is not None:
                facts[name] = value
        return facts

    def value(self, element):
        """(value, truncated) of element's AXValue: text up to VALUE_UNITS, or a scalar; (None, False) otherwise."""
        one = self.c.c_void_p()
        code = self.copy_one(element, self._name('AXValue'), self.c.byref(one))
        if code == AX_API_DISABLED:
            raise _ax_error(code)
        if code != 0 or not one.value:
            return None, False
        ref = self._own(one.value)
        if self.type_of(ref) == self.string_type:
            return self.text(ref, VALUE_UNITS)
        value = self.python(ref)
        return (value, False) if isinstance(value, (bool, int, float)) else (None, False)

    def settable(self, element, name):
        flag = self.c.c_bool()
        return self.is_settable(element, self._name(name), self.c.byref(flag)) == 0 and bool(flag.value)

    def same(self, one, other):
        """Whether two element handles name the same element (CFEqual)."""
        if not hasattr(self, 'equal'):
            fn = self.cf_lib.CFEqual
            fn.restype, fn.argtypes = self.c.c_bool, [self.c.c_void_p, self.c.c_void_p]
            self.equal = fn
        return bool(one) and bool(other) and bool(self.equal(int(one), int(other)))

    def act(self, element, action):
        """AXUIElementPerformAction; the AX error code (0 when it was performed)."""
        if not hasattr(self, 'perform_action'):
            fn = self.ax_lib.AXUIElementPerformAction
            fn.restype, fn.argtypes = self.c.c_int32, [self.c.c_void_p, self.c.c_void_p]
            self.perform_action = fn
        return self.perform_action(element, self._name(action))

    def set(self, element, name, value):
        """AXUIElementSetAttributeValue with a bool or a str; the AX error code."""
        if not hasattr(self, 'set_value'):
            fn = self.ax_lib.AXUIElementSetAttributeValue
            fn.restype, fn.argtypes = self.c.c_int32, [self.c.c_void_p, self.c.c_void_p, self.c.c_void_p]
            self.set_value = fn
            self.true = self.c.c_void_p.in_dll(self.cf_lib, 'kCFBooleanTrue').value
            self.false = self.c.c_void_p.in_dll(self.cf_lib, 'kCFBooleanFalse').value
        if isinstance(value, bool):
            return self.set_value(element, self._name(name), self.true if value else self.false)
        ref = self._own(self.make_string(None, value.encode('utf-8'), UTF8))
        if not ref:
            return AX_CANNOT_COMPLETE
        return self.set_value(element, self._name(name), ref)

    def window_number(self, element):
        """Phase 43 (decision 7): a window element's window-server number through _AXUIElementGetWindow, or None.
        ApplicationServices exports the call without documenting it; window managers have used it for years. It is
        looked up once: when it is missing, every number is None for the worker's life (measured present on this
        macOS, 2026-10-25: a window kept its number through four title changes)."""
        if not hasattr(self, 'get_window'):
            try:
                fn = self.ax_lib._AXUIElementGetWindow
                fn.restype, fn.argtypes = self.c.c_int32, [self.c.c_void_p, self.c.POINTER(self.c.c_uint32)]
            except AttributeError:
                fn = None
            self.get_window = fn
        if self.get_window is None or not element:
            return None
        out = self.c.c_uint32(0)
        if self.get_window(int(element), self.c.byref(out)) != 0:
            return None
        return out.value or None

    @property
    def numbers(self):
        """Whether this macOS has _AXUIElementGetWindow (hello reports it)."""
        self.window_number(None)
        return self.get_window is not None

    # ---- Phase 45 (layer 4): hit-testing before a pixel act ----

    def system(self):
        """The system-wide element (it joins the open session's arena)."""
        if not hasattr(self, 'create_system'):
            fn = self.ax_lib.AXUIElementCreateSystemWide
            fn.restype, fn.argtypes = self.c.c_void_p, []
            self.create_system = fn
        ref = self._own(self.create_system())
        if not ref:
            raise AXError('failed', 'The system-wide element could not be made.')
        return ref

    def element_at(self, system, x, y):
        """AXUIElementCopyElementAtPosition on the system-wide element: the element of whichever window is topmost at
        the global point, of any app (with an app element the test would stay inside that app); None when nothing
        answers. The element joins the session's arena."""
        if not hasattr(self, 'at_position'):
            fn = self.ax_lib.AXUIElementCopyElementAtPosition
            fn.restype = self.c.c_int32
            fn.argtypes = [self.c.c_void_p, self.c.c_float, self.c.c_float, self.c.POINTER(self.c.c_void_p)]
            self.at_position = fn
        self.set_timeout(system, MESSAGING_SECONDS)
        out = self.c.c_void_p()
        code = self.at_position(system, float(x), float(y), self.c.byref(out))
        if code == AX_API_DISABLED:
            raise _ax_error(code)
        return self._own(out.value) if code == 0 and out.value else None

    def pid_of(self, element):
        """AXUIElementGetPid: the process an element belongs to, or None."""
        if not hasattr(self, 'get_pid'):
            fn = self.ax_lib.AXUIElementGetPid
            fn.restype, fn.argtypes = self.c.c_int32, [self.c.c_void_p, self.c.POINTER(self.c.c_int)]
            self.get_pid = fn
        pid = self.c.c_int(0)
        return pid.value if element and self.get_pid(int(element), self.c.byref(pid)) == 0 and pid.value > 0 else None


def _meets(frame, view):
    """Whether an element's frame [x, y, w, h] shares any area with the window's (an unknown frame counts as in view)."""
    if frame is None:
        return True
    x, y, w, h = frame
    vx, vy, vw, vh = view
    return x < vx + vw and vx < x + w and y < vy + vh and vy < y + h


class MacBackend:
    """The real backend (Phase 42): the Accessibility client API through MacAX.

    Read-only until E-N2's evidence (perform refuses). The instance is checked before any AX call; a secure field's
    value is never requested (role, subrole and labels decide first, as is_secure).
    """

    name = 'mac'

    def __init__(self, ax=None, *, start_of=None, clock=time.monotonic, sleep=time.sleep):
        self._ax, self.start_of, self.clock, self.sleep = ax, start_of, clock, sleep

    @property
    def ax(self):
        if self._ax is None:
            self._ax = MacAX()
        return self._ax

    @property
    def available(self):
        try:
            return self.ax.trusted()
        except AXError:
            return False

    def _open(self, pid, start):
        start_of = self.start_of
        if start_of is None:
            from .native_apps import process_start as start_of
        now = start_of(pid)
        if now is None or abs(float(now) - start) > 1e-6:
            raise AXError('instance_changed')
        if not self.ax.trusted():
            raise AXError('not_trusted', NOT_TRUSTED)
        return self.ax.open(pid)

    @property
    def window_numbers(self):
        """Phase 43: whether windows are named by number here (the call exists and PEBBLE_WINDOW_NUMBERS is not 0)."""
        try:
            return os.environ.get('PEBBLE_WINDOW_NUMBERS', '1') != '0' and getattr(self.ax, 'numbers', False) is True
        except AXError:
            return False

    def _windows(self, app):
        facts = self.ax.attributes(app, APP_WINDOW_ATTRIBUTES)
        if facts is None:
            raise AXError('timeout', 'The app did not answer.')
        # Phase 43: the app's own focused (else main) window is its front one, also while the app is in the
        # background, where no window says AXFocused (1.8.0 measured Comet with several windows and none focused).
        focused_ref, main_ref = facts.get('AXFocusedWindow'), facts.get('AXMainWindow')
        front = focused_ref or main_ref
        same = getattr(self.ax, 'same', None)
        numbers = callable(same) and self.window_numbers
        candidates = [(ref, True) for ref in facts.get('AXWindows', [])[:MAX_WINDOWS]]
        if numbers:
            # Phase 43 (decision 2): AXWindows leaves out windows on other Spaces. The app's focused and main windows
            # are admitted anyway, marked unlisted, and only with a number to name them (else 1.8.0's rows alone).
            for extra in (focused_ref, main_ref):
                if extra is not None and not any(same(extra, ref) for ref, _ in candidates):
                    candidates.append((extra, False))
        rows = []
        for index, (ref, listed) in enumerate(candidates[:MAX_WINDOWS]):
            info = self.ax.attributes(ref, WINDOW_ATTRIBUTES) or {}
            title = info.get('AXTitle') if isinstance(info.get('AXTitle'), str) else ''
            focused = info.get('AXFocused') if type(info.get('AXFocused')) is bool else info.get('AXMain') is True
            if front is not None and callable(same):
                focused = bool(same(ref, front))
            identifier = info.get('AXIdentifier')
            row = {'window_ref': f'w{index}.{sha256(clip(title, MAX_TITLE_SOURCE) or "")[:16]}', 'title': title,
                   'ax_identifier': identifier if isinstance(identifier, str) else None, 'focused': focused,
                   'frame': self._frame(info)}
            if numbers:
                number = self.ax.window_number(ref)
                if number is None and not listed:
                    continue
                subrole = info.get('AXSubrole')
                row.update({'window_ref': f'n{number}' if number is not None else row['window_ref'],
                            'window_number': number, 'listed': listed,
                            'main': main_ref is not None and bool(same(ref, main_ref)),
                            'minimized': info.get('AXMinimized') is True,
                            'subrole': subrole if isinstance(subrole, str) and ROLE.fullmatch(subrole) else None})
            rows.append((row, ref))
        return rows

    @staticmethod
    def _frame(info):
        position, size = info.get('AXPosition'), info.get('AXSize')
        if isinstance(position, list) and isinstance(size, list):
            return [position[0], position[1], size[0], size[1]]
        return None

    def windows(self, pid, start):
        app = self._open(pid, start)
        try:
            return [row for row, _ in self._windows(app)]
        finally:
            self.ax.close()

    def _find(self, app, window_ref):
        """(row, window) for window_ref. n<number>: the one window with that number. 1.8.0's form: by index and title
        digest, else by title digest alone when only one window has it; AX lists the active Space's windows only, so
        a window on another desktop is not found that way."""
        match = WINDOW_REF.fullmatch(window_ref)
        if not match:
            raise AXError('invalid', 'window_ref')
        rows = self._windows(app)
        if match.group(3):
            found = [r for r in rows if r[0]['window_ref'] == window_ref]
            if len(found) != 1:
                raise AXError('not_found', "The window is closed, or on another desktop and no longer the app's front "
                                           'window.' if not found else 'Several windows report this number.')
            return found[0]
        same = [r for r in rows if r[0]['window_ref'].endswith('.' + match.group(2))]
        exact = [r for r in same if r[0]['window_ref'] == window_ref]
        if not same:
            raise AXError('not_found', 'The window is closed, minimized or on another desktop.')
        if not exact and len(same) > 1:
            raise AXError('not_found', 'Several windows share this title.')
        return (exact or same)[0]

    pages_first = True  # Phase 43: tree takes page_first (a browser's page walked before its own controls)

    def tree(self, pid, start, window_ref, limit, page_first=False):
        if not WINDOW_REF.fullmatch(window_ref):
            raise AXError('invalid', 'window_ref')
        app = self._open(pid, start)
        try:
            row, window = self._find(app, window_ref)
            elements, truncated = self._walk(window, limit, page_first=page_first)
            return {'window': row, 'elements': elements, 'truncated': truncated}
        finally:
            self.ax.close()

    def _page(self, top):
        """(element, path, depth) of the window's first filled AXWebArea, breadth first within WEB_AREA_SCAN elements
        and WEB_AREA_DEPTH levels, or None."""
        queue, seen = [(ref, f'/{i}', 1) for i, ref in enumerate(top)], 0
        while queue and seen < WEB_AREA_SCAN:
            ref, path, depth = queue.pop(0)
            seen += 1
            info = self.ax.attributes(ref, ('AXRole', 'AXChildren')) or {}
            children = info.get('AXChildren', [])
            if info.get('AXRole') == 'AXWebArea' and children:
                return ref, path, depth
            if depth < WEB_AREA_DEPTH:
                queue.extend((child, f'{path}/{i}', depth + 1) for i, child in enumerate(children))
        return None

    def _landmarks(self, page):
        """[(element, path, depth)] of the page's first banner and first main landmark, in LANDMARK_ORDER, found
        breadth first within LANDMARK_SCAN elements and LANDMARK_DEPTH levels below the web area (none on a page
        without landmarks)."""
        ref, path, depth = page
        found, seen = {}, 0
        top = (self.ax.attributes(ref, ('AXChildren',)) or {}).get('AXChildren', [])
        queue = [(child, f'{path}/{i}', depth + 1) for i, child in enumerate(top)]
        while queue and seen < LANDMARK_SCAN and len(found) < len(LANDMARK_ORDER):
            node, node_path, node_depth = queue.pop(0)
            seen += 1
            info = self.ax.attributes(node, ('AXSubrole', 'AXChildren')) or {}
            subrole = info.get('AXSubrole')
            if subrole in LANDMARK_ORDER and subrole not in found:
                found[subrole] = (node, node_path, node_depth)
                continue  # a landmark inside another is walked with it
            if node_depth - depth < LANDMARK_DEPTH:
                queue.extend((child, f'{node_path}/{i}', node_depth + 1)
                             for i, child in enumerate(info.get('AXChildren', [])))
        return [found[s] for s in LANDMARK_ORDER if s in found]

    def _walk(self, window, limit, page_first=False):
        """Depth first, in reading order, at most limit elements, MAX_DEPTH levels and TREE_SECONDS. Phase 43: with
        page_first (a Chromium-family app, from the host), a filled web area is walked first and the browser's own
        controls after it, so toolbars and tabs (166 of 300 rows in Comet, measured) do not crowd out the page.

        Phase 45 (F132, audit round R4): on a long page the first limit elements in reading order were always the top
        of the page, whatever was scrolled into view, so a link further down ("Pebble Time" on Wikipedia's Pebble
        article) was never seen and scrolling could not help. Inside that web area, a part whose frame lies wholly
        outside the window is walked after everything in view (still in reading order), so what is on screen comes
        first and scrolling brings more of the page in. App windows keep the plain reading order."""
        deadline = self.clock() + TREE_SECONDS
        top = self.ax.attributes(window, ('AXChildren', 'AXPosition', 'AXSize'))
        if top is None:
            raise AXError('timeout', 'The window did not answer.')
        view = self._frame(top)
        stack = [(ref, f'/{i}', 1) for i, ref in reversed(list(enumerate(top.get('AXChildren', []))))]
        page = self._page(top.get('AXChildren', [])) if page_first else None
        if page is not None:
            stack.append(page)  # popped first; met again in the window's own order, it is skipped
        page_seen, marks, first, done = False, [], set(), set()
        elements, truncated, later, second = [], False, [], False
        while True:
            while stack:
                if len(elements) >= limit or self.clock() > deadline:
                    truncated = True
                    break
                ref, path, depth, *forced = stack.pop()
                if forced:
                    pass  # an out-of-view part, met once already: walked now
                elif page is not None and path == page[1]:
                    if page_seen:
                        continue
                    page_seen = True
                    marks = self._landmarks(page)  # Phase 44: walked right after the web area's own row
                elif path in first:
                    first.discard(path)
                    done.add(path)  # a landmark's own visit, ahead of the page's order
                elif path in done:
                    continue  # a landmark already walked, met again in the page's own order
                info = self.ax.attributes(ref, ELEMENT_ATTRIBUTES)
                if info is None:
                    continue  # an element that cannot answer is left out
                if not second and page is not None and view is not None and path.startswith(page[1] + '/') \
                        and not _meets(self._frame(info), view):
                    later.append((ref, path, depth))  # out of view: after everything in view
                    continue
                element = self._element(ref, path, info)
                if element is not None:
                    elements.append(element)
                children = info.get('AXChildren', [])
                if children and depth >= MAX_DEPTH:
                    truncated = True
                    continue
                stack.extend((child, f'{path}/{i}', depth + 1) for i, child in reversed(list(enumerate(children))))
                if marks:
                    stack.extend(reversed(marks))  # banner first, then main, then the rest of the page
                    first.update(mark[1] for mark in marks)
                    marks = []
            if truncated or second or not later:
                break
            second = True
            stack = [(ref, path, depth, True) for ref, path, depth in reversed(later)]
        return elements, truncated

    def _element(self, ref, path, info):
        role = info.get('AXRole')
        if not isinstance(role, str) or not ROLE.fullmatch(role):
            return None
        subrole = info.get('AXSubrole')
        element = {'role': role, 'subrole': subrole if isinstance(subrole, str) else None, 'path': path,
                   'title': info.get('AXTitle'), 'description': info.get('AXDescription'),
                   'placeholder': info.get('AXPlaceholderValue'), 'help': info.get('AXHelp'),
                   'frame': self._frame(info), 'href': info.get('AXURL')}
        for key, name in (('enabled', 'AXEnabled'), ('focused', 'AXFocused'), ('selected', 'AXSelected'),
                          ('expanded', 'AXExpanded')):
            if type(info.get(name)) is bool:
                element[key] = info[name]
        if role in TEXT_ROLES:
            element['multiline'] = role == 'AXTextArea'
            element['editable'] = self.ax.settable(ref, 'AXValue')
        if is_secure(element):
            element['secure'] = True  # its value is never requested
            return element
        value, truncated = self.ax.value(ref)
        if isinstance(value, str):
            element['value'] = value
            if truncated:
                element['value_truncated'], element['value_sha256'] = True, value_digest(value)
        elif value is not None:
            element['value'] = value
        return element

    checks_expect = True  # perform takes the host's expectation of the element (role, label digest)

    def perform(self, pid, start, window_ref, path, action, text, expect=None):
        """One act on the element at path (child indices from the window), once. The element must still have the
        role and label the host observed; a secure field is never acted on; nothing is retried."""
        if not WINDOW_REF.fullmatch(window_ref):
            raise AXError('invalid', 'window_ref')
        app = self._open(pid, start)
        try:
            _, window = self._find(app, window_ref)
            if path == WINDOW_PATH:
                if action != 'focus':
                    raise AXError('unsupported', 'Only focus acts on the window itself.')
                self._done(self.ax.act(window, 'AXRaise'), 'focus')
                return
            element = self._resolve(window, path)
            info = self.ax.attributes(element, ELEMENT_ATTRIBUTES)
            facts = self._element(element, path, info) if info is not None else None
            clean = sanitize_element(facts) if facts is not None else None
            if clean is None:
                raise AXError('not_found', 'The element is gone.')
            if expect and (clean['role'] != expect.get('role') or label_sha256(clean) != expect.get('label_sha256')):
                raise AXError('not_found', 'The element changed since Pebble looked.')
            if clean['secure']:
                raise AXError('unsupported', 'Pebble never acts on a password, code or card field.')
            self._dispatch(element, clean, action, text)
        finally:
            self.ax.close()

    def _resolve(self, window, path):
        steps = path.split('/')[1:] if path.startswith('/') else []
        if not steps or len(steps) > MAX_DEPTH or not all(s.isdigit() and len(s) <= 4 for s in steps):
            raise AXError('invalid', 'path')
        node = window
        for step in steps:
            children = (self.ax.attributes(node, ('AXChildren',)) or {}).get('AXChildren', [])
            if int(step) >= len(children):
                raise AXError('not_found', 'The element is gone.')
            node = children[int(step)]
        return node

    def _dispatch(self, element, clean, action, text):
        role = clean['role']
        if action == 'focus':
            code = self.ax.set(element, 'AXFocused', True)
        elif action == 'scroll':
            code = self.ax.act(element, 'AXScrollToVisible')
        elif action in ('expand', 'collapse'):
            want = action == 'expand'
            if clean.get('expanded') is want:
                return  # already so; nothing to do
            code = self.ax.set(element, 'AXExpanded', want) if self.ax.settable(element, 'AXExpanded') \
                else self.ax.act(element, 'AXPress')
        elif action == 'select':
            code = self.ax.set(element, 'AXSelected', True) if self.ax.settable(element, 'AXSelected') \
                else self.ax.act(element, 'AXPress')
        elif action in ('toggle', 'press'):
            code = self.ax.act(element, 'AXPress')
        elif action == 'type':
            if role not in TEXT_ROLES or not self.ax.settable(element, 'AXValue'):
                raise AXError('unsupported', 'This element does not take typed text.')
            code = self.ax.set(element, 'AXValue', text or '')  # the text replaces the value
        elif action == 'enter':
            code = self.ax.act(element, 'AXConfirm')
        else:
            raise AXError('invalid', 'action')
        self._done(code, action)

    @staticmethod
    def _done(code, what):
        if code == 0:
            return
        if code == AX_CANNOT_COMPLETE:
            raise AXError('uncertain', f'{what}: the app did not answer, so it may or may not have happened.')
        if code in (-25206, -25205):  # kAXErrorActionUnsupported, kAXErrorAttributeUnsupported
            raise AXError('unsupported', f'{what} is not supported by this element.')
        raise _ax_error(code)

    # ---- Phase 43 (1.9.0): page accessibility and bringing a window forward ----

    def _web_area_filled(self, app):
        """Whether the app's focused (else main, else first) window holds an AXWebArea with content. A web area with
        no children is what a Chromium page shows before its accessibility is on; another one (a page rather than a
        side panel) is looked for, breadth first, within WEB_AREA_SCAN elements."""
        facts = self.ax.attributes(app, APP_WINDOW_ATTRIBUTES) or {}
        window = facts.get('AXFocusedWindow') or facts.get('AXMainWindow') or next(iter(facts.get('AXWindows', [])),
                                                                                    None)
        queue, seen = [window] if window is not None else [], 0
        while queue and seen < WEB_AREA_SCAN:
            ref = queue.pop(0)
            seen += 1
            info = self.ax.attributes(ref, ('AXRole', 'AXChildren')) or {}
            children = info.get('AXChildren', [])
            if info.get('AXRole') == 'AXWebArea':
                if children:
                    return True
                continue
            queue.extend(children)
        return False

    def _with_app(self, pid, start, fn):
        app = self._open(pid, start)
        try:
            return fn(app)
        finally:
            self.ax.close()

    def enhance(self, pid, start):
        """Turn on a Chromium or Electron app's page accessibility (decision 7). The originals are read first; one
        that cannot be read is None (unknown), never invented. AXManualAccessibility is set where it is not already
        on and given ENHANCE_MANUAL_WAIT to fill the page; only then AXEnhancedUserInterface, given ENHANCE_WAIT of
        its own. Each check opens a fresh AX session, so polling never piles up copied objects."""
        began = self.clock()
        read, filled = self._with_app(pid, start, lambda app: (self.ax.attributes(app, ENHANCE_ATTRIBUTES) or {},
                                                               self._web_area_filled(app)))
        original = {name: read[name] if type(read.get(name)) is bool else None for name in ENHANCE_ATTRIBUTES}
        changed, refused, codes = [], [], {}
        for name, wait in (('AXManualAccessibility', ENHANCE_MANUAL_WAIT), ('AXEnhancedUserInterface', ENHANCE_WAIT)):
            if filled or original[name] is True:
                continue
            code, after = self._with_app(pid, start, lambda app: self._switch(app, name, True))
            codes[name] = code
            # Measured on Comet (2026-10-25): switching on answers -25204 (cannot complete) and switching off -25208,
            # and both happen; only the value read back, or a page that fills, tells. A switch that reads back false
            # was refused (measured: AppKit apps), so there is nothing to wait for.
            if after is False or code != 0 and after is None and code not in (AX_CANNOT_COMPLETE, AX_NOT_IMPLEMENTED):
                refused.append(name)
                continue
            changed.append(name)
            step = self.clock()
            while not filled and self.clock() - step < wait:
                self.sleep(ENHANCE_POLL)
                filled = self._with_app(pid, start, self._web_area_filled)
        return {'original': original, 'changed': changed, 'refused': refused, 'filled': filled, 'codes': codes,
                'ms': round((self.clock() - began) * 1000)}

    def _switch(self, app, name, value):
        """(the set's AX code, the value read back or None) for one page-accessibility switch, with a longer message."""
        self.ax.set_timeout(app, ENHANCE_SET_SECONDS)
        code = self.ax.set(app, name, value)
        after = (self.ax.attributes(app, (name,)) or {}).get(name)
        return code, after if type(after) is bool else None

    def restore(self, pid, start, changed):
        """Set back only what enhance changed, to the originals it read. Restored means the value read back equals the
        original; an attribute whose original was unknown is left as it is (never invented), and so is one that does
        not read back as the original."""
        def run(app):
            restored, left = [], []
            for name, value in changed.items():
                ok = type(value) is bool and self._switch(app, name, value)[1] is value
                (restored if ok else left).append(name)
            return {'restored': restored, 'left': left}
        return self._with_app(pid, start, run)

    def raise_window(self, pid, start, window_ref):
        """The person approved bringing the window forward: un-minimize it, raise it in its app and make the app
        frontmost through Accessibility (AXFrontmost); macOS may switch to the window's desktop. A frontmost the app
        refuses is reported, so the host can activate it another way."""
        if not WINDOW_REF.fullmatch(window_ref):
            raise AXError('invalid', 'window_ref')

        def run(app):
            row, window = self._find(app, window_ref)
            if row.get('minimized'):
                self._done(self.ax.set(window, 'AXMinimized', False), 'un-minimize')
            self._done(self.ax.act(window, 'AXRaise'), 'raise')
            return {'raised': True, 'frontmost': self.ax.set(app, 'AXFrontmost', True) == 0}
        return self._with_app(pid, start, run)

    def hit(self, pid, start, window_ref, point=None, focused=False):
        """Phase 45 (layer 4): what Accessibility says is under a global point, asked of the system-wide element so a
        window of another app or a system dialog on top answers as itself; or (focused) what holds the keyboard. The
        answer: the element's process, the element and up to HIT_ANCESTORS ancestors (roles, labels, frames; a secure
        field's value is never read, a static text's words are), whether it lies in the granted window, and whether it
        is part of a menu. Read-only; the granted instance and window are checked first."""
        if not WINDOW_REF.fullmatch(window_ref):
            raise AXError('invalid', 'window_ref')

        def run(app):
            _, window = self._find(app, window_ref)
            system = self.ax.system()
            if focused:
                element = (self.ax.attributes(system, ('AXFocusedUIElement',)) or {}).get('AXFocusedUIElement')
                if element is None:
                    # Phase 45 (E-C4, measured 2026-10-26 06:30, `.runtime/phase45-dev/l4/ec4-diagnosis.md`): the
                    # system-wide element answered "cannot complete" for the keyboard's holder whatever app was in
                    # front, so every typing act was refused. The granted app holds the keyboard when it is the active
                    # app (its own frontmost flag); then its own focused element is the answer.
                    own = self.ax.attributes(app, ('AXFrontmost', 'AXFocusedUIElement')) or {}
                    if own.get('AXFrontmost') is True:
                        element = own.get('AXFocusedUIElement')
            else:
                element = self.ax.element_at(system, point[0], point[1])
            if element is None:
                return {'pid': None, 'element': None, 'ancestors': [], 'in_window': None, 'menu': False}
            chain, node = [], element
            for depth in range(HIT_ANCESTORS + 1):
                info = self.ax.attributes(node, HIT_ATTRIBUTES)
                facts = self._hit_element(node, info, depth) if info is not None else None
                if facts is None:
                    break
                chain.append((facts, info, node))
                parent = info.get('AXParent')
                if facts['role'] in ('AXWindow', 'AXApplication') or not isinstance(parent, ElementHandle):
                    break
                node = parent
            if not chain:
                return {'pid': self.ax.pid_of(element), 'element': None, 'ancestors': [], 'in_window': None,
                        'menu': False}
            facts, info, node = chain[0]
            holder = node if facts['role'] == 'AXWindow' else info.get('AXWindow')
            return {'pid': self.ax.pid_of(element), 'element': facts, 'ancestors': [f for f, _, _ in chain[1:]],
                    'in_window': bool(self.ax.same(holder, window)) if holder is not None else None,
                    'menu': any(f['role'] in MENU_ROLES for f, _, _ in chain)}
        return self._with_app(pid, start, run)

    def _hit_element(self, ref, info, depth):
        """One element of a hit, in the shape sanitize_element takes (its path names its place: hit, hit^1, ...)."""
        role = info.get('AXRole')
        if not isinstance(role, str) or not ROLE.fullmatch(role):
            return None
        subrole = info.get('AXSubrole')
        element = {'role': role, 'subrole': subrole if isinstance(subrole, str) else None,
                   'path': 'hit' if depth == 0 else f'hit^{depth}', 'title': info.get('AXTitle'),
                   'description': info.get('AXDescription'), 'placeholder': info.get('AXPlaceholderValue'),
                   'help': info.get('AXHelp'), 'frame': self._frame(info), 'href': info.get('AXURL')}
        for key, name in (('enabled', 'AXEnabled'), ('focused', 'AXFocused'), ('selected', 'AXSelected'),
                          ('expanded', 'AXExpanded')):
            if type(info.get(name)) is bool:
                element[key] = info[name]
        if role in TEXT_ROLES:
            element['multiline'] = role == 'AXTextArea'
            element['editable'] = self.ax.settable(ref, 'AXValue')
        if is_secure(element):
            element['secure'] = True  # its value is never requested
        elif role == 'AXStaticText':
            value, _ = self.ax.value(ref)
            if isinstance(value, str):
                element['value'] = value  # a label source (the words on a control without a title)
        return element


def responsible_app(pid):
    """The process macOS holds responsible for pid (TCC checks a permission against it): {'pid', 'app'} with the app
    path from its bundle down (e.g. 'Pebble.app/Contents/MacOS/pebble'), or None where this cannot be read."""
    try:
        import ctypes
        fn = ctypes.CDLL(None).responsibility_get_pid_responsible_for_pid
    except (OSError, AttributeError):
        return None
    fn.restype, fn.argtypes = ctypes.c_int, [ctypes.c_int]
    owner = fn(pid)
    if owner <= 0:
        return None
    from .native_apps import pid_path
    path = pid_path(owner) or ''
    index = path.rfind('.app/')
    return {'pid': owner, 'app': path[path.rfind('/', 0, index) + 1:] if index > 0 else None}


def probe(bundle_id, title_contains, *, client=None, lister=None, clock=time.monotonic):
    """E-N2, a diagnostic only: in an `--ax-worker` this backend spawns (so a grandchild of Pebble.app, as production's
    would be), is the macOS backend trusted, and does it read the window of a document the harness owns?

    Reached only through the shell's PEBBLE_SHELL_DIAG `ax_probe` command (the backend route diag_ax_probe, never in
    the webviews' ALLOWED). Returns content-free facts: trust, counts, digests and refusal codes; the harness compares
    the text area's value_sha256 with value_digest of the text it wrote.
    """
    if not isinstance(bundle_id, str) or not REF.fullmatch(bundle_id) or not isinstance(title_contains, str) \
            or not 8 <= len(title_contains) <= 64 or CONTROL.search(title_contains):
        raise AXError('invalid', 'probe')
    client = client or WorkerClient(worker_argv() + ['--ax-backend', 'mac'], timeout=10)
    facts, began = {'bundle_id': bundle_id}, clock()
    try:
        facts['hello'] = client.call('hello', {})
        worker = getattr(client, 'proc', None)
        if worker is not None:  # the attribution itself: which app macOS holds responsible for the worker
            owner = responsible_app(worker.pid)
            facts['responsible'] = owner and {**owner, 'is_this_backends_parent': owner['pid'] == os.getppid()}
        if lister is None:
            from .native_apps import instances as lister
        rows = lister(bundle_id)
        facts['running'] = len(rows)
        if len(rows) == 1:
            pid, start = rows[0]['pid'], float(rows[0]['start'])
            windows = client.windows(pid, start)
            matched = [w for w in windows if title_contains in (w.get('title') or '')]
            facts['windows'], facts['matched'] = len(windows), len(matched)
            if len(matched) == 1:
                facts['title_sha256'] = matched[0]['title_sha256']
                snap = client.tree(pid, start, matched[0]['window_ref'], MAX_ELEMENTS)
                elements = snap.get('elements') or []
                roles = {}
                for element in elements:
                    roles[element['role']] = roles.get(element['role'], 0) + 1
                facts.update({'elements': len(elements), 'roles': dict(sorted(roles.items())),
                              'truncated': snap.get('truncated'), 'dropped': snap.get('dropped'),
                              'text_areas': [{'value_sha256': e.get('value_sha256'),
                                              'value_truncated': e.get('value_truncated'), 'secure': e.get('secure')}
                                             for e in elements if e['role'] == 'AXTextArea'][:4]})
    except AXError as exc:
        facts['error'] = {'code': exc.code, 'detail': exc.detail}
    finally:
        client.close()
    facts['ms'] = round((clock() - began) * 1000)
    return facts


def _instance_args(args):
    pid, start = args.get('pid'), args.get('start')
    if type(pid) is not int or not 0 < pid < 1 << 31:
        raise AXError('invalid', 'pid')
    if type(start) not in (int, float) or not math.isfinite(start) or start <= 0:
        raise AXError('invalid', 'start')
    return pid, float(start)


def _ref(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not REF.fullmatch(value):
        raise AXError('invalid', key)
    return value


def _phase43_op(backend, op, args):
    """enhance, restore and raise (Phase 43), each answered with bounded facts only."""
    pid, start = _instance_args(args)
    fn = getattr(backend, {'enhance': 'enhance', 'restore': 'restore', 'raise': 'raise_window'}[op], None)
    if not callable(fn):
        raise AXError('unsupported', op)
    listed = lambda raw, key: [n for n in ENHANCE_ATTRIBUTES if isinstance(raw.get(key), list) and n in raw[key]]
    if op == 'enhance':
        raw = fn(pid, start)
        raw = raw if isinstance(raw, dict) else {}
        original = raw.get('original') if isinstance(raw.get('original'), dict) else {}
        ms = raw.get('ms')
        codes = raw.get('codes') if isinstance(raw.get('codes'), dict) else {}
        return {'original': {n: original.get(n) if type(original.get(n)) is bool else None for n in ENHANCE_ATTRIBUTES},
                'changed': listed(raw, 'changed'), 'refused': listed(raw, 'refused'),
                'filled': raw.get('filled') is True, 'ms': ms if type(ms) is int and 0 <= ms <= 60_000 else None,
                'codes': {n: codes[n] for n in ENHANCE_ATTRIBUTES if type(codes.get(n)) is int and -40000 < codes[n] < 1}}
    if op == 'restore':
        changed = args.get('changed')
        if not isinstance(changed, dict) or not 1 <= len(changed) <= len(ENHANCE_ATTRIBUTES) \
                or any(k not in ENHANCE_ATTRIBUTES or not (v is None or type(v) is bool) for k, v in changed.items()):
            raise AXError('invalid', 'changed')
        raw = fn(pid, start, changed)
        raw = raw if isinstance(raw, dict) else {}
        return {key: listed(raw, key) for key in ('restored', 'left')}
    raw = fn(pid, start, _ref(args, 'window_ref'))
    return {'raised': True, 'frontmost': isinstance(raw, dict) and raw.get('frontmost') is True}


def _phase45_hit(backend, args):
    """hit (Phase 45, layer 4): exactly one of point (two finite global coordinates) and focused (true)."""
    pid, start = _instance_args(args)
    fn = getattr(backend, 'hit', None)
    if not callable(fn):
        raise AXError('unsupported', 'hit')
    point, focused = args.get('point'), args.get('focused', False)
    if type(focused) is not bool or (point is None) != focused:
        raise AXError('invalid', 'Give point or focused.')
    if point is not None and not (isinstance(point, list) and len(point) == 2 and all(
            type(v) in (int, float) and math.isfinite(v) and abs(v) < 1e6 for v in point)):
        raise AXError('invalid', 'point')
    return sanitize_hit(fn(pid, start, _ref(args, 'window_ref'), point=tuple(point) if point else None,
                           focused=focused))


def handle(request, backend):
    """One decoded request -> one response object. Never raises."""
    rid = request.get('id') if isinstance(request, dict) else None
    rid = rid if type(rid) is int and 0 <= rid < 1 << 53 else None
    try:
        if rid is None or set(request) != {'id', 'op', 'args'} or request['op'] not in OPS + EXTRA_OPS \
                or not isinstance(request['args'], dict):
            raise AXError('invalid', 'request')
        op, args = request['op'], request['args']
        if op == 'hello':
            # Phase 43: the ops this worker serves and whether it names windows by number (capability discovery).
            result = {'protocol': PROTOCOL, 'backend': getattr(backend, 'name', 'unknown'),
                      'available': getattr(backend, 'available', True) is True, 'ops': list(OPS),
                      'window_numbers': getattr(backend, 'window_numbers', False) is True}
        elif op in ('enhance', 'restore', 'raise'):
            result = _phase43_op(backend, op, args)
        elif op == 'hit':
            result = _phase45_hit(backend, args)
        elif op == 'windows':
            pid, start = _instance_args(args)
            rows = backend.windows(pid, start)
            if not isinstance(rows, list):
                raise AXError('protocol', 'windows')
            result = [w for w in (sanitize_window(r) for r in rows[:MAX_WINDOWS]) if w is not None]
        elif op == 'tree':
            pid, start = _instance_args(args)
            limit = args.get('limit')
            if type(limit) is not int or not 1 <= limit <= MAX_ELEMENTS:
                raise AXError('invalid', 'limit')
            page_first = args.get('page_first', False)  # Phase 43: optional; only a backend that declares it gets it
            if type(page_first) is not bool:
                raise AXError('invalid', 'page_first')
            kw = {'page_first': True} if page_first and getattr(backend, 'pages_first', False) is True else {}
            result = sanitize_snapshot(backend.tree(pid, start, _ref(args, 'window_ref'), limit, **kw), limit)
        else:
            pid, start = _instance_args(args)
            path, action, text = args.get('path'), args.get('action'), args.get('text')
            if not isinstance(path, str) or not 1 <= len(path) <= MAX_PATH or CONTROL.search(path):
                raise AXError('invalid', 'path')
            if action not in ACTIONS:
                raise AXError('invalid', 'action')
            if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT or '\x00' in text):
                raise AXError('invalid', 'text')
            expect = args.get('expect')  # Phase 42: the host's view of the element, re-checked before acting
            if expect is not None and (not isinstance(expect, dict) or set(expect) != {'role', 'label_sha256'}
                                       or not isinstance(expect['role'], str) or not ROLE.fullmatch(expect['role'])
                                       or not isinstance(expect['label_sha256'], str)
                                       or not HEX64.fullmatch(expect['label_sha256'])):
                raise AXError('invalid', 'expect')
            checks = expect is not None and getattr(backend, 'checks_expect', False) is True
            backend.perform(pid, start, _ref(args, 'window_ref'), path, action, text,
                            **({'expect': expect} if checks else {}))
            result = {'performed': True}
        return {'id': rid, 'ok': True, 'result': result}
    except AXError as exc:
        return {'id': rid, 'ok': False, 'error': exc.code, 'detail': exc.detail}
    except Exception as exc:  # backend bugs answer content-free; the worker keeps serving
        return {'id': rid, 'ok': False, 'error': 'failed', 'detail': type(exc).__name__}


def _encode(response):
    data = json.dumps(response, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
    if len(data) > MAX_RESPONSE_BYTES:
        data = json.dumps({'id': response.get('id'), 'ok': False, 'error': 'protocol',
                           'detail': 'The answer exceeded its size limit.'}).encode('utf-8') + b'\n'
    return data


def serve(rfile, wfile, backend):
    """Answer requests until EOF. An oversized or undecodable line ends the session."""
    while True:
        line = rfile.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_REQUEST_BYTES or not line.endswith(b'\n'):
            wfile.write(_encode({'id': None, 'ok': False, 'error': 'protocol', 'detail': 'request too large'}))
            wfile.flush()
            return
        try:
            request = json.loads(line)
        except ValueError:
            wfile.write(_encode({'id': None, 'ok': False, 'error': 'protocol', 'detail': 'request is not JSON'}))
            wfile.flush()
            return
        wfile.write(_encode(handle(request, backend)))
        wfile.flush()


def worker_main(backend='blocked'):
    """`--ax-worker` entry point. Serves BlockedBackend; `--ax-backend mac` (only the E-N2 probe asks for it) serves
    MacBackend."""
    import resource
    for limit, value in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_NOFILE, 64),
                         (resource.RLIMIT_CPU, 120)):
        resource.setrlimit(limit, (value, value))
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # libraries must never corrupt the protocol
    serve(sys.stdin.buffer, out, MacBackend() if backend == 'mac' else BlockedBackend())


# ---- host client ------------------------------------------------------------

def worker_argv():
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--ax-worker']
    return [sys.executable, '-s', str(Path(__file__).resolve().parents[1] / 'main.py'), '--ax-worker']


def _spawn(argv):
    # Phase 43: the one switch the worker reads (PEBBLE_WINDOW_NUMBERS=0, 1.8.0's refs) passes the cleared environment.
    switch = {'PEBBLE_WINDOW_NUMBERS': os.environ['PEBBLE_WINDOW_NUMBERS']} \
        if os.environ.get('PEBBLE_WINDOW_NUMBERS') == '0' else {}
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            close_fds=True, start_new_session=True,
                            env={'PATH': '/usr/bin:/bin', 'TMPDIR': os.environ.get('TMPDIR', '/tmp'), **switch})


class WorkerClient:
    """Host side of the protocol. One request at a time; any failure kills the worker.

    spawn(argv) must return an object with stdin, stdout (with fileno), poll(),
    kill() and wait(); tests pass a pipe pair served by an in-process thread.
    """

    name = 'worker'
    available = True

    def __init__(self, argv=None, *, timeout=REQUEST_SECONDS, spawn=None, clock=time.monotonic):
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError('Choose a request timeout from above 0 to 60 seconds.')
        self.argv, self.timeout = list(argv or worker_argv()), float(timeout)
        self.spawn, self.clock = spawn or _spawn, clock
        self.proc, self.buffer, self.next_id = None, b'', 0
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            self._kill()

    def _kill(self):
        proc, self.proc, self.buffer = self.proc, None, b''
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
        # Not select(): it refuses descriptors numbered 1024 (FD_SETSIZE) or above, which a busy host reaches.
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise AXError('timeout', 'The accessibility worker did not answer in time.')
                if not selector.select(remaining):
                    continue
                chunk = os.read(fd, 65_536)
                if not chunk:
                    raise AXError('protocol', 'The accessibility worker exited.')
                self.buffer += chunk
                if len(self.buffer) > MAX_RESPONSE_BYTES + 1:
                    raise AXError('protocol', 'The accessibility worker answer exceeded its size limit.')
        line, _, self.buffer = self.buffer.partition(b'\n')
        return line

    def call(self, op, args, *, timeout=None):
        if op not in OPS + EXTRA_OPS:
            raise AXError('invalid', 'op')
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            data = json.dumps({'id': rid, 'op': op, 'args': args}, ensure_ascii=False,
                              separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
            if len(data) > MAX_REQUEST_BYTES:
                raise AXError('invalid', 'The request exceeded its size limit.')
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._kill()
                    self.proc = self.spawn(self.argv)
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
                line = self._readline(self.clock() + (self.timeout if timeout is None else timeout))
                response = json.loads(line)
                if not isinstance(response, dict) or response.get('id') != rid or type(response.get('ok')) is not bool:
                    raise AXError('protocol', 'The accessibility worker answered out of order.')
            except (AXError, OSError, ValueError) as exc:
                self._kill()
                if op == 'perform':  # the act may or may not have happened; never retry it
                    raise AXError('uncertain', getattr(exc, 'detail', type(exc).__name__)) from None
                if isinstance(exc, AXError):
                    raise
                raise AXError('protocol', type(exc).__name__) from None
        if not response['ok']:
            raise AXError(response.get('error'), response.get('detail') or '')
        return response.get('result')

    def windows(self, pid, start):
        return self.call('windows', {'pid': pid, 'start': start})

    pages_first = True

    def tree(self, pid, start, window_ref, limit, page_first=False):
        return self.call('tree', {'pid': pid, 'start': start, 'window_ref': window_ref, 'limit': limit,
                                  **({'page_first': True} if page_first else {})})

    checks_expect = True

    def perform(self, pid, start, window_ref, path, action, text, expect=None):
        return self.call('perform', {'pid': pid, 'start': start, 'window_ref': window_ref, 'path': path,
                                     'action': action, 'text': text, **({'expect': expect} if expect else {})})

    # Phase 43 (1.9.0)
    def hello(self):
        """The worker's hello, asked once per worker process (capability discovery reads it)."""
        cached = getattr(self, '_hello', None)
        if cached is not None and cached[0] is self.proc and self.proc is not None:
            return cached[1]
        answer = self.call('hello', {})
        self._hello = (self.proc, answer)
        return answer

    def enhance(self, pid, start):
        return self.call('enhance', {'pid': pid, 'start': start}, timeout=ENHANCE_SECONDS)

    def restore(self, pid, start, changed):
        return self.call('restore', {'pid': pid, 'start': start, 'changed': changed})

    def raise_window(self, pid, start, window_ref):
        return self.call('raise', {'pid': pid, 'start': start, 'window_ref': window_ref})

    def hit(self, pid, start, window_ref, point=None, focused=False):
        """Phase 45 (layer 4): what is under a global point, or holds the keyboard (sanitize_hit's shape)."""
        args = {'pid': pid, 'start': start, 'window_ref': window_ref}
        return self.call('hit', {**args, 'point': [float(point[0]), float(point[1])]} if point is not None
                         else {**args, 'focused': True})


def production_backend():
    """Phase 42 (1.8.0): on macOS the real backend in its worker (E-N2 passed on the person's Mac, 2026-10-25: the
    worker is attributed to Pebble.app); elsewhere BlockedBackend."""
    if sys.platform != 'darwin':
        return BlockedBackend()
    return WorkerClient(worker_argv() + ['--ax-backend', 'mac'])


def request_access(prompt=True):
    """Whether this process may use Accessibility, through AXIsProcessTrustedWithOptions. With prompt, macOS shows its
    own request ("Pebble would like to control this computer using accessibility features") and lists Pebble under
    Privacy & Security > Accessibility; it shows that request once, after which only the switch there changes it.
    In Pebble's backend macOS attributes the check to Pebble.app (E-N2). False off macOS."""
    if sys.platform != 'darwin':
        return False
    import ctypes
    ax = MacAX()
    P = ctypes.c_void_p
    fn = ax.ax_lib.AXIsProcessTrustedWithOptions
    fn.restype, fn.argtypes = ctypes.c_bool, [P]
    make = ax.cf_lib.CFDictionaryCreate
    make.restype, make.argtypes = P, [P, ctypes.POINTER(P), ctypes.POINTER(P), ctypes.c_long, P, P]
    keys = (P * 1)(P.in_dll(ax.ax_lib, 'kAXTrustedCheckOptionPrompt').value)
    values = (P * 1)(P.in_dll(ax.cf_lib, 'kCFBooleanTrue' if prompt else 'kCFBooleanFalse').value)
    options = make(None, keys, values, 1, ctypes.addressof(ctypes.c_byte.in_dll(ax.cf_lib, 'kCFTypeDictionaryKeyCallBacks')),
                   ctypes.addressof(ctypes.c_byte.in_dll(ax.cf_lib, 'kCFTypeDictionaryValueCallBacks')))
    try:
        return bool(fn(options))
    finally:
        if options:
            ax.release(options)
