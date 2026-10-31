"""Phase 46 (1.9.28, golden held-out workshop_repeat: 2 sol false completions; b-new C1, behind PEBBLE_1928_COPY_CHECK):
a RunCode script that types a file's lines in by hand, with a slip, is refused once before it runs.

Measured on every recorded RunCode script in the dev runs (351 scripts, 152 cells, all families; 58 hold hand copies of
a file the turn read): 0 flagged, also against every file the cell read and without the make_tool exclusion; planted
one-digit and swapped-character slips in the d-wr-01 and d-wr-91 batches were caught 480 of 480, as strings and lists.
"""
import json
import sys
import time

import pytest

from pebble import copy_check
from test_code_mode_37 import Scripted, code, outcome, v34

macos = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox')
BATCH = ('978-0-306-40615-7\n9780306406158\n979-10-90636-07-1\n978 3 16 148410 0\n9781861972712\n'
         '978-1-4028-9462-6\n')
LINES = BATCH.splitlines()
SLIP = [*LINES[:3], LINES[3][:-1] + '9', *LINES[4:]]  # one digit of line 4 typed wrong


def as_string(lines):
    return 'batch = """' + '\n'.join(lines) + '"""\nprint(len(batch.splitlines()))\n'


def as_list(lines):
    return 'batch = [' + ', '.join(repr(line) for line in lines) + ']\nprint(len(batch))\n'


@pytest.mark.parametrize('shape', [as_string, as_list])
def test_a_slip_in_a_hand_typed_copy_is_found_with_both_lines(shape):
    assert copy_check.slips(shape(SLIP), {'isbn/batch.txt': BATCH}) == [('isbn/batch.txt', SLIP[3], LINES[3])]
    assert copy_check.slips(shape(LINES), {'isbn/batch.txt': BATCH}) == []


def test_formatting_alone_and_unrelated_lists_are_never_a_slip():
    plain = [line.replace('-', '').replace(' ', '') for line in LINES]
    assert copy_check.slips(as_string(plain), {'isbn/batch.txt': BATCH}) == []
    assert copy_check.slips(as_list([line.upper() for line in LINES]), {'isbn/batch.txt': BATCH}) == []
    assert copy_check.slips(as_list(['alpha', 'beta', 'gamma', '978-0-306-40615-8']), {'isbn/batch.txt': BATCH}) == []


def test_a_dict_of_key_value_lines_is_compared_too():
    lengths = 'AT 20\nBE 16\nCH 21\nDE 22\n'
    assert copy_check.slips("sizes = {'AT': 20, 'BE': 16, 'CH': 21, 'DE': 22}", {'ibans/lengths.txt': lengths}) == []
    assert copy_check.slips("sizes = {'AT': 20, 'BE': 16, 'CH': 21, 'DE': 23}", {'ibans/lengths.txt': lengths}) == [
        ('ibans/lengths.txt', 'DE 23', 'DE 22')]


def test_a_tools_own_tests_and_a_script_that_edits_are_not_compared(monkeypatch):
    tests = ', '.join('{"inputs": {"line": %r}, "expected": %s}' % (line, n != 3) for n, line in enumerate(SLIP))
    inline = ('pebble.make_tool(name="isbn", purpose="p", code="def isbn(line):\\n    return True\\n", tests=[%s], '
              'inputs={"line": "str"})\n' % tests)
    named = 'cases = [%s]\npebble.make_tool(name="isbn", purpose="p", code="x", tests=cases, inputs={})\n' % tests
    listed = 'cases = %r\npebble.make_tool(name="isbn", purpose="p", code="x", tests=cases, inputs={})\n' % SLIP
    edit = 'new = """%s"""\npebble.write(file_path="isbn/batch.txt", content=new)\n' % '\n'.join(SLIP)
    for script in (inline, named, listed, edit):
        assert copy_check.slips(script, {'isbn/batch.txt': BATCH}) == []
    with monkeypatch.context() as m:  # the exclusion is what keeps a tool's near-copy tests out
        m.setattr(copy_check, '_skipped', lambda tree: set())
        assert copy_check.slips(listed, {'isbn/batch.txt': BATCH}) == [('isbn/batch.txt', SLIP[3], LINES[3])]
    # the made tool's own call with the data typed in is still a copy (sol's d-wr-01 shape)
    assert copy_check.slips(listed + 'print(pebble.mine.isbn(batch=%r))\n' % '\n'.join(SLIP),
                            {'isbn/batch.txt': BATCH}) == [('isbn/batch.txt', SLIP[3], LINES[3])]


# -- w12-bnew's security review (w12b) ------------------------------------------------------------------------------

def test_only_pebbles_own_write_edit_or_append_switches_the_check_off():
    """The review: any `.append(` or `.write(` (a list's, a file's) made the script count as an edit, so the same slip
    was caught without an append and missed with one."""
    for tail in ('valid = []\nfor n, line in enumerate(batch.splitlines(), 1):\n    valid.append(n)\nprint(valid)\n',
                 'import sys\nsys.stdout.write(batch)\n', 'out = open("/dev/null", "w")\nout.write(batch)\n'):
        assert copy_check.slips(as_string(SLIP) + tail, {'isbn/batch.txt': BATCH}) == [('isbn/batch.txt', SLIP[3], LINES[3])]
    for tail in ('pebble.append(file_path="isbn/out.txt", content=batch)\n',
                 'from pebble import write as w\nw(file_path="isbn/batch.txt", content=batch)\n',
                 'import pebble as pb\npb.edit(file_path="isbn/batch.txt", old="a", new="b")\n',
                 'p = pebble\np.write(file_path="isbn/batch.txt", content=batch)\n'):
        assert copy_check.slips(as_string(SLIP) + tail, {'isbn/batch.txt': BATCH}) == []


def test_a_value_beside_the_exact_copies_of_its_neighbours_is_not_a_slip():
    """The review's false refusal: asking whether 1.2.5 exists, beside 1.2.3 and 1.2.4 typed exactly, is no mistyped
    1.2.4; a line the block mistypes still is."""
    versions = '1.2.3\n1.2.4\n2.0.0\n'
    assert copy_check.slips("wanted = ['1.2.3', '1.2.4', '1.2.5']\nprint([v for v in wanted])", {'versions.txt': versions}) == []
    assert copy_check.slips("wanted = ['1.2.3', '2.0.0', '1.2.4', '2.0.1']\nprint(wanted)", {'versions.txt': versions}) == []
    assert copy_check.slips(as_list([*LINES, LINES[2][:-1] + '2']), {'isbn/batch.txt': BATCH}) == []
    assert copy_check.slips(as_list(SLIP), {'isbn/batch.txt': BATCH}) == [('isbn/batch.txt', SLIP[3], LINES[3])]


def test_one_check_costs_a_bounded_number_of_comparisons():
    """The review: about 2.9 s per block per file within the limits (300 near-copies of 120-character lines against a
    2,000-line file), up to 20 files, before the script ran."""
    import random
    rnd = random.Random(1)
    alphabet = [chr(c) for c in range(0x4e00, 0x4e00 + 3000)]
    lines = [''.join(rnd.choice(alphabet) for _ in range(120)) for _ in range(2000)]
    typed = lines[:2] + [line[:-1] + 'x' for line in lines[2:300]]
    script = 'data = %r\nprint(len(data))\n' % typed
    files = {f'f{n}.txt': '\n'.join(lines) for n in range(4)}
    start = time.perf_counter()
    found = copy_check.slips(script * 4, files)
    assert time.perf_counter() - start < 1.0 and found  # the first slips are still found within the budget


def run(tmp_path, monkeypatch, switch, scripts):
    folder = tmp_path / 'project'
    (folder / 'isbn').mkdir(parents=True)
    (folder / 'isbn' / 'batch.txt').write_text(BATCH)
    if switch is not None:
        monkeypatch.setenv(copy_check.SWITCH, switch)
    app = v34.app_for(tmp_path, 'auto', 32768)
    try:
        with Scripted() as scripted:
            scripted.script = scripts + [v34.final('Done.')] * 3
            chat = app.new_chat(str(folder))
            app.work(chat, 'isbn/batch.txt has one ISBN-13 per line. How many lines does it have?', False, [])
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                runs = app.runs.list(chat['id'])
                if runs and app.active is None and runs[0]['state'] not in ('queued', 'running', 'retry_wait'):
                    break
                time.sleep(.02)
            rid = app.runs.list(chat['id'])[0]['id']
            notes = [e['kind'] for e in app.runs.events(rid, 0, 200) if e['kind'] == 'copy_slip_refused']
            return {'run': app.runs.get(rid), 'requests': list(scripted.requests)}, notes
    finally:
        v34.close(app)


@macos
def test_with_the_switch_on_a_slipped_copy_is_refused_once_before_it_runs(tmp_path, monkeypatch):
    result, notes = run(tmp_path, monkeypatch, '1', [code(as_string(SLIP), 'rc1'), code(as_string(SLIP), 'rc2')])
    assert result['run']['state'] == 'completed', result['run'].get('error')
    first = json.loads(next(m['content'] for m in result['requests'][1] if m.get('tool_call_id') == 'rc1'))
    assert 'isbn/batch.txt' in first['error'] and repr(SLIP[3]) in first['error'] and repr(LINES[3]) in first['error']
    assert 'Nothing ran' in first['error'] and notes == ['copy_slip_refused']
    second = outcome(result, 'rc2')  # once per run: the next script runs
    assert second['error'] is None and second['untrusted_printed'].strip() == '6'


@macos
@pytest.mark.parametrize('switch', [None, '0'])
def test_with_the_switch_off_the_script_runs_as_before(tmp_path, monkeypatch, switch):
    result, notes = run(tmp_path, monkeypatch, switch, [code(as_string(SLIP), 'rc1')])
    assert result['run']['state'] == 'completed' and notes == []
    data = outcome(result, 'rc1')
    assert data['error'] is None and data['untrusted_printed'].strip() == '6'
