// Phase 46 (1.9.29, D195, w25-genui-b): visual answers. Pure helpers shared by the reply's free upgrades (Markdown.vue)
// and the model's native kinds (NativeArtifactRenderer, VisualChart, VisualDiagram, VisualCatalog):
// - replySegments: a reply's Markdown table becomes a sortable native table, a mermaid block in a bounded subset of
//   flowchart and sequence syntax becomes a native diagram, and a short numbered list becomes a steps view. The reply's
//   text never changes; anything outside the subset stays as it was (a code block, a list);
// - numbers and dates as Pebble formats them (rounded, compact, percent, a unit), never as the model wrote them.
// Nothing here reaches the network, a route or the DOM.
import { marked, type Token, type Tokens } from "marked";

// ---- numbers ----------------------------------------------------------------------------------------------------

export type NumberFormat = "number" | "percent" | "compact";
const CURRENCY = new Set(["$", "€", "£", "¥", "₹"]);

/** A number rounded for reading: no more than 3 significant decimals, and none above 100. */
export function roundFor(value: number): number {
  const size = Math.abs(value);
  const digits = size >= 100 ? 0 : size >= 10 ? 1 : size >= 1 ? 2 : 3;
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

/** Pebble's words for a number: `percent` reads fractions (0.25 is 25%), `compact` shortens (12.5K), a unit follows
 * (a currency sign leads). */
export function formatNumber(value: number, format: NumberFormat = "number", unit = ""): string {
  if (!Number.isFinite(value)) return "—";
  let text: string;
  if (format === "percent") text = roundFor(value * 100).toLocaleString("en-US", { maximumFractionDigits: 3 }) + "%";
  else if (format === "compact")
    text = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value);
  else text = roundFor(value).toLocaleString("en-US", { maximumFractionDigits: 3 });
  if (!unit) return text;
  if (CURRENCY.has(unit)) return text.startsWith("-") ? "-" + unit + text.slice(1) : unit + text;
  return text + (unit === "%" ? "" : " ") + unit;
}

/** "1,200", "-3.5", "12%", "$4.20", "1.2k", "3 ms": the number a table cell holds, or null. */
export function parseNumber(text: string): number | null {
  const match = /^\s*([-+−]?)\s*([$€£¥₹]?)\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*([kKmMbB]|%|[a-zA-Z]{1,4})?\s*$/.exec(text);
  if (!match) return null;
  const [, sign, , whole, fraction = "", suffix = ""] = match;
  let value = Number(whole.replace(/,/g, "") + fraction);
  if (!Number.isFinite(value)) return null;
  // k and K thousands, M millions, B billions; a lone "m" or "b" may be metres or bits, so it stays a unit
  const scale: Record<string, number> = { k: 1e3, K: 1e3, M: 1e6, B: 1e9 };
  if (Object.hasOwn(scale, suffix)) value *= scale[suffix];
  return sign === "-" || sign === "−" ? -value : value;
}

/** Ticks at 1, 2 or 5 times a power of ten that cover [low, high], about `count` of them. */
export function niceTicks(low: number, high: number, count = 5): number[] {
  if (!Number.isFinite(low) || !Number.isFinite(high)) return [0, 1];
  if (low === high) {
    const pad = Math.abs(low) || 1;
    low -= pad / 2;
    high += pad / 2;
    if (low < 0 && low > -pad) low = Math.min(0, low);
  }
  const raw = (high - low) / Math.max(1, count - 1);
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => s >= raw) ?? 10 * power;
  const start = Math.floor(low / step) * step, ticks: number[] = [];
  for (let value = start; value <= high + step * 1e-9 && ticks.length < 12; value += step) ticks.push(Math.round(value / step) * step);
  if (ticks[ticks.length - 1] < high) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

// ---- dates ------------------------------------------------------------------------------------------------------

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
  "November", "December"];

/** An ISO year, month or day ("2026", "2026-10", "2026-10-31") as words, or the text unchanged when it is not one. */
export function formatDate(iso: string): string {
  const match = /^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$/.exec(iso);
  if (!match) return iso;
  const [, year, month, day] = match;
  if (!month) return year;
  const name = MONTHS[Number(month) - 1];
  if (!name) return iso;
  return day ? `${Number(day)} ${name} ${year}` : `${name} ${year}`;
}

// ---- the reply's free upgrades ----------------------------------------------------------------------------------

export interface ReplyCell {
  text: string; // plain words: sorting, numbers and the screen reader's name
  markdown: string; // the cell's own inline Markdown, which the window renders through its sanitizer as before
}
export interface ReplyTable {
  header: ReplyCell[];
  rows: ReplyCell[][];
  align: ("left" | "right" | "center" | null)[];
}
export interface DiagramNode {
  id: string;
  label: string;
  shape?: "box" | "round" | "decision";
  group?: string;
  sub?: string;
  tone?: string;
}
export interface DiagramEdge {
  from: string;
  to: string;
  label: string;
  dashed?: boolean;
  note?: boolean; // a sequence note over `from` (and `to`)
}
export interface Diagram {
  layout: "flow" | "layers" | "sequence";
  direction: "down" | "right";
  nodes: DiagramNode[];
  edges: DiagramEdge[];
  caption?: string;
}
export type Segment =
  | { type: "markdown"; tokens: Token[] }
  | { type: "table"; table: ReplyTable }
  | { type: "diagram"; diagram: Diagram; source: string }
  | { type: "steps"; items: string[] };

export const LIMITS = { source: 6000, nodes: 40, edges: 80, participants: 8, messages: 60, label: 180, steps: 12, stepChars: 400, cells: 4000 };

/** The plain words of inline tokens (links keep their text, images their alt text, markup none). */
export function plain(tokens: Token[] | undefined, fallback = ""): string {
  if (!tokens) return fallback;
  return tokens.map((token) => {
    const nested = (token as { tokens?: Token[] }).tokens;
    if (nested && nested.length) return plain(nested);
    if (token.type === "br") return " ";
    if (token.type === "html") return "";
    return (token as { text?: string }).text ?? "";
  }).join("").replace(/\s+/g, " ").trim();
}

function tableOf(token: Tokens.Table): ReplyTable | null {
  if (!token.header.length || !token.rows.length || token.header.length * (token.rows.length + 1) > LIMITS.cells) return null;
  const cell = (c: Tokens.TableCell): ReplyCell => ({ text: plain(c.tokens, c.text), markdown: c.text });
  return { header: token.header.map(cell), rows: token.rows.map((row) => row.map(cell)), align: [...token.align] };
}

function stepsOf(token: Tokens.List): string[] | null {
  const start = token.start === "" ? 1 : Number(token.start);
  if (!token.ordered || start !== 1 || token.loose || token.items.length < 3 || token.items.length > LIMITS.steps) return null;
  // only short, single-paragraph steps: a nested list, code block or table stays the list it was
  if (token.items.some((item) => item.task || item.text.length > LIMITS.stepChars
      || item.tokens.some((t) => t.type !== "text" && t.type !== "space"))) return null;
  return token.items.map((item) => item.text);
}

function closedFence(token: Tokens.Code): boolean {
  const raw = token.raw.trimEnd();
  return /^(```|~~~)/.test(token.raw.trimStart()) && (raw.endsWith("```") || raw.endsWith("~~~")) && raw.split("\n").length > 2;
}

/** A reply split into Markdown runs and upgraded parts, in order. Only top-level tables, closed mermaid fences in the
 * subset and short numbered lists are upgraded; every other token is kept for the window's own Markdown pipeline. */
export function replySegments(text: string): Segment[] {
  const tokens = marked.lexer(text, { breaks: true, gfm: true });
  const segments: Segment[] = [];
  let run: Token[] = [];
  const flush = () => {
    if (run.length) segments.push({ type: "markdown", tokens: run });
    run = [];
  };
  for (const token of tokens) {
    let upgraded: Segment | null = null;
    if (token.type === "table") {
      const table = tableOf(token as Tokens.Table);
      if (table) upgraded = { type: "table", table };
    } else if (token.type === "code" && /^mermaid$/i.test(((token as Tokens.Code).lang || "").trim()) && closedFence(token as Tokens.Code)) {
      const diagram = parseMermaid((token as Tokens.Code).text);
      if (diagram) upgraded = { type: "diagram", diagram, source: (token as Tokens.Code).text };
    } else if (token.type === "list") {
      const items = stepsOf(token as Tokens.List);
      if (items) upgraded = { type: "steps", items };
    }
    if (upgraded) {
      flush();
      segments.push(upgraded);
    } else run.push(token);
  }
  flush();
  return segments;
}

/** The columns whose every non-empty cell is a number (at least two of them), with those numbers. */
export function numericColumns(table: ReplyTable): Map<number, (number | null)[]> {
  const found = new Map<number, (number | null)[]>();
  table.header.forEach((_, column) => {
    const values = table.rows.map((row) => (row[column]?.text.trim() ? parseNumber(row[column].text) : null));
    const filled = table.rows.filter((row) => row[column]?.text.trim()).length;
    if (filled >= 2 && values.filter((v) => v !== null).length === filled) found.set(column, values);
  });
  return found;
}

const yearLike = (values: (number | null)[]) => values.every((v) => v === null || (Number.isInteger(v) && v >= 1900 && v <= 2100));

/** What a table's chart toggle can draw: one label column (the first that is not a number, else a column of years)
 * and the number columns beside it; null when there is nothing to chart or too many rows to read as bars. */
export function tableChart(table: ReplyTable): { label: number; values: number[] } | null {
  if (table.rows.length < 2 || table.rows.length > 60) return null;
  const numbers = numericColumns(table);
  const years = [...numbers].filter(([, values]) => yearLike(values)).map(([column]) => column);
  const label = table.header.findIndex((_, column) => !numbers.has(column));
  const chosen = label >= 0 ? label : years[0] ?? -1;
  if (chosen < 0) return null;
  const values = [...numbers.keys()].filter((column) => column !== chosen && !years.includes(column));
  return values.length ? { label: chosen, values } : null;
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
/** Row indexes in order of one column: numbers by value, words as people sort them; empty cells last; stable. */
export function sortedRows(table: ReplyTable, column: number, direction: "ascending" | "descending"): number[] {
  const numbers = numericColumns(table).get(column);
  const sign = direction === "ascending" ? 1 : -1;
  return table.rows.map((_, index) => index).sort((a, b) => {
    const left = table.rows[a][column]?.text.trim() ?? "", right = table.rows[b][column]?.text.trim() ?? "";
    if (!left || !right) return !left && !right ? a - b : !left ? 1 : -1;
    const order = numbers ? (numbers[a] as number) - (numbers[b] as number) : collator.compare(left, right);
    return order * sign || a - b;
  });
}

// ---- mermaid: a bounded subset ----------------------------------------------------------------------------------

const ID_RE = /^[A-Za-z0-9_]{1,60}/;
// longest first, so "([" wins over "(" and "[["  over "["
const SHAPES: [string, string, DiagramNode["shape"]][] = [
  ["([", "])", "round"], ["[[", "]]", "box"], ["((", "))", "round"], ["{{", "}}", "decision"], ["[(", ")]", "box"],
  ["[", "]", "box"], ["(", ")", "round"], ["{", "}", "decision"], [">", "]", "box"],
];
const STYLE_LINE = /^(?:style|classDef|class|linkStyle|direction)\b/;
const ARROW = /^<?(?:-{2,}|={2,}|-\.+-)[>xo]?/;
const ARROW_WITH_TEXT = /^(--|==|-\.)\s+([^-=.>|\n][^>|\n]*?)\s+(-{2,}[>xo]?|={2,}[>xo]?|\.-+[>xo]?)/;

function label(text: string): string {
  let value = text.trim();
  if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
  return value.replace(/<br\s*\/?[>]/gi, " ").replace(/\s+/g, " ").trim().slice(0, LIMITS.label);
}

/** Split one flowchart line at top-level semicolons (not inside a label). */
function statements(line: string): string[] {
  const parts: string[] = [];
  let depth = 0, quoted = false, current = "";
  for (const ch of line) {
    if (ch === '"') quoted = !quoted;
    else if (!quoted && "[({".includes(ch)) depth++;
    else if (!quoted && "])}".includes(ch)) depth = Math.max(0, depth - 1);
    if (ch === ";" && !depth && !quoted) {
      parts.push(current);
      current = "";
    } else current += ch;
  }
  parts.push(current);
  return parts.map((p) => p.trim()).filter(Boolean);
}

function flowchart(lines: string[], header: RegExpExecArray): Diagram | null {
  const direction = /^(LR|RL)$/i.test(header[2] || "") ? "right" : "down";
  const nodes = new Map<string, DiagramNode>(), edges: DiagramEdge[] = [];
  const node = (id: string, text?: string, shape?: DiagramNode["shape"]) => {
    const known = nodes.get(id);
    if (!known) nodes.set(id, { id, label: text || id, shape: shape || "box" });
    else if (text && known.label === id) Object.assign(known, { label: text, shape: shape || known.shape });
    return nodes.size <= LIMITS.nodes;
  };
  for (const line of lines) {
    if (STYLE_LINE.test(line)) continue; // Pebble draws in its own design
    for (const statement of statements(line)) {
      let rest = statement, previous: string | null = null, pending: { label: string; dashed: boolean } | null = null;
      while (rest.length) {
        const id = ID_RE.exec(rest);
        if (!id) return null;
        rest = rest.slice(id[0].length);
        let text: string | undefined, shape: DiagramNode["shape"] | undefined;
        const open = SHAPES.find(([start]) => rest.startsWith(start));
        if (open) {
          const close = rest.indexOf(open[1], open[0].length);
          if (close < 0) return null;
          text = label(rest.slice(open[0].length, close));
          shape = open[2];
          rest = rest.slice(close + open[1].length);
          if (!text) return null;
        }
        if (!node(id[0], text, shape)) return null;
        if (previous !== null && pending) {
          edges.push({ from: previous, to: id[0], label: pending.label, dashed: pending.dashed });
          if (edges.length > LIMITS.edges) return null;
        }
        previous = id[0];
        pending = null;
        rest = rest.trimStart();
        if (!rest) break;
        const worded = ARROW_WITH_TEXT.exec(rest);
        const arrow = worded ? null : ARROW.exec(rest);
        if (!worded && !arrow) return null;
        const used = worded ? worded[0] : (arrow as RegExpExecArray)[0];
        rest = rest.slice(used.length).trimStart();
        let words = worded ? label(worded[2]) : "";
        if (!worded && rest.startsWith("|")) {
          const end = rest.indexOf("|", 1);
          if (end < 0) return null;
          words = label(rest.slice(1, end));
          rest = rest.slice(end + 1).trimStart();
        }
        pending = { label: words.slice(0, 100), dashed: used.includes(".") };
        if (!rest) return null; // an arrow to nothing
      }
    }
  }
  if (!nodes.size) return null;
  return { layout: "flow", direction, nodes: [...nodes.values()], edges };
}

const SEQUENCE_IGNORED = /^(?:autonumber|activate|deactivate|title)\b/;
const PARTICIPANT = /^(?:participant|actor)\s+([A-Za-z0-9_]{1,40})(?:\s+as\s+(.+))?$/;
const MESSAGE = /^([A-Za-z0-9_]{1,40})\s*(-{1,2}>>|-{1,2}>|-{1,2}x|-{1,2}\))\s*[+-]?\s*([A-Za-z0-9_]{1,40})\s*:\s*(.*)$/;
const NOTE = /^note\s+(?:left of|right of|over)\s+([A-Za-z0-9_]{1,40})(?:\s*,\s*([A-Za-z0-9_]{1,40}))?\s*:\s*(.*)$/i;

function sequence(lines: string[]): Diagram | null {
  const nodes = new Map<string, DiagramNode>(), edges: DiagramEdge[] = [];
  let caption: string | undefined;
  const participant = (id: string, text?: string) => {
    if (!nodes.has(id)) nodes.set(id, { id, label: text ? label(text) || id : id });
    return nodes.size <= LIMITS.participants;
  };
  for (const line of lines) {
    if (/^title\b/.test(line)) {
      caption = label(line.slice(5).replace(/^:/, ""));
      continue;
    }
    if (SEQUENCE_IGNORED.test(line)) continue;
    let match: RegExpExecArray | null;
    if ((match = PARTICIPANT.exec(line))) {
      if (!participant(match[1], match[2])) return null;
    } else if ((match = MESSAGE.exec(line))) {
      if (!participant(match[1]) || !participant(match[3])) return null;
      edges.push({ from: match[1], to: match[3], label: label(match[4]).slice(0, 100), dashed: match[2].startsWith("--") });
    } else if ((match = NOTE.exec(line))) {
      if (!participant(match[1]) || (match[2] && !participant(match[2]))) return null;
      edges.push({ from: match[1], to: match[2] || match[1], label: label(match[3]).slice(0, 100), note: true });
    } else return null; // loops, alternatives and boxes stay a code block
    if (edges.length > LIMITS.messages) return null;
  }
  if (!edges.length) return null;
  return { layout: "sequence", direction: "down", nodes: [...nodes.values()], edges, ...(caption ? { caption } : {}) };
}

/** A mermaid flowchart or sequence diagram as Pebble's own diagram data, or null when any line is outside the subset
 * (subgraphs, links, loops, other diagram types) or a bound is passed: the block then stays a code block. */
export function parseMermaid(source: string): Diagram | null {
  if (typeof source !== "string" || source.length > LIMITS.source) return null;
  const lines = source.split(/\r?\n/).map((line) => line.replace(/%%.*$/, "").trim()).filter(Boolean);
  if (!lines.length) return null;
  const flow = /^(flowchart|graph)(?:\s+(TD|TB|BT|LR|RL))?\s*;?$/i.exec(lines[0]);
  if (flow) return flowchart(lines.slice(1), flow);
  if (/^sequenceDiagram$/.test(lines[0])) return sequence(lines.slice(1));
  return null;
}

/** A model's diagram view (artifacts.validate's shape) as Pebble's diagram, checked again at the display boundary:
 * null when anything is out of shape or bound, so the window says so instead of drawing it. */
export function diagramOf(data: unknown, caption?: string): Diagram | null {
  const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
  const words = (v: unknown, max: number, empty = false) => typeof v === "string" && v.length <= max && (empty || !!v.trim());
  if (!object(data) || !Array.isArray(data.nodes) || !Array.isArray(data.edges)) return null;
  const nodes = data.nodes, edges = data.edges;
  if (!nodes.length || nodes.length > LIMITS.nodes || edges.length > LIMITS.edges) return null;
  const layout = data.layout ?? "flow", direction = data.direction ?? "down";
  if (!["flow", "layers", "sequence"].includes(layout as string) || !["down", "right"].includes(direction as string)) return null;
  const ids = new Set<string>();
  for (const n of nodes) {
    if (!object(n) || !words(n.id, 60) || !words(n.label, LIMITS.label) || ids.has(n.id as string)) return null;
    if ((n.group !== undefined && !words(n.group, 40)) || (n.sub !== undefined && !words(n.sub, 60))
        || (n.tone !== undefined && typeof n.tone !== "string")) return null;
    ids.add(n.id as string);
  }
  for (const e of edges)
    if (!object(e) || !ids.has(e.from as string) || !ids.has(e.to as string) || !words(e.label ?? "", 100, true)) return null;
  if (layout === "sequence" && nodes.length > LIMITS.participants) return null;
  const text = typeof data.caption === "string" && data.caption.trim() ? data.caption : caption;
  return {
    layout: layout as Diagram["layout"], direction: direction as Diagram["direction"],
    nodes: nodes.map((n) => {
      const node = n as Record<string, string>;
      return { id: node.id, label: node.label, ...(node.group ? { group: node.group } : {}), ...(node.sub ? { sub: node.sub } : {}),
        ...(node.tone ? { tone: node.tone } : {}) };
    }),
    edges: edges.map((e) => {
      const edge = e as Record<string, string>;
      return { from: edge.from, to: edge.to, label: edge.label ?? "" };
    }),
    ...(text ? { caption: text } : {}),
  };
}

/** A diagram's words in reading order, the text alternative a screen reader and the "as text" view share. */
export function diagramText(diagram: Diagram): string[] {
  const name = new Map(diagram.nodes.map((n) => [n.id, n.label]));
  const lines = diagram.edges.map((edge) => edge.note
    ? `Note on ${name.get(edge.from)}${edge.to !== edge.from ? " and " + name.get(edge.to) : ""}: ${edge.label}`
    : `${name.get(edge.from)} → ${name.get(edge.to)}${edge.label ? ": " + edge.label : ""}`);
  const linked = new Set(diagram.edges.flatMap((e) => [e.from, e.to]));
  return [...lines, ...diagram.nodes.filter((n) => !linked.has(n.id)).map((n) => n.label)];
}
