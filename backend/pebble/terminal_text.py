"""Terminal output projection for the model and the UI (Phase 14, F6).

Raw process bytes stay stored unchanged. This module computes the text a model
or the native panel may see: escape sequences, C0/C1 controls and DEL are
removed, CR and backspace are applied, and bidirectional and tag characters
become visible markers. The projection is display data only; it never grants
authority and never feeds terminal replies back into process input.

Pure functions only: no I/O, no state between calls. Work is linear in the
input: plain runs are skipped by one compiled scanner, every escape body is
matched once, and an aborted or unterminated sequence is resumed past the bytes
already scanned.

Paging: with final=False an incomplete trailing unit (escape sequence, partial
UTF-8 character or a CR that may start CRLF) is held back and reported in
held_bytes, so a byte cursor never splits a sequence. An unterminated sequence
longer than MAX_HELD_BYTES is not held: its introducer is dropped and its body
is shown as ordinary text, which can only reveal, never hide, data.
"""

import codecs
import re

MAX_HELD_BYTES = 4096
MARK = '⟦U+{:04X}⟧'
# Explicit embeddings, overrides and isolates (U+202A-E, U+2066-9), the implicit
# marks LRM/RLM/ALM, and the invisible tag block U+E0000-E007F.
BIDI = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A)) | {0x200E, 0x200F, 0x061C}
TAGS = range(0xE0000, 0xE0080)

# One token per control unit. UTF-8 is self-synchronizing, so a multibyte
# pattern can never match inside another character.
_TOKEN = re.compile(
    rb'\x1b+'                                       # ESC (a run: all but the last are lone)
    rb'|\r+\n?'                                     # bare CRs, optionally ending in CRLF
    rb'|\n+'
    rb'|\x08+'                                      # backspaces
    rb'|[\x00-\x07\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]+'  # other C0 controls and DEL
    rb'|\xc2[\x80-\x9f]'                            # C1 controls U+0080-U+009F
    rb'|\xe2\x80[\x8e\x8f\xaa-\xae]|\xe2\x81[\xa6-\xa9]|\xd8\x9c'  # bidi controls
    rb'|\xf3\xa0[\x80\x81][\x80-\xbf]')             # tag characters
_CSI_BODY = re.compile(rb'[\x30-\x3f]*[\x20-\x2f]*')
_NF_BODY = re.compile(rb'[\x20-\x2f]+')
# An OSC ends at BEL or ST; any other C0 or C1 control aborts it. Other control
# strings (DCS, SOS, PM, APC) end at ST and abort on ESC, CAN, SUB or any C1.
_OSC_STOP = re.compile(rb'[\x00-\x1f]|\xc2[\x80-\x9f]')
_STR_STOP = re.compile(rb'[\x18\x1a\x1b]|\xc2[\x80-\x9f]')
_LABELS = {chr(c).encode(): MARK.format(c) for c in (*BIDI, *TAGS)}
_COMPLETE, _ABORT, _INCOMPLETE = 0, 1, 2
_STRINGS = frozenset(b']PX^_')
_C1_STRINGS = {0x90: False, 0x98: False, 0x9d: True, 0x9e: False, 0x9f: False}


def _csi(raw, start, n):
    q = _CSI_BODY.match(raw, start).end()
    if q >= n:
        return _INCOMPLETE, n
    return (_COMPLETE, q + 1) if 0x40 <= raw[q] <= 0x7e else (_ABORT, q)


def _string(raw, start, n, osc):
    """(status, end). An incomplete string resumes at its body, revealing it."""
    m = (_OSC_STOP if osc else _STR_STOP).search(raw, start)
    if m is None:
        return _INCOMPLETE, start
    t = m.start()
    b = raw[t]
    if b == 0x1b:
        if t + 1 >= n:
            return _INCOMPLETE, start
        return (_COMPLETE, t + 2) if raw[t + 1] == 0x5c else (_ABORT, t)
    if b == 0x07 and osc or b in (0x18, 0x1a):
        return _COMPLETE, t + 1      # BEL ends an OSC; CAN/SUB cancel a string
    if b == 0xc2 and raw[t + 1] == 0x9c:
        return _COMPLETE, t + 2      # 8-bit ST
    return _ABORT, t


def _escape(raw, p, n):
    """Classify the sequence introduced by ESC at p: (status, end)."""
    if p + 1 >= n:
        return _INCOMPLETE, n
    b = raw[p + 1]
    if b == 0x5b:
        return _csi(raw, p + 2, n)
    if b in _STRINGS:
        return _string(raw, p + 2, n, b == 0x5d)
    if 0x20 <= b <= 0x2f:
        q = _NF_BODY.match(raw, p + 1).end()
        if q >= n:
            return _INCOMPLETE, n
        return (_COMPLETE, q + 1) if 0x30 <= raw[q] <= 0x7e else (_ABORT, q)
    if 0x30 <= b <= 0x7e:
        return _COMPLETE, p + 2
    return _ABORT, p + 1             # a lone ESC; the next byte is processed normally


def _char_end(raw, pos, n):
    """(end, complete) of the first UTF-8 character or maximal invalid subpart at pos."""
    b = raw[pos]
    if b < 0xc2 or b > 0xf4:
        return pos + 1, True
    need = 2 if b < 0xe0 else 3 if b < 0xf0 else 4
    low, high = {0xe0: (0xa0, 0xbf), 0xed: (0x80, 0x9f), 0xf0: (0x90, 0xbf), 0xf4: (0x80, 0x8f)}.get(b, (0x80, 0xbf))
    i = pos + 1
    for k in range(1, need):
        if i >= n:
            return i, False
        if not (low <= raw[i] <= high if k == 1 else 0x80 <= raw[i] <= 0xbf):
            return i, True
        i += 1
    return i, True


def _line(pieces, keep):
    """The visible text of a line: each piece up to the length backspace left."""
    if len(pieces) == 1 and keep[0] == len(pieces[0]):
        return pieces[0]
    return ''.join(p if k == len(p) else p[:k] for p, k in zip(pieces, keep))


def _raw(data):
    if isinstance(data, str):
        # Lone surrogates become invalid bytes and are replaced with U+FFFD.
        return data.encode('utf-8', 'surrogatepass')
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError('Terminal text must be bytes or str.')


def project(data, *, final=True, first_unit=False):
    """The safe projection of terminal output, with paging facts.

    Returns {'text', 'controls_removed', 'bidi_marked', 'held_bytes',
    'consumed_bytes', 'strict_utf8'}. Byte counts refer to the UTF-8 input
    (str input is encoded first). consumed_bytes + held_bytes equals the input
    length unless first_unit is set, which stops after the first unit so a pager
    can make bounded progress past a sequence wider than its byte limit.
    """
    raw = _raw(data)
    n = len(raw)
    done, pieces, keep, erasable = [], [], [], []
    reset = False
    removed = marked = held = pos = 0
    strict = True
    search = _TOKEN.search
    while pos < n:
        m = search(raw, pos)
        start = n if m is None else m.start()
        if start > pos:
            if first_unit:
                end, complete = _char_end(raw, pos, n)
                if not complete and not final:
                    held = n - pos
                    break
                chunk, start = raw[pos:end], end
            elif m is None and not final:
                decoder = codecs.getincrementaldecoder('utf-8')('replace')
                text = decoder.decode(raw[pos:], False)
                held = len(decoder.getstate()[0])
                chunk, start = None, n - held
                try:
                    raw[pos:start].decode('utf-8')
                except UnicodeDecodeError:
                    strict = False
            else:
                chunk = raw[pos:start]
            if chunk is not None:
                try:
                    text = chunk.decode('utf-8')
                except UnicodeDecodeError:
                    text = chunk.decode('utf-8', 'replace')
                    strict = False
            if text:
                if reset:
                    del pieces[:], keep[:], erasable[:]
                    reset = False
                erasable.append(len(pieces))
                pieces.append(text)
                keep.append(len(text))
            pos = start
            if m is None or first_unit or held:
                break
        end = m.end()
        b = raw[start]
        if b == 0x1b:
            if end - start > 1:
                removed += end - start - 1   # each ESC followed by ESC stands alone
                start = end - 1
            status, stop = _escape(raw, start, n)
        elif b == 0xc2 and raw[start + 1] == 0x9b:
            status, stop = _csi(raw, end, n)
        elif b == 0xc2 and raw[start + 1] in _C1_STRINGS:
            status, stop = _string(raw, end, n, _C1_STRINGS[raw[start + 1]])
        else:
            status = None
        if status is not None:
            if status == _INCOMPLETE and not final and n - start <= MAX_HELD_BYTES:
                held, pos = n - start, start
                break
            # A complete sequence is dropped whole; an aborted one up to the byte
            # that ended it; an unterminated string resumes at its body.
            removed += 1
            pos = stop
        elif b == 0x0d:
            lf = raw[end - 1] == 0x0a
            crs = end - start - 2 if lf else end - start    # bare CRs; CRLF is a newline
            if not lf and end == n and not final:
                held, end, crs = 1, end - 1, crs - 1         # this CR may start a CRLF
            removed += crs
            if lf:
                done.append(_line(pieces, keep))
                done.append('\n')
                del pieces[:], keep[:], erasable[:]
                reset = False
            elif crs:
                reset = True             # keep the last nonempty segment of this line
            pos = end
            if held:
                break
        elif b == 0x0a:
            done.append(_line(pieces, keep))
            done.append('\n' * (end - start))
            del pieces[:], keep[:], erasable[:]
            reset = False
            pos = end
        elif b == 0x08:
            count = end - start
            removed += count
            while count and erasable and not reset:
                index = erasable[-1]
                take = min(count, keep[index])
                keep[index] -= take
                count -= take
                if not keep[index]:
                    erasable.pop()
            pos = end
        elif b < 0x20 or b == 0x7f:
            removed += end - start
            pos = end
        elif b == 0xc2:
            removed += 1                 # any other C1 control, including a lone ST
            pos = end
        else:
            if reset:
                del pieces[:], keep[:], erasable[:]
                reset = False
            label = _LABELS[raw[start:end]]
            pieces.append(label)
            keep.append(len(label))
            removed += 1
            marked += 1
            pos = end
        if first_unit:
            break
    done.append(_line(pieces, keep))
    return {'text': ''.join(done), 'controls_removed': removed, 'bidi_marked': marked,
            'held_bytes': held, 'consumed_bytes': pos, 'strict_utf8': strict}


def sanitize(data, *, final=True):
    """(text, controls_removed, held_bytes) for bytes or str terminal output."""
    view = project(data, final=final)
    return view['text'], view['controls_removed'], view['held_bytes']
