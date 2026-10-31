"""Site search for OpenURL (Phase 43, 1.9.0): a site's own search address, built from the person's words.

Decision 9 of the 1.9.0 design, the direct route before the screen: OpenURL takes `site` and `query` as well as `url`,
and the host builds the address of the site's own search results, so "search YouTube for this song" is one reviewed
open instead of a dozen screen steps. Pebble only asks macOS to open the address; nothing here sees the page.

Where the address comes from, in this order:
1. A vetted table for well-known sites (YouTube and YouTube Music, Google, DuckDuckGo, Bing, Wikipedia, GitHub,
   Reddit, Stack Overflow, Amazon's stores, Apple Maps and Google Maps), with no fetch. The design names OpenSearch
   first; the table is consulted first on purpose, so no network round trip comes before the person sees the card
   (its addresses are the ones those sites' own search forms use).
2. Else the site's OpenSearch description: the home page's `<link rel="search"
   type="application/opensearchdescription+xml">`, fetched through sources.fetch_response (public https only, every
   redirect checked again). The home page's final address, the description and the template's host must all lie on
   the site's registrable domain; the template is a GET for text/html holding {searchTerms}; optional parameters are
   dropped and the core required ones get their defaults; anything else refuses. Kept per origin in memory (an hour;
   a failure ten minutes; at most CACHE_LIMIT origins).

The query is 1-300 characters without control characters, percent-encoded into the template; nothing else of the
person's text is sent. PEBBLE_SITE_SEARCH=0 turns site search off (OpenURL then takes `url` only).
"""

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urljoin, urlsplit

from .policy import PolicyError
from .research import registrable_domain

MAX_QUERY = 300
MAX_SITE = 253
PAGE_BYTES = 262_144
DESCRIPTION_BYTES = 65_536
FETCH_SECONDS = 6
CACHE_LIMIT = 64
CACHE_SECONDS = 3600.0
MISS_SECONDS = 600.0
CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f]')
HOSTNAME = re.compile(r'(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]')
WIKI_HOST = re.compile(r'(?:[a-z]{2,3}(?:-[a-z]{2,8})?)(?:\.m)?\.wikipedia\.org')
OPENSEARCH_TYPE = 'application/opensearchdescription+xml'
PAGE_TYPES = ('text/html', 'application/xhtml+xml')
PARAM = re.compile(r'\{([^{}]{1,64})\}')
# OpenSearch 1.1's core parameters a template may require, with the value a single search page uses.
DEFAULTS = {'inputEncoding': 'UTF-8', 'outputEncoding': 'UTF-8', 'language': '*', 'count': '20', 'startIndex': '1',
            'startPage': '1'}

# The vetted table: the names a person says, the exact hosts that select an entry (checked first), the registrable
# domain that selects it, and the address with {q} for the query ('path' entries carry it in the path).
TABLE = (
    {'site': 'youtube.com', 'names': ('youtube', 'yt'), 'template': 'https://www.youtube.com/results?search_query={q}'},
    {'site': 'music.youtube.com', 'names': ('youtube music', 'yt music'), 'hosts': ('music.youtube.com',),
     'template': 'https://music.youtube.com/search?q={q}'},
    {'site': 'google.com', 'names': ('google', 'google search'), 'template': 'https://www.google.com/search?q={q}'},
    {'site': 'duckduckgo.com', 'names': ('duckduckgo', 'duck duck go', 'ddg'), 'template': 'https://duckduckgo.com/?q={q}'},
    {'site': 'bing.com', 'names': ('bing',), 'template': 'https://www.bing.com/search?q={q}'},
    # Phase 45 (F128): go=Go opens the article whose title matches instead of the results ("Pebble (watch)")
    {'site': 'wikipedia.org', 'names': ('wikipedia', 'wiki'),
     'template': 'https://en.wikipedia.org/w/index.php?search={q}&go=Go'},
    {'site': 'github.com', 'names': ('github',), 'template': 'https://github.com/search?q={q}'},
    {'site': 'reddit.com', 'names': ('reddit',), 'template': 'https://www.reddit.com/search/?q={q}'},
    {'site': 'stackoverflow.com', 'names': ('stack overflow', 'stackoverflow'),
     'template': 'https://stackoverflow.com/search?q={q}'},
    {'site': 'maps.apple.com', 'names': ('apple maps', 'maps'), 'hosts': ('maps.apple.com',),
     'template': 'https://maps.apple.com/?q={q}'},
    {'site': 'google.com/maps', 'names': ('google maps',), 'hosts': ('maps.google.com',),
     'template': 'https://www.google.com/maps/search/{q}', 'path': True},
)
# Amazon's stores: the store the model names (a bare "Amazon" is amazon.com); each searches at /s?k=.
AMAZON = ('amazon.com', 'amazon.in', 'amazon.co.uk', 'amazon.de', 'amazon.fr', 'amazon.it', 'amazon.es', 'amazon.ca',
          'amazon.com.au', 'amazon.co.jp', 'amazon.com.br', 'amazon.com.mx', 'amazon.nl', 'amazon.se', 'amazon.pl',
          'amazon.sg', 'amazon.ae', 'amazon.sa', 'amazon.com.tr', 'amazon.eg', 'amazon.com.be')

_cache, _cache_lock = OrderedDict(), threading.Lock()


def enabled():
    return os.environ.get('PEBBLE_SITE_SEARCH', '1').strip() != '0'


def clean_query(query):
    """The query with its whitespace collapsed, or PolicyError."""
    if not isinstance(query, str) or CONTROL.search(query.replace('\n', ' ').replace('\t', ' ')):
        raise PolicyError('Give the search words as text without control characters.')
    text = ' '.join(query.split())
    if not 1 <= len(text) <= MAX_QUERY:
        raise PolicyError(f'Give 1 to {MAX_QUERY} characters of search words.')
    return text


def _host(text):
    """The lower-case ASCII host named by a site address, a host or a host with a path; None when there is none."""
    if '://' in text:
        try:
            parts = urlsplit(text)
            port = parts.port
        except ValueError:
            return None
        if parts.scheme not in ('https', 'http') or parts.username or parts.password or port not in (None, 443, 80):
            return None
        host = parts.hostname or ''
    else:
        host = text.split('/', 1)[0]
        if ':' in host or '@' in host:
            return None
    try:
        host = host.rstrip('.').encode('idna').decode('ascii').lower()
    except (UnicodeError, ValueError):
        return None
    return host if HOSTNAME.fullmatch(host) else None


def _entry(site):
    """(table entry or None, host or None) for the site the model named."""
    if not isinstance(site, str) or not site.strip() or len(site) > MAX_SITE or CONTROL.search(site):
        raise PolicyError('Name the site by its name or domain, for example YouTube or example.com.')
    name = ' '.join(site.split()).casefold()
    for entry in TABLE:
        if name in entry['names']:
            return entry, None
    if name == 'amazon':
        return {'site': 'amazon.com', 'template': 'https://www.amazon.com/s?k={q}'}, None
    host = _host(name)
    if host is None:
        raise PolicyError('Name the site by its domain, for example example.com; only public sites can be searched.')
    for entry in TABLE:
        if host in entry.get('hosts', ()):
            return entry, host
    domain = registrable_domain(host)
    if domain in AMAZON:
        return {'site': domain, 'template': 'https://www.' + domain + '/s?k={q}'}, host
    if domain == 'wikipedia.org' and WIKI_HOST.fullmatch(host) and not host.startswith('www.'):
        language = host.replace('.m.', '.')
        return {'site': 'wikipedia.org', 'template': 'https://' + language + '/w/index.php?search={q}&go=Go'}, host
    for entry in TABLE:
        if not entry.get('hosts') and entry['site'] == domain:
            return entry, host
    return None, host


def _fill(entry, query):
    value = quote(query, safe='') if entry.get('path') else quote_plus(query, safe='')
    return entry['template'].replace('{q}', value)


class _SearchLink(HTMLParser):
    """The first <link rel="search" type="application/opensearchdescription+xml" href> of a page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.href = None

    def handle_starttag(self, tag, attrs):
        if self.href is not None or tag != 'link':
            return
        values = {k.lower(): (v or '') for k, v in attrs if isinstance(k, str)}
        if 'search' in values.get('rel', '').lower().split() and \
                values.get('type', '').split(';')[0].strip().lower() == OPENSEARCH_TYPE and values.get('href'):
            self.href = values['href'].strip()

    handle_startendtag = handle_starttag


def _text(response, *, html):
    from .sources import decode, media_type
    _, declared = media_type(response.get('content_type'))
    return decode(response.get('body') or b'', declared, html=html, final=not response.get('truncated'))[0]


def _same_site(url, domain):
    """Whether url is a public https address (default port, no credentials) on the registrable domain."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or '').rstrip('.').lower()
    return parts.scheme == 'https' and port in (None, 443) and not parts.username and not parts.password \
        and HOSTNAME.fullmatch(host) is not None and registrable_domain(host) == domain


def _template(document, domain):
    """The usable search template of an OpenSearch description, with optional parameters dropped and core ones
    filled; PolicyError when it has none."""
    if re.search(r'<!(?:DOCTYPE|ENTITY)', document, re.I):
        raise PolicyError('The site\'s search description declares a document type, so it is not read.')
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        raise PolicyError('The site\'s search description is not valid XML.') from None
    for element in root.iter():
        if not isinstance(element.tag, str) or element.tag.rsplit('}', 1)[-1] != 'Url':
            continue
        kind = (element.get('type') or '').split(';')[0].strip().lower()
        method = (element.get('method') or 'get').strip().lower()
        template = (element.get('template') or '').strip()
        if kind not in PAGE_TYPES or method != 'get' or '{searchTerms}' not in template or len(template) > 2048:
            continue
        filled = _drop_optional(template)
        if filled is not None and _same_site(filled.replace('{searchTerms}', 'x'), domain):
            return filled
    raise PolicyError('The site does not describe a search address Pebble can use; give the address as url.')


def _drop_optional(template):
    """template with every optional parameter removed (a query pair whose value is one, or the bare marker) and the
    core required parameters filled; None when another parameter is required."""
    head, _, fragment = template.partition('#')
    base, mark, query = head.partition('?')
    pairs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        found = PARAM.fullmatch(value)
        if found and found.group(1).endswith('?'):
            continue
        pairs.append((key, value))
    rebuilt = base + (mark + urlencode(pairs, safe='{}', quote_via=quote) if pairs else '')
    for name in set(PARAM.findall(rebuilt)):
        if name == 'searchTerms':
            continue
        bare = name.rstrip('?')
        if name.endswith('?'):
            rebuilt = rebuilt.replace('{' + name + '}', '')
        elif bare in DEFAULTS:
            rebuilt = rebuilt.replace('{' + name + '}', DEFAULTS[bare])
        else:
            return None
    return rebuilt + ('#' + fragment if fragment and '{' not in fragment else '')


def _cached(origin, now):
    with _cache_lock:
        item = _cache.get(origin)
        if item is None:
            return None
        if item[0] <= now:
            _cache.pop(origin, None)
            return None
        _cache.move_to_end(origin)
        return item


def _keep(origin, expires, template, message):
    with _cache_lock:
        _cache[origin] = (expires, template, message)
        _cache.move_to_end(origin)
        while len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)


def opensearch(host, *, fetch=None, clock=time.monotonic):
    """The site's search template for host (cached), or PolicyError saying why there is none."""
    origin = 'https://' + host
    item = _cached(origin, clock())
    if item is not None:
        if item[1] is None:
            raise PolicyError(item[2])
        return item[1]
    if fetch is None:
        from .sources import fetch_response as fetch
    domain = registrable_domain(host)
    try:
        page = fetch(origin + '/', 'text/html,application/xhtml+xml;q=0.9', max_bytes=PAGE_BYTES, max_redirects=3,
                     prefix_ok=True, seconds=FETCH_SECONDS)
        if not _same_site(page.get('final_url') or '', domain):
            raise PolicyError('The site\'s home page moved to another site, so its search is not used.')
        finder = _SearchLink()
        finder.feed(_text(page, html=True))
        if not finder.href:
            raise PolicyError('The site does not publish a search description; give the address as url.')
        description = urljoin(page['final_url'], finder.href)
        if not _same_site(description, domain):
            raise PolicyError('The site\'s search description is on another site, so it is not used.')
        document = fetch(description, OPENSEARCH_TYPE + ',application/xml;q=0.9,text/xml;q=0.8',
                         max_bytes=DESCRIPTION_BYTES, max_redirects=2, prefix_ok=False, seconds=FETCH_SECONDS)
        if not _same_site(document.get('final_url') or description, domain):
            raise PolicyError('The site\'s search description moved to another site, so it is not used.')
        template = _template(_text(document, html=False), domain)
    except PolicyError as exc:
        _keep(origin, clock() + MISS_SECONDS, None, str(exc))
        raise
    except (ValueError, OSError):
        message = 'The site could not be reached to find its search address; give the address as url.'
        _keep(origin, clock() + MISS_SECONDS, None, message)
        raise PolicyError(message) from None
    _keep(origin, clock() + CACHE_SECONDS, template, None)
    return template


def table_url(site, query):
    """Phase 44 (1.9.1): the vetted table's results address for site and query, or None (never the network): what a
    BrowserRequest opens first when the model names a site and its search words."""
    if not enabled():
        return None
    try:
        entry, _ = _entry(site)
        return _fill(entry, without_site_name(entry, clean_query(query))) if entry is not None else None
    except PolicyError:
        return None


def without_site_name(entry, text):
    """Phase 45 (F128, audit round R4): the model searched Wikipedia for "Pebble (watch) Wikipedia", which matches no
    article. A search on a site leaves out the site's own name (kept when nothing else is left)."""
    words = text
    for name in sorted(entry.get('names', ()), key=len, reverse=True):
        words = re.sub(r'(?i)(?:^|\s+)(?:on\s+|in\s+|at\s+)?' + re.escape(name) + r'(?=\s|$)', ' ', words)
    words = ' '.join(words.split())
    return words or text


def build(site, query, *, fetch=None, clock=time.monotonic):
    """{'url', 'route': 'table'|'opensearch', 'site'}: the address of site's own search results for query."""
    if not enabled():
        raise PolicyError('Site search is off on this Mac; give the page\'s address as url.')
    text = clean_query(query)
    entry, host = _entry(site)
    if entry is not None:
        return {'url': _fill(entry, text), 'route': 'table', 'site': entry['site']}
    template = opensearch(host, fetch=fetch, clock=clock)
    return {'url': template.replace('{searchTerms}', quote(text, safe='')), 'route': 'opensearch',
            'site': registrable_domain(host)}
