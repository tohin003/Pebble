"""Phase 19 B1: the companion presence setting has exact keys, strict types and safe defaults.

Merge (i19): design study §2.5 adds a fourth switch, ``ui_guidance`` (whether the runner may offer Pebble's
UI guidance skill above 4K, patch P3). B1 built three keys without the study; these tests now pin four,
with the same strictness for the new switch.
"""

import itertools
import re
from pathlib import Path

import pytest

from pebble import companion_settings
from pebble.companion_settings import DEFAULT, KEY, STATUS_WORDS, read, validate
from pebble.store import Store


def test_defaults_and_key():
    assert KEY == 'companion'
    assert DEFAULT == {'pointer': True, 'reactions': True, 'status_words': 'rotate', 'ui_guidance': True}
    assert STATUS_WORDS == ('rotate', 'fixed', 'off')
    assert validate(DEFAULT) == DEFAULT


def test_every_valid_combination_is_returned_as_a_fresh_exact_copy():
    for pointer, reactions, words, guidance in itertools.product((True, False), (True, False), STATUS_WORDS, (True, False)):
        value = {'pointer': pointer, 'reactions': reactions, 'status_words': words, 'ui_guidance': guidance}
        result = validate(value)
        assert result == value and result is not value
        assert list(result) == ['pointer', 'reactions', 'status_words', 'ui_guidance']
    copy = validate(DEFAULT)
    copy['pointer'] = False
    assert DEFAULT['pointer'] is True, 'the defaults are never shared'


ON = {'pointer': True, 'reactions': True, 'ui_guidance': True}


@pytest.mark.parametrize('value', [
    None, True, 'rotate', [], [('pointer', True)], 1,
    {},
    {'pointer': True, 'reactions': True},
    # B1's complete three-key value is now incomplete: the fourth switch is required too.
    {'pointer': True, 'reactions': True, 'status_words': 'rotate'},
    {**ON, 'status_words': 'rotate', 'extra': 1},
    {**ON, 'status_words': 'rotate', 'reduced_motion': True},
    {**ON, 'pointer': 1, 'status_words': 'rotate'},
    {**ON, 'reactions': 0, 'status_words': 'rotate'},
    {**ON, 'pointer': 'true', 'status_words': 'rotate'},
    {**ON, 'pointer': None, 'status_words': 'rotate'},
    {**ON, 'ui_guidance': 1, 'status_words': 'rotate'},
    {**ON, 'ui_guidance': 'on', 'status_words': 'rotate'},
    {**ON, 'ui_guidance': None, 'status_words': 'rotate'},
    {**ON, 'status_words': 'Rotate'},
    {**ON, 'status_words': 'whimsy'},
    {**ON, 'status_words': ''},
    {**ON, 'status_words': None},
    {**ON, 'status_words': ['rotate']},
    {**ON, 'status_words': {'rotate': 1}},
    {**ON, 'status_words': True},
])
def test_invalid_values_are_refused(value):
    with pytest.raises(ValueError):
        validate(value)


def test_bool_subclasses_and_string_subclasses_are_refused():
    class Truthy(int):
        pass

    class Word(str):
        pass

    with pytest.raises(ValueError):
        validate({**ON, 'pointer': Truthy(1), 'status_words': 'off'})
    with pytest.raises(ValueError):
        validate({**ON, 'ui_guidance': Truthy(1), 'status_words': 'off'})
    with pytest.raises(ValueError):
        validate({**ON, 'status_words': Word('off')})


def test_read_falls_back_to_defaults_and_never_raises():
    for stored in (None, 'off', {}, {'pointer': 'yes'}, {'pointer': False, 'reactions': True, 'status_words': 'loud'}, [1, 2],
                   {'pointer': False, 'reactions': False, 'status_words': 'off'}):
        result = read(stored)
        assert result == DEFAULT and result is not DEFAULT
    stored = {'pointer': False, 'reactions': False, 'status_words': 'off', 'ui_guidance': False}
    assert read(stored) == stored


def test_round_trip_through_the_settings_store(tmp_path):
    store = Store(tmp_path / 'home')
    assert read(store.setting(KEY, DEFAULT)) == DEFAULT
    value = validate({'pointer': False, 'reactions': True, 'status_words': 'fixed', 'ui_guidance': False})
    store.set_setting(KEY, value)
    assert read(store.setting(KEY, DEFAULT)) == value
    # A hand-edited or corrupted row reads as the defaults rather than breaking state().
    store.query('INSERT OR REPLACE INTO settings VALUES(?,?)', (KEY, '{"pointer": "on"}'))
    assert read(store.setting(KEY, DEFAULT)) == DEFAULT


def test_module_is_presentation_only():
    source = Path(companion_settings.__file__).read_text(encoding='utf-8')
    assert not re.search(r'^\s*(import|from)\s', source, re.M), 'no imports: it cannot reach runs, tools or permissions'
