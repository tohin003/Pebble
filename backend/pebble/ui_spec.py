"""Phase 46 (1.9.29, D206, w24-genui-a): pebble-ui-spec v1, the one description of what Pebble's windows draw.

Two parts, both data, never code:
- the work view: each run_activity item's `view` (work_view.SPEC), the reply's `header` and `summary`;
- the native kinds of UIRender (artifact_schema.py and artifacts.validate), which a model fills and Pebble draws.

scripts/generate_ui_spec.py writes schema() and fixtures() to docs/ui-spec/, and the lead publishes them to the iPhone
side as section 3.12 of the bridge contract. The work view's part is derived from work_view.SPEC, so it cannot drift
from what Pebble builds; every kind artifact_schema defines is listed (kinds()), and one whose fields this module does
not describe yet is published with its one-line summary (artifact_schema.SCHEMAS) and marked "x-pebble-shape":
"summary" until NATIVE gains it (test_ui_spec_46 names such kinds). Consumers: render by type, ignore fields they do not
know, and show an unknown type as its item's label and detail (on the iPhone: "Update the app to see this view").
"""

import copy
import json

from . import artifact_schema, work_view

SPEC_ID = 'https://pebble.local/ui-spec/v1'  # an identifier, never fetched
TITLE = 'pebble-ui-spec v1'
ID = {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,40}$'}
FALLBACK = 'Update the app to see this view'  # what the iPhone shows for an unknown type
MAC_ONLY = ('html',)  # a frame with no scripts, shown on the Mac after review; never drawn on the phone (bridge C5)


def _text(limit):
    return {'type': 'string', 'maxLength': limit}


def _field(kind, bound):
    if kind == 'text':
        return _text(bound)
    if kind == 'int':
        return {'type': 'integer', 'minimum': 0}
    if kind == 'num':
        return {'type': 'number', 'minimum': 0}
    if kind == 'bool':
        return {'type': 'boolean'}
    if kind == 'enum':
        return {'enum': list(bound)}
    if kind == 'list':
        return {'type': 'array', 'maxItems': bound[0], 'items': _field(bound[1][0], bound[1][1])}
    if kind == 'object':
        return _object(bound)
    raise ValueError(kind)


def _nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


def _object(fields, nullable=False):
    """A strict object of work_view field specs; `nullable` lets every field be null (header and summary report
    unknowns as null, views leave them out)."""
    properties = {k: _nullable(_field(kind, bound)) if nullable else _field(kind, bound)
                  for k, (kind, bound, *_) in fields.items()}
    untrusted = sorted(k for k, spec in fields.items() if len(spec) > 2 and spec[2])
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            **({'required': list(fields)} if nullable else {}),
            **({'x-pebble-untrusted': untrusted} if untrusted else {})}


def view_defs():
    """$defs for every work view type: `v` 1, `type`, optional `more`, then the type's own fields."""
    found = {}
    for name, fields in work_view.SPEC.items():
        schema = _object(fields)
        schema['properties'] = {'v': {'const': work_view.VERSION}, 'type': {'const': name},
                                'more': {'type': 'boolean'}, **schema['properties']}
        schema['required'] = ['v', 'type']
        found['view.' + name] = schema
    return found


# -- the native kinds of UIRender, described as artifact_schema and artifacts.validate accept them -----------------

_PLAIN = 'Plain text: never a link, URL, markup, script, template or credential (artifact_schema.REJECTED_TEXT, SECRET).'
_CELL = {'type': ['string', 'number', 'boolean', 'null'], 'maxLength': 2000}
_STEP = {'enum': ['pending', 'in_progress', 'blocked', 'completed']}
_EVIDENCE = {'type': 'array', 'maxItems': 32, 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1,
                                                                            'maxLength': 350}}


def _strict(properties, required):
    return {'type': 'object', 'additionalProperties': False, 'required': list(required), 'properties': properties}


_TABLE = _strict({'columns': {'type': 'array', 'minItems': 1, 'maxItems': 12,
                              'items': _strict({'id': ID, 'label': _text(120)}, ('id', 'label'))},
                  'rows': {'type': 'array', 'maxItems': 200,
                           'items': {'type': 'object', 'additionalProperties': _CELL}},
                  'caption': _text(1000)}, ('columns', 'rows'))
# Phase 46 (1.9.29, w25-genui-b): chart types, number formats and a unit; pie and donut draw one series of 2-8 parts.
_CHART = _strict({'type': {'enum': list(artifact_schema.CHART_TYPES)}, 'x_key': ID,
                  'y_keys': {'type': 'array', 'minItems': 1, 'maxItems': 6, 'uniqueItems': True, 'items': ID},
                  'rows': {'type': 'array', 'minItems': 1, 'maxItems': 200,
                           'items': {'type': 'object', 'additionalProperties': _CELL}},
                  'labels': {'type': 'object', 'additionalProperties': _text(120)},
                  'caption': _text(1000), 'format': {'enum': list(artifact_schema.NUMBER_FORMATS)},
                  'unit': _text(12)}, ('type', 'x_key', 'y_keys', 'rows'))
_CHART['allOf'] = [{'if': {'properties': {'type': {'enum': ['pie', 'donut']}}},
                    'then': {'properties': {'y_keys': {'maxItems': 1}, 'rows': {'minItems': 2, 'maxItems': 8}}}}]
_TONE = {'enum': list(artifact_schema.TONES)}
_NUMBER = {'type': 'number', 'maximum': 1e15, 'minimum': -1e15}
_VALUE = {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 160}, _NUMBER, {'type': 'boolean'}]}
# The visual catalog (artifact_schema.CATALOG_KINDS): dates are ISO values the renderer formats; numbers are raw.
_CATALOG = {
    'compare': _strict({'options': {'type': 'array', 'minItems': 2, 'maxItems': 4, 'items': _strict(
                            {'id': ID, 'label': _text(80), 'summary': _text(240)}, ('id', 'label'))},
                        'criteria': {'type': 'array', 'minItems': 1, 'maxItems': 10, 'items': _strict(
                            {'label': _text(80), 'values': {'type': 'array', 'minItems': 2, 'maxItems': 4,
                                                            'items': _VALUE}}, ('label', 'values'))},
                        'recommended': ID, 'caption': _text(400)}, ('options', 'criteria')),
    'metrics': _strict({'tiles': {'type': 'array', 'minItems': 2, 'maxItems': 4, 'items': _strict(
                            {'label': _text(60), 'value': _NUMBER, 'unit': _text(12), 'delta': _NUMBER,
                             'format': {'enum': list(artifact_schema.NUMBER_FORMATS)}, 'better': {'enum': ['up', 'down']},
                             'caption': _text(120)}, ('label', 'value'))},
                        'caption': _text(400)}, ('tiles',)),
    'timeline': _strict({'events': {'type': 'array', 'minItems': 2, 'maxItems': 20, 'items': _strict(
                             {'date': {'type': 'string', 'pattern': r'^\d{4}(-\d{2}(-\d{2})?)?$'}, 'title': _text(120),
                              'detail': _text(400), 'tone': _TONE}, ('date', 'title'))},
                         'caption': _text(400)}, ('events',)),
    'record': _strict({'fields': {'type': 'array', 'minItems': 1, 'maxItems': 12, 'items': _strict(
                           {'label': _text(60), 'value': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 160},
                                                                    _NUMBER]}}, ('label', 'value'))},
                       'subtitle': _text(80), 'note': _text(400)}, ('fields',)),
}
_PLAN = _strict({'steps': {'type': 'array', 'minItems': 1, 'maxItems': 20,
                           'items': _strict({'id': ID, 'title': _text(240), 'status': _STEP,
                                             'evidence_ids': _EVIDENCE}, ('id', 'title', 'status'))}}, ('steps',))
_EVIDENCE_VIEW = _strict({'items': {'type': 'array', 'minItems': 1, 'maxItems': 30,
                                    'items': _strict({'id': ID, 'label': _text(240), 'evidence_id': _text(350),
                                                      'summary': _text(2000)}, ('id', 'label', 'evidence_id'))},
                          'caption': _text(1000)}, ('items',))
_METRIC = _strict({'value': {'type': 'number'}, 'unit': _text(24), 'delta': {'type': 'number'},
                   'caption': _text(240)}, ('value',))
_COMPONENT = {'oneOf': [
    _strict({'type': {'const': 'heading'}, 'text': _text(240)}, ('type', 'text')),
    _strict({'type': {'const': 'button'}, 'label': _text(240), 'goto': ID}, ('type', 'label')),
    _strict({'type': {'const': 'input'}, 'label': _text(240), 'placeholder': _text(240)}, ('type', 'label')),
    _strict({'type': {'const': 'toggle'}, 'label': _text(240)}, ('type', 'label')),
    _strict({'type': {'const': 'list'}, 'items': {'type': 'array', 'maxItems': 30, 'items': _text(240)}},
            ('type', 'items')),
    _strict({'type': {'const': 'card'}, 'title': _text(240), 'body': _text(2000)}, ('type', 'title', 'body')),
    _strict({'type': {'const': 'image_placeholder'}, 'label': _text(240),
             'aspect': {'enum': list(artifact_schema.ASPECTS)}}, ('type', 'label')),
    _strict({'type': {'const': 'nav'}, 'items': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                                                 'items': _strict({'label': _text(120), 'goto': ID},
                                                                  ('label', 'goto'))}}, ('type', 'items'))]}
NATIVE = {
    'form': _strict({'fields': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': _strict(
        {'id': ID, 'label': _text(240), 'type': {'enum': ['text', 'choice']}, 'required': {'const': True},
         'placeholder': _text(240),
         'options': {'type': 'array', 'minItems': 2, 'maxItems': 6,
                     'items': _strict({'id': ID, 'label': _text(120)}, ('id', 'label'))}}, ('id', 'label', 'type'))},
        'description': _text(1500), 'submit_label': _text(80)}, ('fields',)),
    'table': _TABLE,
    'chart': _CHART,
    'plan': _PLAN,
    'evidence': _EVIDENCE_VIEW,
    'quiz': _strict({'duration_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 180},
                     'questions': {'type': 'array', 'minItems': 1, 'maxItems': 50, 'items': _strict(
                         {'question': {'type': 'string', 'minLength': 1, 'maxLength': 1500},
                          'choices': {'type': 'array', 'minItems': 2, 'maxItems': 6,
                                      'items': {'type': 'string', 'minLength': 1, 'maxLength': 700}},
                          'answer': {'type': 'integer', 'minimum': 0},
                          'explanation': {'type': 'string', 'minLength': 1, 'maxLength': 1500}},
                         ('question', 'choices', 'answer', 'explanation'))}}, ('duration_minutes', 'questions')),
    # Phase 46 (1.9.29, w25-genui-b): layout, direction and caption; a node's group (its layer), sub and tone
    'diagram': _strict({'nodes': {'type': 'array', 'minItems': 1, 'maxItems': 40, 'items': _strict(
                            {'id': {'type': 'string', 'minLength': 1, 'maxLength': 60},
                             'label': {'type': 'string', 'minLength': 1, 'maxLength': 180},
                             'group': _text(40), 'sub': _text(60), 'tone': _TONE}, ('id', 'label'))},
                        'edges': {'type': 'array', 'maxItems': 80, 'items': _strict(
                            {'from': {'type': 'string'}, 'to': {'type': 'string'}, 'label': _text(100)},
                            ('from', 'to', 'label'))},
                        'layout': {'enum': list(artifact_schema.DIAGRAM_LAYOUTS)},
                        'direction': {'enum': ['down', 'right']}, 'caption': _text(400)}, ('nodes', 'edges')),
    'dashboard': _strict({'columns': {'enum': [1, 2, 3]},
                          'panels': {'type': 'array', 'minItems': 1, 'maxItems': artifact_schema.MAX_PANELS,
                                     'items': {'oneOf': [
                                         _strict({'id': ID, 'title': _text(120),
                                                  'kind': {'enum': list(artifact_schema.PANEL_KINDS)},
                                                  'data': {'type': 'object'}}, ('id', 'title', 'kind', 'data')),
                                         _strict({'id': ID, 'title': _text(120),
                                                  'kind': {'enum': list(artifact_schema.PANEL_KINDS)},
                                                  'binding': _strict({'source': _text(40), 'params': {'type': 'object'}},
                                                                     ('source',))},
                                                 ('id', 'title', 'kind', 'binding'))]}}}, ('panels',)),
    'prototype': _strict({'viewport': {'enum': list(artifact_schema.VIEWPORTS)}, 'start': ID,
                          'theme': _strict({'accent': {'enum': list(artifact_schema.THEME['accent'])},
                                            'density': {'enum': list(artifact_schema.THEME['density'])},
                                            'radius': {'enum': list(artifact_schema.THEME['radius'])}}, ()),
                          'screens': {'type': 'array', 'minItems': 1, 'maxItems': artifact_schema.MAX_SCREENS,
                                      'items': _strict({'id': ID, 'title': _text(120),
                                                        'components': {'type': 'array',
                                                                       'maxItems': artifact_schema.MAX_COMPONENTS,
                                                                       'items': _COMPONENT}},
                                                       ('id', 'title', 'components'))}}, ('start', 'screens')),
    # a dashboard's metric panel data (artifact_schema.metric), named for the renderers
    'metric': _METRIC,
    **_CATALOG,
}


def kinds():
    """Every native kind artifact_schema defines, in its own order (LEGACY_KINDS, NEW_KINDS, then any other kind it
    names in NATIVE_KINDS or SCHEMAS), so a kind added there is published here without an edit."""
    found = list(dict.fromkeys([*artifact_schema.LEGACY_KINDS, *artifact_schema.NEW_KINDS,
                                *sorted(artifact_schema.NATIVE_KINDS), *artifact_schema.SCHEMAS]))
    return tuple(found)


def native_defs():
    found = {}
    for kind in kinds():
        if kind in MAC_ONLY:
            found['native.' + kind] = {'type': 'object', 'description': artifact_schema.SCHEMAS.get(kind, kind),
                                       'x-pebble-surfaces': ['mac']}
        elif kind in NATIVE:
            found['native.' + kind] = {**copy.deepcopy(NATIVE[kind]), 'description': _PLAIN}
        else:
            found['native.' + kind] = {'type': 'object', 'x-pebble-shape': 'summary',
                                       'description': artifact_schema.SCHEMAS.get(kind, kind) + ' ' + _PLAIN}
    found['native.metric'] = {**copy.deepcopy(NATIVE['metric']), 'description': 'A dashboard metric panel. ' + _PLAIN}
    return found


def summary_only():
    """The native kinds published only with their one-line summary (no field schema in NATIVE yet)."""
    return tuple(k for k in kinds() if k not in NATIVE and k not in MAC_ONLY)


def schema():
    """The pebble-ui-spec v1 JSON Schema (draft 2020-12): an activity item, a run_activity reply, a native view."""
    from .run_activity import DETAIL_CHARS, KINDS, LABEL_CHARS
    views = view_defs()
    defs = {
        **views,
        'view': {'type': 'object', 'required': ['v', 'type'],
                 'properties': {'v': {'const': work_view.VERSION}, 'type': {'type': 'string', 'maxLength': 40}},
                 'description': 'A known type follows its definition; an unknown type is drawn as its item\'s label '
                                'and detail.',
                 'allOf': [{'if': {'properties': {'type': {'const': name[5:]}}}, 'then': {'$ref': '#/$defs/' + name}}
                           for name in views]},
        'item': {'type': 'object', 'additionalProperties': False,
                 'required': ['seq', 'at', 'kind', 'label', 'detail', 'duration', 'status'],
                 'properties': {'seq': {'type': 'integer', 'minimum': 0}, 'at': {'type': 'number', 'minimum': 0},
                                'kind': {'enum': list(KINDS)},
                                'label': {'type': 'string', 'minLength': 1, 'maxLength': LABEL_CHARS},
                                'detail': _nullable(_text(DETAIL_CHARS)),
                                'duration': _nullable({'type': 'number', 'minimum': 0}),
                                'status': {'enum': ['running', 'done', 'failed']},
                                'headers_ms': _nullable({'type': 'integer', 'minimum': 0}),
                                'first_event_ms': _nullable({'type': 'integer', 'minimum': 0}),
                                'view': {'$ref': '#/$defs/view'}}},
        'header': _object(work_view.HEADER, nullable=True),
        'summary': _object(work_view.SUMMARY, nullable=True),
        'activity_event': _strict({'chat_id': {'type': 'string'}, 'run_id': {'type': 'string'},
                                   'item': {'$ref': '#/$defs/item'}}, ('chat_id', 'run_id', 'item')),
        **native_defs(),
        'native': {'type': 'object', 'required': ['kind', 'data'],
                   'properties': {'kind': {'type': 'string'}, 'title': _text(120), 'data': {'type': 'object'}},
                   'allOf': [{'if': {'properties': {'kind': {'const': kind}}},
                              'then': {'properties': {'data': {'$ref': '#/$defs/native.' + kind}}}} for kind in kinds()]},
    }
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': SPEC_ID, 'title': TITLE,
            'description': 'What Pebble\'s windows draw: the work view (each activity item\'s view, the reply\'s '
                           'header and summary) and the native kinds of UIRender. Data only; renderers own the design. '
                           'Render by type, ignore unknown fields, and show an unknown type as its item\'s label and '
                           'detail ("' + FALLBACK + '" on the iPhone).',
            'x-pebble-version': work_view.VERSION, 'x-pebble-native-kinds': list(kinds()),
            'x-pebble-view-types': list(work_view.SPEC), '$defs': defs}


# -- fixtures: built by the real builders from records shaped like Pebble's own ------------------------------------

def _row(name, **fields):
    row = dict.fromkeys(work_view.ROW_COLUMNS)
    return {**row, 'call_id': '1:call_' + name, 'name': name, 'state': 'completed', **fields}


def _action(kind, status='applied', **fields):
    return {**dict.fromkeys(work_view.ACTION_COLUMNS), 'id': 'act_' + kind, 'kind': kind, 'status': status, **fields}


DIFF = ('--- a/src/auth/session.py\n+++ b/src/auth/session.py\n@@ -10,4 +10,6 @@\n-    if expired:\n'
        '-        return None\n+    if expired and refresh:\n+        return refresh_session(token)\n'
        '+    if expired:\n+        return None\n')


def _item(seq, kind, label, view, status='done', detail=None, duration=1.2):
    return {'seq': seq, 'at': round(seq / 64 * 0.8, 3), 'kind': kind, 'label': label, 'detail': detail,
            'duration': duration, 'status': status, **({'view': view} if view else {})}


def fixtures():
    """{name: example}: one or more items per view type, an unknown type, a header, a summary and every native kind
    NATIVE describes. Views come from work_view's builders, never written by hand."""
    tool = work_view.tool_view
    items = {
        'read': _item(64, 'tool', 'Reading src/auth/session.py', tool(_row(
            'Read', a_path='src/auth/session.py', r_path='src/auth/session.py', r_start=1, r_total=212), None)),
        'read_private': _item(128, 'tool', 'Reading .env', tool(_row('Read', a_path='.env', r_path='.env'), None)),
        'search': _item(192, 'tool', 'Searching project source', tool(_row(
            'Grep', a_pattern='refresh_session', r_hits=7), None)),
        'web': _item(256, 'tool', 'Searching the web', tool(_row(
            'WebSearch', a_query='session refresh token rotation', r_results=json.dumps(
                [{'url': 'https://www.rfc-editor.org/rfc/rfc6749'}, {'url': 'https://docs.python.org/3/library/'},
                 {'url': 'https://user:hunter2@example.org/notes'}])), None)),
        'edit': _item(320, 'tool', 'Preparing an edit to src/auth/session.py', tool(
            _row('Edit', a_path='src/auth/session.py', r_action='act_edit', r_status='applied'),
            _action('edit', p_path='src/auth/session.py', p_baseline='sha256:1f2e', p_diff=DIFF))),
        'edit_waiting': _item(384, 'tool', 'Drafting tests/test_session.py', tool(
            _row('Write', a_path='tests/test_session.py', r_action='act_write', r_status='awaiting_user_approval'),
            {**_action('edit', 'pending', p_path='tests/test_session.py', p_baseline=None,
                       p_diff='--- a/tests/test_session.py\n+++ b/tests/test_session.py\n@@ -0,0 +1,2 @@\n+import x\n'
                              '+def test_refresh(): ...\n'), 'id': 'act_write'})),
        'command': _item(448, 'tool', 'Reviewing terminal command', tool(
            _row('RunCommand', a_command='npm run build', r_action='act_command'),
            _action('command', p_command='npm run build', p_network=False, p_cwd='.', p_exit=0,
                    p_output='> app@1.0.0 build\n> vite build\n\nvite v7.1.9 building for production...\n'
                             'dist/app.js  182 kB\nDone in 4.2 s\n'), 10.0, 14.6), duration=4.6),
        'command_failed': _item(512, 'tool', 'Reviewing terminal command', tool(
            _row('RunCommand', a_command='python3 scripts/deploy.py --token=' + 'sk-live-' + '0123456789abcdefghijklmnop',
                 r_action='act_deploy'),
            {**_action('command', p_command='python3 scripts/deploy.py --token=' + 'sk-live-' + '0123456789abcdefghijklmnop',
                       p_network=False, p_cwd='scripts', p_exit=2,
                       p_output='Using API_KEY=abc123secret\nTraceback (most recent call last):\n'
                                'ValueError: no deploy target\n'), 'id': 'act_deploy'}, 3.0, 3.8),
                                status='failed', duration=0.8),
        'tests': _item(576, 'tool', 'Requesting isolated tests', tool(
            _row('RunTests', r_action='act_tests'),
            _action('tests', p_recipe='python-pytest', p_exit=1,
                    p_tests=json.dumps({'passed': 41, 'failed': 1, 'skipped': 0, 'errors': 0, 'total': 42}),
                    p_output='..........\nFAILED tests/test_session.py::test_refresh_expired_session - AssertionError\n'
                             '1 failed, 41 passed in 3.12s\n'), 20.0, 32.4), duration=12.4),
        'git_status': _item(640, 'tool', 'Reading Git status', tool(_row(
            'ReadGitStatus', r_branch='feat/session-refresh', r_upstream='origin/feat/session-refresh', r_ahead=3,
            r_behind=2, r_changed=4), None)),
        'git_commit': _item(704, 'tool', 'Preparing a reviewed Git plan', tool(
            _row('ProposeGitChange', a_op='commit', r_action='act_git'),
            _action('git', p_op='commit', p_summary="Commit 2 files to feat/session-refresh: 'Refresh sessions in "
                                                    "the middleware'", p_head='refs/heads/feat/session-refresh',
                    p_destructive=False, p_publication=False, p_commit='a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'))),
        'window': _item(768, 'tool', 'Pressed “=”', tool(_row(
            'ScreenAct', a_action='press', r_app='Calculator', r_title='Calculator', r_act='press', r_target='=',
            r_before='12*7', r_after='84', r_verdict='occurred', r_observation='4f1c2a9e0b7d6c5e3a21', r_width=320,
            r_height=480), None), detail='12*7 → 84'),
        'window_click': _item(832, 'tool', 'Clicked “Save”', tool(_row(
            'ScreenAct', a_action='click_at', a_x=212, a_y=388, r_app='TextEdit', r_title='Notes.txt',
            r_act='click_at', r_target='Save', r_verdict='not_observed', r_observation='9a8b7c6d5e4f3a2b1c0d',
            r_width=640, r_height=420), None), detail='Nothing changed.'),
        'agent': _item(899, 'child', 'Agent tests', work_view.agent_view(
            {'name': 'tests', 'depth': 1, 'parent_id': 'root', 'elapsed': 42.0, 'charged_tokens': 5200}, 'done'),
            duration=42.0),
        'checks': _item(960, 'check', 'Verified · pytest 42 passed on current files', work_view.checks_view(
            'Checked the reply', {'issues': 0, 'repairable': False},
            {'outcome': 'verified', 'tests': {'runner': 'pytest', 'passed': 42, 'failed': 0},
             'snapshot_current': True, 'files_changed': 2},
            [{'kind': 'tests_pass'}, {'kind': 'unchanged', 'scope': 'tests', 'paths': []},
             {'kind': 'output_equals', 'command': 'make report', 'path': 'samples/report.txt'}], 0, 1)),
        'checks_open': _item(1024, 'check', 'A check asked for more evidence', work_view.checks_view(
            'A check asked for more evidence', {'issues': 1, 'repairable': True}, None,
            [{'kind': 'tests_pass'}], 1, 0), detail='1 item to resolve'),
        'review': _item(1088, 'approval', 'Approved by Pebble (you asked: “run the tests”): Requesting isolated tests',
                        work_view.review_view({'id': 'act_tests', 'status': 'applied'}, 'tests', None, 'run the tests')),
        'review_waiting': _item(1152, 'approval', 'Waiting for your approval: Drafting tests/test_session.py',
                                work_view.review_view({'id': 'act_write', 'status': 'pending'}, 'edit', None, None),
                                status='running', duration=None),
        'question': _item(1216, 'tool', 'Preparing a clarification form', tool(_row(
            'AskUserQuestion', a_questions=1, r_status='answered'), None)),
        'unknown': _item(1280, 'tool', 'Planting a tree in the garden', {'v': 1, 'type': 'hologram', 'trees': 3},
                         detail='A view type this window does not know: it shows this row.'),
    }
    items = {name: {**item, **({'view': work_view.bounded(item['view'])} if item.get('view', {}).get('type')
                               in work_view.SPEC else {})} for name, item in items.items()}
    plan = {'steps': [{'title': 'Read the middleware', 'status': 'completed'},
                      {'title': 'Find the bug', 'status': 'completed'},
                      {'title': 'Fix the refresh', 'status': 'in_progress'},
                      {'title': 'Run the tests', 'status': 'pending'}, {'title': 'Commit', 'status': 'pending'}]}
    usage = {'input': 52310, 'cached': 28770, 'output': 3120, 'reasoning': 800, 'tokens_per_second': 61.2}
    native = {
        'form': {'fields': [{'id': 'scope', 'label': 'Which files should the fix cover?', 'type': 'choice',
                             'required': True, 'options': [{'id': 'auth', 'label': 'Only the auth folder'},
                                                           {'id': 'all', 'label': 'The whole project'}]}],
                 'submit_label': 'Continue'},
        'table': {'columns': [{'id': 'file', 'label': 'File'}, {'id': 'lines', 'label': 'Lines'}],
                  'rows': [{'file': 'session.py', 'lines': 212}, {'file': 'middleware.py', 'lines': 96}],
                  'caption': 'Files the fix touches'},
        'chart': {'type': 'bar', 'x_key': 'day', 'y_keys': ['runs'], 'labels': {'runs': 'Runs'},
                  'rows': [{'day': 'Mon', 'runs': 4}, {'day': 'Tue', 'runs': 7}, {'day': 'Wed', 'runs': 5}]},
        'plan': {'steps': [{'id': 'read', 'title': 'Read the middleware', 'status': 'in_progress', 'evidence_ids': []},
                           {'id': 'fix', 'title': 'Fix the refresh', 'status': 'pending', 'evidence_ids': []}]},
        'evidence': {'items': [{'id': 'e1', 'label': 'Tests passed', 'evidence_id': '3:call_tests',
                                'summary': '42 passed on current files'}]},
        'quiz': {'duration_minutes': 5, 'questions': [{'question': 'What is 2 + 2?', 'choices': ['3', '4'],
                                                       'answer': 1, 'explanation': '2 + 2 is 4.'}]},
        'diagram': {'nodes': [{'id': 'req', 'label': 'Request'}, {'id': 'mw', 'label': 'Auth middleware'},
                              {'id': 'app', 'label': 'App'}],
                    'edges': [{'from': 'req', 'to': 'mw', 'label': 'checks session'},
                              {'from': 'mw', 'to': 'app', 'label': 'passes on'}]},
        'dashboard': {'columns': 2, 'panels': [
            {'id': 'runs', 'title': 'Runs this week', 'kind': 'metric', 'data': {'value': 16, 'unit': 'runs',
                                                                                 'delta': 3}},
            {'id': 'files', 'title': 'Files', 'kind': 'table', 'data': {
                'columns': [{'id': 'name', 'label': 'Name'}], 'rows': [{'name': 'session.py'}]}}]},
        # Phase 46 (1.9.29, w25-genui-b): the visual catalog
        'compare': {'options': [{'id': 'sqlite', 'label': 'SQLite', 'summary': 'One file, no server'},
                                {'id': 'postgres', 'label': 'PostgreSQL', 'summary': 'A server, many writers'}],
                    'criteria': [{'label': 'Setup', 'values': ['None', 'A running server']},
                                 {'label': 'Writers at once', 'values': [1, 100]},
                                 {'label': 'Works offline', 'values': [True, True]}],
                    'recommended': 'sqlite', 'caption': 'For a desktop app with one person writing'},
        'metrics': {'tiles': [{'label': 'Tests passed', 'value': 41, 'delta': 3, 'better': 'up'},
                              {'label': 'Pass rate', 'value': 0.976, 'format': 'percent'},
                              {'label': 'Median time', 'value': 3.12, 'unit': 's', 'delta': -0.4, 'better': 'down'}]},
        'timeline': {'events': [{'date': '2026-10-01', 'title': 'Design agreed'},
                                {'date': '2026-10-20', 'title': 'First build', 'detail': 'Cards on the Mac'},
                                {'date': '2026-10-31', 'title': 'Release', 'tone': 'accent'}]},
        'record': {'fields': [{'label': 'Name', 'value': 'Asha Rao'}, {'label': 'Team', 'value': 'Platform'},
                              {'label': 'Desk', 'value': 412}], 'subtitle': 'New starter'},
        'prototype': {'viewport': 'phone', 'start': 'home', 'screens': [
            {'id': 'home', 'title': 'Home', 'components': [{'type': 'heading', 'text': 'Welcome'},
                                                           {'type': 'button', 'label': 'Settings', 'goto': 'settings'}]},
            {'id': 'settings', 'title': 'Settings', 'components': [{'type': 'toggle', 'label': 'Dark mode'}]}]},
    }
    return {'spec': TITLE, 'items': items,
            'header': work_view.header(plan, 'running', 72.4),
            'summary': work_view.summary(list(items.values()), usage, 95.2),
            'summary_items': list(items),  # the items the summary was computed from, in order
            'native': {kind: data for kind, data in native.items() if kind in kinds()},
            'native_variants': {name: {'kind': kind, 'data': data} for name, (kind, data) in VARIANTS.items()}}


# Phase 46 (1.9.29, w25-genui-b): one example of each chart type and diagram layout beyond `native`'s, for renderers.
VARIANTS = {
    'chart_area': ('chart', {'type': 'area', 'x_key': 'month', 'y_keys': ['users'], 'format': 'compact',
                             'rows': [{'month': '2026-07', 'users': 1200}, {'month': '2026-08', 'users': 2600},
                                      {'month': '2026-09', 'users': 4100}, {'month': '2026-10', 'users': 6900}]}),
    'chart_line': ('chart', {'type': 'line', 'x_key': 'day', 'y_keys': ['p50', 'p95'], 'unit': 'ms',
                             'labels': {'p50': 'Median', 'p95': 'Slowest 5%'},
                             'rows': [{'day': 'Mon', 'p50': 120, 'p95': 480}, {'day': 'Tue', 'p50': 110, 'p95': 390},
                                      {'day': 'Wed', 'p50': 135, 'p95': 520}]}),
    'chart_scatter': ('chart', {'type': 'scatter', 'x_key': 'size', 'y_keys': ['seconds'],
                                'labels': {'size': 'Files', 'seconds': 'Seconds'},
                                'rows': [{'size': 10, 'seconds': 1.2}, {'size': 40, 'seconds': 3.9},
                                         {'size': 80, 'seconds': 8.4}, {'size': 120, 'seconds': 11.1}]}),
    'chart_stacked': ('chart', {'type': 'stacked', 'x_key': 'week', 'y_keys': ['passed', 'failed'],
                                'labels': {'passed': 'Passed', 'failed': 'Failed'},
                                'rows': [{'week': 'W1', 'passed': 40, 'failed': 2}, {'week': 'W2', 'passed': 41, 'failed': 1},
                                         {'week': 'W3', 'passed': 42, 'failed': 0}]}),
    'chart_donut': ('chart', {'type': 'donut', 'x_key': 'part', 'y_keys': ['share'], 'format': 'percent',
                              'rows': [{'part': 'Model', 'share': 0.72}, {'part': 'Tools', 'share': 0.21},
                                       {'part': 'Pebble', 'share': 0.07}]}),
    'diagram_layers': ('diagram', {'layout': 'layers', 'caption': 'How a request reaches the model',
                                   'nodes': [{'id': 'ui', 'label': 'Window', 'group': 'Mac'},
                                             {'id': 'shell', 'label': 'Shell', 'group': 'Mac', 'sub': 'Checks each request'},
                                             {'id': 'run', 'label': 'Runner', 'group': 'Pebble'},
                                             {'id': 'guard', 'label': 'AgentGuard', 'group': 'Pebble', 'tone': 'ok'},
                                             {'id': 'model', 'label': 'Model', 'group': 'Provider'}],
                                   'edges': [{'from': 'ui', 'to': 'shell', 'label': ''},
                                             {'from': 'shell', 'to': 'run', 'label': 'allowed'},
                                             {'from': 'run', 'to': 'guard', 'label': 'checks'},
                                             {'from': 'run', 'to': 'model', 'label': 'asks'}]}),
    'diagram_sequence': ('diagram', {'layout': 'sequence',
                                     'nodes': [{'id': 'you', 'label': 'You'}, {'id': 'pebble', 'label': 'Pebble'},
                                               {'id': 'model', 'label': 'Model'}],
                                     'edges': [{'from': 'you', 'to': 'pebble', 'label': 'Ask'},
                                               {'from': 'pebble', 'to': 'model', 'label': 'Request'},
                                               {'from': 'model', 'to': 'pebble', 'label': 'Reply'},
                                               {'from': 'pebble', 'to': 'you', 'label': 'Answer'}]}),
    'diagram_flow': ('diagram', {'layout': 'flow', 'direction': 'right',
                                 'nodes': [{'id': 'a', 'label': 'Draft'}, {'id': 'b', 'label': 'Review'},
                                           {'id': 'c', 'label': 'Approved', 'tone': 'ok'},
                                           {'id': 'd', 'label': 'Changes asked', 'tone': 'warn'}],
                                 'edges': [{'from': 'a', 'to': 'b', 'label': ''}, {'from': 'b', 'to': 'c', 'label': 'yes'},
                                           {'from': 'b', 'to': 'd', 'label': 'no'}, {'from': 'd', 'to': 'a', 'label': ''}]}),
}
