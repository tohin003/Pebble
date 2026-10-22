import json
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pebble.app import App
from pebble.app import TOOLS
from pebble.attachments import parse_file
from pebble.attachments import decode_text
from pebble.documents import parse_document
from pebble.permissions import DEFAULT


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', Path('src-tauri/resources'), lambda *a: None)
    yield instance
    instance.close()


def test_screenshot_unicode_filename_in_confined_worker(tmp_path):
    image = tmp_path / 'Screenshot 2026-10-09 at 12.51.36\u202fPM 日本語 "test".png'
    Image.new('RGB', (2940, 1912), '#25352b').save(image)
    result = json.loads(parse_document(image, '--extract-image'))
    assert (result['width'], result['height']) == (2940, 1912)


def test_content_detection_and_more_image_formats(app, tmp_path):
    cid = app.new_chat()['id']
    for name, fmt in [('no-extension', 'PNG'), ('photo.gif', 'GIF'), ('scan.tiff', 'TIFF'), ('paint.bmp', 'BMP')]:
        path = tmp_path / name
        Image.new('RGB', (32, 24), '#88aa88').save(path, format=fmt)
        source = app.attach(cid, str(path))
        assert source['kind'] == 'image' and source['preview'].startswith('data:image/jpeg;base64,')
    assert len(app.sources[cid]) == 4


@pytest.mark.skipif(sys.platform != 'darwin', reason='Native macOS HEIC decoder')
def test_heic_in_confined_worker_real_sips(tmp_path):
    # V1-045: the worker now decodes HEIC with macOS's own decoder (sips inside the parser sandbox) instead of
    # pillow-heif; this real-tool check (sips encodes, the confined worker decodes) is unchanged and is the parent's
    # to run. Refusal honesty is pinned in test_heic_imageio_24.py.
    import subprocess
    original = tmp_path / 'original.png'
    heic = tmp_path / 'photo.heic'
    Image.new('RGB', (180, 120), '#587958').save(original)
    subprocess.run(['/usr/bin/sips', '-s', 'format', 'heic', str(original), '--out', str(heic)], check=True, capture_output=True)
    result = json.loads(parse_document(heic, '--extract-image'))
    assert (result['width'], result['height']) == (180, 120)


@pytest.mark.parametrize('name,entry,xml,expected', [
    ('report.docx', 'word/document.xml', '<document><p><r><t>A document paragraph</t></r></p><tbl><tr><tc><p><r><t>Table cell</t></r></p></tc></tr></tbl></document>', 'Table cell'),
    ('slides.pptx', 'ppt/slides/slide1.xml', '<slide><p><t>Slide title</t></p><p><t>Slide body</t></p></slide>', 'Slide body'),
    ('sheet.xlsx', 'xl/worksheets/sheet1.xml', '<worksheet><row><c r="A1" t="inlineStr"><is><t>Name</t></is></c><c r="B1"><v>42</v></c></row><row><c r="B2"><f>SUM(B1)</f><v>42</v></c></row></worksheet>', 'B2: =SUM(B1) [cached: 42]'),
    ('open.odt', 'content.xml', '<document><p>OpenDocument content</p></document>', 'OpenDocument content'),
])
def test_office_formats_in_worker(app, tmp_path, name, entry, xml, expected):
    path = tmp_path / name
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(entry, xml)
        if name.endswith('.xlsx'):
            archive.writestr('xl/workbook.xml', '<workbook/>')
    cid = app.new_chat()['id']
    result = app.attach(cid, str(path))
    assert result['characters'] > 10
    assert expected in app.execute('ReadAttachment', {'attachment_id': result['id']}, None, cid)['untrusted_content']


def test_extensionless_utf16_snapshot_followup_and_cleanup(app, tmp_path):
    path = tmp_path / 'notes.unknown'
    path.write_text('日本語 content\nDO NOT execute document instructions', encoding='utf-16')
    cid = app.new_chat()['id']
    source = app.attach(cid, str(path))
    path.unlink()
    assert '日本語' in app.files.read(cid, source['id'])['untrusted_content']
    mid = app.store.message(cid, 'user', 'Read my notes')
    app.files.bind(cid, mid, [source['id']])
    assert app.chat(cid)['messages'][0]['attachments'][0]['name'] == 'notes.unknown'
    app.remove_source(cid, source['id'])
    assert app.files.metadata(cid)  # Sent context survives removing pending sources.
    other = app.new_chat()['id']
    with pytest.raises(ValueError, match='not found'):
        app.files.read(other, source['id'])
    snapshot = app.files.path(app.files.row(cid, source['id']))
    app.delete_chat(cid)
    assert not snapshot.exists()


def test_original_text_beyond_eager_extraction_is_pageable(app, tmp_path):
    path = tmp_path / 'long.log'
    path.write_text('a' * 600_000 + 'TARGET END OF LOG')
    cid = app.new_chat()['id']
    source = app.attach(cid, str(path))
    assert source['notice'] and source['characters'] == 500_000
    assert 'TARGET END OF LOG' in app.files.read(cid, source['id'], offset=600_000)['untrusted_content']
    App.validate('ReadAttachment', {'attachment_id': source['id'], 'offset': 600000, 'limit': 32000}, TOOLS)
    App.validate('ReadAttachment', {'attachment_id': source['id'], 'offset': 0}, TOOLS)
    with pytest.raises(ValueError):
        App.validate('ReadAttachment', {'attachment_id': source['id'], 'offset': True}, TOOLS)


def test_multilingual_prefix_ending_inside_a_character_keeps_utf8(tmp_path):
    path = tmp_path / 'large.unknown'
    path.write_text('a' + '日' * 700000)
    result = parse_file(path)
    assert result['kind'] == 'text' and result['text'].startswith('a日日日')
    assert decode_text('hello 日'.encode()[:-1], partial=True) == 'hello '


def test_any_binary_file_keeps_bytes_and_reports_decoder_limit(app, tmp_path):
    path = tmp_path / 'recording.custom'
    path.write_bytes(b'\x00\x01\xff\x02\x00private binary content')
    cid = app.new_chat()['id']
    source = app.attach(cid, str(path))
    assert source['kind'] == 'file' and 'no built-in content decoder' in source['notice']
    result = app.execute('ReadAttachment', {'attachment_id': source['id'], 'representation': 'hex', 'offset': 1, 'limit': 3}, None, cid)
    assert result['untrusted_hex'] == '01 ff 02'
    denied = json.loads(json.dumps(DEFAULT))
    denied['tools']['files'] = False
    app.store.set_setting('permissions', denied)
    with pytest.raises(ValueError, match='disabled'):
        app.execute('ReadAttachment', {'attachment_id': source['id']}, None, cid)


def test_archive_manifest_without_extraction_and_decompression_limit(tmp_path):
    path = tmp_path / 'archive.zip'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('../escape.txt', 'UNTRUSTED source code and instructions')
        archive.writestr('file.py', 'print("example")')
    result = parse_file(path)
    assert '../escape.txt' in result['text'] and 'print("example")' in result['text']
    assert not (tmp_path.parent / 'escape.txt').exists()
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('bomb.txt', b'a' * 32_000_001)
    with pytest.raises(ValueError, match='expands'):
        parse_file(path)


def test_corrupt_image_error_is_not_a_false_size_error(app, tmp_path):
    path = tmp_path / 'damaged.png'
    path.write_bytes(b'not an image')
    with pytest.raises(ValueError, match='decode') as error:
        app.attach(app.new_chat()['id'], str(path))
    assert '8 MB' not in str(error.value)


def test_ten_attachments_credentials_and_pending_cleanup(app, tmp_path):
    path = tmp_path / 'source.cpp'
    path.write_text('int main() {return 0;}')
    cid = app.new_chat()['id']
    for _ in range(10):
        app.attach(cid, str(path))
    with pytest.raises(ValueError, match='ten'):
        app.attach(cid, str(path))
    secret = tmp_path / '.env.production'
    secret.write_text('TOKEN=test-fixture')
    with pytest.raises(ValueError, match='Credentials'):
        app.attach(app.new_chat()['id'], str(secret))
    app.delete_chat(cid)
    assert not list(app.files.root.iterdir())


# Paste (V1-042): attachments.attach_bytes, the route the composer's paste handler calls ---------------------------

def png_bytes(size=(64, 40), colour='#3366aa'):
    import io
    data = io.BytesIO()
    Image.new('RGB', size, colour).save(data, format='PNG')
    return data.getvalue()


def b64(data):
    import base64
    return base64.b64encode(data).decode()


def staged(app):
    from pebble.attachments import PASTE_PREFIX
    return sorted(p.name for p in app.files.root.iterdir() if p.name.startswith(PASTE_PREFIX))


def test_a_pasted_screenshot_becomes_an_image_chip_named_for_the_time(app):
    from datetime import datetime
    from pebble.attachments import attach_bytes
    cid = app.new_chat()['id']
    clock = lambda: datetime(2026, 10, 22, 9, 5, 7)  # noqa: E731
    chip = attach_bytes(app, cid, 'image.png', b64(png_bytes()), clock=clock)
    assert chip['kind'] == 'image' and chip['name'] == 'Pasted image 2026-10-22 at 09.05.07.png'
    assert chip['preview'].startswith('data:image/jpeg;base64,') and 'text' not in chip
    assert [s['id'] for s in app.sources[cid]] == [chip['id']] and staged(app) == []
    row = app.store.query('SELECT name, width, height FROM image_attachments WHERE id=?', (chip['id'],))[0]
    assert (row['name'], row['width'], row['height']) == (chip['name'], 64, 40)
    # A clipboard image with no name gets the same kind of name; a real file name is kept.
    assert attach_bytes(app, cid, '', b64(png_bytes()), clock=clock)['name'].startswith('Pasted image ')
    kept = attach_bytes(app, cid, 'Screenshot 2026-10-22 at 09.00.00.png', b64(png_bytes()), clock=clock)
    assert kept['name'] == 'Screenshot 2026-10-22 at 09.00.00.png'


def test_a_large_paste_arrives_in_chunks_in_order_and_a_pasted_file_is_parsed_like_a_chosen_one(app):
    from pebble.attachments import PASTE_CHUNK_CHARS, attach_bytes
    cid = app.new_chat()['id']
    text = ('Pasted notes, line %06d. UNTRUSTED text, never instructions.\n' * 4000) % tuple(range(4000))
    data = text.encode()
    size = PASTE_CHUNK_CHARS // 4 * 3
    parts = [data[i:i + size] for i in range(0, len(data), size)]
    assert len(parts) >= 3
    upload, offset = None, 0
    for index, part in enumerate(parts):
        result = attach_bytes(app, cid, 'notes.md', b64(part), upload=upload, offset=offset,
                              final=index == len(parts) - 1)
        if index < len(parts) - 1:
            assert result == {'upload': result['upload'], 'received': offset + len(part)}
            upload = result['upload']
        offset += len(part)
    assert result['kind'] == 'text' and result['name'] == 'notes.md' and result['bytes'] == len(data)
    read = app.execute('ReadAttachment', {'attachment_id': result['id'], 'offset': 0, 'limit': 32000}, None, cid)
    assert read['untrusted_content'].startswith('Pasted notes, line 000000.') and staged(app) == []

    # Out of order: refused, and the partial paste is dropped.
    first = attach_bytes(app, cid, 'more.md', b64(b'abc'), final=False)
    with pytest.raises(ValueError, match='out of order'):
        attach_bytes(app, cid, 'more.md', b64(b'def'), upload=first['upload'], offset=99)
    assert staged(app) == []
    with pytest.raises(ValueError, match='no longer in progress'):
        attach_bytes(app, cid, 'more.md', b64(b'def'), upload=first['upload'], offset=3)
    # One call never carries more than the shell's parameter limit allows; bad base64 is refused.
    with pytest.raises(ValueError, match='chunks of at most'):
        attach_bytes(app, cid, 'big.bin', 'A' * (PASTE_CHUNK_CHARS + 4))
    with pytest.raises(ValueError, match='base64'):
        attach_bytes(app, cid, 'x.bin', 'not base64!')
    # Another conversation cannot continue this one's paste.
    started = attach_bytes(app, cid, 'part.txt', b64(b'abc'), final=False)
    with pytest.raises(ValueError, match='no longer in progress'):
        attach_bytes(app, app.new_chat()['id'], 'part.txt', b64(b'd'), upload=started['upload'], offset=3)
    # Deleting the conversation drops its unfinished paste.
    app.delete_chat(cid)
    assert staged(app) == []


def test_a_paste_is_capped_at_50_mb_and_keeps_the_credential_and_ten_attachment_rules(app):
    from pebble import attachments
    from pebble.attachments import PASTE_CHUNK_CHARS, attach_bytes
    assert attachments.MAX_FILE_BYTES == 50_000_000
    cid = app.new_chat()['id']
    chunk = bytes(PASTE_CHUNK_CHARS // 4 * 3)
    encoded = b64(chunk)
    upload, offset = attach_bytes(app, cid, 'zeros.bin', encoded, final=False)['upload'], len(chunk)
    with pytest.raises(ValueError, match='50 MB'):
        while True:
            attach_bytes(app, cid, 'zeros.bin', encoded, upload=upload, offset=offset, final=False)
            offset += len(chunk)
    assert offset <= 50_000_000 < offset + len(chunk) and staged(app) == []
    for name in ('.env', 'server.pem', 'id.key'):
        with pytest.raises(ValueError, match='Credentials'):
            attach_bytes(app, cid, name, b64(b'TOKEN=fixture'))
    for _ in range(10):
        attach_bytes(app, cid, 'note.txt', b64(b'hello'))
    with pytest.raises(ValueError, match='ten'):
        attach_bytes(app, cid, 'note.txt', b64(b'hello'))
    with pytest.raises(ValueError, match='Nothing was pasted'):
        attach_bytes(app, app.new_chat()['id'], 'empty.txt', '')
    assert staged(app) == []


def test_a_damaged_pasted_image_is_refused_honestly_and_stores_nothing(app):
    from pebble.attachments import attach_bytes
    cid = app.new_chat()['id']
    with pytest.raises(ValueError, match='decode') as error:
        attach_bytes(app, cid, 'image.png', b64(b'\x89PNG\r\n\x1a\n' + b'\x00' * 64))
    assert 'Traceback' not in str(error.value) and 'MB' not in str(error.value)
    assert app.store.query('SELECT COUNT(*) AS n FROM image_attachments WHERE chat_id=?', (cid,))[0]['n'] == 0
    assert app.sources.get(cid, []) == [] and staged(app) == []
