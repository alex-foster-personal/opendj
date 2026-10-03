/** Pure LRU eviction for the frontend ANLZ cache (PERFMODE-01). */

import { anlzByteCap, anlzEntryCap } from '$lib/rb/perf-tier';

export interface AnlzLruEntry {
	status: string;
	touched?: number;
}

export const ANLZ_ESTIMATE_BYTES = 1.2 * 1024 * 1024;

let _touchSeq = 0;
let _boundCache: Record<string, AnlzLruEntry> | null = null;

export function bindAnlzCapCache(cache: Record<string, AnlzLruEntry>): void {
	_boundCache = cache;
}

export function nextAnlzTouch(): number {
	return ++_touchSeq;
}

export interface EvictAnlzLruOptions<T extends AnlzLruEntry> {
	entryCap: number;
	byteCap: number;
	estimateBytes: (entry: T) => number;
	isReady: (entry: T) => boolean;
}

export function evictAnlzLru<T extends AnlzLruEntry>(
	cache: Record<string, T>,
	opts: EvictAnlzLruOptions<T>
): void {
	const readyIds: string[] = [];
	let readyBytes = 0;
	for (const [id, entry] of Object.entries(cache)) {
		if (!opts.isReady(entry)) continue;
		readyIds.push(id);
		readyBytes += opts.estimateBytes(entry);
	}
	while (readyIds.length > opts.entryCap || readyBytes > opts.byteCap) {
		let victim: string | null = null;
		let oldest = Infinity;
		for (const id of readyIds) {
			const entry = cache[id];
			const touched = entry.touched ?? 0;
			if (touched < oldest) {
				oldest = touched;
				victim = id;
			}
		}
		if (victim === null) break;
		readyBytes -= opts.estimateBytes(cache[victim]);
		delete cache[victim];
		readyIds.splice(readyIds.indexOf(victim), 1);
	}
}

/**
 * Entry caps held by a surface that needs fewer ANLZ entries than its tier
 * allows. Trackify (PERFMODE-15) paints no waveform rows, so it holds the
 * cache to its one deck's track: the tier caps (8/32/64) are sized for
 * browsing a library, and under Trackify they only accumulated every played
 * track's waveform and beatgrid, about 1.25 MB of JS heap a track (measured
 * Thu 1 Oct 2026 on silver: 8.7 -> 46.6 MB over 29 loads, flat after the cap).
 */
const _heldCaps: number[] = [];

/** The tier's entry cap, lowered to the smallest cap currently held. */
export function effectiveAnlzEntryCap(): number {
	return Math.min(anlzEntryCap(), ..._heldCaps);
}

/**
 * Hold the ANLZ cache at or below `cap` ready entries until the returned
 * release runs. Evicts at once when the cache is bound; a release restores
 * the next smallest hold (or the tier cap) for later inserts.
 */
export function holdAnlzEntryCap(cap: number): () => void {
	if (!Number.isInteger(cap) || cap < 1) throw new RangeError(`anlz cap hold ${cap}`);
	_heldCaps.push(cap);
	if (_boundCache !== null) applyAnlzCaps();
	let held = true;
	return () => {
		if (!held) throw new Error('anlz cap released twice');
		held = false;
		_heldCaps.splice(_heldCaps.indexOf(cap), 1);
	};
}

export function applyAnlzCapsToCache<T extends AnlzLruEntry>(
	cache: Record<string, T>,
	isReady: (entry: T) => boolean
): void {
	evictAnlzLru(cache, {
		entryCap: effectiveAnlzEntryCap(),
		byteCap: anlzByteCap(),
		estimateBytes: () => ANLZ_ESTIMATE_BYTES,
		isReady
	});
}

export function retouchAnlzReadyEntry<T extends AnlzLruEntry>(
	cache: Record<string, T>,
	stable_id: string
): void {
	const entry = cache[stable_id];
	if (entry !== undefined && entry.status === 'ready') {
		cache[stable_id] = { ...entry, touched: nextAnlzTouch() };
	}
}

export function applyAnlzCaps(): void {
	if (_boundCache === null) {
		throw new Error('anlz cache is not bound');
	}
	applyAnlzCapsToCache(_boundCache, (entry) => entry.status === 'ready');
}
