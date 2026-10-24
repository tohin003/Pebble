"""Phase 36 (1.4.0, beat plan W6 stage 2): a long task's older tool results are sealed in large chunks.

Since 1.3.0 every request above 8K extends the previous one, so a provider's cache reads everything but the newest step.
A long task's window still grows with every result. Above 32K, once the window passes a soft size (32K tokens, or a
quarter of a smaller budget), the results of older tool exchanges are replaced, in one chunk, by stubs that keep the
tool, the result's facts and a `page:<generation>:<index>` handle; ReadRunEvidence returns the full result from the
run's archive. A seal makes the provider read everything after the first sealed result fresh once, so it happens only
when the chunk is at least 20K tokens and twice that fresh part. ReadRunEvidence joins the tool list in the seal's step,
so a run's request changes once per seal. At 32K and below, and with PEBBLE_CONTEXT_SEAL=0, nothing changes.

The App runner and broker are the checked-in code; only the root model is scripted (some steps answer what the window
shows, so a test can read back a handle the run itself made).
"""
import copy
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'scripts'))

import validate_phase34 as v34  # noqa: E402  (its scripted-model harness)
from pebble import context_seal, runner  # noqa: E402
from pebble.context_seal import plan, seal, sealing  # noqa: E402

BUDGET = 131072


# -- the plan, on hand-built windows -------------------------------------------------------------------------------

def window(sizes, names=None, error_at=()):
    """A system message, the request, then one Read-like exchange per size (that many characters of source)."""
    messages = [{'role': 'system', 'content': 'You are Pebble.'},
                {'role': 'user', 'content': 'Read the parts.', '_pebble_current': True}]
    for i, size in enumerate(sizes):
        name = (names or {}).get(i, 'Read')
        messages.append({'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': f'c{i}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps({'i': i})}}]})
        body = {'error': 'Not found.'} if i in error_at else {
            'path': f'part{i:02d}.txt', 'start_line': 1, 'total_lines': 99, 'receipt_id': f'1:c{i}',
            'file_hash': hashlib.sha256(str(i).encode()).hexdigest(), 'untrusted_source': 'x' * size}
        messages.append({'role': 'tool', 'tool_call_id': f'c{i}', 'content': json.dumps(body)})
    return messages


def tool_index(messages, call_id):
    return next(i for i, m in enumerate(messages) if m.get('role') == 'tool' and m.get('tool_call_id') == call_id)


def test_sealing_is_off_at_32k_and_below_and_with_the_switch_off():
    class App:
        pass
    app = App()
    assert not sealing(app, 32768) and sealing(app, 32769) and sealing(app, BUDGET)
    app.context_seal_enabled = False
    assert not sealing(app, BUDGET)
    assert not sealing(object(), 2.5e5) and not sealing(object(), True)


def test_a_window_below_the_soft_size_never_seals():
    small = window([6000] * 14)  # about 29K tokens
    assert context_seal.estimate(small) < context_seal.SEAL_SOFT_TOKENS
    assert plan(small, BUDGET) is None
    assert plan(window([8000] * 20), 32768) is None  # the same long window at 32K: off


def test_older_results_are_chosen_in_one_chunk_and_the_newest_stay():
    messages = window([8000] * 20)
    chosen = plan(messages, BUDGET)
    assert chosen and chosen['kept_exchanges'] == 3  # three 8,000-character results fill KEEP_TOKENS
    assert chosen['indices'] == [tool_index(messages, f'c{i}') for i in range(17)]
    assert chosen['chars'] == sum(len(messages[i]['content']) for i in chosen['indices'])


def test_small_newest_results_keep_eight_exchanges():
    messages = window([12000] * 14 + [500] * 8)
    chosen = plan(messages, BUDGET)
    assert chosen and chosen['kept_exchanges'] == context_seal.KEEP_EXCHANGES
    assert chosen['indices'] == [tool_index(messages, f'c{i}') for i in range(14)]


def test_a_chunk_below_the_minimum_or_its_payback_waits():
    # Old results too small to be worth a fresh read of what follows them.
    messages = window([2000] * 20 + [30000] * 3)
    assert context_seal.estimate(messages) > context_seal.SEAL_SOFT_TOKENS
    assert plan(messages, BUDGET) is None
    # Large enough, but less than twice the part the provider would read fresh again.
    messages = window([21000] * 4 + [24000] * 3)
    sealed_tokens = 4 * len(window([21000])[3]['content']) // 4
    assert sealed_tokens >= context_seal.SEAL_MIN_TOKENS and plan(messages, BUDGET) is None
    # Past three quarters of the budget the payback test is waived (the request changes anyway, and the 100 %
    # compaction is near); the 20K minimum is not.
    waived = plan(messages, BUDGET, pressing=True)
    assert waived and waived['payback_waived']
    assert waived['indices'] == [tool_index(messages, f'c{i}') for i in range(4)]
    assert waived['window_tokens'] > 0 and waived['budget'] == BUDGET
    assert plan(window([2000] * 20 + [30000] * 3), BUDGET, pressing=True) is None
    assert context_seal.past_offer(messages, 65536) and not context_seal.past_offer(messages, BUDGET * 4)


def test_never_sealed_results_stay_word_for_word():
    names = {0: 'RunTests', 1: 'ReadRunEvidence', 2: 'RunRecipe', 3: 'AskUserQuestion', 4: 'UpdatePlan'}
    messages = window([8000] * 24, names=names, error_at={5})
    chosen = plan(messages, BUDGET)
    kept = {tool_index(messages, f'c{i}') for i in range(6)}
    assert chosen and not kept & set(chosen['indices'])
    sealed, _, _ = seal(messages, chosen, 1)
    assert all(sealed[i] == messages[i] for i in kept)
    # Every message that is not a tool result, and every tool call, is unchanged.
    assert all(a == b for a, b in zip(messages, sealed) if a.get('role') != 'tool')


def test_a_seal_keeps_the_facts_and_a_handle_and_archives_the_original():
    messages = window([8000] * 20)
    before = copy.deepcopy(messages)
    chosen = plan(messages, BUDGET)
    sealed, archive, manifest = seal(messages, chosen, 3)
    assert messages == before  # the input list is not changed
    assert all(sealed[i] is messages[i] for i in range(len(messages)) if i not in chosen['indices'])
    first = chosen['indices'][0]
    stub = json.loads(sealed[first]['content'])
    assert stub == {'sealed': True, 'tool': 'Read', 'path': 'part00.txt',
                    'file_hash': json.loads(before[first]['content'])['file_hash'], 'start_line': 1, 'total_lines': 99,
                    'result_receipt_id': '1:c0', 'chars': len(before[first]['content']),
                    'receipt_id': f'page:3:{first}', 'note': context_seal.STUB_NOTE}
    assert sealed[first]['tool_call_id'] == 'c0'
    assert archive == [{'index': i, 'message': before[i]} for i in chosen['indices']]
    assert manifest['kind'] == 'seal' and manifest['archived_results'] == 17 and manifest['kept_exchanges'] == 3
    assert manifest['sealed_chars'] == chosen['chars']
    # A sealed result is never sealed again, so a later seal chooses only results that arrived since.
    later = sealed + window([8000] * 20)[2:]
    for i, m in enumerate(later[len(sealed):], len(sealed)):
        if m.get('tool_call_id'):
            later[i] = {**m, 'tool_call_id': 'n' + m['tool_call_id']}
        if m.get('tool_calls'):
            later[i] = {**m, 'tool_calls': [{**c, 'id': 'n' + c['id']} for c in m['tool_calls']]}
    again = plan(later, BUDGET)
    assert again and not set(again['indices']) & set(chosen['indices'])


def test_the_journal_refuses_a_stale_or_unbounded_seal(tmp_path):
    app = v34.app_for(tmp_path, 'auto', BUDGET)
    try:
        chat = app.new_chat(str(tmp_path))
        rid = app.runs.create(chat['id'], 'Read the parts.', app.runtime_config(chat, False), [])
        messages = window([8000] * 20)
        chosen = plan(messages, BUDGET)
        _, archive, manifest = seal(messages, chosen, 1)
        with pytest.raises(ValueError, match='another archive generation'):
            app.runs.seal(rid, archive, manifest, 2)
        assert app.runs.seal(rid, archive, manifest, 1) == 1 and app.runs.next_generation(rid) == 2
        with pytest.raises(ValueError, match='another archive generation'):
            app.runs.seal(rid, archive, manifest, 1)
        assert app.runs.context_archive(rid, 1)['archive'] == archive
    finally:
        v34.close(app)


# -- a scripted long task in the App ---------------------------------------------------------------------------------

class Responder(v34.Scripted):
    """The scripted root model; a callable step answers the messages it is sent."""

    def __enter__(self):
        self.real = self.providers.complete

        def complete(provider, messages, tools, cancel, token, effort=False):
            self.requests.append(copy.deepcopy(messages))
            self.tools.append([t['function']['name'] for t in tools or []])
            assert len(self.requests) <= len(self.script), 'Unscripted root model request.'
            step = self.script[len(self.requests) - 1]
            response = copy.deepcopy(step(messages) if callable(step) else step)
            if response.get('content'):
                token(response['content'])
            return response
        self.tools = []
        self.providers.complete = complete
        return self


WORDS = ('river stone ledger harbour signal window copper meadow lantern orchard quiet margin falcon '
         'thistle compass beacon cedar furnace granite hollow island juniper kettle').split()


def parts(folder, count, lines=260, prose=False):
    """`count` files of `lines` distinct records: hex digests (dense: about 1.7 characters a token in a real
    tokenizer), or words (prose-like, as code and text are); `prose` may be a count of leading prose files."""
    folder.mkdir(parents=True)
    for i in range(count):
        rows = []
        for j in range(lines):
            digest = hashlib.sha256(f'{i}-{j}'.encode()).digest()
            words = prose is True or (type(prose) is int and i < prose)
            body = ' '.join(WORDS[b % len(WORDS)] for b in digest[:9]) if words else digest.hex()
            rows.append(f'record {i}-{j}: ' + body)
        (folder / f'part{i:02d}.txt').write_text('\n'.join(rows) + '\n')
    return folder


def events(app, rid):
    found, after = [], 0
    while True:
        page = app.runs.events(rid, after=after, limit=200)
        if not page:
            return found
        found += page
        after = page[-1]['sequence']


def run_task(tmp_path, script, count=20, lines=260, context=BUDGET, enabled=True):
    folder = parts(tmp_path / 'project', count, lines)
    app = v34.app_for(tmp_path, 'auto', context)
    app.context_seal_enabled = enabled
    try:
        with Responder() as scripted:
            scripted.script = script + [v34.final('Done.')] * 3
            chat = app.new_chat(str(folder))
            app.work(chat, 'Read every part file in order, then say you are done.', False, [])
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                runs = app.runs.list(chat['id'])
                if runs and app.active is None and runs[0]['state'] not in ('queued', 'running', 'retry_wait'):
                    break
                time.sleep(.02)
            rid = app.runs.list(chat['id'])[0]['id']
            return {'run': app.runs.get(rid), 'events': events(app, rid), 'requests': scripted.requests,
                    'tools': scripted.tools, 'receipts': app.runs.tools(rid)}
    finally:
        v34.close(app)


def reads(count):
    return [v34.call('Read', {'file_path': f'part{i:02d}.txt'}, f'r{i}') for i in range(count)]


def stubs(messages):
    return [json.loads(m['content']) for m in messages
            if m.get('role') == 'tool' and m['content'].startswith('{"sealed"')]


def test_a_long_task_seals_in_chunks_and_changes_its_request_only_then(tmp_path):
    result = run_task(tmp_path, reads(20))
    assert result['run']['state'] == 'completed'
    sealed = [e for e in result['events'] if e['kind'] == 'context_sealed']
    assert [e['payload']['generation'] for e in sealed] == [1, 2]  # 20 results of about 4K tokens each, at 128K
    for event in sealed:
        manifest = event['payload']['manifest']
        assert manifest['archived_results'] >= 5 and manifest['sealed_tokens'] >= context_seal.SEAL_MIN_TOKENS
    prefixes = [e['payload'].get('prefix') for e in result['events'] if e['kind'] == 'model_attempt']
    changed = [i for i, p in enumerate(prefixes) if p and not p.get('first') and not p['append_only']]
    assert len(changed) == 2, prefixes  # the seals' steps; every other request extends the one before it
    assert all(prefixes[i]['first_change']['role'] == 'tool' for i in changed)
    # ReadRunEvidence joins the tool list in the first seal's step, not before, and stays.
    offered = ['ReadRunEvidence' in names for names in result['tools']]
    assert offered.index(True) == changed[0] and all(offered[changed[0]:])
    assert all(result['tools'][i] == result['tools'][i - 1] for i in range(1, len(result['tools'])) if i != changed[0])
    # The window shrinks at a seal, every Read call is still there, and the run records keep every receipt.
    before, after = result['requests'][changed[0] - 1], result['requests'][changed[0]]
    assert len(json.dumps(after)) < len(json.dumps(before)) // 2
    calls = [c['id'] for m in result['requests'][-1] for c in m.get('tool_calls') or []]
    assert calls == [f'r{i}' for i in range(20)]
    assert {r['call_id'].split(':', 1)[1] for r in result['receipts']} >= {f'r{i}' for i in range(20)}
    assert len(stubs(result['requests'][-1])) == sum(e['payload']['manifest']['archived_results'] for e in sealed)


def test_a_sealed_result_reads_back_byte_for_byte(tmp_path):
    def read_back(messages):
        handle = stubs(messages)[0]['receipt_id']
        return v34.call('ReadRunEvidence', {'receipt_id': handle}, 'back0')

    def next_page(n):
        def step(messages):
            last = json.loads(messages[-1]['content'])
            if last.get('next_receipt_id') is None:
                return v34.final('Done.')
            return v34.call('ReadRunEvidence', {'receipt_id': last['next_receipt_id']}, f'back{n}')
        return step
    result = run_task(tmp_path, reads(20) + [read_back] + [next_page(n) for n in range(1, 8)])
    assert result['run']['state'] == 'completed'
    final = result['requests'][-1]
    pages = [json.loads(m['content']) for m in final
             if m.get('role') == 'tool' and m['tool_call_id'].startswith('back')]
    assert len(pages) == 5 and pages[-1]['next_offset'] is None
    first_stub = stubs(final)[0]
    original = next(m['content'] for request in result['requests'] for m in request
                    if m.get('tool_call_id') == 'r0' and not m['content'].startswith('{"sealed"'))
    assert first_stub['chars'] == len(original) and pages[0]['total_chars'] == len(original)
    assert ''.join(p['untrusted_text'] for p in pages) == original
    assert all(p['origin'] == 'archived_tool_result' and p['authorization'] == 'none' for p in pages)


def test_a_short_task_never_seals_and_its_requests_are_the_same_with_the_switch_off(tmp_path):
    on = run_task(tmp_path / 'on', reads(6))
    off = run_task(tmp_path / 'off', reads(6), enabled=False)
    assert on['run']['state'] == off['run']['state'] == 'completed'
    assert not [e for e in on['events'] if e['kind'] == 'context_sealed']
    assert on['requests'] == off['requests'] and on['tools'] == off['tools']


def test_with_the_switch_off_a_long_task_never_seals_and_keeps_the_half_full_offer(tmp_path):
    result = run_task(tmp_path, reads(20), enabled=False)
    assert result['run']['state'] == 'completed'
    assert not [e for e in result['events'] if e['kind'] == 'context_sealed']
    assert not stubs(result['requests'][-1])
    from pebble.context_manifest import estimate_tokens
    first = next(i for i, names in enumerate(result['tools']) if 'ReadRunEvidence' in names)
    assert estimate_tokens(result['requests'][first]) > BUDGET // 2 >= estimate_tokens(result['requests'][first - 1])


def test_at_32k_nothing_seals(tmp_path):
    result = run_task(tmp_path, reads(10), count=10, lines=60, context=32768)
    assert result['run']['state'] == 'completed'
    assert not [e for e in result['events'] if e['kind'] == 'context_sealed']


def test_the_large_offer_waits_for_a_seal_where_sealing_is_on():
    class App:
        class store:
            @staticmethod
            def query(*a):
                return []
    plain = [{'role': 'system', 'content': 'You are Pebble.'},
             {'role': 'user', 'content': 'Fix it.', '_pebble_current': True}]
    long = {'role': 'tool', 'tool_call_id': 'c2', 'content': 'x' * (3 * 65536 + 300)}  # past half of 128K, below 3/4
    app = App()
    assert 'ReadRunEvidence' not in runner.large_needs(app, 'r', 'Fix it.', plain + [long], BUDGET)
    assert 'ReadRunEvidence' in runner.large_needs(app, 'r', 'Fix it.', plain + [long], BUDGET, compacted=True)
    app.context_seal_enabled = False
    assert 'ReadRunEvidence' in runner.large_needs(app, 'r', 'Fix it.', plain + [long], BUDGET)


# -- the real provider path: seals and the 100 % compaction in one run ---------------------------------------------

class FakeModel:
    """An OpenAI-compatible streaming endpoint on loopback: one Read per request, in order, then a final answer."""

    def __init__(self, count):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.bodies, self.count = [], count
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                model.bodies.append(body)
                n = len(model.bodies) - 1
                if n >= model.count:
                    delta = {'content': 'Done.'}
                else:
                    delta = {'tool_calls': [{'index': 0, 'id': f'r{n}', 'type': 'function', 'function': {
                        'name': 'Read', 'arguments': json.dumps({'file_path': f'part{n:02d}.txt'})}}]}
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
                self.wfile.write(b'data: [DONE]\n\n')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize('context, prose, kinds', [
    (BUDGET, True, {'context_sealed'}),
    (65536, True, {'context_sealed'}),
    # Prose-like files seal; then dense files (a tokenizer splits them much finer than the estimate's three bytes a
    # token) reach the 100 % compaction before the next seal. Both kinds land in one archive sequence.
    (49152, 12, {'context_sealed', 'context_compacted'}),
    # Dense files alone reach the compaction before a 20K-token chunk exists: kept by compaction alone, as in 1.3.0.
    (49152, False, {'context_compacted'}),
])
def test_seals_and_the_100_percent_compaction_share_one_archive_through_the_provider(tmp_path, context, prose, kinds):
    from pebble.app import App
    from pebble.permissions import DEFAULT
    count = 24
    folder = parts(tmp_path / 'project', count, prose=prose)
    model = FakeModel(count)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *a: None)
    try:
        app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'auto'})
        app.store.set_setting('memory_enabled', False)
        app.model_save(name='p36 fixture', url=f'http://127.0.0.1:{model.server.server_port}', model='p36-fixture',
                       context=context)
        chat = app.new_chat(str(folder))
        app.work(chat, 'Read every part file in order, then say you are done.', False, [])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            runs = app.runs.list(chat['id'])
            if runs and app.active is None and runs[0]['state'] not in ('queued', 'running', 'retry_wait'):
                break
            time.sleep(.05)
        rid = app.runs.list(chat['id'])[0]['id']
        assert app.runs.get(rid)['state'] == 'completed', app.runs.get(rid).get('error')
        found = events(app, rid)
        archived = [(e['kind'], e['payload']['generation']) for e in found
                    if e['kind'] in ('context_sealed', 'context_compacted')]
        assert [g for _, g in archived] == list(range(1, len(archived) + 1)), archived
        assert {kind for kind, _ in archived} == kinds, archived
        if kinds == {'context_sealed', 'context_compacted'}:
            assert archived[0][0] == 'context_sealed', archived
        # What went over the wire: no host markers, the stubs as tool results, every call kept.
        assert all(not any(k.startswith('_pebble_') for k in m) for b in model.bodies for m in b['messages'])
        assert any(stubs(b['messages']) for b in model.bodies) is ('context_sealed' in kinds)
        for generation in range(1, len(archived) + 1):
            assert app.runs.context_archive(rid, generation)['archive']
    finally:
        model.close()
        app.close()
