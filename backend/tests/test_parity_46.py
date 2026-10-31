"""Phase 46 (1.9.27): Pebble's own checks run as the golden checkers run them, in both directions.

Measured first (offline, reading golden-2.0/dev/checkers/common.py beside 1.9.26's runners): the checkers run every
command and test with PYTHONDONTWRITEBYTECODE=1 and the project root, then src/, on PYTHONPATH. The terminal set
neither, so a script in scripts/ that imports the project's own package, and `python3 -m pkg.mod` in a src-layout
project, failed only in Pebble (a correct fix ended blocked, and a request's own output check could never pass), and
every Python run left __pycache__ in the project. The terminal's node was the last nvm folder in text order, not the
node RunTests (and the checker) run. And a terminal test command that ran one test module satisfied the completion
gate, while the checkers run every hidden test and the whole suite: a pass of some tests is no longer evidence.
"""

import json
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import async_terminal, guard, recipes, terminal
from pebble.controller import gate_sentences
from pebble.guard import GuardAdapter
from pebble.policy import Workspace

macos = pytest.mark.skipif(not os.path.exists('/usr/bin/sandbox-exec'), reason='the terminal is a macOS sandbox surface')
GOAL = 'Update calc.py so add documents its behaviour.'
SOURCE = 'def add(a, b):\n    return a + b\n'
CHANGED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
TESTS = ('import unittest\n\nfrom calc import add\n\n\n'
         'class AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n')
RULE = '-' * 70
PASSED = 'test_add (test_calc.AddTest.test_add) ... ok\n\n' + RULE + '\nRan 1 test in 0.001s\n\nOK\n'
FAILED = 'test_add (test_calc.AddTest.test_add) ... FAIL\n\n' + RULE + '\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n'


def test_commands_get_the_checkers_bytecode_setting_and_import_path(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    assert terminal.project_environment(root) == {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(root)}
    (root / 'src').mkdir()
    assert terminal.project_environment(root)['PYTHONPATH'] == f'{root}:{root / "src"}'
    linked = tmp_path / 'linked'
    linked.mkdir()
    (linked / 'src').symlink_to(root, target_is_directory=True)
    assert terminal.project_environment(linked)['PYTHONPATH'] == str(linked)  # a linked src/ is not the project's
    colon = tmp_path / 'a:b'
    colon.mkdir()
    assert terminal.project_environment(colon) == {'PYTHONDONTWRITEBYTECODE': '1'}  # ":" would split the entry
    exe = Path('/usr/bin/python3')
    assert async_terminal._environment(exe, str(tmp_path), root)['PYTHONPATH'] == f'{root}:{root / "src"}'
    shim = async_terminal._environment(exe, str(tmp_path))  # the shim's own environment names no project
    assert 'PYTHONPATH' not in shim and 'PYTHONDONTWRITEBYTECODE' not in shim


def run(root, command):
    checked = terminal.inspect_command(command, str(root))
    return terminal.run_command({'command': command, 'root': checked['root'], 'argv': checked['argv'],
                                 'root_identity': checked['root_identity'], 'writable': True}, threading.Event())


@macos
def test_a_script_in_a_folder_imports_the_projects_package_and_leaves_no_bytecode(tmp_path):
    root = tmp_path / 'project'
    (root / 'tallykit').mkdir(parents=True)
    (root / 'tallykit' / '__init__.py').write_text('VALUE = 41\n')
    (root / 'scripts').mkdir()
    (root / 'scripts' / 'show.py').write_text('import sys\n\nimport tallykit\n\n'
                                              'print(tallykit.VALUE + 1, sys.dont_write_bytecode)\n')
    result = run(root, 'python3 scripts/show.py')  # 1.9.26: ModuleNotFoundError: No module named 'tallykit'
    assert result['exit_code'] == 0 and '42 True' in result['output'], result['output']
    assert not list(root.rglob('__pycache__'))


@macos
def test_a_src_layout_module_runs_with_python_m(tmp_path):
    root = tmp_path / 'project'
    (root / 'src' / 'ledger').mkdir(parents=True)
    (root / 'src' / 'ledger' / '__init__.py').write_text('')
    (root / 'src' / 'ledger' / 'statement.py').write_text("print('statement ok')\n")
    result = run(root, 'python3 -m ledger.statement')  # 1.9.26: No module named ledger
    assert result['exit_code'] == 0 and result['output'].strip() == 'statement ok', result['output']


def test_the_terminal_runs_the_node_runtests_runs(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    for version in ('v8.17.0', 'v20.9.0', 'v20.18.0', 'v24.14.1'):
        node = home / '.nvm/versions/node' / version / 'bin/node'
        node.parent.mkdir(parents=True)
        node.write_text('')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(shutil, 'which', lambda name, *args, **kwargs: None)
    newest = (home / '.nvm/versions/node/v24.14.1/bin/node').resolve()
    assert recipes._node().resolve() == newest
    assert terminal.resolve_executable('node') == newest  # text order picked v8.17.0
    on_path = tmp_path / 'bin' / 'node'
    on_path.parent.mkdir()
    on_path.write_text('')
    monkeypatch.setattr(shutil, 'which', lambda name, *args, **kwargs: str(on_path) if name == 'node' else None)
    assert terminal.resolve_executable('node') == on_path.resolve() == recipes._node().resolve()


def test_node_from_the_installer_homebrew_or_volta_is_found_without_nvm_or_path(tmp_path, monkeypatch):
    # Phase 46 (recipes.NODES): a Dock-launched app's PATH has no node; nvm still wins when it is there
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(shutil, 'which', lambda name, *args, **kwargs: None)
    installer, brew = tmp_path / 'usr-local' / 'node', tmp_path / 'homebrew' / 'node'
    monkeypatch.setattr(recipes, 'NODES', (installer, brew))
    with pytest.raises(recipes.PolicyError, match='Install Node'):
        recipes._node()
    for node in (installer, brew, home / '.volta/bin/node'):
        node.parent.mkdir(parents=True)
        node.write_text('')
        node.chmod(0o755)
    assert recipes._node() == installer  # the installer, then Homebrew, then Volta
    installer.unlink()
    assert recipes._node() == brew
    brew.unlink()
    assert recipes._node() == home / '.volta/bin/node'
    nvm = home / '.nvm/versions/node/v24.14.1/bin/node'
    nvm.parent.mkdir(parents=True)
    nvm.write_text('')
    assert recipes._node() == nvm


@pytest.mark.parametrize('command, narrow', [
    ('python3 -m unittest', False), ('python3 -m unittest -v', False),
    ('python3 -m unittest discover -s tests -t . -v', False), ('python3 -m unittest discover tests', False),
    ('python3 -m unittest discover -p "*_test.py"', False), ('python3 -m pytest', False),
    ('python3 -m pytest -q -p no:cacheprovider', False), ('python3 -m pytest tests', False),
    ('python3 -m pytest --tb=short -x tests/', False), ('npm test', False),
    ('python3 -m shop.cli samples/order1.json', False),
    ('python3 -m unittest tests.test_rounding', True), ('python3 -m unittest tests/test_rounding.py -v', True),
    ('python3 -m unittest -k split', True), ('python3 -m unittest discover -s tests -p test_rounding.py', True),
    ('python3 -m unittest discover tests test_rounding.py', True), ('python3 -m pytest tests/test_rounding.py', True),
    ('python3 -m pytest tests/test_rounding.py::test_zero -q', True), ('python3 -m pytest -k rounding', True),
    ('python3 -m pytest -m "not slow"', True), ('python3 -m pytest --lf', True), ('pytest tests/test_rounding.py', True),
])
def test_a_command_that_names_some_tests_is_narrowed(command, narrow):
    assert guard.narrowed(command) is narrow


@pytest.fixture
def adapter(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    (project / 'test_calc.py').write_text(TESTS)
    adapter = GuardAdapter(tmp_path / 'data')
    assert adapter.guard is not None, adapter.error
    value = SimpleNamespace(guard=adapter, project=project, root=Workspace(project).root, rid='run-narrowed')
    assert adapter.begin(value.root, value.rid, GOAL)['state'] == 'started'
    (project / 'calc.py').write_text(CHANGED)
    adapter.stage(value.root, value.rid, 'calc.py', CHANGED)
    adapter.handle('post_tool_use', value.root, value.rid, tool='Write',
                   arguments={'file_path': 'calc.py', 'content': CHANGED}, result='{}')
    yield value
    adapter.close()


def terminal_run(env, command, output, exit_code):
    env.guard.handle('post_tool_use', env.root, env.rid, tool='Bash', arguments={'command': command},
                     result=json.dumps({'exit_code': exit_code, 'output': output}))


def stop(env):
    return env.guard.handle('stop', env.root, env.rid, last_assistant_message='Done; the tests pass.')


def test_a_pass_of_some_tests_is_no_evidence_and_the_repair_says_so(adapter):
    terminal_run(adapter, 'python3 -m unittest test_calc.AddTest.test_add', PASSED, 0)
    held = stop(adapter)
    assert held['action'] == 'block' and held['gate']['kind'] == 'no_test_run', held
    assert held['gate']['narrowed'] == 'terminal test run "python3 -m unittest test_calc.AddTest.test_add"'
    issue, notice = gate_sentences(held['gate'], held)
    assert 'ran only some of the tests' in issue and 'RunTests' in issue and 'no tests were run' not in issue
    assert notice == 'Pebble changed calc.py but ran only some of the tests that cover it.'
    terminal_run(adapter, 'python3 -m unittest -v', PASSED, 0)
    decision = stop(adapter)
    assert decision['action'] == 'allow' and decision['gate']['outcome'] == 'verified', decision


def test_a_failure_of_some_tests_still_holds(adapter):
    terminal_run(adapter, 'python3 -m unittest test_calc', FAILED, 1)
    held = stop(adapter)
    assert held['action'] == 'block' and held['gate']['kind'] == 'failing_tests', held
    assert held['gate']['narrowed'] is None
