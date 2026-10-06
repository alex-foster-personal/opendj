/**
 * Shared-graph entry for the session-restore lifecycle tests: the session
 * module and the performance IPC must be ONE bundle, so the command session
 * the test installs is the same one the restore reads. The namespace export
 * keeps a missing IPC symbol an `undefined` rather than a bundle error.
 */
export * as session from '$lib/rb/performance-session.svelte';
export * as snapshot from '$lib/rb/performance-session-snapshot';
export * as ipc from '$lib/rb/performance-ipc.svelte';
export * as stores from '$lib/stores.svelte';
