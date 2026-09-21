/**
 * Shared-graph entry point for the beatgrid-lazy upgrade test.
 *
 * Same reason as beatgrid-upgrade-analysis-source-entry.ts: loadTypeScriptModule
 * bundles each entry independently, and beatgrid-lazy.ts's dynamic import of
 * beatgrid-upgrade would otherwise land a second unrelated analysisSourceState
 * that the test cannot set. The production lazy wrapper remains covered by the
 * structural source read in deck-beatgrid-fallback-upgrade.test.mjs.
 */
export { upgradeDeckBeatgrid } from '$lib/player/beatgrid-upgrade';
export { analysisSourceState } from '$lib/rb/analysis-source-state.svelte';
