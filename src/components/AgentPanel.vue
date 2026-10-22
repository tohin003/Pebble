<script setup lang="ts">
import { computed, h, onMounted, onUnmounted, ref, watch } from 'vue';
import type { FunctionalComponent } from 'vue';
import { rpc } from '../bridge';

interface AgentNode {
  id: string;
  agent_id: string;
  run_id: string;
  parent_id?: string | null;
  generation?: number;
  name?: string;
  task?: string;
  status: string;
  tools?: string[];
  limits?: Record<string, unknown>;
  depth?: number;
  write_paths?: string[];
  dependencies?: string[];
  result?: unknown;
  evidence_ids?: string[];
  review?: string;
  dispatches?: number;
  charged_tokens?: number;
  elapsed?: number;
  error?: string | null;
}
interface AgentTree {
  root_run_id: string;
  nodes: AgentNode[];
  budget?: Record<string, unknown>;
  runtime?: { local_lane?: number; cloud_limit?: number };
}
interface AgentDetail extends AgentNode {
  receipts?: unknown[];
  messages?: unknown[];
  questions?: unknown[];
  patches?: unknown[];
  effects?: unknown[];
}

const props = defineProps<{ chatId: string; runId?: string; runState?: string; refreshKey?: unknown }>();
const emit = defineEmits<{ refresh: [] }>();
const tree = ref<AgentTree | null>(null), detail = ref<AgentDetail | null>(null);
const selected = ref(''), input = ref(''), error = ref(''), notice = ref('');
const loading = ref(false), detailLoading = ref(false), busy = ref('');
const activeStates = new Set(['queued', 'running', 'waiting_parent', 'waiting_input']);
const rootActiveStates = new Set([...activeStates, 'retry_wait', 'waiting_approval']);
const resumableStates = new Set(['interrupted']);
const knownStates = new Set([...activeStates, ...resumableStates, 'completed', 'failed', 'stopped', 'budget_exhausted']);
let mounted = false, scopeVersion = 0, loadVersion = 0, detailVersion = 0, actionVersion = 0;
let timer: ReturnType<typeof setTimeout> | undefined;

const selectedNode = computed(() => tree.value?.nodes.find(node => node.agent_id === selected.value));
const controllable = computed(() => !!selectedNode.value && (activeStates.has(selectedNode.value.status) || resumableStates.has(selectedNode.value.status)));
const steerable = computed(() => !!selectedNode.value && activeStates.has(selectedNode.value.status));
const rows = computed(() => {
  const nodes = tree.value?.nodes || [];
  const aliases = new Map(nodes.flatMap(node => [[node.agent_id, node], [node.id, node]] as [string, AgentNode][]));
  const result: { node: AgentNode; depth: number }[] = [], visited = new Set<string>();
  function visit(node: AgentNode, depth: number) {
    if (visited.has(node.agent_id)) return;
    visited.add(node.agent_id);
    result.push({ node, depth: Math.min(depth, 8) });
    nodes.filter(child => child.parent_id && aliases.get(child.parent_id)?.agent_id === node.agent_id).forEach(child => visit(child, depth + 1));
  }
  nodes.filter(node => !node.parent_id || !aliases.has(node.parent_id)).forEach(node => visit(node, 0));
  nodes.forEach(node => visit(node, 0));
  return result;
});
const budgets = computed(() => ['root_limits', 'allocated', 'used'].flatMap(group => {
  const value = tree.value?.budget?.[group];
  return value && typeof value === 'object' && !Array.isArray(value)
    ? scalarEntries(value as Record<string, unknown>).map(([key, item]) => [group.replaceAll('_', ' ') + ' · ' + key.replaceAll('_', ' '), item] as [string, unknown])
    : [];
}));
const limits = computed(() => scalarEntries(detail.value?.limits));
const accounting = computed(() => [
  ['Dispatches', detail.value?.dispatches], ['Charged tokens', detail.value?.charged_tokens], ['Model seconds', detail.value?.elapsed],
].filter(([, value]) => typeof value === 'number' && Number.isFinite(value)).map(([label, value]) => label + ': ' + value).join(' · '));
// Phase 24 (V1-111): what a person reads first is each agent's task, state and use, and its result as Markdown;
// IDs, generations, lanes, limits and raw records sit under Technical details.
const usage = computed(() => usageText(detail.value));
function usageText(node?: AgentNode | null) {
  if (!node) return '';
  const parts = [
    typeof node.dispatches === 'number' && Number.isFinite(node.dispatches) ? node.dispatches + (node.dispatches === 1 ? ' model call' : ' model calls') : '',
    typeof node.charged_tokens === 'number' && Number.isFinite(node.charged_tokens) ? node.charged_tokens.toLocaleString() + ' tokens' : '',
    typeof node.elapsed === 'number' && Number.isFinite(node.elapsed) ? Math.round(node.elapsed) + ' s' : '',
  ].filter(Boolean);
  return parts.length ? 'Used ' + parts.join(', ') : '';
}
function rowLine(node: AgentNode) {
  const task = typeof node.task === 'string' ? node.task.replace(/\s+/g, ' ').trim() : '';
  return [task.length > 120 ? task.slice(0, 119) + '…' : task, usageText(node)].filter(Boolean).join(' · ');
}
// A small Markdown reader for an agent's result: headings, lists, quotes, code blocks, **bold** and `code`. Every
// piece becomes a native element with literal text; the result is never parsed as HTML.
type InlinePart = { kind: 'text' | 'strong' | 'code'; text: string };
type Block = { kind: 'h' | 'p' | 'ul' | 'ol' | 'pre' | 'quote'; level: number; lines: InlinePart[][] };
const MAX_RESULT = 8000;
const BLOCK_START = /^(```|#{1,4}\s|\s*[-*•]\s+|\s*\d+[.)]\s+|>)/;
function inline(text: string): InlinePart[] {
  const parts: InlinePart[] = [];
  let last = 0;
  for (const match of text.matchAll(/\*\*[^*\n]+\*\*|`[^`\n]+`/g)) {
    const at = match.index ?? 0;
    if (at > last) parts.push({ kind: 'text', text: text.slice(last, at) });
    parts.push(match[0].startsWith('**') ? { kind: 'strong', text: match[0].slice(2, -2) } : { kind: 'code', text: match[0].slice(1, -1) });
    last = at + match[0].length;
  }
  if (last < text.length) parts.push({ kind: 'text', text: text.slice(last) });
  return parts;
}
function markdownBlocks(value: string): Block[] {
  const lines = value.slice(0, MAX_RESULT).replace(/\r\n?/g, '\n').split('\n'), blocks: Block[] = [];
  const run = (pattern: RegExp, kind: Block['kind'], at: number) => {
    const items: InlinePart[][] = [];
    while (at < lines.length && pattern.test(lines[at])) items.push(inline(lines[at++].replace(pattern, '')));
    blocks.push({ kind, level: 0, lines: items });
    return at;
  };
  for (let i = 0; i < lines.length;) {
    const line = lines[i], heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (line.startsWith('```')) {
      const body: string[] = [];
      for (i++; i < lines.length && !lines[i].startsWith('```'); i++) body.push(lines[i]);
      blocks.push({ kind: 'pre', level: 0, lines: [[{ kind: 'text', text: body.join('\n') }]] });
      i++;
    } else if (heading) {
      blocks.push({ kind: 'h', level: heading[1].length, lines: [inline(heading[2])] });
      i++;
    } else if (/^\s*[-*•]\s+/.test(line)) i = run(/^\s*[-*•]\s+/, 'ul', i);
    else if (/^\s*\d+[.)]\s+/.test(line)) i = run(/^\s*\d+[.)]\s+/, 'ol', i);
    else if (/^>\s?/.test(line)) i = run(/^>\s?/, 'quote', i);
    else if (!line.trim()) i++;
    else {
      const paragraph: InlinePart[][] = [];
      while (i < lines.length && lines[i].trim() && !BLOCK_START.test(lines[i])) paragraph.push(inline(lines[i++]));
      if (paragraph.length) blocks.push({ kind: 'p', level: 0, lines: paragraph });
      else blocks.push({ kind: 'p', level: 0, lines: [inline(lines[i++])] }); // never stall on an unmatched line
    }
  }
  return blocks;
}
const Inline: FunctionalComponent<{ parts: InlinePart[] }> = (props) =>
  props.parts.map((part) => (part.kind === 'strong' ? h('strong', part.text) : part.kind === 'code' ? h('code', part.text) : part.text));
const collections = computed(() => [
  { title: 'Tool records', items: detail.value?.receipts || [] },
  { title: 'Messages', items: detail.value?.messages || [] },
  { title: 'Questions', items: detail.value?.questions || [] },
  { title: 'Patches', items: detail.value?.patches || [] },
  { title: 'Execution requests', items: detail.value?.effects || [] },
]);
function scalarEntries(value?: Record<string, unknown>) {
  return Object.entries(value || {}).filter(([, item]) => typeof item === 'string' || typeof item === 'boolean' || (typeof item === 'number' && Number.isFinite(item))).slice(0, 32);
}
function status(value?: string) { return knownStates.has(value || '') ? value!.replaceAll('_', ' ') : 'unknown'; }
function strings(value?: string[]) { return Array.isArray(value) ? value.filter(item => typeof item === 'string').join(', ') : ''; }
function display(value: unknown) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value, null, 2); } catch { return 'Display data is unavailable.'; }
}
function scope() { return { chat_id: props.chatId, run_id: props.runId! }; }
function current(version: number, chat: string, run?: string) {
  return mounted && version === scopeVersion && chat === props.chatId && run === props.runId;
}
function stopTimer() {
  if (timer !== undefined) clearTimeout(timer);
  timer = undefined;
}
function schedule() {
  stopTimer();
  if (mounted && props.runId && !error.value && (rootActiveStates.has(props.runState || '') || tree.value?.nodes.some(node => activeStates.has(node.status)))) {
    timer = setTimeout(() => { timer = undefined; void load(); }, 2000);
  }
}
function validNode(node: AgentNode, run: string) {
  return node && typeof node.id === 'string' && typeof node.agent_id === 'string' && node.run_id === run && typeof node.status === 'string';
}
// Phase 22 (log noise): a queued run is listed once it runs, and a run the host says is not in this
// conversation is not asked about again on every chat refresh; Refresh agents still asks.
let refused = '';
async function load(explicit = false) {
  if (!mounted || !props.runId || props.runState === 'queued') return;
  if (!explicit && refused === props.chatId + ':' + props.runId) return;
  stopTimer();
  const version = scopeVersion, request = ++loadVersion, chat = props.chatId, run = props.runId;
  loading.value = true;
  error.value = '';
  try {
    const result = await rpc<AgentTree>('agent_list', scope());
    if (!current(version, chat, run) || request !== loadVersion) return;
    if (result?.root_run_id !== run || !Array.isArray(result.nodes) || !result.nodes.every(node => validNode(node, run))) throw new Error('Agent data belongs to a different task or is incomplete.');
    tree.value = result;
    refused = '';
    if (selected.value && !result.nodes.some(node => node.agent_id === selected.value)) {
      selected.value = ''; detail.value = null; input.value = ''; detailVersion++;
    }
    if (selected.value) await loadDetail(selected.value);
  } catch (cause) {
    if (current(version, chat, run) && request === loadVersion) {
      error.value = String(cause);
      if (/(?:not found)/i.test(error.value)) refused = chat + ':' + run; // grouped: never reads as a slash command
    }
  } finally {
    if (current(version, chat, run) && request === loadVersion) { loading.value = false; schedule(); }
  }
}
async function loadDetail(agent: string) {
  if (!mounted || !props.runId || selected.value !== agent) return;
  const version = scopeVersion, request = ++detailVersion, chat = props.chatId, run = props.runId;
  detailLoading.value = true;
  try {
    const result = await rpc<AgentDetail>('agent_get', { ...scope(), agent_id: agent });
    if (!current(version, chat, run) || request !== detailVersion || selected.value !== agent) return;
    if (!validNode(result, run) || result.agent_id !== agent) throw new Error('Agent detail belongs to a different task or is incomplete.');
    detail.value = result;
  } catch (cause) {
    if (current(version, chat, run) && request === detailVersion && selected.value === agent) error.value = String(cause);
  } finally {
    if (current(version, chat, run) && request === detailVersion) detailLoading.value = false;
  }
}
function choose(node: AgentNode) {
  if (busy.value) return;
  detailVersion++;
  detail.value = null; input.value = ''; error.value = ''; notice.value = '';
  selected.value = selected.value === node.agent_id ? '' : node.agent_id;
  if (selected.value) void loadDetail(selected.value);
}
async function act(method: 'agent_steer' | 'agent_cancel') {
  if (busy.value || !controllable.value || !selectedNode.value || !props.runId) return;
  const text = input.value.trim();
  if (method === 'agent_steer' && (!steerable.value || !text)) return;
  const version = scopeVersion, request = ++actionVersion, chat = props.chatId, run = props.runId, agent = selected.value;
  busy.value = method; error.value = ''; notice.value = '';
  try {
    await rpc(method, { ...scope(), agent_id: agent, ...(method === 'agent_steer' ? { text } : {}) });
    if (!current(version, chat, run) || request !== actionVersion || selected.value !== agent) return;
    if (method === 'agent_steer' && input.value.trim() === text) input.value = '';
    notice.value = method === 'agent_steer' ? 'Input sent.' : 'Cancellation requested.';
    emit('refresh');
    await load();
  } catch (cause) {
    if (current(version, chat, run) && request === actionVersion) error.value = String(cause);
  } finally {
    if (current(version, chat, run) && request === actionVersion) busy.value = '';
  }
}
function reset() {
  scopeVersion++; loadVersion++; detailVersion++; actionVersion++;
  stopTimer(); tree.value = null; detail.value = null; selected.value = ''; input.value = '';
  error.value = ''; notice.value = ''; loading.value = false; detailLoading.value = false; busy.value = '';
  if (mounted) void load();
}
watch(() => [props.chatId, props.runId], reset, { flush: 'sync' });
watch(() => [props.runState, props.refreshKey], () => { if (mounted) void load(); });
onMounted(() => { mounted = true; void load(); });
onUnmounted(() => { mounted = false; scopeVersion++; stopTimer(); });
</script>

<template>
  <section v-if="runId" class="agent-panel" aria-label="Agents">
    <div class="agent-heading"><strong>Agents</strong><button class="secondary-button" :disabled="loading || !!busy" @click="load(true)">{{ loading ? 'Refreshing…' : 'Refresh agents' }}</button></div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="!loading && tree && !tree.nodes.length" class="agent-note">No agents recorded for this task.</p>
    <ol v-if="rows.length" class="agent-tree" aria-label="Agent task tree">
      <li v-for="row in rows" :key="row.node.agent_id" :style="{ paddingLeft: row.depth * 16 + 'px' }">
        <button class="secondary-button agent-select" :disabled="!!busy" :aria-expanded="selected === row.node.agent_id" @click="choose(row.node)">{{ row.node.name || row.node.agent_id }}</button>
        <span>{{ status(row.node.status) }}</span><small v-if="rowLine(row.node)">{{ rowLine(row.node) }}</small>
      </li>
    </ol>
    <details v-if="tree && (tree.runtime || budgets.length || rows.length)" class="agent-technical">
      <summary>Technical details</summary>
      <p v-if="tree.runtime" class="agent-note">Local execution lanes: {{ tree.runtime.local_lane }} · Cloud agent limit: {{ tree.runtime.cloud_limit }}</p>
      <dl v-if="budgets.length" class="agent-budget" aria-label="Shared agent budget"><template v-for="[key, value] in budgets" :key="key"><dt>{{ key.replaceAll('_', ' ') }}</dt><dd>{{ value }}</dd></template></dl>
      <ul class="agent-tree" aria-label="Agent records">
        <li v-for="row in rows" :key="row.node.agent_id"><small>{{ row.node.name || row.node.agent_id }}: Generation {{ row.node.generation ?? 1 }} · Tools: {{ strings(row.node.tools) || 'none' }}<template v-if="row.node.review"> · Review: {{ row.node.review }}</template></small></li>
      </ul>
    </details>
    <article v-if="selectedNode" class="agent-detail" aria-label="Selected agent">
      <p v-if="detailLoading" role="status">Loading agent details…</p>
      <template v-if="detail">
        <p v-if="detail.task" class="agent-text">{{ detail.task }}</p>
        <p v-if="detail.error" role="alert">{{ detail.error }}</p>
        <p v-if="usage" class="agent-note">{{ usage }}</p>
        <div v-if="typeof detail.result === 'string' && detail.result.trim()" class="agent-result">
          <strong>Result</strong>
          <template v-for="(block, b) in markdownBlocks(String(detail.result))" :key="b">
            <p v-if="block.kind === 'h'" role="heading" :aria-level="Math.min(6, block.level + 3)" class="agent-md-heading"><Inline :parts="block.lines[0]" /></p>
            <ul v-else-if="block.kind === 'ul'"><li v-for="(line, l) in block.lines" :key="l"><Inline :parts="line" /></li></ul>
            <ol v-else-if="block.kind === 'ol'"><li v-for="(line, l) in block.lines" :key="l"><Inline :parts="line" /></li></ol>
            <blockquote v-else-if="block.kind === 'quote'"><template v-for="(line, l) in block.lines" :key="l"><br v-if="l" /><Inline :parts="line" /></template></blockquote>
            <pre v-else-if="block.kind === 'pre'">{{ block.lines[0][0].text }}</pre>
            <p v-else><template v-for="(line, l) in block.lines" :key="l"><br v-if="l" /><Inline :parts="line" /></template></p>
          </template>
        </div>
        <details class="agent-technical">
          <summary>Technical details</summary>
          <p class="agent-note">Agent: {{ detail.agent_id }} · Task: {{ detail.id }}<template v-if="detail.parent_id"> · Parent: {{ detail.parent_id }}</template></p>
          <dl v-if="limits.length" class="agent-budget" aria-label="Agent limits"><template v-for="[key, value] in limits" :key="key"><dt>{{ key.replaceAll('_', ' ') }}</dt><dd>{{ value }}</dd></template></dl>
          <p v-if="accounting" class="agent-note">{{ accounting }}</p>
          <p v-if="detail.write_paths?.length" class="agent-note">Write paths: {{ strings(detail.write_paths) }}</p>
          <p v-if="detail.dependencies?.length" class="agent-note">Dependencies: {{ strings(detail.dependencies) }}</p>
          <div v-if="detail.result != null && typeof detail.result !== 'string'"><strong>Result record</strong><pre>{{ display(detail.result) }}</pre></div>
          <p v-if="detail.evidence_ids?.length" class="agent-note">Evidence: {{ strings(detail.evidence_ids) }}</p>
          <details v-for="collection in collections.filter(item => item.items.length)" :key="collection.title"><summary>{{ collection.title }} ({{ collection.items.length }})</summary><pre v-for="(item, index) in collection.items" :key="index">{{ display(item) }}</pre></details>
        </details>
      </template>
      <form v-if="controllable" @submit.prevent="act('agent_steer')">
        <label v-if="steerable">Send task input to {{ selectedNode.name || selectedNode.agent_id }}<textarea v-model="input" aria-label="Agent task input" rows="2" maxlength="2000" :disabled="!!busy" /></label>
        <div class="agent-actions"><button v-if="steerable" class="secondary-button" type="submit" :disabled="!!busy || !input.trim()">{{ busy === 'agent_steer' ? 'Sending…' : 'Send input' }}</button><button class="secondary-button" type="button" :disabled="!!busy" @click="act('agent_cancel')">{{ busy === 'agent_cancel' ? 'Cancelling…' : 'Cancel agent and descendants' }}</button></div>
      </form>
      <p v-else class="agent-note">This agent’s saved record is read only.</p>
      <p v-if="notice" role="status">{{ notice }}</p>
    </article>
  </section>
</template>

<style scoped>
.agent-panel { margin-top: 12px; min-width: 0; }
.agent-heading, .agent-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.agent-heading strong { flex: 1; }
.agent-note, .agent-tree small { font-size: 12px; opacity: .75; }
.agent-tree { padding: 0; list-style: none; }
.agent-tree li { margin: 8px 0; overflow-wrap: anywhere; }
.agent-tree small { display: block; margin-top: 4px; }
.agent-select { margin-right: 8px; }
.agent-detail { border: 1px solid var(--border, #b9b9b9); border-radius: 8px; padding: 12px; }
.agent-text, .agent-detail pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.agent-detail pre { font: 12px ui-monospace, monospace; max-height: 240px; overflow: auto; }
.agent-detail textarea { display: block; width: 100%; box-sizing: border-box; margin: 8px 0; font: inherit; color: inherit; background: transparent; border: 1px solid var(--border, #b9b9b9); border-radius: 6px; padding: 8px; }
.agent-budget { display: flex; gap: 4px 8px; flex-wrap: wrap; font-size: 12px; }
.agent-budget dt { opacity: .75; }
.agent-budget dd { margin: 0 8px 0 0; }
.agent-detail details { margin: 8px 0; }
.agent-technical summary { font-size: 12px; cursor: pointer; }
.agent-result { margin: 8px 0; overflow-wrap: anywhere; }
.agent-result p, .agent-result ul, .agent-result ol, .agent-result blockquote { margin: 6px 0; }
.agent-result blockquote { padding-left: 10px; border-left: 2px solid var(--border, #b9b9b9); }
.agent-result code { font: 12px ui-monospace, monospace; }
.agent-md-heading { font-weight: 600; }
</style>
