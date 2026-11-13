/** #5586: the performance dispatcher, the R|M apply path, the prefs it writes
 * and the engine deck state, in one bundled module instance. */
export {
	installPerformanceBrowserIpc,
	parsePerformanceCommandForTest,
	runPerformanceCommandFromUi
} from '$lib/rb/performance-ipc.svelte';
export {
	LEVEL_CALIBRATION_REFUSAL,
	setLevelCalibrationSourcesForTest
} from '$lib/rb/level-calibration-command';
export { uiPrefs } from '$lib/rb/prefs.svelte';
export { deckStates } from '$lib/rb/audio-engine.svelte';
