# Contributing to Pebble

Thank you for helping. Pebble acts on people's projects, accounts and Macs, so changes are held to a few firm rules.
Please read them before you open a pull request.

## Before you start

- **Security problems** go through [SECURITY.md](SECURITY.md), never a public issue.
- **For anything larger than a small fix,** open an issue first and describe the change. Some parts below need a design
  discussion before code.
- Build and test setup: [BUILDING.md](BUILDING.md). How Pebble is put together:
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- By contributing, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE), as its
  section 5 describes. Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Where things are

| Folder | What it holds |
| --- | --- |
| `src/` | The interface: Vue 3 and TypeScript, the main window, the pebble, Settings and the renderers. |
| `src-tauri/` | The native shell (Rust, Tauri 2), its allowlist of routes, and the Keychain helper's C source. |
| `backend/pebble/` | The local service (Python): the agent loop, tools and permissions, memory, providers, storage. |
| `backend/vendor/agentguard/` | AgentGuard, vendored; files Pebble changed are listed in `PROVENANCE.json`. |
| `backend/tests/` | The backend tests, one file per area. |
| `browser-extension/` | Pebble for Chrome and its tests. |
| `scripts/` | Build, sign, install, validation and benchmark tools. |
| `benchmarks/` | The benchmark corpora and drivers. |
| `docs/` | The user guide, data folders, architecture and the build, signing, install and release references. |

## Rules that do not bend

These are Pebble's contracts. A change that would loosen one needs an issue and an agreed design first.

1. **Authorization belongs to Pebble.** Only the person, in the main window, approves effects or widens permissions.
   Nothing a model says, nor a file, a web page, a memory, a skill, a connector or a generated view, can grant access.
   The pet can narrow permissions, never widen them.
2. **Generated UI has no native authority.** Views and mods display data and collect answers; they never call native
   routes, approve or run anything.
3. **Effects are never replayed.** A retry may repeat a model request; it never repeats a tool call whose effect is
   uncertain. Uncertainty is shown, not hidden.
4. **Memory is data.** It informs; it never instructs or permits. Pebble keeps exactly six memory layers (working,
   episodic, semantic, procedural, temporal and profile) with their switches, scopes and forgetting.
5. **No new model tool, and nothing new in small-context requests,** without a design discussion: every tool and every
   line at a 4K budget costs the local models.
6. **Limits stay honest.** Truncation, unknown results and failed checks are reported as such.

## Tests

- **Every change comes with tests.** A bug fix includes a test that fails before the fix and passes after it.
- **Tests never call a model or the network.** Providers are scripted fakes; `backend/tests/conftest.py` refuses
  non-loopback connections and the real Keychain. Tests never touch your `~/.pebble`, `~/Documents/Pebble`, Keychain or
  installed app: use `tmp_path`, fake homes and injected runners.
- **Run tests one file at a time,** the ones your change affects:

  ```sh
  .runtime/agent-env/bin/python -m pytest -c backend/tests/pytest.ini backend/tests/test_<area>.py -q \
    --basetemp "$(mktemp -d /private/tmp/pebble-tests.XXXXXX)/t"
  node --test src/<file>.test.mjs
  npm run test:ui          # every interface and extension test
  ```

  The `--basetemp` under `/private/tmp` matters: pytest's default temp folder is too long for the Unix sockets some
  tests open, and the benchmark sandboxes accept scratch only under `/private/tmp` (see BUILDING.md).

  The whole backend suite takes a long time and a lot of scratch space; the maintainer runs it before each release.
- **Earlier tests and validators are graders, never weakened.** `scripts/validate_*.py` and the tests of earlier work
  pin behaviour people rely on. If your change truly needs one of them to change, say which, why, and how the new
  assertion is at least as strong, in a comment next to it and in the pull request.
- Shell scripts pass `shellcheck`, and the publishing scan (`scripts/publish_tree.py`) must stay at zero findings: no
  personal paths, e-mail addresses or secrets in the tree. Test fixtures use reserved domains such as `example.test`
  and synthetic names.

## Code

- Match the code around you: its naming, comment density and idiom. Python targets 3.14; the interface uses Vue 3 with
  `<script setup>` and TypeScript.
- Write user-facing text in plain words (no internal terms such as "host" or "receipt"); `src/jargon.test.mjs` checks
  the main surfaces.
- **No new dependency** (npm, pip or crate) without an issue first. Every dependency ships in the app and needs its
  licence in the third-party notices (`scripts/release_inventory.py`); GPL and LGPL libraries cannot ship.
- Keep changes focused: one topic per pull request.

## Pull requests

Describe what changed and why, the tests you ran with their last lines, and anything you could not test (for example a
check that needs a real Chrome or a signed build). A maintainer reviews every change against the rules above.
