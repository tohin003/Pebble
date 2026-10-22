"""Phase 24 track t24-agent: what a root request offers, by phrasing and by conversation state (V1-032, V1-085, V1-077).

Above an 8,192-token window the offer follows the person's words and the conversation, not only trigger words in the
current message: a natural current-fact question gets the web tools when the web permission is on, a follow-up keeps
the tools the conversation already uses, a review request gets the read-only Git tools, and the word "agent" alone
("my estate agent") no longer brings the coordination set. No tool is added to the catalogue (D4 not taken).

At 8,192 tokens and below nothing changes. The golden fixtures below were recorded from this copy's source before
any t24-agent edit (the Phase 23 runner with the wave-A merge's other modules), and every 4K and 8K request of the
scenarios here must stay byte-identical to them (after normalising dates, random IDs, timestamps and temporary
paths). A missing fixture is written once and the test then fails, so a lost file never passes silently.
"""
import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from pebble import runner
from pebble.app import App
from pebble.tool_catalogue import AGENT_ROOT_TOOLS

GOLDEN = Path(__file__).resolve().parent / 'fixtures' / 'phase24' / 'agent_golden_requests.json'
CODING = 'Fix add in calc.py so it returns the sum, then run the tests.'
BUDGETS = {'local-4k': None, 'cloud-4k': 4096, 'cloud-8k': 8192}
SCENARIOS = {
    'chat-current-fact': {'text': 'Who won the 2026 Tour de France?'},
    'chat-web-words': {'text': 'Search the web for the latest Python release notes.'},
    'chat-quiz': {'text': 'Make a quiz about the solar system.'},
    'chat-estate-agent': {'text': 'Draft a polite note to my estate agent about the broken boiler.'},
    'chat-history': {'text': 'And which of the two is shorter?',
                     'history': (('user', 'Compare two hiking routes near Kyoto.'),
                                 ('assistant', 'Route A follows the river; Route B climbs the ridge.'))},
    'chat-memory': {'text': 'How far is a marathon in kilometres?',
                    'memories': (('profile', 'I prefer metric units.'),)},
    'project-coding': {'text': CODING, 'project': True, 'script': 'read'},
    'project-review': {'text': 'Review my uncommitted changes and point out any bugs.', 'project': True},
    'project-git': {'text': 'Show the git diff for calc.py.', 'project': True},
}


def names(tools):
    return [t['function']['name'] for t in tools or ()]


def requests_for(tmp_path, *, budget, text, project=False, history=(), memories=(), script=None, web=True,
                 prepare=None, replies=None):
    """Every root request of one run on a real App, only the model call scripted; [(messages, tools)]."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    seen = []
    try:
        if budget is not None:
            app.store.set_setting('model_path', '')
            app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                                      'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                                      'context': budget, 'budget': budget, 'tools': True}])
            app.model.select('route')
        policy = app.store.setting('permissions')
        app.store.set_setting('permissions', {**policy, 'tools': {**policy['tools'], 'web': web}})
        for kind, content in memories:
            app.memory.put(kind, content)

        def complete(messages, tools, cancel, token, deliberate=False):
            seen.append((copy.deepcopy(messages), copy.deepcopy(tools)))
            if replies is not None:
                reply = replies(len(seen), messages, tools)
                if reply.get('content'):
                    token(reply['content'])
                return reply
            if script == 'read' and len(seen) == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'call-1', 'type': 'function',
                     'function': {'name': 'Read', 'arguments': json.dumps({'file_path': 'calc.py'})}}]}
            token('Nothing was changed.\nFINAL: none')
            return {'role': 'assistant', 'content': 'Nothing was changed.\nFINAL: none'}

        app.model.complete = complete
        folder = None
        if project:
            folder = tmp_path / 'project'
            folder.mkdir(exist_ok=True)
            (folder / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        cid = app.new_chat(str(folder) if folder else None)['id']
        for role, content in history:
            app.store.message(cid, role, content)
        if prepare is not None:
            prepare(app, cid)
        app.work(app.chat(cid), text, False, [])
    finally:
        app.active = app.active_run = None
        app.close()
        try:
            from test_high_fd_waits_21 import close_guard_databases
            close_guard_databases(tmp_path)  # resource cleanup only (F21-fd)
        except ImportError:
            pass
    return seen


def normal(value, roots):
    """A canonical, reproducible text of one captured value: temporary paths, dates, random IDs and times masked."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    for root in sorted({str(r) for r in roots}, key=len, reverse=True):
        text = text.replace(root, '<ROOT>')
    text = re.sub(r'Current date: \d{4}-\d{2}-\d{2}', 'Current date: <DATE>', text)
    text = re.sub(r'(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])', '<HEX32>', text)
    text = re.sub(r'(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])', '<HEX24>', text)
    text = re.sub(r'(?<![0-9.])1[0-9]{9}(?:\.[0-9]+)?(?![0-9])', '<TIME>', text)
    return text


def captured(requests, roots):
    """The fixture form of a run's requests: whole messages (host markers kept) and the offered tool names, with a
    SHA-256 over the exact offered schemas."""
    out = []
    for messages, tools in requests:
        schemas = normal(tools, roots)
        out.append({'messages': json.loads(normal(messages, roots)), 'tools': names(tools),
                    'tools_sha256': hashlib.sha256(schemas.encode()).hexdigest()})
    return out


def golden_capture(tmp_path):
    found = {}
    for scenario, spec in SCENARIOS.items():
        for label, budget in BUDGETS.items():
            folder = tmp_path / scenario / label
            params = dict(spec)
            requests = requests_for(folder, budget=budget, text=params.pop('text'), **params)
            found[scenario + '|' + label] = captured(requests, [folder, folder.resolve()])
    return found


def masked(messages):
    """The constraints unit's marker is the SHA-256 of its content, which names random input IDs; the content itself is
    compared (masked), and the marker is checked against it separately."""
    return json.dumps([{**m, '_pebble_constraints': '<DIGEST>'} if '_pebble_constraints' in m else m for m in messages],
                      sort_keys=True, ensure_ascii=False)


def test_4k_and_8k_requests_are_byte_identical_to_the_golden_fixtures(tmp_path):
    found = golden_capture(tmp_path)
    if not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(found, sort_keys=True, ensure_ascii=False, indent=1) + '\n')
        pytest.fail(f'Golden fixture written to {GOLDEN.name}; run again to compare.')
    golden = json.loads(GOLDEN.read_text())
    assert sorted(golden) == sorted(found)
    for key in sorted(golden):
        expected, actual = golden[key], found[key]
        assert len(actual) == len(expected), key
        for index, (old, new) in enumerate(zip(expected, actual)):
            assert new['tools'] == old['tools'], (key, index)
            assert new['tools_sha256'] == old['tools_sha256'], (key, index)
            assert masked(new['messages']) == masked(old['messages']), (key, index)


LARGE = 32768
WEB = {'WebSearch', 'ReadURL'}
NATURAL = ('Who won the 2026 Tour de France?', 'Compare the battery life of the Pixel 10 and the iPhone 17.',
           'What is the bitcoin price right now?', 'Is the A9 motorway closed tonight?')


def system_of(messages):
    return next(m['content'] for m in messages if m['role'] == 'system')


def first(tmp_path, **kwargs):
    return requests_for(tmp_path, **kwargs)[0]


# -- V1-032: natural phrasing and follow-ups above 8K --------------------------------------------------------------

@pytest.mark.parametrize('text', NATURAL)
def test_a_natural_current_fact_question_offers_the_web_above_8k_when_web_is_on(tmp_path, text):
    messages, tools = first(tmp_path / 'on', budget=LARGE, text=text)
    assert WEB <= set(names(tools)), names(tools)
    assert 'For current facts use WebSearch and ReadURL' in system_of(messages)
    # The web permission still decides, and the sentence comes only with the tools.
    messages, tools = first(tmp_path / 'off', budget=LARGE, text=text, web=False)
    assert not (WEB | {'Weather'}) & set(names(tools))
    assert 'WebSearch' not in system_of(messages)
    # 8K is the Phase 23 offer (the golden fixtures pin it): no web tool for these words.
    assert not WEB & set(names(first(tmp_path / '8k', budget=8192, text=text)[1]))


def test_a_project_chat_offers_the_web_for_current_fact_wording_and_after_earlier_web_use(tmp_path):
    question = 'Who won the 2026 Tour de France?'
    assert WEB <= set(names(first(tmp_path / 'fact', budget=LARGE, text=question, project=True)[1]))
    follow = 'Open the second result and check the date it gives.'
    assert not WEB & set(names(first(tmp_path / 'cold', budget=LARGE, text=follow, project=True)[1]))

    def searched(app, cid):  # an earlier run of this chat searched the web (a completed receipt)
        rid = app.runs.create(cid, 'Search the web for the harbour dues', app.runtime_config(app.chat(cid), False), [])
        app.runs.transition(rid, 'running')
        app.runs.begin_tool(rid, '1:c1', 'WebSearch', {'query': 'harbour dues'})
        app.runs.end_tool(rid, '1:c1', {'untrusted_search_results': []})
        app.runs.transition(rid, 'completed')

    def recorded(app, cid):  # an earlier run of this chat recorded web sources in the ledger
        rid = app.runs.create(cid, 'Research the harbour dues', app.runtime_config(app.chat(cid), False), [])
        app.ledger.record_search(rid, 'harbour dues', [{'title': 'Dues', 'url': 'https://example.org/dues',
                                                         'snippet': 'EUR 0.47 per tonne'}])
        app.runs.transition(rid, 'running')
        app.runs.transition(rid, 'completed')
    for label, prepare in (('searched', searched), ('recorded', recorded)):
        offered = names(first(tmp_path / label, budget=LARGE, text=follow, project=True, prepare=prepare)[1])
        assert WEB <= set(offered), (label, offered)
        eight = names(first(tmp_path / (label + '-8k'), budget=8192, text=follow, project=True, prepare=prepare)[1])
        assert not WEB & set(eight), (label, eight)  # unchanged at 8K


def test_read_source_joins_the_step_after_this_run_records_a_page(tmp_path):
    pages = []

    def prepare(app, cid):
        original = app.ledger.sources
        app.ledger.sources = lambda run_id, **kw: pages + original(run_id, **kw)

    def replies(number, messages, tools):
        if number == 1:
            pages.append({'kind': 'page', 'source_id': 'S1'})  # as if this step's ReadURL recorded a page
            return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'w1', 'type': 'function', 'function': {
                'name': 'Weather', 'arguments': json.dumps({'city': ''})}}]}  # answered at once, no network
        return {'role': 'assistant', 'content': 'The page says 2026-07-01.'}
    requests = requests_for(tmp_path, budget=LARGE, text='Search the web for the Port Ellery harbour dues.',
                            prepare=prepare, replies=replies)
    assert 'ReadSource' not in names(requests[0][1]) and 'ReadSource' in names(requests[1][1])


def test_a_quiz_follow_up_offers_the_view_tools_above_8k(tmp_path):
    history = (('user', 'Make a quiz about the solar system.'), ('assistant', 'Here is your quiz.'))
    text = 'Make question 2 harder.'
    assert {'UIRender', 'UIUpdate'} <= set(names(first(tmp_path / 'turns', budget=LARGE, text=text, history=history)[1]))
    assert not {'UIRender', 'UIUpdate'} & set(names(first(tmp_path / '8k', budget=8192, text=text, history=history)[1]))

    def open_view(app, cid):  # a saved view of this chat that is still open (a table: App.chat decorates it as is)
        app.artifacts.list = lambda chat: [{'id': 'v1', 'kind': 'table', 'status': 'active', 'chat_id': cid}]
    offered = names(first(tmp_path / 'view', budget=LARGE, text=text, prepare=open_view)[1])
    assert {'UIRender', 'UIUpdate'} <= set(offered)
    assert 'ui.render views are local data' in system_of(first(tmp_path / 'say', budget=LARGE, text=text,
                                                               prepare=open_view)[0])


# -- V1-077 (sitting a part): "agent" alone is not delegation above 8K ----------------------------------------------

def test_an_estate_agent_brings_no_coordination_tools_above_8k(tmp_path):
    offered = set(names(first(tmp_path / 'estate', budget=LARGE, text=SCENARIOS['chat-estate-agent']['text'])[1]))
    assert offered & AGENT_ROOT_TOOLS == {'SpawnAgent'}, offered & AGENT_ROOT_TOOLS
    delegated = set(names(first(tmp_path / 'two', budget=LARGE, text='Use two agents in parallel to summarise my notes.')[1]))
    assert {'WaitForAgents', 'ReadAgentTree', 'ReviewAgentResult'} <= delegated
    # 8K keeps AGENT_WORDS and its staged pair (the golden fixture pins the estate-agent request there).
    eight = set(names(first(tmp_path / '8k', budget=8192, text=SCENARIOS['chat-estate-agent']['text'])[1]))
    assert eight & AGENT_ROOT_TOOLS == {'SpawnAgent', 'ReadAgentTree'}


@pytest.mark.parametrize('text,delegating', [
    ('Draft a note to my estate agent.', False), ('Ask the travel agent about refunds.', False),
    ('Set the user agent header in the client.', False), ('Delegate the tests to a child agent.', True),
    ('Use two agents to review both files.', True), ('Spawn a helper for the docs.', True),
    ('Run these in parallel.', True), ('Use subagents for the research.', True)])
def test_large_delegation_words(text, delegating):
    assert bool(runner.LARGE_AGENT_WORDS.search(text)) is delegating


# -- V1-085: review requests get the read-only Git tools above 8K -------------------------------------------------

@pytest.mark.parametrize('text', ['Review my uncommitted changes and point out any bugs.',
                                  'What did I change in calc.py since yesterday?'])
def test_a_review_request_offers_the_read_only_git_tools_above_8k(tmp_path, text):
    offered = set(names(first(tmp_path / 'large', budget=LARGE, text=text, project=True)[1]))
    assert {'ReadGitStatus', 'ReadGitDiff'} <= offered
    assert not {'ProposeGitChange'} & offered  # reads only: no Git effect is offered for review wording
    assert not {'ReadGitStatus', 'ReadGitDiff'} & set(names(first(tmp_path / '8k', budget=8192, text=text, project=True)[1]))


# -- V1-087, V1-167: the large-route system prompt ------------------------------------------------------------------

def test_the_large_route_prompt_names_only_offered_tools_and_gives_the_time_zone(tmp_path):
    messages, tools = first(tmp_path / 'chat', budget=LARGE, text='Draft a short thank-you note.', web=False)
    system = system_of(messages)
    for gone in ('No free-form shell exists.', 'correct it once', 'For current facts use WebSearch, ReadURL or Weather',
                 'ui.render views', 'OpenApp only opens'):
        assert gone not in system, gone
    assert 'fix the cause and try again' in system
    # The date line stays exactly as both evaluation arms get it (test_corpus_18a3); the time and zone follow it.
    assert re.search(r'Current date: \d{4}-\d{2}-\d{2}\. Local time: \d{2}:\d{2}.*\(UTC[+-]\d{2}:\d{2}\)\.', system), system
    # (Not "Open …" at the start: that shape is the host's own app route, which reads the installed apps.)
    opened = system_of(first(tmp_path / 'open', budget=LARGE, text='Could you open the calculator for me?')[0])
    assert 'OpenApp only opens an app on an explicit request' in opened
    # 8K keeps SYSTEM and the date line exactly (golden fixtures).
    eight = system_of(first(tmp_path / '8k', budget=8192, text='Draft a short thank-you note.')[0])
    assert 'No free-form shell exists.' in eight and re.search(r'Current date: \d{4}-\d{2}-\d{2}\.', eight)


@pytest.mark.parametrize('budget', [8192, 32768])  # the unit exists only above 4096
def test_the_constraints_marker_still_matches_its_content(tmp_path, budget):
    from pebble.constraints import unit_digest
    spec = SCENARIOS['chat-history']
    messages, _ = requests_for(tmp_path, budget=budget, text=spec['text'], history=spec['history'])[0]
    units = [m for m in messages if '_pebble_constraints' in m]
    assert len(units) == 1 and units[0]['_pebble_constraints'] == unit_digest(units[0]['content'])
