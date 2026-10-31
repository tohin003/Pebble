"""Phase 46 (D166): OpenAI's Fast tier on the ChatGPT sign-in, the person's `fast_replies` setting (runner.fast_now;
responses.complete_responses sends service_tier).

Measured first (dev, gpt-6.1-sol): 'long_tasks' (tree 24q: every request of a follow-up message's run and a run's 4th
request on) moved the dev p95 from 75.2 s to 48.8, 23 of 23 passed. On 2026-10-28 the route refused service_tier
'fast' (HTTP 400), applied 'priority' (copying about 1.4x faster) and reported every reply as served 'default', and the
plan meter Codex reads counted 'priority' requests at the Standard rate. Fast's API price is twice Standard's.
"""
import threading
import time

import pytest

from pebble import responses, runner, settings_file
from pebble.app import App
from pebble.responses import complete_responses
from test_chatgpt_session_header_37 import CHATGPT, MESSAGES, Capture, Probe
from test_context_cost_24 import scripted_app


def asked(profile, tier):
    probe = Probe('chat-1')
    probe.service_tier = tier
    try:
        complete_responses(probe, profile, MESSAGES, [], threading.Event(), [].append, 'medium')
    except Capture:
        pass
    return probe.sent[-1][1], probe.last_timing


@pytest.mark.parametrize('setting, follow_up, turn, fast', [
    ('off', True, 9, False), ('long_tasks', False, 0, False), ('long_tasks', False, 2, False),
    ('long_tasks', False, 3, True), ('long_tasks', True, 0, True), ('always', False, 0, True), ('nonsense', True, 9, False),
    ('follow_ups', False, 0, False), ('follow_ups', False, 9, False), ('follow_ups', True, 0, True), (None, True, 9, False)])
def test_which_requests_ask_for_the_fast_tier(setting, follow_up, turn, fast):
    assert runner.fast_now(setting, follow_up, turn) is fast


def test_only_the_chatgpt_route_above_8k_asks_for_it_and_by_the_name_the_route_accepts():
    body, timing = asked(CHATGPT, 'priority')
    assert body['service_tier'] == 'priority' and timing['tier'] == 'fast'  # 'fast' itself is refused there
    body, timing = asked(CHATGPT, None)
    assert 'service_tier' not in body and 'tier' not in timing
    for small in (8192, 4096):  # 4K and 8K request bodies are unchanged
        assert 'service_tier' not in asked({**CHATGPT, 'budget': small, 'context': small}, 'priority')[0]
    other = {**CHATGPT, 'kind': 'responses', 'url': 'https://llm.example.test/v1'}
    assert 'service_tier' not in asked(other, 'priority')[0]  # an API key's route: the person's own bill, untouched


def test_the_tier_the_reply_reports_is_kept_with_the_attempt():
    state, timing = responses._Pass(), {'first_token': None}
    handle = responses._handler(state, timing, time.monotonic(), [].append, None, 'fixture', False)
    handle({'type': 'response.completed', 'response': {'status': 'completed', 'output': [], 'service_tier': 'default',
                                                        'usage': {'input_tokens': 3, 'output_tokens': 1}}})
    assert timing['served_tier'] == 'default' and state.completed
    assert {'tier', 'served_tier'} <= set(runner.ATTEMPT_TIMING)


def test_the_setting_is_a_validated_person_setting_defaulting_to_long_tasks(tmp_path):
    assert settings_file.default('fast_replies') == 'long_tasks' and 'fast_replies' in settings_file.KEYS
    assert settings_file.schema()['properties']['fast_replies']['enum'] == list(settings_file.FAST)
    for bad in ('sometimes', '', None, True, ['always']):
        with pytest.raises(ValueError):
            settings_file.validate('fast_replies', bad)
    assert settings_file.widening('fast_replies', 'off', 'always') == []  # speed, not access: no confirmation
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        assert app.state()['settings']['fast_replies'] == 'long_tasks'
        assert app.dispatch('setting', {'key': 'fast_replies', 'value': 'off'}) is True
        assert app.state()['settings']['fast_replies'] == 'off'
        with pytest.raises(ValueError):
            app.dispatch('setting', {'key': 'fast_replies', 'value': 'turbo'})
        assert app.state()['settings']['fast_replies'] == 'off'
    finally:
        app.close()


def test_a_run_asks_per_request_and_clears_the_tier_when_it_ends(tmp_path):
    app = scripted_app(tmp_path, 32768)
    seen = []

    def complete(messages, tools, cancel, token, deliberate=False):
        seen.append(getattr(app.model, 'service_tier', None))
        token('Hello.')
        return {'role': 'assistant', 'content': 'Hello.'}
    app.model.complete = complete
    try:
        chat = app.new_chat()
        runner.run(app, app.chat(chat['id']), 'Say hello.', False, [])
        assert seen == [None] and app.model.service_tier is None  # a first message's short run stays Standard
        runner.run(app, app.chat(chat['id']), 'Again, please.', False, [])
        assert seen == [None, 'priority'] and app.model.service_tier is None  # a follow-up asks; the end clears it
        app.dispatch('setting', {'key': 'fast_replies', 'value': 'off'})
        runner.run(app, app.chat(chat['id']), 'Once more.', False, [])
        assert seen == [None, 'priority', None]
    finally:
        app.active = app.active_run = None
        app.close()
