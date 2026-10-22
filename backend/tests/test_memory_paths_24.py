"""Phase 24 t24-memory: remember, forget, contests and deletion coupling (V1-021, -033, -034, -035, -057, -079, -099).

- "Remember that ..." saves one declarative statement and says whether the run continues; recollections,
  questions, reminders and memory-off never fail a run (V1-033).
- Contradicting records the person saved are contested like learned ones: withheld behind the host conflict
  note (which names the person's own values) and listed by memory_contests; a direct correction supersedes (V1-035).
- Deleting a conversation keeps what the person asked Pebble to remember in it (V1-021).
- Conversational forgetting only finds records for the native Forget card; a reply claiming forgetting is
  recognisable for the runner's claim check (V1-034).
- The settings list pages and searches past 300 (V1-099); the working record reads only its run's actions (V1-079);
  forgetting merges the FTS index so forgotten terms leave its segment blocks (V1-057).
No model, network, credential or person's data: providers are scripted fakes.
"""

import itertools
import json
import statistics
import time
import uuid

import pytest

from pebble import eval_corpus_15 as ec15
from pebble import eval_corpus_18 as ec18
from pebble import evaluation, memory_eval, memory_graph
from pebble.app import App
from pebble.memory import KINDS, Memory, MemoryOff, first_statement, forget_request, reply_claims_forgetting
from pebble.tool_catalogue import TOOLS

MIXED = 'Remember that I prefer metric units. Also, how far is 26.2 miles in km?'
SAVED_REPLY = 'Saved this in **your preferences**. You can inspect or forget it in Settings → Memory.'


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield instance
    instance.close()


def memory_save(app, kind, content, **params):
    """App.memory_save with the parent's V1-035 patch: the route's put, then the relation step."""
    ident = app.memory_save(kind, content, **params)
    app.memory.relate_saved(ident)
    return ident


def status(app, ident):
    rows = app.store.query('SELECT status FROM memory_evidence WHERE memory_id=?', (ident,))
    return rows[0]['status'] if rows else None


def recalled(app, text, cid, workspace=None, **options):
    result = app.memory.recall(text, cid, workspace, **options)
    return result['items'], result['diagnostics']


def scripted(app, reply='Here is the answer.'):
    requests = []

    def complete(messages, available, cancel, token, deliberate):
        requests.append(messages)
        token(reply)
        return {'role': 'assistant', 'content': reply}

    app.model.complete = complete
    return requests


# -- V1-033: capture rules ---------------------------------------------------------------------------------------

def test_remember_plus_a_question_saves_only_the_first_statement_and_continues(app):
    chat = app.new_chat()
    captured = app.memory.capture_user(MIXED, chat['id'])
    assert captured['saved'] is True and captured['continue_run'] is True
    assert captured['statement'] == captured['content'] == 'I prefer metric units.' and captured['kind'] == 'profile'
    assert captured['reply'] == 'Saved “I prefer metric units.” in **your preferences**. You can inspect or forget it in Settings → Memory.'
    [stored] = app.memory.list('profile')
    assert stored['content'] == 'I prefer metric units.' and stored['origin'] == 'direct-user' and stored['scope'] == 'global'
    # A single clause keeps the 0.6.1 reply and ends the run (test_companion.py:534, test_usage_run_22.py:222).
    single = app.memory.capture_user('remember that my name is Asha', chat['id'])
    assert single['continue_run'] is False and single['reply'] == SAVED_REPLY and single['content'] == 'my name is Asha'
    later = app.memory.capture_user('Remember that my GP is Dr. Smith. Thanks!', chat['id'])
    assert later['statement'] == 'my GP is Dr. Smith.' and later['continue_run'] is True
    assert first_statement('Python 3.12 is current, e.g. for scripts. Next') == ('Python 3.12 is current, e.g. for scripts.', 'Next')


@pytest.mark.parametrize('text', [
    'Remember when we picked the release date? What did we decide?',
    'Remember how we fixed the parser last week?',
    'Remember what I told you about Lyon?',
    'Remember the time we went to Paris',
    'Remember that time we missed the train?',
    'Remember to call the plumber at 5',
    'Remember to always use metric units',
    'Remember that I prefer metric units?',
    'Do you remember my name?',
    'What should I remember for the exam?'])
def test_recollections_questions_and_reminders_are_not_saved(app, text):
    chat = app.new_chat()
    assert app.memory.capture_user(text, chat['id']) is None
    app.setting('memory_enabled', False)
    assert app.memory.capture_user(text, chat['id']) is None  # memory off never makes these fail
    assert app.memory.list() == [m for m in app.memory.list() if m['origin'] == 'curated']


def test_memory_off_raises_a_fall_through_result_and_stores_nothing(app):
    chat = app.new_chat()
    before = app.memory.list()
    app.setting('memory_enabled', False)
    with pytest.raises(ValueError, match='Memory is off') as raised:  # test_companion.py:559 still holds
        app.memory.capture_user(MIXED, chat['id'])
    assert isinstance(raised.value, MemoryOff)
    assert raised.value.result['saved'] is False and raised.value.result['continue_run'] is True
    assert raised.value.result['statement'] == 'I prefer metric units.' and 'did' in raised.value.result['reply']
    app.setting('memory_enabled', True)
    app.setting('memory_layers', {kind: kind != 'profile' for kind in KINDS})
    with pytest.raises(MemoryOff, match='Memory is off for this layer'):
        app.memory.capture_user('Remember that I like tea.', chat['id'])
    assert app.memory.list() == before


@pytest.mark.parametrize('enabled', [True, False])
def test_a_remember_when_question_reaches_the_model_and_the_run_completes(app, enabled):
    requests = scripted(app, 'You chose 14 November.')
    app.setting('memory_enabled', enabled)
    chat = app.new_chat()
    app.work(chat, 'Remember when we picked the release date? What did we decide?', False, [])
    assert len(requests) == 1
    assert app.chat(chat['id'])['messages'][-1]['content'] == 'You chose 14 November.'
    [run] = app.store.query('SELECT state FROM runs WHERE chat_id=?', (chat['id'],))
    assert run['state'] == 'completed'
    assert not [m for m in app.memory.list() if m['origin'] == 'direct-user']


# -- V1-035: person-saved contradictions and corrections -----------------------------------------------------------

def test_person_saved_contradictions_are_withheld_and_listed_by_memory_contests(app):
    chat = app.new_chat()
    alex = memory_save(app, 'profile', 'My name is Alex.')
    sam = memory_save(app, 'profile', 'My name is Sam.')
    assert status(app, alex) == status(app, sam) == 'contested'
    for window in (4096, 32768):
        items, diagnostics = recalled(app, 'Do you know my name?', chat['id'], window=window)
        assert not {alex, sam} & {i.get('id') for i in items}
        [note] = [i for i in items if i.get('note') == 'conflict']
        assert sorted(note['memory_ids']) == sorted([alex, sam]) and diagnostics['conflict_note'] is True
        # The person's own values, older first, as disputed; at 4K the note stays content-free (nothing new).
        assert note.get('disputed') == (None if window == 4096 else [{'topic': 'name', 'values': ['Alex', 'Sam']}])
        if window == 4096:
            assert 'Alex' not in json.dumps(items) and 'Sam' not in json.dumps(items)
    groups = app.memory_contests()['groups']  # one group per contested record, each naming the other
    assert {(g['memory_id'], tuple(c['memory_id'] for c in g['counterparts'])) for g in groups} == {(alex, (sam,)), (sam, (alex,))}
    assert all(g['origin'] == 'user' and g['kind'] == 'profile' for g in groups)
    # Only the person's native resolution clears it.
    app.learning.consolidate()
    assert status(app, alex) == status(app, sam) == 'contested'
    app.memory_resolve_contest(sam, 'keep')
    assert app.memory_contests()['groups'] == [] and not app.store.query('SELECT 1 FROM memories WHERE id=?', (alex,))
    items, _ = recalled(app, 'Do you know my name?', chat['id'], window=4096)
    assert sam in {i.get('id') for i in items} and not [i for i in items if i.get('note') == 'conflict']


def test_claim_set_contested_seeds_are_withheld_but_their_values_stay_visible_as_disputed(app):
    task = ec18.BY_ID['memory-contested']
    ids = {content: memory_save(app, layer, content) for layer, content, _, _, _ in task.memory}
    alarm = [ident for content, ident in ids.items() if 'alarm code is 30' in content]
    assert len(alarm) == 2 and all(status(app, i) == 'contested' for i in alarm)
    assert {g['memory_id'] for g in app.memory_contests()['groups']} == set(alarm)
    chat = app.new_chat()
    for window in (4096, 32768, 131072):
        items, diagnostics = recalled(app, task.prompt, chat['id'], window=window)
        sent = json.dumps(items, ensure_ascii=False)
        assert not set(alarm) & {i.get('id') for i in items} and len(sent) <= diagnostics['budget'] + 200
        [note] = [i for i in items if i.get('note') == 'conflict']
        if window == 4096:
            assert 'disputed' not in note and '3071' not in sent and '3017' not in sent
            continue
        # test_eval_live_shapes_18 MEMORY_SEEN (scripted window 32768): the model still sees 3071, 3017 and 19:30.
        assert '3071' in sent and '3017' in sent and '19:30' in sent, window
        assert note['disputed'] == [{'topic': 'Kiln studio: alarm code', 'values': ['3071', '3017']}]


def test_only_the_claim_sets_intended_conflicts_become_contests(app):
    seeds = {'memory-contested': None, 'memory-conflict': None}
    tasks = [(t.id, [(layer, content) for layer, content, _, _, _ in t.memory]) for t in ec18.CORPUS if t.memory]
    tasks += [(t.id, [('semantic', content) for content in t.memory]) for t in ec15.CORPUS if t.memory]
    tasks += [(c.id, [('semantic', content) for content in c.memory]) for c in evaluation.CORPUS if c.memory]
    assert {'memory-contested', 'memory-conflict', 'memory-heron'} <= {name for name, _ in tasks}
    for name, memory in tasks:
        for layer, content in memory:
            if layer in ('semantic', 'profile'):
                app.memory.relate_saved(app.memory.put(layer, content))
        contested = sorted(r['content'] for r in app.store.query(
            "SELECT m.content FROM memories m JOIN memory_evidence e ON e.memory_id=m.id WHERE e.status='contested'"))
        if name in seeds:
            assert len(contested) == 2 and len({memory_graph.person_claim(c)['key'] for c in contested}) == 1, name
        else:
            assert contested == [], name
        app.memory_clear()


def test_noise_seeded_claim_set_recall_notes_only_the_intended_contest(app):
    # The claim set seeds 300 noise notes around each memory task (eval_corpus_18._seeded). Their repeated
    # subjects ("X office: the supply cupboard code is N") are real person-saved contradictions and are
    # contested too, but none matches a graded question, so only the intended contest is noted.
    for task_id in ('memory-contested', 'memory-forgotten', 'memory-format', 'memory-ports'):
        task = ec18.BY_ID[task_id]
        for layer, content, _, _, _ in ec18._seeded(task):
            if layer in ('semantic', 'profile'):
                memory_save(app, layer, content)
        assert app.store.query("SELECT count(*) AS n FROM memory_evidence WHERE status='contested'")[0]['n'] > 2
        chat = app.new_chat()
        items, _ = recalled(app, task.prompt, chat['id'], window=32768)
        notes = [i for i in items if i.get('note') == 'conflict']
        sent = json.dumps(items, ensure_ascii=False)
        if task_id == 'memory-contested':
            assert [(n['withheld'], n['disputed']) for n in notes] == [
                (2, [{'topic': 'Kiln studio: alarm code', 'values': ['3071', '3017']}])]
            assert '3071' in sent and '3017' in sent and '19:30' in sent
        else:
            assert notes == [], task_id
        assert all(v.casefold() in sent.casefold() for v in
                   {'memory-forgotten': ('2580',), 'memory-format': ('7:15 pm', '85 euros'),
                    'memory-ports': ('7311', '7312', '7340'), 'memory-contested': ()}[task_id])
        app.memory_clear()


def test_person_claim_grammar_is_narrow():
    claim = memory_graph.person_claim
    assert claim('my name is Alex')['key'] == claim('My name is ALEX.')['key'] == 'person|me|name'
    assert claim('my name is Alex')['value'] == claim('My name is alex.')['value'] != claim('My name is Sam.')['value']
    assert claim("Kiln studio: the alarm code is 3071 (from the landlord's letter, 2026-09-02).")['shown'] == '3071'
    assert claim("My wife's name is Priya, and she is a doctor.")['label'] == "wife's name"
    for text in ('I prefer metric units.', 'Kiln studio: the kiln firing slot starts at 18:00 (booked 2026-05-10).',
                 'Project Heron: before the September 2026 migration the staging server port was 7412; it is retired.',
                 'Preference: my staging deploys use port 8899.', 'My name is not Alex.', 'my name is Alex\nand more',
                 'Choir: the spring term fee was 80 euros.', 'Programme Halcyon: the ingest service listens on port 7311.'):
        assert claim(text) is None, text


def test_a_direct_correction_supersedes_the_older_value_and_says_updated(app):
    chat = app.new_chat()
    alex = app.memory.capture_user('Remember that my name is Alex.', chat['id'])
    sam = app.memory.capture_user('Remember that my name is Sam.', chat['id'])
    assert sam['relation'] == 'updated' and sam['reply'] == 'Updated your saved name. You can inspect or forget it in Settings → Memory.'
    assert status(app, alex['id']) == 'superseded' and status(app, sam['id']) == 'active'
    assert app.store.query('SELECT superseded_by FROM memory_evidence WHERE memory_id=?', (alex['id'],))[0]['superseded_by'] == sam['id']
    other = app.new_chat()
    items, _ = recalled(app, 'Do you know my name?', other['id'], window=4096)
    ids = {i.get('id') for i in items}
    assert sam['id'] in ids and alex['id'] not in ids and not [i for i in items if i.get('note') == 'conflict']
    assert app.memory_contests()['groups'] == []
    # Saying the old value again makes it current again; forgetting it removes the version it superseded.
    again = app.memory.capture_user('Remember that my name is Alex.', chat['id'])
    assert again['id'] == alex['id'] and again['relation'] == 'updated'
    assert status(app, alex['id']) == 'active' and status(app, sam['id']) == 'superseded'
    app.memory_delete(alex['id'])
    assert not app.store.query("SELECT 1 FROM memories WHERE origin='direct-user'")


def test_a_direct_correction_ends_a_person_saved_contest(app):
    chat = app.new_chat()
    alex = memory_save(app, 'profile', 'My name is Alex.')
    sam = memory_save(app, 'profile', 'My name is Sam.')
    fixed = app.memory.capture_user('Remember that my name is Sam.', chat['id'])
    assert fixed['relation'] == 'updated' and status(app, alex) == 'superseded' and status(app, sam) == 'active'
    assert app.memory_contests()['groups'] == []
    items, _ = recalled(app, 'Do you know my name?', chat['id'], window=4096)
    names = [i for i in items if i.get('kind') == 'profile']
    assert names and all('Sam' in i['content'] for i in names)


def test_a_settings_save_never_supersedes_and_other_scopes_contest(app, tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    project = app.new_chat(str(root))
    capture = app.memory.capture_user('Remember that Kiln studio: the alarm code is 3071.', project['id'], str(root))
    assert capture['kind'] == 'semantic' and status(app, capture['id']) is None
    settings = memory_save(app, 'semantic', 'Kiln studio: the alarm code is 3017.')
    assert status(app, capture['id']) == 'contested'  # its global counterpart is visible in the project
    assert status(app, settings) == 'active'  # elsewhere the project record is not visible
    items, _ = recalled(app, 'Kiln studio alarm code', project['id'], str(root), window=32768)
    assert not {capture['id'], settings} & {i.get('id') for i in items}


# -- V1-021: deleting a conversation keeps explicit memories ------------------------------------------------------

def test_delete_chat_keeps_an_explicit_global_preference_and_recalls_it_elsewhere(app, tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    chat = app.new_chat()
    project = app.new_chat(str(root))
    allergy = app.memory.capture_user('Remember that I am allergic to peanuts.', chat['id'])
    fact = app.memory.capture_user('Remember that the staging host is pebble-stage-2.', project['id'], str(root))
    episode = app.memory.put('episodic', '{"user_request": "peanut-free recipes"}', 'chat:' + chat['id'], chat['id'],
                             origin='conversation')
    linked = app.memory.put('semantic', 'Project Orion uses SQLite', scope=str(root), chat_id=project['id'])
    app.delete_chat(chat['id'])
    app.delete_chat(project['id'])
    kept = {m['id']: m for m in app.memory.list()}
    assert allergy['id'] in kept and kept[allergy['id']]['chat_id'] is None and kept[allergy['id']]['scope'] == 'global'
    assert fact['id'] in kept and kept[fact['id']]['scope'] == str(root)
    # Conversation records, and chat-linked rows that are not "Remember that" captures, leave as in 0.6.1.
    assert episode not in kept and linked not in kept
    other = app.new_chat()
    items, _ = recalled(app, 'Am I allergic to anything?', other['id'], window=32768)
    assert allergy['id'] in {i.get('id') for i in items}
    again = app.new_chat(str(root))
    items, _ = recalled(app, 'What is the staging host?', again['id'], str(root), window=32768)
    assert fact['id'] in {i.get('id') for i in items}


# -- V1-034: conversational forgetting is a native card -----------------------------------------------------------

def test_forget_candidates_find_the_record_for_please_forget_my_name(app):
    chat = app.new_chat()
    name = app.memory.capture_user('Remember that my name is Alex.', chat['id'])
    app.memory.capture_user('Remember that I prefer metric units.', chat['id'])
    app.memory.put('semantic', 'The office plant needs water on Fridays.')
    before = app.store.query('SELECT count(*) AS n FROM memories')[0]['n']
    assert forget_request('Please forget my name.') == 'my name'
    found = app.memory.forget_candidates('Please forget my name.', chat['id'])
    assert found[0]['id'] == name['id'] and found[0]['authorization'] == 'none' and found[0]['where'] == 'all'
    assert found[0]['layer'] == 'Your preferences' and 'Alex' in found[0]['content']
    # 'Forget that.' names no subject: the records the person saved in this conversation, newest first.
    assert forget_request('Forget that.') == ''
    assert [r['id'] for r in app.memory.forget_candidates('Forget that.', chat['id'])][-1] == name['id']
    preview = app.memory.forget_preview(ids=[name['id']])
    assert [r['id'] for r in preview['items']] == [name['id']] and preview['authorization'] == 'none'
    assert app.memory.forget_preview('Can you forget my name please?', chat=chat['id'])['items'][0]['id'] == name['id']
    assert app.store.query('SELECT count(*) AS n FROM memories')[0]['n'] == before  # a read: nothing is forgotten
    assert app.memory.forget_candidates('Forget my name.', 'no-such-chat') == []


@pytest.mark.parametrize('text,subject', [
    ('Please forget my name.', 'my name'), ('forget that I am allergic to peanuts', 'I am allergic to peanuts'),
    ('Stop remembering my birthday', 'my birthday'), ("Don't remember this.", ''), ('Forget about it', ''),
    ('Delete my address from your memory.', 'my address'), ('Pebble, could you forget my old phone number?', 'my old phone number'),
    ('Forget it, what is the weather in Lyon?', None), ('Please forget my name. Then tell me a joke.', None),
    ('Did you forget my name?', None), ('Forgetting things is normal.', None), ('Delete the file notes.txt', None)])
def test_forget_requests_are_recognised_narrowly(text, subject):
    assert forget_request(text) == subject


@pytest.mark.parametrize('text,claims', [
    ("Done. I've forgotten your name.", True), ('I have deleted that from my memory.', True),
    ("OK, I won't remember that anymore.", True), ('Your name has been removed from memory.', True),
    ('Consider it forgotten.', True), ("I'll forget your name.", True), ('Done — forgotten.', True),
    ('Your name has been forgotten.', True), ('Memory deleted.', True),
    ("Don't forget to call the plumber.", False), ('I forgot to mention the tax.', False),
    ("I can't delete memories myself; use the Forget card.", False), ('I have not forgotten your name.', False),
    ('I deleted the temporary file.', False), ('Have you forgotten your password?', False),
    ("I'll remember that.", False), ("I don't remember that conversation.", False),
    ('Pebble shows a Forget card where you can remove it.', False)])
def test_reply_claims_forgetting(text, claims):
    assert reply_claims_forgetting(text) is claims
    assert Memory.reply_claims_forgetting(text) is claims


def test_conversational_forget_adds_no_model_tool():
    names = {t['function']['name'] for t in TOOLS}
    assert not [n for n in names if 'forget' in n.casefold() or 'memorydelete' in n.casefold()]
    assert 'MemoryPropose' in names


# -- V1-099: paging and search ------------------------------------------------------------------------------------

def test_list_paging_and_search_reach_the_320th_record(app):
    now = time.time()
    rows = [('note-%03d' % i, 'semantic', 'Fixture note %d about topic%03d.' % (i, i), 'global', None, 'user',
             now - 10_000 + i, now - 10_000 + i, None) for i in range(320)]
    with app.store.lock:
        app.store.db.executemany('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)', rows)
        app.store.db.commit()
    first = app.memory.list('semantic')
    assert len(first) == 300 and first[0]['id'] == 'note-319'  # 0.6.1's default page
    rest = app.memory.list('semantic', offset=300)
    assert [m['id'] for m in rest] == ['note-%03d' % i for i in range(19, -1, -1)]
    assert {m['id'] for m in first} | {m['id'] for m in rest} == {r[0] for r in rows}
    assert [m['id'] for m in app.memory.list('semantic', query='topic000')] == ['note-000']
    assert [m['id'] for m in app.memory.list(query='fixture TOPIC00')][:2] == ['note-009', 'note-008']
    assert len(app.memory.list('semantic', query='fixture', limit=50, offset=300)) == 20
    assert app.memory.list('profile', query='topic000') == []
    assert [m['id'] for m in app.memory.list(query='.')] == [m['id'] for m in app.memory.list() if '.' in m['content']][:300]
    for bad in ({'offset': -1}, {'limit': 0}, {'limit': 301}, {'query': 'x' * 201}, {'offset': '1'}):
        with pytest.raises(ValueError):
            app.memory.list(**bad)


# -- V1-079: the working record reads only its run's actions -------------------------------------------------------

def test_working_state_at_1000_actions_reads_only_run_owned_actions(app):
    chat = app.new_chat()
    old = app.runs.create(chat['id'], 'Earlier edits', app.runtime_config(chat, False))
    app.runs.transition(old, 'running'); app.runs.transition(old, 'completed')
    now, blob = time.time(), 'x' * 20_000
    history = [(uuid.uuid4().hex, chat['id'], 'edit', 'applied',
                json.dumps({'path': 'f%d.py' % i, 'content': blob, 'result': {'applied': True}}), now - 5000 + i)
               for i in range(1000)]
    with app.store.lock:
        app.store.db.executemany('INSERT INTO actions VALUES(?,?,?,?,?,?)', history)
        app.store.db.executemany('INSERT INTO run_action_owners VALUES(?,?,?,?)',
                                 [(row[0], old, 'c%d' % i, 'edit') for i, row in enumerate(history)])
        app.store.db.commit()
    rid = app.runs.create(chat['id'], 'Fix the parser', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    edit = app.store.action(chat['id'], 'edit', {'path': 'parser.py'})
    command = app.store.action(chat['id'], 'command', {'path': None})
    app.store.query('INSERT INTO run_action_owners VALUES(?,?,?,?)', (edit, rid, 'e1', 'edit'))
    app.runs.begin_tool(rid, 'e2', 'RunCommand', {})
    app.runs.end_tool(rid, 'e2', {'status': 'awaiting_user_approval', 'action_id': command})
    statements = []
    app.store.db.set_trace_callback(statements.append)
    try:
        state = app.memory.working_state(rid)
    finally:
        app.store.db.set_trace_callback(None)
    assert state['pending_action_ids'] == [edit, command]
    assert [a['path'] for a in state['actions']] == ['parser.py', None]
    reads = [s for s in statements if 'FROM actions' in s]
    assert reads and all(' id IN (' in s for s in reads), reads  # never the chat's whole history
    timings = []
    for _ in range(7):
        started = time.perf_counter()
        app.memory.working_state(rid)
        timings.append(time.perf_counter() - started)
    with app.store.lock:
        started = time.perf_counter()
        app.store.db.execute('SELECT id,kind,status,payload FROM actions WHERE chat_id=? ORDER BY created,id',
                             (chat['id'],)).fetchall()
        whole = time.perf_counter() - started
    print('V1-079 working_state median %.2f ms at 1,000 actions; the 0.6.1 whole-chat read alone %.2f ms'
          % (statistics.median(timings) * 1000, whole * 1000))
    assert statistics.median(timings) < 0.05


# -- V1-057: forgotten terms leave the FTS segments ----------------------------------------------------------------

def blocks_holding(app, term):
    with app.store.lock:
        return sum(term.encode() in bytes(block) for (block,) in app.store.db.execute('SELECT block FROM memory_search_data'))


def test_after_a_forget_batch_and_optimize_no_fts_segment_block_holds_the_term(app, monkeypatch):
    ids = [app.memory.put('semantic', 'qqzcanary7731 locker note %d' % i) for i in range(6)]
    app.memory.put('semantic', 'Unrelated note about green tea')
    assert blocks_holding(app, 'qqzcanary7731')
    for ident in ids[:-1]:
        app.learning.forget(ident, compact=False)  # a batch: the index keeps the term until it is merged
    assert blocks_holding(app, 'qqzcanary7731')
    app.memory_delete(ids[-1])  # the route forgets and merges
    assert blocks_holding(app, 'qqzcanary7731') == 0
    assert [m['content'] for m in app.memory.list(query='green tea')] == ['Unrelated note about green tea']
    # A deleted conversation and a cleared store merge too; without the merge the term would stay.
    chat = app.new_chat()
    app.memory.put('episodic', '{"user_request": "qqzepisode5521"}', 'chat:' + chat['id'], chat['id'], origin='conversation')
    app.delete_chat(chat['id'])
    assert blocks_holding(app, 'qqzepisode5521') == 0
    monkeypatch.setattr(Memory, 'compact_search', lambda self: False)
    kept = app.memory.put('semantic', 'qqzcleared9902 note')
    app.memory_delete(kept)
    assert blocks_holding(app, 'qqzcleared9902')  # the 0.6.1 behaviour this test guards against
    monkeypatch.undo()
    app.memory_clear()
    assert blocks_holding(app, 'qqzcleared9902') == 0


def test_merge_cost_at_20000_records(app):
    rows = [(uuid.uuid4().hex, 'semantic', 'Synthetic record %d about project %d and port %d.' % (i, i % 97, 7000 + i),
             'global', None, 'user', 1000.0 + i, 1000.0 + i, None) for i in range(20_000)]
    with app.store.lock:
        app.store.db.executemany('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)', rows)
        app.store.db.commit()
    app.learning.forget(rows[0][0], compact=False)
    started = time.perf_counter()
    assert app.memory.compact_search() is True
    elapsed = time.perf_counter() - started
    print('V1-057 FTS optimize at 20,000 records: %.1f ms' % (elapsed * 1000))
    assert elapsed < 5


def test_mr1_report_is_identical_with_and_without_the_merge(monkeypatch):
    def ticking():
        counter = itertools.count()
        return lambda: next(counter) * 0.001

    merged = memory_eval.evaluate(15, noise_levels=(30,), windows=(4096,), clock=ticking())
    monkeypatch.setattr(Memory, 'compact_search', lambda self: False)
    plain = memory_eval.evaluate(15, noise_levels=(30,), windows=(4096,), clock=ticking())
    assert merged['deterministic_sha256'] == plain['deterministic_sha256']
    assert all(merged['gates'].values())
