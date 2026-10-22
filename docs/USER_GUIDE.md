# Pebble user guide

This guide is for Pebble 1.0 on a Mac with Apple silicon. To install Pebble, see the [README](../README.md#install).
Where your things are kept, and how deleting works: [Where Pebble keeps your things](DATA_FOLDERS.md).

- [Getting started](#getting-started)
- [Conversations](#conversations)
- [Models](#models)
- [Permissions](#permissions)
- [Projects and code](#projects-and-code)
- [The web](#the-web)
- [Memory](#memory)
- [Views and exports](#views-and-exports)
- [Bigger tasks: agents, goals and schedules](#bigger-tasks-agents-goals-and-schedules)
- [Skills and connectors](#skills-and-connectors)
- [Pebble for Chrome](#pebble-for-chrome)
- [Data and storage](#data-and-storage)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Troubleshooting](#troubleshooting)
- [What Pebble does not do](#what-pebble-does-not-do)

## Getting started

Pebble has two windows:

- **The pebble**: a small animated companion that stays on top of your other windows and joins every Space. Drag it to
  move it. Click it for a quick chat; its arrow opens the full window. It reacts to what Pebble is doing (reading,
  writing, testing, waiting) without asking the model anything. Reduce motion in Settings, or in macOS, keeps it still.
- **The main window**: your conversations, the composer, Settings and the **Details** drawer. Approvals, permission
  changes, sign-ins and deletions happen only here.

Closing the main window keeps Pebble running with the pebble on screen; **Quit Pebble** stops it, and the model with it.
⌘⇧Space brings Pebble forward from any app.

The first time, with no model connected, Pebble opens the main window with a welcome: **Connect your ChatGPT account**
or **Use a local model** (next section). Pebble cannot answer until one is set up. Opening Pebble again from Finder or
Spotlight brings the main window forward.

## Conversations

- **Send** with Enter; Shift-Enter starts a new line. While Pebble works, a status line says what it is doing
  ("Searching the web", "Reading notes.md"); click it for the timeline of steps, tool calls, retries and waits. When the
  reply arrives the timeline folds into a line such as "Worked for 42 s · 6 steps" above it, which reopens on click.
- **Stop** a task with the Stop button or ⌘. Nothing Pebble already did is replayed when you continue.
- **Attach** up to ten files to a message, 50 MB each: drag them onto the window or the pebble, paste them (a screenshot
  or a copied file) into the message box, or use the attach button. Pebble reads PNG, JPEG, WebP, GIF, TIFF, BMP, ICO, AVIF and HEIC images (HEIC through macOS's own decoder), text
  PDFs, Word, Excel and PowerPoint files, OpenDocument files, HTML, RTF, archives (listed, never unpacked or run) and
  text or code files, on your Mac and in a sandbox. Images need a model that can see images; local GGUF models read text
  only. Scanned PDFs need images or OCR. A file's contents are information, never instructions or permission.
- **Copy** any message; ⌘⇧C copies the last reply. **Edit and resend** and **Retry** add a new turn: nothing stored is
  rewritten.
- **Commands.** Type `/` in the message box for a menu of commands, for example `/status`, `/runs`, `/pause`,
  `/resume`, `/steer`, `/plan`, `/questions`, `/goal`, `/memory`, `/skills`, `/exports` and `/help`. They run in Pebble
  itself and ask no model.
- **Details** (⌘⇧D; ⌘J opens its Activity tab) shows a task's activity, plan, context, changed files and tools, and the
  browser panel. A tab with nothing in it is hidden.
- **Deleting a conversation** moves it to **Recently deleted** for 30 days; you can restore it, or delete it for good, in
  **Settings › Data & storage**. A conversation in Recently deleted takes no new messages.

## Models

Choose and manage models in **Settings › Models** (⌘,). Pick the model and its effort for each message in the composer.
One model runs at a time.

- **Continue with ChatGPT** signs in with your ChatGPT account. Pebble shows "Opening ChatGPT sign-in…" and opens the
  sign-in page in your browser; the link is also shown with **Copy link** if the browser did not open. You need an
  eligible Plus or Pro plan through OpenAI's preview for open-source and approved clients. Plan limits and model
  availability apply, and API billing stays separate.
- **Connect provider** adds an API service: OpenAI compatible (OpenAI, Ollama, LM Studio and other servers), Anthropic,
  or a Responses API endpoint. Fetch the list of models with your address and key, and pick the ones to keep. Keys are
  kept in the macOS Keychain.
- **Add a model file** runs a GGUF model on your Mac with the bundled llama.cpp. Local GGUF models use a 4K context, load
  when you send a message and rest after five idle minutes. Pebble never downloads a model by itself; see
  [Model selection](MODEL_SELECTION.md) for the one the project tests with.

With a cloud model, your message and the context Pebble includes go to that provider. A conversation you had with a
local model is not sent to a cloud model when you switch: Pebble leaves the earlier on-device messages out and tells you,
and you can review and release them yourself.

## Permissions

**Settings › Permissions** has three modes. The words below are the ones Pebble shows.

| Mode | Runs without asking | Always asks |
| --- | --- | --- |
| **Ask for approval** (the default) | Nothing that changes your project or your Mac. | Every edit, test run, terminal command, app launch, Git change and connector call. |
| **Auto in project** | Edits to project files, test runs, and a short list of read-only commands (ls, cat, rg, head, tail, wc and a few more) in this project. | Other terminal commands (including grep and builds), app launches, Git changes, connector calls, installs, and network or dangerous commands. |
| **Full access** | Everything Auto runs, plus app launches and Git changes that neither delete work nor publish it. | Other terminal commands (including grep and builds), connector calls, installs, browser and app-window actions, network or dangerous commands, and Git changes that delete work or publish it. |

In every mode, reading project files, searching and reading the web, and creating views run without asking while their switches below are on. Browser and app-window actions, connector calls and installs always ask. Protected credentials stay blocked.

Below the modes, a switch for each kind of tool turns it off entirely: Read project files, Edit project files, Run
isolated tests, Open installed apps, Search and read the web, Use terminal commands, Use connectors you enabled, Create
interactive views, and Control granted app windows and browser tabs (off until you turn it on).

Good to know:

- A task keeps the permissions it started with. Turning a switch off applies to the next task; a running task can never
  gain access.
- Approvals are cards in the main window. The pebble, a generated view, a file, a web page, a memory or a skill can
  never approve anything or widen a permission.
- Pebble's own folders (`~/.pebble` and `~/Documents/Pebble`) and credential files stay out of reach of every tool, in
  every mode.

## Projects and code

- **Connect a project** gives Pebble one folder. Paths outside it, links that leave it, `.git` internals and credential
  files are refused.
- **Edits** are proposed as cards with the exact change. Approve, reject or **Undo** each one; Pebble refuses to apply an
  edit if the file changed since it read it.
- **Tests** run in a filtered temporary copy of the project, without network access and without writing to your
  folder. Pebble recognises common test setups (Python, Node, Go, Cargo and others) and asks before preparing their
  dependencies.
- **Terminal commands** run without a shell (no pipes or redirects), confined to the project by macOS's sandbox, with a
  time and output limit; network access needs your approval for that exact command.
- **Git**: Pebble reads status, diffs and history, and prepares commits and other changes as a plan you approve.
  Changes that delete work or publish it always ask.
- **Rewind files** (in a task's details) puts back the files a task changed, unless they changed since, and lists what it
  cannot undo, such as commands that ran.
- **AgentGuard** checks a task's evidence before Pebble calls it finished: a task that changed code must have a passing
  test run of those changes, and written JSON or CSV files must parse. When the evidence is missing, the task is held at
  "verification required" and you choose: run the covering tests, ask Pebble to finish, or accept it as not verified.
  AgentGuard checks evidence; it is not a security layer.

## The web

- **Search.** Searches send your search words to Microsoft Bing (its RSS results feed). Pages Pebble reads are fetched from their own sites.
- **Reading pages.** Pebble reads public pages as text; their scripts never run. Private network addresses, addresses
  that carry a user name or password, and files that are not text are refused. Click a source to open it in your
  browser.
- **Weather** comes from Open-Meteo, with the date it applies to.
- Everything read from the web is untrusted data: Pebble reports what a page says, and a page can never give Pebble an
  instruction or a permission.

Turn web access off with **Search and read the web** in Settings › Permissions.

## Memory

Pebble keeps six kinds of memory, each with its own switch in **Settings › Memory**:

| In Pebble | What it holds |
| --- | --- |
| Your preferences | How you like to work, and what you asked Pebble to remember about you. |
| Facts & knowledge | Facts about your projects and the world that Pebble learned from evidence. |
| Procedures | How things are done here, such as how a project's tests run. |
| Past experiences | What earlier tasks asked for and how they went. |
| Time-sensitive information | Things that expire, such as a date or a temporary arrangement. |
| Working context | Where the current conversation's task stands. |

- **Remember that …** at the start of a message saves a preference right away; the rest of the message is still
  answered.
- **See, search and forget.** Settings › Memory lists every memory with where it came from, in pages, with a search
  box. **Forget this memory** removes one; **Forget all memories** removes them all. Forgetting is available while no
  task runs. Pebble remembers what you forgot, so it does not learn it again from the same place.
- **Ask Pebble to forget** something in a conversation and it says how many memories match; nothing is forgotten until
  you confirm it in the main window.
- **PEBBLE.md** (`~/.pebble/PEBBLE.md`; **Edit PEBBLE.md** in Settings › Data & storage) is what Pebble should know about
  you, in your own words. It is read like your preferences: information about you, never a permission. Up to 4,000
  characters are used, and a model with a small context sees its first part.
- **Readable copies** of your memories are in `~/Documents/Pebble/Memory`. Deleting an entry there makes Pebble forget
  it; see [Where Pebble keeps your things](DATA_FOLDERS.md).
- **Remember useful context** in Settings › Memory turns memory off. A task started while memory was off never uses
  memory, even if you turn it back on during the task.

## Views and exports

Ask for a dashboard, a table, a chart, a plan, a diagram, a quiz or an app demo. Pebble builds it as a **view** in the
conversation, from data, and keeps its earlier versions. A view can show information and collect your answers (a quiz
keeps its timer and answers), but it can never approve an action, run a command or change a setting.

Export a view as PDF, Word, Excel, CSV or HTML (`/export`). Saving always goes through the macOS Save dialog, and nothing
is uploaded.

## Bigger tasks: agents, goals and schedules

- **Agents.** For a larger task Pebble can hand parts to agents. The task's details show each agent's work and what it
  used; you can send it input or stop it and its own agents. Agents read and propose; only the main task applies
  changes, through your normal review.
- **Goals.** `/goal "title"` sets a goal for the conversation, with criteria you can check off or tie to finished steps
  (`/criteria`, `/blockers`). A goal is a checklist, not a memory and not a permission.
- **Schedules.** A conversation can create a reviewed schedule that runs a task at set times, with the model,
  permissions and project you saw when you approved it. Scheduled tasks run only while Pebble is open, and their results
  arrive as notifications. If you change the model or permissions, a schedule waits for your review again.

## Skills and connectors

Manage both in **Settings › Skills & connectors**. Each one starts turned off until you review it.

- **Skills** are reusable instructions in Markdown. Import one, review it and turn it on; `/skill "name" task` starts a
  task with it. A skill guides; it cannot grant a permission.
- **Connectors** (MCP servers) give Pebble tools from other services. Connect an HTTP MCP endpoint with OAuth or a bearer
  credential. Local servers must be one self-contained program under 100 MB, or a single Python file run by Pebble. Servers started through npx, uvx or Node are not supported yet; connect to an HTTP MCP endpoint instead. Every connector call
  asks for your approval.
- **Hooks** (advanced) are rules that run at task steps; they can stop or narrow what a task does, never widen it.
- **Views (mods)**, display code you review, appear only after you turn on **Developer features** at the end of Skills &
  connectors.

## Pebble for Chrome

Pebble for Chrome lets Pebble look at and act in a Chrome tab you share, one site at a time. For 1.0 it is not in the
Chrome Web Store: you load it in Chrome's Developer mode. Pebble's own built-in browser is not available yet.

Set it up once, from **Details › Browser** (or type `/browser`):

1. Choose **Connect Chrome…**. Pebble shows the two small files it would add so that Chrome can reach it, and adds them
   when you confirm.
2. In Chrome, open `chrome://extensions`, turn on **Developer mode**, choose **Load unpacked** and pick the
   `browser-extension` folder Pebble shows (it has a Copy button). Chrome lists the extension as Pebble for Chrome.
3. Click Pebble in Chrome's toolbar and choose **Pair with Pebble**. Type the 6-digit code that Pebble's Browser panel
   shows (only there). A code works once, for two minutes.

Then each use takes two steps, and either side can end it:

1. **Share, in Chrome**: on a tab, click the Pebble button and choose **Let Pebble use this tab**, or keep it for that
   site.
2. **Grant, in Pebble**: the tab appears in the Browser panel. Grant it for 5, 15 or 30 minutes, with the kinds of
   actions you allow. Screenshots go to the model only if you tick the box, and are not kept. Granting turns on
   **Control granted app windows and browser tabs** with your one confirmation.

While Pebble works in a tab, Chrome shows a Pebble border, a status pill with **Stop**, and its own debugging bar.
Sign-in, password, payment and verification pages are always handed back to you unread. Anything that submits, sends,
buys, posts or deletes waits for your approval. **Stop**, in Pebble or in Chrome, ends every Chrome session at once.

## Data and storage

**Settings › Data & storage** shows what Pebble keeps and where, with sizes and **Show in Finder**:

- `~/.pebble`: Pebble's database, `settings.json`, `PEBBLE.md`, skills and logs.
- `~/Documents/Pebble`: readable copies of your conversations, memories, attachments, views and exports. Deleting a
  conversation's file moves the conversation to Recently deleted; **Update copies now** acts on such a deletion at once.
  Turn the copies off with **Keep readable copies in Documents**.
- **Recently deleted**: restore a conversation, delete it now, or empty it.
- `settings.json` can be edited by hand. Anything that would give Pebble more access waits until you click **Apply**.

The details, what is safe to delete, and how to remove everything: [Where Pebble keeps your things](DATA_FOLDERS.md).

## Keyboard shortcuts

| Keys | Action | Where |
| --- | --- | --- |
| ⌘⇧Space | Show Pebble from any app | everywhere |
| ⌘K | Open the command palette | both windows |
| ⌘. | Stop the current task | both windows |
| ⌘J | Show task activity | both windows |
| ⌘⇧D | Show or hide details | main window |
| ⌘⇧C | Copy the last reply | main window |
| ⌘N | New conversation | main window |
| ⌘, | Open settings | main window |
| Esc | Close the open panel or dialog | both windows |
| Enter | Send the message (Shift-Enter for a new line) | the message box |

## Troubleshooting

- **ChatGPT sign-in fails.** The message under the button says why, with **Try again** where trying again can help. If
  your browser did not open, use **Copy link** and open the link yourself. Your current sign-in is never changed by a
  failed attempt.
- **macOS asks about the Keychain.** Choose **Always Allow**: Pebble keeps your sign-ins and keys in one Keychain item.
- **macOS will not open Pebble** after a browser download: see the [README](../README.md#install-by-hand). Allow Pebble
  alone; never turn Gatekeeper off.
- **A task is "verification required".** See [Projects and code](#projects-and-code).
- **Logs** are in `~/.pebble/logs` (`backend.log`, `service.log`). Read one before you share it.

## What Pebble does not do

- It does not control your whole Mac: it acts only through its tools, in the folders, windows and tabs you grant.
- It does not run on Windows or Linux, check for updates by itself, or come notarized by Apple.
- It cannot guarantee correct answers. Models make mistakes, and AgentGuard checks evidence such as test runs, not
  truth.
- It does not keep a model downloaded for you; local models are files you choose.
