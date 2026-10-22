// Local animation choreography. Host events choose a pose (companionState.ts);
// elapsed time only supplies movement inside that pose. This never reads
// prompts or asks a model to select an emotion.
//
// Accessibility (design study §2.5): cue scale and opacity pulse at CUE_HZ
// (0.5 Hz), far under three luminance changes a second, and body squash stays
// at or under 0.5 Hz. Reduced motion returns the same frame at every time.
import { visualFor } from "./companionState";

export const CUE_HZ = 0.5;
const TAU = Math.PI * 2;

export function avatarMotion(state: string, seconds: number, reduced = false) {
  const visual = visualFor(state);
  const t = reduced || !Number.isFinite(seconds) ? 0 : seconds;
  // A sine at `hz` cycles per second.
  const wave = (hz: number, offset = 0) => Math.sin(TAU * hz * t + offset);
  let x = 0,
    y = 0,
    tilt = 0,
    sx = 1,
    sy = 1;
  switch (visual.id) {
    case "thinking":
    case "working":
    case "delegating":
      tilt = 6 * wave(0.27);
      y = -3 * (1 + wave(0.37));
      break;
    case "writing":
      y = -2 * (1 + wave(0.45));
      tilt = 2 * wave(0.3);
      break;
    case "sending":
      y = -2 * (1 + wave(0.5));
      break;
    case "loading":
    case "reconnecting":
      y = -4 * (1 + wave(visual.id === "loading" ? 0.48 : 0.3));
      sy = 1 + 0.025 * wave(0.48);
      break;
    case "reading":
    case "searching":
      tilt = 4 * wave(0.32);
      x = 2 * wave(visual.id === "searching" ? 0.45 : 0.32);
      break;
    case "editing":
    case "viewing":
      y = -5 * Math.abs(wave(0.25));
      sx = 1 + 0.03 * wave(0.48);
      sy = 1 - 0.03 * wave(0.48);
      break;
    case "testing":
    case "running":
      tilt = 5 * wave(0.48);
      y = -3 * Math.abs(wave(0.24));
      break;
    case "review":
    case "question":
      tilt = 5;
      y = -2 * wave(0.32);
      break;
    case "happy":
      // A gentle bounce; the celebrate cue pulses at CUE_HZ.
      y = -6 * Math.abs(wave(0.25));
      tilt = 5 * wave(0.25);
      break;
    case "tap":
      y = -3;
      break;
    case "hover":
      tilt = 3;
      break;
    case "sleepy":
    case "paused":
      tilt = (visual.id === "sleepy" ? 12 : 6) + 2 * wave(0.16);
      sy = 0.97;
      break;
    case "error":
    case "offline":
      tilt = -7;
      y = 2;
      break;
    case "blocked":
    case "uncertain":
      tilt = -3;
      break;
    case "stopped":
      tilt = -4;
      break;
  }
  if (reduced) {
    x = 0;
    y = 0;
    tilt = visual.id === "sleepy" ? 8 : 0;
    sx = sy = 1;
  }
  // Three cue elements pulse in turn, each in [0, 1] at CUE_HZ.
  const pulse = [0, 1, 2].map((i) => (reduced ? 0.6 : 0.5 + 0.5 * wave(CUE_HZ, -i * 2.1)));
  return {
    transform: `translate(${x.toFixed(2)} ${y.toFixed(2)}) rotate(${tilt.toFixed(2)}) scale(${sx.toFixed(3)} ${sy.toFixed(3)})`,
    cue: visual.cue,
    pulse,
    t,
    x,
    y,
    tilt,
    sx,
    sy,
  };
}
