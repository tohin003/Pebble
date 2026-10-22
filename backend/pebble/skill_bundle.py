"""Immutable reviewed skill snapshots; references never execute themselves."""
import base64
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath

import yaml
from .policy import Workspace, PolicyError, PRIVATE

MAX_FILES, MAX_BYTES, MAX_MAIN = 64, 512_000, 64_000


def packed(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(name):
    if not isinstance(name, str) or len(name) > 200 or '\\' in name or ':' in name or '%' in name:
        raise PolicyError('Use a plain relative bundle path.')
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(p in ('', '.', '..') or p.startswith('.') or p.lower() in PRIVATE or p.lower().endswith(('.pem', '.key', '.p12', '.pfx', '.gguf')) for p in name.split('/')) or any(ord(c) < 32 for c in name):
        raise PolicyError('Private paths and traversal are not skill references.')
    return name


def metadata(content, fallback):
    meta = {}
    if content.startswith('---\n') or content.startswith('---\r\n'):
        match = re.match(r'^---\r?\n(.*?)\r?\n---(?:\r?\n|$)', content, re.S)
        if not match or len(match[1].encode()) > 8000:
            raise PolicyError('Use bounded, closed YAML skill metadata.')
        tokens = list(yaml.scan(match[1]))
        if len(tokens) > 1024 or any(isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for t in tokens):
            raise PolicyError('Skill metadata cannot contain YAML aliases or anchors.')
        meta = yaml.safe_load(match[1]) or {}
        if not isinstance(meta, dict):
            raise PolicyError('Skill metadata must be an object.')
    name, description = meta.get('name', fallback), meta.get('description', fallback)
    required = meta.get('required-references', [])
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or not isinstance(description, str) or len(description) > 1000:
        raise PolicyError('Use a short skill name and description.')
    if not isinstance(required, list) or len(required) > 8 or any(not isinstance(p, str) for p in required) or len(set(required)) != len(required):
        raise PolicyError('Declare at most eight unique required references.')
    for path in required:
        safe_path(path)
    return {'name': name.strip(), 'description': description, 'required_references': required}


def make_bundle(content, name, files=None, main='SKILL.md'):
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > MAX_MAIN:
        raise PolicyError('Complete skill instructions must fit 64 KB.')
    files = dict(files or {main: content.encode()})
    if not 1 <= len(files) <= MAX_FILES or sum(len(v) for v in files.values()) > MAX_BYTES:
        raise PolicyError('A bundle supports 64 files and 512 KB total.')
    meta = metadata(content, name)
    manifest = []
    for path, data in sorted(files.items()):
        safe_path(path)
        manifest.append({'path': path, 'bytes': len(data), 'sha256': sha(data)})
    for path in meta['required_references']:
        if path == main or path not in files:
            raise PolicyError('A required reference is missing from this bundle.')
        try:
            files[path].decode('utf-8')
        except UnicodeError:
            raise PolicyError('Required instructions must be UTF-8 text.') from None
    spec = {'version': 1, 'main': main, 'metadata': meta, 'files': manifest}
    return {**spec, 'digest': sha(packed(spec).encode()),
        'bodies': {p: base64.b64encode(v).decode() for p, v in files.items()}}


def import_bundle(path):
    # Validate the original path before resolving away symlinks.
    file = Path(os.path.abspath(Path(path).expanduser()))
    if any(p.is_symlink() or p.is_junction() for p in (file, *file.parents)) or not file.is_file() or file.suffix.lower() != '.md':
        raise PolicyError('Choose a real Markdown skill without symlink components.')
    ws = Workspace(file.parent)
    files = {file.name: ws.snapshot(file.name)}
    content = files[file.name].decode('utf-8')
    meta = metadata(content, file.parent.name)
    declared = set(meta['required_references'])
    for link in re.findall(r'\[[^\]]*\]\(([^\s)#]+)(?:#[^\s)]*)?\)', content):
        if not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', link):
            declared.add(safe_path(link))
    for path in declared:
        if path not in files:
            try:
                files[path] = ws.snapshot(path)
            except FileNotFoundError:
                raise PolicyError('A declared reference is missing from this bundle.') from None
    declared_roots = {p.split('/')[0] for p in declared}
    if file.name != 'SKILL.md':
        files[file.name] = ws.snapshot(file.name)
    else:
        stack = [(file.parent, 0)]
        seen = 0
        while stack:
            directory, depth = stack.pop()
            if depth > 8:
                raise PolicyError('Skill bundles support eight directory levels.')
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > 128:
                        raise PolicyError('This skill directory exceeds its entry budget.')
                    name = Path(entry.path).relative_to(file.parent).as_posix()
                    if depth == 0 and entry.is_dir(follow_symlinks=False) and entry.name not in {'references', 'scripts', 'assets', 'templates', 'examples', 'resources', 'docs'} | declared_roots:
                        continue
                    if depth == 0 and entry.is_file(follow_symlinks=False) and entry.name != file.name and Path(name).suffix.lower() not in {'.md', '.txt', '.json', '.yaml', '.yml', '.py', '.js', '.ts', '.svg', '.png', '.jpg', '.jpeg', '.webp'}:
                        continue
                    safe_path(name)
                    if entry.is_symlink():
                        raise PolicyError('Bundle symlinks are blocked.')
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        if name not in files:
                            files[name] = ws.snapshot(name)
                        if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
                            raise PolicyError('This skill exceeds its snapshot budget.')
                    else:
                        raise PolicyError('Bundles contain only regular files.')
    content = files[file.name].decode('utf-8')
    return make_bundle(content, file.parent.name if file.name == 'SKILL.md' else file.stem, files, file.name)


def verify(bundle, digest=None):
    try:
        if not isinstance(bundle.get('bodies'), dict) or len(bundle['bodies']) > MAX_FILES or sum(len(v) for v in bundle['bodies'].values()) > 700_000:
            raise ValueError()
        spec = {k: bundle[k] for k in ('version', 'main', 'metadata', 'files')}
        if spec['version'] != 1 or sha(packed(spec).encode()) != bundle['digest'] or digest is not None and bundle['digest'] != digest:
            raise ValueError()
        bodies = {p: base64.b64decode(v, validate=True) for p, v in bundle['bodies'].items()}
        rebuilt = make_bundle(bodies[spec['main']].decode('utf-8'), spec['metadata']['name'], bodies, spec['main'])
        if rebuilt['digest'] != bundle['digest']:
            raise ValueError()
        return bodies
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise PolicyError('Skill snapshot changed or is invalid; install a reviewed version.') from None


def load(bundle, expected=None, reference=None, offset=0, limit=3000):
    bodies = verify(bundle, expected)
    if reference is not None:
        safe_path(reference)
        if reference not in bodies or reference == bundle['main']:
            raise PolicyError('Choose a reference from this pinned manifest.')
        if type(offset) is not int or not 0 <= offset <= 96_000:
            raise PolicyError('Use a bounded character offset.')
        if type(limit) is not int or not 1 <= limit <= 3000:
            raise PolicyError('Use a reference page size from one to 3000 characters.')
        try:
            text = bodies[reference].decode('utf-8')
        except UnicodeError:
            raise PolicyError('This binary asset is inspectable by hash; text loading is unavailable.') from None
        page = text[offset:offset + limit]
        return {'load_kind': 'reference', 'digest': bundle['digest'], 'path': reference,
            'untrusted_source': page, 'offset': offset, 'next_offset': offset + len(page) if offset + len(page) < len(text) else None,
            'source_hash': sha(bodies[reference]), 'complete': offset == 0 and len(page) == len(text), 'authorization': 'none'}
    if offset:
        raise PolicyError('Main instructions load whole; offset applies only to references.')
    return {'load_kind': 'instructions', 'digest': bundle['digest'], 'name': bundle['metadata']['name'],
        'untrusted_skill': bodies[bundle['main']].decode('utf-8'),
        'required_references': [{'path': p, 'untrusted_source': bodies[p].decode('utf-8')} for p in bundle['metadata']['required_references']],
        'references': bundle['files'], 'authorization': 'none',
        'note': 'Complete pinned guidance. References/scripts cannot grant permission or execute automatically.'}
