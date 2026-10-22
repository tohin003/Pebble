"""Phase 24 (V1-030): model-free checks of the data files a run wrote, before it may complete.

AgentGuard's gate only verifies source code, so a run that wrote a JSON or CSV file settled "completed" on whatever
the model wrote, even when the file did not parse. These checks need no model and no test runner:

- a written ``.json`` file must parse; when the request lists the exact keys of one JSON object, that object must
  have exactly those keys (or at least the listed ones, when the list is not "exactly");
- a written ``.csv`` file must be rectangular (every row as wide as the first, no blank row in between) and, when the
  request gives the header row in backticks, start with exactly that header.

Every problem of a run becomes one repairable completion issue that says where each file breaks (line and column).
File names are project paths and are JSON-quoted; nothing from a file's content is quoted back.
"""

import csv
import io
import json
import re

SUFFIXES = ('.json', '.csv')
MAX_FILES = 8
# "with exactly these keys: a (integer), b, … and c." / "keys `a`, `b` and `c`"
KEYS = re.compile(r'\b(exactly\s+|only\s+)?(?:these|the\s+following|the)?\s*(?:top-level\s+)?keys\s*(?:named\s+)?:?\s*'
                  r'((?:`[^`\n]{1,64}`|[A-Za-z_][\w-]{0,63})(?:\s*\([^)\n]{0,120}\))?'
                  r'(?:\s*(?:,|,?\s+and\b)\s*(?:`[^`\n]{1,64}`|[A-Za-z_][\w-]{0,63})(?:\s*\([^)\n]{0,120}\))?)+)', re.I)
KEY_NAME = re.compile(r'`([^`\n]{1,64})`|([A-Za-z_][\w-]{0,63})')
HEADER = re.compile(r'\bheader(?:\s+row)?\b[^`\n]{0,40}`([^`\n]{1,400})`', re.I)


def listed_keys(goal):
    """(keys, exact) the request lists for a JSON object, or (None, False)."""
    match = KEYS.search(goal or '')
    # A list, not prose that mentions keys: a colon, backticks, or "these"/"the following" keys.
    if not match or not re.search(r':|`|\bthese\b|\bfollowing\b', match[0], re.I):
        return None, False
    body = re.sub(r'\([^)\n]*\)', ' ', match[2])
    keys = []
    for part in re.split(r'\s*,\s*|\s+and\s+', body):
        found = KEY_NAME.fullmatch(part.strip())
        if found:
            keys.append(found[1] or found[2])
    if len(keys) < 2 or len(set(keys)) != len(keys):
        return None, False
    return keys, bool(match[1])


def listed_header(goal):
    match = HEADER.search(goal or '')
    if not match or ',' not in match[1]:
        return None
    return next(csv.reader([match[1]]), None)


def _named(goal, path):
    return path.rsplit('/', 1)[-1] in (goal or '')


def check_json(path, text, goal, only_json):
    try:
        value = json.loads(text)
    except ValueError as exc:
        line, column = getattr(exc, 'lineno', None), getattr(exc, 'colno', None)
        message = getattr(exc, 'msg', 'not valid JSON')
        return f'{json.dumps(path)} is not valid JSON: line {line}, column {column} ({message}).'
    keys, exact = listed_keys(goal)
    if keys and (only_json or _named(goal, path)):
        if not isinstance(value, dict):
            return f'{json.dumps(path)} must be one JSON object with the keys the request lists.'
        missing = [k for k in keys if k not in value]
        extra = sorted(k for k in value if k not in keys) if exact else []
        if missing or extra:
            parts = ([f'missing {json.dumps(missing[:8])}'] if missing else []) + \
                    ([f'not requested {json.dumps(extra[:8])}'] if extra else [])
            return f'{json.dumps(path)} does not have the keys the request lists: ' + '; '.join(parts) + '.'
    return None


def check_csv(path, text, goal, only_csv):
    rows = list(csv.reader(io.StringIO(text)))
    while rows and not rows[-1]:
        rows.pop()  # a trailing newline
    if not rows:
        return f'{json.dumps(path)} has no rows.'
    width = len(rows[0])
    for number, row in enumerate(rows, start=1):
        if len(row) != width:
            what = 'is blank' if not row else f'has {len(row)} fields, not {width}'
            return f'{json.dumps(path)} is not rectangular: line {number} {what}.'
    header = listed_header(goal)
    if header and (only_csv or _named(goal, path)) and rows[0] != header:
        return f'{json.dumps(path)} must start with the header row the request gives ({len(header)} columns).'
    return None


def issues(goal, workspace, paths):
    """(issue sentences, person-facing notice) for the .json and .csv files in paths (applied this run)."""
    written = [p for p in dict.fromkeys(paths) if isinstance(p, str) and p.lower().endswith(SUFFIXES)][:MAX_FILES]
    found = []
    only_json = sum(p.lower().endswith('.json') for p in written) == 1
    only_csv = sum(p.lower().endswith('.csv') for p in written) == 1
    for path in written:
        try:
            text = workspace.read(path)
        except (ValueError, OSError, UnicodeError):
            continue  # deleted since, or unreadable: nothing to check here
        problem = (check_json(path, text, goal, only_json) if path.lower().endswith('.json')
                   else check_csv(path, text, goal, only_csv))
        if problem:
            found.append(problem)
    if not found:
        return [], None
    issue = ' '.join(found) + ' Fix the file before finishing.'
    kinds = {'JSON' if p.lower().endswith('.json') else 'CSV' for p in written}
    notice = 'A file Pebble wrote is not valid ' + ' or '.join(sorted(kinds)) + ' yet.'
    return [issue], notice
