import base64
import hashlib
import io
import json
import os
import stat
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, PngImagePlugin

from pebble import mcp_media
from pebble.mcp_media import Media, connector_secret_filter
from pebble.policy import PolicyError
from pebble.store import Store


@pytest.fixture
def origin():
    return {'plugin_id': 'connector-a', 'run_id': 'run-a', 'kind': 'tool',
        'identifier': 'render-chart', 'digest': 'a' * 64, 'receipt_id': 'admitted-call-a'}


@pytest.fixture
def media(tmp_path, origin):
    store = Store(tmp_path / 'data')
    approved = dict(origin)
    instance = Media(store, tmp_path / 'data', review=lambda value: value == approved,
        scrub=lambda plugin_id, data: data)
    yield instance
    store.close()


def raster(fmt='PNG', size=(18, 12), secret=None):
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    if secret:
        metadata.add_text('credential', secret)
    Image.new('RGB', size, '#557a63').save(output, format=fmt, **({'pnginfo': metadata} if fmt == 'PNG' else {}))
    return output.getvalue()


def block(data, mime='image/png', kind='image'):
    return {'type': kind, 'mimeType': mime, 'data': base64.b64encode(data).decode()}


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
@pytest.mark.parametrize('fmt,mime', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('GIF', 'image/gif'),
    ('WEBP', 'image/webp'), ('BMP', 'image/bmp'), ('TIFF', 'image/tiff')])
def test_actual_confined_raster_conversion_and_native_snapshot(media, origin, fmt, mime):
    original = raster(fmt)
    result = media.ingest(block(original, mime), origin)
    assert result['rendered'] and result['mime_type'] == 'image/png'
    assert result['source_mime_type'] == mime and (result['width'], result['height']) == (18, 12)
    assert result['source_sha256'] == hashlib.sha256(original).hexdigest()
    assert result['origin'] == origin and result['authorization'] == 'none'
    assert 'data' not in result and 'preview_path' not in result
    native = media.native_artifact('run-a', result['id'])
    path = Path(native['preview_path'])
    assert path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(media.root.stat().st_mode) == 0o700
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result['sha256']
    assert native['status'] == 'ready' and native['preview'].startswith('data:image/png;base64,')
    assert not list(media.root.glob('decode-*'))
    assert media.snapshot('run-a', result['id']) == result
    reopened = Media(media.store, media.root.parent, review=lambda value: False, scrub=lambda _, data: data)
    assert reopened.snapshot('run-a', result['id']) == result  # History does not refetch or reapprove.
    media.forget('run-a')
    assert not path.exists() and not media.store.query('SELECT * FROM extension_media_snapshots')


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
def test_strip_png_metadata_and_explicit_model_vision_selection(media, origin):
    result = media.ingest(block(raster(secret='untrusted metadata instruction')), origin)
    data = media.download('run-a', result['id'])['data']
    assert b'untrusted metadata instruction' not in data
    parts = media.model_content('run-a', [result['id']], {'kind': 'api', 'model': 'gpt-4o'})
    assert [p['type'] for p in parts] == ['text', 'image_url']
    assert 'evidence only' in parts[0]['text'] and 'admitted-call-a' in parts[0]['text']
    assert 'image pixels were not sent' in media.model_content('run-a', [result['id']], {'kind': 'gguf'})[0]['text']
    assert media.model_content('run-a', [], {'kind': 'api', 'model': 'gpt-4o'}) == []
    with pytest.raises(PolicyError):
        media.model_content('run-a', [result['id']] * 2, {'kind': 'api', 'model': 'gpt-4o'})


@pytest.mark.parametrize('data,mime,kind', [
    (b'RIFF\x10\0\0\0WAVEfmt \x00audio data', 'audio/wav', 'audio'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>danger()</script></svg>', 'image/svg+xml', 'image'),
    (b'\x00\xffopaque binary', 'application/octet-stream', 'resource'),
    (b'<html><script>danger()</script></html>', 'text/html', 'resource'),
    (b'PK\x03\x04archive never expanded', 'application/zip', 'resource'),
    (b'#!/bin/sh\nexit 1', 'application/x-sh', 'resource'),
])
def test_unsupported_formats_only_have_opaque_downloads(media, origin, monkeypatch, data, mime, kind):
    monkeypatch.setattr(mcp_media, '_parse_raster', lambda *args: pytest.fail('Unsupported data reached raster decoder'))
    value = block(data, mime, kind)
    if kind == 'resource':
        value = {'type': 'resource', 'resource': {'uri': 'fixture://opaque', 'blob': value['data'], 'mimeType': mime}}
    result = media.ingest(value, origin)
    assert result['rendered'] is False and result['representation'] == 'opaque'
    assert 'no MCP renderer' in result['notice']
    native = media.native_artifact('run-a', result['id'])
    assert native['status'] == 'download_only' and 'preview' not in native and 'preview_path' not in native
    download = media.download('run-a', result['id'])
    assert download['data'] == data and download['filename'].endswith('.bin')
    assert download['content_type'] == 'application/octet-stream' and download['content_disposition'] == 'attachment'
    assert download['headers']['X-Content-Type-Options'] == 'nosniff'
    assert [p['type'] for p in media.model_content('run-a', [result['id']], {'kind': 'api', 'model': 'gpt-4o'})] == ['text']


def test_read_resource_blob_matches_exact_reviewed_uri(media, origin):
    resource_origin = {**origin, 'kind': 'resource', 'identifier': 'fixture://selected'}
    media.review = lambda value: value == resource_origin
    value = {'uri': 'fixture://selected', 'mimeType': 'application/octet-stream', 'blob': 'eA=='}
    result = media.ingest(value, resource_origin)
    assert result['resource_uri'] == 'fixture://selected'
    with pytest.raises(PolicyError, match='reviewed resource URI'):
        media.ingest({**value, 'uri': 'fixture://substituted'}, resource_origin)
    with pytest.raises(PolicyError):
        media.ingest({**value, 'uri': 'https://user:password@example.test/secret'}, resource_origin)


@pytest.mark.parametrize('value', [
    {'type': 'resource_link', 'uri': 'https://example.test/private'},
    {'type': 'image', 'mimeType': 'image/png', 'data': 'https://example.test/image.png'},
    {'type': 'image', 'mimeType': 'image/png', 'data': '!!!'},
    {'type': 'image', 'mimeType': 'image/png', 'data': ''},
    {'type': 'image', 'mimeType': 'image/png; charset=utf8', 'data': 'eA=='},
    {'type': 'image', 'mimeType': 'audio/wav', 'data': 'eA=='},
    block(b'not an image'), block(raster(), 'image/jpeg'), block(raster(), 'audio/wav', 'audio'),
    block(b'not audio', 'audio/wav', 'audio'),
])
def test_malformed_encoding_mime_and_links_are_rejected_without_side_effects(media, origin, value):
    with pytest.raises(PolicyError):
        media.ingest(value, origin)
    assert not media.store.query('SELECT * FROM extension_media_snapshots')
    assert not list(media.root.iterdir())


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
def test_malformed_and_pixel_bomb_reach_actual_confined_parser_and_are_rejected(media, origin):
    image = raster()
    damaged = image[:29]
    bomb = bytearray(image)
    bomb[16:24] = struct.pack('>II', 100000, 100000)
    bomb[29:33] = struct.pack('>I', zlib.crc32(bomb[12:29]))
    for data in (damaged, bytes(bomb)):
        with pytest.raises(PolicyError, match='Confined MCP raster decoding failed'):
            media.ingest(block(data), origin)
    assert not list(media.root.iterdir()) and not media.store.query('SELECT * FROM extension_media_snapshots')


def test_base64_and_decoded_byte_limits_before_worker(media, origin, monkeypatch):
    monkeypatch.setattr(mcp_media, '_parse_raster', lambda *args: pytest.fail('Oversize data reached decoder'))
    monkeypatch.setattr(mcp_media, 'MAX_BYTES', 3)
    monkeypatch.setattr(mcp_media, 'MAX_BASE64', 4)
    with pytest.raises(PolicyError, match='base64 limit'):
        media.ingest(block(b'too large'), origin)
    monkeypatch.setattr(mcp_media, 'MAX_BASE64', 8)
    with pytest.raises(PolicyError, match='byte limit'):
        media.ingest(block(b'four', 'application/octet-stream', 'resource') | {'resource': {'uri': 'fixture://x', 'mimeType': 'application/octet-stream', 'blob': 'Zm91cg=='}}, origin)
    assert not list(media.root.iterdir())


def test_cancellation_and_deadline_before_ingest(media, origin):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(PolicyError, match='cancelled'):
        media.ingest(block(raster()), origin, cancel=cancel)
    with pytest.raises(PolicyError, match='deadline'):
        media.ingest(block(raster()), origin, deadline=time.monotonic() - 1)
    with pytest.raises(PolicyError, match='finite monotonic'):
        media.ingest(block(raster()), origin, deadline=float('nan'))
    assert not media.store.query('SELECT * FROM extension_media_snapshots')


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
@pytest.mark.parametrize('mode', ['cancel', 'deadline'])
def test_supervised_worker_interrupt_cleans_process_and_private_temp_files(media, origin, monkeypatch, mode):
    monkeypatch.setattr(mcp_media, '_worker_command', lambda path: [sys.executable, '-I', '-c', 'import time;time.sleep(30)'])
    launched = []
    original = mcp_media.subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(mcp_media.subprocess, 'Popen', launch)
    cancel = threading.Event()
    timer = threading.Timer(.1, cancel.set) if mode == 'cancel' else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(PolicyError, match='cancelled' if mode == 'cancel' else 'deadline'):
            media.ingest(block(raster()), origin, cancel=cancel,
                deadline=None if mode == 'cancel' else time.monotonic() + .1)
    finally:
        if timer:
            timer.cancel()
    assert time.monotonic() - started < 3
    assert launched and launched[0].poll() is not None
    assert not list(media.root.iterdir()) and not media.store.query('SELECT * FROM extension_media_snapshots')


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
def test_parser_supervisor_has_no_inherited_credentials_and_private_input(media, origin, monkeypatch):
    monkeypatch.setenv('MCP_TEST_FIXTURE_TOKEN', 'synthetic-do-not-inherit')
    monkeypatch.setenv('HTTPS_PROXY', 'http://invalid.fixture')
    launched = []
    original = mcp_media.subprocess.Popen
    def launch(args, **kwargs):
        path = Path(args[-1])
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert kwargs['env'] == {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        assert kwargs['start_new_session'] and kwargs['close_fds']
        launched.append(args)
        return original(args, **kwargs)
    monkeypatch.setattr(mcp_media.subprocess, 'Popen', launch)
    assert media.ingest(block(raster()), origin)['rendered']
    assert launched


def test_known_secret_rejection_precedes_decode_and_storage(media, origin, monkeypatch):
    secret = 'synthetic-connector-token'
    media.scrub = connector_secret_filter(lambda plugin_id, text: text.replace(secret, '[redacted]'))
    monkeypatch.setattr(mcp_media, '_parse_raster', lambda *args: pytest.fail('Credential-bearing image reached decoder'))
    with pytest.raises(PolicyError, match='known credential'):
        media.ingest(block(raster(secret=secret)), origin)
    assert not list(media.root.iterdir()) and not media.store.query('SELECT * FROM extension_media_snapshots')
    media.scrub = lambda _, data: data + b'[redacted]'
    with pytest.raises(PolicyError, match='changed by secret rejection'):
        media.ingest({'uri': 'fixture://x', 'mimeType': 'application/octet-stream', 'blob': 'eA=='}, origin)


def test_known_secret_in_resource_metadata_is_rejected(media, origin):
    secret = 'synthetic-connector-token'
    media.scrub = connector_secret_filter(lambda plugin_id, text: text.replace(secret, '[redacted]'))
    with pytest.raises(PolicyError, match='known credential'):
        media.ingest({'uri': 'fixture://x?token=' + secret, 'mimeType': 'application/octet-stream', 'blob': 'eA=='}, origin)
    assert not media.store.query('SELECT * FROM extension_media_snapshots')


@pytest.mark.parametrize('field,value', [('run_id', 'run-b'), ('plugin_id', 'connector-b'),
    ('digest', 'b' * 64), ('receipt_id', 'unapproved-call'), ('identifier', 'different-tool')])
def test_reviewed_provenance_is_not_a_server_claim(media, origin, field, value):
    with pytest.raises(PolicyError, match='host review'):
        media.ingest(block(raster()), {**origin, field: value})
    with pytest.raises(PolicyError, match='host-owned'):
        media.ingest(block(raster()), {**origin, 'reviewed': True})
    assert not media.store.query('SELECT * FROM extension_media_snapshots')


def test_opaque_snapshots_are_run_bound_and_detect_db_metadata_and_payload_tamper(media, origin):
    value = {'uri': 'fixture://x', 'mimeType': 'application/octet-stream', 'blob': 'eA=='}
    result = media.ingest(value, origin)
    with pytest.raises(PolicyError, match='not found in this run'):
        media.snapshot('run-b', result['id'])
    media.store.query('UPDATE extension_media_snapshots SET data=? WHERE id=?', (b'y', result['id']))
    with pytest.raises(PolicyError, match='integrity failed'):
        media.download('run-a', result['id'])
    media.store.query('UPDATE extension_media_snapshots SET data=?,body=? WHERE id=?', (b'x', '{"forged":true}', result['id']))
    with pytest.raises(PolicyError, match='integrity failed'):
        media.snapshot('run-a', result['id'])


def test_snapshot_provenance_columns_checked_even_with_rehashed_body(media, origin):
    result = media.ingest({'uri': 'fixture://x', 'mimeType': 'application/octet-stream', 'blob': 'eA=='}, origin)
    tampered = {**result, 'origin': {**origin, 'receipt_id': 'substituted'}}
    body = mcp_media._packed(tampered)
    media.store.query('UPDATE extension_media_snapshots SET body=?,body_hash=? WHERE id=?', (body, hashlib.sha256(body.encode()).hexdigest(), result['id']))
    with pytest.raises(PolicyError, match='integrity failed'):
        media.snapshot('run-a', result['id'])


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
def test_local_png_cache_tamper_and_symlink_are_refused(media, origin):
    result = media.ingest(block(raster()), origin)
    path = Path(media.native_artifact('run-a', result['id'])['preview_path'])
    path.write_bytes(b'fake')
    with pytest.raises(PolicyError, match='preview integrity'):
        media.native_artifact('run-a', result['id'])
    path.unlink()
    target = media.root / 'not-the-preview'
    target.write_bytes(b'fake')
    path.symlink_to(target)
    with pytest.raises(PolicyError, match='preview integrity'):
        media.native_artifact('run-a', result['id'])


def test_atomic_run_budget_is_enforced(media, origin, monkeypatch):
    monkeypatch.setattr(mcp_media, 'MAX_RUN_ITEMS', 1)
    value = {'uri': 'fixture://x', 'mimeType': 'application/octet-stream', 'blob': 'eA=='}
    media.ingest(value, origin)
    with pytest.raises(PolicyError, match='snapshot budget'):
        media.ingest(value, origin)
    assert len(media.store.query('SELECT * FROM extension_media_snapshots')) == 1


def test_unsupported_platform_fails_closed_before_raster_parser(media, origin, monkeypatch):
    monkeypatch.setattr(mcp_media.sys, 'platform', 'linux')
    with pytest.raises(PolicyError, match='supported macOS attachment sandbox'):
        media.ingest(block(raster()), origin)
    assert not list(media.root.iterdir())


def test_media_outer_transaction_guard_does_not_commit_or_decode(media, origin, monkeypatch):
    monkeypatch.setattr(mcp_media, '_parse_raster', lambda *args: pytest.fail('Media decoded within an outer transaction'))
    with media.store.lock:
        media.store.db.execute('BEGIN')
        media.store.db.execute('INSERT INTO settings VALUES(?,?)', ('media-outer-transaction', '"pending"'))
        try:
            with pytest.raises(PolicyError, match='outside transaction boundary'):
                media.ingest(block(raster()), origin)
            assert media.store.db.in_transaction
            assert media.store.db.execute('SELECT count(*) FROM extension_media_snapshots').fetchone()[0] == 0
        finally:
            media.store.db.rollback()
    assert not media.store.query('SELECT * FROM settings WHERE key=?', ('media-outer-transaction',))
    assert not list(media.root.iterdir())


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
@pytest.mark.parametrize('profile,reason', [
    ({'kind': 'gguf', 'model': 'local', 'context': 4096}, 'does not support image input'),
    ({'kind': 'api', 'model': 'gpt-4o', 'budget': 4096, 'context': 32768}, 'insufficient image allowance'),
])
def test_4k_model_receives_bounded_metadata_and_native_png_remains_available(media, origin, profile, reason):
    from pebble.token_budget import TokenBudget
    long_origin = {**origin, 'identifier': 'render.' + 'x' * 900}
    media.review = lambda value: value == long_origin
    image = media.ingest(block(raster(size=(1, 1))), long_origin)
    parts = media.model_content('run-a', [image['id']], profile)
    assert [part['type'] for part in parts] == ['text']
    text = parts[0]['text']
    assert reason in text and 'image pixels were not sent' in text
    assert 'UNTRUSTED historical' in text and image['id'] in text
    assert origin['receipt_id'] in text and 'x' * 200 not in text
    assert 'data:image/' not in text and 'preview_path' not in text
    counter = TokenBudget(Path('src-tauri/resources'))
    assert counter.count([{'role': 'user', 'content': parts}]) < 1000
    assert media.native_artifact('run-a', image['id'])['preview'].startswith('data:image/png;base64,')
    assert media.snapshot('run-a', image['id'])['origin'] == long_origin


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
def test_32k_single_pixel_vision_and_host_receipt_keep_binary_out_of_tool_text(media, origin):
    from pebble.app import App
    from pebble.token_budget import TokenBudget
    image = media.ingest(block(raster(size=(1, 1))), origin)
    profile = {'kind': 'api', 'model': 'gpt-4o', 'budget': 32768, 'context': 32768}
    parts = media.model_content('run-a', [image['id']], profile)
    assert [part['type'] for part in parts] == ['text', 'image_url']
    counter = TokenBudget(Path('src-tauri/resources'))
    count = counter.count([{'role': 'user', 'content': parts}])
    assert 16000 < count < 32768  # The counter reserves 16K even for this one-pixel raster.
    host = SimpleNamespace(task_policy={'tools': {'plugins': True}}, tool_hook=lambda *args: False,
        active_run='run-a', plugin_media=media, model=SimpleNamespace(current=lambda: profile))
    receipt = App._execute(host, 'ReadPluginMedia', {'snapshot_ids': json.dumps([image['id']])}, None, 'chat-a')
    encoded = json.dumps(receipt)
    assert receipt['images_sent'] == 1 and receipt['selected_snapshot_ids'] == [image['id']]
    assert receipt['authorization'] == 'none' and receipt['freshness'] == 'historical_snapshot'
    assert 'data:image/' not in encoded and 'preview_path' not in encoded and 'image_url' not in encoded
    host.task_policy['tools']['plugins'] = False
    with pytest.raises(PolicyError, match='disabled in Permissions'):
        App._execute(host, 'ReadPluginMedia', {'snapshot_ids': json.dumps([image['id']])}, None, 'chat-a')


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
@pytest.mark.parametrize('budget,expected_images', [(32768, 1), (65536, 2), (128000, 4)])
def test_multiple_selected_images_fit_conservative_budget_in_selection_order(media, origin, budget, expected_images):
    from pebble.token_budget import TokenBudget
    images = [media.ingest(block(raster(size=(i + 1, 1))), origin) for i in range(4)]
    parts = media.model_content('run-a', [image['id'] for image in images],
        {'kind': 'api', 'model': 'gpt-4o', 'context': budget})
    pixels = [part for part in parts if part['type'] == 'image_url']
    captions = [part['text'] for part in parts if part['type'] == 'text']
    assert len(pixels) == expected_images and len(captions) == 4
    for image, pixel in zip(images, pixels):
        png = base64.b64decode(pixel['image_url']['url'].split(',', 1)[1], validate=True)
        assert hashlib.sha256(png).hexdigest() == image['sha256']
    for index, caption in enumerate(captions):
        assert images[index]['id'] in caption and 'historical' in caption
        if index >= expected_images:
            assert 'image pixels were not sent' in caption and 'native preview remains available' in caption
    counter = TokenBudget(Path('src-tauri/resources'))
    assert expected_images * 16000 < counter.count([{'role': 'user', 'content': parts}]) < budget


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
@pytest.mark.parametrize('profile,expected_images', [
    (None, 0), ({}, 0), ({'kind': 'api', 'model': 'unknown-model'}, 0),
    ({'kind': 'api', 'model': 'gpt-4o'}, 4),
])
def test_unknown_profile_does_not_claim_vision_and_legacy_missing_budget_is_bounded(media, origin, profile, expected_images):
    images = [media.ingest(block(raster(size=(1, 1))), origin) for _ in range(4)]
    parts = media.model_content('run-a', [image['id'] for image in images], profile)
    assert sum(part['type'] == 'image_url' for part in parts) == expected_images
    assert sum(part['type'] == 'text' for part in parts) == 4
    if not expected_images:
        assert all('does not support image input' in part['text'] for part in parts)


@pytest.mark.skipif(sys.platform != 'darwin', reason='MCP raster conversion requires native macOS confinement')
def test_opaque_selection_does_not_consume_image_allowance(media, origin):
    opaque = media.ingest({'uri': 'fixture://opaque', 'mimeType': 'application/octet-stream', 'blob': 'eA=='}, origin)
    images = [media.ingest(block(raster(size=(i + 1, 1))), origin) for i in range(3)]
    parts = media.model_content('run-a', [opaque['id'], *[image['id'] for image in images]],
        {'kind': 'api', 'model': 'gpt-4o', 'budget': 65536})
    assert sum(part['type'] == 'image_url' for part in parts) == 2
    assert opaque['id'] in parts[0]['text'] and parts[1]['type'] == 'text'
    assert images[-1]['id'] in parts[-1]['text'] and 'image pixels were not sent' in parts[-1]['text']


@pytest.mark.parametrize('kind', ['image', 'audio', 'resource'])
def test_normalization_does_not_bypass_raw_media_known_secret_rejection(media, origin, monkeypatch, kind):
    from pebble.mcp_content import Content, normalize
    from pebble.mcp_transport import MCPUncertain
    secret = 'synthetic-secret-before-normalization'
    media.scrub = connector_secret_filter(lambda plugin_id, text: text.replace(secret, '[redacted]'))
    if kind == 'image':
        candidate = block(raster(secret=secret))
    elif kind == 'audio':
        candidate = block(b'RIFF\x10\0\0\0WAVEfmt ' + secret.encode(), 'audio/wav', 'audio')
    else:
        candidate = {'type': 'resource', 'resource': {'uri': 'fixture://opaque', 'mimeType': 'application/octet-stream',
            'blob': base64.b64encode(secret.encode()).decode()}}
    response = {'content': [candidate]}
    normalized = normalize(response)
    assert 'data' not in json.dumps(normalized) and secret not in json.dumps(normalized)
    ext = SimpleNamespace(store=media.store, media=media,
        media_origin=lambda *args: origin,
        publish_media=lambda metadata: pytest.fail('A credential-bearing snapshot was published'))
    content = Content(ext)
    monkeypatch.setattr(mcp_media, '_parse_raster', lambda *args: pytest.fail('Credential-bearing bytes reached the decoder'))
    with pytest.raises(MCPUncertain, match='could not be safely archived') as rejected:
        content.capture(response, 'tool', origin['identifier'], {'plugin_id': origin['plugin_id']}, 'run-a', None)
    assert secret not in str(rejected.value)
    assert not media.store.query('SELECT * FROM extension_media_snapshots')
    assert not list(media.root.iterdir())
