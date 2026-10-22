"""Phase 20 merge (i20): the parent patches hold together on the merged source.

The track tests cover the tracks' own modules (test_rewind, test_procedural_recipes_20, test_diagnostics,
test_research_stopping, test_attestation, test_context_reductions_20); these cover the parent seams the tracks
could not reach from their copies: the rewind write wording (G20-rewind P3), the manifest binding recorded at
proposal and re-checked before launch (G20-recipes P1 and P2), the coverage note at the runner's tool boundary
(G20-research P1 and P2), the contract v2 command line (D20-trust P2), the phase20 verifier namespace
(d20-faults2 P4), T1's manifests read by T2, T3 and T5, and T3's release manifest through T2's install check.

No model inference and no network: every model turn is a local fixture function.
"""
import copy
import importlib.util
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import recipes, release_readiness as rr, research
from pebble.app import App
from pebble.policy import PolicyError, Workspace
from pebble.testing import run_tests

ROOT = Path(__file__).resolve().parents[2]
macos = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox')
PACKAGE = '{"name":"fixture","scripts":{"build":"tsc -p .","test":"node --test"}}\n'
LOCK = '{"name":"fixture","lockfileVersion":3,"packages":{"":{"name":"fixture"}}}\n'
NODE_PASS = "const test = require('node:test');\nconst assert = require('node:assert/strict');\ntest('adds', () => { assert.equal(1 + 1, 2); });\n"
WIDE = {'id': 'wide', 'kind': 'openai', 'name': 'Wide', 'url': 'https://unused.invalid', 'model': 'fixture',
        'context': 32768, 'budget': 16384, 'tools': True}
MID = {**WIDE, 'id': 'mid', 'name': 'Mid', 'context': 16384, 'budget': 8192}
HARBOUR = ('Who is the current harbour master of Port Ellery?',
           "In what year was Port Ellery's north breakwater completed?")


@pytest.fixture
def app(tmp_path):
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    value.model.complete = lambda *args, **kwargs: pytest.fail('No model inference here.')
    yield value
    value.active = value.active_run = None
    value.close()


# G20-rewind P3: each rewind write's conversation message ------------------------------------------------------

def test_rewind_writes_say_what_happened_and_ordinary_edits_keep_their_wording():
    tag = {'rewind_id': 'r' * 32, 'run_id': 'a' * 32, 'previous_step': 2}
    assert App.action_summary('edit', {'path': 'a.txt', 'rewind': {**tag, 'step': 0}}, {'applied': 'a.txt'}) == (
        'Rewound `a.txt` to how it was before that task. Commands and other actions were not undone.')
    assert App.action_summary('edit', {'path': 'a.txt', 'rewind': {**tag, 'step': 1}}, {'applied': 'a.txt'}) == (
        'Rewound `a.txt` to how it was after step 1 of that task. Commands and other actions were not undone.')
    for payload in ({'path': 'a.txt'}, {'path': 'a.txt', 'rewind': 'not a tag'}):
        assert App.action_summary('edit', payload, {'applied': 'a.txt'}) == (
            'Applied the edit to `a.txt`. You can undo it in Activity & review.')


# G20-recipes P1 and P2: the manifests under review are recorded, and the ones that run are the ones reviewed -----

def propose_recipe(app, root, monkeypatch):
    plan = {'recipe': 'npm-build', 'kind': 'build', 'argv': ['/opt/node/bin/node', 'npm-cli.js', 'run', 'build']}
    monkeypatch.setattr(recipes, 'resolve', lambda recipe_id, ws, mount=None: dict(plan))
    chat = app.new_chat(str(root))
    rid = app.runs.create(chat['id'], 'Build the project.', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    execution, token = app.controller.enter(app.runs.get(rid))
    app.active, app.active_run = chat['id'], rid
    try:
        result = app.execute('RunRecipe', {'recipe': 'npm-build'}, Workspace(root), chat['id'], call_id='build')
    finally:
        app.active = app.active_run = None
        app.controller.leave(execution, token)
    assert result['status'] == 'awaiting_user_approval'
    return next(a for a in app.store.actions(chat['id']) if a['id'] == result['action_id'])


def test_a_recipe_proposal_records_the_manifests_and_lockfiles_under_review(app, tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'package.json').write_text(PACKAGE)
    (root / 'package-lock.json').write_text(LOCK)
    action = propose_recipe(app, root, monkeypatch)
    bound = action['payload']['bound_files']
    assert bound == recipes.bindings('npm-build', Workspace(root))[0]
    assert sorted(bound) == ['package-lock.json', 'package.json'] and all(len(value) == 64 for value in bound.values())
    assert action['status'] == 'pending' and action['payload']['requires_review'] is not None


def test_an_unbindable_lockfile_leaves_the_recipe_reviewable_but_unbound(app, tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'package.json').write_text(PACKAGE)
    (root / 'package-lock.json').symlink_to(root / 'package.json')  # a link cannot be bound
    action = propose_recipe(app, root, monkeypatch)
    assert action['payload']['bound_files'] is None and action['status'] == 'pending'


@macos
def test_a_manifest_or_lockfile_changed_after_review_refuses_the_run_before_launch(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'package.json').write_text(PACKAGE)
    (root / 'math.test.js').write_text(NODE_PASS)
    ws = Workspace(root)
    payload = {'root': str(ws.root), 'root_identity': list(ws.identity), 'recipe': 'npm-test', 'drafts': [],
               'plan': recipes.resolve('npm-test', ws), 'mount': None,
               'bound_files': recipes.bindings('npm-test', ws)[0]}
    # Positive control: the reviewed binding still holds, so the recipe runs as before.
    assert run_tests(json.loads(json.dumps(payload)), threading.Event())['outcome'] == 'passed'
    (root / 'package-lock.json').write_text(LOCK)  # a lockfile appearing after review changes the binding
    with pytest.raises(PolicyError, match='A project manifest or lockfile changed after review'):
        run_tests(json.loads(json.dumps(payload)), threading.Event())
    # Payloads without a recorded binding (earlier actions, other callers) are unchanged.
    legacy = {key: value for key, value in payload.items() if key != 'bound_files'}
    assert run_tests(json.loads(json.dumps(legacy)), threading.Event())['outcome'] == 'passed'


# G20-research P1 and P2: the coverage note after research steps, above 8K only ----------------------------------

def research_run(app, monkeypatch, profile):
    seen = []
    script = [('ResearchPlan', {'questions': json.dumps(list(HARBOUR))}), ('ResearchStatus', {}),
              ('ResearchStatus', {})]

    def complete(messages, tools, cancel, token, effort):
        seen.append(copy.deepcopy(messages))
        step = len(seen) - 1
        if step < len(script):
            name, args = script[step]
            return {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': f'r{step}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
        token('I could not verify either answer from page sources.')
        return {'role': 'assistant', 'content': 'I could not verify either answer from page sources.'}

    monkeypatch.setattr(app.model, 'complete', complete)
    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(profile))
    chat = app.new_chat()
    app.work(chat, 'Research Port Ellery and cite sources: ' + ' '.join(HARBOUR), False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    notes = [json.loads(row['payload']) for row in app.store.query(
        "SELECT payload FROM run_events WHERE run_id=? AND kind='research_progress' ORDER BY rowid", (rid,))]
    shown = [[m['content'] for m in request if m.get('name') == 'pebble_research'] for request in seen]
    return seen, shown, notes


def test_a_large_planned_run_carries_one_replaced_coverage_note(app, monkeypatch):
    seen, shown, notes = research_run(app, monkeypatch, WIDE)
    assert len(seen) >= 4
    assert shown[0] == [] and all(len(texts) == 1 for texts in shown[1:4])
    # Three notes were written, yet each request carries one: the note is replaced, never accumulated.
    assert all(texts[0].startswith(research.PROGRESS_HEAD) for texts in shown[1:4])
    assert len(notes) == 3 and all(set(note) == {'recommendation', 'all_covered', 'covered', 'uncovered',
                                                 'open_conflicts'} for note in notes)
    assert all(note['recommendation'] in ('stop', 'continue') and note['open_conflicts'] == 0 for note in notes)
    assert 'http' not in json.dumps(notes) and 'http' not in json.dumps(shown)


def test_no_coverage_note_at_8k_or_below(app, monkeypatch):
    seen, shown, notes = research_run(app, monkeypatch, MID)
    assert seen and all(texts == [] for texts in shown) and notes == []


# D20-trust P2: the contract v2 command line ------------------------------------------------------------------------

def load_script(name):
    spec = importlib.util.spec_from_file_location(name + '_under_merge', ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cli():
    return load_script('check_harness_release')


def test_the_release_check_takes_attestations_only_for_a_contract_v2_review(tmp_path, capsys):
    cli = load_cli()
    fixture = rr.build_fixture(tmp_path / 'evidence')
    envelope, evidence = tmp_path / 'envelope.json', tmp_path / 'evidence.json'
    envelope.write_bytes(b'{}')
    evidence.write_bytes(b'{}')
    base = ['--review', str(fixture['review']), '--reports-root', str(fixture['reports_root'])]
    attested = ['--attestation', 'installed_application', str(envelope), str(evidence)]
    with pytest.raises(SystemExit) as usage:
        cli.main(base + attested + ['--record-attestations'])
    assert usage.value.code == 2
    capsys.readouterr()
    # A v1 review with an attestation is malformed evidence; without one it is the unchanged v1 result.
    assert cli.main(base + attested) == rr.EXIT_MALFORMED
    printed = json.loads(capsys.readouterr().out)
    assert printed['contract_version'] == 1 and 'attestation_requires_contract_v2' in {
        item['code'] for item in printed['malformed']}
    plain = cli.main(base)
    v1 = json.loads(capsys.readouterr().out)
    assert plain == v1['exit_code'] != rr.EXIT_MALFORMED and v1['contract_version'] == 1 and 'attestations' not in v1
    # A v2 review: with the shipped empty roots, every attestable gate stays unverified and nothing is recorded.
    review = json.loads(Path(fixture['review']).read_text())
    Path(fixture['review']).write_text(json.dumps(dict(review, readiness_contract=rr.CONTRACT_V2), indent=2) + '\n')
    ledger = tmp_path / 'ledger'
    assert cli.main(base + attested + ['--attestation-ledger', str(ledger)]) == v1['exit_code']
    v2 = json.loads(capsys.readouterr().out)
    assert v2['contract_version'] == 2 and v2['production_ready'] is False and not ledger.exists()
    for gate in ('installed_application', 'real_model_quality', 'independent_human_audit', 'signed_updates'):
        assert v2['gates'][gate]['status'] == 'unverified', gate


# d20-faults2 F8-2, as merged: a status read never waits on a write, and still expires when the database is free ----

def test_a_status_read_expires_when_free_and_never_waits_under_a_held_lock(app):
    """The time-aware upgrade validators (phase15/16/17/19-upgrade) start the service, make one `state` call and
    expect expired memories gone; under another connection's write lock (F8-2) the same read answers at once."""
    import sqlite3
    import time
    expired = app.memory.put('temporal', 'A synthetic fact that has expired.', expires=time.time() + 60)
    kept = app.memory.put('temporal', 'A synthetic fact that still holds.', expires=time.time() + 3600)
    app.store.query('UPDATE memories SET expires=? WHERE id=?', (time.time() - 60, expired))
    rows = lambda: {r['id'] for r in app.store.query('SELECT id FROM memories')}  # noqa: E731
    temporal = lambda state: next(t['count'] for t in state['types'] if t['id'] == 'temporal')  # noqa: E731
    other = sqlite3.connect(app.home / 'pebble.sqlite', timeout=0.2, isolation_level=None)
    try:
        other.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        state = app.memory.stats()  # the database is locked by another connection
        assert time.monotonic() - started < 4.5  # under the 5 s busy timeout: it never waited on the lock
        assert temporal(state) == 1 and rows() >= {expired, kept}  # nothing written, expired row not counted
        assert int(app.store.db.execute('PRAGMA busy_timeout').fetchone()[0]) == 5000  # restored for everyone else
        other.execute('ROLLBACK')
    finally:
        other.close()
    assert temporal(app.state()['memory']) == 1 and expired not in rows() and kept in rows()  # free: expired on read


# d20-faults2 P4: the phase20 namespace and its suite ----------------------------------------------------------------

def test_the_verifier_runs_the_phase20_suite_in_its_own_namespace_and_no_earlier_campaign_changes():
    verify = load_script('verify_harness_candidate')
    # Phase 21 appends 'phase21' after phase20, so phase20 is located by position (same strength).
    rank = verify.PHASES.index('phase20')
    assert verify.PHASES[rank - 1:rank + 1] == ('phase19', 'phase20')
    assert verify.SUITES['phase20-faults'] == 'validate_phase20_faults.py'
    own = [s for s in verify.SUITES if s.startswith('phase20')]
    # The phase20 service validator (V20-validator) joins the fault campaign in the phase20 namespace.
    assert own == ['phase20', 'phase20-faults'] and all((ROOT / 'scripts' / verify.SUITES[s]).is_file() for s in own)
    assert verify.default_suites('phase20') == verify.default_suites('phase19') + own
    now = {phase: verify.default_suites(phase) for phase in verify.PHASES[:rank]}
    # The same function over the Phase 19 tables gives every earlier phase's campaign exactly as before.
    later = [s for s in verify.SUITES if any(s.startswith(p) for p in verify.PHASES[rank:])]
    suites, phases = dict(verify.SUITES), verify.PHASES
    try:
        verify.SUITES = {name: script for name, script in suites.items() if name not in later}
        verify.PHASES = phases[:rank]
        before = {phase: verify.default_suites(phase) for phase in verify.PHASES}
    finally:
        verify.SUITES, verify.PHASES = suites, phases
    assert now == before


# D20 seams: T1's manifests, read by T2 (install), T3 (trust) and T5 (probe and inventory) -------------------------

def test_the_candidate_manifests_are_read_by_the_install_trust_and_probe_tools(tmp_path):
    """T1's build with its own faked tools (test_build_candidate_app), then each consumer's real reader."""
    import test_build_candidate_app as t1
    checkout = t1.make_checkout(tmp_path)
    assert t1.build(checkout).execute()['result'] == 'built'
    C = checkout.out
    app_manifest, stage_manifest = C / 'app-manifest.json', C / 'stage/stage-manifest.json'
    doc = json.loads(app_manifest.read_text())
    # T3: the subject an attestation payload must carry is the release bundle's, platform included.
    attest = load_script('attest')
    assert attest.load_subject(app_manifest) == doc['subject'] == doc['apps']['release']['subject']
    assert doc['subject']['platform'] == attest.at.subject_platform()
    # T2: each bundle by key, equal to a fresh walk of that bundle.
    install = load_script('pebble_install')
    for kind, (name, _identifier, _config) in t1.bca.APPS.items():
        loaded = install.load_app_manifest(app_manifest, kind)
        assert loaded['key'] == kind and loaded['bundle_sha256'] == doc[kind + '_app_bundle_sha256']
        assert install.same_bundle(loaded, install.bundle_manifest(C / kind / (name + '.app')))
    # T5: the probe re-derives the same subject from the bundle, and both readers accept the stage manifest.
    probe = load_script('probe_installed_app')
    subject, _ = probe.compute_subject(C / 'release' / 'Pebble.app', doc['source_snapshot_sha256'])
    assert subject == doc['subject']
    staged, modes = probe.read_stage_manifest(stage_manifest.read_bytes())
    # T1 writes modes as one path-keyed map; the probe's reader takes them from there (merge reconciliation).
    assert staged['bundle_sha256'] == doc['stage_bundle_sha256']
    assert modes == json.loads(stage_manifest.read_text())['modes'] and modes['backend/pebble-backend'] == '0755'
    args = probe.parser().parse_args(['sidecar', '--app', str(C / 'release' / 'Pebble.app'),
                                      '--source-snapshot', str(checkout.snapshot), '--report', str(tmp_path / 'sidecar.json'),
                                      '--stage-manifest', str(stage_manifest),
                                      '--expect-app-sha256', doc['release_app_bundle_sha256']])
    _, sidecar = probe.probe(args, probe.Hooks())  # reads only; the report is returned, not written
    resources = sidecar['automated']['resources']
    assert sidecar['subject'] == doc['subject'] and sidecar['findings'] == [] and sidecar['automated_state'] == 'passed'
    assert resources['modes_compared'] is True and resources['equal_to_stage_manifest'] is True
    inventory = load_script('release_inventory')
    report = inventory.inventory(stage_manifest, cargo_home=tmp_path / 'no-cargo-home',
                                 metadata_lookup=lambda name: None, top_levels=lambda: {})
    assert report['inputs']['stage_manifest']['bundle_sha256'] == doc['stage_bundle_sha256']
    assert 'stage_manifest_digest_mismatch' not in report['summary']['findings']


# Parent decision on the first merge note: T3 (attestation) is the authority for pebble.release/1. T2 reads exactly
# T3's RELEASE_KEYS shape and has T3's verify_release_manifest decide a signed manifest with every install fact.

def sha_of(label):
    import hashlib
    return hashlib.sha256(label.encode()).hexdigest()


SSH_KEYGEN = '/usr/bin/ssh-keygen'
BUNDLE, BACKEND, PREVIOUS, BACKUP = sha_of('candidate app'), sha_of('candidate backend'), sha_of('0.4.2'), sha_of('backup')


@pytest.fixture
def release(tmp_path, capsys):
    """A manifest made by `scripts/attest.py release-payload`, signed by a throwaway release key with the system
    ssh-keygen (T3's own test keys), and assembled by `attest.py envelope`; its root is injected as an argument."""
    if not os.access(SSH_KEYGEN, os.X_OK):
        pytest.skip('Needs /usr/bin/ssh-keygen for throwaway keys.')
    from datetime import datetime, timedelta, timezone
    from pebble import attestation, migrations
    from test_attestation import Keys, root_for
    now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    (tmp_path / 'keys').mkdir()
    keys = Keys(tmp_path / 'keys')
    keys.make('release')
    kid, entry = root_for(keys.pub('release'), ('release',), label='test-release')
    roots = {kid: entry}
    subject = {'source_snapshot_sha256': sha_of('snapshot'), 'candidate_executable_sha256': BACKEND,
               'candidate_bundle_sha256': sha_of('backend tree'), 'app_bundle_sha256': BUNDLE,
               'platform': attestation.subject_platform()}
    (tmp_path / 'subject.json').write_text(json.dumps({'subject': subject}))
    attest = load_script('attest')
    payload, envelope = tmp_path / 'release.payload', tmp_path / 'release.envelope.json'
    assert attest.main(['release-payload', '--subject-from', str(tmp_path / 'subject.json'), '--version', '0.5.0',
                        '--build', '7', '--previous-sha256', PREVIOUS, '--rollback-sha256', BACKUP,
                        '--min-data-epoch', '0', '--data-epoch', str(migrations.SCHEMA_EPOCH),
                        '--not-after', attestation.stamp(now + timedelta(days=7)), '--out', str(payload)],
                       roots=roots, revoked={}, now=now) == 0
    signature = keys.sign_path(keys.release, payload)
    assert attest.main(['envelope', str(payload), str(signature), '--out', str(envelope)],
                       roots=roots, revoked={}, now=now) == 0
    capsys.readouterr()
    install = load_script('pebble_install')

    def check(path, verifier, require_signed=True, at=now, **changes):
        run = install.Run('install', None, install.Env(verifier=verifier, clock=lambda: at.timestamp()), False, {})
        facts = dict(bundle=BUNDLE, backend=BACKEND, version='0.5.0', installed_backend=PREVIOUS,
                     installed_version='0.4.2', backup_sha=BACKUP, data_version=migrations.SCHEMA_EPOCH - 1)
        facts.update(changes)
        return run, install.release_check(run, path, require_signed=require_signed, **facts)

    def refused(path, verifier, **changes):
        with pytest.raises(install.Stop) as caught:
            check(path, verifier, **changes)
        return caught.value.code

    return SimpleNamespace(install=install, roots=roots, kid=kid, now=now, payload=payload, envelope=envelope,
                           check=check, refused=refused, verifier=install.release_verifier(roots, {}),
                           epoch=migrations.SCHEMA_EPOCH, timedelta=timedelta)


def test_a_signed_release_manifest_from_the_trust_tool_passes_the_install_check(release):
    run, found = release.check(release.envelope, release.verifier)
    assert found['signed'] is True and found['verdict']['status'] == 'accepted' and found['verdict']['key_id'] == release.kid
    assert found['payload'] == {'version': '0.5.0', 'build': 7, 'app_bundle_sha256': BUNDLE,
                                'backend_executable_sha256': BACKEND, 'min_data_epoch': 0, 'data_epoch': release.epoch,
                                'previous': PREVIOUS, 'rollback': BACKUP, 'issued_at': found['payload']['issued_at'],
                                'not_after': found['payload']['not_after']}
    assert set(found['payload']) == set(release.install.RELEASE_KEYS) and found['release_keys_pinned'] == 1
    names = [c['check'] for c in run.checks]
    assert names[0] == 'release_signed_manifest_accepted' and all(c['ok'] for c in run.checks)
    assert {'release_previous', 'release_rollback', 'release_version', 'release_data_epoch'} <= set(names)


def test_each_mismatch_refuses_the_install_with_the_trust_channels_code(release):
    later = release.now + release.timedelta(days=8)
    for changes, code in ((dict(bundle=sha_of('another app')), 'foreign_subject'),
                          (dict(backend=sha_of('another backend')), 'foreign_subject'),
                          (dict(installed_backend=sha_of('another installed backend')), 'previous_mismatch'),
                          (dict(backup_sha=sha_of('another backup')), 'rollback_mismatch'),
                          (dict(installed_version='0.5.0'), 'version_not_higher'),
                          (dict(data_version=release.epoch + 1), 'data_epoch'),
                          (dict(at=later), 'expired')):
        assert release.refused(release.envelope, release.verifier, **changes) == code, changes
    revoked = release.install.release_verifier(release.roots, {release.kid: 'test revocation'})
    assert release.refused(release.envelope, revoked) == 'revoked'
    tampered = release.envelope.with_name('tampered.json')
    tampered.write_bytes(release.envelope.read_bytes().replace(b'"payload":"', b'"payload":"A', 1))
    assert release.refused(tampered, release.verifier) in ('malformed', 'bad_signature')
    # The candidate's own Info.plist version is the host's check: T3 accepts, the install still refuses.
    assert release.refused(release.envelope, release.verifier, version='0.6.0') == 'release_version'


def test_with_no_pinned_release_key_require_signed_fails_closed(release):
    production = release.install.default_release_verifier()  # backend/pebble/trust_roots.py ships empty roots
    assert production.pinned(release.now) == []
    assert release.refused(release.envelope, production) == 'unknown_key'
    assert release.refused(release.payload, production) == 'release_manifest_unsigned'
    # An unsigned manifest in T3's shape is read without --require-signed; the host's checks still apply.
    run, found = release.check(release.payload, production, require_signed=False)
    assert found['signed'] is False and found['release_keys_pinned'] == 0 and all(c['ok'] for c in run.checks)
    assert release.refused(release.payload, production, require_signed=False,
                           installed_backend=sha_of('another installed backend')) == 'release_previous'


# Parent fix (merge round 2): settled implies cleaned. A contained or refused supervised job's scratch HOME/TMPDIR is
# removed before its settled row is published, so no reader (test_process_recovery's real-job check among them) can
# see the row settled while the scratch is still there. The observers sit at the publishing UPDATE and the event.

def _observe_settlement(host):
    """{process id: whether its -home folder existed} at the settling UPDATE and at the settled process event."""
    seen = {'update': {}, 'emit': {}}

    def home(process_id):
        jobdir = host.jobs._jobdir_for(process_id)
        return jobdir.with_name(jobdir.name + '-home')

    def trace(statement):
        found = re.search(r"WHERE id='([0-9a-f]{32})'", statement)
        if statement.startswith('UPDATE process_jobs SET state=') and 'settled_by=' in statement and found:
            seen['update'][found.group(1)] = home(found.group(1)).exists()

    def emit(kind, payload):
        if kind == 'process' and payload.get('state') not in ('admitted', 'running'):
            seen['emit'][payload['process_id']] = (payload['state'], home(payload['process_id']).exists())
    with host.store.lock:
        host.store.db.set_trace_callback(trace)
    host.app.emit = emit
    return seen


def _stop_observing(host):
    with host.store.lock:
        host.store.db.set_trace_callback(None)


def test_a_contained_job_is_cleaned_before_anyone_sees_it_settled(tmp_path, monkeypatch):
    import test_process_recovery as prc
    sent = []
    monkeypatch.setattr(os, 'kill', lambda *args: sent.append(('kill', args)))  # recovery never signals anything
    monkeypatch.setattr(os, 'killpg', lambda *args: sent.append(('killpg', args)))
    host = prc.Host(tmp_path, worker=prc.FakeShim(), resume=False)
    try:
        contained = prc.plant(host, [prc.SPAWNING, prc.SPAWNED, prc.exited('command_exit', 0), prc.TEARDOWN])
        # Control: an unproven sweep keeps its scratch for inspection, so the order is the only thing that changed.
        unproven = prc.plant(host, [prc.SPAWNING, prc.SPAWNED, prc.exited('containment_lost', None, False),
                                    prc.TEARDOWN])
        homes = {}
        for job in (contained, unproven):
            homes[job.id] = job.jobdir.with_name(job.jobdir.name + '-home')
            homes[job.id].mkdir()
            (homes[job.id] / 'scratch.txt').write_text('left by the command\n')
        seen = _observe_settlement(host)
        assert sorted(host.jobs.resume()['settled']) == sorted([contained.id, unproven.id])
        _stop_observing(host)
        assert (host.row(contained.id)['state'], host.row(contained.id)['contained']) == ('succeeded', 1)
        assert (host.row(unproven.id)['state'], host.row(unproven.id)['contained']) == ('unknown', 0)
        assert seen['update'] == {contained.id: False, unproven.id: True}, seen
        assert seen['emit'] == {contained.id: ('succeeded', False), unproven.id: ('unknown', True)}, seen
        assert contained.jobdir.exists() and not homes[contained.id].exists() and homes[unproven.id].exists()
        # The time holds settle as before: a contained exit refunds, an unproven one stays charged.
        assert host.ledger.reservation(contained.hold)['state'] == 'settled'
        assert host.ledger.reservation(unproven.hold)['state'] == 'uncertain'
        assert sent == []
    finally:
        _stop_observing(host)
        host.close()


def _sse(delta):
    """One scripted chat-completions stream, as the provider client reads it."""
    class Response(list):
        def __enter__(self):
            return self

        def __exit__(self, *unused):
            return False

        def close(self):
            pass
    return Response([b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n',
                     b'data: ' + json.dumps({'choices': [], 'usage': {'prompt_tokens': 100,
                                                                       'completion_tokens': 20}}).encode() + b'\n',
                     b'data: [DONE]\n'])


def _calls(*items):
    return {'tool_calls': [{'index': n, 'id': ident, 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(args)}}
                           for n, (name, ident, args) in enumerate(items)]}


CALC = 'def add(a, b):\n    return a - b\n'
CALC_FIXED = 'def add(a, b):\n    return a + b\n'
TEST_CALC = ('import unittest\n\nfrom calc import add\n\n\nclass AddTest(unittest.TestCase):\n    def test_add(self):\n'
             '        self.assertEqual(add(1, 2), 3)\n\n\nif __name__ == "__main__":\n    unittest.main()\n')


def _approval_round(tmp_path, context, budget=None):
    """An edit and a test run awaiting approval in Ask mode, then both approved: v20's a3, through the real provider
    client (its projection and reply reserve) with only the HTTP request scripted. Returns (run, requests, chat):
    each request's projection manifest and the max_tokens it sent."""
    from pebble.permissions import DEFAULT
    project = tmp_path / 'project'
    project.mkdir(parents=True)
    (project / 'calc.py').write_text(CALC)
    (project / 'test_calc.py').write_text(TEST_CALC)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    manifests, requests = [], []
    try:
        tools = app.store.setting('permissions', DEFAULT)['tools']
        app.setting('permissions', {'mode': 'ask', 'tools': {name: name != 'screen' for name in tools}})
        app.model_save(name='Small fixture', url='http://127.0.0.1:9', model='small-fixture', context=context,
                       **({'budget': budget} if budget else {}))
        observe = app.model.observe_context
        app.model.observe_context = lambda manifest: (manifests.append(manifest), observe(manifest))[1]
        emitted, decided = [], []

        def request(url, headers, body=None, timeout=30):
            requests.append({'manifest': manifests[-1], 'max_tokens': body['max_tokens'], 'decided': bool(decided)})
            if 'r1' not in emitted:  # each call once, whatever the window still shows of it
                emitted.append('r1')
                delta = _calls(('Read', 'r1', {'file_path': 'calc.py'}),
                               ('Write', 'w1', {'file_path': 'calc.py', 'content': CALC_FIXED}))
            elif 't1' not in emitted:
                emitted.append('t1')
                delta = _calls(('RunTests', 't1', {'recipe': 'python-unittest'}))
            elif decided:
                delta = {'content': 'The fix is applied and the tests passed.\nFINAL: done'}
            else:
                delta = {'content': 'The fix is staged and the tests are requested.\nFINAL: staged'}
            return _sse(delta)
        app.model.request = request
        cid = app.new_chat(str(project))['id']
        rid = app.send(cid, 'Fix add in calc.py so it returns the sum, then run the tests.',
                       limits={'max_steps': 10, 'max_seconds': 120})['run_id']
        settled = lambda: app.active is None and app.runs.get(rid)['state'] not in ('queued', 'running', 'retry_wait')  # noqa: E731
        import test_process_recovery as prc
        assert prc.wait_for(settled, 30), app.runs.get(rid)['state']
        assert app.runs.get(rid)['state'] == 'waiting_approval', (app.runs.get(rid)['state'], app.runs.get(rid)['error'])
        decided.append(True)
        for kind in ('edit', 'tests'):  # the person approves the edit, then the test run
            action = next(a for a in app.store.actions(cid) if a['status'] == 'pending' and a['kind'] == kind)
            app.approve(cid, action['id'])
        prc.wait_for(lambda: settled() and app.runs.get(rid)['state'] != 'waiting_approval', 30)
        return app.runs.get(rid), requests, app.chat(cid)
    finally:
        app.active = app.active_run = None
        app.close()


def test_a_4096_token_model_finishes_an_approval_round_with_a_smaller_reply_not_a_larger_window(tmp_path):
    """Merge round 2 (E1, v20 finding 1): the runner sizes its 4K window by the 4,096-token working budget, but a
    4,096-token model's 1,024-token reply reserve leaves 3,008 input tokens, and the resume after the decision
    needs a protected floor of about 3,050. The provider now sends that floor alone with a smaller reply."""
    run, requests, chat = _approval_round(tmp_path / 'small', 4096)
    assert run['state'] == 'completed', (run['state'], run['error'])
    assert [m['content'] for m in chat['messages'] if m['role'] == 'assistant'][-1].endswith('FINAL: done')
    fitted = [r for r in requests if r['max_tokens'] < 1024]
    assert fitted and all(r['decided'] for r in fitted), [(r['max_tokens'], r['decided']) for r in requests]
    for item in requests:
        manifest = item['manifest']
        # Never above the working budget, and the request and its reply fit the model's 4,096 tokens.
        assert manifest['limits']['input'] + 64 <= 4096 and manifest['dispatchable'] is True
        assert manifest['input_tokens']['after'] + 64 + item['max_tokens'] <= 4096
        assert item['max_tokens'] == 1024 or item['max_tokens'] >= 512
    for item in fitted:  # only the protected floor rides with a smaller reply: nothing retirable was added
        manifest = item['manifest']
        assert manifest['input_tokens']['after'] == manifest['input_tokens']['protected_floor']
        assert all(u['protected'] for u in manifest['units'] if u['selected'])
    # Control: with 8,192 tokens of capacity and the same 4,096 budget nothing changes (v20's "4K" profile).
    control, requests, _ = _approval_round(tmp_path / 'control', 8192, 4096)
    assert control['state'] == 'completed' and {r['max_tokens'] for r in requests} == {1024}
    assert all(r['manifest']['limits']['input'] == 4096 - 64 for r in requests)


def test_a_fitted_reply_never_grows_the_window_or_leaves_too_little_to_answer():
    from pebble.providers import FITTED_REPLY, fitted_reply
    refused = lambda floor: {'input_tokens': {'protected_floor': floor, 'tools_only': 1300}}  # noqa: E731
    assert fitted_reply(refused(3046), 4096, 4096, 1024) == 4096 - 3046 - 64
    assert fitted_reply(refused(4033), 8192, 4096, 1024) is None  # above the working budget: still refused
    assert fitted_reply(refused(4096 - 64 - FITTED_REPLY + 1), 4096, 4096, 1024) is None  # too little to answer
    assert fitted_reply(refused(1000), 4096, 4096, 1024) == 1024  # the reply never grows
    assert fitted_reply({'input_tokens': {'protected_floor': None}}, 4096, 4096, 1024) is None


# Merge round 2 (E2, v20 finding 2): a recipe outcome names its procedure and runner, never a pytest test run --------

OLD_GATE = ('AgentGuard — completion gate: verification_failed\n\nThe test run reported failures — `python -m pytest  '
            '# pebble recipe npm-lint`: 1 failed. The task is not complete while its own tests are failing.')


@pytest.mark.parametrize('recipe, label', [('npm-lint', 'npm-lint recipe (lint, run by npm)'),
                                           ('npm-typecheck', 'npm-typecheck recipe (typecheck, run by npm)'),
                                           ('cargo-build', 'cargo-build recipe (build, run by cargo)'),
                                           ('python-unittest', 'python-unittest recipe (test, run by unittest)')])
def test_a_failing_recipe_is_named_by_its_procedure_and_runner_not_as_a_pytest_run(app, tmp_path, recipe, label):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'app.js').write_text('const unused = 1;\n')
    session = 'merge-round2-' + recipe
    assert app.guard.begin(project, session, 'Run the recipe and fix what it reports.')['state'] == 'started'
    # The run edited a source file (the gate is quiet without one), then the recipe reported a failure.
    app.guard.handle('post_tool_use', project, session, tool='Write',
                     arguments={'file_path': 'app.js', 'content': 'const unused = 2;\n'}, result={'status': 'applied'})
    app.guard.test_result(project, session, recipe, {'outcome': 'failed', 'exit_code': 1, 'tests': {},
                                                     'kind': recipes.REGISTRY[recipe].kind})
    decision = app.guard.handle('stop', project, session, last_assistant_message='Done.')
    reason = decision['reason']
    assert decision['action'] != 'allow' and reason.startswith('AgentGuard — completion gate: verification_failed')
    assert f'The {label} reported failures: 1 failed. The task is not complete while it is failing.' in reason, reason
    assert 'pytest' not in reason and 'test run' not in reason
    # The model-facing completion check gets shorter, at 4K too: AgentGuard's sentence named the pseudo-command.
    if recipe == 'npm-lint':
        assert reason == OLD_GATE.replace('The test run reported failures — `python -m pytest  # pebble recipe npm-lint`',
                                          'The ' + label + ' reported failures').replace(
            'while its own tests are failing', 'while it is failing') and len(reason) < len(OLD_GATE)


# Merge round 2 (E3, v20 finding 3): a model or permission change right after a delegated task waits for its child ---

def test_a_change_after_a_delegated_task_waits_for_its_settled_child_and_a_live_child_still_refuses(app, monkeypatch):
    from pebble.permissions import DEFAULT
    app.model_save(name='One', url='http://127.0.0.1:9', model='one', context=8192)
    first = app.model.current()['id']
    app.model_save(name='Two', url='http://127.0.0.1:9', model='two', context=8192)
    second = app.model.current()['id']
    assert first != second and not app.agents.releasing()

    def worker(ident, release_after=None):
        """A child worker still registered after its node settled (no live agent_nodes row): it is releasing."""
        with app.agents.lock:
            app.agents.workers[ident] = {'id': ident, 'generation': 1}
        if release_after is not None:
            def release():
                time.sleep(release_after)
                with app.agents.lock:
                    app.agents.workers.pop(ident, None)
            threading.Thread(target=release, daemon=True).start()
    try:
        for method, params, check in (
                ('model_select', {'profile_id': first}, lambda: app.model.current()['id'] == first),
                ('setting', {'key': 'permissions', 'value': {**copy.deepcopy(DEFAULT), 'mode': 'auto'}},
                 lambda: app.store.setting('permissions')['mode'] == 'auto')):
            worker('settled-child', release_after=0.4)
            assert app.agents.busy() and app.agents.releasing()
            started = time.monotonic()
            app.dispatch(method, params)  # accepted once the child's worker released, inside the call
            assert 0.3 <= time.monotonic() - started < App.CHANGE_WAIT and check()
        # A worker that never releases in time: the same refusal as before, after the bound.
        monkeypatch.setattr(App, 'CHANGE_WAIT', 0.3)
        worker('stuck-child')
        started = time.monotonic()
        with pytest.raises(ValueError, match=r'^Stop the current task before changing models\.$'):
            app.dispatch('model_select', {'profile_id': second})
        assert time.monotonic() - started >= 0.3 and app.model.current()['id'] == first
        # A live child (its node still active) or a running task refuses at once, as before.
        monkeypatch.setattr(App, 'CHANGE_WAIT', 30.0)
        monkeypatch.setattr(app.agents, 'releasing', lambda: False)
        started = time.monotonic()
        with pytest.raises(ValueError, match=r'^Stop the task before changing permissions\.$'):
            app.dispatch('setting', {'key': 'permissions', 'value': copy.deepcopy(DEFAULT)})
        assert time.monotonic() - started < 1 and app.store.setting('permissions')['mode'] == 'auto'
    finally:
        with app.agents.lock:
            for ident in ('settled-child', 'stuck-child'):
                app.agents.workers.pop(ident, None)


def test_a_refused_job_is_cleaned_before_its_settled_row_is_published(tmp_path):
    import test_process_recovery as prc
    homes = []

    def refuse(payload, cancel, on_started, on_output, *, timeout, max_output, jobdir=None, launcher=None,
               on_disconnect='terminate'):
        home = Path(jobdir).with_name(Path(jobdir).name + '-home')
        home.mkdir(parents=True)
        (home / 'scratch.txt').write_text('made before the refusal\n')
        homes.append(home)
        raise PolicyError('The fixture supervisor refused the command before starting it.')
    host = prc.Host(tmp_path, worker=refuse)
    try:
        seen = _observe_settlement(host)
        (host.project / 'probe.py').write_text("print('x')\n")
        view, _ = host.start(host.payload())
        row = host.settled(view['process_id'])
        assert prc.wait_for(lambda: view['process_id'] in seen['emit'], 10), seen
        assert row['state'] == 'refused' and row['jobdir'] and len(homes) == 1 and not homes[0].exists()
        assert seen['update'] == {view['process_id']: False} and seen['emit'] == {view['process_id']: ('refused', False)}
        assert host.hold(view['process_id'])['state'] == 'settled'  # refused: nothing ran, so the hold is refunded
    finally:
        _stop_observing(host)
        host.close()
