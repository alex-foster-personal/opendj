// requirement: DECKGAP-7691
// Pin 251822d6104b: each mixer item can reset when a new track loads.
// Level, trim, EQ, and filter start off. Stems start on.
// [if] an item is off [then] that value is left alone
// [if] an item is on and already neutral [then] no write
// [if] reset stems is off [then] the previous stem mix is copied, not shared
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/track-load-reset.ts');
});

function stems(muted = false) {
	const one = { muted, solo: false, gain: 0.5 };
	return {
		vocal: { ...one, muted: true, gain: 0.2 },
		instrumental: { ...one },
		drums: { ...one, solo: true },
		bass: { ...one, gain: 0.8 },
		other: { ...one }
	};
}

const flat = {
	fader: 1,
	trim: 0.5,
	eqHigh: 0.5,
	eqMid: 0.5,
	eqLow: 0.5,
	filter: 0.5
};

test('defaults reset stems only', () => {
	assert.deepEqual(mod.TRACK_LOAD_RESET_DEFAULTS, {
		level: false,
		stems: true,
		eq: false,
		trim: false,
		filter: false
	});
	assert.equal(mod.channelResetActions(mod.TRACK_LOAD_RESET_DEFAULTS, {
		...flat,
		fader: 0.2,
		eqHigh: 0.9
	}).length, 0);
});

test('parse keeps known booleans and drops the rest', () => {
	const parsed = mod.parseTrackLoadReset({
		level: true,
		stems: false,
		eq: 'yes',
		filter: true,
		extra: true
	});
	assert.equal(parsed.level, true);
	assert.equal(parsed.stems, false);
	assert.equal(parsed.eq, false);
	assert.equal(parsed.trim, false);
	assert.equal(parsed.filter, true);
	assert.deepEqual(mod.parseTrackLoadReset(null), mod.TRACK_LOAD_RESET_DEFAULTS);
});

test('level, trim, EQ, and filter write only the values that moved', () => {
	const actions = mod.channelResetActions(
		{ level: true, stems: true, eq: true, trim: true, filter: true },
		{ fader: 0.25, trim: 0.5, eqHigh: 0.8, eqMid: 0.5, eqLow: 0.1, filter: 0.7 }
	);
	assert.deepEqual(actions, [
		{ kind: 'fader', value: 1 },
		{ kind: 'eq', band: 'high', value: 0.5 },
		{ kind: 'eq', band: 'low', value: 0.5 },
		{ kind: 'filter', value: 0.5 }
	]);
});

test('stem carry is a clone when reset is off, and absent when it is on', () => {
	const current = stems();
	assert.equal(mod.stemCarryControls({ ...mod.TRACK_LOAD_RESET_DEFAULTS, stems: true }, current), null);
	const carried = mod.stemCarryControls({ ...mod.TRACK_LOAD_RESET_DEFAULTS, stems: false }, current);
	assert.deepEqual(carried, current);
	assert.notEqual(carried, current);
	assert.notEqual(carried.vocal, current.vocal);
	carried.vocal.gain = 1;
	assert.equal(current.vocal.gain, 0.2);
});

test('the warning says Open DJ resets and the controller does not', () => {
	const warning = mod.TRACK_LOAD_RESET_WARNING;
	assert.match(warning, /WARNING/);
	assert.match(warning, /Open DJ/);
	assert.match(warning, /not on your controller/);
	assert.match(warning, /disagree/);
	assert.equal(warning.includes('\u2014'), false);
	assert.equal(warning.includes('\u2013'), false);
});

test('the engine seam writes only the enabled, non-neutral values through its own setters', () => {
	mod.writeTrackLoadReset({ level: true, stems: true, eq: true, trim: false, filter: false });
	const calls = [];
	mod.applyTrackLoadChannelReset(
		{ fader: 0.2, trim: 0.9, eq_high: 0.5, eq_mid: 0.1, eq_low: 0.5, filter: 0.1 },
		{
			setFader: (v) => calls.push(['fader', v]),
			setTrim: (v) => calls.push(['trim', v]),
			setFilter: (v) => calls.push(['filter', v]),
			setEq: (band, v) => calls.push(['eq', band, v])
		}
	);
	// Control: trim and filter are off, high and low EQ are already flat.
	assert.deepEqual(calls, [['fader', 1], ['eq', 'mid', 0.5]]);
});

test('a carried stem mix reaches both the published state and the processor, and nothing else does', () => {
	const unity = { gain: 1, muted: false, solo: false };
	const mix = { vocal: { ...unity, muted: true }, instrumental: unity, drums: unity, bass: unity, other: unity };
	mod.writeTrackLoadReset({ level: false, stems: false, eq: false, trim: false, filter: false });
	mod.captureStemCarry(3, 41, mix);
	const state = { controls: null };
	const sent = [];
	mod.applyStemCarry(3, 41, state, { setControls: (c) => sent.push(c) });
	assert.equal(state.controls.vocal.muted, true);
	assert.equal(sent.length, 1);
	// Control: a newer load token takes nothing from the older snapshot.
	const later = { controls: null };
	mod.applyStemCarry(3, 42, later, { setControls: (c) => sent.push(c) });
	assert.equal(later.controls, null);
	assert.equal(sent.length, 1);
});

test('a carried stem mix is spent once: a graph rebuild on the same load keeps the live mix', () => {
	const unity = { gain: 1, muted: false, solo: false };
	const mix = { vocal: { ...unity, muted: true }, instrumental: unity, drums: unity, bass: unity, other: unity };
	mod.writeTrackLoadReset({ level: false, stems: false, eq: false, trim: false, filter: false });
	mod.captureStemCarry(2, 7, mix);
	const first = { controls: null };
	mod.applyStemCarry(2, 7, first, { setControls: () => {} });
	assert.equal(first.controls.vocal.muted, true);
	// The same token again (a rebuild or stem reload): the carry is gone, so nothing is overwritten.
	const rebuilt = { controls: 'live' };
	const sent = [];
	mod.applyStemCarry(2, 7, rebuilt, { setControls: (c) => sent.push(c) });
	assert.equal(rebuilt.controls, 'live');
	assert.equal(sent.length, 0);
});
