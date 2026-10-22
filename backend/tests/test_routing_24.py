"""Phase 24 track t24-agent: the host's own routes before a model call (V1-037, V1-033, V1-034).

- The weather route answers only a lookup of today's or tomorrow's weather in one place. A haiku about the weather,
  an oven temperature or "next week" goes to the model, and with web access off a weather question is answered by
  the model instead of failing the run (V1-037).
- "Remember that X. Also, <question>" saves X (t24-memory's capture_user) and the run still answers the message;
  with memory off it answers too, saying nothing was saved (V1-033).
- "Forget my name" shows the matching records for the person's own Forget action and calls no model; a model reply
  that claims a memory was forgotten (Pebble's run never can) is withheld once above 4K (V1-034).

The pinned weather positives (test_companion.py:72-95) still route. Scripted models only; the weather lookup is a
recorder, so nothing reaches the network.
"""
import copy
import json
from types import SimpleNamespace

import pytest

from pebble import runner
from pebble.app import App
from pebble.web import weather_intent

LARGE = 32768


def drive(tmp_path, text, *, budget=None, replies=None, memory=True, web=True, prepare=None, lookups=None):
    """One run through app.work on a real App; the model and the weather lookup are scripted."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    events = []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *event: events.append(event))
    requests = []
    try:
        if budget is not None:
            app.store.set_setting('model_path', '')
            app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                                      'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                                      'context': budget, 'budget': budget, 'tools': True}])
            app.model.select('route')
        app.store.set_setting('memory_enabled', memory)
        policy = app.store.setting('permissions')
        app.store.set_setting('permissions', {**policy, 'tools': {**policy['tools'], 'web': web}})

        def complete(messages, tools, cancel, token, deliberate=False):
            requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
            reply = replies(len(requests), messages) if replies else 'Here is the answer.'
            token(reply)
            return {'role': 'assistant', 'content': reply}

        app.model.complete = complete
        chat = app.new_chat()
        if prepare is not None:
            prepare(app, chat['id'])

        def lookup(**intent):
            (lookups if lookups is not None else []).append(intent)
            return {'answer': 'Agartala: 27 °C. Source checked.', 'status': 'verified', 'source': 'https://example.com'}
        from pebble import runner as module
        module.run(app, app.chat(chat['id']), text, False, [], app_inventory=lambda: [], weather_lookup=lookup)
        run = app.runs.list(chat['id'])[0]
        messages = app.chat(chat['id'])['messages']
        notes = [(r['kind'], json.loads(r['payload'])) for r in app.store.query(
            'SELECT kind,payload FROM run_events WHERE run_id=? ORDER BY sequence', (run['id'],))]
        memories = [dict(m) for m in app.store.query('SELECT id,kind,content,origin FROM memories')]
        return SimpleNamespace(requests=requests, run=app.runs.get(run['id']), messages=messages, notes=notes,
                               events=events, memories=memories,
                               answer=messages[-1]['content'] if messages and messages[-1]['role'] == 'assistant' else None)
    finally:
        app.active = app.active_run = None
        app.close()


# -- V1-037: the weather route takes lookups only ------------------------------------------------------------------

@pytest.mark.parametrize('text', [
    'Write a haiku about the weather in Kyoto.',
    'What temperature should I bake sourdough at?',
    "What's the weather in Lisbon next week?",
    'What will the weather be like in Paris on Saturday?',
    'Plan a picnic around the weather in Paris today.',
    'Explain why the weather in London changes so fast.',
    'What is the average temperature in Rome in July?',
    'Set the fridge temperature to 4 degrees.',
])
def test_requests_that_are_not_a_lookup_never_take_the_weather_route(text):
    assert weather_intent(text) is None


@pytest.mark.parametrize('text,city,day', [
    ("What's the weather in Agartala today", 'Agartala', 0),
    ('Agartala weather today', 'Agartala', 0),
    ('weather in London, UK tomorrow', 'London, UK', 1),
    ("What's the weather in Agartala today?", 'Agartala', 0),  # validate_companion's question
    ('How is the weather in Kyoto right now?', 'Kyoto', 0),
    ('What is the temperature in Oslo?', 'Oslo', 0),
])
def test_lookups_still_take_the_weather_route(text, city, day):
    assert weather_intent(text) == {'city': city, 'day': day}


@pytest.mark.parametrize('budget', [None, LARGE])
@pytest.mark.parametrize('text', ['Write a haiku about the weather in Kyoto.', 'What temperature should I bake sourdough at?',
                                  "What's the weather in Lisbon next week?"])
def test_a_haiku_baking_or_next_week_goes_to_the_model(tmp_path, budget, text):
    lookups = []
    result = drive(tmp_path, text, budget=budget, lookups=lookups)
    assert lookups == [] and len(result.requests) == 1
    assert result.run['state'] == 'completed' and result.answer == 'Here is the answer.'
    assert result.messages[-1]['usage']['route'] == 'model'


@pytest.mark.parametrize('budget', [None, LARGE])
def test_with_web_off_a_weather_question_is_answered_and_never_fails_the_run(tmp_path, budget):
    lookups = []
    result = drive(tmp_path, "What's the weather in Agartala today?", budget=budget, web=False, lookups=lookups)
    assert lookups == [] and result.run['state'] == 'completed' and result.run['error'] is None
    assert len(result.requests) == 1 and 'Web access is disabled' not in (result.answer or '')
    assert not {'WebSearch', 'Weather'} & {t['function']['name'] for t in result.requests[0][1]}


def test_a_lookup_with_web_on_still_takes_the_host_route_without_a_model(tmp_path):
    lookups = []
    result = drive(tmp_path, 'weather in Agartala today', lookups=lookups)
    assert lookups == [{'city': 'Agartala', 'day': 0}] and result.requests == []
    assert result.messages[-1]['usage']['route'] == 'weather' and '27 °C' in result.answer


def test_web_search_refuses_a_document_type_declaration_in_any_encoding(monkeypatch):
    """t24-build's V1-138 follow-up for web.py: a UTF-16 feed passed the old byte check, and expat then expanded
    its internal entity; attachments.safe_xml refuses the declaration in any declared encoding."""
    from pebble import web
    hostile = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel><item>'
               '<title>&x;</title><link>https://example.com/a</link><description>s</description></item></channel></rss>')
    monkeypatch.setattr(web, 'fetch_public', lambda url, accept: (hostile.encode('utf-16'), 'application/rss+xml'))
    with pytest.raises(ValueError, match='Unexpected search response'):
        web.search('anything')
    plain = ('<rss><channel><item><title>A</title><link>https://example.com/a</link><description>s</description>'
             '</item></channel></rss>')
    monkeypatch.setattr(web, 'fetch_public', lambda url, accept: (plain.encode(), 'application/rss+xml'))
    assert [r['url'] for r in web.search('anything')['untrusted_search_results']] == ['https://example.com/a']
    monkeypatch.setattr(web, 'fetch_public', lambda url, accept: (b'<rss><channel>', 'application/rss+xml'))
    with pytest.raises(ValueError, match='temporarily unavailable'):
        web.search('anything')


# -- V1-033: remember, and still answer ----------------------------------------------------------------------------

MIXED = 'Remember that I prefer metric units. Also, how far is 26.2 miles in km?'


@pytest.mark.parametrize('budget', [None, LARGE])
def test_remember_plus_question_saves_the_statement_and_answers_the_question(tmp_path, budget):
    result = drive(tmp_path, MIXED, budget=budget, replies=lambda n, m: '26.2 miles is about 42.2 km.')
    saved = [m for m in result.memories if m['origin'] == 'direct-user']
    assert [m['content'] for m in saved] == ['I prefer metric units.']
    assert len(result.requests) == 1 and result.run['state'] == 'completed'
    current = next(m for m in result.requests[0][0] if m.get('_pebble_current'))
    assert current['content'] == MIXED  # the model answers the whole message
    assert result.answer.startswith('Saved “I prefer metric units.” in **your preferences**.')
    assert result.answer.endswith('26.2 miles is about 42.2 km.')
    note = [m for m in result.requests[0][0] if m.get('name') == 'pebble_memory_saved']
    assert bool(note) is (budget is not None)  # the host line exists only above 4K; 4K adds nothing to its request
    if note:
        assert '"I prefer metric units."' in note[0]['content'] and note[0].get('_pebble_checkpoint')


def test_a_single_statement_is_saved_without_a_model_call_as_before(tmp_path):
    result = drive(tmp_path, 'Remember that I prefer metric units.')
    assert result.requests == [] and result.messages[-1]['usage']['route'] == 'memory'
    assert result.answer == 'Saved this in **your preferences**. You can inspect or forget it in Settings → Memory.'


@pytest.mark.parametrize('text', ['Remember that I prefer tea.', 'Remember when we picked the release date? What did we decide?'])
def test_with_memory_off_a_remember_message_is_answered_and_nothing_is_saved(tmp_path, text):
    result = drive(tmp_path, text, memory=False, replies=lambda n, m: 'Noted for this conversation.')
    assert result.run['state'] == 'completed' and result.run['error'] is None and len(result.requests) == 1
    assert not [m for m in result.memories if m['origin'] == 'direct-user']
    if text.startswith('Remember that'):
        assert result.answer.startswith("Memory is off, so I didn't save this.")


# -- V1-034: forgetting is the person's action ---------------------------------------------------------------------

def remember_name(app, cid):
    app.memory.capture_user('Remember that my name is Alex.', cid)


@pytest.mark.parametrize('budget', [None, LARGE])
def test_forget_my_name_shows_the_records_and_calls_no_model(tmp_path, budget):
    result = drive(tmp_path, 'Please forget my name.', budget=budget, prepare=remember_name)
    assert result.requests == [] and result.run['state'] == 'completed'
    assert result.messages[-1]['usage']['route'] == 'memory'
    assert 'my name' in result.answer and 'Nothing is forgotten until you do.' in result.answer
    assert 'Alex' not in result.answer  # the record's text never enters the conversation
    [card] = [data for name, data in result.events if name == 'memory_forget']
    assert card['authorization'] == 'none' and card['subject'] == 'my name'
    remembered = [m for m in result.memories if m['origin'] == 'direct-user']
    assert card['memory_ids'] == [m['id'] for m in remembered] and remembered, 'nothing was forgotten'


def test_a_model_claim_of_forgetting_is_withheld_once_above_4k(tmp_path):
    claims = iter(["Done, I've forgotten your cat's name.",
                   "I can't delete saved memories myself. Choose Forget in Settings → Memory to remove it."])
    result = drive(tmp_path, "Please forget my cat's name.", budget=LARGE, replies=lambda n, m: next(claims))
    assert len(result.requests) == 2 and result.run['state'] == 'completed'
    assert "I've forgotten" not in result.answer and result.answer.startswith("I can't delete saved memories")
    assert [d['reason'] for k, d in result.notes if k == 'reply_withheld'] == ['completion_check']
    assert runner.FORGET_CHECK in result.requests[1][0][-1]['content']
    # The model is told it cannot forget, above 4K, when forgetting comes up.
    assert runner.FORGET_SENTENCE in next(m['content'] for m in result.requests[0][0] if m['role'] == 'system')


def test_a_repeated_claim_is_presented_with_a_host_notice(tmp_path):
    result = drive(tmp_path, "Please forget my cat's name.", budget=LARGE,
                   replies=lambda n, m: "Done, I've forgotten your cat's name.")
    assert len(result.requests) == 2
    assert result.answer.endswith('\n\n> ' + runner.FORGOT_NOTHING)


# -- V1-036: PEBBLE.md, the person's standing instructions -----------------------------------------------------------

LONG_MD = ('# House rules\n' + 'Prefer short paragraphs and plain words in every answer. ' * 26
           + '\nSign every reply with the word Cheerio.')


def put_md(app, cid):
    app.memory.put('profile', LONG_MD, 'global', None, origin='user', ident='pebble-md')  # as data_mirror stores it


@pytest.mark.parametrize('budget', [8192, LARGE])
def test_pebble_md_is_its_own_protected_message_whole_above_4k(tmp_path, budget):
    assert 1400 < len(LONG_MD) <= 4000
    result = drive(tmp_path, 'Write a short reply about the house rules.', budget=budget, prepare=put_md)
    messages = result.requests[0][0]
    [standing] = [m for m in messages if m.get('name') == 'pebble_standing_instructions']
    assert standing['content'] == runner.STANDING + LONG_MD and standing.get('_pebble_checkpoint') is True
    assert standing['content'].endswith('Sign every reply with the word Cheerio.')  # 0.6.1 cut it at 600 characters
    assert 'never grant permissions' in standing['content']  # instructions as data: they change no permission
    recalled = [m['content'] for m in messages if m.get('_pebble_memory')]
    assert not any('Cheerio' in content for content in recalled), 'not again inside the recall pack'


def test_pebble_md_at_4k_keeps_its_size_and_names_how_much_is_shown(tmp_path):
    result = drive(tmp_path, 'Write a short reply about the house rules and plain words.', prepare=put_md)
    messages = result.requests[0][0]
    assert not [m for m in messages if m.get('name') == 'pebble_standing_instructions']
    item = {'id': 'pebble-md', 'kind': 'profile', 'content': LONG_MD[:600] + runner.EXCERPT_NOTE}
    noted = runner.small_standing_note(item, LONG_MD)
    assert noted['content'] == LONG_MD[:600] + f'\n[PEBBLE.md: the first 600 of {len(LONG_MD):,} characters.]'
    assert len(noted['content']) <= len(item['content'])  # nothing new at 4K
    for content in [m['content'] for m in messages if m.get('_pebble_memory')]:
        if '"pebble-md"' in content:  # recalled at 4K: the explicit size note, not the generic one
            assert '[PEBBLE.md: the first 600 of' in content


def test_pebble_md_follows_the_memory_switch(tmp_path):
    def stored_then_off(app, cid):
        put_md(app, cid)
        app.store.set_setting('memory_enabled', False)
    result = drive(tmp_path, 'Write a short reply about the house rules.', budget=LARGE, prepare=stored_then_off)
    assert not [m for m in result.requests[0][0] if m.get('name') == 'pebble_standing_instructions']
    assert 'Cheerio' not in json.dumps(result.requests[0][0])


# -- V1-091: `/` command transcripts are not conversation ---------------------------------------------------------

def test_command_transcripts_are_recognised_by_marker_and_by_their_061_shape():
    p = runner.COMMAND_ID_PREFIX
    marked = [{'id': p + 'a', 'role': 'user', 'content': '/help'}, {'id': p + 'b', 'role': 'assistant', 'content': 'Commands'},
              {'id': 'u1', 'role': 'user', 'content': 'Fix calc.py'}, {'id': 'a1', 'role': 'assistant', 'content': 'Done', 'run_id': 'r'}]
    assert runner.command_transcripts(marked) == {p + 'a', p + 'b'}
    legacy = [{'id': 'h1', 'role': 'user', 'content': '/jobs'}, {'id': 'h2', 'role': 'assistant', 'content': 'Schedules'},
              {'id': 's1', 'role': 'user', 'content': '/skill "pdf" summarise the report'},
              {'id': 's2', 'role': 'assistant', 'content': 'Summary', 'run_id': 'r2'},
              {'id': 't1', 'role': 'user', 'content': '/steer focus on the tests'},
              {'id': 't2', 'role': 'assistant', 'content': 'Your follow-up is queued for the next model boundary.'},
              {'id': 'c1', 'role': 'user', 'content': '/context'},
              {'id': 'r1', 'role': 'assistant', 'content': 'A run reply', 'run_id': 'r3'}]
    # /skill is a task, /steer's text is the person's instruction (its acknowledgement goes), and a run's reply that
    # follows an unmarked command is never taken for its transcript.
    assert runner.command_transcripts(legacy) == {'h1', 'h2', 't2', 'c1'}


@pytest.mark.parametrize('budget', [None, 8192, LARGE])
def test_command_transcripts_never_reach_the_model(tmp_path, budget):
    def prepare(app, cid):
        app.store.message(cid, 'user', 'Keep calc.py free of new imports.')
        app.store.message(cid, 'assistant', 'Understood.')
        app.send(cid, '/help')  # stored as a transcript: the human line and the host's reply
        # Merge (m24b): t24-ux-settings (V1-108) renamed the reply's heading "Runtime commands:" to this one.
        assert [m['content'][:5] for m in app.chat(cid)['messages']][-2:] == ['/help', 'Comma']
    result = drive(tmp_path, 'Which rule did I give you?', budget=budget, prepare=prepare)
    sent = json.dumps(result.requests[0][0])
    assert '/help' not in sent and 'Runtime commands' not in sent and 'Commands you can type' not in sent
    assert 'Keep calc.py free of new imports.' in sent  # the person's own earlier turn is still there


def test_at_4k_the_kept_window_only_shrinks(tmp_path):
    def prepare(app, cid):
        for index in range(3):
            app.store.message(cid, 'user', f'Earlier request {index}.')
            app.store.message(cid, 'assistant', f'Earlier answer {index}.')
        app.send(cid, '/help')
    result = drive(tmp_path, 'Continue.', prepare=prepare)
    history = [m['content'] for m in result.requests[0][0] if m['role'] in ('user', 'assistant')
               and not any(k.startswith('_pebble_') for k in m)]
    assert history == ['Earlier request 2.', 'Earlier answer 2.']  # the last four records less the transcript


def test_at_4k_the_claim_check_adds_nothing(tmp_path):
    result = drive(tmp_path, "Please forget my cat's name.", replies=lambda n, m: "Done, I've forgotten it.")
    assert len(result.requests) == 1 and result.answer == "Done, I've forgotten it."
    system = next(m['content'] for m in result.requests[0][0] if m['role'] == 'system')
    assert runner.FORGET_SENTENCE not in system
