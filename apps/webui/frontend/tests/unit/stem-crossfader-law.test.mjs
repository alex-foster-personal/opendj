// requirement: STEMPERF-14592
/**
 * Stem crossfader lane law (src/lib/rb/stem-crossfader-law.ts), issue #14574.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let law;

before(async () => {
	law = await loadTypeScriptModule('src/lib/rb/stem-crossfader-law.ts');
});

const IDS = ['vocal', 'instrumental', 'drums', 'bass', 'other'];
const POSITIONS = [0, 0.25, 0.5, 0.75, 1];
const mainA = (x) => Math.cos((x * Math.PI) / 2);
const mainB = (x) => Math.cos(((1 - x) * Math.PI) / 2);
const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-12, `${a} != ${b}`);

test('linked lanes equal the main crossfader law for every stem and position', () => {
	/** [if] a linked lane drifts from the main law [then] stems double-fade against the deck bus, [else stop]. */
	for (const x of POSITIONS) {
		const state = law.defaultStemCrossfaderState(0.5);
		for (const id of IDS) {
			const g = law.stemLaneGains(state, x, id);
			close(g.a, mainA(x));
			close(g.b, mainB(x));
			close(law.stemLaneGain(state, x, 'A', id), mainA(x));
			close(law.stemLaneGain(state, x, 'B', id), mainB(x));
		}
	}
});

test('unlinked vocal reads its own lane, drums follow main, relink snaps back', () => {
	/** [if] unlinking one lane moves another or relink keeps the old spot [then] the swap move breaks, [else stop]. */
	const s0 = law.defaultStemCrossfaderState(0.5);
	const s1 = law.setStemLane(s0, 'vocal', { value: 0, linked: false }, 0.5);
	const v = law.stemLaneGains(s1, 0.5, 'vocal');
	close(v.a, 1);
	close(v.b, 0);
	const d = law.stemLaneGains(s1, 0.5, 'drums');
	close(d.a, mainA(0.5));
	close(d.b, mainB(0.5));
	const s2 = law.setStemLane(s1, 'vocal', { linked: true }, 0.5);
	assert.deepEqual(s2.vocal, { position: 0.5, linked: true });
	close(law.stemLaneGains(s2, 0.5, 'vocal').a, mainA(0.5));
});

test('THRU decks get gain 1 on every part whatever the lanes say', () => {
	/** [if] a THRU deck reads a lane [then] parts fade on a deck the crossfader must not touch, [else stop]. */
	let s = law.defaultStemCrossfaderState(0);
	for (const id of IDS) s = law.setStemLane(s, id, { value: 1, linked: false }, 0);
	for (const x of POSITIONS)
		for (const id of IDS) assert.equal(law.stemLaneGain(s, x, 'THRU', id), 1);
});

test('setStemLane clamps values and never mutates its input', () => {
	/** [if] a lane update leaves 0..1 or mutates state [then] mirrors and undo read wrong lanes, [else stop]. */
	const s0 = law.defaultStemCrossfaderState(0.5);
	const snap = structuredClone(s0);
	const hi = law.setStemLane(s0, 'bass', { value: 7 }, 0.5);
	const lo = law.setStemLane(s0, 'bass', { value: -3 }, 0.5);
	assert.deepEqual(hi.bass, { position: 1, linked: false });
	assert.deepEqual(lo.bass, { position: 0, linked: false });
	assert.deepEqual(s0, snap);
	assert.notEqual(hi, s0);
});
