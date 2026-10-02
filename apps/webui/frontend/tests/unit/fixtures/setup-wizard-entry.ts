/**
 * Shared-graph entry point for the setup-wizard tests.
 *
 * Same reason daemon-capability-entry.ts exists: loadTypeScriptModule bundles
 * each entry independently, so loading capabilities.svelte.ts separately from
 * wizard.svelte.ts would give the test one capability store and the wizard a
 * different one. Every setup call is gated on `setupRefusal()`, which reads
 * that store, so the two have to be the same object or the gating tests prove
 * nothing.
 *
 * No stubs: every module is the real thing and the only seam is
 * globalThis.fetch, which the probe and every setup call go through.
 */
export * from '$lib/api/capabilities.svelte';
export * from '$lib/setup/setup-api';
export * from '$lib/setup/folder-rows';
export * from '$lib/setup/wizard.svelte';
// finish() re-reads preflight; the tests must see the SAME preflight store the
// wizard writes, so it comes through this one bundle too.
export {
	_resetPreflightForTests,
	checkPreflight,
	preflightGate
} from '$lib/preflight/preflight.svelte';
export { needsSetupForEmptyLibrary } from '$lib/preflight/fresh-install';
