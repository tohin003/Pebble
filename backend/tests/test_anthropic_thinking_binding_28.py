"""Phase 28: a multi-step Claude task no longer fails with "Invalid `signature` in `thinking` block".

Anthropic binds each thinking block to everything sent before it, and by default (accounts created since 2026-08-31)
refuses a request whose prefix changed. Pebble rebuilds the first user message every step (the host checkpoint and
the working record), so the Claude benchmark pilot's coding and booking tasks failed on their third request. Until the
layout is append-only (V1-078), requests to api.anthropic.com that carry a thinking configuration ask the API to drop
the blocks whose prefix changed (`block_binding.prefix_mismatch_behavior: "drop_block"`, with the
`thinking-binding-controls-2026-08-01` beta header). Gateways and requests without thinking are unchanged. Nothing here
opens a connection: the transport is a stub that records each request.
"""

import contextlib
import copy
import json

import pytest

from pebble.app import App
from pebble.providers import THINKING_BINDING_BETA, Providers


def thinking_then_tool(tool_name):
    return [{'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'SIG'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'content_block_start', 'index': 1,
             'content_block': {'type': 'tool_use', 'id': 'tu_1', 'name': tool_name, 'input': {}}},
            {'type': 'content_block_delta', 'index': 1,
             'delta': {'type': 'input_json_delta', 'partial_json': json.dumps({'file_path': 'calc.py'})}},
            {'type': 'content_block_stop', 'index': 1},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 5}},
            {'type': 'message_stop'}]


FINAL = [{'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
         {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
         {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'add returns a + b.'}},
         {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 3}},
         {'type': 'message_stop'}]


def run(tmp_path, monkeypatch, url, model='claude-opus-5-5', tool_turns=1):
    sent = []

    @contextlib.contextmanager
    def request(provider, target, headers, body=None, timeout=30):
        sent.append((dict(headers), copy.deepcopy(body)))
        tools = [t['name'] for t in body.get('tools') or []]
        events = thinking_then_tool('Read' if 'Read' in tools else tools[0]) if len(sent) <= tool_turns else FINAL
        yield [b'data: ' + json.dumps(event).encode() + b'\n' for event in events]

    monkeypatch.setattr(Providers, 'request', request)
    monkeypatch.setattr(Providers, 'key', lambda self, p: 'fixture-key')
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        app.store.set_setting('model_profiles', [{
            'id': 'a', 'kind': 'anthropic', 'name': 'Anthropic fixture', 'url': url, 'model': model, 'context': 200_000,
            'budget': 200_000, 'budget_version': 2, 'context_mode': 'full', 'tools': True}])
        app.store.set_setting('active_profile', 'a')
        chat = app.new_chat(str(project))
        app.work(chat, 'Read calc.py and say what add does.', False, [])
    finally:
        app.close()
    return sent


def test_every_request_with_thinking_asks_the_api_to_drop_a_stale_block(tmp_path, monkeypatch):
    sent = run(tmp_path, monkeypatch, 'https://api.anthropic.com/v1', tool_turns=2)
    assert len(sent) >= 3, 'a tool loop of three requests, as in the pilot'
    first = sent[0][1]['messages'][0]
    assert any(body['messages'][0] != first for _, body in sent[1:]), 'the first message still changes per step'
    for headers, body in sent:
        assert body['thinking'] == {'type': 'adaptive', 'block_binding': {'prefix_mismatch_behavior': 'drop_block'}}
        assert headers['anthropic-beta'] == THINKING_BINDING_BETA == 'thinking-binding-controls-2026-08-01'
        assert body['cache_control'] == {'type': 'ephemeral'} and json.dumps(body).count('cache_control') == 1
    # The earlier assistant turn is still sent back exactly as received (the API, not Pebble, drops it if stale).
    replayed = sent[1][1]['messages'][1]['content'][0]
    assert replayed == {'type': 'thinking', 'thinking': '', 'signature': 'SIG'}


@pytest.mark.parametrize('url', ['https://gateway.example.test/v1', 'http://127.0.0.1:9/v1'])
def test_gateways_keep_the_plain_thinking_configuration(tmp_path, monkeypatch, url):
    for headers, body in run(tmp_path, monkeypatch, url):
        assert 'block_binding' not in json.dumps(body) and 'anthropic-beta' not in headers


def test_a_request_without_thinking_sends_no_beta(tmp_path, monkeypatch):
    sent = run(tmp_path, monkeypatch, 'https://api.anthropic.com/v1', model='fixture-chat')
    for headers, body in sent:
        assert 'thinking' not in body and 'anthropic-beta' not in headers
