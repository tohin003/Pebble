"""Phase 45 (1.9.15, F225 from audit round R9): a click by position in Preview was refused because Pebble's own window
covered the spot, and Pebble asked the person to grant the window again. When the window in front is Pebble's own,
Pebble's windows make room (the companion collapses to the pebble) and the click is tried once more."""
import pytest

from pebble import screen_session as ss


class Screens:
    def __init__(self, failures):
        self.failures, self.calls = list(failures), 0

    def act_pixels(self, *args, **kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {'status': 'done', 'args': args}


def test_pebbles_own_window_in_front_makes_room_and_tries_once_more():
    emitted, slept = [], []
    screens = Screens([ss.ScreenRefused('covered', "In front of it now: Pebble's own window.", own=True)])
    done = ss.act_pixels_making_room(screens, lambda *a: emitted.append(a), slept.append, 's1', 'r1', 'o1', 'click_at')
    assert done['status'] == 'done' and screens.calls == 2
    assert emitted == [('make_room', {})] and slept == [ss.MAKE_ROOM_SECONDS]


def test_another_cover_or_a_second_refusal_stands():
    other = ss.ScreenRefused('covered', 'In front of it now: Finder.')
    with pytest.raises(ss.ScreenRefused):
        ss.act_pixels_making_room(Screens([other]), lambda *a: None, lambda s: None, 's1', 'r1', 'o1', 'click_at')
    twice = Screens([ss.ScreenRefused('covered', 'x', own=True), ss.ScreenRefused('covered', 'x', own=True)])
    with pytest.raises(ss.ScreenRefused):
        ss.act_pixels_making_room(twice, lambda *a: None, lambda s: None, 's1', 'r1', 'o1', 'click_at')
    assert twice.calls == 2, 'one more try only'
