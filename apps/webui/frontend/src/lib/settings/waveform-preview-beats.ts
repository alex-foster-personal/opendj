/**
 * The Settings waveform preview's beat grid (issue #10244): beats from
 * tests/unit/fixtures/blocks-variance-anlz-window.json in [window+8, window+20),
 * re-based to 0. Regenerate via scripts/write-deck-window-derived.mjs, never hand-edit.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';

/** Seconds of music the preview sample spans. */
export const PREVIEW_SAMPLE_SECONDS = 12;

export const PREVIEW_SAMPLE_BEATS: readonly AnlzBeat[] = [
	{ t: 0.4380000000000024, n: 3, bpm: 128 },
	{ t: 0.9060000000000059, n: 4, bpm: 128 },
	{ t: 1.375, n: 1, bpm: 128 },
	{ t: 1.843999999999994, n: 2, bpm: 128 },
	{ t: 2.3119999999999976, n: 3, bpm: 128 },
	{ t: 2.781000000000006, n: 4, bpm: 128 },
	{ t: 3.25, n: 1, bpm: 128 },
	{ t: 3.718999999999994, n: 2, bpm: 128 },
	{ t: 4.188000000000002, n: 3, bpm: 128 },
	{ t: 4.656000000000006, n: 4, bpm: 128 },
	{ t: 5.125, n: 1, bpm: 128 },
	{ t: 5.593999999999994, n: 2, bpm: 128 },
	{ t: 6.061999999999998, n: 3, bpm: 128 },
	{ t: 6.531000000000006, n: 4, bpm: 128 },
	{ t: 7, n: 1, bpm: 128 },
	{ t: 7.468999999999994, n: 2, bpm: 128 },
	{ t: 7.938000000000002, n: 3, bpm: 128 },
	{ t: 8.406000000000006, n: 4, bpm: 128 },
	{ t: 8.875, n: 1, bpm: 128 },
	{ t: 9.343999999999994, n: 2, bpm: 128 },
	{ t: 9.811999999999998, n: 3, bpm: 128 },
	{ t: 10.281000000000006, n: 4, bpm: 128 },
	{ t: 10.75, n: 1, bpm: 128 },
	{ t: 11.218999999999994, n: 2, bpm: 128 },
	{ t: 11.688000000000002, n: 3, bpm: 128 }
];
