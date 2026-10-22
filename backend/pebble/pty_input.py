"""Validated input for an interactive job's terminal (R07, design study §3 and §8c).

Callers never send raw bytes. encode() accepts either text (printable UTF-8
plus newline and tab) or one named key from KEYS, and returns the exact bytes
the host relays to the PTY master. ESC, every other C0/C1 control, DEL, bidi
and zero-width format characters are refused in text: the only escape
sequences that reach a job are the host's own key encodings. check() is the
shim's independent re-validation of those bytes, so a host bug cannot widen
what reaches the terminal. Terminal-emulator replies (cursor reports, device
attributes) are never generated here and must never be wired into input.

Limits, enforced by the host and again by the shim: WRITE_LIMIT bytes per
write, TOTAL_LIMIT bytes per process and RATE accepted writes in any WINDOW
seconds. receipt() describes a write by length and SHA-256 only, never its
text, so a secret typed while echo is off is not journaled.
"""

import hashlib
import time
from collections import deque

from .policy import PolicyError

WRITE_LIMIT = 1024
TOTAL_LIMIT = 32 * 1024
RATE = 4
WINDOW = 1.0
ORIGINS = ('model', 'human')
KINDS = ('text', 'key')
# The host's key encodings (xterm/VT100 normal cursor mode). Enter is CR, as a
# keyboard sends it; the line discipline's ICRNL turns it into a newline.
KEYS = {
    'enter': b'\r', 'tab': b'\t', 'backspace': b'\x7f', 'ctrl_c': b'\x03', 'ctrl_d': b'\x04',
    'up': b'\x1b[A', 'down': b'\x1b[B', 'right': b'\x1b[C', 'left': b'\x1b[D',
}
_ENCODED = frozenset(KEYS.values())


def _text_ok(text):
    return all(ch in '\n\t' or ch.isprintable() for ch in text)


def encode(*, text=None, key=None):
    """(kind, data) for one write: exactly one of text or key. Refuses anything else."""
    if (text is None) == (key is None):
        raise PolicyError('Send either text or one named key to the process terminal.')
    if key is not None:
        if not isinstance(key, str) or key not in KEYS:
            raise PolicyError('Terminal keys are ' + ', '.join(KEYS) + '.')
        return 'key', KEYS[key]
    if not isinstance(text, str) or not text:
        raise PolicyError('Terminal input text must be a non-empty string.')
    if not _text_ok(text):
        raise PolicyError('Terminal input text is printable characters, newline and tab only. Escape sequences, '
                          'control and bidirectional characters are refused; use a named key instead.')
    try:
        data = text.encode('utf-8')
    except UnicodeEncodeError:
        raise PolicyError('Terminal input text must be valid UTF-8.') from None
    if len(data) > WRITE_LIMIT:
        raise PolicyError(f'Send at most {WRITE_LIMIT} bytes of terminal input at a time.')
    return 'text', data


def check(kind, data):
    """The shim's re-validation of encoded bytes; returns them or raises PolicyError."""
    if not isinstance(data, (bytes, bytearray)) or not 1 <= len(data) <= WRITE_LIMIT:
        raise PolicyError('Terminal input is 1 to %d bytes.' % WRITE_LIMIT)
    data = bytes(data)
    if kind == 'key':
        if data not in _ENCODED:
            raise PolicyError('Terminal key input must be one host-encoded key.')
        return data
    if kind != 'text':
        raise PolicyError('Terminal input is text or a key.')
    try:
        text = data.decode('utf-8', 'strict')
    except UnicodeDecodeError:
        raise PolicyError('Terminal input text must be valid UTF-8.') from None
    if not _text_ok(text):
        raise PolicyError('Terminal input text holds a control or escape character.')
    return data


def receipt(origin, kind, data, key=None):
    """Content-free description of one write: never the text."""
    record = {'origin': origin, 'kind': kind, 'length': len(data), 'sha256': hashlib.sha256(bytes(data)).hexdigest()}
    if kind == 'key' and key in KEYS:
        record['key'] = key  # a named key is not content
    return record


class Limiter:
    """Per-process input budget: bytes per write, bytes in total and accepted writes per window."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.total = 0
        self.recent = deque()

    def refusal(self, size):
        """None if a write of `size` bytes may be sent now, else a fixed reason code."""
        if type(size) is not int or not 1 <= size <= WRITE_LIMIT:
            return 'write_limit'
        if self.total + size > TOTAL_LIMIT:
            return 'total_limit'
        now = self.clock()
        while self.recent and now - self.recent[0] >= WINDOW:
            self.recent.popleft()
        if len(self.recent) >= RATE:
            return 'rate_limit'
        return None

    def spend(self, size):
        """Charge an accepted write (call only after refusal() returned None)."""
        self.total += size
        self.recent.append(self.clock())
