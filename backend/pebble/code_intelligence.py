"""Python definition outlines parsed from untrusted source text.

The standard-library ``ast`` parser reads exactly the text the caller passes in.
Nothing is imported, compiled to bytecode or executed, and no other file is
consulted. Reported definitions are functions, methods, classes, type aliases
and plain-name assignments in module or class bodies. Each keeps its own name
and the index of its enclosing definition, never an expanded qualified name, so
memory stays linear in the file however deep or long the names are.
``__qualname__``-style names (``Outer.method``, ``outer.<locals>.inner``) are
rebuilt from those links on demand: in full for matching and within
IDENTIFIER_CHARS for display, where an ellipsis marks every cut. Line numbers
follow ``str.splitlines`` so they agree with ``Read`` offsets even where form
feeds or Unicode line separators make the Python tokenizer count lines
differently. A function or class is located at its declaration: the line and
1-based character column of its ``def``, ``async`` or ``class`` keyword after
any decorators (``start_line`` is the first decorator). The parser reports no
position for the name itself, so none is guessed. Assignments and ``type``
aliases are located at the target name. This is not a language server: there is no import resolution,
reference search, type inference or support for other languages. A syntax error
keeps only the definitions of a recovered parseable prefix and says so.
``syntax_tree`` exposes that same parse and position mapping so ``python_xref``
locates names exactly where outlines do; ``ts_symbols`` builds outlines of the
same shape for other languages, ranked with KIND_ORDER below.
"""

import ast
import re
import sys
import threading
import warnings
from bisect import bisect_right
from collections import OrderedDict, namedtuple

from .policy import PolicyError
from .search_ranking import MAX_QUERY, MAX_TERMS, fold, subwords

VERSION = 'python-ast-2'
SUFFIXES = ('.py', '.pyi')
MAX_SYMBOLS = 2_000  # definitions kept per file, first in source order
DOC_CHARS = 120
MESSAGE_CHARS = 160
IDENTIFIER_CHARS = 1_024  # displayed name or qualified name
IDENTIFIER_FLOOR = 96  # display bound when a whole row must shrink to fit
CACHE_ENTRIES = 4_096
CACHE_SYMBOLS = 200_000
CACHE_BYTES = 64 * 1024 * 1024  # conservative retained bytes across all cached outlines
SYMBOL_BYTES = 320  # per-definition tuple and integer overhead, beyond its strings
TIERS = ('exact', 'equivalent', 'prefix', 'substring', 'words')
KIND_ORDER = {'class': 0, 'function': 0, 'async_function': 0, 'method': 0, 'async_method': 0,
              'type_alias': 1, 'variable': 1,
              # tree-sitter outlines (ts_symbols): declarations first, then bindings and impl blocks
              'interface': 0, 'struct': 0, 'trait': 0, 'enum': 0, 'module': 0, 'type': 0, 'macro': 0,
              'constant': 1, 'property': 1, 'impl': 1}
FUNCTIONS = frozenset({'function', 'async_function', 'method', 'async_method'})
ELLIPSIS = '\N{HORIZONTAL ELLIPSIS}'  # never part of a Python identifier

# ``parent`` indexes the enclosing definition in the same outline (-1 at module level).
Symbol = namedtuple('Symbol', 'name kind start_line line end_line column depth doc uncertain parent')
Outline = namedtuple('Outline', 'status issue error parsed_through total_lines symbols')
Probe = namedtuple('Probe', 'text name folded encoded words qualifiers exact_qualifiers')
# ``tree`` is the whole module, a recovered prefix ending before parser line ``cut``, or None.
Tree = namedtuple('Tree', 'status issue error tree cut places')

_AST_BREAK = re.compile(r'\r\n|\r|\n')  # what the tokenizer counts as a line end
_SPLIT_BREAK = re.compile('\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85'
                          '\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]')  # str.splitlines
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_ALIAS = getattr(ast, 'TypeAlias', None)


def fingerprint():
    """Parser settings that change an outline; cached outlines and cursors bind it."""
    return repr((VERSION, MAX_SYMBOLS, DOC_CHARS, MESSAGE_CHARS))


def syntax_tree(text):
    """Parse ``text`` exactly as ``outline`` does: the whole module or a recovered prefix.

    ``places`` maps parser positions to the splitlines lines and 1-based
    character columns every result here uses. Nothing is compiled or executed.
    """
    shift = 1 if text.startswith('\N{ZERO WIDTH NO-BREAK SPACE}') else 0  # byte order mark
    source = text[shift:]
    places = _Places(text, shift)
    cut = error = issue = None
    try:
        tree = _parse(source)
    except SyntaxError as exc:
        issue, error = 'syntax_error', places.error(exc.lineno, exc.msg)
        tree, cut = _recover(source, places, exc.lineno)
    except ValueError as exc:  # NUL bytes on interpreters that report them this way
        issue, error, tree = 'syntax_error', {'line': None, 'message': _message(exc)}, None
    except (RecursionError, MemoryError):
        return Tree('failed', 'too_complex', None, None, None, places)
    if tree is None:
        return Tree('failed', issue, error, None, None, places)
    return Tree('parsed' if cut is None else 'partial', issue, error, tree, cut, places)


def outline(text):
    """Definitions in ``text`` and how much of it the parser covered."""
    parsed = syntax_tree(text)
    places, status, issue, cut = parsed.places, parsed.status, parsed.issue, parsed.cut
    if parsed.tree is None:
        return Outline('failed', issue, parsed.error, 0, places.total, ())
    symbols = _collect(parsed.tree, places, partial=cut is not None)
    if len(symbols) > MAX_SYMBOLS:  # parents precede children, so every kept link stays valid
        symbols, status, issue = symbols[:MAX_SYMBOLS], 'partial', issue or 'symbol_limit'
    through = places.total if cut is None else places.through(cut)
    return Outline(status, issue, parsed.error, through, places.total, tuple(symbols))


def qualname_parts(symbols, symbol):
    """Complete file-local qualified name parts, outermost first, rebuilt from parent links."""
    parts, index = [symbol.name], symbol.parent
    while index >= 0:
        owner = symbols[index]
        if owner.kind in FUNCTIONS:
            parts.append('<locals>')
        parts.append(owner.name)
        index = owner.parent
    parts.reverse()
    return parts


def qualname(symbols, symbol):
    """The complete qualified name. Unbounded: show it through ``display_qualname``."""
    return '.'.join(qualname_parts(symbols, symbol))


def clip(text, limit):
    """At most ``limit`` characters keeping both ends; an ellipsis marks the cut."""
    if len(text) <= limit:
        return text
    if limit < 1:
        return ''
    head = (limit - 1) // 2
    return text[:head] + ELLIPSIS + text[len(text) - (limit - 1 - head):]


def display_name(name, limit=None):
    """(text, shortened) for one identifier within ``limit`` characters."""
    limit = IDENTIFIER_CHARS if limit is None else limit
    return clip(name, limit), len(name) > limit


def display_qualname(symbols, symbol, limit=None):
    """(text, shortened): the qualified name within ``limit`` characters.

    A shortened name keeps the outermost scope parts and the definition's own
    name, and every cut is an ellipsis, which no identifier contains, so it can
    never pass for a real qualified name. Path, line and column stay the identity.
    """
    limit = IDENTIFIER_CHARS if limit is None else limit
    parts = qualname_parts(symbols, symbol)
    if sum(map(len, parts)) + len(parts) - 1 <= limit:
        return '.'.join(parts), False
    scope = parts[:-1]
    terminal = clip(parts[-1], limit // 2 if scope else limit)
    room = limit - len(terminal) - 1
    piece = max(1, min(64, room // 3))
    kept = []
    for index, part in enumerate(scope):
        trial = kept + [clip(part, piece)]
        if len('.'.join(trial + ([ELLIPSIS] if index + 1 < len(scope) else []))) > room:
            break
        kept = trial
    if len(kept) < len(scope):
        kept.append(ELLIPSIS)
    return '.'.join(kept + [terminal]), True


def probe(query, outline=False):
    """Validate a definition query. None (outline mode) lists everything and needs a path."""
    if query is None or query == '':
        if outline:
            return None
        raise PolicyError('Name a symbol, or give a path with an empty query to list its definitions.')
    if not isinstance(query, str) or len(query) > MAX_QUERY:
        raise PolicyError(f'Symbol queries are names of at most {MAX_QUERY} characters.')
    if query.splitlines() != [query]:
        raise PolicyError('Symbol queries are one line; remove line breaks.')
    try:
        query.encode('utf-8')
    except UnicodeEncodeError:
        raise PolicyError('Symbol queries must be valid Unicode text.') from None
    text = ' '.join(query.split())
    if not text:
        raise PolicyError('Name a symbol, or give a path with an empty query to list its definitions.')
    if ' ' in text:
        qualifiers, name = (), text
    else:
        parts = text.split('.')
        if not all(parts):
            raise PolicyError('Use a name or a dotted qualified name such as Class.method.')
        qualifiers, name = tuple(part for part in parts[:-1] if part != '<locals>'), parts[-1]
    words = tuple(subwords(name))
    if len(set(words)) > MAX_TERMS:
        raise PolicyError(f'Use at most {MAX_TERMS} distinct words in a symbol query.')
    folded = fold(name)
    return Probe(text, name, folded, folded.encode('utf-8'), words,
                 tuple(fold(part) for part in qualifiers), qualifiers)


def possible(probe, folded):
    """Whether ``bytes.lower()`` source could define a match. False is exact, not a guess."""
    if probe is None or probe.encoded in folded:
        return True
    return bool(probe.words) and all(word.encode('utf-8') in folded for word in probe.words)


def match(symbol, probe, symbols=()):
    """Index into TIERS (lower is better) or None; outline mode matches everything as 0.

    Qualifiers compare against the full enclosing names from ``symbols`` (the
    outline holding ``symbol``), innermost first; lengths are compared before
    folding, so long names cost nothing unless they could be equal.
    """
    if probe is None:
        return 0
    exact_scope = True
    if probe.qualifiers:
        enclosing, index = [], symbol.parent
        while index >= 0 and len(enclosing) < len(probe.qualifiers):
            enclosing.append(symbols[index].name)
            index = symbols[index].parent
        if len(enclosing) < len(probe.qualifiers):
            return None
        for name, folded, exact in zip(enclosing, reversed(probe.qualifiers), reversed(probe.exact_qualifiers)):
            if len(name) != len(exact) or fold(name) != folded:
                return None
            exact_scope = exact_scope and name == exact
    name = symbol.name
    folded = fold(name)
    if name == probe.name and exact_scope:
        return 0
    if folded == probe.folded:
        return 1
    joined = ''.join(probe.words)
    if probe.words and folded.replace('_', '') == joined and subwords(name) == list(probe.words):
        return 1
    if folded.startswith(probe.folded):
        return 2
    if probe.folded in folded:
        return 3
    if probe.words and all(word in folded for word in probe.words) and set(probe.words) <= set(subwords(name)):
        return 4
    return None


def cost(key, outline):
    """Conservative retained bytes of one cache entry: key, outline, definitions and strings."""
    total = 1_024 + sum(sys.getsizeof(part) for part in key)
    if outline.error:
        total += 256 + sys.getsizeof(outline.error.get('message') or '')
    for symbol in outline.symbols:
        total += SYMBOL_BYTES + sys.getsizeof(symbol.name)
        if symbol.doc:
            total += sys.getsizeof(symbol.doc)
    return total


class OutlineCache:
    """Bounded LRU of outlines keyed by (root namespace, content hash, parser fingerprint).

    Entries, definitions and estimated retained bytes are all bounded; an outline
    larger than a whole bound is not cached at all. Keys are content addressed,
    so a hit always describes the exact text whose hash the caller holds.
    Nothing is persisted; a restart parses again.
    """

    def __init__(self, entries=None, symbols=None, byte_limit=None):
        self.entries = CACHE_ENTRIES if entries is None else entries
        self.symbols = CACHE_SYMBOLS if symbols is None else symbols
        self.byte_limit = CACHE_BYTES if byte_limit is None else byte_limit
        self._items = OrderedDict()  # key -> (outline, definitions cost, byte cost)
        self._count = self._bytes = 0
        self._lock = threading.Lock()

    def __len__(self):
        with self._lock:
            return len(self._items)

    def usage(self):
        with self._lock:
            return {'entries': len(self._items), 'symbols': self._count, 'bytes': self._bytes}

    def get(self, key):
        with self._lock:
            found = self._items.get(key)
            if found is None:
                return None
            self._items.move_to_end(key)
            return found[0]

    def put(self, key, value):
        """Cache ``value`` unless it alone exceeds a bound; True when it was kept."""
        count, size = len(value.symbols) + 1, cost(key, value)
        with self._lock:
            self._discard(key)
            if count > self.symbols or size > self.byte_limit or self.entries < 1:
                return False
            self._items[key] = (value, count, size)
            self._count += count
            self._bytes += size
            while (len(self._items) > self.entries or self._count > self.symbols
                   or self._bytes > self.byte_limit):
                self._discard(next(iter(self._items)))
            return True

    def drop(self, namespace):
        """Forget every outline of one root namespace."""
        with self._lock:
            for key in [key for key in self._items if key[0] == namespace]:
                self._discard(key)

    def namespaces(self):
        with self._lock:
            return {key[0] for key in self._items}

    def _discard(self, key):
        found = self._items.pop(key, None)
        if found is not None:
            self._count -= found[1]
            self._bytes -= found[2]


def _parse(source):
    # Compile-time warnings (invalid escapes and the like) describe untrusted text, not this process.
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        return ast.parse(source, filename='<untrusted>')


def _message(value):
    return ' '.join(str(value).split())[:MESSAGE_CHARS]


def _recover(source, places, lineno):
    """Parse the longest cheaply recoverable prefix before a syntax error, if any."""
    starts = places.starts
    if not isinstance(lineno, int) or lineno < 2:
        return None, None
    lineno = min(lineno, len(starts))
    cuts = [lineno]
    top = _statement_start(source, starts, lineno)
    if top is not None and top < lineno:
        cuts.append(top)
    for cut in cuts:
        try:
            return _parse(source[:starts[cut - 1]]), cut
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            continue
    return None, None


def _statement_start(source, starts, before):
    """The last column-0 statement line before parser line ``before``, moved up over decorators."""
    def text(number):
        return source[starts[number - 1]:starts[number] if number < len(starts) else len(source)]

    found = next((number for number in range(before - 1, 0, -1)
                  if text(number)[:1] not in ('', ' ', '\t', '\x0c', '\r', '\n', '#')), None)
    if found is None:
        return None
    number = found - 1
    while number >= 1:
        line = text(number)
        if not line.strip() or line.lstrip().startswith('#'):
            number -= 1
        elif line.startswith('@'):
            found, number = number, number - 1
        else:
            break
    return found


def _blocks(node):
    """Statement lists nested in a compound statement, still in the enclosing scope."""
    for field in ('body', 'orelse', 'finalbody'):
        block = getattr(node, field, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            yield block
    for item in getattr(node, 'handlers', None) or ():
        yield item.body
    for item in getattr(node, 'cases', None) or ():
        yield item.body


def _targets(node):
    if isinstance(node, ast.Assign):
        pending = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        pending = [node.target]
    else:
        pending = [node.name]
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Name):
            yield target
        elif isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)


def _doc(node):
    text = ast.get_docstring(node, clean=True)
    if not text:
        return None
    first = next((line for line in text.splitlines() if line.strip()), '')
    return ' '.join(first.split())[:DOC_CHARS] or None


def _collect(tree, places, partial):
    found = []  # symbols whose ``parent`` is, for now, the parent's position in this list
    # In a recovered prefix, spans reaching its last statement may continue past the cut.
    last = tree.body[-1].end_lineno if tree.body else 0
    pending = [(tree.body, -1, 0, 'module')]
    while pending:
        body, parent, depth, scope = pending.pop()
        for node in body:
            if isinstance(node, _DEFINITIONS):
                is_class = isinstance(node, ast.ClassDef)
                if is_class:
                    kind = 'class'
                else:
                    kind = 'method' if scope == 'class' else 'function'
                    if isinstance(node, ast.AsyncFunctionDef):
                        kind = 'async_' + kind
                pending.append((node.body, len(found), depth + 1, 'class' if is_class else 'function'))
                found.append(places.symbol(node, node, node.name, kind, depth,
                                           partial and node.end_lineno >= last, _doc(node), parent))
            elif scope != 'function' and (isinstance(node, (ast.Assign, ast.AnnAssign))
                                          or _ALIAS is not None and isinstance(node, _ALIAS)):
                kind = 'variable' if isinstance(node, (ast.Assign, ast.AnnAssign)) else 'type_alias'
                for target in _targets(node):
                    found.append(places.symbol(node, target, target.id, kind, depth,
                                               partial and node.end_lineno >= last, None, parent))
            else:
                pending.extend((block, parent, depth, scope) for block in _blocks(node))
    # Source order; a parent always starts before anything in its body, so links point backwards.
    order = sorted(range(len(found)), key=lambda position: (found[position].line, found[position].column, position))
    moved = {old: new for new, old in enumerate(order)}
    return [found[old]._replace(parent=moved[found[old].parent] if found[old].parent >= 0 else -1)
            for old in order]


class _Places:
    """Map parser positions (tokenizer lines, UTF-8 byte columns) to splitlines numbering."""

    def __init__(self, text, shift):
        self.text, self.shift, self.source = text, shift, text[shift:]
        self.starts = [0] + [found.end() for found in _AST_BREAK.finditer(self.source)]
        self.lines = [0] + [found.end() for found in _SPLIT_BREAK.finditer(text)]
        self.total = len(text.splitlines())

    def offset(self, line, column=0):
        """Character offset in ``text`` of a parser line and byte column."""
        if line > len(self.starts):
            return len(self.text)
        line = max(line, 1)
        begin = self.starts[line - 1]
        end = self.starts[line] if line < len(self.starts) else len(self.source)
        prefix = self.source[begin:end].encode('utf-8')[:max(column, 0)]
        return self.shift + begin + len(prefix.decode('utf-8', 'ignore'))

    def line(self, offset):
        return min(max(1, bisect_right(self.lines, offset)), max(self.total, 1))

    def start(self, line, column):
        at = self.offset(line, column)
        number = self.line(at)
        return number, at - self.lines[number - 1] + 1

    def end(self, line, column):
        return self.line(max(self.offset(line, column) - 1, 0))

    def through(self, cut):
        """Last splitlines line wholly before parser line ``cut``."""
        return 0 if cut <= 1 else self.line(self.offset(cut) - 1)

    def error(self, lineno, message):
        line = self.line(self.offset(lineno)) if isinstance(lineno, int) and lineno >= 1 else None
        return {'line': line, 'message': _message(message)}

    def symbol(self, node, anchor, name, kind, depth, uncertain, doc, parent):
        line, column = self.start(anchor.lineno, anchor.col_offset)
        start = min([line] + [self.start(item.lineno, item.col_offset)[0]
                              for item in getattr(node, 'decorator_list', None) or ()])
        end = max(self.end(node.end_lineno, node.end_col_offset), line)
        return Symbol(name, kind, start, line, end, column, depth, doc, bool(uncertain), parent)
