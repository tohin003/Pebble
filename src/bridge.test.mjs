import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { transform } from "sucrase";

// Phase 24 wave-A merge: bridge.ts splits the shell's "<code>\u001f<message>" into an RpcError (t24-core, V1-067 and
// the sign-in classes of V1-001) and sends a pasted file to attach_bytes in chunks the shell accepts (t24-build,
// V1-042). The Tauri modules are stubs; nothing native runs.
const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./bridge.ts", import.meta.url), "utf8");
const calls = [];
let answer = async () => null;
const stubs = {
  "@tauri-apps/api/core": { isTauri: () => true, invoke: (command, args) => { calls.push({ command, args }); return answer(command, args); } },
  "@tauri-apps/api/window": { getCurrentWindow: () => ({ onDragDropEvent: () => () => {} }) },
  "@tauri-apps/api/event": { listen: async () => () => {} },
  "@tauri-apps/plugin-dialog": { open: async () => null },
};
const module = { exports: {} };
new Function("require", "module", "exports", transform(source, { transforms: ["typescript", "imports"] }).code)(
  (name) => stubs[name] ?? require(name), module, module.exports);
const bridge = module.exports;

async function rejection(promise) {
  try { await promise; } catch (cause) { return cause; }
  assert.fail("expected a refusal");
}

// A coded refusal becomes an RpcError whose message (and String()) is the message alone.
answer = async () => { throw "disk_full\u001fThe disk is full. Free some space, then try again."; };
let error = await rejection(bridge.rpc("send", { id: "c", text: "hi" }));
assert.ok(error instanceof bridge.RpcError && error instanceof Error);
assert.equal(error.code, "disk_full");
assert.equal(error.message, "The disk is full. Free some space, then try again.");
assert.equal(String(error), error.message);
assert.equal(error instanceof Error ? error.message : String(error), error.message, "callers' usual formatting shows only the message");

// A message keeps every character after the first separator, including line breaks.
answer = async () => { throw "http_403\u001fRefused.\nYour current sign-in is unchanged."; };
error = await rejection(bridge.rpc("chatgpt_start"));
assert.equal(error.code, "http_403");
assert.equal(error.message, "Refused.\nYour current sign-in is unchanged.");

// Uncoded refusals stay exactly what the shell sent (plain strings, as before Phase 24).
for (const plain of ["Invalid local request.", "Open the workspace to change this.", "Bad\u001fcode", "\u001fno code", 42]) {
  answer = async () => { throw plain; };
  assert.equal(await rejection(bridge.rpc("state")), plain);
}
answer = async () => ({ ok: true });
assert.deepEqual(await bridge.rpc("state"), { ok: true });

// Paste: at most 72,000 bytes (96,000 base64 characters) per call, in order, then the final call's chip.
calls.length = 0;
const bytes = new Uint8Array(150_000).map((_, i) => (i * 31) % 256);
answer = async (command, args) => (args.params.final ? { id: "chip", name: "notes.bin" } : { upload: "u".repeat(32), received: args.params.offset + 1 });
const chip = await bridge.attachBytes("chat", "notes.bin", new Blob([bytes]));
assert.deepEqual(chip, { id: "chip", name: "notes.bin" });
assert.deepEqual(calls.map((c) => [c.command, c.args.method, c.args.params.offset, c.args.params.final, c.args.params.upload]), [
  ["rpc", "attach_bytes", 0, false, undefined],
  ["rpc", "attach_bytes", 72_000, false, "u".repeat(32)],
  ["rpc", "attach_bytes", 144_000, true, "u".repeat(32)],
]);
assert.ok(calls.every((c) => c.args.params.data_base64.length <= 96_000 && JSON.stringify(c.args.params).length < 128_000));
const sent = Buffer.concat(calls.map((c) => Buffer.from(c.args.params.data_base64, "base64")));
assert.deepEqual(new Uint8Array(sent), bytes, "the chunks put back together are the pasted bytes");

// Over 50 MB is refused before anything is sent.
calls.length = 0;
const huge = { size: 50_000_001, arrayBuffer: async () => assert.fail("never read") };
await assert.rejects(bridge.attachBytes("chat", "big.bin", huge), /smaller than 50 MB/);
assert.equal(calls.length, 0);
