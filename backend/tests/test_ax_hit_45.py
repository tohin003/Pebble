"""Phase 45 (layer 4): the AX worker's hit op over a fake Accessibility layer (test_ax_mac_43's FakeAX43 with the
system-wide element, element_at and pid_of), its protocol and sanitizer, and the client's request. No window is read
and nothing under a real point is asked; the real bindings are only resolved."""
import sys

import pytest

from pebble import ax_worker
from pebble.ax_worker import AXError, BlockedBackend, ElementHandle, MacBackend, WorkerClient, handle, sanitize_hit

from test_ax_mac_42 import Element
from test_ax_mac_43 import FakeAX43


class HitAX(FakeAX43):
    """FakeAX43 whose single-element attributes (AXParent, AXWindow, AXFocusedUIElement) come back as ElementHandle
    values, as MacAX.python() returns them; at(x, y) answers a point, owners maps an element to its process."""

    def __init__(self, windows, *, at=None, focused_element=None, owners=None, **kw):
        super().__init__(windows, **kw)
        self.registry, self.at, self.focused_element, self.owners = {}, at, focused_element, owners or {}
        self.points, self.systems = [], 0

    def handle(self, element):
        made = ElementHandle(id(element))
        self.registry[int(made)] = element
        return made

    def resolve(self, value):
        return self.registry.get(int(value), value) if isinstance(value, ElementHandle) else value

    def attributes(self, element, names):
        element = self.resolve(element)
        if element == 'system':
            return {'AXFocusedUIElement': self.handle(self.focused_element)} \
                if self.focused_element is not None and 'AXFocusedUIElement' in names else {}
        facts = super().attributes(element, names)
        for key in ('AXParent', 'AXWindow'):
            if facts and facts.get(key) is not None:
                facts[key] = self.handle(facts[key])
        return facts

    def same(self, one, other):
        return self.resolve(one) is self.resolve(other)

    def value(self, element):
        return super().value(self.resolve(element))

    def settable(self, element, name):
        return super().settable(self.resolve(element), name)

    def system(self):
        self.systems += 1
        return 'system'

    def element_at(self, system, x, y):
        assert system == 'system', 'the system-wide element, so another app on top answers as itself'
        self.points.append((x, y))
        return self.at

    def pid_of(self, element):
        return self.owners.get(id(self.resolve(element)), 42)


def backend(ax, start=1000.5):
    return MacBackend(ax, start_of=lambda pid: start)


def window(title='Draft', number=7):
    return Element('AXWindow', title=title, number=number, AXPosition=[100.0, 50.0], AXSize=[640.0, 400.0])


def inside(element, parent, window_):
    element.attrs.update(AXParent=parent, AXWindow=window_)
    return element


@pytest.fixture(autouse=True)
def numbers(monkeypatch):
    monkeypatch.delenv('PEBBLE_WINDOW_NUMBERS', raising=False)


def test_a_point_answers_with_its_process_element_ancestors_and_window():
    win = window()
    group = inside(Element('AXGroup', AXDescription='Toolbar'), win, win)
    button = inside(Element('AXButton', title='Send', AXPosition=[400.0, 300.0], AXSize=[80.0, 24.0],
                            AXEnabled=True), group, win)
    ax = HitAX([win], focused=win, at=button)
    found = backend(ax).hit(42, 1000.5, 'n7', point=(440.0, 312.0))
    assert ax.points == [(440.0, 312.0)] and ax.systems == 1
    assert found['pid'] == 42 and found['in_window'] is True and found['menu'] is False
    assert (found['element']['role'], found['element']['title'], found['element']['frame']) == (
        'AXButton', 'Send', [400.0, 300.0, 80.0, 24.0])
    assert [a['role'] for a in found['ancestors']] == ['AXGroup', 'AXWindow'] and found['element']['path'] == 'hit'
    assert ax.opened == ax.closed == 1, 'the session is closed'


def test_another_apps_dialog_on_top_answers_as_its_own_process_and_window():
    win, other = window(), window('Allow access?', 99)
    allow = inside(Element('AXButton', title='Allow'), other, other)
    ax = HitAX([win], focused=win, at=allow, owners={id(allow): 501})
    found = backend(ax).hit(42, 1000.5, 'n7', point=(300.0, 200.0))
    assert found['pid'] == 501 and found['in_window'] is False and found['element']['title'] == 'Allow'


def test_a_text_inside_an_unlabelled_button_says_its_words_and_a_secure_value_is_never_read():
    win = window()
    button = inside(Element('AXButton'), win, win)
    label = inside(Element('AXStaticText', value='Delete'), button, win)
    ax = HitAX([win], focused=win, at=label)
    found = backend(ax).hit(42, 1000.5, 'n7', point=(1.0, 2.0))
    assert found['element']['value'] == 'Delete' and found['ancestors'][0]['role'] == 'AXButton'
    secret = inside(Element('AXTextField', subrole='AXSecureTextField', value='hunter2', settable=True), win, win)
    ax = HitAX([win], focused=win, at=secret)
    found = backend(ax).hit(42, 1000.5, 'n7', point=(1.0, 2.0))
    assert found['element']['secure'] is True and 'value' not in found['element']
    assert sanitize_hit(found)['element']['value'] is None
    assert secret not in ax.value_reads and 'hunter2' not in repr(found)


def test_the_keyboards_element_is_the_system_wides_focus():
    win = window()
    field = inside(Element('AXTextField', title='To', value='a@b', settable=True), win, win)
    ax = HitAX([win], focused=win, focused_element=field)
    found = backend(ax).hit(42, 1000.5, 'n7', focused=True)
    assert found['element']['role'] == 'AXTextField' and found['element']['editable'] is True
    assert found['in_window'] is True and ax.points == [], 'no point is asked'


def test_a_menu_item_has_no_window_and_nothing_at_a_point_answers_empty():
    win = window()
    menu = Element('AXMenu')
    item = Element('AXMenuItem', title='Copy', AXParent=menu)
    ax = HitAX([win], focused=win, at=item)
    found = backend(ax).hit(42, 1000.5, 'n7', point=(5.0, 5.0))
    assert found['menu'] is True and found['in_window'] is None and found['ancestors'][0]['role'] == 'AXMenu'
    empty = backend(HitAX([win], focused=win, at=None)).hit(42, 1000.5, 'n7', point=(5.0, 5.0))
    assert empty == {'pid': None, 'element': None, 'ancestors': [], 'in_window': None, 'menu': False}


def test_ancestors_are_bounded_and_the_instance_is_checked_first():
    win = window()
    node = win
    for depth in range(10):
        node = inside(Element('AXGroup', title=f'level {depth}'), node, win)
    ax = HitAX([win], focused=win, at=node)
    found = backend(ax).hit(42, 1000.5, 'n7', point=(5.0, 5.0))
    assert len(found['ancestors']) == ax_worker.HIT_ANCESTORS
    ax = HitAX([win], focused=win, at=node)
    with pytest.raises(AXError) as changed:
        MacBackend(ax, start_of=lambda pid: 2000.0).hit(42, 1000.5, 'n7', point=(5.0, 5.0))
    assert changed.value.code == 'instance_changed' and ax.opened == 0 and ax.points == []
    with pytest.raises(AXError) as gone:
        backend(HitAX([win], focused=win, at=node)).hit(42, 1000.5, 'n8', point=(5.0, 5.0))
    assert gone.value.code == 'not_found'


class Served:
    name, available, window_numbers = 'served45', True, True

    def __init__(self, answer=None):
        self.answer, self.calls = answer, []

    def hit(self, pid, start, window_ref, point=None, focused=False):
        self.calls.append((pid, start, window_ref, point, focused))
        return self.answer


def call(op, args, served):
    return handle({'id': 1, 'op': op, 'args': args}, served)


def test_the_protocol_serves_hit_with_exactly_one_target_and_bounded_facts():
    raw = {'pid': 42, 'element': {'role': 'AXButton', 'path': 'hit', 'title': 'x' * 900, 'extra': 'dropped'},
           'ancestors': [{'role': 'AXGroup', 'path': 'hit^1'}] * 9 + ['junk'], 'in_window': 'yes', 'menu': 1,
           'title': 'a page title'}
    served = Served(raw)
    answer = call('hit', {'pid': 42, 'start': 1000.5, 'window_ref': 'n7', 'point': [10, 20.5]}, served)
    assert answer['ok'] is True and served.calls == [(42, 1000.5, 'n7', (10, 20.5), False)]
    result = answer['result']
    assert set(result) == {'pid', 'element', 'ancestors', 'in_window', 'menu'} and result['in_window'] is None
    assert len(result['element']['title']) == ax_worker.MAX_LABEL_SOURCE and 'extra' not in result['element']
    assert len(result['ancestors']) == ax_worker.HIT_ANCESTORS and result['menu'] is False
    for args in ({}, {'point': [1, 2], 'focused': True}, {'point': [1]}, {'point': [1, float('nan')]},
                 {'point': [1e7, 2]}, {'focused': 'yes'}, {'point': '1,2'}):
        refused = call('hit', {'pid': 42, 'start': 1000.5, 'window_ref': 'n7', **args}, served)
        assert refused == {'id': 1, 'ok': False, 'error': 'invalid', 'detail': refused['detail']}, args
    assert call('hit', {'pid': 42, 'start': 1000.5, 'window_ref': 'n7', 'focused': True}, served)['ok'] is True
    assert call('hit', {'pid': 42, 'start': 1000.5, 'window_ref': 'n7', 'focused': True}, object())['error'] == \
        'unsupported'


def test_hello_keeps_its_list_while_hit_is_served():
    hello = call('hello', {}, Served())['result']
    assert hello['ops'] == list(ax_worker.OPS) and 'hit' not in hello['ops'] and ax_worker.EXTRA_OPS == ('hit',)
    with pytest.raises(AXError) as blocked:
        BlockedBackend().hit(42, 1000.5, 'n7', point=(1.0, 1.0))
    assert blocked.value.code == 'unavailable'


def test_the_sanitizer_drops_bad_pids_and_malformed_elements():
    assert sanitize_hit({'pid': True, 'element': {'role': 'button'}}) == {
        'pid': None, 'element': None, 'ancestors': [], 'in_window': None, 'menu': False}
    assert sanitize_hit({'pid': 1 << 40, 'element': None, 'ancestors': 'x', 'in_window': False})['in_window'] is False
    with pytest.raises(AXError):
        sanitize_hit(['not', 'an', 'object'])


def test_the_client_asks_for_a_point_or_the_focus():
    client = WorkerClient(['unused'])
    sent = []
    client.call = lambda op, args, **kw: sent.append((op, args)) or {}
    client.hit(42, 1000.5, 'n7', point=(3, 4))
    client.hit(42, 1000.5, 'n7', focused=True)
    assert sent == [('hit', {'pid': 42, 'start': 1000.5, 'window_ref': 'n7', 'point': [3.0, 4.0]}),
                    ('hit', {'pid': 42, 'start': 1000.5, 'window_ref': 'n7', 'focused': True})]


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS bindings')
def test_the_real_bindings_resolve_without_asking_anything():
    ax = ax_worker.MacAX()
    for name in ('AXUIElementCreateSystemWide', 'AXUIElementCopyElementAtPosition', 'AXUIElementGetPid'):
        assert getattr(ax.ax_lib, name) is not None
    ax.arena = []
    system = ax.system()  # an object only: nothing is asked of any app until a query
    assert system and ax.arena == [system]
    ax.close()
