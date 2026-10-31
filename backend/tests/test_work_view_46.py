"""Phase 46 (1.9.29, D206, w24-genui-a): the work view, each run_activity item's typed, bounded `view`.

Views are built from Pebble's own records (the tool and action ledgers, content-free run events), once per item and
record change, and reach windows only: the model's requests are byte-identical with views on and off. Every view and
reply conforms to pebble-ui-spec v1 (ui_spec.schema()). The real App, runner, broker and journals run; only the root
model is scripted, as in test_run_activity_22.
"""

import copy
import json
import re
import statistics
import time
from types import SimpleNamespace

import jsonschema
import pytest

from pebble import ui_spec, work_view
from pebble.app import App
from pebble.permissions import DEFAULT
from pebble.providers import Providers
from test_high_fd_waits_21 import close_guard_databases
from test_reasoning_summary_22 import answer, env, no_network, settle, tool  # noqa: F401 (fixtures)

_VALIDATOR = []


def validator():
    if not _VALIDATOR:
        schema = ui_spec.schema()
        jsonschema.Draft202012Validator.check_schema(schema)
        _VALIDATOR.append(jsonschema.Draft202012Validator(schema))
    return _VALIDATOR[0]


def conforms(reply):
    """A run_activity reply: every item, its view, the header and the summary, against pebble-ui-spec v1."""
    check = validator()
    defs = check.schema['$defs']
    for item in reply['items']:
        errors = [e.message for e in check.evolve(schema={**defs['item'], '$defs': defs}).iter_errors(item)]
        assert not errors, (item, errors)
        if 'view' in item:
            assert item['view']['type'] in work_view.SPEC, item  # Pebble builds only the types it publishes
            assert len(json.dumps(item['view'], ensure_ascii=False)) <= work_view.VIEW_BYTES
    for part in ('header', 'summary'):
        if reply.get(part) is not None:
            errors = [e.message for e in check.evolve(schema={**defs[part], '$defs': defs}).iter_errors(reply[part])]
            assert not errors, (part, reply[part], errors)


def activity(app, rid, after=0):
    return app.dispatch('run_activity', {'run_id': rid, 'after': after})


def views(reply, kind=None):
    return [i['view'] for i in reply['items'] if 'view' in i and (kind is None or i['view']['type'] == kind)]


# -- the builders, from records shaped like Pebble's own (ui_spec.fixtures) -------------------------------------------

def test_every_fixture_conforms_and_each_view_type_has_one():
    fixtures = ui_spec.fixtures()
    defs = validator().schema['$defs']
    for name, item in fixtures['items'].items():
        errors = list(validator().evolve(schema={**defs['item'], '$defs': defs}).iter_errors(item))
        assert not errors, (name, [e.message for e in errors])
    shown = {item['view']['type'] for item in fixtures['items'].values() if 'view' in item}
    assert set(work_view.SPEC) <= shown, set(work_view.SPEC) - shown
    assert 'hologram' in shown, 'an unknown type is in the fixtures, so renderers prove their fallback'
    conforms({'items': [], 'header': fixtures['header'], 'summary': fixtures['summary']})


def test_views_are_plain_bounded_and_carry_no_secret_or_private_name():
    fixtures = ui_spec.fixtures()
    text = json.dumps(fixtures['items'], ensure_ascii=False)
    for secret in ('sk-live-0123456789', 'hunter2', 'abc123secret', 'PRIVATE'):
        assert secret not in text, secret
    items = fixtures['items']
    assert items['read_private']['view'] == {'v': 1, 'type': 'read', 'private': True}, 'a private file is never named'
    assert items['web']['view']['sites'] == ['rfc-editor.org', 'docs.python.org', 'example.org'], 'host names only'
    command = items['command_failed']['view']
    assert command['command'] == 'python3 scripts/deploy.py --token=***' and command['where'] == 'Project folder › scripts'
    assert command['tail'][0] == 'Using API_KEY=***' and command['state'] == 'failed' and command['exit'] == 2
    assert re.search(r'[\x00-\x08\x0b-\x1f\x7f]', text) is None


def test_each_card_says_what_happened_from_the_records():
    items = ui_spec.fixtures()['items']
    assert items['read']['view'] == {'v': 1, 'type': 'read', 'path': 'src/auth/session.py', 'start': 1, 'total': 212}
    assert items['edit']['view'] == {'v': 1, 'type': 'edit', 'path': 'src/auth/session.py', 'added': 4, 'removed': 2,
                                     'state': 'done', 'action_id': 'act_edit'}
    assert items['edit_waiting']['view']['new'] is True and items['edit_waiting']['view']['state'] == 'waiting'
    command = items['command']['view']
    assert command['command'] == 'npm run build' and command['exit'] == 0 and command['seconds'] == 4.6
    assert command['tail'][-2:] == ['dist/app.js 182 kB', 'Done in 4.2 s'] and command['network'] is False
    tests = items['tests']['view']
    assert (tests['passed'], tests['failed'], tests['total'], tests['state']) == (41, 1, 42, 'failed')
    assert tests['failing'] == ['tests/test_session.py::test_refresh_expired_session']
    git = items['git_commit']['view']
    assert (git['op'], git['branch'], git['commit'], git['publication']) == ('commit', 'feat/session-refresh',
                                                                            'a1b2c3d4e5f6', False)
    assert items['git_status']['view']['ahead'] == 3 and items['git_status']['view']['behind'] == 2
    window = items['window']['view']
    assert (window['verb'], window['target'], window['before'], window['after'], window['verdict']) == (
        'Pressed', '=', '12*7', '84', 'happened')
    assert window['picture'] == {'ref': '4f1c2a9e0b7d6c5e3a21', 'width': 320, 'height': 480}, 'a reference, no pixels'
    assert items['window_click']['view']['point'] == {'x': 212.0, 'y': 388.0}
    checks = items['checks']['view']
    assert [r['state'] for r in checks['requirements']] == ['met'] * 3 and checks['repaired'] is True
    assert checks['requirements'][2]['text'] == 'The output of make report matches samples/report.txt'
    opened = items['checks_open']['view']
    assert opened['unmet'] == 1 and [r['state'] for r in opened['requirements']] == ['open'], \
        'a requirement the check did not name is never called unmet'
    assert items['review']['view']['decided_by'] == 'pebble' and items['review']['view']['quote'] == 'run the tests'
    assert items['review_waiting']['view']['state'] == 'waiting'
    summary = ui_spec.fixtures()['summary']
    assert summary['files'] == {'count': 1, 'added': 4, 'removed': 2} and summary['commands'] == {'count': 2, 'failed': 1}
    assert summary['tests'] == {'passed': 41, 'failed': 1} and summary['tokens'] == {'input': 52310, 'cached': 28770,
                                                                                    'output': 3120}


def test_the_text_helpers_mask_bound_and_parse():
    for raw, shown in (('curl -H "Authorization: ' + 'Bearer abcdefghijklmnop1234"', 'curl -H "Authorization: Bearer ***"'),
                       ('AWS_ACCESS_KEY_ID=' + 'AKIA' + 'ABCDEFGHIJKLMNOP', 'AWS_ACCESS_KEY_ID=***'),
                       ('git clone https://me:pa55@example.com/r.git', 'git clone https://***@example.com/r.git'),
                       ('password: swordfish now', 'password: *** now'),
                       ('ghp_' + 'a' * 30, '***'), ('npm test', 'npm test')):
        assert work_view.masked(raw) == shown, raw
    assert work_view.diff_counts('--- a/x\n+++ b/x\n@@\n-a\n+b\n+c\n context\n') == (2, 1)
    assert work_view.tail('\n'.join(f'line {n}' for n in range(30))) == [f'line {n}' for n in range(22, 30)]
    assert len(work_view.tail('x' * 500)[0]) == work_view.LINE_CHARS
    output = ('FAILED tests/a.py::test_one - boom\nFAIL: test_two (pkg.Tests)\n--- FAIL: TestThree (0.00s)\n'
              'not ok 4 - four\n' + ''.join(f'FAILED t.py::test_{n}\n' for n in range(20)))
    assert work_view.failing(output)[:4] == ['tests/a.py::test_one', 'test_two (pkg.Tests)', 'TestThree', 'four']
    assert len(work_view.failing(output)) == work_view.FAILING_NAMES and work_view.failing(None) == []
    long = work_view.bounded({'v': 1, 'type': 'tests', 'failing': ['x' * 160] * 40})
    assert long['more'] is True and len(json.dumps(long)) <= work_view.VIEW_BYTES


# -- the live timeline: views in events and reads, rebuilt only when a record changes -------------------------------

def test_a_reviewed_edit_reads_as_cards_live_and_in_the_read_and_resends_when_it_is_approved(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [answer(None, calls=tool('Read', {'file_path': 'calc.py'}, 'read')),
                  answer(None, calls=tool('Edit', {'file_path': 'calc.py', 'old_string': 'return a + b',
                                                   'new_string': 'return b + a'}, 'edit')),
                  answer('Swapped the operands.'), answer('Swapped the operands.')]
    app.work(chat, 'In calc.py swap the operands of the addition.', False, [])
    rid = app.runs.list(cid)[0]['id']
    waiting = activity(app, rid)
    conforms(waiting)
    edit = [v for v in views(waiting, 'edit')]
    assert edit and edit[-1]['state'] == 'waiting' and (edit[-1]['added'], edit[-1]['removed']) == (1, 1)
    assert any(v['path'] == 'calc.py' and v['total'] == 2 for v in views(waiting, 'read'))
    assert views(waiting, 'review')[-1]['state'] == 'waiting'
    assert waiting['summary'] is None and waiting['header']['state'] == 'waiting_approval'
    action = next(a for a in app.store.actions(cid) if a['status'] == 'pending')
    app.approve(cid, action['id'])
    settle(app, rid, 'completed')
    done = activity(app, rid)
    conforms(done)
    assert views(done, 'edit')[-1]['state'] == 'done' and views(done, 'review')[-1]['decided_by'] == 'you'
    assert done['summary']['files'] == {'count': 1, 'added': 1, 'removed': 1}
    # live: the edit's item was sent again once its view changed, and the last event per seq equals the read
    sent = {}
    for name, data in env.events:
        if name == 'activity' and data['run_id'] == rid:
            sent.setdefault(data['item']['seq'], []).append(data['item'])
    seq = next(i['seq'] for i in done['items'] if i.get('view', {}).get('type') == 'edit')
    assert [i['view'].get('state') for i in sent[seq] if 'view' in i][-2:] == ['waiting', 'done']
    assert {s: items[-1] for s, items in sent.items()} == {i['seq']: i for i in done['items']}


def test_views_are_built_once_per_item_and_change_and_switch_off_with_the_control_arm(env, monkeypatch):
    app = env.start()
    chat = app.new_chat(str(env.project))
    env.script = [answer(None, calls=[c for n in range(6) for c in tool('Read', {'file_path': 'calc.py'}, f'r{n}')]),
                  answer('It adds.')]
    app.work(chat, 'Read calc.py a few times and say what it does.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    feed = app.activity
    activity(app, rid)
    built = feed._views.builds
    for _ in range(5):
        activity(app, rid)
        feed.publish(chat['id'], rid)
    assert feed._views.builds == built, 'a settled timeline builds nothing again'
    monkeypatch.setenv('PEBBLE_WORK_VIEW', '0')
    feed._views.runs.clear()
    assert views(activity(app, rid)) == [] and feed._views.builds == built


def test_the_per_step_cost_of_views_is_bounded(env, monkeypatch):
    """At each step the runner publishes the timeline. With views, a warm step (no new item) adds only dictionary
    lookups; a step with new items reads their rows once. Measured on a 120-tool run; the bounds are loose for a shared
    8 GB Mac and the report quotes the measured medians."""
    app = env.start()
    chat = app.new_chat(str(env.project))
    env.script = [answer(None, calls=[c for n in range(20) for c in tool('Read', {'file_path': 'calc.py'}, f's{s}r{n}')])
                  for s in range(6)] + [answer('Done.')]
    app.work(chat, 'Read calc.py many times.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    feed = app.activity

    def timed(views_on):
        began = time.perf_counter()
        feed.items(rid, views=views_on)
        return (time.perf_counter() - began) * 1000
    feed._views.runs.clear()
    cold = timed(True)
    base, warm, one = [], [], []
    for _ in range(41):  # interleaved, so the machine's drift falls on every arm alike
        base.append(timed(False))
        warm.append(timed(True))
        with feed._views.lock:  # the step that brings one new item: its view is built, every other reused
            cache = feed._views.runs[rid]
            del cache[max(seq for seq, entry in cache.items() if entry[1] and entry[1]['type'] == 'read')]
        one.append(timed(True))
    base, warm, one = (statistics.median(times) for times in (base, warm, one))
    published = {'0': [], '1': []}  # the runner's own call at each step, with the control arm interleaved
    for _ in range(41):
        for arm in ('0', '1'):
            monkeypatch.setenv('PEBBLE_WORK_VIEW', arm)
            began = time.perf_counter()
            feed.publish(chat['id'], rid)
            published[arm].append((time.perf_counter() - began) * 1000)
    off, on = (statistics.median(published[arm]) for arm in ('0', '1'))
    count = len(feed.items(rid)[1])
    print(f'\nwork view cost on {count} items (medians of 41): without views {base:.2f} ms per step; with views '
          f'{warm:.2f} ms (+{warm - base:.2f}) when no item is new, {one:.2f} ms (+{one - base:.2f}) when one is; '
          f'every view at once {cold:.1f} ms; publish {off:.2f} ms off, {on:.2f} ms on (+{on - off:.2f})')
    assert count >= 120
    assert warm - base < 3.0, (base, warm)  # lookups only
    assert one - base < 5.0, (base, one)  # plus one bounded read of that item's rows
    assert on - off < 3.0, (off, on)
    assert cold < 150, cold  # every view of the run at once (a reopened conversation), never at each step


# -- the model sees nothing new -------------------------------------------------------------------------------------

def _arm(tmp_path, monkeypatch, name, enabled, captured):
    monkeypatch.setenv('PEBBLE_WORK_VIEW', '1' if enabled else '0')
    root = tmp_path / name
    project = root / 'project'
    project.mkdir(parents=True)
    (project / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
    events = []
    app = App(root / 'data', root / 'resources', lambda *args: events.append(copy.deepcopy(args)))
    app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'ask'})
    app.model_save(name='Summary fixture', url='http://127.0.0.1:9', model='summary-fixture', context=32768)
    script = [answer(None, calls=tool('Read', {'file_path': 'calc.py'}, 'read')),
              answer(None, calls=tool('Grep', {'pattern': 'add'}, 'grep')),
              answer(None, calls=tool('Edit', {'file_path': 'calc.py', 'old_string': 'return a + b',
                                               'new_string': 'return b + a'}, 'edit')),
              answer('Swapped them.'), answer('Swapped them.')]
    state = SimpleNamespace(script=script, during=[], events=events)

    def complete(provider, messages, tools, cancel, token, effort=False):
        captured.append(json.dumps({'messages': messages, 'tools': tools}, sort_keys=True, default=str))
        return state.script.pop(0)(token, state)
    monkeypatch.setattr(Providers, 'complete', complete)
    try:
        chat = app.new_chat(str(project))
        app.work(chat, 'In calc.py swap the operands of the addition.', False, [])
        rid = app.runs.list(chat['id'])[0]['id']
        action = next(a for a in app.store.actions(chat['id']) if a['status'] == 'pending')
        app.approve(chat['id'], action['id'])
        settle(app, rid, 'completed')
        shown = [d['item'] for n, d in events if n == 'activity' and 'view' in d['item']]
        return str(root), shown
    finally:
        app.active = app.active_run = None
        app.close()
        close_guard_databases(root)


def _normalized(requests, root):
    text = '\n'.join(requests).replace(root, '<root>')
    text = re.sub(r'\b[0-9a-f]{32}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', '<id>', text)
    text = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?', '<time>', text)
    return re.sub(r'\b\d{1,2}:\d{2}\b', '<clock>', text)


def test_the_models_requests_are_byte_identical_with_the_work_view_on_and_off(tmp_path, monkeypatch):
    on, off = [], []
    root_on, shown = _arm(tmp_path, monkeypatch, 'on', True, on)
    root_off, hidden = _arm(tmp_path, monkeypatch, 'off', False, off)
    assert shown and not hidden, 'the on arm built views and the off arm none'
    assert len(on) == len(off) >= 4
    assert _normalized(on, root_on) == _normalized(off, root_off)
    assert all('"view": {' not in request for request in on), 'no view reaches a request'
