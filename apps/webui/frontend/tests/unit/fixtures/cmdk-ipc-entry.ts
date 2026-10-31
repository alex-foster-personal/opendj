/** The production dispatcher with the Cmd K adapter seam, for the CMDK-15 behavioral test. */
export {
	deckCommandDispatchCount,
	deckFaderWriteCount,
	deckLoadDispatchCount,
	dispatchPerformanceCommand,
	installPerformanceBrowserIpc,
	parsePerformanceCommandForTest,
	performanceCommandQueueScopes,
	queryPerformanceState,
	registerCmdkAdapter
} from '$lib/rb/performance-ipc.svelte';
