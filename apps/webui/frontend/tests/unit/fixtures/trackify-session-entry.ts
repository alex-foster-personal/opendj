/**
 * Shared-graph entry for the Trackify session teardown-ownership race test
 * (trackify-session.test.mjs, PERFMODE-15). Runs the REAL performance
 * dispatcher, exactly like trackify-autoplay-entry.ts: only the audio
 * transport boundary (`engine`'s load/unload/play/pause/dispose) is faked by
 * the test.
 */
export { deckStates, engine } from '$lib/rb/audio-engine.svelte';
export { installPerformanceBrowserIpc } from '$lib/rb/performance-ipc.svelte';
export { uiPrefs } from '$lib/rb/prefs.svelte';
export { toasts } from '$lib/stores.svelte';
export { TRACKIFY_ANLZ_ENTRY_CAP, TRACKIFY_DECK_ID } from '$lib/rb/trackify-autoplay';
export { effectiveAnlzEntryCap } from '$lib/components/rb/wave/anlz-cache-caps';
export { anlzEntryCap } from '$lib/rb/perf-tier';
export { e2ePrimeTrackifyFeed } from '$lib/rb/trackify-feed.svelte';
export {
	currentGigRuntimeGeneration,
	noteGigRuntimeMounted,
	resetLibraryModeRuntimeForTest
} from '$lib/rb/library-mode-runtime';
export { installTrackifySession } from '$lib/rb/trackify-session.svelte';
