import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { before, test } from "node:test";

import { engineBlockAfter } from "./engine-source.mjs";
import { loadTypeScriptModule } from "./load-typescript.mjs";

// 863c0eb6 - ControlExplainer wiring + Beat Sync phase-lock UX.
//
// The commit makes one promise to the DJ: the pitch window the explainer
// ADVERTISES is the pitch window the engine ENFORCES, and when a lock inside
// that window is impossible the BEAT SYNC button reverts rather than sitting
// lit with no schedule behind it.
//
// That promise spans two files with two independent copies of the same
// arithmetic: `_tempoBounds` in audio-engine.svelte.ts (what is enforced) and
// `tempoBoundsFromPitchRange` in auto-play.ts (what DeckHeader prints).
// ControlExplainer.svelte says only "Copy must match audio-engine" in a
// comment, which is exactly the shape of rule that a refactor changes on one
// side and leaves stale on the other - the UI then lies about when sync will
// engage, and the revert error quotes a window nobody enforces.
//
// Regression lines:
// - if the two bounds formulas disagree for any pitch range then the explainer
//   advertises a window the engine does not enforce
// - if DeckHeader hardcodes the window instead of deriving it then changing the
//   pitch range silently desyncs the copy from the engine
// - if the Beat Sync bullets stop stating the revert then a lit-but-dead button
//   is undocumented behaviour
// - if the setBeatSync catch stops clearing beat_sync_enabled then BEAT SYNC
//   stays lit with no schedule - the exact bug 863c0eb6 fixed
// - if ControlExplainer is unmounted from CUE/SLIP/BEAT SYNC/MASTER then the
//   teaching chrome silently disappears

// The engine half of this guard reads through engine-source.mjs, which tracks a
// LIST of engine source files. T4 splits audio-engine.svelte.ts into player/*
// behind a barrel; a guard hardcoding the old path would then grep re-exports
// and assert against nothing while still reporting green.
const SRC = fileURLToPath(new URL("../../src", import.meta.url));

/**
 * Read source with LF endings whatever the checkout used. The drift guards
 * below match multi-line shapes with `\n`, and a Windows checkout carries
 * CRLF -- the guard would then fail on the line endings rather than on the
 * arithmetic it exists to watch.
 */
function readSource(relative) {
  return readFileSync(`${SRC}/${relative}`, "utf8").replaceAll("\r\n", "\n");
}

const DECK_HEADER_SRC = readSource("lib/components/rb/deck/DeckHeader.svelte");
const JOG_DIAL_SRC = readSource("lib/components/rb/deck/JogDial.svelte");
const TRANSPORT_SRC = readSource(
  "lib/components/rb/deck/TransportCluster.svelte",
);

let autoPlay;
let audio;

before(async () => {
  autoPlay = await loadTypeScriptModule("src/lib/rb/auto-play.ts");
  audio = await loadTypeScriptModule("src/lib/rb/audio-engine.svelte.ts");
});

/**
 * Build a callable from the engine's real `_tempoBounds` body so the two
 * formulas are compared by the values they produce, not by their text. A
 * cosmetic reformat must not fail this; a changed number must.
 */
function enforcedBoundsFn() {
  // engineBlockAfter fails loudly when _tempoBounds is renamed, reshaped or
  // absent from every listed engine source - it never hands back an empty body.
  const body = engineBlockAfter(
    "function _tempoBounds(deck: DeckId): { min: number; max: number } {",
  ).replace(/pitchRanges\[deck\]/g, "pct");
  return new Function("pct", body);
}

test("the advertised pitch window is the enforced pitch window, for every pitch range", () => {
  const enforced = enforcedBoundsFn();
  assert.ok(
    Array.isArray(audio.PITCH_RANGES) && audio.PITCH_RANGES.length > 0,
    "if PITCH_RANGES stops being a non-empty array then this comparison loops zero " +
      "times and passes without comparing anything",
  );
  for (const pct of audio.PITCH_RANGES) {
    const advertised = autoPlay.tempoBoundsFromPitchRange(pct);
    assert.deepEqual(
      enforced(pct),
      advertised,
      `if the engine and the explainer disagree at +-${pct}% then the UI ` +
        "promises a lock the engine will refuse",
    );
  }
});

test("the Beat Sync explainer derives its window from the real bounds function", () => {
  assert.match(
    DECK_HEADER_SRC,
    /tempoBoundsFromPitchRange\(pitchRanges\[deckId\]\)/,
    "if the window is not derived from the deck pitch range then switching " +
      "+-8/16/100% leaves the copy stale",
  );
  assert.match(
    DECK_HEADER_SRC,
    /pitch range \[\$\{syncBounds\.min\}, \$\{syncBounds\.max\}\]/,
    "if the bounds are hardcoded rather than interpolated then the explainer " +
      "can drift from the engine without any test noticing",
  );
});

test("the Beat Sync explainer states that an impossible lock reverts the button", () => {
  const bullets = DECK_HEADER_SRC.slice(
    DECK_HEADER_SRC.indexOf("beatSyncBullets"),
    DECK_HEADER_SRC.indexOf("masterTitle"),
  );
  assert.match(
    bullets,
    /reverts/,
    "if the bullets stop stating the revert then the engine reverts silently " +
      "and the DJ is never told why sync did not engage",
  );
  assert.match(
    bullets,
    /BAR/,
    "if BAR is dropped from the copy then the strict 1-4 phase constraint is undocumented",
  );
});

test("setBeatSync clears the lit flag before rethrowing an impossible phase lock", () => {
  const body = engineBlockAfter(
    "setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {",
  );

  const catchAt = body.indexOf(".catch(");
  assert.ok(
    catchAt > 0,
    "if the catch is removed then a failed synchronize leaves BEAT SYNC lit " +
      "with no schedule - the bug 863c0eb6 fixed",
  );
  const revertAt = body.indexOf("st.beat_sync_enabled = false", catchAt);
  const throwAt = body.indexOf("throw new Error", catchAt);
  assert.ok(
    revertAt > catchAt && revertAt < throwAt,
    "if the flag is not cleared before the rethrow then the button stays lit " +
      "after the failure propagates",
  );
  assert.match(
    body.slice(throwAt),
    /cannot phase-lock within pitch \[\$\{bounds\.min\}, \$\{bounds\.max\}\] \(BAR\)/,
    "if the error stops naming the enforced window then the operator cannot " +
      "tell which pitch range refused the lock",
  );
});

test("ControlExplainer is mounted on CUE, SLIP, BEAT SYNC and MASTER", () => {
  for (const [label, src] of [
    ["CUE", TRANSPORT_SRC],
    ["SLIP", JOG_DIAL_SRC],
    ["BEAT SYNC / MASTER", DECK_HEADER_SRC],
  ]) {
    assert.match(
      src,
      /import ControlExplainer from '\.\/ControlExplainer\.svelte'/,
      `if ${label} drops the import then its teaching chrome is gone`,
    );
    assert.match(
      src,
      /<ControlExplainer\b/,
      `if ${label} stops mounting it then the hover is dead`,
    );
  }
  const deckHeaderMounts = DECK_HEADER_SRC.match(/<ControlExplainer\b/g) ?? [];
  assert.equal(
    deckHeaderMounts.length,
    2,
    "if DeckHeader does not mount exactly two explainers then BEAT SYNC or " +
      "MASTER has lost its own",
  );
  assert.match(
    TRANSPORT_SRC,
    /demo="cue"/,
    "if the cue demo is dropped then the SVG teach is gone",
  );
  assert.match(
    JOG_DIAL_SRC,
    /demo="slip"/,
    "if the slip demo is dropped then the SVG teach is gone",
  );
});
