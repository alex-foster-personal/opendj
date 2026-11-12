/**
 * PERF-CAPTURE-10504: pure arithmetic and guards for the four-deck frame bench.
 *
 * No I/O and no Playwright import. The only consumers are
 * tests/live/four-deck-frame-bench.mjs (the driver) and
 * tests/unit/frame-bench-stats.test.mjs. Every failure is a named
 * `four-deck-frame-bench: ...` error; no function returns null or NaN numbers.
 */
export const DECK_COUNT = 4;
export const LONG_FRAME_MS = 50;
export const BENCH = 'four-deck-frame-bench';

const round = (value, dp = 2) => Math.round(value * 10 ** dp) / 10 ** dp;

/** Nearest-rank percentile, the same rule as playhead-smoothness.mjs. */
export function percentile(values, p) {
	if (values.length === 0) throw new Error(`${BENCH}: no samples`);
	const sorted = [...values].sort((a, b) => a - b);
	return sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
}

/** Median idle rAF delta: the display refresh interval frame deltas are read against. */
export function refreshIntervalMs(idleDeltas) {
	if (idleDeltas.length === 0) throw new Error(`${BENCH}: no idle samples`);
	return percentile(idleDeltas, 0.5);
}

/** Throws unless all four decks are playing. Accepts the query().decks record or an array. */
export function assertFourDecksPlaying(decks) {
	const list = Array.isArray(decks)
		? decks
		: Array.from({ length: DECK_COUNT }, (_, i) => decks?.[i + 1] ?? decks?.[String(i + 1)]);
	const playing = list.slice(0, DECK_COUNT).filter((d) => d?.playing === true).length;
	if (playing !== DECK_COUNT)
		throw new Error(`${BENCH}: ${playing} of ${DECK_COUNT} decks playing`);
}

/** Builds the one JSON row the driver prints. */
export function summarizeFrameBench({ deltas, idleDeltas, waveRowMs, sampleMs }) {
	if (deltas.length === 0) throw new Error(`${BENCH}: no frame deltas sampled`);
	if (!(sampleMs > 0)) throw new Error(`${BENCH}: sample window is ${sampleMs} ms`);
	return {
		frames: deltas.length,
		interval_p50_ms: round(percentile(deltas, 0.5)),
		interval_p95_ms: round(percentile(deltas, 0.95)),
		interval_max_ms: round(Math.max(...deltas)),
		long_frames_over_50ms: deltas.filter((d) => d > LONG_FRAME_MS).length,
		wave_row_share: round(waveRowMs / sampleMs, 4),
		refresh_interval_ms: round(refreshIntervalMs(idleDeltas))
	};
}
