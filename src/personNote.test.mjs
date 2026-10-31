import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { transform } from "sucrase";

// Phase 45 (F070, F087): the note a held or finished run carries is written for the model; the person sees only the
// sentences meant for them, in the Companion's summary and the workspace's run panel alike (one filter, inlined in
// both, since the components' fixtures resolve only their own stubs).
const blocks = ["./components/PetRunSummary.vue", "./components/RunPanel.vue"].map((file) => {
  const vue = readFileSync(new URL(file, import.meta.url), "utf8");
  const start = vue.indexOf("const FOR_THE_MODEL");
  const end = vue.indexOf("\n}\n", vue.indexOf("function personNote", start)) + 3;
  assert.ok(start > 0 && end > start, file);
  assert.doesNotMatch(vue, /\{\{ current\??\.error \}\}/, file);
  return vue.slice(start, end);
});
const module = { exports: {} };
new Function("module", transform(blocks[0] + "\nmodule.exports = personNote;", { transforms: ["typescript"] }).code)(module);
const personNote = module.exports;

test("both surfaces carry the same filter", () => assert.equal(blocks[0], blocks[1]));

test("the audit's blocked YouTube hold keeps only what macOS showed", () => {
  const note = "macOS shows nothing that started playing sound or video during this task. Check the page or app (a tab in " +
    "the background plays nothing until it is shown: ScreenAct focus on its Address row), then look again; otherwise " +
    "tell the person it is not confirmed to play. Never say it plays.";
  assert.equal(personNote(note), "macOS shows nothing that started playing sound or video during this task.");
});

test("guidance and tool names never reach the person; plain words do", () => {
  assert.equal(personNote("Do not claim the tests passed. The tests did not run."), "The tests did not run.");
  assert.equal(personNote("Use BrowserRequest again. The site was not allowed."), "The site was not allowed.");
  assert.equal(personNote(""), "");
  assert.equal(personNote(undefined), "");
  assert.equal(personNote("Stopped by the person."), "Stopped by the person.");
});
