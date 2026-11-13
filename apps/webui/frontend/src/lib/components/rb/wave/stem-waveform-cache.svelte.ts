/**
 * Route-owned bounded cache for stem mini-waveform envelopes (issue #1036).
 *
 * Fetches server-produced mono peaks only - never stem PCM or decodeAudioData.
 */
import { untrack } from 'svelte';

import { fetchRbJson, RbApiError } from '$lib/rb/api-rb-json';
import { registerCapsConsumer } from '$lib/rb/cache-caps-registry';
import { refuseStickRead } from '$lib/rb/track-source';
import {
	applyStemWaveformCapsToCache,
	nextStemWaveformTouch,
	retouchStemWaveformReadyEntry
} from './stem-waveform-cache-caps';

interface StemWaveformEnvelope {
	schema: number;
	stable_id: string;
	part: string;
	layout: string;
	points: number;
	envelope: number[];
}

/** GET /tracks/{stable_id}/stems/{part}/waveform - server mono peak envelope.
 * Lives here, not in api-rb.ts, so it ships with /performance rather than in
 * the library page's first-paint bundle. */
async function fetchStemWaveform(stable_id: string, part: string): Promise<StemWaveformEnvelope> {
	// Spec 4b: stick tracks have no stems, so no stem waveform either.
	refuseStickRead(stable_id, `stem ${part} waveform`);
	const data = await fetchRbJson<StemWaveformEnvelope>(
		`/api/v1/tracks/${encodeURIComponent(stable_id)}/stems/${encodeURIComponent(part)}/waveform`
	);
	if (!Array.isArray(data.envelope) || data.envelope.length === 0) {
		throw new Error(`stem waveform ${stable_id}/${part}: empty envelope`);
	}
	return data;
}

export type StemWaveformEntry =
	| { status: 'loading' }
	| { status: 'ready'; envelope: readonly number[]; touched: number }
	| { status: 'error'; code: string };

// $state.raw (issue #10237): entries are replaced whole, never edited in
// place, so the store holds the published objects as-is (no deep proxy).
let _cache = $state.raw<Record<string, StemWaveformEntry>>({});

function _put(key: string, entry: StemWaveformEntry): void {
	_cache = { ..._cache, [key]: entry };
}

/** Evict on a copy, then publish the copy as the new whole value. */
function _applyCaps(): void {
	const next = { ..._cache };
	applyStemWaveformCapsToCache(next, (entry) => entry.status === 'ready');
	_cache = next;
}
registerCapsConsumer('stem-waveform', _applyCaps);

function _cacheKey(stable_id: string, part: string): string {
	return `${stable_id}:${part}`;
}

function _publishReady(key: string, envelope: readonly number[]): void {
	_put(key, { status: 'ready', envelope, touched: nextStemWaveformTouch() });
	_applyCaps();
}

/** Untracked: callers run this inside `$effect`, and the LRU retouch below
 * writes the entry it just read, so a tracked read re-runs the caller forever
 * (effect_update_depth_exceeded freezes the whole page). */
export function ensureStemWaveform(stable_id: string, part: string): void {
	untrack(() => _ensureStemWaveform(stable_id, part));
}

function _ensureStemWaveform(stable_id: string, part: string): void {
	const key = _cacheKey(stable_id, part);
	const existing = _cache[key];
	if (existing !== undefined) {
		if (existing.status === 'ready') {
			const next = { ..._cache };
			retouchStemWaveformReadyEntry(next, key);
			_cache = next;
		}
		return;
	}
	_put(key, { status: 'loading' });
	void fetchStemWaveform(stable_id, part).then(
		(data) => {
			_publishReady(key, data.envelope);
		},
		(err: unknown) => {
			if (err instanceof RbApiError) {
				_put(key, { status: 'error', code: err.code });
				return;
			}
			_put(key, { status: 'error', code: 'FETCH_FAILED' });
			throw err;
		}
	);
}

export function getStemWaveformEntry(
	stable_id: string,
	part: string
): StemWaveformEntry | undefined {
	return _cache[_cacheKey(stable_id, part)];
}
