/** The jobs drawer command graph, exposed for behavioural tests.
 *
 * `jobsStore` rides along because the test reads back the SAME store
 * instance the dispatcher writes: two separate entries would each bundle
 * their own copy of the store and prove nothing. */
export { jobsDrawerToggleCommand, jobsStore } from '$lib/rb/jobs-store.svelte';
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	performanceCommandQueueScopes,
	queryPerformanceState,
	runPerformanceCommandFromUi
} from '$lib/rb/performance-ipc.svelte';
