"""The trust channel (D20 §6): pinned-key attestations, with each negative case refused by its exact code.

Keys are throwaway Ed25519, RSA and ECDSA keys generated in pytest tmp by /usr/bin/ssh-keygen with HOME
pointed at tmp and no SSH agent in the environment; ~/.ssh, the Keychain and the network are never used."""

import ast
import base64
import hashlib
import importlib.util
import inspect
import json
import os
import stat
import struct
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pebble import attestation as at
from pebble import release_readiness as rr
from pebble import trust_roots

SSH_KEYGEN = '/usr/bin/ssh-keygen'
REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not os.access(SSH_KEYGEN, os.X_OK) or not rr.anchored_io_supported(),
                                reason='Needs /usr/bin/ssh-keygen and descriptor-anchored file access.')

UTC = timezone.utc
REVIEW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
ISSUED = NOW - timedelta(hours=1)
WINDOW = ('2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z')


def h(label):
    return hashlib.sha256(label.encode()).hexdigest()


SUBJECT = {'source_snapshot_sha256': h('snapshot'), 'candidate_executable_sha256': h('executable'),
           'candidate_bundle_sha256': h('bundle'), 'app_bundle_sha256': h('app'), 'platform': 'darwin-arm64'}
OTHER = dict(SUBJECT, app_bundle_sha256=h('another candidate app'))
PREVIOUS, BACKUP = h('installed 0.4.2 backend'), h('backup.json')
V2_REVIEW = {'readiness_contract': at.CONTRACT, 'date': '2026-10-13T08:00:00+00:00', 'passed': True}
DEFAULT = object()


class Keys:
    """Throwaway keys and signatures made with the system ssh-keygen, isolated from the user's."""

    def __init__(self, base):
        self.base, self.home, self.count = base, base / 'home', 0
        self.home.mkdir(mode=0o700)
        # Built from scratch: no SSH_AUTH_SOCK, so no agent is consulted; HOME is the tmp folder.
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}

    def run(self, *args, stdin=b''):
        return subprocess.run([SSH_KEYGEN, *args], env=self.env, cwd=self.base, input=stdin,
                              capture_output=True, timeout=120)

    def make(self, name, kind='ed25519'):
        path = self.base / name
        bits = {'ed25519': [], 'rsa': ['-b', '2048'], 'ecdsa': ['-b', '256']}[kind]
        result = self.run('-q', '-N', '', '-t', kind, *bits, '-C', f'pebble-test-{name}', '-f', str(path))
        assert result.returncode == 0, result.stderr
        setattr(self, name, path)
        return path

    def pub(self, name):
        return (self.base / f'{name}.pub').read_text()

    def raw(self, name):
        return at.public_key_line(self.pub(name))[0]

    def sign_path(self, key, path, namespace=at.NAMESPACE, *options):
        result = self.run('-Y', 'sign', '-n', namespace, *options, '-f', str(key), str(path))
        assert result.returncode == 0, result.stderr
        return Path(str(path) + '.sig')

    def sign(self, key, data, namespace=at.NAMESPACE, *options):
        self.count += 1
        folder = self.base / 'signed'
        folder.mkdir(exist_ok=True)
        target = folder / f'payload-{self.count}'
        target.write_bytes(data)
        return self.sign_path(key, target, namespace, *options).read_text()


@pytest.fixture(scope='module')
def keys(tmp_path_factory):
    made = Keys(tmp_path_factory.mktemp('keys'))
    for name in ('operator', 'auditor', 'release', 'other'):
        made.make(name)
    made.make('rsa', 'rsa')
    made.make('ecdsa', 'ecdsa')
    return made


def root_for(pub_line, roles, gates=None, window=WINDOW, label='test-key'):
    raw, blob = at.public_key_line(pub_line)
    gates = gates or tuple(gate for gate, role in at.GATE_ROLES.items() if role in roles)
    return trust_roots.key_id(raw), {'alg': 'ssh-ed25519', 'public_key': blob, 'roles': tuple(roles),
                                     'gates': tuple(gates), 'not_before': window[0], 'not_after': window[1],
                                     'label': label}


def assemble(payload, signature, kid, **extra):
    """An envelope exactly as `attest.py envelope` writes it, without its self-consistency check."""
    return at.canonical({'format': at.FORMAT, 'key_id': kid, 'payload': base64.b64encode(payload).decode(),
                         'signature': signature, **extra})


def ssh_string(data):
    return struct.pack('>I', len(data)) + data


def armor(blob):
    text = base64.b64encode(blob).decode()
    return at.ARMOR_BEGIN + '\n' + '\n'.join(text[i:i + 70] for i in range(0, len(text), 70)) + '\n' + at.ARMOR_END + '\n'


def sshsig(public_raw, signature_raw, namespace=at.NAMESPACE.encode(), reserved=b'', hash_algorithm=b'sha512',
           version=1, trailing=b''):
    return armor(b'SSHSIG' + struct.pack('>I', version) + ssh_string(ssh_string(b'ssh-ed25519') + ssh_string(public_raw))
                 + ssh_string(namespace) + ssh_string(reserved) + ssh_string(hash_algorithm)
                 + ssh_string(ssh_string(b'ssh-ed25519') + ssh_string(signature_raw)) + trailing)


class World:
    """Pinned test roots (operator, auditor, release and a second operator key), a ledger and evidence."""

    def __init__(self, keys, base):
        self.keys, self.base = keys, base
        self.ids, self.roots = {}, {}
        for name, roles in (('operator', ('operator',)), ('auditor', ('auditor',)), ('release', ('release',)),
                            ('other', ('operator',))):
            kid, entry = root_for(keys.pub(name), roles, label=f'test-{name}')
            self.ids[name], self.roots[kid] = kid, entry
        self.revoked = {}
        self.ledger = at.Ledger(base / 'ledger')
        self.evidence = b'{"probe": "installed-application", "checks": 9}\n'

    def body(self, gate='installed_application', **changes):
        body = {'contract': at.CONTRACT, 'gate': gate, 'gate_version': 2, 'role': at.GATE_ROLES.get(gate, 'operator'),
                'subject': dict(SUBJECT), 'result': 'passed', 'findings': [],
                'evidence_sha256': rr.sha256(self.evidence), 'issued_at': at.stamp(ISSUED),
                'not_after': at.stamp(ISSUED + timedelta(days=7)), 'nonce': at.new_nonce()}
        body.update(changes)
        return body

    def envelope(self, body, signer='operator', kid=None):
        payload = body if isinstance(body, bytes) else at.canonical(body)
        signature = self.keys.sign(getattr(self.keys, signer), payload)
        return assemble(payload, signature, kid or self.ids[signer])

    def verify(self, envelope, gate='installed_application', subject=SUBJECT, evidence=DEFAULT, now=NOW,
               review=REVIEW, roots=None, revoked=None, ledger=None):
        return at.verify(envelope, roots=self.roots if roots is None else roots,
                         revoked=self.revoked if revoked is None else revoked, subject=subject, gate=gate,
                         review_date=review, now=now, ledger=self.ledger if ledger is None else ledger,
                         evidence=self.evidence if evidence is DEFAULT else evidence)

    def release_manifest(self, signer='release', **changes):
        body = {'format': at.RELEASE_FORMAT, 'version': '0.5.0', 'build': 21,
                'app_bundle_sha256': SUBJECT['app_bundle_sha256'],
                'backend_executable_sha256': SUBJECT['candidate_executable_sha256'], 'min_data_epoch': 0,
                'data_epoch': 9, 'previous': PREVIOUS, 'rollback': BACKUP, 'issued_at': at.stamp(ISSUED),
                'not_after': at.stamp(ISSUED + timedelta(days=7))}
        body.update(changes)
        return self.envelope(body, signer)

    def release_check(self, envelope, **changes):
        arguments = dict(roots=self.roots, revoked=self.revoked, app_bundle_sha256=SUBJECT['app_bundle_sha256'],
                         backend_executable_sha256=SUBJECT['candidate_executable_sha256'],
                         installed_backend_sha256=PREVIOUS, backup_record_sha256=BACKUP, installed_version='0.4.2',
                         installed_data_epoch=0, data_epoch=9, now=NOW)
        arguments.update(changes)
        return at.verify_release_manifest(envelope, **arguments)


@pytest.fixture
def world(keys, tmp_path):
    return World(keys, tmp_path)


def code(verdict):
    assert verdict.status == ('accepted' if verdict.code is None else 'refused')
    return verdict.code


def load_cli():
    spec = importlib.util.spec_from_file_location('attest_cli', REPO / 'scripts/attest.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(path):
    return sorted(str(item.relative_to(path)) for item in path.rglob('*')) if path.exists() else []


# ---- Accepted envelope and the system tool ----------------------------------------------------


def test_accepted_envelope_matches_ssh_keygen_verify(world, keys, tmp_path):
    payload = at.canonical(world.body())
    signature = keys.sign(keys.operator, payload)
    envelope = assemble(payload, signature, world.ids['operator'])
    verdict = world.verify(envelope)
    assert verdict.accepted and verdict.code is None and verdict.recorded == 'new'
    assert (verdict.key_id, verdict.gate, verdict.result) == (world.ids['operator'], 'installed_application', 'passed')
    assert verdict.envelope_sha256 == rr.sha256(envelope) and verdict.payload_sha256 == rr.sha256(payload)

    # The system tool agrees on the exact bytes, and refuses one flipped byte as this module does.
    blob = keys.pub('operator').split()[1]
    allowed = tmp_path / 'allowed_signers'
    allowed.write_text(f'pebble-operator namespaces="{at.NAMESPACE}" ssh-ed25519 {blob}\n')
    signature_file = tmp_path / 'payload.sig'
    signature_file.write_text(signature)
    good = keys.run('-Y', 'verify', '-f', str(allowed), '-I', 'pebble-operator', '-n', at.NAMESPACE,
                    '-s', str(signature_file), stdin=payload)
    assert good.returncode == 0 and b'Good' in good.stdout + good.stderr
    nonce = json.loads(payload)['nonce']
    flipped = payload.replace(nonce.encode(), (('0' if nonce[0] != '0' else '1') + nonce[1:]).encode())
    assert keys.run('-Y', 'verify', '-f', str(allowed), '-I', 'pebble-operator', '-n', at.NAMESPACE,
                    '-s', str(signature_file), stdin=flipped).returncode != 0
    assert code(world.verify(assemble(flipped, signature, world.ids['operator']))) == 'bad_signature'

    parsed = at.parse_sshsig(signature)
    assert (parsed.public_key_type, parsed.signature_type, parsed.namespace, parsed.hash_algorithm, parsed.reserved) == (
        b'ssh-ed25519', b'ssh-ed25519', b'pebble-attestation@1', b'sha512', b'')
    assert parsed.public_key == keys.raw('operator') and len(parsed.signature) == 64

    # The ledger keeps the exact accepted bytes in new read-only files.
    assert world.ledger.accepted_envelopes() == [(rr.sha256(envelope), envelope)]
    stored = Path(world.ledger.path) / 'envelopes' / (rr.sha256(envelope) + '.json')
    claim = Path(world.ledger.path) / 'nonces' / (json.loads(payload)['nonce'] + '.json')
    assert stat.S_IMODE(stored.stat().st_mode) == 0o444 and stat.S_IMODE(claim.stat().st_mode) == 0o444
    assert json.loads(claim.read_bytes())['payload_sha256'] == rr.sha256(payload)


def test_build_envelope_assembles_only_self_consistent_ed25519_signatures(world, keys):
    payload = at.build_payload(gate='installed_application', subject=SUBJECT, evidence_sha256=rr.sha256(world.evidence),
                               result='passed', issued_at=ISSUED, not_after=ISSUED + timedelta(days=3))
    assert at.canonical(json.loads(payload)) == payload
    envelope = at.build_envelope(payload, keys.sign(keys.operator, payload))
    assert json.loads(envelope)['key_id'] == world.ids['operator'] == at.envelope_key_id(envelope)
    assert world.verify(envelope).accepted
    with pytest.raises(at.SignatureError):
        at.build_envelope(payload, keys.sign(keys.rsa, payload))
    with pytest.raises(at.SignatureError):
        at.build_envelope(payload.replace(b'"passed"', b'"failed"'), keys.sign(keys.operator, payload))
    pretty = json.dumps(json.loads(payload), indent=2).encode()
    with pytest.raises(ValueError):
        at.build_envelope(pretty, keys.sign(keys.operator, pretty))
    with pytest.raises(ValueError):
        at.build_payload(gate='other_platform_containment', subject=SUBJECT, evidence_sha256=h('x'), result='passed',
                         issued_at=ISSUED, not_after=ISSUED + timedelta(days=1))
    with pytest.raises(ValueError):
        at.build_payload(gate='installed_application', subject=SUBJECT, evidence_sha256=h('x'), result='passed',
                         issued_at=ISSUED, not_after=ISSUED + timedelta(days=31))
    with pytest.raises(ValueError):
        at.build_payload(gate='independent_human_audit', subject=SUBJECT, evidence_sha256=h('x'), result='passed',
                         issued_at=ISSUED, not_after=ISSUED + timedelta(days=1),
                         findings=[{'code': 'sandbox-escape', 'severity': 'critical', 'count': 1}])


# ---- §6.5 negative cases, one exact code each -------------------------------------------------


def test_flipped_payload_byte_is_bad_signature(world, keys):
    body = world.body()
    payload = at.canonical(body)
    signature = keys.sign(keys.operator, payload)
    at_nonce = payload.index(body['nonce'].encode())
    flipped = bytearray(payload)
    flipped[at_nonce] = ord('0') if flipped[at_nonce] != ord('0') else ord('1')
    assert code(world.verify(assemble(bytes(flipped), signature, world.ids['operator']))) == 'bad_signature'
    broken = bytearray(payload)
    broken[0] = ord('[')
    assert code(world.verify(assemble(bytes(broken), signature, world.ids['operator']))) == 'malformed'


def test_reserialized_payload_is_never_accepted(world, keys):
    body = world.body()
    payload = at.canonical(body)
    signature = keys.sign(keys.operator, payload)
    # Re-serialized after signing: the verifier checks the bytes it is given and never re-serializes.
    for variant in (json.dumps(body, indent=2, sort_keys=True).encode(), json.dumps(body, separators=(',', ':')).encode(),
                    payload.replace(b'"passed"', b'"p\\u0061ssed"'), payload + b'\n'):
        assert json.loads(variant) == body
        assert code(world.verify(assemble(variant, signature, world.ids['operator']))) == 'bad_signature'
    # A person who signs a re-serialized (non-canonical) payload gets non_canonical.
    for variant in (json.dumps(body, indent=2, sort_keys=True).encode(), json.dumps(body, separators=(',', ':')).encode(),
                    payload.replace(b'"passed"', b'"p\\u0061ssed"'), payload + b'\n',
                    payload.replace(b'{"contract"', b'{ "contract"', 1)):
        envelope = assemble(variant, keys.sign(keys.operator, variant), world.ids['operator'])
        assert code(world.verify(envelope)) == 'non_canonical'


def test_swapped_keys_are_bad_signature(world, keys):
    payload = at.canonical(world.body())
    by_operator, by_other = keys.sign(keys.operator, payload), keys.sign(keys.other, payload)
    assert code(world.verify(assemble(payload, by_other, world.ids['operator']))) == 'bad_signature'
    assert code(world.verify(assemble(payload, by_operator, world.ids['other']))) == 'bad_signature'
    # The other key's signature spliced under the pinned key's public key does not verify either.
    spliced = sshsig(keys.raw('operator'), at.parse_sshsig(by_other).signature)
    assert code(world.verify(assemble(payload, spliced, world.ids['operator']))) == 'bad_signature'
    # The rebuilt blob itself is sound: the operator's own signature in it verifies.
    rebuilt = sshsig(keys.raw('operator'), at.parse_sshsig(by_operator).signature)
    assert world.verify(assemble(payload, rebuilt, world.ids['operator'])).accepted


def test_unknown_and_revoked_keys(world, keys):
    envelope = world.envelope(world.body())
    without = {kid: entry for kid, entry in world.roots.items() if kid != world.ids['operator']}
    assert code(world.verify(envelope, roots=without)) == 'unknown_key'
    assert code(world.verify(envelope, revoked={world.ids['operator']: 'compromised 2026-10'})) == 'revoked'
    # Still revoked after a release that also removed the key from ROOTS.
    assert code(world.verify(envelope, roots=without, revoked={world.ids['operator']: 'compromised'})) == 'revoked'
    assert world.verify(envelope).accepted


def test_key_outside_its_validity_window(world, keys):
    envelope = world.envelope(world.body())
    kid = world.ids['operator']
    for window in (('2026-01-01T00:00:00Z', '2026-10-01T00:00:00Z'),   # ended before now
                   ('2026-12-01T00:00:00Z', '2027-12-01T00:00:00Z'),   # not yet valid
                   ('2026-10-13T11:30:00Z', '2027-01-01T00:00:00Z')):  # valid now, but not when issued
        roots = dict(world.roots, **{kid: dict(world.roots[kid], not_before=window[0], not_after=window[1])})
        assert code(world.verify(envelope, roots=roots)) == 'key_expired'
    assert world.verify(envelope).accepted


def test_wrong_role_for_the_gate(world, keys):
    # The operator key cannot attest the independent audit, even if the payload names the auditor role.
    audit = world.envelope(world.body('independent_human_audit'), 'operator')
    assert code(world.verify(audit, gate='independent_human_audit')) == 'role_not_allowed'
    claimed = world.envelope(world.body('independent_human_audit', role='operator'), 'operator')
    assert code(world.verify(claimed, gate='independent_human_audit')) == 'role_not_allowed'
    # The auditor key cannot attest an operator gate.
    by_auditor = world.envelope(world.body(role='auditor'), 'auditor')
    assert code(world.verify(by_auditor)) == 'role_not_allowed'
    # An envelope presented for a gate other than the one it names.
    installed = world.envelope(world.body())
    assert code(world.verify(installed, gate='real_model_quality')) == 'role_not_allowed'
    # A root that does not list the gate.
    kid = world.ids['operator']
    narrowed = dict(world.roots, **{kid: dict(world.roots[kid], gates=('real_model_quality',))})
    assert code(world.verify(installed, roots=narrowed)) == 'role_not_allowed'
    assert world.verify(installed).accepted
    assert world.verify(world.envelope(world.body('independent_human_audit'), 'auditor'),
                        gate='independent_human_audit').accepted


def test_foreign_subject(world, keys):
    for subject in (OTHER, dict(SUBJECT, platform='linux-x86_64'), dict(SUBJECT, candidate_bundle_sha256=h('b2'))):
        assert code(world.verify(world.envelope(world.body(subject=subject)))) == 'foreign_subject'


def test_lifetime_and_expiry(world, keys):
    too_long = world.body(not_after=at.stamp(ISSUED + timedelta(days=31)))
    assert code(world.verify(world.envelope(too_long))) == 'lifetime'
    inverted = world.body(issued_at=at.stamp(NOW + timedelta(seconds=200)), not_after=at.stamp(NOW + timedelta(seconds=100)))
    assert code(world.verify(world.envelope(inverted))) == 'lifetime'
    expired = world.body(issued_at=at.stamp(REVIEW + timedelta(hours=1)), not_after=at.stamp(REVIEW + timedelta(hours=2)))
    assert code(world.verify(world.envelope(expired))) == 'expired'
    assert world.verify(world.envelope(world.body(not_after=at.stamp(ISSUED + timedelta(days=30))))).accepted


def test_issued_before_the_review_is_stale(world, keys):
    assert code(world.verify(world.envelope(world.body(issued_at=at.stamp(REVIEW - timedelta(hours=1)))))) == 'stale'
    assert code(world.verify(world.envelope(world.body(issued_at=at.stamp(REVIEW))))) == 'stale'


def test_issued_more_than_300_seconds_ahead_is_future(world, keys):
    ahead = world.body(issued_at=at.stamp(NOW + timedelta(seconds=301)),
                       not_after=at.stamp(NOW + timedelta(days=1)))
    assert code(world.verify(world.envelope(ahead))) == 'future'
    edge = world.body(issued_at=at.stamp(NOW + timedelta(seconds=300)), not_after=at.stamp(NOW + timedelta(days=1)))
    assert world.verify(world.envelope(edge)).accepted


def test_nonce_replay_and_idempotent_resubmission(world, keys, tmp_path):
    nonce = at.new_nonce()
    first = world.envelope(world.body(nonce=nonce))
    assert world.verify(first).recorded == 'new'
    again = world.verify(first)
    assert again.accepted and again.recorded == 'existing'
    # The same payload and signature in a reformatted envelope is the same attestation.
    reformatted = json.dumps(json.loads(first), indent=1).encode()
    assert world.verify(reformatted).recorded == 'existing'
    # The nonce reused on another subject, or another gate, is a replay.
    other_subject = world.envelope(world.body(nonce=nonce, subject=OTHER))
    assert code(world.verify(other_subject, subject=OTHER)) == 'replay'
    other_gate = world.envelope(world.body('real_model_quality', nonce=nonce))
    assert code(world.verify(other_gate, gate='real_model_quality')) == 'replay'
    # A later run over the same directory sees the same history.
    later = at.Ledger(world.ledger.path)
    assert code(world.verify(other_subject, subject=OTHER, ledger=later)) == 'replay'
    assert world.verify(first, ledger=later).recorded == 'existing'
    assert tree(Path(world.ledger.path)) == sorted(
        ['envelopes', 'nonces', f'nonces/{nonce}.json', f'envelopes/{rr.sha256(first)}.json',
         f'envelopes/{rr.sha256(reformatted)}.json'])
    # A read-only ledger writes nothing but still detects replay within its session.
    dry = at.Ledger(tmp_path / 'dry', record=False)
    fresh = world.envelope(world.body())
    assert world.verify(fresh, ledger=dry).recorded == 'dry_run'
    assert code(world.verify(world.envelope(dict(json.loads(base64.b64decode(json.loads(fresh)['payload'])),
                                                 subject=OTHER)), subject=OTHER, ledger=dry)) == 'replay'
    assert world.verify(first, ledger=at.Ledger(world.ledger.path, record=False)).recorded == 'existing'
    assert not (tmp_path / 'dry').exists()


def test_duplicate_keys_nan_oversized_and_deep_documents_are_malformed(world, keys):
    body = world.body()
    payload = at.canonical(body)
    good = world.envelope(body)
    duplicated = payload[:-1] + b',"nonce":"' + at.new_nonce().encode() + b'"}'
    not_a_number = payload.replace(b'"gate_version":2', b'"gate_version":NaN')
    infinite = payload.replace(b'"gate_version":2', b'"gate_version":Infinity')
    oversized = at.canonical(dict(body, padding='x' * (at.MAX_PAYLOAD_BYTES + 1)))
    deep = payload.replace(b'"findings":[]', b'"findings":' + b'[' * 60 + b']' * 60)
    for variant in (duplicated, not_a_number, infinite, oversized, deep, b'\xef\xbb\xbf' + payload, b'\xff' + payload):
        envelope = assemble(variant, keys.sign(keys.operator, variant), world.ids['operator'])
        assert code(world.verify(envelope)) == 'malformed'
    doc = json.loads(good)
    head = good[:-1]
    for envelope in (head + b',"format":"pebble.attestation/1"}', head + b',"extra":NaN}',
                     head + b',"padding":"' + b'x' * at.MAX_ENVELOPE_BYTES + b'"}',
                     b' ' * (rr.MAX_JSON_BYTES + 1), head + b',"deep":' + b'[' * 60 + b']' * 60 + b'}',
                     b'[]', b'', at.canonical(dict(doc, payload='not base64!')),
                     at.canonical(dict(doc, payload=doc['payload'][:8] + '\n' + doc['payload'][8:])),
                     at.canonical(dict(doc, payload=doc['payload'].replace('+', '-').replace('/', '_') + '-'))):
        assert code(world.verify(envelope)) == 'malformed'
    assert code(world.verify(json.dumps(doc))) == 'malformed'  # text, not the file's bytes
    assert world.verify(good).accepted


def test_format_violations(world, keys):
    good = world.envelope(world.body())
    doc = json.loads(good)
    for envelope in (at.canonical(dict(doc, extra=1)), at.canonical(dict(doc, format='pebble.attestation/2')),
                     at.canonical(dict(doc, key_id=doc['key_id'].upper())), at.canonical(dict(doc, signature=None)),
                     at.canonical({key: value for key, value in doc.items() if key != 'signature'})):
        assert code(world.verify(envelope)) == 'format'
    body = world.body()
    for changed in ({'nonce': 'a' * 31}, {'nonce': 'A' * 32}, {'issued_at': '2026-10-13T11:00:00+00:00'},
                    {'findings': [{'code': 'x', 'severity': 'severe', 'count': 1}]},
                    {'findings': [{'code': 'x', 'severity': 'low', 'count': 0}]},
                    {'findings': [{'code': 'x', 'severity': 'low', 'count': 1}, {'code': 'x', 'severity': 'low', 'count': 2}]},
                    {'issued_at': '２０２６-10-13T11:00:00Z'}, {'not_after': '2026-10-20T12:00:00.5Z'},
                    {'result': 'pass'}, {'gate': 'audit'}, {'gate_version': True}, {'evidence_sha256': 'f' * 63},
                    {'subject': dict(SUBJECT, extra=h('x'))}, {'contract': 'pebble.release-readiness/3'}):
        assert code(world.verify(world.envelope(dict(body, **changed)))) == 'format'
    missing = {key: value for key, value in body.items() if key != 'nonce'}
    assert code(world.verify(world.envelope(missing))) == 'format'
    counted = at.canonical(body).replace(b'"gate_version":2', b'"gate_version":2.0')
    assert code(world.verify(assemble(counted, keys.sign(keys.operator, counted), world.ids['operator']))) == 'format'
    listed = b'[1,2]'
    assert code(world.verify(assemble(listed, keys.sign(keys.operator, listed), world.ids['operator']))) == 'format'


def test_refusals_follow_the_d20_order(world, keys):
    """With two faults, the earlier §6.4 step names the refusal."""
    kid = world.ids['operator']
    payload = at.canonical(world.body(subject=OTHER))
    by_other = keys.sign(keys.other, payload)
    assert code(world.verify(assemble(payload, by_other, kid), revoked={kid: 'retired'})) == 'revoked'
    assert code(world.verify(assemble(payload, by_other, kid), gate='real_model_quality')) == 'role_not_allowed'
    assert code(world.verify(assemble(payload, by_other, kid))) == 'bad_signature'
    pretty = json.dumps(world.body(gate_version=1), indent=1).encode()
    assert code(world.verify(assemble(pretty, keys.sign(keys.operator, pretty), kid))) == 'non_canonical'
    assert code(world.verify(world.envelope(world.body(gate_version=1, subject=OTHER)))) == 'gate_version'
    late = world.body(subject=OTHER, issued_at=at.stamp(REVIEW - timedelta(hours=1)))
    assert code(world.verify(world.envelope(late))) == 'foreign_subject'
    assert code(world.verify(world.envelope(dict(late, subject=SUBJECT, not_after=at.stamp(REVIEW))))) == 'stale'
    assert code(world.verify(world.envelope(world.body(issued_at=at.stamp(NOW + timedelta(seconds=400)),
                                                       not_after=at.stamp(NOW + timedelta(days=40)))))) == 'future'
    nonce = at.new_nonce()
    assert world.verify(world.envelope(world.body(nonce=nonce))).accepted
    replayed = world.envelope(world.body(nonce=nonce, result='failed', evidence_sha256=h('other evidence')))
    assert code(world.verify(replayed)) == 'replay'
    unbound = world.envelope(world.body(result='failed', evidence_sha256=h('other evidence')))
    assert code(world.verify(unbound)) == 'evidence_mismatch'
    assert code(world.verify(world.envelope(world.body(
        result='failed', findings=[{'code': 'escape', 'severity': 'critical', 'count': 1}])))) == 'result_failed'
    assert list(at.REFUSALS[:3]) == ['malformed', 'format', 'downgrade'] and len(set(at.REFUSALS)) == len(at.REFUSALS)


def test_v1_contract_string_is_a_downgrade(world, keys):
    for contract in ('pebble.release-readiness/1', rr.CONTRACT, 'pebble.release-readiness/0'):
        assert code(world.verify(world.envelope(world.body(contract=contract)))) == 'downgrade'


def test_gate_version_must_match(world, keys):
    for version in (1, 3):
        assert code(world.verify(world.envelope(world.body(gate_version=version)))) == 'gate_version'


def test_other_namespace_hash_and_algorithms_are_bad_signature(world, keys):
    payload = at.canonical(world.body())
    kid = world.ids['operator']
    for namespace in ('file', 'pebble-attestation@2', 'git'):
        assert code(world.verify(assemble(payload, keys.sign(keys.operator, payload, namespace), kid))) == 'bad_signature'
    sha256_signed = keys.sign(keys.operator, payload, at.NAMESPACE, '-O', 'hashalg=sha256')
    assert at.parse_sshsig(sha256_signed).hash_algorithm == b'sha256'
    assert code(world.verify(assemble(payload, sha256_signed, kid))) == 'bad_signature'
    for key, key_type, signature_type in ((keys.rsa, b'ssh-rsa', b'rsa-sha2-512'),
                                          (keys.ecdsa, b'ecdsa-sha2-nistp256', b'ecdsa-sha2-nistp256')):
        signature = keys.sign(key, payload)
        parsed = at.parse_sshsig(signature)
        assert (parsed.public_key_type, parsed.signature_type) == (key_type, signature_type)
        assert code(world.verify(assemble(payload, signature, kid))) == 'bad_signature'
    # Hand-built blobs: a reserved field, another version and trailing bytes never verify.
    real = at.parse_sshsig(keys.sign(keys.operator, payload)).signature
    for signature in (sshsig(keys.raw('operator'), real, reserved=b'x'), sshsig(keys.raw('operator'), real, version=2),
                      sshsig(keys.raw('operator'), real, trailing=b'\x00'),
                      sshsig(keys.raw('operator'), real).replace('\n', '\r\n'),
                      sshsig(keys.raw('operator'), real) * 2, 'not a signature'):
        assert code(world.verify(assemble(payload, signature, kid))) == 'bad_signature'


def test_non_ed25519_roots_cannot_be_pinned(keys):
    raw_line = keys.pub('rsa')
    with pytest.raises(ValueError):
        at.public_key_line(raw_line)
    kid, entry = root_for(keys.pub('operator'), ('operator',))
    with pytest.raises(ValueError):
        at.load_roots({kid: dict(entry, alg='ssh-rsa')}, {})
    with pytest.raises(ValueError):
        at.load_roots({kid: dict(entry, public_key=raw_line.split()[1])}, {})


def test_evidence_changed_after_signing(world, keys):
    envelope = world.envelope(world.body())
    for evidence in (world.evidence + b' ', b'', None, world.evidence.decode()):
        assert code(world.verify(envelope, evidence=evidence)) == 'evidence_mismatch'
    assert world.verify(envelope).accepted


def test_failed_result_and_serious_findings(world, keys):
    failed = world.verify(world.envelope(world.body(
        result='failed', findings=[{'code': 'banner-duplicated', 'severity': 'medium', 'count': 1}])))
    assert code(failed) == 'result_failed' and failed.result == 'failed'
    assert failed.findings == (('banner-duplicated', 'medium', 1),)
    for severity in ('critical', 'high'):
        body = world.body('independent_human_audit', findings=[{'code': 'sandbox-escape', 'severity': severity, 'count': 1}])
        assert code(world.verify(world.envelope(body, 'auditor'), gate='independent_human_audit')) == 'serious_finding'
    minor = world.body('independent_human_audit', findings=[{'code': 'docs-gap', 'severity': 'low', 'count': 2},
                                                            {'code': 'naming', 'severity': 'info', 'count': 1}])
    accepted = world.verify(world.envelope(minor, 'auditor'), gate='independent_human_audit')
    assert accepted.accepted and accepted.findings == (('docs-gap', 'low', 2), ('naming', 'info', 1))
    assert tree(Path(world.ledger.path) / 'nonces') == [minor['nonce'] + '.json']


def test_unattestable_gates_are_never_satisfiable(world, keys):
    kid, entry = root_for(keys.pub('operator'), ('operator',))
    for gate in at.UNATTESTABLE:
        for role in ('operator', 'platform'):
            envelope = world.envelope(world.body(gate, role=role))
            assert code(world.verify(envelope, gate=gate)) == 'role_not_allowed'
        with pytest.raises(ValueError):
            at.load_roots({kid: dict(entry, gates=(gate,))}, {})
    with pytest.raises(ValueError):
        at.load_roots({kid: dict(entry, roles=('platform',), gates=('installed_application',))}, {})
    supplied = [(gate, world.envelope(world.body(gate)), world.evidence) for gate in at.UNATTESTABLE]
    outcome = at.evaluate_gates(V2_REVIEW, supplied, roots=world.roots, revoked={}, subject=SUBJECT, review_date=REVIEW,
                                now=NOW, ledger=world.ledger)
    assert set(outcome['gates']) == set(at.GATE_ROLES) and not set(at.UNATTESTABLE) & set(outcome['gates'])
    assert [verdict['verdict']['code'] for verdict in outcome['verdicts']] == ['role_not_allowed'] * 2
    with pytest.raises(ValueError):
        world.verify(world.envelope(world.body()), gate='not_a_gate')


# ---- Trust roots ------------------------------------------------------------------------------


def test_shipped_roots_are_empty_and_key_id_is_pure():
    assert trust_roots.ROOTS == {} and trust_roots.REVOKED == {} and trust_roots.ROOTS_VERSION == 1
    assert at.pinned_trust() == ({}, {}) and at.load_roots(trust_roots.ROOTS, trust_roots.REVOKED) == ({}, {})
    assert trust_roots.key_id(bytes(32)) == hashlib.sha256(bytes(32)).hexdigest()[:16] == '66687aadf862bd77'
    assert trust_roots.key_id(bytearray(b'abc')) == trust_roots.key_id(b'abc') == hashlib.sha256(b'abc').hexdigest()[:16]
    with pytest.raises(TypeError):
        trust_roots.key_id('not bytes')
    # Nothing in the channel reads the environment, and the CLI never runs ssh-keygen or asks for a secret.
    sources = {name: (REPO / name).read_text() for name in
               ('backend/pebble/trust_roots.py', 'backend/pebble/attestation.py', 'scripts/attest.py')}
    for name, text in sources.items():
        assert 'os.environ' not in text and 'getenv' not in text, name
    assert 'import os' not in sources['backend/pebble/trust_roots.py'] and 'open(' not in sources['backend/pebble/trust_roots.py']
    for forbidden in ('subprocess', 'getpass', 'input('):
        assert forbidden not in sources['scripts/attest.py'] and forbidden not in sources['backend/pebble/attestation.py']


def test_load_roots_refuses_malformed_pins(keys):
    kid, entry = root_for(keys.pub('operator'), ('operator',))
    _auditor_kid, auditor = root_for(keys.pub('auditor'), ('auditor',))
    for roots in ({kid.upper(): entry}, {kid[:-1] + ('0' if kid[-1] != '0' else '1'): entry},
                  {kid: dict(entry, roles=('operator', 'auditor'),
                             gates=('installed_application', 'independent_human_audit'))},
                  {kid: dict(entry, gates=('independent_human_audit',))}, {kid: dict(entry, roles=())},
                  {kid: dict(entry, roles=('operator', 'operator'))},
                  {kid: dict(entry, not_before=entry['not_after'], not_after=entry['not_before'])},
                  {kid: dict(entry, not_after='2027-01-01T00:00:00+00:00')}, {kid: dict(entry, label='bad\nlabel')},
                  {kid: dict(entry, extra=True)}, {kid: dict(auditor)}, {kid: [entry]}):
        with pytest.raises(ValueError):
            at.load_roots(roots, {})
    for revoked in ({'short': 'reason'}, {kid: ''}, {kid: 'line\nbreak'}, {kid: 1}):
        with pytest.raises(ValueError):
            at.load_roots({kid: entry}, revoked)
    loaded, _ = at.load_roots({kid: entry}, {kid: 'rotated 2027'})
    assert loaded[kid].raw == keys.raw('operator') and loaded[kid].roles == frozenset({'operator'})


# ---- Release manifest (the install tool's seam) -----------------------------------------------


def test_release_manifest_accepted_and_bound(world, keys):
    manifest = world.release_manifest()
    verdict = world.release_check(manifest)
    assert verdict.accepted and verdict.code is None and verdict.key_id == world.ids['release']
    assert verdict.manifest['version'] == '0.5.0' and verdict.manifest['data_epoch'] == 9
    assert world.release_check(manifest, installed_version=None, installed_data_epoch=None).accepted
    refusals = {
        'foreign_subject': [dict(app_bundle_sha256=h('tampered app')), dict(backend_executable_sha256=h('other'))],
        'previous_mismatch': [dict(installed_backend_sha256=h('another installed backend'))],
        'rollback_mismatch': [dict(backup_record_sha256=h('another backup'))],
        'version_not_higher': [dict(installed_version='0.5.0'), dict(installed_version='0.10.0')],
        'data_epoch': [dict(data_epoch=10), dict(installed_data_epoch=10)],
        'expired': [dict(now=ISSUED + timedelta(days=8))],
        'future': [dict(now=ISSUED - timedelta(seconds=301))],
        'unknown_key': [dict(roots={})],
        'revoked': [dict(revoked={world.ids['release']: 'retired'})],
    }
    for expected, cases in refusals.items():
        for changes in cases:
            assert code(world.release_check(manifest, **changes)) == expected, changes
    assert code(world.release_check(world.release_manifest(min_data_epoch=1))) == 'data_epoch'
    assert code(world.release_check(world.release_manifest(signer='operator'))) == 'role_not_allowed'
    assert code(world.release_check(world.release_manifest(not_after=at.stamp(ISSUED + timedelta(days=31))))) == 'lifetime'
    assert code(world.release_check(world.release_manifest(format='pebble.release/0'))) == 'downgrade'
    assert code(world.release_check(world.release_manifest(format='pebble.release/2'))) == 'format'
    assert code(world.release_check(world.release_manifest(min_data_epoch=10))) == 'format'
    assert code(world.release_check(world.envelope(world.body('signed_updates'), 'release'))) == 'format'
    payload = base64.b64decode(json.loads(manifest)['payload'])
    flipped = payload.replace(b'"0.5.0"', b'"0.9.0"')
    assert code(world.release_check(assemble(flipped, json.loads(manifest)['signature'], world.ids['release']))) == 'bad_signature'
    assert code(world.release_check(b'{"format": "pebble.attestation/1", "format": 1}')) == 'malformed'
    with pytest.raises(ValueError):
        world.release_check(manifest, backup_record_sha256=None)
    with pytest.raises(ValueError):
        world.release_check(manifest, installed_version='0.4')
    built = at.build_release_payload(version='0.5.0', build=21, app_bundle_sha256=SUBJECT['app_bundle_sha256'],
                                     backend_executable_sha256=SUBJECT['candidate_executable_sha256'], min_data_epoch=0,
                                     data_epoch=9, previous=PREVIOUS, rollback=BACKUP, issued_at=ISSUED,
                                     not_after=ISSUED + timedelta(days=7))
    assert world.release_check(at.build_envelope(built, keys.sign(keys.release, built))).accepted
    assert set(at.RELEASE_REFUSALS) >= set(refusals) | {'role_not_allowed', 'lifetime', 'downgrade', 'format', 'malformed',
                                                       'bad_signature', 'non_canonical', 'key_expired'}


def test_signed_updates_gate_needs_a_signed_release_manifest_as_evidence(world, keys):
    manifest = world.release_manifest()
    body = world.body('signed_updates', evidence_sha256=rr.sha256(manifest))
    envelope = world.envelope(body, 'release')
    assert world.verify(envelope, gate='signed_updates', evidence=manifest).accepted
    # A hash match is not enough: the evidence must verify as a release manifest for this app and backend.
    foreign = world.release_manifest(app_bundle_sha256=h('another app'))
    unsigned = b'{"format":"pebble.release/1"}'
    for evidence in (foreign, unsigned):
        claim = world.envelope(world.body('signed_updates', evidence_sha256=rr.sha256(evidence)), 'release')
        assert code(world.verify(claim, gate='signed_updates', evidence=evidence)) == 'evidence_mismatch'
    assert code(world.verify(world.envelope(world.body('signed_updates', evidence_sha256=rr.sha256(manifest)), 'operator'),
                             gate='signed_updates', evidence=manifest)) == 'role_not_allowed'


# ---- Contract v2 adapter ----------------------------------------------------------------------


def all_four(world):
    manifest = world.release_manifest()
    return [('installed_application', world.envelope(world.body()), world.evidence),
            ('real_model_quality', world.envelope(world.body('real_model_quality')), world.evidence),
            ('independent_human_audit', world.envelope(world.body('independent_human_audit'), 'auditor'), world.evidence),
            ('signed_updates', world.envelope(world.body('signed_updates', evidence_sha256=rr.sha256(manifest)), 'release'),
             manifest)]


def evaluate(world, supplied, review=V2_REVIEW, **changes):
    arguments = dict(roots=world.roots, revoked=world.revoked, subject=SUBJECT, review_date=REVIEW, now=NOW,
                     ledger=world.ledger)
    arguments.update(changes)
    return at.evaluate_gates(review, supplied, **arguments)


def test_evaluate_gates_passes_only_authenticated_bound_evidence(world, keys):
    supplied = all_four(world)
    outcome = evaluate(world, supplied)
    assert outcome['contract'] == 'pebble.release-readiness/2'
    for gate, rendered in outcome['gates'].items():
        assert rendered['status'] == 'passed', (gate, rendered)
        assert rendered['version'] == 2 and rendered['scope'] == 'production' and rendered['reason'] == at.PASSED_REASON
        assert rendered['issues'] == [] and rendered['missing_inputs'] == []
        assert rendered['observed']['role'] == at.GATE_ROLES[gate] and len(rendered['observed']['accepted']) == 1
        # The v1 gate shape, so the parent's patch only wires the dicts in.
        assert set(rendered) == {'version', 'scope', 'status', 'reason', 'missing_inputs', 'issues', 'observed'}
    assert [item['status'] for item in outcome['verdicts']] == ['accepted'] * 4
    assert [item['verdict']['recorded'] for item in outcome['verdicts']] == ['new'] * 4
    # Re-running the same evidence is idempotent.
    again = evaluate(world, supplied)
    assert all(rendered['status'] == 'passed' for rendered in again['gates'].values())
    assert [item['verdict']['recorded'] for item in again['verdicts']] == ['existing'] * 4
    assert len(world.ledger.accepted_envelopes()) == 4


def test_evaluate_gates_fails_on_any_refusal_and_waits_for_missing_evidence(world, keys):
    expired = world.envelope(world.body(issued_at=at.stamp(REVIEW + timedelta(hours=1)),
                                        not_after=at.stamp(REVIEW + timedelta(hours=2))))
    supplied = [('installed_application', world.envelope(world.body()), world.evidence),
                ('installed_application', expired, world.evidence),
                ('real_model_quality', world.envelope(world.body('real_model_quality', result='failed')), world.evidence)]
    outcome = evaluate(world, supplied)
    gates = outcome['gates']
    assert gates['installed_application']['status'] == 'failed'
    assert gates['installed_application']['issues'] == [{'code': 'attestation_expired', 'count': 1, 'subjects': ['envelope[1]']}]
    assert gates['real_model_quality']['status'] == 'failed'
    assert [issue['code'] for issue in gates['real_model_quality']['issues']] == ['attestation_result_failed']
    for gate in ('independent_human_audit', 'signed_updates'):
        assert gates[gate]['status'] == 'unverified' and gates[gate]['missing_inputs']
    # A subject the validator could not compute leaves every gate unverified and evaluates nothing.
    partial = evaluate(world, all_four(world), subject=dict(SUBJECT, app_bundle_sha256=None))
    assert {rendered['status'] for rendered in partial['gates'].values()} == {'unverified'}
    assert {item['status'] for item in partial['verdicts']} == {'not_evaluated'}
    assert all('app_bundle_sha256' in rendered['missing_inputs'][0] for rendered in partial['gates'].values())
    with pytest.raises(ValueError):
        evaluate(world, [], review={'date': V2_REVIEW['date'], 'passed': True})
    with pytest.raises(ValueError):
        evaluate(world, all_four(world), ledger=None)
    with pytest.raises(rr.EvidenceError):
        evaluate(world, [('installed_application', expired, world.evidence)] * (at.MAX_ENVELOPES + 1))
    with pytest.raises(rr.EvidenceError):
        evaluate(world, [('not_a_gate', expired, world.evidence)])


def test_empty_roots_default_leaves_every_production_gate_unverified(world, keys, tmp_path):
    roots, revoked = at.pinned_trust()
    bare = at.evaluate_gates(V2_REVIEW, [], roots=roots, revoked=revoked, subject=SUBJECT, review_date=REVIEW,
                             now=NOW, ledger=None)
    assert {gate: rendered['status'] for gate, rendered in bare['gates'].items()} == dict.fromkeys(at.GATE_ROLES, 'unverified')
    assert all(rendered['observed']['pinned_keys'] == [] for rendered in bare['gates'].values())
    # Validly signed envelopes from keys nobody pinned cannot help, and nothing is recorded.
    ledger = at.Ledger(tmp_path / 'empty-roots-ledger')
    signed = at.evaluate_gates(V2_REVIEW, all_four(world), roots=roots, revoked=revoked, subject=SUBJECT,
                               review_date=REVIEW, now=NOW, ledger=ledger)
    assert {rendered['status'] for rendered in signed['gates'].values()} == {'unverified'}
    assert {item['verdict']['code'] for item in signed['verdicts']} == {'unknown_key'}
    assert not (tmp_path / 'empty-roots-ledger').exists()
    # The v1 validator keeps all six production gates unverified as well.
    result = rr.check_release(**rr.build_fixture(tmp_path / 'fixture'), fixture=True)
    assert result['exit_code'] == rr.EXIT_LOCAL_READY and result['production_ready'] is False
    assert {gate: result['gates'][gate]['status'] for gate in rr.PRODUCTION_GATES} == dict.fromkeys(rr.PRODUCTION_GATES, 'unverified')


def test_claim_keys_in_a_review_are_still_ignored(world, keys, tmp_path):
    claims = {'installed_verified': True, 'human_audit': {'passed': True}, 'signed_updates': True,
              'real_model_quality': True, 'attestation': {'signed': True, 'installed_application': 'accepted'},
              'operator_attestation': 'signed', 'production_ready': True, 'independent_audit': True}
    plain = evaluate(world, [])
    claimed = evaluate(world, [], review=dict(V2_REVIEW, **claims))
    assert claimed['gates'] == plain['gates']
    assert {rendered['status'] for rendered in claimed['gates'].values()} == {'unverified'}
    assert {item['key'] for item in claimed['ignored_claims']} == {
        'installed_verified', 'human_audit', 'signed_updates', 'real_model_quality', 'attestation', 'attestation.signed',
        'operator_attestation', 'production_ready', 'independent_audit'}
    # Envelope-shaped claims in the review are just as inert as booleans.
    forged = dict(V2_REVIEW, attestations=[{'gate': 'installed_application', 'status': 'accepted', 'key_id': world.ids['operator']}])
    assert evaluate(world, [], review=forged)['gates'] == plain['gates']
    # Contract v1: the same claims in a v1 review are listed as ignored and change no gate.
    arguments = rr.build_fixture(tmp_path / 'fixture')
    review = json.loads(Path(arguments['review']).read_text())
    Path(arguments['review']).write_text(json.dumps(dict(review, **claims), indent=2) + '\n')
    result = rr.check_release(**arguments)
    assert result['contract_version'] == 1
    assert {'installed_verified', 'attestation', 'operator_attestation', 'signed_updates'} <= {
        item['key'] for item in result['ignored_claims']}
    assert all(result['gates'][gate]['status'] != 'passed' for gate in rr.PRODUCTION_GATES)


# ---- Ledger -----------------------------------------------------------------------------------


def test_ledger_never_overwrites_or_follows_links(world, keys, tmp_path):
    envelope = world.envelope(world.body())
    ledger_root = tmp_path / 'planted'
    (ledger_root / 'envelopes').mkdir(parents=True)
    planted = ledger_root / 'envelopes' / (rr.sha256(envelope) + '.json')
    planted.write_bytes(b'not the envelope')
    with pytest.raises(at.LedgerError) as raised:
        world.verify(envelope, ledger=at.Ledger(ledger_root))
    assert raised.value.code == 'ledger_conflict' and planted.read_bytes() == b'not the envelope'
    corrupt_root = tmp_path / 'corrupt'
    (corrupt_root / 'nonces').mkdir(parents=True)
    body = world.body()
    (corrupt_root / 'nonces' / (body['nonce'] + '.json')).write_bytes(b'{truncated')
    assert code(world.verify(world.envelope(body), ledger=at.Ledger(corrupt_root))) == 'replay'
    target = tmp_path / 'real-ledger'
    target.mkdir()
    (tmp_path / 'linked-ledger').symlink_to(target)
    with pytest.raises(at.LedgerError) as raised:
        world.verify(envelope, ledger=at.Ledger(tmp_path / 'linked-ledger'))
    assert raised.value.code == 'ledger_not_directory' and tree(target) == []
    with pytest.raises(ValueError):
        world.verify(envelope, ledger=object())
    # An unwritable ledger inside the gate adapter leaves the gate unverified instead of crashing.
    outcome = evaluate(world, [('installed_application', envelope, world.evidence)], ledger=at.Ledger(tmp_path / 'linked-ledger'))
    assert outcome['gates']['installed_application']['status'] == 'unverified'
    assert outcome['verdicts'][0]['ledger'] == 'ledger_not_directory'


def test_app_bundle_digest_matches_the_release_readiness_walker(tmp_path):
    app = tmp_path / 'Pebble Candidate.app'
    (app / 'Contents' / 'MacOS').mkdir(parents=True)
    (app / 'Contents' / 'Info.plist').write_bytes(b'<plist/>')
    (app / 'Contents' / 'MacOS' / 'pebble').write_bytes(b'\xcf\xfa\xed\xfe shell')
    entries = [{'path': 'Contents/Info.plist', 'bytes': 8, 'sha256': rr.sha256(b'<plist/>')},
               {'path': 'Contents/MacOS/pebble', 'bytes': 10, 'sha256': rr.sha256(b'\xcf\xfa\xed\xfe shell')}]
    assert at.app_bundle_digest(app) == rr.bundle_digest(entries)
    (app / 'Contents' / 'escape').symlink_to('/etc/hosts')
    assert at.app_bundle_digest(app) is None
    assert at.subject_platform() == at.subject_platform().lower() and at.PLATFORM_LABEL.fullmatch(at.subject_platform())


# ---- Command line -----------------------------------------------------------------------------


def subcommand_options(parser):
    action = next(item for item in parser._actions if item.__class__.__name__ == '_SubParsersAction')
    return {name: {option for item in sub._actions for option in item.option_strings} for name, sub in action.choices.items()}


def test_cli_payload_envelope_verify_round_trip(world, keys, tmp_path, capsys):
    cli = load_cli()
    trust = {'roots': world.roots, 'revoked': {}}
    subject_file = tmp_path / 'subject.json'
    subject_file.write_text(json.dumps({'format': 'pebble.attestation-subject/1', 'subject': SUBJECT}))
    evidence = tmp_path / 'installed-probes.json'
    evidence.write_bytes(world.evidence)
    payload = tmp_path / 'installed.payload'
    assert cli.main(['payload', '--gate', 'installed_application', '--subject-from', str(subject_file), '--evidence',
                     str(evidence), '--result', 'passed', '--not-after', '2026-10-20T12:00:00Z', '--finding',
                     'docs-gap:low:1', '--out', str(payload)], now=NOW, **trust) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['sign_command'] == f'/usr/bin/ssh-keygen -Y sign -n pebble-attestation@1 -f <your-ed25519-private-key> {payload}'
    data = payload.read_bytes()
    body = json.loads(data)
    assert at.canonical(body) == data and body['issued_at'] == at.stamp(NOW) and body['role'] == 'operator'
    assert body['evidence_sha256'] == rr.sha256(world.evidence) and at.NONCE.fullmatch(body['nonce'])
    assert body['findings'] == [{'code': 'docs-gap', 'count': 1, 'severity': 'low'}]
    # The key holder signs with their own key (here a throwaway one); the tool never sees it.
    signature = keys.sign_path(keys.operator, payload)
    envelope = tmp_path / 'installed.envelope.json'
    assert cli.main(['envelope', str(payload), str(signature), '--out', str(envelope)], now=NOW, **trust) == 0
    assembled = json.loads(capsys.readouterr().out)
    assert assembled['key_id'] == world.ids['operator'] and assembled['pinned'] is True
    ledger = tmp_path / 'ledger'
    verify = ['verify', str(envelope), '--subject-from', str(subject_file), '--gate', 'installed_application',
              '--review-date', '2026-10-13T08:00:00+00:00', '--evidence', str(evidence), '--ledger', str(ledger)]
    later = NOW + timedelta(minutes=5)
    assert cli.main(verify, now=later, **trust) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry['verdict']['status'] == 'accepted' and dry['verdict']['recorded'] == 'dry_run'
    assert dry['ledger_mode'] == 'read_only' and not ledger.exists()
    assert cli.main(verify + ['--execute'], now=later, **trust) == 0
    assert json.loads(capsys.readouterr().out)['verdict']['recorded'] == 'new'
    assert tree(ledger / 'envelopes') == [rr.sha256(envelope.read_bytes()) + '.json']
    # The shipped (empty) roots refuse it; nothing on the command line or in the environment adds a root.
    assert cli.main(verify) == 20
    assert json.loads(capsys.readouterr().out)['verdict']['code'] == 'unknown_key'
    # Outputs are never overwritten.
    before = payload.read_bytes()
    assert cli.main(['payload', '--gate', 'installed_application', '--subject-from', str(subject_file), '--evidence',
                     str(evidence), '--result', 'passed', '--not-after', '2026-10-20T12:00:00Z', '--out', str(payload)],
                    now=NOW, **trust) == 30
    assert payload.read_bytes() == before and 'output_exists' in capsys.readouterr().err
    # A changed payload does not assemble.
    tampered = tmp_path / 'tampered.payload'
    tampered.write_bytes(data.replace(b'"passed"', b'"failed"'))
    assert cli.main(['envelope', str(tampered), str(signature), '--out', str(tmp_path / 'x.json')], now=NOW, **trust) == 20
    assert json.loads(capsys.readouterr().out) == {'refused': 'bad_signature'} and not (tmp_path / 'x.json').exists()
    # Refusals: a payload whose lifetime exceeds 30 days, and a passed result with a critical finding.
    for extra in (['--not-after', '2026-11-13T12:00:01Z'], ['--not-after', '2026-10-20T12:00:00Z', '--finding', 'escape:critical:1']):
        assert cli.main(['payload', '--gate', 'installed_application', '--subject-from', str(subject_file), '--evidence',
                         str(evidence), '--result', 'passed', *extra, '--out', str(tmp_path / 'refused.payload')],
                        now=NOW, **trust) == 30
    assert not (tmp_path / 'refused.payload').exists()
    capsys.readouterr()


def test_cli_cannot_add_trust_roots(world, keys, tmp_path, monkeypatch, capsys):
    cli = load_cli()
    options = subcommand_options(cli.build_parser())
    assert options['verify'] == {'-h', '--help', '--subject-from', '--gate', '--review-date', '--evidence', '--ledger', '--execute'}
    assert not any(word in option for names in options.values() for option in names
                   for word in ('trust', 'roots', 'allowed', 'public-key', 'revoked', '--now', '--key'))
    envelope = tmp_path / 'e.json'
    envelope.write_bytes(world.envelope(world.body()))
    subject_file = tmp_path / 'subject.json'
    subject_file.write_text(json.dumps({'subject': SUBJECT}))
    evidence = tmp_path / 'evidence.json'
    evidence.write_bytes(world.evidence)
    monkeypatch.setenv('PEBBLE_TRUST_ROOTS', json.dumps(world.roots))
    monkeypatch.setenv('PEBBLE_ATTESTATION_ROOTS', json.dumps(world.roots))
    argv = ['verify', str(envelope), '--subject-from', str(subject_file), '--gate', 'installed_application',
            '--review-date', '2026-10-13T08:00:00Z', '--evidence', str(evidence), '--ledger', str(tmp_path / 'ledger')]
    assert cli.main(argv, now=NOW) == 20
    assert json.loads(capsys.readouterr().out)['verdict']['code'] == 'unknown_key'
    with pytest.raises(SystemExit):
        cli.main(argv + ['--roots', str(subject_file)])
    with pytest.raises(ValueError):
        cli.main(argv, roots=world.roots)
    capsys.readouterr()


def test_cli_subject_files_must_be_consistent(world, keys, tmp_path, capsys):
    cli = load_cli()
    entries = [{'path': 'Contents/Info.plist', 'bytes': 8, 'sha256': rr.sha256(b'<plist/>')}]
    digest = rr.bundle_digest(entries)
    evidence = tmp_path / 'evidence.json'
    evidence.write_bytes(world.evidence)
    good = tmp_path / 'app-manifest.json'
    good.write_text(json.dumps({'bundle_sha256': digest, 'files': entries, 'subject': dict(SUBJECT, app_bundle_sha256=digest)}))
    bad = tmp_path / 'bad-manifest.json'
    bad.write_text(json.dumps({'bundle_sha256': digest, 'files': entries, 'subject': SUBJECT}))
    none = tmp_path / 'no-subject.json'
    none.write_text(json.dumps({'bundle_sha256': digest, 'files': entries}))
    duplicated = tmp_path / 'duplicated.json'
    duplicated.write_text('{"subject": {}, "subject": {}}')
    base = ['payload', '--gate', 'real_model_quality', '--evidence', str(evidence), '--result', 'passed',
            '--not-after', '2026-10-14T12:00:00Z']
    assert cli.main(base + ['--subject-from', str(good), '--out', str(tmp_path / 'good.payload')],
                    now=NOW, roots=world.roots, revoked={}) == 0
    capsys.readouterr()
    # T1's pebble.candidate-app-manifest/1: top-level hashes, the release bundle's file list, this host's platform.
    release = {'app_bundle_sha256': digest, 'backend_executable_sha256': SUBJECT['candidate_executable_sha256'],
               'files': entries, 'links': []}
    t1 = {'schema': 'pebble.candidate-app-manifest/1', 'source_snapshot_sha256': SUBJECT['source_snapshot_sha256'],
          'candidate_executable_sha256': SUBJECT['candidate_executable_sha256'],
          'candidate_bundle_sha256': SUBJECT['candidate_bundle_sha256'], 'bundles': {'release': release, 'test': {}}}
    t1_file = tmp_path / 't1-app-manifest.json'
    t1_file.write_text(json.dumps(t1))
    t1_payload = tmp_path / 't1.payload'
    assert cli.main(base + ['--subject-from', str(t1_file), '--out', str(t1_payload)], now=NOW, roots=world.roots,
                    revoked={}) == 0
    capsys.readouterr()
    assert json.loads(t1_payload.read_bytes())['subject'] == dict(SUBJECT, app_bundle_sha256=digest,
                                                                  platform=at.subject_platform())
    tampered = dict(t1, bundles={'release': dict(release, files=[dict(entries[0], bytes=9)])})
    swapped = dict(t1, bundles={'release': dict(release, backend_executable_sha256=h('another backend'))})
    for name, doc in (('t1-tampered.json', tampered), ('t1-swapped.json', swapped)):
        (tmp_path / name).write_text(json.dumps(doc))
    for path, error in ((bad, 'subject_inconsistent'), (none, 'subject_missing'), (duplicated, 'duplicate_json_key'),
                        (tmp_path / 't1-tampered.json', 'subject_inconsistent'),
                        (tmp_path / 't1-swapped.json', 'subject_inconsistent')):
        assert cli.main(base + ['--subject-from', str(path), '--out', str(tmp_path / 'never.payload')],
                        now=NOW, roots=world.roots, revoked={}) == 30
        assert json.loads(capsys.readouterr().err) == {'error': error}
    assert not (tmp_path / 'never.payload').exists()


def test_pinned_keys_names_the_usable_channel(world):
    assert at.pinned_keys({}, {}, gate='signed_updates', now=NOW) == []
    assert at.pinned_keys(world.roots, {}, gate='installed_application', now=NOW) == sorted(
        [world.ids['operator'], world.ids['other']])
    assert at.pinned_keys(world.roots, {}, gate='independent_human_audit', now=NOW) == [world.ids['auditor']]
    assert at.pinned_keys(world.roots, {}, gate='signed_updates', now=NOW) == [world.ids['release']]
    assert at.pinned_keys(world.roots, {world.ids['release']: 'retired'}, gate='signed_updates', now=NOW) == []
    assert at.pinned_keys(world.roots, {}, gate='signed_updates', now=datetime(2027, 6, 1, tzinfo=UTC)) == []
    for gate in at.UNATTESTABLE:
        assert at.pinned_keys(world.roots, {}, gate=gate, now=NOW) == []


def test_cli_root_entry_and_release_payload(world, keys, tmp_path, capsys):
    cli = load_cli()
    trust = {'roots': world.roots, 'revoked': {}}
    public = tmp_path / 'pebble-operator-2026.pub'
    public.write_text(keys.pub('operator'))
    assert cli.main(['root-entry', str(public), '--role', 'operator', '--not-before', '2026-10-13T00:00:00Z',
                     '--not-after', '2027-10-13T00:00:00Z', '--label', 'operator-2026'], **trust) == 0
    printed = json.loads(capsys.readouterr().out)
    pinned = ast.literal_eval('{' + printed['roots_entry'] + '}')
    assert list(pinned) == [world.ids['operator']] and printed['key_id'] == world.ids['operator']
    assert pinned[world.ids['operator']]['gates'] == ('installed_application', 'real_model_quality')
    assert at.load_roots(pinned, {})[0][world.ids['operator']].raw == keys.raw('operator')
    assert printed['allowed_signers_line'].startswith('operator-2026 namespaces="pebble-attestation@1" ssh-ed25519 AAAA')
    # It reads public keys only, and never echoes anything else.
    secret = tmp_path / 'leaked.pub'
    secret.write_bytes(keys.operator.read_bytes())
    assert cli.main(['root-entry', str(secret), '--role', 'operator', '--not-before', '2026-10-13T00:00:00Z',
                     '--not-after', '2027-10-13T00:00:00Z', '--label', 'x'], **trust) == 30
    streams = capsys.readouterr()
    assert json.loads(streams.err) == {'error': 'private_key_refused'} and 'PRIVATE' not in streams.out + streams.err
    assert cli.main(['root-entry', str(keys.operator), '--role', 'operator', '--not-before', '2026-10-13T00:00:00Z',
                     '--not-after', '2027-10-13T00:00:00Z', '--label', 'x'], **trust) == 30
    assert json.loads(capsys.readouterr().err) == {'error': 'not_a_public_key_file'}
    # Release manifest payload, signed by the release key, verifies for the install tool.
    subject_file = tmp_path / 'subject.json'
    subject_file.write_text(json.dumps({'subject': SUBJECT}))
    release = tmp_path / 'release.payload'
    assert cli.main(['release-payload', '--subject-from', str(subject_file), '--version', '0.5.0', '--build', '21',
                     '--previous-sha256', PREVIOUS, '--rollback-sha256', BACKUP, '--min-data-epoch', '0',
                     '--data-epoch', '9', '--not-after', '2026-10-20T12:00:00Z', '--out', str(release)],
                    now=NOW, **trust) == 0
    assert json.loads(capsys.readouterr().out)['role'] == 'release'
    envelope = tmp_path / 'release.envelope.json'
    assert cli.main(['envelope', str(release), str(keys.sign_path(keys.release, release)), '--out', str(envelope)],
                    now=NOW, **trust) == 0
    capsys.readouterr()
    assert world.release_check(envelope.read_bytes(), now=NOW + timedelta(minutes=1)).accepted


def test_cli_audit_packet_is_read_only_and_refuses_stale_source(world, keys, tmp_path, capsys):
    cli = load_cli()
    source = tmp_path / 'source'
    files = {'backend/pebble/attestation.py': (REPO / 'backend/pebble/attestation.py').read_bytes(),
             'backend/pebble/trust_roots.py': (REPO / 'backend/pebble/trust_roots.py').read_bytes(),
             'scripts/attest.py': (REPO / 'scripts/attest.py').read_bytes(), 'backend/main.py': b'print("main")\n'}
    for rel, data in files.items():
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_bytes(data)
    (source / 'docs').mkdir()
    (source / 'docs/HARNESS_TEST_THREAT_MODEL.md').write_text('# Threat model\n')
    snapshot = tmp_path / 'tested-source.json'
    snapshot.write_text(json.dumps({rel: rr.sha256(data) for rel, data in sorted(files.items())}, indent=2) + '\n')
    findings = tmp_path / 'open-findings.json'
    findings.write_text(json.dumps({'findings': [{'code': 'phase14-orphan-survives-sigkill', 'severity': 'high', 'count': 1,
                                                  'note': 'Known production blocker, CLAUDE_HANDOFF_CONTEXT.md section 7.'}]}))
    before = {rel: rr.sha256((source / rel).read_bytes()) for rel in files}
    packet = tmp_path / 'audit-packet.json'
    argv = ['audit-packet', '--snapshot', str(snapshot), '--source-root', str(source), '--threat-model',
            'docs/HARNESS_TEST_THREAT_MODEL.md', '--findings', str(findings), '--out', str(packet)]
    assert cli.main(argv, now=NOW, roots=world.roots, revoked={}) == 0
    summary = json.loads(capsys.readouterr().out)
    document = json.loads(packet.read_bytes())
    assert summary['packet_sha256'] == rr.sha256(packet.read_bytes())
    assert document['format'] == 'pebble.audit-packet/1' and document['gate'] == 'independent_human_audit'
    assert document['source_snapshot']['sha256'] == rr.sha256(snapshot.read_bytes())
    assert document['source_snapshot']['source_sha256'] == {rel: rr.sha256(data) for rel, data in files.items()}
    assert document['threat_models'] == {'docs/HARNESS_TEST_THREAT_MODEL.md': rr.sha256(b'# Threat model\n')}
    assert document['open_findings'][0]['severity'] == 'high'
    assert document['trust_channel']['pinned_key_ids'] == sorted(world.roots)
    assert document['trust_channel']['files']['backend/pebble/attestation.py']['in_snapshot'] is True
    assert {rel: rr.sha256((source / rel).read_bytes()) for rel in files} == before
    # Never overwritten; a stale source writes no packet.
    first = packet.read_bytes()
    assert cli.main(argv, now=NOW, roots=world.roots, revoked={}) == 30 and packet.read_bytes() == first
    capsys.readouterr()
    (source / 'backend/main.py').write_bytes(b'print("changed")\n')
    stale = tmp_path / 'stale-packet.json'
    assert cli.main(argv[:-1] + [str(stale)], now=NOW, roots=world.roots, revoked={}) == 20
    assert json.loads(capsys.readouterr().out)['modified'] == ['backend/main.py'] and not stale.exists()


# ---- Contract v2 wiring in release_readiness.check_release ------------------------------------
# Merge (i20): the parent patch (release_readiness P1) landed, so these tests run the real check_release.
# The mirror of the patched flow is gone; in its place, v1_check_release is a step-for-step copy of the
# Phase 19 check_release before contract v2, so the v1 test compares the real function with the v1 flow
# rather than with itself.


def v1_check_release(*, review, reports_root, source_root=None, source_snapshot=None, candidate_root=None,
                     candidate_manifest=None, artifact_root=None, test_log=None, evaluation_reports=(),
                     native_app=None, executable=None, require_suites=(), min_backend_tests=None,
                     signing_identity_status='unknown', fixture=False, platform=None, runner=None, tools=None):
    if signing_identity_status not in ('available', 'absent', 'unknown'):
        raise ValueError('signing_identity_status must be available, absent or unknown.')
    if min_backend_tests is not None and not rr._count(min_backend_tests):
        raise ValueError('min_backend_tests must be a non-negative integer.')
    run = rr.Assessment(bool(fixture), rr.PLATFORM if platform is None else platform)
    run.gates['macos_distribution_signing'].observed['operator_reported_identity'] = signing_identity_status
    try:
        for suite in require_suites:
            if not (isinstance(suite, str) and rr.SUITE.fullmatch(suite)):
                raise rr.EvidenceError('required_suite_invalid')
        review_doc = run.guard(run.load_review, review)
        candidate = None
        if review_doc is not None:
            coverage = run.guard(rr.read_coverage, review_doc.get('source_coverage'))
            run.guard(run.check_snapshot, review_doc, source_snapshot)
            run.guard(run.check_internal, review_doc)
            log = run.guard(run.load_test_log, test_log) if test_log is not None else None
            run.guard(run.check_backend_tests, review_doc, log, min_backend_tests)
            run.guard(run.check_reports, review_doc, reports_root, tuple(require_suites))
            run.guard(run.check_artifacts, review_doc, artifact_root)
            if coverage is not None:
                run.guard(run.check_source, review_doc, source_root, coverage)
            candidate = run.guard(run.check_candidate, review_doc, candidate_root, candidate_manifest, executable)
        run.guard(run.check_evaluations, list(evaluation_reports))
        run.guard(run.check_native, native_app, candidate, runner, tools)
        run.guard(run.observe_build, review_doc)
    except rr.EvidenceError as error:
        run.malformed.add((error.code, error.subject or ''))
    finally:
        for root in run.roots:
            root.close()
    return run.result()


def test_contract_v2_wiring_is_applied_to_the_real_check_release():
    parameters = inspect.signature(rr.check_release).parameters
    assert {'attestations', 'attestation_ledger', 'record_attestations', 'trust', 'now'} <= set(parameters)
    assert all(parameters[name].kind is inspect.Parameter.KEYWORD_ONLY for name in parameters)
    assert (rr.CONTRACT_V2_KEY, rr.CONTRACT_V2) == (at.REVIEW_CONTRACT_KEY, at.CONTRACT)


def fixture_with_app(base, contract=None):
    arguments = rr.build_fixture(base / 'evidence')
    if contract is not None:
        review = json.loads(Path(arguments['review']).read_text())
        Path(arguments['review']).write_text(json.dumps(dict(review, readiness_contract=contract), indent=2) + '\n')
    app = base / 'build' / 'Pebble.app'
    (app / 'Contents' / 'MacOS').mkdir(parents=True)
    (app / 'Contents' / 'Info.plist').write_bytes(b'<plist/>')
    (app / 'Contents' / 'MacOS' / 'pebble').write_bytes(b'\xcf\xfa\xed\xfe synthetic shell')
    candidate = Path(arguments['candidate_root']) / 'pebble-backend'
    (app / 'Contents' / 'Resources' / 'resources' / 'backend').mkdir(parents=True)
    (app / 'Contents' / 'Resources' / 'resources' / 'backend' / 'pebble-backend').write_bytes(candidate.read_bytes())
    review = json.loads(Path(arguments['review']).read_text())
    subject = {'source_snapshot_sha256': review['source_snapshot_sha256'],
               'candidate_executable_sha256': review['candidate']['executable_sha256'],
               'candidate_bundle_sha256': review['candidate']['bundle_sha256'],
               'app_bundle_sha256': at.app_bundle_digest(app), 'platform': at.subject_platform()}
    # tools={} keeps the native signing checks from running any real tool.
    return dict(arguments, native_app=app, tools={}), subject


def attest_all(world, subject, base):
    manifest = world.release_manifest(app_bundle_sha256=subject['app_bundle_sha256'],
                                      backend_executable_sha256=subject['candidate_executable_sha256'])
    made = []
    for gate, signer, evidence in (('installed_application', 'operator', world.evidence),
                                   ('real_model_quality', 'operator', world.evidence),
                                   ('independent_human_audit', 'auditor', world.evidence),
                                   ('signed_updates', 'release', manifest)):
        envelope = world.envelope(world.body(gate, subject=dict(subject), evidence_sha256=rr.sha256(evidence)), signer)
        envelope_file, evidence_file = base / f'{gate}.envelope.json', base / f'{gate}.evidence'
        envelope_file.write_bytes(envelope)
        evidence_file.write_bytes(evidence)
        made.append((gate, envelope_file, evidence_file))
    return made


def test_contract_v2_wiring_keeps_v1_results_byte_identical(tmp_path):
    arguments, _subject = fixture_with_app(tmp_path)
    for supplied in (dict(arguments), {key: value for key, value in arguments.items() if key not in ('native_app', 'tools')}):
        expected = v1_check_release(**supplied)
        assert json.dumps(rr.check_release(**supplied), sort_keys=True) == json.dumps(expected, sort_keys=True)
        assert expected['contract_version'] == 1 and 'attestations' not in expected
        assert set(expected['gate_versions'].values()) == {1}


def test_contract_v2_wiring_passes_only_attested_gates_for_the_computed_subject(world, keys, tmp_path):
    arguments, subject = fixture_with_app(tmp_path, contract=at.CONTRACT)
    supplied = attest_all(world, subject, tmp_path)
    ledger = tmp_path / 'harness-attestations'
    result = rr.check_release(**arguments, attestations=supplied, attestation_ledger=ledger, record_attestations=True,
                              trust=(world.roots, {}), now=NOW)
    gates = result['gates']
    assert result['contract_version'] == 2 and result['evidence'] == 'well_formed'
    assert {gate: gates[gate]['status'] for gate in at.GATE_ROLES} == dict.fromkeys(at.GATE_ROLES, 'passed')
    assert {gate: result['gate_versions'][gate] for gate in at.GATE_ROLES} == dict.fromkeys(at.GATE_ROLES, 2)
    assert gates['real_model_quality']['observed']['unauthenticated']['kinds'] == {'scripted_fixture': 1}
    # The two unattestable gates keep v1 behavior, so production readiness stays false.
    assert gates['other_platform_containment']['status'] == 'unverified'
    assert gates['macos_distribution_signing']['status'] == 'unverified'
    assert result['production_ready'] is False and result['exit_code'] == rr.EXIT_LOCAL_READY
    assert [item['status'] for item in result['attestations']] == ['accepted'] * 4
    assert len(at.Ledger(ledger).accepted_envelopes()) == 4
    assert len(result['inputs']['attestation_sha256']) == 4
    # The shipped empty roots leave every attestation gate unverified, and record nothing.
    empty = rr.check_release(**arguments, attestations=supplied, attestation_ledger=tmp_path / 'unused', now=NOW)
    assert {empty['gates'][gate]['status'] for gate in at.GATE_ROLES} == {'unverified'}
    assert empty['exit_code'] == rr.EXIT_LOCAL_READY and not (tmp_path / 'unused').exists()
    # An attestation for another candidate app fails its gate; local readiness is unaffected.
    other = world.envelope(world.body(subject=dict(subject, app_bundle_sha256=h('another app'))))
    (tmp_path / 'foreign.json').write_bytes(other)
    failed = rr.check_release(**arguments, attestations=[('installed_application', tmp_path / 'foreign.json',
                                                         tmp_path / 'installed_application.evidence')],
                              attestation_ledger=ledger, trust=(world.roots, {}), now=NOW)
    assert failed['gates']['installed_application']['status'] == 'failed'
    assert [issue['code'] for issue in failed['gates']['installed_application']['issues']] == ['attestation_foreign_subject']
    assert failed['exit_code'] == rr.EXIT_LOCAL_READY
    # Without the app the subject is incomplete: nothing is evaluated.
    no_app = rr.check_release(**{key: value for key, value in arguments.items() if key != 'native_app'},
                              attestations=supplied, attestation_ledger=ledger, trust=(world.roots, {}), now=NOW)
    assert {no_app['gates'][gate]['status'] for gate in at.GATE_ROLES} == {'unverified'}
    assert {item['status'] for item in no_app['attestations']} == {'not_evaluated'}


def test_contract_v2_wiring_refuses_attestations_for_a_v1_review(world, keys, tmp_path):
    arguments, subject = fixture_with_app(tmp_path)
    supplied = attest_all(world, subject, tmp_path)
    result = rr.check_release(**arguments, attestations=supplied, attestation_ledger=tmp_path / 'ledger',
                              trust=(world.roots, {}), now=NOW)
    assert result['exit_code'] == rr.EXIT_MALFORMED and result['contract_version'] == 1
    assert {item['code'] for item in result['malformed']} == {'attestation_requires_contract_v2'}
    v2_arguments, _ = fixture_with_app(tmp_path / 'v2', contract=at.CONTRACT)
    missing = rr.check_release(**v2_arguments, attestations=supplied, trust=(world.roots, {}), now=NOW)
    assert {item['code'] for item in missing['malformed']} == {'attestation_ledger_missing'}
    assert missing['exit_code'] == rr.EXIT_MALFORMED and missing['contract_version'] == 2
