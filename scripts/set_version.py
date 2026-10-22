"""Move Pebble's release version everywhere it is declared, in one step (V1-174).

  set_version.py X.Y.Z [--root DIR] [--write]

The seven fields that backend/tests/test_release_version_21.py pins equal: backend/pebble/__init__.py VERSION,
package.json version, package-lock.json version and packages[""].version, src-tauri/tauri.conf.json version,
src-tauri/Cargo.toml [package] version and the pebble entry of src-tauri/Cargo.lock. Each is rewritten in place
(only those bytes change), and every field is read back afterwards.

Refused: a version that is not digits-only X.Y.Z (the install tool's version_key reads no pre-release), one that is
not higher than the current release, and a checkout whose seven fields already disagree. The default is a dry run.
Not touched, deliberately: MCP clientInfo (mcp_transport.MCP_CLIENT_VERSION stays 0.4.2, D8), the browser
extension's own manifest version, and README/CHANGELOG (written by hand).

Exit codes: 0 done (or planned), 2 refused.
"""

import argparse
import ast
import json
import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r'\d+\.\d+\.\d+')
# (field, file, pattern): group 1 is the text before the version, group 2 the version itself.
FIELDS = (
    ('backend', 'backend/pebble/__init__.py', re.compile(r"(?m)^(VERSION = ')([^']+)(')$")),
    ('package.json', 'package.json', re.compile(r'(?s)\A(\{\s*"name":\s*"[^"]*",\s*"version":\s*")([^"]+)(")')),
    ('package-lock.json', 'package-lock.json', re.compile(r'(?s)\A(\{\s*"name":\s*"[^"]*",\s*"version":\s*")([^"]+)(")')),
    ('package-lock.json packages[""]', 'package-lock.json',
     re.compile(r'(?s)("packages":\s*\{\s*"":\s*\{\s*"name":\s*"[^"]*",\s*"version":\s*")([^"]+)(")')),
    ('tauri.conf.json', 'src-tauri/tauri.conf.json', re.compile(r'(?m)^(  "version": ")([^"]+)(",?)$')),
    ('Cargo.toml', 'src-tauri/Cargo.toml', re.compile(r'(?s)\A(\[package\]\nname = "pebble"\nversion = ")([^"]+)(")')),
    ('Cargo.lock', 'src-tauri/Cargo.lock', re.compile(r'(\[\[package\]\]\nname = "pebble"\nversion = ")([^"]+)(")')),
)


class Refused(Exception):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def key(version):
    return tuple(int(part) for part in version.split('.'))


def current(root):
    """{field: version} of the seven fields, each found exactly once."""
    found = {}
    for field, rel, pattern in FIELDS:
        try:
            text = (Path(root) / rel).read_text(encoding='utf-8')
        except OSError:
            raise Refused('file_missing', rel) from None
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise Refused('field_not_found_once', {'field': field, 'file': rel, 'matches': len(matches)})
        found[field] = matches[0].group(2)
    return found


def read_back(root):
    """The seven values as their own parsers read them (the way test_release_version_21 reads them)."""
    root = Path(root)
    line = re.search(r"(?m)^VERSION = ('[^']+')$", (root / 'backend/pebble/__init__.py').read_text()).group(1)
    lock = json.loads((root / 'package-lock.json').read_text())
    return {'backend': ast.literal_eval(line),
            'package.json': json.loads((root / 'package.json').read_text())['version'],
            'package-lock.json': lock['version'], 'package-lock.json packages[""]': lock['packages']['']['version'],
            'tauri.conf.json': json.loads((root / 'src-tauri/tauri.conf.json').read_text())['version'],
            'Cargo.toml': tomllib.loads((root / 'src-tauri/Cargo.toml').read_text())['package']['version'],
            'Cargo.lock': re.search(r'\[\[package\]\]\nname = "pebble"\nversion = "([^"]+)"',
                                    (root / 'src-tauri/Cargo.lock').read_text()).group(1)}


def write_text(path, text):
    """Replace one file atomically, keeping its mode."""
    mode = path.stat().st_mode & 0o7777
    temporary = path.with_name('.%s.%d.tmp' % (path.name, os.getpid()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
    os.fchmod(fd, mode)  # exactly the old mode, whatever the umask
    with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def set_version(new, root=ROOT, write=False):
    if not isinstance(new, str) or not VERSION.fullmatch(new):
        raise Refused('version_not_x_y_z', new)
    found = current(root)
    if len(set(found.values())) != 1:
        raise Refused('versions_disagree', found)
    old = next(iter(found.values()))
    if not VERSION.fullmatch(old) or key(new) <= key(old):
        raise Refused('version_not_higher', {'current': old, 'requested': new})
    texts = {}
    for field, rel, pattern in FIELDS:
        text = texts.get(rel)
        if text is None:
            text = (Path(root) / rel).read_text(encoding='utf-8')
        texts[rel] = pattern.sub(lambda m: m.group(1) + new + m.group(3), text, count=1)
    files = sorted(texts)
    if write:
        for rel in files:
            write_text(Path(root) / rel, texts[rel])
        after = read_back(root)
        if set(after.values()) != {new}:
            raise Refused('read_back_differs', after)
    return {'from': old, 'to': new, 'fields': [field for field, _, _ in FIELDS], 'files': files, 'written': write}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('version')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--write', action='store_true', help='Rewrite the files (default: report only).')
    args = parser.parse_args(argv)
    try:
        result = set_version(args.version, args.root, args.write)
    except Refused as error:
        print(json.dumps({'refused': error.code, 'detail': error.detail}), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
