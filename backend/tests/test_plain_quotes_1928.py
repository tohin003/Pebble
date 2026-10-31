"""Phase 46 (1.9.28, the lead, after w17's report): the checks that read the person's words treat typographic
apostrophes and quotes as plain ones. macOS types "Don’t" for "Don't" (smart quotes) and pasted text carries
them; before, the requirement ledger read "Don’t touch data/records.jsonl" as no rule at all, and the read-only
rule and the test-intent rule missed "Don’t change anything" and "don’t modify the tests"."""

import pytest

from pebble import coding_api, edit_scope, requirements as rq
from pebble.runner import read_only_goal
from pebble.text_safety import plain_quotes

SENTENCES = [
    "Run scripts/migrate.py (data/records.jsonl -> out/records.csv). Don't touch data/records.jsonl.",
    "Fix calc.py but don't modify the tests.",
    "Leave notes.txt alone and don't change config/settings.toml.",
    "Run tools/summary.py and tell me how many ERROR lines it counts. Don't change anything.",
    "The output of `python3 report.py` must stay byte-identical to golden/report.txt; don't edit golden/.",
    "Merge feature into main. Don't change main's history, and don't push.",
    "Please don't touch the \"vendor\" folder; it's third-party code.",
]


def curly(text):
    return text.replace("'", '\N{RIGHT SINGLE QUOTATION MARK}').replace('"', '\N{LEFT DOUBLE QUOTATION MARK}', 1).replace(
        '"', '\N{RIGHT DOUBLE QUOTATION MARK}', 1)


def test_plain_quotes_folds_one_character_for_one():
    text = 'Don\N{RIGHT SINGLE QUOTATION MARK}t \N{LEFT DOUBLE QUOTATION MARK}x\N{RIGHT DOUBLE QUOTATION MARK} it\N{MODIFIER LETTER APOSTROPHE}s'
    assert plain_quotes(text) == "Don't \"x\" it's" and len(plain_quotes(text)) == len(text)
    assert plain_quotes(None) is None and plain_quotes('plain') == 'plain'


@pytest.mark.parametrize('text', SENTENCES)
def test_each_reader_reads_curly_text_as_it_reads_straight_text(text):
    typed = curly(text)
    assert typed != text
    assert rq.extract(typed) == rq.extract(text)
    assert read_only_goal(typed, True)[0] == read_only_goal(text, True)[0]
    assert read_only_goal(typed, False)[0] == read_only_goal(text, False)[0]
    assert edit_scope.asks_for_tests(typed) == edit_scope.asks_for_tests(text)


def test_the_cases_that_failed_before():
    assert [r['paths'] for r in rq.extract(curly(SENTENCES[0]))] == [['data/records.jsonl']]
    assert any(r['kind'] == 'unchanged' for r in rq.extract(curly(SENTENCES[1])))
    assert read_only_goal(curly(SENTENCES[3]), True)[0] is True


@pytest.mark.parametrize('text', ["Commit the fix. Don't push.", "Commit it but don't push main or develop.",
                                  "Tag v2.1 and publish only that tag to origin. Don't push any branch."])
def test_the_push_guard_reads_curly_text_as_straight_text(text):
    straight = coding_api.push_forbidden([text])
    assert straight is not None and coding_api.push_forbidden([curly(text)]) == straight
