"""Native root stop/pause respects the shared local inference owner."""

import threading

import pytest

from test_agent_manager import env, root


@pytest.mark.parametrize('action', ['stop', 'pause'])
@pytest.mark.parametrize('owns_lane', [False, True])
def test_native_root_cancel_does_not_interrupt_another_local_owner(env, monkeypatch, action, owns_lane):
    cid, rid = root(env)
    monkeypatch.setattr(env.app.model, 'current', lambda: {'kind': 'gguf'})
    interrupted = []
    monkeypatch.setattr(env.app.model, 'cancel', lambda: interrupted.append('local'))
    owner = 'root:' + rid if owns_lane else 'child:independent-task'
    with env.app.agents.lane.local(owner, threading.Event()):
        if action == 'pause':
            env.app.run_pause(cid, rid)
        else:
            env.app.stop(cid, rid)
        assert env.app.cancel.is_set()
        assert interrupted == (['local'] if owns_lane else [])
