/**
 * Client ANLZ vocals refresh when hydrated library data reports a newer vocal
 * status than the shared cache (issue #1031). Split out of anlz-cache.svelte.ts
 * to stay under the file-size ratchet; takes cache primitives as ports (same
 * pattern as analysis-source-refresh.ts) so this module never imports back.
 */
import type { AnlzData } from '$lib/rb/anlz-types';
import { vocalsOf, type AnlzWithVocals, type Vocals } from '$lib/rb/api-rb';

export interface AnlzVocalsRefreshPorts {
	getReadyAnlz: (stable_id: string) => AnlzData | null;
	refreshAnlzCacheEntry: (stable_id: string, data: AnlzData) => void | Promise<void>;
	fetchAnlzBypassingHttpCache: (stable_id: string) => Promise<AnlzData>;
	fetchAnlzUntilSourceConfirmed: (fetch: () => Promise<AnlzData>) => Promise<AnlzData>;
	anlzMatchesConfirmedSource: (data: AnlzData) => boolean;
	currentAnlzFetchGeneration: () => number;
	snapshotVocalsRefreshGuard: (stable_id: string) => void;
	notifyVocalsSink: (stable_id: string, data: AnlzWithVocals, previous: Vocals | null) => void;
}

const _inflight = new Map<string, Promise<void>>();

function _cachedVocalsStatus(ports: AnlzVocalsRefreshPorts, stable_id: string): Vocals['status'] | null {
	const cached = ports.getReadyAnlz(stable_id);
	if (cached === null) return null;
	return vocalsOf(cached).status;
}

/** Fire-and-forget: bypasses the browser HTTP cache only when `observedStatus`
 * disagrees with the ready shared-cache entry's validated vocals status. */
export function refreshAnlzVocalsIfNeeded(
	stable_id: string,
	observedStatus: Vocals['status'],
	ports: AnlzVocalsRefreshPorts
): void {
	const cachedStatus = _cachedVocalsStatus(ports, stable_id);
	if (cachedStatus === observedStatus) return;
	if (cachedStatus === null && observedStatus === 'not_analyzed') return;

	const existing = _inflight.get(stable_id);
	if (existing !== undefined) {
		void existing;
		return;
	}

	const work = (async () => {
		// The vocals decks may still carry: whatever the cache held when this
		// refresh began. The deck sink uses it to reconcile late holders.
		const atStart = ports.getReadyAnlz(stable_id);
		const previous = atStart === null ? null : vocalsOf(atStart);
		ports.snapshotVocalsRefreshGuard(stable_id);
		const generation = ports.currentAnlzFetchGeneration();
		const initialFresh = (await ports.fetchAnlzBypassingHttpCache(stable_id)) as AnlzWithVocals;
		const fresh = (
			generation === ports.currentAnlzFetchGeneration() &&
			ports.anlzMatchesConfirmedSource(initialFresh)
				? initialFresh
				: await ports.fetchAnlzUntilSourceConfirmed(() =>
						ports.fetchAnlzBypassingHttpCache(stable_id)
					)
		) as AnlzWithVocals;

		const base = ports.getReadyAnlz(stable_id);
		if (base !== null && vocalsOf(base).status === observedStatus) {
			// Another request already refreshed the cache while this one was in
			// flight; decks loaded before it may still hold the old vocals.
			ports.notifyVocalsSink(stable_id, base as AnlzWithVocals, previous);
			return;
		}

		const merged: AnlzWithVocals =
			base === null ? fresh : { ...base, vocals: fresh.vocals };
		await ports.refreshAnlzCacheEntry(stable_id, merged);
		ports.notifyVocalsSink(stable_id, merged, previous);
	})().finally(() => {
		_inflight.delete(stable_id);
	});

	_inflight.set(stable_id, work);
	void work;
}
