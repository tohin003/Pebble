"""Phase 43 (1.9.0, M1): screen sessions over the worker's protocol 2. A target may name its window by number (the
title is then only a hint); the window's lifetime check; the versioned observation envelope; a minimized or
off-desktop window brought forward after one approval per session; Chromium page accessibility turned on once per app
instance and restored at its last session's end; typed act results; the window request naming the number; capability
discovery and its HOST CAPABILITIES line."""
import json
import uuid
from types import SimpleNamespace

import pytest

from pebble import ax_worker, runner, screen_session as ss
from pebble.ax_worker import AXError
from pebble.screen_session import ScreenRefused
from pebble.store import Store
from test_screen_session import ALL, BUNDLE, TEAM, TITLE, App, Clock, FakeAX, Window, act, compose, make, refused


class Window43(Window):
    """A protocol-2 window: named n<number>, with its listed, minimized and subrole facts."""

    def __init__(self, number, title, elements, *, listed=True, minimized=False, subrole='AXStandardWindow', **kw):
        super().__init__(f'n{number}', title, elements, **kw)
        self.number, self.listed, self.minimized, self.subrole = number, listed, minimized, subrole

    def raw(self):
        return {**super().raw(), 'window_number': self.number, 'listed': self.listed, 'main': self.focused,
                'minimized': self.minimized, 'subrole': self.subrole}


class FakeAX43(FakeAX):
    """FakeAX with enhance, restore and raise; processes name the bundle path (for the Chromium check)."""

    def __init__(self):
        super().__init__()
        self.path, self.filled, self.frontmost = None, True, True
        self.enhances, self.restores, self.raises = [], [], []

    def processes(self, bundle_id):
        return [{**row, 'path': self.path} for row in super().processes(bundle_id)]

    def enhance(self, pid, start):
        self._app(pid, start)
        self.enhances.append(pid)
        return {'original': {'AXManualAccessibility': False, 'AXEnhancedUserInterface': None},
                'changed': ['AXManualAccessibility'], 'refused': [], 'filled': self.filled, 'ms': 120}

    def restore(self, pid, start, changed):
        self.restores.append(dict(changed))
        return {'restored': [k for k, v in changed.items() if v is not None],
                'left': [k for k, v in changed.items() if v is None]}

    def raise_window(self, pid, start, ref):
        window = self._app(pid, start).windows[ref]
        self.raises.append(ref)
        window.minimized, window.listed = False, True
        return {'raised': True, 'frontmost': self.frontmost}


def chromium_bundle(tmp_path, name='Comet'):
    path = tmp_path / f'{name}.app'
    (path / 'Contents/Frameworks' / f'{name} Framework.framework/Versions/155.0/Helpers'
     / f'{name} Helper (Renderer).app').mkdir(parents=True)
    return str(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv('PEBBLE_AX_ENHANCE', raising=False)
    clock, ax = Clock(), FakeAX43()
    app = App(4242, 1_700_000_000.25)
    ax.apps.append(app)
    window = app.add(Window43(7001, TITLE, compose()))
    store = Store(tmp_path / 'home')
    sessions = make(store, ax, clock)
    yield SimpleNamespace(clock=clock, ax=ax, app=app, window=window, store=store, sessions=sessions, tmp=tmp_path,
                          chat=uuid.uuid4().hex, run=uuid.uuid4().hex)
    store.close()


def target(number=7001, title=TITLE):
    return {'kind': 'app', 'bundle_id': BUNDLE, 'team_id': TEAM,
            'window': {'title_sha256': ax_worker.sha256(title), 'ax_identifier': None, 'window_number': number}}


def grant(env, tgt=None):
    return env.sessions.grant(env.chat, env.run, tgt or target(), ALL, 600)['session_id']


# ---- targets and window identity (decision 7) --------------------------------------------------------------------

def test_a_target_may_name_its_window_number_and_an_old_target_keeps_its_shape():
    assert ss.validate_target(target())['window']['window_number'] == 7001
    old = {'kind': 'app', 'bundle_id': BUNDLE, 'team_id': TEAM,
           'window': {'title_sha256': ax_worker.sha256(TITLE), 'ax_identifier': None}}
    assert ss.validate_target(old) == old, 'a 1.8.0 target (and so its digest) is unchanged'
    for bad in (0, -1, 1 << 32, '7001', 7.0, True, None):
        refused('invalid', ss.validate_target, target(number=bad))
    refused('invalid', ss.validate_target, {**old, 'window': {**old['window'], 'extra': 1}})


def test_a_window_named_by_number_survives_its_title_changing(env):
    sid = grant(env, target(title='the title the card showed'))
    env.window.title = 'Draft — Notes (edited)'
    view = env.sessions.observe(sid, env.run)
    assert view['observation_id'] and view['window']['title'] == 'Draft — Notes (edited)'


def test_a_title_target_is_pinned_by_number_when_the_worker_names_one(env):
    old = {'kind': 'app', 'bundle_id': BUNDLE, 'team_id': TEAM,
           'window': {'title_sha256': ax_worker.sha256(TITLE), 'ax_identifier': None}}
    sid = env.sessions.grant(env.chat, env.run, old, ALL, 600)['session_id']
    pin = json.loads(env.store.query('SELECT pin FROM screen_sessions WHERE session_id=?', (sid,))[0]['pin'])
    assert pin['window_ref'] == 'n7001' and pin['subrole'] == 'AXStandardWindow'
    env.window.title = 'Renamed by the app'
    assert env.sessions.observe(sid, env.run)['observation_id']


def test_a_number_that_now_names_another_kind_of_window_is_gone(env):
    sid = grant(env)
    env.window.subrole = 'AXDialog'
    refused('target_gone', env.sessions.observe, sid, env.run)


def test_a_number_no_window_has_is_not_found_at_the_grant(env):
    refused('window_not_found', env.sessions.grant, env.chat, env.run, target(number=9999), ALL, 600)


# ---- the observation envelope (§4) -------------------------------------------------------------------------------

def test_the_envelope_is_versioned_and_links_observations(env):
    sid = grant(env)
    first = env.sessions.observe(sid, env.run)
    second = env.sessions.observe(sid, env.run)
    assert (first['version'], first['environment'], first['source'], first['status']) == (2, 'app', 'native_ax', 'ready')
    assert first['changed_from'] is None and second['changed_from'] == first['observation_id']
    assert first['target_incarnation'] == second['target_incarnation'] and len(first['target_incarnation']) == 16
    assert first['coverage'] == {'shown': len(first['elements']), 'omitted': first['omitted'], 'truncated': False}
    assert first['document_epoch'] is None and 'note' not in first
    rows = {e['role']: e for e in first['elements']}
    assert rows['CheckBox'].get('actionable') is True and 'actionable' not in rows['StaticText']
    assert first['authorization'] == 'none' and first['untrusted'] == ss.UNTRUSTED, '1.8.0 fields stay'


def test_an_unlisted_window_says_what_it_shows_may_be_out_of_date(env):
    env.window.listed = False
    sid = grant(env)
    view = env.sessions.observe(sid, env.run)
    assert view['status'] == 'off_space' and 'another desktop' in view['note'] and view['elements']


# ---- bringing a window forward, once per session -------------------------------------------------------------

def test_a_minimized_window_asks_once_to_come_forward_and_later_observations_raise_it(env):
    env.window.minimized = True
    sid = grant(env)
    answer = env.sessions.observe(sid, env.run)
    assert (answer['status'], answer['reason'], answer['next'], answer['observation_id']) == (
        'needs_bring_forward', 'minimized', 'review', None)
    assert answer['proposal'] == {'session_id': sid, 'app': BUNDLE, 'reason': 'minimized', 'bundle_id': BUNDLE,
                                  'title': TITLE}
    assert env.ax.raises == [], 'nothing moves before the person approves'
    done = env.sessions.raise_window(sid)
    assert done == {'session_id': sid, 'raised': True, 'frontmost': True, 'fresh_observation_required': True,
                    'authorization': 'none'}
    assert env.sessions.observe(sid, env.run)['observation_id']
    env.window.minimized = True
    assert env.sessions.observe(sid, env.run)['observation_id'], 'approved once: the next minimize asks nothing'
    assert env.ax.raises == ['n7001', 'n7001']


def test_an_empty_window_on_another_desktop_asks_and_a_full_one_is_read_where_it_is(env):
    env.window.listed, elements, env.window.elements = False, env.window.elements, []
    sid = grant(env)
    assert env.sessions.observe(sid, env.run)['reason'] == 'off_space'
    env.window.elements = elements
    assert env.sessions.observe(sid, env.run)['status'] == 'off_space', 'readable where it is: no raise needed'


def test_raising_needs_a_live_session_and_marks_earlier_observations_stale(env):
    sid = grant(env)
    view = env.sessions.observe(sid, env.run)
    env.sessions.raise_window(sid)
    record = env.sessions.observation
    refused('unknown_observation', record, sid, view['observation_id'])
    env.sessions.revoke(sid)
    refused('session_revoked', env.sessions.raise_window, sid)


def test_an_app_that_refuses_frontmost_is_activated_through_launchservices(env, tmp_path):
    seen = []
    env.ax.frontmost, env.ax.path = False, str(tmp_path / 'Notes.app')
    adapter = env.sessions.adapters['app']
    sid = grant(env)
    row = env.sessions._session(sid)
    result = adapter.raise_window(row['target'], row['pin'], opener=lambda argv: seen.append(argv) or 0)
    assert result == {'raised': True, 'frontmost': True} and seen == [['/usr/bin/open', '-a', env.ax.path]]


# ---- Chromium page accessibility (decision 7) -----------------------------------------------------------------

def test_a_chromium_app_is_enhanced_once_per_instance_and_restored_at_the_last_end(env):
    env.ax.path = chromium_bundle(env.tmp)
    one, two = grant(env), grant(env)
    env.sessions.observe(one, env.run)
    env.sessions.observe(two, env.run)
    assert env.ax.enhances == [4242], 'once per app instance'
    env.sessions.revoke(one)
    assert env.ax.restores == []
    env.sessions.revoke(two)
    assert env.ax.restores == [{'AXManualAccessibility': False}]
    assert env.sessions.enhanced == {}


def test_an_expired_session_is_restored_at_the_next_step(env):
    env.ax.path = chromium_bundle(env.tmp)
    sid = grant(env)
    env.sessions.observe(sid, env.run)
    env.clock.advance(601)
    assert env.sessions.session_for_run(env.run) is None
    assert env.ax.restores == [{'AXManualAccessibility': False}]


def test_a_page_that_stays_empty_is_reported_thin(env):
    env.ax.path, env.ax.filled = chromium_bundle(env.tmp), False
    sid = grant(env)
    view = env.sessions.observe(sid, env.run)
    assert view['status'] == 'thin_tree' and 'little' in view['note']


def test_other_apps_and_the_switch_never_enhance(env, monkeypatch):
    env.sessions.observe(grant(env), env.run)
    assert env.ax.enhances == [], 'not Chromium-family'
    env.ax.path = chromium_bundle(env.tmp)
    monkeypatch.setenv('PEBBLE_AX_ENHANCE', '0')
    env.sessions.observe(grant(env), env.run)
    assert env.ax.enhances == []


def test_an_enhance_that_fails_leaves_the_observation_to_say_what_it_reads(env):
    env.ax.path = chromium_bundle(env.tmp)

    def boom(pid, start):
        raise AXError('timeout', 'The app did not answer.')
    env.ax.enhance = boom
    view = env.sessions.observe(grant(env), env.run)
    assert view['observation_id'] and view['status'] == 'ready' and env.sessions.enhanced == {}


def test_the_chromium_check_looks_five_levels_down_and_follows_no_link(tmp_path):
    assert ss.chromium_family(chromium_bundle(tmp_path)) is True
    (tmp_path / 'Slack.app/Contents/Frameworks/Slack Helper (Renderer).app').mkdir(parents=True)
    assert ss.chromium_family(str(tmp_path / 'Slack.app')) is True, 'Electron: one level down'
    (tmp_path / 'Notes.app/Contents/Frameworks/Some.framework').mkdir(parents=True)
    assert ss.chromium_family(str(tmp_path / 'Notes.app')) is False
    linked = tmp_path / 'Linked.app/Contents/Frameworks'
    linked.mkdir(parents=True)
    (linked / 'Elsewhere').symlink_to(tmp_path / 'Comet.app/Contents/Frameworks')
    assert ss.chromium_family(str(tmp_path / 'Linked.app')) is False
    (tmp_path / 'Deep.app/Contents/Frameworks/a/b/c/d/e/X Helper (Renderer).app').mkdir(parents=True)
    assert ss.chromium_family(str(tmp_path / 'Deep.app')) is False
    assert ss.chromium_family(None) is False and ss.chromium_family(str(tmp_path / 'missing.app')) is False


# ---- typed act results (§4) -------------------------------------------------------------------------------------

def test_act_results_are_typed_from_the_verdict(env):
    sid = grant(env)
    done = act(env, sid, 'w/to', 'type', 'sam@example.com')
    # Phase 45 (D140): an act that settles returns the window after it, so the next step is the next act
    assert (done['dispatch'], done['outcome'], done['next']) == ('sent', 'confirmed', 'act')
    env.window.hooks[('w/body', 'type')] = lambda app, window, text: None
    ignored = act(env, sid, 'w/body', 'type', 'Bye')
    assert (ignored['dispatch'], ignored['outcome'], ignored['next']) == ('sent', 'no_visible_effect', 'act')
    review = act(env, sid, 'w/send', 'press')
    assert (review['dispatch'], review['outcome'], review['next']) == ('not_sent', 'review', 'review')

    def lost(app, window, text):
        raise AXError('uncertain', 'no answer')
    env.window.hooks[('w/bold', 'toggle')] = lost
    unsure = act(env, sid, 'w/bold', 'toggle')
    assert (unsure['dispatch'], unsure['outcome'], unsure['next']) == ('unknown', 'needs_reconciliation', 'handoff')


# ---- the window request (ScreenRequest) ----------------------------------------------------------------------

def test_a_window_request_names_the_number_and_says_when_the_window_is_elsewhere(env):
    from test_app_control_42 import bundle, reader
    path = bundle(env.tmp, 'Notes', BUNDLE)
    rows = [{'pid': 4242, 'start': env.app.start, 'executable': path + '/Contents/MacOS/Notes', 'bundle_path': path}]
    host = SimpleNamespace(screens=env.sessions)
    ask = lambda: ss.request_proposal(host, {'app': 'Notes', 'why': 'to read it'}, lister=lambda: rows,
                                      reader=reader(('Notes', BUNDLE, TEAM)))
    env.window.listed = False
    payload = ask()
    assert payload['window'] == {'title_sha256': ax_worker.sha256(TITLE), 'ax_identifier': None, 'window_number': 7001}
    assert payload['off_space'] is True and 'minimized' not in payload
    env.app.add(Window43(7002, TITLE, compose(), focused=False))
    assert ask()['window']['window_number'] == 7001, 'a shared title no longer refuses: the number names it'
    granted = ss.grant_requested(host, env.chat, env.run, ask())
    assert granted['granted'] is True and granted['session_id'] in env.sessions.requested


# ---- capability discovery (§3) -------------------------------------------------------------------------------

class Backend:
    def hello(self):
        return {'available': True, 'window_numbers': True}


def host(revision=3):
    return SimpleNamespace(
        screens=SimpleNamespace(adapters={'app': SimpleNamespace(backend=Backend()), 'browser': object()},
                                revision=revision),
        browser_bridge=SimpleNamespace(status=lambda: {'extension': {'connected': True}}),
        model=SimpleNamespace(current=lambda: {'kind': 'chatgpt', 'model': 'gpt-6.1'}))


def test_capabilities_are_host_facts_with_the_unmeasured_ones_off():
    assert ss.capabilities(host(), 'r1') == {
        'native': {'ax': True, 'window_numbers': True, 'capture': False, 'foreground_input': False},
        'browser': {'extension_connected': True, 'owned_tabs': True, 'frame_reads': False, 'focus_emulation': False,
                    'pebble_browser': True, 'window_route': True},  # Phase 44 (D117): BrowserRequest's window route
        'model': {'image_input': True}, 'scope': {'run_id': 'r1', 'grant_revision': 3}}
    bare = ss.capabilities(SimpleNamespace(), None)
    assert bare['native'] == {'ax': False, 'window_numbers': False, 'capture': False, 'foreground_input': False}
    assert bare['browser']['extension_connected'] is False and bare['model'] == {'image_input': False}


def test_the_capability_line_follows_the_screen_tools_and_is_replaced_when_a_fact_changes():
    app, tool = host(), {'function': {'name': 'ScreenObserve'}}
    messages = runner.announce_capabilities(app, [], 'r1', [tool])
    assert [m['name'] for m in messages] == [ss.CAPABILITIES_MESSAGE]
    line = messages[0]['content']
    assert line.startswith(ss.CAPABILITIES_HEAD + '{') and 'never work around it' in line and messages[0]['role'] == 'user'
    assert runner.announce_capabilities(app, messages, 'r1', [tool]) is messages, 'unchanged: no new line'
    assert runner.announce_capabilities(app, [], 'r1', [{'function': {'name': 'ReadFile'}}]) == []
    assert runner.announce_capabilities(app, [], 'r1', [{'function': {'name': ss.REQUEST_NAME}}]), 'with ScreenRequest'
    app.screens.revision = 4
    again = runner.announce_capabilities(app, messages + [{'role': 'user', 'content': 'next'}], 'r1', [tool])
    assert [m.get('name') for m in again] == [None, ss.CAPABILITIES_MESSAGE]
    assert '"grant_revision":4' in again[-1]['content']


def test_the_grant_revision_moves_on_every_grant_and_end(env):
    before = env.sessions.revision
    sid = grant(env)
    assert env.sessions.revision == before + 1
    env.sessions.revoke(sid)
    assert env.sessions.revision == before + 2
    other = grant(env)
    env.clock.advance(601)
    refused('session_expired', env.sessions.observe, other, env.run)
    assert env.sessions.revision == before + 4
