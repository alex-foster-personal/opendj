/**
 * Shared-graph entry point for the beatgrid-lazy PARITY-02 gating test.
 *
 * Same reason as beatgrid-upgrade-analysis-source-entry.ts: loadTypeScriptModule
 * bundles each entry independently (including across a dynamic `import()`
 * inlined by esbuild), so loading beatgrid-lazy.ts alone would give it its own
 * unrelated copy of analysisSourceState that this test could never set.
 */
export { upgradeDeckBeatgrid } from '$lib/player/beatgrid-lazy';
export { analysisSourceState } from '$lib/rb/analysis-source.svelte';
