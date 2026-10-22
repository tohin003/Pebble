"""Owned source/frozen RPC proof for revisioned forms, static views and mods.

Uses a temporary app home and synthetic loopback provider. No accounts, native
credential RPCs, production builds or real external effects are required.
"""

import argparse
import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT, Service


TABLE = {'columns': [{'id': 'scope', 'label': 'Scope'}], 'rows': [{'scope': 'project'}]}
FORM = {'fields': [{'id': 'scope', 'label': 'Which scope?', 'type': 'choice', 'required': True,
                    'options': [{'id': 'project', 'label': 'This project'}, {'id': 'all', 'label': 'All projects'}]}]}
STATIC = {'html': '<h1>Owned static fixture</h1><script>rpcFake()</script><button onclick="rpcFake()">Approve</button><img src="https://example.invalid/pixel">',
          'css': 'h1{color:#334455}'}


def rejected(service, method, **params):
    try:
        service.rpc(method, **params)
    except RuntimeError as error:
        return str(error)
    raise AssertionError('Rejected request was admitted: ' + method)


def settled(service, cid, rid, expected, seconds=20, minimum_steps=1):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        run = service.rpc('run_get', id=cid, run_id=rid)
        if not service.rpc('state')['active']:
            # A native answer wakes the controller asynchronously. The old
            # waiting_input state can still be visible before that queued turn
            # starts; it cannot prove a new form revision has settled.
            if run['state'] == expected and run['steps'] >= minimum_steps:
                return run
            if run['state'] in {'failed', 'budget_exhausted', 'stopped', 'interrupted'}:
                raise AssertionError({'expected': expected, 'state': run['state'], 'error': run.get('error'), 'tools': run.get('tools')})
        time.sleep(.03)
    raise AssertionError('Run did not settle within the fixture bound: ' + expected)


def artifact_id(messages):
    found = []
    for message in messages:
        if message.get('role') != 'tool' or not isinstance(message.get('content'), str):
            continue
        try:
            item = json.loads(message['content'])
        except ValueError:
            continue
        if isinstance(item, dict) and item.get('kind') == 'form' and item.get('id'):
            found.append(item)
    assert found, 'Provider lacked its exact form artifact receipt.'
    return max(found, key=lambda item: item['revision'])['id']


def tool(name, ident, args, index=0):
    return {'index': index, 'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def native_mod(service, source, name):
    value = {'version': 1, 'name': name, 'events': ['manual'], 'capabilities': ['ui.render'], 'source': source}
    review = service.rpc('mod_preview', value=value)
    assert review['manifest'] == value and review['digest'] and not review['enabled']
    installed = service.rpc('mod_install', value=review['manifest'], digest=review['digest'])
    assert not installed['enabled'] and installed['grant_digest'] is None
    return service.rpc('mod_toggle', id=installed['id'], enabled=True, digest=installed['digest'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT / 'backend/main.py')]
    state = {'mode': 'forms', 'steps': {}, 'requests': [], 'errors': [], 'mod': None}
    checks = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'].append(body)
            mode = state['mode']
            step = state['steps'][mode] = state['steps'].get(mode, 0) + 1
            try:
                if mode in {'forms', 'cancel-form'} and step == 1:
                    assert 'UIRender' in {t['function']['name'] for t in body['tools']}, 'Required native form tool was not offered.'
                if mode == 'forms' and step == 1:
                    calls = [tool('UIRender', 'form', {'title': 'Scope form', 'kind': 'form', 'data': json.dumps(FORM)}),
                             tool('UIRender', 'sibling', {'title': 'Planned sibling', 'kind': 'diagram', 'data': json.dumps({'nodes': [{'id': 'a', 'label': 'Scoped'}], 'edges': []})}, 1)]
                elif mode == 'forms' and step == 2:
                    assert 'human-origin' in json.dumps(body['messages'])
                    calls = [tool('UIUpdate', 'revised-form', {'id': artifact_id(body['messages']), 'revision': 1,
                             'title': 'Confirm scope', 'kind': 'form', 'data': json.dumps({**FORM, 'description': 'Confirm this ordinary task choice.'})})]
                elif mode == 'forms' and step == 3:
                    calls = [tool('UIUpdate', 'scope-table', {'id': artifact_id(body['messages']), 'revision': 2,
                             'title': 'Chosen scope', 'kind': 'table', 'data': json.dumps(TABLE)}),
                             tool('UIRender', 'static', {'title': 'Static review', 'kind': 'html', 'data': json.dumps(STATIC)}, 1)]
                elif mode in {'mod', 'grantdrop', 'loop'} and step == 1:
                    spec = next(t for t in body['tools'] if t['function']['name'] == 'RenderMod')
                    assert state['mod']['id'] in spec['function']['parameters']['properties']['id']['enum']
                    calls = [tool('RenderMod', mode, {'id': state['mod']['id'], 'input': json.dumps({'count': 7})})]
                elif mode == 'invalid' and step == 1:
                    calls = [tool('UIRender', 'invalid-table', {'title': 'Invalid schema', 'kind': 'table', 'data': json.dumps({**TABLE, 'origin': 'human'})})]
                elif mode == 'permission' and step == 1:
                    assert 'UIRender' not in {t['function']['name'] for t in body['tools']}
                    calls = [tool('UIRender', 'disabled-view', {'title': 'Disabled schema', 'kind': 'table', 'data': json.dumps(TABLE)})]
                elif mode == 'cancel-form' and step == 1:
                    calls = [tool('UIRender', 'cancel-form', {'title': 'Cancelable form', 'kind': 'form', 'data': json.dumps(FORM)})]
                else:
                    calls = []
                delta = {'tool_calls': calls} if calls else {'content': 'Completed the owned phase seven fixture.'}
            except Exception as error:
                state['errors'].append(type(error).__name__ + ': ' + str(error)[:300])
                delta = {'content': 'Provider fixture assertion failed.'}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\ndata: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-phase7-smoke-') as directory:
            home = Path(directory)
            service = Service(command, home)
            try:
                assert not service.rpc('mods')['items']
                runtime = service.rpc('mods')['runtime']
                assert runtime['available'] and runtime['platform'] == 'macos', 'Actual confined mod proof requires the supported macOS runtime.'
                example = service.rpc('mod_example')
                assert example['capabilities'] == ['ui.render'] and not service.rpc('mods')['items']
                checks.append('native SDK example remains uninstalled and runtime reports confined macOS capability')
                service.rpc('model_save', name='Owned Phase 7 fixture', url=f'http://127.0.0.1:{server.server_port}', model='phase7-fixture', context=32768)
                cid = service.rpc('new_chat')['id']
                rid = service.rpc('send', id=cid, text='Collect ordinary choices and revise the scoped artifact')['run_id']
                run = settled(service, cid, rid, 'waiting_input')
                assert state['steps']['forms'] == 1 and len(run['tools']) == 2
                assert run['tools'][0]['state'] == 'completed' and run['tools'][1]['state'] == 'planned'
                first = service.rpc('get_chat', id=cid)['artifacts'][0]
                assert first['kind'] == 'form' and first['owner_run_id'] == rid and first['revision'] == 1
                qid = first['form_question']['id']
                checks.append('UIRender form suspends the same run and leaves sibling effect planned')
                assert 'stale' in rejected(service, 'artifact_submit', chat_id=cid, id=first['id'], revision=2, answers={'scope': {'option_id': 'project'}}).lower()
                other = service.rpc('new_chat')['id']
                rejected(service, 'artifact_get', chat_id=other, id=first['id'])
                rejected(service, 'artifact_submit', chat_id=other, id=first['id'], revision=1, answers={'scope': {'option_id': 'project'}})
                checks.append('stale and cross-conversation native form capabilities fail before delivery')
                service.close()
                service = Service(command, home)
                restored = service.rpc('artifact_get', chat_id=cid, id=first['id'])
                assert restored['form_question']['id'] == qid and restored['form_question']['status'] == 'pending'
                assert service.rpc('run_get', id=cid, run_id=rid)['state'] == 'waiting_input'
                checks.append('required artifact question and revision restore across real service restart')
                service.rpc('artifact_submit', chat_id=cid, id=first['id'], revision=1, answers={'scope': {'option_id': 'project'}})
                second_run = settled(service, cid, rid, 'waiting_input', minimum_steps=2)
                revised = service.rpc('artifact_get', chat_id=cid, id=first['id'])
                assert revised['revision'] == 2 and revised['form_question']['id'] != qid
                assert second_run['questions'][0]['status'] == 'answered' and second_run['questions'][1]['status'] == 'pending'
                assert any('human-origin' in json.dumps(body['messages']) for body in state['requests'][1:])
                checks.append('native answer resumes original owner and UIUpdate binds a fresh required question')
                service.rpc('artifact_submit', chat_id=cid, id=first['id'], revision=2, answers={'scope': {'text': 'Keep this project scope'}})
                complete = settled(service, cid, rid, 'completed')
                assert state['steps']['forms'] == 4 and complete['id'] == rid
                history = service.rpc('artifact_history', chat_id=cid, id=first['id'])
                assert [r['kind'] for r in history] == ['form', 'form', 'table'] and [r['revision'] for r in history] == [1, 2, 3]
                assert all(r['created'] == history[0]['created'] for r in history)
                assert all(q['status'] == 'answered' for q in complete['questions'])
                checks.append('two human inputs settle to a table with immutable form history and original timestamps')
                rejected(service, 'artifact_submit', chat_id=cid, id=first['id'], revision=1, answers={'scope': {'option_id': 'project'}})
                current = service.rpc('artifact_get', chat_id=cid, id=first['id'])
                stopped = service.rpc('artifact_stop', chat_id=cid, id=first['id'], revision=current['revision'])
                assert stopped['status'] == 'stopped' and stopped['revision'] == 4
                assert service.rpc('artifact_get', chat_id=cid, id=first['id'], revision=1)['kind'] == 'form'
                checks.append('native stop appends a terminal revision while historical inspection remains available')
                html = next(a for a in service.rpc('get_chat', id=cid)['artifacts'] if a['kind'] == 'html')
                page = service.rpc('artifact_view_open', chat_id=cid, id=html['id'], revision=html['revision'])
                with urllib.request.urlopen(page['url'], timeout=3) as response:
                    document = response.read().decode()
                    csp = response.headers['Content-Security-Policy']
                assert all(term in csp for term in ("connect-src 'none'", "frame-src 'none'", "form-action 'none'", 'sandbox allow-scripts'))
                assert 'Owned static fixture' in document and all(term not in document for term in ('rpcFake', '<button', '<img', 'example.invalid'))
                rejected(service, 'view_stage', html='<p>unsaved arbitrary view</p>', script='nativeRpc()')
                checks.append('saved static HTML strips executable/approval/resource elements and exposes strict CSP without a host channel')
                service.rpc('artifact_stop', chat_id=cid, id=html['id'], revision=html['revision'])
                try:
                    urllib.request.urlopen(page['url'], timeout=3)
                except urllib.error.HTTPError as error:
                    assert error.code == 404
                else:
                    raise AssertionError('Stopped view URL remained live.')
                checks.append('native artifact stop revokes its exact static preview URL')

                state['mode'] = 'mod'
                source = 'pebble.on("manual",function(p){pebble.render({id:"counts",title:"Reviewed counts",kind:"table",data:{columns:[{id:"count",label:"Count"}],rows:[{count:p.count}]}});});'
                mod = state['mod'] = native_mod(service, source, 'Owned table mod')
                rejected(service, 'mod_toggle', id=mod['id'], enabled=True, digest='0' * 64)
                checks.append('full source review installs disabled and native enable pins the exact immutable digest')
                mod_cid = service.rpc('new_chat')['id']
                mod_rid = service.rpc('send', id=mod_cid, text='Present the reviewed mod count')['run_id']
                mod_run = settled(service, mod_cid, mod_rid, 'completed')
                assert mod_run['config']['mods'] == [{'id': mod['id'], 'digest': mod['digest'], 'grant_digest': mod['digest']}]
                mod_artifact = service.rpc('get_chat', id=mod_cid)['artifacts'][0]
                assert mod_artifact['kind'] == 'table' and mod_artifact['data']['rows'] == [{'count': 7}]
                assert mod_run['tools'][0]['result']['authorization'] == 'none'
                checks.append('real JavaScriptCore worker publishes only validated owned native data through source/frozen entry point')
                service.rpc('mod_toggle', id=mod['id'], enabled=False, digest=mod['digest'])

                for mode in ('invalid', 'permission'):
                    state['mode'] = mode
                    policy = copy.deepcopy(service.rpc('state')['settings']['permissions'])
                    if mode == 'permission':
                        disabled = copy.deepcopy(policy); disabled['tools']['views'] = False
                        service.rpc('setting', key='permissions', value=disabled)
                    bad_cid = service.rpc('new_chat')['id']
                    bad_rid = service.rpc('send', id=bad_cid, text='Exercise the explicit view boundary')['run_id']
                    bad_run = settled(service, bad_cid, bad_rid, 'completed')
                    assert not service.rpc('get_chat', id=bad_cid)['artifacts']
                    result = bad_run['tools'][0].get('result')
                    wire_errors = [json.loads(m['content']).get('error') for m in state['requests'][-1]['messages']
                                   if m.get('role') == 'tool' and isinstance(m.get('content'), str)]
                    assert isinstance(result, dict) and result.get('error') or any(wire_errors)
                    if mode == 'permission':
                        service.rpc('setting', key='permissions', value=policy)
                checks.append('invalid generated authority/schema fields and disabled view tools publish no artifact')

                state['mode'] = 'grantdrop'
                slow = source.replace('function(p){', 'function(p){const end=Date.now()+600;while(Date.now()<end){};')
                revoked = state['mod'] = native_mod(service, slow, 'Owned revocation fixture')
                revoked_cid = service.rpc('new_chat')['id']
                revoked_rid = service.rpc('send', id=revoked_cid, text='Run the revocable presentation')['run_id']
                deadline = time.monotonic() + 5
                admitted = False
                while time.monotonic() < deadline:
                    with sqlite3.connect(home / 'pebble.sqlite') as database:
                        admitted = bool(database.execute("SELECT 1 FROM mod_dispatches WHERE run_id=? AND mod_id=? AND status='running'", (revoked_rid, revoked['id'])).fetchone())
                    if admitted:
                        break
                    time.sleep(.01)
                assert admitted, 'Revocation fixture never reached durable worker admission.'
                service.rpc('mod_toggle', id=revoked['id'], enabled=False, digest=revoked['digest'])
                revoked_run = settled(service, revoked_cid, revoked_rid, 'completed')
                assert not service.rpc('get_chat', id=revoked_cid)['artifacts']
                assert revoked_run['tools'][0]['result']['dispatches'][0]['reason'] == 'revoked'
                checks.append('native grant revocation during a real worker drops staged output without failing core work')

                state['mode'] = 'loop'
                loop = state['mod'] = native_mod(service, 'pebble.on("manual",function(){while(true){}});', 'Owned timeout fixture')
                loop_cid = service.rpc('new_chat')['id']
                loop_rid = service.rpc('send', id=loop_cid, text='Exercise bounded optional presentation')['run_id']
                loop_run = settled(service, loop_cid, loop_rid, 'completed')
                quarantined = next(item for item in service.rpc('mods')['items'] if item['id'] == loop['id'])
                assert not quarantined['enabled'] and quarantined['grant_digest'] is None and quarantined['error'] in {'timeout', 'memory', 'memory-monitor'}
                assert not service.rpc('get_chat', id=loop_cid)['artifacts']
                assert loop_run['tools'][0]['result']['status'] == 'failed'
                checks.append('infinite-loop worker is terminated and quarantined; the provider task still completes')

                state['mode'] = 'cancel-form'
                canceled_cid = service.rpc('new_chat')['id']
                canceled_rid = service.rpc('send', id=canceled_cid, text='Collect an ordinary choice in a native form for cancellation')['run_id']
                settled(service, canceled_cid, canceled_rid, 'waiting_input')
                canceled = service.rpc('get_chat', id=canceled_cid)['artifacts'][0]
                service.rpc('artifact_stop', chat_id=canceled_cid, id=canceled['id'], revision=1)
                canceled_run = service.rpc('run_get', id=canceled_cid, run_id=canceled_rid)
                assert canceled_run['state'] == 'stopped' and canceled_run['questions'][0]['status'] == 'cancelled'
                rejected(service, 'artifact_submit', chat_id=canceled_cid, id=canceled['id'], revision=1, answers={'scope': {'option_id': 'project'}})
                checks.append('native form stop cancels its required question and dependent run without accepting a late answer')

                prior_attempts = len(state['requests'])
                service.close(); service = Service(command, home)
                assert service.rpc('run_get', id=mod_cid, run_id=mod_rid)['state'] == 'completed'
                assert service.rpc('artifact_get', chat_id=mod_cid, id=mod_artifact['id'])['data_hash'] == mod_artifact['data_hash']
                assert len(state['requests']) == prior_attempts
                assert [r['revision'] for r in service.rpc('artifact_history', chat_id=cid, id=first['id'])] == [1, 2, 3, 4]
                checks.append('settled mod/form histories survive actual restart without worker or provider replay')
                for chat in (cid, other, mod_cid, revoked_cid, loop_cid, canceled_cid):
                    service.rpc('delete_chat', id=chat)
                with sqlite3.connect(home / 'pebble.sqlite') as database:
                    assert not database.execute('SELECT 1 FROM mod_dispatches LIMIT 1').fetchone()
                    assert not database.execute('SELECT 1 FROM artifact_form_links LIMIT 1').fetchone()
                for item in service.rpc('mods')['items']:
                    service.rpc('mod_remove', id=item['id'])
                assert not service.rpc('mods')['items'] and not state['errors'], state['errors']
                checks.append('chat deletion cascades form links and mod admissions; installed mods require separate native removal')
            finally:
                service.close()
    finally:
        server.shutdown(); server.server_close()
    files = ['backend/main.py', 'backend/pebble/app.py', 'backend/pebble/artifacts.py', 'backend/pebble/artifact_schema.py',
             'backend/pebble/artifact_inputs.py', 'backend/pebble/mods.py', 'backend/pebble/mod_worker.py', 'backend/pebble/views.py',
             'backend/pebble/tool_catalogue.py', 'backend/pebble/runner.py', 'backend/pebble/interaction.py',
             'backend/pebble/permissions.py', 'backend/pebble/runtime.py', 'backend/pebble/runtime_commands.py']
    source_hashes = {file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest() for file in files}
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source', 'passed': True,
              'checks': checks, 'synthetic_provider_attempts': len(state['requests']),
              'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else source_hashes['backend/main.py'],
              'source_sha256': source_hashes,
              'limits': 'Owned JSON-RPC/SSE and actual macOS JavaScriptCore confinement fixtures; no real accounts, Keychain authorization, installed native UI, production builds/deployment, Claude ABI compatibility or frontier evaluation.'}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
