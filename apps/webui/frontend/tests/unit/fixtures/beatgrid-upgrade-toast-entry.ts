/**
 * Shared-graph entry point for the beatgrid-upgrade error-toast tests.
 *
 * Same reason as `deck-observer-entry.ts`: `loadTypeScriptModule` bundles each
 * entry independently, so loading `beatgrid-upgrade.ts` and `stores.svelte`
 * as two separate entries would give two unrelated copies of the `toasts`
 * array - pushToast() inside the bundled upgrade module would write to a copy
 * the test can never read. Re-exporting both from one entry means the toasts
 * array the test asserts on IS the one `pushToast` (called from inside
 * `upgradeDeckBeatgrid`) mutates.
 */
export { upgradeDeckBeatgrid } from '$lib/player/beatgrid-upgrade';
export { toasts } from '$lib/stores.svelte';
