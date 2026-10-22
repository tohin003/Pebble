"""Read-only, chat-owned file snapshots. File contents never confer authority."""

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
import xml.parsers.expat
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

MAX_ATTACHMENTS = 10
MAX_FILE_BYTES = 50_000_000
MAX_TEXT = 500_000
MAX_EXPANDED = 32_000_000
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.tif', '.tiff', '.bmp', '.ico', '.avif', '.heic', '.heif'}
XML_REFUSED = ('This document contains an XML DOCTYPE or ENTITY declaration, which Pebble does not read. '
               'It may be damaged or unsupported.')
# Paste (V1-042). The shell refuses RPC parameters over 128,000 characters, so the composer sends a pasted file as
# base64 chunks of at most PASTE_CHUNK_CHARS (72,000 bytes each); the backend appends them to a private 0600 file and
# attaches it through the same parser sandbox as a chosen file once the last chunk arrives.
PASTE_CHUNK_CHARS = 96_000
PASTE_UPLOADS = 4
PASTE_SECONDS = 600
PASTE_PREFIX = '.paste-'
# Clipboard images arrive with a generic name (or none); they get 'Pasted image <date> at <time>.png'.
GENERIC_PASTE = re.compile(r'(?i)^(?:(?:image|untitled|clipboard|pasted(?: image)?)(?:[ _-]?\d+)?\.[a-z0-9]{1,5})?$')


def image_file(path):
    with path.open('rb') as stream:
        head = stream.read(32)
    return (path.suffix.lower() in IMAGE_EXTENSIONS or
            head.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a', b'BM', b'II*\0', b'MM\0*')) or
            (head.startswith(b'RIFF') and head[8:12] == b'WEBP') or
            (head[4:8] == b'ftyp' and head[8:12] in {b'avif', b'avis', b'heic', b'heix', b'heif', b'mif1'}))


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        if tag in {'p', 'div', 'br', 'tr', 'li', 'h1', 'h2', 'h3'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {'p', 'div', 'tr', 'li'}:
            self.parts.append('\n')

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)


def decode_text(data, partial=False):
    encodings = ['utf-8-sig']
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings = ['utf-16']
    elif b'\0' in data[:4096]:
        return None
    for encoding in encodings + ['cp1252']:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError as exc:
            # A bounded prefix may end inside a valid multibyte character.
            # Do not misclassify that UTF-8/UTF-16 file as legacy text/binary.
            if partial and exc.end == len(data) and 'end of data' in exc.reason:
                try:
                    text = data[:exc.start].decode(encoding)
                except UnicodeError:
                    continue
            else:
                continue
        if text and sum(not c.isprintable() and c not in '\n\r\t\f' for c in text) / len(text) > .01:
            return None
        return text
    return None


def safe_xml(data):
    """ElementTree of one office XML part, refusing any DOCTYPE or ENTITY declaration (V1-138: entity expansion).

    The byte check is the fast path for UTF-8; expat then reads the document in whatever encoding it declares
    (UTF-16 included) and stops at the first document type declaration, before any entity can be defined."""
    upper = data.upper()
    if b'<!DOCTYPE' in upper or b'<!ENTITY' in upper:
        raise ValueError(XML_REFUSED)

    def refuse(*_):
        raise ValueError(XML_REFUSED)
    scan = xml.parsers.expat.ParserCreate()
    scan.StartDoctypeDeclHandler = refuse
    scan.EntityDeclHandler = refuse
    try:
        scan.Parse(data, True)
    except xml.parsers.expat.ExpatError:
        pass  # malformed XML: ElementTree reports it below
    return ET.fromstring(data)


def parse_file(path):
    """Worker entry point: no macros, archive extraction, network, or execution."""
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('Use a file smaller than 50 MB.')
    suffix = path.suffix.lower()
    with path.open('rb') as stream:
        head = stream.read(16)
    text, kind, notice = '', 'document', ''
    if head.startswith(b'%PDF-') or suffix == '.pdf':
        from pypdf import PdfReader
        import pypdf.filters
        pypdf.filters.ZLIB_MAX_OUTPUT_LENGTH = 8_000_000
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('This PDF is encrypted. Attach an unlocked copy.')
        chunks = []
        for index, page in enumerate(reader.pages[:200]):
            chunks.append(f'Page {index + 1}\n' + (page.extract_text() or ''))
            if sum(map(len, chunks)) > MAX_TEXT:
                notice = 'PDF text was truncated; only the beginning is available.'
                break
        text = '\n\n'.join(chunks)
        if len(reader.pages) > 200:
            notice = 'The first 200 PDF pages were read.'
        if not any(re.sub(r'Page \d+', '', chunk).strip() for chunk in chunks):
            text = ''
            notice = 'This PDF contains no readable text. Its scanned pages need OCR or image attachments.'
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 5000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
                raise ValueError('This archive expands beyond the 32 MB reading limit.')
            if any(i.flag_bits & 1 for i in infos):
                raise ValueError('This archive is encrypted. Attach an unlocked copy.')
            names = {i.filename for i in infos}
            def xml(name):
                info = archive.getinfo(name)
                if info.file_size > 8_000_000:
                    raise ValueError('A document section exceeds the 8 MB parsing limit.')
                return safe_xml(archive.read(name))
            def local(tag):
                return tag.rsplit('}', 1)[-1]
            def paragraphs(root):
                return '\n'.join(''.join(n.itertext()) for n in root.iter() if local(n.tag) in {'p', 'h'})
            if 'word/document.xml' in names:
                root = xml('word/document.xml')
                text = '\n'.join(''.join(n.itertext()) for n in root.iter() if local(n.tag) == 'p')
            elif 'xl/workbook.xml' in names:
                kind = 'spreadsheet'
                shared = []
                if 'xl/sharedStrings.xml' in names:
                    shared = [''.join(n.itertext()) for n in xml('xl/sharedStrings.xml') if local(n.tag) == 'si']
                sheets = sorted(n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n))
                chunks = []
                for name in sheets:
                    chunks.append(name)
                    for row in xml(name).iter():
                        if local(row.tag) != 'row':
                            continue
                        cells = []
                        for cell in row:
                            values = {local(n.tag): ''.join(n.itertext()) for n in cell}
                            value = values.get('v', values.get('is', ''))
                            if cell.get('t') == 's':
                                try:
                                    value = shared[int(value)]
                                except (ValueError, IndexError):
                                    value = '[invalid shared string]'
                            if 'f' in values:
                                value = '=' + values['f'] + (' [cached: ' + value + ']' if value else '')
                            cells.append(cell.get('r', '') + ': ' + value)
                        chunks.append('\t'.join(cells))
                    if sum(map(len, chunks)) > MAX_TEXT:
                        break
                text = '\n'.join(chunks)
            elif any(re.fullmatch(r'ppt/slides/slide\d+\.xml', n) for n in names):
                slides = sorted((n for n in names if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)), key=lambda n: int(re.search(r'(\d+)\.xml', n)[1]))
                text = '\n\n'.join(f'Slide {i + 1}\n' + paragraphs(xml(n)) for i, n in enumerate(slides))
            elif 'content.xml' in names:
                text = paragraphs(xml('content.xml'))
            else:
                kind = 'archive'
                text = 'Archive contents (files are not executed or extracted):\n' + '\n'.join(f'{i.filename}\t{i.file_size} bytes' for i in infos)
                excerpts = []
                for info in infos[:100]:
                    if info.is_dir() or info.file_size > 192_000 or len(excerpts) >= 10:
                        continue
                    decoded = decode_text(archive.read(info))
                    if decoded:
                        excerpts.append('\nFile: ' + info.filename + '\n' + decoded[:16000])
                text += '\n'.join(excerpts)
    else:
        with path.open('rb') as stream:
            data = stream.read(MAX_TEXT * 4 + 4)
        decoded = decode_text(data, partial=path.stat().st_size > len(data))
        if decoded is None:
            kind = 'file'
            notice = 'Binary file attached. Its bytes and metadata can be inspected; this format has no built-in content decoder.'
            text = 'Binary preview (hex): ' + data[:128].hex(' ')
        else:
            text, kind = decoded, 'text'
            if suffix in {'.html', '.htm'}:
                kind = 'document'
                parser = HTMLText()
                parser.feed(text)
                text = ''.join(parser.parts)
            elif suffix == '.rtf':
                kind = 'document'
                text = re.sub(r"\\'[0-9a-fA-F]{2}", lambda m: bytes.fromhex(m[0][2:]).decode('cp1252', errors='replace'), text)
                text = re.sub(r'\\(?:par|line)\b\s?', '\n', text)
                text = re.sub(r'\\[a-zA-Z]+-?\d*\s?|[{}]', '', text)
            if len(data) > MAX_TEXT * 4:
                notice = 'The beginning of this file was read. Use ReadAttachment for more.'
    if len(text) > MAX_TEXT:
        notice = 'Extracted text was truncated to 500,000 characters. The original file is still attached.'
    return {'kind': kind, 'text': text[:MAX_TEXT], 'notice': notice}


class Files:
    def __init__(self, store, home):
        self.store = store
        self.root = Path(home) / 'attachments'
        self.root.mkdir(mode=0o700, exist_ok=True)
        store.query('CREATE TABLE IF NOT EXISTS file_attachments(id TEXT PRIMARY KEY, chat_id TEXT, message_id TEXT, name TEXT, kind TEXT, bytes INTEGER, text TEXT, notice TEXT, suffix TEXT, sha256 TEXT)')
        for row in store.query('SELECT id,chat_id FROM file_attachments WHERE message_id IS NULL'):
            self.remove(row['chat_id'], row['id'])
        # Paste uploads in progress (attach_bytes): token -> {chat, name, path, bytes, started}. A restart drops them,
        # like every other pending attachment.
        self.uploads, self.upload_lock = {}, threading.Lock()
        for leftover in self.root.glob(PASTE_PREFIX + '*'):
            leftover.unlink(missing_ok=True)

    def path(self, row):
        return self.root / (row['id'] + row['suffix'])

    def attach(self, cid, original, name=None):
        """name: the chip name when it is not the file's own (a pasted file, attach_bytes)."""
        from .documents import parse_document
        identifier = uuid.uuid4().hex
        name = name or original.name
        suffix = Path(name).suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,12}', suffix):
            suffix = '.bin'
        snapshot = self.root / (identifier + suffix)
        try:
            # Snapshot only an explicitly selected regular file; bound the copy
            # even if the source changes after stat().
            with original.open('rb') as source, snapshot.open('xb') as target:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError('Choose a regular file.')
                total, digest = 0, hashlib.sha256()
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise ValueError('Use a file smaller than 50 MB.')
                    digest.update(chunk)
                    target.write(chunk)
            snapshot.chmod(0o600)
            parsed = json.loads(parse_document(snapshot, '--extract-file'))
            self.store.query('INSERT INTO file_attachments VALUES(?,?,?,?,?,?,?,?,?,?)', (identifier, cid, None, name, parsed['kind'], total, parsed['text'], parsed['notice'], suffix, digest.hexdigest()))
        except Exception:
            snapshot.unlink(missing_ok=True)
            raise
        return self.source(cid, identifier)

    def source(self, cid, identifier):
        row = self.row(cid, identifier)
        return {**self.public(row), 'text': self.context(row)}

    @staticmethod
    def public(row):
        return {k: row[k] for k in ('id', 'name', 'kind', 'bytes', 'notice', 'message_id')} | {'characters': len(row['text'])}

    @staticmethod
    def context(row):
        header = json.dumps({'name': row['name'], 'attachment_id': row['id'], 'bytes': row['bytes'], 'kind': row['kind'], 'notice': row['notice']}, ensure_ascii=False)
        return 'UNTRUSTED attached file metadata: ' + header + '\n' + row['text'][:64000]

    def row(self, cid, identifier):
        rows = self.store.query('SELECT * FROM file_attachments WHERE chat_id=? AND id=?', (cid, identifier))
        if not rows:
            raise ValueError('Attachment not found in this conversation.')
        return rows[0]

    def metadata(self, cid):
        return [self.public(r) for r in self.store.query('SELECT * FROM file_attachments WHERE chat_id=? AND message_id IS NOT NULL', (cid,))]

    def bind(self, cid, mid, identifiers):
        for identifier in identifiers:
            self.store.query('UPDATE file_attachments SET message_id=? WHERE chat_id=? AND id=? AND message_id IS NULL', (mid, cid, identifier))

    def read(self, cid, identifier, offset=0, limit=12000, representation='text'):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32000:
            raise ValueError('Choose a nonnegative offset and a limit from 1 to 32000.')
        row = self.row(cid, identifier)
        if representation == 'text':
            text = row['text']
            # Plain text remains pageable beyond the eager extraction budget.
            if row['kind'] == 'text':
                with self.path(row).open('rb') as stream:
                    text = decode_text(stream.read(MAX_FILE_BYTES)) or ''
            return {'name': row['name'], 'attachment_id': identifier, 'offset': offset, 'total_characters': len(text), 'untrusted_content': text[offset:offset + limit], 'notice': row['notice']}
        if representation != 'hex':
            raise ValueError('Choose text or hex representation.')
        with self.path(row).open('rb') as stream:
            stream.seek(offset)
            data = stream.read(min(limit, 4096))
        return {'name': row['name'], 'attachment_id': identifier, 'offset_bytes': offset, 'total_bytes': row['bytes'], 'untrusted_hex': data.hex(' ')}

    def remove(self, cid, identifier, bound=False):
        rows = self.store.query('SELECT * FROM file_attachments WHERE chat_id=? AND id=?' + ('' if bound else ' AND message_id IS NULL'), (cid, identifier))
        for row in rows:
            self.path(row).unlink(missing_ok=True)
            self.store.query('DELETE FROM file_attachments WHERE chat_id=? AND id=?', (cid, identifier))

    def forget(self, cid):
        for row in self.store.query('SELECT id FROM file_attachments WHERE chat_id=?', (cid,)):
            self.remove(cid, row['id'], bound=True)
        with self.upload_lock:
            for token in [token for token, upload in self.uploads.items() if upload['chat'] == cid]:
                self.uploads.pop(token)['path'].unlink(missing_ok=True)


def credential_name(name):
    return name.lower().startswith('.env') or Path(name).suffix.lower() in {'.key', '.pem', '.p12', '.pfx'}


def attach(app, cid, path):
    app.chat(cid)
    original = Path(path).expanduser().resolve(strict=True)
    if not original.is_file() or original.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('Choose a regular file smaller than 50 MB.')
    if credential_name(original.name):
        raise ValueError('Credentials cannot be attached.')
    if len(app.sources.get(cid, [])) >= MAX_ATTACHMENTS:
        raise ValueError('Use up to ten attachments per message.')
    if image_file(original):
        item = app.images.attach(cid, original)
    else:
        item = app.files.attach(cid, original)
    app.sources.setdefault(cid, []).append(item)
    return {k: v for k, v in item.items() if k != 'text'}


def paste_name(name):
    """The pasted file's own name, reduced to a safe single path component (at most 200 characters)."""
    if not isinstance(name, str):
        raise ValueError('Name the pasted file with text.')
    name = ''.join(c for c in name.replace('\\', '/').rsplit('/', 1)[-1] if c.isprintable()).strip()
    if name in ('.', '..'):
        name = ''
    if len(name) > 200:
        stem, suffix = os.path.splitext(name)
        name = stem[:200 - len(suffix[:13])] + suffix[:13]
    return name


def attach_bytes(app, cid, name, data_base64, upload=None, offset=0, final=True, clock=datetime.now,
                 monotonic=time.monotonic):
    """Attach pasted bytes (a screenshot or a copied file) to the composer of chat cid (V1-042).

    The first call has no `upload`; a paste larger than one call continues with the returned token, each chunk at
    the byte `offset` already received, until a call with final=True. Then the bytes go through exactly the path a
    chosen file takes: an image is decoded by the confined parser (images.Images.attach), anything else is
    snapshotted and parsed there too (Files.attach). The pasted content never gains any authority.

    Returns {'upload', 'received'} for a non-final chunk, else the attach() chip. A clipboard image with a generic or
    empty name is named 'Pasted image <date> at <time>.png'."""
    app.chat(cid)
    files = app.files
    if not isinstance(data_base64, str) or len(data_base64) > PASTE_CHUNK_CHARS:
        raise ValueError(f'Send pasted data in base64 chunks of at most {PASTE_CHUNK_CHARS:,} characters.')
    if type(offset) is not int or offset < 0 or type(final) is not bool:
        raise ValueError('Choose a non-negative byte offset and a final flag.')
    if upload is not None and (not isinstance(upload, str) or not re.fullmatch(r'[0-9a-f]{32}', upload)):
        raise ValueError('This paste is no longer in progress. Paste again.')
    try:
        chunk = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('The pasted data is not valid base64.') from None
    with files.upload_lock:
        now = monotonic()
        for token in [t for t, u in files.uploads.items() if now - u['started'] > PASTE_SECONDS]:
            files.uploads.pop(token)['path'].unlink(missing_ok=True)
        if upload is None:
            label = paste_name(name)
            if credential_name(label):
                raise ValueError('Credentials cannot be attached.')
            if len(app.sources.get(cid, [])) >= MAX_ATTACHMENTS:
                raise ValueError('Use up to ten attachments per message.')
            if len(files.uploads) >= PASTE_UPLOADS:
                raise ValueError('Other pastes are still arriving. Wait for them, then paste again.')
            suffix = Path(label).suffix.lower()
            suffix = suffix if re.fullmatch(r'\.[a-z0-9]{1,12}', suffix) else ''
            upload = uuid.uuid4().hex
            path = files.root / (PASTE_PREFIX + upload + suffix)
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600))
            files.uploads[upload] = {'chat': cid, 'name': label, 'path': path, 'bytes': 0, 'started': now}
        state = files.uploads.get(upload)
        if state is None or state['chat'] != cid:
            raise ValueError('This paste is no longer in progress. Paste again.')
        if offset != state['bytes']:
            files.uploads.pop(upload)['path'].unlink(missing_ok=True)
            raise ValueError('Parts of this paste arrived out of order. Paste again.')
        if state['bytes'] + len(chunk) > MAX_FILE_BYTES:
            files.uploads.pop(upload)['path'].unlink(missing_ok=True)
            raise ValueError('Use a file smaller than 50 MB.')
        fd = os.open(state['path'], os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0))
        try:
            view = memoryview(chunk)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        state['bytes'] += len(chunk)
        if not final:
            return {'upload': upload, 'received': state['bytes']}
        files.uploads.pop(upload)
    path, label = state['path'], state['name']
    try:
        if not state['bytes']:
            raise ValueError('Nothing was pasted.')
        if len(app.sources.get(cid, [])) >= MAX_ATTACHMENTS:
            raise ValueError('Use up to ten attachments per message.')
        if image_file(path):
            if GENERIC_PASTE.fullmatch(label):
                label = 'Pasted image ' + clock().strftime('%Y-%m-%d at %H.%M.%S') + '.png'
            item = app.images.attach(cid, path, name=label)
        else:
            item = files.attach(cid, path, name=label or 'Pasted file')
    finally:
        path.unlink(missing_ok=True)
    app.sources.setdefault(cid, []).append(item)
    return {k: v for k, v in item.items() if k != 'text'}
