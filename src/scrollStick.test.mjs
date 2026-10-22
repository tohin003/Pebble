import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { transform } from "sucrase";

// Phase 24 (V1-130, i8-code-04): streamed text follows only a person who is at the end; one more than 48 px above
// the bottom keeps their place; sending always shows the end. Both windows use the helper for every streamed token.
const require = createRequire(import.meta.url);
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const source = read("./scrollStick.ts");
const module = { exports: {} };
new Function("require", "module", "exports", transform(source, { transforms: ["typescript", "imports"] }).code)(require, module, module.exports);
const S = module.exports;

// A scroller whose content grows when the window draws, as a real one does after Vue patches the DOM.
function scroller({ height = 400, content = 2000, top = 1600 } = {}) {
  return { clientHeight: height, scrollHeight: content, scrollTop: top };
}
function harness(el) {
  const queue = [];
  const stick = S.createScrollStick(() => el, (fn) => queue.push(fn));
  // A token: measured now, drawn later (the content grows by `grow` px before the queued work runs).
  const token = (grow = 120) => {
    const followed = stick.follow();
    el.scrollHeight += grow;
    while (queue.length) queue.shift()();
    return followed;
  };
  const send = (grow = 60) => {
    stick.force();
    el.scrollHeight += grow;
    while (queue.length) queue.shift()();
  };
  return { stick, token, send };
}

test("a person at the end follows the streaming reply", () => {
  const el = scroller();
  const h = harness(el);
  for (let i = 0; i < 20; i++) assert.equal(h.token(), true);
  assert.equal(S.distanceFromBottom(el), 0, "still at the end after 20 tokens");
});

test("within 48 px of the end still counts as the end", () => {
  const el = scroller({ top: 1600 - 48 });
  assert.equal(S.atBottom(el), true);
  assert.equal(harness(el).token(), true);
  assert.equal(S.distanceFromBottom(el), 0);
});

test("a person more than 48 px above the end keeps their place while tokens stream", () => {
  const el = scroller({ top: 1600 - 49 });
  const h = harness(el);
  const before = el.scrollTop;
  for (let i = 0; i < 20; i++) assert.equal(h.token(), false);
  assert.equal(el.scrollTop, before, "the reading position holds");
  assert.ok(S.distanceFromBottom(el) > 48 * 20);
  // Scrolling back down to the end resumes following.
  el.scrollTop = el.scrollHeight - el.clientHeight;
  assert.equal(h.token(), true);
  assert.equal(S.distanceFromBottom(el), 0);
});

test("sending forces the scroll, wherever the person was", () => {
  const el = scroller({ top: 0 });
  const h = harness(el);
  h.send();
  assert.equal(S.distanceFromBottom(el), 0);
  assert.equal(S.atBottom(null), true, "no scroller: nothing to hold");
  S.toBottom(null);
});

test("both windows stream through the helper, and only sending forces", () => {
  ok(!/\brpc\b|invoke\(|localStorage|setTimeout|setInterval/.test(source), "pure helper");
  const app = read("./App.vue"), pet = read("./Pet.vue");
  for (const [name, text] of [["App.vue", app], ["Pet.vue", pet]]) {
    ok(/import \{ createScrollStick \} from "\.\/scrollStick";/.test(text), name + " imports the helper");
    ok(/createScrollStick\(/.test(text), name + " creates one");
  }
  // App: tokens and run events follow; send forces. The old unconditional scroll on a token is gone.
  const token = app.slice(app.indexOf('if (event.event === "token") {'), app.indexOf('} else if (event.event === "usage")'));
  ok(/stick\.follow\(\)/.test(token) && !/scrollBottom|scrollTo|stick\.force/.test(token), "App: a token follows only");
  ok(/applyRunEvent\(chat\.value, event\.event, d\)\) \{ stick\.follow\(\); return; \}/.test(app), "App: run events follow only");
  const send = app.slice(app.indexOf("async function send("), app.indexOf("async function stop("));
  ok(/stick\.force\(\)/.test(send), "App: sending forces the scroll");
  // Pet: the same rules.
  const petToken = pet.slice(pet.indexOf('if (event === "token") {'), pet.indexOf('if (event === "actions")'));
  ok(/stick\.follow\(\)/.test(petToken) && !/bottom\(\)|stick\.force/.test(petToken), "Pet: a token follows only");
  const petSend = pet.slice(pet.indexOf("async function send("), pet.indexOf("async function newChat("));
  ok(/stick\.force\(\)/.test(petSend), "Pet: sending forces the scroll");
});
function ok(value, message) {
  assert.ok(value, message);
}
