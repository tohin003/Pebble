# Changelog

Notable changes to Pebble, newest first. Versions follow [Semantic Versioning](https://semver.org); the format follows
[Keep a Changelog](https://keepachangelog.com). Pebble 1.0.1 is the first public release; 1.0.0 and earlier versions
were built and installed only on the maintainer's Mac.

## [1.4.0]

Long tasks keep a smaller window. Above a 32K budget, once a task has read a lot, Pebble seals the results of its older
tool calls in one large chunk: each becomes a short stub with the result's facts and a handle, and ReadRunEvidence
returns the full result exactly as it was. Whether this lowers what long tasks cost, without losing quality, is
measured in the next benchmark; no saving is claimed here.

### Highlights

- A long task's older tool results are sealed into short stubs; the newest stay word for word.
- ReadRunEvidence returns any sealed result exactly as it was, page by page.
- Seals are rare and large, so the requests between them still extend each other for prompt caching.

### Changed

- **Older tool results of a long task are sealed.** Above a 32K working budget, once a task's window passes 32K
  estimated tokens (or a quarter of a smaller budget), the results of its older tool calls are replaced, in one chunk,
  by stubs. A stub keeps the tool, the result's facts (path, file hash, line counts, status, exit code, its receipt)
  and a `page:` handle; ReadRunEvidence with that handle returns the full result exactly as it was, from the run's
  archive. Every tool call stays in the window, so the model still sees what it did.
- **What is never sealed:** the newest three to eight tool exchanges (eight when they are small), a result that
  reports an error, test and recipe results, pages read back with ReadRunEvidence, the request, your inputs, the plan,
  the goal and Pebble's own notes.
- **Seals are rare and large.** A seal rewrites earlier context, so a provider reads what follows it fresh once. Pebble
  seals only when the chunk is at least 20K tokens and at least twice that part, or, once the window passes three
  quarters of the budget (where the 100 % compaction, which rewrites more, is near), whenever a 20K-token chunk is
  there. ReadRunEvidence joins the tools in the first seal's step, or at three quarters of the window if nothing can
  be sealed (where sealing is on, it no longer joins at half the window), so a request changes once per seal.
- At 32K and below nothing changes. Above it, a task is unchanged until its window reaches the size at which a seal
  starts; a task that reads several large files can reach it within ten steps.

### For contributors

- `PEBBLE_CONTEXT_SEAL=0` keeps 1.3.0's requests, for comparisons such as the next benchmark's control arm.
- Each seal is recorded as a `context_sealed` run event and an archive generation (`run_compactions`), the same
  records the 100 % compaction uses, so the context inspector and ReadRunEvidence read it unchanged.
- `backend/pebble/context_seal.py`; `backend/tests/test_context_seal_36.py`; `scripts/validate_phase36.py`. D66:
  validate_phase35 accepts a checkout past 1.3.0; its checks are unchanged.

## [1.3.0]

Requests that a model provider can cache, and fewer round trips. Above 8K tokens of context, every request of a task
now extends the previous one instead of rewriting its start, so a provider's prompt cache can reuse what it has
already read. On the ChatGPT route the model may also ask for several tool calls at once. How much this lowers cost
and time on real tasks is measured in the next benchmark and is not claimed here.

### Highlights

- Above 8K, each request of a task now extends the previous one, so prompt caching can reuse it.
- The ChatGPT route can ask for several tool calls in one step; Pebble still performs them one at a time.
- On the Anthropic route the model's earlier reasoning stays valid between steps, since nothing before it changes.

### Changed

- **Requests above 8K extend each other.** Measured on 1.2.0 in a scripted coding task, every request from the
  second on kept only the system prompt the same: the task checkpoint was rewritten after each tool result, and the
  file audit, the plan and the diagnostics were inserted near the top and rewritten. Now, above 8K:
  - the task checkpoint is written once, with the local time the task started;
  - the file audit and a changed plan are added after the newest tool results, listing only what changed; earlier
    ones stay where they were, and only the newest plan is protected when older context is archived;
  - pinned skills, plugin schemas and connector routes stay where they were first added;
  - the system prompt carries the date only, so it is the same for a whole day's tasks.
  In the same scripted tasks every request now extends the previous one. Two events still change the start of a
  request, on purpose: diagnostics are added and removed as they come and go (one that no longer matches its file is
  removed, so the model never acts on it), and ReadSource joins the tools once a web page has been read. At 8K and
  below nothing changes.
- **Several tool calls per step on the ChatGPT route** (above 8K). Pebble 1.0.4's kept benchmark runs spent 99 % of
  their time waiting on the model, and the slowest ones read three files in three separate requests. The model may
  now ask for independent calls together; Pebble performs them in order, each under its own permission and approval.

### For contributors

- Each model request above 8K records how much of the previous request it kept (`model_attempt.prefix`), content-free.
- `backend/tests/test_cache_first_35.py`; `scripts/validate_phase35.py`. D64: validate_phase28 records, rather than
  asserts, that the first message changes per step; every check on the stale-block binding is unchanged.

## [1.2.0]

Precision: Pebble checks its work without touching your tests, leaves test changes to you unless you ask for them, and
follows a few plain writing rules. The rules are instructions to the model; whether they change results is measured in
the next benchmark, on a held-out set sealed before this work began, and is not claimed here.

### Highlights

- Pebble can check a case your tests don't cover in a temporary copy, without adding a test file to your project.
- A change to a test file you didn't ask for waits for your approval, in Auto mode too.
- New writing rule: keep to the reply shape you ask for, such as three lines or a required last line.
- New writing rule: cite only the source relied on, and say plainly when an answer can't be found.
- A long task no longer fails when a request is retried right after Pebble archived older context.

### Added

- **Checks that never touch your project.** RunTests takes a check: one extra test module, in your project's own test
  framework (unittest or node:test), that Pebble writes to probe a case your tests don't cover. It is added only to
  the temporary copy the tests run in, under a name Pebble chooses (`test_pebble_check.py` or `pebble-check.test.mjs`),
  and it never counts as your project's tests: it can't satisfy the completion check, and its failures are not
  remembered as test failures. The approval card says it's Pebble's own check and shows its text. Projects whose
  tests use another runner (pytest, npm, Go, Cargo or make) are told that a check isn't available there.
- **Test files are yours.** When Pebble wants to create or change a test file and your request didn't ask for test
  changes, the change waits for your approval, in Auto mode too, and the card says why. "Add a test for the empty
  case" or "update test_parser.py" lets it through; "fix it so the tests pass" doesn't. Full access is unchanged.
- **Writing rules** (above 8K tokens of context):
  - When two instructions seem to conflict, Pebble is told to take the reading that satisfies both, and to ask only
    when none does.
  - When you set the shape of the reply (a number of lines, only a value, a required last line), it is told to give
    exactly that shape; a required last line ends the reply, not a file, unless you name the file.
  - When you ask it to cite the source it relied on, it is told to cite only that one and to describe any source it set
    aside without its path or link. When an answer can't be determined, it is told to say so without quoting a figure
    that could be taken for the answer.

### Fixed

- **A retried request after compaction no longer fails the task.** When a request was retried (a dropped stream, for
  example) right after Pebble had archived older context to make room, the retry built the archive again with the
  time left at that moment. A retry in a later second didn't match the archive already saved, and the task failed
  with "The model checkpoint changed during compaction". A retry now sends exactly what was saved. This is the failure
  seen once while proving 1.1.0; a test reproduces it with the fix switched off.
- **Stopping a connector's worker no longer fails when it has just exited.** If a plugin (MCP) worker, a hook worker or
  an image-decoding worker exited at the moment Pebble stopped it, macOS refused the stop signal and Pebble reported an
  error instead of finishing. Pebble now treats that refusal as the worker being gone only when it really has exited,
  and still stops a running one. Found while proving 1.2.0.
- **The Veil sweeps on and off as designed.** In 1.1.0 the dusk colour, rim and glass arrived, and left, in a single
  frame: each change started from where it was meant to end, so only the squash, blink and ring played. The README's
  renders of the Veil showed the same, although their description said the colour sweeps over the pebble. Both are
  corrected, and a test now measures the sweep part-way through. Found while building Pebble's landing page.
- **The Keyboard settings caption names the right keys.** It said only ⌘⇧Space works from other apps, while ⌃⌥⌘S and
  ⌃⌥⌘H do too; it now points to the table's "Any app" column.

### For contributors

- `backend/tests/test_precision_34.py` and `backend/tests/test_compaction_retry_34.py`; `scripts/validate_phase34.py`
  checks that the new rules name no task, fixture or grader of the development benchmark set.

## [1.1.0]

**Stealth Lockdown**: one switch that keeps Pebble out of screen capture and screen sharing, and stops it taking focus
from the app you're in. Turn it on in Settings → Privacy and focus, from the menu-bar icon, or with ⌃⌥⌘S. It is built
for privacy and for working without interruptions: a presentation, a call or a full-screen app keeps the screen and
the keyboard.

> Hidden from screen capture and screen sharing, not from a camera, a capture card or a process list. Exams and
> interviews that forbid assistants still forbid it.

### Highlights

- Stealth Lockdown keeps Pebble out of screen shares and recordings and never takes focus from the app you're in.
- While it's on, the pebble wears the Veil: a dusk-blue glass with a light rim, so you can always tell.
- ⌃⌥⌘H hides every Pebble window at once; the menu-bar icon brings Pebble back.
- Turn it on in Settings → Privacy and focus, from the menu-bar icon, or with ⌃⌥⌘S.
- A task that stopped right after its answer no longer repeats the answer when you resume it.
- A plugin tool that could not be checked in time now says so, instead of calling your input invalid.
- Tests run in parallel, so each release is proved in about a third of the time.

### Added

- **Stealth Lockdown.** One switch turns on both halves.
  - **Stealth:** every Pebble window is left out of screen capture and screen sharing, the workspace has no window
    shadow, and Pebble leaves the Dock, ⌘Tab and Mission Control. The full workspace opens as a window instead of in
    full screen, so it never takes over the screen you share. ⌃⌥⌘H, or **Hide Pebble now** in the menu-bar menu, hides
    every Pebble window at once.
  - **Lockdown:** Pebble never takes keyboard focus from the app in front. Its windows become panels that take your
    typing without making Pebble the active app, so your slides, call or full-screen app stay in front, and Pebble
    never brings itself forward.
  - The menu-bar icon changes shape while it's on (a filled pebble in a ring) and carries the switch. With Pebble out of
    the Dock and ⌘Tab, it is the way back in. macOS draws the menu bar, so the icon itself shows in a full-screen share.
  - It stays on across restarts until you turn it off.
- **The Veil.** When you turn Stealth Lockdown on, the pebble gathers, a dusk colour sweeps down over it and it opens
  calmer eyes behind a light rim (1.25 s). While it's on it keeps that glassy dusk look, with one slow shimmer every
  6 s. Turning it off lifts the colour and the pebble hops back (0.65 s). With Reduce motion on, each change is a
  0.3 s fade. At sidebar and status-line sizes, the colour and a thicker rim carry it.

### Fixed

- **A resumed task no longer repeats its answer.** If Pebble stopped (a restart or a quit) right after showing a task's
  answer but before marking it done, resuming the task asked the model to continue, and the answer could come twice.
  Pebble now finishes such a task with the answer you already saw, after its usual completion check, with no new
  request.
- **A plugin tool check that fails says so.** When Pebble's isolated checker for a plugin tool's input failed (it ran
  out of memory, for example), the call was refused as "arguments do not match the reviewed tool schema". It now says
  the input could not be validated, and names the failure.

### For contributors

- **Parallel tests.** `scripts/run_full_suite.py` runs the backend suite on six workers and then, alone, the tests
  that measure time or the machine (`backend/tests/serial_register.json`, each with its measured reason). It writes
  the same proof log the release checks read. A release proof now takes about 17 minutes instead of about 47.
  `scripts/affected_tests.py` picks the tests a change can reach for everyday work, and runs everything when unsure.
- **A fairer benchmark harness.** An audit of the gpt-6.1-sol comparison found three harness defects, all counting
  against Codex CLI: Python cache files counted as unrequested edits, a question that obeyed the prompt's `FINAL: done`
  line was never answered, and a process race lost one cell. They are fixed for every product, with tests
  (`backend/tests/test_bench_harness_fixes_32.py`). That comparison's results are not published yet.
- **A held-out benchmark set is sealed** for the next comparison, before any change aimed at it: SHA-256
  `27889ccdb1fa8d26c047ec3d127f10d2dadd9cf8cb550d74f0d565d70054d051`.

## [1.0.4]

An audit of every command, shortcut and feature claim against the code. Its fixes are below, and so is every claim
that said more than Pebble does. 1.0.2 and 1.0.3 were not published on their own: this release also carries 1.0.2's
fixes (multi-step tasks on Claude no longer fail, requests to Anthropic's API are marked for prompt caching, and two
display fixes; see 1.0.2 in CHANGELOG.md), and 1.0.3 was this release before the full-screen fix.

### Fixed

- **The full workspace is a real full screen, without the Dock in the way.** The pebble's **Open full workspace** opens
  the main window in macOS full screen. There, the Dock stays hidden instead of sliding up over the message box, and
  the red, yellow and green buttons show (macOS greys out yellow in full screen). **Floating companion**, the red
  button and ⌘W leave full screen and go back to the pebble. Before, the full-screen window hid its buttons, and the
  Dock covered the message box whenever the pointer reached the bottom.
- **⌘+ and ⌘− change the text size.** They did nothing: the window lacked the permission its zoom keys need.
- **`/stop` never resumes a task.** With no task running, `/stop` and Enter sent `/resume`, which restarted the latest
  paused or stopped task. Enter now runs a command only when you typed its name or picked it in the menu.
- **A command's exact name wins.** `/agent` ran `/agents`, and `/export` ran `/exports`.
- **Esc closes the open panel.** It now closes the details panel or the canvas wherever focus is, as the shortcut list
  says.
- **The pebble's Views no longer show buttons it can't use.** In the pebble, stopping a view, answering a form,
  opening a static preview and connector media now point to the workspace instead of failing.
- **Pebble's benchmark runs never use the installed app's identity.** The harness refuses a backend inside the release
  app, which shares your Keychain item, and runs the candidate's test app instead.

### Removed

- **PDF export.** It was the export panel's first choice, but a release build could never make the file, because
  Pebble's native PDF view isn't built yet. Export HTML and print it to PDF from your browser. PDF returns when it
  works.

### Corrections

These claims, in the README, the user guide and the app, said more than Pebble does. They now say what it does.

- **⌘+ and ⌘−** were listed in 1.0.0's What's new but did nothing until this release.
- **`/stop`** could resume a task instead of stopping one (see Fixed).
- **PDF export** never worked in a release build (see Removed).
- **App windows.** Pebble can't see or operate other apps' windows yet; it can open an app when you approve. The
  switch is now **Screen control: Chrome tabs you share**, and `/windows` describes the Chrome tabs you shared.
- **Pebble for Chrome** works in one Chrome tab that you share and then grant, on that tab's site, for up to 30
  minutes. It can't open, close, list or switch tabs, or go to another site. Every press, and Enter, asks; looking,
  typing and choosing run under your grant. It has been tested against a simulated Chrome, not yet end to end in a real
  one. **Stop Pebble in Chrome** in the Browser panel ends every Chrome session; a task's Stop ends the task.
- **Memory.** No memory can be edited: you can add, view and forget memories, and write `PEBBLE.md`. Pebble also
  learns facts and procedures from evidence. "Remember that …" saves a preference when it is about you, and a fact
  otherwise. Pebble follows `PEBBLE.md` as your standing instructions; other memories are data. A memory you forget is
  not learnt again from the same place, but **Forget all** starts over.
- **Models.** The model you choose applies to every conversation, not to one.
- **Schedules.** Results appear in Details › Tools; macOS notifications are off until you turn them on.
- **AgentGuard** holds changed code only when tests cover it, and its invented-API challenge reads Python code only.
- **Agents** never change your project, run commands or call connectors themselves; a worktree agent writes in its own
  copy.
- **Permissions.** A switch can't change while a task runs: stop the task first.
- **Attachments.** Word, Excel and PowerPoint mean .docx, .xlsx and .pptx files, and only ZIP archives are listed.
- **Exports.** A Word file turns tables into paragraphs and is labelled unverified. `/export` shows a plan; the view's
  Export… writes the file.
- **Local MCP servers** need the Terminal and Files switches on, and a Python file needs a Python from python.org.

## [1.0.2]

### Fixed

- **Tasks with several steps no longer fail on Claude.** On Claude models, a task's third request could fail with
  "Invalid `signature` in `thinking` block". Anthropic now refuses a request whose earlier conversation changed after
  Claude's reasoning was written, and Pebble refreshes its task notes near the start of the conversation at each step.
  Pebble now asks Anthropic's API to set the outdated reasoning aside instead, so the task carries on.
- **Prompt caching on Anthropic's API.** Pebble now marks every request to `api.anthropic.com` for caching.
  Savings stay small for now, because the task notes Pebble refreshes each step sit near the start of the
  conversation, where they end the cached part early. Moving them to the end is planned. Anthropic-compatible gateways
  are unchanged.
- **Your messages no longer start with a space.** The chat bubble drew a space before (and after) what you typed.
- **Settings › Models names a local model's window once.** A model on this Mac showed "4,096-token window" twice.

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
