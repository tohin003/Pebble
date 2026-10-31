// Phase 46 (1.9.29, D195, w25-genui-b): Pebble lays out a diagram itself, so a model (or a mermaid block) gives only
// nodes and edges. A small layered layout, no dependency:
// - flow: cycles broken where a depth-first walk meets its own path (those edges are drawn back along the side), each
//   node one row below its longest chain of predecessors, rows ordered by their parents' positions (barycentres), and
//   at most MAX_ROW boxes in a row (a wider row wraps, as the design's complexity budget asks);
// - layers: one band per group, in the order the groups first appear, each band at most MAX_ROW boxes in a row;
// - sequence: one lifeline per participant and one row per message or note, in order.
// Coordinates are SVG user units; text is wrapped by an estimate of its width, never measured in a DOM.
import type { Diagram, DiagramEdge, DiagramNode } from "./visuals";

export const MAX_ROW = 4;
const CHAR = 7.1, MARGIN = 16, LINE = 17;

export interface PlacedNode {
  id: string;
  lines: string[];
  label: string;
  sub: string;
  shape: "box" | "round" | "decision";
  tone: string;
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface PlacedEdge {
  path: string;
  label: string;
  lx: number;
  ly: number;
  dashed: boolean;
  back: boolean;
}
export interface Band {
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface PlacedMessage {
  x1: number;
  x2: number;
  y: number;
  label: string;
  dashed: boolean;
  self: boolean;
  note: boolean;
  width: number;
}
export interface DiagramLayout {
  width: number;
  height: number;
  nodes: PlacedNode[];
  edges: PlacedEdge[];
  bands: Band[];
  lifelines: { x: number; y1: number; y2: number }[];
  messages: PlacedMessage[];
}

const TONES = new Set(["neutral", "accent", "ok", "warn", "danger"]);

/** Words wrapped into at most `rows` lines of `chars` characters; the last line ends in "…" when words are left. */
export function wrap(text: string, chars: number, rows = 2): string[] {
  const words = text.split(/\s+/).filter(Boolean), lines: string[] = [];
  let current = "";
  for (const word of words) {
    const next = current ? current + " " + word : word;
    if (next.length <= chars) current = next;
    else {
      if (current) lines.push(current);
      current = word.length > chars ? word.slice(0, chars - 1) + "…" : word;
    }
  }
  if (current) lines.push(current);
  if (lines.length > rows) {
    const kept = lines.slice(0, rows);
    kept[rows - 1] = (kept[rows - 1].length >= chars ? kept[rows - 1].slice(0, chars - 1) : kept[rows - 1]) + "…";
    return kept;
  }
  return lines.length ? lines : [""];
}

function box(node: DiagramNode, width: number): Omit<PlacedNode, "x" | "y" | "height"> {
  const chars = Math.max(8, Math.floor((width - 24) / CHAR));
  return {
    id: node.id, label: node.label, lines: wrap(node.label, chars), sub: node.sub ? wrap(node.sub, chars + 2, 1)[0] : "",
    shape: node.shape || "box", tone: TONES.has(node.tone || "") ? (node.tone as string) : "neutral", width,
  };
}

function nodeWidth(nodes: DiagramNode[], low = 112, high = 184): number {
  const longest = Math.max(...nodes.map((n) => Math.max(Math.min(n.label.length, 26), n.sub ? Math.min(n.sub.length, 26) : 0)));
  return Math.round(Math.min(high, Math.max(low, longest * CHAR + 28)));
}

function nodeHeight(placed: Omit<PlacedNode, "x" | "y" | "height">[]): number {
  const lines = Math.max(1, ...placed.map((n) => n.lines.length));
  return 20 + lines * LINE + (placed.some((n) => n.sub) ? 15 : 0);
}

/** Depth-first walk in the nodes' own order: edges that reach a node on the current path are back edges. */
export function backEdges(ids: string[], edges: DiagramEdge[]): Set<number> {
  const out = new Map(ids.map((id) => [id, [] as number[]]));
  edges.forEach((edge, index) => out.get(edge.from)?.push(index));
  const state = new Map<string, 1 | 2>(), back = new Set<number>();
  for (const root of ids) {
    if (state.has(root)) continue;
    const stack: [string, number][] = [[root, 0]];
    state.set(root, 1);
    while (stack.length) {
      const top = stack[stack.length - 1];
      const list = out.get(top[0]) || [];
      if (top[1] >= list.length) {
        state.set(top[0], 2);
        stack.pop();
        continue;
      }
      const index = list[top[1]++], to = edges[index].to;
      if (to === top[0] || state.get(to) === 1) back.add(index);
      else if (!state.has(to)) {
        state.set(to, 1);
        stack.push([to, 0]);
      }
    }
  }
  return back;
}

/** Each node's row: one below its longest chain of predecessors along forward edges (Kahn's order). */
export function layers(ids: string[], edges: DiagramEdge[], back: Set<number>): Map<string, number> {
  const layer = new Map(ids.map((id) => [id, 0])), incoming = new Map(ids.map((id) => [id, 0]));
  const forward = edges.filter((_, index) => !back.has(index));
  forward.forEach((edge) => incoming.set(edge.to, (incoming.get(edge.to) || 0) + 1));
  const queue = ids.filter((id) => !incoming.get(id));
  for (let head = 0; head < queue.length; head++) {
    const id = queue[head];
    for (const edge of forward.filter((e) => e.from === id)) {
      layer.set(edge.to, Math.max(layer.get(edge.to) || 0, (layer.get(id) || 0) + 1));
      incoming.set(edge.to, (incoming.get(edge.to) || 0) - 1);
      if (!incoming.get(edge.to)) queue.push(edge.to);
    }
  }
  return layer;
}

function curve(x1: number, y1: number, x2: number, y2: number, vertical: boolean) {
  const bend = vertical ? (y2 - y1) / 2 : (x2 - x1) / 2;
  const c = vertical ? [x1, y1 + bend, x2, y2 - bend] : [x1 + bend, y1, x2 - bend, y2];
  // the curve's midpoint, B(1/2) = (P0 + 3 P1 + 3 P2 + P3) / 8, carries the label
  return {
    path: `M ${r(x1)} ${r(y1)} C ${r(c[0])} ${r(c[1])} ${r(c[2])} ${r(c[3])} ${r(x2)} ${r(y2)}`,
    lx: (x1 + 3 * c[0] + 3 * c[2] + x2) / 8, ly: (y1 + 3 * c[1] + 3 * c[3] + y2) / 8,
  };
}
const r = (value: number) => Math.round(value * 10) / 10;

function route(nodes: Map<string, PlacedNode>, edges: DiagramEdge[], back: Set<number>, vertical: boolean, right: number) {
  let lane = 0;
  return edges.map((edge, index): PlacedEdge => {
    const a = nodes.get(edge.from) as PlacedNode, b = nodes.get(edge.to) as PlacedNode;
    const base = { label: edge.label, dashed: !!edge.dashed };
    if (edge.from === edge.to) {
      const x = a.x + a.width, y = a.y + a.height / 2;
      return { ...base, back: true, path: `M ${r(x)} ${r(y - 8)} C ${r(x + 28)} ${r(y - 22)} ${r(x + 28)} ${r(y + 22)} ${r(x)} ${r(y + 8)}`, lx: x + 30, ly: y };
    }
    const forward = vertical ? b.y > a.y + a.height / 2 : b.x > a.x + a.width / 2;
    if (back.has(index) || !forward) {
      // drawn back along the outside edge, in its own lane, so it never crosses a box
      const out = right + 14 + 10 * lane++;
      if (vertical) {
        const y1 = a.y + a.height / 2, y2 = b.y + b.height / 2, x1 = a.x + a.width, x2 = b.x + b.width;
        return { ...base, back: true, path: `M ${r(x1)} ${r(y1)} L ${r(out)} ${r(y1)} L ${r(out)} ${r(y2)} L ${r(x2)} ${r(y2)}`, lx: out, ly: (y1 + y2) / 2 };
      }
      const x1 = a.x + a.width / 2, x2 = b.x + b.width / 2, y1 = a.y + a.height, y2 = b.y + b.height;
      return { ...base, back: true, path: `M ${r(x1)} ${r(y1)} L ${r(x1)} ${r(out)} L ${r(x2)} ${r(out)} L ${r(x2)} ${r(y2)}`, lx: (x1 + x2) / 2, ly: out };
    }
    const c = vertical
      ? curve(a.x + a.width / 2, a.y + a.height, b.x + b.width / 2, b.y, true)
      : curve(a.x + a.width, a.y + a.height / 2, b.x, b.y + b.height / 2, false);
    return { ...base, back: false, ...c };
  });
}

function empty(): DiagramLayout {
  return { width: 0, height: 0, nodes: [], edges: [], bands: [], lifelines: [], messages: [] };
}

/** A flowchart: rows top to bottom ("down") or columns left to right ("right"). */
export function layoutFlow(diagram: Diagram): DiagramLayout {
  const ids = diagram.nodes.map((n) => n.id), vertical = diagram.direction !== "right";
  const back = backEdges(ids, diagram.edges), layer = layers(ids, diagram.edges, back);
  const depth = Math.max(0, ...layer.values());
  const ranks: string[][] = Array.from({ length: depth + 1 }, () => []);
  ids.forEach((id) => ranks[layer.get(id) || 0].push(id));
  // order each rank by its parents' mean position, down then up, twice; ties keep the nodes' own order
  const forward = diagram.edges.filter((e, index) => !back.has(index) && e.from !== e.to);
  const position = new Map<string, number>();
  const place = () => ranks.forEach((rank) => rank.forEach((id, i) => position.set(id, i)));
  place();
  for (let sweep = 0; sweep < 4; sweep++) {
    const down = sweep % 2 === 0;
    for (let k = down ? 1 : depth - 1; down ? k <= depth : k >= 0; k += down ? 1 : -1) {
      const key = (id: string) => {
        const near = forward.filter((e) => (down ? e.to === id : e.from === id)).map((e) => position.get(down ? e.from : e.to) as number);
        return near.length ? near.reduce((a, b) => a + b, 0) / near.length : (position.get(id) as number);
      };
      const keys = new Map(ranks[k].map((id) => [id, key(id)]));
      ranks[k].sort((a, b) => (keys.get(a) as number) - (keys.get(b) as number) || ids.indexOf(a) - ids.indexOf(b));
      place();
    }
  }
  const rows = ranks.flatMap((rank) => Array.from({ length: Math.ceil(rank.length / MAX_ROW) }, (_, i) => rank.slice(i * MAX_ROW, (i + 1) * MAX_ROW)));
  const width = nodeWidth(diagram.nodes), boxes = new Map(diagram.nodes.map((n) => [n.id, box(n, width)]));
  const height = nodeHeight([...boxes.values()]);
  const across = vertical ? 28 : 22, along = vertical ? 58 : 76;
  const widest = Math.max(...rows.map((row) => row.length));
  const span = widest * (vertical ? width : height) + (widest - 1) * across;
  const placed = new Map<string, PlacedNode>();
  rows.forEach((row, i) => {
    const size = row.length * (vertical ? width : height) + (row.length - 1) * across;
    row.forEach((id, j) => {
      const offset = MARGIN + (span - size) / 2 + j * ((vertical ? width : height) + across);
      const step = MARGIN + i * ((vertical ? height : width) + along);
      placed.set(id, { ...(boxes.get(id) as PlacedNode), height, x: vertical ? offset : step, y: vertical ? step : offset });
    });
  });
  const extent = MARGIN + rows.length * ((vertical ? height : width) + along) - along;
  const right = vertical ? MARGIN + span : extent;
  const edges = route(placed, diagram.edges, back, vertical, right);
  const lanes = edges.filter((e) => e.back).length;
  return {
    ...empty(), nodes: ids.map((id) => placed.get(id) as PlacedNode), edges,
    width: Math.ceil((vertical ? MARGIN + span : extent) + MARGIN + (vertical && lanes ? 14 + 10 * lanes + 24 : 0)),
    height: Math.ceil((vertical ? extent : MARGIN + span) + MARGIN + (!vertical && lanes ? 14 + 10 * lanes : 0)),
  };
}

/** Layers: one band per group, top to bottom, at most MAX_ROW boxes in a row inside each band. */
export function layoutLayers(diagram: Diagram): DiagramLayout {
  const groups = [...new Set(diagram.nodes.map((n) => n.group || ""))];
  const width = nodeWidth(diagram.nodes, 104, 168), boxes = new Map(diagram.nodes.map((n) => [n.id, box(n, width)]));
  const height = nodeHeight([...boxes.values()]), gap = 20, labelColumn = 112, pad = 14;
  const widest = Math.min(MAX_ROW, Math.max(...groups.map((g) => diagram.nodes.filter((n) => (n.group || "") === g).length)));
  const inner = widest * width + (widest - 1) * gap;
  const bandWidth = labelColumn + inner + 2 * pad;
  const placed = new Map<string, PlacedNode>(), bands: Band[] = [];
  let y = MARGIN;
  for (const group of groups) {
    const members = diagram.nodes.filter((n) => (n.group || "") === group);
    const rows = Math.ceil(members.length / MAX_ROW);
    const bandHeight = rows * height + (rows - 1) * gap + 2 * pad;
    bands.push({ label: group, x: MARGIN, y, width: bandWidth, height: bandHeight });
    members.forEach((node, i) => {
      const row = Math.floor(i / MAX_ROW), inRow = Math.min(MAX_ROW, members.length - row * MAX_ROW);
      const size = inRow * width + (inRow - 1) * gap;
      const x = MARGIN + labelColumn + pad + (inner - size) / 2 + (i % MAX_ROW) * (width + gap);
      placed.set(node.id, { ...(boxes.get(node.id) as PlacedNode), height, x, y: y + pad + row * (height + gap) });
    });
    y += bandHeight + 12;
  }
  const edges = diagram.edges.map((edge): PlacedEdge => {
    const a = placed.get(edge.from) as PlacedNode, b = placed.get(edge.to) as PlacedNode;
    const base = { label: edge.label, dashed: !!edge.dashed, back: false };
    if (edge.from === edge.to) return { ...base, path: "", lx: a.x, ly: a.y };
    if (Math.abs(a.y - b.y) < 1) {
      const leftToRight = a.x < b.x;
      return { ...base, ...curve(leftToRight ? a.x + a.width : a.x, a.y + a.height / 2, leftToRight ? b.x : b.x + b.width, b.y + b.height / 2, false) };
    }
    return b.y > a.y
      ? { ...base, ...curve(a.x + a.width / 2, a.y + a.height, b.x + b.width / 2, b.y, true) }
      : { ...base, ...curve(a.x + a.width / 2, a.y, b.x + b.width / 2, b.y + b.height, true) };
  }).filter((edge) => edge.path);
  return { ...empty(), nodes: diagram.nodes.map((n) => placed.get(n.id) as PlacedNode), edges, bands,
    width: Math.ceil(MARGIN * 2 + bandWidth), height: Math.ceil(y - 12 + MARGIN) };
}

/** A sequence: participants across the top, one row per message or note, in order. */
export function layoutSequence(diagram: Diagram): DiagramLayout {
  const count = diagram.nodes.length;
  const width = nodeWidth(diagram.nodes, 96, count > 5 ? 120 : 150), gap = 36, head = 40;
  const boxes = diagram.nodes.map((node, i) => ({ ...box(node, width), x: MARGIN + i * (width + gap), y: MARGIN, height: head }));
  const centre = new Map(boxes.map((b) => [b.id, b.x + width / 2]));
  let y = MARGIN + head + 30;
  const messages = diagram.edges.map((edge): PlacedMessage => {
    const x1 = centre.get(edge.from) as number, x2 = centre.get(edge.to) as number;
    const self = edge.from === edge.to && !edge.note;
    const span = edge.note ? Math.max(Math.abs(x2 - x1) + width * 0.8, width) : Math.abs(x2 - x1);
    const placed = { x1, x2, y, label: edge.label, dashed: !!edge.dashed, self, note: !!edge.note, width: span };
    y += edge.note ? 44 : self ? 52 : 40;
    return placed;
  });
  const bottom = y;
  return {
    ...empty(), nodes: boxes.map((b) => ({ ...b, lines: wrap(b.label, Math.floor((width - 16) / CHAR), 1) })), messages,
    lifelines: boxes.map((b) => ({ x: b.x + width / 2, y1: MARGIN + head, y2: bottom })),
    width: Math.ceil(MARGIN * 2 + count * width + (count - 1) * gap + 40), height: Math.ceil(bottom + MARGIN),
  };
}

export function layout(diagram: Diagram): DiagramLayout {
  if (!diagram.nodes.length) return empty();
  if (diagram.layout === "sequence") return layoutSequence(diagram);
  if (diagram.layout === "layers") return layoutLayers(diagram);
  return layoutFlow(diagram);
}
