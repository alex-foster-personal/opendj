/**
 * Shared-graph entry for the real-engine unit tests (real-engine-rig.mjs).
 *
 * One bundle holds the REAL engine, the perf ring it writes to and the toast
 * store it pushes to, so a ring row or a toast a test looks for is read from
 * the same instance the engine wrote. Two separate bundles would give the test
 * an empty ring and an empty toast list, and "nothing was reported" would read
 * as a pass when nothing was measured.
 */
export { deckLoadErrors, deckStates, engine } from '$lib/rb/audio-engine.svelte';
export { readPerfEvents, resetPerfEventLog } from '$lib/rb/perf-event-log';
export { toasts } from '$lib/stores.svelte';
// The engine's own instance of the unexpected-stop reporter, so a test can ask
// what origin a stop would be attributed to right now.
export { notePlayingFallingEdge, readPauseOrigin } from '$lib/rb/unexpected-pause-report';
// The scoped sync runner a deferred beatgrid upgrade settles through. The app
// installs it from performance-ipc.svelte.ts; the rig installs the same shape.
export { DECK_IDS, installScopedSyncRunner } from '$lib/rb/audio-engine.svelte';
export { ScopedCommandScheduler } from '$lib/rb/performance-command-scheduler';
