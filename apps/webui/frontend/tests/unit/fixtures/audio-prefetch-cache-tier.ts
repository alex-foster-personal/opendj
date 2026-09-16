import { setResolvedTier } from '$lib/rb/perf-tier';
import * as cache from '$lib/rb/audio-prefetch-cache.svelte';

import type { PerfTierName } from '$lib/rb/perf-tier';

export function applyTier(tier: PerfTierName) {
	setResolvedTier(tier, 'override');
}

export * from '$lib/rb/audio-prefetch-cache.svelte';
