import argparse
import json
import logging
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


PARSER_MEMORY_LIMIT = 768 * 1024 * 1024
# V1-010: backend.log is rotated at 5 MB and three older files are kept (backend.log.1 to .3): at most 20 MB.
LOG_BYTES, LOG_BACKUPS = 5 * 1024 * 1024, 3
LOG_FORMAT = '%(levelname)s:%(name)s:%(message)s [%(threadName)s %(asctime)s]'
RPC_LOG = 'pebble.rpc'
ERROR_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')  # the RPC error payload's content-free 'code'
CLOSE_ANSWER_SECONDS = 2.0  # a state or Stop already running at EOF is answered before the store closes


class LogFormatter(logging.Formatter):
    """'LEVEL:logger:message [thread time]'. The line starts as it always did (scripts/pebble_install.py's
    HANDLED_TRACEBACK and the probe's logs check read it), and ends with the local time, to the millisecond and with
    its UTC offset, so a failure can be placed next to a sleep, a wake or a network change."""

    def __init__(self):
        super().__init__(LOG_FORMAT)

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec='milliseconds')


def configure_logging(folder):
    """backend.log in folder, rotated; warnings from every library, Pebble's own INFO lines (refusals), and every
    uncaught thread exception with its time. Returns the handler."""
    handler = RotatingFileHandler(Path(folder) / 'backend.log', maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS,
                                  encoding='utf-8')
    handler.setFormatter(LogFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger('pebble').setLevel(logging.INFO)
    threading.excepthook = log_thread_exception
    return handler


def log_thread_exception(args):
    """threading.excepthook: a background thread that dies says so in backend.log, with its time and traceback."""
    if args.exc_type is SystemExit:
        return
    logging.getLogger('pebble.thread').error('Thread %s stopped by %s', getattr(args.thread, 'name', 'unknown'),
                                             args.exc_type.__name__,
                                             exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def refusal(exc):
    """An answer the person is meant to read: every Pebble refusal is a ValueError. Decoding errors are not."""
    return isinstance(exc, ValueError) and not isinstance(exc, (UnicodeError, json.JSONDecodeError))


def error_reply(ident, exc, friendly):
    """The RPC error payload: the message, plus 'code' when the exception carries a content-free one (sign-in
    classes, handoff_required, trashed_conversation, disk_full). friendly(exc) words database failures."""
    shown = friendly(exc) or exc
    reply = {"id": ident, "error": str(shown)}
    code = getattr(shown, 'code', None)
    if isinstance(code, str) and ERROR_CODE.fullmatch(code):
        reply['code'] = code
    return reply, shown


def method_label(method):
    """A request's method as one safe log token (a method name cannot break the line)."""
    return re.sub(r'[^A-Za-z0-9_.-]', '?', str(method)[:80]) or '?'


def _memory_watchdog(limit=PARSER_MEMORY_LIMIT, interval=0.02):
    """Parser workers exit once their peak resident memory passes the limit (a decompression bomb).

    macOS does not enforce RLIMIT_AS, so a daemon thread samples the process's own high-water mark."""
    if os.name == 'nt':
        return
    import resource, sys, threading, time
    scale = 1 if sys.platform == 'darwin' else 1024  # ru_maxrss: bytes on macOS, KiB on Linux

    def watch():
        while True:
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale > limit:
                os.write(1, b'{"error": "This file needs too much memory to read safely."}')
                os._exit(3)
            time.sleep(interval)
    threading.Thread(target=watch, name='parser-memory-watchdog', daemon=True).start()


def main():
    # Phase 22: Pebble for Chrome's native messaging host (Chrome starts it through the launcher that
    # scripts/install_native_host.py writes). It imports only the standard library and reads only the bridge token.
    # It must precede argparse, because Chrome appends the calling extension's origin.
    if len(sys.argv) > 1 and sys.argv[1] == '--browser-native-host':
        from pebble.native_host import main as native_host_main
        sys.exit(native_host_main(sys.argv[2:]))
    # Export readback worker (Phase 17): confined and data-only; runs before App or credentials load.
    if len(sys.argv) > 1 and sys.argv[1] == '--export-worker':
        from pebble.export_worker import worker_main as export_worker_main
        export_worker_main()
        return
    # Per-job supervisor and in-sandbox anchor (Phase 14 containment) share
    # this executable, so the frozen app needs no separate interpreter.
    if len(sys.argv) > 1 and sys.argv[1] in ('--job-anchor', '--job-shim', '--job-canary'):
        if sys.argv[1] == '--job-anchor':
            from pebble.containment import anchor_main
            sys.exit(anchor_main(sys.argv))
        if sys.argv[1] == '--job-canary':  # the containment self-test's outside canary
            from pebble.containment import canary_main
            sys.exit(canary_main(sys.argv))
        from pebble.job_shim import main as shim_main
        sys.exit(shim_main(sys.argv))
    parser = argparse.ArgumentParser()
    parser.add_argument("--home")
    # Phase 22: the folder Pebble used before ~/.pebble (the shell's app_data_dir); moved once, never deleted.
    parser.add_argument("--legacy-home")
    parser.add_argument("--resources")
    parser.add_argument("--extract-pdf")
    parser.add_argument("--extract-image")
    parser.add_argument("--extract-file")
    parser.add_argument('--hook-worker', action='store_true')
    parser.add_argument('--mod-worker', action='store_true')
    parser.add_argument('--mcp-http-worker', action='store_true')
    parser.add_argument('--mcp-stdio-worker', action='store_true')
    parser.add_argument('--mcp-schema-worker', action='store_true')
    parser.add_argument('--mcp-media-image-worker')
    parser.add_argument('--ax-worker', action='store_true')
    # Phase 22 (k22-identity): one-shot vault maintenance, run by the parent as the installed bundle's own backend (the
    # vault helper's owner): `status` reports the one-time Keychain migration; `cleanup-legacy` deletes the old
    # per-item Keychain items only when asked (after the person no longer wants a no-sign-in rollback).
    parser.add_argument('--vault-maintenance', choices=('status', 'cleanup-legacy'))
    args = parser.parse_args()
    if args.vault_maintenance:
        from pebble.vault import Vault
        result = Vault().cleanup_legacy() if args.vault_maintenance == 'cleanup-legacy' else Vault().migration_status()
        print(json.dumps(result))
        return
    if args.mod_worker:
        from pebble.mod_worker import worker_main
        worker_main()
        return
    if args.mcp_media_image_worker:
        from pebble.mcp_media import worker_main
        worker_main(args.mcp_media_image_worker)
        return
    if args.mcp_http_worker:
        from pebble.mcp_transport import worker_main
        worker_main()
        return
    if args.mcp_stdio_worker:
        from pebble.mcp_transport import stdio_supervisor
        stdio_supervisor()
        return
    if args.mcp_schema_worker:
        from pebble.mcp_transport import schema_worker_main
        schema_worker_main()
        return
    if args.ax_worker:
        # Phase 16 D04: serves only the blocked backend until experiment E-N2.
        from pebble.ax_worker import worker_main as ax_main
        ax_main()
        return
    if args.hook_worker:
        from pebble.hook_worker import main as hook_main
        hook_main()
        return
    if args.extract_image or args.extract_file or args.extract_pdf:
        _memory_watchdog()
    if args.extract_image or args.extract_file:
        if os.name != 'nt':
            import resource
            resource.setrlimit(resource.RLIMIT_CPU,(8,8))
        from pebble.attachment_worker import extract
        extract('--extract-image' if args.extract_image else '--extract-file', args.extract_image or args.extract_file)
        return
    if args.extract_pdf:
        if os.name != "nt":
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        from pypdf import PdfReader
        import pypdf.filters

        pypdf.filters.ZLIB_MAX_OUTPUT_LENGTH = 8_000_000

        reader = PdfReader(args.extract_pdf)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF")
        chunks = []
        for page in reader.pages[:30]:
            chunks.append(page.extract_text() or "")
            if sum(map(len, chunks)) > 18000:
                break
        print("\n".join(chunks)[:18000])
        return
    if not args.resources:
        parser.error("--resources is required")
    os.umask(0o077)
    lock = threading.Lock()
    output = sys.stdout
    # Libraries must never corrupt the JSON protocol.
    sys.stdout = sys.stderr

    def write(data):
        with lock:
            output.write(json.dumps(data, ensure_ascii=False) + "\n")
            output.flush()

    emit = lambda name, data: write({"event": name, "data": data})
    # Phase 22 (s22-storage): --home, then PEBBLE_HOME, then ~/.pebble. A --home that is not ~/.pebble and came
    # without --legacy-home is a flat home exactly as before (tests and validators): the database sits in it.
    from pebble import data_home

    layout = data_home.resolve(args.home, args.legacy_home)
    if layout.structured:
        logs = layout.root / data_home.LOGS
        try:
            data_home.ensure_layout(layout)
        except OSError:
            # ~/.pebble is a link, a file or not ours: stay on the old home when it has data, and say so.
            if layout.legacy is None or not os.path.lexists(layout.legacy / data_home.DATABASE):
                raise
            report = {"state": "refused", "reason": "home_unusable", "data": str(layout.legacy),
                      "message": data_home.REASONS["home_unusable"]}
            logs = layout.legacy
        else:
            moving = data_home.move_pending(layout)
            if moving:
                emit("storage_migration", {"state": "checking"})
            report = data_home.migrate(layout)  # never raises; on a refusal or failure it names the old home
        home = Path(report["data"])
        data_home.activate(layout, home, report)
        if report.get("state") not in ("current", "fresh") or report.get("warning") or report.get("legacy_changed"):
            emit("storage_migration", {key: report.get(key) for key in ("state", "reason", "message", "warning")})
    else:
        home = logs = Path(args.home) if args.home else layout.root  # a --home exactly as given, as before
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        configure_logging(logs)
    except (OSError, ValueError):
        pass  # the Phase 22 log below, without rotation, rather than no log
    # Phase 22's flat-home log, kept as the fallback: once configure_logging has added its handler, this does nothing.
    logging.basicConfig(filename=logs / "backend.log", level=logging.WARNING)
    from pebble.app import App
    from pebble.store import error_kind, friendly_error

    def start():
        return App(home, Path(args.resources), emit)

    from pebble.migrations import MigrationRefused
    from pebble.maintenance import REMEDIABLE, Maintenance

    try:
        app = start()
    except MigrationRefused as exc:
        # A refusal the user can resolve (a full backup budget) serves the
        # maintenance routes instead of exiting; every other refusal still stops.
        if exc.reason not in REMEDIABLE:
            raise
        logging.warning("Startup is waiting for an upgrade step: %s", exc.reason)
        app = Maintenance(home, exc, start, emit)

    rpc_log = logging.getLogger(RPC_LOG)

    def request(req):
        try:
            result = app.dispatch(req["method"], req.get("params", {}))
            write({"id": req["id"], "result": result})
        except Exception as exc:
            reply, shown = error_reply(req.get("id"), exc, friendly_error)
            if shown is not exc or refusal(exc):
                # V1-010: an answered refusal is not a crash: one INFO line with its class and code, no traceback.
                rpc_log.info("Request refused: %s (%s%s)", method_label(req.get("method")), error_kind(exc),
                             ", " + reply["code"] if "code" in reply else "")
            else:
                logging.exception("Request failed: %s", method_label(req.get("method")))
            write(reply)

    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pebble-rpc")
    # V1-011: Stop and the status poll each have their own threads. Before, both ran on this reader thread, so a
    # slow or blocked state call delayed every Stop behind it (11-13 s at 1,200 conversations).
    lanes = {"cancel": ThreadPoolExecutor(max_workers=2, thread_name_prefix="pebble-rpc-cancel"),
             "state": ThreadPoolExecutor(max_workers=1, thread_name_prefix="pebble-rpc-state")}
    answering = set()
    try:
        for line in sys.stdin:
            if len(line) > 256000:
                continue
            try:
                req = json.loads(line)
                if (
                    not isinstance(req, dict)
                    or not isinstance(req.get("id"), str)
                    or not isinstance(req.get("method"), str)
                    or not isinstance(req.get("params", {}), dict)
                ):
                    continue
                if req["method"] in lanes:
                    answering = {future for future in answering if not future.done()}
                    answering.add(lanes[req["method"]].submit(request, req))
                else:
                    pool.submit(request, req)
            except ValueError:
                continue
    finally:
        for executor in (pool, *lanes.values()):
            executor.shutdown(wait=False, cancel_futures=True)
        # Answered before the store closes under them, as when they ran on the reader thread; bounded.
        wait(answering, timeout=CLOSE_ANSWER_SECONDS)
        app.close()


if __name__ == "__main__":
    main()
