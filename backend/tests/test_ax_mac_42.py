"""Phase 42 (1.8.0 groundwork, E-N2): ax_worker.MacBackend over a fake Accessibility layer, the E-N2 probe, and the
real ctypes bindings (MacAX) on CoreFoundation values made here (no window is read; no Accessibility trust needed)."""
import json
import sys

import pytest

from pebble import ax_worker
from pebble.ax_worker import AXError, MacBackend, handle, value_digest


class Element:
    def __init__(self, role, title=None, value=None, kids=(), subrole=None, settable=False, **attrs):
        self.attrs = {'AXRole': role, **({'AXTitle': title} if title is not None else {}),
                      **({'AXSubrole': subrole} if subrole else {}), **attrs}
        self.value, self.kids, self.settable = value, list(kids), settable


class FakeAX:
    """MacAX's surface over Element trees: one app, its windows; records every value read."""

    def __init__(self, windows, trusted=True):
        self.windows, self.is_trusted = list(windows), trusted
        self.opened = self.closed = 0
        self.value_reads, self.truncate, self.acts, self.sets = [], set(), [], []

    def trusted(self):
        return self.is_trusted

    def open(self, pid):
        self.opened += 1
        return 'app'

    def close(self):
        self.closed += 1

    front = None  # Phase 43: the app's AXFocusedWindow

    def same(self, one, other):
        return one is other

    def attributes(self, element, names):
        if element == 'app':
            facts = {'AXWindows': self.windows} if 'AXWindows' in names else {}
            if self.front is not None and 'AXFocusedWindow' in names:
                facts['AXFocusedWindow'] = self.front
            return facts
        if element.attrs.get('busy'):
            return None
        facts = {n: element.attrs[n] for n in names if n in element.attrs}
        if 'AXChildren' in names and element.kids:
            facts['AXChildren'] = element.kids
        return facts

    def value(self, element):
        self.value_reads.append(element)
        return element.value, element in self.truncate

    def settable(self, element, name):
        return element.settable

    def act(self, element, action):
        self.acts.append((element, action))
        return getattr(element, 'code', 0)

    def set(self, element, name, value):
        self.sets.append((element, name, value))
        return getattr(element, 'code', 0)


def doc_window(title='e-n2-ab12cd34.txt', text='Pebble E-N2 marker 7f3a', **extra):
    password = Element('AXTextField', subrole='AXSecureTextField', value='hunter2', settable=True)
    secret = Element('AXTextField', title='Verification code', value='123456', settable=True)
    area = Element('AXTextArea', value=text, settable=True)
    scroll = Element('AXScrollArea', kids=[area])
    close = Element('AXButton', subrole='AXCloseButton', AXDescription='close button', AXEnabled=True,
                    AXPosition=[10.0, 20.0], AXSize=[14.0, 16.0])
    window = Element('AXWindow', title=title, kids=[close, scroll, password, secret], AXMain=True,
                     AXPosition=[100.0, 50.0], AXSize=[800.0, 600.0], **extra)
    return window, area, password, secret


def backend(ax, start=1000.5):
    return MacBackend(ax, start_of=lambda pid: start)


def test_windows_name_each_by_index_and_title_digest():
    window, *_ = doc_window()
    rows = backend(FakeAX([window])).windows(42, 1000.5)
    digest = ax_worker.sha256('e-n2-ab12cd34.txt')[:16]
    assert rows == [{'window_ref': f'w0.{digest}', 'title': 'e-n2-ab12cd34.txt', 'ax_identifier': None,
                     'focused': True, 'frame': [100.0, 50.0, 800.0, 600.0]}]


def test_the_tree_walks_in_reading_order_and_never_reads_a_secure_value():
    window, area, password, secret = doc_window()
    ax = FakeAX([window])
    mac = backend(ax)
    ref = mac.windows(42, 1000.5)[0]['window_ref']
    snap = mac.tree(42, 1000.5, ref, 300)
    paths = [(e['role'], e['path']) for e in snap['elements']]
    assert paths == [('AXButton', '/0'), ('AXScrollArea', '/1'), ('AXTextArea', '/1/0'), ('AXTextField', '/2'),
                     ('AXTextField', '/3')]
    assert password not in ax.value_reads and secret not in ax.value_reads, 'secure values are never requested'
    by_path = {e['path']: e for e in snap['elements']}
    assert by_path['/2']['secure'] is True and 'value' not in by_path['/2']
    assert by_path['/3']['secure'] is True, 'a secret-looking label raises an editable field to secure'
    assert by_path['/1/0']['value'] == 'Pebble E-N2 marker 7f3a' and by_path['/1/0']['multiline'] is True
    assert by_path['/0']['frame'] == [10.0, 20.0, 14.0, 16.0] and by_path['/0']['enabled'] is True
    assert ax.opened == ax.closed == 2, 'every session is closed'


def test_through_the_protocol_the_text_reads_back_as_its_digest():
    window, *_ = doc_window()
    mac = backend(FakeAX([window]))
    rows = handle({'id': 1, 'op': 'windows', 'args': {'pid': 42, 'start': 1000.5}}, mac)['result']
    tree = handle({'id': 2, 'op': 'tree', 'args': {'pid': 42, 'start': 1000.5, 'window_ref': rows[0]['window_ref'],
                                                   'limit': 300}}, mac)['result']
    area = next(e for e in tree['elements'] if e['role'] == 'AXTextArea')
    assert area['value_sha256'] == value_digest('Pebble E-N2 marker 7f3a')
    assert all(e['value'] is None for e in tree['elements'] if e['secure'])
    assert 'hunter2' not in json.dumps(tree) and '123456' not in json.dumps(tree)


def test_a_long_value_reports_its_prefix_digest_as_truncated():
    window, area, *_ = doc_window(text='x' * 70_000)
    ax = FakeAX([window])
    ax.truncate.add(area)
    mac = backend(ax)
    snap = mac.tree(42, 1000.5, mac.windows(42, 1000.5)[0]['window_ref'], 300)
    element = next(e for e in snap['elements'] if e['role'] == 'AXTextArea')
    assert element['value_truncated'] is True and element['value_sha256'] == value_digest('x' * 70_000)


def test_a_restarted_app_and_a_missing_grant_refuse_before_any_ax_call():
    window, *_ = doc_window()
    ax = FakeAX([window])
    with pytest.raises(AXError) as exc:
        MacBackend(ax, start_of=lambda pid: 2000.0).windows(42, 1000.5)
    assert exc.value.code == 'instance_changed' and ax.opened == 0
    with pytest.raises(AXError) as exc:
        MacBackend(ax, start_of=lambda pid: None).windows(42, 1000.5)
    assert exc.value.code == 'instance_changed'
    untrusted = FakeAX([window], trusted=False)
    with pytest.raises(AXError) as exc:
        backend(untrusted).windows(42, 1000.5)
    assert exc.value.code == 'not_trusted' and 'Accessibility' in exc.value.detail and untrusted.opened == 0
    assert backend(untrusted).available is False and backend(ax).available is True


def test_a_window_is_found_again_by_title_after_the_order_changes_but_never_guessed():
    first, *_ = doc_window('one.txt')
    second, *_ = doc_window('two.txt')
    ax = FakeAX([first, second])
    mac = backend(ax)
    ref = mac.windows(42, 1000.5)[1]['window_ref']
    ax.windows = [second, first]  # the app reordered its windows
    assert mac.tree(42, 1000.5, ref, 300)['window']['title'] == 'two.txt'
    twin, *_ = doc_window('two.txt')
    ax.windows = [first, twin, second]
    with pytest.raises(AXError) as exc:
        mac.tree(42, 1000.5, ref.replace('w1.', 'w0.'), 300)
    assert exc.value.code == 'not_found'
    ax.windows = [first]
    with pytest.raises(AXError) as exc:
        mac.tree(42, 1000.5, ref, 300)
    assert exc.value.code == 'not_found'
    with pytest.raises(AXError) as exc:
        mac.tree(42, 1000.5, 'window-1', 300)
    assert exc.value.code == 'invalid'


def test_limits_hold_elements_depth_and_time():
    chain = Element('AXGroup')
    node = chain
    for _ in range(ax_worker.MAX_DEPTH + 5):
        child = Element('AXGroup')
        node.kids = [child]
        node = child
    wide = Element('AXGroup', kids=[Element('AXStaticText', value=f'row {i}') for i in range(50)])
    window = Element('AXWindow', title='limits.txt', kids=[wide, chain])
    mac = backend(FakeAX([window]))
    ref = mac.windows(42, 1000.5)[0]['window_ref']
    snap = mac.tree(42, 1000.5, ref, 10)
    assert len(snap['elements']) == 10 and snap['truncated'] is True
    deep = backend(FakeAX([Element('AXWindow', title='deep.txt', kids=[chain])]))
    snap = deep.tree(42, 1000.5, deep.windows(42, 1000.5)[0]['window_ref'], 300)
    assert len(snap['elements']) == ax_worker.MAX_DEPTH and snap['truncated'] is True
    ticks = iter(range(0, 1000))
    slow = MacBackend(FakeAX([window]), start_of=lambda pid: 1000.5, clock=lambda: next(ticks))
    snap = slow.tree(42, 1000.5, ref, 300)
    assert snap['truncated'] is True and len(snap['elements']) <= ax_worker.TREE_SECONDS + 1


def test_an_element_that_cannot_answer_is_left_out_and_an_unknown_window_is_not_found():
    window, *_ = doc_window()
    window.kids.insert(0, Element('AXGroup', busy=True))
    mac = backend(FakeAX([window]))
    snap = mac.tree(42, 1000.5, mac.windows(42, 1000.5)[0]['window_ref'], 300)
    assert [e['path'] for e in snap['elements']][:2] == ['/1', '/2']
    with pytest.raises(AXError) as exc:  # Phase 42: acting works; a window no longer listed is not found
        mac.perform(42, 1000.5, 'w0.' + '0' * 16, '/1', 'press', None)
    assert exc.value.code == 'not_found' and 'another desktop' in exc.value.detail


class Client:
    """WorkerClient's surface, answering through handle() in process."""

    def __init__(self, backend_):
        self.backend, self.closed, self.next = backend_, False, 0

    def call(self, op, args):
        self.next += 1
        response = handle({'id': self.next, 'op': op, 'args': args}, self.backend)
        if not response['ok']:
            raise AXError(response['error'], response.get('detail') or '')
        return response['result']

    def windows(self, pid, start):
        return self.call('windows', {'pid': pid, 'start': start})

    def tree(self, pid, start, window_ref, limit):
        return self.call('tree', {'pid': pid, 'start': start, 'window_ref': window_ref, 'limit': limit})

    def close(self):
        self.closed = True


def test_the_probe_reports_trust_and_the_documents_digest_and_nothing_of_its_text():
    window, *_ = doc_window()
    other, *_ = doc_window('Personal notes.txt', text='private words')
    client = Client(backend(FakeAX([other, window])))
    facts = ax_worker.probe('com.apple.TextEdit', 'e-n2-ab12cd34', client=client,
                            lister=lambda bundle: [{'pid': 42, 'start': 1000.5}])
    # D112 (Phase 43, 1.9.0): hello also names the worker's ops and whether it names windows by number.
    assert facts['hello'] == {'protocol': ax_worker.PROTOCOL, 'backend': 'mac', 'available': True,
                              'ops': list(ax_worker.OPS), 'window_numbers': False}
    assert (facts['running'], facts['windows'], facts['matched']) == (1, 2, 1)
    assert facts['text_areas'] == [{'value_sha256': value_digest('Pebble E-N2 marker 7f3a'), 'value_truncated': False,
                                    'secure': False}]
    assert facts['roles']['AXTextField'] == 2 and client.closed
    text = json.dumps(facts)
    assert 'private' not in text and 'Personal' not in text and 'marker' not in text and 'hunter2' not in text


def test_the_probe_reports_a_refusal_as_a_code():
    window, *_ = doc_window()
    client = Client(backend(FakeAX([window], trusted=False)))
    facts = ax_worker.probe('com.apple.TextEdit', 'e-n2-ab12cd34', client=client,
                            lister=lambda bundle: [{'pid': 42, 'start': 1000.5}])
    assert facts['hello']['available'] is False and facts['error']['code'] == 'not_trusted'
    for bad in ('short', 'x' * 65, 'line\nbreak'):
        with pytest.raises(AXError):
            ax_worker.probe('com.apple.TextEdit', bad, client=client, lister=lambda bundle: [])


def test_the_worker_serves_the_mac_backend_only_when_asked(monkeypatch):
    served = []
    monkeypatch.setattr(ax_worker, 'serve', lambda rfile, wfile, backend_: served.append(backend_.name))
    monkeypatch.setattr(ax_worker.sys, 'stdout', ax_worker.sys.stdout)
    import resource
    monkeypatch.setattr(resource, 'setrlimit', lambda *args: None)
    ax_worker.worker_main()
    ax_worker.worker_main('mac')
    assert served == ['blocked', 'mac']


# ---- the real bindings, on CoreFoundation values made here ------------------------------------------------------

mac_only = pytest.mark.skipif(sys.platform != 'darwin', reason='CoreFoundation is macOS only')


@mac_only
def test_mac_ax_converts_real_cf_values():
    import ctypes
    ax = ax_worker.MacAX()
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    services = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
    cf.CFNumberCreate.restype, cf.CFNumberCreate.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int,
                                                                               ctypes.c_void_p]
    services.AXValueCreate.restype = ctypes.c_void_p
    services.AXValueCreate.argtypes = [ctypes.c_int, ctypes.c_void_p]
    ax.arena = []
    made = []

    def keep(ref):
        made.append(ref)
        return ref
    try:
        text = keep(ax.make_string(None, 'Pebble \N{EM DASH} E-N2'.encode(), ax_worker.UTF8))
        assert ax.python(text) == 'Pebble \N{EM DASH} E-N2'
        assert ax.text(text, 6) == ('Pebble', True)
        whole = ctypes.c_int64(42)
        assert ax.python(keep(cf.CFNumberCreate(None, 4, ctypes.byref(whole)))) == 42
        half = ctypes.c_double(0.5)
        assert ax.python(keep(cf.CFNumberCreate(None, 13, ctypes.byref(half)))) == 0.5
        assert ax.python(ctypes.c_void_p.in_dll(cf, 'kCFBooleanTrue').value) is True
        point = (ctypes.c_double * 2)(12.5, 30.0)
        assert ax.python(keep(services.AXValueCreate(1, point))) == [12.5, 30.0]
        size = (ctypes.c_double * 2)(800.0, 600.0)
        assert ax.python(keep(services.AXValueCreate(2, size))) == [800.0, 600.0]
        items = (ctypes.c_void_p * 1)(text)
        array = keep(ax.make_array(None, items, 1, ax.callbacks))
        assert ax.python(array) == [], 'an array keeps only element handles'
        assert ax.python(None) is None
        assert isinstance(ax.trusted(), bool)
    finally:
        ax.close()
        for ref in made:
            ax.release(ref)


@mac_only
def test_the_responsible_app_of_this_process_is_read():
    import os
    owner = ax_worker.responsible_app(os.getpid())
    assert owner is None or (owner['pid'] > 0 and (owner['app'] is None or '.app/' in owner['app']))


@mac_only
def test_the_probe_reports_which_app_macos_holds_responsible_for_its_worker():
    import os
    window, *_ = doc_window()
    client = Client(backend(FakeAX([window])))
    client.proc = type('Proc', (), {'pid': os.getpid()})()
    facts = ax_worker.probe('com.apple.TextEdit', 'e-n2-ab12cd34', client=client,
                            lister=lambda bundle: [{'pid': 42, 'start': 1000.5}])
    owner = ax_worker.responsible_app(os.getpid())
    assert facts.get('responsible') == (owner and {**owner, 'is_this_backends_parent': owner['pid'] == os.getppid()})


@mac_only
def test_mac_ax_without_trust_refuses_reads_as_not_trusted():
    ax = ax_worker.MacAX()
    import os
    app = ax.open(os.getpid())
    try:
        if ax.trusted():
            # Phase 47 (2.0.0's trial allows no skip): the process running the suite is trusted for Accessibility on
            # this Mac (its terminal was allowed for the native checks), so the same read is never refused as not_trusted.
            try:
                ax.attributes(app, ('AXWindows',))
            except AXError as error:
                assert error.code != 'not_trusted', error.code
            return
        with pytest.raises(AXError) as exc:
            ax.attributes(app, ('AXWindows',))
        assert exc.value.code == 'not_trusted'
    finally:
        ax.close()


# ---- acting (Phase 42, 1.8.0) -----------------------------------------------------------------------------------

def observed(mac, path):
    """The host's view of the element at path: its sanitized facts, role and label digest."""
    rows = mac.windows(42, 1000.5)
    snap = handle({'id': 9, 'op': 'tree', 'args': {'pid': 42, 'start': 1000.5, 'window_ref': rows[0]['window_ref'],
                                                   'limit': 300}}, mac)['result']
    element = next(e for e in snap['elements'] if e['path'] == path)
    return rows[0]['window_ref'], {'role': element['role'], 'label_sha256': ax_worker.label_sha256(element)}


def test_a_press_reaches_the_element_at_its_path_once():
    window, *_ = doc_window()
    ax = FakeAX([window])
    mac = backend(ax)
    ref, expect = observed(mac, '/0')
    mac.perform(42, 1000.5, ref, '/0', 'press', None, expect=expect)
    assert ax.acts == [(window.kids[0], 'AXPress')] and ax.sets == []


def test_an_element_that_changed_since_it_was_observed_is_not_acted_on():
    window, *_ = doc_window()
    ax = FakeAX([window])
    mac = backend(ax)
    ref, expect = observed(mac, '/0')
    window.kids[0].attrs['AXDescription'] = 'delete everything'
    with pytest.raises(AXError) as exc:
        mac.perform(42, 1000.5, ref, '/0', 'press', None, expect=expect)
    assert exc.value.code == 'not_found' and ax.acts == []
    window.kids.insert(0, Element('AXButton', title='Buy now'))  # the path now names another element
    with pytest.raises(AXError):
        mac.perform(42, 1000.5, ref, '/0', 'press', None, expect=expect)
    assert ax.acts == []


def test_a_secure_field_is_never_acted_on():
    window, area, password, secret = doc_window()
    ax = FakeAX([window])
    mac = backend(ax)
    ref = mac.windows(42, 1000.5)[0]['window_ref']
    for path in ('/2', '/3'):
        with pytest.raises(AXError) as exc:
            mac.perform(42, 1000.5, ref, path, 'type', 'hunter3')
        assert exc.value.code == 'unsupported'
    assert ax.sets == [] and ax.acts == [] and password not in ax.value_reads


def test_typing_replaces_the_value_and_focus_on_the_window_raises_it():
    window, area, *_ = doc_window()
    ax = FakeAX([window])
    mac = backend(ax)
    ref, expect = observed(mac, '/1/0')
    mac.perform(42, 1000.5, ref, '/1/0', 'type', 'New text', expect=expect)
    mac.perform(42, 1000.5, ref, ax_worker.WINDOW_PATH, 'focus', None)
    assert ax.sets == [(area, 'AXValue', 'New text')] and ax.acts == [(window, 'AXRaise')]
    with pytest.raises(AXError) as exc:  # a button takes no text
        mac.perform(42, 1000.5, ref, '/0', 'type', 'x')
    assert exc.value.code == 'unsupported'
    with pytest.raises(AXError) as exc:
        mac.perform(42, 1000.5, ref, ax_worker.WINDOW_PATH, 'press', None)
    assert exc.value.code == 'unsupported'


def test_an_app_that_does_not_answer_makes_the_act_uncertain_and_unsupported_actions_say_so():
    window, *_ = doc_window()
    ax = FakeAX([window])
    mac = backend(ax)
    ref = mac.windows(42, 1000.5)[0]['window_ref']
    window.kids[0].code = ax_worker.AX_CANNOT_COMPLETE
    with pytest.raises(AXError) as exc:
        mac.perform(42, 1000.5, ref, '/0', 'press', None)
    assert exc.value.code == 'uncertain'
    window.kids[0].code = -25206
    with pytest.raises(AXError) as exc:
        mac.perform(42, 1000.5, ref, '/0', 'press', None)
    assert exc.value.code == 'unsupported'
    with pytest.raises(AXError) as exc:
        mac.perform(42, 1000.5, ref, '/9', 'press', None)
    assert exc.value.code == 'not_found'
    with pytest.raises(AXError) as exc:
        mac.perform(42, 1000.5, ref, '../0', 'press', None)
    assert exc.value.code == 'invalid'


def test_the_protocol_checks_the_expectation_and_passes_it_on():
    window, *_ = doc_window()
    ax = FakeAX([window])
    mac = backend(ax)
    ref, expect = observed(mac, '/0')
    base = {'pid': 42, 'start': 1000.5, 'window_ref': ref, 'path': '/0', 'action': 'press', 'text': None}
    for bad in ({'role': 'AXButton'}, {'role': 'button', 'label_sha256': expect['label_sha256']},
                {'role': 'AXButton', 'label_sha256': 'x' * 64}, 'AXButton'):
        answer = handle({'id': 1, 'op': 'perform', 'args': {**base, 'expect': bad}}, mac)
        assert answer['ok'] is False and answer['error'] == 'invalid', bad
    assert ax.acts == []
    assert handle({'id': 2, 'op': 'perform', 'args': {**base, 'expect': expect}}, mac)['ok'] is True
    assert ax.acts == [(window.kids[0], 'AXPress')]


@mac_only
def test_production_uses_the_mac_worker_and_the_default_worker_stays_blocked():
    client = ax_worker.production_backend()
    assert isinstance(client, ax_worker.WorkerClient) and client.argv[-3:] == ['--ax-worker', '--ax-backend', 'mac']
    assert client.checks_expect is True and getattr(ax_worker.BlockedBackend(), 'checks_expect', False) is False
    assert ax_worker.worker_argv()[-1] == '--ax-worker'


def test_the_apps_own_focused_window_is_its_front_one_while_the_app_is_in_the_background():
    # Phase 43 (1.9.0): Comet in the background had several windows and none said AXFocused (1.8.0's dead end).
    first, *_ = doc_window('Watch something — Audio playing')
    second, *_ = doc_window('YouTube')
    ax = FakeAX([first, second])
    mac = backend(ax)
    assert [r['focused'] for r in mac.windows(42, 1000.5)] == [True, True], 'per-window flags alone (both say AXMain)'
    ax.front = second  # the app's own record decides, over the per-window flags
    assert [r['focused'] for r in mac.windows(42, 1000.5)] == [False, True]
