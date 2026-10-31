"""Phase 45 (1.9.10, D133): the companion answers simple cards only (a site, a window, bringing a window forward) through
approve_simple and reject_simple; every other card is refused there and stays the workspace's."""
import pytest

from pebble.app import App, SIMPLE_CARDS
from pebble.policy import PolicyError
from pebble.tool_authz import NATIVE_ONLY


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield a
    a.close()


def pending(app, chat, kind, payload=None):
    action = app.store.action(chat['id'], kind, payload or {'requires_review': True})
    return action['id'] if isinstance(action, dict) else action


def test_the_simple_kinds_and_the_routes_are_native_only():
    assert SIMPLE_CARDS == {'browser_grant', 'screen_grant', 'screen_raise'}
    assert {'approve_simple', 'reject_simple'} <= NATIVE_ONLY, 'never a model tool'


def test_a_simple_card_is_declined_in_the_companion_and_any_other_is_refused(app):
    chat = app.new_chat()
    grant = pending(app, chat, 'screen_grant', {'app': 'Calculator', 'requires_review': True})
    edit = pending(app, chat, 'edit', {'path': 'calc.py', 'requires_review': True})
    with pytest.raises(PolicyError, match='Review this one in the workspace'):
        app.dispatch('approve_simple', {'id': chat['id'], 'action_id': edit})
    with pytest.raises(PolicyError, match='Review this one in the workspace'):
        app.dispatch('reject_simple', {'id': chat['id'], 'action_id': edit})
    app.dispatch('reject_simple', {'id': chat['id'], 'action_id': grant})
    states = {a['id']: a['status'] for a in app.store.actions(chat['id'])}
    assert states[grant] == 'rejected' and states[edit] == 'pending'
    with pytest.raises(PolicyError, match='no longer awaiting approval'):
        app.dispatch('reject_simple', {'id': chat['id'], 'action_id': grant})
