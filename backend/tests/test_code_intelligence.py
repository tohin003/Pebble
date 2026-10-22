"""Python definition outlines: ast-only, truthful partial coverage, splitlines numbering."""

import sys
import warnings

import pytest

from pebble import code_intelligence
from pebble.code_intelligence import (ELLIPSIS, IDENTIFIER_CHARS, Outline, OutlineCache, Symbol, display_name,
                                      display_qualname, match, outline, possible, probe, qualname)
from pebble.policy import PolicyError

SOURCE = '''\
"""Module docstring."""
import os

LIMIT = 10
first, (second, *rest) = 1, (2, 3)


class Outer:
    """Outer holds things."""
    size: int = 3

    class Inner:
        def method(self):
            return 1

    @staticmethod
    @decorate(
        option=True)
    def helper(value):
        def nested():
            local = 1
            return local
        return nested

    async def fetch(self):
        pass


async def runner():
    class Local:
        def go(self):
            pass
    return Local


if os.environ:
    def conditional():
        pass
else:
    CONDITIONAL = 1

try:
    import missing_module
except ImportError:
    def fallback():
        pass

type Alias = list[int]
'''


def names(result):
    return [qualname(result.symbols, s) for s in result.symbols]


def table(result):
    return {qualname(result.symbols, s): (s.kind, s.start_line, s.line, s.end_line, s.column, s.depth)
            for s in result.symbols}


def sym(name, kind='function', depth=0, parent=-1):
    return Symbol(name, kind, 1, 1, 1, 1, depth, None, False, parent)


def chain(*specs):
    """Symbols nested in order: each (name, kind) encloses the next."""
    return tuple(sym(name, kind, depth, depth - 1) for depth, (name, kind) in enumerate(specs))


def pathological():
    """The parent's reproducer: 80 nested 600+ character class names, 100 methods inside."""
    text = ''.join(' ' * i + 'class C' + str(i) + '_' + 'x' * 600 + ':\n' for i in range(80))
    return text + ''.join(' ' * 80 + f'def reconcile_{i:03d}(self): pass\n' for i in range(100))


def test_definitions_qualified_names_kinds_and_spans():
    result = outline(SOURCE)
    assert (result.status, result.issue, result.error) == ('parsed', None, None)
    assert result.parsed_through == result.total_lines == 48
    assert table(result) == {
        'LIMIT': ('variable', 4, 4, 4, 1, 0),
        'first': ('variable', 5, 5, 5, 1, 0),
        'second': ('variable', 5, 5, 5, 9, 0),
        'rest': ('variable', 5, 5, 5, 18, 0),
        'Outer': ('class', 8, 8, 26, 1, 0),
        'Outer.size': ('variable', 10, 10, 10, 5, 1),
        'Outer.Inner': ('class', 12, 12, 14, 5, 1),
        'Outer.Inner.method': ('method', 13, 13, 14, 9, 2),
        'Outer.helper': ('method', 16, 19, 23, 5, 1),
        'Outer.helper.<locals>.nested': ('function', 20, 20, 22, 9, 2),
        'Outer.fetch': ('async_method', 25, 25, 26, 5, 1),
        'runner': ('async_function', 29, 29, 33, 1, 0),
        'runner.<locals>.Local': ('class', 30, 30, 32, 5, 1),
        'runner.<locals>.Local.go': ('method', 31, 31, 32, 9, 2),
        'conditional': ('function', 37, 37, 38, 5, 0),
        'CONDITIONAL': ('variable', 40, 40, 40, 5, 0),
        'fallback': ('function', 45, 45, 46, 5, 0),
        'Alias': ('type_alias', 48, 48, 48, 6, 0),
    }
    # Function locals and imports are not definitions; source order is preserved.
    assert [s.line for s in result.symbols] == sorted(s.line for s in result.symbols)
    docs = {qualname(result.symbols, s): s.doc for s in result.symbols if s.doc}
    assert docs == {'Outer': 'Outer holds things.'}
    assert not any(s.uncertain for s in result.symbols)
    # Parent links point backwards to the enclosing definition, never forwards.
    assert all(s.parent < index for index, s in enumerate(result.symbols))


def test_line_numbers_follow_splitlines_like_read():
    feed = outline('import os\n\x0c\ndef after_feed():\n    return 1\n')
    assert table(feed)['after_feed'] == ('function', 4, 4, 5, 1, 0)
    separator = outline('s = "a\u2028b"\ndef after_separator():\n    pass\n')
    assert table(separator)['s'] == ('variable', 1, 1, 2, 1, 0)
    assert table(separator)['after_separator'][1:4] == (3, 3, 4)
    crlf = outline('class A:\r\n    def b(self):\r\n        pass\r\n')
    assert table(crlf) == {'A': ('class', 1, 1, 3, 1, 0), 'A.b': ('method', 2, 2, 3, 5, 1)}
    bom = outline('\ufeffdef f():\n    pass\n')
    assert table(bom)['f'] == ('function', 1, 1, 2, 2, 0)  # the BOM is character 1 of line 1
    wide = outline('é = 1; ñ = 2\n')  # parser columns are UTF-8 bytes; results are characters
    assert table(wide) == {'é': ('variable', 1, 1, 1, 1, 0), 'ñ': ('variable', 1, 1, 1, 8, 0)}


def test_syntax_error_keeps_only_the_recovered_prefix_and_says_so():
    text = ('def good():\n'
            '    return 1\n'
            '\n'
            'class Kept:\n'
            '    def inner(self):\n'
            '        return 2\n'
            '    oops = = 3\n'
            '\n'
            'def lost():\n'
            '    return 4\n')
    result = outline(text)
    assert (result.status, result.issue) == ('partial', 'syntax_error')
    assert result.error['line'] == 7 and result.error['message']
    assert (result.parsed_through, result.total_lines) == (6, 10)
    assert names(result) == ['good', 'Kept', 'Kept.inner']
    # Spans that reach the cut may continue past it; earlier ones are exact.
    assert {qualname(result.symbols, s): s.uncertain for s in result.symbols} == {
        'good': False, 'Kept': True, 'Kept.inner': True}


def test_recovery_backs_up_to_a_statement_start_over_decorators():
    text = ('def first():\n'
            '    return 1\n'
            '@decorator\n'
            'def second():\n'
            '    return = 2\n'
            'def third():\n'
            '    pass\n')
    result = outline(text)
    assert result.status == 'partial' and result.parsed_through == 2
    assert names(result) == ['first']
    unclosed = outline('def ok():\n    pass\nvalue = call(\n    1,\n')
    assert unclosed.status == 'partial' and names(unclosed) == ['ok']
    assert unclosed.parsed_through == 2


def test_unrecoverable_and_too_complex_sources_report_no_definitions(monkeypatch):
    failed = outline('def (:\n    pass\n')
    assert (failed.status, failed.issue, failed.parsed_through, failed.symbols) == ('failed', 'syntax_error', 0, ())
    assert failed.error['line'] == 1

    def deep(_source):
        raise RecursionError('nested too deeply')

    monkeypatch.setattr(code_intelligence, '_parse', deep)
    complex_ = outline('def a():\n    pass\n')
    assert (complex_.status, complex_.issue, complex_.symbols) == ('failed', 'too_complex', ())


def test_symbol_limit_is_partial_coverage_not_silent(monkeypatch):
    monkeypatch.setattr(code_intelligence, 'MAX_SYMBOLS', 3)
    result = outline('a = 1\nb = 2\nc = 3\nd = 4\n')
    assert (result.status, result.issue) == ('partial', 'symbol_limit')
    assert [s.name for s in result.symbols] == ['a', 'b', 'c'] and result.parsed_through == 4


def test_parsing_never_imports_or_executes_project_code(tmp_path):
    marker = tmp_path / 'ran.txt'
    text = (f'import definitely_missing_module_xyz\n'
            f'open({str(marker)!r}, "w").write("ran")\n'
            f'raise SystemExit(3)\n'
            f'def still_listed():\n'
            f'    pass\n')
    result = outline(text)
    assert not marker.exists()
    assert names(result) == ['still_listed'] and result.status == 'parsed'


def test_compile_warnings_from_untrusted_text_are_not_emitted():
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter('always')
        result = outline('PATTERN = "\\d+"\nif x is 1:\n    pass\n')
    assert result.status == 'parsed' and [s.name for s in result.symbols] == ['PATTERN']
    assert not [w for w in seen if issubclass(w.category, SyntaxWarning)]


def test_match_tiers_and_qualified_names():
    query = probe('SearchIndex')
    tiers = {name: match(sym(name), query) for name in (
        'SearchIndex', 'searchindex', 'search_index', 'SearchIndexer', 'MySearchIndexCache', 'IndexSearch', 'Search')}
    assert tiers == {'SearchIndex': 0, 'searchindex': 1, 'search_index': 1, 'SearchIndexer': 2,
                     'MySearchIndexCache': 3, 'IndexSearch': 4, 'Search': None}
    assert match(sym('SearchIndex'), probe('search index')) == 1
    symbols = chain(('Outer', 'class'), ('Inner', 'class'), ('method', 'method'))
    method = symbols[2]
    assert qualname(symbols, method) == 'Outer.Inner.method'
    assert match(method, probe('Outer.Inner.method'), symbols) == 0
    assert match(method, probe('Inner.method'), symbols) == 0
    assert match(method, probe('outer.inner.method'), symbols) == 1  # same scope, different case
    assert match(method, probe('Other.method'), symbols) is None
    assert match(method, probe('Outer.method'), symbols) is None  # qualifiers are the innermost scopes
    assert match(method, probe('Root.Outer.Inner.method'), symbols) is None  # deeper than the scope
    assert match(sym('method'), probe('Inner.method')) is None
    nested = chain(('run', 'function'), ('helper', 'function'))
    assert qualname(nested, nested[1]) == 'run.<locals>.helper'
    assert match(nested[1], probe('run.helper'), nested) == 0
    assert match(nested[1], probe('run.<locals>.helper'), nested) == 0
    assert code_intelligence.TIERS[2] == 'prefix'


def test_probe_validation_and_outline_mode():
    assert probe('', outline=True) is None and probe(None, outline=True) is None
    assert match(sym('anything'), None) == 0
    for bad in ('', None, '   ', 'a..b', '.a', 'a.', 'x' * 257, 'a\nb', 7, '\ud800',
                ' '.join(f'word{chr(97 + i)}' for i in range(17))):
        with pytest.raises(PolicyError):
            probe(bad)
    assert probe('  Outer.method ').qualifiers == ('outer',)


def test_prefilter_is_a_necessary_condition_only():
    query = probe('SearchIndex')
    assert possible(query, b'class searchindex: pass')
    assert possible(query, b'class search_index: pass')  # subword match still possible
    assert not possible(query, b'class searcher: pass')
    assert possible(None, b'')
    text = 'class Sea_rchIndex:\n    pass\nclass IndexSearch:\n    pass\ndef search_index():\n    pass\n'
    matched = [s.name for s in outline(text).symbols if match(s, query) is not None]
    # Word-tier and separator-equivalent matches survive the prefilter of their own file.
    assert matched == ['IndexSearch', 'search_index'] and possible(query, text.encode().lower())


def test_outline_cache_is_bounded_lru_and_namespaced():
    one = Outline('parsed', None, None, 1, 1, (sym('a'),))
    three = Outline('parsed', None, None, 1, 1, (sym('a'), sym('b'), sym('c')))
    cache = OutlineCache(entries=2, symbols=100)
    cache.put(('ns1', 'h1'), one)
    cache.put(('ns1', 'h2'), one)
    assert cache.get(('ns1', 'h1')) is one  # refreshes h1
    cache.put(('ns2', 'h3'), one)
    assert cache.get(('ns1', 'h2')) is None and len(cache) == 2
    cache.drop('ns1')
    assert cache.namespaces() == {'ns2'} and cache.get(('ns1', 'h1')) is None
    weighted = OutlineCache(entries=10, symbols=5)
    assert weighted.put(('ns', 'big'), three)  # cost 4
    assert weighted.put(('ns', 'small'), one)  # cost 2; total 6 evicts the oldest
    assert weighted.get(('ns', 'big')) is None and weighted.get(('ns', 'small')) is one
    assert not weighted.put(('ns', 'huge'), Outline('parsed', None, None, 1, 1, tuple(sym(str(i)) for i in range(9))))
    assert weighted.get(('ns', 'huge')) is None and len(weighted) == 1


def test_outline_cache_bounds_estimated_retained_bytes():
    small = Outline('parsed', None, None, 1, 1, (sym('a'),))
    documented = Outline('parsed', None, None, 1, 1, (sym('a')._replace(doc='d' * 100),))
    unit = code_intelligence.cost(('ns', 's0'), small)
    # Strings are charged at their real size, docstrings included.
    assert code_intelligence.cost(('ns', 's0'), documented) - unit == sys.getsizeof('d' * 100)
    cache = OutlineCache(entries=100, symbols=10 ** 6, byte_limit=3 * unit)
    for i in range(5):
        assert cache.put(('ns', f's{i}'), small)
    assert len(cache) == 3 and cache.usage()['bytes'] <= 3 * unit
    assert cache.get(('ns', 's0')) is None and cache.get(('ns', 's4')) is small
    big = outline(pathological())
    size = code_intelligence.cost(('ns', 'big'), big)
    tight = OutlineCache(entries=100, symbols=10 ** 6, byte_limit=size - 1)
    assert not tight.put(('ns', 'big'), big)  # larger than the whole budget: never cached
    assert tight.get(('ns', 'big')) is None and tight.usage() == {'entries': 0, 'symbols': 0, 'bytes': 0}


def test_pathological_names_stay_linear_in_memory_and_display_bounded():
    text = pathological()
    assert len(text.encode()) == 63_110
    result = outline(text)
    assert result.status == 'parsed' and len(result.symbols) == 180
    # Only each definition's own name is stored; nothing like the expanded qualified names.
    assert sum(len(s.name) for s in result.symbols) == 49_610
    assert sum(len(qualname(result.symbols, s)) for s in result.symbols) > 6_000_000
    assert code_intelligence.cost(('ns', 'h' * 64), result) < 200_000
    method = next(s for s in result.symbols if s.name == 'reconcile_000')
    assert (method.kind, method.line, method.column, method.depth) == ('method', 81, 81, 80)
    full = qualname(result.symbols, method)
    assert len(full) > 48_000 and full.startswith('C0_x') and full.endswith('.C79_' + 'x' * 600 + '.reconcile_000')
    shown, shortened = display_qualname(result.symbols, method)
    assert shortened and len(shown) <= IDENTIFIER_CHARS and ELLIPSIS in shown
    assert shown.startswith('C0_x') and shown.endswith('.reconcile_000')
    narrow, _ = display_qualname(result.symbols, method, code_intelligence.IDENTIFIER_FLOOR)
    assert len(narrow) <= code_intelligence.IDENTIFIER_FLOOR and narrow.endswith('.reconcile_000')
    # Matching uses the full parsed names: a shortened display form never resolves as a real scope.
    assert match(method, probe(narrow), result.symbols) is None
    assert match(method, probe('reconcile_000'), result.symbols) == 0
    deep = chain(('K' * 200, 'class'), ('run', 'method'))
    assert match(deep[1], probe('K' * 200 + '.run'), deep) == 0


def test_display_forms_mark_every_cut_and_keep_short_names_whole():
    assert display_name('short') == ('short', False)
    shown, shortened = display_name('n' * 3000)
    assert shortened and len(shown) == IDENTIFIER_CHARS and shown.startswith('nn') and ELLIPSIS in shown
    top = (sym('q' * 3000),)
    shown, shortened = display_qualname(top, top[0])  # no scope: the name gets the whole budget
    assert shortened and len(shown) == IDENTIFIER_CHARS
    layered = chain(('A' * 500, 'class'), ('B' * 500, 'class'), ('leaf', 'method'))
    shown, shortened = display_qualname(layered, layered[2], 96)
    assert shortened and len(shown) <= 96 and shown.startswith('AA') and shown.endswith('.leaf')
    assert shown.count(ELLIPSIS) == 2  # each clipped scope part is marked
    plain = chain(('Box', 'class'), ('run', 'method'))
    assert display_qualname(plain, plain[1]) == ('Box.run', False)


def test_syntax_tree_is_the_exact_parse_behind_outlines():
    from pebble.code_intelligence import syntax_tree
    parsed = syntax_tree(SOURCE)
    assert (parsed.status, parsed.issue, parsed.error, parsed.cut) == ('parsed', None, None, None)
    limit = parsed.tree.body[2]  # LIMIT = 10
    assert parsed.places.start(limit.lineno, limit.col_offset) == (4, 1)
    text = 'def good():\n    return 1\n\ndef broken(:\n    pass\n'
    partial, summary = syntax_tree(text), outline(text)
    assert (partial.status, partial.issue) == ('partial', 'syntax_error') and partial.tree is not None
    assert partial.places.through(partial.cut) == summary.parsed_through == 3
    assert partial.error == summary.error
    failed = syntax_tree('def (:\n    pass\n')
    assert (failed.status, failed.tree, failed.cut) == ('failed', None, None)
    bom = syntax_tree('﻿def f():\n    pass\n')
    node = bom.tree.body[0]
    assert bom.places.start(node.lineno, node.col_offset) == (1, 2)  # same columns as outline rows


def test_kind_order_ranks_every_python_and_tree_sitter_kind():
    from pebble import ts_symbols
    produced = {'class', 'function', 'async_function', 'method', 'async_method', 'type_alias', 'variable',
                'constant', 'property', 'struct', 'interface', 'type', 'enum', 'module', 'trait', 'impl', 'macro'}
    produced |= set(ts_symbols._JS.values()) | set(ts_symbols._GO.values()) | set(ts_symbols._RUST.values())
    assert produced <= set(code_intelligence.KIND_ORDER)
    # Declarations rank before bindings, as for Python definitions and assignments.
    assert {kind for kind, rank in code_intelligence.KIND_ORDER.items() if rank == 1} == {
        'type_alias', 'variable', 'constant', 'property', 'impl'}
