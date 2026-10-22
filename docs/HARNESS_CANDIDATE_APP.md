# Isolated candidate app builder (D20 §1.6)

`scripts/build_candidate_app.py` builds the Phase 21 candidate `C` (normally `.runtime/phase21-candidate`) from the tested source. It builds two app bundles:

- **`C/release/Pebble.app`**, identifier `com.pebble.local`. It is never launched before install.
- **`C/test/Pebble Candidate.app`**, identifier `com.pebble.candidate`. It has its own data home and is used for the pre-install checks.

The tool writes none of the shared production resources listed in D20 §1.5 (the Phase 20 design study, not published):

- `src-tauri/resources/**`, `dist/**`, `src-tauri/gen/**`, `src-tauri/permissions/**`;
- `build/pebble-backend.spec` (Phase 24; before it, `.runtime/pebble-backend.spec`, which stays only for earlier proofs);
- `src-tauri/target/**`.

Phase 24 (1.0) makes the build reproducible from a clean clone; see "The 1.0 build" below for the lock, the build
environment, the third-party notices, the path remaps and the release checks.

The tool never does any of the following:

- run `scripts/package_backend.py`, call tiktoken, or pass `--specpath` or `--clean`;
- launch a bundle;
- sign with anything but the local identity of `scripts/pebble_signing.py`, or re-sign the pinned vault helper. With `--identity auto` (the default) and that identity present, the stage signs every backend Mach-O and copy-out re-signs both bundles; the pinned helper in `src-tauri/resources/helpers` is staged byte for byte and checked by cdhash (see [signing identity](HARNESS_SIGNING_IDENTITY.md)). Otherwise the only signature is Tauri's ad-hoc one, from `signingIdentity: "-"`;
- delete anything or overwrite evidence. The one exception is the symlinks in its own staged copy of the backend. The stage step replaces each with what it names and records it (see "Symlinks in the bundle"); the PyInstaller output in `C/backend/dist` keeps its links.

Real builds are the parent's step. The tests (`backend/tests/test_build_candidate_app.py`) use an owned checkout in pytest's `tmp_path` and an injected command runner.

## Usage

```sh
# Plan (the default): prints JSON with every command (argv, cwd, complete environment, log path), every path each
# step writes, the two merge configs and any problem. Writes nothing. Exit 0 when clean, 3 with problems.
.runtime/agent-env/bin/python scripts/build_candidate_app.py --out .runtime/phase21-candidate \
  --snapshot .runtime/phase20-tested-source.json --node-bin "$(dirname "$(command -v node)")"

# Build. Creates C with mode 0700 and refuses if it exists. Exit 0 when built; 3 on a refusal, printed as JSON.
... --execute

# Continue a stopped build. Runs only the steps that inputs.json does not record as done.
... --execute --resume [--steps shell-test,copy-out,guard-after]
```

**Options:**

| Option | Meaning |
| --- | --- |
| `--root` | The checkout to build. Defaults to the tool's own checkout. |
| `--steps` | A comma-separated subset of `guard-before,backend,frontend,stage,shell-release,shell-test,copy-out,guard-after`. Steps run in this order, and a step runs only when every earlier step is done. |
| `--llama-from <dir>` | Defaults to `src-tauri/resources/llama`. It may be `.runtime/b11499/llama-b11499`. |
| `--shell-from-copy` | Fallback for open check 1 (see below). |
| `--allow-flattened-links` | See "Symlinks in the bundle". |

**Exit codes:**

| Code | Meaning |
| --- | --- |
| 0 | Built, or a clean plan. |
| 2 | Usage error. |
| 3 | Refused. |
| 1 | Crash. |

**Refusal reports.** Every refusal is `{"refused": <code>, "paths": [...], "detail": ...}`. Paths are exact:
- relative to the checkout when they are inside it;
- relative to the compared tree for bundle and stage differences;
- absolute otherwise.

## Preflight (before C exists; nothing is written on a refusal)

| Check | Refusal |
| --- | --- |
| macOS | `unsupported_platform` |
| C's parent exists. C is under `<root>/.runtime` or outside the checkout. C does not contain the checkout. In merge mode, C's path has no glob characters. C does not exist (or holds `inputs.json` with `--resume`). | `out_parent_missing`, `out_inside_checkout`, `out_contains_root`, `out_path_is_a_glob`, `out_exists`, `resume_without_inputs` |
| The current sources equal `--snapshot`. This uses `scripts/verify_harness_candidate.py`'s own `sources()`, bound to `--root`. | `snapshot_missing`, `snapshot_unreadable`, `snapshot_malformed`, `source_unreadable`, `source_differs_from_snapshot` (with every differing path) |
| The code the tool runs is the tested code: this script, the verifier, `token_budget.py`, `release_readiness.py` and `policy.py` equal their snapshot entries. | `tool_differs_from_snapshot` |
| Every absolute path in the spec is inside `--root`. Phase 24's `build/pebble-backend.spec` has none: its paths are relative to the spec (`SPECPATH`). | `spec_missing`, `spec_outside_root` |
| Phase 24: `backend/requirements.lock` pins every `backend/requirements.txt` pin at the same version and every pin has a SHA-256; `rust-toolchain.toml` names `TOOLCHAIN`'s channel. | `lock_missing`, `lock_unreadable`, `lock_differs_from_requirements`, `lock_unhashed`, `toolchain_pin_differs` |
| These tools exist and are executable: `.runtime/build-env/bin/python` (Phase 24: the environment made from the lock; before, the developer environment); `node`; `node_modules/.bin/{vue-tsc,vite,tauri}`; the pinned toolchain's `cargo` and `rustc`; `/usr/bin/ditto`; `/usr/bin/codesign` | `tool_missing` |
| `src-tauri/resources` holds the staged inputs. It has nothing besides those, the replaced `backend/` and `llama/`, and (optional, Phase 22) `helpers/`, the pinned vault helper, which must equal its manifest and source. The tokenizer's SHA256 equals `token_budget.TOKENIZER_SHA256`, imported. `llama-server` is present. | `resource_missing`, `unexpected_resource`, `tokenizer_mismatch`, `llama_server_missing`, `vault_helper_invalid`, `vault_helper_differs`, `vault_helper_source_differs`, `vault_helper_identity_mismatch` |
| The requested steps follow the done ones in order. | `steps_out_of_order` |

## Environment

Commands get a complete environment built from scratch; nothing is inherited. Variables such as `APPLE_SIGNING_IDENTITY` and `TAURI_SIGNING_PRIVATE_KEY` therefore never reach Tauri.

| Kind | Variables |
| --- | --- |
| Every command | `HOME=C/home` and `TMPDIR=C/tmp/`. These keep PyInstaller, npm, xcrun and the Tauri CLI's temp `.gitignore` out of `~/Library` and the shared temp folder. |
| Node and Python | `PATH=<node-bin>:<toolchain bin>:<root>/.runtime/toolchain/cargo/bin:/usr/bin:/bin:/usr/sbin:/sbin`, `LANG=LC_ALL=en_US.UTF-8` |
| PyInstaller | Adds `PYINSTALLER_CONFIG_DIR=C/cache/pyinstaller`, `PYTHONDONTWRITEBYTECODE=1` (no `__pycache__` in the checkout), `PYTHONNOUSERSITE=1`, `TIKTOKEN_CACHE_DIR=C/cache/tiktoken`, and (Phase 24) `PYTHONHASHSEED=0` and `SOURCE_DATE_EPOCH=1767225600` |
| Shell | The toolchain from `CLAUDE_HANDOFF_CONTEXT.md` §9 and `README.md`, plus the owned target: `CARGO_HOME=<root>/.runtime/toolchain/cargo`, `RUSTUP_HOME=<root>/.runtime/toolchain/rustup` (Phase 24: `--cargo-home` and `--rustup-home` name others, for a clean clone), `RUSTUP_TOOLCHAIN=1.99.0-aarch64-apple-darwin`, `RUSTC=<toolchain bin>/rustc`, `CARGO_TARGET_DIR=C/target`, `CARGO_BUILD_JOBS=2`, and (Phase 24) `SOURCE_DATE_EPOCH` and `CARGO_ENCODED_RUSTFLAGS` with `--remap-path-prefix` for the checkout (`/pebble`), the cargo and rustup homes (`/cargo`, `/rustup`) and C (`/build`), deepest last. The pinned toolchain's `bin` comes before `CARGO_HOME/bin`, so `cargo` is the toolchain binary rather than a rustup proxy. |
| `ditto`, `codesign` | `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, `LANG=C` |

`.runtime/phase19-baseline/pipeline.sh` is not in the fork, so these variables come from the handoff (§9) and the README. The parent should compare them with that script before the first real run.

The frontend tools run as `node_modules/.bin/<tool>`, the local install that `npx <tool>` would pick. Calling them directly means npm can never download a missing package or check for its own updates.

## Steps and evidence

Each step is recorded in `C/inputs.json` under `steps.<step>.attempts[]`, with:
- `started_at` and `finished_at`;
- `status`;
- `logs`: one new file per command in `C/logs/<step>.<attempt>.<label>.log`, holding stdout and stderr;
- `moved_aside`;
- `evidence`, or `failure`.

After every step from `backend` through `copy-out`, the shared paths are re-hashed against step 1 and the sources are re-checked against the snapshot. A change stops the build at the step that made it (`shared_paths_changed`, with the exact `changed`, `added` and `removed` paths).

| # | Step | Commands | Writes | Refuses on | Evidence |
| --- | --- | --- | --- | --- | --- |
| 1 | `guard-before` | `--version` of python, PyInstaller, node, vue-tsc, vite, tauri, cargo, rustc (cwd `C/tmp`) | `inputs.json` | a failing version command | `tool_versions`; `guard.before`. The guard holds content hashes (mode and SHA256, link targets) of `src-tauri/resources/**`, `dist/**`, `src-tauri/gen/**`, `src-tauri/permissions/**` and the spec, plus lstat (type, mode, size, mtime, inode) of `src-tauri/target/release/pebble` and everything under `src-tauri/target/release/bundle`. |
| 2 | `backend` | `python -m PyInstaller --noconfirm --distpath C/backend/dist --workpath C/backend/work <root>/.runtime/pebble-backend.spec` (cwd `C/backend`) | `C/backend/**`, `C/cache/pyinstaller`, `C/backend/candidate-manifest.json` | an unsafe or missing output (`backend_missing`, `backend_unsafe`, including a link the flattened view refuses); a UPX-marked Mach-O (`upx_packed`, exact paths) | executable SHA256; `candidate_manifest` from `verify_harness_candidate.candidate_manifest` (`candidate_bundle_sha256`, file count, bytes, file SHA256; links not followed); the flattened view's `flattened_bundle_sha256` and `flattened_file_count`; `links`, `directory_links` and `links_sha256`; `upx_log` (`available`, `not_available` or `unknown`, from PyInstaller's log); `pillow_heif_files` |
| 3 | `frontend` | `vue-tsc --noEmit`, then `vite build --outDir <abs C/frontend/dist> --emptyOutDir` (cwd root) | `C/frontend/dist/**`, `C/frontend/dist-manifest.json` | no `index.html` | dist digest and file count |
| 4 | `stage` | `ditto C/backend/dist/pebble-backend C/stage/resources/backend`, then in-process flattening of that copy's links | see the next table | see the next table | stage digest, file count, bytes, remaining link count, `flattened_links` count, `stage-manifest.json` SHA256, skipped llama entries |
| 5 | `shell-release` | `tauri build --bundles app --config <abs C/tauri.release.json> -- --locked --offline`. The cwd is the root, or `C/shell-src` in copy mode. | `C/tauri.release.json` (O_EXCL), `C/target/**` | the stage or frontend changed before or after the build (`stage_changed`, `frontend_changed`); the log shows `beforeBuildCommand` ran (`before_build_command_ran`); an unsafe bundle tree | built-bundle digest, merge config SHA256 |
| 6 | `shell-test` | the same, with `C/tauri.test.json` | `C/tauri.test.json`, `C/target/**` | the same | the same |
| 7 | `copy-out` | per bundle: `ditto <C/target/release/bundle/macos/X.app> C/<release\|test>/X.app`, then `codesign --verify --deep --strict` | `C/release/Pebble.app`, `C/test/Pebble Candidate.app`, `C/app-manifest.json` | see the copy-out checks below | `app_manifest_sha256`; each bundle's `app_bundle_sha256` |
| 8 | `guard-after` | none | `inputs.json` | any change since step 1, and any source change | `guard.after`; `result: "built"` |

**Stage step (4) in detail.**
- **What it copies.**
  - It copies `model.json`, `NOTICE.md`, `AGENTGUARD-LICENSE`, `LICENSE`, `tokenizer/` and `licenses/` read-only from `src-tauri/resources/`. From `--llama-from` it copies `llama-server` and `*.dylib`, the same selection as `package_backend.py`. Files are copied by content and mode into new `O_EXCL` files. Links are copied as links and never followed.
  - Phase 22: when the checkout holds `browser-extension/manifest.json`, it copies Pebble for Chrome to `resources/browser-extension` the same way, without its `*.test.mjs` files, and checks each staged file against its source like every other staged file. The backend reports that folder for Chrome's "Load unpacked". The snapshot (`verify_harness_candidate.sources()`) covers `browser-extension/**`.
  - After `ditto-backend`, it **flattens** every symlink in `C/stage/resources/backend`, because Tauri's resource copy drops symlinks (see "Symlinks in the bundle"). A file link becomes a byte-identical new file with its target's mode. A directory link becomes a new directory holding its target's flattened view, with the target's modes. Targets are resolved in the tree as `ditto` wrote it, and read through the descriptor-anchored `release_readiness.Root`.
- **What it checks.**
  - The backend is still the recorded one: both its walker digest and its flattened view's digest and links (`backend_output_changed`).
  - Before any write, every backend link resolves inside `stage/resources/backend`. It must not be absolute, escaping, dangling, looping, or a link to a special file or a private path, and must not be a directory link that expands into itself (`link_cycle`). Private files are never read. Otherwise the step refuses with `stage_link_unsafe`, naming each link by its stage path (`backend/…`).
  - The staged tokenizer has the SHA256 of `token_budget.TOKENIZER_SHA256` (`tokenizer_mismatch`).
  - No other staged link is absolute, escaping, dangling or looping (`stage_unsafe`). These are reported by their source paths, using the `release_readiness` walker's codes.
  - The staged `backend/` equals the **flattened view** of the backend output file for file, in content and mode, with no links on either side. The replaced links must be exactly the output's own links (`staged_backend_differs`; the problems are `missing`, `extra`, `content_differs`, `mode_differs` and `flattened_link_differs`).
  - Every other staged file equals its source (`stage_copy_differs`).
- **What it writes.** `C/stage/resources/**` and `C/stage/stage-manifest.json`, plus `C/shell-src/**` and `C/shell-src-manifest.json` in copy mode.

**Copy-out checks (7).** The step refuses when:
- the copy differs from the built bundle (`copy_differs_from_build`);
- the `Info.plist` identifier is not the expected one (`bundle_identity_mismatch`);
- the bundled `pebble-backend` differs from step 2's executable, or the bundled `backend/` digest differs from step 2's `flattened_bundle_sha256` (`bundled_backend_differs`);
- `Contents/Resources/resources` differs from the stage manifest in presence, content, mode or link (`bundle_resources_differ`, with the exact paths and per-path problems);
- `codesign` fails (`command_failed`, naming its log).

A failing command is refused as `command_failed`, naming its log and exit code.

### Merge configs

Merge mode (the default):

```json
C/tauri.release.json  {"identifier": "com.pebble.local",
                       "build": {"beforeBuildCommand": "", "frontendDist": "<abs C>/frontend/dist"},
                       "bundle": {"resources": {"resources/": null, "<abs C>/stage/resources/": "resources/"}}}
C/tauri.test.json     the same plus "identifier": "com.pebble.candidate", "productName": "Pebble Candidate"
```

`--shell-from-copy`:

```json
C/tauri.release.json  {"identifier": "com.pebble.local", "build": {"beforeBuildCommand": ""}}
C/tauri.test.json     {"identifier": "com.pebble.candidate", "productName": "Pebble Candidate", "build": {"beforeBuildCommand": ""}}
```

In copy mode, the layout of `C/shell-src` supplies the frontend and the resources:
- the unmodified `tauri.conf.json` keeps `frontendDist: "../dist"` and `resources/ → resources/`;
- `C/shell-src/dist` is a `ditto` of `C/frontend/dist`;
- `C/shell-src/src-tauri/resources` is a `ditto` of the stage.

### Manifests (the seams for T2, T3 and T5)

**`C/stage/stage-manifest.json` (`pebble.candidate-stage/1`).**
- `files` and `links` are exactly `release_readiness.bundle_entries` output for `C/stage/resources`.
- `bundle_sha256 = bundle_digest(files)`.
- `release_readiness.read_manifest` therefore accepts the file, which is what `probe_installed_app.py` and `release_inventory.py` use.
- `modes` maps each file path to its octal permission bits. For a file link, these are the target's bits.
- `sources` maps each file and link to its source path.
- `links` holds no `backend/` entries, because the stage flattened them. `flattened_links` lists each replaced link as `{path, target, kind}`, the walker's shape: `target` is the resolved path it named and `kind` is `file` or `directory`.
- It also records `tokenizer`, `backend` and `llama.from`/`skipped`. `backend` holds the executable SHA256, `candidate_bundle_sha256` (step 2's walker digest, links not followed) and `flattened_bundle_sha256`.

**`C/app-manifest.json` (`pebble.candidate-app-manifest/1`).**
- **`apps.release` and `apps.test`** are each a `release_readiness.read_manifest` manifest. T2's `load_app_manifest(path, key)` selects one by key. Each holds:
  - `path`, `identifier`, `product_name`, `info_plist`;
  - `bundle_sha256`, equal to `app_bundle_sha256`, plus `file_count` and `bytes`;
  - `files` and `links` (the walker's output for the whole `.app`);
  - `backend_executable_sha256`, `resources_equal_stage`, `flattened_links`, `codesign_verify_deep_strict`, `launched: false`;
  - `subject`: `{source_snapshot_sha256, candidate_executable_sha256, candidate_bundle_sha256, app_bundle_sha256, platform}`. It is computed from the bundle itself, as `probe_installed_app.compute_subject` does; `platform` is `"<sys.platform>-<machine>"`.
- **The top level** holds:
  - `subject`, which is the release bundle's. T3's `scripts/attest.py --subject-from C/app-manifest.json` reads it.
  - `release_app_bundle_sha256` and `test_app_bundle_sha256`, for T5's `--expect-app-sha256`;
  - the candidate and stage digests. `candidate_bundle_sha256` is the shipped backend's digest, step 2's `flattened_bundle_sha256`. It equals `subject.candidate_bundle_sha256` and what `attest.py`'s fallback reads from the top level. `dist_bundle_sha256` is step 2's walker digest of the PyInstaller output, with links not followed.

  It deliberately has no top-level `files`. A reader that treats a top-level file list as "the" app would otherwise get the release bundle for the `test` key.
- **The definition.** `app_bundle_sha256` is `release_readiness.bundle_digest(release_readiness.bundle_entries(Root(<.app>)))`.

### inputs.json, resume and retries

**What inputs.json holds.**
- Configuration:
  - `root`, `out`, `shell_mode`, `llama_from`, `node_bin` and `allow_flattened_links`;
  - this tool's SHA256;
  - the snapshot path, SHA256 and file count;
  - the imported code's SHA256s.
- `tool_versions`, `guard.before` and `guard.after`.
- One record per step, with its attempts.
- `failure` (the latest) and `result`.

**How it is written.** It is replaced atomically (a new `O_EXCL` temp file, then `rename`). Its history is append-only: attempts are never removed. A copy changed by anyone else is refused (`inputs_changed_externally`), not overwritten.

**How a build resumes.** `--resume` refuses any configuration that differs from the recorded one (`resume_mismatch`, naming the keys). It re-checks the guard and the sources before running anything. It then runs only the steps that are not done.

**How a step is retried.** Before a retry, the step's earlier outputs are renamed into `C/aside/<step>.attempt<N>/`. Nothing is deleted.

**Locking.** An `flock` on `C/.build.lock` refuses a second concurrent build (`build_locked`).

### Symlinks in the bundle

PyInstaller's onedir output holds relative symlinks, and `ditto` keeps them. Examples are `_internal/Python` → `Python.framework/Versions/3.14/Python`, `Python.framework/{Python,Resources,Versions/Current}`, and dylib aliases into `PIL/.dylibs` and `pillow_heif/.dylibs`.

**What happened on the first real build (2026-10-14).** Tauri's resource copy **dropped every symlink**. All 22 stage links in `backend/_internal` were missing from the built `.app`, so the bundled backend could not even load Python, and copy-out refused `bundle_resources_differ` (`link_differs`, with `actual: null`). The installed 0.4.2 app was built by `package_backend.py`, whose `shutil.copytree` follows links. It has no symlinks: `Python` and the dylibs are regular files, `Python.framework/Versions/{3.14,Current}` are both real directories, and `codesign --verify --deep --strict` passes on it.

**The stage therefore flattens the backend** (step 4) into the tree `copytree` makes:
- **Exactness.** `backend_unsafe`, `stage_link_unsafe`, `staged_backend_differs` and `bundled_backend_differs` stay exact against the flattened view of step 2's output. The bundle-against-stage comparison stays exact too, with no backend links on either side.
- **Evidence kept.** The PyInstaller output keeps its links as evidence. The stage manifest lists every replaced link.
- **Digests.** A directory link's files count once flattened, so the shipped backend's digest (`flattened_bundle_sha256`) differs from `candidate_bundle_sha256` (the walker does not descend directory links). The subject uses the shipped one.
- **Size.** The bundle holds each aliased file once per path that names it, as 0.4.2 did.

**Links elsewhere in the stage** (`llama/`, `tokenizer/`, `licenses/`) are not flattened. The default comparison is strict, so a change to them is refused as `link_differs`. `--allow-flattened-links` accepts exactly two transformations, and nothing else:
- a stage file link that became a regular file with the same bytes and mode;
- a stage directory link that is absent, with nothing at or under its path.

A file link Tauri dropped (as it did in the first build) is a missing file, which the flag never accepts. The accepted paths are recorded in `apps.<kind>.flattened_links`.

## Parent checks

### D20 §8 open check 1: Tauri `--config` merge with `"resources/": null` and absolute sources

**Decision.** Make it after the first dry run, a full `--execute` into a scratch C such as `.runtime/phase21-candidate-dryrun1`. Keep that C as evidence. Then:

1. **The dry run reaches `result: "built"`.** Merge mode is verified for this toolchain, because copy-out proved that:
   - each bundle's resources equal the stage file for file;
   - the guard proved no shared write;
   - `before_build_command_ran` did not fire.

   Build the real C in merge mode from the same snapshot.
2. **`tauri build` fails reading the config, or copy-out refuses `bundle_resources_differ`.**
   - A config failure is in `C/logs/shell-release.1.tauri-build.log`.
   - A refusal may list `extra` paths under `backend/` that equal the old `src-tauri/resources/backend`, meaning `resources/` was not dropped.
   - Or it may list `missing` paths, meaning the absolute source was ignored.

   Build a new C with `--shell-from-copy`.
3. **`before_build_command_ran` fires, or the guard reports `dist/**`.** The empty `beforeBuildCommand` was not honoured. Stop and record it; neither mode is safe until the hook is disabled.
4. **Only `link_differs` refusals appear, under symlinks the stage holds.** Since Phase 21 these can only be links outside `backend/`, because the stage flattens the backend's. If each is a file link that became a file, or a directory link dropped, review the listed paths and rebuild in a new C with `--allow-flattened-links`. That flag is a recorded decision, not a default. A `missing` file at the same path means Tauri dropped a file link. The flag does not cover that; flatten the source folder instead (for example, a `--llama-from` folder without links).
5. **`shared_paths_changed` lists `src-tauri/gen/**` or `src-tauri/permissions/**` after `shell-release`.**
   - In merge mode, the shell compiles the checkout's own `src-tauri`. Its `build.rs` (`tauri_build`) therefore rewrites those files in place. The guard tolerates identical bytes but cannot prevent the write.
   - A byte change means the checked-in generated files are stale for this toolchain. The checkout has already been written, so record it as a finding.
   - Then either regenerate deliberately under a new snapshot, or build with `--shell-from-copy`. Copy mode never writes the checkout, because `build.rs` writes into `C/shell-src`.

**Copy mode's own refusals.** These apply in both cases:
- `C/shell-src` was changed by the build (`shell_source_changed`; for example, regenerated `permissions/autogenerated`);
- `build.rs` wrote a `gen/` that differs from the checkout's (`generated_files_differ`).

The second is the copy-mode equivalent of a guarded `src-tauri/gen` change in merge mode.

### D20 §8 open check 8: UPX, and pillow_heif completeness

**UPX.**
- `inputs.json` → `steps.backend.attempts[-1].evidence.upx_log` records PyInstaller's own line.
- Step 2 refuses any Mach-O whose first 64 KiB holds the `UPX!` marker.

**Parent check.**
1. Read `C/logs/backend.1.pyinstaller.log` for "UPX is not available".
2. Run `codesign --verify --deep --strict C/release/Pebble.app`. The tool already ran it in copy-out.
3. Before the release proof, set `upx=False` in the spec. This is a covered-source edit and needs a new snapshot.

**pillow_heif.** Superseded in Phase 24 (V1-045, the person's decision): HEIC and HEIF are decoded by macOS itself, `/usr/bin/sips` run by the confined parser. `build/pebble-backend.spec` excludes `pillow_heif`; `evidence.pillow_heif_files` must be 0, and step 2 refuses any `libx265`, `libde265`, `libheif` or `pillow_heif` file (`copyleft_binary_in_backend`). pillow-heif stays in `backend/requirements-dev.txt` only, as `validate_phase21.py`'s fixture encoder.

## The 1.0 build (Phase 24)

A clean clone builds the same app as the parent's checkout. In order:

1. **The lock.** `backend/requirements.lock` pins all 36 distributions (30 shipped, 6 build tools) under `# [runtime]` and `# [build]`. Hashes need the network, once, by the parent: `python scripts/package_backend.py lock-hashes --write` downloads this Mac's wheel of each pin with `pip download --no-deps --only-binary=:all:` into a temporary folder and records its SHA-256. `package_backend.py lock-check` (offline) shows any requirement the lock misses and any unhashed pin.
2. **The build environment.** `python scripts/package_backend.py build-env` creates `.runtime/build-env` (refusing an existing one) and installs exactly the lock with `--require-hashes --no-deps --only-binary=:all:`. guard-before lists its distributions (`version-distributions`) and refuses any difference from the lock (`build_env_differs_from_lock`): the developer environment, which also holds pytest, FastAPI and pillow-heif, can no longer be frozen.
3. **The notices.** With the build environment's interpreter, `.runtime/build-env/bin/python scripts/release_inventory.py notices --out src-tauri/resources/licenses/THIRD-PARTY-NOTICES.txt --cargo-metadata <(cargo metadata --format-version 1 --filter-platform aarch64-apple-darwin --locked --offline)` (the metadata file may be omitted, which lists every crate of `Cargo.lock`). It exits 10 and names every component without a licence text; a package that ships none gets a name-matched file in `resources/licenses/` (as `eastasianwidth-LICENSE`). Regenerate after any lock change: the inventory reports `notices_inputs_differ` otherwise.
4. **The toolchain.** `rust-toolchain.toml` pins 1.99.0. A clean clone passes `--rustup-home ~/.rustup --cargo-home ~/.cargo` after `cargo fetch --locked` (network, once); the build itself stays `--locked --offline`.
5. **Build** as above. Step 2 refuses GPL/LGPL HEIF libraries. The stage step scans every staged backend and llama file, and each member of `base_library.zip`, for this Mac's paths (the checkout, C, the toolchain homes and `/Users/<name>/`) and refuses any (`machine_paths_in_stage`); copy-out does the same for each shell executable (`machine_paths_in_shell`). The stage manifest's `machine_paths` and each app's `shell_machine_paths` record the counts, including the generic `/Users/` count for review. Compressed PYZ entries inside the executable are not decompressed by this scan (a limit; the parent's `strings`/PYZ check covers them).
6. **Inventory and gates.** `scripts/release_inventory.py --stage-manifest C/stage/stage-manifest.json --report <new>` must say `complete` (exit 0). `release_readiness.release_gates()` then needs that report, `pip-audit -r backend/requirements.lock -f json` and/or osv-scanner output with a triage file, a gitleaks report of the staged tree, and the two manifests (docs/HARNESS_RELEASE_GATES.md).
7. **The DMG.** `scripts/package_dmg.py --candidate C [--execute]` packages only `C/release/Pebble.app`, and only when it still hashes to `app-manifest.json`'s `release_app_bundle_sha256`, with `/usr/bin/codesign`, `/usr/bin/ditto` and `/usr/bin/hdiutil`, and writes `<dmg>.json` with the DMG's SHA-256. The DMG is not signed and the app is not notarized: a Developer ID is an external input (D17).
8. **Version.** `scripts/set_version.py 1.0.0 --write` moves the seven declared fields together (dry run by default).

### HEIC under the parser sandbox (measured)

Every parse gets the deny-default profile of `documents.parser_profile`: Mach lookups only from `PARSER_MACH_SERVICES` (libinfo, notify, cfprefsd daemon and agent, the type map), no IOKit, no network, writes only to `/dev/null`. PDF and document parses (`--extract-pdf`, `--extract-file`) keep exactly that. An **image parse** (`--extract-image`, the only parse with a scratch folder) adds its scratch folder and the set macOS's HEIF decoder (`/usr/bin/sips`) needs:

| Grant | Why |
| --- | --- |
| Mach `com.apple.coremedia.videodecoder` (`IMAGE_MACH_SERVICES`) | the HEVC decoder that decodes the photo's image items |
| Mach `com.apple.bsd.dirhelper` | resolves the per-user temporary folder ImageIO writes into |
| IOKit `IOSurfaceRootUserClient` only (`IMAGE_IOKIT_CLASSES`) | the decoded frames are IOSurfaces |
| `(allow user-preference-read)` (`IMAGE_RULES`) | the CoreFoundation preferences ImageIO reads (no write) |
| `ipc-posix-shm-read-data/-metadata` for `ipc-posix-name-prefix "apple.cfprefs."` | the preferences daemon's shared memory, read only |
| `file-write* file-read*` for `regex #"^/private/var/folders/[^/]+/[^/]+/T/\.?[0-9A-F-]+(-[A-Za-z0-9]+)?$"` | ImageIO's temporary files, which go in the per-user Darwin temp folder whatever `TMPDIR` says: only its direct children with a hexadecimal (UUID-like) name. SBPL regex has no `{n}` counts, hence `+`. Without this rule the decode fails (`sips -s exited 13: Unable to write image to file`). |

**Method (the parent, 2026-10-22, this Mac).** `sandbox-exec` with the image profile and the real `/usr/bin/sips -s format png` on a 640×480 and a 1024×768 HEIC; each denial read with `log show --last 5m --style compact --predicate 'sender == "Sandbox" AND eventMessage CONTAINS "sips"'`; the set grown until both decoded at the right size, then each item removed in turn to keep only what the decode needs. `AGXDeviceUserClient` and `IOSurfaceAcceleratorClient` were measured absent with the decode still succeeding, so they are not granted.

**Still denied, and not needed:** `com.apple.CoreServices.coreservicesd`, `com.apple.tccd.system`, `com.apple.windowserver.active`, DiskArbitration, `opendirectoryd.membership`, logd, powerlog, the `vfs.disk-space` system-info and `/dev/dtracehelper`.

**Pinned by tests (no sips).** `test_parser_sandbox_24.py` pins the image additions by exact text, checks a PDF or document parse's profile has none of them, checks the temp-file regex reaches only hex-named direct children of `T/` (not pytest's folders, nested paths, `C/` or anything outside `/private/var/folders`), refuses any URL-loading, DNS, keychain, TCC, LaunchServices, window-server or AppleEvents name in either list, and measures with `bootstrap_look_up` that those services stay denied under the image profile. The `real_sips` tests and `validate_phase21.py` h1/h2 are the parent's real-tool check after any change.

**If a later macOS needs more,** backend.log carries `sips <step> exited N: <stderr>` from the worker; repeat the method above and add the item to the image-only tuples with its reason, never to the base lists.

### Other checks before relying on a build

- **Step 8 (readiness).** Run:
  ```
  check_harness_release.py … --candidate-root C/stage/resources/backend --native-app C/release/Pebble.app
  ```
  Expect exit 10, with the signing gate failed as `adhoc_signature` (ad hoc) or, for a stable-identity build, `not_developer_id_application`, `gatekeeper_rejected` and `ticket_not_stapled`.
  - **Why the stage.** `C/stage/resources/backend` is the shipped backend: it has no links and equals each bundle's `backend/` file for file (copy-out checks this). The readiness subject's `candidate_bundle_sha256` is the walker digest of `--candidate-root`. The installed-app probe and `app-manifest.json` compute it from the bundled, flattened backend. With `C/backend/dist/pebble-backend`, the walker skips the contents of `Versions/Current`, so the two digests differ and no attestation can bind both.
  - **The frozen review.** Its candidate (`verify_harness_candidate.py --candidate`) must be `C/stage/resources/backend/pebble-backend` for the same reason. Its `bundle_sha256` must equal `app-manifest.json`'s `candidate_bundle_sha256`.
- **Uncovered shell inputs.** `src-tauri/icons/**` and `src-tauri/permissions/**` are not in the tested snapshot. Copy mode records them in `C/shell-src-manifest.json` under `not_in_snapshot`, and the guard hashes `permissions/`.

## Limits

- **Not a reproducible-build proof.** Equality checks bind the bundles to the stage and to the backend step's output. They do not prove that compilation is reproducible. Phase 24 removes the known sources of difference it controls (a hashed lock and a build environment equal to it, a fixed hash seed and archive time, remapped Rust paths); two builds have not been compared byte for byte.
- **Not network-confined.** No step is confined from the network. `cargo` runs `--locked --offline`, and npm is not used. PyInstaller's analysis imports packages, and nothing here could stop a package that fetched data at import.
- **Unguarded writes.** The guard covers the D20 paths, `src-tauri/permissions` and the shared target. Builds can still write elsewhere:
  - `vite` may write a transient bundled config under `node_modules/.vite-temp`;
  - `cargo` updates its registry cache in the shared `CARGO_HOME`.

  Neither path is a production resource, and neither is guarded.
- **Read-to-use races.** Hashes and stats are taken at check time. Descriptor-anchored, no-follow reads (`release_readiness.Root`) narrow the window for a concurrent writer but do not eliminate it.
- **Signing.** Without the local identity, only Tauri's ad-hoc signature (as `tauri.conf.json` specifies). With it, the builder re-signs after Tauri and checks with `codesign --verify --deep --strict`; the pinned vault helper is never re-signed. Open checks: HARNESS_SIGNING_IDENTITY.md.
- **Flattening.** It copies bytes and permission bits only. Extended attributes, ACLs and timestamps of the flattened entries are not copied, which is also true of `copytree`'s directories.
  - It needs write access to the folder that holds each link. PyInstaller writes `0755` folders, and a read-only one would stop the step with a crash record.
  - Links outside `backend/` are not flattened.
  - That the flattened `.app` passes `codesign --verify --deep --strict` is shown by the 0.4.2 app's identical layout and by copy-out's own `codesign` run. The tests cannot show it.
