"""OpenURL (Phase 43, 1.9.0): open a web address in the person's browser, the direct route.

The person's first app-control task in 1.8.0 (Comet: open YouTube, search a song and play it) took 28 steps and failed,
where one link would have done it: OpenApp takes no address, the Shell's sandbox denies LaunchServices, and Pebble's
own browser opens tabs only for the person. OpenURL asks macOS to open one http or https address in the person's
default browser or in a named installed browser (an app that declares the http and https URL schemes). It only
opens the page: Pebble cannot see it, so nothing here is evidence of what the page did.

Checks: the address is http or https, has a host and no user name or password, and is at most 2,048 characters with
no spaces or control characters; the browser's identity is pinned when proposed and checked again before opening; an
address built from untrusted text (a page, a file, a tool's output) or carrying words of the person's private files
always waits for the person, in every mode.

Site search (M1, decision 9): instead of `url`, OpenURL takes `site` and `query`, and site_search builds the address
of that site's own search results (its vetted table, else its OpenSearch description). The built address passes every
check above. The search words must be the person's own words in this task (each of them appears in their message);
otherwise, or when they carry untrusted text or private words, the open waits for the person.
"""

import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .policy import PolicyError

OPEN = '/usr/bin/open'
MAX_URL = 2048
SCHEMES = ('http', 'https')
# A request to open, play, watch or visit something; a question alone never opens a page (as OpenApp's rule).
OPEN_INTENT = re.compile(r'\b(open|launch|start|play|watch|visit|go to|browse|navigate|show me|pull up|load)\b', re.I)
CONTROL = re.compile(r'[\x00-\x20\x7f]')


def validate(url):
    """The address as given when it may be opened, else PolicyError naming the rule."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL or CONTROL.search(url):
        raise PolicyError('Give one web address of at most 2,048 characters, without spaces.')
    parts = urlsplit(url)
    if parts.scheme.lower() not in SCHEMES or not parts.hostname:
        raise PolicyError('OpenURL opens http and https addresses only.')
    if parts.username is not None or parts.password is not None:
        raise PolicyError('An address with a user name or password is never opened.')
    return url


def _declares_web(path):
    try:
        info = plistlib.loads((Path(path) / 'Contents/Info.plist').read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    schemes = {s.lower() for entry in info.get('CFBundleURLTypes') or () if isinstance(entry, dict)
               for s in entry.get('CFBundleURLSchemes') or () if isinstance(s, str)}
    return {'http', 'https'} <= schemes


def browser(name, *, inventory=None, pin=None):
    """The pinned identity of the installed browser called name (its display name or bundle ID, case-insensitive), or
    None for the person's default browser. PolicyError when no such browser is installed."""
    if name is None or not str(name).strip():
        return None
    if not isinstance(name, str) or len(name) > 100:
        raise PolicyError('Name the browser in at most 100 characters.')
    if inventory is None or pin is None:
        from .native_apps import inventory as inventory_, pin as pin_
        inventory, pin = inventory or (lambda: inventory_(team=False)), pin or pin_
    wanted = ' '.join(name.split()).casefold().removesuffix('.app')
    found = [e for e in inventory() if wanted in {e['name'].casefold(), e['bundle_id'].casefold()}
             and _declares_web(e['id'])]
    if len(found) != 1:
        names = sorted({e['name'] for e in inventory() if _declares_web(e['id'])})[:20]
        raise PolicyError('No single installed browser is called that. Installed browsers: ' + ', '.join(names) + '.')
    return pin(found[0]['id'])


WORD = re.compile(r'\w+')


def own_words(query, person_text):
    """Whether every word of query appears among the words of the person's message (casefolded)."""
    if not isinstance(person_text, str) or not person_text.strip():
        return False
    said = set(WORD.findall(person_text.casefold()))
    words = WORD.findall(query.casefold())
    return bool(words) and all(w in said for w in words)


def proposal(args, *, untrusted=(), private=None, inventory=None, pin=None, person_text=None, search=None):
    """The payload of the reviewed `url` action. requires_review when the address came from untrusted text or carries
    the person's private words, or when a site search's words are not the person's own (person_text: their message
    in this task); otherwise the permission mode decides, as for opening an app."""
    url, site, query = args.get('url'), args.get('site'), args.get('query')
    searching = site is not None or query is not None
    if (url is not None) == searching:
        raise PolicyError('Give either url, or site and query for a site search.')
    if searching and (site is None or query is None):
        raise PolicyError('A site search needs both site and query.')
    route, where = 'url', None
    if searching:
        if search is None:
            from .site_search import build as search
        built = search(site, query)
        url, route, where = built['url'], built['route'], built['site']
        query = ' '.join(query.split())
    url = validate(url)
    why = args.get('why')
    if why is not None and (not isinstance(why, str) or len(why) > 300):
        raise PolicyError('why is one sentence of at most 300 characters.')
    entry = browser(args.get('browser'), inventory=inventory, pin=pin)
    reasons = []
    if searching and not own_words(query, person_text):
        reasons.append('These search words are not your own words in this task, so you decide.')
    from .shell_policy import tainted
    if untrusted and (tainted(url, untrusted) or searching and tainted(query, untrusted)):
        reasons.append('This address came from a page, a file or a tool\'s output, so you decide.')
    leaked = (set(private(url)) | (set(private(query)) if searching else set())) if private else ()
    if leaked:
        reasons.append('This address carries words from files Pebble read, so you decide.')
    payload = {'url': url, 'browser': entry, 'why': ' '.join((why or '').split())[:300], 'reasons': reasons,
               'requires_review': bool(reasons), 'route': route, 'site': where}
    if searching:
        payload['query'] = query
    return payload


def open_url(payload, *, opener=None, pin=None, lister=None, clock=time.monotonic, sleep=time.sleep):
    """Ask macOS to open the address; re-check the browser's identity first. {'opened', 'browser', 'launch_observed',
    'proves'}: proves is always 'open_request_only'."""
    url = validate(payload.get('url'))
    entry = payload.get('browser')
    argv = [OPEN, url]
    if entry is not None:
        if pin is None:
            from .native_apps import pin as pin
        current = pin(entry['id'])
        if (current.get('bundle_id'), current.get('team_id')) != (entry.get('bundle_id'), entry.get('team_id')):
            raise PolicyError('That browser changed since you approved it, so the address was not opened.')
        argv = [OPEN, '-a', entry['id'], url]
    if sys.platform != 'darwin' and opener is None:
        raise PolicyError('OpenURL works on macOS.')
    run = opener or (lambda a: subprocess.run(a, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, timeout=10, check=False).returncode)
    if run(argv) != 0:
        raise PolicyError('macOS did not accept the request to open that address.')
    observed = None
    if entry is not None:
        if lister is None:
            from .native_apps import running as lister
        deadline = clock() + 3.0
        while clock() < deadline:
            try:
                observed = bool(lister(entry['id']))
            except Exception:
                observed = None
                break
            if observed:
                break
            sleep(0.2)
    result = {'opened': url, 'browser': entry['name'] if entry else 'the default browser',
              'launch_observed': observed, 'proves': 'open_request_only'}
    if payload.get('route') in ('table', 'opensearch') and isinstance(payload.get('query'), str):
        result['search'] = {'site': payload.get('site'), 'query': payload['query'][:300], 'route': payload['route']}
    return result


def describe(result):
    """The action summary: what was asked of macOS, never more."""
    result = result or {}
    search = result.get('search') if isinstance(result.get('search'), dict) else None
    what = ('the search for “' + str(search.get('query', ''))[:200] + '” on ' + str(search.get('site', 'the site'))[:128]
            + ' (' + str(result.get('opened', 'the address'))[:300] + ')') if search \
        else str(result.get('opened', 'the address'))[:300]
    return ('Asked macOS to open ' + what + ' in ' + str(result.get('browser', 'the browser'))[:128]
            + '. Pebble cannot see the page, so it cannot confirm what the page shows or does.')


# Phase 43 (1.9.0, §7): an opened page proves only that macOS accepted the request. A task that asked for something to
# play or be done there is not done until an act inside the page or window is observed.
PAGE_WORK = re.compile(r'\b(play|plays|playing|watch|listen|click|press|type|fill|sign up|subscribe|buy|order|book|'
                       r'post|reply|send|add to (?:cart|basket)|download|upload|submit)\b', re.I)


def outcome_issue(request_text, receipts, screen_evidence=()):
    """A completion issue when the person asked for something to play or be done on a page and only the open request
    is evidenced; an input or commit that occurred in a granted window or tab counts (ScreenSessions.evidence)."""
    if not receipts or not isinstance(request_text, str) or not PAGE_WORK.search(request_text):
        return None
    if any(e.get('verdict') == 'occurred' and e.get('class') in ('input', 'commit') for e in screen_evidence or ()):
        return None
    return ('The page was only opened (a request to the browser); nothing shows that it plays or that the rest was '
            'done there. Say what was opened, not that it plays or is done.')


def _origin(url):
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
    except ValueError:
        return None
    if parts is None or parts.scheme != 'https' or not parts.hostname:
        return None
    return 'https://' + parts.hostname.lower() + (f':{parts.port}' if parts.port else '')


def browser_request_for(actions, run_id, url):
    """Phase 45 (D128): the refusal for an OpenURL whose site this run already asked BrowserRequest for (waiting for the
    person or granted), else None. Measured at 01:33 on 2026-10-26: the model opened the same search with OpenURL while
    its BrowserRequest waited, so the person's browser showed the page twice and Pebble could see only one of them."""
    origin = _origin(url)
    if not run_id or origin is None:
        return None
    for action in actions or ():
        payload = action.get('payload') if isinstance(action, dict) else None
        if action.get('kind') != 'browser_grant' or action.get('status') not in ('pending', 'running', 'applied') \
                or not isinstance(payload, dict) or payload.get('run_id') != run_id:
            continue
        if origin in (payload.get('read') or ()) or origin in (payload.get('sites') or ()):
            return ('This task already asked for ' + origin + ' with BrowserRequest. Do not open it again with '
                    'OpenURL: wait for the person to answer the review, then use ScreenObserve and ScreenAct in that '
                    'session (it opens the page itself).')
    return None
