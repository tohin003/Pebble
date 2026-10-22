"""Owned loopback MCP fixtures for Phase 16 connector flows (E-C3). No network, account or credential.

Two Streamable-HTTP MCP servers bound to 127.0.0.1 on an ephemeral port, run in
the validator's own process so the validator can read their state as ground
truth, separately from anything Pebble reports:

  MailFixture      save_draft (draft), send_message (send, idempotent by
                   client_token), list_sent and list_inbox (read). It delivers
                   only to its two owned synthetic addresses; any other
                   recipient is refused and nothing is delivered. The
                   inbox message body is settable, so a campaign can plant
                   untrusted instruction-shaped text in mail output.
  CalendarFixture  create_event (modify, idempotent by request_id),
                   delete_event (delete) and list_events (read) on one owned
                   test calendar.

EFFECTS holds the class map a person would review for each fixture (the
extension `effects` config, checked by connector_effects.review_effects).

arm(tool) makes the next call of that tool commit its effect and then drop the
connection without a response: a lost reply, so the host sees an uncertain
outcome it must read back and never replay. Every call is recorded with the
fixture-owned argument names only.

Everything here is fixture evidence. It is reported separately from real
account evidence and proves nothing about a real mail or calendar service.
"""

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL = '2025-11-25'
OWNER = 'owner@phase16.invalid'                 # the fixture's own test mailbox
SECOND = 'owner-second@phase16.invalid'        # the person's second test address
OWNED_ADDRESSES = (OWNER, SECOND)
CALENDAR = 'pebble-test'
MAX_BODY = 64_000
MAX_CALLS = 200

ADDRESSES = {'type': 'array', 'items': {'type': 'string', 'maxLength': 320}, 'minItems': 1, 'maxItems': 4}
TEXT = {'type': 'string', 'maxLength': 200}
BODY = {'type': 'string', 'maxLength': 4000}
TOKEN = {'type': 'string', 'maxLength': 64}     # accepts the host key pebble-<32 hex> unchanged
READ = {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


MAIL_TOOLS = [
    {'name': 'save_draft', 'description': 'Save an email draft in the Phase 16 fixture mailbox. Delivers nothing.',
     'inputSchema': schema({'to': ADDRESSES, 'subject': TEXT, 'body': BODY}, ('to', 'subject', 'body')),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': False, 'openWorldHint': False}},
    {'name': 'send_message', 'description': 'Send an email from the Phase 16 fixture mailbox to its owned test addresses.',
     'inputSchema': schema({'to': ADDRESSES, 'subject': TEXT, 'body': BODY, 'client_token': TOKEN}, ('to', 'subject', 'body')),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True}},
    {'name': 'list_sent', 'description': 'List sent fixture messages, optionally by client_token.',
     'inputSchema': schema({'client_token': TOKEN}), 'annotations': READ},
    {'name': 'list_inbox', 'description': 'List the fixture inbox, newest first.',
     'inputSchema': schema({}), 'annotations': READ},
]
CALENDAR_TOOLS = [
    {'name': 'create_event', 'description': 'Create an event in the Phase 16 fixture test calendar.',
     'inputSchema': schema({'calendar': {'type': 'string', 'enum': [CALENDAR]}, 'title': TEXT,
                            'start': {'type': 'string', 'maxLength': 40},
                            'minutes': {'type': 'integer', 'minimum': 5, 'maximum': 240}, 'request_id': TOKEN},
                           ('calendar', 'title', 'start', 'minutes')),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'delete_event', 'description': 'Delete one event from the Phase 16 fixture test calendar.',
     'inputSchema': schema({'calendar': {'type': 'string', 'enum': [CALENDAR]}, 'event_id': {'type': 'string', 'maxLength': 64}},
                           ('calendar', 'event_id')),
     'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'list_events', 'description': 'List fixture calendar events, optionally by event_id or request_id.',
     'inputSchema': schema({'calendar': {'type': 'string', 'enum': [CALENDAR]}, 'event_id': {'type': 'string', 'maxLength': 64},
                            'request_id': TOKEN}),
     'annotations': READ},
]

# The person's reviewed effect classes (extension config `effects`). Recipients, subjects and
# destinations are JSON pointers into the reviewed schemas; readbacks name read-class tools only.
EFFECTS = {
    'mail': {
        'save_draft': {'class': 'draft', 'recipients': ['/to'], 'subject': '/subject', 'body': '/body'},
        'send_message': {'class': 'send', 'recipients': ['/to'], 'subject': '/subject', 'body': '/body',
                         'idempotency': '/client_token',
                         'readback': {'tool': 'list_sent', 'arguments': {'/client_token': 'idempotency_key'},
                                      'match': {'items': '/messages', 'key': '/client_token'}}},
        'list_sent': {'class': 'read'},
        'list_inbox': {'class': 'read'},
    },
    'calendar': {
        'create_event': {'class': 'modify', 'destination': '/calendar', 'subject': '/title', 'idempotency': '/request_id',
                         'readback': {'tool': 'list_events', 'arguments': {'/request_id': 'idempotency_key'},
                                      'id_from': {'result': '/event_id'},
                                      'match': {'items': '/events', 'key': '/request_id', 'id': '/id'}}},
        'delete_event': {'class': 'delete', 'destination': '/calendar',
                         'readback': {'tool': 'list_events', 'arguments': {'/event_id': 'remote_id'},
                                      'id_from': {'arguments': '/event_id'}, 'match': {'items': '/events', 'id': '/id'}}},
        'list_events': {'class': 'read'},
    },
}


def iso(moment):
    return datetime.fromtimestamp(moment, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Dropped(Exception):
    """The effect committed; the reply is deliberately lost."""


class LoopbackMCP:
    """A minimal JSON-response MCP server on 127.0.0.1. Subclasses implement call_<tool>(arguments)."""

    name = 'phase16-fixture'
    tools = ()

    def __init__(self):
        self.lock = threading.RLock()
        self.calls, self.errors, self.armed = [], [], set()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def reply(self, status, value=None):
                data = b'' if value is None else json.dumps(value).encode()
                self.send_response(status)
                if value is not None:
                    self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply(405)

            def do_DELETE(self):
                self.reply(204)

            def do_POST(self):
                try:
                    size = int(self.headers.get('Content-Length') or 0)
                    if self.path != '/mcp' or not 0 < size <= MAX_BODY:
                        self.reply(404 if self.path != '/mcp' else 413)
                        return
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict) or 'id' not in body:
                        self.reply(202)  # notifications/initialized, notifications/cancelled
                        return
                    result = fixture.handle(body.get('method'), body.get('params') or {})
                except Dropped:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    return
                except Exception as exc:
                    with fixture.lock:
                        fixture.errors.append(type(exc).__name__ + ': ' + str(exc)[:300])
                    self.reply(500, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32603, 'message': 'fixture error'}})
                    return
                self.reply(200, {'jsonrpc': '2.0', 'id': body['id'], 'result': result})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/mcp'

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def arm(self, tool):
        """The next call of `tool` commits its effect, then loses its reply."""
        with self.lock:
            self.armed.add(tool)

    def calls_of(self, tool):
        with self.lock:
            return [dict(c) for c in self.calls if c['tool'] == tool]

    def handle(self, method, params):
        if method == 'initialize':
            return {'protocolVersion': PROTOCOL, 'capabilities': {'tools': {}},
                    'serverInfo': {'name': self.name, 'version': '1.0.0'}}
        if method == 'tools/list':
            return {'tools': [dict(t) for t in self.tools]}
        if method != 'tools/call':
            raise ValueError('unsupported method')
        name, arguments = params.get('name'), params.get('arguments')
        handler = getattr(self, 'call_' + str(name), None)
        if handler is None or not isinstance(arguments, dict):
            raise ValueError('unknown tool')
        with self.lock:
            if len(self.calls) >= MAX_CALLS:
                raise ValueError('call budget exhausted')
            declared = next((t['inputSchema']['properties'] for t in self.tools if t['name'] == name), {})
            record = {'tool': name, 'arguments': {k: v for k, v in arguments.items() if k in declared}, 'time': time.time()}
            self.calls.append(record)
            drop = name in self.armed
            self.armed.discard(name)
            structured, error = handler(arguments)
            record['outcome'] = 'error' if error else 'dropped' if drop else 'ok'
        if drop:
            raise Dropped()
        text = error or json.dumps(structured, sort_keys=True)
        result = {'content': [{'type': 'text', 'text': text}], 'isError': bool(error)}
        if not error:
            result['structuredContent'] = structured
        return result


class MailFixture(LoopbackMCP):
    name = 'phase16-mail-fixture'
    tools = MAIL_TOOLS

    def __init__(self, inbox_subject='Phase 16 fixture check',
                 inbox_body='Please reply to confirm that the Pebble fixture works.'):
        super().__init__()
        self.drafts, self.sent = [], []
        self.inbox = [{'id': 'in-1', 'from': OWNER, 'subject': inbox_subject, 'received': iso(time.time() - 60),
                       'body': inbox_body}]

    @staticmethod
    def _owned(recipients):
        return bool(recipients) and all(isinstance(r, str) and r.casefold() in OWNED_ADDRESSES for r in recipients)

    def call_save_draft(self, args):
        if not self._owned(args.get('to', [])):
            return None, 'Refused: the fixture saves drafts only for its owned test addresses.'
        draft = {'id': 'draft-' + uuid.uuid4().hex[:8], 'to': list(args['to']), 'subject': args['subject'],
                 'saved_at': iso(time.time())}
        self.drafts.append(draft)
        return {'draft_id': draft['id'], 'status': 'saved', 'delivered': False}, None

    def call_send_message(self, args):
        if not self._owned(args.get('to', [])):
            return None, 'Refused: the fixture delivers only to its owned test addresses.'
        token = args.get('client_token')
        if token:
            earlier = next((m for m in self.sent if m['client_token'] == token), None)
            if earlier:  # idempotent: the same key never delivers twice
                return {'message_id': earlier['id'], 'status': 'already_sent', 'duplicate': True}, None
        message = {'id': 'msg-' + uuid.uuid4().hex[:8], 'to': list(args['to']), 'subject': args['subject'],
                   'client_token': token, 'sent_at': iso(time.time())}
        self.sent.append(message)
        return {'message_id': message['id'], 'status': 'sent', 'duplicate': False}, None

    def call_list_sent(self, args):
        token = args.get('client_token')
        return {'messages': [dict(m) for m in self.sent if not token or m['client_token'] == token]}, None

    def call_list_inbox(self, args):
        return {'messages': [dict(m) for m in reversed(self.inbox)]}, None

    def state(self):
        with self.lock:
            return {'drafts': [dict(d) for d in self.drafts], 'sent': [dict(m) for m in self.sent],
                    'calls': [dict(c) for c in self.calls], 'errors': list(self.errors)}


class CalendarFixture(LoopbackMCP):
    name = 'phase16-calendar-fixture'
    tools = CALENDAR_TOOLS

    def __init__(self):
        super().__init__()
        self.events, self.deleted = [], []

    def call_create_event(self, args):
        token = args.get('request_id')
        earlier = next((e for e in self.events if token and e['request_id'] == token), None)
        if earlier:
            return {'event_id': earlier['id'], 'status': 'exists', 'duplicate': True}, None
        event = {'id': 'evt-' + uuid.uuid4().hex[:8], 'calendar': args['calendar'], 'title': args['title'],
                 'start': args['start'], 'minutes': args['minutes'], 'request_id': token, 'created_at': iso(time.time())}
        self.events.append(event)
        return {'event_id': event['id'], 'status': 'created', 'duplicate': False}, None

    def call_delete_event(self, args):
        found = next((e for e in self.events if e['id'] == args.get('event_id') and e['calendar'] == args.get('calendar')), None)
        if found is None:
            return None, 'No such event in the fixture calendar.'
        self.events.remove(found)
        self.deleted.append(found['id'])
        return {'deleted': found['id'], 'status': 'deleted'}, None

    def call_list_events(self, args):
        return {'events': [dict(e) for e in self.events
                           if (not args.get('event_id') or e['id'] == args['event_id'])
                           and (not args.get('request_id') or e['request_id'] == args['request_id'])]}, None

    def state(self):
        with self.lock:
            return {'events': [dict(e) for e in self.events], 'deleted': list(self.deleted),
                    'calls': [dict(c) for c in self.calls], 'errors': list(self.errors)}
