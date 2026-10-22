"""Native input, immutable artifacts, static view revocation and real mod flow."""
import copy
import http.client
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest

from pebble.app import App
from pebble import mod_worker
from pebble.policy import PolicyError
from pebble.views import Views


FORM = {'fields': [{'id': 'scope', 'label': 'Which scope?', 'type': 'choice', 'required': True,
    'options': [{'id': 'small', 'label': 'Small'}, {'id': 'large', 'label': 'Large'}]}]}
DIAGRAM = {'nodes': [{'id': 'a', 'label': 'A'}], 'edges': []}
TABLE = {'columns': [{'id': 'measure', 'label': 'Measure'}, {'id': 'value', 'label': 'Value'}],
    'rows': [{'measure': 'Tokens', 'value': 4}]}
ANSWER = {'scope': {'option_id': 'small'}}


@pytest.fixture
def app(tmp_path):
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield value
    value.active = value.active_run = None
    value.close()


def args(kind, data, title='Native example'):
    return {'title': title, 'kind': kind, 'data': json.dumps(data)}


def call(name, arguments, ident):
    return {'id': ident, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments)}}


def response(*calls):
    return {'role': 'assistant', 'content': None, 'tool_calls': list(calls)}


def final(token):
    token('Finished with the native answer.')
    return {'role': 'assistant', 'content': 'Finished with the native answer.'}


def settled(app, rid, state='completed'):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = app.runs.get(rid)
        if run['state'] == state and app.active is None:
            return run
        time.sleep(.01)
    pytest.fail('Run failed to settle: ' + app.runs.get(rid)['state'])


def request_form(app, sibling=False, goal='Draw a native UI form to clarify scope.'):
    chat, requests = app.new_chat(), []
    first = response(call('UIRender', args('form', FORM), 'form'))
    if sibling:
        first['tool_calls'].append(call('UIRender', args('diagram', DIAGRAM), 'sibling'))
    def complete(messages, tools, cancel, token, effort):
        requests.append(copy.deepcopy(messages))
        return first if len(requests) == 1 else final(token)
    app.model.complete = complete
    app.work(chat, goal, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    row = app.artifact_get(chat['id'], app.artifacts.list(chat['id'])[0]['id'])
    return chat, rid, row, requests


@contextmanager
def owning_run(app, chat=None, rid=None):
    chat = chat or app.new_chat()
    rid = rid or app.runs.create(chat['id'], 'Render native presentation data.', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    previous = app.active, app.active_run
    app.active, app.active_run = chat['id'], rid
    try:
        yield chat, rid
    finally:
        app.active, app.active_run = previous


def fetch(url, host=None):
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        connection.request('GET', parsed.path + (('?' + parsed.query) if parsed.query else ''),
            headers={'Host': host} if host else {})
        result = connection.getresponse()
        return result.status, dict(result.getheaders()), result.read().decode()
    finally:
        connection.close()


def html_artifact(app, html='<p>Snapshot</p>', css='p{color:red}', script=''):
    with owning_run(app) as (chat, rid):
        row = app.execute('UIRender', args('html', {'html': html, 'css': css, 'script': script}), None, chat['id'])
    return chat, rid, row


def test_form_render_is_required_wait_and_sibling_remains_planned(app):
    chat, rid, row, requests = request_form(app, sibling=True)
    assert app.runs.get(rid)['state'] == 'waiting_input' and len(requests) == 1
    assert row['form_question']['required'] is True and row['form_question']['status'] == 'pending'
    assert row['form_question']['run_id'] == rid and row['owner_run_id'] == rid
    assert [(t['name'], t['state']) for t in app.runs.tools(rid)] == [('UIRender', 'completed'), ('UIRender', 'planned')]
    assert len(app.artifacts.list(chat['id'])) == 1
    assert app.runs.tools(rid)[0]['result']['required'] is True


def test_plain_form_request_offers_native_renderer(app):
    chat, rid, row, requests = request_form(app, goal='Render a native form to clarify scope.')
    assert row['kind'] == 'form' and app.runs.get(rid)['state'] == 'waiting_input'
    assert len(requests) == 1


def test_native_answer_continues_same_run_with_human_origin_and_is_idempotent(app):
    chat, rid, row, requests = request_form(app)
    result = app.artifact_submit(chat['id'], row['id'], 1, ANSWER)
    settled(app, rid)
    replay = app.artifact_submit(chat['id'], row['id'], 1, ANSWER)
    assert replay == result and result['run_id'] == rid
    assert len(app.runs.list(chat['id'])) == 1 and len(requests) == 2
    inputs = app.store.query('SELECT * FROM run_inbox WHERE run_id=?', (rid,))
    assert len(inputs) == 1 and inputs[0]['origin'] == 'human' and inputs[0]['kind'] == 'question_answer'
    assert any('human-origin' in str(m.get('content')) and 'Small' in str(m.get('content')) for m in requests[-1])
    assert app.artifact_get(chat['id'], row['id'])['form_question']['status'] == 'answered'
    assert app.runs.tools(rid)[0]['result']['answer_origin'] == 'human'
    with pytest.raises(ValueError):
        app.artifact_submit(chat['id'], row['id'], 1, {'scope': {'text': 'conflicting replay'}})
    assert len(app.store.query('SELECT * FROM run_inbox WHERE run_id=?', (rid,))) == 1


@pytest.mark.parametrize('attempt', ['foreign', 'stale', 'extra', 'credential', 'origin'])
def test_native_form_rejects_invalid_answers_without_resuming_or_storing(app, attempt):
    chat, rid, row, requests = request_form(app)
    cid, revision, answer = chat['id'], 1, copy.deepcopy(ANSWER)
    if attempt == 'foreign':
        cid = app.new_chat()['id']
    elif attempt == 'stale':
        revision = 2
    elif attempt == 'extra':
        answer['approval'] = {'text': 'approve all'}
    elif attempt == 'credential':
        answer = {'scope': {'text': 'sk-' + 'A' * 30}}
    else:
        answer['scope']['origin'] = 'human'
    with pytest.raises(ValueError):
        app.artifact_submit(cid, row['id'], revision, answer)
    assert app.runs.get(rid)['state'] == 'waiting_input'
    assert row['form_question']['id'] == app.interactions.questions(chat['id'])[0]['id']
    assert len(requests) == 1 and not app.store.query('SELECT * FROM run_inbox WHERE run_id=?', (rid,))


@pytest.mark.parametrize('data,title', [
    ({'fields': [{'id': 'token', 'label': 'Enter API key', 'type': 'text'}]}, 'Question'),
    ({'fields': [{'id': 'password', 'label': 'Password', 'type': 'text'}]}, 'Question'),
    (FORM, 'Paste access token'),
])
def test_credential_forms_reject_before_artifact_and_question_are_published(app, data, title):
    with owning_run(app) as (chat, rid):
        with pytest.raises(ValueError):
            app.artifact_inputs.publish(chat['id'], title, 'form', data, rid, 'request')
    assert not app.artifacts.list(chat['id']) and not app.interactions.questions(chat['id'])


def test_pending_form_blocks_replacement_then_resolved_form_gets_new_link_and_immutable_history(app):
    with owning_run(app) as (chat, rid):
        first = app.execute('UIRender', args('form', FORM), None, chat['id'], call_id='form-one')
        with pytest.raises(ValueError, match='Resolve'):
            app.artifact_inputs.publish(chat['id'], 'Replacement', 'diagram', DIAGRAM, rid,
                'blocked-update', first['id'], 1)
        assert len(app.artifacts.history(chat['id'], first['id'])) == 1
        app.artifact_submit(chat['id'], first['id'], 1, ANSWER)
        second_data = {'fields': [{'id': 'detail', 'label': 'What detail?', 'type': 'text', 'required': True}]}
        second = app.execute('UIUpdate', {**args('form', second_data), 'id': first['id'], 'revision': 1},
            None, chat['id'], call_id='form-two')
        assert second['revision'] == 2 and second['question_id'] != first['question_id']
        with pytest.raises(ValueError, match='stale'):
            app.artifact_submit(chat['id'], first['id'], 1, ANSWER)
    old = app.artifact_get(chat['id'], first['id'], 1)
    current = app.artifact_get(chat['id'], first['id'])
    assert old['data'] == FORM and old['form_question']['status'] == 'answered'
    assert current['data'] == second_data and current['form_question']['status'] == 'pending'
    assert [r['revision'] for r in app.artifacts.history(chat['id'], first['id'])] == [1, 2]


def test_native_stop_atomically_cancels_required_question_and_stops_run(app):
    chat, rid, row, requests = request_form(app)
    result = app.artifact_stop(chat['id'], row['id'], 1)
    assert result['status'] == 'stopped' and result['revision'] == 2
    assert app.interactions.question(row['form_question']['id'])['status'] == 'cancelled'
    assert app.runs.get(rid)['state'] == 'stopped' and len(requests) == 1
    assert not app.interactions.required_pending(rid)
    with pytest.raises(ValueError):
        app.artifact_submit(chat['id'], row['id'], 2, ANSWER)
    assert app.artifact_stop(chat['id'], row['id'], 2)['revision'] == 2
    inbox = app.store.query('SELECT kind,origin FROM run_inbox WHERE run_id=?', (rid,))
    assert inbox == [{'kind': 'question_cancelled', 'origin': 'human'}]


def test_form_link_question_and_wait_restore_after_restart(tmp_path):
    home, resources = tmp_path / 'data', tmp_path / 'resources'
    first = App(home, resources, lambda *args: None)
    try:
        chat, rid, row, _ = request_form(first)
    finally:
        first.close()
    second = App(home, resources, lambda *args: None)
    try:
        second.model.complete = lambda m, t, c, token, e: final(token)
        restored = second.artifact_get(chat['id'], row['id'])
        assert restored['data'] == row['data'] and restored['record_hash'] == row['record_hash']
        assert restored['form_question']['id'] == row['form_question']['id']
        assert second.runs.get(rid)['state'] == 'waiting_input'
        second.artifact_submit(chat['id'], row['id'], 1, ANSWER)
        settled(second, rid)
        assert len(second.runs.tools(rid)) == 1
    finally:
        second.close()


def test_native_updates_preserve_history_and_reject_foreign_owner_stale_and_stopped(app):
    with owning_run(app) as (chat, rid):
        first = app.execute('UIRender', args('diagram', DIAGRAM), None, chat['id'], call_id='original')
        second = app.execute('UIUpdate', {**args('table', TABLE), 'id': first['id'], 'revision': 1},
            None, chat['id'], call_id='replacement')
        for cid, owner, revision in [(app.new_chat()['id'], rid, 2), (chat['id'], 'foreign-run', 2),
                                     (chat['id'], rid, 1)]:
            with pytest.raises(ValueError):
                app.artifact_inputs.publish(cid, 'Forged', 'diagram', DIAGRAM, owner, 'bad', first['id'], revision)
    assert app.artifacts.get(chat['id'], first['id'], 1)['data'] == DIAGRAM
    assert app.artifacts.get(chat['id'], first['id'], 2)['data'] == TABLE
    assert second['created'] == first['created'] and second['owner_run_id'] == rid
    app.artifact_stop(chat['id'], first['id'], 2)
    with pytest.raises(ValueError, match='stopped'):
        app.artifact_inputs.publish(chat['id'], 'Restart', 'diagram', DIAGRAM, rid, 'bad', first['id'], 3)
    assert len(app.artifacts.history(chat['id'], first['id'])) == 3


def test_native_submit_rechecks_view_permission(app):
    chat, rid, row, _ = request_form(app)
    policy = app.store.setting('permissions'); policy['tools']['views'] = False
    app.setting('permissions', policy)
    with pytest.raises(PolicyError):
        app.artifact_submit(chat['id'], row['id'], 1, ANSWER)
    assert not app.store.query('SELECT * FROM run_inbox WHERE run_id=?', (rid,))
    # Native stop stays available to cancel a required question after revocation.
    assert app.artifact_stop(chat['id'], row['id'], 1)['status'] == 'stopped'


class Tags(HTMLParser):
    def __init__(self):
        super().__init__(); self.elements = []
    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def test_static_preview_strips_active_malformed_html_and_has_no_generated_input_channel(app):
    dangerous = '''<div onclick="fetch('/rpc')"><script>alert('supplied-js')</script>
      <style>@import 'https://bad.test';</style><img src="https://bad.test" onerror="evil()">
      <iframe srcdoc="<script>evil()</script>"></iframe><form action="/rpc"><input name="approval"></form>
      <svg><script>evil()</script></svg><a href="javascript:evil()">Label</a><p class="good">Kept</x></p></div>'''
    chat, _, row = html_artifact(app, dangerous)
    view = app.artifact_view_open(chat['id'], row['id'], 1)
    status, headers, document = fetch(view['url'])
    parsed = Tags(); parsed.feed(document)
    assert status == 200 and 'Kept' in document
    assert not any(tag in {'iframe', 'form', 'input', 'svg', 'img', 'a'} for tag, _ in parsed.elements)
    assert not any(any(k.startswith('on') for k in attributes) for _, attributes in parsed.elements)
    assert len([tag for tag, _ in parsed.elements if tag == 'script']) == 1
    assert all(s not in document for s in ('supplied-js', 'bad.test', 'javascript:', 'postMessage', '/rpc'))
    assert "connect-src 'none'" in headers['Content-Security-Policy']
    assert "form-action 'none'" in headers['Content-Security-Policy']
    assert "default-src 'none'" in headers['Content-Security-Policy']
    assert headers['Cache-Control'] == 'no-store' and headers['X-Content-Type-Options'] == 'nosniff'
    assert fetch(view['url'], host='evil.test')[0] == 404
    assert view['origin'] == 'generated_view' and view['authorization'] == 'none'


def test_static_preview_requires_saved_current_revision_and_rejects_supplied_script(app):
    with pytest.raises(ValueError):
        app.view_stage(html='<p>Unsaved</p>', script='')
    with owning_run(app) as (chat, rid):
        # Phase 19 F8: a model-published custom view is static; its script is refused, never stored.
        with pytest.raises(PolicyError, match='static'):
            app.execute('UIRender', args('html', {'html': '<p>Snapshot</p>', 'script': 'fetch("/rpc")'}), None, chat['id'])
        assert not app.artifacts.list(chat['id'])
        # An earlier stored script still never opens.
        row = app.artifacts.render(chat['id'], 'Legacy view', 'html', {'html': '<p>Legacy</p>', 'script': 'fetch("/rpc")'}, rid)
    with pytest.raises(ValueError, match='scripts'):
        app.artifact_view_open(chat['id'], row['id'], 1)
    assert not app.views.pages
    with pytest.raises(ValueError):
        app.artifact_view_open(app.new_chat()['id'], row['id'], 1)
    with pytest.raises(ValueError):
        app.artifact_view_open(chat['id'], row['id'], 2)


def test_static_css_accepts_hex_colors_without_treating_them_as_geometry(app):
    chat, _, row = html_artifact(app, css='p{color:#223024;background-color:#f4f6ef;padding:12px}')
    opened = app.artifact_view_open(chat['id'], row['id'], 1)
    assert fetch(opened['url'])[0] == 200


@pytest.mark.parametrize('css', [
    'p{background-color:url(https://bad.test)}', '@import "https://bad.test";',
    'p{position:fixed}', 'p{animation-duration:999s}', 'p{width:99999px}',
    'p{width:999vh}', 'p{color:var(--secret)}', 'p{/*hidden*/color:red}',
    'p{color:r\\65d}', 'p{font-size:calc(100px)}',
    'x' * 40001, 'p{color:red;}' * 65, 'p{' + 'color:red;' * 257 + '}',
])
def test_static_css_resource_dynamic_overlay_geometry_and_count_bounds(css):
    views = Views()
    try:
        with pytest.raises(ValueError):
            views.stage('<p>Data</p>', css)
        assert not views.pages and views.server is None
    finally:
        views.close()


@pytest.mark.parametrize('html', ['<div></div>' * 513, '<div>' * 33 + 'Text', 'x' * 60001])
def test_static_html_byte_node_and_depth_bounds(html):
    views = Views()
    try:
        with pytest.raises(ValueError):
            views.stage(html)
        assert not views.pages and views.server is None
    finally:
        views.close()


@pytest.mark.parametrize('change', ['expire', 'stop', 'revision', 'permission', 'paused', 'stopped'])
def test_page_revokes_on_expiry_native_stop_revision_permission_or_run_state(app, change):
    chat, rid, row = html_artifact(app)
    opened = app.artifact_view_open(chat['id'], row['id'], 1)
    assert fetch(opened['url'])[0] == 200
    assert 0 < opened['expires_at'] - time.time() <= 300
    if change == 'expire':
        path = urlsplit(opened['url']).path
        with app.views.lock:
            doc, policy, _, binding = app.views.pages[path]
            app.views.pages[path] = (doc, policy, time.time() - 1, binding)
    elif change == 'stop':
        app.artifact_stop(chat['id'], row['id'], 1)
    elif change == 'revision':
        with owning_run(app, chat, rid):
            app.execute('UIUpdate', {**args('html', {'html': '<p>New revision</p>'}),
                'id': row['id'], 'revision': 1}, None, chat['id'])
    elif change == 'permission':
        policy = app.store.setting('permissions'); policy['tools']['views'] = False
        app.setting('permissions', policy)
    else:
        app.runs.transition(rid, change)
    assert fetch(opened['url'])[0] == 404


@pytest.mark.parametrize('mutation', ['host', 'localhost', 'userinfo', 'https', 'query', 'fragment'])
def test_stop_url_must_match_exact_loopback_origin_without_query_or_fragment(app, mutation):
    chat, _, row = html_artifact(app)
    opened = app.artifact_view_open(chat['id'], row['id'], 1)
    parsed = urlsplit(opened['url'])
    target = {
        'host': opened['url'].replace('127.0.0.1', 'evil.test'),
        'localhost': opened['url'].replace('127.0.0.1', 'localhost'),
        'userinfo': opened['url'].replace('http://', 'http://attacker@'),
        'https': opened['url'].replace('http://', 'https://'),
        'query': opened['url'] + '?stop=all', 'fragment': opened['url'] + '#fragment',
    }[mutation]
    with pytest.raises(ValueError):
        app.views.stop(target)
    assert fetch(opened['url'])[0] == 200
    assert app.views.stop(opened['url']) is True
    assert fetch(opened['url'])[0] == 404


def test_revision_updates_and_reopens_never_reuse_old_page_capability(app):
    chat, rid, row = html_artifact(app)
    first = app.artifact_view_open(chat['id'], row['id'], 1)
    second = app.artifact_view_open(chat['id'], row['id'], 1)
    assert second['url'] != first['url'] and fetch(first['url'])[0] == 404
    with owning_run(app, chat, rid):
        app.execute('UIUpdate', {**args('html', {'html': '<p>Replacement</p>'}), 'id': row['id'], 'revision': 1},
            None, chat['id'])
    assert fetch(second['url'])[0] == 404
    with pytest.raises(ValueError):
        app.artifact_view_open(chat['id'], row['id'], 1)
    third = app.artifact_view_open(chat['id'], row['id'], 2)
    assert fetch(third['url'])[0] == 200 and 'Replacement' in fetch(third['url'])[2]


@pytest.mark.skipif(not mod_worker.supported(), reason='macOS confined JavaScriptCore required')
def test_native_reviewed_mod_executes_real_jsc_through_render_mod_and_updates_owned_pane(app):
    source = '''pebble.on("manual",function(p){
      pebble.render({id:"counter",title:"Counter",kind:"table",data:{
        columns:[{id:"measure",label:"Measure"},{id:"value",label:"Value"}],
        rows:[{measure:"Tokens",value:p.tokens}]}});
    });'''
    manifest = {'version': 1, 'name': 'Counter', 'events': ['manual'], 'capabilities': ['ui.render'], 'source': source}
    review = app.mods.preview(manifest)
    installed = app.mod_operation('install', value=manifest, digest=review['digest'])
    app.mod_operation('toggle', ident=installed['id'], enabled=True, digest=installed['digest'])
    with owning_run(app) as (chat, rid):
        first = app.execute('RenderMod', {'id': installed['id'], 'input': json.dumps({'tokens': 4})},
            None, chat['id'], call_id='mod-one')
        assert first['origin'] == 'mod_presentation' and first['authorization'] == 'none'
        assert len(first['artifacts']) == 1 and first['artifacts'][0]['revision'] == 1
        second = app.execute('RenderMod', {'id': installed['id'], 'input': json.dumps({'tokens': 9})},
            None, chat['id'], call_id='mod-two')
    pane = first['artifacts'][0]['id']
    assert second['artifacts'][0]['id'] == pane and second['artifacts'][0]['revision'] == 2
    assert app.artifacts.get(chat['id'], pane, 1)['data']['rows'][0]['value'] == 4
    assert app.artifacts.get(chat['id'], pane)['data']['rows'][0]['value'] == 9
    assert app.artifacts.get(chat['id'], pane)['owner_run_id'] == rid
    assert not app.interactions.questions(chat['id'])


def install_mod(app, source, name='Reviewed mod'):
    value = {'version': 1, 'name': name, 'events': ['manual'], 'capabilities': ['ui.render'], 'source': source}
    review = app.mods.preview(value)
    installed = app.mod_operation('install', value=value, digest=review['digest'])
    return app.mod_operation('toggle', ident=installed['id'], enabled=True, digest=installed['digest'])


@pytest.mark.skipif(not mod_worker.supported(), reason='macOS confined JavaScriptCore required')
def test_endless_mod_is_quarantined_but_next_required_question_and_same_run_completion_continue(app):
    installed = install_mod(app, 'pebble.on("manual",function(){while(true){}});', 'Endless optional pane')
    chat, requests = app.new_chat(), []
    items = [{'id': 'scope', 'title': 'Which scope?', 'options': [{'id': 'small', 'label': 'Small'}]}]
    def complete(messages, tools, cancel, token, effort):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return response(call('RenderMod', {'id': installed['id'], 'input': '{}'}, 'mod'))
        if len(requests) == 2:
            return response(call('AskUserQuestion', {'questions': json.dumps(items), 'required': True}, 'question'))
        return final(token)
    app.model.complete = complete
    app.work(chat, 'Draw an optional reviewed pane, then clarify scope.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'waiting_input' and len(requests) == 2
    disabled = app.mods.inspect(installed['id'])
    assert disabled['enabled'] is False and disabled['error'] == 'timeout'
    assert not app.artifacts.list(chat['id'])
    question = app.interactions.questions(chat['id'])[0]
    app.question_answer(chat['id'], question['id'], 1, ANSWER)
    settled(app, rid)
    assert len(requests) == 3 and len(app.runs.list(chat['id'])) == 1
    assert [(t['name'], t['state']) for t in app.runs.tools(rid)] == [('RenderMod', 'completed'), ('AskUserQuestion', 'completed')]


@pytest.mark.skipif(not mod_worker.supported(), reason='macOS confined JavaScriptCore required')
def test_mod_disable_while_waiting_preserves_native_answer_automatic_same_run_resume(app):
    installed = install_mod(app, 'pebble.on("manual",function(){});')
    chat, rid, row, requests = request_form(app)
    app.mod_operation('toggle', ident=installed['id'], enabled=False, digest=installed['digest'])
    app.artifact_submit(chat['id'], row['id'], 1, ANSWER)
    settled(app, rid)
    assert len(requests) == 2 and len(app.runs.list(chat['id'])) == 1
    assert app.mods.inspect(installed['id'])['enabled'] is False
    assert app.artifact_get(chat['id'], row['id'])['form_question']['status'] == 'answered'


@pytest.mark.parametrize('tamper', ['missing', 'revision', 'owner', 'items'])
@pytest.mark.parametrize('operation', ['submit', 'stop', 'replace'])
def test_tampered_form_question_capability_cannot_answer_stop_or_replace(app, tamper, operation):
    with owning_run(app) as (chat, rid):
        form = app.execute('UIRender', args('form', FORM), None, chat['id'], call_id='form')
        qid = form['question_id']
        if tamper == 'missing':
            app.store.query('DELETE FROM artifact_form_links WHERE artifact_id=?', (form['id'],))
        elif tamper == 'revision':
            app.store.query('UPDATE run_questions SET revision=2 WHERE id=?', (qid,))
        elif tamper == 'owner':
            app.store.query('UPDATE run_questions SET run_id=? WHERE id=?', ('foreign-run', qid))
        else:
            app.store.query('UPDATE run_questions SET items=? WHERE id=?',
                (json.dumps([{'id': 'other', 'title': 'Other question', 'options': []}]), qid))
        with pytest.raises(ValueError):
            if operation == 'submit':
                app.artifact_submit(chat['id'], form['id'], 1, ANSWER)
            elif operation == 'stop':
                app.artifact_stop(chat['id'], form['id'], 1)
            else:
                app.artifact_inputs.publish(chat['id'], 'Replacement', 'diagram', DIAGRAM, rid, 'replacement', form['id'], 1)
    assert len(app.artifacts.history(chat['id'], form['id'])) == 1
    assert not app.store.query('SELECT * FROM run_inbox WHERE run_id=?', (rid,))


@pytest.mark.parametrize('operation', ['publish', 'answer'])
def test_form_and_native_inbox_disk_failure_roll_back_all_related_state(app, operation):
    with owning_run(app) as (chat, rid):
        if operation == 'publish':
            app.store.query("CREATE TRIGGER reject_link BEFORE INSERT ON artifact_form_links BEGIN SELECT RAISE(ABORT,'disk failure'); END")
            with pytest.raises(sqlite3.IntegrityError, match='disk failure'):
                app.artifact_inputs.publish(chat['id'], 'Question', 'form', FORM, rid, 'form')
            assert not app.artifacts.list(chat['id']) and not app.interactions.questions(chat['id'])
        else:
            form = app.execute('UIRender', args('form', FORM), None, chat['id'], call_id='form')
            app.store.query("CREATE TRIGGER reject_inbox BEFORE INSERT ON run_inbox BEGIN SELECT RAISE(ABORT,'disk failure'); END")
            with pytest.raises(sqlite3.IntegrityError, match='disk failure'):
                app.artifact_submit(chat['id'], form['id'], 1, ANSWER)
            assert app.interactions.question(form['question_id'])['status'] == 'pending'
            assert app.runs.tools(rid)[0]['result']['status'] == 'awaiting_user_input'
            assert not app.store.query('SELECT * FROM messages WHERE chat_id=?', (chat['id'],))
        assert not app.store.query('SELECT * FROM run_inbox WHERE run_id=?', (rid,))
        assert not app.store.db.in_transaction


@pytest.mark.skipif(not mod_worker.supported(), reason='macOS confined JavaScriptCore required')
def test_cancel_during_final_real_mod_observer_still_settles_run_and_dispatch(app, monkeypatch):
    source = 'pebble.on("run.state",function(p){if(p.state==="completed"){while(true){}}});'
    value = {'version': 1, 'name': 'Final optional pane', 'events': ['run.state'], 'capabilities': ['ui.render'], 'source': source}
    review = app.mods.preview(value)
    installed = app.mod_operation('install', value=value, digest=review['digest'])
    app.mod_operation('toggle', ident=installed['id'], enabled=True, digest=installed['digest'])
    chat, entered, timer = app.new_chat(), threading.Event(), None
    actual_run = mod_worker.run
    def observing(source, event, payload, **kwargs):
        nonlocal timer
        if event == 'run.state' and payload['state'] == 'completed':
            entered.set()
            timer = threading.Timer(.12, lambda: app.stop(chat['id']))
            timer.start()
        return actual_run(source, event, payload, **kwargs)
    monkeypatch.setattr(mod_worker, 'run', observing)
    app.model.complete = lambda m, t, c, token, e: final(token)
    try:
        app.work(chat, 'Explain the current display.', False, [])
    finally:
        if timer:
            timer.cancel(); timer.join()
    assert entered.is_set()
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'stopped' and app.active is None
    dispatches = app.store.query('SELECT status,reason FROM mod_dispatches WHERE run_id=? ORDER BY created', (rid,))
    assert dispatches[-1] == {'status': 'cancelled', 'reason': 'cancelled'}
    assert not any(d['status'] == 'running' for d in dispatches)
    states = [e['payload'].get('to') for e in app.runs.events(rid) if e['kind'] == 'state']
    assert states[-1] == 'stopped'
    assert app.runs.handoff(rid) is not None


def test_invalid_table_authority_field_can_be_repaired_without_uncertain_effect(app):
    chat, requests = app.new_chat(), []
    invalid = {**TABLE, 'authorization': 'full'}
    def complete(messages, tools, cancel, token, effort):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return response(call('UIRender', args('table', invalid), 'invalid-table'))
        if len(requests) == 2:
            assert not app.artifacts.list(chat['id'])
            assert not app.interactions.questions(chat['id'])
            return response(call('UIRender', args('table', TABLE), 'correct-table'))
        return final(token)
    app.model.complete = complete
    app.work(chat, 'Render a native artifact with tabular counts.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'completed' and len(requests) == 3
    rows = app.artifacts.list(chat['id'])
    assert len(rows) == 1 and rows[0]['data'] == TABLE and rows[0]['owner_run_id'] == rid
    receipts = app.runs.tools(rid)
    assert receipts[0]['state'] == 'completed' and set(receipts[0]['result']) == {'error'}
    assert receipts[1]['state'] == 'completed' and receipts[1]['result']['id'] == rows[0]['id']
    assert app.store.setting('permissions')['mode'] == 'ask'


def test_invalid_required_form_creates_no_partial_capability_and_corrected_form_waits(app):
    chat, requests = app.new_chat(), []
    invalid = copy.deepcopy(FORM); invalid['fields'][0]['type'] = 'host_action'
    def complete(messages, tools, cancel, token, effort):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return response(call('UIRender', args('form', invalid), 'invalid-form'))
        if len(requests) == 2:
            assert not app.artifacts.list(chat['id'])
            assert not app.interactions.questions(chat['id'])
            assert not app.store.query('SELECT * FROM artifact_form_links')
            return response(call('UIRender', args('form', FORM), 'correct-form'))
        return final(token)
    app.model.complete = complete
    app.work(chat, 'Render a required form widget to clarify scope.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'waiting_input' and len(requests) == 2
    rows = app.artifacts.list(chat['id'])
    assert len(rows) == 1 and len(app.interactions.questions(chat['id'])) == 1
    assert len(app.store.query('SELECT * FROM artifact_form_links')) == 1
    receipts = app.runs.tools(rid)
    assert receipts[0]['state'] == 'completed' and set(receipts[0]['result']) == {'error'}
    assert receipts[1]['state'] == 'completed' and receipts[1]['result']['required'] is True
    app.artifact_submit(chat['id'], rows[0]['id'], 1, ANSWER)
    settled(app, rid)
    assert len(requests) == 3 and len(app.runs.list(chat['id'])) == 1


def test_stale_update_is_resolved_error_and_next_current_cas_updates_same_owner(app):
    chat, requests, aid = app.new_chat(), [], None
    middle, last = copy.deepcopy(TABLE), copy.deepcopy(TABLE)
    middle['rows'][0]['value'] = 7; last['rows'][0]['value'] = 9
    def complete(messages, tools, cancel, token, effort):
        nonlocal aid
        requests.append(copy.deepcopy(messages))
        turn = len(requests)
        if turn == 1:
            return response(call('UIRender', args('table', TABLE), 'create'))
        aid = app.artifacts.list(chat['id'])[0]['id']
        if turn == 2:
            return response(call('UIUpdate', {**args('table', middle), 'id': aid, 'revision': 1}, 'first-update'))
        if turn == 3:
            return response(call('UIUpdate', {**args('table', last), 'id': aid, 'revision': 1}, 'stale-update'))
        if turn == 4:
            assert app.artifacts.get(chat['id'], aid)['revision'] == 2
            assert app.artifacts.get(chat['id'], aid)['data'] == middle
            assert len(app.artifacts.history(chat['id'], aid)) == 2
            return response(call('UIUpdate', {**args('table', last), 'id': aid, 'revision': 2}, 'current-update'))
        return final(token)
    app.model.complete = complete
    app.work(chat, 'Render and update a native table view.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'completed' and len(requests) == 5
    history = app.artifacts.history(chat['id'], aid)
    assert [r['revision'] for r in history] == [1, 2, 3]
    assert [r['data']['rows'][0]['value'] for r in history] == [4, 7, 9]
    assert all(r['owner_run_id'] == rid for r in history)
    receipts = app.runs.tools(rid)
    assert all(r['state'] == 'completed' for r in receipts)
    assert receipts[2]['call_id'] == '3:stale-update' and set(receipts[2]['result']) == {'error'}
    assert receipts[3]['result']['revision'] == 3
