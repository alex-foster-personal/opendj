/**
 * Shared-graph entry point for the deck-observer emitter tests.
 *
 * Same reason as `perf-ipc-entry.ts`: `loadTypeScriptModule` bundles each entry
 * independently, so loading the engine and the emitter as two entries would
 * give two unrelated copies of `deckStates` and `mixerState`. A test that wrote
 * to one and read through the other would pass no matter what the code did.
 *
 * Re-exporting all of them from a single entry means the state these tests
 * drive IS the state `queryPerformanceState` reads and the emitter projects.
 * No stub engine, no hand-rolled snapshot object: the deck and mixer records,
 * the IPC query and the projection are each the real production module.
 */
export { deckStates, mixerState } from '$lib/rb/audio-engine.svelte';
export { queryPerformanceState } from '$lib/rb/performance-ipc.svelte';
export { createDeckObserverEmitter, MAX_BATCH_SNAPSHOTS } from '$lib/sets/deck-observer-emitter';
// The install half is a separate module (route/window wiring vs the emitter
// state machine) but must stay in THIS entry: it calls
// `createDeckObserverEmitter`, so importing it as its own entry would give the
// installed emitter a second copy of the engine state and the tests that drive
// `deckStates` through the installer would pass against nothing.
export { installDeckObserverEmitter, DECK_OBSERVER_GLOBAL } from '$lib/sets/deck-observer-install';
export { DeckProjectionError, toWireSnapshot } from '$lib/sets/deck-snapshot-wire';
export {
	deckWasHeard,
	everyStemPartSilent,
	externallyRoutedDecks,
	masterPathGain,
	SILENCE_GAIN_EPSILON
} from '$lib/sets/deck-audibility';
