"""Phase 46: RunTests can test a src-layout Python project.

Measured first (offline, 1.9.26): a project with its package under src/ and nothing configuring pytest's pythonpath
failed both Python recipes (ModuleNotFoundError: -I ignores PYTHONPATH, and neither unittest discover nor pytest adds
src/), while the golden checkers run hidden tests with the root and src/ on PYTHONPATH and Codex and Claude Code type
PYTHONPATH=src. Pebble therefore could not verify a correct fix and reported it blocked (graded format_only: the
golden held-out coding_fix family had 3 such cells on each model).
"""

import os
import threading

import pytest

from pebble import recipes, testing
from pebble.policy import Workspace

TEST = 'import unittest\nfrom shop import total\n\n\nclass T(unittest.TestCase):\n    def test_total(self):\n' \
       '        self.assertEqual(total([1, 2]), 3)\n'


def project(tmp_path, src=True, extra=None):
    root = tmp_path / 'project'
    package = root / ('src/shop' if src else 'shop')
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('def total(xs):\n    return sum(xs)\n')
    (root / 'tests').mkdir()
    (root / 'tests' / '__init__.py').write_text('')
    (root / 'tests' / 'test_shop.py').write_text(TEST)
    for name, text in (extra or {}).items():
        (root / name).write_text(text)
    return Workspace(root)


def run(ws, recipe):
    return testing.run_tests({'root': str(ws.root), 'root_identity': list(ws.identity), 'recipe': recipe, 'drafts': [],
                              'plan': recipes.resolve(recipe, ws)}, threading.Event())


@pytest.mark.skipif(not os.path.exists('/usr/bin/sandbox-exec'), reason='RunTests is a macOS sandbox surface')
@pytest.mark.parametrize('recipe', ['python-unittest', 'python-pytest'])
def test_a_src_layout_project_runs_its_tests(tmp_path, recipe):
    result = run(project(tmp_path), recipe)
    assert result['outcome'] == 'passed' and result['tests']['passed'] == 1, result['output'][-300:]


def test_other_projects_keep_their_exact_plan(tmp_path):
    ws = project(tmp_path, src=False)
    assert recipes.resolve('python-unittest', ws)['argv'][1:] == list(recipes.REGISTRY['python-unittest'].argv)
    assert recipes.resolve('python-pytest', ws)['argv'][1:] == list(recipes.REGISTRY['python-pytest'].argv)


def test_a_project_that_sets_its_own_pythonpath_is_left_to_it(tmp_path):
    ws = project(tmp_path, extra={'pytest.ini': '[pytest]\npythonpath = src lib\n'})
    assert '-o' not in recipes.resolve('python-pytest', ws)['argv']
    src = project(tmp_path / 'b')
    assert recipes.resolve('python-pytest', src)['argv'][-2:] == ['-o', 'pythonpath=src']
    assert recipes.resolve('python-unittest', src)['argv'][1:3] == ['-I', '-c']
