import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, defineComponent, h, nextTick, reactive, ref, effectScope } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Avatar, pointer and motion fixtures (design study §2.2, §2.4–§2.7; F4, F12), following
// NativeArtifactRenderer.test.mjs. The real Avatar.vue and vendored bloub engine run under a
// fake animation-frame clock; only checked-in sources are compiled.
const require = createRequire(import.meta.url);
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

// ---- a fake window: document listeners, animation frames, clock, media query ----
const saved = { document: globalThis.document, performance: globalThis.performance, raf: globalThis.requestAnimationFrame, caf: globalThis.cancelAnimationFrame, matchMedia: globalThis.matchMedia };
const listeners = new Map();
let added = [];
globalThis.document = {
  hidden: false,
  addEventListener(type, fn) { added.push(type); (listeners.get(type) || listeners.set(type, new Set()).get(type)).add(fn); },
  removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
};
const dispatch = (type, event = {}) => { for (const fn of [...(listeners.get(type) || [])]) fn(event); };
let clock = 1000;
Object.defineProperty(globalThis, "performance", { value: { now: () => clock }, configurable: true, writable: true });
let rafQueue = new Map(), rafSeq = 0, rafRequests = 0, rafRuns = 0;
globalThis.requestAnimationFrame = (cb) => { rafRequests++; const id = ++rafSeq; rafQueue.set(id, cb); return id; };
globalThis.cancelAnimationFrame = (id) => { rafQueue.delete(id); };
// Timers run on the same fake clock, before each frame's animation callbacks (as in a browser).
const realTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
const timers = new Map();
let timerSeq = 0;
globalThis.setTimeout = (fn, ms = 0) => { const id = ++timerSeq; timers.set(id, { at: clock + Math.max(0, ms), fn }); return id; };
globalThis.clearTimeout = (id) => { timers.delete(id); };
const pending = () => rafQueue.size + timers.size;
let systemReduce = false;
const media = new Set();
globalThis.matchMedia = () => ({ get matches() { return systemReduce; }, addEventListener: (_t, fn) => media.add(fn), removeEventListener: (_t, fn) => media.delete(fn) });
async function settle() { for (let i = 0; i < 6; i++) { await Promise.resolve(); await nextTick(); } }
async function frames(n, ms = 1000 / 60) {
  for (let i = 0; i < n; i++) {
    clock += ms;
    for (const [id, t] of [...timers.entries()].sort((a, b) => a[1].at - b[1].at))
      if (t.at <= clock && timers.has(id)) { timers.delete(id); t.fn(); }
    const queue = rafQueue;
    rafQueue = new Map();
    for (const cb of queue.values()) { rafRuns++; cb(clock); }
    await Promise.resolve();
  }
  await settle();
}

// ---- compile the real sources ----
const cache = new Map();
function loadTs(url) {
  if (cache.has(url.href)) return cache.get(url.href).exports;
  const module = { exports: {} };
  cache.set(url.href, module);
  new Function("require", "module", "exports", transform(readFileSync(url, "utf8"), { transforms: ["typescript", "imports"] }).code)(
    (name) => (name.startsWith(".") ? loadTs(new URL(name.endsWith(".ts") ? name : name + ".ts", url)) : require(name)), module, module.exports);
  return module.exports;
}
const avatarUrl = new URL("./Avatar.vue", import.meta.url);
const avatarSource = readFileSync(avatarUrl, "utf8");
const descriptor = parse(avatarSource, { filename: "Avatar.vue" }).descriptor;
const compiled = compileScript(descriptor, { id: "Avatar.vue", inlineTemplate: true });
const avatarModule = { exports: {} };
new Function("require", "module", "exports", transform(compiled.content, { transforms: ["typescript", "imports"] }).code)(
  (name) => (name.startsWith(".") ? loadTs(new URL(name + ".ts", avatarUrl)) : require(name)), avatarModule, avatarModule.exports);
const Avatar = avatarModule.exports.default;
const engineModule = loadTs(new URL("../vendor/bloub/engine.ts", import.meta.url));
const { liveliness, eyePoses } = loadTs(new URL("../vendor/bloub/face.ts", import.meta.url));
const P = loadTs(new URL("../companionPointer.ts", import.meta.url));
const { avatarMotion, CUE_HZ } = loadTs(new URL("../avatarMotion.ts", import.meta.url));
const { POSES } = loadTs(new URL("../companionState.ts", import.meta.url));

// Spy on the engine: samples (and the time each was taken at), states and looks.
const proto = engineModule.BotEngine.prototype;
const original = { sample: proto.sample, setLook: proto.setLook, setState: proto.setState };
let samples = [], looks = [], states = [];
proto.sample = function (t) { samples.push({ engine: this, t }); return original.sample.call(this, t); };
proto.setLook = function (look, t, morph) { looks.push({ engine: this, look }); return original.setLook.call(this, look, t, morph); };
proto.setState = function (id, t) { states.push({ engine: this, id }); return original.setState.call(this, id, t); };

// ---- static contract ----
ok(!/@pointermove|@pointerleave|v-html|innerHTML|\beval\s*\(|new Function/.test(avatarSource), "the SVG has no pointer handlers or markup sinks (one shared listener instead)");
ok(/aria-hidden="true"/.test(avatarSource) && /focusable="false"/.test(avatarSource));
ok(!/from "\.\.\/(bridge|types)"/.test(avatarSource), "the avatar makes no request");

// ---- a renderer whose SVG nodes have boxes ----
function node(type, text = "") {
  const n = { type, text, children: [], props: {}, parent: null, rect: { left: 100, top: 100, width: 80, height: 80 } };
  n.getBoundingClientRect = () => n.rect;
  return n;
}
const renderer = createRenderer({
  createElement: (type) => node(type), createText: (text) => node("#text", text), createComment: (text) => node("#comment", text),
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child);
  },
  remove(child) { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null; },
  setText(child, text) { child.text = text; },
  setElementText(child, text) { child.text = text; child.children = []; },
  patchProp(child, key, _previous, value) { child.props[key] = value; },
  parentNode: (child) => child.parent,
  nextSibling: (child) => child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
});
const all = (el) => [el, ...el.children.flatMap(all)];
const vueErrors = [];
function mount(list) {
  const root = node("root");
  const app = renderer.createApp({ setup: () => () => h("div", list.map((p) => h(Avatar, { key: p.key, ...p }))) });
  app.config.errorHandler = (error) => vueErrors.push(error);
  app.mount(root);
  const svgs = () => all(root).filter((el) => el.type === "svg");
  const by = (key) => svgs()[list.findIndex((p) => p.key === key)];
  return { root, app, svgs, by };
}
const animated = (svgs) => svgs.filter((s) => s.props["data-animated"] === "true");

// ---- one animated avatar: 30 fps cap, live poses keep animating ----
{
  const props = reactive({ key: "a", state: "reading" });
  const view = mount([props]);
  await settle();
  const svg = view.svgs()[0];
  eq([svg.props["data-pose"], svg.props["data-emotion"], svg.props["data-animated"], svg.props["aria-hidden"]], ["reading", "reading", "true", "true"]);
  samples = [];
  let runs = rafRuns;
  await frames(600);
  ok(samples.length >= 295 && samples.length <= 305, "30 fps over 10 s at 60 Hz: " + samples.length);
  ok(rafRuns - runs <= 305, "skipped display frames request no animation frame: " + (rafRuns - runs));
  eq(pending(), 1, "a live pose keeps exactly one wake-up pending");
  props.fps = 15;
  await settle();
  samples = [];
  runs = rafRuns;
  await frames(600);
  ok(samples.length >= 145 && samples.length <= 155, "15 fps for the pet's low-power flag: " + samples.length);
  ok(rafRuns - runs <= 155, "15 fps wakes 15 times a second, not 60: " + (rafRuns - runs));
  props.fps = 30;
  // A calm pose holds a static frame after 8 s and then runs no animation frame.
  props.state = "idle";
  await settle();
  samples = [];
  await frames(60 * 8 + 30);
  ok(samples.length > 200, "the calm pose animates until its hold");
  const requests = rafRequests;
  samples = [];
  await frames(600);
  eq([samples.length, pending(), rafRequests - requests], [0, 0, 0], "static after 8 s: zero samples, wake-ups and animation frames");
  // A pose change or a gaze change wakes it.
  props.state = "thinking";
  await settle();
  await frames(30);
  ok(samples.length >= 14 && pending() === 1, "a new pose wakes the animation");
  eq(states.at(-1).id, "idle");
  props.state = "review";
  await settle();
  eq(states.at(-1).id, "alert", "review uses the bloub alert state");
  await frames(200);
  eq(all(view.root).filter((el) => el.type === "path" && el.props.fill === "var(--avatar-eye)").length, 0, "the alert glyph replaces the face");
  props.state = "question";
  await settle();
  eq(states.at(-1).id, "notify");
  await frames(60);
  ok(all(view.root).some((el) => el.type === "circle" && el.props.fill === "var(--accent)"), "the notify dot is drawn");
  // Hidden: the loop stops; visible again: it resumes.
  props.state = "thinking";
  await settle();
  globalThis.document.hidden = true;
  dispatch("visibilitychange");
  await frames(5);
  eq(pending(), 0, "hidden: no animation frame or timer");
  samples = [];
  await frames(120);
  eq(samples.length, 0);
  globalThis.document.hidden = false;
  dispatch("visibilitychange");
  await frames(10);
  ok(pending() === 1 && samples.length > 0, "visible: resumes");
  view.app.unmount();
  await frames(5);
  eq(pending(), 0, "unmounted: no animation frame or timer");
  eq(P.animationClaims(), 0, "unmounted: the slot is released");
}

// ---- reduced motion: no animation frame at all, settled frame with open eyes ----
{
  for (const variant of [{ reduced: true }, { animate: false }, { system: true }]) {
    systemReduce = !!variant.system;
    const requests = rafRequests;
    samples = [];
    const view = mount([{ key: "r", state: "thinking", reduced: !!variant.reduced, animate: variant.animate !== false }]);
    await settle();
    await frames(300);
    eq(rafRequests - requests, 0, JSON.stringify(variant) + ": zero animation frames");
    eq(view.svgs()[0].props["data-animated"], "false");
    ok(samples.length >= 1 && samples.every((s) => liveliness(s.t).lid >= 0.99), "static frames never freeze mid-blink");
    eq(P.pointerListeners().followers, 0, "a still avatar does not follow the pointer");
    const transform = all(view.root).find((el) => el.type === "g" && el.props.transform?.startsWith("translate"))?.props.transform;
    eq(transform, "translate(0.00 0.00) rotate(0.00) scale(1.000 1.000)", "no movement under reduced motion");
    view.app.unmount();
    systemReduce = false;
  }
  // The system preference changing at run time stops the loop.
  const view = mount([{ key: "s", state: "thinking" }]);
  await settle();
  await frames(10);
  eq(pending(), 1);
  systemReduce = true;
  for (const fn of media) fn();
  await frames(10);
  eq([pending(), view.svgs()[0].props["data-animated"]], [0, "false"], "prefers-reduced-motion stops animation and gaze");
  systemReduce = false;
  for (const fn of media) fn();
  await frames(10);
  eq(pending(), 1);
  view.app.unmount();
  await frames(2);
}

// ---- F12: one animated avatar per window; one document listener; rAF-throttled gaze ----
{
  added = [];
  const list = reactive([
    { key: "brand", state: "thinking", rank: P.AVATAR_RANK.brand, size: 30 },
    { key: "welcome", state: "thinking", rank: P.AVATAR_RANK.welcome, size: 130 },
    { key: "message", state: "thinking", rank: P.AVATAR_RANK.message, size: 29 },
  ]);
  const root = node("root");
  const app = renderer.createApp({ setup: () => () => h("div", list.map((p) => h(Avatar, { key: p.key, ...p }))) });
  app.config.errorHandler = (error) => vueErrors.push(error);
  app.mount(root);
  await settle();
  const svgs = () => all(root).filter((el) => el.type === "svg");
  const pose = (key) => svgs().find((s) => s.props.width === list.find((p) => p.key === key)?.size);
  eq(animated(svgs()).map((s) => s.props.width), [130], "the welcome avatar animates; the others are static");
  await frames(60);
  eq(pending(), 1, "one animation loop for the whole window");
  eq(added.filter((t) => t === "pointermove").length, 1, "one document pointermove listener");
  eq(listeners.get("pointermove")?.size, 1);
  eq(P.pointerListeners(), { listening: true, followers: 1 });
  // Fifty pointer events in one frame update the gaze once.
  const welcome = pose("welcome");
  welcome.rect = { left: 0, top: 0, width: 130, height: 130 };
  looks = [];
  for (let i = 0; i < 50; i++) dispatch("pointermove", { clientX: 265 + i, clientY: 65 });
  await frames(1);
  const welcomeLooks = looks.filter((l) => l.look);
  eq(welcomeLooks.length, 1, "rAF-throttled: one gaze update for fifty events");
  ok(welcomeLooks[0].look.yaw > 0 && welcomeLooks[0].look.pitch === 0, "the eyes turn toward the pointer on the right");
  eq(new Set(looks.map((l) => l.engine)).size, 1, "only the animated avatar follows");
  // Leaving the window rests the eyes.
  looks = [];
  dispatch("pointerout", { relatedTarget: null });
  await frames(1);
  eq(looks.map((l) => l.look), [null]);
  dispatch("pointerout", { relatedTarget: {} });
  await frames(1);
  eq(looks.length, 1, "moving between elements is not leaving the window");
  // The welcome avatar goes away: the last message takes the slot, then the brand.
  list.splice(1, 1);
  await settle();
  eq(animated(svgs()).map((s) => s.props.width), [29]);
  list.splice(1, 1);
  await settle();
  eq(animated(svgs()).map((s) => s.props.width), [30]);
  // Equal ranks: the most recently mounted wins.
  list.push({ key: "late", state: "thinking", rank: P.AVATAR_RANK.brand, size: 44 });
  await settle();
  eq(animated(svgs()).map((s) => s.props.width), [44]);
  // gaze=false (companion.pointer off) removes the follower and then the listener.
  list[1].gaze = false;
  await settle();
  eq(P.pointerListeners(), { listening: false, followers: 0 });
  eq(listeners.get("pointermove")?.size || 0, 0, "no listener once nothing follows");
  list[1].gaze = true;
  await settle();
  eq(P.pointerListeners().followers, 1);
  app.unmount();
  await frames(2);
  eq([P.pointerListeners().listening, P.animationClaims(), pending()], [false, 0, 0], "everything released");
}

// ---- Phase 22: the status line's companion takes part in the same election ----
{
  const list = reactive([
    { key: "brand", state: "thinking", rank: P.AVATAR_RANK.brand, size: 30 },
    { key: "message", state: "thinking", rank: P.AVATAR_RANK.message, size: 29 },
  ]);
  const root = node("root");
  const app = renderer.createApp({ setup: () => () => h("div", list.map((p) => h(Avatar, { key: p.key, ...p }))) });
  app.config.errorHandler = (error) => vueErrors.push(error);
  app.mount(root);
  await settle();
  const svgs = () => all(root).filter((el) => el.type === "svg");
  eq(animated(svgs()).map((s) => s.props.width), [29], "before work: the last message animates");
  ok(P.AVATAR_RANK.status < P.AVATAR_RANK.message, "the status rank outranks message avatars");
  list.push({ key: "status", state: "searching", rank: P.AVATAR_RANK.status, size: 24 });
  await settle();
  await frames(30);
  eq(animated(svgs()).map((s) => s.props.width), [24], "while Pebble works, only the status-line companion animates");
  eq(pending(), 1, "still one animation loop for the window, never a second one");
  list.pop();
  await settle();
  eq(animated(svgs()).map((s) => s.props.width), [29], "when the status line goes, the message avatar takes the slot back");
  list.push({ key: "status", state: "thinking", rank: P.AVATAR_RANK.status, size: 24, reduced: true });
  await settle();
  await frames(10);
  eq(animated(svgs()).map((s) => s.props.width), [29], "under reduced motion the status companion is a still frame and claims nothing");
  app.unmount();
  await frames(2);
  eq([P.animationClaims(), pending()], [0, 0]);
}

// ---- external gaze (the pet's native sampler) and pointer precedence ----
{
  const props = reactive({ key: "pet", state: "thinking", look: null });
  const view = mount([props]);
  await settle();
  looks = [];
  props.look = { x: 0.5, y: -0.25 };
  await frames(1);
  eq(looks.at(-1).look, { yaw: 20, pitch: 7.5, mix: 0.8, spin: 0, wander: 0.1 }, "the native direction steers the eyes: above and right");
  view.svgs()[0].rect = { left: 0, top: 0, width: 100, height: 100 };
  dispatch("pointermove", { clientX: 50, clientY: 250 });
  await frames(1);
  ok(looks.at(-1).look.pitch < 0 && looks.at(-1).look.yaw === 0, "a pointer inside the window, below the avatar, wins over the native gaze");
  dispatch("pointerout", { relatedTarget: null });
  await frames(1);
  eq(looks.at(-1).look.yaw, 20, "outside the window the native gaze resumes");
  props.gaze = false;
  await frames(1);
  eq(looks.at(-1).look, null, "the setting off rests the eyes");
  view.app.unmount();
  await frames(2);
}

// ---- pure pointer helpers ----
eq(P.quantize(0.3), 0.25);
eq(P.quantize(-0.06), 0, "no -0");
eq(Object.is(P.quantize(-0.01), -0), false);
eq(P.quantize(7), 1);
eq(P.quantize(Number.NaN), 0);
eq(P.gazeToward(300, 100, { left: 0, top: 0, width: 200, height: 200 }), { x: 1, y: 0 });
eq(P.gazeToward(100, 100, { left: 0, top: 0, width: 200, height: 200 }), { x: 0, y: 0 });
eq(P.gazeToward(100, 100, { left: 0, top: 0, width: 0, height: 0 }), null, "an empty box (display:none) gives no gaze");
eq(P.gazeToward(Number.NaN, 1, { left: 0, top: 0, width: 10, height: 10 }), null);
eq(P.gazeToward(1, 1, null), null);
for (const payload of [null, undefined, 3, "0.5,0.5", [0.5, 0.5], {}, { x: 0.5 }, { x: 0.5, y: 0.5, screen_x: 800 },
  { x: 812, y: 400 }, { x: Number.NaN, y: 0 }, { x: "0.5", y: 0 }, { x: Infinity, y: 0 }, { x: 1.01, y: 0 }])
  eq(P.parseNativeGaze(payload), null, "refused native payload " + JSON.stringify(payload));
eq(P.parseNativeGaze({ x: 0.5, y: -1 }), { x: 0.5, y: -1 });
eq(P.parseNativeGaze({ x: 0.33, y: 0 }), { x: 0.375, y: 0 }, "quantized again in the webview");
eq(P.lookFor(null), null);
// Phase 26 (D24): the eyes look toward the pointer on both axes. Gaze y is screen-down; the eyes, drawn by the engine's
// own face geometry, sit lower for a pointer below the avatar and higher for one above it.
{
  const eyesY = (gaze) => { const look = P.lookFor(gaze); return eyePoses({ yaw: look.yaw, pitch: look.pitch, roll: 0 }, 1)[0].y; };
  const eyesX = (gaze) => { const look = P.lookFor(gaze); return eyePoses({ yaw: look.yaw, pitch: look.pitch, roll: 0 }, 1)[0].x; };
  ok(P.lookFor({ x: 0, y: 1 }).pitch < 0 && P.lookFor({ x: 0, y: -1 }).pitch > 0, "pitch is positive up, gaze y positive down");
  ok(eyesY({ x: 0, y: 1 }) > eyesY({ x: 0, y: 0 }) && eyesY({ x: 0, y: 0 }) > eyesY({ x: 0, y: -1 }), "a pointer below draws the eyes lower; above, higher");
  ok(eyesX({ x: 1, y: 0 }) > eyesX({ x: 0, y: 0 }) && eyesX({ x: 0, y: 0 }) > eyesX({ x: -1, y: 0 }), "a pointer to the right draws the eyes to the right");
  eq(P.lookFor(P.gazeToward(50, 250, { left: 0, top: 0, width: 100, height: 100 })).pitch < 0, true, "an in-window pointer below looks down");
}
eq([P.gazeAllowed({ pointer: true }), P.gazeAllowed({ pointer: false }), P.gazeAllowed({ reduced: true }), P.gazeAllowed({ systemReduced: true })], [true, false, false, false]);

// ---- useNativeGaze: pet_gaze(enabled) lifecycle with a fake shell ----
{
  const invokes = [];
  let handler = null, unlistened = 0;
  const bridge = {
    async listen(event, fn) { eq(event, "pet-gaze"); handler = fn; return () => unlistened++; },
    async invoke(command, args) { invokes.push([command, args]); },
  };
  const allowed = ref(true);
  const scope = effectScope();
  const gaze = scope.run(() => P.useNativeGaze(bridge, () => allowed.value));
  await settle();
  eq(invokes, [["pet_gaze", { enabled: true }]], "armed once while allowed");
  await gaze.rearm();
  eq(invokes.length, 1, "re-arming while armed is free");
  handler({ payload: { x: 0.25, y: -0.5 } });
  eq(gaze.look.value, { x: 0.25, y: -0.5 });
  handler({ payload: { x: 812, y: 400 } });
  eq(gaze.look.value, null, "screen coordinates are refused");
  handler({ payload: null });
  dispatch("pointerout", { relatedTarget: null });
  await settle();
  eq(invokes.at(-1), ["pet_gaze", { enabled: true }], "after the shell stops, leaving the window re-arms it");
  eq(invokes.length, 2);
  allowed.value = false;
  await settle();
  eq(invokes.at(-1), ["pet_gaze", { enabled: false }], "setting off or reduced motion disarms");
  handler({ payload: { x: 0.5, y: 0.5 } });
  eq(gaze.look.value, null, "nothing is shown while not allowed");
  allowed.value = true;
  await settle();
  globalThis.document.hidden = true;
  dispatch("visibilitychange");
  await settle();
  eq(invokes.at(-1), ["pet_gaze", { enabled: false }], "a hidden pet stops sampling");
  globalThis.document.hidden = false;
  dispatch("visibilitychange");
  await settle();
  eq(invokes.at(-1), ["pet_gaze", { enabled: true }]);
  scope.stop();
  await settle();
  eq([invokes.at(-1), unlistened], [["pet_gaze", { enabled: false }], 1], "disposal stops sampling and the listener");
  ok(!listeners.get("pointerout")?.size && !listeners.get("visibilitychange")?.size, "no listeners left");
}

// ---- motion: no flashing, cue and squash pulses at or under 0.5 Hz, reduced is still ----
const extrema = (series) => series.slice(1, -1).filter((v, i) => (v - series[i]) * (series[i + 2] - v) < 0 && Math.abs(v - series[i]) > 1e-9).length;
for (const id of Object.keys(POSES)) {
  const series = Array.from({ length: 1201 }, (_, i) => avatarMotion(id, i / 60));
  ok(series.every((m) => !m.transform.includes("NaN")), id);
  for (const k of [0, 1, 2]) {
    const pulse = series.map((m) => m.pulse[k]);
    ok(extrema(pulse) <= 2 * CUE_HZ * 20 + 1, `${id} cue ${k} pulses at <= 0.5 Hz`);
  }
  ok(extrema(series.map((m) => m.sx)) <= 21 && extrema(series.map((m) => m.sy)) <= 21, `${id} squash at <= 0.5 Hz`);
  eq(avatarMotion(id, 0.2, true), avatarMotion(id, 8, true), id + " reduced is time-invariant");
  eq(avatarMotion(id, 0).cue, POSES[id].cue);
}
ok(CUE_HZ <= 0.5);
for (const id of ["thinking", "reading", "editing", "testing", "loading", "happy", "writing"])
  ok(avatarMotion(id, 0.4).transform !== avatarMotion(id, 1.3).transform, id + " moves");
eq(avatarMotion("idle", 0).cue, "");
eq(avatarMotion("loading", 0).cue, "orbit");
eq(avatarMotion("editing", 0).cue, "spark");
eq(avatarMotion("happy", 0).cue, "celebrate");
eq(avatarMotion("success", 0).cue, "celebrate", "legacy names still map");
eq(avatarMotion("__proto__", 0).cue, "");

// ---- Phase 33 (1.1.0, D57): the Veil, Stealth Lockdown's look (stealthVeil.ts timings) ----
{
  const veilOf = (svg) => all(svg).find((el) => el.props.class === "avatar-veil");
  const rimOf = (svg) => all(svg).find((el) => el.type === "path" && el.props.stroke === "var(--avatar-stealth-rim)");
  const pingOf = (svg) => all(svg).find((el) => el.type === "circle" && el.props.stroke === "var(--avatar-stealth-glow)");
  const props = reactive({ key: "v", state: "idle", stealth: false });
  const quiet = reactive({ key: "q", state: "idle", stealth: false, rank: 9 }); // a lower rank wins the animation slot
  const view = mount([props, quiet]);
  await settle();
  const [svg, other] = view.svgs();
  eq([svg.props["data-stealth"], svg.props["data-animated"], other.props["data-animated"]], ["off", "true", "false"]);
  ok(!veilOf(svg) && !rimOf(svg), "off: no Veil layers at all");
  const cloak = (el) => all(el).find((n) => n.type === "rect" && n.parent?.type === "clipPath" && String(n.parent.props.id).startsWith("veil-cloak"));
  // Phase 34: how far down the dusk colour reaches (32 = none, 188 = all); no Veil drawn yet is the plain pebble.
  const cloakHeight = (el) => cloak(el)?.props.height ?? 32;
  props.stealth = true;
  quiet.stealth = true;
  await settle();
  await frames(6); // 0.1 s: the squash and blink, before the colour moves
  ok(cloakHeight(svg) < 60, `Phase 34: the cast starts from the plain pebble (1.1.0 drew it fully cloaked at once): ${cloakHeight(svg)}`);
  await frames(18); // 0.4 s: the cloak is under way
  const mid = veilOf(svg);
  ok(mid && mid.props["clip-path"] && pingOf(svg), "the cast wipes the dusk colour down and sends one ping");
  const sweeping = cloakHeight(svg);
  ok(sweeping > 60 && sweeping < 180, `Phase 34: mid-cast the dusk colour covers part of the pebble: ${sweeping}`);
  ok(veilOf(other) && !pingOf(other) && rimOf(other).props.opacity > 0.9 && cloak(other).props.height >= 188,
    "an avatar that does not animate shows the end state at once: fully cloaked, rim on, no cast");
  await frames(60); // past 1.25 s
  eq(svg.props["data-stealth"], "on");
  ok(!pingOf(svg) && rimOf(svg).props.opacity > 0.9, "the cast ends calm, with the light rim");
  const glass = all(veilOf(svg)).find((el) => el.type === "path" && String(el.props.fill).startsWith("url(#veil-glass"));
  ok(glass && Math.abs(glass.props["fill-opacity"] - 0.7) < 0.01, "the glass body at 70% opacity");
  await frames(9 * 60); // past the 8 s hold: the still frame still carries the state
  ok(veilOf(svg) && rimOf(svg).props.opacity > 0.9, "a held frame keeps the tint and the rim");
  props.stealth = false;
  await settle();
  await frames(18); // 0.3 s: the colour lifts
  const lifting = cloakHeight(svg);
  ok(lifting > 60 && lifting < 180, `Phase 34: mid-release the colour has partly lifted (1.1.0 dropped it at once): ${lifting}`);
  await frames(27); // past 0.65 s
  ok(!veilOf(svg) && !rimOf(svg), "the release leaves the plain pebble");
  view.app.unmount();
}
{
  // Reduce Motion: one cross-fade (opacity on the layers, a CSS transition), no wipe, ping or squash.
  const props = reactive({ key: "r", state: "idle", stealth: false, reduced: true });
  const view = mount([props]);
  await settle();
  props.stealth = true;
  await settle();
  await frames(3);
  const svg = view.svgs()[0];
  const veil = all(svg).find((el) => el.props.class === "avatar-veil");
  ok(veil && veil.props.opacity === 1 && !veil.props["clip-path"], "reduced: the layers fade, never wipe");
  ok(!all(svg).some((el) => el.type === "circle" && el.props.stroke === "var(--avatar-stealth-glow)"), "reduced: no ping");
  ok(/transition: opacity 0\.3s/.test(avatarSource), "the fade is 0.3 s");
  view.app.unmount();
}
{
  // Under 64 px: tint and a thicker rim carry the state; no glass, highlight or shimmer.
  const props = reactive({ key: "s", state: "idle", stealth: true, size: 24 });
  const view = mount([props]);
  await settle();
  const svg = view.svgs()[0];
  const rim = all(svg).find((el) => el.type === "path" && el.props.stroke === "var(--avatar-stealth-rim)");
  ok(Math.abs(rim.props["stroke-width"] - (2 * 1.3 * 220) / 24) < 1e-6, "the rim stays about 1.3 px wide on screen");
  ok(!all(svg).some((el) => el.type === "ellipse") && !all(svg).some((el) => String(el.props.fill).startsWith("url(#veil-glass")),
    "no glass or highlight at 24 px");
  view.app.unmount();
}
eq(vueErrors, [], "no Vue errors in the Veil fixtures");

// ---- SSR: a static, aria-hidden frame with no claim and no listener ----
const html = await renderToString(createSSRApp(defineComponent({ render: () => h(Avatar, { state: "happy", size: 40 }) })));
ok(html.includes('aria-hidden="true"') && html.includes('data-pose="happy"') && html.includes('data-animated="false"'));
eq([P.animationClaims(), P.pointerListeners().followers], [0, 0], "SSR claims nothing");
eq(vueErrors, []);

// ---- E1: the reproducible frame fixture, with its pass bars ----
const { measure } = await import(new URL("../../scripts/measure_companion_frames.mjs", import.meta.url).href);
proto.sample = original.sample;
proto.setLook = original.setLook;
proto.setState = original.setState;
Object.assign(globalThis, { document: saved.document, requestAnimationFrame: saved.raf, cancelAnimationFrame: saved.caf, matchMedia: saved.matchMedia, ...realTimers });
Object.defineProperty(globalThis, "performance", { value: saved.performance, configurable: true, writable: true });
const report = await measure({ seconds: 12, samples: 1500 });
console.log("measure_companion_frames report:\n" + JSON.stringify(report, null, 2));
for (const check of report.checks) ok(check.pass, `${check.name}: ${check.value} (bar ${check.bar})`);
ok(report.pass);
console.log(`Avatar fixtures passed: ${checks} checks; 30/15 fps caps, 8 s hold with zero animation frames, reduced and hidden stillness, one animated avatar and one rAF-throttled pointer listener per window (F12), native gaze lifecycle and payload checks (F4), cue pulses at or under 0.5 Hz, the Veil (cast, hold, release, reduced, small sizes), E1 frame fixture.`);
