"""Bounded static loopback snapshots. Executable UI mods use mod_worker.

No generated-frame input/RPC bridge exists. Authority controls live in the native
shell; the generated document contains only sanitized presentation data.
"""
import html as escape
import re
import secrets
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class _StaticHTML(HTMLParser):
    TAGS = frozenset('div span p h1 h2 h3 h4 h5 h6 main section article header footer aside strong b em i small pre code blockquote ul ol li table thead tbody tr th td caption br hr'.split())
    DROP = frozenset('script style iframe object embed svg math form input button textarea select video audio img link meta base canvas template'.split())
    VOID = frozenset('br hr img input embed link meta base'.split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output, self.stack, self.nodes = [], [], 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 512 or len(self.stack) >= 32:
            raise ValueError('Static views support 512 elements and 32 nesting levels.')
        suppressed = any(s[1] for s in self.stack) or tag in self.DROP
        if not suppressed and tag in self.TAGS:
            safe = []
            for name, value in attrs:
                if name in ('class', 'title') and value and len(value) <= 120 and re.fullmatch(r'[\w .,:-]+', value):
                    safe.append(f' {name}="{escape.escape(value, quote=True)}"')
                elif name in ('colspan', 'rowspan') and tag in ('td', 'th') and value and value.isdigit() and 1 <= int(value) <= 16:
                    safe.append(f' {name}="{value}"')
            self.output.append('<' + tag + ''.join(safe) + '>')
        if tag not in self.VOID:
            self.stack.append((tag, suppressed))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        while self.stack:
            opened, suppressed = self.stack.pop()
            if not suppressed and opened in self.TAGS:
                self.output.append('</' + opened + '>')
            if opened == tag:
                break

    def handle_data(self, data):
        if not any(s[1] for s in self.stack):
            self.output.append(escape.escape(data))


def static_html(source):
    if not isinstance(source, str) or len(source.encode()) > 60000:
        raise ValueError('Static HTML exceeds its limit.')
    parser = _StaticHTML()
    parser.feed(source)
    while parser.stack:
        parser.handle_endtag(parser.stack[-1][0])
    return ''.join(parser.output)


def static_css(source):
    if not isinstance(source, str) or len(source.encode()) > 40000:
        raise ValueError('Static CSS exceeds its limit.')
    # Deliberately small CSS contract: no external resource, animation, shader,
    # positioning overlay, arbitrary selector complexity or huge geometry.
    if not source.strip():
        return ''
    allowed = frozenset('color background-color font-size font-weight text-align white-space border border-color border-width border-style border-radius padding margin gap width max-width display flex-direction justify-content align-items'.split())
    if any(x in source.lower() for x in ('@', 'url', 'expression', '\\', '/*', 'calc(', 'var(')):
        raise ValueError('Static CSS cannot load resources or execute dynamic rules.')
    result, count = [], 0
    for rule in source.split('}'):
        if not rule.strip():
            continue
        if rule.count('{') != 1:
            raise ValueError('Use simple static CSS rules.')
        selector, declarations = rule.split('{')
        if not 1 <= len(selector.strip()) <= 120 or not re.fullmatch(r'[A-Za-z0-9_.#, \-]+', selector):
            raise ValueError('Use simple element or class selectors.')
        safe = []
        for declaration in declarations.split(';'):
            if not declaration.strip():
                continue
            if declaration.count(':') != 1:
                raise ValueError('Use simple static CSS declarations.')
            name, value = [s.strip().lower() for s in declaration.split(':')]
            if name not in allowed or len(value) > 80 or not re.fullmatch(r'[A-Za-z0-9#., %\-]+', value):
                raise ValueError('This CSS property or value is outside the static view contract.')
            geometry = re.sub(r'#[a-f0-9]{3,8}\b', '', value)
            for number, unit in re.findall(r'(-?\d+(?:\.\d+)?)(px|em|rem|%|vh|vw)?', geometry):
                if abs(float(number)) > (100 if unit in ('%', 'vh', 'vw', 'em', 'rem') else 4096):
                    raise ValueError('Static CSS geometry exceeds its limit.')
            safe.append(name + ':' + value)
            count += 1
        if count > 256 or len(result) >= 64:
            raise ValueError('Static CSS exceeds its rule limit.')
        result.append(selector + '{' + ';'.join(safe) + '}')
    return ''.join(result)


class Views:
    def __init__(self, guard=None):
        self.pages, self.server, self.guard = {}, None, guard
        self.lock = threading.RLock()

    def stage(self, html, css='', script='', binding=None):
        if not all(isinstance(v, str) for v in (html, css, script)) or len((html+css+script).encode()) > 120000:
            raise ValueError('Generated view source exceeds its limit.')
        html, css = static_html(html), static_css(css)
        with self.lock:
            if not self.server:
                owner = self
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass
                    def do_GET(self):
                        with owner.lock:
                            page = owner.pages.get(self.path)
                        valid = bool(page and page[2] > time.time())
                        if valid and page[3] and owner.guard:
                            try:
                                valid = owner.guard(page[3])
                            except Exception:
                                valid = False
                        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}' or not valid:
                            self.send_error(404)
                            return
                        doc, policy, _, _ = page
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.send_header('Content-Security-Policy', policy)
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Referrer-Policy', 'no-referrer')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.send_header('Content-Length', str(len(doc)))
                        self.end_headers()
                        self.wfile.write(doc)
                self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                self.server.daemon_threads = True
                threading.Thread(target=self.server.serve_forever, daemon=True).start()
            key, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            path = '/view/' + key
            # allow-scripts retained for opaque-origin compatibility, but no
            # supplied script is emitted. The sole nonce script is host-owned.
            policy = f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; sandbox allow-scripts"
            document = f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{{font:14px system-ui;margin:20px;background:#f4f6ef;color:#223024}}{css}</style></head><body>{html}<script nonce="{nonce}">/* Static presentation; no input channel. */</script></body></html>'
            expires = time.time() + 300
            if len(self.pages) >= 8:
                self.pages.pop(next(iter(self.pages)))
            self.pages[path] = (document.encode(), policy, expires, dict(binding) if binding else None)
            return {'url': f'http://127.0.0.1:{self.server.server_port}{path}', 'expires_at': expires,
                    'origin': 'generated_view', 'authorization': 'none', 'script_blocked': bool(script.strip())}

    def stop(self, url):
        if not isinstance(url, str):
            raise ValueError('Use the exact generated view URL.')
        parsed = urlsplit(url)
        with self.lock:
            if not self.server or parsed.scheme != 'http' or parsed.netloc != f'127.0.0.1:{self.server.server_port}' or parsed.query or parsed.fragment:
                raise ValueError('Use the exact generated view URL.')
            self.pages.pop(parsed.path, None)
        return True

    def revoke(self, artifact_id=None, chat_id=None):
        with self.lock:
            for path, page in list(self.pages.items()):
                binding = page[3] or {}
                if (artifact_id and binding.get('artifact_id') == artifact_id) or (chat_id and binding.get('chat_id') == chat_id):
                    self.pages.pop(path, None)

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.pages.clear()
