"""Prepare, assemble and verify Pebble production attestations (docs/HARNESS_TRUST_CHANNEL.md).

This tool never holds, generates or asks for a private key, and never runs
ssh-keygen. `payload` and `release-payload` write the exact canonical bytes a
person signs and print the one /usr/bin/ssh-keygen command they run with their
own key. `envelope` assembles their signature. `verify` checks an envelope
against the roots pinned in backend/pebble/trust_roots.py and nothing else: no
flag, environment variable or file adds a root. `audit-packet` exports, read
only, what an independent auditor needs. `root-entry` prints the trust_roots.py
entry for a public key. Every output file is created new (O_EXCL), and nothing
is overwritten or deleted. `verify` reads the ledger and records an accepted
envelope only with --execute.

Exit codes: 0 accepted or written; 20 refused, or the source differs from the snapshot; 30 unusable
input, or an output path that already exists; 2 usage.
"""
import argparse
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from pebble import attestation as at  # noqa: E402
from pebble import release_readiness as rr  # noqa: E402
from pebble import trust_roots  # noqa: E402

EXIT_OK, EXIT_REFUSED, EXIT_UNUSABLE = 0, 20, 30
DEFAULT_LEDGER = ROOT / 'reports' / 'harness-attestations'
MAX_EVIDENCE_BYTES, MAX_PUBLIC_KEY_BYTES = 256_000_000, 4096
KEY_PLACEHOLDER = '<your-ed25519-private-key>'
# The verifier's own files, which an auditor reviews along with the source.
CHANNEL_FILES = ('backend/pebble/attestation.py', 'backend/pebble/trust_roots.py', 'scripts/attest.py',
                 'backend/pebble/release_readiness.py')
PACKET_FORMAT = 'pebble.audit-packet/1'
APP_MANIFEST_SCHEMA = 'pebble.candidate-app-manifest/1'  # written by scripts/build_candidate_app.py (T1)
NOTE_CHARS = 200


class Unusable(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def read(path, limit, what):
    try:
        return rr.read_explicit(path, limit, what)
    except rr.EvidenceError as error:
        raise Unusable(error.code) from None


def write_new(path, data):
    """Create a new file; an existing path (or link) is never replaced."""
    try:
        fd = os.open(os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_CLOEXEC', 0), 0o644)
    except FileExistsError:
        raise Unusable('output_exists') from None
    except OSError:
        raise Unusable('output_unwritable') from None
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def moment(text):
    """A strict UTC stamp or any timezone-aware ISO 8601 time."""
    parsed = at.parse_stamp(text) or rr.timestamp(text)
    if parsed is None:
        raise argparse.ArgumentTypeError('expected a timezone-aware ISO 8601 time such as 2026-10-20T12:00:00Z')
    return parsed


def sha256_text(text):
    if not isinstance(text, str) or not rr.HEX64.fullmatch(text):
        raise argparse.ArgumentTypeError('expected a lowercase SHA256')
    return text


def finding(text):
    parts = text.split(':')
    if len(parts) != 3 or not re.fullmatch(r'[0-9]{1,7}', parts[2]):
        raise argparse.ArgumentTypeError('expected CODE:SEVERITY:COUNT, for example tls-pinning:medium:1')
    item = {'code': parts[0], 'severity': parts[1], 'count': int(parts[2])}
    try:
        at.check_findings([item])
    except ValueError:
        raise argparse.ArgumentTypeError(f'severity is one of {", ".join(at.SEVERITIES)}; the code is lowercase '
                                         'and the count at least 1') from None
    return item


def app_manifest_subject(doc):
    """The subject of T1's pebble.candidate-app-manifest/1: its top-level hashes, the release bundle's
    app_bundle_sha256 (re-derived from its file list) and this host's platform label."""
    release = doc.get('bundles', {}).get('release') if isinstance(doc.get('bundles'), dict) else None
    if not isinstance(release, dict):
        raise Unusable('subject_schema')
    files = release.get('files')
    if (not isinstance(files, list) or not files or not all(isinstance(entry, dict) and set(entry) == {'path', 'bytes', 'sha256'}
                                                            for entry in files)):
        raise Unusable('subject_schema')
    if rr.bundle_digest(files) != release.get('app_bundle_sha256'):
        raise Unusable('subject_inconsistent')
    if release.get('backend_executable_sha256', doc.get('candidate_executable_sha256')) != doc.get('candidate_executable_sha256'):
        raise Unusable('subject_inconsistent')
    subject = {'source_snapshot_sha256': doc.get('source_snapshot_sha256'),
               'candidate_executable_sha256': doc.get('candidate_executable_sha256'),
               'candidate_bundle_sha256': doc.get('candidate_bundle_sha256'),
               'app_bundle_sha256': release['app_bundle_sha256'], 'platform': at.subject_platform()}
    try:
        return at.check_subject(subject)
    except ValueError:
        raise Unusable('subject_schema') from None


def load_subject(path):
    """The subject in a subject.json (a top-level `subject` object) or in T1's app-manifest.json. When
    a subject document also carries an app's `files` and `bundle_sha256`, they must agree with it."""
    data = read(path, rr.MAX_JSON_BYTES, 'subject')
    try:
        doc = rr.parse_json(data, 'subject')
    except rr.EvidenceError as error:
        raise Unusable(error.code) from None
    if isinstance(doc, dict) and doc.get('schema') == APP_MANIFEST_SCHEMA and 'subject' not in doc:
        return app_manifest_subject(doc)
    if not isinstance(doc, dict) or 'subject' not in doc:
        raise Unusable('subject_missing')
    try:
        subject = at.check_subject(doc['subject'])
    except ValueError:
        raise Unusable('subject_schema') from None
    if 'files' in doc or 'bundle_sha256' in doc:
        try:
            manifest = rr.read_manifest(data)
        except rr.EvidenceError:
            raise Unusable('subject_manifest_schema') from None
        if not rr.bundle_digest(manifest['files']) == manifest['bundle_sha256'] == subject['app_bundle_sha256']:
            raise Unusable('subject_inconsistent')
    return subject


def emit(value):
    print(json.dumps(value, indent=2, sort_keys=True))


def sign_command(payload):
    return ' '.join(['/usr/bin/ssh-keygen', '-Y', 'sign', '-n', at.NAMESPACE, '-f', KEY_PLACEHOLDER,
                     shlex.quote(os.fspath(payload))])


def envelope_command(payload):
    target = os.fspath(payload)
    return ' '.join(['.runtime/agent-env/bin/python', 'scripts/attest.py', 'envelope', shlex.quote(target),
                     shlex.quote(target + '.sig'), '--out', shlex.quote(target + '.envelope.json')])


# ---- Commands ---------------------------------------------------------------------------------


def cmd_verify(args, roots, revoked, now):
    envelope = read(args.envelope, 2 * at.MAX_ENVELOPE_BYTES, 'envelope')
    subject = load_subject(args.subject_from)
    evidence = read(args.evidence, MAX_EVIDENCE_BYTES, 'evidence')
    ledger = at.Ledger(args.ledger, record=args.execute)
    try:
        verdict = at.verify(envelope, roots=roots, revoked=revoked, subject=subject, gate=args.gate,
                            review_date=args.review_date, now=now, ledger=ledger, evidence=evidence)
    except at.LedgerError as error:
        raise Unusable(error.code) from None
    emit({'verdict': verdict.as_dict(), 'ledger': ledger.path, 'ledger_mode': 'record' if args.execute else 'read_only',
          'roots_version': trust_roots.ROOTS_VERSION, 'pinned_key_ids': sorted(roots),
          'checked_at': at.stamp(now)})
    return EXIT_OK if verdict.accepted else EXIT_REFUSED


def cmd_payload(args, roots, revoked, now):
    subject = load_subject(args.subject_from)
    evidence = read(args.evidence, MAX_EVIDENCE_BYTES, 'evidence')
    try:
        data = at.build_payload(gate=args.gate, subject=subject, evidence_sha256=rr.sha256(evidence),
                                result=args.result, issued_at=now, not_after=args.not_after, findings=args.finding)
    except ValueError as error:
        print(json.dumps({'error': 'payload_refused', 'detail': str(error)}), file=sys.stderr)
        return EXIT_UNUSABLE
    write_new(args.out, data)
    body = json.loads(data)
    emit({'payload': os.fspath(args.out), 'payload_sha256': rr.sha256(data), 'gate': body['gate'],
          'role': body['role'], 'result': body['result'], 'nonce': body['nonce'], 'issued_at': body['issued_at'],
          'not_after': body['not_after'], 'evidence_sha256': body['evidence_sha256'],
          'sign_command': sign_command(args.out), 'then': envelope_command(args.out),
          'note': f'The {body["role"]} key holder reads the payload, then signs it with their own key. '
                  'This tool never sees that key.'})
    return EXIT_OK


def cmd_release_payload(args, roots, revoked, now):
    subject = load_subject(args.subject_from)
    try:
        data = at.build_release_payload(
            version=args.version, build=args.build, app_bundle_sha256=subject['app_bundle_sha256'],
            backend_executable_sha256=subject['candidate_executable_sha256'], min_data_epoch=args.min_data_epoch,
            data_epoch=args.data_epoch, previous=args.previous_sha256, rollback=args.rollback_sha256,
            issued_at=now, not_after=args.not_after)
    except ValueError as error:
        print(json.dumps({'error': 'payload_refused', 'detail': str(error)}), file=sys.stderr)
        return EXIT_UNUSABLE
    write_new(args.out, data)
    emit({'payload': os.fspath(args.out), 'payload_sha256': rr.sha256(data), 'format': at.RELEASE_FORMAT,
          'role': 'release', 'sign_command': sign_command(args.out), 'then': envelope_command(args.out)})
    return EXIT_OK


def cmd_envelope(args, roots, revoked, now):
    payload = read(args.payload, at.MAX_PAYLOAD_BYTES, 'payload')
    try:
        signature = read(args.signature, at.MAX_SIGNATURE_CHARS, 'signature').decode('ascii')
    except UnicodeDecodeError:
        raise Unusable('signature_not_ascii') from None
    try:
        data = at.build_envelope(payload, signature)
    except at.SignatureError:
        emit({'refused': 'bad_signature'})
        return EXIT_REFUSED
    except ValueError:
        emit({'refused': 'non_canonical'})
        return EXIT_REFUSED
    write_new(args.out, data)
    kid = at.envelope_key_id(data)
    emit({'envelope': os.fspath(args.out), 'envelope_sha256': rr.sha256(data), 'key_id': kid,
          'pinned': kid in roots, 'revoked': kid in revoked,
          'note': 'Assembly is not trust: run `attest.py verify` against the pinned roots.'})
    return EXIT_OK


def load_open_findings(path):
    data = read(path, rr.MAX_JSON_BYTES, 'findings')
    try:
        doc = rr.parse_json(data, 'findings')
    except rr.EvidenceError as error:
        raise Unusable(error.code) from None
    items = doc.get('findings') if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise Unusable('findings_schema')
    out = []
    for item in items:
        if (not isinstance(item, dict) or not {'code', 'severity', 'count'} <= set(item) <= {'code', 'severity', 'count', 'note'}
                or ('note' in item and not (isinstance(item['note'], str) and item['note'].isprintable()
                                            and item['note'].isascii() and len(item['note']) <= NOTE_CHARS))):
            raise Unusable('findings_schema')
        out.append({key: item[key] for key in ('code', 'severity', 'count', 'note') if key in item})
    try:
        at.check_findings([{key: item[key] for key in ('code', 'severity', 'count')} for item in out])
    except ValueError:
        raise Unusable('findings_schema') from None
    return out, rr.sha256(data)


def cmd_audit_packet(args, roots, revoked, now):
    data = read(args.snapshot, rr.MAX_JSON_BYTES, 'snapshot')
    try:
        snapshot = rr.digest_map(rr.parse_json(data, 'snapshot'), 'snapshot', rr.MAX_SOURCE_FILES)
        threat_paths = sorted({rr.safe_relative(path, 'threat_model') for path in args.threat_model})
    except rr.EvidenceError as error:
        raise Unusable(error.code) from None
    findings, findings_sha256 = load_open_findings(args.findings)
    subject = load_subject(args.subject_from) if args.subject_from is not None else None
    check = {'matched': 0, 'modified': [], 'missing': [], 'unsafe': []}
    threat_models, channel = {}, {}
    try:
        with rr.Root(os.fspath(args.source_root), 'source_root') as root:
            for rel in sorted(snapshot):
                try:
                    digest, _size = root.digest(rel, rr.MAX_SOURCE_FILE_BYTES)
                except rr.Missing:
                    check['missing'].append(rel)
                    continue
                except rr.Unsafe:
                    check['unsafe'].append(rel)
                    continue
                if digest == snapshot[rel]:
                    check['matched'] += 1
                else:
                    check['modified'].append(rel)
            for rel in threat_paths:
                try:
                    threat_models[rel] = root.digest(rel, rr.MAX_SOURCE_FILE_BYTES)[0]
                except (rr.Missing, rr.Unsafe):
                    raise Unusable('threat_model_unreadable') from None
            for rel in CHANNEL_FILES:
                try:
                    digest = root.digest(rel, rr.MAX_SOURCE_FILE_BYTES)[0]
                except (rr.Missing, rr.Unsafe):
                    digest = None
                channel[rel] = {'sha256': digest, 'in_snapshot': snapshot.get(rel) == digest and digest is not None}
    except rr.EvidenceError as error:
        raise Unusable(error.code) from None
    stale = len(check['modified']) + len(check['missing']) + len(check['unsafe'])
    if stale:
        emit({'refused': 'source_differs_from_snapshot', 'matched': check['matched'],
              'modified': check['modified'][:rr.MAX_SUBJECTS], 'missing': check['missing'][:rr.MAX_SUBJECTS],
              'unsafe': check['unsafe'][:rr.MAX_SUBJECTS], 'differing': stale})
        return EXIT_REFUSED
    packet = {
        'format': PACKET_FORMAT, 'generated_at': at.stamp(now),
        'purpose': 'What an independent auditor needs to review this exact source and sign, with their own key, an '
                   'independent_human_audit attestation. Reviewing it is the auditor\'s work; this file only binds it.',
        'contract': at.CONTRACT, 'gate': 'independent_human_audit', 'role': at.GATE_ROLES['independent_human_audit'],
        'gate_version': at.GATE_VERSIONS['independent_human_audit'],
        'source_snapshot': {'sha256': rr.sha256(data), 'files': len(snapshot), 'source_sha256': snapshot},
        'source_check': {'matched': check['matched'], 'differing': 0},
        'threat_models': threat_models,
        'open_findings': findings, 'open_findings_sha256': findings_sha256,
        'trust_channel': {'roots_version': trust_roots.ROOTS_VERSION, 'pinned_key_ids': sorted(roots),
                          'revoked_key_ids': sorted(revoked), 'files': channel},
        'subject': subject,
        'auditor_steps': [
            'Review the files listed in source_snapshot (each must hash to its recorded SHA256) against the threat models.',
            'Write your report; findings are reported by code, severity (critical, high, medium, low, info) and count.',
            'Ask for, or run, `scripts/attest.py payload --gate independent_human_audit --subject-from <subject.json> '
            '--evidence <your report> --result passed|failed --finding CODE:SEVERITY:COUNT ... --not-after <time> '
            '--out <file>`, read the payload, then sign it with your own key: '
            f'/usr/bin/ssh-keygen -Y sign -n {at.NAMESPACE} -f {KEY_PLACEHOLDER} <file>.',
            'A passed result with an open critical or high finding is refused by the verifier.'],
    }
    output = (json.dumps(packet, indent=2, sort_keys=True) + '\n').encode()
    write_new(args.out, output)
    emit({'packet': os.fspath(args.out), 'packet_sha256': rr.sha256(output), 'files': len(snapshot),
          'threat_models': len(threat_models), 'open_findings': len(findings)})
    return EXIT_OK


def cmd_root_entry(args, roots, revoked, now):
    if not os.fspath(args.public_key).endswith('.pub'):
        raise Unusable('not_a_public_key_file')
    data = read(args.public_key, MAX_PUBLIC_KEY_BYTES, 'public_key')
    if b'PRIVATE KEY' in data:
        raise Unusable('private_key_refused')
    try:
        text = data.decode('ascii')
    except UnicodeDecodeError:
        raise Unusable('public_key_not_ascii') from None
    gates = args.gate or [gate for gate, role in at.GATE_ROLES.items() if role in args.role]
    try:
        kid, entry = at.root_entry(text, roles=args.role, gates=gates, not_before=at.stamp(args.not_before),
                                   not_after=at.stamp(args.not_after), label=args.label)
    except ValueError as error:
        print(json.dumps({'error': 'root_entry_refused', 'detail': str(error)}), file=sys.stderr)
        return EXIT_UNUSABLE
    lines = [f'    {kid!r}: {{'] + [f'        {key!r}: {entry[key]!r},' for key in
                                      ('alg', 'public_key', 'roles', 'gates', 'not_before', 'not_after', 'label')] + ['    },']
    emit({'key_id': kid, 'roots_entry': '\n'.join(lines) + '\n',
          'allowed_signers_line': f'{entry["label"]} namespaces="{at.NAMESPACE}" {at.KEY_TYPE} {entry["public_key"]}',
          'already_pinned': kid in roots, 'revoked': kid in revoked,
          'note': 'Public key only. Paste roots_entry into ROOTS in backend/pebble/trust_roots.py in a new release; '
                  'that changes covered source and needs a new trial.'})
    return EXIT_OK


# ---- Command line -----------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)

    verify = commands.add_parser('verify', help='Verify an envelope against the pinned roots (read-only ledger by default).')
    verify.add_argument('envelope', type=Path)
    verify.add_argument('--subject-from', type=Path, required=True, help='subject.json or app-manifest.json with a subject object.')
    verify.add_argument('--gate', required=True, choices=rr.PRODUCTION_GATES)
    verify.add_argument('--review-date', type=moment, required=True, help='The review date the attestation must follow.')
    verify.add_argument('--evidence', type=Path, required=True, help='The evidence file the payload names by SHA256.')
    verify.add_argument('--ledger', type=Path, default=DEFAULT_LEDGER, help='Append-only ledger (default reports/harness-attestations).')
    verify.add_argument('--execute', action='store_true', help='Record an accepted envelope in the ledger (O_EXCL).')
    verify.set_defaults(handler=cmd_verify)

    payload = commands.add_parser('payload', help='Write the exact canonical payload bytes a person signs.')
    payload.add_argument('--gate', required=True, choices=tuple(at.GATE_ROLES))
    payload.add_argument('--subject-from', type=Path, required=True)
    payload.add_argument('--evidence', type=Path, required=True)
    payload.add_argument('--result', required=True, choices=('passed', 'failed'))
    payload.add_argument('--not-after', type=moment, required=True, help='At most 30 days from now.')
    payload.add_argument('--finding', type=finding, action='append', default=[], help='CODE:SEVERITY:COUNT (repeatable).')
    payload.add_argument('--out', type=Path, required=True, help='New file for the payload bytes.')
    payload.set_defaults(handler=cmd_payload)

    release = commands.add_parser('release-payload', help='Write the canonical pebble.release/1 manifest a release key signs.')
    release.add_argument('--subject-from', type=Path, required=True, help='Supplies the app bundle and backend hashes.')
    release.add_argument('--version', required=True)
    release.add_argument('--build', type=int, required=True)
    release.add_argument('--previous-sha256', type=sha256_text, required=True, help='Installed backend it replaces.')
    release.add_argument('--rollback-sha256', type=sha256_text, required=True, help='SHA256 of the backup.json.')
    release.add_argument('--min-data-epoch', type=int, required=True)
    release.add_argument('--data-epoch', type=int, required=True)
    release.add_argument('--not-after', type=moment, required=True)
    release.add_argument('--out', type=Path, required=True)
    release.set_defaults(handler=cmd_release_payload)

    envelope = commands.add_parser('envelope', help='Assemble an envelope from a payload and its ssh-keygen signature.')
    envelope.add_argument('payload', type=Path)
    envelope.add_argument('signature', type=Path)
    envelope.add_argument('--out', type=Path, required=True)
    envelope.set_defaults(handler=cmd_envelope)

    packet = commands.add_parser('audit-packet', help='Export, read-only, what an independent auditor needs.')
    packet.add_argument('--snapshot', type=Path, required=True, help='Tested-source snapshot ({path: sha256}).')
    packet.add_argument('--source-root', type=Path, default=ROOT)
    packet.add_argument('--threat-model', action='append', required=True, help='Threat model path below the source root.')
    packet.add_argument('--findings', type=Path, required=True, help='{"findings": [{code, severity, count, note?}]}')
    packet.add_argument('--subject-from', type=Path)
    packet.add_argument('--out', type=Path, required=True)
    packet.set_defaults(handler=cmd_audit_packet)

    entry = commands.add_parser('root-entry', help='Print the trust_roots.py entry for an ssh-ed25519 public key.')
    entry.add_argument('public_key', type=Path, help='The .pub file (public key only).')
    entry.add_argument('--role', action='append', required=True, choices=at.ROLES)
    entry.add_argument('--gate', action='append', choices=tuple(at.GATE_ROLES), help='Default: every gate of the roles.')
    entry.add_argument('--not-before', type=moment, required=True)
    entry.add_argument('--not-after', type=moment, required=True)
    entry.add_argument('--label', required=True)
    entry.set_defaults(handler=cmd_root_entry)
    return parser


def main(argv=None, *, roots=None, revoked=None, now=None):
    """Run one command. roots, revoked and now exist for tests only; the command line cannot set them."""
    args = build_parser().parse_args(argv)
    if roots is None and revoked is None:
        roots, revoked = at.pinned_trust()
    elif roots is None or revoked is None:
        raise ValueError('inject roots and revoked together')
    try:
        at.load_roots(roots, revoked)
    except ValueError as error:
        print(json.dumps({'error': 'trust_roots_invalid', 'detail': str(error)}), file=sys.stderr)
        return EXIT_UNUSABLE
    current = datetime.now(timezone.utc) if now is None else now
    try:
        return args.handler(args, roots, revoked, current)
    except Unusable as error:
        print(json.dumps({'error': error.code}), file=sys.stderr)
        return EXIT_UNUSABLE


if __name__ == '__main__':
    sys.exit(main())
