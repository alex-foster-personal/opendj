/**
 * Shared-graph entry point for the beatgrid-upgrade PARITY-02 gating tests.
 *
 * Same reason as beatgrid-upgrade-toast-entry.ts: loadTypeScriptModule
 * bundles each entry independently, so loading beatgrid-upgrade.ts and
 * analysis-source.svelte as two separate entries would give two unrelated
 * copies of analysisSourceState - a test writing 'own'/'rekordbox' to one
 * copy would never be seen by the upgradeDeckBeatgrid the OTHER copy calls.
 * Re-exporting both from one entry means the state the test sets IS the
 * state upgradeDeckBeatgrid reads.
 */
export { upgradeDeckBeatgrid } from '$lib/player/beatgrid-upgrade';
export { analysisSourceState } from '$lib/rb/analysis-source-state.svelte';
