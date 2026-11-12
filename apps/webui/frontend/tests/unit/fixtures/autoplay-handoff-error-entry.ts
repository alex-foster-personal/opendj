/**
 * Shared-graph entry for AutoPlay handoff reservation tests.
 *
 * The controller runs against the REAL performance dispatcher; only the audio
 * transport boundary on `engine` is faked in the test harness.
 */
export { deckStates, engine } from '$lib/rb/audio-engine.svelte';
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	performanceCommandStatus
} from '$lib/rb/performance-ipc.svelte';
export { uiPrefs } from '$lib/rb/prefs.svelte';
export { toasts } from '$lib/stores.svelte';
export {
	installAutoPlay,
	readAutoPlayHandoffInFlight,
	readAutoPlayHandoffPending,
	readAutoPlayMirrorStatus
} from '$lib/rb/auto-play.svelte';
export { setAutoPlayTrackFeed } from '$lib/rb/auto-play';
