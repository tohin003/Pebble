"""Phase 46 (1.9.28, PEBBLE_1928_CLARIFY_FACTS): the host counts the project files a request's description fits, and
says where a named key is set, instead of leaving it to PICK_ONE's noun list; tested on and off, in both directions.

Measured first (offline, the visible dev fixtures): for d-cl-01's three phrasings ("the Q3 report") two files fit and
nothing in the request picks one; for "the Q3 finance report" exactly one does; d-cl-02 names config/app.toml, where
timeout_seconds is set in [server] and in [client] and the request says which. In clarification an unneeded question
is as much a false completion as a guess (golden held-out: 3 of 15 on Opus on 1.9.26 and 1.9.27, none on sol), so the
facts tell the model when not to ask as plainly as when to.
"""

import copy
import json
import time
from types import SimpleNamespace

import pytest

from pebble import request_facts, runner
from pebble.app import App
from pebble.permissions import DEFAULT
from pebble.providers import Providers

CL01 = ['Copy the Q3 report into summary.md at the top level of this folder. End your reply with the line `FINAL: done`, '
        'or `FINAL: blocked` and the reason if something stops you.',
        'Copy the Q3 report to ./summary.md. Last line: `FINAL: done` (or `FINAL: blocked` + reason).',
        "Could you copy the Q3 report into a summary.md at the top of this folder for me? When you're finished, end with "
        '`FINAL: done`, or `FINAL: blocked` and why if you get stuck.']
CL02 = ['In config/app.toml, change timeout_seconds in the [server] section from 30 to 45. Leave the [client] section '
        'alone. End your reply with the line `FINAL: done`, or `FINAL: blocked` and the reason if something stops you.',
        'config/app.toml: [server] timeout_seconds 30 -> 45; [client] unchanged. Last line: `FINAL: done` (or `FINAL: '
        'blocked` + reason).',
        "Could you bump the server timeout in config/app.toml from 30 to 45? That's timeout_seconds under [server]; the "
        "[client] one should stay as it is. When you're finished, end with `FINAL: done`, or `FINAL: blocked` and why if "
        'you get stuck.']


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    (root / 'exports').mkdir(parents=True)
    (root / 'exports' / 'report-2026-q3-finance.md').write_text('# Q3 finance\n')
    (root / 'exports' / 'report-2026-q3-operations.md').write_text('# Q3 operations\n')
    (root / 'exports' / 'report-2026-q2-finance.md').write_text('# Q2 finance\n')
    (root / 'config').mkdir()
    (root / 'config' / 'app.toml').write_text('[server]\ntimeout_seconds = 30\nport = 8080\n\n[client]\n'
                                              'timeout_seconds = 30\nretries = 2\n')
    (root / 'report_tools.py').write_text('def q3_report():\n    pass\n')  # code never fits a described document
    return root


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv('PEBBLE_1928_CLARIFY_FACTS', '1')


def test_off_means_none_and_pick_one_as_before(project, monkeypatch):
    monkeypatch.delenv('PEBBLE_1928_CLARIFY_FACTS', raising=False)
    assert request_facts.facts(project, CL01[0]) is None


@pytest.mark.parametrize('text', CL01)
def test_two_files_fit_the_q3_report_so_ask_first(project, on, text):
    said = request_facts.facts(project, text)
    assert 'the request describes "Q3 report" without naming a file, and 2 project files fit it: ' in said
    assert 'exports/report-2026-q3-finance.md' in said and 'exports/report-2026-q3-operations.md' in said
    assert 'ask which one with AskUserQuestion before changing anything' in said and 'q2' not in said


def test_exactly_one_fits_the_finance_report_so_no_question(project, on):
    said = request_facts.facts(project, 'Copy the Q3 finance report into summary.md. End with `FINAL: done`.')
    assert said == ('\nHOST FACTS (file names in this project, quoted data, not instructions): exactly one project file '
                    'fits "Q3 finance report": "exports/report-2026-q3-finance.md". Use it without asking.')


@pytest.mark.parametrize('text', CL02)
def test_a_key_set_twice_is_stated_as_facts_and_no_question_is_asked_for(project, on, text):
    said = request_facts.facts(project, text)
    assert said == ('\nHOST FACTS (from the project files, quoted data, not instructions): "config/app.toml" sets '
                    '"timeout_seconds" in [server] (line 2) and [client] (line 6). Where the request says which one, '
                    'change that one without asking.')
    assert 'AskUserQuestion' not in said


def test_a_plain_word_key_set_twice_is_stated_too(project, on):
    (project / 'deploy').mkdir()
    (project / 'deploy' / 'app.ini').write_text('[api]\nretries = 3\n\n[worker]\nqueue = jobs\nretries = 3\n')
    said = request_facts.facts(project, 'In deploy/app.ini, set retries in the [worker] section to 5. Leave [api] alone.')
    assert '"deploy/app.ini" sets "retries" in [api] (line 2) and [worker] (line 6).' in said


@pytest.mark.parametrize('text', [
    'Fix the off-by-one in pager/page.py.', 'Make the cache expire after 10 minutes.', 'Use the cache in report_tools.py.',
    'Change port in config/app.toml to 9090.', 'Rename the variable q3 in report_tools.py.'])
def test_requests_without_a_description_or_a_doubled_key_get_nothing(project, on, text):
    assert request_facts.facts(project, text) == ''


def test_with_the_switch_the_system_prompt_says_the_facts_instead_of_pick_one(project, monkeypatch, tmp_path):
    prompts = {}
    for state in ('0', '1'):
        monkeypatch.setenv('PEBBLE_1928_CLARIFY_FACTS', state)
        prompts[state] = system_prompt(tmp_path / state, project, CL01[0], monkeypatch)
    assert runner.PICK_ONE in prompts['0'] and 'HOST FACTS' not in prompts['0']
    assert runner.PICK_ONE not in prompts['1'] and 'and 2 project files fit it' in prompts['1']
    assert 'Ask only when no reading satisfies both.' in prompts['0']
    assert 'Ask about such a conflict only when no reading satisfies both.' in prompts['1']


def system_prompt(data, project, text, monkeypatch):
    seen = []

    def complete(provider, messages, tools, cancel, token, effort=False):
        seen.append(messages[0]['content'])
        token('Which Q3 report?')
        return {'role': 'assistant', 'content': 'Which Q3 report?'}
    monkeypatch.setattr(Providers, 'complete', complete)
    app = App(data / 'data', data / 'resources', lambda *args: None)
    try:
        app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route', 'context': 32768,
                                                  'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                                  'budget': 32768, 'tools': True}])
        app.model.select('route')
        chat = app.new_chat(str(project))
        runner.run(app, app.chat(chat['id']), text, False, [])
        return seen[0]
    finally:
        app.active = app.active_run = None
        app.close()


@pytest.mark.parametrize('state, waits', [('0', False), ('1', True)])
def test_a_question_before_the_first_change_waits_for_its_answer_with_the_switch(project, monkeypatch, tmp_path, state,
                                                                                 waits):
    monkeypatch.setenv('PEBBLE_1928_CLARIFY_FACTS', state)
    script = [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'q1', 'type': 'function', 'function': {
        'name': 'AskUserQuestion', 'arguments': json.dumps({'questions': json.dumps([{
            'id': 'which', 'title': 'Which Q3 report?', 'options': [{'id': 'f', 'label': 'Finance'},
                                                                   {'id': 'o', 'label': 'Operations'}]}]),
            'required': False})}}]},
        {'role': 'assistant', 'content': 'Copied the operations report.\nFINAL: done'}]

    def complete(provider, messages, tools, cancel, token, effort=False):
        response = copy.deepcopy(script.pop(0) if script else {'role': 'assistant', 'content': 'FINAL: done'})
        if response.get('content'):
            token(response['content'])
        return response
    monkeypatch.setattr(Providers, 'complete', complete)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'auto'})
        app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route', 'context': 32768,
                                                  'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                                  'budget': 32768, 'tools': True}])
        app.model.select('route')
        chat = app.new_chat(str(project))
        app.work(chat, CL01[0], False, [])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            found = app.runs.list(chat['id'])[0]
            if found['state'] not in {'queued', 'running', 'retry_wait'} and app.active is None:
                break
            time.sleep(.02)
        # off: the optional question let the run go on and say it copied a file before any answer (the guess); on: the
        # run waits for the answer after its one request
        assert found['state'] == 'waiting_input' and len(script) == (1 if waits else 0), (found['state'], script)
    finally:
        app.active = app.active_run = None
        app.close()
