import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { transform } from "sucrase";

// Phase 24 (V1-103, i11a-core-06): one source for the permission modes' names and copy, and the copy says what the
// host does. Settings, the pet and the composer label must show the same strings; the words are checked against
// permissions.automatic(), terminal.READ_ONLY and the review rules, so a behaviour change fails here until the copy
// changes with it (D3: the copy is corrected now, the behaviour after 1.0).
const require = createRequire(import.meta.url);
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const source = read("./permissionModes.ts");
const module = { exports: {} };
new Function("require", "module", "exports", transform(source, { transforms: ["typescript", "imports"] }).code)(require, module, module.exports);
const P = module.exports;
const STALE = ["Approve in my project", "Ask approval", "Apply project edits and isolated tests automatically", "Supported actions can run on your behalf",
  "Enabled tools run automatically", "dangerous commands remain guarded", "Allow routine actions; review risky changes", "Review actions before they run."];

test("one frozen table of three modes, pure data", () => {
  assert.ok(!/\brpc\b|invoke\(|fetch\(|innerHTML|v-html|localStorage/.test(source), "no route, markup or storage");
  assert.deepEqual([...P.MODE_IDS], ["ask", "auto", "full"]);
  assert.ok(Object.isFrozen(P.MODES) && Object.isFrozen(P.MODE_IDS) && P.MODE_IDS.every((id) => Object.isFrozen(P.MODES[id])));
  assert.deepEqual(P.MODE_IDS.map((id) => P.MODES[id].name), ["Ask for approval", "Auto in project", "Full access"], "one name each; Auto has one name");
  for (const id of P.MODE_IDS) {
    const mode = P.MODES[id];
    assert.deepEqual(Object.keys(mode).sort(), ["always_asks", "id", "name", "runs_without_asking"]);
    assert.equal(mode.id, id);
    for (const key of ["name", "runs_without_asking", "always_asks"]) assert.ok(mode[key].length > 0 && mode[key].length <= 200 && !/[<>]/.test(mode[key]), id + "." + key);
  }
  assert.equal(new Set(P.MODE_IDS.map((id) => P.MODES[id].name)).size, 3);
  assert.equal(P.modeName("auto"), "Auto in project");
  assert.equal(P.modeName("constructor"), "Ask for approval", "an unknown or prototype key is the host default");
  assert.equal(P.modeName(undefined), "Ask for approval");
});

test("the copy matches what the host does", () => {
  const permissions = read("../backend/pebble/permissions.py");
  const terminal = read("../backend/pebble/terminal.py");
  // Auto: exactly edit, tests, command and worktree; Full: every kind (still subject to requires_review).
  const automatic = permissions.slice(permissions.indexOf("def automatic("));
  assert.match(automatic, /policy\["mode"\] == "full"\s*\n\s*or policy\["mode"\] == "auto"\s*\n\s*and kind in \{"edit", "tests", "command", "worktree"\}/,
    "permissions.automatic() changed: update permissionModes.ts with it");
  // Commands that run without review are the READ_ONLY executables, in Auto and Full alike.
  const readOnly = [...terminal.match(/READ_ONLY = \{([^}]*)\}/)[1].matchAll(/"([a-z]+)"/g)].map((m) => m[1]).sort();
  assert.deepEqual(readOnly, ["cat", "echo", "file", "head", "ls", "pwd", "rg", "tail", "uname", "wc", "which"]);
  assert.match(terminal, /"requires_review": network or dangerous or not readonly,/);
  const named = [...P.MODES.auto.runs_without_asking.matchAll(/\b(ls|cat|rg|head|tail|wc|pwd|file|uname|which|echo|grep)\b/g)].map((m) => m[1]);
  assert.ok(named.length >= 3 && named.every((name) => readOnly.includes(name)), "Auto names only read-only commands: " + named);
  assert.ok(!readOnly.includes("grep") && /including grep/.test(P.MODES.auto.always_asks) && /including grep/.test(P.MODES.full.always_asks), "grep asks in Auto and Full, and the copy says so");
  // Full adds app launches and Git changes that neither delete nor publish (git_plans requires review for those).
  assert.match(read("../backend/pebble/git_plans.py"), /'requires_review': destructive or publication/);
  assert.match(P.MODES.full.runs_without_asking, /app launches and Git changes that neither delete work nor publish/);
  // Connector calls, installs and presses in a granted Chrome tab ask in every mode (Phase 30, D42: app windows cannot
  // be granted, and looking, typing and choosing in a granted tab run under the grant).
  assert.match(read("../backend/pebble/extensions.py"), /'run_id': run_id, 'requires_review': True\}/);
  assert.match(read("../backend/pebble/coding_api.py"), /app\.propose\(cid, 'install', \{\*\*payload, 'requires_review': True\}\)/);
  assert.match(read("../backend/pebble/app.py"), /self\.propose\(cid, 'screen', \{\*\*result\['proposal'\], 'requires_review': True\}\)\s*# every mode/);
  for (const words of ["connector calls", "installs", "every press in a chrome tab you granted", "run under your grant"]) assert.ok(P.EVERY_MODE.toLowerCase().includes(words), "every mode: " + words);
  assert.ok(!/app-window|app window/i.test(P.EVERY_MODE + P.MODES.full.always_asks), "no app-window wording");
  for (const id of ["auto", "full"]) assert.ok(/connector calls/.test(P.MODES[id].always_asks) && /installs/.test(P.MODES[id].always_asks), id);
  assert.match(P.MODES.ask.always_asks, /Every edit, test run, terminal command, app launch, Git change and connector call/);
});

test("Settings shows the shared strings and keeps none of its own", () => {
  const settings = read("./components/AdvancedSettings.vue");
  assert.match(settings, /import \{ EVERY_MODE, MODES, MODE_IDS \} from "\.\.\/permissionModes";/);
  assert.match(settings, /\{\{ MODES\[id\]\.name \}\}/);
  assert.match(settings, /Runs without asking: \{\{ MODES\[id\]\.runs_without_asking \}\}/);
  assert.match(settings, /Always asks: \{\{ MODES\[id\]\.always_asks \}\}/);
  for (const id of P.MODE_IDS) {
    for (const key of ["runs_without_asking", "always_asks"]) assert.ok(!settings.includes(P.MODES[id][key]), "no copy of " + id + "." + key);
    // A name may appear in prose ("Full access increases the impact…"), never as its own string or label.
    for (const form of ['"' + P.MODES[id].name + '"', ">" + P.MODES[id].name + "<"]) assert.ok(!settings.includes(form), "no local name " + form);
  }
  for (const stale of STALE) assert.ok(!settings.includes(stale), "stale copy: " + stale);
});

// The pet (src/Pet.vue) and the composer label (src/App.vue) belong to t24-ux-shell, which imports this module in
// wave C. Until that merge these checks report as todo, never as a pass; once a surface imports the module, the check
// is strict.
for (const [file, label, pattern] of [["./Pet.vue", "the pet", /MODES\[[^\]]+\]\.(name|runs_without_asking|always_asks)|modeName\(/], ["./App.vue", "the composer", /modeName\(|MODES\[[^\]]+\]\.name/]]) {
  const text = read(file);
  const wired = /from ["']\.\/permissionModes["']/.test(text);
  test(label + " uses the same strings", { todo: wired ? false : "waits for t24-ux-shell to import src/permissionModes.ts in " + file.slice(2) }, () => {
    assert.ok(wired, file + " imports permissionModes");
    assert.match(text, pattern, file + " shows the shared copy");
    for (const stale of STALE) assert.ok(!text.includes(stale), file + " keeps no stale copy: " + stale);
    for (const id of P.MODE_IDS) assert.ok(!text.includes('"' + P.MODES[id].name + '"'), file + " does not retype " + P.MODES[id].name);
  });
}
