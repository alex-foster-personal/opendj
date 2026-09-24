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
export async function watchContinuousPlaybackUntil(page, signal, { pollMs = 2_000 } = {}) {
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
			const playing = await page
				.evaluate(() => {
					const ipc = window.musicDjToolsTrackify;
					if (ipc === undefined) return false;
					const state = ipc.query();
					return state.deck.stable_id !== null && state.deck.playing === true;
				})
				.catch(() => false);
			if (!playing && gapAt === null) gapAt = Date.now();
			if (stopped) break;
			await Promise.race([stopSignal, new Promise((resolve) => setTimeout(resolve, pollMs))]);
		}
	})();
	await signal;
	await poller;
	if (gapAt !== null) {
		throw new Error(
			'Trackify was not continuously playing throughout the measured interval ' +
				`(first gap detected at ${new Date(gapAt).toISOString()}); this capture is invalid`
		);
	}
}
