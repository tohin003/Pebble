"""Check explicit Pebble release evidence against the versioned readiness gates.

Reads only the files and roots named on the command line. It never installs,
launches, downloads, signs or modifies anything; with --native-app it runs the
fixed read-only codesign, spctl and stapler checks on that owned candidate app
(macOS only). Output is metadata only. See docs/HARNESS_RELEASE_GATES.md.

With a contract v2 review, --attestation GATE ENVELOPE EVIDENCE verifies an attestation
against the pinned roots in backend/pebble/trust_roots.py; --record-attestations appends
accepted envelopes to --attestation-ledger (O_EXCL, never replaced).

Exit codes: 0 production ready, 10 local source and candidate contract ready
(production gates open), 20 local readiness not met (stale, tampered, failing or
incomplete evidence), 30 malformed or unsafe evidence or report target, 2 usage.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from pebble import release_readiness as readiness  # noqa: E402

EVIDENCE = ('review', 'reports_root', 'source_root', 'source_snapshot', 'candidate_root', 'candidate_manifest',
            'artifact_root', 'test_log', 'native_app', 'executable')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--review', type=Path, help='Integrated review JSON (Phase 9 or Phase 10 shape).')
    parser.add_argument('--reports-root', type=Path, help='Directory holding the service reports the review names.')
    parser.add_argument('--source-root', type=Path, help='Checkout whose current files must equal the tested snapshot.')
    parser.add_argument('--source-snapshot', type=Path, help='Tested-source snapshot file whose digest the review records.')
    parser.add_argument('--candidate-root', type=Path, help='Onedir backend candidate directory.')
    parser.add_argument('--candidate-manifest', type=Path, help='Complete candidate manifest (files and bundle_sha256).')
    parser.add_argument('--artifact-root', type=Path, help='Root of the review verification_sha256 paths (hashed only).')
    parser.add_argument('--test-log', type=Path, help='Full backend pytest log to cross-check outcome counts.')
    parser.add_argument('--evaluation-report', type=Path, action='append', default=[],
                        help='Evaluation report for the quality gate (repeatable; never a pass in contract v1).')
    parser.add_argument('--attestation', nargs=3, action='append', default=[], metavar=('GATE', 'ENVELOPE', 'EVIDENCE'),
                        help='Contract v2 reviews only: a pebble.attestation/1 envelope for GATE and the evidence file it '
                             'names (repeatable). Verified only against backend/pebble/trust_roots.py.')
    parser.add_argument('--attestation-ledger', type=Path,
                        help='Append-only ledger of accepted envelopes (production: reports/harness-attestations).')
    parser.add_argument('--record-attestations', action='store_true',
                        help='Record newly accepted envelopes in the ledger (O_EXCL; nothing is replaced or deleted).')
    parser.add_argument('--executable', help='Candidate executable relative to --candidate-root (default from the review).')
    parser.add_argument('--require-suite', action='append', default=[], help='Suite that must exist in both modes (repeatable).')
    parser.add_argument('--min-backend-tests', type=int, help='Explicit floor for passing backend tests.')
    parser.add_argument('--native-app', type=Path,
                        help='Owned candidate .app for read-only codesign/spctl/stapler checks (macOS; never installed copies).')
    parser.add_argument('--signing-identity-status', choices=('available', 'absent', 'unknown'), default='unknown',
                        help='Parent-obtained read-only summary; recorded only, never a pass.')
    parser.add_argument('--fixture', action='store_true',
                        help='Alone: validate a generated synthetic evidence tree. With evidence: mark it synthetic. '
                             'Production readiness stays false either way.')
    parser.add_argument('--report', type=Path, help='Write the full result here atomically (replaces only an earlier result).')
    args = parser.parse_args(argv)
    if args.min_backend_tests is not None and args.min_backend_tests < 0:
        parser.error('--min-backend-tests must not be negative.')
    if args.record_attestations and args.attestation_ledger is None:
        parser.error('--record-attestations needs --attestation-ledger.')
    supplied = [name for name in EVIDENCE if getattr(args, name) is not None]
    if args.fixture and not supplied and not args.evaluation_report and not args.require_suite and not args.attestation:
        result = readiness.run_fixture()
    elif args.review is None or args.reports_root is None:
        parser.error('--review and --reports-root are required unless --fixture is used alone.')
    else:
        result = readiness.check_release(
            review=args.review, reports_root=args.reports_root, source_root=args.source_root,
            source_snapshot=args.source_snapshot, candidate_root=args.candidate_root,
            candidate_manifest=args.candidate_manifest, artifact_root=args.artifact_root, test_log=args.test_log,
            evaluation_reports=args.evaluation_report, native_app=args.native_app, executable=args.executable,
            require_suites=args.require_suite, min_backend_tests=args.min_backend_tests,
            signing_identity_status=args.signing_identity_status, fixture=args.fixture,
            attestations=[(gate, Path(envelope), Path(evidence)) for gate, envelope, evidence in args.attestation],
            attestation_ledger=args.attestation_ledger, record_attestations=args.record_attestations)
    if args.report is not None:
        try:
            readiness.write_report(args.report, result,
                                   protected_trees=(args.candidate_root, args.native_app, args.attestation_ledger))
        except readiness.EvidenceError as error:
            print(json.dumps({'error': error.code}), file=sys.stderr)
            return readiness.EXIT_MALFORMED
        print(json.dumps(readiness.summary(result), indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result['exit_code']


if __name__ == '__main__':
    sys.exit(main())
