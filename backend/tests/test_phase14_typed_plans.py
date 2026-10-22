"""Phase 14f parent integration: RunPlan, WriteProcessInput, native terminal input and plan credentials.

The executor itself (pipelines, broker network, PTY relay) is W8's and is tested in
test_plan_executor.py / test_pty_input.py. These cases prove the App wiring: a typed
plan always waits for native review, runs supervised after approval, and the model
and the person type into an interactive job under the ECHO-off rule.
"""
import json
import time
import uuid

import pytest

from pebble.policy import PolicyError, Workspace
from test_phase14_security import Host, canary_result, confined_host, host, macos, wait_for  # noqa: F401

GEN = "for word in ('alpha', 'beta', 'gamma'):\n    print(word)\n"
UPPER = "import sys\nfor line in sys.stdin:\n    print(line.strip().upper())\n"
ASK = ("import getpass\nsecret = getpass.getpass('Secret: ')\nname = input('Name: ')\n"
       "print('Hello ' + name + ' (secret length ' + str(len(secret)) + ')')\n")


def stage_plan(host, rid, plan):
    host.own(rid)
    try:
        return host.app.execute('RunPlan', {'plan': json.dumps(plan)}, Workspace(host.project), host.cid,
                                call_id='1:' + uuid.uuid4().hex[:12])
    finally:
        host.release()


def output(host, rid, process_id):
    page = host.app.process_read(host.cid, rid, process_id, after=0, limit=8000)
    return page['untrusted_process_output']


@macos
def test_pipeline_plan_waits_for_native_review_even_in_full_mode_then_runs_supervised(confined_host):
    host = confined_host
    (host.project / 'gen.py').write_text(GEN)
    (host.project / 'upper.py').write_text(UPPER)
    host.permissions('full')
    rid = host.root()
    staged = stage_plan(host, rid, {'steps': [{'argv': ['python3', 'gen.py']}, {'argv': ['python3', 'upper.py']}],
                                    'pipeline': 'linear'})
    assert staged.get('status') == 'awaiting_user_approval' and staged.get('action_id'), staged
    action = next(a for a in host.app.store.actions(host.cid) if a['id'] == staged['action_id'])
    assert action['payload']['typed'] is True and action['payload']['command'].startswith('plan ')
    assert not host.app.store.query('SELECT 1 FROM process_jobs WHERE run_id=?', (rid,)), 'It ran before review.'
    admitted = host.app.approve(host.cid, staged['action_id'])  # the native review, outside the model turn
    row = host.settled(admitted['process_id'])
    assert (row['state'], row['exit_code'], row['contained']) == ('succeeded', 0, 1), dict(row)
    assert output(host, rid, admitted['process_id']).split() == ['ALPHA', 'BETA', 'GAMMA']


def test_typed_plan_refusals_stage_nothing(host):
    (host.project / 'gen.py').write_text(GEN)
    rid = host.root()
    cases = [
        ('not json', 'plan must be JSON'),
        (json.dumps([1, 2]), 'JSON object with a steps list'),
        (json.dumps({'steps': [{'argv': ['python3', 'gen.py']}],
                     'network': {'mode': 'broker', 'destinations': [['api.example.com', 443]],
                                 'credentials': [{'ref': 'never-saved', 'destination': ['api.example.com', 443],
                                                  'methods': ['GET'], 'path_prefix': '/'}]}}),
         'Add these command credentials in Settings first: never-saved'),
        (json.dumps({'steps': [{'argv': ['python3', 'gen.py']}], 'shell': True}), 'Unknown command plan fields'),
    ]
    for raw, message in cases:
        host.own(rid)
        try:
            with pytest.raises(PolicyError, match=message):
                host.app.execute('RunPlan', {'plan': raw}, Workspace(host.project), host.cid,
                                 call_id='1:' + uuid.uuid4().hex[:12])
        finally:
            host.release()
    host.app.runs.transition(rid, 'stopped')  # one live root run per conversation
    host.permissions('ask', web=False)
    rid = host.root()  # a run pins its permissions when it starts
    host.own(rid)
    try:
        with pytest.raises(PolicyError, match='Network access is disabled'):
            host.app.execute('RunPlan', {'plan': json.dumps({'steps': [{'argv': ['python3', 'gen.py']}],
                                                             'network': {'mode': 'broker',
                                                                         'destinations': [['api.example.com', 443]]}})},
                             Workspace(host.project), host.cid, call_id='1:' + uuid.uuid4().hex[:12])
    finally:
        host.release()
    assert not host.app.store.actions(host.cid), 'A refused plan staged an action.'


def test_typed_plans_need_their_owning_root_run(host):
    with pytest.raises(PolicyError, match='owning root run'):
        host.app.execute('RunPlan', {'plan': json.dumps({'steps': [{'argv': ['python3', 'job.py']}]})},
                         Workspace(host.project), host.cid, call_id='1:z')


@macos
def test_interactive_plan_model_and_person_typing_follow_the_echo_rule(confined_host):
    host = confined_host
    (host.project / 'ask.py').write_text(ASK)
    rid = host.root()
    staged = stage_plan(host, rid, {'steps': [{'argv': ['python3', 'ask.py'], 'stdin': 'pty'}], 'interactive': True})
    admitted = host.app.approve(host.cid, staged['action_id'])  # the native review, outside the model turn
    pid = admitted['process_id']
    terminal = lambda: host.app.process_terminal(host.cid, rid, pid)
    assert wait_for(lambda: terminal()['live'] and terminal()['echo'] is False, 20), terminal()
    # A password prompt: the model is refused, the person is accepted and only length/hash are journaled.
    host.own(rid)
    try:
        with pytest.raises(PolicyError):
            host.app.execute('WriteProcessInput', {'process_id': pid, 'text': 'hunter2\n'}, Workspace(host.project),
                             host.cid, call_id='1:w1')
    finally:
        host.release()
    delivered = host.app.process_input_human(host.cid, rid, pid, text='hunter2')
    assert delivered['delivered'] is True and delivered['origin'] == 'human'
    host.app.process_input_human(host.cid, rid, pid, key='enter')
    assert wait_for(lambda: terminal()['echo'] is True, 20), terminal()
    host.own(rid)
    try:
        typed = host.app.execute('WriteProcessInput', {'process_id': pid, 'text': 'Pebble\n'}, Workspace(host.project),
                                 host.cid, call_id='1:w2')
    finally:
        host.release()
    assert typed['delivered'] is True and typed['origin'] == 'model'
    row = host.settled(pid)
    text = output(host, rid, pid)
    assert row['state'] == 'succeeded' and 'Hello Pebble (secret length 7)' in text and 'hunter2' not in text
    journal = json.dumps([e['payload'] for e in host.app.runs.events(rid, 0, 200) if e['kind'] == 'process_input'])
    assert 'hunter2' not in journal and 'Pebble' not in journal, 'Typed text reached the journal.'
    with pytest.raises(PolicyError):
        host.app.process_input_human(host.cid, rid, pid, text='late')  # settled: nothing to type into


def test_model_input_is_screened_and_needs_exactly_one_of_text_or_key(host):
    rid = host.root()
    host.own(rid)
    try:
        with pytest.raises(PolicyError, match='either text or one key'):
            host.app.execute('WriteProcessInput', {'process_id': 'a' * 32}, Workspace(host.project), host.cid,
                             call_id='1:a')
        host.guard_action = 'block'
        with pytest.raises(PolicyError, match='AgentGuard'):
            host.app.execute('WriteProcessInput', {'process_id': 'a' * 32, 'text': 'rm -rf ~\n'},
                             Workspace(host.project), host.cid, call_id='1:b')
    finally:
        host.release()


def test_plan_credentials_keep_values_in_the_vault_and_names_in_settings(host, monkeypatch):
    from pebble import app as app_module
    vault = {}
    monkeypatch.setattr(app_module.PlanVault, 'set', lambda self, account, secret: vault.__setitem__(account, secret))
    monkeypatch.setattr(app_module.PlanVault, 'get', lambda self, account: vault.get(account, ''))
    monkeypatch.setattr(app_module.PlanVault, 'delete', lambda self, account: vault.pop(account, None))
    app = host.app
    assert app.plan_credentials()['names'] == []
    with pytest.raises(PolicyError, match='not a saved command credential'):
        app.plan_credential('api')
    assert app.plan_credential_save('api', 'synthetic-token')['names'] == ['api']
    assert vault == {'plan:api': 'synthetic-token'}
    assert 'synthetic-token' not in json.dumps(app.store.setting('plan_credentials', []))
    value = app.plan_credential('api')
    assert isinstance(value, bytearray) and bytes(value) == b'synthetic-token'
    with pytest.raises(ValueError):
        app.plan_credential_save('bad name!', 'x')
    assert app.plan_credential_delete('api')['names'] == [] and vault == {}
    with pytest.raises(PolicyError):
        app.plan_credential('api')
