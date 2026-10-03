/** Wait for a stem bundle the server is still fetching from R2 (STEM-37).
 *
 * Its own module, imported only by the deck engine, so the wait loop stays out
 * of the library's initial-load bundle (scripts/check-bundle-size.sh). */
import { probeStemArtifact, RB_API_BASE } from '$lib/rb/api-rb';
import type { StemArtifactProbe } from '$lib/rb/api-rb';
import { releaseEagerStemDecodeNow } from '$lib/rb/stem-decode-shed';
import { stemDecodeBlockReason } from '$lib/rb/stem-decode-policy';
import { unavailableStemDeckState } from '$lib/rb/stem-graph';
import type { StemDeckState, StemFetchProgress } from '$lib/rb/stem-types';
import type { HeldStemLandingPort, StemLandingOutcome, StemLandingPort } from '$lib/rb/stem-live-handoff';

// The deck engine sits at its import fan-out ceiling, so the rest of the stem
// landing surface reaches it through this module, which it already imports.
export type { StemLandingOutcome } from '$lib/rb/stem-live-handoff';

/** STEM-47. Land built stems on a deck (stem-live-handoff.ts). The handoff is
 * imported on first use, not statically: the engine is in the library page's
 * first paint, and a landing only ever follows a stem decode of seconds, so
 * one local chunk fetch there costs the deck nothing it would notice. */
export async function landStemsOnDeck(port: StemLandingPort): Promise<StemLandingOutcome> {
	const handoff = await import('$lib/rb/stem-live-handoff');
	return handoff.landStemsOnDeck(port);
}

/** STEM-47. `LOAD NOW` on a held bundle, settling the deck if it rejects. */
export async function landHeldStemsOrSettle(port: HeldStemLandingPort): Promise<void> {
	return (await import('$lib/rb/stem-live-handoff')).landHeldStemsOrSettle(port);
}

/** Why a decode is held (shown on the deck while `waiting`). */
export const STEM_HELD_BY_PRESSURE = 'this machine is under pressure while a deck is playing';
/** Why finished stems have not taken over yet (shown on the deck while `waiting`). */
export const STEM_HELD_BY_TRANSPORT = 'the deck was busy with transport commands';

/** PERFMODE-15: the settled `unavailable` stem state a deck shows while stems
 * are blocked (Trackify), naming the reason, or null when stems are allowed.
 * Here, beside the probe the block gates, because the engine already imports
 * this module; the policy itself stays dependency-free. */
export function stemsBlockedState(): StemDeckState | null {
	const reason = stemDecodeBlockReason();
	return reason === null ? null : unavailableStemDeckState(`stems disabled: ${reason}`);
}

/** The block check for ONE stem upgrade. Once stems are blocked it returns
 * true and, while `isCurrent()` still holds for that upgrade's load, settles
 * `deck.stems` to the blocked state. Read it before the upgrade's first await
 * and in every stale check after it, so an upgrade already under way when the
 * block begins stops at its next check. */
export function stemBlockCheck(deck: { stems: StemDeckState }, isCurrent: () => boolean): () => boolean {
	return () => {
		const blockedState = stemsBlockedState();
		if (blockedState === null) return false;
		if (isCurrent()) deck.stems = blockedState;
		return true;
	};
}

/** How long a deck keeps re-asking for a bundle the server is still fetching
 * from R2. A four-part bundle is tens of MB; ten minutes covers a slow venue
 * link, and the loop stops the moment the deck loads another track. */
export const STEM_HYDRATE_MAX_WAIT_MS = 10 * 60 * 1000;
/** Re-ask delays (PERFMODE-18). The deck learns a fetch finished only on its
 * next probe, so the delay IS the lag between "the engine has the bundle" and
 * the deck leaving FETCHING. It was 1, 2, 3 then a steady 5 s, which left a
 * finished fetch unnoticed for up to 5 s (3 s measured Thu 1 Oct 2026). Now
 * 500 ms, then 1 s for the first minute, where almost every fetch ends (7 to
 * 17 s measured); a download still running after that is a slow link, and
 * falls back to one small GET per deck per 5 s. */
export const STEM_HYDRATE_POLL_MS: readonly number[] = [500, 1000];
export const STEM_HYDRATE_SLOW_AFTER_MS = 60_000;
export const STEM_HYDRATE_SLOW_POLL_MS = 5000;

export function stemHydratePollDelayMs(attempt: number, waitedMs: number): number {
	if (waitedMs >= STEM_HYDRATE_SLOW_AFTER_MS) return STEM_HYDRATE_SLOW_POLL_MS;
	return STEM_HYDRATE_POLL_MS[Math.min(attempt, STEM_HYDRATE_POLL_MS.length - 1)];
}

export type AwaitStemArtifactOptions = {
	/** True once the deck has moved on; the wait returns `null` at once. */
	isStale?: () => boolean;
	maxWaitMs?: number;
	sleep?: (ms: number) => Promise<void>;
	now?: () => number;
	probe?: (stableId: string) => Promise<StemArtifactProbe>;
	/** STEM-45: called for every `hydrating` answer with the fetch's progress
	 * (null when the server reported none), so the deck can name the fetch. */
	onHydrating?: (progress: StemFetchProgress | null) => void;
};

/** STEM-46: ask the engine to fetch (or re-fetch) this track's cloud bundle,
 * clearing a recorded failure first. The manifest GET alone cannot do that: a
 * failed fetch answers 502 there until something asks again. Rejects on any
 * non-2xx, naming the status; never silent. */
export async function requestStemHydration(
	stableId: string,
	fetcher: typeof fetch = fetch
): Promise<void> {
	const path = `/api/v1/tracks/${encodeURIComponent(stableId)}/stems/hydrate`;
	const response = await fetcher(`${RB_API_BASE}${path}`, { method: 'POST' });
	if (!response.ok) throw new Error(`stem hydrate request failed: HTTP ${response.status} ${path}`);
}

export type SettledStemArtifactProbe = Exclude<StemArtifactProbe, { status: 'hydrating' }>;

/** Probe the stem bundle, and keep re-probing while the server reports it is
 * still hydrating it from R2, until it is `ready`, settled `unavailable`, or
 * the deck goes stale (`null`).
 *
 * This is the path every installed spoke (the Air, silver) takes for a track
 * whose bundle lives only in R2: the first GET starts the download and answers
 * `hydrating`. Reading that first answer as final left the deck with no stems
 * until the track was loaded a second time, which is the "stems not reliably
 * appearing" report. A wait that outlives `maxWaitMs` rejects loudly, so the
 * deck shows a stems error rather than silently claiming the track has none. */
export async function awaitStemArtifact(
	stableId: string,
	options: AwaitStemArtifactOptions = {}
): Promise<SettledStemArtifactProbe | null> {
	const isStale = options.isStale ?? (() => false);
	const maxWaitMs = options.maxWaitMs ?? STEM_HYDRATE_MAX_WAIT_MS;
	const sleep =
		options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
	const now = options.now ?? (() => Date.now());
	const probe = options.probe ?? probeStemArtifact;
	const started = now();
	for (let attempt = 0; ; attempt += 1) {
		const result = await probe(stableId);
		if (isStale()) return null;
		if (result.status !== 'hydrating') return result;
		options.onHydrating?.(result.progress);
		const waited = now() - started;
		if (waited >= maxWaitMs) {
			throw new Error(
				`stem bundle still downloading after ${Math.round(waited / 1000)} s ` +
					`(${result.error}); reload the track to try again`
			);
		}
		const delay = stemHydratePollDelayMs(attempt, waited);
		await sleep(Math.min(delay, Math.max(0, maxWaitMs - waited)));
		if (isStale()) return null;
	}
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
		else if ((port.releaseDecode ?? releaseEagerStemDecodeNow)() === 0) {
			throw new Error(`deck ${deck} stems are already loading (${stems.load?.phase ?? 'probing'})`);
		}
		return;
	}
	if (port.reload === null || stableId === null) {
		throw new Error(`retryStems: deck ${deck} has no decoded mix to align stems to`);
	}
	if (stems.status === 'error') await (port.requestHydration ?? requestStemHydration)(stableId);
	port.reload();
}
