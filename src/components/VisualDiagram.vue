<script setup lang="ts">
// Phase 46 (1.9.29, D195, w25-genui-b): a diagram Pebble lays out and draws itself (diagramLayout.ts): a flowchart,
// layers or a sequence, from a model's diagram view or a reply's mermaid block. Two hues at most: the accent and the
// neutral line; ok, warn and danger only where a node's tone says so. Every word is a text node, and the whole diagram
// is also a list in reading order under "Diagram as text".
import { computed } from "vue";
import { layout, type PlacedNode } from "../diagramLayout";
import { diagramText, type Diagram } from "../visuals";

const props = defineProps<{ diagram: Diagram; title: string }>();
// one arrowhead per diagram, so two diagrams in one reply never share an id
const marker = "pebble-arrow-" + Math.random().toString(36).slice(2, 10);
const placed = computed(() => layout(props.diagram));
const words = computed(() => diagramText(props.diagram));
const kind = computed(() => ({ flow: "Flowchart", layers: "Layer diagram", sequence: "Sequence diagram" })[props.diagram.layout] || "Diagram");
const name = computed(() => `${props.title}: ${kind.value.toLowerCase()} with ${props.diagram.nodes.length} ${props.diagram.layout === "sequence" ? "participants" : "boxes"}. Read it as text below.`);
const TONES: Record<string, string> = { neutral: "tone-neutral", accent: "tone-accent", ok: "tone-ok", warn: "tone-warn", danger: "tone-danger" };
const toneOf = (node: PlacedNode) => TONES[node.tone] || "tone-neutral";
function hexagon(node: PlacedNode) {
  const { x, y, width: w, height: h } = node, d = Math.min(14, h / 2);
  return `M ${x + d} ${y} L ${x + w - d} ${y} L ${x + w} ${y + h / 2} L ${x + w - d} ${y + h} L ${x + d} ${y + h} L ${x} ${y + h / 2} Z`;
}
const firstLine = (node: PlacedNode) => node.y + node.height / 2 - ((node.lines.length - 1) * 17) / 2 - (node.sub ? 7 : 0) + 4.5;
</script>

<template>
  <figure class="visual-diagram">
    <figcaption v-if="diagram.caption">{{ diagram.caption }}</figcaption>
    <svg :viewBox="`0 0 ${placed.width} ${placed.height}`" :width="placed.width" role="img" :aria-label="name" focusable="false">
      <defs>
        <marker :id="marker" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 1 L 9 5 L 0 9 z" class="arrowhead" />
        </marker>
      </defs>
      <g v-for="(band, index) in placed.bands" :key="'band' + index" class="band">
        <rect :x="band.x" :y="band.y" :width="band.width" :height="band.height" rx="10" />
        <text :x="band.x + 14" :y="band.y + band.height / 2 + 4">{{ band.label }}</text>
      </g>
      <g v-for="(line, index) in placed.lifelines" :key="'life' + index" class="lifeline">
        <line :x1="line.x" :x2="line.x" :y1="line.y1" :y2="line.y2" />
      </g>
      <g v-for="(edge, index) in placed.edges" :key="'edge' + index" class="edge" :class="{ dashed: edge.dashed, back: edge.back }">
        <path :d="edge.path" :marker-end="`url(#${marker})`" />
        <text v-if="edge.label" :x="edge.lx" :y="edge.ly + 4" :text-anchor="edge.back ? 'start' : 'middle'" :dx="edge.back ? 6 : 0">{{ edge.label }}</text>
      </g>
      <g v-for="(message, index) in placed.messages" :key="'message' + index" class="message" :class="{ dashed: message.dashed, note: message.note }">
        <template v-if="message.note">
          <rect :x="(message.x1 + message.x2) / 2 - message.width / 2" :y="message.y - 15" :width="message.width" height="30" rx="6" />
          <text :x="(message.x1 + message.x2) / 2" :y="message.y + 4" text-anchor="middle">{{ message.label }}</text>
        </template>
        <template v-else-if="message.self">
          <path :d="`M ${message.x1} ${message.y - 6} h 34 v 20 h -32`" :marker-end="`url(#${marker})`" />
          <text :x="message.x1 + 40" :y="message.y + 8">{{ message.label }}</text>
        </template>
        <template v-else>
          <line :x1="message.x1" :x2="message.x2 + (message.x2 > message.x1 ? -2 : 2)" :y1="message.y" :y2="message.y" :marker-end="`url(#${marker})`" />
          <text :x="(message.x1 + message.x2) / 2" :y="message.y - 7" text-anchor="middle">{{ message.label }}</text>
        </template>
      </g>
      <g v-for="node in placed.nodes" :key="node.id" class="node" :class="toneOf(node)">
        <path v-if="node.shape === 'decision'" :d="hexagon(node)" />
        <rect v-else :x="node.x" :y="node.y" :width="node.width" :height="node.height" :rx="node.shape === 'round' ? node.height / 2 : 8" />
        <text :x="node.x + node.width / 2" :y="firstLine(node)" text-anchor="middle">
          <tspan v-for="(line, index) in node.lines" :key="index" :x="node.x + node.width / 2" :dy="index ? 17 : 0">{{ line }}</tspan>
          <tspan v-if="node.sub" class="sub" :x="node.x + node.width / 2" dy="16">{{ node.sub }}</tspan>
        </text>
        <title>{{ node.label }}</title>
      </g>
    </svg>
    <details class="diagram-text">
      <summary>Diagram as text</summary>
      <ol>
        <li v-for="(line, index) in words" :key="index">{{ line }}</li>
      </ol>
    </details>
  </figure>
</template>

<style scoped>
.visual-diagram {
  margin: 0;
  min-width: 0;
}
figcaption {
  color: var(--secondary);
  font-size: var(--text-md);
  margin: 0 0 var(--space-2);
}
svg {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 0 auto;
  font: 13px system-ui, sans-serif;
}
.node rect,
.node path {
  fill: var(--panel);
  stroke: var(--line-strong);
  stroke-width: 1;
}
.node text {
  fill: var(--text);
}
.node .sub {
  fill: var(--secondary);
  font-size: 11px;
}
.tone-accent rect,
.tone-accent path {
  fill: var(--soft);
  stroke: var(--accent);
}
.tone-ok rect,
.tone-ok path {
  stroke: var(--ok);
  stroke-width: 1.5;
}
.tone-warn rect,
.tone-warn path {
  stroke: var(--warn);
  stroke-width: 1.5;
}
.tone-danger rect,
.tone-danger path {
  stroke: var(--danger);
  stroke-width: 1.5;
}
.edge path,
.message line,
.message path {
  fill: none;
  stroke: var(--secondary);
  stroke-width: 1.2;
}
.arrowhead {
  fill: var(--secondary);
}
.dashed path,
.dashed line,
.back path {
  stroke-dasharray: 5 4;
}
.edge text,
.message text {
  fill: var(--secondary);
  font-size: 11px;
  paint-order: stroke;
  stroke: var(--panel);
  stroke-width: 4px;
  stroke-linejoin: round;
}
.message.note rect {
  fill: var(--code);
  stroke: var(--line-strong);
}
.message.note text {
  fill: var(--text);
}
.band rect {
  fill: var(--code);
  stroke: var(--line);
}
.band text {
  fill: var(--secondary);
  font-size: 12px;
  font-weight: 600;
}
.lifeline line {
  stroke: var(--line-strong);
  stroke-dasharray: 3 4;
}
.diagram-text {
  margin-top: var(--space-2);
  font-size: var(--text-sm);
}
.diagram-text summary {
  cursor: pointer;
  color: var(--secondary);
}
.diagram-text summary:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.diagram-text ol {
  margin: var(--space-2) 0 0;
  padding-left: 20px;
}
</style>
