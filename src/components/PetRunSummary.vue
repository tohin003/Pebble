<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { rpc } from "../bridge";
import type { Chat } from "../types";

// Read-only task summary for the pet window (design study §2.4, F6). It renders
// no output, worker, stream or reconciliation panel and makes no change: answers,
// reviews, reconciliation and budget changes open the full workspace. Its only
// request is goal_list (pet allowlist, §8 patch 1); if that is refused the goal
// chip is simply not shown. It is not a live region: the pet's one status region
// is StatusLine in its header (Phase 19 §2.6), so a run change is announced once.
interface GoalChip {
  id: string;
  state: string;
  progress: { criteria_met: number; criteria_total: number };
  blocker: { kind: string; label: string } | null;
}
const props = defineProps<{ chat: Chat; refreshKey?: unknown }>();
const emit = defineEmits<{ workspace: [] }>();
const CURRENT = ["active", "paused", "blocked"];
const states: Record<string, string> = {
  queued: "queued", running: "running", retry_wait: "reconnecting", paused: "paused", stopped: "stopped",
  interrupted: "interrupted", failed: "failed", completed: "completed", waiting_input: "waiting for you",
  waiting_approval: "waiting for your review", budget_exhausted: "budget reached",
  verification_required: "needs verification", needs_reconciliation: "needs a check",
};
// States whose next step lives in the workspace (resume limits, readback, review).
const workspaceStates: Record<string, string> = {
  needs_reconciliation: "An outcome is uncertain. Inspect it in the workspace before continuing.",
  budget_exhausted: "The task reached its budget. Resume it with new limits in the workspace.",
  verification_required: "The task needs verification in the workspace.",
  waiting_approval: "An action needs your review in the workspace.",
};
const pick = (table: Record<string, string>, key: unknown) =>
  typeof key === "string" && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
const goal = ref<GoalChip | null>(null);
let ticket = 0;
let alive = true;

const pending = computed(() => props.chat.questions?.filter((q) => q.status === "pending") || []);
const current = computed(() => {
  const runs = props.chat.runs || [];
  return runs.find((r) => ["queued", "running", "retry_wait"].includes(r.state))
    || runs.find((r) => pending.value.some((q) => q.run_id === r.id))
    || runs.find((r) => r.state === "waiting_approval") || runs[0];
});
const plan = computed(() => {
  const value = props.chat.plan;
  if (!value || value.run_id !== current.value?.id || !value.steps.length) return null;
  return {
    done: value.steps.filter((s) => s.status === "completed").length,
    total: value.steps.length,
    now: value.steps.find((s) => s.status === "in_progress")?.title || "",
  };
});
// Phase 45 (F185, audit round R6): a card the companion answers itself (D133: a site, a window, bringing a window
// forward) is not "in the workspace"
const SIMPLE_CARDS = ["browser_grant", "screen_grant", "screen_raise"];
const simpleOnly = computed(() => {
  const waiting = (props.chat.actions || []).filter((a) => a.status === "pending");
  return waiting.length > 0 && waiting.every((a) => SIMPLE_CARDS.includes(a.kind));
});
const next = computed(() => current.value?.state === "waiting_approval" && simpleOnly.value
  ? undefined : pick(workspaceStates, current.value?.state));
// Phase 45 (F070, F087; audit rounds R2 and R3): a run's note is written for the model ("Do not claim ...", "Never say
// it plays", "ScreenAct focus on its Address row"); the person sees only the sentences meant for them. The same in
// PetRunSummary.vue and RunPanel.vue (personNote.test.mjs holds them equal).
const FOR_THE_MODEL = /^(Do not|Don't|Never|Say |Tell the person|Check the|Otherwise)/;
const TOOL_NAME = /\b(ScreenAct|ScreenObserve|ScreenRequest|BrowserRequest|OpenURL|OpenApp|AppAction|RunTests|RunCode|ReadFile|WriteFile|EditFile|Shell)\b/;
function personNote(text: unknown) {
  return String(text || "").split(/(?<=\.)\s+/).filter((s) => !FOR_THE_MODEL.test(s) && !TOOL_NAME.test(s)).join(" ");
}
const errorText = computed(() => personNote(current.value?.error));
// Phase 45 (F053): a plain finished reply needs no task summary (its note read as if the real result were elsewhere).
const worth = computed(() => !!(goal.value || pending.value.length || plan.value || next.value || current.value?.error
  || (current.value && current.value.state !== "completed")));
const chip = computed(() => {
  if (!goal.value) return "";
  const p = goal.value.progress;
  return `Goal · ${p.criteria_met}/${p.criteria_total} criteria`
    + (goal.value.blocker ? ` · blocked: ${String(goal.value.blocker.label).toLowerCase()}` : goal.value.state === "paused" ? " · paused" : "");
});

async function load() {
  const mine = ++ticket, chat = props.chat.id;
  try {
    const list = await rpc<GoalChip[]>("goal_list", { chat_id: chat, limit: 5 });
    if (!alive || mine !== ticket || chat !== props.chat.id) return;
    goal.value = (Array.isArray(list) && list.find((g) => g && CURRENT.includes(g.state) && g.progress)) || null;
  } catch {
    if (alive && mine === ticket) goal.value = null;
  }
}
watch(() => [props.chat.id, props.refreshKey], () => {
  goal.value = null;
  load();
});
onMounted(load);
onUnmounted(() => (alive = false));
</script>

<template>
  <section v-if="worth" class="pet-run-summary" aria-label="Task summary">
    <button v-if="goal" type="button" class="pet-goal-chip" @click="emit('workspace')">
      {{ chip }}<span aria-hidden="true"> → </span>Open
    </button>
    <p v-if="current" class="pet-summary-task">Task: {{ pick(states, current.state) || "unknown" }}</p>
    <p v-if="plan">
      Plan · {{ plan.done }}/{{ plan.total }} steps<template v-if="plan.now"> · now: {{ plan.now }}</template>
    </p>
    <p v-if="pending.length">
      Pebble needs your answer{{ pending.length > 1 ? ` (${pending.length} questions)` : "" }}.
      <button type="button" class="pet-summary-link" @click="emit('workspace')">Answer in the workspace</button>
    </p>
    <p v-else-if="next">
      {{ next }} <button type="button" class="pet-summary-link" @click="emit('workspace')">Open</button>
    </p>
    <p v-if="errorText" class="pet-summary-note">{{ errorText }}</p>
    <small class="pet-summary-note">{{ simpleOnly ? "Output and agents are in the workspace." : "Output, agents and reviews are in the workspace." }}</small>
  </section>
</template>

<style scoped>
.pet-run-summary { margin: 10px 0; font-size: 12px; min-width: 0; }
.pet-run-summary p { margin: 6px 0; overflow-wrap: anywhere; }
.pet-goal-chip { display: inline-block; max-width: 100%; padding: 5px 9px; border-radius: 8px; background: var(--hover); color: var(--accent); font-size: 11px; text-align: left; overflow-wrap: anywhere; }
.pet-summary-link { color: var(--accent); font-size: 11px; padding: 0 2px; text-decoration: underline; }
.pet-summary-note { display: block; color: var(--muted); font-size: 10px; }
</style>
