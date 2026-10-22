"""Verified local exports. An export is a file on this Mac, never a publication.

preview -> render -> save. A single-use plan pins the source (kind, id, revision,
digest). Render normalizes the source into host blocks, writes into
``<home>/exports/<chat>/<export>/`` and reads the file back in a confined worker;
a receipt is ``verified`` only when every check passes. Save copies the file only
to a destination the native shell bound after its own Save dialog: the webview
holds a single-use token, never a path. Nothing is shared, uploaded, emailed or
sent, and a save is never retried after a crash. Source text is untrusted data:
it is escaped into static documents with no scripts, links out or formulas.
"""
import hashlib
import json
import math
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import export_worker as formats
from . import quiz_grading
from .artifact_schema import COMPONENTS, validate_native
from .views import static_css, static_html

FORMATS = formats.FORMATS
SOURCE_KINDS = ('artifact', 'message', 'goal_report', 'run_report')
STATES = ('rendering', 'verified', 'unverified', 'unavailable', 'failed', 'cancelled', 'discarded')
PLAN_SECONDS = 600
TOKEN_SECONDS = 120
NATIVE_PDF_SECONDS = 90
MAX_PLANS = 16
MAX_TOKENS = 8
MAX_BLOCKS = 4000
MAX_SOURCE_TEXT = 1_000_000
MAX_ROWS = 10_000
MAX_COLUMNS = 64
MAX_CELLS = 200_000
MAX_DOCUMENT_ROWS = 2_000
MAX_SHEETS = 32
MAX_CELL_TEXT = 32_767
COVERAGE = 0.98
UNIT = {'xlsx': 'sheets', 'csv': 'sheets'}
TEXTUTIL = '/usr/bin/textutil'
QLMANAGE = '/usr/bin/qlmanage'

# Shared with migrations.REFERENCE_DDL (schema epoch 7); keep them identical.
EXPORT_DDL = """
CREATE TABLE IF NOT EXISTS export_records(id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,plan_id TEXT NOT NULL,
  format TEXT NOT NULL,source TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS export_records_chat ON export_records(chat_id,created);
CREATE TABLE IF NOT EXISTS export_saves(id TEXT PRIMARY KEY,export_id TEXT NOT NULL,chat_id TEXT NOT NULL,state TEXT NOT NULL,
  path TEXT NOT NULL,sha256 TEXT,verified INTEGER NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS export_saves_export ON export_saves(export_id,created);
"""

CHECK_LABELS = {
    'parsed': 'The file opens and parses',
    'text_coverage': 'Text matches the source',
    'cells_match': 'Cells match source values and types',
    'sheet_count': 'Every table has its sheet',
    'no_formulas': 'No formulas',
    'formulas_neutralized': 'Formula-like text is neutralized',
    'no_external_refs': 'No external links, scripts or attachments',
    'no_macros': 'No macros or embedded objects',
    'tables_preserved': 'Tables keep their rows and columns',
    'resanitized_equal': 'Re-sanitizing gives the same bytes',
    'head_matches': 'The page header is Pebble\'s fixed template',
    'csp': 'The content policy blocks scripts and network',
    'no_scripts': 'No scripts or event handlers',
    'thumbnail': 'Quick Look shows content within the margins',
    'page_size': 'Pages use the chosen paper size',
    'blank_pages': 'No blank pages',
    'margins': 'Nothing is printed in the page margin',
    'answer_key_absent': 'The answer key is left out',
}


class ExportError(ValueError):
    """A refusal shown to the person; nothing was written."""


class ExportCancelled(Exception):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


_INVALID = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]')
_IDENT = re.compile(r'[A-Za-z0-9_-]{1,64}')


def _clean(text):
    return _INVALID.sub('�', text.replace('\r\n', '\n').replace('\r', '\n'))


def _ident(value, label='export'):
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise ExportError(f'Use a valid {label} ID.')
    return value


# Host block model


class _Blocks:
    """Bounded headings, paragraphs, lists, tables, code and sanitized static HTML."""

    def __init__(self):
        self.blocks, self.chars, self.cells, self.notes = [], 0, 0, []

    def _text(self, value, limit=MAX_CELL_TEXT):
        if not isinstance(value, str):
            raise ExportError('This source contains a value Pebble cannot export.')
        value = _clean(value)
        if len(value) > limit:
            raise ExportError('A piece of text in this source is longer than exports allow.')
        self.chars += len(value)
        if self.chars > MAX_SOURCE_TEXT:
            raise ExportError('This source is larger than 1,000,000 characters, the export limit.')
        return value

    def _add(self, block):
        if len(self.blocks) >= MAX_BLOCKS:
            raise ExportError('This source has more than 4,000 parts, the export limit.')
        self.blocks.append(block)

    def heading(self, text, level=2):
        text = self._text(text).strip()
        if text:
            self._add({'type': 'heading', 'level': level, 'text': text})

    def paragraph(self, text):
        text = self._text(text).strip('\n')
        if text.strip():
            self._add({'type': 'paragraph', 'text': text})

    def items(self, items, ordered=False):
        items = [self._text(item).strip() for item in items]
        if any(items):
            self._add({'type': 'list', 'ordered': ordered, 'items': [item for item in items if item]})

    def code(self, text, language=None):
        self._add({'type': 'code', 'text': self._text(text, MAX_SOURCE_TEXT), 'language': language})

    def _cell(self, value):
        if type(value) is str:
            return self._text(value)
        if type(value) is float and not math.isfinite(value):
            raise ExportError('Tables can only export finite numbers.')
        if value is None or type(value) in (bool, int, float):
            return value
        raise ExportError('Table cells must be text, numbers, true/false or empty.')

    def table(self, columns, rows, caption=None):
        if not 1 <= len(columns) <= MAX_COLUMNS:
            raise ExportError('Tables can export one to 64 columns.')
        if len(rows) > MAX_ROWS:
            raise ExportError('A table has more than 10,000 rows, the export limit.')
        self.cells += len(columns) * (len(rows) + 1)
        if self.cells > MAX_CELLS:
            raise ExportError('This source has more than 200,000 table cells, the export limit.')
        block = {'type': 'table', 'caption': self._text(caption).strip() if caption else None,
                 'columns': [self._text(label) for label in columns],
                 'rows': [[self._cell(value) for value in row] for row in rows]}
        if any(len(row) != len(columns) for row in block['rows']):
            raise ExportError('Every table row needs one value per column.')
        self._add(block)

    def html(self, source, css):
        try:
            body, style = static_html(source), static_css(css)
        except ValueError as exc:
            raise ExportError(str(exc)) from None
        self._text(formats.visible_text(body), MAX_SOURCE_TEXT)
        self._add({'type': 'html', 'html': body, 'css': style})


_FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})\s*([\w+.#-]{0,30})')
_HEADING = re.compile(r'^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$')
_BULLET = re.compile(r'^\s*[-*+]\s+(.*)$')
_ORDERED = re.compile(r'^\s*\d{1,9}[.)]\s+(.*)$')
_RULE = re.compile(r'^\s{0,3}([-*_])(?:\s*\1){2,}\s*$')
_ROW = re.compile(r'^\s*\|.*\|\s*$')
_SEPARATOR = re.compile(r'^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$')


def _inline(text):
    """Markdown emphasis and links become plain text; a URL stays visible, never a link."""
    text = re.sub(r'!\[([^\]]*)\]\(([^)\s]*)[^)]*\)', lambda m: '[image: ' + (m.group(1) or 'untitled') + ']', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)\s]+)[^)]*\)', lambda m: m.group(1) + ' (' + m.group(2) + ')', text)
    text = re.sub(r'(\*\*|__)(?=\S)(.+?)(?<=\S)\1', r'\2', text)
    text = re.sub(r'(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])', r'\1', text)
    return re.sub(r'`([^`\n]+)`', r'\1', text)


def _infer(text):
    """Markdown table text that is a canonical number becomes a typed number."""
    if re.fullmatch(r'-?(?:0|[1-9]\d{0,14})', text):
        return int(text)
    if re.fullmatch(r'-?(?:0|[1-9]\d{0,14})\.\d{1,15}', text) and repr(float(text)) == text:
        return float(text)
    return text


def _cells(line):
    line = line.strip()
    line = line[1:] if line.startswith('|') else line
    line = line[:-1] if line.endswith('|') and not line.endswith('\\|') else line
    return [cell.strip().replace('\\|', '|') for cell in re.split(r'(?<!\\)\|', line)]


def markdown_blocks(builder, text):
    lines, index, paragraph = _clean(text).split('\n'), 0, []

    def flush():
        if paragraph:
            builder.paragraph(_inline(' '.join(paragraph)))
            paragraph.clear()
    while index < len(lines):
        line = lines[index]
        fence = _FENCE.match(line)
        if fence:
            flush()
            marker, body = fence.group(1), []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(marker):
                body.append(lines[index])
                index += 1
            builder.code('\n'.join(body), fence.group(2) or None)
            index += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            builder.heading(_inline(heading.group(2)), min(4, len(heading.group(1)) + 1))
        elif _RULE.match(line):
            flush()
        elif _ROW.match(line) and index + 1 < len(lines) and '-' in lines[index + 1] and _SEPARATOR.match(lines[index + 1]):
            flush()
            columns = [_inline(cell) or f'Column {n}' for n, cell in enumerate(_cells(line), 1)]
            rows = []
            index += 2
            while index < len(lines) and _ROW.match(lines[index]):
                cells = [_infer(_inline(cell)) for cell in _cells(lines[index])][:len(columns)]
                rows.append(cells + [None] * (len(columns) - len(cells)))
                index += 1
            builder.table(columns, rows)
            continue
        elif _BULLET.match(line) or _ORDERED.match(line):
            flush()
            ordered = bool(_ORDERED.match(line))
            pattern, items = (_ORDERED if ordered else _BULLET), []
            while index < len(lines) and (pattern.match(lines[index]) or (items and lines[index].startswith(('  ', '\t')) and lines[index].strip())):
                match = pattern.match(lines[index])
                if match:
                    items.append(_inline(match.group(1)))
                else:
                    items[-1] += ' ' + _inline(lines[index].strip())
                index += 1
            builder.items(items, ordered)
            continue
        elif not line.strip():
            flush()
        else:
            paragraph.append(re.sub(r'^\s{0,3}>\s?', '', line).strip())
        index += 1
    flush()


_COMPONENTS = COMPONENTS  # The prototype validator's exact component shapes.


def _prototype(builder, data):
    """Declarative screens only; href, url, src, action, on*, style, html and script are refused."""
    def short(value, limit=240):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ExportError('This app demo has an invalid label.')
        return builder._text(value.strip())
    if type(data) is not dict or not {'screens', 'start'} <= set(data) or set(data) - {'viewport', 'start', 'theme', 'screens'}:
        raise ExportError('This app demo uses fields exports do not support.')
    screens = data['screens']
    if type(screens) is not list or not 1 <= len(screens) <= 12:
        raise ExportError('App demos export one to twelve screens.')
    ids = [screen.get('id') if type(screen) is dict else None for screen in screens]
    if any(not isinstance(i, str) or not _IDENT.fullmatch(i) for i in ids) or len(set(ids)) != len(ids) or data['start'] not in ids:
        raise ExportError('App demo screens need unique IDs and an existing start screen.')
    result = []
    for screen in screens:
        if set(screen) != {'id', 'title', 'components'} or type(screen['components']) is not list or len(screen['components']) > 30:
            raise ExportError('App demo screens need an ID, a title and at most 30 components.')
        components = []
        for item in screen['components']:
            shape = _COMPONENTS.get(item.get('type')) if type(item) is dict else None
            if not shape or not shape[0] <= set(item) or set(item) - shape[0] - shape[1]:
                raise ExportError('This app demo has a component exports do not support.')
            entry = {'type': item['type']}
            for key in sorted(set(item) - {'type', 'items', 'goto'}):
                entry[key] = short(item[key], 2000 if key == 'body' else 240)
            if 'goto' in item:
                if item['goto'] not in ids:
                    raise ExportError('An app demo control points to a missing screen.')
                entry['goto'] = item['goto']
            if item['type'] == 'list':
                if type(item['items']) is not list or len(item['items']) > 30:
                    raise ExportError('App demo lists hold at most 30 items.')
                entry['items'] = [short(value) for value in item['items']]
            elif item['type'] == 'nav':
                if type(item['items']) is not list or not 1 <= len(item['items']) <= 8:
                    raise ExportError('App demo navigation holds one to eight links.')
                links = []
                for link in item['items']:
                    if type(link) is not dict or set(link) != {'label', 'goto'} or link['goto'] not in ids:
                        raise ExportError('An app demo control points to a missing screen.')
                    links.append({'label': short(link['label']), 'goto': link['goto']})
                entry['items'] = links
            components.append(entry)
        result.append({'id': screen['id'], 'title': short(screen['title'], 120), 'components': components})
    builder._add({'type': 'screens', 'start': data['start'], 'screens': result})


def _dashboard(builder, data, options, notes):
    """Inline panels become host blocks. A bound panel's live host data is read only inside Pebble."""
    try:
        data = validate_native('dashboard', data)
    except ValueError as exc:
        raise ExportError('Dashboard exports need a valid dashboard view. ' + str(exc)) from None
    live = 0
    for panel in data['panels']:
        builder.heading(panel['title'], 2)
        if 'binding' in panel:
            live += 1
            builder.paragraph('Live host data (' + panel['binding']['source'] + ') is shown only in Pebble and is not part of this export.')
        elif panel['kind'] == 'metric':
            metric = panel['data']
            builder.table(['Value', 'Unit', 'Change'], [[metric['value'], metric.get('unit'), metric.get('delta')]], metric.get('caption'))
        else:
            _artifact_blocks(builder, panel['kind'], panel['data'], options, notes)
    if live:
        notes.append(f'{live} live panel(s) were left out: their host data is read only inside Pebble.')


def _quiz_source(store, cid, ident, revision, public, include):
    """A host-graded quiz's key is read for the person's include-answers choice, and otherwise
    only so the readback can prove no answer or explanation reached the file."""
    with store.lock:
        key = quiz_grading.export_key(store.db, cid, ident, revision, public)
    if key is None:
        if include:
            raise ExportError('This quiz\'s answer key is unavailable, so its answers cannot be exported.')
        return public
    answers = {item['id']: item for item in key['questions']}
    return {'duration_minutes': public['duration_minutes'], 'questions': [
        {'question': q['question'], 'choices': q['choices'], 'answer': answers[q['id']]['answer'],
         'explanation': answers[q['id']]['explanation']} for q in public['questions']]}


def _artifact_blocks(builder, kind, data, options, notes):
    secrets_ = []
    if kind == 'table':
        columns = data['columns']
        builder.table([c['label'] for c in columns], [[row[c['id']] for c in columns] for row in data['rows']], data.get('caption'))
    elif kind == 'chart':
        keys, labels = [data['x_key'], *data['y_keys']], data.get('labels', {})
        builder.table([labels.get(key, key) for key in keys], [[row[key] for key in keys] for row in data['rows']],
                      data.get('caption') or f'{data["type"].capitalize()} chart data')
    elif kind == 'plan':
        builder.table(['Step', 'Status'], [[step['title'], step['status'].replace('_', ' ')] for step in data['steps']])
    elif kind == 'evidence':
        builder.table(['Item', 'Evidence', 'Summary'], [[i['label'], i['evidence_id'], i.get('summary')] for i in data['items']], data.get('caption'))
    elif kind == 'form':
        if data.get('description'):
            builder.paragraph(data['description'])
        builder.items([f['label'] + (': ' + ' / '.join(o['label'] for o in f['options']) if f['type'] == 'choice' else '') for f in data['fields']])
        builder.paragraph('Answers to this form are not part of the export.')
    elif kind == 'quiz':
        include = options['include_answers']
        keyed = any('answer' in q for q in data['questions'])
        for number, question in enumerate(data['questions'], 1):
            builder.heading(f'Question {number}', 3)
            builder.paragraph(question['question'])
            builder.items(question['choices'], ordered=True)
            if include and 'answer' in question:
                builder.paragraph('Correct answer: ' + question['choices'][question['answer']])
                if question.get('explanation'):
                    builder.paragraph('Explanation: ' + question['explanation'])
            elif question.get('explanation') and len(question['explanation'].strip()) >= 12:
                secrets_.append(question['explanation'])
        if include and not keyed:
            notes.append('This quiz keeps its answers with the host, so the export has no answers.')
        elif not include:
            notes.append('Answers were left out. Choose "Include the answer key" to add them.')
            secrets_.append('Correct answer:')
    elif kind == 'diagram':
        names = {node['id']: node['label'] for node in data['nodes']}
        builder.items([node['label'] for node in data['nodes']])
        if data['edges']:
            builder.table(['From', 'To', 'Label'], [[names[e['from']], names[e['to']], e['label']] for e in data['edges']], 'Connections')
    elif kind == 'html':
        builder.html(data['html'], data.get('css', ''))
        if isinstance(data.get('script'), str) and data['script'].strip():
            notes.append('Exports never include scripts; this view\'s script was left out.')
    elif kind == 'prototype':
        _prototype(builder, data)
    elif kind == 'dashboard':
        _dashboard(builder, data, options, notes)
    else:
        raise ExportError('This kind of view cannot be exported.')
    return secrets_ if kind == 'quiz' and not options['include_answers'] else None


def _utc(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return ''
    return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(value))


def _filename(title, extension):
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', '-', title)
    name = ' '.join(name.split()).strip(' .-')[:80] or 'Pebble export'
    return f'{name}.{extension}'


def _sheet_names(blocks):
    names, used, heading = [], set(), None
    for block in blocks:
        if block['type'] == 'heading':
            heading = block['text']
        if block['type'] != 'table':
            continue
        base = re.sub(r"[\[\]:*?/\\\x00-\x1f]", ' ', block.get('caption') or heading or f'Table {len(names) + 1}')
        base = ' '.join(base.split()).strip("' ")[:28] or f'Table {len(names) + 1}'
        name, number = base, 2
        while name.casefold() in used:
            name = f'{base[:26]} {number}'
            number += 1
        used.add(name.casefold())
        names.append((name, block))
    return names


# Confined subprocesses


def _q(path):
    return json.dumps(str(path), ensure_ascii=False)


# V1-051: name resolution (mDNSResponder, dnssd) is denied as well as URL loading, so textutil has no DNS channel.
_NO_SERVICES = ('(deny mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.securityd.xpc") (global-name "com.apple.secd")'
                ' (global-name "com.apple.cfnetwork.AuthBroker") (global-name-regex #"^com\\.apple\\.WebKit") (global-name-regex #"nsurl")'
                ' (global-name-regex #"cfnetwork") (global-name-regex #"^com\\.apple\\.networkd")'
                ' (global-name "com.apple.mDNSResponder") (global-name "com.apple.dnssd.service")'
                ' (global-name "com.apple.SecurityServer"))')
# textutil needs a few system lookups (font and preference services); WebKit,
# URL-loading and credential services, the network and every file write are denied.
TEXTUTIL_PROFILE = f'''(version 1) (deny default)
  (allow process-exec (literal "{TEXTUTIL}"))
  (allow sysctl-read) (allow file-read-metadata) (allow mach-lookup) {_NO_SERVICES}
  (allow file-read* (literal "/") (subpath "/System") (subpath "/usr") (subpath "/Library/Fonts") (subpath "/private/var/db/timezone")
    (subpath "/private/var/db/dyld") (subpath "/private/etc") (literal "/dev/null") (literal "/dev/urandom"))
  (allow file-write* (literal "/dev/null"))
  (allow ipc-posix-shm-read-data ipc-posix-shm-read-metadata)'''


def _run(command, data, *, timeout, cancel=None, limit, env, cwd=None):
    """Run one owned process group with bounded output, a deadline and cancellation."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, cwd=cwd, close_fds=True, start_new_session=True)
    out, err, sent = bytearray(), bytearray(), 0
    deadline = time.monotonic() + timeout
    try:
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        with selectors.DefaultSelector() as selector:
            if data:
                selector.register(process.stdin, selectors.EVENT_WRITE, 'in')
            else:
                process.stdin.close()
            selector.register(process.stdout, selectors.EVENT_READ, out)
            selector.register(process.stderr, selectors.EVENT_READ, err)
            while selector.get_map():
                if cancel is not None and cancel.is_set():
                    raise ExportCancelled()
                if time.monotonic() > deadline:
                    raise ExportError('timeout')
                for key, _ in selector.select(0.05):
                    if key.data == 'in':
                        try:
                            sent += os.write(key.fileobj.fileno(), data[sent:sent + 65536])
                        except BrokenPipeError:
                            sent = len(data)
                        if sent >= len(data):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(block)
                    if len(out) > limit or len(err) > 65536:
                        raise ExportError('output')
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return process.returncode, bytes(out), bytes(err)
    except subprocess.TimeoutExpired:
        raise ExportError('timeout') from None
    finally:
        # Sweep the whole owned group, including helpers that outlived the leader.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass


def _environment(home='/var/empty'):
    return {'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8', 'HOME': home}


def _worker_command():
    """The readback worker: no network, no writes, reads only its own runtime."""
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        return None
    frozen = bool(getattr(sys, 'frozen', False))
    executable = Path(sys.executable)
    resolved = executable.resolve()
    worker = Path(formats.__file__).resolve()
    runtime = Path(getattr(sys, '_MEIPASS', resolved.parent) if frozen else sys.prefix).resolve()
    base = Path(sys.base_prefix).resolve()
    if runtime in {Path('/'), Path.home()} or base in {Path('/'), Path.home()}:
        return None
    launcher = base / 'Resources/Python.app/Contents/MacOS/Python'
    executables = {executable, resolved} | ({launcher, launcher.resolve()} if not frozen and launcher.exists() else set())
    ancestors = {*resolved.parents, *executable.parents, *worker.parents, *runtime.parents, *base.parents}
    handles = ' '.join('(literal ' + _q(path) + ')' for path in sorted(ancestors, key=str))
    # Frozen: the PyInstaller bootloader reads its own executable to load the embedded archive, and in the
    # one-folder layout the executable sits outside sys._MEIPASS (found by the Phase 17 frozen pre-check).
    script = (' '.join('(literal ' + _q(path) + ')' for path in sorted({executable, resolved}, key=str)) if frozen
              else '(literal ' + _q(worker) + ')')
    profile = f'''(version 1) (deny default)
  (allow process-exec {' '.join('(literal ' + _q(path) + ')' for path in sorted(executables, key=str))})
  (allow sysctl-read) (allow file-read-metadata)
  (allow file-read* {handles} {script} (subpath {_q(runtime)}) (subpath {_q(base)}) (subpath "/System") (subpath "/usr/lib")
    (subpath "/private/var/db/dyld") (literal "/dev/null") (literal "/dev/urandom"))
  (allow file-write* (literal "/dev/null"))
  (deny file-read-data (regex #"(^|/)(\\.env[^/]*|\\.ssh|\\.aws|\\.gnupg|credentials|secrets|vault)(/|$)"))'''
    command = [str(executable), '--export-worker'] if frozen else [str(executable), '-I', '-B', str(worker)]
    return ['/usr/bin/sandbox-exec', '-p', profile, *command]


def _read_file(path, limit=formats.MAX_FILE):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ExportError('Export files must be regular files.')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ExportError('This export is larger than 32 MB, the readback limit.')
    return data


def _quicklook(path, cancel=None):
    """First-page PNG from Quick Look, or None. qlmanage applies Apple's own sandbox
    and refuses to nest inside sandbox-exec, so it runs with a sanitized environment,
    a deadline and an owned process group instead (see HARNESS_EXPORTS.md)."""
    if sys.platform != 'darwin' or not Path(QLMANAGE).exists():
        return None
    with tempfile.TemporaryDirectory(prefix='pebble-export-thumb-') as scratch:
        folder = Path(scratch).resolve()
        try:
            code, _, _ = _run([QLMANAGE, '-t', '-s', '512', '-o', str(folder), str(Path(path).resolve())], b'',
                              timeout=30, cancel=cancel, limit=65536, env=_environment(str(folder)))
        except (ExportError, OSError):
            return None
        images = [item for item in folder.iterdir() if item.suffix == '.png' and not item.is_symlink()]
        if code or len(images) != 1:
            return None
        try:
            return _read_file(images[0], formats.MAX_THUMBNAIL)
        except (OSError, ExportError):
            return None


def _compare(actual, wanted, typed):
    empty = ['e', ''] if typed else ''
    rows = max(len(actual), len(wanted))
    columns = max([len(r) for r in actual] + [len(r) for r in wanted] + [0])
    for r in range(rows):
        for c in range(columns):
            found = actual[r][c] if r < len(actual) and c < len(actual[r]) else empty
            expected = wanted[r][c] if r < len(wanted) and c < len(wanted[r]) else empty
            if found != expected:
                short = lambda v: (v[:80] if isinstance(v, str) else [v[0], v[1][:80]])
                return False, {'row': r + 1, 'column': formats.column_name(c), 'expected': short(expected), 'found': short(found)}
    return True, None


def readback(path, fmt, expect=None, *, cancel=None, thumbnail=True):
    """Read an export file back in the confined worker.

    Returns {'pages'|'sheets', 'text_coverage', 'blank_pages', 'external_refs', 'layout'}.
    ``expect`` (host-built) adds coverage and cell comparisons; it is never read from the file.
    """
    if fmt not in FORMATS:
        raise ExportError('Unsupported export format.')
    unit, expect = UNIT.get(fmt, 'pages'), expect or {}
    failed = lambda reason: {unit: None, 'text_coverage': None, 'blank_pages': [], 'external_refs': [], 'layout': [], 'error': reason}
    data = _read_file(path)
    image = _quicklook(path, cancel) if thumbnail and fmt in ('docx', 'pdf') else None
    command = _worker_command()
    if command is None:
        return failed('unsupported')
    header = json.dumps({'op': 'readback', 'format': fmt, 'size': len(data), 'thumbnail': len(image or b'')}).encode() + b'\n'
    try:
        code, out, _ = _run(command, header + data + (image or b''), timeout=60, cancel=cancel,
                            limit=48 * 1024 * 1024, env=_environment())
        facts = json.loads(out) if not code else {'error': 'runtime'}
    except ExportError as exc:
        return failed(str(exc))
    except OSError:
        return failed('runtime')
    except (ValueError, UnicodeDecodeError):
        return failed('invalid')
    if type(facts) is not dict or 'error' in facts:
        return failed(facts.get('error', 'invalid') if type(facts) is dict else 'invalid')
    coverage = formats.coverage(expect['text'], facts.get('text', '')) if 'text' in expect else None
    layout = []
    if fmt in ('xlsx', 'csv'):
        wanted = expect.get('sheets')
        for index, sheet in enumerate(facts['sheets']):
            entry = {key: sheet[key] for key in ('name', 'rows', 'columns', 'formulas', 'unprotected_triggers')}
            if wanted is not None:
                match, mismatch = _compare(sheet['cells'], wanted[index]['cells'], fmt == 'xlsx') if index < len(wanted) else (False, None)
                entry.update(cells_match=match, first_mismatch=mismatch)
            layout.append(entry)
        layout.append({'package': fmt, 'macros': bool(facts.get('macros')), 'byte_order_mark': facts.get('bom')})
        count = len(facts['sheets'])
    else:
        layout = list(facts.get('layout') or [])
        count = facts.get('pages')
        if fmt == 'docx':
            layout.append({'package': 'docx', 'macros': bool(facts.get('macros'))})
    if thumbnail and fmt in ('docx', 'pdf'):
        layout.append({'thumbnail': facts.get('thumbnail'), 'source': 'quicklook' if image else 'unavailable'})
    if expect.get('secrets') is not None:
        text = formats.squash(facts.get('text', ''))
        leaked = [secret for secret in expect['secrets'] if formats.squash(secret) in text]
        layout.append({'answer_key': 'present' if leaked else 'absent'})
    blank = list(facts.get('blank_pages') or [])
    if fmt == 'docx' and (facts.get('thumbnail') or {}).get('blank'):
        blank = [1]
    return {unit: count, 'text_coverage': coverage, 'blank_pages': blank,
            'external_refs': list(facts.get('external_refs') or [])[:50], 'layout': layout}


def checks(fmt, result, expect):
    """Every check a verified receipt needs. A missing fact is a failed check."""
    out = []

    def add(name, passed, detail=''):
        out.append({'name': name, 'label': CHECK_LABELS[name], 'passed': bool(passed), 'detail': detail})
    if result.get('error'):
        add('parsed', False, f'The file could not be read back ({result["error"]}).')
        return out
    add('parsed', True, 'Parsed in the confined readback worker.')
    layout = result['layout']
    pick = lambda key: [entry for entry in layout if key in entry]
    refs = result['external_refs']
    add('no_external_refs', not refs, 'None found.' if not refs else
        f'{len(refs)} found: ' + ', '.join(sorted({ref.get("kind", "reference") for ref in refs})))
    coverage = result['text_coverage']
    add('text_coverage', coverage is not None and coverage >= COVERAGE,
        'Not measured.' if coverage is None else f'{coverage:.1%} of the source words were found (98% needed).')
    package = (pick('package') or [{}])[0]
    if fmt in ('xlsx', 'csv'):
        sheets = pick('rows')
        add('sheet_count', len(sheets) == len(expect.get('sheets') or []), f'{len(sheets)} of {len(expect.get("sheets") or [])} sheets.')
        mismatch = next((entry['first_mismatch'] for entry in sheets if not entry.get('cells_match')), None)
        add('cells_match', sheets and all(entry.get('cells_match') for entry in sheets),
            'All cells match.' if all(entry.get('cells_match') for entry in sheets) else
            ('First difference at ' + mismatch['column'] + str(mismatch['row']) + '.' if mismatch else 'Cells were not compared.'))
        unprotected = sum(entry['unprotected_triggers'] for entry in sheets)
        add('formulas_neutralized', unprotected == 0, 'No formula-like text is left unprotected.' if not unprotected else f'{unprotected} cells start like a formula.')
        if fmt == 'xlsx':
            formulas = sum(entry['formulas'] for entry in sheets)
            add('no_formulas', formulas == 0, 'No formula cells.' if not formulas else f'{formulas} formula cells.')
            add('no_macros', not package.get('macros', True), 'No macros.' if not package.get('macros', True) else 'Macro or embedded parts found.')
    elif fmt == 'html':
        document = (pick('resanitized_equal') or [{}])[0]
        add('resanitized_equal', document.get('resanitized_equal') is True, '')
        add('head_matches', document.get('head_sha256') == expect.get('head_sha256'), '')
        add('csp', document.get('csp') == formats.CSP, '')
        scripts = (document.get('scripts'), document.get('event_handlers'), document.get('javascript_urls'))
        add('no_scripts', scripts == (0, 0, 0), 'None found.' if scripts == (0, 0, 0) else 'Scripts or handlers found.')
    elif fmt in ('docx', 'pdf'):
        if fmt == 'docx':
            add('no_macros', not package.get('macros', True), 'No macros.' if not package.get('macros', True) else 'Macro or embedded parts found.')
            if expect.get('tables'):
                found = sum(entry.get('tables', 0) for entry in pick('paragraphs'))
                add('tables_preserved', found >= expect['tables'],
                    f'{found} of {expect["tables"]} tables kept.' + ('' if found >= expect['tables'] else
                    ' macOS textutil writes Word tables as plain paragraphs; export XLSX for the table or HTML for layout.'))
        preview = (pick('thumbnail') or [{}])[0].get('thumbnail')
        add('thumbnail', bool(preview) and not preview['blank'] and not preview['margin_ink'],
            'Quick Look could not render a preview.' if not preview else
            ('The first page renders empty.' if preview['blank'] else 'Ink reaches the page edge.' if preview['margin_ink'] else 'First page has content inside the margins.'))
        if fmt == 'pdf':
            width, height = formats.PAPER[expect.get('paper', 'a4')]
            pages = pick('page')
            sized = all(abs(p['width'] - width) <= 2 and abs(p['height'] - height) <= 2 for p in pages)
            add('page_size', pages and sized, f'{len(pages)} pages.' if sized else 'Some pages differ from the chosen paper size.')
            add('blank_pages', not result['blank_pages'], 'None.' if not result['blank_pages'] else 'Blank: ' + ', '.join(map(str, result['blank_pages'][:10])))
            margin = sum(p['margin_text'] for p in pages)
            add('margins', margin == 0, 'No text in the margin band.' if not margin else f'{margin} text runs in the margin band.')
    if expect.get('secrets') is not None:
        key = (pick('answer_key') or [{}])[0].get('answer_key')
        add('answer_key_absent', key == 'absent', '' if key == 'absent' else 'Answer text appears in the file.')
    return out


class Exports:
    def __init__(self, home, store, artifacts, *, runs=None, interactions=None, goals=None, emit=None):
        self.home, self.store, self.artifacts = Path(home), store, artifacts
        self.runs, self.interactions, self.goals, self.emit = runs, interactions, goals, emit
        self.lock = threading.RLock()
        self.plans, self.tokens, self.inflight, self.native = {}, {}, {}, {}
        self.native_pdf = False
        self._pdf = threading.Lock()
        with store.lock:
            if store.db.in_transaction:
                raise ExportError('Exports require an outside transaction boundary.')
            with store.db as db:
                for statement in EXPORT_DDL.split(';'):
                    if statement.strip():
                        db.execute(statement)
                # A render or save without a settled outcome is never retried.
                now = time.time()
                for ident, receipt in db.execute("SELECT id,receipt FROM export_records WHERE state='rendering'").fetchall():
                    data = {**json.loads(receipt), 'status': 'failed', 'file': None,
                            'reason': 'The service stopped before this export finished. Nothing was retried.'}
                    db.execute("UPDATE export_records SET state='failed',receipt=?,updated=? WHERE id=?", (canonical(data), now, ident))
                db.execute("UPDATE export_saves SET state='uncertain',updated=? WHERE state='writing'", (now,))

    # Sources

    def _resolve(self, cid, source, options):
        if type(source) is not dict or set(source) - {'kind', 'id', 'revision'} or source.get('kind') not in SOURCE_KINDS:
            raise ExportError('Choose a view, message, goal or run to export.')
        kind, ident, revision = source['kind'], _ident(source.get('id'), 'source'), source.get('revision')
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ExportError('Use a positive revision number.')
        builder, notes, secrets_, extra = _Blocks(), [], None, {}
        if kind == 'artifact':
            row = self.artifacts.get(cid, ident)
            if revision is not None and row['revision'] != revision:
                raise ExportError(f'This view changed to revision {row["revision"]}. Preview the export again.')
            revision, title = row['revision'], row['title']
            if row['kind'] == 'mcp_media':
                raise ExportError('Media views cannot be exported.')
            builder.heading(title, 1)
            data = row['data']
            if row['kind'] == 'quiz' and quiz_grading.is_public(data):
                data = _quiz_source(self.store, cid, ident, revision, data, options['include_answers'])
            secrets_ = _artifact_blocks(builder, row['kind'], data, options, notes)
            raw = {'artifact': ident, 'revision': revision, 'kind': row['kind'], 'title': title, 'data_hash': row['data_hash']}
            extra['artifact_kind'] = row['kind']
        elif kind == 'message':
            rows = self.store.query('SELECT id,role,content FROM messages WHERE id=? AND chat_id=?', (ident, cid))
            if not rows or rows[0]['role'] not in ('user', 'assistant') or not isinstance(rows[0]['content'], str):
                raise ExportError('Message not found in this conversation.')
            content = rows[0]['content']
            first = next((line.strip('# ').strip() for line in content.split('\n') if line.strip()), '')
            title = _inline(first)[:80] or 'Message'
            builder.heading(title, 1)
            markdown_blocks(builder, content)
            raw = {'message': ident, 'role': rows[0]['role'], 'content_sha256': hashlib.sha256(content.encode('utf-8', 'surrogatepass')).hexdigest()}
        elif kind == 'run_report':
            if self.runs is None:
                raise ExportError('Run reports are not available in this build.')
            run = self.runs.get(ident, cid)
            tools = self.runs.tools(ident)
            plan = self.interactions.plan(ident) if self.interactions is not None else None
            title = 'Run report ' + ident[:8]
            builder.heading(title, 1)
            builder.table(['Field', 'Value'], [['State', str(run.get('state', ''))], ['Steps', run.get('steps')],
                                               ['Started', _utc(run.get('created'))], ['Updated', _utc(run.get('updated'))],
                                               ['Error', run.get('error') or '']], 'Run')
            outcome = lambda tool: tool['result'].get('status', '') if isinstance(tool.get('result'), dict) else ''
            rows = [[tool['call_id'], tool['name'], tool['state'], str(outcome(tool))] for tool in tools]
            if rows:
                builder.table(['Call', 'Tool', 'State', 'Outcome'], rows, 'Tools')
            if plan and plan.get('steps'):
                builder.table(['Step', 'Status'], [[step.get('title', ''), str(step.get('status', '')).replace('_', ' ')] for step in plan['steps']], 'Plan')
            builder.paragraph('Tool arguments and outputs are not part of this report.')
            raw = {'run': ident, 'state': run.get('state'), 'steps': run.get('steps'), 'updated': run.get('updated'), 'error': run.get('error'),
                   'tools': [[r[0], r[1], r[2], r[3]] for r in rows], 'plan': plan and [[s.get('title'), s.get('status')] for s in plan['steps']]}
        else:
            # Goals.get(chat_id, goal_id) is the person's goal snapshot (X17 O2: there is no Goals.snapshot).
            read = getattr(self.goals, 'get', None)
            if read is None:
                raise ExportError('Goal reports are not available in this build.')
            try:
                goal = read(cid, ident)
            except ValueError:
                goal = None
            if type(goal) is not dict or not isinstance(goal.get('title'), str):
                raise ExportError('Goal not found in this conversation.')
            if revision is not None and goal.get('revision') != revision:
                raise ExportError(f'This goal changed to revision {goal.get("revision")}. Preview the export again.')
            revision, title = goal.get('revision'), goal['title']
            builder.heading(title, 1)
            builder.paragraph(f'State: {goal.get("state", "")} · revision {revision}')
            criteria = [c for c in goal.get('criteria') or [] if type(c) is dict and c.get('status') not in ('candidate', 'rejected')]
            if criteria:
                builder.table(['Criterion', 'Kind', 'Status'], [[str(c.get('text', '')), str(c.get('kind', '')).replace('_', ' '), str(c.get('status', ''))] for c in criteria], 'Completion criteria')
            steps = [s for s in goal.get('steps') or [] if type(s) is dict]
            if steps:
                builder.table(['Step', 'Status', 'Depends on', 'Blocker'],
                              [[str(s.get('title', '')), str(s.get('status', '')).replace('_', ' '), ', '.join(map(str, s.get('depends_on') or [])),
                                str(((s.get('blocker') or {}).get('kind') if isinstance(s.get('blocker'), dict) else s.get('blocker_kind')) or '')]
                               for s in steps], 'Steps')
            raw = {'goal': ident, 'snapshot': goal}
        return {'kind': kind, 'id': ident, 'revision': revision, 'digest': digest(raw), 'title': title,
                'blocks': builder.blocks, 'notes': notes, 'secrets': secrets_, **extra}

    @staticmethod
    def _format(fmt):
        if fmt == 'pptx':
            raise ExportError('Slide (PPTX) exports arrive in a later update.')
        if fmt not in FORMATS:
            raise ExportError('Choose PDF, DOCX, XLSX, CSV or HTML.')
        return fmt

    @staticmethod
    def _options(options):
        options = {} if options is None else options
        if type(options) is not dict or set(options) - {'include_answers', 'paper'}:
            raise ExportError('Export options accept include_answers and paper only.')
        include, paper = options.get('include_answers', False), options.get('paper', 'a4')
        if type(include) is not bool or paper not in formats.PAPER:
            raise ExportError('Choose whether to include answers, and A4 or Letter paper.')
        return {'include_answers': include, 'paper': paper}

    @staticmethod
    def _fits(blocks, fmt):
        tables = [block for block in blocks if block['type'] == 'table']
        if not formats.block_text(blocks).strip():
            raise ExportError('This source has nothing to export.')
        if fmt == 'csv' and len(tables) != 1:
            raise ExportError('CSV holds exactly one table; this source has ' + str(len(tables)) + '. Use XLSX instead.')
        if fmt == 'xlsx' and not 1 <= len(tables) <= MAX_SHEETS:
            raise ExportError('XLSX needs one to 32 tables; this source has ' + str(len(tables)) + '.')
        if fmt in ('pdf', 'docx', 'html') and sum(len(t['rows']) for t in tables) > MAX_DOCUMENT_ROWS:
            raise ExportError('Documents hold up to 2,000 table rows. Export large tables as XLSX or CSV.')

    @staticmethod
    def _pages(blocks, fmt):
        if fmt in ('xlsx', 'csv'):
            return max(1, math.ceil(sum(len(b['rows']) + 1 for b in blocks if b['type'] == 'table') / 45))
        chars = len(formats.block_text([b for b in blocks if b['type'] not in ('table', 'code', 'screens')]))
        rows = sum(len(b['rows']) + 1 for b in blocks if b['type'] == 'table')
        lines = sum(b['text'].count('\n') + 1 for b in blocks if b['type'] == 'code')
        screens = sum(len(b['screens']) for b in blocks if b['type'] == 'screens')
        return max(1, math.ceil(chars / 2800 + rows / 38 + lines / 55) + screens)

    # Plans

    def preview(self, cid, source, fmt, options=None):
        fmt, options = self._format(fmt), self._options(options)
        resolved = self._resolve(cid, source, options)
        self._fits(resolved['blocks'], fmt)
        now, plan_id = time.time(), uuid.uuid4().hex
        plan = {'plan_id': plan_id, 'chat_id': cid, 'format': fmt, 'options': options, 'deadline': now + PLAN_SECONDS,
                'source': {key: resolved[key] for key in ('kind', 'id', 'revision', 'digest')},
                'pages_estimate': self._pages(resolved['blocks'], fmt)}
        with self.lock:
            for key in [k for k, p in self.plans.items() if p['deadline'] <= now]:
                self.plans.pop(key)
            mine = sorted((p['deadline'], k) for k, p in self.plans.items() if p['chat_id'] == cid)
            for _, key in mine[:max(0, len(mine) - MAX_PLANS + 1)]:
                self.plans.pop(key)
            self.plans[plan_id] = plan
        return {key: plan[key] for key in ('plan_id', 'source', 'format', 'pages_estimate', 'deadline')}

    def _take(self, cid, plan_id):
        with self.lock:
            plan = self.plans.get(plan_id) if isinstance(plan_id, str) else None
            if not plan or plan['chat_id'] != cid:
                raise ExportError('This export preview is no longer available. Preview it again.')
            self.plans.pop(plan_id)
        if plan['deadline'] <= time.time():
            raise ExportError('This export preview expired. Preview it again.')
        return plan

    def cancel(self, cid, plan_id):
        with self.lock:
            entry = self.inflight.get(plan_id)
            if not entry or entry[0] != cid:
                return False
            entry[1].set()
            return True

    # Records

    def _folder(self, cid, export_id, create=False):
        root = self.home / 'exports'
        for path in (root, root / _ident(cid, 'conversation'), root / cid / _ident(export_id)):
            if path.is_symlink():
                raise ExportError('The export folder is not a plain folder.')
            if create:
                path.mkdir(mode=0o700, exist_ok=path != root / cid / export_id)
        return root / cid / export_id

    def _save_record(self, receipt):
        with self.store.lock, self.store.db as db:
            db.execute('INSERT INTO export_records VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,'
                       'receipt=excluded.receipt,updated=excluded.updated',
                       (receipt['export_id'], receipt['chat_id'], receipt['plan_id'], receipt['format'], canonical(receipt['source']),
                        receipt['status'], canonical(receipt), receipt['created'], time.time()))

    def get(self, cid, export_id):
        rows = self.store.query('SELECT receipt FROM export_records WHERE id=? AND chat_id=?', (_ident(export_id), cid))
        if not rows:
            raise ExportError('Export not found in this conversation.')
        receipt = json.loads(rows[0]['receipt'])
        receipt['saves'] = self.store.query('SELECT id,state,path,sha256,verified,created FROM export_saves WHERE export_id=? ORDER BY created', (export_id,))
        for save in receipt['saves']:
            save['verified'] = bool(save['verified'])
        return receipt

    def list(self, cid):
        rows = self.store.query('SELECT receipt FROM export_records WHERE chat_id=? ORDER BY created DESC LIMIT 50', (cid,))
        keep = ('export_id', 'format', 'status', 'title', 'source', 'file', 'failed_checks', 'created', 'published')
        return [{key: receipt.get(key) for key in keep} for receipt in map(lambda r: json.loads(r['receipt']), rows)]

    # Rendering

    def _expect(self, fmt, resolved, options):
        blocks = resolved['blocks']
        tables = [block for block in blocks if block['type'] == 'table']
        # A sheet holds only labels and cells; a caption becomes the sheet name.
        sheet_text = '\n'.join(t for table in tables for t in [*table['columns'], *(formats.cell_text(v) for row in table['rows'] for v in row)])
        expect = {'text': sheet_text if fmt in ('xlsx', 'csv') else formats.block_text(blocks), 'paper': options['paper']}
        if fmt == 'docx':
            expect['tables'] = len(tables)
        if fmt == 'xlsx':
            expect['sheets'] = [{'name': name, 'cells': formats.sheet_cells(table)} for name, table in _sheet_names(blocks)]
        elif fmt == 'csv':
            expect['sheets'] = [{'name': 'CSV', 'cells': formats.sheet_cells(tables[0], typed=False)}]
        if resolved['secrets'] is not None:
            expect['secrets'] = resolved['secrets']
        return expect

    def _footer(self, resolved):
        revision = f' revision {resolved["revision"]}' if resolved['revision'] else ''
        return f'Exported from Pebble · {resolved["kind"].replace("_", " ")}{revision}. A local copy, not a publication.'

    def _write(self, fmt, resolved, options, folder, cancel):
        blocks, title = resolved['blocks'], resolved['title']
        if fmt == 'csv':
            return formats.write_csv(next(b for b in blocks if b['type'] == 'table')), {}
        if fmt == 'xlsx':
            if any(b['type'] == 'table' and any(type(v) is int and abs(v) > 2 ** 53 for row in b['rows'] for v in row) for b in blocks):
                resolved['notes'].append('Whole numbers longer than 15 digits were stored as text so no digits are lost.')
            if any(b['type'] != 'table' for b in blocks[1:]):
                resolved['notes'].append('Spreadsheets hold only the tables from this source.')
            return formats.write_xlsx(_sheet_names(blocks)), {}
        if fmt == 'html':
            head, body = formats.html_parts(title, blocks, 'html', options['paper'], self._footer(resolved))
            return (head + body + formats.TAIL).encode('utf-8'), {'head_sha256': hashlib.sha256(head.encode()).hexdigest()}
        if fmt == 'docx':
            if sys.platform != 'darwin' or not Path(TEXTUTIL).exists() or not Path('/usr/bin/sandbox-exec').exists():
                raise _Unavailable('DOCX export needs macOS textutil, which is not available here.')
            source = formats.html_document(title, blocks, 'docx', options['paper'], self._footer(resolved)).encode('utf-8')
            try:
                code, out, _ = _run(['/usr/bin/sandbox-exec', '-p', TEXTUTIL_PROFILE, TEXTUTIL, '-format', 'html', '-convert', 'docx',
                                     '-stdin', '-stdout'], source, timeout=120, cancel=cancel, limit=formats.MAX_FILE, env=_environment())
            except ExportError as exc:
                raise ExportError(f'The confined DOCX converter stopped ({exc}).') from None
            if code or not out.startswith(b'PK'):
                raise ExportError('The confined DOCX converter did not produce a document.')
            return out, {}
        return self._native_pdf(resolved, options, folder, cancel), {}

    def _native_pdf(self, resolved, options, folder, cancel):
        """WKWebView createPDF in the native shell; Python never writes a substitute PDF."""
        if not self.native_pdf or self.emit is None:
            raise _Unavailable('PDF needs Pebble\'s native export view, which is not available in this build. Nothing was written.')
        if not self._pdf.acquire(blocking=False):
            raise ExportError('Another PDF export is in progress. Try again when it finishes.')
        try:
            page = folder / 'print.html'
            _write_new(page, formats.html_document(resolved['title'], resolved['blocks'], 'pdf', options['paper'], self._footer(resolved)).encode('utf-8'))
            token, done = secrets.token_urlsafe(32), threading.Event()
            entry = {'chat_id': resolved['chat_id'], 'token': token, 'done': done, 'result': None, 'path': folder / 'native.pdf'}
            with self.lock:
                self.native[resolved['export_id']] = entry
            width, height = formats.PAPER[options['paper']]
            self.emit('native_export_pdf', {'chat_id': resolved['chat_id'], 'export_id': resolved['export_id'], 'attach_token': token,
                                            'html_path': str(page), 'pdf_path': str(entry['path']),
                                            'page': {'width': width, 'height': height, 'margin': 45.35}})
            deadline = time.monotonic() + NATIVE_PDF_SECONDS
            while not done.wait(0.05):
                if cancel.is_set():
                    raise ExportCancelled()
                if time.monotonic() > deadline:
                    raise ExportError('The native PDF view did not finish in time.')
            if entry['result'] != 'attached':
                raise ExportError('The native PDF view failed: ' + str(entry['result'])[:200])
            data = _read_file(entry['path'])
            if hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise ExportError('The native PDF changed after it was written.')
            entry['path'].unlink()
            return data
        finally:
            with self.lock:
                self.native.pop(resolved.get('export_id'), None)
            self._pdf.release()

    def render(self, cid, plan_id, cancel=None):
        plan = self._take(cid, plan_id)
        cancel = cancel if cancel is not None else threading.Event()
        with self.lock:
            self.inflight[plan_id] = (cid, cancel)
        try:
            return self._render(cid, plan, cancel)
        finally:
            with self.lock:
                self.inflight.pop(plan_id, None)

    def _render(self, cid, plan, cancel):
        fmt, options, pinned = plan['format'], plan['options'], plan['source']
        resolved = self._resolve(cid, {'kind': pinned['kind'], 'id': pinned['id']}, options)
        if pinned['kind'] in ('artifact', 'goal_report') and resolved['revision'] != pinned['revision']:
            raise ExportError(f'This {"view" if pinned["kind"] == "artifact" else "goal"} changed to revision {resolved["revision"]}. Preview the export again.')
        if resolved['digest'] != pinned['digest']:
            raise ExportError('The source changed after the preview. Preview the export again.')
        export_id, now = uuid.uuid4().hex, time.time()
        resolved.update(chat_id=cid, export_id=export_id)
        extension = fmt
        receipt = {'export_id': export_id, 'plan_id': plan['plan_id'], 'chat_id': cid, 'format': fmt, 'status': 'rendering',
                   'title': resolved['title'], 'source': pinned, 'options': options, 'file': None, 'checks': [], 'failed_checks': [],
                   'readback': None, 'notes': resolved['notes'], 'reason': None, 'created': now, 'completed': None,
                   'published': False, 'authorization': 'none'}
        folder = self._folder(cid, export_id, create=True)
        self._save_record(receipt)
        target = folder / f'export.{extension}'
        try:
            data, extra = self._write(fmt, resolved, options, folder, cancel)
            if cancel.is_set():
                raise ExportCancelled()
            _write_new(target, data)
            receipt['file'] = {'name': target.name, 'suggested_name': _filename(resolved['title'], extension),
                               'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            expect = {**self._expect(fmt, resolved, options), **extra}
            result = readback(target, fmt, expect, cancel=cancel)
            if cancel.is_set():
                raise ExportCancelled()
            receipt['readback'] = result
            receipt['checks'] = checks(fmt, result, expect)
            receipt['failed_checks'] = [c['name'] for c in receipt['checks'] if not c['passed']]
            receipt['status'] = 'unverified' if receipt['failed_checks'] else 'verified'
        except _Unavailable as exc:
            receipt.update(status='unavailable', reason=str(exc), file=None)
        except ExportCancelled:
            receipt.update(status='cancelled', reason='Cancelled. The partial file was removed.', file=None, readback=None, checks=[], failed_checks=[])
        except (ExportError, OSError) as exc:
            receipt.update(status='failed', file=None, reason=str(exc) if isinstance(exc, ExportError) else 'Pebble could not write the export file.')
        except Exception:
            receipt.update(status='failed', file=None, reason='The export stopped unexpectedly. Nothing was saved.')
        if receipt['status'] not in ('verified', 'unverified'):
            shutil.rmtree(folder, ignore_errors=True)
        receipt['completed'] = time.time()
        self._save_record(receipt)
        return receipt

    # Native shell only (never in the webview's rpc allowlist)

    def native_ready(self, pdf=False):
        if type(pdf) is not bool:
            raise ExportError('Native export capabilities are booleans.')
        self.native_pdf = pdf
        return {'pdf': pdf}

    def _pending(self, cid, export_id, attach_token):
        with self.lock:
            entry = self.native.get(export_id) if isinstance(export_id, str) else None
        if not entry or entry['chat_id'] != cid or not isinstance(attach_token, str) or not secrets.compare_digest(entry['token'], attach_token):
            raise ExportError('No native PDF export is waiting for this file.')
        return entry

    def pdf_attach(self, cid, export_id, attach_token, sha256):
        entry = self._pending(cid, export_id, attach_token)
        if not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise ExportError('Attach the native PDF with its SHA-256.')
        entry.update(sha256=sha256, result='attached')
        entry['done'].set()
        return {'accepted': True}

    def pdf_failed(self, cid, export_id, attach_token, reason=''):
        entry = self._pending(cid, export_id, attach_token)
        entry['result'] = _clean(reason if isinstance(reason, str) else '')[:200] or 'no reason given'
        entry['done'].set()
        return {'accepted': True}

    def bind_destination(self, cid, export_id, path, replace=False):
        """Called by the native shell after its Save dialog; returns a single-use token."""
        receipt = self.get(cid, export_id)
        if receipt['status'] not in ('verified', 'unverified') or not receipt.get('file'):
            raise ExportError('Only a finished export can be saved.')
        if type(replace) is not bool:
            raise ExportError('Replacing a file is a yes-or-no choice from the Save dialog.')
        target = self._destination(path, receipt['format'], replace)
        token, now = secrets.token_urlsafe(32), time.time()
        with self.lock:
            for key in [k for k, t in self.tokens.items() if t['expires'] <= now]:
                self.tokens.pop(key)
            while len(self.tokens) >= MAX_TOKENS:
                self.tokens.pop(next(iter(self.tokens)))
            self.tokens[token] = {'chat_id': cid, 'export_id': export_id, 'path': str(target), 'replace': replace, 'expires': now + TOKEN_SECONDS}
        return {'destination_token': token, 'expires_at': now + TOKEN_SECONDS, 'name': target.name}

    # Phase 18: the same native Save-dialog token for a file Pebble writes itself (telemetry export). A purpose
    # token is never an export token (its chat_id is None, so save() refuses it) and never replaces a file.
    FILE_PURPOSES = {'telemetry': 'json'}

    def bind_file_destination(self, purpose, path, replace=False):
        if purpose not in self.FILE_PURPOSES:
            raise ExportError('Unknown destination purpose.')
        if replace is not False:
            raise ExportError('This file is never replaced; choose a new name. Nothing was overwritten.')
        target = self._destination(path, self.FILE_PURPOSES[purpose], False)
        token, now = secrets.token_urlsafe(32), time.time()
        with self.lock:
            for key in [k for k, t in self.tokens.items() if t['expires'] <= now]:
                self.tokens.pop(key)
            while len(self.tokens) >= MAX_TOKENS:
                self.tokens.pop(next(iter(self.tokens)))
            self.tokens[token] = {'chat_id': None, 'export_id': None, 'purpose': purpose, 'path': str(target),
                                  'replace': False, 'expires': now + TOKEN_SECONDS}
        return {'destination_token': token, 'expires_at': now + TOKEN_SECONDS, 'name': target.name}

    def claim_file_destination(self, purpose, token):
        """Single use: the bound path, re-validated; raises for an unknown, used, expired or other-purpose token."""
        with self.lock:
            entry = self.tokens.get(token) if isinstance(token, str) else None
            if entry and entry.get('purpose') == purpose:
                self.tokens.pop(token)
            else:
                entry = None
        if not entry or entry['expires'] <= time.time():
            raise ExportError('Choose where to save again with the Save dialog. Nothing was written.')
        return str(self._destination(entry['path'], self.FILE_PURPOSES[purpose], False))

    def _destination(self, path, extension, replace):
        if not isinstance(path, str) or not path.startswith('/') or '\0' in path or len(path) > 4096:
            raise ExportError('Choose where to save with the Save dialog.')
        target = Path(path)
        if not target.name or target.name.startswith('.') or target.suffix.lower() != '.' + extension:
            raise ExportError(f'Save the file with the .{extension} extension.')
        parent = target.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ExportError('The folder you chose does not exist.')
        home = self.home.resolve()
        if parent.resolve() == home or home in parent.resolve().parents:
            raise ExportError('Choose a folder outside Pebble\'s own data.')
        from .data_home import inside_protected
        if inside_protected(parent):  # Phase 22: ~/.pebble (settings.json, PEBBLE.md) and the old app-data folder
            raise ExportError('Choose a folder outside Pebble\'s own data.')
        # The same folder under another spelling (case-insensitive volume, the /System/Volumes/Data firmlink) is
        # refused by identity, not by its path string (X17 F8).
        home_info = home.stat()
        for folder in (parent.resolve(), *parent.resolve().parents):
            try:
                info = folder.stat()
            except OSError:
                continue
            if (info.st_dev, info.st_ino) == (home_info.st_dev, home_info.st_ino):
                raise ExportError('Choose a folder outside Pebble\'s own data.')
        if os.path.lexists(target):
            if not replace:
                raise ExportError('That file already exists; choose a new name. Nothing was overwritten.')
            if not stat.S_ISREG(os.lstat(target).st_mode):
                raise ExportError('Only a regular file can be replaced.')
        return target

    # Saving

    def save(self, cid, export_id, destination_token):
        with self.lock:
            entry = self.tokens.pop(destination_token, None) if isinstance(destination_token, str) else None
        if not entry or entry['chat_id'] != cid or entry['export_id'] != export_id or entry['expires'] <= time.time():
            raise ExportError('Choose where to save again with the Save dialog. Nothing was written.')
        receipt = self.get(cid, export_id)
        if receipt['status'] not in ('verified', 'unverified') or not receipt.get('file'):
            raise ExportError('Only a finished export can be saved.')
        target = self._destination(entry['path'], receipt['format'], entry['replace'])
        data = _read_file(self._folder(cid, export_id) / receipt['file']['name'])
        if hashlib.sha256(data).hexdigest() != receipt['file']['sha256']:
            raise ExportError('The exported file changed after it was checked. Export it again. Nothing was written.')
        save_id, now = uuid.uuid4().hex, time.time()
        with self.store.lock, self.store.db as db:
            db.execute('INSERT INTO export_saves VALUES(?,?,?,?,?,?,?,?,?)', (save_id, export_id, cid, 'writing', str(target), None, 0, now, now))
        try:
            _write_copy(target, data, entry['replace'])
            copy = hashlib.sha256(_read_file(target)).hexdigest()
        except (OSError, ExportError):
            self._settle_save(save_id, 'failed', None, False)
            raise ExportError('Pebble could not write that file. Check the folder and try again.') from None
        verified = receipt['status'] == 'verified' and copy == receipt['file']['sha256']
        self._settle_save(save_id, 'saved' if copy == receipt['file']['sha256'] else 'uncertain', copy, verified)
        return {'path': str(target), 'sha256': copy, 'verified': verified, 'published': False}

    def _settle_save(self, save_id, state, sha, verified):
        with self.store.lock, self.store.db as db:
            db.execute('UPDATE export_saves SET state=?,sha256=?,verified=?,updated=? WHERE id=?', (state, sha, int(verified), time.time(), save_id))

    # Retention

    def discard(self, cid, export_id):
        receipt = self.get(cid, export_id)
        receipt.pop('saves', None)
        shutil.rmtree(self._folder(cid, export_id), ignore_errors=True)
        receipt.update(status='discarded', file=None)
        self._save_record(receipt)
        return {'export_id': export_id, 'status': 'discarded'}

    def forget(self, cid):
        with self.lock:
            for key in [k for k, p in self.plans.items() if p['chat_id'] == cid]:
                self.plans.pop(key)
            for key in [k for k, t in self.tokens.items() if t['chat_id'] == cid]:
                self.tokens.pop(key)
        with self.store.lock, self.store.db as db:
            db.execute('DELETE FROM export_saves WHERE chat_id=?', (cid,))
            db.execute('DELETE FROM export_records WHERE chat_id=?', (cid,))
        folder = self.home / 'exports' / cid
        if _IDENT.fullmatch(cid) and folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder, ignore_errors=True)


class _Unavailable(Exception):
    pass


def _write_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _write_copy(target, data, replace):
    if not replace:
        try:
            _write_new(target, data)
        except FileExistsError:
            raise ExportError('That file already exists; choose a new name. Nothing was overwritten.') from None
        except OSError:
            if os.path.lexists(target) and stat.S_ISREG(os.lstat(target).st_mode):
                os.unlink(target)
            raise
        return
    # The Save dialog confirmed replacement: write beside it, then swap the entry.
    # os.replace never follows a link planted at the target.
    temporary = target.parent / f'.{target.name}.pebble-{secrets.token_hex(4)}.tmp'
    try:
        _write_new(temporary, data)
        if os.path.lexists(target) and not stat.S_ISREG(os.lstat(target).st_mode):
            raise ExportError('Only a regular file can be replaced.')
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
