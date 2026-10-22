// Pebble for Chrome's service worker: Chrome glue only. The logic and its tests live in lib/.
import { createWorker } from './lib/worker.mjs';
import { iconImages } from './lib/icon.mjs';

createWorker(chrome, { draw: () => iconImages() }).start();
