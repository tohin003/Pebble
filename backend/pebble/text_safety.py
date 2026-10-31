"""One set of invisible and control characters for Pebble's text checks (Phase 24, V1-054 and V1-168).

Thirteen modules each wrote their own character class, and they drifted: the one behind Pebble for Chrome's
secret-field label check (ax_worker) missed the bidi isolates, U+061C, U+180E and the tag block that
sources.INVISIBLE already strips (the T7 F1 fix), so 'Pass' + U+2066 + 'word' was not a secret label.

INVISIBLE_CHARS is exactly sources.INVISIBLE's set (pinned equal by test_text_safety_24.py), written as escapes so
no source file carries a raw invisible character. ax_worker builds its sanitizer on it; the other modules can move
to it one by one (the note lists them).

secret_text() is for raise-only word checks such as SECRET_LABEL: it also folds compatibility forms (fullwidth,
mathematical letters) and drops every format character and combining mark, so a label can only become more likely
to read as a secret. It is never used to change what is shown or stored.
"""

import re
import unicodedata

# Soft hyphen, ALM, MVS, zero-width space/joiners, LRM/RLM, bidi embeddings and overrides, word joiner and invisible
# operators, bidi isolates, BOM and the tag block.
INVISIBLE_CHARS = ('\U000000ad\U0000061c\U0000180e\U0000200b-\U0000200f\U0000202a-\U0000202e\U00002060-\U00002064'
                   '\U00002066-\U00002069\U0000feff\U000e0000-\U000e007f')
INVISIBLE = re.compile('[' + INVISIBLE_CHARS + ']')
# C0 and C1 controls plus DEL.
CONTROL_CHARS = '\x00-\x1f\x7f-\x9f'
CONTROL = re.compile('[' + CONTROL_CHARS + ']')
# Either kind: text that must not carry them at all (paths, identifiers, expected text).
UNSAFE = re.compile('[' + CONTROL_CHARS + INVISIBLE_CHARS + ']')
_IGNORED = frozenset({'Cf', 'Mn', 'Me'})


def strip_invisible(text):
    """text without the invisible characters (non-text is returned unchanged)."""
    return INVISIBLE.sub('', text) if isinstance(text, str) else text


# Phase 46 (1.9.28, the lead, after w17's report): typographic apostrophes and quotes as their plain forms, one
# character for one so offsets hold. macOS types "Don\u2019t" for "Don't" (smart quotes) and pasted text carries them,
# while the checks that read the person's words match "don't": the requirement ledger read "Don\u2019t touch
# data/records.jsonl" as no rule, and the read-only and push rules missed "Don\u2019t change anything" and "Don\u2019t
# push". For reading only, never for what is shown or stored.
_PLAIN_QUOTES = str.maketrans({'\u2018': "'", '\u2019': "'", '\u201b': "'", '\u02bc': "'", '\u2032': "'", '\uff07': "'",
                               '\u201c': '"', '\u201d': '"', '\u201f': '"', '\uff02': '"'})


def plain_quotes(text):
    """text with its typographic apostrophes and quotes as ' and " (non-text is returned unchanged)."""
    return text.translate(_PLAIN_QUOTES) if isinstance(text, str) else text


def secret_text(text):
    """A folded copy of text for raise-only word checks, or '' for non-text.

    NFKC folds compatibility forms (fullwidth and mathematical letters to plain 'pass'); the INVISIBLE set, every
    other format character (category Cf) and every combining mark are dropped and controls become spaces, so no
    character between two letters can split a word past the check.
    """
    if not isinstance(text, str):
        return ''
    folded = unicodedata.normalize('NFKC', INVISIBLE.sub('', text))
    kept = ''.join(c for c in folded if unicodedata.category(c) not in _IGNORED)
    return CONTROL.sub(' ', kept)
