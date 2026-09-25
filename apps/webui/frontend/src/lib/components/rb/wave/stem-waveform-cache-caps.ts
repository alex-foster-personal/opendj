/** Pure LRU eviction for the route-owned stem waveform envelope cache (issue #1036). */

import { stemWaveformByteCap, stemWaveformEntryCap } from '$lib/rb/perf-tier';

export interface StemWaveformLruEntry {
	status: string;
	touched?: number;
	envelope?: readonly number[];
}

export const STEM_WAVEFORM_ESTIMATE_BYTES = 4 * 1024;

let _touchSeq = 0;
let _boundCache: Record<string, StemWaveformLruEntry> | null = null;

export function bindStemWaveformCapCache(cache: Record<string, StemWaveformLruEntry>): void {
	_boundCache = cache;
}

export function nextStemWaveformTouch(): number {
	return ++_touchSeq;
}

export interface EvictStemWaveformLruOptions<T extends StemWaveformLruEntry> {
	entryCap: number;
	byteCap: number;
	estimateBytes: (entry: T) => number;
	isReady: (entry: T) => boolean;
}

export function evictStemWaveformLru<T extends StemWaveformLruEntry>(
	cache: Record<string, T>,
	opts: EvictStemWaveformLruOptions<T>
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

export function applyStemWaveformCapsToCache<T extends StemWaveformLruEntry>(
	cache: Record<string, T>,
	isReady: (entry: T) => boolean
): void {
	evictStemWaveformLru(cache, {
		entryCap: stemWaveformEntryCap(),
		byteCap: stemWaveformByteCap(),
		estimateBytes: (entry) =>
			entry.envelope !== undefined ? entry.envelope.length * 8 : STEM_WAVEFORM_ESTIMATE_BYTES,
		isReady
	});
}

export function retouchStemWaveformReadyEntry<T extends StemWaveformLruEntry>(
	cache: Record<string, T>,
	key: string
): void {
	const entry = cache[key];
	if (entry !== undefined && entry.status === 'ready') {
		cache[key] = { ...entry, touched: nextStemWaveformTouch() };
	}
}

export function applyStemWaveformCaps(): void {
	if (_boundCache === null) {
		throw new Error('stem waveform cache is not bound');
	}
	applyStemWaveformCapsToCache(_boundCache, (entry) => entry.status === 'ready');
}
