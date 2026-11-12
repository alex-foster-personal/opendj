/**
 * Beat-jump display formatting boundaries (#13105, DECKUX-10470).
 *
 * [if] a whole-bar beat jump is formatted [then the ladder reads 1 through 64]
 * [if] a fractional beat jump is formatted [then half-bar reads 1/2 signed or not]
 * [if] a negative jump is formatted [then the sign prefixes the magnitude string]
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const BEAT_JUMP_SIZES = [0.5, 1, 2, 4, 8, 16, 32, 64];

let formatBeatJumpMagnitude;
let formatBeatJumpDelta;

before(async () => {
	({ formatBeatJumpMagnitude, formatBeatJumpDelta } = await loadTypeScriptModule(
		'src/lib/rb/format-beat-jump.ts'
	));
});

describe('formatBeatJumpMagnitude', () => {
	it('formats fractional half-bar as 1/2', () => {
		assert.equal(formatBeatJumpMagnitude(0.5), '1/2');
	});

	it('formats whole-bar ladder sizes as integer strings', () => {
		for (const size of [1, 2, 4, 8, 16, 32, 64]) {
			assert.equal(formatBeatJumpMagnitude(size), String(size));
		}
	});

	it('covers all BEAT_JUMP_SIZES without drift', () => {
		const expected = { 0.5: '1/2', 1: '1', 2: '2', 4: '4', 8: '8', 16: '16', 32: '32', 64: '64' };
		for (const size of BEAT_JUMP_SIZES) {
			assert.equal(formatBeatJumpMagnitude(size), expected[size]);
		}
	});

	it('does not normalize negative magnitudes (callers use Math.abs)', () => {
		assert.equal(formatBeatJumpMagnitude(-0.5), '-0.5');
	});
});

describe('formatBeatJumpDelta', () => {
	it('prefixes whole-bar forward jumps with +', () => {
		assert.equal(formatBeatJumpDelta(4), '+4');
		assert.equal(formatBeatJumpDelta(8), '+8');
		assert.equal(formatBeatJumpDelta(64), '+64');
	});

	it('prefixes whole-bar back jumps with -', () => {
		assert.equal(formatBeatJumpDelta(-4), '-4');
		assert.equal(formatBeatJumpDelta(-8), '-8');
	});

	it('formats fractional forward and back jumps with 1/2', () => {
		assert.equal(formatBeatJumpDelta(0.5), '+1/2');
		assert.equal(formatBeatJumpDelta(-0.5), '-1/2');
	});

	it('pins zero delta as -0 (not +0)', () => {
		assert.equal(formatBeatJumpDelta(0), '-0');
	});
});
