#!/usr/bin/env node
// Reproducible frame and CPU fixture for the companion avatar (Phase 19 B1, design study §2.7,
// experiment E1, first step).
//
// It mounts the real src/components/Avatar.vue and the vendored bloub engine under a fake
// animation-frame clock, drives every pose for a simulated period, and reports JSON: engine samples
// and animation frames per second, animation frames after the 8 s hold, and the real time this
// process spends per frame. Pass bars:
//   - engine sampling p95 under 1.5 ms per frame;
//   - at most 30 fps, and 15 fps with the pet's low-power/fullscreen flag;
//   - zero animation frames in static states (rows 13–18 after 8 s, reduced motion, hidden, and
//     every avatar but the window's elected one);
//   - an estimated 3% or less of one core while animating and 0.5% or less while static;
//   - no frame of 50 ms or more; at most one pointer update per frame, from one listener.
// The CPU figure covers script work only (engine, motion and Vue patching in Node). It excludes
// WebKit style, layout, paint and compositing, so the installed webview trace under synthetic
// load (E1 second step, parent-run) remains the authority for the 3% / 0.5% targets.
//
// Usage: node scripts/measure_companion_frames.mjs [--seconds=20] [--hz=60] [--samples=3000]
//        [--load=N busy threads] [--out=report.json]
// Exit code 0 when every check passes. No network, no model, no files read but checked-in sources.
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import { pathToFileURL } from "node:url";
import { Worker } from "node:worker_threads";

const require = createRequire(import.meta.url);
const ROOT = new URL("../", import.meta.url);

export const BARS = {
  engine_sample_p95_ms: 1.5,
  max_fps: 30,
  low_power_fps: 15,
  static_raf_frames: 0,
  animating_cpu_pct: 3,
  static_cpu_pct: 0.5,
  long_frame_ms: 50,
  pointer_listeners: 1,
  pointer_updates_per_frame: 1,
  animated_avatars_per_window: 1,
};
const HOLD_S = 8;

function installWindow() {
  const saved = {
    document: globalThis.document, performance: globalThis.performance, requestAnimationFrame: globalThis.requestAnimationFrame,
    cancelAnimationFrame: globalThis.cancelAnimationFrame, matchMedia: globalThis.matchMedia,
    setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout,
  };
  const listeners = new Map();
  const env = {
    clock: 0,
    queue: new Map(),
    timers: new Map(),
    seq: 0,
    requests: 0,
    listeners,
    dispatch(type, event = {}) { for (const fn of [...(listeners.get(type) || [])]) fn(event); },
    // Timers due by now run first, then this frame's animation callbacks (as in a browser).
    runTimers() {
      let ran = 0;
      for (const [id, t] of [...env.timers.entries()].sort((a, b) => a[1].at - b[1].at))
        if (t.at <= env.clock && env.timers.has(id)) { env.timers.delete(id); ran++; t.fn(); }
      return ran;
    },
    restore() {
      for (const [key, value] of Object.entries(saved)) {
        if (key === "performance") Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
        else if (value === undefined) delete globalThis[key];
        else globalThis[key] = value;
      }
    },
  };
  globalThis.document = {
    hidden: false,
    addEventListener: (type, fn) => (listeners.get(type) || listeners.set(type, new Set()).get(type)).add(fn),
    removeEventListener: (type, fn) => listeners.get(type)?.delete(fn),
  };
  Object.defineProperty(globalThis, "performance", { value: { now: () => env.clock }, configurable: true, writable: true });
  globalThis.requestAnimationFrame = (cb) => { env.requests++; const id = ++env.seq; env.queue.set(id, cb); return id; };
  globalThis.cancelAnimationFrame = (id) => { env.queue.delete(id); };
  globalThis.setTimeout = (fn, delay = 0) => { const id = ++env.seq; env.timers.set(id, { at: env.clock + Math.max(0, delay), fn }); return id; };
  globalThis.clearTimeout = (id) => { env.timers.delete(id); };
  globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  return env;
}

function compile() {
  const { transform } = require("sucrase");
  const { parse, compileScript } = require("@vue/compiler-sfc");
  const cache = new Map();
  const loadTs = (url) => {
    if (cache.has(url.href)) return cache.get(url.href).exports;
    const module = { exports: {} };
    cache.set(url.href, module);
    new Function("require", "module", "exports", transform(readFileSync(url, "utf8"), { transforms: ["typescript", "imports"] }).code)(
      (name) => (name.startsWith(".") ? loadTs(new URL(name.endsWith(".ts") ? name : name + ".ts", url)) : require(name)), module, module.exports);
    return module.exports;
  };
  const url = new URL("src/components/Avatar.vue", ROOT);
  const descriptor = parse(readFileSync(url, "utf8"), { filename: "Avatar.vue" }).descriptor;
  const script = compileScript(descriptor, { id: "Avatar.vue", inlineTemplate: true });
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(script.content, { transforms: ["typescript", "imports"] }).code)(
    (name) => (name.startsWith(".") ? loadTs(new URL(name + ".ts", url)) : require(name)), module, module.exports);
  return {
    Avatar: module.exports.default,
    engine: loadTs(new URL("src/vendor/bloub/engine.ts", ROOT)),
    skins: loadTs(new URL("src/vendor/bloub/skins.ts", ROOT)),
    expressions: loadTs(new URL("src/vendor/bloub/expressions.ts", ROOT)),
    state: loadTs(new URL("src/companionState.ts", ROOT)),
    pointer: loadTs(new URL("src/companionPointer.ts", ROOT)),
  };
}

const ms = (start) => Number(process.hrtime.bigint() - start) / 1e6;
function stats(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const at = (q) => sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] : 0;
  const round = (v) => Math.round(v * 10000) / 10000;
  return { n: sorted.length, p50: round(at(0.5)), p95: round(at(0.95)), p99: round(at(0.99)), max: round(sorted.at(-1) || 0) };
}

// Engine sampling cost: every engine/expression pair the poses use, with a moving gaze.
function sampleBenchmark(m, count) {
  const radii = m.skins.SHAPE_BY_ID.get("galet").radii;
  const pairs = [...new Set(Object.values(m.state.POSES).map((v) => v.engine + "/" + v.expression))];
  const times = [];
  for (const pair of pairs) {
    const [state, expression] = pair.split("/");
    const engine = new m.engine.BotEngine(65, "idle", radii);
    engine.setState(state, 0);
    engine.setExpression(m.expressions.EXPRESSION_BY_ID.get(expression) || null, 0);
    for (let i = 0; i < count + 200; i++) {
      const t = i / 30;
      if (i % 5 === 0) engine.setLook({ yaw: 30 * Math.sin(t), pitch: 20 * Math.cos(t), mix: 0.8, spin: 0, wander: 0.1 }, t);
      const start = process.hrtime.bigint();
      engine.sample(t);
      if (i >= 200) times.push(ms(start));
    }
  }
  return { pairs: pairs.length, ...stats(times) };
}

function renderer() {
  const { createRenderer } = require("vue");
  const node = (type, text = "") => {
    const n = { type, text, children: [], props: {}, parent: null };
    n.getBoundingClientRect = () => ({ left: 0, top: 0, width: 112, height: 112 });
    return n;
  };
  return {
    node,
    r: createRenderer({
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
      patchProp(child, key, _p, value) { child.props[key] = value; },
      parentNode: (child) => child.parent,
      nextSibling: (child) => child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
    }),
  };
}
const flush = async () => { for (let i = 0; i < 3; i++) await Promise.resolve(); };
const all = (el) => [el, ...el.children.flatMap(all)];

// One pose for `seconds` simulated seconds at `hz`, counting frames and timing real work.
async function scenario(m, env, { state, seconds, hz, fps = 30, reduced = false, hidden = false, second = false, mode = "animated" }) {
  const { h } = require("vue");
  const proto = m.engine.BotEngine.prototype;
  const sample = proto.sample;
  let sampled = 0;
  proto.sample = function (t) { sampled++; return sample.call(this, t); };
  globalThis.document.hidden = hidden;
  env.queue.clear();
  env.timers.clear();
  const { node, r } = renderer();
  const root = node("root");
  const list = [{ key: "a", state, fps, reduced, rank: 0 }];
  if (second) list.push({ key: "b", state, fps, reduced, rank: 2 });
  const app = r.createApp({ setup: () => () => h("div", list.map((p) => h(m.Avatar, p))) });
  app.mount(root);
  await flush();
  const step = 1000 / hz;
  const frames = Math.round(seconds * hz);
  const record = [];
  for (let i = 0; i < frames; i++) {
    env.clock += step;
    const before = sampled;
    const start = process.hrtime.bigint();
    const timers = env.runTimers();
    const queue = env.queue;
    env.queue = new Map();
    if (queue.size || timers) {
      for (const cb of queue.values()) cb(env.clock);
      await flush();
      record.push({ t: (i + 1) / hz, raf: queue.size, timers, samples: sampled - before, cost: ms(start) });
    } else record.push({ t: (i + 1) / hz, raf: 0, timers: 0, samples: 0, cost: 0 });
  }
  const animatedAvatars = all(root).filter((el) => el.type === "svg" && el.props["data-animated"] === "true").length;
  app.unmount();
  await flush();
  proto.sample = sample;
  globalThis.document.hidden = false;
  const visual = m.state.POSES[state];
  const window = (from) => record.filter((f) => f.t > from);
  const steady = visual.hold && mode === "animated" ? window(HOLD_S + 1) : window(1);
  const span = steady.length / hz || 1;
  const cost = steady.reduce((a, f) => a + f.cost, 0);
  const perSecond = (frames, key) => Math.round((frames.reduce((a, f) => a + f[key], 0) / ((frames.length / hz) || 1)) * 100) / 100;
  const woke = (f) => f.raf > 0 || f.timers > 0;
  return {
    pose: state,
    mode,
    hold: visual.hold,
    animated_avatars: animatedAvatars,
    fps: perSecond(window(1), "samples"),
    raf_callbacks_per_s: perSecond(window(1), "raf"),
    timer_wakeups_per_s: perSecond(window(1), "timers"),
    wake_frames: record.filter(woke).length,
    wake_frames_steady: steady.filter(woke).length,
    steady_window_s: Math.round(span * 100) / 100,
    frame_ms: stats(record.filter(woke).map((f) => f.cost)),
    cpu_pct_estimate: Math.round((cost / (span * 1000)) * 100 * 1000) / 1000,
  };
}

// Pointer: many events per frame, one shared listener, at most one gaze update per frame.
async function pointerScenario(m, env, { seconds = 2, hz = 60, perFrame = 10 } = {}) {
  const { h } = require("vue");
  env.queue.clear();
  env.timers.clear();
  const { node, r } = renderer();
  const root = node("root");
  const app = r.createApp({ setup: () => () => h("div", [h(m.Avatar, { state: "thinking", rank: 0 }), h(m.Avatar, { state: "thinking", rank: 2 })]) });
  app.mount(root);
  await flush();
  let maxUpdates = 0, updates = 0, events = 0;
  for (let i = 0; i < seconds * hz; i++) {
    for (let j = 0; j < perFrame; j++) { env.dispatch("pointermove", { clientX: (i * 7 + j * 13) % 400, clientY: (i * 5) % 300 }); events++; }
    env.clock += 1000 / hz;
    env.runTimers();
    const queue = env.queue;
    env.queue = new Map();
    const pointerFlushes = [...queue.values()].filter((cb) => cb.name === "flush").length;
    maxUpdates = Math.max(maxUpdates, pointerFlushes);
    updates += pointerFlushes;
    for (const cb of queue.values()) cb(env.clock);
    await flush();
  }
  const result = { events, frames: seconds * hz, updates, max_updates_per_frame: maxUpdates, listeners: env.listeners.get("pointermove")?.size || 0, followers: m.pointer.pointerListeners().followers };
  app.unmount();
  await flush();
  result.listeners_after_unmount = env.listeners.get("pointermove")?.size || 0;
  return result;
}

async function startLoad(threads) {
  const workers = [];
  for (let i = 0; i < threads; i++) workers.push(new Worker("for (;;) {}", { eval: true }));
  if (threads) await new Promise((resolve) => setTimeout(resolve, 200));
  return workers;
}

export async function measure({ seconds = 20, hz = 60, samples = 3000, load = 0 } = {}) {
  if (seconds < HOLD_S + 2) throw new Error(`Use at least ${HOLD_S + 2} simulated seconds so the hold window is measured.`);
  const workers = await startLoad(load); // before the fake clock: it waits on a real timer
  const env = installWindow();
  try {
    const m = compile();
    const engine = sampleBenchmark(m, samples);
    const poses = [];
    for (const state of Object.keys(m.state.POSES)) poses.push(await scenario(m, env, { state, seconds, hz }));
    const special = [
      await scenario(m, env, { state: "thinking", seconds, hz, fps: 15, mode: "pet-low-power" }),
      await scenario(m, env, { state: "thinking", seconds, hz, reduced: true, mode: "reduced" }),
      await scenario(m, env, { state: "thinking", seconds, hz, hidden: true, mode: "hidden" }),
      await scenario(m, env, { state: "thinking", seconds, hz, second: true, mode: "two-avatars" }),
    ];
    const pointer = await pointerScenario(m, env, { hz });
    const animating = poses.filter((p) => !p.hold);
    const held = poses.filter((p) => p.hold);
    const longest = Math.max(...[...poses, ...special].map((p) => p.frame_ms.max));
    const checks = [
      { name: "engine sample p95 ms", value: engine.p95, bar: `< ${BARS.engine_sample_p95_ms}`, pass: engine.p95 < BARS.engine_sample_p95_ms },
      { name: "max fps while animating", value: Math.max(...poses.map((p) => p.fps)), bar: `<= ${BARS.max_fps}`, pass: poses.every((p) => p.fps <= BARS.max_fps + 0.5) },
      { name: "animating poses keep animating", value: Math.min(...animating.map((p) => p.fps)), bar: ">= 29", pass: animating.every((p) => p.fps >= 29) },
      { name: "pet low-power fps", value: special[0].fps, bar: `<= ${BARS.low_power_fps}`, pass: special[0].fps <= BARS.low_power_fps + 0.5 && special[0].fps >= 14 },
      { name: "animation-frame callbacks per second over fps (max; skipped display frames sleep)", value: Math.max(...[...animating, special[0]].map((p) => Math.round((p.raf_callbacks_per_s - p.fps) * 100) / 100)), bar: "<= 1", pass: [...animating, special[0]].every((p) => p.raf_callbacks_per_s <= p.fps + 1) },
      { name: "wake-ups after the 8 s hold (rows 13–18 and other calm poses)", value: held.reduce((a, p) => a + p.wake_frames_steady, 0), bar: `= ${BARS.static_raf_frames}`, pass: held.every((p) => p.wake_frames_steady === 0) },
      { name: "wake-ups under reduced motion", value: special[1].wake_frames, bar: "= 0", pass: special[1].wake_frames === 0 },
      { name: "wake-ups while hidden", value: special[2].wake_frames, bar: "= 0", pass: special[2].wake_frames === 0 },
      { name: "animated avatars with two mounted", value: special[3].animated_avatars, bar: `= ${BARS.animated_avatars_per_window}`, pass: special[3].animated_avatars === 1 && special[3].fps <= BARS.max_fps + 0.5 },
      { name: "estimated script CPU % of one core while animating (max)", value: Math.max(...animating.map((p) => p.cpu_pct_estimate)), bar: `<= ${BARS.animating_cpu_pct}`, pass: animating.every((p) => p.cpu_pct_estimate <= BARS.animating_cpu_pct) },
      { name: "estimated script CPU % of one core while static (max)", value: Math.max(...held.map((p) => p.cpu_pct_estimate), special[1].cpu_pct_estimate, special[2].cpu_pct_estimate), bar: `<= ${BARS.static_cpu_pct}`, pass: [...held, special[1], special[2]].every((p) => p.cpu_pct_estimate <= BARS.static_cpu_pct) },
      { name: "longest frame ms", value: longest, bar: `< ${BARS.long_frame_ms}`, pass: longest < BARS.long_frame_ms },
      { name: "document pointermove listeners", value: pointer.listeners, bar: `= ${BARS.pointer_listeners}`, pass: pointer.listeners === 1 && pointer.listeners_after_unmount === 0 },
      { name: "pointer gaze updates per frame (max)", value: pointer.max_updates_per_frame, bar: `<= ${BARS.pointer_updates_per_frame}`, pass: pointer.max_updates_per_frame <= 1 && pointer.followers === 1 },
    ];
    return {
      fixture: "measure_companion_frames",
      version: 1,
      design: "docs/HARNESS_PHASE19_DESIGN_STUDY.md §2.7 (E1, fake-clock step)",
      environment: {
        node: process.version, platform: process.platform, arch: process.arch, cpus: os.cpus().length,
        cpu_model: os.cpus()[0]?.model || "unknown", loadavg: os.loadavg().map((v) => Math.round(v * 100) / 100), synthetic_load_threads: load,
      },
      config: { simulated_seconds: seconds, display_hz: hz, engine_samples_per_pair: samples, hold_seconds: HOLD_S },
      engine_sample_ms: engine,
      poses,
      special,
      pointer,
      bars: BARS,
      checks,
      pass: checks.every((c) => c.pass),
      limits: [
        "Fake animation-frame clock: frame counts are exact for this source; per-frame cost is real time in this Node process.",
        "CPU % covers script work only (engine, motion, Vue patching); WebKit style, layout, paint and compositing are excluded. The installed webview trace under synthetic load is the authority for the 3% and 0.5% targets.",
        "Low Power Mode and fullscreen detection are a native flag (parent patch P8); here the 15 fps path is driven through the fps prop.",
      ],
    };
  } finally {
    env.restore();
    await Promise.all(workers.map((w) => w.terminate()));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const args = Object.fromEntries(process.argv.slice(2).map((a) => a.replace(/^--/, "").split("=")));
  const options = {};
  for (const key of ["seconds", "hz", "samples", "load"]) if (args[key] !== undefined) options[key] = Number(args[key]);
  const report = await measure(options);
  const text = JSON.stringify(report, null, 2);
  if (args.out) writeFileSync(args.out, text + "\n");
  console.log(text);
  process.exitCode = report.pass ? 0 : 1;
}
