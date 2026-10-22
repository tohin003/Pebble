"""python_xref: ast-only imports, cross-file definitions, graded references and diagnostics."""

import hashlib
import json
import threading

import pytest

from pebble import python_xref
from pebble.policy import PolicyError, Workspace
from pebble.python_xref import CursorError, definition, diagnostics, imports, references

INIT = '''\
"""Package facade."""
from .models import Ledger as Book
from .util import *

__all__ = ['Book', 'helper', 'CONSTANT']
'''
MODELS = '''\
"""Models."""
import os
from . import util
from .util import helper as assist, CONSTANT


class Base:
    def total(self):
        return 0


class Ledger(Base):
    rate = 2

    def __init__(self, entries):
        self.entries = entries

    def add(self, value):
        self.entries.append(value)
        return self.total() + assist(value) + CONSTANT

    @classmethod
    def build(cls):
        return cls([])


def make():
    book = Ledger([])
    book.add(1)
    return Ledger.rate, util.helper(2), os.sep
'''
UTIL = '''\
CONSTANT = 3


def helper(value):
    """Doubles."""
    return value * 2


def _private_helper():
    pass


class Tally:
    def add(self):
        return 1


Tally().add()
'''
DEEP = '''\
import json
import app.models as models_alias
import app.nope
from app import Book, helper
from ..models import Ledger, make
from .missing_mod import nothing
from ..util import not_there
from ... import top_level
from .... import beyond


def run(unknown_thing):
    ledger = Ledger([])
    ledger.add(2)
    models_alias.make()
    unknown_thing.add(3)
    text = "Ledger add"  # add in a comment
    ", ".join([])
    return Book, helper, json.dumps
'''
TOOL = '''\
from app.models import Ledger


def main():
    return Ledger([]).add(5)
'''


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write(root, name, text):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return text


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    for name, text in {'app/__init__.py': INIT, 'app/models.py': MODELS, 'app/util.py': UTIL,
                       'app/sub/__init__.py': '', 'app/sub/deep.py': DEEP, 'scripts/tool.py': TOOL,
                       'top_level.py': 'VALUE = 1\n'}.items():
        write(root, name, text)
    return root


def located(rows):
    return [(row['path'], row['line'], row['column'], row['kind'], row['qualname']) for row in rows]


def where(text, needle, occurrence=1):
    """1-based (line, column) of the n-th occurrence of ``needle``."""
    index = -1
    for _ in range(occurrence):
        index = text.index(needle, index + 1)
    line = text.count('\n', 0, index) + 1
    return line, index - (text.rfind('\n', 0, index) + 1) + 1


def test_import_table_resolves_only_inside_the_root(project):
    table = imports(Workspace(project), 'app/sub/deep.py')
    rows = [(r['line'], r['statement'], r['module'], r['level'], r['name'], r['binding'], r['status'],
             r.get('target'), r.get('reason')) for r in table['imports']]
    assert rows == [
        (1, 'import', 'json', 0, None, 'json', 'external', None, None),
        (2, 'import', 'app.models', 0, None, 'models_alias', 'resolved', 'app/models.py', None),
        (3, 'import', 'app.nope', 0, None, 'app', 'unresolved', None, 'missing'),
        (4, 'from', 'app', 0, 'Book', 'Book', 'resolved', 'app/__init__.py', None),
        (4, 'from', 'app', 0, 'helper', 'helper', 'resolved', 'app/__init__.py', None),
        (5, 'from', 'models', 2, 'Ledger', 'Ledger', 'resolved', 'app/models.py', None),
        (5, 'from', 'models', 2, 'make', 'make', 'resolved', 'app/models.py', None),
        (6, 'from', 'missing_mod', 1, 'nothing', 'nothing', 'unresolved', None, 'missing'),
        (7, 'from', 'util', 2, 'not_there', 'not_there', 'resolved', 'app/util.py', None),
        (8, 'from', '', 3, 'top_level', 'top_level', 'resolved', '.', None),
        (9, 'from', '', 4, 'beyond', 'beyond', 'unresolved', None, 'beyond_root'),
    ]
    assert table['file_hash'] == sha(DEEP) and table['basis'] == 'disk' and table['coverage']['complete']
    # A script outside any package resolves absolute imports from the project root.
    tool = imports(Workspace(project), 'scripts/tool.py')['imports']
    assert [(r['module'], r['status'], r['target']) for r in tool] == [('app.models', 'resolved', 'app/models.py')]


def test_definition_follows_local_module_import_then_target_outline(project):
    ws = Workspace(project)
    line, column = where(MODELS, 'assist(value)')
    found = definition(ws, 'app/models.py', line, column)
    assert found['status'] == 'resolved' and found['grade'] == 'resolved'
    assert located(found['untrusted_definitions']) == [('app/util.py', 4, 1, 'function', 'helper')]
    row = found['untrusted_definitions'][0]
    assert (row['file_hash'], row['basis'], row['parse'], row['end_line']) == (sha(UTIL), 'disk', 'parsed', 6)
    assert row['text'] == 'def helper(value):'
    assert [step['step'] for step in found['via']] == ['module', 'import', 'outline']
    assert found['files'] == {'app/models.py': {'file_hash': sha(MODELS), 'basis': 'disk', 'parse': 'parsed'},
                              'app/util.py': {'file_hash': sha(UTIL), 'basis': 'disk', 'parse': 'parsed'}}
    assert found['coverage']['complete'] and 'grants no Read witness' in found['note']
    # Local scope wins over module level: `value` is the parameter of add().
    line, column = where(MODELS, 'append(value)')
    local = definition(ws, 'app/models.py', line, column + len('append('))
    assert located(local['untrusted_definitions']) == [('app/models.py', 18, 19, 'parameter', 'Ledger.add.<locals>.value')]
    assert local['via'][0]['step'] == 'local'


def test_definitions_cross_reexports_relative_imports_and_packages(project):
    ws = Workspace(project)
    line, column = where(DEEP, 'Book, helper, json')
    book = definition(ws, 'app/sub/deep.py', line, column)
    assert located(book['untrusted_definitions']) == [('app/models.py', 12, 1, 'class', 'Ledger')]
    assert [s['step'] for s in book['via']] == ['module', 'import', 'outline', 'import', 'outline']
    # `helper` is not bound in app/__init__.py; its star import from .util (listed in __all__) binds it.
    line, column = where(DEEP, 'helper, json')
    star = definition(ws, 'app/sub/deep.py', line, column)
    assert located(star['untrusted_definitions']) == [('app/util.py', 4, 1, 'function', 'helper')]
    assert 'star' in [s['step'] for s in star['via']]
    module = definition(ws, 'app/sub/deep.py', 2, where(DEEP, 'models as')[1] + 4)
    assert module['untrusted_definitions'][0] == {'path': 'app/models.py', 'kind': 'module', 'line': 1, 'column': 1,
                                                  'file_hash': sha(MODELS), 'basis': 'disk', 'parse': 'parsed'}
    beyond = definition(ws, 'app/sub/deep.py', 9, where(DEEP, 'beyond')[1])
    assert beyond['status'] == 'unresolved' and beyond['reasons'] == ['unresolved_import:beyond_root']
    external = definition(ws, 'app/sub/deep.py', *where(DEEP, 'dumps'))
    assert (external['status'], external['external']) == ('external', ['json.dumps'])


def test_attributes_resolve_through_self_bases_classmethods_and_constructor_assignments(project):
    ws = Workspace(project)
    total = definition(ws, 'app/models.py', *where(MODELS, 'total() +'))
    assert located(total['untrusted_definitions']) == [('app/models.py', 8, 5, 'method', 'Base.total')]
    assert 'member' in [s['step'] for s in total['via']]
    added = definition(ws, 'app/models.py', *where(MODELS, 'add(1)'))
    assert located(added['untrusted_definitions']) == [('app/models.py', 18, 5, 'method', 'Ledger.add')]
    entries = definition(ws, 'app/models.py', *where(MODELS, 'entries.append'))
    assert located(entries['untrusted_definitions']) == [('app/models.py', 16, 14, 'attribute', 'Ledger.entries')]
    rate = definition(ws, 'app/models.py', *where(MODELS, 'rate, util'))
    assert located(rate['untrusted_definitions']) == [('app/models.py', 13, 5, 'variable', 'Ledger.rate')]
    via_module = definition(ws, 'app/models.py', *where(MODELS, 'helper(2)'))
    assert located(via_module['untrusted_definitions']) == [('app/util.py', 4, 1, 'function', 'helper')]
    untyped = definition(ws, 'app/sub/deep.py', *where(DEEP, 'add(3)'))
    assert untyped['status'] == 'unresolved' and untyped['reasons'] == ['untyped_receiver']
    assert not untyped['untrusted_definitions']


def test_lookup_by_name_dotted_builtin_and_unbound(project):
    ws = Workspace(project)
    method = definition(ws, 'app/models.py', name='Ledger.add')
    assert located(method['untrusted_definitions']) == [('app/models.py', 18, 5, 'method', 'Ledger.add')]
    assert located(definition(ws, 'app/models.py', name='util.helper')['untrusted_definitions']) == [
        ('app/util.py', 4, 1, 'function', 'helper')]
    assert definition(ws, 'app/models.py', name='os.path')['external'] == ['os.path']
    builtin = definition(ws, 'app/models.py', name='len')
    assert (builtin['status'], builtin['builtin']) == ('builtin', ['len'])
    missing = definition(ws, 'app/models.py', name='nothing_here')
    assert (missing['status'], missing['reasons']) == ('unresolved', ['unbound'])
    # A name plus a line resolves in the scope enclosing that line.
    scoped = definition(ws, 'app/models.py', line=20, name='value')
    assert located(scoped['untrusted_definitions'])[0][3] == 'parameter'
    private = definition(ws, 'app/__init__.py', name='_private_helper')
    assert private['status'] == 'unresolved'  # listed __all__ does not export it


def test_references_are_graded_ordered_and_hash_pinned(project):
    ws = Workspace(project)
    result = references(ws, 'app/models.py:Ledger.add')
    assert result['target'] == {'path': 'app/models.py', 'name': 'add', 'qualname': 'Ledger.add', 'line': 18,
                                'column': 5, 'kind': 'method', 'file_hash': sha(MODELS), 'basis': 'disk'}
    rows = [(r['grade'], r['path'], r['line'], r['role'], r.get('reason')) for r in result['untrusted_references']]
    assert rows == [
        ('resolved', 'app/models.py', 18, 'definition', None),
        ('resolved', 'app/models.py', 29, 'attribute', None),  # book = Ledger([]); book.add(1)
        ('resolved', 'app/sub/deep.py', 14, 'attribute', None),  # ledger = Ledger([]); ledger.add(2)
        ('resolved', 'scripts/tool.py', 5, 'attribute', None),  # Ledger([]).add(5)
        ('attribute_candidate', 'app/sub/deep.py', 16, 'attribute', 'untyped_receiver'),
        ('textual', 'app/sub/deep.py', 17, 'text', 'text'),
        ('textual', 'app/sub/deep.py', 17, 'text', 'text'),
    ]
    hashes = {r['path']: r['file_hash'] for r in result['untrusted_references']}
    assert hashes == {'app/models.py': sha(MODELS), 'app/sub/deep.py': sha(DEEP), 'scripts/tool.py': sha(TOOL)}
    coverage = result['coverage']
    # Tally.add and its call resolve elsewhere: excluded, not hidden.
    assert coverage['excluded_other_bindings'] == 2
    assert coverage['grades'] == {'resolved': 4, 'attribute_candidate': 1, 'textual': 2}
    assert coverage['complete'] and coverage['searched'] == 7 and result['cursor'] is None
    assert set(result) == {'target', 'untrusted_references', 'coverage', 'cursor', 'note'}
    by_row = references(ws, {'path': 'app/models.py', 'line': 18, 'column': 5, 'file_hash': sha(MODELS)})
    assert by_row['untrusted_references'] == result['untrusted_references']


def test_references_to_module_functions_follow_aliases_and_star_reexports(project):
    result = references(Workspace(project), {'path': 'app/util.py', 'qualname': 'helper'})
    rows = {(r['path'], r['line'], r['grade'], r['role']) for r in result['untrusted_references']}
    assert {('app/util.py', 4, 'resolved', 'definition'),
            ('app/models.py', 4, 'resolved', 'import_name'),  # from .util import helper as assist
            ('app/models.py', 30, 'resolved', 'attribute'),  # util.helper(2)
            ('app/sub/deep.py', 4, 'resolved', 'import_name'),  # from app import helper (star re-export)
            ('app/sub/deep.py', 19, 'resolved', 'name')} <= rows
    # `assist(value)` refers to it by another name, so it is not an occurrence of `helper`.
    assert not [r for r in result['untrusted_references'] if r['grade'] == 'attribute_candidate']
    textual = [r for r in result['untrusted_references'] if r['grade'] == 'textual']
    assert [(r['path'], r['line']) for r in textual] == [('app/__init__.py', 5)]  # inside __all__


def test_reference_pages_and_cursors_bind_target_scope_and_file_states(project):
    ws = Workspace(project)
    first = references(ws, 'app/models.py:Ledger.add', scope=('chat', 'run'), limit=3)
    assert len(first['untrusted_references']) == 3 and first['cursor']
    assert first['coverage']['matches_truncated'] and not first['coverage']['complete']
    second = references(ws, 'app/models.py:Ledger.add', scope=('chat', 'run'), limit=3, cursor=first['cursor'])
    third = references(ws, 'app/models.py:Ledger.add', scope=('chat', 'run'), limit=3, cursor=second['cursor'])
    whole = references(ws, 'app/models.py:Ledger.add')['untrusted_references']
    assert first['untrusted_references'] + second['untrusted_references'] + third['untrusted_references'] == whole
    assert third['cursor'] is None and third['coverage']['resumed']
    with pytest.raises(CursorError, match='another conversation'):
        references(ws, 'app/models.py:Ledger.add', scope=('chat', 'other'), limit=3, cursor=first['cursor'])
    with pytest.raises(CursorError, match='different symbol'):
        references(ws, 'app/util.py:helper', scope=('chat', 'run'), limit=3, cursor=first['cursor'])
    body, tag = first['cursor'].split('.')
    with pytest.raises(CursorError, match='altered, or was issued before a restart'):
        references(ws, 'app/models.py:Ledger.add', scope=('chat', 'run'), limit=3, cursor=body[:-2] + 'xx.' + tag)
    write(project, 'scripts/tool.py', TOOL + '\n# add\n')
    with pytest.raises(CursorError, match='changed since'):
        references(ws, 'app/models.py:Ledger.add', scope=('chat', 'run'), limit=3, cursor=first['cursor'])


def test_diagnostics_report_syntax_errors_and_unresolved_in_root_imports(project):
    ws = Workspace(project)
    result = diagnostics(ws, 'app/sub/deep.py')
    found = [(d['code'], d['line'], d.get('module'), d.get('name')) for d in result['diagnostics']]
    assert found == [
        ('unresolved_import', 3, 'app.nope', None),
        ('unresolved_import', 6, '.missing_mod', None),
        ('unresolved_import_name', 7, '..util', 'not_there'),
        ('unresolved_import', 9, '....', None),
    ]
    assert 'outside the selected project' in result['diagnostics'][-1]['message']
    coverage = result['coverage']
    assert coverage['external_imports'] == 1 and coverage['complete'] and result['file_hash'] == sha(DEEP)
    clean = diagnostics(ws, 'app/models.py')
    assert clean['diagnostics'] == [] and clean['coverage']['complete']
    write(project, 'app/broken.py', 'from .util import helper\nfrom .gone import x\n\ndef f(:\n    pass\n'
                                    'from .later import y\n')
    broken = diagnostics(ws, 'app/broken.py')
    assert [(d['code'], d['line']) for d in broken['diagnostics']] == [('unresolved_import', 2), ('syntax_error', 4)]
    assert broken['parse'] == 'partial' and broken['coverage']['checked_through_line'] == 3
    assert not broken['coverage']['complete']  # imports after the error were not checked


def test_private_paths_symlinks_large_and_excluded_files_never_contribute(project, tmp_path):
    ws_root = project
    write(ws_root, 'secrets/keys.py', 'def add():\n    pass\n')
    write(ws_root, '.env.py', 'add = 1\n')
    write(ws_root, 'node_modules/pkg/add.py', 'def add():\n    pass\n')
    write(ws_root, '.hidden/add.py', 'def add():\n    pass\n')
    outside = write(tmp_path, 'outside/linked.py', 'def add():\n    pass\n')
    (ws_root / 'linked.py').symlink_to(tmp_path / 'outside/linked.py')
    write(ws_root, 'big.py', '# add\n' + 'x = 1\n' * 20_000)
    ws = Workspace(ws_root)
    result = references(ws, 'app/models.py:Ledger.add')
    paths = {r['path'] for r in result['untrusted_references']}
    assert paths == {'app/models.py', 'app/sub/deep.py', 'scripts/tool.py'}
    assert result['coverage']['skip_reasons'] == {'large': 1} and not result['coverage']['complete']
    assert outside not in json.dumps(result)
    for private in ('secrets/keys.py', '.env.py', 'node_modules/pkg/add.py', '.hidden/add.py', 'linked.py'):
        with pytest.raises(PolicyError):
            definition(ws, private, name='add')
    # An import of a private package never resolves into it.
    write(ws_root, 'uses_secret.py', 'from secrets.keys import add\n')
    row = imports(ws, 'uses_secret.py')['imports'][0]
    assert row['status'] == 'external' and 'target' not in row


def test_budgets_and_hop_limits_are_reported_as_partial_coverage(project, monkeypatch):
    ws = Workspace(project)
    monkeypatch.setattr(python_xref, 'PARSE_BUDGET', len(MODELS.encode()) + 10)
    limited = references(ws, 'app/models.py:Ledger.add')
    coverage = limited['coverage']
    assert coverage['deferred'] >= 1 and not coverage['complete']
    deferred = [r for r in limited['untrusted_references'] if r.get('reason') == 'deferred']
    assert deferred and all(r['grade'] == 'textual' for r in deferred)
    monkeypatch.setattr(python_xref, 'PARSE_BUDGET', 8 * 1024 * 1024)
    for index in range(5):
        write(project, f'chain/m{index}.py', f'from .m{index + 1} import value\n')
    write(project, 'chain/m5.py', 'value = 5\n')
    write(project, 'chain/__init__.py', '')
    assert located(definition(ws, 'chain/m0.py', name='value')['untrusted_definitions']) == [
        ('chain/m5.py', 1, 1, 'variable', 'value')]
    monkeypatch.setattr(python_xref, 'MAX_HOPS', 2)
    hopped = definition(ws, 'chain/m0.py', name='value')
    assert hopped['status'] == 'unresolved' and 'hop_limit' in hopped['reasons']
    assert hopped['coverage']['hops_limited'] and not hopped['coverage']['complete']


def test_drafts_are_resolved_with_draft_hashes_and_stale_hashes_are_refused(project):
    ws = Workspace(project)
    draft = UTIL.replace('def helper(value):', 'def helper(value, scale=2):')
    ws.draft('app/util.py', draft)
    found = definition(ws, 'app/models.py', *where(MODELS, 'assist(value)'))
    row = found['untrusted_definitions'][0]
    assert (row['basis'], row['file_hash'], row['text']) == ('draft', sha(draft), 'def helper(value, scale=2):')
    with pytest.raises(PolicyError, match='changed since that hash'):
        definition(ws, 'app/models.py', name='Ledger', expected_hash=sha('something else'))
    with pytest.raises(PolicyError, match='No module- or class-level definition'):
        references(ws, {'path': 'app/models.py', 'qualname': 'Ledger.absent'})


def test_parsing_never_imports_or_executes_project_code(project, tmp_path):
    marker = tmp_path / 'ran.txt'
    write(project, 'app/side.py', f'import definitely_missing_xyz\nopen({str(marker)!r}, "w").write("x")\n'
                                  'raise SystemExit(3)\nfrom .util import helper\n')
    ws = Workspace(project)
    assert definition(ws, 'app/side.py', name='helper')['status'] == 'resolved'
    assert diagnostics(ws, 'app/side.py')['diagnostics'] == []
    references(ws, 'app/util.py:helper')
    assert not marker.exists()


def test_request_validation_and_cancellation(project):
    ws = Workspace(project)
    for bad in (dict(path='../x.py', name='a'), dict(path='/abs.py', name='a'), dict(path='notes.md', name='a'),
                dict(path='app/models.py'), dict(path='app/models.py', line=0, column=1),
                dict(path='app/models.py', name='a b'), dict(path='app/models.py', line=3)):
        with pytest.raises(PolicyError):
            definition(ws, **bad)
    with pytest.raises(PolicyError):
        references(ws, 'no-colon')
    with pytest.raises(PolicyError):
        references(ws, 'app/models.py:Ledger.add', limit=0)
    with pytest.raises(PolicyError, match='not in the selected project'):
        definition(ws, 'app/absent.py', name='x')
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        references(ws, 'app/models.py:Ledger.add', cancel=stop)
    with pytest.raises(InterruptedError):
        definition(ws, 'app/models.py', name='Ledger', cancel=lambda: True)
    assert definition(ws, 'app/models.py', 1, 1)['status'] == 'not_an_identifier'
