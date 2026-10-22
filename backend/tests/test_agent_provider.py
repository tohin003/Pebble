"""Owned local fixtures and genuine loopback provider streams (no live billing)."""

import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from pebble.agent_provider import InferenceLane, PinnedProvider, ProviderProfileChanged
from pebble.capabilities import capabilities
from pebble.chatgpt import ChatGPT
from pebble.providers import Providers
from pebble.recovery import ProviderFailure


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Fixture did not reach the expected state.")


class MemoryStore:
    def __init__(self, profile):
        self.lock = threading.RLock()
        self.settings = {"active_profile": profile["id"], "model_profiles": [copy.deepcopy(profile)]}
        if profile["kind"] == "gguf":
            self.settings["model_path"] = profile["path"]
        self.writes = 0

    def setting(self, key, default=None):
        with self.lock:
            return copy.deepcopy(self.settings.get(key, default))

    def set_setting(self, key, value):
        with self.lock:
            self.settings[key] = copy.deepcopy(value)
            self.writes += 1


def cloud_app(tmp_path, url, kind="responses"):
    profile = {"id": "native-profile", "kind": kind, "model": "fixture-model", "name": "Fixture",
               "provider_name": "Fixture", "connection_id": "native-connection", "url": url + "/v1",
               "tools": True, "context": 8192, "budget": 8192, "context_mode": "full",
               "budget_version": 2, "context_version": 1, "has_key": False,
               "max_output_tokens": 512,
               "reasoning_capabilities": {"parameter": "reasoning" if kind == "responses" else "reasoning_effort",
                                          "levels": ["low", "high"], "default": "low"}}
    store = MemoryStore(profile)
    app = SimpleNamespace(home=tmp_path, resources=tmp_path / "resources", store=store)
    app.model = Providers(app.home, app.resources, store, lambda *args: None)
    app.model.last_usage = {"input_tokens": 9999}
    app.model.response = object()
    return app, app.model.current()


class RequestJob:
    def __init__(self, provider, label, effort=False):
        self.result = None
        self.error = None
        self.tokens = []
        self.cancel_event = threading.Event()

        def run():
            try:
                self.result = provider.complete(
                    [{"role": "user", "content": label, "_pebble_current": True}],
                    [], self.cancel_event, self.tokens.append, effort)
            except Exception as exc:
                self.error = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self):
        self.thread.join(3)
        assert not self.thread.is_alive(), "Inference did not stop or finish promptly."
        return self


@pytest.fixture
def stream_server():
    started, releases, before_headers = {}, {}, set()
    records = []
    lock = threading.RLock()

    def started_event(label):
        with lock:
            return started.setdefault(label, threading.Event())

    def release_event(label):
        with lock:
            return releases.setdefault(label, threading.Event())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = body.get("messages", body.get("input", []))
            label = messages[-1]["content"]
            if isinstance(label, list):
                label = label[-1]["text"]
            with lock:
                records.append((label, self.path, body))
            try:
                if label in before_headers:
                    started_event(label).set()
                    release_event(label).wait(3)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def emit(event):
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                    self.wfile.flush()

                usage = {"input_tokens": 100 + len(label), "output_tokens": len(label)}
                text = "reply:" + label
                if self.path.endswith("/responses"):
                    emit({"type": "response.output_text.delta", "delta": text})
                elif self.path.endswith("/messages"):
                    emit({"type": "message_start", "message": {"usage": usage}})
                    emit({"type": "content_block_start", "index": 0,
                          "content_block": {"type": "thinking", "thinking": "private:" + label}})
                    emit({"type": "content_block_start", "index": 1,
                          "content_block": {"type": "text", "text": ""}})
                    emit({"type": "content_block_delta", "index": 1,
                          "delta": {"type": "text_delta", "text": text}})
                else:
                    emit({"choices": [{"delta": {"content": text}}]})
                started_event(label).set()
                release_event(label).wait(3)
                if label == "incomplete":
                    return
                if self.path.endswith("/responses"):
                    emit({"type": "response.completed", "response": {
                        "status": "completed", "usage": usage,
                        "output": [{"type": "reasoning", "encrypted_content": "opaque:" + label},
                                   {"type": "message", "content": [{"type": "output_text", "text": text}]}]}})
                elif self.path.endswith("/messages"):
                    emit({"type": "message_stop"})
                else:
                    emit({"choices": [], "usage": {"prompt_tokens": usage["input_tokens"],
                                                    "completion_tokens": usage["output_tokens"]}})
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture = SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", records=records,
        started=started_event, release=release_event, before_headers=before_headers)
    yield fixture
    with lock:
        for event in releases.values():
            event.set()
    server.shutdown()
    server.server_close()
    thread.join(2)


def pin(app, profile, owner, lane, observer=None):
    provider = PinnedProvider(app, profile, owner, lane, observer)
    if provider._backend:
        # Keep this fixture offline; context assembly itself remains real.
        provider._backend.token_budget.count = lambda messages, tools=(): 80
    return provider


@pytest.mark.parametrize("kind", ["openai", "responses", "anthropic"])
def test_real_cloud_stream_cancel_isolated_from_sibling_and_root(tmp_path, stream_server, monkeypatch, kind):
    app, profile = cloud_app(tmp_path, stream_server.url, kind)
    monkeypatch.setattr("pebble.model.Model.load", lambda *args: pytest.fail("Cloud child loaded a local worker."))
    lane = InferenceLane()
    one = pin(app, profile, "one", lane)
    two = pin(app, profile, "two", lane)
    assert one._backend.http is not two._backend.http
    root_response = app.model.response
    a, b = RequestJob(one, "alpha"), RequestJob(two, "bravo-long")
    try:
        assert stream_server.started("alpha").wait(2)
        assert stream_server.started("bravo-long").wait(2)
        wait_for(lambda: one._backend.response is not None and two._backend.response is not None)
        b_response = two._backend.response
        assert one._backend.response is not b_response
        one.cancel()
        a.join()
        assert isinstance(a.error, InterruptedError)
        assert two._backend.response is b_response and not b_response.closed
        stream_server.release("bravo-long").set()
        b.join()
        assert b.error is None and b.result["content"] == "reply:bravo-long"
        assert two.last_usage["input_tokens"] == 110
        assert one.last_usage == {}
        assert app.model.last_usage == {"input_tokens": 9999}
        assert app.model.response is root_response
    finally:
        one.close()
        two.close()
        stream_server.release("alpha").set()
        stream_server.release("bravo-long").set()
        a.join()
        b.join()


def test_cloud_projection_reasoning_usage_and_profiles_are_independent(tmp_path, stream_server):
    app, profile = cloud_app(tmp_path, stream_server.url)
    initial_settings = copy.deepcopy(app.store.settings)
    parent_observations = []
    app.model.context_observer = parent_observations.append
    app.model.context_transform = lambda *args: pytest.fail("Child invoked parent compaction.")
    observations = {"a": [], "b": []}
    lane = InferenceLane()
    one = pin(app, profile, "one", lane, observations["a"].append)
    two = pin(app, profile, "two", lane, observations["b"].append)
    profile["reasoning_capabilities"]["levels"].clear()
    copied = one.current()
    copied["model"] = "changed"
    copied["reasoning_capabilities"]["levels"].clear()
    a, b = RequestJob(one, "alpha", "low"), RequestJob(two, "bravo-long", "high")
    try:
        assert stream_server.started("alpha").wait(2)
        assert stream_server.started("bravo-long").wait(2)
        stream_server.release("alpha").set()
        stream_server.release("bravo-long").set()
        a.join()
        b.join()
        assert a.error is None and b.error is None
        assert a.result["_responses_output"][0]["encrypted_content"] == "opaque:alpha"
        assert b.result["_responses_output"][0]["encrypted_content"] == "opaque:bravo-long"
        bodies = {label: body for label, _, body in stream_server.records}
        assert bodies["alpha"]["reasoning"] == {"effort": "low"}
        assert bodies["bravo-long"]["reasoning"] == {"effort": "high"}
        assert "_pebble_" not in json.dumps(bodies)
        assert len(observations["a"]) == len(observations["b"]) == 1
        assert observations["a"][0] is not observations["b"][0]
        observations["a"][0]["limits"]["context"] = -1
        assert observations["b"][0]["limits"]["context"] > 0
        assert not parent_observations
        assert one.current()["model"] == "fixture-model"
        assert one.current()["connection_id"] == "native-connection"
        assert one.current()["reasoning_capabilities"]["levels"] == ["low", "high"]
        assert one.last_usage["output_tokens"] == 5 and two.last_usage["output_tokens"] == 10
        usage = one.last_usage
        usage["input_tokens"] = -1
        assert one.last_usage["input_tokens"] == 105
        assert app.store.settings == initial_settings and app.store.writes == 0
    finally:
        one.close()
        two.close()


@pytest.mark.parametrize("external", [False, True])
def test_cloud_cancel_during_response_header_startup(tmp_path, stream_server, external):
    stream_server.before_headers.add("startup")
    app, profile = cloud_app(tmp_path, stream_server.url)
    provider = pin(app, profile, "startup-owner", InferenceLane())
    job = RequestJob(provider, "startup")
    try:
        assert stream_server.started("startup").wait(2)
        assert provider._backend.response is None
        if external:
            job.cancel_event.set()
        else:
            provider.cancel()
        job.join()
        assert isinstance(job.error, InterruptedError)
        assert provider.last_usage == {}
    finally:
        stream_server.release("startup").set()
        provider.close()
        job.join()


def test_cancel_during_dns_ignores_late_resolution_and_opens_no_request(tmp_path, stream_server, monkeypatch):
    app, profile = cloud_app(tmp_path, stream_server.url)
    provider = pin(app, profile, "dns-owner", InferenceLane())
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    import socket
    real_resolve = socket.getaddrinfo

    def resolve(*args, **kwargs):
        entered.set()
        release.wait(3)
        try:
            return real_resolve(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr("pebble.agent_provider.socket.getaddrinfo", resolve)
    job = RequestJob(provider, "dns")
    try:
        assert entered.wait(2)
        provider.cancel()
        job.join()
        assert isinstance(job.error, InterruptedError) and not stream_server.records
        release.set()
        assert finished.wait(2)
        assert not stream_server.records
    finally:
        release.set()
        provider.close()
        job.join()


def test_https_without_explicit_port_uses_443_and_no_live_connection(tmp_path, monkeypatch):
    app, profile = cloud_app(tmp_path, "https://127.0.0.1", "openai")
    provider = pin(app, profile, "https-owner", InferenceLane())
    ports = []

    def stop_before_connect(host, port, *args):
        assert host == "127.0.0.1"
        ports.append(port)
        raise OSError("Owned fixture stops before any external connection.")

    monkeypatch.setattr("pebble.agent_provider.socket.getaddrinfo", stop_before_connect)
    job = RequestJob(provider, "https").join()
    assert isinstance(job.error, ProviderFailure)
    assert ports == [443]
    provider.close()


def test_chatgpt_root_and_children_share_only_locked_credential_refresh(tmp_path, stream_server):
    app, profile = cloud_app(tmp_path, stream_server.url, "chatgpt")
    auth = ChatGPT(app.model)
    app.model.auth = auth
    refreshed = []

    class Records:
        value = {"expires_at": 0, "refresh_token": "fixture-rotation", "client_id": "fixture-client"}

        def get(self, name):
            assert name == "oauth:native-connection"
            return copy.deepcopy(self.value)

        def set(self, name, value):
            self.value = copy.deepcopy(value)

    auth.records = Records()
    auth.config = lambda: {"token_endpoint": "https://auth.openai.com/fixture"}

    def refresh(*args):
        refreshed.append(True)
        time.sleep(0.03)
        return {"access_token": "fixture-access", "refresh_token": "fixture-rotated", "expires_in": 600,
                "token_type": "Bearer", "scope": "chatgpt.tokens.use.direct resource.invoke"}

    auth.http_json = refresh
    lane = InferenceLane()
    providers = [pin(app, profile, str(i), lane) for i in range(2)]
    assert all(provider._backend.auth is auth for provider in providers)
    results = []
    callbacks = [lambda: auth.access("native-connection")] + [
        lambda provider=provider: provider._backend.key(provider.current()) for provider in providers]
    threads = [threading.Thread(target=lambda callback=callback: results.append(callback())) for callback in callbacks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert results == ["fixture-access"] * 3 and refreshed == [True]
    assert not stream_server.records
    assert app.model.last_usage == {"input_tokens": 9999}


def test_failed_attempt_clears_previous_usage_and_does_not_retry(tmp_path, stream_server):
    app, profile = cloud_app(tmp_path, stream_server.url)
    provider = pin(app, profile, "owner", InferenceLane())
    try:
        stream_server.release("alpha").set()
        first = RequestJob(provider, "alpha").join()
        assert first.error is None and provider.last_usage["input_tokens"] == 105
        stream_server.release("incomplete").set()
        second = RequestJob(provider, "incomplete").join()
        assert isinstance(second.error, ProviderFailure) and second.error.retryable
        assert provider.last_usage == {}
        assert len(stream_server.records) == 2
    finally:
        provider.close()


def test_cloud_lane_bounds_three_real_streams_and_admits_fifo(tmp_path, stream_server):
    app, profile = cloud_app(tmp_path, stream_server.url)
    lane = InferenceLane()
    providers = [pin(app, profile, str(i), lane) for i in range(5)]
    jobs = []
    try:
        for i in range(3):
            jobs.append(RequestJob(providers[i], f"cloud-{i}"))
            assert stream_server.started(f"cloud-{i}").wait(2)
        jobs.append(RequestJob(providers[3], "cloud-3"))
        wait_for(lambda: len(lane._queues["cloud"]) == 1)
        jobs.append(RequestJob(providers[4], "cloud-4"))
        wait_for(lambda: len(lane._queues["cloud"]) == 2)
        assert len(stream_server.records) == 3
        providers[0].cancel()
        jobs[0].join()
        assert stream_server.started("cloud-3").wait(2)
        assert not stream_server.started("cloud-4").is_set()
        stream_server.release("cloud-1").set()
        jobs[1].join()
        assert stream_server.started("cloud-4").wait(2)
        assert [label for label, _, _ in stream_server.records] == [f"cloud-{i}" for i in range(5)]
    finally:
        for i, provider in enumerate(providers):
            stream_server.release(f"cloud-{i}").set()
            provider.close()
        for job in jobs:
            job.join()


class OwnedLocal:
    def __init__(self, lane):
        self.lane = lane
        self.started, self.releases = {}, {}
        self.last_usage = {"input_tokens": 9999}
        self.order, self.cancelled = [], []
        self.active = None
        self.running, self.max_running, self.worker_starts = 0, 0, 0
        self.resident = False
        self.context_observer = lambda manifest: None
        self.context_transform = lambda *args: args[0]

    def event(self, collection, owner):
        return collection.setdefault(owner, threading.Event())

    def complete(self, messages, tools, cancel, token, deliberate=False):
        owner = messages[-1]["content"]
        assert self.lane.owns_local(owner)
        if not self.resident:
            self.worker_starts += 1
            self.resident = True
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.active = owner
        self.order.append(owner)
        try:
            self.context_observer({"fixture_owner": owner})
            if owner != "root":
                assert self.context_transform is None
            self.event(self.started, owner).set()
            # Like a blocked streaming read, this fixture requires the owned
            # cancellation callback to wake it before inspecting the event.
            assert self.event(self.releases, owner).wait(3)
            if owner == "failure":
                raise ProviderFailure("Local fixture failed.", retryable=True)
            if cancel.is_set():
                raise InterruptedError("Stopped by you.")
            self.last_usage = {"input_tokens": len(owner), "output_tokens": 1}
            token(owner)
            return {"role": "assistant", "content": owner}
        finally:
            self.running -= 1
            self.active = None

    def cancel(self):
        self.cancelled.append(self.active)
        if self.active:
            self.event(self.releases, self.active).set()


def local_app(tmp_path, lane):
    profile = {"id": "local-profile", "kind": "gguf", "name": "Qwen3.5 Fixture",
               "path": str(tmp_path / "fixture.gguf"), "context": 4096, "tools": True}
    app = SimpleNamespace(home=tmp_path, resources=tmp_path / "resources", store=MemoryStore(profile))
    app.model = SimpleNamespace(local=OwnedLocal(lane), auth=None)
    return app, {**profile, "connection_id": "local", "provider_name": "On this device", **capabilities(profile)}


def root_job(app, profile, lane):
    class Root:
        def complete(self, messages, tools, cancel, token, deliberate=False):
            with lane.admit("root", profile, cancel):
                return app.model.local.complete(messages, tools, cancel, token, deliberate)

    return RequestJob(Root(), "root")


def test_local_root_and_children_share_one_fair_lane_and_restore_callbacks(tmp_path):
    lane = InferenceLane()
    app, profile = local_app(tmp_path, lane)
    local = app.model.local
    original_observer, original_transform = local.context_observer, local.context_transform
    observed_a, observed_b = [], []
    a = pin(app, profile, "child-a", lane, observed_a.append)
    b = pin(app, profile, "child-b", lane, observed_b.append)
    assert a._local is b._local is local and a._backend is None
    root = root_job(app, profile, lane)
    assert local.event(local.started, "root").wait(2)
    first = RequestJob(a, "child-a")
    wait_for(lambda: len(lane._queues["local"]) == 1)
    second = RequestJob(b, "child-b")
    wait_for(lambda: len(lane._queues["local"]) == 2)
    local.event(local.releases, "root").set()
    root.join()
    assert local.event(local.started, "child-a").wait(2)
    assert not local.event(local.started, "child-b").is_set()
    local.event(local.releases, "child-a").set()
    first.join()
    assert local.event(local.started, "child-b").wait(2)
    assert a.last_usage == {"input_tokens": 7, "output_tokens": 1}
    local.event(local.releases, "child-b").set()
    second.join()
    assert root.error is first.error is second.error is None
    assert local.order == ["root", "child-a", "child-b"]
    assert local.worker_starts == local.max_running == 1
    assert observed_a == [{"fixture_owner": "child-a"}]
    assert observed_b == [{"fixture_owner": "child-b"}]
    assert local.context_observer is original_observer and local.context_transform is original_transform
    local.last_usage["input_tokens"] = -1
    assert b.last_usage["input_tokens"] == 7
    assert app.store.writes == 0


def test_cancelling_queued_local_child_cannot_interrupt_root_or_sibling(tmp_path):
    lane = InferenceLane()
    app, profile = local_app(tmp_path, lane)
    local = app.model.local
    root = root_job(app, profile, lane)
    assert local.event(local.started, "root").wait(2)
    queued = pin(app, profile, "queued", lane)
    sibling = pin(app, profile, "sibling", lane)
    waiting = RequestJob(queued, "queued")
    wait_for(lambda: len(lane._queues["local"]) == 1)
    next_job = RequestJob(sibling, "sibling")
    wait_for(lambda: len(lane._queues["local"]) == 2)
    queued.cancel()
    waiting.join()
    assert isinstance(waiting.error, InterruptedError)
    assert local.cancelled == [] and local.active == "root"
    local.event(local.releases, "root").set()
    root.join()
    assert local.event(local.started, "sibling").wait(2)
    queued.close()
    assert local.cancelled == [] and local.active == "sibling"
    local.event(local.releases, "sibling").set()
    next_job.join()
    assert root.error is next_job.error is None
    assert local.order == ["root", "sibling"] and local.worker_starts == 1


@pytest.mark.parametrize("external", [False, True])
def test_owned_local_cancellation_restores_context_and_never_targets_next_owner(tmp_path, external):
    lane = InferenceLane()
    app, profile = local_app(tmp_path, lane)
    local = app.model.local
    original_observer, original_transform = local.context_observer, local.context_transform
    provider = pin(app, profile, "child", lane)
    job = RequestJob(provider, "child")
    assert local.event(local.started, "child").wait(2)
    if external:
        job.cancel_event.set()
    else:
        provider.cancel()
    job.join()
    assert isinstance(job.error, InterruptedError)
    assert local.cancelled == ["child"]
    assert local.context_observer is original_observer and local.context_transform is original_transform
    assert not lane.owns_local("child")
    root = root_job(app, profile, lane)
    assert local.event(local.started, "root").wait(2)
    provider.close()
    assert local.cancelled == ["child"]
    assert not lane.cancel_local("child", local.cancel)
    local.event(local.releases, "root").set()
    root.join()
    assert root.error is None


def test_local_failure_restores_callbacks_and_discards_old_usage(tmp_path):
    lane = InferenceLane()
    app, profile = local_app(tmp_path, lane)
    local = app.model.local
    original_observer, original_transform = local.context_observer, local.context_transform
    local.event(local.releases, "failure").set()
    provider = pin(app, profile, "failure", lane)
    job = RequestJob(provider, "failure").join()
    assert isinstance(job.error, ProviderFailure)
    assert provider.last_usage == {}
    assert local.context_observer is original_observer and local.context_transform is original_transform
    assert not lane.owns_local("failure")


@pytest.mark.parametrize("field,value", [("connection_id", "other-connection"), ("budget", 6000), ("model", "other-model")])
def test_full_pin_freshness_rejected_before_cloud_request(tmp_path, stream_server, field, value):
    app, profile = cloud_app(tmp_path, stream_server.url)
    provider = pin(app, profile, "owner", InferenceLane())
    changed = app.store.setting("model_profiles")
    changed[0][field] = value
    app.store.set_setting("model_profiles", changed)
    job = RequestJob(provider, "alpha").join()
    assert isinstance(job.error, ProviderProfileChanged)
    assert not stream_server.records
    assert provider.current() == profile
    assert provider.last_usage == {}


def test_pin_checked_after_local_queue_admission_without_preference_writes(tmp_path):
    lane = InferenceLane()
    app, profile = local_app(tmp_path, lane)
    local = app.model.local
    root = root_job(app, profile, lane)
    assert local.event(local.started, "root").wait(2)
    provider = pin(app, profile, "child", lane)
    waiting = RequestJob(provider, "child")
    wait_for(lambda: len(lane._queues["local"]) == 1)
    app.store.set_setting("model_path", str(tmp_path / "changed.gguf"))
    local.event(local.releases, "root").set()
    root.join()
    waiting.join()
    assert isinstance(waiting.error, ProviderProfileChanged)
    assert local.order == ["root"] and app.store.writes == 1


def test_pre_cancelled_provider_and_concurrent_same_owner_are_rejected(tmp_path, stream_server):
    app, profile = cloud_app(tmp_path, stream_server.url)
    lane = InferenceLane()
    provider = pin(app, profile, "owner", lane)
    provider.cancel()
    job = RequestJob(provider, "alpha").join()
    assert isinstance(job.error, InterruptedError) and not stream_server.records
    with lane.local("root", threading.Event()):
        with pytest.raises(RuntimeError):
            with lane.cloud("root", threading.Event()):
                pytest.fail("Owner was admitted twice.")


@pytest.mark.parametrize("limit", [0, 4, True])
def test_lane_cannot_exceed_cloud_bound(limit):
    with pytest.raises(ValueError):
        InferenceLane(limit)
