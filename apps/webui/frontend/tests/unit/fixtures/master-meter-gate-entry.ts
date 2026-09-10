/**
 * Shared-graph entry point for the master-meter RAF gate test.
 *
 * `loadTypeScriptModule` bundles each entry independently, so loading
 * playing-gate.ts and audio-engine.svelte.ts as two separate entries would
 * give two unrelated copies of `deckStates` - a test that mutated one and
 * read the gate's verdict through the other would pass no matter what the
 * code does (same trap `deck-observer-entry.ts` documents). Re-exporting
 * both from one entry means the state this test drives IS the state
 * `anyDeckPlaying` reads: no stub deck, no hand-rolled predicate.
 */
export { anyDeckPlaying } from '$lib/rb/playing-gate';
export { deckStates, DECK_IDS } from '$lib/rb/audio-engine.svelte';
