"""Phase 45 (1.9.16, F240 from audit round R10): Preview's Shapes popover closes once Rectangle is chosen, so the pressed
button is gone after the press. Pebble read that as an unknown outcome and paused the window for the person's check,
six rounds running. An ordinary press whose element left a window that changed did what a press does; a typed field
that vanishes stays unknown, as before."""
from test_screen_session import act, el, env, grant  # noqa: F401


def popover(env):
    env.window.elements += [el('AXButton', 'w/shapes', title='Shapes', frame=[230, 96, 30, 22]),
                            el('AXButton', 'w/pop/rect', title='Rectangle', frame=[209, 165, 38, 38]),
                            el('AXButton', 'w/pop/oval', title='Oval', frame=[245, 165, 38, 38])]

    def choose(app, window, text):
        window.elements = [e for e in window.elements if not e['path'].startswith('w/pop/')] + [
            el('AXGroup', 'w/canvas/shape1', description='Rectangle', frame=[300, 300, 120, 80])]
    env.window.hooks[('w/pop/rect', 'press')] = choose


def test_a_choice_that_closes_its_popover_counts_as_done(env):
    popover(env)
    sid = grant(env)
    result = act(env, sid, 'w/pop/rect', 'press', full_access=True)  # as measured: Full access, an ordinary press
    assert result['class'] == 'input' and (result['verdict'], result['basis']) == ('occurred', 'target_gone_window_changed')
    assert result['needs_reconciliation'] is False and result['session_state'] == 'active' and result['next'] == 'act'
    assert any(row['label'] == 'Rectangle' and row['role'] == 'AXGroup' for row in env.sessions.observation(
        sid, result['observation']['observation_id'])['elements']), 'the window after the press shows the new shape'
    assert env.sessions.observe(sid, env.run)['state'] == 'active', 'the window is not paused'


def test_a_typed_field_that_vanishes_is_still_unknown(env):
    def swallow(app, window, text):
        window.elements = [e for e in window.elements if e['path'] != 'w/to'] + [
            el('AXStaticText', 'w/done', value='Submitted')]
    env.window.hooks[('w/to', 'type')] = swallow
    sid = grant(env)
    result = act(env, sid, 'w/to', 'type', 'x@example.com')
    assert (result['verdict'], result['basis']) == ('unknown', 'target_gone_after_input')


def test_a_swatch_that_closes_its_popover_counts_as_done(env):
    """F252 (audit round R11): Preview's red swatch is a selection button; pressing it closed the colour popover and the
    window was paused for the person's check."""
    env.window.elements += [el('AXButton', 'w/border', title='Border Colour', frame=[260, 96, 30, 22]),
                            el('AXRadioButton', 'w/pop/red', description='rgb 1 0.149131 0 1', selected=False,
                               frame=[209, 165, 20, 20])]

    def pick(app, window, text):
        window.elements = [e for e in window.elements if not e['path'].startswith('w/pop/')]
    env.window.hooks[('w/pop/red', 'press')] = pick
    sid = grant(env)
    result = act(env, sid, 'w/pop/red', 'press', full_access=True)
    assert (result['verdict'], result['basis']) == ('occurred', 'target_gone_window_changed')
    assert result['needs_reconciliation'] is False and env.sessions.observe(sid, env.run)['state'] == 'active'


def test_a_press_the_app_answers_with_an_error_but_may_have_done_is_not_paused(env):
    """F252 (measured, native/f252-notes.md): Preview's red swatch answered AXPress with AX error -25200 and applied the
    colour; with the rows unchanged, the press read as unknown and the window paused. Typing keeps the old rule."""
    from pebble.ax_worker import AXError
    env.window.elements += [el('AXButton', 'w/red', description='rgb 1 0.149131 0 1', frame=[209, 165, 18, 18])]

    def answered_with_error(app, window, text):
        raise AXError('failed', 'AX error -25200')
    env.window.hooks[('w/red', 'press')] = answered_with_error
    sid = grant(env)
    result = act(env, sid, 'w/red', 'press', full_access=True)
    assert (result['verdict'], result['basis']) == ('not_observed', 'app_reported_error'), result
    assert result['needs_reconciliation'] is False and 'Observe with a screenshot' in result['message']
    assert env.sessions.observe(sid, env.run)['state'] == 'active', 'the window is not paused'
    env.window.hooks[('w/to', 'type')] = answered_with_error
    typed = act(env, sid, 'w/to', 'type', 'x')
    assert (typed['verdict'], typed['basis']) == ('unknown', 'dispatch_failed'), 'typing stays unknown and pauses'
