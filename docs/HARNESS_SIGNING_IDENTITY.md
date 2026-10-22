# Signing identity, vault helper and Keychain prompts (Phase 22, k22-identity)

The person reported (Phase 22 plan, item 8) that after every install macOS asks for access several times. The goal is the frontier-app experience: once a question is answered, it stays answered across updates. This page explains why macOS kept asking, what changes, what the person sees once, the rare cases that ask again, and what a future Developer ID would change.

## Why macOS kept asking

**Every build was signed ad hoc.** Tauri signs with `signingIdentity: "-"`, and PyInstaller ad-hoc signs the backend. An ad-hoc signature has no signer, so macOS knows the app only by its code hash (cdhash). Each build has a new hash, so each install looks like a new app.

**Two Keychain checks** decide whether code may read an item without asking:
1. **The item's access list.** It names the code that may read the item by its *designated requirement*.
2. **The item's partition list (since macOS 10.12).** It names the code by its *partition ID*:
   - Apple's code is `apple:`;
   - code with an Apple-issued Team ID (Developer ID) is `teamid:XXXXXXXXXX`;
   - any other code gets a partition ID derived from its **cdhash**. A self-signed identity is in this group.

The old store kept each provider secret as its own item under `com.pebble.local.providers`. A ChatGPT sign-in alone is a manifest plus several 1,800-character chunks. Each item failed both checks for every new build, so each item asked once per install.

**Privacy grants** (Screen Recording, Accessibility, Documents) are recorded against the app's designated requirement. For ad-hoc code that requirement is its cdhash, so the grants reset with every build too.

## What changes

1. **A stable local identity, for privacy grants.**
   - `scripts/pebble_signing.py` creates a self-signed code-signing certificate, "Pebble Local Signing", in its own keychain. The parent creates it once, with the person's approval.
   - Every build signs the app and the backend with it, each with an explicit designated requirement:
     ```
     designated => identifier "com.pebble.local" and certificate leaf = H"<certificate SHA-1>"
     ```
     Every later build meets the same requirement, so privacy grants persist.
   - The identity alone cannot fix the Keychain: the partition ID of self-signed code is still its cdhash, which changes with every build.
2. **A vault helper that never changes, for the Keychain.**
   - `pebble-vault` is a tiny C program (`src-tauri/helpers/pebble-vault/pebble_vault.c`). It is the only code that reads or writes the installed app's single vault item: service `com.pebble.local.vault`, account `v1`, holding a JSON map of every secret.
   - `scripts/build_vault_helper.py` builds it **once**, signs it with the identity and pins it at `src-tauri/resources/helpers/pebble-vault`.
   - The candidate builder copies that file **byte-identical** into every release and never re-signs it. Its cdhash, and therefore its partition ID, never changes, so an "Always Allow" given once stays given across updates.
3. **Migration that keeps the old items.**
   - The first stable build copies each old per-secret item into the vault item once.
   - It **keeps** the old items, so a rollback to 0.5.0 still works without a new sign-in.
   - It never reads them again, because each such read would ask again on the next build.
   - A separate, explicit cleanup removes them later.
4. **Readiness stays honest.** A self-signed identity is not Developer ID and is not notarized, so the production signing gate keeps failing (see "Readiness").

## What the person sees

| When | What appears | What to answer |
| --- | --- | --- |
| Once, when the parent runs `pebble_signing.py create --execute` | One macOS authorization prompt (login password or Touch ID): "You are making changes to your Certificate Trust Settings". It trusts the certificate **for code signing only**. | Allow. |
| The first launch of the first build with the helper | One Keychain prompt **per old item the app reads**, attributed to `pebble-backend`: the ChatGPT sign-in's manifest and chunks, and each API key in use. Each item is then copied into the vault item, which the helper creates. Creating its own item never prompts. | **Always Allow.** |
| The same first launch, when a feature needs it | Screen Recording, Accessibility or Documents access, given once more. Before this build, they were bound to an ad-hoc code hash. | Grant as before. |
| Every later update, while the helper is unchanged | **Nothing.** The backend no longer reads Keychain items itself. The helper, the same bytes as before, reads the vault item it created. | — |

### The rare cases that ask again

- **The helper is rebuilt** (a fix, or `--replace-pinned-helper`): one "Always Allow" for the vault item, attributed to `pebble-vault`.
- **The identity is rotated**: new certificate, new requirement. The helper must be rebuilt with it, so it costs one Keychain prompt plus the privacy grants once more.
- **A connector or plan credential that was never migrated**: its old item asks once, the first time the new build reads it, and never again.
- **Rollback to a build before Phase 22, then forward again**: the old items are still the old build's, and the vault item still holds the newer values. Nothing asks; see "Rollback" for one caveat about ChatGPT tokens.
- **The person chooses "Deny"** on a prompt: the vault reports a Keychain error and does not try to get around it.

## The identity tool: `scripts/pebble_signing.py`

```sh
.runtime/agent-env/bin/python scripts/pebble_signing.py status [--probe]
.runtime/agent-env/bin/python scripts/pebble_signing.py create            # dry run: every command and path
.runtime/agent-env/bin/python scripts/pebble_signing.py create --execute --i-am-the-parent-with-user-approval 20261014T093000Z
.runtime/agent-env/bin/python scripts/pebble_signing.py remove [--execute --i-am-the-parent-with-user-approval STAMP]
```

**What it creates:**
- `~/Library/Keychains/pebble-signing.keychain-db`: a dedicated keychain holding only the identity. It is not left on the user search list.
  - It **locks itself 15 minutes after use and on sleep**, and `create` leaves it locked. The builders unlock it only around their `codesign` commands and lock it again right after, even when signing fails.
  - The reason: while the keychain is open, any process of this user could have codesign sign as Pebble and inherit Pebble's privacy grants.
  - **Accepted residual (V1-139).** The auto-lock only shortens the time the keychain stays open by itself. It does not stop an active process running as the same user: that process can read `~/.pebble/signing/keychain-password` (0600, the same user), unlock the keychain and sign as Pebble. This is the same trust boundary as the rest of Pebble (a same-user process can also read the backend's memory and the bridge token), so 1.0 accepts it and states it here. A Developer ID with its key in a hardware token, or a password kept only in the login keychain, would remove it; neither is planned for 1.0.
- `~/.pebble/signing/keychain-password`: the keychain's random 256-bit password, mode 0600, in a 0700 folder. s22-storage marks this folder important: losing it strands the identity.
- `~/.pebble/signing/identity.json`: the record, mode 0600, with no secrets: the name, the SHA-1, the keychain path, the dates, the approval stamp, and the designated requirements of the app, the test app, the backend and the helper.

**What it runs** (Apple's own tools only, no network):
1. `security list-keychains -d user`, so the search list can be put back exactly afterwards.
2. `openssl req -x509` (macOS's LibreSSL) with a fixed profile:
   - CN "Pebble Local Signing", RSA 2048, SHA-256, 10 years;
   - `CA:FALSE`, and `digitalSignature` and `codeSigning`, all critical.

   The key is written encrypted with a one-time passphrase given on stdin, in a private temporary folder that is removed at the end.
3. `openssl pkcs12 -export` with 3DES and SHA-1 MAC, which `security import` accepts.
4. The keychain:
   - `security create-keychain`, then the search list is restored and re-read;
   - `set-keychain-settings -l -u -t 900`;
   - `unlock-keychain`;
   - `import … -x -T /usr/bin/codesign`: the private key is **not extractable**, and only codesign may use it;
   - `set-key-partition-list -S apple-tool:,apple: -s`.
5. `security add-trusted-cert -r trustRoot -p codeSign -k <keychain> cert.pem`: the user trust domain, for code signing only. **This shows the one authorization prompt.**
6. `security find-identity -v -p codesigning <keychain>` must list exactly this certificate. Then `lock-keychain`.

**Secrets.**
- No report, log or environment variable ever holds the keychain password or the one-time passphrase. Reports show placeholders, and captured output is redacted.
- `security` takes the keychain password as an argument for the moment a command runs. macOS lets only the same user and root read another process's arguments, and both can read the 0600 file anyway.

**Approval and exit codes.**
- `--execute` needs the approval stamp, and the stamp is recorded.
- Exit codes: 0 done (or a clean dry run), 2 usage, 3 refused before any change, 4 stopped after a change. `remove` undoes a partial create.

## The vault helper: `pebble-vault`

### Protocol

One request on stdin and one reply on stdout, each a single line of JSON in exactly these forms. Anything else is a bad request.

```
{"op":"ping"}                      -> {"ok":true,"version":1}
{"op":"get_all"}                   -> {"ok":true,"found":true,"map_b64":"…"} | {"ok":true,"found":false}
{"op":"put_all","map_b64":"…"}     -> {"ok":true}   (replace the item's data, read it back, compare)
```

- **The map is opaque to the helper.** It is `backend/pebble/vault.py`'s JSON, carried as base64 so the C needs no JSON parser.
- **Errors** are `{"ok":false,"error":"<code>"}`, with the OSStatus for Keychain failures. Item data never appears in an error, and nothing is written to stderr.
- **Exit codes:** 0 served, 1 Keychain or internal failure, 2 bad request, 3 caller refused.
- **Limits:** the map is at most 1 MiB, and the request at most its base64 plus 64 bytes. Buffers that held item data are cleared before they are freed.
- **The helper reads no environment variable**, and it uses only Security.framework, CoreFoundation and libSystem (CommonCrypto for the leaf's SHA-1 and SHA-256, libproc for the parent's path).

### Caller check

The helper serves only when its **parent process** is Pebble's own backend inside an intact Pebble.app. Since Phase 24 (V1-025, V1-140; decision D5) all three of these must hold, and otherwise it exits 3 before it reads the request, so `ping` is checked too:

1. **The running parent** satisfies
   ```
   (identifier "com.pebble.local.backend" or identifier "com.pebble.local") and certificate leaf = H"<sha1>"
   ```
   where `<sha1>` is the SHA-1 of the leaf certificate the helper itself is signed with, **and** the parent's leaf certificate has the same **SHA-256** as the helper's. The requirement language's `H"…"` is a SHA-1 by Apple's definition, so the SHA-256 is compared inside the helper (CommonCrypto). The designated requirements of the app, the backend and the helper are unchanged.
2. **The parent's path.** The kernel's path for the parent's PID (`proc_pidpath`) must be `<bundle>.app/Contents/Resources/resources/backend/pebble-backend`. A backend folder copied out of the app has no enclosing bundle and is refused.
3. **The enclosing app, statically.** `<bundle>.app` must pass `SecStaticCodeCheckValidity` with `kSecCSStrictValidate | kSecCSCheckAllArchitectures` and its resources validated (nothing opts out), against `identifier "com.pebble.local" and certificate leaf = H"<sha1>"`, and its leaf must have the same SHA-256. The backend's Python payload (`_internal/base_library.zip` and the rest) is not covered by the backend executable's own signature, but it is sealed as resources of the app. So a copied app with an altered payload fails here.

The helper reads its own leaf with `SecCodeCopySelf` and `SecCodeCopySigningInformation`, so an unsigned or ad-hoc helper has no signer and refuses.
- The parent is found with `getppid()` and checked with `SecCodeCopyGuestWithAttributes` (by PID) and `SecCodeCheckValidity`.
- `getppid()` is read again after every check. Otherwise the request is refused with exit 3.
- **Cost.** Step 3 hashes the app's sealed resources on every helper request: about what `codesign --verify --strict` of the installed app costs (it is not `--deep`). A parent check measures it (below).

**PID reuse, and why it is acceptable here.**
- A PID names a process only while it lives. If the parent exited between `getppid()` and the check, its PID could in principle be reused.
- But a child whose parent exits is re-parented at once, so the second `getppid()` then differs and the helper refuses.
- The backend spawns the helper per request and waits for it, so the parent is alive throughout.
- Code that spawns the helper itself is the parent being checked, so it cannot borrow the backend's identity.

**Hardened runtime.** The helper is signed with the hardened runtime and no entitlements. A process that runs it directly cannot inject a library with `DYLD_*` variables to bypass the check, and library validation admits only Apple's libraries.

**Limits of the check:**
- The test app (`com.pebble.candidate`) fails step 3's identifier, so its backend is refused; it never asks anyway, because the vault treats it as a non-owner.
- Code injected into a running backend could call the helper, but it could also read the backend's memory: the same trust boundary. The backend is not hardened by default (see `--hardened-backend`).
- **Interim, not durable (V1-025).** Step 3 checks the bundle on disk when the helper runs, not what the running backend loaded. A same-user attacker can still start the genuine backend with an altered payload, have that payload restore the sealed bytes, and only then spawn the helper; the check then passes. The interim check stops the copy-and-edit attack that i9-release-03 described, and raises this one to a timed restore inside the attacker's own payload. **The durable design, after 1.0:** only the sealed Rust shell (`com.pebble.local`, whose code pages the kernel checks against its signature) runs the helper, and it passes the secrets map to the backend over a pipe; the helper then requires its parent to be `Contents/MacOS/pebble`. That needs a protocol change in `vault.py` and `main.rs`, and one more re-pin.
- **The data home is not bound (t24-sec-recheck-arch-01, Phase 24).** The check binds the backend's code and bundle, not the data home it serves. A process running as the same user can start the genuine backend (or the genuine shell with `PEBBLE_HOME`) against a home it controls; that backend then uses the vault for the connections the home's settings name, including sending a saved API key or ChatGPT token to a URL a profile there names, with no prompt. This is the same trust boundary as V1-139 (such a process can also read `~/.pebble` and the signing password file), so 1.0 accepts it and states it here and in `SECURITY.md`. Binding the vault to the data home alone would not close it, because such a process can also repoint a model profile in the person's own `~/.pebble` (profiles are database settings, outside `settings.json` review), and the backend's default home follows `$HOME`. **After 1.0:** bind each saved secret to its destination inside its vault record, as connector grants already are (`connector_auth.py`: the resource and base digest are stored with the credential and a changed destination is refused): the provider's origin for an API key, the ChatGPT route's fixed origin for a sign-in; and resolve the owner's home from the account record rather than `$HOME` (t24-sec-recheck-arch-04).

### Building and pinning it: `scripts/build_vault_helper.py`

```sh
.runtime/agent-env/bin/python scripts/build_vault_helper.py            # dry run
.runtime/agent-env/bin/python scripts/build_vault_helper.py --execute --i-am-the-parent-with-user-approval STAMP
```

1. **Versions.** Record `xcrun clang --version` and the SDK version.
2. **Two builds.** Compile and link twice with one fixed command, all in a new work folder (`<root>/.runtime/vault-helper-<stamp>`):
   - `xcrun clang -std=c11 -Os -arch arm64 -mmacosx-version-min=12.0 -Wall -Wextra -Werror … -c -o obj/pebble_vault.o`;
   - then a link with `-Wl,-dead_strip -Wl,-no_adhoc_codesign -framework Security -framework CoreFoundation -o out/pebble-vault`;
   - build 1 is moved aside, and **the two must be byte-identical**.
3. **Reproducibility, measured with this toolchain** (scratch compiles in this track):
   - Nothing in the binary depends on the build: no `__DATE__`, no debug information, no source path; the only embedded paths are the system libraries.
   - **The linker's ad-hoc signature embeds the output file name.** It is turned off (`-no_adhoc_codesign`), because the identity signature is the only one wanted.
   - **`LC_UUID` is derived from the content and the output path.** Two links of one object to the same path were identical; different output names gave different UUIDs. The tool therefore uses fixed relative paths and keeps `LC_UUID`, because crash reporting and debuggers key on it. It does not use `-no_uuid`.
   - **Not measured:** whether a relative output name gives the same UUID from different folders. Within one run, the comparison holds.
4. **Sign.**
   - The tool unlocks the identity keychain and signs with `--identifier com.pebble.vault-helper`, the explicit requirement, `--options runtime` and `--timestamp=none`.
   - It locks the keychain again, even on failure.
   - Then it checks the signature: `--verify --strict`; `--display` (identifier, cdhash, the identity as the only authority, no team, runtime, not ad hoc); and the requirement read back.
5. **Pin.**
   - It writes `src-tauri/resources/helpers/pebble-vault` (0755) and `pebble-vault.json` (0644), each new (O_EXCL).
   - The manifest records the SHA-256, the cdhash, the requirement, the signer, the source SHA-256 and the compiler, and no machine path. It ships inside the app.
   - An existing pinned pair is refused. `--replace-pinned-helper`, for an identity rotation or a fix, moves the old pair into the work folder first.
   - A replacement costs every Mac one Keychain prompt, so it needs its **own** approval stamp: a stamp equal to the pinned helper's `approved_stamp` is refused (`replacement_needs_new_approval`). The new manifest records what it replaces (`replaces`: the old SHA-256, cdhash and approval stamp).

**The Phase 24 re-pin (D5).** `pebble_vault.c` changed (the caller check above), so the pinned helper no longer matches its source, and the candidate builder refuses with `vault_helper_source_differs` until the parent rebuilds it:
```sh
.runtime/agent-env/bin/python scripts/build_vault_helper.py --replace-pinned-helper            # dry run
.runtime/agent-env/bin/python scripts/build_vault_helper.py --replace-pinned-helper --execute --i-am-the-parent-with-user-approval <new stamp>
```
The 1.0 build then ships the new helper, and the person answers one Keychain prompt for the vault item ("Always Allow"), attributed to `pebble-vault`.

## The candidate builder: `scripts/build_candidate_app.py`

- **`--identity auto`** (the default): it signs with the local identity when its record, keychain and password file exist, and otherwise keeps Tauri's ad-hoc signature exactly as before.
  - `--identity adhoc` forces ad hoc.
  - `--identity <SHA-1>` requires that identity.
- **The pinned helper.** When `src-tauri/resources/helpers/` exists, preflight checks it:
  - it holds exactly the binary and its manifest;
  - the binary's SHA-256 and the source's SHA-256 match the manifest;
  - its signer is this build's identity.

  The stage then copies it **byte for byte**, and `codesign --display` must show the pinned cdhash, identifier and signer, with the hardened runtime. It is **never re-signed**.
  - Its record is under `vault_helper` in the stage and app manifests.
  - Refusals: `vault_helper_invalid`, `vault_helper_differs`, `vault_helper_source_differs`, `vault_helper_identity_mismatch`, `vault_helper_signature_differs`.
  - An ad-hoc build also ships it unchanged. The helper then refuses that build's ad-hoc backend, and the vault keeps per-item items.
- **The backend.** After the staged backend is proven equal to the flattened PyInstaller output, every Mach-O is signed with the identity, deepest first and `pebble-backend` last.
  - Identifiers: `com.pebble.local.backend` for the executable, and `com.pebble.local.backend.<path with / as .>` for every other binary.
  - Each file is signed as a copy in `C/signing/backend/NNNN/` (in place, codesign would treat a framework's binary as its bundle), then verified and displayed, and only then put in place with its mode.
  - The stage manifest records the identifiers, cdhashes and SHA-256s.
- **Both bundles.** `codesign --force --sign … --identifier com.pebble.local|com.pebble.candidate --requirements … --timestamp=none --preserve-metadata=entitlements,flags,runtime`.
  - Only the main executable and `Contents/_CodeSignature` may change; `codesign --verify --deep --strict` must pass.
  - The identity, cdhash, flags and requirement are recorded in `app-manifest.json`, with `developer_id: false` and `notarized: false`.
- **Keychain lock.** It is unlocked only around the signing commands and locked right after, even on failure.
- **Hardened runtime.**
  - The shell keeps whatever Tauri set.
  - The backend is signed without it by default, as PyInstaller's ad-hoc signature was.
  - `--hardened-backend` is an untested experiment, with `disable-library-validation` and `allow-unsigned-executable-memory`.
- **`src-tauri/tauri.conf.json` is unchanged** (`signingIdentity: "-"`). The builder re-signs after Tauri.

## The vault: `backend/pebble/vault.py`

**Who uses the helper.** Only the backend frozen inside the release bundle (`com.pebble.local`), and only when `resources/helpers/pebble-vault` exists and answers `ping`.
- That decision is made once per process.
- Without a working helper (none in the bundle, or it refuses the caller), the release backend keeps the per-item items exactly as 0.5.0 does. **Sign-in never fails because of the helper.**
- If the helper stops answering during a session, that session falls back to the per-item items and logs the reason code.
- A Keychain refusal reported by the helper is raised, not bypassed.

**Everyone else keeps the per-item items exactly as before:** source runs, validators, the test bundle, the staged backend and evaluation drivers. They never run the helper or touch the vault item.

**The item.** `{"format": "pebble.vault/1", "services": {<service>: {"secrets": {…}, "migrated": {…}}}}`, with one section each for providers, connectors and typed-plan credentials.
- Reads come from a 30-second cache. The release backend is the item's only writer.
- Every write is followed by a read-back and a comparison.

**Migration, once per account:**
- An account the item does not know is looked up once in its old per-item item. When found, it is copied into the item and recorded with its SHA-256 under `migrated`.
- The old item is **kept**, and never read again: a later `get` answers from the item, or `""` once the account is deleted.
- Setting or deleting an account whose old item was never read marks it `superseded`, without reading it. So a kept old item can never bring back an old or deleted value.

**Maintenance** (for the installed app; the parent wires it):
- `Vault().migration_status()`: counts only.
- `Vault().cleanup_legacy()`: deletes the kept old items of migrated accounts and drops their markers. An item that cannot be deleted keeps its marker for a retry.

**Unchanged for callers.** The public API (`Vault()`, `get`, `set`, `delete`, a subclass's `service`) and every error message stay the same. Secrets never appear in an exception, a log or a file. Windows is unchanged.

## Rollback, live evaluation, rotation and removal

**Rollback to a build before Phase 22** (`pebble_install.py rollback`). The old per-item items are still there, so the old build starts signed in. Two caveats:
- **API keys** are as they were at migration. A key changed in the new build is not in the old items.
- **The ChatGPT sign-in rotates its refresh token** on every refresh, at most a day apart (`expires_in ≤ 86400`). Once the new build has refreshed, the kept items hold a refresh token that OpenAI has already replaced. A rollback after that asks for a ChatGPT sign-in once.
- Rolling forward again answers from the vault item: the newer values.

**Live evaluation (`scripts/evaluate_harness.py live`).** Phase 24 (t24-bench, V1-015):
- Live evaluation runs only the separate evaluation connection that `scripts/evaluate_harness.py eval-connection` signs in (its own connection id, Keychain record and ChatGPT host id).
- Every live command refuses the installed app's connection and host id, read read-only from its settings, so a live run never refreshes the app's grant.
- Before Phase 24 the driver ran the person's own profile under the evaluation Python (the **kept per-item** items) while the installed app used the vault item: two copies of one OAuth grant, so whichever refreshed second was signed out.

**Rehearsal and drill with `--allow-keychain`.** The release candidate migrates the secrets it reads into the vault item. It keeps the old items, so the installed build is unaffected.

**Rotation** (expiry in ten years, a suspected key compromise, or a lost password file):
1. `pebble_signing.py remove`, then `create`: one authorization prompt each.
2. `build_vault_helper.py --replace-pinned-helper`, then a build.
3. The person answers one Keychain prompt for the vault item ("Always Allow") and gives the privacy grants once more.

**Removal.** `remove` deletes the trust setting, the keychain, the password file and the record. Builds then fall back to ad hoc, and the pinned helper refuses ad-hoc backends, so the vault uses per-item items again, with the old per-build prompts.

## What a future Developer ID changes

- **Designated requirement.** Developer ID code carries Apple's default requirement: its identifier, `anchor apple generic`, the Developer ID markers and `certificate leaf[subject.OU] = <Team ID>`. It is stable across certificate renewals.
- **Partition ID.** It becomes `teamid:<Team ID>`. Then even a rebuilt helper, or the backend itself, would not ask after an update, and the helper becomes optional.
- **Notarization and stapling.** These make Gatekeeper accept the app. With a hardened runtime and a secure timestamp, the readiness signing gate can pass.
- **The switch itself.** It changes the requirement once, so privacy grants are given once more and the vault item asks once.

## Readiness

- `release_readiness.judge_signing` reads `codesign --display`. A self-signed build is `signature_kind: unrecognized`, so the gate issues `not_developer_id_application`. `spctl` rejects it (`gatekeeper_rejected`), and there is no stapled ticket (`ticket_not_stapled`). The production gate keeps failing, for the honest reason.
- `HARNESS_CANDIDATE_APP.md`'s "expect `adhoc_signature`" becomes `not_developer_id_application` for a stable-identity build.
- `build_config` still reports `configured_signing: adhoc`, because `tauri.conf.json` is unchanged.
- The proposed informational line "stable local identity: yes" belongs to the readiness graders. It is proposed in the track note, not made here.

## Checks for the parent before relying on a real build

Only fakes ran in this track. The C source compiled cleanly with `-Wall -Wextra -Werror` (scratch, unsigned, never run). On the person's Mac:

1. **`pebble_signing.py create`**, dry run then execute:
   - LibreSSL accepts the flags;
   - `add-trusted-cert -k` tolerates the certificate that `import` added;
   - `find-identity -v` lists it as valid;
   - `lock-keychain` works.
2. **`build_vault_helper.py`**, dry run then execute:
   - the two builds are byte-identical on this toolchain;
   - the signature shows `runtime`.
3. **A candidate build with the identity:**
   - codesign finds the identity through `--keychain` while the keychain is off the search list, with `HOME=C/home`. If not, create with `--keep-in-search-list`;
   - `--preserve-metadata=entitlements,flags,runtime` leaves no `adhoc` flag;
   - `codesign --verify --deep --strict` passes;
   - the frozen backend runs;
   - the vault answers `migration_status()` with `store: helper`.
4. **The helper's caller check on a real install.** It serves the installed backend and refuses another parent. Check the refusal by running the bundled helper from a shell: it must exit 3 without touching the Keychain.
5. **Count Keychain prompts.** Count them on the first helper install (one per old item read) and on the **second** install with the same helper, which should show none. Check that privacy grants hold after the second.
6. **Phase 24, the test-bundle `ping` check (V1-025), before and after the re-pin.** On a test bundle only (never the installed app), have each of these send `{"op":"ping"}` through the bundled helper; `ping` never touches the Keychain and the caller check runs first:
   - the genuine bundle's backend: exit 0 before and after;
   - a copy of `Contents/Resources/resources/backend` outside any app: exit 0 before the re-pin confirms V1-025; exit 3 after;
   - a copy of the whole app with one altered module in `_internal/base_library.zip`: exit 0 before; exit 3 after.
7. **Phase 24, the requirement and the cost.** That `SecRequirementCreateWithString` still accepts the helper's requirements (the helper refuses everything if not), and the time of one `ping` through the genuine bundle before and after (the bundle check is about one `codesign --verify --strict` of the app); multiply by the helper calls of one session.
8. **Phase 24, the Keychain access after the re-pin (D5).** Whether the vault item asks once for the new helper and then never again, and whether a rollback to the 0.6.x helper still reads the item without a prompt.
