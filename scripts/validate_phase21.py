"""Phase 21 source/frozen JSON-RPC proof: the release identity, a HEIC photo end to end and the frozen backend's
links.

Drives the *actual* service over its JSON-RPC stdio contract, launched from main.rs's environment allowlist
names with HOME and TMPDIR in owned scratch and measure_resources' egress trap as its proxy, with one owned
loopback SSE provider and an owned home outside the checkout. The one model turn is scripted synthetic output;
every attachment goes through the route the UI calls. No real model, external network, account, vault, Keychain
or user data is used.

Checks (planned names, in campaign order):

  s1  Static: App.vue and Pet.vue attach a file with rpc("attach", { id, path }); main.rs allows attach and
      remove_source, lets the pet window attach, and attach is no model tool.
  v1  Release identity: pebble.VERSION equals the version declared by src-tauri/tauri.conf.json,
      src-tauri/Cargo.toml and package.json (and their lockfiles), and the running service's state reports it.
      In frozen mode this proves the frozen backend carries the bumped version.
  l1  Informational: with --backend, whether the executable's _internal folder holds symbolic links (the
      PyInstaller dist keeps them; build_candidate_app.py flattens the shipped stage), with the pillow_heif
      extension and libraries it carries. In source mode it records that no frozen layout applies.
  h1  A small HEIC photo, encoded in owned scratch by the repository's own pillow_heif from a generated two-colour
      RGB image, is attached through the attach route and decoded to its size and colours (the preview thumbnail
      and the stored row); sent to a scripted vision model, the request carries it as a PNG of that size and the
      chat reports its dimensions. In frozen mode this proves pillow_heif and its libraries work in the bundle.
  h2  A HEIC cut short inside its image data and one whose boxes after the file type are noise are refused with
      an honest message (no traceback, path or false size limit); nothing is stored, the service keeps answering
      and the valid photo still attaches afterwards.

Each check is recorded independently under a planned unique name with content-free evidence; source and frozen
runs record the same names. backend_inputs_stable_during_campaign re-hashes the checkout, and in frozen mode the
executable too.

Usage:
  python scripts/validate_phase21.py --report PATH [--backend PATH] [--keep-scratch]

--report must be a new file: it is created with O_CREAT|O_EXCL|O_NOFOLLOW, so an existing path or a link is
refused and nothing is overwritten. --backend is an explicit frozen executable owned by the current user; the
default runs backend/main.py from this checkout.

This is fixture evidence on this machine. It is not live-model quality, the native shell, a webview or a file
picker (the validator reads main.rs and the UI source and calls the route they call), installed or signed desktop
behaviour, a photo from a camera, UPX or code signing of the bundle, or other platforms.
"""

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import threading
import time
import tomllib
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT
from validate_phase13 import bounded, final, owned_backend, sha
from validate_phase19 import shell, tool_name
from validate_phase20 import Service, backend_path, write_new
from measure_resources import EgressTrap

MAIN_RS = ROOT / 'src-tauri/src/main.rs'
UI_FILES = ('src/App.vue', 'src/Pet.vue')
UI_ATTACH = re.compile(r'rpc\(\s*"attach"\s*,\s*\{\s*id:\s*c\.id\s*,\s*path\s*\}\s*\)')
MODEL_VISION = 'gpt-4o-phase21-fixture'  # images.vision() reads the gpt-4o prefix as image input; loopback only
MODELS = frozenset({MODEL_VISION})
CONTEXT = 32768
TRANSIENT = frozenset({'queued', 'running', 'retry_wait'})
SETTLE_SECONDS = 45
ATTACH_SECONDS = 45     # a frozen parser worker's cold start inside sandbox-exec
VERIFIER_SECONDS = 240  # verify_harness_candidate.run_validator stops a validator after this
BUDGET_SECONDS = 215    # every scenario ends by here; the report, teardown and the interpreter fit in the rest
LIMITS = {'max_steps': 10, 'max_seconds': 120}
MAX_WALK_ENTRIES = 100_000

# The HEIC fixture: two flat colour halves, so a decode is checked by size and by colour (lossy HEVC and the JPEG
# preview stay well inside the tolerance on patches away from the seam).
HEIC_SIZE = (160, 96)
LEFT, RIGHT = (204, 64, 40), (36, 96, 208)
COLOUR_TOLERANCE = 32
HEIC_QUALITY = 90
HEIC_NAME, TRUNCATED_NAME, DAMAGED_NAME = 'phase21-photo.heic', 'phase21-truncated.heic', 'phase21-damaged.heic'
HEIC_BRANDS = frozenset({b'heic', b'heix', b'mif1'})
GOAL = 'Describe the attached photo in one sentence.'
REPLY = 'Synthetic fixture: the attached photo shows two colour blocks.'
ATTACHED_TEXT = 'UNTRUSTED attached image: ' + HEIC_NAME  # images.content's text part for the photo
# A refusal is honest when it says the file could not be decoded or read (attachment_worker's and documents'
# messages, or the decoder's own invalid-input text), and never blames a limit the file does not exceed.
HONEST = re.compile(r'(?i)\b(decode|damaged|corrupt|invalid|unsupported|truncated|end of (file|data))\b')
FALSE_LIMIT = re.compile(r'(?i)\b\d+\s*MB\b|megapixel|pixels per side|credential|ten attachments|timed out')

SCENARIOS = {
    'static': (
        's1_the_ui_attaches_files_through_the_attach_route_the_shell_allows',
        'v1_the_shell_cargo_and_npm_declare_the_backend_release_version',
    ),
    'links': ('l1_the_frozen_backend_internal_folder_links_are_reported',),
    'identity': ('v1_the_running_service_reports_the_release_version',),
    'heic': (
        'h1_a_heic_photo_attached_as_the_ui_does_is_decoded_at_its_size',
        'h1_the_decoded_photo_reaches_a_vision_request_and_the_chat_reports_its_dimensions',
        'h2_a_corrupt_heic_is_refused_honestly_and_the_service_keeps_working',
    ),
}
COMMON_FIRST = ('fixture_scratch_created_outside_the_checkout',)
COMMON_LAST = ('synthetic_provider_protocol_clean', 'no_proxy_honouring_egress_was_attempted',
               'backend_inputs_stable_during_campaign')
PLANNED = COMMON_FIRST + tuple(name for checks in SCENARIOS.values() for name in checks) + COMMON_LAST
# The longest each scenario may need (frozen cold starts included). A scenario starts only if its bound still fits
# BUDGET_SECONDS, so a slow machine records it as not run instead of overrunning the verifier's limit.
SCENARIO_BOUNDS = {'static': 10, 'links': 20, 'identity': 60, 'heic': 110}


def checkout_version():
    backend_path()
    from pebble import VERSION
    return VERSION


def declared_versions():
    """The app version each release declaration names: the shell, Cargo and npm, with their lockfiles."""
    read = lambda name: (ROOT / name).read_text(encoding='utf-8')  # noqa: E731
    cargo_lock = re.search(r'\[\[package\]\]\nname = "pebble"\nversion = "([^"]+)"', read('src-tauri/Cargo.lock'))
    npm_lock = json.loads(read('package-lock.json'))
    return {'src-tauri/tauri.conf.json': json.loads(read('src-tauri/tauri.conf.json')).get('version'),
            'src-tauri/Cargo.toml': tomllib.loads(read('src-tauri/Cargo.toml'))['package'].get('version'),
            'package.json': json.loads(read('package.json')).get('version'),
            'src-tauri/Cargo.lock': cargo_lock.group(1) if cargo_lock else None,
            'package-lock.json': npm_lock.get('version'),
            'package-lock.json packages[""]': (npm_lock.get('packages') or {}).get('', {}).get('version')}


def boxes(data):
    """Top-level ISO BMFF boxes of a file: [(type, offset, size)]."""
    found, at = [], 0
    while at + 8 <= len(data):
        size, kind = struct.unpack('>I4s', data[at:at + 8])
        if size == 1 and at + 16 <= len(data):
            size = struct.unpack('>Q', data[at + 8:at + 16])[0]
        elif size == 0:
            size = len(data) - at
        if size < 8:
            break
        found.append((kind.decode('latin-1'), at, size))
        at += size
    return found


def write_bytes_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as out:
        out.write(data)


def write_heic(folder):
    """The photo, encoded by the repository's own pillow_heif from a generated RGB image, and two corrupt copies:
    one cut short inside its image data (mdat) and one whose boxes after the file type are noise."""
    from PIL import Image
    import pillow_heif
    image = Image.new('RGB', HEIC_SIZE, LEFT)
    image.paste(RIGHT, (HEIC_SIZE[0] // 2, 0, HEIC_SIZE[0], HEIC_SIZE[1]))
    encoded = io.BytesIO()
    pillow_heif.from_pillow(image).save(encoded, quality=HEIC_QUALITY)
    data = encoded.getvalue()
    found = boxes(data)
    kinds = [kind for kind, _, _ in found]
    assert data[4:8] == b'ftyp' and data[8:12] in HEIC_BRANDS, ('not a HEIC file type', data[4:12])
    assert 'mdat' in kinds and kinds[0] == 'ftyp', ('unexpected HEIC layout', kinds)
    _, mdat, mdat_size = next(box for box in found if box[0] == 'mdat')
    _, _, ftyp_size = found[0]
    noise = b''.join(hashlib.sha256(b'pebble-phase21-corrupt-heic-%d' % i).digest() for i in range(len(data) // 32 + 1))
    made = {'photo': folder / HEIC_NAME, 'truncated': folder / TRUNCATED_NAME, 'damaged': folder / DAMAGED_NAME}
    write_bytes_new(made['photo'], data)
    write_bytes_new(made['truncated'], data[:mdat + 8 + max(1, (mdat_size - 8) // 2)])
    write_bytes_new(made['damaged'], data[:ftyp_size] + noise[:len(data) - ftyp_size])
    facts = {'brand': data[8:12].decode('latin-1'), 'bytes': len(data), 'boxes': kinds,
             'truncated_bytes': made['truncated'].stat().st_size, 'pillow_heif': getattr(pillow_heif, '__version__', None)}
    with contextlib.suppress(Exception):
        facts['libheif'] = pillow_heif.libheif_version()
    return made, facts


def describe(url):
    """Format, size and the mean colour of two patches of a data-URL image: numbers only, never its bytes."""
    head, marker, data = url.partition(';base64,') if isinstance(url, str) else ('', '', '')
    if not marker or not head.startswith('data:'):
        return {'error': 'not a base64 data URL'}
    from PIL import Image, ImageStat
    try:
        with Image.open(io.BytesIO(base64.b64decode(data, validate=True))) as image:
            image.load()
            found = {'mime': head[5:], 'format': image.format, 'width': image.width, 'height': image.height}
            rgb = image.convert('RGB')
    except Exception as exc:
        return {'mime': head[5:], 'error': bounded(exc, 200)}
    w, h = rgb.size
    for side, box in (('left', (w // 10, h // 5, w * 7 // 20, h * 4 // 5)),
                      ('right', (w * 13 // 20, h // 5, w * 9 // 10, h * 4 // 5))):
        found[side] = [round(value) for value in ImageStat.Stat(rgb.crop(box)).mean]
    return found


def matches(found):
    """The decoded image is the fixture: its size, and each half's colour within the tolerance."""
    return ((found.get('width'), found.get('height')) == HEIC_SIZE
            and all(isinstance(found.get(side), list) and len(found[side]) == 3
                    and all(abs(a - b) <= COLOUR_TOLERANCE for a, b in zip(found[side], colour))
                    for side, colour in (('left', LEFT), ('right', RIGHT))))


def frozen_layout(executable):
    """The frozen backend's _internal folder as it is on disk: its symbolic links (informational: the PyInstaller dist
    keeps them and build_candidate_app.py flattens the shipped stage) and the pillow_heif binaries it carries.
    Nothing is followed or changed."""
    internal = Path(executable).parent / '_internal'
    try:
        info = os.lstat(internal)
    except FileNotFoundError:
        return {'internal_folder': False, 'links': 0, 'has_links': False}
    if not stat.S_ISDIR(info.st_mode):
        return {'internal_folder': 'link' if stat.S_ISLNK(info.st_mode) else 'not_a_directory', 'links': 0,
                'has_links': stat.S_ISLNK(info.st_mode)}
    real = Path(os.path.realpath(internal))
    links, heif, errors, entries = [], [], [], 0
    for folder, folders, files in os.walk(internal, followlinks=False, onerror=lambda e: errors.append(bounded(e, 200))):
        for name in sorted(folders + files):
            entries += 1
            if entries > MAX_WALK_ENTRIES:
                raise AssertionError(f'The _internal folder has more than {MAX_WALK_ENTRIES} entries.')
            path = Path(folder, name)
            rel = path.relative_to(internal).as_posix()
            linked = path.is_symlink()
            if linked:
                target = os.readlink(path)
                kind = 'directory' if path.is_dir() else 'file' if path.is_file() else 'dangling'
                inside = Path(os.path.realpath(path)).is_relative_to(real)
                links.append({'path': rel, 'target': target, 'kind': kind, 'absolute': os.path.isabs(target),
                              'inside': inside})
            if re.search(r'heif|de265|x265', rel, re.I) and rel.endswith(('.so', '.dylib')):
                heif.append({'path': rel, 'link': linked})
    links.sort(key=lambda link: link['path'])
    counts = {}
    for link in links:
        counts[link['kind']] = counts.get(link['kind'], 0) + 1
    return {'internal_folder': True, 'entries': entries, 'links': len(links), 'has_links': bool(links),
            'link_kinds': counts, 'absolute_or_outside': sum(link['absolute'] or not link['inside'] for link in links),
            'sample': links[:20], 'pillow_heif_binaries': sorted(heif, key=lambda item: item['path'])[:20],
            'walk_errors': errors[:5]}


class Provider:
    """Owned loopback SSE provider for the scripted vision turn. It decodes each image part a request carries and
    keeps only its format, size and two patch colours; every request is answered with REPLY."""

    def __init__(self):
        self.lock = threading.RLock()
        self.records, self.errors, self.anomalies = [], [], []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    delta, usage = provider.respond(body)
                except Exception as exc:
                    with provider.lock:
                        provider.errors.append(bounded(exc))
                    with contextlib.suppress(Exception):
                        self.send_error(500)
                    return
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
                    self.wfile.write(b'data: ' + json.dumps({'choices': [], 'usage': usage}).encode() + b'\n\n')
                    self.wfile.write(b'data: [DONE]\n\n')
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name='phase21-provider', daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, body):
        model = body.get('model')
        if model not in MODELS:
            raise AssertionError('Unexpected model route reached the fixture.')
        raw = json.dumps(body, ensure_ascii=False)
        if '_pebble_' in raw:
            raise AssertionError('Host-private message fields reached the provider.')
        images, named = [], False
        for message in body.get('messages') or []:
            content = message.get('content')
            for part in content if isinstance(content, list) else ():
                if not isinstance(part, dict):
                    continue
                if part.get('type') == 'image_url':
                    images.append(describe((part.get('image_url') or {}).get('url')))
                elif part.get('type') == 'text' and ATTACHED_TEXT in (part.get('text') or ''):
                    named = True
        with self.lock:
            self.records.append({'model': model, 'index': len(self.records), 'images': images, 'image_named': named,
                                 'tools_offered': len(body.get('tools') or []), 'time': time.monotonic()})
        return final(REPLY), {'prompt_tokens': max(1, len(raw) // 16), 'completion_tokens': 12}


class Campaign:
    def __init__(self, command, backend):
        self.command, self.backend = command, backend
        self.provider = Provider()
        self.trap = EgressTrap()  # proxy-honouring egress from the backend is refused on loopback and recorded
        self.checks, self.names, self.facts = [], set(), {}
        self.observations = {'scenario_seconds': {}, 'services': [], 'attach_seconds': {}}
        self.fatal = None
        self.stderr_tails = {}
        self.services = []
        self.deadline = time.monotonic() + BUDGET_SECONDS

    # Check bookkeeping ---------------------------------------------------------------------

    def check(self, name, fn):
        assert name in PLANNED, 'Unplanned check name: ' + name
        assert name not in self.names, 'Check names must be unique: ' + name
        self.names.add(name)
        try:
            evidence = fn() or {}
            self.checks.append({'name': name, 'passed': True, 'evidence': evidence})
            return True
        except Exception as exc:
            self.checks.append({'name': name, 'passed': False, 'error': bounded(exc, 2000),
                                'trace': traceback.format_exc(limit=4)[-1500:]})
            return False

    def finish(self):
        owner = {check: scenario for scenario, names in SCENARIOS.items() for check in names}
        errors = self.observations.get('scenario_errors', {})
        for name in PLANNED:
            if name in self.names or name == 'backend_inputs_stable_during_campaign':
                continue
            self.names.add(name)
            reason = errors.get(owner.get(name)) or self.fatal or 'an earlier step stopped the campaign.'
            self.checks.append({'name': name, 'passed': False, 'not_run': True, 'error': 'Not run: ' + reason[:600]})

    def passing(self):
        return all(c['passed'] for c in self.checks)

    def need(self, key):
        if key not in self.facts:
            raise AssertionError('Prerequisite was not established: ' + key)
        return self.facts[key]

    def scenario(self, name, fn, *args):
        """Run one scenario; an unexpected error is recorded for its checks and the campaign goes on."""
        started = time.monotonic()
        try:
            if started + SCENARIO_BOUNDS[name] > self.deadline:
                raise AssertionError(f'Not started: its {SCENARIO_BOUNDS[name]} s bound no longer fits the campaign\'s '
                                     f'{BUDGET_SECONDS} s budget.')
            fn(*args)
        except Exception as exc:
            self.observations.setdefault('scenario_errors', {})[name] = bounded(exc, 1500)
        finally:
            self.observations['scenario_seconds'][name] = round(time.monotonic() - started, 1)

    # Service helpers ------------------------------------------------------------------------

    def service(self, home, folder, label):
        found = Service(self.command, home, folder, label, extra_env=self.trap.environment())
        self.services.append(found)
        return found

    def stop(self, service):
        if service in self.services:
            self.services.remove(service)
            if not self.passing():
                self.stderr_tails[service.label] = service.stderr_tail()
            self.observations['services'].append(service.close())

    @staticmethod
    def refused(service, method, wait=15.0, **params):
        """The service's refusal text for one request; an accepted request or a failed service is not a refusal."""
        try:
            result = service.rpc(method, wait=wait, **params)
        except RuntimeError as exc:
            text = str(exc)
            if text.startswith(('Backend timed out', 'Backend stopped')):
                raise AssertionError('No refusal was observed; the service failed: ' + text[:300]) from None
            return text[:600]
        raise AssertionError(method + ' was accepted: ' + json.dumps(
            {k: result.get(k) for k in ('kind', 'name', 'bytes')} if isinstance(result, dict) else result)[:300])

    @staticmethod
    def rows(home, sql, args=()):
        """A read-only view of a running home's database (the service keeps writing to it)."""
        path = home / 'pebble.sqlite'
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, args)]

    def settle(self, service, cid, rid, seconds=SETTLE_SECONDS):
        limit = min(self.deadline + 20, time.monotonic() + seconds)
        while True:
            run = service.rpc('run_get', id=cid, run_id=rid)
            if run['state'] not in TRANSIENT and not service.rpc('state')['active']:
                return run
            if time.monotonic() > limit:
                with contextlib.suppress(Exception):
                    service.rpc('cancel', id=cid, run_id=rid)
                raise AssertionError('Timed out waiting for the run to settle: ' + run['state'])
            time.sleep(0.1)

    def attach(self, service, cid, path, label):
        started = time.monotonic()
        try:
            return service.rpc('attach', wait=ATTACH_SECONDS, id=cid, path=str(path))
        finally:
            self.observations['attach_seconds'][label] = round(time.monotonic() - started, 2)

    # Campaign --------------------------------------------------------------------------------

    def run_all(self, scratch):
        self.facts.update(scratch=scratch, home=scratch / 'home')

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout and .runtime.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            files = scratch / 'files'
            files.mkdir(mode=0o700)
            self.facts['files'] = files
            return {'outside_checkout': True, 'fixture_folder_mode': '0700'}
        self.check('fixture_scratch_created_outside_the_checkout', created)
        try:
            self.scenario('static', self.static_checks)
            self.scenario('links', self.links_check)
            self.provider.start()
            self.trap.start()
            service = self.service(self.facts['home'], scratch / 'service-main', 'main')
            try:
                self.scenario('identity', self.scenario_identity, service)
                self.scenario('heic', self.scenario_heic, service)
            finally:
                self.stop(service)
            self.final_checks()
        except Exception as exc:
            self.fatal = bounded(exc, 3000)
        finally:
            for service in list(self.services):
                with contextlib.suppress(Exception):
                    self.stop(service)
            with contextlib.suppress(Exception):
                self.provider.stop()
            with contextlib.suppress(Exception):
                self.trap.stop()
            self.finish()

    # s1 and v1: declarations -----------------------------------------------------------------

    def static_checks(self):
        backend_path()

        def attach_route():
            from pebble.tool_catalogue import TOOLS
            facts = shell(MAIN_RS.read_text(encoding='utf-8'))
            assert {'attach', 'remove_source'} <= set(facts['allowed']), 'The shell does not allow the attachment routes.'
            assert 'attach' in facts['pet'], 'The pet window cannot attach.'
            assert 'attach' not in facts['native_only'], 'attach is a shell-only route.'
            callers = {name: len(UI_ATTACH.findall((ROOT / name).read_text(encoding='utf-8'))) for name in UI_FILES}
            assert all(callers.values()), ('a UI file no longer attaches through rpc("attach", { id, path })', callers)
            assert 'attach' not in {(tool_name(t) or '').casefold() for t in TOOLS}, 'attach is a model tool.'
            return {'shell_allows': ['attach', 'remove_source'], 'pet_allows_attach': True, 'ui_callers': callers}
        self.check('s1_the_ui_attaches_files_through_the_attach_route_the_shell_allows', attach_route)

        def declared():
            version = checkout_version()
            found = declared_versions()
            different = {name: value for name, value in found.items() if value != version}
            assert not different, ('declarations that differ from pebble.VERSION ' + version, different)
            return {'version': version, 'declarations': found}
        self.check('v1_the_shell_cargo_and_npm_declare_the_backend_release_version', declared)

    # l1: the frozen layout (informational) --------------------------------------------------------

    def links_check(self):
        def layout():
            if not self.backend:
                found = {'mode': 'source', 'applicable': False,
                         'reason': 'No --backend: the source backend has no PyInstaller _internal folder.'}
            else:
                found = {'mode': 'frozen', 'applicable': True, **frozen_layout(self.backend)}
            self.observations['frozen_layout'] = {k: v for k, v in found.items() if k != 'sample'}
            return found
        self.check('l1_the_frozen_backend_internal_folder_links_are_reported', layout)

    # v1: the running service ---------------------------------------------------------------------

    def scenario_identity(self, service):
        def reported():
            state = service.rpc('state')
            version, expected = state.get('version'), checkout_version()
            assert version == expected, ('the service reports', version, 'the checkout declares', expected)
            return {'service_version': version, 'checkout_version': expected, 'frozen_backend': bool(self.backend),
                    'startup_seconds': service.facts.get('startup_seconds')}
        self.check('v1_the_running_service_reports_the_release_version', reported)

    # h1 and h2: HEIC through the attach route -----------------------------------------------------

    def scenario_heic(self, service):
        home, folder = self.facts['home'], self.need('files')
        made = {}

        def decoded():
            files, facts = write_heic(folder)
            made.update(files)
            cid = service.rpc('new_chat')['id']
            item = self.attach(service, cid, files['photo'], 'photo')
            assert item.get('kind') == 'image' and item.get('name') == HEIC_NAME, {k: item.get(k) for k in ('kind', 'name')}
            preview = describe(item.get('preview'))
            assert preview.get('mime') == 'image/jpeg' and preview.get('format') == 'JPEG' and matches(preview), preview
            stored = self.rows(home, 'SELECT width, height, length(data) AS bytes, message_id FROM image_attachments '
                                     'WHERE id=? AND chat_id=?', (item['id'], cid))
            assert len(stored) == 1 and (stored[0]['width'], stored[0]['height']) == HEIC_SIZE, stored
            assert stored[0]['bytes'] == item['bytes'] > 0 and stored[0]['message_id'] is None, stored
            made.update(cid=cid, item=item['id'])
            return {'fixture': facts, 'size': list(HEIC_SIZE), 'preview': preview, 'stored_png_bytes': item['bytes'],
                    'attach_seconds': self.observations['attach_seconds']['photo']}
        self.check('h1_a_heic_photo_attached_as_the_ui_does_is_decoded_at_its_size', decoded)

        def presented():
            if 'item' not in made:
                raise AssertionError('Prerequisite: the attached photo of the previous check.')
            profile = service.rpc('model_save', name='Phase 21 vision fixture', url=self.provider.url, model=MODEL_VISION,
                                  context=CONTEXT)['profile_id']
            service.rpc('model_select', profile_id=profile)
            with self.provider.lock:
                before = len(self.provider.records)
            rid = service.rpc('send', id=made['cid'], text=GOAL, source_ids=[made['item']], limits=dict(LIMITS))['run_id']
            run = self.settle(service, made['cid'], rid)
            assert run['state'] == 'completed', ('the photo run settled as', run['state'], str(run.get('error'))[:300])
            with self.provider.lock:
                requests = [dict(r) for r in self.provider.records[before:]]
            images = [image for request in requests for image in request['images']]
            assert requests and images, ('no image reached the provider', len(requests))
            assert all(image.get('mime') == 'image/png' and image.get('format') == 'PNG' and matches(image)
                       for image in images), images
            assert any(request['image_named'] for request in requests), 'the image part is not labelled as the photo'
            chat = service.rpc('get_chat', id=made['cid'])
            attached = [a for m in chat['messages'] if m.get('role') == 'user' for a in m.get('attachments') or []
                        if a.get('id') == made['item']]
            assert len(attached) == 1 and attached[0].get('name') == HEIC_NAME, attached
            assert (attached[0].get('width'), attached[0].get('height')) == HEIC_SIZE, attached
            replies = [m for m in chat['messages'] if m.get('role') == 'assistant' and REPLY in (m.get('content') or '')]
            assert len(replies) == 1, ('replies shown', len(replies))
            return {'run_state': run['state'], 'requests': len(requests), 'image_parts': len(images),
                    'request_image': images[0], 'chat_dimensions': [attached[0]['width'], attached[0]['height']]}
        self.check('h1_the_decoded_photo_reaches_a_vision_request_and_the_chat_reports_its_dimensions', presented)

        def corrupt():
            if 'photo' not in made:
                raise AssertionError('Prerequisite: the HEIC fixture of h1.')
            cid = service.rpc('new_chat')['id']
            refusals = {}
            for key in ('truncated', 'damaged'):
                started = time.monotonic()
                refusals[key] = self.refused(service, 'attach', wait=ATTACH_SECONDS, id=cid, path=str(made[key]))
                self.observations['attach_seconds'][key] = round(time.monotonic() - started, 2)
            for key, text in refusals.items():
                assert text.strip() and HONEST.search(text), ('not an honest decode refusal', key, text[:300])
                assert not FALSE_LIMIT.search(text), ('a refusal blames a limit the file does not exceed', key, text[:300])
                assert 'Traceback' not in text and str(folder) not in text and len(text) <= 300, (key, text[:300])
            assert service.process.poll() is None, 'The service exited after a corrupt HEIC.'
            assert isinstance(service.rpc('state').get('chats'), list), 'The service stopped answering.'
            stored = self.rows(home, 'SELECT COUNT(*) AS n FROM image_attachments WHERE chat_id=?', (cid,))[0]['n']
            assert stored == 0, ('rows stored for refused files', stored)
            again = self.attach(service, cid, made['photo'], 'photo_after_refusals')
            preview = describe(again.get('preview'))
            assert again.get('kind') == 'image' and matches(preview), preview
            assert service.rpc('remove_source', id=cid, source_id=again['id']) is True
            return {'refusals': {key: text[:160] for key, text in refusals.items()}, 'stored_rows_for_refused_files': 0,
                    'service_answered_after': True, 'valid_photo_attached_after_refusals': True}
        self.check('h2_a_corrupt_heic_is_refused_honestly_and_the_service_keeps_working', corrupt)

    # Final -------------------------------------------------------------------------------------

    def final_checks(self):
        def clean():
            with self.provider.lock:
                assert not self.provider.errors, self.provider.errors[-5:]
                assert not self.provider.anomalies, self.provider.anomalies[-5:]
                assert self.provider.records, 'The scripted provider received no request.'
                assert not [r for r in self.provider.records if r['model'] not in MODELS]
                return {'provider_attempts': len(self.provider.records)}
        self.check('synthetic_provider_protocol_clean', clean)

        def egress():
            found = self.trap.report()
            assert found['attempts'] == 0, found
            return found
        self.check('no_proxy_honouring_egress_was_attempted', egress)


def checkout_digests():
    """Digests of every checkout file this campaign reads: the backend it launches or imports, the release
    declarations, the shell and UI files it parses, this validator and the validators it imports."""
    paths = ['backend/main.py', *[p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'src-tauri/src/main.rs', *UI_FILES, 'src-tauri/tauri.conf.json', 'src-tauri/Cargo.toml',
             'src-tauri/Cargo.lock', 'package.json', 'package-lock.json', 'scripts/validate_phase21.py',
             'scripts/validate_phase20.py', 'scripts/validate_phase19.py', 'scripts/validate_phase17.py',
             'scripts/validate_phase14.py', 'scripts/validate_phase13.py', 'scripts/validate_runtime.py',
             'scripts/measure_resources.py']
    digests = {}
    for path in paths:
        try:
            digests[path] = sha((ROOT / path).read_bytes())
        except OSError:
            digests[path] = None
    return digests


def build_report(campaign, *, backend, backend_sha, sources, sources_after, duration, scratch, keep):
    results = campaign.checks
    changed = sorted(p for p in set(sources) | set(sources_after) if sources.get(p) != sources_after.get(p))
    backend_after = sha(backend.read_bytes()) if backend else None
    stable = not changed and backend_after == backend_sha
    stability = {'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                 'evidence': {'checkout_files_compared': len(sources), 'executable_compared': bool(backend)}}
    if not stable:
        stability['error'] = ('Backend inputs changed during the campaign: '
                              + json.dumps({'checkout_files': changed[:20],
                                            'executable': bool(backend) and backend_after != backend_sha}))
    results.append(stability)
    results.sort(key=lambda c: PLANNED.index(c['name']))
    complete = [c['name'] for c in results] == list(PLANNED)
    passed = complete and campaign.fatal is None and all(c['passed'] for c in results)
    with campaign.provider.lock:
        records = [dict(r) for r in campaign.provider.records]
        errors, anomalies = list(campaign.provider.errors), list(campaign.provider.anomalies)
    return {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source',
        'passed': passed,
        'checks': [c['name'] for c in results if c['passed']],
        'failed': [c['name'] for c in results if not c['passed']],
        'pending': [],
        'planned_checks': list(PLANNED), 'all_planned_checks_recorded': complete,
        'scenarios': {name: list(checks) for name, checks in SCENARIOS.items()},
        'check_results': results, 'fatal_error': campaign.fatal,
        'observations': campaign.observations,
        'duration_seconds': duration, 'duration_budget_seconds': VERIFIER_SECONDS,
        'scenario_budget_seconds': BUDGET_SECONDS, 'scenario_bounds_seconds': dict(SCENARIO_BOUNDS),
        'synthetic_provider_attempts': len(records), 'request_metadata': records,
        'provider_errors': errors, 'provider_anomalies': anomalies,
        'backend_sha256': backend_sha,
        'backend_path': str(backend) if backend else None,
        'source_sha256': sources,
        'source_note': ('Digests of this checkout. In frozen mode they do not prove the executable was built from '
                        'these files; the candidate manifest is bound separately.'),
        'scratch_preserved': str(scratch) if keep else None,
        'backend_stderr_tail': campaign.stderr_tails if not passed else {},
        'limits': ('Owned loopback synthetic provider, an owned temporary home outside the checkout, the actual service '
                   'JSON-RPC launched from the main.rs environment allowlist names (HOME and TMPDIR owned, proxy '
                   'egress trapped), the attach route the UI calls and the shell and UI read as source. The HEIC '
                   'fixture is a generated two-colour image encoded by the repository\'s pillow_heif. Fixture evidence '
                   'on this machine: no live model, webview, file picker, camera photo, installed or signed desktop, '
                   'UPX or code-signing check, or other platform.'),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary home even on success')
    args = parser.parse_args(argv)
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; move it aside or choose a new path (earlier raw evidence, '
                     'including a failed run, is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    command = [str(backend)] if backend else [sys.executable, str(ROOT / 'backend/main.py')]
    backend_sha = sha(backend.read_bytes()) if backend else None
    sources = checkout_digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase21-')).resolve()
    if scratch.is_relative_to(ROOT):
        parser.error('The temporary folder is inside this checkout; set TMPDIR elsewhere.')
    started = time.monotonic()
    campaign = Campaign(command, backend)
    try:
        campaign.run_all(scratch)
    finally:
        duration = round(time.monotonic() - started, 1)
        sources_after = checkout_digests()
    keep = args.keep_scratch or not (campaign.fatal is None and campaign.passing())
    report = build_report(campaign, backend=backend, backend_sha=backend_sha, sources=sources,
                          sources_after=sources_after, duration=duration, scratch=scratch, keep=keep)
    if not report['passed']:
        keep = True
        report['scratch_preserved'] = str(scratch)
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_new(report_path, json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': report['passed'], 'passed_checks': len(report['checks']),
                      'failed': report['failed'], 'fatal_error': campaign.fatal, 'duration_seconds': duration,
                      'scenario_seconds': campaign.observations.get('scenario_seconds'),
                      'scratch_preserved': report['scratch_preserved']}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
