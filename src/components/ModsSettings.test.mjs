import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Compile only checked-in component source. Mock IPC never evaluates mod code.
const require = createRequire(import.meta.url);
const calls = [],
  installed = [],
  reviews = new Map();
let previewBarrier = null;
const runtime = { available: true, max_mods: 8, max_dispatches_per_run: 12 };
const example = {
  version: 1,
  name: "Context counts",
  events: ["context.summary", "manual"],
  capabilities: ["ui.render"],
  source:
    'pebble.on("manual", p => { pebble.render({id:"counts",title:"Counts",kind:"table",data:{columns:[{id:"count",label:"Count"}],rows:[{count:p.messages || 0}]}}); });',
};
function canonical(value) {
  const input =
    typeof value === "string"
      ? JSON.parse(value)
      : JSON.parse(JSON.stringify(value));
  return {
    capabilities: input.capabilities,
    events: [...input.events].sort(),
    name: input.name.trim(),
    source: input.source,
    version: input.version,
  };
}
const digest = (manifest) =>
  createHash("sha256")
    .update(JSON.stringify(canonical(manifest)))
    .digest("hex");
const clone = (value) => JSON.parse(JSON.stringify(value));
const bridge = {
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    if (method === "mods")
      return { items: clone(installed), runtime: clone(runtime) };
    if (method === "mod_example") return clone(example);
    if (method === "mod_preview") {
      const manifest = canonical(params.value),
        hash = digest(manifest);
      reviews.set(hash, clone(manifest));
      if (previewBarrier) await previewBarrier.promise;
      return { manifest, digest: hash, enabled: false, ...runtime };
    }
    if (method === "mod_install") {
      assert.deepEqual(
        canonical(params.value),
        reviews.get(params.digest),
        "install must use exact reviewed manifest",
      );
      assert.equal(params.digest, digest(params.value));
      const item = {
        id: "mod-" + (installed.length + 1),
        name: params.value.name,
        manifest: clone(params.value),
        digest: params.digest,
        enabled: false,
        grant_digest: null,
      };
      installed.push(item);
      return clone(item);
    }
    if (method === "mod_toggle") {
      const item = installed.find((item) => item.id === params.id);
      assert.equal(
        params.digest,
        item.digest,
        "toggle must use pinned installed digest",
      );
      item.enabled = params.enabled;
      item.grant_digest = params.enabled ? params.digest : null;
      return clone(item);
    }
    if (method === "mod_remove") {
      installed.splice(
        installed.findIndex((item) => item.id === params.id),
        1,
      );
      return true;
    }
    throw new Error("Unexpected RPC " + method);
  },
};
const source = readFileSync(
  new URL("./ModsSettings.vue", import.meta.url),
  "utf8",
);
const descriptor = parse(source, { filename: "ModsSettings.vue" }).descriptor;
const script = compileScript(descriptor, { id: "mods-fixture" });
const template = compileTemplate({
  source: descriptor.template.content,
  filename: "ModsSettings.vue",
  id: "mods-fixture",
  compilerOptions: { bindingMetadata: script.bindings },
});
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function(
    "require",
    "module",
    "exports",
    transform(code, { transforms: ["typescript", "imports"] }).code,
  )(
    (name) => (name === "../bridge" ? bridge : require(name)),
    module,
    module.exports,
  );
  return module.exports;
}
const Component = evaluate(script.content).default;
Component.render = evaluate(template.code).render;

// A minimal host runs real Vue mounted hooks, watchers, directives and buttons.
// It intentionally has no HTML parser, browser capabilities or script evaluator.
function node(type, text = "") {
  return {
    type,
    tagName: type.toUpperCase(),
    text,
    children: [],
    props: {},
    parent: null,
    value: "",
    addEventListener() {},
    removeEventListener() {},
    getRootNode() {
      return globalThis.document;
    },
  };
}
const renderer = createRenderer({
  createElement: (type) => node(type),
  createText: (text) => node("#text", text),
  createComment: (text) => node("#comment", text),
  insert(child, parent, anchor) {
    if (child.parent)
      child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    parent.children.splice(
      index < 0 ? parent.children.length : index,
      0,
      child,
    );
  },
  remove(child) {
    if (child.parent)
      child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = null;
  },
  setText: (child, text) => {
    child.text = text;
  },
  setElementText: (child, text) => {
    child.text = text;
    child.children = [];
  },
  patchProp: (child, key, _previous, value) => {
    child.props[key] = value;
  },
  parentNode: (child) => child.parent,
  nextSibling: (child) =>
    child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
});
const previousDocument = globalThis.document;
const previousDocumentClass = globalThis.Document,
  previousShadowRoot = globalThis.ShadowRoot;
globalThis.Document = class Document {
  activeElement = null;
};
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
const textOf = (element) =>
  (element.type === "#comment" ? "" : element.text) +
  element.children.map(textOf).join("");
const all = (element) => [element, ...element.children.flatMap(all)];
async function settle() {
  for (let index = 0; index < 6; index++) {
    await Promise.resolve();
    await nextTick();
  }
}
const props = reactive({ active: false, allowed: true });
let state;
const root = node("root");
const instrumented = {
  ...Component,
  setup(properties, context) {
    state = Component.setup(properties, context);
    return state;
  },
};
const app = renderer.createApp({
  setup: () => () =>
    h(instrumented, { active: props.active, allowed: props.allowed }),
});
app.config.errorHandler = (error) => {
  console.error(error);
  throw error;
};
app.mount(root);
function button(label) {
  const result = all(root).find(
    (element) => element.type === "button" && textOf(element) === label,
  );
  assert.ok(result, "Missing button " + label);
  return result;
}
async function click(label) {
  const target = button(label);
  assert.ok(!target.props.disabled, label + " must be enabled");
  await target.props.onClick();
  await settle();
}
function assertDisabled(label, expected = true) {
  assert.equal(!!button(label).props.disabled, expected, label);
}

try {
  await settle();
  await click("Load example");
  assert.equal(state.review.value, null);
  await click("Preview exact version");
  const hash = digest(example);
  assert.equal(state.review.value.digest, hash);
  assert.equal(installed.length, 0);
  await click("Install disabled version");
  assert.equal(installed[0].enabled, false);
  assert.equal(installed[0].grant_digest, null);
  assert.equal(calls.filter((call) => call.method === "mod_toggle").length, 0);
  assertDisabled("Enable reviewed version", false);
  await click("Enable reviewed version");
  assert.equal(installed[0].enabled, true);
  assert.equal(installed[0].grant_digest, hash);
  assert.deepEqual(calls.find((call) => call.method === "mod_toggle").params, {
    id: "mod-1",
    enabled: true,
    digest: hash,
  });

  props.active = true;
  await settle();
  assertDisabled("Install disabled version");
  assertDisabled("Disable", false);
  assertDisabled("Remove", false);
  await click("Disable");
  assertDisabled("Enable reviewed version");
  props.active = false;
  props.allowed = false;
  await settle();
  assertDisabled("Install disabled version");
  assertDisabled("Enable reviewed version");
  props.active = true;
  await settle();
  assertDisabled("Remove", false);
  await click("Remove");
  assert.equal(installed.length, 0);
  props.active = false;
  props.allowed = true;
  await settle();

  await click("Preview exact version");
  state.input.value = JSON.stringify({ ...example, name: "Edited version" });
  await settle();
  assert.equal(
    state.review.value,
    null,
    "editing input must invalidate reviewed digest",
  );
  assert.equal(state.selected.value, "");
  await click("Preview exact version");
  await click("Install disabled version");
  const item = installed[0];
  state.review.value = {
    manifest: clone(item.manifest),
    digest: "f".repeat(64),
  };
  state.selected.value = item.id;
  await settle();
  assertDisabled("Enable reviewed version");
  await state.toggle(item);
  await settle();
  assert.ok(state.error.value.includes("full source"));
  await click("Review source");
  assert.equal(state.review.value.digest, item.digest);
  assert.equal(state.selected.value, item.id);
  assertDisabled("Enable reviewed version", false);
  runtime.available = false;
  await state.load();
  await settle();
  assertDisabled("Enable reviewed version");
  runtime.available = true;
  await state.load();
  await settle();

  // A changed input must also invalidate a preview already in flight.
  let release;
  previewBarrier = {
    promise: new Promise((resolve) => {
      release = resolve;
    }),
  };
  const pending = state.preview();
  await Promise.resolve();
  state.input.value = JSON.stringify({
    ...example,
    name: "Changed during preview",
  });
  await settle();
  release();
  await pending;
  previewBarrier = null;
  await settle();
  assert.equal(
    state.review.value,
    null,
    "late preview reply must not restore an invalidated review after input changes",
  );

  let releaseInspect;
  previewBarrier = {
    promise: new Promise((resolve) => {
      releaseInspect = resolve;
    }),
  };
  const inspecting = state.inspect(installed[0]);
  await Promise.resolve();
  state.input.value = JSON.stringify({
    ...example,
    name: "Changed during source inspection",
  });
  await settle();
  releaseInspect();
  await inspecting;
  previewBarrier = null;
  await settle();
  assert.equal(
    state.review.value,
    null,
    "late source inspection must not restore edited review",
  );
  assert.equal(state.selected.value, "");

  const hostile = {
    ...example,
    name: "<img src=x onerror=host()>",
    source: "// <script>host()</script>\n" + example.source,
  };
  const seeded = {
    ...Component,
    setup(properties, context) {
      const bindings = Component.setup(properties, context);
      bindings.items.value = [
        {
          id: "literal",
          name: hostile.name,
          manifest: hostile,
          digest: "d".repeat(64),
          enabled: false,
          error: "<svg onload=host()>",
        },
      ];
      bindings.review.value = { manifest: hostile, digest: "d".repeat(64) };
      return bindings;
    },
  };
  const html = await renderToString(
    createSSRApp(seeded, { active: false, allowed: true }),
  );
  assert.ok(
    html.includes("&lt;img src=x onerror=host()&gt;") &&
      html.includes("&lt;script&gt;host()&lt;/script&gt;") &&
      html.includes("&lt;svg onload=host()&gt;"),
  );
  assert.ok(
    !html.includes("<script>") &&
      !html.includes("<img") &&
      !html.includes("<svg"),
  );
  console.log(
    "Mods settings passed: pinned review/install/enable, disabled install, separate grant, active/views/runtime guards, revocation controls, input and in-flight review invalidation, literal source/title/row rendering. Backend IPC is simulated; no mod source executes.",
  );
} finally {
  app.unmount();
  globalThis.document = previousDocument;
  globalThis.Document = previousDocumentClass;
  globalThis.ShadowRoot = previousShadowRoot;
}
