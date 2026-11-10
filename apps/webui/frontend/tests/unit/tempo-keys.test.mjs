/**
 * KEYB-11711: tempo step, reset and pitch-range cycle pure helpers.
 *
 * Regression lines:
 * - if a fine step at the range limit returns null then broken
 * - if range cycle skips a range that cannot hold the tempo then broken
 * - if tempo-keys duplicates PITCH_RANGES literally then broken
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let tempoKeys;
let constants;

before(async () => {
	tempoKeys = await loadTypeScriptModule('src/lib/rb/tempo-keys.ts');
	constants = await loadTypeScriptModule('src/lib/player/constants.ts');
});

const deck2 = (pitch, range, loaded = true) => ({
	deck: 2,
	loaded,
	pitch,
	range
});

test('deck 2 at +1.2% in range 8: steps and reset', () => {
	const { tempoKeyCommand, tempoPctFromRatio } = tempoKeys;
	const base = deck2(1.012, 8);
	assert.equal(
		tempoPctFromRatio(
			tempoKeyCommand({ kind: 'tempo-step', direction: 1, coarse: false }, base).ratio
		),
		1.3
	);
	assert.equal(
		tempoPctFromRatio(
			tempoKeyCommand({ kind: 'tempo-step', direction: -1, coarse: false }, base).ratio
		),
		1.1
	);
	assert.equal(
		tempoPctFromRatio(
			tempoKeyCommand({ kind: 'tempo-step', direction: 1, coarse: true }, base).ratio
		),
		2.2
	);
	assert.equal(
		tempoPctFromRatio(
			tempoKeyCommand({ kind: 'tempo-step', direction: -1, coarse: true }, base).ratio
		),
		0.2
	);
	const reset = tempoKeyCommand({ kind: 'tempo-reset' }, base);
	assert.deepEqual(reset, { type: 'tempo', deck: 2, ratio: 1 });
});

test('clamp top: +7.95% fine up to 8.0, further up is null', () => {
	const { tempoKeyCommand } = tempoKeys;
	const nearTop = deck2(1.0795, 8);
	const atTop = tempoKeyCommand({ kind: 'tempo-step', direction: 1, coarse: false }, nearTop);
	assert.equal(atTop.ratio, 1.08);
	const blocked = tempoKeyCommand(
		{ kind: 'tempo-step', direction: 1, coarse: false },
		deck2(atTop.ratio, 8)
	);
	assert.equal(blocked, null);
	const coarse = tempoKeyCommand(
		{ kind: 'tempo-step', direction: 1, coarse: true },
		deck2(1.075, 8)
	);
	assert.equal(coarse.ratio, 1.08);
});

test('clamp bottom: -7.95% fine down to -8.0, further down is null', () => {
	const { tempoKeyCommand } = tempoKeys;
	const nearBot = deck2(0.9205, 8);
	const atBot = tempoKeyCommand({ kind: 'tempo-step', direction: -1, coarse: false }, nearBot);
	assert.equal(atBot.ratio, 0.92);
	const blocked = tempoKeyCommand(
		{ kind: 'tempo-step', direction: -1, coarse: false },
		deck2(atBot.ratio, 8)
	);
	assert.equal(blocked, null);
});

test('reset at 0% and empty deck send nothing', () => {
	const { tempoKeyCommand } = tempoKeys;
	assert.equal(tempoKeyCommand({ kind: 'tempo-reset' }, deck2(1, 8)), null);
	assert.equal(
		tempoKeyCommand({ kind: 'tempo-step', direction: 1, coarse: false }, deck2(1, 8, false)),
		null
	);
	assert.equal(tempoKeyCommand({ kind: 'pitch-range-cycle' }, null), null);
});

test('pitch range cycle wraps and skips ranges that cannot hold tempo', () => {
	const { tempoKeyCommand, nextPitchRange } = tempoKeys;
	const { PITCH_RANGES } = constants;
	assert.deepEqual(tempoKeyCommand({ kind: 'pitch-range-cycle' }, deck2(1, 8)), {
		type: 'pitch_range',
		deck: 2,
		range: 16
	});
	assert.deepEqual(tempoKeyCommand({ kind: 'pitch-range-cycle' }, deck2(1, 16)), {
		type: 'pitch_range',
		deck: 2,
		range: 100
	});
	assert.deepEqual(tempoKeyCommand({ kind: 'pitch-range-cycle' }, deck2(1, 100)), {
		type: 'pitch_range',
		deck: 2,
		range: 8
	});
	assert.equal(nextPitchRange(100, 12), 16);
	assert.equal(nextPitchRange(100, 50), null);
	let r = PITCH_RANGES[0];
	const seen = new Set();
	for (let i = 0; i < PITCH_RANGES.length; i++) {
		seen.add(r);
		r = nextPitchRange(r, 0);
	}
	assert.equal(seen.size, PITCH_RANGES.length);
	assert.equal(r, PITCH_RANGES[0]);
});

test('tempo-keys.ts imports PITCH_RANGES without a second range literal', async () => {
	const source = await readFile('src/lib/rb/tempo-keys.ts', 'utf8');
	assert.match(source, /PITCH_RANGES/);
	assert.doesNotMatch(source, /\[\s*8\s*,\s*16\s*,\s*100\s*\]/);
});
