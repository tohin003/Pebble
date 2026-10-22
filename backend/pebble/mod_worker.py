"""Disposable, data-only JavaScriptCore mod runner; no App/provider imports.

This module is also the source worker entry point. Frozen entry points must call
worker_main before importing the application or loading credentials.
"""
import ctypes
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

EVENTS = frozenset({'manual', 'run.state', 'context.summary', 'tool.result'})
# Dashboards carry inline data only and quizzes stay self-check (v1); mods.validate_artifacts enforces both.
KINDS = frozenset({'table', 'chart', 'plan', 'evidence', 'diagram', 'quiz', 'dashboard', 'prototype'})
INPUT_BYTES = OUTPUT_BYTES = 32_768
SOURCE_BYTES = 16_000
PAYLOAD_BYTES = 8_000
RSS_BYTES = 128 * 1024 * 1024
POLL_SECONDS = .02
JSC_LIBRARY = '/System/Library/Frameworks/JavaScriptCore.framework/JavaScriptCore'


class ModWorkerError(ValueError):
    """A sanitized worker failure. No JavaScript exception text is returned."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__('Mod worker rejected its ' + reason + ' boundary.')


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')


def _tree(value, depth=0, budget=None):
    budget = [2048] if budget is None else budget
    budget[0] -= 1
    if depth > 12 or budget[0] < 0:
        raise ModWorkerError('invalid')
    if value is None or type(value) is bool:
        return
    if type(value) in (int, float):
        if abs(value) > 1e100 or not math.isfinite(value):
            raise ModWorkerError('invalid')
        return
    if type(value) is str:
        if len(value) > 4000 or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ModWorkerError('invalid')
        return
    if type(value) is list and len(value) <= 256:
        for item in value:
            _tree(item, depth + 1, budget)
        return
    if type(value) is dict and len(value) <= 128:
        for key, item in value.items():
            if (type(key) is not str or not 1 <= len(key) <= 80
                    or key in {'__proto__', 'constructor', 'prototype'}
                    or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in key)):
                raise ModWorkerError('invalid')
            _tree(item, depth + 1, budget)
        return
    raise ModWorkerError('invalid')


def _packet(source, event, payload, timeout):
    if (type(source) is not str or '\0' in source
            or any(0xD800 <= ord(c) <= 0xDFFF for c in source)
            or not 1 <= len(source.encode('utf-8')) <= SOURCE_BYTES
            or type(event) is not str or event not in EVENTS
            or type(payload) is not dict or type(timeout) not in (int, float)
            or not .1 <= timeout <= 2 or not math.isfinite(timeout)):
        raise ModWorkerError('invalid')
    _tree(payload)
    if len(_encoded(payload)) > PAYLOAD_BYTES:
        raise ModWorkerError('invalid')
    packet = {'source': source, 'event': event, 'payload': payload, 'timeout': timeout}
    if len(_encoded(packet)) > INPUT_BYTES:
        raise ModWorkerError('invalid')
    return packet


def _panes(value):
    if type(value) is not list or len(value) > 4:
        raise ModWorkerError('invalid')
    ids = set()
    for pane in value:
        if (type(pane) is not dict or set(pane) != {'id', 'title', 'kind', 'data'}
                or type(pane['id']) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', pane['id'])
                or pane['id'] in ids or type(pane['title']) is not str
                or not 1 <= len(pane['title']) <= 120 or any(ord(c) < 32 for c in pane['title'])
                or type(pane['kind']) is not str or pane['kind'] not in KINDS
                or type(pane['data']) is not dict):
            raise ModWorkerError('invalid')
        ids.add(pane['id'])
        _tree(pane['data'])
    if len(_encoded(value)) > OUTPUT_BYTES:
        raise ModWorkerError('output')
    return value


class _TaskInfo(ctypes.Structure):
    # sys/proc_info.h struct proc_taskinfo, PROC_PIDTASKINFO = 4.
    _fields_ = [(name, ctypes.c_uint64) for name in (
        'virtual_size', 'resident_size', 'total_user', 'total_system', 'threads_user', 'threads_system')]
    _fields_ += [(name, ctypes.c_int32) for name in (
        'policy', 'faults', 'pageins', 'cow_faults', 'messages_sent', 'messages_received',
        'syscalls_mach', 'syscalls_unix', 'csw', 'threadnum', 'numrunning', 'priority')]


def _memory_reader():
    try:
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        info = library.proc_pidinfo
        info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        info.restype = ctypes.c_int
        if ctypes.sizeof(_TaskInfo) != 96:
            raise ValueError
        def resident(pid):
            task = _TaskInfo()
            if info(pid, 4, 0, ctypes.byref(task), ctypes.sizeof(task)) != ctypes.sizeof(task):
                raise ModWorkerError('memory-monitor')
            return task.resident_size
        resident(os.getpid())  # Verify availability before creating the worker.
        return resident
    except (OSError, AttributeError, ValueError):
        raise ModWorkerError('unsupported') from None


def supported():
    return sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()


def _launch(folder):
    frozen = bool(getattr(sys, 'frozen', False))
    executable = Path(sys.executable).resolve()
    framework = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
    if not frozen and framework.is_file():
        executable = framework.resolve()
    worker = Path(__file__).resolve()
    command = [str(executable), '--mod-worker'] if frozen else [str(executable), '-I', '-B', str(worker)]
    runtime = Path(getattr(sys, '_MEIPASS', executable.parent) if frozen else sys.base_prefix).resolve()
    if runtime in {Path('/'), Path.home()}:
        raise ModWorkerError('unsupported')
    quote = lambda path: json.dumps(str(path))
    ancestors = {*executable.parents, *folder.parents, *worker.parents}
    handles = ' '.join('(literal ' + quote(path) + ')' for path in ancestors)
    source = '' if frozen else '(literal ' + quote(worker) + ')'
    profile = f'''(version 1) (deny default)
      (allow process-exec (literal {quote(executable)})) (deny process-fork)
      (allow sysctl-read) (allow file-read-metadata)
      (allow file-read* {handles} {source} (literal {quote(executable)})
        (subpath {quote(runtime)}) (subpath "/System") (subpath "/usr/lib")
        (subpath "/private/var/db/dyld") (literal "/dev/null"))
      (allow file-read* file-write* (subpath {quote(folder)}))
      (deny mach-lookup (global-name "com.apple.securityd")
        (global-name "com.apple.securityd.xpc") (global-name "com.apple.secd")
        (global-name "com.apple.cfnetwork.AuthBroker"))
      (deny file-read-data file-write* (regex #"(^|/)(\\.env[^/]*|\\.ssh|\\.aws|\\.gnupg|\\.pebble|credentials|secrets|vault)(/|$)"))'''
    environment = {'HOME': str(folder), 'TMPDIR': str(folder), 'PATH': '/usr/bin:/bin',
        'LANG': 'en_US.UTF-8', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        # Request interpreter mode; resource budgets do not rely on JSC options.
        'JSC_useJIT': 'false', 'JSC_useWasm': 'false'}
    return ['/usr/bin/sandbox-exec', '-p', profile, *command], environment


def _confirmed_exit(process):
    """Confirm a disappearing process exited; never treat a live PID as reaped.

    macOS can remove task info or refuse a zombie group just before waitpid
    observes its exit. A bounded wait closes that observation window without
    accepting an unmonitored live worker or swallowing a live signal refusal.
    """
    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=POLL_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True


def _reap(process):
    # Kill the owned group even when its leader has already exited.
    try:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS answers EPERM for a group whose only member is an exited,
            # unreaped leader. Tolerate that only once waitpid confirms the
            # exit; a refusal for a live leader is a genuine cleanup failure.
            if not _confirmed_exit(process):
                raise
        process.wait(timeout=2)
    finally:
        _close(process.stdin, process.stdout, process.stderr)


def _close(*streams):
    """Close every owned pipe; the first close error surfaces after all are tried."""
    failure = None
    for stream in streams:
        if stream and not stream.closed:
            try:
                stream.close()
            except OSError as exc:
                failure = failure or exc
    if failure:
        raise failure


def run(source, event, payload, cancel=None, timeout=2):
    """Run one synchronous host event and return data-only native panes.

    Kind-specific schema validation remains the native artifact layer's duty.
    Memory is sampled RSS, not a hard kernel heap/allocation guarantee.
    """
    cancel = cancel if cancel is not None else threading.Event()
    if cancel.is_set():
        raise InterruptedError('Mod cancelled before dispatch.')
    packet = _packet(source, event, payload, timeout)
    if not supported():
        raise ModWorkerError('unsupported')
    resident = _memory_reader()
    raw = _encoded(packet)
    with tempfile.TemporaryDirectory(prefix='pebble-mod-') as scratch:
        folder = Path(scratch).resolve()
        os.chmod(folder, 0o700)
        command, environment = _launch(folder)
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=folder, env=environment, close_fds=True, start_new_session=True)
        except OSError:
            raise ModWorkerError('runtime') from None
        out, err, sent = bytearray(), bytearray(), 0
        deadline = time.monotonic() + timeout
        try:
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE, 'in')
                selector.register(process.stdout, selectors.EVENT_READ, 'out')
                selector.register(process.stderr, selectors.EVENT_READ, 'err')
                while selector.get_map() or process.poll() is None:
                    if cancel.is_set():
                        raise InterruptedError('Mod cancelled during dispatch.')
                    if time.monotonic() >= deadline:
                        raise ModWorkerError('timeout')
                    if process.poll() is None:
                        try:
                            size = resident(process.pid)
                        except ModWorkerError:
                            if not _confirmed_exit(process):
                                raise
                        else:
                            if size > RSS_BYTES:
                                raise ModWorkerError('memory')
                    for key, _ in selector.select(POLL_SECONDS):
                        if key.data == 'in':
                            sent += os.write(key.fileobj.fileno(), raw[sent:])
                            if sent == len(raw):
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                        else:
                            block = os.read(key.fileobj.fileno(), 4096)
                            if not block:
                                selector.unregister(key.fileobj)
                                continue
                            (out if key.data == 'out' else err).extend(block)
                            if len(out) > OUTPUT_BYTES or len(err) > 2048:
                                raise ModWorkerError('output')
            process.wait(timeout=max(.01, deadline - time.monotonic()))
            if cancel.is_set():
                raise InterruptedError('Mod cancelled before result acceptance.')
            if process.returncode in {-signal.SIGXCPU, 128 + signal.SIGXCPU}:
                raise ModWorkerError('timeout')
            if process.returncode:
                raise ModWorkerError('runtime')
            try:
                value = json.loads(out)
            except (ValueError, UnicodeError):
                raise ModWorkerError('invalid') from None
            if type(value) is dict and set(value) == {'error'} and value['error'] in {'invalid', 'runtime', 'output'}:
                raise ModWorkerError(value['error'])
            return _panes(value)
        except InterruptedError:
            raise
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            raise ModWorkerError('runtime') from None
        finally:
            _reap(process)


# No native callback is installed. Captured closures are held as protected JSC
# values by the worker and cannot be replaced through the global namespace.
_BOOTSTRAP = r'''
(function () {
  "use strict";
  const apply = Reflect.apply, keys = Reflect.ownKeys, own = Object.prototype.hasOwnProperty;
  const descriptor = Object.getOwnPropertyDescriptor, proto = Object.getPrototypeOf;
  const freeze = Object.freeze, define = Object.defineProperty, create = Object.create;
  const objectProto = Object.prototype, arrayProto = Array.prototype;
  const array = Array.isArray, finite = Number.isFinite, stringify = JSON.stringify;
  const code = String.prototype.charCodeAt, ErrorType = Error;
  const handlers = create(null), rendered = [], ids = create(null);
  let failure = false, active = false, attempts = 0, size = 2, count = 0;
  function reject() { failure = true; throw new ErrorType("Rejected mod data"); }
  function bytes(s) {
    let n = 0;
    for (let i = 0; i < s.length; i++) {
      const c = apply(code, s, [i]);
      if (c < 128) n++; else if (c < 2048) n += 2;
      else if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
        const next = apply(code, s, [i + 1]);
        if (next >= 0xdc00 && next <= 0xdfff) { n += 4; i++; } else reject();
      } else if (c >= 0xd800 && c <= 0xdfff) reject(); else n += 3;
    }
    return n;
  }
  function validKey(k) {
    if (typeof k !== "string" || k.length < 1 || k.length > 80 ||
        k === "__proto__" || k === "constructor" || k === "prototype") reject();
    for (let i = 0; i < k.length; i++) if (apply(code, k, [i]) < 32) reject();
    bytes(k);
  }
  function encode(v, depth, budget) {
    if (depth > 12 || --budget[0] < 0) reject();
    if (v === null) return "null";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number") {
      if (!finite(v) || v > 1e100 || v < -1e100) reject();
      return stringify(v);
    }
    if (typeof v === "string") {
      if (v.length > 4000) reject(); bytes(v); return stringify(v);
    }
    if (typeof v !== "object") reject();
    const list = array(v), p = proto(v);
    if (p !== (list ? arrayProto : objectProto) && !(p === null && !list)) reject();
    const names = keys(v);
    if (names.length > (list ? 257 : 128)) reject();
    let out = list ? "[" : "{";
    if (list) {
      const length = descriptor(v, "length").value;
      if (length > 256 || names.length !== length + 1) reject();
      for (let i = 0; i < length; i++) {
        const d = descriptor(v, stringify(i));
        if (!d || !apply(own, d, ["value"]) || !d.enumerable) reject();
        out += (i ? "," : "") + encode(d.value, depth + 1, budget);
        if (out.length > 32700) reject();
      }
    } else {
      for (let i = 0; i < names.length; i++) {
        const k = names[i]; validKey(k);
        const d = descriptor(v, k);
        if (!d || !apply(own, d, ["value"]) || !d.enumerable) reject();
        out += (i ? "," : "") + stringify(k) + ":" + encode(d.value, depth + 1, budget);
        if (out.length > 32700) reject();
      }
    }
    return out + (list ? "]" : "}");
  }
  function on(event, handler) {
    if (typeof event !== "string" ||
        (event !== "manual" && event !== "run.state" && event !== "context.summary" && event !== "tool.result") ||
        typeof handler !== "function" || handlers[event] || active) reject();
    handlers[event] = handler;
  }
  function render(pane) {
    if (!active || ++attempts > 4 || typeof pane !== "object" || pane === null || array(pane)) reject();
    const names = keys(pane);
    if (names.length !== 4) reject();
    const fields = create(null);
    for (let i = 0; i < names.length; i++) {
      const k = names[i];
      if (k !== "id" && k !== "title" && k !== "kind" && k !== "data") reject();
      const d = descriptor(pane, k); if (!d || !apply(own, d, ["value"]) || !d.enumerable) reject();
      fields[k] = d.value;
    }
    const id = fields.id, title = fields.title, kind = fields.kind;
    if (typeof id !== "string" || id.length < 1 || id.length > 64 || ids[id] ||
        typeof title !== "string" || title.length < 1 || title.length > 120 ||
        typeof kind !== "string" || (kind !== "table" && kind !== "chart" && kind !== "plan" &&
          kind !== "evidence" && kind !== "diagram" && kind !== "quiz" && kind !== "dashboard" &&
          kind !== "prototype") ||
        typeof fields.data !== "object" || fields.data === null || array(fields.data)) reject();
    for (let i = 0; i < id.length; i++) {
      const c = apply(code, id, [i]);
      if (!((c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) ||
          (i && (c === 95 || c === 46 || c === 45)))) reject();
    }
    for (let i = 0; i < title.length; i++) if (apply(code, title, [i]) < 32) reject();
    const text = encode(fields, 0, [2048]);
    size += bytes(text) + (count ? 1 : 0);
    if (size > 32700) reject();
    ids[id] = true;
    // Define own slots so hostile Array.prototype setters cannot intercept them.
    const slot = create(null); slot.value = text; slot.enumerable = true;
    define(rendered, count++, slot);
  }
  function immutable(value) {
    if (value !== null && typeof value === "object") {
      const names = keys(value);
      for (let i = 0; i < names.length; i++) immutable(descriptor(value, names[i]).value);
      freeze(value);
    }
    return value;
  }
  const api = create(null); api.on = on; api.render = render;
  // Apple's bare API context includes native console logging. Disable it before
  // source evaluation so mods have only the serialized render channel.
  define(globalThis, "console", {value: undefined, writable: false, configurable: false});
  define(globalThis, "pebble", {value: freeze(api), writable: false, configurable: false});
  return function dispatch(event, payload) {
    if (failure) reject();
    const handler = handlers[event];
    if (handler) {
      active = true;
      try {
        const result = apply(handler, undefined, [immutable(payload)]);
        if (result !== undefined) reject();
      } finally { active = false; }
    }
    if (failure) reject();
    let result = "[";
    for (let i = 0; i < count; i++) result += (i ? "," : "") + rendered[i];
    return result + "]";
  };
})()
'''


def _evaluate(packet):
    """Only called in the sandboxed child, with no Python objects in the VM."""
    lib = ctypes.CDLL(JSC_LIBRARY)
    pointer = ctypes.c_void_p
    signatures = {
        'JSGlobalContextCreate': ([pointer], pointer),
        'JSGlobalContextRelease': ([pointer], None),
        'JSStringCreateWithUTF8CString': ([ctypes.c_char_p], pointer),
        'JSStringRelease': ([pointer], None),
        'JSEvaluateScript': ([pointer, pointer, pointer, pointer, ctypes.c_int, ctypes.POINTER(pointer)], pointer),
        'JSValueProtect': ([pointer, pointer], None),
        'JSValueUnprotect': ([pointer, pointer], None),
        'JSValueMakeFromJSONString': ([pointer, pointer], pointer),
        'JSObjectCallAsFunction': ([pointer, pointer, pointer, ctypes.c_size_t, ctypes.POINTER(pointer), ctypes.POINTER(pointer)], pointer),
        'JSValueToStringCopy': ([pointer, pointer, ctypes.POINTER(pointer)], pointer),
        'JSStringGetMaximumUTF8CStringSize': ([pointer], ctypes.c_size_t),
        'JSStringGetUTF8CString': ([pointer, pointer, ctypes.c_size_t], ctypes.c_size_t),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(lib, name); function.argtypes = arguments; function.restype = result
    context = lib.JSGlobalContextCreate(None)
    if not context:
        raise ModWorkerError('runtime')
    dispatch = None
    def evaluate(source):
        text = lib.JSStringCreateWithUTF8CString(source.encode('utf-8'))
        exception = pointer()
        try:
            value = lib.JSEvaluateScript(context, text, None, None, 1, ctypes.byref(exception))
            if exception.value or not value:
                raise ModWorkerError('runtime')
            return value
        finally:
            lib.JSStringRelease(text)
    def argument(value):
        text = lib.JSStringCreateWithUTF8CString(_encoded(value))
        try:
            result = lib.JSValueMakeFromJSONString(context, text)
            if not result:
                raise ModWorkerError('invalid')
            return result
        finally:
            lib.JSStringRelease(text)
    try:
        dispatch = evaluate(_BOOTSTRAP)
        lib.JSValueProtect(context, dispatch)
        # Parse/protect input before source evaluation; captured immutable()
        # freezes it before invoking the registered handler.
        event, payload = argument(packet['event']), argument(packet['payload'])
        lib.JSValueProtect(context, event); lib.JSValueProtect(context, payload)
        try:
            evaluate(packet['source'])
            exception = pointer()
            args = (pointer * 2)(event, payload)
            value = lib.JSObjectCallAsFunction(context, dispatch, None, 2, args, ctypes.byref(exception))
            if exception.value or not value:
                raise ModWorkerError('runtime')
            text = lib.JSValueToStringCopy(context, value, ctypes.byref(exception))
            if exception.value or not text:
                raise ModWorkerError('runtime')
            try:
                maximum = lib.JSStringGetMaximumUTF8CStringSize(text)
                if maximum > OUTPUT_BYTES * 3 + 1:
                    raise ModWorkerError('output')
                buffer = ctypes.create_string_buffer(maximum)
                length = lib.JSStringGetUTF8CString(text, buffer, maximum)
                raw = buffer.raw[:length - 1]
                if len(raw) > OUTPUT_BYTES:
                    raise ModWorkerError('output')
                return _panes(json.loads(raw))
            finally:
                lib.JSStringRelease(text)
        finally:
            lib.JSValueUnprotect(context, event); lib.JSValueUnprotect(context, payload)
    finally:
        if dispatch:
            lib.JSValueUnprotect(context, dispatch)
        lib.JSGlobalContextRelease(context)


def worker_main():
    """Early source/frozen entry point. All diagnostics are fixed, sanitized JSON."""
    try:
        import resource
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_BYTES, OUTPUT_BYTES))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        raw = sys.stdin.buffer.read(INPUT_BYTES + 1)
        if len(raw) > INPUT_BYTES:
            raise ModWorkerError('invalid')
        packet = json.loads(raw)
        if type(packet) is not dict or set(packet) != {'source', 'event', 'payload', 'timeout'}:
            raise ModWorkerError('invalid')
        packet = _packet(**packet)
        cpu = math.ceil(packet['timeout'])
        # Distinct soft/hard limits produce SIGXCPU rather than an ambiguous
        # SIGKILL if CPU exhaustion wins the parent wall-clock race.
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        result = _evaluate(packet)
    except ModWorkerError as exc:
        result = {'error': exc.reason if exc.reason in {'invalid', 'output'} else 'runtime'}
    except Exception:
        result = {'error': 'runtime'}
    sys.stdout.buffer.write(_encoded(result))
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    worker_main()
