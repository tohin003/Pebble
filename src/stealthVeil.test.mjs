import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { transform } from "sucrase";

// Phase 33 (1.1.0, D57): the Veil's timings, the person's chosen look A from the design preview
// (.runtime/stealth-preview-v1.html). The pure function is checked against the preview's motion spec.
const url = new URL("./stealthVeil.ts", import.meta.url);
const code = transform(readFileSync(url, "utf8"), { transforms: ["typescript", "imports"] }).code;
const module = { exports: {} };
new Function("module", "exports", code)(module, module.exports);
const { veil, moving, shimmer, ON, OFF, ACTIVATE, RELEASE, FADE, GLASS_OPACITY, nextVeilId } = module.exports;
const near = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps;

test("the cast follows the preview's four steps and ends calm, cloaked and glassy", () => {
  assert.equal(ACTIVATE, 1.25);
  let v = veil(true, 0.1);
  assert.ok(v.sy < 1 && v.cloak === 0 && v.eye < 0.5, "gather: squash, eyes closing, no colour yet");
  v = veil(true, 0.4);
  assert.ok(v.cloak > 0 && v.cloak < 1 && v.hem > 0 && v.ping >= 0 && v.eye === 0, "cloak: wipe, hem, ping, eyes closed");
  v = veil(true, 0.9);
  assert.ok(v.cloak === 1 && v.rim > 0.9 && v.glass > 0 && v.hem === 0 && v.ping < 0, "finale: glass arriving");
  v = veil(true, ACTIVATE);
  assert.ok(near(v.cloak, 1) && near(v.rim, 1) && near(v.glass, 1, 1e-3) && near(v.eye, 0.8) && near(v.sy, 1));
  assert.ok(!moving(true, ACTIVATE) && moving(true, 1.2));
  assert.equal(GLASS_OPACITY, 0.7);
});

test("the release folds, lifts and hops back to the plain pebble in 0.65 s", () => {
  assert.equal(RELEASE, 0.65);
  let v = veil(false, 0.24, ON);
  assert.ok(v.glass < 0.2 && v.cloak > 0.5, "fold first: glass goes, colour still there");
  v = veil(false, 0.5, ON);
  assert.ok(near(v.cloak, 0) && v.sy > 1, "lift done, hop");
  v = veil(false, RELEASE, ON);
  assert.deepEqual({ ...v, sx: Math.round(v.sx * 1e6) / 1e6 }, { ...OFF, sx: 1 });
});

test("reversing mid-cast starts from where the pebble is, so nothing jumps", () => {
  const mid = veil(true, 0.4);
  const back = veil(false, 0, mid);
  assert.ok(near(back.cloak, mid.cloak) && near(back.rim, mid.rim) && near(back.glass, mid.glass));
});

test("Reduce Motion is one 0.3 s cross-fade with no squash, wipe, ping or blink", () => {
  assert.equal(FADE, 0.3);
  for (const t of [0, 0.1, 0.2, 0.3, 0.5]) {
    const v = veil(true, t, OFF, true);
    assert.ok(v.fade && v.sy === 1 && v.ping === -1 && v.hem === 0);
  }
  assert.ok(near(veil(true, FADE, OFF, true).cloak, 1) && near(veil(false, FADE, ON, true).cloak, 0));
  assert.ok(!moving(true, FADE, true));
});

test("one 1.1 s shimmer every 6 s while on, and brightness changes stay at 0.25 Hz or slower", () => {
  assert.equal(shimmer(-1), null);
  assert.ok(shimmer(0.55).opacity > 0.99, "the shimmer peaks mid-band");
  assert.equal(shimmer(2), null);
  assert.ok(shimmer(6.5) !== null && shimmer(6.5).x > 0);
  const lit = [];
  for (let t = 0; t < 60; t += 0.05) lit.push(shimmer(t) !== null);
  const starts = lit.filter((on, i) => on && !lit[i - 1]).length;
  assert.ok(starts <= 60 / 4, "at most one brightness event every 4 s");
});

test("every avatar gets its own clip and gradient ids", () => {
  const a = nextVeilId(), b = nextVeilId();
  assert.notEqual(a.body, b.body);
  assert.deepEqual(Object.keys(a).sort(), ["base", "body", "cloak", "glass", "sheen"]);
});
