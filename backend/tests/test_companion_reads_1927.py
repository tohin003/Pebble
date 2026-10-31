"""Phase 46 (1.9.27, the lead's review of the integrated companion Shell fix): a command the policy cannot fully read
(a loop, find -exec, xargs, an interpreter's inline code) asks for the home folders it names only when it can write.
Before this, a read-only loop over ~/Documents asked "It writes outside the workbench: ~/Documents" for writes it never
made; the moves the fix was made for still ask (test_companion_moves_46), and a write the policy misses is still refused
by the sandbox and reported (companion_shell's sandbox_refused hint).
"""

import os
from pathlib import Path

import pytest

from pebble import shell_policy


@pytest.fixture
def home(tmp_path):
    root = tmp_path / 'home'
    (root / 'Downloads').mkdir(parents=True)
    (root / 'Documents').mkdir(parents=True)
    (root / 'Downloads' / 'a.txt').write_text('a\n')
    return Path(os.path.realpath(root))


@pytest.mark.parametrize('line', [
    'for f in ~/Documents/*.txt; do wc -l "$f"; done',
    r'find ~/Downloads -name "*.pdf" -exec ls -l {} \;',
    'ls ~/Downloads | xargs -n1 echo',
    "python3 -c \"import os; print(sorted(os.listdir(os.path.expanduser('~/Downloads'))))\"",
    'for f in ~/Downloads/*; do echo "$f"; done 2>/dev/null',
    'for f in ~/Downloads/*.txt; do head -1 "$f" 2>&1; done'])
def test_a_command_that_only_reads_asks_for_no_folder(home, line):
    decision = shell_policy.decide(line, cwd=str(home), home=str(home), grants={})
    assert decision.folders == []


@pytest.mark.parametrize('line, folders', [
    # a loop's writes cannot be told from its reads: every home folder it names is asked for
    ('for f in ~/Downloads/*.txt; do cat "$f" >> ~/Documents/all.txt; done', {'Documents/all.txt', 'Downloads', 'Documents'}),
    ("python3 -c \"import os; open(os.path.expanduser('~/Documents/x.txt'), 'w').write('x')\"", {'Documents'}),
    ('ls ~/Downloads/*.txt | xargs -I{} cp {} ~/Documents/', {'Documents'}),  # the parser reads xargs' cp: its target
    (r"find ~/Downloads -name '*.txt' -exec sed -i '' 's/a/b/' {} \;", {'Downloads'})])
def test_a_command_that_can_write_still_asks_for_the_folders_it_names(home, line, folders):
    decision = shell_policy.decide(line, cwd=str(home), home=str(home), grants={})
    assert decision.action == 'ask'
    assert {str(Path(f).relative_to(home)) for f in decision.folders} == folders
