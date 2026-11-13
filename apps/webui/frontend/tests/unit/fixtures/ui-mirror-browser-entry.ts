/**
 * Shared-graph entry for UI mirror browser projection tests.
 *
 * `loadTypeScriptModule` bundles each entry independently, so loading IPC and
 * mirror as separate entries would give two unrelated copies of performance
 * state; adapter registration on one copy would not affect `buildUiMirror`.
 */
export { buildUiMirror } from '$lib/rb/ui-mirror';
export {
	registerPerformanceBrowserAdapter,
	installPerformanceBrowserIpc,
	queryPerformanceState
} from '$lib/rb/performance-ipc.svelte';
