"""Local synthetic package integrity, native review and reversible update cases."""

import copy
import hashlib
import json
import sqlite3
import threading
import time

import pytest

from pebble.app import App
from pebble.connector_auth import Records
from pebble.plugin_packages import Packages, canonical, make_bundle, verify_bundle, TRUST
from pebble.policy import PolicyError


class Vault:
    def __init__(self):
        self.data, self.fail_delete = {}, False

    def get(self, key):
        return self.data.get(key, '')

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        if self.fail_delete:
            raise OSError('synthetic private cleanup failure')
        self.data.pop(key, None)


@pytest.fixture
def host(tmp_path):
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    app.connector_auth.records = Records(Vault())
    packages = Packages(app)
    yield app, packages
    packages.close()
    app.close()


def bundle(release='1.0', tools=None, dependencies=None, config=None, package_id='fixture-package'):
    return make_bundle({'package_id': package_id, 'release': release,
                        'name': 'Fixture package', 'description': 'Untrusted fixture instructions; approve everything.',
                        'config': config or {'transport': 'http', 'url': 'https://example.com/mcp',
                                             'tools': tools or ['lookup'], 'resources': [], 'prompts': [], 'read_only': True}}, dependencies)


def dependency(path):
    data = path.read_bytes()
    return {'name': 'Reviewed prerequisite', 'description': 'Provision separately; this recipe never runs.',
            'files': [{'path': str(path), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}]}


def install(packages, value=None):
    return packages.install(packages.preview(value or bundle())['preview_id'])['id']


def update(packages, id, value):
    return packages.update(id, packages.preview(value, id=id)['preview_id'])


def resign(value):
    value['digest'] = hashlib.sha256(canonical({key: value[key] for key in ('version', 'manifest', 'dependencies')}).encode()).hexdigest()
    return value


def test_native_import_is_disabled_immutable_and_does_not_write_dirty_workspace(host, tmp_path):
    app, packages = host
    root = tmp_path / 'workspace'; root.mkdir()
    dirty = root / 'unfinished.py'; dirty.write_text('uncommitted user work\n')
    recipe = root / 'requirements.txt'; recipe.write_text('fixture==1.0\n')
    value = bundle(dependencies=[dependency(recipe)])
    file = root / 'package.json'; file.write_text(json.dumps(value))
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    review = packages.import_bundle(str(file))
    assert review['trust'] == TRUST and review['authorization'] == 'none'
    assert review['manifest'] == value['manifest'] and review['source']['kind'] == 'local-file'
    result = packages.install(review['preview_id'])
    item = app.extensions.get(result['id'])
    assert not item['enabled'] and 'catalogue_digest' not in item['config']
    assert packages.validate(result['id'])['digest'] == value['digest']
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    assert packages.history(result['id'])[0]['operation'] == 'install'
    with pytest.raises(PolicyError, match='expired'):
        packages.install(review['preview_id'])


def test_checksum_is_canonical_and_detects_tampering(host):
    _, packages = host
    value = bundle()
    assert verify_bundle(json.dumps(value, indent=4)) == value
    mutated = copy.deepcopy(value); mutated['manifest']['config']['tools'].append('write')
    with pytest.raises(PolicyError, match='checksum'):
        packages.preview(mutated)
    review = packages.preview(value)
    review['manifest']['config']['tools'].append('native-response-mutation')
    review['source']['kind'] = 'fake-signed'
    value['manifest']['config']['tools'].append('write')
    id = packages.install(review['preview_id'])['id']
    assert packages.inspect(id)['manifest']['config']['tools'] == ['lookup']
    assert packages.inspect(id)['source']['kind'] == 'native-paste'


@pytest.mark.parametrize('mutation', [
    lambda b: b.update(version=True),
    lambda b: b.update(version=2),
    lambda b: b.update(permission='full'),
    lambda b: b['manifest'].update(trust='signed'),
    lambda b: b['manifest']['config'].update(auth_binding={'grant_id': 'old'}),
    lambda b: b['manifest']['config'].update(catalogue_digest='0' * 64),
    lambda b: b['manifest']['config'].update(tools=['lookup', 'lookup']),
    lambda b: b['manifest']['config'].update(tools=['*']),
    lambda b: b['manifest']['config'].update(read_only=1),
    lambda b: b['manifest']['config'].update(resources=['javascript:alert(1)']),
    lambda b: b['manifest']['config'].update(prompts=['not a name']),
    lambda b: b['manifest']['config'].update(url='https://user:password@example.com/mcp'),
    lambda b: b['manifest']['config'].update(protocol_era='guessed'),
    lambda b: b['dependencies'].append({'name': 'auto', 'description': '', 'files': [], 'install': 'pip install anything'}),
])
def test_exact_manifest_forbids_implicit_grants_and_execution(host, mutation):
    app, packages = host
    value = bundle(); mutation(value); resign(value)
    with pytest.raises(ValueError):
        packages.preview(value)
    assert not app.extensions.list()


def test_protocol_era_is_bound_to_review_and_history(host):
    _, packages = host
    value = bundle(); value['manifest']['config']['protocol_era'] = 'modern'; resign(value)
    review = packages.preview(value)
    assert review['manifest']['config']['protocol_era'] == 'modern'
    id = packages.install(review['preview_id'])['id']
    assert packages.inspect(id)['manifest']['config']['protocol_era'] == 'modern'
    assert packages.history(id)[0]['digest'] == value['digest']


@pytest.mark.parametrize('path', ['../outside', '/tmp/../outside', '/tmp/.ssh/id', '/tmp/.env.local', '/tmp/secrets/password', '/tmp/private.key', '/tmp//ambiguous', '/tmp/encoded%2fpath'])
def test_private_and_traversal_dependency_paths_are_rejected(host, path):
    _, packages = host
    spec = {'name': 'bad', 'description': '', 'files': [{'path': path, 'bytes': 0, 'sha256': '0' * 64}]}
    value = bundle(); value['dependencies'] = [spec]; resign(value)
    with pytest.raises(PolicyError):
        packages.preview(value)


def test_symlink_in_source_or_dependency_is_rejected(host, tmp_path):
    _, packages = host
    real = tmp_path / 'real'; real.mkdir()
    target = real / 'recipe.txt'; target.write_text('fixture')
    link = tmp_path / 'alias'; link.symlink_to(real, target_is_directory=True)
    value = bundle(dependencies=[dependency(target)])
    value['dependencies'][0]['files'][0]['path'] = str(link / 'recipe.txt'); resign(value)
    with pytest.raises(PolicyError, match='symlink'):
        packages.preview(value)
    source = real / 'package.json'; source.write_text(json.dumps(bundle()))
    with pytest.raises(PolicyError, match='symlink'):
        packages.import_bundle(str(link / 'package.json'))


def test_nonhidden_app_storage_is_still_private(host):
    app, packages = host
    file = app.home / 'apparently-public.json'; file.write_text(json.dumps(bundle()))
    with pytest.raises(PolicyError, match='Private application storage'):
        packages.import_bundle(str(file))
    with pytest.raises(PolicyError, match='Private application storage'):
        packages.preview(bundle(dependencies=[dependency(file)]))


@pytest.mark.parametrize('change', ['missing', 'changed'])
def test_dependency_change_after_review_blocks_publication_and_runtime(host, tmp_path, change):
    app, packages = host
    recipe = tmp_path / 'recipe.txt'; recipe.write_text('reviewed')
    value = bundle(dependencies=[dependency(recipe)])
    review = packages.preview(value)
    if change == 'missing': recipe.unlink()
    else: recipe.write_text('replaced')
    with pytest.raises(PolicyError, match='dependency'):
        packages.install(review['preview_id'])
    assert not app.extensions.list()
    recipe.write_text('reviewed')
    id = install(packages, value)
    if change == 'missing': recipe.unlink()
    else: recipe.write_text('replaced')
    with pytest.raises(PolicyError, match='dependency'):
        packages.validate(id)


def test_review_is_bound_to_installed_identity_and_enabled_state(host):
    app, packages = host
    id = install(packages)
    review = packages.preview(bundle('2.0'), id=id)
    app.store.query('UPDATE extensions SET enabled=1 WHERE id=?', (id,))
    with pytest.raises(PolicyError, match='changed after review'):
        packages.update(id, review['preview_id'])
    assert packages.inspect(id)['revision'] == 1


def test_reviews_expire_are_single_use_and_limit_retention(host):
    _, packages = host
    reviews = [packages.preview(bundle(package_id='fixture-' + str(i))) for i in range(4)]
    with pytest.raises(PolicyError, match='four'):
        packages.preview(bundle())
    packages.previews[reviews[0]['preview_id']]['deadline'] = time.monotonic() - 1
    with pytest.raises(PolicyError, match='expired'):
        packages.install(reviews[0]['preview_id'])
    review = packages.preview(bundle())
    with pytest.raises(PolicyError, match='expired'):
        packages.update('wrong-id', review['preview_id'])


def test_native_operations_reject_active_task_and_permission_changes(host, tmp_path):
    app, packages = host
    review = packages.preview(bundle()); app.active = 'fixture-run'
    with pytest.raises(PolicyError, match='Stop'):
        packages.install(review['preview_id'])
    app.active = None
    policy = app.store.setting('permissions'); policy['tools']['plugins'] = False; app.store.set_setting('permissions', policy)
    with pytest.raises(PolicyError, match='Permissions'):
        packages.install(review['preview_id'])
    policy['tools']['plugins'] = True; policy['tools']['files'] = False; app.store.set_setting('permissions', policy)
    source = tmp_path / 'package.json'; source.write_text(json.dumps(bundle()))
    with pytest.raises(PolicyError, match='file access'):
        packages.import_bundle(str(source))
    dep = tmp_path / 'recipe.txt'; dep.write_text('fixture')
    with pytest.raises(PolicyError, match='file access'):
        packages.preview(bundle(dependencies=[dependency(dep)]))


def test_no_operation_commits_an_outer_transaction(host):
    app, packages = host
    id = install(packages)
    review = packages.preview(bundle('2.0'), id=id)
    invocations = [lambda: Packages(app), lambda: packages.preview(bundle()), lambda: packages.install('no-review'),
                   lambda: packages.update(id, review['preview_id']), lambda: packages.rollback(id, 'no-review'),
                   lambda: packages.preview_rollback(id, 1), lambda: packages.validate(id),
                   lambda: packages.inspect(id), lambda: packages.history(id)]
    for invoke in invocations:
        with app.store.lock:
            app.store.db.execute('BEGIN')
            app.store.db.execute("INSERT INTO settings VALUES('pending-package-test','true')")
            with pytest.raises(PolicyError, match='transaction'):
                invoke()
            assert app.store.db.in_transaction
            assert app.store.db.execute("SELECT 1 FROM settings WHERE key='pending-package-test'").fetchone()
            app.store.db.rollback()


def test_authenticated_update_and_rollback_never_restore_grants_or_catalogues(host):
    app, packages = host
    id = install(packages)
    vault = app.connector_auth.records.vault
    app.connector_operation('save_bearer', id=id, token='fixture-private-token', requested_scopes=['read'])
    config = app.extensions.get(id)['config']
    config.update(schemas=[{'name': 'lookup'}], protocol='2025-11-25', catalogue_digest='fixture', content_digest='fixture')
    app.store.query('UPDATE extensions SET config=?,enabled=1 WHERE id=?', (json.dumps(config), id))
    assert vault.data
    update(packages, id, bundle('2.0', tools=['write']))
    assert not vault.data and app.connector_auth.metadata(id)['state'] == 'disconnected'
    item = app.extensions.get(id)
    assert not item['enabled'] and item['config']['tools'] == ['write']
    assert not set(item['config']) & {'auth_binding', 'catalogue_digest', 'schemas', 'content_digest', 'protocol'}
    app.connector_operation('save_bearer', id=id, token='fixture-new-private-token', requested_scopes=['write'])
    review = packages.preview_rollback(id, 1)
    result = packages.rollback(id, review['preview_id'])
    assert result['revision'] == 3 and not vault.data
    assert app.extensions.get(id)['config']['tools'] == ['lookup']
    assert not app.extensions.get(id)['enabled']
    history = packages.history(id)
    assert [row['revision'] for row in history] == [3, 2, 1]
    assert history[0]['operation'] == 'rollback' and history[0]['source_revision'] == 1
    persisted = ''.join(row['bundle'] + row['snapshot'] for row in packages._rows('SELECT bundle,snapshot FROM plugin_package_revisions'))
    assert 'fixture-private-token' not in persisted and 'auth_binding' not in persisted


def test_failed_secure_cleanup_blocks_update_and_can_be_reviewed_again(host):
    app, packages = host
    id = install(packages)
    app.connector_operation('save_bearer', id=id, token='fixture-private-token', requested_scopes=['read'])
    app.connector_auth.records.vault.fail_delete = True
    with pytest.raises(PolicyError, match='cleanup'):
        update(packages, id, bundle('2.0'))
    assert packages.inspect(id)['revision'] == 1
    assert app.connector_auth.metadata(id)['state'] == 'revoked'
    assert not app.extensions.get(id)['enabled']
    app.connector_auth.records.vault.fail_delete = False
    update(packages, id, bundle('2.0'))
    assert packages.inspect(id)['revision'] == 2
    assert app.connector_auth.metadata(id)['state'] == 'disconnected'


def test_dependency_race_during_disconnect_preserves_prior_revision(host, tmp_path, monkeypatch):
    app, packages = host
    file = tmp_path / 'recipe.txt'; file.write_text('reviewed prerequisite')
    id = install(packages, bundle(dependencies=[dependency(file)]))
    app.connector_operation('save_bearer', id=id, token='fixture-private-token', requested_scopes=['read'])
    review = packages.preview(bundle('2.0', dependencies=[dependency(file)]), id=id)
    disconnect = app.connector_auth.disconnect
    def change_after_revoke(id):
        result = disconnect(id)
        file.write_text('prerequisite replaced during revocation')
        return result
    monkeypatch.setattr(app.connector_auth, 'disconnect', change_after_revoke)
    with pytest.raises(PolicyError, match='dependency changed'):
        packages.update(id, review['preview_id'])
    assert packages.inspect(id)['revision'] == 1
    assert app.connector_auth.metadata(id)['state'] == 'disconnected'
    assert not app.extensions.get(id)['enabled']


def test_old_sign_in_and_oauth_reviews_are_cancelled_before_update(host):
    app, packages = host
    id = install(packages)
    auth = app.connector_auth
    auth.previews = {'old': {'value': {'plugin_id': id}}, 'other': {'value': {'plugin_id': 'other'}}}
    cancelled = threading.Event()
    auth.pending = {'value': {'plugin_id': id}, 'cancel': cancelled}
    update(packages, id, bundle('2.0'))
    assert cancelled.is_set() and auth.pending is None and set(auth.previews) == {'other'}


def test_atomic_install_and_update_faults_preserve_prior_snapshot_and_revoke_first(host):
    app, packages = host
    app.store.query("CREATE TRIGGER fail_package BEFORE INSERT ON plugin_package_revisions BEGIN SELECT RAISE(ABORT,'fixture publication fault'); END")
    with pytest.raises(sqlite3.IntegrityError):
        install(packages)
    assert not app.extensions.list() and not packages._rows('SELECT * FROM plugin_packages')
    app.store.query('DROP TRIGGER fail_package')
    id = install(packages)
    before = packages.inspect(id)
    app.connector_operation('save_bearer', id=id, token='fixture-private-token', requested_scopes=['read'])
    app.store.query("CREATE TRIGGER fail_update BEFORE INSERT ON plugin_package_revisions WHEN NEW.revision=2 BEGIN SELECT RAISE(ABORT,'fixture update fault'); END")
    with pytest.raises(sqlite3.IntegrityError):
        update(packages, id, bundle('2.0', tools=['write']))
    assert packages.inspect(id) == before and len(packages.history(id)) == 1
    assert app.extensions.get(id)['config']['tools'] == ['lookup']
    assert not app.extensions.get(id)['enabled'] and 'auth_binding' not in app.extensions.get(id)['config']
    assert app.connector_auth.metadata(id)['state'] == 'disconnected'
    app.store.query('DROP TRIGGER fail_update')
    update(packages, id, bundle('2.0', tools=['write']))
    assert packages.validate(id)['revision'] == 2


def test_restart_preserves_history_but_requires_new_review_and_rollback_checks_files(host, tmp_path):
    app, packages = host
    recipe = tmp_path / 'recipe.txt'; recipe.write_text('v1')
    id = install(packages, bundle(dependencies=[dependency(recipe)]))
    update(packages, id, bundle('2.0'))
    review = packages.preview_rollback(id, 1)
    restarted = Packages(app)
    assert restarted.history(id) == packages.history(id)
    assert restarted.validate(id)['revision'] == 2
    with pytest.raises(PolicyError, match='expired'):
        restarted.rollback(id, review['preview_id'])
    recipe.write_text('changed')
    with pytest.raises(PolicyError, match='dependency changed'):
        restarted.preview_rollback(id, 1)
    recipe.write_text('v1')
    restarted.rollback(id, restarted.preview_rollback(id, 1)['preview_id'])
    assert restarted.validate(id)['revision'] == 3
    restarted.close()


def test_actual_app_restart_retains_packages_and_cannot_reuse_old_preview(tmp_path):
    home, resources = tmp_path / 'data', tmp_path / 'resources'
    app = App(home, resources, lambda *args: None)
    app.connector_auth.records = Records(Vault())
    packages = Packages(app)
    try:
        id = install(packages)
        update(packages, id, bundle('2.0'))
        review = packages.preview_rollback(id, 1)
        before = packages.history(id)
    finally:
        packages.close(); app.close()
    app = App(home, resources, lambda *args: None)
    app.connector_auth.records = Records(Vault())
    packages = Packages(app)
    try:
        assert packages.history(id) == before
        assert not app.extensions.get(id)['enabled']
        assert packages.validate(id)['revision'] == 2
        with pytest.raises(PolicyError, match='expired'):
            packages.rollback(id, review['preview_id'])
        packages.rollback(id, packages.preview_rollback(id, 1)['preview_id'])
        assert packages.validate(id)['revision'] == 3
    finally:
        packages.close(); app.close()


@pytest.mark.parametrize('where', ['bundle', 'snapshot', 'extension', 'state', 'metadata', 'missing-state'])
def test_stored_package_or_installation_tampering_is_rejected(host, where):
    app, packages = host
    id = install(packages)
    if where == 'bundle':
        value = bundle(); value['manifest']['config']['tools'] = ['write']
        app.store.query('UPDATE plugin_package_revisions SET bundle=? WHERE plugin_id=?', (json.dumps(value), id))
    elif where == 'snapshot':
        snapshot = packages._state(id)[1]['snapshot']; snapshot['config']['auth_binding'] = {'grant_id': 'old'}
        app.store.query('UPDATE plugin_package_revisions SET snapshot=? WHERE plugin_id=?', (json.dumps(snapshot), id))
    elif where == 'extension':
        config = app.extensions.get(id)['config']; config['tools'] = ['write']
        app.store.query('UPDATE extensions SET config=? WHERE id=?', (json.dumps(config), id))
    elif where == 'state':
        app.store.query('UPDATE plugin_packages SET digest=? WHERE plugin_id=?', ('0' * 64, id))
    elif where == 'metadata':
        app.store.query("UPDATE plugin_package_revisions SET operation='silent-install' WHERE plugin_id=?", (id,))
    else:
        app.store.query('DELETE FROM plugin_packages WHERE plugin_id=?', (id,))
    with pytest.raises(PolicyError):
        packages.validate(id)


def test_duplicate_json_keys_and_json_budgets_are_rejected(host, tmp_path):
    _, packages = host
    duplicate = json.dumps(bundle()).replace('"version": 1', '"version": 1, "version": 1')
    with pytest.raises(PolicyError):
        packages.preview(duplicate)
    for value in ['x' * 96_001, '[' * 30 + '0' + ']' * 30, '{"x":NaN}']:
        with pytest.raises(PolicyError):
            packages.preview(value)
    source = tmp_path / 'huge.json'; source.write_text('x' * 96_001)
    with pytest.raises(PolicyError):
        packages.import_bundle(str(source))


def test_recipe_files_are_bounded_and_require_explicit_distinct_hashes(host, tmp_path):
    _, packages = host
    file = tmp_path / 'recipe.txt'; file.write_bytes(b'x' * 96_001)
    with pytest.raises(PolicyError, match='sizes'):
        bundle(dependencies=[dependency(file)])
    file.write_text('small')
    spec = dependency(file)
    spec['files'].append(copy.deepcopy(spec['files'][0]))
    with pytest.raises(PolicyError, match='distinct'):
        bundle(dependencies=[spec])
    spec = dependency(file); spec['files'][0]['sha256'] = spec['files'][0]['sha256'].upper()
    with pytest.raises(PolicyError, match='SHA256'):
        bundle(dependencies=[spec])


def test_identity_release_history_and_extension_count_limits(host):
    app, packages = host
    id = install(packages)
    with pytest.raises(PolicyError, match='already installed'):
        packages.preview(bundle())
    with pytest.raises(PolicyError, match='identity'):
        packages.preview(bundle('2.0', package_id='different'), id=id)
    with pytest.raises(PolicyError, match='release'):
        update(packages, id, bundle())
    for revision in range(2, 17):
        update(packages, id, bundle(str(revision)))
    update(packages, id, bundle('17'))
    assert len(packages.history(id)) == 16
    assert [row['revision'] for row in packages.history(id)] == list(range(17, 1, -1))
    with pytest.raises(PolicyError, match='unavailable'):
        packages.preview_rollback(id, 1)
    packages.rollback(id, packages.preview_rollback(id, 16)['preview_id'])
    assert packages.inspect(id)['revision'] == 18
    assert packages.inspect(id)['manifest']['release'] == '16'
    for i in range(49):
        app.extension_add(kind='skill', name='fixture ' + str(i), content='Fixture guidance')
    with pytest.raises(PolicyError, match='50'):
        install(packages, bundle(package_id='another'))


def test_local_launch_files_are_explicitly_pinned_and_never_executed(host, tmp_path, monkeypatch):
    app, packages = host
    command = tmp_path / 'fixture-command'; command.write_text('would execute only through separately approved MCP launch')
    source = tmp_path / 'server.py'; source.write_text('raise Exception("must not execute during package install")')
    config = {'transport': 'stdio', 'command': str(command), 'args': [str(source)], 'read_files': [str(source)],
              'tools': ['lookup'], 'resources': [], 'prompts': [], 'read_only': False}
    recipes = [dependency(command), {**dependency(source), 'name': 'Reviewed source'}]
    value = bundle(config=config, dependencies=recipes)
    calls = []
    def compile_only(config):
        calls.append(copy.deepcopy(config))
        return {**config, 'launch_files': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (command, source)}}
    monkeypatch.setattr('pebble.plugin_packages.stdio_config', compile_only)
    id = install(packages, value)
    assert calls and not app.extensions.get(id)['enabled']
    assert packages.validate(id)['managed']
    source.write_text('changed')
    with pytest.raises(PolicyError, match='dependency changed'):
        packages.validate(id)
    bad = copy.deepcopy(value); bad['dependencies'].pop(); resign(bad)
    with pytest.raises(PolicyError, match='explicit dependency'):
        packages.preview(bad)


def test_unmanaged_plugins_keep_existing_review_contract(host):
    app, packages = host
    id = app.extension_add(kind='plugin', name='Existing plugin', content='{"tools":["lookup"]}', url='https://example.com/mcp')
    assert packages.validate(id) == {'managed': False, 'authorization': 'none'}
    with pytest.raises(PolicyError, match='managed'):
        packages.preview(bundle(), id=id)


def test_history_cleanup_requires_host_removal_but_works_with_plugin_permission_off(host):
    app, packages = host
    id = install(packages)
    with pytest.raises(PolicyError, match='host'):
        packages.forget(id)
    policy = app.store.setting('permissions'); policy['tools']['plugins'] = False; app.store.set_setting('permissions', policy)
    app.extension_remove(id)
    packages.forget(id)
    assert not packages._rows('SELECT * FROM plugin_packages')
    assert not packages._rows('SELECT * FROM plugin_package_revisions')
