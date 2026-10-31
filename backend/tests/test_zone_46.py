"""Phase 46: the commands Pebble runs for the person keep the person's time zone setting (TZ).

Measured first (offline, 1.9.26): RunTests, the terminal, background jobs, RunCode and the companion shell built their
environments without TZ, so with TZ=UTC (every golden case sets it for every product; Codex's and Claude Code's shells
inherit it) Pebble's tests ran at the Mac's UTC+05:30 while the checker, the competitors and the person's own terminal
used UTC. A date-dependent test then failed only in Pebble, which reported the fix as blocked.
"""

import os
import threading

import pytest

from pebble import async_terminal, companion_shell, recipes, testing, zone
from pebble.policy import Workspace

ZONE_TEST = '''import os, time, unittest


class Zone(unittest.TestCase):
    def test_the_persons_zone(self):
        self.assertEqual(os.environ.get('TZ'), 'UTC')
        self.assertEqual(time.strftime('%z', time.localtime(0)), '+0000')
'''


@pytest.mark.parametrize('value, kept', [('UTC', True), ('Europe/Berlin', True), (':America/New_York', True),
                                         ('UTC0', True), ('a b', False), ('x' * 65, False), ('UTC\x00', False)])
def test_only_a_well_formed_zone_is_passed_on(monkeypatch, value, kept):
    monkeypatch.setenv('TZ', value) if '\x00' not in value else monkeypatch.setattr(os, 'environ', {'TZ': value})
    assert zone.person_zone() == ({'TZ': value} if kept else {})


def test_without_a_zone_nothing_is_added(monkeypatch):
    monkeypatch.delenv('TZ', raising=False)
    assert zone.person_zone() == {}
    assert 'TZ' not in async_terminal._environment(recipes.Path('/usr/bin/python3'), '/tmp')


def test_the_launchers_carry_it(monkeypatch, tmp_path):
    monkeypatch.setenv('TZ', 'UTC')
    assert async_terminal._environment(recipes.Path('/usr/bin/python3'), str(tmp_path))['TZ'] == 'UTC'
    assert companion_shell.environment(tmp_path, tmp_path)['TZ'] == 'UTC'


@pytest.mark.skipif(not os.path.exists('/usr/bin/sandbox-exec'), reason='RunTests is a macOS sandbox surface')
def test_run_tests_runs_the_project_in_the_persons_zone(monkeypatch, tmp_path):
    monkeypatch.setenv('TZ', 'UTC')
    root = tmp_path / 'project'
    (root / 'tests').mkdir(parents=True)
    (root / 'tests' / '__init__.py').write_text('')
    (root / 'tests' / 'test_zone.py').write_text(ZONE_TEST)
    ws = Workspace(root)
    plan = recipes.resolve('python-unittest', ws)
    assert 'TZ' not in plan['env']  # the reviewed plan is unchanged; the zone joins at launch
    result = testing.run_tests({'root': str(ws.root), 'root_identity': list(ws.identity), 'recipe': 'python-unittest',
                                'drafts': [], 'plan': plan}, threading.Event())
    assert result['outcome'] == 'passed', result['output'][-400:]
