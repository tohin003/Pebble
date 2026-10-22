<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from 'vue';
import type { Ref } from 'vue';
import { native, rpc } from '../bridge';

type Data = Record<string, unknown>;
type Row = [string, string];
interface Recipe { id: string; kind: string; available: boolean; reason?: unknown; issue?: unknown; script?: unknown }
interface Receipt { receipt_id: string; operation: string; state: string; summary?: unknown; destructive?: unknown; publication?: unknown; undo_of?: unknown; undone_by?: unknown; created?: unknown; error?: unknown; note?: unknown }
interface Binding { worktree_id: string; run_id: string; agent_id: string; state: string; generation?: unknown; base_commit?: unknown; path?: unknown; writes?: unknown; changed_paths?: unknown }
interface Environment { environment_id: string; kind: string; state: string; size_bytes?: unknown; file_count?: unknown; lock_digest?: unknown; registries?: unknown; reason?: unknown; phase?: unknown }

const props = defineProps<{ chatId: string; workspace?: string | null; refreshKey?: unknown }>();
const receiptStates = new Set(['executing', 'applied', 'failed', 'uncertain', 'undone']);
const worktreeStates = new Set(['proposed', 'allocating', 'active', 'failed', 'rejected', 'uncertain', 'retiring', 'retired']);
const environmentStates = new Set(['installing', 'ready', 'failed', 'uncertain', 'removed']);
const removable = new Set(['ready', 'failed', 'uncertain']);
const installKinds = [['npm-ci', 'Prepare npm-ci install'], ['pip-hashes', 'Prepare pip-hashes install']] as const;
// Phase 24 (V1-124): review cards are in the Details drawer's Activity tab; "Activity & review" no longer exists.
const reviewNotice = 'Plan prepared as a review card in Details › Activity. Nothing runs until you approve it there.';
const changedWorktree = 'That worktree changed or is no longer active. Nothing was removed.';
const changedEnvironment = 'That environment changed or can no longer be removed. Nothing was deleted.';

const recipes = shallowRef<Recipe[] | null>(null), inventoryTruncated = ref(false);
const receipts = shallowRef<Receipt[] | null>(null), readback = shallowRef<{ receipt_id: string; data: Data } | null>(null);
const worktrees = shallowRef<Binding[] | null>(null), discarding = shallowRef<Binding | null>(null), retired = shallowRef<{ agent_id: string; data: Data } | null>(null);
const environments = shallowRef<Environment[] | null>(null), removing = shallowRef<Environment | null>(null);
const busy = ref(''), target = ref('');
const recipesLoading = ref(false), receiptsLoading = ref(false), worktreesLoading = ref(false), environmentsLoading = ref(false);
const recipesError = ref(''), receiptsError = ref(''), gitError = ref(''), worktreesError = ref(''), worktreeError = ref(''), environmentsError = ref(''), environmentError = ref('');
const gitNotice = ref(''), worktreeNotice = ref(''), environmentNotice = ref('');
const readbackCard = ref<HTMLElement | null>(null), discardCard = ref<HTMLElement | null>(null), removeCard = ref<HTMLElement | null>(null);
const commitForm = reactive(blankCommit()), branchForm = reactive(blankBranch());
let mounted = false, queued: { all: boolean } | null = null;
let scopeVersion = 0, recipesRequest = 0, receiptsRequest = 0, worktreesRequest = 0, environmentsRequest = 0, actionVersion = 0;

function blankCommit() { return { paths: '', message: '', branch: '' }; }
function blankBranch() { return { name: '', switch: false }; }
function scopeKey() { return props.chatId + '\u0000' + (props.workspace || ''); }
function current(version: number, scope: string) { return mounted && version === scopeVersion && scope === scopeKey(); }
function failure(cause: unknown) {
  const text = cause instanceof Error ? cause.message : String(cause);
  return (text || 'The request failed.').slice(0, 1000);
}
function display(value: unknown, limit = 2000) {
  if (value == null) return '';
  let text: string;
  if (typeof value === 'string') text = value;
  else if (typeof value === 'number' || typeof value === 'boolean') text = String(value);
  else {
    try { text = JSON.stringify(value) ?? ''; } catch { return 'Display data is unavailable.'; }
  }
  return text.length > limit ? text.slice(0, limit) + '… (truncated for display)' : text;
}
function short(value: unknown) { return typeof value === 'string' && /^[0-9a-f]{16,128}$/i.test(value) ? value.slice(0, 12) : display(value, 200); }
function record(value: unknown): Data { return value && typeof value === 'object' && !Array.isArray(value) ? value as Data : {}; }
function when(value: unknown) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }
  return typeof value === 'string' && value ? value.slice(0, 100) : '—';
}
function stateName(value: unknown, known: Set<string>) { return typeof value === 'string' && known.has(value) ? value.replaceAll('_', ' ') : 'unknown'; }
function size(value: unknown) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 'size unknown';
  for (const [unit, step] of [['GiB', 2 ** 30], ['MiB', 2 ** 20], ['KiB', 2 ** 10]] as const) {
    if (value >= step) return (value / step).toFixed(1).replace(/\.0$/, '') + ' ' + unit;
  }
  return value + ' bytes';
}
function registries(value: unknown) {
  return Array.isArray(value) ? value.slice(0, 8).map(item => Array.isArray(item) ? display(item[0], 253) + ':' + display(item[1], 8) : display(item, 300)).join(', ') : '';
}
function undoable(item: Receipt) { return item.state === 'applied' && item.operation !== 'undo'; }

function relativePaths(value: string) {
  const paths = [...new Set(value.split(/\r?\n/).map(line => line.trim()).filter(Boolean))];
  if (!paths.length || paths.length > 64) throw new Error('List 1–64 relative file paths, one per line.');
  for (const path of paths) {
    const parts = path.split('/');
    if (path.length > 512 || /[\u0000-\u001f\u007f\\]/.test(path) || /^(?:~|[A-Za-z]:)/.test(path) || parts.some(part => !part || part === '.' || part === '..' || part.toLowerCase() === '.git')) {
      throw new Error('Use paths relative to the project, such as src/app.ts: ' + path.slice(0, 120));
    }
  }
  return paths;
}
function commitMessage(value: string) {
  const text = value.replace(/\r\n?/g, '\n');
  if (!text.trim() || text.length > 4000 || /[\u0000-\u0008\u000b-\u001f\u007f]/.test(text)) throw new Error('Write a commit message of 1–4000 characters without control characters.');
  const subject = text.split('\n', 1)[0];
  if (!subject.trim() || subject.length > 200) throw new Error('The first line of the commit message must be non-empty and at most 200 characters.');
  return text;
}
// Mirrors the host's branch rule so obvious mistakes never reach native; the host re-checks.
function branchName(value: string) {
  const name = value.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._\/-]{0,99}$/.test(name) || name.includes('..') || name.includes('//') || /[\/.]$/.test(name) || name === 'HEAD'
    || /^[0-9a-f]{40}$/.test(name) || name.split('/').some(part => part.startsWith('.') || part.endsWith('.lock'))) {
    throw new Error('Use a plain branch name: letters, digits, ".", "_", "-" and "/", at most 100 characters, without "..", "//", a ".lock" part or a trailing "/" or ".".');
  }
  return name;
}

// Only fields the panel renders are trusted to exist; anything else in a reply is ignored.
function validRecipes(value: unknown): value is { recipes: Recipe[]; inventory_truncated?: unknown } {
  const item = value as { recipes?: unknown } | null;
  return !!item && typeof item === 'object' && Array.isArray(item.recipes)
    && item.recipes.every(recipe => !!recipe && typeof recipe.id === 'string' && typeof recipe.kind === 'string' && typeof recipe.available === 'boolean');
}
function validReceipts(value: unknown): value is { items: Receipt[] } {
  const item = value as { items?: unknown } | null;
  return !!item && typeof item === 'object' && Array.isArray(item.items)
    && item.items.every(row => !!row && typeof row.receipt_id === 'string' && /^[0-9a-f]{32}$/.test(row.receipt_id) && typeof row.operation === 'string' && typeof row.state === 'string');
}
function validBindings(value: unknown): value is Binding[] {
  return Array.isArray(value) && value.every(row => !!row && typeof row.worktree_id === 'string' && typeof row.run_id === 'string' && typeof row.agent_id === 'string' && typeof row.state === 'string');
}
function validEnvironments(value: unknown): value is { items: Environment[] } {
  const item = value as { items?: unknown } | null;
  return !!item && typeof item === 'object' && Array.isArray(item.items)
    && item.items.every(row => !!row && typeof row.environment_id === 'string' && typeof row.kind === 'string' && typeof row.state === 'string');
}
// A staged plan is a pending review card, never an approval.
function staged(value: unknown) {
  const item = value as { status?: unknown } | null;
  if (!item || typeof item !== 'object' || typeof item.status !== 'string') throw new Error('The host reply was incomplete. Check Details › Activity before preparing it again.');
  return item.status === 'awaiting_user_approval' ? reviewNotice : 'Pebble answered “' + item.status.slice(0, 60) + '”. Check Details › Activity.';
}

const readbackView = computed(() => {
  const open = readback.value;
  if (!open) return null;
  const data = open.data, checks = Array.isArray(data.expected) ? data.expected : [];
  const match = (value: unknown) => value === true ? 'yes' : value === false ? 'no' : 'not applicable';
  const rows: Row[] = [['Receipt state', display(data.state, 40) || 'not reported'], ['Readback verified', data.verified === true ? 'yes' : 'no'],
    ['Matches the receipt', match(data.matches_receipt)], ['Matches the state before', match(data.matches_before)]];
  if (data.issue) rows.push(['Issue', display(data.issue, 1000)]);
  return {
    id: open.receipt_id.slice(0, 12), rows, hidden: Math.max(0, checks.length - 64),
    checks: checks.slice(0, 64).map(check => {
      const item = record(check);
      return display(item.fact, 40) + (item.key ? ' ' + display(item.key, 300) : '') + ': ' + (item.match === true ? 'matches' : 'differs')
        + ' · expected ' + (short(item.expected) || 'none') + ' · current ' + (short(item.current) || 'none');
    }),
  };
});
const retiredView = computed(() => {
  const open = retired.value;
  if (!open) return null;
  const data = open.data, preserved = Array.isArray(data.preserved) ? data.preserved : [];
  const rows: Row[] = [['State', display(data.state, 40)], ['Worktree', short(data.worktree_id) || 'not reported'], ['Integrated paths', display(data.integrated_count, 20) || '0'], ['Saved as blobs', String(preserved.length)]];
  return {
    agent: open.agent_id, rows, hidden: Math.max(0, preserved.length - 64),
    preserved: preserved.slice(0, 64).map(entry => {
      const item = record(entry);
      return display(item.path, 500) + ' · ' + (item.deleted === true ? 'deleted in the worktree; nothing to keep' : 'blob ' + (display(item.blob_oid, 64) || 'not reported'));
    }),
  };
});

async function loadRecipes() {
  if (!mounted || !native || !props.workspace) return;
  const version = scopeVersion, scope = scopeKey(), request = ++recipesRequest;
  const live = () => current(version, scope) && request === recipesRequest;
  recipesLoading.value = true; recipesError.value = '';
  try {
    const result = await rpc<unknown>('project_recipes', { id: props.chatId });
    if (!live()) return;
    if (!validRecipes(result)) throw new Error('Recipe data is incomplete.');
    recipes.value = result.recipes.slice(0, 64); inventoryTruncated.value = result.inventory_truncated === true;
  } catch (cause) {
    if (live()) recipesError.value = failure(cause);
  } finally {
    if (live()) recipesLoading.value = false;
  }
}
async function loadReceipts() {
  if (!mounted || !native) return;
  const version = scopeVersion, scope = scopeKey(), request = ++receiptsRequest;
  const live = () => current(version, scope) && request === receiptsRequest;
  receiptsLoading.value = true; receiptsError.value = '';
  try {
    const result = await rpc<unknown>('git_plan_receipts', { id: props.chatId });
    if (!live()) return;
    if (!validReceipts(result)) throw new Error('Git receipt data is incomplete.');
    receipts.value = result.items.slice(0, 200);
    const open = readback.value;
    if (open && !result.items.some(item => item.receipt_id === open.receipt_id)) readback.value = null;
  } catch (cause) {
    if (live()) receiptsError.value = failure(cause);
  } finally {
    if (live()) receiptsLoading.value = false;
  }
}
async function loadWorktrees() {
  if (!mounted || !native) return;
  const version = scopeVersion, scope = scopeKey(), request = ++worktreesRequest;
  const live = () => current(version, scope) && request === worktreesRequest;
  worktreesLoading.value = true; worktreesError.value = '';
  try {
    const result = await rpc<unknown>('agent_worktrees', { id: props.chatId });
    if (!live()) return;
    if (!validBindings(result)) throw new Error('Child worktree data is incomplete.');
    worktrees.value = result.slice(0, 100);
    const open = discarding.value;
    if (open && !result.some(item => item.worktree_id === open.worktree_id && item.state === 'active')) { discarding.value = null; worktreeError.value = changedWorktree; }
  } catch (cause) {
    if (live()) worktreesError.value = failure(cause);
  } finally {
    if (live()) worktreesLoading.value = false;
  }
}
async function loadEnvironments() {
  if (!mounted || !native) return;
  const version = scopeVersion, scope = scopeKey(), request = ++environmentsRequest;
  const live = () => current(version, scope) && request === environmentsRequest;
  environmentsLoading.value = true; environmentsError.value = '';
  try {
    const result = await rpc<unknown>('environment_list', { id: props.chatId });
    if (!live()) return;
    if (!validEnvironments(result)) throw new Error('Environment data is incomplete.');
    environments.value = result.items.slice(0, 100);
    const open = removing.value;
    if (open && !result.items.some(item => item.environment_id === open.environment_id && removable.has(item.state))) { removing.value = null; environmentError.value = changedEnvironment; }
  } catch (cause) {
    if (live()) environmentsError.value = failure(cause);
  } finally {
    if (live()) environmentsLoading.value = false;
  }
}
async function loadLists() { await Promise.all([loadReceipts(), loadWorktrees(), loadEnvironments()]); }

// One native request at a time; replies from an earlier scope never touch this one.
async function perform<T>(key: string, issue: Ref<string>, work: (chat: string) => Promise<T>, done: (result: T) => void, after?: () => unknown) {
  if (busy.value || !mounted || !native) return;
  const version = scopeVersion, scope = scopeKey(), request = ++actionVersion;
  const live = () => current(version, scope) && request === actionVersion;
  busy.value = key; issue.value = ''; gitNotice.value = ''; worktreeNotice.value = ''; environmentNotice.value = '';
  try {
    const result = await work(props.chatId);
    if (live()) done(result);
  } catch (cause) {
    if (live()) issue.value = failure(cause);
  } finally {
    if (live() && after) await after();
    if (live()) busy.value = '';
  }
}
function propose(key: string, params: Data, done: () => void) {
  if (!props.workspace) return;
  return perform(key, gitError, chat => rpc<unknown>('git_plan_propose', { id: chat, ...params }), result => { gitNotice.value = staged(result); done(); });
}
function proposeCommit() {
  if (busy.value || !mounted || !props.workspace) return;
  gitError.value = ''; gitNotice.value = '';
  let params: Data;
  try {
    const name = commitForm.branch.trim();
    params = { operation: 'commit', paths: JSON.stringify(relativePaths(commitForm.paths)), message: commitMessage(commitForm.message), ...(name ? { branch: branchName(name) } : {}) };
  } catch (cause) {
    gitError.value = failure(cause); return;
  }
  // The host binds the plan to the bytes it reads now; a later edit here never changes it.
  const sent = { ...commitForm };
  return propose('commit', params, () => {
    if (commitForm.paths === sent.paths && commitForm.message === sent.message && commitForm.branch === sent.branch) Object.assign(commitForm, blankCommit());
  });
}
function proposeBranch() {
  if (busy.value || !mounted || !props.workspace) return;
  gitError.value = ''; gitNotice.value = '';
  let name: string;
  try { name = branchName(branchForm.name); } catch (cause) { gitError.value = failure(cause); return; }
  return propose('create_branch', { operation: 'create_branch', name, switch: branchForm.switch === true }, () => { if (branchForm.name.trim() === name) Object.assign(branchForm, blankBranch()); });
}
function proposeSwitch() {
  if (busy.value || !mounted || !props.workspace) return;
  gitError.value = ''; gitNotice.value = '';
  let name: string;
  try { name = branchName(target.value); } catch (cause) { gitError.value = failure(cause); return; }
  return propose('switch_branch', { operation: 'switch_branch', name }, () => { if (target.value.trim() === name) target.value = ''; });
}
function proposeUndo(item: Receipt) {
  if (!undoable(item)) return;
  return propose('undo:' + item.receipt_id, { operation: 'undo', receipt_id: item.receipt_id }, () => undefined);
}
function readBack(item: Receipt) {
  return perform('readback:' + item.receipt_id, gitError, chat => { readback.value = null; return rpc<unknown>('git_plan_readback', { id: chat, receipt_id: item.receipt_id }); }, data => {
    const facts = record(data);
    if (facts.receipt_id !== item.receipt_id) throw new Error('Readback data is incomplete.');
    readback.value = { receipt_id: item.receipt_id, data: facts };
    void nextTick(() => readbackCard.value?.focus());
  });
}
function showRetired(item: Binding, data: unknown, discard: boolean) {
  const facts = record(data);
  if (typeof facts.state !== 'string') throw new Error('Retirement data is incomplete. Refresh the list before continuing.');
  retired.value = { agent_id: item.agent_id, data: facts };
  worktreeNotice.value = facts.state === 'retired'
    ? (discard ? 'Worktree removed. Un-integrated files were saved as Git blobs first; their IDs are listed below.' : 'Worktree retired. Every change it made was already in the project.')
    : 'Removal was not confirmed, so the record is uncertain. Inspect the folder before continuing.';
}
function retire(item: Binding) {
  if (item.state !== 'active') return;
  return perform('retire:' + item.worktree_id, worktreeError, chat => { retired.value = null; return rpc<unknown>('agent_worktree_retire', { id: chat, run_id: item.run_id, agent_id: item.agent_id, discard: false }); },
    data => showRetired(item, data, false), loadWorktrees);
}
function openDiscard(item: Binding) {
  if (busy.value || item.state !== 'active') return;
  worktreeError.value = ''; worktreeNotice.value = '';
  discarding.value = item;
  void nextTick(() => discardCard.value?.focus());
}
function confirmDiscard() {
  const item = discarding.value;
  if (busy.value || !item) return;
  if (!worktrees.value?.some(entry => entry.worktree_id === item.worktree_id && entry.state === 'active')) { discarding.value = null; worktreeError.value = changedWorktree; return; }
  return perform('discard:' + item.worktree_id, worktreeError, chat => { retired.value = null; return rpc<unknown>('agent_worktree_retire', { id: chat, run_id: item.run_id, agent_id: item.agent_id, discard: true }); },
    data => showRetired(item, data, true), () => { if (discarding.value === item) discarding.value = null; return loadWorktrees(); });
}
function planInstall(kind: 'npm-ci' | 'pip-hashes') {
  if (!props.workspace) return;
  return perform('plan:' + kind, environmentError, chat => rpc<unknown>('environment_plan', { id: chat, kind }), result => { environmentNotice.value = staged(result); });
}
function openRemove(item: Environment) {
  if (busy.value || !removable.has(item.state)) return;
  environmentError.value = ''; environmentNotice.value = '';
  removing.value = item;
  void nextTick(() => removeCard.value?.focus());
}
function confirmRemove() {
  const item = removing.value;
  if (busy.value || !item) return;
  if (!environments.value?.some(entry => entry.environment_id === item.environment_id && removable.has(entry.state))) { removing.value = null; environmentError.value = changedEnvironment; return; }
  return perform('remove:' + item.environment_id, environmentError, chat => rpc<unknown>('environment_remove', { id: chat, environment_id: item.environment_id }), data => {
    const facts = record(data);
    environmentNotice.value = facts.state !== 'removed' ? 'Removal was not confirmed. Refresh the list and inspect the record.'
      : facts.deleted === true ? 'Environment folder deleted and the record settled.' : 'No folder remained; the record is settled.';
  }, () => { if (removing.value === item) removing.value = null; return loadEnvironments(); });
}

function queueRefresh(all: boolean) {
  if (!mounted) return;
  if (queued) { queued.all ||= all; return; }
  const next = queued = { all };
  // Coalesce prop updates so a half-switched chat/workspace pair never reaches native.
  void nextTick(() => {
    if (queued === next) queued = null;
    if (!mounted) return;
    void loadLists();
    if (next.all) void loadRecipes();
  });
}
function reset() {
  scopeVersion++; recipesRequest++; receiptsRequest++; worktreesRequest++; environmentsRequest++; actionVersion++;
  recipes.value = null; inventoryTruncated.value = false; receipts.value = null; readback.value = null;
  worktrees.value = null; discarding.value = null; retired.value = null; environments.value = null; removing.value = null;
  busy.value = ''; target.value = '';
  recipesLoading.value = false; receiptsLoading.value = false; worktreesLoading.value = false; environmentsLoading.value = false;
  for (const message of [recipesError, receiptsError, gitError, worktreesError, worktreeError, environmentsError, environmentError, gitNotice, worktreeNotice, environmentNotice]) message.value = '';
  Object.assign(commitForm, blankCommit()); Object.assign(branchForm, blankBranch());
  queueRefresh(true);
}
watch(scopeKey, reset, { flush: 'sync' });
watch(() => props.refreshKey, () => queueRefresh(false));
onMounted(() => { mounted = true; void loadRecipes(); void loadLists(); });
onUnmounted(() => { mounted = false; queued = null; scopeVersion++; });
</script>

<template>
  <section class="workflow-panel" aria-label="Coding workflows">
    <p v-if="!native" class="workflow-note">Coding workflows are available in the Pebble desktop app.</p>
    <template v-else>
      <section class="workflow-section" aria-label="Project recipes" :aria-busy="recipesLoading">
        <div class="workflow-heading"><strong>Project recipes</strong><button v-if="workspace" class="secondary-button" :disabled="recipesLoading || !!busy" @click="loadRecipes">{{ recipesLoading ? 'Refreshing recipes…' : 'Refresh recipes' }}</button></div>
        <p v-if="!workspace" class="workflow-note">Choose a workspace folder for this chat to detect project recipes.</p>
        <template v-else>
          <!-- Phase 24 (V1-127): an empty section is one line; its explanation shows once there is something to explain. -->
          <p v-if="recipes?.length" class="workflow-note">Detected from the project’s root manifests. Recipe names, reasons and scripts are repository data. Running one still needs a test request and your review; this list grants nothing.</p>
          <p v-if="recipesLoading && !recipes" role="status">Loading recipes…</p>
          <p v-if="recipesError" role="alert">{{ recipesError }}</p>
          <p v-if="recipes && !recipes.length" class="workflow-note">No recipes were detected in this project.</p>
          <p v-if="inventoryTruncated" class="workflow-note">The project file list exceeded its bound, so detection saw only part of it.</p>
          <ul v-if="recipes?.length" class="workflow-list" aria-label="Detected recipes">
            <li v-for="item in recipes" :key="item.id">
              <div class="workflow-row"><strong>{{ item.id }}</strong><span>{{ display(item.kind, 20) }} · {{ item.available ? 'available' : 'unavailable' }}</span></div>
              <small v-if="item.reason">Detected from: {{ display(item.reason, 300) }}</small>
              <p v-if="!item.available && item.issue" class="workflow-note">Issue: {{ display(item.issue, 1000) }}</p>
              <p v-if="typeof item.script === 'string'" class="workflow-text">Script: {{ display(item.script, 4096) }}</p>
            </li>
          </ul>
        </template>
      </section>

      <section class="workflow-section" aria-label="Reviewed Git changes" :aria-busy="receiptsLoading">
        <div class="workflow-heading"><strong>Reviewed Git changes</strong><button class="secondary-button" :disabled="receiptsLoading || !!busy" @click="loadReceipts">{{ receiptsLoading ? 'Refreshing…' : 'Refresh receipts' }}</button></div>
        <p v-if="gitError" role="alert">{{ gitError }}</p>
        <p v-if="gitNotice" role="status" aria-live="polite">{{ gitNotice }}</p>
        <p v-if="!workspace" class="workflow-note">Choose a workspace folder for this chat to prepare Git plans.</p>
        <details v-else class="workflow-manual">
          <summary>Prepare a Git change by hand</summary>
          <p class="workflow-note">Each form prepares one typed Git plan bound to the current repository state. The plan appears as a review card in Details › Activity, and nothing runs until you approve it there. Hooks and the network never run.</p>
          <form class="workflow-form" aria-label="Reviewed commit" @submit.prevent="proposeCommit">
            <fieldset :disabled="!!busy">
              <legend>Commit selected paths</legend>
              <label>Paths to commit, one per line<textarea v-model="commitForm.paths" rows="3" maxlength="40000" spellcheck="false" /></label>
              <label>Commit message<textarea v-model="commitForm.message" rows="3" maxlength="4000" /></label>
              <label>New branch (optional)<input v-model="commitForm.branch" type="text" maxlength="100" spellcheck="false" autocomplete="off" /></label>
              <p class="workflow-note">Only these paths are committed, from their current bytes. Other staged and unstaged work stays exactly as it is. Leave the branch empty to commit on the current branch.</p>
              <button class="secondary-button" type="submit" :disabled="!!busy">{{ busy === 'commit' ? 'Preparing…' : 'Prepare commit plan' }}</button>
            </fieldset>
          </form>
          <form class="workflow-form" aria-label="New branch plan" @submit.prevent="proposeBranch">
            <fieldset :disabled="!!busy">
              <legend>Create a branch at HEAD</legend>
              <label>Branch name<input v-model="branchForm.name" type="text" maxlength="100" spellcheck="false" autocomplete="off" /></label>
              <label class="workflow-check"><input v-model="branchForm.switch" type="checkbox" />Switch to it after creating</label>
              <button class="secondary-button" type="submit" :disabled="!!busy">{{ busy === 'create_branch' ? 'Preparing…' : 'Prepare branch plan' }}</button>
            </fieldset>
          </form>
          <form class="workflow-form" aria-label="Switch branch plan" @submit.prevent="proposeSwitch">
            <fieldset :disabled="!!busy">
              <legend>Switch to an existing branch</legend>
              <label>Branch to switch to<input v-model="target" type="text" maxlength="100" spellcheck="false" autocomplete="off" /></label>
              <p class="workflow-note">Switching needs a clean tracked working tree. Untracked files are kept.</p>
              <button class="secondary-button" type="submit" :disabled="!!busy">{{ busy === 'switch_branch' ? 'Preparing…' : 'Prepare switch plan' }}</button>
            </fieldset>
          </form>
        </details>
        <p v-if="receiptsLoading && !receipts" role="status">Loading Git changes…</p>
        <p v-if="receiptsError" role="alert">{{ receiptsError }}</p>
        <p v-if="receipts && !receipts.length" class="workflow-note">No reviewed Git receipts in this chat.</p>
        <ul v-if="receipts?.length" class="workflow-list" aria-label="Reviewed Git changes in this chat">
          <li v-for="item in receipts" :key="item.receipt_id">
            <div class="workflow-row"><strong>{{ display(item.operation, 40).replaceAll('_', ' ') }}</strong><span>{{ stateName(item.state, receiptStates) }}<template v-if="item.destructive === true"> · destructive</template><template v-if="item.publication === true"> · publication</template></span></div>
            <small>Change {{ item.receipt_id.slice(0, 12) }} · {{ when(item.created) }}<template v-if="item.undo_of"> · undoes {{ display(item.undo_of, 40) }}</template><template v-if="item.undone_by"> · undone by {{ display(item.undone_by, 40) }}</template></small>
            <p v-if="item.summary" class="workflow-text">{{ display(item.summary, 2000) }}</p>
            <p v-if="item.error" class="workflow-note">Error: {{ display(item.error, 1000) }}</p>
            <p v-if="item.note" class="workflow-note">{{ display(item.note, 1000) }}</p>
            <div class="workflow-actions">
              <button class="secondary-button" :disabled="!!busy" :aria-label="'Read back receipt ' + item.receipt_id.slice(0, 12)" @click="readBack(item)">{{ busy === 'readback:' + item.receipt_id ? 'Reading back…' : 'Read back' }}</button>
              <button v-if="undoable(item) && workspace" class="secondary-button" :disabled="!!busy" :aria-label="'Undo receipt ' + item.receipt_id.slice(0, 12) + ' as a new reviewed plan'" @click="proposeUndo(item)">{{ busy === 'undo:' + item.receipt_id ? 'Preparing undo…' : 'Undo' }}</button>
            </div>
          </li>
        </ul>
        <article v-if="readbackView" ref="readbackCard" tabindex="-1" class="workflow-review" :aria-label="'Readback of receipt ' + readbackView.id">
          <p><strong>Readback of receipt {{ readbackView.id }}</strong></p>
          <p class="workflow-note">Read only: current repository facts compared with the record of this change. Readback never retries or repairs anything, and its values are repository data.</p>
          <dl class="workflow-facts"><template v-for="[label, value] in readbackView.rows" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></template></dl>
          <ul v-if="readbackView.checks.length" class="workflow-list" aria-label="Readback checks"><li v-for="(check, index) in readbackView.checks" :key="index">{{ check }}</li></ul>
          <p v-if="readbackView.hidden" class="workflow-note">{{ readbackView.hidden }} more checks not shown.</p>
          <div class="workflow-actions"><button class="secondary-button" :disabled="!!busy" @click="readback = null">Close readback</button></div>
        </article>
      </section>

      <section class="workflow-section" aria-label="Child worktrees" :aria-busy="worktreesLoading">
        <div class="workflow-heading"><strong>Child worktrees</strong><button class="secondary-button" :disabled="worktreesLoading || !!busy" @click="loadWorktrees">{{ worktreesLoading ? 'Refreshing worktrees…' : 'Refresh worktrees' }}</button></div>
        <p v-if="worktrees?.length" class="workflow-note">Each child agent writes only in its own detached worktree; your uncommitted project work is never copied into one. Retire removes a worktree only after every change it made is integrated into the project, and never while its child runs.</p>
        <p v-if="worktreesLoading && !worktrees" role="status">Loading child worktrees…</p>
        <p v-if="worktreesError" role="alert">{{ worktreesError }}</p>
        <p v-if="worktreeError" role="alert">{{ worktreeError }}</p>
        <p v-if="worktreeNotice" role="status" aria-live="polite">{{ worktreeNotice }}</p>
        <p v-if="worktrees && !worktrees.length" class="workflow-note">No child worktrees in this chat.</p>
        <ul v-if="worktrees?.length" class="workflow-list" aria-label="Child worktrees in this chat">
          <li v-for="item in worktrees" :key="item.worktree_id">
            <div class="workflow-row"><strong>{{ item.agent_id }}</strong><span>{{ stateName(item.state, worktreeStates) }}</span></div>
            <small>Worktree {{ item.worktree_id.slice(0, 12) }} · Base {{ short(item.base_commit) || 'unknown' }} · Run {{ item.run_id }}<template v-if="item.generation != null"> · Generation {{ display(item.generation, 20) }}</template></small>
            <small v-if="item.path">{{ display(item.path, 1000) }}</small>
            <small v-if="typeof item.writes === 'number'">{{ item.writes }} write(s) · {{ display(item.changed_paths, 20) || '0' }} path(s)</small>
            <div v-if="item.state === 'active'" class="workflow-actions">
              <button class="secondary-button" :disabled="!!busy" :aria-label="'Retire the worktree of ' + item.agent_id + ' (' + item.worktree_id.slice(0, 8) + ')'" @click="retire(item)">{{ busy === 'retire:' + item.worktree_id ? 'Retiring…' : 'Retire' }}</button>
              <button class="secondary-button danger" :disabled="!!busy" :aria-label="'Discard un-integrated work of ' + item.agent_id + ' (' + item.worktree_id.slice(0, 8) + ')'" @click="openDiscard(item)">Discard un-integrated work</button>
            </div>
          </li>
        </ul>
        <article v-if="discarding" ref="discardCard" tabindex="-1" class="workflow-review" :aria-label="'Discard review for ' + discarding.agent_id">
          <p><strong>Discard un-integrated work of {{ discarding.agent_id }}</strong></p>
          <p class="workflow-note">Pebble first saves every changed file that is not already in the project as a Git blob in the project’s object store and lists the blob IDs, so those bytes stay recoverable. Then it deletes the worktree folder. Integrated changes and your project files are not touched. It is refused while the child still runs.</p>
          <dl class="workflow-facts">
            <dt>Worktree</dt><dd>{{ discarding.worktree_id }}</dd>
            <dt>Base</dt><dd>{{ short(discarding.base_commit) || 'unknown' }}</dd>
            <dt>Folder to delete</dt><dd>{{ display(discarding.path, 1000) || 'not reported' }}</dd>
          </dl>
          <div class="workflow-actions"><button class="secondary-button danger" :disabled="!!busy" @click="confirmDiscard">{{ busy === 'discard:' + discarding.worktree_id ? 'Discarding…' : 'Confirm discard and delete folder' }}</button><button class="secondary-button" :disabled="!!busy" @click="discarding = null">Keep worktree</button></div>
        </article>
        <article v-if="retiredView" class="workflow-review" :aria-label="'Retirement result for ' + retiredView.agent">
          <p><strong>Retirement result for {{ retiredView.agent }}</strong></p>
          <dl class="workflow-facts"><template v-for="[label, value] in retiredView.rows" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></template></dl>
          <template v-if="retiredView.preserved.length">
            <p class="workflow-note">Keep these blob IDs: git cat-file -p with an ID prints the saved bytes.</p>
            <ul class="workflow-list" aria-label="Saved blobs"><li v-for="(entry, index) in retiredView.preserved" :key="index">{{ entry }}</li></ul>
            <p v-if="retiredView.hidden" class="workflow-note">{{ retiredView.hidden }} more not shown.</p>
          </template>
        </article>
      </section>

      <section class="workflow-section" aria-label="Dependency environments" :aria-busy="environmentsLoading">
        <div class="workflow-heading"><strong>Dependency environments</strong><button class="secondary-button" :disabled="environmentsLoading || !!busy" @click="loadEnvironments">{{ environmentsLoading ? 'Refreshing environments…' : 'Refresh environments' }}</button></div>
        <p v-if="environments?.length || workspace" class="workflow-note">Installs run only from your lockfile into a managed folder outside the project. Install scripts never run, and the network reaches only the allowlisted registries through Pebble's proxy. Preparing one creates a review card; nothing installs until you approve it.</p>
        <p v-if="environmentsLoading && !environments" role="status">Loading environments…</p>
        <p v-if="environmentsError" role="alert">{{ environmentsError }}</p>
        <p v-if="environmentError" role="alert">{{ environmentError }}</p>
        <p v-if="environmentNotice" role="status" aria-live="polite">{{ environmentNotice }}</p>
        <p v-if="environments && !environments.length" class="workflow-note">No managed dependency environments in this chat.</p>
        <ul v-if="environments?.length" class="workflow-list" aria-label="Managed environments">
          <li v-for="item in environments" :key="item.environment_id">
            <div class="workflow-row"><strong>{{ display(item.kind, 40) }}</strong><span>{{ stateName(item.state, environmentStates) }}</span></div>
            <small>Environment {{ item.environment_id.slice(0, 12) }} · Lockfile {{ short(item.lock_digest) || 'unknown' }}<template v-if="item.size_bytes != null"> · {{ size(item.size_bytes) }}</template><template v-if="item.file_count != null"> · {{ display(item.file_count, 20) }} files</template></small>
            <small v-if="registries(item.registries)">Registries: {{ registries(item.registries) }}</small>
            <p v-if="item.reason" class="workflow-note">Reason: {{ display(item.reason, 500) }}</p>
            <div v-if="removable.has(item.state)" class="workflow-actions"><button class="secondary-button danger" :disabled="!!busy" :aria-label="'Remove environment ' + item.environment_id.slice(0, 12)" @click="openRemove(item)">Remove</button></div>
          </li>
        </ul>
        <article v-if="removing" ref="removeCard" tabindex="-1" class="workflow-review" :aria-label="'Removal review for environment ' + removing.environment_id.slice(0, 12)">
          <p><strong>Remove environment {{ removing.environment_id.slice(0, 12) }} ({{ display(removing.kind, 40) }})</strong></p>
          <p class="workflow-note">This deletes the managed folder outside the project after Pebble verifies it is the exact folder it created. Your project and its lockfile are not touched; you can prepare a fresh install later.</p>
          <div class="workflow-actions"><button class="secondary-button danger" :disabled="!!busy" @click="confirmRemove">{{ busy === 'remove:' + removing.environment_id ? 'Removing…' : 'Confirm remove' }}</button><button class="secondary-button" :disabled="!!busy" @click="removing = null">Keep environment</button></div>
        </article>
        <p v-if="!workspace" class="workflow-note">Choose a workspace folder for this chat to prepare a dependency install.</p>
        <div v-else class="workflow-actions">
          <button v-for="[kind, label] in installKinds" :key="kind" class="secondary-button" :disabled="!!busy" @click="planInstall(kind)">{{ busy === 'plan:' + kind ? 'Preparing…' : label }}</button>
        </div>
      </section>
    </template>
  </section>
</template>

<style scoped>
.workflow-panel { margin-top: 12px; min-width: 0; font-size: 13px; }
.workflow-section + .workflow-section { margin-top: 16px; }
.workflow-heading, .workflow-row, .workflow-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.workflow-heading strong, .workflow-row strong { flex: 1; }
.workflow-note, .workflow-list small { font-size: 12px; opacity: .75; }
.workflow-list { padding: 0; list-style: none; }
.workflow-list li { margin: 8px 0; overflow-wrap: anywhere; }
.workflow-list small { display: block; margin-top: 4px; }
.workflow-text { white-space: pre-wrap; overflow-wrap: anywhere; }
.workflow-review { border: 1px solid var(--border, #b9b9b9); border-radius: 8px; padding: 12px; margin: 8px 0; }
.workflow-form fieldset { border: 0; padding: 0; margin: 8px 0; min-width: 0; }
.workflow-form legend { font-weight: 600; margin-bottom: 8px; }
.workflow-form label { display: block; margin: 8px 0; }
.workflow-form input:not([type=checkbox]), .workflow-form textarea { display: block; width: 100%; box-sizing: border-box; margin-top: 4px; font: inherit; color: inherit; background: transparent; border: 1px solid var(--border, #b9b9b9); border-radius: 6px; padding: 6px 8px; }
.workflow-form .workflow-check { display: flex; gap: 8px; align-items: center; }
.workflow-facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: 12px; }
.workflow-facts dt { opacity: .75; }
.workflow-facts dd { margin: 0; overflow-wrap: anywhere; }
</style>
