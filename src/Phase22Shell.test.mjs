import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Phase 22 shell structure (u22-shell, design study D22 §2–§5, §9–§11): the chat column holds only the
// conversation, the status line and the composer; the inspector lives in the Details drawer that replaced
// the ⌘J aside; the status-line companion takes part in the avatar election; slash commands map to
// handlers the window already has; Retry and Edit append through send(); copy uses the raw text; the
// token meter hides without numbers; the bundle loads each surface on demand; stale version strings are
// gone. Sources are compiled and read, never run against a service.
const require = createRequire(import.meta.url);
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };
function compiled(filename) {
  const source = read(filename);
  const { descriptor, errors } = parse(source, { filename });
  assert.deepEqual(errors, [], filename);
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename,
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] } });
  assert.deepEqual(template.errors, [], filename);
  return { source, template: descriptor.template.content, script: descriptor.scriptSetup.content, compiled: script.content, render: template.code };
}
function evaluateTs(code, resolve = (name) => require(name)) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(resolve, module, module.exports);
  return module.exports;
}
const between = (text, start, end) => {
  const from = text.indexOf(start);
  assert.ok(from >= 0, "missing " + start);
  const to = text.indexOf(end, from);
  assert.ok(to > from, "missing " + end + " after " + start);
  return text.slice(from, to + end.length);
};
const app = compiled("./App.vue");

// ---- 1. The chat column holds only the conversation, the status line and the composer (§10.3) ----
const column = between(app.template, '<main class="conversation"', "</main>");
const drawer = between(app.template, "<DetailsDrawer", "</DetailsDrawer>");
ok(app.template.indexOf("<DetailsDrawer") > app.template.indexOf("</main>"), "the drawer is beside the chat column, not in it");
ok(!/class="inspector"|Activity & review/.test(app.template), "the ⌘J aside became the drawer: one surface, not two");
for (const panel of ["GoalPanel", "CompanionWorkflows", "CodingWorkflows", "ScreenSessions", "PebbleBrowser", "ResearchSources", "ExportPanel", "ModelHandoff", "action-card", "activity-log"]) {
  ok(!column.includes("<" + panel) && !column.includes('class="' + panel), panel + " is not in the chat column");
  ok(drawer.includes(panel), panel + " is in the Details drawer");
}
for (const panel of ["AgentPanel", "ContextInspector", "RewindPanel", "ContextPanel", "MemoryImpactView"]) ok(!column.includes("<" + panel), panel + " is not in the chat column");
const runPanels = [...column.matchAll(/<RunPanel\b[^>]*>/g)].map((m) => m[0]);
eq(runPanels.length, 1);
ok(/view="conversation"/.test(runPanels[0]), "the chat column's task panel is the conversation view (questions and a stop note)");
for (const view of ["activity", "task", "context", "changes"]) ok(new RegExp(`<RunPanel view="${view}"`).test(drawer), "the drawer's " + view + " view");
ok(!/job_notifications/.test(column) && /job_notifications/.test(drawer), "schedule notifications moved to the drawer");
ok(drawer.includes('<PebbleBrowser v-if="chat" :chat-id="chat.id" :refresh-key="screenRevision" @changed="screenRevision++" />'), "Pebble's browser is mounted unchanged");
// Merge (i22, deliberate): the design study's five tabs plus Browser, the panel Pebble for Chrome's popup names for
// its pairing code; Pebble's browser moved there from Tools unchanged.
for (const tab of ["empty", "activity", "task", "context", "changes", "tools", "browser"]) ok(drawer.includes("<template #" + tab + ">"), tab + " slot");
ok(/const DRAWER_TABS = \["activity", "task", "context", "changes", "tools", "browser"\];/.test(app.script), "the design study's tabs and Browser");
ok(/<template #browser>\s*<PebbleBrowser v-if="chat"/.test(drawer) && !/<template #tools>[\s\S]*?<PebbleBrowser[\s\S]*?<\/template>\s*<template #browser>/.test(drawer), "Pebble's browser is the Browser tab, not Tools");
ok(/browser: false, \/\/ listed once asked for/.test(app.script) && /case "browser": openDrawer\("browser", false, true\)/.test(app.script), "the Browser tab is listed when asked for; /browser opens it");
ok(/"Pebble’s Browser panel|Browser panel/.test(read("../browser-extension/lib/popup-model.mjs")), "the popup sends the person to the Browser panel");
ok(/const tabHasContent = computed/.test(app.script) && /DRAWER_TABS\.filter\(\(id\) => tabHasContent\.value\[id\]/.test(app.script), "a tab with nothing to show is not listed");
ok(/:more="moreTabs"/.test(drawer) && /@reveal="revealed\.add\(\$event\)"/.test(drawer), "hidden tabs are offered under More");
ok(/Nothing to review yet\./.test(drawer), "an empty drawer says so once");
ok(/<AgentPanel v-if="show\.agents"/.test(read("./components/RunPanel.vue")), "agent_list is asked only from the drawer's task views");
const drawerSource = read("./components/DetailsDrawer.vue");
ok(/<slot :name="current\.id" \/>/.test(drawerSource) && /v-if="current"/.test(drawerSource), "only the selected tab is mounted");
ok(/v-if="drawerOpen && !canvas && chat"/.test(drawer), "nothing in the drawer is mounted while it is closed");
ok(/viewport\.value < 1050/.test(app.script), "below 1,050 px the drawer overlays as a sheet");
ok(/readDrawer\(storage, DRAWER_KEY, DRAWER_TABS\)/.test(app.script) && /writeDrawer\(storage, DRAWER_KEY,/.test(app.script) && /return window\.localStorage;\s*\} catch/.test(app.script), "remembered per window, storage failures tolerated");
ok(/function announceApprovals/.test(app.script) && /\.action-card\.pending \.action-title/.test(app.script) && /INPUT\|TEXTAREA\|SELECT/.test(app.script), "a new approval opens Activity and focuses its card unless the person is typing");
ok(/class="action-title" tabindex="-1"/.test(drawer));

// ---- 2. The companion in the status line, the secondary text, exact time, the disclosure (§2–§3) ----
const live = between(column, '<div class="live-turn">', "</StatusLine>");
ok(/:expandable="!!liveRecord"/.test(live) && /:expanded="liveOpen"/.test(live) && /controls="live-thinking"/.test(live) && /@toggle="liveOpen = !liveOpen"/.test(live));
ok(/<template #companion\s*><Avatar[\s\S]*?:size="24"[\s\S]*?:reduced="state\.settings\.reduced_motion"[\s\S]*?:rank="AVATAR_RANK\.status"/.test(live), "the Avatar replaces the dot, in the window's avatar election");
ok(!/STATUS_RANK/.test(app.source), "no local rank: companionPointer.ts owns it");
const pointer = read("./companionPointer.ts");
ok(/AVATAR_RANK = \{ booting: 0, welcome: 0, status: 0, message: 1, brand: 2 \}/.test(pointer));
ok(/<template v-if="statusDetail" #detail>\{\{ statusDetail \}\}<\/template>/.test(live), "secondary text after the word, never the word");
ok(/:started-at="hostStart \?\? companion\.pose\.value\.startedAt"/.test(live) && /:exact="hostStart !== null"/.test(live), "the host's clock when known");
ok(/coalesce<string \| null>\(\(text\) => \(statusDetail\.value = text\), 1500\)/.test(app.script), "each secondary text dwells 1.5 s");
ok(/<ThinkingDisclosure\s+v-if="liveOpen && liveRecord[^"]*"\s+live\s+open\s+body-id="live-thinking"/.test(column), "the live timeline opens under the status line");
ok(/<ThinkingDisclosure\s+v-if="recordFor\(message\)"/.test(column), "settled runs leave a receipt above the reply");
ok(/<ThinkingDisclosure\s+full\s+open\s+body-id="drawer-thinking"/.test(drawer), "the full timeline is in the drawer's Activity tab");
ok(/fetch: \(runId, after\) => rpc\("run_activity", \{ run_id: runId, after \}\)/.test(app.script), "run_activity is read with the contract's parameters");
ok(/activityStore\.accept\(event\)/.test(app.script), "activity, reasoning and progress events feed the store");

// ---- 3. Slash commands: actions on existing handlers, host commands through send() (§4) ----
const slash = evaluateTs(read("./slashCommands.ts"));
const runSlash = between(app.script, "function runSlash(action: SlashAction) {", "\n}\n");
const existing = { createChat: "function createChat", openSettings: "function openSettings", openDrawer: "function openDrawer", toggleDrawer: "function toggleDrawer", copyLastReply: "function copyLastReply", stop: "function stop" };
for (const [name, declaration] of Object.entries(existing)) ok(app.script.includes(declaration), name + " exists");
const handlerFor = (action) => runSlash.match(new RegExp('case "' + action + '": ([^\\n]*)'))?.[1] || "";
for (const command of slash.LOCAL_COMMANDS) {
  const body = handlerFor(command.action);
  ok(body, "/" + command.name + " has a handler");
  ok(/createChat\(\)|openSettings\(|openDrawer\(|toggleDrawer\(|copyLastReply\(\)|stop\(\)|effortSelect\.value\?\.focus\(\)|input\.value = /.test(body), "/" + command.name + " uses an existing handler: " + body);
  ok(!/rpc\(|invoke\(|approve|decide\(|send\(/.test(body), "/" + command.name + " calls no route, sends nothing and approves nothing");
}
eq((runSlash.match(/case "/g) || []).length, slash.LOCAL_COMMANDS.length, "no handler without a listed command");
ok(/tab === 'models'/.test(read("./components/AdvancedSettings.vue")) && /openSettings\("models"\)/.test(handlerFor("model")));
const choose = between(app.script, "function chooseSlashItem(", "\n}\n");
ok(/input\.value = choice\.text;\s*send\(\);/.test(choose) && !/rpc\(/.test(choose), "service commands go through send(), as typed text");
ok(/completeArgument\(typed, item\)/.test(choose), "an argument option completes the text; nothing is sent");
ok(/hostCommands\.value = validHost\(await rpc\("command_catalogue"\)\)/.test(app.script), "service commands come only from the host catalogue");
ok(/event\.stopPropagation\(\);\s*slashDismissed\.value = input\.value;/.test(app.script), "Escape closes only the menu");
ok(/"No matching command": nothing is sent/.test(app.script), "Enter on no match sends nothing");
ok(/aria-autocomplete="list"/.test(app.template) && /:aria-activedescendant="slashOpen && slashIndex >= 0 && slashItems\[slashIndex\] \? 'slash-menu-' \+ slashIndex : undefined"/.test(app.template));
ok(/recordUse\(storage, item\.name\)/.test(app.script) && /clearRecent\(storage\)/.test(app.script), "recent use stays in this window's storage, cleared with a conversation");
ok(/else if \(id === "details"\) toggleDrawer\(true\);/.test(app.script) && /@click="toggleDrawer\(\)"/.test(app.template), "the shortcut and the header button share one handler");
ok(/else if \(id === "copy_reply"\) copyLastReply\(\);/.test(app.script), "⌘⇧C copies the last reply");

// ---- 4. Copy uses the raw text; Retry and Edit append through send() (§5) ----
const actions = [...column.matchAll(/<MessageActions\b[\s\S]*?\/>/g)].map((m) => m[0]);
eq(actions.length, 2, "one for replies, one for the person's messages");
for (const tag of actions) ok(/:text="message\.content"/.test(tag) && /@copied="notify\('Copied'\)"/.test(tag), "copy gets the stored text itself; the toast says Copied");
ok(actions.some((tag) => /role="user"/.test(tag) && /:edit="message === lastUser && !isWorking"/.test(tag) && /@edit="startEdit\(message\)"/.test(tag)), "edit offered only on the latest message while no task runs");
const retry = between(app.script, "async function retry(", "\n}\n");
ok(/await send\(question\.content\);/.test(retry) && !/rpc\(/.test(retry), "Retry sends the same words again as a new turn");
ok(/attachments\?\.length \? "Attachments aren't sent again/.test(app.script), "Retry says why when the question had attachments");
const edited = between(app.script, "async function sendEdited(", "\n}\n");
ok(/await send\(text\);/.test(edited) && !/rpc\(/.test(edited), "Edit sends a new message");
ok(/source_ids: command \|\| followup \|\| !typed \? \[\]/.test(app.script), "text sent again carries no attachments");
ok(/Undo this turn's file changes…/.test(column) && /openDrawer\('changes', false, true\)/.test(column), "file undo is the reviewed rewind, opened by the person");
ok(!/copy-message/.test(app.template), "the old copy button is gone");

// ---- 5. The token meter hides without numbers; the context ring (§9) ----
const stats = compiled("./components/TokenStats.vue");
const TokenStats = evaluateTs(stats.compiled).default;
TokenStats.render = evaluateTs(stats.render).render;
const meter = (usage) => renderToString(createSSRApp(TokenStats, { usage }));
const nulls = { input_tokens: null, output_tokens: null, cached_tokens: null, tokens_per_second: null };
eq(await meter(nulls), "<!--v-if-->", "no numbers: no meter at all");
eq(await meter({ ...nulls, input_tokens: 0, output_tokens: 0, cached_tokens: 0 }), "<!--v-if-->", "zeros mean nothing was reported");
let html = await meter({ ...nulls, input_tokens: 1240, output_tokens: 312, generation_seconds: 3.1 });
ok(html.includes("1,240 in") && html.includes("312 out") && html.includes("3.1 s") && !html.includes("cached") && !html.includes("tok/s") && !html.includes("—"), "only the reported fields: " + html);
html = await meter({ input_tokens: 10, output_tokens: 40, cached_tokens: 0, tokens_per_second: 42.2, seconds: 2 });
ok(!html.includes("tok/s"), "no rate under 64 output tokens");
html = await meter({ input_tokens: 10, output_tokens: 400, cached_tokens: 8, tokens_per_second: 42.2, seconds: 2 });
ok(html.includes("42 tok/s") && html.includes("8 cached") && html.includes('role="group"') && html.includes('aria-label="Generation statistics"'));
ok((await meter({ ...nulls, route: "memory" })).includes("Memory saved"), "direct routes still say so");
// Merge (i22): totals unknown (a sent attempt reported nothing) → what the provider did report, marked as such.
const report = { input: 900, cached: 512, output: 120, reasoning: 64, tokens_per_second: 40, attempts: 1, generation_seconds: 3 };
html = await meter({ ...nulls, usage_incomplete: true, reported: report });
ok(html.includes("Reported") && html.includes("900 in") && html.includes("512 cached") && html.includes("120 out") && html.includes("3.0 s") && html.includes("40 tok/s") && html.includes("as reported"), "unknown totals show the reported counts, marked: " + html);
html = await meter({ ...nulls, reported: { input: null, cached: null, output: 30, reasoning: null, tokens_per_second: null } });
ok(html.includes("30 out") && !html.includes(" in<") && !html.includes("cached") && !html.includes("tok/s"), "unreported fields stay hidden: " + html);
eq(await meter({ ...nulls, reported: { input: 0, cached: 0, output: 0, reasoning: null, tokens_per_second: null } }), "<!--v-if-->", "reported zeros are still nothing");
html = await meter({ ...nulls, input_tokens: 10, output_tokens: 5, reported: report });
ok(!html.includes("Reported") && html.includes("10 in"), "known totals win over the per-attempt report");
ok(/<TokenStats v-if="message\.usage" :usage="message\.usage"/.test(column), "usage sits in the message's action row");
ok(/v-if="contextUse && contextUse\.percent >= RING_AT"/.test(app.template) && /openDrawer\('context', false, true\)/.test(app.template), "the ring appears from 60 % and opens Context");

// ---- 6. Names, roles and motion ----
ok(/aria-controls="details-drawer"/.test(app.template) && /:aria-expanded="drawerOpen && !canvas"/.test(app.template) && /:aria-label="attention \? `Details, \$\{attention\} waiting` : 'Details'"/.test(app.template), "the header toggle names its state");
ok(/<main class="conversation" aria-label="Conversation">/.test(app.template));
for (const [file, patterns] of Object.entries({
  "./components/DetailsDrawer.vue": [/aria-label="Details"/, /role="tablist"/, /aria-label="Close details"/, /role="separator"/, /aria-haspopup="menu"/, /prefers-reduced-motion/],
  "./components/SlashMenu.vue": [/role="listbox" aria-label="Slash commands"/, /role="option"/, /:aria-disabled=/],
  "./components/MessageActions.vue": [/Copy your message/, /Copy Pebble’s reply/, /aria-label="Edit your message"/, /Retry: ask again as a new message/, /prefers-reduced-motion/],
  "./components/ThinkingDisclosure.vue": [/:aria-expanded="own"/, /:aria-controls="id"/, /Pebble's reasoning, summarised by the model/, /prefers-reduced-motion/],
  "./components/StatusLine.vue": [/aria-label="What Pebble is doing"/, /:aria-expanded="expanded"/, /prefers-reduced-motion/],
})) for (const pattern of patterns) ok(pattern.test(read(file)), file + " " + pattern);
ok(/\.messages \.message:focus-within \.message-actions/.test(read("./style.css")), "message actions appear on keyboard focus");
ok(/select:focus-visible,\s*summary:focus-visible,\s*a:focus-visible,\s*\[tabindex\]:focus-visible/.test(read("./style.css")), "one focus ring for every keyboard stop");

// ---- 7. Routes: only reads the drawer, completion, the ring and the timeline need ----
const methods = new Set([...app.script.matchAll(/\brpc(?:<[^>]*>)?\(\s*"([a-z_]+)"/g)].map((m) => m[1]));
const phase21 = ["state", "get_chat", "new_chat", "set_workspace", "send", "cancel", "attach", "remove_source", "attach_url", "set_model", "model_unload", "model_load", "apps", "open_app", "git_plan_propose", "setting", "delete_chat", "terminal_propose", "model_effort", "model_select"];
const added = [...methods].filter((m) => !phase21.includes(m)).sort();
// Phase 24 (deliberate; the Phase 22 list stays a subset): the window also reads job_list and screen_sessions for the
// Tools tab (V1-124) and memory_forget_preview for the Forget card (V1-034), and four writes, each run only by the
// person's click (checked below): storage_trash_chat with Undo through storage_restore_chat (V1-063, D2),
// memory_delete on the Forget card (V1-034) and chat_rename (V1-125). Still an exact list.
const READS_22 = ["agent_list", "command_catalogue", "context_inspect", "extensions", "goal_get", "goal_list", "run_activity"];
const READS_24 = ["job_list", "memory_forget_preview", "screen_sessions"];
const WRITES_24 = { storage_trash_chat: "async function trashChat(", storage_restore_chat: "async function restoreChat(", memory_delete: "async function forgetMemory(", chat_rename: "async function saveRename(" };
eq(added, [...READS_22, ...READS_24, ...Object.keys(WRITES_24)].sort(), "the only routes the window newly calls: the Phase 22 reads, three reads and four person-initiated writes");
for (const [route, handler] of Object.entries(WRITES_24)) {
  const body = between(app.script, handler, "\n}\n");
  eq([...app.script.matchAll(new RegExp(`\\brpc(?:<[^>]*>)?\\(\\s*"${route}"`, "g"))].length, 1, route + " is called in one place");
  ok(new RegExp(`\\brpc(?:<[^>]*>)?\\(\\s*"${route}"`).test(body), route + " is called only by " + handler);
}
ok(/@click="trashChat"/.test(app.template) && /run: \(\) => void restoreChat\(target\.id, current\)/.test(app.script), "trash runs from the dialog's button; restore only from its Undo");
ok(/@click="forgetMemory\(item\)"/.test(app.template) && /@submit\.prevent="saveRename\(c\)"/.test(app.template), "forgetting and renaming run from the person's own controls");
const shell = read("../src-tauri/src/main.rs");
const allowed = shell.match(/const ALLOWED: &\[&str\] = &\[(.*?)\];/s)?.[1] || "";
// Phase 24 (V1-125, m24c merge): chat_rename is in ALLOWED now (the parent's main.rs patch landed), so the temporary
// exemption that skipped it while the shell still refused it is removed: every new route is checked, as in Phase 22.
for (const route of added.filter((r) => r !== "run_activity")) ok(allowed.includes(`"${route}"`) || shell.includes(`"${route}"`), route + " is already a main-window route");
if (!allowed.includes('"run_activity"'))
  ok(evaluateTs(read("./activity.ts")).isUnsupported(new Error("Invalid local request.")), "until the shell allows run_activity, its refusal turns the read off quietly");
// Merge (i22): the shell allows the timeline's read in the main window only; the pet never reads it.
const petList = shell.match(/window\.label\(\)=="pet" && !\[(.*?)\]\.contains/s)?.[1] || "";
ok(allowed.includes('"run_activity"') && !petList.includes('"run_activity"'), "run_activity is a main-window route");
for (const route of ["browser_extension_status", "browser_extension_unpair", "storage_summary", "storage_purge_chat"])
  ok(allowed.includes(`"${route}"`) && !petList.includes(`"${route}"`), route + " is main-window only");
// Phase 24: the window's new routes are main-window only too (the pet never reaches them).
for (const route of [...READS_24, ...Object.keys(WRITES_24)]) ok(!petList.includes(`"${route}"`), route + " is not on the pet's list");
// Storage notices (s22 P8) re-read state through the existing route; no new route, never a send.
const storageHandler = between(app.script, "function storageEvent(", "\n}\n");
ok(/d\?\.changed === "recently_deleted"\) refresh\(\)\.catch\(fail\)/.test(storageHandler) && !/rpc\(|send\(/.test(storageHandler), "Recently deleted changes re-read state; storage notes show a note");
ok(/event\.event === "storage" \|\| event\.event === "storage_migration"\) return storageEvent/.test(app.script) && /openSettings\('storage'\)/.test(app.template), "the note opens Settings → Data & storage");

// ---- 8. Bundle split (§11): each surface loads on demand; the pet's static graph has no Settings or renderer ----
const main = read("./main.ts");
ok(/import\("\.\/Pet\.vue"\)/.test(main) && /import\("\.\/App\.vue"\)/.test(main) && !/import (App|Pet) from/.test(main), "main.ts picks the surface, then imports only it");
for (const name of ["AdvancedSettings", "ArtifactPanel", "ExportPanel", "CommandPalette"])
  ok(new RegExp(`const ${name} = defineAsyncComponent\\(\\(\\) => import\\("\\./components/${name}\\.vue"\\)\\)`).test(app.script), name + " is an async component");
ok(/const ArtifactPanel = defineAsyncComponent\(\(\) => import\("\.\/ArtifactPanel\.vue"\)\)/.test(read("./components/CompanionSidebar.vue")), "CompanionSidebar loads the canvas lazily");
function staticGraph(start) {
  const seen = new Set(), queue = [start];
  while (queue.length) {
    const file = queue.shift();
    if (seen.has(file)) continue;
    seen.add(file);
    const text = read(file);
    for (const [, spec] of text.matchAll(/^\s*import\s+(?!type\b)[^;"'()]*?from\s+["'](\.[^"']+)["']/gm)) {
      const base = new URL(spec, new URL(file, import.meta.url));
      const target = [base, new URL(base.href + ".ts")].find((u) => existsSync(u));
      if (target) queue.push("./" + target.pathname.split("/src/")[1]);
    }
  }
  return [...seen];
}
const petGraph = staticGraph("./Pet.vue").join(" ");
ok(["CompanionSidebar.vue", "StatusLine.vue", "statusWords.ts", "Avatar.vue"].every((name) => petGraph.includes(name)), "the walk follows static imports: " + petGraph);
for (const name of ["AdvancedSettings", "ArtifactPanel", "NativeArtifactRenderer", "QuizRenderer", "PrototypeRenderer", "DashboardRenderer", "ModsSettings"])
  ok(!petGraph.includes(name), "the pet's initial load has no " + name);
const appGraph = staticGraph("./App.vue").join(" ");
ok(["DetailsDrawer.vue", "RunPanel.vue", "AgentPanel.vue", "activity.ts"].every((name) => appGraph.includes(name)), "the walk follows static imports: " + appGraph);
for (const name of ["AdvancedSettings", "ArtifactPanel", "ExportPanel", "CommandPalette", "QuizRenderer"]) ok(!appGraph.includes(name), "the main window loads " + name + " on first use");

// ---- 9. Stale strings ----
ok(!/0\.4\.2|0\.4 · companion preview/.test(app.source), "no stale version strings");
ok(/<strong>Pebble <span v-if="state\.version">\{\{ state\.version \}\}<\/span><\/strong/.test(app.template) && /version: "", \/\/ the service's own version/.test(app.script), "About shows the service's version");
console.log(`Phase 22 shell fixtures passed: ${checks} checks; inspector only in the Details drawer that replaced the ⌘J aside (browser mounted unchanged in its own tab, empty tabs under More), status-line companion in the avatar election with secondary text and host time, slash actions on existing handlers and host commands through send(), append-only Retry and Edit, raw-text copy, meter hidden without numbers and the context ring, names and reduced motion, new routes (reads, plus Phase 24's person-initiated writes) on the main window only, the split bundle and no stale version strings.`);
