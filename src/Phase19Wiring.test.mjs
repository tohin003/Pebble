import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";

// Phase 19 merge (i19): App.vue and Pet.vue compile with the presence wiring (patches P9, P10). The SFC compiler
// checks the template and script syntax only; vue-tsc (run by the parent) checks the types. Nothing is evaluated.
function compiled(filename) {
  const source = readFileSync(new URL(filename, import.meta.url), "utf8");
  const { descriptor, errors } = parse(source, { filename });
  assert.deepEqual(errors, [], filename);
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename,
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] } });
  assert.deepEqual(template.errors, [], filename);
  return { source, template: descriptor.template.content, script: descriptor.scriptSetup.content };
}

const app = compiled("./App.vue");
const pet = compiled("./Pet.vue");

for (const [name, file] of [["App.vue", app], ["Pet.vue", pet]]) {
  // The reducer replaces the time-only moods (F1); words come from StatusLine, never a progress label.
  assert.ok(file.script.includes('import { useCompanion } from "./avatarActivity"') && !file.script.includes("useAvatarActivity"), name);
  assert.ok(/companion\.accept\(/.test(file.script) && /companion\.seed\(state\.value\??\.runs, state\.value\??\.active_run\)/.test(file.script), name);
  assert.ok(/companion\.sending\(true\)/.test(file.script) && /finally \{\s*companion\.sending\(false\);/.test(file.script), name);
  assert.ok(file.template.includes("<StatusLine") && !/\{\{\s*(label|latestProgress)\s*\}\}/.test(file.template), name);
  assert.ok(/:outcome="(outcome|companionOutcome)"/.test(file.template), name + ": Reply finished does not depend on reactions");
  assert.ok(/@feedback="demoFeedback"/.test(file.template), name + ": app demo feedback prefills the composer");
  assert.ok(!/v-html/.test(file.template.replace(/<Markdown[^>]*>/g, "")), name);
}

// F3: the main window never marks a run active before the host admits it.
assert.ok(!/state\.value\.active = c\.id/.test(app.script));
// F12: one animated avatar per window, by rank; message avatars animate only while working.
for (const rank of ["brand", "booting", "welcome", "message"]) assert.ok(app.template.includes(`:rank="AVATAR_RANK.${rank}"`), rank);
assert.ok(app.template.includes(':animate="isWorking && message === chat?.messages.at(-1)"'));
// Companion settings live in the main window only; the pet never writes them (main.rs keeps its gate).
assert.ok(/rpc\("setting", \{ key: "companion", value \}\)/.test(app.script) && !/key: "companion"/.test(pet.script));
for (const key of ["pointer", "reactions", "ui_guidance"]) assert.ok(app.template.includes(`changeCompanion({ ${key}: !companionSettings.${key} })`), key);
assert.ok(/<option value="rotate">[\s\S]*<option value="fixed">[\s\S]*<option value="off">/.test(app.template));

// F9: the pet feed is no live region; StatusLine is the pet's one status region (header or collapsed pill).
assert.ok(/<div ref="feed" class="quick-feed">/.test(pet.template) && !/quick-feed"[^>]*aria-live/.test(pet.template));
assert.equal((pet.template.match(/<StatusLine/g) || []).length, 2);
assert.ok(/<StatusLine\s+v-if="!expanded"\s+class="pet-pill"/.test(pet.template), "the collapsed region stays mounted");
// F4: native gaze only through pet_gaze, never cursor coordinates; the collapsed pill resizes through pet_expand.
assert.ok(/useNativeGaze\(/.test(pet.script) && /gazeAllowed\(\{ pointer: companionSetting\.value\.pointer, reduced: reducedMotion\.value/.test(pet.script));
assert.ok(/invoke\("pet_expand", \{[\s\S]*status: collapsedStatus\.value,[\s\S]*\}\)/.test(pet.script));
assert.ok(!/cursorPosition|cursor_position/.test(pet.source + app.source));
assert.ok(pet.template.includes(':look="nativeGaze?.look.value ?? null"') && pet.template.includes(':gaze="companionSetting.pointer"'));
// Taps, hovers and drags are presentation only: they call the reducer, never an RPC.
assert.ok(/companion\.interact\("tap"\)/.test(pet.script) && /companion\.interact\("drag"\)/.test(pet.script));
assert.ok(pet.template.includes("@pointerenter=\"companion.interact('hover')\"") && pet.template.includes("@pointerleave=\"companion.interact('leave')\""));

console.log("Phase 19 wiring fixtures passed: App.vue and Pet.vue compile; the reducer, StatusLine, F3 sending, F9 single region, F12 ranks, native gaze and the collapsed pill are wired; companion settings stay in the main window.");
