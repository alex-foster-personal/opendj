import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Captured from the live read-only ANLZ endpoint on 2026-09-03:
// /api/v1/tracks/310f7d2431c0327bca197e7ab38525e79fbcc383/anlz?points=100
// Azara A is 16 actual PQTZ intervals, not its raw BeatLoopSize 1048577.
const cue = { slot: 'A', in_ms: 115147, out_ms: 122952, is_loop: true,
	active_loop: false, beat_loop_size: 1048577, color_table_index: 0, comment: null };
const beats = [114.171, 114.659, 115.147, 115.635, 116.122, 116.61, 117.098,
	117.586, 118.074, 118.561, 119.049, 119.537, 120.025, 120.513, 121,
	121.488, 121.976, 122.464, 122.952, 123.44, 123.927].map((t) => ({ t }));

const syntheticLines = [
	{ start_ms: 0, text: 'alpha bravo charlie' },
	{ start_ms: 3000, text: 'delta echo foxtrot' },
	{ start_ms: 6000, text: 'golf hotel india' }
];

let hotCueTitle;

before(async () => {
	({ hotCueTitle } = await loadTypeScriptModule('src/lib/rb/hot-cue-label.ts'));
});

test('real Azara loop reports 16 PQTZ beats and 4 bars, not encoded vendor metadata', () => {
	assert.equal(hotCueTitle(cue, beats), 'hot cue A - loop 01:55.147 to 02:02.952; 16 beats (4 bars, PQTZ)');
	assert.equal(hotCueTitle(cue, beats, []), 'hot cue A - loop 01:55.147 to 02:02.952; 16 beats (4 bars, PQTZ)');
});

test('missing real grid reports unavailable rather than interpreting raw vendor metadata', () => {
	assert.match(hotCueTitle(cue, []), /beat count unavailable/);
	assert.doesNotMatch(hotCueTitle(cue, []), /1048577/);
	assert.match(hotCueTitle(cue, [], []), /beat count unavailable/);
});

test('point cue mid-line shows chevron on interpolated word', () => {
	const pointCue = { slot: 'A', in_ms: 1000, out_ms: null, is_loop: false,
		active_loop: false, beat_loop_size: 0, color_table_index: 0, comment: 'intro drop' };
	assert.equal(
		hotCueTitle(pointCue, [], syntheticLines),
		'intro drop - 00:01.000\nalpha ▸bravo charlie'
	);
});

test('point cue at exact line start puts chevron on first word', () => {
	const pointCue = { slot: 'A', in_ms: 0, out_ms: null, is_loop: false,
		active_loop: false, beat_loop_size: 0, color_table_index: 0, comment: 'start' };
	assert.equal(
		hotCueTitle(pointCue, [], syntheticLines),
		'start - 00:00.000\n▸alpha bravo charlie'
	);
});

test('point cue before first lyric omits lyric row', () => {
	const pointCue = { slot: 'A', in_ms: 500, out_ms: null, is_loop: false,
		active_loop: false, beat_loop_size: 0, color_table_index: 0, comment: 'early' };
	const lateStart = [{ start_ms: 1000, text: 'alpha bravo charlie' }];
	assert.equal(hotCueTitle(pointCue, [], lateStart), 'early - 00:00.500');
});

test('loop cue lists spanning lyric lines without chevrons', () => {
	const loopLines = [
		{ start_ms: 115000, text: 'alpha bravo' },
		{ start_ms: 120000, text: 'delta echo' },
		{ start_ms: 130000, text: 'golf hotel' }
	];
	assert.equal(
		hotCueTitle(cue, beats, loopLines),
		'hot cue A - loop 01:55.147 to 02:02.952; 16 beats (4 bars, PQTZ)\nalpha bravo\ndelta echo'
	);
});

test('loop cue with no lyrics in range keeps single-line title', () => {
	const afterOutOnly = [{ start_ms: 125000, text: 'alpha bravo' }];
	assert.equal(
		hotCueTitle(cue, beats, afterOutOnly),
		'hot cue A - loop 01:55.147 to 02:02.952; 16 beats (4 bars, PQTZ)'
	);
});

test('point cue with PQTZ beat appends beat coordinate', () => {
	const pointCue = { slot: 'A', in_ms: 115147, out_ms: null, is_loop: false,
		active_loop: false, beat_loop_size: 0, color_table_index: 0, comment: 'drop' };
	const title = hotCueTitle(pointCue, beats, []);
	assert.match(title, /01:55\.147/);
	assert.match(title, /beat 2 \(PQTZ\)/);
});
