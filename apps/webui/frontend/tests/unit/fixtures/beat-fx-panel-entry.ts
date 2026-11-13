// One bundle for the beat FX panel test, so the panel logic, the dispatcher, the live unit and
// the flag store share module instances.
export * as panel from '$lib/rb/beat-fx-panel';
export * as explainers from '$lib/rb/planned-explainers';
export { parseBeatFxCommand } from '$lib/rb/beat-fx';
export { beatFxRackEnabled } from '$lib/rb/beat-fx-flag';
export { beatFxView } from '$lib/rb/beat-fx-live.svelte';
export {
	installPerformanceBrowserIpc,
	runPerformanceCommandFromUi
} from '$lib/rb/performance-ipc.svelte';
export { buildFlags } from '$lib/api/store-build.svelte';
