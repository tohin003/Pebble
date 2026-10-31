<script setup lang="ts">
import { computed, ref } from "vue";
import { formatDuration, formatOffset } from "../activity";
import { cardTitle, commandWords, diffWords, round, STATE_WORDS, testWords, type Card, type View } from "../workView";

// Phase 46 (1.9.29, D206): one card of the work view, drawn from host-built views (workView.parseView checked them).
// Display only: every string is text, never a link, markup or a button's label; the card has no route and no button
// that acts (its only buttons show more of what it already holds). Colour carries meaning: --ok passed or done,
// --danger failed, --warn waiting or open; the words say the same for a screen reader and in either theme.
const props = withDefaults(defineProps<{ card: Card; running?: boolean; reduced?: boolean }>(), { running: false, reduced: false });
const SHOWN = 6; // the complexity budget: six chips or rows before "Show more"
const all = ref(false);
const views = computed(() => props.card.views);
const first = computed(() => views.value[0]);
const title = computed(() => cardTitle(props.card));
const item = computed(() => props.card.items[props.card.items.length - 1]);
const live = computed(() => props.running && props.card.items.some((i) => i.status === "running"));
const failed = computed(() => props.card.items.some((i) => i.status === "failed"));
const str = (v: unknown) => (typeof v === "string" ? v : "");
const num = (v: unknown) => (typeof v === "number" ? v : 0);
const limited = <T,>(list: readonly T[]) => (all.value ? list : list.slice(0, SHOWN));
// status chip: the view's own state when it has one, else the item's
const tone = (state: string) => (state === "done" || state === "met" ? "ok" : state === "failed" || state === "declined" ? "bad" : state === "waiting" || state === "open" || state === "held" ? "warn" : "neutral");
const status = computed(() => {
  const st = str(first.value.state);
  if (props.card.kind === "command") return { word: commandWords(first.value), tone: tone(st) };
  if (props.card.kind === "tests") return { word: STATE_WORDS[st] || "", tone: tone(st) };
  if (props.card.kind === "checks") return { word: num(first.value.unmet) ? "Not all met" : (first.value.requirements as unknown[] | undefined)?.length ? "All met" : "", tone: num(first.value.unmet) ? "warn" : "ok" };
  if (live.value) return { word: "In progress", tone: "neutral" };
  if (failed.value) return { word: "Failed", tone: "bad" };
  return { word: "", tone: "neutral" };
});
// files: one row per path, its counts summed; a bar whose length follows the largest change
const files = computed(() => {
  const byPath = new Map<string, { path: string; added: number; removed: number; state: string; isNew: boolean; hidden: boolean }>();
  views.value.forEach((v, i) => {
    const key = str(v.path) || "private-" + i;
    const old = byPath.get(key);
    byPath.set(key, { path: str(v.path) || "A private file", added: (old?.added || 0) + num(v.added), removed: (old?.removed || 0) + num(v.removed), state: str(v.state), isNew: v.new === true, hidden: !str(v.path) });
  });
  return [...byPath.values()];
});
const diff = computed(() => diffWords(views.value));
const widest = computed(() => Math.max(1, ...files.value.map((f) => f.added + f.removed)));
const bar = (count: number) => Math.max(count ? 4 : 0, Math.round((count / widest.value) * 64)) + "px";
const chips = computed(() =>
  views.value.map((v) =>
    v.type === "read" ? { text: v.private ? "A private file" : str(v.path), note: typeof v.total === "number" ? `${round(num(v.total))} lines` : "" }
      : { text: str(v.query) || "Search", note: typeof v.hits === "number" ? `${num(v.hits)} found` : "" }),
);
const sites = computed(() => [...new Set(views.value.flatMap((v) => (Array.isArray(v.sites) ? (v.sites as string[]) : [])))]);
const queries = computed(() => views.value.map((v) => str(v.query)).filter(Boolean));
// tests: the bar's segments, by count
const segments = computed(() => {
  const v = first.value, total = Math.max(1, num(v.passed) + num(v.failed) + num(v.skipped) + num(v.errors));
  return [["ok", num(v.passed)], ["bad", num(v.failed) + num(v.errors)], ["muted", num(v.skipped)]]
    .filter(([, count]) => (count as number) > 0)
    .map(([kind, count]) => ({ kind: kind as string, width: ((count as number) / total) * 100 + "%" }));
});
const failing = computed(() => (Array.isArray(first.value.failing) ? (first.value.failing as string[]) : []));
const tail = computed(() => (Array.isArray(first.value.tail) ? (first.value.tail as string[]) : []));
// window: the press point inside the observed picture's frame, when both are known
const frame = computed(() => {
  const v = [...views.value].reverse().find((w) => w.picture && w.point) as View | undefined;
  const picture = v?.picture as { width?: number; height?: number } | undefined, point = v?.point as { x?: number; y?: number } | undefined;
  if (!picture?.width || !picture?.height || typeof point?.x !== "number" || typeof point?.y !== "number") return null;
  return { width: picture.width, height: picture.height, x: Math.min(point.x, picture.width), y: Math.min(point.y, picture.height) };
});
const VERDICTS: Record<string, string> = { happened: "Happened", nothing_changed: "Nothing changed", uncertain: "Not sure", refused: "Not done" };
const requirements = computed(() => (Array.isArray(first.value.requirements) ? (first.value.requirements as { text: string; state: string }[]) : []));
const checkTests = computed(() => first.value.tests as { runner?: string; passed?: number; failed?: number; current?: boolean } | undefined);
const more = computed(() => (props.card.kind === "explored" ? chips.value.length : props.card.kind === "files" ? files.value.length : props.card.kind === "window" || props.card.kind === "agents" ? views.value.length : 0) - SHOWN);
</script>

<template>
  <article class="work-card" :class="[card.kind, { live, failed, reduced }]" :aria-label="title">
    <header class="card-head">
      <h4 class="card-title" :class="{ mono: card.kind === 'command' }">
        <span v-if="card.kind === 'command'" class="prompt" aria-hidden="true">$ </span>{{ title }}
      </h4>
      <span v-if="card.kind === 'files'" class="diff-words"><span class="plus">+{{ diff.added }}</span> <span class="minus">−{{ diff.removed }}</span></span>
      <span v-if="status.word" class="card-status" :class="status.tone">{{ status.word }}</span>
      <span class="card-time">{{ formatOffset(card.items[0].at) }}</span>
    </header>

    <!-- Reading and searching -->
    <ul v-if="card.kind === 'explored'" class="chips" aria-label="Files and searches">
      <li v-for="(chip, i) in limited(chips)" :key="i" class="chip"><span class="chip-text">{{ chip.text }}</span><span v-if="chip.note" class="chip-note">{{ chip.note }}</span></li>
    </ul>
    <template v-else-if="card.kind === 'web'">
      <p v-if="queries.length" class="card-line">{{ queries.join(" · ") }}</p>
      <ul v-if="sites.length" class="chips" aria-label="Sites"><li v-for="site in sites" :key="site" class="chip"><span class="chip-text">{{ site }}</span></li></ul>
    </template>

    <!-- Changed files -->
    <ul v-else-if="card.kind === 'files'" class="file-rows" aria-label="Changed files">
      <li v-for="file in limited(files)" :key="file.path" class="file-row">
        <span class="file-path" :class="{ muted: file.hidden }">{{ file.path }}<span v-if="file.isNew" class="file-new"> · new</span></span>
        <span class="file-bar" role="img" :aria-label="`${file.added} lines added, ${file.removed} removed`"
          ><span class="seg ok" :style="{ width: bar(file.added) }"></span><span class="seg bad" :style="{ width: bar(file.removed) }"></span
        ></span>
        <span class="file-count"><span class="plus">+{{ file.added }}</span> <span class="minus">−{{ file.removed }}</span></span>
        <span class="file-state" :class="tone(file.state)">{{ STATE_WORDS[file.state] || "" }}</span>
      </li>
    </ul>

    <!-- A command -->
    <template v-else-if="card.kind === 'command'">
      <p class="card-line muted">{{ [first.where, first.network === true ? "with network" : first.network === false ? "no network" : ""].filter(Boolean).join(" · ") }}</p>
      <pre v-if="tail.length" class="tail" aria-label="Last lines of output">{{ tail.join("\n") }}</pre>
      <p v-if="first.cut" class="card-line muted">The output was longer; these are its last lines.</p>
    </template>

    <!-- Tests -->
    <template v-else-if="card.kind === 'tests'">
      <p class="card-line">{{ testWords(first) }}<span v-if="typeof first.seconds === 'number'" class="muted"> · {{ formatDuration(first.seconds as number) }}</span></p>
      <span v-if="segments.length" class="meter" role="img" :aria-label="testWords(first)"><span v-for="(seg, i) in segments" :key="i" class="seg" :class="seg.kind" :style="{ width: seg.width }"></span></span>
      <ul v-if="failing.length" class="failing" aria-label="Failing tests"><li v-for="name in failing" :key="name" class="mono">{{ name }}</li></ul>
    </template>

    <!-- A Git step -->
    <template v-else-if="card.kind === 'git'">
      <ul class="chips" aria-label="Git">
        <li v-if="first.branch" class="chip"><span class="chip-text">{{ first.branch }}</span></li>
        <li v-if="first.commit" class="chip mono"><span class="chip-text">{{ first.commit }}</span></li>
        <li v-if="typeof first.ahead === 'number'" class="chip"><span class="chip-text">{{ first.ahead }} ahead · {{ first.behind ?? 0 }} behind</span></li>
        <li v-if="typeof first.changed === 'number'" class="chip"><span class="chip-text">{{ first.changed }} changed</span></li>
        <li v-if="first.push_to" class="chip"><span class="chip-text">Push to {{ first.push_to }}</span></li>
      </ul>
      <p v-if="first.destructive || first.publication" class="card-line warn">{{ [first.destructive ? "Changes history" : "", first.publication ? "Publishes" : ""].filter(Boolean).join(" · ") }}</p>
      <p v-if="first.state" class="card-line" :class="tone(String(first.state))">{{ STATE_WORDS[String(first.state)] }}</p>
    </template>

    <!-- An app window -->
    <template v-else-if="card.kind === 'window'">
      <svg v-if="frame" class="frame" :viewBox="`0 0 ${frame.width} ${frame.height}`" role="img" aria-label="Where Pebble pressed in the window">
        <rect x="0" y="0" :width="frame.width" :height="frame.height" rx="6" />
        <circle :cx="frame.x" :cy="frame.y" :r="Math.max(6, Math.min(frame.width, frame.height) / 18)" />
      </svg>
      <ol class="steps" aria-label="Steps in the window">
        <li v-for="(v, i) in limited(views)" :key="i">
          <span>{{ v.verb }}<template v-if="v.target"> “{{ v.target }}”</template></span>
          <span v-if="v.before !== undefined || v.after !== undefined" class="muted"> {{ v.before ?? "" }} → {{ v.after ?? "" }}</span>
          <span v-if="v.verdict" class="step-verdict" :class="v.verdict === 'happened' ? 'ok' : 'warn'">{{ VERDICTS[String(v.verdict)] }}</span>
        </li>
      </ol>
    </template>

    <!-- Agents -->
    <ul v-else-if="card.kind === 'agents'" class="agents" aria-label="Agents">
      <li v-for="(v, i) in limited(views)" :key="i" :style="{ paddingLeft: Math.min(3, Math.max(0, num(v.depth) - 1)) * 14 + 'px' }">
        <span class="agent-name">{{ v.name }}</span>
        <span class="file-state" :class="tone(String(v.state))">{{ STATE_WORDS[String(v.state)] }}</span>
        <span class="muted">{{ [typeof v.seconds === "number" ? formatDuration(v.seconds as number) : "", typeof v.tokens === "number" && v.tokens ? round(v.tokens as number) + " tokens" : ""].filter(Boolean).join(" · ") }}</span>
      </li>
    </ul>

    <!-- AgentGuard's checks -->
    <template v-else-if="card.kind === 'checks'">
      <p v-if="first.verdict" class="card-line" :class="String(first.verdict).startsWith('Verified') ? 'ok' : 'warn'">{{ first.verdict }}</p>
      <p v-if="checkTests" class="card-line muted">
        {{ checkTests.runner }} {{ checkTests.passed }} passed<template v-if="checkTests.failed"> · {{ checkTests.failed }} failed</template>{{ checkTests.current ? " on current files" : "" }}
      </p>
      <ul v-if="requirements.length" class="requirements" aria-label="What your request requires">
        <li v-for="(req, i) in requirements" :key="i" :class="req.state">
          <span class="req-mark" role="img" :aria-label="req.state === 'met' ? 'Met' : 'Not confirmed yet'"></span>{{ req.text }}
        </li>
      </ul>
      <p v-if="first.repaired" class="card-line muted">Met after Pebble repaired its work.</p>
      <p v-if="num(first.unmet)" class="card-line warn">{{ num(first.unmet) }} {{ num(first.unmet) === 1 ? "issue" : "issues" }} not met yet; the reply says which.</p>
    </template>

    <button v-if="more > 0" type="button" class="card-more" :aria-expanded="all" @click="all = !all">{{ all ? "Show less" : `Show ${more} more` }}</button>
    <p v-if="item.detail && (card.kind === 'command' || card.kind === 'tests') && item.status === 'failed'" class="card-line muted">{{ item.detail }}</p>
  </article>
</template>

<style scoped>
.work-card {
  display: grid;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--panel);
  font-size: var(--text-sm);
  color: var(--secondary);
  min-width: 0;
}
.work-card.failed {
  border-color: var(--danger-line);
}
.card-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.card-title {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: var(--text-md);
  font-weight: 560;
  color: var(--text);
  overflow-wrap: anywhere;
}
.mono,
.tail {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: var(--text-sm);
}
.prompt {
  color: var(--muted);
}
.card-status,
.file-state,
.step-verdict {
  font-size: var(--text-xs);
  white-space: nowrap;
}
.card-time {
  color: var(--muted);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.ok {
  color: var(--ok);
}
.bad {
  color: var(--danger);
}
.warn {
  color: var(--warn);
}
.muted,
.neutral {
  color: var(--muted);
}
.card-line {
  margin: 0;
  overflow-wrap: anywhere;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.chip {
  display: inline-flex;
  gap: 6px;
  max-width: 100%;
  padding: 2px 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--soft);
  color: var(--text);
}
.chip-text {
  overflow-wrap: anywhere;
}
.chip-note {
  color: var(--muted);
  white-space: nowrap;
}
.file-rows,
.failing,
.steps,
.agents,
.requirements {
  display: grid;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.file-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 68px auto auto;
  align-items: center;
  gap: 8px;
}
.file-path {
  color: var(--text);
  overflow-wrap: anywhere;
}
.file-new {
  color: var(--muted);
}
.file-bar,
.meter {
  display: flex;
  gap: 1px;
  height: 6px;
  border-radius: 3px;
  overflow: hidden;
}
.meter {
  width: 100%;
  background: var(--line);
}
.seg {
  display: block;
  height: 100%;
}
.seg.ok {
  background: var(--ok);
}
.seg.bad {
  background: var(--danger);
}
.seg.muted {
  background: var(--line-strong);
}
.file-count,
.diff-words {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.plus {
  color: var(--ok);
}
.minus {
  color: var(--danger);
}
.tail {
  margin: 0;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--code);
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 160px;
  overflow: auto;
}
.frame {
  width: 100%;
  max-width: 240px;
  max-height: 160px;
}
.frame rect {
  fill: var(--soft);
  stroke: var(--line-strong);
}
.frame circle {
  fill: none;
  stroke: var(--accent);
  stroke-width: 3;
  vector-effect: non-scaling-stroke;
}
.steps li,
.agents li {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: baseline;
}
.agent-name {
  color: var(--text);
}
.requirements li {
  display: flex;
  gap: 8px;
  align-items: baseline;
  color: var(--text);
  overflow-wrap: anywhere;
}
.req-mark {
  flex: none;
  width: 10px;
  height: 10px;
  border-radius: 3px;
  border: 1.5px solid var(--warn);
  transform: translateY(1px);
}
.requirements li.met .req-mark {
  border-color: var(--ok);
  background: var(--ok);
}
.card-more {
  justify-self: start;
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  color: var(--secondary);
  font-size: var(--text-xs);
}
.card-more:hover {
  background: var(--hover);
  color: var(--text);
}
.card-more:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
.live .card-title::after {
  content: "";
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-left: 8px;
  border-radius: 50%;
  background: var(--accent);
  vertical-align: middle;
  animation: card-breathe 2s ease-in-out infinite alternate;
}
@keyframes card-breathe {
  from {
    opacity: 0.45;
  }
  to {
    opacity: 1;
  }
}
.reduced.live .card-title::after {
  animation: none;
}
@media (prefers-reduced-motion: reduce) {
  .live .card-title::after {
    animation: none;
  }
}
</style>
