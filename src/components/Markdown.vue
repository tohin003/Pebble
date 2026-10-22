<script lang="ts">
// Phase 24 helpers, exported for the window's fixtures (Phase24Shell.test.mjs).
export const ALLOWED_TAGS = [
  "p", "br", "strong", "em", "del", "code", "pre", "ul", "ol", "li", "blockquote",
  "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td", "a",
];
/**
 * V1-122: host chrome around sanitized HTML: a Copy button before each code block and a scrollable, focusable region
 * around each table. Only Pebble's own constant markup is added; the sanitized text is unchanged (the sanitizer leaves
 * these tags without attributes, and any "<" in text is already escaped, so the literal tags are the elements).
 */
export function decorate(html: string): string {
  return html
    .replace(/<pre>/g, '<div class="code-block"><button type="button" class="code-copy" aria-label="Copy code">Copy</button><pre>')
    .replace(/<\/pre>/g, "</pre></div>")
    .replace(/<table>/g, '<div class="table-scroll" role="region" aria-label="Table" tabindex="0"><table>')
    .replace(/<\/table>/g, "</table></div>");
}
export const LINK_REFUSED = "Pebble opens only secure web links (https).";
/** A link the person's browser may open: https, or a local preview this Mac serves. */
export function openable(href: string | null | undefined): href is string {
  return typeof href === "string" && (href.startsWith("https://") || href.startsWith("http://127.0.0.1:"));
}
interface FrameClock {
  request(fn: () => void): unknown;
  cancel(id: unknown): void;
}
function frameClock(): FrameClock {
  if (typeof requestAnimationFrame === "function")
    return { request: (fn) => requestAnimationFrame(fn), cancel: (id) => cancelAnimationFrame(id as number) };
  return { request: (fn) => setTimeout(fn, 16), cancel: (id) => clearTimeout(id as ReturnType<typeof setTimeout>) };
}
/** V1-081: run `work` at most once per animation frame, however often it is requested. */
export function perFrame(work: () => void, clock: FrameClock = frameClock()) {
  let id: unknown = null;
  return {
    request() {
      if (id === null)
        id = clock.request(() => {
          id = null;
          work();
        });
    },
    cancel() {
      if (id !== null) clock.cancel(id);
      id = null;
    },
  };
}
</script>

<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { native, rpc } from "../bridge";
const props = defineProps<{ text: string }>();
const root = ref<HTMLElement | null>(null);
const html = ref("");
// Parsed once now (history and first paint), then at most once per frame while a reply streams: 0.6.1 re-parsed
// the whole reply for every token in both windows (738 ms per 2,000-word reply).
function render() {
  html.value = decorate(
    DOMPurify.sanitize(marked.parse(props.text, { breaks: true, async: false }) as string, {
      ALLOWED_TAGS,
      ALLOWED_ATTR: ["href", "title"],
      FORBID_ATTR: ["style"],
    }),
  );
}
render();
const frame = perFrame(render);
watch(() => props.text, () => frame.request());
let copiedTimer: ReturnType<typeof setTimeout> | null = null;
onUnmounted(() => {
  frame.cancel();
  if (copiedTimer !== null) clearTimeout(copiedTimer);
});
// A notice for the window's polite toast (App.vue and Pet.vue listen for it): fixed host words, never link text.
function notice(text: string) {
  root.value?.dispatchEvent(new CustomEvent("pebble-notice", { bubbles: true, detail: text }));
}
async function copyCode(button: Element) {
  const code = button.parentElement?.querySelector("pre")?.textContent ?? "";
  try {
    await navigator.clipboard.writeText(code);
    button.textContent = "Copied";
    if (copiedTimer !== null) clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => {
      copiedTimer = null;
      button.textContent = "Copy";
    }, 1600);
    notice("Copied the code");
  } catch {
    notice("Pebble couldn't copy the code.");
  }
}
function click(e: MouseEvent) {
  const target = e.target as Element | null;
  const copy = target?.closest?.(".code-copy");
  if (copy) {
    e.preventDefault();
    void copyCode(copy);
    return;
  }
  const anchor = target?.closest?.("a");
  if (!anchor) return;
  e.preventDefault();
  const href = anchor.getAttribute("href");
  if (!openable(href)) return notice(LINK_REFUSED);
  if (!native) return notice("Links open in the desktop app.");
  rpc("open_url", { url: href }).catch(() => notice("Pebble couldn't open that link."));
}
</script>
<template><div ref="root" class="markdown" v-html="html" @click="click" /></template>
