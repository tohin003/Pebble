"""Phase 45 (1.9.14, F203 from audit round R7): keeping the same tool twice in one task asks the person once."""
from types import SimpleNamespace

from pebble import workshop


class Store:
    def __init__(self):
        self.values = {}

    def setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


def test_the_same_tool_already_kept_or_waiting_is_not_asked_again(monkeypatch):
    spec = {'name': 'uxtest_f_to_c', 'purpose': 'Converts Fahrenheit to Celsius.', 'code': 'def run(f):\n    return (f - 32) * 5 / 9\n'}
    digest = workshop.digest(spec)
    record = {'spec': spec, 'digest': digest, 'validated': True, 'validation': {'tests': 3}, 'uses': 0, 'errors': 0}
    app = SimpleNamespace(store=Store(), runs=SimpleNamespace(note=lambda *a, **k: None),
                          emit=lambda *a, **k: (_ for _ in ()).throw(AssertionError('no card')),
                          model=SimpleNamespace(current=lambda: {}))
    monkeypatch.setattr(workshop, '_made', lambda app, rid: {'uxtest_f_to_c': record})
    app.store.set_setting(workshop.SETTING, {'tools': [{'spec': spec, 'digest': digest}], 'pending': []})
    answer = workshop.keep(app, 'r1', 'c1', {'name': 'uxtest_f_to_c'}, validate_tool=None)
    assert answer['kept'] is True and answer['already'] is True
    app.store.set_setting(workshop.SETTING, {'tools': [], 'pending': [{'id': 'p1', 'record': {'spec': spec, 'digest': digest}}]})
    answer = workshop.keep(app, 'r1', 'c1', {'name': 'uxtest_f_to_c'}, validate_tool=None)
    assert answer['asked'] is True and answer['already_asked'] is True
