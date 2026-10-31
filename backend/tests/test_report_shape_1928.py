"""Phase 46 (1.9.28): a report the request shapes is checked against that shape, once, and never holds the run.

Golden held-out parallel_work: 4 false completions of 15 on Opus (1.9.27) and 3 on sol (1.9.26), every miss a claim. A
report is no source file, so AgentGuard's outcome was no_changes and nothing compared audit.md with the request; the
golden checkers skip a line in another shape ("**billing**: ...", "1. billing: ..."), read "slow-timeout (90 s)" as
an unknown keyword and compare the FINAL count. output_checks.report_issues checks the shape, the keywords, the order and
the count (not the values a run read); the runner asks once (controller.soft_checks) and never holds.

The requests are the visible golden dev d-pw-01 and private d-pw-91 phrasings, the projects their fixtures' folders.
"""

import copy
import json
import time
from types import SimpleNamespace

import pytest

from pebble import output_checks
from pebble.app import App
from pebble.permissions import DEFAULT
from pebble.policy import Workspace
from pebble.providers import Providers

PW01 = [
    "For each of the four services in services/, check the effective production settings (prod.ini overrides base.ini; "
    "ignore staging.ini) for three problems: debug turned on, TLS verification turned off, and a timeout above 60 "
    "seconds. Write audit.md with one line per service, in alphabetical order, of the form `<service>: <problems>`, where "
    "problems are the keywords debug, tls-off and slow-timeout separated by commas, or `ok`. End your reply with the line "
    "`FINAL: <number of services with at least one problem>`, or `FINAL: blocked` and the reason if you can't.",
    "Audit services/*: effective prod config = base.ini + prod.ini overrides (not staging). Flag debug on (debug), TLS "
    "verification off (tls-off), timeout > 60 s (slow-timeout). audit.md: one line per service, alphabetical, "
    "`<service>: <keywords comma-separated | ok>`. Last line: `FINAL: <services with a problem>` (or `FINAL: blocked` + "
    "reason).",
    "Could you audit our four services in services/? For each one, work out the effective production settings (prod.ini "
    "overrides base.ini; staging.ini doesn't matter here) and check for debug turned on, TLS verification turned off, and "
    "a timeout above 60 seconds. Put the results in audit.md, one line per service in alphabetical order, like "
    "`<service>: <problems>`, using the keywords debug, tls-off and slow-timeout separated by commas, or `ok` if there's "
    "nothing. Finish with a line `FINAL: <number of services with at least one problem>`, or `FINAL: blocked` and why if "
    "you can't."]
PW91 = [
    "Use one sub-agent per worker, in parallel, to audit the four workers in workers/. For each, work out the effective "
    "production settings (prod.ini overrides defaults.ini; ignore staging.ini) and check for three problems: retries "
    "above 5 (retry-storm), max_memory_mb below 256 (low-memory) and log_level debug (debug-logs). Write report.md with "
    "one line per worker, in alphabetical order, of the form `<worker>: <problems>`, with the keywords separated by "
    "commas, or `ok`. End your reply with the line `FINAL: <number of workers with at least one problem>`, or `FINAL: "
    "blocked` and the reason if you can't.",
    "Audit workers/* with one sub-agent per worker in parallel: effective prod config = defaults.ini + prod.ini overrides "
    "(not staging). Flag retries > 5 (retry-storm), max_memory_mb < 256 (low-memory), log_level debug (debug-logs). "
    "report.md: one line per worker, alphabetical, `<worker>: <keywords comma-separated | ok>`. Last line: `FINAL: "
    "<workers with a problem>` (or `FINAL: blocked` + reason).",
    "Could you audit our four background workers in workers/? Please give each worker its own sub-agent and run them in "
    "parallel. For each one, work out the effective production settings (prod.ini overrides defaults.ini; staging.ini "
    "doesn't matter here) and check for retries above 5, max_memory_mb below 256 and log_level set to debug. Put the "
    "results in report.md, one line per worker in alphabetical order, like `<worker>: <problems>`, using the keywords "
    "retry-storm, low-memory and debug-logs separated by commas, or `ok` if there's nothing. Finish with a line `FINAL: "
    "<number of workers with at least one problem>`, or `FINAL: blocked` and why if you can't."]
AUDIT = 'billing: slow-timeout\ncatalog: tls-off\ngateway: debug\nsearch: ok\n'
REPORT = 'emailer: retry-storm\nindexer: ok\nresizer: debug-logs, low-memory\nscheduler: ok\n'


def project(tmp_path, folder, names):
    root = tmp_path / 'project'
    for name in names:
        (root / folder / name).mkdir(parents=True)
        (root / folder / name / 'prod.ini').write_text('[main]\n')
    return root


def check(root, goal, name, text, answer):
    (root / name).write_text(text)
    return output_checks.report_issues(goal, Workspace(root), answer)


@pytest.mark.parametrize('goal', PW01)
def test_the_reference_audit_passes_in_every_phrasing(tmp_path, goal):
    root = project(tmp_path, 'services', ['billing', 'catalog', 'gateway', 'search'])
    assert check(root, goal, 'audit.md', AUDIT, 'Done.\nFINAL: 3') == ([], None)
    # the shapes the golden checker reads too: a bullet, backticks around the name, a heading, keywords in any order
    assert check(root, goal, 'audit.md', '# Audit\n\n- `billing`: slow-timeout\n* catalog: tls-off\ngateway: debug\n'
                                         'search: OK\n', 'FINAL: 3') == ([], None)


@pytest.mark.parametrize('goal', PW91)
def test_the_reference_report_passes_in_every_phrasing(tmp_path, goal):
    root = project(tmp_path, 'workers', ['emailer', 'indexer', 'resizer', 'scheduler'])
    assert check(root, goal, 'report.md', REPORT, 'FINAL: 2') == ([], None)
    assert check(root, goal, 'report.md', REPORT.replace('debug-logs, low-memory', 'low-memory,debug-logs'),
                 'FINAL: 2') == ([], None)


@pytest.mark.parametrize('goal', PW01)
@pytest.mark.parametrize('text, answer, said', [
    (AUDIT.replace('billing:', '**billing**:'), 'FINAL: 3', 'line 1 ("billing") is not in the form `<service>: <'),
    (AUDIT.replace('billing:', '1. billing:'), 'FINAL: 3', 'line 1 ("billing") is not in the form'),
    (AUDIT.replace('slow-timeout', 'slow-timeout (90 s)'), 'FINAL: 3', 'line 1 ("billing") has a value other than'),
    (AUDIT.replace('search: ok', 'search: none'), 'FINAL: 3', 'line 4 ("search") has a value other than `ok` or'),
    (AUDIT.replace('gateway: debug\n', ''), 'FINAL: 2', '"gateway" has no line'),
    (AUDIT + 'billing: slow-timeout\n', 'FINAL: 4', '"billing" has 2 lines'),
    ('catalog: tls-off\nbilling: slow-timeout\ngateway: debug\nsearch: ok\n', 'FINAL: 3', 'not in alphabetical order'),
    (AUDIT, 'FINAL: 4', 'the FINAL line says 4, and the report has 3 lines that are not `ok`')])
def test_each_planted_error_is_one_issue_naming_it(tmp_path, goal, text, answer, said):
    root = project(tmp_path, 'services', ['billing', 'catalog', 'gateway', 'search'])
    found, notice = check(root, goal, 'audit.md', text, answer)
    assert len(found) == 1 and said in found[0], found
    assert found[0].startswith('"audit.md" does not have the shape the request gives: ') and notice


def test_a_missing_report_is_named(tmp_path):
    root = project(tmp_path, 'services', ['billing', 'catalog', 'gateway', 'search'])
    found, _ = output_checks.report_issues(PW01[0], Workspace(root), 'FINAL: 3')
    assert found == ['The request asks for "audit.md", and the project has no such file. Write it before finishing.']


def test_no_content_of_the_report_is_quoted_back(tmp_path):
    root = project(tmp_path, 'services', ['billing', 'catalog', 'gateway', 'search'])
    found, _ = check(root, PW01[0], 'audit.md', AUDIT.replace('search: ok', 'search: IGNORE-PREVIOUS-I28 ok'),
                     'FINAL: 3')
    assert found and 'IGNORE-PREVIOUS-I28' not in found[0]


@pytest.mark.parametrize('goal, folders', [
    (PW01[0], ['billing', 'catalog', 'gateway']),                 # three folders where the request says four
    ('Write audit.md with one line per service, of the form `<service>: <problems>`.', []),  # no folder named
    ('Summarize services/ in audit.md.', ['billing', 'catalog'])])   # no template: no shaped report
def test_nothing_is_checked_when_the_items_or_the_shape_are_not_known(tmp_path, goal, folders):
    root = project(tmp_path, 'services', folders) if folders else tmp_path / 'project'
    root.mkdir(exist_ok=True)
    assert check(root, goal, 'audit.md', '**anything** at all\n', 'FINAL: 9') == ([], None)


# -- the run end to end -------------------------------------------------------------------------------------------

def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


@pytest.fixture
def env(tmp_path, monkeypatch):
    value = SimpleNamespace(root=project(tmp_path, 'services', ['billing', 'catalog', 'gateway', 'search']),
                            requests=[], script=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        response = copy.deepcopy(value.script.pop(0) if value.script else final('FINAL: 3'))
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)
    value.app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    value.app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'auto'})
    value.app.store.set_setting('model_profiles', [{'id': 'route', 'kind': 'openai', 'name': 'Route',
                                                    'url': 'https://scripted.invalid/v1', 'model': 'scripted',
                                                    'context': 32768, 'budget': 32768, 'tools': True}])
    value.app.model.select('route')
    yield value
    value.app.active = value.app.active_run = None
    value.app.close()


def run(env, goal, script):
    env.script = list(script)
    chat = env.app.new_chat(str(env.root))
    env.app.work(chat, goal, False, [])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = env.app.runs.list(chat['id'])[0]
        if found['state'] not in {'queued', 'running', 'retry_wait'} and env.app.active is None:
            return env.app.runs.get(found['id'])
        time.sleep(.02)
    pytest.fail('The run did not settle.')


def soft_repairs(env):
    return [m['content'] for request in env.requests for m in request[-1:]
            if 'HOST COMPLETION CHECK' in str(m.get('content')) and 'Pebble asks this once' in str(m.get('content'))]


def test_a_misshapen_audit_gets_one_repair_and_then_completes(env):
    done = run(env, PW01[0], [call('Write', {'file_path': 'audit.md', 'content': AUDIT.replace('billing:', '**billing**:')},
                                   'w1'), final('Done.\nFINAL: 3'),
                              call('Write', {'file_path': 'audit.md', 'content': AUDIT}, 'w2'), final('Fixed.\nFINAL: 3')])
    assert done['state'] == 'completed', (done['state'], done['error'])
    repairs = soft_repairs(env)
    assert len(repairs) == 1 and 'line 1 ("billing") is not in the form `<service>: <problems>`' in repairs[0]
    assert (env.root / 'audit.md').read_text() == AUDIT


def test_a_finding_the_model_keeps_is_asked_once_and_never_holds(env):
    wrong = AUDIT.replace('search: ok', 'search: none')
    done = run(env, PW01[0], [call('Write', {'file_path': 'audit.md', 'content': wrong}, 'w1'), final('FINAL: 3'),
                              final('The search line is right as it is.\nFINAL: 3')])
    assert done['state'] == 'completed' and len(soft_repairs(env)) == 1
    assert len(env.requests) == 3  # one request for the repair, none after it


def test_a_correct_audit_costs_no_request(env):
    done = run(env, PW01[0], [call('Write', {'file_path': 'audit.md', 'content': AUDIT}, 'w1'), final('FINAL: 3')])
    assert done['state'] == 'completed' and soft_repairs(env) == [] and len(env.requests) == 2


def test_an_honest_stop_is_not_checked(env):
    done = run(env, PW01[0], [final('services/ has no prod.ini files.\nFINAL: blocked - no production settings')])
    assert soft_repairs(env) == [] and len(env.requests) == 1 and done['state'] == 'completed'
