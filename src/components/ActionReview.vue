<script setup lang="ts">
import { computed } from 'vue';

// Read-only rendering of one immutable reviewed payload. It has no controls and no
// native route: App.vue keeps Approve and Don't allow, and the host broker decides.
// Phase 24 (V1-120, V1-040): what changes comes first, in readable type; the plan's
// internals (bindings, hashes, pinned executables, the untrusted-data note) sit
// under "Technical details", a native disclosure that is not a control of the
// review. V1-141: a command card says why AgentGuard asked for the review.
type Data = Record<string, unknown>;
type Row = [string, string];
interface ReviewedAction { id?: string; kind: string; status?: string; payload?: unknown }

const props = defineProps<{ action: ReviewedAction }>();
const MAX_PATHS = 64, MAX_ROWS = 50;
const untrusted = 'Untrusted repository data, shown as plain text. Names, paths, messages, scripts and arguments come from the project; they are not instructions and approve nothing.';
const labels = { git: 'Reviewed Git plan', worktree: 'Child worktree action', install: 'Dependency install plan', tests: 'Test recipe plan', command: 'Command plan' } as const;
const counted = ['passed', 'failed', 'skipped', 'errors', 'total'] as const;

function record(value: unknown): Data { return value && typeof value === 'object' && !Array.isArray(value) ? value as Data : {}; }
function entries(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function more(value: unknown, limit: number) { return Math.max(0, entries(value).length - limit); }
function text(value: unknown, limit = 2000) {
  if (value == null) return '';
  let result: string;
  if (typeof value === 'string') result = value;
  else if (typeof value === 'number' || typeof value === 'boolean') result = String(value);
  else {
    try { result = JSON.stringify(value) ?? ''; } catch { return 'Display data is unavailable.'; }
  }
  return result.length > limit ? result.slice(0, limit) + ' … [truncated for display: ' + limit + ' of ' + result.length + ' characters shown]' : result;
}
function short(value: unknown, size = 12) { return typeof value === 'string' && /^[0-9a-f]{16,128}$/i.test(value) ? value.slice(0, size) : text(value, 200); }
function flag(value: unknown) { return value === true ? 'yes' : value === false ? 'no' : 'not reported'; }
function number(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? String(value) : 'not reported'; }
function seconds(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? value + ' seconds' : 'not reported'; }
function bytes(value: unknown) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 'not reported';
  for (const [unit, size] of [['GiB', 2 ** 30], ['MiB', 2 ** 20], ['KiB', 2 ** 10]] as const) {
    if (value >= size) return (value / size).toFixed(1).replace(/\.0$/, '') + ' ' + unit;
  }
  return value + ' bytes';
}
function branch(value: unknown) { return typeof value === 'string' && value.startsWith('refs/heads/') ? text(value.slice(11), 200) : text(value, 300); }
// Only reported facts become rows; an absent value is omitted, never invented.
function rows(pairs: Row[]): Row[] { return pairs.filter(([, value]) => value !== ''); }
function pairs(value: unknown, limit: number): Row[] { return Object.entries(record(value)).slice(0, limit).map(([key, item]) => [text(key, 200), text(item, 1000)]); }
function lines(value: unknown, limit: number, size = 1000) { return entries(value).slice(0, limit).map(item => text(item, size)); }
function argv(value: unknown) {
  const shown = entries(value).slice(0, 64).map(item => { const part = text(item, 1000); return part === '' || /[\s"'\\]/.test(part) ? JSON.stringify(part) : part; });
  return text(shown.join(' '), 4000) + (more(value, 64) ? ' … [' + more(value, 64) + ' more arguments not shown]' : '');
}
function pathRow(value: unknown) {
  const item = record(value), previous = record(item.previous), restore = record(item.restore), after = record(item.index_after ?? item.tree_after);
  const change = typeof item.kind === 'string' ? item.kind
    : 'previous' in item ? (item.previous ? 'discard; current bytes saved first' : 'recreate the deleted file')
    : 'restore' in item ? (item.restore ? 'restore the prior index entry' : 'remove from the index') : 'unknown';
  const blob = item.blob_oid ?? previous.blob_oid ?? restore.oid ?? after.oid;
  return { path: text(item.path, 500), change: text(change, 80), blob: blob == null ? 'none' : short(blob) };
}

const payload = computed(() => record(props.action?.payload));
const result = computed<Data | null>(() => { const value = payload.value.result; return value && typeof value === 'object' && !Array.isArray(value) ? value as Data : null; });
const kind = computed(() => {
  const value = props.action?.kind;
  if (value === 'git' || value === 'worktree' || value === 'install') return value;
  if (value === 'command') return 'command';
  return value === 'tests' && payload.value.plan && typeof payload.value.plan === 'object' && !Array.isArray(payload.value.plan) ? 'tests' : '';
});
const label = computed(() => kind.value ? labels[kind.value] : '');
// V1-141: the host's own sentence on why AgentGuard asked (payload.guard_review), shown as text.
const guardReview = computed(() => kind.value === 'command' && typeof payload.value.guard_review === 'string' ? text(payload.value.guard_review, 300) : '');

const git = computed(() => {
  const p = payload.value, b = record(p.bindings), who = record(b.identity), findings = entries(p.sensitive_findings);
  const remote = typeof p.remote === 'string' || typeof b.remote_path === 'string';
  return {
    operation: text(p.operation, 40) || 'unknown operation',
    summary: text(p.summary, 4000),
    destructive: p.destructive === true, publication: p.publication === true, review: p.requires_review === true,
    hooks: p.hooks_run === false ? 'false' : 'not reported as false',
    network: p.network === false ? 'false' : text(p.network, 80) || 'not reported',
    facts: rows([
      ['Repository root', text(p.root, 1000)],
      ['Branch', typeof b.head_ref === 'string' ? branch(b.head_ref) : 'head_ref' in b ? 'detached HEAD' : ''],
      ['HEAD', short(b.head_commit)],
      ['Author', who.name || who.email ? text(who.name, 200) + ' <' + text(who.email, 254) + '>' + (who.source ? ' · from ' + text(who.source, 40) : '') : ''],
      ['New branch', p.operation === 'commit' ? text(p.branch, 200) : ''],
      ['Local branch', p.operation === 'push' ? text(p.branch, 200) : ''],
      ['Branch name', text(p.name, 200)],
      ['Target ref', text(b.target_ref ?? b.branch_ref, 300)],
      ['Source ref', text(b.source_ref, 300)],
      ['Switch after creating', typeof p.switch === 'boolean' ? flag(p.switch) : ''],
      ['Remote', text(p.remote, 100)],
      ['Remote branch', text(p.remote_branch, 200)],
      ['Remote URL kind', remote ? (typeof b.remote_path === 'string' ? 'local bare repository (network remotes are refused)' : 'not reported') : ''],
      ['Remote repository', text(b.remote_path, 1000)],
      ['Remote ref moves', typeof b.remote_ref === 'string' ? text(b.remote_ref, 300) + ': ' + (b.old_oid ? short(b.old_oid) : 'absent') + ' → ' + (short(b.new_oid) || 'not reported') : ''],
      ['Commits copied', b.commits == null ? '' : number(b.commits)],
      ['Sensitive findings', Array.isArray(p.sensitive_findings) ? (findings.length ? findings.length + ' (listed below)' : 'none reported') : ''],
      ['Undo of receipt', text(p.undo_of, 40)],
      ['Undoes operation', text(p.undo_operation, 40)],
      ['Plan ID', text(p.plan_id, 40)],
      ['Plan hash', short(p.plan_hash, 16)],
    ]),
    paths: entries(b.paths).slice(0, MAX_PATHS).map(pathRow), pathCount: entries(b.paths).length, pathsHidden: more(b.paths, MAX_PATHS),
    message: typeof p.message === 'string' ? text(p.message, 4000) : '',
    findings: findings.slice(0, 20).map(item => { const found = record(item); return { path: text(found.path, 500), line: found.line == null ? 'whole file' : 'line ' + text(found.line, 20), kind: text(found.kind, 60) }; }),
    effects: lines(p.effects, MAX_ROWS),
  };
});
const gitSettled = computed(() => {
  const r = result.value;
  if (!r) return null;
  const facts = record(r.result), readback = record(r.readback), checks = entries(readback.checks);
  return {
    facts: rows([
      ['Receipt state', text(r.state, 40)],
      ['Phase', text(r.phase, 200)],
      ['Receipt ID', text(r.receipt_id, 40)],
      ['New commit', short(facts.commit)],
      ['Tree', short(facts.tree)],
      ['Ref', text(facts.target_ref ?? facts.branch_ref ?? facts.remote_ref ?? facts.head_ref, 300)],
      ['Remote object', short(facts.new_oid)],
      ['HEAD commit', short(facts.head_commit)],
      ['Merge kind', text(facts.merge_kind, 40)],
      ['Effect started', 'effect_started' in r ? flag(r.effect_started) : ''],
      ['Readback verified', 'readback' in r ? flag(readback.verified) : ''],
      ['Readback checks', checks.length ? checks.filter(check => record(check).match === true).length + ' of ' + checks.length + ' matched' : ''],
      ['Error', text(r.error, 1000)],
      ['Note', text(r.note, 1000)],
    ]),
    changed: lines(readback.changed_paths, MAX_PATHS, 500),
    indexSync: pairs(facts.index_sync, MAX_PATHS),
  };
});

const worktree = computed(() => {
  const p = payload.value, allocate = p.operation === 'allocate_child_worktree', retire = p.operation === 'retire_child_worktree';
  return {
    allocate,
    title: allocate ? 'Allocate a child worktree' : retire ? 'Retire a child worktree' : 'Unknown worktree operation: ' + text(p.operation, 60),
    facts: rows([
      ['Child agent', text(p.agent_id, 128)],
      ['Generation', text(p.generation, 20)],
      ['Root run', text(p.run_id, 128)],
      ['Worktree ID', text(p.worktree_id, 40)],
      ['Base commit', short(p.base_commit)],
      ['Folder', text(p.path, 1000)],
      ['Repository root', text(p.root, 1000)],
    ]),
    disclosure: text(p.disclosure, 2000),
    effects: lines(p.effects, MAX_ROWS),
  };
});
const worktreeSettled = computed(() => {
  const r = result.value;
  if (!r) return null;
  return {
    facts: rows([
      ['State', text(r.state, 40)],
      ['Detached checkout', 'detached' in r ? flag(r.detached) : ''],
      ['Dirty work copied', 'dirty_work_copied' in r ? flag(r.dirty_work_copied) : ''],
      ['Project status unchanged', 'parent_status_unchanged' in r ? flag(r.parent_status_unchanged) : ''],
      ['Project index unchanged', 'parent_index_unchanged' in r ? flag(r.parent_index_unchanged) : ''],
      ['Integrated paths', 'integrated_count' in r ? number(r.integrated_count) : ''],
    ]),
    preserved: entries(r.preserved).slice(0, MAX_ROWS).map(item => { const kept = record(item); return text(kept.path, 500) + ' · ' + (kept.deleted === true ? 'deleted in the worktree; nothing to keep' : 'blob ' + (text(kept.blob_oid, 64) || 'not reported')); }),
    preservedHidden: more(r.preserved, MAX_ROWS),
  };
});

const install = computed(() => {
  const p = payload.value;
  return {
    summary: text(p.summary, 2000),
    allowlisted: p.network === 'allowlisted_registry_only', network: text(p.network, 80) || 'not reported',
    facts: rows([
      ['Kind', text(p.kind, 40)],
      ['Environment ID', text(p.environment_id, 40)],
      ['Lockfile digest', short(p.lock_digest)],
      ['Manifest digest', short(p.manifest_digest)],
      ['Packages', number(p.packages)],
      ['Size limit', bytes(p.size_limit)],
      ['Timeout', seconds(p.timeout)],
      ['Project root', text(p.root, 1000)],
    ]),
    registries: entries(p.registries).slice(0, 8).map(item => Array.isArray(item) ? text(item[0], 253) + ':' + text(item[1], 8) : text(item, 300)),
    effects: lines(p.effects, MAX_ROWS),
  };
});
const installSettled = computed(() => {
  const r = result.value;
  if (!r) return null;
  const log = entries(r.egress_log);
  return {
    facts: rows([
      ['State', text(r.state, 40)],
      ['Reason', text(r.reason, 200)],
      ['Exit code', text(r.exit_code, 20)],
      ['Tree digest', short(r.tree_digest)],
      ['Size', r.size_bytes == null ? '' : bytes(r.size_bytes)],
      ['Files', r.file_count == null ? '' : number(r.file_count)],
      ['Egress connections', 'egress_log' in r ? log.length + ' (' + log.filter(item => record(item).allowed === true).length + ' allowed)' : ''],
    ]),
    egress: log.slice(0, MAX_ROWS).map(item => {
      const entry = record(item), up = entry.bytes_up, down = entry.bytes_down;
      const total = typeof up === 'number' && typeof down === 'number' ? bytes(up + down) : 'bytes not reported';
      return text(entry.host, 253) + (entry.port == null ? '' : ':' + text(entry.port, 8)) + ' · ' + (entry.allowed === true ? 'allowed' : 'refused')
        + (entry.reason ? ' · ' + text(entry.reason, 60) : '') + ' · ' + total;
    }),
    egressHidden: more(r.egress_log, MAX_ROWS),
  };
});

const command = computed(() => {
  const p = payload.value, plan = p.plan && typeof p.plan === 'object' && !Array.isArray(p.plan) ? record(p.plan) : null;
  if (!plan) return { planned: false, steps: [] as { argv: string; facts: [string, string][] }[], facts: [] as [string, string][],
    network: '', destinations: [] as string[], credentials: [] as string[], interactive: false };
  const steps = (Array.isArray(plan.steps) ? plan.steps : []).slice(0, 4).map(raw => {
    const step = record(raw), executable = record(step.executable);
    const env = Object.entries(record(step.env)).slice(0, 16).map(([key, value]) => text(key, 64) + '=' + text(value, 200));
    return { argv: argv(step.argv), facts: rows([['Executable realpath', text(executable.realpath, 1000)],
      ['Executable SHA-256', short(executable.sha256)], ['Working folder', text(step.cwd, 200)],
      ['Environment', env.join(', ')], ['Input', step.stdin === 'pty' ? 'interactive terminal' : '']]) };
  });
  const network = record(plan.network), resources = record(plan.resources);
  const destinations = (Array.isArray(network.destinations) ? network.destinations : []).slice(0, 16)
    .map(item => Array.isArray(item) ? text(item[0], 253) + ':' + text(item[1], 6) : '').filter(Boolean);
  const credentials = (Array.isArray(network.credentials) ? network.credentials : []).slice(0, 8).map(raw => {
    const item = record(raw), destination = Array.isArray(item.destination) ? text(item.destination[0], 253) : '';
    return text(item.ref, 128) + ' → ' + destination + ' ' + lines(item.methods, 6).join('/') + ' ' + text(item.path_prefix, 256);
  });
  return { planned: true, steps, network: text(network.mode, 20), destinations, credentials,
    interactive: plan.interactive === true, facts: rows([
    ['Pipeline', text(plan.pipeline, 20)], ['Writable', lines(plan.writable, 16).join(', ') || 'read-only'],
    ['Network', network.mode === 'none' ? 'denied' : network.mode === 'broker' ? 'reviewed destinations only' : text(network.mode, 20)],
    ['Process model', plan.process_model === 'single' ? 'one process, no children' : text(plan.process_model, 20)],
    ['Timeout', seconds(resources.timeout)], ['CPU', seconds(resources.cpu_seconds)],
    ['Memory', resources.memory_mb ? text(resources.memory_mb, 8) + ' MB' : ''], ['Processes', text(resources.nproc, 6)],
    ['Output limit', bytes(resources.output_bytes)], ['Plan hash', short(plan.plan_hash)]]) };
});

const tests = computed(() => {
  const p = payload.value, plan = record(p.plan), executable = record(plan.executable);
  const entrypoint = plan.entrypoint && typeof plan.entrypoint === 'object' ? record(plan.entrypoint) : null;
  const source = plan.mount && typeof plan.mount === 'object' ? plan.mount : p.mount && typeof p.mount === 'object' ? p.mount : null;
  const mount = source ? record(source) : null;
  return {
    recipe: text(plan.recipe ?? p.recipe, 80) || 'unknown recipe',
    facts: rows([
      ['Recipe', text(plan.recipe ?? p.recipe, 80)],
      ['Kind', text(plan.kind, 20)],
      ['Working folder', plan.cwd === 'snapshot' ? 'temporary project snapshot' : text(plan.cwd, 200)],
      ['Timeout', seconds(plan.timeout)],
      ['Output limit', bytes(plan.output_limit)],
      ['Network', plan.network === false ? 'denied' : text(plan.network, 40) || 'not reported'],
      ['Drafts in the snapshot', Array.isArray(p.drafts) ? String(p.drafts.length) : ''],
    ]),
    argv: argv(plan.argv),
    script: typeof plan.script === 'string' ? text(plan.script, 4096) : '',
    env: pairs(plan.env, 32),
    reads: lines(plan.toolchain_reads, 16),
    executable: rows([
      ['Executable realpath', text(executable.realpath, 1000)],
      ['Executable SHA-256', short(executable.sha256)],
      ['npm entrypoint', entrypoint ? text(entrypoint.realpath, 1000) : ''],
      ['npm entrypoint SHA-256', entrypoint ? short(entrypoint.sha256) : ''],
    ]),
    mount: mount ? {
      facts: rows([['Kind', text(mount.kind, 40)], ['Root', text(mount.root, 1000)], ['Environment ID', text(mount.environment_id, 128)], ['Tree digest', short(mount.tree_digest)]]),
      reads: lines(mount.reads, 16), links: pairs(mount.links, 8),
    } : null,
  };
});
const testsSettled = computed(() => {
  const r = result.value;
  if (!r) return null;
  const counts = record(r.tests);
  return {
    facts: rows([
      ['Outcome', text(r.outcome, 40)],
      ['Tests', 'tests' in r ? (r.tests && typeof r.tests === 'object' ? counted.map(key => key + ' ' + number(counts[key])).join(' · ') : 'not counted') : ''],
      ['Exit code', text(r.exit_code, 20)],
      ['Snapshot digest', short(r.snapshot_digest)],
      ['Large files skipped', 'skipped_large_count' in r ? number(r.skipped_large_count) : ''],
      ['Snapshot truncated', 'snapshot_truncated' in r ? flag(r.snapshot_truncated) : ''],
      ['Drafts included', Array.isArray(r.drafts_included) ? (r.drafts_included.length ? r.drafts_included.length + ' (listed below)' : 'none') : ''],
    ]),
    drafts: lines(r.drafts_included, MAX_PATHS, 500), draftsHidden: more(r.drafts_included, MAX_PATHS),
  };
});
</script>

<template>
  <section v-if="kind" class="action-review" :aria-label="label">
    <template v-if="kind === 'git'">
      <p class="review-title"><strong>Git {{ git.operation }}</strong></p>
      <p v-if="git.destructive || git.publication" class="review-badges">
        <span v-if="git.destructive" class="review-badge review-alert">Destructive</span>
        <span v-if="git.publication" class="review-badge review-alert">Publication</span>
      </p>
      <p v-if="git.summary" class="review-text review-lead">{{ git.summary }}</p>
      <p v-if="git.destructive" class="review-note">Destructive: this removes or rewrites work, so it needs your approval in every permission mode. The receipt keeps what an undo needs.</p>
      <p v-if="git.publication" class="review-note">Publication: this copies commits into another repository. Only local bare repositories are supported.</p>
      <template v-if="git.paths.length">
        <p class="review-label">Selected paths ({{ git.pathCount }})</p>
        <ul class="review-list" aria-label="Selected paths"><li v-for="(item, index) in git.paths" :key="index">{{ item.path }} · {{ item.change }} · blob {{ item.blob }}</li></ul>
        <p v-if="git.pathsHidden" class="review-note">{{ git.pathsHidden }} more path(s) not shown.</p>
      </template>
      <template v-if="git.message"><p class="review-label">Commit message (plain text)</p><pre class="review-pre">{{ git.message }}</pre></template>
      <template v-if="git.findings.length">
        <p class="review-label">Sensitive findings (values are never shown)</p>
        <ul class="review-list" aria-label="Sensitive findings"><li v-for="(item, index) in git.findings" :key="index">{{ item.path }} · {{ item.line }} · {{ item.kind }}</li></ul>
      </template>
      <template v-if="git.effects.length">
        <p class="review-label">Effects</p>
        <ul class="review-list" aria-label="Planned effects"><li v-for="(item, index) in git.effects" :key="index">{{ item }}</li></ul>
      </template>
      <details class="review-details">
        <summary>Technical details</summary>
        <p class="review-untrusted">{{ untrusted }}</p>
        <p class="review-badges">
          <span v-if="git.review" class="review-badge">Needs native review</span>
          <span class="review-badge">hooks_run: {{ git.hooks }}</span>
          <span class="review-badge">network: {{ git.network }}</span>
        </p>
        <dl class="review-facts" aria-label="Plan bindings"><template v-for="[name, value] in git.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
      </details>
      <template v-if="gitSettled">
        <p class="review-label">Receipt readback</p>
        <dl class="review-facts" aria-label="Receipt readback"><template v-for="[name, value] in gitSettled.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
        <template v-if="gitSettled.changed.length"><p class="review-label">Changed paths</p><ul class="review-list" aria-label="Changed paths"><li v-for="(item, index) in gitSettled.changed" :key="index">{{ item }}</li></ul></template>
        <template v-if="gitSettled.indexSync.length"><p class="review-label">Index sync (index_sync)</p><ul class="review-list" aria-label="Index sync"><li v-for="[path, outcome] in gitSettled.indexSync" :key="path">{{ path }} · {{ outcome }}</li></ul></template>
      </template>
    </template>

    <template v-else-if="kind === 'worktree'">
      <p class="review-title"><strong>{{ worktree.title }}</strong></p>
      <template v-if="worktree.allocate">
        <p class="review-note">Dirty work is not copied: uncommitted project changes stay where they are and are never copied or discarded. The child writes only inside this folder.</p>
        <p v-if="worktree.disclosure" class="review-text">{{ worktree.disclosure }}</p>
      </template>
      <template v-if="worktree.effects.length">
        <p class="review-label">Effects</p>
        <ul class="review-list" aria-label="Planned effects"><li v-for="(item, index) in worktree.effects" :key="index">{{ item }}</li></ul>
      </template>
      <details class="review-details">
        <summary>Technical details</summary>
        <p class="review-untrusted">{{ untrusted }}</p>
        <dl class="review-facts" aria-label="Worktree binding"><template v-for="[name, value] in worktree.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
      </details>
      <template v-if="worktreeSettled">
        <p class="review-label">Receipt</p>
        <dl class="review-facts" aria-label="Worktree receipt"><template v-for="[name, value] in worktreeSettled.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
        <template v-if="worktreeSettled.preserved.length">
          <p class="review-label">Saved as Git blobs before removal</p>
          <ul class="review-list" aria-label="Preserved blobs"><li v-for="(item, index) in worktreeSettled.preserved" :key="index">{{ item }}</li></ul>
          <p v-if="worktreeSettled.preservedHidden" class="review-note">{{ worktreeSettled.preservedHidden }} more not shown.</p>
        </template>
      </template>
    </template>

    <template v-else-if="kind === 'install'">
      <p class="review-title"><strong>Install dependencies from the lockfile</strong></p>
      <p class="review-badges">
        <span class="review-badge">Install scripts disabled</span>
        <span :class="install.allowlisted ? 'review-badge' : 'review-badge review-alert'">{{ install.allowlisted ? 'Network: allowlisted registries only' : 'Unexpected network value: ' + install.network }}</span>
      </p>
      <p v-if="install.summary" class="review-text review-lead">{{ install.summary }}</p>
      <p class="review-note">Install scripts disabled: npm runs with --ignore-scripts, and pip installs hash-pinned binary wheels only. The network reaches only the registries below, on port 443, through the host proxy. Files go to a managed folder outside the project.</p>
      <p class="review-label">Registries</p>
      <ul v-if="install.registries.length" class="review-list" aria-label="Allowlisted registries"><li v-for="(item, index) in install.registries" :key="index">{{ item }}</li></ul>
      <p v-else class="review-note">No registry was reported.</p>
      <template v-if="install.effects.length">
        <p class="review-label">Effects</p>
        <ul class="review-list" aria-label="Planned effects"><li v-for="(item, index) in install.effects" :key="index">{{ item }}</li></ul>
      </template>
      <details class="review-details">
        <summary>Technical details</summary>
        <p class="review-untrusted">{{ untrusted }}</p>
        <dl class="review-facts" aria-label="Install plan"><template v-for="[name, value] in install.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
      </details>
      <template v-if="installSettled">
        <p class="review-label">Install record</p>
        <dl class="review-facts" aria-label="Install record"><template v-for="[name, value] in installSettled.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
        <template v-if="installSettled.egress.length">
          <p class="review-label">Egress log (host · decision · bytes)</p>
          <ul class="review-list" aria-label="Egress log"><li v-for="(item, index) in installSettled.egress" :key="index">{{ item }}</li></ul>
          <p v-if="installSettled.egressHidden" class="review-note">{{ installSettled.egressHidden }} more connections not shown.</p>
        </template>
      </template>
    </template>

    <template v-else-if="kind === 'command'">
      <p v-if="guardReview" class="review-why">{{ guardReview }}</p>
      <template v-if="command.planned">
        <p class="review-title"><strong>Typed command plan</strong> · executable pinned at review and re-verified before it runs</p>
        <template v-for="(step, index) in command.steps" :key="index">
          <p class="review-label">Step {{ index + 1 }} argv (exact, no shell)</p><pre class="review-pre">{{ step.argv }}</pre>
        </template>
        <details class="review-details">
          <summary>Technical details</summary>
          <p class="review-untrusted">{{ untrusted }}</p>
          <dl v-for="(step, index) in command.steps" :key="index" class="review-facts" :aria-label="'Step ' + (index + 1)"><template v-for="[name, value] in step.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
          <dl class="review-facts" aria-label="Command plan"><template v-for="[name, value] in command.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
        </details>
        <template v-if="command.destinations.length">
          <p class="review-label">Network destinations (the only hosts it can reach)</p>
          <ul class="review-list"><li v-for="item in command.destinations" :key="item">{{ item }}</li></ul>
        </template>
        <template v-if="command.credentials.length">
          <p class="review-badge review-alert">Uses saved command credentials. The value is added by Pebble's broker on these routes only and is never visible to the command.</p>
          <ul class="review-list"><li v-for="item in command.credentials" :key="item">{{ item }}</li></ul>
        </template>
        <p v-if="command.interactive" class="review-note">Interactive: the model may type into this command's terminal, except at password prompts, which only you can answer.</p>
      </template>
      <template v-else>
        <p class="review-note">Unplanned command: the text could not be expressed as an exact argv plan, so its executable is resolved when it runs. Review the command text above carefully.</p>
        <details class="review-details"><summary>Technical details</summary><p class="review-untrusted">{{ untrusted }}</p></details>
      </template>
    </template>

    <template v-else>
      <p class="review-title"><strong>Test recipe {{ tests.recipe }}</strong></p>
      <p class="review-label">Command (argv)</p>
      <pre class="review-pre">{{ tests.argv }}</pre>
      <template v-if="tests.script"><p class="review-label">npm script (plain text)</p><pre class="review-pre">{{ tests.script }}</pre></template>
      <details class="review-details">
        <summary>Technical details</summary>
        <p class="review-untrusted">{{ untrusted }}</p>
        <dl class="review-facts" aria-label="Recipe plan"><template v-for="[name, value] in tests.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
        <template v-if="tests.env.length">
          <p class="review-label">Environment ({temp} is a fresh per-run temporary folder)</p>
          <dl class="review-facts" aria-label="Environment variables"><template v-for="[name, value] in tests.env" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
        </template>
        <template v-if="tests.reads.length">
          <p class="review-label">Toolchain reads</p>
          <ul class="review-list" aria-label="Toolchain reads"><li v-for="(item, index) in tests.reads" :key="index">{{ item }}</li></ul>
        </template>
        <dl v-if="tests.executable.length" class="review-facts" aria-label="Pinned executable"><template v-for="[name, value] in tests.executable" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
        <template v-if="tests.mount">
          <p class="review-label">Read-only environment mount</p>
          <dl class="review-facts" aria-label="Environment mount"><template v-for="[name, value] in tests.mount.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
          <ul v-if="tests.mount.reads.length" class="review-list" aria-label="Mount reads"><li v-for="(item, index) in tests.mount.reads" :key="index">Reads {{ item }}</li></ul>
          <ul v-if="tests.mount.links.length" class="review-list" aria-label="Mount links"><li v-for="[name, value] in tests.mount.links" :key="name">Links {{ name }} → {{ value }}</li></ul>
        </template>
      </details>
      <template v-if="testsSettled">
        <p class="review-label">Result</p>
        <dl class="review-facts" aria-label="Test result"><template v-for="[name, value] in testsSettled.facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
        <template v-if="testsSettled.drafts.length">
          <ul class="review-list" aria-label="Drafts included"><li v-for="(item, index) in testsSettled.drafts" :key="index">{{ item }}</li></ul>
          <p v-if="testsSettled.draftsHidden" class="review-note">{{ testsSettled.draftsHidden }} more not shown.</p>
        </template>
      </template>
    </template>
  </section>
</template>

<style scoped>
/* Phase 24 (V1-040): the most safety-critical reading in the app is readable: 12 px body and monospace, 11 px badges. */
.action-review { margin: 10px 0; min-width: 0; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.review-untrusted, .review-note { margin: 6px 0; color: var(--secondary); }
.review-title { margin: 6px 0; font-size: 13px; }
.review-lead { margin: 6px 0; color: var(--text); }
.review-why { margin: 6px 0; padding: 6px 8px; border-left: 2px solid var(--accent); color: var(--text); }
.review-text, .review-pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.review-pre { margin: 4px 0; padding: 8px; max-height: 240px; overflow: auto; border-radius: 6px; background: var(--code); font: 12px/1.6 ui-monospace, monospace; }
.review-badges { display: flex; flex-wrap: wrap; gap: 5px; margin: 8px 0; }
.review-badge { padding: 2px 7px; border-radius: 4px; background: var(--hover); color: var(--secondary); font-size: 11px; }
.review-alert { border: 1px solid var(--danger); color: var(--danger); font-weight: 600; }
.review-details { margin: 8px 0; }
.review-details > summary { cursor: pointer; color: var(--secondary); }
.review-facts { display: grid; grid-template-columns: max-content 1fr; gap: 3px 10px; margin: 8px 0; }
.review-facts dt { color: var(--muted); }
.review-facts dd { margin: 0; overflow-wrap: anywhere; }
.review-label { margin: 8px 0 4px; font-weight: 600; }
.review-list { margin: 0; padding-left: 16px; }
</style>
