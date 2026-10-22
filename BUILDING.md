# Building Pebble

This builds Pebble from a clean clone on a Mac with Apple silicon. It is the same path the project uses for its
releases; the maintainer's extra steps (signing, checks, packaging) are in [docs/RELEASE.md](docs/RELEASE.md), and the
builder itself is described in [docs/HARNESS_CANDIDATE_APP.md](docs/HARNESS_CANDIDATE_APP.md).

Nothing here needs administrator rights. The network is needed once, for the downloads in step 2; the build itself
runs offline (`cargo --locked --offline`, no npm install, PyInstaller from a hashed environment).

## What you need

- A Mac with Apple silicon. Pebble 1.0 was built on macOS 27.
- The Xcode Command Line Tools (`xcode-select --install`): `clang`, and the system's `codesign`, `ditto` and
  `sandbox-exec`.
- Node.js for arm64, version 20.19 or later (Vite 7 needs it), with npm.
- Python 3.14 for arm64. The lock pins wheels for 3.14.
- [rustup](https://rustup.rs). The repository pins Rust 1.99.0 in `rust-toolchain.toml`.
- About 10 GB of free space.

## 1. Clone and install the interface tools

```sh
git clone https://github.com/tohin003/Pebble.git
cd Pebble
npm ci
```

`npm ci` installs exactly `package-lock.json`. The build calls `node_modules/.bin/{vue-tsc,vite,tauri}` directly and
never installs anything itself.

## 2. Fetch the inputs that are not in the repository

Each download is checked against a SHA-256 the repository pins. Everything lands in folders Git ignores.

**The tokenizer** (OpenAI's `o200k_base` vocabulary, 3.6 MB; see [NOTICE.md](NOTICE.md)). The app bundles it so that it
never downloads it, and the tests need it too:

```sh
mkdir -p src-tauri/resources/tokenizer
curl --proto '=https' --tlsv1.2 -fsSL https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken \
  -o src-tauri/resources/tokenizer/fb374d419588a4632f3f557e76b4b70aebbca790
shasum -a 256 src-tauri/resources/tokenizer/fb374d419588a4632f3f557e76b4b70aebbca790
# must print 446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d
```

**llama.cpp b11499**, the local model runtime (pinned in `config/model.json`):

```sh
mkdir -p .runtime/b11499
curl --proto '=https' --tlsv1.2 -fsSL \
  https://github.com/ggml-org/llama.cpp/releases/download/b11499/llama-b11499-bin-macos-arm64.tar.gz \
  -o .runtime/llama-b11499-bin-macos-arm64.tar.gz
shasum -a 256 .runtime/llama-b11499-bin-macos-arm64.tar.gz
# must print 5ef4ce6b4b42298f19d1f82e9d53b419bfaaf1fddfbb10adc02a9369e2312997
tar -xzf .runtime/llama-b11499-bin-macos-arm64.tar.gz -C .runtime/b11499
```

The build takes the folder that holds `llama-server` (`--llama-from` below). `python3 scripts/bootstrap_model.py` does
the same download and check, and also downloads the default local model (2.8 GB) into `models/`, which the build does
not need.

**The Python build environment**, from the hashed lock:

```sh
python3.14 scripts/package_backend.py lock-check     # every requirement pinned, every pin hashed
python3.14 scripts/package_backend.py build-env      # creates .runtime/build-env with --require-hashes
```

**The Rust toolchain and crates:**

```sh
rustup toolchain install 1.99.0-aarch64-apple-darwin --profile minimal
cargo +1.99.0 fetch --locked --manifest-path src-tauri/Cargo.toml
```

These use your usual `~/.rustup` and `~/.cargo`; the build is told where they are.

## 3. Run the tests

A developer environment (separate from the build environment, which holds exactly what ships):

```sh
python3.14 -m venv .runtime/agent-env
.runtime/agent-env/bin/python -m pip install -r backend/requirements-dev.txt
```

Run backend tests one file at a time, and the interface and extension tests with Node's runner:

```sh
.runtime/agent-env/bin/python -m pytest -c backend/tests/pytest.ini backend/tests/test_memory_retrieval.py -q \
  --basetemp "$(mktemp -d /private/tmp/pebble-tests.XXXXXX)/t"
npm run test:ui
```

Give each run a new `--basetemp` under `/private/tmp`, as above. pytest's default temp folder (under `/var/folders`) is
too long for the Unix sockets some tests open (macOS allows 104 bytes), and the benchmark sandboxes accept scratch only
under `/private/tmp`. macOS empties `/private/tmp` when it restarts.

Tests never call a model, never reach the network (a test-wide guard refuses it) and never touch the real Keychain;
every provider is scripted. A few tests use real macOS tools that are always present (`sandbox-exec`, `sips`,
`plutil`). See [CONTRIBUTING.md](CONTRIBUTING.md).

## 4. Build

The builder works from a snapshot of the tested source: it refuses to build if any covered file changed after the
snapshot was taken.

```sh
.runtime/build-env/bin/python - > .runtime/tested-source.json <<'EOF'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('verify', 'scripts/verify_harness_candidate.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)
json.dump(verify.sources(), sys.stdout, indent=1, sort_keys=True)
EOF

# The plan: every command, path and environment, and any problem. Writes nothing.
.runtime/build-env/bin/python scripts/build_candidate_app.py --out .runtime/candidate \
  --snapshot .runtime/tested-source.json --llama-from .runtime/b11499/llama-b11499 \
  --rustup-home ~/.rustup --cargo-home ~/.cargo --identity adhoc \
  --node-bin "$(dirname "$(command -v node)")"

# The build: the same command with --execute.
```

The build makes two apps:

- `.runtime/candidate/release/Pebble.app`, identifier `com.pebble.local`, the one that ships;
- `.runtime/candidate/test/Pebble Candidate.app`, identifier `com.pebble.candidate`, with its own data, for trying the
  build without touching your Pebble.

It records every step, digest and log in `.runtime/candidate/inputs.json`, and it refuses (exit 3, with the reason) a
build environment that differs from the lock, a backend that would ship a GPL or LGPL library, and any binary that holds
this Mac's paths.

**Third-party notices.** `src-tauri/resources/licenses/THIRD-PARTY-NOTICES.txt` is generated from the exact inputs for
each release. After changing a lock file, regenerate it with the build environment's Python:

```sh
cargo +1.99.0 metadata --format-version 1 --filter-platform aarch64-apple-darwin --locked --offline \
  --manifest-path src-tauri/Cargo.toml > .runtime/cargo-metadata.json
.runtime/build-env/bin/python scripts/release_inventory.py notices \
  --out src-tauri/resources/licenses/THIRD-PARTY-NOTICES.txt --cargo-metadata .runtime/cargo-metadata.json \
  --cargo-home ~/.cargo
```

It exits 10 and names any component without a licence text.

## 5. Signing

- **`--identity adhoc`** (above) keeps Tauri's ad-hoc signature. The app runs, but macOS treats each rebuild as a new
  app, so it may ask again for Keychain access, and without a pinned vault helper Pebble keeps each secret as its own
  Keychain item.
- **A stable identity of your own.** `scripts/pebble_signing.py create` makes a local code-signing identity in its own
  keychain (a dry run; it tells you what `--execute` would do), and `scripts/build_vault_helper.py` builds and pins the
  vault helper with it. Then build with `--identity auto`. Both are described in
  [docs/HARNESS_SIGNING_IDENTITY.md](docs/HARNESS_SIGNING_IDENTITY.md). A stable identity is still not an Apple Developer
  ID: the app is not notarized.

## 6. Run what you built

Try the test app first; it keeps its data apart from your Pebble:

```sh
open ".runtime/candidate/test/Pebble Candidate.app"
```

To install the release app over an existing Pebble with a backup, a rehearsal on copies and a rollback, use
`scripts/pebble_install.py` ([docs/HARNESS_INSTALL_RUNBOOK.md](docs/HARNESS_INSTALL_RUNBOOK.md)). To package it as the
release zip, see [docs/RELEASE.md](docs/RELEASE.md).

## Working on the interface

The native shell always starts the frozen backend from `src-tauri/resources/backend/`. For `npm exec tauri dev`, put
one there first from a build (Git ignores the folder):

```sh
ditto .runtime/candidate/stage/resources/backend src-tauri/resources/backend
npm exec tauri dev
```

The candidate builder refuses nothing for that folder: it replaces it with its own build.

## Version

`scripts/set_version.py X.Y.Z` shows the seven places the version is declared; `--write` changes them together.
