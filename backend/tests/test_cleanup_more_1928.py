"""Phase 46 (1.9.28, the lead after w12b): three cleanup wordings 1.9.27 checked, read again by CLEANUP_MORE, with no
new trigger on ordinary requests or on any visible prompt."""

import json
import os
import time
from pathlib import Path

import pytest

from pebble import requirements as rq

KINDS = lambda text: [item['kind'] for item in rq.extract(text)]  # noqa: E731


@pytest.mark.parametrize('text', [
    'Clean the repo of generated files.',
    "Delete everything in build/ and dist/, it's all build output.",
    "Don't delete anything except the generated files.",
    'Please clean up the project of all compiled artifacts before the release.',
])
def test_the_three_wordings_ask_for_the_cleanup_check(text):
    assert 'generated_removed' in KINDS(text)


@pytest.mark.parametrize('text', [
    'Remove the debug prints from all compiled templates.',
    'Delete the line in the Makefile that echoes whatever gets compiled.',
    "Don't delete the build output.",
    'Clean the repo of stale branches.',
    "Don't delete anything except the old logs.",
    'Delete everything in tmp/, it is all scratch.',
])
def test_ordinary_requests_still_ask_for_none(text):
    assert 'generated_removed' not in KINDS(text)


# The dev splits live outside the published tree: PEBBLE_GOLDEN_DEV and PEBBLE_GOLDEN_EXTRA name their folders (never a
# default, as test_git_private_cases_46 does)
SPLITS = [Path(os.environ[name]) for name in ('PEBBLE_GOLDEN_DEV', 'PEBBLE_GOLDEN_EXTRA') if os.environ.get(name)]


@pytest.mark.skipif(not SPLITS, reason='needs PEBBLE_GOLDEN_DEV or PEBBLE_GOLDEN_EXTRA (the dev splits)')
def test_no_visible_prompt_gains_the_check():
    gained = []
    for split in SPLITS:
        cases = split / 'cases.jsonl'
        if not cases.is_file():
            continue
        for line in cases.read_text().splitlines():
            case = json.loads(line)
            texts = [case.get('prompt') or ''] + [a if isinstance(a, str) else json.dumps(a) for a in
                                                  case.get('alternate_prompts') or []]
            for text in texts:
                if rq.CLEANUP_MORE.search(text) and not rq.CLEANUP.search(text):
                    gained.append(case['id'])
    assert gained == []


def test_it_stays_linear_on_hostile_input():
    started = time.monotonic()
    for text in ('clean the repo of ' + 'x ' * 50000, 'delete everything in build/ ' + ' ' * 100000,
                 "don't delete anything except " + 'generated ' * 20000):
        rq.CLEANUP_MORE.search(text)
    assert time.monotonic() - started < 1.0
