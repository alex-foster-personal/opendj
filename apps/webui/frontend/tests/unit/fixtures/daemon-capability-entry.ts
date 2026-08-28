/**
 * Shared-graph entry point for the daemon-capability tests.
 *
 * loadTypeScriptModule bundles each entry independently, so loading
 * capabilities.svelte.ts and jobs-store.svelte.ts as two separate entries
 * would give the test one capability store and the store under test a
 * different one -- a test that probes through the first and asserts on the
 * second proves nothing.
 *
 * Re-exporting them from one entry puts them in one bundle, so `capabilities`
 * here is the exact object jobs-store and progress-api close over. No stubs:
 * every module is the real thing, and the only seam the tests use is
 * globalThis.fetch, which is what the probe and both consumers go through.
 */
export * from '$lib/api/capabilities.svelte';
export * from '$lib/rb/jobs-store.svelte';
export { fetchProgress, validateProgressResponse } from '../../../src/routes/progress-tree/progress-api';
