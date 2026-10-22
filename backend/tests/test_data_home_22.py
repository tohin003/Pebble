"""Phase 22 (s22-storage): resolving Pebble's home, the ~/.pebble layout and the protected roots.

Every test runs under a fake HOME (tmp_path); nothing here reads or writes the real ~/.pebble, ~/Documents or
~/Library.
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from pebble import data_home
from pebble.data_home import Layout, resolve


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    assert Path.home() == home
    yield home
    data_home.deactivate()


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_resolution_order_is_home_flag_then_environment_then_dot_pebble(fake_home, tmp_path):
    flat = resolve(str(tmp_path / 'validator-home'), env={})
    assert (flat.root, flat.structured, flat.source, flat.legacy) == (tmp_path / 'validator-home', False, '--home', None)
    assert flat.data == flat.root  # tests and validators keep the database directly in --home

    other = tmp_path / 'elsewhere'
    from_env = resolve(None, env={'PEBBLE_HOME': str(other)})
    assert (from_env.root, from_env.structured, from_env.source) == (other, True, 'PEBBLE_HOME')
    assert from_env.data == other / 'data' and from_env.legacy is None
    # --home wins over PEBBLE_HOME.
    assert resolve(str(tmp_path / 'x'), env={'PEBBLE_HOME': str(other)}).root == tmp_path / 'x'

    default = resolve(None, env={})
    assert (default.root, default.structured, default.source) == (fake_home / '.pebble', True, 'default')
    if sys.platform == 'darwin':
        assert default.legacy == fake_home / 'Library/Application Support/com.pebble.local'


def test_the_shells_arguments_and_a_marked_home_are_structured(fake_home, tmp_path):
    legacy = tmp_path / 'old'
    shell = resolve(str(fake_home / '.pebble'), str(legacy), env={})
    assert shell.structured and shell.legacy == legacy and shell.data == fake_home / '.pebble' / 'data'
    assert shell.is_default
    assert resolve(str(fake_home / '.pebble'), env={}).structured  # ~/.pebble itself, even without --legacy-home
    named = resolve(str(tmp_path / 'named'), str(legacy), env={})
    assert named.structured and not named.is_default
    marked = tmp_path / 'marked'
    marked.mkdir()
    (marked / data_home.MARKER).write_text('{}')
    assert resolve(str(marked), env={}).structured
    # A marker reached through a link does not count.
    linked = tmp_path / 'linked'
    linked.mkdir()
    os.symlink(marked / data_home.MARKER, linked / data_home.MARKER)
    assert not resolve(str(linked), env={}).structured


def test_an_existing_flat_home_is_never_reinterpreted(fake_home, tmp_path):
    old = tmp_path / 'flat'
    old.mkdir()
    (old / 'pebble.sqlite').write_bytes(b'')
    for layout in (resolve(None, env={'PEBBLE_HOME': str(old)}), resolve(str(old), str(tmp_path / 'legacy'), env={})):
        assert not layout.structured and layout.data == old and layout.legacy is None


def test_ensure_layout_creates_the_private_internal_folder(fake_home):
    layout = resolve(None, env={})
    data_home.ensure_layout(layout)
    root = fake_home / '.pebble'
    assert mode(root) == 0o700
    for name in ('backups', 'runtime', 'cache', 'logs', 'skills'):
        assert (root / name).is_dir() and mode(root / name) == 0o700
    assert not (root / 'data').exists(), 'data/ is left to the move or to the first start'
    assert data_home.marked(root)
    readme = (root / 'README.md').read_text()
    for needle in ('`data/`', '**No**', 'settings.json', 'PEBBLE.md', 'mcp.json', 'Keychain', 'MOVED.md',
                   'gives Pebble more access', 'not synced by iCloud'):
        assert needle in readme
    schema = json.loads((root / 'settings.schema.json').read_text())
    assert schema['additionalProperties'] is True and set(schema['properties']) >= {
        'theme', 'permissions', 'memory_layers', 'notifications', 'storage', 'companion'}
    template = (root / 'PEBBLE.md').read_text()
    assert template.startswith('<!--') and template.rstrip().endswith('-->')
    assert (root / 'skills' / 'README.md').read_text().startswith('# skills')
    # The person's PEBBLE.md is never overwritten; generated files are refreshed only when they differ.
    (root / 'PEBBLE.md').write_text('My name is Sam.\n')
    before = os.lstat(root / 'README.md').st_mtime_ns
    data_home.ensure_layout(layout)
    assert (root / 'PEBBLE.md').read_text() == 'My name is Sam.\n'
    assert os.lstat(root / 'README.md').st_mtime_ns == before
    # A shared mode is tightened again.
    os.chmod(root, 0o755)
    data_home.ensure_layout(layout)
    assert mode(root) == 0o700


def test_a_flat_layout_creates_nothing(tmp_path):
    flat = resolve(str(tmp_path / 'flat'), env={})
    data_home.ensure_layout(flat)
    assert not (tmp_path / 'flat').exists()
    assert data_home.migrate(flat)['state'] == 'not_applicable'


def test_a_linked_home_or_file_is_refused(fake_home, tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    os.symlink(target, fake_home / '.pebble')
    with pytest.raises(data_home.DataHomeError):
        data_home.ensure_layout(resolve(None, env={}))
    assert list(target.iterdir()) == []
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep')
    os.symlink(victim, tmp_path / 'link.json')
    with pytest.raises(data_home.DataHomeError):
        data_home.write_atomic(tmp_path / 'link.json', b'{}')
    assert victim.read_text() == 'keep'


def test_write_atomic_replaces_whole_files_privately(tmp_path):
    folder = tmp_path / 'atomic'
    folder.mkdir()
    path = folder / 'file.json'
    data_home.write_atomic(path, b'one')
    data_home.write_atomic(path, b'two')
    assert path.read_bytes() == b'two' and mode(path) == 0o600
    assert [p.name for p in folder.iterdir()] == ['file.json'], 'no temporary file is left behind'
    assert data_home.refresh(path, b'two') is False and data_home.refresh(path, b'three') is True


def test_protected_roots_cover_dot_pebble_the_old_folder_and_active_homes(fake_home, tmp_path, monkeypatch):
    inside = data_home.inside_protected
    assert inside(fake_home / '.pebble') and inside(fake_home / '.pebble' / 'data' / 'pebble.sqlite')
    assert inside(fake_home / 'Library/Application Support/com.pebble.local/pebble.sqlite')
    assert inside(str(fake_home / '.PEBBLE' / 'settings.json')), 'compared case-insensitively, like APFS'
    assert not inside(fake_home / '.pebble-other') and not inside(fake_home / 'Documents' / 'Pebble')
    assert not inside(tmp_path / 'project')
    (fake_home / '.pebble').mkdir()
    project = tmp_path / 'project'
    project.mkdir()
    os.symlink(fake_home / '.pebble', project / 'sneaky')
    assert inside(project / 'sneaky' / 'settings.json'), 'a link into ~/.pebble is resolved first'
    custom = tmp_path / 'custom-home'
    monkeypatch.setenv('PEBBLE_HOME', str(custom))
    assert inside(custom / 'data')
    monkeypatch.delenv('PEBBLE_HOME')
    elsewhere = Layout(tmp_path / 'activated', True, '--home', tmp_path / 'old-home')
    assert not inside(tmp_path / 'activated' / 'data')
    data_home.activate(elsewhere, elsewhere.data)
    assert inside(tmp_path / 'activated' / 'data') and inside(tmp_path / 'old-home' / 'x')


def test_for_data_finds_the_activated_or_marked_layout_only(fake_home, tmp_path):
    flat = tmp_path / 'flat'
    flat.mkdir()
    assert data_home.for_data(flat) == (None, None)
    plain = tmp_path / 'plain' / 'data'
    plain.mkdir(parents=True)
    assert data_home.for_data(plain) == (None, None), 'a folder named data is not enough'
    layout = resolve(None, env={})
    data_home.ensure_layout(layout)
    found, _ = data_home.for_data(layout.data)
    assert found.root == layout.root and found.structured
    legacy = tmp_path / 'legacy'
    data_home.activate(layout, legacy, {'state': 'refused', 'reason': 'legacy_in_use'})
    found, report = data_home.for_data(legacy)
    assert found is layout and report['reason'] == 'legacy_in_use'


def test_store_on_a_flat_home_has_no_settings_file(tmp_path):
    from pebble.store import Store
    store = Store(tmp_path / 'flat')
    try:
        assert store.settings_file is None
        store.set_setting('theme', 'dark')
        assert not (tmp_path / 'flat' / 'settings.json').exists()
    finally:
        store.close()


def test_main_resolves_the_home_and_keeps_a_flat_home_exactly(tmp_path):
    source = (Path(__file__).resolve().parents[1] / 'main.py').read_text()
    assert 'parser.add_argument("--legacy-home")' in source
    assert 'data_home.resolve(args.home, args.legacy_home)' in source
    assert 'Path(args.home) if args.home else layout.root' in source
    assert 'logging.basicConfig(filename=logs / "backend.log"' in source
    rust = (Path(__file__).resolve().parents[2] / 'src-tauri/src/main.rs').read_text()
    spawn = rust[rust.index('fn pebble_home('):rust.index('let reader = BufReader::new(')]
    assert 'app.path().app_data_dir()' in spawn and '.join(".pebble")' in spawn and '"PEBBLE_HOME"' in spawn
    assert '"--legacy-home"' in spawn and 'home.join("logs").join("service.log")' in spawn
    assert 'path.is_absolute()' in spawn
    # The backend environment allowlist is unchanged: PEBBLE_HOME is resolved by the shell, never passed on.
    assert '"PATH","HOME","USER","LOGNAME","SHELL","LANG","LC_ALL","LC_CTYPE","TMPDIR","__CF_USER_TEXT_ENCODING"' in rust
