/**
 * Polls Trackify's own playback state throughout a measured window, instead
 * of only at its two endpoints (Sol review, PR #3676: "Trackify playback is
 * checked only immediately before READY and again after the sampler
 * finishes ... The page can be stopped or idle for most of the 60-second or
 * one-hour window, resume before the final check, and still produce
 * `measured: true` savings and leak KPIs against an invalid workload").
 *
 * Kept side-effect-free and independently importable -- no Playwright launch
 * or argv parsing -- so it can be unit tested against a fake `page`, unlike
 * `mode_ratio_browser.mjs` itself, whose top-level `await`s make it
 * unimportable outside a real browser run.
 */
/**
 * Shared poll loop: calls `isPlayingInPage` (a zero-arg function run via
 * `page.evaluate`) on an interval until `signal` settles, and throws
 * `invalidMessage` if any poll (or a rejected `evaluate`, e.g. the page
 * navigated or closed) ever saw "not playing".
 */
async function _watchUntil(
	page,
	signal,
	isPlayingInPage,
	invalidMessage,
	{ pollMs = 2_000, evaluateArg } = {}
) {
	let stopped = false;
	let gapAt = null;
	// Chained off `signal` (not just flipped by the caller after `await
	// signal` resolves) so a poll iteration currently sleeping between polls
	// wakes IMMEDIATELY once the caller's own wait ends, via the `Promise.race`
	// below -- without this, that sleep is on a timer nothing else ever fires
	// again once the caller's `signal` has already settled, so the whole
	// function would hang past its own final `await poller`.
	const stopSignal = signal.then(() => {
		stopped = true;
	});
	const poller = (async () => {
		while (!stopped) {
			const playing = await page.evaluate(isPlayingInPage, evaluateArg).catch(() => false);
			if (!playing && gapAt === null) gapAt = Date.now();
			if (stopped) break;
			await Promise.race([stopSignal, new Promise((resolve) => setTimeout(resolve, pollMs))]);
		}
	})();
	await signal;
	await poller;
	if (gapAt !== null) {
		throw new Error(`${invalidMessage} (first gap detected at ${new Date(gapAt).toISOString()}); this capture is invalid`);
	}
}

export async function watchContinuousPlaybackUntil(page, signal, opts = {}) {
	await _watchUntil(
		page,
		signal,
		() => {
			const ipc = window.musicDjToolsTrackify;
			if (ipc === undefined) return false;
			const state = ipc.query();
			return state.deck.stable_id !== null && state.deck.playing === true;
		},
		'Trackify was not continuously playing throughout the measured interval',
		opts
	);
}

/**
 * Gig-side counterpart (Sol review round 6, PR #3676): `GIG_READY` only
 * proves four load/play commands were issued before the initial wait --
 * nothing verified all four decks were STILL playing while
 * `capture_mode_ratios.py`'s process-tree sampler actually ran, so a deck
 * whose track ended mid-window could sample a partially idle Gig session as
 * a measured four-deck steady state. Mirrors `watchContinuousPlaybackUntil`
 * exactly, checking every deck in `deckIds` via the Performance IPC instead
 * of the single Trackify deck.
 */
export async function watchGigDecksPlayingUntil(page, signal, deckIds, opts = {}) {
	await _watchUntil(
		page,
		signal,
		(decks) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) return false;
			const state = ipc.query();
			return decks.every((deckId) => {
				const deck = state.decks[deckId];
				return deck !== undefined && deck.stable_id !== null && deck.playing === true;
			});
		},
		'Gig was not continuously playing on all four decks throughout the measured interval',
		{ ...opts, evaluateArg: deckIds }
	);
}
