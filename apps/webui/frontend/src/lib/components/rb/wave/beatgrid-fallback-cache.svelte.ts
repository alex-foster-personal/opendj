/**
 * Reactive per-track beatgrid-fallback cache (build unit: wavestack).
 * Mirrors anlz-cache.svelte.ts. Callers MUST gate on
 * shouldUseBeatgridFallback(anlzErrorCode) first (see beatgrid-fallback.ts) -
 * this cache exists to fill the gap when a rekordbox ANLZ grid is
 * confirmed absent, never to race or override a real ANLZ payload.
 *
 * .svelte.ts extension is REQUIRED for the $state rune (RECON-FRONTEND 10.1).
 */
import { RbApiError } from '$lib/rb/api-rb-error';
import { fetchBeatgridFallback } from '$lib/rb/beatgrid-fallback-api';
import type { BeatgridFallbackOut } from '$lib/rb/beatgrid-fallback-api';

export type BeatgridFallbackEntry =
	| { status: 'loading' }
	| { status: 'ready'; data: BeatgridFallbackOut }
	| { status: 'error'; code: string };

const _cache = $state<Record<string, BeatgridFallbackEntry>>({});

/** Kick off the /beatgrid-fallback fetch for a track unless already
 * cached/in-flight. MUTATES rune state - call from $effect, never from
 * $derived. */
export function ensureBeatgridFallback(stable_id: string): void {
	if (_cache[stable_id] !== undefined) return;
	_cache[stable_id] = { status: 'loading' };
	void fetchBeatgridFallback(stable_id).then(
		(data: BeatgridFallbackOut) => {
			_cache[stable_id] = { status: 'ready', data };
		},
		(err: unknown) => {
			if (err instanceof RbApiError) {
				// Explicit backend state (e.g. BEATGRID_FALLBACK_NOT_FOUND - a
				// grid is never invented). Never rendered as if it were 'ready'.
				_cache[stable_id] = { status: 'error', code: err.code };
				return;
			}
			_cache[stable_id] = { status: 'error', code: 'FETCH_FAILED' };
			throw err; // loud: network/shape failures must not vanish
		}
	);
}

/** Pure read; undefined = never requested for this stable_id. */
export function getBeatgridFallbackEntry(stable_id: string): BeatgridFallbackEntry | undefined {
	return _cache[stable_id];
}
