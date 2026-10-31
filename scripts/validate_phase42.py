#!/usr/bin/env python3
"""Phase 42 service validator: 1.8.0, app control (D90, D100-D102; E-N2 and E-B1 passed on the person's Mac).

Pebble reads and operates one app window at a time through macOS Accessibility. In a conversation without a project,
with Screen control on, the model may ask for one window of a running app (ScreenRequest); only the person's review
grants it, to the run that asked, and the screen tools then follow that session. These checks drive the checked-in
App, runner, screen sessions and the AX backend in process with a scripted root model, a fake Accessibility reader (no
real window is ever read here) and a throwaway home folder, the same in source and frozen modes:
  - a requested window is granted only by the person's review, and nothing is granted before it;
  - the screen tools follow the granted window without UI words in the message, and not before the grant;
  - typing runs at once, a press waits for review, and every act reaches the reader with the element it observed;
  - the apps Pebble never operates are refused without a card;
  - the request is offered with Screen control on, with its sentence (Phase 45, D132: in project conversations too);
  - the macOS backend re-checks an element before acting and never acts on a secure field;
  - the Accessibility routes are the main window's, and macOS is asked only on the person's click;
  - the Space anchor is made on a desktop Space only (the shell's source, measured natively in phase42-dev);
  - Pebble's own browser reads pages in CSS pixels, and refuses a page whose scale it cannot measure;
  - the release version is 1.8.0 and its notes describe this without a saving claim.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import copy
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_runtime import ROOT  # noqa: E402
from validate_phase13 import bounded, owned_backend, sha  # noqa: E402
from validate_phase20 import write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402
import validate_phase34 as v34  # noqa: E402  (its scripted-model calls)
import validate_phase40 as v40  # noqa: E402  (its App factory and offer-recording scripted model)

for _path in (ROOT / 'backend', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'a_requested_window_is_granted_only_by_the_persons_review',
    'the_screen_tools_follow_the_granted_window_without_ui_words',
    'typing_runs_and_a_press_waits_for_review_with_the_element_rechecked',
    'never_operated_apps_are_refused_without_a_card',
    'the_request_is_offered_with_screen_control_in_every_conversation',
    'the_mac_backend_rechecks_elements_and_never_acts_on_secure_fields',
    'the_accessibility_routes_are_main_window_only',
    'the_space_anchor_is_made_on_a_desktop_space_only',
    'the_browser_reads_pages_in_css_pixels',
    'release_version_is_1_8_0_and_its_notes_describe_it',
    'backend_inputs_stable_during_campaign',
)
READS = ('backend/pebble/ax_worker.py', 'backend/pebble/screen_session.py', 'backend/pebble/runner.py',
         'backend/pebble/app.py', 'backend/pebble/tool_authz.py', 'backend/pebble/permissions.py',
         'backend/pebble/tool_catalogue.py', 'backend/pebble/browser_session.py', 'src-tauri/src/main.rs',
         'src/components/ScreenSessions.vue', 'src/components/ActionReview.vue', 'src/App.vue', 'CHANGELOG.md',
         'scripts/validate_phase34.py', 'scripts/validate_phase40.py', 'scripts/validate_phase42.py',
         'package.json', 'package-lock.json', 'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock',
         'src-tauri/tauri.conf.json')
BUNDLE, TEAM, TITLE = 'com.example.p42-notes', 'ABCDE12345', 'Draft — Notes'
APPS = {'notes': {'name': 'Notes', 'bundle_id': BUNDLE, 'team_id': TEAM},
        'terminal': {'name': 'Terminal', 'bundle_id': 'com.apple.Terminal', 'team_id': None},
        'keychain access': {'name': 'Keychain Access', 'bundle_id': 'com.apple.keychainaccess', 'team_id': None}}
NO_UI_WORDS = 'Fix the subject of my draft, then send it.'
WHY = 'to fix the subject of your draft and send it'


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def flat(text):
    return re.sub(r'\s+', ' ', text)


def changelog_entry(version):
    text = read('CHANGELOG.md')
    match = re.search(r'^## \[' + re.escape(version) + r'\][^\n]*\n(.*?)(?=^## \[|\Z)', text, re.M | re.S)
    assert match, 'CHANGELOG.md has no entry for ' + version
    return flat(match.group(1))


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


def function(source, name):
    start = source.index('fn ' + name + '(')
    return source[start:source.index('\n}\n', start) + 2]


def module(source, name):
    start = source.index('mod ' + name + ' {')
    return source[start:source.index('\n}\n', start) + 2]


class FakeReader:
    """The AX backend contract for one app with one window: a Subject field and a Send button (and, with secure, a
    password field). Records every perform with the expectation the host sent; a press on Send shows 'Sent'."""

    name, available, checks_expect = 'fake', True, True

    def __init__(self, secure=False):
        self.pid, self.start, self.calls = 4242, 1_700_000_000.25, []
        self.elements = [
            {'role': 'AXTextField', 'path': '/0', 'title': 'Subject', 'value': 'Draft', 'editable': True,
             'frame': [10, 10, 200, 20]},
            {'role': 'AXButton', 'path': '/1', 'title': 'Send', 'frame': [10, 40, 60, 20], 'enabled': True}]
        if secure:  # a sign-in field: the window is handed to the person (Phase 16), its value never read
            self.elements.append({'role': 'AXTextField', 'subrole': 'AXSecureTextField', 'path': '/2',
                                  'title': 'Password', 'value': 'hunter2-p42', 'frame': [10, 70, 200, 20]})

    def processes(self, bundle_id):
        return [{'pid': self.pid, 'start': self.start, 'bundle_id': BUNDLE, 'team_id': TEAM}] \
            if bundle_id == BUNDLE else []

    def _window(self):
        from pebble import ax_worker
        return {'window_ref': 'w0.' + ax_worker.sha256(TITLE)[:16], 'title': TITLE, 'focused': True,
                'frame': [0, 0, 800, 600], 'ax_identifier': None}

    def windows(self, pid, start):
        self.calls.append(('windows',))
        return [self._window()]

    def tree(self, pid, start, ref, limit):
        self.calls.append(('tree',))
        return {'window': self._window(), 'elements': copy.deepcopy(self.elements[:limit]), 'truncated': False}

    def perform(self, pid, start, ref, path, action, text, expect=None):
        self.calls.append(('perform', path, action, text, expect))
        element = next(e for e in self.elements if e['path'] == path)
        if action == 'type':
            element['value'] = text
        elif action == 'press' and path == '/1':
            self.elements.append({'role': 'AXStaticText', 'path': '/3', 'value': 'Sent', 'frame': [10, 100, 60, 20]})

    def performs(self):
        return [c for c in self.calls if c[0] == 'perform']


class Scripted(v40.Scripted):
    """The offer-recording scripted model; an entry may be a function of the request's messages."""

    def __enter__(self):
        super().__enter__()
        recorded = self.providers.complete

        def complete(provider, messages, tools, cancel, token, effort=False):
            index = len(self.requests)
            if index < len(self.script) and callable(self.script[index]):
                self.script[index] = self.script[index](messages)
            return recorded(provider, messages, tools, cancel, token, effort)
        self.providers.complete = complete
        return self


def tool_message(messages, ident):
    message = next(m for m in messages if m.get('role') == 'tool' and m.get('tool_call_id') == ident)
    return json.loads(message['content'])


def element_ref(view, label):
    return next(e['ref'] for e in view['elements'] if e.get('label') == label)


def flow(home, script, *, text=NO_UI_WORDS, screen=True, project=False, decide=None, reader=None):
    """One run: Screen control as given, the fake reader installed, the fake app list patched in; every pending
    action is decided by `decide(action)` (True approves, False rejects) as the person would, until the run settles."""
    from pebble import screen_session as ss
    from pebble.screen_session import AppAdapter, ScreenSessions
    reader = reader or FakeReader()
    app = v40.app_for(home / ('app-' + str(time.monotonic_ns())), context=32768)
    policy = app.store.setting('permissions', {})
    app.store.set_setting('permissions', {**policy, 'tools': {**policy['tools'], 'screen': screen}})
    app.screens = ScreenSessions(app.store, {'app': AppAdapter(reader, processes=reader.processes)})
    folder = v34.project(home / ('project-' + str(time.monotonic_ns()))) if project else None
    real = ss._running_app

    def running(name, lister=None, read_identity=None):
        entry = APPS.get(' '.join(name.split()).casefold())
        if entry is None:
            raise ss.ScreenRefused('not_running', 'No running app is called that.')
        return dict(entry)
    ss._running_app = running
    decided = []
    try:
        with Scripted() as scripted:
            scripted.script = list(script) + [v34.final('Done.')] * 3
            chat = app.new_chat(str(folder) if folder else None)
            app.work(chat, text, False, [])
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                runs = app.runs.list(chat['id'])
                if runs and app.active is None and runs[0]['state'] not in ('queued', 'running', 'retry_wait'):
                    pending = [a for a in app.store.actions(chat['id']) if a['status'] == 'pending']
                    if not pending or decide is None:
                        break
                    action = pending[0]
                    choice = decide(action)
                    decided.append({'kind': action['kind'], 'approved': choice,
                                    'sessions_before': len(app.screens.sessions(chat['id']))})
                    (app.approve if choice else app.reject)(chat['id'], action['id'])
                time.sleep(.02)
            rid = app.runs.list(chat['id'])[0]['id']
            return {'app': app, 'chat': chat, 'run': app.runs.get(rid), 'run_id': rid, 'reader': reader,
                    'requests': list(scripted.requests), 'tools': list(scripted.tools), 'decided': decided,
                    'actions': app.store.actions(chat['id'])}
    finally:
        ss._running_app = real


def request(abilities='press', app_name='Notes', ident='sr1'):
    return v34.call('ScreenRequest', {'app': app_name, 'why': WHY, 'abilities': abilities}, ident)


SESSION = re.compile(r'session_id\\?"\s*:\s*\\?"([0-9a-f]{32})')


def latest_session(messages):
    """The newest session_id the model has been given (the approval reaches it as a host input and a settled receipt)."""
    found = [s for m in messages for s in SESSION.findall(m.get('content') or '' if isinstance(m.get('content'), str)
                                                          else json.dumps(m.get('content')))]
    assert found, 'no session_id reached the model'
    return found[-1]


def observe(ident):
    return lambda messages: v34.call('ScreenObserve', {'session_id': latest_session(messages)}, ident)


def act(observed, label, action, ident, text=None, expect=None):
    def reply(messages):
        view = tool_message(messages, observed)
        args = {'session_id': latest_session(messages), 'observation_id': view['observation_id'],
                'element': element_ref(view, label), 'action': action}
        if text is not None:
            args['text'] = text
        if expect is not None:
            args['expect'] = expect
        return v34.call('ScreenAct', args, ident)
    return reply


def full_script():
    """Ask; hold while the person decides; observe; type; observe again; press (staged for review); hold."""
    return [request(), v34.final('I asked to use the window of your draft.'), observe('so1'),
            act('so1', 'Subject', 'type', 'sa1', text='Fixed subject'), observe('so2'),
            act('so2', 'Send', 'press', 'sa2', expect='Sent'), v34.final('Sending waits for your review.')]


class Campaign(v40.Campaign):
    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
        if self.only is not None and name not in self.only:
            return
        began = time.monotonic()
        try:
            evidence = fn() or {}
            self.results.append({'name': name, 'passed': True, 'evidence': evidence,
                                 'seconds': round(time.monotonic() - began, 2)})
        except Exception as exc:
            self.results.append({'name': name, 'passed': False, 'error': bounded(exc, 1500),
                                 'trace': traceback.format_exc(limit=3)[-1200:].replace(str(ROOT), '<checkout>')})

    def flow(self, *args, **kw):
        result = flow(self.home, *args, **kw)
        self.apps.append(result['app'])
        return result

    def run(self):
        scratch = self.scratch
        self.home = scratch / 'home'

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            self.home.mkdir()
            os.environ['HOME'] = str(self.home)
            return {'outside_checkout': True, 'home': 'scratch'}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        shared = {}

        def granted_on_review():
            result = shared['full'] = self.flow(full_script(), decide=lambda action: True)
            decided = result['decided']
            assert decided and decided[0]['kind'] == 'screen_grant' and decided[0]['sessions_before'] == 0, decided
            grant = next(a for a in result['actions'] if a['kind'] == 'screen_grant')
            assert grant['payload'].get('requires_review') is True and grant['payload']['bundle_id'] == BUNDLE
            assert grant['payload']['title'] == TITLE and grant['payload']['run_id'] == result['run_id']
            sessions = result['app'].screens.sessions(result['chat']['id'])
            assert len(sessions) == 1 and sessions[0]['kind'] == 'app', sessions
            refused = self.flow([request()], decide=lambda action: False)
            assert refused['decided'] and not refused['app'].screens.sessions(refused['chat']['id'])
            return {'card': 'screen_grant', 'granted_after_review': 1, 'granted_when_declined': 0}
        self.check('a_requested_window_is_granted_only_by_the_persons_review', granted_on_review)

        def follow():
            result = shared['full']
            first, waiting, later = result['tools'][0], result['tools'][1], result['tools'][2]
            assert 'ScreenRequest' in first and 'ScreenObserve' not in first, first
            assert 'ScreenObserve' not in waiting, 'screen tools before the person decided'
            assert {'ScreenObserve', 'ScreenAct'} <= set(later), later
            from pebble.screen_session import UI_INTENT
            assert not UI_INTENT.search(NO_UI_WORDS), 'the message must carry no UI words'
            return {'before_grant': 'ScreenRequest only', 'after_grant': 'ScreenObserve, ScreenAct'}
        self.check('the_screen_tools_follow_the_granted_window_without_ui_words', follow)

        def acts():
            result = shared['full']
            performs = result['reader'].performs()
            assert [(p[1], p[2], p[3]) for p in performs] == [('/0', 'type', 'Fixed subject'), ('/1', 'press', None)], \
                performs
            assert all(p[4] and p[4]['role'] in ('AXTextField', 'AXButton') and re.fullmatch(r'[0-9a-f]{64}',
                                                                                             p[4]['label_sha256'])
                       for p in performs), 'an act reached the reader without the element it observed'
            kinds = [d['kind'] for d in result['decided']]
            assert kinds == ['screen_grant', 'screen'], kinds
            press = next(a for a in result['actions'] if a['kind'] == 'screen')
            assert press['payload']['review']['element']['label'] == 'Send' and press['status'] == 'applied'
            secure = self.flow([request(), v34.final('Asked.'), observe('so1')], decide=lambda action: True,
                               reader=FakeReader(secure=True))
            view = tool_message(secure['requests'][-1], 'so1')
            assert view['state'] == 'handoff' and 'secure_field' in view['reasons'], view
            assert 'hunter2-p42' not in json.dumps(secure['requests']) and not secure['reader'].performs()
            return {'typed': 'at once (input)', 'pressed': 'after review (commit)', 'expectation_sent': True,
                    'secure_field_window': 'handed to the person, no value read'}
        self.check('typing_runs_and_a_press_waits_for_review_with_the_element_rechecked', acts)

        def never():
            for name in ('Terminal', 'Keychain Access'):
                result = self.flow([request(app_name=name)], decide=lambda action: True)
                answer = tool_message(result['requests'][1], 'sr1')
                assert 'not operate' in json.dumps(answer), answer
                assert not [a for a in result['actions'] if a['kind'] == 'screen_grant'], 'a card was made'
                assert not result['reader'].calls, 'a window was read'
            return {'refused': ['Terminal', 'Keychain Access'], 'cards': 0}
        self.check('never_operated_apps_are_refused_without_a_card', never)

        def offered():
            on = shared['full']
            system = on['requests'][0][0]['content']
            assert 'use ScreenRequest with the app' in system, 'the request sentence is missing'
            off = self.flow([], screen=False)
            assert 'ScreenRequest' not in off['tools'][0] and 'ScreenRequest' not in off['requests'][0][0]['content']
            project = self.flow([], project=True)
            assert 'ScreenRequest' in project['tools'][0], project['tools'][0]  # Phase 45 (D132, the person)
            return {'companion_screen_on': True, 'screen_off': False, 'project': True}
        self.check('the_request_is_offered_with_screen_control_in_every_conversation', offered)

        def backend():
            from pebble import ax_worker
            from pebble.ax_worker import AXError, MacBackend

            class Node:
                def __init__(self, role, title=None, kids=(), subrole=None):
                    self.facts = {'AXRole': role, **({'AXTitle': title} if title else {}),
                                  **({'AXSubrole': subrole} if subrole else {})}
                    self.kids = list(kids)

            class Layer:
                def __init__(self, window):
                    self.window, self.done = window, []

                def trusted(self):
                    return True

                def open(self, pid):
                    return 'app'

                def close(self):
                    pass

                def attributes(self, element, names):
                    if element == 'app':
                        return {'AXWindows': [self.window]}
                    facts = {n: element.facts[n] for n in names if n in element.facts}
                    if 'AXChildren' in names and element.kids:
                        facts['AXChildren'] = element.kids
                    return facts

                def value(self, element):
                    return None, False

                def settable(self, element, name):
                    return True

                def act(self, element, action):
                    self.done.append(action)
                    return 0

                def set(self, element, name, value):
                    self.done.append(name)
                    return 0
            send, secret = Node('AXButton', 'Send'), Node('AXTextField', 'Password', subrole='AXSecureTextField')
            layer = Layer(Node('AXWindow', TITLE, [send, secret]))
            mac = MacBackend(layer, start_of=lambda pid: 1.0)
            ref = mac.windows(7, 1.0)[0]['window_ref']
            wrong = {'role': 'AXButton', 'label_sha256': '0' * 64}
            for path, action, expect, code in (('/0', 'press', wrong, 'not_found'), ('/1', 'type', None, 'unsupported')):
                try:
                    mac.perform(7, 1.0, ref, path, action, 'x', expect=expect)
                except AXError as exc:
                    assert exc.code == code, (path, exc.code)
                else:
                    raise AssertionError(f'{action} on {path} was performed')
            assert layer.done == [], layer.done
            right = {'role': 'AXButton', 'label_sha256': ax_worker.label_sha256(
                ax_worker.sanitize_element({'role': 'AXButton', 'path': '/0', 'title': 'Send'}))}
            mac.perform(7, 1.0, ref, '/0', 'press', None, expect=right)
            assert layer.done == ['AXPress'], layer.done
            return {'changed_element': 'not_found', 'secure_field': 'unsupported', 'matching_press': 'performed'}
        self.check('the_mac_backend_rechecks_elements_and_never_acts_on_secure_fields', backend)

        def routes():
            from pebble import tool_authz
            shell = read('src-tauri/src/main.rs')
            allowed = re.search(r'const ALLOWED: &\[&str\] = &\[(.*?)\];', shell, re.S).group(1)
            pet = re.search(r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains', shell, re.S).group(1)
            for name in ('app_control_status', 'app_control_request'):
                assert f'"{name}"' in allowed and f'"{name}"' not in pet and name in tool_authz.NATIVE_ONLY, name
            panel = read('src/components/ScreenSessions.vue')
            asks = re.findall(r"rpc<unknown>\('app_control_request'\)", panel)
            assert len(asks) == 1 and '@click="askAccess"' in panel, 'macOS is asked other than on a click'
            assert 'diag_ax_probe' not in allowed and 'diag_ax_probe' in tool_authz.NATIVE_ONLY
            return {'main_window_only': 2, 'asked_on_click': True}
        self.check('the_accessibility_routes_are_main_window_only', routes)

        def anchor():
            shell = read('src-tauri/src/main.rs')
            space = module(shell, 'space_anchor')
            setup = shell[shell.index('configure_pet(app.handle())?;'):]
            assert re.search(r'\n\s*space_anchor::place\(app\.handle\(\), "launch"\);', setup[:1500])
            assert 'space_anchor::place(&handle, "space_changed");' in function(shell, 'companion_follows_spaces')
            for needle in ('menuBarOwningApplication', 'AXFullScreen', 'AXUIElementSetMessagingTimeout(app, 0.25)',
                           'if full != Some(true) { create(); }', 'run_on_main_thread', 'pebble-space-anchor-place'):
                assert needle in space, needle
            assert 'fn create()' in space and 'pub fn create()' not in space, 'only place() makes the anchor'
            return {'launch': 'place', 'space_change': 'place', 'full_screen_space': 'waits for a desktop'}
        self.check('the_space_anchor_is_made_on_a_desktop_space_only', anchor)

        def pixels():
            from pebble import browser_session as bs
            from pebble.screen_session import ScreenRefused

            class Tab:
                session = 's'

            def adapter(quads):
                instance = bs.BrowserAdapter.__new__(bs.BrowserAdapter)
                instance._call = lambda method, params=None, session=None, effect=False: {'quads': quads}
                return instance
            doc = bs._Doc('f', 'https://example.com/', 'https://example.com/', (0.0, 60.0))
            html = bs._Node('HTML', {}, -1, 3)
            html.bounds = [0.0, 60.0, 2400.0, 1432.0]
            doc.by_index.append(html)
            scale = adapter([[0, 0, 1200, 0, 1200, 716, 0, 716]])._scale(Tab(), doc)
            assert scale == 2.0, scale
            doc.rescale(scale)
            assert html.bounds == [0.0, 30.0, 1200.0, 716.0] and doc.scroll == (0.0, 30.0), (html.bounds, doc.scroll)
            try:
                adapter([])._scale(Tab(), doc)
            except ScreenRefused as exc:
                assert exc.code == 'ax_failed' and 'scale' in str(exc)
            else:
                raise AssertionError('a page with no measurable scale was read')
            return {'retina_scale': 2.0, 'unmeasurable': 'ax_failed'}
        self.check('the_browser_reads_pages_in_css_pixels', pixels)

        def version():
            current = checkout_version()
            assert tuple(int(x) for x in current.split('.')) >= (1, 8, 0), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = changelog_entry('1.8.0')
            for needle in ('Accessibility', 'one window', 'allow it in Pebble', 'waits for your review', 'never retried',
                           'password, code or card field', 'never operates', 'Retina', 'Stealth', 'Known limitation'):
                assert needle in entry, ('the 1.8.0 notes lack', needle)
            for claim in ('faster', 'cheaper', 'undetectable', '% fewer', 'saves '):
                assert claim not in entry.lower(), ('the 1.8.0 notes claim', claim)
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_1_8_0_and_its_notes_describe_it', version)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary folder even on success')
    args = parser.parse_args(argv)
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; choose a new path (earlier raw evidence is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    backend_sha = sha(backend.read_bytes()) if backend else None
    before = digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase42-', dir='/private/tmp')).resolve()
    real_home = os.environ.get('HOME')
    started = time.monotonic()
    campaign = Campaign(scratch, backend)
    fatal = None
    try:
        campaign.run()
    except Exception as exc:
        fatal = bounded(exc, 1500)
    finally:
        campaign.close()
        if real_home is not None:
            os.environ['HOME'] = real_home
    after_digests = digests()
    stable = before == after_digests and (not backend or sha(backend.read_bytes()) == backend_sha)
    campaign.results.append({'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                             'evidence': {'files_compared': len(before), 'executable_compared': bool(backend)}})
    recorded = {r['name'] for r in campaign.results}
    failed = [r['name'] for r in campaign.results if not r['passed']] + [n for n in PLANNED if n not in recorded]
    passed = fatal is None and not failed and recorded == set(PLANNED)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source', 'passed': passed,
        'checks': [r['name'] for r in campaign.results if r['passed']], 'failed': failed, 'planned_checks': list(PLANNED),
        'check_results': campaign.results, 'fatal_error': fatal,
        'duration_seconds': round(time.monotonic() - started, 1),
        'backend_sha256': backend_sha, 'source_sha256': before,
        'source_note': 'Digests of the tested sources this validator reads; the campaign binds them to its snapshot.',
    }
    if passed and not args.keep_scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    else:
        report['scratch_preserved'] = str(scratch)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_new(report_path, json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']), 'failed': failed,
                      'fatal_error': fatal}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
