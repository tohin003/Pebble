"""Phase 46 (1.9.28, golden held-out workshop_repeat: 2 sol false completions; b-new C1): a RunCode script that types a
file's lines in by hand instead of reading them.

In 23 of 36 recorded dev turns of d-wr-01 (sol) the script typed the batch in from the prefetched text instead of
calling pebble.read; one slipped character gives a computed-looking wrong answer that is then claimed. Before a script
runs, every literal block in it (a multi-line string, a list, tuple or set of strings, a dict of "key value" pairs) is
compared with the files this run read or was given: a block that holds at least two exact lines of a file and, beside
them, a line close to one of its lines but different in a letter or digit is a copy slip, and the script is refused
with both lines, once, so it reads the file instead. Formatting alone (spaces, hyphens, case) is never a slip. Literals
a script passes to pebble.make_tool or keep_tool are not compared, nor the values of the names it passes to them (a
tool's own tests hold near-copies on purpose: one digit changed to test a failing checksum, and D69 keeps only tools
with three tests), and a script that writes or edits is not compared at all (its near-copies are the change it means;
dev d-lh-01's edit scripts). Off unless PEBBLE_1928_COPY_CHECK is "1" (the lead measures it first); a check that
cannot run never stops the script.

Phase 46 (1.9.28, w12-bnew's security review): a script "writes or edits" only through Pebble's own pebble.write, edit
and append (any `valid.append(n)` switched the check off); a typed line is a slip only beside a file line it could be a
copy of that the block does not already hold exactly (['1.2.3', '1.2.4', '1.2.5'] asks about 1.2.5, it does not mistype
1.2.4); and one check compares at most COMPARISONS lines (2.9 s per block per file before).
"""
import ast
import difflib
import os
import re
from pathlib import Path

SWITCH = 'PEBBLE_1928_COPY_CHECK'
RATIO = 0.8          # difflib's ratio for "close to a line of the file"
SHORTEST = 5         # characters; shorter typed lines are never compared
FILES = 20           # files compared at most
FILE_BYTES = 256_000
LINES = 2000         # lines of one file compared at most
TYPED = 300          # lines of one literal block compared at most
COMPARISONS = 40_000  # typed line against file line, in one check at most; past it the script runs unchecked
TOOL_CALLS = frozenset({'make_tool', 'keep_tool'})
CHANGES = frozenset({'write', 'edit', 'append', 'write_file', 'edit_file'})
SEPARATORS = re.compile(r'[\s:=,;|]+')
FORMATTING = re.compile(r'[\W_]+')
REFUSAL = ('The script types lines of {path} in by hand, and one differs from the file: it has {typed!r} where the file '
           'has {line!r}. Nothing ran. Read the data in the script instead (pebble.read(file_path={path!r}) returns '
           'the file\'s text) so every value is the file\'s own.')


def enabled():
    return os.environ.get(SWITCH) == '1'


def _skipped(tree):
    """ids of the nodes a script passes to pebble.make_tool or keep_tool, and of the values of the names it passes."""
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr in TOOL_CALLS]
    names, skipped = set(), set()
    for call in calls:
        for argument in [*call.args, *(k.value for k in call.keywords)]:
            for node in ast.walk(argument):
                skipped.add(id(node))
                if isinstance(node, ast.Name):
                    names.add(node.id)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in names for t in targets):
                skipped |= {id(inner) for inner in ast.walk(node.value)}
    return skipped


def _changes(tree):
    """Whether the script calls Pebble's own write, edit or append: pebble.write(...), through `import pebble as p` or
    `p = pebble`, or a name `from pebble import write` gives; never a list's or a file's .append and .write."""
    modules, names = {'pebble'}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.asname for alias in node.names if alias.name == 'pebble' and alias.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == 'pebble':
            names |= {alias.asname or alias.name for alias in node.names if alias.name in CHANGES}
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id == 'pebble':
            modules |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return any(isinstance(node, ast.Call) and (
        isinstance(node.func, ast.Attribute) and node.func.attr in CHANGES and isinstance(node.func.value, ast.Name)
        and node.func.value.id in modules or isinstance(node.func, ast.Name) and node.func.id in names)
        for node in ast.walk(tree))


def blocks(code):
    """The script's literal blocks, each a list of typed lines (stripped, non-empty)."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []
    if _changes(tree):
        return []  # a script that writes or edits means to change text: its near-copies are the change
    skipped, found = _skipped(tree), []
    text = lambda node: isinstance(node, ast.Constant) and isinstance(node.value, str)  # noqa: E731
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            items = [e.value.strip() for e in node.elts if text(e) and e.value.strip()]
            if len(items) >= 3:
                found.append(items)
        elif isinstance(node, ast.Dict):
            pairs = [f'{k.value} {v.value}' for k, v in zip(node.keys, node.values) if k is not None and text(k)
                     and isinstance(v, ast.Constant) and isinstance(v.value, (str, int, float)) and not isinstance(v.value, bool)]
            if len(pairs) >= 3:
                found.append(pairs)
        elif text(node) and '\n' in node.value.strip():
            found.append([line.strip() for line in node.value.splitlines() if line.strip()])
    return found


def slips(code, files, ratio=RATIO):
    """[(path, typed, line)]: typed lines close to, but not, a line of a file another line of the same block copies,
    and that the block does not hold exactly; at most COMPARISONS comparisons in all."""
    found, budget = [], COMPARISONS
    for block in blocks(code):
        block = block[:TYPED]
        for path, content in files.items():
            lines = [line.strip() for line in content.splitlines()[:LINES] if line.strip()]
            shown = {**{SEPARATORS.sub(' ', line): line for line in lines}, **{line: line for line in lines}}
            if sum(typed in shown for typed in block) < 2:
                continue
            plain = {FORMATTING.sub('', line).casefold() for line in lines}
            copied = {shown[typed] for typed in block if typed in shown}
            others = [key for key, line in shown.items() if line not in copied]
            for typed in block:
                if typed in shown or len(typed) < SHORTEST or FORMATTING.sub('', typed).casefold() in plain:
                    continue
                budget -= len(others)
                if budget < 0:
                    return found
                close = difflib.get_close_matches(typed, others, n=1, cutoff=ratio)
                if close:
                    found.append((path, typed, shown[close[0]]))
    return found


def check(root, paths, code):
    """The refusal for the first copy slip of `code` against the files `paths` (relative to root), or None."""
    files, root = {}, Path(root)
    for rel in paths:
        if len(files) >= FILES:
            break
        if not isinstance(rel, str) or rel.endswith('/'):
            continue
        path = root / rel
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > FILE_BYTES:
                continue
            files[rel] = path.read_bytes().decode('utf-8', 'replace')
        except OSError:
            continue
    found = slips(code, files) if files else []
    if not found:
        return None
    path, typed, line = found[0]
    return REFUSAL.format(path=path, typed=typed[:200], line=line[:200])
