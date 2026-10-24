"""Phase 37: on the ChatGPT route a run's opaque cache key also goes in a session_id header.

Measured before the change: on the sol claim run's ChatGPT backend, Codex's input was 89.7 % cached and Pebble's 0.0 %
(1.0.4, and again 1.4.0 in the D71 check), although Pebble's requests above 8K extend each other. The route's own
client (Codex) names its session in a session_id header; Pebble now sends its run id there too, beside the
prompt_cache_key it already sent. Whether the backend's cache routes by it is measured on the next live check.
"""
import threading

from pebble.responses import complete_responses, request_headers


class Capture(Exception):
    pass


class Probe:
    """The provider surface complete_responses uses; it keeps the headers and body and sends nothing."""
    last_usage = None

    def __init__(self, cache_key):
        self.prompt_cache_key, self.sent = cache_key, []

    def key(self, profile):
        return 'fixture'

    def headers(self, kind, key):
        return {'Authorization': 'Bearer ' + key}

    def request(self, url, headers, body, timeout=None):
        self.sent.append((headers, body))
        raise Capture()

    def project_context(self, messages, tools, budget, counter=None, method=None):
        return messages


CHATGPT = {'id': 'p37', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex', 'model': 'gpt-6-luna',
           'context': 1050000, 'budget': 32768, 'tools': True}
MESSAGES = [{'role': 'user', 'content': 'Hello.', '_pebble_current': True}]


def sent(profile, cache_key):
    probe = Probe(cache_key)
    try:
        complete_responses(probe, profile, MESSAGES, [], threading.Event(), [].append, 'medium')
    except Capture:
        pass
    return probe.sent[-1]


def test_a_chatgpt_run_names_its_cache_key_in_session_id():
    headers, body = sent(CHATGPT, 'run-0123abcd')
    assert body['prompt_cache_key'] == 'run-0123abcd' and headers['session_id'] == 'run-0123abcd'
    assert headers['Authorization'] == 'Bearer fixture'


def test_no_cache_key_no_header_and_other_routes_unchanged():
    headers, body = sent(CHATGPT, None)
    assert 'prompt_cache_key' not in body and 'session_id' not in headers
    small = {**CHATGPT, 'budget': 4096}  # the cache key is sent above 4K only, and the header with it
    headers, body = sent(small, 'run-0123abcd')
    assert 'prompt_cache_key' not in body and 'session_id' not in headers
    other = {**CHATGPT, 'kind': 'openai', 'url': 'https://api.openai.invalid/v1'}
    assert 'session_id' not in request_headers(Probe(None), other, 'k', {'prompt_cache_key': 'run-0123abcd'})


def test_a_malformed_key_never_reaches_a_header():
    assert 'session_id' not in request_headers(Probe(None), CHATGPT, 'k', {'prompt_cache_key': 'bad key\r\nX: y'})
