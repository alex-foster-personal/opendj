// requirement: AUTOPLAY-12017
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let hasPhraseAnalysis;
let mixWindowsForTrack;
let pickAutoPlayMixPair;

const INTRO = 1;
const UP = 2;
const DOWN = 3;
const CHORUS = 5;
const OUTRO = 6;
const BEAT_S = 60 / 128;

function _beats(totalBars) {
	const out = [];
	for (let i = 0; i < totalBars * 4; i += 1) {
		out.push({ n: (i % 4) + 1, bpm: 128, t: i * BEAT_S });
	}
	return out;
}

function _phrases(arrangement) {
	const phrases = [];
	let bar = 0;
	for (const [kind, bars] of arrangement) {
		const startS = bar * 4 * BEAT_S;
		bar += bars;
		phrases.push({ start_s: startS, end_s: bar * 4 * BEAT_S, kind, mood: 1 });
	}
	return phrases;
}

const DEFAULT_ARRANGEMENT = [
	[INTRO, 16],
	[UP, 16],
	[CHORUS, 32],
	[DOWN, 16],
	[UP, 16],
	[CHORUS, 16],
	[OUTRO, 16]
];

function _trackInput(arrangement = DEFAULT_ARRANGEMENT, vocals = null) {
	const totalBars = arrangement.reduce((sum, row) => sum + row[1], 0);
	const beats = _beats(totalBars);
	return {
		beats,
		duration_sec: totalBars * 4 * BEAT_S,
		phrases: _phrases(arrangement),
		phrases_source: 'rekordbox',
		vocals
	};
}

before(async () => {
	({ hasPhraseAnalysis, mixWindowsForTrack, pickAutoPlayMixPair } = await loadTypeScriptModule(
		'src/lib/rb/auto-play-windows.ts'
	));
});

test('[if] no phrase analysis [then] helpers offer no invented phrase windows', () => {
	const empty = {
		beats: _beats(32),
		duration_sec: 32 * 4 * BEAT_S,
		phrases: [],
		phrases_source: 'none'
	};
	assert.equal(hasPhraseAnalysis(empty), false);
	assert.deepEqual(mixWindowsForTrack(empty, 'out'), []);
	assert.deepEqual(pickAutoPlayMixPair(empty, empty), {
		phrase_analysis: false,
		out: null,
		in: null
	});
});

test('[if] both tracks have phrases [then] out prefers outro and in prefers intro', () => {
	const out = _trackInput();
	const inTrack = _trackInput([
		[INTRO, 32],
		[CHORUS, 32],
		[OUTRO, 16]
	]);
	const outWindows = mixWindowsForTrack(out, 'out');
	const inWindows = mixWindowsForTrack(inTrack, 'in');
	assert.equal(outWindows[0]?.label, 'outro');
	assert.equal(inWindows[0]?.label, 'intro');
	assert.equal(inWindows[0]?.start_bar, 1);
});

test('[if] vocals are analyzed [then] vocal_free_bars is set on ranked windows', () => {
	const arrangement = [
		[INTRO, 16],
		[CHORUS, 16],
		[OUTRO, 16]
	];
	const input = _trackInput(arrangement, {
		status: 'rekordbox',
		fps: 150,
		regions: [{ start_s: 16 * 4 * BEAT_S, end_s: 32 * 4 * BEAT_S, intensity: 1 }]
	});
	const inWindows = mixWindowsForTrack(input, 'in');
	assert.ok(inWindows[0]?.vocal_free_bars !== null && inWindows[0].vocal_free_bars >= 0);
});

test('[if] outro tail is under 4 bars [then] the out window is marked too_short', () => {
	const beats = _beats(20);
	const phrases = [{ start_s: 16 * 4 * BEAT_S, end_s: 20 * 4 * BEAT_S, kind: OUTRO, mood: 1 }];
	const input = {
		beats,
		duration_sec: 20 * 4 * BEAT_S,
		phrases,
		phrases_source: 'rekordbox'
	};
	const [out] = mixWindowsForTrack(input, 'out');
	assert.equal(out.label, 'outro');
	assert.equal(out.too_short, true);
	assert.equal(out.max_bars, 3);
});

test('[if] both tracks have phrases [then] pickAutoPlayMixPair returns ranked ends', () => {
	const pair = pickAutoPlayMixPair(
		_trackInput(),
		_trackInput([
			[INTRO, 32],
			[OUTRO, 8]
		])
	);
	assert.equal(pair.phrase_analysis, true);
	assert.equal(pair.out?.label, 'outro');
	assert.equal(pair.in?.label, 'intro');
});
