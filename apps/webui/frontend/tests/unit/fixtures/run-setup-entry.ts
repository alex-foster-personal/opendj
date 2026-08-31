/**
 * Shared-graph entry point for the setup entry-point tests.
 *
 * Same reason first-run-entry.ts exists: loadTypeScriptModule bundles each
 * entry independently, so loading capabilities.svelte.ts separately from
 * run-setup.ts would give the test one capability store and runSetup a
 * different one. runSetup reads that store (through setupRefusal) AND awaits
 * its probe, so the two have to be the same object or the refusal tests prove
 * nothing.
 *
 * No stubs: every module is the real thing and the only seams are
 * globalThis.fetch and the injected navigate function.
 */
export * from '$lib/api/capabilities.svelte';
export * from '$lib/setup/run-setup';
export * from '$lib/setup/setup-api';
export * from '$lib/setup/wizard.svelte';
