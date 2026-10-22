"""Stateless Responses adapter; broker remains the only tool executor.

The terminal ``response.completed`` event is required before anything is
returned. Its ``output`` is authoritative when nonempty. Some routes (the
ChatGPT backend) complete with an empty ``output`` and deliver the finalized
items only as ``response.output_item.done`` events; the output is then rebuilt
from those items, and only when they are finished, contiguous by
``output_index``, identity-consistent with their announcements and well formed.
Any conflict is refused rather than guessed, and nothing partial is returned.

Phase 22: on the ChatGPT sign-in route above a 4,096-token budget the request
asks for reasoning summaries (`reasoning.summary`). Their text reaches only
`token.reasoning` (display data): returned reasoning items carry an empty
summary, so it is never model context, a run record or an instruction. A
watchdog bounds the time to the stream's first line and between lines
(recovery.StreamWatchdog); a stalled request is abandoned as a retryable
failure before anything from it is used.

Phase 24 (t24-auth): on the ChatGPT route a request that could not connect is
worded without "check its URL" (Pebble chose the address); a request refused
with HTTP 401 refreshes the sign-in once and is asked once more, and a second
401 ends with the reconnect message (V1-070); above a 4,096-token budget the
body carries prompt_cache_key, the run id the runner sets on the provider, so
consecutive steps of one run share the provider's prompt cache (V1-078).
"""
import json
import math
import re
import socket
import threading
import time
from urllib.parse import urlsplit
from .stats import usage_metrics
from .capabilities import capabilities, output_budget
from .recovery import (READ_MARGIN, REQUEST_TIMEOUT, ProviderFailure, StreamBudget, StreamWatchdog, connect_message,
                       open_watched)

MAX_OUTPUT_ITEMS = 256
FINISHED = (None, 'completed')
SUMMARY = 'auto'       # the Responses API's own choice of summary detail
SUMMARY_ABOVE = 4096   # request bodies at or below this working budget are unchanged
LOOPBACK = {'localhost', '127.0.0.1', '::1'}
CONNECT_NOTICE = 1.0   # seconds still connecting before the activity timeline shows "Connecting to …"
CACHE_KEY = re.compile(r'[A-Za-z0-9_.:-]{1,64}')  # prompt_cache_key: an opaque run id, never task content


def _refuse(reason):
    raise ValueError('Provider stream output is inconsistent (' + reason + '). No tools were executed.')


def _index(event):
    index = event.get('output_index')
    if type(index) is not int or not 0 <= index < MAX_OUTPUT_ITEMS:
        _refuse('invalid output index')
    return index


def _item(value):
    if not isinstance(value, dict) or not isinstance(value.get('type'), str) or not value['type']:
        _refuse('malformed output item')
    return value


def _argument_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate argument key.')
        result[key] = value
    return result


def _argument_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('Non-finite argument number.')
    return value


def _argument_constant(text):
    raise ValueError('Non-JSON argument constant.')


def _final(value):
    """A finished item; a function call must be a complete, well-formed call."""
    item = _item(value)
    if item.get('status') not in FINISHED:
        _refuse('an output item is unfinished')
    if item['type'] == 'function_call':
        call_id, name, arguments = item.get('call_id'), item.get('name'), item.get('arguments')
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 300 or not isinstance(name, str) or not 1 <= len(name) <= 128:
            _refuse('malformed function call identity')
        try:
            parsed = json.loads(arguments, object_pairs_hook=_argument_object, parse_float=_argument_float,
                                parse_constant=_argument_constant) if isinstance(arguments, str) else None
        except (ValueError, RecursionError):
            parsed = None
        if not isinstance(parsed, dict):
            _refuse('function call arguments are not a complete JSON object')
    return item


def _same(a, b):
    """Same item identity: type, and every identity field both sides carry."""
    return a['type'] == b['type'] and all(
        a.get(key) is None or b.get(key) is None or a[key] == b[key] for key in ('id', 'call_id', 'name'))


def _same_call(a, b):
    return _same(a, b) and (a['type'] != 'function_call' or
                            all(a.get(key) == b.get(key) for key in ('call_id', 'name', 'arguments')))


class _StreamOutput:
    """Announced, finished and final-argument records, keyed by output_index."""

    def __init__(self):
        self.added, self.done, self.arguments = {}, {}, {}

    def announce(self, event):
        index, item = _index(event), _item(event.get('item'))
        if index in self.done:
            _refuse('an item was announced after it finished')
        if index in self.added and not _same(self.added[index], item):
            _refuse('conflicting items share an output index')
        self.added.setdefault(index, item)

    def finish(self, event):
        index, item = _index(event), _final(event.get('item'))
        if index in self.done:
            if self.done[index] != item:
                _refuse('conflicting finished items share an output index')
            return
        if index in self.added and not _same(self.added[index], item):
            _refuse('a finished item differs from its announcement')
        if index in self.arguments and (item['type'] != 'function_call' or item['arguments'] != self.arguments[index]):
            _refuse('finished call arguments differ from their final arguments')
        self.done[index] = item

    def call_arguments(self, event, final):
        if 'output_index' not in event:
            return  # Progress without identity; the finished item stays authoritative.
        index = _index(event)
        known = self.done.get(index) or self.added.get(index)
        if known is not None:
            if known['type'] != 'function_call':
                _refuse('call arguments target a non-call item')
            if event.get('item_id') is not None and known.get('id') is not None and event['item_id'] != known['id']:
                _refuse('call arguments name another item')
        if not final:
            if index in self.done:
                _refuse('call arguments continued after the item finished')
            return
        arguments = event.get('arguments')
        if not isinstance(arguments, str) or self.arguments.get(index, arguments) != arguments:
            _refuse('conflicting final call arguments')
        if index in self.done and self.done[index].get('arguments') != arguments:
            _refuse('finished call arguments differ from their final arguments')
        self.arguments[index] = arguments

    def output(self, terminal):
        if not isinstance(terminal, list) or len(terminal) > MAX_OUTPUT_ITEMS:
            _refuse('malformed final output')
        if terminal:
            # Authoritative output; every streamed record must agree with it.
            final = [_final(item) for item in terminal]
            for index, item in self.done.items():
                if index >= len(final) or not _same_call(final[index], item):
                    _refuse('final output differs from a finished streamed item')
            for index, item in self.added.items():
                if index >= len(final) or not _same(final[index], item):
                    _refuse('an announced item is missing from the final output')
            for index, arguments in self.arguments.items():
                if index >= len(final) or final[index]['type'] != 'function_call' or final[index]['arguments'] != arguments:
                    _refuse('final output differs from streamed call arguments')
            return final
        if (set(self.added) | set(self.arguments)) - set(self.done):
            _refuse('an output item never finished')
        if sorted(self.done) != list(range(len(self.done))):
            _refuse('finished output items are not contiguous')
        return [self.done[index] for index in range(len(self.done))]


def _message_text(output):
    parts = []
    for item in output:
        if item['type'] != 'message' or not isinstance(item.get('content'), list):
            continue
        for part in item['content']:
            if not isinstance(part, dict):
                continue
            text = part.get('refusal', part.get('text')) if part.get('type') == 'refusal' else part.get('text')
            if part.get('type') in {'output_text', 'refusal'} and isinstance(text, str):
                parts.append(text)
    return ''.join(parts)


def input_items(messages):
    items = []
    for m in messages:
        if m.get('_responses_output'):
            # Replay opaque reasoning and function items together with their result.
            items.extend(m['_responses_output'])
        elif m['role'] == 'tool':
            items.append({'type': 'function_call_output', 'call_id': m['tool_call_id'], 'output': m['content']})
        else:
            if m.get('content'):
                content=m['content']
                if isinstance(content,list):
                    content=[{'type':'input_text','text':c['text']} if c['type']=='text' else {'type':'input_image','image_url':c['image_url']['url']} for c in content]
                items.append({'role': m['role'], 'content': content})
            for call in m.get('tool_calls', []):
                items.append({'type': 'function_call', 'call_id': call['id'], **call['function']})
    return items


def summaries_requested(provider, profile):
    """Whether this request asks for reasoning summaries (Phase 22): the ChatGPT sign-in route, a reasoning model,
    a working budget above SUMMARY_ABOVE, and a profile whose route has not refused them (Providers.summary_refused
    remembers that across restarts).

    API-key Responses routes keep their Phase 21 request (test_release_v03 pins it; an unverified OpenAI organisation
    is refused summaries)."""
    if not (profile.get('kind') == 'chatgpt' and capabilities(profile)['effort_parameter'] == 'reasoning'
            and profile.get('budget', profile['context']) > SUMMARY_ABOVE):
        return False
    refused = getattr(provider, 'summary_refused', None)
    if callable(refused):
        return not refused(profile)
    return (profile.get('id'), profile.get('model')) not in getattr(provider, 'summaries_refused', ())


def _remember_refusal(provider, profile):
    remember = getattr(provider, 'refuse_summaries', None)
    if callable(remember):
        remember(profile)
    else:
        provider.summaries_refused = set(getattr(provider, 'summaries_refused', ())) | {(profile.get('id'), profile.get('model'))}


class _SummariesRefused(Exception):
    """The stream refused reasoning summaries before anything of it was shown."""


def _names_summaries(text):
    return 'summar' in str(text).casefold()


def _summary_refusal(exc):
    """A refusal that names reasoning summaries, before anything was shown: asked once more without them."""
    return isinstance(exc, _SummariesRefused) or (
        isinstance(exc, ProviderFailure) and exc.status == 400 and _names_summaries(exc))


def stream_budget(provider, profile, effort='default'):
    """The run's watchdog budget (Providers.stream_budget, set by the runner) or the default, its idle deadline fixed
    for this request's effort. A loopback server can hold its first line while it reads a long prompt, so it keeps
    the Phase 21 bound before its first line."""
    budget = getattr(provider, 'stream_budget', None)
    budget = budget if isinstance(budget, StreamBudget) else StreamBudget()
    budget = budget.resolved(capabilities(profile).get('default_effort') if effort == 'default' else effort)
    if urlsplit(profile['url']).hostname in LOOPBACK:
        budget = StreamBudget(REQUEST_TIMEOUT, budget.idle)
    return budget


def _read_timeout(response, seconds):
    """After the headers the socket waits as long as the idle deadline plus a margin, so the watchdog, which counts
    any stream line, decides; the socket never cuts a long silent reasoning phase first."""
    try:
        response.fp.raw._sock.settimeout(seconds)
    except (OSError, AttributeError, ValueError):
        pass


def _abort(response):
    """Unblock a read of this response from the watchdog's thread: the socket's shutdown, as Providers.cancel does.
    A transport that offers abort() (a scripted one) is asked directly."""
    abort = getattr(response, 'abort', None)
    if callable(abort):
        abort()
        return
    try:
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except (OSError, AttributeError):
        pass


class _Pass:
    """One stream pass: what it streamed, and whether anything of it was shown (text or a summary)."""

    def __init__(self):
        self.content, self.first, self.total, self.completed, self.shown = '', None, 0, False, False
        self.terminal, self.usage = [], {}
        self.reported = None  # the latest usage a response.* event carried, for a terminal event without its own
        self.streamed = _StreamOutput()
        self.summary_part, self.summary_parts = None, set()


def _stream_pass(provider, profile, body, key, cancel, token, reasoning, budget, timing, summary):
    """Send the request once and read its stream under the watchdog.

    The deadline starts before name resolution and connect (the watchdog exists before the open). Any stream line
    (an event of any type or an SSE comment) is liveness. Timing is recorded in seconds after the request's start:
    `headers` when the response's headers arrived, `first_event` at its first line. A request still connecting after
    CONNECT_NOTICE seconds calls provider.connect_observer once (the activity timeline's connect item)."""
    from .providers import safe_error
    state, sent = _Pass(), time.monotonic()
    timing.update(headers=None, first_event=None, first_token=None, reasoning_first=None, reasoning_last=None,
                  reasoning_chars=0, total=None, stall=None, sent=None)

    def summarize(event, part):
        # Display only: the text goes to token.reasoning and nowhere else; parts are separated by a blank line.
        part_key = (event.get('item_id'), event.get('summary_index'))
        offset = round(time.monotonic() - sent, 4)
        if timing['reasoning_first'] is None:
            timing['reasoning_first'] = offset
        timing['reasoning_last'] = offset
        separator = '\n\n' if state.summary_part is not None and part_key != state.summary_part else ''
        state.summary_part = part_key
        state.summary_parts.add(part_key)
        timing['reasoning_chars'] += len(part)
        if reasoning is not None:
            state.shown = True
            reasoning(separator + part)

    def request():
        try:
            return provider.request(profile['url'] + '/responses', provider.headers('responses', key), body,
                                    timeout=REQUEST_TIMEOUT)
        except ProviderFailure as exc:
            if profile.get('kind') == 'chatgpt' and getattr(exc, 'code', None) is not None and exc.status is None:
                exc.args = (connect_message(exc.code, 'chatgpt'),)  # Pebble chose this address: no "check its URL"
            raise

    watchdog = StreamWatchdog(budget)
    observer = getattr(provider, 'connect_observer', None)
    notice = threading.Timer(CONNECT_NOTICE, observer) if callable(observer) else None
    try:
        if notice is not None:
            notice.daemon = True
            notice.start()
        try:
            # The root provider opens on a helper thread, so a stalled resolve, connect or header wait is abandoned
            # at the first-event deadline; other providers (a child's pinned transport, scripted ones) open here.
            response = open_watched(request, watchdog, cancel) if getattr(provider, 'watched_open', False) else request()
        finally:
            if notice is not None:
                notice.cancel()
        timing['headers'], timing['sent'] = round(time.monotonic() - sent, 4), True
        with response:
            provider.response = response
            _read_timeout(response, budget.idle + READ_MARGIN)
            watchdog.arm(lambda: _abort(response))
            while True:
                try:
                    raw = response.readline(2_000_001)
                except Exception:
                    if watchdog.expired:
                        raise watchdog.failure() from None
                    raise
                if watchdog.expired:
                    raise watchdog.failure()
                if not raw:
                    break
                watchdog.beat()  # any line is liveness: every event type, and SSE comments
                if timing['first_event'] is None:
                    timing['first_event'] = round(time.monotonic() - sent, 4)
                if cancel.is_set():
                    raise InterruptedError('Stopped by you.')
                state.total += len(raw)
                if len(raw) > 2_000_000 or state.total > 8_000_000:
                    raise ValueError('Provider stream exceeds the response limit.')
                if not raw.startswith(b'data:') or raw.strip() == b'data: [DONE]':
                    continue
                event = json.loads(raw[5:])
                if not isinstance(event, dict):
                    raise ValueError('Invalid provider stream event.')
                kind = event.get('type')
                if isinstance(event.get('response'), dict) and isinstance(event['response'].get('usage'), dict):
                    state.reported = event['response']['usage']
                if state.completed and kind in {'response.output_text.delta', 'response.output_item.added',
                                                'response.output_item.done', 'response.function_call_arguments.delta',
                                                'response.function_call_arguments.done', 'response.completed'}:
                    _refuse('output continued after completion')
                if kind == 'response.output_text.delta':
                    part = event.get('delta', '')
                    if not isinstance(part, str):
                        raise ValueError('Invalid provider text delta.')
                    state.first = state.first or time.monotonic()
                    if part and timing['first_token'] is None:
                        timing['first_token'] = round(time.monotonic() - sent, 4)
                    state.content += part
                    state.shown = state.shown or bool(part)
                    token(part)
                elif kind == 'response.reasoning_summary_text.delta':
                    part = event.get('delta')
                    if isinstance(part, str) and part and not state.completed:
                        summarize(event, part)
                elif kind == 'response.reasoning_summary_text.done':
                    # A route that sends only the finished part: shown once, never after its own deltas.
                    part = event.get('text')
                    if (isinstance(part, str) and part and not state.completed
                            and (event.get('item_id'), event.get('summary_index')) not in state.summary_parts):
                        summarize(event, part)
                elif kind == 'response.output_item.added':
                    state.first = state.first or time.monotonic()
                    state.streamed.announce(event)
                elif kind == 'response.output_item.done':
                    state.streamed.finish(event)
                elif kind in {'response.function_call_arguments.delta', 'response.function_call_arguments.done'}:
                    state.first = state.first or time.monotonic()
                    state.streamed.call_arguments(event, kind.endswith('.done'))
                elif kind == 'response.completed':
                    res = event.get('response')
                    if not isinstance(res, dict) or res.get('status', 'completed') != 'completed':
                        raise ValueError('Provider response did not complete.')
                    # Phase 22: usage from the terminal event, else from the stream's last event that carried it.
                    state.terminal, state.usage = res.get('output', []), res.get('usage') or state.reported or {}
                    state.completed = True
                elif kind in {'response.failed', 'error'}:
                    message = 'Provider: ' + safe_error(event.get('response', event), (key,))
                    if summary and not state.shown and _names_summaries(message):
                        raise _SummariesRefused(message)
                    raise ValueError(message)
                elif kind == 'response.incomplete':
                    reason = event.get('response', {}).get('incomplete_details', {}).get('reason', 'unknown')
                    raise ValueError(f'Provider response incomplete ({str(reason)[:80]}). Try a shorter task or another reasoning mode.')
    except BaseException as exc:
        if getattr(exc, 'unsent', False):
            timing['sent'] = False  # no byte of the request was written: this attempt used no model tokens
        raise
    finally:
        watchdog.close()
        provider.response = None
        timing['total'] = round(time.monotonic() - sent, 4)
        timing['stall'] = watchdog.expired
    return state


def complete_responses(provider, profile, messages, tools, cancel, token, effort):
    entered = time.monotonic()
    # Phase 22: content-free timing for the run's activity timeline, bound to this request (seconds after it was sent).
    timing = {'projection': None, 'headers': None, 'first_event': None, 'first_token': None, 'reasoning_first': None,
              'reasoning_last': None, 'reasoning_chars': 0, 'total': None, 'stall': None, 'summaries': False,
              'sent': None}
    provider.last_timing = timing
    body = {'model': profile['model'], 'input': input_items(messages), 'store': False,
            'stream': True, 'max_output_tokens': output_budget(effort)}
    summary = summaries_requested(provider, profile)
    if capabilities(profile)['effort_parameter'] == 'reasoning':
        if effort != 'default':
            body['reasoning'] = {'effort': effort}
        body['include'] = ['reasoning.encrypted_content']
        if summary:
            body.setdefault('reasoning', {})['summary'] = SUMMARY
    timing['summaries'] = summary
    if tools:
        body['tools'] = [{'type': 'function', **t['function'], 'strict': False} for t in tools]
        body['tool_choice'] = 'auto'
        body['parallel_tool_calls'] = False
    cache_key = getattr(provider, 'prompt_cache_key', None)
    if (profile.get('kind') == 'chatgpt' and isinstance(cache_key, str) and CACHE_KEY.fullmatch(cache_key)
            and profile.get('budget', profile['context']) > SUMMARY_ABOVE):
        body['prompt_cache_key'] = cache_key  # Phase 24 (V1-078): one run's steps share the provider's prompt cache
    key = provider.key(profile)
    budget = min(profile.get('budget', profile['context']), profile['context']-body['max_output_tokens'])
    if budget < 1200:
        body['max_output_tokens'] = min(body['max_output_tokens'], profile['context']//2)
        budget = min(profile.get('budget',profile['context']), profile['context']-body['max_output_tokens'])
    factor = None  # Set only for a local estimate; the provider's own input count needs no calibration.
    if urlsplit(profile['url']).hostname == 'api.openai.com' and profile['kind'] != 'chatgpt':
        def count(ms):
            if cancel.is_set():
                raise InterruptedError('Stopped by you.')
            payload = {'model':body['model'], 'input':input_items(ms)}
            if tools:
                payload['tools'] = body['tools']
            with provider.request(profile['url']+'/responses/input_tokens',provider.headers('responses',key),payload) as response:
                value=json.loads(response.read(8192)).get('input_tokens')
            if type(value) is not int or value<0:
                raise ValueError('Provider returned an invalid input token count.')
            return value
        messages = provider.project_context(messages, tools, budget, counter=lambda ms, ts:count(ms), method='responses_input_tokens')
    else:
        # Some subscription/proxy scopes only permit inference and model listing.
        factor = provider.calibration_factor(profile) if hasattr(provider, 'calibration_factor') else 1.0
        scaled = {} if factor == 1.0 else {'multiplier': factor}  # Any counter works uncalibrated.
        messages = provider.project_context(messages, tools, budget,
            counter=lambda ms, ts:provider.token_budget.count(input_items(ms),body.get('tools',[]),**scaled), method='o200k_local_estimate')
    body['input'] = input_items(messages)
    timing['projection'] = round(time.monotonic() - entered, 4)
    reasoning = getattr(token, 'reasoning', None)
    reasoning = reasoning if callable(reasoning) else None
    budget_watch = stream_budget(provider, profile, effort)
    renewed = False
    while True:
        try:
            state = _stream_pass(provider, profile, body, key, cancel, token, reasoning, budget_watch, timing, summary)
            break
        except (ProviderFailure, _SummariesRefused) as exc:
            if isinstance(exc, ProviderFailure) and exc.status == 401 and profile.get('kind') == 'chatgpt':
                # Phase 24 (V1-070): the route refused this token before anything streamed (no output, no tool). The
                # sign-in refreshes once and the same request is asked once more; a second refusal ends the sign-in.
                renew = getattr(provider, 'renew_key', None)
                if not callable(renew):
                    raise
                if renewed:
                    from .chatgpt import SignInError
                    raise SignInError('reconnect_required', status=401) from None
                key, renewed = renew(profile, key), True
                if not key:
                    raise exc from None
                continue
            if not summary or not _summary_refusal(exc):
                raise
            # The route refused reasoning summaries before anything was shown: ask once more without them (the Phase
            # 21 body) and remember the refusal for this profile, so the turn never fails because of the field.
            _remember_refusal(provider, profile)
            body['reasoning'].pop('summary', None)
            if not body['reasoning']:
                del body['reasoning']
            timing['summaries'] = summary = False
    if cancel.is_set():
        raise InterruptedError('Stopped by you.')
    if not state.completed:
        raise ProviderFailure('Provider stream ended before completion. No tools were executed.', retryable=True)
    # Reasoning items are replayed with an empty summary (the Phase 21 wire shape): summary text is display data
    # only, so it never returns to the model, the run's records or a checkpoint.
    output = [{**item, 'summary': []} if item['type'] == 'reasoning' and item.get('summary') else item
              for item in state.streamed.output(state.terminal)]
    calls = [{'id': i['call_id'], 'type': 'function', 'function': {'name': i['name'], 'arguments': i['arguments']}}
             for i in output if i['type'] == 'function_call']
    if len({call['id'] for call in calls}) != len(calls):
        _refuse('duplicate function call identity')
    content = state.content
    if not content:
        # Only a route that streamed no text supplies it here, so nothing is shown twice.
        content = _message_text(output)
        if content:
            token(content)
    if not content and not calls:
        raise ValueError('Model returned no text or tool calls.')
    provider.last_usage = usage_metrics(state.usage, {}, state.first, time.monotonic())
    if factor is not None and hasattr(provider, 'calibrate'):
        provider.calibrate(profile, factor)  # Local estimates only; the input_tokens route is exact.
    return {'role': 'assistant', 'content': content or None, '_responses_output': output,
            **({'tool_calls': calls} if calls else {})}
