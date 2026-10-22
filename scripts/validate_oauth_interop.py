"""Synthetic source/frozen RPC OAuth interoperability with owned OS-vault cleanup."""
import argparse
import base64
import faulthandler
import hashlib
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from validate_runtime import ROOT, Service
sys.path.insert(0, str(ROOT/'backend'))
from pebble.connector_auth import ConnectorVault, Records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--trace-after', type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.trace_after <= 120:
        parser.error('--trace-after must be between 1 and 120 seconds')
    faulthandler.enable()
    faulthandler.dump_traceback_later(args.trace_after, repeat=True)
    def progress(stage):
        print('OAuth interop: '+stage, file=sys.stderr, flush=True)
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT/'backend/main.py')]
    values = {key: 'synthetic-interop-'+key+'-'+uuid.uuid4().hex
              for key in ('access', 'refresh', 'registration', 'unused-secret')}
    state = {'home': None, 'mode': 'normal', 'gets': [], 'registrations': [],
             'mcp_posts': [], 'unapproved_mcp_posts': 0, 'mcp_allowed': False,
             'tokens': [], 'revocations': 0, 'expected_digest': None, 'challenge': None}
    checks = []

    def rows(sql, parameters=()):
        database = sqlite3.connect(state['home']/'pebble.sqlite')
        try:
            database.row_factory = sqlite3.Row
            return [dict(row) for row in database.execute(sql, parameters).fetchall()]
        finally:
            database.close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def write(self, value, status=200):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            state['gets'].append(self.path)
            if self.path == '/mcp':
                self.send_response(401)
                self.send_header('WWW-Authenticate', state['challenge'] or
                    f'Digest realm="fixture, realm", nonce="a\\\"b", Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp", scope="read"')
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{}')
            elif self.path == '/.well-known/oauth-protected-resource/mcp':
                self.write({'resource': base+'/mcp', 'authorization_servers': [issuer], 'scopes_supported': ['default:read']})
            elif self.path == '/tenant/.well-known/openid-configuration':
                self.write({'issuer': issuer, 'authorization_endpoint': base+'/authorize',
                    'token_endpoint': base+'/token', 'registration_endpoint': base+'/register',
                    'revocation_endpoint': base+'/revoke', 'code_challenge_methods_supported': ['S256'],
                    'token_endpoint_auth_methods_supported': ['none'], 'authorization_response_iss_parameter_supported': True})
            else:
                self.write({}, 404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            if self.path == '/register':
                body = json.loads(raw)
                assert self.headers.get('Authorization') is None
                assert body['token_endpoint_auth_method'] == 'none' and body['application_type'] == 'native'
                assert body['grant_types'] == ['authorization_code'] and body['response_types'] == ['code']
                assert body['scope'] == 'read'
                admission = rows('SELECT state,plan_digest FROM connector_registrations WHERE plan_digest=?', (state['expected_digest'],))
                assert admission == [{'state': 'admitted', 'plan_digest': state['expected_digest']}], {'admission': admission, 'expected_digest': state['expected_digest'], 'all_rows': rows('SELECT plugin_id,state,plan_digest FROM connector_registrations')}
                state['registrations'].append(body)
                if state['mode'] == 'lost-registration':
                    self.connection.shutdown(2)
                    self.connection.close()
                    return
                self.write({'client_id': 'owned-public-client', **body,
                    'registration_access_token': values['registration'],
                    'registration_client_uri': base+'/clients/owned-public-client',
                    'client_secret': values['unused-secret']}, 201)
                return
            if self.path in {'/token', '/revoke'}:
                form = {key: value[0] for key, value in parse_qs(raw.decode()).items()}
                assert form['client_id'] == 'owned-public-client' and 'client_secret' not in form
                if self.path == '/revoke':
                    assert form['token'] == values['refresh']
                    state['revocations'] += 1
                    self.send_response(200)
                    self.end_headers()
                    return
                assert form['resource'] == base+'/mcp' and form['redirect_uri'] == state['approved_redirect']
                assert base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'].encode()).digest()).decode().rstrip('=') == state['pkce']
                state['tokens'].append(form)
                self.write({'access_token': values['access'], 'refresh_token': values['refresh'],
                    'token_type': 'Bearer', 'expires_in': 3600, 'scope': 'read'})
                return
            body = json.loads(raw)
            state['mcp_posts'].append(body)
            if not state['mcp_allowed']:
                state['unapproved_mcp_posts'] += 1
            if self.headers.get('Authorization') != 'Bearer '+values['access']:
                self.write({}, 401)
                return
            if 'id' not in body:
                self.send_response(202)
                self.end_headers()
                return
            if body['method'] == 'initialize':
                result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}}
            elif body['method'] == 'tools/list':
                result = {'tools': [{'name': 'lookup', 'description': 'Synthetic read fixture '+values['registration'], 'inputSchema': {'type': 'object'}}]}
            else:
                self.write({'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32601, 'message': 'Fixture has no effects'}})
                return
            self.write({'jsonrpc': '2.0', 'id': body['id'], 'result': result})

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    http.daemon_threads = True
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{http.server_port}'
    issuer = base+'/tenant'
    records = Records(ConnectorVault())
    service = None
    owned = []
    owned_refs = []

    def rejected(method, **params):
        try:
            service.rpc(method, **params)
        except RuntimeError as error:
            assert all(value not in str(error) for value in values.values())
            return
        raise AssertionError('Unsafe OAuth request was accepted')

    def scan_private_files():
        for path in state['home'].rglob('*'):
            if path.is_file():
                data = path.read_bytes()
                assert all(value.encode() not in data for value in values.values()), path.name
        assert all(value not in json.dumps(service.events) for value in values.values())

    try:
        # Keep public recovery references when native cleanup fails. A failed
        # validation must not erase the owned DB needed for owner-identity cleanup.
        fixture = tempfile.TemporaryDirectory(prefix='pebble-oauth-interop-', delete=False)
        with fixture as temporary:
            state['home'] = Path(temporary).resolve()/'data'
            progress('fixture home '+str(state['home']))
            service = Service(command, state['home'])
            try:
                progress('read-only discovery')
                id = service.rpc('extension_add', kind='plugin', name='Owned OAuth interop fixture', content='{"tools":["lookup"]}', url=base+'/mcp')
                owned.append(id)
                discovery = service.rpc('connector_discover_interop', id=id)
                assert discovery['suggested_scopes'] == ['read'] and discovery['scopes_supported'] == ['default:read']
                assert not state['registrations'] and not state['mcp_posts']
                state['challenge'] = 'Bearer scope="read", Scope="write"'
                rejected('connector_discover_interop', id=id)
                state['challenge'] = None
                checks.append('real unauthenticated GET parses multiple quoted challenges, preserves operation scope priority and rejects duplicate poison without posting MCP methods')
                progress('reserve reviewed registration callback')
                preview = service.rpc('connector_preview_registration', id=id, issuer=issuer, requested_scopes=['read'])
                assert preview['requires_native_review'] and not state['registrations'] and not state['mcp_posts']
                assert state['gets'][-3:] == ['/.well-known/oauth-authorization-server/tenant', '/.well-known/openid-configuration/tenant', '/tenant/.well-known/openid-configuration']
                callback = urlsplit(preview['redirect_uri'])
                with socket.socket() as reservation:
                    try:
                        reservation.bind((callback.hostname, callback.port))
                    except OSError:
                        pass
                    else:
                        raise AssertionError('Preview callback was not owned')
                state['expected_digest'] = preview['digest']
                progress('native DCR admission and OS-vault publication')
                approved = service.rpc('connector_register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
                assert len(state['registrations']) == 1
                registration = rows("SELECT * FROM connector_registrations WHERE plugin_id=?", (id,))[0]
                assert registration['state'] == 'registered' and registration['plan_digest'] == preview['digest']
                owned_refs.append(registration['grant_id'])
                # The frozen executable owns its vault ACL. Runner Python must
                # not decrypt or delete its live entries under another identity.
                # Native credential admission after restart verifies the private
                # management token below, through schema echo/redaction.
                if not args.backend:
                    progress('same-source-identity private registration check')
                    secure = records.get(registration['grant_id'])
                    assert secure['values']['registration_access_token'] == values['registration']
                    assert 'client_secret' not in secure['values'] and values['unused-secret'] not in json.dumps(secure)
                assert all(value not in json.dumps(approved) and value not in json.dumps(registration) for value in values.values())
                rejected('connector_register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
                assert len(state['registrations']) == 1
                checks.append('OIDC issuer fallbacks preserve exact issuer; owned callback and one-use native approval journal admit one DCR201 POST before private OS-vault persistence')
                progress('real PKCE callback and token publication')
                started = service.rpc('connector_start', id=id, preview_id=approved['preview_id'])
                query = parse_qs(urlsplit(started['url']).query)
                state['approved_redirect'] = preview['redirect_uri']
                state['pkce'] = query['code_challenge'][0]
                assert query['redirect_uri'] == [preview['redirect_uri']] and query['resource'] == [base+'/mcp']
                wrong = query['redirect_uri'][0]+'?'+urlencode({'state': query['state'][0], 'code': 'fixture-code', 'iss': base+'/wrong-issuer'})
                try:
                    urllib.request.urlopen(wrong, timeout=5)
                except urllib.error.HTTPError as error:
                    assert error.code == 400
                else:
                    raise AssertionError('Mismatched callback issuer was accepted')
                assert not state['tokens']
                correct = query['redirect_uri'][0]+'?'+urlencode({'state': query['state'][0], 'code': 'fixture-code', 'iss': issuer})
                with urllib.request.urlopen(correct, timeout=12) as response:
                    assert response.status == 200
                assert len(state['tokens']) == 1 and service.rpc('connector_metadata', id=id)['state'] == 'connected'
                grant = service.rpc('connector_metadata', id=id)['grant_id']
                owned_refs.append(grant)
                if not args.backend:
                    assert records.get(grant)['values']['access_token'] == values['access']
                assert not service.rpc('extensions')[0]['enabled']
                checks.append('real callback rejects wrong issuer, then verifies S256 and resource-bound code exchange on the exact registration redirect; sign-in leaves tools disabled')
                scan_private_files()
                progress('restart with native owner of private OS-vault records')
                service.close()
                service = Service(command, state['home'])
                assert Path(service.rpc('state')['data_dir']).resolve() == state['home']
                assert service.rpc('connector_metadata', id=id)['state'] == 'connected'
                assert service.rpc('connector_discover_interop', id=id)['registered_clients'][0]['client_id'] == 'owned-public-client'
                assert len(state['registrations']) == 1 and not state['mcp_posts']
                # Explicit native enablement permits only fixture initialization
                # and catalogue reads. credential() reloads both OAuth and DCR
                # private records under the frozen owner identity. Echoed DCR
                # material must be scrubbed from its published catalogue.
                progress('approved native catalogue read verifies owner-only vault reload and private token redaction')
                state['mcp_allowed'] = True
                service.rpc('extension_toggle', id=id, enabled=True)
                published = service.rpc('extensions')
                assert '[connector credential redacted]' in json.dumps(published)
                assert all(value not in json.dumps(published) for value in values.values())
                assert state['mcp_posts'] and not state['unapproved_mcp_posts']
                service.rpc('extension_toggle', id=id, enabled=False)
                state['mcp_allowed'] = False
                scan_private_files()
                checks.append('actual service restart preserves issuer-bound client/OAuth state; approved native catalogue admission reloads private OS-vault records under their owning identity and scrubs the echoed registration token')
                progress('lost registration response and durable unknown outcome')
                lost = service.rpc('extension_add', kind='plugin', name='Lost registration fixture', content='{"tools":["lookup"]}', url=base+'/mcp')
                owned.append(lost)
                lost_preview = service.rpc('connector_preview_registration', id=lost, issuer=issuer, requested_scopes=['read'])
                state['expected_digest'] = lost_preview['digest']
                state['mode'] = 'lost-registration'
                rejected('connector_register_client', id=lost, preview_id=lost_preview['preview_id'], digest=lost_preview['digest'])
                assert len(state['registrations']) == 2
                interrupted = rows('SELECT state,grant_id FROM connector_registrations WHERE plugin_id=?', (lost,))[0]
                assert interrupted['state'] == 'unknown'
                owned_refs.append(interrupted['grant_id'])
                service.close()
                service = Service(command, state['home'])
                progress('restart rejects unknown registration replay')
                rejected('connector_preview_registration', id=lost, issuer=issuer, requested_scopes=['read'])
                rejected('connector_register_client', id=lost, preview_id=lost_preview['preview_id'], digest=lost_preview['digest'])
                assert len(state['registrations']) == 2 and not state['unapproved_mcp_posts']
                assert service.rpc('connector_discover_interop', id=lost)['unresolved_registrations'][0]['state'] == 'unknown'
                checks.append('lost registration response remains durably unknown across service restart and blocks both old approval replay and a blind new registration review')
                state['mode'] = 'normal'
                progress('native owner disconnect and secure cleanup')
                disconnected = service.rpc('connector_disconnect', id=id)
                assert disconnected['remote_revocation_confirmed'] and not disconnected['storage_cleanup_pending']
                assert state['revocations'] == 1 and not records.vault.get(grant+':manifest')
                assert not records.vault.get(registration['grant_id']+':manifest')
                assert rows('SELECT grant_id FROM connector_registrations WHERE plugin_id=?', (id,))[0]['grant_id'] is None
                scan_private_files()
                checks.append('local disconnect and empty200 revocation securely clear both OAuth and registration OS-vault records; private fields are absent from RPC, event and application-file scans')
            finally:
                progress('final owner-only fixture cleanup')
                state['mode'] = 'normal'
                cleanup_failed = []
                if service:
                    for ident in owned:
                        try:
                            assert not service.rpc('connector_disconnect', id=ident)['storage_cleanup_pending']
                        except Exception:
                            cleanup_failed.append(ident)
                    service.close()
                    service = None
                # Same-source identity can recover its own leftovers. Frozen
                # live entries are cleaned exclusively by the native owner above.
                if not args.backend:
                    for reference in owned_refs:
                        if reference:
                            records.delete(reference)
                assert not cleanup_failed
                progress('verify absence of fixture-owned records after native cleanup')
                assert all(not records.vault.get(reference+':manifest') for reference in owned_refs if reference)
                fixture.cleanup()
    finally:
        if service:
            service.close()
        http.shutdown()
        http.server_close()
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source',
        'passed': True, 'checks': checks, 'registration_posts': len(state['registrations']),
        'unapproved_mcp_posts': state['unapproved_mcp_posts'],
        'approved_mcp_posts': len(state['mcp_posts'])-state['unapproved_mcp_posts'], 'token_exchanges': len(state['tokens']),
        'private_vault_verification': 'native owner reload and private schema redaction; no runner reads of live frozen records' if args.backend else 'same-source-identity record inspection plus native owner reload/redaction',
        'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
        'limits': 'Owned loopback servers and synthetic credentials with actual macOS connector OS vault. No real account, external registration, public client metadata hosting, installed desktop or other-platform claim. Owned fixture grants securely removed.'}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2)+'\n')
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
