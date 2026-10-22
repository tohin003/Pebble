"""Phase 19 B1: every catalogue tool has a host activity class, and the browser's fallback table matches."""

import re
from pathlib import Path

import pytest

from pebble import activity_kinds
from pebble.activity_kinds import ACTIVITY, ACTIVITY_KEYS, HOST, HOST_KEYS, KEYS, QUIET, classify
from pebble.runtime import LIVE, STATES
from pebble.tool_catalogue import TOOLS

ROOT = Path(__file__).resolve().parents[2]
STATUS_WORDS_TS = (ROOT / 'src' / 'statusWords.ts').read_text(encoding='utf-8')
NAMES = [t['function']['name'] for t in TOOLS]


def ts_block(marker):
    match = re.search(r'// %s:begin\n(.*?)// %s:end' % (marker, marker), STATUS_WORDS_TS, re.S)
    assert match, marker
    return match.group(1)


def ts_array(name):
    match = re.search(r'export const %s = \[(.*?)\] as const;' % name, STATUS_WORDS_TS, re.S)
    assert match, name
    return tuple(re.findall(r'"([a-z_]+)"', match.group(1)))


def test_every_catalogue_tool_is_classified_and_no_class_names_a_missing_tool():
    assert len(NAMES) == len(set(NAMES)), 'tool names are unique'
    unclassified = sorted(set(NAMES) - set(ACTIVITY))
    assert unclassified == [], 'classify these tools in activity_kinds.py and src/statusWords.ts: %s' % unclassified
    stale = sorted(set(ACTIVITY) - set(NAMES))
    assert stale == [], 'these classified names are not catalogue tools: %s' % stale
    for name in NAMES:
        assert classify(name, 'working') in ACTIVITY_KEYS, name


def test_classes_and_keys_are_disjoint_and_closed():
    assert set(ACTIVITY.values()) <= set(ACTIVITY_KEYS)
    assert not set(ACTIVITY_KEYS) & set(HOST_KEYS)
    assert KEYS == ACTIVITY_KEYS + HOST_KEYS and len(set(KEYS)) == len(KEYS)
    # Every class that the table can produce is used by at least one tool or by classify itself.
    produced = set(ACTIVITY.values()) | {'thinking', 'working'}
    assert produced <= set(ACTIVITY_KEYS)
    # writing and loading come from tokens and the model state, never from a tool.
    assert not {'writing', 'loading'} & set(ACTIVITY.values())


@pytest.mark.parametrize('tool,expected', [
    ('Read', 'reading'), ('ReadAttachment', 'reading'), ('ReadURL', 'reading'), ('ReadProcess', 'reading'),
    ('LoadSkill', 'reading'), ('ReadPluginData', 'reading'), ('Glob', 'searching'), ('Grep', 'searching'),
    ('SearchFiles', 'searching'), ('WebSearch', 'searching'), ('Weather', 'searching'), ('Write', 'editing'),
    ('Append', 'editing'), ('Edit', 'editing'), ('IntegrateAgentPatch', 'editing'), ('IntegrateWorktreeFile', 'editing'),
    ('RunTests', 'testing'), ('RunRecipe', 'testing'), ('RunCommand', 'running'), ('StartCommand', 'running'),
    ('WaitProcess', 'running'), ('RunPlan', 'running'), ('SpawnAgent', 'delegating'), ('WaitForAgents', 'delegating'),
    ('UIRender', 'viewing'), ('UIUpdate', 'viewing'), ('RenderMod', 'viewing'), ('UpdatePlan', 'thinking'),
    ('OpenApp', 'working'), ('PluginCall', 'working'),
])
def test_design_study_classes(tool, expected):
    assert classify(tool, 'working') == expected


def test_classify_is_total_and_never_borrows_a_meaning():
    assert classify(None, 'working') == 'thinking', 'a working event without a tool is the model turn'
    assert classify('', 'working') == 'thinking'
    for odd in (7, ['Read'], {'Read': 1}, b'Read', 3.5, True):
        assert classify(odd, 'working') == 'thinking'
    # An invented or plugin-provided name is plain "working": the name itself is never shown.
    for name in ('Approve', 'approve', 'read', 'READ', '__proto__', 'constructor', 'toString', '../../etc/passwd',
                 '<img src=x onerror=alert(1)>', 'Read ', 'x' * 10_000):
        assert classify(name, 'working') == 'working', name
    # Only working events describe current activity; done, blocked and review events do not.
    for kind in ('done', 'blocked', 'review', '', None, 'Working', 'working '):
        for tool in ('Read', None, 'Approve'):
            assert classify(tool, kind) is None, (tool, kind)


def test_host_states_cover_every_run_state():
    assert set(HOST) | QUIET == STATES, 'classify a new run state in HOST or QUIET (and RUN_STATUS in statusWords.ts)'
    assert not set(HOST) & QUIET
    assert set(HOST.values()) <= set(HOST_KEYS)
    assert LIVE <= QUIET | set(HOST)
    assert HOST['retry_wait'] == 'reconnecting' and HOST['needs_reconciliation'] == 'uncertain'
    assert {HOST['waiting_input'], HOST['waiting_approval']} == {'question', 'review'}


def test_browser_fallback_table_matches_the_host_table():
    block = ts_block('activity-kinds')
    browser = {}
    for key, names in re.findall(r'^\s*([a-z]+): \[(.*)\],$', block, re.M):
        for name in re.findall(r'"([A-Za-z]+)"', names):
            assert name not in browser, 'one class per tool: ' + name
            browser[name] = key
    assert browser == ACTIVITY
    run_status = dict(re.findall(r'^\s*([a-z_]+): "([a-z]+)",$', ts_block('run-status'), re.M))
    assert run_status == HOST
    assert ts_array('ACTIVITY_KEYS') == ACTIVITY_KEYS
    assert ts_array('HOST_KEYS') == HOST_KEYS
    words = re.search(r'export const STATUS_WORDS[^=]*= table\(\{(.*?)\}\)', STATUS_WORDS_TS, re.S).group(1)
    assert tuple(re.findall(r'^\s*([a-z]+): \[', words, re.M)) == KEYS


def test_progress_payload_shape_for_parent_patch_p1():
    # The runner adds exactly one field to every progress event (P1), None when it is not an
    # activity; labels are never an input.
    def progress(label, kind='working', **extra):
        return {'chat_id': 'c', 'run_id': 'r', 'label': label, 'kind': kind, **extra,
                'activity': classify(extra.get('tool'), kind)}
    assert progress('Reading /private/secret.txt', tool='Read')['activity'] == 'reading'
    assert progress('Reading /private/secret.txt', 'done', tool='Read')['activity'] is None
    assert progress('Working with your model')['activity'] == 'thinking'
    assert progress('Reconnecting to the selected model', 'working', attempt=2)['activity'] == 'thinking'
    assert progress('Tool argument is invalid.', 'blocked', tool='Approve')['activity'] is None
    assert all(progress('x', tool=n)['activity'] in ACTIVITY_KEYS for n in NAMES)
    # An extra field named activity cannot override the host's class.
    assert progress('x', tool='Read', activity='review')['activity'] == 'reading'


def test_module_is_pure_data():
    source = Path(activity_kinds.__file__).read_text(encoding='utf-8')
    assert not re.search(r'^\s*(import|from)\s', source, re.M), 'activity_kinds imports nothing'
    assert 'open(' not in source and 'emit' not in source
