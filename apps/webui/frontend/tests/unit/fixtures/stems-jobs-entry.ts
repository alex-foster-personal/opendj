/**
 * Shared-graph entry for the stems-jobs tests.
 *
 * Same reason as daemon-capability-entry.ts: loadTypeScriptModule bundles
 * each entry independently, so importing the capability probe, the jobs store
 * and the stems module separately would hand the test three private copies of
 * state that the real app shares. One entry, one bundle, one `jobsStore`.
 *
 * No stubs. The only seam these tests use is globalThis.fetch, which is what
 * the probe and every call here goes through.
 */
export * from '$lib/api/capabilities.svelte';
export * from '$lib/rb/jobs-store.svelte';
export * from '$lib/rb/stems-jobs.svelte';
