"""Pinned child inference, isolated cloud transports, and owned inference lanes.

The manager must also admit every root inference attempt through the same lane.
This module never selects a profile, writes settings, or loads another local
worker. Provider retries belong to the executor's existing retry boundary.
usage_probe/attempt_usage attribute a provider usage report to exactly the one
attempt that produced it; nothing here prices or estimates usage.
"""

import copy
import errno
import http.client
import selectors
import socket
import ssl
import threading
import time
import urllib.request
from collections import deque
from contextlib import contextmanager, nullcontext
from pathlib import Path

import certifi

from .capabilities import capabilities, choose
from .providers import NoRedirect, Providers


class ProviderProfileChanged(ValueError):
    """The native selected route or its configuration no longer matches a pin."""


USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_tokens")


def usage_probe(provider):
    """Mark a provider's usage report state immediately before one request.

    PinnedProvider counts its requests. Other providers (the root Providers
    facade, fixtures) report an attempt by binding a fresh ``last_usage``
    mapping during it; an unchanged binding means that attempt reported nothing.
    """
    serial = getattr(provider, "usage_serial", None)
    if type(serial) is int:
        return ("serial", serial)
    return ("binding", getattr(provider, "last_usage", None))


def attempt_usage(provider, probe):
    """The usage reported by exactly the probed attempt, or None if it reported none.

    Only the token counts are kept. A stale report from an earlier attempt,
    another actor's provider or an ambiguous overlap is never attributed here.
    The caller validates the counts; malformed values are not repaired.
    """
    kind, before = probe
    if kind == "serial":
        if getattr(provider, "usage_serial", None) != before + 1:
            return None
        usage = provider.last_usage
    else:
        usage = getattr(provider, "last_usage", None)
        if usage is before:
            return None
    if isinstance(usage, dict):
        return {key: usage[key] for key in USAGE_FIELDS if key in usage} or None
    return usage


class InferenceLane:
    """FIFO admission: one resident local request, at most three cloud requests.

    Ownership covers startup, token counting, context callbacks, generation and
    usage capture. Cancellation of a waiter cannot reach the current worker.
    Local and cloud queues are independent; owners may hold only one admission.
    """

    def __init__(self, cloud_limit=3):
        if type(cloud_limit) is not int or not 1 <= cloud_limit <= 3:
            raise ValueError("Cloud inference limit must be between one and three.")
        self.cloud_limit = cloud_limit
        self._condition = threading.Condition(threading.RLock())
        self._queues = {"local": deque(), "cloud": deque()}
        self._owners = {"local": set(), "cloud": set()}

    @contextmanager
    def admit(self, owner_id, profile, cancel):
        kind = "local" if not profile or profile.get("kind") == "gguf" else "cloud"
        with self._admission(kind, owner_id, cancel):
            yield

    def local(self, owner_id, cancel):
        return self._admission("local", owner_id, cancel)

    def cloud(self, owner_id, cancel):
        return self._admission("cloud", owner_id, cancel)

    @contextmanager
    def _admission(self, kind, owner_id, cancel):
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("Inference requires an owner ID.")
        ticket = object()
        admitted = False
        with self._condition:
            if cancel.is_set():
                raise InterruptedError("Stopped by you.")
            if any(owner_id in owners for owners in self._owners.values()) or any(
                any(owner == owner_id for _, owner in queue)
                for queue in self._queues.values()
            ):
                raise RuntimeError("An inference owner already has an active admission.")
            queue = self._queues[kind]
            queue.append((ticket, owner_id))
            try:
                limit = 1 if kind == "local" else self.cloud_limit
                while True:
                    if cancel.is_set():
                        raise InterruptedError("Stopped by you.")
                    if queue[0][0] is ticket and len(self._owners[kind]) < limit:
                        queue.popleft()
                        self._owners[kind].add(owner_id)
                        admitted = True
                        self._condition.notify_all()
                        break
                    # External cancellation events do not signal this condition.
                    self._condition.wait(0.05)
            finally:
                if not admitted:
                    queue.remove((ticket, owner_id))
                    self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._owners[kind].remove(owner_id)
                self._condition.notify_all()

    def wake(self):
        with self._condition:
            self._condition.notify_all()

    def owns_local(self, owner_id):
        with self._condition:
            return owner_id in self._owners["local"]

    def cancel_local(self, owner_id, callback):
        """Check and interrupt atomically, before this owner can release its lane."""
        with self._condition:
            if owner_id not in self._owners["local"]:
                return False
            callback()
            return True


class _Cancellation:
    def __init__(self, external, owned):
        self.external, self.owned = external, owned

    def is_set(self):
        return self.external.is_set() or self.owned.is_set()


def _connect_settled(sock, timeout):
    """Whether a non-blocking connect finished (succeeded or failed) within timeout.

    Not select(): it refuses descriptors numbered 1024 (FD_SETSIZE) or above, which a busy host reaches.
    A failed connect also reports writable, so SO_ERROR then tells the outcome.
    """
    with selectors.DefaultSelector() as selector:
        selector.register(sock, selectors.EVENT_WRITE)
        return bool(selector.select(timeout))


def _selected_profile(app):
    """Read the selected profile without Providers.profiles' migration writes."""
    store = app.store
    with getattr(store, "lock", nullcontext()):
        selected = store.setting("active_profile", "default-local")
        profile = next((copy.deepcopy(p) for p in store.setting("model_profiles", [])
                        if p.get("id") == selected), None)
        path = store.setting("model_path", "")
    if profile is None and selected == "default-local" and path:
        profile = {"id": "default-local", "kind": "gguf", "name": Path(path).stem,
                   "path": path, "context": 4096, "tools": True}
    if not profile:
        return None
    if profile["kind"] == "gguf" and path != profile.get("path"):
        return None
    return {**profile,
            "connection_id": profile.get("connection_id", "local" if profile["kind"] == "gguf" else profile["id"]),
            "provider_name": profile.get("provider_name", "On this device" if profile["kind"] == "gguf" else profile["name"]),
            **capabilities(profile)}


class PinnedProvider:
    """Provider-compatible surface for one child, pinned to native configuration.

    Public methods deliberately omit provider/model/preference mutation. Each
    child is sequential; independent children may stream concurrently in cloud
    lanes. ``cancel`` and ``close`` permanently stop this child provider.
    """

    def __init__(self, app, profile, owner_id, lane, observer=None):
        if not isinstance(profile, dict) or profile.get("kind") not in {
            "gguf", "openai", "responses", "anthropic", "chatgpt"
        } or not isinstance(profile.get("id"), str):
            raise ValueError("Choose a model profile before starting a child.")
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("Inference requires an owner ID.")
        self._app, self._profile = app, copy.deepcopy(profile)
        self._owner_id, self._lane = owner_id, lane
        self._observer = observer
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._active = False
        self._active_cancel = None
        self._socket = None
        self._last_usage = {}
        self._usage_serial = 0
        self._local = app.model.local
        self._backend = None
        self._verify_pin()
        if profile["kind"] != "gguf":
            # Providers creates only an unloaded Model facade. Discard that
            # facade; cloud children never load, cancel, or expose a local Model.
            backend = Providers(app.home, app.resources, app.store, lambda *args: None)
            backend.local = None
            backend.current = self.current
            backend.auth = app.model.auth  # ChatGPT.access owns its refresh lock.
            backend.context_observer = self._observe
            backend.context_transform = None
            backend.http = self._opener()
            # Phase 22: this transport publishes each socket before connect, so cancellation already interrupts a
            # stalled open on the request's own thread; a helper thread could publish a stale socket into a later
            # request. The stream watchdog still applies from the response on (recovery.StreamWatchdog).
            backend.watched_open = False
            self._backend = backend

    def current(self):
        return copy.deepcopy(self._profile)

    @property
    def last_usage(self):
        with self._lock:
            return copy.deepcopy(self._last_usage)

    @property
    def usage_serial(self):
        """Requests started on this provider; ``last_usage`` belongs to the latest."""
        with self._lock:
            return self._usage_serial

    def _verify_pin(self):
        if _selected_profile(self._app) != self._profile:
            raise ProviderProfileChanged("The selected model profile changed. Start a new child.")

    def _observe(self, manifest):
        if self._observer:
            self._observer(copy.deepcopy(manifest))

    def _track_socket(self, sock):
        with self._lock:
            self._socket = sock
            if self._stop.is_set() or (self._active_cancel and self._active_cancel.is_set()):
                self._shutdown(sock)
                raise InterruptedError("Stopped by you.")

    @staticmethod
    def _shutdown(sock):
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _opener(self):
        owner = self

        class Connection(http.client.HTTPConnection):
            def connect(connection):
                if owner._active_cancel.is_set():
                    raise InterruptedError("Stopped by you.")
                last_error = None
                # Publish each socket before connect or response headers, so a
                # cancellation racing network startup interrupts only this child.
                for family, kind, proto, _, address in owner._addresses(
                    connection.host, connection.port, connection.timeout
                ):
                    sock = socket.socket(family, kind, proto)
                    connection.sock = sock
                    try:
                        if connection.source_address:
                            sock.bind(connection.source_address)
                        owner._track_socket(sock)
                        sock.setblocking(False)
                        error = sock.connect_ex(address)
                        pending = {errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK, errno.EINTR}
                        deadline = time.monotonic() + connection.timeout
                        while error in pending:
                            if owner._active_cancel.is_set():
                                raise InterruptedError("Stopped by you.")
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Provider connection timed out.")
                            if _connect_settled(sock, 0.05):
                                error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        if error:
                            raise OSError(error, "Provider connection failed.")
                        sock.settimeout(connection.timeout)
                        owner._track_socket(sock)
                        return
                    except OSError as exc:
                        last_error = exc
                        sock.close()
                        if owner._active_cancel.is_set():
                            raise InterruptedError("Stopped by you.") from None
                if last_error:
                    raise last_error
                raise OSError("Provider address could not be resolved.")

        class SecureConnection(Connection):
            default_port = http.client.HTTPS_PORT

            def connect(connection):
                super().connect()
                context = ssl.create_default_context(cafile=certifi.where())
                connection.sock = context.wrap_socket(connection.sock,
                    server_hostname=connection.host, do_handshake_on_connect=False)
                owner._track_socket(connection.sock)
                connection.sock.do_handshake()
                owner._track_socket(connection.sock)

        class HTTPHandler(urllib.request.HTTPHandler):
            def http_open(handler, request):
                return handler.do_open(Connection, request)

        class HTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(handler, request):
                return handler.do_open(SecureConnection, request)

        return urllib.request.build_opener(urllib.request.ProxyHandler({}),
            NoRedirect(), HTTPHandler(), HTTPSHandler())

    def _addresses(self, host, port, timeout):
        # System DNS itself has no cancellation API. Resolve off the request
        # thread, ignore a late result after cancellation, and open no socket.
        ready, result = threading.Event(), []

        def resolve():
            try:
                result.append(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
            except Exception as exc:
                result.append(exc)
            finally:
                ready.set()

        threading.Thread(target=resolve, daemon=True, name="pebble-child-dns").start()
        deadline = time.monotonic() + timeout
        while not ready.wait(0.05):
            if self._active_cancel.is_set():
                raise InterruptedError("Stopped by you.")
            if time.monotonic() >= deadline:
                raise TimeoutError("Provider resolution timed out.")
        if self._active_cancel.is_set():
            raise InterruptedError("Stopped by you.")
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    def _interrupt(self, expected=None):
        self._lane.wake()
        if self._profile["kind"] == "gguf":
            def cancel_owned():
                with self._lock:
                    if self._active and (expected is None or expected is self._active_cancel):
                        self._local.cancel()
            self._lane.cancel_local(self._owner_id, cancel_owned)
        else:
            with self._lock:
                if expected is None or expected is self._active_cancel:
                    self._shutdown(self._socket)

    def cancel(self):
        self._stop.set()
        self._interrupt()

    def close(self):
        self.cancel()

    def complete(self, messages, tools, cancel, token, deliberate=False):
        combined = _Cancellation(cancel, self._stop)
        with self._lock:
            if self._active:
                raise RuntimeError("A child provider already has an active request.")
            if combined.is_set():
                raise InterruptedError("Stopped by you.")
            self._active = True
            self._active_cancel = combined
            self._last_usage = {}
            self._usage_serial += 1
        done = threading.Event()

        def watch_cancel():
            while not done.wait(0.05):
                if combined.is_set():
                    self._interrupt(combined)
                    return

        watcher = threading.Thread(target=watch_cancel, daemon=True,
                                   name="pebble-child-cancel")
        watcher.start()
        try:
            with self._lane.admit(self._owner_id, self._profile, combined):
                self._verify_pin()  # Check after waiting, before callbacks/request.
                if combined.is_set():
                    raise InterruptedError("Stopped by you.")
                if self._profile["kind"] == "gguf":
                    local = self._local
                    old_observer = getattr(local, "context_observer", None)
                    old_transform = getattr(local, "context_transform", None)
                    local.context_observer, local.context_transform = self._observe, None
                    local.last_usage = {}
                    try:
                        result = local.complete(messages, tools, combined, token,
                            choose(self._profile, deliberate) == "think")
                    finally:
                        with self._lock:
                            self._last_usage = copy.deepcopy(local.last_usage)
                        local.context_observer, local.context_transform = old_observer, old_transform
                else:
                    backend = self._backend
                    backend.last_usage = {}
                    try:
                        result = backend.complete(messages, tools, combined, token, deliberate)
                    finally:
                        with self._lock:
                            self._last_usage = copy.deepcopy(backend.last_usage)
                if combined.is_set():
                    raise InterruptedError("Stopped by you.")
                return result
        except Exception:
            if combined.is_set():
                raise InterruptedError("Stopped by you.") from None
            raise
        finally:
            done.set()
            watcher.join(timeout=0.2)
            with self._lock:
                self._socket = None
                self._active_cancel = None
                self._active = False
