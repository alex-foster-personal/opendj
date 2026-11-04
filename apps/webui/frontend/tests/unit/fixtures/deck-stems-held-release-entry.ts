/**
 * Shared-graph entry for deck-stems-held-release.test.mjs (STEM-4756).
 *
 * One bundle holds the REAL engine and the perf ring it writes to, so the
 * `deck-stems ... deferredToStop=1` row the test waits for is read from the
 * same ring instance the engine appended to. Two separate bundles would give
 * the test an empty ring, and "no held row" would read as "nothing was held".
 */
export { deckStates, engine } from '$lib/rb/audio-engine.svelte';
export { readPerfEvents, resetPerfEventLog } from '$lib/rb/perf-event-log';
