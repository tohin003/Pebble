"""Phase 20 T5: scripts/probe_installed_app.py on owned temporary bundles and homes only.

Every bundle here is a fake .app tree under pytest's tmp_path; the epoch-9 home is built by the repository's
own migration code (migrations.prepare, REFERENCE_DDL, Upgrade.complete) without starting App, so no
Keychain, provider or worker is touched. Process tables, ps CPU times, clocks and sleeps are injected. No
app is launched or signalled, nothing under /Applications or ~/Library is read, and no network is used.
"""

import hashlib
import importlib.util
import json
import os
import platform
import plistlib
import sqlite3
import stat
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pebble import migrations, token_budget
from pebble.release_readiness import Root, bundle_digest, bundle_entries

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load('probe_installed_app', ROOT / 'scripts/probe_installed_app.py')
pytestmark = pytest.mark.skipif(not migrations.supported(), reason='needs POSIX descriptor-relative file operations')

CANDIDATE, PRODUCTION = 'com.pebble.candidate', 'com.pebble.local'
RES = 'Contents/Resources/resources/'
TOKENIZER = ROOT / 'src-tauri/resources/tokenizer' / token_budget.TOKENIZER_FILE
SENTINEL = 'sentinel-user-text-7f3a'
EXECUTABLE = 0o755


def tokenizer_available():
    try:
        import tiktoken  # noqa: F401
    except ImportError:
        return False
    return TOKENIZER.is_file()


needs_tokenizer = pytest.mark.skipif(not tokenizer_available(), reason='needs tiktoken and the checkout tokenizer')


def make_app(parent, identifier=CANDIDATE, name='Pebble Candidate.app', tokenizer=True):
    app = parent / name
    files = {
        'Contents/Info.plist': plistlib.dumps({'CFBundleIdentifier': identifier, 'CFBundleExecutable': 'pebble',
                                               'CFBundleShortVersionString': '0.4.2', 'CFBundleVersion': '0.4.2'}),
        'Contents/MacOS/pebble': b'\xcf\xfa\xed\xfe fake shell',
        'Contents/Resources/icon.icns': b'icns',
        RES + 'backend/pebble-backend': b'\xcf\xfa\xed\xfe fake backend',
        RES + 'backend/_internal/base_library.zip': b'PK fake library',
        RES + 'llama/llama-server': b'\xcf\xfa\xed\xfe fake llama-server',
        RES + 'llama/libggml.dylib': b'\xcf\xfa\xed\xfe fake ggml',
        RES + 'model.json': b'{"model": "fixture"}',
        RES + 'NOTICE.md': b'# Pebble third-party notices\n',
        RES + 'licenses/PyJWT-LICENSE': b'MIT License\n',
        RES + 'tokenizer/' + token_budget.TOKENIZER_FILE: TOKENIZER.read_bytes() if tokenizer else b'not the ranks',
    }
    for rel, data in files.items():
        path = app / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if rel.endswith(('MacOS/pebble', 'pebble-backend', 'llama-server')):
            path.chmod(EXECUTABLE)
    return app


def stage_manifest(app, path):
    """The shape T1 (scripts/build_candidate_app.py) writes: entries carry mode and source beside path, bytes, sha256."""
    resources = app / RES.rstrip('/')
    with Root(str(resources), 'stage') as root:
        entries, links, problems = bundle_entries(root)
    assert not problems
    files = [dict(entry, mode='%04o' % stat.S_IMODE(os.lstat(resources / entry['path']).st_mode),
                  source='src-tauri/resources/' + entry['path']) for entry in entries]
    path.write_text(json.dumps({'schema': 'pebble.candidate-stage/1', 'resources': str(resources),
                                'bundle_sha256': bundle_digest(entries), 'file_count': len(entries), 'files': files,
                                'links': [dict(link, source=None) for link in links],
                                'tokenizer': {'path': 'tokenizer/' + token_budget.TOKENIZER_FILE}}))
    return path


def epoch9_home(parent, name=CANDIDATE):
    """A current epoch-9 home from the migration layer's own fresh path: prepare, the reference schema, complete."""
    home = parent / name
    upgrade = migrations.prepare(home)
    try:
        with closing(sqlite3.connect(home / migrations.DATABASE, isolation_level=None)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(migrations.REFERENCE_DDL)
            for table, version in migrations.VERSIONS.items():  # each subsystem's version row, as its owner writes it
                db.execute(f'INSERT INTO {table}(version) VALUES(?)', (version,))
            db.execute("INSERT INTO search_index_meta VALUES('schema', ?)", (migrations.SEARCH_SCHEMA,))
            db.execute("INSERT INTO chats VALUES(?,?,NULL,?)", (uuid.uuid4().hex, 'Chat ' + SENTINEL, time.time()))
        receipt = upgrade.complete()
    finally:
        upgrade.abort()
    assert receipt['state'] == 'fresh' and receipt['completed']
    return home


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def iso(when):
    return when.isoformat()


@pytest.fixture(scope='module')
def snapshot(tmp_path_factory):
    path = tmp_path_factory.mktemp('snapshot') / 'tested-source.json'
    path.write_text(json.dumps({'files': {'backend/main.py': 'a' * 64}}))
    return path


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    return make_app(tmp_path_factory.mktemp('bundle'), tokenizer=tokenizer_available())


@pytest.fixture(scope='module')
def home(tmp_path_factory):
    return epoch9_home(tmp_path_factory.mktemp('homes'))


class Runner:
    def __init__(self, tmp_path, snapshot):
        self.tmp_path, self.snapshot, self.count = tmp_path, snapshot, 0

    def __call__(self, check, app, *extra, hooks=None, report=None):
        self.count += 1
        report = report or self.tmp_path / f'report-{self.count:02d}-{check}.json'
        code = probe.main([check, '--app', str(app), '--source-snapshot', str(self.snapshot), '--report', str(report),
                           *map(str, extra)], hooks)
        return code, (json.loads(report.read_text()) if code != 2 else None)


@pytest.fixture
def run(tmp_path, snapshot):
    return Runner(tmp_path, snapshot)


def no_processes():
    return []


def hooks(**changes):
    return probe.Hooks(**{'processes': no_processes, 'cpu_times': lambda pids: {}, **changes})


def codes(report):
    return [finding['code'] for finding in report['findings']]


# ---- Constants -----------------------------------------------------------------------------------


@needs_tokenizer
def test_probe_count_is_the_exact_o200k_count_and_the_cold_start_bar_is_measure_resources():
    encoding = token_budget._load(TOKENIZER.parent)
    counted = len(encoding.encode(probe.PROBE_TEXT, disallowed_special=()))
    assert counted == probe.PROBE_TOKENS, counted
    assert counted != len(probe.PROBE_TEXT.encode()), 'the probe text must tell o200k from the byte estimate'
    measure = load('measure_resources_for_probe', ROOT / 'scripts/measure_resources.py')
    assert probe.COLD_START_P95_SECONDS == measure.THRESHOLDS['cold_start_p95_seconds']


def test_the_cli_help_renders(capsys):
    with pytest.raises(SystemExit) as done:
        probe.main(['--help'])
    assert done.value.code == 0 and 'needs_user' in capsys.readouterr().out
    with pytest.raises(SystemExit) as done:
        probe.main(['no_such_check', '--app', 'x.app', '--source-snapshot', 's', '--report', 'r'])
    assert done.value.code == 2


def test_ps_time_column_parses_every_shape():
    assert probe.parse_cputime('0:01.50') == 1.5
    assert probe.parse_cputime('12:00.00') == 720.0
    assert probe.parse_cputime('1:02:03.45') == pytest.approx(3723.45)
    assert probe.parse_cputime('2-01:00:00') == 2 * 86400 + 3600
    with pytest.raises(ValueError):
        probe.parse_cputime('soon')


# ---- Subject binding and refusals ------------------------------------------------------------------


def test_every_report_binds_the_subject_hashes(tmp_path, run, app, home, snapshot):
    with Root(str(app), 'app') as root:
        entries, _, problems = bundle_entries(root)
    assert not problems
    executable = app / RES / 'backend/pebble-backend'
    verifier = load('verify_harness_candidate_for_probe', ROOT / 'scripts/verify_harness_candidate.py')
    expected = {'source_snapshot_sha256': sha(snapshot), 'candidate_executable_sha256': sha(executable),
                'candidate_bundle_sha256': verifier.candidate_manifest(executable)['bundle_sha256'],
                'app_bundle_sha256': bundle_digest(entries),
                'platform': f'{sys.platform}-{platform.machine()}'.lower()}  # the trust channel's label (T3)
    since = iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    manifest = stage_manifest(app, tmp_path / 'stage-manifest.json')
    runs = [('sidecar', '--stage-manifest', manifest), ('processes',), ('logs', '--home', home, '--since', since),
            ('data', '--home', home), ('timezone', '--home', home, '--since', since, '--scratch', tmp_path / 'scratch'),
            ('login_item',), ('pet_gaze',), ('frame_rate',), ('accessibility',), ('containment',), ('cold_start',),
            ('export_uninstall',), ('webview_cpu', '--max-cpu-percent', 5, '--seconds', 1), ('webview_store',),
            ('adhoc_replacement',)]
    for check, *extra in runs:
        code, report = run(check, app, *extra, hooks=hooks())
        assert report is not None, check
        assert report['format'] == probe.FORMAT and report['check'] == check
        assert report['subject'] == expected, check
        assert report['result'] in ('passed', 'failed', 'needs_user') and code == probe.EXIT[report['result']]
        assert report['app']['bundle_identifier'] == CANDIDATE and report['probe_sha256'] == sha(probe.__file__)
    assert set(probe.CHECKS) == {item[0] for item in runs} | {'banners', 'sleep_wake'}


def test_existing_report_production_home_and_installed_location_are_refused(tmp_path, run, app, capsys):
    existing = tmp_path / 'existing.json'
    existing.write_text('keep')
    code, _ = run('pet_gaze', app, report=existing)
    assert code == 2 and existing.read_text() == 'keep'
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == 'report_exists'

    production_home = tmp_path / 'other' / PRODUCTION
    production_home.mkdir(parents=True)
    report = tmp_path / 'production.json'
    code, written = run('logs', app, '--home', production_home, '--since', iso(datetime.now(timezone.utc)), report=report)
    assert code == 2 and written is None
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == 'production_home_needs_installed'

    # A link named otherwise that resolves to a com.pebble.local folder is refused the same way.
    alias = tmp_path / 'alias-home'
    alias.symlink_to(production_home)
    code, written = run('logs', app, '--home', alias, '--since', iso(datetime.now(timezone.utc)))
    assert code == 2 and written is None
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == 'production_home_needs_installed'

    # The test bundle cannot be probed against another identifier's home.
    other = tmp_path / 'com.pebble.other'
    other.mkdir()
    code, written = run('logs', app, '--home', other, '--since', iso(datetime.now(timezone.utc)))
    assert code == 2 and written is None
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == 'home_identifier_mismatch'

    # Nothing is ever written into the home or the app.
    candidate_home = tmp_path / CANDIDATE
    candidate_home.mkdir()
    code, written = run('pet_gaze', app, '--home', candidate_home, report=candidate_home / 'report.json')
    assert code == 2 and not (candidate_home / 'report.json').exists()
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == 'report_inside_subject'

    # An installed location is refused without --installed, before anything there is read.
    with pytest.raises(probe.Refused) as refused:
        probe.app_path('/Applications/Pebble Probe Fixture That Does Not Exist.app', False)
    assert refused.value.code == 'installed_app_needs_installed'
    assert probe.installed_location('/Applications/Pebble.app/') and not probe.installed_location(str(tmp_path))

    # With --installed the production identifiers are accepted, and a database copy needs an explicit --scratch.
    release = make_app(tmp_path / 'release', identifier=PRODUCTION, name='Pebble.app', tokenizer=False)
    code, written = run('pet_gaze', release, '--home', production_home, '--installed')
    assert code == probe.EXIT['needs_user'] and written['installed'] and written['home']['installed']
    code, written = run('timezone', release, '--home', production_home, '--installed', '--since',
                        iso(datetime.now(timezone.utc)))
    assert code == 2 and written is None
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == 'scratch_required'


def test_a_future_since_and_an_unexpected_bundle_are_not_accepted(tmp_path, run, app, home, capsys):
    code, _ = run('logs', app, '--home', home, '--since', iso(datetime.now(timezone.utc) + timedelta(days=30)))
    assert code == 2
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == 'since_in_future'
    code, report = run('pet_gaze', app, '--expect-app-sha256', 'f' * 64, *witness_all('pet_gaze'))
    assert code == probe.EXIT['failed'] and 'app_bundle_unexpected' in codes(report)


# ---- Sidecar ---------------------------------------------------------------------------------------


@needs_tokenizer
def test_a_resource_that_differs_from_the_stage_manifest_fails_the_sidecar_check_with_its_exact_path(tmp_path, run):
    app = make_app(tmp_path / 'bundle')
    manifest = stage_manifest(app, tmp_path / 'stage-manifest.json')
    code, report = run('sidecar', app, '--stage-manifest', manifest, hooks=hooks())
    assert code == 0 and report['result'] == 'passed', report['findings']
    tokenizer = report['automated']['tokenizer']
    assert tokenizer['counted_tokens'] == probe.PROBE_TOKENS and tokenizer['method'] == 'o200k_bundled'
    assert report['automated']['resources']['equal_to_stage_manifest']
    assert all(asset['present'] and asset['executable'] for asset in report['automated']['assets'].values())

    (app / RES / 'llama/libggml.dylib').write_bytes(b'\xcf\xfa\xed\xfe changed ggml')
    (app / RES / 'licenses/Unlisted-LICENSE').write_bytes(b'unlisted')
    code, report = run('sidecar', app, '--stage-manifest', manifest, hooks=hooks())
    assert code == probe.EXIT['failed'] and report['result'] == 'failed'
    resources = report['automated']['resources']
    assert resources['differing'] == ['llama/libggml.dylib'] and resources['unexpected'] == ['licenses/Unlisted-LICENSE']
    assert {'code': 'resource_differs', 'subject': 'llama/libggml.dylib'} in report['findings']
    assert {'code': 'resource_unexpected', 'subject': 'licenses/Unlisted-LICENSE'} in report['findings']

    # Same bytes with lost permission bits also differ from the stage, and the server is no longer executable.
    app = make_app(tmp_path / 'bundle-2')
    manifest = stage_manifest(app, tmp_path / 'stage-manifest-2.json')
    (app / RES / 'llama/llama-server').chmod(0o644)
    code, report = run('sidecar', app, '--stage-manifest', manifest, hooks=hooks())
    assert report['result'] == 'failed' and report['automated']['resources']['mode_differs'] == ['llama/llama-server']
    assert set(codes(report)) == {'resource_mode_differs', 'asset_not_executable'}


def test_an_altered_or_missing_tokenizer_and_a_missing_llama_server_fail_the_sidecar_check(tmp_path, run):
    app = make_app(tmp_path / 'bundle', tokenizer=False)
    (app / RES / 'llama/llama-server').unlink()
    manifest = stage_manifest(app, tmp_path / 'stage-manifest.json')
    code, report = run('sidecar', app, '--stage-manifest', manifest, hooks=hooks())
    assert code == probe.EXIT['failed']
    assert {'code': 'tokenizer_digest_mismatch', 'subject': 'tokenizer/' + token_budget.TOKENIZER_FILE} in report['findings']
    assert {'code': 'asset_missing', 'subject': 'llama/llama-server'} in report['findings']
    assert report['automated']['tokenizer']['counted_tokens'] is None
    assert report['automated']['resources']['equal_to_stage_manifest']  # equal, and still not a pass


# ---- Logs ------------------------------------------------------------------------------------------


def traceback_text(kind):
    return (f'ERROR:root:Request failed: chat\nTraceback (most recent call last):\n  File "app.py", line 3, in run\n'
            f'    raise {kind}()\n{kind}: {SENTINEL}\n')


def test_a_traceback_after_the_timestamp_fails_the_log_check_while_an_older_one_does_not(tmp_path, run, app):
    home = tmp_path / CANDIDATE
    home.mkdir()
    log = home / 'service.log'
    log.write_text('starting\n' + traceback_text('ValueError'))
    old = time.time() - 7200
    os.utime(log, (old, old))
    since = iso(datetime.now(timezone.utc) - timedelta(hours=1))
    code, baseline = run('logs', app, '--home', home, '--since', since)
    assert code == 0 and baseline['result'] == 'passed'
    entry = baseline['automated']['logs']['service.log']
    assert entry['tracebacks'] == {'total': 1, 'new': 0, 'old': 1, 'unknown': 0}
    assert entry['judged_by'] == 'modification_time' and baseline['automated']['logs']['backend.log'] == {'exists': False}

    with log.open('a') as stream:
        stream.write(traceback_text('RuntimeError'))
    code, report = run('logs', app, '--home', home, '--since', since, '--baseline', baseline_path(run, tmp_path))
    assert code == probe.EXIT['failed']
    entry = report['automated']['logs']['service.log']
    assert entry['tracebacks'] == {'total': 2, 'new': 1, 'old': 1, 'unknown': 0}
    assert entry['judged_by'] == 'baseline_offset' and entry['new_exception_types'] == ['RuntimeError']
    assert codes(report) == ['new_traceback'] and SENTINEL not in json.dumps(report)

    # Without a baseline a log modified after --since cannot date its tracebacks, so the check fails closed.
    code, report = run('logs', app, '--home', home, '--since', since)
    assert code == probe.EXIT['failed'] and set(codes(report)) == {'traceback_time_unknown'}

    # A log created after the baseline is new in full; a replaced log is a baseline mismatch.
    (home / 'backend.log').write_text(traceback_text('KeyError'))
    code, report = run('logs', app, '--home', home, '--since', since, '--baseline', baseline_path(run, tmp_path))
    assert report['automated']['logs']['backend.log']['tracebacks']['new'] == 1
    log.write_text('rotated\n')
    code, report = run('logs', app, '--home', home, '--since', since, '--baseline', baseline_path(run, tmp_path))
    assert ('log_baseline_mismatch', 'service.log') in {(f['code'], f['subject']) for f in report['findings']}


def baseline_path(run, tmp_path):
    return tmp_path / 'report-01-logs.json'


# ---- Witness ---------------------------------------------------------------------------------------


def witness_all(check, value='yes'):
    items = []
    for key, bound, _ in probe.CHECKS[check].checklist:
        items += ['--witness', f'{key}={value if bound in (None, probe.OBSERVE) else bound}']
    return items


def test_witness_entries_are_recorded_as_witnessed_and_a_needs_user_check_never_passes_without_them(run, app, capsys):
    keys = [key for key, _, _ in probe.CHECKS['pet_gaze'].checklist]
    code, report = run('pet_gaze', app)
    assert code == probe.EXIT['needs_user'] and report['result'] == 'needs_user'
    assert report['missing_witness'] == keys and report['automated'] == {} and report['automated_state'] == 'none'
    assert [item['key'] for item in report['checklist']] == keys and all(item['prompt'] for item in report['checklist'])

    code, report = run('pet_gaze', app, *witness_all('pet_gaze')[:-2])
    assert report['result'] == 'needs_user' and report['missing_witness'] == keys[-1:]

    code, report = run('pet_gaze', app, *witness_all('pet_gaze'))
    assert code == 0 and report['result'] == 'passed'
    assert report['witnessed']['entries'] == dict.fromkeys(keys, 'yes') and report['witnessed']['origin'] == 'person'
    assert report['automated'] == {} and not any(key in json.dumps(report['automated']) for key in keys)

    code, report = run('pet_gaze', app, *witness_all('pet_gaze')[:-2], '--witness', keys[-1] + '=no')
    assert report['result'] == 'failed' and report['witnessed']['failing'] == [keys[-1]]

    code, report = run('frame_rate', app, '--witness', 'fps_active=31', '--witness', 'fps_low_power=15',
                       '--witness', 'no_frames_after_hold=yes')
    assert report['result'] == 'failed' and report['witnessed']['failing'] == ['fps_active']

    for bad in (['--witness', 'seen_by_robot=yes'], ['--witness', keys[0] + '=maybe'],
                ['--witness', keys[0] + '=yes', '--witness', keys[0] + '=yes']):
        code, report = run('pet_gaze', app, *bad)
        assert code == 2 and report is None
    code, report = run('processes', app, '--witness', 'banner_seen=yes', hooks=hooks())
    assert code == 2 and report is None
    refusals = [json.loads(line)['refused'] for line in capsys.readouterr().err.strip().splitlines()]
    assert refusals == ['witness_unknown_key', 'witness_value', 'witness_duplicate', 'witness_unknown_key']


def test_login_item_needs_the_route_outputs_and_the_person(tmp_path, run, app, capsys):
    def output(name, status):
        path = tmp_path / f'{name}.json'
        path.write_text(json.dumps({'status': status}))
        return path
    sequence = ['--route-output', output('a', 'requires_approval'), '--route-output', output('b', 'enabled'),
                '--route-output', output('c', 'not_registered')]
    code, report = run('login_item', app, *witness_all('login_item'))
    assert report['result'] == 'needs_user' and report['automated_state'] == 'inconclusive'
    code, report = run('login_item', app, *sequence)
    assert report['result'] == 'needs_user' and report['automated']['sequence_complete']
    assert [item['status'] for item in report['automated']['route_outputs']] == ['requires_approval', 'enabled',
                                                                                 'not_registered']
    code, report = run('login_item', app, *sequence, *witness_all('login_item'))
    assert code == 0 and report['result'] == 'passed'
    code, report = run('login_item', app, '--route-output', output('d', 'not_found'), *witness_all('login_item'))
    assert report['result'] == 'failed' and codes(report) == ['login_item_not_found']
    output('e', 'enabled')
    (tmp_path / 'e.json').write_text('{"status": "enabled", "extra": 1}')
    code, report = run('login_item', app, '--route-output', tmp_path / 'e.json')
    assert code == 2 and json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == 'route_output_invalid'


# ---- Data and rows ---------------------------------------------------------------------------------


def test_data_check_inspects_the_epoch9_home_read_only(tmp_path, run, app, home):
    before = sha(home / migrations.DATABASE)
    code, report = run('data', app, '--home', home)
    assert code == 0 and report['result'] == 'passed', report['findings']
    inspected = report['automated']['inspect']
    assert inspected['state'] == 'current' and inspected['database']['user_version'] == migrations.SCHEMA_EPOCH
    # A fresh home's history row has no backup, which inspect does not count as an upgrade.
    assert inspected['database']['integrity'] == 'ok' and inspected['database']['upgrade_history'] == 0
    assert SENTINEL not in json.dumps(report)

    rows = dict(inspected['database']['rows'])
    expected = tmp_path / 'expected-rows.json'
    expected.write_text(json.dumps({'inspect': {'database': {'rows': {**rows, 'chats': rows['chats'] + 1}}}}))
    code, report = run('data', app, '--home', home, '--expect-rows', expected, '--upgraded')
    assert code == probe.EXIT['failed']
    assert {'row_count_changed', 'upgrade_history_missing', 'no_verified_backup'} == set(codes(report))
    assert report['automated']['expected_rows']['changed'] == ['chats']
    expected.write_text(json.dumps({'rows': rows}))
    code, report = run('data', app, '--home', home, '--expect-rows', expected)
    assert code == 0 and report['automated']['expected_rows']['changed'] == []
    assert sha(home / migrations.DATABASE) == before


def test_an_older_epoch_is_not_current_and_its_upgrade_passes_the_installed_readback(tmp_path, run, app):
    home = epoch9_home(tmp_path)
    with closing(sqlite3.connect(home / migrations.DATABASE)) as db:
        db.execute('PRAGMA user_version=8')
    code, report = run('data', app, '--home', home)
    assert code == probe.EXIT['failed'] and {'state_not_current', 'epoch_mismatch'} <= set(codes(report))

    # The first launch's upgrade, through the migration layer itself: a verified backup, then epoch 9.
    upgrade = migrations.prepare(home)
    try:
        receipt = upgrade.complete()
    finally:
        upgrade.abort()
    assert receipt['state'] == 'backed_up' and receipt['backup_id']
    code, report = run('data', app, '--home', home, '--upgraded')
    assert code == 0 and report['result'] == 'passed', report['findings']
    backups = report['automated']['inspect']['backups']
    assert [backup['id'] for backup in backups] == [receipt['backup_id']] and backups[0]['verified'] is True
    assert report['automated']['inspect']['database']['upgrade_history'] == 1


def seed_rows(home, since):
    after, before = since + 30, since - 600
    with closing(sqlite3.connect(home / migrations.DATABASE, isolation_level=None)) as db:
        db.execute('BEGIN')
        db.executemany('INSERT INTO lifecycle_observations(id,kind,source,observed,started,seconds,zone) VALUES(?,?,?,?,?,?,?)', [
            (uuid.uuid4().hex, 'will_sleep', 'native', before, None, 0.0, None),  # an older sleep is ignored
            (uuid.uuid4().hex, 'will_sleep', 'native', after, None, 0.0, None),
            (uuid.uuid4().hex, 'sleep', 'clock', after + 150, after, 150.0, None),
            (uuid.uuid4().hex, 'did_wake', 'native', after + 151, None, 0.0, None),
            (uuid.uuid4().hex, 'timezone_changed', 'native', after + 200, None, 0.0, 'Europe/London')])
        job, chat, notice = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        db.execute('INSERT INTO native_banners(id,scope,job_id,chat_id,members,version,state,delivered_version,attempts,'
                   'reason,opened,posted,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (notice, 'job', job, chat, 1, 1, 'delivered', 1, 1, None, after, after + 1, after + 1))
        db.execute('INSERT INTO native_deliveries(notification_id,job_id,chat_id,category,state,reason,banner_id,'
                   'banner_version,attempts,created,posted,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (notice, job, chat, 'completed', 'delivered', None, notice, 1, 1, after, after + 1, after + 1))
        db.execute('INSERT INTO host_notices(id,chat_id,job_id,kind,fingerprint,title,body,created,acknowledged) '
                   'VALUES(?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, chat, job, 'wake_summary', uuid.uuid4().hex,
                                                 'Missed while asleep', SENTINEL, after + 152, None))
        db.execute('COMMIT')


def test_banner_sleep_and_timezone_rows_come_from_a_read_only_copy(tmp_path, run, app, capsys):
    home = epoch9_home(tmp_path)
    since = datetime.now(timezone.utc) - timedelta(minutes=10)
    seed_rows(home, since.timestamp())
    before = sha(home / migrations.DATABASE)

    code, report = run('banners', app, '--home', home, '--since', iso(since), '--scratch', tmp_path / 'copy-1')
    assert report['result'] == 'needs_user' and report['automated_state'] == 'passed'
    copy = report['automated']['database_copy']
    assert copy['source_mode'] == 'immutable' and copy['sha256'] == sha(copy['path'])
    assert Path(copy['path']).parent == tmp_path / 'copy-1' and (Path(copy['path']).stat().st_mode & 0o777) == 0o400
    code, report = run('banners', app, '--home', home, '--since', iso(since), '--scratch', tmp_path / 'copy-2',
                       *witness_all('banners'))
    assert code == 0 and report['result'] == 'passed'
    assert report['automated']['deliveries'] == {'delivered': 1}

    code, report = run('sleep_wake', app, '--home', home, '--since', iso(since), '--scratch', tmp_path / 'copy-3')
    assert report['result'] == 'needs_user' and report['automated_state'] == 'passed', report['findings']
    observations = report['automated']['observations']
    assert observations['will_sleep']['count'] == 1 and observations['sleep']['longest_seconds'] == 150.0
    assert report['automated']['wake_summaries'] == 1 and SENTINEL not in json.dumps(report)
    code, report = run('sleep_wake', app, '--home', home, '--since', iso(since), '--scratch', tmp_path / 'copy-4',
                       '--min-sleep-seconds', 600, *witness_all('sleep_wake'))
    assert report['result'] == 'failed' and codes(report) == ['sleep_too_short']

    code, report = run('timezone', app, '--home', home, '--since', iso(since), '--scratch', tmp_path / 'copy-5',
                       '--expect-zone', 'Europe/London')
    assert code == 0 and report['automated']['rows'] == [{'zone': 'Europe/London', 'source': 'native'}]
    code, report = run('timezone', app, '--home', home, '--since', iso(since), '--scratch', tmp_path / 'copy-6',
                       '--expect-zone', 'Asia/Kolkata')
    assert report['result'] == 'failed' and codes(report) == ['timezone_unexpected']
    assert sha(home / migrations.DATABASE) == before and not (home / (migrations.DATABASE + '-shm')).exists()

    # A live home (its -wal present) is copied through SQLite's read-only mode; the copy is still immutable.
    with closing(sqlite3.connect(home / migrations.DATABASE, isolation_level=None)) as writer:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("INSERT INTO lifecycle_observations(id,kind,source,observed,started,seconds,zone) "
                       "VALUES(?,'timezone_changed','native',?,NULL,0,'Asia/Kolkata')", (uuid.uuid4().hex, time.time()))
        code, report = run('timezone', app, '--home', home, '--since', iso(since), '--scratch', tmp_path / 'copy-7')
    assert report['automated']['database_copy']['source_mode'] == 'read_only'
    assert codes(report) == ['timezone_rows'] and len(report['automated']['rows']) == 2

    (tmp_path / 'taken').mkdir()
    code, report = run('timezone', app, '--home', home, '--since', iso(since), '--scratch', tmp_path / 'taken')
    assert code == 2 and json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == 'scratch_exists'
    rows = sqlite3.connect((home / migrations.DATABASE).as_uri() + '?mode=ro', uri=True)
    with closing(rows):
        assert rows.execute('SELECT count(*) FROM lifecycle_observations').fetchone()[0] == 6


def test_a_home_without_a_database_fails_the_row_checks(tmp_path, run, app):
    home = tmp_path / CANDIDATE
    home.mkdir()
    code, report = run('banners', app, '--home', home, '--since', iso(datetime.now(timezone.utc)),
                       '--scratch', tmp_path / 'copy', *witness_all('banners'))
    assert code == probe.EXIT['failed'] and codes(report) == ['database_missing']


# ---- Processes and CPU -----------------------------------------------------------------------------


def table(app, home, *, shells=1, parent=None, home_arg=None, extra=()):
    shell = str(app / 'Contents/MacOS/pebble')
    backend = str(app / RES / 'backend/pebble-backend')
    rows = [{'pid': 100 + n, 'ppid': 1, 'path': shell, 'args': shell} for n in range(shells)]
    rows.append({'pid': 200, 'ppid': 100 if parent is None else parent, 'path': backend,
                 'args': f'{backend} --home {home_arg or home} --resources {app / RES.rstrip("/")}'})
    rows.append({'pid': 201, 'ppid': 200, 'path': backend, 'args': f'{backend} --job-anchor /tmp/job'})
    # Same names elsewhere: the user's own installed app, and a decoy whose arguments claim the bundle's path.
    rows.append({'pid': 300, 'ppid': 1, 'path': '/Applications/Pebble.app/Contents/MacOS/pebble', 'args': 'pebble'})
    rows.append({'pid': 301, 'ppid': 100, 'path': '/private/tmp/decoy/pebble-backend', 'args': f'{backend} --home {home}'})
    rows.extend(extra)
    return lambda: [dict(row) for row in rows]


def test_processes_are_matched_by_exact_executable_path(run, app, home, monkeypatch):
    def forbidden(*unused, **unused_too):
        raise AssertionError('The probe must never signal or start a process.')
    for name in ('kill', 'killpg'):
        monkeypatch.setattr(os, name, forbidden)
    monkeypatch.setattr(probe, 'run_bounded', forbidden)

    code, report = run('processes', app, '--home', home, hooks=hooks(processes=table(app, home)))
    assert code == 0 and report['result'] == 'passed', report['findings']
    observed = report['automated']
    assert [row['pid'] for row in observed['shell']] == [100] and [row['pid'] for row in observed['service']] == [200]
    assert [row['pid'] for row in observed['helpers']] == [201] and observed['service_home_matches']
    assert [row['pid'] for row in observed['other_copies']] == [300]  # reported before install, never matched

    cases = [(dict(shells=2), 'shell_count'), (dict(parent=999), 'service_not_child_of_shell'),
             (dict(home_arg='/private/tmp/elsewhere/com.pebble.candidate'), 'service_home_mismatch')]
    for change, code_name in cases:
        code, report = run('processes', app, '--home', home, hooks=hooks(processes=table(app, home, **change)))
        assert code == probe.EXIT['failed'] and code_name in codes(report), change

    code, report = run('processes', app, '--expect', 'absent', hooks=hooks(processes=table(app, home)))
    assert report['result'] == 'failed' and codes(report).count('process_from_app_running') == 3
    code, report = run('processes', app, '--expect', 'absent', hooks=hooks(processes=lambda: []))
    assert code == 0

    def broken():
        raise OSError('ps failed')
    code, report = run('processes', app, hooks=hooks(processes=broken))
    assert report['result'] == 'failed' and codes(report) == ['process_list_unavailable']

    code, report = run('export_uninstall', app, *witness_all('export_uninstall'), hooks=hooks(processes=lambda: []))
    assert code == 0 and report['automated']['expect'] == 'absent'
    code, report = run('export_uninstall', app, *witness_all('export_uninstall'), hooks=hooks(processes=table(app, home)))
    assert report['result'] == 'failed'


class FakeHost:
    """A clock, sleep and ps CPU column that advance together; per-pid CPU percent of one core."""

    def __init__(self, rates):
        self.rates, self.now = rates, 1000.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def cpu_times(self, pids):
        return {pid: round(self.rates[pid] / 100 * (self.now - 1000.0), 2) for pid in pids if pid in self.rates}


def test_webview_cpu_samples_ps_for_a_bounded_window(run, app, home, capsys):
    webkit = next(iter(sorted(probe.WEBKIT_PATHS)))
    extra = ({'pid': 400, 'ppid': 1, 'path': webkit, 'args': webkit}, {'pid': 401, 'ppid': 1, 'path': webkit, 'args': webkit})

    def window(rates, *options):
        host = FakeHost(rates)
        return run('webview_cpu', app, '--max-cpu-percent', 2.0, '--seconds', 10, '--interval', 1, *options,
                   hooks=hooks(processes=table(app, home, extra=extra), cpu_times=host.cpu_times, clock=host.clock,
                               sleep=host.sleep))

    code, report = window({100: 0.5, 400: 0.5, 401: 0.5})
    assert code == 0 and report['automated']['total_cpu_percent'] == pytest.approx(1.5, abs=0.05)
    assert len(report['automated']['samples']) == 10
    assert report['automated']['webkit_attribution'] == 'all_webkit_processes_upper_bound'
    code, report = window({100: 0.5, 400: 0.5, 401: 5.0})
    assert report['result'] == 'needs_user' and codes(report) == ['webkit_unattributed_over_budget']
    code, report = window({100: 0.5, 400: 5.0, 401: 0.1}, '--webkit-pid', 400)
    assert report['result'] == 'failed' and codes(report) == ['cpu_over_budget']
    code, report = window({100: 0.5, 400: 0.5, 401: 5.0}, '--webkit-pid', 400)
    assert code == 0 and report['automated']['webkit_attribution'] == 'given_pids'
    code, report = window({100: 0.5}, '--webkit-pid', 100)
    assert report['result'] == 'failed' and codes(report) == ['webkit_pid_not_webkit']
    code, report = run('webview_cpu', app, hooks=hooks())
    assert code == 2 and json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == 'max_cpu_percent_required'


# ---- Open checks 3 and 5 -----------------------------------------------------------------------------


def test_webview_store_is_listed_read_only_and_needs_the_person(tmp_path, run, app, capsys):
    store = tmp_path / 'WebKit' / CANDIDATE
    write = {'WebsiteData/LocalStorage/tauri_localhost_0.localstorage': SENTINEL.encode(),
             'WebsiteData/IndexedDB/v1/x.sqlite3': b'0123456789'}
    for rel, data in write.items():
        (store / rel).parent.mkdir(parents=True, exist_ok=True)
        (store / rel).write_bytes(data)
    before = {rel: sha(store / rel) for rel in write}
    code, report = run('webview_store', app, '--webview-store', store)
    assert report['result'] == 'needs_user' and report['automated_state'] == 'passed'
    listed = report['automated']['store']
    assert listed['exists'] and listed['files'] == 2 and listed['bytes'] == len(SENTINEL) + 10
    assert listed['top_level'] == ['WebsiteData'] and SENTINEL not in json.dumps(report)
    code, report = run('webview_store', app, '--webview-store', store, *witness_all('webview_store'))
    assert code == 0 and report['result'] == 'passed'
    assert {rel: sha(store / rel) for rel in write} == before

    # Moving the store aside is an observation, either way; a lost conversation is a failure.
    code, report = run('webview_store', app, '--webview-store', store, *witness_all('webview_store')[:-2],
                       '--witness', 'only_drafts_lost=no')
    assert report['result'] == 'failed' and report['witnessed']['failing'] == ['only_drafts_lost']
    code, report = run('webview_store', app, '--webview-store', tmp_path / 'absent' / CANDIDATE, *witness_all('webview_store'))
    assert report['result'] == 'needs_user' and report['automated']['store'] == {
        'path': str(tmp_path / 'absent' / CANDIDATE), 'exists': False}
    code, report = run('webview_store', app, *witness_all('webview_store'))
    assert report['result'] == 'needs_user' and report['automated'] == {'store': None}

    production = tmp_path / 'WebKit' / PRODUCTION
    production.mkdir()
    for target, refusal in ((production, 'production_store_needs_installed'),
                            (tmp_path / 'WebKit', 'store_identifier_mismatch')):
        code, report = run('webview_store', app, '--webview-store', target)
        assert code == 2 and json.loads(capsys.readouterr().err.strip().splitlines()[-1])['refused'] == refusal


def test_adhoc_replacement_records_what_macos_asked_for_without_failing_on_it(run, app):
    keys = {key: bound for key, bound, _ in probe.CHECKS['adhoc_replacement'].checklist}
    observed = [key for key, bound in keys.items() if bound == probe.OBSERVE]
    assert observed == ['keychain_prompted', 'tcc_regrant_needed', 'login_item_reapproval_needed',
                        'notifications_still_authorized']
    answers = []
    for key, bound in keys.items():
        answers += ['--witness', f'{key}={"no" if bound == probe.OBSERVE else "yes"}']
    code, report = run('adhoc_replacement', app, *answers)
    assert code == 0 and report['result'] == 'passed' and report['witnessed']['failing'] == []
    assert report['witnessed']['entries']['keychain_prompted'] == 'no'
    assert {item['key']: item['answer'] for item in report['checklist']}['keychain_prompted'] == 'yes or no, recorded either way'
    code, report = run('adhoc_replacement', app, *answers[:-4])
    assert report['result'] == 'needs_user'
    code, report = run('adhoc_replacement', app, *[item.replace('provider_status_read_back=yes', 'provider_status_read_back=no')
                                                   for item in answers])
    assert report['result'] == 'failed' and report['witnessed']['failing'] == ['provider_status_read_back']


# ---- Evidence-backed checks ------------------------------------------------------------------------


def test_cold_start_binds_a_frozen_resources_report_to_the_bundled_backend(tmp_path, run, app):
    backend = sha(app / RES / 'backend/pebble-backend')

    def resources(name, **changes):
        doc = {'schema': 'pebble.resources/1', 'kind': 'frozen', 'backend_sha256': backend,
               'thresholds': {'T8_cold_start_p95_seconds': {'measured': 1.2, 'limit': 5.0, 'met': True}}, **changes}
        path = tmp_path / name
        path.write_text(json.dumps(doc))
        return path
    launches = witness_all('cold_start')
    code, report = run('cold_start', app, *launches)
    assert report['result'] == 'needs_user' and report['automated'] == {'resources_report': None}
    code, report = run('cold_start', app, '--evidence', resources('ok.json'), *launches)
    assert code == 0 and report['automated']['met'] is True and report['evidence'][0]['kind'] == 'pebble.resources/1'
    slow = launches[:-1] + ['launch_5_seconds=6.5']
    code, report = run('cold_start', app, '--evidence', resources('ok2.json'), *slow)
    assert report['result'] == 'failed' and report['witnessed']['failing'] == ['launch_5_seconds']
    code, report = run('cold_start', app, '--evidence', resources('other.json', backend_sha256='0' * 64), *launches)
    assert report['result'] == 'failed' and codes(report) == ['resources_report_other_backend']
    code, report = run('cold_start', app, '--evidence', resources('source.json', kind='source'), *launches)
    assert report['result'] == 'failed' and 'resources_report_not_frozen' in codes(report)
