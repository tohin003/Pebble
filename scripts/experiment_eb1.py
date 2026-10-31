#!/usr/bin/env python3
"""Experiment E-B1: does Google Chrome run Pebble's private browser over a CDP pipe? (Phase 22, b22-browser)

The parent runs this once on the Mac, after the person approves. It launches the
installed Google Chrome through browser_session's own PipeLauncher, with a fresh
private profile in a new temporary folder, and records evidence that would justify
adding Chrome to browser_session.VERIFIED_BROWSERS (criteria in
docs/HARNESS_BROWSER_EXTENSION.md §7):

  bundle      the app's bundle ID, version, signing team and signature state;
  argv        --remote-debugging-pipe, the private --user-data-dir, no TCP flag;
  handshake   Browser.getVersion answered over the pipe (product, protocol);
  no_port     lsof shows no listening TCP socket for the browser process, and the
              profile holds no DevToolsActivePort file;
  navigation  a tab opened on a loopback fixture page commits to that origin;
  snapshot    the fixture's heading, text field and button read back with roles;
  input       typing into the text field inside the effect guard reads back;
  secure      a password page hands off and its value never leaves the adapter;
  exit        stop() ends the browser cleanly and no process keeps the profile;
  profile     the profile was private (0700, marked) and is deleted afterwards.

Dry run by default: it prints the plan and touches nothing. --execute needs
--i-am-the-parent-with-user-approval naming --stamp (as scripts/pebble_install.py).
The evidence is a new file (O_EXCL) in .runtime/phase22-eb1/. Nothing here
changes VERIFIED_BROWSERS: the parent decides from the evidence.

Recording a passing run (second step, same approval rule):

  --record EVIDENCE [--home DATA_HOME] --execute --stamp S --i-am-the-parent-with-user-approval S

checks that EVIDENCE passed every criterion, then writes a 0600 copy as
<home>/browser/e-b1/eb1-<stamp>.json and <home>/browser/e-b1/verified.json
naming its SHA-256, bundle ID, Chrome version and signing team. At start the
backend's browser_session.launcher_from_evidence(home) reads that record back,
re-checks the digest and the criteria, and builds PipeLauncher(verified=…). The
launcher refuses to start a Chrome whose version differs, so E-B1 runs again
after every Chrome update. Without --execute, --record only reports what it
would write.

Exit codes: 0 every criterion held (or a dry run), 1 a criterion failed (evidence
written), 2 usage, 3 refused before anything ran.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(ROOT / 'backend'))

from pebble import browser_session as bs  # noqa: E402
from pebble.screen_session import ScreenSessions  # noqa: E402
from pebble.store import Store  # noqa: E402

FORMAT = bs.EB1_FORMAT
APP = '/Applications/Google Chrome.app'
EVIDENCE = ROOT / '.runtime' / 'phase22-eb1'
DATA_HOME = Path.home() / 'Library/Application Support/com.pebble.local'  # main.rs app_data_dir; only with --record
STAMP = re.compile(r'\d{8}T\d{6}Z')
LSOF, PS = '/usr/sbin/lsof', '/bin/ps'
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3
SECRET = 'eb1-secret-value-7f3a'
CRITERIA = bs.EB1_CRITERIA
PAGES = {
    '/plain': ('<!doctype html><title>E-B1 fixture</title><h1>E-B1 fixture</h1>'
               '<label>Name <input id="name" type="text" autocomplete="off"></label>'
               '<button type="button" id="go">Go</button>'),
    '/secure': (f'<!doctype html><title>E-B1 secure</title><h1>Sign in</h1>'
                f'<label>Password <input type="password" value="{SECRET}"></label>'),
}


class Fixture:
    """A loopback HTTP server for the two fixture pages (127.0.0.1, an ephemeral port, GET only)."""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = PAGES.get(self.path.split('?', 1)[0])
                data = (body or 'not found').encode()
                self.send_response(200 if body else 404)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def default_lsof(pid):
    done = subprocess.run([LSOF, '-nP', '-a', '-p', str(pid), '-iTCP', '-sTCP:LISTEN'], stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=20, check=False)
    return [line for line in done.stdout.decode('utf-8', 'replace').splitlines()[1:] if line.strip()]


def default_ps():
    done = subprocess.run([PS, '-axww', '-o', 'pid=,args='], stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                          check=False)
    return done.stdout.decode('utf-8', 'replace').splitlines()


PAGE_FACTS_JS = ('JSON.stringify({visibility:document.visibilityState,has_focus:document.hasFocus(),'
                 'active:document.activeElement?(document.activeElement.id||document.activeElement.tagName):null,'
                 'field:(document.getElementById("name")||{}).value||""})')


def page_facts(browser, tab_id):
    """The fixture page's own focus facts (a diagnosis around the input act, never a criterion)."""
    try:
        tab, _ = browser.attach(tab_id)
        result = browser.conn.call('Runtime.evaluate', {'expression': PAGE_FACTS_JS, 'returnByValue': True},
                                   session=tab.session, timeout=browser.timeout)
        value = (result.get('result') or {}).get('value')
        return json.loads(value) if isinstance(value, str) else {'unreadable': 'no value'}
    except Exception as exc:
        return {'unreadable': type(exc).__name__}


GEOMETRY_JS = ('JSON.stringify({dpr:devicePixelRatio,inner:[innerWidth,innerHeight],'
               'rect:(()=>{const e=document.getElementById("name");if(!e)return null;'
               'const r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height]})()})')


def geometry_facts(browser, tab_id):
    """Where the fixture's text field is by each measure Pebble uses (a diagnosis for a moved-element refusal)."""
    facts = {}
    try:
        tab, _ = browser.attach(tab_id)

        def call(method, params=None):
            return browser.conn.call(method, params or {}, session=tab.session, timeout=browser.timeout)
        value = (call('Runtime.evaluate', {'expression': GEOMETRY_JS, 'returnByValue': True}).get('result') or {}).get(
            'value')
        facts['page'] = json.loads(value) if isinstance(value, str) else None
        metrics = call('Page.getLayoutMetrics')
        facts['layout_metrics'] = {k: metrics.get(k) for k in ('cssContentSize', 'contentSize', 'cssLayoutViewport',
                                                                'layoutViewport', 'cssVisualViewport', 'visualViewport')}
        raw = call('DOMSnapshot.captureSnapshot', {'computedStyles': [], 'includeDOMRects': True})
        for item in raw.get('documents') or []:
            facts.setdefault('snapshot_documents', []).append(
                {k: item.get(k) for k in ('contentWidth', 'contentHeight', 'scrollOffsetX', 'scrollOffsetY')})
        for doc in bs._documents(raw).values():
            for node in doc.by_index:
                if node.name == 'INPUT' and node.attrs.get('id') == 'name':
                    facts['snapshot_bounds'], facts['snapshot_scroll'] = node.bounds, list(doc.scroll)
        root = call('DOM.getDocument', {'depth': 0}).get('root', {}).get('nodeId')
        node = call('DOM.querySelector', {'nodeId': root, 'selector': '#name'}).get('nodeId')
        quads = call('DOM.getContentQuads', {'nodeId': node}).get('quads')
        facts['content_quad'] = quads[0] if isinstance(quads, list) and quads else None
    except Exception as exc:
        facts['unreadable'] = type(exc).__name__
    return facts


def default_front():
    """The frontmost app's bundle ID (lsappinfo), or None."""
    try:
        asn = subprocess.run(['/usr/bin/lsappinfo', 'front'], stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=10, check=False).stdout.decode().strip()
        info = subprocess.run(['/usr/bin/lsappinfo', 'info', '-only', 'bundleid', asn], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=10, check=False).stdout.decode()
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'bundleID="([A-Za-z0-9.-]{1,255})"', info)
    return match.group(1) if match else None


def launched_argv(env, pid):
    """The browser's command line as the system reports it (ps), or None."""
    for line in env.ps() if pid else ():
        head, _, rest = line.strip().partition(' ')
        if head == str(pid):
            return rest.split(' ')
    return None


class Env:
    """What the experiment touches outside Python; tests replace each part with a fake."""

    def __init__(self, *, pin=None, launcher=None, lsof=default_lsof, ps=default_ps, fixture=Fixture, clock=time.time,
                 monotonic=time.monotonic, sleep=time.sleep, tempdir=None, pages=None, front=None):
        if pin is None:
            from pebble.native_apps import pin as pin
        self.pin, self.launcher, self.lsof, self.ps, self.fixture = pin, launcher, lsof, ps, fixture
        self.front = front  # the frontmost app, read only on the Mac (main); tests leave it out
        self.clock, self.monotonic, self.sleep, self.tempdir, self.pages = clock, monotonic, sleep, tempdir, pages


def plan(app, evidence_dir, stamp):
    return {'format': FORMAT, 'state': 'dry_run', 'app': app, 'evidence': str(Path(evidence_dir) / f'eb1-{stamp}.json'),
            'steps': ['read the bundle identity (native_apps.pin)', 'create a fresh private profile in a new temporary folder',
                      'launch over --remote-debugging-pipe with PipeLauncher (verified only for this run)',
                      'Browser.getVersion over the pipe', 'lsof: no listening TCP socket; no DevToolsActivePort file',
                      'serve two fixture pages on 127.0.0.1; open a tab on each',
                      'observe, type into the text field, observe the password page (hand-off)',
                      'stop the browser; check no process keeps the profile', 'forget the profile; delete the folder',
                      'write the evidence (O_EXCL)'],
            'needs': '--execute --stamp STAMP --i-am-the-parent-with-user-approval STAMP', 'changes_verified_browsers': False}


def _private(path):
    info = os.lstat(path)
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700


def experiment(app, stamp, env):
    """Run every step, record each criterion, and always clean up. Returns the evidence document."""
    started = env.clock()
    checks = {name: {'passed': False} for name in CRITERIA}
    record = {'format': FORMAT, 'experiment': 'E-B1', 'stamp': stamp, 'app': app, 'host': host_facts(),
              'started_at': datetime.fromtimestamp(started, timezone.utc).isoformat(), 'checks': checks, 'notes': []}
    folder = Path(env.tempdir or tempfile.mkdtemp(prefix='pebble-eb1-'))
    home = folder / 'home'
    browser = store = fixture = None
    try:
        entry = env.pin(app)
        bundle = entry.get('bundle_id')
        checks['bundle'] = {'passed': bundle in bs.CHROMIUM_BUNDLES and entry.get('signature') == 'team'
                            and bool(entry.get('team_id')), **{k: entry.get(k) for k in
                                                                ('bundle_id', 'version', 'team_id', 'signature')}}
        if not checks['bundle']['passed']:
            raise RuntimeError('bundle')
        verified = {bundle: {'experiment': 'E-B1', 'run': stamp}}  # this run only; the module's table is untouched
        launcher = env.launcher(entry, verified) if env.launcher else bs.PipeLauncher(entry, verified=verified,
                                                                                         pin=env.pin)
        home.mkdir(mode=0o700, parents=True)
        store = Store(home)
        browser = bs.PebbleBrowser(bs.BrowserProfile(home), launcher=launcher)
        t0 = env.monotonic()
        status = browser.start()
        profile = browser.profile.path
        pid = getattr(browser.process, 'pid', None)
        argv = launched_argv(env, pid) or getattr(launcher, 'argv', None) or []
        flags = ' '.join(argv)
        checks['argv'] = {'passed': '--remote-debugging-pipe' in argv and f'--user-data-dir={profile}' in flags
                          and not any(a.split('=', 1)[0] in bs.FORBIDDEN_FLAGS for a in argv), 'argv': argv[:24],
                          'source': 'ps' if launched_argv(env, pid) else 'launcher'}
        version = browser.conn.call('Browser.getVersion')
        checks['handshake'] = {'passed': str(version.get('product', '')).startswith(('Chrome/', 'HeadlessChrome/'))
                               and version.get('protocolVersion') == '1.3', 'product': status.get('product'),
                               'protocol': version.get('protocolVersion'), 'seconds': round(env.monotonic() - t0, 3)}
        listening = env.lsof(pid) if pid else ['no pid']
        active_port = (profile / 'DevToolsActivePort').exists()
        checks['no_port'] = {'passed': not listening and not active_port, 'listening': listening[:8],
                             'devtools_active_port_file': active_port, 'pid': pid}
        checks['profile'] = {'passed': _private(profile) and (profile / bs.PROFILE_MARKER).is_file(), 'mode': '0700'}
        fixture = env.fixture()
        adapter = bs.BrowserAdapter(browser)
        sessions = ScreenSessions(store, {'browser': adapter})
        origin = bs.url_origin(fixture.base + '/')
        chat, run = uuid.uuid4().hex, uuid.uuid4().hex
        plain = browser.open_tab(fixture.base + '/plain')
        env.sleep(1.0)
        rows = {r['tab_id']: r for r in browser.tabs()}
        checks['navigation'] = {'passed': rows.get(plain['tab_id'], {}).get('origin') == origin, 'origin': origin,
                                'tabs': len(rows)}
        sid = sessions.grant(chat, run, {'kind': 'browser', 'profile': 'pebble', 'tab_id': plain['tab_id'],
                                         'origins': [origin]}, ['focus', 'type'], 300)['session_id']
        view = sessions.observe(sid, run)
        roles = sorted({(e['role'], e.get('label', '')) for e in view.get('elements', [])})
        found = {'heading': ('Heading', 'E-B1 fixture') in roles, 'field': any(r == 'TextField' for r, _ in roles),
                 'button': ('Button', 'Go') in roles}
        checks['snapshot'] = {'passed': view.get('state') == 'active' and all(found.values()), 'found': found,
                              'elements': len(view.get('elements', [])), 'focused': view.get('window', {}).get('focused')}
        field = next((e for e in view.get('elements', []) if e['role'] == 'TextField'), None)
        # 2026-10-25: the first Mac run's input ended dispatch_stale_ui with no word on which re-check stopped it, so
        # each refusal's own code, detail and whether anything was sent are kept, with the page's focus facts around
        # the act. A diagnosis only: the criterion is unchanged.
        refusals, perform = [], adapter.perform

        def recorded(*args, **kwargs):
            try:
                return perform(*args, **kwargs)
            except Exception as exc:
                refusals.append({'code': getattr(exc, 'code', type(exc).__name__),
                                 'detail': ' '.join(str(getattr(exc, 'detail', None) or '').split())[:300],
                                 'dispatched': (getattr(exc, 'facts', None) or {}).get('dispatched')})
                raise
        adapter.perform = recorded
        before = {'page': page_facts(browser, plain['tab_id']), 'front_app': env.front() if env.front else None}
        result = sessions.act(sid, run, view['observation_id'], field['ref'], 'type', 'Pebble') if field else {}
        adapter.perform = perform
        checks['input'] = {'passed': result.get('verdict') == 'occurred', 'verdict': result.get('verdict'),
                           'basis': result.get('basis'), 'blocked_requests': browser.effect_log(plain['tab_id']),
                           'diagnosis': {'refusals': refusals, 'before': before,
                                         'observed_frame': field.get('frame') if field else None,
                                         'after': {'page': page_facts(browser, plain['tab_id']),
                                                   'geometry': geometry_facts(browser, plain['tab_id'])}}}
        secure = browser.open_tab(fixture.base + '/secure')
        env.sleep(1.0)
        other = sessions.grant(chat, run, {'kind': 'browser', 'profile': 'pebble', 'tab_id': secure['tab_id'],
                                           'origins': [origin]}, ['focus'], 300)['session_id']
        handoff = sessions.observe(other, run)
        raw = adapter.snapshot({'kind': 'browser', 'profile': 'pebble', 'tab_id': secure['tab_id'], 'origins': [origin]},
                               {'instance': browser.instance, 'tab_id': secure['tab_id']}, 300)
        leaked = SECRET in json.dumps(raw) or SECRET in json.dumps(store.query('SELECT * FROM screen_sessions'))
        checks['secure'] = {'passed': handoff.get('state') == 'handoff' and 'secure_field' in handoff.get('reasons', [])
                            and not leaked, 'reasons': handoff.get('reasons'), 'value_left_adapter': leaked}
    except Exception as exc:  # every failure still leaves evidence, and the cleanup below always runs
        record['notes'].append({'stopped_at': next((n for n in CRITERIA if not checks[n].get('passed')), None),
                                'error': type(exc).__name__, 'detail': ' '.join(str(exc).split())[:300]})
    finally:
        record['checks']['exit'] = finish(browser, env, record)
        if fixture is not None:
            fixture.close()
        if store is not None:
            store.close()
        forgotten = None
        if browser is not None:
            try:
                forgotten = browser.profile.forget()
            except Exception as exc:  # a profile we cannot prove is ours is left for the parent to inspect
                record['notes'].append({'profile_forget': type(exc).__name__})
        if checks['profile'].get('passed'):
            checks['profile']['forgotten'] = bool(forgotten and forgotten.get('forgotten'))
            checks['profile']['passed'] = checks['profile']['forgotten']
        if env.tempdir is None:
            shutil.rmtree(folder, ignore_errors=True)
        record['folder_removed'] = not folder.exists() if env.tempdir is None else None
    record['finished_at'] = datetime.fromtimestamp(env.clock(), timezone.utc).isoformat()
    record['passed'] = all(checks[name].get('passed') is True for name in CRITERIA)
    return record


def finish(browser, env, record):
    """Stop the browser over its pipe and check nothing keeps the profile afterwards."""
    if browser is None or browser.process is None:
        return {'passed': False, 'reason': 'never started'}
    process, profile = browser.process, str(browser.profile.path)
    t0 = env.monotonic()
    try:
        stopped = browser.stop()
    except Exception as exc:
        return {'passed': False, 'reason': type(exc).__name__}
    seconds = round(env.monotonic() - t0, 3)
    env.sleep(5.0)
    keepers = [line.strip()[:200] for line in env.ps() if f'--user-data-dir={profile}' in line]
    code = getattr(process, 'returncode', None)
    return {'passed': stopped is True and code is not None and code >= 0 and not keepers and seconds <= 15,
            'returncode': code, 'seconds': seconds, 'processes_left': keepers[:8]}


def host_facts():
    import platform
    return {'macos': platform.mac_ver()[0] or None, 'machine': platform.machine(), 'python': platform.python_version()}


def write_evidence(record, evidence_dir, stamp):
    folder = Path(evidence_dir)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f'eb1-{stamp}.json'
    data = (json.dumps(record, indent=2, sort_keys=True) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
    return path, hashlib.sha256(data).hexdigest()


def proposal(record, path, digest):
    """The VERIFIED_BROWSERS entry the evidence would support (for the parent to review; never applied here)."""
    if not record.get('passed'):
        return None
    bundle = record['checks']['bundle']
    return {bundle['bundle_id']: {'experiment': 'E-B1', 'evidence': str(path), 'evidence_sha256': digest,
                                  'version': bundle['version'], 'team_id': bundle['team_id'], 'stamp': record['stamp']}}


def _stamp_arg(value):
    if not STAMP.fullmatch(value):
        raise argparse.ArgumentTypeError('use a UTC stamp such as 20261014T120000Z')
    return value


def _private_folder(path):
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f'{path} is not a private folder of this user.')


def _write_private(path, data, *, exclusive):
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def record_evidence(evidence, home, stamp, *, execute, clock=time.time):
    """Record a passing E-B1 run for the App: the evidence copied under <home>/browser/e-b1 and verified.json naming
    it by SHA-256. Pebble's own browser then starts only with that Chrome at that version (load_verified)."""
    evidence, home = Path(evidence), Path(home)
    raw = evidence.read_bytes()
    if len(raw) > bs.MAX_EVIDENCE:
        raise ValueError('The evidence file is too large.')
    doc = json.loads(raw.decode('utf-8'))
    facts = bs.check_eb1_evidence(doc)  # a failed or partial run is never recorded
    if doc.get('stamp') != stamp:
        raise ValueError('--stamp must be the stamp of the evidence being recorded.')
    folder, path = bs.eb1_paths(home)
    copy = folder / f'eb1-{stamp}.json'
    record = {'format': bs.EB1_RECORD_FORMAT, 'experiment': 'E-B1', 'stamp': stamp, 'evidence': copy.name,
              'evidence_sha256': hashlib.sha256(raw).hexdigest(), **facts,
              'recorded_at': datetime.fromtimestamp(clock(), timezone.utc).isoformat()}
    actions = [{'action': 'copy_evidence', 'path': str(copy)}, {'action': 'write_record', 'path': str(path)}]
    if not execute:
        return {'format': FORMAT, 'state': 'dry_run', 'record': record, 'actions': actions}
    if not home.is_dir():
        raise ValueError("Pebble's data folder does not exist.")
    _private_folder(home / 'browser')
    _private_folder(folder)
    if copy.exists():
        if copy.read_bytes() != raw:
            raise ValueError('Different evidence with this stamp is already recorded; nothing was changed.')
    else:
        _write_private(copy, raw, exclusive=True)
    if os.path.lexists(path):  # the previous record is kept beside, never deleted
        replaced = path.with_name(f'{path.name}.replaced-{datetime.fromtimestamp(clock(), timezone.utc):%Y%m%dT%H%M%SZ}')
        os.rename(path, replaced)
        actions.append({'action': 'kept_previous_record', 'path': str(replaced)})
    _write_private(path, (json.dumps(record, indent=2, sort_keys=True) + '\n').encode(), exclusive=False)
    verified, reason = bs.load_verified(home)
    if facts['bundle_id'] not in verified:
        raise ValueError(f'The record did not read back: {reason}')
    return {'format': FORMAT, 'state': 'recorded', 'record': record, 'actions': actions,
            'launcher': {'bundle_id': facts['bundle_id'], 'version': facts['version']}}


def main(argv=None, out=None, env=None, evidence_dir=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--app', default=APP, help='the Chromium-family app bundle (default %(default)s)')
    parser.add_argument('--execute', action='store_true', help='run the experiment; without it nothing happens')
    parser.add_argument('--stamp', type=_stamp_arg)
    parser.add_argument('--i-am-the-parent-with-user-approval', dest='approval', metavar='STAMP', type=_stamp_arg)
    parser.add_argument('--record', metavar='EVIDENCE', help='record a passing run for the App instead of running one')
    parser.add_argument('--home', help="Pebble's data folder for --record (default ~/Library/Application "
                                       'Support/com.pebble.local)')
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else EXIT_USAGE
    evidence_dir = evidence_dir or EVIDENCE
    stamp = args.stamp or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if args.execute and (args.stamp is None or args.approval != args.stamp):
        out.write(json.dumps({'format': FORMAT, 'state': 'refused', 'error': 'E-B1 needs --stamp and '
                              '--i-am-the-parent-with-user-approval naming it, given only after the person approved.'}) + '\n')
        return EXIT_REFUSED
    if args.record:
        if args.stamp is None:
            out.write(json.dumps({'format': FORMAT, 'state': 'refused', 'error': '--record needs --stamp.'}) + '\n')
            return EXIT_USAGE
        try:
            report = record_evidence(args.record, args.home or DATA_HOME, args.stamp, execute=args.execute)
        except (OSError, ValueError, bs.PolicyError) as exc:
            out.write(json.dumps({'format': FORMAT, 'state': 'refused', 'error': str(exc)[:300]}) + '\n')
            return EXIT_REFUSED
        out.write(json.dumps(report, indent=2) + '\n')
        return EXIT_OK
    if not args.execute:
        out.write(json.dumps(plan(args.app, evidence_dir, stamp), indent=2) + '\n')
        return EXIT_OK
    if (Path(evidence_dir) / f'eb1-{args.stamp}.json').exists():
        out.write(json.dumps({'format': FORMAT, 'state': 'refused', 'error': 'Evidence for this stamp exists; choose a '
                              'new stamp.'}) + '\n')
        return EXIT_REFUSED
    record = experiment(args.app, args.stamp, env or Env(front=default_front))
    path, digest = write_evidence(record, evidence_dir, args.stamp)
    out.write(json.dumps({'format': FORMAT, 'state': 'done', 'passed': record['passed'], 'evidence': str(path),
                          'evidence_sha256': digest, 'failed': [n for n in CRITERIA if not record['checks'][n].get('passed')],
                          'verified_browsers_proposal': proposal(record, path, digest)}, indent=2) + '\n')
    return EXIT_OK if record['passed'] else EXIT_FAILED


if __name__ == '__main__':
    sys.exit(main())
