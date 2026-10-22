"""Phase 24 (t24-bench, V1-015): live evaluation never shares the installed app's ChatGPT grant.

In 0.6.1 the live driver ran the installed app's own profile (the metadata file was exported from it), so the driver's
owned Apps and the app held two copies of one rotating refresh token, and a live run broke one of them. Live commands
now run only the connection the driver's own sign-in recorded in its ledger, and refuse any profile whose connection
id or host id the installed app's settings hold. These tests use a fake HOME with a fake installed store, fake Apps
and no network; no live request, Keychain item or credential is involved.
"""

import importlib.util
import json
import os
import socket
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import evaluation as ev

APP_PROFILE = {'id': 'a1b2c3d4e5f60718293a4b5c6d7e8f90', 'kind': 'chatgpt', 'name': 'fixture@example.invalid · ChatGPT',
               'url': 'https://api.openai.com/v1', 'model': 'gpt-6-luna', 'context': 32768, 'budget': 32768,
               'tools': True, 'has_key': False, 'client_id': 'oaiapp_installed', 'subject': 'user-fixture',
               'email': 'fixture@example.invalid', 'connection_id': 'a1b2c3d4e5f60718293a4b5c6d7e8f90',
               'context_mode': 'full', 'budget_version': 2, 'context_version': 1,
               'provider_name': 'fixture@example.invalid · ChatGPT'}
APP_HOST = 'urn:uuid:11111111-1111-4111-8111-111111111111'
EVAL_PROFILE = {**APP_PROFILE, 'id': 'feedfacefeedfacefeedfacefeedface', 'connection_id': 'feedfacefeedfacefeedfacefeedface',
                'client_id': 'oaiapp_evaluation'}
EVAL_HOST = 'urn:uuid:22222222-2222-4222-8222-222222222222'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('These tests must not open network connections.')
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            refuse()
        return connect(self, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', refuse)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """HOME with an installed app's store at ~/.pebble/data holding the app's own ChatGPT connection."""
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    installed_store(home / '.pebble' / 'data', [APP_PROFILE], APP_HOST)
    return home


def installed_store(folder, profiles, host):
    folder.mkdir(parents=True)
    db = sqlite3.connect(folder / 'pebble.sqlite')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
    db.execute('CREATE TABLE messages(id TEXT PRIMARY KEY,content TEXT)')
    db.execute("INSERT INTO messages VALUES('m1','private words the guard must never read')")
    db.executemany('INSERT INTO settings VALUES(?,?)', [('model_profiles', json.dumps(profiles)),
                                                        ('chatgpt_host_id', json.dumps(host))])
    db.commit()
    db.close()


def metadata_file(path, profile, host):
    path.write_text(json.dumps({'profile': profile, 'chatgpt_host_id': host}))
    os.chmod(path, 0o600)
    return path


def load_cli():
    spec = importlib.util.spec_from_file_location('evaluate_harness', ev.ROOT / 'scripts/evaluate_harness.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


# -- the installed app's identities ------------------------------------------------------------------------------

def test_installed_identities_come_from_the_apps_settings_only(fake_home, tmp_path):
    found = ev.installed_connection_identities()
    assert found == {'connection_ids': {APP_PROFILE['id']}, 'host_ids': {APP_HOST}, 'stores': 1}
    assert ev.installed_app_homes() == [fake_home / '.pebble/data',
                                        fake_home / 'Library/Application Support/com.pebble.local']
    # The pre-0.6 copy (kept after the move) and PEBBLE_HOME are checked too.
    legacy = fake_home / 'Library/Application Support/com.pebble.local'
    installed_store(legacy, [{**APP_PROFILE, 'id': 'old', 'connection_id': 'old-connection'}], APP_HOST)
    assert ev.installed_connection_identities()['connection_ids'] == {APP_PROFILE['id'], 'old', 'old-connection'}
    assert ev.installed_app_homes(env={'PEBBLE_HOME': str(tmp_path / 'ph')})[1] == tmp_path / 'ph' / 'data'
    # Fail closed: a store that exists but cannot be read is never treated as "no shared grant".
    broken = tmp_path / 'broken'
    broken.mkdir()
    (broken / 'pebble.sqlite').write_bytes(b'not a database')
    with pytest.raises(ev.EvaluationError, match='installed_app_settings_unreadable'):
        ev.installed_connection_identities([broken])
    assert ev.installed_connection_identities([tmp_path / 'absent']) == {'connection_ids': set(), 'host_ids': set(),
                                                                          'stores': 0}


def test_the_installed_store_is_opened_read_only(fake_home):
    database = fake_home / '.pebble/data/pebble.sqlite'
    before = database.stat().st_mtime_ns, database.read_bytes()
    ev.installed_connection_identities()
    assert (database.stat().st_mtime_ns, database.read_bytes()) == before


# -- the guard ------------------------------------------------------------------------------------------------------

def test_the_guard_refuses_the_installed_apps_connection_and_host(fake_home, tmp_path):
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    installed = ev.installed_connection_identities()
    for profile, host, code in ((APP_PROFILE, APP_HOST, 'installed_app_connection_refused'),
                                ({**EVAL_PROFILE, 'connection_id': APP_PROFILE['connection_id']}, EVAL_HOST,
                                 'installed_app_connection_refused'),
                                ({**EVAL_PROFILE, 'id': APP_PROFILE['id']}, EVAL_HOST, 'installed_app_connection_refused'),
                                (EVAL_PROFILE, APP_HOST, 'installed_app_host_refused')):
        with pytest.raises(ev.EvaluationError, match=code):
            ev.record_evaluation_connection(ledger, profile, host, installed=installed)
        with pytest.raises(ev.EvaluationError, match=code):
            ev.require_evaluation_connection({'profile': profile, 'chatgpt_host_id': host}, ledger)
    assert not (ledger.root / ev.EVALUATION_CONNECTION_FILE).exists()


def test_only_the_recorded_evaluation_connection_runs(fake_home, tmp_path):
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    metadata = {'profile': EVAL_PROFILE, 'chatgpt_host_id': EVAL_HOST}
    with pytest.raises(ev.EvaluationError, match='evaluation_connection_not_recorded'):
        ev.require_evaluation_connection(metadata, ledger)
    with pytest.raises(ev.EvaluationError, match='evaluation_host_id_missing'):
        ev.record_evaluation_connection(ledger, EVAL_PROFILE, None)
    assert ev.record_evaluation_connection(ledger, EVAL_PROFILE, EVAL_HOST) == {'evaluation_connection': 'recorded'}
    assert ev.require_evaluation_connection(metadata, ledger) == {'evaluation_connection': 'separate',
                                                                  'installed_stores_checked': 1}
    with pytest.raises(ev.EvaluationError, match='evaluation_connection_already_recorded'):
        ev.record_evaluation_connection(ledger, EVAL_PROFILE, EVAL_HOST)
    other = {**EVAL_PROFILE, 'id': 'c0ffee' * 5 + 'aa', 'connection_id': 'c0ffee' * 5 + 'aa'}
    for changed in ({'profile': other, 'chatgpt_host_id': EVAL_HOST},
                    {'profile': EVAL_PROFILE, 'chatgpt_host_id': 'urn:uuid:33333333-3333-4333-8333-333333333333'}):
        with pytest.raises(ev.EvaluationError, match='evaluation_connection_mismatch'):
            ev.require_evaluation_connection(changed, ledger)
    with pytest.raises(ev.EvaluationError, match='evaluation_host_id_missing'):
        ev.require_evaluation_connection({'profile': EVAL_PROFILE, 'chatgpt_host_id': None}, ledger)
    # The record is content-free: salted fingerprints, no id, host or e-mail address.
    path = ledger.root / ev.EVALUATION_CONNECTION_FILE
    raw = path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not any(value in raw for value in (EVAL_PROFILE['id'], EVAL_HOST, EVAL_PROFILE['email'], 'oaiapp_'))
    # An edited record is refused, and so is one others can read.
    record = json.loads(raw)
    path.write_text(json.dumps({**record, 'connection': record['profile']}))
    with pytest.raises(ev.EvaluationError, match='evaluation_connection_record_invalid'):
        ev.require_evaluation_connection(metadata, ledger)
    path.write_text(raw)
    os.chmod(path, 0o644)
    with pytest.raises(ev.EvaluationError, match='evaluation_connection_record_invalid'):
        ev.require_evaluation_connection(metadata, ledger)


# -- the CLI: live and block ------------------------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch):
    module = load_cli()
    started = []

    def never(**kwargs):
        started.append(kwargs)
        raise AssertionError('The driver started a live evaluation.')
    monkeypatch.setattr(module, 'run_paired_block', never)
    monkeypatch.setattr(module, 'run_evaluation', never)
    module.started = started
    return module


def test_block_refuses_the_installed_apps_connection_before_any_app(fake_home, tmp_path, cli, capsys):
    """The 0.6.1 driver ran this metadata (the app's own profile) and refreshed the app's grant."""
    plan_file = tmp_path / 'plan.json'
    plan_file.write_text(json.dumps(ev.campaign_plan('pilot')))
    ledger = tmp_path / 'ledger'
    metadata = metadata_file(tmp_path / 'app.json', APP_PROFILE, APP_HOST)
    report = tmp_path / 'b1.json'
    argv = ['block', '--plan', str(plan_file), '--block', '1', '--invocation', '1', '--profile-metadata', str(metadata),
            '--ledger', str(ledger), '--report', str(report), '--confirm-live']
    assert cli.main(argv) == 2
    assert 'installed_app_connection_refused' in capsys.readouterr().err
    assert cli.started == [] and not report.exists()
    # The app's connection under a fresh host id is still the app's grant.
    metadata_file(metadata, APP_PROFILE, EVAL_HOST)
    assert cli.main(argv) == 2
    assert 'installed_app_connection_refused' in capsys.readouterr().err
    # A separate connection that the driver's own sign-in never recorded is refused too.
    metadata_file(metadata, EVAL_PROFILE, EVAL_HOST)
    assert cli.main(argv) == 2
    assert 'evaluation_connection_not_recorded' in capsys.readouterr().err
    assert cli.started == [] and not report.exists()


def test_on_0_6_1_the_same_block_reaches_a_live_run_with_the_apps_connection(fake_home, tmp_path, cli, capsys,
                                                                             monkeypatch):
    """0.6.1's driver had no guard. Modelled by neutralising it, the test above fails: the driver starts a live
    evaluation with the installed app's connection (and would refresh the app's grant)."""
    monkeypatch.setattr(cli, 'require_evaluation_connection', lambda *a, **k: {'evaluation_connection': 'none'})
    with pytest.raises(AssertionError, match='started a live evaluation'):
        test_block_refuses_the_installed_apps_connection_before_any_app(fake_home, tmp_path, cli, capsys)
    assert cli.started and cli.started[0]['profile']['connection_id'] == APP_PROFILE['connection_id']


def test_live_refuses_the_installed_apps_connection_and_runs_the_recorded_one(fake_home, tmp_path, cli, capsys,
                                                                              monkeypatch):
    ledger = tmp_path / 'ledger'
    report = tmp_path / 'live.json'
    argv = ['live', '--profile-metadata', None, '--arm', 'solo', '--ledger', str(ledger), '--report', str(report),
            '--confirm-live']
    argv[2] = str(metadata_file(tmp_path / 'app.json', APP_PROFILE, APP_HOST))
    assert cli.main(argv) == 2
    assert 'installed_app_connection_refused' in capsys.readouterr().err and cli.started == []
    ev.record_evaluation_connection(ev.EvidenceLedger(ledger), EVAL_PROFILE, EVAL_HOST)
    argv[2] = str(metadata_file(tmp_path / 'eval.json', EVAL_PROFILE, EVAL_HOST))
    with pytest.raises(AssertionError, match='started a live evaluation'):
        cli.main(argv)  # the guard passed; the (stubbed) evaluation was reached
    assert cli.started[0]['profile'] == EVAL_PROFILE and cli.started[0]['chatgpt_host_id'] == EVAL_HOST
    # --installed-home names the folders to check explicitly.
    elsewhere = tmp_path / 'elsewhere'
    installed_store(elsewhere, [EVAL_PROFILE], EVAL_HOST)
    cli.started.clear()
    assert cli.main(argv + ['--installed-home', str(elsewhere)]) == 2
    assert 'installed_app_connection_refused' in capsys.readouterr().err and cli.started == []


# -- the sign-in command ------------------------------------------------------------------------------------------

class FakeApp:
    """An owned App for the evaluation home: its sign-in connects a new connection, never the app's."""

    def __init__(self, home, profile=EVAL_PROFILE, host=EVAL_HOST, fail=None):
        self.home, self.closed, self.started = home, False, []
        self.chatgpt = SimpleNamespace(start=self.start, status=self.status, cancel=lambda: None)
        self.model = SimpleNamespace(profiles=lambda: [dict(profile)])
        self.store = SimpleNamespace(setting=lambda key, default=None: host if key == 'chatgpt_host_id' else default)
        self.profile, self.fail = profile, fail

    def start(self, profile_id=None):
        self.started.append(profile_id)
        return {'url': 'https://auth.openai.com/authorize?fixture', 'state': 'waiting'}

    def status(self):
        if self.fail:
            return {'state': 'failed', 'error': self.fail}
        return {'state': 'connected', 'profile_id': self.profile['id'], 'email': 'fixture@example.invalid'}

    def close(self):
        self.closed = True


def sign_in(cli, argv, app):
    return cli._evaluation_connection(cli._parser().parse_args(argv), app_factory=lambda home, resources: app,
                                      sleep=lambda seconds: None)


def test_the_sign_in_records_a_separate_connection_and_the_live_guard_accepts_it(fake_home, tmp_path, cli, capsys):
    ledger, metadata = tmp_path / 'ledger', tmp_path / 'eval.json'
    argv = ['eval-connection', '--home', str(tmp_path / 'eval-home'), '--ledger', str(ledger), '--metadata',
            str(metadata)]
    app = FakeApp(tmp_path / 'eval-home')
    assert sign_in(cli, argv, app) == 2  # the person's go-ahead is required
    assert app.started == [] and not metadata.exists()
    capsys.readouterr()
    assert sign_in(cli, argv + ['--confirm-sign-in'], app) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {'evaluation_connection': 'recorded', 'installed_stores_checked': 1}
    assert app.started == [None] and app.closed  # a new connection: never start(<the app's profile>)
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o600
    loaded = ev.load_live_metadata(metadata)
    assert loaded == {'profile': EVAL_PROFILE, 'chatgpt_host_id': EVAL_HOST}
    assert ev.require_evaluation_connection(loaded, ev.EvidenceLedger(ledger))['evaluation_connection'] == 'separate'
    # Reconnecting signs in the same recorded connection again (start with its profile id) and writes nothing new.
    again = FakeApp(tmp_path / 'eval-home')
    assert sign_in(cli, argv + ['--confirm-sign-in', '--reconnect'], again) == 0
    assert json.loads(capsys.readouterr().out)['evaluation_connection'] == 'reconnected'
    assert again.started == [EVAL_PROFILE['id']]
    # A second new sign-in into the same ledger or metadata path is refused.
    assert cli.main(argv + ['--confirm-sign-in']) == 2
    assert 'evaluation_metadata_exists' in capsys.readouterr().err


def test_the_sign_in_refuses_the_apps_home_and_a_connection_the_app_holds(fake_home, tmp_path, cli, capsys):
    ledger, metadata = tmp_path / 'ledger', tmp_path / 'eval.json'
    for home in (fake_home / '.pebble/data', fake_home / '.pebble/data/sub', fake_home / '.pebble'):
        argv = ['eval-connection', '--home', str(home), '--ledger', str(ledger), '--metadata', str(metadata),
                '--confirm-sign-in']
        app = FakeApp(home)
        with pytest.raises(ev.EvaluationError, match='evaluation_home_is_installed_home'):
            sign_in(cli, argv, app)
        assert app.started == []
    argv = ['eval-connection', '--home', str(tmp_path / 'eval-home'), '--ledger', str(ledger), '--metadata',
            str(metadata), '--confirm-sign-in']
    # Signing in returned the app's own account connection (for example a reused profile): nothing is recorded.
    with pytest.raises(ev.EvaluationError, match='installed_app_connection_refused'):
        sign_in(cli, argv, FakeApp(tmp_path / 'eval-home', profile=APP_PROFILE))
    assert not (ledger / ev.EVALUATION_CONNECTION_FILE).exists() and not metadata.exists()
    assert sign_in(cli, argv, FakeApp(tmp_path / 'eval-home', fail='Sign-in expired. Try again.')) == 2
    assert 'Sign-in expired' in capsys.readouterr().err
    assert not metadata.exists() and not (ledger / ev.EVALUATION_CONNECTION_FILE).exists()


def test_the_auth_stop_message_names_the_evaluation_connection(cli):
    assert 'evaluation connection' in cli.AUTH_STOPPED and 'installed app' in cli.AUTH_STOPPED
    assert 'Sign in again in the app' not in Path(cli.__file__).read_text()
