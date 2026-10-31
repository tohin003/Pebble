"""Phase 45 (1.9.12): the person's D136 (a site or window allowed stays allowed for the conversation, up to its 15
minutes; a message in another conversation ends it) and D137 (in Full access an ordinary press or click in an allowed
window needs no card; anything that looks like sending, buying, deleting, saving, signing in or sharing still asks)."""
import uuid
from types import SimpleNamespace

import pytest

from pebble import screen_session as ss
from pebble.store import Store
from test_screen_session import (ALL, TITLE, App, Clock, FakeAX, Window, act, compose, el, grant, make,  # noqa: F401
                                 refused, target)


@pytest.fixture
def env(tmp_path):
    clock, ax = Clock(), FakeAX()
    app = App(4242, 1_700_000_000.25)
    ax.apps.append(app)
    window = app.add(Window('win-1', TITLE, compose() + [el('AXButton', 'w/options', title='Options',
                                                             frame=[300, 260, 60, 20])]))
    store = Store(tmp_path / 'home')
    sessions = make(store, ax, clock)
    yield SimpleNamespace(clock=clock, ax=ax, app=app, window=window, store=store, sessions=sessions,
                          chat=uuid.uuid4().hex, run=uuid.uuid4().hex)
    store.close()


def test_full_access_lowers_only_an_ordinary_press():
    assert ss.full_access_class('commit', ['press_activates'], True) == ('input', ['full_access', 'press_activates'])
    assert ss.full_access_class('commit', ['double_click_opens', 'press_activates'], True)[0] == 'input'
    for reasons in (['label_commit_word', 'press_activates'], ['leaves_origin'], ['key_chord'], ['no_label'],
                    ['outside_granted_window'], ['form_submit'], ['not_a_text_field'], []):
        assert ss.full_access_class('commit', reasons, True) == ('commit', reasons), reasons
    assert ss.full_access_class('commit', ['press_activates'], False) == ('commit', ['press_activates'])


def test_in_full_access_an_ordinary_press_runs_and_a_send_still_asks(env):
    sid = grant(env)
    assert act(env, sid, 'w/options', 'press')['status'] == 'needs_native_review', 'Ask and Auto keep the card'
    done = act(env, sid, 'w/options', 'press', full_access=True)
    assert done['status'] == 'done' and done['class'] == 'input'
    send = act(env, sid, 'w/send', 'press', full_access=True)
    assert send['status'] == 'needs_native_review' and 'label_commit_word' in send['reasons']


def test_the_next_task_in_the_conversation_keeps_the_window(env):
    sid = grant(env)
    nxt = uuid.uuid4().hex
    assert env.sessions.carry_over(env.chat, nxt, lambda run: run == env.run) == [sid]
    assert env.sessions.session_for_run(nxt)['id'] == sid
    assert env.sessions.observe(sid, nxt)['state'] == 'active'
    refused('wrong_run', env.sessions.observe, sid, env.run)
    payload = {'bundle_id': target()['bundle_id'], 'team_id': target()['team_id'], 'window': target()['window']}
    assert env.sessions.held_window(nxt, payload)['session_id'] == sid
    assert env.sessions.held_window(nxt, {**payload, 'bundle_id': 'com.example.other'}) is None


def test_a_live_task_keeps_its_window_and_another_conversation_ends_a_finished_ones(env):
    sid = grant(env)
    assert env.sessions.carry_over(env.chat, uuid.uuid4().hex, lambda run: False) == [], 'a live task keeps it'
    assert env.sessions.session_for_run(env.run)['id'] == sid
    assert env.sessions.carry_over(uuid.uuid4().hex, uuid.uuid4().hex, lambda run: True) == []
    [row] = env.sessions.sessions(env.chat)
    assert row['state'] == 'expired' and row['reason'] == {'reasons': ['left_the_conversation']}


def test_a_grant_past_its_lifetime_is_not_carried(env):
    sid = grant(env, ttl=60)
    env.clock.advance(61)
    assert env.sessions.carry_over(env.chat, uuid.uuid4().hex, lambda run: True) == []
    assert env.sessions.sessions(env.chat)[0]['state'] == 'expired' and sid
