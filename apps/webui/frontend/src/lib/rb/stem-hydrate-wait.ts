/** Wait for a stem bundle the server is still fetching from R2 (STEM-37).
 *
 * Its own module, imported only by the deck engine, so the wait loop stays out
 * of the library's initial-load bundle (scripts/check-bundle-size.sh). */
import { probeStemArtifact } from '$lib/rb/api-rb';
import type { StemArtifactProbe } from '$lib/rb/api-rb';
import { stemDecodeBlockReason } from '$lib/rb/stem-decode-policy';
import { unavailableStemDeckState } from '$lib/rb/stem-graph';
import type { StemDeckState } from '$lib/rb/stem-types';

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
/** Re-ask delays: quick at first (a cached hub answers in seconds), then a
 * steady 5 s, so a long download costs one small GET per deck per 5 s. */
export const STEM_HYDRATE_POLL_MS: readonly number[] = [1000, 2000, 3000, 5000];

export type AwaitStemArtifactOptions = {
	/** True once the deck has moved on; the wait returns `null` at once. */
	isStale?: () => boolean;
	maxWaitMs?: number;
	sleep?: (ms: number) => Promise<void>;
	now?: () => number;
	probe?: (stableId: string) => Promise<StemArtifactProbe>;
};

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
		const waited = now() - started;
		if (waited >= maxWaitMs) {
			throw new Error(
				`stem bundle still downloading after ${Math.round(waited / 1000)} s ` +
					`(${result.error}); reload the track to try again`
			);
		}
		const delay = STEM_HYDRATE_POLL_MS[Math.min(attempt, STEM_HYDRATE_POLL_MS.length - 1)];
		await sleep(Math.min(delay, Math.max(0, maxWaitMs - waited)));
		if (isStale()) return null;
	}
}
