"""Phase 46 (1.9.29, item 5 of the release; the memory of 2026-10-30): Apple's /usr/bin shims (git, make, cc, python3)
follow xcode-select, and with Xcode selected they died in Pebble's hand-built profiles, which read the Command Line
Tools and not Xcode ("xcrun: error: unable to load libxcrun").

w21's F2 gave RunCommand and StartCommand the Command Line Tools (terminal.developer_environment). Measured on this Mac
(Xcode selected) for the rest, before this change:
- RunTests under the framework Python: a project test that runs git, make or cc failed (its plan had no DEVELOPER_DIR;
  only a recipe whose toolchain is the CLT's set one). Now it gets the same helper at launch; a plan's own value wins.
- The Companion's Shell: the shims ran (it reads /Applications and runs Xcode.app's tools), so it keeps the person's
  choice, as their own terminal does; it now also runs them from an Xcode of another name that xcode-select chose
  (companion_shell.selected_developer, the rule git_workflows.confinement uses), never from a folder anyone can write.
- RunCode's worker, hooks and dependency installs run only the backend's or the framework's own Python, no shim.
"""
import os
import sys
import threading
from pathlib import Path

import pytest

from pebble import companion_shell, terminal
from pebble.testing import run_tests

CLT = Path('/Library/Developer/CommandLineTools')
XCODE = Path('/Applications/Xcode.app/Contents/Developer')
macos = pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file() or not CLT.is_dir(),
                           reason='the macOS sandbox and the Command Line Tools')
SHIMS = '\n'.join((
    'import os, subprocess, unittest', '', '',
    'class Shims(unittest.TestCase):',
    '    def test_shims_run(self):',
    '        for argv in (["/usr/bin/git", "--version"], ["/usr/bin/make", "--version"], ["/usr/bin/cc", "--version"]):',
    '            done = subprocess.run(argv, capture_output=True, text=True)',
    '            self.assertEqual(done.returncode, 0, done.stderr)',
    '            self.assertNotIn("libxcrun", done.stderr)',
    '        self.assertEqual(os.environ.get("DEVELOPER_DIR"), "/Library/Developer/CommandLineTools")', ''))


@macos
def test_a_project_test_that_runs_git_make_and_cc_passes_in_runtests(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'test_shims.py').write_text(SHIMS)
    result = run_tests({'root': str(project), 'recipe': 'python-unittest', 'drafts': []}, threading.Event())
    assert result['outcome'] == 'passed', result['output']


def test_the_helper_is_the_command_line_tools_or_nothing(monkeypatch):
    from pebble import sandbox_profiles
    assert terminal.developer_environment() == ({'DEVELOPER_DIR': str(CLT)} if CLT.is_dir() else {})
    monkeypatch.setattr(sandbox_profiles, 'CLT', Path('/nonexistent/CommandLineTools'))
    assert terminal.developer_environment() == {}  # without the CLT the shims follow xcode-select, as before


# -- the Companion follows the selection ----------------------------------------------------------------------------

@macos
@pytest.mark.parametrize('selected', [CLT, XCODE])
def test_the_companions_shims_run_under_either_selection(tmp_path, selected):
    if not selected.is_dir():
        pytest.skip(f'{selected} is not installed here')
    home = tmp_path / 'home'
    home.mkdir()
    # DEVELOPER_DIR is how a shim is pointed at a developer folder, as xcode-select's choice does
    line = '; '.join(f'DEVELOPER_DIR={selected} {tool}' for tool in (
        '/usr/bin/git --version', '/usr/bin/python3 -c "print(1)"', '/usr/bin/make --version')) + '; echo exit=$?'
    r = companion_shell.run(line, home=home)
    assert r['exit_code'] == 0 and 'libxcrun' not in r['output'] and 'exit=0' in r['output'], r['output']
    assert 'git version' in r['output'] and 'GNU Make' in r['output']


def test_only_a_root_owned_developer_folder_is_followed(monkeypatch, tmp_path):
    fake = tmp_path / 'Applications' / 'Xcode.app' / 'Contents' / 'Developer'
    (fake / 'usr' / 'bin').mkdir(parents=True)
    link = tmp_path / 'developer_dir'
    for target in (fake, tmp_path / 'tools', Path('/usr/local')):
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        monkeypatch.setattr(companion_shell, 'SELECTION_LINKS', (str(link),))
        assert companion_shell.selected_developer() is None, target  # not /Applications, or not root's
    monkeypatch.setattr(companion_shell, 'SELECTION_LINKS', (str(tmp_path / 'absent'),))
    assert companion_shell.selected_developer() is None


@macos
@pytest.mark.parametrize('selected', [CLT, XCODE])
def test_a_root_owned_selection_is_followed_into_the_shells_exec_paths(monkeypatch, tmp_path, selected):
    if not selected.is_dir():
        pytest.skip(f'{selected} is not installed here')
    link = tmp_path / 'developer_dir'
    link.symlink_to(selected)  # as `xcode-select -s` leaves it
    monkeypatch.setattr(companion_shell, 'SELECTION_LINKS', (str(tmp_path / 'absent'), str(link)))
    assert companion_shell.selected_developer() == selected
    assert selected in companion_shell.exec_paths(tmp_path)


def test_following_the_selection_costs_microseconds_a_shell_call(tmp_path):
    import time
    began = time.perf_counter()
    for _ in range(1000):
        companion_shell.selected_developer()
    assert (time.perf_counter() - began) / 1000 < 0.001  # two readlinks and three lstats at most


@macos
def test_this_macs_selection_is_followed_and_runs(tmp_path):
    selected = companion_shell.selected_developer()
    if selected is None:
        # Phase 47 (2.0.0's trial allows no skip): without a link (this Mac; macOS 26 keeps none until `xcode-select -s`
        # runs) xcode-select uses its default, Xcode.app or else the Command Line Tools, which the Shell already runs.
        default = XCODE if XCODE.is_dir() else CLT
        assert default in companion_shell.exec_paths(tmp_path) and os.access(default / 'usr' / 'bin', os.X_OK)
        return
    assert selected in (CLT, XCODE) or selected.parent.parent.parent == Path('/Applications')
    assert selected in companion_shell.exec_paths(tmp_path)
    assert os.access(selected / 'usr' / 'bin', os.X_OK)
