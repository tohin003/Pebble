"""Phase 24 (t24-build, V1-174): scripts/set_version.py moves the seven version fields together.

Each case copies the seven declaring files into tmp_path; the checkout itself is never written. The parent runs the
real bump (`scripts/set_version.py 1.0.0 --write`) at merge.
"""
import importlib.util
import json
import re
import tomllib
from pathlib import Path

import pytest

import pebble

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('set_version_under_test', ROOT / 'scripts/set_version.py')
sv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sv)
FILES = ('backend/pebble/__init__.py', 'package.json', 'package-lock.json', 'src-tauri/tauri.conf.json',
         'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock')
# f24-locked: the parent's release step ran the real bump (`scripts/set_version.py 1.0.0 --write`), so 1.0.0 is now the
# checkout's own version and set_version refuses it (version_not_higher). The copy moves to the next patch release of
# whatever the checkout declares instead (1.0.0 -> 1.0.1).
MAJOR, MINOR, PATCH = map(int, pebble.VERSION.split('.'))
NEXT = f'{MAJOR}.{MINOR}.{PATCH + 1}'
# The seven fields, by file: package-lock.json declares the version twice (its root and its packages[''] entry).
FIELDS = {'backend/pebble/__init__.py': 1, 'package.json': 1, 'package-lock.json': 2, 'src-tauri/tauri.conf.json': 1,
          'src-tauri/Cargo.toml': 1, 'src-tauri/Cargo.lock': 1}


def checkout(tmp_path):
    root = tmp_path / 'checkout'
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes((ROOT / rel).read_bytes())
    return root


def declared(root):
    """test_release_version_21's reading of every declaration, against another root."""
    tauri = json.loads((root / 'src-tauri/tauri.conf.json').read_text())['version']
    cargo = tomllib.loads((root / 'src-tauri/Cargo.toml').read_text())['package']['version']
    lock = re.search(r'\[\[package\]\]\nname = "pebble"\nversion = "([^"]+)"', (root / 'src-tauri/Cargo.lock').read_text()).group(1)
    package = json.loads((root / 'package.json').read_text())['version']
    npm_lock = json.loads((root / 'package-lock.json').read_text())
    backend = re.search(r"(?m)^VERSION = '([^']+)'$", (root / 'backend/pebble/__init__.py').read_text()).group(1)
    return [backend, tauri, cargo, lock, package, npm_lock['version'], npm_lock['packages']['']['version']]


def test_the_checkout_declares_one_version_and_set_version_reads_all_seven():
    assert set(declared(ROOT)) == {pebble.VERSION}
    found = sv.current(ROOT)
    assert len(found) == 7 and set(found.values()) == {pebble.VERSION} == set(sv.read_back(ROOT).values())


def test_writing_moves_exactly_the_seven_fields_and_nothing_else(tmp_path):
    root = checkout(tmp_path)
    before = {rel: (root / rel).read_bytes() for rel in FILES}
    (root / 'package.json').chmod(0o644)
    modes = {rel: (root / rel).stat().st_mode & 0o777 for rel in FILES}
    plan = sv.set_version(NEXT, root)  # f24-locked: was '1.0.0' before the release step (see NEXT)
    assert plan['written'] is False and plan['from'] == pebble.VERSION and len(plan['fields']) == 7
    assert {rel: (root / rel).read_bytes() for rel in FILES} == before  # a dry run writes nothing
    assert sv.main([NEXT, '--root', str(root), '--write']) == 0
    assert declared(root) == [NEXT] * 7 and set(sv.read_back(root).values()) == {NEXT}
    old, new = pebble.VERSION.encode(), NEXT.encode()
    assert old != new and sum(FIELDS.values()) == 7 and set(FIELDS) == set(FILES)
    for rel in FILES:
        after = (root / rel).read_bytes()
        # Only the version bytes changed; every other byte (formatting, other packages' versions) is the same.
        assert after.count(new) - before[rel].count(new) == before[rel].count(old) - after.count(old) >= 1, rel
        assert len(after) == len(before[rel]) + (len(new) - len(old)) * (before[rel].count(old) - after.count(old))
        # f24-locked: 1.0.0 -> 1.0.1 keeps every length, so each file moves exactly its own fields (seven in all).
        assert before[rel].count(old) - after.count(old) == FIELDS[rel], rel
        assert (root / rel).stat().st_mode & 0o777 == modes[rel]
    lock = json.loads((root / 'package-lock.json').read_text())
    assert lock['packages']['node_modules/vue']['version'] == json.loads(before['package-lock.json'])['packages']['node_modules/vue']['version']
    from pebble import mcp_transport
    assert mcp_transport.MCP_CLIENT_VERSION == '0.4.2'  # D8: never moved by set_version


@pytest.mark.parametrize('version,code', [('1.0.0-rc.1', 'version_not_x_y_z'), ('v1.0.0', 'version_not_x_y_z'),
                                          ('1.0', 'version_not_x_y_z'), ('0.6.0', 'version_not_higher'),
                                          (pebble.VERSION, 'version_not_higher')])
def test_pre_releases_and_downgrades_are_refused(tmp_path, version, code):
    root = checkout(tmp_path)
    with pytest.raises(sv.Refused) as error:
        sv.set_version(version, root, write=True)
    assert error.value.code == code and declared(root) == [pebble.VERSION] * 7


def test_a_checkout_whose_fields_disagree_is_refused_untouched(tmp_path, capsys):
    root = checkout(tmp_path)
    conf = root / 'src-tauri/tauri.conf.json'
    conf.write_text(conf.read_text().replace('"version": "%s"' % pebble.VERSION, '"version": "0.6.0"', 1))
    before = {rel: (root / rel).read_bytes() for rel in FILES}
    assert sv.main(['1.0.0', '--root', str(root), '--write']) == 2
    assert json.loads(capsys.readouterr().err)['refused'] == 'versions_disagree'
    assert {rel: (root / rel).read_bytes() for rel in FILES} == before
