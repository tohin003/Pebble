"""Definition outlines for TypeScript, JavaScript, Go and Rust via tree-sitter.

The optional ``tree_sitter_language_pack`` grammars parse exactly the text the
caller passes in; nothing is imported, compiled or executed. Availability is
probed lazily, once per grammar, in the style of AgentGuard's ``symbols_ts``.
When the package or a grammar is absent, ``outline`` returns a failed outline
with issue ``unavailable``: evidence of nothing, never a claim that a definition
does not exist. Files whose extension is not listed in SUFFIXES are not outlined.

Outlines reuse ``code_intelligence.Outline`` and ``Symbol``, so matching, ranking,
qualified names and bounded display stay one implementation. Lines follow
``str.splitlines`` like ``Read``. ``line`` and ``column`` (1-based characters)
locate the definition's name. ``start_line`` also covers decorators, ``export``,
Rust attributes and the like. Rust ``impl`` blocks are named after their type.
A Go method's parent is its receiver type when that type is declared earlier
in the same file. ``doc`` is the first line of an adjacent JSDoc, Go or Rust
doc comment, which is untrusted text. Function locals are not definitions,
except nested named functions and classes, as in Python outlines.

tree-sitter recovers from syntax errors instead of stopping. An outline with an
error node is therefore ``partial``: ``parsed_through`` ends before the first
error, and definitions at or after it are kept but marked ``uncertain`` because
their extent or existence may be an artifact of recovery.
"""

import functools
import threading
from bisect import bisect_right
from collections import namedtuple

from .code_intelligence import DOC_CHARS, MAX_SYMBOLS, Outline, Symbol, _SPLIT_BREAK, _message

VERSION = 'tree-sitter-outline-1'
SUFFIXES = {'.ts': 'typescript', '.mts': 'typescript', '.cts': 'typescript', '.tsx': 'tsx',
            '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript', '.jsx': 'javascript',
            '.go': 'go', '.rs': 'rust'}
LANGUAGES = ('typescript', 'tsx', 'javascript', 'go', 'rust')
MAX_SOURCE = 1024 * 1024  # UTF-8 bytes one outline may parse; search passes at most MAX_FILE
MAX_NODES = 400_000  # syntax nodes visited per outline
DOC_LOOKBACK = 64  # adjacent comment lines examined for a doc comment

_SCOPES = frozenset({'class', 'interface', 'struct', 'trait', 'enum', 'module', 'impl',
                     'function', 'method'})
_CALLABLE = frozenset({'function', 'method'})
_JS = {
    'class_declaration': 'class', 'abstract_class_declaration': 'class',
    'function_declaration': 'function', 'generator_function_declaration': 'function',
    'function_signature': 'function', 'method_definition': 'method', 'method_signature': 'method',
    'abstract_method_signature': 'method', 'interface_declaration': 'interface',
    'type_alias_declaration': 'type_alias', 'enum_declaration': 'enum', 'internal_module': 'module',
    'module': 'module', 'public_field_definition': 'property', 'field_definition': 'property',
    'property_signature': 'property',
}
_GO = {
    'function_declaration': 'function', 'method_declaration': 'method', 'type_alias': 'type_alias',
    'method_elem': 'method', 'method_spec': 'method',
}
_RUST = {
    'function_item': 'function', 'function_signature_item': 'function', 'struct_item': 'struct',
    'union_item': 'struct', 'enum_item': 'enum', 'trait_item': 'trait', 'mod_item': 'module',
    'impl_item': 'impl', 'type_item': 'type_alias', 'const_item': 'constant', 'static_item': 'variable',
    'macro_definition': 'macro', 'enum_variant': 'constant',
}
_KINDS = {'typescript': _JS, 'tsx': _JS, 'javascript': _JS, 'go': _GO, 'rust': _RUST}
_FUNCTION_VALUES = frozenset({'arrow_function', 'function_expression', 'function', 'generator_function'})
_COMMENTS = frozenset({'comment', 'line_comment', 'block_comment'})
# Literals and comments never contain declarations; their subtrees are not walked.
_OPAQUE = _COMMENTS | {'string', 'template_string', 'string_literal', 'raw_string_literal',
                       'interpreted_string_literal', 'regex', 'jsx_text', 'number'}
_PATTERN_NAMES = frozenset({'identifier', 'shorthand_property_identifier_pattern'})

Probe = namedtuple('Probe', 'available package tree_sitter languages reason')

_LOCAL = threading.local()


def language_for(path):
    """The grammar for a path's extension, or None when this module does not outline it."""
    dot = path.rfind('.')
    return SUFFIXES.get(path[dot:].lower()) if dot > path.rfind('/') else None


def _import_pack():
    import tree_sitter_language_pack
    return tree_sitter_language_pack


@functools.lru_cache(maxsize=None)
def _versions():
    try:
        from importlib import metadata
        return metadata.version('tree-sitter-language-pack'), metadata.version('tree-sitter')
    except Exception:  # noqa: BLE001 - metadata is informative only
        return None, None


@functools.lru_cache(maxsize=None)
def _language(language):
    """The loaded grammar, or None when the package or this grammar is unavailable."""
    if language not in LANGUAGES:
        return None
    try:
        return _import_pack().get_language(language)
    except Exception:  # noqa: BLE001 - absent package, unbuilt grammar or ABI mismatch
        return None


def available(language=None):
    """Whether ``language`` (or, with None, any supported language) can be outlined now."""
    if language is None:
        return any(_language(name) is not None for name in LANGUAGES)
    return _language(language) is not None


def probe():
    """Content-free availability report; never downloads or installs anything."""
    try:
        _import_pack()
    except Exception as exc:  # noqa: BLE001
        return Probe(False, None, None, {name: 'unavailable' for name in LANGUAGES},
                     f'tree_sitter_language_pack is not importable ({type(exc).__name__})')
    package, core = _versions()
    languages = {name: 'available' if _language(name) is not None else 'unavailable' for name in LANGUAGES}
    found = any(state == 'available' for state in languages.values())
    return Probe(found, package, core, languages, None if found else 'no supported grammar loaded')


def reset():
    """Forget cached availability (tests, or after the environment changed)."""
    _language.cache_clear()
    _versions.cache_clear()
    _LOCAL.__dict__.clear()


def fingerprint(language):
    """Settings and grammar state that change an outline; caches and cursors bind it."""
    return repr((VERSION, language, MAX_SYMBOLS, DOC_CHARS, MAX_NODES, available(language)) + _versions())


def _parser(language):
    parsers = _LOCAL.__dict__.setdefault('parsers', {})  # tree-sitter parsers are not shared across threads
    if language not in parsers:
        grammar = _language(language)
        if grammar is None:
            return None
        try:
            parsers[language] = _import_pack().get_parser(language)
        except Exception:  # noqa: BLE001
            return None
    return parsers[language]


def outline(text, language):
    """Definitions in ``text`` for one supported grammar, and how much the parser covered."""
    total = len(text.splitlines())
    if language not in LANGUAGES:
        return Outline('failed', 'unsupported', {'line': None, 'message': f'No outline grammar for {language!r}.'},
                       0, total, ())
    data = text.encode('utf-8', 'surrogatepass')
    if len(data) > MAX_SOURCE:
        return Outline('failed', 'too_large', None, 0, total, ())
    parser = _parser(language)
    if parser is None:
        return Outline('failed', 'unavailable',
                       {'line': None, 'message': f'The tree-sitter {language} grammar is not installed.'}, 0, total, ())
    try:
        tree = parser.parse(data)
    except Exception as exc:  # noqa: BLE001 - malformed input must never break discovery
        return Outline('failed', 'parser_error', {'line': None, 'message': _message(exc)}, 0, total, ())
    walker = _Walker(text, data, language, total)
    try:
        symbols, stopped = walker.walk(tree.root_node)
    except (RecursionError, MemoryError):
        return Outline('failed', 'too_complex', None, 0, total, ())
    status, issue, error, through = 'parsed', None, None, total
    if tree.root_node.has_error:
        line = walker.first_error(tree.root_node)
        status, issue, through = 'partial', 'syntax_error', max(0, (line or 1) - 1)
        error = {'line': line, 'message': 'tree-sitter recovered from a syntax error'}
        symbols = [symbol._replace(uncertain=True) if line and symbol.end_line >= line else symbol
                   for symbol in symbols]
    if stopped is not None:
        status, issue, through = 'partial', issue or 'too_complex', min(through, max(0, stopped - 1))
    if len(symbols) > MAX_SYMBOLS:  # pre-order: parents precede children, so kept links stay valid
        symbols, status, issue = symbols[:MAX_SYMBOLS], 'partial', issue or 'symbol_limit'
    return Outline(status, issue, error, through, total, tuple(symbols))


class _Walker:
    """Collect definitions in pre-order, mapping byte offsets to splitlines lines and characters."""

    def __init__(self, text, data, language, total):
        self.text, self.data, self.language, self.total = text, data, language, total
        self.kinds = _KINDS[language]
        # Byte offset where each splitlines line starts (str.splitlines breaks, not only \n).
        starts, offset, previous = [0], 0, 0
        for found in _SPLIT_BREAK.finditer(text):
            offset += len(text[previous:found.end()].encode('utf-8', 'surrogatepass'))
            previous = found.end()
            starts.append(offset)
        self.starts = starts
        self.visited = 0
        self.types = {}  # Go receiver type name -> symbol index
        self.last = (0, 0, 0)  # (line, byte offset, characters before it on that line)

    def line(self, offset):
        return min(max(1, bisect_right(self.starts, offset)), max(self.total, 1))

    def column(self, offset):
        # Pre-order names arrive in increasing offsets, so a long minified line is decoded once.
        number = self.line(offset)
        line, at, chars = self.last
        if line != number or offset < at:
            at, chars = self.starts[number - 1], 0
        chars += len(self.data[at:offset].decode('utf-8', 'ignore'))
        self.last = (number, offset, chars)
        return chars + 1

    def first_error(self, root):
        stack = [root]
        while stack:
            node = stack.pop()
            if node.is_error or node.is_missing:
                return self.line(node.start_byte)
            if node.has_error:
                stack.extend(reversed(node.children))
        return None

    def name(self, node):
        return self.data[node.start_byte:node.end_byte].decode('utf-8', 'replace')

    def walk(self, root):
        found = []
        stack = [(root, -1, 0, 'module')]
        while stack:
            node, parent, depth, scope = stack.pop()
            self.visited += 1
            if self.visited > MAX_NODES:
                return found, self.line(node.start_byte)
            entries = self.definitions(node, scope)
            inner, inner_depth, inner_scope = parent, depth, scope
            for kind, name_node, outer in entries:
                index = len(found)
                owner = parent
                if node.type == 'method_declaration' and self.language == 'go':
                    owner, entry_depth = self.receiver(node, found)
                else:
                    entry_depth = depth
                found.append(self.symbol(node, name_node, outer, kind, entry_depth, owner))
                if kind == 'struct' or kind == 'interface' or kind == 'type':
                    self.types.setdefault(found[index].name, index)
                if kind in _SCOPES and len(entries) == 1:
                    inner, inner_depth, inner_scope = index, entry_depth + 1, kind
            if node.type not in _OPAQUE:
                stack.extend((child, inner, inner_depth, inner_scope) for child in reversed(node.children))
        return found, None

    def definitions(self, node, scope):
        """(kind, name node, outer node) for each definition ``node`` introduces."""
        kind = self.kinds.get(node.type)
        language, local = self.language, scope in _CALLABLE
        if language == 'go':
            if node.type == 'type_spec':
                body = node.child_by_field_name('type')
                kind = {'struct_type': 'struct', 'interface_type': 'interface'}.get(body.type if body else '', 'type')
            elif node.type in ('const_spec', 'var_spec') and not local:
                kind = 'constant' if node.type == 'const_spec' else 'variable'
                return [(kind, name, node) for name in node.children_by_field_name('name') if name.is_named]
            elif node.type == 'field_declaration' and scope == 'struct':
                return [('property', name, node) for name in node.children_by_field_name('name') if name.is_named]
        elif language == 'rust':
            if node.type == 'field_declaration' and scope == 'struct':
                kind = 'property'
            elif kind == 'function' and scope in ('impl', 'trait'):
                kind = 'method'
            elif kind in ('constant', 'variable') and local or kind == 'constant' and node.type == 'enum_variant' \
                    and scope != 'enum':
                return []
        else:
            if node.type == 'variable_declarator':
                if local:
                    return []
                value = node.child_by_field_name('value')
                target = node.child_by_field_name('name')
                if target is None:
                    return []
                if target.type not in ('identifier', 'type_identifier'):
                    return [('variable', name, node) for name in self.pattern_names(target)]
                kind = ('function' if value is not None and value.type in _FUNCTION_VALUES else
                        'class' if value is not None and value.type == 'class' else 'variable')
                return [(kind, target, node)]
            if node.type == 'method_definition' and (node.parent is None or node.parent.type != 'class_body'):
                return []  # object-literal methods are not declarations
            if (kind == 'property' or node.type == 'method_signature') and scope not in ('class', 'interface'):
                return []  # members of object type literals, not of a declared type
            if node.type in ('enum_body',):
                return [('constant', child, child) for child in node.children
                        if child.type == 'property_identifier']
            if node.type == 'enum_assignment':
                kind = 'constant'
        if kind is None:
            return []
        if kind == 'impl':
            target = node.child_by_field_name('type')
            if target is not None and target.type == 'generic_type':
                target = target.child_by_field_name('type') or target
            return [(kind, target, node)] if target is not None else []
        name = node.child_by_field_name('name') or node.child_by_field_name('property')
        if name is None:
            return []  # anonymous: export default function () {}
        return [(kind, name, node)]

    def pattern_names(self, pattern):
        names, stack = [], [pattern]
        while stack and len(names) < 256:
            item = stack.pop()
            if item.type in _PATTERN_NAMES:
                names.append(item)
            elif item.type in ('pair_pattern',):
                value = item.child_by_field_name('value')
                if value is not None:
                    stack.append(value)
            else:
                stack.extend(reversed(item.named_children))
        return names

    def receiver(self, node, found):
        """(parent index, depth) for a Go method: its receiver type if declared earlier here."""
        receiver = node.child_by_field_name('receiver')
        stack = [receiver] if receiver is not None else []
        while stack:
            item = stack.pop()
            if item.type == 'type_identifier':
                index = self.types.get(self.name(item))
                return (index, found[index].depth + 1) if index is not None else (-1, 0)
            stack.extend(reversed(item.named_children))
        return -1, 0

    def symbol(self, node, name_node, outer, kind, depth, parent):
        name = self.name(name_node)
        if kind == 'module' and name[:1] in '"\'`' and name[-1:] == name[:1]:
            name = name[1:-1]  # declare module "name"
        first = self.leading(outer)
        start = min(first.start_byte, node.start_byte)
        line = self.line(name_node.start_byte)
        end = max(self.line(max(outer.end_byte - 1, outer.start_byte)), line)
        return Symbol(name, kind, min(self.line(start), line), line, end, self.column(name_node.start_byte),
                      depth, self.doc(first), False, parent)

    def leading(self, node):
        """The outermost node that still belongs to this declaration (export, attributes)."""
        if node.parent is not None and node.parent.type in ('export_statement', 'ambient_declaration'):
            node = node.parent
        if (self.language == 'go' and node.parent is not None and node.prev_named_sibling is None
                and node.parent.type in ('type_declaration', 'const_declaration', 'var_declaration')):
            node = node.parent  # the comment above `type T struct` documents the first spec
        if self.language == 'rust':
            while node.prev_sibling is not None and node.prev_sibling.type == 'attribute_item':
                node = node.prev_sibling
        return node

    def doc(self, node):
        comments, sibling = [], node.prev_sibling
        row = node.start_point[0]
        while sibling is not None and sibling.type in _COMMENTS and len(comments) < DOC_LOOKBACK:
            if sibling.end_point[0] < row - 1:
                break
            comments.append(sibling)
            row = sibling.start_point[0]
            sibling = sibling.prev_sibling
        if not comments:
            return None
        comments.reverse()
        texts = [self.name(item) for item in comments]
        if self.language == 'rust':
            texts = [text for text in texts if text.startswith(('///', '/**')) and not text.startswith('////')]
        elif self.language != 'go':
            texts = [text for text in texts if text.startswith('/**')][-1:]
        for text in texts:
            for raw in text.splitlines():
                line = raw.strip().lstrip('/').lstrip('*').strip()
                if line.endswith('*/'):
                    line = line[:-2].strip()
                if line:
                    return ' '.join(line.split())[:DOC_CHARS]
        return None
