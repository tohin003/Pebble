"""Phase 46 (1.9.29, D206, w24-genui-a): pebble-ui-spec v1 cannot drift from the code that builds what it describes.

The published files (docs/ui-spec/) are what scripts/generate_ui_spec.py writes now; every native kind artifact_schema
defines is in the schema; each native fixture passes both the schema and Pebble's own validator (artifacts.validate),
and the schema refuses what that validator refuses for the shapes it can express.
"""

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

from pebble import artifact_schema, artifacts, ui_spec, work_view

ROOT = Path(__file__).resolve().parents[2]


def generator():
    spec = importlib.util.spec_from_file_location('generate_ui_spec', ROOT / 'scripts' / 'generate_ui_spec.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native(kind):
    schema = ui_spec.schema()
    return jsonschema.Draft202012Validator({**schema['$defs']['native.' + kind], '$defs': schema['$defs']})


def test_the_published_files_are_current():
    assert generator().main(['--check']) == 0, 'run python scripts/generate_ui_spec.py and commit docs/ui-spec/'


def test_the_schema_is_valid_and_lists_every_kind_and_view_type():
    schema = ui_spec.schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    kinds = set(schema['x-pebble-native-kinds'])
    assert set(artifact_schema.LEGACY_KINDS) | set(artifact_schema.NEW_KINDS) | set(artifact_schema.NATIVE_KINDS) \
        | set(artifact_schema.SCHEMAS) <= kinds, 'a kind artifact_schema defines is missing from the spec'
    assert schema['x-pebble-view-types'] == list(work_view.SPEC)
    assert schema['$defs']['native.html']['x-pebble-surfaces'] == ['mac'], 'model-written HTML never reaches the phone'
    # A kind w25-genui-b (or anyone) adds to artifact_schema is published at once, with its summary; this names the
    # ones still waiting for their field schema in ui_spec.NATIVE, so a merge cannot leave one undescribed by accident.
    assert ui_spec.summary_only() == (), 'describe these in ui_spec.NATIVE: ' + ', '.join(ui_spec.summary_only())


def test_a_new_kind_in_artifact_schema_is_picked_up_without_an_edit(monkeypatch):
    # Restated in Phase 46 (1.9.29, w25-genui-b): 'timeline' is now a described catalog kind, so the stand-in for a
    # kind not yet described is 'gauge'; the check is the same.
    monkeypatch.setattr(artifact_schema, 'SCHEMAS', {**artifact_schema.SCHEMAS, 'gauge': 'gauge:{value,max}.'})
    assert 'gauge' in ui_spec.kinds() and ui_spec.summary_only() == ('gauge',)
    found = ui_spec.schema()['$defs']['native.gauge']
    assert found['x-pebble-shape'] == 'summary' and found['description'].startswith('gauge:{value')


@pytest.mark.parametrize('kind', sorted(set(ui_spec.NATIVE) - {'metric'}))
def test_each_native_fixture_passes_the_schema_and_pebbles_own_validator(kind):
    data = ui_spec.fixtures()['native'][kind]
    errors = [e.message for e in native(kind).iter_errors(data)]
    assert not errors, errors
    assert artifacts.validate(kind, json.loads(json.dumps(data))) is not None


@pytest.mark.parametrize('kind, data', [
    ('form', {'fields': []}),
    ('form', {'fields': [{'id': 'a', 'label': 'A', 'type': 'choice', 'required': True, 'options': [{'id': 'x', 'label': 'X'}]}]}),
    ('table', {'columns': [{'id': 'a', 'label': 'A'}] * 13, 'rows': []}),
    ('table', {'columns': [{'id': 'a', 'label': 'A', 'href': 'x'}], 'rows': []}),
    ('chart', {'type': 'pie', 'x_key': 'x', 'y_keys': ['y'], 'rows': [{'x': 1, 'y': 2}]}),
    ('plan', {'steps': [{'id': 'a', 'title': 'A', 'status': 'done'}]}),
    ('diagram', {'nodes': [], 'edges': []}),
    ('dashboard', {'panels': [{'id': 'p', 'title': 'P', 'kind': 'metric'}]}),
    ('prototype', {'start': 'a', 'screens': [{'id': 'a', 'title': 'A', 'components': [{'type': 'script', 'text': 'x'}]}]}),
    ('quiz', {'duration_minutes': 0, 'questions': []}),
])
def test_the_schema_refuses_what_pebble_refuses(kind, data):
    assert list(native(kind).iter_errors(data)), 'the schema accepted it'
    with pytest.raises(ValueError):
        artifacts.validate(kind, data)


def test_the_fixtures_file_holds_every_view_type_and_an_unknown_one():
    fixtures = json.loads((ROOT / 'docs' / 'ui-spec' / 'fixtures.json').read_text(encoding='utf-8'))
    types = {item['view']['type'] for item in fixtures['items'].values() if 'view' in item}
    assert set(work_view.SPEC) < types and types - set(work_view.SPEC) == {'hologram'}
    # the file's keys are sorted; summary_items keeps the timeline's order, in which the summary was computed
    assert sorted(fixtures['summary_items']) == sorted(fixtures['items'])
    seqs = [fixtures['items'][name]['seq'] for name in fixtures['summary_items']]
    assert seqs == sorted(seqs)
