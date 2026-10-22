"""code_references: the bounded, untrusted CodeReferences tool over python_xref (C07)."""

import hashlib
import json
import threading

import pytest

from pebble import code_references, python_xref
from pebble.code_references import SCHEMA, cost_tokens, offer, page_chars, row_limit, run
from pebble.context import encode_result
from pebble.policy import PolicyError, Workspace
from pebble.python_xref import CursorError

SCOPE = ('chat-a', 'run-1')
MODELS = '''\
from .util import helper as assist


class Ledger:
    def __init__(self, entries):
        self.entries = entries

    def add(self, value):
        self.entries.append(value)
        return assist(value)


def make():
    book = Ledger([])
    book.add(1)
    return book
'''
UTIL = '''\
def helper(value):
    return value * 2


class Tally:
    def add(self):
        return 1
'''
USES = '''\
from app.models import Ledger
from .missing import nothing


def run(thing):
    Ledger([]).add(2)
    thing.add(3)
    return "add"  # add
'''
MANY = 'def f():\n    return 0\n\n\n' + 'f()\n' * 60
# Measured local estimate (token_calibration.schema_cost); growth needs review.
PINNED_SCHEMA_TOKENS = 290


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write(root, name, text):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return text


def where(text, needle):
    """1-based (line, column) of the first ``needle``."""
    index = text.index(needle)
    return text.count('\n', 0, index) + 1, index - (text.rfind('\n', 0, index) + 1) + 1


def chars(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    for name, text in {'app/__init__.py': '', 'app/models.py': MODELS, 'app/util.py': UTIL,
                       'app/uses.py': USES, 'many.py': MANY}.items():
        write(root, name, text)
    return root


def ask(ws, budget=32768, scope=SCOPE, cancel=None, **args):
    return run(ws, args, scope=scope, budget=budget, cancel=cancel)


def walk(ws, budget, **args):
    """Every page of one request, following cursors."""
    pages = [ask(ws, budget, **args)]
    while pages[-1]['cursor']:
        assert len(pages) < 100
        pages.append(ask(ws, budget, **args, cursor=pages[-1]['cursor']))
    return pages


def rows(pages, key):
    return [row for page in pages for row in page[key]]


@pytest.fixture
def untouched(monkeypatch):
    """python_xref entry points that fail the test if a request reaches them."""
    def refuse(*_args, **_kwargs):
        raise AssertionError('python_xref was called')
    for name in ('definition', 'references', 'diagnostics', 'imports'):
        monkeypatch.setattr(python_xref, name, refuse)


def test_schema_is_a_bounded_catalogue_entry_with_a_pinned_cost():
    from pebble import tool_catalogue
    from pebble.app import App
    from pebble.token_calibration import schema_cost
    from pebble.tool_authz import strict_schema_errors
    fn = SCHEMA['function']
    params = fn['parameters']
    assert SCHEMA == tool_catalogue.tool(fn['name'], fn['description'], params['properties'], params['required'])
    assert fn['name'] == 'CodeReferences' and len(fn['description']) <= 300
    assert params['required'] == ['mode', 'path'] and params['properties']['mode']['enum'] == list(
        code_references.MODES)
    # The catalogue lists this exact object or nothing by that name: one source of truth.
    assert all(t is SCHEMA or t['function']['name'] != 'CodeReferences' for t in tool_catalogue.TOOLS)
    from pebble.tool_authz import canonical_catalogue, catalogue_digest
    catalogue = canonical_catalogue()
    catalogue_digest(catalogue if SCHEMA in catalogue else catalogue + [SCHEMA])  # one entry per name, canonical
    good = {'mode': 'definition', 'path': 'app/models.py', 'line': 50_000, 'column': 3, 'limit': 50}
    assert strict_schema_errors(params, good) == []
    App.validate('CodeReferences', good, [SCHEMA])  # explicit maxima, not App's implied 10,000
    for bad in ({**good, 'line': 0}, {**good, 'limit': 51}, {**good, 'extra': 1}, {'path': 'a.py'},
                {**good, 'mode': 'callers'}, {**good, 'column': True}, {**good, 'name': 'x' * 257}):
        assert strict_schema_errors(params, bad)
    assert cost_tokens() == schema_cost([SCHEMA]) == PINNED_SCHEMA_TOKENS


def test_never_offered_at_4k_always_above_8k_and_by_intent_between():
    asks = ('Where is Ledger.add defined?', 'find references to helper', 'who imports util', 'fix app/models.py',
            'list diagnostics', '')
    for text in asks:
        assert not offer(4096, text) and not offer(1024, text) and not offer(1, text)
        assert offer(8193, text) and offer(200_000, text)
    assert all(offer(budget, text) for budget in (4097, 8192) for text in asks[:-1])
    for text in ('Write a short poem', 'summarize the README', '', None):
        assert not offer(8192, text)
    assert not offer(None, 'defined') and not offer(8193.0, 'defined') and not offer(True, 'defined')


def test_budget_clamps_rows_and_serialized_size():
    assert [row_limit(b) for b in (1, 4096, 4097, 8192, 8193, 200_000)] == [5, 5, 20, 20, 50, 50]
    assert [page_chars(b) for b in (4096, 8192, 8193, 16384, 32768, 200_000)] == [
        4000, 6000, 6000, 8192, 16384, 24000]
    for budget in (0, -1, 4096.0, True, None):
        with pytest.raises(ValueError):
            run(None, {'mode': 'imports', 'path': 'a.py'}, scope=SCOPE, budget=budget, cancel=None)


def test_definition_resolves_across_files_with_untrusted_labels(project):
    ws = Workspace(project)
    line, column = where(MODELS, 'assist(value)')
    result = ask(ws, mode='definition', path='app/models.py', line=line, column=column)
    expected = python_xref.definition(ws, 'app/models.py', line, column)
    assert result['untrusted_definitions'] == expected['untrusted_definitions']
    row = result['untrusted_definitions'][0]
    assert (row['path'], row['line'], row['kind'], row['qualname'], row['file_hash']) == (
        'app/util.py', 1, 'function', 'helper', sha(UTIL))
    assert result['status'] == 'resolved' and result['grade'] == 'resolved'
    assert result['query'] == {'path': 'app/models.py', 'line': line, 'column': column, 'identifier': 'assist',
                               'role': 'name'}
    assert [step['step'] for step in result['via']] == ['module', 'import', 'outline']
    assert result['consulted_files'] == expected['files'] and 'files' not in result
    assert (result['authorization'], result['untrusted'], result['cursor']) == ('none', True, None)
    assert 'grants no Read witness' in result['note']
    coverage = result['coverage']
    assert coverage['complete'] and coverage['matches_returned'] == 1 and not coverage['paths_withheld']
    # By name, from module scope; and an unbound name stays unresolved.
    by_name = ask(ws, mode='definition', path='app/uses.py', name='Ledger')
    assert [(r['path'], r['qualname']) for r in by_name['untrusted_definitions']] == [('app/models.py', 'Ledger')]
    missing = ask(ws, mode='definition', path='app/uses.py', name='nothing_here')
    assert (missing['status'], missing['reasons'], missing['untrusted_definitions']) == (
        'unresolved', ['unbound'], [])


def test_references_page_through_every_graded_row_in_order(project):
    ws = Workspace(project)
    whole = python_xref.references(ws, 'app/models.py:Ledger.add', limit=100)['untrusted_references']
    assert [r['grade'] for r in whole] == ['resolved'] * 3 + ['attribute_candidate'] + ['textual'] * 2
    pages = walk(ws, 32768, mode='references', path='app/models.py', name='Ledger.add', limit=2)
    assert rows(pages, 'untrusted_references') == whole
    assert [len(p['untrusted_references']) for p in pages] == [2, 2, 2]
    assert [p['coverage']['resumed'] for p in pages] == [False, True, True]
    assert [p['coverage']['complete'] for p in pages] == [False, False, True]
    first = pages[0]
    assert first['target']['qualname'] == 'Ledger.add' and first['target']['file_hash'] == sha(MODELS)
    assert first['coverage']['grades'] == {'resolved': 3, 'attribute_candidate': 1, 'textual': 2}
    assert first['coverage']['match_limit'] == 2 and first['coverage']['matches_truncated']
    # The declaration's line and column name the same definition.
    by_place = ask(ws, mode='references', path='app/models.py', line=8, column=5)
    assert by_place['untrusted_references'] == whole and by_place['cursor'] is None


def test_cursors_bind_mode_request_scope_and_state(project):
    ws = Workspace(project)
    args = {'mode': 'references', 'path': 'app/models.py', 'name': 'Ledger.add', 'limit': 2}
    first = ask(ws, **args)
    cursor = first['cursor']
    with pytest.raises(CursorError, match='another conversation or run'):
        ask(ws, scope=('chat-a', 'run-2'), **args, cursor=cursor)
    for other in ({**args, 'mode': 'imports', 'name': None}, {**args, 'name': 'Ledger'},
                  {**args, 'path': 'app/util.py'}):
        other = {k: v for k, v in other.items() if v is not None}
        with pytest.raises(CursorError, match='another mode, path or name'):
            ask(ws, **other, cursor=cursor)
    body, tag = cursor.split('.')
    for altered in (body[:-2] + 'xx.' + tag, body, cursor + '.x', 'é.é'):
        with pytest.raises(CursorError, match='altered, or was issued before a restart'):
            ask(ws, **args, cursor=altered)
    # The limit may change between pages; the rows continue where they stopped.
    second = ask(ws, **{**args, 'limit': 50}, cursor=cursor)
    assert first['untrusted_references'] + second['untrusted_references'] == ask(
        ws, **{**args, 'limit': 50})['untrusted_references']
    write(project, 'app/uses.py', USES + '# add\n')
    with pytest.raises(CursorError, match='changed since'):
        ask(ws, **args, cursor=cursor)
    # A cursor this process never sealed in that shape is stale, never trusted.
    forged = code_references._seal(*cursor_ids(args), None, 3, None)  # mid-batch without a batch digest
    with pytest.raises(CursorError, match='changed since'):
        ask(ws, **args, cursor=forged)


def cursor_ids(args, scope=SCOPE):
    request = {'line': None, 'column': None, 'name': None, **args}
    return (code_references._digest([code_references.VERSION, *(request[k] for k in (
        'mode', 'path', 'line', 'column', 'name'))]), code_references._digest([code_references.NAME, scope]))


def test_reference_pages_resume_inside_and_across_python_xref_batches(project, monkeypatch):
    ws = Workspace(project)
    whole = python_xref.references(ws, 'many.py:f', limit=100)['untrusted_references']
    assert len(whole) == 61
    monkeypatch.setattr(code_references, 'BATCH', 7)
    pages = walk(ws, 4096, mode='references', path='many.py', name='f')
    assert rows(pages, 'untrusted_references') == whole
    assert max(len(p['untrusted_references']) for p in pages) == 5
    assert all(chars(p) <= page_chars(4096) for p in pages)
    # Mid-batch in the first batch, the batch digest detects a change ...
    args = {'mode': 'references', 'path': 'many.py', 'name': 'f'}
    first = ask(ws, 4096, **args)
    # ... and at a batch boundary python_xref's own cursor does.
    boundary = ask(ws, 4096, **args, cursor=first['cursor'])
    assert len(boundary['untrusted_references']) == 2  # rows 5 and 6 end the first batch of 7
    write(project, 'many.py', MANY + 'f()\n')
    for cursor in (first['cursor'], boundary['cursor']):
        with pytest.raises(CursorError, match='changed since'):
            ask(ws, 4096, **args, cursor=cursor)


def test_limit_is_clamped_by_the_window_and_pages_fit_encode_result(project):
    ws = Workspace(project)
    args = {'mode': 'references', 'path': 'many.py', 'name': 'f', 'limit': 50}
    small = ask(ws, 4096, **args)
    assert len(small['untrusted_references']) == 5 and small['coverage']['match_limit'] == 5
    middle = ask(ws, 8192, **args)
    shown = len(middle['untrusted_references'])
    assert middle['coverage']['match_limit'] == 20 and 1 <= shown <= 20
    assert shown == 20 or middle['coverage']['page_char_clipped']
    large = ask(ws, 32768, **args)
    assert len(large['untrusted_references']) == 50 and large['cursor']
    for budget, result, limit in ((4096, small, 6500), (8192, middle, 6500), (32768, large, 32768)):
        assert chars(result) <= page_chars(budget)
        # The runner keeps a paged result whole; nothing is filtered or replaced.
        assert json.loads(encode_result(result, limit=limit)) == result
    assert {'partial', 'failed', 'deferred', 'hops_limited', 'scan_complete'} <= set(large['coverage'])


def test_serialized_size_shortens_pages_and_rows_stay_reachable(project, monkeypatch):
    ws = Workspace(project)
    monkeypatch.setattr(code_references, 'page_chars', lambda budget: 3000)
    pages = walk(ws, 32768, mode='references', path='many.py', name='f')
    assert all(chars(p) <= 3000 for p in pages)
    assert pages[0]['coverage']['page_char_clipped'] and len(pages[0]['untrusted_references']) < 50
    assert rows(pages, 'untrusted_references') == python_xref.references(
        ws, 'many.py:f', limit=100)['untrusted_references']


def test_definition_pages_bind_the_answer_state(project):
    ws = Workspace(project)
    write(project, 'multi.py', ''.join(f'value = {i}\n' for i in range(8)) + 'print(value)\n')
    expected = python_xref.definition(ws, 'multi.py', name='value')['untrusted_definitions']
    assert len(expected) == 8
    pages = walk(ws, 4096, mode='definition', path='multi.py', name='value')
    assert [len(p['untrusted_definitions']) for p in pages] == [5, 3]
    assert rows(pages, 'untrusted_definitions') == expected
    first = pages[0]
    write(project, 'multi.py', 'value = -1\n' + ''.join(f'value = {i}\n' for i in range(8)))
    with pytest.raises(CursorError, match='changed since'):
        ask(ws, 4096, mode='definition', path='multi.py', name='value', cursor=first['cursor'])


def test_diagnostics_and_imports_match_python_xref_and_shorten_long_names(project, monkeypatch):
    ws = Workspace(project)
    found = ask(ws, mode='diagnostics', path='app/uses.py')
    assert found['untrusted_diagnostics'] == python_xref.diagnostics(ws, 'app/uses.py')['diagnostics']
    assert [(d['code'], d['line']) for d in found['untrusted_diagnostics']] == [('unresolved_import', 2)]
    assert (found['path'], found['file_hash'], found['parse']) == ('app/uses.py', sha(USES), 'parsed')
    table = ask(ws, mode='imports', path='app/uses.py')
    assert [(r['module'], r['status'], r.get('target')) for r in table['untrusted_imports']] == [
        ('app.models', 'resolved', 'app/models.py'), ('missing', 'unresolved', None)]
    assert table['coverage']['complete'] and table['cursor'] is None
    long = 'a' * 300
    write(project, 'app/long.py', f'from .{long} import x\nimport {long}\n')
    diagnosed = ask(ws, mode='diagnostics', path='app/long.py')['untrusted_diagnostics'][0]
    assert len(diagnosed['module']) == code_references.NAME_CHARS and diagnosed['shortened_to_fit']
    assert len(diagnosed['message']) <= code_references.MESSAGE_CHARS
    imported = ask(ws, mode='imports', path='app/long.py')['untrusted_imports']
    assert all(len(r['module']) == code_references.NAME_CHARS and r['shortened_to_fit'] for r in imported)
    # A capped table is reported, never silently shortened.
    monkeypatch.setattr(code_references, 'MAX_ROWS', 1)
    capped = ask(ws, mode='imports', path='app/uses.py')
    assert len(capped['untrusted_imports']) == 1 and capped['cursor'] is None
    assert capped['coverage']['rows_truncated'] and capped['coverage']['rows_available'] == 2
    assert not capped['coverage']['complete']


def test_long_identifiers_are_shortened_for_display_only(project):
    ws = Workspace(project)
    name = 'Long' + 'x' * 300
    text = write(project, 'longname.py', f'class {name}:\n    pass\n\n\n{name}()\n')
    found = ask(ws, mode='definition', path='longname.py', line=5, column=1)
    row = found['untrusted_definitions'][0]
    assert (row['line'], row['column'], row['qualname_truncated']) == (1, 1, True)
    assert len(row['qualname']) == code_references.NAME_CHARS and row['file_hash'] == sha(text)
    assert found['query']['identifier_truncated'] and len(found['query']['identifier']) == code_references.NAME_CHARS
    target = ask(ws, mode='references', path='longname.py', line=1, column=1)['target']
    assert target['qualname_truncated'] and target['name_truncated'] and len(target['name']) == 160


def test_private_hidden_and_excluded_paths_are_refused_before_any_read(project, untouched):
    ws = Workspace(project)
    for path in ('secrets/a.py', '.env.py', 'node_modules/p/a.py', '.hidden/a.py', 'pkg/credentials/a.py',
                 ' secrets/a.py', './.ssh/a.py', 'certs.pem/a.py', 'build/a.py', 'pkg/.venv/a.py',
                 'conf/.envrc.py'):
        for mode in code_references.MODES:
            args = {'mode': mode, 'path': path, **({'name': 'a'} if mode in ('definition', 'references') else {})}
            with pytest.raises(PolicyError, match='Private, hidden and excluded'):
                ask(ws, **args)


def test_withheld_paths_never_leave_the_tool(project, monkeypatch):
    ws = Workspace(project)
    real_references, real_definition, real_imports = (python_xref.references, python_xref.definition,
                                                      python_xref.imports)

    def leaky_references(*args, **kwargs):
        found = real_references(*args, **kwargs)
        found['untrusted_references'].append({**found['untrusted_references'][0], 'path': 'secrets/leak.py'})
        return found

    def leaky_definition(*args, **kwargs):
        found = real_definition(*args, **kwargs)
        found['untrusted_definitions'].append({**found['untrusted_definitions'][0], 'path': 'credentials/x.py'})
        found['via'].append({'step': 'import', 'path': '.hidden/x.py', 'line': 1, 'column': 1})
        found['files']['node_modules/x.py'] = {'file_hash': 'f' * 64, 'basis': 'disk', 'parse': 'parsed'}
        return found

    def leaky_imports(*args, **kwargs):
        found = real_imports(*args, **kwargs)
        found['imports'].append({**found['imports'][0], 'target': 'build', 'target_kind': 'namespace_package'})
        return found
    monkeypatch.setattr(python_xref, 'references', leaky_references)
    monkeypatch.setattr(python_xref, 'definition', leaky_definition)
    monkeypatch.setattr(python_xref, 'imports', leaky_imports)
    line, column = where(MODELS, 'assist(value)')
    for args, withheld in (({'mode': 'references', 'path': 'app/models.py', 'name': 'Ledger.add'}, 1),
                           ({'mode': 'definition', 'path': 'app/models.py', 'line': line, 'column': column}, 3),
                           ({'mode': 'imports', 'path': 'app/uses.py'}, 1)):
        result = ask(ws, **args)
        text = json.dumps(result)
        assert all(word not in text for word in ('secrets/', 'credentials/', '.hidden/', 'node_modules/',
                                                 '"build"'))
        assert result['coverage']['paths_withheld'] == withheld and not result['coverage']['complete']


def test_a_row_withheld_in_an_earlier_batch_keeps_later_pages_incomplete(project, monkeypatch):
    ws = Workspace(project)
    real = python_xref.references

    def leaky(*args, **kwargs):
        found = real(*args, **kwargs)
        if kwargs.get('cursor') is None:  # only the first python_xref batch
            found['untrusted_references'].append({**found['untrusted_references'][0], 'path': 'secrets/leak.py'})
        return found
    monkeypatch.setattr(python_xref, 'references', leaky)
    monkeypatch.setattr(code_references, 'BATCH', 7)
    pages = walk(ws, 4096, mode='references', path='many.py', name='f')
    assert len(rows(pages, 'untrusted_references')) == 61 and 'secrets/' not in json.dumps(pages)
    assert all(p['coverage']['paths_withheld'] == 1 and not p['coverage']['complete'] for p in pages)


def test_arguments_are_validated_strictly_before_any_read(project, untouched):
    ws = Workspace(project)
    base = {'mode': 'definition', 'path': 'app/models.py', 'line': 3, 'column': 1}
    bad = [
        None, [], 'definition', {}, {'path': 'app/models.py'}, {'mode': 'definition'},
        {**base, 'mode': 'callers'}, {**base, 'mode': ['definition']}, {**base, 'extra': 1}, {**base, 1: 2},
        {**base, 'path': ''}, {**base, 'path': 'a' * 510 + '.py'}, {**base, 'path': 'a\n.py'},
        {**base, 'path': 'a\x00.py'}, {**base, 'path': 7},
        {**base, 'line': 0}, {**base, 'line': True}, {**base, 'line': 96_001}, {**base, 'line': 2.0},
        {**base, 'column': -1}, {**base, 'column': '3'},
        {'mode': 'definition', 'path': 'app/models.py', 'name': ''},
        {'mode': 'definition', 'path': 'app/models.py', 'name': 'x' * 257},
        {'mode': 'definition', 'path': 'app/models.py', 'name': 'a\nb'},
        {'mode': 'definition', 'path': 'app/models.py', 'name': '\ud800'},
        {**base, 'cursor': ''}, {**base, 'cursor': 'c' * 1025}, {**base, 'limit': 0}, {**base, 'limit': 51},
        {**base, 'limit': False},
        {'mode': 'definition', 'path': 'app/models.py', 'line': 3},
        {'mode': 'definition', 'path': 'app/models.py', 'column': 3},
        {'mode': 'definition', 'path': 'app/models.py', 'column': 3, 'name': 'x'},
        {'mode': 'references', 'path': 'app/models.py'},
        {'mode': 'references', 'path': 'app/models.py', 'line': 8},
        {'mode': 'references', 'path': 'app/models.py', 'name': 'Ledger.add', 'line': 8, 'column': 5},
        {'mode': 'diagnostics', 'path': 'app/models.py', 'line': 1},
        {'mode': 'imports', 'path': 'app/models.py', 'name': 'x'},
    ]
    for args in bad:
        with pytest.raises(PolicyError):
            run(ws, args, scope=SCOPE, budget=32768, cancel=None)
    args = {'mode': 'imports', 'path': 'app/models.py', 'limit': 3}
    snapshot = dict(args)
    with pytest.raises(AssertionError, match='python_xref was called'):
        run(ws, args, scope=SCOPE, budget=32768, cancel=None)
    assert args == snapshot  # arguments are never rewritten


def test_python_xref_refusals_pass_through(project):
    ws = Workspace(project)
    for args, message in (({'mode': 'imports', 'path': '../x.py'}, 'relative'),
                          ({'mode': 'imports', 'path': 'notes.md'}, '.py or .pyi'),
                          ({'mode': 'diagnostics', 'path': 'app/absent.py'}, 'not in the selected project'),
                          ({'mode': 'references', 'path': 'app/models.py', 'name': 'Ledger.absent'},
                           'No module- or class-level definition'),
                          ({'mode': 'definition', 'path': 'app/models.py', 'name': 'a b'}, 'Python name')):
        with pytest.raises(PolicyError, match=message):
            ask(ws, **args)
    result = ask(ws, mode='definition', path='app/models.py', line=1, column=1)
    assert result['status'] == 'not_an_identifier' and result['untrusted_definitions'] == []


def test_cancellation_is_honoured_before_during_and_after_parsing(project, monkeypatch):
    ws = Workspace(project)
    args = {'mode': 'references', 'path': 'app/models.py', 'name': 'Ledger.add'}
    stop = threading.Event()
    stop.set()
    for cancel in (stop, lambda: True):
        with pytest.raises(InterruptedError, match='Stopped by you'):
            ask(ws, cancel=cancel, **args)
    calls = []
    real = python_xref.definition

    def stopped_midway(*a, **k):
        calls.append(k['cancel'])
        found = real(*a, **k)
        late.set()  # the person stops while the answer is being prepared
        return found
    late = threading.Event()
    monkeypatch.setattr(python_xref, 'definition', stopped_midway)
    with pytest.raises(InterruptedError):
        ask(ws, cancel=late, mode='definition', path='app/uses.py', name='Ledger')
    assert calls == [late]  # python_xref receives the same cancel and checks it while walking and parsing


def test_answers_are_deterministic(project):
    ws = Workspace(project)
    line, column = where(MODELS, 'assist(value)')
    for args in ({'mode': 'references', 'path': 'many.py', 'name': 'f', 'limit': 7},
                 {'mode': 'definition', 'path': 'app/models.py', 'line': line, 'column': column},
                 {'mode': 'diagnostics', 'path': 'app/uses.py'}, {'mode': 'imports', 'path': 'app/models.py'}):
        assert json.dumps(ask(ws, **args)) == json.dumps(ask(ws, **args))


def test_answers_do_not_create_a_read_witness_or_edit_permission(tmp_path):
    from pebble.app import App
    root = tmp_path / 'project'
    root.mkdir()
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        chat = app.new_chat(str(root))
        ws = Workspace(root)
        (root / 'a.py').write_text('def needle():\n    return 1\n\n\nneedle()\n')
        rid = app.runs.create(chat['id'], 'Find references', app.runtime_config(chat, False))
        execution, token = app.controller.enter(app.runs.get(rid))
        app.active, app.active_run = chat['id'], rid
        try:
            for args in ({'mode': 'definition', 'path': 'a.py', 'line': 5, 'column': 1},
                         {'mode': 'references', 'path': 'a.py', 'name': 'needle'},
                         {'mode': 'diagnostics', 'path': 'a.py'}, {'mode': 'imports', 'path': 'a.py'}):
                result = run(ws, args, scope=(chat['id'], rid), budget=32768, cancel=app.cancel)
                assert result['authorization'] == 'none' and result['untrusted'] is True
            assert not app.provenance.records(rid)
            assert not ws.drafts
            with pytest.raises(PolicyError, match='Read it again'):
                app.execute('Write', {'file_path': 'a.py', 'content': 'overwrite'}, ws, chat['id'], call_id='write')
            assert not app.store.actions(chat['id'])
        finally:
            app.active = app.active_run = None
            app.controller.leave(execution, token)
    finally:
        app.close()
