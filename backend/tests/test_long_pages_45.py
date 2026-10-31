"""Phase 45 (1.9.7, F132 and F128 from audit round R4): on a long page what is in view is observed first (a link far
down was never seen, whatever was scrolled into view); a site search leaves out the site's own name; "open the article"
asks for pressing."""
import pytest

from pebble import screen_session as ss
from pebble import site_search
from pebble.ax_worker import MacBackend

from test_ax_mac_42 import Element
from test_ax_mac_43 import FakeAX43, backend


def page_window(scrolled):
    """A Comet-like window (640 x 480 at the origin) holding a web area whose article runs far below the window. With
    scrolled, the "Pebble Time" link is in view and the top of the article is above it."""
    top_y, link_y = (-3000.0, 200.0) if scrolled else (40.0, 3200.0)
    intro = [Element('AXStaticText', value=f'intro line {i}', AXPosition=[10.0, top_y + 20.0 * i], AXSize=[600.0, 18.0])
             for i in range(12)]
    head = Element('AXGroup', title='intro', kids=intro, AXPosition=[0.0, top_y], AXSize=[640.0, 260.0])
    link = Element('AXLink', title='Pebble Time', AXURL='https://en.wikipedia.org/wiki/Pebble_Time',
                   AXPosition=[20.0, link_y], AXSize=[90.0, 18.0])
    section = Element('AXGroup', title='successors', kids=[link], AXPosition=[0.0, link_y - 10.0], AXSize=[640.0, 40.0])
    web = Element('AXWebArea', title='Pebble (watch) - Wikipedia', kids=[head, section],
                  AXPosition=[0.0, 0.0], AXSize=[640.0, 480.0])
    return Element('AXWindow', title='Pebble (watch) - Wikipedia', number=303, kids=[web],
                   AXPosition=[0.0, 0.0], AXSize=[640.0, 480.0])


def titles(snapshot):
    return [e.get('title') or e.get('value') for e in snapshot['elements']]


@pytest.mark.parametrize('scrolled', [False, True])
def test_what_is_in_view_comes_first_on_a_long_page(scrolled):
    window = page_window(scrolled)
    mac = backend(FakeAX43([window]))
    ref = mac.windows(42, 1000.5)[0]['window_ref']
    snap = mac.tree(42, 1000.5, ref, 6, page_first=True)
    names = titles(snap)
    if scrolled:
        assert names[:3] == ['Pebble (watch) - Wikipedia', 'successors', 'Pebble Time'], names
    else:
        assert names[:3] == ['Pebble (watch) - Wikipedia', 'intro', 'intro line 0'] and 'Pebble Time' not in names
    full = titles(mac.tree(42, 1000.5, ref, 300, page_first=True))
    assert sorted(map(str, full)) == sorted(map(str, titles(mac.tree(42, 1000.5, ref, 300)))), 'nothing is lost'


def test_app_windows_keep_reading_order():
    window = page_window(True)
    mac = backend(FakeAX43([window]))
    ref = mac.windows(42, 1000.5)[0]['window_ref']
    names = titles(mac.tree(42, 1000.5, ref, 300))  # not page_first: an app window
    assert names.index('intro') < names.index('Pebble Time')


@pytest.mark.parametrize('site, query, words', [
    ('wikipedia', 'Pebble (watch) Wikipedia', 'Pebble+%28watch%29'),
    ('wikipedia', 'on Wikipedia Pebble smartwatch', 'Pebble+smartwatch'),
    ('youtube', 'navior song youtube', 'navior+song'),
    ('wikipedia', 'Wikipedia', 'Wikipedia'),
])
def test_a_site_search_leaves_out_the_sites_own_name(site, query, words):
    url = site_search.table_url(site, query)
    assert ('search=' + words + '&') in url or url.endswith('search_query=' + words), url


@pytest.mark.parametrize('text, press', [
    ('On Wikipedia, open the article about the Pebble smartwatch', True),
    ('Open that video and watch it', True),
    ('open the first result', True),
    ('Open the docs site', False),
    ('Open YouTube', False),
])
def test_opening_an_article_or_result_asks_for_pressing(text, press):
    assert bool(ss.PRESS_INTENT.search(text)) is press
