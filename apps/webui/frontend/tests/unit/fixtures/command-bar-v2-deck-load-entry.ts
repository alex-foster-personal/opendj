/**
 * Shared-graph entry for CMDK-04's pick cancellation: the bar's pick loader,
 * the IPC dispatcher and the Web Audio engine must come from ONE bundle, so a
 * load through the dispatcher moves the same deck state the mark reads.
 */
export * as audio from '$lib/rb/audio-engine.svelte';
export * as ipc from '$lib/rb/performance-ipc.svelte';
export * as stores from '$lib/stores.svelte';
export * as registry from '$lib/rb/audio-context-registry';
export * as cmdk from '$lib/rb/cmdk-load';
