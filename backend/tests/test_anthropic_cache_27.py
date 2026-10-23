"""Phase 27 (D26): Pebble's Anthropic route asks Anthropic's own API to cache the conversation.

Through 1.0.1 the Messages body carried no cache_control, so every request of a task paid the full input price on the
whole conversation again. One top-level {"type": "ephemeral"} marker turns on the API's automatic caching (the
breakpoint sits on the last cacheable block and moves forward as the conversation grows). Anthropic-compatible
gateways may refuse an unknown field, so only api.anthropic.com gets it, and the usage Pebble records keeps the cache
reads. Nothing here opens a connection: the request is a stub.
"""

import contextlib
import copy
import json

import pytest

from pebble.app import App
from pebble.providers import Providers

EVENTS = [
    {'type': 'message_start', 'message': {'usage': {'input_tokens': 12, 'cache_read_input_tokens': 900,
                                                     'cache_creation_input_tokens': 300}}},
    {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
    {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'fixture reply'}},
    {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 6}},
    {'type': 'message_stop'},
]


def profile(url):
    return {'id': 'anthropic-fixture', 'kind': 'anthropic', 'name': 'Anthropic fixture', 'url': url,
            'model': 'claude-opus-5-5', 'context': 200_000, 'budget': 200_000, 'budget_version': 2,
            'context_mode': 'full', 'tools': True}


def run(tmp_path, monkeypatch, url):
    sent = []

    @contextlib.contextmanager
    def request(provider, target, headers, body=None, timeout=30):
        sent.append((target, copy.deepcopy(body)))
        yield [b'data: ' + json.dumps(event).encode() + b'\n' for event in EVENTS]

    monkeypatch.setattr(Providers, 'request', request)
    monkeypatch.setattr(Providers, 'key', lambda self, p: 'fixture-key')
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        app.store.set_setting('model_profiles', [profile(url)])
        app.store.set_setting('active_profile', 'anthropic-fixture')
        chat = app.new_chat()
        app.work(chat, 'Explain a stack briefly', False, [])
        message = app.chat(chat['id'])['messages'][-1]
    finally:
        app.close()
    return sent, message


def test_anthropic_api_requests_carry_one_automatic_cache_marker(tmp_path, monkeypatch):
    sent, message = run(tmp_path, monkeypatch, 'https://api.anthropic.com/v1')
    assert sent and all(target == 'https://api.anthropic.com/v1/messages' for target, _ in sent)
    for _, body in sent:
        assert body['cache_control'] == {'type': 'ephemeral'}
        # Only the top-level marker: no block-level breakpoint, so the API's limit of four is never reached.
        assert json.dumps(body).count('cache_control') == 1
    assert 'fixture reply' in message['content']
    assert message['usage']['cached_tokens'] == 900 and message['usage']['output_tokens'] == 6


@pytest.mark.parametrize('url', ['https://gateway.example.test/v1', 'http://127.0.0.1:9/v1',
                                 'https://api.anthropic.com.example.test/v1'])
def test_compatible_gateways_get_no_cache_marker(tmp_path, monkeypatch, url):
    sent, message = run(tmp_path, monkeypatch, url)
    assert sent and all('cache_control' not in json.dumps(body) for _, body in sent)
    assert 'fixture reply' in message['content']
