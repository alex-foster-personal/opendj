// One bundle for the beat FX agent parity test, so the dispatcher and the flag store share
// module instances.
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	queryPerformanceState
} from '$lib/rb/performance-ipc.svelte';
export { buildFlags } from '$lib/api/store-build.svelte';
