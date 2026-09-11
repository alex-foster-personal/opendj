/** Production pairing snapshot command graph, exposed for behavioral tests.
 *
 * `analysisSourceState` rides along because PARITY-02's readback assertion is
 * about the SAME instance `queryPerformanceState` closes over: two separate
 * entries would give the test its own copy of the rune and prove nothing. */
export { deckStates, mixerState } from '$lib/rb/audio-engine.svelte';
export { analysisSourceState } from '$lib/rb/analysis-source.svelte';
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	queryPerformanceState,
	uiPrefs
} from '$lib/rb/performance-ipc.svelte';
