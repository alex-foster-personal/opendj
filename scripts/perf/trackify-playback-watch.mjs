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
 * Ceiling on the fraction of polls that may see "not playing" once enough
 * samples exist to judge it, even when no SINGLE gap ever exceeds
 * `TRACKIFY_HANDOFF_TOLERANCE_MS` (Sol review round 11, PR #3676: "a run
 * that is idle for seven seconds, plays for one poll, and repeats can pass
 * the 8 s tolerance while spending most of the measured window idle").
 * Real Trackify handoff overhead across a set of multi-minute tracks should
 * be a small fraction of total playback time; 15% gives generous headroom
 * over that while still catching a duty-cycle pattern like the one quoted
 * above (which idles roughly 3 times as often as it plays).
 */
const TRACKIFY_MAX_IDLE_DUTY_CYCLE = 0.15;

/** Minimum polls observed before judging a duty cycle at all, so one early
 * handoff cannot look like 100% idle against a near-empty sample. */
const _MIN_DUTY_CYCLE_SAMPLE_POLLS = 8;

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
 *    past it invalidates the capture immediately. Separately, when
 *    `maxIdleDutyCycle` is also set, the OVERALL not-playing fraction across
 *    every poll in the whole window is checked once at the end -- a
 *    duty cycle is a property of the whole window, not a point-in-time
 *    snapshot, so an early cluster of gaps that the rest of a long capture
 *    dilutes back down must not be judged before all the samples are in
 *    (unlike a sustained-gap failure, which stays failed once true). A
 *    sustained-gap failure is never un-recorded by a later recovery.
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
	{ pollMs = 2_000, tolerateGapsUnderMs = null, maxIdleDutyCycle = null, evaluateArg } = {}
) {
	let stopped = false;
	let gapAt = null; // zero-tolerance mode: sticky, first gap ever seen.
	let gapStartedAt = null; // bounded-tolerance mode: reset whenever playback resumes.
	let sustainedGapAt = null; // bounded-tolerance mode: sticky once a gap exceeds the bound.
	let evaluateFailedAt = null;
	let totalPolls = 0;
	let notPlayingPolls = 0;
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
			totalPolls += 1;
			if (!playing) notPlayingPolls += 1;
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
	if (
		maxIdleDutyCycle !== null &&
		totalPolls >= _MIN_DUTY_CYCLE_SAMPLE_POLLS &&
		notPlayingPolls / totalPolls > maxIdleDutyCycle
	) {
		const pct = Math.round((notPlayingPolls / totalPolls) * 100);
		throw new Error(
			`${invalidMessage} (not playing on ${pct}% of ${totalPolls} polls, exceeding the ${Math.round(maxIdleDutyCycle * 100)}% idle budget); this capture is invalid`
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
		{
			tolerateGapsUnderMs: TRACKIFY_HANDOFF_TOLERANCE_MS,
			maxIdleDutyCycle: TRACKIFY_MAX_IDLE_DUTY_CYCLE,
			...opts
		}
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

/**
 * Why `decks` (deck id -> {stable_id, duration_ms, playing} or null) is not
 * the four-deck Gig baseline `expected` names, one string per fault; empty
 * when every deck holds exactly its picked track, loaded with a positive
 * duration, and playing. Queue-idle and HEAD 200 do not prove a load landed
 * (Sol P1/BLOCKING, PR #4540), so mode_ratio_browser.mjs checks this before
 * printing GIG_READY.
 */
export function gigBaselineDeckFaults(decks, expected) {
	return expected.flatMap((stableId, index) => {
		const deckId = index + 1;
		const deck = decks[deckId];
		if (deck === undefined || deck === null) return [`deck ${deckId} is missing from the IPC state`];
		const faults = [];
		if (deck.stable_id !== stableId) {
			faults.push(`deck ${deckId} holds ${deck.stable_id}, expected ${stableId}`);
		}
		if (typeof deck.duration_ms !== 'number' || !(deck.duration_ms > 0)) {
			faults.push(`deck ${deckId} duration_ms=${deck.duration_ms}`);
		}
		if (deck.playing !== true) faults.push(`deck ${deckId} is not playing`);
		return faults;
	});
}
