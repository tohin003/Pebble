# Releasing Pebble

This is the maintainer's procedure for a release. Release history is in [CHANGELOG.md](../CHANGELOG.md); this page
replaces the 0.3 preview's verification notes.

Two rules run through it:

- **Nothing goes public before the person who owns the project says so,** explicitly, for that release. The tools never
  push, create a repository or a release, or upload anything: `scripts/publish_tree.py` and
  `scripts/package_release.py` print the commands, and the maintainer runs them after that go.
- **Verify before publishing, never fix in place.** A published release's assets are never replaced: a mistake is fixed
  by a new release, because people check downloads against `SHA256SUMS`.

## 1. Version and notes

1. `scripts/set_version.py X.Y.Z` (a dry run), then `--write`: the seven declared versions move together.
2. In [CHANGELOG.md](../CHANGELOG.md), finish the `## [X.Y.Z]` section and add the release date (`## [X.Y.Z] - YYYY-MM-DD`).
   `package_release.py` builds the release notes from it.

## 2. Build the candidate

Follow [BUILDING.md](../BUILDING.md) with the project's stable signing identity (`--identity auto`) and the pinned vault
helper, so that updates keep people's Keychain approvals. Then:

1. Regenerate the third-party notices from the exact inputs (BUILDING.md §4).
2. `scripts/release_inventory.py --stage-manifest C/stage/stage-manifest.json --report <new file>` must say `complete`.
3. `scripts/check_harness_release.py` with `--candidate-root C/stage/resources/backend --native-app
   C/release/Pebble.app`: the local gates pass; the Developer ID, notarization and attestation gates stay open and are
   recorded as such ([HARNESS_RELEASE_GATES.md](HARNESS_RELEASE_GATES.md)).
4. Try `C/test/Pebble Candidate.app`, then install over the maintainer's own Pebble with `scripts/pebble_install.py`:
   backup, rehearsal, drill, install and readback ([HARNESS_INSTALL_RUNBOOK.md](HARNESS_INSTALL_RUNBOOK.md)).

## 3. The public tree

`scripts/publish_tree.py` copies only what [publish/manifest.json](../publish/manifest.json) allows into a new folder,
applies its scrubs and runs every gate. It refuses on any hit.

1. Write the private markers file, `.runtime/publish-private-markers.txt`: one string per line that must never be
   published and that no public rule may name (the maintainer's e-mail addresses, private folder and repository names,
   and the development accounts' names). It stays in `.runtime`, which is never published, and the report names a
   marker only by its line number.
2. Put `gitleaks` (8.25 or later) and `shellcheck` on `PATH`.
3. For the clean-clone tests, make a fresh environment from `backend/requirements-dev.txt` (BUILDING.md §3) and pass it
   with `--python`, so the tests run with what a contributor installs.
4. Run:

   ```sh
   .runtime/agent-env/bin/python scripts/publish_tree.py --out <new folder> --report <new file> \
     --inventory-report <the inventory report from §2> --python <fresh environment>/bin/python
   ```

   The gates: no never-published path; `.gitignore` hides no published file; no `/Users/<name>` outside the reviewed
   synthetic names and none of this Mac's paths; no e-mail address outside reserved example domains and the reviewed
   list; no account folder; none of the private markers; gitleaks with [.gitleaks.toml](../.gitleaks.toml) finds
   nothing; `bash -n` and shellcheck pass on `install.sh`; the inventory is complete and was made from the published
   notices and lock files; and every published test file passes in a fresh copy of the tree. Exit 0 means publishable.
5. Read the report. When it is publishable it lists the commands that make the repository's history from that folder:
   a new history, one commit and the version tag. The commit's author address becomes public: use GitHub's noreply
   address unless a public one is wanted.

## 4. The release assets

```sh
.runtime/agent-env/bin/python scripts/package_release.py --candidate C            # the plan; writes nothing
.runtime/agent-env/bin/python scripts/package_release.py --candidate C --execute
```

It refuses a candidate whose app no longer matches `C/app-manifest.json`, checks the signature, zips `Pebble.app` with
`ditto -c -k --sequesterRsrc --keepParent`, checks the zip by unpacking it and hashing the app again, and writes
`Pebble-macos-arm64.zip`, `SHA256SUMS`, `RELEASE_NOTES.md` and `release-manifest.json` into `release/vX.Y.Z/` (which Git
ignores). Read `RELEASE_NOTES.md` before going on.

## 5. Publish, after the explicit go

1. Push the history and the tag (the commands from §3).
2. Create the release with the `gh release create … --verify-tag` command `package_release.py` printed: the zip,
   `SHA256SUMS` and `RELEASE_NOTES.md`, with the notes as the release text.
3. Check what is published: the release's assets and sizes match `release-manifest.json`, and
   `https://github.com/tohin003/Pebble/releases/latest/download/SHA256SUMS` equals the local file.
4. **Before announcing,** run the one-line installer in a new macOS user account (or with a clean staged `HOME`), open
   Pebble once, and run `--uninstall`.
5. On a new repository, turn on private vulnerability reporting (Settings › Code security) so that
   [SECURITY.md](../SECURITY.md) works.
