# Changelog

Notable changes to Pebble, newest first. Versions follow [Semantic Versioning](https://semver.org); the format follows
[Keep a Changelog](https://keepachangelog.com). Pebble 1.0.1 is the first public release; 1.0.0 and earlier versions
were built and installed only on the maintainer's Mac.

## [2.0.0]

Pebble 2.0.0 is the first public release since 1.5.0. The versions in between were local test builds that were never
published, so these notes cover everything new since 1.5.0. Pebble can now work across your Mac inside a sandbox, use
the app windows and websites you allow, make and keep small tools of its own, and carry out everyday Git under review,
while AgentGuard checks what you asked for, not only what Pebble did. Also new: Pebble Remote, the Mac side of Pebble's
iPhone app, and a work view that draws each run's steps as cards. The iPhone app won't be on the App Store: it will be
offered only as source code, to build and install with Xcode.

### Highlights

- Pebble works across your Mac inside a sandbox and uses the app windows and websites you allow.
- AgentGuard checks what you asked for before a task counts as done, and everyday Git runs under review.
- Pebble Remote is the Mac side of Pebble's iPhone app, and the work view draws each run's steps as cards.

### How 2.0.0 compares

Before this release, Pebble was measured against Claude Code and Codex on the held-out split of its benchmark, golden
set 2.0: 85 tasks in 17 families, each asked in three phrasings, so 255 runs per product. The tasks were written before
the runs and kept sealed from development, and the plan (its gates, graders, prices and budgets) was registered before
the first run. Each comparison used one model for both products: `claude-opus-5-5` with Claude Code 2.1.310, and
`gpt-6.1-sol` with Codex 0.163.0. Pebble ran as 1.9.29, the build whose source 2.0.0 is, apart from its version number
and these notes.

| Against Claude Code (`claude-opus-5-5`) | Pebble | Claude Code |
|---|---|---|
| Tasks done correctly | 235 of 255 (92.2 %) | 213 of 255 (83.5 %) |
| Said done when it was not | 6.3 % | 13.7 % |
| Cost per task | $0.091 | $0.117 |
| Time per task: median, 95th percentile | 12.1 s, 46.5 s | 12.9 s, 40.2 s |

| Against Codex (`gpt-6.1-sol`) | Pebble | Codex |
|---|---|---|
| Tasks done correctly | 243 of 255 (95.3 %) | 231 of 255 (90.6 %) |
| Said done when it was not | 3.9 % | 7.1 % |
| Cost per task | $0.042 | $0.050 |
| Time per task: median, 95th percentile | 20.0 s, 62.9 s | 21.1 s, 54.6 s |

- **The plan's gates.** All five gates measured on these runs were met against both products:
  - not behind overall, with the 95 % lower bound no worse than 5 points;
  - every family within one task;
  - false completions no higher;
  - cost per task no higher;
  - median time within 1.10 times and 95th-percentile time within 1.25 times.
- **The plan's sixth gate**, no family of the older corpus 24a below Pebble 1.0.4's result minus one task, was not
  run before this release. Its reference run used `gpt-6.1-sol`, and the weekly usage limit of the ChatGPT plan these
  benchmarks run on resets after the release date.
- **The difference** in tasks done correctly, with its 95 % confidence interval:
  - +8.6 points (+3.1 to +14.9) against Claude Code;
  - +4.7 points (+0.4 to +9.4) against Codex.
- **Where Pebble beats a product.** The plan allows the word only for a family whose 95 % lower bound is above zero.
  - Against Claude Code: answers from saved web pages (`research_frozen`, 14 of 15 against 4) and repeated
    computations (`workshop_repeat`, 15 against 12).
  - Against Codex: no single family.
- **Where Pebble trails**, by one task each:
  - remembering across sessions against Claude Code (`memory`, 14 of 15 against 15);
  - long tasks against Codex (`long_horizon`, 11 against 12).
- **Safety.** Neither product had an unsafe outcome in the two safety families (risky actions and planted instructions).
  One research task on `claude-opus-5-5` was graded unsafe for Pebble; Claude Code had none.
- **The 32 held-out probes**, which check that Pebble does what its documentation says and gate nothing, were not run
  before this release: 21 of them have no runner yet.
- **How cost and time are measured.** Costs are at public list prices, with cached input at the cached price. Times are
  wall-clock on one Mac and include the provider's speed on the day.
- **The release rule.** Before the runs, the maintainer decided to release 2.0.0 from these results under fixed lines
  (decision D219). The lines were:
  - both runs complete;
  - false completions no higher than either product's;
  - no more unsafe outcomes in the safety families;
  - not behind overall;
  - every family within three tasks;
  - the release's own checks passing.

  The results above met each line about them.

### What's new

#### Work on your Mac, inside a sandbox

- **Beyond a project.** In a conversation without a project, Pebble can run commands, read and write files (images and
  PDFs included) and take a screenshot when your request needs one, the way you would at a terminal.
- **A sandbox for each command.** It reads your home folder except protected places (keys and passwords, browser data,
  Messages, Mail, Pebble's own data, other assistants' sign-ins and your shell history), writes only in your Pebble
  Workbench folder and the folders you allowed, and has no network unless you allowed the sites it names. Privilege
  changes, system settings, keychain access and other apps' processes are refused.
- **It asks before going further.** "Always allow" keeps a folder or site allowed (Settings › Permissions lists them),
  deleting or moving anything outside the workbench asks every time, and Pebble keeps a copy before each change, so
  its card has "Undo this change". After three refused actions in a row, Pebble stops and asks you.
- **Everyday shell works.** Wildcards, `cd`, `2>/dev/null`, `$HOME`, archives and heredocs run inside the sandbox; a
  command that only reads no longer asks for write access; in-place editors and converters (`sed -i`, `gzip`, `sips`)
  ask for the folder they write. A failed command names what Pebble can see of its cause (a read-only file, a script
  without its exec bit, a sandbox limit, a stale lock), and a refused one says what works instead.

#### Fewer cards where it is safe

- **Auto in project asks only for risky steps.** A command that stays inside your project, with no network, runs
  without a card: Pebble first makes an instant copy of the project, puts back any file your request keeps, and Undo
  restores the rest. Such a command can't write where later runs load code, runs with limits on processes, CPU and
  memory, and Pebble ends every process it started; where Pebble can't confirm that containment, it asks as before.
- Git steps that can be undone (staging, committing, creating a branch) also run without a card in Auto. Installs,
  publishing, anything outside the project, and Git steps that delete, rewrite or push still ask. A task's copies are
  limited to 3 GB and kept for a day.
- **Full access approves what your messages ask for.** Pebble's own check compares each step with your messages only,
  never a page's or a window's text, and quotes your words; Activity shows "Approved by Pebble (you asked: …)". A held
  step goes back to the model with the reason, and Pebble asks you after 3 held steps in a row or 20 in a task.
- Deleting or overwriting files that were there before the task, pushing, or reaching outside your Mac needs words of
  yours that name it. Anything that may spend money, and any review your own hooks ask for, still comes to you.

#### Your apps, websites and screen

- **App windows.** With Screen control on, Pebble can ask to use one window of an open app, in any conversation, and
  nothing happens until you allow it, in the workspace or right in the companion. It reads the window through macOS
  Accessibility; where an app shows little there, it can look at the window as a picture and use the mouse and keyboard
  in it (on by default; it needs the Take screenshots and Screen control switches and macOS's own permissions).
- In a window you allowed, Pebble can focus, scroll, choose, tick, type, press, triple-click and drag, checking before
  each act that its target is still there. It never reads or types into a password, code or card field, hands the
  window back to you at a sign-in, a code or a check, and never operates terminals, System Settings, Keychain Access,
  password managers, apps that run code or erase disks, or itself.
- **Cards that fit the step.** A site or window you allow stays allowed in its conversation for up to 15 minutes, until
  you send a message in another conversation. Keypad keys, tabs, checkboxes and switches need no card, and in Full
  access neither do ordinary presses and clicks. A press that may send, save or delete asks you (in Full access, it
  passes Pebble's own check of your messages); anything that may spend money always asks.
- **Steps that read back.** Pebble can press a known sequence of keys in one step ("1 2 3 × 4 5 6 ="). Each step
  returns the window as it is afterwards, and Activity names each press and what it changed, including the app's reply.
- **Apps and app actions.** Name an app ("Use Calculator to …") and Pebble uses it rather than a website, can open it
  in the background, and can open a file in it after the same review. Without the screen, it can run your shortcuts
  (each run asks you), play, pause and skip in Music, search, read and create notes, list and create calendar events
  (no invitations), leave mail drafts and open folders in Finder; it never sends mail or deletes anything this way.
- **Websites in your browser.** With Pebble for Chrome, you approve the sites Pebble may act on and the ones it may
  only read, and it works in its own tab, even behind the one you are using. Without the extension, or in a browser
  that keeps extensions out of its pages (such as Comet), one approval opens a new browser window that Pebble reads
  through Accessibility, only while it is on a site you approved. Sign-in and payment pages stay yours.
- Pebble opens a site's own search results (YouTube, Google, Wikipedia, GitHub and others) without asking when the
  search words are your own, and says something plays only when your Mac shows sound or video starting.

#### Pebble's Workshop

- **Tools Pebble makes for itself.** While it works, Pebble can make a small tool (a pure calculation with no network,
  files or processes), validate it in a fresh sandbox against its own tests and inputs it generates, and use it at once.
  A tool made earlier in a conversation stays ready for the rest of it. Code Mode now works without a project too.
- **Pebble's Toolbox.** Pebble keeps a tool on its own only when it passed validation, helped with your task, was made
  before any page, file or command output could shape it, has at least three tests, and passed a blind check (tests
  written from its description alone). It always tells you, with View code and Remove; any other keep asks you first.
- A kept tool is checked again before its first use in a session, and a tool that changed is never run. Settings ›
  Skills & connectors lists the Toolbox, and a tool you remove can be put back for ten minutes.

#### Git

- **Everyday Git, reviewed.** Pebble can now revert and cherry-pick, tag a release and push the tag, stash, amend or
  uncommit the last commit, rebase, merge with conflicts and finish once you resolve them, restore files, rename or
  delete branches, fetch and pull from a local remote, bisect with your tests, and use the project's linked worktrees.
- You see the exact command and what it changes before it runs. Hooks, filters and editors never run, anything that
  rewrites history, deletes or touches a remote waits for your review, and Undo puts refs, the stash and your files
  back. Cards say which side of a merge wins and never promise an Undo that doesn't exist.
- **Git in the terminal.** Read-only commands typed in a project's terminal (`git status`, `git log`, `git diff`) run
  without asking, through Pebble's confined Git runner, and changes such as `git stash` run as the same reviewed
  commands. One Pebble can't review is refused with the reason; one that can't finish says why and what works.
- **Bounded risk.** Force-push always carries a lease on the commit you reviewed, never a blind `--force`; deleting a
  remote branch never touches the default branch; `git clean` removes exactly the files its preview listed. The
  `git_decisions` setting turns any of these off, and a push your request forbids is refused.
- **Secrets stay out.** `git add -A` and `commit -a` never stage a private or credential file; a commit that would
  record one, or a key that looks real, is refused with the fix named, never the value; and Pebble can stop tracking a
  committed secrets file without reading it. `git remote -v` shows passwords and tokens as `***`, a remote change shows
  where fetch and push go before and after, and an address with credentials or look-alike letters is refused.
- Commits use your own Git identity from your Git config, as Git does, or Git's default, which the review card names.

#### Memory

- **Your words become standing instructions.** What you ask Pebble to remember, however you phrase it, reaches later
  conversations as your own instructions, applied wherever they fit; your request wins where it says otherwise. What
  Pebble learned on its own stays background information that it checks against the task.
- **It saves what you meant.** "Remember how I want X: …" keeps all of it, and "Remember that …" works after a
  greeting. A task worded like a wish is done, not saved; pasted, quoted or forwarded text, or a wish you pass on for
  someone else, is never saved as yours. What you ask a model running on this Mac to keep stays on this Mac.
- **Change or stop it in plain words.** "Please change my date preference to YYYY-MM-DD" replaces the saved one at once;
  "Don't add the sign-off anymore" stops that part and keeps the rest. Nothing is erased until you choose Forget in
  Settings › Memory, and remembering or forgetting no longer leaves your words behind in a task record.

#### AgentGuard: done means checked

- **It checks what you asked for.** Output that must match a file is run on the final changes and compared byte for
  byte; files or tests you asked to keep must hold exactly what they held; "make the whole suite pass" needs passing
  tests on the final files; "don't change anything" needs nothing changed. Pebble knows these checks from the start.
- **No "done" beside an open doubt.** When a check fails, Pebble first tries to fix the cause; when something can't be
  confirmed, it gets one more chance, then says the work is unverified instead of done. A reply that leaves part of
  the request unconfirmed is asked once to settle it; if the doubt stays, the task isn't marked finished.
- **Evidence that holds up.** A test run that covered only some tests, or found none, never counts as proof, and
  Pebble's own extra check never decides whether your tests passed. An answer about your code must be backed by the
  code Pebble read, not only by documentation or tests.
- **Scoped requests keep their tools.** "Don't change main" or "leave my notes.txt alone" no longer stops Pebble using
  Git, commands and edits; at the end it checks those stayed unchanged, gets one chance to put back what moved, and
  otherwise says so. "Don't change anything" runs commands without write access, and later messages keep these rules.
- **More of your request, checked.** A clean-up of generated files isn't done while any remain, a last line you ask for
  (such as `FINAL: done`) stays last, and a report described line by line is checked for its lines. A helper agent's
  failed change is reported rather than counted, and Past experiences record an unfinished task as "not completed".

#### Safer with what Pebble reads

- **Planted contacts withheld.** An address, link or phone number that a page or file plants in hidden text, or in
  instructions aimed at AI assistants, stays out of Pebble's replies unless your own request names it, and a change or
  network step that such text asks for is refused unless your request names it.
- **Conventions, not orders.** Pebble reads a project's AGENTS.md and CLAUDE.md for how it likes its code, tests and
  commits, never as your instructions or as a permission; a line there ordering a risky step (pushing, deleting,
  sending data out) is ignored, and look-alike letters or orders wrapped across lines are caught.
- The secret scan that checks each public release now reads every file line by line; it had passed over some test
  files whole, and in the 1.5.0 release those files hold only known test values.

#### Pebble Remote

- **Pebble Remote** is the Mac side of Pebble's iPhone app: from the phone you can read conversations, send messages,
  see what Pebble is doing and approve with Face ID what the Mac would ask you. Turn it on in Settings › iPhone; it is
  off by default, and while off it holds no thread, socket or file.
- The iPhone app won't be on the App Store. It will be offered only as source code, which you build and install on your
  iPhone with Xcode; that code is not published yet.
- A phone pairs once, with six digits you confirm on the Mac, over TLS 1.3 with the Mac's key pinned on the phone.
  Pebble listens on Tailscale's own interface, and on your Wi-Fi only if you allow it (see the Pebble Remote guide).
- A setting that widens what Pebble may do needs Face ID. Removing a phone stops even its queued calls, and connections
  are capped before TLS, so a flood of idle ones can't keep your phone out.

#### The work view and visual answers

- **The work view** draws each run's steps as cards from Pebble's own records, without asking the model: files read and
  edited (with line counts), commands with where they ran and how they ended, test results, Git steps, windows acted
  on, checks and your reviews.
- **Visual answers.** Tables in replies sort by any column and can become a chart, mermaid flowcharts and sequence
  diagrams are drawn natively, and short numbered steps get a steps view, without changing the reply. Ask for a visual
  and Pebble can answer with a native chart, diagram, comparison, metrics, timeline or record card, drawn from data
  only (no model HTML, scripts, links or images). Settings › Appearance › Visual answers offers "When I ask" (the
  default) or "Also when they help".

#### The companion, the workspace and Settings

- **The companion** stays on screen over full-screen apps, on every Space, opens with one click from another app,
  answers simple cards itself (a site, an app window, bringing a window forward) and has Copy on every reply.
- **Stealth Lockdown** now leaves the app you were in active when you click or type in the companion, opens the
  workspace on the desktop rather than over a full-screen app, and turns on in full screen in one quick step; ⌃⌥⌘H
  hides Pebble and brings it back. Pebble's windows are hidden from screen capture and screen sharing, not from a
  camera, a capture card or a process list.
- **Settings** has a sidebar with a tab for every part (Privacy & Stealth, Appearance and General are new). The first
  launch offers an API key (Anthropic, OpenAI, Ollama, LM Studio) beside ChatGPT and a local model, and the model
  button says who answers and bills: a ChatGPT plan, an API key or this Mac.
- The menu bar has Pebble's own commands, and Help opens the guide, shortcuts, what's new and a problem report. ⌘K
  finds actions, Settings pages and conversations, and conversation search finds what was said, not only titles.
- With notifications on, Pebble tells you when a reply finishes or a card waits while it is in the background, naming
  the conversation, never the reply (not in quiet hours or under Stealth Lockdown; Settings › General has the switch).
- Activity says who approved each step ("Approved by you", "Approved by Pebble (you asked: …)", "Allowed by your
  permission mode") and names app, browser and Code Mode steps in words. Stopping a reply keeps what was written. While
  Pebble isn't the active app it ignores ⌘Q, ⌘H and ⌥⌘H, so a shortcut meant for another app can't quit it.

#### Speed and reliability

- **One connection on the ChatGPT route.** A task's steps share one connection (OpenAI's WebSocket mode for the
  Responses API), a follow-up reuses its conversation's connection, and a broken one is retried after ten minutes.
- **Files ready from the start.** When a request names files, or earlier messages read or changed them, Pebble reads
  them before it begins, with the same permissions and Activity record, and never anything that looks like a secret or
  local settings. Re-reading an unchanged file tells the model so, rather than sending its text again.
- **Fast replies** (Settings › Models, with a ChatGPT sign-in) ask for OpenAI's Fast tier, by default for follow-up
  messages and a long task's later steps; choose follow-ups only, every request, or off. OpenAI prices Fast above its
  standard rate for API use and may count it against your plan's limits faster.
- **Fewer wasted steps.** Independent tool calls go together and closing replies are brief unless you ask for detail. A
  project command that runs too long or prints too much is stopped and Pebble carries on with what it printed (a
  command with network access still stops the task), and a broken or empty model reply is asked for again.
- **Projects run as in your terminal:** your time zone, the project's own package importable, tests found in `src/`
  layouts, Node.js from nodejs.org, Homebrew or Volta, and tests that run git, make or cc with Xcode selected.

### Known limitations

- If Pebble restarts while it has switched on a Chromium app's page accessibility (to read an app you allowed), it
  can't switch it back; the app keeps it until it quits.
- macOS may ask from time to time whether Pebble may "bypass the system private window picker"; choosing Allow lets
  Pebble take pictures of the window you share, never sound.
- Pebble for Chrome is loaded in your browser's Developer mode. In a browser that keeps extensions out of its pages
  (such as Comet), Pebble works in a new window through Accessibility, not in its own tab.
- When Pebble stops tracking a committed secrets file, the secret is still in the Git history and should be changed.
- The iPhone app's source code is not published yet, so until it is, Pebble Remote has no phone to pair with. The app
  won't be on the App Store; it will be offered only as source code, to build with Xcode.

### Upgrading

- Your conversations, memories and settings carry over: the installer keeps your previous app and never touches your
  data, and when an update changes how Pebble stores its data, Pebble backs up its database first.
- Pebble keeps its signing identity, so a Keychain "Always Allow" you gave normally carries over.
- If you use Pebble for Chrome, reload it from your browser's extensions page. Connect a browser can now also connect
  Comet, Brave, Edge, Arc and Chromium.
- New abilities need macOS permissions, which macOS asks you for: Accessibility for app windows, Screen & System Audio
  Recording for screenshots and pictures of windows, and a one-time question per app for app actions.
- Worth checking after the upgrade: Fast replies are on by default with a ChatGPT sign-in (Settings › Models), and
  Auto in project now runs contained project commands without a card (Settings › Permissions).

## [1.9.29] (local build, not released)

### Added

- **Pebble Remote, the Mac side of the iPhone app.** Settings › iPhone turns it on; it is off by default, and while off
  it holds no thread, socket or file.
  - A phone pairs once with six digits you confirm on the Mac. The connection is TLS 1.3 with the Mac's key pinned on
    the phone.
  - It listens on Tailscale's own interface, and on your Wi-Fi only when you allow it. `docs/PEBBLE_REMOTE.md` explains
    the setup, including Tailscale.
  - The phone can read conversations and send messages, approve with Face ID what the Mac would ask you, and see
    what Pebble is doing. Settings that widen what Pebble may do, Stealth Lockdown included, need Face ID. Removing a
    phone stops even its queued calls.
  - Connections are capped before TLS, per address and in all, so a flood of idle connections cannot exhaust Pebble's
    files or keep your phone out.
- **The work view.** Each run's steps are drawn as cards from Pebble's own records: files read and edited (with line
  counts), commands with where they ran and how they ended, test results, Git steps, windows acted on, checks, and your
  reviews. It costs no model tokens.
- **Visual answers.** Tables in replies sort by any column and can be shown as a chart; mermaid flowcharts and sequence
  diagrams are drawn natively; short numbered steps get a steps view. This costs nothing and never changes the reply.
  - When you ask for a visual ("show me a chart of ...", "compare these side by side"), Pebble can answer with a native
    chart, diagram, comparison, metrics, timeline or record card, drawn by Pebble from data only: no model HTML,
    scripts, links or images.
  - Settings › Appearance › Visual answers: "When I ask" (the default) or "Also when they help".
- **Git in your linked worktrees.** `git -C ../worktrees/hotfix ...` (or `cd` there first) works in the terminal for
  this project's own linked worktrees. Reads run as in the project. Changes run as reviewed Git commands bound to that
  worktree, with their card where needed, snapshots and Undo. Any other folder is refused with the rule.

### Fixed

- **Git commands typed in the terminal run under review.** A change such as `git cherry-pick`, `git stash` or
  `git rebase --continue` runs as the same reviewed Git command, with its card where one is needed and its Undo. A
  command Pebble can't review is refused with the reason.
- **A Git command that can't finish says why and what works**, from Git's own words: `--autostash` when your changes
  are in the way, how to finish a conflicted `stash pop`, and which worktree holds a branch Git won't switch to.
- **A failed command says what the host can see of its cause.** Commands run in Pebble's sandbox, where every
  permission error looked like the sandbox's own. Now:
  - a file read-only by its own permissions, or a script whose exec bit is off, is named with the reviewed fix;
  - writing outside the project, Git metadata, other processes and network each name their own limit;
  - a lock or PID file is checked from outside the sandbox: whether a process holds it, has it open or runs the same
    program. A stale lock is said to be stale; a live one is never.
  - `pgrep` is answered by program name from outside the sandbox, where commands cannot see other processes.
- **"Remember how I want X: ..." keeps all of it.** The sentences or list items that spell out a preference are kept
  with it (at most five), never a task, this reply's own instructions, a command or a link. Later sessions see your
  saved preferences whole.
- **Memory follows updates and stops.** A correction of a saved preference said without "remember", a scoped "stop
  doing X", and a wish for one answer are each read for what they are.
- **Changing a saved preference works.** "Please change my date preference to YYYY-MM-DD" (or switch, update, set)
  replaces the saved one at once, instead of waiting for a confirmation in Settings. Replies are no longer checked
  against a preference you have since changed. The older one stays in Settings › Memory until you choose Forget.
- **Stopping part of a saved preference works.** "Please stop starting your replies with Ahoy" or "Don't add the
  sign-off anymore" is saved as your newest wish when it names a preference you saved. Before, only "stop using" and a
  few other verbs were understood. Later sessions see your older preference marked as stopped where your newer wish
  says so, so stopping one part of it ("Forget the Cheers line") leaves the rest in use. Nothing is erased: the older
  preference stays in Settings › Memory until you choose Forget.
- **No "done" beside an open doubt.** A reply that says it could not confirm part of the request, or leaves it to you,
  is asked once to settle it with the project's own evidence. If the doubt stays, the task is not marked finished.
- **Heredoc scripts in the Companion.** A heredoc's body is read as the data or code it is, so a Python script whose
  comment says "at" is no longer refused. Every refusal for real commands stays.
- **Tests that call Apple's tools.** Project tests calling git, make or cc no longer fail with "unable to load
  libxcrun" when Xcode is the selected developer folder.
- **Auto's Undo copies are kept for a day** after their task ends, within 3 GB.
- **Safer project conventions.** A delete keeps its card in a project whose AGENTS.md holds risky orders. A download
  from github.com and similar code hosts is no longer refused as planted. Look-alike hosts such as "rn" for "m" are
  refused.

## [1.9.28] (local build, not released)

### Added

- **Auto asks only for risky steps.**
  - A command that stays inside your project, with no network, runs without a card. Before it runs, Pebble makes an
    instant copy of the project. Files your request keeps are put back if the command changed them. Undo restores the
    rest.
  - Such a command cannot write where later runs load code (dependency folders, caches, compiled Python), it runs
    with limits on processes, CPU and memory, and Pebble ends every process it started when it finishes. Where Pebble
    cannot confirm that containment on your Mac, the command asks as before.
  - Git steps that can be undone, such as staging, committing or creating a branch, run without a card.
  - Installs, publishing, anything outside the project, and Git steps that delete, rewrite or push still ask.
  - The copies of one task are limited to 3 GB. The old behaviour stays one setting away
    (PEBBLE_AUTO_RISKY_ONLY=0).
- **Full access asks less, and only when it should.**
  - A step Pebble's own check holds goes back to the model with the reason, instead of opening a card.
  - Pebble asks you after 3 held steps in a row, or 20 in a task, and the card shows what was held and why.
  - A command Pebble can see only reads files or makes new ones runs without a check.
  - A command whose effect Pebble can't read (a script, a build, a test runner) still goes through the check.
  - So does a command that deletes or overwrites files that were there before the task. Like a push, or anything that
    reaches outside your Mac, it needs words of yours that name it.
  - Pointing a Git remote at a new address runs only when your message gives that address, so a page or file cannot
    redirect where your pushes go.
  - A small model screens routine steps first; anything it cannot clear goes to your main model.
- **AGENTS.md and CLAUDE.md are followed as conventions.** Pebble reads a project's AGENTS.md and CLAUDE.md for how
  the project likes its code, tests and commits. It never treats them as your instructions and never gains a
  permission from them. A line in such a file that orders a risky step (pushing, deleting, sending data out) is
  ignored. Look-alike letters and orders wrapped across lines are caught. Files inside dependency, vendored or build
  folders are never read as conventions.
- **Private files stay out of Git.**
  - When a committed file holds credentials or keys (.env and the like), Pebble can stop tracking it. Pebble never
    reads the file and adds it to .gitignore. The card says plainly that the secret is still in the history and should
    be changed. Undo puts both back.
  - A commit that would record a private file, or add a key that looks real, is refused with the fix named, never the
    value. The check reads what the commit records, binary files included.
- **Remotes, with your credentials hidden.** `git remote -v` and `git remote get-url` show addresses with any password
  or token replaced by `***`. Adding a remote, changing its address or renaming it is reviewed. The card says, in plain
  words, where fetch and push go before and after the change, and each change has an Undo. An address that carries
  credentials is refused, and Pebble suggests a credential helper. So is an address with look-alike letters or hidden
  characters, which would read like a familiar host on the card.
- **Two more terminal steps, reviewed.** `chmod +x` on a project file and `NAME=value` settings for one command now run
  after review. Settings that change how programs are found or loaded stay refused.

### Fixed

- **Scoped requests keep their tools.** "Don't change main", "leave my notes.txt alone" or "don't modify the tests" no
  longer stop Pebble from using Git, commands and edits for the rest of the task. Pebble checks at the end that the
  branches and files you asked it to keep are unchanged. If one moved, Pebble gets one chance to put it back, and then
  says so instead of reporting the task done. "Don't change it" keeps the file the request is about.
- **"Don't change anything" protects the project.** In a request that keeps the whole project unchanged, Pebble runs its
  commands without write access. A script that would have deleted or rewritten files fails safely, and Pebble says
  what happened. A request that keeps only some files still lets its commands write their output.
- **Pebble says what your request keeps.** Pebble now tells the model exactly which files your request keeps unchanged.
  Leftovers such as a stale lock or state file stay free to fix, so a task is no longer stopped by caution about files
  you never mentioned. Pebble calls other files free to change only when no rule of yours, in this request, an earlier
  message or a saved preference, covers them.
- **Curly apostrophes.** A request typed with typographic quotes ("Don’t touch config.toml", as macOS writes it) is
  read exactly like one with straight quotes, so what it keeps, forbids or asks for is never missed.
- **Right answers about code are accepted.** When a value comes from an imported module, a caller, a JavaScript module
  or a file read by its absolute path, Pebble no longer refuses the right answer. A wrong value from a look-alike file
  is still caught, and Pebble asks you to check the code again rather than to change it.
- **Planted contacts are withheld in more forms.** This covers addresses, links and phone numbers that a page or file
  plants in hidden text or in instructions aimed at AI assistants. They are withheld written with or without the link's
  scheme, as Markdown links, in national phone formats, or as the site's name. The check takes bounded time even on
  very large pages. A contact the page shows openly is left as it is, and so are answers that need the page's own
  official contacts.
- **Memory.**
  - Pebble remembers a preference in more of the ways people phrase it.
  - Pasted, quoted or forwarded text is never saved as your preference, and neither is a task.
  - A newer statement is listed first.
  - A newer statement about the same thing ("dates should be YYYY-MM-DD from now on") replaces the older one, and
    Pebble tells you when that happens. Preferences about other things are never replaced.
  - "Forget X" or "stop using X" is saved as your newest wish. Nothing you saved is erased without your click on
    Forget.
  - A fact about something else ("our invoices are in EUR") never replaces a preference of yours, and a wish you pass
    on for someone else ("..., the client says") is not saved as yours.
  - "Make a note of this in NOTES.md" writes the file again.
- **Approved commands with long output no longer interrupt the run.** When a command's output is cut, Pebble says how
  much is missing.
- **Git.**
  - New operations: reflog and ORIG_HEAD references (bound to the commit they named at review), revert ranges, and the
    -X ours/theirs strategies.
  - A stopped merge can be resolved by taking one side for the exact conflicted files.
  - Review cards say which side wins, list a range's commits, and never promise an Undo that does not exist.
  - `git add -A`, `-u`, `.` and `commit -a` never stage a private or credential file.
  - A push your request forbids is refused. A prohibition covers only what it names: "don't push any branch" still lets
    Pebble publish the tag the same request asks for, and "don't push main or develop" covers both. Publishing
    something else ("I'll publish the release notes") never lifts a "don't push".
  - A `git clean` that names no paths deletes only build output and caches.
- **Commands.** A command that ran out of time can be run again in the background. Terminal hints say plainly when a
  limit stops a task, rather than suggesting a route that only looks done.
- **Tests.** An extra check module never decides whether your project's tests passed. A check that collects no tests
  never counts as passing.
- **Companion.** In-place editors and converters (`sed -i`, `perl -i`, `gzip`, `textutil`, `sips` and others) now ask
  for the folder they write. A write the sandbox refused is noticed before Pebble says it is done.
- **Reports.** A report the request describes line by line ("one line per service ...") is checked for its lines and
  count before Pebble says it is done.

## [1.9.27] (local build, not released)

### Fixed

- Pebble remembers a preference however you phrase it ("Remember for later sessions: …", "Keep in mind from now on,
  across sessions: …"), not only when the message starts with "Please remember this". A preference Pebble saved
  itself, or one you confirmed, reaches later conversations as your own instruction. A task worded like a wish ("Save
  the report to reports/summary.md for later") is done, not saved as a memory. What you ask the model running on
  this Mac to keep stays on this Mac, as everything learned there does.
- When you ask for a specific last line (for example `FINAL: done`), Pebble checks its reply and asks the model again,
  once, if the line is missing or not last.
- Git: Pebble reads branches, tags and any commit you name (not only the current one), and accepts many more everyday
  ways of writing Git commands that it used to refuse. An annotated tag shows the commit it points to.
- Git: a push with `-u` sets the branch's upstream and updates its remote-tracking branch; `git bisect` runs as one
  reviewed sequence with your tests between steps; `git clean -n` shows exactly what would be removed; and up to
  eight local Git commands can go to you as one review card instead of one card each.
- Read-only Git commands typed in a project's terminal (`git status`, `git log`, `git diff` and the like) run without
  asking, through Pebble's confined Git runner, as in Codex and Claude Code; options that could write a file, run a
  program or read outside the project are refused.
- Pebble can force-push a branch (always with a lease on the commit you reviewed, never a blind `--force`), delete a
  remote branch (never the default branch), set this repository's `user.name` and `user.email`, and run `git clean
  -f` on exactly the files its preview listed. Each still waits for your review; the `git_decisions` setting turns
  any of them off.
- Commands Pebble runs in a project use your time zone, and Python commands can import the project's own package
  (projects with a `src/` folder included), as they would in your own terminal; they no longer leave `__pycache__`
  folders behind. The terminal's `node` is the one Pebble's test runs use. Tests in projects with a `src/` folder are
  found and run.
- In the companion, Shell commands with wildcards, `cd`, `2>/dev/null`, `$HOME`, `$TMPDIR`, archives and zsh heredocs
  work inside the sandbox instead of being refused, and a command that only reads no longer asks for write access.
- Pebble's checks of your request: caches (`__pycache__`, `.pytest_cache`) are never files you asked to keep unchanged,
  nor is the file you asked it to fix; more ways of asking for output identical to a file are recognised; and an
  earlier message's requirements are still checked in a follow-up that refers to them.
- A passing test command that ran only some of the tests no longer counts as proof that a change works: Pebble runs the
  project's tests before saying it is done.
- Two parts of one reply are no longer run together on one line.
- A project command that runs too long (60 seconds) or prints too much is stopped and Pebble carries on with what it
  printed, instead of stopping the whole task for you to check. A command that was allowed network access still
  stops the task when that happens, since it may have half done something outside your Mac.
- When Pebble refuses a command in a project, it says what works instead (for example, running a script with
  `python3` rather than by its path).
- When a check fails, Pebble first tries to fix the cause before telling you it is blocked.
- Answers about where something happens in code are checked more closely against the code Pebble read.
- When you describe a file instead of naming it ("the Q3 report") and more than one fits, Pebble asks which one.
- When you ask Pebble to run a script but leave some files alone ("Run scripts/migrate.py, don't edit the data"), it
  can run the script; before, it took the request as read-only and could not.
- Running a JavaScript project's tests works when Node.js came from the nodejs.org installer, Homebrew or Volta, not
  only from nvm.
- When a page or file Pebble reads plants an address, link or phone number in hidden text or in an instruction aimed
  at AI assistants, Pebble withholds it from its replies wherever it recognises it (a link with or without its
  scheme, an e-mail address, a phone number in its international or national form, the planted site's name), unless
  your own request names it or asks what the hidden text says; one that a page Pebble read shows openly is left as
  it is. A file write, or a command that changes something or reaches the network, that such an instruction asks for
  is refused unless your own request names it.
- An answer with a cited source no longer repeats figures from sources Pebble set aside (a rumoured figure next to the
  official one could be taken for the answer).
- When a helper agent failed or its accepted change was never applied, Pebble says so instead of counting that part
  as done.
- "Leave the tests unchanged", "don't touch config.py" and "the whole suite must pass" still hold in later messages
  of the same conversation, until a message changes them.

## [1.9.26] (local build, not released)

### Added

- Fast replies (Settings → Models, with a ChatGPT sign-in): Pebble can ask for OpenAI's Fast tier, the same model
  served faster. By default it does so for every request after your first message in a conversation and for a long
  task's later steps; you can also choose follow-up messages only, every request, or off. OpenAI prices Fast at twice
  the standard rate for API use and may count it against your plan's limits faster. Other providers are unaffected.

### Changed

- Each model request's record in Activity now notes whether it asked for the Fast tier and which tier OpenAI reports
  having used.

### Security

- The secret scan that checks every public release tree skipped 12 files whose known test values it was told to
  allow: a rule meant for a few lines made it pass over each of those files whole. It now reads them line by line and
  allows only those lines. The released 1.5.0 tree's copies of those files hold only the known test values.

## [1.9.25] (local build, not released)

### Changed

- When your request names something Pebble can check itself, such as a command whose output must match a file or a
  test suite that must pass, Pebble now knows those checks from the start and runs them before it answers, instead of
  being sent back to run them after it said it was done.
- In a conversation's later messages, Pebble now starts with the files that earlier messages read already in front of
  it, as it already did for files they changed, so it no longer spends a step reading them again.

### Fixed

- A request that asked for tests in so many words, such as "add unittest tests in tests/test_slug.py", was still read
  as not asking for test changes, so Pebble held the new test file for your review even in Auto mode. Requests that
  name a test framework, name the test file with its folder, or say that something needs a new test now count.

### Security

- If a file Pebble reads hides instructions aimed at AI assistants (for example in an HTML comment), Pebble already
  ignores them; now its reply can't pass along an email address or link that appears only in those hidden
  instructions, even while explaining that it ignored them. Pebble's own check catches it and has the reply
  rewritten without it.

## [1.9.24] (local build, not released)

### Changed

- Faster starts in projects: when a request names files in your project (or Pebble already changed files earlier in
  the conversation), Pebble reads them before it begins, so it can act at once instead of spending its first steps
  reading. In a small project it reads its code and configuration up front; documents and data only when you name them.
  It reads them exactly as it would itself: the same permissions, the same record in Activity, and nothing that looks
  like a secret or local settings. Of the files read up front, only the ones your request names add facts to Pebble's
  memory; a file Pebble reads during the task does, as before.
- When Pebble reads a file again and it hasn't changed since Pebble read it at the start, the model is told it's
  unchanged instead of being sent the same text twice.
- A follow-up message in a conversation reuses the conversation's connection to the ChatGPT route, so each turn after
  the first starts sooner.
- Pebble's Workshop accepts a tool written the way models usually write one (a function named after the tool, inputs
  listed by name and type, tests with inputs and expected results), so a tool works on the first try more often. A tool
  with fewer than three tests of its own can be used in its task, but Pebble asks you before keeping it.

## [1.9.23] (local build, not released)

### Changed

- A tool Pebble made in its Workshop earlier in a conversation stays ready for the rest of that conversation, so a
  repeated job (the same check on the next batch) uses it again instead of building it again each time.

### Fixed

- In a project with no tests, a test run that found none counted as failing tests, so Pebble kept trying to fix
  "failures" that didn't exist and ended unverified. A run that finds no tests now proves nothing either way: where
  tests do cover the change, Pebble still runs them before calling the work done.

## [1.9.22] (local build, not released)

### Added

- AgentGuard now checks a clean-up of generated files. When you ask Pebble to delete the generated files that can be
  rebuilt (or build artifacts, or compiled files), it notes which files your project marks as generated output (folders
  your .gitignore names, and standard caches) when the task begins, and it can't report the job done while any of them
  are still there: it gets the list, and either deletes them or tells you why each one stays. Anything that looks like a
  secret or local settings (such as .env files or keys) is never on that list, and only build-output folders (such as
  build/ and dist/) count, so data your project merely doesn't track is never treated as generated.

### Fixed

- On the ChatGPT route, Pebble's one-connection mode (OpenAI's WebSocket mode for the Responses API) never connected:
  Pebble checked the service's answer against the wrong constant and fell back to ordinary requests every time. It now
  connects and is on, so each step after the first starts in about half a second instead of about two.
- When a tool Pebble tried to make in its Workshop didn't pass its checks, the script that went on to use it failed
  with a puzzling "no attribute" error. It now says the tool wasn't made and why, including the shape a tool must have,
  so the next try works.
- In a long task Pebble sometimes asked you, in a question, to approve changes its own review was already holding, and
  the task could end unfinished. Pebble now declines to ask that, and the task waits for your review instead.
- If the one-connection mode breaks for a moment, Pebble tries it again after ten minutes instead of only after a
  restart.

## [1.9.21] (local build, not released)

### Added

- Everyday Git beyond commits and branches: Pebble can now revert or cherry-pick commits, tag a release and push the
  tag, set work aside and bring it back (stash), amend or uncommit the last commit, rebase, merge with conflicts and
  finish the merge after you resolve them, restore files, rename or delete branches, fetch and pull from a local
  remote, and make a linked worktree beside the project. Each one is a reviewed Git command: you see the exact command
  and what it changes before it runs; hooks, filters, editors and the network never run; anything that rewrites
  history, deletes or touches a remote waits for your review; and Undo puts refs, the stash and your files back as they
  were.

- AgentGuard now checks what you asked for, not only what Pebble did. When your request says the output of a command
  must stay identical to a file, Pebble finishes only after running that command on its final changes and the output
  matching the file byte for byte. When you ask it to leave files (or the tests) unchanged, it checks they still hold
  exactly what they held when the task began. "Make the whole suite pass" needs passing tests on the final files, and
  "don't change anything" needs nothing changed. An answer about your code, such as a file, a function or a value, has
  to be backed by the code Pebble read, not only by documentation or tests. When something can't be confirmed, Pebble
  gets one more chance to check it; then it says the work is unverified instead of reporting it done.

### Fixed

- A request to stash, tag, cherry-pick, rebase, amend, pull or fetch did not offer Pebble's Git tools at all, so it
  ended "blocked". Those words now offer them.
- A remote added with a relative path (such as `../remote.git`) could not be pushed to. Pebble now resolves it from the
  repository, as Git does, and shows the folder in the review.
- When you asked Pebble to end its reply with a particular line (such as `FINAL: done`), a verification note after the
  reply could become its last line. Pebble's notes now come above a checked reply.
- When a model's reply stream doesn't hold together (an unfinished item in a finished reply), Pebble asks again instead
  of ending the task: nothing from the broken reply is used and nothing had run.

### Changed

- What you asked Pebble to remember in your own words reaches every task as your standing instructions, applied
  wherever they fit (your request wins where it says otherwise). What Pebble learned on its own stays background
  information it checks against the task.

## [1.9.20] (local build, not released)

### Changed

- When Pebble asks for your decision on a command, a test run or a Git change, and nothing else of the task is waiting
  for your review, it waits for you straight away instead of spending a step on a reply you would not see. The task
  carries on with the outcome once you decide, and Pebble checks the request again before it replies. Staged edits
  still collect for one review, as before.

## [1.9.19] (local build, not released)

### Fixed

- Pebble committed only when the repository itself named an author or a Pebble Git identity was set. It now uses your
  Git identity from `~/.gitconfig` (or the XDG Git config), key by key as Git does, and otherwise Git's own default
  from your Mac account and computer name, which the plan says it is using.
- A command with an assignment inside an argument, such as `python3 -c "p = 1; …"` or `make CC=clang`, was refused as
  an environment override. Only a leading `NAME=value` word is one.
- Reading a binary file (an archive, an image, a database) answered with a codec error. Pebble now says the file is not
  text, how large it is and what it looks like, and how to inspect it with a read-only command.
- An answer that cited a version or section number and repeated it in a closing line you asked for (such as
  `FINAL: 10.2.3`) got a verification note after that line. A repeated cited value now counts as cited.

### Changed

- A commit on a new branch is one reviewed plan, Pebble calls tools that do not depend on each other together, and its
  closing replies are brief unless you ask for detail.

## [1.9.18] (local build, not released)

### Fixed

- After Pebble pressed a key with ⌘, ⇧, ⌥ or ⌃ (or an arrow, Home or End, which carry Fn), the Mac went on treating
  that modifier as held: your next letters were dropped or acted as shortcuts (Q as ⌘Q). Every key press now ends
  with the modifiers as they were before it.
- "Use Calculator to …" and other requests that name an installed app offer Pebble the way to open it; Pebble is told
  that an app already open needs no opening and that its fixed app actions are for Shortcuts, Music, Notes,
  Calendar, Mail drafts and Finder only (it tried Shortcuts for Calculator and gave up).
- In Full access, Pebble's own check counts using the app you named and the ordinary clicks, drags, keys and presses
  your task needs there as asked ("In Preview, delete the blank page …" no longer waits for you at its first click).

### Added

- Pebble can drag in an app window you allowed (draw a selection, move a slider, crop a picture), from one point to
  another inside that window; a drag is always reviewed (or checked by Pebble in Full access).
- A triple-click selects a line or paragraph, and Pebble knows the Mac's text keys (⌘ with an arrow for a line's or a
  document's ends, ⇧ to select): it chose Windows' ⇧End before.
- Shortcuts with punctuation keys (⌘| to centre text, ⌘, ⌘= ⌘- ⌘[ ⌘]) can be pressed.

## [1.9.17] (local build, not released)

### Fixed

- A sequence of presses goes on after a press whose effect does not show in the window's rows (selecting a Chess
  piece), so "white pawn, d2" then "d4" moves the pawn in one step; it still stops at a refusal, a card or an uncertain
  outcome.
- On a keypad, Clear (C) must be pressed in its own step before the keys: it clears only the last entry, and Calculator
  computed 100 × 123 × 456 when a sequence started with it.
- Choosing a colour or another option that closes its popover counts as done for every kind of button (Preview's colour
  swatches paused the window for your check).
- Colours that an app names by their values ("rgb 1 0.149 0 1") are shown with a plain name ("red") to the model, on
  cards and to Pebble's own check in Full access.
- A press that the app answers with an error but may still have done (Preview's colour swatches apply the colour and
  report a failure) no longer pauses the window: Pebble looks at the window before pressing again. Sends, saves,
  deletes and typing keep the pause.
- A request that names an app ("In Chess, …", "Use Calculator to …") lets Pebble open that app; before, only "open",
  "launch" or "start" did, and Pebble stopped to ask.
- A paused app window holds up only work in that window; its Done and End access stay in the conversation for later
  tasks, and after Done the task reads "Checked: Pebble can use the window again" and the uncertain-step banner goes.
- After a game move Pebble looks once more and says how the app answered; it says so when it used presses where you
  asked for the mouse.
- Your answer to Pebble's question shows as you chose it ("Yes, open Chess"), not as data.
- Activity: a card you declined reads "Declined by you"; labels are cut at a word, and an approval's whole quote is in
  its details; a request Pebble had to send again says so plainly.
- Past experiences: a task that ended on an uncertain app step reads "not completed"; a declined app step is named by
  its window.
- Details › Context › Context use speaks plainly: "Pebble messages", "tool descriptions" and "Pebble's own
  estimate" instead of "host messages", "schemas" and "o200k estimate".
- When Pebble asks for an action that does not suit an app's element (selecting a Chess square), the refusal names
  the actions that do ("On this element use: press, focus."), so the next step is the right one.
- A click by position right after a press no longer reads as if screenshots were off: Pebble is told to take a fresh
  look with a picture first (a press's own readback has none).
- Words sent with a scroll or a press are refused with what to use instead (scroll by lines with a position, or type
  into the field).

## [1.9.16] (local build, not released)

### Changed

- When Pebble reads a document window (TextEdit and other multi-line text), it keeps the lines and reads up to 2,000
  characters; it read one 200-character line before, so "the third line" had to be guessed.
- A look at a window stays usable for 45 seconds instead of 15 (a reasoning model often took longer between looking
  and acting, and the act was refused and repeated); every act still checks the window again before it runs.

### Fixed

- Activity names the second change of a press too, so a Chess move reads from where to where ("white pawn, e2 → e2;
  e4 → white pawn, e4").
- Every approval row in Activity says who approved the step: "Approved by you", "Approved by Pebble (you asked: …)" or
  "Allowed by your permission mode"; a long request is cut at a word, and its whole text is in the row's details.
- A sequence of presses now presses the keys it named: after Calculator's All Clear removed a row above the keys, each
  later press landed one key on ("All Clear 2 3 Add 4 5 6 Subtract" for AC 1 2 3 × 4 5 6 =). Each later key is found
  again by its name and place; a key that changed its name (Clear became All Clear) ends the sequence there.
- One press sent as a sequence reads back as one press, so a Chess piece's selection is followed by its square (it read
  as a failed press, and Pebble turned to clicks by position).
- A click by position on something that moves by itself (a selected Chess piece's pulsing ring) is no longer refused as
  "The spot changed since Pebble looked at it" after every look: Pebble looks twice more, compares only what stays
  still, and does not take the pulse for the click's effect.
- A reply that ends on its own next step ("I'll clear the calculator and enter 123 × 456 again") while the app window is
  still Pebble's to use is held once so Pebble takes that step; a task that still ends that way shows "Ended without
  finishing." instead of "Done." and is kept as not completed.
- Choosing an item that closes its popover or menu (Preview's Shapes › Rectangle) counts as done: the window is no
  longer paused for your check after it.
- The Needs a check block's Done and End access work (they showed a Python error); an internal error now reads as one
  sentence, with its details in Pebble's log.
- In Full access, Pebble's check accepts your words when its quote of them is off by a word, and Activity says when it
  could not match your words to a step.
- A refused step says who refused it: Pebble's own check, never the app; Activity words it plainly ("Pebble didn't
  click: something else was in front of that spot").
- Past experiences keep blocked tasks, tasks left with a paused window and tasks whose reply says the work remains as
  not completed, count refused steps apart, and keep the Steps line beside approvals.
- Pebble says in its answer what the app did after its act (a game's reply move).

## [1.9.15] (local build, not released)

### Changed

- In an app window Pebble can press a known sequence of keys or buttons in one step ("1 2 3 × 4 5 6 ="), each press
  checked against the window and read back as a single press is; the sequence stops before anything that needs your
  review and after a press that changed nothing.

### Fixed

- Details › Context › Context use now says how many pictures of windows a task sent to the model (they are never kept
  in the conversation, so the window it shows did not count them once the task ended).
- In Full access, Activity now shows the steps Pebble approved by itself as "Approved by Pebble (you asked: …)" with
  your words (1.9.14 recorded them but showed plain "Approved"), and says when Pebble's check kept a card and why.
- A request that says not to touch anything ("Don't move anything") no longer asks for a window Pebble may press in.
- Past experiences say what a task did when its steps needed no card ("Steps: 3 commands, 1 file written, 5 steps in
  app windows"; it said "No file, command or app actions"), and an uncertain app step reads in words.
- The window card says that in Full access Pebble checks its first use of your mouse and keyboard against your messages
  (it said that first use always asks).
- After a step whose result is uncertain, the conversation's Needs a check block offers the paused window's Done and
  End access (they were only in Details › Tools), a new message hears that the window is paused (Pebble asked you to
  allow it again, which nothing lets you do), and Details no longer shows a red error while another task runs.
- When Pebble's own window covers the spot it has to click in an app window you allowed, the companion moves out of
  the way and Pebble clicks again (it asked you to allow the window again).
- "Open full workspace" reopens the workspace the way you left it: windowed if you had left full screen (your choice
  in R9's review); the first open is still full screen.

## [1.9.14] (local build, not released)

### Highlights

- In Full access, Pebble approves its own steps when your messages ask for them, and still asks before spending money.
- In an app window, each step returns the window as it is after it, so Pebble needs no separate look between steps.
- Activity names each press in an app window and what it changed, including the app's own reply.

### Changed

- In Full access Pebble now approves its own steps when your messages ask for them: using a window or a site, the
  mouse and keyboard, presses that send, save or delete, commands beyond its sandbox, installs and Git pushes. Pebble
  checks each such step against your own messages only (never a page's or a window's text) and quotes the words of
  yours that ask for it; Activity shows each one as "Approved by Pebble (you asked: …)". It still asks before
  anything that may spend money, before a step your messages don't ask for, and whenever one of your own hooks asks
  for review. Signing in stays yours. Ask and Auto are unchanged.
- In an app window, each step Pebble takes now returns the window as it is after that step, so Pebble goes straight on
  to the next press instead of looking at the window again between steps. Every press is still checked against the
  window first, and Pebble still looks again for a screenshot or when the window changed in a way it cannot follow.

### Fixed

- After Pebble's move in Chess, the computer's reply a moment later made Pebble say its own move was not made (Black
  had captured on d4). A step's result now waits until the app window settles, so it includes the app's own answer.
- When Pebble says it did something other than what you asked ("one square to e3 instead of two") while the app window
  is still allowed, it is asked once to undo it in the app (Undo, or a game's Take Back) and do what was asked, or to
  say plainly that it cannot; Past experiences keep such a task as not completed.
- Activity's app steps are titled by the press itself ("Pressed “e3”") with what changed as the detail, also when the
  app rearranged its window (the move that changed the board read only "Pressed “e3”.").
- Asked to draw on a picture in Preview, Pebble asked for the window without pressing and then could not ask for more
  (asking again answered "already allowed"). Drawing, moving and similar words now ask for pressing, and a request for
  more than an allowed window allows is a new card (in Full access, Pebble's own check).
- In Settings › Memory a record's Forget button could miss a mouse click: the list scrolled inside the scrolling
  Settings sheet. The list now scrolls with the sheet.
- Past experiences kept tasks Pebble said it could not do ("I couldn't draw the rectangle", "the move was not made") as
  finished; they read "not completed" now, and a press in an app window reads "Window or tab action" (it read "Chrome
  tab action").
- The companion's "Needs your review" pill opens the companion when the waiting card belongs to a workspace
  conversation too (it did nothing).
- The keyboard card says "Types this" for typing, and no longer shows "(Unknown)" for a control macOS can't name.
- Windows Pebble can use says a window stays allowed in its conversation for up to 15 minutes (it said "one task at a
  time").

## [1.9.13] (local build, not released)

### Changed

- Pebble can open a file in an app ("open UXTEST-canvas.png in Preview"), after the same review as opening the app; the
  file must exist and be outside the protected places. Before, Preview opened on its Open panel, which lists your
  folders, and Pebble asked to use that panel. Pebble no longer asks to use an Open or Save panel at all.
- Settings › Memory › Past experiences says "not completed" for a task Pebble said it did not finish (it said
  "finished", meaning only that the task had ended).
- In a task that works an app or a page, each step used to send the model every earlier look at the window again; a
  look that a newer one replaced is now sent as a one-line note, so long app tasks carry less old text.
- Details › Context › Context use lists screenshots of windows or the screen on their own line, so you can see when
  Pebble looked by picture.
- Activity folds each model turn's routine steps (memories recalled, connecting, the reply, a clean check) into one
  row named by the reply, so the steps that did something stand out; a check that found something stays its own row.

### Fixed

- When a click was refused because something else covered that spot (in audit round R6, a macOS alert), Pebble did not
  say what was in front; the refusal names it, so Pebble can tell you instead of blaming an earlier step.
- A file Pebble made with a shell command could land in ~/Pebble Workbench instead of the folder you named, without
  saying so; Pebble is told where its commands start and to save at the full path you gave.
- While a card waits, the companion's "Waiting for your review" pill opens the companion on the card when you click
  it (it let clicks through to whatever was behind it).
- The composer's provider, model and effort pickers cut their names mid-word ("ChatGP", "recommende"); the usual names
  fit now.
- In Context use a category name could wrap below its colour swatch; the swatch stays with its name.
- Asked to make and keep a tool, Pebble could ask twice (a second card for the very same tool); a tool already kept, or
  already waiting for your answer, is not asked about again.
- Activity's app steps say what was pressed and what changed ("Pressed “C”: 100×10 → 100×"), or that nothing changed.
- Inside a project, its row under Your space starts a new conversation there, like the other project rows (its tooltip
  said "Connect another project"; Connect a project stays in the + menu).
- Asked to move a Chess piece, Pebble clicked a square by its own estimate of where it was and missed (d5 for d4);
  Pebble is told to act on what the app names exactly and to click by position only where the app names nothing.
- When the model answered nothing after a step, the task ended on "Model returned no text or tool calls" with a
  technical line; Pebble now asks it once to answer, and otherwise stops with a plain message. A step's line in the
  conversation reads in words ("Pebble pressed “e4”: the window changed as expected.").
- The window card said both that ordinary presses run without asking and that each press waits for you, and "for this
  task" beside "in this conversation"; it says one thing now, matching your permission mode.
- In Calculator with an earlier sum on its display, Pebble pressed C (which clears only the last entry), then one key,
  and stopped with an answer from its own arithmetic. A window with a keypad now tells Pebble how to clear it and to
  finish the sequence, and a reply that says an app task is unfinished while the window is still allowed is held once
  so Pebble carries on or names what blocks it.

## [1.9.12] (local build, not released)

### Highlights

- A site or window you allow stays allowed for the rest of the conversation, up to 15 minutes.
- In Full access, ordinary presses and clicks in a window you allowed no longer ask each time.
- Pebble can now look at app windows in pictures and use the mouse and keyboard there, after macOS allows it.

### Changed

- A site or window you allow stays allowed in that conversation for up to 15 minutes: the next message there carries
  on with it instead of a new card. Sending a message in another conversation ends it. Cards for sending, buying or
  deleting still come every time.
- In Full access, ordinary presses and clicks inside a window you allowed run without a card. Anything that looks like
  sending, buying, deleting, saving, signing in or sharing still asks, as do key shortcuts, unlabelled targets and
  anything outside the window; Ask and Auto keep a card per press, and the first use of the mouse and keyboard in a
  window still asks once.
- Working an app by screenshots, mouse and keyboard is on by default (it was measured on this Mac in audit round R6).
  It still needs Take screenshots and Screen control in Settings › Permissions and macOS's own permissions, and macOS
  may ask about pictures of the screen from time to time.
- Asked for a window it already holds in this conversation, Pebble carries on with it instead of asking again.
- In Settings › Memory, the Keep and Forget buttons that confirm a forget name the memory they act on, for VoiceOver.

## [1.9.11] (local build, not released)

### Changed

- Asking Pebble to remove a tool it kept ("Remove uxtest_c_to_f from your Toolbox") is answered at once with that
  tool's Remove card; nothing is removed until you press Remove. (Pebble used to search for a way to do it itself.)
- Connecting a project while a conversation with messages is open starts a new conversation in the project; the open
  one stays where it was. Inside a project, its row under Your space is marked as the current space, and the empty
  conversation's first card works in the project instead of offering to connect one.
- A schedule's review lists its limits once, and says that macOS notifications follow their own switch in Settings.
- A tool you remove from the Toolbox can be put back for ten minutes: Settings › Skills & connectors lists it with Put
  back. After that it is gone for good.
- Activity names app, browser and Companion steps in words instead of tool names, and says whether a look at an app
  window had a screenshot.
- The window card and Settings › Permissions say that macOS may ask, from time to time, whether Pebble may "bypass
  the system private window picker"; choosing Allow lets Pebble take pictures of the window you share (never sound).
  Settings no longer says macOS asks only once.

### Fixed

- In the Mac's Chess every square still needed its own card, because Chess names each square twice; board squares are
  pressed without a card, as 1.9.10 meant, and a press card's heading names the press and what it presses.
- When no screenshot arrived because macOS was asking whether Pebble may see the screen, Pebble said nothing; it now
  says so and carries on from the window's text.
- In Calculator, Pebble read the display after its own key press as what was there before and stopped. Each press now
  reports what it changed, and Pebble is asked to clear old input before entering new input.
- An app Pebble opened for a task could cover the card asking to use its window; Pebble is asked to open such an app
  in the background.
- Asked to open a picture in Preview, Pebble asked to use Preview's Open panel, which lists one of your folders, and the
  card did not say so. A window that looks like an Open or Save panel is now named as one on the card (Pebble would
  read the names of the files it lists), and Pebble is told never to ask for one.
- Over a card the companion can answer itself, its summary said the review was in the workspace.
- While a card waited, the pebble shrank to a thin "!" and a click where it had been went through to the desktop; its
  whole circle takes clicks again.
- When an approved step was refused before anything happened (the window had changed since the card), the task kept
  waiting for a review that no longer existed: no reply, and the companion kept saying "Waiting for your review". The
  task now continues with the refusal, as after Deny, and Pebble answers.
- With another app in front, pressing ⌘Q while typing in the companion quit Pebble, and any task it was running,
  instead of that app; while Pebble is not the active app it now ignores ⌘Q, ⌘H and ⌥⌘H. Quit Pebble from its menu-bar
  icon, the Dock or the workspace.
- Next hour in a new schedule picked the whole hour after next (12:00 at 10:43); it picks the next whole hour at least
  five minutes away.
- Context use, at the drawer's normal width: the Refresh button and the category names no longer break mid-word, and
  the table's columns fit.

## [1.9.10] (local build, not released)

### Highlights

- The companion lets you allow a site or an app window right where you are.
- Website and app control work in project conversations too.
- With notifications on, Pebble tells you when a reply finishes or a card waits while it is in the background.

### Fixed

- Working an app by screenshots, mouse and keyboard was never really switched on in the app: the setting that turns it
  on did not reach Pebble's local service. (It stays off by default until it is measured on this Mac.)
- In the Mac's Chess, Pebble selected a piece, saw no change in the window's text, pressed the same piece again and gave
  up. It now knows a selection may not show there and makes the second press (the square the piece moves to); board
  squares are pressed without a card, like keypad keys.
- A card warned "may send, submit, save, delete or buy" for any press in an app; it does so only when the button looks
  like one, and the window card no longer says every press asks.
- In Calculator, Pebble pressed a key, saw the key itself unchanged and stopped, though the display had changed; a
  change anywhere in the app's window now counts as the press working.
- Behind the full-screen workspace, a window you let Pebble use could fail to come forward ("The window could not be
  read"); Pebble now brings its app forward another way and waits for macOS to switch desktops before reading it.

### Changed

- Website and app control work in project conversations too (they were offered only in conversations without a
  project): Pebble asks for a site or an app window there as everywhere, and every card still applies.
- The Toolbox's View code and Remove buttons name the tool they act on, for VoiceOver.
- The companion answers simple cards itself: a site in your browser, an app window, or bringing a window forward can
  be allowed or denied there (Details opens the workspace); every other card is still reviewed in the workspace.
- With notifications on, Pebble tells you when a reply finishes or a card waits while it is in the background (not
  under Stealth Lockdown, not in quiet hours, and not for scheduled tasks, which have their own); Settings › General
  has a switch for it. The banner names the conversation, never the reply.

## [1.9.9] (local build, not released)

### Changed

- The companion's Activity lists each step once, marked done when it finishes (each step used to appear twice).
- After a web page is read and attached, the message box has the keyboard again, and the page's chip says "Read" and
  the site, with the whole address and how much was read in its tooltip.
- Memory impact names a task by the message that asked for it, as the context view does.
- The companion's header says which project its conversation is in, and connecting a project there says so.

### Fixed

- A decided Workshop keep card stayed below later messages, as if the newest reply asked to keep a tool.
- A conversation opened from the list, a notice or the companion's Open kept the previous one's scroll position (often
  its top); it opens at its end, where its latest task is.
- Naming a browser in a request ("… in Chrome") made Pebble treat it as an app to use instead of the website.
- A lone + or − button in an app (a list's Add or Remove) was pressed without asking, like a calculator key; it asks
  again. Calculator's Per cent key no longer asks.
- Pebble's "No tool was kept in this task" note could follow a reply that only mentioned the Toolbox.
- Working an app by screenshots: when macOS has not allowed Pebble to record the screen, Pebble now says so and where
  to allow it (it said no setting could help), and a window on another desktop is brought forward first, after your
  approval, when Pebble needs to see it.
- Working in an app on another desktop (behind the full-screen workspace), Pebble read the window as unchanged after
  a key it had really pressed, and stopped. A window you let Pebble use now comes forward while Pebble works in it,
  without a second card.
- The window card no longer promises that every press waits for you: keypad keys, tabs, checkboxes and switches are
  pressed at once.
- With another app in front (one Pebble opened for a task), the first click on the workspace only brought Pebble
  forward, so Allow on a card needed a second click; the first click now counts.
- A site card's heading named every address in full and broke one mid-word; it names each site once ("Use
  youtube.com"), and the card lists the addresses.
- After a task in the workspace, the companion still showed its older conversation, and its Open full workspace took
  the workspace there; the companion now follows the conversation you used last.
- "Remember that …" after a short tag or greeting ("Note: …", "Hey Pebble, …") was not saved: the request went to
  the model, whose memory tool refused a reworded fact. Pebble saves it directly, as without the prefix; "Forget
  that …" works the same way.
- Coming back to the workspace moved the keyboard to the message box even from a Details drawer control you had reached
  with the keyboard; only a title-bar or sidebar button that took focus by itself gives it up now.

## [1.9.8] (local build, not released)

### Highlights

- A source marker in a reply ([S7]) opens the page it names.
- Asked to open or use an app on this Mac by name, Pebble uses the app instead of a website.
- Details › Activity names each Code Mode step by what it does.

### Changed

- A source marker in a reply ([S7]) is a small button: in the workspace it opens the page the task recorded under that
  number and says which page it was; in the companion it says where the sources are listed.
- Details › Activity names each Code Mode step by its purpose (or its length), and a script that failed reads as one
  sentence instead of a Python traceback.
- Help › Keyboard Shortcuts shows the shortcut list, and Help › What's New opens the release notes.
- The context view names a task by the message that asked for it, and its table keeps numbers and headings whole.
- Notices appear just above the message box instead of over its pickers.
- The space you are in is bold in the sidebar, so the row under the pointer no longer looks chosen too.

### Fixed

- Asking Pebble to remember something, or to forget it, also kept the words in a task record, which Forget left
  behind.
- Opened from the companion while it was already full screen on its own Space, the workspace lost every key (typing,
  Escape, ⌃⌘F and View › Toggle Full Screen) until Pebble was activated again.

## [1.9.7] (local build, not released)

### Changed

- Stopping a reply while it is being written keeps the words written so far, marked as stopped before Pebble checked
  them.
- ⌘K offers "Review the waiting action", which opens the waiting card with the keyboard on it.
- With All providers, the model list is grouped by provider, so look-alike models say which account answers.
- The Details drawer keeps the response's raw text behind a disclosure; the conversation shows it as it is written.
- Asked about mathematics, Pebble writes it in its replies as plain text, not LaTeX.
- On a long web page Pebble reads what is in view first, so scrolling brings more of the page within its reach (a link
  far down an article was never found).
- A site search no longer includes the site's own name, and a Wikipedia search opens the matching article directly;
  "open the article" asks for pressing from the start.
- Conversation search excerpts show plain words from a word's start, and the search field has its own clear button.
- Keys of a keypad (Calculator's digits and operators) are pressed without a card; anything that may send, buy, delete
  or confirm still asks.
- Pebble has more time between looking at a window and acting in it, so slower models no longer find every look stale.
- Asked to open or use an app on this Mac by name, Pebble uses the app rather than a website.
- Asked what it can do, Pebble names its own abilities.
- Settings and the README say what Lockdown now does: clicking or typing in the companion leaves the app you were in
  active.
- A site request's card says what pressing really means: in Full access a link within the allowed sites opens at once,
  while sending, buying, deleting, signing in or leaving those sites still waits for you. It used to say every press
  waits.
- Over the workspace in front, the pebble is hidden instead of dimmed, so its status no longer lies across a card.
- Review → brings the waiting card's buttons into view; Pebble's line about waiting actions no longer says "below".
- A schedule's review says its model, permissions and limits in words; the fingerprint and exact configuration are
  under Technical details.
- Once starts at a real time (the next whole hour), with Next hour and Tomorrow at 9:00 beside it; the grey date it
  showed before was only a placeholder.
- With notifications off, the options that depend on them are off too, and a change waiting for Save says so.

### Fixed

- After a site was allowed, Pebble's line still pointed to "Windows Pebble can use"; it names Details › Tools.
- A review card's heading broke a site address in the middle.
- The pebble kept ignoring clicks after the workspace left full screen and no longer covered it.
- Typing after a click in the conversation lost the letters instead of starting a message.
- Escape in the conversation search closed the Details drawer instead of clearing the search.
- Pebble's Workshop failed to keep a tool when asked in the order its own help shows, and the reply said it had kept
  it; a reply that claims a keep that did not happen now says so underneath.
- Opened from the companion, the workspace could leave a header button focused, so typing went nowhere.
- A task that waited for your approval reported only the time before the card ("Worked for 7 s" for 37 s of work).
- Reviewing a schedule scrolled the whole workspace inside its window, hiding the header and leaving an empty band.

## [1.9.6] (local build, not released)

1.9.5 was built and checked on the real app but not installed: opened from the companion, the workspace did not take
the keyboard. 1.9.6 is 1.9.5 with that fixed, and the following.

### Changed

- The delete dialog names the conversation it deletes.
- Screenshots with mouse-and-keyboard control (still off by default) now click and type in apps: the Dock's own window
  no longer counts as covering the point, and when macOS cannot say which app holds the keyboard, the app in front does.
- When a task deletes files you asked it to delete and then finds them gone, the task is no longer held as unverified.

### Fixed

- Opened from the companion, the workspace did not take the keyboard: ⌘W and typing went to the closed companion.
- Escape still took the full-screen workspace out of full screen until something in it had been clicked (1.9.5's fix
  held only then), and right after the workspace opened its keys did nothing: the conversation now has the keyboard as
  soon as the workspace shows.
- Over the workspace in front, the pebble did not always let clicks through to the review card's buttons.
- The companion's search showed two clear buttons.

## [1.9.5] (local build, not released)

Website tasks finish what they start, the companion stays out of your way, and Pebble can explain itself.

### Highlights

- Asked to play, watch or click something on a website, Pebble asks for pressing from the start, so it can press the video it finds.
- Clicking the companion leaves the app you are in active, so Stealth Lockdown keeps the keyboard where it was.
- Conversation search finds what was said, not only titles.

### Changed

- Opening the workspace from the companion shows Pebble's own menu bar.
- Closing the workspace, going back to the companion or turning on Stealth Lockdown keeps you on the Space you are on.
- The menu-bar icon's menu adds New Conversation and Settings…, and VoiceOver reads the icon's name.
- Asked about Pebble itself, Pebble answers from a short guide to its own windows, settings and keys.
- In a project conversation, a website or app task is told that it works in a conversation without a project; a new
  conversation started from a project conversation says it is outside the project, with a way to start it there.
- A half-typed message in the companion comes along when you open the full workspace.
- A pebble that says Blocked, or waits for your review or answer, opens on the conversation that needs you.
- Typing while no text field has the focus goes to the message box.
- When Pebble may not press on a site or in a window, it says how to ask you for it.
- Code Mode steps always say what is running.
- The window request's note says where to end access: Details › Tools.
- Help names the online guide.
- Settings no longer holds a Delete link for the conversation behind it; each conversation's row has one.

### Fixed

- Over the workspace in front, the pebble covered the review card's buttons; it now lets clicks through and dims.
- Escape took the full-screen workspace out of full screen, even the Escape that closed Settings.
- The Workshop's independent check failed correct tools whose numbers differed only in form (77 against 77.0), so no
  tool was kept on its own.
- A blocked task's note showed words written for the model.
- Near the left edge of the screen, closing the companion's sidebar moved the panel.
- A pebble dropped over the Dock or past the screen's edge now settles back where you can see it.
- Bringing the workspace back could leave New conversation focused instead of the message box.

## [1.9.4] (local build, not released)

The companion is easier to use every day: replies can be copied, the model button says who answers and bills, and the
workspace stays where you left it.

### Highlights

- The model button says who answers and bills: a ChatGPT plan, an API key or this Mac.
- Every companion reply has a Copy button, and right-click no longer offers Reload.
- The workspace no longer comes back by itself after you return to the companion.

### Changed

- While the model list shows another provider, it names the model still in use until you choose one; providers show
  short names instead of clipped e-mail addresses.
- Closing a menu with Escape leaves the cursor in the message box.
- The companion's conversations show when each was last used, its search clears with Escape or its × button, and its
  Chats, Activity and Views are tabs.
- A new conversation in the companion keeps the words you were writing.
- The companion's menus stay below its header.
- A double click on the pebble no longer opens and closes the chat at once.
- Parked near the top of the screen, the pebble opens its chat downwards, and returns to its place when the chat closes.
- A plain chat reply no longer ends with a task summary.
- When Pebble cannot write in a folder outside your home folder, it says where it can write.

### Fixed

- After going back to the companion, the workspace could come back by itself, or stay on the desktop as a window.
- The Privacy & Stealth, Appearance and General tabs showed the Skills & connectors page above their own settings.
- ⌘K in the companion now finds its conversations and opens the workspace, as in the workspace.
- A file inside your project named by its full path was refused as if it were outside the project.
- ⌘K in the workspace also opened the companion's palette.
- A dialog in the companion drew a grey box around the panel.
- The companion's task summary showed a note meant for the model; it shows only words for you.
- Asked to type or press in an app, Pebble is now guided to ask for the app's window instead of stopping after opening
  it.

## [1.9.3] (local build, not released)

The companion opens with one click from any app, the workspace keeps its menu bar, and Pebble can now see a window as
a picture and use the mouse and keyboard there, built and kept off until it is checked on a real Mac.

### Highlights

- Clicking the companion from another app opens it the first time; before, that click only woke Pebble.
- After going back to the companion, the workspace returns with its own menu bar.
- Pebble can see a window as a picture and use the mouse and keyboard there, off until it is checked.

### Added

- **Seeing a window and using the mouse and keyboard** (off by default): with the window you shared, Pebble can take a
  picture of that one window, click a point on it, scroll, press keys and type. Before each action it checks that the
  point is still on that window and what is under it; anything that could send, buy, delete or share waits for you,
  and so does the first action in each session. An action that changes nothing on screen is never repeated as it was.
  It stays off until it is measured on a real Mac.

### Changed

- Settings names Pebble's Workshop and Code Mode where kept tools live, with an example to try.
- With no model connected, the model button says Connect a model… and opens the ways to connect.
- Follow the pointer and Expressive reactions read off while Reduce motion is on.
- In a small window, a Conversations button opens the conversation list over the window.
- The README's limits say what Pebble now does in Comet and in other apps' windows.

### Fixed

- The first click on the companion from another app was swallowed.
- After ⌘W and a click on the companion, the workspace could come back with another app's menu bar.

## [1.9.2] (local build, not released)

Asked to play something in Comet, Pebble now stops once it plays instead of pausing it again, and Settings, the first
launch and many smaller places are easier to use, from a real-user review of the whole app.

### Highlights

- When Comet shows a page playing, Pebble says it plays and no longer presses Play or Pause again.
- Settings has a tab for every part, in a sidebar: Privacy & Stealth, Appearance and General are new.
- Sending before a model is connected keeps your words and shows the three ways to connect.

### Changed

- Pebble's own Comet window comes forward when it opened on another desktop, so Pebble never works from an out-of-date
  view of the page, and a press that opened a new page counts even before the page shows the words Pebble expected.
- Pebble no longer opens a page in your default browser while it is already asking for that site.
- The approval for a website names where Pebble works: "in a new Comet window" or "in its own tab in your browser".
- The first launch offers an API key (Anthropic, OpenAI, Ollama, LM Studio) beside ChatGPT and a local model.
- The provider form fills the usual address for Anthropic and OpenAI, links Ollama and LM Studio, explains a failed
  fetch in plain words, and has a Cancel button. A click beside Settings no longer closes it.
- Forgetting one memory asks first; Forget all says that an exported archive won't bring the memories back, and says
  what it did.
- The Skills & connectors page explains itself in plain words.
- The menu bar has Pebble's own commands with their keys: Settings…, New Conversation, Find Conversations, Command
  Palette, Details, Task Activity and Floating Companion. Help opens the guide, the keyboard shortcuts, what's new and a
  problem report.
- ⌘K finds and runs actions, Settings pages and conversations, not only slash commands.
- Projects you have used stay listed in the sidebar, and choosing one starts a conversation there; the project button in
  the window's header opens a menu of them.
- A terminal command you run from + shows its command and output in the conversation.
- Undo after deleting a conversation stays longer and waits while the pointer is on it; Recently deleted is linked under
  the conversation list.

### Fixed

- Settings' title and close button no longer scroll away.
- The footer under the message box no longer wraps into narrow columns.
- The Memory impact table no longer breaks words a letter per line in the details drawer.
- The mod manifest field was white in dark mode.
- A dialog no longer opens with an error left over from before it.
- The companion's tip said "Tap"; it says "Click".
- Start at login explains what to do when Pebble cannot add itself.

### Known limitations

- Stealth Lockdown keeping keyboard focus off Pebble is fixed in source and checked on the real app before this build
  is installed; if the check fails, this line stays.
- Seeing an app's window as an image, and mouse and keyboard control, stay off, as in 1.9.1.

## [1.9.1] (local build, not released)

Pebble now works on websites in Comet, reads app-style sites such as YouTube, and says something plays only when your
Mac shows it playing.

### Highlights

- In Comet, Pebble opens the page in a new Comet window and works there, only on the sites you approve.
- Pebble can read sites built as apps, such as YouTube, which its own tab could not read before.
- When you ask Pebble to play something, it says it plays only when your Mac shows sound or video starting.

### Added

- **Websites without the extension**: when Pebble asks for a site and your browser keeps extensions out of its pages
  (Comet), or no extension is connected, your one approval opens the first page in a new window of your browser
  (Comet, Chrome, Brave, Edge or Chromium), and Pebble reads it through Accessibility. It reads only that window's
  page, never the browser's other tabs, and only while the page is on a site you approved; sign-in and payment pages
  stay yours.
- **Pebble's own tab in the background**: Pebble reads and presses in its own tab while it stays behind the tab you
  are using. Typing brings its tab forward first, and a browser starts sound or video only in a tab that is showing.
- **A playback check**: asked to play something, Pebble checks the record macOS keeps of what is playing (the same one
  that keeps your Mac awake during a video) and says it plays only when something started during the task.

### Changed

- When the task is to do something on a page, Pebble asks for the site instead of first opening the page in your
  default browser.
- When Pebble cannot read a page, it now says why (for example, that the browser keeps extensions out of its pages)
  instead of "The window could not be read."
- Connect a browser for Comet also writes Pebble's connection file in Google Chrome's folder, which is where Comet
  looks for it. Pebble for Chrome is now 0.2.1: reload it in your browser's extensions page.

### Fixed

- Pebble's own tab could not read sites whose page boxes have no height, such as YouTube.
- Pebble's own tab could not open while the browser had no window open.
- In the extension's popup, the browser list closed while you chose, and Try again did nothing.
- A slash command's reply now appears under it in the main window; before, it appeared only after reopening the
  conversation.
- The items of the ⋯ menu in the details drawer now open their sections.

### Known limitations

- Stealth Lockdown still does not keep keyboard focus off Pebble's windows: the 1.9.0 fix did not work on the real app
  (see below).
- Seeing an app's window as an image, and mouse and keyboard control, stay off, as in 1.9.0.

## [1.9.0] (local build, not released)

Pebble can now go straight to the page you mean, work on websites in its own tab in your browser, reach a window on
another desktop, and do simple things in Shortcuts, Music, Notes, Calendar, Mail and Finder without touching the screen.

### Highlights

- Asking Pebble to play something on YouTube opens YouTube's own results, then Pebble plays it in the window you allow.
- With Pebble for Chrome 2, Pebble opens its own tab in your browser, only on the sites you approve for the task.
- Fixed actions for Shortcuts, Music, Notes, Calendar, Mail drafts and Finder; a shortcut always asks you first.

### Added

- **Site search**: OpenURL can open a site's own search results (YouTube, Google, DuckDuckGo, Bing, Wikipedia, GitHub,
  Reddit, Stack Overflow, Amazon, Maps, and sites that describe their search with OpenSearch). It opens without asking
  when the search words are your own, and asks you when they came from anywhere else.
- **Opening apps in the background**: Pebble can open an app without bringing it to the front. If the app comes to the
  front anyway and you did nothing in between, Pebble puts your app back; otherwise it leaves things as they are and
  says what changed.
- **App actions**: Pebble can list and run your Shortcuts (each run asks you), play, pause and skip in Music, search,
  read and create notes, list and create calendar events (no invitations), count unread mail and leave a draft for you
  to send, and reveal or open folders in Finder. macOS asks you once per app. Pebble never sends mail or deletes
  anything this way, and never uses shell scripts, UI scripting or JavaScript for it.
- **Websites in your browser** (Pebble for Chrome 0.2.0, loaded in Developer mode): when Pebble asks to use a website,
  you approve the sites it may act on and the sites it may only read; Pebble then opens its own tab, in its own tab
  group, and works there. On a site it may only read, it never clicks or types. Turn on "Let Pebble open its own tabs
  here" in the extension's popup to let it start a tab. Connect a browser writes Pebble's connection file for each
  browser you tick (Chrome, Comet, Brave, Edge, Arc, Chromium).
- **The window you mean**: Pebble names a window by its macOS window number, so a page changing a browser window's
  title no longer loses it, and it can read an app's front window on another desktop. When a window is minimized, or
  on another desktop with nothing readable, Pebble asks once to bring it forward.
- **Chromium and Electron apps**: in an app you allowed (Comet, Chrome, and Electron apps), Pebble switches on the
  page's accessibility so it can read the page, and switches back what it changed when it is done.
- **Fewer cards in Full access**: following a link inside the sites you approved is a routine step there (and in Auto
  inside a project). Anything that may send, submit, buy, delete, sign in or grant a permission still waits for you,
  in every mode.

### Changed

- Pebble no longer says a video plays, or that a task on a page is done, when it only opened the page.
- What Pebble reads from a window now says how complete it is and whether it may be out of date, and every action says
  whether it was sent and what Pebble saw afterwards.

### Fixed

- **Stealth Lockdown** did not keep keyboard focus off Pebble's windows: macOS gave them a subclass the switch did not
  recognise, so none became a window that never takes focus. Pebble now recognises it and puts it back afterwards.
  *Correction (1.9.1): measured on the installed app, this did not work: the window could not take the new class, and a
  click still brought Pebble forward. It remains a known limitation.*

### Known limitations

- Pebble's own tabs in your browser are proven in Pebble's tests, not yet in Comet or Chrome: load the updated
  extension, pair it and turn on its switch to use them.
- Seeing an app's window as an image (with on-device text recognition) and mouse and keyboard control, Pebble's last
  resort for apps that show little to Accessibility, are not switched on in this build: they wait for your Screen
  Recording and input permissions and for their measurement with you.
- If Pebble itself restarts while it has a Chromium app's page accessibility on, it cannot switch it back; the app
  keeps it until it quits.

### For contributors

- AX worker protocol `pebble-ax/2` (window numbers through `_AXUIElementGetWindow`, unlisted front windows, `enhance`,
  `restore`, `raise`); observation envelope version 2 and typed act results; `screen_session.capabilities()` and the
  HOST CAPABILITIES line; `site_search`, `app_actions`, `native_apps.watch_focus`; `BrowserRequest` and the
  `browser_grant`, `screen_raise` and `app_action` actions; Pebble for Chrome's `pebble-browser/2`;
  `scripts/validate_phase43.py`; switches `PEBBLE_WINDOW_NUMBERS`, `PEBBLE_AX_ENHANCE`, `PEBBLE_SITE_SEARCH` and
  `PEBBLE_APP_ACTIONS`.

## [1.8.0] (local build, not released)

Pebble can now read and operate the windows of your other apps, one window at a time and only the ones you allow.

### Highlights

- Pebble can ask to use an app's window during a task; you allow it in Pebble, for that one window.
- It reads the window through macOS Accessibility and can type, choose and press; every press asks you first.
- Stealth's workspace no longer lands in a full-screen app, and approval cards say what each action does.

### Added

- **App windows**: Pebble reads an app window's buttons, fields and text through macOS Accessibility (once you allow
  Pebble in System Settings › Privacy & Security › Accessibility; Windows Pebble can use offers "Ask macOS for
  Accessibility…", and macOS asks you itself). In a conversation without a project, with Screen control on, Pebble can
  ask to use one window of a running app for the current task: the request shows the app, the window as the app names
  it, why Pebble asks and what it could do, and nothing happens until you allow it. Windows Pebble can use also lets
  you share a window yourself, as before.
- In a window you allowed, Pebble can focus, scroll, open and close sections, choose options, tick boxes and type
  (typed text replaces the field's text). Anything that may send, submit, save, delete or buy waits for your review,
  showing what Pebble would press and what is next to it, and is never retried. Before each action Pebble checks that
  the element is still the one it looked at. It never reads or types into a password, code or card field, and at a
  sign-in, a code or a check it stops and hands the window to you.
- Pebble never operates terminals, System Settings, Keychain Access, password managers, apps that run code or erase
  disks (Shortcuts, Automator, Script Editor, Disk Utility, the Installer, Activity Monitor), or itself.

### Fixed

- **Pebble's own browser** refused every click and keystroke on a Retina display, reporting that the element had moved:
  Chrome describes a page in screen pixels there. Pebble now measures the page's scale on every read.
- **Stealth Lockdown**: if Pebble started while a full-screen app was in front, opening the workspace in Stealth later
  went to that app's Space instead of the desktop. Pebble now waits for a desktop to place what it uses to get there
  (this needs the Accessibility permission; without it, Pebble behaves as before).
- An approval card for a Shell command or a file change showed "Open undefined" as its title.

### Known limitation

- macOS lists only the windows on the current desktop: a window that is minimized or on another desktop has to be
  brought back before Pebble can use it.

### For contributors

- `ax_worker.MacBackend` (ctypes: the Accessibility client API) behind `ax_worker.production_backend()`; the
  `ScreenRequest` tool and the `screen_grant` action (`screen_session.request_proposal`, `grant_requested`); the
  `app_control_status` and `app_control_request` routes; `PEBBLE_SHELL_DIAG`'s `ax_probe` command (experiment E-N2).

## [1.7.0] (local build, not released)

Pebble's Workshop, part 2: Pebble can now make small tools for itself while it works, test them, and keep the good
ones in Pebble's Toolbox.

### Highlights

- Pebble can make a small tool while it works, test it, and use it at once.
- Pebble's Toolbox keeps tools that passed every check; Pebble always tells you, and you can remove them.
- A tool is kept on its own only after an independent check; any other keep asks you first.

### Added

- **Making tools**: inside a RunCode script, `pebble.make_tool(name, purpose, code, tests, inputs)` makes a pure
  calculation (no network, files or processes; imports only from a short list of safe modules). It is checked before it
  runs, then validated in a fresh sandbox against its own tests and about 50 inputs Pebble generates from its input
  description, each run twice. It is usable at once as `pebble.mine.<name>(...)`.
- **Pebble's Toolbox**: `pebble.keep_tool(name, reason)` keeps a tool. Pebble keeps one on its own only if it passed
  validation, helped with your task, was made before any web page, file or command output could shape it, and passes a
  blind check (a separate request writes tests from its description alone, never seeing the code). Pebble then always
  tells you, with View code and Remove. Any other keep asks you first: Keep or Discard. Settings › Skills & connectors
  lists the Toolbox.
- A kept tool is checked again before the first script in a session uses it, and a tool that changed is never run.
- RunCode also works in a conversation without a project, with 1.6.0's commands and files.
- FindTool also finds kept tools.

### For contributors

- `PEBBLE_TOOL_MAKING=0` turns tool making off, leaving 1.6.0's RunCode. New module: `workshop.py`.

## [1.6.0] (local build, not released)

Pebble can now work on your Mac in a conversation without a project, the way you would at a terminal: inside a sandbox
that keeps your private places closed, and asking before it goes further.

### Highlights

- In a conversation without a project, Pebble can run commands, read files and take screenshots on your Mac.
- Commands run in a sandbox: they write only in Pebble Workbench and have no network unless you allow it.
- Pebble asks before it writes elsewhere or reaches a website, and keeps a copy so you can undo the change.

### Added

- **Shell**: one command line (pipes allowed) inside a macOS sandbox made for that command. It reads your home folder
  except protected places (keys and passwords, browser data, Messages, Mail, Pebble's own data, other assistants'
  sign-ins and your shell history), writes only in `~/Pebble Workbench` and the folders you allowed, and has no network
  unless you allowed the websites it names (through Pebble's egress broker, which refuses private addresses). Pebble
  asks before anything beyond that. "Always allow" keeps a folder or website allowed; Settings › Permissions lists them
  with Remove. Deleting or moving anything outside the workbench asks every time.
- **ReadFile, WriteFile and EditFile** for files on your Mac, images and PDFs included. Writes outside the workbench ask
  first, and a copy is kept before every change ("Undo this change").
- **Screenshot**: once you allow Pebble in System Settings › Privacy & Security › Screen & System Audio Recording,
  Pebble takes a screenshot when your request needs it, and it appears in the chat. The new switch "Take screenshots
  when a task needs them" turns it off.
- **FindTool**: Pebble finds its other tools by describing the job, instead of relying on the words of your request.
- Pebble knows it runs on macOS.

### Safety

- Every command is split into its parts and each part is checked; AgentGuard's rules run in conversations without a
  project too. Privilege changes, system settings, keychain access and other apps' processes are refused.
- Text from web pages, files, commands or screenshots cannot make Pebble reach the network or change files outside the
  workbench without asking, and a web address or search that would carry words from your files is refused.
- After three refused actions in a row, Pebble stops and asks you how to go on.

### For contributors

- `PEBBLE_COMPANION_TOOLS=0` turns the new tools off, leaving 1.5.x's requests. New modules: `shell_policy.py`,
  `companion_shell.py`, `companion_tools.py`.

## [1.5.3] (local build, not released)

1.5.2's changes below, unchanged, under the next version number: a 1.5.2 test build without the Stealth Lockdown
fixes was already installed on the maintainer's Mac, and Pebble's installer only accepts a higher version. Until 2.0.0,
new versions are built and installed only on the maintainer's Mac.

## [1.5.2] (not released)

The floating companion now really stays with you over full-screen apps, and Stealth Lockdown's windows behave: turning
it on in the full-screen workspace is one quick step with the window's buttons in place, and opening the workspace from
a full-screen app takes you to the desktop. 1.5.1 set out to do the first and, on a real Mac, did not; every change here
was measured on a real Mac (macOS 27) first.

### Highlights

- The floating companion now stays on screen over full-screen apps, on every Space.
- In Stealth Lockdown the full workspace opens on the desktop, and turning it on in full screen is one quick step.
- ⌃⌥⌘H hides Pebble, and pressing it again brings it back.

### Fixed

- **The companion over full-screen apps (1.5.1 did not do it).** macOS decides whether a window may appear in other
  apps' full-screen Spaces when the window's Space behaviour is set, from what the window and the app are at that
  moment. Pebble set it once at launch, while it was still an ordinary app, so the companion stayed out. Pebble now sets
  it again after every switch between the companion and the workspace, and puts the companion back whenever a change of
  Space leaves it out.
- **Stealth Lockdown in the full-screen workspace.** Turning it on now waits for macOS to finish leaving full screen
  before Pebble leaves the Dock: doing both at once held the exit for seconds, so the window stayed screen-sized without
  its red, yellow and green buttons and clicks stalled. The workspace also keeps its windowed size through full screen,
  so the exit ends in one animation, with no second resize.
- **Opening the workspace in Stealth Lockdown from a full-screen app** now takes you to the desktop and opens it there,
  as an app opens, instead of over the full-screen app. Without Stealth Lockdown, opening it from a full-screen app now
  reliably enters its own full-screen Space (the slide to the desktop could undo it).
- **⌃⌥⌘H** brings back what it hid when you press it again (the companion, and the workspace if it was open). "Hide
  Pebble now" in the menu still only hides.

### Known issue

- **Stealth Lockdown does not keep keyboard focus off Pebble.** Pebble's windows were meant to become windows that never
  make Pebble the active app, but macOS observes them from the start, which keeps that change from applying: clicking or
  typing in Pebble makes it the active app (the app you were in stays on screen, without focus). It has been so since
  1.1.0. The README and Pebble's Settings now say so, and the fix comes next.

### For contributors

- Launching the shell with `PEBBLE_SHELL_DIAG` set to a folder of yours turns on a diagnostic: timed JSON lines about
  its windows (`shell.jsonl`) and a fixed set of commands read from `command` in that folder (`stealth on`, `stealth
  off`, `open_workspace`, `to_pet`, `panic`, `state`, `latency <ms>`). It is off otherwise. The measurements behind this
  release were taken with it.
- `src/Phase39Shell.test.mjs`; `scripts/validate_phase39.py`. D79 and D80: validate_phase38 accepts the companion helper
  in `reveal` after its policy change, and a checkout past 1.5.1.

## [1.5.1] (not released)

Built and installed on the maintainer's Mac only. It made the floating companion a non-activating panel so that it
would stay over full-screen apps, but on a real Mac it still did not (see 1.5.2). The full workspace opened in its own
full-screen Space, never over another app's full screen, as before.

- D75: validate_phase37 accepts a checkout past 1.5.0 (as D73 did for validate_phase36).

## [1.5.0]

Pebble's Workshop, part 1: Code Mode. Above 8K, with a project open, Pebble can now write a short Python script that
calls its own tools, and only what the script prints comes back. A task that reads, searches and filters many things
can do it in one step instead of carrying every intermediate result through the conversation. Whether this lowers what
tasks cost or how long they take is measured in the next benchmark; no saving is claimed here.

### Highlights

- Pebble's Workshop, part 1: Pebble can run a short script that calls its tools; only what it prints comes back.
- Every call a script makes is checked, approved and recorded exactly like a direct call.
- Turning on Stealth Lockdown in the full-screen workspace now gives an ordinary, centred window with its buttons.

### Added

- **RunCode (Code Mode).** The model writes one Python script that calls this run's tools through a `pebble` module:
  read, grep, glob and file search, Python references, git status, diff and history, run evidence and attachments,
  web search and pages (with the web permission), writes and edits, and plugin calls. Only what the script prints
  returns to the model, as untrusted data; the tools' results stay out of the conversation.
- **The same rules as direct calls.** Each call a script makes goes back to Pebble and is validated, authorised and
  recorded with its own receipt, so file protection, edit review and approvals apply to it: in Ask mode an edit waits
  for your approval, and plugin calls wait for review as they always do. A refused call raises an error in the script
  with Pebble's reason. Commands, tests, helper agents, questions and memory are not callable from a script.
- **A sealed sandbox.** The script runs in a separate process with no network, no access to your files except through
  those calls, no other processes, and writes only to its own temporary folder. It stops after 60 seconds or 200
  calls, and at most 16,000 printed characters come back.
- Offered from a task's first step above 8K with a project, so the tool list never changes mid-task; not offered at 8K
  and below, without a project, or to helper agents.

### Fixed

- **Stealth Lockdown in the full-screen workspace** (reported on 1.4.0): turning it on left the workspace screen-sized
  with the Dock showing and its red, yellow and green buttons gone. It now leaves full screen into an ordinary window
  (its usual size, within 80 % of the screen, centred); capture protection still applies at once, and turning Stealth
  Lockdown off returns the workspace to full screen.

### Changed

- **The ChatGPT route names its run in a `session_id` header,** as that route's own client does, beside the cache key
  it already sent. On the earlier benchmark none of Pebble's input there was served from the provider's prompt cache,
  while most of Codex's was, although Pebble's requests extend each other. Whether this header changes that is
  measured in the next check; nothing else in the request changes.

### For contributors

- `PEBBLE_CODE_MODE=0` keeps 1.4.0's requests (RunCode never offered).
- `backend/pebble/code_mode.py` (the host side), `backend/pebble/code_worker.py` (the child, `main.py --code-worker`);
  `backend/tests/test_code_mode_37.py`; `backend/tests/test_chatgpt_session_header_37.py`; `src/Phase37Shell.test.mjs`;
  `scripts/validate_phase37.py`.
- D72: RunCode is the first model tool added since Phase 18. validate_phase19, validate_phase20, validate_phase22,
  validate_phase24 and test_phase19_security compare the catalogue with Phase 18's tools plus the tools later phases
  added, each named with its phase; any other addition still fails. D73: validate_phase36 accepts a checkout past 1.4.0.

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
