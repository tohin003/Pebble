"""Phase 46 (1.9.27): the Companion Shell's two measured refusals behind companion_desktop cells (golden dev d-cd-01
on sol: a heredoc refused; golden held-out companion_desktop on Opus: 2 false completions and 2 honest incompletes of
15). Real runs under sandbox-exec on a throwaway home folder, as test_companion_shell_40.

- zsh writes here-document and =(...) temporary files under TMPPREFIX (default /tmp/zsh; zsh manual, Parameters), not
  TMPDIR; the sandbox denies /tmp, so every heredoc failed with "can't create temp file for here document: operation
  not permitted" (zsh 5.9, measured). Claude Code's sandbox had the same fault (its tracker, issues 17989 and 25990).
- shell_policy opened the literal path a command named: a glob ("~/Downloads/receipt-2026-10-*") or a folder that
  did not exist yet ("~/Documents/Receipts/2026-10" before mkdir -p), and "$HOME/..." was read as a workbench path.
  Measured under sandbox-exec: with only those paths allowed, `mv` and `mkdir -p` fail with "Operation not
  permitted" after the person approved the command; the nearest existing folder makes them work.
"""
import os
import sys
from pathlib import Path

import pytest

from pebble import companion_shell as cs
from pebble import shell_policy as sp

MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
macos = pytest.mark.skipif(not MAC, reason='macOS sandbox only')


@pytest.fixture
def home(tmp_path):
    root = Path(os.path.realpath(tmp_path / 'home'))
    (root / 'Downloads').mkdir(parents=True)
    (root / 'Documents' / 'Receipts' / '2026-09').mkdir(parents=True)
    (root / 'Pebble Workbench').mkdir()
    for name in ('receipt-2026-10-03-cafe.pdf', 'receipt-2026-10-17-hardware.pdf', 'receipt-2026-09-28-books.pdf',
                 'invoice-2026-10-05-studio.pdf'):
        (root / 'Downloads' / name).write_bytes(b'%PDF ' + name.encode())
    return root


def test_the_shell_environment_keeps_zsh_temporary_files_inside_the_job(tmp_path):
    env = cs.environment(tmp_path / 'home', tmp_path / 'temp')
    assert env['TMPDIR'] == str(tmp_path / 'temp') + '/'
    assert env['TMPPREFIX'] == str(tmp_path / 'temp') + '/zsh'


@macos
def test_a_heredoc_and_a_process_substitution_work_in_the_sandbox(home):
    r = cs.run("cat <<'EOF'\nhello from a heredoc\nEOF", home=home)
    assert r['exit_code'] == 0 and r['output'] == 'hello from a heredoc\n', r
    r = cs.run('cat =(echo substituted)', home=home)
    assert r['exit_code'] == 0 and r['output'] == 'substituted\n', r
    assert not list(home.glob('zsh*')) and not list((home / 'Pebble Workbench').glob('zsh*'))


def decide(line, home, **kw):
    return sp.decide(line, cwd=str(home / 'Pebble Workbench'), home=home, grants={'hosts': {}, 'folders': {}}, **kw)


def test_a_move_by_glob_asks_for_the_folders_that_hold_the_files(home):
    d = decide('mkdir -p ~/Documents/Receipts/2026-10 && mv ~/Downloads/receipt-2026-10-*.pdf ~/Documents/Receipts/2026-10/', home)
    assert d.action == 'ask'
    # the new folder keeps its exact path (mkdir -p works with it: measured); the glob opens the folder it expands in
    assert sorted(d.folders) == sorted([str(home / 'Documents' / 'Receipts' / '2026-10'), str(home / 'Downloads')])
    assert any(g.startswith('deleting or moving') for g in d.gates)  # the move away still asks every time
    assert not any('*' in f for f in d.folders)


def test_a_quoted_home_variable_names_the_persons_home(home):
    d = decide('mv "$HOME/Downloads/receipt-2026-10-03-cafe.pdf" ~/Documents/Receipts/2026-09/', home)
    # integrated with the checkout's targets(): a literal file asks for its own path (a single-file mv and rm work with
    # it in the real sandbox, measured 2026-10-28 16:40); a glob asks for its folder (the test above)
    assert sorted(d.folders) == sorted([str(home / 'Documents' / 'Receipts' / '2026-09'),
                                        str(home / 'Downloads' / 'receipt-2026-10-03-cafe.pdf')])
    assert str(home / 'Pebble Workbench') not in ' '.join(d.folders + d.gates)
    assert sp._resolve('${HOME}/Desktop', str(home), home) == home / 'Desktop'
    assert sp._resolve('~other/x', str(home), home) is None


@macos
def test_the_approved_move_then_works_in_the_sandbox(home):
    line = 'mkdir -p ~/Documents/Receipts/2026-10 && mv ~/Downloads/receipt-2026-10-*.pdf ~/Documents/Receipts/2026-10/'
    d = decide(line, home)
    r = cs.run(line, home=home, writable=d.folders)
    assert r['exit_code'] == 0, r
    moved = sorted(p.name for p in (home / 'Documents' / 'Receipts' / '2026-10').iterdir())
    assert moved == ['receipt-2026-10-03-cafe.pdf', 'receipt-2026-10-17-hardware.pdf']
    assert sorted(p.name for p in (home / 'Downloads').iterdir()) == ['invoice-2026-10-05-studio.pdf',
                                                                      'receipt-2026-09-28-books.pdf']
    assert (home / 'Documents' / 'Receipts' / '2026-10' / 'receipt-2026-10-03-cafe.pdf').read_bytes() == \
        b'%PDF receipt-2026-10-03-cafe.pdf'


@macos
def test_the_old_literal_paths_let_the_approved_move_fail(home):
    """The measured failure: the glob and the missing leaf as the only writable paths."""
    line = 'mkdir -p ~/Documents/Receipts/2026-10 && mv ~/Downloads/receipt-2026-10-*.pdf ~/Documents/Receipts/2026-10/'
    r = cs.run(line, home=home, writable=[str(home / 'Downloads' / 'receipt-2026-10-*.pdf'),
                                          str(home / 'Documents' / 'Receipts' / '2026-10')])
    assert r['exit_code'] != 0 and 'Operation not permitted' in r['output'] and 'writable=' in r.get('hint', '')
    assert (home / 'Downloads' / 'receipt-2026-10-03-cafe.pdf').exists()
