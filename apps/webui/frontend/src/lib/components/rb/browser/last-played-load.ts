/**
 * One cached `GET /tracks/{id}/plays` per library track. Stick ids have no
 * library plays route. Visible rows share a small queue so opening the
 * table does not fire every request at once.
 */

import { api, unwrap } from '$lib/api/client';
import { isUsbTrackId } from '$lib/rb/track-source';
import { playFactsFromUnknown, type PlayFacts } from './last-played';

const MAX_IN_FLIGHT = 4;

const cache = new Map<string, PlayFacts | null>();
const inflight = new Map<string, Promise<PlayFacts | null>>();
let active = 0;
const waiters: Array<() => void> = [];

function gate<T>(job: () => Promise<T>): Promise<T> {
	return new Promise((resolve, reject) => {
		const run = (): void => {
			active += 1;
			job().then(resolve, reject).finally(() => {
				active -= 1;
				const next = waiters.shift();
				if (next !== undefined) next();
			});
		};
		if (active < MAX_IN_FLIGHT) run();
		else waiters.push(run);
	});
}

/** `undefined` when this id has not been loaded yet. */
export function peekTrackPlays(stableId: string): PlayFacts | null | undefined {
	if (!cache.has(stableId)) return undefined;
	return cache.get(stableId) ?? null;
}

export function loadTrackPlays(stableId: string): Promise<PlayFacts | null> {
	if (isUsbTrackId(stableId)) return Promise.resolve(null);
	if (cache.has(stableId)) return Promise.resolve(cache.get(stableId) ?? null);
	const existing = inflight.get(stableId);
	if (existing !== undefined) return existing;
	const tracked = gate(async () => {
		try {
			const body = await unwrap(
				api.GET('/api/v1/tracks/{stable_id}/plays', {
					params: { path: { stable_id: stableId } }
				})
			);
			const facts = playFactsFromUnknown(body);
			cache.set(stableId, facts);
			return facts;
		} catch {
			// Not cached: a failed fetch is retried the next time the row mounts.
			return null;
		}
	});
	inflight.set(stableId, tracked);
	void tracked.finally(() => {
		inflight.delete(stableId);
	});
	return tracked;
}
