import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Real PQTZ entries captured from the cached ANLZ payload for stable id
// e3f272a118f7c8f610c2d28d2881719d442b0de8. Geometry tests consume the
// same n/bpm/t shape that GET /api/v1/tracks/{stable_id}/anlz returns.
const REAL_PQTZ_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 },
	{ n: 1, bpm: 127, t: 2.025 },
	{ n: 2, bpm: 127, t: 2.497 },
	{ n: 3, bpm: 127, t: 2.97 },
	{ n: 4, bpm: 127, t: 3.442 }
];
let visibleBeatLines;

before(async () => {
	({ visibleBeatLines } = await loadTypeScriptModule(
		'src/lib/components/rb/wave/wave-math.ts'
	));
});

test('visible beat lines span the full row and retain stronger downbeat caps', () => {
	assert.equal(typeof visibleBeatLines, 'function');

	const lines = visibleBeatLines(REAL_PQTZ_BEATS, 0, 100, 360, 40);
	assert.deepEqual(
		lines.map((line) => line.x),
		[14, 61, 108, 155, 203, 250, 297, 344]
	);
	assert.ok(lines.every((line) => line.y === 0 && line.height === 40));

	const downbeat = lines[0];
	const ordinaryBeat = lines[1];
	assert.equal(downbeat.isDownbeat, true);
	assert.equal(ordinaryBeat.isDownbeat, false);
	assert.ok(downbeat.width > ordinaryBeat.width);
	assert.ok(downbeat.alpha > ordinaryBeat.alpha);
	assert.ok(downbeat.capHeight > ordinaryBeat.capHeight);
	assert.ok(downbeat.capAlpha > ordinaryBeat.capAlpha);
});

test('a tempo jump just past the right edge does not thin the visible grid', () => {
	// Window is 0 to 1.0 s at 100 px/s. The 1.0 to 1.01 s gap starts in view but
	// ends outside it, so it must not set the stride: the three beats in view
	// (0.0, 0.5, 1.0 s) are 50 px apart and all keep a line.
	const beats = [
		{ n: 1, bpm: 120, t: 0 },
		{ n: 2, bpm: 120, t: 0.5 },
		{ n: 3, bpm: 120, t: 1.0 },
		{ n: 4, bpm: 120, t: 1.01 },
		{ n: 1, bpm: 120, t: 1.02 }
	];
	const lines = visibleBeatLines(beats, 0, 100, 100, 40);
	assert.deepEqual(
		lines.map((line) => line.x),
		[0, 50, 100]
	);
});
