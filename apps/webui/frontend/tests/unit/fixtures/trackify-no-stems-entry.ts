/**
 * Shared-graph entry for trackify-no-stems.test.mjs (PERFMODE-15).
 *
 * One bundle holds the REAL engine, the REAL stem decode policy and the REAL
 * Trackify session, so the policy a session sets is the exact module instance
 * the engine reads. Two separate bundles would each get their own copy of the
 * policy and every "blocked" assertion would read an unblocked engine.
 */
export { deckStates, engine, upgradeDeckStemsForTest } from '$lib/rb/audio-engine.svelte';
export { blockStemDecode, stemDecodeBlockReason } from '$lib/rb/stem-decode-policy';
export { e2ePrimeTrackifyFeed } from '$lib/rb/trackify-feed.svelte';
export { resetLibraryModeRuntimeForTest } from '$lib/rb/library-mode-runtime';
export { installTrackifySession } from '$lib/rb/trackify-session.svelte';
