"""Phase 45 (layer 4): screenshots in observations and pixel acts in a granted app window, through ScreenSessions over
a fake Accessibility backend (test_screen_m1_43's FakeAX43 plus hit) and a fake helper lane (captures it draws, inputs
it records). The window sits at (100, 50) and is 640 x 400 points; its capture is 1280 x 800 pixels, within the model's
budget, so a screenshot pixel (x, y) is the global point (100 + x / 2, 50 + y / 2).

Covered: the screenshot's pixel space and image parts (sent once, to the owning run); no capture without the caller's
flag, with capture off, or when the capture cannot vouch for the window; a site-fenced browser window cropped to its
page; text marks in thin trees with secure fields blanked; every refusal before input (no screenshot, input off, a
stale or padded point, a moved window, a changed spot, another process or a secure field under the point, the keyboard
elsewhere, the grant); classification from what is under the point; the first act asking once per session; the
readback (a change at the point, elsewhere, a closed or moved window, nothing visible and its loop breaker); an input
without an answer pausing the session; a refusal before any event; every check again when a reviewed act is approved;
capabilities wired to the lane and the person's switches; the schema; and App.execute end to end. No real helper,
window or event is used."""
import json
import uuid
from types import SimpleNamespace

import pytest
from PIL import ImageDraw, Image

from pebble import ax_worker, screen_pixels as sp, screen_session as ss, tool_authz
from pebble.screen_helper import HelperError
from pebble.screen_session import ScreenRefused
from pebble.store import Store
from test_screen_m1_43 import FakeAX43, Window43
from test_screen_session import ALL, BUNDLE, TEAM, TITLE, App as RunningApp, Clock, el, make, refused

FRAME = [100, 50, 640, 400]
NUMBER, PID = 7001, 4242
LOOK = ['focus', 'scroll', 'expand', 'collapse']


def elements():
    return [
        el('AXTextField', '/0', title='To', value='', frame=[110, 60, 300, 24]),
        el('AXButton', '/1', title='Send', frame=[400, 300, 80, 24]),
        el('AXCheckBox', '/2', title='Bold', value=0, frame=[500, 300, 20, 20]),
        el('AXGroup', '/3', frame=[110, 120, 600, 160]),   # a canvas: nothing named
        el('AXRow', '/4', title='Inbox', selected=False, frame=[110, 380, 200, 20]),
        el('AXTextArea', '/5', description='Body', value='Hello', frame=[530, 380, 200, 60]),
    ]


def at(frame):
    """The screenshot pixel at the centre of a frame in global points."""
    return (frame[0] + frame[2] / 2 - FRAME[0]) * 2, (frame[1] + frame[3] / 2 - FRAME[1]) * 2


SEND, BOLD, CANVAS, ROW, TO, BODY = (tuple(int(v) for v in at(e['frame'])) for e in
                                     (elements()[1], elements()[2], elements()[3], elements()[4], elements()[0],
                                      elements()[5]))


class Window45(Window43):
    def raw(self):
        return {**super().raw(), 'frame': list(FRAME)}


class FakeAX45(FakeAX43):
    """FakeAX43 plus hit: the topmost element of the window whose frame holds the point (or the focused path), or a
    fixed answer the test sets."""

    def __init__(self):
        super().__init__()
        self.answer, self.focused_path, self.hits = None, '/0', []

    def hit(self, pid, start, ref, point=None, focused=False):
        app = self._app(pid, start)
        window = app.windows[ref]
        self.hits.append((point, focused))
        if self.answer is not None:
            return self.answer
        if focused:
            found = [e for e in window.elements if e['path'] == self.focused_path]
        else:
            found = [e for e in window.elements if e.get('frame') and e['frame'][0] <= point[0] < e['frame'][0]
                     + e['frame'][2] and e['frame'][1] <= point[1] < e['frame'][1] + e['frame'][3]]
        element = dict(found[-1], path='hit') if found else {'role': 'AXWindow', 'path': 'hit', 'title': window.title}
        return {'pid': pid, 'element': element, 'ancestors': [], 'in_window': True, 'menu': False}


class FakeLane:
    """PixelLane's surface over a picture of the window this test draws on: facts, capture, send."""

    def __init__(self):
        self.capture_on = self.input_on = True
        self.frame, self.owner, self.number = [float(v) for v in FRAME], PID, NUMBER
        self.image = Image.new('RGB', (1280, 800), (200, 200, 200))
        self.captures, self.sends, self.asked, self.lines = 0, [], [], None
        self.effect = self.error = None

    def facts(self):
        return {'capture': self.capture_on, 'foreground_input': self.input_on, 'hello': {}}

    def capture(self, number, own_pids=(), *, ocr=None):
        self.captures += 1
        if self.error_on_capture is not None:
            raise self.error_on_capture
        if number != self.number:
            raise HelperError('not_found', 'The window is gone.')
        shot = {'pixels': list(self.image.size), 'frame': list(self.frame), 'scale': self.image.size[0] / self.frame[2],
                'owner_pid': self.owner, 'on_screen': True, 'sha256': 'a' * 64, 'image': self.image.copy()}
        asked = ocr(shot) if ocr is not None else None
        if asked is not None:
            self.asked.append(asked)
            shot['lines'] = self.lines or {'lines': [], 'truncated': False, 'blanked': 0}
        return shot

    error_on_capture = None

    def send(self, session_id, kind, fields):
        self.sends.append((kind, dict(fields)))
        if self.error is not None:
            raise self.error
        if self.effect is not None:
            self.effect(self, kind, fields)
        return {'posted': 2}


def paint(lane, box, shade=(30, 30, 30)):
    ImageDraw.Draw(lane.image).rectangle(box, fill=shade)


def near(point, size=12):
    x, y = point
    return (x - size, y - size, x + size, y + size)


@pytest.fixture
def env(tmp_path):
    clock, ax, lane = Clock(), FakeAX45(), FakeLane()
    running = RunningApp(PID, 1_700_000_000.25)
    ax.apps.append(running)
    window = running.add(Window45(NUMBER, TITLE, elements()))
    store = Store(tmp_path / 'home')
    sessions = make(store, ax, clock, pixels=lane)
    yield SimpleNamespace(clock=clock, ax=ax, lane=lane, running=running, window=window, store=store,
                          sessions=sessions, chat=uuid.uuid4().hex, run=uuid.uuid4().hex)
    store.close()


def target(**extra):
    return {'kind': 'app', 'bundle_id': BUNDLE, 'team_id': TEAM, **extra,
            'window': {'title_sha256': ax_worker.sha256(TITLE), 'ax_identifier': None, 'window_number': NUMBER}}


def grant(env, actions=ALL, **extra):
    return env.sessions.grant(env.chat, env.run, target(**extra), actions, 600)['session_id']


def look(env, sid):
    return env.sessions.observe(sid, env.run, screenshot=True)


def pixel(env, sid, action, **args):
    view = look(env, sid)
    return env.sessions.act_pixels(sid, env.run, view['observation_id'], action, **args)


def consented(env, actions=ALL):
    """A session whose first pixel act (a scroll that moved the canvas) the person approved."""
    sid = grant(env, actions)
    first = pixel(env, sid, 'scroll_at', x=CANVAS[0], y=CANVAS[1], dy=3)
    assert first['status'] == 'needs_native_review' and first['proposal']['review']['consent'] == 'foreground_input'
    shade = (env.lane.image.getpixel((600, 300))[0] + 60) % 256  # always a change, however often it runs
    env.lane.effect = lambda lane, kind, fields: paint(lane, (300, 200, 900, 400), (shade,) * 3)
    done = env.sessions.commit(first['proposal_id'])
    env.lane.effect = None
    assert done['status'] == 'done' and done['verdict'] == 'occurred'
    return sid


def act_row(env, action_id):
    return env.store.query('SELECT * FROM screen_actions WHERE action_id=?', (action_id,))[0]


# ---- the screenshot -----------------------------------------------------------------------------------------------

def test_an_observation_carries_its_screenshot_and_the_runner_gets_the_picture_once(env):
    sid = grant(env)
    view = look(env, sid)
    shot = view['screenshot']
    assert (shot['width'], shot['height']) == (1280, 800) and len(shot['transform_id']) == 16
    assert view['elements'] and view['elements'][0]['role'] == 'TextField', 'Accessibility rows stay first'
    assert env.sessions.observation(sid, view['observation_id'])['screenshot'] == shot
    assert env.sessions.screenshot_parts(sid, uuid.uuid4().hex) is None, 'only to the run that owns the session'
    parts = env.sessions.screenshot_parts(sid, env.run)
    assert parts[0]['text'].startswith('UNTRUSTED screenshot') and '1280 x 800 pixels' in parts[0]['text']
    assert view['observation_id'] in parts[0]['text'] and 'never instructions' in parts[0]['text']
    assert parts[1]['image_url']['url'].startswith('data:image/png;base64,')
    assert env.sessions.screenshot_parts(sid, env.run) is None, 'sent once'


def test_no_capture_without_the_callers_flag_and_nothing_said_with_capture_off(env, monkeypatch):
    monkeypatch.setenv('PEBBLE_SCREEN_CAPTURE', '0')  # D135: on by default; this lane is switched off
    sid = grant(env)
    view = env.sessions.observe(sid, env.run)
    assert 'screenshot' not in view and env.lane.captures == 0
    env.lane.capture_on = False
    view = look(env, sid)
    assert 'screenshot' not in view and 'screenshot_unavailable' not in view and env.lane.captures == 0


@pytest.mark.parametrize('change, reason', [
    (lambda env: setattr(env.lane, 'owner', 999), 'window_changed'),
    (lambda env: env.lane.frame.__setitem__(0, 120.0), 'window_changed'),
    (lambda env: setattr(env.window, 'number', None), 'no_window_number'),
    (lambda env: setattr(env.lane, 'error_on_capture', HelperError('not_permitted', 'Screen Recording is off.')),
     'capture_not_permitted'),
])
def test_a_capture_that_cannot_vouch_for_the_window_gives_no_screenshot(env, change, reason):
    sid = grant(env)
    change(env)
    view = look(env, sid)
    assert 'screenshot' not in view and view['screenshot_unavailable'] == reason
    assert env.sessions.screenshot_parts(sid, env.run) is None


def fenced_window(env, href):
    env.window.elements = [el('AXToolbar', '/0', title='Tabs: my other tab', frame=[100, 50, 640, 80]),
                           el('AXWebArea', '/1', href=href, frame=[100, 130, 640, 320]),
                           el('AXLink', '/1/0', title='Next', href='https://www.example.com/next',
                              frame=[120, 150, 80, 20])]
    return grant(env, origins=['https://www.example.com'], read=['https://www.example.com'])


def test_a_site_fenced_browser_window_is_cropped_to_its_page_and_shows_nothing_away_from_it(env):
    sid = fenced_window(env, 'https://www.example.com/watch?v=1')
    view = look(env, sid)
    assert (view['screenshot']['width'], view['screenshot']['height']) == (1280, 640), 'the page only, no tabs'
    away = fenced_window(env, 'https://elsewhere.example/')
    view = look(env, away)
    assert view['status'] == 'away' and view['screenshot_unavailable'] == 'page_not_shown'


def test_a_thin_tree_adds_confident_text_marks_with_secure_fields_blanked_first(env):
    env.window.elements = elements()[3:4] + [el('AXSecureTextField', '/9', title='Password',
                                                frame=[200, 300, 100, 20])]
    sid = grant(env)
    assert env.sessions.observe(sid, env.run, screenshot=True)['state'] == 'handoff'
    env.sessions.resume(sid)  # the person handled the password field and pressed Done
    env.lane.lines = {'lines': [{'text': 'Untitled drawing', 'box': [40.0, 300.0, 200.0, 40.0], 'confidence': 0.95},
                                {'text': 'n0ise', 'box': [10.0, 10.0, 20.0, 10.0], 'confidence': 0.1},
                                {'text': 'off the picture', 'box': [1270.0, 10.0, 40.0, 10.0], 'confidence': 0.9}],
                      'truncated': False, 'blanked': 1}
    view = look(env, sid)
    assert view['text_marks'] == [{'text': 'Untitled drawing', 'box': [40, 300, 200, 40]}]
    assert env.lane.asked[-1] == {'blank': [[200.0, 500.0, 200.0, 40.0]], 'level': 'fast'}, 'in capture pixels'
    env.window.elements = elements() + [el('AXButton', f'/b{i}', title=f'Tool {i}', frame=[110 + 10 * i, 60, 8, 8])
                                        for i in range(8)]
    sid2 = grant(env)
    assert 'text_marks' not in look(env, sid2), 'a rich tree needs no recognised text'


# ---- refusals before any input ------------------------------------------------------------------------------------

def test_a_pixel_act_needs_the_newest_screenshot_and_the_input_lane(env):
    sid = grant(env)
    plain = env.sessions.observe(sid, env.run)
    refused('no_screenshot', env.sessions.act_pixels, sid, env.run, plain['observation_id'], 'click_at', x=1, y=1)
    first, second = look(env, sid), look(env, sid)
    refused('stale_ui', env.sessions.act_pixels, sid, env.run, first['observation_id'], 'click_at', x=1, y=1)
    refused('unknown_observation', env.sessions.act_pixels, sid, env.run, 'f' * 20, 'click_at', x=1, y=1)
    env.lane.input_on = False
    refused('pixel_unavailable', env.sessions.act_pixels, sid, env.run, second['observation_id'], 'click_at', x=1, y=1)
    env.lane.input_on = True
    env.clock.advance(ss.OBSERVATION_MAX_AGE + 1)
    refused('stale_ui', env.sessions.act_pixels, sid, env.run, second['observation_id'], 'click_at', x=1, y=1)
    assert env.lane.sends == [] and env.ax.raises == []


@pytest.mark.parametrize('action, args', [
    ('click_at', {'x': 10}), ('click_at', {'x': 10, 'y': True}), ('click_at', {'x': -1, 'y': 3}),
    ('click_at', {'x': 1, 'y': 1, 'count': 4}), ('click_at', {'x': 1, 'y': 1, 'button': 'right', 'count': 2}),
    ('click_at', {'x': 1, 'y': 1, 'keys': 'cmd+s'}), ('scroll_at', {'x': 1, 'y': 1, 'dy': 0}),
    ('scroll_at', {'x': 1, 'y': 1, 'dy': 51}), ('key', {'keys': 'f1'}), ('key', {'keys': 'cmd+s', 'x': 1}),
    ('type_text', {'text': 'a\x07b'}), ('type_text', {'text': ''}), ('type_text', {'text': 'x' * 501}),
    ('click', {'x': 1, 'y': 1}),
])
def test_malformed_pixel_acts_are_refused_before_anything_happens(env, action, args):
    sid = grant(env)
    view = look(env, sid)
    refused('invalid', env.sessions.act_pixels, sid, env.run, view['observation_id'], action, **args)
    refused('invalid', env.sessions.act_pixels, sid, env.run, view['observation_id'], 'click_at', x=1, y=1,
            expect='Sent')
    assert env.ax.raises == [] and env.lane.sends == []


def test_a_browser_tab_takes_no_pixel_acts(env):
    class Tab:
        kind = 'browser'

        def pin(self, target):
            return {'instance': 'x'}
    sessions = make(env.store, env.ax, env.clock, pixels=env.lane, extra={'browser': Tab()})
    sid = sessions.grant(env.chat, env.run, {'kind': 'browser', 'profile': 'pebble', 'tab_id': 't1',
                                             'origins': ['https://a.example']}, ALL, 60)['session_id']
    refused('unsupported_target', sessions.act_pixels, sid, env.run, 'a' * 20, 'click_at', x=1, y=1)


def test_a_point_off_the_window_a_moved_window_or_a_changed_spot_is_refused(env):
    sid = consented(env)
    refused('outside_target', pixel, env, sid, 'click_at', x=1280, y=10)
    view = look(env, sid)
    env.lane.frame[0] += 20  # the person moved the window after Pebble looked
    refused('stale_ui', env.sessions.act_pixels, sid, env.run, view['observation_id'], 'click_at', x=BOLD[0], y=BOLD[1])
    env.lane.frame[0] -= 20
    view = look(env, sid)
    paint(env.lane, near(BOLD, 30))  # a dialog came up over the spot
    error = refused('stale_ui', env.sessions.act_pixels, sid, env.run, view['observation_id'], 'click_at', x=BOLD[0],
                    y=BOLD[1])
    assert 'spot changed' in str(error)
    view = look(env, sid)
    ImageDraw.Draw(env.lane.image).line((BOLD[0] + 20, BOLD[1] - 10, BOLD[0] + 20, BOLD[1] + 10), fill=(0, 0, 0))
    result = env.sessions.act_pixels(sid, env.run, view['observation_id'], 'click_at', x=BOLD[0], y=BOLD[1])
    assert result['status'] == 'done', 'a caret beside the point is not a change'
    assert len(env.lane.sends) == 2  # the consent scroll and this click only


def test_another_process_a_secure_field_or_the_keyboard_elsewhere_is_refused(env):
    sid = consented(env)
    env.ax.answer = {'pid': 9999, 'element': {'role': 'AXButton', 'path': 'hit', 'title': 'Allow'}, 'ancestors': [],
                     'in_window': None, 'menu': False}
    refused('covered', pixel, env, sid, 'click_at', x=SEND[0], y=SEND[1])
    refused('focus_elsewhere', pixel, env, sid, 'type_text', text='hello')
    env.ax.answer = {'pid': PID, 'element': {'role': 'AXTextField', 'subrole': 'AXSecureTextField', 'path': 'hit'},
                     'ancestors': [], 'in_window': True, 'menu': False}
    refused('secure_field', pixel, env, sid, 'click_at', x=TO[0], y=TO[1])
    refused('secure_field', pixel, env, sid, 'type_text', text='hunter2')
    env.ax.answer = {'pid': None, 'element': None, 'ancestors': [], 'in_window': None, 'menu': False}
    refused('focus_elsewhere', pixel, env, sid, 'key', keys='tab')
    assert pixel(env, sid, 'click_at', x=CANVAS[0], y=CANVAS[1])['class'] == 'commit', \
        'nothing named under the point: the window check at input decides, and the person reviews it'
    assert len(env.lane.sends) == 1


def test_an_app_that_will_not_come_forward_takes_no_click_or_keys(env):
    sid = consented(env)
    env.ax.frontmost = False
    refused('focus_elsewhere', pixel, env, sid, 'click_at', x=BOLD[0], y=BOLD[1])
    refused('focus_elsewhere', pixel, env, sid, 'key', keys='tab')
    assert pixel(env, sid, 'scroll_at', x=CANVAS[0] + 40, y=CANVAS[1], dy=1)['status'] == 'done', \
        'a scroll reaches the window under the pointer without it'


def test_the_grant_must_cover_what_the_act_amounts_to(env):
    sid = consented(env, LOOK)
    refused('action_not_granted', pixel, env, sid, 'click_at', x=BOLD[0], y=BOLD[1])  # a toggle
    refused('action_not_granted', pixel, env, sid, 'type_text', text='hi')
    raises = len(env.ax.raises)
    refused('action_not_granted', pixel, env, sid, 'key', keys='cmd+s')
    assert len(env.ax.raises) == raises, 'refused before the window was touched'
    assert pixel(env, sid, 'click_at', x=TO[0], y=TO[1])['status'] == 'done', 'focusing a field is looking'
    assert pixel(env, sid, 'key', keys='tab')['status'] == 'done'


# ---- the first act, classification and dispatch ----------------------------------------------------------------

def test_the_first_pixel_act_asks_once_and_its_approval_covers_the_session(env):
    sid = grant(env)
    first = pixel(env, sid, 'scroll_at', x=CANVAS[0], y=CANVAS[1], dy=3)
    review = first['proposal']['review']
    assert first['class'] == 'local' and review['consent'] == 'foreground_input' and review['action'] == 'scroll at'
    assert 'rest of this session' in first['message'] and 'window_facts' not in review and 'pixel' not in review
    assert env.lane.sends == [] and act_row(env, first['proposal_id'])['state'] == 'proposed'
    done = env.sessions.commit(first['proposal_id'])
    assert done['status'] == 'done' and done['dispatch'] == 'sent'
    kind, fields = env.lane.sends[0]
    assert kind == 'scroll' and fields['dy'] == -3 and fields['dx'] == 0 and fields['window_number'] == NUMBER
    assert fields['point'] == [100 + CANVAS[0] / 2, 50 + CANVAS[1] / 2]
    second = pixel(env, sid, 'scroll_at', x=CANVAS[0] + 100, y=CANVAS[1], dy=-2)
    assert second['status'] == 'done' and len(env.lane.sends) == 2, 'asked once'
    other = grant(env)
    declined = pixel(env, other, 'scroll_at', x=CANVAS[0], y=CANVAS[1], dy=3)
    assert env.sessions.reject(declined['proposal_id'])
    assert pixel(env, other, 'scroll_at', x=CANVAS[0], y=CANVAS[1], dy=3)['status'] == 'needs_native_review', \
        'declined: it asks again'


@pytest.mark.parametrize('point, answer, args, klass, reasons', [
    (SEND, None, {}, 'commit', ['label_commit_word', 'press_activates']),
    (BOLD, None, {}, 'input', []),
    (CANVAS, None, {}, 'commit', ['no_label', 'press_activates']),
    (TO, None, {}, 'local', []),
    (ROW, None, {}, 'input', []),
    (ROW, None, {'count': 2}, 'commit', ['double_click_opens']),
    (CANVAS, None, {'button': 'right'}, 'input', ['context_menu']),
    (CANVAS, {'pid': PID, 'element': {'role': 'AXStaticText', 'path': 'hit', 'value': 'Delete'},
              'ancestors': [{'role': 'AXButton', 'path': 'hit^1'}], 'in_window': True, 'menu': False},
     {}, 'commit', ['label_commit_word', 'press_activates']),
    (BOLD, {'pid': PID, 'element': {'role': 'AXCheckBox', 'path': 'hit', 'title': 'Bold'}, 'ancestors': [],
            'in_window': False, 'menu': False}, {}, 'commit', ['outside_granted_window']),
    (BOLD, {'pid': PID, 'element': {'role': 'AXMenuItem', 'path': 'hit', 'title': 'Copy'}, 'ancestors': [],
            'in_window': None, 'menu': True}, {}, 'commit', ['press_activates']),
])
def test_a_click_is_classified_by_what_is_under_its_point(env, point, answer, args, klass, reasons):
    sid = consented(env)
    env.ax.answer = answer
    result = pixel(env, sid, 'click_at', x=point[0], y=point[1], **args)
    assert result['class'] == klass, result
    if klass == 'commit':
        assert result['status'] == 'needs_native_review' and result['reasons'] == reasons
        assert result['proposal']['review']['class'] == 'commit' and 'consent' not in result['proposal']['review']
        assert len(env.lane.sends) == 1, 'a commit waits for the person'
    else:
        assert result['status'] == 'done' and len(env.lane.sends) == 2
        kind, fields = env.lane.sends[-1]
        assert kind == 'click' and fields['window_number'] == NUMBER
        assert (fields['button'], fields['count']) == (args.get('button', 'left'), args.get('count', 1))


def test_the_review_card_shows_what_is_under_the_point(env):
    sid = consented(env)
    result = pixel(env, sid, 'click_at', x=SEND[0], y=SEND[1])
    review = result['proposal']['review']
    assert review['action'] == 'click' and review['element']['label'] == 'Send' and review['element']['role'] == 'AXButton'
    assert review['postcondition']['kind'] == 'visible_change' and 'Send' not in json.dumps(review['context'])
    stored = json.loads(act_row(env, result['proposal_id'])['review'])
    assert stored['pixel']['spec']['action'] == 'click_at' and stored['pixel']['window_number'] == NUMBER
    assert 'base64' not in act_row(env, result['proposal_id'])['review'], 'no image is ever kept'


@pytest.mark.parametrize('keys, focused, klass, kind', [
    ('tab', '/0', 'local', 'key'), ('page_down', '/5', 'local', 'key'), ('cmd+s', '/0', 'commit', None),
    ('return', '/0', 'commit', None),
])
def test_keys_are_local_only_as_plain_navigation(env, keys, focused, klass, kind):
    sid = consented(env)
    env.ax.focused_path = focused
    result = pixel(env, sid, 'key', keys=keys)
    assert result['class'] == klass
    if kind:
        assert result['status'] == 'done' and env.lane.sends[-1][0] == 'key'
        assert env.lane.sends[-1][1] == {'key': sp.parse_chord(keys)['key'], 'modifiers': [], 'expect_pid': PID}
    else:
        assert result['status'] == 'needs_native_review'
        if keys == 'cmd+s':
            assert result['proposal']['review']['action'] == 'press ⌘S in' and result['reasons'] == ['key_chord']
        else:
            assert result['reasons'] == ['enter_submits', 'key_chord']


def test_typing_goes_to_the_focused_field_and_anything_else_is_a_commit(env):
    sid = consented(env)
    env.lane.effect = lambda lane, kind, fields: paint(lane, near(TO, 8))
    typed = pixel(env, sid, 'type_text', text='alex@example.com')
    assert typed['class'] == 'input' and typed['status'] == 'done' and typed['verdict'] == 'occurred'
    assert env.lane.sends[-1] == ('type', {'text': 'alex@example.com', 'expect_pid': PID})
    journal = act_row(env, typed['action_id'])
    assert journal['text_sha256'] == ax_worker.value_digest('alex@example.com')
    assert 'alex@example.com' not in journal['review'], 'typed words are journaled as their digest only'
    assert pixel(env, sid, 'type_text', text='line one\nline two')['reasons'] == ['newline_submits']
    env.ax.focused_path = '/5'
    env.lane.effect = lambda lane, kind, fields: paint(lane, near(BODY, 8))
    assert pixel(env, sid, 'type_text', text='second\nline')['status'] == 'done', 'a text area takes a newline'
    env.ax.focused_path = '/3'
    assert pixel(env, sid, 'type_text', text='v')['reasons'] == ['not_a_text_field']


# ---- the readback ---------------------------------------------------------------------------------------------------

def test_a_change_at_the_point_elsewhere_or_of_the_window_is_reported(env):
    sid = consented(env)
    env.lane.effect = lambda lane, kind, fields: paint(lane, near(BOLD, 6))
    result = pixel(env, sid, 'click_at', x=BOLD[0], y=BOLD[1])
    assert (result['verdict'], result['basis'], result['visible_change'], result['outcome']) == (
        'occurred', 'visible_change', 'at_point', 'confirmed')
    assert result['under_point'] == {'role': 'CheckBox', 'label': 'Bold'} and 'retry' not in result
    env.lane.effect = lambda lane, kind, fields: paint(lane, (0, 0, 200, 799), (10, 10, 10))
    result = pixel(env, sid, 'click_at', x=BOLD[0], y=BOLD[1])
    assert (result['verdict'], result['basis'], result['visible_change']) == ('occurred', 'changed_elsewhere',
                                                                              'elsewhere')
    env.lane.effect = lambda lane, kind, fields: lane.frame.__setitem__(2, 700.0)
    assert pixel(env, sid, 'click_at', x=BOLD[0], y=BOLD[1])['basis'] == 'window_changed'
    env.lane.frame[2] = 640.0
    env.lane.effect = lambda lane, kind, fields: setattr(lane, 'number', NUMBER + 1)
    closed = pixel(env, sid, 'click_at', x=BOLD[0], y=BOLD[1])
    assert (closed['verdict'], closed['basis']) == ('occurred', 'window_closed')
    assert env.sessions.readback(closed['action_id'])['evidence']['basis'] == 'not_provable_by_reobservation'


def test_nothing_visible_is_reported_and_the_same_act_is_never_repeated_as_it_was(env):
    sid = consented(env)
    result = pixel(env, sid, 'click_at', x=BOLD[0], y=BOLD[1])
    assert (result['verdict'], result['basis'], result['outcome'], result['retry']) == (
        'not_observed', 'no_visible_change', 'no_visible_effect', 'never_as_is')
    assert 'Never repeat' in result['message'] and act_row(env, result['action_id'])['verdict'] == 'not_observed'
    sent = len(env.lane.sends)
    refused('repeat_ineffective', pixel, env, sid, 'click_at', x=BOLD[0] + 3, y=BOLD[1] - 2)
    assert len(env.lane.sends) == sent, 'the repeat was refused before any input'
    assert pixel(env, sid, 'click_at', x=BOLD[0], y=BOLD[1], count=2)['status'] == 'needs_native_review', \
        'another act (a double click) is not the same act'
    other = pixel(env, sid, 'click_at', x=BOLD[0] + 10, y=BOLD[1])
    assert other['status'] == 'done', 'another point is another act'
    paint(env.lane, near(BOLD, 4), (120, 120, 120))  # the screen changed since
    assert pixel(env, sid, 'click_at', x=BOLD[0], y=BOLD[1])['status'] == 'done'


def test_an_input_without_an_answer_is_uncertain_and_pauses_the_session(env):
    sid = consented(env)
    env.lane.error = HelperError('uncertain', 'The screen helper did not answer in time.')
    result = pixel(env, sid, 'click_at', x=BOLD[0], y=BOLD[1])
    assert (result['status'], result['verdict'], result['dispatch'], result['needs_reconciliation']) == (
        'uncertain', 'unknown', 'unknown', True)
    assert result['session_state'] == 'interrupted' and result['next'] == 'handoff'
    assert act_row(env, result['action_id'])['state'] == 'unknown'
    refused('interrupted', env.sessions.observe, sid, env.run, screenshot=True)
    env.lane.error = None
    sid2 = consented(env)
    env.lane.error = HelperError('interrupted', 'The person used the keyboard or mouse; Pebble stopped.')
    result = pixel(env, sid2, 'scroll_at', x=CANVAS[0] + 50, y=CANVAS[1], dy=1)
    assert result['basis'] == 'interrupted_by_person' and result['session_state'] == 'interrupted'
    assert result['needs_reconciliation'] is False, 'a local act needs no reconciliation, as for element acts'


def test_a_refusal_before_any_event_is_a_refusal_and_the_session_carries_on(env):
    sid = consented(env)
    env.lane.error = HelperError('refused', 'The person is using the Mac; Pebble waits.')
    error = refused('input_refused', pixel, env, sid, 'click_at', x=BOLD[0], y=BOLD[1])
    assert 'using the Mac' in str(error)
    row = env.store.query("SELECT state, evidence FROM screen_actions WHERE session_id=? ORDER BY created DESC LIMIT 1",
                          (sid,))[0]
    assert row['state'] == 'refused' and json.loads(row['evidence'])['basis'] == 'input_refused'
    env.lane.error = sp.LeaseBusy('refused', 'Another task is using the keyboard and mouse.')
    refused('input_busy', pixel, env, sid, 'click_at', x=BOLD[0], y=BOLD[1])
    env.lane.error = None
    assert env.sessions.sessions(env.chat)[0]['state'] == 'active'
    assert pixel(env, sid, 'click_at', x=BOLD[0], y=BOLD[1])['status'] == 'done'


# ---- approval -------------------------------------------------------------------------------------------------------

def test_ending_a_session_drops_its_pictures(env):
    sid = consented(env)
    look(env, sid)
    assert env.sessions.runtime[sid].get('screenshot') is not None
    env.sessions.revoke(sid)
    assert not {'screenshot', 'reviews', 'ineffective'} & set(env.sessions.runtime[sid])
    assert env.sessions.screenshot_parts(sid, env.run) is None


def test_an_approved_act_is_sent_once_after_every_check_again(env):
    sid = consented(env)
    proposed = pixel(env, sid, 'click_at', x=SEND[0], y=SEND[1])
    env.lane.effect = lambda lane, kind, fields: paint(lane, near(SEND, 6))
    done = env.sessions.commit(proposed['proposal_id'])
    assert done['status'] == 'done' and done['class'] == 'commit' and done['verdict'] == 'occurred'
    assert env.lane.sends[-1][0] == 'click' and len(env.lane.sends) == 2
    refused('already_decided', env.sessions.commit, proposed['proposal_id'])
    assert len(env.lane.sends) == 2, 'never replayed'


@pytest.mark.parametrize('drift, why', [
    (lambda env: setattr(env.ax, 'answer', {'pid': PID, 'element': {'role': 'AXButton', 'path': 'hit',
                                                                    'title': 'Cancel'},
                                             'ancestors': [], 'in_window': True, 'menu': False}), 'changed since'),
    (lambda env: paint(env.lane, near(SEND, 30)), 'spot changed'),
    (lambda env: env.lane.frame.__setitem__(1, 70.0), 'moved'),
])
def test_drift_after_the_review_ends_the_proposal_unsent(env, drift, why):
    sid = consented(env)
    proposed = pixel(env, sid, 'click_at', x=SEND[0], y=SEND[1])
    drift(env)
    error = refused('stale_ui', env.sessions.commit, proposed['proposal_id'])
    assert why in str(error)
    assert act_row(env, proposed['proposal_id'])['state'] == 'refused' and len(env.lane.sends) == 1


def test_an_act_that_became_riskier_than_reviewed_is_not_sent(env):
    sid = grant(env)
    first = pixel(env, sid, 'click_at', x=BOLD[0], y=BOLD[1])  # the consent card for a toggle (input)
    assert first['class'] == 'input' and first['proposal']['review']['consent'] == 'foreground_input'
    env.ax.answer = {'pid': PID, 'element': {'role': 'AXCheckBox', 'path': 'hit', 'title': 'Bold'}, 'ancestors': [],
                     'in_window': False, 'menu': False}  # now inside a sheet that came up over the window
    error = refused('stale_ui', env.sessions.commit, first['proposal_id'])
    assert 'more than the person reviewed' in str(error) and env.lane.sends == []
    assert env.sessions.runtime[sid].get('input_consent') is not True, 'nothing approved was used'


# ---- capabilities, the schema and the App ----------------------------------------------------------------------

def test_capabilities_follow_the_lane_and_the_persons_own_switches():
    lane = FakeLane()

    def host(tools):
        return SimpleNamespace(screens=SimpleNamespace(adapters={}, revision=1, pixels=lane),
                               task_policy={'mode': 'ask', 'tools': tools})
    native = ss.capabilities(host({'capture': True, 'screen': True}))['native']
    assert (native['capture'], native['foreground_input']) == (True, True)
    native = ss.capabilities(host({'capture': False, 'screen': True}))['native']
    assert (native['capture'], native['foreground_input']) == (False, True), 'Take screenshots off'
    native = ss.capabilities(host({'capture': True, 'screen': False}))['native']
    assert (native['capture'], native['foreground_input']) == (True, False), 'Screen control off'
    lane.capture_on = lane.input_on = False
    native = ss.capabilities(host({'capture': True, 'screen': True}))['native']
    assert (native['capture'], native['foreground_input']) == (False, False), 'the helper or macOS says no'
    assert set(native) == {'ax', 'window_numbers', 'capture', 'foreground_input'}


def test_screen_act_takes_pixel_actions_and_still_refuses_click_and_unknown_arguments():
    spec = ss.SCHEMAS[1]['function']['parameters']
    base = {'session_id': 'a' * 32, 'observation_id': 'b' * 20}
    for args in ({'action': 'click_at', 'x': 10, 'y': 20, 'button': 'right', 'count': 1},
                 {'action': 'scroll_at', 'x': 0, 'y': 0, 'dy': -50}, {'action': 'key', 'keys': 'cmd+s'},
                 {'action': 'type_text', 'text': 'hello'}, {'action': 'drag_at', 'x': 1, 'y': 2, 'x2': 40, 'y2': 50},
                 {'action': 'press', 'element': 'e1@' + 'b' * 20}):
        assert tool_authz.strict_schema_errors(spec, {**base, **args}) == [], args
    for args in ({'action': 'click', 'x': 1, 'y': 1}, {'action': 'click_at', 'x': True, 'y': 1},
                 {'action': 'click_at', 'x': 1.5, 'y': 1}, {'action': 'scroll_at', 'x': 1, 'y': 1, 'dy': 99},
                 {'action': 'click_at', 'x': 1, 'y': 1, 'button': 'middle'}, {'action': 'click_at', 'z': 1}):
        assert tool_authz.strict_schema_errors(spec, {**base, **args}), args
    assert ss.PIXEL_ACTIONS == ('click_at', 'scroll_at', 'key', 'type_text', 'drag_at')  # F262 (1.9.18): drag_at
    assert not set(ss.PIXEL_ACTIONS) & set(ax_worker.ACTIONS), 'pixel acts are never granted as element actions'


def test_the_app_sends_app_window_pictures_first_and_falls_back_to_chrome():
    from pebble.app import App as PebbleApp
    host = SimpleNamespace(screens=SimpleNamespace(screenshot_parts=lambda sid, rid: ['picture']), browser_bridge=None,
                           chrome_tabs=None)
    assert PebbleApp.browser_screenshot_parts(host, 'a' * 32, 'b' * 32) == ['picture']
    host.screens = SimpleNamespace(screenshot_parts=lambda sid, rid: None)
    assert PebbleApp.browser_screenshot_parts(host, 'a' * 32, 'b' * 32) is None


@pytest.fixture
def screen_app(tmp_path, monkeypatch):
    from pebble.app import App as PebbleApp
    from pebble.permissions import DEFAULT
    from pebble.screen_session import AppAdapter, ScreenSessions
    from test_phase16_security import WIDE, quiet
    app = quiet(PebbleApp(tmp_path / 'data', tmp_path / 'resources', lambda *args: None), monkeypatch,
                {'value': {**WIDE, 'input_modalities': ['text', 'image']}})
    clock, ax, lane = Clock(), FakeAX45(), FakeLane()
    running = RunningApp(PID, 1_700_000_000.25)
    ax.apps.append(running)
    running.add(Window45(NUMBER, TITLE, elements()))
    app.screens = ScreenSessions(app.store, {'app': AppAdapter(ax, processes=ax.processes)}, clock=clock.monotonic,
                                 wall=clock.wall, sleep=clock.sleep, pixels=lane)
    app.setting('permissions', {'mode': 'full', 'tools': {**DEFAULT['tools'], 'screen': True}})
    yield SimpleNamespace(app=app, ax=ax, lane=lane)
    app.close()


def test_through_the_app_a_screenshot_is_taken_sent_and_a_pixel_act_waits_for_the_person(screen_app):
    from test_phase16_security import approve, owning, start
    app, lane = screen_app.app, screen_app.lane
    cid, rid = start(app, 'Scroll down the canvas in the Notes window.')
    sid = app.screens.grant(cid, rid, target(), ALL, 600)['session_id']
    with owning(app, cid, rid):
        view = app.execute('ScreenObserve', {'session_id': sid}, None, cid, call_id='1:observe-l4')
        assert view['screenshot']['width'] == 1280
        assert app.browser_screenshot_parts(sid, rid)[0]['text'].startswith('UNTRUSTED screenshot')
        mixed = {'session_id': sid, 'observation_id': view['observation_id'], 'action': 'press',
                 'element': 'e1@' + view['observation_id'], 'x': 3}
        with pytest.raises(ScreenRefused):
            app.execute('ScreenAct', mixed, None, cid, call_id='2:mixed-l4')
        proposed = app.execute('ScreenAct', {'session_id': sid, 'observation_id': view['observation_id'],
                                             'action': 'scroll_at', 'x': CANVAS[0], 'y': CANVAS[1], 'dy': 4}, None,
                               cid, call_id='3:act-l4')
    assert proposed['status'] == 'awaiting_user_approval' and lane.sends == []
    action = next(a for a in app.store.actions(cid) if a['id'] == proposed['action_id'])
    assert action['kind'] == 'screen' and action['payload']['review']['consent'] == 'foreground_input'
    result = approve(app, cid, rid, proposed['action_id'])
    assert result['status'] == 'done' and lane.sends[0][0] == 'scroll' and lane.sends[0][1]['dy'] == -4
    assert 'Pebble scrolled at' in app.action_summary('screen', action['payload'], result)  # 1.9.13 (F197): in words


# ---- Phase 45 (1.9.16, F232 from audit round R10; measured on Chess, native/f232-notes.md) ---------------------------

def pulsing(env, ring=40, width=8):
    """Each capture draws a ring around Bold in the other of two shades, as a selected Chess piece's ring pulses."""
    plain, count = env.lane.capture, {'n': 0}

    def capture(number, own_pids=(), *, ocr=None):
        count['n'] += 1
        shade = 90 if count['n'] % 2 else 170
        ImageDraw.Draw(env.lane.image).ellipse(near(BOLD, ring), outline=(shade,) * 3, width=width)
        return plain(number, own_pids, ocr=ocr)
    env.lane.capture = capture
    return count


def test_a_spot_that_pulses_by_itself_can_still_be_clicked(env):
    sid = consented(env)
    count = pulsing(env)
    view = look(env, sid)
    before = count['n']
    result = env.sessions.act_pixels(sid, env.run, view['observation_id'], 'click_at', x=BOLD[0], y=BOLD[1])
    assert result['status'] == 'done', result
    assert count['n'] - before >= 3, 'the click looked twice more to tell the pulse from a change'
    assert sp.stale(*(lambda a, b: (a, b))(Image.new('L', (200, 200), 90), Image.new('L', (200, 200), 170)),
                    100, 100, 50), 'without the extra looks the pulse reads as a changed spot'


def test_a_real_change_at_a_pulsing_spot_is_still_refused(env):
    sid = consented(env)
    pulsing(env)
    view = look(env, sid)
    paint(env.lane, near(BOLD, 60))  # a dialog came up over the spot, while the ring still pulses on top of it
    error = refused('stale_ui', env.sessions.act_pixels, sid, env.run, view['observation_id'], 'click_at', x=BOLD[0],
                    y=BOLD[1])
    assert 'spot changed' in str(error)


def test_stale_steady_leaves_out_only_what_moves_by_itself():
    base = Image.new('L', (100, 100), 120)
    ring_a, ring_b = base.copy(), base.copy()
    ImageDraw.Draw(ring_a).ellipse((30, 30, 70, 70), outline=40, width=6)
    ImageDraw.Draw(ring_b).ellipse((30, 30, 70, 70), outline=220, width=6)
    assert sp.stale(ring_a, ring_b, 50, 50, 40), 'the pulse alone fails the plain test'
    assert not sp.stale_steady(ring_a, ring_b, [ring_a, ring_b], 50, 50, 40)
    moved_a, moved_b = ring_a.copy(), ring_b.copy()  # something steady changed too, in every capture after the look
    for picture in (moved_a, moved_b):
        ImageDraw.Draw(picture).rectangle((10, 10, 40, 90), fill=0)
    assert sp.stale_steady(ring_a, moved_b, [moved_a, moved_b], 50, 50, 40) is True
    noisy = Image.effect_noise((100, 100), 80)  # nearly everything moves: nothing steady to compare
    assert sp.stale_steady(ring_a, noisy, [Image.effect_noise((100, 100), 80)], 50, 50, 40) is True


def test_the_pulse_is_not_read_back_as_the_clicks_effect():
    still = Image.new('L', (200, 200), 120)
    phases = []
    for shade in (40, 130, 220):
        picture = still.copy()
        ImageDraw.Draw(picture).ellipse((60, 60, 140, 140), outline=shade, width=6)
        phases.append(picture)
    moving = sp.moving_mask(phases[:2])
    assert sp.visible_change(phases[0], phases[2], (100, 100), 60) == 'at_point', 'the plain readback sees the pulse'
    assert sp.visible_change(phases[0], phases[2], (100, 100), 60, moving=moving) == 'none'
    clicked = phases[2].copy()
    ImageDraw.Draw(clicked).rectangle((90, 90, 110, 110), fill=0)  # what the click did, inside the ring
    assert sp.visible_change(phases[0], clicked, (100, 100), 60, moving=moving) == 'at_point'
