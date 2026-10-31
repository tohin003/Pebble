import { createApp } from "vue";
import "./style.css";
// Phase 22 (design study §11): pick the surface first, then load only its code. The pet window never
// parses the main window (Settings, the canvas and its renderers, the Details drawer), and the main
// window never parses the pet.
// Phase 23: each surface is its own import() statement. Vite gives one preload list to each import site, so a
// single `pet ? import(Pet) : import(App)` expression made the pet window load App's stylesheet instead of its own
// (0.6.0: an opaque, unstyled companion window). Keep them separate (src/surfaceEntry.test.mjs).
const pet = new URLSearchParams(location.search).has("pet");
// Phase 45 (F054, F060): the web view's own menu (its only item was Reload) never shows; text fields and selected text
// keep theirs (Copy, Paste, Look Up).
document.addEventListener("contextmenu", (event) => {
  const target = event.target as HTMLElement | null;
  const editable = target?.closest?.("input, textarea, [contenteditable='true']");
  if (!editable && !String(window.getSelection?.() || "").trim()) event.preventDefault();
});
if (pet) {
  document.documentElement.classList.add("pet-surface");
  import("./Pet.vue").then(({ default: root }) => createApp(root).mount("#app"));
} else {
  import("./App.vue").then(({ default: root }) => createApp(root).mount("#app"));
}
