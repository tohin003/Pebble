"""Phase 43 (1.9.0, M1): ax_worker protocol 2 over a fake Accessibility layer: windows named by their window-server
number, the focused and main windows AXWindows leaves out, the PEBBLE_WINDOW_NUMBERS switch, enhance/restore of a
Chromium page's accessibility, raise, and the worker protocol (no window is read; no Accessibility trust needed)."""
import io
import json
import sys

import pytest

from pebble import ax_worker
from pebble.ax_worker import AXError, BlockedBackend, MacBackend, handle

from test_ax_mac_42 import Element, FakeAX, doc_window


class FakeAX43(FakeAX):
    """FakeAX with window numbers, the app's focused and main windows, app attributes and settable app facts."""

    numbers = True

    def __init__(self, windows, *, focused=None, main=None, app=None, fill_on=None, web=None):
        super().__init__(windows)
        self.front, self.main = focused, main
        self.app = dict(app or {})      # the app element's own attributes (AXManualAccessibility, ...)
        self.fill_on, self.web = fill_on, web  # setting fill_on on the app gives the web area its children
        self.timeouts, self.codes, self.hidden = [], {}, set()  # codes: name -> what a set answers; hidden: unreadable

    def window_number(self, element):
        return getattr(element, 'attrs', {}).get('number')

    def set_timeout(self, element, seconds):
        self.timeouts.append(seconds)

    def attributes(self, element, names):
        if element == 'app':
            facts = super().attributes(element, names)
            if self.main is not None and 'AXMainWindow' in names:
                facts['AXMainWindow'] = self.main
            facts.update({n: self.app[n] for n in names if n in self.app and n not in self.hidden})
            return facts
        return super().attributes(element, names)

    def set(self, element, name, value):
        self.sets.append((element, name, value))
        if element == 'app':
            if name in self.app.get('unsettable', ()):
                return -25205
            self.app[name] = value
            if name == self.fill_on and self.web is not None:
                self.web.kids = [Element('AXStaticText', value='page text')]
            return self.codes.get(name, 0)
        return getattr(element, 'code', 0)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def backend(ax, clock=None):
    clock = clock or Clock()
    return MacBackend(ax, start_of=lambda pid: 1000.5, clock=clock, sleep=clock.sleep)


def window(title, number, **attrs):
    return Element('AXWindow', title=title, number=number, AXPosition=[0.0, 0.0], AXSize=[640.0, 480.0], **attrs)


def test_windows_are_named_by_number_and_the_unlisted_front_window_is_admitted(monkeypatch):
    monkeypatch.delenv('PEBBLE_WINDOW_NUMBERS', raising=False)
    listed, away = window('Inbox', 101, AXMain=False), window('YouTube - Comet', 202, AXSubrole='AXStandardWindow')
    rows = backend(FakeAX43([listed], focused=away, main=away)).windows(42, 1000.5)
    assert [(r['window_ref'], r['listed'], r['focused'], r['main']) for r in rows] == [
        ('n101', True, False, False), ('n202', False, True, True)]
    assert rows[1]['window_number'] == 202 and rows[1]['subrole'] == 'AXStandardWindow' and rows[1]['minimized'] is False
    assert len(rows) == 2, 'a focused window that is also the main one is admitted once'


def test_an_unlisted_window_without_a_number_is_left_out_and_a_listed_one_keeps_the_old_ref(monkeypatch):
    monkeypatch.delenv('PEBBLE_WINDOW_NUMBERS', raising=False)
    listed, away = window('Notes', None), window('Elsewhere', None)
    rows = backend(FakeAX43([listed], focused=away)).windows(42, 1000.5)
    assert len(rows) == 1 and rows[0]['window_ref'] == 'w0.' + ax_worker.sha256('Notes')[:16]
    assert rows[0]['window_number'] is None and rows[0]['listed'] is True


def test_the_switch_restores_1_8_0_rows_exactly(monkeypatch):
    monkeypatch.setenv('PEBBLE_WINDOW_NUMBERS', '0')
    doc, *_ = doc_window()
    away = window('Elsewhere', 303)
    rows = backend(FakeAX43([doc], focused=away)).windows(42, 1000.5)
    digest = ax_worker.sha256('e-n2-ab12cd34.txt')[:16]
    assert rows == [{'window_ref': f'w0.{digest}', 'title': 'e-n2-ab12cd34.txt', 'ax_identifier': None,
                     'focused': False, 'frame': [100.0, 50.0, 800.0, 600.0]}]


def test_a_number_ref_survives_a_title_change_where_the_old_ref_did_not(monkeypatch):
    monkeypatch.delenv('PEBBLE_WINDOW_NUMBERS', raising=False)
    doc, area, *_ = doc_window()
    doc.attrs['number'] = 404
    ax = FakeAX43([doc], focused=doc)
    mac = backend(ax)
    assert mac.windows(42, 1000.5)[0]['window_ref'] == 'n404'
    doc.attrs['AXTitle'] = 'e-n2-ab12cd34.txt - Edited'
    snap = mac.tree(42, 1000.5, 'n404', 300)
    assert snap['window']['window_ref'] == 'n404' and any(e['role'] == 'AXTextArea' for e in snap['elements'])
    legacy = 'w0.' + ax_worker.sha256('e-n2-ab12cd34.txt')[:16]
    with pytest.raises(AXError) as refused:
        MacBackend(FakeAX([doc]), start_of=lambda pid: 1000.5).tree(42, 1000.5, legacy, 300)
    assert refused.value.code == 'not_found'


def test_a_number_ref_with_no_window_is_not_found(monkeypatch):
    monkeypatch.delenv('PEBBLE_WINDOW_NUMBERS', raising=False)
    with pytest.raises(AXError) as refused:
        backend(FakeAX43([window('A', 1)])).tree(42, 1000.5, 'n999', 10)
    assert refused.value.code == 'not_found' and 'front window' in refused.value.detail


def test_window_refs_accept_both_forms_only():
    for good in ('w0.' + 'a' * 16, 'n1', 'n4294967295'):
        assert ax_worker.WINDOW_REF.fullmatch(good)
    for bad in ('n0', 'n01', 'n12345678901', 'w1', 'x5', 'n-3'):
        assert not ax_worker.WINDOW_REF.fullmatch(bad)


def page_app():
    web = Element('AXWebArea')
    return window('Page', 7, kids=[Element('AXGroup', kids=[web])]), web


def test_enhance_sets_manual_accessibility_first_and_stops_once_the_page_fills():
    win, web = page_app()
    ax = FakeAX43([win], focused=win, app={'AXEnhancedUserInterface': False}, fill_on='AXManualAccessibility', web=web)
    clock = Clock()
    result = backend(ax, clock).enhance(42, 1000.5)
    assert result['original'] == {'AXManualAccessibility': None, 'AXEnhancedUserInterface': False}, 'unknown, not invented'
    assert result['changed'] == ['AXManualAccessibility'] and result['refused'] == [] and result['filled'] is True
    assert ('app', 'AXEnhancedUserInterface', True) not in ax.sets, 'the window-manager-disturbing switch stays off'
    assert ax.opened == ax.closed, 'every AX session is closed'


def test_enhance_turns_on_the_enhanced_interface_only_when_the_page_stays_empty():
    win, web = page_app()
    ax = FakeAX43([win], focused=win, app={'AXManualAccessibility': False, 'AXEnhancedUserInterface': False},
                  fill_on='AXEnhancedUserInterface', web=web)
    clock = Clock()
    result = backend(ax, clock).enhance(42, 1000.5)
    assert result['changed'] == ['AXManualAccessibility', 'AXEnhancedUserInterface'] and result['filled'] is True
    assert ax_worker.ENHANCE_MANUAL_WAIT <= clock.now < ax_worker.ENHANCE_WAIT and result['ms'] == round(clock.now * 1000)


def test_enhance_gives_up_after_its_wait_and_reports_an_empty_page():
    win, web = page_app()
    ax = FakeAX43([win], focused=win, web=web)
    clock = Clock()
    result = backend(ax, clock).enhance(42, 1000.5)
    assert result['filled'] is False and result['changed'] == ['AXManualAccessibility', 'AXEnhancedUserInterface']
    assert result['refused'] == [] and clock.now >= ax_worker.ENHANCE_WAIT


def test_enhance_waits_only_after_a_switch_it_changed():
    win, web = page_app()
    unsettable = ('AXManualAccessibility', 'AXEnhancedUserInterface')
    clock = Clock()
    result = backend(FakeAX43([win], focused=win, app={'unsettable': unsettable}, web=web), clock).enhance(42, 1000.5)
    assert result['changed'] == [] and result['refused'] == list(unsettable) and clock.now == 0, 'no wait at all'
    clock = Clock()
    half = backend(FakeAX43([win], focused=win, app={'unsettable': unsettable[1:]}, web=web), clock).enhance(42, 1000.5)
    assert half['changed'] == ['AXManualAccessibility'] and half['refused'] == ['AXEnhancedUserInterface']
    assert ax_worker.ENHANCE_MANUAL_WAIT <= clock.now < ax_worker.ENHANCE_WAIT


def test_enhance_changes_nothing_when_the_page_is_already_filled():
    win, web = page_app()
    web.kids = [Element('AXLink', title='Home')]
    ax = FakeAX43([win], focused=win, app={'AXManualAccessibility': False})
    result = backend(ax).enhance(42, 1000.5)
    assert result['changed'] == [] and result['filled'] is True and ax.sets == []


def test_restore_sets_back_only_originals_it_knows():
    ax = FakeAX43([window('Page', 7)], app={'AXManualAccessibility': True, 'AXEnhancedUserInterface': True})
    result = backend(ax).restore(42, 1000.5, {'AXManualAccessibility': None, 'AXEnhancedUserInterface': False})
    assert result == {'restored': ['AXEnhancedUserInterface'], 'left': ['AXManualAccessibility']}
    assert ax.sets == [('app', 'AXEnhancedUserInterface', False)]


def test_raise_unminimizes_raises_and_brings_the_app_forward(monkeypatch):
    monkeypatch.delenv('PEBBLE_WINDOW_NUMBERS', raising=False)
    away = window('Report', 55, AXMinimized=True)
    ax = FakeAX43([away], focused=away)
    assert backend(ax).raise_window(42, 1000.5, 'n55') == {'raised': True, 'frontmost': True}
    assert (away, 'AXMinimized', False) in ax.sets and (away, 'AXRaise') in ax.acts
    assert ('app', 'AXFrontmost', True) in ax.sets


def test_the_instance_is_checked_before_any_new_op():
    mac = MacBackend(FakeAX43([window('A', 1)]), start_of=lambda pid: 2000.0)
    for call in (lambda: mac.enhance(42, 1000.5), lambda: mac.restore(42, 1000.5, {'AXManualAccessibility': False}),
                 lambda: mac.raise_window(42, 1000.5, 'n1')):
        with pytest.raises(AXError) as refused:
            call()
        assert refused.value.code == 'instance_changed'


class Served:
    name, available, window_numbers = 'fake43', True, True

    def enhance(self, pid, start):
        return {'original': {'AXManualAccessibility': 'yes', 'AXEnhancedUserInterface': False, 'Other': True},
                'changed': ['AXManualAccessibility', 'AXSomething'], 'refused': 'x', 'filled': 1, 'ms': 10 ** 9,
                'title': 'secret page'}

    def restore(self, pid, start, changed):
        return {'restored': list(changed), 'left': ['nope'], 'extra': 1}

    def raise_window(self, pid, start, window_ref):
        return {'frontmost': True, 'title': 'secret'}


def call(op, args, backend_=None):
    return handle({'id': 1, 'op': op, 'args': args}, backend_ or Served())


def test_hello_reports_the_protocol_ops_and_window_numbers():
    assert call('hello', {}) == {'id': 1, 'ok': True, 'result': {
        'protocol': 'pebble-ax/2', 'backend': 'fake43', 'available': True,
        'ops': ['hello', 'windows', 'tree', 'perform', 'enhance', 'restore', 'raise'], 'window_numbers': True}}


def test_the_new_ops_answer_bounded_facts_only():
    enhanced = call('enhance', {'pid': 42, 'start': 1000.5})['result']
    assert enhanced == {'original': {'AXManualAccessibility': None, 'AXEnhancedUserInterface': False},
                        'changed': ['AXManualAccessibility'], 'refused': [], 'filled': False, 'ms': None,
                        'codes': {}}
    restored = call('restore', {'pid': 42, 'start': 1000.5, 'changed': {'AXEnhancedUserInterface': False}})['result']
    assert restored == {'restored': ['AXEnhancedUserInterface'], 'left': []}
    raised = call('raise', {'pid': 42, 'start': 1000.5, 'window_ref': 'n9'})['result']
    assert raised == {'raised': True, 'frontmost': True}


@pytest.mark.parametrize('changed', [None, {}, {'AXOther': True}, {'AXManualAccessibility': 'false'},
                                     {'AXManualAccessibility': True, 'AXEnhancedUserInterface': True, 'x': 1}])
def test_restore_refuses_anything_but_the_two_attributes(changed):
    answer = call('restore', {'pid': 42, 'start': 1000.5, 'changed': changed})
    assert answer['ok'] is False and answer['error'] == 'invalid'


def test_a_backend_without_the_new_ops_refuses_them_and_the_blocked_one_is_unavailable():
    class Old:
        name = 'old'
    assert call('enhance', {'pid': 42, 'start': 1000.5}, Old())['error'] == 'unsupported'
    for op, args in (('enhance', {}), ('restore', {'changed': {'AXManualAccessibility': False}}),
                     ('raise', {'window_ref': 'n3'})):
        assert call(op, {'pid': 42, 'start': 1000.5, **args}, BlockedBackend())['error'] == 'unavailable'


def test_protocol_2_window_facts_are_bounded_and_sanitizing_is_idempotent():
    raw = {'window_ref': 'n7', 'title': 'Doc', 'window_number': 1 << 40, 'listed': 'no', 'main': 1, 'minimized': True,
           'subrole': 'not a role', 'focused': True}
    once = ax_worker.sanitize_window(raw)
    assert (once['window_number'], once['listed'], once['main'], once['minimized'], once['subrole']) == (
        None, True, False, True, None)
    assert ax_worker.sanitize_window(once) == once
    assert ax_worker.sanitize_window({**raw, 'window_number': 7, 'listed': False})['listed'] is False
    old = ax_worker.sanitize_window({'window_ref': 'w0.' + 'a' * 16, 'title': 'Doc'})
    assert not {'window_number', 'listed', 'main', 'minimized', 'subrole'} & set(old), '1.8.0 rows keep their shape'


def test_the_client_gives_enhance_its_longer_deadline_and_asks_hello_once():
    client = ax_worker.WorkerClient(['true'])
    seen = []

    def fake_call(op, args, *, timeout=None):
        seen.append((op, timeout))
        client.proc = client.proc or object()
        return {'protocol': ax_worker.PROTOCOL} if op == 'hello' else {}
    client.call = fake_call
    client.enhance(1, 2.0)
    client.hello()
    client.hello()
    assert seen == [('enhance', ax_worker.ENHANCE_SECONDS), ('hello', None)]


def test_only_the_window_numbers_switch_reaches_the_worker(monkeypatch):
    seen = {}

    class Popen:
        def __init__(self, argv, **kwargs):
            seen.update(kwargs['env'])
    monkeypatch.setattr(ax_worker.subprocess, 'Popen', Popen)
    monkeypatch.setenv('PEBBLE_WINDOW_NUMBERS', '0')
    monkeypatch.setenv('PEBBLE_SECRET_SETTING', 'x')
    ax_worker._spawn(['true'])
    assert seen == {'PATH': '/usr/bin:/bin', 'TMPDIR': seen['TMPDIR'], 'PEBBLE_WINDOW_NUMBERS': '0'}
    seen.clear()
    monkeypatch.setenv('PEBBLE_WINDOW_NUMBERS', '1')
    ax_worker._spawn(['true'])
    assert 'PEBBLE_WINDOW_NUMBERS' not in seen


@pytest.mark.skipif(sys.platform != 'darwin', reason='the real ApplicationServices binding is macOS only')
def test_the_real_binding_looks_the_call_up_once_and_answers_none_without_a_window():
    ax = ax_worker.MacAX()
    assert ax.window_number(None) is None and isinstance(ax.numbers, bool)
    assert ax.numbers is (ax.get_window is not None)


def test_a_switch_that_answers_cannot_complete_but_happened_counts_as_changed_and_is_waited_for():
    """Measured on Comet (2026-10-25): switching on answers -25204 and happens; switching off answers -25208 and
    happens. Only the value read back (or a page that fills) tells."""
    win, web = page_app()
    ax = FakeAX43([win], focused=win, app={'AXEnhancedUserInterface': False}, fill_on='AXEnhancedUserInterface',
                  web=web)
    ax.hidden.add('AXManualAccessibility')  # Comet: unreadable
    ax.codes.update({'AXManualAccessibility': -25204, 'AXEnhancedUserInterface': -25204})
    result = backend(ax).enhance(42, 1000.5)
    assert result['changed'] == ['AXManualAccessibility', 'AXEnhancedUserInterface'] and result['filled'] is True
    assert result['codes'] == {'AXManualAccessibility': -25204, 'AXEnhancedUserInterface': -25204}
    assert ax_worker.ENHANCE_SET_SECONDS in ax.timeouts, 'a longer message for the switch'
    ax.codes['AXEnhancedUserInterface'] = -25208
    restored = backend(ax).restore(42, 1000.5, {'AXEnhancedUserInterface': False, 'AXManualAccessibility': None})
    assert restored == {'restored': ['AXEnhancedUserInterface'], 'left': ['AXManualAccessibility']}
    assert ax.app['AXEnhancedUserInterface'] is False


def test_a_switch_that_reads_back_false_was_refused_and_is_not_waited_for():
    win, web = page_app()
    ax = FakeAX43([win], focused=win, app={'AXEnhancedUserInterface': False, 'AXManualAccessibility': False},
                  web=web)
    ax.app['unsettable'] = ('AXManualAccessibility', 'AXEnhancedUserInterface')
    clock = Clock()
    result = backend(ax, clock).enhance(42, 1000.5)
    assert result['changed'] == [] and result['refused'] == ['AXManualAccessibility', 'AXEnhancedUserInterface']
    assert clock.now == 0


def test_a_restore_that_does_not_read_back_is_left_never_claimed():
    ax = FakeAX43([window('Page', 7)], app={'AXEnhancedUserInterface': True})
    ax.hidden.add('AXEnhancedUserInterface')
    assert backend(ax).restore(42, 1000.5, {'AXEnhancedUserInterface': False}) == {
        'restored': [], 'left': ['AXEnhancedUserInterface']}


def browser_window():
    page = Element('AXWebArea', kids=[Element('AXLink', title='First video', AXURL='https://example.com/watch?v=1'),
                                      Element('AXStaticText', value='results')])
    toolbar = Element('AXToolbar', kids=[Element('AXButton', title='Back'), Element('AXButton', title='Reload')])
    tabs = Element('AXTabGroup', kids=[Element('AXRadioButton', title='Tab one')])
    return window('Results - Comet', 9, kids=[toolbar, tabs, Element('AXGroup', kids=[page])]), page


def test_page_first_walks_a_browsers_page_before_its_own_controls_with_the_same_paths(monkeypatch):
    monkeypatch.delenv('PEBBLE_WINDOW_NUMBERS', raising=False)
    win, _ = browser_window()
    mac = backend(FakeAX43([win], focused=win))
    plain = [(e['role'], e['path']) for e in mac.tree(42, 1000.5, 'n9', 300)['elements']]
    first = [(e['role'], e['path']) for e in mac.tree(42, 1000.5, 'n9', 300, page_first=True)['elements']]
    assert plain[0] == ('AXToolbar', '/0') and first[0] == ('AXWebArea', '/2/0'), first
    assert sorted(plain) == sorted(first) and len(first) == len(set(first)), 'the same rows, each once'
    assert first.index(('AXLink', '/2/0/0')) < first.index(('AXButton', '/0/0'))
    capped = mac.tree(42, 1000.5, 'n9', 3, page_first=True)
    assert [e['role'] for e in capped['elements']] == ['AXWebArea', 'AXLink', 'AXStaticText'] and capped['truncated']


def test_the_protocol_passes_page_first_only_to_a_backend_that_declares_it():
    seen = []

    class Declares(Served):
        pages_first = True

        def tree(self, pid, start, ref, limit, page_first=False):
            seen.append(page_first)
            return {'window': {'window_ref': ref, 'title': 'T'}, 'elements': []}

    class Plain(Served):
        def tree(self, pid, start, ref, limit):
            seen.append('plain')
            return {'window': {'window_ref': ref, 'title': 'T'}, 'elements': []}
    args = {'pid': 42, 'start': 1000.5, 'window_ref': 'n9', 'limit': 5, 'page_first': True}
    assert call('tree', args, Declares())['ok'] and call('tree', args, Plain())['ok']
    assert seen == [True, 'plain']
    assert call('tree', {**args, 'page_first': 'yes'}, Declares())['error'] == 'invalid'


def test_a_pages_banner_and_main_landmarks_come_before_its_navigation(monkeypatch):
    # Phase 44 (1.9.1), measured on YouTube in Comet: a signed-in person's guide (navigation, dozens of subscriptions)
    # came before the results (main) in the page's order and filled every row; the banner holds the search box.
    monkeypatch.delenv('PEBBLE_WINDOW_NUMBERS', raising=False)
    guide = Element('AXGroup', AXSubrole='AXLandmarkNavigation',
                    kids=[Element('AXLink', title=f'Channel {n}', AXURL=f'https://example.com/@c{n}') for n in range(8)])
    banner = Element('AXGroup', AXSubrole='AXLandmarkBanner', kids=[Element('AXComboBox', title='Search')])
    main = Element('AXGroup', AXSubrole='AXLandmarkMain',
                   kids=[Element('AXGroup', kids=[Element('AXLink', title='First video',
                                                          AXURL='https://example.com/watch?v=1')])])
    page = Element('AXWebArea', kids=[Element('AXGroup', kids=[guide, banner, main])])
    win = window('Results - Comet', 9, kids=[Element('AXToolbar', kids=[Element('AXButton', title='Back')]),
                                             Element('AXGroup', kids=[page])])
    mac = backend(FakeAX43([win], focused=win))
    rows = mac.tree(42, 1000.5, 'n9', 300, page_first=True)['elements']
    order = [(e['role'], e.get('title')) for e in rows]
    assert order[:5] == [('AXWebArea', None), ('AXGroup', None), ('AXComboBox', 'Search'), ('AXGroup', None),
                         ('AXGroup', None)], order
    assert order.index(('AXLink', 'First video')) < order.index(('AXLink', 'Channel 0'))
    paths = [e['path'] for e in rows]
    assert len(paths) == len(set(paths)), 'each row once'
    plain = mac.tree(42, 1000.5, 'n9', 300)['elements']
    assert sorted(e['path'] for e in plain) == sorted(paths), 'the same rows as the plain walk'
    capped = mac.tree(42, 1000.5, 'n9', 6, page_first=True)['elements']
    assert ('AXLink', 'First video') in [(e['role'], e.get('title')) for e in capped], 'the results fit in a few rows'
