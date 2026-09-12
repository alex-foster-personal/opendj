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

export function applyAnlzCapsToCache<T extends AnlzLruEntry>(
	cache: Record<string, T>,
	isReady: (entry: T) => boolean
): void {
	evictAnlzLru(cache, {
		entryCap: anlzEntryCap(),
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
