import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { createServer } from 'vite';

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
const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let visibleBeatLines;

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true }
	});
	({ visibleBeatLines } = await vite.ssrLoadModule(
		'/src/lib/components/rb/wave/wave-math.ts'
	));
});

after(async () => {
	await vite.close();
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
