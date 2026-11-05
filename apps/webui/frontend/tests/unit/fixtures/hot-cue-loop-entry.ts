/** HOTCUE-LOOP-01: the deck hot-cue actions and the IPC they dispatch through,
 * from ONE bundle, so the test's driver seam is the instance the actions reach. */
export { createDeckHotCueActions } from '$lib/rb/deck-hot-cue-actions';
export {
	installPerformanceBrowserIpc,
	installPerformanceHotCueDriverForTest,
	uiPrefs
} from '$lib/rb/performance-ipc.svelte';
