# Pebble

Pebble is a desktop companion for Macs with Apple silicon. A small animated pebble stays on your screen; click it to
chat, or open the full window to work with a model you choose: ask questions, have Pebble read, edit and test a project
folder under your review, search and read the web, keep what you tell it in memory you can see and edit, and build small
interactive views such as dashboards, quizzes and app demos.

Pebble keeps you in charge. Every change to your project or your Mac goes through a permission mode you pick, approvals
happen only in Pebble's main window, and nothing a model, a web page, a file or a generated view says can widen what
Pebble may do.

**Version 1.0.1.** Licensed under the [Apache License 2.0](LICENSE). What changed: [CHANGELOG.md](CHANGELOG.md).
How to use it: [the user guide](docs/USER_GUIDE.md).

## Install

You need a Mac with Apple silicon (M1 or later) and macOS 12 or later. Pebble 1.0 was built and tested on macOS 27.

In Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/tohin003/Pebble/main/install.sh | bash
```

The installer:

- runs only on macOS on Apple silicon, not under Rosetta, and refuses a macOS older than the app needs;
- downloads `Pebble-macos-arm64.zip` and `SHA256SUMS` from the latest GitHub release over HTTPS only, into a new private
  temporary folder, and checks the zip's SHA-256 against its line in `SHA256SUMS`;
- unpacks it there and checks the app's identifier (`com.pebble.local`), version and code signature before it changes
  anything;
- stops if Pebble is running (it never quits or kills anything);
- moves an existing `Pebble.app` to `~/PebbleBackups/<time>-pre-install.noindex/Pebble.app.backup` (it never deletes
  one);
- installs into `/Applications` if your account can write there, otherwise into `~/Applications`;
- removes only the temporary folder it created for the download, and never touches your data (`~/.pebble`,
  `~/Documents/Pebble`);
- never uses `sudo`, never changes Gatekeeper and never removes quarantine flags.

Options, added after `bash -s --`:

```sh
curl -fsSL https://raw.githubusercontent.com/tohin003/Pebble/main/install.sh | bash -s -- --dry-run          # show what it would do; nothing changes
curl -fsSL https://raw.githubusercontent.com/tohin003/Pebble/main/install.sh | bash -s -- --version 1.0.1    # a specific release
curl -fsSL https://raw.githubusercontent.com/tohin003/Pebble/main/install.sh | bash -s -- --uninstall        # move the app aside; see below
```

To read the installer before running it, download it first: `curl -fsSL -o install.sh
https://raw.githubusercontent.com/tohin003/Pebble/main/install.sh`, read it, then run `bash install.sh`.

### Install by hand

1. From the [latest release](https://github.com/tohin003/Pebble/releases/latest), download `Pebble-macos-arm64.zip` and
   `SHA256SUMS` into the same folder.
2. In Terminal, in that folder: `shasum -a 256 -c SHA256SUMS`. It should print `Pebble-macos-arm64.zip: OK`.
3. Double-click the zip and drag `Pebble.app` to Applications.
4. The first time you open it, macOS may say it cannot verify the app, because a file downloaded in a browser is
   quarantined and Pebble is not notarized (see below). Allow Pebble alone: Control-click `Pebble.app`, choose **Open**,
   then **Open** again. On recent macOS versions, open **System Settings › Privacy & Security** and click **Open Anyway**
   next to the message about Pebble. Never turn Gatekeeper off.

### Signing, honestly

- Pebble is signed with the project's own stable signing identity ("Pebble Local Signing"), not an Apple Developer ID,
  and it is **not notarized** by Apple. The identity stays the same from release to release, so a Keychain **Always
  Allow** you give once normally carries over to later updates.
- `SHA256SUMS` is not signed, and it comes from the same release as the zip. Checking it shows that the download
  arrived intact; it does **not** prove who made it (integrity, not authenticity).
- The installer downloads with curl, which does not quarantine files, so macOS opens Pebble without the warning above.

### First run

1. Open Pebble from Applications or Spotlight. The first time, with no model connected, the full window opens with a
   welcome: **Connect your ChatGPT account** or **Use a local model**. After that Pebble starts as the pebble; click it
   to chat, and use its arrow (or open Pebble again) to bring up the full window.
2. Connect a model in **Settings › Models** (⌘,):
   - **Continue with ChatGPT** signs in with your ChatGPT account in your browser. It needs an eligible Plus or Pro plan
     through OpenAI's preview for open-source and approved clients; plan limits apply, and API billing is separate.
   - **Connect provider** adds an API key for an OpenAI-compatible service (OpenAI, Ollama, LM Studio and others), an
     Anthropic key, or a Responses API endpoint.
   - **Add a model file** runs a GGUF model on your Mac with the bundled llama.cpp. Pebble downloads no model by itself;
     [docs/MODEL_SELECTION.md](docs/MODEL_SELECTION.md) names the default the project tests with (about 2.8 GB).
3. If macOS asks whether Pebble may use its item in your Keychain, enter your login password and choose **Always
   Allow**. Your sign-ins and keys are kept there, never in Pebble's files.

### Update

Quit Pebble and run the installer again. It keeps the previous app in `~/PebbleBackups` and leaves your data where it
is. When an update changes how Pebble stores its data, Pebble first backs up its database in
`~/.pebble/data/upgrade-backups/`. Pebble 1.0 does not check for updates by itself: watch the
[releases](https://github.com/tohin003/Pebble/releases) and read [CHANGELOG.md](CHANGELOG.md).

### Uninstall

```sh
curl -fsSL https://raw.githubusercontent.com/tohin003/Pebble/main/install.sh | bash -s -- --uninstall
```

This moves `Pebble.app` to `~/PebbleBackups/<time>-uninstall.noindex/` and lists what Pebble keeps on your Mac. Your data
is never touched: [docs/DATA_FOLDERS.md](docs/DATA_FOLDERS.md), "Remove Pebble completely", says what each folder
holds and how to remove everything, including the Keychain items.

## What Pebble does

[The user guide](docs/USER_GUIDE.md) covers each of these in detail.

- **Chat** with the model you choose, in the full window or from the pebble. Replies show what Pebble did and how long
  it took; ⌘. stops a task.
- **Work in a project.** Connect a folder and Pebble can read it, propose edits (each one a card you approve, with
  Undo), run its tests in an isolated copy, run terminal commands under review, and prepare Git changes for you to
  approve. AgentGuard, built in, checks that a task that changed code really ran its tests before Pebble calls it done.
- **Permissions.** Three modes, **Ask for approval** (the default), **Auto in project** and **Full access**, plus a switch
  for each kind of tool. Approvals happen only in the main window.
- **The web.** Searches send your search words to Microsoft Bing (its RSS results feed); pages Pebble reads are fetched
  from their own sites; weather comes from Open-Meteo. Everything read from the web is treated as untrusted data.
- **Memory.** Six kinds of memory you can see, search, edit in your own words (`~/.pebble/PEBBLE.md`) and forget, with
  one switch to turn it off.
- **Views.** Ask for a dashboard, a chart, a quiz or an app demo, and Pebble builds it from data. A view can show and
  collect things, but it can never approve, run or change anything.
- **Bigger tasks.** Pebble can split work across agents, track a goal with criteria, and run reviewed schedules while
  it is open. Type `/` in the message box for its commands.
- **Skills and connectors.** Add reusable instructions (skills) and MCP connectors in **Settings › Skills & connectors**.
- **Pebble for Chrome** lets Pebble look at and act in a Chrome tab you share and grant, one site at a time. It is
  loaded in Chrome's Developer mode for 1.0; see [the user guide](docs/USER_GUIDE.md#pebble-for-chrome).

## Your data and privacy

- **`~/.pebble`** holds Pebble's database, settings, `PEBBLE.md` and logs. **`~/Documents/Pebble`** holds readable
  copies of your conversations, memories and files, which you can turn off. **Passwords, keys and sign-ins** stay in the
  macOS Keychain. [docs/DATA_FOLDERS.md](docs/DATA_FOLDERS.md) lists everything, what is safe to delete, and how
  deletion works.
- **What leaves your Mac:** the request to the model provider you chose (your message and the context Pebble sends with
  it), searches to Bing, the pages and weather you ask for, and calls to connectors you add. With a local model, the
  conversation stays on your Mac. A conversation you had with a local model is not sent to a cloud model unless you
  review and release it.
- **No telemetry.** Pebble sends no analytics. Optional usage counters are off by default, stay on your Mac, and can be
  exported or cleared.
- Pebble's own tools can never read or change `~/.pebble` or `~/Documents/Pebble`, even when your project contains them.

## Build it yourself

[BUILDING.md](BUILDING.md) builds Pebble from a clean clone (Node, Rust 1.99.0, Python 3.14, and the Xcode Command Line
Tools). [CONTRIBUTING.md](CONTRIBUTING.md) explains how changes are tested: tests never call a model or the network,
and earlier phases' tests and validators are never weakened.

## Limits

- macOS on Apple silicon only. No Windows or Linux release.
- Not notarized and no automatic updates (see above). No independent security audit has been done.
- Models make mistakes, including confident ones. AgentGuard checks evidence such as test runs; it is not a security
  layer, and it cannot make every answer right. Commands and parsers run in macOS's `sandbox-exec`, which limits them
  but is not a promise against every attack, and prompt injection from web pages or files is limited, not impossible.
- Web search depends on Bing's RSS feed, which may change or stop without notice. The free Open-Meteo service is for
  non-commercial use.
- Pebble for Chrome needs Chrome's Developer mode, and Pebble's own built-in browser is not available yet.
- Scheduled tasks run only while Pebble is open.

## Security and licence

Report a vulnerability privately as [SECURITY.md](SECURITY.md) describes. Pebble is licensed under the
[Apache License 2.0](LICENSE); [NOTICE.md](NOTICE.md) lists the third-party software it includes. Community standards:
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
