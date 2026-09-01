/**
 * Reactive per-track ANLZ cache for the wavestack (build unit: wavestack).
 *
 * One /anlz fetch per stable_id per session; rows $derive off the entry so
 * they repaint when the payload lands. Backend error codes are kept as REAL
 * row states (ANALYSIS_NOT_FOUND renders the explicit 'no analysis' row -
 * SCREENSHOT-SPEC 7); unexpected failures are recorded AND re-thrown so they
 * surface loudly as unhandled rejections. No silent fallbacks, no invented
 * waveforms.
 *
 * .svelte.ts extension is REQUIRED for the $state rune (RECON-FRONTEND 10.1).
 */
import { fetchAnlz, RbApiError } from '$lib/rb/api-rb';
import { recordAnlzPrefetchSampled } from '$lib/rb/library-perf';
import type { AnlzData } from '$lib/rb/anlz-types';

export type AnlzEntry =
	| { status: 'loading' }
	| { status: 'ready'; data: AnlzData }
	| { status: 'error'; code: string };

const _cache = $state<Record<string, AnlzEntry>>({});

/** Kick off the /anlz fetch for a track unless already cached/in-flight.
 * MUTATES rune state - call from $effect, never from $derived.
 *
 * Timed on the MISS path only (PERF-R5 Q9): a cache hit returns above without
 * doing work, so including it would drag the sampled figure toward zero and
 * hide the cold fetch this warm-up exists to pay for. Sampled 1 in 5 because
 * arrow-key row selection fires this in bursts. */
export function ensureAnlz(stable_id: string): void {
	if (_cache[stable_id] !== undefined) return;
	_cache[stable_id] = { status: 'loading' };
	const startedAt = performance.now();
	void fetchAnlz(stable_id).then(
		(data: AnlzData) => {
			_cache[stable_id] = { status: 'ready', data };
			recordAnlzPrefetchSampled(performance.now() - startedAt, 'ready');
		},
		(err: unknown) => {
			recordAnlzPrefetchSampled(performance.now() - startedAt, 'error');
			if (err instanceof RbApiError) {
				// Explicit backend state (e.g. ANALYSIS_NOT_FOUND, 0.1% of tracks).
				_cache[stable_id] = { status: 'error', code: err.code };
				return;
			}
			_cache[stable_id] = { status: 'error', code: 'FETCH_FAILED' };
			throw err; // loud: network/shape failures must not vanish
		}
	);
}

/** Pure read; undefined = never requested for this stable_id. */
export function getAnlzEntry(stable_id: string): AnlzEntry | undefined {
	return _cache[stable_id];
}

/** Count of ready ANLZ entries for memory tracking. */
export function anlzCacheEntryCount(): number {
	let count = 0;
	for (const entry of Object.values(_cache)) {
		if (entry.status === 'ready') count++;
	}
	return count;
}

/** Estimated bytes of ready ANLZ JSON for hover breakdown only.
 * ~1.2 MB/track at points=38400. Cache grows on select/deck ensureAnlz,
 * not on table scroll. */
export function anlzCacheEstimatedBytes(): number {
	return anlzCacheEntryCount() * 1.2 * 1024 * 1024;
}
