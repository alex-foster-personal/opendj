/**
 * One bundle for rust-auto-gain.test.mjs: the Rust engine mode and the auto
 * gain store must share one `deckAutoGain`, or the test would publish into a
 * copy the engine never reads (see rust-mode-entry.ts for the same reason).
 */
export * from '$lib/audio-engine/rust-engine';
export { deckAutoGain, deckAutoGainApplied, publishDeckAutoGain } from '$lib/rb/auto-gain.svelte';
