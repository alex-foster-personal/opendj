/**
 * Shared-graph entry for the waveform-seek IPC tests (#4011 review).
 *
 * The waveform_seek branch reads the REAL deck state through getDeckState, so
 * the test must write the same `deckStates` object the dispatcher closes over;
 * two separately bundled entries would each get their own copy and the test
 * would prove nothing (see perf-ipc-entry.ts). No stubs beyond the engine's
 * own documented hot-cue driver seam.
 */
export { deckStates } from '$lib/rb/audio-engine.svelte';
export {
	installPerformanceBrowserIpc,
	installPerformanceHotCueDriverForTest,
	queryPerformanceState,
	uiPrefs
} from '$lib/rb/performance-ipc.svelte';
export {
	installPerformanceQuantizedLaunchDriverForTest,
	queryMasterMode,
	queryQuantizedLaunchArmed,
	queryWaveformSeekArmed,
	resetQuantizedLaunchArmedForTest
} from '$lib/rb/performance-ipc.svelte';
