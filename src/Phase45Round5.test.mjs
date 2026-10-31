// Phase 45 (1.9.11): audit round R5's last findings (F162, F172-F176), held to what the person sees.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const app = read("./App.vue");

test("Next hour picks the next whole hour at least five minutes away (F173)", () => {
  const source = read("./components/CompanionWorkflows.vue");
  const local = source.match(/function local\(date: Date\) \{[\s\S]*?\n\}/)[0].replace("(date: Date)", "(date)").replace("(n: number)", "(n)");
  const inAnHour = source.match(/function inAnHour\(\) \{[\s\S]*?\n\}/)[0];
  const at = (hours, minutes) => {
    const fixed = new Date(2026, 9, 26, hours, minutes).getTime();
    class FakeDate extends Date {
      static now() { return fixed; }
    }
    return new Function("Date", `${local}\n${inAnHour}\nreturn inAnHour();`)(FakeDate);
  };
  assert.equal(at(10, 43), "2026-10-26T11:00", "at 10:43 it gave 12:00 (R5)");
  assert.equal(at(10, 54), "2026-10-26T11:00");
  assert.equal(at(10, 56), "2026-10-26T12:00", "four minutes is too close to review and create");
  assert.equal(at(23, 30), "2026-10-27T00:00");
});

test("the schedule review lists its limits once and says where macOS notifications are switched (F174)", () => {
  const source = read("./components/CompanionWorkflows.vue");
  const review = source.slice(source.indexOf('aria-label="Schedule review"'), source.indexOf('aria-label="Configuration to pin"'));
  assert.ok(!review.includes("<dt>Limits</dt>"), "Limits is in the configuration rows, in words");
  assert.ok(review.includes("macOS notifications follow Settings › General › Notifications."));
});

test("connecting a project keeps an open conversation where it was, and the project's row is marked (F175, F176)", () => {
  const choose = app.slice(app.indexOf("async function chooseProject() {"), app.indexOf("// Phase 45 (F030): projects stay listed"));
  assert.ok(choose.indexOf("if (chat.value?.messages.length) await createChat({ quiet: true });") < choose.indexOf("const c = await ensureChat();"));
  assert.ok(app.includes(`:class="{ selected: !!chat?.workspace }"\n        :aria-current="chat?.workspace ? 'true' : undefined"`));
  assert.ok(app.includes(">Work in {{ projectName }}</small"), "the empty project's first card works in it");
});

test("a request to remove a kept tool shows its Remove card, and only the person's click removes it (F162)", () => {
  assert.ok(app.includes('(d.kind !== "kept" && d.kind !== "asked" && d.kind !== "remove")'));
  assert.ok(app.includes('if ((card.kind === "asked" || card.kind === "remove") && !card.done) return true;'));
  const card = app.slice(app.indexOf("<template v-else-if=\"card.kind === 'remove'\">"), app.indexOf("<template v-else>", app.indexOf("<template v-else-if=\"card.kind === 'remove'\">")));
  assert.ok(card.includes("Keep it") && card.includes('@click="toolboxRemove(card)">Remove</button>'));
  assert.ok(read("./components/TokenStats.vue").includes('route === "toolbox"'));
});

test("Context use keeps its button and category words whole at the drawer's width (F172)", () => {
  const panel = read("./components/ContextPanel.vue");
  assert.ok(panel.includes(".context-head .secondary-button { flex: none; white-space: nowrap; }"));
  assert.ok(panel.includes(".context-panel tbody th { overflow-wrap: normal; word-break: normal; }"));
  assert.ok(panel.includes('<th scope="col" title="Share of the context window">Share</th>'));
});
