# Release evidence and readiness gates

`backend/pebble/release_readiness.py` validates explicit release evidence and reports versioned gates. `scripts/check_harness_release.py` is its command line. It only reads the files and roots named on the command line. It never installs, launches, downloads, signs, staples, notarizes or modifies anything. With `--native-app`, it also runs four fixed read-only macOS checks against that owned candidate app.

The validator separates two questions:

- **Local readiness.** Does the supplied source and candidate match the evidence recorded for them? The validator answers by hashing the files itself.
- **Production readiness.** Do signing, installed routing, update, audit, other-platform and quality gates pass? A JSON claim never satisfies any of these. In contract v1, only the macOS signing gate has a verification channel: native checks this tool runs itself. The other five production gates have no authenticated channel and stay `unverified`. Production readiness is therefore always false in v1.

No signing, notarization or installation was performed while building this validator. During Phase 10, the parent host had no valid code-signing identity. The current Tauri configuration signs ad-hoc (`signingIdentity: "-"`). The signing gate records this in `observed.build_config`. Native checks of an ad-hoc build fail the gate with `adhoc_signature`; they never pass it.

## Gates

Each gate reports `version`, `scope`, `status` (`passed`, `failed` or `unverified`), `reason`, `missing_inputs`, `issues` (codes, counts and safe subjects) and metadata-only `observed` values. A gate passes only when it has no issue, no missing input and no unverified reason. Any contradiction fails it.

| Gate | Scope | Passes when |
| --- | --- | --- |
| `evidence_integrity` | local | The review is internally consistent: its snapshot digest is reproducible or matches `--source-snapshot`, its module and validator maps agree with the snapshot, and its check total is the sum of its reports. Every named report matches its recorded SHA256, check count and date, and no report is dated after the review. Every listed verification artifact matches. |
| `backend_tests` | local | `collected == passed`, with no skipped, failed, errored, xfailed or xpassed tests. Totals agree, each `phaseN_suite_passes` count is positive and any `--min-backend-tests` floor is met. If `--test-log` is supplied, the log's final pytest summary must equal these outcomes and the log must be one of the review's verification artifacts. |
| `source_freshness` | local | Every snapshot file under `--source-root` is a regular, non-linked file with the recorded SHA256. No covered input exists outside the snapshot, and no link exists under a covered folder. |
| `source_services` | local | Every source report passed, has unique non-empty checks and has a non-empty `source_sha256` consistent with the snapshot. Its `backend_sha256`, when set, is the source entry point and not the candidate. Any `parent_invocation` names this snapshot and candidate and records an unchanged, successful run. Every suite also has a source mode. |
| `candidate_bundle` | local | The tree under `--candidate-root` matches `--candidate-manifest` entry for entry. Manifest, tree and review agree on bundle digest, file count, byte total and executable SHA256. The executable is a regular file with an execute bit. |
| `frozen_services` | local | Every frozen report passed and records `backend_sha256` equal to the candidate executable. Every suite has a frozen mode with the same check names as its source mode, the same `approved_source_sha256` when one is set, and the same `parent_invocation` consistency. |
| `macos_distribution_signing` | production | On macOS, `codesign --verify --deep --strict` succeeds. `codesign --display` shows a Developer ID Application signature with a team identifier, hardened runtime, secure timestamp and the Developer ID chain to Apple Root CA. `spctl --assess` accepts the app as *Notarized Developer ID*. `stapler validate` finds a stapled ticket. The app must also contain a regular file whose bytes equal the validated candidate executable. |
| `installed_application` | production | Never in v1. The tool does not touch installed copies and has no authenticated installed-routing probe. |
| `signed_updates` | production | Never in v1. There is no pinned updater key or signed update artifact to verify; `observed.build_config` reports whether an updater is configured. |
| `independent_human_audit` | production | Never in v1. A JSON statement cannot authenticate a human reviewer. |
| `other_platform_containment` | production | Never in v1. It needs evidence produced and authenticated on those platforms. |
| `real_model_quality` | production | Never in v1. `evaluation.py` reports are observed but not authenticated, and no release quality threshold exists. |

`readiness.local_source` requires the four source-side gates. `readiness.local_candidate` additionally requires `candidate_bundle` and `frozen_services`. `production_ready` requires every gate and is always false with `--fixture`.

The result also separates **history** from **freshness**. The `history` booleans show whether the review agrees with the reports, artifacts and manifest it names. The `freshness` booleans show whether current source and candidate bytes match that evidence. Legacy reports therefore provide historical integrity, not current-source proof.

## Release hygiene gates (Phase 24)

`release_readiness.release_gates(...)` evaluates four more gates over explicit reports, separately from `check_release`, so readiness results of contract v1 and v2 keep their exact shape. Each gate has scope `release`, version 1 and the same `status`/`reason`/`issues`/`observed` form; a gate whose input is missing is `unverified`, and the result's `passed` is true only when all four pass. Results stay content-free: a secret finding is reported as its rule and file name only.

| Gate | Input | Passes when |
| --- | --- | --- |
| `third_party_notices` | `scripts/release_inventory.py` report (`pebble.release-inventory/1`) and `backend/requirements.lock` | The report's `result` is `complete` with no finding, and its `THIRD-PARTY-NOTICES.txt` was generated from this lock (V1-046). Each inventory finding is also an issue (`inventory_<code>`). |
| `dependency_audit` | `pip-audit -r backend/requirements.lock -f json` and/or `osv-scanner --lockfile backend/requirements.lock --format json` reports, and a triage file | Every reported vulnerability (by its id or an alias) has a triage entry with a verdict (`not_affected`, `fixed_in_lock` or `accepted_risk`) and a non-empty reason, and the triage file names this lock's SHA-256 (`pebble.audit-triage/1`: `{"format", "lock_sha256", "entries": {id: {"verdict", "reason"}}}`). |
| `secret_scan` | gitleaks JSON report over the staged tree (`C/stage/resources`), with the reviewed allowlist | The report lists nothing. |
| `machine_paths` | `C/stage/stage-manifest.json` and `C/app-manifest.json` from `scripts/build_candidate_app.py` | The stage and every app record a scan (`files_scanned > 0`) with `machine_specific == 0` (V1-023): no shipped binary or `base_library.zip` member carries the build machine's paths. |

The inventory also refuses GPL and LGPL code in the bundle (`copyleft_licence`, `copyleft_binary`) and stale notices (`notice_version_differs`, `notices_inputs_differ`). `scripts/check_harness_release.py` does not call `release_gates` yet; the Phase 24 validator and finalizer (t24-validator) are its intended callers.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Production ready (unreachable in contract v1). |
| 10 | Evidence is well formed and both local readiness levels pass; production gates are open. |
| 20 | Evidence is well formed, but local readiness is not met because of stale, tampered, failing, unbound or incomplete evidence, or a missing input. |
| 30 | Malformed or unsafe evidence: unparseable or oversized JSON, duplicate keys or entries, schema violations, unsafe or private paths, symlinked roots or documents, or a refused `--report` target. |
| 2 | Command-line usage error. |

An unexpected crash exits 1, which is never a readiness result.

## Input contract (v1)

Every path inside a document must be an exact relative POSIX path. Absolute paths, `..`, `.`, empty components, backslashes, drive letters and control characters are refused. Duplicates that differ only by case or Unicode normalization are refused, and so are private paths: `.env*`, `.ssh`, `.git`, `.aws`, `credentials`, `secrets`, `.pebble`, key-material suffixes such as `.key`, `.p12`, `.p8` and `.keychain`, and `.pem` outside the candidate bundle. JSON must be UTF-8 without a BOM, with no duplicate keys, `NaN` or `Infinity`; documents are capped at 8 MB, depth 48 and 1,000,000 nodes. Roots and named documents must not be symlinks. Everything below a root is opened one component at a time with `O_NOFOLLOW` and an identity re-check, and private files are never read.

| Option | Contract |
| --- | --- |
| `--review` (required) | Integrated review JSON. Required fields are `date` (timezone-aware ISO 8601) and `passed` (bool). `backend_outcomes` requires integer `collected`, `passed`, `skipped`, `xfailed` and `xpassed`; `failed`, `errors` and `skipped_ids` are optional. `service_reports` maps each report path to `{passed, checks, kind: source/frozen, date, sha256}` with an optional `suite`. `candidate` requires `executable_sha256`, `bundle_sha256`, `files` and `bytes`; `executable` is optional, and Phase 9's `path` contributes only its final name. `source_sha256` maps path to SHA256, and `source_snapshot_sha256` is required. Optional fields: `verification_sha256`, `validator_sha256`, `phaseN_modules`, `ui.source_sha256`, `phaseN_suite_passes`, `backend_tests`, `backend_tests_skipped`, `service_checks`, `source_entry` (default `backend/main.py`) and `source_coverage`. Claim-like keys such as `signed`, `notarized`, `production_ready`, `score`, `human_audit` and `installed_verified` are listed in `ignored_claims` and never used. |
| `--reports-root` (required) | Directory holding the reports named in `service_reports`. Without a `suite` field, a report name must end in `-source.json` or `-frozen.json`. A report must have `passed: true`, a matching `kind`, a non-empty list of unique `checks` and an ISO `date`. It must not list `failures`, `failed_checks`, `failed` or `errors`. Frozen reports need `backend_sha256`. Source reports need a non-empty `source_sha256`. `parent_invocation`, as written by the parent's `scripts/verify_harness_candidate.py`, is validated when present. |
| `--source-root` | Current checkout used to recompute snapshot hashes and coverage. Without it, `source_freshness` is unverified. |
| `--source-snapshot` | The tested-source snapshot file. Its SHA256 must equal `source_snapshot_sha256` and its content must equal `source_sha256`. Without it, the digest must reproduce from `json.dumps(source_sha256, indent=2) + "\n"`, the Phase 9 format. |
| `--candidate-root`, `--candidate-manifest` | The onedir candidate and its complete manifest (`files` of exactly `{path, bytes, sha256}` plus `bundle_sha256`; `executable_sha256`, `algorithm` and `links` are optional). Each is required for `candidate_bundle` to pass. |
| `--artifact-root` | Root for `verification_sha256` paths, which are hashed but never parsed. It is required when the review lists artifacts. |
| `--test-log` | The full verbose pytest log, used for the outcome cross-check. |
| `--evaluation-report` | Repeatable `pebble.harness_evaluation/1` per-arm report from `backend/pebble/evaluation.py`. `evidence` is `live_model` or `scripted_fixture`. Each case's `outcome` is one of `passed`, `failed`, `error`, `timeout`, `cancelled`, `unsupported`, `not_run`, `budget_exhausted`, `solo_violation` or `non_delegated`. `summary.selected`, `summary.passed`, `summary.not_passed` and `summary.outcomes` must agree with the cases; a disagreement fails the gate. Live reports are recorded as `live_model_recorded`, scripted reports as plumbing only, and comparison or other schemas as `unrecognized`. A ledger `signature` is counted but not verified. |
| `--require-suite` | Repeatable suite name that must exist in both modes. |
| `--min-backend-tests` | Explicit floor; no total is built into the validator. |
| `--executable` | Executable path relative to the candidate root. The default comes from the review, then `pebble-backend`. |
| `--native-app` | Owned candidate `.app` with `Contents/Info.plist`. Symlinks and installed locations (`/Applications`, `~/Applications`, `/System`, `/Library`, `/usr`, `/opt`) are refused. |
| `--signing-identity-status` | `available`, `absent` or `unknown`. This is a parent-obtained, read-only summary that is recorded only; this tool never queries identities or reads credential material. |
| `--fixture` | Used alone, it validates a generated synthetic tree. Used with evidence, it marks the run as synthetic. Production readiness stays false either way. |
| `--report` | Writes the full result atomically. It refuses symlinks, names that start with `harness-phase9-` or `.`, non-`.json` names, paths inside the candidate or app, and any existing file that is not an earlier readiness result. |

### Source coverage

`source_coverage` is `{"include": [[folder, [glob, ...]], ...], "fixed": [path, ...], "skip_dirs": [name, ...]}`. Globs match file names only. Any link under a covered folder is an issue; declare `skip_dirs` for directories such as virtual environments. Without a declaration, the Phase 9 default applies: `backend/**/*.py`, `src/**/*.{vue,ts,mjs,css}`, `scripts/**/validate_*.py`, the fixed build inputs and `skip_dirs: ["__pycache__"]`. The Phase 10 review declares `scripts` with `*.py` so that `evaluate_harness.py`, `check_harness_release.py`, `verify_harness_candidate.py` and `benchmark_search.py` are covered:

```json
{"include": [["backend", ["*.py"]], ["src", ["*.vue", "*.ts", "*.mjs", "*.css"]], ["scripts", ["*.py"]]],
 "fixed": ["backend/requirements.txt", "package.json", "package-lock.json", "src-tauri/src/main.rs",
           "src-tauri/Cargo.toml", "src-tauri/Cargo.lock", "src-tauri/tauri.conf.json", ".runtime/pebble-backend.spec"],
 "skip_dirs": ["__pycache__"]}
```

Phase 24 reviews name `build/pebble-backend.spec`, `backend/requirements.lock`, `backend/requirements-dev.txt` and `rust-toolchain.toml` among their fixed inputs instead (`scripts/verify_harness_candidate.py` hashes them; it hashes the two historical `.runtime` inputs only where they exist). The default coverage above is unchanged, because earlier reviews depend on it.

### Candidate bundle digest

The walker reproduces the Phase 8 and Phase 9 algorithm. Each regular file, and each relative file link that resolves inside the root, becomes `{path, bytes, sha256}` of the bytes it names; this is how PyInstaller's `Python.framework` aliases were recorded. Directory links are listed but not descended. Entries are ordered by path component, and the digest is the SHA256 of their sorted-key compact JSON. Absolute, escaping, dangling or looping links, special files, colliding names and private files are issues, and their contents are never read. A case-sensitive file system can hold two spellings that differ only by case, and the walker refuses them. On a case-insensitive volume such as default APFS, both spellings name one file, so the tree cannot contain the collision; a manifest that lists both spellings is refused as a duplicate entry instead. `observed.links_sha256` fingerprints the link inventory; a manifest that adds `links` must match it exactly.

## Commands

Run these from the repository root with the backend interpreter, for example `.runtime/agent-env/bin/python`.

```sh
# Synthetic self-check: expect exit 10, fixture true, production_ready false.
python scripts/check_harness_release.py --fixture

# Phase 9 history: expect exit 20. history.* is true while source_services is unverified, because historical
# source reports carry no source_sha256 and cannot prove the current source ran.
python scripts/check_harness_release.py --review reports/harness-phase9-review.json --reports-root reports \
  --candidate-manifest reports/harness-phase9-candidate-manifest.json --artifact-root .

# Phase 10 (substitute the parent's final file names): expect exit 10 when local evidence is complete.
python scripts/check_harness_release.py --review <phase10-review.json> --reports-root <published-reports-dir> \
  --source-root . --source-snapshot <phase10-tested-source.json> \
  --candidate-root .runtime/harness-candidate/pebble-backend --candidate-manifest <phase10-candidate-manifest.json> \
  --artifact-root . --test-log <phase10-full-tests.log> --evaluation-report <evaluation-report.json> \
  --require-suite <suite> --signing-identity-status absent --report reports/<phase10-release-readiness.json>

# The Phase 10 finalizer runs the Phase 10 check itself, staged before publication, and writes
# reports/harness-phase10-release-readiness.json only if it is local-ready with production_ready false.
python scripts/finalize_harness_phase10.py

# Optional, parent-reviewed, macOS only: read-only checks of an owned candidate .app (never an installed copy).
python scripts/check_harness_release.py ... --native-app <owned-build-dir>/Pebble.app
```

## Native checks

Tools are used only from fixed absolute paths and only if each is a root-owned regular executable that only root can modify: `/usr/bin/codesign`, `/usr/sbin/spctl`, and the Command Line Tools or Xcode `stapler`. `xcrun` is deliberately skipped because its shim can offer to download tools. Each check runs without a shell or stdin, with a minimal `PATH`/`LANG` environment, in its own process group with `cwd=/`, a 60-second deadline and a 64 KB output cap. The output is parsed into kinds and booleans and then discarded; no authority name, team identifier or tool text is reported. A missing tool, timeout or truncated output leaves the gate unverified, and a non-macOS host reports it as unsupported. `spctl` and `stapler` may consult Apple services as part of the operating system's own assessment.

## Limits

- Matching hashes show that the supplied files equal the recorded evidence at check time. They do not prove a reproducible build or that the frozen executable was compiled from the snapshot.
- Synthetic service suites and scripted evaluations validate plumbing, never model quality.
- Production gates without an authenticated attestation channel stay unverified in contract v1. A later contract version needs a pinned verification key or an equivalent channel before any of them can pass.
- Descriptor-anchored no-follow reads narrow check-to-use races with a concurrent writer but do not eliminate them.
- Signing checks cover only the explicit candidate app on this host, not installed copies, update channels or other platforms.
