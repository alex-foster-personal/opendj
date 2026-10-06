/**
 * Shared-graph entry for PLAY-18's deck-stop log: the engine, the IPC, the
 * agent-order executor and the stop log must be ONE bundle, or a stop the
 * engine records would land in a second copy of the register the test reads.
 */
export * as audio from '$lib/rb/audio-engine.svelte';
export * as ipc from '$lib/rb/performance-ipc.svelte';
export * as stopLog from '$lib/rb/deck-stop-log';
export * as stores from '$lib/stores.svelte';
export * as registry from '$lib/rb/audio-context-registry';
export * as perf from '$lib/rb/perf-event-log';
export { executeAgentOrder } from '$lib/rb/agent-orders';
