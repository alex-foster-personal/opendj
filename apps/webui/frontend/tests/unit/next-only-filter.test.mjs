import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/next-only-filter.ts');
});

describe('next-only-filter', () => {
	it('uses 6% BPM window', () => {
		const { bpmInNextWindow, NEXT_BPM_WINDOW_PCT } = mod;
		assert.equal(NEXT_BPM_WINDOW_PCT, 6);
		assert.equal(bpmInNextWindow(100, 100), true);
		assert.equal(bpmInNextWindow(106, 100), true);
		assert.equal(bpmInNextWindow(106.1, 100), false);
		assert.equal(bpmInNextWindow(null, 100), false);
		assert.equal(bpmInNextWindow(100, null), false);
		assert.equal(bpmInNextWindow(0, 100), false);
	});

	it('keeps half/double within 15 BPM as appropriate', () => {
		const { bpmInNextWindow, isAppropriateNext } = mod;
		assert.equal(bpmInNextWindow(90, 180), true);
		assert.equal(bpmInNextWindow(100, 180), true);
		assert.equal(bpmInNextWindow(106, 180), false);
		assert.equal(bpmInNextWindow(256, 128), true);
		assert.equal(isAppropriateNext({ key: '8A', bpm: 90 }, { key: '8A', bpm: 180 }), true);
	});

	it('requires Camelot family + BPM window', () => {
		const { isAppropriateNext } = mod;
		const ref = { key: '8A', bpm: 124 };
		assert.equal(isAppropriateNext({ key: '8A', bpm: 124 }, ref), true);
		assert.equal(isAppropriateNext({ key: '9A', bpm: 124 }, ref), true);
		assert.equal(isAppropriateNext({ key: '8B', bpm: 124 }, ref), true);
		assert.equal(isAppropriateNext({ key: '10A', bpm: 124 }, ref), false);
		assert.equal(isAppropriateNext({ key: '8A', bpm: 140 }, ref), false);
		assert.equal(isAppropriateNext({ key: null, bpm: 124 }, ref), false);
	});
});
