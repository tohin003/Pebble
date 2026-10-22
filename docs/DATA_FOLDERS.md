# Where Pebble keeps your things

Pebble keeps your things in two places:

- **`~/.pebble`**: Pebble's own folder. It holds your settings and Pebble's database. It is important, private to you and hidden (its name starts with a dot). It works like `~/.claude` for Claude Code or `~/.codex` for Codex.
- **`~/Documents/Pebble`**: readable copies of what you make with Pebble, so you can open, search, back up or delete them in Finder. This is like `~/Documents/Codex` or `~/Documents/ChatGPT`.

Your passwords and sign-ins are in neither place. They stay in the macOS Keychain. A few other places hold things too; see [Outside these folders](#outside-these-folders).

To open either folder, go to Pebble, then **Settings › Data & storage**, and click **Show in Finder**. In Finder you can also press ⌘⇧G and type `~/.pebble`. To see hidden files, press ⌘⇧. (Command, Shift, period).

## Your Documents folder: `~/Documents/Pebble`

Everything here is a **copy** that Pebble keeps up to date. Editing the text of a file here changes nothing in Pebble, and Pebble never reads these files back as instructions or as information. Deleting some of them does change Pebble, as the table says. Secret-shaped text, such as API keys, tokens, passwords, cookies and card numbers, is removed from the copies and from their file names. That is best effort: a secret with no recognisable shape can stay, so do not rely on it.

Pebble's own tools (file edits, terminal commands and connectors) can neither open nor change this folder, even when your project is `~/Documents`. Only you can delete a copy, so only you can make Pebble forget something or move a conversation to Recently deleted this way.

| Folder | What it is | Safe to delete? | What happens if you delete it |
| --- | --- | --- | --- |
| `Conversations/` | One file per conversation, named like `2026-10-14 Plan a trip.md`. Only your messages and Pebble's replies. | Yes, but it deletes the conversation | After a minute (or at once when you choose **Update copies now**), the conversation moves to **Recently deleted** in Pebble. You can restore it there for 30 days; after that it is deleted for good. |
| `Memory/` | One file per kind of memory: Facts & knowledge, Your preferences, Procedures, Past experiences, Time-sensitive information, and Working context (only a count). | Yes, but it makes Pebble forget | Deleting an entry makes Pebble forget it after a minute (or at once when you choose **Update copies now**). More than 3 entries at once, or a whole file, wait until you confirm in **Data & storage**. Pebble remembers that you removed them, so it does not learn them again from the same place. |
| `Attachments/` | Copies of files and images you attached. | Yes | Only the copy goes. The conversation keeps its attachment, and Pebble does not copy it again. |
| `Artifacts/` | Dashboards, quizzes (questions only, never the answers) and app demos Pebble made. | Yes | Only the copy goes. |
| `Exports/` | Copies of documents, tables and web pages you exported. | Yes | Only the copy goes. Files you saved elsewhere with **Save** stay where you put them. |
| `Downloads/` | Files downloaded by Pebble's own browser. | Yes | The file is gone. |
| `Logs/` | Recent troubleshooting logs. Not copied while your Documents folder syncs with iCloud Drive. | Yes | Nothing; new logs appear as Pebble runs. |

Good to know:

- **Pebble acts on a deletion only when it is sure.** On its own, Pebble checks twice at least a minute apart: a file or entry must stay gone for that minute. When you choose **Update copies now** in **Data & storage**, Pebble checks twice a moment apart (about 2 seconds) and acts in that same update. Either way the file must be missing from its folder both times, the folder must be there and readable, and no app may be in the middle of saving it (a temporary file next to it, as many editors and sync tools make, means "still saving"). A file that is there but cannot be read, is empty, or is kept only in iCloud ("Optimize Mac Storage") counts as present. A sync tool renaming a file for a moment changes nothing.
- **Saving an older copy forgets nothing new.** Each Memory file has a `pebble:memory-version` line. If an editor saves a version of the file from before Pebble added an entry, that entry is not forgotten: Pebble writes it back. Keep that line when you edit; without it Pebble cannot tell which version you changed, so it forgets nothing.
- **Deleting a whole folder** (for example all of `Conversations/`) changes nothing in Pebble: Pebble simply makes the folder again. Only deleting files *inside* a folder counts.
- **Moving a file out** of `Conversations/` or `Memory/` counts as deleting it. To keep a copy somewhere else, *copy* it instead (hold the Option key while you drag).
- **iCloud:** if your Documents folder syncs with iCloud Drive, these copies sync too. Pebble tells you once when it first notices, and the **Data & storage** page says so. Troubleshooting logs are not copied while Documents syncs. Pebble's database is never in Documents, so it never syncs.
- **Turning the copies off:** in **Settings › Data & storage**, switch off **Keep readable copies in Documents**. Files already there stay. You can also choose another folder with **Change location…**; the old folder stays as it is. Pebble remembers the folders it wrote, so deleting a conversation later still removes its copies there too.
- **Deleting a conversation** removes Pebble's own copies of it everywhere it wrote them: its transcript, attachment, artifact and export copies. A copy you changed is yours and stays. Discarding an export removes its copy too.

## Recently deleted

Deleting a conversation in Pebble moves it to **Recently deleted**, on the **Data & storage** page, like deleting its file in Documents. It is hidden everywhere and takes no new messages. For 30 days you can:

- **Restore** it: it comes back, with its file in Documents.
- **Delete now…**: it is deleted for good after you confirm.

After 30 days it is deleted for good on its own. **Delete all conversations…** moves every conversation to Recently deleted at once, and **Delete all now…** empties Recently deleted for good; both ask first.

When a conversation is deleted for good, Pebble removes its messages, its runs and their checks (including what AgentGuard kept about them), its attachments, artifacts and exports, and the memories learned from it. Preferences you saved with "Remember that …" stay, because they belong to you, not to the conversation. The deleted text is overwritten inside Pebble's database. Copies made before the deletion stay where they are until you remove them: upgrade backups, install backups and the old app-data folder (see below).

## Pebble's folder: `~/.pebble`

| Item | What it is | Safe to delete? | What happens if you delete it |
| --- | --- | --- | --- |
| `data/` | The live database (`pebble.sqlite` with its `-wal` and `-shm` files), the original attachments and exports, Pebble's browser profile, AgentGuard data, the local model's `model.log`, and upgrade backups of the database (`data/upgrade-backups/`). | **No** | Every conversation, memory, setting and history is lost. If you copy it somewhere, quit Pebble first. |
| `settings.json` | Your settings. `settings.schema.json` describes every one. | Yes, with Pebble closed | Pebble writes it again from the settings it has. |
| `PEBBLE.md` | What Pebble should know about you, in your words. | Yes | Pebble stops using it and forgets the preference it came from. An empty template comes back the next time Pebble starts. |
| `mcp.json` | A read-only list of your connectors, without passwords. | Yes | Pebble writes it again. |
| `skills/` | A place for your own copies of skill folders. | Yes | Skills you already imported keep working. |
| `backups/` | Reserved for backups; the install tool keeps its backups in `~/PebbleBackups` instead (`pebble_install.py backups` lists them). | Only if you no longer need them | You can no longer restore from them. |
| `runtime/` | Small working files (the last move report, settings sync state, the Documents copy index). | Yes, with Pebble closed | A settings change, or a forget waiting for your confirmation, is dropped. |
| `cache/` | Rebuildable caches. | Yes | Pebble rebuilds what it needs. |
| `logs/` | `service.log` and `backend.log` for troubleshooting, with their earlier parts (`backend.log.1` to `backend.log.3`, `service.log.1`). | Yes | Nothing; new logs start. |
| `signing/` | Only after Pebble's own signing identity was created (`scripts/pebble_signing.py`): the password of its code-signing keychain and the identity's record. Used only to build Pebble on this Mac. | **No** | New builds can no longer be signed as the same Pebble, so macOS asks again for Keychain and privacy access after the next update. |
| `README.md`, `.pebble-home.json` | The guide inside the folder, and a marker that tells Pebble this is its folder. | Keep the marker | Without the marker Pebble may not recognise the folder. |

Pebble's own tools (file edits, terminal commands and connectors) are never allowed to change `~/.pebble`.

Inside `data/`:

- **Upgrade backups** (`data/upgrade-backups/`): up to three copies of the database from before an upgrade. They hold conversations you deleted since. The **Data & storage** page shows their size.
- **AgentGuard** (`data/guard/agentguard.db`): the checks Pebble ran on project tasks. It keeps events for 7 days, decisions for 14, verifications for 30, task and session summaries (with the task's request) for 30, and metrics for 14. Deleting a conversation deletes its rows. `data/guard-projects/` holds at most four project copies used for those checks; they are removed when Pebble closes, and at the next start after a crash.

### settings.json

You can change your settings in Pebble or in this file. **Show settings.json** on the **Data & storage** page opens it in your text editor. A few rules keep you safe:

- **More access needs your confirmation.** If the file asks Pebble to do more than before (for example to turn on a tool group, use **Auto** or **Full** access, turn memory back on, show notification content, or send copies to a new folder), Pebble does not apply it until you click **Apply** in **Settings › Data & storage**. Until then, `settings.json` shows the setting that is in effect.
- **Less access applies at once**, through the same path as the switches in Settings. If a task is running, Pebble waits until it ends.
- **A broken file changes nothing.** If the file is not valid JSON or a value is wrong, Pebble keeps your last good settings, shows the problem in the app, and leaves your file as you wrote it so you can fix it.
- **Extra keys are kept.** Pebble does not remove keys it does not know.

The settings in the file are `theme`, `reduced_motion`, `memory_enabled`, `memory_layers`, `permissions`, `companion`, `notifications` and `storage`. Models, effort and accounts are chosen in the app; connectors and skills are reviewed in the app.

### PEBBLE.md

Write plain sentences about yourself and how you like to work, such as "My name is Sam" or "I prefer short answers". **Edit PEBBLE.md** on the **Data & storage** page opens it in your text editor. Pebble reads this file exactly like **Settings → Memory → Your preferences**: as information about you, never as a permission. It cannot turn on tools or skip an approval. While memory, or the **Your preferences** layer, is off, Pebble does not use it.

Up to 4,000 characters are used; text inside `<!-- … -->` comments is ignored. A longer file is used up to its last full line within 4,000 characters, and the **Data & storage** page says how much is used. A single line longer than that cannot be cut, so Pebble does not use the file until you shorten it. Pebble never keeps using an earlier version. If you forget this preference in **Settings → Memory**, Pebble does not bring it back until you edit `PEBBLE.md` again.

### mcp.json, skills and keyboard shortcuts

- `mcp.json` is a list of your connectors for reference. Edits are not applied: add, review or remove connectors in **Settings › Skills & connectors**.
- `skills/` is just a place to keep skill folders. Pebble never loads anything from it by itself; use **Import** in **Settings › Skills & connectors** to add one.
- There is no `keybindings.json`, because Pebble's shortcuts cannot be changed yet. The shortcut that opens Pebble from anywhere is ⌘⇧Space.

## Outside these folders

Pebble, macOS and the install tool also keep things here. The **Data & storage** page shows the sizes of the first three; Pebble never changes them on its own.

| Where | What it is | How to remove it |
| --- | --- | --- |
| `~/Library/Application Support/com.pebble.local` | The old app-data folder of Pebble 0.5 and earlier, frozen when the data moved to `~/.pebble`. It still holds conversations deleted since, and old logs. | Move it to the Trash once you are happy with the move. |
| `~/PebbleBackups` | The install tool's backups, one folder per install: the previous app, your data and the window storage. They hold conversations deleted since, and the signing keychain's password if it existed. | `scripts/pebble_install.py backups` lists them and says which can still roll back. Move a folder to the Trash to release it. |
| `~/Library/WebKit/com.pebble.local` | The app window's own storage: quiz drafts, panel layout and the pet's last conversation. | Move it to the Trash with Pebble closed. |
| `~/Library/Caches/com.pebble.local`, `~/Library/HTTPStorages/com.pebble.local` (and `.binarycookies`), `~/Library/Saved Application State/com.pebble.local.savedState`, `~/Library/Preferences/com.pebble.local.plist` | macOS caches, web storage, window state and preferences for the app. | Move them to the Trash with Pebble closed. |
| Keychain: `com.pebble.local.vault` | Pebble's vault item (account `v1`): every provider key, connector credential and sign-in. | Remove keys and sign-ins in Pebble. To remove everything, delete the item in Keychain Access. |
| Keychain: `com.pebble.local.providers`, `com.pebble.local.connectors`, `com.pebble.local.plan-credentials` | Old per-item copies kept so that a version before the vault could still sign in. | **Remove old sign-in copies** on the **Data & storage** page. After that, going back to such a version needs a new sign-in. |
| Keychain: `com.pebble.local.browser-pairing` | Pebble for Chrome's pairing key. | Unpair in Pebble for Chrome, or delete the item in Keychain Access. |
| `~/Library/Keychains/pebble-signing.keychain-db` | Pebble's own code-signing keychain, only if it was created. | `scripts/pebble_signing.py remove` (it also removes the certificate's trust setting). |
| `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.pebble.browser.json` (and the same file for Chrome Beta, Chrome Canary and Chromium) | How Chrome finds Pebble for Chrome. | `scripts/install_native_host.py uninstall`. |
| Chrome: the Pebble for Chrome extension | Its pairing key, in the extension's own storage. | Remove the extension at `chrome://extensions`. |
| System Settings | The login item, notifications and privacy grants (Accessibility, Screen Recording, Files and Folders). | Remove Pebble in each list. |
| macOS itself | Notification Center (titles only, unless you turned on notification content), crash reports in `~/Library/Logs/DiagnosticReports`, and Time Machine copies of `~/.pebble`. | macOS keeps these on its own schedule. |

## Remove Pebble completely

If you installed Pebble with the one-line installer, `install.sh --uninstall` moves the app aside to `~/PebbleBackups` and lists which of the items below exist on your Mac. In a clone of the repository, `scripts/pebble_install.py purge` lists every item below, with sizes, and deletes nothing. To remove Pebble and everything it keeps, in this order:

1. Quit Pebble from the tray.
2. Remove Chrome's helper files: in a clone of the repository, run `scripts/install_native_host.py uninstall`; otherwise move the `com.pebble.browser.json` files listed above to the Trash.
3. Only on a Mac that built Pebble with its own signing identity: run `scripts/pebble_signing.py remove` in that clone. An app from the installer never creates one.
4. Move the app to the Trash.
5. Move these folders and files to the Trash: `~/.pebble`, `~/Documents/Pebble` (or keep it: it is yours) and any other copies folder you chose, `~/Library/Application Support/com.pebble.local`, `~/Library/WebKit/com.pebble.local`, `~/PebbleBackups` (each holds a copy of your data), and the `~/Library` items listed above.
6. Delete the Keychain items by hand: in Keychain Access, search for `com.pebble.local`. The tool never reads the Keychain.
7. Remove Pebble in System Settings: Login Items, Notifications, and Privacy & Security (Accessibility, Screen Recording, Files and Folders). Remove Pebble for Chrome at `chrome://extensions`.

## Moving from the old folder

Pebble 0.5 and earlier kept everything in `~/Library/Application Support/com.pebble.local`. The first time a newer version starts, Pebble:

1. checks that no other Pebble is using the old folder;
2. copies it into `~/.pebble/data`;
3. checks the copy: the database's own integrity check, the same number of rows in every table with the same content, and the same fingerprint (SHA-256) for every other file;
4. switches to the copy; and
5. writes `MOVED.md` in the old folder, saying where everything went.

The old folder is **never deleted**: when you are happy, delete it yourself. Old logs, and folders that belong to the old location (Git worktrees and dependency environments), stay there; `MOVED.md` lists them.

If anything is not right (another Pebble is open, there is not enough space, an interrupted upgrade must finish first, or Git worktrees and dependency environments that Pebble manages still live in the old folder), Pebble keeps using the old folder and **Data & storage** says why. Nothing is lost: Pebble tries again the next time it starts. An interrupted copy is cleaned up and redone.

## Going back to the previous version (rollback)

The install tool's rollback (`scripts/pebble_install.py rollback`) puts back the home that was in use when the backup was taken:

- **If Pebble already kept its data in `~/.pebble`** (any Mac that ran 0.6 or later): it moves the current `~/.pebble` aside into the backup folder and restores `~/.pebble` exactly as it was backed up, together with the previous app. It also sets aside the restored index of your Documents copies (`runtime/mirror.json`), so the previous version starts a new index: files in `~/Documents/Pebble` that the newer version wrote are never read as deletions, and nothing is forgotten because of them. The old folder (`~/Library/Application Support/com.pebble.local`) is left exactly as it is.
- **If the backup was taken before the move** (Pebble 0.5 and earlier): it restores the previous app and the old folder exactly as they were backed up, and leaves `~/.pebble` untouched. The previous version then uses the old folder. If you install the new version again later, it keeps using `~/.pebble`, and its **Data & storage** page tells you that the old folder changed in the meantime.

Nothing is deleted: whatever the rollback replaces stays in the backup folder, named with the time of the rollback. Your passwords and sign-ins stay in the Keychain and are not changed.

Before installing, the tool backs up both places (in `~/PebbleBackups`, one folder per install), rehearses the upgrade on temporary copies (never your real folders), and drills the whole install and rollback on a staged copy. After installing, it checks that `~/.pebble` holds your data and that the old folder is unchanged apart from `MOVED.md`. To see your backups and which can still roll back, run `scripts/pebble_install.py backups`; to release one, move its folder to the Trash.
