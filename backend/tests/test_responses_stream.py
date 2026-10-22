"""Responses stream reconstruction through the real complete_responses.

The transport is a fixed in-memory SSE fixture shaped like the ChatGPT backend
stream recorded in .runtime/phase10-stream-shapes.json (shapes only): items are
announced, their arguments stream, each item finishes with
response.output_item.done, and response.completed may carry an empty output.
No network, account, credential or file I/O is involved.
"""

import copy
import json
import threading

import pytest

from pebble.recovery import ProviderFailure
from pebble.responses import complete_responses, input_items


PROFILE = {'id': 'fixture', 'name': 'Fixture', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
           'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'tools': True}
SECRET = 'sk-fixture-secret-0001'
TOOLS = [{'type': 'function', 'function': {'name': 'Read', 'description': 'Read a project file.', 'parameters': {
    'type': 'object', 'properties': {'file_path': {'type': 'string'}}, 'required': ['file_path']}}}]
MESSAGES = [{'role': 'user', 'content': 'Read a.py and summarize it.', '_pebble_current': True}]
USAGE = {'input_tokens': 40, 'output_tokens': 8, 'input_tokens_details': {'cached_tokens': 12}}
ARGS = '{"file_path": "a.py"}'


class Stream:
    def __init__(self, lines, on_read):
        self.lines, self.on_read, self.reads, self.closed = list(lines), on_read, 0, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def readline(self, limit):
        line = self.lines.pop(0) if self.lines else b''
        self.reads += 1
        if self.on_read:
            self.on_read(self.reads, line)
        return line


class Provider:
    """The provider surface complete_responses uses, with a fixed transport."""

    def __init__(self, events, on_read=None):
        lines = []
        for event in events:
            lines += [b'data: ' + json.dumps(event).encode() + b'\n', b'\n']
        self.stream, self.requests, self.response = Stream(lines, on_read), [], None
        self.last_usage = {'sentinel': True}

    def key(self, profile):
        return SECRET

    def headers(self, kind, key):
        return {'Authorization': 'Bearer ' + key}

    def request(self, url, headers, body, timeout=None):
        self.requests.append((url, copy.deepcopy(body)))
        return self.stream

    def project_context(self, messages, tools, budget, counter=None, method=None):
        return messages


def run(events, cancel=None, on_read=None):
    provider, tokens = Provider(events, on_read), []
    result = complete_responses(provider, PROFILE, MESSAGES, TOOLS, cancel or threading.Event(), tokens.append, 'medium')
    return result, provider, tokens


def refused(events, match='No tools were executed', kind=ValueError):
    provider, tokens = Provider(events), []
    with pytest.raises(kind, match=match) as caught:
        complete_responses(provider, PROFILE, MESSAGES, TOOLS, threading.Event(), tokens.append, 'medium')
    # Retry-safe failure: no usage published, transport released.
    assert provider.last_usage == {'sentinel': True} and provider.response is None and provider.stream.closed
    return caught.value, tokens


def call(call_id='call_1', item_id='fc_1', arguments=ARGS, name='Read', status='completed'):
    return {'type': 'function_call', 'id': item_id, 'call_id': call_id, 'name': name,
            'arguments': arguments, 'status': status}


def reasoning(item_id='rs_1'):
    return {'type': 'reasoning', 'id': item_id, 'summary': [], 'encrypted_content': 'opaque-' + item_id}


def message(text, item_id='msg_1'):
    return {'type': 'message', 'id': item_id, 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


def added(index, item):
    started = {**item, 'status': 'in_progress'}
    if item['type'] == 'function_call':
        started['arguments'] = ''
    if item['type'] == 'message':
        started['content'] = []
    started.pop('encrypted_content', None)
    return {'type': 'response.output_item.added', 'output_index': index, 'item': started}


def done(index, item):
    return {'type': 'response.output_item.done', 'output_index': index, 'item': item}


def call_events(index, item):
    """The recorded shape: announce, argument deltas, final arguments, finished item."""
    text = item['arguments']
    return [added(index, item),
            {'type': 'response.function_call_arguments.delta', 'output_index': index, 'item_id': item['id'],
             'delta': text[:7], 'obfuscation': 'pad'},
            {'type': 'response.function_call_arguments.delta', 'output_index': index, 'item_id': item['id'],
             'delta': text[7:], 'obfuscation': 'pad'},
            {'type': 'response.function_call_arguments.done', 'output_index': index, 'item_id': item['id'],
             'arguments': text},
            done(index, item)]


def start():
    shell = {'status': 'in_progress', 'output': []}
    return [{'type': 'response.created', 'response': shell}, {'type': 'response.in_progress', 'response': shell}]


def completed(output=(), status='completed'):
    return {'type': 'response.completed', 'response': {'status': status, 'output': list(output), 'usage': USAGE}}


def tool_calls(result):
    return [(c['id'], c['function']['name'], c['function']['arguments']) for c in result.get('tool_calls', [])]


@pytest.mark.parametrize('arguments', [
    '{"file_path":"a.py","file_path":"b.py"}',
    '{"file_path":"a.py","options":{"limit":NaN}}',
    '{"file_path":"a.py","limit":1e999}',
])
def test_ambiguous_or_nonfinite_tool_arguments_are_refused(arguments):
    refused(start() + [done(0, call(arguments=arguments)), completed()])


# -- the live failure: completed output is empty, the call exists only as done -------

def test_empty_terminal_output_returns_the_finalized_call_for_replay():
    result, provider, tokens = run(start() + call_events(0, call()) + [completed()])
    assert tool_calls(result) == [('call_1', 'Read', ARGS)]
    assert result['content'] is None and tokens == []
    assert result['_responses_output'] == [call()]
    assert provider.last_usage['input_tokens'] == 40 and provider.last_usage['output_tokens'] == 8
    assert provider.response is None and provider.stream.closed
    body = provider.requests[0][1]
    assert body['store'] is False and body['parallel_tool_calls'] is False
    # Stateless replay: the finalized call precedes its result with the same identity.
    replay = input_items([result, {'role': 'tool', 'tool_call_id': 'call_1', 'content': '{"ok": true}'}])
    assert [i['type'] for i in replay] == ['function_call', 'function_call_output']
    assert replay[0]['call_id'] == replay[1]['call_id'] == 'call_1' and replay[0]['arguments'] == ARGS


def test_mixed_reasoning_message_and_call_keep_index_order_and_stream_text_once():
    text = 'Checking the file.'
    events = (start() + [added(0, reasoning()), done(0, reasoning()), added(1, message(text)),
                         {'type': 'response.output_text.delta', 'output_index': 1, 'item_id': 'msg_1', 'delta': 'Checking '},
                         {'type': 'response.output_text.delta', 'output_index': 1, 'item_id': 'msg_1', 'delta': 'the file.'},
                         done(1, message(text))]
              + call_events(2, call()) + [completed()])
    result, _, tokens = run(events)
    assert [i['type'] for i in result['_responses_output']] == ['reasoning', 'message', 'function_call']
    assert result['_responses_output'][0]['encrypted_content'] == 'opaque-rs_1'
    assert result['content'] == text and tokens == ['Checking ', 'the file.']  # no completed-message repeat
    assert tool_calls(result) == [('call_1', 'Read', ARGS)]


def test_finalized_message_without_text_deltas_is_shown_exactly_once():
    result, _, tokens = run(start() + [added(0, message('Done.')), done(0, message('Done.')), completed()])
    assert result['content'] == 'Done.' and tokens == ['Done.'] and 'tool_calls' not in result


def test_out_of_order_finishes_are_ordered_by_index_and_exact_redelivery_is_idempotent():
    second = call(call_id='call_2', item_id='fc_2', arguments='{"file_path": "b.py"}')
    events = start() + [added(0, call()), added(1, second), done(1, second), done(0, call()), done(1, second), completed()]
    result, _, _ = run(events)
    assert [c[0] for c in tool_calls(result)] == ['call_1', 'call_2']
    assert [i['call_id'] for i in result['_responses_output']] == ['call_1', 'call_2']


# -- conflicts, duplicates and identity mismatches are refused, not guessed --------

@pytest.mark.parametrize('events', [
    pytest.param([done(0, call()), done(0, call(arguments='{"file_path": "other.py"}'))], id='conflicting_redelivery'),
    pytest.param([added(0, call()), done(0, call(call_id='call_2'))], id='call_id_changed'),
    pytest.param([added(0, message('x')), done(0, call())], id='type_changed'),
    pytest.param([added(0, call()), added(0, call(item_id='fc_9'))], id='conflicting_announcement'),
    pytest.param([added(0, call()), {'type': 'response.function_call_arguments.done', 'output_index': 0,
                                     'item_id': 'fc_other', 'arguments': ARGS}, done(0, call())], id='arguments_for_other_item'),
    pytest.param([added(0, call()), {'type': 'response.function_call_arguments.done', 'output_index': 0,
                                     'item_id': 'fc_1', 'arguments': '{"file_path": "b.py"}'}, done(0, call())],
                 id='final_arguments_disagree'),
    pytest.param([added(0, message('x')), {'type': 'response.function_call_arguments.delta', 'output_index': 0,
                                           'delta': '{}'}], id='arguments_target_message'),
    pytest.param([done(0, call()), added(0, call())], id='announced_after_finish'),
    pytest.param([done(0, call()), {'type': 'response.function_call_arguments.delta', 'output_index': 0,
                                    'item_id': 'fc_1', 'delta': 'x'}], id='arguments_after_finish'),
    pytest.param([done(0, call()), done(1, call(item_id='fc_2'))], id='duplicate_call_identity'),
])
def test_conflicting_or_duplicate_items_are_refused(events):
    refused(start() + events + [completed()])


# -- unfinished, truncated and incomplete streams never return a call --------------

def test_stream_cut_after_a_finished_call_is_retryable_and_returns_nothing():
    error, tokens = refused(start() + call_events(0, call()), match='before completion', kind=ProviderFailure)
    assert error.retryable is True and tokens == []


@pytest.mark.parametrize('events,match', [
    pytest.param(call_events(0, call())[:-1] + [completed()], 'never finished', id='missing_done'),
    pytest.param([added(0, call()), completed()], 'never finished', id='announced_only'),
    pytest.param([{'type': 'response.function_call_arguments.done', 'output_index': 0, 'arguments': ARGS}, completed()],
                 'never finished', id='final_arguments_only'),
    pytest.param([done(0, call(status='in_progress')), completed()], 'unfinished', id='unfinished_status'),
    pytest.param([done(0, call()), done(2, call(call_id='call_3', item_id='fc_3')), completed()], 'not contiguous',
                 id='index_gap'),
    pytest.param([done(0, call()), completed(status='incomplete')], 'did not complete', id='terminal_not_completed'),
    pytest.param([done(0, call()), {'type': 'response.incomplete',
                                    'response': {'incomplete_details': {'reason': 'max_output_tokens'}}}],
                 'incomplete', id='incomplete_event'),
    pytest.param([completed(), done(0, call())], 'after completion', id='item_after_completion'),
    pytest.param([completed(), completed()], 'after completion', id='second_terminal'),
])
def test_unfinished_or_incomplete_output_is_refused(events, match):
    refused(start() + events, match=match)


def test_provider_failure_is_reported_without_the_credential():
    error, _ = refused(start() + [{'type': 'response.failed', 'response': {
        'error': {'message': 'denied for ' + SECRET, 'code': 'fixture_denied'}}}], match='Provider')
    assert SECRET not in str(error)


# -- malformed calls and records ----------------------------------------------------

@pytest.mark.parametrize('events', [
    pytest.param([done(0, call(arguments='{"file_path": "a.py"'))], id='truncated_arguments'),
    pytest.param([done(0, call(arguments='["a.py"]'))], id='arguments_not_object'),
    pytest.param([done(0, {**call(), 'arguments': {'file_path': 'a.py'}})], id='arguments_not_string'),
    pytest.param([done(0, {k: v for k, v in call().items() if k != 'call_id'})], id='missing_call_id'),
    pytest.param([done(0, call(call_id='c' * 301))], id='oversized_call_id'),
    pytest.param([done(0, call(name=''))], id='empty_name'),
    pytest.param([{'type': 'response.output_item.done', 'output_index': -1, 'item': call()}], id='negative_index'),
    pytest.param([{'type': 'response.output_item.done', 'output_index': True, 'item': call()}], id='boolean_index'),
    pytest.param([{'type': 'response.output_item.done', 'item': call()}], id='missing_index'),
    pytest.param([{'type': 'response.output_item.done', 'output_index': 0, 'item': 'function_call'}], id='item_not_object'),
])
def test_malformed_calls_are_refused(events):
    refused(start() + events + [completed()])


# -- cancellation --------------------------------------------------------------------

@pytest.mark.parametrize('moment', ['before_completion', 'after_stream_end'])
def test_cancellation_returns_nothing_even_after_a_finished_call(moment):
    cancel, events = threading.Event(), start() + call_events(0, call()) + [completed()]

    def on_read(_, line):
        if moment == 'before_completion' and b'response.completed' in line or moment == 'after_stream_end' and not line:
            cancel.set()
    provider, tokens = Provider(events, on_read), []
    with pytest.raises(InterruptedError):
        complete_responses(provider, PROFILE, MESSAGES, TOOLS, cancel, tokens.append, 'medium')
    assert provider.last_usage == {'sentinel': True} and provider.response is None and provider.stream.closed


# -- a nonempty terminal output stays authoritative and must agree -----------------

def test_nonempty_terminal_output_consistent_with_streamed_items_is_used():
    terminal = [reasoning(), call()]
    events = start() + [added(0, reasoning()), done(0, reasoning())] + call_events(1, call()) + [completed(terminal)]
    result, _, _ = run(events)
    assert result['_responses_output'] == terminal and tool_calls(result) == [('call_1', 'Read', ARGS)]


@pytest.mark.parametrize('terminal', [
    pytest.param([call(arguments='{"file_path": "other.py"}')], id='arguments_differ'),
    pytest.param([call(call_id='call_9')], id='call_identity_differs'),
    pytest.param([message('hello')], id='item_type_differs'),
    pytest.param([call(), call(call_id='call_2', item_id='fc_2', status='incomplete')], id='unfinished_terminal_item'),
    pytest.param([call(), call(item_id='fc_2')], id='duplicate_terminal_call_identity'),
])
def test_nonempty_terminal_output_that_disagrees_is_refused(terminal):
    refused(start() + call_events(0, call()) + [completed(terminal)])


def test_terminal_output_missing_a_streamed_item_is_refused():
    events = start() + [added(0, reasoning()), done(0, reasoning())] + call_events(1, call()) + [completed([reasoning()])]
    refused(events, match='differs from a finished streamed item')


def test_terminal_output_that_is_not_a_list_is_refused():
    events = start() + [{'type': 'response.completed', 'response': {'status': 'completed', 'output': {'x': 1}}}]
    refused(events, match='malformed final output')


# -- legacy standard streams are unchanged ------------------------------------------

def test_legacy_text_stream_with_terminal_message_keeps_streamed_text_and_opaque_output():
    terminal = [{'type': 'reasoning', 'encrypted_content': 'opaque:alpha'},
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'reply:alpha'}]}]
    result, _, tokens = run([{'type': 'response.output_text.delta', 'delta': 'reply:alpha'}, completed(terminal)])
    assert result['content'] == 'reply:alpha' and tokens == ['reply:alpha']
    assert result['_responses_output'] == terminal and 'tool_calls' not in result


def test_legacy_terminal_call_with_identity_free_argument_delta_is_dispatchable():
    terminal = [{'type': 'reasoning', 'id': 'r', 'summary': [], 'encrypted_content': 'opaque'},
                {'type': 'function_call', 'id': 'fc', 'call_id': 'call_1', 'name': 'Read', 'arguments': ARGS}]
    result, provider, _ = run([{'type': 'response.function_call_arguments.delta', 'delta': '{}'}, completed(terminal)])
    assert tool_calls(result) == [('call_1', 'Read', ARGS)] and result['_responses_output'] == terminal
    assert provider.last_usage['cached_tokens'] == 12


def test_legacy_terminal_message_without_deltas_is_shown_once():
    result, _, tokens = run([completed([message('Only at the end.')])])
    assert result['content'] == 'Only at the end.' and tokens == ['Only at the end.']


def test_completed_response_with_nothing_usable_is_still_refused():
    refused(start() + [completed()], match='no text or tool calls')
