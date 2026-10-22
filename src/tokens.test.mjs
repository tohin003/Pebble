import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Phase 24 (V1-039, i5-ux-01; V1-041 off switch): every text colour token reaches WCAG AA (4.5:1) on every surface
// colour token, in the light and the dark theme, and the off switch's track is a visible control (3:1 on the panels
// it sits on). The tokens are read from tokens.css itself, so a new or changed colour is checked where it is defined.
const css = readFileSync(new URL("./tokens.css", import.meta.url), "utf8");
const block = (selector) => {
  const start = css.indexOf(selector + " {");
  assert.ok(start >= 0, "missing " + selector);
  return css.slice(start, css.indexOf("}", start));
};
function tokens(text) {
  const out = {};
  for (const [, name, value] of text.matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)) out[name] = value.trim();
  return out;
}
const light = tokens(block(":root"));
const dark = { ...light, ...tokens(block(':root[data-theme="dark"]')) };
function hex(theme, name, seen = new Set()) {
  const value = theme[name];
  assert.ok(value, "token --" + name);
  const ref = /^var\(--([a-z0-9-]+)\)$/.exec(value);
  if (ref) {
    assert.ok(!seen.has(name), "a token loop at --" + name);
    return hex(theme, ref[1], new Set([...seen, name]));
  }
  assert.match(value, /^#[0-9a-f]{3}(?:[0-9a-f]{3})?$/i, "--" + name + " is an opaque colour");
  return value.length === 4 ? "#" + [...value.slice(1)].map((c) => c + c).join("") : value;
}
function luminance(colour) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(colour.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export function contrast(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}
// m24c merge: the grid is exactly the release requirement: the four text tokens on the nine surface tokens (the user
// bubble included, and --surface though it is --panel today, so a later change to it is checked), in both themes.
// The accent, used as link and label text, is held to the same bar.
const TEXT = ["text", "secondary", "muted", "danger"];
const SURFACES = ["bg", "side", "panel", "surface", "soft", "code", "user-bubble", "hover", "selected"];
const THEMES = [["light", light], ["dark", dark]];
const worst = (theme, colour) => SURFACES.map((s) => [s, contrast(colour, hex(theme, s))]).sort((a, b) => a[1] - b[1])[0];

test("the contrast formula matches WCAG's reference values", () => {
  assert.equal(contrast("#000000", "#ffffff").toFixed(2), "21.00");
  assert.equal(contrast("#767676", "#ffffff").toFixed(2), "4.54");
  // i5-ux-01's measurements of 0.6.1's light --muted (#939b93) on white and its own fix.
  assert.ok(contrast("#939b93", "#ffffff") < 3);
  assert.ok(contrast("#646d65", "#f0f5ed") >= 4.5);
});

for (const [name, theme] of THEMES) {
  test(`every text token on every surface token is at least 4.5:1 (${name} theme)`, () => {
    const failing = [], checked = [];
    for (const text of [...TEXT, "accent"])
      for (const surface of SURFACES) {
        const ratio = contrast(hex(theme, text), hex(theme, surface));
        checked.push(text + "/" + surface);
        if (ratio < 4.5) failing.push(`--${text} ${hex(theme, text)} on --${surface} ${hex(theme, surface)}: ${ratio.toFixed(2)}`);
      }
    assert.equal(checked.length, (TEXT.length + 1) * SURFACES.length, "the whole grid");
    assert.deepEqual(failing, [], name + ":\n" + failing.join("\n"));
    // Text on the accent fill (primary buttons, badges).
    assert.ok(contrast(hex(theme, "on-accent"), hex(theme, "accent")) >= 4.5, name + " --on-accent on --accent");
    console.log(name + " worst pairs: " + [...TEXT, "accent"].map((t) => {
      const [surface, ratio] = worst(theme, hex(theme, t));
      return `--${t} on --${surface} ${ratio.toFixed(2)}:1`;
    }).join("; "));
  });
  test(`the off switch is a visible control: its track reaches 3:1 on the panels (${name} theme)`, () => {
    for (const surface of ["panel", "bg", "side"])
      assert.ok(contrast(hex(theme, "control-off"), hex(theme, surface)) >= 3, name + " --control-off on --" + surface);
    // The thumb stays visible on the off and on tracks.
    assert.ok(contrast(hex(theme, "panel"), hex(theme, "control-off")) >= 3, name + " thumb on the off track");
  });
}

// Each text token Phase 24 changed, with its 0.6.1 value: the old value failed somewhere on the grid, the new one
// passes everywhere. 0.6.1's light --secondary #626c64 is the value the extension popup still copied and the one the
// validator measured at 4.49:1 on --selected (t24-validator note §4.2). Every other text token is unchanged.
const CHANGED = [
  ["light", "secondary", "#626c64", "#5d665e"],
  ["light", "muted", "#939b93", "#636b63"],
  ["light", "danger", "#ad5a4a", "#a45141"],
  ["dark", "muted", "#7c897a", "#94a192"],
];
test("measured: each changed text token, 0.6.1 and now, with its worst pair", () => {
  const rows = [];
  for (const [name, token, before, after] of CHANGED) {
    const theme = name === "light" ? light : dark;
    assert.equal(hex(theme, token), after, name + " --" + token);
    const [was, low] = worst(theme, before), [is, high] = worst(theme, after);
    assert.ok(low < 4.5 && high >= 4.5, `${name} --${token}: ${low.toFixed(2)} -> ${high.toFixed(2)}`);
    rows.push(`${name} --${token} ${before} (worst --${was} ${low.toFixed(2)}:1) -> ${after} (worst --${is} ${high.toFixed(2)}:1)`);
  }
  rows.push(`light off switch on --panel ${contrast("#c7cec3", hex(light, "panel")).toFixed(2)}:1 -> ${contrast(hex(light, "control-off"), hex(light, "panel")).toFixed(2)}:1`);
  console.log(rows.join("; "));
});

test("the inspection's three corrections are in place", () => {
  // i5-ux-01 proposed #646d65 and computed #636b63 as the minimum that passes on every light surface; #646d65 is
  // 4.41:1 on --selected (#e5ebe0), so the computed minimum is used.
  assert.ok(contrast("#646d65", "#e5ebe0") < 4.5 && contrast("#636b63", "#e5ebe0") >= 4.5);
  assert.equal(light.muted, "#636b63");
  assert.equal(dark.muted, "#94a192");
  assert.equal(light.danger, "#a45141");
});

// m24c merge: Pebble for Chrome's popup keeps its own copy of the colour tokens (it cannot load the app's CSS). Its
// text tokens meet the same bar on the popup's own surfaces, in both themes.
test("the Chrome extension's popup text tokens reach 4.5:1 on its surfaces (both themes)", () => {
  const popup = readFileSync(new URL("../browser-extension/popup.css", import.meta.url), "utf8");
  const lightBlock = popup.slice(popup.indexOf(":root {"), popup.indexOf("}", popup.indexOf(":root {")));
  const darkStart = popup.indexOf(":root {", popup.indexOf("@media (prefers-color-scheme: dark)"));
  const popupLight = tokens(lightBlock);
  const popupDark = { ...popupLight, ...tokens(popup.slice(darkStart, popup.indexOf("}", darkStart))) };
  const failing = [], lowest = [];
  for (const [name, theme] of [["light", popupLight], ["dark", popupDark]])
    for (const text of TEXT) {
      let low = Infinity;
      for (const surface of ["bg", "panel", "soft"]) {
        const ratio = contrast(hex(theme, text), hex(theme, surface));
        low = Math.min(low, ratio);
        if (ratio < 4.5) failing.push(`${name} popup --${text} ${hex(theme, text)} on --${surface}: ${ratio.toFixed(2)}`);
      }
      lowest.push(`${name} --${text} ${low.toFixed(2)}:1`);
    }
  assert.deepEqual(failing, [], failing.join("\n"));
  console.log("popup lowest: " + lowest.join("; "));
});
