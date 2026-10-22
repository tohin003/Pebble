<script setup lang="ts">
import { onUnmounted, ref } from "vue";

// Actions under a message (Phase 22; design study §5.3), shown on hover, on keyboard focus and
// always under the latest reply. Copy writes the message's own text exactly as stored: the person's
// words as sent, or the reply's Markdown source; the window's toast says "Copied". Edit and Retry
// only emit: the window opens an editor or sends the same words again as a new turn through
// send(), so nothing is re-run silently and nothing stored changes. A Retry that cannot run says
// why (`retryReason`). The default slot holds this message's usage, shown in the same quiet row.
const props = withDefaults(
  defineProps<{
    text: string;
    role: "user" | "assistant";
    edit?: boolean;
    retry?: boolean;
    retryReason?: string;
    disabled?: boolean;
    copier?: (text: string) => Promise<void>;
  }>(),
  { edit: false, retry: false, retryReason: "", disabled: false, copier: undefined },
);
const emit = defineEmits<{ edit: [text: string]; retry: []; copied: []; failed: [message: string] }>();
const copied = ref(false);
let timer: ReturnType<typeof setTimeout> | null = null;

async function copy() {
  const write = props.copier || ((text: string) => navigator.clipboard.writeText(text));
  try {
    await write(props.text);
    copied.value = true;
    emit("copied");
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      copied.value = false;
    }, 1600);
  } catch (error) {
    emit("failed", "Couldn't copy: " + (error instanceof Error ? error.message : String(error)));
  }
}
function askAgain() {
  if (!props.retryReason && !props.disabled) emit("retry");
}
onUnmounted(() => {
  if (timer !== null) clearTimeout(timer);
  timer = null;
});
</script>

<template>
  <div class="message-actions" :class="role" role="group" :aria-label="role === 'user' ? 'Your message' : 'Pebble’s reply'">
    <button
      type="button"
      class="message-action"
      :aria-label="role === 'user' ? 'Copy your message' : 'Copy Pebble’s reply'"
      :title="copied ? 'Copied' : role === 'user' ? 'Copy your message' : 'Copy reply (Markdown)'"
      @click="copy"
    >
      <svg v-if="!copied" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false">
        <rect x="8.5" y="8.5" width="11" height="11" rx="2.5" />
        <path d="M15.5 8.5V6.8A2.3 2.3 0 0 0 13.2 4.5H6.8A2.3 2.3 0 0 0 4.5 6.8v6.4a2.3 2.3 0 0 0 2.3 2.3h1.7" />
      </svg>
      <svg v-else viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false">
        <path d="M5 12.5l4.5 4.5L19 7.5" />
      </svg>
    </button>
    <button
      v-if="edit"
      type="button"
      class="message-action"
      aria-label="Edit your message"
      title="Edit: send a new version as a new message; this one stays"
      :disabled="disabled"
      @click="emit('edit', text)"
    >
      <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false">
        <path d="M14.5 5.5l4 4M4.5 19.5l1-4.5 10-10 3.5 3.5-10 10z" />
      </svg>
    </button>
    <button
      v-if="retry"
      type="button"
      class="message-action"
      :aria-label="retryReason ? 'Retry is unavailable: ' + retryReason : 'Retry: ask again as a new message'"
      :title="retryReason || 'Retry: ask the same question again as a new message; this reply stays'"
      :aria-disabled="retryReason || disabled ? 'true' : undefined"
      :class="{ unavailable: !!retryReason || disabled }"
      @click="askAgain"
    >
      <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false">
        <path d="M19 12a7 7 0 1 1-2.05-4.95M19 4.5V9h-4.5" />
      </svg>
    </button>
    <span v-if="$slots.default" class="message-meta"><slot /></span>
  </div>
</template>

<style scoped>
.message-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  min-height: 28px;
  margin-top: 4px;
  opacity: 0;
  transition: opacity 0.15s ease;
}
.message-actions.user {
  justify-content: flex-end;
}
.message-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: var(--radius-md, 8px);
  color: var(--muted);
}
.message-action:hover:not(:disabled):not(.unavailable) {
  color: var(--text);
  background: var(--hover);
}
.message-action.unavailable {
  cursor: default;
  opacity: 0.45;
}
.message-action svg {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.message-meta {
  margin-left: 8px;
  min-width: 0;
}
.message-actions.user .message-meta {
  order: -1;
  margin: 0 8px 0 0;
}
@media (prefers-reduced-motion: reduce) {
  .message-actions {
    transition: none;
  }
}
</style>
