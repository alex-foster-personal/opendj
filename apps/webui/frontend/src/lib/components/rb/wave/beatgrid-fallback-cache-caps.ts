/** Pure LRU eviction for the beatgrid fallback cache (issue #10237), shaped like anlz-cache-caps.ts. */

import { anlzEntryCap } from '$lib/rb/perf-tier';

export interface BeatgridFallbackLruEntry {
	status: string;
	touched?: number;
}

/** Ready entries kept by default: one fallback grid is small, so the floor sits above the ANLZ tier cap. */
export const BEATGRID_FALLBACK_DEFAULT_ENTRY_CAP = 64;

let _touchSeq = 0;

/** Next monotonic LRU touch sequence number. */
export function nextBeatgridFallbackTouch(): number {
	return ++_touchSeq;
}

/** Entry cap from the perf tier: the ANLZ tier cap, never below the 64-entry default. */
export function beatgridFallbackEntryCap(): number {
	return Math.max(BEATGRID_FALLBACK_DEFAULT_ENTRY_CAP, anlzEntryCap());
}

/** Return a new record with the oldest ready entries dropped until at most entryCap remain; loading and error entries are never evicted. */
export function evictBeatgridFallbackLru<T extends BeatgridFallbackLruEntry>(
	cache: Readonly<Record<string, T>>,
	entryCap: number
): Record<string, T> {
	const ready = Object.entries(cache)
		.filter(([, entry]) => entry.status === 'ready')
		.sort(([, a], [, b]) => (a.touched ?? 0) - (b.touched ?? 0));
	if (ready.length <= entryCap) return cache as Record<string, T>;
	const next = { ...cache };
	for (const [id] of ready.slice(0, ready.length - entryCap)) delete next[id];
	return next;
}

/** Return a new record with the ready entry for stable_id re-touched, or the same record when it is not ready. */
export function retouchBeatgridFallbackEntry<T extends BeatgridFallbackLruEntry>(
	cache: Readonly<Record<string, T>>,
	stable_id: string
): Record<string, T> {
	const entry = cache[stable_id];
	if (entry === undefined || entry.status !== 'ready') return cache as Record<string, T>;
	return { ...cache, [stable_id]: { ...entry, touched: nextBeatgridFallbackTouch() } };
}
