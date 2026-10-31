# pebble-ui-spec v1

What Pebble's windows draw, as data. It has two parts:

- **The work view:** cards of what Pebble is doing, built by Pebble from its own records. It uses no model tokens.
- **Native views:** the kinds a model fills with `UIRender`, which Pebble draws in its own design.

The same JSON reaches the Mac (Vue) and the iPhone (SwiftUI). Neither side ever runs code or HTML from it. This page is
section 3.12 of the Pebble Remote bridge contract.

**Files** (written by `scripts/generate_ui_spec.py` from the code that builds the views; a test fails when they are
stale):

- `docs/ui-spec/pebble-ui-spec-v1.schema.json`: JSON Schema, draft 2020-12.
- `docs/ui-spec/fixtures.json`: one example or more of every view type, an unknown type, a header, a summary and
  every native kind. Pebble's own builders make the work view's examples. Renderers test against these.

## Rules for every renderer

1. **Render by type.** A `view` whose `type` you don't know, or whose fields fail your checks, is not drawn. Show its
   item's `label` and `detail` instead, as Pebble showed every step before 1.9.29. On the iPhone, add the line
   "Update the app to see this view".
2. **Ignore fields you don't know.** Version 1 only ever gains fields and types. A new meaning gets a new type or a new
   `v`.
3. **Plain text only.** Every string is display text. Never turn one into a link, markup or a button's label.
   - The schema lists fields that come from a project, a page, a window or a model under `x-pebble-untrusted`: paths,
     commands, output lines, branch names, commit subjects, window titles and agent names.
   - Show them literally. A branch called "Approve all" is text beside a button, never the button's words.
4. **Buttons never act on their own.** A work-view card has no route of its own. The branch card's one button asks
   Pebble to *prepare* a merge, which lands on the usual review card for the person to approve. Native views keep
   today's authority rules (`artifact_schema.REJECTED_KEYS`, `REJECTED_TEXT`): a model's view can only put text in
   the message box.
5. **Design:**
   - flat surfaces with hairline borders, two font weights, sentence case;
   - colour carries meaning: passed or done in green, failed in red, waiting or open in amber, with the same words
     beside it;
   - dark mode for everything;
   - numbers rounded (52K, 1.2K);
   - at most six chips or rows before "Show more".
6. **Accessibility:** every mark, ring and bar has words. The status line stays the one live region, and cards update
   in place without being announced. Motion stops under Reduce motion.

## Where views arrive

- **Live:** the `activity` event, `{chat_id, run_id, item}`. An item is sent again with the same `seq` whenever its
  label, detail, status, duration or view changes. Keep the newest per `seq`.
- **On request:** `run_activity {run_id, after}` returns `{run_id, state, started, elapsed, items, reasoning, usage,
  next, header, summary}`. It is a read, so it needs no unlock on the phone.
- **The plan:** the `plan` event, `{chat_id, run_id, revision, steps}`. It feeds the plan rail between reads.

An item keeps the fields it had before 1.9.29: `seq, at, kind, label, detail, duration, status`, plus
`headers_ms, first_event_ms` on model items. It gains an optional `view`:

```json
{"seq": 448, "at": 5.6, "kind": "tool", "label": "Reviewing terminal command", "detail": null, "duration": 4.6,
 "status": "done", "view": {"v": 1, "type": "command", "command": "npm run build", "where": "Project folder",
 "network": false, "state": "done", "exit": 0, "seconds": 4.6, "tail": ["dist/app.js 182 kB", "Done in 4.2 s"],
 "action_id": "act_command"}}
```

## Bounds

- A view is at most **4 KB** as JSON. Past that, its lists lose items from the end and `more: true` is set.
- Strings are at most 200 characters unless the type says otherwise:
  - a command line: 400;
  - an output line or a failing test's name: 160;
  - a requirement: 160;
  - a Git summary: 240.
- Lists: a command's tail has 8 lines, failing tests 10, web sites 5, requirements 10 and plan steps 20.
- Fields that are unknown are left out of a view, never sent as null. In `header` and `summary`, an unknown is null.

## The work view's types

`state`, wherever it appears, is one of `waiting` (for the person), `running`, `done`, `failed`, `declined`, `held`
(Pebble's own check held it), `undone` or `stopped`.

| Type | Means | Fields |
|---|---|---|
| `read` | Pebble read a file | `path`, `start` (first line), `total` (lines in the file). A private file (keys, `.env`) has only `private: true`, never its name |
| `search` | a search of the project | `tool` (Grep, Glob, SearchFiles, CodeReferences), `query`, `hits` |
| `web` | a web search or page | `query`, `sites`: host names only (`docs.python.org`), never a full address |
| `edit` | a file change | `path`, `added` and `removed` lines (from the change's diff), `new` (the file did not exist), `private`, `state`, `action_id` |
| `command` | a terminal command | `command` as the person would read it, `where` ("Project folder › scripts"), `network`, `state`, `exit`, `seconds`, `tail` (its last lines), `cut` (the output was longer), `action_id` |
| `tests` | a test run | `recipe` (python-pytest, npm-test …), `state`, `passed`, `failed`, `skipped`, `errors`, `total`, `failing` (names, for display only), `exit`, `seconds`, `action_id` |
| `git` | a Git step or status read | `op` (commit, merge, push, status …), `summary` (Pebble's sentence for the plan), `branch`, `upstream`, `ahead`, `behind`, `changed`, `commit` (short hash), `push_to` (the remote's name), `destructive`, `publication`, `state`, `action_id` |
| `window` | a look or a press in an app window | `app`, `title`, `verb` (Pressed, Typed into, Clicked …), `target`, `before` and `after` (what changed), `verdict` (`happened`, `nothing_changed`, `uncertain`, `refused`), `picture` `{ref, width, height}`, `point` `{x, y}` (where it pressed, in the picture's pixels) |
| `agent` | a child agent | `name`, `state`, `depth`, `parent`, `seconds`, `tokens` |
| `checks` | the completion check | `verdict` ("Verified · pytest 42 passed on current files"), `tests` `{runner, passed, failed, current}`, `files_changed`, `issues`, `repairable`, `requirements` `[{text, state}]`, `unmet`, `repaired` |
| `review` | a card to review, in place | `action_id`, `action` (its kind), `state`, `decided_by` (`you`, `pebble` with `quote`: the person's words Pebble's check matched, or `mode`: the permission mode allowed it) |
| `question` | a question to the person | `questions` (how many), `state` |

Notes:

- **`checks.requirements`** lists AgentGuard's requirement ledger in plain words. Each requirement is `met` when the
  check found nothing unmet. Otherwise it is `open`, and `unmet` counts the issues.
  - Pebble's records keep how many issues were unmet, not which requirement each belongs to. So the card never calls
    one requirement unmet; the reply says which.
  - `repaired` is set when an earlier check found issues and this one found none.
- **`window.picture`** is a reference to Pebble's newest observation of the window, never the pixels. The picture stays
  in memory on the Mac. Version 1 has no route that serves it: draw the press point in a neutral frame of the picture's
  size, as the Mac does.
- **`review` and `question`** items are drawn as today's rows on the Mac. Their real cards are the review cards and
  questions the window already shows.
- **Never in a view:** the person's prompt, checkpoints, held drafts, the model's reasoning text, credentials or file
  contents. Command lines and output are masked for secrets: private keys, `sk-`, `gh*_` and `xox*` tokens, AWS keys,
  bearer tokens, `password=`-style values and a URL's user and password.

## The header and the summary

- **`header`** `{plan, state, elapsed}`:
  - `plan` is `{done, total, steps: [{title, status}]}` from UpdatePlan, or null. Step titles are the model's words
    (untrusted).
  - `state` is the run's state; `elapsed` is in seconds.
  - **The task line is not in the header.** It is the first line of the person's own message, which the client
    already holds; Pebble's activity record never reads the prompt.
- **`summary`** (null until the run settles; also null while it waits for the person) `{files, commands, tests, checks,
  seconds, tokens}`:
  - `files` `{count, added, removed}` counts applied edits;
  - `commands` `{count, failed}`;
  - `tests` `{passed, failed}` comes from the last test run;
  - `checks` is the last check's verdict;
  - `tokens` `{input, cached, output}` is the cost: Pebble records no price.

## Native kinds (UIRender)

Each kind's `data` is described in `$defs/native.<kind>`:

- **form:** 1 to 3 required fields, text or choice.
- **table:** 1 to 12 columns, up to 200 rows of primitives.
- **chart:** bar, line, area, scatter, stacked, pie or donut, 1 to 6 numeric series, 1 to 200 rows, with an
  optional `format` and `unit`. Pie and donut take one series of 2 to 8 parts, none below zero; scatter takes numbers
  on both axes; stacked takes no negative parts.
- **plan:** 1 to 20 steps, with one in progress.
- **evidence:** 1 to 30 items, each citing a receipt.
- **quiz:** 1 to 50 questions with 2 to 6 choices. Pebble keeps the answer and the explanation out of the view it
  shows.
- **diagram:** 1 to 40 nodes and up to 80 edges, with an optional `layout` (flow, layers or sequence), `direction`
  and `caption`, and per node an optional `group`, `sub` and `tone`.
- **compare:** 2 to 4 options against 1 to 10 criteria, one value per option, optionally one marked recommended.
- **metrics:** 2 to 4 tiles, each a value with its unit, change, format and whether up or down is better.
- **timeline:** 2 to 20 events. Dates are ISO values (YYYY, YYYY-MM or YYYY-MM-DD) that the renderer formats.
- **record:** one card of 1 to 12 label and value fields, text and numbers only.
- **dashboard:** 1 to 8 panels: metric, table, chart, plan or evidence. Each has inline data or a binding Pebble
  resolves.
- **prototype:** 1 to 12 screens of up to 30 components, whose controls only switch screens.
- **html** is Mac-only (`x-pebble-surfaces: ["mac"]`). The phone never draws it.

Every string in a native view is plain text, as `artifact_schema.REJECTED_TEXT` enforces: no URL, markup, script,
template or credential. `x-pebble-native-kinds` lists every kind `artifact_schema.py` defines. A kind that version 1
does not yet describe field by field carries `x-pebble-shape: "summary"` and its one-line summary. Draw it as text
until it is described.

Numbers in these views are raw; the renderer rounds and formats them. `fixtures.json` holds one fixture per kind and
`native_variants`, one per chart type and diagram layout. A renderer that does not know a kind shows the
"Update the app" fallback.

## Not in version 1

- **A read that serves a window's picture** (scaled, from memory, for the live session only; it needs the phone's
  unlock).
- **A full-body read of one item** (every hunk of an edit, a page of output).
- **A per-requirement verdict for `checks`:** it needs AgentGuard's check to say which requirement each issue belongs
  to.
- **A branch read that compares two tips** (both histories, the merge base and the line totals). The Mac's branch card
  uses today's `git_status` and `git_history` reads, on request.
