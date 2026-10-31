"""Phase 45 (1.9.15, D142 from audit round R8): a burst of named presses in one act. Calculator's 123 × 456 took 26
model steps for 11 keys; ScreenAct's elements presses the named keys of one observation in order, each the ordinary
act, and stops before a press that needs review, at a refusal or after a press that changed nothing."""
import json

import pytest

from pebble import screen_session as ss
from test_screen_session import (ALL, App, Clock, FakeAX, Store, Window, el, make, refused, target)  # noqa: F401
import uuid
from types import SimpleNamespace

KEYS = [('k/1', '1'), ('k/2', '2'), ('k/3', '3'), ('k/x', 'multiply'), ('k/4', '4'), ('k/5', '5'), ('k/6', '6'),
        ('k/eq', 'equals'), ('k/send', 'Send')]
SHOWN = {'1': '1', '2': '2', '3': '3', 'multiply': '×', '4': '4', '5': '5', '6': '6', 'equals': '='}


@pytest.fixture
def calc(tmp_path):
    clock, ax = Clock(), FakeAX()
    app = App(4242, 1_700_000_000.25)
    ax.apps.append(app)
    elements = [el('AXStaticText', 'k/display', value='0', frame=[10, 10, 200, 30])] + [
        el('AXButton', path, title=title, frame=[10 + 40 * i, 50, 36, 36]) for i, (path, title) in enumerate(KEYS)]
    window = app.add(Window('win-k', 'Calculator', elements))
    display = window.find('k/display')

    def key(sym):
        def press(app, window, text):
            display['value'] = (display['value'] if display['value'] != '0' else '') + sym
        return press
    for path, title in KEYS[:-1]:
        window.hooks[(path, 'press')] = key(SHOWN[title])
    store = Store(tmp_path / 'home')
    sessions = make(store, ax, clock)
    env = SimpleNamespace(clock=clock, ax=ax, window=window, store=store, sessions=sessions, display=display,
                          chat=uuid.uuid4().hex, run=uuid.uuid4().hex)
    env.sid = sessions.grant(env.chat, env.run, target(title='Calculator'), ALL, 600)['session_id']
    yield env
    store.close()


def refs(env, view, *paths):
    record = env.sessions.observation(env.sid, view['observation_id'])
    return ' '.join(f"e{next(e['index'] for e in record['elements'] if e['path'] == p)}@{view['observation_id']}"
                    for p in paths)


def test_a_known_key_sequence_runs_in_one_act(calc):
    view = calc.sessions.observe(calc.sid, calc.run)
    keys = ['k/1', 'k/2', 'k/3', 'k/x', 'k/4', 'k/5', 'k/6', 'k/eq']
    done = calc.sessions.act_sequence(calc.sid, calc.run, view['observation_id'], refs(calc, view, *keys))
    assert calc.display['value'] == '123×456='
    assert [p['verdict'] for p in done['pressed']] == ['occurred'] * 8 and 'stopped' not in done
    assert done['target'] == '1 2 3 multiply 4 5 6 equals' and done['message'].startswith('Pressed 8 of 8 in order.')
    assert done['changes'] == [{'role': 'AXStaticText', 'before': '0', 'after': '123×456='}]
    assert done['observation']['observation_id'] != view['observation_id'], 'the window after the last press'
    assert [c[1] for c in calc.ax.performs()] == keys


def test_a_press_that_needs_review_ends_the_burst_before_it(calc):
    view = calc.sessions.observe(calc.sid, calc.run)
    held = calc.sessions.act_sequence(calc.sid, calc.run, view['observation_id'], refs(calc, view, 'k/1', 'k/send', 'k/2'))
    assert held['status'] == 'needs_native_review' and held['stopped']['code'] == 'review'
    assert [p['target'] for p in held['pressed']] == ['1'] and calc.display['value'] == '1'
    assert [c[1] for c in calc.ax.performs()] == ['k/1'], 'Send and the key after it did not run'


def test_a_press_that_changed_nothing_lets_the_next_one_run(calc):
    """Phase 45 (1.9.17, F245 from audit round R11, deliberate): 1.9.15 stopped a burst after a press that changed
    nothing; Chess's "white pawn, d2" then "d4" stopped after the pawn, whose selection never shows in the rows."""
    calc.window.hooks[('k/2', 'press')] = lambda app, window, text: None
    view = calc.sessions.observe(calc.sid, calc.run)
    done = calc.sessions.act_sequence(calc.sid, calc.run, view['observation_id'], refs(calc, view, 'k/1', 'k/2', 'k/3'))
    assert [p['verdict'] for p in done['pressed']] == ['occurred', 'not_observed', 'occurred'] and 'stopped' not in done
    assert calc.display['value'] == '13' and done['message'].startswith('Pressed 3 of 3 in order. Nothing in the rows '
                                                                       'showed “2” (a selection may not show there).')


def test_bad_bursts_are_refused(calc):
    view = calc.sessions.observe(calc.sid, calc.run)
    refused('invalid', calc.sessions.act_sequence, calc.sid, calc.run, view['observation_id'], '')
    one = refs(calc, view, 'k/1')
    refused('invalid', calc.sessions.act_sequence, calc.sid, calc.run, view['observation_id'], ' '.join([one] * 25))
    refused('unknown_observation', calc.sessions.act_sequence, calc.sid, calc.run, 'f' * 20, one)
    assert calc.ax.performs() == []


def test_the_schemas_offer_elements_within_their_budget():
    assert len(json.dumps(ss.SCHEMAS)) < 2600  # F262 (1.9.18): drag_at's two coordinates and words added 155
    act = next(t for t in ss.SCHEMAS if t['function']['name'] == 'ScreenAct')['function']
    assert act['parameters']['properties']['elements'] == {'type': 'string', 'maxLength': 1600}
    assert 'elements: refs to press in turn' in act['description']


def test_the_keypad_note_and_the_session_line_offer_the_burst():
    from pebble import runner
    assert 'in one ScreenAct with elements' in ss.KEYPAD_NOTE
    line = runner.screen_session_text({'id': 's1', 'kind': 'app', 'actions': ['press'], 'expires_at': None})
    assert 'Press a known sequence of named keys or buttons in one ScreenAct with elements' in line


# ---- Phase 45 (1.9.16, audit round R10) ------------------------------------------------------------------------------

@pytest.fixture
def shifting(tmp_path):
    """Calculator as Accessibility lists it: a child index for a path, so All Clear removing the Last Expression row
    above the keys moves every later key one place up the list (F237)."""
    clock, ax = Clock(), FakeAX()
    app = App(4242, 1_700_000_000.25)
    ax.apps.append(app)
    labels = ['All Clear', '1', '2', '3', 'multiply', '4', '5', '6', 'equals', 'add', 'subtract', '0', '7', '8', '9']
    rows = [el('AXStaticText', 'w/0', value='Last Expression 100 × 10', frame=[10, 0, 200, 20]),
            el('AXStaticText', 'w/1', value='1,000', frame=[10, 20, 200, 30])]
    keys = [el('AXButton', f'w/{2 + i}', title=label, frame=[10 + 40 * i, 60, 36, 36]) for i, label in enumerate(labels)]
    window = app.add(Window('win-s', 'Calculator', rows + keys))
    shown = {'multiply': '×', 'equals': '=', 'add': '+', 'subtract': '−'}

    def press_at(path):
        def press(app, window, text):
            element, display = window.find(path), next(e for e in window.elements if e['role'] == 'AXStaticText'
                                                       and 'Last Expression' not in e['value'])
            if element['title'] == 'All Clear':
                window.elements[:] = [e for e in window.elements if 'Last Expression' not in str(e.get('value'))]
                for index, e in enumerate(window.elements):
                    e['path'] = f'w/{index}'
                display['value'] = '0'
            else:
                display['value'] = (display['value'] if display['value'] not in ('0', '1,000') else '') + \
                    shown.get(element['title'], element['title'])
        return press
    for index in range(len(rows) + len(keys)):
        window.hooks[(f'w/{index}', 'press')] = press_at(f'w/{index}')
    store = Store(tmp_path / 'home')
    sessions = make(store, ax, clock)
    env = SimpleNamespace(clock=clock, ax=ax, window=window, store=store, sessions=sessions,
                          chat=uuid.uuid4().hex, run=uuid.uuid4().hex)
    env.sid = sessions.grant(env.chat, env.run, target(title='Calculator'), ALL, 600)['session_id']
    yield env
    store.close()


def by_label(env, view, *labels):
    record = env.sessions.observation(env.sid, view['observation_id'])
    return ' '.join(f"e{next(e['index'] for e in record['elements'] if e['label'] == label)}@{view['observation_id']}"
                    for label in labels)


def display_of(env):
    return next(e['value'] for e in env.window.elements if e['role'] == 'AXStaticText')


def test_a_row_removed_above_the_keys_never_shifts_the_later_presses(shifting):
    """F237: measured on the person's Calculator, 1.9.15 pressed "All Clear 2 3 Add 4 5 6 Subtract" for AC 1 2 3 × 4 5 6
    = because each later press was found by its old place in the window's list."""
    view = shifting.sessions.observe(shifting.sid, shifting.run)
    sequence = ['All Clear', '1', '2', '3', 'multiply', '4', '5', '6', 'equals']
    done = shifting.sessions.act_sequence(shifting.sid, shifting.run, view['observation_id'],
                                          by_label(shifting, view, *sequence))
    assert done['target'] == 'All Clear 1 2 3 multiply 4 5 6 equals' and 'stopped' not in done
    assert display_of(shifting) == '123×456='
    assert done['message'].startswith('Pressed 9 of 9 in order.')


def test_a_key_that_changed_its_label_ends_the_burst_before_it(shifting):
    """A key that changes its name once pressed (Play becomes Pause) is not that key any more for a later press."""
    play = shifting.window.find('w/2')
    play['title'] = 'Play'
    shifting.window.hooks[('w/2', 'press')] = lambda app, window, text: play.update(title='Pause')
    view = shifting.sessions.observe(shifting.sid, shifting.run)
    done = shifting.sessions.act_sequence(shifting.sid, shifting.run, view['observation_id'],
                                          by_label(shifting, view, 'Play', 'Play', '1'), full_access=True)
    assert [p['target'] for p in done['pressed']] == ['Play'] and done['stopped']['code'] == 'stale_ui'
    assert done['message'].startswith('Pressed 1 of 3 in order. The next element (“Play”) is no longer in the window')
    assert [c[1] for c in shifting.ax.performs()] == ['w/2'], 'nothing after the changed key ran'


def test_on_a_keypad_a_partial_clear_goes_in_its_own_act(shifting):
    """F251 (audit round R11): "Clear 1 2 3 Multiply 4 5 6 Equals" on Calculator's "100 × 10" gave 100 × 123 × 456."""
    shifting.window.find('w/2')['title'] = 'Clear'
    view = shifting.sessions.observe(shifting.sid, shifting.run)
    error = refused('invalid', shifting.sessions.act_sequence, shifting.sid, shifting.run, view['observation_id'],
                    by_label(shifting, view, 'Clear', '1', '2'), full_access=True)
    assert 'press Clear (or C, CE) in its own act' in str(error) and shifting.ax.performs() == [], 'nothing pressed'
    alone = shifting.sessions.act_sequence(shifting.sid, shifting.run, view['observation_id'],
                                           by_label(shifting, view, 'Clear'), full_access=True)
    assert alone['status'] == 'done', 'Clear on its own is the right act'
    shifting.window.find('w/2')['title'] = 'All Clear'
    view = shifting.sessions.observe(shifting.sid, shifting.run)
    done = shifting.sessions.act_sequence(shifting.sid, shifting.run, view['observation_id'],
                                          by_label(shifting, view, 'All Clear', '1', '2'), full_access=True)
    assert done['message'].startswith('Pressed 3 of 3 in order.'), 'All Clear may lead a burst'


def test_one_named_press_reads_back_as_the_ordinary_press(calc):
    """F232: "elements" with one Chess piece answered only "Pressed 1 of 1 in order.", without the single press's
    "make the second press now", and the model took the piece's selection for a failed press."""
    calc.window.hooks[('k/2', 'press')] = lambda app, window, text: None
    view = calc.sessions.observe(calc.sid, calc.run)
    done = calc.sessions.act_sequence(calc.sid, calc.run, view['observation_id'], refs(calc, view, 'k/2'))
    assert done['message'].startswith(ss.UNCHANGED_ROWS) and 'first of two presses' in done['message']
    assert done['target'] == '2' and [p['verdict'] for p in done['pressed']] == ['not_observed']


def test_a_burst_whose_last_press_changed_nothing_says_so(calc):
    calc.window.hooks[('k/2', 'press')] = lambda app, window, text: None
    view = calc.sessions.observe(calc.sid, calc.run)
    done = calc.sessions.act_sequence(calc.sid, calc.run, view['observation_id'], refs(calc, view, 'k/1', 'k/2'))
    assert done['message'].startswith('Pressed 2 of 2 in order. The last press changed nothing in the window')
