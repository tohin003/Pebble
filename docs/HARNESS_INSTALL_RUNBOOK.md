# Install, rehearsal and rollback runbook (D20 §2 and §6.7)

`scripts/pebble_install.py` is the one tool the parent uses on this Mac for D20 §2.3–§2.7: back up the installed app and its data, rehearse the first-launch upgrade on a copy, drill the whole procedure on a staged root, install, read back and, if needed, roll back. Two more subcommands only read: `backups` lists every backup and what it can still do, and `purge` lists everything a full removal of Pebble touches. The design is §2, §6.7 and §8 of the Phase 20 design study (D20), a maintainers' record that is not published.

**The 1.0 install (Phase 24).** Every Mac that ran 0.6.x already keeps its data in `~/.pebble/data`, and 1.0 opens it in place. The sequence is: `backup`, `rehearse --data-layout pebble-home`, the drill with `--source-pebble-home` (which `plan` emits), `install`, then `readback --data-layout pebble-home`. If needed, `rollback` restores `~/.pebble` itself in one command (V1-006); see step 12. Until this tool's Phase 24 version is merged, the readiness report must say "manual rollback only".

**Status.** The tool has never been run against this Mac's real paths. Its tests (`backend/tests/test_pebble_install.py`) run only against staged roots under pytest's `tmp_path`, using:
- fake `Pebble.app` trees with real `Info.plist` files;
- a real legacy (epoch 0) data home, upgraded by the repository's own `App`;
- injected process listers, copiers, codesign runners and launchers.

The real launcher, process lister, `ditto` and `codesign --verify` are exercised there only on owned temporary trees. Every real backup, rehearsal, drill, install and rollback is a later parent step, taken only after the user approves.

**Phase 22 (the data home moves into `~/.pebble`).** The Phase 22 shell passes `--home ~/.pebble --legacy-home <old app-data folder>`, and the first launch moves the data home into `~/.pebble/data` once, verified (see [DATA_FOLDERS.md](DATA_FOLDERS.md)). For that install:
- `rehearse`, `drill` and `readback` take `--data-layout pebble-home` (the `plan` runbook names it). The default `legacy` is exactly the earlier behaviour.
- `backup` also copies `~/.pebble` when it exists (to `B/pebble-home`). Take the backup **after** `scripts/pebble_signing.py create` has run, because install refuses a `~/.pebble` that changed since the backup (`pebble_home_unchanged_since_backup`); the copy then holds `~/.pebble/signing/keychain-password`, so keep `B` private.
- After the install, read the installed app's files with `scripts/probe_installed_app.py logs --home ~/.pebble/logs` and `data --home ~/.pebble/data`.
- Rollback follows the home that was live at the backup (`backup.json` `live_home`):
  - `legacy` (the backup was taken before the move): it restores the old app and the old data home and leaves `~/.pebble` untouched (`pebble_home_untouched`).
  - `pebble_home` (every backup taken on a Mac that ran 0.6.x): it restores `~/.pebble` from `B/pebble-home` and leaves the old folder untouched (step 12). Until Phase 24 this was refused with `rollback_supported`.
- Install Pebble for Chrome's native host (`scripts/install_native_host.py`) only after the first launch moved the data, so its launcher names `~/.pebble/data`.

## 1. Safety model

- **Dry run by default.** A subcommand without `--execute` runs its read-only preconditions and prints, as JSON, every action it would take with exact paths. It changes nothing; the tests compare full snapshots before and after.
- **`--execute` acts.** On real paths (no `--root`) it also needs:
  - `--i-am-the-parent-with-user-approval <stamp>`, naming the backup stamp of this run;
  - every `--quiet-file` to be quiet.
- **`--quiet-file PATH`** (repeatable) names a live trial or worker marker, such as a ledger, a stream log or a lock. It counts as quiet when it is absent, or when it was last modified at least `--quiet-seconds` ago (default 300).
- **Each step re-checks.** Every step re-checks its own preconditions and stops at the first mismatch. The report names the failed check and its exact paths.
- **Nothing is deleted:**
  - a replaced bundle, data home or webview store is renamed into the backup root `B`;
  - renames use `renamex_np(RENAME_EXCL)`, which can never replace an existing path, and refuse to cross volumes;
  - every record is a new file opened with `O_EXCL`;
  - `ditto` never copies into an existing path;
  - the tests scan the source for deletion, replacement and signal calls, and record any made at run time.
- **No signals.** "Pebble is running" means a process whose executable lies inside the target bundle (`…/Pebble.app/Contents/…`). Executables are read by exact path from libproc `proc_pidpath`, never by name or a stored PID.
  - A similar name elsewhere (`Pebble.app.old`, `Pebble Candidate.app`, the candidate in `.runtime`) does not count.
  - The tool waits up to `--wait-seconds` (default 30, at most 900), then refuses and lists what still runs. Quitting is the user's action.
  - The only signal the tool can ever send is SIGKILL to its own rehearsal child, through that child's live, unreaped `Popen` handle, after the launch timeout.
- **Service lease.** As a second guard, `runtime-owner.lock` and `upgrade-owner.lock` in the data home are probed with a non-blocking shared `flock`. The files are opened read-only and never created, and a held lock refuses the step.
- **SQLite never opens the live home while its service is stopped.** A read-only open of a WAL database still creates `-wal` and `-shm` files; this was observed with `migrations.inspect` on a closed test home. Therefore:
  - the backup runs `PRAGMA integrity_check` on its copy with `mode=ro&immutable=1`;
  - it runs `migrations.inspect` on a scratch copy of `pebble.sqlite` and `upgrade-backups/` in `B/inspection/`;
  - `plan` reads the 100-byte SQLite header directly;
  - readback inspects the live home only while the new app runs, and a dry run skips that step.
- **No content leaves the data.** Reports carry paths, hashes, counts, versions and states only. Backend responses are reduced to their shape (`state` keys, whether a response came back, known error strings), and other error text is replaced by its hash.

## 2. Paths

| Name | Real path | With `--root R` |
| --- | --- | --- |
| Installed bundle | `/Applications/Pebble.app` | `R/Applications/Pebble.app` |
| Data home `H` | `~/Library/Application Support/com.pebble.local` (`main.rs:247`) | `R/home/Library/Application Support/com.pebble.local` |
| Webview store `W` | `~/Library/WebKit/com.pebble.local` (D20 §8 open check 3; change with `--webview-store ~/…`) | `R/home/Library/WebKit/com.pebble.local` |
| `~/.pebble` | `~/.pebble` (`main.rs` `pebble_home()`) | `R/home/.pebble` |
| Backups | `~/PebbleBackups` | `R/home/PebbleBackups` |
| Backup root `B` | `~/PebbleBackups/<stamp>-pre-install.noindex` (mode 0700, created new) | same, below `R/home` |
| Rehearsal | `/private/tmp/pebble-rehearsal-<now>` (or `--rehearsal-dir`) | `R/pebble-rehearsal-<now>` |
| Drill (as `plan` writes it) | `/private/tmp/pebble-drill-<stamp>` | the same |

With `--root`, `Path.home()` is never consulted. The tests patch it to an empty sentinel that must stay empty. `--root /` is refused.

**Names (V1-137, V1-173).** From Phase 24 a backup root ends in `.noindex`, so Spotlight does not index it, and the bundle copy is `Pebble.app.backup`, which Finder and LaunchServices do not treat as a launchable app. Roots written by Phases 21–23 (`<stamp>-pre-phase21`, bundle copy `Pebble.app`) still load: `backup.json` `app.copy` names the bundle copy. A stamp that exists under both names is refused (`backup_root_unique`).

### What `B` holds

| Entry | Written by |
| --- | --- |
| `Pebble.app.backup` (`Pebble.app` in Phase 21–23 roots) | backup: `ditto` of the installed bundle |
| `com.pebble.local` | backup: `ditto` of `H`, the rollback source while `H` is live. Once `~/.pebble` is live, `H` is frozen; when an earlier root holds a verified, identical copy of it, this copy is not made again, and `backup.json` `data_home.reused` names that root (V1-137). |
| `pebble-home` | backup: `ditto` of `~/.pebble`, when it exists; the rollback source while `~/.pebble` is live |
| `webview-store` | backup: `ditto` of `W`, when present |
| `inspection/` | backup: scratch database copy that `inspect` read (it gains `-wal` and `-shm`) |
| `backup.json` | backup record (`pebble.install-backup/1`) |
| `backup-run-<t>.json` | backup run record |
| `rehearsal-<t>.json` | rehearse run record |
| `install-<t>.json` | install record: candidate tree, log offsets, release manifest |
| `Pebble.app.moved` | install: the old bundle, moved aside |
| `readback-install-<t>.json`, `readback-rollback-<t>.json` | readback records |
| `rollback-<t>.json` | rollback record |
| `Pebble.app.failed-<t>` | rollback: the failed bundle, moved aside |
| `com.pebble.local.after-<t>` | rollback (old data home live at the backup): the post-install data home, moved aside |
| `pebble-home.after-<t>` | rollback (`~/.pebble` live at the backup): the post-install `~/.pebble`, moved aside |
| `pebble-home.mirror-<t>.json` | rollback (`~/.pebble` live): the restored Documents copy index, moved aside |
| `webview-store.after-<t>` | rollback: the post-install webview store, moved aside |

A stopped or refused run that changed something still keeps its record. A failed readback always keeps its record. A refusal that changed nothing writes nothing.

## 3. Output and exit codes

Each run prints one JSON report (`pebble.install-run/1`). Its fields:
- `command`, `execute` and `state` (`dry_run`, `done`, `refused` or `stopped`);
- `checks`: every check with `ok` and its facts;
- `actions`: every step with `op`, its paths, its `kind` (`read`, `touch` or `write`) and its `state` (`planned`, `done`, `failed`, `skipped`…);
- `results`, `error` (`code`, `message`, facts), `recovery` (what to do after a stop) and `record` (path and SHA256 of the kept record).

Long lists such as file manifests are printed as counts; the kept records hold them in full.

| Exit | Meaning |
| --- | --- |
| 0 | Done, or a dry run whose preconditions hold |
| 2 | Usage error |
| 3 | Refused before any change |
| 4 | Stopped after a change; follow `recovery` (usually: roll back) |

## 4. The parent's sequence

Run everything from the main checkout root, with the toolchain environment of `CLAUDE_HANDOFF_CONTEXT.md` §9. No live trial and no development worker may be running.

```sh
PY=.runtime/agent-env/bin/python
T=scripts/pebble_install.py
S=$(date -u +%Y%m%dT%H%M%SZ)          # the backup stamp; record it in the parent review
OLD=<installed backend SHA-256>         # plan's results.observed.installed.backend_sha256, checked against the release record
C=<candidate folder>                   # built and checked with T1's tool (D20 §1.6)
CAND="$C/release/Pebble.app"
MAN="$C/app-manifest.json"
B="$HOME/PebbleBackups/$S-pre-install.noindex"
DRILL="/private/tmp/pebble-drill-$S"
Q=(--quiet-file <each live trial or worker marker>)
GO=(--execute --i-am-the-parent-with-user-approval "$S")
```

**0. Preconditions** (D20 §2.5 step 1). All of these must hold:
- The final candidate passed §1.6 step 8.
- Its version was bumped above 0.4.2. `install` refuses a version that is not higher (`version_higher`), and the source is still labelled 0.4.2 (D20 finding 2).
- The user is present and has approved the backup and the install.
- Disk space covers, at a minimum:
  - the backup: |app| + |H| + |W| + |database|, plus a later rehearsal copy of |H|, plus `--margin-bytes` (default 1 GiB);
  - the drill: about 3 × (|app| + |H| + |W|) + 2 × |candidate|.

  Each command checks its own need.

**1. Survey (read-only).**
```sh
$PY $T plan --stamp "$S" --expect-backend-sha256 $OLD --candidate "$CAND" --app-manifest "$MAN"
```
Read the `results.observed` fields:
- `processes_inside_bundle` must be empty;
- `installed_backend_matches` must be `true` (open check 4);
- `data_home.database.user_version` is the installed epoch;
- `data_home.sidecars` shows whether `-wal` is empty;
- `webview_store.exists` answers open check 3;
- `free_bytes`.

`results.steps` lists these same commands with paths filled in.

**2. User: quit Pebble.** Use the tray's *Quit Pebble* and confirm the dialog (`main.rs:666-669`). A finished job's supervisor may linger; the tool waits and reports, and never signals it.

**3. Backup (§2.4).**
```sh
$PY $T backup --stamp "$S" --expect-backend-sha256 $OLD "${Q[@]}"            # dry run
$PY $T backup --stamp "$S" --expect-backend-sha256 $OLD "${Q[@]}" "${GO[@]}"
```
1. **Preconditions:**
   - no process inside the bundle;
   - no held lease;
   - `Info.plist` identifier is `com.pebble.local` and the executable is `pebble`;
   - the backend SHA256 equals `$OLD`;
   - `B` does not exist;
   - `H` is a real directory with a valid `pebble.sqlite`, an empty or absent `-wal`, and no `-journal`;
   - there is enough free space.
2. **Bundle:** `ditto` the bundle to `B/Pebble.app`. The two `bundle_entries` manifests must be equal (the T1 walker), and both copies must pass `codesign --verify --deep --strict`.
3. **Data home:** `ditto` `H` to `B/com.pebble.local`. The two full tree manifests must be equal: bytes, SHA256, modes, directories and link targets, private files included. Then run `integrity_check` on the copy (immutable). The copy is unchanged afterwards.
4. **Webview store:** `ditto` `W` to `B/webview-store` when present, with equal manifests.
5. **Inspect:** run `migrations.inspect` on `B/inspection` and record its row counts, epoch and upgrade-backup ids.
6. **Record:** write `B/backup.json`. Record `results.backup_json_sha256` in the parent review.

If `-wal` holds frames, the backup refuses with `wal_checkpointed`. Pebble did not checkpoint on quit: ask the user to start and quit it once more, then retry with a new stamp.

**4. Rehearse (§2.3).**
```sh
$PY $T rehearse --stamp "$S" --candidate "$CAND" --app-manifest "$MAN" "${Q[@]}"
$PY $T rehearse --stamp "$S" --candidate "$CAND" --app-manifest "$MAN" "${Q[@]}" "${GO[@]}"
```
1. **Checks before launch:** re-hash `B/com.pebble.local` against `backup.json`, and the candidate against `app-manifest.json`.
2. **Copy:** `ditto` the data copy into a new rehearsal home.
3. **Launch:** run `"$CAND"/Contents/Resources/resources/backend/pebble-backend --home <copy> --resources <bundle resources>` once:
   - under `sandbox-exec` with the profile in §6;
   - with the shell's environment allowlist (`main.rs:258`) and `TMPDIR` inside the rehearsal directory;
   - with stderr written to `<rehearsal>/backend.stderr.log`.
4. **Requests:** send `state` and `upgrade_backups`, close stdin, and wait at most `--launch-timeout` (default 180 s). A started App answers `upgrade_backups` with `Unknown request.`; only the maintenance service serves that route (`maintenance.py:17`).
5. **Candidate unchanged:** re-hash the candidate to show the launch did not change it.
6. **Inspect the copy:** run `inspect(copy, deep=True)`. The pass criteria:
   - state `current`, epoch 9, `upgrade_history` ≥ 1, integrity `ok`;
   - every pre-existing table's row count equals `backup.json`, apart from exact accepted changes (§7);
   - every new upgrade backup passes `verify_backup`.
7. **Restore check:** `restore_copy` the newest new backup into `<rehearsal>/restored`. Its row counts must equal `backup.json`.

`results.migrations_sha256` records which `migrations.py` judged the copy. It is the repository source the tool imports, not the frozen candidate's copy; compare it with the candidate's source snapshot.

**5. Drill (§2.7, on a staged root).** Copy the drill command from `plan`'s `results.steps`: it follows `backup.json` `live_home` (`results.runbook_basis`). For the 1.0 install over 0.6.x, `~/.pebble` was live, so it is:
```sh
$PY $T drill --root "$DRILL" --source-app "$B/Pebble.app.backup" --source-pebble-home "$B/pebble-home" \
  --source-home "$B/com.pebble.local" --source-webview "$B/webview-store" --candidate "$CAND" --app-manifest "$MAN" \
  --expect-backend-sha256 $OLD --data-layout pebble-home --execute
```
- `--source-home` is the frozen old folder beside `~/.pebble`. Omit it when the backup recorded none; when `backup.json` `data_home.reused` names an earlier root, use that root's `com.pebble.local` (`plan` does).
- Omit `--source-webview` when no store was backed up.
- For a backup taken while the old data home was live, the drill instead stages `--source-home "$B/com.pebble.local"` alone and the candidate moves it on first launch.

The drill:
1. refuses an existing `$DRILL`;
2. refuses a `--source-home` that the move already left behind (it holds `MOVED.md` and `pebble-moved.json`) or that is a copy of `~/.pebble/data` (the move receipt), unless `--source-pebble-home` is given (`drill_source_not_moved`). Both drilled a different start than 1.0's: the first opened an empty database, the second a fresh move (V1-044);
3. stages copies of the backed-up bundle, `~/.pebble`, old folder and store, reading them only;
4. runs backup, then install of the candidate, then a first launch of the installed backend as the shell starts it (`--home ~/.pebble --legacy-home <old folder>`, sandboxed as in step 4, with any Documents folder that the staged `settings.json` names write-protected), then a data readback (with `~/.pebble` live: migration state `current`), then rollback;
5. requires the restored bundle, `~/.pebble` (apart from its Documents copy index, which the rollback moves aside), old folder and store to equal the staged originals byte for byte;
6. requires the failed candidate bundle and the post-install home to be kept beside its backup.

Its record is `"$DRILL/drill-report.json"`; `results.first_launch.move.state` shows how the candidate opened the data. `--no-launch` skips the first launch. The drill root holds a copy of `~/.pebble`, including `signing/keychain-password`: it is 0700, and is released with the backup.

**6. Optional: signed release manifest (§6.7).** It is T3's `pebble.release/1` (exactly `attestation.RELEASE_KEYS`; write it with `scripts/attest.py release-payload`):
```json
{"format": "pebble.release/1", "version": "…", "build": 1,
 "app_bundle_sha256": "<app-manifest bundle_sha256>", "backend_executable_sha256": "<candidate backend>",
 "min_data_epoch": 0, "data_epoch": 9, "previous": "$OLD", "rollback": "<sha256 of B/backup.json>",
 "issued_at": "<UTC stamp>", "not_after": "<at most 30 days later>"}
```
The user signs it with the release key and assembles T3's envelope (`pebble.attestation/1`, `attest.py envelope`). `install` has T3's `verify_release_manifest` decide the envelope with every fact it needs from this install, and stops with T3's refusal code (for example `unknown_key`, `previous_mismatch`, `version_not_higher`). The shipped roots are empty, so under `--require-signed` every envelope is refused (`unknown_key`) until a release key is pinned.

**7. User: approve the install.** Pebble must still be quit.

**8. Install (§2.5).**
```sh
$PY $T install --stamp "$S" --candidate "$CAND" --app-manifest "$MAN" --expect-backend-sha256 $OLD \
  --rehearsal-report "$B/rehearsal-<t>.json" --drill-report "$DRILL/drill-report.json" \
  [--release-manifest <signed manifest> --require-signed] "${Q[@]}"              # dry run, then add "${GO[@]}"
```
1. **Not running:** no process from the bundle; no held lease.
2. **Re-hashes:**
   - `B/Pebble.app`, `B/com.pebble.local` and `B/webview-store` equal `backup.json`;
   - the installed bundle equals `backup.json`, and its backend equals `$OLD`;
   - `H` and `W` are unchanged since the backup. Otherwise a rollback would lose the change, so the install refuses; take a new backup.
3. **Candidate:**
   - it equals `app-manifest.json`;
   - its identifier is `com.pebble.local`;
   - `codesign --verify --deep --strict` passes;
   - its version is higher than the installed one.
4. **Release manifest, when given:** a signed one must be accepted by T3's verifier (signature, pinned release key, lifetime, `app_bundle_sha256`, `backend_executable_sha256`, `previous`, `rollback`, a version higher than the installed one, and the backed-up epoch within `[min_data_epoch, data_epoch]`); then, signed or not, `app_bundle_sha256`, `backend_executable_sha256`, `previous`, `rollback`, `version` (equal to the candidate's and higher than the installed one), `data_epoch` (9) and `min_data_epoch` ≤ the backed-up epoch.
5. **Reports:** on real paths, the rehearsal report must have passed and be bound to this `backup.json` and candidate. The drill report must have passed and be bound to this candidate and `$OLD`.
6. **Move aside:** the name `B/Pebble.app.moved` must be free, and the bundle and `B` must be on the same volume. Move the bundle to `B/Pebble.app.moved` (a rename) and check the moved backend hash.
7. **Copy and verify:**
   - `ditto` the candidate to `/Applications/Pebble.app`;
   - the installed tree must equal `app-manifest.json`, with the candidate's backend;
   - `codesign --verify --deep --strict` must pass.
8. **Record:** `B/install-<t>.json` keeps the candidate tree and the sizes of `service.log` and `backend.log`.

An interrupted copy stops with `copy_interrupted` (exit 4). The report confirms `B/Pebble.app.moved` was re-hashed intact, and `recovery` gives the exact rollback command. The partial bundle is moved aside by that rollback, never deleted.

**9. User: first launch.** The user opens `/Applications/Pebble.app` and answers any Keychain, notification or permission prompts (§5). Then they check their chats, memories, model and effort, and permissions (`RELEASE_V042.md:23`). Provider sign-in status is checked in Settings without starting a run.

**10. Readback (§2.6), while the app runs.**
```sh
$PY $T readback --stamp "$S" --data-layout pebble-home "${Q[@]}" "${GO[@]}"
```
With `--data-layout pebble-home` it reads `~/.pebble/data` (state `current` when `~/.pebble` was live at the backup; `migrated` after a move), the logs in both homes, and the old folder (unchanged apart from `MOVED.md`). Without it, the checks below apply to `H`.
Readback passes only when all of these hold:
- **Processes:** exactly one `Contents/MacOS/pebble` and one `pebble-backend` service from the installed bundle. Service and worker are told apart by argv (`KERN_PROCARGS2`), read only for Pebble's own executable names.
  - Worker modes of the same executable (`--mcp-stdio-worker`, `--job-anchor` and the others in `backend/main.py`) are listed, not counted.
  - An unreadable argv counts as a service, so the check stays fail-closed.
  - Similar names outside the bundle are listed.
- **Logs:** no `Traceback (most recent call last)` in `service.log` or `backend.log` after the install's recorded sizes.
- **Bundle:** the installed tree equals `app-manifest.json`, re-read and required to have the same SHA256 the install checked.
- **Data:** `inspect(H)` reports `current`, epoch 9 and `upgrade_history` ≥ 1, and the pre-existing row counts equal `backup.json` (exact acceptances as in §7).
- **Upgrade backup:** at least one new `upgrade-backups/` entry exists, and `verify_backup` reports every new one valid.

The record `B/readback-install-<t>.json` is kept even when readback fails. Then run the installed checks of D20 §4, the ★ rows, with T5's probes.

**11. LaunchServices** (§2.5 step 6, read-only). Check that `com.pebble.local` resolves to `/Applications`:
```sh
mdfind 'kMDItemCFBundleIdentifier == "com.pebble.local"'
```
If a build copy (a candidate folder in `.runtime/…`, `src-tauri/target/…`, or a `Pebble.app` inside a Phase 21–23 backup root) is listed, `open -b com.pebble.local` and a banner's Open action may pick it. Phase 24 roots are `.noindex` and hold `Pebble.app.backup`, so they are not listed. Unregistering it (`lsregister -u <path>`) is a LaunchServices write, so decide it with the user. This tool does not do it.

**12. Rollback (§2.7).** Triggers: any readback failure, a startup refusal, or the user's request.
```sh
# user: quit Pebble
$PY $T rollback --stamp "$S" "${Q[@]}"                     # dry run
$PY $T rollback --stamp "$S" "${Q[@]}" "${GO[@]}"
# user: open the restored Pebble
$PY $T readback --stamp "$S" --mode rollback --data-layout pebble-home "${Q[@]}" "${GO[@]}"
```
The rollback restores the home that was live at the backup. **When `~/.pebble` was live** (`backup.json` `live_home: pebble_home`, the 1.0 install over 0.6.x; V1-006):
1. **Checks:** no process from the bundle; no held lease in the old folder or `~/.pebble/data`. The bundle copy (or `B/Pebble.app.moved` with `--bundle-source moved`), `B/pebble-home` and `B/webview-store` re-hash equal to `backup.json` before anything moves. The database restored must be the one in the recorded `ditto` copy of `~/.pebble`. The old folder's copy is not needed and not checked.
2. **Move aside:** the installed bundle goes to `B/Pebble.app.failed-<t>`, and `~/.pebble` to `B/pebble-home.after-<t>` (one rename on one volume).
3. **Restore `~/.pebble`:** `ditto` `B/pebble-home` to `~/.pebble` and re-hash it against `backup.json` (`restored_pebble_home_matches`).
4. **Reset the Documents copy index:** rename the restored `~/.pebble/runtime/mirror.json` to `B/pebble-home.mirror-<t>.json`, then check that `~/.pebble` equals the backup without it (`mirror_index_reset`). The restored Pebble starts a new index of `~/Documents/Pebble`, so a Memory or conversation file that the newer version wrote is never read as a deletion (`data_mirror.py` forgets what goes missing from an index it knows).
5. **Restore the bundle and the webview store** as below, and re-hash both.
6. **The old folder** is never touched; its shape is compared before and after (`old_folder_untouched`).
7. **No Keychain item is read or changed.** The vault item is shared by both versions and is not in the backup, so the newest sign-in stays. If 1.0 shipped a re-pinned vault helper (D5), the restored helper keeps the access given to it before.
8. **After the user opens the restored app:** `readback --mode rollback` follows `backup.json` by itself and reads `~/.pebble/data` (`results.restored_home: pebble_home`): one shell and one service; the bundle and backend equal `backup.json`; the backed-up epoch and row counts; the move record says `current`; the old folder is unchanged; no new tracebacks after the backed-up log sizes in both homes.

**When the old data home was live** (a backup taken before the move):
1. **Checks:** no process from the bundle and no held lease. The bundle copy (or `B/Pebble.app.moved` with `--bundle-source moved`), `B/com.pebble.local` and `B/webview-store` re-hash equal to `backup.json` before anything moves.
2. **Data source:** the data source must be the recorded `ditto` copy. An `upgrade-backups/` SQLite file is never put into a live home.
3. **Move aside:** the installed bundle goes to `B/Pebble.app.failed-<t>`, and `H` to `B/com.pebble.local.after-<t>`.
4. **Restore data, then bundle:**
   - `ditto` `B/com.pebble.local` to `H` and re-hash it against `backup.json`;
   - then restore the bundle by `ditto` from the bundle copy (default), or by renaming `B/Pebble.app.moved` back with `--bundle-source moved`;
   - re-hash the bundle against `backup.json`, check its backend is `$OLD`, and run `codesign --verify`.
5. **Webview store:** if one was recorded, move the current store aside to `B/webview-store.after-<t>`, then restore and re-hash it.
6. **After the user opens the old app:** `readback --mode rollback` checks:
   - one shell and one service from the restored bundle;
   - its tree and backend equal `backup.json`;
   - the backed-up epoch;
   - unchanged row counts;
   - no new tracebacks since the backed-up log sizes.

The old binary is never relied on to refuse epoch 9: data is restored before the bundle. Keychain items are never modified by this tool. A rollback that stops can be rerun: whatever is in place is moved aside again under a new stamp.

**13. Retention.** Keep `B` until the user releases it in writing (D20 §7 step 8). Keep the rehearsal and drill directories until the parent review records them. Removing any of them is a separate, logged parent decision; the tool never removes anything.

**14. Listing backups (read-only, V1-137).**
```sh
$PY $T backups [--verify]
```
For every root in `~/PebbleBackups` (both namings), newest first: the stamp, the naming, the size, `live_home`, the versions (backed up, and the one installed after it when an install record exists), whether each copy is present and intact, whether a rollback of that stamp can still run (`rollback.capable`, `restores`, `missing_or_changed`, the command), whether it was rolled back, whether it holds launchable bundles or is indexed by Spotlight, whether it holds the signing keychain's password, and which other roots use its old-folder copy (`old_folder_copy_used_by`). "Intact" compares shapes (paths, sizes, modes, links) by default; `--verify` re-hashes every copy. It has no `--execute` and needs no approval. The person releases a backup by moving its folder to the Trash in Finder; keep the newest rollback-capable backup of the installed version until they are sure of it.

**15. A full removal (read-only list, V1-061).**
```sh
$PY $T purge
```
It lists, with sizes, every file and folder a full removal touches: the app, `~/.pebble`, the old folder, the webview store, `~/Documents/Pebble` (and a folder that `settings.json` names), `~/PebbleBackups`, Pebble's caches, web storage, window state and preferences in `~/Library`, Chrome's native host manifests, the signing keychain, and leftover rehearsal and drill folders. It names, without querying them, the Keychain items (`com.pebble.local.vault` and the kept per-item services) and the System Settings entries (login item, notifications, privacy grants), plus the Chrome extension. It gives the order (quit, `install_native_host.py uninstall`, `pebble_signing.py remove`, then the app and the folders). It never deletes, moves or reads a Keychain item, and it has no `--execute`.

## 5. The user's steps

| When | The user | Watch for |
| --- | --- | --- |
| Before backup, install and rollback | Quits Pebble from the tray and confirms. | Background schedules ask for confirmation first (`main.rs:228-243`). |
| Before backup | Approves the backup and the install, once and together (D20 §7 step 4). | The parent passes the stamp in `--i-am-the-parent-with-user-approval` only after this. |
| If the backup refuses `wal_checkpointed` | Starts and quits Pebble once more. | |
| Rehearsal and drill | May stay away: the profile denies Keychain services, so no prompt appears (open check 2). | Only with `--allow-keychain` must the user be present to answer, preferably *Deny*. |
| First launch after install | Opens the app and answers the prompts. | A Keychain prompt for the new ad-hoc backend on `com.pebble.local.providers`, `.connectors` or `.plan-credentials`. *Allow* or *Always Allow* keeps the provider signed in; *Deny* leaves it signed out, and nothing is lost. |
| After the first launch | Checks chats, memories, model, effort, permissions and provider sign-in (without starting a run). | Anything missing triggers a rollback. |
| ★ checks (D20 §4) | Toggles the login item in Settings, logs out and in, then toggles it off. | `enabled` or `requires_approval` (System Settings › General › Login Items), a start at login, and `not_registered` afterwards. |
| ★ checks | Confirms notifications are still allowed (System Settings › Notifications › Pebble) and sees one test banner. | Authorization is per bundle identifier. |
| ★ checks (D04) | Re-grants Accessibility and Screen Recording if Pebble lists them (Privacy & Security). | Ad-hoc grants go stale after each rebuild (D20 §3). |

## 6. The rehearsal sandbox profile

The real launcher writes this profile (`sandbox_profile()`) to `<rehearsal>/sandbox.sb` with `O_EXCL` and runs `/usr/bin/sandbox-exec -f <profile> <backend> --home … --resources …`. Paths are emitted as Seatbelt string literals with only `\` and `"` escaped; JSON `\u` escapes broke Seatbelt in 0.4.2. Each protected path appears both as given and under `/System/Volumes/Data`.

```
;; Pebble install rehearsal (D20 2.3, scripts/pebble_install.py).
(version 1)
(allow default)
(deny network*)
(allow network* (local ip "localhost:*"))
(allow network* (remote ip "localhost:*"))
(deny mach-lookup (global-name "com.apple.SecurityServer"))
(deny mach-lookup (global-name "com.apple.securityd"))
(deny mach-lookup (global-name "com.apple.secd"))
(deny file-write* (subpath "/System/Volumes/Data/Users/<user>/Library"))
(deny file-write* (subpath "/Users/<user>/Library"))
(deny file-write* (subpath "/Applications"))
(deny file-write* (subpath "/System/Volumes/Data/Applications"))
(deny file-write* (subpath "/System/Volumes/Data/Users/<user>/PebbleBackups"))
(deny file-write* (subpath "/Users/<user>/PebbleBackups"))
(deny file-write* (subpath "/System/Volumes/Data<candidate>/release/Pebble.app"))
(deny file-write* (subpath "<candidate>/release/Pebble.app"))
(deny file-write* (subpath "/System/Volumes/Data/Users/<user>/Documents"))
(deny file-write* (subpath "/Users/<user>/Documents"))
(deny file-write* (subpath "/System/Volumes/Data/Users/<user>/.pebble"))
(deny file-write* (subpath "/Users/<user>/.pebble"))
```

Each protected path is emitted in sorted order of its forms, in this sequence: the rehearsal's `~/Library`, `/Applications`, `~/PebbleBackups` and the candidate, then (with `--data-layout pebble-home`) the real `~/.pebble`, `~/Documents` and any folder the copied `settings.json` names for the Documents copies, then the launcher's own copies of `~/Library`, `/Applications`, `~/PebbleBackups`, `~/Documents` and `~/.pebble`, with duplicates dropped. Phase 24 added the real `~/Documents` and `~/.pebble` to the launcher's own list, because a drill now starts the candidate on a copy of `~/.pebble`, whose settings may name a Documents folder.

What the profile does:
- Network is loopback only, and DNS fails because `mDNSResponder` is unreachable.
- `~/Library`, which holds the data home, the webview store and the Keychain files, cannot be written, and neither can `/Applications`, the backups or the candidate bundle.
- Keychain services cannot be reached: `Vault.get` raises `Keychain access failed` (`vault.py:91-92`) instead of prompting, so no item's access list changes.
- The copy, `restored/` and `TMPDIR` live in the rehearsal directory and stay writable.
- `--allow-keychain` drops the `mach-lookup` lines.

For the drill's first launch, the protected paths are the staged `Applications`, the staged backups and the candidate, plus the same real paths. The staged data home stays writable.

**Verified locally.** A real `sandbox-exec` run of this profile denied a write to a protected directory, allowed a write outside it, and allowed a loopback connection. The real launcher with a fake backend answered the protocol, and a hung one was stopped at its timeout. Not exercised: non-loopback denial (no network in tests), the Keychain denial, and the frozen backend under the profile; the first real rehearsal is that check. If the backend fails on a denied write, `backend.stderr.log` names the path. Change the profile only by reviewed source edit.

## 7. Row counts and exact acceptances

D20's pass rule is strict: every pre-existing table keeps its row count. Startup legitimately changes some counts:
- startup recovery appends interruption events to `run_events` for runs it settles (`migrations.PRESERVED['run_events']` is append-only);
- expired temporal memories, and conversation memories above 1,500, may go (`PRESERVED['memories'].missing`);
- settings that App adds when absent (`ADDED_SETTINGS`, permissions) may appear.

The tests hit the first case: the legacy fixture's two interrupted runs gain two events, so `run_events` goes from 1 to 3.

The tool still fails and names the exact change. The parent may accept one only after reading the upgrade receipt and the evidence, by passing `--accept-row-change TABLE:BEFORE:AFTER` (repeatable) to `rehearse`, `readback` and `drill`.
- An acceptance matches only that exact table and pair of counts.
- Accepted changes are recorded under `results.row_changes.accepted`, and acceptances that matched nothing under `unused_acceptances`.
- Use the same acceptances in readback that the rehearsal needed. A different change in readback is a rollback trigger.

## 8. D20 §8 open checks this work touches

| # | Check | How to verify |
| --- | --- | --- |
| 2 | Whether backend startup touches the Keychain (§2.3) | **Rehearsal (Keychain services denied):**<br>• `results.launch.responses.state.ok == true` means startup does not need the Keychain.<br>• Then search `<rehearsal>/backend.stderr.log` and `<rehearsal>/home/backend.log` for `Keychain`. A hit means some startup path tried it and failed closed.<br>**Source evidence so far:** the tests make every `Vault` method raise, and App startup on the fixture home called none of them. That fixture has no keyed profile and no ChatGPT sign-in, so it is partial. The real home is the rehearsal's job.<br>**Startup jobs:** catch-up job fires in the copy can reach providers only through loopback and the denied Keychain. |
| 3 | The webview store's location, and whether losing it matters (§2.1) | **Location:** `plan` reports `observed.webview_store.exists` for `~/Library/WebKit/com.pebble.local`. Also list `~/Library/WebKit/com.pebble.local/WebsiteData`, `~/Library/Caches/com.pebble.local` and `~/Library/HTTPStorages/com.pebble.local` (read-only). If the store is elsewhere, pass the same `--webview-store ~/…` to every command; `backup.json` records the source.<br>**What it holds:** only the pet's last chat and quiz drafts (`src/Pet.vue:181-218`, `src/App.vue:671`).<br>**Handling:** backup copies it, install leaves it in place for the same identifier, and rollback restores it. Caches and HTTP storage are not copied. |
| 4 | The installed backend still equals `00031ad3…` (§2.4 step 2) | `plan … --expect-backend-sha256 $OLD` reports `installed_backend_matches`. `backup` refuses with `installed_backend` and prints the actual hash. On a mismatch, stop and report; never re-run with another expected hash without the user. |
| 5 | Keychain, TCC, login item and notifications across an ad-hoc replacement (§3) | Observed by the user at the first launch and in the ★ checks (§5 above), and recorded in the parent review. The tool reads none of these. Record:<br>• whether a Keychain prompt appeared, and for which service;<br>• the login-item status before and after logout and login;<br>• whether notifications stayed authorized;<br>• whether Accessibility and Screen Recording needed a re-grant.<br>After a rollback, the old binary's Keychain access-list entries are untouched; "Always Allow" adds the new binary beside them. |

## 9. Command reference

Common options:
- `--root DIR` and `--execute`;
- `--i-am-the-parent-with-user-approval STAMP`;
- `--quiet-file PATH` (repeatable) and `--quiet-seconds N` (default 300);
- `--webview-store ~/…`;
- `--wait-seconds N` (0–900, default 30);
- `--margin-bytes N` (default 1 GiB).

| Subcommand | Required | Optional |
| --- | --- | --- |
| `plan` | — | `--stamp`, `--expect-backend-sha256`, `--candidate`, `--app-manifest`, `--app-key` |
| `backup` | `--expect-backend-sha256` | `--stamp` (required with `--execute` on real paths; default: now) |
| `rehearse` | `--stamp`, `--candidate`, `--app-manifest` | `--app-key` (default `release`), `--rehearsal-dir`, `--launch-timeout` (5–1800 s, default 180), `--accept-row-change`, `--allow-keychain`, `--data-layout legacy\|pebble-home` (Phase 22) |
| `install` | `--stamp`, `--candidate`, `--app-manifest`, `--expect-backend-sha256` | `--release-manifest`, `--require-signed`, `--rehearsal-report` and `--drill-report` (required on real paths) |
| `readback` | `--stamp` | `--mode install` (default) or `rollback`, `--app-manifest` (default: the install record's), `--expect-backend-sha256`, `--accept-row-change`, `--data-layout legacy\|pebble-home` (Phase 22) |
| `rollback` | `--stamp` | `--bundle-source copy` (default) or `moved`, `--expect-backend-sha256` |
| `drill` | `--root` (new), `--source-app`, `--source-home` and/or `--source-pebble-home` (Phase 24; needs `--data-layout pebble-home`), `--candidate`, `--app-manifest`, `--expect-backend-sha256` | `--source-webview`, `--no-launch`, `--launch-timeout`, `--accept-row-change`, `--allow-keychain`, `--data-layout legacy\|pebble-home` (Phase 22) |
| `backups` (read-only) | — | `--root`, `--webview-store`, `--verify` |
| `purge` (read-only) | — | `--root`, `--webview-store` |

`plan`, `backups` and `purge` only read, so they skip the approval gate; `backups` and `purge` have no `--execute`.

**`app-manifest.json` (the T1 seam).** Either one app object `{bundle_sha256, files[, links]}`, or `{"apps": {"release": object, …}}` selected with `--app-key`. `files` and `links` are `release_readiness.bundle_entries` over the `.app` root, and `bundle_sha256` is `bundle_digest(files)`. The digest is re-derived; when `links` is present it is compared too.

**Release verifier (the T3 seam).** `release_verifier(roots, revoked)` binds `pebble.attestation.verify_release_manifest` to the pinned roots (`attestation.pinned_trust()`); only tests pass other roots, as arguments. `release_check` calls it with the envelope's exact bytes and `app_bundle_sha256`, `backend_executable_sha256`, `installed_backend_sha256`, `backup_record_sha256`, `installed_version`, `installed_data_epoch`, `data_epoch` and `now`, and stops with the `ReleaseVerdict`'s code unless it is accepted. An unsigned `pebble.release/1` document is checked against T3's schema (`attestation.check_release_body`) and accepted only without `--require-signed`.

## 10. Limits

- **Fixtures only so far.** Proven only on staged fixtures and on owned temporary trees. The real `ditto`, `codesign --verify`, libproc listing and sandboxed launcher ran on those trees; nothing ran against this Mac's real paths or the frozen candidate.
- **What the data-home manifest compares.** Bytes, SHA256, modes, directories and link targets. Not extended attributes, ACLs, file flags, ownership or timestamps; `ditto` preserves those, but the tool does not check them.
- **Walk bounds.** Walks stay on one volume (a mount point is refused) and are bounded at 200,000 entries and 16 GiB per file. `backup.json` lists every file path of `H` (no contents) and stays inside the 0700 backup root.
- **Process view.** Processes of other users may be unreadable and are then not counted. A Pebble started by another account is visible only through the lease probe.
- **Lease probe race.** The probe holds a shared lock for an instant. A backend starting in that instant would fail its own lease and exit.
- **Which `migrations.py` judges.** Inspection, `verify_backup` and `restore_copy` run the repository's `migrations.py`, not the frozen candidate's (compare `migrations_sha256`). The pass rule is stricter than the migration's own preservation check (§7).
- **No in-tool restore of `upgrade-backups/` data.** Restoring an `upgrade-backups/` database is `restore_copy`'s job, and only into a new directory.
- **Same volume.** The install and rollback renames need `/Applications`, the data home and `~/PebbleBackups` on one volume; the tool refuses otherwise rather than copying and deleting.
- **Unchecked webview data.** Webview caches, HTTP storage, Keychain items and macOS notification, login-item and TCC state are not backed up. The Keychain is never modified by this tool.
- **Report names.** Records are named by UTC second; a collision adds `-1`, `-2` and so on, never an overwrite.
- **Post-move rollback (Phase 24).** Proven on staged fixtures only, like the rest. It restores `~/.pebble` exactly as backed up, including `signing/` and `data/upgrade-backups/`; a signing identity rotated after the backup would need `pebble_signing.py` again. Anything done in the newer version is in `B/pebble-home.after-<t>`, never merged back. Files the newer version wrote in `~/Documents/Pebble` stay there; the restored Pebble's new index rewrites its own copies and counts nothing as deleted.
- **A reused old-folder copy.** A post-move backup whose `data_home.reused` names an earlier root needs that root for its rehearsal and drill (not for its rollback). `backups` shows the dependency; release the earlier root only after those are done.
- **Rotated logs.** Readback scans `service.log` and `backend.log` from the recorded size, or from the start when the file was replaced; lines that a log rotation moved into `backend.log.1` are not scanned.
