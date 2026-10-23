import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse, compileTemplate } from "@vue/compiler-sfc";

// Phase 27 (1.0.2): the landing session's captures of the real app showed the person's message starting with a space.
// The bubble keeps the text's own line breaks (white-space: pre-wrap), and the message sat on its own template line,
// so the compiler's condensed whitespace (" " + text + " ") was drawn. The text now has an element of its own, and the
// whitespace around it, between elements, is dropped.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("the person's message is drawn without a leading or trailing space", () => {
  assert.ok(/\.user-bubble \{[^}]*white-space: pre-wrap;/.test(read("./style.css")), "the bubble keeps line breaks");
  const { descriptor } = parse(read("./App.vue"), { filename: "App.vue" });
  const { code, errors } = compileTemplate({ source: descriptor.template.content, filename: "App.vue", id: "app",
    compilerOptions: { expressionPlugins: ["typescript"] } });
  assert.deepEqual(errors, []);
  const shown = [...code.matchAll(/[^\n]*_toDisplayString\(message\.content\)[^\n]*/g)].map((match) => match[0]);
  assert.equal(shown.length, 1, shown.join("\n"));
  assert.ok(!/" "\s*\+\s*_toDisplayString\(message\.content\)|_toDisplayString\(message\.content\)\s*\+\s*" "/.test(shown[0]),
    "no padding around the message: " + shown[0]);
  assert.ok(/class: "user-text"/.test(code), "the message has its own element");
});
