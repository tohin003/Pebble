<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { rpc } from "../bridge";
import { moveRowFocus } from "../shortcuts";

// The person's goal for this conversation (design study §4.4). Goals are
// operational records, not memory. Every change is a native click sent with the
// revision the person saw; the host refuses stale revisions, unmet criteria and
// cyclic steps. Agent-authored text is labelled and always shown as plain text.
interface Receipt { run_id: string; call_id: string }
interface Criterion {
  id: string; text: string; kind: string; status: string; origin: string;
  checked_by: string | null; evidence: Receipt[]; detail: string | null;
}
interface Step {
  id: string; title: string; status: string; depends_on: string[]; evidence: Receipt[];
  evidence_current: boolean | null; blocker: { kind: string; ref: string | null; active: boolean } | null; ready: boolean;
}
interface Blocker { kind: string; ref: string; label: string; step_id?: string; detail?: string; detail_origin?: string }
interface Progress { criteria_met: number; criteria_total: number; candidates: number; steps_completed: number; steps_total: number }
interface Goal {
  id: string; chat_id: string; title: string; revision: number; state: string;
  criteria: Criterion[]; steps: Step[]; blockers: Blocker[]; progress: Progress; note?: string;
}
interface GoalSummary { id: string; title: string; state: string; updated: number; progress: Progress }

const props = defineProps<{ chatId: string; refreshKey?: unknown; running?: boolean }>();
const emit = defineEmits<{ reviews: [] }>();
const CURRENT = ["active", "paused", "blocked"];
const LIST_LIMIT = 20;
// Phase 24 (V1-108): plain words where no fixture pins the old ones (GoalPanel.test.mjs pins the blockers caption,
// the step evidence count and the "Verify receipts" control).
const NOTE = "A goal is a checklist for this conversation, not a memory. Evidence links point to finished task steps; they do not prove the work is correct or grant permission.";
const goalStates: Record<string, string> = { active: "active", paused: "paused", blocked: "blocked", done: "done", abandoned: "abandoned" };
const criterionStates: Record<string, string> = { candidate: "proposed", open: "open", met: "met", unmet: "not met", rejected: "rejected" };
const stepStates: Record<string, string> = { pending: "pending", in_progress: "in progress", blocked: "blocked", completed: "completed" };
const kinds: Record<string, string> = { human_check: "Your check", evidence: "Task evidence" };
const blockerKinds: Record<string, string> = { question: "Question", approval: "Review", failure: "Failure", text: "Your note" };
const word = (table: Record<string, string>, value: unknown) =>
  typeof value === "string" && Object.prototype.hasOwnProperty.call(table, value) ? table[value] : "unknown";

const goals = ref<GoalSummary[]>([]);
const goal = ref<Goal | null>(null);
const expanded = ref(false);
const starting = ref(false);
const loading = ref(false);
const busy = ref("");
const error = ref("");
const notice = ref("");
const confirmAbandon = ref(false);
const editing = ref("");
const draft = reactive({ title: "", criterion: "", kind: "human_check", step: "", depends: [] as string[], blockStep: "", blockText: "", edit: "" });
let ticket = 0;
let alive = true;

const live = computed(() => !!goal.value && CURRENT.includes(goal.value.state));
const accepted = computed(() => goal.value?.criteria.filter((c) => !["candidate", "rejected"].includes(c.status)) || []);
const proposals = computed(() => goal.value?.criteria.filter((c) => c.status === "candidate") || []);
const rejected = computed(() => goal.value?.criteria.filter((c) => c.status === "rejected").length || 0);
const history = computed(() => goals.value.filter((g) => g.id !== goal.value?.id));
const blockable = computed(() => goal.value?.steps.filter((s) => !["completed", "blocked"].includes(s.status)) || []);
const summary = computed(() => {
  if (!goal.value) return "";
  const p = goal.value.progress, first = goal.value.blockers[0];
  return `${p.criteria_met}/${p.criteria_total} criteria · ${p.steps_completed}/${p.steps_total} steps`
    + (first ? ` · blocked: ${String(first.label).toLowerCase()}` : "");
});
const text = (e: unknown) => (e instanceof Error ? e.message : String(e));
const when = (seconds: number) => (Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleDateString() : "—");

async function load() {
  const mine = ++ticket, chat = props.chatId;
  loading.value = true;
  try {
    const list = await rpc<GoalSummary[]>("goal_list", { chat_id: chat, limit: LIST_LIMIT });
    if (!alive || mine !== ticket) return;
    if (!Array.isArray(list)) throw new Error("The goal list was malformed.");
    const current = list.find((item) => CURRENT.includes(item.state));
    const detail = current ? await rpc<Goal>("goal_get", { chat_id: chat, goal_id: current.id }) : null;
    if (!alive || mine !== ticket) return;
    if (detail && (detail.id !== current?.id || detail.chat_id !== chat))
      throw new Error("The goal reply belongs to a different conversation.");
    goals.value = list;
    goal.value = detail;
  } catch (e) {
    if (alive && mine === ticket && !error.value) error.value = text(e);
  } finally {
    if (alive && mine === ticket) loading.value = false;
  }
}
function reset() {
  ticket++;
  goals.value = [];
  goal.value = null;
  Object.assign(draft, { title: "", criterion: "", kind: "human_check", step: "", depends: [], blockStep: "", blockText: "", edit: "" });
  error.value = notice.value = busy.value = editing.value = "";
  confirmAbandon.value = starting.value = expanded.value = false;
}
async function change(key: string, method: string, params: Record<string, unknown>, done: string) {
  const current = goal.value, chat = props.chatId;
  if (busy.value || !current || !live.value) return false;
  busy.value = key;
  error.value = notice.value = "";
  try {
    const result = await rpc<Goal>(method, { chat_id: chat, goal_id: current.id, revision: current.revision, ...params });
    if (!alive || chat !== props.chatId) return false;
    if (result && result.id === current.id && result.chat_id === chat) goal.value = result;
    notice.value = done;
    if (!CURRENT.includes(goal.value?.state || "")) await load();
    return true;
  } catch (e) {
    if (!alive || chat !== props.chatId) return false;
    error.value = text(e);
    await load(); // A refusal may be revision drift: show the host's current record.
    return false;
  } finally {
    if (alive && busy.value === key) busy.value = "";
  }
}
async function create() {
  const title = draft.title.trim(), chat = props.chatId;
  if (!title || busy.value) return;
  busy.value = "create";
  error.value = notice.value = "";
  try {
    await rpc<Goal>("goal_create", { chat_id: chat, title });
    if (!alive || chat !== props.chatId) return;
    draft.title = "";
    starting.value = false;
    expanded.value = true;
    notice.value = "Goal started. Add completion criteria below.";
    await load();
  } catch (e) {
    if (alive && chat === props.chatId) error.value = text(e);
  } finally {
    if (alive && busy.value === "create") busy.value = "";
  }
}
const setState = (state: string, done: string) => change("state:" + state, "goal_state", { state }, done);
async function abandon() {
  if (!confirmAbandon.value) {
    confirmAbandon.value = true;
    return;
  }
  confirmAbandon.value = false;
  await setState("abandoned", "Goal abandoned. Its record is kept and closed.");
}
function check(c: Criterion, met: boolean) {
  const done = !met ? `Criterion ${c.id} marked not met.`
    : c.kind === "evidence" ? `Criterion ${c.id} is met by current task evidence.` : `Criterion ${c.id} checked by you.`;
  return change("criterion:" + c.id, "goal_criterion_check", { criterion_id: c.id, met }, done);
}
const review = (c: Criterion, decision: "accept" | "reject") =>
  change("criterion:" + c.id, "goal_criterion_review", { criterion_id: c.id, decision }, `Proposed criterion ${c.id} ${decision}ed.`);
async function addCriterion() {
  const value = draft.criterion.trim();
  if (value && (await change("criterion:add", "goal_criterion_add", { text: value, kind: draft.kind }, "Criterion added.")))
    draft.criterion = "";
}
function startEdit(c: Criterion) {
  editing.value = c.id;
  draft.edit = c.text;
}
async function saveEdit(c: Criterion) {
  const value = draft.edit.trim();
  if (!value || value === c.text) editing.value = "";
  else if (await change("criterion:" + c.id, "goal_criterion_edit", { criterion_id: c.id, text: value }, `Criterion ${c.id} updated; its check was cleared.`))
    editing.value = "";
}
async function addStep() {
  const value = draft.step.trim();
  if (value && (await change("step:add", "goal_step_add", { title: value, depends_on: [...draft.depends] }, "Step added."))) {
    draft.step = "";
    draft.depends = [];
  }
}
async function block() {
  const step = draft.blockStep, reason = draft.blockText.trim();
  if (step && reason && (await change("step:" + step, "goal_step_block", { step_id: step, text: reason }, `Step ${step} blocked.`))) {
    draft.blockStep = "";
    draft.blockText = "";
  }
}
const unblock = (s: Step) => change("step:" + s.id, "goal_step_block", { step_id: s.id }, `Step ${s.id} unblocked.`);
async function continueGoal() {
  const current = goal.value, chat = props.chatId;
  if (busy.value || !current || props.running || !["active", "blocked"].includes(current.state)) return;
  busy.value = "continue";
  error.value = notice.value = "";
  try {
    await rpc("goal_continue", { chat_id: chat, goal_id: current.id });
    if (alive && chat === props.chatId) notice.value = "Started a new task toward this goal.";
  } catch (e) {
    if (alive && chat === props.chatId) error.value = text(e);
  } finally {
    if (alive && busy.value === "continue") busy.value = "";
  }
}
function blockerText(s: Step) {
  if (!s.blocker) return s.ready ? "ready" : "—";
  const base = word(blockerKinds, s.blocker.kind) + (s.blocker.active ? "" : " (resolved)");
  return s.blocker.kind === "text" && s.blocker.ref ? `${base}: ${s.blocker.ref}` : base;
}
function evidenceText(s: Step) {
  if (!s.evidence.length) return "—";
  const count = `${s.evidence.length} receipt${s.evidence.length === 1 ? "" : "s"}`;
  return s.evidence_current === false ? count + " · not current" : s.evidence_current ? count + " · current" : count;
}
watch(() => props.chatId, () => { reset(); load(); });
watch(() => props.refreshKey, () => load());
onMounted(load);
onUnmounted(() => (alive = false));
</script>

<template>
  <section class="goal-panel" aria-label="Goal">
    <div class="goal-head">
      <button v-if="goal" type="button" class="goal-toggle" :aria-expanded="expanded" aria-controls="goal-body" @click="expanded = !expanded">
        <strong>Goal · {{ goal.title }}</strong>
        <small>{{ word(goalStates, goal.state) }} · {{ summary }}</small>
      </button>
      <template v-else>
        <span class="goal-muted">{{ loading ? "Loading goals…" : "No current goal." }}</span>
        <button v-if="!starting" type="button" class="secondary-button" :disabled="loading" @click="starting = true">Set a goal</button>
      </template>
    </div>
    <form v-if="!goal && starting" class="goal-form" @submit.prevent="create">
      <label>Goal<input v-model="draft.title" maxlength="240" placeholder="What should be true when this is done?" /></label>
      <button type="submit" class="primary-button" :disabled="!!busy || !draft.title.trim()">{{ busy === "create" ? "Starting…" : "Start goal" }}</button>
      <button type="button" class="text-button" @click="starting = false">Cancel</button>
      <small>Goals are your records: they start no task and grant no permission.</small>
    </form>
    <p v-if="error" class="goal-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="goal-muted" role="status">{{ notice }}</p>

    <div v-if="goal && expanded" id="goal-body" class="goal-body">
      <div v-if="live" class="goal-actions">
        <button v-if="goal.state !== 'paused'" type="button" class="secondary-button" :disabled="!!busy || running" title="Start a new task toward this goal" @click="continueGoal">{{ busy === "continue" ? "Starting…" : "Continue goal" }}</button>
        <button v-if="goal.state !== 'paused'" type="button" class="secondary-button" :disabled="!!busy" @click="setState('paused', 'Goal paused.')">Pause</button>
        <button v-else type="button" class="secondary-button" :disabled="!!busy" @click="setState('active', 'Goal resumed.')">Resume</button>
        <button type="button" class="primary-button" :disabled="!!busy" @click="setState('done', 'Goal marked done.')">Mark done</button>
        <button type="button" class="text-button danger" :disabled="!!busy" @click="abandon">{{ confirmAbandon ? "Confirm: abandon goal" : "Abandon…" }}</button>
        <button v-if="confirmAbandon" type="button" class="text-button" @click="confirmAbandon = false">Keep goal</button>
      </div>

      <table class="goal-table">
        <caption>Completion criteria · {{ goal.progress.criteria_met }}/{{ goal.progress.criteria_total }} met</caption>
        <thead><tr><th scope="col">ID</th><th scope="col">Criterion</th><th scope="col">Kind</th><th scope="col">Status</th><th scope="col">Actions</th></tr></thead>
        <tbody>
          <tr v-for="(c, i) in accepted" :key="c.id" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
            <td>{{ c.id }}</td>
            <td>
              <input v-if="editing === c.id" v-model="draft.edit" maxlength="500" :aria-label="'Edit criterion ' + c.id" @keydown.enter.prevent="saveEdit(c)" />
              <template v-else>{{ c.text }}</template>
              <small v-if="c.origin === 'agent'">Proposed by Pebble, accepted by you</small>
              <small v-if="c.detail">{{ c.detail }}</small>
            </td>
            <td>{{ word(kinds, c.kind) }}</td>
            <td>{{ word(criterionStates, c.status) }}<small v-if="c.checked_by">{{ c.checked_by === "human" ? "by you" : "by task evidence" }}</small></td>
            <td class="goal-row-actions">
              <template v-if="live && editing === c.id">
                <button type="button" class="secondary-button" :disabled="!!busy" @click="saveEdit(c)">Save</button>
                <button type="button" class="text-button" @click="editing = ''">Cancel</button>
              </template>
              <template v-else-if="live">
                <button v-if="c.status !== 'met'" type="button" class="secondary-button" :disabled="!!busy" :aria-label="(c.kind === 'evidence' ? 'Verify receipts for criterion ' : 'Mark met: criterion ') + c.id" @click="check(c, true)">{{ c.kind === "evidence" ? "Verify receipts" : "Mark met" }}</button>
                <button v-else type="button" class="secondary-button" :disabled="!!busy" :aria-label="'Mark not met: criterion ' + c.id" @click="check(c, false)">Mark not met</button>
                <button type="button" class="text-button" :disabled="!!busy" :aria-label="'Edit criterion ' + c.id" @click="startEdit(c)">Edit</button>
              </template>
            </td>
          </tr>
          <tr v-if="!accepted.length"><td colspan="5">No accepted criteria yet. A goal is done only when every criterion is met.</td></tr>
        </tbody>
      </table>
      <p v-if="rejected" class="goal-muted">{{ rejected }} rejected {{ rejected === 1 ? "proposal is" : "proposals are" }} kept in the goal history and not listed.</p>

      <table v-if="proposals.length" class="goal-table">
        <caption>Proposed by Pebble · accept or reject</caption>
        <thead><tr><th scope="col">ID</th><th scope="col">Proposed criterion (agent-authored)</th><th scope="col">Kind</th><th scope="col">Actions</th></tr></thead>
        <tbody>
          <tr v-for="(c, i) in proposals" :key="c.id" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
            <td>{{ c.id }}</td><td>{{ c.text }}</td><td>{{ word(kinds, c.kind) }}</td>
            <td class="goal-row-actions">
              <button type="button" class="secondary-button" :disabled="!!busy || !live" :aria-label="'Accept proposed criterion ' + c.id" @click="review(c, 'accept')">Accept</button>
              <button type="button" class="text-button" :disabled="!!busy || !live" :aria-label="'Reject proposed criterion ' + c.id" @click="review(c, 'reject')">Reject</button>
            </td>
          </tr>
        </tbody>
      </table>

      <form v-if="live" class="goal-form" @submit.prevent="addCriterion">
        <label>New criterion<input v-model="draft.criterion" maxlength="500" placeholder="e.g. Tests pass on main" /></label>
        <label>Checked by<select v-model="draft.kind"><option value="human_check">Your check</option><option value="evidence">Task evidence</option></select></label>
        <button type="submit" class="secondary-button" :disabled="!!busy || !draft.criterion.trim()">Add criterion</button>
      </form>

      <table class="goal-table">
        <caption>Steps · {{ goal.progress.steps_completed }}/{{ goal.progress.steps_total }} completed</caption>
        <thead><tr><th scope="col">ID</th><th scope="col">Step</th><th scope="col">Status</th><th scope="col">After</th><th scope="col">Evidence</th><th scope="col">Blocker</th><th scope="col">Actions</th></tr></thead>
        <tbody>
          <tr v-for="(s, i) in goal.steps" :key="s.id" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
            <td>{{ s.id }}</td><td>{{ s.title }}</td><td>{{ word(stepStates, s.status) }}</td>
            <td>{{ s.depends_on.join(", ") || "—" }}</td><td>{{ evidenceText(s) }}</td><td>{{ blockerText(s) }}</td>
            <td class="goal-row-actions">
              <button v-if="live && s.status === 'blocked'" type="button" class="text-button" :disabled="!!busy" :aria-label="'Unblock step ' + s.id" @click="unblock(s)">Unblock</button>
            </td>
          </tr>
          <tr v-if="!goal.steps.length"><td colspan="7">No steps yet. Pebble reports progress only on steps you add.</td></tr>
        </tbody>
      </table>
      <form v-if="live" class="goal-form" @submit.prevent="addStep">
        <label>New step<input v-model="draft.step" maxlength="240" placeholder="Next piece of work" /></label>
        <fieldset v-if="goal.steps.length">
          <legend>After</legend>
          <label v-for="s in goal.steps" :key="s.id" class="goal-check"><input v-model="draft.depends" type="checkbox" :value="s.id" />{{ s.id }}</label>
        </fieldset>
        <button type="submit" class="secondary-button" :disabled="!!busy || !draft.step.trim()">Add step</button>
      </form>
      <form v-if="live && blockable.length" class="goal-form" @submit.prevent="block">
        <label>Block step<select v-model="draft.blockStep"><option value="" disabled>Choose a step</option><option v-for="s in blockable" :key="s.id" :value="s.id">{{ s.id }} · {{ s.title }}</option></select></label>
        <label>Reason<input v-model="draft.blockText" maxlength="300" placeholder="What is in the way?" /></label>
        <button type="submit" class="secondary-button" :disabled="!!busy || !draft.blockStep || !draft.blockText.trim()">Block step</button>
      </form>

      <table v-if="goal.blockers.length" class="goal-table">
        <caption>Blockers (host records)</caption>
        <thead><tr><th scope="col">Blocker</th><th scope="col">Kind</th><th scope="col">Reference</th><th scope="col">Detail</th><th scope="col">Next</th></tr></thead>
        <tbody>
          <tr v-for="(b, i) in goal.blockers" :key="b.kind + ':' + b.ref + ':' + i" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
            <td>{{ b.label }}</td><td>{{ word(blockerKinds, b.kind) }}</td>
            <td>{{ b.ref }}<small v-if="b.step_id">step {{ b.step_id }}</small></td>
            <td>{{ b.detail || "—" }}<small v-if="b.detail && b.detail_origin === 'agent'">agent-authored</small></td>
            <td>
              <button v-if="b.kind === 'approval'" type="button" class="secondary-button" @click="emit('reviews')">Review</button>
              <span v-else-if="b.kind === 'question'">Answer it in the task panel</span>
              <span v-else>Inspect it in the task panel</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="goal-muted">{{ goal.note || NOTE }}</p>
    </div>

    <table v-if="history.length && (expanded || !goal)" class="goal-table">
      <caption>Earlier goals</caption>
      <thead><tr><th scope="col">Goal</th><th scope="col">State</th><th scope="col">Criteria met</th><th scope="col">Updated</th></tr></thead>
      <tbody>
        <tr v-for="(g, i) in history" :key="g.id" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
          <td>{{ g.title }}</td><td>{{ word(goalStates, g.state) }}</td>
          <td>{{ g.progress.criteria_met }}/{{ g.progress.criteria_total }}</td><td>{{ when(g.updated) }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="goals.length >= LIST_LIMIT" class="goal-muted">Showing the {{ LIST_LIMIT }} most recent goals; older goals are kept but not listed.</p>
  </section>
</template>

<style scoped>
.goal-panel { margin: 12px 0; font-size: 13px; min-width: 0; }
.goal-head, .goal-actions, .goal-form, .goal-row-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.goal-toggle { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; width: 100%; padding: 10px 12px; text-align: left; border: 1px solid var(--border, #b9b9b9); border-radius: 12px; color: inherit; }
.goal-toggle strong { overflow-wrap: anywhere; }
.goal-toggle small, .goal-muted, .goal-table small { color: var(--muted); font-size: 11px; }
.goal-table small { display: block; }
.goal-muted { margin: 6px 0; }
.goal-error { color: var(--danger); font-size: 12px; margin: 6px 0; }
.goal-body { padding: 4px 2px; }
.goal-form { margin: 10px 0; }
.goal-form label { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.goal-form input, .goal-form select, .goal-table input { font: inherit; color: inherit; background: transparent; border: 1px solid var(--border, #b9b9b9); border-radius: 6px; padding: 5px 7px; min-width: 0; }
.goal-form fieldset { display: flex; gap: 6px; border: 0; padding: 0; margin: 0; flex-wrap: wrap; }
.goal-form legend { float: left; font-size: 12px; margin-right: 6px; }
.goal-check { gap: 3px !important; }
.goal-table { width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 12px; }
.goal-table caption { text-align: left; font-weight: 600; padding-bottom: 6px; }
.goal-table th, .goal-table td { text-align: left; vertical-align: top; padding: 5px 6px; border-bottom: 1px solid var(--border, #b9b9b9); overflow-wrap: anywhere; }
.goal-table th { font-weight: 500; color: var(--muted); }
.goal-table tr:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
