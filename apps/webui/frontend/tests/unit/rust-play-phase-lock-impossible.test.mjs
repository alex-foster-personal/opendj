// PLAY-27 in ?engine=rust (#5738, review thread r4203671252). -Claude
// [if] a Beat Sync follower cannot phase-lock in rust mode, Max on or off [then] play still starts it on
//   plain transport and a red notice names the deck [else stop: the AutoPlay handoff aborts into silence].
// [if] the follower can lock [then] it joins with tempo, seek and play as before.
import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import { loadTypeScriptModule } from "./load-typescript.mjs";
let m, sent, positions, toasts;
before(async () => { m = await loadTypeScriptModule("tests/unit/fixtures/rust-mode-entry.ts"); });
beforeEach(() => {
  sent = []; positions = {}; toasts = [];
  m.link.toast = (message, kind) => toasts.push({ message, kind }); // the page toast performance-ipc hands over
  m.installRustEngineClientForTest({ connected: true, notBuilt: new Set(), send: (cmd) => { sent.push(cmd); return Promise.resolve({ type: "result", id: null, ok: true }); }, positionMs: (d) => positions[d] ?? null });
  m.rustMode.enabled = true; m.rustMode.keyLock = false; m.rustMaster.deck = null; m.rustMaster.mode = "auto";
  for (const d of [1, 2, 3, 4]) {
    Object.assign(m.deckStates[d], { stable_id: null, playing: false, audible: false, is_master: false, anlz: null, loop: null, cue_ms: null, position_ms: 0, pitch: 1, beat_sync_enabled: false, quantize_enabled: false, quantize_grid_beats: 1, sync_mode: "beat", sync_error: null });
    m.pitchRanges[d] = 16; m.mixerState.channels[d].fader = 1; m.mixerState.channels[d].trim = 0.5; m.mixerState.channels[d].assign = "THRU";
  }
  m.mixerState.crossfader = 0.5; m.mixerState.master = 1;
});
after(() => { m.installRustEngineClientForTest(null); m.rustMode.enabled = false; });
const grid = (bpm, count = 300) => Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: 0.1 + (i * 60) / bpm }));
const load = (d, beats, extra = {}) => Object.assign(m.deckStates[d], { stable_id: `track-${d}`, duration_ms: beats.at(-1).t * 1000, anlz: { beatgrid: { source: "rekordbox", beats, beat_count: beats.length, status: "ok" } }, ...extra });
async function master128() { load(1, grid(128)); await m.executeInRustEngine({ type: "play", deck: 1, playing: true }); positions[1] = 20_000; assert.equal(m.rustMaster.deck, 1); sent = []; }
for (const max of [true, false]) {
  test(`[PLAY-27] rust mode, Max ${max ? "on" : "off"}: an out-of-range synced follower (160 vs 128) still starts`, async () => {
    m.uiPrefs.beat_sync_max = max;
    await master128();
    load(2, grid(160), { beat_sync_enabled: true });
    let error = null;
    try { await m.executeInRustEngine({ type: "play", deck: 2, playing: true }); } catch (e) { error = e; }
    assert.equal(error, null, "if play rejects then the AutoPlay handoff aborts");
    assert.ok(sent.some((c) => c.type === "play" && c.deck === 2 && c.playing === true), "if no play reaches the engine for deck 2 then the room goes silent");
    assert.equal(sent.some((c) => c.type === "tempo" && c.deck === 2), false, "an unlocked follower keeps its own tempo");
    assert.deepEqual(toasts.map((t) => t.kind), ["error"], "the pitch bound stays hard: the refusal is a red notice");
    assert.equal(toasts[0].message, "Beat Sync cannot lock deck 2: tempo 25% away; playing unsynced");
  });
}
test("[PLAY-27] rust mode control: a compatible follower (126 vs 128) joins with tempo, seek and play", async () => {
  m.uiPrefs.beat_sync_max = true;
  await master128();
  load(2, grid(126), { beat_sync_enabled: true });
  await m.executeInRustEngine({ type: "play", deck: 2, playing: true });
  assert.ok(sent.some((c) => c.type === "tempo" && c.deck === 2));
  assert.ok(sent.some((c) => c.type === "play" && c.deck === 2));
});
