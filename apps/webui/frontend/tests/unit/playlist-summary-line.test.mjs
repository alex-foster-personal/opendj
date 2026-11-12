// requirement: SETPREP-10645
// [if] the summary line format drifts from the acceptance example [then] fail.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY =
	"export { formatDurationHms, formatPlaylistSummaryLine } from '$lib/rb/playlist-summary-format';";

const sample = {
	tracks: 42,
	duration_s: 10_080,
	bpm: { min: 122, median: 126, max: 128, missing: 1 },
	energy: { min: 5, max: 8, missing: 2 },
	keys: { distinct: 9, missing: 1 },
	unanalysed: 3
};

/** @type {import('$lib/rb/playlist-summary-format')} */
let mod;

before(async () => {
	mod = await loadRuneModule(ENTRY);
});

test('duration formats hours and minutes', () => {
	assert.equal(mod.formatDurationHms(10_080), '2 h 48 m');
	assert.equal(mod.formatDurationHms(90), '1 m');
});

test('summary line matches the acceptance shape', () => {
	const line = mod.formatPlaylistSummaryLine(sample);
	assert.match(line, /^42 tracks, 2 h 48 m,/);
	assert.match(line, /122 to 128 BPM \(126\)/);
	assert.match(line, /energy 5 to 8/);
	assert.match(line, /9 keys/);
	assert.match(line, /3 unanalysed$/);
});
