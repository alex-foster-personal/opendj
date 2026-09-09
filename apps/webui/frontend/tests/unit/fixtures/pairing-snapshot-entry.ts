/** Production pairing snapshot command graph, exposed for behavioral tests. */
export { deckStates, mixerState } from '$lib/rb/audio-engine.svelte';
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	queryPerformanceState,
	uiPrefs
} from '$lib/rb/performance-ipc.svelte';
