// One bundle holding perf-tier-client AND the perf-tier state it writes, so a
// test can read the resolved tier from the same module instance the client set.
// anlz-cache.svelte binds the anlz cap cache at module load, exactly as the app
// does before perf-tier-client ever applies caps.
import '$lib/components/rb/wave/anlz-cache.svelte';

export { fetchPerfTier, PERF_TIER_PATH } from '$lib/rb/perf-tier-client';
export { resolvedTier, resolvedTierSource, perfTierFaultReason } from '$lib/rb/perf-tier';
