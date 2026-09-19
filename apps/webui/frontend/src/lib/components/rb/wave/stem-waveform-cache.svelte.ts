/**
 * Route-owned bounded cache for stem mini-waveform envelopes (issue #1036).
 *
 * Fetches server-produced mono peaks only - never stem PCM or decodeAudioData.
 */
import { fetchStemWaveform, RbApiError } from '$lib/rb/api-rb';
import { registerCapsConsumer } from '$lib/rb/cache-caps-registry';
import {
	applyStemWaveformCaps,
	bindStemWaveformCapCache,
	nextStemWaveformTouch,
	resetStemWaveformCapStateForTests,
	retouchStemWaveformReadyEntry
} from './stem-waveform-cache-caps';

export type StemWaveformEntry =
	| { status: 'loading' }
	| { status: 'ready'; envelope: readonly number[]; touched: number }
	| { status: 'error'; code: string };

const _cache = $state<Record<string, StemWaveformEntry>>({});
bindStemWaveformCapCache(_cache);
registerCapsConsumer('stem-waveform', applyStemWaveformCaps);

function _cacheKey(stable_id: string, part: string): string {
	return `${stable_id}:${part}`;
}

function _publishReady(key: string, envelope: readonly number[]): void {
	_cache[key] = { status: 'ready', envelope, touched: nextStemWaveformTouch() };
	applyStemWaveformCaps();
}

export function ensureStemWaveform(stable_id: string, part: string): void {
	const key = _cacheKey(stable_id, part);
	const existing = _cache[key];
	if (existing !== undefined) {
		if (existing.status === 'ready') retouchStemWaveformReadyEntry(_cache, key);
		return;
	}
	_cache[key] = { status: 'loading' };
	void fetchStemWaveform(stable_id, part).then(
		(data) => {
			_publishReady(key, data.envelope);
		},
		(err: unknown) => {
			if (err instanceof RbApiError) {
				_cache[key] = { status: 'error', code: err.code };
				return;
			}
			_cache[key] = { status: 'error', code: 'FETCH_FAILED' };
			throw err;
		}
	);
}

export function getStemWaveformEntry(stable_id: string, part: string): StemWaveformEntry | undefined {
	return _cache[_cacheKey(stable_id, part)];
}

/** Test hook: reset cache and re-bind caps consumer state. */
export function resetStemWaveformCacheForTests(): void {
	for (const key of Object.keys(_cache)) delete _cache[key];
	resetStemWaveformCapStateForTests();
}

/** Count of ready entries for bounded-cache tests. */
export function stemWaveformCacheReadyCount(): number {
	let count = 0;
	for (const entry of Object.values(_cache)) {
		if (entry.status === 'ready') count++;
	}
	return count;
}

/** Force cap eviction using current tier limits. */
export function applyStemWaveformCacheCapsNow(): void {
	applyStemWaveformCaps();
}
