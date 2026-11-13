/** #12602 entry: the UI boundary, the agent-order executor, the typed dispatcher
 * (with the IPC install that opens its command session) and the control-event recorder in one bundle, so a test sees the source the
 * page capture point records for each caller through one module instance. */
export { executeAgentOrder } from '$lib/rb/agent-orders';
export {
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	runPerformanceCommandFromUi
} from '$lib/rb/performance-ipc.svelte';
export { PAGE_COMMAND_SOURCES } from '$lib/rb/performance-ipc-source';
export {
	bindControlEventSession,
	installControlEventRecorderForTest,
	resetControlEventRecorderForTest
} from '$lib/sets/control-event-recorder';
