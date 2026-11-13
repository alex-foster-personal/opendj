/**
 * Reactive per-track beatgrid-fallback cache (build unit: wavestack).
 * Mirrors anlz-cache.svelte.ts. Callers MUST gate on
 * shouldUseBeatgridFallback(anlzErrorCode) first (see beatgrid-fallback.ts) -
 * this cache exists to fill the gap when a rekordbox ANLZ grid is
 * confirmed absent, never to race or override a real ANLZ payload.
 *
 * Held as $state.raw (issue #10237): entries are replaced whole, never edited
 * in place, so a deep proxy only cost signals. Ready entries are LRU-capped by
 * beatgrid-fallback-cache-caps.ts; loading entries are never evicted.
 *
 * .svelte.ts extension is REQUIRED for the $state rune (RECON-FRONTEND 10.1).
 */
import { untrack } from 'svelte';

import { RbApiError } from '$lib/rb/api-rb-error';
import { fetchBeatgridFallback } from '$lib/rb/beatgrid-fallback-api';
import type { BeatgridFallbackOut } from '$lib/rb/beatgrid-fallback-api';
import {
	beatgridFallbackEntryCap,
	evictBeatgridFallbackLru,
	nextBeatgridFallbackTouch,
	retouchBeatgridFallbackEntry
} from './beatgrid-fallback-cache-caps';

export type BeatgridFallbackEntry =
	| { status: 'loading' }
	| { status: 'ready'; data: BeatgridFallbackOut; touched: number }
	| { status: 'error'; code: string };

let _cache = $state.raw<Record<string, BeatgridFallbackEntry>>({});

function _put(stable_id: string, entry: BeatgridFallbackEntry): void {
	_cache = { ..._cache, [stable_id]: entry };
}

/** Kick off the /beatgrid-fallback fetch for a track unless already
 * cached/in-flight. MUTATES rune state - call from $effect, never from
 * $derived. Untracked so the LRU retouch never re-runs the caller. */
export function ensureBeatgridFallback(stable_id: string): void {
	untrack(() => _ensureBeatgridFallback(stable_id));
}

function _ensureBeatgridFallback(stable_id: string): void {
	const existing = _cache[stable_id];
	if (existing !== undefined) {
		if (existing.status === 'ready') _cache = retouchBeatgridFallbackEntry(_cache, stable_id);
		return;
	}
	_put(stable_id, { status: 'loading' });
	void fetchBeatgridFallback(stable_id).then(
		(data: BeatgridFallbackOut) => {
			_put(stable_id, { status: 'ready', data, touched: nextBeatgridFallbackTouch() });
			_cache = evictBeatgridFallbackLru(_cache, beatgridFallbackEntryCap());
		},
		(err: unknown) => {
			if (err instanceof RbApiError) {
				// Explicit backend state (e.g. BEATGRID_FALLBACK_NOT_FOUND - a
				// grid is never invented). Never rendered as if it were 'ready'.
				_put(stable_id, { status: 'error', code: err.code });
				return;
			}
			_put(stable_id, { status: 'error', code: 'FETCH_FAILED' });
			throw err; // loud: network/shape failures must not vanish
		}
	);
}

/** Pure read; undefined = never requested for this stable_id. */
export function getBeatgridFallbackEntry(stable_id: string): BeatgridFallbackEntry | undefined {
	return _cache[stable_id];
}
