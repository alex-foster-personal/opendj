/**
 * Shared-graph entry for Trackify autoplay controller tests.
 *
 * The controller runs against the REAL performance dispatcher: command
 * parsing, the per-deck scope queue, the command session and command status
 * are all production code. Only the audio transport boundary (`engine`'s
 * load/unload/play/pause, which need an AudioContext, a decoder and a worklet)
 * is replaced, by the test, on the engine instance the dispatcher calls.
 */
export { deckStates, engine } from '$lib/rb/audio-engine.svelte';
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	performanceCommandStatus
} from '$lib/rb/performance-ipc.svelte';
export { uiPrefs } from '$lib/rb/prefs.svelte';
export { toasts } from '$lib/stores.svelte';
export { noteGigRuntimeMounted } from '$lib/rb/library-mode-runtime';
export { TRACKIFY_DECK_ID, TRACKIFY_LOAD_SKIP_DEADLINE_MS } from '$lib/rb/trackify-autoplay';
export {
	e2eForceTrackifyLoad,
	installTrackifyAutoplay,
	readTrackifyAutoplayState,
	requestTrackifySkipNext
} from '$lib/rb/trackify-autoplay.svelte';
export { e2ePrimeTrackifyFeed } from '$lib/rb/trackify-feed.svelte';
export { executeSilenceDropoutPlan } from '$lib/rb/silence-dropout-act';
