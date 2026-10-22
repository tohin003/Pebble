"""Phase 22 (t22-stream): the run_activity route and live `activity` events (the shared activity contract).

Items are built from host records only: run events, tool records, the action ledger, agent nodes and stream
attempts. They never carry the private checkpoint, a held or withdrawn draft, a prompt or a credential, and every
string is plain display text. The same item has the same seq in the live event and the route, so a window can merge
either. The real App, runner, broker and journals run; only the root model is scripted.
"""

import json
import re
import time

import pytest

from pebble.recovery import ProviderFailure
from pebble.run_activity import KINDS, ActivityFeed
from test_reasoning_summary_22 import answer, env, failing, no_network, settle, tool  # noqa: F401 (fixtures)

CONTRACT = {'run_id', 'state', 'started', 'elapsed', 'items', 'reasoning', 'usage', 'next'}
ITEM = {'seq', 'at', 'kind', 'label', 'detail', 'duration', 'status'}
MODEL_ITEM = ITEM | {'headers_ms', 'first_event_ms'}  # model items also carry the attempt's time to headers and first event


def activity(app, rid, after=0):
    return app.dispatch('run_activity', {'run_id': rid, 'after': after})


def well_formed(view):
    assert set(view) == CONTRACT and isinstance(view['run_id'], str) and isinstance(view['state'], str)
    assert type(view['started']) is float and type(view['elapsed']) is float and view['elapsed'] >= 0
    assert set(view['reasoning']) == {'available', 'text'} and len(view['reasoning']['text']) <= 20000
    assert type(view['next']) is int
    seqs = [i['seq'] for i in view['items']]
    assert seqs == sorted(set(seqs))
    for item in view['items']:
        assert set(item) == (MODEL_ITEM if item['kind'] == 'model' else ITEM), item
        assert type(item['seq']) is int and type(item['at']) is float and item['at'] >= 0
        for field in set(item) - ITEM:
            assert item[field] is None or type(item[field]) is int and item[field] >= 0
        assert item['kind'] in KINDS and item['status'] in ('running', 'done', 'failed')
        assert isinstance(item['label'], str) and 0 < len(item['label']) <= 80 and '\n' not in item['label']
        assert item['detail'] is None or isinstance(item['detail'], str) and len(item['detail']) <= 2000
        assert item['duration'] is None or type(item['duration']) is float and item['duration'] >= 0
        assert not re.search(r'[\x00-\x08\x0b-\x1f\x7f]', item['label'] + (item['detail'] or ''))
    return view


def live_items(env, rid):
    """The latest live `activity` item per seq for one run."""
    found = {}
    for name, data in env.events:
        if name == 'activity' and data['run_id'] == rid:
            assert set(data) == {'chat_id', 'run_id', 'item'}
            found[data['item']['seq']] = data['item']
    return found


def test_an_unknown_run_is_refused_like_run_get(env):
    app = env.start()
    cid = app.new_chat(None)['id']
    with pytest.raises(ValueError, match='^Run not found in this conversation.$'):
        activity(app, 'not-a-run')
    with pytest.raises(ValueError, match='^Run not found in this conversation.$'):
        app.dispatch('run_get', {'id': cid, 'run_id': 'not-a-run'})
    env.script = [answer('Hello.')]
    app.work(app.chat(cid), 'hi', False, [])
    rid = app.runs.list(cid)[0]['id']
    for bad in (-1, '3', 1.5, True):
        with pytest.raises(ValueError, match='cursor'):
            activity(app, rid, bad)


def test_a_tool_run_reads_as_context_model_tool_model_check_and_matches_its_live_events(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    env.script = [answer('Reading the source first.', calls=tool('Read', {'file_path': 'calc.py'}, 'read')),
                  answer('add returns the sum of a and b.')]
    app.work(chat, 'What does calc.py do?', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    view = well_formed(activity(app, rid))
    run = app.runs.get(rid)
    assert view['state'] == 'completed' and view['started'] == run['created'] and view['usage'] is None
    assert [(i['kind'], i['status']) for i in view['items']] == [
        ('context', 'done'), ('model', 'done'), ('tool', 'done'), ('model', 'done'), ('check', 'done')]
    context, first, read, second, check = view['items']
    assert context['label'] in ('Context ready',) or context['label'].startswith('Recalled ')
    assert context['detail'].startswith('Context ready in ') and context['detail'].endswith(' ms')
    assert first['label'] == 'The model responded' and second['label'] == 'The model responded'
    assert read['label'] == 'Reading calc.py' and read['duration'] is not None
    assert check['label'] == 'Checked the reply'
    # Stable: the same items on every read, and the live events ended on exactly these items.
    assert activity(app, rid)['items'] == view['items']
    live = live_items(env, rid)
    assert {seq: live[seq] for seq in live} == {i['seq']: i for i in view['items']}
    # The cursor: nothing after the last item, and the cursor stays put.
    assert activity(app, rid, view['next'])['items'] == [] and activity(app, rid, view['next'])['next'] == view['next']
    assert [i['seq'] for i in activity(app, rid, first['seq'])['items']] == [i['seq'] for i in view['items'][2:]]


def test_recalled_memories_are_counted_with_the_context_time(env):
    app = env.start()
    app.memory.put('profile', 'My name is Asha.')
    chat = app.new_chat(None)
    env.script = [answer('Yes, you are Asha.')]
    app.work(chat, 'do you know my name', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    [context] = [i for i in activity(app, rid)['items'] if i['kind'] == 'context']
    found = re.fullmatch(r'Recalled (\d+) memor(y|ies)', context['label'])
    assert found and int(found[1]) >= 1 and context['duration'] < 5
    assert 'Asha' not in json.dumps(activity(app, rid))  # a count, never the memory's content


def test_an_approval_waits_then_reads_as_decided_and_no_held_text_or_prompt_appears(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    early, after = 'HELD-DRAFT-TEXT notes.md is staged.\nFINAL: written', 'notes.md is written.\nFINAL: written'
    goal = 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.'
    env.script = [answer(None, calls=tool('Write', {'file_path': 'notes.md', 'content': 'PRIVATE-CONTENT\n'}, 'write')),
                  answer(early), answer(after)]
    app.work(chat, goal, False, [])
    rid = app.runs.list(cid)[0]['id']
    waiting = well_formed(activity(app, rid))
    [approval] = [i for i in waiting['items'] if i['kind'] == 'approval']
    assert approval == {**approval, 'label': 'Waiting for your approval: Drafting notes.md', 'status': 'running',
                        'duration': None}
    notes = [i['label'] for i in waiting['items'] if i['kind'] == 'note']
    assert notes == ['Waiting for your decision; the reply follows it', 'Waiting for your approval']
    # Running items are always returned, whatever the cursor.
    assert [i['seq'] for i in activity(app, rid, waiting['next'])['items']] == [approval['seq']]
    for private in ('HELD-DRAFT-TEXT', 'PRIVATE-CONTENT', goal, 'Host task checkpoint', 'HOST'):
        assert private not in json.dumps(waiting), private
    action = next(a for a in app.store.actions(cid) if a['status'] == 'pending')
    app.approve(cid, action['id'])
    settle(app, rid, 'completed')
    decided = well_formed(activity(app, rid))
    [approval] = [i for i in decided['items'] if i['kind'] == 'approval']
    assert approval['label'] == 'Approved: Drafting notes.md' and approval['status'] == 'done'
    assert approval['duration'] is not None
    assert 'HELD-DRAFT-TEXT' not in json.dumps(decided) and 'PRIVATE-CONTENT' not in json.dumps(decided)


def test_a_transient_failure_reads_as_a_retry_item(env):
    app = env.start()
    chat = app.new_chat(None)

    def overloaded(token, env):
        raise ProviderFailure('Provider HTTP 503: overloaded', retryable=True, status=503)

    env.script = [overloaded, answer('Hello again.')]
    app.work(chat, 'hi', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    view = well_formed(activity(app, rid))
    assert [(i['kind'], i['status']) for i in view['items'] if i['kind'] in ('model', 'retry')] == [
        ('model', 'failed'), ('retry', 'done'), ('model', 'done')]
    [retry] = [i for i in view['items'] if i['kind'] == 'retry']
    assert retry['label'] == 'Reconnecting to the model (attempt 2)' and retry['detail'] == 'The model returned HTTP 503.'
    assert retry['duration'] is not None and retry['duration'] <= 8
    assert [i['label'] for i in view['items'] if i['kind'] == 'model'] == ['The model request failed',
                                                                            'The model responded (attempt 2)']


def test_children_read_from_their_agent_nodes(env):
    app = env.start()
    chat = app.new_chat(None)
    env.script = [answer('Done.')]
    app.work(chat, 'hi', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    now = time.time()
    with app.store.lock, app.store.db as db:
        for ident, name, status, elapsed in (('n1', 'researcher', 'running', 0.0), ('n2', 'checker', 'completed', 4.5),
                                             ('n3', 'stopped-one', 'stopped', 0.0)):
            db.execute('INSERT INTO agent_nodes(id,run_id,parent_id,name,task,context,tools,limits,dependencies,'
                       'write_paths,depth,generation,status,result,evidence_ids,elapsed,created,updated) '
                       "VALUES(?,?,?,?,?,'{}','[]','{}','[]','[]',1,1,?,NULL,'[]',?,?,?)",
                       (ident, rid, None, name, 'PRIVATE-TASK', status, elapsed, now, now + 2))
    children = [(i['label'], i['status'], i['duration']) for i in well_formed(activity(app, rid))['items']
                if i['kind'] == 'child']
    assert children == [('Agent researcher', 'running', None), ('Agent checker', 'done', 4.5),
                        ('Agent stopped-one', 'failed', 2.0)]
    assert 'PRIVATE-TASK' not in json.dumps(activity(app, rid))


def test_an_attempt_open_when_the_service_stopped_reads_as_interrupted(env):
    app = env.start()
    cid = app.new_chat(None)['id']
    rid = app.runs.create(cid, 'goal text', app.runtime_config(app.chat(cid), False))
    app.runs.transition(rid, 'running')
    app.runs.note(rid, 'model_attempt_started', {'step': 1, 'attempt': 1})
    running = activity(app, rid, after=10_000)
    assert [(i['kind'], i['label'], i['status']) for i in running['items']] == [('model', 'Waiting for the model', 'running')]
    app.runs.transition(rid, 'interrupted')
    view = well_formed(activity(app, rid))
    assert [(i['kind'], i['label'], i['status']) for i in view['items']] == [
        ('model', 'Interrupted while waiting for the model', 'failed'), ('note', 'Interrupted when Pebble closed', 'done')]


def test_a_slow_connect_reads_as_its_own_item_live_and_after_the_attempt(env):
    app = env.start()
    cid = app.new_chat(None)['id']
    rid = app.runs.create(cid, 'goal text', app.runtime_config(app.chat(cid), False))
    app.runs.transition(rid, 'running')
    app.runs.note(rid, 'model_attempt_started', {'step': 1, 'attempt': 1})
    app.runs.note(rid, 'model_connecting', {'step': 1, 'attempt': 1})  # the provider's notice after a second
    live = well_formed(activity(app, rid))['items']
    assert [(i['label'], i['status']) for i in live] == [('Waiting for the model', 'running'),
                                                         ('Connecting to the model…', 'running')]
    assert live[1]['seq'] == live[0]['seq'] + 1
    app.runs.note(rid, 'model_attempt', {'step': 1, 'attempt': 1, 'outcome': 'completed', 'seconds': 4.2,
                                         'headers': 2.5, 'first_event': 2.6, 'first_token': 3.9, 'sent': True})
    done = well_formed(activity(app, rid))['items']
    assert [(i['label'], i['status'], i['duration']) for i in done] == [
        ('The model responded', 'done', 4.2), ('Connected to the model in 2.5 s', 'done', 2.5)]
    assert (done[0]['headers_ms'], done[0]['first_event_ms']) == (2500, 2600) and done[1]['headers_ms'] == 2500
    assert done[0]['detail'] == 'Headers after 2.5 s; first event after 2.6 s; first text after 3.9 s.'
    # A fast connect (under a second) gets no item; the attempt's fields still say how fast it was.
    app.runs.note(rid, 'model_attempt_started', {'step': 2, 'attempt': 1})
    app.runs.note(rid, 'model_attempt', {'step': 2, 'attempt': 1, 'outcome': 'completed', 'seconds': 1.0,
                                         'headers': 0.3, 'first_event': 0.31})
    fast = [i for i in activity(app, rid)['items'] if i['seq'] > done[1]['seq']]
    assert [(i['label'], i['headers_ms'], i['first_event_ms']) for i in fast] == [('The model responded', 300, 310)]


def test_get_chat_tags_each_reply_with_the_run_that_produced_it(env):
    app = env.start()
    chat = app.new_chat(None)
    cid = chat['id']
    env.script = [answer('First reply.'), answer('Second reply.')]
    for text in ('hi', 'hello again'):
        app.store.message(cid, 'user', text)  # as send() stores it; app.work runs the turn itself
        time.sleep(0.01)
        app.work(app.chat(cid), text, False, [])
        time.sleep(0.01)
    first, second = sorted(app.runs.list(cid), key=lambda r: r['created'])
    messages = app.chat(cid)['messages']
    assert [(m['role'], m.get('run_id')) for m in messages] == [
        ('user', None), ('assistant', first['id']), ('user', None), ('assistant', second['id'])]
    app.close()
    reopened = env.start()  # a cold cache reads the same tags from the runs' records
    assert [m.get('run_id') for m in reopened.chat(cid)['messages']] == [None, first['id'], None, second['id']]


def test_after_a_restart_the_timeline_and_the_reasoning_item_remain_but_the_summary_text_does_not(env):
    app = env.start()
    chat = app.new_chat(None)
    env.script = [answer('Your name is Asha.', ['A summary that lives in memory only.'])]
    app.work(chat, 'do you know my name', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    before = activity(app, rid)
    assert before['reasoning']['available'] is True
    app.close()
    reopened = env.start()
    after = well_formed(activity(reopened, rid))
    assert after['items'] == before['items']  # derived from the host's records, so identical
    assert after['reasoning'] == {'available': False, 'text': ''}


def test_reading_a_long_runs_timeline_stays_fast(env):
    app = env.start()
    cid = app.new_chat(None)['id']
    rid = app.runs.create(cid, 'long run', app.runtime_config(app.chat(cid), False))
    with app.store.lock, app.store.db as db:
        for index in range(300):
            call = '%d:c%d' % (index + 1, index)
            db.execute("INSERT INTO run_tools VALUES(?,?,?,?,?,?)", (rid, call, 'Read',
                       json.dumps({'file_path': 'src/file_%d.py' % index}), 'completed', json.dumps({'status': 'ok'})))
            for kind, payload in (('model_attempt_started', {'step': index + 1, 'attempt': 1}),
                                  ('model_attempt', {'step': index + 1, 'attempt': 1, 'outcome': 'completed', 'seconds': 1.0}),
                                  ('tool_authorized', {'call_id': call}), ('tool_started', {'call_id': call, 'tool': 'Read'}),
                                  ('tool_finished', {'call_id': call, 'uncertain': False}),
                                  ('context_projected', {'step': index, 'manifest': {'filler': 'x' * 4000}})):
                app.runs.event(db, rid, kind, payload)
    began = time.perf_counter()
    view = activity(app, rid)
    took = time.perf_counter() - began
    assert len(view['items']) == 400 and view['next'] == view['items'][-1]['seq']  # a bounded page
    assert len(activity(app, rid, view['next'])['items']) == 200
    print('\nrun_activity over 1,800 run events and 300 tools: %.1f ms' % (took * 1000))
    assert took < 1.0


def test_the_feed_never_raises_into_a_run_when_publishing_fails():
    feed = ActivityFeed(store=None, runs=None, emit=lambda *a: (_ for _ in ()).throw(RuntimeError('emit failed')))
    feed.publish('chat', 'run')  # its records cannot be read: nothing is sent and nothing is raised
    feed.reasoning('chat', 'run', 1, 1, 'text', live=False)
    feed.settle('run', 1, 1, 'committed')
    assert feed.text('run') == ''  # held text is never part of the readable summary
