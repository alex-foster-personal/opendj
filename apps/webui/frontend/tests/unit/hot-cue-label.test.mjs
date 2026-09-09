import assert from 'node:assert/strict';
import { test } from 'node:test';
import { build } from 'esbuild';

// Captured from the live read-only ANLZ endpoint on 2026-09-03:
// /api/v1/tracks/310f7d2431c0327bca197e7ab38525e79fbcc383/anlz?points=100
// Azara A is 16 actual PQTZ intervals, not its raw BeatLoopSize 1048577.
const cue = { slot: 'A', in_ms: 115147, out_ms: 122952, is_loop: true,
	active_loop: false, beat_loop_size: 1048577, color_table_index: 0, comment: null };
const beats = [114.171, 114.659, 115.147, 115.635, 116.122, 116.61, 117.098,
	117.586, 118.074, 118.561, 119.049, 119.537, 120.025, 120.513, 121,
	121.488, 121.976, 122.464, 122.952, 123.44, 123.927].map((t) => ({ t }));

async function labels() {
	const result = await build({ entryPoints: [new URL('../../src/lib/rb/hot-cue-label.ts', import.meta.url).pathname],
		bundle: true, write: false, format: 'esm', platform: 'node', logLevel: 'silent' });
	return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

test('real Azara loop reports 16 PQTZ beats and 4 bars, not encoded vendor metadata', async () => {
	const { hotCueTitle } = await labels();
	assert.equal(hotCueTitle(cue, beats), 'hot cue A - loop 01:55.147 to 02:02.952; 16 beats (4 bars, PQTZ)');
});

test('missing real grid reports unavailable rather than interpreting raw vendor metadata', async () => {
	const { hotCueTitle } = await labels();
	assert.match(hotCueTitle(cue, []), /beat count unavailable/);
	assert.doesNotMatch(hotCueTitle(cue, []), /1048577/);
});
