"""Live attempt events present partial text without changing what a retry may do."""

import copy
import hashlib
import http.client
import json
import threading

import pytest

from pebble import stream_journal
from pebble.recovery import PresentationFailure, ProviderFailure, RetryPolicy, complete_with_retry, transient
from pebble.runtime import RunJournal, RunLimits
from pebble.store import Store
from pebble.stream_journal import StreamJournal


FAST = RetryPolicy(initial_delay=0, max_delay=0)
GOAL = [{'role': 'user', 'content': 'goal'}]
TOOLS = [{'type': 'function', 'function': {'name': 'Read', 'parameters': {'type': 'object'}}}]
KEYS = {'begin': {'type', 'attempt', 'state'}, 'delta': {'type', 'attempt', 'state', 'text'},
        'discard': {'type', 'attempt', 'state', 'reason', 'retrying', 'status', 'usage'},
        'commit': {'type', 'attempt', 'state', 'bytes', 'sha256'}}


def run(complete, events, output=None, cancel=None, policy=FAST, **kwargs):
    kwargs.setdefault('on_event', events.append)
    return complete_with_retry(complete, GOAL, TOOLS, cancel or threading.Event(),
        (output if output is not None else []).append, False, policy=policy, **kwargs)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def begin(n):
    return {'type': 'begin', 'attempt': n, 'state': 'open'}


def delta(n, text):
    return {'type': 'delta', 'attempt': n, 'state': 'open', 'text': text}


def discard(n, state='discarded', reason='provider', retrying=False, status=None):
    return {'type': 'discard', 'attempt': n, 'state': state, 'reason': reason, 'retrying': retrying,
            'status': status, 'usage': 'unknown'}


def commit(n, text):
    return {'type': 'commit', 'attempt': n, 'state': 'committed', 'bytes': len(text.encode()), 'sha256': sha(text)}


def assert_shapes(events):
    assert all(set(e) == KEYS[e['type']] for e in events)


def test_reconnect_presents_partial_then_discards_and_commits_identical_retry():
    inputs, output, events, retries, failures = [], [], [], [], []

    def complete(messages, tools, cancel, token, effort):
        inputs.append((copy.deepcopy(messages), tools))
        if len(inputs) == 1:
            token('Abandoned ')
            token('')  # buffered as before, never presented
            raise ProviderFailure('EOF', retryable=True, status=503)
        token('Committed answer')
        return {'role': 'assistant', 'content': 'Committed answer'}

    result = run(complete, events, output, on_retry=lambda *args: retries.append(args),
                 on_failure=lambda exc, n: failures.append((type(exc), n)))
    assert result['content'] == 'Committed answer' and output == ['Committed answer']
    assert inputs[0] == inputs[1] and inputs[0][1] is inputs[1][1] is TOOLS
    assert events == [begin(1), delta(1, 'Abandoned '), discard(1, retrying=True, status=503),
                      begin(2), delta(2, 'Committed answer'), commit(2, 'Committed answer')]
    assert failures == [(ProviderFailure, 1)] and [r[0] for r in retries] == [1] and retries[0][2] == 503
    assert_shapes(events)


def scripted():
    calls = []

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        token('Draft ')
        if len(calls) == 1:
            raise ConnectionResetError('reset')
        token('final.')
        return {'role': 'assistant', 'content': 'Draft final.'}
    return complete, calls


def test_default_buffering_is_unchanged_by_presentation():
    plain, plain_calls = scripted()
    live, live_calls = scripted()
    plain_out, live_out, events = [], [], []
    expected = complete_with_retry(plain, GOAL, TOOLS, threading.Event(), plain_out.append, False, policy=FAST)
    assert run(live, events, live_out) == expected
    assert plain_out == live_out == ['Draft ', 'final.'] and len(plain_calls) == len(live_calls) == 2
    assert events[-1] == commit(2, 'Draft final.')


def test_cancel_mid_stream_interrupts_without_retry_or_commit():
    cancel, calls, output, events, failures = threading.Event(), [], [], [], []

    def complete(messages, tools, stop, token, effort):
        calls.append(True)
        token('Partial')
        cancel.set()
        token('never shown')

    with pytest.raises(InterruptedError):
        run(complete, events, output, cancel=cancel, on_failure=lambda exc, n: failures.append(n))
    assert len(calls) == 1 and output == [] and failures == [1]
    assert events == [begin(1), delta(1, 'Partial'), discard(1, 'interrupted', 'cancelled')]


def test_cancel_after_provider_success_discards_completed_text():
    cancel, output, events, failures = threading.Event(), [], [], []

    def complete(messages, tools, stop, token, effort):
        token('Complete text')
        cancel.set()
        return {'role': 'assistant', 'content': 'Complete text'}

    with pytest.raises(InterruptedError):
        run(complete, events, output, cancel=cancel, on_failure=lambda exc, n: failures.append(n))
    assert output == [] and failures == []  # unchanged: no failure accounting for a stopped success
    assert events == [begin(1), delta(1, 'Complete text'), discard(1, 'interrupted', 'cancelled')]


def test_deadline_during_stream_interrupts_without_retry():
    now, calls, events = [0.0], [], []

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        token('Before the budget')
        now[0] = 11.0
        token('after the budget')

    with pytest.raises(ProviderFailure, match='time budget'):
        run(complete, events, deadline=10.0, clock=lambda: now[0])
    assert len(calls) == 1
    assert events == [begin(1), delta(1, 'Before the budget'), discard(1, 'interrupted', 'deadline')]


def test_deadline_after_success_raises_timeout_without_commit():
    now, output, events = [0.0], [], []

    def complete(messages, tools, cancel, token, effort):
        token('Finished late')
        now[0] = 10.0
        return {'role': 'assistant', 'content': 'Finished late'}

    with pytest.raises(TimeoutError):
        run(complete, events, output, deadline=10.0, clock=lambda: now[0])
    assert output == [] and events == [begin(1), delta(1, 'Finished late'), discard(1, 'interrupted', 'deadline')]


def test_deadline_during_backoff_stops_reconnect():
    now, calls, events, retries = [0.0], [], [], []

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        token('Lost')
        now[0] = 10.0
        raise ProviderFailure('overloaded', retryable=True, status=529)

    with pytest.raises(TimeoutError, match='reconnecting'):
        run(complete, events, deadline=10.0, clock=lambda: now[0], on_retry=lambda *a: retries.append(a))
    assert len(calls) == 1 and retries == []
    assert events == [begin(1), delta(1, 'Lost'), discard(1, 'interrupted', 'deadline', status=529)]


@pytest.mark.parametrize('fragment,error', [
    (b'raw bytes', ProviderFailure), ('\ud800', UnicodeEncodeError),
    ('x' * 8_000_001, ProviderFailure), (None, ValueError)],
    ids=['bytes', 'lone-surrogate', 'oversized', 'malformed-json'])
def test_malformed_fragments_and_protocol_are_discarded_not_retried(fragment, error):
    calls, events, output = [], [], []

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        token('Visible ')
        if fragment is None:
            raise ValueError('malformed JSON')
        token(fragment)
        return {'role': 'assistant', 'content': 'unreachable'}

    with pytest.raises(error) as info:
        run(complete, events, output)
    assert info.type is error and len(calls) == 1 and output == []
    assert events == [begin(1), delta(1, 'Visible '), discard(1)]


def test_exhausted_retries_discard_every_attempt_and_keep_accounting():
    calls, events, failures, retries = [], [], [], []

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        token(f'try {len(calls)}')
        raise http.client.IncompleteRead(b'partial')

    with pytest.raises(http.client.IncompleteRead):
        run(complete, events, on_failure=lambda exc, n: failures.append(n), on_retry=lambda *a: retries.append(a))
    assert len(calls) == 3 and failures == [1, 2, 3] and [r[0] for r in retries] == [1, 2]
    assert events == [begin(1), delta(1, 'try 1'), discard(1, retrying=True),
                      begin(2), delta(2, 'try 2'), discard(2, retrying=True),
                      begin(3), delta(3, 'try 3'), discard(3)]


def test_long_server_delay_discards_without_early_retry():
    calls, events = [], []

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        raise ProviderFailure('rate limit', retryable=True, status=429, retry_after=120)

    with pytest.raises(ProviderFailure):
        run(complete, events)
    assert len(calls) == 1 and events == [begin(1), discard(1, status=429)]


def failing_on(kind, error=ConnectionError('presentation store unavailable')):
    seen = []

    def on_event(event):
        seen.append(event)
        if event['type'] == kind:
            raise error
    return seen, on_event


def test_delta_callback_failure_fails_closed_even_for_transient_errors():
    calls, output, failures, retries = [], [], [], []
    seen, on_event = failing_on('delta')

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        token('Shown')
        return {'role': 'assistant', 'content': 'Shown'}

    with pytest.raises(PresentationFailure) as info:
        run(complete, [], output, on_event=on_event, on_failure=lambda exc, n: failures.append((type(exc), n)),
            on_retry=lambda *a: retries.append(a))
    assert isinstance(info.value.__cause__, ConnectionError) and transient(info.value.__cause__)
    assert len(calls) == 1 and output == [] and retries == []
    assert failures == [(PresentationFailure, 1)]
    assert seen == [begin(1), delta(1, 'Shown')]  # nothing presented after the failure


@pytest.mark.parametrize('provider', ['wraps', 'swallows'])
def test_provider_cannot_turn_a_callback_failure_into_a_retry_or_commit(provider):
    calls, output, failures = [], [], []
    seen, on_event = failing_on('delta', RuntimeError('disk full'))

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        try:
            token('First')
        except Exception:
            if provider == 'wraps':
                raise ProviderFailure('stream reset', retryable=True) from None
        try:
            token('Second')
        except Exception:
            pass
        return {'role': 'assistant', 'content': 'FirstSecond'}

    with pytest.raises(PresentationFailure):
        run(complete, [], output, on_event=on_event, on_failure=lambda exc, n: failures.append(n))
    assert len(calls) == 1 and output == [] and failures == [1]
    assert seen == [begin(1), delta(1, 'First')]


def test_begin_callback_failure_never_reaches_the_provider():
    calls, failures = [], []
    seen, on_event = failing_on('begin')

    def complete(*args):
        calls.append(True)

    with pytest.raises(PresentationFailure):
        run(complete, [], on_event=on_event, on_failure=lambda exc, n: failures.append(n))
    assert calls == [] and failures == [] and seen == [begin(1)]


def test_commit_callback_failure_publishes_nothing():
    calls, output, failures = [], [], []
    seen, on_event = failing_on('commit')

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        token('Complete')
        return {'role': 'assistant', 'content': 'Complete'}

    with pytest.raises(PresentationFailure):
        run(complete, [], output, on_event=on_event, on_failure=lambda exc, n: failures.append(n))
    assert len(calls) == 1 and output == [] and failures == [1]
    assert seen == [begin(1), delta(1, 'Complete'), commit(1, 'Complete')]


def test_discard_callback_failure_prevents_the_retry():
    calls, retries = [], []
    seen, on_event = failing_on('discard')

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        token('Lost')
        raise ProviderFailure('overloaded', retryable=True)

    with pytest.raises(PresentationFailure):
        run(complete, [], on_event=on_event, on_retry=lambda *a: retries.append(a))
    assert len(calls) == 1 and retries == [] and seen[-1] == discard(1, retrying=True)


def test_finished_attempt_callback_cannot_reach_a_later_attempt():
    tokens, events, output, stale = [], [], [], []

    def complete(messages, tools, cancel, token, effort):
        tokens.append(token)
        if len(tokens) == 1:
            token('First attempt')
            raise ConnectionResetError('reset')
        try:
            tokens[0]('late text from the first connection')
        except ProviderFailure as exc:
            stale.append(str(exc))
        token('Second attempt')
        return {'role': 'assistant', 'content': 'Second attempt'}

    run(complete, events, output)
    assert output == ['Second attempt'] and stale == ['A finished model attempt cannot add text.']
    assert [e for e in events if e['type'] == 'delta'] == [delta(1, 'First attempt'), delta(2, 'Second attempt')]


def test_tool_responses_present_only_visible_text_and_run_nothing():
    events, output, calls = [], [], []
    call = {'id': 'call_tool_identity', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{"file_path": "a.txt"}'}}

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        if len(calls) == 1:
            raise ProviderFailure('reset', retryable=True)
        token('Let me read it.')
        return {'role': 'assistant', 'content': 'Let me read it.', 'tool_calls': [copy.deepcopy(call)]}

    response = run(complete, events, output)
    assert response['tool_calls'] == [call] and len(calls) == 2 and output == ['Let me read it.']
    assert events[-1] == commit(2, 'Let me read it.')
    assert 'file_path' not in json.dumps(events) and 'call_tool_identity' not in json.dumps(events)
    assert_shapes(events)


def test_presentation_contract_is_explicit():
    assert transient(PresentationFailure()) is False
    with pytest.raises(TypeError):
        run(lambda *args: None, [], on_event='not callable')


def test_journal_wiring_replays_a_reconnect_idempotently_and_in_isolation(tmp_path):
    store = Store(tmp_path / 'home')
    runs = RunJournal(store, tmp_path / 'home')
    stream = StreamJournal(store, runs)
    try:
        cid = store.new_chat()
        rid = runs.create(cid, 'goal', {'limits': RunLimits().as_dict()})
        runs.transition(rid, 'running')
        state, seen, mid, output = {}, [], [], []

        def present(event):  # what the parent runner does; commit waits for the response record
            if event['type'] == 'begin':
                state['id'] = stream.begin(rid, 1, event['attempt'])
            elif event['type'] == 'delta':
                stream.append(state['id'], event['text'])
            elif event['type'] == 'discard':
                stream.settle(state['id'], event['state'])
            else:
                state['commit'] = (state['id'], event['sha256'])

        def complete(messages, tools, cancel, token, effort):
            seen.append(copy.deepcopy(messages))
            if len(seen) == 1:
                token('Abandoned partial')
                mid.append(stream.read(cid, rid))  # a client reconnecting mid-attempt
                raise ProviderFailure('reset', retryable=True)
            token('Final ')
            token('answer.')
            return {'role': 'assistant', 'content': 'Final answer.'}

        response = complete_with_retry(complete, GOAL, [], threading.Event(), output.append, False,
                                       on_event=present, policy=FAST)
        attempt_id, digest = state['commit']
        assert stream.read(cid, rid)['events'][-1]['kind'] == 'delta'  # not committed before the response record
        runs.response(rid, response, {'phase': 'tools', 'messages': GOAL + [response]}, steps=1, elapsed=0.1)
        committed = stream.settle(attempt_id, 'committed', expect_sha256=digest)

        events = []
        after = 0
        while True:
            page = stream.read(cid, rid, after, 2)
            events += page['events']
            after = page['next_after']
            if not page['more']:
                break
        assert stream.read(cid, rid, 0, 100)['events'] == events  # replay is idempotent
        assert stream.settle(attempt_id, 'committed', expect_sha256=digest) == committed
        assert mid[0]['events'] == events[:2] and mid[0]['more'] is False

        attempts = {}
        for event in events:
            attempts.setdefault(event['attempt_id'], []).append(event)
        first, second = attempts.values()
        assert [e['kind'] for e in first] == ['begin', 'delta', 'settle'] and first[-1]['state'] == 'discarded'
        assert ''.join(e['text'] for e in second if e['kind'] == 'delta') == ''.join(output) == response['content']
        assert second[-1] == committed and committed['sha256'] == digest

        # Abandoned presentation never reached the retry or any canonical record.
        assert seen[0] == seen[1] == GOAL
        assert 'Abandoned' not in json.dumps(runs.events(rid)) + json.dumps(runs.get(rid)['checkpoint'])
        assert runs.tools(rid) == []
    finally:
        runs.close()
        store.close()


def test_journal_storage_failure_fails_closed_and_host_interrupts(tmp_path, monkeypatch):
    store = Store(tmp_path / 'home')
    runs = RunJournal(store, tmp_path / 'home')
    stream = StreamJournal(store, runs)
    try:
        cid = store.new_chat()
        rid = runs.create(cid, 'goal', {'limits': RunLimits().as_dict()})
        runs.transition(rid, 'running')
        monkeypatch.setattr(stream_journal, 'MAX_ATTEMPT_DELTAS', 1)
        state, calls, failures = {}, [], []

        def present(event):
            if event['type'] == 'begin':
                state['id'] = stream.begin(rid, 1, event['attempt'])
            elif event['type'] == 'delta':
                stream.append(state['id'], event['text'])

        def complete(messages, tools, cancel, token, effort):
            calls.append(True)
            token('one')
            token('two')
            return {'role': 'assistant', 'content': 'onetwo'}

        with pytest.raises(PresentationFailure) as info:
            complete_with_retry(complete, GOAL, [], threading.Event(), lambda part: None, False,
                                on_event=present, on_failure=lambda exc, n: failures.append(n), policy=FAST)
        assert isinstance(info.value.__cause__, stream_journal.StreamCapacity)
        assert len(calls) == 1 and failures == [1]
        [settled] = stream.interrupt_open(rid)  # the parent's run finalization
        assert settled['state'] == 'interrupted' and settled['bytes'] == 3
        assert [e['kind'] for e in stream.read(cid, rid)['events']] == ['begin', 'delta', 'settle']
        assert runs.tools(rid) == [] and not any(e['kind'] == 'response_committed' for e in runs.events(rid))
    finally:
        runs.close()
        store.close()
