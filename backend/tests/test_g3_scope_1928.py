"""Phase 46 (1.9.28): G3 (a request that keeps the whole project unchanged runs its commands without write access) is
decided by the runner's own read-only decision, never by the ledger's READ_ONLY pattern alone (the security review of
w12-bnew: a quoted "Read-only file system" and "add a read-only mode" lost write access in edit requests, and "Never
change anything in this project" kept it). Its whole-project rule decides on every route: 8K and below, the runner's
older rule also took a prohibition on named files, and a run asked to produce a file could not write it (private
d-rc-106, phrasing 1, on the lead's scripted check)."""

import pytest

from pebble.app import keeps_whole_project


@pytest.mark.parametrize('text, kept', [
    ('Run tools/summary.py and tell me how many ERROR lines it counts. Don\'t change anything.', True),
    ('Could you run tools/summary.py? Please don\'t change anything in this project.', True),
    ('Never change anything in this project. Run tools/summary.py and tell me how many ERROR lines it counts.', True),
    ("Don't change anything in tests/; fix the bug in calc.py so that `python3 build.py` writes dist/out.txt.", False),
    ('The log says "Read-only file system" when the export runs; fix scripts/export.py so it writes to out/ instead.',
     False),
    ('Add a read-only mode to the CLI (a --read-only flag) and run the tests.', False),
    ('Fix the off-by-one in pager/page.py. Don\'t change anything else.', False),
    ('Merge feature into main with a merge commit. Don\'t change anything on origin.', False),
    ('Run scripts/report.py to produce out/report.csv and tell me the total it prints. The script and data/orders.csv '
     'are reviewed, so don\'t change them.', False),
])
def test_only_a_whole_project_prohibition_takes_command_write_access(text, kept):
    assert keeps_whole_project(text) is kept


def test_the_8k_rule_hides_schemas_but_never_takes_write_access():
    from pebble.runner import read_only_goal
    text = ('Run scripts/report.py to produce out/report.csv and tell me the total it prints. The script and '
            'data/orders.csv are reviewed, so don\'t change them.')
    assert read_only_goal(text, False)[0] is True  # 8K and below: no file-effect schemas, as in 1.9.27
    assert keeps_whole_project(text) is False  # its commands still write out/report.csv
