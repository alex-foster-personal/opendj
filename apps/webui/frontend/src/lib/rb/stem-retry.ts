/** STEM-46/47: "get this deck's stems now", and the hydrate request it sends.
 *
 * Its own module, imported on first use through stem-hydrate-wait.ts, so a
 * decision that only ever runs from a click or a command stays out of the
 * library page's first paint (scripts/check-bundle-size.sh). */
import { RB_API_BASE } from '$lib/rb/api-rb';
import { releaseEagerStemDecodeNow } from '$lib/rb/stem-decode-shed';
import type { StemDeckState } from '$lib/rb/stem-types';
import { refuseStickRead } from '$lib/rb/track-source';

/** STEM-46: ask the engine to fetch (or re-fetch) this track's cloud bundle,
 * clearing a recorded failure first. The manifest GET alone cannot do that: a
 * failed fetch answers 502 there until something asks again. Rejects on any
 * non-2xx, naming the status; never silent. */
export async function requestStemHydration(
	stableId: string,
	fetcher: typeof fetch = fetch
): Promise<void> {
	// Spec 4b: stick tracks have no stem bundle and no stems route.
	refuseStickRead(stableId, 'stem hydrate');
	const path = `/api/v1/tracks/${encodeURIComponent(stableId)}/stems/hydrate`;
	const response = await fetcher(`${RB_API_BASE}${path}`, { method: 'POST' });
	if (!response.ok) throw new Error(`stem hydrate request failed: HTTP ${response.status} ${path}`);
}

/** What the deck engine hands a stem retry (STEM-46/47). */
export interface StemRetryPort {
	/** Land a bundle held behind transport commands; null when none is held. */
	landHeld: (() => Promise<unknown>) | null;
	/** Re-run the whole stem load from the probe; null when the deck has no
	 * decoded mix to align stems to. */
	reload: (() => void) | null;
	releaseDecode?: () => number;
	requestHydration?: (stableId: string) => Promise<void>;
	/** False once the deck has loaded another track since the retry began. */
	current: () => boolean;
}

/**
 * "Get this deck's stems now." One decision for the deck button, the
 * `stem_load` command and an agent:
 *
 *   ready    -> nothing to do
 *   loading  -> land a held bundle, else start a decode held by pressure; a
 *               load that is already running by itself rejects, naming its phase
 *   error    -> clear the engine's recorded fetch failure, then load again
 *   unavailable -> load again (the answer may have changed)
 */
export function retryDeckStems(
	deck: number,
	stableId: string | null,
	stems: StemDeckState,
	port: StemRetryPort
): Promise<void> {
	// One retry per deck and track at a time: a double click or two agents
	// sending `stem_load` join the retry already running instead of starting a
	// second hydrate, reload or landing beside it. A retry for a track loaded
	// since is its own retry, never the old track's.
	const key = `${deck} ${stableId}`;
	const running = _retrying.get(key);
	if (running !== undefined) return running;
	const retry = _retryDeckStems(deck, stableId, stems, port).finally(() => _retrying.delete(key));
	_retrying.set(key, retry);
	return retry;
}

const _retrying = new Map<string, Promise<void>>();

async function _retryDeckStems(
	deck: number,
	stableId: string | null,
	stems: StemDeckState,
	port: StemRetryPort
): Promise<void> {
	if (stems.status === 'ready') return;
	else if (stems.status === 'loading') {
		if (port.landHeld !== null) await port.landHeld();
		else if ((port.releaseDecode ?? (() => releaseEagerStemDecodeNow(deck)))() === 0) {
			throw new Error(`deck ${deck} stems are already loading (${stems.load?.phase ?? 'probing'})`);
		}
		return;
	}
	if (port.reload === null || stableId === null) {
		throw new Error(`retryStems: deck ${deck} has no decoded mix to align stems to`);
	}
	if (stems.status === 'error') await (port.requestHydration ?? requestStemHydration)(stableId);
	// The hydrate request awaited: a track loaded since then owns the deck, and
	// this track's stems must never land on it.
	if (!port.current()) return;
	port.reload();
}
