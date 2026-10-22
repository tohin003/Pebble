"""The person's ``companion`` presence setting (Phase 19 B1, design study §2.5).

Four switches, all presentation only: whether the companion's eyes follow the
pointer, whether it shows expressive reactions (a celebration after a finished
task, a reaction to a tap, drowsiness after a long quiet spell), how the status
line words behave, and whether Pebble's own UI guidance skill may be offered to
the model above a 4K window (the runner, parent patch P3). Nothing here changes
what a run may do or grants a permission. The app stores the value through
``App.setting`` and reports it in ``state()['settings']`` (parent patch P6).

Reduced motion (the app's ``reduced_motion`` setting or the system preference)
overrides ``pointer`` and animation in the windows themselves; it is not folded
into this value.
"""

KEY = 'companion'
STATUS_WORDS = ('rotate', 'fixed', 'off')
DEFAULT = {'pointer': True, 'reactions': True, 'status_words': 'rotate', 'ui_guidance': True}
SWITCHES = ('pointer', 'reactions', 'ui_guidance')


def validate(value):
    """Return a fresh, exact copy of a complete companion setting, or raise ValueError."""
    if not isinstance(value, dict) or set(value) != set(DEFAULT):
        raise ValueError('Supply exactly pointer, reactions, status_words and ui_guidance for the companion.')
    if any(type(value[name]) is not bool for name in SWITCHES):
        raise ValueError('Companion pointer, reactions and UI guidance are each on or off.')
    words = value['status_words']
    if type(words) is not str or words not in STATUS_WORDS:
        raise ValueError('Companion status words are rotate, fixed or off.')
    return {'pointer': value['pointer'], 'reactions': value['reactions'], 'status_words': words,
            'ui_guidance': value['ui_guidance']}


def read(stored):
    """The stored setting, or the defaults when it is absent or malformed (never raises)."""
    try:
        return validate(stored)
    except ValueError:
        return dict(DEFAULT)
