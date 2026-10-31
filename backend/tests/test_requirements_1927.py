"""Phase 46 (1.9.27): the requirement ledger's gaps behind the golden held-out coding_fix format_only cells and the
long_horizon false completions, reproduced offline on the golden DEV prompts' shapes (never the sealed split).

- A test run through the terminal writes tests/__pycache__ and .pytest_cache into the project (terminal.py sets no
  PYTHONDONTWRITEBYTECODE); "leave the tests as they are" then read as "this run changed the tests", the gate held a
  right fix and the reply turned to "FINAL: blocked" (format_only). Caches are never the files a request means.
- "Fix the off-by-one in pager/page.py and leave the rest exactly as it is" made pager/page.py itself "unchanged".
- The golden-output sentence was a requirement in one shape only ("The output of `CMD` must stay byte-identical to
  PATH"); dev d-lh-01's phrasing 3 ("The invoice printed by `CMD` has to stay byte-for-byte the same as PATH") and
  private d-lh-91's turns ("must still match PATH byte for byte", "prints exactly what it prints now") were not, so
  the host checked nothing and the model heard of no check up front.
"""

import pytest

from pebble import requirements as rq

GW = ("I've fixed the empty-input bug in app/parse.py but haven't committed it. My changes in app/experimental.py and "
      "notes.txt aren't ready: leave them uncommitted, exactly as they are.")
LH1 = ('Migrate the shop package. The output of `python3 -m shop.cli samples/order1.json` must stay byte-identical to '
       'samples/invoice1.txt, and the CSV export must not change. Make the whole suite pass.')
LH3 = ('The invoice printed by `python3 -m shop.cli samples/order1.json` has to stay byte-for-byte the same as '
       "samples/invoice1.txt, and the CSV export can't change its columns or number format.")
LH91 = ('In this inventory project, add a method `low_stock(threshold=5)` to Store. The report printed by '
        '`python3 -m inventory.cli data/stock.json` must stay byte-identical to samples/report.txt.')


def project(tmp_path):
    root = tmp_path / 'p'
    (root / 'tests').mkdir(parents=True)
    (root / 'pkg').mkdir()
    (root / 'tests' / 'test_a.py').write_text('def test_a():\n    assert True\n')
    (root / 'pkg' / 'mod.py').write_text('x = 1\n')
    return root


def edit(created=10, path='pkg/mod.py'):
    return {'kind': 'edit', 'status': 'applied', 'created': created, 'payload': {'path': path}}


def test_caches_written_by_a_test_run_are_not_changed_tests(tmp_path):
    root = project(tmp_path)
    goal = 'Fix the bug in pkg/mod.py. Leave the tests as they are. End your reply with the line `FINAL: done`.'
    found = rq.extract(goal)
    assert found == [{'kind': 'unchanged', 'scope': 'tests', 'paths': []}]
    base = rq.baseline(root, found)
    (root / 'tests' / '__pycache__').mkdir()
    (root / 'tests' / '__pycache__' / 'test_a.cpython-312-pytest-8.pyc').write_bytes(b'\x00pyc')
    (root / 'tests' / '.pytest_cache').mkdir()
    (root / 'tests' / '.pytest_cache' / 'v').write_text('x')
    (root / '.pytest_cache').mkdir()
    (root / '.pytest_cache' / 'README.md').write_text('cache')
    (root / 'tests' / '.DS_Store').write_bytes(b'\0')
    assert rq.issues(found, base, root=root, actions=[edit()], answer='Done.\n\nFINAL: done', reads=[],
                     test_evidence=True) == []
    # a real change to a test is still caught, and named without the caches beside it
    (root / 'tests' / 'test_a.py').write_text('def test_a():\n    assert False\n')
    issues = rq.issues(found, base, root=root, actions=[edit()], answer='x', reads=[], test_evidence=True)
    assert len(issues) == 1 and 'tests/test_a.py' in issues[0][0] and '__pycache__' not in issues[0][0]


def test_a_named_folder_kept_unchanged_ignores_its_caches_too(tmp_path):
    root = project(tmp_path)
    found = rq.extract("Don't touch pkg/ at all; leave pkg/ exactly as it is.")
    assert found and found[0]['kind'] == 'unchanged' and found[0]['paths'] == ['pkg/']
    base = rq.baseline(root, found)
    (root / 'pkg' / '__pycache__').mkdir()
    (root / 'pkg' / '__pycache__' / 'mod.cpython-312.pyc').write_bytes(b'\x00')
    assert rq.issues(found, base, root=root, actions=[edit(path='other.py')], answer='x', reads=[], test_evidence=True) == []
    assert all(not rq.ignored(rel) for rel in base), base


@pytest.mark.parametrize('sentence', [
    'Fix the off-by-one in pager/page.py and leave the rest of the module exactly as it is.',
    'Update invoicing/rounding.py so the parts add up, and keep everything else exactly as it is.',
    'Correct the week number in weekly/dates.py; leave the rest of the package unchanged.',
])
def test_the_file_a_sentence_asks_to_fix_is_never_its_own_unchanged_requirement(sentence):
    assert [f for f in rq.extract(sentence) if f['kind'] == 'unchanged'] == []


def test_the_files_a_sentence_asks_to_leave_alone_still_are():
    assert rq.extract(GW) == [{'kind': 'unchanged', 'scope': 'named', 'paths': ['app/experimental.py', 'notes.txt']}]
    both = rq.extract('Fix the bug in app/parse.py. Leave app/experimental.py and notes.txt exactly as they are.')
    assert both == [{'kind': 'unchanged', 'scope': 'named', 'paths': ['app/experimental.py', 'notes.txt']}]


@pytest.mark.parametrize('text, command, path', [
    (LH1, 'python3 -m shop.cli samples/order1.json', 'samples/invoice1.txt'),
    (LH3, 'python3 -m shop.cli samples/order1.json', 'samples/invoice1.txt'),
    (LH91, 'python3 -m inventory.cli data/stock.json', 'samples/report.txt'),
    ('`python3 -m shop.cli samples/order1.json` must still print exactly what samples/invoice1.txt holds.',
     'python3 -m shop.cli samples/order1.json', 'samples/invoice1.txt'),
    ('The text produced by `make report` must remain identical to docs/expected.txt.', 'make report', 'docs/expected.txt'),
])
def test_the_golden_output_sentence_is_a_requirement_in_each_of_its_shapes(text, command, path):
    found = [f for f in rq.extract(text) if f['kind'] == 'output_equals']
    assert len(found) == 1 and found[0]['command'] == command and found[0]['path'] == path
    note = rq.checks_note(rq.extract(text))
    assert note and f'run `{command}` with RunCommand after your last change' in note


def test_a_golden_named_without_its_command_is_kept_for_binding_only(tmp_path):
    found = rq.extract('Before I merge, the full text report for data/stock.json must still match samples/report.txt byte for byte.')
    assert found == [{'kind': 'golden', 'path': 'samples/report.txt'}]
    assert rq.checks_note(found) is None
    root = project(tmp_path)
    assert rq.baseline(root, found) == {}
    assert rq.issues(found, {}, root=root, actions=[edit()], answer='FINAL: done', reads=[], test_evidence=True) == []


def test_an_earlier_turns_golden_is_carried_into_a_turn_that_keeps_it_alive():
    earlier = [rq.extract(LH91)]
    command = earlier[0][0]
    for text in ['Prices are floats today. Switch them to integer cents. The text report must print exactly what it prints now.',
                 'Add a `--json` flag to the CLI. Without the flag the output stays as it is. End with `FINAL: done`.',
                 'Before I merge: the whole suite must pass, and the full text report for data/stock.json must still '
                 'match samples/report.txt byte for byte.',
                 'Keep samples/report.txt output byte-identical.']:
        carried = rq.carried(earlier, text)
        assert carried == [{**command, 'carried': True}], text
    # a turn that says nothing about the output carries nothing: it may change the output on purpose
    assert rq.carried(earlier, 'Rename Store.remove to Store.take everywhere. End with `FINAL: done`.') == []
    assert rq.carried(earlier, 'Change the report to list prices in euros.') == []
    # a turn that restates the check itself needs no carry, and the newest command for a golden wins
    assert rq.carried(earlier, LH91) == []
    newer = [earlier[0], rq.extract('The report printed by `python3 -m inventory.cli --full data/stock.json` must stay '
                                    'byte-identical to samples/report.txt.')]
    assert rq.carried(newer, 'The output stays as it is.')[0]['command'] == 'python3 -m inventory.cli --full data/stock.json'
    assert rq.carried([], 'The output stays as it is.') == []
