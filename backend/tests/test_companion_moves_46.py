"""Phase 46: the companion Shell asks for the folders a desktop chore really writes, and never lets a refused write pass
as done.

Measured first (offline, 1.9.26, a synthetic home): `mv ~/Downloads/receipt-2026-10-*.pdf ~/Documents/Receipts/2026-10/`
asked for the literal glob as a folder, so after the person allowed it every rename was "Operation not permitted";
`find ~/Downloads ... -exec mv {} ... +`, a `for` loop and a python3 -c one-liner ran without asking, the sandbox refused
their writes, and `find` still exited 0 with nothing moved; `cd ~/Downloads && mv ... ../Documents/...` was refused
outright. On the golden held-out companion_desktop family 1.9.26 on Opus ended 2 cells false_completion and 2
honest_incomplete (Claude Code 13 of 15).
"""

import os
from pathlib import Path

import pytest

from pebble import companion_shell, shell_policy

MOVES = {
    'glob': 'mv ~/Downloads/receipt-2026-10-*.pdf ~/Documents/Receipts/2026-10/',
    'find': r'find ~/Downloads -name "receipt-2026-10-*" -exec mv {} ~/Documents/Receipts/2026-10/ \;',
    'loop': 'for f in ~/Downloads/receipt-2026-10-*; do mv "$f" ~/Documents/Receipts/2026-10/; done',
    'python': "python3 -c \"import glob, os, shutil; [shutil.move(f, os.path.expanduser('~/Documents/Receipts/2026-10/')) "
              "for f in glob.glob(os.path.expanduser('~/Downloads/receipt-2026-10-*'))]\"",
    'cd': 'cd ~/Downloads && mv receipt-2026-10-* ../Documents/Receipts/2026-10/'}


@pytest.fixture
def home(tmp_path):
    root = tmp_path / 'home'
    (root / 'Downloads').mkdir(parents=True)
    (root / 'Documents' / 'Receipts' / '2026-10').mkdir(parents=True)
    for name in ('receipt-2026-10-03-cafe.pdf', 'receipt-2026-10-17-hw.pdf', 'receipt-2026-09-30-train.pdf'):
        (root / 'Downloads' / name).write_text('pdf')
    return Path(os.path.realpath(root))


@pytest.mark.parametrize('kind', sorted(MOVES))
def test_each_way_to_move_the_receipts_asks_for_both_folders(home, kind):
    decision = shell_policy.decide(MOVES[kind], cwd=str(home), home=str(home), grants={})
    assert decision.action == 'ask'
    assert {str(Path(f).relative_to(home)) for f in decision.folders} == {'Downloads', 'Documents/Receipts/2026-10'}


@pytest.mark.skipif(not os.path.exists('/usr/bin/sandbox-exec'), reason='the Shell is a macOS sandbox surface')
@pytest.mark.parametrize('kind', sorted(MOVES))
def test_once_allowed_the_move_happens(home, kind):
    decision = shell_policy.decide(MOVES[kind], cwd=str(home), home=str(home), grants={})
    result = companion_shell.run(MOVES[kind], home=str(home), cwd=str(home), writable=[str(f) for f in decision.folders])
    assert result['exit_code'] == 0, result['output'][-300:]
    assert sorted(p.name for p in (home / 'Documents/Receipts/2026-10').iterdir()) == [
        'receipt-2026-10-03-cafe.pdf', 'receipt-2026-10-17-hw.pdf']
    assert sorted(p.name for p in (home / 'Downloads').iterdir()) == ['receipt-2026-09-30-train.pdf']


@pytest.mark.skipif(not os.path.exists('/usr/bin/sandbox-exec'), reason='the Shell is a macOS sandbox surface')
def test_a_refused_write_behind_exit_0_is_said_plainly(home):
    result = companion_shell.run(MOVES['find'], home=str(home), cwd=str(home), writable=[])
    assert result['exit_code'] == 0 and result['sandbox_refused'] is True and 'did not happen' in result['hint']
    assert len(list((home / 'Downloads').iterdir())) == 3


def test_plain_reads_still_run_without_asking(home):
    for line in ('ls ~/Downloads', 'find ~/Downloads -name "*.pdf"', 'ls ~/Documents/Receipts/2026-10 | wc -l'):
        assert shell_policy.decide(line, cwd=str(home), home=str(home), grants={}).action == 'allow'


@pytest.mark.parametrize('line', [
    'ls -la ~/Downloads | head -100; echo ---; ls -1d ~/Downloads/receipt-2026-10-* 2>/dev/null',
    'cd "$HOME/Downloads" && ls -la && echo --- && ls -1d receipt-2026-10-* 2>/dev/null',
    'ls ~/Documents/Receipts 2>&1 >/dev/null; echo done > /dev/stderr'])
def test_a_listing_that_discards_errors_is_not_a_write(home, line):
    # Phase 46 (live, dev d-cd-01 phrasing 3 on 1.9.26): three such listings refused in a row ended the task
    assert shell_policy.decide(line, cwd=str(home), home=str(home), grants={}).action == 'allow'


@pytest.mark.skipif(not os.path.exists('/usr/bin/sandbox-exec'), reason='the Shell is a macOS sandbox surface')
def test_the_sandbox_lets_errors_go_to_dev_null(home):
    result = companion_shell.run('ls -1d ~/Downloads/receipt-2026-10-* 2>/dev/null', home=str(home), cwd=str(home))
    assert result['exit_code'] == 0 and 'receipt-2026-10-03-cafe.pdf' in result['output']


@pytest.mark.parametrize('line, folders', [
    ('sort ~/Downloads/list.txt > "$TMPDIR/sorted.txt"', set()),        # the call's own temporary folder
    ('echo note > "$HOME/Documents/note.txt"', {'Documents/note.txt'}),  # $HOME is the home folder, not ./$HOME
    ('zip -j ~/Documents/receipts.zip ~/Downloads/receipt-2026-10-*', {'Documents'}),
    ('tar -czf ~/Documents/r.tgz -C ~/Downloads .', {'Documents'}),
    ('unzip ~/Downloads/a.zip -d ~/Documents/a', {'Documents/a'}),
    ('tar -tzf ~/Downloads/a.tgz', set())])
def test_scratch_home_and_archive_writes_ask_for_the_right_folder(home, line, folders):
    decision = shell_policy.decide(line, cwd=str(home), home=str(home), grants={})
    assert {str(Path(f).relative_to(home)) for f in decision.folders} == folders
    assert decision.action == ('ask' if folders else 'allow')


@pytest.mark.skipif(not os.path.exists('/usr/bin/sandbox-exec'), reason='the Shell is a macOS sandbox surface')
def test_once_allowed_a_zip_is_written(home):
    line = 'zip -j ~/Documents/receipts.zip ~/Downloads/receipt-2026-10-*'
    decision = shell_policy.decide(line, cwd=str(home), home=str(home), grants={})
    result = companion_shell.run(line, home=str(home), cwd=str(home), writable=[str(f) for f in decision.folders])
    assert result['exit_code'] == 0 and (home / 'Documents' / 'receipts.zip').is_file()
