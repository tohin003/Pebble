"""Phase 20 G20-diag: structured diagnostics after edits (D19 §8.3; frontier gaps C07/X09).

The parsers read recorded output shapes of tsc, pyright, ruff, eslint, mypy, Go, cargo, py_compile, unittest and
pytest. Binding, freshness, delivery and the completion rule use a real App store and project folder. The macOS
cases run real recipes in the sandbox. The parent's testing.py and app.py patches (see the track note) are simulated
with their exact code, and the simulation is idempotent with the merged patches. No model, network or credential is
used: the one end-to-end run scripts Providers.complete.
"""

import copy
import hashlib
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from pebble import diagnostics, testing
from pebble.app import App
from pebble.diagnostics import DiagnosticsError
from pebble.permissions import DEFAULT
from pebble.policy import Workspace
from pebble.providers import Providers
from pebble.token_budget import TokenBudget

macos = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox')
SNAP = '/private/var/folders/zz/T/pebble-test-abc/project'
RESULT_KEYS = {'exit_code', 'output', 'output_projection', 'controls_removed', 'output_bytes', 'output_truncated',
               'outcome', 'tests', 'recipe', 'kind', 'snapshot_digest', 'snapshot_files', 'skipped_large',
               'skipped_large_count', 'snapshot_truncated', 'drafts_included', 'executable_sha256', 'pinned',
               'environment', 'isolation'}


def sha(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def extract(text, *names, exit_code=1, recipe='npm-lint', kind='lint'):
    return diagnostics.extract(recipe, kind, exit_code, text, {n: sha(n) for n in names}, roots=diagnostics._roots(SNAP))


def rows(record):
    return [(d['path'], d['line'], d['column'], d['severity'], d['tool'], d['code']) for d in record['diagnostics']]


# -- parsers: every supported output shape binds only to snapshot files --------------------------------------------

def test_tsc_plain_and_pretty_output_binds_only_snapshot_files():
    text = ("src/math.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"
            "  Type 'string' is not assignable.\n"
            "node_modules/@types/node/index.d.ts(20,1): error TS6200: Definitions conflict.\n"
            "app/(group)/page.tsx(2,3): error TS1005: ';' expected.\n"
            "src/app.ts:4:5 - error TS2551: Property 'lenght' does not exist on type 'string[]'. Did you mean 'length'?\n"
            "\n4     xs.lenght\n      ~~~~~~~\n\nFound 3 errors in 3 files.\n\nErrors  Files\n     1  src/app.ts:4\n")
    record = extract(text, 'src/math.ts', 'src/app.ts', 'app/(group)/page.tsx')
    assert rows(record) == [('src/math.ts', 3, 7, 'error', 'tsc', 'TS2322'),
                            ('app/(group)/page.tsx', 2, 3, 'error', 'tsc', 'TS1005'),
                            ('src/app.ts', 4, 5, 'error', 'tsc', 'TS2551')]
    assert record['diagnostics'][0]['message'] == "Type 'string' is not assignable to type 'number'."
    assert record['unbound'] == 1 and record['omitted'] == 0
    assert record['counts'] == {'error': 3, 'warning': 0, 'info': 0}
    assert record['files'] == {n: sha(n) for n in ('app/(group)/page.tsx', 'src/app.ts', 'src/math.ts')}


def test_pyright_absolute_paths_bind_under_the_snapshot_and_its_alias_only():
    text = (f'{SNAP}/src/app.py\n'
            f'  {SNAP}/src/app.py:4:12 - error: "undefined_name" is not defined (reportUndefinedVariable)\n'
            '  /var/folders/zz/T/pebble-test-abc/project/src/app.py:1:8 - warning: Import "yaml" could not be '
            'resolved from source (reportMissingModuleSource)\n'
            '  /Library/Frameworks/Python.framework/Versions/3.14/lib/python3.14/typing.py:1:1 - error: Stub\n'
            f'  {SNAP}/src/app.py:9:1 - information: Code is unreachable\n'
            '2 errors, 1 warning, 1 information \n')
    record = extract(text, 'src/app.py', 'typing.py')
    assert rows(record) == [('src/app.py', 4, 12, 'error', 'pyright', 'reportUndefinedVariable'),
                            ('src/app.py', 1, 8, 'warning', 'pyright', 'reportMissingModuleSource'),
                            ('src/app.py', 9, 1, 'info', 'pyright', None)]
    assert record['diagnostics'][0]['message'] == '"undefined_name" is not defined'
    assert record['unbound'] == 1  # The library typing.py never becomes the project's typing.py.


def test_ruff_concise_old_full_and_current_full_formats():
    concise = ('src/app.py:1:8: F401 [*] `os` imported but unused\n'
               'src/app.py:7:89: E501 Line too long (101 > 88)\n'
               '  |\n1 | import os\n  |        ^^ F401\n  |\n  = help: Remove unused import: `os`\n'
               'Found 2 errors.\n[*] 1 fixable with the `--fix` option.\n')
    record = extract(concise, 'src/app.py')
    assert rows(record) == [('src/app.py', 1, 8, 'error', None, 'F401'), ('src/app.py', 7, 89, 'error', None, 'E501')]
    assert record['diagnostics'][0]['message'] == '`os` imported but unused'
    full = ('F401 [*] `sys` imported but unused\n --> src/util.py:2:8\n  |\n1 | import os\n2 | import sys\n'
            '  |        ^^^\n  |\nhelp: Remove unused import: `sys`\n\n'
            'invalid-syntax: Expected an indented block after function definition\n --> src/util.py:5:1\n  |\n'
            '4 | def f():\n5 | return 1\n  | ^^^^^^\n  |\n\nFound 2 errors.\n')
    record = extract(full, 'src/util.py')
    assert rows(record) == [('src/util.py', 2, 8, 'error', 'ruff', 'F401'),
                            ('src/util.py', 5, 1, 'error', 'ruff', 'invalid-syntax')]
    assert record['diagnostics'][1]['message'] == 'Expected an indented block after function definition'
    # A clean run (exit 0) reports untyped findings as warnings, never as errors.
    assert rows(extract(concise, 'src/app.py', exit_code=0))[0][3] == 'warning'


def test_eslint_stylish_blocks_and_unix_lines():
    text = (f'{SNAP}/src/index.js\n'
            "   1:10  error    'helper' is defined but never used  no-unused-vars\n"
            '  12:3   warning  Unexpected console statement        no-console\n'
            '  14:1   error    Unexpected any. Specify a different type  @typescript-eslint/no-explicit-any\n'
            '\n'
            f'{SNAP}/src/other.js\n'
            '  2:1  error  Parsing error: Unexpected token )\n'
            '\n'
            '/opt/elsewhere/vendor.js\n'
            '  3:1  error  Something outside  no-undef\n'
            '\n'
            '✖ 5 problems (4 errors, 1 warning)\n'
            f'{SNAP}/src/index.js:5:7: Unexpected var. [Error/no-var]\n')
    record = extract(text, 'src/index.js', 'src/other.js')
    assert rows(record) == [('src/index.js', 1, 10, 'error', 'eslint', 'no-unused-vars'),
                            ('src/index.js', 12, 3, 'warning', 'eslint', 'no-console'),
                            ('src/index.js', 14, 1, 'error', 'eslint', '@typescript-eslint/no-explicit-any'),
                            ('src/other.js', 2, 1, 'error', 'eslint', None),
                            ('src/index.js', 5, 7, 'error', 'eslint', 'no-var')]
    assert [d['message'] for d in record['diagnostics']][:2] == ["'helper' is defined but never used",
                                                                  'Unexpected console statement']
    assert record['diagnostics'][3]['message'] == 'Parsing error: Unexpected token )'
    assert record['unbound'] == 1


def test_mypy_gcc_go_pylint_and_python_warning_lines():
    text = ('src/app.py:4: error: Incompatible types in assignment (expression has type "str", variable has type '
            '"int")  [assignment]\n'
            'src/app.py:9:5: note: Revealed type is "builtins.int"\n'
            "src/main.c:3:5: warning: unused variable 'x' [-Wunused-variable]\n"
            './add.go:3:30: undefined: c\n'
            'src/app.py:1:0: C0114: Missing module docstring (missing-module-docstring)\n'
            f'{SNAP}/src/app.py:3: DeprecationWarning: the imp module is deprecated\n'
            'Found 1 error in 1 file (checked 2 source files)\n')
    record = extract(text, 'src/app.py', 'src/main.c', 'add.go')
    assert rows(record) == [('src/app.py', 4, None, 'error', None, 'assignment'),
                            ('src/app.py', 9, 5, 'info', None, None),
                            ('src/main.c', 3, 5, 'warning', None, '-Wunused-variable'),
                            ('add.go', 3, 30, 'error', None, None),
                            ('src/app.py', 1, None, 'error', None, 'C0114'),
                            ('src/app.py', 3, None, 'warning', 'python', 'DeprecationWarning')]
    assert record['diagnostics'][0]['message'] == ('Incompatible types in assignment (expression has type "str", '
                                                   'variable has type "int")')
    assert record['diagnostics'][3]['message'] == 'undefined: c'


def test_cargo_and_rustc_arrow_locations():
    text = ('error[E0308]: mismatched types\n --> src/main.rs:2:18\n  |\n2 |     let x: i32 = "a";\n'
            '  |            ---   ^^^ expected `i32`, found `&str`\n\n'
            'warning: unused variable: `y`\n --> src/main.rs:3:9\n\n'
            'error: could not compile `demo` (bin "demo") due to 1 previous error\n')
    record = extract(text, 'src/main.rs', recipe='cargo-build', kind='build')
    assert rows(record) == [('src/main.rs', 2, 18, 'error', None, 'E0308'), ('src/main.rs', 3, 9, 'warning', None, None)]
    assert record['diagnostics'][0]['message'] == 'mismatched types'


def test_py_compile_compileall_and_older_sorry_lines():
    text = ('  File "src/broken.py", line 2\n    def f(\n         ^\nSyntaxError: \'(\' was never closed\n'
            "*** Error compiling './src/third.py'...\n"
            '  File "./src/third.py", line 1\n    x = = 1\n        ^\nSyntaxError: invalid syntax\n\n'
            'Sorry: IndentationError: unexpected indent (src/other.py, line 4)\n')
    record = extract(text, 'src/broken.py', 'src/third.py', 'src/other.py', recipe='python-unittest', kind='test')
    assert rows(record) == [('src/broken.py', 2, None, 'error', 'python', 'SyntaxError'),
                            ('src/third.py', 1, None, 'error', 'python', 'SyntaxError'),
                            ('src/other.py', 4, None, 'error', 'python', 'IndentationError')]
    assert [d['message'] for d in record['diagnostics']] == ["SyntaxError: '(' was never closed",
                                                             'SyntaxError: invalid syntax',
                                                             'IndentationError: unexpected indent']


LIBRARY = '/Library/Frameworks/Python.framework/Versions/3.14/lib/python3.14/unittest'
UNITTEST_OUTPUT = f'''test_add (test_calc.AddTest.test_add) ... FAIL
test_div (test_calc.AddTest.test_div) ... ERROR

======================================================================
ERROR: test_div (test_calc.AddTest.test_div)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "{SNAP}/test_calc.py", line 9, in test_div
    divide(1, 0)
    ~~~~~~^^^^^^
  File "{SNAP}/calc.py", line 5, in divide
    return a / b
           ~~^~~
ZeroDivisionError: division by zero

======================================================================
FAIL: test_add (test_calc.AddTest.test_add)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "{SNAP}/test_calc.py", line 6, in test_add
    self.assertEqual(add(1, 2), 4)
    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^
AssertionError: 3 != 4

======================================================================
ERROR: test_io (test_io.IOTest.test_io)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "{LIBRARY}/case.py", line 58, in testPartExecutor
    yield
  File "{LIBRARY}/loader.py", line 396, in _find_test_path
    raise RuntimeError('library only')
RuntimeError: library only

----------------------------------------------------------------------
Ran 3 tests in 0.001s

FAILED (failures=1, errors=2)
'''
IMPORT_FAILURE = f'''======================================================================
ERROR: test_calc (unittest.loader._FailedTest.test_calc)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_calc
Traceback (most recent call last):
  File "{LIBRARY}/loader.py", line 396, in _find_test_path
    module = self._get_module_from_name(name)
  File "{SNAP}/test_calc.py", line 3, in <module>
    from calc import add
  File "{SNAP}/calc.py", line 1
    def add(a, b)
                 ^
SyntaxError: expected ':'


----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
'''


def test_unittest_blocks_use_the_innermost_project_frame_and_never_library_files():
    record = extract(UNITTEST_OUTPUT, 'calc.py', 'test_calc.py', 'loader.py', 'case.py',
                     recipe='python-unittest', kind='test')
    assert rows(record) == [('calc.py', 5, None, 'error', 'unittest', 'ZeroDivisionError'),
                            ('test_calc.py', 6, None, 'error', 'unittest', 'AssertionError')]
    assert [d['message'] for d in record['diagnostics']] == ['ZeroDivisionError: division by zero (ERROR test_div)',
                                                             'AssertionError: 3 != 4 (FAIL test_add)']
    assert record['unbound'] == 1  # The library-only traceback; project loader.py and case.py stay untouched.
    imported = extract(IMPORT_FAILURE, 'calc.py', 'test_calc.py', 'loader.py', recipe='python-unittest', kind='test')
    assert rows(imported) == [('calc.py', 1, None, 'error', 'unittest', 'SyntaxError')]
    assert imported['diagnostics'][0]['message'] == "SyntaxError: expected ':' (ERROR test_calc)"


def test_pytest_location_lines_take_their_explanation_and_frame_headers_are_skipped():
    text = ('_________________________________ test_add _________________________________\n\n'
            '    def test_add():\n>       assert add(1, 2) == 4\nE       assert 3 == 4\nE        +  where 3 = add(1, 2)\n\n'
            'test_calc.py:5: AssertionError\n'
            '_________________________________ test_sub _________________________________\n'
            'test_calc.py:9: in test_sub\n    assert sub(1, 2) == 0\ncalc.py:4: in sub\n    raise ValueError("no")\n'
            'E   ValueError: no\n\ncalc.py:4: ValueError\n'
            '=========================== short test summary info ============================\n'
            'FAILED test_calc.py::test_add - assert 3 == 4\n')
    record = extract(text, 'calc.py', 'test_calc.py', recipe='python-pytest', kind='test')
    assert rows(record) == [('test_calc.py', 5, None, 'error', 'pytest', 'AssertionError'),
                            ('calc.py', 4, None, 'error', 'pytest', 'ValueError')]
    assert [d['message'] for d in record['diagnostics']] == ['AssertionError: assert 3 == 4', 'ValueError: ValueError: no']


def test_go_test_failure_lines():
    text = '=== RUN   TestAdd\n--- FAIL: TestAdd (0.00s)\n    add_test.go:7: bad\nFAIL\nFAIL\texample.com/fixture\t0.001s\n'
    record = extract(text, 'add_test.go', recipe='go-test', kind='test')
    assert rows(record) == [('add_test.go', 7, None, 'error', None, None)]
    assert record['diagnostics'][0]['message'] == 'bad'


def test_noise_and_paths_outside_the_snapshot_never_become_diagnostics():
    text = ('2026-10-13 12:30:45: started\n'
            'see http://localhost:8080: ok\n'
            'Note: nothing here\n'
            'src/app.py\n'
            '> demo@1.0.0 lint\n> eslint .\n'
            'npm error Lifecycle script `lint` failed with error:\nnpm error path /private/var/folders/zz/T/x/project\n'
            '../outside.py:3:1: E1 escapes the project\n'
            '/Users/someone/other/src/app.py:3:1: F401 a suffix match is not this project\n'
            'docs/missing.py:1:1: F401 not in the snapshot\n'
            '    at TestContext.<anonymous> (/private/var/folders/zz/T/pebble-test-abc/project/x.test.js:3:30)\n'
            "    location: '/private/var/folders/zz/T/pebble-test-abc/project/x.test.js:3:1'\n"
            '  File "src/app.py", line 3, in <module>\n'  # A frame with no exception line is not a diagnostic.
            'Ran 2 tests in 0.001s\n')
    record = extract(text, 'src/app.py', 'x.test.js', 'outside.py')
    assert record['diagnostics'] == [] and record['files'] == {}
    assert record['unbound'] == 4  # The timestamp looks like a location too; none of the four binds.


def test_records_are_bounded_errors_first_and_sanitized():
    lines = [f'src/a.py:{n}:1: W291 trailing whitespace' for n in range(1, 501)]
    lines += ['src/a.py:900:1: error: late error one', 'src/a.py:901:1: error: late error two',
              'src/a.py:901:1: error: late error two']  # The repeated line is a duplicate.
    lines += ['src/a.py:903:1: error: ' + 'x' * 5000]  # Over MAX_LINE: skipped.
    lines += ['src/a.py:904:1: error: \x1b]8;;file:///etc/passwd\x1b\\link\x1b]8;;\x1b\\ \x1b[31mred\x1b[0m‮​text\x07end '
              + 'y' * 400]
    record = extract('\n'.join(lines), 'src/a.py', exit_code=0)
    assert len(record['diagnostics']) == diagnostics.MAX_DIAGNOSTICS == 200
    assert record['counts'] == {'error': 3, 'warning': 197, 'info': 0} and record['omitted'] == 303
    errors = [d for d in record['diagnostics'] if d['severity'] == 'error']
    assert [d['line'] for d in errors] == [900, 901, 904]
    assert [d['line'] for d in record['diagnostics']] == sorted(d['line'] for d in record['diagnostics'])  # output order
    message = errors[-1]['message']
    assert len(message) == diagnostics.MAX_MESSAGE and message.endswith('…')
    assert message.startswith('link red text end ')
    assert not any(ord(c) < 32 or c in '‮​\x1b' for d in record['diagnostics'] for c in d['message'])
    over = extract('src/a.py:1:1: error: first\n' + 'z' * diagnostics.TEXT_LIMIT + '\nsrc/a.py:2:1: error: past the limit',
                   'src/a.py')
    assert [d['line'] for d in over['diagnostics']] == [1]


def test_adversarial_output_parses_in_bounded_time():
    shapes = [':' * 4000, 'a:' * 2000, ('x' * 50 + ':1:') * 70, ' --> ' + 'a' * 3000 + ':1:', '(' * 4000,
              'src/a.ts(' + '1,' * 1500, '  1:1  error  ' + 'x  ' * 1300, 'a:1:1 - error: ' + ' ' * 3900 + 'x',
              'a:1: error: ' + ' ' * 3900 + 'x', 'Sorry: E: ' + '(' * 3000 + ' (a, line 1', 'a-' * 2000,
              'E   ' + 'e' * 3000, '  File "' + 'f' * 1000 + '", line 1']
    text = '\n'.join(shapes * 10)[:diagnostics.TEXT_LIMIT]
    started = time.monotonic()
    record = extract(text, 'a', 'src/a.ts')
    assert time.monotonic() - started < 5
    assert record['omitted'] == 0 and len(record['diagnostics']) <= diagnostics.MAX_DIAGNOSTICS


# -- records, capture and commit ------------------------------------------------------------------------------------

def test_validate_refuses_every_malformed_record():
    record = extract('src/a.py:1:1: error: bad', 'src/a.py')
    assert diagnostics.validate(json.loads(json.dumps(record))) == record
    broken = [
        {**record, 'extra': 1},
        {**record, 'version': 2},
        {**record, 'authorization': 'granted'},
        {**record, 'recipe': '../npm'},
        {**record, 'recipe_kind': 'deploy'},
        {**record, 'files': {'src/a.py': 'not-a-hash'}},
        {**record, 'files': {'../a.py': sha('x')}},
        {**record, 'diagnostics': [{**record['diagnostics'][0], 'path': 'src/b.py'}]},
        {**record, 'diagnostics': [{**record['diagnostics'][0], 'severity': 'fatal'}]},
        {**record, 'diagnostics': [{**record['diagnostics'][0], 'line': 0}]},
        {**record, 'diagnostics': [{**record['diagnostics'][0], 'message': ''}]},
        {**record, 'diagnostics': [{**record['diagnostics'][0], 'tool': 'shell'}]},
        {**record, 'diagnostics': [{**record['diagnostics'][0], 'grant': True}]},
        {**record, 'counts': {'error': 2, 'warning': 0, 'info': 0}},
        {**record, 'omitted': -1},
        {**record, 'root_identity': [1]},
        {**record, 'diagnostics': [record['diagnostics'][0]] * 201},
    ]
    for value in broken:
        with pytest.raises(DiagnosticsError):
            diagnostics.validate(value)


def test_offer_is_a_no_op_without_a_capture_and_never_raises_with_one(tmp_path):
    ws = Workspace(tmp_path)
    plan = {'recipe': 'npm-lint', 'kind': 'lint'}
    assert diagnostics.offer(plan, 1, b'a.py:1:1: error: x', tmp_path, {'a.py': sha('x')}, ws) is None
    with diagnostics.capture() as box:
        assert diagnostics.offer(plan, 1, b'a.py:1:1: error: x', tmp_path, {'a.py': 'bad hash'}, ws) is None
    assert box.record is None and box.error == 'DiagnosticsError'
    notes = []
    runs = SimpleNamespace(note=lambda rid, kind, payload: notes.append((rid, kind, payload)))
    assert diagnostics.commit(runs, 'run', box, 'action') is False
    assert notes == [('run', 'recipe_diagnostics_failed', {'type': 'DiagnosticsError'})]
    failing = SimpleNamespace(note=lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    assert diagnostics.commit(failing, 'run', box, 'action') is False  # Advisory data never fails the action.
    assert diagnostics.commit(runs, None, box, 'action') is False


def test_captures_are_isolated_per_thread_and_nested_captures_take_the_inner_run(tmp_path):
    (tmp_path / 'a.py').write_text('x')
    ws = Workspace(tmp_path)
    plan = {'recipe': 'npm-lint', 'kind': 'lint'}
    seen = []

    def other_thread():
        seen.append(diagnostics.offer(plan, 1, b'a.py:1:1: error: x', tmp_path, {'a.py': sha('x')}, ws))

    with diagnostics.capture() as outer:
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        with diagnostics.capture() as inner:
            diagnostics.offer(plan, 1, b'a.py:1:1: error: x', tmp_path, {'a.py': sha('x')}, ws)
    assert seen == [None] and outer.record is None
    assert rows(inner.record) == [('a.py', 1, 1, 'error', None, None)]
    assert inner.record['root'] == str(ws.root) and inner.record['root_identity'] == list(ws.identity)


def test_a_file_the_recipe_changed_or_replaced_in_its_snapshot_is_dropped(tmp_path):
    snapshot = tmp_path / 'snap' / 'project'
    (snapshot / 'src').mkdir(parents=True)
    for name in ('kept.py', 'fixed.py', 'linked.py'):
        (snapshot / 'src' / name).write_text(f'{name}\n')
    hashes = {f'src/{n}': sha(f'{n}\n') for n in ('kept.py', 'fixed.py', 'linked.py')}
    (snapshot / 'src' / 'fixed.py').write_text('rewritten by --fix\n')
    secret = tmp_path / 'secret.txt'
    secret.write_text('linked.py\n')  # Same bytes, but a link is never followed.
    (snapshot / 'src' / 'linked.py').unlink()
    (snapshot / 'src' / 'linked.py').symlink_to(secret)
    output = ''.join(f'{snapshot.resolve()}/src/{n}:1:1: error: problem\n' for n in ('kept.py', 'fixed.py', 'linked.py'))
    with diagnostics.capture() as box:
        diagnostics.offer({'recipe': 'npm-lint', 'kind': 'lint'}, 1, output.encode(), snapshot.resolve(), hashes,
                          Workspace(tmp_path))
    assert rows(box.record) == [('src/kept.py', 1, 1, 'error', None, None)]
    assert box.record['dropped_after_run'] == 2 and box.record['files'] == {'src/kept.py': hashes['src/kept.py']}


# -- store: freshness, supersession, delivery and the completion rule ------------------------------------------------

APP = 'import os\n\n\ndef main():\n    return 1\n'
UTIL = 'def helper():\n    return 2\n'


@pytest.fixture
def host(tmp_path):
    project = tmp_path / 'project'
    (project / 'src').mkdir(parents=True)
    (project / 'src' / 'app.py').write_text(APP)
    (project / 'src' / 'util.py').write_text(UTIL)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    chat = app.new_chat(str(project))
    value = SimpleNamespace(app=app, project=project, chat=chat, cid=chat['id'], tmp=tmp_path, snaps=0)
    value.rid = new_run(value)
    yield value
    if not app.closed:
        app.active = app.active_run = None
        app.close()


def new_run(host, chat=None):
    chat = chat or host.chat
    return host.app.runs.create(chat['id'], 'Fix the lint errors.', host.app.runtime_config(chat, False), [])


def snapshot_of(host, ws):
    """The project plus staged drafts, copied as testing._collect copies them, with its file hashes."""
    host.snaps += 1
    snapshot = host.tmp / f'snap{host.snaps}' / 'project'
    hashes = {}
    contents = {name: (ws.root / name).read_bytes() for name in ws.files()}
    contents.update({name: draft['after'].encode() for name, draft in ws.drafts.items()})
    for name, data in contents.items():
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[name] = sha(data)
    return snapshot.resolve(), hashes


def record(host, output, recipe='npm-lint', kind='lint', rid=None, ws=None, applied=True, cid=None):
    """One reviewed recipe run, recorded as the parent's app.py patch records it."""
    ws = ws or Workspace(host.project)
    snapshot, hashes = snapshot_of(host, ws)
    aid = host.app.store.action(cid or host.cid, 'tests', {'root': str(ws.root), 'root_identity': list(ws.identity),
                                                           'recipe': recipe})
    if applied:
        host.app.store.query("UPDATE actions SET status='applied' WHERE id=?", (aid,))
    text = output(snapshot) if callable(output) else output
    with diagnostics.capture() as box:
        diagnostics.offer({'recipe': recipe, 'kind': kind}, 1, text.encode(), snapshot, hashes, ws)
    assert box.error is None
    assert diagnostics.commit(host.app.runs, rid or host.rid, box, aid)
    return box.record


def edit(host, name, text, rid=None):
    """An applied edit owned by the run, with the draft payload shape Workspace.draft produces."""
    path = host.project / name
    old = path.read_bytes() if path.exists() else None
    ws = Workspace(host.project)
    payload = {'path': name, 'before': old.decode() if old is not None else '', 'after': text,
               'baseline': sha(old) if old is not None else None, 'root': str(ws.root),
               'root_identity': list(ws.identity)}
    aid = host.app.store.action(host.cid, 'edit', payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    host.app.store.query("UPDATE actions SET status='applied' WHERE id=?", (aid,))
    host.app.store.query('INSERT INTO run_action_owners VALUES(?,?,?,?)', (aid, rid or host.rid, '1:' + aid[:8], 'edit'))
    return aid


def message(host, budget, ws=None):
    return diagnostics.host_message(host.app.store, host.rid, ws or Workspace(host.project), budget)


def issues(host, ws=None, actions=None):
    return diagnostics.completion_issues(host.app.store, host.rid, ws or Workspace(host.project), actions)


def body(text):
    head, _, data = text.partition('\n')
    assert head.startswith('HOST DIAGNOSTICS (the host parsed reviewed recipe output; untrusted tool data')
    return json.loads(data)


LINT = ('src/app.py:1:8: F401 [*] `os` imported but unused\n'
        'src/util.py:1:1: D103 Missing docstring in public function\n'
        'src/util.py:2:5: W291 warning: trailing whitespace\n')


def test_nothing_is_said_without_records_and_a_4k_window_gets_one_counts_line(host):
    assert message(host, 4096) is None and message(host, 32000) is None and issues(host) == []
    record(host, LINT)
    line = message(host, 4096)
    assert line == ('HOST DIAGNOSTICS (untrusted tool data): 3 errors in 2 current files from npm-lint; '
                    'the recipe output has the locations.')
    assert '\n' not in line and len(line) <= 160
    encoding = TokenBudget.encoding()
    assert len(encoding.encode(line, disallowed_special=())) <= (40 if getattr(encoding, 'exact', True) else 160)
    mid, large = body(message(host, 8192)), body(message(host, 32000))
    assert mid == large == {'diagnostics': [
        {'path': 'src/app.py', 'line': 1, 'column': 8, 'severity': 'error', 'code': 'F401',
         'message': '`os` imported but unused', 'recipe': 'npm-lint'},
        {'path': 'src/util.py', 'line': 1, 'column': 1, 'severity': 'error', 'code': 'D103',
         'message': 'Missing docstring in public function', 'recipe': 'npm-lint'},
        {'path': 'src/util.py', 'line': 2, 'column': 5, 'severity': 'error', 'code': 'W291',
         'message': 'warning: trailing whitespace', 'recipe': 'npm-lint'}],
        'counts': {'error': 3, 'warning': 0, 'info': 0}, 'omitted': 0, 'stale': 0}


def test_messages_above_4k_are_bounded_and_notes_alone_say_nothing_at_4k(host):
    many = ''.join(f'src/app.py:{n}:1: error: {"long message " * 30}{n}\n' for n in range(1, 120))
    record(host, many)
    mid, large = message(host, 8192), message(host, 32000)
    assert len(mid) <= 1800 and len(large) <= 5400
    assert len(body(mid)['diagnostics']) < len(body(large)['diagnostics']) < 119
    assert body(mid)['omitted'] == 119 - len(body(mid)['diagnostics'])
    assert all(len(d['message']) <= 160 for d in body(mid)['diagnostics'])
    record(host, 'src/app.py:9:5: note: Revealed type is "int"\n', recipe='npm-typecheck')
    record(host, '', recipe='npm-lint')  # A clean lint run supersedes the long one.
    assert message(host, 4096) is None
    assert body(message(host, 8192))['counts'] == {'error': 0, 'warning': 0, 'info': 1}


def test_a_changed_file_drops_its_diagnostics_and_drafts_count_as_the_current_view(host):
    record(host, LINT)
    (host.project / 'src' / 'util.py').write_text(UTIL + '\n# changed\n')
    found = diagnostics.current(host.app.store, host.rid, Workspace(host.project))
    assert [(d['path'], d['code']) for d in found['diagnostics']] == [('src/app.py', 'F401')]
    assert found['stale'] == 2 and found['files'] == 1
    assert body(message(host, 8192))['stale'] == 2
    # A staged draft is in the snapshot; while it is the current view its diagnostics are current.
    ws = Workspace(host.project)
    ws.draft('src/app.py', 'import sys\n')
    record(host, 'src/app.py:1:8: F401 [*] `sys` imported but unused\n', ws=ws, recipe='npm-typecheck')
    assert ('src/app.py', 'F401', 'npm-typecheck') in {
        (d['path'], d['code'], d['recipe']) for d in diagnostics.current(host.app.store, host.rid, ws)['diagnostics']}
    # Read from disk without the draft, the same file no longer matches the typecheck record.
    plain = diagnostics.current(host.app.store, host.rid, Workspace(host.project))['diagnostics']
    assert ('npm-typecheck' not in {d['recipe'] for d in plain}) and any(d['recipe'] == 'npm-lint' for d in plain)
    # When the disk changes under the draft, the draft is no longer the view: stale.
    (host.project / 'src' / 'app.py').write_text('changed underneath\n')
    assert all(d['path'] != 'src/app.py' for d in diagnostics.current(host.app.store, host.rid, ws)['diagnostics'])


def test_a_test_result_is_stale_after_any_later_edit_while_lint_stays_bound_per_file(host):
    (host.project / 'test_app.py').write_text('import unittest\n\n\nclass T(unittest.TestCase):\n    def test_main(self):\n'
                                              '        self.assertEqual(1, 2)\n')
    failing = ('=' * 70 + '\nFAIL: test_main (test_app.T.test_main)\n' + '-' * 70 + '\nTraceback (most recent call last):\n'
               '  File "{root}/test_app.py", line 6, in test_main\n    self.assertEqual(1, 2)\nAssertionError: 1 != 2\n\n'
               + '-' * 70 + '\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n')
    record(host, lambda snapshot: failing.format(root=snapshot), recipe='python-unittest', kind='test')
    record(host, 'src/util.py:1:1: D103 Missing docstring in public function\n')
    found = diagnostics.current(host.app.store, host.rid, Workspace(host.project))
    assert sorted((d['recipe'], d['path'], d['line']) for d in found['diagnostics']) == [
        ('npm-lint', 'src/util.py', 1), ('python-unittest', 'test_app.py', 6)]
    # The failing assertion names test_app.py, but the fix is usually elsewhere: any later edit retires the result.
    edit(host, 'src/app.py', APP + '\n')
    found = diagnostics.current(host.app.store, host.rid, Workspace(host.project))
    assert [(d['recipe'], d['path']) for d in found['diagnostics']] == [('npm-lint', 'src/util.py')]
    assert found['stale'] == 1


def test_the_latest_run_of_each_recipe_supersedes_and_other_recipes_remain(host):
    record(host, LINT)
    record(host, 'src/util.py:1:5: error: Function is missing a return type annotation  [no-untyped-def]\n',
           recipe='npm-typecheck')
    record(host, 'src/util.py:1:1: D103 Missing docstring in public function\n')
    found = diagnostics.current(host.app.store, host.rid, Workspace(host.project))
    assert sorted((d['recipe'], d['code']) for d in found['diagnostics']) == [('npm-lint', 'D103'),
                                                                               ('npm-typecheck', 'no-untyped-def')]
    assert found['recipes'] == ['npm-lint', 'npm-typecheck']


def test_unapplied_foreign_root_and_malformed_records_are_ignored(host, tmp_path):
    record(host, LINT, applied=False)
    other = tmp_path / 'other'
    (other / 'src').mkdir(parents=True)
    (other / 'src' / 'app.py').write_text(APP)
    foreign = Workspace(other)
    record(host, LINT, ws=foreign)  # A child worktree or another folder: never this project's evidence.
    host.app.runs.note(host.rid, diagnostics.EVENT, {'kind': diagnostics.EVENT, 'diagnostics': 'forged'})
    host.app.store.query('INSERT INTO run_events VALUES(?,?,?,?,?)', (host.rid, 999, diagnostics.EVENT, 'not json', 0))
    assert message(host, 32000) is None and issues(host) == []
    # The same folder replaced by a new one at the same path is a different root identity.
    record(host, LINT)
    moved = host.tmp / 'moved'
    host.project.rename(moved)
    host.project.mkdir()
    (host.project / 'src').mkdir()
    (host.project / 'src' / 'app.py').write_text(APP)
    assert message(host, 32000) is None


def test_new_error_diagnostics_on_lines_this_run_changed_are_one_repairable_issue(host):
    edit(host, 'src/app.py', APP.replace('return 1', 'return undefined_name'))
    record(host, 'src/app.py:1:8: F401 [*] `os` imported but unused\n'
                 'src/app.py:5:12: F821 Undefined name `undefined_name`\n'
                 'src/app.py:5:1: W291 warning: trailing whitespace\n'
                 'src/util.py:1:1: D103 Missing docstring in public function\n', recipe='npm-lint')
    record(host, 'src/app.py:5:12: warning: unused expression\n', recipe='npm-typecheck')
    expected = ['2 new error diagnostics from npm-lint in 1 file edited in this run. Fix them and run the recipe '
                'again, or state plainly why they remain.']
    # F401 sits on an unchanged line with no earlier report: not attributed to this run. util.py was not edited.
    assert issues(host) == expected
    actions = [a for a in host.app.store.actions(host.cid)]
    assert issues(host, actions=actions) == expected
    listed = body(message(host, 32000))['diagnostics']
    assert [d.get('edited') for d in listed] == [True, True, True, True, None]  # This run's files come first.


def test_a_report_from_before_the_edit_marks_existing_diagnostics_as_not_new(host):
    record(host, 'src/app.py:1:8: F401 [*] `os` imported but unused\n')
    edit(host, 'src/app.py', '"""Module."""\n' + APP)
    record(host, 'src/app.py:2:8: F401 [*] `os` imported but unused\n')
    assert issues(host) == []  # The same finding moved one line down.
    edit(host, 'src/app.py', '"""Module."""\nimport sys\n' + APP)
    record(host, 'src/app.py:3:8: F401 [*] `os` imported but unused\nsrc/app.py:2:8: F401 [*] `sys` imported but unused\n')
    assert issues(host) == ['1 new error diagnostic from npm-lint in 1 file edited in this run. Fix them and run the '
                            'recipe again, or state plainly why they remain.']


def settled_run(host, output, chat=None):
    """A completed run with one recorded recipe run; the service owns one live run at a time."""
    host.app.runs.transition(host.rid, 'paused')
    chat = chat or host.chat
    rid = new_run(host, chat)
    record(host, output, rid=rid, cid=chat['id'])
    host.app.runs.transition(rid, 'completed')
    host.app.runs.transition(host.rid, 'running')
    return rid


def test_a_baseline_from_an_earlier_run_of_this_conversation_counts_but_not_from_another(host):
    settled_run(host, 'src/app.py:5:5: E999 SyntaxError: pre-existing\n')
    edit(host, 'src/app.py', APP.replace('return 1', 'return 1  '))
    record(host, 'src/app.py:5:5: E999 SyntaxError: pre-existing\n')
    assert issues(host) == []
    settled_run(host, 'src/util.py:2:5: E999 SyntaxError: elsewhere\n', chat=host.app.new_chat(str(host.project)))
    edit(host, 'src/util.py', UTIL.replace('return 2', 'return 3'))
    record(host, 'src/util.py:2:5: E999 SyntaxError: elsewhere\n')
    assert issues(host) == ['1 new error diagnostic from npm-lint in 1 file edited in this run. Fix them and run the '
                            'recipe again, or state plainly why they remain.']


def test_created_files_count_every_error_and_stale_or_warning_diagnostics_never_hold_completion(host):
    edit(host, 'src/new.py', 'import os\n\nVALUE = 1\n')
    record(host, 'src/new.py:1:8: F401 [*] `os` imported but unused\nsrc/new.py:3:1: warning: style only\n')
    assert issues(host) == ['1 new error diagnostic from npm-lint in 1 file edited in this run. Fix them and run the '
                            'recipe again, or state plainly why they remain.']
    edit(host, 'src/new.py', 'VALUE = 1\n')  # Edited after the lint run: the report is stale, not a failure.
    assert issues(host) == []


def test_measured_message_sizes_and_parse_times(host):
    """The effects reported in the track note (printed; run with -rP). Bounds only are asserted."""
    record(host, LINT)
    encoding = TokenBudget.encoding()
    line, mid, large = message(host, 4096), message(host, 8192), message(host, 32000)
    eslint = ''.join(f'{SNAP}/src/f{n}.js\n' + ''.join(f'  {k}:1  error  Unexpected var  no-var\n' for k in range(1, 11))
                     + '\n' for n in range(20))
    started = time.perf_counter()
    typical = extract(eslint, *[f'src/f{n}.js' for n in range(20)])
    typical_seconds = time.perf_counter() - started
    worst_text = '\n'.join(['a:1:1 - error: ' + ' ' * 3900 + 'x', 'src/a.ts(' + '1,' * 1500, ':' * 4000] * 40)
    started = time.perf_counter()
    extract(worst_text[:diagnostics.TEXT_LIMIT], 'a', 'src/a.ts')
    worst_seconds = time.perf_counter() - started
    measured = {'tokenizer': getattr(encoding, 'name', 'o200k_base'),
                'small_line_tokens': len(encoding.encode(line, disallowed_special=())), 'small_line_chars': len(line),
                'mid_chars': len(mid), 'mid_tokens': len(encoding.encode(mid, disallowed_special=())),
                'large_chars': len(large), 'typical_diagnostics': len(typical['diagnostics']),
                'typical_parse_ms': round(typical_seconds * 1000, 2), 'worst_parse_ms': round(worst_seconds * 1000, 1),
                'worst_bytes': len(worst_text[:diagnostics.TEXT_LIMIT])}
    print('G20-DIAG-MEASURE ' + json.dumps(measured, sort_keys=True))
    assert measured['typical_diagnostics'] == 200 and typical['omitted'] == 0
    assert measured['small_line_chars'] <= 160 and measured['typical_parse_ms'] < 1000 and measured['worst_parse_ms'] < 5000


def test_current_view_checks_are_bounded_per_boundary(host):
    for n in range(diagnostics.CHECK_FILES + 6):
        (host.project / 'src' / f'm{n:03}.py').write_text(f'x = {n}\n')
    record(host, ''.join(f'src/m{n:03}.py:1:1: error: e\n' for n in range(diagnostics.CHECK_FILES + 6)))
    found = diagnostics.current(host.app.store, host.rid, Workspace(host.project))
    assert len(found['diagnostics']) == diagnostics.CHECK_FILES and found['unchecked'] == 6


# -- real recipes in the macOS sandbox, through the parent's patches (simulated with their exact code) ---------------

@pytest.fixture
def offered(monkeypatch):
    """P1 (testing.py): offer the complete output, the snapshot and the pre-launch hashes; the result is unchanged."""
    real = testing._launch
    state = SimpleNamespace(ws=None, offers=0)

    def launch(plan, profile, snapshot, env, output, cancel):
        hashes = {}
        for base, folders, files in os.walk(snapshot):
            for name in files:
                path = os.path.join(base, name)
                if not os.path.islink(path):
                    with open(path, 'rb') as handle:
                        hashes[os.path.relpath(path, snapshot)] = sha(handle.read())
        code, data, size = real(plan, profile, snapshot, env, output, cancel)
        state.offers += 1
        diagnostics.offer(plan, code, data, snapshot, hashes, state.ws)
        return code, data, size

    monkeypatch.setattr(testing, '_launch', launch)
    return state


def run_recipe(root, recipe, offered):
    ws = Workspace(root)
    offered.ws = ws
    payload = {'root': str(ws.root), 'root_identity': list(ws.identity), 'recipe': recipe, 'drafts': [],
               'plan': testing.recipes.resolve(recipe, ws)}
    with diagnostics.capture() as box:
        result = testing.run_tests(payload, threading.Event())
    assert box.error is None and offered.offers == 1
    return result, box.record


def project(tmp_path, files, name='project'):
    root = tmp_path / name
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    return root


@macos
def test_a_real_unittest_run_binds_a_syntax_error_and_leaves_the_tool_result_unchanged(tmp_path, offered):
    root = project(tmp_path, {'calc.py': 'def add(a, b)\n    return a + b\n',
                              'test_calc.py': 'import unittest\n\nfrom calc import add\n\n\nclass AddTest(unittest.TestCase):\n'
                                              '    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n'})
    result, found = run_recipe(root, 'python-unittest', offered)
    assert set(result) == RESULT_KEYS and result['outcome'] == 'failed', result['output']
    assert 'diagnostic' not in json.dumps(result)
    assert rows(found) == [('calc.py', 1, None, 'error', 'unittest', 'SyntaxError')], result['output']
    assert found['files'] == {'calc.py': sha((root / 'calc.py').read_bytes())}
    assert found['root'] == str(Workspace(root).root) and found['exit_code'] == result['exit_code']


@macos
def test_a_real_unittest_failure_points_at_the_assertion(tmp_path, offered):
    root = project(tmp_path, {'calc.py': 'def add(a, b):\n    return a - b\n',
                              'test_calc.py': 'import unittest\n\nfrom calc import add\n\n\nclass AddTest(unittest.TestCase):\n'
                                              '    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n'})
    result, found = run_recipe(root, 'python-unittest', offered)
    assert result['outcome'] == 'failed' and result['tests']['failed'] == 1
    assert rows(found) == [('test_calc.py', 8, None, 'error', 'unittest', 'AssertionError')], result['output']
    assert found['diagnostics'][0]['message'] == 'AssertionError: -1 != 3 (FAIL test_add)'


LINT_JS = r"""
const fs = require('fs');
const path = require('path');
if (process.argv.includes('--fix')) {
  const target = path.join('src', 'app.js');
  fs.writeFileSync(target, fs.readFileSync(target, 'utf8').replace('var ', 'let '));
}
const out = [];
let count = 0;
for (const name of ['src/app.js', 'src/other.js']) {
  const file = path.join(process.cwd(), name);
  if (!fs.existsSync(file)) continue;
  const found = [];
  fs.readFileSync(file, 'utf8').split('\n').forEach((text, i) => {
    const at = text.indexOf('var ');
    if (at >= 0) found.push(`  ${i + 1}:${at + 1}  error  Unexpected var, use let or const instead  no-var`);
  });
  if (found.length) { out.push(file, ...found, ''); count += found.length; }
}
out.push('/opt/elsewhere/vendor.js', '  1:1  error  Outside the project  no-undef', '');
console.log(out.join('\n'));
console.log(`✖ ${count + 1} problems (${count + 1} errors, 0 warnings)`);
process.exit(count ? 1 : 0);
"""


@macos
def test_a_real_npm_lint_run_binds_eslint_blocks_and_drops_files_its_fix_mode_rewrote(tmp_path, offered):
    files = {'package.json': json.dumps({'name': 'demo', 'version': '1.0.0', 'scripts': {'lint': 'node lint.js'}}),
             'lint.js': LINT_JS, 'src/app.js': 'var a = 1;\nvar b = 2;\n', 'src/other.js': 'let ok = 1;\nvar no = 2;\n'}
    root = project(tmp_path, files)
    result, found = run_recipe(root, 'npm-lint', offered)
    assert set(result) == RESULT_KEYS and result['outcome'] == 'failed' and result['kind'] == 'lint'
    assert rows(found) == [('src/app.js', 1, 1, 'error', 'eslint', 'no-var'), ('src/app.js', 2, 1, 'error', 'eslint', 'no-var'),
                           ('src/other.js', 2, 1, 'error', 'eslint', 'no-var')], result['output']
    assert found['unbound'] == 1 and found['dropped_after_run'] == 0
    fixing = project(tmp_path, {**files, 'package.json': json.dumps({'name': 'demo', 'version': '1.0.0',
                                                                     'scripts': {'lint': 'node lint.js --fix'}})}, 'fixing')
    offered.offers = 0
    result, found = run_recipe(fixing, 'npm-lint', offered)
    assert rows(found) == [('src/other.js', 2, 1, 'error', 'eslint', 'no-var')], result['output']
    assert found['dropped_after_run'] == 1  # app.js line 2 is reported against content the project does not have.


@macos
def test_end_to_end_boundaries_see_current_diagnostics_and_completion_counts_only_new_ones(tmp_path, monkeypatch):
    # Merge (i20): parent patches P1-P4 landed, so this run goes through the real testing.py offer, the real
    # app.py capture and commit, the runner's HOST DIAGNOSTICS message and the controller's completion check.
    root = project(tmp_path, {
        'package.json': json.dumps({'name': 'demo', 'version': '1.0.0', 'scripts': {'lint': 'node lint.js'}}),
        'lint.js': LINT_JS, 'src/app.js': 'var legacy = 1;\nconsole.log(legacy);\n'})
    changed = 'var legacy = 1;\nconsole.log(legacy);\nvar added = 2;\nconsole.log(added);\n'
    script = [('Read', {'file_path': 'src/app.js'}), ('RunRecipe', {'recipe': 'npm-lint'}),
              ('Write', {'file_path': 'src/app.js', 'content': changed}), ('RunRecipe', {'recipe': 'npm-lint'})]
    seen = SimpleNamespace(requests=[], boundary=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        seen.requests.append(copy.deepcopy(messages))
        ws = Workspace(root)
        # What the runner patch (P3) would place at this boundary, for a 4K and a large window.
        seen.boundary.append((diagnostics.host_message(app.store, app.active_run, ws, 4096),
                              diagnostics.host_message(app.store, app.active_run, ws, 32000)))
        step = len(seen.requests) - 1
        if step < len(script):
            name, args = script[step]
            return {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': f'c{step}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
        token('Done.')
        return {'role': 'assistant', 'content': 'Done.'}

    monkeypatch.setattr(Providers, 'complete', complete)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        app.setting('permissions', {'mode': 'auto', 'tools': dict(DEFAULT['tools'])})
        chat = app.new_chat(str(root))
        app.work(chat, 'Fix the lint errors in src/app.js and add a second counter.', False, [])
        rid = app.runs.list(chat['id'])[0]['id']
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (
                app.active is None and len(seen.requests) >= 5 and app.runs.get(rid)['state'] not in ('queued', 'running', 'retry_wait')):
            time.sleep(.05)
        assert len(seen.requests) >= 5, app.runs.get(rid)
        lint_runs = [a for a in app.store.actions(chat['id']) if a['kind'] == 'tests']
        assert [a['status'] for a in lint_runs] == ['applied', 'applied']
        assert all(set(a['payload']['result']) == RESULT_KEYS for a in lint_runs)  # The tool result is unchanged.
        # Boundary 3 follows the first lint run, boundary 4 the edit (stale) and boundary 5 the second run.
        assert seen.boundary[0] == seen.boundary[1] == (None, None)
        first, after_edit, second = seen.boundary[2], seen.boundary[3], seen.boundary[4]
        assert first[0] == ('HOST DIAGNOSTICS (untrusted tool data): 1 error in 1 current file from npm-lint; '
                            'the recipe output has the locations.')
        assert [(d['path'], d['line'], d['code'], d.get('edited')) for d in body(first[1])['diagnostics']] == [
            ('src/app.js', 1, 'no-var', None)]
        assert after_edit == (None, None)
        assert second[0] == ('HOST DIAGNOSTICS (untrusted tool data): 2 errors in 1 current file from npm-lint; '
                             'the recipe output has the locations.')
        assert [(d['path'], d['line'], d['code'], d.get('edited')) for d in body(second[1])['diagnostics']] == [
            ('src/app.js', 1, 'no-var', True), ('src/app.js', 3, 'no-var', True)]
        for request, (small, _) in zip(seen.requests, seen.boundary):
            shown = [m['content'] for m in request if m.get('name') == diagnostics.MESSAGE_NAME]
            # Merge (i20): the runner patch (P3) landed, so each boundary carries exactly its text (a 4K local route).
            assert shown == ([small] if small else [])
        carried = [i for i, request in enumerate(seen.requests) if 'HOST DIAGNOSTICS' in json.dumps(request)]
        # Requests 3 and 5 follow the lint runs. Merge (i20): with the controller patch (P4) the new error holds
        # completion, so the runner's two repair turns (requests 6 and 7) still carry the current line.
        assert len(seen.requests) == 7 and carried == [2, 4, 5, 6]
        # Line 1 was reported before the edit; only the added line 3 is new.
        assert diagnostics.completion_issues(app.store, rid, Workspace(root)) == [
            '1 new error diagnostic from npm-lint in 1 file edited in this run. Fix them and run the recipe again, '
            'or state plainly why they remain.']
        # Merge (i20): the controller patch (P4) adds that one counts-only line to the completion issues.
        assert any('new error diagnostic' in issue for issue in app.controller.completion(rid, Workspace(root), 'Done.')['issues'])
    finally:
        if not app.closed:
            app.active = app.active_run = None
            app.close()
