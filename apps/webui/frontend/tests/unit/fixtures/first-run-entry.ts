/**
 * Shared-graph entry point for the first-run overlay tests.
 *
 * Same reason setup-wizard-entry.ts exists: loadTypeScriptModule bundles each
 * entry independently, so loading capabilities.svelte.ts separately from
 * first-run.ts would give the test one capability store and the gate a
 * different one. The gate reads `setupRefusal()`, which reads that store, so
 * the two have to be the same object or the refusal test proves nothing.
 *
 * No stubs: every module is the real thing and the only seam is
 * globalThis.fetch, which the probe and the status call both go through.
 */
export * from '$lib/api/capabilities.svelte';
export * from '$lib/setup/first-run';
export * from '$lib/setup/setup-api';
