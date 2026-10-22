"""Loopback-only static previews of a filtered snapshot, with no directory listing."""
import mimetypes
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from urllib.parse import urlsplit, unquote

ALLOWED = {'.html', '.css', '.js', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.ico'}


class Previews:
    def __init__(self):
        self.servers = {}

    def start(self, workspace):
        files, total = {}, 0
        for name in set(workspace.files()) | set(workspace.drafts):
            path = PurePosixPath(name.replace('\\', '/'))
            if path.suffix.lower() not in ALLOWED or any(p.startswith('.') for p in path.parts):
                continue
            if name in workspace.drafts:
                raw = workspace.drafts[name]['after'].encode()
            else:
                p = workspace.path(name)
                if p.stat().st_size > 2_000_000:
                    continue
                raw = p.read_bytes()
            total += len(raw)
            if total > 12_000_000 or len(files) >= 150:
                raise ValueError('Static preview exceeds its 12 MB / 150-file limit.')
            files[path.as_posix()] = raw
        if 'index.html' not in files:
            raise ValueError('Create index.html in the project before starting a static preview.')
        self.stop(str(workspace.root))
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                    self.send_error(403)
                    return
                name = unquote(urlsplit(self.path).path).lstrip('/') or 'index.html'
                if name not in files:
                    self.send_error(404)
                    return
                raw = files[name]
                self.send_response(200)
                self.send_header('Content-Type', mimetypes.guess_type(name)[0] or 'application/octet-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'self' data:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'none'; frame-ancestors 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        key = str(workspace.root)
        self.servers[key] = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        timer = threading.Timer(1800, self.stop, args=(key, server))
        timer.daemon = True
        timer.start()
        return {'status': 'running', 'url': f'http://127.0.0.1:{server.server_port}/', 'files': sorted(files),
                'draft_snapshot': bool(workspace.drafts), 'expires_minutes': 30,
                'message': 'A local static snapshot is running. No project commands executed. Restart preview after changing files.'}

    def stop(self, key, expected=None):
        server = self.servers.get(key)
        if server and (expected is None or expected is server):
            self.servers.pop(key, None)
            server.shutdown()
            server.server_close()

    def close(self):
        for key in list(self.servers):
            self.stop(key)
