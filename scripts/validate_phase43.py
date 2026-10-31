#!/usr/bin/env python3
"""Phase 43 service validator: 1.9.0, browser control and computer use (D105-D112; design .runtime/phase43-plan.md).

M1, routes, windows and contracts. These checks drive the checked-in App, runner, screen sessions and the AX worker's
protocol in process with a scripted root model, a fake protocol-2 Accessibility reader (no real window is ever read
here) and a throwaway home folder, the same in source and frozen modes:
  - a window is named by its window-server number, so a title change after the card no longer loses it;
  - a minimized window, or one on another desktop, comes forward only after one review per session;
  - a Chromium-family app's page accessibility is turned on once per app instance and set back at the last end;
  - observations carry the versioned envelope and acts their typed results;
  - a HOST CAPABILITIES line states host facts beside the screen tools;
  - the worker speaks protocol 2, and PEBBLE_WINDOW_NUMBERS=0 restores 1.8.0's rows exactly.
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
import validate_phase42 as v42  # noqa: E402  (its flow helpers)

for _path in (ROOT / 'backend', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'a_window_is_named_by_its_number_and_survives_a_title_change',
    'a_minimized_or_off_desktop_window_comes_forward_after_one_review',
    'a_chromium_app_is_enhanced_once_and_restored_at_the_last_end',
    'observations_carry_the_envelope_and_acts_their_typed_results',
    'the_capability_line_states_host_facts_beside_the_screen_tools',
    'the_worker_speaks_protocol_2_and_its_switch_restores_1_8_0',
    'a_site_search_opens_the_sites_own_results_and_asks_unless_the_words_are_the_persons',
    'app_verbs_read_at_once_taint_the_run_and_a_shortcut_waits_for_review',
    'the_scripting_verbs_carry_arguments_in_argv_and_never_shell_ui_or_javascript',
    'an_app_opened_in_the_background_is_put_back_only_when_the_jump_was_pebbles',
    'a_browser_request_is_granted_only_by_review_and_opens_pebbles_own_tab',
    'a_read_site_is_observed_and_never_acted_on',
    'connect_a_browser_writes_only_the_ticked_browsers',
    'host_native_host_and_extension_speak_pebble_browser_2',
    'release_version_is_1_9_0_and_its_notes_describe_it',
    'backend_inputs_stable_during_campaign',
)
SHOP, DOCS = 'https://shop.example', 'https://docs.example'
READS = ('backend/pebble/ax_worker.py', 'backend/pebble/screen_session.py', 'backend/pebble/runner.py',
         'backend/pebble/app.py', 'backend/pebble/tool_authz.py', 'backend/pebble/permissions.py',
         'backend/pebble/tool_catalogue.py', 'backend/pebble/open_url.py', 'backend/pebble/site_search.py',
         'backend/pebble/app_actions.py', 'backend/pebble/native_apps.py', 'backend/pebble/companion_tools.py',
         'backend/pebble/browser_bridge.py', 'backend/pebble/native_host.py', 'backend/pebble/native_host_setup.py',
         'browser-extension/lib/policy.mjs', 'browser-extension/manifest.json', 'CHANGELOG.md',
         'src/components/ActionReview.vue', 'src/App.vue', 'src/types.ts',
         'scripts/validate_phase34.py', 'scripts/validate_phase40.py', 'scripts/validate_phase42.py',
         'scripts/validate_phase43.py', 'package.json', 'package-lock.json', 'src-tauri/Cargo.toml',
         'src-tauri/Cargo.lock', 'src-tauri/tauri.conf.json')
BUNDLE, TEAM, TITLE = v42.BUNDLE, v42.TEAM, v42.TITLE
NUMBER = 31337
RETITLED = 'Draft — Notes (edited by the app)'


def first_seen(result, ident):
    """Phase 45 (F191, 1.9.13): a tool message as the first model request after it carried it (later requests carry a
    superseded observation as a one-line stub)."""
    for messages in result['requests']:
        if any(m.get('role') == 'tool' and m.get('tool_call_id') == ident for m in messages):
            return v42.tool_message(messages, ident)
    raise AssertionError(f'no request carried {ident}')


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


class FakeReader43(v42.FakeReader):
    """validate_phase42's reader on protocol 2: its one window is named by number and can be retitled, minimized or
    moved to another desktop; it serves hello, enhance, restore and raise, and records each."""

    name, window_numbers = 'fake43', True

    def __init__(self, *, path=None, minimized=False, listed=True, retitle=False):
        super().__init__()
        self.pid, self.start = 4343, 1_700_000_000.5
        self.path, self.minimized, self.listed, self.retitle, self.title = path, minimized, listed, retitle, TITLE

    def hello(self):
        return {'protocol': 'pebble-ax/2', 'backend': self.name, 'available': True, 'window_numbers': True}

    def processes(self, bundle_id):
        return [{**row, 'pid': self.pid, 'start': self.start, 'path': self.path} for row in super().processes(bundle_id)]

    def _window(self):
        return {'window_ref': f'n{NUMBER}', 'title': self.title, 'focused': True, 'frame': [0, 0, 800, 600],
                'ax_identifier': None, 'window_number': NUMBER, 'listed': self.listed, 'main': True,
                'minimized': self.minimized, 'subrole': 'AXStandardWindow'}

    def tree(self, pid, start, ref, limit):
        if self.retitle:
            self.title = RETITLED  # a browser retitles itself between the card and the first look
        return super().tree(pid, start, ref, limit)

    def enhance(self, pid, start):
        self.calls.append(('enhance',))
        return {'original': {'AXManualAccessibility': False, 'AXEnhancedUserInterface': None},
                'changed': ['AXManualAccessibility'], 'refused': [], 'filled': True, 'ms': 140}

    def restore(self, pid, start, changed):
        self.calls.append(('restore', dict(changed)))
        return {'restored': [k for k, v in changed.items() if v is not None], 'left': []}

    def raise_window(self, pid, start, ref):
        self.calls.append(('raise', ref))
        self.minimized, self.listed = False, True
        return {'raised': True, 'frontmost': True}

    def named(self, kind):
        return [c for c in self.calls if c[0] == kind]


class FakeBridge:
    """The bridge surface a BrowserRequest uses, recording each call: a connected extension in Chrome, one site grant,
    one of Pebble's own tabs at the first address. (Phase 44, D117/D121: Comet refuses the extension's debugger, so a
    request there takes the window route; Pebble's own tab is Chrome's.)"""

    def __init__(self):
        self.calls, self.grant, self.tab = [], None, None

    def status(self):
        return {'extension': {'connected': True, 'browser': 'chrome'}}

    def grant_sites(self, run_id, act, read, ttl):
        self.calls.append(('grant', list(act), list(read)))
        self.grant = {'grant_id': 'a' * 32, 'act': list(act), 'read': list(dict.fromkeys([*act, *read]))}
        return dict(self.grant)

    def open_tab(self, grant_id, url, **kw):
        self.calls.append(('open', url))
        self.tab = 'owned-7'
        return {'tab_id': self.tab, 'origin': SHOP}

    def access(self, tab_id):
        return {'tab_id': tab_id, 'owned': True, 'share_id': 'owned-' + self.grant['grant_id'],
                'act': self.grant['act'], 'read': self.grant['read']} if tab_id == self.tab else None

    def end_site_grant(self, grant_id):
        self.calls.append(('end', grant_id))
        return True

    def ended(self, session_id):
        self.calls.append(('ended', session_id))


class OwnedTab:
    """A browser adapter for one of Pebble's own tabs: a shop page with a basket button, then (moved) a docs page."""

    kind = 'browser'

    def __init__(self, origin=SHOP):
        self.origin, self.performed = origin, []

    def pin(self, target):
        return {'tab_id': target['tab_id']}

    def snapshot(self, target, pin, limit):
        window = {'tab_id': target['tab_id'], 'origin': self.origin, 'url': self.origin + '/item/1', 'title': 'Item',
                  'navigation_id': 'n1', 'loader_id': 'L1', 'focused': True, 'frame': [0, 0, 800, 600]}
        return {'window': window, 'truncated': False, 'elements': [
            {'role': 'AXStaticText', 'path': 'p/name', 'value': 'A kettle', 'frame': [10, 10, 200, 20]},
            {'role': 'AXButton', 'path': 'p/add', 'title': 'Add to basket', 'enabled': True,
             'frame': [10, 40, 120, 24]}]}

    def perform(self, target, pin, element, action, text):
        self.performed.append((element['path'], action))


def browser_flow(home, script, bridge, tab):
    """validate_phase42's flow with Pebble for Chrome connected (the fake bridge) and Pebble's own tab adapter."""
    from pebble.screen_session import AppAdapter, ScreenSessions
    app = v40.app_for(home / ('app-' + str(time.monotonic_ns())), context=32768)
    policy = app.store.setting('permissions', {})
    app.store.set_setting('permissions', {**policy, 'tools': {**policy['tools'], 'screen': True}})
    reader = FakeReader43()
    app.screens = ScreenSessions(app.store, {'app': AppAdapter(reader, processes=reader.processes), 'browser': tab})
    app.browser_bridge = bridge
    decided = []
    with v42.Scripted() as scripted:
        scripted.script = list(script) + [v34.final('Done.')] * 3
        chat = app.new_chat(None)
        app.work(chat, 'Add the kettle on shop.example to my basket.', False, [])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            runs = app.runs.list(chat['id'])
            if runs and app.active is None and runs[0]['state'] not in ('queued', 'running', 'retry_wait'):
                pending = [a for a in app.store.actions(chat['id']) if a['status'] == 'pending']
                if not pending:
                    break
                decided.append({'kind': pending[0]['kind'], 'calls_before': len(bridge.calls)})
                app.approve(chat['id'], pending[0]['id'])
            time.sleep(.02)
        rid = app.runs.list(chat['id'])[0]['id']
        return {'app': app, 'chat': chat, 'run_id': rid, 'requests': list(scripted.requests),
                'tools': list(scripted.tools), 'decided': decided, 'actions': app.store.actions(chat['id'])}


def chromium_bundle(folder):
    path = folder / 'Comet.app'
    (path / 'Contents/Frameworks/Comet Framework.framework/Versions/155.0/Helpers/Comet Helper (Renderer).app').mkdir(
        parents=True)
    return str(path)


def ask():
    return v42.request(abilities='press')


def observe(ident, before=None):
    """ScreenObserve on the newest session; `before` runs first (the window changes while the model plans)."""
    def reply(messages):
        if before:
            before()
        return v42.observe(ident)(messages)
    return reply


def flow(home, script, reader, **kw):
    """validate_phase42's flow with this reader in place of its own."""
    return v42.flow(home, script, reader=reader, decide=lambda action: True, **kw)


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

    def flow(self, script, reader, **kw):
        result = flow(self.home, script, reader, **kw)
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

        def numbered():
            reader = FakeReader43(retitle=True)
            script = [ask(), v34.final('I asked to use the window of your draft.'), observe('so1'),
                      v42.act('so1', 'Subject', 'type', 'sa1', text='Fixed subject'), observe('so2'),
                      v42.act('so2', 'Send', 'press', 'sa2', expect='Sent'), v34.final('Sending waits for review.')]
            result = shared['numbered'] = self.flow(script, reader)
            grant = next(a for a in result['actions'] if a['kind'] == 'screen_grant')
            assert grant['payload']['window'].get('window_number') == NUMBER, grant['payload']['window']
            assert grant['payload']['title'] == TITLE, 'the card shows the title as it was'
            # Phase 45 (F191, 1.9.13): a newer observation turns the one before into a stub in later requests, so each
            # observation is read from the first request that carried it (as the model saw it when it was the newest)
            view = first_seen(result, 'so1')
            assert view.get('observation_id') and view['window']['title'] == RETITLED, view
            performs = reader.performs()
            assert [(p[1], p[2]) for p in performs] == [('/0', 'type'), ('/1', 'press')], performs
            sessions = result['app'].screens.sessions(result['chat']['id'])
            assert sessions[0]['target']['window']['window_number'] == NUMBER, sessions
            return {'card_title': 'as shown', 'title_after_card': 'changed', 'observed': True, 'acts': len(performs)}
        self.check('a_window_is_named_by_its_number_and_survives_a_title_change', numbered)

        def forward():
            reader = FakeReader43(minimized=True)
            again = lambda: setattr(reader, 'minimized', True)
            script = [ask(), v34.final('Asked.'), observe('so1'), v34.final('I asked to bring it forward.'),
                      observe('so2'), observe('so3', before=again), v34.final('Done.')]
            result = self.flow(script, reader)
            kinds = [d['kind'] for d in result['decided']]
            assert kinds == ['screen_grant', 'screen_raise'], kinds
            card = next(a for a in result['actions'] if a['kind'] == 'screen_raise')
            assert card['payload']['requires_review'] is True and card['payload']['reason'] == 'minimized', card
            first = v42.tool_message(result['requests'][3], 'so1')
            assert (first.get('status'), first.get('kind')) == ('awaiting_user_approval', 'screen_raise'), first
            assert [c[1] for c in reader.named('raise')] == [f'n{NUMBER}', f'n{NUMBER}'], reader.calls
            last = v42.tool_message(result['requests'][-1], 'so3')
            assert last.get('observation_id'), 'approved once: the next minimize reads without a card'
            review, shell, kinds_ts = read('src/components/ActionReview.vue'), read('src/App.vue'), read('src/types.ts')
            assert "kind === 'screen_raise'" in review and 'macOS may switch to its desktop' in review
            assert '"screen_raise"' in kinds_ts and 'Bring forward' in shell
            return {'cards': kinds, 'raises': 2, 'second_minimize': 'raised without a card'}
        self.check('a_minimized_or_off_desktop_window_comes_forward_after_one_review', forward)

        def enhanced():
            reader = FakeReader43(path=chromium_bundle(scratch))
            script = [ask(), v34.final('Asked.'), observe('so1'), observe('so2'), v34.final('Read it.')]
            result = self.flow(script, reader)
            assert len(reader.named('enhance')) == 1, reader.calls
            assert not reader.named('restore'), 'restored while the session lives'
            session = result['app'].screens.sessions(result['chat']['id'])[0]['session_id']
            result['app'].screens.revoke(session)
            assert reader.named('restore') == [('restore', {'AXManualAccessibility': False})], reader.calls
            plain = FakeReader43(path=str(scratch / 'missing.app'))
            self.flow([ask(), v34.final('Asked.'), observe('so1'), v34.final('Read it.')], plain)
            assert not plain.named('enhance'), 'an app without a Chromium helper was switched'
            return {'enhanced': 1, 'restored_at_end': {'AXManualAccessibility': False}, 'other_apps': 0}
        self.check('a_chromium_app_is_enhanced_once_and_restored_at_the_last_end', enhanced)

        def envelope():
            result = shared['numbered']
            first = first_seen(result, 'so1')  # Phase 45 (F191): as the model saw it when it was the newest
            second = first_seen(result, 'so2')
            for key, wanted in (('version', 2), ('environment', 'app'), ('source', 'native_ax'), ('status', 'ready')):
                assert first.get(key) == wanted, (key, first.get(key))
            assert first['coverage'] == {'shown': len(first['elements']), 'omitted': first['omitted'],
                                         'truncated': False}, first['coverage']
            # Phase 45 (D140, 1.9.14): the act returns the window after it as the session's next observation, so the
            # chain runs so1 -> the act's observation -> so2, each with the same envelope and target incarnation
            typed = first_seen(result, 'sa1')
            assert (typed['dispatch'], typed['outcome'], typed['next']) == ('sent', 'confirmed', 'act'), typed
            seen = typed['observation']
            for key, wanted in (('version', 2), ('environment', 'app'), ('source', 'native_ax'), ('status', 'ready')):
                assert seen.get(key) == wanted, (key, seen.get(key))
            assert seen['changed_from'] == first['observation_id'] and second['changed_from'] == seen['observation_id']
            assert len(first['target_incarnation']) == 16
            assert first['target_incarnation'] == seen['target_incarnation'] == second['target_incarnation']
            pressed = v42.tool_message(result['requests'][-1], 'sa2')  # a commit reaches the model as its staged card
            assert (pressed.get('status'), pressed.get('kind')) == ('awaiting_user_approval', 'screen'), pressed
            return {'version': 2, 'typed_result': [typed['dispatch'], typed['outcome'], typed['next']],
                    'act_observation': True}
        self.check('observations_carry_the_envelope_and_acts_their_typed_results', envelope)

        def capability_line():
            from pebble.screen_session import CAPABILITIES_HEAD, CAPABILITIES_MESSAGE
            result = shared['numbered']
            lines = [m['content'] for request in result['requests'] for m in request
                     if m.get('name') == CAPABILITIES_MESSAGE]
            assert lines, 'no HOST CAPABILITIES line reached the model'
            facts = json.loads(lines[-1][len(CAPABILITIES_HEAD):lines[-1].rindex('}') + 1])
            assert facts['native'] == {'ax': True, 'window_numbers': True, 'capture': False,
                                       'foreground_input': False}, facts['native']
            assert facts['scope']['run_id'] == result['run_id'] and 'never work around' in lines[-1]
            assert facts['browser']['owned_tabs'] is False, 'owned tabs before M2'
            off = self.flow([], FakeReader43(), screen=False)
            assert not any(m.get('name') == CAPABILITIES_MESSAGE for r in off['requests'] for m in r), \
                'the line without any screen tool'
            return {'line': 'beside the screen tools', 'native': facts['native'], 'screen_off': 'absent'}
        self.check('the_capability_line_states_host_facts_beside_the_screen_tools', capability_line)

        def protocol():
            from pebble import ax_worker
            from pebble.ax_worker import MacBackend, handle

            class Layer:
                numbers = True

                def __init__(self):
                    self.window = {'title': TITLE}

                def trusted(self):
                    return True

                def open(self, pid):
                    return 'app'

                def close(self):
                    pass

                def same(self, one, other):
                    return one is other

                def window_number(self, element):
                    return NUMBER

                def attributes(self, element, names):
                    if element == 'app':
                        return {'AXWindows': [self.window]} if 'AXWindows' in names else {}
                    return {'AXTitle': TITLE} if 'AXTitle' in names else {}
            mac = MacBackend(Layer(), start_of=lambda pid: 1.0)
            hello = handle({'id': 1, 'op': 'hello', 'args': {}}, mac)['result']
            assert hello['protocol'] == 'pebble-ax/2' and hello['window_numbers'] is True
            assert set(hello['ops']) >= {'enhance', 'restore', 'raise'}, hello
            rows = handle({'id': 2, 'op': 'windows', 'args': {'pid': 7, 'start': 1.0}}, mac)['result']
            assert rows[0]['window_ref'] == f'n{NUMBER}' and rows[0]['window_number'] == NUMBER, rows
            saved = os.environ.get('PEBBLE_WINDOW_NUMBERS')
            os.environ['PEBBLE_WINDOW_NUMBERS'] = '0'
            try:
                old = handle({'id': 3, 'op': 'windows', 'args': {'pid': 7, 'start': 1.0}}, mac)['result']
            finally:
                if saved is None:
                    os.environ.pop('PEBBLE_WINDOW_NUMBERS', None)
                else:
                    os.environ['PEBBLE_WINDOW_NUMBERS'] = saved
            assert old[0]['window_ref'] == 'w0.' + ax_worker.sha256(TITLE)[:16], old
            assert 'window_number' not in old[0], '1.8.0 rows carry no protocol-2 facts'
            return {'protocol': hello['protocol'], 'ref': rows[0]['window_ref'], 'switched_off': old[0]['window_ref']}
        self.check('the_worker_speaks_protocol_2_and_its_switch_restores_1_8_0', protocol)

        def site_search():
            from pebble import open_url
            opened, real = [], open_url.open_url
            open_url.open_url = lambda payload, **kw: real(payload, opener=lambda argv: opened.append(argv) or 0, **kw)
            try:
                own = self.flow([v34.call('OpenURL', {'site': 'YouTube', 'query': 'lofi beats'}, 'ou1'),
                                 v34.final('Opened.')], FakeReader43(), text='Play lofi beats on YouTube')
                other = self.flow([v34.call('OpenURL', {'site': 'YouTube', 'query': 'cheap flights'}, 'ou1'),
                                   v34.final('Opened.')], FakeReader43(), text='Play lofi beats on YouTube')
            finally:
                open_url.open_url = real
            mine = next(a for a in own['actions'] if a['kind'] == 'url')['payload']
            theirs = next(a for a in other['actions'] if a['kind'] == 'url')['payload']
            assert mine['url'] == 'https://www.youtube.com/results?search_query=lofi+beats', mine['url']
            assert (mine['route'], mine['site'], mine['requires_review']) == ('table', 'youtube.com', False), mine
            assert theirs['requires_review'] is True, 'words that are not the person\'s own opened without asking'
            assert [a[-1] for a in opened] == [mine['url'], theirs['url']], opened
            return {'route': 'table', 'own_words': 'opened by the mode', 'other_words': 'asked first'}
        self.check('a_site_search_opens_the_sites_own_results_and_asks_unless_the_words_are_the_persons', site_search)

        def verbs():
            from pebble import app_actions
            from pebble.companion_tools import state as companion_state
            ran, real = [], app_actions._osascript
            found = ('1' + app_actions.RS + 'x-coredata://AB12-CD34/ICNote/p7' + app_actions.US + 'Groceries p43'
                     + app_actions.US + '2026-10-25T21:00:00' + app_actions.RS)

            def fake(script, argv, limit):
                ran.append((script, list(argv)))
                return 0, found if script is app_actions.NOTES_SEARCH else '', ''
            app_actions._osascript = fake
            try:
                result = self.flow([v34.call('AppAction', {'action': 'notes.search', 'query': 'Groceries'}, 'aa1'),
                                    v34.call('AppAction', {'action': 'shortcuts.run', 'name': 'Morning'}, 'aa2'),
                                    v34.final('Done.')], FakeReader43(),
                                   text='Find my groceries note, then run my Morning shortcut')
            finally:
                app_actions._osascript = real
            read = v42.tool_message(result['requests'][-1], 'aa1')
            assert read.get('status') == 'done' and 'Groceries p43' in json.dumps(read), read
            assert ran[0] == (app_actions.NOTES_SEARCH, ['Groceries']), 'the read ran first, with its words in argv'
            card = next(a for a in result['actions'] if a['kind'] == 'app_action')
            assert card['payload']['class'] == 'shortcut_run' and card['payload']['requires_review'] is True, card
            assert [d['kind'] for d in result['decided']] == ['app_action'], result['decided']
            assert [r[0] for r in ran] == [app_actions.NOTES_SEARCH, app_actions.SHORTCUTS_RUN], 'ran before review'
            assert companion_state(result['app']).leaks(result['run_id'], 'Groceries p43'), 'the private read left no taint'
            return {'read': 'at once', 'taint': True, 'shortcut': 'after review'}
        self.check('app_verbs_read_at_once_taint_the_run_and_a_shortcut_waits_for_review', verbs)

        def argv_only():
            from pebble import app_actions
            scripts = {verb: entry[4] for verb, entry in app_actions.VERBS.items()}  # (app, class, needs, may, script, limit)
            assert len(scripts) == 18 and all(s.startswith('on run argv') for s in scripts.values()), sorted(scripts)
            assert not [v for v, s in scripts.items() if app_actions.FORBIDDEN.search(s)], 'a forbidden word'
            hostile = 'x" & (do shell script "touch /tmp/p43") & "'
            ran, real = [], app_actions._osascript
            app_actions._osascript = lambda script, argv, limit: (ran.append((script, list(argv))) or (0, '0', ''))
            try:
                app_actions.run(app_actions.proposal({'action': 'notes.search', 'query': hostile}))
            finally:
                app_actions._osascript = real
            assert ran == [(app_actions.NOTES_SEARCH, [hostile])], 'the words reached the script other than as argv'
            return {'verbs': len(scripts), 'forbidden_words': 0, 'hostile_query': 'argv only'}
        self.check('the_scripting_verbs_carry_arguments_in_argv_and_never_shell_ui_or_javascript', argv_only)

        def background():
            from pebble import native_apps
            assert native_apps.launch_argv({'id': '/Applications/Music.app'}, background=True)[:2] == ['/usr/bin/open',
                                                                                                    '-g']
            before = {'bundle_id': 'com.example.front', 'path': '/Applications/Front.app'}
            launched = {'bundle_id': 'com.apple.Music', 'path': '/System/Applications/Music.app'}

            def watch(idle_value):
                clock, fronts, opened = [100.0], [launched], []

                def front():
                    return fronts[-1]

                def opener(argv):
                    opened.append(argv)
                    fronts.append(before)
                    return 0
                # launched one second before the first look: input within that second is the person's
                state = native_apps.watch_focus(launched['path'], before, 99.0, opener=opener, front=front,
                                                idle=lambda: idle_value(clock[0]), clock=lambda: clock[0],
                                                sleep=lambda s: clock.__setitem__(0, clock[0] + s))
                return state, opened
            quiet, opened = watch(lambda now: now - 98.0)
            assert quiet['state'] == 'restored' and opened == [['/usr/bin/open', '-a', before['path']]], (quiet, opened)
            busy, opened = watch(lambda now: 0.01)
            assert busy['state'] == 'left' and not opened, (busy, opened)
            return {'quiet_person': 'restored', 'person_typed': 'left alone'}
        self.check('an_app_opened_in_the_background_is_put_back_only_when_the_jump_was_pebbles', background)

        def browser_request():
            bridge, tab = FakeBridge(), OwnedTab()
            script = [v34.call('BrowserRequest', {'sites': SHOP, 'read': DOCS, 'url': SHOP + '/item/1',
                                                  'why': 'to add the kettle to your basket'}, 'br1'),
                      v34.final('I asked to use the shop.'), v42.observe('bo1'), v34.final('I can see the page.')]
            result = browser_flow(self.home, script, bridge, tab)
            self.apps.append(result['app'])
            assert 'BrowserRequest' in result['tools'][0], 'not offered with the extension connected'
            card = next(a for a in result['actions'] if a['kind'] == 'browser_grant')
            # Phase 44 (D121): a site and its www twin are one site to a person; both are on the card
            twin = 'https://www.' + SHOP.split('://', 1)[1]
            assert card['payload']['requires_review'] is True and card['payload']['sites'] == [SHOP, twin], \
                card['payload']
            assert result['decided'] and result['decided'][0] == {'kind': 'browser_grant', 'calls_before': 0}, \
                'a grant before the person decided'
            docs_twin = 'https://www.' + DOCS.split('://', 1)[1]
            assert bridge.calls[:2] == [('grant', [SHOP, twin], [SHOP, twin, DOCS, docs_twin]),
                                        ('open', SHOP + '/item/1')], bridge.calls
            view = v42.tool_message(result['requests'][-1], 'bo1')
            assert view.get('observation_id') and view['window']['origin'] == SHOP, view
            session = result['app'].screens.sessions(result['chat']['id'])[0]
            assert session['target']['read'] == [SHOP, twin, DOCS, docs_twin] and session['target']['origins'] == [SHOP, twin]
            result['app'].screen_session_revoke(session['session_id'])
            assert bridge.calls[-1] == ('end', 'a' * 32), 'ending access left the site grant'
            return {'card': 'browser_grant', 'grant_after_review': True, 'own_tab': True, 'end_access_ends_grant': True}
        self.check('a_browser_request_is_granted_only_by_review_and_opens_pebbles_own_tab', browser_request)

        def read_site():
            from pebble.screen_session import ScreenRefused, ScreenSessions
            from pebble.store import Store
            tab = OwnedTab(origin=DOCS)
            store = Store(self.home / 'read-site')
            try:
                sessions = ScreenSessions(store, {'browser': tab})
                target = {'kind': 'browser', 'profile': 'chrome', 'tab_id': 'owned-7', 'origins': [SHOP],
                          'read': [SHOP, DOCS]}
                chat, run = 'c' * 32, 'd' * 32
                sid = sessions.grant(chat, run, target, ['focus', 'press'], 600)['session_id']
                view = sessions.observe(sid, run)
                assert view.get('observation_id') and view.get('state') == 'active', 'a read site was handed back'
                button = next(e['ref'] for e in view['elements'] if e.get('label') == 'Add to basket')
                try:
                    sessions.act(sid, run, view['observation_id'], button, 'press', routine=True)
                except ScreenRefused as exc:
                    assert exc.code == 'action_not_granted', exc.code
                else:
                    raise AssertionError('an act on a read site went through')
                assert tab.performed == []
            finally:
                store.close()
            return {'observed': True, 'act': 'action_not_granted'}
        self.check('a_read_site_is_observed_and_never_acted_on', read_site)

        def connect():
            from pebble import browser_bridge as bb
            from pebble import native_host_setup as setup
            data, user = scratch / 'connect' / '.pebble' / 'data', scratch / 'connect'
            data.mkdir(parents=True, mode=0o700)
            for folder in ('Google/Chrome', 'Comet', 'BraveSoftware/Brave-Browser'):
                (user / 'Library/Application Support' / folder).mkdir(parents=True)
            program = scratch / 'connect-app/pebble-backend'
            program.parent.mkdir(parents=True)
            program.write_text('#!/bin/sh\nexit 0\n')
            program.chmod(0o755)
            plain = setup.plan(data, user_home=user, program=[program])
            assert plain['browsers'] == ['chrome'], 'the default plan is no longer Chrome alone'
            shown = setup.plan(data, user_home=user, program=[program], browsers=['comet'])
            setup.install(data, user_home=user, program=[program], confirm=shown['plan_sha256'], browsers=['comet'])
            assert bb.host_manifest_path(user, 'comet').is_file(), 'Comet was not connected'
            # Phase 44 (D118, D121): Comet reads Google Chrome's folder (measured), so connecting Comet writes that one
            # too; no browser outside the ticked one and the folder it reads is ever written.
            assert bb.host_manifest_path(user, 'chrome').is_file(), 'the folder Comet reads was not written'
            assert not bb.host_manifest_path(user, 'brave').exists(), 'an unticked browser was written'
            for browser in ('comet', 'chrome'):
                manifest = json.loads(bb.host_manifest_path(user, browser).read_text())
                assert manifest['allowed_origins'] == [bb.EXTENSION_ORIGIN] and manifest['name'] == bb.HOST_NAME
            return {'ticked': ['comet'], 'written': ['comet', 'chrome (where Comet reads)'], 'default': ['chrome']}
        self.check('connect_a_browser_writes_only_the_ticked_browsers', connect)

        def protocol_two():
            from pebble import browser_bridge as bb
            from pebble import native_host
            policy = read('browser-extension/lib/policy.mjs')
            match = re.search(r"export const PROTOCOL = '([^']+)';", policy)
            assert bb.PROTOCOL == native_host.PROTOCOL == 'pebble-browser/2' and match and match.group(1) == bb.PROTOCOL
            manifest = json.loads(read('browser-extension/manifest.json'))
            assert sorted(manifest['permissions']) == ['activeTab', 'debugger', 'nativeMessaging', 'storage',
                                                       'tabGroups'], 'the extension asks for more than before'
            return {'protocol': bb.PROTOCOL, 'permissions': 'unchanged'}
        self.check('host_native_host_and_extension_speak_pebble_browser_2', protocol_two)

        def version():
            current = checkout_version()
            assert tuple(int(x) for x in current.split('.')) >= (1, 9, 0), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = v42.changelog_entry('1.9.0')
            for needle in ('own search results', 'background', 'Shortcuts', 'own tab', 'window number', 'another desktop',
                           "page's accessibility", 'only opened the page', 'in every mode', 'Known limitations'):
                assert needle in entry, ('the 1.9.0 notes lack', needle)
            for claim in ('faster', 'cheaper', 'undetectable', '% fewer', 'saves '):
                assert claim not in entry.lower(), ('the 1.9.0 notes claim', claim)
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_1_9_0_and_its_notes_describe_it', version)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase43-', dir='/private/tmp')).resolve()
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
