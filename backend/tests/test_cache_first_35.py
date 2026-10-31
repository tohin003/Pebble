"""Phase 35 (1.3.0, beat plan W5): above 8K every request of a run extends the previous one.

A provider's prompt cache can only reuse a request's prefix. Measured on the Phase 34 source (runner.prefix_report on a
scripted 32K coding task), every request from the second on kept only the system message: the task checkpoint at index
1 was rewritten after each tool result, and the source audit, plan and diagnostics were inserted at index 2 and
rewritten. Above 8K the checkpoint is now written once (with the local time, which left the system prompt), and the
audit and plan are appended after the newest results when they change; pinned skills, plugin schemas and connector
routes stay where they were first appended. At 8K and below nothing changes (the golden fixtures pin it). Diagnostics
and ReadSource keep their Phase 20 and Phase 16 rules (stale diagnostics are dropped; ReadSource joins once a page is
recorded), each costing one cache miss when it happens.

The App runner, broker and sandboxed test runner are the checked-in code; only the root model is scripted.
"""
import json
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'scripts'))

import validate_phase34 as v34  # noqa: E402  (its scripted-model harness)
from pebble import runner  # noqa: E402

PLAN1 = json.dumps([{'id': 'read', 'title': 'Read stats.py', 'status': 'in_progress'},
                    {'id': 'fix', 'title': 'Fix mean', 'status': 'pending'}])
PLAN2 = json.dumps([{'id': 'read', 'title': 'Read stats.py', 'status': 'completed'},
                    {'id': 'fix', 'title': 'Fix mean', 'status': 'in_progress'}])
macos = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox')


def run(tmp_path, script, text, context=32768):
    folder = v34.project(tmp_path / 'project')
    app = v34.app_for(tmp_path, 'auto', context)
    try:
        with v34.Scripted() as scripted:
            scripted.script = script + [v34.final('Done.')] * 3
            chat = app.new_chat(str(folder))
            app.work(chat, text, False, [])
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                runs = app.runs.list(chat['id'])
                if runs and app.active is None and runs[0]['state'] not in ('queued', 'running', 'retry_wait'):
                    break
                time.sleep(.02)
            rid = app.runs.list(chat['id'])[0]['id']
            reports = [e['payload'].get('prefix') for e in app.runs.events(rid, limit=200) if e['kind'] == 'model_attempt']
            return reports, [r for r in scripted.requests], app.runs.get(rid)
    finally:
        v34.close(app)


# -- the measurement -----------------------------------------------------------------------------------------------

def test_prefix_report_counts_the_kept_messages_and_names_the_first_change():
    state = {}
    a = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'c', '_pebble_checkpoint': True}]
    assert runner.prefix_report(state, a, []) == {'messages': 2, 'first': True}
    b = a + [{'role': 'assistant', 'content': 'x'}]
    assert runner.prefix_report(state, b, []) == {'messages': 3, 'previous': 2, 'kept': 2, 'tools_changed': False,
                                                  'append_only': True}
    c = [b[0], {'role': 'user', 'content': 'changed', '_pebble_checkpoint': True}, b[2]]
    report = runner.prefix_report(state, c, [{'type': 'function', 'function': {'name': 'Read'}}])
    assert report['kept'] == 1 and not report['append_only'] and report['tools_changed']
    assert report['first_change'] == {'index': 1, 'role': 'user', 'name': '_pebble_checkpoint'}
    # Host markers never reach the wire, so a marker-only change is not a change of the request.
    d = [dict(m) for m in c]
    d[1].pop('_pebble_checkpoint')
    assert runner.prefix_report(state, d, [{'type': 'function', 'function': {'name': 'Read'}}])['append_only'] is True


# -- append-only above 8K --------------------------------------------------------------------------------------------

@macos
def test_a_coding_task_extends_every_request_above_8k(tmp_path):
    reports, requests, _ = run(tmp_path, [
        v34.call('Read', {'file_path': 'stats.py'}, 'r1'), v34.call('Read', {'file_path': 'test_stats.py'}, 'r2'),
        v34.call('Write', {'file_path': 'stats.py', 'content': v34.FIXED}, 'w1'),
        v34.call('RunTests', {'recipe': 'python-unittest', 'check': v34.CHECK}, 'c1'),
        v34.call('RunTests', {'recipe': 'python-unittest'}, 't1')], 'Make mean return 0.0 for an empty list.')
    # Phase 46 (runner.PREFETCH): the host reads the tiny project's stats.py and test_stats.py before the first request,
    # shown in one HOST PREFETCH message appended after the request (5 messages without it; its views are current, so
    # no audit lists them until one changes); every later request still extends the one before.
    extra = ['pebble_prefetch'] if runner.PREFETCH else []
    assert reports[0] == {'messages': 5 + len(extra), 'first': True}
    assert [m.get('name') for m in requests[0][5:]] == extra
    assert len(reports) >= 6 and all(r['append_only'] for r in reports[1:]), reports
    # The audit is appended, listing only changed views; the earlier ones stay where they were.
    audits = [m for m in requests[-1] if m.get('name') == 'pebble_source_status']
    assert len(audits) >= 2 and all(m.get('_pebble_source') and runner.AUDIT_TEXT in m['content'] for m in audits)
    assert all('Only views whose status changed' in m['content'] for m in audits)


def test_a_plan_is_appended_and_only_the_newest_is_protected(tmp_path):
    reports, requests, _ = run(tmp_path, [
        v34.call('UpdatePlan', {'steps': PLAN1}, 'p1'), v34.call('Read', {'file_path': 'stats.py'}, 'r1'),
        v34.call('UpdatePlan', {'steps': PLAN2}, 'p2')], 'First plan the work, then make mean return 0.0 for an empty list.')
    assert all(r['append_only'] for r in reports[1:]), reports
    plans = [m for m in requests[-1] if isinstance(m.get('content'), str) and m['content'].startswith('HOST PLAN')]
    assert plans and plans[-1].get('_pebble_plan') and all(not m.get('_pebble_plan') for m in plans[:-1])
    assert all(m.get('_pebble_source') for m in plans[:-1])


def test_the_checkpoint_is_written_once_with_the_local_time_and_the_system_prompt_has_the_date_only(tmp_path):
    _, requests, _ = run(tmp_path, [v34.call('Read', {'file_path': 'stats.py'}, 'r1')], 'What does stats.py do?')
    checkpoints = [next(m for m in request if m.get('_pebble_checkpoint') and m.get('name') is None) for request in requests]
    assert len({c['content'] for c in checkpoints}) == 1
    assert checkpoints[0]['content'].startswith(runner.STABLE_CHECKPOINT)
    assert re.search(r'Local time when this task started: \d{2}:\d{2}', checkpoints[0]['content'])
    system = requests[0][0]['content']
    assert re.search(r'Current date: \d{4}-\d{2}-\d{2}\.', system) and 'Local time' not in system


@pytest.mark.parametrize('context', [8192, 4096])
def test_at_8k_and_below_the_checkpoint_still_carries_the_latest_observations(tmp_path, context):
    reports, requests, _ = run(tmp_path, [v34.call('Read', {'file_path': 'stats.py'}, 'r1')], 'What does stats.py do?',
                               context=context)
    assert all(r is None for r in reports)  # measured above 8K only
    checkpoint = next(m for m in requests[-1] if m.get('_pebble_checkpoint') and m.get('name') is None)
    assert checkpoint['content'].startswith('HOST TASK CHECKPOINT') and 'stats.py' in checkpoint['content']
