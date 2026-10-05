/**
 * One bundle for usb-stick-session-edits.test.mjs: the performance IPC, the
 * api-rb hot cue functions, the deck engine's rating path, getTrack and the
 * toast store, all over ONE copy of lib/rb/stick-session-edits.ts.
 *
 * `loadTypeScriptModule` bundles each entry independently, so loading the IPC
 * and api-rb as two entries would give the test two session stores: the slots
 * the test reads through one would never be the slots the IPC save checks its
 * revision against. No stubs: every module here is the real one, and the only
 * seams are `fetch` and the IPC's own documented hot cue driver.
 */
export * as ipc from '$lib/rb/performance-ipc.svelte';
export { clearHotCue, fetchAnlz, fetchHotCueSlots, restoreHotCue, saveHotCue } from '$lib/rb/api-rb';
export { deckStates, rateDeckTrack } from '$lib/rb/audio-engine.svelte';
export { getTrack } from '$lib/api';
export { toasts } from '$lib/stores.svelte';
export { hotCueEditsAllowed } from '$lib/rb/track-source';
