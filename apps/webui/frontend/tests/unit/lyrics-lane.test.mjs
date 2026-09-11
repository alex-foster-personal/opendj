import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let lyricLanePositionPercent;
let activeLyricLineIndex;

before(async () => {
	({ lyricLanePositionPercent, activeLyricLineIndex } = await loadTypeScriptModule(
		'src/lib/components/rb/wave/lyrics-lane.ts'
	));
});

test('lyrics lane projects track timestamps through the same pitch-scaled waveform window', () => {
	assert.equal(
		lyricLanePositionPercent({ lineStartMs: 66_000, positionMs: 60_000, pitch: 1, windowSeconds: 24 }),
		75
	);
	assert.equal(
		lyricLanePositionPercent({ lineStartMs: 66_000, positionMs: 60_000, pitch: 2, windowSeconds: 24 }),
		62.5
	);
});

test('lyrics lane follows the latest line at or before the playhead', () => {
	const lines = [{ start_ms: 0 }, { start_ms: 1_000 }, { start_ms: 2_500 }];
	assert.equal(activeLyricLineIndex(lines, 0), 0);
	assert.equal(activeLyricLineIndex(lines, 2_499), 1);
	assert.equal(activeLyricLineIndex(lines, 2_500), 2);
	assert.equal(activeLyricLineIndex(lines, -1), -1);
});
