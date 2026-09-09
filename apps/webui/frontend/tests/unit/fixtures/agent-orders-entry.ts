/** Production agent-order executor plus the session it dispatches through,
 * exposed together so a behavioral test shares one bundled module instance
 * (see pairing-snapshot-entry.ts for the same need). */
export { executeAgentOrder } from '$lib/rb/agent-orders';
export { installPerformanceBrowserIpc, queryPerformanceState } from '$lib/rb/performance-ipc.svelte';
