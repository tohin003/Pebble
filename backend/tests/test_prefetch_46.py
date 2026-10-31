"""Phase 46 (D154, D164): the files a run reads before its first model request (prefetch.py).

Measured first (golden dev split on gpt-6.1-sol and Opus): a run's first requests read the files its request names or,
in a follow-up turn, the files the conversation already changed; each such request costs about 5 s on sol, and every
follow-up turn of dev d-lh-91 read the project again. Codex's first step on dev d-lh-01 read every file of the small
project at once.
"""

import pytest

from pebble import prefetch


def project(tmp_path, files):
    root = tmp_path / 'p'
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data if isinstance(data, bytes) else data.encode())
    return root


def test_the_files_a_request_names_come_first_and_only_real_text_files_count(tmp_path):
    root = project(tmp_path, {'invoicing/rounding.py': 'def split_amount(): ...\n', 'tests/test_rounding.py': 'x = 1\n',
                              'README.md': '# r\n', 'blob.bin': b'\x00\x01', 'big.txt': 'x' * 30_000,
                              **{f'pkg/m{n}.py': 'y = 1\n' for n in range(prefetch.TINY_FILES + 1)}})
    text = ('`split_amount` in invoicing/rounding.py must not round; tests/test_rounding.py fails. See README.md, '
            'blob.bin, big.txt, missing.py and https://example.com/a.py.')
    assert prefetch.choose(root, text) == ['invoicing/rounding.py', 'tests/test_rounding.py', 'README.md']


def test_a_tiny_project_is_read_whole_and_a_larger_one_is_not(tmp_path):
    tiny = project(tmp_path / 't', {'app/parse.py': 'def parse(): ...\n', 'notes.txt': 'n\n', '.env': 'TOKEN=1\n',
                                    'pages/rumours.md': '40 hours?\n', 'data/rows.csv': 'a,b\n',
                                    'tests/test_parse.py': 't\n', 'build/out.js': 'o\n', '.git/config': 'c\n'})
    assert prefetch.choose(tiny, 'Fix the parser.') == ['app/parse.py', 'tests/test_parse.py']  # not notes.txt ...
    assert prefetch.choose(tiny, 'Fix the parser; see notes.txt.') == ['notes.txt', 'app/parse.py', 'tests/test_parse.py']
    larger = project(tmp_path / 'l', {f'src/m{n}.py': 'z = 1\n' for n in range(prefetch.TINY_FILES + 1)})
    assert prefetch.choose(larger, 'Fix the parser.') == []


def test_secrets_and_local_settings_are_never_prefetched_even_when_named(tmp_path):
    root = project(tmp_path, {'.env': 'TOKEN=1\n', 'config/secrets.json': '{}', 'deploy/id_rsa': 'k\n',
                              'settings.py': 's = 1\n', **{f'src/m{n}.py': 'z = 1\n' for n in range(prefetch.TINY_FILES + 1)}})
    assert prefetch.choose(root, 'Read .env, config/secrets.json, deploy/id_rsa and settings.py.') == ['settings.py']


def test_a_follow_up_turn_gets_what_the_conversation_changed_within_the_bounds(tmp_path):
    files = {f'src/m{n}.py': 'w = 1\n' * 300 for n in range(20)}  # 1,800 bytes each, a larger project
    root = project(tmp_path, files)
    actions = [{'kind': 'edit', 'status': 'applied', 'payload': {'path': f'src/m{n}.py'}} for n in range(20)]
    actions.append({'kind': 'edit', 'status': 'rejected', 'payload': {'path': 'src/m0.py'}})
    earlier = prefetch.earlier_edits(actions)
    assert earlier[:3] == ['src/m19.py', 'src/m18.py', 'src/m17.py'] and len(earlier) == prefetch.EARLIER_FILES
    assert prefetch.choose(root, 'Now add tests.', earlier)[:2] == ['src/m19.py', 'src/m18.py']
    assert len(prefetch.choose(root, 'Now add tests.', earlier, budget=4000)) == 2  # the window's budget bounds it


@pytest.mark.parametrize('rel', ['../outside.py', '/etc/hosts', 'link.py'])
def test_nothing_outside_the_project_or_behind_a_link(tmp_path, rel):
    root = project(tmp_path, {'a.py': 'a = 1\n', **{f'src/m{n}.py': 'z = 1\n' for n in range(prefetch.TINY_FILES + 1)}})
    (tmp_path / 'outside.py').write_text('secret = 1\n')
    (root / 'link.py').symlink_to(tmp_path / 'outside.py')
    assert prefetch.choose(root, f'Look at {rel} and a.py.') == ['a.py']


# -- end to end: the run's first exchange, through App.execute and the tool ledger -------------------------------------

import json  # noqa: E402
import time  # noqa: E402

from pebble import runner  # noqa: E402

from test_approval_handoff_22 import call, env, final, settled  # noqa: E402,F401 (fixture)


@pytest.fixture
def prefetching(monkeypatch):
    # On from 1.9.24 (runner.PREFETCH; off in 1.9.23 until the pins of a run's first exchange were restated)
    from pebble import runner
    monkeypatch.setattr(runner, 'PREFETCH', True)


def test_the_prefetch_is_on_from_1_9_24():
    from pebble import runner
    assert runner.PREFETCH is True


def test_a_project_run_starts_with_the_files_it_names_read_through_the_ledger(env, prefetching):
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    env.script = [final('add returns a + b.\n\nFINAL: calc.py:add')]
    app.work(chat, "Which function in calc.py adds two numbers? Don't change anything. End with `FINAL: <path>:<name>`.",
             False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    run = settled(env, app, rid, 'completed', 1)
    first = env.requests[0]
    [shown] = [m for m in first if m.get('name') == 'pebble_prefetch']
    assert shown['role'] == 'user' and shown['content'].startswith('HOST PREFETCH: Pebble read these project files')
    assert '\n"calc.py": {' in shown['content'] and '\n"test_calc.py": {' in shown['content']
    assert 'def add' in shown['content'] and 'untrusted_source' in shown['content']
    # one host message, the same on every provider: no host-made assistant turn and no tool messages before the model's
    assert not [m for m in first if m['role'] in ('assistant', 'tool')]
    reads = [t for t in app.runs.tools(rid) if t['name'] == 'Read' and t['state'] == 'completed']
    assert [t['call_id'] for t in reads] == ['host:prefetch_1', 'host:prefetch_2']
    assert run['state'] == 'completed'  # AgentGuard's grounding check counted the prefetched read of calc.py
    noted = app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='prefetch'", (rid,))
    assert json.loads(noted[0]['payload']) == {'files': 2, 'paths': ['calc.py', 'test_calc.py'], 'named': ['calc.py']}


def test_only_the_named_files_prefetched_teach_memory_facts(env, prefetching):
    # validate_phase15 m2 with prefetch on: a tiny project's dev.yaml was read too, and its port contested prod.yaml's
    (env.project / 'dev.yaml').write_text('port: 8080\n')
    (env.project / 'prod.yaml').write_text('port: 9090\n')
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    env.script = [final('prod.yaml sets port 9090.\n\nFINAL: 9090')]
    app.work(chat, 'Which port does prod.yaml configure? End with `FINAL: <port>`.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, app, rid, 'completed', 1)
    noted = json.loads(app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='prefetch'", (rid,))[0]['payload'])
    assert {'dev.yaml', 'prod.yaml'} <= set(noted['paths']) and noted['named'] == ['prod.yaml']
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not app.store.query("SELECT 1 FROM run_events WHERE run_id=? AND kind='memory_maintenance'", (rid,)):
        time.sleep(.05)
    learned = ' '.join(r['content'] for r in app.store.query("SELECT content FROM memories WHERE origin='auto-learned'"))
    assert 'port: 9090' in learned and 'port: 8080' not in learned, learned


def test_8k_and_smaller_windows_are_unchanged(env, prefetching):
    app = env.start(8192)
    chat = app.new_chat(str(env.project))
    env.script = [call('Read', {'file_path': 'calc.py'}, 'r1'), final('add.\n\nFINAL: calc.py:add')]
    app.work(chat, 'Which function in calc.py adds two numbers? End with `FINAL: <path>:<name>`.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, app, rid, 'completed', 2)  # the model reads calc.py itself
    assert not [m for m in env.requests[0] if m['role'] == 'tool']
    assert not app.store.query("SELECT 1 FROM run_events WHERE run_id=? AND kind='prefetch'", (rid,))


def test_a_re_read_of_an_unchanged_prefetched_file_is_not_sent_twice(env, prefetching):
    # The scripted context-cost plan re-read every prefetched file (test_context_cost_24: median ratio 2.97 with the
    # text sent twice and every Read result's hashes and notes; 2.74 without prefetch and with the compact views)
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    env.script = [call('Read', {'file_path': 'calc.py'}, 'r1'), final('add.\n\nFINAL: calc.py:add')]
    app.work(chat, 'Which function in calc.py adds two numbers? End with `FINAL: <path>:<name>`.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, app, rid, 'completed', 2)
    [shown] = [m for m in env.requests[0] if m.get('name') == 'pebble_prefetch']
    assert '"calc.py": {"total_lines":' in shown['content'] and 'file_hash' not in shown['content']
    [again] = [json.loads(m['content']) for m in env.requests[1] if m['role'] == 'tool']
    assert again == {'path': 'calc.py', 'total_lines': again['total_lines'], 'unchanged': runner.PREFETCH_SEEN}
    [receipt] = [t for t in app.runs.tools(rid) if t['name'] == 'Read' and not t['call_id'].startswith('host:')]
    assert 'def add' in receipt['result']['untrusted_source'] and receipt['result']['file_hash']  # the record is whole


def test_a_changed_file_or_a_prefetch_no_longer_in_the_context_is_shown_whole():
    shown = {'path': 'calc.py', 'total_lines': 2, 'untrusted_source': 'def add(a, b):\n    return a - b\n'}
    note = [{'role': 'user', 'name': 'pebble_prefetch', 'content': 'HOST PREFETCH: ...'}]
    assert runner.unchanged_since_prefetch(shown, {'calc.py': 'def add(a, b):\n    return a + b\n'}, note) is shown
    assert runner.unchanged_since_prefetch(shown, {'calc.py': shown['untrusted_source']}, []) is shown  # compacted away
    assert runner.unchanged_since_prefetch(shown, {'calc.py': shown['untrusted_source']}, note)['unchanged']


def test_a_follow_up_turn_also_gets_what_earlier_turns_read():
    runs = [[{'name': 'Read', 'state': 'completed', 'arguments': {'file_path': 'ibans/batch2.txt'}},
             {'name': 'Read', 'state': 'refused', 'arguments': {'file_path': 'secret.txt'}}],
            [{'name': 'Read', 'state': 'completed', 'arguments': {'file_path': 'ibans/lengths.txt'}},
             {'name': 'RunCode', 'state': 'completed', 'arguments': {'code': 'x'}},
             {'name': 'Read', 'state': 'completed', 'arguments': {'file_path': 'ibans/batch1.txt'}},
             {'name': 'Read', 'state': 'completed', 'arguments': {'file_path': 'ibans/batch2.txt'}}]]
    # newest run first, and within a run its last read first; once each; only completed Reads
    assert prefetch.earlier_reads(runs) == ['ibans/batch2.txt', 'ibans/batch1.txt', 'ibans/lengths.txt']
    assert prefetch.earlier_reads(runs, limit=2) == ['ibans/batch2.txt', 'ibans/batch1.txt']


def test_the_second_turn_starts_with_the_file_the_first_turn_read(env, prefetching):
    (env.project / 'rates.txt').write_text('GB 22\nDE 22\n')  # a document: a tiny project's prefetch leaves it out
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    env.script = [final('Two rates.\n\nFINAL: 2'), final('Still two.\n\nFINAL: 2')]
    app.work(chat, 'How many rates does rates.txt list? End with `FINAL: <n>`.', False, [])
    first = app.runs.list(chat['id'])[0]['id']
    settled(env, app, first, 'completed', 1)
    app.work(app.chat(chat['id']), 'And now? Same last line.', False, [])
    second = app.runs.list(chat['id'])[0]['id']
    assert second != first
    settled(env, app, second, 'completed', 2)
    [shown] = [m for m in env.requests[1] if m.get('name') == 'pebble_prefetch']
    assert '\n"rates.txt": {' in shown['content'] and 'GB 22' in shown['content']
    noted = json.loads(app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='prefetch'", (second,))[0]['payload'])
    assert 'rates.txt' in noted['paths'] and 'rates.txt' not in noted['named']  # read before, not named now: it teaches nothing
