import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { transform } from "sucrase";

// Phase 24 wave-B merge: bridge.openExternal (t24-ux-settings 4.3, V1-112) opens only https pages, through open_url,
// and refuses anything else before any call; the shell's plain refusal (4.2) still turns an activity read off quietly.
// The Tauri modules are stubs; nothing native runs.
const require = createRequire(import.meta.url);
const load = (file, stubs = {}) => {
  const module = { exports: {} };
  const code = transform(readFileSync(new URL(file, import.meta.url), "utf8"), { transforms: ["typescript", "imports"] }).code;
  new Function("require", "module", "exports", code)((name) => stubs[name] ?? require(name), module, module.exports);
  return module.exports;
};
const calls = [];
const bridge = load("./bridge.ts", {
  "@tauri-apps/api/core": { isTauri: () => true, invoke: async (command, args) => { calls.push({ command, args }); return null; } },
  "@tauri-apps/api/window": { getCurrentWindow: () => ({ onDragDropEvent: () => () => {} }) },
  "@tauri-apps/api/event": { listen: async () => () => {} },
  "@tauri-apps/plugin-dialog": { open: async () => null },
});

await bridge.openExternal("https://example.org/report?id=7");
assert.deepEqual(calls, [{ command: "rpc", args: { method: "open_url", params: { url: "https://example.org/report?id=7" } } }]);
calls.length = 0;
for (const url of ["http://example.org", "javascript:alert(1)", "file:///etc/passwd", "https://exa mple.org", "https://", " https://example.org",
  "https://example.org/" + "a".repeat(3000), 42, null]) {
  await assert.rejects(bridge.openExternal(url), /Only https pages can be opened from here\./, String(url).slice(0, 40));
}
assert.equal(calls.length, 0, "nothing is sent for a refused address");

const activity = load("./activity.ts");
for (const message of ["Pebble can't do that from this window.", "Invalid local request.", "Unknown request."])
  assert.ok(activity.isUnsupported(new Error(message)), message);
assert.ok(!activity.isUnsupported(new Error("Stop the current task first.")));
