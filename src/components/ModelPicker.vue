<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { PhCpu, PhCaretDown } from "@phosphor-icons/vue";
import type { ModelProfile } from "../types";
// Phase 24: the provider list only filters the models (V1-145: choosing a provider never switches the model; the
// person picks one) and is hidden when there is one provider (V1-163). `notice` (V1-002) says that earlier messages
// of this conversation ran on this Mac and are not sent to a cloud model unless the person reviews and includes
// them; its button only emits `review` (the main window opens the reviewed handoff, the pet the workspace).
const props = defineProps<{
  profiles: ModelProfile[];
  current?: string;
  disabled?: boolean;
  compact?: boolean;
  notice?: string;
  reviewLabel?: string;
}>();
const emit = defineEmits<{ select: [id: string]; review: []; dismiss: [] }>();
const provider = ref("all");
const connection = (p: ModelProfile) =>
  p.connection_id || (p.kind === "gguf" ? "local" : p.id);
const providers = computed(() => [
  ...new Map(
    props.profiles.map((p) => [
      connection(p),
      p.provider_name || (p.kind === "gguf" ? "On this Mac" : p.name),
    ]),
  ).entries(),
]);
const models = computed(() =>
  props.profiles.filter(
    (p) => provider.value === "all" || connection(p) === provider.value,
  ),
);
// The selected model is not in the filtered list: the list says so and nothing is selected for the person.
const outside = computed(() => !!props.current && !models.value.some((p) => p.id === props.current));
watch(
  () => props.current,
  () => {
    const current = props.profiles.find((p) => p.id === props.current);
    if (
      current &&
      provider.value !== "all" &&
      connection(current) !== provider.value
    )
      provider.value = connection(current);
  },
);
function selectProvider(event: Event) {
  provider.value = (event.target as HTMLSelectElement).value;
}
function selectModel(event: Event) {
  const id = (event.target as HTMLSelectElement).value;
  if (id && id !== props.current) emit("select", id);
}
</script>
<template>
  <div class="model-picker-wrap" :class="{ compact }">
    <div class="model-selectors" :class="{ compact }">
      <label v-if="providers.length > 1" class="composer-select provider-picker">
        <select
          :value="provider"
          :disabled="disabled || !profiles.length"
          aria-label="Show models from"
          @change="selectProvider"
        >
          <option value="all">All providers</option>
          <option v-for="[id, name] in providers" :key="id" :value="id">
            {{ name }}
          </option></select
        ><PhCaretDown :size="10" />
      </label>
      <label class="composer-select model-picker"
        ><PhCpu :size="15" />
        <select
          :value="outside ? '' : current || ''"
          :disabled="disabled || !models.length"
          aria-label="Choose model"
          @change="selectModel"
        >
          <option v-if="!models.length" value="">Choose model</option>
          <option v-else-if="outside" value="" disabled>Choose a model from this provider</option>
          <option v-for="p in models" :key="p.id" :value="p.id">
            {{ p.display_name || p.model || p.name }}
          </option></select
        ><PhCaretDown :size="10" />
      </label>
    </div>
    <p v-if="notice" class="model-notice" role="status">
      <span>{{ notice }}</span>
      <button type="button" class="text-button" @click="emit('review')">{{ reviewLabel || "Review…" }}</button>
      <button type="button" class="icon-button model-notice-close" aria-label="Dismiss this note" @click="emit('dismiss')">
        <span aria-hidden="true">×</span>
      </button>
    </p>
  </div>
</template>
<style scoped>
.model-picker-wrap {
  position: relative;
  display: flex;
  align-items: center;
  min-width: 0;
}
.model-picker-wrap.compact {
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
}
.model-selectors {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
}
.provider-picker select {
  max-width: 130px;
}
.model-picker select {
  max-width: 165px;
}
.compact .provider-picker select {
  max-width: 95px;
}
.compact .model-picker select {
  max-width: 135px;
}
.compact {
  font-size: 12px;
}
.compact .composer-select {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 4px;
  border-radius: 7px;
}
.compact select {
  border: 0;
  background: transparent;
  color: inherit;
  appearance: none;
  outline-offset: 2px;
  font: inherit;
}
/* V1-002: above the composer's tools in the main window; in the pet's model menu, below the selects. */
.model-notice {
  position: absolute;
  left: 0;
  bottom: calc(100% + 8px);
  z-index: 4;
  display: flex;
  align-items: center;
  gap: 8px;
  width: max-content;
  max-width: min(520px, 80vw);
  margin: 0;
  padding: 8px 10px;
  border: 1px solid var(--accent-soft);
  border-radius: 9px;
  background: var(--panel);
  box-shadow: var(--shadow-soft);
  color: var(--text);
  font-size: 12px;
  line-height: 1.5;
}
.compact .model-notice {
  position: static;
  width: auto;
  max-width: none;
  box-shadow: none;
}
.model-notice span {
  flex: 1;
  min-width: 0;
}
.model-notice .text-button {
  margin: 0;
  font-size: 12px;
  color: var(--accent);
  white-space: nowrap;
}
.model-notice-close {
  width: 24px;
  height: 24px;
}
@media (max-width: 850px) {
  .provider-picker select {
    max-width: 100px;
  }
  .model-picker select {
    max-width: 135px;
  }
}
</style>
