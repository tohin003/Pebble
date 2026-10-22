"""Phase 24 (t24-data, V1-014): a Documents copy that is there but cannot be read is never a deletion.

In 0.6.1 a conversation copy counted as present only if its first line could be read: a file without read permission,
a 0-byte stand-in or an iCloud file kept only online ("Optimize Mac Storage", dataless) moved its conversation to
Recently deleted on the next pass, and 30 days later it was deleted for good. Here:
- a file with the recorded name is present, whatever it holds or whether it can be read;
- a dataless file is never opened (opening downloads it) and never counts as gone;
- a name Pebble did not write that cannot be read may be the person's renamed copy, so nothing missing is trashed;
- an absence counts only when it held on two passes at least CONFIRM_SECONDS apart.
The Memory folder follows the same rules: an unreadable, empty or dataless layer file forgets nothing.
Under a fake HOME with an injected clock; the dataless flag comes from a monkeypatched lstat.
"""

import inspect
import os
import shutil
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import data_home, data_mirror
from pebble.app import App
from pebble.data_home import resolve
from pebble.data_mirror import DataServices
from pebble.store import VISIBLE_CHATS

# Read softly: these tests also ran against 0.6.1's module, which has none of these names.
CONFIRM = getattr(data_mirror, 'CONFIRM_SECONDS', 60.0)
SF_DATALESS = getattr(data_mirror, 'SF_DATALESS', 0x40000000)
BACKGROUND = {'background': True} if 'background' in inspect.signature(DataServices.tick).parameters else {}
GAPPED = {'sleep'} & set(inspect.signature(DataServices).parameters)


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    monkeypatch.setenv('AGENTGUARD_HOME', str(tmp_path / 'guard-unused'))
    data_home.deactivate()
    yield home
    data_home.deactivate()


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def env(fake_home):
    layout = resolve(str(fake_home / '.pebble'), None, env={})
    data_home.ensure_layout(layout)
    report = data_home.migrate(layout)
    data_home.activate(layout, Path(report['data']), report)
    original = DataServices.start
    DataServices.start = lambda self: None  # these tests drive their own instance
    try:
        app = App(Path(report['data']), fake_home / 'resources', lambda *event: None)
    finally:
        DataServices.start = original
    clock, gaps, during = Clock(), [], []

    def nap(seconds):  # the CONFIRM_GAP wait of a pass the person started; `during` changes files within it
        gaps.append(seconds)
        if during:
            during.pop(0)()
    services = DataServices(app, opener=lambda path: None, clock=clock, **({'sleep': nap} if GAPPED else {}))
    folder = fake_home / 'Documents' / 'Pebble' / 'Conversations'
    yield SimpleNamespace(app=app, services=services, clock=clock, folder=folder, gaps=gaps, during=during,
                          memory=fake_home / 'Documents' / 'Pebble' / 'Memory')
    for path in list(folder.glob('*')) + list((fake_home / 'Documents' / 'Pebble' / 'Memory').glob('*')):
        try:
            os.chmod(path, 0o600)  # pytest must be able to clean up
        except OSError:
            pass
    services.stop()
    app.close()


def chat(app, title='Plan a trip to Lisbon'):
    cid = app.store.new_chat()
    app.store.message(cid, 'user', title)
    app.store.message(cid, 'assistant', 'Here is a plan.')
    return cid


def copies(env):
    return sorted(p.name for p in env.folder.iterdir() if p.name != 'README.md')


def listed(app):
    return {r['id'] for r in app.store.query('SELECT id FROM chats WHERE ' + VISIBLE_CHATS)}


def passes(env, *gaps):
    """One pass the person started (Update copies now), then a background poll pass after each gap in seconds."""
    env.services.tick()
    for gap in gaps:
        env.clock.now += gap
        env.services.tick(**BACKGROUND)


def background(env, seconds=0):
    env.clock.now += seconds
    return env.services.tick(**BACKGROUND)


def refresh(env):
    return env.services.methods()['storage_refresh']()


def dataless(monkeypatch, *paths):
    """macOS's SF_DATALESS on these files (an iCloud file kept only online), as os.lstat would report it."""
    names = {os.path.realpath(p) for p in paths}
    real = os.lstat

    def lstat(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        if os.path.realpath(path) in names:
            return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
                                   st_flags=getattr(info, 'st_flags', 0) | SF_DATALESS)
        return info
    monkeypatch.setattr(data_mirror, '_lstat', lstat, raising=False)
    return names


def watch_opens(monkeypatch, names):
    """The opens of these files, recorded (a failure raised inside the storage pass would only stop that pass)."""
    real, seen = os.open, []

    def recorded(path, *args, **kwargs):
        if os.path.realpath(path) in names:
            seen.append(path)
        return real(path, *args, **kwargs)
    monkeypatch.setattr(data_mirror.os, 'open', recorded)
    return seen


# ---- conversations ----------------------------------------------------------------------------------------------------

def test_a_copy_without_read_permission_is_present(env):
    keep, other = chat(env.app, 'Keep this one'), chat(env.app, 'Another one')
    env.services.tick()
    [name] = [n for n in copies(env) if 'Keep this one' in n]
    os.chmod(env.folder / name, 0)
    passes(env, 30, 61, 600)
    assert env.app.store.trashed() == {} and {keep, other} <= listed(env.app)
    os.chmod(env.folder / name, 0o600)
    env.services.tick()
    assert keep in listed(env.app)


def test_a_zero_byte_stand_in_is_present_and_kept_until_the_conversation_changes(env):
    cid = chat(env.app)
    env.services.tick()
    [name] = copies(env)
    (env.folder / name).write_bytes(b'')
    passes(env, 61, 600)
    assert env.app.store.trashed() == {} and cid in listed(env.app)
    assert (env.folder / name).read_bytes() == b'', 'an unchanged conversation never overwrites it'
    env.app.store.message(cid, 'assistant', 'Day two: Belem.')
    env.services.tick()
    assert 'Day two: Belem.' in (env.folder / name).read_text(), 'a change writes the copy again'


def test_a_dataless_copy_is_present_and_never_opened(env, monkeypatch):
    cid = chat(env.app)
    env.services.tick()
    [name] = copies(env)
    (env.folder / name).write_bytes(b'')  # what is on the disk of a file kept only in iCloud
    opened = watch_opens(monkeypatch, dataless(monkeypatch, env.folder / name))
    passes(env, 61, 600)
    assert env.app.store.trashed() == {} and cid in listed(env.app)
    assert opened == [], 'a dataless file is never opened (that downloads it)'


def test_a_renamed_dataless_copy_leaves_the_answer_open(env, monkeypatch):
    cid = chat(env.app)
    env.services.tick()
    [name] = copies(env)
    os.rename(env.folder / name, env.folder / 'renamed in Finder.md')
    (env.folder / 'renamed in Finder.md').write_bytes(b'')  # kept only in iCloud: nothing readable on this Mac
    opened = watch_opens(monkeypatch, dataless(monkeypatch, env.folder / 'renamed in Finder.md'))
    passes(env, 61, 600)
    assert env.app.store.trashed() == {} and cid in listed(env.app), 'it may be the missing one: nothing is trashed'
    assert opened == [], 'a dataless file is never opened (that downloads it)'


def test_a_renamed_copy_without_read_permission_leaves_the_answer_open(env):
    cid = chat(env.app)
    env.services.tick()
    [name] = copies(env)
    os.rename(env.folder / name, env.folder / 'renamed in Finder.md')
    os.chmod(env.folder / 'renamed in Finder.md', 0)
    passes(env, 61, 600)
    assert env.app.store.trashed() == {} and cid in listed(env.app)
    os.chmod(env.folder / 'renamed in Finder.md', 0o600)
    env.services.tick()
    assert cid in listed(env.app) and copies(env) == [name], 'read again, it is found and named back'


def test_a_background_absence_must_hold_on_two_passes_a_minute_apart(env):
    cid = chat(env.app)
    env.services.tick()
    [name] = copies(env)
    text = (env.folder / name).read_bytes()
    os.unlink(env.folder / name)
    background(env)
    background(env, CONFIRM - 1)
    assert env.app.store.trashed() == {}, 'not yet a minute'
    assert copies(env) == [], 'never written back while Pebble waits'
    (env.folder / name).write_bytes(text)  # back (an atomic save or a sync rename finished)
    background(env, 2)
    background(env, 600)
    assert env.app.store.trashed() == {} and cid in listed(env.app)
    os.unlink(env.folder / name)
    background(env)
    result = background(env, CONFIRM)
    assert result['reconcile']['trashed'] == 1 and cid in env.app.store.trashed()
    assert env.app.store.trashed()[cid]['origin'] == 'documents'


# ---- Update copies now: the person's own pass confirms within itself, on every point -------------------------------

def test_update_copies_now_moves_a_deleted_copy_within_the_pass(env):
    cid = chat(env.app)
    refresh(env)
    [name] = copies(env)
    os.unlink(env.folder / name)
    refresh(env)
    assert cid in env.app.store.trashed() and env.gaps == [getattr(data_mirror, 'CONFIRM_GAP', 2.0)]


def test_update_copies_now_moves_nothing_that_is_back_saving_a_placeholder_or_folderless(env):
    cid = chat(env.app)
    refresh(env)
    [name] = copies(env)
    path = env.folder / name
    text = path.read_bytes()
    # Back by the second look (an atomic save, a sync rename finishing).
    os.unlink(path)
    env.during.append(lambda: path.write_bytes(text))
    refresh(env)
    assert env.app.store.trashed() == {} and env.gaps, 'two looks were taken'
    # An atomic save's temporary file beside it (macOS safe save, Pebble's own, an editor's): never on either path.
    for temporary in (path.with_name(path.stem + '.sb-1a2b3c4d-XyZ'), path.with_name('.' + name + '.tmp-0f1e2d'),
                      path.with_name(name + '~')):
        os.unlink(path)
        temporary.write_bytes(b'partial save')
        refresh(env)
        background(env)
        background(env, 600)
        assert env.app.store.trashed() == {}, temporary.name
        os.unlink(temporary)
        path.write_bytes(text)
        refresh(env)
    # Kept by iCloud only online: the placeholder stands for it, on either path.
    os.rename(path, env.folder / ('.' + name + '.icloud'))
    refresh(env)
    background(env, 600)
    assert env.app.store.trashed() == {}
    os.rename(env.folder / ('.' + name + '.icloud'), path)
    # The whole folder gone: written again, nothing moved, on either path.
    shutil.rmtree(env.folder)
    refresh(env)
    background(env, 600)
    assert env.app.store.trashed() == {} and cid in listed(env.app) and copies(env)


# ---- memory --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('stand_in', ['no_permission', 'zero_bytes', 'dataless'])
def test_an_unreadable_memory_file_forgets_nothing(env, monkeypatch, stand_in):
    ids = [env.app.memory.put('semantic', f'Fact {n} about the lighthouse.') for n in range(3)]
    env.services.tick()
    path = env.memory / 'Facts & knowledge.md'
    if stand_in == 'no_permission':
        os.chmod(path, 0)
    elif stand_in == 'zero_bytes':
        path.write_bytes(b'')
    else:
        path.write_bytes(b'')
        opened = watch_opens(monkeypatch, dataless(monkeypatch, path))
    passes(env, 61, 600)
    found = {r['id'] for r in env.app.store.query("SELECT id FROM memories WHERE kind='semantic'")}
    assert set(ids) <= found and not env.app.store.query('SELECT 1 FROM memory_tombstones')
    if stand_in == 'dataless':
        assert opened == [], 'a dataless file is never opened (that downloads it)'
    assert env.services.summary()['documents']['confirm'] == []
    if stand_in == 'no_permission':
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0, 'the person\'s file is left as it is'
