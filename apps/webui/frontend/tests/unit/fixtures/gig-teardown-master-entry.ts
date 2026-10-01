/**
 * Shared-graph entry point for the Gig-teardown master-restoration tests
 * (gig-teardown-remount.test.mjs, discussion_r4119767451).
 *
 * `loadTypeScriptModule` bundles each entry independently, so loading
 * audio-engine.svelte.ts and player/state.svelte.ts as two separate entries
 * gives two unrelated copies of `_masterWriteRevision` (same trap
 * master-meter-gate-entry.ts documents for `deckStates`): a test that wrote
 * through `engine.setMaster` from one bundle and read
 * `readMasterWriteRevision` from the other would see it never move,
 * regardless of what the real code does. `mixerState` alone survives this
 * because it opts into a globalThis-keyed singleton; the write-revision
 * counter is a plain module-scope `let` and does not. Re-exporting both from
 * one entry means the revision this test reads IS the one `engine.setMaster`
 * actually bumps.
 */
export { engine, ensureAudioGraphForCue } from '$lib/rb/audio-engine.svelte';
export { mixerState, readMasterWriteRevision } from '$lib/player/state.svelte';
