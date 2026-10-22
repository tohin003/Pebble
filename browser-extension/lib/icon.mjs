// The toolbar icon, drawn at runtime: a sage pebble with two eyes, like Pebble's companion (icons/pebble.svg is
// the same drawing for the popup). Per-tab badges carry the sharing state, so the icon itself never changes.

export function drawPebble(ctx, size) {
  const s = size / 32;
  ctx.clearRect(0, 0, size, size);
  ctx.fillStyle = '#506e54';
  ctx.beginPath();
  ctx.ellipse(16 * s, 17 * s, 13 * s, 11.5 * s, -0.12, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#ffffff';
  for (const x of [11.5, 20.5]) {
    ctx.beginPath();
    ctx.ellipse(x * s, 16 * s, 2 * s, 2.6 * s, 0, 0, Math.PI * 2);
    ctx.fill();
  }
}

// {16: ImageData, 32: ImageData} for chrome.action.setIcon, or null where OffscreenCanvas is missing.
export function iconImages(Canvas = globalThis.OffscreenCanvas) {
  if (typeof Canvas !== 'function') return null;
  const out = {};
  for (const size of [16, 32]) {
    const canvas = new Canvas(size, size);
    const ctx = canvas.getContext('2d');
    drawPebble(ctx, size);
    out[size] = ctx.getImageData(0, 0, size, size);
  }
  return out;
}
