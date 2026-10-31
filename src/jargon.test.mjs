import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { babelParse, parse } from "@vue/compiler-sfc";

// Phase 24 (V1-108, i5-ux-08, i11b-features-08, i11a-core-17): internal words stay off the surfaces this track owns.
// The scan reads what a person can see or hear: template text, visible attributes (aria-label, title, placeholder,
// alt, label) and the string literals of scripts that read as words (comments, identifiers, route names and CSS
// classes are not shown). A pinned string stays only while the fixture that pins it still contains it, so the
// allowlist can only shrink.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const BANNED = /\b(inference|receipts?|host|runtime|capacity|E-B1)\b/i;
const NOWHERE = /Activity & review|Windows Pebble can use|Pebble's browser|Settings → Mods|Settings → Extensions/;
const OWNED = [
  "./components/AdvancedSettings.vue", "./components/NotificationSettings.vue", "./components/CommandPalette.vue",
  "./components/CodingWorkflows.vue", "./components/CompanionWorkflows.vue", "./components/AgentPanel.vue",
  "./components/GoalPanel.vue", "./components/ContextPanel.vue", "./components/ContextInspector.vue",
  "./components/UpgradeMaintenance.vue", "./components/ModsSettings.vue", "./components/ResearchSources.vue",
  "./components/MemoryImpactView.vue", "./slashCommands.ts", "./permissionModes.ts",
  // Phase 46 (1.9.29): the work view's surfaces
  "./components/WorkView.vue", "./components/WorkCard.vue", "./components/BranchCard.vue", "./components/WorkStrip.vue", "./workView.ts",
  // Phase 46 (1.9.29): visual answers' surfaces
  "./components/VisualChart.vue", "./components/VisualDiagram.vue", "./components/VisualCatalog.vue", "./components/ReplyTable.vue",
  "./components/ReplyBody.vue", "./visuals.ts", "./chartModel.ts", "./diagramLayout.ts",
];
// Exact strings that existing fixtures pin, with the fixture that pins each. Changing one needs that fixture to
// change too, which this track may not do; each is listed in the t24-ux-settings note for its owner.
const pin = (file, by, ...texts) => texts.map((text) => ({ file, by, text }));
const PINNED = [
  ...pin("./components/CodingWorkflows.vue", "./components/CodingWorkflows.test.mjs", "Refresh receipts", "No reviewed Git receipts in this chat.", "Read back receipt",
    "Undo receipt", "Readback of receipt", "Receipt state", "Matches the receipt", "Git receipt data is incomplete.", "host reply was incomplete"),
  ...pin("./components/GoalPanel.vue", "./components/GoalPanel.test.mjs", "Verify receipts for criterion", "Verify receipts", "Blockers (host records)"),
  // Phase 45 (1.9.17, audit round R10): Context use names Pebble, never "host"
  ...pin("./components/ContextPanel.vue", "./components/ContextPanel.test.mjs", "Receipt "),
  ...pin("./components/ContextInspector.vue", "./components/ContextInspector.test.mjs", "Host records only: nothing here is summarised or judged by a model.",
    "Statuses are as of the last host check and are not re-checked here; /context re-checks them.", "Host method:"),
  ...pin("./components/MemoryImpactView.vue", "./components/MemoryImpactView.test.mjs", "Task receipts:"),
];
const VISIBLE_ATTRS = new Set(["aria-label", "title", "placeholder", "alt", "label", "aria-description"]);

function literals(code) {
  const found = [];
  let ast;
  try {
    ast = babelParse(code, { sourceType: "module", plugins: ["typescript"], errorRecovery: true });
  } catch {
    ast = babelParse("(" + code + ")", { plugins: ["typescript"] });
  }
  const walk = (node) => {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) return node.forEach(walk);
    if (node.type === "StringLiteral") found.push(node.value);
    if (node.type === "TemplateLiteral") found.push(node.quasis.map((q) => q.value.cooked ?? "").join(" "));
    if (node.type === "ImportDeclaration" || node.type === "TSTypeAnnotation" || node.type === "TSTypeAliasDeclaration" || node.type === "TSInterfaceDeclaration") return;
    if (node.type === "CallExpression" && /^(rpc|require)$/.test(node.callee?.name || "")) return node.arguments.slice(1).forEach(walk);
    for (const [key, value] of Object.entries(node)) if (!["loc", "start", "end", "leadingComments", "trailingComments", "innerComments", "extra"].includes(key)) walk(value);
  };
  walk(ast.program ?? ast);
  return found;
}
const readsAsWords = (text) => /\s/.test(text.trim()) || /^[A-Z][a-z]/.test(text.trim());
/** The strings a person can see in a .vue or .ts file. */
function shown(path) {
  const source = read(path);
  if (path.endsWith(".ts")) return literals(source).filter(readsAsWords);
  const { descriptor } = parse(source, { filename: path });
  const out = [];
  const visit = (node) => {
    if (!node) return;
    if (node.type === 2) out.push(node.content);
    if (node.type === 5) out.push(...literals(node.content.content));
    if (node.type === 1) {
      for (const prop of node.props) {
        if (prop.type === 6 && VISIBLE_ATTRS.has(prop.name) && prop.value) out.push(prop.value.content);
        if (prop.type === 7 && prop.name === "bind" && VISIBLE_ATTRS.has(prop.arg?.content) && prop.exp) out.push(...literals(prop.exp.content));
      }
    }
    (node.children || []).forEach(visit);
  };
  visit(descriptor.template.ast);
  for (const block of [descriptor.script, descriptor.scriptSetup]) if (block) out.push(...literals(block.content).filter(readsAsWords));
  return out.map((text) => text.replace(/\s+/g, " ").trim()).filter(Boolean);
}

test("no internal words on the surfaces this track owns", () => {
  const hits = [];
  for (const path of OWNED) {
    const allowed = PINNED.filter((p) => p.file === path).map((p) => p.text);
    for (const text of shown(path)) {
      if (!BANNED.test(text) && !NOWHERE.test(text)) continue;
      if (allowed.some((pinned) => text.includes(pinned) && !BANNED.test(text.replace(pinned, "")))) continue;
      hits.push(path.replace("./", "") + ": " + text);
    }
  }
  assert.deepEqual(hits, [], "plain words needed:\n" + hits.join("\n"));
});

test("the scan sees what a person sees", () => {
  const settings = shown("./components/AdvancedSettings.vue");
  assert.ok(settings.includes("Continue with ChatGPT") && settings.includes("Forget this memory:") && settings.includes("Search this layer's memories"),
    "template text, visible attributes and script words are read");
  assert.ok(!settings.some((t) => /chatgpt_start|memory_list|settings-error/.test(t)), "route names and classes are not text");
  assert.ok(!shown("./slashCommands.ts").some((t) => t === "host"), "an identifier-like value is not shown text");
});

test("every pinned string is still pinned and still present", () => {
  for (const { file, text, by } of PINNED) {
    assert.ok(shown(file).some((t) => t.includes(text.trim())), file + " still shows " + JSON.stringify(text) + " (remove the entry otherwise)");
    assert.ok(read(by).includes(text), by + " still pins " + JSON.stringify(text) + " (change the copy otherwise)");
  }
});

test("the / menu descriptions are plain, and the one pinned /goal phrase stays", () => {
  const backend = read("../backend/pebble/runtime_commands.py");
  const descriptions = [...backend.matchAll(/['"]description['"]:\s*(['"])((?:\\.|(?!\1).)*)\1/g)].map((m) => m[2].replace(/\\'/g, "'"));
  assert.equal(descriptions.length, 28);
  for (const text of descriptions) assert.ok(!BANNED.test(text), text);
  // test_phase17_security.py:1201 pins "no inference" in the /goal reply; it is the only allowed use.
  const uses = [...backend.matchAll(/no inference/g)].length;
  assert.equal(uses, 2, "the phrase appears in GOAL_FOOTER and in the comment that explains it, nowhere else");
  assert.match(backend, /GOAL_FOOTER = '[^']*uses no inference\.'/);
  assert.match(read("../backend/tests/test_phase17_security.py"), /'no inference' in reply\.lower\(\)/);
});

test("the browser surfaces of t24-browser show no E-B1 or E-N2 plan names", () => {
  for (const path of ["./components/PebbleBrowser.vue", "./components/ScreenSessions.vue"])
    for (const text of shown(path)) assert.ok(!/\bE-(B|N)\d\b/.test(text), path + ": " + text);
});
