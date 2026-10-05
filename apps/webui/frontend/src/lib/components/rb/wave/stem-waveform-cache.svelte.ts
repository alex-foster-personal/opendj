/**
 * Route-owned bounded cache for stem mini-waveform envelopes (issue #1036).
 *
 * Fetches server-produced mono peaks only - never stem PCM or decodeAudioData.
 */
import { fetchRbJson, RbApiError } from '$lib/rb/api-rb-json';
import { registerCapsConsumer } from '$lib/rb/cache-caps-registry';
import { refuseStickRead } from '$lib/rb/track-source';
import {
	applyStemWaveformCaps,
	bindStemWaveformCapCache,
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
