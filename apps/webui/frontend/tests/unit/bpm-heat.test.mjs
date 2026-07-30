import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/bpm-heat.ts');
});

describe('bpm-heat', () => {
	it('marks ±6% as sweet (bright)', () => {
		const { classifyBpmHeat } = mod;
		const hit = classifyBpmHeat(128, 128);
		assert.equal(hit.lane, 'sweet');
		assert.equal(hit.fold, 1);
		const edge = classifyBpmHeat(128 * 1.06, 128);
		assert.equal(edge.lane, 'sweet');
	});

	it('uses purple half/double within 15 BPM (180↔90)', () => {
		const { classifyBpmHeat } = mod;
		const half = classifyBpmHeat(90, 180);
		assert.equal(half.lane, 'half');
		assert.equal(half.fold, 0.5);
		const near = classifyBpmHeat(100, 180);
		assert.equal(near.lane, 'half');
		assert.ok(near.absDelta <= 15);
		const double = classifyBpmHeat(256, 128);
		assert.equal(double.lane, 'half');
		assert.equal(double.fold, 2);
	});

	it('goes red past 25 below / 30 above', () => {
		const { classifyBpmHeat } = mod;
		const slow = classifyBpmHeat(100, 128); // 28 below
		assert.equal(slow.lane, 'far');
		const fast = classifyBpmHeat(160, 128); // 32 above
		assert.equal(fast.lane, 'far');
		const mid = classifyBpmHeat(140, 128); // 12 above, under 30
		assert.equal(mid.lane, 'mid');
	});

	it('prefers half lane over far when both could apply', () => {
		const { classifyBpmHeat } = mod;
		// master 180, track 90: also "far" on 1x, but half wins
		const h = classifyBpmHeat(90, 180);
		assert.equal(h.lane, 'half');
	});
});
