"""Phase 46: everyday spellings of reviewed Git commands that were refused for their spelling alone.

Measured first (offline, git_commands.parse on 123 everyday commands, 1.9.26): 30 refused, among them `commit -am`,
a commit message given as `-m "subject" -m "body"` and `merge -X theirs`. Each refusal costs the run a step and, with
several, ends the task; the golden held-out git_workflow family ended 6 cells (sol) and 3 (Opus) honest_incomplete.
"""

import pytest

from pebble import git_commands
from pebble.policy import PolicyError


@pytest.mark.parametrize('argv, canonical', [
    (['commit', '-am', 'Fix parse'], ['commit', '--all', '-m', 'Fix parse']),
    (['commit', '-m', 'Fix parse', '-m', 'Empty lines no longer crash.'],
     ['commit', '-m', 'Fix parse\n\nEmpty lines no longer crash.']),
    (['merge', '-X', 'theirs', 'feature'], ['merge', '-X', 'theirs', '--no-edit', 'feature']),
    (['merge', '--strategy-option=ours', '-m', 'Merge feature', 'feature'],
     ['merge', '-X', 'ours', '-m', 'Merge feature', 'feature'])])
def test_everyday_spellings_parse_to_their_canonical_command(argv, canonical):
    assert git_commands.parse(argv).argv == canonical


@pytest.mark.parametrize('argv', [['commit', '-ax', 'msg'], ['merge', '-X', 'patience', 'feature'],
                                  ['commit', '--amend', '--amend', '--no-edit'], ['merge', '-X', 'ours', '-X', 'theirs', 'f']])
def test_anything_else_is_still_refused(argv):
    with pytest.raises(PolicyError):
        git_commands.parse(argv)


def test_reads_point_at_the_tools_that_now_show_the_refs():
    with pytest.raises(PolicyError, match='stash entries'):
        git_commands.parse(['stash', 'list'])
    with pytest.raises(PolicyError, match='origin/main'):
        git_commands.parse(['log', 'origin/main'])
