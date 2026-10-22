<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from 'vue';
import type { Ref } from 'vue';
import { native, rpc } from '../bridge';

// The late-slot policy is sent only when it is not the host default (run once at the latest missed time).
type Policy = { catch_up?: 'skip_if_late'; grace_seconds?: number };
type Schedule = ({ kind: 'once'; at: number } | { kind: 'interval'; seconds: number } | { kind: 'daily'; time: string; timezone: string }) & Policy;
interface Job { id: string; name: string; task: string; schedule: Schedule; state: string; revision: number; next_fire?: number | null; error?: string | null; last_run_id?: string | null; last_run_state?: string | null }
interface JobNotification { id: string; title: string; body?: string; status: string; created?: number; source?: string; body_origin?: string }
interface JobList { jobs: Job[]; notifications: JobNotification[]; service?: { running: boolean; execution: string } }
interface Worktree { id: string; state: string; path?: string; base?: string; dirty?: boolean; error?: string | null }
interface Review { review_id: string; summary: unknown; base?: unknown; root?: unknown; expires?: number | string }
interface ConfigReview { config_fingerprint: string; config: Record<string, unknown>; next_times: string[] }
interface Draft extends ConfigReview { version: number; payload: { name: string; task: string; schedule: Schedule; monitor: boolean; limits: { max_steps: number; max_seconds: number } } }

const props = defineProps<{ chatId: string; workspace?: string | null; refreshKey?: unknown }>();
const jobStates = new Set(['active', 'running', 'paused', 'needs_review', 'finished', 'completed', 'failed']);
const pausable = new Set(['active', 'running', 'needs_review']), resumable = new Set(['paused', 'needs_review']);
const worktreeStates = new Set(['creating', 'ready', 'removing', 'uncertain', 'failed']);
const removable = new Set(['ready', 'uncertain', 'failed']);
const configFields = [['Workspace', 'workspace'], ['Model', 'model'], ['Effort', 'effort'], ['Permissions', 'permissions'], ['Limits', 'limits']] as const;
const limitsMessage = 'Choose 1–256 steps and 1–86400 seconds per run.';
const keptMessage = 'This worktree has uncommitted changes or is not ready, so it stays in place.';
const changedMessage = 'That schedule changed. Review it again.';

const jobs = shallowRef<Job[] | null>(null), notifications = shallowRef<JobNotification[]>([]), service = shallowRef<JobList['service'] | null>(null);
const status = shallowRef<unknown>(null), worktrees = shallowRef<Worktree[] | null>(null);
const draft = shallowRef<Draft | null>(null), pending = shallowRef<{ action: 'resume' | 'delete'; job: Job; review: ConfigReview | null } | null>(null);
const gitReview = shallowRef<{ kind: 'create' | 'remove'; worktree_id: string; review: Review } | null>(null);
const exported = shallowRef<{ worktree_id: string; data: unknown } | null>(null);
const exportTarget = ref(''), exportPaths = ref(''), busy = ref('');
const jobsLoading = ref(false), statusLoading = ref(false), worktreesLoading = ref(false);
const jobsError = ref(''), jobsActionError = ref(''), formError = ref(''), statusError = ref(''), worktreesError = ref(''), gitActionError = ref('');
const jobsNotice = ref(''), gitNotice = ref('');
const scheduleCard = ref<HTMLElement | null>(null), pendingCard = ref<HTMLElement | null>(null), gitCard = ref<HTMLElement | null>(null);
const form = reactive(blankForm());
let mounted = false, queued: { git: boolean } | null = null, formVersion = 0;
let scopeVersion = 0, jobsRequest = 0, statusRequest = 0, worktreesRequest = 0, actionVersion = 0;

const gitLoading = computed(() => statusLoading.value || worktreesLoading.value);
const pendingText = computed(() => pending.value?.action === 'resume'
  ? { title: 'Resume', note: 'Resuming re-pins exactly this configuration with the schedule’s own effort and limits. If it changes before you confirm, resuming is refused. Permissions still apply on each run, it fires only while the Pebble service is running, and an earlier uncertain fire is not restarted.', confirm: busy.value === 'resume' ? 'Resuming…' : 'Confirm resume', cancel: 'Cancel review', style: 'primary-button' }
  : { title: 'Delete', note: 'Deleting stops future fires. It never cancels an unrelated run.', confirm: busy.value === 'delete' ? 'Deleting…' : 'Confirm delete', cancel: 'Keep schedule', style: 'secondary-button danger' });
const gitConfirm = computed(() => gitReview.value?.kind === 'create'
  ? (busy.value === 'create-worktree' ? 'Creating…' : 'Create reviewed worktree')
  : (busy.value === 'remove-worktree' ? 'Removing…' : 'Remove reviewed clean worktree'));

function localZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; }
}
function blankForm() {
  return { name: '', task: '', kind: 'interval' as 'once' | 'interval' | 'daily', at: '', minutes: '60' as string | number, time: '09:00', timezone: localZone(), monitor: false, max_steps: '64' as string | number, max_seconds: '900' as string | number, catch_up: 'latest' as 'latest' | 'skip_if_late', grace_minutes: '120' as string | number };
}
function scopeKey() { return props.chatId + '\u0000' + (props.workspace || ''); }
function current(version: number, scope: string) { return mounted && version === scopeVersion && scope === scopeKey(); }
function failure(cause: unknown) {
  const text = cause instanceof Error ? cause.message : String(cause);
  return (text || 'The request failed.').slice(0, 1000);
}
function display(value: unknown, limit = 20000) {
  if (value == null) return '';
  let text: string;
  if (typeof value === 'string') text = value;
  else {
    try { text = JSON.stringify(value, null, 2) ?? String(value); } catch { return 'Display data is unavailable.'; }
  }
  return text.length > limit ? text.slice(0, limit) + '\n… (truncated for display)' : text;
}
// Phase 24 (V1-114): Git status as a file list in words; the raw record stays under Technical details.
const STATUS_WORDS: Record<string, string> = { M: 'modified', A: 'added', D: 'deleted', R: 'renamed', C: 'copied', T: 'type changed', U: 'in conflict' };
function fileState(entry: Record<string, unknown>) {
  if (entry.kind === 'untracked') return 'new, not tracked yet';
  if (entry.kind === 'unmerged') return 'in conflict';
  const staged = typeof entry.index === 'string' ? entry.index : '.', unstaged = typeof entry.worktree === 'string' ? entry.worktree : '.';
  const parts = [...(staged !== '.' ? [(STATUS_WORDS[staged] || 'changed') + ', staged'] : []),
    ...(unstaged !== '.' ? [(STATUS_WORDS[unstaged] || 'changed') + (staged !== '.' ? ', not staged' : '')] : [])];
  return parts.join('; ') || 'changed';
}
const statusView = computed(() => {
  const value = status.value;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  const entries = (Array.isArray(v.entries) ? v.entries : []).filter((e): e is Record<string, unknown> => !!e && typeof e === 'object' && typeof (e as Record<string, unknown>).path === 'string').slice(0, 200);
  const total = typeof v.total === 'number' && Number.isFinite(v.total) ? v.total : entries.length;
  const offset = typeof v.offset === 'number' && Number.isFinite(v.offset) ? v.offset : 0;
  return {
    branch: typeof v.branch === 'string' ? v.branch.slice(0, 200) : '',
    upstream: typeof v.upstream === 'string' ? v.upstream.slice(0, 200) : '',
    ahead: typeof v.ahead === 'number' ? v.ahead : 0, behind: typeof v.behind === 'number' ? v.behind : 0,
    clean: v.clean === true,
    files: entries.map((e) => ({ path: String(e.path).slice(0, 500), state: fileState(e) + (e.submodule === true ? ' (submodule)' : '') })),
    more: Math.max(0, total - offset - entries.length),
  };
});
function compact(value: unknown) {
  if (typeof value === 'string') return value;
  try { return (JSON.stringify(value) ?? String(value)).slice(0, 2000); } catch { return 'Display data is unavailable.'; }
}
function configRows(config: Record<string, unknown>) {
  return configFields.map(([label, key]) => [label, key in config ? compact(config[key]) : 'not reported'] as [string, string]);
}
// Only the native fingerprint and summary are kept; any other reply field is ignored.
function configReview(value: unknown): ConfigReview {
  const item = value as { config_fingerprint?: unknown; config?: unknown; next_times?: unknown } | null;
  if (!item || typeof item !== 'object' || typeof item.config_fingerprint !== 'string' || !/^[0-9a-f]{64}$/.test(item.config_fingerprint)
    || !item.config || typeof item.config !== 'object' || Array.isArray(item.config)) {
    throw new Error('Configuration review is incomplete. Nothing was changed.');
  }
  const next_times = Array.isArray(item.next_times) ? item.next_times.filter((time): time is string => typeof time === 'string').slice(0, 5) : [];
  return { config_fingerprint: item.config_fingerprint, config: item.config as Record<string, unknown>, next_times };
}
function when(value: unknown) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }
  return typeof value === 'string' && value ? value : '—';
}
function utc(seconds: number) {
  const date = new Date(seconds * 1000);
  return Number.isNaN(date.getTime()) ? String(seconds) : date.toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
}
function span(seconds: number) {
  for (const [unit, size] of [['day', 86400], ['hour', 3600], ['minute', 60]] as const) {
    if (seconds >= size && seconds % size === 0) { const count = seconds / size; return count + ' ' + unit + (count === 1 ? '' : 's'); }
  }
  return seconds + ' seconds';
}
function describe(value: unknown, exact = false) {
  const item = (value && typeof value === 'object' ? value : {}) as Record<string, unknown>;
  if (item.kind === 'once' && typeof item.at === 'number') return 'Once at ' + when(item.at) + (exact ? ' (' + utc(item.at) + ')' : '');
  if (item.kind === 'interval' && typeof item.seconds === 'number') return 'Every ' + span(item.seconds);
  if (item.kind === 'daily' && typeof item.time === 'string') return 'Daily at ' + item.time + ' (' + String(item.timezone ?? '') + ')';
  return 'Unknown schedule';
}
function catchUp(value: unknown) {
  const item = (value && typeof value === 'object' ? value : {}) as Record<string, unknown>;
  return item.catch_up === 'skip_if_late' && typeof item.grace_seconds === 'number'
    ? 'Skips a missed time that is more than ' + span(item.grace_seconds) + ' late'
    : 'Runs once at the latest missed time; earlier missed times are combined, not replayed';
}
function clockIn(seconds: number, timeZone?: string) {
  try { return new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', ...(timeZone ? { timeZone } : {}) }).format(new Date(seconds * 1000)); } catch { return ''; }
}
// A daily schedule keeps its reviewed zone; the device's own zone is shown beside it, never substituted.
function zoneTimes(value: unknown, next: unknown) {
  const item = (value && typeof value === 'object' ? value : {}) as Record<string, unknown>;
  if (item.kind !== 'daily' || typeof item.timezone !== 'string' || typeof next !== 'number' || !Number.isFinite(next) || !validZone(item.timezone)) return '';
  const there = clockIn(next, item.timezone), here = clockIn(next);
  return there && here ? there + ' ' + item.timezone + ' · ' + here + ' here' : '';
}
function stateName(value: unknown, known: Set<string>) { return typeof value === 'string' && known.has(value) ? value.replaceAll('_', ' ') : 'unknown'; }
function validJobs(value: unknown): value is JobList {
  const item = value as Partial<JobList> | null;
  return !!item && Array.isArray(item.jobs) && Array.isArray(item.notifications)
    && item.jobs.every(job => !!job && typeof job.id === 'string' && typeof job.name === 'string' && typeof job.task === 'string' && typeof job.state === 'string' && Number.isInteger(job.revision))
    && item.notifications.every(note => !!note && typeof note.id === 'string' && typeof note.title === 'string' && typeof note.status === 'string');
}
function validReview(value: unknown): value is Review {
  const item = value as Review | null;
  return !!item && typeof item === 'object' && typeof item.review_id === 'string' && item.review_id.length > 0 && item.review_id.length <= 256 && 'summary' in item;
}
function expired(review: Review) {
  const at = typeof review.expires === 'number' ? review.expires * 1000 : typeof review.expires === 'string' ? Date.parse(review.expires) : NaN;
  return Number.isFinite(at) && at <= Date.now();
}

function bounded(value: unknown, limit: number, message: string) {
  const result = String(value ?? '').trim();
  if (!result || result.length > limit) throw new Error(message);
  return result;
}
function whole(value: unknown, min: number, max: number, message: string) {
  const raw = String(value ?? '').trim();
  const result = /^\d{1,9}$/.test(raw) ? Number(raw) : NaN;
  if (!(result >= min && result <= max)) throw new Error(message);
  return result;
}
function validZone(value: string) {
  if (value.length > 64 || !/^[A-Za-z][A-Za-z0-9_+-]*(?:\/[A-Za-z0-9_+-]+){0,2}$/.test(value)) return false;
  try { new Intl.DateTimeFormat('en-US', { timeZone: value }); return true; } catch { return false; }
}
function policy(): Policy {
  if (form.catch_up !== 'skip_if_late') return {};
  return { catch_up: 'skip_if_late', grace_seconds: whole(form.grace_minutes, 1, 1440, 'Choose a late limit of 1–1440 minutes.') * 60 };
}
function schedule(): Schedule {
  if (form.kind === 'once') {
    const at = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(form.at) ? Math.floor(new Date(form.at).getTime() / 1000) : NaN;
    if (!Number.isFinite(at) || at <= Date.now() / 1000) throw new Error('Choose a future date and time.');
    return { kind: 'once', at, ...policy() };
  }
  if (form.kind === 'daily') {
    const time = String(form.time).trim(), timezone = String(form.timezone).trim();
    if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time)) throw new Error('Use a 24-hour time such as 09:30.');
    if (!validZone(timezone)) throw new Error('Use an IANA time zone such as Europe/London.');
    return { kind: 'daily', time, timezone, ...policy() };
  }
  if (form.kind !== 'interval') throw new Error('Choose how often the schedule repeats.');
  return { kind: 'interval', seconds: whole(form.minutes, 1, 43200, 'Choose an interval of 1–43200 minutes.') * 60, ...policy() };
}
function relativePaths(value: string) {
  const paths = [...new Set(value.split(/\r?\n/).map(line => line.trim()).filter(Boolean))];
  if (!paths.length || paths.length > 64) throw new Error('List 1–64 relative file paths, one per line.');
  for (const path of paths) {
    const parts = path.split('/');
    if (path.length > 512 || /[\u0000-\u001f\u007f\\]/.test(path) || /^(?:~|[A-Za-z]:)/.test(path) || parts.some(part => !part || part === '.' || part === '..' || part.toLowerCase() === '.git')) {
      throw new Error('Use paths relative to the worktree, such as src/app.ts: ' + path.slice(0, 120));
    }
  }
  return paths;
}

async function loadJobs() {
  if (!mounted || !native) return;
  const version = scopeVersion, scope = scopeKey(), request = ++jobsRequest;
  const live = () => current(version, scope) && request === jobsRequest;
  jobsLoading.value = true; jobsError.value = '';
  try {
    const result = await rpc<unknown>('job_list', { id: props.chatId });
    if (!live()) return;
    if (!validJobs(result)) throw new Error('Schedule data is incomplete.');
    jobs.value = result.jobs; notifications.value = result.notifications;
    service.value = result.service && typeof result.service.running === 'boolean' ? result.service : null;
    const open = pending.value, job = open && result.jobs.find(item => item.id === open.job.id);
    if (open && (!job || job.revision !== open.job.revision || (open.action === 'resume' && !resumable.has(job.state)))) {
      pending.value = null; jobsActionError.value = changedMessage;
    }
  } catch (cause) {
    if (live()) jobsError.value = failure(cause);
  } finally {
    if (live()) jobsLoading.value = false;
  }
}
async function loadStatus() {
  if (!mounted || !native || !props.workspace) return;
  const version = scopeVersion, scope = scopeKey(), request = ++statusRequest;
  const live = () => current(version, scope) && request === statusRequest;
  statusLoading.value = true; statusError.value = '';
  try {
    const result = await rpc<unknown>('git_status', { id: props.chatId });
    if (live()) status.value = result ?? null;
  } catch (cause) {
    if (live()) { status.value = null; statusError.value = failure(cause); }
  } finally {
    if (live()) statusLoading.value = false;
  }
}
async function loadWorktrees() {
  if (!mounted || !native || !props.workspace) return;
  const version = scopeVersion, scope = scopeKey(), request = ++worktreesRequest;
  const live = () => current(version, scope) && request === worktreesRequest;
  worktreesLoading.value = true; worktreesError.value = '';
  try {
    const result = await rpc<{ worktrees?: Worktree[] } | null>('git_worktrees', { id: props.chatId });
    if (!live()) return;
    const items = result?.worktrees;
    if (!Array.isArray(items) || !items.every(item => !!item && typeof item.id === 'string' && typeof item.state === 'string')) throw new Error('Worktree data is incomplete.');
    worktrees.value = items;
    const review = gitReview.value, listed = review && items.find(item => item.id === review.worktree_id);
    if (review?.kind === 'remove' && (!listed || listed.dirty === true || !removable.has(listed.state))) {
      gitReview.value = null; gitActionError.value = 'That worktree changed or has uncommitted changes. It was not removed.';
    }
    if (exportTarget.value && !items.some(item => item.id === exportTarget.value && item.state === 'ready')) { exportTarget.value = ''; exportPaths.value = ''; }
    if (exported.value && !items.some(item => item.id === exported.value?.worktree_id)) exported.value = null;
  } catch (cause) {
    if (live()) worktreesError.value = failure(cause);
  } finally {
    if (live()) worktreesLoading.value = false;
  }
}
async function loadGit() { await Promise.all([loadStatus(), loadWorktrees()]); }

// One native mutation or review at a time; replies from an earlier scope never touch this one.
async function perform<T>(key: string, issue: Ref<string>, work: (chat: string) => Promise<T>, done: (result: T) => void, after?: () => unknown) {
  if (busy.value || !mounted || !native) return;
  const version = scopeVersion, scope = scopeKey(), request = ++actionVersion;
  const live = () => current(version, scope) && request === actionVersion;
  busy.value = key; issue.value = ''; jobsNotice.value = ''; gitNotice.value = '';
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
function reviewSchedule() {
  if (busy.value || !mounted) return;
  draft.value = null; formError.value = ''; jobsNotice.value = '';
  let payload: Draft['payload'];
  try {
    payload = {
      name: bounded(form.name, 80, 'Name the schedule in up to 80 characters.'),
      task: bounded(form.task, 4000, 'Describe the task in up to 4000 characters.'),
      schedule: schedule(),
      monitor: form.monitor === true,
      limits: { max_steps: whole(form.max_steps, 1, 256, limitsMessage), max_seconds: whole(form.max_seconds, 1, 86400, limitsMessage) },
    };
  } catch (cause) {
    formError.value = failure(cause); return;
  }
  // The host computes the configuration fingerprint; an edit while it does so drops the reply.
  const edition = formVersion;
  return perform('preview-schedule', formError, chat => rpc<unknown>('job_preview', { id: chat, schedule: payload.schedule, limits: payload.limits }), result => {
    if (edition !== formVersion) { formError.value = 'The schedule changed while it was being reviewed. Review it again.'; return; }
    draft.value = { version: scopeVersion, payload, ...configReview(result) };
    void nextTick(() => scheduleCard.value?.focus());
  });
}
function createSchedule() {
  const reviewed = draft.value;
  if (!reviewed || reviewed.version !== scopeVersion) return;
  return perform('create', formError, chat => rpc('job_create', { id: chat, ...reviewed.payload, config_fingerprint: reviewed.config_fingerprint }), () => {
    if (draft.value === reviewed) Object.assign(form, blankForm());
    jobsNotice.value = 'Schedule created. Permissions still apply on each run.';
  }, () => { if (draft.value === reviewed) draft.value = null; return loadJobs(); });
}
function pause(job: Job) {
  return perform('pause:' + job.id, jobsActionError, chat => rpc('job_pause', { id: chat, job_id: job.id, revision: job.revision }), () => { jobsNotice.value = 'Schedule paused.'; }, loadJobs);
}
function reviewResume(job: Job) {
  let shown = false;
  return perform('preview-resume:' + job.id, jobsActionError, chat => { pending.value = null; return rpc<unknown>('job_preview_resume', { id: chat, job_id: job.id, revision: job.revision }); }, result => {
    const review = configReview(result), listed = jobs.value?.find(item => item.id === job.id);
    if (!listed || listed.revision !== job.revision || !resumable.has(listed.state)) throw new Error(changedMessage);
    pending.value = { action: 'resume', job, review }; shown = true;
    void nextTick(() => pendingCard.value?.focus());
  }, () => shown ? undefined : loadJobs());
}
function openDelete(job: Job) {
  if (busy.value) return;
  jobsActionError.value = ''; jobsNotice.value = '';
  pending.value = { action: 'delete', job, review: null };
  void nextTick(() => pendingCard.value?.focus());
}
function confirmPending() {
  const reviewed = pending.value;
  if (!reviewed) return;
  const { id: job_id, revision } = reviewed.job, resume = reviewed.action === 'resume', config_fingerprint = reviewed.review?.config_fingerprint;
  if (resume && !config_fingerprint) return;
  return perform(reviewed.action, jobsActionError, chat => resume ? rpc('job_resume', { id: chat, job_id, revision, config_fingerprint }) : rpc('job_delete', { id: chat, job_id, revision }), () => {
    jobsNotice.value = resume ? 'Schedule resumed with the reviewed configuration.' : 'Schedule deleted.';
  }, () => { if (pending.value === reviewed) pending.value = null; return loadJobs(); });
}
function acknowledge(item: JobNotification) {
  return perform('ack:' + item.id, jobsActionError, chat => rpc('notification_ack', { id: chat, notification_id: item.id }), () => undefined, loadJobs);
}
function showReview(kind: 'create' | 'remove', worktree_id: string, review: unknown) {
  if (!validReview(review)) throw new Error('Review data is incomplete. Nothing was changed.');
  gitReview.value = { kind, worktree_id, review };
  void nextTick(() => gitCard.value?.focus());
}
function previewCreate() {
  if (!props.workspace) return;
  return perform('preview-create', gitActionError, chat => { gitReview.value = null; return rpc<unknown>('git_preview_create', { id: chat }); }, review => showReview('create', '', review));
}
function previewRemove(item: Worktree) {
  if (busy.value || !props.workspace) return;
  if (item.dirty === true || !removable.has(item.state)) { gitActionError.value = keptMessage; return; }
  return perform('preview-remove:' + item.id, gitActionError, chat => { gitReview.value = null; return rpc<unknown>('git_preview_remove', { id: chat, worktree_id: item.id }); }, review => {
    const listed = worktrees.value?.find(entry => entry.id === item.id);
    if (!listed || listed.dirty === true) throw new Error(keptMessage);
    showReview('remove', item.id, review);
  });
}
function confirmGit() {
  const reviewed = gitReview.value;
  if (busy.value || !reviewed || !props.workspace) return;
  if (expired(reviewed.review)) { gitReview.value = null; gitActionError.value = 'This review expired. Review it again.'; return; }
  const create = reviewed.kind === 'create', listed = worktrees.value?.find(entry => entry.id === reviewed.worktree_id);
  if (!create && (!listed || listed.dirty === true)) { gitReview.value = null; gitActionError.value = keptMessage; return; }
  const review_id = reviewed.review.review_id;
  return perform(create ? 'create-worktree' : 'remove-worktree', gitActionError,
    chat => create ? rpc('git_create_worktree', { id: chat, review_id }) : rpc('git_remove_worktree', { id: chat, review_id }),
    () => { gitNotice.value = create ? 'Worktree created.' : 'Worktree removed.'; },
    () => { if (gitReview.value === reviewed) gitReview.value = null; return loadGit(); });
}
function openExport(item: Worktree) {
  if (busy.value) return;
  exportTarget.value = exportTarget.value === item.id ? '' : item.id;
  exportPaths.value = ''; gitActionError.value = '';
}
function exportFiles() {
  const target = exportTarget.value;
  if (busy.value || !target || !props.workspace) return;
  let file_paths: string[];
  try { file_paths = relativePaths(exportPaths.value); } catch (cause) { gitActionError.value = failure(cause); return; }
  return perform('export', gitActionError, chat => { exported.value = null; return rpc<unknown>('git_export', { id: chat, worktree_id: target, file_paths }); }, data => {
    exported.value = { worktree_id: target, data };
    gitNotice.value = 'Export proposal shown below as data. Nothing was applied.';
  });
}

function queueRefresh(git: boolean) {
  if (!mounted) return;
  if (queued) { queued.git ||= git; return; }
  const next = queued = { git };
  // Coalesce prop updates so a half-switched chat/workspace pair never reaches native.
  void nextTick(() => {
    if (queued === next) queued = null;
    if (!mounted) return;
    void loadJobs();
    if (next.git) void loadGit();
  });
}
function reset() {
  scopeVersion++; jobsRequest++; statusRequest++; worktreesRequest++; actionVersion++;
  jobs.value = null; notifications.value = []; service.value = null; status.value = null; worktrees.value = null;
  draft.value = null; pending.value = null; gitReview.value = null; exported.value = null;
  exportTarget.value = ''; exportPaths.value = ''; busy.value = '';
  jobsLoading.value = false; statusLoading.value = false; worktreesLoading.value = false;
  for (const message of [jobsError, jobsActionError, formError, statusError, worktreesError, gitActionError, jobsNotice, gitNotice]) message.value = '';
  Object.assign(form, blankForm());
  queueRefresh(true);
}
watch(scopeKey, reset, { flush: 'sync' });
watch(() => props.refreshKey, () => queueRefresh(false));
watch(form, () => { formVersion++; draft.value = null; }, { flush: 'sync' });
onMounted(() => { mounted = true; void loadJobs(); void loadGit(); });
onUnmounted(() => { mounted = false; queued = null; scopeVersion++; });
</script>

<template>
  <section class="workflow-panel" aria-label="Schedules and worktrees">
    <p v-if="!native" class="workflow-note">Schedules and worktrees are available in the Pebble desktop app.</p>
    <template v-else>
      <section class="workflow-section" aria-label="Schedules" :aria-busy="jobsLoading">
        <div class="workflow-heading"><strong>Schedules</strong><button class="secondary-button" :disabled="jobsLoading || !!busy" @click="loadJobs">{{ jobsLoading ? 'Refreshing schedules…' : 'Refresh schedules' }}</button></div>
        <p class="workflow-note">Schedules in this chat fire only while the Pebble service is running. Permissions still apply on each run; nothing is approved in advance. Pausing or deleting a schedule never cancels an unrelated run. After sleep, a schedule runs once at its latest missed time unless it skips late times.</p>
        <p v-if="service" class="workflow-note" role="status">{{ service.running ? 'Pebble service is running.' : 'Pebble service is not running, so schedules will not fire until it starts.' }}</p>
        <p v-if="jobsLoading && !jobs" role="status">Loading schedules…</p>
        <p v-if="jobsError" role="alert">{{ jobsError }}</p>
        <p v-if="jobsActionError" role="alert">{{ jobsActionError }}</p>
        <p v-if="jobsNotice" role="status">{{ jobsNotice }}</p>
        <ul v-if="notifications.length" class="workflow-list" aria-label="Schedule notifications">
          <li v-for="item in notifications" :key="item.id">
            <div class="workflow-row"><strong>{{ item.title }}</strong><small>{{ item.status === 'acknowledged' ? 'Acknowledged' : 'New' }} · {{ when(item.created) }}<template v-if="item.source === 'host'"> · From Pebble</template><template v-else-if="item.body_origin === 'run_output'"> · Run output, shown as text</template></small></div>
            <p v-if="item.body" class="workflow-text">{{ item.body }}</p>
            <button v-if="item.status !== 'acknowledged'" class="secondary-button" :disabled="!!busy" :aria-label="'Acknowledge ' + item.title" @click="acknowledge(item)">{{ busy === 'ack:' + item.id ? 'Acknowledging…' : 'Acknowledge' }}</button>
          </li>
        </ul>
        <p v-if="jobs && !jobs.length" class="workflow-note">No schedules in this chat.</p>
        <ul v-if="jobs?.length" class="workflow-list" aria-label="Schedules in this chat">
          <li v-for="job in jobs" :key="job.id">
            <div class="workflow-row"><strong>{{ job.name }}</strong><span>{{ stateName(job.state, jobStates) }}</span></div>
            <small>{{ describe(job.schedule) }} · Next: {{ when(job.next_fire) }}<template v-if="zoneTimes(job.schedule, job.next_fire)"> ({{ zoneTimes(job.schedule, job.next_fire) }})</template><template v-if="job.schedule?.catch_up === 'skip_if_late'"> · {{ catchUp(job.schedule) }}</template> · Revision {{ job.revision }}<template v-if="job.last_run_id"> · Last run: {{ job.last_run_id }}<template v-if="job.last_run_state"> ({{ job.last_run_state.replaceAll('_', ' ') }})</template></template></small>
            <p class="workflow-text">{{ job.task }}</p>
            <p v-if="job.error" class="workflow-note">Last error: {{ job.error }}</p>
            <div class="workflow-actions">
              <button v-if="pausable.has(job.state)" class="secondary-button" :disabled="!!busy" :aria-label="'Pause ' + job.name" @click="pause(job)">{{ busy === 'pause:' + job.id ? 'Pausing…' : 'Pause' }}</button>
              <button v-if="resumable.has(job.state)" class="secondary-button" :disabled="!!busy" :aria-label="'Review and resume ' + job.name" @click="reviewResume(job)">{{ busy === 'preview-resume:' + job.id ? 'Reviewing…' : 'Review and resume' }}</button>
              <button class="secondary-button danger" :disabled="!!busy" :aria-label="'Delete ' + job.name" @click="openDelete(job)">Delete</button>
            </div>
          </li>
        </ul>
        <article v-if="pending" ref="pendingCard" tabindex="-1" class="workflow-review" :aria-label="pendingText.title + ' ' + pending.job.name">
          <p><strong>{{ pendingText.title }} {{ pending.job.name }}</strong></p>
          <p class="workflow-note">{{ describe(pending.job.schedule) }} · Revision {{ pending.job.revision }}</p>
          <p class="workflow-text">{{ pending.job.task }}</p>
          <template v-if="pending.review">
            <dl class="workflow-facts" aria-label="Configuration to re-pin">
              <template v-for="[label, value] in configRows(pending.review.config)" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></template>
              <dt>Configuration fingerprint</dt><dd>{{ pending.review.config_fingerprint }}</dd>
            </dl>
            <details open><summary>Exact configuration summary</summary><pre>{{ display(pending.review.config) }}</pre></details>
          </template>
          <p class="workflow-note">{{ pendingText.note }}</p>
          <div class="workflow-actions"><button :class="pendingText.style" :disabled="!!busy" @click="confirmPending">{{ pendingText.confirm }}</button><button class="secondary-button" :disabled="!!busy" @click="pending = null">{{ pendingText.cancel }}</button></div>
        </article>
        <form class="workflow-form" aria-label="New schedule" @submit.prevent="reviewSchedule">
          <fieldset :disabled="!!busy && busy !== 'preview-schedule'">
            <legend>New schedule</legend>
            <label>Name<input v-model="form.name" type="text" maxlength="80" autocomplete="off" /></label>
            <label>Task<textarea v-model="form.task" rows="3" maxlength="4000" /></label>
            <fieldset class="workflow-choice">
              <legend>Repeat</legend>
              <label><input v-model="form.kind" type="radio" value="once" />Once</label>
              <label><input v-model="form.kind" type="radio" value="interval" />Every interval</label>
              <label><input v-model="form.kind" type="radio" value="daily" />Daily</label>
            </fieldset>
            <label v-if="form.kind === 'once'">Date and time<input v-model="form.at" type="datetime-local" /></label>
            <label v-if="form.kind === 'interval'">Interval in minutes<input v-model="form.minutes" type="number" min="1" max="43200" step="1" /></label>
            <div v-if="form.kind === 'daily'" class="workflow-limits">
              <label>Time<input v-model="form.time" type="time" /></label>
              <label>Time zone<input v-model="form.timezone" type="text" maxlength="64" spellcheck="false" autocomplete="off" /></label>
            </div>
            <fieldset class="workflow-choice">
              <legend>If a time is missed while Pebble is asleep or not running</legend>
              <label><input v-model="form.catch_up" type="radio" value="latest" />Run once at the latest missed time</label>
              <label><input v-model="form.catch_up" type="radio" value="skip_if_late" />Skip a time that is too late</label>
            </fieldset>
            <label v-if="form.catch_up === 'skip_if_late'">Late limit in minutes<input v-model="form.grace_minutes" type="number" min="1" max="1440" step="1" /></label>
            <div class="workflow-limits">
              <label>Step limit<input v-model="form.max_steps" type="number" min="1" max="256" step="1" /></label>
              <label>Active seconds<input v-model="form.max_seconds" type="number" min="1" max="86400" step="1" /></label>
            </div>
            <label class="workflow-check"><input v-model="form.monitor" type="checkbox" />Monitor: notify about successful runs only when the result changes</label>
            <p v-if="formError" role="alert">{{ formError }}</p>
            <button v-if="!draft" class="secondary-button" type="submit" :disabled="!!busy">{{ busy === 'preview-schedule' ? 'Reviewing…' : 'Review schedule' }}</button>
          </fieldset>
        </form>
        <article v-if="draft" ref="scheduleCard" tabindex="-1" class="workflow-review" aria-label="Schedule review">
          <p><strong>{{ draft.payload.name }}</strong></p>
          <p class="workflow-text">{{ draft.payload.task }}</p>
          <dl class="workflow-facts">
            <dt>Schedule</dt><dd>{{ describe(draft.payload.schedule, true) }}</dd>
            <dt>If a time is missed</dt><dd>{{ catchUp(draft.payload.schedule) }}</dd>
            <dt>Limits</dt><dd>{{ draft.payload.limits.max_steps }} steps · {{ draft.payload.limits.max_seconds }} seconds per run</dd>
            <dt>Notifications</dt><dd>{{ draft.payload.monitor ? 'Failures, requests for action, and successful results only when they change' : 'Every completion, failure or request for action' }}</dd>
            <template v-if="draft.next_times.length"><dt>Next fires</dt><dd>{{ draft.next_times.join(' · ') }}</dd></template>
          </dl>
          <dl class="workflow-facts" aria-label="Configuration to pin">
            <template v-for="[label, value] in configRows(draft.config)" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></template>
            <dt>Configuration fingerprint</dt><dd>{{ draft.config_fingerprint }}</dd>
          </dl>
          <details open><summary>Exact configuration summary</summary><pre>{{ display(draft.config) }}</pre></details>
          <p class="workflow-note">Creating pins exactly this configuration. If it changes before you create, the service refuses and asks for a fresh review; if it changes later, the schedule waits for your review before it runs again. Permissions still apply on each run; nothing is approved in advance. It fires only while the Pebble service is running.</p>
          <div class="workflow-actions"><button class="primary-button" :disabled="!!busy" @click="createSchedule">{{ busy === 'create' ? 'Creating…' : 'Create reviewed schedule' }}</button><button class="secondary-button" :disabled="!!busy" @click="draft = null">Edit schedule</button></div>
        </article>
      </section>

      <section class="workflow-section" aria-label="Git worktrees" :aria-busy="gitLoading">
        <div class="workflow-heading"><strong>Git worktrees</strong><button v-if="workspace" class="secondary-button" :disabled="gitLoading || !!busy" @click="loadGit">{{ gitLoading ? 'Refreshing Git…' : 'Refresh Git' }}</button></div>
        <p v-if="!workspace" class="workflow-note">Choose a workspace folder for this chat to inspect Git and manage worktrees.</p>
        <template v-else>
          <p class="workflow-note">Inspection is read only. New worktrees are detached at the reviewed base commit; uncommitted changes in your workspace stay where they are and are not copied. The main task can read worktrees and use exported files only through the reviewed file broker.</p>
          <p v-if="gitLoading && !worktrees" role="status">Loading Git…</p>
          <p v-if="statusError" role="alert">Git status: {{ statusError }}</p>
          <section v-if="statusView" class="git-status" aria-label="Git status">
            <p class="workflow-note">{{ statusView.branch ? 'On branch ' + statusView.branch : 'On a detached checkout' }}<template v-if="statusView.upstream"> · {{ statusView.ahead }} ahead of and {{ statusView.behind }} behind {{ statusView.upstream }}</template> · file names are repository data</p>
            <p v-if="statusView.clean" class="workflow-note">No changes: the files match the last commit.</p>
            <ul v-else-if="statusView.files.length" class="workflow-list" aria-label="Changed files">
              <li v-for="file in statusView.files" :key="file.path"><code>{{ file.path }}</code> · {{ file.state }}</li>
            </ul>
            <p v-if="statusView.more" class="workflow-note">{{ statusView.more }} more file{{ statusView.more === 1 ? '' : 's' }} not listed.</p>
          </section>
          <details v-if="status != null"><summary>Technical details</summary><pre>{{ display(status) }}</pre></details>
          <p v-if="worktreesError" role="alert">{{ worktreesError }}</p>
          <p v-if="gitActionError" role="alert">{{ gitActionError }}</p>
          <p v-if="gitNotice" role="status">{{ gitNotice }}</p>
          <p v-if="worktrees && !worktrees.length" class="workflow-note">No managed worktrees for this workspace.</p>
          <ul v-if="worktrees?.length" class="workflow-list" aria-label="Managed worktrees">
            <li v-for="item in worktrees" :key="item.id">
              <div class="workflow-row"><strong>{{ item.id }}</strong><span>{{ stateName(item.state, worktreeStates) }}{{ item.dirty === true ? ' · uncommitted changes' : item.dirty === false ? ' · clean' : '' }}</span></div>
              <small>Base: {{ item.base || 'unknown' }}<template v-if="item.path"> · {{ item.path }}</template></small>
              <p v-if="item.error" class="workflow-note">Last error: {{ item.error }}</p>
              <p v-if="item.dirty === true" class="workflow-note">Removal is unavailable while this worktree has uncommitted changes. Export the files you need first.</p>
              <div class="workflow-actions">
                <button v-if="item.state === 'ready'" class="secondary-button" :disabled="!!busy" :aria-expanded="exportTarget === item.id" :aria-label="'Export files from ' + item.id" @click="openExport(item)">Export files</button>
                <button v-if="removable.has(item.state)" class="secondary-button danger" :disabled="!!busy || item.dirty === true" :aria-label="'Review removal of ' + item.id" @click="previewRemove(item)">{{ busy === 'preview-remove:' + item.id ? 'Reviewing…' : 'Review removal' }}</button>
              </div>
              <form v-if="exportTarget === item.id" class="workflow-form" :aria-label="'Export from ' + item.id" @submit.prevent="exportFiles">
                <label>Relative file paths, one per line<textarea v-model="exportPaths" rows="3" maxlength="40000" spellcheck="false" :disabled="!!busy" /></label>
                <div class="workflow-actions"><button class="secondary-button" type="submit" :disabled="!!busy || !exportPaths.trim()">{{ busy === 'export' ? 'Exporting…' : 'Export file proposals' }}</button><button class="secondary-button" type="button" :disabled="!!busy" @click="openExport(item)">Close export</button></div>
              </form>
            </li>
          </ul>
          <div class="workflow-actions"><button class="secondary-button" :disabled="!!busy" @click="previewCreate">{{ busy === 'preview-create' ? 'Reviewing…' : 'Review new worktree' }}</button></div>
          <article v-if="gitReview" ref="gitCard" tabindex="-1" class="workflow-review" :aria-label="gitReview.kind === 'create' ? 'New worktree review' : 'Worktree removal review'">
            <p><strong>{{ gitReview.kind === 'create' ? 'Create a detached worktree' : 'Remove worktree ' + gitReview.worktree_id }}</strong></p>
            <pre>{{ display(gitReview.review.summary) }}</pre>
            <dl class="workflow-facts">
              <dt>Base</dt><dd>{{ display(gitReview.review.base) || 'unknown' }}</dd>
              <dt>Root</dt><dd>{{ display(gitReview.review.root) || 'unknown' }}</dd>
              <dt>Review expires</dt><dd>{{ when(gitReview.review.expires) }}</dd>
              <dt>Review ID</dt><dd>{{ gitReview.review.review_id }}</dd>
            </dl>
            <p class="workflow-note">{{ gitReview.kind === 'create' ? 'Your workspace’s uncommitted changes stay in place and are not copied.' : 'Only a clean worktree is removed. If it has uncommitted changes, removal is refused; nothing is forced.' }}</p>
            <div class="workflow-actions"><button :class="gitReview.kind === 'create' ? 'primary-button' : 'secondary-button danger'" :disabled="!!busy" @click="confirmGit">{{ gitConfirm }}</button><button class="secondary-button" :disabled="!!busy" @click="gitReview = null">Discard review</button></div>
          </article>
          <article v-if="exported" class="workflow-review" aria-label="Export proposal">
            <p><strong>Export proposal from {{ exported.worktree_id }}</strong></p>
            <p class="workflow-note">Untrusted proposal data, shown as text. Nothing was applied, committed or pushed, and this panel has no control to do so.</p>
            <pre>{{ display(exported.data, 200000) }}</pre>
          </article>
        </template>
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
.workflow-text, .workflow-panel pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.workflow-panel pre { font: 12px ui-monospace, monospace; max-height: 240px; overflow: auto; }
.workflow-review { border: 1px solid var(--border, #b9b9b9); border-radius: 8px; padding: 12px; margin: 8px 0; }
.workflow-form fieldset { border: 0; padding: 0; margin: 8px 0; min-width: 0; }
.workflow-form legend { font-weight: 600; margin-bottom: 8px; }
.workflow-form label { display: block; margin: 8px 0; }
.workflow-form input:not([type=radio]):not([type=checkbox]), .workflow-form textarea { display: block; width: 100%; box-sizing: border-box; margin-top: 4px; font: inherit; color: inherit; background: transparent; border: 1px solid var(--border, #b9b9b9); border-radius: 6px; padding: 6px 8px; }
.workflow-choice label { display: inline-flex; gap: 6px; align-items: center; margin-right: 12px; }
.workflow-form .workflow-check { display: flex; gap: 8px; align-items: center; }
.workflow-limits { display: flex; gap: 8px; }
.workflow-limits label { flex: 1; }
.workflow-facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: 12px; }
.workflow-facts dt { opacity: .75; }
.workflow-facts dd { margin: 0; overflow-wrap: anywhere; }
</style>
