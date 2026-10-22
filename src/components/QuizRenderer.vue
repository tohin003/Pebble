<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import {
  PhArrowLeft,
  PhArrowRight,
  PhArrowUpRight,
  PhCheckCircle,
  PhPlay,
  PhShareNetwork,
} from "@phosphor-icons/vue";
import { rpc, native } from "../bridge";
import type {
  QuizAnswers,
  QuizAttempt,
  QuizQuestionResult,
  QuizResult,
  QuizV2Artifact,
} from "../types";

const props = defineProps<{
  artifact: QuizV2Artifact;
  historical?: boolean;
  surface?: "main" | "pet";
}>();
// Sharing is main-window chrome; the pet only points to the workspace.
const emit = defineEmits<{ workspace: [] }>();

const reserved = new Set(["__proto__", "prototype", "constructor", "__text"]);
const STATUSES = new Set(["open", "submitted", "expired", "superseded"]);
function require(condition: unknown, message = "This quiz contains invalid display data."): asserts condition {
  if (!condition) throw new Error(message);
}
function object(value: unknown, message?: string): Record<string, unknown> {
  require(value !== null && typeof value === "object" && !Array.isArray(value), message);
  return value as Record<string, unknown>;
}
function shape(value: unknown, keys: string[]) {
  const row = object(value);
  require(keys.every((key) => Object.hasOwn(row, key)) && Object.keys(row).every((key) => keys.includes(key)));
  return row;
}
function text(value: unknown, maximum: number, message?: string) {
  require(typeof value === "string" && !!value.trim() && value.length <= maximum, message);
}

// Public v2 questions only: the answer key and explanations stay with the host.
function validate(data: unknown) {
  require(new TextEncoder().encode(JSON.stringify(data)).length <= 96000);
  const row = shape(data, ["duration_minutes", "questions"]);
  const minutes = row.duration_minutes, questions = row.questions;
  require(typeof minutes === "number" && Number.isInteger(minutes) && minutes >= 1 && minutes <= 180);
  require(Array.isArray(questions) && questions.length >= 1 && questions.length <= 50);
  const ids = questions.map((question) => shape(question, ["id", "question", "choices"]).id);
  require(
    ids.every((id) => typeof id === "string" && /^[A-Za-z0-9_-]{1,40}$/.test(id) && !reserved.has(id)) &&
      new Set(ids).size === ids.length,
  );
  questions.forEach((question) => {
    text(question.question, 1500);
    const choices = question.choices;
    require(Array.isArray(choices) && choices.length >= 2 && choices.length <= 6);
    choices.forEach((choice) => text(choice, 700));
  });
}
const validation = computed(() => {
  try {
    validate(props.artifact.data);
    return "";
  } catch (error) {
    return error instanceof Error ? error.message : "This quiz cannot be displayed.";
  }
});
const questions = computed(() => (validation.value ? [] : props.artifact.data.questions));
const revision = computed(() => props.artifact.revision || 1);
const inactive = computed(() => ["stopped", "expired"].includes(props.artifact.status || ""));
const invalidReply = "The app returned an invalid quiz attempt.";

function choiceIndex(value: unknown, id: string, message = invalidReply) {
  const question = questions.value.find((item) => item.id === id);
  require(
    !!question && typeof value === "number" && Number.isInteger(value) && value >= 0 && value < question.choices.length,
    message,
  );
  return value as number;
}
function parseResult(value: unknown): QuizResult {
  const row = object(value, invalidReply);
  const score = row.score, total = row.total, rows = row.per_question;
  require(
    typeof total === "number" && total === questions.value.length && typeof score === "number" &&
      Number.isInteger(score) && score >= 0 && score <= total && Array.isArray(rows) && rows.length <= total,
    invalidReply,
  );
  const seen = new Set<string>();
  const per_question = rows.map((entry): QuizQuestionResult => {
    const item = object(entry, invalidReply);
    const id = item.id, correct = item.correct, explanation = item.explanation;
    require(typeof id === "string" && !seen.has(id) && questions.value.some((q) => q.id === id), invalidReply);
    seen.add(id);
    require(typeof correct === "boolean", invalidReply);
    const result: QuizQuestionResult = { id, correct };
    if (item.chosen !== undefined) result.chosen = item.chosen === null ? null : choiceIndex(item.chosen, id);
    if (item.answer !== undefined) result.answer = choiceIndex(item.answer, id);
    if (explanation !== undefined) {
      text(explanation, 1500, invalidReply);
      result.explanation = explanation as string;
    }
    return result;
  });
  return { score, total, per_question };
}
// Host replies are checked too: an attempt for another view or revision is never adopted as open.
function parseAttempt(value: unknown): QuizAttempt {
  const row = object(value, invalidReply);
  const id = row.attempt_id, deadline = row.deadline, status = row.status ?? "open";
  require(typeof id === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(id), invalidReply);
  require(typeof deadline === "number" && Number.isFinite(deadline) && deadline > 0, invalidReply);
  require(typeof status === "string" && STATUSES.has(status), invalidReply);
  require(row.artifact_id === undefined || row.artifact_id === props.artifact.id, "The app returned an attempt for another view.");
  require(row.chat_id === undefined || row.chat_id === props.artifact.chat_id, "The app returned an attempt for another conversation.");
  const answers: QuizAnswers = {};
  const saved = row.answers === undefined ? {} : object(row.answers, invalidReply);
  for (const question of questions.value)
    answers[question.id] =
      saved[question.id] === undefined || saved[question.id] === null ? null : choiceIndex(saved[question.id], question.id);
  const attempt: QuizAttempt = {
    attempt_id: id,
    deadline,
    status: status as QuizAttempt["status"],
    answers,
    result: row.result === undefined || row.result === null ? null : parseResult(row.result),
    shared: row.shared === true,
  };
  const bound = row.revision;
  if (bound !== undefined) {
    require(typeof bound === "number" && Number.isInteger(bound), invalidReply);
    attempt.revision = bound;
    // An attempt bound to another revision is closed for this view, never resumed.
    if (bound !== revision.value && attempt.status === "open") attempt.status = "superseded";
  }
  return attempt;
}

const attempt = ref<QuizAttempt | null>(null);
const answers = reactive<Record<string, number | null>>({});
const index = ref(0);
const loading = ref(false), busy = ref(false), sharing = ref(false);
const actionError = ref(""), saveError = ref("");
const saveState = ref<"idle" | "saving" | "saved" | "error">("idle");
const replaced = ref<number | null>(null);
const now = ref(Date.now());
let generation = 0, drafts = 0, saving = false, dirty = false;
let owner = "", ownerRevision = 0;

const message = (error: unknown) => (error instanceof Error ? error.message : String(error));
function adopt(next: QuizAttempt | null) {
  now.value = Date.now();
  attempt.value = next;
  for (const question of questions.value) answers[question.id] = next?.answers[question.id] ?? null;
  if (next?.status === "open") {
    owner = `${props.artifact.chat_id}:${props.artifact.id}`;
    ownerRevision = revision.value;
  }
}
function snapshot(): QuizAnswers {
  return Object.fromEntries(questions.value.map((question) => [question.id, answers[question.id] ?? null]));
}
const identity = () => ({ chat_id: props.artifact.chat_id, id: props.artifact.id, revision: revision.value });

async function load() {
  const current = ++generation;
  drafts++;
  const view = `${props.artifact.chat_id}:${props.artifact.id}`;
  // An open attempt on an earlier revision was closed by the update; say so once.
  replaced.value =
    attempt.value?.status === "open" && owner === view && ownerRevision < revision.value
      ? ownerRevision
      : owner === view
        ? replaced.value
        : null;
  if (owner !== view) owner = "";
  Object.keys(answers).forEach((key) => delete answers[key]);
  attempt.value = null;
  index.value = 0;
  actionError.value = saveError.value = "";
  saveState.value = "idle";
  busy.value = sharing.value = false;
  if (validation.value || props.historical || !native) return;
  loading.value = true;
  try {
    const reply = await rpc<unknown>("quiz_attempt", identity());
    if (current !== generation) return;
    adopt(reply === null ? null : parseAttempt(reply));
  } catch (error) {
    if (current === generation) actionError.value = message(error);
  } finally {
    if (current === generation) loading.value = false;
  }
}
watch(
  () => JSON.stringify([props.artifact.chat_id, props.artifact.id, revision.value, !!props.historical, validation.value]),
  load,
  { immediate: true },
);

const remaining = computed(() =>
  attempt.value ? Math.max(0, Math.ceil(attempt.value.deadline - now.value / 1000)) : 0,
);
const clock = computed(
  () => `${String(Math.floor(remaining.value / 60)).padStart(2, "0")}:${String(remaining.value % 60).padStart(2, "0")}`,
);
const open = computed(() => attempt.value?.status === "open");
const timeUp = computed(() => open.value && remaining.value === 0);
const writable = computed(() => native && !props.historical && !validation.value && !inactive.value);
const answering = computed(() => writable.value && open.value && !timeUp.value && !busy.value);
const answered = computed(() => questions.value.filter((question) => answers[question.id] !== null && answers[question.id] !== undefined).length);
const canStart = computed(
  () => writable.value && !loading.value && !busy.value && (!attempt.value || attempt.value.status !== "open"),
);
const result = computed(() => (attempt.value?.status !== "superseded" ? attempt.value?.result || null : null));
const review = computed(() =>
  result.value
    ? questions.value.map((question, number) => {
        const row = result.value?.per_question.find((item) => item.id === question.id);
        const chosen = row?.chosen !== undefined ? row.chosen : (answers[question.id] ?? null);
        return {
          number: number + 1,
          question,
          state: !row ? "Not graded" : chosen === null ? "Unanswered" : row.correct ? "Correct" : "Incorrect",
          correct: !!row?.correct,
          chosen: chosen === null ? "" : question.choices[chosen],
          answer: row?.answer === undefined ? "" : question.choices[row.answer],
          explanation: row?.explanation || "",
        };
      })
    : [],
);

async function start() {
  if (!canStart.value) return;
  const current = generation;
  busy.value = true;
  actionError.value = "";
  try {
    const reply = parseAttempt(await rpc<unknown>("quiz_start", identity()));
    if (current !== generation) return;
    require(reply.status === "open" || !!reply.result, invalidReply);
    drafts++;
    adopt(reply);
    index.value = 0;
    replaced.value = null;
    saveState.value = "idle";
  } catch (error) {
    if (current === generation) actionError.value = message(error);
  } finally {
    if (current === generation) busy.value = false;
  }
}
function choose(id: string, choice: number) {
  if (!answering.value) return;
  try {
    answers[id] = choiceIndex(choice, id);
  } catch {
    return;
  }
  save();
}
// Phase 24 (V1-162): the choices are one radio group (one Tab stop, arrow keys move and choose, as native radios do);
// Back, Next and the question buttons move focus to the new question and say which one it is.
const heading = ref<HTMLElement | null>(null);
const choiceButtons: (HTMLElement | null)[] = [];
const keepChoice = (i: number) => (el: unknown) => {
  choiceButtons[i] = (el as HTMLElement | null) || null;
};
const where = ref("");
function tabStop(i: number) {
  const question = questions.value[index.value];
  const chosen = question ? answers[question.id] : null;
  return (typeof chosen === "number" ? chosen : 0) === i ? 0 : -1;
}
function choiceKey(event: KeyboardEvent, i: number) {
  const question = questions.value[index.value];
  if (!question) return;
  const count = question.choices.length;
  const next = ({ ArrowDown: i + 1, ArrowRight: i + 1, ArrowUp: i - 1, ArrowLeft: i - 1, Home: 0, End: count - 1 } as Record<string, number>)[event.key];
  if (next === undefined) return;
  event.preventDefault();
  const target = (next + count) % count;
  choose(question.id, target);
  nextTick(() => choiceButtons[target]?.focus?.());
}
function go(to: number) {
  if (to < 0 || to >= questions.value.length) return;
  index.value = to;
  where.value = `Question ${to + 1} of ${questions.value.length}`;
  nextTick(() => heading.value?.focus?.());
}
const answeredLabel = (question: { id: string }, i: number) =>
  "Question " + (i + 1) + (answers[question.id] !== null && answers[question.id] !== undefined ? ", answered" : "");
// Drafts go to the host, never to browser storage. One save runs at a time; the latest answers win.
async function save() {
  const current = attempt.value;
  if (!current || current.status !== "open") return;
  if (saving) {
    dirty = true;
    return;
  }
  saving = true;
  const draft = drafts;
  saveState.value = "saving";
  try {
    do {
      dirty = false;
      await rpc("quiz_save", { chat_id: props.artifact.chat_id, attempt_id: current.attempt_id, revision: revision.value, answers: snapshot() });
    } while (dirty && draft === drafts);
    if (draft === drafts) {
      saveState.value = "saved";
      saveError.value = "";
    }
  } catch (error) {
    if (draft === drafts) {
      saveState.value = "error";
      saveError.value = message(error);
    }
  } finally {
    saving = false;
  }
}
// A refused submission keeps the draft on screen and is never retried automatically.
async function submit() {
  const current = attempt.value;
  if (!current || current.status !== "open" || busy.value || !writable.value) return;
  const view = generation;
  busy.value = true;
  actionError.value = "";
  drafts++;
  try {
    const reply = parseAttempt(
      await rpc<unknown>("quiz_submit", { chat_id: props.artifact.chat_id, attempt_id: current.attempt_id, revision: revision.value, answers: snapshot() }),
    );
    if (view !== generation) return;
    require(reply.attempt_id === current.attempt_id, invalidReply);
    const local = snapshot();
    adopt(reply);
    if (reply.status !== "superseded")
      for (const question of questions.value) if (answers[question.id] === null) answers[question.id] = local[question.id];
    saveState.value = "idle";
  } catch (error) {
    if (view === generation) actionError.value = message(error);
  } finally {
    if (view === generation) busy.value = false;
  }
}
// Nothing reaches Pebble until the person presses Share on the main window.
async function share() {
  const current = attempt.value;
  if (props.surface === "pet" || !current || !result.value || current.shared || sharing.value || !writable.value) return;
  const view = generation;
  sharing.value = true;
  actionError.value = "";
  try {
    const reply = object(await rpc<unknown>("quiz_share", { chat_id: props.artifact.chat_id, attempt_id: current.attempt_id }), invalidReply);
    require(reply.shared === true, "The app did not confirm the share.");
    const latest = attempt.value;
    if (view === generation && latest && latest.attempt_id === current.attempt_id) attempt.value = { ...latest, shared: true };
  } catch (error) {
    if (view === generation) actionError.value = message(error);
  } finally {
    if (view === generation) sharing.value = false;
  }
}

let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  timer = setInterval(() => (now.value = Date.now()), 1000);
});
onUnmounted(() => {
  generation++;
  drafts++;
  if (timer) clearInterval(timer);
});
</script>

<template>
  <section class="quiz" :aria-label="artifact.title">
    <header class="quiz-head">
      <span class="quiz-eyebrow">Quiz · checked by Pebble</span>
      <h2>{{ artifact.title }}</h2>
      <p v-if="!validation" class="quiz-facts">
        <span>{{ questions.length }} {{ questions.length === 1 ? "question" : "questions" }}</span>
        <span>{{ artifact.data.duration_minutes }} min</span>
        <span v-if="open" class="quiz-clock" :class="{ urgent: remaining < 120 }">{{ clock }} left</span>
      </p>
    </header>
    <p v-if="validation" class="quiz-error" role="alert">{{ validation }}</p>
    <template v-else>
      <p v-if="historical" class="quiz-note">Historical revision. Return to the current revision to take this quiz.</p>
      <p v-else-if="loading" class="quiz-note" role="status">Loading your attempt…</p>
      <template v-else-if="open && attempt">
        <progress class="quiz-progress" :value="answered" :max="questions.length" aria-label="Questions answered" />
        <div class="quiz-count">
          <span>Question {{ index + 1 }} of {{ questions.length }}</span>
          <span class="quiz-saved" :class="saveState">{{
            saveState === "saving" ? "Saving…" : saveState === "saved" ? "Draft saved" : saveState === "error" ? "Draft not saved" : ""
          }}</span>
        </div>
        <h3 :id="'quiz-q-' + questions[index].id" ref="heading" class="quiz-question" tabindex="-1">{{ questions[index].question }}</h3>
        <p class="quiz-sr" role="status" aria-live="polite">{{ where }}</p>
        <div class="quiz-choices" role="radiogroup" :aria-labelledby="'quiz-q-' + questions[index].id">
          <button
            v-for="(choice, i) in questions[index].choices"
            :key="i"
            :ref="keepChoice(Number(i))"
            type="button"
            role="radio"
            class="quiz-choice"
            :class="{ chosen: answers[questions[index].id] === i }"
            :aria-checked="answers[questions[index].id] === i ? 'true' : 'false'"
            :tabindex="tabStop(Number(i))"
            :disabled="!answering"
            @click="choose(questions[index].id, Number(i))"
            @keydown="choiceKey($event, Number(i))"
          >
            <span class="quiz-letter">{{ String.fromCharCode(65 + Number(i)) }}</span><span>{{ choice }}</span>
          </button>
        </div>
        <p v-if="timeUp" class="quiz-note" role="status">Time is up. Submit to see how you did.</p>
        <p v-if="saveError" class="quiz-error" role="alert">Draft not saved: {{ saveError }}</p>
        <div class="quiz-nav">
          <button type="button" class="secondary-button" :disabled="index === 0" @click="go(index - 1)">
            <PhArrowLeft :size="13" />Back
          </button>
          <span>{{ answered }} of {{ questions.length }} answered</span>
          <button v-if="index < questions.length - 1" type="button" class="secondary-button" @click="go(index + 1)">
            Next<PhArrowRight :size="13" />
          </button>
          <button v-else type="button" class="primary-button" :disabled="busy || !writable" @click="submit">
            {{ busy ? "Checking…" : "Submit answers" }}
          </button>
        </div>
        <div class="quiz-jumps" aria-label="Jump to question">
          <button
            v-for="(question, i) in questions"
            :key="question.id"
            type="button"
            :class="{ done: answers[question.id] !== null && answers[question.id] !== undefined, current: index === i }"
            :aria-label="answeredLabel(question, Number(i))"
            :aria-current="index === i ? 'step' : undefined"
            @click="go(Number(i))"
          >
            {{ Number(i) + 1 }}
          </button>
        </div>
      </template>
      <template v-else-if="result && attempt">
        <div class="quiz-score">
          <PhCheckCircle :size="30" weight="duotone" />
          <div>
            <strong>{{ result.score }} / {{ result.total }}</strong>
            <span>{{ attempt.status === "expired" ? "Graded when time ran out." : "Graded by Pebble." }}</span>
          </div>
        </div>
        <ol class="quiz-review">
          <li v-for="row in review" :key="row.question.id" :class="{ correct: row.correct, incorrect: row.state === 'Incorrect' }">
            <small>Question {{ row.number }} · {{ row.state }}</small>
            <strong>{{ row.question.question }}</strong>
            <p v-if="row.chosen">Your answer: {{ row.chosen }}</p>
            <p v-if="row.answer && !row.correct">Correct answer: {{ row.answer }}</p>
            <p v-if="row.explanation" class="quiz-explanation">{{ row.explanation }}</p>
          </li>
        </ol>
        <div class="quiz-share">
          <template v-if="surface === 'pet'">
            <button type="button" class="secondary-button" @click="emit('workspace')">
              Share in the workspace<PhArrowUpRight :size="13" />
            </button>
          </template>
          <p v-else-if="attempt.shared" class="quiz-shared" role="status">
            <PhShareNetwork :size="14" />Shared with Pebble
          </p>
          <template v-else>
            <button type="button" class="primary-button" :disabled="sharing || !writable" @click="share">
              <PhShareNetwork :size="14" />{{ sharing ? "Sharing…" : "Share result with Pebble" }}
            </button>
          </template>
          <small>Pebble sees your score and each question's result only after you share them.</small>
        </div>
        <button v-if="canStart" type="button" class="secondary-button quiz-retake" @click="start">Retake quiz</button>
      </template>
      <div v-else class="quiz-intro">
        <p v-if="replaced" class="quiz-note" role="status">
          This quiz changed after revision {{ replaced }}, so your earlier attempt was closed without a grade.
        </p>
        <p v-else-if="attempt?.status === 'superseded'" class="quiz-note" role="status">
          This quiz changed while you were answering, so that attempt was closed without a grade.
        </p>
        <p v-else-if="attempt?.status === 'expired'" class="quiz-note" role="status">
          Time ran out before that attempt was submitted.
        </p>
        <p>
          Answer at your own pace within the time limit. Pebble checks your answers; the answer key is not part of this view.
          Your result stays here unless you share it.
        </p>
        <p v-if="inactive" class="quiz-note">This quiz is {{ artifact.status }}.</p>
        <p v-else-if="!native" class="quiz-note">Open the desktop app to take this quiz.</p>
        <button type="button" class="primary-button" :disabled="!canStart" @click="start">
          <PhPlay :size="14" weight="fill" />{{ busy ? "Starting…" : attempt ? "Start again" : "Start quiz" }}
        </button>
        <small>Questions are generated by AI. Read the explanations after you submit.</small>
      </div>
      <p v-if="actionError" class="quiz-error" role="alert">{{ actionError }}</p>
    </template>
  </section>
</template>

<style scoped>
.quiz {
  min-width: 0;
  font-size: 13px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.quiz-eyebrow {
  font-size: 11px;
  letter-spacing: 1.6px;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
}
h2 {
  font-size: 23px;
  font-weight: 500;
  line-height: 1.3;
  letter-spacing: -0.5px;
  margin: 4px 0 6px;
}
.quiz-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin: 0 0 18px;
  color: var(--secondary);
  font-size: 12px;
}
.quiz-clock {
  margin-left: auto;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.quiz-clock.urgent {
  color: var(--danger);
}
.quiz-note {
  color: var(--muted);
  font-size: 12px;
}
.quiz-error {
  color: var(--danger, #b64747);
  white-space: pre-wrap;
}
.quiz-progress {
  appearance: none;
  display: block;
  width: 100%;
  height: 3px;
  margin: 4px 0 20px;
  border: 0;
  border-radius: 10px;
  background: var(--line);
  overflow: hidden;
}
.quiz-progress::-webkit-progress-bar {
  background: var(--line);
}
.quiz-progress::-webkit-progress-value {
  background: var(--accent);
  transition: width 0.2s;
}
.quiz-progress::-moz-progress-bar {
  background: var(--accent);
}
.quiz-count {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  letter-spacing: 1.2px;
  text-transform: uppercase;
  color: var(--muted);
}
.quiz-saved {
  letter-spacing: 0;
  text-transform: none;
}
.quiz-saved.error {
  color: var(--danger);
}
.quiz-sr {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
.quiz-question:focus {
  outline: none;
}
.quiz-question:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}
.quiz-question {
  font-size: 19px;
  font-weight: 500;
  line-height: 1.45;
  letter-spacing: -0.3px;
  margin: 12px 0 18px;
  white-space: pre-wrap;
}
.quiz-choices {
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.quiz-choice {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  width: 100%;
  padding: 13px 14px;
  text-align: left;
  line-height: 1.55;
  color: var(--text);
  background: var(--code);
  border: 1px solid var(--line);
  border-radius: 11px;
  transition: border-color 0.12s, background 0.12s;
}
.quiz-choice:not(:disabled):hover {
  border-color: var(--line-strong);
}
.quiz-choice.chosen {
  border-color: var(--accent);
  background: var(--soft);
}
.quiz-letter {
  flex-shrink: 0;
  min-width: 22px;
  text-align: center;
  padding: 1px 0;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  font-size: 11px;
  color: var(--secondary);
}
.quiz-choice.chosen .quiz-letter {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--on-accent);
}
.quiz-nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 22px;
}
.quiz-nav span {
  font-size: 11px;
  color: var(--muted);
}
.quiz-jumps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 22px;
}
.quiz-jumps button {
  width: 28px;
  height: 28px;
  border-radius: 7px;
  font-size: 11px;
  color: var(--muted);
  background: var(--code);
  border: 1px solid var(--line);
  font-variant-numeric: tabular-nums;
}
.quiz-jumps .done {
  background: var(--soft);
  color: var(--text);
}
.quiz-jumps .current {
  border-color: var(--accent);
}
.quiz-score {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 18px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--soft);
  color: var(--accent);
}
.quiz-score div {
  display: flex;
  flex-direction: column;
}
.quiz-score strong {
  font-size: 30px;
  font-weight: 500;
  letter-spacing: -0.6px;
  line-height: 1.2;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.quiz-score span {
  color: var(--secondary);
  font-size: 12px;
}
.quiz-review {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
}
.quiz-review li {
  padding: 18px 0;
  border-bottom: 1px solid var(--line);
}
.quiz-review small {
  font-size: 11px;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--muted);
}
.quiz-review li.correct small {
  color: var(--accent);
}
.quiz-review li.incorrect small {
  color: var(--danger);
}
.quiz-review strong {
  display: block;
  margin-top: 6px;
  font-weight: 500;
  white-space: pre-wrap;
}
.quiz-review p {
  margin: 8px 0 0;
}
.quiz-explanation {
  color: var(--secondary);
  white-space: pre-wrap;
}
.quiz-share {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  margin-top: 20px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
}
.quiz-share small,
.quiz-intro small {
  color: var(--muted);
  font-size: 11px;
}
.quiz-shared {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  color: var(--accent);
  font-weight: 550;
}
.quiz-retake {
  margin-top: 14px;
}
.quiz-intro {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 14px;
  padding: 8px 0;
}
.quiz-intro p {
  margin: 0;
  color: var(--secondary);
  line-height: 1.75;
}
.quiz-intro .quiz-note {
  color: var(--muted);
}
button:disabled {
  opacity: 0.55;
  cursor: default;
}
</style>
