"""Phase 21 merge (I21): what the merge adds beside the five C1 fix tracks' own tests.

1. F21-investigate's optional diagnostics patch. With opt-in private diagnostics, each cell's grader reason codes (the
   construct a static gate refused, for example) sit beside its private reply and run error. Only plain codes are
   kept, and they never reach the public report or the analysis (public_verdict drops a verdict's reasons).
2. F21-fd's parent patch P2, guarded. An App's AgentGuard database connection (three descriptors) now closes with the
   App, but only when no other adapter in the process is still open. AGENTGUARD_HOME is process-global, so another
   open App's workspaces may be bound to that connection; then it stays open, as before.

No model calls and no network.
"""
import json
import os
import weakref
from pathlib import Path

import pytest

from pebble import evaluation as ev
from pebble.app import App
from pebble.guard import GuardAdapter

from test_eval_paired_18 import (TASKS, Setup, corpus, grade, no_network_or_live_route,  # noqa: F401 (autouse)
                                 stable_source)
from test_high_fd_waits_21 import close_guard_databases, descriptor_path

CODE = 'name_not_allowed'  # a reason code eval_corpus_18.static_gate gives


def gated(task, evidence, workdir, *, fixture_log=None):
    """The stand-in grader failing a static gate: one plain reason code and one free-text reason."""
    verdict = grade(task, evidence, workdir, fixture_log=fixture_log)
    return {**verdict, 'passed': False, 'checks': {**verdict['checks'], 'static_gate': False},
            'reasons': [*verdict['reasons'], 'static_gate', CODE, 'refused: type(worked) is not int']}


def test_grader_reason_codes_reach_only_the_private_diagnostics(tmp_path):
    module = corpus(*TASKS[:2], version='18t.r')
    module.grade = gated
    setup = Setup(tmp_path, module=module)
    report = setup.run(diagnostics=True)
    [path] = list((tmp_path / 'ledger').glob('*.diagnostics.json'))
    items = json.loads(path.read_text())
    assert [(i['task_id'], i['arm']) for i in items] == [(c['task_id'], c['arm']) for c in report['cells']]
    for item in items:
        assert item['grader_reasons'][-2:] == ['static_gate', CODE], item
        assert all(ev.PUBLIC_NAME.fullmatch(reason) for reason in item['grader_reasons'])  # free text is dropped
    public = json.dumps(report) + json.dumps(setup.analyse([report]))
    assert CODE not in public and 'type(worked)' not in public and 'grader_reasons' not in public


@pytest.fixture
def open_adapters(monkeypatch):
    """Only this test's adapters count as open: another test's unclosed App must not decide what closes here."""
    monkeypatch.setattr(GuardAdapter, '_open', weakref.WeakSet())


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'a.py').write_text('x = 1\n')
    return root


def guard_files(home):
    """This process's open descriptors on an AgentGuard database under home (the database, its WAL and shm)."""
    home = os.path.realpath(home) + os.sep
    paths = (descriptor_path(int(name)) for name in os.listdir('/dev/fd'))
    return sorted(p for p in paths if p and p.startswith(home) and Path(p).name.startswith('agentguard.db'))


def host(home):
    return App(home, home.parent / 'resources', lambda *args: None)


def test_p2_the_last_open_app_closes_its_agentguard_connection(tmp_path, open_adapters, project):
    for run in ('r1', 'r2'):  # the second App is a restart on the same home
        app = host(tmp_path / 'data')
        assert app.guard.begin(str(project), run, 'Fix a.py.')['state'] == 'started'
        assert guard_files(tmp_path / 'data')
        app.close()
        assert guard_files(tmp_path / 'data') == []
    assert close_guard_databases(tmp_path) == 0  # nothing is left for a test-side cleanup


def test_p2_an_app_still_open_keeps_the_connection_its_workspaces_may_share(tmp_path, open_adapters, project):
    first, second = host(tmp_path / 'a'), host(tmp_path / 'b')
    try:
        # AGENTGUARD_HOME now names b's folder, so first's new workspace is bound to b's database.
        assert first.guard.begin(str(project), 'r1', 'Fix a.py.')['state'] == 'started'
        second.close()
        assert guard_files(tmp_path / 'b')  # first still uses it
        assert first.guard.begin(str(project), 'r2', 'Fix a.py.')['state'] == 'started'
    finally:
        first.close()
    # Shared across homes, it outlives both Apps as it did before P2; this test closes its own.
    assert close_guard_databases(tmp_path / 'b') == 1 and guard_files(tmp_path / 'b') == []
