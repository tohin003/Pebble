"""Confine the document parser separately from the agent and its credentials."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# V1-051: the parser may look up only these Mach services; every other one (URL loading, DNS, keychain and
# credentials, LaunchServices, TCC, WebKit, AppleEvents) is denied by the profile's (deny default). The first two are
# what Python needs under a deny-default profile (sandbox_profiles.MACH_SERVICES, measured in Phase 14); the other
# three are read-only, local preference and type-map services. None of them can reach the network.
# test_parser_sandbox_24 measures the denials with bootstrap_look_up under this exact profile.
PARSER_MACH_SERVICES = (
    'com.apple.system.opendirectoryd.libinfo', 'com.apple.system.notification_center',
    'com.apple.cfprefsd.daemon', 'com.apple.cfprefsd.agent', 'com.apple.lsd.mapdb',
)
# IOKit user-client classes every parse may open: none.
PARSER_IOKIT_CLASSES = ()
# V1-045: what macOS's HEIF decoder (/usr/bin/sips, run by the parser) needs, granted to image parses only (an
# --extract-image parse with its scratch folder); PDF and document parses never get any of it. Measured by the
# parent on this Mac (2026-10-22) with sandbox-exec, the real sips and `log show` denials, removing each item in turn
# while a 640x480 and a 1024x768 HEIC still decoded to their sizes (docs/HARNESS_CANDIDATE_APP.md).
IMAGE_MACH_SERVICES = (
    'com.apple.coremedia.videodecoder',  # the HEVC decoder that decodes the photo's image items
    'com.apple.bsd.dirhelper',           # resolves the per-user temporary folder ImageIO writes into
)
IMAGE_IOKIT_CLASSES = (
    'IOSurfaceRootUserClient',  # the decoded frames are IOSurfaces (AGXDevice and IOSurfaceAccelerator are not needed)
)
IMAGE_RULES = (
    '(allow user-preference-read)',  # CoreFoundation preferences ImageIO reads; no preference write
    # The preferences daemon's shared-memory segments, read only.
    '(allow ipc-posix-shm-read-data ipc-posix-shm-read-metadata (ipc-posix-name-prefix "apple.cfprefs."))',
    # ImageIO's temporary files go in the per-user Darwin temp folder whatever TMPDIR says: only its direct children
    # with a hexadecimal (UUID-like) name. SBPL regex has no {n} counts, hence "+".
    r'(allow file-write* file-read* (regex #"^/private/var/folders/[^/]+/[^/]+/T/\.?[0-9A-F-]+(-[A-Za-z0-9]+)?$"))',
)
SIPS = '/usr/bin/sips'
SCRATCH_ENV = 'PEBBLE_PARSER_SCRATCH'  # images.SCRATCH_ENV; read by the worker, never by the host


def _quote(path):
    # Seatbelt literals take UTF-8, not JSON's ASCII-only \\u escapes.
    # macOS screenshot names contain U+202F before AM/PM.
    return json.dumps(str(path), ensure_ascii=False)


def parser_command(file, flag, scratch=None):
    """The worker argv: under sandbox-exec on macOS, with the profile this parse needs. Only an image parse has a
    scratch folder, and with it the image grants."""
    if scratch is not None and flag != "--extract-image":
        raise ValueError("Only an image parse gets a scratch folder.")
    file = Path(file)
    frozen = getattr(sys, "frozen", False)
    cmd = (
        [sys.executable, flag, str(file)]
        if frozen
        else [
            sys.executable,
            str(Path(__file__).parents[1] / "main.py"),
            flag,
            str(file),
        ]
    )
    if sys.platform == "darwin":
        cmd = ["/usr/bin/sandbox-exec", "-p", parser_profile(file, scratch), *cmd]
    return cmd


def parser_profile(file, scratch=None):
    """SBPL for one parse: reads of the runtime, the system and the one file; writes only to /dev/null; no network;
    Mach lookups only from PARSER_MACH_SERVICES. An image parse (a scratch folder given) also gets its scratch folder
    (where sips writes the decoded PNG) and the measured HEIF-decoder grants: IMAGE_MACH_SERVICES,
    IMAGE_IOKIT_CLASSES and IMAGE_RULES."""
    image = scratch is not None
    file = Path(file)
    frozen = getattr(sys, "frozen", False)
    executable = Path(sys.executable).resolve()
    runtime = Path(sys.executable).parent if frozen else Path(sys.prefix).resolve()
    framework = (
        Path("/Library/Frameworks/Python.framework") if not frozen else runtime
    )
    ancestors = set(executable.parents) | set(file.parents) | set(runtime.parents)
    if scratch is not None:
        ancestors |= set(Path(scratch).parents)
    handles = " ".join("(literal " + _quote(p) + ")" for p in sorted(ancestors, key=str))
    script = "" if frozen else "(subpath " + _quote(Path(__file__).parents[1]) + ")"
    services = PARSER_MACH_SERVICES + (IMAGE_MACH_SERVICES if image else ())
    classes = PARSER_IOKIT_CLASSES + (IMAGE_IOKIT_CLASSES if image else ())
    mach = " ".join("(global-name " + _quote(name) + ")" for name in services)
    work = f"(subpath {_quote(scratch)})" if image else ""
    writes = " ".join(rule for rule in (work, '(literal "/dev/null")') if rule)
    iokit = ("\n          (allow iokit-open " + " ".join("(iokit-user-client-class " + _quote(name) + ")"
                                                   for name in classes) + ")") if classes else ""
    extra = "".join("\n          " + rule for rule in IMAGE_RULES) if image else ""
    return f"""(version 1) (deny default)
          (allow process*) (allow sysctl-read) (allow file-read-metadata)
          (allow mach-lookup {mach}){iokit}
          (allow ipc-posix-shm-read-data ipc-posix-shm-read-metadata (ipc-posix-name "apple.shm.notification_center")){extra}
          (allow file-read* {handles} {script} {work} (subpath "/System") (subpath "/usr") (subpath "/bin") (subpath "/private/etc") (subpath {_quote(runtime)}) (subpath {_quote(framework)}) (literal {_quote(file)}) (literal "/dev/null"))
          (allow file-write* {writes})"""


def parse_document(file, flag="--extract-pdf"):
    file = Path(file).resolve(strict=True)
    scratch = None
    env = None
    if flag == "--extract-image" and sys.platform == "darwin":
        # A new private folder per parse: the only place the confined worker (and the sips it runs for a HEIC
        # photo) may write. It is removed afterwards, whatever happened.
        scratch = Path(tempfile.mkdtemp(prefix="pebble-parse-")).resolve()
        env = {**os.environ, SCRATCH_ENV: str(scratch), "TMPDIR": str(scratch) + "/"}
    cmd = parser_command(file, flag, scratch)
    # The parser's CPU/page/text/decompression limits are imposed by main.py.
    try:
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, env=env
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Reading this file timed out. Try a smaller file.") from None
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    if result.returncode:
        import logging
        logging.warning("Attachment parser failed (%s): %s", flag, result.stderr[-4000:])
        # Workers publish validation errors as a small JSON object on stdout;
        # never expose a traceback or misreport every failure as a size limit.
        try:
            message = json.loads(result.stdout).get("error")
        except (ValueError, AttributeError):
            message = None
        raise ValueError(message or "Could not read this file. It may be damaged or inaccessible.")
    return result.stdout


def extract_pdf(file):
    return parse_document(file)[:18000]
