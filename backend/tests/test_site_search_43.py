"""Phase 43 (1.9.0): site search for OpenURL: the vetted table first (no fetch), else the site's own OpenSearch
description on its registrable domain. Every fetch here is a fake; nothing touches the network."""
import pytest

from pebble import site_search
from pebble.policy import PolicyError


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.delenv('PEBBLE_SITE_SEARCH', raising=False)
    site_search._cache.clear()
    yield
    site_search._cache.clear()


def no_fetch(*args, **kwargs):
    raise AssertionError('a table site is never fetched')


@pytest.mark.parametrize('site,url,where', [
    ('YouTube', 'https://www.youtube.com/results?search_query=navior+song', 'youtube.com'),
    (' youtube.com ', 'https://www.youtube.com/results?search_query=navior+song', 'youtube.com'),
    ('https://m.youtube.com/watch?v=x', 'https://www.youtube.com/results?search_query=navior+song', 'youtube.com'),
    ('YouTube Music', 'https://music.youtube.com/search?q=navior+song', 'music.youtube.com'),
    ('music.youtube.com', 'https://music.youtube.com/search?q=navior+song', 'music.youtube.com'),
    ('google', 'https://www.google.com/search?q=navior+song', 'google.com'),
    ('DuckDuckGo', 'https://duckduckgo.com/?q=navior+song', 'duckduckgo.com'),
    ('bing.com', 'https://www.bing.com/search?q=navior+song', 'bing.com'),
    # Phase 45 (F128): go=Go opens an article whose title matches instead of the results
    ('Wikipedia', 'https://en.wikipedia.org/w/index.php?search=navior+song&go=Go', 'wikipedia.org'),
    ('de.wikipedia.org', 'https://de.wikipedia.org/w/index.php?search=navior+song&go=Go', 'wikipedia.org'),
    ('en.m.wikipedia.org', 'https://en.wikipedia.org/w/index.php?search=navior+song&go=Go', 'wikipedia.org'),
    ('www.wikipedia.org', 'https://en.wikipedia.org/w/index.php?search=navior+song&go=Go', 'wikipedia.org'),
    ('GitHub', 'https://github.com/search?q=navior+song', 'github.com'),
    ('reddit', 'https://www.reddit.com/search/?q=navior+song', 'reddit.com'),
    ('Stack Overflow', 'https://stackoverflow.com/search?q=navior+song', 'stackoverflow.com'),
    ('Amazon', 'https://www.amazon.com/s?k=navior+song', 'amazon.com'),
    ('amazon.in', 'https://www.amazon.in/s?k=navior+song', 'amazon.in'),
    ('www.amazon.co.uk', 'https://www.amazon.co.uk/s?k=navior+song', 'amazon.co.uk'),
    ('Maps', 'https://maps.apple.com/?q=navior+song', 'maps.apple.com'),
    ('Apple Maps', 'https://maps.apple.com/?q=navior+song', 'maps.apple.com'),
    ('Google Maps', 'https://www.google.com/maps/search/navior%20song', 'google.com/maps'),
])
def test_table_sites_build_their_own_search_address_without_a_fetch(site, url, where):
    assert site_search.build(site, '  navior   song ', fetch=no_fetch) == {'url': url, 'route': 'table', 'site': where}


def test_the_query_is_percent_encoded_and_bounded():
    built = site_search.build('YouTube', 'আমার সোনার বাংলা & more #1', fetch=no_fetch)
    assert built['url'].startswith('https://www.youtube.com/results?search_query=%E0%A6%86')
    assert '&more' not in built['url'] and '#' not in built['url'] and '%26' in built['url'] and '%231' in built['url']
    for bad in ('', '   ', 'x' * 301, 'a\x00b', 'a\x1bb', None, 42):
        with pytest.raises(PolicyError):
            site_search.build('YouTube', bad, fetch=no_fetch)


@pytest.mark.parametrize('site', ['localhost', '127.0.0.1', 'example', 'ftp://example.com', 'https://example.com:8443',
                                  'user@example.com', '', None, 'x' * 300, 'exa\x00mple.com', '[::1]'])
def test_only_public_sites_by_name_or_domain_are_searched(site):
    with pytest.raises(PolicyError):
        site_search.build(site, 'song', fetch=no_fetch)


def test_the_switch_turns_site_search_off(monkeypatch):
    monkeypatch.setenv('PEBBLE_SITE_SEARCH', '0')
    assert site_search.enabled() is False
    with pytest.raises(PolicyError, match='off'):
        site_search.build('YouTube', 'song', fetch=no_fetch)


HOME = (b'<html><head><title>Films</title><link rel="stylesheet" href="/a.css">'
        b'<link rel="search" type="application/opensearchdescription+xml" title="Films" href="/opensearch.xml">'
        b'</head><body>hi</body></html>')
DESCRIPTION = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
               '<ShortName>Films</ShortName>'
               '<Url type="application/x-suggestions+json" template="https://www.films.example.org/s?q={searchTerms}"/>'
               '<Url type="text/html" method="post" template="https://www.films.example.org/post?q={searchTerms}"/>'
               '<Url type="text/html" template="https://www.films.example.org/find?q={searchTerms}&amp;p={startPage?}'
               '&amp;enc={inputEncoding}"/>'
               '</OpenSearchDescription>')


class Fetcher:
    """Answers the home page and the description; records every request."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, accept, **kwargs):
        self.calls.append((url, kwargs))
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        body, kind, final = page
        return {'body': body if isinstance(body, bytes) else body.encode(), 'content_type': kind,
                'final_url': final or url, 'truncated': False}


def site(pages):
    return Fetcher({'https://films.example.org/': (HOME, 'text/html; charset=utf-8', 'https://www.films.example.org/'),
                    'https://www.films.example.org/opensearch.xml': (DESCRIPTION, OPENSEARCH, None), **pages})


OPENSEARCH = 'application/opensearchdescription+xml'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_other_sites_use_their_own_opensearch_description_and_it_is_kept():
    fetch, clock = site({}), Clock()
    built = site_search.build('films.example.org', 'the third man', fetch=fetch, clock=clock)
    assert built == {'url': 'https://www.films.example.org/find?q=the%20third%20man&enc=UTF-8', 'route': 'opensearch',
                     'site': 'example.org'}
    assert [c[0] for c in fetch.calls] == ['https://films.example.org/', 'https://www.films.example.org/opensearch.xml']
    assert fetch.calls[0][1]['max_bytes'] == site_search.PAGE_BYTES and fetch.calls[1][1]['prefix_ok'] is False
    again = site_search.build('https://films.example.org/about', 'brief encounter', fetch=fetch, clock=clock)
    assert again['url'].endswith('q=brief%20encounter&enc=UTF-8') and len(fetch.calls) == 2, 'kept for the hour'
    clock.now += site_search.CACHE_SECONDS + 1
    site_search.build('films.example.org', 'brief encounter', fetch=fetch, clock=clock)
    assert len(fetch.calls) == 4, 'fetched again after the hour'


@pytest.mark.parametrize('pages,needle', [
    ({'https://films.example.org/': (HOME, 'text/html', 'https://elsewhere.example.net/')}, 'another site'),
    ({'https://films.example.org/': (b'<html><head></head></html>', 'text/html', None)}, 'does not publish'),
    ({'https://films.example.org/': (HOME.replace(b'/opensearch.xml', b'https://cdn.example.net/os.xml'), 'text/html',
                                     None)}, 'on another site'),
    ({'https://www.films.example.org/opensearch.xml': (DESCRIPTION.replace('https://www.films.example.org/find',
                                                                           'https://evil.example.net/find'), OPENSEARCH,
                                                       None)}, 'does not describe'),
    ({'https://www.films.example.org/opensearch.xml': ('<!DOCTYPE x [<!ENTITY a "b">]>' + DESCRIPTION, OPENSEARCH,
                                                       None)}, 'document type'),
    ({'https://www.films.example.org/opensearch.xml': ('<OpenSearchDescription', OPENSEARCH, None)}, 'not valid XML'),
    ({'https://www.films.example.org/opensearch.xml': (DESCRIPTION.replace('{inputEncoding}', '{moz:mystery}'),
                                                       OPENSEARCH, None)}, 'does not describe'),
    ({'https://www.films.example.org/opensearch.xml': (DESCRIPTION.replace('https://www.films.example.org/find',
                                                                           'http://www.films.example.org/find'),
                                                       OPENSEARCH, None)}, 'does not describe'),
    ({'https://films.example.org/': ValueError('Source returned HTTP 503.')}, 'could not be reached'),
])
def test_a_description_off_the_site_or_unusable_refuses_and_the_refusal_is_kept(pages, needle):
    fetch, clock = site(pages), Clock()
    with pytest.raises(PolicyError, match=needle):
        site_search.build('films.example.org', 'the third man', fetch=fetch, clock=clock)
    calls = len(fetch.calls)
    with pytest.raises(PolicyError, match=needle):
        site_search.build('films.example.org', 'the third man', fetch=fetch, clock=clock)
    assert len(fetch.calls) == calls, 'a failure is kept for ten minutes'
    clock.now += site_search.MISS_SECONDS + 1
    with pytest.raises(PolicyError):
        site_search.build('films.example.org', 'the third man', fetch=fetch, clock=clock)
    assert len(fetch.calls) > calls


def test_the_cache_is_bounded():
    clock = Clock()
    for i in range(site_search.CACHE_LIMIT + 5):
        site_search._keep(f'https://s{i}.example.org', clock() + 10, 'https://s.example.org/?q={searchTerms}', None)
    assert len(site_search._cache) == site_search.CACHE_LIMIT and 'https://s0.example.org' not in site_search._cache
