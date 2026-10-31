"""Phase 43 (1.9.0): OpenURL, the direct route: one http or https address in the person's default or named browser,
reviewed like opening an app and always when the address came from untrusted text or carries the person's words."""
import plistlib

import pytest

from pebble import open_url, runner
from pebble.policy import PolicyError


def app_bundle(tmp_path, name, bundle_id, web=True):
    path = tmp_path / f'{name}.app'
    (path / 'Contents').mkdir(parents=True)
    info = {'CFBundleIdentifier': bundle_id, 'CFBundleName': name}
    if web:
        info['CFBundleURLTypes'] = [{'CFBundleURLName': 'Web site URL', 'CFBundleURLSchemes': ['http', 'https']}]
    (path / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    return {'id': str(path), 'name': name, 'bundle_id': bundle_id, 'team_id': None}


@pytest.fixture
def apps(tmp_path):
    entries = [app_bundle(tmp_path, 'Comet', 'ai.perplexity.comet'), app_bundle(tmp_path, 'Safari', 'com.apple.Safari'),
               app_bundle(tmp_path, 'Notes', 'com.apple.Notes', web=False)]
    pinned = {e['id']: dict(e, signature='unknown', version='1') for e in entries}
    return entries, (lambda: entries), (lambda path: dict(pinned[path]))


@pytest.mark.parametrize('url', ['https://www.youtube.com/results?search_query=navior', 'http://example.com/a b'[:18],
                                 'https://maps.apple.com/?q=Kolkata'])
def test_web_addresses_are_accepted(url):
    assert open_url.validate(url) == url


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,hi', 'zoommtg://join?x=1',
                                 'https://user:secret@example.com/', 'https://example.com/a b', 'https:///nohost',
                                 'https://example.com/' + 'x' * 2048, '', None, 'https://example.com/\nX'])
def test_other_addresses_are_refused(url):
    with pytest.raises(PolicyError):
        open_url.validate(url)


def test_a_named_browser_must_be_an_installed_app_that_opens_web_addresses(apps):
    entries, inventory, pin = apps
    assert open_url.browser('Comet', inventory=inventory, pin=pin)['bundle_id'] == 'ai.perplexity.comet'
    assert open_url.browser('  comet.app ', inventory=inventory, pin=pin)['name'] == 'Comet'
    assert open_url.browser('com.apple.Safari', inventory=inventory, pin=pin)['name'] == 'Safari'
    assert open_url.browser(None, inventory=inventory, pin=pin) is None
    with pytest.raises(PolicyError) as exc:
        open_url.browser('Notes', inventory=inventory, pin=pin)
    assert 'Comet' in str(exc.value) and 'Notes' not in str(exc.value).split('browsers:')[1]
    with pytest.raises(PolicyError):
        open_url.browser('Netscape', inventory=inventory, pin=pin)


def test_an_address_from_untrusted_text_or_carrying_private_words_always_asks(apps):
    _, inventory, pin = apps
    clean = open_url.proposal({'url': 'https://www.youtube.com/results?search_query=navior', 'browser': 'Comet',
                               'why': 'to find the song'}, inventory=inventory, pin=pin)
    assert clean['requires_review'] is False and clean['browser']['name'] == 'Comet' and clean['why'] == 'to find the song'
    planted = 'https://evil.example/collect?d=secret'
    tainted = open_url.proposal({'url': planted}, untrusted={'evil.example/collect'}, inventory=inventory, pin=pin)
    assert tainted['requires_review'] is True and 'page' in tainted['reasons'][0]
    leaked = open_url.proposal({'url': 'https://example.com/?q=tax-return-2025'}, private=lambda url: ['tax-return-2025'],
                               inventory=inventory, pin=pin)
    assert leaked['requires_review'] is True and 'words from files' in leaked['reasons'][0]
    with pytest.raises(PolicyError):
        open_url.proposal({'url': 'https://example.com', 'why': 'x' * 301}, inventory=inventory, pin=pin)


def test_opening_asks_macos_once_and_proves_only_the_request(apps):
    entries, inventory, pin = apps
    calls = []
    payload = open_url.proposal({'url': 'https://www.youtube.com/watch?v=abc', 'browser': 'Comet'}, inventory=inventory,
                                pin=pin)
    result = open_url.open_url(payload, opener=lambda argv: calls.append(argv) or 0, pin=pin,
                               lister=lambda path: [{'pid': 7, 'start': 1.0}])
    assert calls == [['/usr/bin/open', '-a', entries[0]['id'], 'https://www.youtube.com/watch?v=abc']]
    assert result == {'opened': 'https://www.youtube.com/watch?v=abc', 'browser': 'Comet', 'launch_observed': True,
                      'proves': 'open_request_only'}
    default = open_url.open_url({'url': 'https://example.com', 'browser': None}, opener=lambda argv: calls.append(argv) or 0)
    assert calls[-1] == ['/usr/bin/open', 'https://example.com'] and default['browser'] == 'the default browser'
    with pytest.raises(PolicyError):
        open_url.open_url({'url': 'https://example.com', 'browser': None}, opener=lambda argv: 1)
    changed = lambda path: dict(pin(path), bundle_id='com.example.impostor')
    with pytest.raises(PolicyError) as exc:
        open_url.open_url(payload, opener=lambda argv: calls.append(argv) or 0, pin=changed)
    assert 'changed' in str(exc.value) and len(calls) == 2, 'nothing opened after the identity changed'
    text = open_url.describe(result)
    assert 'cannot confirm' in text and 'Comet' in text and 'playing' not in text


def test_the_companion_is_offered_openurl_with_its_sentence():
    # D114: the sentence teaches the site's own search and names no web tool (the web permission decides that one).
    sentence = runner.tool_sentences({'OpenURL'})
    assert 'OpenURL' in sentence and 'site and query' in sentence and 'WebSearch' not in sentence
    assert open_url.OPEN_INTENT.search('open comet browser and open youtube on it and search for the navior song and play it')
    assert not open_url.OPEN_INTENT.search('what is the capital of France?')
    from pebble import permissions, tool_authz, runtime, rewind
    assert permissions.GROUP['OpenURL'] == 'apps' and 'OpenURL' in tool_authz.ROOT_ONLY and 'OpenURL' in runtime.EFFECTS
    assert rewind.KINDS['OpenURL'] == 'app'


# ---- M1 (decision 9): a site's own search address, built from the person's words --------------------------------

def search_stub(calls):
    def search(site, query):
        calls.append((site, query))
        return {'url': 'https://www.youtube.com/results?search_query=' + '+'.join(query.split()), 'route': 'table',
                'site': 'youtube.com'}
    return search


def test_a_site_search_builds_the_address_and_records_its_route(apps):
    _, inventory, pin = apps
    calls = []
    payload = open_url.proposal({'site': 'YouTube', 'query': ' navior  song ', 'browser': 'Comet'}, inventory=inventory,
                                pin=pin, person_text='open comet and search youtube for the navior song and play it',
                                search=search_stub(calls))
    assert calls == [('YouTube', ' navior  song ')]
    assert payload['url'] == 'https://www.youtube.com/results?search_query=navior+song'
    assert (payload['route'], payload['site'], payload['query']) == ('table', 'youtube.com', 'navior song')
    assert payload['requires_review'] is False and payload['reasons'] == []
    plain = open_url.proposal({'url': 'https://example.com'}, inventory=inventory, pin=pin)
    assert (plain['route'], plain['site']) == ('url', None) and 'query' not in plain


def test_a_site_search_through_the_real_table_needs_no_fetch(apps):
    _, inventory, pin = apps
    payload = open_url.proposal({'site': 'youtube.com', 'query': 'navior'}, inventory=inventory, pin=pin,
                                person_text='play navior on youtube')
    assert payload['url'] == 'https://www.youtube.com/results?search_query=navior' and payload['route'] == 'table'


@pytest.mark.parametrize('args', [{'url': 'https://example.com', 'site': 'YouTube', 'query': 'x'},
                                  {'url': 'https://example.com', 'query': 'x'}, {'site': 'YouTube'}, {'query': 'x'}, {}])
def test_exactly_one_of_url_or_site_and_query(apps, args):
    _, inventory, pin = apps
    with pytest.raises(PolicyError):
        open_url.proposal(args, inventory=inventory, pin=pin, person_text='x', search=search_stub([]))


@pytest.mark.parametrize('person_text', [None, '', 'play something nice', 'open youtube'])
def test_search_words_that_are_not_the_persons_own_wait_for_them(apps, person_text):
    _, inventory, pin = apps
    payload = open_url.proposal({'site': 'YouTube', 'query': 'navior song'}, inventory=inventory, pin=pin,
                                person_text=person_text, search=search_stub([]))
    assert payload['requires_review'] is True and 'not your own words' in payload['reasons'][0]


def test_the_persons_words_count_in_any_order_and_case(apps):
    _, inventory, pin = apps
    payload = open_url.proposal({'site': 'YouTube', 'query': 'Song Navior'}, inventory=inventory, pin=pin,
                                person_text="Play Navior's new song on YouTube", search=search_stub([]))
    assert payload['requires_review'] is False
    assert open_url.own_words('navior', 'NAVIOR!') and not open_url.own_words('navior remix', 'navior')
    assert not open_url.own_words('!!!', 'anything') and not open_url.own_words('a', None)


def test_untrusted_or_private_search_words_always_ask(apps):
    _, inventory, pin = apps
    tainted = open_url.proposal({'site': 'YouTube', 'query': 'collect secret now'}, inventory=inventory, pin=pin,
                                person_text='search youtube for collect secret now', untrusted={'collect secret now'},
                                search=search_stub([]))
    assert tainted['requires_review'] is True and any('page' in r for r in tainted['reasons'])
    leaked = open_url.proposal({'site': 'YouTube', 'query': 'taxreturn2025 summary'}, inventory=inventory, pin=pin,
                               person_text='search youtube for taxreturn2025 summary',
                               private=lambda text: ['taxreturn2025'] if 'taxreturn2025' in text else [],
                               search=search_stub([]))
    assert leaked['requires_review'] is True and any('words from files' in r for r in leaked['reasons'])


def test_a_built_address_still_passes_every_url_check(apps):
    _, inventory, pin = apps
    bad = lambda site, query: {'url': 'javascript:alert(1)', 'route': 'table', 'site': 'evil.example'}
    with pytest.raises(PolicyError):
        open_url.proposal({'site': 'x.example', 'query': 'q'}, inventory=inventory, pin=pin, person_text='q',
                          search=bad)


def test_opening_a_search_says_which_search_was_asked_for(apps):
    entries, inventory, pin = apps
    calls = []
    payload = open_url.proposal({'site': 'YouTube', 'query': 'navior song', 'browser': 'Comet'}, inventory=inventory,
                                pin=pin, person_text='search youtube for the navior song', search=search_stub([]))
    result = open_url.open_url(payload, opener=lambda argv: calls.append(argv) or 0, pin=pin,
                               lister=lambda path: [{'pid': 7, 'start': 1.0}])
    assert calls == [['/usr/bin/open', '-a', entries[0]['id'], payload['url']]]
    assert result['search'] == {'site': 'youtube.com', 'query': 'navior song', 'route': 'table'}
    assert result['proves'] == 'open_request_only'
    text = open_url.describe(result)
    assert 'navior song' in text and 'youtube.com' in text and 'cannot confirm' in text and 'playing' not in text
