/**
 * The Settings waveform preview's beat grid (issue #10244): beats from
 * tests/unit/fixtures/blocks-variance-anlz-window.json in [window+8, window+20),
 * re-based to 0. Regenerate via scripts/write-deck-window-derived.mjs, never hand-edit.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';

/** Seconds of music the preview sample spans. */
export const PREVIEW_SAMPLE_SECONDS = 12;

export const PREVIEW_SAMPLE_BEATS: readonly AnlzBeat[] = [
	{ t: 0.22499999999999432, n: 3, bpm: 128 },
	{ t: 0.6940000000000026, n: 4, bpm: 128 },
	{ t: 1.1629999999999967, n: 1, bpm: 128 },
	{ t: 1.632000000000005, n: 2, bpm: 128 },
	{ t: 2.0999999999999943, n: 3, bpm: 128 },
	{ t: 2.5690000000000026, n: 4, bpm: 128 },
	{ t: 3.0379999999999967, n: 1, bpm: 128 },
	{ t: 3.507000000000005, n: 2, bpm: 128 },
	{ t: 3.9749999999999943, n: 3, bpm: 128 },
	{ t: 4.444000000000003, n: 4, bpm: 128 },
	{ t: 4.912999999999997, n: 1, bpm: 128 },
	{ t: 5.382000000000005, n: 2, bpm: 128 },
	{ t: 5.849999999999994, n: 3, bpm: 128 },
	{ t: 6.319000000000003, n: 4, bpm: 128 },
	{ t: 6.787999999999997, n: 1, bpm: 128 },
	{ t: 7.257000000000005, n: 2, bpm: 128 },
	{ t: 7.724999999999994, n: 3, bpm: 128 },
	{ t: 8.194000000000003, n: 4, bpm: 128 },
	{ t: 8.662999999999997, n: 1, bpm: 128 },
	{ t: 9.132000000000005, n: 2, bpm: 128 },
	{ t: 9.599999999999994, n: 3, bpm: 128 }
];
