"""Actual confined terminal outcomes and the shared Git metadata boundary."""
import sys
import threading
import time

import pytest

from pebble.terminal import CommandOutcomeUncertain, inspect_command, run_command
from pebble.policy import PolicyError


@pytest.mark.parametrize('command', ['git status', '/usr/bin/git diff', 'cat .git/config', 'cat .GIT/HEAD'])
def test_generic_terminal_cannot_bypass_typed_git_review(tmp_path, command):
    with pytest.raises(PolicyError):
        inspect_command(command, tmp_path)


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual macOS confinement')
def test_admitted_scripts_cannot_read_or_mutate_git_metadata(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    metadata = root / '.git'
    metadata.mkdir()
    (metadata / 'HEAD').write_text('ref: refs/heads/main\n')
    (root / 'probe.py').write_text(
        "from pathlib import Path\n"
        "for name in ['.git/HEAD', '.GIT/HEAD']:\n"
        " try: print('LEAK', Path(name).read_text())\n"
        " except (PermissionError,FileNotFoundError): print('READ DENIED')\n"
        "try: Path('.git/HEAD').write_text('changed')\n"
        "except PermissionError: print('WRITE DENIED')\n"
        "Path('ordinary.txt').write_text('allowed')\n")
    checked = inspect_command('python3 probe.py', root)
    result = run_command({**checked, 'writable': True}, threading.Event())
    assert result['exit_code'] == 0, result
    assert result['output'].count('READ DENIED') == 2 and 'WRITE DENIED' in result['output']
    assert 'LEAK' not in result['output']
    assert (metadata / 'HEAD').read_text() == 'ref: refs/heads/main\n'
    assert (root / 'ordinary.txt').read_text() == 'allowed'


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual macOS confinement')
def test_cancelled_admitted_command_retains_partial_effect_and_reports_uncertainty(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'partial.py').write_text("from pathlib import Path\nimport time\nPath('effect.txt').write_text('committed by process')\ntime.sleep(60)\n")
    cancel = threading.Event()
    observed = threading.Event()

    def stop_after_effect():
        until = time.monotonic() + 8
        while time.monotonic() < until:
            if (root / 'effect.txt').exists():
                observed.set()
                cancel.set()
                return
            time.sleep(.01)
        cancel.set()

    watcher = threading.Thread(target=stop_after_effect)
    watcher.start()
    try:
        with pytest.raises(CommandOutcomeUncertain, match='outcome is uncertain'):
            run_command({**inspect_command('python3 partial.py', root), 'writable': True}, cancel)
    finally:
        cancel.set()
        watcher.join(timeout=8)
    assert not watcher.is_alive() and observed.is_set()
    assert (root / 'effect.txt').read_text() == 'committed by process'
