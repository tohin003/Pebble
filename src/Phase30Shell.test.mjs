import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse, compileTemplate } from "@vue/compiler-sfc";

// Phase 30 (1.0.3, D42): the window fixes of the D37 audit. ⌘+ and ⌘− had no permission behind them; Esc ("Close the
// open panel or dialog") left the details panel and the canvas open unless they had focus; and the pet's Views panel
// showed controls whose calls the pet's window may not make, so each failed only when pressed.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("every window with the zoom keys has the zoom permission", () => {
  const windows = JSON.parse(read("../src-tauri/tauri.conf.json")).app.windows;
  const zoomed = windows.filter((w) => w.zoomHotkeysEnabled).map((w) => w.label);
  assert.ok(zoomed.includes("main"), "⌘+ and ⌘− zoom the main window (V1-119)");
  const capability = JSON.parse(read("../src-tauri/capabilities/main.json"));
  for (const label of zoomed) {
    assert.ok(capability.windows.includes(label), label);
    // Tauri's hotkey polyfill calls setZoom, which "Requires `core:webview:allow-set-webview-zoom` permission".
    assert.ok(capability.permissions.includes("core:webview:allow-set-webview-zoom"), label + " can zoom");
  }
});

// The main window's Esc branch, run on its own with stand-in state.
const app = read("./App.vue");
const branch = app.match(/\n  if \(shortcut === "close"\) \{\n[\s\S]*?\n  \} else if/);
assert.ok(branch, "the Esc branch of the main window's key handler");
const body = branch[0].replace(/^\n  if \(shortcut === "close"\) \{/, "").replace(/\} else if$/, "");
function escape(state, prevented = false) {
  const ref = (value) => ({ value });
  const closed = [];
  const scope = {
    palette: ref(!!state.palette), modal: ref(state.modal ?? null), showAttach: ref(!!state.attach),
    canvas: ref(!!state.canvas), drawerOpen: ref(!!state.drawer),
    closeCanvas: () => closed.push("canvas"), closeDrawer: () => closed.push("drawer"),
  };
  new Function(...Object.keys(scope), "e", body)(...Object.values(scope), { defaultPrevented: prevented });
  return { closed, palette: scope.palette.value, modal: scope.modal.value, attach: scope.showAttach.value };
}

test("Esc closes the open panel wherever focus is, one thing at a time", () => {
  assert.deepEqual(escape({ drawer: true }).closed, ["drawer"], "the details panel closes without focus inside it");
  assert.deepEqual(escape({ canvas: true }).closed, ["canvas"], "the canvas closes");
  assert.deepEqual(escape({ palette: true, drawer: true }), { closed: [], palette: false, modal: null, attach: false }, "the palette first");
  assert.deepEqual(escape({ modal: "settings", drawer: true }), { closed: [], palette: false, modal: null, attach: false }, "then a dialog");
  assert.deepEqual(escape({ attach: true, canvas: true }).closed, [], "then the attach menu");
  assert.deepEqual(escape({ drawer: true }, true).closed, [], "an Esc something inside already handled closes nothing more");
  assert.deepEqual(escape({}).closed, []);
});

test("the pet's Views panel offers only what the pet's window may call", () => {
  const panel = read("./components/ArtifactPanel.vue");
  const rust = read("../src-tauri/src/main.rs");
  const pet = new Set(rust.match(/window\.label\(\)=="pet" && !\[([^\]]*)\]\.contains/)[1].match(/"([a-z_]+)"/g).map((s) => s.slice(1, -1)));
  const called = new Set([...panel.matchAll(/\brpc(?:<[^>]*>)?\(\s*["']([a-z_]+)["']/g)].map((m) => m[1]));
  const workspaceOnly = [...called].filter((method) => !pet.has(method)).sort();
  // A new call the pet may not make needs its own guard here before this list grows.
  assert.deepEqual(workspaceOnly, ["artifact_stop", "artifact_submit", "artifact_view_open", "media_artifact", "media_download"]);
  assert.match(panel, /const inPet = computed\(\(\) => panelSurface\.value === "pet"\);/);
  assert.match(panel, /<button v-if="!inPet" class="secondary-button"[^>]*@click="stopArtifact">/, "artifact_stop: no Stop artifact in the pet");
  assert.match(panel, /<NativeArtifactRenderer v-if="nativeArtifact"[^>]*:disabled="!native \|\| inPet"[^>]*:disabled-note="inPet \? 'Answer this form in the workspace\.' : undefined"/,
    "artifact_submit: the form is read-only in the pet and says where to answer it");
  assert.match(panel, /<button v-if="inPet" type="button" class="secondary-button" @click="emit\('workspace'\)">Open in the workspace<\/button>\s*<button\s+v-else\s+class="primary-button"[^>]*@click="runCustom"/,
    "artifact_view_open: the pet opens the workspace instead of a static preview");
  assert.match(panel, /if \(active\.value\?\.kind==='mcp_media' && !inPet\.value\) \{[\s\S]{0,200}rpc\('media_artifact'/, "media_artifact is never called from the pet");
  assert.match(panel, /async function downloadConnectorMedia\(\) \{\n  if \(inPet\.value \|\| /, "media_download is never called from the pet");
  const { descriptor } = parse(panel, { filename: "ArtifactPanel.vue" });
  const { errors } = compileTemplate({ source: descriptor.template.content, filename: "ArtifactPanel.vue", id: "panel",
    compilerOptions: { expressionPlugins: ["typescript"] } });
  assert.deepEqual(errors, []);
  assert.match(read("./components/NativeArtifactRenderer.vue"), /\{\{ disabledNote \|\| "Open the desktop app to submit answers\." \}\}/);
});
