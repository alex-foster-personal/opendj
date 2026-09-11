/**
 * Shared-graph entry point for the PREF-02 feedback_mark tests.
 *
 * loadTypeScriptModule bundles each entry independently, so loading
 * audio-engine.svelte.ts and performance-ipc.svelte.ts as two separate entries
 * yields two separate copies of `deckStates` - a test that writes to one and
 * reads through the other silently proves nothing.
 *
 * Re-exporting both from a single entry puts them in one bundle, so `deckStates`
 * here is the exact object `dispatchPerformanceCommand` / `queryPerformanceState`
 * close over. No stubs: both modules are the real thing.
 */
export { deckStates, mixerState } from '$lib/rb/audio-engine.svelte';
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	performanceCommandQueueScopes,
	queryPerformanceState
} from '$lib/rb/performance-ipc.svelte';
