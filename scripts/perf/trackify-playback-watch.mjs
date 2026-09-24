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
 * How long a Trackify deck may legitimately read "not playing" before a gap
 * counts as a real stall rather than an ordinary track-boundary handoff
 * (Sol review round 10, PR #3676: "Trackify intentionally uses one deck and
 * unloads, loads, then plays at every track boundary, so `deck.playing` is
 * legitimately false during each decode gap. Any two-second poll that lands
 * in that normal transition permanently invalidates the capture"). Sized to
 * absorb a couple of back-to-back quarantined-candidate retries, each
 * bounded near `TRACKIFY_LOAD_SKIP_DEADLINE_MS` (2000ms) in
 * trackify-autoplay.svelte.ts, while still catching a genuinely stuck deck
 * well inside a 60 s or one-hour capture window. Not imported from that
 * frontend module: this script lives outside the SvelteKit build and is
 * deliberately dependency-free (see file docstring above).
 */
const TRACKIFY_HANDOFF_TOLERANCE_MS = 8_000;

/**
 * Shared poll loop: calls `isPlayingInPage` (a zero-arg function run via
 * `page.evaluate`) on an interval until `signal` settles.
 *
 * Two modes, selected by `tolerateGapsUnderMs`:
 *  - `null` (default; used by the Gig variant, which has no legitimate
 *    reason for any deck to stop): the ORIGINAL zero-tolerance behavior --
 *    any single poll that ever saw "not playing" invalidates the capture,
 *    even if it recovers well before `signal` settles.
 *  - a number (used by the Trackify variant): a gap that recovers within
 *    that many ms is forgiven as a normal handoff; only a gap SUSTAINED
 *    past it invalidates the capture, and a sustained failure is never
 *    un-recorded by a later recovery.
 *
 * Either mode treats a rejected `evaluate` (e.g. the page navigated or
 * closed) as an unconditional, non-tolerated failure: that is a stronger
 * signal than a legitimate playback gap and is never forgiven.
 */
async function _watchUntil(
	page,
	signal,
	isPlayingInPage,
	invalidMessage,
	{ pollMs = 2_000, tolerateGapsUnderMs = null, evaluateArg } = {}
) {
	let stopped = false;
	let gapAt = null; // zero-tolerance mode: sticky, first gap ever seen.
	let gapStartedAt = null; // bounded-tolerance mode: reset whenever playback resumes.
	let sustainedGapAt = null; // bounded-tolerance mode: sticky once a gap exceeds the bound.
	let evaluateFailedAt = null;
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
			let playing;
			try {
				playing = await page.evaluate(isPlayingInPage, evaluateArg);
			} catch {
				playing = false;
				if (evaluateFailedAt === null) evaluateFailedAt = Date.now();
			}
			if (tolerateGapsUnderMs === null) {
				if (!playing && gapAt === null) gapAt = Date.now();
			} else if (playing) {
				gapStartedAt = null;
			} else {
				if (gapStartedAt === null) gapStartedAt = Date.now();
				if (sustainedGapAt === null && Date.now() - gapStartedAt > tolerateGapsUnderMs) {
					sustainedGapAt = gapStartedAt;
				}
			}
			if (stopped) break;
			await Promise.race([stopSignal, new Promise((resolve) => setTimeout(resolve, pollMs))]);
		}
	})();
	await signal;
	await poller;
	if (tolerateGapsUnderMs === null) {
		if (gapAt !== null) {
			throw new Error(`${invalidMessage} (first gap detected at ${new Date(gapAt).toISOString()}); this capture is invalid`);
		}
		return;
	}
	if (evaluateFailedAt !== null) {
		throw new Error(`${invalidMessage} (page.evaluate failed at ${new Date(evaluateFailedAt).toISOString()}); this capture is invalid`);
	}
	if (sustainedGapAt !== null) {
		throw new Error(
			`${invalidMessage} (stalled for over ${tolerateGapsUnderMs}ms starting ${new Date(sustainedGapAt).toISOString()}); this capture is invalid`
		);
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
		{ tolerateGapsUnderMs: TRACKIFY_HANDOFF_TOLERANCE_MS, ...opts }
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
 * of the single Trackify deck. Kept zero-tolerance (no handoff allowance):
 * unlike Trackify, Gig has no track-boundary design that legitimately drops
 * a deck to not-playing, so any gap here is a real anomaly.
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
