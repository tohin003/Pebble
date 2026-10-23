#!/usr/bin/env node
// Phase 33 (1.1.0, D46, D57): the README's captures of the Veil, rendered by Pebble's own Avatar.vue and animation
// engine. A window Stealth Lockdown protects cannot be screen-recorded, so these frames come from the component itself,
// driven by a fake clock exactly as src/components/Avatar.test.mjs drives it.
//
//   node scripts/render_veil_media.mjs OUT_DIR [--fps 30] [--size 220]
//
// Writes OUT_DIR/{light,dark}/NNN.svg, one frame per 1/fps s: the pebble idle 0.6 s, the cast (1.25 s), 6.6 s on (one
// shimmer), the release (0.65 s), then idle 0.6 s. Colours are tokens.css's for each theme, set on the frame's root.
// scripts/render_veil_gif.py turns the frames into GIFs.
import { mkdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript } from "@vue/compiler-sfc";
import { createRenderer, h, nextTick, reactive } from "vue";
import { transform } from "sucrase";

const require = createRequire(import.meta.url);
const args = process.argv.slice(2);
const out = args[0];
if (!out || existsSync(out)) {
  console.error("usage: render_veil_media.mjs NEW_OUT_DIR [--fps 30] [--size 220]");
  process.exit(2);
}
const option = (name, fallback) => (args.includes(name) ? Number(args[args.indexOf(name) + 1]) : fallback);
const FPS = option("--fps", 30);
const SIZE = option("--size", 220);

// ---- a fake window: one clock for animation frames and timers; no reduced motion ----
let clock = 1000;
const rafs = new Map();
let seq = 0;
globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} };
Object.defineProperty(globalThis, "performance", { value: { now: () => clock }, configurable: true, writable: true });
globalThis.requestAnimationFrame = (cb) => { const id = ++seq; rafs.set(id, cb); return id; };
globalThis.cancelAnimationFrame = (id) => rafs.delete(id);
const timers = new Map();
globalThis.setTimeout = (fn, ms = 0) => { const id = ++seq; timers.set(id, { at: clock + ms, fn }); return id; };
globalThis.clearTimeout = (id) => timers.delete(id);
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
async function settle() { for (let i = 0; i < 6; i++) { await Promise.resolve(); await nextTick(); } }
async function advance(ms) {
  const end = clock + ms;
  while (clock < end) {
    clock = Math.min(end, clock + 1000 / 60);
    for (const [id, t] of [...timers].sort((a, b) => a[1].at - b[1].at)) if (t.at <= clock && timers.delete(id)) t.fn();
    const due = [...rafs.values()];
    rafs.clear();
    for (const cb of due) cb(clock);
    await Promise.resolve();
  }
  await settle();
}

// ---- compile the real component ----
const root = new URL("../src/", import.meta.url);
const cache = new Map();
function loadTs(url) {
  if (cache.has(url.href)) return cache.get(url.href).exports;
  const module = { exports: {} };
  cache.set(url.href, module);
  new Function("require", "module", "exports", transform(readFileSync(url, "utf8"), { transforms: ["typescript", "imports"] }).code)(
    (name) => (name.startsWith(".") ? loadTs(new URL(name.endsWith(".ts") ? name : name + ".ts", url)) : require(name)), module, module.exports);
  return module.exports;
}
const avatarUrl = new URL("components/Avatar.vue", root);
const compiled = compileScript(parse(readFileSync(avatarUrl, "utf8"), { filename: "Avatar.vue" }).descriptor, { id: "Avatar.vue", inlineTemplate: true });
const avatarModule = { exports: {} };
new Function("require", "module", "exports", transform(compiled.content, { transforms: ["typescript", "imports"] }).code)(
  (name) => (name.startsWith(".") ? loadTs(new URL(name + ".ts", avatarUrl)) : require(name)), avatarModule, avatarModule.exports);
const Avatar = avatarModule.exports.default;

// ---- a renderer that keeps an element tree, serialised as SVG ----
const node = (type, text = "") => ({ type, text, children: [], props: {}, parent: null, getBoundingClientRect: () => ({ left: 0, top: 0, width: SIZE, height: SIZE }) });
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
const escape = (value) => String(value).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
function serialise(el, style) {
  if (el.type === "#text") return escape(el.text);
  if (el.type === "#comment") return "";
  const attrs = Object.entries(el.props)
    .filter(([key, value]) => value !== undefined && value !== null && value !== false && typeof value !== "function" && key !== "class")
    .map(([key, value]) => ` ${key}="${escape(value)}"`).join("");
  const extra = style ? ` xmlns="http://www.w3.org/2000/svg" style="${escape(style)}"` : "";
  return `<${el.type}${extra}${attrs}>${el.children.map((c) => serialise(c)).join("")}</${el.type}>`;
}

// ---- tokens.css, per theme ----
const tokens = readFileSync(new URL("tokens.css", root), "utf8");
function themeVars(selector) {
  const block = tokens.slice(tokens.indexOf(selector)).split("}")[0];
  return Object.fromEntries([...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}
const light = themeVars(":root {");
const dark = { ...light, ...themeVars(':root[data-theme="dark"]') };
const resolve = (vars, value) => (value?.startsWith("var(") ? resolve(vars, vars[value.slice(4, -1)]) : value);
const styleFor = (vars) => [`color: ${resolve(vars, "var(--avatar-body)")}`,
  ...Object.keys(vars).filter((k) => k.startsWith("--avatar") || k.startsWith("--stealth")).map((k) => `${k}: ${resolve(vars, vars[k])}`)].join("; ");

// ---- the scene ----
const props = reactive({ state: "idle", size: SIZE, stealth: false, gaze: false });
const host = node("root");
renderer.createApp({ setup: () => () => h("div", [h(Avatar, props)]) }).mount(host);
await settle();
const svg = () => {
  const find = (el) => (el.type === "svg" ? el : el.children.map(find).find(Boolean));
  return find(host);
};
const frames = [];
const step = 1000 / FPS;
async function record(seconds) {
  for (let i = 0; i < Math.round(seconds * FPS); i++) {
    await advance(step);
    frames.push({ light: serialise(svg(), styleFor(light)), dark: serialise(svg(), styleFor(dark)) });
  }
}
await advance(500);
await record(0.6);
props.stealth = true;
await settle();
await record(1.25 + 6.6); // the cast, then one full shimmer cycle on
props.stealth = false;
await settle();
await record(0.65 + 0.6);
for (const theme of ["light", "dark"]) {
  mkdirSync(`${out}/${theme}`, { recursive: true });
  frames.forEach((frame, i) => writeFileSync(`${out}/${theme}/${String(i).padStart(3, "0")}.svg`, frame[theme]));
}
writeFileSync(`${out}/frames.json`, JSON.stringify({ fps: FPS, size: SIZE, frames: frames.length,
  source: "src/components/Avatar.vue with src/stealthVeil.ts and src/vendor/bloub, on a fake clock" }, null, 1) + "\n");
console.log(JSON.stringify({ out, frames: frames.length, fps: FPS }));
