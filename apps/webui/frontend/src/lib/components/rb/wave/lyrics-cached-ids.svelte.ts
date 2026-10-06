/** Loads GET /tracks/lyrics-cached-ids once for wave-row lyrics gating (#1869). */
import { API_BASE } from '$lib/api';
import { bootScheduler } from '$lib/rb/boot-scheduler';

import { shouldFetchTrackLyrics } from './lyrics-cached-ids';

let cachedIds = $state<ReadonlySet<string> | null>(null);
let loadError = $state<Error | null>(null);
let loadStarted = false;

async function loadLyricsCachedIds(): Promise<void> {
	const response = await fetch(`${API_BASE}/api/v1/tracks/lyrics-cached-ids`);
	if (!response.ok) {
		throw new Error(`GET /tracks/lyrics-cached-ids failed with ${response.status}`);
	}
	const body = (await response.json()) as { stable_ids?: unknown };
	if (
		typeof body !== 'object' ||
		body === null ||
		!Array.isArray(body.stable_ids) ||
		body.stable_ids.some((value) => typeof value !== 'string')
	) {
		throw new Error('GET /tracks/lyrics-cached-ids returned an invalid stable_ids list');
	}
	cachedIds = new Set(body.stable_ids);
}

export function ensureLyricsCachedIdsLoaded(): void {
	if (loadStarted) return;
	loadStarted = true;
	// LIBM-172: a 1.7 s engine read at boot; it waits for the library index.
	bootScheduler.defer('lyrics-cached-ids:load', () => {
		void loadLyricsCachedIds().catch((error: unknown) => {
			loadError = error instanceof Error ? error : new Error(String(error));
		});
	});
}

export function getLyricsCachedIds(): ReadonlySet<string> | null {
	return cachedIds;
}

export function trackHasCachedLyrics(stableId: string): boolean {
	return shouldFetchTrackLyrics(stableId, cachedIds);
}

export function lyricsCachedIdsReady(): boolean {
	return cachedIds !== null;
}

export function lyricsCachedIdsLoadError(): Error | null {
	return loadError;
}
