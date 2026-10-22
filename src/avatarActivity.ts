// Vue glue for the companion reducer (companionState.ts). One instance per
// window: host bridge events and the window's own counts go in; the pose (for
// Avatar) and the status key (for StatusLine) come out. There is no periodic
// timer: a single timeout wakes the pose at its next deadline (a settle window
// ending, a token going quiet). The earlier time-derived moods ("impatient",
// "sleepy" during a run, a curious/thinking swap) are gone (F1), and streamed
// text is "writing", not "building" (F2).
import { computed, getCurrentScope, onScopeDispose, ref, shallowRef, watch, type Ref } from "vue";
import {
  fromEvent,
  initial,
  nextChange,
  pose as poseOf,
  reduce,
  seedSignals,
  type HostSignal,
} from "./companionState";

type Source<T> = Ref<T> | (() => T);
const read = <T>(source: Source<T> | undefined): T | undefined =>
  source === undefined ? undefined : typeof source === "function" ? (source as () => T)() : source.value;

export interface CompanionInputs {
  approvals?: Source<number>; // pending action reviews
  questions?: Source<number>; // pending native questions
  notices?: Source<number>; // unacknowledged job notifications
  model?: Source<string | undefined>; // state.model.state
  reactions?: Source<boolean | undefined>; // companion.reactions
  clock?: () => number;
}

export function useCompanion(inputs: CompanionInputs = {}) {
  const clock = inputs.clock || (() => Date.now());
  const state = shallowRef(initial(clock()));
  const now = ref(clock());
  let timer: ReturnType<typeof setTimeout> | null = null;
  const pose = computed(() => poseOf(state.value, now.value, { reactions: read(inputs.reactions) !== false }));
  function arm() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    const at = nextChange(state.value, now.value);
    if (at !== null)
      timer = setTimeout(() => {
        timer = null;
        now.value = Math.max(clock(), at);
        arm();
      }, Math.max(0, at - now.value));
  }
  function signal(sig: HostSignal) {
    const t = clock();
    const next = reduce(state.value, sig, t);
    now.value = t;
    if (next !== state.value) state.value = next;
    arm();
  }
  const accept = (event: { event: string; data: any }) => fromEvent(event).forEach(signal);
  const sending = (on: boolean) => signal({ k: "sending", on });
  const interact = (what: "hover" | "leave" | "tap" | "drag") => signal({ k: "interaction", what });
  const seed = (runs: readonly { id?: unknown; state?: unknown }[] | undefined, activeRun?: unknown) =>
    seedSignals(runs, activeRun).forEach(signal);
  const halts = [
    watch(() => read(inputs.approvals), (n) => n !== undefined && signal({ k: "approvals", pending: n }), { immediate: true }),
    watch(() => read(inputs.questions), (n) => n !== undefined && signal({ k: "question", pending: n }), { immediate: true }),
    watch(() => read(inputs.notices), (n) => n !== undefined && signal({ k: "notices", pending: n }), { immediate: true }),
    watch(() => read(inputs.model), (m) => typeof m === "string" && signal({ k: "model", state: m }), { immediate: true }),
  ];
  function stop() {
    halts.forEach((h) => h());
    if (timer !== null) clearTimeout(timer);
    timer = null;
  }
  if (getCurrentScope()) onScopeDispose(stop);
  return {
    state,
    pose,
    mood: computed(() => pose.value.id),
    status: computed(() => pose.value.status),
    accept,
    signal,
    sending,
    interact,
    seed,
    stop,
  };
}

/**
 * The earlier call shape, kept so App.vue and Pet.vue work unchanged until
 * their parent patches (P9, P10) pass real counts. `active` is ignored on
 * purpose: it is set optimistically before the host admits a run (F3), and the
 * companion now follows the host's `run` events instead.
 */
export function useAvatarActivity(_active: Ref<boolean>, loading: Ref<boolean>, pending: Ref<boolean>) {
  const companion = useCompanion({
    approvals: () => (pending.value ? 1 : 0),
    model: () => (loading.value ? "loading" : undefined),
  });
  // The host's `model` events carry the real state (ready, error, unloaded);
  // only end a loading state this flag started if no event has replaced it.
  const halt = watch(loading, (on) => {
    if (!on && companion.state.value.model === "loading") companion.signal({ k: "model", state: "ready" });
  });
  if (getCurrentScope()) onScopeDispose(halt);
  return { mood: companion.mood, accept: companion.accept, companion };
}
