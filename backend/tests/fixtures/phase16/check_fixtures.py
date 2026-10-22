"""Phase 16 fixture self-check (T7): layout, size budget, generator determinism and
cross-references for every case, plus each 'calibrated: T1' expectation checked
against T1's finished sources.py, web.py and source_ledger.py.

This is not the Phase 16 security suite (test_phase16_security.py), and pytest does
not collect it by default because its name does not match test_*.py. Run it by path:

    .runtime/agent-env/bin/python -m pytest backend/tests/fixtures/phase16/check_fixtures.py

Every peer is a local fake; nothing reaches the network.
"""

import importlib.util
import json
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # backend/, as backend/tests/conftest.py does

_spec = importlib.util.spec_from_file_location('p16_kit', HERE / 'kit.py')
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

from pebble import sources, web  # noqa: E402
from pebble.runtime import RunJournal  # noqa: E402
from pebble.source_ledger import SourceLedger, claim_key, freshness, registrable  # noqa: E402
from pebble.store import Store  # noqa: E402

THREAT_MODEL = HERE.parents[3] / 'docs' / 'HARNESS_PHASE16_THREAT_MODEL.md'
MANIFEST = kit.manifest()
CASES = kit.load('web/cases.json')['cases']
CHAINS = kit.load('web/redirect_chains.json')
LARGE = kit.load('web/large_page.json')
BOMBS = kit.load('web/pdf_bombs.json')
NOW = kit.NOW


def calibrate(expected, observed, what):
    """A 'CALIBRATE' placeholder fails with the observed value, so it can be recorded."""
    assert expected != 'CALIBRATE', f'{what}: record the observed value {json.dumps(observed, ensure_ascii=False)}'
    assert observed == expected, what


# -- layout ------------------------------------------------------------------------------------

def owned_files():
    return sorted(p for p in HERE.rglob('*') if p.is_file() and '__pycache__' not in p.parts)


def test_layout_budget_and_cross_references():
    files = owned_files()
    total = sum(p.stat().st_size for p in files)
    cache = sum(p.stat().st_size for p in HERE.rglob('__pycache__/*') if p.is_file())
    print(f'phase16 fixture bytes: {total} in {len(files)} files; __pycache__ {cache}')
    assert total <= kit.BUDGET_BYTES
    listed = {name for case in MANIFEST['cases'] for name in case['files']}
    support = {'manifest.json', 'README.md', 'kit.py', 'check_fixtures.py'}
    assert {str(p.relative_to(HERE)) for p in files} == listed | support
    doc = THREAT_MODEL.read_text(encoding='utf-8')
    gates = set(re.findall(r'^### (G\d+)\. ', doc, re.M))
    assert gates == {f'G{n}' for n in range(1, len(gates) + 1)}
    opens = set(re.findall(r'^\| (O\d+) \|', doc, re.M))
    ids = [case['id'] for case in MANIFEST['cases']]
    assert len(ids) == len(set(ids))
    for case in MANIFEST['cases']:
        assert case['gate'] in gates, case['id']
        for name in case['tests']:
            assert name in doc, name
        for name in case['files']:
            if name.endswith('.json') and name != 'web/cases.json':
                value = kit.load(name)
                assert value['format'] in kit.FORMATS and value['gate'] == case['gate'], name
                assert value['case'] == case['id'], name
                assert set(value.get('open_items', [])) <= opens, name
            kit.data(name)
    for key, case in CASES.items():
        assert case['gate'] in gates and key in ids
    for name in listed:
        raw = kit.text(name)
        assert set(re.findall(r'\bO\d+\b', raw)) <= opens, name


def test_large_page_and_pdf_generators_are_deterministic_and_small():
    first, second = kit.large_page(LARGE), kit.large_page(LARGE)
    params = LARGE['params']
    assert first == second and len(first) >= params['target_bytes']
    cap = params['cap_bytes']
    assert first[cap - 1:cap + 1] == params['straddle'].encode()
    assert b'P16-LATE-MARKER' not in first[:cap]
    calibrate(LARGE['sha256'], kit.sha256(first), 'large_page sha256')
    for spec in BOMBS['kinds']:
        if 'generator' in spec:
            body = kit.pdf(spec)
            assert body.startswith(b'%PDF-') and len(body) <= spec['max_file_bytes'], spec['id']


def test_echo_bodies_hide_the_token_only_where_the_fixture_says():
    echo = kit.load('connectors/token_echo.json')
    token = echo['token']
    for response in echo['responses']:
        body = kit.echo_body(response, token)
        head = ' '.join(response.get('headers', {}).values()).replace('{token}', token)
        exact = response['id'].startswith('exact_')
        assert (token.encode() in body or token in head) is exact, response['id']
        assert response.get('body_transform') in (None, *kit.TRANSFORMS), response['id']


def nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item)


@pytest.mark.parametrize('name', ['ui/ax_click_send.json', 'ui/ax_mislabelled_secure.json', 'ui/ax_moved_element.json',
                                  'ui/ax_spoofed_title.json', 'ui/ax_autosubmit_form.json', 'ui/navigation_during_act.json',
                                  'ui/unsupported_targets.json'])
def test_ui_references_resolve(name):
    value = kit.load(name)
    defined = {n['_ref'] for n in nodes(value) if '_ref' in n and ('AXRole' in n or 'tag' in n)}
    used = {n['_ref'] for n in nodes(value) if '_ref' in n and 'action' in n}
    assert used <= defined, used - defined
    for scenario in value.get('scenarios', []):
        for key in ('observe', 'live'):
            ref = scenario.get(key)
            if isinstance(ref, str):
                assert ref in value['trees'], (scenario['id'], ref)
            elif isinstance(ref, dict):
                assert set(ref['windows']) <= set(value['windows']), scenario['id']
    for n in nodes(value):
        if 'AXFrame' in n:
            assert len(n['AXFrame']) == 4 and all(type(v) is int for v in n['AXFrame'])


@pytest.mark.parametrize('name', ['connectors/draft_email_is_send.json', 'connectors/recipient_injection.json'])
def test_connector_calls_name_served_and_reviewed_tools(name):
    value = kit.load(name)
    tools = {t['name'] for t in value['server']['tools']}
    reviews = value.get('reviews') or {'default': value['review']}
    for review in reviews.values():
        assert set(review.get('effects') or {}) <= tools
    for call in value['calls']:
        assert call.get('tool', 'send_email') in tools, call['id']
        if 'turn' in call:
            assert call['turn'] in value['turns']


# -- T1 calibration: fakes ------------------------------------------------------------------------

class Reply:
    def __init__(self, status, headers, body):
        self.status, self.body = status, body
        self.headers = {k.lower(): v for k, v in headers.items()}

    def read1(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def getheader(self, key, default=None):
        return self.headers.get(key.lower(), default)


class FakeWeb:
    """A fake resolver and HTTPS peer for redirect chains (the pattern of test_web_fetch.py)."""

    def __init__(self, monkeypatch, chain):
        self.dns = {host: list(ips) for host, ips in chain['dns'].items()}
        self.pages, self.requests = {}, []
        for hop in chain['serve']:
            parts = urlsplit(hop['url'])
            self.dns.setdefault(parts.hostname, [CHAINS['public_ip']])
            headers = {}
            if hop.get('location'):
                headers['Location'] = hop['location']
            if hop.get('content_type'):
                headers['Content-Type'] = hop['content_type']
            target = (parts.path or '/') + ('?' + parts.query if parts.query else '')
            self.pages[(parts.hostname, target)] = (hop['status'], headers, hop.get('body', '').encode())
        fake = self

        class Connection:
            def __init__(self, host, **kwargs):
                self.host = host

            def request(self, method, target, headers):
                fake.requests.append(self.host)
                self.reply = fake.pages.get((self.host, target), (404, {}, b''))

            def getresponse(self):
                return Reply(*self.reply)

            def close(self):
                pass

        def resolve(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for ip in self.dns.get(host, [])]

        monkeypatch.setattr(sources.socket, 'getaddrinfo', resolve)
        monkeypatch.setattr(sources.http.client, 'HTTPSConnection', Connection)


class Pages:
    """url -> (body, content type, headers), read through T1's real extractor."""

    def __init__(self):
        self.pages, self.calls = {}, []

    def add(self, url, body, content_type, headers=None):
        self.pages[url] = (body, content_type, headers or {})

    def __call__(self, url, *, max_bytes):
        self.calls.append((url, max_bytes))
        body, content_type, headers = self.pages[url]
        return sources.extract_document({
            'url': url, 'final_url': url, 'redirects': [], 'content_type': content_type, 'date': headers.get('Date'),
            'last_modified': headers.get('Last-Modified'), 'body': body[:max_bytes], 'truncated': len(body) > max_bytes})


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / 'home')
    runs = RunJournal(store, tmp_path / 'home')
    chat = store.new_chat()
    rid = runs.create(chat, 'Research Northfield', {'limits': {}})
    pages = Pages()
    ledger = SourceLedger(store, fetch=pages, clock=lambda: NOW)
    yield type('Env', (), {'store': store, 'rid': rid, 'pages': pages, 'ledger': ledger})
    runs.close()
    store.close()


def serve(env, case):
    headers = case.get('headers', {})
    env.pages.add(case['url'], kit.data(case['file']), case['content_type'], headers)
    return env.ledger.read(env.rid, case['url'], window_chars=6500)


# -- T1 calibration: web cases --------------------------------------------------------------------

def test_w1_hidden_text_is_counted_never_kept_and_the_page_is_flagged(env):
    case = CASES['W1']
    result = serve(env, case)
    text, shown = env.ledger.text(env.rid, result['source_id']), json.dumps(result, ensure_ascii=False)
    for marker in case['visible'] + case['residual_kept']:
        assert marker in text, marker
    for marker in case['hidden_counted'] + case['hidden_skipped']:
        assert marker not in text and marker not in shown, marker
    assert result['hidden_text_removed'] >= sum(map(len, case['hidden_counted']))
    assert result['title'] == case['expect']['title']
    assert set(case['expect']['flags_include']) <= set(result.get('flags', []))
    for key, value in case['expect']['ledger'].items():
        assert result[key] == value, key


def test_w1b_format_characters_and_the_flag(env):
    case = CASES['W1b']
    result = serve(env, case)
    text = env.ledger.text(env.rid, result['source_id'])
    for marker in case['visible']:
        assert marker in text, marker
    kept = [p for p in case['format_code_points'] if chr(int(p[2:], 16)) in text]
    calibrate(case['expect'], {'kept_code_points': kept, 'instruction_shaped': result['instruction_shaped']}, 'W1b')


@pytest.mark.parametrize('key', ['W2', 'W2b'])
def test_w2_dates_are_claims_with_a_basis(env, key):
    case = CASES[key]
    expect = case['expect']
    if 'extract' in expect:
        document = sources.extract_document({'url': case['url'], 'body': kit.data(case['file']),
                                             'content_type': case['content_type'], 'date': case['headers']['Date'],
                                             'last_modified': case['headers']['Last-Modified']})
        for field in ('claimed_published', 'claimed_modified'):
            assert sources.iso_date(document[field]) == expect['extract'][field], field
        assert document['date_basis'] == expect['extract']['date_basis']
        for marker in case.get('absent', []):
            assert marker not in document['text']
    result = serve(env, case)
    assert result['dates'] == expect['ledger']['dates']
    assert set(expect['ledger'].get('flags_include', [])) <= set(result.get('flags', []))
    assert freshness(result, now=NOW) == expect['freshness']


@pytest.mark.parametrize('chain', CHAINS['chains'], ids=[c['id'] for c in CHAINS['chains']])
def test_w3_redirect_chains(monkeypatch, chain):
    fake = FakeWeb(monkeypatch, chain)
    expect = chain['expect']
    if expect['outcome'] == 'refused':
        with pytest.raises(ValueError, match=re.escape(expect['message'])):
            sources.fetch_document(CHAINS['start'])
    else:
        document = sources.fetch_document(CHAINS['start'])
        assert document['final_url'] == expect['final_url'] and document['redirects'] == expect['redirects']
        assert expect['text_contains'] in document['text']
    assert fake.requests == expect['contacted']


def test_w4_large_page_prefix_window_and_run_caps(env):
    body, expect, read = kit.large_page(LARGE), LARGE['expect'], LARGE['read']
    for url in LARGE['urls']:
        env.pages.add(url, body, LARGE['content_type'])
    result = env.ledger.read(env.rid, LARGE['urls'][0], window_chars=read['window_chars'],
                             query_terms=tuple(read['query_terms']))
    text = env.ledger.text(env.rid, result['source_id'])
    source = env.ledger.get(env.rid, result['source_id'])
    assert source['bytes'] == expect['bytes'] and result['characters'] == expect['characters']
    assert result['truncated_at_fetch'] is expect['truncated_at_fetch']
    assert set(expect['flags_include']) <= set(result['flags'])
    window = result['untrusted_source']
    assert all(m in window for m in expect['window_contains'])
    assert len(json.dumps(window, ensure_ascii=False)) - 2 <= expect['window_max_escaped_chars']
    assert result['omitted_characters'] == result['characters'] - (result['shown'][1] - result['shown'][0])
    assert result['shown'][0] > 0
    assert all(m in text for m in expect['ledger_text_contains'])
    assert not any(m in text for m in expect['ledger_text_absent'])
    assert not set(expect['absent_keys']) & set(result)
    second = env.ledger.read(env.rid, LARGE['urls'][1], window_chars=read['window_chars'])
    assert env.ledger.get(env.rid, second['source_id'])['bytes'] == expect['second_read_bytes']
    with pytest.raises(ValueError, match=re.escape(expect['third_read_error'])):
        env.ledger.read(env.rid, LARGE['urls'][2], window_chars=read['window_chars'])
    assert [url for url, _ in env.pages.calls] == LARGE['urls'][:2]
    cap = LARGE['params']['cap_bytes']
    uncapped = sources.extract_document({'url': LARGE['urls'][0], 'body': body[:cap], 'truncated': True,
                                         'content_type': LARGE['content_type']}, max_chars=10 ** 7)
    assert all(m in uncapped['text'] for m in expect['extract_uncapped']['contains'])
    assert not any(m in uncapped['text'] for m in expect['extract_uncapped']['absent'])
    assert not set(expect['extract_uncapped']['flags_absent']) & set(uncapped['flags'])


@pytest.mark.parametrize('spec', BOMBS['kinds'], ids=[k['id'] for k in BOMBS['kinds']])
def test_w5_pdf_bombs(spec):
    body = kit.pdf(spec) if 'generator' in spec else spec['body'].encode()
    limit = spec.get('max_bytes', sources.MAX_FETCH_BYTES)
    assert (len(body) > limit) is ('max_bytes' in spec), 'only over_cap is cut by the byte cap'
    response = {'url': spec['url'], 'body': body[:limit], 'truncated': len(body) > limit, 'content_type': spec['content_type']}
    expect = spec['expect']
    if 'error' in expect:
        with pytest.raises(ValueError, match=re.escape(expect['error'])):
            sources.extract_document(response)
        return
    document = sources.extract_document(response)
    assert document['kind'] == expect['kind']
    if 'text' in expect:
        assert document['text'] == expect['text']
    if 'flags' in expect:
        assert document['flags'] == expect['flags']
    assert set(expect.get('flags_include', [])) <= set(document['flags'])
    if 'truncated' in expect:
        assert document['truncated'] is expect['truncated']
    assert all(m in document['text'] for m in expect.get('contains', []))
    assert not any(m in document['text'] for m in expect.get('absent', []))


def test_w6_conflicting_numbers_hidden_numbers_and_snippets(env):
    case = CASES['W6']
    expect, ids = case['expect'], {}
    shown = env.ledger.record_search(env.rid, case['search']['query'], case['search']['results'])
    ids['snippet'] = shown[0]['source_id']
    for page in case['pages']:
        env.pages.add(page['url'], kit.data(page['file']), page['content_type'])
        ids[page['id']] = env.ledger.read(env.rid, page['url'], window_chars=6500)['source_id']
    assert [ids[name] for name in case['order']] == [f'S{n}' for n in range(1, len(case['order']) + 1)]
    for name, label in expect['classes'].items():
        assert env.ledger.get(env.rid, ids[name])['primary_class'] == label, name
    for name, domain in expect['domains'].items():
        assert env.ledger.get(env.rid, ids[name])['domain'] == domain, name
    assert expect['hidden_absent'] not in env.ledger.text(env.rid, ids['primary'])
    key = claim_key(case['key']['subject'], case['key']['unit'])
    assert key == case['key']['expect']
    for claim in case['claims']:
        source = ids[claim['source']]
        if 'span' in claim:
            span = claim['span']
        else:
            text = env.ledger.text(env.rid, source)
            start = text.index(claim.get('span_of', claim['token']))
            span = [start, start + len(claim.get('span_of', claim['token']))]
        record = {'sentence': f"Northfield output is {claim['token']} [{source}].", 'token': claim['token'],
                  'support': 'supported', 'source_id': source, 'span': span, 'match': 'exact', 'key': key,
                  'value': claim['value']}
        if claim['expect'] == 'recorded':
            assert env.ledger.record_claims(env.rid, [record])['claim_ids']
        else:
            with pytest.raises(ValueError, match=re.escape(claim['expect'][len('error:'):])):
                env.ledger.record_claims(env.rid, [record])
    conflicts = env.ledger.conflicts(env.rid, state='open')
    assert [(c['key'], c['values'], c['state']) for c in conflicts] == [
        (expect['conflict']['key'], expect['conflict']['values'], expect['conflict']['state'])]
    probe = case['domain_probe']
    assert [registrable(urlsplit(u).hostname) for u in probe['urls']] == probe['t1_registrable']
    for answer in case['answers']:
        assert '{' not in kit.fill(answer['text'], ids), answer['id']


@pytest.mark.parametrize('key', ['W7', 'W7b'])
def test_w7_search_feeds(env, monkeypatch, key):
    case = CASES[key]
    monkeypatch.setattr(web, 'fetch_public', lambda url, accept: (kit.data(case['file']), 'application/rss+xml'))
    expect = case['expect']
    if 'error' in expect:
        with pytest.raises(ValueError, match=re.escape(expect['error'])):
            web.search(case['query'])
        return
    found = web.search(case['query'])
    items = found['untrusted_search_results']
    assert [i['url'] for i in items] == expect['search_urls']
    assert [i.get('published') for i in items] == expect['search_published']
    assert not any(m in json.dumps(found) for m in expect['absent'])
    shown = env.ledger.record_search(env.rid, case['query'], items)
    assert len(shown) == len(expect['ledger'])
    for item, wanted in zip(shown, expect['ledger']):
        source = env.ledger.get(env.rid, item['source_id'])
        assert item.get('published') == wanted['published']
        assert set(wanted['flags_include']) <= set(source['flags'])
        assert source['instruction_shaped'] is wanted['instruction_shaped']


def test_w9_memory_bait_is_flagged_and_carries_no_authority(env):
    case = CASES['W9']
    result = serve(env, case)
    for key, value in case['expect']['ledger'].items():
        assert result[key] == value, key
    assert case['bait_line'] in env.ledger.text(env.rid, result['source_id'])
