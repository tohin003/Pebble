"""Host commands: every catalogue entry dispatches, /help is the catalogue and no UI text advertises an absent command."""

import re
from pathlib import Path

import pytest

from pebble.app import App
from pebble.goals import Goals
from pebble.runtime_commands import ARGUMENTS, CATALOGUE, command


ROOT = Path(__file__).resolve().parents[2]
# One host-parsed invocation per catalogue entry; a new entry without a sample fails the dispatch test.
SAMPLES = {
    'jobs': '/jobs', 'git': '/git', 'agents': '/agents', 'agent': '/agent missing', 'views': '/views',
    'mods': '/mods', 'help': '/help', 'status': '/status', 'runs': '/runs', 'pause': '/pause',
    'resume': '/resume', 'cancel': '/cancel', 'steer': '/steer use the project scope', 'plan': '/plan',
    'questions': '/questions', 'context': '/context', 'handoff': '/handoff', 'compact': '/compact',
    'memory': '/memory', 'skills': '/skills', 'skill': '/skill', 'plugins': '/plugins', 'hooks': '/hooks',
    'goal': '/goal', 'criteria': '/criteria', 'blockers': '/blockers', 'exports': '/exports',
    'export': '/export missing pdf'}
ROUTING = re.compile('Unknown command|does not accept those arguments')
# A command mention in UI or reply text: "/name" after a space, quote, backtick, bracket or tag.
MENTION = re.compile(r'''(?:^|(?<=[\s"'`(>]))/([a-z][a-z-]{1,15})(?![\w/.:-])''', re.M)
ITEMS = [{'id': 'scope', 'title': 'Which scope should I use?', 'options': []}]


@pytest.fixture
def app(tmp_path):
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    value.goals = Goals(value)
    value.model.complete = lambda *args: pytest.fail('A host command must not run inference')
    yield value
    value.close()


def say(app, cid, text):
    result = app.send(cid, text)
    assert result['command'] is True
    return app.chat(cid)['messages'][-1]['content']


def running(app, cid):
    rid = app.runs.create(cid, 'Work on the goal', app.runtime_config(app.chat(cid), False))
    app.runs.transition(rid, 'running')
    return rid


def receipt(app, rid, call_id, name, result):
    app.runs.begin_tool(rid, call_id, name, {})
    app.runs.end_tool(rid, call_id, result)


class FakeExports:
    """Loopback stand-in for the A2 exporter: records calls; rendering or saving would fail the test."""

    def __init__(self):
        self.calls = []

    def preview(self, cid, source, fmt, options):
        self.calls.append((cid, source, fmt, options))
        return {'plan_id': 'plan-1', 'source': {**source, 'revision': 1, 'digest': 'd1g3st'}, 'format': fmt,
                'pages_estimate': 2, 'deadline': 0}

    def list(self, cid):
        return [{'export_id': 'e1', 'format': 'pdf', 'source': {'kind': 'artifact', 'id': 'v1', 'revision': 1},
                 'status': 'unverified'}]

    def render(self, *args, **kwargs):
        pytest.fail('A command must never render an export')

    save = render


def test_every_catalogue_entry_dispatches_without_inference(app):
    assert set(SAMPLES) == {c['name'] for c in CATALOGUE}
    for name, text in SAMPLES.items():
        cid = app.new_chat()['id']
        for invoke in (lambda: app.send(cid, text), lambda: command(app, cid, text)):
            try:
                result = invoke()
            except Exception as exc:  # A handler's own refusal still proves routing.
                assert not ROUTING.search(str(exc)), (name, str(exc))
            else:
                assert result['command'] is True if isinstance(result, dict) else isinstance(result, str), name
    assert not app.runs.list()


def test_help_lists_exactly_the_catalogue_and_arguments_match(app):
    cid = app.new_chat()['id']
    lines = command(app, cid, '/help').split('\n\n', 1)[1].split('\n')
    assert lines == [f"- /{c['name']} {c['arguments']} — {c['description']}" for c in CATALOGUE]
    names = [c['name'] for c in CATALOGUE]
    assert len(set(names)) == len(names) and all(re.fullmatch('[a-z]+', n) for n in names)
    assert all(set(c) == {'name', 'arguments', 'description'} for c in CATALOGUE)
    assert {'goal', 'criteria', 'blockers', 'exports', 'export'} <= set(names)
    assert app.dispatch('command_catalogue', {}) == CATALOGUE
    for item in CATALOGUE:
        if item['name'] == 'skill':
            continue  # app.send parses /skill arguments before this dispatcher.
        assert (item['name'] in ARGUMENTS) == bool(item['arguments']), item['name']
        if not item['arguments']:
            with pytest.raises(ValueError, match='does not accept'):
                command(app, cid, f"/{item['name']} extra")


def test_no_ui_string_advertises_an_absent_command():
    names = {c['name'] for c in CATALOGUE}
    seen = {}
    for path in sorted((ROOT / 'src').rglob('*')):
        # Shipped UI only: *.test.mjs fixtures hold regex literals such as /approve/i, not UI text.
        if path.suffix in {'.vue', '.ts', '.mjs'} and 'vendor' not in path.parts and not path.name.endswith('.test.mjs'):
            for name in MENTION.findall(path.read_text(encoding='utf-8')):
                seen.setdefault(name, set()).add(path.relative_to(ROOT).as_posix())
    absent = {n: sorted(p) for n, p in seen.items() if n not in names}
    assert absent == {}, repr(absent)
    assert 'skill' in seen  # The existing Settings hint proves the scan reads UI text.
    for module in ('runtime_commands.py', 'goals.py'):
        replies = set(MENTION.findall((ROOT / 'backend' / 'pebble' / module).read_text(encoding='utf-8')))
        assert replies <= names and replies, module
    assert MENTION.findall('Use /goal "x", see https://a.example/path, </div> and /[a-z]/gi') == ['goal']


def test_goal_commands_settle_only_when_every_criterion_is_met(app):
    cid = app.new_chat()['id']
    assert say(app, cid, '/goal') == 'This conversation has no current goal. Start one with /goal "title".'
    assert 'Started goal' in say(app, cid, '/goal "Ship the release"')
    with pytest.raises(ValueError, match='already has a current goal'):
        app.send(cid, '/goal “Another goal”')
    assert 'Added criterion `c1` (human check)' in say(app, cid, '/criteria add "Release notes reviewed"')
    assert 'Added criterion `c2` (evidence)' in say(app, cid, '/criteria add --evidence "The tests pass"')
    with pytest.raises(ValueError, match='not done yet: criterion c1 is open'):
        app.send(cid, '/goal done')
    with pytest.raises(ValueError, match='no evidence yet'):
        app.send(cid, '/criteria check c2')
    assert say(app, cid, '/criteria check c1') == 'Criterion `c1` checked by you.'
    shown = say(app, cid, '/goal')
    assert '**Ship the release** (active, revision 4)' in shown and 'Criteria: 1/2 met.' in shown
    gid = app.goals.current(cid)['id']
    app.goals.add_step(cid, gid, 'Run the tests', criteria=['c2'])
    rid = running(app, cid)
    app.goals.link_run(rid)
    receipt(app, rid, '1:tests', 'RunTests', {'status': 'applied', 'result': {'exit_code': 0}})
    app.goals.agent_update(rid, [{'id': 's1', 'status': 'completed', 'evidence_ids': ['1:tests']}])
    app.runs.transition(rid, 'completed')
    assert '`c2` [evidence] met' in say(app, cid, '/criteria')
    assert 'is done: all 2 criteria are met' in say(app, cid, '/goal done')
    assert say(app, cid, '/goal').startswith('This conversation has no current goal')
    routes = {r['message_id']: r['route_class'] for r in app.store.query('SELECT * FROM message_routes WHERE chat_id=?', (cid,))}
    assert set(routes.values()) == {'device'}


def test_goal_state_commands_and_usage_errors(app):
    cid = app.new_chat()['id']
    for text, error in (('/goal pause', 'no current goal'), ('/goal Ship it', 'Use /goal'),
                        ('/criteria add "x"', 'no current goal'), ('/goal "x" extra', 'Use /goal')):
        with pytest.raises(ValueError, match=error):
            app.send(cid, text)
    say(app, cid, '/goal "Plan the trip"')
    for text in ('/criteria add Hotel booked', '/criteria check', '/criteria remove c1', '/criteria add --evidence'):
        with pytest.raises(ValueError, match='Use /criteria'):
            app.send(cid, text)
    assert 'will not work on it' in say(app, cid, '/goal pause')
    with pytest.raises(ValueError, match='already paused'):
        app.send(cid, '/goal pause')
    assert say(app, cid, '/goal resume').endswith('is active.')
    assert say(app, cid, '/goal abandon').endswith('is abandoned.')
    assert say(app, cid, '/criteria').startswith('This conversation has no current goal')


def test_criteria_commands_review_agent_candidates(app):
    cid = app.new_chat()['id']
    say(app, cid, '/goal "Plan the trip"')
    rid = running(app, cid)
    app.goals.link_run(rid)
    app.goals.agent_update(rid, candidates=[{'text': 'Hotel is booked'}, {'text': 'Budget agreed', 'kind': 'evidence'}])
    listed = say(app, cid, '/criteria')
    assert '`c1` [human check] candidate (proposed by Pebble)' in listed
    assert 'Proposed by Pebble (accept or reject with /criteria)' in say(app, cid, '/goal')
    with pytest.raises(ValueError, match='Accept this proposed criterion'):
        app.send(cid, '/criteria check c1')
    assert say(app, cid, '/criteria accept c1') == 'Criterion `c1` accepted.'
    assert say(app, cid, '/criteria reject c2') == 'Criterion `c2` rejected.'
    assert say(app, cid, '/criteria check c1') == 'Criterion `c1` checked by you.'
    assert say(app, cid, '/criteria uncheck c1') == 'Criterion `c1` marked not met.'
    with pytest.raises(ValueError, match='running task'):
        app.send(cid, '/goal done')
    app.runs.transition(rid, 'completed')


def test_blockers_command_reads_host_records(app):
    cid = app.new_chat()['id']
    assert say(app, cid, '/blockers') == 'This conversation has no goal or runs yet.'
    rid = running(app, cid)
    app.interactions.create_question(rid, '1:ask', ITEMS, False)
    without_goal = say(app, cid, '/blockers')
    assert 'Needs your answer: question' in without_goal and 'Which scope should I use? (agent-authored)' in without_goal
    say(app, cid, '/goal "Ship the release"')
    app.goals.link_run(rid)
    assert 'Needs your answer' in say(app, cid, '/blockers')
    assert '(blocked, revision' in say(app, cid, '/goal')
    question = app.interactions.questions(cid)[0]
    app.interactions.cancel_question(cid, question['id'], 1)
    # Phase 24: validate_phase17.py (c1) pins this exact reply, so V1-108's plainer wording was reverted here.
    assert say(app, cid, '/blockers') == 'No host-recorded blockers.'
    app.runs.transition(rid, 'completed')


def test_export_previews_only_and_exports_lists(app):
    cid, other = app.new_chat()['id'], app.new_chat()['id']
    view = app.artifacts.render(cid, 'Fixture', 'diagram', {'nodes': [{'id': 'a', 'label': 'A'}], 'edges': []})
    # Without App.exports both commands fail closed (the state before the parent wired it).
    wired, app.exports = app.exports, None
    assert say(app, cid, '/exports') == 'Exports are not available in this build.'
    assert 'Nothing was written' in say(app, cid, f"/export {view['id']} pdf")
    # Phase 17 merge: the real service previews only; nothing is rendered, saved or listed.
    app.exports = wired
    assert say(app, cid, '/exports') == 'This conversation has no exports.'
    reply = say(app, cid, f"/export {view['id']} html")
    assert 'preview only. Nothing was written, saved or published' in reply and 'native Save dialog' in reply
    folder = Path(app.home) / 'exports' / cid
    assert wired.list(cid) == [] and (not folder.exists() or not any(p.is_file() for p in folder.rglob('*')))
    app.exports = FakeExports()
    reply = say(app, cid, f"/export {view['id']} PDF")
    assert app.exports.calls == [(cid, {'kind': 'artifact', 'id': view['id']}, 'pdf', {})]
    assert 'Export preview `plan-1`' in reply and 'digest `d1g3st`' in reply and 'about 2 pages' in reply
    assert 'preview only. Nothing was written, saved or published' in reply and 'native Save dialog' in reply
    for text, error in ((f"/export {view['id']} pptx", 'Use /export'), (f"/export {view['id']}", 'Use /export'),
                        ('/export ../../etc pdf', 'Use /export'), ('/export missing csv', 'not found')):
        with pytest.raises(ValueError, match=error):
            app.send(cid, text)
    with pytest.raises(ValueError, match='not found'):
        app.send(other, f"/export {view['id']} html")
    assert len(app.exports.calls) == 1
    assert 'nothing is published' in say(app, cid, '/exports') and '`e1` — pdf of artifact `v1` revision 1: unverified' in say(app, cid, '/exports')


JARGON = re.compile(r'\b(inference|receipts?|host|runtime|capacity|E-B1)\b', re.I)
# The one pinned phrase: test_phase17_security.py requires "no inference" in the /goal reply.
PINNED = 'uses no inference'
# Text goals.py writes into a criterion's detail, passed through unchanged; goals.py is outside this track (the
# t24-ux-settings note asks for its wording change). Nothing else is exempt.
PASSED_THROUGH = ('link a step to it or choose receipts in the Goal panel',
                  # validate_phase17.py (c1) pins the /blockers reply exactly; historical validators are never edited.
                  'No host-recorded blockers.')


def plain(text):
    for words in (PINNED, *PASSED_THROUGH):
        text = text.replace(words, '')
    return JARGON.search(text) is None


def test_descriptions_and_replies_use_plain_words_without_json(app):
    """V1-108: no internal words in the / menu or in any reply, and no JSON or digests in a reply."""
    for item in CATALOGUE:
        assert plain(item['description']), item
    cid = app.new_chat()['id']
    texts = [*SAMPLES.values(), '/memory candidates', '/memory maintain', '/goal "Ship it"', '/goal', '/criteria add --evidence "Tests pass"',
             '/criteria', '/blockers', '/runs', '/status', '/context', '/handoff', '/compact', '/agents', '/help']
    rid = running(app, cid)
    app.runs.transition(rid, 'completed')
    replies = 0
    for text in texts:
        try:
            reply = command(app, cid, text)
        except Exception as exc:  # a refusal is still words for the person
            reply = str(exc)
        else:
            replies += 1
        assert plain(reply), (text, reply)
        assert not re.search(r'\{\s*"|":\s|\b[0-9a-f]{32,}\b', reply), (text, reply)
    assert replies >= 20
    assert PINNED in command(app, app.new_chat()['id'], '/goal "Pinned phrase"').lower()
    runs = command(app, cid, '/runs')
    assert runs.startswith('Recent tasks:') and '“Work on the goal” — completed, 0 steps, started ' in runs and rid not in runs
    assert command(app, cid, '/status').startswith('Latest task, “Work on the goal”: **completed**. 0 of ')
    assert command(app, app.new_chat()['id'], '/status') == 'This conversation has no tasks yet.'


def test_jobs_git_and_agent_render_markdown_lists(app):
    """V1-108/V1-111/V1-114: /jobs, /git and /agent print short Markdown lists in words, never a JSON dump."""
    cid = app.new_chat()['id']
    packet = {'jobs': [{'id': 'j' * 32, 'name': 'Morning brief', 'task': 'Summarise the news', 'schedule': {'kind': 'daily', 'time': '08:00', 'timezone': 'Europe/Paris'},
                        'state': 'active', 'revision': 3, 'next_fire': 1760000000, 'error': None, 'last_run_id': None, 'last_run_state': None,
                        'monitor': None, 'config': {'fingerprint': 'f' * 64}},
                       {'id': 'k' * 32, 'name': 'Backup check', 'schedule': {'kind': 'interval', 'seconds': 7200}, 'state': 'needs_review',
                        'next_fire': None, 'error': 'The model changed; review the schedule again.', 'config': {}}],
              'notifications': [{'id': 'n1', 'title': 'Morning brief is ready', 'created': 1760000100}],
              'service': {'running': True, 'execution': 'while_service_running', 'last_error': None}}
    app.job_api.methods['job_list'] = lambda id: packet
    jobs = command(app, cid, '/jobs')
    assert jobs.startswith('Schedules in this conversation (they run only while Pebble is open):\n\n- **Morning brief** — every day at 08:00 (Europe/Paris); active; next run ')
    assert '- **Backup check** — every 2 hours; needs review. Needs attention: The model changed; review the schedule again.' in jobs
    assert '\n\nWaiting notifications:\n\n- Morning brief is ready (' in jobs
    assert not re.search(r'[{}]|fingerprint|while_service_running|revision', jobs), jobs
    app.job_api.methods['job_list'] = lambda id: {'jobs': [], 'notifications': [], 'service': {'running': True}}
    assert command(app, cid, '/jobs') == 'This conversation has no schedules. Create one under Details › Tools.'

    status = {'origin': 'local_git_repository', 'trust': 'untrusted', 'authorization': 'none', 'root': '/work/a', 'head_commit': 'c' * 40,
              'branch': 'main', 'detached': False, 'shallow': False, 'fingerprint': 'f' * 64, 'upstream': 'origin/main', 'ahead': 2, 'behind': 0,
              'clean': False, 'counts': {'staged': 1, 'unstaged': 2, 'untracked': 1, 'unmerged': 1},
              'entries': [{'path': 'src/app.ts', 'kind': 'changed', 'index': 'M', 'worktree': '.', 'submodule': False},
                          {'path': 'README.md', 'kind': 'changed', 'index': 'A', 'worktree': 'M', 'submodule': False},
                          {'path': 'old.txt', 'kind': 'changed', 'index': '.', 'worktree': 'D', 'submodule': False},
                          {'path': 'notes `[x](https://evil.example)`.md', 'kind': 'untracked', 'index': None, 'worktree': None, 'submodule': False},
                          {'path': 'merge.py', 'kind': 'unmerged', 'index': 'U', 'worktree': 'U', 'submodule': False}],
              'total': 7, 'offset': 0, 'next_offset': 5, 'private_omitted': 1, 'output_truncated': False}
    app.git.status = lambda chat_id: status
    git = command(app, cid, '/git')
    assert git.split('\n\n')[:3] == ['Git status on branch `main` (file names are repository data):', '2 commits ahead of and 0 behind `origin/main`.',
        "- `src/app.ts` — modified, staged\n- `README.md` — added, staged; modified, not staged\n- `old.txt` — deleted\n"
        "- `notes '[x](https://evil.example)'.md` — new, not tracked yet\n- `merge.py` — in conflict"]
    assert '2 more files are not listed.' in git and '1 private files are not listed.' in git
    assert not re.search(r'[{}]|fingerprint|authorization|head_commit|' + 'c' * 40, git), git
    app.git.status = lambda chat_id: {**status, 'clean': True, 'entries': [], 'total': 0, 'upstream': None, 'private_omitted': 0, 'branch': None}
    assert command(app, cid, '/git') == 'Git status on a detached checkout (file names are repository data):\n\nNo changes: the files match the last commit.'

    assert command(app, cid, '/agent a1') == 'This conversation has no tasks yet.'
    rid = running(app, cid)
    node = {'id': 'worker', 'agent_id': 'worker', 'run_id': rid, 'name': 'Researcher', 'task': 'Find the release notes.', 'status': 'completed',
            'generation': 2, 'tools': ['Read'], 'limits': {'max_steps': 8}, 'dispatches': 3, 'charged_tokens': 1200, 'elapsed': 4.2,
            'result': '## Found\n\n- v1.0 notes are in docs/RELEASE.md', 'evidence_ids': ['receipt-1'], 'receipts': [{'id': 'receipt-1'}]}
    app.agents.get = lambda chat, run, agent: node
    app.agents.list = lambda chat, run: {'root_run_id': rid, 'nodes': [node], 'runtime': {'local_lane': 1, 'cloud_limit': 3}}
    agent = command(app, cid, '/agent worker')
    assert agent.split('\n\n')[:4] == ['**Researcher** — completed', 'Task: Find the release notes.', 'Used: 3 model calls, 1,200 tokens, 4 seconds.',
                                       'Result:']
    assert '## Found\n\n- v1.0 notes are in docs/RELEASE.md' in agent and agent.endswith('it grants no permission.')
    assert not re.search(r'[{}]|generation|receipt|max_steps|cloud_limit', agent), agent
    assert command(app, cid, '/agents') == 'Agents on the latest task:\n\n- **Researcher** (`worker`): completed'
    app.runs.transition(rid, 'completed')


def test_goal_commands_fail_closed_without_the_goal_service(app):
    cid = app.new_chat()['id']
    app.goals = None
    for text in ('/goal', '/criteria', '/blockers'):
        with pytest.raises(ValueError, match='Goals are not available'):
            command(app, cid, text)
