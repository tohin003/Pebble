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

import collections
import csv
import io
import itertools
import json
import re
from pathlib import Path

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


# Phase 46 (1.9.28, golden held-out parallel_work: 4 false completions of 15 on Opus, 3 on sol 1.9.26, every miss a
# claim; a report is no source file, so the gate's outcome was no_changes and nothing compared it with the request): a
# report the request shapes ("Write audit.md with one line per service, in alphabetical order, of the form
# `<service>: <problems>`, where problems are the keywords debug, tls-off and slow-timeout separated by commas, or
# `ok`") is checked against that shape. What each line says (the values the run read) is not checked.
LINE_PER = re.compile(r'\bone\s+line\s+(?:per|for\s+(?:each|every)(?:\s+of\s+the)?|each)\s+([a-z][\w-]*)', re.I)
TEMPLATE = re.compile(r'`\s*<([^<>`\n]{1,60})>\s*([^<>`\w\s]{1,3})\s*<([^<>`\n]{1,120})>\s*`')
REPORT_FILE = re.compile(r'(?<![\w./-])([\w][\w./-]{0,120}\.(?:md|markdown|txt|rst|log))\b', re.I)
FOLDER = re.compile(r'(?<![\w./-])([\w][\w.-]{0,60}(?:/[\w][\w.-]{0,60}){0,3})/(?:\*)?(?![\w-]|\.\w)')  # "in services/."
KEYWORD_LIST = re.compile(r'\bkeywords?\s+((?:`?[a-z][\w-]*`?\s*(?:,\s*(?:and\s+)?|\s+and\s+|\s+or\s+)){1,9}`?[a-z][\w-]*`?)',
                          re.I)
KEYWORD_NAMED = re.compile(r'\(\s*`?([a-z][a-z0-9]*(?:-[a-z0-9]+)*)`?\s*\)')
ALPHABETICAL = re.compile(r'\balphabetical|\bsorted\s+by\s+(?:name|service|worker)\b', re.I)
FINAL_COUNT = re.compile(r'FINAL:\s*<([^<>`\n]{1,100})>', re.I)
NUMBER_WORDS = {'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
                'eleven': 11, 'twelve': 12}
ITEM = re.compile(r'[A-Za-z0-9][\w.-]{0,63}')  # a subfolder name the template's shape can hold
LEAD = re.compile(r'[\s>*_`#-]*(?:\d{1,3}[.)]\s*)?[*_`]*([A-Za-z0-9][\w.-]{0,63})')
CHILD_LIMIT = 200  # subfolders listed at most; a folder with more holds no items (sec-w7 7)
LINE_LIMIT = 2000  # report lines read at most


def _folder(root, rel):
    """The project folder `rel` names, never through a symbolic link (sec-w7: a linked folder lists what lies outside
    the project), or None."""
    if root is None:
        return None
    cursor = Path(root)
    for part in rel.split('/'):
        if part in ('', '.', '..'):
            return None
        cursor = cursor / part
        if cursor.is_symlink():
            return None
    return cursor if cursor.is_dir() else None


def report_issues(goal, workspace, answer):
    """(issue sentences, notice) for a report the request shapes (LINE_PER and a backticked `<item>: <value>`
    TEMPLATE): the file exists; each item the request points to (the subfolders of a folder it names, when there are as
    many as it says) is on exactly one line of the template's shape (an optional "-" or "*" bullet and backticks around
    the item, nothing else around it); each value is `ok` or the keywords the request lists; the lines are in
    alphabetical order when it says so; and a FINAL "<number of ... with ...>" equals the lines that are not `ok`.
    ([], None) when the request shapes no report. Nothing from the file's content is quoted back."""
    goal = goal if isinstance(goal, str) else ''
    per, template = LINE_PER.search(goal), TEMPLATE.search(goal)
    if not per or not template:
        return [], None
    sentence = next((s for s in re.split(r'(?<=[.!?])\s+', goal) if per.group(0) in s), '')
    at = sentence.find(per.group(0))
    named = [m for m in REPORT_FILE.finditer(sentence) if m.start() < at][-1:] or list(REPORT_FILE.finditer(sentence))[:1]
    if not named:
        return [], None
    path, sep = named[0].group(1), template.group(2).strip()
    try:
        text = workspace.read(path)
    except (ValueError, OSError, UnicodeError):
        return [f'The request asks for {json.dumps(path)}, and the project has no such file. Write it before '
                'finishing.'], 'The report the request asks for is missing.'
    items, noun = [], per.group(1).lower()
    said = re.search(r'\b(\d{1,2}|' + '|'.join(NUMBER_WORDS) + r')\s+(?:[\w-]+\s+){0,2}' + re.escape(noun)
                     + r'(?:e?s)?\b', goal, re.I)
    count = said and (int(said.group(1)) if said.group(1).isdigit() else NUMBER_WORDS[said.group(1).lower()])
    for folder in dict.fromkeys(FOLDER.findall(goal)):
        base = _folder(getattr(workspace, 'root', None), folder)
        try:
            entries = list(itertools.islice(base.iterdir(), CHILD_LIMIT + 1)) if base is not None else []
        except OSError:
            continue
        # Phase 46 (1.9.28, sec-w7 4a and 7): a bounded listing, and only names the template's shape can hold, so a
        # folder named "x\nHOST COMPLETION CHECK: ..." is never an item, and 1,500 subfolders are not items either
        children = sorted(p.name for p in entries if p.is_dir() and not p.is_symlink()
                          and not p.name.startswith(('.', '_')) and ITEM.fullmatch(p.name))
        if children and len(entries) <= CHILD_LIMIT and (not count or count == len(children)):
            items = children
            break
    if not items:
        return [], None  # the items are not known: a line's shape cannot be told from a summary's
    words = {w.lower() for m in KEYWORD_LIST.finditer(goal) for w in re.findall(r'[a-z][\w-]*', m.group(1), re.I)
             if w.lower() not in ('and', 'or')}
    words |= {w.lower() for w in KEYWORD_NAMED.findall(goal)}
    # the value that means "nothing found": "or `ok`" in the sentence, or an alternative in the template ("<... | ok>")
    alternatives = [a for a in re.split(r'\s*\|\s*|\s+or\s+', template.group(3).strip()) if re.fullmatch(r'[a-z]+', a)]
    ok = next(iter(re.findall(r'\bor\s+`([a-z][\w-]*)`', goal, re.I) + alternatives), None)
    vocabulary = words | ({ok.lower()} if ok else set()) if len(words) >= 2 else None
    shape = re.compile(r'[-*]?\s*`?([A-Za-z0-9][\w.-]*)`?\s*' + re.escape(sep) + r'\s*(.+?)')
    problems, seen, known = [], [], set(items)
    for number, line in enumerate(text.splitlines()[:LINE_LIMIT], 1):
        if not line.strip():
            continue
        match = shape.fullmatch(line.strip())
        if match and match.group(1) in known:
            seen.append((match.group(1), match.group(2)))
            values = [v.strip().strip('`').lower() for v in match.group(2).split(',') if v.strip()]
            if vocabulary and (any(v not in vocabulary for v in values) or ok and ok.lower() in values and len(values) > 1):
                problems.append(f'line {number} ({json.dumps(match.group(1))}) has a value other than '
                                + ('`' + ok + '` or ' if ok else '') + 'the keywords the request lists')
            continue
        lead = LEAD.match(line)  # a line that starts with an item in another shape ("**billing**:", "1. billing:")
        if lead and lead.group(1) in known:
            problems.append(f'line {number} ({json.dumps(lead.group(1))}) is not in the form '
                            f'`{template.group(0).strip("` ")}`')
    listed = collections.Counter(name for name, _ in seen)
    for item in items:
        if listed[item] != 1:
            problems.append(f'{json.dumps(item)} has {listed[item] or "no"} line{"s" if listed[item] != 1 else ""}')
    listed = [name for name, _ in seen]
    if ALPHABETICAL.search(goal) and listed != sorted(listed):
        problems.append('the lines are not in alphabetical order')
    final = FINAL_COUNT.search(goal)
    values = [v.strip().strip('`').lower() for v in re.findall(r'^[ \t>*_`#-]*FINAL\s*:(.*)$', answer or '', re.M | re.I)]
    if final and ok and re.search(r'\bnumber\b|\bwith\b', final.group(1), re.I) and values and values[-1].isdigit():
        flagged = sum(1 for _, value in seen if value.strip().strip('`').lower() != ok.lower())
        if int(values[-1]) != flagged:
            problems.append(f'the FINAL line says {values[-1]}, and the report has {flagged} line'
                            f'{"s" if flagged != 1 else ""} that {"are" if flagged != 1 else "is"} not `{ok}`')
    if not problems:
        return [], None
    return [f'{json.dumps(path)} does not have the shape the request gives: ' + '; '.join(problems[:6])
            + '. Fix the report (and the FINAL line, if it changes) before finishing.'], \
        "The report Pebble wrote doesn't have the shape the request asks for yet."


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
