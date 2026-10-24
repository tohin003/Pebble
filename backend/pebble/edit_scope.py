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

TEST_FILE = re.compile(r'(?:^|/)(?:tests?/|__tests__/|test_[^/]*$|[^/]*_test\.[a-z]+$|[^/]*\.(?:test|spec)\.[a-z]+$)')
CHANGE = (r'(?:add|write|create|update|change|edit|modify|extend|rewrite|expand|adjust|correct|remove|delete|rename|'
          r'replace|improve|refactor|amend|include|provide)')
DETERMINER = (r'(?:a|an|the|some|more|new|extra|missing|failing|broken|existing|its|their|my|our|these|those|that|this|'
              r'any|unit|regression|integration|another|additional|proper)')
NOUN = r'(?:tests?|test cases?|test files?|test suites?|specs?|coverage)'
INTENT = re.compile(rf'\b{CHANGE}\w*\s+(?:{DETERMINER}\s+){{0,3}}{NOUN}\b', re.I)
NEGATION = re.compile(r"\b(?:don'?t|do not|never|not|without|no need to|avoid|leave)\b[\w\s,]{0,24}$", re.I)
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


def asks_for_test_changes(text, path):
    """Whether the request text asks to change tests in general, or this test file by name."""
    if not isinstance(text, str) or not text.strip():
        return False
    if _affirmed(INTENT, text):
        return True
    name = re.escape(PurePosixPath(path).name)
    return _affirmed(re.compile(rf'\b{CHANGE}\w*\s+(?:\S+\s+){{0,3}}`?{name}\b', re.I), text)


def review_reason(path, text):
    """The host's reason when this edit must wait for the person, or None."""
    if is_test_file(path) and not asks_for_test_changes(text, path):
        return REASON
    return None
