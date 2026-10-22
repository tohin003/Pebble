"""tree-sitter outlines for ts/tsx/js/go/rust: shared Symbol shape, explicit absence, truthful partials."""

import pytest

from pebble import code_intelligence, ts_symbols
from pebble.code_intelligence import match, probe, qualname

grammars = pytest.mark.skipif(not all(ts_symbols.available(name) for name in ts_symbols.LANGUAGES),
                              reason='tree_sitter_language_pack grammars are not installed')

TS = '''/** Doc for A. */
@dec()
export abstract class A<T> extends B implements C {
  static count: number = 0;
  #secret = 1;
  constructor(private x: number) { super(); }
  get value(): number { return 1; }
  async *gen() {}
  abstract run(): void;
}
export default function () {}
export function f(a: string): void { function inner() {} const local = 1; }
export const g = () => 1, h = 2;
let [d1, d2] = [1, 2];
interface I { a: string; m(x: number): void; }
type T = { notAMember: string };
enum E { One, Two = 2 }
namespace N { export const z = 1; }
declare module "mod" { export function q(): void; }
function overload(a: string): void;
var x = function named() {};
const obj = { meth() {}, arrow: () => 1 };
const K = class { inside() {} };
'''
GO = '''package main

// Doc for T.
// Second line.
type T struct {
\tA, B int
\tc string
}
type I interface { M(x int) error }
type Alias = T
type (
\tU int
)
const C, D = 1, 2
var V = 3
func F() { var local = 1; _ = local }
func (t *T) Method() {}
func (u U) Other() {}
func (w *Later) Early() {}
type Later struct{}
func G[K comparable](k K) {}
'''
RUST = '''/// Doc for S.
#[derive(Debug)]
pub struct S { pub a: i32, b: u8 }
pub enum E { One, Two(i32) }
pub trait Tr { fn req(&self); fn prov(&self) {} }
impl Tr for S { fn req(&self) {} }
impl<T> Gen<T> { pub fn new() -> Self { todo!() } }
mod inner { pub fn f() {} }
type Al = i32;
const K: i32 = 1;
static ST: i32 = 2;
macro_rules! mac { () => {} }
pub fn free() { fn nested() {} let x = 1; }
union U { a: i32 }
'''
JS = '''class A { constructor() { this.x = 1 } m() {} static s = 1; #p = 2 }
function* gen() {}
export const f = async () => {}, y = 3;
module.exports.z = function () {};
const { a, b: renamed, ...rest } = load();
'''


def rows(result):
    return [(qualname(result.symbols, s), s.kind, s.start_line, s.line, s.end_line, s.column, s.depth)
            for s in result.symbols]


def test_extension_detection_and_unsupported_languages():
    expected = {'a.ts': 'typescript', 'types.d.ts': 'typescript', 'b.mts': 'typescript', 'c.tsx': 'tsx',
                'd.js': 'javascript', 'e.JSX': 'javascript', 'f.cjs': 'javascript', 'main.go': 'go', 'lib.rs': 'rust',
                'x.py': None, 'y.c': None, 'z.swift': None, 'Makefile': None, 'dir.ts/README': None, 'go': None}
    assert {path: ts_symbols.language_for(path) for path in expected} == expected
    unsupported = ts_symbols.outline('int main() {}\n', 'c')
    assert (unsupported.status, unsupported.issue, unsupported.symbols) == ('failed', 'unsupported', ())


@grammars
def test_typescript_kinds_scopes_and_declaration_locations():
    result = ts_symbols.outline(TS, 'typescript')
    assert result[:5] == ('parsed', None, None, 23, 23)
    assert rows(result) == [
        ('A', 'class', 2, 3, 10, 23, 0),  # starts at its decorator; located at its name
        ('A.count', 'property', 4, 4, 4, 10, 1), ('A.#secret', 'property', 5, 5, 5, 3, 1),
        ('A.constructor', 'method', 6, 6, 6, 3, 1), ('A.value', 'method', 7, 7, 7, 7, 1),
        ('A.gen', 'method', 8, 8, 8, 10, 1), ('A.run', 'method', 9, 9, 9, 12, 1),
        ('f', 'function', 12, 12, 12, 17, 0), ('f.<locals>.inner', 'function', 12, 12, 12, 47, 1),
        ('g', 'function', 13, 13, 13, 14, 0), ('h', 'variable', 13, 13, 13, 27, 0),
        ('d1', 'variable', 14, 14, 14, 6, 0), ('d2', 'variable', 14, 14, 14, 10, 0),
        ('I', 'interface', 15, 15, 15, 11, 0), ('I.a', 'property', 15, 15, 15, 15, 1),
        ('I.m', 'method', 15, 15, 15, 26, 1), ('T', 'type_alias', 16, 16, 16, 6, 0),
        ('E', 'enum', 17, 17, 17, 6, 0), ('E.One', 'constant', 17, 17, 17, 10, 1),
        ('E.Two', 'constant', 17, 17, 17, 15, 1), ('N', 'module', 18, 18, 18, 11, 0),
        ('N.z', 'variable', 18, 18, 18, 28, 1), ('mod', 'module', 19, 19, 19, 16, 0),
        ('mod.q', 'function', 19, 19, 19, 40, 1), ('overload', 'function', 20, 20, 20, 10, 0),
        ('x', 'function', 21, 21, 21, 5, 0), ('obj', 'variable', 22, 22, 22, 7, 0),
        ('K', 'class', 23, 23, 23, 7, 0), ('K.inside', 'method', 23, 23, 23, 19, 1),
    ]
    # Locals, object-literal members, members of type literals and anonymous defaults are not definitions.
    names = {s.name for s in result.symbols}
    assert not names & {'local', 'meth', 'arrow', 'notAMember', 'default', 'named'}
    assert {qualname(result.symbols, s): s.doc for s in result.symbols if s.doc} == {'A': 'Doc for A.'}
    assert all(s.parent < index for index, s in enumerate(result.symbols))
    lines = TS.splitlines()
    for s in result.symbols:  # every location points at the name itself
        assert lines[s.line - 1][s.column - 1:].startswith(s.name.strip('"')) or s.kind == 'module'
    assert result.symbols[0].kind in code_intelligence.KIND_ORDER


@grammars
def test_tsx_and_javascript_outlines():
    tsx = ts_symbols.outline('export const App = () => <div>hi</div>;\n'
                             'class K extends React.Component { render() { return <p/>; } }\n', 'tsx')
    assert tsx.status == 'parsed' and rows(tsx) == [
        ('App', 'function', 1, 1, 1, 14, 0), ('K', 'class', 2, 2, 2, 7, 0), ('K.render', 'method', 2, 2, 2, 35, 1)]
    js = ts_symbols.outline(JS, 'javascript')
    assert js.status == 'parsed' and rows(js) == [
        ('A', 'class', 1, 1, 1, 7, 0), ('A.constructor', 'method', 1, 1, 1, 11, 1), ('A.m', 'method', 1, 1, 1, 40, 1),
        ('A.s', 'property', 1, 1, 1, 54, 1), ('A.#p', 'property', 1, 1, 1, 61, 1), ('gen', 'function', 2, 2, 2, 11, 0),
        ('f', 'function', 3, 3, 3, 14, 0), ('y', 'variable', 3, 3, 3, 34, 0),
        ('a', 'variable', 5, 5, 5, 9, 0), ('renamed', 'variable', 5, 5, 5, 15, 0), ('rest', 'variable', 5, 5, 5, 27, 0)]
    # `module.exports.z = ...` is an assignment expression, not a declaration.
    assert 'z' not in {s.name for s in js.symbols}


@grammars
def test_go_types_fields_receivers_and_doc_comments():
    result = ts_symbols.outline(GO, 'go')
    assert result[:5] == ('parsed', None, None, 21, 21)
    assert rows(result) == [
        ('T', 'struct', 5, 5, 8, 6, 0), ('T.A', 'property', 6, 6, 6, 2, 1), ('T.B', 'property', 6, 6, 6, 5, 1),
        ('T.c', 'property', 7, 7, 7, 2, 1), ('I', 'interface', 9, 9, 9, 6, 0), ('I.M', 'method', 9, 9, 9, 20, 1),
        ('Alias', 'type_alias', 10, 10, 10, 6, 0), ('U', 'type', 11, 12, 12, 2, 0),
        ('C', 'constant', 14, 14, 14, 7, 0), ('D', 'constant', 14, 14, 14, 10, 0),
        ('V', 'variable', 15, 15, 15, 5, 0), ('F', 'function', 16, 16, 16, 6, 0),
        ('T.Method', 'method', 17, 17, 17, 13, 1), ('U.Other', 'method', 18, 18, 18, 12, 1),
        # The receiver type is declared later, so no forward parent link is invented.
        ('Early', 'method', 19, 19, 19, 17, 0), ('Later', 'struct', 20, 20, 20, 6, 0),
        ('G', 'function', 21, 21, 21, 6, 0),
    ]
    assert result.symbols[0].doc == 'Doc for T.'
    assert all(s.parent < index for index, s in enumerate(result.symbols))


@grammars
def test_rust_items_impls_attributes_and_doc_comments():
    result = ts_symbols.outline(RUST, 'rust')
    assert result.status == 'parsed' and rows(result) == [
        ('S', 'struct', 2, 3, 3, 12, 0), ('S.a', 'property', 3, 3, 3, 20, 1), ('S.b', 'property', 3, 3, 3, 28, 1),
        ('E', 'enum', 4, 4, 4, 10, 0), ('E.One', 'constant', 4, 4, 4, 14, 1), ('E.Two', 'constant', 4, 4, 4, 19, 1),
        ('Tr', 'trait', 5, 5, 5, 11, 0), ('Tr.req', 'method', 5, 5, 5, 19, 1), ('Tr.prov', 'method', 5, 5, 5, 34, 1),
        ('S', 'impl', 6, 6, 6, 13, 0), ('S.req', 'method', 6, 6, 6, 20, 1),
        ('Gen', 'impl', 7, 7, 7, 9, 0), ('Gen.new', 'method', 7, 7, 7, 25, 1),
        ('inner', 'module', 8, 8, 8, 5, 0), ('inner.f', 'function', 8, 8, 8, 20, 1),
        ('Al', 'type_alias', 9, 9, 9, 6, 0), ('K', 'constant', 10, 10, 10, 7, 0), ('ST', 'variable', 11, 11, 11, 8, 0),
        ('mac', 'macro', 12, 12, 12, 14, 0), ('free', 'function', 13, 13, 13, 8, 0),
        ('free.<locals>.nested', 'function', 13, 13, 13, 20, 1), ('U', 'struct', 14, 14, 14, 7, 0),
        ('U.a', 'property', 14, 14, 14, 11, 1),
    ]
    assert result.symbols[0].doc == 'Doc for S.'  # the attribute between doc and item is skipped
    # Qualified matching is shared with Python outlines.
    method = next(s for s in result.symbols if qualname(result.symbols, s) == 'S.req')
    assert match(method, probe('S.req'), result.symbols) == 0
    assert match(method, probe('Tr.req'), result.symbols) is None


@grammars
def test_syntax_errors_are_partial_and_later_definitions_uncertain():
    text = ('export function before() { return 1; }\n'
            'function broken( {\n'
            '  return 2;\n'
            '}\n'
            'class After { m() {} }\n')
    result = ts_symbols.outline(text, 'typescript')
    assert (result.status, result.issue) == ('partial', 'syntax_error')
    line = result.error['line']
    assert line == 2 and result.parsed_through == 1 and result.total_lines == 5
    marks = {qualname(result.symbols, s): s.uncertain for s in result.symbols}
    assert marks['before'] is False
    assert marks and all(flag for name, flag in marks.items() if name != 'before')


@grammars
def test_lines_follow_splitlines_and_columns_count_characters():
    text = 'const sep = "a b";\n\x0c\nfunction after() {}\nconst é = 1, ñ = 2;\n'
    result = ts_symbols.outline(text, 'javascript')
    found = {s.name: (s.line, s.column) for s in result.symbols}
    # U+2028, the form feed and the newline after it each start a line for Read, so `after` is on line 5.
    assert found == {'sep': (1, 7), 'after': (5, 10), 'é': (6, 7), 'ñ': (6, 14)}
    bom = ts_symbols.outline('﻿function f() {}\n', 'javascript')
    assert [(s.name, s.line, s.column) for s in bom.symbols] == [('f', 1, 11)]  # the BOM is character 1
    crlf = ts_symbols.outline('fn a() {}\r\nfn b() {}\r\n', 'rust')
    assert [(s.name, s.line) for s in crlf.symbols] == [('a', 1), ('b', 2)]


@grammars
def test_symbol_and_node_limits_are_partial_not_silent(monkeypatch):
    monkeypatch.setattr(ts_symbols, 'MAX_SYMBOLS', 3)
    limited = ts_symbols.outline('fn a() {}\nfn b() {}\nfn c() {}\nfn d() {}\n', 'rust')
    assert (limited.status, limited.issue, [s.name for s in limited.symbols]) == ('partial', 'symbol_limit',
                                                                                  ['a', 'b', 'c'])
    monkeypatch.setattr(ts_symbols, 'MAX_SYMBOLS', 2_000)
    monkeypatch.setattr(ts_symbols, 'MAX_NODES', 12)
    stopped = ts_symbols.outline('fn a() {}\nfn b() {}\nfn c() {}\nfn d() {}\n', 'rust')
    assert (stopped.status, stopped.issue) == ('partial', 'too_complex')
    assert stopped.parsed_through < 4 and len(stopped.symbols) < 4
    monkeypatch.setattr(ts_symbols, 'MAX_SOURCE', 10)
    assert ts_symbols.outline('fn long_enough() {}\n', 'rust')[:2] == ('failed', 'too_large')


@grammars
def test_parsing_never_executes_source(tmp_path):
    marker = tmp_path / 'ran.txt'
    text = (f'require("fs").writeFileSync({str(marker)!r}, "ran");\n'
            'process.exit(3);\nfunction stillListed() {}\n')
    result = ts_symbols.outline(text, 'javascript')
    assert not marker.exists() and [s.name for s in result.symbols] == ['stillListed']


@grammars
def test_probe_and_fingerprint_describe_the_installed_grammars():
    found = ts_symbols.probe()
    assert found.available and found.reason is None and found.package and found.tree_sitter
    assert found.languages == {name: 'available' for name in ts_symbols.LANGUAGES}
    assert ts_symbols.fingerprint('go') != ts_symbols.fingerprint('rust')


def test_absent_package_degrades_explicitly_and_changes_the_fingerprint(monkeypatch):
    before = ts_symbols.fingerprint('typescript')

    def missing():
        raise ImportError('No module named tree_sitter_language_pack')

    monkeypatch.setattr(ts_symbols, '_import_pack', missing)
    ts_symbols.reset()
    try:
        assert not ts_symbols.available() and not ts_symbols.available('typescript')
        report = ts_symbols.probe()
        assert not report.available and 'not importable' in report.reason
        assert set(report.languages.values()) == {'unavailable'}
        result = ts_symbols.outline('export function f() {}\n', 'typescript')
        assert (result.status, result.issue, result.symbols, result.total_lines) == ('failed', 'unavailable', (), 1)
        assert 'not installed' in result.error['message']
        assert ts_symbols.fingerprint('typescript') != before or not ts_symbols.available('typescript')
    finally:
        monkeypatch.undo()
        ts_symbols.reset()
    assert ts_symbols.fingerprint('typescript') == before


def test_one_missing_grammar_leaves_the_others_usable(monkeypatch):
    real = ts_symbols._import_pack

    class Partial:
        def __init__(self, pack):
            self.pack = pack

        def get_language(self, name):
            if name == 'go':
                raise LookupError('Could not find language library for go')
            return self.pack.get_language(name)

        def get_parser(self, name):
            if name == 'go':
                raise LookupError('Could not find language library for go')
            return self.pack.get_parser(name)

    try:
        pack = real()
    except ImportError:
        pytest.skip('tree_sitter_language_pack is not installed')
    monkeypatch.setattr(ts_symbols, '_import_pack', lambda: Partial(pack))
    ts_symbols.reset()
    try:
        assert not ts_symbols.available('go') and ts_symbols.available('rust')
        assert ts_symbols.probe().languages['go'] == 'unavailable'
        assert ts_symbols.outline('package p\nfunc F() {}\n', 'go').issue == 'unavailable'
        assert [s.name for s in ts_symbols.outline('fn f() {}\n', 'rust').symbols] == ['f']
    finally:
        monkeypatch.undo()
        ts_symbols.reset()
