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

	it('IOPIN-11 (pin 84a92dd0b175): uses the closest raw, half, or double relationship for strict-under-8 compatibility', () => {
		const { classifyBpmCompatibility } = mod;
		const raw = classifyBpmCompatibility(135.9, 128);
		assert.equal(raw.fold, 1);
		assert.ok(Math.abs(raw.absDelta - 7.9) < 1e-9);
		assert.equal(raw.compatible, true);

		const edge = classifyBpmCompatibility(136, 128);
		assert.equal(edge.compatible, false, '8 BPM exactly is outside the strict under-8 default');
		assert.equal(edge.severity, 'neutral');

		const folded = classifyBpmCompatibility(64.1, 128);
		assert.equal(folded.fold, 0.5);
		assert.equal(folded.compatible, true);
	});

	it('IOPIN-11: escalates red BPM-border severity only after 8, 16, and 24 BPM', () => {
		const { classifyBpmCompatibility } = mod;
		assert.equal(classifyBpmCompatibility(136.1, 128).severity, 'warn');
		assert.equal(classifyBpmCompatibility(144.1, 128).severity, 'danger');
		assert.equal(classifyBpmCompatibility(152.1, 128).severity, 'critical');
	});
});
