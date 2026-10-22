"""Phase 24 (t24-browser): one invisible-character set for Pebble's text checks (V1-054, V1-168).

The Chrome secret-field label check (ax_worker.is_secure, also behind browser_session's page facts) missed the bidi
isolates, U+061C, U+180E and the tag block, so 'Pass' + one of them + 'word' was an ordinary field whose value could be
read and typed into. text_safety now holds the set sources.INVISIBLE already used (the T7 F1 fix), as escapes.

Every invisible character in this file is built with chr(), so the file itself stays plain ASCII.
"""

import re
from pathlib import Path

import pytest

from pebble import ax_worker, sources, text_safety

ROOT = Path(__file__).resolve().parents[2]
# The characters V1-054 names, each between 'Pass' and 'word'.
SPLITTERS = [0x2066, 0x2067, 0x2068, 0x2069, 0x061C, 0x180E, 0xE0041]
EVERY = ''.join(chr(c) for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF)


def field(label, key='title', role='AXTextField'):
    return {'role': role, 'path': 'w/f', key: label, 'value': 'hunter2', 'enabled': True, 'frame': [0, 0, 100, 20]}


@pytest.mark.parametrize('code', SPLITTERS, ids=[f'U+{c:04X}' for c in SPLITTERS])
def test_a_secret_label_split_by_an_invisible_character_is_still_secret(code):
    label = 'Pass' + chr(code) + 'word'
    for key in ('title', 'description', 'placeholder', 'help'):
        assert ax_worker.is_secure(field(label, key)), (key, hex(code))
        clean = ax_worker.sanitize_element(field(label, key))
        assert clean['secure'] is True and clean['value'] is None and clean['value_sha256'] is None, (key, hex(code))
        assert chr(code) not in (clean[key] or ''), 'the sanitizer drops the character itself'
    # Labels only ever raise: a button with the same label is not a field, so it stays an ordinary button.
    assert not ax_worker.is_secure(field(label, role='AXButton'))


def test_the_shared_set_is_exactly_sources_invisible_and_ax_worker_uses_it():
    shared = set(text_safety.INVISIBLE.findall(EVERY))
    assert shared == set(sources.INVISIBLE.findall(EVERY)), sorted(hex(ord(c)) for c in shared ^ set(
        sources.INVISIBLE.findall(EVERY)))
    assert {chr(c) for c in SPLITTERS} <= shared and len(shared) == 1 + 1 + 1 + 5 + 5 + 5 + 4 + 1 + 128
    assert ax_worker.INVISIBLE is text_safety.INVISIBLE
    controls = set(ax_worker.CONTROL.findall(EVERY))
    assert controls == shared | {chr(c) for c in [*range(0x20), *range(0x7F, 0xA0)]}
    # The 0.6.1 classes, rebuilt here from code points, are inside the new ones: nothing that was refused passes now.
    old_invisible = {chr(c) for c in [0xAD, *range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x2065),
                                      0xFEFF]}
    assert old_invisible < shared and old_invisible | {chr(c) for c in [*range(0x20), *range(0x7F, 0xA0)]} < controls


def test_clip_and_the_worker_checks_drop_or_refuse_every_shared_character():
    for char in sorted(set(text_safety.INVISIBLE.findall(EVERY)))[:200]:
        assert ax_worker.clip('a' + char + 'b', 10) == 'ab', hex(ord(char))
        assert ax_worker.sanitize_element({'role': 'AXButton', 'path': 'w/' + char + 'x'}) is None, hex(ord(char))
    # Expected text for a commit refuses them too (screen_session's expect check uses ax_worker.CONTROL).
    assert ax_worker.CONTROL.search('Sent' + chr(0x2068))


def test_folding_catches_other_disguises_and_leaves_ordinary_labels_alone():
    fullwidth = ''.join(chr(0xFF00 + ord(c) - 0x20) for c in 'password')
    combining = 'Pass' + chr(0x0332) + 'word'  # a combining low line, which NFKC cannot compose away
    cgj = 'Pass' + chr(0x034F) + 'word'
    for label in (fullwidth, combining, cgj, 'One' + chr(0x200D) + '-time code', 'P' + chr(0x00AD) + 'IN'):
        assert ax_worker.is_secure(field(label)), ascii(label)
    for label in ('To', 'Subject', 'Search mail', 'Pinned items', 'Username', 'Passport number', 'Spinner'):
        assert not ax_worker.is_secure(field(label)), label
    assert text_safety.secret_text(None) == '' and text_safety.secret_text('Pass\nword') == 'Pass word'
    assert text_safety.strip_invisible('a' + chr(0x2066) + 'b') == 'ab' and text_safety.strip_invisible(7) == 7


def test_the_owned_sources_carry_no_raw_invisible_character():
    raw = re.compile('[' + text_safety.INVISIBLE_CHARS + ']')
    for name in ('ax_worker.py', 'text_safety.py', 'screen_session.py', 'browser_bridge.py', 'browser_session.py',
                 'native_host_setup.py'):
        text = (ROOT / 'backend/pebble' / name).read_text(encoding='utf-8')
        found = [(i + 1, hex(ord(m.group()))) for i, line in enumerate(text.splitlines()) for m in raw.finditer(line)]
        assert found == [], (name, found)
