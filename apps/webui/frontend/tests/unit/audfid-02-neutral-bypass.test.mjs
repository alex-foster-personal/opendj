// requirement: AUDFID-02 (neutral stem controls play the original file)
// [if] a ready bundle has every stem control at its default [then] the deck plays the original file, the stems are held ⛔️
// [if] any stem control leaves its default [then] the deck must switch to the stems ⛔️
// [if] the first touch lands while stems are held [then] the value is set on the held stems before they engage, so the press is not lost ⛔️
// [if] the engage crossfade is shorter than 10 ms, or the two ramps do not share one window [then] the switch can click ⛔️
// [if] EQ moves on a deck playing the original file [then] no switch is needed: the EQ sits after the processor ⛔️
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/stem-neutral-entry.ts');
});

const FLAT = { eq_low: 0.5, eq_mid: 0.5, eq_high: 0.5 };
const ready = (controls) => ({ status: 'ready', controls });

test('if default controls do not read as neutral then every stem track keeps playing four lossy stretchers', () => {
	assert.equal(m.stemControlsAreDefault(m.createDefaultStemControls()), true);
	assert.equal(m.deckPlaysOriginalFile(m.createDefaultStemControls()), true);
	assert.equal(m.stemControlsNeutral(ready(m.createDefaultStemControls()), FLAT), true);
});

test('if a touched control still reads as neutral then the touch is heard on the original file, i.e. lost', () => {
	for (const [field, value] of [
		['muted', true],
		['solo', true],
		['gain', 0.51]
	]) {
		const controls = m.createDefaultStemControls();
		controls.drums[field] = value;
		assert.equal(m.deckPlaysOriginalFile(controls), false, `${field}=${value}`);
		assert.equal(m.stemControlsNeutral(ready(controls), FLAT), false, `${field}=${value}`);
	}
});

test('if EQ off flat reads as stem-neutral for the fidelity guard then the guard compares a shaped master to the raw file', () => {
	assert.equal(
		m.stemControlsNeutral(ready(m.createDefaultStemControls()), { ...FLAT, eq_high: 0.3 }),
		false
	);
	assert.equal(
		m.stemControlsNeutral({ status: 'unavailable', controls: m.createDefaultStemControls() }, FLAT),
		true
	);
	assert.equal(m.channelEqIsFlat({ ...FLAT, eq_low: 0.5000001 }), false);
});

function fakeParam(log, name) {
	return {
		setValueAtTime: (v, t) => log.push([name, 'set', v, t]),
		linearRampToValueAtTime: (v, t) => log.push([name, 'ramp', v, t])
	};
}

test('if the engage crossfade is under 10 ms or the ramps use different windows then the switch can click', () => {
	assert.ok(m.STEM_ENGAGE_CROSSFADE_SEC >= 0.01);
	const log = [];
	const window = m.scheduleStemEngageCrossfade(
		fakeParam(log, 'in'),
		fakeParam(log, 'out'),
		12.5,
		m.STEM_ENGAGE_CROSSFADE_SEC
	);
	const end = 12.5 + m.STEM_ENGAGE_CROSSFADE_SEC;
	assert.deepEqual(log, [
		['in', 'set', 0, 12.5],
		['in', 'ramp', 1, end],
		['out', 'set', 1, 12.5],
		['out', 'ramp', 0, end]
	]);
	assert.deepEqual(window, { startSec: 12.5, endSec: end });
	assert.throws(
		() => m.scheduleStemEngageCrossfade(fakeParam([], 'in'), null, 1, 0.005),
		RangeError
	);
	assert.throws(
		() => m.scheduleStemEngageCrossfade(fakeParam([], 'in'), null, Number.NaN, 0.02),
		RangeError
	);
});

function heldDeck() {
	const held = Object.create(m.AlignedStemDeckProcessor.prototype);
	const applied = [];
	held.setControls = (controls) => applied.push(structuredClone(controls));
	const st = {
		stems: {
			status: 'ready',
			available_controls: ['vocal', 'instrumental', 'drums', 'bass', 'other'],
			layout: 'demucs4',
			controls: m.createDefaultStemControls(),
			error: null
		}
	};
	const mix = { setControls: () => assert.fail('the mix processor has no stem controls') };
	return { held, applied, st, rt: { processor: mix } };
}

test('if the first touch on held stems is not applied to them before they engage then the press is lost', () => {
	const { held, applied, st, rt } = heldDeck();
	const engaged = [];
	m.applyStemControl(1, 'vocal', 'muted', true, {
		requireLoaded: () => ({ st, rt }),
		getChannel: () => ({}),
		heldStems: () => held,
		engageHeldStems: (deck) => engaged.push([deck, applied.length])
	});
	assert.equal(applied.length, 1);
	assert.equal(applied[0].vocal.muted, true);
	assert.deepEqual(engaged, [[1, 1]], 'engage must run once, after the value is on the stems');
	assert.equal(st.stems.controls.vocal.muted, true);
});

test('if a touch on stems already engaged re-engages them then every press re-runs the handoff', () => {
	const { held, applied, st } = heldDeck();
	const rt = { processor: held };
	m.applyStemControl(2, 'drums', 'gain', 0.8, {
		requireLoaded: () => ({ st, rt }),
		getChannel: () => ({}),
		heldStems: () => null,
		engageHeldStems: () => assert.fail('engaged stems were engaged again')
	});
	assert.equal(applied[0].drums.gain, 0.8);
});

test('if the engine lands a neutral bundle instead of holding it, or the engage skips the crossfade, then stems play by default again', () => {
	const barrel = readFileSync(
		new URL('../../src/lib/rb/audio-engine.svelte.ts', import.meta.url),
		'utf8'
	);
	const stemRouting = readFileSync(
		new URL('../../src/lib/rb/audio-engine-stem-routing.ts', import.meta.url),
		'utf8'
	);
	const engine = `${barrel}\n${stemRouting}`;
	assert.match(
		engine,
		/crossfadeSec: STEM_ENGAGE_CROSSFADE_SEC,\n\t\tcreateGain: \(\) => ctx\.createGain\(\)/
	);
	const hold = engine.slice(engine.indexOf('function _holdNeutralStems('));
	assert.match(hold.slice(0, 600), /playback: 'original'/);
	const engage = engine.slice(engine.indexOf('function _engageHeldNeutralStems('));
	assert.match(engage.slice(0, 1400), /engageStemsOnDeck\(/);
	assert.match(
		engage.slice(0, 1400),
		/_stemUpgradeLandingPort\(deck, \{ token: held\.token, processor: held\.processor, state \}, ctx, stale, \{\n\t\t\tdeferBundle: false/
	);
	assert.match(engage.slice(0, 1200), /playback: 'stems'/);
	assert.match(
		engine,
		/heldStems: \(d: DeckId\) => _rt\[d\]\.heldNeutralStems\?\.processor \?\? null/
	);
});
