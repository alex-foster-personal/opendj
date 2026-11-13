/**
 * One bundle, so the test loads the same deck state, build-flag store and
 * recorder the /performance hotkey listener reads. `recorded` comes from the
 * stub by its own path; the test aliases `$lib/rb/performance-ipc.svelte` to
 * that same file, so both are one module.
 */
export { installPerformanceHotkeys } from '$lib/rb/performance-hotkeys';
export { buildFlags } from '$lib/api/store-build.svelte';
export { deckStates } from '$lib/player/state.svelte';
export { _resetRecentDeckForTests } from '$lib/rb/recent-deck';
export { recorded } from './grid-jump-keys-repeat-stub-ipc';
