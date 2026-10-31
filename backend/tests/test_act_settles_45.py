"""Phase 45 (1.9.14, F208 from audit round R8): Black captured on d4 a moment after d2-d4, the act's readback had been
taken at the first change, and Pebble said its move was not made. An app act's result is read once the window settles."""
from types import SimpleNamespace

from pebble import runner
from pebble import screen_session as ss


class Sessions(ss.ScreenSessions):
    def __init__(self, reads):
        self.now, self.reads, self.readback_interval = 0.0, list(reads), ss.READBACK_INTERVAL
        self.clock = lambda: self.now

    def sleep(self, seconds):
        self.now += seconds

    def _snapshot(self, row):
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    @staticmethod
    def _digest(kind, snap):
        return snap['board']


def test_the_result_waits_for_the_apps_own_answer():
    sessions = Sessions([{'board': 'white d4'}, {'board': 'white d4'}, {'board': 'black takes d4'},
                         {'board': 'black takes d4'}])
    settled = sessions._settled({'kind': 'app'}, {'board': 'white d4'}, ss.SETTLE_TURN)  # the move changed the title
    assert settled == {'board': 'black takes d4'}
    assert sessions.now >= ss.SETTLE_TURN and sessions.now <= ss.SETTLE_MAX + ss.READBACK_INTERVAL
    quick = Sessions([{'board': 'white d4'}, {'board': 'white d4'}, {'board': 'white d4'}, {'board': 'black takes d4'}])
    assert quick._settled({'kind': 'app'}, {'board': 'white d4'}) == {'board': 'white d4'}, \
        'without a new title (a key, a field) the short wait applies'


def test_a_window_that_never_stops_changing_is_read_for_at_most_settle_max():
    frames = [{'board': str(n)} for n in range(100)]
    sessions = Sessions(frames)
    sessions._settled({'kind': 'app'}, {'board': 'start'})
    assert ss.SETTLE_MAX <= sessions.now <= ss.SETTLE_MAX + ss.READBACK_INTERVAL + 1e-9


def test_a_quiet_window_costs_only_the_stable_wait():
    sessions = Sessions([{'board': 'same'}])
    assert sessions._settled({'kind': 'app'}, {'board': 'same'}) == {'board': 'same'}
    assert sessions.now <= ss.SETTLE_STABLE + ss.READBACK_INTERVAL + 1e-9


def test_the_session_line_says_an_app_may_answer_on_its_own():
    line = runner.screen_session_text({'id': 's1', 'kind': 'app', 'app': 'Chess', 'title': 'Game 1',
                                       'actions': ['press'], 'expires_at': None})
    assert "An app may answer an act on its own (a game's reply)" in line
