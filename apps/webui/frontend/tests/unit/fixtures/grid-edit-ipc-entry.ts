/**
 * Shared-graph entry point for the grid_edit dispatcher tests (GRIDEDIT-6235d).
 *
 * One bundle, so `deckStates` here is the exact object the dispatcher reads
 * (see feedback-mark-entry.ts for why two entries would prove nothing).
 */
export { deckStates } from '$lib/rb/audio-engine.svelte';
export {
	abortPreparedPerformancePreset,
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	preparePerformancePresetTransaction
} from '$lib/rb/performance-ipc.svelte';
