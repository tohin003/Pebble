# Changelog

Notable changes to Pebble, newest first. Versions follow [Semantic Versioning](https://semver.org); the format follows
[Keep a Changelog](https://keepachangelog.com). Pebble 1.0.1 is the first public release; 1.0.0 and earlier versions
were built and installed only on the maintainer's Mac.

## [1.0.1]

The first public release: everything listed under 1.0.0 in
[CHANGELOG.md](https://github.com/tohin003/Pebble/blob/main/CHANGELOG.md), plus the fixes below. 1.0.0 was never
published.

### Fixed

- **The companion's eyes follow the pointer up and down.** They looked up at a pointer below the avatar and down at one
  above it; left and right were already right.
- **Licence notices cover everything the app ships.** urllib3's optional `backports.zstd` import made the backend
  bundler pull in setuptools (MIT) and its runtime hook, with no notice for it, though nothing Pebble runs uses it. The
  backend now leaves setuptools, `pkg_resources` and distutils out. The release inventory counts the Rust crates the
  macOS build uses, as the notices file does, and every one has its notice.

## [1.0.0]

The 1.0 release: an inspection of every part of Pebble by ten review teams, the fixes for every release-blocking
and must-fix finding, and the open-source release. Licensed under the Apache License 2.0.

### Highlights

Pebble's Settings shows these lines under What's new (a test keeps the two the same).

- Delete moves a conversation to Recently deleted for 30 days, with Undo; Delete now is in Settings → Data & storage.
- Paste a screenshot or a file into the message box to attach it.
- A conversation that ran on this Mac doesn't send its earlier messages to a cloud model unless you review and include them.
- Say “forget …” to see the saved memories it matches and forget them one by one.
- ⌘+ and ⌘− change the text size; secondary text is easier to read in both themes.
- Approval cards say first what changes and whether it can be undone; Don't allow replaces Dismiss.
- Search and rename conversations in the sidebar.

### Added

- **Install and publish.** A one-line installer (`install.sh`) that checks the download's SHA-256, keeps any earlier app
  in `~/PebbleBackups`, never uses `sudo` and never changes Gatekeeper; `--dry-run` and `--uninstall`. Releases ship
  `Pebble-macos-arm64.zip` with `SHA256SUMS`.
- **Licence and documents.** The Apache License 2.0, a NOTICE, third-party notices generated from the exact build
  inputs, a user guide, and guides for building, contributing and reporting security issues.
- **Recently deleted in the app.** Deleting a conversation moves it to Recently deleted for 30 days, as deleting its
  file in Documents does. Data & storage adds Delete all conversations…, Delete all now… and Remove old sign-in
  copies…, each with a confirmation, and lists the old app-data folder, the install backups and the window storage
  with their sizes.
- **Memory you can manage.** Search and page through every memory in Settings › Memory, see where each came from in
  plain words, and forget one or all of them. "Remember that …" saves the statement and still answers the rest of the
  message. Asking Pebble to forget something shows what matches and forgets only after you confirm.
- **Pebble for Chrome without a developer script.** Connect Chrome… in Details › Browser adds Chrome's helper files with
  one confirmation, and a tab you share and grant reaches the next task in the conversation, turning on screen control
  in the same step when you agree.
- **Install tool** (`scripts/pebble_install.py`): a one-command rollback after the move to `~/.pebble`, a drill of the
  new version's real first start, and read-only `backups` and `purge` listings.

### Changed

- **ChatGPT sign-in.** Each failure says what went wrong (offline, the network, a timeout, the service), with Try again
  where it can help; Pebble shows "Opening ChatGPT sign-in…" at once and a link you can copy; brief network and service
  errors are retried, and the logs now say why a sign-in or a refresh failed.
- **Switching a conversation to a cloud model** no longer sends its earlier on-device messages, their results or what
  Pebble learned on this Mac. Pebble says so, and you can review and release them.
- **Your Documents copies** are outside the reach of Pebble's tools. Pebble acts on a deleted copy only when it is sure
  (twice, a minute apart, or at once with Update copies now), an editor saving an older copy forgets nothing new, a file
  that is present but unreadable is never treated as deleted, copies leave with their conversation, and secrets are kept
  out of file names.
- **AgentGuard's completion check.** A failing test run in the terminal no longer counts as passing, a rejected edit no
  longer counts as a change to test, written JSON and CSV files must parse, and a held task offers three ways forward.
  Each project run now releases its working copy, so memory no longer grows with every run.
- **Speed with many conversations.** The Documents copy pass no longer reads every conversation each cycle: with a
  thousand conversations, a pass with nothing to do takes milliseconds instead of seconds.
- **First run and accessibility.** A first start without a model opens the main window with a welcome to connect
  ChatGPT or use a local model. Secondary text meets WCAG AA contrast in both themes, approval cards use readable text
  sizes, and VoiceOver hears the state of every switch, tab and permission mode. Images and files can be pasted into the
  message box.
- **Plain words.** Every `/` command, Settings tab and permission description is written for people, and the three
  permission modes are described from one source that a test keeps true to what Pebble does. Views (mods) are behind
  Developer features. Settings says where web searches go.
- **HEIC photos** are decoded by macOS itself; the bundled HEIF libraries (GPL and LGPL) are gone.
- **Dependencies:** certifi 2026.10.19 (drops a compromised root certificate), Pillow 13.0.0 and PyJWT 2.15.0.
- **Hardening.** The document and image parser runs under a narrower sandbox, the Keychain helper accepts only the
  signed Pebble app it belongs to, and terminal output can no longer pose as Pebble's own messages.
- **Builds from a clean clone.** The backend spec, a hashed Python lock and the Rust toolchain pin are in the repository;
  builds strip this Mac's paths from the binaries and refuse any that remain.

### Known limits

- Pebble is signed with its own stable identity, not an Apple Developer ID, and is not notarized. `SHA256SUMS` shows a
  download is intact, not who made it.
- No automatic update check yet: run the installer again to update.
- Web search uses Microsoft Bing's RSS feed, which may change without notice.
- Pebble for Chrome loads in Chrome's Developer mode; Pebble's own built-in browser is not available yet.

## [0.6.1]

- A maintenance release of 0.6.0 with the same features and data format, checked again end to end before it was
  installed.

## [0.6.0]

- **A cleaner window.** The conversation, a status line and the composer; everything else moved to a Details drawer
  (Activity, Task, Context, Changes, Tools). The window's first load is about a tenth of its earlier size.
- **See what Pebble is doing.** Status words from what Pebble is actually doing, a timeline of steps, tool calls, retries
  and waits, ChatGPT's reasoning summaries (shown, never stored), and "Worked for 42 s · 6 steps" after each reply.
- **Commands and message actions.** A `/` menu of 28 commands, Copy on every message, Edit and resend, Retry, and a
  token meter that shows only real numbers.
- **Faster, steadier ChatGPT connections.** A slow address no longer costs a minute: a 20-second first-event deadline, 4
  seconds per address, an idle limit scaled by effort, and retries only before any tool ran.
- **Data you can see.** Pebble moved its data to `~/.pebble` (copied, checked and switched over; the old folder stays)
  and keeps readable copies in `~/Documents/Pebble`, with a Data & storage page, `settings.json` and `PEBBLE.md`.
- **Fewer macOS prompts after updates.** Secrets moved into one Keychain item, with a stable signing identity and a
  pinned helper.
- **Pebble for Chrome.** A Chrome extension and native host: Pebble acts only in a tab you share and grant, for one
  site, pairs with a 6-digit code, hands sign-in and payment pages back to you, and asks before anything is submitted.

## [0.5.0]

Phases 13 to 21 of the harness work, installed on 2026-10-14.

- **Coding:** child worktrees, reviewed Git changes, project test recipes and undo.
- **Commands and processes:** interactive terminals, stronger containment, typed command plans, resource and network
  limits, and reconciliation after a crash.
- **Context and memory:** six memory layers kept up to date in the background, hybrid recall, a reviewed handoff
  between models, and better delegation to agents.
- **Research and connectors:** grounded research with source checks, scoped app and browser control, and daily
  connectors.
- **Companion:** goals, commands and inspectors, schedules, notifications, the login item, and checked exports of views.
- **Release:** a signed candidate build, an install tool with backup, rehearsal, drill and rollback, and readiness
  checks.

## [0.4.2] - 2026-10-09

- Screenshots with Unicode spaces in their names attach correctly.
- Drag-and-drop and Add context share one attachment path: up to ten files of 50 MB each, read locally in a sandbox
  (images, text PDFs, Office and OpenDocument files, HTML, RTF, archive listings and text), and kept with their
  messages for follow-up questions.

## [0.4.1] - 2026-10-09

- The companion folds out a sidebar with conversations, live task activity and views.
- The pebble tilts, bounces, scans and reacts to task events and long waits, without asking the model anything, and
  holds still under reduced motion.
- Quizzes keep their timer and answers when the sidebar closes.

## [0.4.0]

- File and image drag-and-drop, screenshot previews, and compact companion popovers for every composer control.
- Cloud profiles use their full available context, with older tool exchanges compacted first.
- Several models per provider connection, a provider filter, and a quick delete dialog.

## [0.3.0]

- The first local preview: a native window and the animated pebble, a local model through llama.cpp, OpenAI, Anthropic
  and Gemini providers, Continue with ChatGPT, effort controls, project edits with review and undo, isolated tests,
  interactive views and quizzes, six memory layers, and AgentGuard checks.
