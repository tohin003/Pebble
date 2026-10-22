"""Export file formats: pure writers for host blocks and a confined readback parser.

The service writes spreadsheet and web-page bytes from host-validated blocks.
Readback runs in a separate sandboxed process (``worker_main``) that receives
the file on stdin, has no file, network or credential access, and returns
bounded JSON facts. This module is also the source worker entry point, so it
imports no pebble modules; frozen builds call worker_main from
``--export-worker`` before the application is imported.

Blocks are plain dicts: heading{level,text}, paragraph{text},
list{ordered,items}, table{caption,columns,rows}, code{text,language},
html{html,css} (already sanitized by views.static_html/static_css) and
screens{start,screens} (a declarative prototype).
"""
import csv
import hashlib
import html as markup
import io
import json
import math
import re
import sys
import unicodedata
import zipfile
from collections import Counter
from html.parser import HTMLParser
from xml.etree import ElementTree
from xml.parsers import expat

MAX_FILE = 32 * 1024 * 1024
MAX_THUMBNAIL = 4 * 1024 * 1024
MAX_HEADER = 4096
MAX_TEXT = 2_000_000
MAX_CELLS = 200_000
MAX_PAGES = 500
ZIP_ENTRIES = 256
ZIP_PART = 64 * 1024 * 1024
ZIP_TOTAL = 128 * 1024 * 1024
FORMATS = ('pdf', 'docx', 'xlsx', 'csv', 'html')
# A spreadsheet treats text starting with these as a formula or command.
TRIGGERS = ('=', '+', '-', '@', '\t', '\r', '＝', '＋', '－', '＠')
_NUMBER = re.compile(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')
_CJK = '぀-ヿ㐀-䶿一-鿿豈-﫿가-힯'
_TOKEN = re.compile(f'[{_CJK}]|[^\\W{_CJK}]+')
CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; connect-src 'none'; "
       "frame-src 'none'; object-src 'none'; media-src 'none'; base-uri 'none'; form-action 'none'")
TAIL = '\n</body></html>\n'
PAPER = {'a4': (595.28, 841.89), 'letter': (612.0, 792.0)}


class ReadbackError(ValueError):
    """A sanitized parse failure; the reason is a fixed word."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__('Export readback refused its ' + reason + ' boundary.')


# Shared text model


def cell_text(value):
    if value is None:
        return ''
    if value is True or value is False:
        return 'true' if value else 'false'
    if type(value) is float:
        return repr(value)
    return str(value)


def plain_number(text):
    return bool(_NUMBER.fullmatch(text))


def triggered(text):
    # A leading CR or LF before a trigger also counts: exports._clean turns CR into LF before this check (X17 F7).
    return (isinstance(text, str) and (text.startswith(TRIGGERS) or text.lstrip('\r\n').startswith(TRIGGERS))
            and not plain_number(text))


def tokens(text):
    return Counter(_TOKEN.findall(unicodedata.normalize('NFKC', text or '').casefold()))


def coverage(expected, actual):
    """Share of the expected word tokens present in the readback text (multiset)."""
    want = tokens(expected)
    total = sum(want.values())
    if not total:
        return 1.0
    have = tokens(actual)
    return round(sum(min(count, have[token]) for token, count in want.items()) / total, 4)


def squash(text):
    return ' '.join(unicodedata.normalize('NFKC', text or '').casefold().split())


class _Text(HTMLParser):
    SKIP = frozenset({'style', 'script', 'title', 'head'})
    BREAK = frozenset({'p', 'div', 'li', 'tr', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'table', 'section', 'caption', 'td', 'th', 'nav', 'blockquote'})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BREAK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BREAK:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def visible_text(source):
    parser = _Text()
    parser.feed(source)
    parser.close()
    return ''.join(parser.parts)


def _component_text(item):
    kind = item['type']
    if kind in ('heading',):
        return [item['text']]
    if kind in ('button', 'toggle', 'image_placeholder'):
        return [item['label']]
    if kind == 'input':
        return [item['label'], item.get('placeholder', '')]
    if kind == 'list':
        return list(item['items'])
    if kind == 'card':
        return [item['title'], item['body']]
    if kind == 'nav':
        return [entry['label'] for entry in item['items']]
    return []


def block_text(blocks):
    """The visible text every format must carry, in reading order."""
    parts = []
    for block in blocks:
        kind = block['type']
        if kind in ('heading', 'paragraph', 'code'):
            parts.append(block['text'])
        elif kind == 'list':
            parts.extend(block['items'])
        elif kind == 'table':
            if block.get('caption'):
                parts.append(block['caption'])
            parts.extend(block['columns'])
            parts.extend(cell_text(value) for row in block['rows'] for value in row)
        elif kind == 'html':
            parts.append(visible_text(block['html']))
        elif kind == 'screens':
            for screen in block['screens']:
                parts.append(screen['title'])
                for item in screen['components']:
                    parts.extend(_component_text(item))
    return '\n'.join(parts)


# Static HTML


class Sanitizer(HTMLParser):
    """Export-body allowlist: views.static_html plus host fragment links.

    Idempotent over its own output, so readback can prove the stored body
    equals a re-sanitized copy byte for byte.
    """
    TAGS = frozenset('div span p h1 h2 h3 h4 h5 h6 main section article header footer aside strong b em i small pre code '
                     'blockquote ul ol li table thead tbody tr th td caption br hr a nav'.split())
    DROP = frozenset('script style iframe object embed svg math form input button textarea select video audio img link '
                     'meta base canvas template title head noscript frame frameset applet portal'.split())
    VOID = frozenset('br hr img input embed link meta base'.split())
    SCREEN = re.compile(r'screen-[A-Za-z0-9_-]{1,64}')

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output, self.stack, self.nodes = [], [], 0

    def _attribute(self, tag, name, value):
        if value is None:
            return None
        if name in ('class', 'title') and len(value) <= 120 and re.fullmatch(r'[\w .,:-]+', value):
            return value
        if name in ('colspan', 'rowspan') and tag in ('td', 'th') and value.isdigit() and 1 <= int(value) <= 16:
            return value
        if name == 'dir' and value in ('auto', 'ltr', 'rtl'):
            return value
        if name == 'id' and tag == 'section' and self.SCREEN.fullmatch(value):
            return value
        if name == 'href' and tag == 'a' and value.startswith('#') and self.SCREEN.fullmatch(value[1:]):
            return value
        return None

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 400_000 or len(self.stack) >= 64:
            raise ReadbackError('size')
        suppressed = any(entry[1] for entry in self.stack) or tag in self.DROP
        if not suppressed and tag in self.TAGS:
            safe = []
            for name, value in attrs:
                kept = self._attribute(tag, name, value)
                if kept is not None:
                    safe.append(f' {name}="{markup.escape(kept, quote=True)}"')
            self.output.append('<' + tag + ''.join(safe) + '>')
        if tag not in self.VOID:
            self.stack.append((tag, suppressed))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not any(entry[0] == tag for entry in self.stack):
            return
        while self.stack:
            opened, suppressed = self.stack.pop()
            if not suppressed and opened in self.TAGS:
                self.output.append('</' + opened + '>')
            if opened == tag:
                break

    def handle_data(self, data):
        if not any(entry[1] for entry in self.stack):
            self.output.append(markup.escape(data))


def sanitize(body):
    parser = Sanitizer()
    parser.feed(body)
    parser.close()
    while parser.stack:
        parser.handle_endtag(parser.stack[-1][0])
    return ''.join(parser.output)


_E = lambda text: markup.escape(text, quote=True)
_BASE_CSS = (':root{color-scheme:light}'
             'body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif;color:#1d2420;background:#fff;margin:32px auto;max-width:860px;padding:0 20px}'
             'h1{font-size:26px;margin:0 0 16px}h2{font-size:20px;margin:24px 0 10px}h3{font-size:17px;margin:20px 0 8px}h4{font-size:15px;margin:16px 0 6px}'
             'table{border-collapse:collapse;margin:12px 0;width:100%}caption{text-align:start;font-weight:600;padding:4px 0}'
             'th,td{border:1px solid #c9d1cb;padding:4px 8px;text-align:start;vertical-align:top;overflow-wrap:anywhere}th{background:#eef2ee}'
             'td.num{text-align:right;font-variant-numeric:tabular-nums}'
             'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f4;padding:10px;border-radius:6px;font:13px/1.45 ui-monospace,Menlo,monospace}'
             '.export-footer{color:#5c6a60;font-size:12px;border-top:1px solid #dde3de;margin-top:28px;padding-top:8px}'
             '.banner{background:#fff6dd;border:1px solid #e8d48b;padding:8px 12px;border-radius:8px}'
             '.screen{border:1px solid #c9d1cb;border-radius:12px;padding:16px;margin:16px 0}'
             'a.button{display:inline-block;padding:6px 12px;border-radius:8px;background:#1d6b55;color:#fff;text-decoration:none;margin:4px 0}'
             'nav a{margin-right:12px}')
# A prototype shows its start screen; a fragment link (:target) swaps screens without script.
_TARGET_CSS = '.screen{display:none}.screen:target,.screen.start{display:block}.screen:target~.screen.start{display:none}'
_PRINT_CSS = '@media print{body{margin:0;max-width:none}tr,pre,li{break-inside:avoid}h1,h2,h3,h4{break-after:avoid}}'
_DOCX_CSS = ('body{font-family:Helvetica,Arial,sans-serif;font-size:11pt}table{border-collapse:collapse}'
             'th,td{border:1px solid #999999;padding:3pt 6pt}pre{font-family:Menlo,Courier,monospace;font-size:9pt}')


def html_head(title, css='', mode='html', paper='a4'):
    if mode == 'docx':
        style = _DOCX_CSS + css
    else:
        style = _BASE_CSS + (_TARGET_CSS if mode == 'html' else '') + _PRINT_CSS + css
        if mode == 'pdf':
            style += '@page{size:%s;margin:16mm}' % ('A4' if paper == 'a4' else 'letter')
    return ('<!doctype html>\n<html><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{_E(CSP)}">'
            '<meta name="referrer" content="no-referrer">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_E(title)}</title><style>{style}</style></head>\n<body>\n')


def _lines(text):
    return '<br>'.join(_E(line) for line in text.split('\n'))


def _component(item, mode):
    kind = item['type']
    link = (lambda label, goto: f'<a class="button" href="#screen-{_E(goto)}">{_E(label)}</a>') if mode == 'html' else \
           (lambda label, goto: f'<p dir="auto">[{_E(label)}]</p>')
    if kind == 'heading':
        return f'<h4 dir="auto">{_E(item["text"])}</h4>'
    if kind == 'button':
        return link(item['label'], item['goto']) if item.get('goto') else f'<p dir="auto">[{_E(item["label"])}]</p>'
    if kind == 'input':
        hint = f' <span class="placeholder">{_E(item["placeholder"])}</span>' if item.get('placeholder') else ''
        return f'<p class="input" dir="auto">{_E(item["label"])}:{hint}</p>'
    if kind == 'toggle':
        return f'<p class="toggle" dir="auto">☐ {_E(item["label"])}</p>'
    if kind == 'list':
        return '<ul>' + ''.join(f'<li dir="auto">{_E(entry)}</li>' for entry in item['items']) + '</ul>'
    if kind == 'card':
        return f'<div class="card"><h4 dir="auto">{_E(item["title"])}</h4><p dir="auto">{_E(item["body"])}</p></div>'
    if kind == 'image_placeholder':
        return f'<div class="image" dir="auto">{_E(item["label"])} ({_E(item.get("aspect", ""))})</div>'
    if kind == 'nav':
        if mode == 'html':
            return '<nav>' + ' '.join(f'<a href="#screen-{_E(entry["goto"])}">{_E(entry["label"])}</a>' for entry in item['items']) + '</nav>'
        return '<ul>' + ''.join(f'<li dir="auto">{_E(entry["label"])}</li>' for entry in item['items']) + '</ul>'
    raise ValueError('Unsupported prototype component.')


def _block_html(block, mode):
    kind = block['type']
    if kind == 'heading':
        level = min(4, max(1, block['level']))
        return f'<h{level} dir="auto">{_E(block["text"])}</h{level}>'
    if kind == 'paragraph':
        return f'<p dir="auto">{_lines(block["text"])}</p>'
    if kind == 'list':
        tag = 'ol' if block['ordered'] else 'ul'
        return f'<{tag}>' + ''.join(f'<li dir="auto">{_lines(item)}</li>' for item in block['items']) + f'</{tag}>'
    if kind == 'code':
        return f'<pre><code>{_E(block["text"])}</code></pre>'
    if kind == 'table':
        caption = f'<caption dir="auto">{_E(block["caption"])}</caption>' if block.get('caption') else ''
        head = ''.join(f'<th dir="auto">{_E(label)}</th>' for label in block['columns'])
        rows = ''.join('<tr>' + ''.join(
            f'<td class="num">{_E(cell_text(value))}</td>' if type(value) in (int, float) else f'<td dir="auto">{_E(cell_text(value))}</td>'
            for value in row) + '</tr>' for row in block['rows'])
        return f'<table>{caption}<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'
    if kind == 'html':
        return f'<div class="artifact-html">{block["html"]}</div>'
    if kind == 'screens':
        start = block['start']
        ordered = [s for s in block['screens'] if s['id'] != start] + [s for s in block['screens'] if s['id'] == start]
        parts = ['<p class="banner">Interactive mock-up · controls only navigate this demo</p>'] if mode == 'html' else []
        for screen in (ordered if mode == 'html' else block['screens']):
            classes = 'screen start' if screen['id'] == start else 'screen'
            inner = f'<h3 dir="auto">{_E(screen["title"])}</h3>' + ''.join(_component(item, mode) for item in screen['components'])
            parts.append(f'<section id="screen-{_E(screen["id"])}" class="{classes}">{inner}</section>')
        return '\n'.join(parts)
    raise ValueError('Unsupported export block.')


def html_parts(title, blocks, mode='html', paper='a4', footer=''):
    """Return (head, body) for a static export; the document is head + body + TAIL."""
    css = ''.join(block.get('css', '') for block in blocks if block['type'] == 'html')
    body = '\n'.join(_block_html(block, mode) for block in blocks)
    if footer:
        body += f'\n<footer class="export-footer">{_E(footer)}</footer>'
    return html_head(title, css, mode, paper), body


def html_document(title, blocks, mode='html', paper='a4', footer=''):
    head, body = html_parts(title, blocks, mode, paper, footer)
    return head + body + TAIL


# Spreadsheets


def xlsx_cell(value):
    """Typed cell: ('n'|'b'|'s'|'e', text). Integers beyond Excel's 53-bit precision stay text."""
    if value is None:
        return ['e', '']
    if value is True or value is False:
        return ['b', '1' if value else '0']
    if type(value) is int:
        return ['n', str(value)] if abs(value) <= 2 ** 53 else ['s', str(value)]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('Spreadsheet numbers must be finite.')
        return ['n', repr(value)]
    return ['s', value]


def csv_cell(value):
    text = cell_text(value)
    return "'" + text if type(value) is str and triggered(text) else text


def sheet_cells(table, typed=True):
    """The exact grid a writer emits and a readback must find (header first)."""
    header = [['s', label] for label in table['columns']] if typed else [csv_cell(label) for label in table['columns']]
    return [header] + [[xlsx_cell(value) if typed else csv_cell(value) for value in row] for row in table['rows']]


def write_csv(table):
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator='\r\n')
    for row in sheet_cells(table, typed=False):
        writer.writerow(row)
    # The byte-order mark lets spreadsheet apps detect UTF-8 (RTL and CJK text).
    return ('\ufeff' + stream.getvalue()).encode('utf-8')


_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'


def column_name(index):
    name = ''
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def column_index(name):
    value = 0
    for char in name:
        value = value * 26 + ord(char) - 64
    return value - 1


def _xml_text(text):
    return markup.escape(text, quote=False)


def _sheet_xml(table):
    rows = []
    for number, row in enumerate(sheet_cells(table), 1):
        cells = []
        for index, (kind, text) in enumerate(row):
            ref = column_name(index) + str(number)
            style = (2 if number == 1 else 0) + (1 if kind == 's' and triggered(text) else 0)
            attr = f' s="{style}"' if style else ''
            if kind == 'e':
                continue
            if kind == 's':
                cells.append(f'<c r="{ref}" t="inlineStr"{attr}><is><t xml:space="preserve">{_xml_text(text)}</t></is></c>')
            elif kind == 'b':
                cells.append(f'<c r="{ref}" t="b"{attr}><v>{text}</v></c>')
            else:
                cells.append(f'<c r="{ref}"{attr}><v>{text}</v></c>')
        rows.append(f'<row r="{number}">' + ''.join(cells) + '</row>')
    pane = '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="{_MAIN}">{pane}<sheetData>'
            + ''.join(rows) + '</sheetData></worksheet>')


_STYLES = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<styleSheet xmlns="{_MAIN}">'
           '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
           '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
           '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" quotePrefix="1"/>'
           '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
           '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" quotePrefix="1"/></cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def write_xlsx(sheets):
    """Minimal SpreadsheetML: inline typed cells, no formulas, shared strings or links.

    ``sheets`` is a list of (name, table); names are already unique and valid.
    """
    count = len(sheets)
    parts = {
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + ''.join(f'<Override PartName="/xl/worksheets/sheet{n}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for n in range(1, count + 1))
            + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        '_rels/.rels': f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{_PKG}">'
            f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="{_MAIN}" xmlns:r="{_REL}"><sheets>'
            + ''.join(f'<sheet name="{markup.escape(name, quote=True)}" sheetId="{n}" r:id="rId{n}"/>' for n, (name, _) in enumerate(sheets, 1))
            + '</sheets></workbook>',
        'xl/_rels/workbook.xml.rels': f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{_PKG}">'
            + ''.join(f'<Relationship Id="rId{n}" Type="{_REL}/worksheet" Target="worksheets/sheet{n}.xml"/>' for n in range(1, count + 1))
            + f'<Relationship Id="rId{count + 1}" Type="{_REL}/styles" Target="styles.xml"/></Relationships>',
        'xl/styles.xml': _STYLES,
    }
    for n, (_, table) in enumerate(sheets, 1):
        parts[f'xl/worksheets/sheet{n}.xml'] = _sheet_xml(table)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o600 << 16
            archive.writestr(info, text.encode('utf-8'))
    return stream.getvalue()


# Readback (worker side). Every parser bounds its input and returns facts only.


def _archive(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise ReadbackError('parse') from None
    entries = archive.infolist()
    if len(entries) > ZIP_ENTRIES or sum(e.file_size for e in entries) > ZIP_TOTAL:
        raise ReadbackError('size')
    parts = {}
    for entry in entries:
        if entry.flag_bits & 1 or entry.file_size > ZIP_PART or entry.filename in parts:
            raise ReadbackError('size')
        with archive.open(entry) as stream:
            content = stream.read(ZIP_PART + 1)
        if len(content) > ZIP_PART:
            raise ReadbackError('size')
        parts[entry.filename] = content
    return parts


def _refuse_declaration(*_):
    raise ReadbackError('xml')


def _xml(content):
    head = content[:4096].lower()
    if b'<!doctype' in head or b'<!entity' in content.lower():
        raise ReadbackError('xml')
    # V1-138: the byte check above reads only ASCII-compatible encodings. Expat reads the part in the encoding it
    # declares (UTF-16 too) and stops at the first document type or entity declaration, before any expansion.
    scan = expat.ParserCreate()
    scan.StartDoctypeDeclHandler = scan.EntityDeclHandler = _refuse_declaration
    try:
        scan.Parse(content, True)
    except expat.ExpatError:
        raise ReadbackError('xml') from None
    try:
        return ElementTree.fromstring(content)
    except (ElementTree.ParseError, ValueError):
        raise ReadbackError('xml') from None


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _relationships(parts):
    refs = []
    for name, content in parts.items():
        if not name.endswith('.rels'):
            continue
        for item in _xml(content).iter():
            if _local(item.tag) == 'Relationship' and item.get('TargetMode', '').lower() == 'external':
                refs.append({'kind': 'external_relationship', 'part': name[:120],
                             'type': item.get('Type', '').rsplit('/', 1)[-1][:60], 'target': item.get('Target', '')[:200]})
    return refs


def _macros(parts):
    types = parts.get('[Content_Types].xml', b'').lower()
    return any(name.lower().endswith(('vbaproject.bin', '.bin')) or '/activex/' in name.lower() or 'embeddings/' in name.lower()
               for name in parts) or b'macroenabled' in types or b'vbaproject' in types


def _grid(sheet, styles, shared):
    cells, formulas, unprotected, maximum = {}, 0, 0, (0, 0)
    for row in sheet.iter():
        if _local(row.tag) != 'row':
            continue
        number = int(row.get('r') or 0)
        maximum = (max(maximum[0], number), maximum[1])
        for cell in row:
            if _local(cell.tag) != 'c':
                continue
            match = re.fullmatch(r'([A-Z]{1,3})(\d{1,7})', cell.get('r', ''))
            if not match:
                raise ReadbackError('parse')
            column, line = column_index(match.group(1)), int(match.group(2))
            kind = cell.get('t', 'n')
            value = text = None
            for child in cell:
                name = _local(child.tag)
                if name == 'f':
                    formulas += 1
                elif name == 'v':
                    value = child.text or ''
                elif name == 'is':
                    text = ''.join(part.text or '' for part in child.iter() if _local(part.tag) == 't')
            if kind == 'inlineStr':
                entry = ['s', text or '']
            elif kind == 's':
                index = int(value or -1)
                if not 0 <= index < len(shared):
                    raise ReadbackError('parse')
                entry = ['s', shared[index]]
            elif kind == 'str':
                entry = ['s', value or '']
            elif kind in ('b', 'e'):
                entry = [kind if kind == 'b' else 'error', value or '']
            else:
                entry = ['n', value] if value is not None else ['e', '']
            style = int(cell.get('s') or 0)
            if entry[0] == 's' and triggered(entry[1]) and not (0 <= style < len(styles) and styles[style]):
                unprotected += 1
            cells[(line, column)] = entry
            maximum = (max(maximum[0], line), max(maximum[1], column + 1))
            if len(cells) > MAX_CELLS:
                raise ReadbackError('size')
    grid = [[cells.get((line, column), ['e', '']) for column in range(maximum[1])] for line in range(1, maximum[0] + 1)]
    return grid, formulas, unprotected


def _read_xlsx(data):
    parts = _archive(data)
    refs = _relationships(parts)
    workbook = _xml(parts.get('xl/workbook.xml') or b'')
    if any(_local(node.tag) in ('externalReferences', 'externalReference') for node in workbook.iter()):
        refs.append({'kind': 'external_workbook'})
    targets = {}
    if 'xl/_rels/workbook.xml.rels' in parts:
        for item in _xml(parts['xl/_rels/workbook.xml.rels']).iter():
            if _local(item.tag) == 'Relationship':
                targets[item.get('Id')] = item.get('Target', '')
    styles = []
    if 'xl/styles.xml' in parts:
        for node in _xml(parts['xl/styles.xml']).iter():
            if _local(node.tag) == 'cellXfs':
                styles = [entry.get('quotePrefix') in ('1', 'true') for entry in node if _local(entry.tag) == 'xf']
    shared = []
    if 'xl/sharedStrings.xml' in parts:
        for item in _xml(parts['xl/sharedStrings.xml']):
            shared.append(''.join(part.text or '' for part in item.iter() if _local(part.tag) == 't'))
    sheets, texts = [], []
    for node in workbook.iter():
        if _local(node.tag) != 'sheet':
            continue
        target = targets.get(node.get(f'{{{_REL}}}id'), '')
        path = 'xl/' + target.lstrip('/').removeprefix('xl/')
        if path not in parts:
            raise ReadbackError('parse')
        sheet = _xml(parts[path])
        if any(_local(item.tag) == 'hyperlink' for item in sheet.iter()):
            refs.append({'kind': 'hyperlink', 'part': path[:120]})
        grid, formulas, unprotected = _grid(sheet, styles, shared)
        texts.extend(('true' if text == '1' else 'false') if kind == 'b' else text for row in grid for kind, text in row if kind in ('s', 'n', 'b'))
        sheets.append({'name': node.get('name', '')[:64], 'rows': len(grid), 'columns': max((len(r) for r in grid), default=0),
                       'cells': grid, 'formulas': formulas, 'unprotected_triggers': unprotected})
    return {'sheets': sheets, 'text': '\n'.join(texts)[:MAX_TEXT], 'external_refs': refs, 'macros': _macros(parts)}


def _read_csv(data):
    bom = data.startswith(b'\xef\xbb\xbf')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ReadbackError('parse') from None
    try:
        grid = list(csv.reader(io.StringIO(text, newline=''), strict=True))
    except csv.Error:
        raise ReadbackError('parse') from None
    if sum(len(row) for row in grid) > MAX_CELLS:
        raise ReadbackError('size')
    unprotected = sum(1 for row in grid for value in row if triggered(value))
    return {'sheets': [{'name': 'CSV', 'rows': len(grid), 'columns': max((len(r) for r in grid), default=0),
                        'cells': grid, 'formulas': 0, 'unprotected_triggers': unprotected}],
            'text': '\n'.join(value for row in grid for value in row)[:MAX_TEXT], 'external_refs': [], 'macros': False, 'bom': bom}


_FIELDS = re.compile(r'\b(HYPERLINK|INCLUDETEXT|INCLUDEPICTURE|DDE|DDEAUTO|IMPORT|LINK)\b', re.I)


def _read_docx(data):
    parts = _archive(data)
    if 'word/document.xml' not in parts:
        raise ReadbackError('parse')
    refs = _relationships(parts)
    document = _xml(parts['word/document.xml'])
    paragraphs, tables, lines = 0, 0, []
    for node in document.iter():
        name = _local(node.tag)
        if name == 'p':
            paragraphs += 1
            line = []
            for item in node.iter():
                tag = _local(item.tag)
                if tag == 't':
                    line.append(item.text or '')
                elif tag == 'tab':
                    line.append('\t')
                elif tag == 'instrText' and _FIELDS.search(item.text or ''):
                    refs.append({'kind': 'field', 'instruction': (item.text or '')[:120].strip()})
                elif tag == 'fldSimple':
                    instruction = next((v for k, v in item.attrib.items() if _local(k) == 'instr'), '')
                    if _FIELDS.search(instruction):
                        refs.append({'kind': 'field', 'instruction': instruction[:120].strip()})
            lines.append(''.join(line))
        elif name == 'tbl':
            tables += 1
        if sum(map(len, lines)) > MAX_TEXT:
            raise ReadbackError('size')
    return {'pages': None, 'text': '\n'.join(lines), 'external_refs': refs, 'macros': _macros(parts),
            'layout': [{'part': 'word/document.xml', 'paragraphs': paragraphs, 'tables': tables, 'parts': len(parts)}]}


_ACTIVE = {'/JavaScript': 'javascript', '/JS': 'javascript', '/OpenAction': 'open_action', '/AA': 'additional_actions',
           '/Launch': 'launch', '/EmbeddedFiles': 'embedded_file', '/EmbeddedFile': 'embedded_file', '/URI': 'uri',
           '/GoToR': 'remote_goto', '/GoToE': 'embedded_goto', '/SubmitForm': 'submit_form', '/ImportData': 'import_data',
           '/RichMedia': 'rich_media', '/XFA': 'xfa', '/Sound': 'sound', '/Movie': 'movie'}
_PAINT = frozenset(b'Tj TJ \' " Do f F f* B B* b b* S s sh BI EI'.split()) | {b'INLINE IMAGE'}


def _active(reader):
    """Bounded walk of the whole object graph for actions, scripts and attachments."""
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject
    found, seen, stack, budget = [], set(), [reader.trailer], 50_000
    while stack and budget:
        budget -= 1
        node = stack.pop()
        if isinstance(node, IndirectObject):
            key = (node.idnum, node.generation)
            if key in seen:
                continue
            seen.add(key)
            try:
                node = node.get_object()
            except Exception:
                continue
        if isinstance(node, DictionaryObject):
            for key, value in node.items():
                if key in _ACTIVE:
                    found.append({'kind': _ACTIVE[key]})
                if key == '/S' and str(value) in _ACTIVE:
                    found.append({'kind': _ACTIVE[str(value)]})
                if key not in ('/Parent', '/P'):
                    stack.append(value)
        elif isinstance(node, ArrayObject):
            stack.extend(node)
    if stack:
        found.append({'kind': 'unscanned_objects'})
    unique = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique


def _read_pdf(data, margin=9.0):
    from pypdf import PdfReader
    from pypdf.generic import ContentStream
    import pypdf.filters
    pypdf.filters.ZLIB_MAX_OUTPUT_LENGTH = 16_000_000
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ReadbackError('encrypted')
        pages = reader.pages
        count = len(pages)
    except ReadbackError:
        raise
    except Exception:
        raise ReadbackError('parse') from None
    if not 0 < count <= MAX_PAGES:
        raise ReadbackError('size')
    refs, texts, blank, layout = _active(reader), [], [], []
    for index, page in enumerate(pages):
        box = page.mediabox
        width, height = float(box.width), float(box.height)
        outside = []

        def visit(text, cm, tm, *_):
            if not text.strip():
                return
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4] - float(box.left)
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5] - float(box.bottom)
            if not (margin <= x <= width - margin and margin <= y <= height - margin):
                outside.append(1)
        try:
            text = page.extract_text(visitor_text=visit) or ''
            contents = page.get_contents()
            operators = {op for _, op in ContentStream(contents, reader).operations} if contents is not None else set()
        except Exception:
            raise ReadbackError('parse') from None
        texts.append(text)
        if not operators & _PAINT:
            blank.append(index + 1)
        layout.append({'page': index + 1, 'width': round(width, 2), 'height': round(height, 2),
                       'text_chars': len(text), 'margin_text': len(outside)})
        if sum(map(len, texts)) > MAX_TEXT:
            raise ReadbackError('size')
    return {'pages': count, 'text': '\n'.join(texts), 'external_refs': refs, 'blank_pages': blank, 'layout': layout}


class _Audit(HTMLParser):
    """Inspect real tags and attributes (never escaped text) for scripts and references."""
    LOADS = frozenset('iframe object embed img link base frame audio video source track portal applet'.split())
    URLS = frozenset('src href action formaction poster data background srcset xlink:href ping cite manifest'.split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs, self.scripts, self.handlers, self.javascript, self.csp, self.style = [], 0, 0, 0, None, []
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.scripts += 1
        if tag in self.LOADS:
            self.refs.append({'kind': tag})
        values = dict(attrs)
        if tag == 'meta' and (values.get('http-equiv') or '').lower() == 'content-security-policy':
            self.csp = values.get('content')
        for name, value in attrs:
            value = value or ''
            if name.startswith('on'):
                self.handlers += 1
            if 'javascript:' in value.replace(' ', '').lower():
                self.javascript += 1
            if name in self.URLS and not value.startswith('#'):
                self.refs.append({'kind': 'reference', 'attribute': name})
            if name == 'style' and re.search(r'url\s*\(|@import', value, re.I):
                self.refs.append({'kind': 'css_url'})
        self._in_style = tag == 'style'

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.style.append(data)


def _read_html(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ReadbackError('parse') from None
    marker = '\n<body>\n'
    position = text.find(marker)
    head = text[:position + len(marker)] if position >= 0 else ''
    body = text[position + len(marker):-len(TAIL)] if position >= 0 and text.endswith(TAIL) else None
    audit = _Audit()
    audit.feed(text)
    audit.close()
    refs = audit.refs + [{'kind': 'css_url'} for _ in re.finditer(r'url\s*\(|@import', ''.join(audit.style), re.I)]
    resanitized = body is not None and sanitize(body) == body
    layout = [{'document': 1, 'resanitized_equal': resanitized, 'head_sha256': hashlib.sha256(head.encode()).hexdigest(),
               'csp': audit.csp, 'scripts': audit.scripts, 'event_handlers': audit.handlers,
               'javascript_urls': audit.javascript, 'bytes': len(data)}]
    return {'pages': 1, 'text': visible_text(body if body is not None else text)[:MAX_TEXT], 'external_refs': refs, 'layout': layout}


def ink(png):
    """Thumbnail facts: blank when no dark pixels; margin ink when any touch the 3% edge band."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 16_000_000
    try:
        with Image.open(io.BytesIO(png)) as image:
            if image.format != 'PNG' or image.width > 4096 or image.height > 4096:
                raise ReadbackError('thumbnail')
            rgba = image.convert('RGBA')
    except ReadbackError:
        raise
    except Exception:
        raise ReadbackError('thumbnail') from None
    grey = Image.alpha_composite(Image.new('RGBA', rgba.size, (255, 255, 255, 255)), rgba).convert('L')
    mask = grey.point(lambda value: 255 if value < 200 else 0)
    width, height = mask.size
    histogram = mask.histogram()
    dark = histogram[255]
    band = max(2, round(min(width, height) * 0.03))
    if width <= 2 * band or height <= 2 * band:
        raise ReadbackError('thumbnail')
    inner = mask.crop((band, band, width - band, height - band)).histogram()[255]
    return {'width': width, 'height': height, 'ink_pixels': dark, 'blank': dark < 20, 'margin_ink': dark - inner > 0}


_READERS = {'xlsx': _read_xlsx, 'csv': _read_csv, 'docx': _read_docx, 'pdf': _read_pdf, 'html': _read_html}


def read(fmt, data, thumbnail=None):
    if fmt not in _READERS or len(data) > MAX_FILE or (thumbnail and len(thumbnail) > MAX_THUMBNAIL):
        raise ReadbackError('invalid')
    facts = _READERS[fmt](data)
    facts['thumbnail'] = ink(thumbnail) if thumbnail else None
    return facts


def worker_main():
    """Confined entry point: one framed request on stdin, one JSON object on stdout."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (40, 41))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        stream = sys.stdin.buffer
        line = stream.readline(MAX_HEADER + 1)
        if len(line) > MAX_HEADER or not line.endswith(b'\n'):
            raise ReadbackError('invalid')
        header = json.loads(line)
        if (type(header) is not dict or set(header) != {'op', 'format', 'size', 'thumbnail'} or header['op'] != 'readback'
                or header['format'] not in FORMATS or type(header['size']) is not int or not 0 < header['size'] <= MAX_FILE
                or type(header['thumbnail']) is not int or not 0 <= header['thumbnail'] <= MAX_THUMBNAIL):
            raise ReadbackError('invalid')
        data = stream.read(header['size'])
        thumbnail = stream.read(header['thumbnail']) if header['thumbnail'] else None
        if len(data) != header['size'] or (thumbnail is not None and len(thumbnail) != header['thumbnail']) or stream.read(1):
            raise ReadbackError('invalid')
        result = read(header['format'], data, thumbnail)
    except ReadbackError as exc:
        result = {'error': exc.reason}
    except RecursionError:
        result = {'error': 'size'}
    except Exception:
        result = {'error': 'parse'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    worker_main()
