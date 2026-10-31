<script setup lang="ts">
// Phase 46 (1.9.29, D195, w25-genui-b): a reply drawn with Pebble's free upgrades, always on and at no model cost (the
// stored text never changes): a Markdown table becomes a sortable native table with a chart toggle, a mermaid block in
// the bounded subset becomes a native diagram (its source one click away), and a short numbered list becomes a steps
// view, numbered and without states (Pebble does not track a reply's steps). Everything else, and any reply with none
// of these, goes to Markdown.vue exactly as before. Parsed at most once per animation frame while a reply streams.
import { onUnmounted, ref, watch } from "vue";
import Markdown, { perFrame } from "./Markdown.vue";
import ReplyTable from "./ReplyTable.vue";
import VisualDiagram from "./VisualDiagram.vue";
import { replySegments, type Segment } from "../visuals";

const props = defineProps<{ text: string }>();
type Upgraded = Exclude<Segment, { type: "markdown" }>;
type Part = { key: string } & ({ type: "markdown"; text: string } | Upgraded);
const parts = ref<Part[]>([]);

function render() {
  try {
    const segments = replySegments(props.text);
    parts.value = segments.every((s) => s.type === "markdown")
      ? [{ key: "all", type: "markdown", text: props.text }] // nothing to upgrade: the reply's own text, as before
      : segments.map((segment, index): Part => segment.type === "markdown"
        ? { key: index + "-markdown", type: "markdown", text: segment.tokens.map((token) => token.raw).join("") }
        : { key: index + "-" + segment.type, ...segment });
  } catch {
    parts.value = [{ key: "all", type: "markdown", text: props.text }];
  }
}
render();
const frame = perFrame(render);
watch(() => props.text, () => frame.request());
onUnmounted(() => frame.cancel());
</script>

<template>
  <div class="reply-body">
    <template v-for="part in parts" :key="part.key">
      <Markdown v-if="part.type === 'markdown'" :text="part.text" />
      <ReplyTable v-else-if="part.type === 'table'" :table="part.table" />
      <div v-else-if="part.type === 'diagram'" class="reply-diagram">
        <VisualDiagram :diagram="part.diagram" title="Diagram from the reply" />
        <details class="source">
          <summary>Diagram source</summary>
          <pre><code>{{ part.source }}</code></pre>
        </details>
      </div>
      <ol v-else-if="part.type === 'steps'" class="reply-steps" role="list" aria-label="Steps">
        <li v-for="(item, index) in part.items" :key="index">
          <span class="number" aria-hidden="true">{{ index + 1 }}</span>
          <Markdown :text="item" />
        </li>
      </ol>
    </template>
  </div>
</template>

<style scoped>
.reply-body {
  min-width: 0;
}
.reply-diagram {
  margin: 1em 0;
  padding: var(--space-3);
  border: 0.5px solid var(--line-strong);
  border-radius: var(--radius-lg);
  background: var(--panel);
}
.source {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
}
.source summary {
  cursor: pointer;
  color: var(--secondary);
}
.source summary:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.source pre {
  overflow: auto;
  padding: var(--space-2) var(--space-3);
  background: var(--code);
  border-radius: var(--radius-md);
  font-size: 12px;
}
.reply-steps {
  list-style: none;
  margin: 1em 0;
  padding: 0;
}
.reply-steps li {
  position: relative;
  display: flex;
  gap: var(--space-3);
  padding: 0 0 var(--space-3);
}
/* the rail between numbers */
.reply-steps li:not(:last-child)::before {
  content: "";
  position: absolute;
  left: 11px;
  top: 24px;
  bottom: 2px;
  width: 1px;
  background: var(--line-strong);
}
.number {
  flex: 0 0 auto;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--accent);
  border: 1px solid var(--accent);
  background: var(--panel);
  font-variant-numeric: tabular-nums;
}
.reply-steps li :deep(.markdown) {
  min-width: 0;
  padding-top: 1px;
}
.reply-steps li :deep(.markdown p) {
  margin: 0;
}
</style>
