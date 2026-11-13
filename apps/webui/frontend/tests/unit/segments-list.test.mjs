/**
 * Requirements: STRUCT-9657.
 *
 * Segments tab row model (segments-list.ts): one row per section with its
 * start bar, length in bars and `m:ss` start time, the playing row, and the
 * first empty hot cue slot for "+ hot cue".
 *
 * Regression lines:
 * - if a boundary on a downbeat is counted into the section it closes then
 *   every length reads one bar long
 * - if a track with no beat grid gets bar numbers then a guess renders as fact
 * - if the playhead past the last section still highlights it then the list
 *   lies about what is playing
 * - if a full bank yields a slot then "+ hot cue" overwrites a cue
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/deck/segments-list.ts');
});

const BAR_S = (4 * 60) / 128;
const beats = Array.from({ length: 96 * 4 }, (_, i) => ({
	n: (i % 4) + 1,
	bpm: 128,
	t: (i * 60) / 128
}));
const span = (label, startBar, bars) => ({
	start_s: startBar * BAR_S,
	end_s: (startBar + bars) * BAR_S,
	label,
	family: 'neutral',
	ambient: false,
	labelWidthPx: -1
});
const SIX = [
	span('Intro', 0, 16),
	span('Verse 1', 16, 16),
	span('Build', 32, 8),
	span('Drop', 40, 16),
	span('Breakdown', 56, 16),
	span('Drop 2', 72, 16)
];

describe('buildSegmentRows', () => {
	it('gives each section its full label, start bar, length in bars and m:ss time', () => {
		const { rows } = mod.buildSegmentRows(SIX, beats, 0);
		assert.deepEqual(
			rows.map((r) => [r.label, r.start_bar, r.length_bars, r.time]),
			[
				['Intro', 1, 16, '0:00'],
				['Verse 1', 17, 16, '0:30'],
				['Build', 33, 8, '1:00'],
				['Drop', 41, 16, '1:15'],
				['Breakdown', 57, 16, '1:45'],
				['Drop 2', 73, 16, '2:15']
			]
		);
		assert.equal(rows[3].start_ms, 75000);
	});

	it('marks the section holding the playhead as playing, and follows it', () => {
		assert.equal(mod.buildSegmentRows(SIX, beats, 0).playing_index, 0);
		assert.equal(mod.buildSegmentRows(SIX, beats, 80_000).playing_index, 3);
		assert.equal(mod.buildSegmentRows(SIX, beats, 75_000).playing_index, 3);
		assert.equal(mod.buildSegmentRows(SIX, beats, 200_000).playing_index, -1);
	});

	it('leaves bar cells null with no beat grid instead of guessing', () => {
		const { rows } = mod.buildSegmentRows(SIX, null, 0);
		assert.equal(rows[2].start_bar, null);
		assert.equal(rows[2].length_bars, null);
		assert.equal(rows[2].time, '1:00');
	});

	it('returns no rows for a track with no sections', () => {
		assert.deepEqual(mod.buildSegmentRows([], beats, 0), { rows: [], playing_index: -1 });
		assert.equal(mod.NO_SECTIONS_MESSAGE, 'No sections for this track');
	});
});

describe('firstEmptyHotCueSlot', () => {
	it('picks the first slot A..H with no cue', () => {
		assert.equal(mod.firstEmptyHotCueSlot([]), 'A');
		assert.equal(mod.firstEmptyHotCueSlot([{ slot: 'A' }, { slot: 'C' }]), 'B');
	});

	it('returns null when all 8 are full so nothing is overwritten', () => {
		const full = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({ slot }));
		assert.equal(mod.firstEmptyHotCueSlot(full), null);
		assert.equal(mod.HOT_CUES_FULL_MESSAGE, 'All 8 hot cues are full');
	});
});
