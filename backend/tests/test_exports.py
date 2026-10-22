"""Phase 17 track A2: verified local exports (design study §5, experiment E6)."""
import hashlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from pebble import export_worker as formats
from pebble import exports as module
from pebble.artifacts import Artifacts
from pebble.exports import ExportError, Exports, readback
from pebble.store import Store

FIXTURES = Path(__file__).parent / 'fixtures' / 'exports'


def fixture(name):
    text = (FIXTURES / name).read_text(encoding='utf-8')
    return json.loads(text) if name.endswith('.json') else text


@pytest.fixture
def env(tmp_path):
    home = tmp_path / 'home'
    store = Store(home)
    artifacts = Artifacts(store)
    events = []
    exports = Exports(home, store, artifacts, emit=lambda name, data: events.append((name, data)))
    cid = store.new_chat()
    out = tmp_path / 'out'
    out.mkdir()
    yield type('Env', (), {'home': home, 'store': store, 'artifacts': artifacts, 'exports': exports, 'cid': cid,
                           'out': out, 'events': events, 'tmp': tmp_path})
    store.close()


def run(env, source, fmt, options=None, cid=None):
    plan = env.exports.preview(cid or env.cid, source, fmt, options)
    return plan, env.exports.render(cid or env.cid, plan['plan_id'])


def artifact(env, kind, data, title='Fixture view'):
    return {'kind': 'artifact', 'id': env.artifacts.render(env.cid, title, kind, data)['id']}


def message(env, content, role='assistant'):
    return {'kind': 'message', 'id': env.store.message(env.cid, role, content)}


def export_path(env, receipt):
    return env.home / 'exports' / env.cid / receipt['export_id'] / receipt['file']['name']


def failed(receipt):
    return set(receipt['failed_checks'])


def verified(receipt):
    problems = [(c['name'], c['detail']) for c in receipt['checks'] if not c['passed']]
    assert receipt['status'] == 'verified', (receipt['status'], receipt['reason'], problems)
    return receipt


# Block model and plans


def test_markdown_message_normalizes_into_host_blocks():
    builder = module._Blocks()
    module.markdown_blocks(builder, fixture('message.md'))
    kinds = [block['type'] for block in builder.blocks]
    assert kinds == ['heading', 'paragraph', 'paragraph', 'heading', 'list', 'list', 'paragraph', 'code',
                     'heading', 'paragraph', 'heading', 'paragraph']
    intro = builder.blocks[1]['text']
    # Links and images become visible text, never a link or a fetch.
    assert 'project page (https://example.invalid/report?id=7)' in intro and '**' not in intro and '`' not in intro
    assert builder.blocks[2]['text'] == '[image: site photo]'
    assert builder.blocks[4] == {'type': 'list', 'ordered': False, 'items': [
        'Water levels rose at the northern site', 'Soil samples were dry at the south site and need a second visit',
        'Equipment logs are complete']}
    assert builder.blocks[5]['ordered'] is True and builder.blocks[7]['language'] == 'python'
    assert builder.blocks[8]['text'] == 'مرحبا بالعالم' and '北部站点' in builder.blocks[11]['text']
    table = module._Blocks()
    module.markdown_blocks(table, '| Item | Count |\n|---|:--:|\n| a | 007 |\n| b | 12 |\n| c | 1.5 | extra |\n| d |')
    assert table.blocks == [{'type': 'table', 'caption': None, 'columns': ['Item', 'Count'],
                             'rows': [['a', '007'], ['b', 12], ['c', 1.5], ['d', None]]}]


def test_preview_returns_exactly_the_specified_plan(env):
    source = artifact(env, 'table', fixture('hostile_table.json'))
    plan = env.exports.preview(env.cid, source, 'xlsx')
    assert set(plan) == {'plan_id', 'source', 'format', 'pages_estimate', 'deadline'}
    assert set(plan['source']) == {'kind', 'id', 'revision', 'digest'}
    assert plan['source']['kind'] == 'artifact' and plan['source']['revision'] == 1 and len(plan['source']['digest']) == 64
    assert plan['format'] == 'xlsx' and plan['pages_estimate'] >= 1 and plan['deadline'] > 0
    # A plan is single use, bound to its conversation, and expires.
    other = env.store.new_chat()
    with pytest.raises(ExportError, match='no longer available'):
        env.exports.render(other, plan['plan_id'])
    env.exports.render(env.cid, plan['plan_id'])
    with pytest.raises(ExportError, match='no longer available'):
        env.exports.render(env.cid, plan['plan_id'])
    stale = env.exports.preview(env.cid, source, 'csv')
    env.exports.plans[stale['plan_id']]['deadline'] = 0
    with pytest.raises(ExportError, match='expired'):
        env.exports.render(env.cid, stale['plan_id'])


def test_preview_refuses_unsupported_requests(env):
    table = artifact(env, 'table', fixture('hostile_table.json'))
    text = message(env, '# Notes\n\nNo tables here.')
    with pytest.raises(ExportError, match='later update'):
        env.exports.preview(env.cid, table, 'pptx')
    with pytest.raises(ExportError, match='Choose PDF'):
        env.exports.preview(env.cid, table, 'exe')
    with pytest.raises(ExportError, match='include_answers and paper'):
        env.exports.preview(env.cid, table, 'pdf', {'path': '/tmp/x.pdf'})
    with pytest.raises(ExportError, match='exactly one table'):
        env.exports.preview(env.cid, text, 'csv')
    with pytest.raises(ExportError, match='one to 32 tables'):
        env.exports.preview(env.cid, text, 'xlsx')
    with pytest.raises(ExportError):
        env.exports.preview(env.cid, {'kind': 'file', 'id': 'x'}, 'html')
    with pytest.raises(ValueError, match='not found'):
        env.exports.preview(env.store.new_chat(), table, 'html')
    with pytest.raises(ExportError, match='revision 1'):
        env.exports.preview(env.cid, {**table, 'revision': 2}, 'html')
    with pytest.raises(ExportError, match='not available in this build'):
        env.exports.preview(env.cid, {'kind': 'goal_report', 'id': 'g1'}, 'html')
    with pytest.raises(ExportError, match='not available in this build'):
        env.exports.preview(env.cid, {'kind': 'run_report', 'id': 'r1'}, 'html')
    host = env.store.message(env.cid, 'tool', 'hidden host text')
    with pytest.raises(ExportError, match='not found'):
        env.exports.preview(env.cid, {'kind': 'message', 'id': host}, 'html')


# Spreadsheets


def test_xlsx_has_typed_cells_no_formulas_and_verifies(env):
    data = fixture('hostile_table.json')
    plan, receipt = run(env, artifact(env, 'table', data, 'Readings'), 'xlsx')
    verified(receipt)
    assert receipt['published'] is False and receipt['authorization'] == 'none'
    assert receipt['source'] == plan['source'] and receipt['file']['suggested_name'] == 'Readings.xlsx'
    assert 'Whole numbers longer than 15 digits were stored as text so no digits are lost.' in receipt['notes']
    names = {check['name'] for check in receipt['checks']}
    assert {'cells_match', 'no_formulas', 'formulas_neutralized', 'no_macros', 'no_external_refs', 'text_coverage'} <= names
    raw = export_path(env, receipt).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == receipt['file']['sha256']
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        sheet = archive.read('xl/worksheets/sheet1.xml').decode()
        workbook = archive.read('xl/workbook.xml').decode()
        assert not any('External' in archive.read(n).decode() for n in archive.namelist() if n.endswith('.rels'))
    assert '<f>' not in sheet and 'sharedStrings' not in workbook and 'name="Readings  north  draft"' not in workbook
    assert 'name="Readings north draft"' in workbook  # [ ] : are invalid in sheet names
    # Formula-like text stays text, protected by quotePrefix; numbers and booleans keep their types.
    assert '<c r="A2" t="inlineStr" s="1"><is><t xml:space="preserve">=1+2</t></is></c>' in sheet
    assert '<c r="C1" t="inlineStr" s="3">' in sheet
    assert '<c r="B2"><v>3</v></c>' in sheet and '<c r="B3"><v>-4.25</v></c>' in sheet
    assert '<c r="B4" t="inlineStr"><is><t xml:space="preserve">9007199254740993</t></is></c>' in sheet
    assert '<c r="B5" t="b"><v>1</v></c>' in sheet and 'r="B6"' not in sheet
    assert '<c r="A7" t="inlineStr"><is><t xml:space="preserve">-5</t></is></c>' in sheet
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in sheet
    layout = receipt['readback']['layout'][0]
    assert layout == {'name': 'Readings north draft', 'rows': 7, 'columns': 3, 'formulas': 0, 'unprotected_triggers': 0,
                      'cells_match': True, 'first_mismatch': None}
    assert set(receipt['readback']) == {'sheets', 'text_coverage', 'blank_pages', 'external_refs', 'layout'}


def test_csv_neutralizes_formulas_and_keeps_utf8(env):
    plan, receipt = run(env, artifact(env, 'table', fixture('hostile_table.json')), 'csv')
    verified(receipt)
    raw = export_path(env, receipt).read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')
    text = raw.decode('utf-8-sig')
    assert text.startswith('Site,Value,"\'=HYPERLINK(""https://example.invalid"")"\r\n')
    for cell in ("'=1+2", "'+cmd|' /C calc'!A0", "'-2+3", "'@SUM(A1:A2)", "'\tTabbed", "'＝full width"):
        assert cell in text
    assert ',-4.25,' in text and '\r\n-5,0.1,plain text\r\n' in text and 'مرحبا,true,你好世界' in text
    assert receipt['readback']['layout'][0]['unprotected_triggers'] == 0


def test_large_table_exports_to_sheets_but_not_documents(env):
    rows = '\n'.join(f'| row {n} | {n} |' for n in range(10_000))
    source = message(env, '# Big\n\n| Name | Count |\n|---|---|\n' + rows)
    plan, receipt = run(env, source, 'xlsx')
    verified(receipt)
    assert receipt['readback']['layout'][0]['rows'] == 10_001 and plan['pages_estimate'] > 200
    for fmt in ('docx', 'html', 'pdf'):
        with pytest.raises(ExportError, match='2,000 table rows'):
            env.exports.preview(env.cid, source, fmt)
    too_big = message(env, '| A |\n|---|\n' + '| x |\n' * 10_001)
    with pytest.raises(ExportError, match='10,000 rows'):
        env.exports.preview(env.cid, too_big, 'xlsx')


# Static HTML


def test_html_is_static_with_csp_and_no_scripts(env):
    plan, receipt = run(env, artifact(env, 'html', fixture('hostile_view.json'), 'Status board'), 'html')
    verified(receipt)
    assert "Exports never include scripts; this view's script was left out." in receipt['notes']
    page = export_path(env, receipt).read_text()
    lowered = page.lower()
    for marker in ('<script', 'onclick', 'onerror', '<iframe', 'srcdoc', '<img', '<form', '<input', '<button',
                   'javascript:', 'https://example.invalid', '__tauri__', 'url('):
        assert marker not in lowered, marker
    assert f'<meta http-equiv="Content-Security-Policy" content="{formats.CSP.replace(chr(39), "&#x27;")}">' in page
    assert '<h2 class="lead">Status board</h2>' in page and 'Click to approve' in page and '<td colspan="2">Ready</td>' in page
    assert 'h2{color:#225544}' in page
    body = page.split('\n<body>\n', 1)[1][:-len(formats.TAIL)]
    assert formats.sanitize(body) == body
    names = {check['name']: check['passed'] for check in receipt['checks']}
    assert names['resanitized_equal'] and names['csp'] and names['no_scripts'] and names['head_matches']


def test_html_readback_flags_tampering(env):
    plan, receipt = run(env, message(env, '# Hello\n\nPlain words only.'), 'html')
    path = export_path(env, receipt)
    page = path.read_text()
    hostile = env.tmp / 'tampered.html'
    hostile.write_text(page.replace('Plain words only.', 'Plain words only.<script>x()</script><img src="https://example.invalid/a">'
                                    '<p onclick="x()">t</p>'))
    expect = {'text': 'Hello Plain words only.', 'head_sha256': hashlib.sha256(page.split('\n<body>\n')[0].encode() + b'\n<body>\n').hexdigest()}
    result = readback(hostile, 'html', expect)
    document = result['layout'][0]
    assert document['scripts'] == 1 and document['event_handlers'] == 1 and document['resanitized_equal'] is False
    assert {ref['kind'] for ref in result['external_refs']} >= {'img', 'reference'}
    outcome = {check['name']: check['passed'] for check in module.checks('html', result, expect)}
    assert outcome['no_scripts'] is False and outcome['resanitized_equal'] is False and outcome['no_external_refs'] is False
    assert outcome['head_matches'] is True
    # Escaped source text that merely mentions a script is not a script.
    _, quoted = run(env, message(env, 'Use `<script src="https://example.invalid/x.js">` with care.'), 'html')
    verified(quoted)


def test_quiz_exports_without_its_key_unless_the_person_opts_in(env):
    source = artifact(env, 'quiz', fixture('quiz.json'), 'Science check')
    _, hidden = run(env, source, 'html')
    verified(hidden)
    page = export_path(env, hidden).read_text()
    assert 'Carbon dioxide' in page and 'Correct answer' not in page and 'by-product' not in page
    assert {'name': 'answer_key_absent', 'passed': True} == {k: v for k, v in next(c for c in hidden['checks'] if c['name'] == 'answer_key_absent').items() if k in ('name', 'passed')}
    assert any('Answers were left out' in note for note in hidden['notes'])
    _, shown = run(env, source, 'html', {'include_answers': True})
    verified(shown)
    page = export_path(env, shown).read_text()
    assert 'Correct answer: Carbon dioxide' in page and 'by-product' in page
    assert 'answer_key_absent' not in {c['name'] for c in shown['checks']}
    _, csv_hidden = run(env, source, 'docx')
    assert 'answer_key_absent' not in csv_hidden['failed_checks']


class _Views:
    """A stand-in artifact store for kinds that arrive with later tracks."""

    def __init__(self, kind, data):
        self.row = {'revision': 1, 'title': 'App demo', 'kind': kind, 'data': data, 'data_hash': 'h' * 64}

    def get(self, cid, artifact_id, revision=None):
        return dict(self.row)


def test_prototype_uses_target_navigation_and_refuses_active_keys(env):
    exports = Exports(env.home, env.store, _Views('prototype', fixture('prototype.json')))
    plan = exports.preview(env.cid, {'kind': 'artifact', 'id': 'demo'}, 'html')
    receipt = exports.render(env.cid, plan['plan_id'])
    verified(receipt)
    page = export_path(env, receipt).read_text()
    assert '.screen:target,.screen.start{display:block}' in page and '<script' not in page
    assert '<a class="button" href="#screen-login">Sign in</a>' in page
    assert '<nav><a href="#screen-home">Home</a> <a href="#screen-settings">Settings</a></nav>' in page
    # The start screen is last so a targeted screen can hide it without script.
    assert page.index('id="screen-login"') < page.index('id="screen-home" class="screen start"')
    assert 'Interactive mock-up' in page and '<input' not in page
    hostile = fixture('prototype.json')
    for change in ({'href': 'https://example.invalid'}, {'onclick': 'x()'}, {'style': 'color:red'}, {'goto': 'missing'}):
        broken = json.loads(json.dumps(hostile))
        broken['screens'][0]['components'][1].update(change)
        with pytest.raises(ExportError):
            Exports(env.home, env.store, _Views('prototype', broken)).preview(env.cid, {'kind': 'artifact', 'id': 'demo'}, 'html')
    with pytest.raises(ExportError, match='Dashboard exports'):
        Exports(env.home, env.store, _Views('dashboard', {'panels': []})).preview(env.cid, {'kind': 'artifact', 'id': 'd'}, 'html')


# Documents


def test_docx_uses_confined_textutil_and_verifies_rtl_and_cjk(env):
    plan, receipt = run(env, message(env, fixture('message.md')), 'docx')
    verified(receipt)
    assert receipt['readback']['text_coverage'] >= 0.98
    preview = next(entry for entry in receipt['readback']['layout'] if 'thumbnail' in entry)
    assert preview['source'] == 'quicklook' and preview['thumbnail']['blank'] is False
    with zipfile.ZipFile(export_path(env, receipt)) as archive:
        document = archive.read('word/document.xml').decode()
        rels = [archive.read(n).decode() for n in archive.namelist() if n.endswith('.rels')]
    assert 'مرحبا بالعالم' in document and '北部站点' in document
    assert 'https://example.invalid/report?id=7' in document and not any('External' in r for r in rels)


def test_docx_tables_are_honestly_unverified(env):
    _, receipt = run(env, artifact(env, 'table', fixture('hostile_table.json')), 'docx')
    assert receipt['status'] == 'unverified' and failed(receipt) == {'tables_preserved'}
    detail = next(c['detail'] for c in receipt['checks'] if c['name'] == 'tables_preserved')
    assert 'textutil writes Word tables as plain paragraphs' in detail
    assert receipt['file'] and export_path(env, receipt).is_file()


def test_docx_without_quicklook_is_unverified(env, monkeypatch):
    monkeypatch.setattr(module, '_quicklook', lambda path, cancel=None: None)
    _, receipt = run(env, message(env, '# Notes\n\nShort text.'), 'docx')
    assert receipt['status'] == 'unverified' and failed(receipt) == {'thumbnail'}
    assert next(c for c in receipt['checks'] if c['name'] == 'thumbnail')['detail'] == 'Quick Look could not render a preview.'


def test_readback_flags_external_and_macro_docx(env):
    parts = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                               '<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>',
        'word/_rels/document.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                        '<Relationship Id="r1" Type="http://x/attachedTemplate" Target="https://example.invalid/t.dotm" TargetMode="External"/></Relationships>',
        'word/document.xml': '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                             '<w:p><w:r><w:instrText> INCLUDEPICTURE "https://example.invalid/p.png" </w:instrText></w:r><w:r><w:t>Hello</w:t></w:r></w:p>'
                             '</w:body></w:document>',
        'word/vbaProject.bin': 'macro',
    }
    path = env.tmp / 'hostile.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    result = readback(path, 'docx', {'text': 'Hello'}, thumbnail=False)
    assert {ref['kind'] for ref in result['external_refs']} == {'external_relationship', 'field'}
    outcome = {check['name']: check['passed'] for check in module.checks('docx', result, {'text': 'Hello'})}
    assert outcome['no_external_refs'] is False and outcome['no_macros'] is False and outcome['text_coverage'] is True
    entity = env.tmp / 'entity.docx'
    with zipfile.ZipFile(entity, 'w') as archive:
        archive.writestr('word/document.xml', '<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document>&a;</w:document>')
    assert readback(entity, 'docx', thumbnail=False)['error'] == 'xml'


def test_readback_flags_formula_and_changed_xlsx(env):
    table = {'type': 'table', 'caption': None, 'columns': ['A'], 'rows': [['=1+2'], [5]]}
    raw = formats.write_xlsx([('Sheet', table)])
    source = io.BytesIO(raw)
    target = io.BytesIO()
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, 'w') as new:
        for item in old.infolist():
            data = old.read(item)
            if item.filename == 'xl/worksheets/sheet1.xml':
                data = data.replace(b'<c r="A3"><v>5</v></c>', b'<c r="A3"><f>1+4</f><v>5</v></c>').replace(b' s="1"', b'')
            new.writestr(item, data)
    path = env.tmp / 'changed.xlsx'
    path.write_bytes(target.getvalue())
    expect = {'text': '=1+2 5 A', 'sheets': [{'name': 'Sheet', 'cells': formats.sheet_cells(table)}]}
    result = readback(path, 'xlsx', expect)
    sheet = result['layout'][0]
    assert sheet['formulas'] == 1 and sheet['unprotected_triggers'] == 1 and sheet['cells_match'] is True
    outcome = {check['name']: check['passed'] for check in module.checks('xlsx', result, expect)}
    assert outcome['no_formulas'] is False and outcome['formulas_neutralized'] is False
    expect['sheets'][0]['cells'][2] = [['n', '6']]
    mismatch = readback(path, 'xlsx', expect)['layout'][0]
    assert mismatch['cells_match'] is False and mismatch['first_mismatch'] == {'row': 3, 'column': 'A', 'expected': ['n', '6'], 'found': ['n', '5']}


# PDF


def _pdf(path, lines, *, javascript=False, link=False, blank_page=False, margin=False, size=(595.28, 841.89)):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(*size)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    commands = []
    for index, line in enumerate(lines):
        escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        commands.append(f'BT /F1 12 Tf {2 if margin and index == 0 else 72} {760 - 18 * index} Td ({escaped}) Tj ET')
    stream = DecodedStreamObject()
    stream.set_data('\n'.join(commands).encode('latin-1'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    if blank_page:
        writer.add_blank_page(*size)
    if javascript:
        from pypdf.generic import TextStringObject
        action = DictionaryObject({NameObject('/S'): NameObject('/JavaScript'), NameObject('/JS'): TextStringObject('app.alert(1)')})
        writer._root_object[NameObject('/OpenAction')] = writer._add_object(action)
    if link:
        from pypdf.annotations import Link
        writer.add_annotation(page_number=0, annotation=Link(rect=(72, 700, 200, 720), url='https://example.invalid'))
    with open(path, 'wb') as stream_:
        writer.write(stream_)


def test_pdf_is_unavailable_from_python(env):
    source = message(env, '# Plan\n\nAlpha beta gamma.')
    plan = env.exports.preview(env.cid, source, 'pdf', {'paper': 'letter'})
    receipt = env.exports.render(env.cid, plan['plan_id'])
    assert receipt['status'] == 'unavailable' and receipt['file'] is None
    assert 'native export view' in receipt['reason'] and 'Nothing was written' in receipt['reason']
    assert not (env.home / 'exports' / env.cid / receipt['export_id']).exists()
    assert env.events == [] and env.exports.get(env.cid, receipt['export_id'])['status'] == 'unavailable'


def _native_shell(env, lines, **hostile):
    """Simulates the patched shell: it writes the PDF where told and attaches it by hash."""
    def shell(name, data):
        assert name == 'native_export_pdf'
        env.events.append((name, data))
        page = Path(data['html_path']).read_text()
        assert '<script' not in page and '@page{size:A4;margin:16mm}' in page

        def write():
            _pdf(data['pdf_path'], lines, **hostile)
            digest = hashlib.sha256(Path(data['pdf_path']).read_bytes()).hexdigest()
            env.exports.pdf_attach(data['chat_id'], data['export_id'], data['attach_token'], digest)
        threading.Thread(target=write).start()
    return shell


def test_native_pdf_attach_is_read_back(env):
    env.exports.native_ready(pdf=True)
    source = message(env, '# Plan\n\nAlpha beta gamma.')
    env.exports.emit = _native_shell(env, ['Plan', 'Plan', 'Alpha beta gamma.'])
    _, receipt = run(env, source, 'pdf')
    verified(receipt)
    assert receipt['readback']['pages'] == 1 and receipt['readback']['layout'][0]['width'] == 595.28
    assert {c['name'] for c in receipt['checks']} >= {'page_size', 'blank_pages', 'margins', 'thumbnail', 'no_external_refs'}
    folder = env.home / 'exports' / env.cid / receipt['export_id']
    assert sorted(p.name for p in folder.iterdir()) == ['export.pdf', 'print.html']
    with pytest.raises(ExportError, match='No native PDF export is waiting'):
        env.exports.pdf_attach(env.cid, receipt['export_id'], 'forged', '0' * 64)


def test_hostile_pdf_is_flagged_unverified(env):
    env.exports.native_ready(pdf=True)
    source = message(env, '# Plan\n\nAlpha beta gamma.')
    env.exports.emit = _native_shell(env, ['Plan', 'Plan', 'Alpha beta gamma.'], javascript=True, link=True, blank_page=True, margin=True)
    _, receipt = run(env, source, 'pdf')
    assert receipt['status'] == 'unverified'
    assert {'no_external_refs', 'blank_pages', 'margins'} <= failed(receipt)
    kinds = {ref['kind'] for ref in receipt['readback']['external_refs']}
    assert {'javascript', 'uri'} <= kinds and receipt['readback']['blank_pages'] == [2]
    env.exports.emit = _native_shell(env, ['Plan', 'Plan', 'Alpha beta gamma.'], size=(612, 792))
    _, letter = run(env, message(env, '# Plan\n\nAlpha beta gamma.'), 'pdf')
    assert failed(letter) == {'page_size'}


def test_native_pdf_failure_and_cancel(env):
    env.exports.native_ready(pdf=True)
    source = message(env, '# Plan\n\nAlpha beta gamma.')

    def fails(name, data):
        threading.Thread(target=env.exports.pdf_failed, args=(data['chat_id'], data['export_id'], data['attach_token'], 'WebKit\x00 crashed')).start()
    env.exports.emit = fails
    _, receipt = run(env, source, 'pdf')
    assert receipt['status'] == 'failed' and receipt['reason'] == 'The native PDF view failed: WebKit� crashed'
    plan = env.exports.preview(env.cid, source, 'pdf')
    env.exports.emit = lambda name, data: threading.Thread(target=env.exports.cancel, args=(env.cid, plan['plan_id'])).start()
    receipt = env.exports.render(env.cid, plan['plan_id'])
    assert receipt['status'] == 'cancelled' and not (env.home / 'exports' / env.cid / receipt['export_id']).exists()


# Saving


def test_save_uses_a_single_use_native_destination_token(env):
    _, receipt = run(env, artifact(env, 'table', fixture('hostile_table.json'), 'Readings'), 'csv')
    export_id = receipt['export_id']
    target = env.out / 'Readings.csv'
    # The webview only ever holds a token; a path is not a token.
    with pytest.raises(ExportError, match='Save dialog'):
        env.exports.save(env.cid, export_id, str(target))
    bound = env.exports.bind_destination(env.cid, export_id, str(target))
    assert set(bound) == {'destination_token', 'expires_at', 'name'} and bound['name'] == 'Readings.csv'
    with pytest.raises(ExportError, match='Save dialog'):
        env.exports.save(env.store.new_chat(), export_id, bound['destination_token'])
    with pytest.raises(ExportError, match='Save dialog'):
        env.exports.save(env.cid, export_id, bound['destination_token'])  # consumed by the refused attempt
    bound = env.exports.bind_destination(env.cid, export_id, str(target))
    saved = env.exports.save(env.cid, export_id, bound['destination_token'])
    assert saved == {'path': str(target), 'sha256': receipt['file']['sha256'], 'verified': True, 'published': False}
    assert target.read_bytes() == export_path(env, receipt).read_bytes()
    with pytest.raises(ExportError, match='Save dialog'):
        env.exports.save(env.cid, export_id, bound['destination_token'])
    with pytest.raises(ExportError, match='already exists'):
        env.exports.bind_destination(env.cid, export_id, str(target))
    target.write_text('older')
    replaced = env.exports.save(env.cid, export_id, env.exports.bind_destination(env.cid, export_id, str(target), replace=True)['destination_token'])
    assert replaced['verified'] is True and target.read_bytes() == export_path(env, receipt).read_bytes()
    assert sorted(p.name for p in env.out.iterdir()) == ['Readings.csv']
    saves = env.exports.get(env.cid, export_id)['saves']
    assert [(s['state'], s['verified']) for s in saves] == [('saved', True), ('saved', True)]


def test_destination_validation(env):
    _, receipt = run(env, artifact(env, 'table', fixture('hostile_table.json')), 'xlsx')
    export_id = receipt['export_id']
    bind = lambda path, replace=False: env.exports.bind_destination(env.cid, export_id, path, replace)
    for path, message_ in ((str(env.out / 'x.csv'), '.xlsx extension'), ('relative.xlsx', 'Save dialog'),
                           (str(env.out / '.hidden.xlsx'), '.xlsx extension'), (str(env.out / 'missing' / 'x.xlsx'), 'does not exist'),
                           (str(env.home / 'x.xlsx'), "outside Pebble's own data"), (str(env.out / 'x\0.xlsx'), 'Save dialog')):
        with pytest.raises(ExportError, match=message_):
            bind(path)
    secret = env.tmp / 'secret.txt'
    secret.write_text('keep')
    link = env.out / 'link.xlsx'
    link.symlink_to(secret)
    with pytest.raises(ExportError, match='regular file'):
        bind(str(link), replace=True)
    linked = env.tmp / 'linked'
    linked.symlink_to(env.out)
    with pytest.raises(ExportError, match='does not exist'):
        bind(str(linked / 'x.xlsx'))
    # A link planted after binding is never followed.
    bound = bind(str(env.out / 'late.xlsx'))
    (env.out / 'late.xlsx').symlink_to(secret)
    with pytest.raises(ExportError, match='already exists'):
        env.exports.save(env.cid, export_id, bound['destination_token'])
    assert secret.read_text() == 'keep'
    with pytest.raises(ExportError, match='Replacing a file'):
        bind(str(env.out / 'y.xlsx'), replace='yes')


def test_unverified_and_tampered_exports_save_honestly(env, monkeypatch):
    monkeypatch.setattr(module, '_quicklook', lambda path, cancel=None: None)
    _, receipt = run(env, message(env, '# Notes\n\nShort text.'), 'docx')
    assert receipt['status'] == 'unverified'
    saved = env.exports.save(env.cid, receipt['export_id'],
                             env.exports.bind_destination(env.cid, receipt['export_id'], str(env.out / 'n.docx'))['destination_token'])
    assert saved['verified'] is False and saved['published'] is False
    _, other = run(env, message(env, '# Two\n\nMore.'), 'html')
    export_path(env, other).write_text('changed')
    token = env.exports.bind_destination(env.cid, other['export_id'], str(env.out / 'two.html'))['destination_token']
    with pytest.raises(ExportError, match='changed after it was checked'):
        env.exports.save(env.cid, other['export_id'], token)
    assert not (env.out / 'two.html').exists()
    _, pdf = run(env, message(env, '# P\n\nText.'), 'pdf')
    with pytest.raises(ExportError, match='Only a finished export'):
        env.exports.bind_destination(env.cid, pdf['export_id'], str(env.out / 'p.pdf'))


# Drift, cancellation, restart and retention


def test_revision_and_content_drift_refuse_render(env):
    with env.store.lock, env.store.db as db:
        db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, chat_id TEXT, state TEXT)')
        db.execute('INSERT INTO runs VALUES(?,?,?)', ('run1', env.cid, 'running'))
    data = fixture('hostile_table.json')
    view = env.artifacts.render(env.cid, 'Readings', 'table', data, run_id='run1')
    plan = env.exports.preview(env.cid, {'kind': 'artifact', 'id': view['id'], 'revision': 1}, 'xlsx')
    env.artifacts.update(env.cid, view['id'], 1, 'Readings', 'table', {**data, 'caption': 'Changed'}, 'run1')
    with pytest.raises(ExportError, match='changed to revision 2'):
        env.exports.render(env.cid, plan['plan_id'])
    mid = env.store.message(env.cid, 'assistant', '# Draft\n\nFirst words.')
    plan = env.exports.preview(env.cid, {'kind': 'message', 'id': mid}, 'html')
    env.store.message(env.cid, 'assistant', '# Draft\n\nReplaced words.', mid)
    with pytest.raises(ExportError, match='source changed after the preview'):
        env.exports.render(env.cid, plan['plan_id'])
    assert env.exports.list(env.cid) == []


def test_cancel_removes_partial_output(env):
    source = artifact(env, 'table', fixture('hostile_table.json'))
    plan = env.exports.preview(env.cid, source, 'csv')
    stop = threading.Event()
    stop.set()
    receipt = env.exports.render(env.cid, plan['plan_id'], cancel=stop)
    assert receipt['status'] == 'cancelled' and receipt['file'] is None
    assert not (env.home / 'exports' / env.cid / receipt['export_id']).exists()
    assert env.exports.cancel(env.cid, 'unknown') is False


def test_restart_settles_interrupted_work_without_retry(env):
    _, receipt = run(env, artifact(env, 'table', fixture('hostile_table.json')), 'csv')
    with env.store.lock, env.store.db as db:
        db.execute("UPDATE export_records SET state='rendering' WHERE id=?", (receipt['export_id'],))
        db.execute('INSERT INTO export_saves VALUES(?,?,?,?,?,?,?,?,?)', ('s1', receipt['export_id'], env.cid, 'writing', '/x/y.csv', None, 0, 1.0, 1.0))
    restarted = Exports(env.home, env.store, env.artifacts)
    record = restarted.get(env.cid, receipt['export_id'])
    assert record['status'] == 'failed' and 'Nothing was retried' in record['reason'] and record['file'] is None
    assert [s['state'] for s in record['saves']] == ['uncertain']


def test_list_discard_and_forget(env):
    _, first = run(env, artifact(env, 'table', fixture('hostile_table.json'), 'Readings'), 'csv')
    _, second = run(env, message(env, '# Notes\n\nText.'), 'html')
    listed = env.exports.list(env.cid)
    assert [item['export_id'] for item in listed] == [second['export_id'], first['export_id']]
    assert all(item['published'] is False and 'checks' not in item for item in listed)
    assert env.exports.discard(env.cid, first['export_id']) == {'export_id': first['export_id'], 'status': 'discarded'}
    assert not (env.home / 'exports' / env.cid / first['export_id']).exists()
    assert env.exports.get(env.cid, first['export_id'])['file'] is None
    with pytest.raises(ExportError, match='not found'):
        env.exports.get(env.store.new_chat(), second['export_id'])
    env.exports.preview(env.cid, message(env, '# Plan\n\nWords.'), 'html')
    env.exports.forget(env.cid)
    assert env.exports.list(env.cid) == [] and not (env.home / 'exports' / env.cid).exists()
    assert not [p for p in env.exports.plans.values() if p['chat_id'] == env.cid]


# Reports


class _Runs:
    def get(self, rid, cid=None):
        if rid != 'run1':
            raise ValueError('Run not found in this conversation.')
        return {'id': rid, 'state': 'completed', 'steps': 4, 'created': 0.0, 'updated': 60.0, 'error': None}

    def tools(self, rid):
        return [{'call_id': 'c1', 'name': 'Read', 'arguments': {'path': '/secret/file'}, 'state': 'done', 'result': {'status': 'ok', 'content': 'SECRET BODY'}}]


class _Plans:
    def plan(self, rid):
        return {'steps': [{'id': 's1', 'title': 'Read the file', 'status': 'completed'}]}


class _Goals:
    # The real interface is Goals.get(chat_id, goal_id) with each step's blocker as {kind, ref, active} (merge: X17 O2;
    # the planned Goals.snapshot never existed). A candidate criterion is not part of a goal report.
    def get(self, cid, goal_id):
        return {'id': goal_id, 'title': 'Ship the report', 'revision': 3, 'state': 'blocked',
                'criteria': [{'id': 'k1', 'text': 'Tables reviewed', 'kind': 'human_check', 'status': 'met'},
                             {'id': 'k2', 'text': 'Proposed by the agent', 'kind': 'human_check', 'status': 'candidate'}],
                'steps': [{'id': 's1', 'title': 'Draft', 'status': 'completed', 'depends_on': [], 'blocker': None},
                          {'id': 's2', 'title': 'Review', 'status': 'blocked', 'depends_on': ['s1'],
                           'blocker': {'kind': 'question', 'ref': 'q1', 'active': True}}]}


def test_run_and_goal_reports(env):
    exports = Exports(env.home, env.store, env.artifacts, runs=_Runs(), interactions=_Plans(), goals=_Goals())
    plan = exports.preview(env.cid, {'kind': 'run_report', 'id': 'run1'}, 'xlsx')
    receipt = exports.render(env.cid, plan['plan_id'])
    verified(receipt)
    assert [entry['name'] for entry in receipt['readback']['layout'] if 'rows' in entry] == ['Run', 'Tools', 'Plan']
    plan = exports.preview(env.cid, {'kind': 'run_report', 'id': 'run1'}, 'html')
    page = export_path(env, exports.render(env.cid, plan['plan_id'])).read_text()
    assert '1970-01-01 00:01 UTC' in page and 'SECRET BODY' not in page and '/secret/file' not in page
    plan = exports.preview(env.cid, {'kind': 'goal_report', 'id': 'g1', 'revision': 3}, 'html')
    assert plan['source']['revision'] == 3
    goal = exports.render(env.cid, plan['plan_id'])
    verified(goal)
    report = export_path(env, goal).read_text()
    assert 'State: blocked · revision 3' in report and 'question' in report and 'Proposed by the agent' not in report
    with pytest.raises(ExportError, match='revision 3'):
        exports.preview(env.cid, {'kind': 'goal_report', 'id': 'g1', 'revision': 2}, 'html')
    with pytest.raises(ValueError, match='Run not found'):
        exports.preview(env.cid, {'kind': 'run_report', 'id': 'other'}, 'html')


# Confinement


def test_textutil_profile_denies_writes_and_network(env):
    output = env.tmp / 'escape.docx'
    result = subprocess.run(['/usr/bin/sandbox-exec', '-p', module.TEXTUTIL_PROFILE, module.TEXTUTIL, '-format', 'html',
                             '-convert', 'docx', '-stdin', '-output', str(output)], input=b'<p>x</p>', capture_output=True,
                            env=module._environment(), timeout=60)
    assert result.returncode != 0 and not output.exists()
    assert '(deny default)' in module.TEXTUTIL_PROFILE and '(allow network' not in module.TEXTUTIL_PROFILE
    assert 'file-write* (literal "/dev/null"))' in module.TEXTUTIL_PROFILE and 'WebKit' in module.TEXTUTIL_PROFILE


def test_readback_worker_is_confined(env):
    command = module._worker_command()
    profile = command[2]
    assert command[:2] == ['/usr/bin/sandbox-exec', '-p'] and '(deny default)' in profile and 'network' not in profile
    assert '(allow file-write* (literal "/dev/null"))' in profile and command[-1].endswith('export_worker.py')
    secret = env.tmp / 'secret.txt'
    secret.write_text('private')
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    probe = ('import socket,sys\n'
             f'try:\n open({str(secret)!r}).read(); print("read")\nexcept OSError: print("no-read")\n'
             f'try:\n socket.create_connection(("127.0.0.1",{listener.getsockname()[1]}),timeout=2); print("net")\nexcept OSError: print("no-net")\n'
             f'try:\n open({str(env.tmp / "w.txt")!r},"w").write("x"); print("wrote")\nexcept OSError: print("no-write")\n')
    code, out, _ = module._run(command[:-1] + ['-c', probe], b'', timeout=60, limit=4096, env=module._environment())
    listener.close()
    assert code == 0 and out.decode().split() == ['no-read', 'no-net', 'no-write']
    for header in (b'not json\n', b'{"op":"readback","format":"exe","size":1,"thumbnail":0}\nx', b'{"op":"readback","format":"csv","size":5,"thumbnail":0}\nab'):
        code, out, _ = module._run(command, header, timeout=60, limit=4096, env=module._environment())
        assert json.loads(out) in ({'error': 'invalid'}, {'error': 'parse'})
