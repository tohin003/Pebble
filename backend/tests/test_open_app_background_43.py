"""Phase 43 (1.9.0, decision 9): OpenApp in the background: `open -g`, and the previous front app put back only when
the launched app took the front with no input from the person since the launch. Every opener, process lister, front
reader and idle reader is a fake; nothing launches a real app."""
import plistlib

import pytest

from pebble import native_apps
from pebble.policy import PolicyError

TEAM = 'ABCDE12345'


@pytest.fixture(autouse=True)
def fresh_cache():
    native_apps._cache.clear()
    yield
    native_apps._cache.clear()


def bundle(root, name, bundle_id):
    path = root / f'{name}.app'
    (path / 'Contents/MacOS').mkdir(parents=True, exist_ok=True)
    (path / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': bundle_id, 'CFBundleName': name,
                                                               'CFBundleShortVersionString': '1.0'}))
    (path / 'Contents/MacOS' / name).write_bytes(b'\xcf\xfa\xed\xfe' + name.encode())
    return path


class Clock:
    def __init__(self):
        self.now, self.sleeps = 100.0, 0

    def monotonic(self):
        return self.now

    def wall(self):
        return 1_800_000_000.0 + self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'Applications'
    music, editor = bundle(root, 'Music', 'com.apple.Music'), bundle(root, 'Editor', 'com.example.editor')
    entry = native_apps.pin(str(music), roots=[root], signer=lambda path: ('team', TEAM))
    return root, music, editor, entry


def front_of(path, bundle_id):
    return {'bundle_id': bundle_id, 'path': str(path)}


def run(entry, root, clock, opened, *, fronts, idle=lambda: 600.0, background=None, lister=None):
    """Launch with a scripted front sequence (the last entry repeats)."""
    sequence = list(fronts)

    def front():
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]

    def opener(argv):
        opened.append(argv)
        return 0

    return native_apps.launch(entry, roots=[root], signer=lambda path: ('team', TEAM), opener=opener,
                              lister=lister or (lambda: []), clock=clock.monotonic, wall=clock.wall, sleep=clock.sleep,
                              readback_seconds=0.5, background=background, front=front, idle=idle)


def test_a_foreground_launch_is_1_8_0s(setup):
    root, music, editor, entry = setup
    opened, clock = [], Clock()
    result = run(entry, root, clock, opened, fronts=[None])
    assert opened == [['/usr/bin/open', '-a', str(music)]]
    assert 'background' not in result and 'focus' not in result
    assert native_apps.describe(result) == 'macOS accepted the request to open Music (running: not observed).'
    assert native_apps.launch_argv(entry) == ['/usr/bin/open', '-a', str(music)]


def test_a_background_launch_that_never_takes_the_front_keeps_it(setup):
    root, music, editor, entry = setup
    opened, clock = [], Clock()
    result = run(entry, root, clock, opened, fronts=[front_of(editor, 'com.example.editor')], background=True)
    assert opened == [['/usr/bin/open', '-g', '-a', str(music)]], 'no restore was needed'
    assert result['background'] is True
    assert result['focus'] == {'state': 'kept', 'front_before': 'com.example.editor', 'front_after': 'com.example.editor'}
    assert 'stayed in front' in native_apps.describe(result) and 'in the background' in native_apps.describe(result)


def test_the_payloads_background_flag_is_what_the_person_reviewed(setup):
    root, music, editor, entry = setup
    opened, clock = [], Clock()
    run({**entry, 'background': True}, root, clock, opened, fronts=[front_of(editor, 'com.example.editor')])
    assert opened[0][:2] == ['/usr/bin/open', '-g']
    opened.clear()
    run({**entry, 'background': 'yes'}, root, clock, opened, fronts=[front_of(editor, 'com.example.editor')])
    assert opened[0] == ['/usr/bin/open', '-a', str(music)], 'only a true flag means background'


def test_a_jump_with_no_input_from_the_person_is_put_back(setup):
    root, music, editor, entry = setup
    opened, clock = [], Clock()
    fronts = [front_of(editor, 'com.example.editor'), front_of(editor, 'com.example.editor'),
              front_of(music, 'com.apple.Music'), front_of(editor, 'com.example.editor')]
    result = run(entry, root, clock, opened, fronts=fronts, background=True, idle=lambda: 600.0)
    assert opened == [['/usr/bin/open', '-g', '-a', str(music)], ['/usr/bin/open', '-a', str(editor)]]
    assert result['focus'] == {'state': 'restored', 'front_before': 'com.example.editor',
                               'front_after': 'com.example.editor'}
    assert 'put your previous app back' in native_apps.describe(result)


def test_a_jump_while_the_person_was_using_the_mac_is_left_alone(setup):
    root, music, editor, entry = setup
    opened, clock = [], Clock()
    fronts = [front_of(editor, 'com.example.editor'), front_of(music, 'com.apple.Music')]
    result = run(entry, root, clock, opened, fronts=fronts, background=True, idle=lambda: 0.1)
    assert opened == [['/usr/bin/open', '-g', '-a', str(music)]], 'nothing was put back'
    assert result['focus'] == {'state': 'left', 'front_before': 'com.example.editor', 'front_after': 'com.apple.Music',
                               'reason': 'person_input'}
    unreadable = run(entry, root, Clock(), [], fronts=fronts, background=True, idle=lambda: None)
    assert unreadable['focus']['state'] == 'left' and unreadable['focus']['reason'] == 'input_unreadable'


def test_another_app_coming_forward_is_never_attributed_to_pebble(setup, tmp_path):
    root, music, editor, entry = setup
    other = bundle(tmp_path / 'Elsewhere', 'Mail', 'com.apple.mail')
    opened, clock = [], Clock()
    fronts = [front_of(editor, 'com.example.editor'), front_of(other, 'com.apple.mail')]
    result = run(entry, root, clock, opened, fronts=fronts, background=True)
    assert len(opened) == 1 and result['focus'] == {'state': 'left', 'front_before': 'com.example.editor',
                                                    'front_after': 'com.apple.mail', 'reason': 'other_app'}


def test_an_unconfirmed_restore_and_an_unreadable_front_are_said_as_such(setup):
    root, music, editor, entry = setup
    opened, clock = [], Clock()
    stuck = [front_of(editor, 'com.example.editor'), front_of(music, 'com.apple.Music')]
    result = run(entry, root, clock, opened, fronts=stuck, background=True)
    assert result['focus']['state'] == 'restore_unconfirmed' and len(opened) == 2
    assert 'could not confirm' in native_apps.describe(result)
    blind = run(entry, root, Clock(), [], fronts=[None], background=True)
    assert blind['focus'] == {'state': 'unknown', 'front_before': None, 'front_after': None}
    lost = run(entry, root, Clock(), [], fronts=[front_of(editor, 'com.example.editor'), None], background=True)
    assert lost['focus'] == {'state': 'unknown', 'front_before': 'com.example.editor', 'front_after': None}


def test_an_app_already_in_front_needs_nothing(setup):
    root, music, editor, entry = setup
    opened = []
    result = run(entry, root, Clock(), opened, fronts=[front_of(music, 'com.apple.Music')], background=True)
    assert len(opened) == 1 and result['focus']['state'] == 'kept'


def test_the_watch_ends_within_five_seconds_of_the_launch(setup):
    root, music, editor, entry = setup
    clock = Clock()
    run(entry, root, clock, [], fronts=[front_of(editor, 'com.example.editor')], background=True)
    assert clock.now - 100.0 <= native_apps.FOCUS_SECONDS + native_apps.FOCUS_INTERVAL + 0.5


def test_the_front_app_is_read_from_lsappinfo():
    answers = {
        ('/usr/bin/lsappinfo', 'front'): 'ASN:0x0-0x530a305:\n',
        ('/usr/bin/lsappinfo', 'info', '-only', 'bundleid', '-only', 'bundlepath', 'ASN:0x0-0x530a305:'):
            '[ NULL ]  ASN:0x0-0x530a305: (in front) \n    bundleID="ai.perplexity.comet"\n'
            '    bundle path="/Applications/Comet.app"\n    executable path=[ NULL ] \n',
    }
    capture = lambda argv: answers.get(tuple(argv))
    assert native_apps.front_app(capture=capture) == {'bundle_id': 'ai.perplexity.comet', 'path': '/Applications/Comet.app'}
    assert native_apps.front_app(capture=lambda argv: None) is None
    assert native_apps.front_app(capture=lambda argv: 'nothing here') is None
    odd = dict(answers)
    odd[('/usr/bin/lsappinfo', 'info', '-only', 'bundleid', '-only', 'bundlepath', 'ASN:0x0-0x530a305:')] = \
        '    bundle path="/usr/bin/zsh"\n'
    assert native_apps.front_app(capture=lambda argv: odd.get(tuple(argv))) is None, 'only an app bundle'


def test_the_idle_reader_answers_seconds_or_none(monkeypatch):
    class Broken:
        def __call__(self, *args):
            raise OSError('no CoreGraphics')
    monkeypatch.setattr(native_apps, '_idle_fn', lambda state, kind: 12.5)
    assert native_apps.idle_seconds() == 12.5
    monkeypatch.setattr(native_apps, '_idle_fn', lambda state, kind: float('nan'))
    assert native_apps.idle_seconds() is None
    monkeypatch.setattr(native_apps, '_idle_fn', Broken())
    assert native_apps.idle_seconds() is None


def test_identity_checks_still_come_first(setup):
    root, music, editor, entry = setup
    opened = []
    with pytest.raises(PolicyError, match='predates identity pinning'):
        native_apps.launch({'id': str(music), 'background': True}, roots=[root], opener=opened.append)
    assert opened == []
