/**
 * Issue #10788 (S11): the pure rules for playing a capture's stem settings (spec 3.2, 5.1), ported
 * from the v1 reference. Unity stems play the mix clip, part gains follow the deck's mute/solo gate,
 * a track with no stem-set yet shows stems pending, and the pool key follows the stem settings.
 *
 * Regression lines:
 * - if every part is unity then the mix clip plays and no stem mix is built
 * - if drums are soloed then only drums sound, and mute beats solo
 * - if the track has no stem-set then stems are pending and the mix clip plays
 * - if one gain changes then the pool key changes
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let M;
let L;
before(async () => {
	M = await loadTypeScriptModule('src/lib/player/sampler/sampler-stem-mix.ts');
	L = await loadTypeScriptModule('src/lib/player/sampler/sampler-store-logic.ts');
});

const unity = () => ({
	vocals: { gain_db: 0, mute: false, solo: false },
	drums: { gain_db: 0, mute: false, solo: false },
	bass: { gain_db: 0, mute: false, solo: false },
	inst: { gain_db: 0, mute: false, solo: false }
});
const FOUR = ['vocals', 'drums', 'bass', 'inst'];
const bassMuted = () => {
	const s = unity();
	s.bass.mute = true;
	return s;
};

test('if every part is 0 dB unmuted unsoloed (or stems is null) then stems are unity', () => {
	/** [if] stems are all unity or null [then] stemsAreUnity is true, [else stop]. */
	assert.equal(M.stemsAreUnity(unity()), true);
	assert.equal(M.stemsAreUnity(null), true);
});

test('if one part is muted, soloed or off 0 dB then stems are not unity', () => {
	/** [if] one part is muted, soloed or at -3 dB [then] stemsAreUnity is false, [else stop]. */
	for (const patch of [{ mute: true }, { solo: true }, { gain_db: -3 }]) {
		const s = unity();
		Object.assign(s.bass, patch);
		assert.equal(M.stemsAreUnity(s), false, JSON.stringify(patch));
	}
});

test('if bass is muted and the track has no stem-set then stems are pending and the mix clip plays', () => {
	/** [if] stems change the sound and stems_ready is false [then] pending, no stem mix, [else stop]. */
	const pending = { vector: { stems: bassMuted() }, stems_ready: false };
	assert.equal(L.stemsPending(pending), true);
	assert.equal(M.playsStems(pending), false);
});

test('if bass is muted and the stem-set exists then the slot plays a stem mix', () => {
	/** [if] stems change the sound and stems_ready is true [then] playsStems, not pending, [else stop]. */
	const ready = { vector: { stems: bassMuted() }, stems_ready: true };
	assert.equal(L.stemsPending(ready), false);
	assert.equal(M.playsStems(ready), true);
});

test('if the vector is unity or absent then the mix clip plays and no stem mix is built', () => {
	/** [if] stems are unity or the vector is null [then] neither playsStems nor stemsPending, [else stop]. */
	for (const vector of [{ stems: unity() }, { stems: null }, null]) {
		for (const stems_ready of [true, false]) {
			assert.equal(M.playsStems({ vector, stems_ready }), false);
			assert.equal(L.stemsPending({ vector, stems_ready }), false);
		}
	}
});

test('if drums are soloed then only drums sound', () => {
	/** [if] drums are soloed on a 4-part stem-set [then] only drums have gain 1, [else stop]. */
	const s = unity();
	s.drums.solo = true;
	assert.deepEqual(M.effectivePartGains(s, FOUR), { vocals: 0, drums: 1, bass: 0, inst: 0 });
});

test('if a part is muted and soloed then it is silent (mute wins, as on the deck)', () => {
	/** [if] vocals are muted and soloed [then] every part is silent, [else stop]. */
	const s = unity();
	s.vocals.solo = true;
	s.vocals.mute = true;
	assert.deepEqual(M.effectivePartGains(s, FOUR), { vocals: 0, drums: 0, bass: 0, inst: 0 });
});

test('if drums are soloed on a vocals+inst stem-set then solo is ignored and both parts sound', () => {
	/** [if] the soloed part is missing from the stem-set [then] the present parts all sound, [else stop]. */
	const s = unity();
	s.drums.solo = true;
	assert.deepEqual(M.effectivePartGains(s, ['vocals', 'inst']), { vocals: 1, inst: 1 });
	assert.throws(() => M.effectivePartGains(s, []), /at least one part/);
});

test('if a gain is -6 dB or the -96 dB floor then the linear gain is 10^(-6/20) or 0', () => {
	/** [if] bass is -6 dB and inst is at the floor [then] gains are 10^(-6/20) and 0, [else stop]. */
	const s = unity();
	s.bass.gain_db = -6;
	s.inst.gain_db = -96;
	const g = M.effectivePartGains(s, FOUR);
	assert.ok(Math.abs(g.bass - 10 ** (-6 / 20)) < 1e-12 && g.inst === 0, JSON.stringify(g));
});

test('if one gain changes then the pool key changes, and the mix key is slot@rate', () => {
	/** [if] a stem gain changes [then] the key changes; equal settings share a key; null is slot@rate, [else stop]. */
	const b = unity();
	b.vocals.gain_db = -1;
	assert.notEqual(M.stemMixPoolKey('s', 48000, unity()), M.stemMixPoolKey('s', 48000, b));
	assert.equal(M.stemMixPoolKey('s', 48000, unity()), M.stemMixPoolKey('s', 48000, unity()));
	assert.equal(M.stemMixPoolKey('s', 48000, null), 's@48000');
});
