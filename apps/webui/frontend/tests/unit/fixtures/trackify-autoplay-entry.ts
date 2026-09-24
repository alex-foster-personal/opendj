/**
 * Shared-graph entry for Trackify autoplay controller tests.
 */
export { deckStates } from '$lib/rb/audio-engine.svelte';
export { uiPrefs } from '$lib/rb/prefs.svelte';
export { toasts } from '$lib/stores.svelte';
export { TRACKIFY_DECK_ID, TRACKIFY_LOAD_SKIP_DEADLINE_MS } from '$lib/rb/trackify-autoplay';
export {
	e2eForceTrackifyLoad,
	installTrackifyAutoplay,
	readTrackifyAutoplayState,
	requestTrackifySkipNext
} from '$lib/rb/trackify-autoplay.svelte';
export { e2ePrimeTrackifyFeed } from '$lib/rb/trackify-feed.svelte';
export {
	getDispatchPerformanceCommandCalls,
	resetDispatchPerformanceCommandStub,
	setDispatchPerformanceCommandStub
} from './trackify-autoplay-stub-ipc';
