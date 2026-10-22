"""Bounded model-request recovery. This module never executes/retries tools."""

import contextlib
import errno
import hashlib
import http.client
import math
import random
import re
import ssl
import socket
import sys
import threading
import time
import urllib.error
import weakref
from dataclasses import dataclass
from email.utils import parsedate_to_datetime


class ProviderFailure(ValueError):
    """A model request that failed. `network` (Phase 24) marks one that got no HTTP response because the network was
    away (name lookup, connect or a timeout): complete_with_retry waits longer for it (RetryPolicy.network_*), and
    `code` names its class (failure_class)."""

    def __init__(self, message, *, retryable=False, status=None, retry_after=None, network=False, code=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after
        self.network = network
        self.code = code


# Phase 24 (t24-auth, V1-001/V1-066/V1-072): content-free classes for a request that got no HTTP response. They name
# the failure in logs, sign-in errors and run errors; none carries the request, its reply or the exception's text.
NETWORK = frozenset({'offline', 'connect', 'timeout'})
_NO_ROUTE = frozenset(filter(None, (getattr(errno, name, None) for name in
                                    ('ENETUNREACH', 'EHOSTUNREACH', 'ENETDOWN', 'EADDRNOTAVAIL'))))
_SSL_REASON = re.compile(r'[A-Z0-9_]{1,64}')
CONNECT_WORDS = {
    'offline': 'this Mac seems to be offline (the address could not be looked up)',
    'connect': 'the connection was refused or dropped',
    'timeout': 'it did not answer in time',
    'tls': "its secure connection could not be verified (a network filter, a proxy or this Mac's date and time may "
           "be interfering)",
}
CONNECT_FAILED = 'Could not connect to the model provider. Check its URL and network connection.'


def _reason(exc):
    reason = getattr(exc, 'reason', None) if isinstance(exc, urllib.error.URLError) else None
    return reason if isinstance(reason, BaseException) else exc


def failure_class(exc):
    """'offline' (the name could not be looked up, or there is no route), 'connect' (refused, reset or dropped),
    'timeout', 'tls' (the secure connection could not be set up or verified), or None for anything else."""
    if isinstance(exc, urllib.error.HTTPError):
        return None
    reason = _reason(exc)
    if isinstance(reason, ssl.SSLError):
        return 'tls'
    if isinstance(reason, socket.gaierror):
        return 'offline'
    if isinstance(reason, TimeoutError):
        return 'timeout'
    if isinstance(reason, OSError) and reason.errno in _NO_ROUTE:
        return 'offline'
    if isinstance(reason, (OSError, http.client.HTTPException)):
        return 'connect'
    return None


def failure_detail(exc):
    """Words for a log line about a failure, without its text: the exception class, its errno and the TLS library's
    reason code (for example CERTIFICATE_VERIFY_FAILED)."""
    reason = _reason(exc)
    parts = [type(reason).__name__]
    if type(getattr(reason, 'errno', None)) is int:
        parts.append(f'errno {reason.errno}')
    tls = getattr(reason, 'reason', None) if isinstance(reason, ssl.SSLError) else None
    if isinstance(tls, str) and _SSL_REASON.fullmatch(tls):
        parts.append(tls)
    return ', '.join(parts)


def connect_message(code, route=None):
    """A run's words for a model request that could not connect. The ChatGPT route's address is Pebble's own, so it
    is never told to check a URL; other routes keep the advice (and, unclassified, the exact pre-Phase 24 text).
    Every form starts 'Could not connect to the model provider' (eval_baseline.PROVIDER_TEXT counts it as a provider
    failure)."""
    words = CONNECT_WORDS.get(code)
    if route == 'chatgpt':
        return ('Could not connect to the model provider (ChatGPT): ' + (words or 'the connection failed')
                + '. No tools were executed.')
    return CONNECT_FAILED if words is None else f'Could not connect to the model provider: {words}. ' \
                                                 'Check its URL and network connection.'


NETWORK_WAIT = 'Waiting for the network'


def waiting_for_network(exc):
    """Whether a failed model request is waiting for the network (a retryable failure with no HTTP response)."""
    return isinstance(exc, ProviderFailure) and exc.retryable and getattr(exc, 'network', False) is True


def retry_label(exc, default='Reconnecting to the selected model'):
    """The progress words for a retry after `exc` (the runner's reconnect row)."""
    return NETWORK_WAIT if waiting_for_network(exc) else default


# Phase 22 (t22-stream): the stall watchdog. Live, a ChatGPT request got no byte or event for 120.1 s before its retry:
# the root opened it through socket.create_connection, which gives each resolved address the whole 60 s socket timeout,
# so two dead addresses (or a dead one, then a silent header wait) cost 120 s inside the open. The watchdog bounds one
# streamed request in two deadlines; liveness is ANY stream line (an event of any type, response.created, in_progress,
# reasoning-summary parts, an SSE comment), so only a long silence is a stall:
# - first_event, 20 s: from the request's start, BEFORE name resolution and connect, to the stream's first line. The
#   first Responses event is the acknowledgement sent before any reasoning (the measured healthy attempt's first text
#   came at 10.6 s, so its first event was earlier), so 20 s bounds the whole connect phase with room to spare. Each
#   address also gets only CONNECT_ATTEMPT seconds (providers.connect_any), so one dead address costs seconds.
# - idle, by effort: 90 s up to medium, 180 s for high and above (IDLE_STANDARD, IDLE_HIGH). Summaries, which the
#   ChatGPT route is asked for, arrive while the model reasons; a route without them can think silently, longest at
#   high effort. 90 s is 1.5x and 180 s is 3x Phase 21's old 60 s per-read cut, so a long silent reasoning phase is no
#   longer cut where it was. Codex waits 300 s per event (stream_idle_timeout_ms) because it has no separate
#   first-event deadline; with one, 180 s bounds a dead mid-stream connection at three minutes.
# The socket's read timeout follows the idle deadline (idle + READ_MARGIN), so the socket never cuts first.
# A model request never runs a tool (the runner executes calls only after a committed response), so an abandoned
# attempt has made no tool call or effect, and complete_with_retry's existing caps (three attempts, backoff, the run
# deadline) decide whether to ask again.
REQUEST_TIMEOUT = 60.0          # the socket timeout before the response (headers); each address gets CONNECT_ATTEMPT
CONNECT_ATTEMPT = 4.0           # seconds per resolved address (Happy-Eyeballs-style interleaved families)
FIRST_EVENT = 20.0
IDLE_STANDARD, IDLE_HIGH, IDLE_MAX = 90.0, 180.0, 300.0
HIGH_EFFORTS = frozenset({'high', 'xhigh', 'max'})
READ_MARGIN = 5.0
WATCHDOG_KEYS = ('first_event_seconds', 'idle_seconds')
# Phase 24 (t24-auth, V1-068): time.monotonic is mach_absolute_time on macOS, which stops while the Mac sleeps, so a
# stream that died during sleep would wait out its whole idle deadline after wake. The watchdog also reads a clock that
# counts through sleep; when the two drift apart by SLEEP_MIN (or lifecycle reports did_wake), a stream that sends no
# line within WAKE_GRACE awake seconds is abandoned as a retryable stall. A stream that sends a line keeps going.
SLEEP_POLL = 1.0    # the watcher compares the clocks at least this often while a stream is open
SLEEP_MIN = 2.0     # seconds asleep below this are clock noise (lifecycle.SLEEP_MIN)
WAKE_GRACE = 10.0
_WATCHED = weakref.WeakSet()
_WATCHED_LOCK = threading.Lock()


def clocks():
    """(awake, total): time.monotonic, which stops while the machine sleeps, and a clock that keeps counting asleep
    (Darwin's CLOCK_MONOTONIC, Linux's CLOCK_BOOTTIME; elsewhere the same clock, so only did_wake reports sleep)."""
    awake = time.monotonic()
    if sys.platform == 'darwin':
        return awake, time.clock_gettime(time.CLOCK_MONOTONIC)
    if hasattr(time, 'CLOCK_BOOTTIME'):
        return awake, time.clock_gettime(time.CLOCK_BOOTTIME)
    return awake, awake


def note_wake():
    """The native did_wake hint (lifecycle.Lifecycle.event): every open stream gets WAKE_GRACE to send a line."""
    with _WATCHED_LOCK:
        watchdogs = list(_WATCHED)
    for watchdog in watchdogs:
        watchdog.wake()


def idle_for(effort):
    """The idle deadline for a reasoning effort (an unknown or default effort is treated as medium)."""
    return IDLE_HIGH if effort in HIGH_EFFORTS else IDLE_STANDARD


@dataclass(frozen=True)
class StreamBudget:
    first_event: float = FIRST_EVENT
    idle: float = None  # None: by the request's effort (idle_for)

    @classmethod
    def parse(cls, value=None):
        """A run config's `stream_watchdog` ({first_event_seconds, idle_seconds}); None is the default budget."""
        if value is None:
            return cls()
        if not isinstance(value, dict) or not value or set(value) - set(WATCHDOG_KEYS):
            raise ValueError('The stream watchdog accepts first_event_seconds and idle_seconds.')
        first, idle = value.get('first_event_seconds', FIRST_EVENT), value.get('idle_seconds')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (first, *(() if idle is None else (idle,)))) \
                or not 1 <= first <= REQUEST_TIMEOUT or idle is not None and not 10 <= idle <= IDLE_MAX:
            raise ValueError('Choose a first-event budget of 1–60 seconds and an idle budget of 10–300 seconds.')
        return cls(float(first), None if idle is None else float(idle))

    def as_dict(self):
        return {'first_event_seconds': self.first_event, **({} if self.idle is None else {'idle_seconds': self.idle})}

    def resolved(self, effort):
        """This budget with its idle deadline fixed for one request's effort."""
        return self if self.idle is not None else StreamBudget(self.first_event, idle_for(effort))


class StreamStalled(ProviderFailure):
    """The watchdog abandoned a silent request. Retryable: nothing it streamed is used and it ran no tool.

    `unsent` is True when no byte of the request was written (it was abandoned while resolving or connecting, and
    could not be sent later): that attempt used no model tokens, so its usage is known to be zero."""

    def __init__(self, phase, waited, unsent=False):
        if phase == 'sleep':
            text = 'The Mac slept while waiting for the model, and the model sent nothing after it woke, so Pebble ' \
                   'stopped waiting. No tools were executed.'
        else:
            what = 'no response' if phase == 'first_event' else 'nothing more'
            text = f'The model sent {what} for {waited:g} s, so Pebble stopped waiting. No tools were executed.'
        super().__init__(text, retryable=True)
        self.phase, self.waited, self.unsent = phase, waited, unsent


class StreamWatchdog:
    """One streamed request's budgets. The reader calls beat() for every line it reads; arm() names how to unblock
    a read that has the response (its socket's shutdown). On expiry `expired` names the budget ('first_event',
    'idle', or 'sleep' for a stream silent after a sleep) and the armed abort runs once; the reader then raises
    failure(). close() ends the watcher thread. `clock` returns (awake, total) seconds (clocks())."""

    def __init__(self, budget, clock=None):
        self.budget = budget
        self.clock = clock or clocks
        awake, total = self.clock()
        self.started = awake
        self.first = None
        self.expired = None
        self.woke = None  # awake seconds when a sleep was noticed with no line since; cleared by the next line
        self._last, self._abort, self._closed = self.started, None, False
        self._mark = (awake, total)
        self._condition = threading.Condition()
        with _WATCHED_LOCK:
            _WATCHED.add(self)
        threading.Thread(target=self._watch, daemon=True, name='pebble-stream-watchdog').start()

    def _budget_deadline(self):
        return self.started + self.budget.first_event if self.first is None else self._last + self.budget.idle

    def _deadline(self):
        deadline = self._budget_deadline()
        return deadline if self.woke is None else min(deadline, self.woke + WAKE_GRACE)

    def beat(self):
        with self._condition:
            awake, total = self.clock()
            self._last, self._mark = awake, (awake, total)  # a sleep before this line is over: the stream is alive
            self.woke = None
            if self.first is None:
                self.first = self._last
                self._condition.notify_all()  # the idle deadline may come before the first-event one

    def wake(self):
        """A sleep ended (did_wake): unless a line arrives within WAKE_GRACE, the stream is abandoned."""
        with self._condition:
            if self.woke is None and self.expired is None and not self._closed:
                self.woke = self.clock()[0]
                self._condition.notify_all()

    def _now(self):
        """Awake seconds now; notes a sleep since the last look (the total clock ran ahead of the awake one)."""
        awake, total = self.clock()
        (last_awake, last_total), self._mark = self._mark, (awake, total)
        ran, counted = awake - last_awake, total - last_total
        if ran >= 0 and counted >= 0 and counted - ran >= SLEEP_MIN and self.woke is None:
            self.woke = awake
        return awake

    def arm(self, abort):
        with self._condition:
            self._abort = abort
            fired = self.expired is not None
        if fired:
            _quietly(abort)

    def _watch(self):
        with self._condition:
            while not self._closed:
                now = self._now()
                remaining = self._deadline() - now
                if remaining <= 0:
                    if self.woke is not None and self.woke + WAKE_GRACE < self._budget_deadline():
                        self.expired = 'sleep'
                    else:
                        self.expired = 'first_event' if self.first is None else 'idle'
                    abort = self._abort
                    break
                self._condition.wait(min(remaining, SLEEP_POLL))
            else:
                return
        if abort is not None:
            _quietly(abort)

    def failure(self, unsent=False):
        waited = {'first_event': self.budget.first_event, 'sleep': WAKE_GRACE}.get(self.expired, self.budget.idle)
        return StreamStalled(self.expired, waited, unsent)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        with _WATCHED_LOCK:
            _WATCHED.discard(self)


def _quietly(callback, *args):
    try:
        callback(*args)
    except Exception:
        pass


def _close(response):
    if response is not None:
        _quietly(response.close)


class OpenRecord:
    """One watched open, shared by open_watched and the connection opened on its helper thread (current_open()).

    The connection calls track() when it starts (before name resolution) and may_send() before it writes any byte of
    the request; abandon() then refuses every later send, so an abandoned open never reaches the provider after the
    attempt ended. unsent() is certain only for a tracked open: another transport (a scripted one) is never assumed
    to have sent nothing."""

    def __init__(self):
        self._lock = threading.Lock()
        self.abandoned = self.sent = self.tracked = False

    def track(self):
        with self._lock:
            self.tracked = True
            return not self.abandoned

    def unsent(self):
        with self._lock:
            return self.tracked and not self.sent

    def may_send(self):
        with self._lock:
            if not self.abandoned:
                self.sent = True
            return not self.abandoned

    def abandon(self):
        with self._lock:
            self.abandoned = True
            return self.sent


_OPEN = threading.local()


def current_open():
    """The OpenRecord of the watched open running on this thread, or None (an ordinary request)."""
    return getattr(_OPEN, 'record', None)


@contextlib.contextmanager
def tracked_open():
    """This thread's OpenRecord for one request: the watched open's, or a new one for an ordinary request, so Pebble's
    own connection reports whether any byte of the request was written (Providers.open_url, Phase 24)."""
    record = current_open()
    if record is not None:
        yield record
        return
    record = _OPEN.record = OpenRecord()
    try:
        yield record
    finally:
        _OPEN.record = None


def open_watched(open_request, watchdog, cancel):
    """Open a request (resolve, connect, TLS, send, headers) on a helper thread, so a stalled open is abandoned at the
    first-event budget or when the person stops, instead of waiting out each address's socket timeout. The request's
    own errors re-raise here unchanged, with `unsent` set on a ProviderFailure that no request byte preceded. An
    abandoned open's late response is closed unread: it cannot reach a later attempt, and nothing from it is parsed."""
    box, done, lock, record = {}, threading.Event(), threading.Lock(), OpenRecord()

    def run():
        value, error = None, None
        _OPEN.record = record
        try:
            value = open_request()
        except BaseException as exc:  # handed to the caller, or dropped with an abandoned open
            error = exc
        finally:
            _OPEN.record = None
        with lock:
            if box.get('abandoned'):
                _close(value)
            else:
                box['value'], box['error'] = value, error
            done.set()

    threading.Thread(target=run, daemon=True, name='pebble-model-open').start()
    while not done.wait(0.05):
        if cancel.is_set() or watchdog.expired:
            with lock:
                if not done.is_set():
                    box['abandoned'] = True
            if box.get('abandoned'):
                record.abandon()
                if cancel.is_set():
                    raise InterruptedError('Stopped by you.')
                raise watchdog.failure(unsent=record.unsent())
    if box['error'] is not None:
        if isinstance(box['error'], ProviderFailure) and record.unsent():
            box['error'].unsent = True  # it failed before any byte of the request was written (resolve, connect, TLS)
        raise box['error']
    return box['value']


class PresentationFailure(RuntimeError):
    """A live-presentation callback or its storage failed. Never a provider retry."""

    def __init__(self):
        super().__init__("The live response record failed, so this model request stopped without retrying.")


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}


def retry_after(value, now=None):
    """Return a finite server delay, without copying arbitrary response headers."""
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - (now or time.time())
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def transient(exc):
    if isinstance(exc, (InterruptedError, ssl.SSLError)):
        return False
    if isinstance(exc, ProviderFailure):
        return exc.retryable
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_STATUS
    if isinstance(exc, urllib.error.URLError):
        return not isinstance(exc.reason, ssl.SSLError)
    return isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror, http.client.HTTPException))


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    initial_delay: float = 0.5
    max_delay: float = 8.0
    max_server_delay: float = 30.0
    # Phase 24 (V1-066): a request that got no HTTP response because the network was away (waiting_for_network) may be
    # asked up to network_attempts times in all, waiting 1, 2, 4, 8 and 8 s (about 23 s: a Wi-Fi handover or a wake),
    # always within the run deadline. HTTP statuses and stalls keep `attempts` (the Phase 20 and 22 graders' caps).
    network_attempts: int = 6
    network_delay: float = 1.0


class _Attempt:
    """One attempt's buffer. A finished attempt's callback cannot reach a later one."""

    def __init__(self):
        self.parts, self.size, self.open, self.expired = [], 0, True, False
        self.reasoning, self.reasoning_broken = 0, False


REASONING_ATTEMPT_CHARS = 200_000  # reasoning-summary text forwarded per attempt; display data only


def complete_with_retry(complete, messages, tools, cancel, token, effort,
                        *, on_retry=None, on_failure=None, on_event=None, deadline=None, policy=None,
                        clock=time.monotonic, on_reasoning=None):
    """Only completed attempts publish text. Failed partial JSON/text is discarded.

    Calls regenerate from the same committed context/provider/tools. `token`
    still receives only the returned attempt's text, after its checks.
    Provider billing may include abandoned attempts; unknown usage is not guessed.

    Optional `on_event` presents attempts while they stream, as plain dicts:
    begin, delta (visible text only), discard (discarded on provider failure,
    interrupted on cancel/deadline; usage unknown) and commit (bytes and
    SHA-256, after the cancel/deadline checks and before `token`). A commit
    event is not canonical: the caller commits presentation with its own
    response record. A callback error raises PresentationFailure at once:
    no further events, and never a provider retry.

    Optional `on_reasoning(attempt, text)` (Phase 22) receives an open
    attempt's reasoning-summary text, which a provider sends through
    `token.reasoning`. It is display data, never the response, never model
    context and never a reason to retry: text after the attempt closed or the
    person stopped is dropped, and an observer error only ends that attempt's
    reasoning display.
    """
    policy = policy or RetryPolicy()
    if on_event is not None and not callable(on_event):
        raise TypeError("on_event must be callable.")
    if on_reasoning is not None and not callable(on_reasoning):
        raise TypeError("on_reasoning must be callable.")
    broken = []

    def emit(event):
        if broken:
            raise broken[0]
        try:
            on_event(event)
        except Exception as exc:
            broken.append(PresentationFailure())
            raise broken[0] from exc

    def discard(attempt, reason, exc=None, retrying=False):
        if on_event is None or broken:
            return
        status = getattr(exc, "status", None)
        emit({"type": "discard", "attempt": attempt,
              "state": "interrupted" if reason in ("cancelled", "deadline") else "discarded",
              "reason": reason, "retrying": retrying,
              "status": status if type(status) is int else None, "usage": "unknown"})

    if policy.attempts < 1:
        raise RuntimeError("Invalid retry policy.")
    waits = 0  # retries so far that waited for the network
    for attempt in range(1, max(policy.attempts, policy.network_attempts) + 1):
        if cancel.is_set():
            raise InterruptedError("Stopped by you.")
        if deadline is not None and clock() >= deadline:
            raise TimeoutError("The run reached its time budget.")
        current = _Attempt()
        if on_event is not None:
            emit({"type": "begin", "attempt": attempt, "state": "open"})

        def collect(part, current=current, attempt=attempt):
            if not current.open:
                raise ProviderFailure("A finished model attempt cannot add text.")
            if cancel.is_set():
                raise InterruptedError("Stopped by you.")
            if deadline is not None and clock() >= deadline:
                current.expired = True
                raise ProviderFailure("The run reached its time budget.")
            if not isinstance(part, str):
                raise ProviderFailure("Provider returned an invalid text fragment.")
            current.size += len(part.encode())
            if current.size > 8_000_000:
                raise ProviderFailure("Provider text exceeded its limit.")
            current.parts.append(part)
            if on_event is not None and part:
                emit({"type": "delta", "attempt": attempt, "state": "open", "text": part})

        if on_reasoning is not None:
            def reason(part, current=current, attempt=attempt):
                if not current.open or current.reasoning_broken or broken or cancel.is_set():
                    return
                if not isinstance(part, str) or not part or current.reasoning + len(part) > REASONING_ATTEMPT_CHARS:
                    return
                current.reasoning += len(part)
                try:
                    on_reasoning(attempt, part)
                except Exception:
                    current.reasoning_broken = True
            collect.reasoning = reason

        try:
            response = complete(messages, tools, cancel, collect, effort)
        except Exception as exc:
            current.open = False
            if on_failure:
                on_failure(exc, attempt)
            if cancel.is_set():
                discard(attempt, "cancelled", exc)
                raise InterruptedError("Stopped by you.") from None
            if broken:
                # However the provider wrapped it, a presentation failure is final.
                raise broken[0]
            network = waiting_for_network(exc)
            if not transient(exc) or attempt >= (policy.network_attempts if network else policy.attempts):
                discard(attempt, "deadline" if current.expired else "provider", exc)
                raise
            server_delay = getattr(exc, "retry_after", None)
            if server_delay is not None and server_delay > policy.max_server_delay:
                # Respect the server instead of retrying before its requested time.
                discard(attempt, "provider", exc)
                raise
            if network:
                waits += 1
                delay = min(policy.max_delay, policy.network_delay * 2 ** (waits - 1))
            else:
                delay = min(policy.max_delay, policy.initial_delay * 2 ** (attempt - 1))
            delay *= random.uniform(0.8, 1.2)
            delay = max(delay, server_delay or 0)
            if deadline is not None and clock() + delay >= deadline:
                discard(attempt, "deadline", exc)
                raise TimeoutError("The run reached its time budget while reconnecting.") from None
            discard(attempt, "provider", exc, retrying=True)
            if on_retry:
                on_retry(attempt, delay, getattr(exc, "status", None))
            if cancel.wait(delay):
                raise InterruptedError("Stopped by you.")
            continue
        current.open = False
        if broken:
            # The provider swallowed a presentation failure; its response is unused.
            if on_failure:
                on_failure(broken[0], attempt)
            raise broken[0]
        if cancel.is_set():
            discard(attempt, "cancelled")
            raise InterruptedError("Stopped by you.")
        if deadline is not None and clock() >= deadline:
            discard(attempt, "deadline")
            raise TimeoutError("The run reached its time budget.")
        if on_event is not None:
            try:
                emit({"type": "commit", "attempt": attempt, "state": "committed", "bytes": current.size,
                      "sha256": hashlib.sha256("".join(current.parts).encode()).hexdigest()})
            except PresentationFailure as failure:
                if on_failure:
                    on_failure(failure, attempt)
                raise
        for part in current.parts:
            token(part)
        return response
    raise RuntimeError("Invalid retry policy.")
