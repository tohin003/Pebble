"""Phase 24 (V1-030): a run that wrote a broken JSON or CSV file does not settle "completed".

AgentGuard's gate verifies only source code: JSON, CSV and Markdown are not "source", so 0.6.1 completed whatever the
model wrote, and an invalid policy.json settled completed (the T3 mechanism behind the documents family). Now the
written .json must parse (and have the keys the request lists) and a written .csv must be rectangular and start with
the header the request gives; each problem says where the file breaks, and the run gets one repairable issue.

The App, runner, controller and output checks are the checked-in code; only the model is scripted.
"""

import copy
import json
import time
from types import SimpleNamespace

import pytest

from pebble import output_checks
from pebble.app import App
from pebble.eval_corpus_18 import CORPUS as TASKS
from pebble.permissions import DEFAULT
from pebble.policy import Workspace
from pebble.providers import Providers

POLICY_GOAL = next(t for t in TASKS if t.id == 'doc-policy').prompt
FEES_GOAL = next(t for t in TASKS if t.id == 'doc-pdf-fees').prompt
POLICY_KEYS = ['meal_cap_eur_per_day', 'hotel_cap_eur_per_night', 'high_cost_city_hotel_cap_eur', 'high_cost_cities',
               'economy_below_flight_hours', 'director_approval_above_eur', 'mileage_eur_per_km',
               'claim_deadline_days', 'business_class_allowed']
VALID = json.dumps({key: 1 for key in POLICY_KEYS}, indent=2) + '\n'
INVALID = '{\n  "meal_cap_eur_per_day": 40,\n  "hotel_cap_eur_per_night": 120\n  "high_cost_cities": []\n}\n'


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    return SimpleNamespace(root=root, workspace=lambda: Workspace(root))


def check(ws, goal, files):
    for name, text in files.items():
        (ws.root / name).write_text(text)
    return output_checks.issues(goal, ws.workspace(), list(files))


def test_the_corpus_requests_list_their_keys_and_header():
    assert output_checks.listed_keys(POLICY_GOAL) == (POLICY_KEYS, True)
    header = output_checks.listed_header(FEES_GOAL)
    assert header and header[0] and len(header) >= 2
    assert output_checks.listed_keys('Summarize the key ideas, themes and conclusions into notes.json.') == (None, False)


def test_invalid_json_is_held_with_its_line_and_column(ws):
    issues, notice = check(ws, POLICY_GOAL, {'policy.json': INVALID})
    assert len(issues) == 1 and issues[0].startswith('"policy.json" is not valid JSON: line 4, column 3')
    assert issues[0].endswith('Fix the file before finishing.')
    assert notice == 'A file Pebble wrote is not valid JSON yet.'


def test_valid_json_with_the_listed_keys_passes(ws):
    assert check(ws, POLICY_GOAL, {'policy.json': VALID}) == ([], None)


def test_the_listed_key_set_is_enforced(ws):
    wrong = json.loads(VALID)
    wrong.pop('claim_deadline_days')
    wrong['notes'] = 'extra'
    issues, _ = check(ws, POLICY_GOAL, {'policy.json': json.dumps(wrong)})
    assert issues and 'missing ["claim_deadline_days"]' in issues[0] and 'not requested ["notes"]' in issues[0]
    issues, _ = check(ws, POLICY_GOAL, {'policy.json': json.dumps([1, 2])})
    assert issues and 'must be one JSON object' in issues[0]


def test_a_csv_header_mismatch_and_a_ragged_row_are_held(ws):
    header = output_checks.listed_header(FEES_GOAL)
    good = ','.join(header) + '\n' + ','.join(['x'] * len(header)) + '\n'
    assert check(ws, FEES_GOAL, {'fees.csv': good}) == ([], None)
    issues, notice = check(ws, FEES_GOAL, {'fees.csv': 'kind,price\n' + ','.join(['x'] * 2) + '\n'})
    assert issues and 'must start with the header row the request gives' in issues[0] and notice.endswith('CSV yet.')
    issues, _ = check(ws, FEES_GOAL, {'fees.csv': good + 'only,two\n'})
    assert issues and 'is not rectangular: line 3 has 2 fields' in issues[0]
    issues, _ = check(ws, FEES_GOAL, {'fees.csv': ','.join(header) + '\n\n' + ','.join(['x'] * len(header)) + '\n'})
    assert issues and 'line 2 is blank' in issues[0]


def test_no_project_content_is_quoted_back(ws):
    secret = 'I24-OUTPUT-CANARY'
    issues, _ = check(ws, POLICY_GOAL, {'policy.json': '{"' + secret + '": tru}'})
    assert issues and secret not in issues[0]


# -- the run end to end ---------------------------------------------------------------------------------------------

def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    (root / 'docs').mkdir(parents=True)
    (root / 'docs' / 'travel_policy.md').write_text('# Travel policy\nMeals: 40 EUR a day.\n')
    value = SimpleNamespace(root=root, requests=[], script=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        response = copy.deepcopy(value.script.pop(0) if value.script else final('FINAL: written'))
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)
    value.app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    value.app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'auto'})
    yield value
    value.app.active = value.app.active_run = None
    value.app.close()


def run(env, script):
    env.script = list(script)
    chat = env.app.new_chat(str(env.root))
    env.app.work(chat, POLICY_GOAL, False, [])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = env.app.runs.list(chat['id'])[0]
        if found['state'] not in {'queued', 'running', 'retry_wait'} and env.app.active is None:
            return env.app.runs.get(found['id'])
        time.sleep(.02)
    pytest.fail('The run did not settle.')


def test_an_invalid_policy_json_holds_the_run_and_one_repair_fixes_it(env):
    written = run(env, [call('Read', {'file_path': 'docs/travel_policy.md'}, 'read'),
                        call('Write', {'file_path': 'policy.json', 'content': INVALID}, 'write'),
                        final('FINAL: written'),
                        call('Write', {'file_path': 'policy.json', 'content': VALID}, 'fix'),
                        final('FINAL: written')])
    assert written['state'] == 'completed', (written['state'], written['error'])
    repairs = [m['content'] for m in env.requests[3] if 'HOST COMPLETION CHECK' in str(m.get('content'))]
    assert len(repairs) == 1 and '"policy.json" is not valid JSON: line 4, column 3' in repairs[0]
    assert json.loads((env.root / 'policy.json').read_text()) == json.loads(VALID)


def test_a_json_that_stays_invalid_settles_verification_required(env):
    held = run(env, [call('Write', {'file_path': 'policy.json', 'content': INVALID}, 'write'),
                     final('FINAL: written'), final('FINAL: written'), final('FINAL: written')])
    assert held['state'] == 'verification_required' and 'not valid JSON' in held['error']
