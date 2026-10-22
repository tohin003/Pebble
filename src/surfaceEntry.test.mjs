// Phase 23: the entry chooses the surface with one import() statement per surface.
// Vite attaches one preload list (scripts and stylesheets) to each import() site; when both surfaces shared one
// conditional expression, the pet window received App's stylesheet and never its own (0.6.0's opaque, unstyled
// companion window). This pins the shape that keeps each surface's stylesheet with its own import.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
const code = source.replace(/\/\/.*$/gm, "");

test("each surface is imported by its own statement", () => {
  const imports = [...code.matchAll(/import\(\s*["']([^"']+)["']\s*\)/g)].map((m) => m[1]);
  assert.deepEqual(imports.sort(), ["./App.vue", "./Pet.vue"]);
  for (const line of code.split("\n")) {
    const count = (line.match(/import\(/g) || []).length;
    assert.ok(count <= 1, "two import() calls on one line: " + line.trim());
  }
  assert.ok(!/\?\s*import\(/.test(code) && !/:\s*import\(/.test(code), "a conditional expression chooses between import() calls");
  assert.ok(!/=\s*\w+\s*\?[^;]*import\(/.test(code), "a conditional expression chooses between import() calls");
});

test("the pet branch marks the document before it loads the pet", () => {
  const branch = code.slice(code.indexOf("if (pet)"), code.indexOf("} else"));
  assert.ok(branch.indexOf('classList.add("pet-surface")') !== -1 && branch.indexOf('classList.add("pet-surface")') < branch.indexOf('import("./Pet.vue")'));
  assert.ok(code.slice(code.indexOf("} else")).includes('import("./App.vue")'));
});

test("the global stylesheet still loads for both surfaces", () => {
  assert.ok(/^import\s+["']\.\/style\.css["'];?$/m.test(source));
});
