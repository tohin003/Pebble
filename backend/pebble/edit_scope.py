"""Phase 34 (W1, beat plan): a test file changes only when the request asks for test changes.

Tests are the person's evidence that the code works. An edit that creates or changes a test file when the current
request does not ask for test changes is staged for the person's review, in Auto mode too, with the host's reason on
the approval card. To probe a case the tests do not cover, the model passes a check to RunTests instead
(testing.CHECK_FILES): it runs in the isolated copy only and never writes to the project.

The intent test is deliberately narrow: a change verb whose object is tests ("add a test", "update the unit tests"),
or a change verb a few words before the file's own name ("add a case to test_math.py"). Anything else, "fix it so the
tests pass" included, leaves the edit to the person. A negated request ("don't change the tests") is never intent.
"""
import re
from pathlib import PurePosixPath

from .text_safety import plain_quotes

TEST_FILE = re.compile(r'(?:^|/)(?:tests?/|__tests__/|test_[^/]*$|[^/]*_test\.[a-z]+$|[^/]*\.(?:test|spec)\.[a-z]+$)')
CHANGE = (r'(?:add|write|create|update|change|edit|modify|extend|rewrite|expand|adjust|correct|remove|delete|rename|'
          r'replace|improve|refactor|amend|include|provide)')
# Phase 46 (dev d-cb-01: "Also add unittest tests for it in tests/test_slug.py." was held for review in Auto mode, an
# extra request each time): a test framework's name describes the tests as "unit" does.
DETERMINER = (r'(?:a|an|the|some|more|new|extra|missing|failing|broken|existing|its|their|my|our|these|those|that|this|'
              r'any|unit|regression|integration|another|additional|proper|unittest|pytest|jest|vitest|mocha|doctest)')
NOUN = r'(?:tests?|test cases?|test files?|test suites?|specs?|coverage)'
INTENT = re.compile(rf'\b{CHANGE}\w*\s+(?:{DETERMINER}\s+){{0,3}}{NOUN}\b', re.I)
# Phase 46 (private dev d-lh-91, its last turn: "every behaviour we added or changed today needs a test in tests/"): a
# thing that needs a new test asks for one; "needs the tests to pass" does not.
NEED = re.compile(r'\bneeds?\s+(?:a|an|its own|new|more)\s+(?:new\s+)?(?:tests?|test cases?)\b(?!\s+to\b)', re.I)
# Phase 46 (1.9.28, fix-research-1.9.28 false-completions F6): everyday ways to ask for a test that INTENT's "verb +
# tests" misses: "cover the new option with a test", "now add the flag, and test it", "plus a regression test for it",
# "every new behaviour should be tested", "make sure there is a test for it". requirements.carried() shares
# asks_for_tests, so such a message also releases an earlier message's "don't change the tests". "Make the tests pass",
# "the suite must still pass", "check it with the tests" and "there is a test that fails" ask for none.
ASKS = re.compile(
    r'\b(?:cover|back)\s+(?:it|them|this|that|the\s+[\w\s-]{1,40}?)\s+(?:with|by)\s+'
    r'(?:a\s+|an\s+|new\s+|more\s+|unit\s+|some\s+)?(?:tests?|test cases?)\b'
    r'|\b(?:cover|back)\s+(?:it|them|this|that)\s+in\s+`?(?:[\w.-]+/)*test'
    r'|\b(?:and|then|also|plus)\s+test\s+(?:it|them|this|that)\b'
    r'|\b(?:should|must|needs?\s+to|has\s+to|have\s+to)\s+be\s+(?:unit[- ])?tested\b'
    r'|\b(?:plus|along\s+with|together\s+with)\s+(?:a|an|new|more|another)\s+(?:regression\s+|unit\s+)?tests?\b'
    r'|\bwith\s+(?:a\s+|an\s+)?(?:new|regression)\s+tests?\b'
    r'|\b(?:make\s+sure|ensure)\s+(?:that\s+)?there\s+(?:is|are)\s+(?:a\s+|an\s+)?(?:new\s+|regression\s+|unit\s+)?tests?\b',
    re.I)
NEGATION = re.compile(r"\b(?:don'?t|doesn'?t|do not|does not|never|not|without|no need to|avoid|leave)\b[\w\s,]{0,24}$",
                      re.I)
REASON = ("Pebble wants to change a test file, and your request didn't ask for test changes. Tests are your evidence "
          "that the code works, so this change waits for you.")
MODEL_NOTE = ("This test file change waits for the person's decision, because the request did not ask for test "
              "changes. Leave the project's tests as they are; to probe a case they do not cover, pass a check to RunTests "
              "(it runs in the isolated copy only).")


def is_test_file(path):
    return isinstance(path, str) and bool(TEST_FILE.search(PurePosixPath(path).as_posix()))


def _affirmed(pattern, text):
    """Whether any match of pattern in text is not negated just before it."""
    for match in pattern.finditer(text):
        if not NEGATION.search(text[max(0, match.start() - 40):match.start()]):
            return True
    return False


def asks_for_tests(text):
    """Whether the request text asks to change tests in general (INTENT, NEED, ASKS), never in a negation."""
    text = plain_quotes(text)  # Phase 46 (1.9.28): "don\u2019t modify the tests" as typed on a Mac
    return isinstance(text, str) and (_affirmed(INTENT, text) or _affirmed(NEED, text) or _affirmed(ASKS, text))


def asks_for_test_changes(text, path):
    """Whether the request text asks to change tests in general, or this test file by name."""
    if not isinstance(text, str) or not text.strip():
        return False
    text = plain_quotes(text)
    if asks_for_tests(text):
        return True
    name = re.escape(PurePosixPath(path).name)
    return _affirmed(re.compile(rf'\b{CHANGE}\w*\s+(?:\S+\s+){{0,3}}`?(?:[\w.-]+/)*{name}\b', re.I), text)


def review_reason(path, text):
    """The host's reason when this edit must wait for the person, or None."""
    if is_test_file(path) and not asks_for_test_changes(text, path):
        return REASON
    return None
