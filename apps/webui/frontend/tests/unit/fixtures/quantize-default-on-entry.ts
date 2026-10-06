/**
 * Shared-graph entry for DECKUX-39 (Q1-DEFAULT-ON): the IPC, the Web Audio
 * engine and the agent-order executor must come from ONE bundle, or a load
 * through the IPC would write a second copy of the deck state the test reads.
 */
export * as audio from '$lib/rb/audio-engine.svelte';
export * as ipc from '$lib/rb/performance-ipc.svelte';
export * as stores from '$lib/stores.svelte';
export * as registry from '$lib/rb/audio-context-registry';
export { executeAgentOrder } from '$lib/rb/agent-orders';
