"""API keys in macOS Keychain / Windows Credential Manager, never SQLite.

Phase 22 (k22-identity; docs/HARNESS_SIGNING_IDENTITY.md). The installed Pebble keeps its secrets in ONE Keychain item,
service com.pebble.local.vault, account v1, which only its vault helper reads and writes: resources/helpers/
pebble-vault, a small C program built once and copied byte-identical into every release (scripts/build_vault_helper.py).
macOS remembers an "Always Allow" per code identity and, for code without an Apple Team ID, per code hash. The helper's
never changes, so once its item exists nothing asks again across updates.

- Who uses it: only the backend frozen inside the release bundle (com.pebble.local), and only when the helper is
  there and answers ping (its caller check passes). Every other process (source runs, validators, the test bundle,
  evaluation drivers), and the release backend without a working helper, keeps the per-item items under each
  service exactly as before Phase 22.
- Migration: an account the item does not know is looked up once in its old per-item item. When found, it is copied
  into the item (written by the helper, read back and compared) and recorded as migrated, so it is never read again:
  each such read would ask again, once per build. The old item is KEPT, so a rollback to 0.5.0 still finds it;
  only cleanup_legacy() deletes it.
- Providers, connectors and typed-plan credentials share the one item, each service in its own section.
- If the helper stops answering mid-session, the session falls back to the per-item items, so sign-in never fails
  because of the helper. A Keychain refusal the helper reports (for example the person chose Deny) is raised, not
  bypassed.
- The public API is unchanged: Vault(), get, set and delete, and a subclass's `service`. Secrets never appear in an
  exception, a log or a file. Windows is unchanged.

Phase 24 (t24-auth; docs/PEBBLE_V1_INSPECTION.md V1-009, V1-017):
- The fallback never serves or writes a per-item item for an account the item lists (its secrets or its migrated
  markers, as last read or written by this process), nor for any account when the item exists but was never read
  here: that per-item copy is older (a rotated ChatGPT refresh token presented again ends the sign-in). Such an
  account raises VaultUnavailable instead. An account the item does not list still falls back as before.
- A helper that timed out is not dropped for the session: it rests REST_FIRST seconds, doubling to REST_MAX, and is
  pinged again on the next use after that. Any other failure keeps the Phase 22 rule (per-item for the session).
- set_many writes several accounts in one item update (one put_all and its read-back); chatgpt.SecretRecords writes a
  whole OAuth record with it, so one refresh costs at most three helper processes.
- delete also removes the account's kept per-item item (removal reads no item data); one that cannot be removed keeps
  its marker, so it is never read, and cleanup_legacy() retries it.
"""

import base64
import ctypes as C
import hashlib
import json
import logging
import plistlib
import subprocess
import sys
import threading
import time
from pathlib import Path

LEGACY_SERVICE = b"com.pebble.local.providers"
VAULT_SERVICE, VAULT_ACCOUNT = b"com.pebble.local.vault", b"v1"
OWNER_IDENTIFIER = "com.pebble.local"
FORMAT = "pebble.vault/1"
MAX_MAP = 1 << 20  # the helper's own cap on the item (pebble_vault.c MAX_MAP)
MAX_REPLY = 2 << 20
HELPER_SECONDS = 120  # long enough for a person to answer a Keychain prompt shown for the helper
CACHE_SECONDS = 30
NOT_FOUND = -25300  # errSecItemNotFound
SUPERSEDED = "superseded"  # the kept old item is older than this item's value (or the value was deleted)
PING = b'{"op":"ping"}'
GET_ALL = b'{"op":"get_all"}'
PUT_PREFIX, PUT_SUFFIX = b'{"op":"put_all","map_b64":"', b'"}'
EXIT_REFUSED = 3
REST_FIRST, REST_MAX = 2.0, 60.0  # after a helper timeout: seconds before the next ping, doubling up to the cap
_LOCK = threading.RLock()  # one item per process: every read-modify-write of it holds this
_OWNER = []
_HELPER = {}
log = logging.getLogger(__name__)


def bundle_identifier(executable):
    """CFBundleIdentifier of the .app whose Contents holds executable, or None outside a bundle."""
    try:
        path = Path(executable).resolve()
    except (OSError, RuntimeError):
        return None
    for parent in path.parents:
        if parent.name == "Contents" and parent.parent.suffix == ".app":
            try:
                info = plistlib.loads((parent / "Info.plist").read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                return None
            value = info.get("CFBundleIdentifier") if isinstance(info, dict) else None
            return value if isinstance(value, str) else None
    return None


def installed_owner():
    """Whether this process is the release app's frozen backend, the owner of the consolidated item."""
    if not _OWNER:
        _OWNER.append(sys.platform == "darwin" and bool(getattr(sys, "frozen", False))
                      and bundle_identifier(sys.executable) == OWNER_IDENTIFIER)
    return _OWNER[0]


def helper_path(executable=None):
    """Where the bundle keeps the helper: resources/helpers/pebble-vault, beside resources/backend/."""
    return Path(executable or sys.executable).resolve().parent.parent / "helpers" / "pebble-vault"


class HelperError(Exception):
    """The helper answered with a Keychain failure (a fixed code and an OSStatus; never item data)."""

    def __init__(self, code, status=None):
        super().__init__(code)
        self.code, self.status = code, status


class HelperUnavailable(HelperError):
    """The helper could not serve at all: missing, refused this caller, crashed, timed out or replied nonsense."""


class VaultUnavailable(ValueError):
    """The helper cannot serve an account the item holds, and its kept per-item copy is older, so neither is used."""

    def __init__(self):
        super().__init__("Pebble's vault in Keychain is not answering, so a saved key or sign-in was not used. "
                         'Try again in a moment.')


def _accounts(data):
    """Every (service, account) a map lists, with a secret or a migrated marker."""
    return {(name.encode(), account) for name, section in data["services"].items()
            for part in ("secrets", "migrated") for account in section[part]}


def run_helper(path, request, timeout):
    """Run the helper once with no environment and no inherited descriptors; (exit code or None, stdout bytes)."""
    try:
        done = subprocess.run([str(path)], input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={},
                              cwd="/", timeout=timeout, close_fds=True)
    except subprocess.TimeoutExpired:
        return None, b""
    except OSError:
        raise HelperUnavailable("spawn_failed") from None
    return done.returncode, done.stdout[:MAX_REPLY + 1]


class VaultHelper:
    """The client side of pebble_vault.c's protocol: one request in, one reply out, both single-line JSON."""

    def __init__(self, path, run=None):
        self.path, self.run = path, run if run is not None else run_helper

    def call(self, request):
        code, output = self.run(self.path, request + b"\n", HELPER_SECONDS)
        if code is None:
            raise HelperUnavailable("timeout")
        if code == EXIT_REFUSED:
            raise HelperUnavailable("caller_refused")
        if len(output) > MAX_REPLY:
            raise HelperUnavailable("reply_too_large")
        try:
            reply = json.loads(output)
        except ValueError:
            reply = None
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise HelperUnavailable("bad_reply")
        if not reply["ok"]:
            error = reply.get("error") if isinstance(reply.get("error"), str) and reply["error"].isidentifier() else "unknown"
            status = reply.get("status") if type(reply.get("status")) is int else None
            if code == 1 and error.startswith(("keychain_", "verify_", "item_")):
                raise HelperError(error, status)
            raise HelperUnavailable(error[:40])
        if code != 0:
            raise HelperUnavailable("bad_reply")
        return reply

    def ping(self):
        if self.call(PING).get("version") != 1:
            raise HelperUnavailable("version")

    def get_all(self):
        reply = self.call(GET_ALL)
        if reply.get("found") is False:
            return None
        try:
            raw = base64.b64decode(reply["map_b64"], validate=True)
        except (KeyError, TypeError, ValueError):
            raise HelperUnavailable("bad_reply") from None
        if len(raw) > MAX_MAP:
            raise HelperUnavailable("reply_too_large")
        return raw

    def put_all(self, raw):
        if not raw or len(raw) > MAX_MAP:
            raise ValueError("The Pebble vault item in Keychain is full.")
        self.call(PUT_PREFIX + base64.b64encode(raw) + PUT_SUFFIX)


class HelperStore:
    """The item's map through the helper, cached briefly: this process is its only writer (see Vault).

    `listed` holds every (service, account) the item lists or may list (from the last map read, plus any write since
    whose outcome is unknown), or None before a map was read; Vault never falls back to a per-item copy of those.
    After a timeout the store rests (rest()), and wake() pings it again once the rest is over."""

    def __init__(self, helper, clock=time.monotonic):
        self.helper, self.clock, self.cached, self.at = helper, clock, None, None
        self.listed, self.resting, self.rests = None, None, 0

    def load(self, fresh=False):
        if fresh or self.cached is None or self.clock() - self.at > CACHE_SECONDS:
            raw = self.helper.get_all()
            self.cached, self.at = self.parse(raw), self.clock()
            self.listed = _accounts(self.cached)
        return json.loads(json.dumps(self.cached))  # callers change their own copy

    def lists(self, service, account):
        return self.listed is not None and (service, account) in self.listed

    def rest(self):
        """After a timeout: no helper call for a while (doubling), and the next read is fresh."""
        self.rests += 1
        self.resting = self.clock() + min(REST_MAX, REST_FIRST * 2 ** (self.rests - 1))
        self.cached = None
        return self.resting - self.clock()

    def wake(self):
        """Ping a resting store once its rest is over; while it still rests, HelperUnavailable('resting')."""
        if self.resting is None:
            return
        if self.clock() < self.resting:
            raise HelperUnavailable("resting")
        self.helper.ping()
        self.resting, self.rests = None, 0
        log.warning("Vault helper answers again.")

    @staticmethod
    def parse(raw):
        if raw is None:
            return {"format": FORMAT, "services": {}}
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        valid = (isinstance(data, dict) and data.get("format") == FORMAT and isinstance(data.get("services"), dict)
                 and all(isinstance(section, dict) and set(section) == {"secrets", "migrated"}
                         and all(isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items())
                                 for v in section.values())
                         for section in data["services"].values()))
        if not valid:
            raise ValueError("The Pebble vault item in Keychain is unreadable.")
        return data

    def commit(self, data):
        """Write the map through the helper, then read it back and compare before anyone relies on it. Until the
        read-back matches, the item may hold the new map or the old one: both are listed, and the cache is dropped."""
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        self.listed = (self.listed or set()) | _accounts(data)
        self.cached = None
        self.helper.put_all(raw)
        if self.helper.get_all() != raw:
            raise ValueError("Keychain could not save the key.")
        self.cached, self.at = json.loads(raw), self.clock()
        self.listed = _accounts(self.cached)


def _auto_helper():
    """The release backend's helper store, decided once per process: the pinned helper answers ping, or None."""
    with _LOCK:
        if "store" not in _HELPER:
            store = None
            path = helper_path()
            if path.is_file():
                helper = VaultHelper(path)
                try:
                    helper.ping()
                    store = HelperStore(helper)
                except HelperError as error:
                    if error.code == "timeout":  # Phase 24: a slow helper rests and is asked again, never dropped
                        store = HelperStore(helper)
                        log.warning("Vault helper stopped answering (timeout); asking it again in %g s.", store.rest())
                    else:
                        log.warning("Vault helper unavailable (%s); using per-item Keychain storage.", error.code)
            _HELPER["store"] = store
        return _HELPER["store"]


class MacKeychain:
    """Generic-password items through the legacy SecKeychain* API (ctypes). Items are found by service and
    account, never enumerated."""

    def __init__(self):
        self.lib = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.cf.CFRelease.argtypes = [C.c_void_p]
        self.lib.SecKeychainFindGenericPassword.argtypes = [
            C.c_void_p,
            C.c_uint32,
            C.c_char_p,
            C.c_uint32,
            C.c_char_p,
            C.POINTER(C.c_uint32),
            C.POINTER(C.c_void_p),
            C.POINTER(C.c_void_p),
        ]
        self.lib.SecKeychainAddGenericPassword.argtypes = [
            C.c_void_p,
            C.c_uint32,
            C.c_char_p,
            C.c_uint32,
            C.c_char_p,
            C.c_uint32,
            C.c_void_p,
            C.POINTER(C.c_void_p),
        ]
        self.lib.SecKeychainItemModifyAttributesAndData.argtypes = [
            C.c_void_p,
            C.c_void_p,
            C.c_uint32,
            C.c_void_p,
        ]
        self.lib.SecKeychainItemDelete.argtypes = [C.c_void_p]
        self.lib.SecKeychainItemFreeContent.argtypes = [C.c_void_p, C.c_void_p]

    def _item(self, service, account, with_data):
        size, data, item = C.c_uint32(), C.c_void_p(), C.c_void_p()
        code = self.lib.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            C.byref(size) if with_data else None,
            C.byref(data) if with_data else None,
            C.byref(item),
        )
        if code not in (0, NOT_FOUND):
            raise ValueError(f"Keychain access failed ({code}).")
        return code, size, data, item

    def _release(self, data, item):
        if data.value:
            self.lib.SecKeychainItemFreeContent(None, data)
        if item.value:
            self.cf.CFRelease(item)

    def read(self, service, account):
        """The item's data, or None when there is no item. Reading another identity's item may prompt."""
        code, size, data, item = self._item(service, account, True)
        try:
            return None if code == NOT_FOUND else C.string_at(data, size.value)
        finally:
            self._release(data, item)

    def exists(self, service, account):
        """Whether the item exists. Its data is not requested, so this never prompts."""
        code, _, data, item = self._item(service, account, False)
        self._release(data, item)
        return code == 0

    def write(self, service, account, raw):
        code, _, data, item = self._item(service, account, False)
        try:
            if code == 0:
                code = self.lib.SecKeychainItemModifyAttributesAndData(item, None, len(raw), raw)
            else:
                code = self.lib.SecKeychainAddGenericPassword(
                    None, len(service), service, len(account), account, len(raw), raw, None
                )
            if code:
                raise ValueError(f"Keychain could not save the key ({code}).")
        finally:
            self._release(data, item)

    def remove(self, service, account):
        """Delete the item; False when there was none."""
        code, _, data, item = self._item(service, account, False)
        try:
            if code == NOT_FOUND:
                return False
            if self.lib.SecKeychainItemDelete(item):
                raise ValueError("Keychain could not delete the key.")
            return True
        finally:
            self._release(data, item)


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Vault:
    service = LEGACY_SERVICE

    def __init__(self, keychain=None, owner=None, helper=None):
        """For tests: keychain is a fake Security backend, owner says who owns the item, and helper is a HelperStore
        (or False for none). By default both are decided for this process (installed_owner, the pinned helper)."""
        self._owner, self._helper = owner, helper
        self._lost = None  # an injected helper store this session stopped using: what its item lists still counts
        self.keychain = keychain
        if sys.platform == "darwin":
            if self.keychain is None:
                self.keychain = MacKeychain()
            self.lib = getattr(self.keychain, "lib", None)
        elif sys.platform == "win32":
            from ctypes import wintypes as W

            class Credential(C.Structure):
                _fields_ = [
                    ("Flags", W.DWORD),
                    ("Type", W.DWORD),
                    ("TargetName", W.LPWSTR),
                    ("Comment", W.LPWSTR),
                    ("LastWritten", W.FILETIME),
                    ("CredentialBlobSize", W.DWORD),
                    ("CredentialBlob", C.POINTER(C.c_ubyte)),
                    ("Persist", W.DWORD),
                    ("AttributeCount", W.DWORD),
                    ("Attributes", C.c_void_p),
                    ("TargetAlias", W.LPWSTR),
                    ("UserName", W.LPWSTR),
                ]

            self.Credential = Credential
            self.lib = C.WinDLL("Advapi32", use_last_error=True)
            self.lib.CredReadW.argtypes = [
                W.LPCWSTR,
                W.DWORD,
                W.DWORD,
                C.POINTER(C.POINTER(Credential)),
            ]
            self.lib.CredWriteW.argtypes = [C.POINTER(Credential), W.DWORD]
            self.lib.CredDeleteW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD]
            self.lib.CredFree.argtypes = [C.c_void_p]
        else:
            self.lib = None

    # ---- Which store ----------------------------------------------------------------------------------------

    def _owns(self):
        owner = installed_owner() if self._owner is None else self._owner
        return sys.platform == "darwin" and bool(owner)

    def helper_store(self):
        """The helper store when this process owns the item and its helper works; else None (per-item items)."""
        if not self._owns():
            return None
        if self._helper is None:
            return _auto_helper()
        return self._helper or None

    def _disable(self, error, store):
        log.warning("Vault helper stopped answering (%s); this session uses per-item Keychain storage.", error.code)
        if self._helper is None:
            _HELPER["store"], _HELPER["lost"] = None, store
        else:
            self._helper, self._lost = False, store

    def _guard(self, accounts, store=None):
        """Before the item's owner reads or writes per-item items (Phase 24, V1-009): refuse an account the item lists,
        and every account while the item exists but was never read in this process, since the kept per-item copy of
        such an account is older than the item's. Other processes keep the per-item store as their own."""
        if not self._owns():
            return
        known = store or (_HELPER.get("lost") if self._helper is None else self._lost)
        if known is not None and known.listed is not None:
            if any(known.lists(self.service, account) for account in accounts):
                raise VaultUnavailable()
        elif self.keychain.exists(VAULT_SERVICE, VAULT_ACCOUNT):
            raise VaultUnavailable()

    def _find(self, account):
        """This service's per-item secret for account (the store before Phase 22), or None."""
        raw = self.keychain.read(self.service, account.encode())
        return None if raw is None else raw.decode()

    def _section(self, data):
        return data["services"].setdefault(self.service.decode(), {"secrets": {}, "migrated": {}})

    def _through(self, operation, accounts, *args):
        """Run operation on the helper store; on a helper that cannot serve, fall back to the per-item items, except
        for the accounts _guard refuses. A helper that timed out rests and is pinged again later; any other failure
        ends its use for this session (Phase 22)."""
        store = self.helper_store()
        if store is None:
            self._guard(accounts)
            return False, None
        with _LOCK:
            try:
                store.wake()
                return True, operation(store, *args)
            except HelperUnavailable as error:
                failure = error
            except HelperError as error:  # a Keychain refusal or failure behind the helper: report it, never bypass
                raise ValueError(f"Keychain access failed (vault helper: {error.code}"
                                 + (f", {error.status}" if error.status is not None else "") + ").") from None
            if failure.code == "timeout":
                log.warning("Vault helper stopped answering (timeout); asking it again in %g s.", store.rest())
            elif failure.code != "resting":
                self._disable(failure, store)
            self._guard(accounts, store)
        return False, None

    def _remove_kept(self, account):
        """Delete this account's kept per-item item, if any (V1-017). Removal reads no item data."""
        try:
            self.keychain.remove(self.service, account.encode())
        except ValueError:
            log.warning("A kept per-item Keychain item could not be deleted; it is never read, and cleanup_legacy() "
                        "retries it.")

    # ---- Through the helper ---------------------------------------------------------------------------------

    def _helper_get(self, store, account):
        section = self._section(store.load())
        if account in section["secrets"]:
            return section["secrets"][account]
        if account in section["migrated"]:
            return ""  # migrated earlier and deleted since: the kept old item is never read again
        old = self._find(account)  # the one read of an old item (one prompt, once)
        if old is None:
            return ""
        data = store.load(fresh=True)
        section = self._section(data)
        section["secrets"][account] = old
        section["migrated"][account] = _digest(old)
        store.commit(data)
        return old

    def _mark_superseded(self, section, account):
        """A kept old item for account must not be read later: it is older than what the item now says."""
        if account not in section["migrated"] and self.keychain.exists(self.service, account.encode()):
            section["migrated"][account] = SUPERSEDED
            return True
        return False

    def _helper_set(self, store, account, secret):
        data = store.load(fresh=True)
        section = self._section(data)
        if account not in section["secrets"]:
            self._mark_superseded(section, account)
        section["secrets"][account] = secret
        store.commit(data)

    def _helper_delete(self, store, account):
        data = store.load(fresh=True)
        section = self._section(data)
        changed = section["secrets"].pop(account, None) is not None
        changed = self._mark_superseded(section, account) or changed
        if changed:
            store.commit(data)
        self._remove_kept(account)  # V1-017: the deleted secret does not live on in the kept per-item item

    def _helper_set_many(self, store, changes):
        # One read-modify-write: the map this process (its only writer) read at most CACHE_SECONDS ago, then one
        # put_all and its read-back. A deletion's kept per-item item goes once the item no longer holds the secret.
        data = store.load()
        section = self._section(data)
        removed = []
        for account, secret in changes.items():
            if secret is None:
                section["secrets"].pop(account, None)
                self._mark_superseded(section, account)
                removed.append(account)
            else:
                if account not in section["secrets"]:
                    self._mark_superseded(section, account)
                section["secrets"][account] = secret
        store.commit(data)
        for account in removed:
            self._remove_kept(account)

    # ---- Public API -----------------------------------------------------------------------------------------

    def get(self, account):
        if sys.platform == "darwin":
            served, value = self._through(self._helper_get, (account,), account)
            if served:
                return value
            return self._find(account) or ""
        if sys.platform == "win32":
            credential = C.POINTER(self.Credential)()
            if not self.lib.CredReadW("Pebble:" + account, 1, 0, C.byref(credential)):
                if C.get_last_error() == 1168:
                    return ""
                raise ValueError("Windows Credential Manager could not read this key.")
            try:
                return C.string_at(
                    credential.contents.CredentialBlob,
                    credential.contents.CredentialBlobSize,
                ).decode()
            finally:
                self.lib.CredFree(credential)
        raise ValueError("Secure API-key storage requires macOS or Windows.")

    @staticmethod
    def _valid(secret):
        raw = secret.encode()
        if not raw or len(raw) > 2400 or "\n" in secret or "\r" in secret:
            raise ValueError("Enter a valid API key.")
        return raw

    def set_many(self, changes):
        """Write several accounts at once: {account: secret, or None to delete it}. True when the vault helper is the
        store and the changes were written as one item update; False, with nothing written, when it is not (the
        caller then writes item by item with set and delete). Raises as set does, or VaultUnavailable."""
        if not isinstance(changes, dict) or not changes:
            raise ValueError("Choose the keys to save.")
        for secret in changes.values():
            if secret is not None:
                self._valid(secret)
        if sys.platform != "darwin":
            return False
        served, _ = self._through(self._helper_set_many, tuple(changes), changes)
        return served

    def set(self, account, secret):
        raw = self._valid(secret)
        if sys.platform == "darwin":
            served, _ = self._through(self._helper_set, (account,), account, secret)
            if not served:
                self.keychain.write(self.service, account.encode(), raw)
            return
        if sys.platform == "win32":
            blob = (C.c_ubyte * len(raw)).from_buffer_copy(raw)
            item = self.Credential(
                Type=1,
                TargetName="Pebble:" + account,
                CredentialBlobSize=len(raw),
                CredentialBlob=blob,
                Persist=2,
                UserName="Pebble",
            )
            if not self.lib.CredWriteW(C.byref(item), 0):
                raise ValueError("Windows Credential Manager could not save the key.")
            return
        raise ValueError("Secure API-key storage requires macOS or Windows.")

    def delete(self, account):
        if sys.platform == "darwin":
            served, _ = self._through(self._helper_delete, (account,), account)
            if not served:
                self.keychain.remove(self.service, account.encode())
        elif sys.platform == "win32":
            if (
                not self.lib.CredDeleteW("Pebble:" + account, 1, 0)
                and C.get_last_error() != 1168
            ):
                raise ValueError("Windows Credential Manager could not delete the key.")

    # ---- Maintenance (the installed app only; wired by the parent) ------------------------------------------

    def migration_status(self):
        """Counts only: where secrets live and how many old items were migrated, per service."""
        store = self.helper_store()
        if store is None:
            return {"store": "per_item", "services": {}}
        with _LOCK:
            data = store.load(fresh=True)
        return {"store": "helper", "services": {name: {"secrets": len(section["secrets"]),
                                                       "migrated": len(section["migrated"])}
                                                for name, section in sorted(data["services"].items())}}

    def cleanup_legacy(self):
        """Delete the kept old per-item items of every migrated account (after which a rollback to a build before
        Phase 22 needs a new sign-in). Counts only; an item that cannot be deleted keeps its marker for a retry."""
        store = self.helper_store()
        if store is None:
            raise ValueError("The vault helper is not in use, so the per-item items are still the store.")
        report = {}
        with _LOCK:
            data = store.load(fresh=True)
            for name, section in sorted(data["services"].items()):
                if name.encode() == VAULT_SERVICE:
                    # Phase 24 (merge, V1-017): only a service's kept per-item copies go. Nothing under the vault item's
                    # own service (com.pebble.local.vault, account v1: the current store of every sign-in and key) is
                    # ever removed here, whatever a map lists.
                    continue
                counts = {"removed": 0, "absent": 0, "failed": 0}
                for account in sorted(section["migrated"]):
                    try:
                        removed = self.keychain.remove(name.encode(), account.encode())
                    except ValueError:
                        counts["failed"] += 1
                        continue
                    counts["removed" if removed else "absent"] += 1
                    del section["migrated"][account]
                report[name] = counts
            store.commit(data)
        return report
