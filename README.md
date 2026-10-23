# Pebble media

Images that the README on `main` embeds: the Pebble companion drawn by the app's own animation engine (bloub, MIT;
rendered from `src/components/Avatar.vue` with Vue's server renderer), and screenshots of the app with synthetic demo
data. Nothing here is code or part of a release.

## README captures (`readme/`)

These are stills of Pebble 1.0.1's own interface, captured for the README with synthetic demo data. Every name, file,
number and conversation in them is fictional. Each image has a light twin (`-light`), and the PNGs carry no metadata.

| File | Shows |
|---|---|
| `01-hero` | The main window in the Companion space, with the floating pebble |
| `02-project-edit` | Two edit cards waiting for approval in "Ask for approval" |
| `03-agentguard` | AgentGuard holding a task that changed files without running their tests |
| `03b-agentguard-challenge` | AgentGuard's scope challenge, as the refused step's error in Activity |
| `04-memory` | Settings › Memory: the six layers and one layer's records |
| `04b-memory-forget` | The in-chat Forget card after one Forget |
| `05-views` | A generated dashboard in the canvas |
| `06-pebble-gaze.gif` | The pebble's eyes following a pointer that circles it |
| `07-stealth-veil.gif` | Stealth Lockdown's Veil (1.1.0): the cast, one shimmer, the release. Rendered frame by frame from `src/components/Avatar.vue` on a fake clock (`scripts/render_veil_media.mjs`, `scripts/render_veil_gif.py`), since a protected window can't be screen-recorded |
