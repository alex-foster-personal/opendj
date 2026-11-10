/**
 * The Settings waveform preview's beat grid (issue #10244): the REAL PQTZ
 * beats of tests/unit/fixtures/blocks-variance-anlz-window.json whose t falls
 * in [104, 116), re-based to 0, so the grid lines sit on the same seconds
 * 8..20 the band sample (waveform-preview-sample.ts) spans. A separate small
 * module so the band sample stays a lazy chunk.
 * Regenerate from that fixture, never hand-edit.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';

/** Seconds of music the preview sample spans. */
export const PREVIEW_SAMPLE_SECONDS = 12;

export const PREVIEW_SAMPLE_BEATS: readonly AnlzBeat[] = [
	{ t: 0.134, n: 2, bpm: 128 },
	{ t: 0.602, n: 3, bpm: 128 },
	{ t: 1.071, n: 4, bpm: 128 },
	{ t: 1.54, n: 1, bpm: 128 },
	{ t: 2.009, n: 2, bpm: 128 },
	{ t: 2.477, n: 3, bpm: 128 },
	{ t: 2.946, n: 4, bpm: 128 },
	{ t: 3.415, n: 1, bpm: 128 },
	{ t: 3.884, n: 2, bpm: 128 },
	{ t: 4.352, n: 3, bpm: 128 },
	{ t: 4.821, n: 4, bpm: 128 },
	{ t: 5.29, n: 1, bpm: 128 },
	{ t: 5.759, n: 2, bpm: 128 },
	{ t: 6.227, n: 3, bpm: 128 },
	{ t: 6.696, n: 4, bpm: 128 },
	{ t: 7.165, n: 1, bpm: 128 },
	{ t: 7.634, n: 2, bpm: 128 },
	{ t: 8.102, n: 3, bpm: 128 },
	{ t: 8.571, n: 4, bpm: 128 },
	{ t: 9.04, n: 1, bpm: 128 },
	{ t: 9.509, n: 2, bpm: 128 },
	{ t: 9.977, n: 3, bpm: 128 },
	{ t: 10.446, n: 4, bpm: 128 },
	{ t: 10.915, n: 1, bpm: 128 },
	{ t: 11.384, n: 2, bpm: 128 },
	{ t: 11.852, n: 3, bpm: 128 }
];
