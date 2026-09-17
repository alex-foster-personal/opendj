/**
 * CUEOUT-15 R6: the library preview tempo-matches the playing master deck.
 *
 * Regression lines:
 * - if a preview does not match an in-range master tempo then the operator auditions against a clashing pulse - broken
 * - if a match outside the pitch range is forced then a preview plays at a tempo the track cannot hold - broken
 * - if half and double time are not treated as a match then a 174 master re-pitches an 87 track by 2x - broken
 * - if the mode is off and a rate other than 1 is used then the setting does nothing - broken
 * - if a missing or nonsense BPM produces a rate then a preview is re-pitched on invented data - broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/player/preview-beat-sync.ts');
});

const rate = (masterBpm, trackBpm, mode = 'tempo') =>
	mod.previewSyncRate({ mode, masterBpm, trackBpm });

test('an in-range master tempo is matched exactly', () => {
	const got = rate(128, 124);
	assert.ok(
		Math.abs(got.rate - 128 / 124) < 1e-12,
		'if a preview does not match an in-range master tempo then the operator auditions against a clashing pulse - broken'
	);
	assert.equal(got.matched, true);
});

test('a slower master is matched too (rate below 1)', () => {
	const got = rate(120, 128);
	assert.ok(Math.abs(got.rate - 120 / 128) < 1e-12);
	assert.equal(got.matched, true);
});

test('a match outside the pitch range is refused, not forced', () => {
	// 128 against 100: 1.28 straight, 0.64 halved, 2.56 doubled. Nothing fits.
	const got = rate(128, 100);
	assert.deepEqual(
		got,
		{ rate: 1, matched: false },
		'if a match outside the pitch range is forced then a preview plays at a tempo the track cannot hold - broken'
	);
});

test('half and double time count as a match and need no re-pitch', () => {
	for (const [master, track] of [
		[174, 87],
		[70, 140]
	]) {
		const got = rate(master, track);
		assert.ok(
			Math.abs(got.rate - 1) < 1e-12 && got.matched,
			`if half and double time are not treated as a match then a 174 master re-pitches an 87 track by 2x - broken (${master}/${track})`
		);
	}
});

test('a straight in-range match wins over a folded one', () => {
	// 130 against 128 is 1.0156 straight; the doubled candidate 2.03 is out of
	// range anyway, but the halved 0.5078 would be too far even if it were not.
	const got = rate(130, 128);
	assert.ok(Math.abs(got.rate - 130 / 128) < 1e-12);
});

test('the range boundary is inclusive on both sides', () => {
	const pct = mod.PREVIEW_TEMPO_RANGE_PCT;
	const atMax = rate(100 * (1 + pct / 100), 100);
	const atMin = rate(100 * (1 - pct / 100), 100);
	assert.equal(atMax.matched, true);
	assert.equal(atMin.matched, true);
	assert.equal(rate(100 * (1 + pct / 100) + 0.5, 100).matched, false);
});

test('mode off never re-pitches', () => {
	assert.deepEqual(
		rate(128, 124, 'off'),
		{ rate: 1, matched: false },
		'if the mode is off and a rate other than 1 is used then the setting does nothing - broken'
	);
});

test('missing or nonsense tempo data yields no match', () => {
	for (const [master, track] of [
		[null, 124],
		[128, null],
		[0, 124],
		[128, 0],
		[-128, 124],
		[Number.NaN, 124],
		[Number.POSITIVE_INFINITY, 124]
	]) {
		assert.deepEqual(
			rate(master, track),
			{ rate: 1, matched: false },
			`if a missing or nonsense BPM produces a rate then a preview is re-pitched on invented data - broken (${String(master)}/${String(track)})`
		);
	}
});
