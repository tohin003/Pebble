"""User-selected local GGUF, OpenAI-compatible and Anthropic providers."""

import copy
import http.client
import ipaddress
import itertools
import json
import re
import socket
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlencode
import certifi
from . import VERSION
from .model import Model
from .vault import Vault
from .responses import complete_responses
from .recovery import (CONNECT_ATTEMPT, NETWORK, ProviderFailure, RETRYABLE_STATUS, connect_message, current_open,
                       failure_class, retry_after, tracked_open, transient)
from .capabilities import capabilities, choose, output_budget
from .token_budget import TokenBudget

MAX_CONTEXT = 10_000_000
# Phase 28: the beta that lets a request choose what happens to a thinking block whose prefix changed
# (platform.claude.com/docs/en/build-with-claude/thinking, Preserved thinking).
THINKING_BINDING_BETA = 'thinking-binding-controls-2026-08-01'


def model_capacity(url, model):
    # Verified against official model documentation, 2026-10-08. Unknown models
    # use provider metadata or a user override, never a guessed family limit.
    if urlsplit(url).hostname == "api.openai.com" and model in {'gpt-6-luna','gpt-6-sol','gpt-6-astra','gpt-6.1-sol'}:
        return 1_050_000
    return None


def safe_error(data, secrets=()):
    error = data.get("error", data) if isinstance(data, dict) else {}
    if not isinstance(error, dict):
        return "Generation failed."
    text = str(error.get("message") or error.get("code") or "Generation failed.")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?:sk-[\w-]+|Bearer\s+\S+|eyJ[\w.-]{30,})", "[redacted]", text)
    text = re.sub(r"https?://\S+", "[provider URL]", text)
    text = " ".join(text.split())[:700]
    param = error.get("param")
    if isinstance(param, str) and re.fullmatch(r"[\w.\[\]-]{1,80}", param):
        text += f" (parameter: {param})"
    return text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class SystemProxy(urllib.request.ProxyHandler):
    """Every request through one plain HTTP proxy (HTTPS is tunnelled with CONNECT). The caller chose it and applied
    the system's bypass list (Providers._system_proxy), so urllib's own environment and bypass lookups are not used."""

    def __init__(self, hostport):
        super().__init__({'http': 'http://' + hostport, 'https': 'http://' + hostport})
        self.hostport = hostport

    def proxy_open(self, req, proxy, type):
        req.set_proxy(self.hostport, 'http')
        return None  # the HTTP(S) handler below opens it (through a CONNECT tunnel for HTTPS)


def _local(host):
    """A host that is never proxied: loopback, a private or link-local address, or a .local name."""
    host = host.strip('[]').rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local')):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified


def interleave(addresses):
    """Alternate address families, each in the resolver's order, starting with the first family it returned
    (RFC 8305 §4), so a dead IPv6 path costs one short attempt before an IPv4 address is tried, and the reverse."""
    if not addresses:
        return []
    first = addresses[0][0]
    same, other = [a for a in addresses if a[0] == first], [a for a in addresses if a[0] != first]
    return [a for pair in itertools.zip_longest(same, other) for a in pair if a is not None]


def connect_any(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *, resolve=socket.getaddrinfo,
                factory=socket.socket, attempt=CONNECT_ATTEMPT):
    """socket.create_connection with a short connect timeout per address (Phase 22).

    create_connection gives every resolved address the request's whole timeout, so two dead addresses cost 120 s
    (the live stall). Here each address gets at most `attempt` seconds, families interleaved; the connected socket
    then takes the request's timeout. A watched open that was abandoned tries no further address."""
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        timeout = socket.getdefaulttimeout()
    record, (host, port), error = current_open(), address, None
    for family, kind, proto, _, sockaddr in interleave(list(resolve(host, port, 0, socket.SOCK_STREAM))):
        if record is not None and record.abandoned:
            break
        sock = None
        try:
            sock = factory(family, kind, proto)
            sock.settimeout(attempt if timeout is None else min(attempt, timeout))
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            sock.settimeout(timeout)
            return sock
        except OSError as exc:
            error = exc
            if sock is not None:
                sock.close()
    raise error if error is not None else OSError('The provider address could not be reached.')


ABANDONED = 'The model request was abandoned before it was sent.'
SUMMARY_REFUSALS = 'reasoning_summary_refused'  # setting: [[profile id, model], ...] that refused reasoning summaries


def endpoint(url):
    if not isinstance(url, str) or len(url) > 500:
        raise ValueError("Enter the provider's API base URL.")
    parsed = urlsplit(url)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Use an HTTP(S) API base URL without credentials, query or fragment."
        )
    if parsed.scheme != "https" and not loopback:
        raise ValueError(
            "Online providers require HTTPS. HTTP is allowed only for local servers."
        )
    base, path = url.rstrip("/"), parsed.path.rstrip("/")
    # Phase 24: a pasted request URL (as provider docs show it, e.g. https://api.anthropic.com/v1/messages) is reduced
    # to its API base; Pebble appends /models and the request path itself.
    for suffix in ENDPOINT_SUFFIXES:
        if path.endswith(suffix):
            base, path = base[: -len(suffix)], path[: -len(suffix)]
            break
    if path in {"", "/"}:
        base += "/v1"
    return base, loopback


# Request paths a provider's docs show after the API base (longest first).
ENDPOINT_SUFFIXES = ("/chat/completions", "/completions", "/messages", "/responses", "/models")


from .stats import usage_metrics


CALIBRATION_WAIT = 0.25


def calibrated(factor):
    """Counter keyword arguments: none at the uncalibrated factor 1.0, so any counter keeps working."""
    return {} if factor == 1.0 else {'multiplier': factor}


FITTED_REPLY = 512  # merge round 2 (E1): the smallest reply a fitted request leaves room for


def fitted_reply(manifest, capacity, budget, reply, reserve=64):
    """Reply tokens that let a refused request's protected floor through, or None (merge round 2, E1).

    The runner sizes its window by the working budget, but the reply reserve comes out of the capacity, so a
    4,096-token model has 3,008 input tokens for a 4,096-token plan. Only the reply shrinks: the request then
    carries exactly the protected floor (nothing retirable is added), it stays within the working budget, and at
    least FITTED_REPLY tokens remain for the answer, the way Model.budget fits a local model's reply."""
    floor = (manifest.get('input_tokens') or {}).get('protected_floor')
    if type(floor) is not int or floor + reserve > budget:
        return None
    fitted = min(reply, capacity - floor - reserve)
    return fitted if fitted >= FITTED_REPLY else None


class Providers:
    def __init__(self, home, resources, store, emit):
        self.store, self.emit = store, emit
        self.local = Model(home, resources, store, emit)
        self.vault = Vault()
        # Phase 22: this provider's own connections (connect_any), with the resolver and socket factory as seams.
        self.resolve, self.socket_factory = socket.getaddrinfo, socket.socket
        self.http = self._opener()
        # Phase 24 (V1-072): the system proxy, tried once after a direct connect failure (see open_url), with the
        # proxy lookups as seams. Only this opener is ever proxied: a replaced self.http (a child's, a test's) is not.
        self._own_http, self._proxied, self.via_proxy = self.http, {}, False
        self.getproxies, self.proxy_bypass = urllib.request.getproxies, urllib.request.proxy_bypass
        # Phase 24 (V1-078): the runner sets this to its run id; the ChatGPT route sends it as prompt_cache_key.
        self.prompt_cache_key = None
        self.last_usage = {}
        self.response = None
        self.auth = None
        self.catalogues = {}
        # Phase 22 (t22-stream), Responses route: the run's watchdog budget (recovery.StreamBudget; None is the
        # default), the latest request's content-free timing, routes that refused reasoning summaries in this service,
        # and whether a request opens on a helper thread so a stalled connect can be abandoned (this provider's
        # transport has no socket handle before the response; a child's pinned transport opens on its own thread).
        self.stream_budget = None
        self.last_timing = None
        self.summaries_refused = set()
        self.watched_open = True
        self.connect_observer = None  # called once when a Responses request is still connecting after 1 s
        self.token_budget = TokenBudget(resources)
        # C08: provider-reported input usage calibrates local estimates per route (only ever upward).
        from .token_calibration import Calibration
        self.calibration = Calibration(self._calibration_load, self._calibration_append)
        self.last_input_estimate = None
        self.context_observer = None
        self.context_transform = None
        self.local.context_observer = self.observe_context
        self.local.context_transform = self.transform_context

    def _opener(self, proxy=None):
        """The root's HTTP(S) opener: no proxy (or exactly `proxy`, host:port), no redirect, certifi trust, and
        connections that connect through connect_any and, inside a watched open (recovery.open_watched), never write a
        byte once it was abandoned."""
        owner = self

        class Connection(http.client.HTTPConnection):
            def connect(connection):
                record = current_open()
                if record is not None and not record.track():
                    raise OSError(ABANDONED)
                connection._create_connection = lambda address, timeout, source=None: connect_any(
                    address, timeout, source, resolve=owner.resolve, factory=owner.socket_factory)
                super().connect()

            def send(connection, data):
                # http.client connects lazily inside send(); connect first, so a byte is marked sent only once it is
                # about to be written on an open connection.
                if connection.sock is None and connection.auto_open:
                    connection.connect()
                record = current_open()
                if record is not None and not record.may_send():
                    raise OSError(ABANDONED)
                return super().send(data)

        class SecureConnection(Connection, http.client.HTTPSConnection):
            pass

        class HTTPHandler(urllib.request.HTTPHandler):
            def http_open(handler, request):
                return handler.do_open(Connection, request)

        class HTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(handler, request):
                return handler.do_open(SecureConnection, request, context=handler._context)

        return urllib.request.build_opener(
            SystemProxy(proxy) if proxy else urllib.request.ProxyHandler({}), NoRedirect(), HTTPHandler(),
            HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))

    def _system_proxy(self, req):
        """host:port of the system's HTTP proxy for this request, or None: only for this provider's own opener, never
        for a local or bypassed host, and never a proxy that needs credentials (Pebble does not read them)."""
        host = urlsplit(req.full_url).hostname
        if self.http is not self._own_http or not host or _local(host):
            return None
        try:
            value = (self.getproxies() or {}).get(req.type)
            if not isinstance(value, str) or not value or self.proxy_bypass(host):
                return None
            parsed = urlsplit(value if '://' in value else 'http://' + value)
            port = parsed.port or 80
        except Exception:  # an unreadable proxy setting is no proxy
            return None
        if parsed.scheme != 'http' or parsed.username or parsed.password or not parsed.hostname \
                or parsed.path not in ('', '/'):
            return None
        return (f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname) + f':{port}'

    def _through_proxy(self, hostport):
        if hostport not in self._proxied:
            self._proxied[hostport] = self._opener(proxy=hostport)
        return self._proxied[hostport]

    def open_url(self, req, timeout):
        """Open req on this provider's opener; after a failure that wrote no byte of the request (name lookup,
        connect), once more through the other route: the system proxy (urllib.request.getproxies: the environment,
        else the Mac's network settings) after a direct failure, or direct after a proxy failure. The route that worked
        is tried first next time. A request that may have been sent is never repeated here, so a one-time code or a
        refresh token is never presented twice (Phase 24, V1-072; Codex retries its token connection the same way)."""
        with tracked_open() as record:
            proxy = self._system_proxy(req) if self.via_proxy else None
            try:
                return (self._through_proxy(proxy) if proxy else self.http).open(req, timeout=timeout)
            except urllib.error.HTTPError:
                raise
            except (OSError, urllib.error.URLError) as exc:
                if not record.unsent() or record.abandoned or failure_class(exc) not in NETWORK:
                    raise
                other = None if proxy else self._system_proxy(req)
                if not proxy and not other:
                    raise
            again = urllib.request.Request(req.full_url, data=req.data, headers=dict(req.headers),
                                           method=req.get_method())
            response = (self._through_proxy(other) if other else self.http).open(again, timeout=timeout)
            self.via_proxy = bool(other)
            return response

    def summary_refused(self, profile):
        """Whether this profile's route refused reasoning summaries (remembered across restarts, Phase 22)."""
        key = [profile.get('id'), profile.get('model')]
        if tuple(key) in self.summaries_refused:
            return True
        try:
            return key in (self.store.setting(SUMMARY_REFUSALS, []) or [])
        except Exception:
            return False

    def refuse_summaries(self, profile):
        """Remember a refusal for this profile. A separate setting, so the profile and its route identity (which pins
        every run's config) are unchanged; a failed write still holds for this service process."""
        key = [profile.get('id'), profile.get('model')]
        self.summaries_refused.add(tuple(key))
        try:
            refused = [k for k in (self.store.setting(SUMMARY_REFUSALS, []) or []) if k != key]
            self.store.set_setting(SUMMARY_REFUSALS, (refused + [key])[-64:])
        except Exception:
            pass

    def profiles(self):
        profiles = self.store.setting("model_profiles", [])
        changed = False
        for p in profiles:
            if p.get('kind') != 'gguf' and not p.get('budget_version'):
                p['context_mode'] = 'full'
                p['budget'] = p['context']
                p['budget_version'] = 2
                changed = True
            if p.get('kind') != 'gguf' and not p.get('connection_id'):
                p['connection_id'] = p['id']
                p['provider_name'] = p['name']
                changed = True
            capacity = model_capacity(p.get("url", ""), p.get("model", ""))
            if capacity and not p.get("context_version"):
                # Migrate the old UI ceiling, preserving deliberate smaller limits.
                if p.get("context") == 131072:
                    p["context"] = capacity
                p["context_version"] = 1
                changed = True
            if p.get('context_mode')=='full' and p.get('budget')!=p['context']:
                p['budget']=p['context'];changed=True
        if changed:
            self.store.set_setting("model_profiles", profiles)
        if self.store.setting("model_path", "") and not any(
            p["kind"] == "gguf" and p["path"] == self.store.setting("model_path")
            for p in profiles
        ):
            profiles.insert(
                0,
                {
                    "id": "default-local",
                    "kind": "gguf",
                    "name": Path(self.store.setting("model_path")).stem,
                    "path": self.store.setting("model_path"),
                    "context": 4096,
                    "tools": True,
                },
            )
        return [{**p, 'connection_id': p.get('connection_id', 'local' if p['kind']=='gguf' else p['id']), 'provider_name': p.get('provider_name', 'On this device' if p['kind']=='gguf' else p['name']), **capabilities(p)} for p in profiles]

    def current(self):
        selected = self.store.setting("active_profile", "default-local")
        return next((p for p in self.profiles() if p["id"] == selected), None)

    def status(self):
        p = self.current()
        if not p or p["kind"] == "gguf":
            return {
                **self.local.status(),
                "kind": "gguf",
                "profile_id": p["id"] if p else None,
                **capabilities(p or {'kind':'gguf'}),
            }
        _, local = endpoint(p["url"])
        return {
            "state": "ready",
            "configured": True,
            "name": p["model"],
            "path": "",
            "context": p["context"],
            "kind": p["kind"],
            "profile_id": p["id"],
            "local": local,
            "provider": p["name"],
            "budget": p.get("budget", p["context"]),
            "protocol": self.protocol(p),
            **capabilities(p),
        }

    def effort(self, effort):
        p = self.current()
        if not p:
            raise ValueError('Choose a model first.')
        value = choose(p, effort)
        profiles = self.profiles()
        for profile in profiles:
            if profile['id'] == p['id']:
                profile['effort'] = value
        self.store.set_setting('model_profiles', profiles)
        self.emit('model', self.status())
        return self.status()

    @staticmethod
    def protocol(p):
        if p["kind"] in {"responses", "chatgpt"} or (
            p["kind"] == "openai" and urlsplit(p["url"]).hostname == "api.openai.com"
        ):
            return "responses"
        return p["kind"]

    def add_gguf(self, path):
        selected = Path(path).expanduser().resolve(strict=True)
        if selected.suffix.lower() != ".gguf" or selected.stat().st_size < 1_000_000:
            raise ValueError("Select a GGUF model file.")
        with selected.open("rb") as file:
            if file.read(4) != b"GGUF":
                raise ValueError("This is not a GGUF model file.")
        profiles = self.profiles()
        profile = next(
            (p for p in profiles if p["kind"] == "gguf" and p["path"] == str(selected)),
            None,
        )
        if not profile:
            profile = {
                "id": uuid.uuid4().hex,
                "kind": "gguf",
                "name": selected.stem,
                "path": str(selected),
                "context": 4096,
                "tools": True,
            }
            profiles.append(profile)
        self.store.set_setting("model_profiles", profiles)
        return self.select(profile["id"])

    def select(self, profile_id):
        p = next((p for p in self.profiles() if p["id"] == profile_id), None)
        if not p:
            raise ValueError("Model profile not found.")
        self.local.unload()
        if p["kind"] == "gguf":
            self.store.set_setting("model_path", p["path"])
        self.store.set_setting("active_profile", profile_id)
        self.emit("model", self.status())
        return self.status()

    def headers(self, kind, key):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Pebble/" + VERSION,  # Phase 22: the running version (no grader pins the old "Pebble/0.3")
        }
        if kind == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            if key:
                headers["x-api-key"] = key
        elif key:
            headers["Authorization"] = "Bearer " + key
        return headers

    def request(self, url, headers, body=None, timeout=30):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            return self.open_url(req, timeout)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read(8192))
            except (ValueError, OSError):
                data = {}
            finally:
                exc.close()  # Phase 24 (V1-168): the error response's connection is closed, not left to the collector
            secrets = [headers.get("Authorization", "").removeprefix("Bearer "), headers.get("x-api-key", ""), headers.get("x-goog-api-key", "")]
            raise ProviderFailure(
                f"Provider HTTP {exc.code}: {safe_error(data, secrets)}",
                retryable=exc.code in RETRYABLE_STATUS, status=exc.code,
                retry_after=retry_after(exc.headers.get('Retry-After')) if exc.headers else None,
            ) from None
        except (OSError, urllib.error.URLError) as exc:
            # Phase 24 (V1-066): the failure's class names it; no HTTP response while the network was away waits longer.
            code, retryable = failure_class(exc), transient(exc)
            raise ProviderFailure(connect_message(code), retryable=retryable, code=code,
                                  network=retryable and code in NETWORK) from None

    def discover(self, url, kind="openai", api_key="", profile_id=None):
        if kind not in {"openai", "responses", "anthropic", "chatgpt"}:
            raise ValueError("Choose an OpenAI-compatible or Anthropic provider.")
        base, _ = endpoint(url)
        if profile_id and not api_key:
            p = next((p for p in self.profiles() if p["id"] == profile_id), None)
            if not p or p.get("url") != base or p["kind"] != kind:
                raise ValueError("The saved key belongs to a different endpoint.")
            api_key = self.key(p)
        if urlsplit(base).hostname == 'generativelanguage.googleapis.com' and '/openai' in base:
            with self.request('https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000', {'x-goog-api-key':api_key}) as response:
                raw=response.read(1_000_001)
                if len(raw)>1_000_000:
                    raise ValueError('Provider catalogue is too large.')
                data=json.loads(raw)
            models=[{'id':m['name'].removeprefix('models/'),'name':m.get('displayName',m['name']), 'context':m.get('inputTokenLimit')} for m in data.get('models',[]) if isinstance(m.get('name'),str) and 'generateContent' in m.get('supportedGenerationMethods',[])]
            self.catalogues[(base,kind)]=models
            return models
        models, cursor = [], ""
        for _ in range(10):
            suffix = (
                (
                    "?"
                    + urlencode(
                        {"limit": 100, **({"after_id": cursor} if cursor else {})}
                    )
                )
                if kind == "anthropic"
                else ""
            )
            with self.request(
                base + "/models" + suffix, self.headers(kind, api_key)
            ) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("The provider's model list is too large.")
                data = json.loads(raw)
            for p in data.get("data", data.get("models", [])):
                mid = p.get("id", p.get("slug"))
                if kind == "chatgpt" and p.get("visibility") != "list":
                    continue
                if isinstance(mid, str) and len(mid) < 200:
                    advertised = p.get('context_window') or p.get('context_length') or p.get('max_input_tokens') or model_capacity(base,mid)
                    extended = p.get('max_context_window')
                    usable = extended if p.get('supports_experimental_context') is True and type(extended) is int else advertised
                    reasoning = [r.get('effort') for r in p.get('supported_reasoning_levels',[]) if isinstance(r,dict)]
                    models.append(
                        {
                            "id": mid,
                            "name": p.get("display_name", mid),
                            "context": usable,
                            "context_source": 'account catalogue' if kind=='chatgpt' else 'provider catalogue' if advertised and not model_capacity(base,mid) else 'official model specification' if advertised else 'user configured',
                            **({'advertised_max_context':extended,'extended_context_available':p.get('supports_experimental_context') is True} if type(extended) is int else {}),
                            **({'reasoning_capabilities':{'parameter':'reasoning','levels':reasoning,'default':p.get('default_reasoning_level','medium')}} if kind=='chatgpt' and reasoning else {}),
                            **({'reasoning_capabilities':p['reasoning_capabilities']} if isinstance(p.get('reasoning_capabilities'),dict) else {}),
                            **({'max_output_tokens':p['max_output_tokens']} if type(p.get('max_output_tokens')) is int else {}),
                            **({'input_modalities':p['input_modalities']} if isinstance(p.get('input_modalities'),list) else {}),
                        }
                    )
            if (
                kind != "anthropic"
                or not data.get("has_more")
                or not data.get("last_id")
            ):
                break
            cursor = data["last_id"]
        if not models:
            raise ValueError(
                "This endpoint returned no models. You can enter a model ID manually if discovery is unsupported."
            )
        if kind=='chatgpt':
            owner=next(x for x in self.profiles() if x['id']==profile_id).get('connection_id',profile_id) if profile_id else 'new'
            self.catalogues[(base,kind,owner)]=models
        else:
            self.catalogues[(base,kind)]=models
        return models

    def save_many(self, name, url, models, kind='openai', api_key='', profile_id=None,
                  context_mode='full', budget=None, context=None, tools=True):
        """Commit a selected model set with one owned credential per connection."""
        if not isinstance(models,list) or not 1 <= len(models) <= 50 or any(not isinstance(m,str) or not m or len(m)>200 for m in models) or len(set(models))!=len(models):
            raise ValueError('Select between one and fifty different models.')
        if context_mode not in {'full','custom'} or type(tools) is not bool or not isinstance(name,str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Enter a connection name and context mode.')
        base,local=endpoint(url)
        if kind not in {'openai','responses','anthropic','chatgpt'}:
            raise ValueError('Choose a supported API format.')
        profiles=self.profiles()
        previous=next((p for p in profiles if p['id']==profile_id),None)
        if profile_id and (not previous or previous['kind']=='gguf'):
            raise ValueError('Provider connection not found.')
        if previous and (previous['url']!=base or previous['kind']!=kind):
            raise ValueError('Create a separate connection when changing its endpoint.')
        if kind=='chatgpt' and (not previous or api_key or base!='https://api.openai.com/v1'):
            raise ValueError('Connect ChatGPT through validated sign-in.')
        connection=previous.get('connection_id',previous['id']) if previous else uuid.uuid4().hex
        existing=[p for p in profiles if p.get('connection_id',p['id'])==connection]
        catalogue=self.catalogues.get((base,kind,connection) if kind=='chatgpt' else (base,kind),[])
        if kind=='chatgpt' and not catalogue:
            catalogue=self.discover(base,kind,profile_id=profile_id)
        selected=[]
        for model in models:
            meta=next((m for m in catalogue if m['id']==model),None)
            old=next((p for p in existing if p.get('model')==model),None)
            if kind=='chatgpt' and not meta:
                raise ValueError('Select only models available to this ChatGPT account.')
            capacity=(meta or {}).get('context') or model_capacity(base,model) or (old or {}).get('context') or context or 8192
            if type(capacity) is not int or not 4096 <= capacity <= MAX_CONTEXT:
                raise ValueError('The provider reported an invalid model capacity.')
            working=capacity if context_mode=='full' else budget
            if type(working) is not int or not 4096 <= working <= capacity:
                raise ValueError('Custom context budget must fit every selected model.')
            pid=old['id'] if old else connection if not selected and not existing else uuid.uuid4().hex
            item={**(old or {}),'id':pid,'connection_id':connection,'provider_name':name.strip(),'name':name.strip(),
                  'kind':kind,'url':base,'local':local,'model':model,'display_name':(meta or {}).get('name',model),
                  'context':capacity,'budget':working,'context_mode':context_mode,'budget_version':2,'context_version':1,
                  'tools':tools,'has_key':bool(api_key) or bool(previous and previous.get('has_key'))}
            for key in ['context_source','advertised_max_context','extended_context_available','reasoning_capabilities','max_output_tokens','input_modalities']:
                if meta and key in meta:item[key]=meta[key]
            if kind=='chatgpt':
                item.update({key:previous[key] for key in ['client_id','subject','email'] if key in previous})
            selected.append(item)
        # Validate everything before writing credentials or replacing profile rows.
        if api_key:self.vault.set(connection,api_key)
        self.store.set_setting('model_profiles',[p for p in profiles if p.get('connection_id',p['id'])!=connection]+selected)
        active=self.store.setting('active_profile')
        return self.select(active if any(p['id']==active for p in selected) else selected[0]['id'])

    def save(
        self, name, url, model, kind="openai", api_key="", context=8192, tools=True,
        budget=None, profile_id=None
    ):
        if (
            kind not in {"openai", "responses", "anthropic", "chatgpt"}
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 80
            or not isinstance(model, str)
            or not model.strip()
            or len(model) > 200
            or type(context) is not int
            or not 4096 <= context <= MAX_CONTEXT
            or type(tools) is not bool
        ):
            raise ValueError(
                "Enter a name, model ID, and context between 4096 and 10000000 tokens."
            )
        budget = context if budget is None else budget
        if type(budget) is not int or not 4096 <= budget <= context:
            raise ValueError("Working context must be between 4096 and the model capacity.")
        base, local = endpoint(url)
        previous = next((p for p in self.profiles() if p["id"] == profile_id), None)
        if profile_id and (not previous or previous["kind"] == "gguf"):
            raise ValueError("Provider profile not found.")
        if previous and (previous["url"] != base or previous["kind"] != kind) and not api_key:
            raise ValueError("Enter a new credential when changing the provider endpoint.")
        if kind == "chatgpt" and (not previous or base != "https://api.openai.com/v1" or api_key):
            raise ValueError("ChatGPT profiles are created only through validated sign-in.")
        pid = profile_id or uuid.uuid4().hex
        if api_key:
            self.vault.set(previous.get('connection_id',pid) if previous else pid, api_key)
        profile = {
            "id": pid,
            "name": name.strip(),
            "kind": kind,
            "url": base,
            "model": model.strip(),
            "context": context,
            "tools": tools,
            "has_key": bool(api_key) or bool(previous and previous.get("has_key")),
            "local": local,
            "budget": budget,
            "context_version": 1,
            "budget_version": 2,
            "context_mode": "full" if budget == context else "custom",
            "connection_id": previous.get("connection_id", previous["id"]) if previous else pid,
            "provider_name": name.strip(),
            **({'effort': previous.get('effort', 'default')} if previous else {}),
            **({key: previous[key] for key in ('client_id','subject','email') if key in previous} if previous and kind == 'chatgpt' else {}),
        }
        fetched = next((m for m in self.catalogues.get((base,kind),[]) if m['id']==model.strip()),None)
        if fetched and fetched.get('reasoning_capabilities'):
            profile['reasoning_capabilities']=fetched['reasoning_capabilities']
        if fetched and fetched.get('max_output_tokens'):
            profile['max_output_tokens']=fetched['max_output_tokens']
        self.store.set_setting("model_profiles", [p for p in self.profiles() if p["id"] != pid] + [profile])
        return self.select(pid)

    def key(self, p):
        if p["kind"] == "chatgpt":
            if not self.auth:
                raise ValueError("Reconnect your ChatGPT account.")
            return self.auth.access(p.get("connection_id",p["id"]))
        return self.vault.get(p.get("connection_id",p["id"])) if p.get("has_key") else ""

    def renew_key(self, p, rejected):
        """A new access token after the model route refused `rejected` with HTTP 401 (Phase 24, V1-070): the ChatGPT
        sign-in refreshes once (unless another request already did); None for every other route, which never retries."""
        if p.get("kind") != "chatgpt" or not self.auth:
            return None
        return self.auth.access(p.get("connection_id", p["id"]), rejected=rejected)

    def remove(self, profile_id):
        profiles = self.profiles()
        p = next((p for p in profiles if p["id"] == profile_id), None)
        if not p:
            raise ValueError("Model profile not found.")
        connection = p.get('connection_id', p['id'])
        if p.get('has_key') and not any(x['id'] != profile_id and x.get('connection_id', x['id']) == connection for x in profiles):
            self.vault.delete(connection)
        profiles = [p for p in profiles if p["id"] != profile_id]
        if p["kind"] == "gguf" and p["path"] == self.store.setting("model_path"):
            self.store.set_setting("model_path", "")
        self.store.set_setting("model_profiles", profiles)
        if self.store.setting("active_profile", "default-local") == profile_id:
            self.local.unload()
            self.store.set_setting(
                "active_profile", profiles[0]["id"] if profiles else ""
            )
            if profiles and profiles[0]["kind"] == "gguf":
                self.store.set_setting("model_path", profiles[0]["path"])
        return self.status()

    def load(self, cancel=None):
        if not self.current() or self.current()["kind"] == "gguf":
            self.local.load(cancel)

    def unload(self):
        self.local.unload()

    def idle(self):
        self.local.idle()

    def cancel(self):
        self.local.cancel()
        if self.response:
            try:
                self.response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass

    @staticmethod
    def bounded(messages, context, counter=None, reserve=1200):
        from .context_manifest import assemble_context
        projection = assemble_context(messages, [], context, reserve,
            counter=(lambda ms, ts: counter(ms)) if counter else None)
        return Providers.wire_context(projection.messages)

    @staticmethod
    def wire_context(messages):
        return [{k: v for k, v in m.items() if not k.startswith('_pebble_')} for m in messages]

    # The request path waits at most CALIBRATION_WAIT for the shared store: a busy store uses the
    # cached factor and skips the sample, so calibration can never stall or deadlock a request.
    def _calibration_load(self, route):
        from .token_calibration import SELECT_SQL
        if not self.store.lock.acquire(timeout=CALIBRATION_WAIT):
            raise TimeoutError('store busy')
        try:
            return self.store.db.execute(SELECT_SQL, (route,)).fetchall()
        except sqlite3.OperationalError:  # A store without the epoch-6 table: uncalibrated.
            return []
        finally:
            self.store.lock.release()

    def _calibration_append(self, route, estimated, reported):
        from .token_calibration import INSERT_SQL, PRUNE_SQL
        if not self.store.lock.acquire(timeout=CALIBRATION_WAIT):
            raise TimeoutError('store busy')
        try:
            with self.store.db as db:
                db.execute(INSERT_SQL, (route, route, estimated, reported, time.time()))
                db.execute(PRUNE_SQL, (route, route))
        finally:
            self.store.lock.release()

    def calibration_factor(self, profile):
        """The route's estimate multiplier in [1, 2]; 1.0 until enough provider samples exist."""
        from .token_calibration import route_fingerprint
        cache = self.__dict__.setdefault('_calibration_cache', {})
        try:
            route = route_fingerprint(profile)
        except Exception:
            return 1.0
        try:
            cache[route] = self.calibration.multiplier(route)
        except Exception:
            pass
        return cache.get(route, 1.0)

    def calibrate(self, profile, factor):
        """Record the last request's unmultiplied local estimate against the provider's reported input."""
        from .token_calibration import route_fingerprint
        estimate, reported = self.last_input_estimate, (self.last_usage or {}).get('input_tokens')
        self.last_input_estimate = None
        if type(estimate) is not int or estimate <= 0:
            return None
        try:
            return self.calibration.record(route_fingerprint(profile), max(1, round(estimate / factor)), reported)
        except Exception:
            return None  # Calibration never blocks a reply.

    def observe_context(self, manifest):
        if self.context_observer:
            self.context_observer(manifest)

    def transform_context(self, projection, messages, tools, context, reserve, counter, method):
        transform = getattr(self, 'context_transform', None)
        return transform(projection, messages, tools, context, reserve, counter, method) if transform else projection

    def project_context(self, messages, tools, context, *, reserve=64, counter=None, method=None, observe_refusal=True):
        from .context_manifest import assemble_context, ContextBudgetExceeded
        try:
            projection = assemble_context(messages, tools, context, reserve, counter=counter, counter_method=method)
            projection = self.transform_context(projection, messages, tools, context, reserve, counter, method)
        except ContextBudgetExceeded as exc:
            if observe_refusal:
                self.observe_context(exc.manifest)
            raise
        self.observe_context(projection.manifest)
        self.last_input_estimate = projection.manifest.get('input_tokens')
        return self.wire_context(projection.messages)

    def complete(self, messages, tools, cancel, token, deliberate=False):
        try:
            return self._complete(messages, tools, cancel, token, deliberate)
        finally:
            self.response = None

    def _complete(self, messages, tools, cancel, token, deliberate=False):
        p = self.current()
        if not p or p["kind"] == "gguf":
            thinking = choose(p, deliberate) == 'think' if p else bool(deliberate)
            result = self.local.complete(messages, tools, cancel, token, thinking)
            self.last_usage = self.local.last_usage
            return result
        if cancel.is_set():
            raise InterruptedError("Stopped by you.")
        budget = p.get("budget", p["context"])
        tools = tools if p["tools"] else []
        effort = choose(p, deliberate)
        if self.protocol(p) == "responses":
            return complete_responses(self, p, messages, tools, cancel, token, effort)
        requested_output = output_budget(capabilities(p)['default_effort'] if effort == 'default' else effort) if capabilities(p)['effort_parameter'] else 1024
        max_output = min(requested_output, p.get('max_output_tokens', requested_output), p['context']//2)
        factor = self.calibration_factor(p)
        count = lambda ms, ts:self.token_budget.count(self.wire_context(ms),ts,**calibrated(factor))  # noqa: E731
        from .context_manifest import ContextBudgetExceeded
        try:
            messages = self.project_context(messages, tools, min(budget,p['context']-max_output),
                counter=count, method='o200k_local_estimate', observe_refusal=False)
        except ContextBudgetExceeded as exc:
            # Merge round 2 (E1): a small model whose reply reserve leaves less than its working budget (4,096
            # tokens of capacity: 3,008 for input) sends the protected floor alone with a smaller reply instead.
            reply = None if capabilities(p)['effort_parameter'] else fitted_reply(exc.manifest, p['context'], budget, max_output)
            if reply is None:
                self.observe_context(exc.manifest)
                raise
            max_output = reply
            messages = self.project_context(messages, tools, p['context'] - max_output,
                counter=count, method='o200k_local_estimate')
        body = {
            "model": p["model"],
            "stream": True,
            "max_tokens": max_output,
            "messages": messages,
        }
        if p["kind"] == "openai" and (
            p["model"].startswith(("gpt-5", "gpt-6")) or re.match(r"^o\d", p["model"])
        ):
            body["max_completion_tokens"] = body.pop("max_tokens")
        path = "/chat/completions"
        if p["kind"] == "anthropic":
            cap=capabilities(p)
            if cap['effort_parameter']:
                if p['model'].startswith('claude-opus-4-5'):
                    body['thinking']={'type':'enabled','budget_tokens':min(1024 if effort=='low' else 4096, max_output-512)}
                else:
                    body['thinking']={'type':'adaptive'}
            if effort != 'default' and capabilities(p)['effort_parameter']:
                body['output_config'] = {'effort': effort}
            path = "/messages"
            body["system"] = "\n".join(
                m["content"] for m in messages if m["role"] == "system"
            )
            converted = []
            for m in messages:
                if m["role"] == "system":
                    continue
                if m.get('_anthropic_content'):
                    item={'role':'assistant','content':m['_anthropic_content']}
                elif m["role"] == "tool":
                    item = {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m["tool_call_id"],
                                "content": m["content"],
                            }
                        ],
                    }
                elif m.get("tool_calls"):
                    blocks = (
                        [{"type": "text", "text": m["content"]}]
                        if m.get("content")
                        else []
                    ) + [
                        {
                            "type": "tool_use",
                            "id": c["id"],
                            "name": c["function"]["name"],
                            "input": json.loads(c["function"]["arguments"]),
                        }
                        for c in m["tool_calls"]
                    ]
                    item = {"role": "assistant", "content": blocks}
                else:
                    value=m.get('content') or ' '
                    if isinstance(value,list):
                        content=[]
                        for part in value:
                            if part['type']=='text':content.append({'type':'text','text':part['text']})
                            else:
                                image=part['image_url']['url'];mime,data=image.removeprefix('data:').split(';base64,',1)
                                content.append({'type':'image','source':{'type':'base64','media_type':mime,'data':data}})
                    else:content=[{'type':'text','text':value}]
                    item = {
                        "role": m["role"],
                        "content": content,
                    }
                if converted and converted[-1]["role"] == item["role"]:
                    converted[-1]["content"] += item["content"]
                else:
                    converted.append(item)
            body["messages"] = converted
            if tools:
                body["tools"] = [
                    {
                        "name": t["function"]["name"],
                        "description": t["function"]["description"],
                        "input_schema": t["function"]["parameters"],
                    }
                    for t in tools
                ]
            # Phase 27 (D26): automatic prompt caching. One top-level marker and Anthropic's API keeps the breakpoint
            # on the last cacheable block, moving it forward as the conversation grows, so each request of a task
            # reads the tools, system and earlier turns at the cache price instead of paying full input on them
            # again. Anthropic-compatible gateways may refuse the field, so only Anthropic's own API gets it.
            if urlsplit(p["url"]).hostname == "api.anthropic.com":
                body["cache_control"] = {"type": "ephemeral"}
                # Phase 28: Anthropic binds each thinking block to everything sent before it (system, tools and
                # earlier messages) and, by default on accounts created since 2026-08-31, refuses a request with
                # HTTP 400 when that prefix changed. Pebble rebuilds the first user message each step (the host
                # checkpoint and the working record), so a task's third request failed ("Invalid `signature` in
                # `thinking` block"). Until the request layout is append-only (V1-078), the API is asked to drop
                # the blocks whose prefix changed instead; the task continues without that earlier reasoning.
                if isinstance(body.get("thinking"), dict):
                    body["thinking"] = {**body["thinking"],
                                        "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        else:
            cap=capabilities(p)
            if effort != 'default' and capabilities(p)['effort_parameter'] == 'reasoning_effort':
                body['reasoning_effort'] = effort
            body["stream_options"] = {"include_usage": True}
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
        key = self.key(p)
        content, calls, usage, timings, first = "", {}, {}, {}, None
        blocks, finished = {}, False
        received = 0
        headers = self.headers(p["kind"], key)
        if isinstance(body.get("thinking"), dict) and body["thinking"].get("block_binding"):
            headers["anthropic-beta"] = THINKING_BINDING_BETA
        with self.request(
            p["url"] + path, headers, body, timeout=60
        ) as response:
            self.response = response
            for raw in response:
                if cancel.is_set():
                    raise InterruptedError("Stopped by you.")
                received += len(raw)
                if received > 2_000_000 or len(raw) > 192000:
                    raise ValueError("Provider stream exceeds the response limit.")
                if raw.strip() == b"data: [DONE]":
                    finished = True
                    continue
                if not raw.startswith(b"data: "):
                    continue
                event = json.loads(raw[6:])
                if event.get("error") or event.get("type") == "error":
                    raise ValueError(
                        "The provider reported a generation error. Check model compatibility."
                    )
                if p["kind"] == "anthropic":
                    if event.get('type')=='message_stop':
                        finished=True
                    if event.get("type") == "message_start":
                        usage.update(event["message"].get("usage", {}))
                    if event.get("type") == "message_delta":
                        usage.update(event.get("usage", {}))
                        if event.get('delta',{}).get('stop_reason')=='max_tokens':
                            raise ValueError('Claude reached its output limit before completing. Lower effort or split the task.')
                    if event.get('type')=='content_block_start':
                        blocks[event['index']]=copy.deepcopy(event['content_block'])
                    if (
                        event.get("type") == "content_block_start"
                        and event["content_block"]["type"] == "tool_use"
                    ):
                        block = event["content_block"]
                        calls[event["index"]] = {
                            "id": block["id"],
                            "type": "function",
                            "function": {"name": block["name"], "arguments": ""},
                        }
                    delta = event.get("delta", {})
                    part = delta.get("text", "")
                    if delta.get('type')=='thinking_delta':
                        first = first or time.monotonic()
                        blocks[event['index']]['thinking']=blocks[event['index']].get('thinking','')+delta['thinking']
                    if delta.get('type')=='signature_delta':
                        blocks[event['index']]['signature']=delta['signature']
                    if part and event.get('index') in blocks:
                        blocks[event['index']]['text']=blocks[event['index']].get('text','')+part
                    if delta.get("type") == "input_json_delta":
                        calls[event["index"]]["function"]["arguments"] += delta[
                            "partial_json"
                        ]
                else:
                    reason = (event.get('choices') or [{}])[0].get('finish_reason')
                    if reason in {'length','content_filter'}:
                        raise ValueError('Provider stopped before completing. Lower effort or split the task. No tool calls were executed.')
                    if reason:
                        finished = True
                    usage = event.get("usage") or usage
                    timings = event.get("timings") or timings
                    delta = (event.get("choices") or [{}])[0].get("delta", {})
                    part = delta.get("content") or ""
                    for c in delta.get("tool_calls", []):
                        call = calls.setdefault(
                            c.get("index", 0),
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        call["id"] += c.get("id") or ""
                        # Gemini-compatible thought signatures are opaque tool
                        # metadata. Preserve them for the next model step; they
                        # do not grant any broker authority.
                        if isinstance(c.get('extra_content'),dict):
                            call['extra_content']=copy.deepcopy(c['extra_content'])
                        for k in ("name", "arguments"):
                            call["function"][k] += c.get("function", {}).get(k) or ""
                if part:
                    first = first or time.monotonic()
                    content += part
                    token(part)
                if calls:
                    first = first or time.monotonic()
        self.response = None
        if not finished:
            raise ProviderFailure('Provider stream ended before completion. No tool calls were executed.', retryable=True)
        if cancel.is_set():
            raise InterruptedError("Stopped by you.")
        self.last_usage = usage_metrics(usage, timings, first, time.monotonic())
        self.calibrate(p, factor)
        if not content and not calls:
            raise ValueError(
                "This model returned no text or tools. Choose a chat model compatible with this provider."
            )
        for call in calls.values():
            if not call["function"]["arguments"]:
                call["function"]["arguments"] = "{}"
            try:
                if not isinstance(json.loads(call['function']['arguments']),dict):
                    raise ValueError()
            except ValueError:
                raise ValueError('Provider returned an incomplete tool call. No tool calls were executed.') from None
        for index,block in blocks.items():
            if block['type']=='tool_use':
                block['input']=json.loads(calls[index]['function']['arguments'])
        return {
            "role": "assistant",
            "content": content or None,
            **({"tool_calls": list(calls.values())} if calls else {}),
            **({'_anthropic_content':list(blocks.values())} if blocks else {}),
        }
