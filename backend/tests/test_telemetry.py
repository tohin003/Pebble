"""Phase 18 E06: opt-in, local-only, bounded and redacted telemetry (backend/pebble/telemetry.py).

Off by default and silent on disk; the consent version gates collection; the
schema is a closed allowlist with no free text; bounds and retention hold;
exports go only to a single-use native destination; no socket is ever opened;
and canaries planted in a real App run (prompt, file, memory, vault-like value,
URL, model output) never reach the queue or the export.
"""

import ast
import json
import math
import os
import re
import socket
import stat
import threading
from pathlib import Path

import pytest

from pebble import telemetry
from pebble.telemetry import CONSENT_VERSION, FIELDS, SCHEMA, Refused, Telemetry, validate_event

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0
CANARIES = ('canary-prompt-7f3a91', 'canary-file-c41d07', 'canary-memory-9b2e55', 'canary-output-e81f20',
            'canary-host.invalid', 'sk-live-canary0000000000000000000000', 'canary-tool-5d11')


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def home(tmp_path):
    folder = tmp_path / 'home'
    folder.mkdir()
    return folder


def record(**extra):
    base = {'state': 'completed', 'steps': 4, 'tools': ['Read', 'Read', 'Edit'], 'approvals': 1, 'rejections': 0,
            'questions': 0, 'uncertain': 0, 'wall_seconds': 2.5, 'model_seconds': 1.0, 'input_tokens': 1000,
            'output_tokens': 40}
    return {**base, **extra}


def files_text(folder):
    return b''.join(p.read_bytes() for p in Path(folder).rglob('*') if p.is_file())


def event(kind='error', **data):
    return {'schema': SCHEMA, 'kind': kind, 'hour': int(NOW // 3600), 'data': data or {'code': 'internal'}}


def test_schema_constants_are_fixed_and_match_the_host():
    from pebble.runtime import STATES
    assert SCHEMA == 'pebble.telemetry/1' and CONSENT_VERSION == 'pebble.telemetry.consent/1'
    assert telemetry.RUN_STATES == tuple(sorted(STATES))
    assert set(FIELDS) == {'run_settled', 'resource_sample', 'error'}
    assert telemetry.DESTINATION is None
    assert telemetry.ROUTES == ('telemetry_get', 'telemetry_set', 'telemetry_export', 'telemetry_clear')
    assert (telemetry.MAX_EVENTS, telemetry.MAX_BYTES, telemetry.RETENTION_SECONDS) == (1000, 256 * 1024, 30 * 86400)
    # Every field is typed and bounded: no string field other than closed enums.
    for kind, fields in FIELDS.items():
        for name, spec in fields.items():
            assert spec['type'] in {'enum', 'count', 'log2', 'tool_counts'}, (kind, name)
    assert telemetry.CONSENT_SHA256 == __import__('hashlib').sha256(telemetry.CONSENT_TEXT.encode()).hexdigest()
    for promise in ('never', 'messages', 'files', 'memory notes', 'web addresses', 'credentials', 'Nothing is sent',
                    '1,000 events', '256 KB', '30 days', 'deletes the log'):
        assert promise in telemetry.CONSENT_TEXT, promise


def test_off_by_default_writes_nothing_and_collects_nothing(home):
    t = Telemetry(home, clock=Clock())
    status = t.status()
    assert status['enabled'] is False and status['events'] == 0 and status['consent_version'] is None
    assert status['destination'] is None and status['network_upload'] is False and status['install_id'] is None
    assert status['current_consent_version'] == CONSENT_VERSION and status['export'] == 'native_save_dialog'
    assert t.observe_run(record()) is False
    assert t.observe_resource(100 * 2 ** 20, 3.0) is False and t.observe_error('internal') is False
    with pytest.raises(ValueError):
        t.export('a' * 32)
    assert t.disable()['enabled'] is False and t.clear()['enabled'] is False
    assert list(home.iterdir()) == [], 'default telemetry writes zero bytes'


def test_a_deferred_record_is_built_only_when_telemetry_is_on(home):
    t, built = Telemetry(home, clock=Clock()), []

    def build():
        built.append(1)
        return record()
    assert t.observe_run(build) is False and built == [] and list(home.iterdir()) == []
    t.enable(CONSENT_VERSION)
    assert t.observe_run(build) is True and built == [1] and t.status()['events'] == 1
    with pytest.raises(RuntimeError):
        t.observe_run(lambda: (_ for _ in ()).throw(RuntimeError('database unavailable')))
    assert t.status()['events'] == 1 and t.observe_run(lambda: {'prompt': 'x'}) is False


def test_enable_needs_the_current_consent_version_and_records_it(home):
    clock = Clock()
    t = Telemetry(home, clock=clock)
    for stale in (None, '', 'pebble.telemetry.consent/0', 'pebble.telemetry.consent/2', 1, True):
        with pytest.raises(ValueError):
            t.enable(stale)
    assert not (home / 'telemetry').exists()
    status = t.enable(CONSENT_VERSION)
    assert status['enabled'] is True and status['consent_version'] == CONSENT_VERSION
    assert status['consented_at'] == int(NOW) and re.fullmatch(r'[0-9a-f]{32}', status['install_id'])
    folder = home / 'telemetry'
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert {p.name for p in folder.iterdir()} == {'queue.jsonl', 'state.json'}
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in folder.iterdir())
    clock.value += 100
    again = t.enable(CONSENT_VERSION)
    assert again['install_id'] == status['install_id'] and again['consented_at'] == int(NOW)


def test_stale_or_damaged_consent_stops_collection(home):
    t = Telemetry(home, clock=Clock())
    t.enable(CONSENT_VERSION)
    state_path = home / 'telemetry' / 'state.json'
    state = json.loads(state_path.read_text())
    for damaged in ({**state, 'consent_version': 'pebble.telemetry.consent/0'}, {**state, 'consent_sha256': '0' * 64},
                    {**state, 'install_id': 'not-hex'}, {**state, 'dropped': -1}, ['list']):
        state_path.write_text(json.dumps(damaged))
        assert t.status()['enabled'] is False and t.observe_run(record()) is False
    state_path.write_text('{not json')
    assert t.status()['enabled'] is False
    fresh = t.enable(CONSENT_VERSION)
    assert fresh['enabled'] is True and fresh['install_id'] != state['install_id'] and fresh['events'] == 0


def test_observe_run_buckets_counts_and_collapses_unlisted_tools(home):
    t = Telemetry(home, clock=Clock())
    t.enable(CONSENT_VERSION)
    assert t.observe_run(record(tools=['Read', 'Read', 'Edit', 'Exfiltrate canary-tool-5d11', 7, None])) is True
    line = (home / 'telemetry' / 'queue.jsonl').read_bytes()
    stored = json.loads(line)
    assert stored == validate_event(stored)
    assert stored['kind'] == 'run_settled' and stored['hour'] == int(NOW // 3600)
    data = stored['data']
    assert data['tools'] == {'Edit': 1, 'Read': 2, 'unlisted': 3}
    assert data['wall_ms_log2'] == math.floor(math.log2(2500)) + 1 == 12
    assert data['model_ms_log2'] == 10 and data['input_tokens_log2'] == 10 and data['output_tokens_log2'] == 6
    assert (data['state'], data['steps'], data['approvals'], data['questions']) == ('completed', 4, 1, 0)
    assert b'canary' not in line and b'Exfiltrate' not in line
    assert t.observe_run(record(tools={'Read': 2, 'Write': 0}, model_seconds=None, input_tokens=None)) is True
    second = json.loads((home / 'telemetry' / 'queue.jsonl').read_bytes().splitlines()[1])
    assert second['data']['tools'] == {'Read': 2} and second['data']['model_ms_log2'] is None
    assert t.observe_resource(300 * 2 ** 20, 3.7) is True and t.observe_error('compaction_stalled') is True
    status = t.status()
    assert status['events'] == 4 and status['kinds'] == {'run_settled': 2, 'resource_sample': 1, 'error': 1}
    resource = json.loads((home / 'telemetry' / 'queue.jsonl').read_bytes().splitlines()[2])
    assert resource['data'] == {'cpu_percent_bucket': 2, 'footprint_mb_log2': 9}


@pytest.mark.parametrize('bad', [
    {'prompt': 'canary-prompt-7f3a91'}, {'url': 'https://canary-host.invalid/'}, {'state': 'completed: canary'},
    {'state': 'Completed'}, {'steps': -1}, {'steps': 1.5}, {'steps': True}, {'steps': '4'},
    {'tools': 'Read'}, {'tools': {'Read': -2}}, {'tools': {'Read': True}}, {'wall_seconds': -1},
    {'wall_seconds': float('nan')}, {'wall_seconds': float('inf')}, {'wall_seconds': '2'},
    {'input_tokens': 1.5}, {'input_tokens': -3}, {'output_tokens': 'many'}])
def test_bad_records_are_refused_whole_and_counted(home, bad):
    t = Telemetry(home, clock=Clock())
    t.enable(CONSENT_VERSION)
    assert t.observe_run(record(**bad)) is False
    assert t.status()['refused'] == 1 and t.status()['events'] == 0
    assert b'canary' not in files_text(home / 'telemetry')
    assert t.observe_run('not a record') is False and t.observe_run({'steps': 1}) is False
    assert t.observe_error('free text canary-output-e81f20') is False
    assert t.observe_resource(-1, 3) is False and t.observe_resource(1, -3) is False
    assert t.status()['refused'] == 6 and b'canary' not in files_text(home / 'telemetry')


BAD_EVENTS = [
    ('extra envelope key', {**event(), 'install_id': '0' * 32}),
    ('missing envelope key', {k: v for k, v in event().items() if k != 'hour'}),
    ('foreign schema', {**event(), 'schema': 'pebble.telemetry/2'}),
    ('unknown kind', event(kind='prompt', code='internal')),
    ('negative hour', {**event(), 'hour': -1}),
    ('float hour', {**event(), 'hour': 1.0}),
    ('free-text error', event(code='internal: canary-output-e81f20')),
    ('extra data key', event(code='internal', url='https://canary-host.invalid')),
    ('not a dict', ['canary']),
    ('run state outside the enum', event('run_settled', **{**dict.fromkeys(FIELDS['run_settled'], 0),
                                                           'state': 'canary', 'tools': {}})),
    ('tool outside the catalogue', event('run_settled', **{**dict.fromkeys(FIELDS['run_settled'], 0),
                                                           'state': 'completed', 'tools': {'canary-tool-5d11': 1}})),
    ('zero tool count', event('run_settled', **{**dict.fromkeys(FIELDS['run_settled'], 0),
                                                'state': 'completed', 'tools': {'Read': 0}})),
    ('bool count', event('run_settled', **{**dict.fromkeys(FIELDS['run_settled'], 0),
                                           'state': 'completed', 'tools': {}, 'steps': True})),
    ('count above the bound', event('run_settled', **{**dict.fromkeys(FIELDS['run_settled'], 0),
                                                      'state': 'completed', 'tools': {}, 'steps': 10_001})),
    ('log2 above the bound', event('resource_sample', footprint_mb_log2=49, cpu_percent_bucket=0)),
    ('cpu bucket outside the set', event('resource_sample', footprint_mb_log2=3, cpu_percent_bucket=3)),
    ('bool cpu bucket', event('resource_sample', footprint_mb_log2=3, cpu_percent_bucket=True)),
    ('float cpu bucket', event('resource_sample', footprint_mb_log2=3, cpu_percent_bucket=1.0)),
]


@pytest.mark.parametrize('label,value', BAD_EVENTS, ids=[label for label, _ in BAD_EVENTS])
def test_validate_event_refuses_anything_outside_the_allowlist(label, value):
    with pytest.raises(Refused) as refused:
        validate_event(value)
    assert 'canary' not in str(refused.value) and re.fullmatch(r'[a-z0-9_]+', str(refused.value))


def test_validate_event_accepts_and_canonicalises_each_kind():
    run = event('run_settled', **{**dict.fromkeys(FIELDS['run_settled'], None), 'state': 'needs_reconciliation',
                                  'steps': 3, 'tools': {'Read': 1, 'Edit': 2}, 'approvals': 0, 'rejections': 1,
                                  'questions': 0, 'uncertain': 1})
    assert list(validate_event(run)['data']['tools']) == ['Edit', 'Read']
    assert validate_event(event('resource_sample', footprint_mb_log2=None, cpu_percent_bucket=800))
    assert validate_event(event('error', code='provider_auth'))['data'] == {'code': 'provider_auth'}
    assert telemetry.log2_bucket(0.5) == 0 and telemetry.log2_bucket(1) == 1 and telemetry.log2_bucket(2 ** 80) == 48
    assert telemetry.cpu_bucket(0.99) == 0 and telemetry.cpu_bucket(1) == 1 and telemetry.cpu_bucket(5000) == 800


def test_bounds_drop_the_oldest_events_and_count_them(home, monkeypatch):
    t = Telemetry(home, clock=Clock())
    t.enable(CONSENT_VERSION)
    monkeypatch.setattr(telemetry, 'MAX_EVENTS', 5)
    for index in range(8):
        assert t.observe_run(record(steps=index)) is True
    status = t.status()
    assert status['events'] == 5 and status['dropped'] == 3
    steps = [json.loads(line)['data']['steps'] for line in (home / 'telemetry' / 'queue.jsonl').read_bytes().splitlines()]
    assert steps == [3, 4, 5, 6, 7]
    monkeypatch.setattr(telemetry, 'MAX_EVENTS', 1000)
    monkeypatch.setattr(telemetry, 'MAX_BYTES', 900)
    assert t.observe_run(record(steps=99)) is True
    assert (home / 'telemetry' / 'queue.jsonl').stat().st_size <= 900 and t.status()['dropped'] > 3


def test_default_bounds_hold_for_a_full_queue(home):
    t = Telemetry(home, clock=Clock())
    t.enable(CONSENT_VERSION)
    line = telemetry.encode(validate_event(event('run_settled', **{
        **dict.fromkeys(FIELDS['run_settled'], 3), 'state': 'completed',
        'tools': {name: 9 for name in sorted(telemetry.tool_names())[:6]}}))) + b'\n'
    (home / 'telemetry' / 'queue.jsonl').write_bytes(line * 1200)          # generated, never a fixture file
    assert t.observe_error('internal') is True
    data = (home / 'telemetry' / 'queue.jsonl').read_bytes()
    assert len(data.splitlines()) <= telemetry.MAX_EVENTS and len(data) <= telemetry.MAX_BYTES
    assert t.status()['dropped'] >= 201 and json.loads(data.splitlines()[-1])['kind'] == 'error'


def test_events_older_than_thirty_days_are_deleted(home):
    clock = Clock()
    t = Telemetry(home, clock=clock)
    t.enable(CONSENT_VERSION)
    t.observe_run(record())
    clock.value += 29 * 86400
    assert t.status()['events'] == 1
    clock.value += 2 * 86400
    assert t.status()['events'] == 0
    t.observe_error('internal')
    lines = (home / 'telemetry' / 'queue.jsonl').read_bytes().splitlines()
    assert len(lines) == 1 and t.status()['expired'] == 1


def test_tampered_queue_lines_are_dropped_never_exported(home, tmp_path):
    t = Telemetry(home, clock=Clock())
    t.enable(CONSENT_VERSION)
    t.observe_run(record())
    queue = home / 'telemetry' / 'queue.jsonl'
    planted = json.dumps({**event(), 'data': {'code': 'internal', 'note': 'canary-file-c41d07'}}).encode()
    queue.write_bytes(queue.read_bytes() + planted + b'\n{broken\n' + b'[' * 5000 + b'\n')
    status = t.status()
    assert status['events'] == 1 and status['refused'] == 3
    target = tmp_path / 'export.json'
    t.claim_destination = lambda token: target
    t.export('t' * 32)
    assert b'canary' not in target.read_bytes() and json.loads(target.read_bytes())['counts']['events'] == 1
    queue.write_bytes(b'x' * (2 * telemetry.MAX_BYTES + 10))
    assert t.status()['events'] == 0


def test_a_linked_shared_or_replaced_folder_is_refused(home, tmp_path):
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (home / 'telemetry').symlink_to(elsewhere)
    t = Telemetry(home, clock=Clock())
    with pytest.raises(ValueError):
        t.enable(CONSENT_VERSION)
    assert t.observe_run(record()) is False and list(elsewhere.iterdir()) == []
    (home / 'telemetry').unlink()
    (home / 'telemetry').mkdir(mode=0o755)
    os.chmod(home / 'telemetry', 0o755)
    with pytest.raises(ValueError):
        t.enable(CONSENT_VERSION)
    os.chmod(home / 'telemetry', 0o700)
    t.enable(CONSENT_VERSION)
    queue = home / 'telemetry' / 'queue.jsonl'
    queue.unlink()
    queue.symlink_to(elsewhere / 'target')
    with pytest.raises(ValueError):
        t.status()
    assert t.observe_run(record()) is False and not (elsewhere / 'target').exists()


def test_clear_rotates_the_install_id_and_disable_deletes_everything(home):
    t = Telemetry(home, clock=Clock())
    first = t.enable(CONSENT_VERSION)['install_id']
    t.observe_run(record())
    cleared = t.clear()
    assert cleared['enabled'] is True and cleared['events'] == 0 and cleared['install_id'] != first
    t.observe_run(record())
    off = t.disable()
    assert off['enabled'] is False and off['events'] == 0 and not (home / 'telemetry').exists()
    assert t.observe_run(record()) is False and list(home.iterdir()) == []


def test_export_uses_only_a_single_use_native_destination(home, tmp_path):
    t = Telemetry(home, clock=Clock())
    t.enable(CONSENT_VERSION)
    t.observe_run(record())
    claimed, tokens = [], {'tok-' + 'a' * 28: tmp_path / 'saved.json', 'tok-' + 'b' * 28: tmp_path / 'saved.json',
                           'tok-' + 'c' * 28: tmp_path / 'link-dir' / 'x.json', 'tok-' + 'd' * 28: Path('relative.json')}

    def claim(token):
        claimed.append(token)
        if token not in tokens:
            raise ValueError('Unknown or used destination.')
        return tokens.pop(token)
    with pytest.raises(ValueError, match='Save dialog'):
        t.export('tok-' + 'a' * 28)                                   # not wired: refused
    t.claim_destination = claim
    for bad in ('/tmp/x.json', '../escape', 'short', 'tok-a' + '/' * 20, None, 7):
        with pytest.raises(ValueError):
            t.export(bad)
    assert claimed == [], 'a malformed token never reaches the native resolver'
    receipt = t.export('tok-' + 'a' * 28)
    saved = tmp_path / 'saved.json'
    assert receipt['verified'] is True and receipt['published'] is False and receipt['network_upload'] is False
    assert receipt['path'] == str(saved) and stat.S_IMODE(saved.stat().st_mode) == 0o600
    document = json.loads(saved.read_bytes())
    assert document['schema'] == 'pebble.telemetry_export/1' and document['telemetry_schema'] == SCHEMA
    assert document['consent_version'] == CONSENT_VERSION and document['destination'] is None
    assert document['counts']['events'] == 1 and all(validate_event(e) == e for e in document['events'])
    assert receipt['sha256'] == __import__('hashlib').sha256(saved.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        t.export('tok-' + 'a' * 28)                                   # single use
    before = saved.read_bytes()
    with pytest.raises(ValueError, match='already exists'):
        t.export('tok-' + 'b' * 28)                                   # never overwrites
    assert saved.read_bytes() == before
    (tmp_path / 'real-dir').mkdir()
    (tmp_path / 'link-dir').symlink_to(tmp_path / 'real-dir')
    with pytest.raises(ValueError):
        t.export('tok-' + 'c' * 28)
    with pytest.raises(ValueError):
        t.export('tok-' + 'd' * 28)
    assert list((tmp_path / 'real-dir').iterdir()) == []
    t.disable()
    claimed.clear()
    with pytest.raises(ValueError, match='off'):
        t.export('tok-' + 'e' * 28)
    assert claimed == [], 'an export while off never claims the token'


def test_the_module_never_opens_a_socket(home, tmp_path, monkeypatch):
    opened = []

    def refuse(*args, **kwargs):
        opened.append(args)
        raise AssertionError('telemetry opened a network socket')
    for name in ('socket', 'create_connection', 'getaddrinfo', 'socketpair', 'fromfd'):
        monkeypatch.setattr(socket, name, refuse)
    monkeypatch.setattr(telemetry, '_tool_names', None)
    t = Telemetry(home, clock=Clock())
    t.status()
    t.enable(CONSENT_VERSION)
    t.observe_run(record())
    t.observe_resource(64 * 2 ** 20, 0.5)
    t.observe_error('internal')
    t.claim_destination = lambda token: tmp_path / 'out.json'
    t.export('z' * 32)
    t.clear()
    t.disable()
    assert opened == []
    tree = ast.parse((ROOT / 'backend/pebble/telemetry.py').read_text())
    imported = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names} | {node.module.split('.')[0] for node in ast.walk(tree)
                                            if isinstance(node, ast.ImportFrom) and node.module}
    assert not imported & {'socket', 'ssl', 'http', 'urllib', 'requests', 'httpx', 'asyncio', 'subprocess'}


def test_concurrent_observations_are_serialised(home):
    t = Telemetry(home, clock=Clock())
    t.enable(CONSENT_VERSION)
    results = []

    def worker():
        results.extend(t.observe_run(record()) for _ in range(10))
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [True] * 40 and t.status()['events'] == 40 and t.status()['refused'] == 0


def test_routes_are_never_model_tools_nor_reachable_from_the_pet():
    from pebble.tool_catalogue import TOOLS
    names = {tool['function']['name'].lower() for tool in TOOLS}
    assert not names & set(telemetry.ROUTES)
    source = (ROOT / 'src-tauri/src/main.rs').read_text()
    pet = re.search(r'window\.label\(\)=="pet" && !\[([^\]]*)\]', source).group(1)
    assert not set(re.findall(r'"([a-z_]+)"', pet)) & set(telemetry.ROUTES)
    from pebble import tool_authz
    if set(telemetry.ROUTES) <= tool_authz.NATIVE_ONLY:   # after the parent's integration patch
        allowed = re.search(r'const ALLOWED: &\[&str\] = &\[([^\]]*)\]', source).group(1)
        assert set(telemetry.ROUTES) <= set(re.findall(r'"([a-z_]+)"', allowed))


def test_backups_and_the_memory_archive_never_carry_telemetry(home):
    from pebble import migrations
    assert migrations.DATABASE == 'pebble.sqlite' and migrations.SCOPE.startswith('pebble.sqlite only')
    t = Telemetry(home, clock=Clock())
    t.enable(CONSENT_VERSION)
    t.observe_run(record())
    assert not (home / 'pebble.sqlite').exists(), 'telemetry never writes the database'
    archive = (ROOT / 'backend/pebble/memory_portable.py').read_text()
    assert 'telemetry' not in archive


def test_canaries_from_a_real_app_run_never_reach_the_queue_or_export(tmp_path):
    """A real App run whose prompt, file, memory, vault-like value, URL and output carry canaries."""
    from pebble.app import App
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        project = tmp_path / 'project'
        project.mkdir()
        (project / 'notes.md').write_text('Project note canary-file-c41d07; key sk-live-canary0000000000000000000000\n')
        app.memory_save('semantic', 'Remember canary-memory-9b2e55 for the fixture.')
        chat = app.new_chat(str(project))
        calls = []

        def complete(messages, tools, cancel, token, effort):
            calls.append(len(messages))
            if len(calls) == 1:
                return {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'read-1', 'type': 'function',
                    'function': {'name': 'Read', 'arguments': json.dumps({'file_path': 'notes.md'})}}]}
            token('Summary canary-output-e81f20 from https://canary-host.invalid/path?key=1')
            return {'role': 'assistant', 'content': 'Summary canary-output-e81f20 from https://canary-host.invalid/path'}
        app.model.complete = complete
        app.work(chat, 'Summarise notes.md canary-prompt-7f3a91 https://canary-host.invalid/q', False, [])
        rid = app.runs.list(chat['id'])[0]['id']
        assert app.runs.get(rid)['state'] not in ('queued', 'running')
        made = telemetry.run_record(app, rid, usage={'input_tokens': 1234, 'output_tokens': 56,
                                                     'generation_seconds': 0.8}, wall_seconds=1.25)
        assert set(made) == telemetry.RECORD_KEYS and made['tools'] == ['Read']
        assert made['input_tokens'] == 1234 and made['model_seconds'] == 0.8 and made['wall_seconds'] == 1.25
        t = Telemetry(tmp_path / 'data', clock=Clock())
        t.enable(CONSENT_VERSION)
        assert t.observe_run(made) is True
        assert t.observe_run({**made, 'tools': made['tools'] + ['canary-tool-5d11']}) is True
        target = tmp_path / 'telemetry-export.json'
        t.claim_destination = lambda token: target
        t.export('x' * 32)
        stored = files_text(tmp_path / 'data' / 'telemetry') + target.read_bytes()
        for canary in CANARIES:
            assert canary.encode() not in stored, canary
        events = [json.loads(line) for line in (tmp_path / 'data' / 'telemetry' / 'queue.jsonl').read_bytes().splitlines()]
        assert [e['data']['tools'] for e in events] == [{'Read': 1}, {'Read': 1, 'unlisted': 1}]
        assert events[0]['data']['state'] == app.runs.get(rid)['state']
    finally:
        app.active = app.active_run = None
        app.close()


def test_nearest_rank_agrees_with_the_statistics_module_when_present():
    stats = pytest.importorskip('pebble.eval_stats')
    import importlib.util
    spec = importlib.util.spec_from_file_location('measure_resources_for_stats', ROOT / 'scripts/measure_resources.py')
    measure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(measure)
    for values in ([5], [3, 1, 2], list(range(1, 21)), [0.1 * i for i in range(97)]):
        for share in (0.05, 0.5, 0.95, 1.0):
            assert measure.nearest_rank(values, share) == stats.nearest_rank(values, share)
