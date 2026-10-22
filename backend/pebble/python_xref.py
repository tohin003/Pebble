"""Cross-file Python imports, definitions, graded references and diagnostics from ``ast`` alone.

Answers come from parsing exact disk snapshots or staged drafts of in-root files.
Nothing is imported, compiled to bytecode or executed, and no other program
runs. The file inventory is a bounded walk that never follows links and skips
hidden, excluded and private folders. Every file is read through
``Workspace.snapshot`` (``Workspace.read`` for a staged draft), so private names,
symbolic links and files over 96 KB never contribute. An import resolves only
to a file in that inventory; otherwise it is ``external`` (no in-root
candidate at all) or ``unresolved`` (it should be in the root but is not).

``definition`` follows Python's lookup order: the enclosing function scopes,
then module level, then the imported binding, and finally the definition in the
target module's outline. It crosses re-exports and star imports, at most
MAX_HOPS hops. Attributes are followed through modules, classes and their
in-root bases, and through ``self``/``cls``/``Class()`` receivers.
``references`` grades every occurrence of the name:

- ``resolved``: that same lookup lands on the definition;
- ``attribute_candidate``: ``x.name`` whose receiver cannot be typed;
- ``textual``: strings, comments, unparsed text, or names whose binding cannot
  be decided (star imports from outside, keywords of unknown callees).

Occurrences that resolve to some other definition are excluded and counted.
``diagnostics`` reports syntax errors and imports that should resolve inside the
root but do not.

Results are hash pinned: each row names the SHA-256 and basis of the text it
came from, and cursors bind every file state that shaped the answer. Results
are bounded (walk entries, read and parsed bytes, hops, rows) and report what
they did not cover. This is not a type checker. There is no inference beyond
the receivers above, no runtime ``sys.path``, no stubs outside the root, and
conditional imports are not evaluated. Results are untrusted evidence that
grants no Read witness or edit permission.
"""

import ast
import base64
import builtins
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections import Counter, namedtuple

from . import code_intelligence, search_ranking
from .code_intelligence import SUFFIXES
from .policy import EXCLUDED, MAX_FILE, PRIVATE, PolicyError, digest

VERSION = 'python-xref-1'
MAX_ENTRIES = 50_000  # walk entries examined
MAX_DEPTH = 64
MAX_FILES = 20_000  # Python files inventoried
READ_BUDGET = 64 * 1024 * 1024  # source bytes one call may read
PARSE_BUDGET = 8 * 1024 * 1024  # source bytes one call may parse
MAX_HOPS = 8  # imports, re-exports, star imports and base classes followed per lookup
MAX_DEFINITIONS = 8  # bindings reported per name
MAX_REFERENCES = 2_000  # occurrences collected before paging
REFERENCE_LIMIT = 100
MAX_DIAGNOSTICS = 200
MAX_NAME = 256
MAX_CURSOR = 2_048
TEXT_CHARS = 100
GRADES = ('resolved', 'attribute_candidate', 'textual')
SCOPE = ('in-root Python (.py/.pyi) files up to 96 KB and staged drafts parsed with ast; hidden/excluded '
         'folders, symbolic links and private paths omitted; nothing imported or executed; no type inference')
NOTE = ('Untrusted evidence from static parsing. It grants no Read witness or edit permission; '
        'Read a file before changing it.')
STALE = 'Project files changed since this cursor was issued. Repeat the request without a cursor.'

_BUILTINS = frozenset(dir(builtins))
_KEY = secrets.token_bytes(32)  # cursors authenticate within this process only
_DEF = re.compile(r'(?:async\s+)?(?:def|class)\s+')
_FROM = re.compile(r'from\s+((?:\.\s*)*)')
_WORD = re.compile(r'\w+')
_AS = re.compile(r'\bas\s+(\w+)')
_INIT = ('__init__.py', '__init__.pyi')

Binding = namedtuple('Binding', 'name kind line column node scope info')
Import = namedtuple('Import', 'module name level line column')  # name None for `import m`
Ident = namedtuple('Ident', 'line column name role node scope info')


class CursorError(PolicyError):
    """A references cursor that this request cannot honor."""


def _stop(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise InterruptedError('Stopped by you.')


def _private(path):
    for part in path.split('/'):
        lower = part.lower()
        if lower in PRIVATE or lower.startswith('.env') or lower.endswith(('.pem', '.key', '.p12', '.pfx', '.gguf')):
            return True
    return False


def _walked(name):
    parts = name.split('/')
    return len(parts) - 1 <= MAX_DEPTH and not any(
        part in EXCLUDED or part.startswith('.') or part.lower() in PRIVATE for part in parts[:-1])


def _parent(path):
    return path.rsplit('/', 1)[0] if '/' in path else ''


def _join(folder, name):
    return f'{folder}/{name}' if folder else name


def _check_root(ws):
    try:
        info = os.stat(ws.root, follow_symlinks=False)
    except OSError:
        info = None
    if info is None or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != ws.identity:
        raise PolicyError('The selected workspace was replaced or removed. Start a fresh task.')


# Inventory -------------------------------------------------------------------------------------


def _inventory(ws, cancel):
    """Sorted eligible Python paths (plus staged new files) and walk statistics."""
    walk = {'files': [], 'seen': 0, 'errors': 0, 'truncated': False, 'cancel': cancel}
    anchored = (os.scandir in os.supports_fd and os.open in os.supports_dir_fd
                and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'))
    walk['anchored'] = anchored
    if anchored:
        try:
            root = os.open(ws.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            _check_root(ws)
            raise PolicyError('The selected project folder could not be opened.') from None
        try:
            info = os.fstat(root)
            if (info.st_dev, info.st_ino) != ws.identity:
                raise PolicyError('The selected workspace was replaced or removed. Start a fresh task.')
            _visit(walk, root, '', 0)
        finally:
            os.close(root)
    else:
        _check_root(ws)
        _visit(walk, str(ws.root), '', 0)
    files = set(walk['files'])
    for name in sorted(ws.drafts):
        if (isinstance(name, str) and name.endswith(SUFFIXES) and name not in files and _walked(name)
                and not _private(name) and len(files) < MAX_FILES):
            files.add(name)
    return sorted(files), walk


def _visit(walk, handle, prefix, depth):
    try:
        with os.scandir(handle) as listing:
            entries = []
            for entry in listing:
                if walk['seen'] + len(entries) >= MAX_ENTRIES:
                    walk['truncated'] = True
                    break
                entries.append(entry)
    except OSError:
        walk['errors'] += 1
        return
    walk['seen'] += len(entries)
    _stop(walk['cancel'])
    for entry in sorted(entries, key=lambda item: item.name):
        name = entry.name
        try:
            if entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)():
                continue
            if entry.is_dir(follow_symlinks=False):
                if name in EXCLUDED or name.startswith('.') or name.lower() in PRIVATE:
                    continue
                if depth >= MAX_DEPTH:
                    walk['truncated'] = True
                elif walk['anchored']:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
                    try:
                        _visit(walk, child, f'{prefix}{name}/', depth + 1)
                    finally:
                        os.close(child)
                else:
                    _visit(walk, entry.path, f'{prefix}{name}/', depth + 1)
            elif name.endswith(SUFFIXES) and entry.is_file(follow_symlinks=False):
                path = prefix + name
                path.encode('utf-8')  # undecodable names can be neither read nor requested
                if _private(path):
                    continue
                if len(walk['files']) >= MAX_FILES:
                    walk['truncated'] = True
                    continue
                walk['files'].append(path)
        except InterruptedError:
            raise
        except (OSError, UnicodeError):
            walk['errors'] += 1


class _Corpus:
    """One request's view: inventory, bounded reads and parses, and every file state it saw."""

    def __init__(self, ws, cancel):
        self.ws, self.cancel = ws, cancel
        self.paths, self.walk = _inventory(ws, cancel)
        self.files = set(self.paths)
        self.dirs = {''}
        for path in self.paths:
            folder = _parent(path)
            while folder not in self.dirs:
                self.dirs.add(folder)
                folder = _parent(folder)
        self.modules = {}  # path -> _Module or None
        self.states = {}  # path -> (status, hash, basis) for every file read or refused
        self.counts = Counter()
        self.read_left, self.parse_left = READ_BUDGET, PARSE_BUDGET
        self._roots = None

    # Reading

    def text(self, path):
        """((text, hash, basis, size), None) or (None, skip reason); never raises for one file."""
        module = self.modules.get(path)
        if module is not None:  # one version per request: never reread a parsed file
            return (module.text, module.hash, module.basis, len(module.text.encode('utf-8'))), None
        if path in self.states and self.states[path][0] not in ('read', 'deferred'):
            return None, self.states[path][0]
        ws, draft = self.ws, self.ws.drafts.get(path)
        _stop(self.cancel)
        if draft is not None:
            try:
                text = ws.read(path)  # drops a draft whose disk baseline moved
            except (OSError, UnicodeError, PolicyError):
                return self._skip(path, 'unreadable')
            data = text.encode('utf-8')
            basis = 'draft' if ws.drafts.get(path) is draft and text == draft['after'] else 'disk'
            return self._loaded(path, text, data, basis)
        try:
            info = os.stat(ws.root / path, follow_symlinks=False)
        except FileNotFoundError:
            return self._skip(path, 'missing')
        except OSError:
            return self._skip(path, 'unreadable')
        if not stat.S_ISREG(info.st_mode):
            return self._skip(path, 'refused')
        if info.st_size > MAX_FILE:
            return self._skip(path, 'large')
        if info.st_size > self.read_left:
            return self._skip(path, 'read_deferred')
        try:
            data = ws.snapshot(path)
        except PermissionError:
            return self._skip(path, 'denied')
        except FileNotFoundError:
            return self._skip(path, 'missing')
        except PolicyError:
            _check_root(ws)  # a replaced root aborts the request instead of describing another folder
            return self._skip(path, 'refused')
        except OSError:
            return self._skip(path, 'unreadable')
        self.read_left -= len(data)
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            return self._skip(path, 'binary', digest(data))
        return self._loaded(path, text, data, 'disk')

    def _skip(self, path, reason, file_hash=None):
        if path not in self.states:
            self.counts['skip:' + reason] += 1
        self.states[path] = (reason, file_hash, None)
        return None, reason

    def _loaded(self, path, text, data, basis):
        file_hash = digest(data)
        status = 'read'
        if path not in self.states:
            self.counts['read'] += 1
        else:
            if self.states[path][1] not in (None, file_hash):
                self.counts['changed_during_request'] += 1  # reported; the newer text is the one described
            if self.states[path][0] == 'deferred':
                status = 'deferred'  # read again for a text scan; still never parsed
        self.states[path] = (status, file_hash, basis)
        return (text, file_hash, basis, len(data)), None

    def module(self, path, loaded=None):
        """The parsed module at an inventory path, or None (missing, skipped or deferred)."""
        if path in self.modules:
            return self.modules[path]
        found = None
        if path in self.files:
            if loaded is None:
                loaded, _reason = self.text(path)
            if loaded is not None:
                text, file_hash, basis, size = loaded
                if size > self.parse_left:
                    self.counts['deferred'] += 1
                    self.states[path] = ('deferred', file_hash, basis)
                else:
                    _stop(self.cancel)
                    self.parse_left -= size
                    found = _Module(path, text, file_hash, basis)
                    self.counts[found.status] += 1
                    if found.issue:
                        self.counts['issue:' + found.issue] += 1
                    self.states[path] = (found.status, file_hash, basis)
        self.modules[path] = found
        return found

    # Import resolution

    def roots(self, path):
        """Absolute-import roots for one file: its own top-level package root, the project, then others."""
        if self._roots is None:
            found = set()
            for candidate in self.paths:
                folder, name = _parent(candidate), candidate.rsplit('/', 1)[-1]
                if name in _INIT and folder and not self._package(_parent(folder)):
                    found.add(_parent(folder))
            self._roots = sorted(found)
        own = _parent(path)
        while own and self._package(own):
            own = _parent(own)
        ordered = []
        for root in (own, '', *self._roots):
            if root not in ordered:
                ordered.append(root)
        return ordered

    def _package(self, folder):
        return any(_join(folder, name) in self.files for name in _INIT) if folder else False

    def _find(self, base, parts, namespace):
        folder = '/'.join(parts)
        stem = _join(base, folder)
        if not namespace:
            for candidate in (_join(stem, '__init__.py'), stem + '.py', _join(stem, '__init__.pyi'), stem + '.pyi'):
                if candidate in self.files:
                    return ('module', candidate)
            return None
        return ('namespace', stem) if stem in self.dirs else None

    def resolve(self, origin, module, level):
        """('module', path) | ('namespace', folder) | ('external', name) | ('unresolved', reason)."""
        parts = [part for part in (module or '').split('.') if part]
        if level:
            base = _parent(origin)
            for _ in range(level - 1):
                if not base:
                    return ('unresolved', 'beyond_root')
                base = _parent(base)
            if not parts:  # from . import name
                init = next((_join(base, name) for name in _INIT if _join(base, name) in self.files), None)
                return ('module', init) if init else ('namespace', base)
            return self._find(base, parts, False) or self._find(base, parts, True) or ('unresolved', 'missing')
        if not parts:
            return ('unresolved', 'missing')
        roots = self.roots(origin)
        for namespace in (False, True):
            for root in roots:
                found = self._find(root, parts, namespace)
                if found:
                    return found
        if any(self._find(root, parts[:1], False) or self._find(root, parts[:1], True) for root in roots):
            return ('unresolved', 'missing')  # the top-level package is in the root; the rest is not
        return ('external', '.'.join(parts))

    def submodule(self, folder, name):
        return self._find(folder, [name], False) or self._find(folder, [name], True)

    # Coverage

    def coverage(self):
        skipped = {key[5:]: value for key, value in sorted(self.counts.items()) if key.startswith('skip:')}
        issues = {key[6:]: value for key, value in sorted(self.counts.items()) if key.startswith('issue:')}
        return {
            'scope': SCOPE, 'python_files': len(self.paths), 'inventory_truncated': self.walk['truncated'],
            'inventory_errors': self.walk['errors'], 'entry_limit': MAX_ENTRIES, 'depth_limit': MAX_DEPTH,
            'file_limit': MAX_FILES, 'files_read': self.counts['read'], 'parsed': self.counts['parsed'],
            'partial': self.counts['partial'], 'failed': self.counts['failed'], 'deferred': self.counts['deferred'],
            'parse_issues': issues, 'skipped': sum(skipped.values()), 'skip_reasons': skipped,
            'file_size_limit': MAX_FILE, 'read_budget': READ_BUDGET, 'parse_budget': PARSE_BUDGET,
            'hop_limit': MAX_HOPS, 'hops_limited': self.counts['hops_limited'],
            'changed_during_request': self.counts['changed_during_request'],
        }

    def scan_complete(self):
        return not (self.walk['truncated'] or self.walk['errors'] or self.counts['changed_during_request'])

    def consulted_complete(self, paths):
        return all(self.states.get(path, ('parsed',))[0] == 'parsed' for path in paths)


# One parsed module ------------------------------------------------------------------------------


class _Scope:
    __slots__ = ('kind', 'name', 'node', 'parent', 'bindings', 'globals', 'nonlocals', 'stars', 'attrs',
                 'binding', 'first', 'span')

    def __init__(self, kind, name, node, parent):
        self.kind, self.name, self.node, self.parent = kind, name, node, parent
        self.bindings, self.globals, self.nonlocals, self.stars, self.attrs = {}, set(), set(), [], {}
        self.binding = None  # the class or function binding that owns this scope
        self.first = None  # first positional parameter name of a function scope
        self.span = None  # (first line, last line) in splitlines numbering


class _Module:
    """Text, hash, parse status, scopes, bindings and identifier occurrences of one file."""

    def __init__(self, path, text, file_hash, basis):
        self.path, self.text, self.hash, self.basis = path, text, file_hash, basis
        parsed = code_intelligence.syntax_tree(text)
        self.status, self.issue, self.error = parsed.status, parsed.issue, parsed.error
        self.places, self.tree = parsed.places, parsed.tree
        self.lines = text.splitlines()
        self.package = path.rsplit('/', 1)[-1] in _INIT
        self.folder = _parent(path)
        self.scope = _Scope('module', None, self.tree, None)
        self.scopes, self.idents, self.imports, self.classes = [self.scope], [], [], {}
        self.covered = parsed.places.through(parsed.cut) if parsed.cut is not None else (
            parsed.places.total if self.tree is not None else 0)
        if self.tree is not None:
            try:
                self._build()
            except (RecursionError, MemoryError):
                self.status, self.issue, self.covered = 'failed', 'too_complex', 0
                self.scope = _Scope('module', None, None, None)
                self.scopes, self.idents, self.imports, self.classes = [self.scope], [], [], {}
        for scope in self.scopes:
            for found in scope.bindings.values():
                found.sort(key=lambda item: (item.line, item.column))

    # Positions

    def at(self, node):
        return self.places.start(node.lineno, node.col_offset)

    def end_line(self, node):
        return max(self.places.end(node.end_lineno, node.end_col_offset), self.at(node)[0])

    def line_text(self, line):
        return self.lines[line - 1] if 1 <= line <= len(self.lines) else ''

    def _after(self, node, name):
        """Where ``name`` ends a node (``as`` names, attributes), verified against the text."""
        line, after = self.places.start(node.end_lineno, node.end_col_offset)
        column = after - len(name)
        return (line, column) if column >= 1 and self.line_text(line)[column - 1:after - 1] == name else None

    def _named(self, node, name, keyword_end=None):
        """First occurrence of ``name`` as a word on the node's first line, after ``keyword_end``."""
        line, column = self.at(node)
        text = self.line_text(line)
        for found in _WORD.finditer(text, keyword_end if keyword_end is not None else column - 1):
            if found.group() == name:
                return line, found.start() + 1
        return None

    # Building

    def _build(self):
        stack = [(self.tree, self.scope)]
        while stack:
            node, scope = stack.pop()
            visit = getattr(self, '_v_' + type(node).__name__, None)
            if visit is not None:
                visit(node, scope, stack)
            else:
                stack.extend((child, scope) for child in reversed(list(ast.iter_child_nodes(node))))

    def bind(self, scope, name, kind, place, node, info=None):
        if place is None:
            place = self.at(node)
        binding = Binding(name, kind, place[0], place[1], node, scope, info)
        scope.bindings.setdefault(name, []).append(binding)
        return binding

    def ident(self, place, name, role, node, scope, info=None):
        if place is not None:
            self.idents.append(Ident(place[0], place[1], name, role, node, scope, info))

    def _child(self, kind, name, node, parent, binding=None):
        scope = _Scope(kind, name, node, parent)
        scope.binding = binding
        if node is not None and hasattr(node, 'end_lineno'):
            scope.span = (self.at(node)[0], self.end_line(node))
        self.scopes.append(scope)
        return scope

    def _v_FunctionDef(self, node, scope, stack):
        kind = 'method' if scope.kind == 'class' else 'function'
        if isinstance(node, ast.AsyncFunctionDef):
            kind = 'async_' + kind
        binding = self.bind(scope, node.name, kind, self.at(node), node, node)
        found = _DEF.match(self.line_text(binding.line), binding.column - 1)
        if found and self.line_text(binding.line).startswith(node.name, found.end()):
            self.ident((binding.line, found.end() + 1), node.name, 'definition', node, scope, binding)
        inner = self._child('function', node.name, node, scope, binding)
        self._arguments(node, scope, inner, stack)
        if node.returns is not None:
            stack.append((node.returns, scope))
        for item in getattr(node, 'type_params', None) or ():
            stack.append((item, inner))
        stack.extend((statement, inner) for statement in reversed(node.body))
        stack.extend((decorator, scope) for decorator in reversed(node.decorator_list))

    _v_AsyncFunctionDef = _v_FunctionDef

    def _v_Lambda(self, node, scope, stack):
        inner = self._child('function', '<lambda>', node, scope)
        self._arguments(node, scope, inner, stack)
        stack.append((node.body, inner))

    def _arguments(self, node, scope, inner, stack):
        arguments = node.args
        positional = arguments.posonlyargs + arguments.args
        inner.first = positional[0].arg if positional else None
        for default in arguments.defaults + [item for item in arguments.kw_defaults if item is not None]:
            stack.append((default, scope))
        every = positional + arguments.kwonlyargs + [item for item in (arguments.vararg, arguments.kwarg) if item]
        for item in every:
            binding = self.bind(inner, item.arg, 'parameter', self.at(item), item)
            self.ident((binding.line, binding.column), item.arg, 'parameter', item, inner, binding)
            if item.annotation is not None:
                stack.append((item.annotation, scope))

    def _v_ClassDef(self, node, scope, stack):
        binding = self.bind(scope, node.name, 'class', self.at(node), node, node)
        found = _DEF.match(self.line_text(binding.line), binding.column - 1)
        if found and self.line_text(binding.line).startswith(node.name, found.end()):
            self.ident((binding.line, found.end() + 1), node.name, 'definition', node, scope, binding)
        inner = self._child('class', node.name, node, scope, binding)
        self.classes[id(node)] = inner
        for item in getattr(node, 'type_params', None) or ():
            stack.append((item, inner))
        stack.extend((statement, inner) for statement in reversed(node.body))
        stack.extend((item, scope) for item in reversed(node.bases + [k.value for k in node.keywords]
                                                        + node.decorator_list))

    def _v_Import(self, node, scope, stack):
        for alias in node.names:
            start = self.at(alias)
            offset = 0
            for index, part in enumerate(alias.name.split('.')):
                place = (start[0], start[1] + offset)
                if self.line_text(place[0])[place[1] - 1:place[1] - 1 + len(part)] == part:
                    prefix = '.'.join(alias.name.split('.')[:index + 1])
                    self.ident(place, part, 'module', alias, scope, Import(prefix, None, 0, *place))
                offset += len(part) + 1
            if alias.asname:
                place = self._after(alias, alias.asname) or start
                info = Import(alias.name, None, 0, *place)
                binding = self.bind(scope, alias.asname, 'import', place, alias, info)
                self.ident(place, alias.asname, 'import', alias, scope, binding)
            else:
                top = alias.name.split('.')[0]
                info = Import(top, None, 0, *start)
                self.bind(scope, top, 'import', start, alias, info)
            self.imports.append((node, alias, scope, info))

    def _v_ImportFrom(self, node, scope, stack):
        line, column = self.at(node)
        text = self.line_text(line)
        found = _FROM.match(text, column - 1)
        module, level = node.module or '', node.level or 0
        if found and module:
            offset, parts = found.end(), module.split('.')
            for index, part in enumerate(parts):
                word = _WORD.match(text, offset)
                if not word or word.group() != part:
                    break
                prefix = '.'.join(parts[:index + 1])
                self.ident((line, word.start() + 1), part, 'module', node, scope, Import(prefix, None, level, line,
                                                                                          word.start() + 1))
                offset = word.end()
                while offset < len(text) and text[offset] in ' \t.':
                    offset += 1
        for alias in node.names:
            start = self.at(alias)
            if alias.name == '*':
                info = Import(module, '*', level, *start)
                scope.stars.append(info)
                self.imports.append((node, alias, scope, info))
                continue
            info = Import(module, alias.name, level, *start)
            place = (self._after(alias, alias.asname) or start) if alias.asname else start
            binding = self.bind(scope, alias.asname or alias.name, 'import', place, alias, info)
            self.ident(start, alias.name, 'import_name', alias, scope, binding)
            if alias.asname:
                self.ident(place, alias.asname, 'import', alias, scope, binding)
            self.imports.append((node, alias, scope, info))

    def _v_Global(self, node, scope, stack):
        target = scope.globals if isinstance(node, ast.Global) else scope.nonlocals
        keyword = len('global' if isinstance(node, ast.Global) else 'nonlocal')
        for name in node.names:
            target.add(name)
            line, column = self.at(node)
            self.ident(self._named(node, name, column - 1 + keyword), name, 'global', node, scope)

    _v_Nonlocal = _v_Global

    def _store(self, scope, name, place, node, kind='variable', info=None):
        if name in scope.nonlocals:
            return None
        if name in scope.globals:
            scope = self.scope
        return self.bind(scope, name, kind, place, node, info)

    def _v_Name(self, node, scope, stack):
        place = self.at(node)
        if isinstance(node.ctx, ast.Store):
            self._store(scope, node.id, place, node)
        self.ident(place, node.id, 'name', node, scope)

    def _v_Assign(self, node, scope, stack):
        # `name = value` keeps its value, so `name = Class(...)` can type `name.attr` later.
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0]
            place = self.at(target)
            self._store(scope, target.id, place, target, info=node.value)
            self.ident(place, target.id, 'name', target, scope)
            stack.append((node.value, scope))
        else:
            stack.extend((item, scope) for item in reversed([*node.targets, node.value]))

    def _v_AnnAssign(self, node, scope, stack):
        if isinstance(node.target, ast.Name):
            place = self.at(node.target)
            self._store(scope, node.target.id, place, node.target, info=node.value)
            self.ident(place, node.target.id, 'name', node.target, scope)
        else:
            stack.append((node.target, scope))
        if node.value is not None:
            stack.append((node.value, scope))
        stack.append((node.annotation, scope))

    def _v_NamedExpr(self, node, scope, stack):
        target = node.target
        place = self.at(target)
        holder = scope
        while holder.kind == 'comprehension':
            holder = holder.parent
        self._store(holder, target.id, place, target)
        self.ident(place, target.id, 'name', target, scope)
        stack.append((node.value, scope))

    def _v_TypeAlias(self, node, scope, stack):
        place = self.at(node.name)
        self._store(scope, node.name.id, place, node.name, 'type_alias')
        self.ident(place, node.name.id, 'name', node.name, scope)
        stack.append((node.value, scope))
        stack.extend((item, scope) for item in getattr(node, 'type_params', None) or ())

    def _v_Attribute(self, node, scope, stack):
        place = self._after(node, node.attr)
        self.ident(place, node.attr, 'attribute', node, scope)
        if (isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name) and scope.kind == 'function'
                and scope.first == node.value.id and scope.parent is not None and scope.parent.kind == 'class'
                and place is not None):
            # self.name = ... inside a method: an instance attribute of that class
            scope.parent.attrs.setdefault(node.attr, []).append(
                Binding(node.attr, 'attribute', place[0], place[1], node, scope.parent, None))
        stack.append((node.value, scope))

    def _v_Call(self, node, scope, stack):
        for keyword in node.keywords:
            if keyword.arg is not None and hasattr(keyword, 'lineno'):
                self.ident(self.at(keyword), keyword.arg, 'keyword', keyword, scope, node.func)
            stack.append((keyword.value, scope))
        stack.extend((item, scope) for item in reversed(node.args))
        stack.append((node.func, scope))

    def _comprehension(self, node, scope, stack, results):
        generators = node.generators
        inner = self._child('comprehension', None, node, scope)
        for index, generator in enumerate(generators):
            stack.append((generator.target, inner))
            stack.extend((item, inner) for item in generator.ifs)
            stack.append((generator.iter, inner if index else scope))
        stack.extend((item, inner) for item in results)

    def _v_ListComp(self, node, scope, stack):
        self._comprehension(node, scope, stack, [node.elt])

    _v_SetComp = _v_GeneratorExp = _v_ListComp

    def _v_DictComp(self, node, scope, stack):
        self._comprehension(node, scope, stack, [node.key, node.value])

    def _v_ExceptHandler(self, node, scope, stack):
        if node.name:
            line, column = self.at(node)
            found = _AS.search(self.line_text(line), column - 1)
            place = (line, found.start(1) + 1) if found and found.group(1) == node.name else None
            self._store(scope, node.name, place or self.at(node), node)
            self.ident(place, node.name, 'name', node, scope)
        if node.type is not None:
            stack.append((node.type, scope))
        stack.extend((statement, scope) for statement in reversed(node.body))

    def _v_MatchAs(self, node, scope, stack):
        if node.name:
            place = self._after(node, node.name) if node.pattern is not None else self.at(node)
            self._store(scope, node.name, place or self.at(node), node)
            self.ident(place, node.name, 'name', node, scope)
        if node.pattern is not None:
            stack.append((node.pattern, scope))

    def _v_MatchStar(self, node, scope, stack):
        if node.name:
            place = self._after(node, node.name)
            self._store(scope, node.name, place or self.at(node), node)
            self.ident(place, node.name, 'name', node, scope)

    def _v_MatchMapping(self, node, scope, stack):
        if node.rest:
            line, after = self.places.start(node.end_lineno, node.end_col_offset)
            text = self.line_text(line)
            column = text.rfind('**' + node.rest, 0, after) + 3
            place = (line, column) if column > 2 else None
            self._store(scope, node.rest, place or self.at(node), node)
            self.ident(place, node.rest, 'name', node, scope)
        stack.extend((item, scope) for item in reversed(node.keys + node.patterns))

    # Queries

    def qualname(self, binding):
        parts, scope = [binding.name], binding.scope
        while scope is not None and scope.kind != 'module':
            if scope.kind == 'function':
                parts.append('<locals>')
            if scope.kind != 'comprehension':
                parts.append(scope.name)
            scope = scope.parent
        return '.'.join(reversed(parts))

    def scope_at(self, line):
        """The innermost function or class scope whose span holds ``line``, else module scope."""
        best = self.scope
        for scope in self.scopes:
            if scope.span and scope.kind in ('function', 'class') and scope.span[0] <= line <= scope.span[1]:
                if best.span is None or scope.span[0] >= best.span[0] and scope.span[1] <= best.span[1]:
                    best = scope
        return best

    def exported(self, name):
        """Whether ``from this import *`` binds ``name``: a literal ``__all__``, else public names.

        None when ``__all__`` is computed, extended or assigned more than once.
        """
        if '__all__' not in self.scope.bindings:
            return not name.startswith('_')
        values = []
        for statement in self.tree.body if self.tree is not None else ():
            targets = (statement.targets if isinstance(statement, ast.Assign) else
                       [statement.target] if isinstance(statement, (ast.AnnAssign, ast.AugAssign)) else [])
            if any(isinstance(target, ast.Name) and target.id == '__all__' for target in targets):
                if isinstance(statement, ast.AugAssign) or len(targets) != 1:
                    return None
                values.append(statement.value)
        if len(values) != 1 or len(self.scope.bindings['__all__']) != 1:
            return None
        value = values[0]
        if not isinstance(value, (ast.List, ast.Tuple)) or not all(
                isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts):
            return None
        return name in {item.value for item in value.elts}


# Resolution ------------------------------------------------------------------------------------


class _Found:
    """One lookup outcome: a binding, a module, or why there is none."""

    __slots__ = ('kind', 'module', 'binding', 'path', 'name', 'certain', 'instance')

    def __init__(self, kind, module=None, binding=None, path=None, name=None, certain=True, instance=False):
        self.kind, self.module, self.binding, self.path, self.name = kind, module, binding, path, name
        self.certain, self.instance = certain, instance

    def key(self):
        if self.kind == 'binding':
            return (self.module.path, self.binding.line, self.binding.column)
        return (self.kind, self.path or self.name)


def _unknown(reason, path=None):
    return _Found('unknown', name=reason, path=path)


# Receivers whose type this module cannot know; `receiver.name` may be any definition.
_UNTYPED = frozenset({'untyped_receiver', 'call_result', 'expression', 'unbound'})
_LITERALS = {ast.Constant: None, ast.JoinedStr: 'str', ast.List: 'list', ast.ListComp: 'list', ast.Dict: 'dict',
             ast.DictComp: 'dict', ast.Set: 'set', ast.SetComp: 'set', ast.Tuple: 'tuple'}


class _Resolver:
    def __init__(self, corpus):
        self.corpus = corpus
        self.consulted = set()  # files whose text shaped an answer

    def module_of(self, target):
        """A resolution tuple from _Corpus.resolve as a _Found."""
        kind, value = target
        if kind == 'module':
            return _Found('module', path=value)
        if kind == 'namespace':
            return _Found('namespace', path=value)
        if kind == 'external':
            return _Found('external', name=value)
        return _unknown('unresolved_import:' + value)

    def name(self, module, scope, name, hops, via):
        """Python's lookup order from ``scope``: local, enclosing functions, module, star imports, builtins."""
        self.consulted.add(module.path)
        chain, current = [], scope
        if name in scope.globals:
            chain = [module.scope]
        else:
            if name not in scope.nonlocals:
                chain.append(scope)
            current = scope.parent
            while current is not None:
                if current.kind != 'class':
                    chain.append(current)
                current = current.parent
        for holder in chain:
            found = holder.bindings.get(name)
            if found:
                step = {'module': 'module', 'class': 'class'}.get(holder.kind, 'local')
                via.append({'step': step, 'path': module.path, 'line': found[0].line, 'column': found[0].column})
                return self.bindings(module, found, hops, via)
            if holder is module.scope and holder.stars:
                starred = self.stars(module, name, hops, via)
                if starred:
                    return starred
        if name in _BUILTINS:
            return [_Found('builtin', name=name)]
        return [_unknown('unbound')]

    def stars(self, module, name, hops, via):
        """``name`` through the module's star imports; the last one that binds it wins."""
        undecided = False
        for info in reversed(module.scope.stars):
            target = self.module_of(self.corpus.resolve(module.path, info.module, info.level))
            if target.kind != 'module':
                undecided = True  # an external or unresolvable star may bind anything
                continue
            star = self.corpus.module(target.path)
            if star is None:
                undecided = True
                continue
            self.consulted.add(star.path)
            exported = star.exported(name)
            if exported is None:
                undecided = True
            if not exported:
                continue
            mark = len(via)
            via.append({'step': 'star', 'path': module.path, 'line': info.line, 'column': info.column})
            result = self.member(target, name, hops + 1, via)
            if any(item.kind in ('binding', 'module', 'namespace', 'external') for item in result):
                return result
            del via[mark:]
            undecided = undecided or any(item.kind == 'unknown' and item.name != 'missing_member' for item in result)
        return [_unknown('star_import')] if undecided else []

    def bindings(self, module, found, hops, via):
        results = []
        for binding in found[:MAX_DEFINITIONS]:
            if binding.kind == 'import':
                results.extend(self.follow(module, binding, hops, via))
            else:
                results.append(_Found('binding', module=module, binding=binding))
        return results

    def follow(self, module, binding, hops, via):
        info = binding.info
        via.append({'step': 'import', 'path': module.path, 'line': binding.line, 'column': binding.column})
        if hops >= MAX_HOPS:
            self.corpus.counts['hops_limited'] += 1
            return [_unknown('hop_limit')]
        target = self.module_of(self.corpus.resolve(module.path, info.module, info.level))
        if info.name is None:
            return [target]
        return self.member(target, info.name, hops + 1, via)

    def member(self, owner, name, hops, via):
        """``owner.name`` for a module, namespace package, external module, class or instance."""
        if owner.kind == 'unknown':
            return [owner]  # keep the reason the owner itself could not be found
        if hops > MAX_HOPS:
            self.corpus.counts['hops_limited'] += 1
            return [_unknown('hop_limit')]
        if owner.kind == 'external':
            return [_Found('external', name=f'{owner.name}.{name}')]
        if owner.kind == 'builtin':
            return [_Found('builtin', name=f'{owner.name}.{name}')]
        if owner.kind == 'namespace':
            sub = self.corpus.submodule(owner.path, name)
            return [self.module_of(sub)] if sub else [_unknown('missing_member', owner.path)]
        if owner.kind == 'module':
            target = self.corpus.module(owner.path)
            if target is None:
                return [_unknown('not_parsed', owner.path)]
            self.consulted.add(target.path)
            found = target.scope.bindings.get(name)
            if found:
                via.append({'step': 'outline', 'path': target.path, 'line': found[0].line,
                            'column': found[0].column})
                return self.bindings(target, found, hops, via)
            if target.package:
                sub = self.corpus.submodule(target.folder, name)
                if sub:
                    return [self.module_of(sub)]
            if target.scope.stars:
                starred = self.stars(target, name, hops, via)
                if starred:
                    return starred
            if '__getattr__' in target.scope.bindings or target.status != 'parsed':
                return [_unknown('undecidable_member', target.path)]
            return [_unknown('missing_member', target.path)]
        if owner.kind == 'binding' and owner.binding.kind == 'class':
            return self.attribute(owner, name, hops, via, owner.instance)
        return [_unknown('untyped_receiver')]

    def attribute(self, owner, name, hops, via, instance):
        """Class member lookup: the class body, instance attributes, then in-root bases in order."""
        module, binding = owner.module, owner.binding
        scope = module.classes.get(id(binding.node))
        if scope is None:
            return [_unknown('untyped_receiver')]
        self.consulted.add(module.path)
        found = scope.bindings.get(name)
        if found:
            via.append({'step': 'member', 'path': module.path, 'line': found[0].line, 'column': found[0].column})
            return [item if item.kind != 'binding' else _Found('binding', module=item.module, binding=item.binding,
                                                               certain=owner.certain)
                    for item in self.bindings(module, found, hops, via)]
        if instance and name in scope.attrs:
            return [_Found('binding', module=module, binding=item, certain=owner.certain)
                    for item in scope.attrs[name][:MAX_DEFINITIONS]]
        certain = owner.certain
        for base in binding.node.bases:
            if hops >= MAX_HOPS:
                self.corpus.counts['hops_limited'] += 1
                return [_unknown('hop_limit')]
            for candidate in self.expression(module, binding.scope, base, hops + 1, via):
                if candidate.kind == 'binding' and candidate.binding.kind == 'class':
                    lifted = _Found('binding', module=candidate.module, binding=candidate.binding,
                                    certain=certain and candidate.certain, instance=instance)
                    result = self.attribute(lifted, name, hops + 1, via, instance)
                    if any(item.kind == 'binding' for item in result):
                        return result
                elif not (candidate.kind == 'builtin' and candidate.name == 'object'):
                    certain = False  # an untyped base might define it first
        return [_unknown('missing_member' if certain else 'undecidable_member', module.path)]

    def expression(self, module, scope, node, hops, via):
        if isinstance(node, ast.Name):
            results = []
            for item in self.name(module, scope, node.id, hops, via):
                item = self.receiver(item)
                binding = item.binding if item.kind == 'binding' else None
                if binding is None or binding.kind != 'variable' or not isinstance(binding.info, ast.Call):
                    results.append(item)
                    continue
                if hops >= MAX_HOPS:
                    self.corpus.counts['hops_limited'] += 1
                    results.append(item)
                    continue
                # `name = Class(...)`: an instance of Class, certain only if that is its single binding.
                single = len(binding.scope.bindings.get(binding.name, ())) == 1
                typed = [found for found in self.expression(item.module, binding.scope, binding.info, hops + 1, via)
                         if found.kind == 'binding' and found.instance]
                results.extend(_Found('binding', module=found.module, binding=found.binding,
                                      certain=found.certain and single, instance=True) for found in typed)
                if not typed:
                    results.append(item)
            return results
        if isinstance(node, ast.Attribute):
            results = []
            for owner in self.expression(module, scope, node.value, hops, via):
                results.extend(self.member(owner, node.attr, hops + 1, via))
            return results
        if isinstance(node, ast.Call):
            results = []
            for owner in self.expression(module, scope, node.func, hops, via):
                if owner.kind == 'binding' and owner.binding.kind == 'class' and not owner.instance:
                    results.append(_Found('binding', module=owner.module, binding=owner.binding,
                                          certain=owner.certain, instance=True))
                else:
                    results.append(_unknown('call_result'))
            return results
        if type(node) in _LITERALS:  # literals are builtin types, never project classes
            kind = _LITERALS[type(node)] or type(node.value).__name__
            return [_Found('builtin', name=kind)]
        return [_unknown('expression')]

    def receiver(self, found):
        """``self``/``cls`` parameters stand for the enclosing class (an instance unless classmethod)."""
        if found.kind != 'binding' or found.binding.kind != 'parameter':
            return found
        scope = found.binding.scope
        owner = scope.parent
        if (scope.kind != 'function' or scope.first != found.binding.name or owner is None or owner.kind != 'class'
                or owner.binding is None or not isinstance(scope.node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            return found
        decorators = {getattr(item, 'id', getattr(item, 'attr', None)) for item in scope.node.decorator_list}
        if 'staticmethod' in decorators:
            return found
        return _Found('binding', module=found.module, binding=owner.binding,
                      instance='classmethod' not in decorators)

    def ident(self, module, ident, via):
        """Resolve one identifier occurrence; returns (results, receiver_untyped)."""
        role = ident.role
        if role == 'attribute':
            results, untyped = [], False
            for owner in self.expression(module, ident.scope, ident.node.value, 0, via):
                if owner.kind == 'unknown' and owner.name in _UNTYPED:
                    untyped = True
                    continue
                for item in self.member(owner, ident.name, 1, via):
                    if item.kind == 'unknown' and item.name in _UNTYPED:
                        untyped = True
                    else:
                        results.append(item)
            return results, untyped
        if role in ('import', 'import_name'):
            return self.bindings(module, [ident.info], 0, via), False
        if role == 'module':
            return [self.module_of(self.corpus.resolve(module.path, ident.info.module, ident.info.level))], False
        if role == 'keyword':
            results = []
            for owner in self.expression(module, ident.scope, ident.info, 0, via):
                if owner.kind == 'binding' and owner.binding.kind == 'class':
                    results.extend(self.attribute(owner, ident.name, 1, via, True))
                elif owner.kind == 'binding' and owner.binding.kind in code_intelligence.FUNCTIONS:
                    scope = next((item for item in owner.module.scopes if item.node is owner.binding.node), None)
                    params = scope.bindings.get(ident.name, []) if scope else []
                    results.extend(_Found('binding', module=owner.module, binding=item)
                                   for item in params if item.kind == 'parameter')
                    if not params:
                        results.append(_unknown('missing_parameter'))
                else:
                    results.append(_unknown('untyped_callee'))
            return results, False
        if role == 'definition':
            return [_Found('binding', module=module, binding=ident.info)], False
        if role == 'parameter':
            return [_Found('binding', module=module, binding=ident.info)], False
        if role == 'global':
            return self.name(module, module.scope, ident.name, 0, via), False
        return self.name(module, ident.scope, ident.name, 0, via), False


# Public API -------------------------------------------------------------------------------------


def _validate_path(path):
    if not isinstance(path, str) or not path or len(path) > 512 or path.splitlines() != [path]:
        raise PolicyError('Name one relative Python file in the selected project.')
    text = path.strip()
    while text.startswith('./'):
        text = text[2:]
    if text.startswith('/') or '..' in text.split('/'):
        raise PolicyError('Paths stay relative to the selected project.')
    if not text.endswith(SUFFIXES):
        raise PolicyError('Python cross-references need a .py or .pyi file.')
    return text


def _validate_name(name):
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME:
        raise PolicyError(f'Use a Python name or dotted name of at most {MAX_NAME} characters.')
    parts = name.strip().split('.')
    if not all(part.isidentifier() for part in parts):
        raise PolicyError('Use a Python name or dotted name such as Class.method.')
    return parts


def _position(value, label):
    if value is not None and (type(value) is not int or value < 1):
        raise PolicyError(f'The {label} is a positive 1-based number.')
    return value


def _target(corpus, path, expected_hash):
    if path not in corpus.files:
        if _private(path) or not _walked(path):
            raise PolicyError('Private, hidden and excluded paths are outside cross-reference access.')
        raise PolicyError('That Python file is not in the selected project.')
    module = corpus.module(path)
    if module is None:
        reason = corpus.states.get(path, ('unreadable',))[0]
        raise PolicyError(f'That Python file could not be parsed here ({reason}).')
    if expected_hash is not None and expected_hash != module.hash:
        raise PolicyError('The file changed since that hash. Read it again, or repeat without expected_hash.')
    return module


def _location(found):
    module, binding = found.module, found.binding
    node = binding.node
    end = module.end_line(node) if isinstance(node, code_intelligence._DEFINITIONS) else binding.line
    qualname, shortened = code_intelligence.display_name(module.qualname(binding))
    row = {'path': module.path, 'line': binding.line, 'column': binding.column, 'end_line': end,
           'kind': binding.kind, 'qualname': qualname, 'file_hash': module.hash, 'basis': module.basis,
           'parse': module.status,
           **search_ranking.excerpt(module.line_text(binding.line), binding.column - 1, TEXT_CHARS)}
    if shortened:
        row['qualname_truncated'] = True
    return row


def _module_row(corpus, found):
    if found.kind == 'namespace':
        return {'path': found.path or '.', 'kind': 'namespace_package', 'line': None, 'column': None}
    module = corpus.module(found.path)
    row = {'path': found.path, 'kind': 'module', 'line': 1, 'column': 1}
    if module is not None:
        row.update(file_hash=module.hash, basis=module.basis, parse=module.status)
    else:
        row['skip_reason'] = corpus.states.get(found.path, ('unreadable',))[0]
    return row


def _identifier_at(module, line, column, name):
    """The identifier covering (line, column), or the first one named ``name`` on that line."""
    best = None
    for ident in module.idents:
        if ident.line != line:
            continue
        if column is not None and ident.column <= column <= ident.column + len(ident.name):
            if name is None or ident.name == name:
                if best is None or ident.column > best.column:  # the innermost at the cursor
                    best = ident
        elif column is None and ident.name == name and (best is None or ident.column < best.column):
            best = ident
    return best


def definition(ws, path, line=None, column=None, *, name=None, expected_hash=None, cancel=None):
    """Where the name at ``path:line:column`` (or ``name`` in ``path``) is defined.

    With ``line`` (and ``column``, 1-based characters like Read and symbol rows)
    the identifier there is resolved in its own scope. With ``name`` alone the
    dotted name is resolved from module level, and with ``name`` and ``line``
    from the scope enclosing that line.
    """
    path = _validate_path(path)
    _position(line, 'line')
    _position(column, 'column')
    parts = _validate_name(name) if name is not None else None
    if line is None and (parts is None or column is not None):
        raise PolicyError('Give a line and column, a name, or a name with a line.')
    if line is not None and column is None and parts is None:
        raise PolicyError('Give the column with the line, or name the identifier on that line.')
    corpus = _Corpus(ws, cancel)
    module = _target(corpus, path, expected_hash)
    resolver = _Resolver(corpus)
    via, query = [], {'path': path, 'line': line, 'column': column, 'name': name}
    ident = None
    if column is not None:
        ident = _identifier_at(module, line, column, parts[-1] if parts and len(parts) == 1 else None)
        if ident is None:
            return _definition_result(corpus, resolver, module, query, [], 'not_an_identifier', via)
    elif line is not None and len(parts) == 1:
        ident = _identifier_at(module, line, None, parts[0])  # else: look the name up in that line's scope
    if ident is not None:
        query.update(identifier=ident.name, role=ident.role)
        results, untyped = resolver.ident(module, ident, via)
        if untyped and not results:
            results = [_unknown('untyped_receiver')]
    else:
        scope = module.scope_at(line) if line is not None else module.scope
        results = resolver.expression(module, scope, ast.Name(id=parts[0]), 0, via)
        for part in parts[1:]:
            results = [item for owner in results for item in resolver.member(owner, part, 1, via)]
    return _definition_result(corpus, resolver, module, query, results, None, via)


def _definition_result(corpus, resolver, module, query, results, status, via):
    seen, rows, kinds = set(), [], Counter(item.kind for item in results)
    certain = True
    for found in results:
        if found.kind not in ('binding', 'module', 'namespace') or found.key() in seen:
            continue
        seen.add(found.key())
        rows.append(_location(found) if found.kind == 'binding' else _module_row(corpus, found))
        certain = certain and found.certain
        if found.kind == 'binding':
            resolver.consulted.add(found.module.path)
    rows.sort(key=lambda row: (row['path'], row['line'] or 0, row['column'] or 0))
    if status is None:
        status = ('resolved' if rows else 'external' if kinds['external'] else 'builtin' if kinds['builtin']
                  else 'unresolved')
    result = {'query': query, 'status': status, 'untrusted_definitions': rows[:MAX_DEFINITIONS]}
    if rows:
        result['grade'] = 'resolved' if certain else 'attribute_candidate'
    externals = sorted({item.name for item in results if item.kind == 'external'})
    if externals:
        result['external'] = externals[:MAX_DEFINITIONS]
    if kinds['builtin']:
        result['builtin'] = sorted({item.name for item in results if item.kind == 'builtin'})[:MAX_DEFINITIONS]
    reasons = sorted({item.name for item in results if item.kind == 'unknown'})
    if reasons and not rows:
        result['reasons'] = reasons[:MAX_DEFINITIONS]
    result['via'] = via[:2 * MAX_HOPS]
    consulted = sorted(resolver.consulted | {module.path})
    result['files'] = {path: {'file_hash': corpus.states[path][1], 'basis': corpus.states[path][2],
                              'parse': corpus.states[path][0]} for path in consulted if path in corpus.states}
    coverage = corpus.coverage()
    undecided = any(item.kind == 'unknown' and item.name in ('not_parsed', 'undecidable_member', 'star_import',
                                                              'hop_limit') for item in results)
    coverage['complete'] = (corpus.scan_complete() and corpus.consulted_complete(consulted)
                            and not coverage['hops_limited'] and not undecided and len(rows) <= MAX_DEFINITIONS)
    result['coverage'] = coverage
    result['note'] = NOTE
    return result


def imports(ws, path, *, expected_hash=None, cancel=None):
    """The per-file import table, each entry resolved only to files inside the root."""
    path = _validate_path(path)
    corpus = _Corpus(ws, cancel)
    module = _target(corpus, path, expected_hash)
    rows = []
    for node, alias, scope, info in module.imports:
        plain = isinstance(node, ast.Import)
        if info.name == '*':
            binding = None
        elif plain:
            binding = alias.asname or alias.name.split('.')[0]  # `import a.b` binds `a`
        else:
            binding = alias.asname or alias.name
        # `import a.b` without `as` binds package `a` but also needs `a.b` to exist.
        imported = alias.name if plain else info.module
        target = corpus.resolve(module.path, imported, info.level)
        status = 'resolved' if target[0] in ('module', 'namespace') else target[0]
        row = {'line': info.line, 'column': info.column, 'statement': 'import' if plain else 'from',
               'module': imported, 'level': info.level, 'name': info.name, 'binding': binding,
               'scope': scope.kind, 'status': status}
        if status == 'resolved':
            row['target'] = target[1] or '.'
            row['target_kind'] = 'namespace_package' if target[0] == 'namespace' else 'module'
        elif status == 'unresolved':
            row['reason'] = target[1]
        rows.append(row)
    rows.sort(key=lambda row: (row['line'], row['column']))
    coverage = corpus.coverage()
    coverage['complete'] = corpus.scan_complete() and module.status == 'parsed'
    return {'path': path, 'file_hash': module.hash, 'basis': module.basis, 'parse': module.status,
            'imports': rows, 'coverage': coverage, 'note': NOTE}


def diagnostics(ws, path, *, expected_hash=None, cancel=None):
    """Syntax errors and imports that should resolve inside the root but do not."""
    path = _validate_path(path)
    corpus = _Corpus(ws, cancel)
    module = _target(corpus, path, expected_hash)
    resolver = _Resolver(corpus)
    found, unchecked, external = [], 0, 0
    if module.issue == 'syntax_error' and module.error:
        found.append({'code': 'syntax_error', 'severity': 'error', 'line': module.error.get('line'),
                      'column': None, 'message': module.error.get('message') or 'invalid syntax'})
    elif module.issue == 'too_complex':
        found.append({'code': 'too_complex', 'severity': 'warning', 'line': None, 'column': None,
                      'message': 'The parser could not analyse this file within its nesting limits.'})
    for node, alias, scope, info in module.imports:
        _stop(cancel)
        target = corpus.resolve(module.path, info.module, info.level)
        shown = ('.' * info.level) + (info.module or '')
        if target[0] == 'external':
            external += 1
            continue
        if target[0] == 'unresolved':
            reason = 'outside the selected project' if target[1] == 'beyond_root' else 'not found in the project'
            found.append({'code': 'unresolved_import', 'severity': 'error', 'line': info.line,
                          'column': info.column, 'module': shown,
                          'message': f'Import {shown!r} is {reason}.'})
            continue
        if isinstance(node, ast.Import) and alias.asname is None and '.' in alias.name:
            full = corpus.resolve(module.path, alias.name, 0)
            if full[0] == 'unresolved':
                found.append({'code': 'unresolved_import', 'severity': 'error', 'line': info.line,
                              'column': info.column, 'module': alias.name,
                              'message': f'Import {alias.name!r} is not found in the project.'})
            continue
        if info.name in (None, '*'):
            continue
        owner = resolver.module_of(target)
        results = resolver.member(owner, info.name, 1, [])
        if any(item.kind in ('binding', 'module', 'namespace', 'external') for item in results):
            continue
        reasons = {item.name for item in results if item.kind == 'unknown'}
        if reasons <= {'missing_member'}:
            found.append({'code': 'unresolved_import_name', 'severity': 'error', 'line': info.line,
                          'column': info.column, 'module': shown, 'name': info.name,
                          'message': f'{info.name!r} is not defined in {shown!r}.'})
        else:
            unchecked += 1
    found.sort(key=lambda row: (row['line'] or 0, row['column'] or 0, row['code']))
    coverage = corpus.coverage()
    coverage.update({'imports': len(module.imports), 'external_imports': external, 'imports_unchecked': unchecked,
                     'checked_through_line': module.covered, 'total_lines': module.places.total,
                     'diagnostic_limit': MAX_DIAGNOSTICS, 'diagnostics_truncated': len(found) > MAX_DIAGNOSTICS})
    coverage['complete'] = (corpus.scan_complete() and module.status == 'parsed' and not unchecked
                            and len(found) <= MAX_DIAGNOSTICS and not coverage['hops_limited'])
    return {'path': path, 'file_hash': module.hash, 'basis': module.basis, 'parse': module.status,
            'diagnostics': found[:MAX_DIAGNOSTICS], 'coverage': coverage, 'note': NOTE}


def _symbol(symbol):
    """(path, qualname or None, line, column, expected hash) from a definition row, dict or 'path:qualname'."""
    if isinstance(symbol, str):
        path, sep, qualname = symbol.rpartition(':')
        if not sep or not qualname:
            raise PolicyError("Name the definition as 'path.py:Qualified.name' or pass a definition row.")
        symbol = {'path': path, 'qualname': qualname}
    if not isinstance(symbol, dict) or not isinstance(symbol.get('path'), str):
        raise PolicyError('Name the definition by path and qualname, or path, line and column.')
    qualname, line, column = symbol.get('qualname'), symbol.get('line'), symbol.get('column')
    if qualname is not None:
        if not isinstance(qualname, str) or not 1 <= len(qualname) <= code_intelligence.IDENTIFIER_CHARS or not all(
                part.isidentifier() or part == '<locals>' for part in qualname.split('.')):
            raise PolicyError('A qualname is a dotted Python name such as Class.method.')
    elif line is None or column is None:
        raise PolicyError('Name the definition by path and qualname, or path, line and column.')
    _position(line, 'line')
    _position(column, 'column')
    expected = symbol.get('file_hash')
    if expected is not None and not isinstance(expected, str):
        raise PolicyError('A file hash is the hexadecimal SHA-256 returned with a result.')
    return _validate_path(symbol['path']), qualname, line, column, expected


def references(ws, symbol, *, scope=None, cursor=None, limit=50, cancel=None):
    """Graded occurrences of one in-root definition across the project, in pages.

    ``symbol`` is a definition row (path, line, column, optional file_hash), a
    dict with path and qualname, or the string ``'path.py:Qualified.name'``.
    Rows order by grade, path, line and column. A cursor binds the target,
    the host scope and every file state that shaped the ranking.
    """
    path, qualname, line, column, expected = _symbol(symbol)
    if type(limit) is not int or not 1 <= limit <= REFERENCE_LIMIT:
        raise PolicyError(f'Choose a reference limit from 1 to {REFERENCE_LIMIT}.')
    scope_id = hashlib.sha256(json.dumps(scope, default=str).encode()).hexdigest()[:32]
    query = hashlib.sha256(json.dumps([VERSION, path, qualname, line, column]).encode()).hexdigest()[:32]
    after = _open(cursor, query, scope_id)
    corpus = _Corpus(ws, cancel)
    module = _target(corpus, path, expected)
    outline = code_intelligence.outline(module.text)
    if qualname is not None:
        targets = [s for s in outline.symbols if code_intelligence.qualname(outline.symbols, s) == qualname]
    else:
        targets = [s for s in outline.symbols if (s.line, s.column) == (line, column)]
    if not targets:
        raise PolicyError('No module- or class-level definition matches that symbol in the current file.')
    name = targets[0].name
    wanted = {(path, s.line, s.column) for s in targets}
    resolver = _Resolver(corpus)
    rows, counts, state = [], Counter(), hashlib.sha256()
    needle, pattern = name.encode('utf-8'), re.compile(r'(?<!\w)' + re.escape(name) + r'(?!\w)')
    for candidate in corpus.paths:
        _stop(cancel)
        loaded, reason = (corpus.text(candidate) if candidate != path else
                          ((module.text, module.hash, module.basis, len(module.text.encode())), None))
        state.update(json.dumps([candidate, reason, loaded[1] if loaded else None]).encode())
        if loaded is None:
            continue
        counts['searched'] += 1
        if needle not in loaded[0].encode('utf-8'):
            counts['prefiltered'] += 1
            continue
        target = module if candidate == path else corpus.module(candidate, loaded)
        rows.extend(_occurrences(corpus, resolver, candidate, loaded, target, name, wanted, pattern, counts))
        if len(rows) > MAX_REFERENCES:
            counts['truncated'] += 1
            break
    rows.sort(key=lambda row: (GRADES.index(row['grade']), row['path'], row['line'], row['column']))
    truncated = len(rows) > MAX_REFERENCES or counts['truncated']
    rows = rows[:MAX_REFERENCES]
    token = hashlib.sha256(state.digest() + repr((VERSION, corpus.walk['truncated'], corpus.walk['errors'],
                                                  sorted(wanted))).encode()).hexdigest()[:32]
    start = 0
    if after is not None:
        if after['w'] != token or after['p'] + 1 >= len(rows):
            raise CursorError(STALE)
        start = after['p'] + 1
    page = rows[start:start + limit]
    more = start + len(page) < len(rows)
    coverage = corpus.coverage()
    graded = Counter(row['grade'] for row in rows)
    unparsed = sum(1 for key in ('partial', 'failed', 'deferred') if coverage[key])
    coverage.update({
        'searched': counts['searched'], 'prefiltered': counts['prefiltered'],
        'excluded_other_bindings': counts['excluded'], 'grades': {grade: graded[grade] for grade in GRADES},
        'reference_limit': MAX_REFERENCES, 'references_truncated': bool(truncated),
        'match_limit': limit, 'matches_returned': len(page), 'matches_truncated': more, 'resumed': after is not None,
    })
    coverage['scan_complete'] = corpus.scan_complete() and not coverage['skipped']
    coverage['complete'] = (coverage['scan_complete'] and not unparsed and not coverage['hops_limited']
                            and not truncated and not more)
    target_row = {'path': path, 'name': name, 'qualname': code_intelligence.display_name(
        code_intelligence.qualname(outline.symbols, targets[0]))[0], 'line': targets[0].line,
        'column': targets[0].column, 'kind': targets[0].kind, 'file_hash': module.hash, 'basis': module.basis}
    next_cursor = _seal(query, scope_id, token, start + len(page) - 1) if more else None
    return {'target': target_row, 'untrusted_references': page, 'coverage': coverage, 'cursor': next_cursor,
            'note': NOTE}


def _occurrences(corpus, resolver, path, loaded, module, name, wanted, pattern, counts):
    text, file_hash, basis, _size = loaded
    rows, places = [], set()
    if module is not None:
        for ident in module.idents:
            if ident.name != name or ident.role == 'module':
                if ident.name == name:
                    places.add((ident.line, ident.column))
                continue
            places.add((ident.line, ident.column))
            if ident.role == 'parameter':
                counts['excluded'] += 1
                continue
            via = []
            results, untyped = resolver.ident(module, ident, via)
            hits = [item for item in results if item.kind == 'binding'
                    and (item.module.path, item.binding.line, item.binding.column) in wanted]
            if hits:
                grade = 'resolved' if all(item.certain for item in hits) else 'attribute_candidate'
                reason = None
            elif untyped:
                grade, reason = 'attribute_candidate', 'untyped_receiver'
            elif results and all(item.kind == 'unknown' and item.name not in ('missing_member', 'missing_parameter')
                                 for item in results):
                grade, reason = 'textual', sorted({item.name for item in results})[0]
            else:
                counts['excluded'] += 1
                continue
            rows.append(_reference(module.lines, path, ident.line, ident.column, grade, ident.role, reason,
                                   file_hash, basis))
    lines = text.splitlines()
    covered = module.covered if module is not None else 0
    for number, line in enumerate(lines, 1):
        if name not in line:
            continue
        for found in pattern.finditer(line):
            column = found.start() + 1
            if (number, column) in places:
                continue
            reason = 'text' if number <= covered else ('unparsed' if module is not None else
                                                       corpus.states.get(path, ('deferred',))[0])
            rows.append(_reference(lines, path, number, column, 'textual', 'text', reason, file_hash, basis))
    return rows


def _reference(lines, path, line, column, grade, role, reason, file_hash, basis):
    row = {'path': path, 'line': line, 'column': column, 'grade': grade, 'role': role,
           **search_ranking.excerpt(lines[line - 1] if line <= len(lines) else '', column - 1, TEXT_CHARS),
           'file_hash': file_hash, 'basis': basis}
    if reason:
        row['reason'] = reason
    return row


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _mac(body):
    return hmac.new(_KEY, b'pebble-xref-cursor\0' + body, hashlib.sha256).digest()


def _seal(query, scope_id, token, position):
    body = json.dumps({'v': 1, 'q': query, 's': scope_id, 'w': token, 'p': position},
                      separators=(',', ':'), sort_keys=True).encode('utf-8')
    return f'{_b64(body)}.{_b64(_mac(body))}'


def _open(cursor, query, scope_id):
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not 0 < len(cursor) <= MAX_CURSOR:
        raise CursorError('Reference cursors are the opaque strings returned with a previous page.')
    try:
        body, tag = (base64.urlsafe_b64decode(part.encode('ascii') + b'=' * (-len(part) % 4))
                     for part in cursor.split('.'))
        if not hmac.compare_digest(tag, _mac(body)):
            raise ValueError
        data = json.loads(body)
    except ValueError:
        raise CursorError('This reference cursor was altered, or was issued before a restart. '
                          'Repeat the request without a cursor.') from None
    if not (isinstance(data, dict) and data.get('v') == 1 and type(data.get('p')) is int
            and 0 <= data['p'] < MAX_REFERENCES and isinstance(data.get('w'), str)):
        raise CursorError('This reference cursor is malformed.')
    if data.get('q') != query:
        raise CursorError('This cursor belongs to a different symbol.')
    if data.get('s') != scope_id:
        raise CursorError('This cursor belongs to another conversation or run.')
    return data
