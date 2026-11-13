/**
 * One bundle for the hotkey rejection tests (#12275): the real
 * installPerformanceHotkeys listener and the real performance-ipc dispatcher,
 * so the key press, the command status and the toast store are the same
 * module instances the app uses.
 */
export { installPerformanceHotkeys } from '$lib/rb/performance-hotkeys';
export {
	installPerformanceBrowserIpc,
	performanceCommandStatus,
	runPerformanceCommandFromUi,
	setRescueRestorePhaseForTest
} from '$lib/rb/performance-ipc.svelte';
export { getDeckState } from '$lib/rb/audio-engine.svelte';
export { noteRecentDeck } from '$lib/rb/recent-deck';
export { toasts } from '$lib/stores.svelte';
