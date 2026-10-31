"""Phase 45 (1.9.15, F228 from audit round R9): Past experiences said "No file, command or app actions" for tasks that ran
commands, wrote a file and pressed buttons, because only steps that went through a card were listed. The record also
counts the run's finished steps by kind (no arguments, no results)."""
from types import SimpleNamespace

from pebble import runner


def test_a_runs_finished_steps_are_counted_by_kind():
    tools = [{'name': 'Shell', 'state': 'completed'}, {'name': 'Shell', 'state': 'completed'},
             {'name': 'WriteFile', 'state': 'completed'}, {'name': 'ScreenAct', 'state': 'completed'},
             {'name': 'ScreenAct', 'state': 'failed'}, {'name': 'ScreenObserve', 'state': 'completed'},
             {'name': 'OpenApp', 'state': 'completed'}]
    app = SimpleNamespace(runs=SimpleNamespace(tools=lambda run_id: tools))
    assert runner.episode_steps(app, 'r1') == {'command': 2, 'file': 1, 'app_step': 1, 'app_opened': 1}
    broken = SimpleNamespace(runs=SimpleNamespace(tools=lambda run_id: (_ for _ in ()).throw(RuntimeError())))
    assert runner.episode_steps(broken, 'r1') == {}


def test_refused_steps_are_counted_apart():
    """F242 (audit round R10): "6 steps in app windows" were one press and five clicks Pebble's own checks refused."""
    tools = [{'name': 'ScreenAct', 'state': 'completed', 'result': {'status': 'done', 'verdict': 'not_observed'}}] + [
        {'name': 'ScreenAct', 'state': 'completed', 'result': {'error': 'The spot changed since Pebble looked at it.'}}] * 5
    app = SimpleNamespace(runs=SimpleNamespace(tools=lambda run_id: tools))
    assert runner.episode_steps(app, 'r1') == {'app_step': 1, 'refused': 5}
