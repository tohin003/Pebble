"""Phase 45 (1.9.8, audit round R4, F148): Details › Activity names each Code Mode step by its purpose (or its length),
never by "Running a script" alone, and a failed script reads as one sentence, not a Python traceback."""
from pebble.app import App
from pebble.run_activity import ActivityFeed, script_failure

TRACE = ('Traceback (most recent call last):\n  File "pebble/code_worker.py", line 240, in run\n    keep_tool(\'c_to_f\', \'x\')\n'
         "TypeError: <lambda>() takes 0 positional arguments but 2 were given\n")


def rows_of(tool, finished=(2.0, False)):
    found = []
    feed = ActivityFeed(None, None, None, label=App.tool_label)
    feed._tool(lambda seq, created, kind, label, detail=None, duration=None, status='done':
               found.append((label, detail, status)), tool, 1, 1.0, finished, False, False)
    return found


def tool(**extra):
    return {'name': 'RunCode', 'file_path': None, 'arg_name': None, 'purpose': None, 'code_lines': None,
            'status': None, 'error': None, 'state': 'completed', 'agent_id': None, **extra}


def test_a_script_row_names_its_purpose_or_its_length():
    assert rows_of(tool(purpose='Check that 0 \N{DEGREE SIGN}C is 32 \N{DEGREE SIGN}F')) == [
        ('Running a script: Check that 0 \N{DEGREE SIGN}C is 32 \N{DEGREE SIGN}F', None, 'done')]
    assert rows_of(tool(code_lines=4)) == [('Running a script (4 lines of Python)', None, 'done')]
    assert rows_of(tool()) == [('Running a script', None, 'done')]
    assert App.tool_label('RunCode', {'code': 'a = 1\nprint(a)\n'}) == 'Running a script (2 lines of Python)'
    assert App.tool_label('RunCode', {'purpose': '  ', 'code': ''}) == 'Running a script'


def test_a_failed_script_reads_as_one_sentence():
    [(label, detail, status)] = rows_of(tool(purpose='Keep the tool', error=TRACE))
    assert status == 'failed' and detail == ('The script stopped with an error: TypeError: <lambda>() takes 0 positional '
                                             'arguments but 2 were given')
    assert script_failure('The tool failed (ValueError).') == 'The tool failed (ValueError).'
    assert script_failure('Traceback (most recent call last):\n') == 'The script stopped with an error.'
    assert script_failure(None) is None


def test_the_reader_takes_purpose_and_length_never_the_code(tmp_path):
    import sqlite3
    from pebble.run_activity import TOOLS_SQL
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE run_tools (run_id, call_id, name, state, arguments, result)')
    db.execute('INSERT INTO run_tools VALUES (?,?,?,?,?,?)', ('r', 'c1', 'RunCode', 'completed',
               '{"purpose": "Convert", "code": "a = 1\\nb = 2\\nprint(a + b)"}', '{"error": null}'))
    db.execute('INSERT INTO run_tools VALUES (?,?,?,?,?,?)', ('r', 'c2', 'Read', 'completed', '{"file_path": "x.py"}', '{}'))
    rows = {r['call_id']: dict(r) for r in db.execute(TOOLS_SQL, ('r',))}
    assert rows['c1']['purpose'] == 'Convert' and rows['c1']['code_lines'] == 3
    assert rows['c2']['purpose'] is None and rows['c2']['code_lines'] is None
    assert not any('print' in str(v) for v in rows['c1'].values()), 'the code itself is never read'
