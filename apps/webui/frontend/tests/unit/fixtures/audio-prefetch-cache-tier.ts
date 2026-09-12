import { setResolvedTier } from '$lib/rb/perf-tier';
import * as cache from '$lib/rb/audio-prefetch-cache.svelte';

export function applyTier(tier) {
	setResolvedTier(tier, 'override');
}

export * from '$lib/rb/audio-prefetch-cache.svelte';
