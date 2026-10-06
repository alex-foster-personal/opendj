/** Production Beat Sync Max graph for DECKUX-37 behavioural tests: the prefs
 * setter, the engine's deck state and the performance IPC are ONE instance, so
 * a Max toggle reaches the same decks `queryPerformanceState` reads. */
export { deckStates } from '$lib/rb/audio-engine.svelte';
export { setBeatSyncMax } from '$lib/rb/prefs.svelte';
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	queryPerformanceState,
	uiPrefs
} from '$lib/rb/performance-ipc.svelte';
export {
	beatSyncMaxLoadCommand,
	beatSyncMaxToggleCommands,
	enableBeatSyncAfterLoad,
	installBeatSyncMaxEnabler
} from '$lib/rb/beat-sync-max-enable';
