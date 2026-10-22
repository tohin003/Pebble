"""Frozen attachment regression with disposable files and a loopback provider."""
import argparse
import json
import queue
import subprocess
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--resources', type=Path, default=ROOT / '.runtime/attachment-release/ui/src-tauri/resources')
args = parser.parse_args()
resources = args.resources.resolve()
records, target, events = [], None, []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({'data': [{'id': 'gpt-6-luna', 'context_window': 1050000, 'input_modalities': ['text', 'image']}]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        records.append(body)
        output = []
        if target and not any(i.get('type') == 'function_call_output' for i in body['input']):
            output = [{'type': 'function_call', 'call_id': 'read-file', 'name': 'ReadAttachment', 'arguments': json.dumps({'attachment_id': target, 'offset': 600000, 'limit': 100})}]
        else:
            output = [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Synthetic attachment received.'}]}]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        event = {'type': 'response.completed', 'response': {'status': 'completed', 'output': output, 'usage': {'input_tokens': 120, 'output_tokens': 5}}}
        self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
with tempfile.TemporaryDirectory(prefix='pebble-attachments-') as work:
    home, files = Path(work) / 'home', Path(work) / 'files'
    files.mkdir()
    image = files / 'Screenshot 2026-10-09 at 12.51.36\u202fPM 日本語.png'
    Image.new('RGB', (2940, 1912), '#6d8063').save(image)
    text = files / 'report.unknown'
    text.write_text('a' * 600000 + 'PAGEABLE FILE END')
    docx = files / 'report.docx'
    with zipfile.ZipFile(docx, 'w') as archive:
        archive.writestr('word/document.xml', '<document><p><t>Document paragraphs and tables</t></p></document>')
    heic = files / 'photo.heic'
    subprocess.run(['/usr/bin/sips', '-s', 'format', 'heic', str(image), '--out', str(heic)], check=True, capture_output=True)
    opaque = files / 'custom.bin'
    opaque.write_bytes(b'\x00\x01\xffopaque fixture')

    process, reader, lines, sequence = None, None, queue.Queue(), 0
    def start():
        global process, reader
        process = subprocess.Popen([str(resources / 'backend/pebble-backend'), '--home', str(home), '--resources', str(resources)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def read():
            for line in process.stdout:
                lines.put(json.loads(line))
        reader = threading.Thread(target=read, daemon=True)
        reader.start()

    def stop():
        process.stdin.close()
        process.wait(timeout=15)
        assert process.returncode == 0, process.stderr.read()
        reader.join(timeout=2)

    def rpc(method, **params):
        global sequence
        sequence += 1
        rid = str(sequence)
        process.stdin.write(json.dumps({'id': rid, 'method': method, 'params': params}) + '\n')
        process.stdin.flush()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            item = lines.get(timeout=max(.1, deadline - time.monotonic()))
            if item.get('id') == rid:
                if 'error' in item:
                    raise AssertionError(item['error'])
                return item['result']
            events.append(item)
        raise TimeoutError(method)

    def wait():
        deadline = time.monotonic() + 20
        while rpc('state')['active'] and time.monotonic() < deadline:
            time.sleep(.05)
        assert not rpc('state')['active']
        assert not [e for e in events if e.get('event') == 'error'], events[-5:]

    try:
        start()
        assert rpc('state')['version'] == '0.4.2'
        url = f'http://127.0.0.1:{server.server_port}/v1'
        rpc('model_discover', url=url, kind='responses')
        rpc('model_save_many', name='Local test', url=url, kind='responses', models=['gpt-6-luna'], tools=True)
        cid = rpc('new_chat')['id']
        screenshot = rpc('attach', id=cid, path=str(image))
        assert screenshot['kind'] == 'image'
        heif = rpc('attach', id=cid, path=str(heic))
        assert heif['preview'].startswith('data:image/jpeg;base64,')
        rpc('remove_source', id=cid, source_id=heif['id'])
        rpc('send', id=cid, text='Describe this screenshot.', source_ids=[screenshot['id']])
        wait()
        assert any(p.get('type') == 'input_image' for i in records[-1]['input'] for p in i.get('content', []) if isinstance(p, dict))
        assert rpc('get_chat', id=cid)['messages'][0]['attachments'][0]['name'] == image.name
        cid2 = rpc('new_chat')['id']
        document = rpc('attach', id=cid2, path=str(docx))
        binary = rpc('attach', id=cid2, path=str(opaque))
        assert document['characters'] > 10 and binary['kind'] == 'file' and binary['notice']
        for item in (document, binary):
            rpc('remove_source', id=cid2, source_id=item['id'])
        source = rpc('attach', id=cid2, path=str(text))
        target = source['id']
        text.unlink()
        rpc('send', id=cid2, text='Read the ending of the attached file.', source_ids=[target])
        wait()
        outputs = [i['output'] for i in records[-1]['input'] if i.get('type') == 'function_call_output']
        assert any('PAGEABLE FILE END' in o for o in outputs), outputs
        assert any(t['name'] == 'ReadAttachment' for t in records[-1]['tools'])
        stop()
        start()
        assert rpc('get_chat', id=cid2)['messages'][0]['attachments'][0]['id'] == target
        rpc('send', id=cid2, text='What was at the end of that file?', source_ids=[])
        wait()
        assert 'PAGEABLE FILE END' in json.dumps(records[-1])
        rpc('delete_chat', id=cid2)
        rpc('delete_chat', id=cid)
        assert not rpc('state')['chats']
        assert not list((home / 'attachments').iterdir())
        report = {'frozen_backend': True, 'unicode_screenshot': True, 'heic': True, 'native_provider_image_parts': True, 'office_document': True, 'arbitrary_binary_file': True, 'read_attachment_tool_without_project': True, 'long_file_paging': True, 'snapshot_survives_original_deletion': True, 'document_followup_after_restart': True, 'deletion_cleanup': True, 'paid_inference': False, 'fixture_provider': 'loopback only'}
        (ROOT / 'reports/attachments-frozen-v042.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))
    finally:
        if process and process.poll() is None:
            stop()
        server.shutdown()
        server.server_close()
