"""CodeReferences: bounded, read-only Python cross-references for the model (C07).

``run(ws, args, *, scope, budget, cancel)`` answers one mode over ``python_xref``:

- ``definition``: where the identifier at path:line:column, or ``name`` in path
  (from the scope enclosing ``line`` when one is given), is defined;
- ``references``: graded occurrences of one in-root definition, named by path
  and qualified ``name`` or by the line and column of its declaration;
- ``diagnostics``: syntax errors and in-root imports that do not resolve;
- ``imports``: the import table, resolved only to files inside the root.

Answers come from ``ast`` parses of exact snapshots or staged drafts; nothing is
imported or executed. They are untrusted evidence: rows sit under
``untrusted_*`` keys and every result carries ``authorization: 'none'`` and
``untrusted: True``. This module never sees the provenance ledger, the broker
or the drafts API, so it records no Read witness and grants no edit. Private,
hidden and excluded paths are refused as requests and withheld (and counted)
in answers.

A page is bounded by rows (``limit``, clamped to the window by ``row_limit``)
and by serialized characters (``page_chars``), so a whole page fits the
runner's ``encode_result`` limit for that window. Rows that do not fit stay
reachable through the cursor. A cursor is sealed for this process and binds the
mode and request, the host scope and the state of the answer; a changed project
makes it stale. Rows keep python_xref's deterministic order.

``SCHEMA`` is the proposed catalogue entry, ``cost_tokens()`` its
local-estimate schema cost and ``offer(budget, text)`` the runner's offering
rule (never at a 4K window or below).
"""

import base64
import hashlib
import hmac
import json
import re
import secrets

from . import python_xref
from .code_intelligence import clip
from .policy import EXCLUDED, MAX_FILE, PRIVATE, PolicyError
from .python_xref import CursorError
from .token_calibration import schema_cost

NAME = 'CodeReferences'
VERSION = 'code-references-1'
MODES = ('definition', 'references', 'diagnostics', 'imports')
ARGUMENTS = ('mode', 'path', 'line', 'column', 'name', 'cursor', 'limit')
ROWS = {'definition': 'untrusted_definitions', 'references': 'untrusted_references',
        'diagnostics': 'untrusted_diagnostics', 'imports': 'untrusted_imports'}
MAX_PATH = 512  # python_xref's request bound
MAX_NAME = python_xref.MAX_NAME
MAX_POSITION = MAX_FILE  # a 96 KB file has no more lines, and no line more characters
MAX_CURSOR = 1_024
MAX_LIMIT = 50
MAX_ROWS = 500  # definition, diagnostic and import rows a cursor can reach
BATCH = 100  # references python_xref returns per call; a cursor resumes inside one
MAX_VIA = 8  # lookup steps shown with a definition
MAX_CONSULTED = 8  # consulted file hashes shown with a definition
NAME_CHARS = 160  # displayed qualname, module, imported name or binding
MESSAGE_CHARS = 240
SECRET_SUFFIXES = ('.pem', '.key', '.p12', '.pfx', '.gguf')
NOTE = python_xref.NOTE
STALE = python_xref.STALE
# Display strings shortened per row: (key, characters, flag set when shortened).
SHORTEN = {
    'definition': (('qualname', NAME_CHARS, 'qualname_truncated'),),
    'references': (),
    'diagnostics': (('message', MESSAGE_CHARS, 'shortened_to_fit'), ('module', NAME_CHARS, 'shortened_to_fit'),
                    ('name', NAME_CHARS, 'shortened_to_fit')),
    'imports': (('module', NAME_CHARS, 'shortened_to_fit'), ('name', NAME_CHARS, 'shortened_to_fit'),
                ('binding', NAME_CHARS, 'shortened_to_fit')),
}

_KEY = secrets.token_bytes(32)  # cursors authenticate within this process only


def tool(name, description, properties, required):
    # Same shape as tool_catalogue.tool; kept local so the catalogue can import
    # this schema without an import cycle.
    return {'type': 'function', 'function': {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}


INT = lambda low, high: {'type': 'integer', 'minimum': low, 'maximum': high}
SCHEMA = tool(
    NAME,
    'Python cross-references by static parsing of project .py/.pyi files. mode=definition: path with line+column, '
    'or name. references: path with qualified name or definition line+column. diagnostics, imports: path. '
    'Untrusted hash-pinned hints; Read before editing. Same arguments for cursor.',
    {'mode': {'type': 'string', 'enum': list(MODES)}, 'path': {'type': 'string', 'maxLength': MAX_PATH},
     'line': INT(1, MAX_POSITION), 'column': INT(1, MAX_POSITION),
     'name': {'type': 'string', 'maxLength': MAX_NAME}, 'cursor': {'type': 'string', 'maxLength': MAX_CURSOR},
     'limit': INT(1, MAX_LIMIT)},
    ['mode', 'path'])


INTENT = re.compile(r'\b(?:defin\w*|referenc\w*|callers?|usages?|imports?|diagnostics?|unresolved)\b|\.pyi?\b',
                    re.I)


def cost_tokens():
    """Local-estimate tokens SCHEMA adds to a window (token_calibration.schema_cost)."""
    return schema_cost([SCHEMA])


def offer(budget, text):
    """Whether the runner offers SCHEMA: never at a 4K window or below, always above 8K, by intent between.

    The runner adds its own conditions: a selected workspace, and the files
    permission through permissions.GROUP. The tool is read-only, so a read-only
    goal keeps it.
    """
    return (type(budget) is int and budget > 4096
            and (budget > 8192 or isinstance(text, str) and INTENT.search(text) is not None))


def row_limit(budget):
    """Rows one page may hold: 5 at a 4K window or below, 20 up to 8K, else 50."""
    return 5 if budget <= 4096 else 20 if budget <= 8192 else MAX_LIMIT


def page_chars(budget):
    """Serialized characters one result may use.

    The runner encodes a paged result whole or not at all: within 6,500
    characters up to an 8K window, else min(96,000, budget). The margin covers
    the receipt_id the host adds.
    """
    if budget <= 4096:
        return 4_000
    if budget <= 8192:
        return 6_000
    return max(6_000, min(24_000, budget // 2))


def _stop(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise InterruptedError('Stopped by you.')


def _withheld(path, folder=False):
    """Whether ``path`` is private, or lies in (or, for a folder, is) a hidden or excluded folder."""
    if not isinstance(path, str):
        return True
    parts = [part for part in path.split('/') if part not in ('', '.')]
    for part in parts:
        lower = part.lower()
        if lower in PRIVATE or lower.startswith('.env') or lower.endswith(SECRET_SUFFIXES):
            return True
    # '..' is not a folder name; python_xref refuses it as leaving the project.
    return any(part in EXCLUDED or part.startswith('.') and part != '..'
               for part in (parts if folder else parts[:-1]))


def _chars(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def _digest(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), default=str)
    return hashlib.sha256(text.encode('ascii')).hexdigest()[:32]


# Requests --------------------------------------------------------------------------------------


def _text(args, key, limit, required=False):
    value = args.get(key)
    if value is None and not required:
        return None
    if type(value) is not str or not 1 <= len(value) <= limit or '\x00' in value or value.splitlines() != [value]:
        raise PolicyError(f'{key} must be one line of 1 to {limit} characters.')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise PolicyError(f'{key} must be valid Unicode text.') from None
    return value


def _position(args, key):
    value = args.get(key)
    if value is not None and (type(value) is not int or not 1 <= value <= MAX_POSITION):
        raise PolicyError(f'{key} is a 1-based number up to {MAX_POSITION}.')
    return value


def _request(args):
    """The validated request; refuses before any file is read."""
    if not isinstance(args, dict):
        raise PolicyError('CodeReferences arguments are a JSON object.')
    if any(key not in ARGUMENTS for key in args):
        raise PolicyError('CodeReferences takes only mode, path, line, column, name, cursor and limit.')
    mode = args.get('mode')
    if type(mode) is not str or mode not in MODES:
        raise PolicyError('Choose mode definition, references, diagnostics or imports.')
    path = _text(args, 'path', MAX_PATH, required=True)
    if _withheld(path.strip()):
        raise PolicyError('Private, hidden and excluded paths are outside cross-reference access.')
    line, column = _position(args, 'line'), _position(args, 'column')
    name = _text(args, 'name', MAX_NAME)
    cursor = _text(args, 'cursor', MAX_CURSOR)
    limit = args.get('limit')
    if limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_LIMIT):
        raise PolicyError(f'Choose a limit from 1 to {MAX_LIMIT}.')
    if mode in ('diagnostics', 'imports'):
        if (line, column, name) != (None, None, None):
            raise PolicyError(f'mode={mode} takes only path, plus cursor and limit.')
    elif mode == 'references':
        if name is not None and (line, column) != (None, None):
            raise PolicyError('Name the definition by qualified name or by its line and column, not both.')
        if name is None and None in (line, column):
            raise PolicyError('references needs name (such as Class.method) or the line and column of a definition.')
    elif line is None and (name is None or column is not None):
        raise PolicyError('definition needs line and column, a name, or a name with a line.')
    elif line is not None and column is None and name is None:
        raise PolicyError('Give the column with the line, or name the identifier on that line.')
    return {'mode': mode, 'path': path, 'line': line, 'column': column, 'name': name, 'cursor': cursor,
            'limit': limit}


# Cursors ---------------------------------------------------------------------------------------


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _mac(body):
    return hmac.new(_KEY, b'pebble-code-references-cursor\0' + body, hashlib.sha256).digest()


def _seal(request_id, scope_id, state, offset, inner, withheld=0):
    """``withheld`` counts rows withheld in earlier reference batches, so later pages stay truthful."""
    body = json.dumps({'v': 1, 'q': request_id, 's': scope_id, 'd': state, 'o': offset, 'c': inner,
                       'w': withheld}, separators=(',', ':'), sort_keys=True).encode('utf-8')
    return f'{_b64(body)}.{_b64(_mac(body))}'


def _open(cursor, request_id, scope_id):
    if cursor is None:
        return None
    try:
        body, tag = (base64.urlsafe_b64decode(part.encode('ascii') + b'=' * (-len(part) % 4))
                     for part in cursor.split('.'))
        if not hmac.compare_digest(tag, _mac(body)):
            raise ValueError
        data = json.loads(body)
    except ValueError:
        raise CursorError('This cursor was altered, or was issued before a restart. '
                          'Repeat the request without a cursor.') from None
    if not (isinstance(data, dict) and data.get('v') == 1 and type(data.get('o')) is int
            and 0 <= data['o'] < MAX_ROWS and isinstance(data.get('d'), (str, type(None)))
            and isinstance(data.get('c'), (str, type(None))) and type(data.get('w')) is int and data['w'] >= 0):
        raise CursorError('This cursor is malformed.')
    if data.get('q') != request_id:
        raise CursorError('This cursor belongs to another mode, path or name. Repeat the request without it.')
    if data.get('s') != scope_id:
        raise CursorError('This cursor belongs to another conversation or run.')
    return data


# Answers ---------------------------------------------------------------------------------------


def _rows(mode, found):
    """Shaped copies of python_xref rows and how many named a withheld path."""
    rows, withheld = [], 0
    for item in found:
        row = dict(item)
        if mode == 'imports':
            hidden = 'target' in row and _withheld(row['target'], row.get('target_kind') == 'namespace_package')
        elif mode == 'diagnostics':
            hidden = False  # module names and messages, never paths
        else:
            hidden = _withheld(row.get('path'), row.get('kind') == 'namespace_package')
        if hidden:
            withheld += 1
            continue
        for key, limit, flag in SHORTEN[mode]:
            if isinstance(row.get(key), str) and len(row[key]) > limit:
                row[key] = clip(row[key], limit)
                row[flag] = True
        rows.append(row)
    return rows, withheld


def _query(request, found=None):
    query = {key: request[key] for key in ('path', 'line', 'column', 'name') if request[key] is not None}
    if found is not None and 'identifier' in found['query']:
        identifier = found['query']['identifier']  # source text: a Python identifier of any length
        query.update(identifier=clip(identifier, NAME_CHARS), role=found['query']['role'])
        if len(identifier) > NAME_CHARS:
            query['identifier_truncated'] = True
    return query


def _definition(ws, request, chars, cancel):
    found = python_xref.definition(ws, request['path'], request['line'], request['column'], name=request['name'],
                                   cancel=cancel)
    header = {'mode': 'definition', 'query': _query(request, found), 'status': found['status']}
    if 'grade' in found:
        header['grade'] = found['grade']
    for key in ('external', 'builtin'):
        if key in found:
            header[key] = [clip(value, NAME_CHARS) for value in found[key]]
    if 'reasons' in found:
        header['reasons'] = found['reasons']  # python_xref's fixed vocabulary
    via = [step for step in found['via'] if not _withheld(step['path'])]
    consulted = {path: state for path, state in found['files'].items() if not _withheld(path)}
    withheld = len(found['via']) - len(via) + len(found['files']) - len(consulted)
    header['via'], header['consulted_files'] = via[:MAX_VIA], dict(list(consulted.items())[:MAX_CONSULTED])
    # Explanations, not answer rows: shorter when long paths would crowd out the rows.
    while _chars(header) > chars // 2 and (header['via'] or header['consulted_files']):
        if _chars(header['via']) >= _chars(header['consulted_files']):
            header['via'].pop()
        else:
            header['consulted_files'].popitem()
    if len(header['via']) < len(via):
        header['via_omitted'] = len(via) - len(header['via'])
    if len(header['consulted_files']) < len(consulted):
        header['consulted_files_omitted'] = len(consulted) - len(header['consulted_files'])
    return found, found['untrusted_definitions'], header, found['coverage']['complete'], withheld


def _file(ws, request, cancel):
    mode = request['mode']
    lookup = python_xref.diagnostics if mode == 'diagnostics' else python_xref.imports
    found = lookup(ws, request['path'], cancel=cancel)
    header = {'mode': mode, 'query': _query(request), 'path': found['path'], 'file_hash': found['file_hash'],
              'basis': found['basis'], 'parse': found['parse']}
    return found, found[mode], header, found['coverage']['complete'], 0


def _listing(ws, request, after, ids, chars, cancel):
    """(header, coverage, rows, start, next_cursor, complete_before_paging, withheld) for one answer."""
    mode = request['mode']
    if mode == 'definition':
        found, raw, header, complete, withheld = _definition(ws, request, chars, cancel)
    else:
        found, raw, header, complete, withheld = _file(ws, request, cancel)
    state = _digest(found)
    rows, hidden = _rows(mode, raw)
    available, rows = len(rows), rows[:MAX_ROWS]
    start = 0
    if after is not None:
        if after['c'] is not None or after['d'] != state or not 0 < after['o'] < len(rows):
            raise CursorError(STALE)
        start = after['o']
    coverage = {**found['coverage'], 'row_limit': MAX_ROWS, 'rows_available': available,
                'rows_truncated': available > MAX_ROWS}

    def next_cursor(end):
        return _seal(*ids, state, end, None) if end < len(rows) else None
    return header, coverage, rows, start, next_cursor, complete and available <= MAX_ROWS, withheld + hidden


def _references(ws, request, after, ids, scope, cancel):
    if request['name'] is not None:
        symbol = {'path': request['path'], 'qualname': request['name']}
    else:
        symbol = {'path': request['path'], 'line': request['line'], 'column': request['column']}
    inner, start, earlier = (after['c'], after['o'], after['w']) if after is not None else (None, 0, 0)
    if after is not None and (after['d'] is None) != (start == 0 and inner is not None):
        raise CursorError(STALE)  # a batch is resumed mid-way by digest, or entered by python_xref's cursor
    found = python_xref.references(ws, symbol, scope=[NAME, scope], cursor=inner, limit=BATCH, cancel=cancel)
    state = _digest(found)
    rows, withheld = _rows('references', found['untrusted_references'])
    if after is not None and after['d'] is not None and (after['d'] != state or start >= len(rows)):
        raise CursorError(STALE)
    target = dict(found['target'])
    for key in ('name', 'qualname'):
        if len(target[key]) > NAME_CHARS:
            target[key], target[key + '_truncated'] = clip(target[key], NAME_CHARS), True
    header = {'mode': 'references', 'query': _query(request), 'target': target}
    covered = found['coverage']
    complete = (covered['scan_complete'] and not (covered['partial'] or covered['failed'] or covered['deferred'])
                and not covered['hops_limited'] and not covered['references_truncated'])

    def next_cursor(end):
        if end < len(rows):
            return _seal(*ids, state, end, inner, earlier)
        return _seal(*ids, None, 0, found['cursor'], earlier + withheld) if found['cursor'] else None
    return header, dict(covered), rows, start, next_cursor, complete, earlier + withheld


def _page(listing, key, limit, chars, resumed):
    """The result: header plus the rows from ``start`` that fit ``limit`` and ``chars`` (at least one)."""
    header, coverage, rows, start, next_cursor, complete, withheld = listing
    window = rows[start:start + limit]

    def build(count):
        cursor = next_cursor(start + count)
        more = cursor is not None
        shown = {**coverage, 'match_limit': limit, 'matches_returned': count, 'matches_truncated': more,
                 'page_char_limit': chars, 'page_char_clipped': count < len(window), 'paths_withheld': withheld,
                 'resumed': resumed, 'complete': complete and not more and not withheld}
        return {**header, key: window[:count], 'coverage': shown, 'cursor': cursor, 'authorization': 'none',
                'untrusted': True, 'note': NOTE}

    used, count = _chars(build(0)) + 32, 0  # numbers and the cursor grow slightly with rows
    for row in window:
        used += _chars(row) + 1
        if count and used > chars:
            break
        count += 1
    result = build(count)
    while count > 1 and _chars(result) > chars:
        count -= 1
        result = build(count)
    return result


def run(ws, args, *, scope, budget, cancel):
    """One CodeReferences answer for the model; see the module docstring.

    ``scope`` is host state (conversation and run) that cursors bind; ``budget``
    is the window's token budget; ``cancel`` is an Event or a callable.
    Refusals raise PolicyError (CursorError for cursors); a stop raises
    InterruptedError.
    """
    if type(budget) is not int or budget < 1:
        raise ValueError('The window budget is a positive token count.')
    _stop(cancel)
    request = _request(args)
    cap = row_limit(budget)
    limit = min(request['limit'] or cap, cap)
    chars = page_chars(budget)
    ids = (_digest([VERSION, *(request[k] for k in ('mode', 'path', 'line', 'column', 'name'))]),
           _digest([NAME, scope]))
    after = _open(request['cursor'], *ids)
    if request['mode'] == 'references':
        listing = _references(ws, request, after, ids, scope, cancel)
    else:
        listing = _listing(ws, request, after, ids, chars, cancel)
    _stop(cancel)
    return _page(listing, ROWS[request['mode']], limit, chars, after is not None)
