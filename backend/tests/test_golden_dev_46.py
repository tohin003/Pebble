"""Golden-set 2.0 dev cases on 1.9.18 (2026-10-27, before any of Pebble's held-out cells): what they found.

- d-sa-01: Read answered a binary build artifact with a codec error ("'utf-8' codec can't decode byte 0x80 ..."), and
  the terminal refused `python3 -c "...; p=Path(...)"` as an environment override (any space before `name=` counted).
- d-gw-01 (test_git_plans) and d-rl-01 (test_research_gate_live_answers) are covered beside their own code.
- Speed (D154): after a response whose only calls were a staged command or Git change, the run asked the model again;
  it read the plan back and wrote a reply nobody saw (d-gw-01: 2 of 8 requests, 18 of 54 s). It now waits at once.
- d-mt-01: the host's notice under a checked reply became its last line, below the `FINAL: done` line the request
  asked to end with; the notices now go above a checked reply.
"""

import copy
import json
import gzip
import io
import tarfile

import pytest

from pebble.agent_manager import Agents
from pebble.app import App
from pebble.policy import PolicyError, Workspace, binary_note
from pebble.terminal import inspect_command
from test_approval_handoff_22 import call, env, final, no_network, pending, settled  # noqa: F401 (fixtures)

PROFILE = {'id': 'golden-dev-fixture', 'kind': 'openai', 'name': 'Golden dev fixture',
           'url': 'https://unused.invalid', 'model': 'fixture', 'context': 8192,
           'budget': 4096, 'tools': True}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(Agents, 'start', lambda self: None)
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    monkeypatch.setattr(value.model, 'current', lambda: copy.deepcopy(PROFILE))
    monkeypatch.setattr(value.guard, 'handle', lambda *args, **kwargs: {'action': 'allow'})
    yield value
    value.active = value.active_run = None
    value.close()


def running(app, project):
    chat = app.new_chat(str(project))
    config = app.runtime_config(chat, False, {'max_steps': 8, 'max_seconds': 180})
    rid = app.runs.create(chat['id'], 'Delete the generated files that can be rebuilt.', config)
    app.runs.transition(rid, 'running')
    app.active, app.active_run = chat['id'], rid
    app.task_policy = copy.deepcopy(config['permissions'])
    return chat, rid


def test_read_describes_a_binary_file_instead_of_a_codec_error(app, tmp_path):
    project = tmp_path / 'project'
    (project / 'dist').mkdir(parents=True)
    data = gzip.compress(b'not a real archive\n' * 40)
    (project / 'dist' / 'survey_report-1.0.tar.gz').write_bytes(data)
    (project / 'notes.txt').write_text('plain text\n')
    chat, rid = running(app, project)
    ws = Workspace(project)
    with pytest.raises(PolicyError) as refused:
        app.execute('Read', {'file_path': 'dist/survey_report-1.0.tar.gz'}, ws, chat['id'], call_id='read-binary')
    text = str(refused.value)
    assert text.startswith('dist/survey_report-1.0.tar.gz is not UTF-8 text (%s bytes, starting like gzip' % f'{len(data):,}')
    assert '`file dist/survey_report-1.0.tar.gz`' in text and 'codec' not in text
    # the run's receipt says the same, and a text file still reads as before
    assert [t['result'] for t in app.runs.tools(rid) if t['call_id'] == 'read-binary'][0] == {'error': text}
    assert app.execute('Read', {'file_path': 'notes.txt'}, ws, chat['id'], call_id='read-text')['untrusted_source'] \
        == 'plain text\n'


def test_binary_note_names_common_kinds_and_tar_archives():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        info = tarfile.TarInfo('report.txt')
        info.size = 5
        archive.addfile(info, io.BytesIO(b'hello'))
    assert 'starting like a tar archive' in binary_note('a.tar', buffer.getvalue())
    assert 'starting like a PDF document' in binary_note('a.pdf', b'%PDF-1.7\n\xe2\xe3')
    assert binary_note('blob', b'\x80\x81\x82') == ('blob is not UTF-8 text (3 bytes), so Read cannot show it. Look at '
                                                     'it with a read-only command such as `file blob`, or `xxd -l 256 '
                                                     'blob` for its first bytes.')


def test_assignments_inside_arguments_are_not_environment_overrides(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    code = "from pathlib import Path; p=Path('dist/survey_report-1.0.tar.gz'); print(p.stat().st_size)"
    assert inspect_command('python3 -c "%s"' % code, project)['argv'] == ['python3', '-c', code]
    assert inspect_command('make CC=clang', project)['argv'] == ['make', 'CC=clang']
    # a leading NAME=value word is still an environment override, quoted or not, and substitutions stay blocked.
    # Phase 46 (1.9.28, D178: the person allows plain NAME=value words outside a deny list, reviewed, unless
    # PEBBLE_1928_TERMINAL=0): plain ones are reviewed overrides now; quoted ones and substitutions stay refused.
    # Phase 46 (1.9.28, the follow-ups security review 11, restated): for the project's own program (it was `ls`)
    assert inspect_command('A=1 B=2 python3 x.py', project)['env'] == {'A': '1', 'B': '2'}
    for command in ("'FOO=1' ls", 'python3 -c "$(cat x)"', 'echo ${HOME}'):
        with pytest.raises(PolicyError, match='environment overrides'):
            inspect_command(command, project)
    monkeypatch.setenv('PEBBLE_1928_TERMINAL', '0')
    for command in ('FOO=1 ls', "'FOO=1' ls", 'A=1 B=2 ls'):
        with pytest.raises(PolicyError, match='environment overrides'):
            inspect_command(command, project)


@pytest.fixture
def staged_stop(monkeypatch):
    # On from 1.9.20 (runner.STAGED_STOP); kept so these tests say what they rely on
    from pebble import runner
    monkeypatch.setattr(runner, 'STAGED_STOP', runner.STOP_KINDS)


def kinds(app, rid):
    return [r['kind'] for r in app.store.query('SELECT kind FROM run_events WHERE run_id=? ORDER BY sequence', (rid,))]


def test_a_response_of_only_staged_commands_waits_for_the_decision_at_once(env, staged_stop):
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [call('RunCommand', {'command': 'touch made.txt', 'network': False}, 'cmd'),
                  final('Created made.txt.\nFINAL: done')]
    app.work(chat, 'Create an empty made.txt with a command. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 1)  # one model request: none spent waiting for the decision
    assert 'staged_stop' in kinds(app, rid) and 'reply_withheld' not in kinds(app, rid)
    app.approve(cid, pending(app, cid, 'command')['id'])
    settled(env, app, rid, 'completed', 2)
    assert (env.project / 'made.txt').is_file()
    # the resumed request saw the command's result, not a note asking for a held reply
    resumed = json.dumps(env.requests[1])
    assert 'HOST RESUME' in resumed and 'was not shown to the person' not in resumed
    # d-sa-01: with no step to look further while it waited, the model is told to carry on to the whole request
    assert 'The decision you waited for is in. Before you reply, read the request again' in resumed
    assert any('FINAL: done' in m['content'] for m in app.chat(cid)['messages'] if m['role'] == 'assistant')


def test_a_read_beside_the_staged_command_still_waits_with_its_result_at_hand(env, staged_stop):
    # d-rc-01: a read and the script run in one response; the run waits for the run's decision, the read's result in
    # the context, instead of editing without the script's output and holding its reply to the end
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [{'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'r', 'type': 'function', 'function': {'name': 'Read', 'arguments': json.dumps({'file_path': 'calc.py'})}},
        {'id': 'c', 'type': 'function', 'function': {'name': 'RunCommand', 'arguments': json.dumps(
            {'command': 'touch made.txt', 'network': False})}}]}, final('Done.\nFINAL: done')]
    app.work(chat, 'Read calc.py and create made.txt with a command. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 1)
    assert 'staged_stop' in kinds(app, rid)


@pytest.mark.parametrize('calls', [
    [('Write', {'file_path': 'NOTES.md', 'content': '# Notes\n'})],  # a staged edit (a new file needs no Read first)
    [('Write', {'file_path': 'NOTES.md', 'content': '# Notes\n'}),  # and a command beside a staged edit
     ('RunCommand', {'command': 'touch made.txt', 'network': False})],
])
def test_staged_edits_keep_working_while_waiting(env, staged_stop, calls):
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    batch = {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'c%d' % n, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
        for n, (name, args) in enumerate(calls)]}
    env.script = [batch, final('Waiting for your review.\nFINAL: staged')]
    app.work(chat, 'Do it. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 2)  # the model was asked again, as before
    assert 'staged_stop' not in kinds(app, rid) and 'reply_withheld' in kinds(app, rid)
    assert not app.runs.get(rid)['checkpoint'].get('staged_stop')  # the held-reply resume stays as it was



def test_the_staged_stop_is_on():
    from pebble import runner
    assert runner.STAGED_STOP == runner.STOP_KINDS == frozenset({'command', 'shell', 'git', 'install', 'tests'})


# -- the host's notices above a checked reply (golden dev d-mt-01) --------------------------------------------------

def test_a_checked_reply_keeps_the_last_line_it_was_asked_to_end_with(tmp_path):
    # d-mt-01 on the 1.9.19 code: the request said "End your reply with the line `FINAL: done`", and the host's notice
    # under the checked reply became its last line, so a right answer was graded format_only.
    from test_routing_24 import LARGE, drive
    reply = 'I created units.py with to_celsius, rounded to one decimal place.\n\nFINAL: done'
    notice = 'No tests ran, so nothing checked the change.'

    def prepare(app, cid):
        app.controller.completion = lambda rid, ws, answer: {
            'state': 'verification_required', 'issues': ['RunTests collected no tests.'], 'repairable': False,
            'notices': [notice]}
    result = drive(tmp_path, 'Create units.py with to_celsius(fahrenheit). End your reply with the line `FINAL: done`.',
                   budget=LARGE, replies=lambda n, m: reply, prepare=prepare)
    assert result.run['state'] == 'verification_required'
    assert result.answer == '> Verification remains incomplete: ' + notice + '\n\n' + reply


# -- held-out s-cd-01 on 1.9.20: one inconsistent stream ended a 520 s task ------------------------------------------

def test_an_inconsistent_stream_is_refused_and_retryable():
    from pebble.recovery import ProviderFailure, transient
    from test_responses_stream import completed, message, refused, start
    item = {**message('Half a reply.'), 'status': 'incomplete'}
    error, tokens = refused([*start(), completed([item])], match='an output item is unfinished', kind=ProviderFailure)
    assert error.retryable is True and transient(error) and isinstance(error, ValueError)  # still refused, asked again


# -- Phase 46 (D154): text verbosity on the ChatGPT route, as OpenAI's own Codex asks of these models --------------------

@pytest.mark.parametrize('kind, model, sent', [('chatgpt', 'gpt-6.1-sol', True), ('chatgpt', 'gpt-5.6-sol', True),
                                               ('chatgpt', 'gpt-4.1', False), ('openai', 'gpt-6.1-sol', False)])
def test_the_chatgpt_route_asks_these_models_for_the_set_verbosity(monkeypatch, kind, model, sent):
    import threading
    from pebble import responses
    from test_responses_stream import PROFILE, MESSAGES, TOOLS, Provider, call, call_events, completed, start
    monkeypatch.setattr(responses, 'VERBOSITY', 'low')
    provider = Provider(start() + call_events(0, call()) + [completed()])
    responses.complete_responses(provider, {**PROFILE, 'kind': kind, 'model': model}, MESSAGES, TOOLS, threading.Event(),
                                 lambda text: None, 'medium')
    body = provider.requests[0][1]
    assert (body.get('text') == {'verbosity': 'low'}) is sent and ('text' in body) is sent
    monkeypatch.setattr(responses, 'VERBOSITY', '')  # off: nothing sent, the API's own default
    provider = Provider(start() + call_events(0, call()) + [completed()])
    responses.complete_responses(provider, {**PROFILE, 'kind': kind, 'model': model}, MESSAGES, TOOLS, threading.Event(),
                                 lambda text: None, 'medium')
    assert 'text' not in provider.requests[0][1]


# -- dev d-lh-91 on sol: a question asking the person to approve the run's own held changes ---------------------------

def test_a_question_asking_to_approve_held_changes_is_refused(app, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    chat, rid = running(app, project)
    ws = Workspace(project)
    app.current_tool_call = 'q1'
    ask = {'questions': json.dumps([{'id': 'apply_tests', 'title': 'The test edits are awaiting approval in Activity & '
                                     'review. Please approve them so I can verify the saved project.',
                                     'options': [{'id': 'approved', 'label': 'I approved them'},
                                                 {'id': 'declined', 'label': 'Do not apply them'}]}]),
           'required': True}
    other = {'questions': json.dumps([{'id': 'units', 'title': 'Should prices use euros or dollars?', 'options': [
        {'id': 'eur', 'label': 'Euros'}, {'id': 'usd', 'label': 'Dollars'}]}]), 'required': True}
    aid = app.store.action(chat['id'], 'edit', {'path': 'tests/test_x.py'})
    app.store.query('INSERT INTO run_action_owners VALUES(?,?,?,?)', (aid, rid, '1:call_write', 'edit'))
    with pytest.raises(PolicyError, match="own review already asks the person"):
        app.execute('AskUserQuestion', ask, ws, chat['id'], call_id='q1')
    assert app.execute('AskUserQuestion', other, ws, chat['id'], call_id='q2')['status'] == 'awaiting_user_input'
    app.store.query("UPDATE actions SET status='applied' WHERE id=?", (aid,))  # nothing held any more: asking is fine
    assert app.execute('AskUserQuestion', ask, ws, chat['id'], call_id='q3')['status'] == 'awaiting_user_input'
