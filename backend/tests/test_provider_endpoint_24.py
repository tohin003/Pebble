"""Phase 24 (parent): the API base URL field accepts a pasted request URL, as provider docs show it.

0.6.1 kept https://api.anthropic.com/v1/messages as the base, so model discovery asked for
https://api.anthropic.com/v1/messages/models (404) and the person saw no models.
"""
import pytest

from pebble.providers import endpoint


@pytest.mark.parametrize('url, base', [
    ('https://api.anthropic.com/v1/messages', 'https://api.anthropic.com/v1'),
    ('https://api.anthropic.com/v1/messages/', 'https://api.anthropic.com/v1'),
    ('https://api.anthropic.com/v1', 'https://api.anthropic.com/v1'),
    ('https://api.anthropic.com', 'https://api.anthropic.com/v1'),
    ('https://api.anthropic.com/messages', 'https://api.anthropic.com/v1'),
    ('https://api.openai.com/v1/chat/completions', 'https://api.openai.com/v1'),
    ('https://api.openai.com/v1/responses', 'https://api.openai.com/v1'),
    ('https://api.openai.com/v1/models', 'https://api.openai.com/v1'),
    ('http://localhost:1234/v1/chat/completions', 'http://localhost:1234/v1'),
    ('http://localhost:1234', 'http://localhost:1234/v1'),
    ('https://example.com/openai/v1/completions', 'https://example.com/openai/v1'),
])
def test_a_pasted_request_url_becomes_the_api_base(url, base):
    assert endpoint(url)[0] == base


def test_only_a_trailing_request_path_is_removed():
    # A path segment that merely contains a request word is a different base and stays as typed.
    assert endpoint('https://example.com/messages-api/v1')[0] == 'https://example.com/messages-api/v1'
    assert endpoint('https://example.com/v1/messagesx')[0] == 'https://example.com/v1/messagesx'


@pytest.mark.parametrize('url', ['http://example.com/v1/messages', 'https://user:key@example.com/v1/messages',
                                 'https://api.anthropic.com/v1/messages?key=a'])
def test_the_url_rules_still_apply_to_a_pasted_request_url(url):
    with pytest.raises(ValueError):
        endpoint(url)
