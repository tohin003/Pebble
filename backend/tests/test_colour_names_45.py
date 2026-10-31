"""Phase 45 (1.9.17, F253 from audit round R11): Preview names its colour swatches by their values ("rgb 1 0.149131 0
1"), so the model, the approval card and Pebble's own check could not tell the red one; a shown label adds the nearest
plain colour name. Labels are matched and hashed as Accessibility gives them."""
from pebble import screen_session as ss, step_judge as sj
from test_popover_choice_45 import act, el, env, grant  # noqa: F401


def test_colour_values_get_a_plain_name():
    assert ss.named_colour('rgb 1 0.149131 0 1') == 'rgb 1 0.149131 0 1 (red)'
    assert ss.named_colour('rgb 1 0.576372 0 1') == 'rgb 1 0.576372 0 1 (orange)'
    assert ss.named_colour('rgba(255, 59, 48, 1)') == 'rgba(255, 59, 48, 1) (red)'
    for plain in ('Red', 'Border Colour', 'rgb', '', None):
        assert ss.named_colour(plain) == plain


def test_the_model_and_the_card_see_the_colour(env):
    env.window.elements += [el('AXRadioButton', 'w/red', description='rgb 1 0.149131 0 1', selected=False,
                               frame=[209, 165, 20, 20])]
    sid = grant(env)
    view = env.sessions.observe(sid, env.run)
    assert any(e.get('label') == 'rgb 1 0.149131 0 1 (red)' for e in view['elements'])
    said = sj.describe('screen', {'review': {'element': {'label': ss.named_colour('rgb 1 0.149131 0 1')},
                                             'window': {'title': 'UXTEST-canvas.png'}, 'action': 'press'}})
    assert '(red)' in said
