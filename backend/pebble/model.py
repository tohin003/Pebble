import json
import copy
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
import http.client
from pathlib import Path
from .stats import usage_metrics
from .capabilities import capabilities


class ToolCallTruncated(ValueError):
    pass


class Model:
    def __init__(self, home, resources, store, emit):
        self.home, self.resources, self.store, self.emit = home, resources, store, emit
        self.process = None
        self.lock = threading.RLock()
        self.state = "unloaded"
        self.last_used = time.monotonic()
        self.busy = False
        self.key = secrets.token_urlsafe(32)
        self.base = None
        self.last_usage = {}
        self.active_socket = None
        self.cancel_event = None
        owner = self

        class Connection(http.client.HTTPConnection):
            def connect(connection):
                if owner.busy and owner.cancel_event and owner.cancel_event.is_set():
                    raise InterruptedError("Stopped by you.")
                super().connect()
                owner.active_socket = connection.sock
                if owner.busy and owner.cancel_event and owner.cancel_event.is_set():
                    connection.sock.close()
                    raise InterruptedError("Stopped by you.")

        class Handler(urllib.request.HTTPHandler):
            def http_open(handler, request):
                return handler.do_open(Connection, request)

        self.http = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), Handler()
        )

    def cancel(self):
        if self.active_socket:
            try:
                self.active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def status(self):
        path = self.store.setting("model_path", "")
        selected = self.store.setting('active_profile', 'default-local')
        p = next((p for p in self.store.setting('model_profiles', []) if p['id'] == selected and p['kind'] == 'gguf'), {'kind':'gguf','name':Path(path).stem if path else ''})
        return {
            "state": self.state,
            "configured": bool(path and Path(path).is_file()),
            "name": Path(path).stem if path else "Choose a local model",
            "path": path,
            "context": 4096,
            "kind": "gguf",
            **capabilities(p),
        }

    def unload(self):
        with self.lock:
            if self.process:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                self.process = None
            self.state = "unloaded"
            self.emit("model", self.status())

    def load(self, cancel=None):
        with self.lock:
            if self.process and self.process.poll() is None and self.state == "ready":
                return
            path = Path(self.store.setting("model_path", ""))
            if not path.is_file() or path.suffix.lower() != ".gguf":
                # Phase 24 (V1-038): plain words that name the two ways to connect a model.
                raise ValueError("Connect a model first: in Settings → Models, choose Continue with ChatGPT or "
                                 "Add a model file.")
            name = "llama-server.exe" if os.name == "nt" else "llama-server"
            binary = self.resources / "llama" / name
            if not binary.is_file():
                raise ValueError(
                    "The local inference runtime is missing. Rebuild or reinstall Pebble."
                )
            self.state = "loading"
            self.emit("model", self.status())
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            self.base = f"http://127.0.0.1:{port}"
            key_file = self.home / "runtime.key"
            key_file.write_text(self.key)
            key_file.chmod(0o600)
            log = (self.home / "model.log").open("w")
            env = dict(os.environ)
            env["DYLD_LIBRARY_PATH"] = str(binary.parent)
            cmd = [
                str(binary),
                "--model",
                str(path),
                "--alias",
                "pebble-local",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--ctx-size",
                "4096",
                "--parallel",
                "1",
                "--threads",
                "4",
                "--batch-size",
                "256",
                "--ubatch-size",
                "128",
                "--n-gpu-layers",
                "all",
                "--flash-attn",
                "on",
                "--jinja",
                "--no-webui",
                "--reasoning-budget",
                "512",
                "--api-key-file",
                str(key_file),
            ]
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=log,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            finally:
                log.close()
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if cancel and cancel.is_set():
                    self.unload()
                    raise InterruptedError("Stopped by you.")
                if self.process.poll() is not None:
                    self.state = "error"
                    self.emit("model", self.status())
                    raise ValueError(
                        "Model could not start. Check the local model log in Settings."
                    )
                try:
                    with self.http.open(self.base + "/health", timeout=1) as response:
                        if response.status == 200:
                            self.state = "ready"
                            self.emit("model", self.status())
                            return
                except OSError:
                    time.sleep(0.25)
            self.unload()
            raise TimeoutError(
                "Model startup timed out. Close other memory-heavy apps and try again."
            )

    def json_request(self, path, body):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.key,
            },
        )
        with self.http.open(req, timeout=15) as response:
            return json.load(response)

    def budget(self, body, cancel=None):
        """Project whole exchanges using the actual local chat template count."""
        from .context_manifest import assemble_context, ContextBudgetExceeded
        body = copy.deepcopy(body)
        requested = body['max_tokens']
        reserve = min(requested, 1200)

        def wire(messages):
            return [{k: v for k, v in m.items() if not k.startswith('_pebble_')} for m in messages]

        def count(messages, tools):
            if cancel and cancel.is_set():
                raise InterruptedError('Stopped by you.')
            candidate = {**body, 'messages': wire(messages)}
            if tools:
                candidate['tools'] = tools
            formatted = self.json_request('/apply-template', candidate)['prompt']
            return len(self.json_request('/tokenize', {'content': formatted, 'add_special': False})['tokens'])

        observer = getattr(self, 'context_observer', None)
        try:
            projection = assemble_context(body['messages'], body.get('tools', []), 4000,
                reserve, counter=count, counter_method='llama_template_tokens')
            transform = getattr(self, 'context_transform', None)
            if transform:
                projection = transform(projection, body['messages'], body.get('tools', []), 4000,
                    reserve, count, 'llama_template_tokens')
        except ContextBudgetExceeded as exc:
            if observer:
                observer(exc.manifest)
            raise ContextBudgetExceeded('This request is too large for the 4K local context. Use a focused source excerpt or a model with more context.', exc.manifest) from None
        used = projection.manifest['input_tokens']['after']
        body['max_tokens'] = min(requested, 4000 - used)
        projection.manifest['limits'].update(output_reserve=body['max_tokens'], input=4000 - body['max_tokens'])
        projection.manifest['local_output_floor'] = reserve
        if observer:
            observer(projection.manifest)
        body['messages'] = wire(projection.messages)
        return body

    def complete(self, messages, tools, cancel, token, deliberate=False):
        self.load(cancel)
        if cancel.is_set():
            raise InterruptedError("Stopped by you.")
        self.busy = True
        self.cancel_event = cancel
        self.last_used = time.monotonic()
        try:
            body = {
                "model": "pebble-local",
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": 1800 if any(t['function']['name'] in {'Write', 'Append', 'UIRender'} for t in tools) else (1024 if deliberate else 768),
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "chat_template_kwargs": {"enable_thinking": deliberate},
            }
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
            body = self.budget(body, cancel)
            req = urllib.request.Request(
                self.base + "/v1/chat/completions",
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self.key,
                },
            )
            content, calls = "", {}
            usage, timings, first = {}, {}, None
            with self.http.open(req, timeout=120) as response:
                for raw in response:
                    if cancel.is_set():
                        raise InterruptedError("Stopped by you.")
                    if not raw.startswith(b"data: ") or raw.strip() == b"data: [DONE]":
                        continue
                    event = json.loads(raw[6:])
                    if event.get("error"):
                        raise ValueError(
                            "The local runtime reported a generation error."
                        )
                    usage = event.get("usage") or usage
                    timings = event.get("timings") or timings
                    if not event.get("choices"):
                        continue
                    delta = event["choices"][0].get("delta", {})
                    text = delta.get("content") or ""
                    if text:
                        first = first or time.monotonic()
                        content += text
                        token(text)
                    for part in delta.get("tool_calls", []):
                        first = first or time.monotonic()
                        index = part.get("index", 0)
                        call = calls.setdefault(
                            index,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        call["id"] += part.get("id") or ""
                        fn = part.get("function", {})
                        call["function"]["name"] += fn.get("name") or ""
                        call["function"]["arguments"] += fn.get("arguments") or ""
            self.last_used = time.monotonic()
            if cancel.is_set():
                raise InterruptedError("Stopped by you.")
            self.last_usage = usage_metrics(usage, timings, first, self.last_used)
            for call in calls.values():
                try:
                    parsed = json.loads(call['function']['arguments'])
                    if not isinstance(parsed, dict):
                        raise ValueError()
                except ValueError:
                    raise ToolCallTruncated('The local model returned incomplete tool arguments. No partial edit was applied.') from None
            return {
                "role": "assistant",
                "content": content or None,
                **({"tool_calls": list(calls.values())} if calls else {}),
            }
        except urllib.error.HTTPError as exc:
            raw = exc.read(12000).decode('utf-8', errors='replace')
            if 'Failed to parse tool call arguments as JSON' in raw:
                raise ToolCallTruncated('The local model produced an incomplete tool call. No partial edit was applied.') from None
            raise ValueError(f'Local inference HTTP {exc.code}. Check the local runtime log in Settings.') from None
        except (OSError, http.client.HTTPException):
            if cancel.is_set():
                raise InterruptedError("Stopped by you.") from None
            raise
        finally:
            self.busy = False
            self.active_socket = None
            self.cancel_event = None

    def idle(self):
        if (
            not self.busy
            and self.state == "ready"
            and time.monotonic() - self.last_used > 300
        ):
            self.unload()
